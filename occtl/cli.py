from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import socket
import sys
import termios
import time
import tty
import urllib.request
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None  # type: ignore[misc,assignment]

from . import clipboard, config, mailbox, tmux
from .notify import alert_router_webhook, discord_webhook, mac_notify
from .relay import serve as serve_relay
from .voice import parse_voice

COMMANDS = (
    "map",
    "maps",
    "new",
    "ensure",
    "ls",
    "focus",
    "focused",
    "status",
    "say",
    "enter",
    "attach",
    "kill",
    "watch",
    "set-webhook",
    "set-alert-router",
    "set-relay-token",
    "relay",
    "voice",
    "mailbox",
    "clipboard",
    "completion",
)

WAIT_PATTERNS = (
    r"press enter",
    r"awaiting input",
    r"continue\?",
    r"\bcontinue\b",
    r"\(y/n\)",
    r"user input required",
    r"confirm\?",
)

STALL_PATTERNS = (
    r"thinking:\s+planning",
    r"planning phase\s+\d+",
    r"spawning planner\.{0,3}",
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _match_wait_pattern(pane_text: str) -> str | None:
    for pattern in WAIT_PATTERNS:
        if re.search(pattern, pane_text):
            return pattern
    return None


def _match_stall_pattern(pane_text: str) -> str | None:
    for pattern in STALL_PATTERNS:
        if re.search(pattern, pane_text):
            return pattern
    return None


def _snippet_for_pattern(pane_text: str, pattern: str) -> str:
    lines = [line.strip() for line in pane_text.splitlines() if line.strip()]
    for line in reversed(lines):
        if re.search(pattern, line.lower()):
            return _truncate_snippet(line)
    if lines:
        return _truncate_snippet(lines[-1])
    return ""


def _truncate_snippet(text: str, limit: int = 160) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _session_context(session: str) -> tuple[str, str]:
    project_dir = config.get_mapping(session)
    if not project_dir:
        focus = config.get_focus()
        if focus:
            focus_dir = config.get_mapping(focus)
            if focus_dir:
                project_dir = f"{focus_dir} (focus:{focus})"
    if not project_dir:
        project_dir = "(unmapped)"
    host = socket.gethostname()
    return project_dir, host


def _in_ssh_session() -> bool:
    return bool(
        os.environ.get("SSH_CONNECTION")
        or os.environ.get("SSH_CLIENT")
        or os.environ.get("SSH_TTY")
    )


def _clipboard_attach_hints() -> list[str]:
    try:
        data = clipboard.status(tmux_socket=None)
    except clipboard.ClipboardError:
        return []

    reasons = set(data.get("reasons", []))
    verification = data.get("verification", {})
    ssh_session = _in_ssh_session()
    selected_mode = data.get("selected_mode", "")

    if not ssh_session and not selected_mode:
        return []

    hints: list[str] = []

    if not data.get("configured_on_disk"):
        if ssh_session:
            hints.append(
                "clipboard: SSH copy is not configured on this host; run "
                "`oc clipboard setup --mode auto --reload`"
            )
            hints.append(
                "clipboard: after setup, in iTerm2 use Option-drag for local visual copy, or "
                "use `Ctrl-b` `[` for tmux copy mode; then run "
                "`oc clipboard verify` to confirm local paste works"
            )
        return hints

    if data.get("tmux_socket_ambiguous"):
        hints.append(
            "clipboard: multiple tmux servers detected; find the right server with "
            "`oc clipboard status --tmux-socket <path>` and reload that tmux instance"
        )
        return hints

    loaded_in_tmux = data.get("loaded_in_tmux")
    if loaded_in_tmux is False:
        hints.append(
            "clipboard: config is installed but not loaded in tmux; run "
            "`oc clipboard setup --mode auto --reload` or `tmux source-file ~/.tmux.conf`"
        )
    elif loaded_in_tmux is None and ssh_session:
        hints.append(
            "clipboard: tmux status could not be checked from this shell; if copy fails, run "
            "`oc clipboard status` or `oc clipboard setup --mode auto --reload` inside tmux"
        )

    if data.get("helper_health") is False:
        hints.append(
            "clipboard: OSC52 helper is missing or unhealthy; rerun "
            "`oc clipboard setup --mode auto --reload`"
        )

    if verification.get("emission_verified") and not verification.get("clipboard_verified"):
        hints.append(
            "clipboard: OSC52 emits but paste was not confirmed; run `oc clipboard verify`, and if "
            "it still fails, enable OSC52 clipboard access in your terminal"
        )
    elif ssh_session and selected_mode == "osc52" and not verification.get("verified_at"):
        hints.append(
            "clipboard: not yet verified in this terminal; run `oc clipboard verify` if copy fails"
        )

    if hints and ssh_session and selected_mode == "osc52" and "tmux_not_loaded" not in reasons:
        hints.append(
            "clipboard: in iTerm2, Option-drag does local copy; for tmux-aware copy, "
            "use `Ctrl-b` `[`"
        )

    return hints


def _ensure_clipboard_for_attach() -> list[str]:
    try:
        data = clipboard.status(tmux_socket=None)
    except clipboard.ClipboardError as e:
        return [f"clipboard: status check failed ({e})"]

    if data.get("tmux_socket_ambiguous"):
        return []

    # "scroll" keeps mouse on for scrolling while still wiring OSC52/copy bindings.
    # "tmux" is also acceptable (user explicitly chose full tmux mouse). Both satisfy attach.
    # "terminal" disables mouse entirely (breaks scrolling) — upgrade it on every attach.
    needs_setup = (
        not data.get("configured_on_disk")
        or data.get("loaded_in_tmux") is False
        or data.get("mouse_mode") not in {"tmux"}
    )
    if not needs_setup:
        return []

    try:
        clipboard.setup(
            mode="auto",
            tmux_conf=None,
            tmux_socket=None,
            dry_run=False,
            print_snippet=False,
            reload_tmux=True,
            bind_keys="copy-mode-y",
            follow_symlink=False,
            mouse_mode="tmux",
        )
    except clipboard.ClipboardError as e:
        return [f"clipboard: auto-setup failed ({e})"]
    return []


def _relay_status() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8878/health", timeout=0.8) as resp:
            body = resp.read().decode("utf-8", errors="ignore").lower()
            if resp.status == 200 and '"status": "ok"' in body:
                return "up"
    except Exception:
        pass
    return "down"


def _send_waiting_alert(
    *,
    session: str,
    title: str,
    reason: str,
    detail: str,
    severity: str,
    status: str,
    fingerprint_suffix: str,
    snippet: str = "",
) -> None:
    project_dir, host = _session_context(session)
    body = (
        f"{reason}; session={session}; project={project_dir}; host={host}; detail={detail}"
        f"; snippet={snippet or '(none)'}"
    )
    mac_notify(title, body)
    discord_webhook(config.get_webhook(), f"**{title}**\n{body}")
    alert_router_webhook(
        config.get_alert_router(),
        service_name=f"oc-watch:{session}",
        severity=severity,
        status=status,
        host_name=host,
        message=body,
        fingerprint=f"oc-watch-{session}-{fingerprint_suffix}",
    )


def _fmt_cmds_for_shell(commands: Sequence[str]) -> str:
    return " ".join(commands)


def cmd_map(args: argparse.Namespace) -> int:
    config.set_mapping(args.name, args.path)
    print(f"mapped: {args.name} -> {config.get_mapping(args.name)}")
    return 0


def cmd_maps(_: argparse.Namespace) -> int:
    m = config.load_mappings()
    if not m:
        print("(no mappings)")
        return 0
    for k in sorted(m.keys()):
        print(f"{k}\t{m[k]}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    name = args.name
    if tmux.has_session(name):
        config.set_focus(name)
        print(f"exists+focused: {name}")
        return 0

    workdir = config.get_mapping(name)
    if not workdir:
        print(f"No mapping for '{name}'. Add one:\n  oc map {name} /path/to/project")
        return 1

    if not Path(workdir).exists():
        print(f"Mapped directory does not exist: {workdir}")
        return 1

    tmux.new_session(name, workdir)
    tmux.send_keys(f"{name}:main", ["opencode", "Enter"])
    tmux.new_window(name, "logs", workdir)
    tmux.new_window(name, "shell", workdir)

    config.set_focus(name)
    print(f"created+focused: {name}\tdir={workdir}")
    return 0


def cmd_ensure(args: argparse.Namespace) -> int:
    name = args.name
    if not tmux.has_session(name):
        return cmd_new(argparse.Namespace(name=name))
    config.set_focus(name)
    print(f"focused: {name}")
    return 0


def cmd_ls(_: argparse.Namespace) -> int:
    rows = tmux.list_sessions()
    if not rows:
        print("(no tmux sessions)")
        return 0
    for r in rows:
        print(f"{r['name']}\tattached={int(r['attached'])}\twindows={r['windows']}")
    return 0


def cmd_focus(args: argparse.Namespace) -> int:
    config.set_focus(args.name)
    print(f"focused: {args.name}")
    return 0


def cmd_focused(_: argparse.Namespace) -> int:
    print(config.get_focus())
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    focus = config.get_focus()
    webhook = config.get_webhook()
    alert_router = config.get_alert_router()
    relay_token = config.get_relay_token()
    m = config.load_mappings()

    print(f"focus:\t{focus or '(none)'}")
    if focus and focus in m:
        print(f"dir:\t{m[focus]}")
    else:
        print("dir:\t(n/a)")
    print(f"webhook:\t{'set' if webhook else '(none)'}")
    print(f"alert_router:\t{'set' if alert_router else '(none)'}")
    print(f"relay_token:\t{'set' if relay_token else '(none)'}")
    print(f"relay:\t{_relay_status()}")

    try:
        if focus and tmux.has_session(focus):
            last = tmux.pane_last_activity(focus, "main")
            now = int(time.time())
            delta = now - last if last > 0 else 0
            print(f"idle_seconds:\t{delta}")
        else:
            print("idle_seconds:\t(n/a)")
    except tmux.TmuxError:
        print("idle_seconds:\t(n/a)")
    return 0


def _resolve_session(explicit: str | None) -> str | None:
    return explicit or (config.get_focus() or None)


def cmd_say(args: argparse.Namespace) -> int:
    session = _resolve_session(args.session)
    if not session:
        print("no focused session; run: oc focus <name>")
        return 1
    if not tmux.has_session(session):
        print(f"session not found: {session}")
        return 1
    tmux.send_keys(f"{session}:main", [args.text, "Enter"])
    print(f"sent: {session}\t{args.text}")
    return 0


def cmd_enter(args: argparse.Namespace) -> int:
    session = _resolve_session(args.session)
    if not session:
        print("no focused session; run: oc focus <name>")
        return 1
    if not tmux.has_session(session):
        print(f"session not found: {session}")
        return 1
    tmux.send_keys(f"{session}:main", ["Enter"])
    print(f"enter: {session}")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    target = args.name or _choose_attach_session_interactive()
    if not target:
        print("attach cancelled")
        return 1
    session = _session_from_tmux_target(target)

    if not tmux.has_session(session):
        if config.get_mapping(session):
            rc = cmd_new(argparse.Namespace(name=session))
            if rc != 0:
                return rc
        else:
            print(f"session not found: {session}")
            return 1

    config.set_focus(session)
    config.touch_recent_attach(session)
    for warning in _ensure_clipboard_for_attach():
        print(warning)
    for hint in _clipboard_attach_hints():
        print(hint)
    tmux.attach(target, control_mode=bool(getattr(args, "cc", False)))
    return 0


def _build_attach_menu_rows(
    expanded: set[str] | None = None, expanded_sessions: set[str] | None = None
) -> list[dict[str, object]]:
    """Build the list of visible menu rows.

    Mappings with multiple running instances are rendered as a collapsible group:
    - row_type "group"  — parent row showing the mapping name and instance count
    - row_type "child"  — indented child rows (only emitted when the group is expanded)
    Mappings with 0 or 1 instance and unclaimed sessions are row_type "leaf".
    """
    if expanded is None:
        expanded = set()
    if expanded_sessions is None:
        expanded_sessions = set()

    mappings = config.load_mappings()
    all_sessions = tmux.list_sessions_with_paths()
    window_details = tmux.list_window_details()
    recent = config.get_recent_attaches()
    recent_rank = {name: i for i, name in enumerate(recent)}
    focus = config.get_focus()
    mailbox_links: dict[str, str] = {}

    def _resolve_path(p: str) -> str:
        try:
            return str(Path(p).expanduser().resolve())
        except Exception:
            return p

    def _instances_for_mapping(mapping_name: str, mapped_dir: str) -> list[dict]:
        canonical = _resolve_path(mapped_dir) if mapped_dir else ""
        return [
            s
            for s in all_sessions
            if s["name"] == mapping_name or (canonical and _resolve_path(s["path"]) == canonical)
        ]

    def _mailbox_info(mapped_dir: str, session_name: str) -> tuple[str, str]:
        if not mapped_dir:
            return "", ""
        canonical = _resolve_path(mapped_dir)
        if canonical not in mailbox_links:
            mailbox_links.update(
                {
                    f"{canonical}|{target}": label
                    for target, label in mailbox.linked_targets(canonical).items()
                }
            )
        prefix = f"{canonical}|{session_name}:"
        for key, label in mailbox_links.items():
            if key.startswith(prefix):
                return label, key.removeprefix(f"{canonical}|")
        return "", ""

    def _window_fields(session_name: str) -> dict[str, object]:
        return window_details.get(
            session_name,
            {
                "active_window": "",
                "active_command": "",
                "main_command": "",
                "window_list": [],
            },
        )

    def _append_window_rows(
        *,
        out: list[dict[str, object]],
        session_row: dict[str, object],
        mapped_dir: str,
        mapping_name: str,
    ) -> None:
        if str(session_row["name"]) not in expanded_sessions:
            return
        mailbox_target = str(session_row.get("mailbox_target") or "")
        windows = session_row.get("window_list", [])
        if not isinstance(windows, list):
            return
        for window in windows:
            if not isinstance(window, dict):
                continue
            window_name = str(window.get("name") or "")
            if not window_name:
                continue
            target = f"{session_row['name']}:{window_name}"
            command = str(window.get("command") or "")
            out.append(
                {
                    "row_type": "window",
                    "name": target,
                    "display_name": window_name,
                    "session_name": str(session_row["name"]),
                    "window_name": window_name,
                    "mapping_name": mapping_name,
                    "mapped_dir": mapped_dir,
                    "running": True,
                    "attached": session_row.get("attached", False),
                    "windows": 1,
                    "focused": session_row.get("focused", False),
                    "mailbox_role": session_row.get("mailbox_role", "")
                    if target == mailbox_target
                    else "",
                    "mailbox_target": mailbox_target if target == mailbox_target else "",
                    "active_window": window_name,
                    "active_command": command,
                    "main_command": "",
                    "window_list": [],
                    "window_active": bool(window.get("active")),
                    "command": command,
                    "exit": False,
                }
            )

    claimed_names: set[str] = set()
    mapping_instances: dict[str, list[dict]] = {}
    for mapping_name, mapped_dir in mappings.items():
        instances = _instances_for_mapping(mapping_name, mapped_dir)
        mapping_instances[mapping_name] = instances
        for s in instances:
            claimed_names.add(s["name"])

    rows: list[dict[str, object]] = []

    for mapping_name in sorted(
        mappings.keys(),
        key=lambda n: (recent_rank.get(n, len(recent_rank) + 1), n),
    ):
        mapped_dir = mappings[mapping_name]
        instances = mapping_instances[mapping_name]

        if len(instances) > 1:
            is_expanded = mapping_name in expanded
            rows.append(
                {
                    "row_type": "group",
                    "name": mapping_name,
                    "mapping_name": mapping_name,
                    "mapped_dir": mapped_dir,
                    "running": True,
                    "attached": any(s["attached"] for s in instances),
                    "windows": sum(s["windows"] for s in instances),
                    "focused": any(s["name"] == focus for s in instances),
                    "expanded": is_expanded,
                    "children": instances,
                    "mailbox_role": "",
                    "exit": False,
                }
            )
            if is_expanded:
                for j, sess in enumerate(instances, 1):
                    row = {
                        "row_type": "child",
                        "name": sess["name"],
                        "display_name": f"{mapping_name} {j}",
                        "mapping_name": mapping_name,
                        "mapped_dir": mapped_dir,
                        "running": True,
                        "attached": sess["attached"],
                        "windows": sess["windows"],
                        "focused": sess["name"] == focus,
                        "mailbox_role": _mailbox_info(mapped_dir, str(sess["name"]))[0],
                        "mailbox_target": _mailbox_info(mapped_dir, str(sess["name"]))[1],
                        "expanded_sessions": expanded_sessions,
                        **_window_fields(str(sess["name"])),
                        "exit": False,
                    }
                    rows.append(row)
                    _append_window_rows(
                        out=rows,
                        session_row=row,
                        mapped_dir=mapped_dir,
                        mapping_name=mapping_name,
                    )
        elif len(instances) == 1:
            sess = instances[0]
            row = {
                "row_type": "leaf",
                "name": sess["name"],
                "mapping_name": mapping_name,
                "mapped_dir": mapped_dir,
                "running": True,
                "attached": sess["attached"],
                "windows": sess["windows"],
                "focused": sess["name"] == focus,
                "mailbox_role": _mailbox_info(mapped_dir, str(sess["name"]))[0],
                "mailbox_target": _mailbox_info(mapped_dir, str(sess["name"]))[1],
                "expanded_sessions": expanded_sessions,
                **_window_fields(str(sess["name"])),
                "exit": False,
            }
            rows.append(row)
            _append_window_rows(
                out=rows,
                session_row=row,
                mapped_dir=mapped_dir,
                mapping_name=mapping_name,
            )
        else:
            rows.append(
                {
                    "row_type": "leaf",
                    "name": mapping_name,
                    "mapping_name": mapping_name,
                    "mapped_dir": mapped_dir,
                    "running": False,
                    "attached": False,
                    "windows": 0,
                    "focused": mapping_name == focus,
                    "mailbox_role": "",
                    "mailbox_target": "",
                    "active_window": "",
                    "active_command": "",
                    "main_command": "",
                    "window_list": [],
                    "exit": False,
                }
            )

    for s in all_sessions:
        if s["name"] not in claimed_names:
            row = {
                "row_type": "leaf",
                "name": s["name"],
                "mapping_name": s["name"],
                "mapped_dir": "",
                "running": True,
                "attached": s["attached"],
                "windows": s["windows"],
                "focused": s["name"] == focus,
                "mailbox_role": "",
                "mailbox_target": "",
                "expanded_sessions": expanded_sessions,
                **_window_fields(str(s["name"])),
                "exit": False,
            }
            rows.append(row)
            _append_window_rows(out=rows, session_row=row, mapped_dir="", mapping_name=s["name"])

    rows.append(
        {
            "row_type": "exit",
            "name": "Exit",
            "mapping_name": "",
            "mapped_dir": "",
            "running": False,
            "attached": False,
            "windows": 0,
            "focused": False,
            "mailbox_role": "",
            "mailbox_target": "",
            "active_window": "",
            "active_command": "",
            "main_command": "",
            "window_list": [],
            "exit": True,
        }
    )
    return rows


def _fit_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _compact_path(path: str, max_segments: int = 3) -> str:
    if not path:
        return "(unmapped)"
    home = str(Path.home())
    shown = path.replace(home, "~")
    parts = shown.split("/")
    if len(parts) <= max_segments + 1:
        return shown
    tail = "/".join(parts[-max_segments:])
    return f".../{tail}"


def _box_top(inner: int, title: str = "") -> str:
    if title:
        t = f" {title} "
        dashes = max(0, inner - len(t) - 1)
        return "┌─" + t + "─" * dashes + "┐"
    return "┌" + "─" * inner + "┐"


def _box_mid(inner: int) -> str:
    return "├" + "─" * inner + "┤"


def _box_bot(inner: int) -> str:
    return "└" + "─" * inner + "┘"


def _menu_row(text: str, inner_width: int) -> str:
    visible = len(ANSI_RE.sub("", text))
    if visible > inner_width:
        text = _fit_text(ANSI_RE.sub("", text), inner_width)
        visible = len(text)
    return "│" + text + (" " * max(0, inner_width - visible)) + "│"


def _visible_ljust(text: str, width: int) -> str:
    vis = len(ANSI_RE.sub("", text))
    return text + " " * max(0, width - vis)


def _supports_color() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    term = os.environ.get("TERM", "")
    return term != "dumb"


def _colorize(text: str, code: str) -> str:
    if not _supports_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _session_idle_seconds(name: str) -> int | None:
    try:
        if not tmux.has_session(name):
            return None
        last = tmux.pane_last_activity(name, "main")
        if last <= 0:
            return 0
        return max(0, int(time.time()) - last)
    except tmux.TmuxError:
        return None


def _window_badge(row: dict[str, object]) -> str:
    if not row.get("running"):
        return ""
    main_command = str(row.get("main_command") or "")
    active_window = str(row.get("active_window") or "")
    active_command = str(row.get("active_command") or "")

    parts: list[str] = []
    if main_command:
        parts.append(f"main:{main_command}")
    if active_window and active_window != "main":
        active = f"active:{active_window}"
        if active_command:
            active += f":{active_command}"
        parts.append(active)
    return " ".join(parts)


_VERSION = "0.8.0"

# Visible width of the status indicator ("● running" / "○ stopped")
_STATUS_W = 9
# Right-side padding between the status indicator and the border
_RIGHT_MARGIN = 6


def _render_attach_menu(
    rows: list[dict[str, object]],
    idx: int,
    *,
    host: str = "",
    focus: str = "",
    mailbox_mode: bool = False,
    mailbox_selection: list[str] | None = None,
    notice: str = "",
) -> None:
    cols = shutil.get_terminal_size(fallback=(100, 30)).columns
    inner = max(40, cols - 2)  # full terminal width, minus the two border chars
    # Layout per row: "  {cursor} {name_w}  {status}{_RIGHT_MARGIN}"
    name_w = max(16, min(60, inner - 4 - 2 - _STATUS_W - _RIGHT_MARGIN))
    gap = max(2, inner - 4 - name_w - _STATUS_W - _RIGHT_MARGIN)

    now = datetime.now().strftime("%H:%M")
    info = f"  {host}  ·  {focus}  ·  {now}  ·  v{_VERSION}"
    if mailbox_mode:
        hints = "  MAILBOX MODE · Enter select two sessions · m cancel · q quit"
    else:
        hints = (
            "  ↑↓/jk · Enter open · m mailbox · → expand · ← collapse · "
            "n new · x kill-window · r remap · q quit"
        )

    lines: list[str] = [
        "\033[2J\033[H",
        _box_top(inner, "OC SESSION MANAGER"),
        _menu_row(_colorize(info, "2"), inner),
        _box_mid(inner),
        _menu_row(_colorize(hints, "2"), inner),
        _box_mid(inner),
        _menu_row(
            _colorize("  SESSION".ljust(name_w + 4), "2") + " " * gap + _colorize("STATE", "2"),
            inner,
        ),
        _box_mid(inner),
    ]

    for i, row in enumerate(rows):
        selected = i == idx
        cursor = ">" if selected else " "
        row_type = row.get("row_type", "leaf")

        if row["exit"]:
            lines.append(_box_mid(inner))
            line = _menu_row(f"  {cursor} Exit", inner)
            lines.append(f"\033[7m{line}\033[0m" if selected else line)
            continue

        if bool(row["running"]):
            state = _colorize("● running", "32")
        else:
            state = _colorize("○ stopped", "2")

        if row_type == "group":
            arrow = "▾" if row["expanded"] else "▸"
            n = len(list(row.get("children", [])))
            raw = f"{arrow} {row['name']} [{n}]"
            display = _colorize(_fit_text(raw, name_w), "1")
        elif row_type == "child":
            label = str(row.get("display_name") or row["name"])
            arrow = "▾" if str(row["name"]) in row.get("expanded_sessions", set()) else "▸"
            display = _fit_text(f"  └ {arrow} {label}", name_w)
        elif row_type == "window":
            label = str(row.get("display_name") or row["name"])
            command = str(row.get("command") or "")
            marker = "*" if row.get("window_active") else " "
            suffix = f" {command}" if command else ""
            display = _fit_text(f"      └ {marker} {label}{suffix}", name_w)
        else:
            prefix = ""
            if row.get("running") and int(row.get("windows") or 0) > 1:
                prefix = "▾ " if str(row["name"]) in row.get("expanded_sessions", set()) else "▸ "
            display = _fit_text(f"{prefix}{row['name']}", name_w)

        role = str(row.get("mailbox_role") or "")
        if role:
            display = _fit_text(f"{display} [{role}]", name_w)
        badge = _window_badge(row)
        if badge:
            display = _fit_text(f"{display} {badge}", name_w)
        if mailbox_selection and str(row["name"]) in mailbox_selection:
            display = _fit_text(f"* {display}", name_w)

        left = f"  {cursor} {_visible_ljust(display, name_w)}"
        row_text = left + " " * gap + state + " " * _RIGHT_MARGIN
        line = _menu_row(row_text, inner)
        lines.append(f"\033[7m{line}\033[0m" if selected else line)

    lines.append(_box_mid(inner))

    # Footer — each piece on its own line, default (cream) colour
    sel = rows[idx]
    if sel["exit"]:
        footer_lines = ["  Exit without attaching"]
    else:
        mapped = _compact_path(str(sel["mapped_dir"]))
        row_type = sel.get("row_type", "leaf")
        if row_type == "group":
            action = "collapse" if sel["expanded"] else "expand"
        elif row_type == "window":
            action = "attach window"
        elif bool(sel["running"]):
            action = "attach"
        else:
            action = "start + attach"
        idle = (
            _session_idle_seconds(str(sel["name"]))
            if bool(sel["running"]) and row_type != "window"
            else None
        )
        idle_str = f"  idle {idle}s" if idle is not None else ""
        footer_lines = [
            f"  {sel['name']}  ·  {action}{idle_str}",
            f"  {mapped}",
        ]
        if sel.get("mailbox_role"):
            target = str(sel.get("mailbox_target") or "")
            suffix = f" target {target}" if target else ""
            footer_lines.append(f"  mailbox: {sel['mailbox_role']}{suffix}")
        active_window = str(sel.get("active_window") or "")
        active_command = str(sel.get("active_command") or "")
        main_command = str(sel.get("main_command") or "")
        if main_command:
            footer_lines.append(f"  main: {main_command}")
        if active_window:
            active = f"  active window: {active_window}"
            if active_command:
                active += f" ({active_command})"
            footer_lines.append(active)
        if mailbox_mode:
            selected = ", ".join(mailbox_selection or []) or "none"
            footer_lines.append(f"  mailbox selection: {selected}")
        if sel["mapped_dir"]:
            footer_lines.append("  n: spawn another instance")
        if row_type == "window":
            footer_lines.append("  x: kill this tmux window")
    if notice:
        footer_lines.append(f"  {notice}")

    for fl in footer_lines:
        lines.append(_menu_row(fl, inner))
    lines.append(_box_bot(inner))

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _read_menu_key(fd: int) -> str:
    # Use os.read on the raw fd to bypass Python's text/binary buffer layers.
    # Python's TextIOWrapper + BufferedReader have separate internal buffers that
    # make peek() and select() disagree, causing spurious 75ms timeouts on arrow keys.
    try:
        ch = os.read(fd, 1)
    except OSError:
        return "other"
    if ch == b"\x1b":
        # Check the kernel buffer directly — no Python buffer confusion.
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            return "esc"
        rest = os.read(fd, 2)
        if rest in {b"[A", b"OA"}:
            return "up"
        if rest in {b"[B", b"OB"}:
            return "down"
        if rest in {b"[C", b"OC"}:
            return "right"
        if rest in {b"[D", b"OD"}:
            return "left"
        return "esc"
    if ch in {b"k", b"K"}:
        return "up"
    if ch in {b"j", b"J"}:
        return "down"
    if ch in {b"\r", b"\n"}:
        return "enter"
    if ch in {b"q", b"Q"}:
        return "quit"
    if ch in {b"r", b"R"}:
        return "remap"
    if ch in {b"n", b"N"}:
        return "new"
    if ch in {b"m", b"M"}:
        return "mailbox"
    if ch in {b"x", b"X"}:
        return "kill-window"
    return "other"


def _next_session_name(base: str) -> str:
    """Return the next available session name based on base, e.g. 'cash claw 2'."""
    if not tmux.has_session(base):
        return base
    for i in range(2, 100):
        candidate = f"{base} {i}"
        if not tmux.has_session(candidate):
            return candidate
    return f"{base} {int(time.time())}"


def _path_completer(text: str, state: int) -> str | None:
    """Readline completer for file/directory paths with ~ expansion."""
    # Expand ~ to home directory for matching
    expanded = os.path.expanduser(text) if text.startswith("~") else text
    base, partial = os.path.split(expanded)
    if not base:
        base = "."
    try:
        entries = os.listdir(base)
    except OSError:
        entries = []
    matches = [e for e in entries if e.startswith(partial)]
    if state >= len(matches):
        return None
    # Return full path (re-attach ~ prefix if used)
    result = os.path.join(base, matches[state])
    if text.startswith("~"):
        home = str(Path.home())
        if result.startswith(home):
            result = "~" + result[len(home) :]
    return result + "/" if os.path.isdir(result) else result


def _prompt_for_path(session: str, current: str, fd: int, old_termios: list) -> str | None:
    print("\033[2J\033[H", end="")
    print(f"Remap directory for: {session}")
    print(f"Current: {current or '(unmapped)'}")
    print("Enter new path (Tab: autocomplete, Enter: confirm, Esc/Ctrl+C: cancel):")
    print()

    # Save readline state and configure for path completion
    old_completer = None
    old_delims = None
    if readline is not None:
        old_completer = readline.get_completer()
        old_delims = readline.get_completer_delims()
        readline.set_completer(_path_completer)
        readline.set_completer_delims(" \t\n")  # Exclude / so paths complete component-wise
        # macOS uses libedit which has different binding syntax than GNU readline
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")

    termios.tcsetattr(fd, termios.TCSADRAIN, old_termios)
    try:
        path = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    finally:
        tty.setcbreak(fd)
        # Restore readline state
        if readline is not None and old_completer is not None:
            readline.set_completer(old_completer)
            if old_delims is not None:
                readline.set_completer_delims(old_delims)
    return path if path else None


def _auto_link_two_session_mailboxes() -> None:
    mappings = config.load_mappings()
    sessions = tmux.list_sessions_with_paths()
    window_details = tmux.list_window_details()

    def _resolve_path(value: str) -> str:
        try:
            return str(Path(value).expanduser().resolve())
        except Exception:
            return value

    for mapped_dir in mappings.values():
        canonical = _resolve_path(mapped_dir)
        instances = [
            s
            for s in sessions
            if _resolve_path(str(s.get("path", ""))) == canonical and str(s.get("name", "")).strip()
        ]
        if len(instances) != 2:
            continue
        try:
            mailbox.ensure_mailbox(canonical)
        except mailbox.MailboxError:
            continue
        names = sorted(str(s["name"]) for s in instances)
        linked = mailbox.linked_targets(canonical)
        targets = {
            names[0]: _preferred_mailbox_window(names[0], "Rig A", window_details),
            names[1]: _preferred_mailbox_window(names[1], "Rig B", window_details),
        }
        wanted = {f"{name}:{window}" for name, window in targets.items()}
        if set(linked.keys()) == wanted:
            _set_rig_env_from_targets(names, linked, canonical)
            continue
        existing = {
            label: _session_from_tmux_target(target)
            for target, label in linked.items()
            if _session_from_tmux_target(target) in names
        }
        missing_labels = [label for label in ("Rig A", "Rig B") if label not in existing]
        unassigned = [name for name in names if name not in set(existing.values())]
        if len(missing_labels) == 1 and len(unassigned) == 1:
            label = missing_labels[0]
            session = unassigned[0]
            try:
                mailbox.link_rig(
                    workspace=canonical,
                    label=label,
                    session=session,
                    runtime="claude-code" if label == "Rig A" else "codex",
                    window=_preferred_mailbox_window(session, label, window_details),
                )
                _set_rig_env_from_targets(names, mailbox.linked_targets(canonical), canonical)
            except mailbox.MailboxError:
                continue
            continue
        try:
            mailbox.link_rig(
                workspace=canonical,
                label="Rig A",
                session=names[0],
                runtime="claude-code",
                window=targets[names[0]],
            )
            mailbox.link_rig(
                workspace=canonical,
                label="Rig B",
                session=names[1],
                runtime="codex",
                window=targets[names[1]],
            )
            _set_rig_env_from_targets(names, mailbox.linked_targets(canonical), canonical)
        except mailbox.MailboxError:
            continue


def _session_from_tmux_target(target: str) -> str:
    return target.rsplit(":", 1)[0] if ":" in target else target


def _preferred_mailbox_window(
    session: str, label: str, window_details: dict[str, dict[str, object]]
) -> str:
    details = window_details.get(session, {})
    windows = details.get("window_list", [])
    if not isinstance(windows, list):
        return "main"

    role_commands = {
        "Rig A": {"claude"},
        "Rig B": {"codex", "node"},
    }.get(label, set())
    ai_commands = {"claude", "codex", "node", "opencode"}

    for preferred in (role_commands, ai_commands):
        for window in windows:
            if not isinstance(window, dict):
                continue
            command = str(window.get("command") or "")
            name = str(window.get("name") or "")
            if name and command in preferred:
                return name

    for window in windows:
        if not isinstance(window, dict):
            continue
        if window.get("active") and window.get("name"):
            return str(window["name"])
    return "main"


def _set_rig_env_from_targets(
    session_names: list[str], linked_targets: dict[str, str], workspace: str | Path
) -> None:
    for session in session_names:
        for target, role in linked_targets.items():
            if _session_from_tmux_target(target) == session:
                _set_rig_session_env(session, role, workspace)
                break


def _set_rig_session_env(session: str, rig_name: str, workspace: str | Path) -> None:
    with suppress(tmux.TmuxError):
        tmux.set_session_environment(
            session,
            {
                "RIG_NAME": rig_name,
                "RIG_WORKSPACE": str(Path(workspace).expanduser().resolve()),
            },
        )


def _link_selected_mailbox_sessions(rows: list[dict[str, object]], selected: list[str]) -> str:
    by_name = {str(row["name"]): row for row in rows if not row.get("exit")}
    if len(selected) != 2 or selected[0] not in by_name or selected[1] not in by_name:
        return "select two running sessions"
    first = by_name[selected[0]]
    second = by_name[selected[1]]
    if not first.get("running") or not second.get("running"):
        return "both selected sessions must be running"
    first_dir = str(first.get("mapped_dir") or "")
    second_dir = str(second.get("mapped_dir") or "")
    if not first_dir or first_dir != second_dir:
        return "selected sessions must share one mapped mailbox workspace"
    try:
        mailbox.ensure_mailbox(first_dir)
        window_details = tmux.list_window_details()
        mailbox.link_rig(
            workspace=first_dir,
            label="Rig A",
            session=selected[0],
            runtime="claude-code",
            window=_preferred_mailbox_window(selected[0], "Rig A", window_details),
        )
        mailbox.link_rig(
            workspace=first_dir,
            label="Rig B",
            session=selected[1],
            runtime="codex",
            window=_preferred_mailbox_window(selected[1], "Rig B", window_details),
        )
        _set_rig_session_env(selected[0], "Rig A", first_dir)
        _set_rig_session_env(selected[1], "Rig B", first_dir)
    except mailbox.MailboxError as e:
        return str(e)
    return f"linked mailbox: {selected[0]} <-> {selected[1]}"


def _choose_attach_session_interactive() -> str | None:
    expanded: set[str] = set()
    expanded_sessions: set[str] = set()
    _auto_link_two_session_mailboxes()
    rows = _build_attach_menu_rows(expanded, expanded_sessions)
    if not rows:
        print("no mapped or running sessions found")
        return None

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("attach requires a session name in non-interactive mode")
        return None

    # Cache expensive per-render values — host never changes, focus only changes on attach.
    # Refresh both whenever rows are rebuilt (expand/collapse/new).
    host = socket.gethostname()
    focus = config.get_focus() or "none"

    def _rebuild_rows() -> list[dict[str, object]]:
        nonlocal focus
        focus = config.get_focus() or "none"
        _auto_link_two_session_mailboxes()
        return _build_attach_menu_rows(expanded, expanded_sessions)

    idx = 0
    mailbox_mode = False
    mailbox_selection: list[str] = []
    notice = ""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\033[?25l")  # hide cursor
        sys.stdout.flush()
        while True:
            _render_attach_menu(
                rows,
                idx,
                host=host,
                focus=focus,
                mailbox_mode=mailbox_mode,
                mailbox_selection=mailbox_selection,
                notice=notice,
            )
            key = _read_menu_key(fd)
            notice = ""
            if key == "up":
                idx = (idx - 1) % len(rows)
            elif key == "down":
                idx = (idx + 1) % len(rows)
            elif key == "mailbox":
                mailbox_mode = not mailbox_mode
                mailbox_selection = []
                notice = "mailbox mode on" if mailbox_mode else "mailbox mode off"
            elif key == "enter":
                row = rows[idx]
                if row["exit"]:
                    return None
                if mailbox_mode:
                    if not row.get("running") or row.get("row_type") in {"group", "window"}:
                        notice = "select a running session row"
                        continue
                    name = str(row["name"])
                    if name in mailbox_selection:
                        mailbox_selection.remove(name)
                    else:
                        mailbox_selection.append(name)
                    if len(mailbox_selection) == 2:
                        notice = _link_selected_mailbox_sessions(rows, mailbox_selection)
                        mailbox_mode = False
                        mailbox_selection = []
                        rows = _rebuild_rows()
                    continue
                if row.get("row_type") == "group":
                    # Toggle expand/collapse and stay on this row
                    mapping_name = str(row["mapping_name"])
                    if mapping_name in expanded:
                        expanded.discard(mapping_name)
                    else:
                        expanded.add(mapping_name)
                    rows = _rebuild_rows()
                    for i, r in enumerate(rows):
                        if r.get("row_type") == "group" and r["mapping_name"] == mapping_name:
                            idx = i
                            break
                elif row.get("row_type") in {"leaf", "child"} and int(row.get("windows") or 0) > 1:
                    session_name = str(row["name"])
                    if session_name in expanded_sessions:
                        expanded_sessions.discard(session_name)
                    else:
                        expanded_sessions.add(session_name)
                    rows = _rebuild_rows()
                    for i, r in enumerate(rows):
                        if r["name"] == session_name and r.get("row_type") in {"leaf", "child"}:
                            idx = i
                            break
                else:
                    return str(row["name"])
            elif key == "right":
                row = rows[idx]
                if row.get("row_type") == "group" and not row["expanded"]:
                    mname = str(row["mapping_name"])
                    expanded.add(mname)
                    rows = _rebuild_rows()
                    for i, r in enumerate(rows):
                        if r.get("row_type") == "group" and r["mapping_name"] == mname:
                            idx = i
                            break
                elif row.get("row_type") in {"leaf", "child"} and int(row.get("windows") or 0) > 1:
                    session_name = str(row["name"])
                    expanded_sessions.add(session_name)
                    rows = _rebuild_rows()
                    for i, r in enumerate(rows):
                        if r["name"] == session_name and r.get("row_type") in {"leaf", "child"}:
                            idx = i
                            break
            elif key == "left":
                row = rows[idx]
                row_type = row.get("row_type")
                session_name = str(row.get("session_name") or row.get("name") or "")
                if (
                    row_type == "window" or row_type in {"leaf", "child"}
                ) and session_name in expanded_sessions:
                    expanded_sessions.discard(session_name)
                    rows = _rebuild_rows()
                    for i, r in enumerate(rows):
                        if r["name"] == session_name and r.get("row_type") in {"leaf", "child"}:
                            idx = i
                            break
                else:
                    mapping_name = str(row.get("mapping_name", ""))
                    if mapping_name in expanded:
                        expanded.discard(mapping_name)
                        rows = _rebuild_rows()
                        for i, r in enumerate(rows):
                            if r.get("row_type") == "group" and r["mapping_name"] == mapping_name:
                                idx = i
                                break
            elif key == "kill-window":
                row = rows[idx]
                if row.get("row_type") != "window":
                    notice = "select a window row to kill"
                    continue
                target = str(row["name"])
                try:
                    tmux.kill_window(target)
                    notice = f"killed window: {target}"
                except tmux.TmuxError as e:
                    notice = str(e)
                rows = _rebuild_rows()
                idx = min(idx, len(rows) - 1)
            elif key == "remap":
                if rows[idx]["exit"]:
                    continue
                if rows[idx].get("row_type") == "window":
                    continue
                mapping_name = str(rows[idx]["mapping_name"])
                current_dir = str(rows[idx]["mapped_dir"])
                new_path = _prompt_for_path(mapping_name, current_dir, fd, old)
                if new_path:
                    config.set_mapping(mapping_name, new_path)
                    rows = _rebuild_rows()
                    for i, r in enumerate(rows):
                        if r["mapping_name"] == mapping_name and r.get("row_type") != "child":
                            idx = i
                            break
            elif key == "new":
                row = rows[idx]
                if row["exit"] or not row["mapped_dir"]:
                    continue
                mapped_dir = str(row["mapped_dir"])
                if not Path(mapped_dir).exists():
                    continue
                mapping_name = str(row["mapping_name"])
                new_name = _next_session_name(mapping_name)
                try:
                    tmux.new_session(new_name, mapped_dir)
                    tmux.send_keys(f"{new_name}:main", ["opencode", "Enter"])
                    tmux.new_window(new_name, "logs", mapped_dir)
                    tmux.new_window(new_name, "shell", mapped_dir)
                except tmux.TmuxError:
                    rows = _rebuild_rows()
                    continue
                # Auto-expand the group and land on the new child row
                expanded.add(mapping_name)
                rows = _rebuild_rows()
                for i, r in enumerate(rows):
                    if r["name"] == new_name:
                        idx = i
                        break
                notice = "auto-linked mailbox if this project now has exactly two sessions"
            elif key in {"quit", "esc"}:
                return None
    finally:
        sys.stdout.write("\033[?25h")  # restore cursor
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def cmd_kill(args: argparse.Namespace) -> int:
    session = _resolve_session(args.name)
    if not session:
        print("no session provided and nothing focused")
        return 1
    if not tmux.has_session(session):
        print(f"session not found: {session}")
        return 1

    tmux.kill_session(session)
    if config.get_focus() == session:
        config.set_focus("")
    print(f"killed: {session}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    # Watch focused session by default
    session = args.name or config.get_focus()
    if not session:
        print("no session provided and nothing focused")
        return 1
    if not tmux.has_session(session):
        print(f"session not found: {session}")
        return 1

    pane_text = tmux.capture_last_lines(session, "main", lines=args.capture_lines)
    pane_text_lc = pane_text.lower()

    matched_pattern = _match_wait_pattern(pane_text_lc)
    if matched_pattern:
        snippet = _snippet_for_pattern(pane_text, matched_pattern)
        _send_waiting_alert(
            session=session,
            title="OpenCode awaiting input",
            reason="AI agent waiting for input",
            detail=f"prompt pattern '{matched_pattern}' matched",
            severity="warning",
            status="degraded",
            fingerprint_suffix="pattern",
            snippet=snippet,
        )
        print(f"notified: {session}\tpattern={matched_pattern}")
        return 0

    last = tmux.pane_last_activity(session, "main")
    now = int(time.time())
    delta = now - last if last > 0 else 0

    matched_stall = _match_stall_pattern(pane_text_lc)
    if matched_stall and delta >= args.idle_seconds:
        snippet = _snippet_for_pattern(pane_text, matched_stall)
        _send_waiting_alert(
            session=session,
            title="OpenCode stalled?",
            reason="AI agent appears stalled",
            detail=f"stall pattern '{matched_stall}' matched and idle for {delta}s",
            severity="warning",
            status="degraded",
            fingerprint_suffix="stall",
            snippet=snippet,
        )
        print(f"notified: {session}\tstall_pattern={matched_stall}\tidle={delta}s")
        return 0

    if delta >= args.idle_seconds:
        _send_waiting_alert(
            session=session,
            title="OpenCode waiting?",
            reason="No output detected",
            detail=f"idle for {delta}s",
            severity="info",
            status="degraded",
            fingerprint_suffix="idle",
        )
        print(f"notified: {session}\tidle={delta}s")
    else:
        print(f"ok: {session}\tidle={delta}s")
    return 0


def cmd_set_webhook(args: argparse.Namespace) -> int:
    config.set_webhook(args.url)
    print("webhook set" if args.url else "webhook cleared")
    return 0


def cmd_set_alert_router(args: argparse.Namespace) -> int:
    config.set_alert_router(args.url)
    print("alert-router set" if args.url else "alert-router cleared")
    return 0


def cmd_set_relay_token(args: argparse.Namespace) -> int:
    config.set_relay_token(args.token)
    print("relay-token set" if args.token else "relay-token cleared")
    return 0


def cmd_relay(args: argparse.Namespace) -> int:
    token = args.token or config.get_relay_token()
    if not token:
        print("missing relay token; run: oc set-relay-token <token>")
        return 1
    serve_relay(host=args.host, port=args.port, token=token)
    return 0


def cmd_voice(args: argparse.Namespace) -> int:
    intent = parse_voice(args.phrase)

    # Voice-first decision:
    # - "attach/open/go to" => focus-only (Shortcuts SSH is non-interactive).
    # - interactive attach happens in Termius when you want the live screen.
    if intent.action == "status":
        return cmd_status(args)
    if intent.action == "ls":
        return cmd_ls(args)
    if intent.action == "new":
        return cmd_new(argparse.Namespace(name=intent.session))
    if intent.action == "focus":
        return cmd_focus(argparse.Namespace(name=intent.session))
    if intent.action == "attach_or_focus":
        return cmd_focus(argparse.Namespace(name=intent.session))
    if intent.action == "enter":
        return cmd_enter(argparse.Namespace(session=None))
    if intent.action == "say":
        return cmd_say(argparse.Namespace(session=intent.session, text=intent.text or ""))
    print("unhandled intent")
    return 2


def cmd_completion(args: argparse.Namespace) -> int:
    shell = args.shell.lower()
    if shell == "bash":
        print(_bash_completion_script())
        return 0
    if shell == "zsh":
        print(_zsh_completion_script())
        return 0
    if shell == "fish":
        print(_fish_completion_script())
        return 0

    print(f"unsupported shell: {shell}")
    return 2


def cmd_mailbox_link(args: argparse.Namespace) -> int:
    workspace = args.workspace or config.get_mapping(args.session) or os.getcwd()
    try:
        mailbox.ensure_mailbox(workspace)
        window = (
            _preferred_mailbox_window(args.session, args.rig, tmux.list_window_details())
            if args.window == "auto"
            else args.window
        )
        rigs_file = mailbox.link_rig(
            workspace=workspace,
            label=args.rig,
            session=args.session,
            runtime=args.runtime,
            window=window,
        )
        _set_rig_session_env(args.session, args.rig, workspace)
    except mailbox.MailboxError as e:
        print(str(e))
        return 1

    print(f"linked:\t{args.rig} -> {mailbox.tmux_target(args.session, window)}")
    print(f"file:\t{rigs_file}")
    return 0


def _prompt_default(label: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    return raw or default


def _prompt_yes_no(label: str, default: bool = True) -> bool:
    marker = "Y/n" if default else "y/N"
    raw = input(f"{label} [{marker}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _mailbox_workspace_default() -> str:
    focus = config.get_focus()
    if focus:
        mapped = config.get_mapping(focus)
        if mapped:
            return mapped
    return os.getcwd()


def _workspace_slug(workspace: str) -> str:
    name = Path(workspace).expanduser().resolve().name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return slug or "project"


def _ensure_mailbox_tmux_session(session: str, workspace: str, command: str, rig_name: str) -> None:
    if tmux.has_session(session):
        _set_rig_session_env(session, rig_name, workspace)
        return
    tmux.new_session(session, workspace)
    _set_rig_session_env(session, rig_name, workspace)
    if command:
        tmux.send_keys(f"{session}:main", [command, "Enter"])


def cmd_mailbox_wizard(_: argparse.Namespace) -> int:
    print("Mailbox tmux setup")
    print()
    sessions = tmux.list_sessions()
    if sessions:
        print("Running tmux sessions:")
        for row in sessions:
            print(f"  - {row['name']}")
        print()

    workspace = _prompt_default(
        "Workspace for project-local .rig-mailbox", _mailbox_workspace_default()
    )
    workspace_path = Path(workspace).expanduser().resolve()
    slug = _workspace_slug(str(workspace_path))

    rig_a_session = _prompt_default("Rig A tmux session", f"{slug}-rig-a")
    rig_a_runtime = _prompt_default("Rig A runtime label", "claude-code")
    rig_a_command = _prompt_default("Rig A launch command if session is missing", "claude")

    rig_b_session = _prompt_default("Rig B tmux session", f"{slug}-rig-b")
    rig_b_runtime = _prompt_default("Rig B runtime label", "codex")
    rig_b_command = _prompt_default("Rig B launch command if session is missing", "codex")

    if _prompt_yes_no("Create missing tmux sessions", True):
        try:
            _ensure_mailbox_tmux_session(rig_a_session, str(workspace_path), rig_a_command, "Rig A")
            _ensure_mailbox_tmux_session(rig_b_session, str(workspace_path), rig_b_command, "Rig B")
        except tmux.TmuxError as e:
            print(str(e))
            return 1

    try:
        mailbox.ensure_mailbox(workspace_path)
        window_details = tmux.list_window_details()
        mailbox.link_rig(
            workspace=workspace_path,
            label="Rig A",
            session=rig_a_session,
            runtime=rig_a_runtime,
            window=_preferred_mailbox_window(rig_a_session, "Rig A", window_details),
        )
        rigs_file = mailbox.link_rig(
            workspace=workspace_path,
            label="Rig B",
            session=rig_b_session,
            runtime=rig_b_runtime,
            window=_preferred_mailbox_window(rig_b_session, "Rig B", window_details),
        )
        _set_rig_session_env(rig_a_session, "Rig A", workspace_path)
        _set_rig_session_env(rig_b_session, "Rig B", workspace_path)
    except mailbox.MailboxError as e:
        print(str(e))
        return 1

    print()
    print(f"linked:\tRig A -> {mailbox.tmux_target(rig_a_session)}")
    print(f"linked:\tRig B -> {mailbox.tmux_target(rig_b_session)}")
    print(f"file:\t{rigs_file}")
    print('test:\trig send "Rig A" "mailbox tmux test"')
    return 0


def cmd_clipboard_setup(args: argparse.Namespace) -> int:
    try:
        result = clipboard.setup(
            mode=args.mode,
            tmux_conf=args.tmux_conf,
            tmux_socket=args.tmux_socket,
            dry_run=args.dry_run,
            print_snippet=args.print_snippet,
            reload_tmux=args.reload,
            bind_keys=args.bind_keys,
            follow_symlink=args.follow_symlink,
            mouse_mode=args.mouse_mode,
        )
    except clipboard.ClipboardError as e:
        print(str(e))
        return 1

    if args.print_snippet:
        print("# Add this block to your tmux config")
        print(result["snippet"])
        print("# Managed include content")
        print(result["include_text"])
        return 0

    if args.dry_run:
        print(f"mode:\t{result['mode']}")
        print(f"tmux_conf:\t{result['tmux_conf']}")
        print(f"tmux_conf_changed:\t{int(result['changes']['tmux_conf_changed'])}")
        print(f"include_changed:\t{int(result['changes']['include_changed'])}")
        return 0

    print(f"configured:\t{result['mode']}")
    print(f"mouse_mode:\t{result['mouse_mode']}")
    print(f"tmux_conf:\t{result['tmux_conf']}")
    print(f"include:\t{result['include_file']}")
    if result.get("helper_file"):
        print(f"helper:\t{result['helper_file']}")
    if result.get("reload_error"):
        print(f"reload:\tfailed ({result['reload_error']})")
        print("tip:\treload manually with `tmux source-file ~/.tmux.conf`")
    elif args.reload:
        print("reload:\tok")
    return 0


def cmd_clipboard_status(args: argparse.Namespace) -> int:
    data = clipboard.status(tmux_socket=args.tmux_socket)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    print(f"configured_on_disk:\t{int(data['configured_on_disk'])}")
    loaded = data["loaded_in_tmux"]
    loaded_text = "unknown" if loaded is None else str(int(bool(loaded)))
    print(f"loaded_in_tmux:\t{loaded_text}")
    if data.get("tmux_socket_used"):
        print(f"tmux_socket:\t{data['tmux_socket_used']}")
    if data.get("tmux_socket_ambiguous"):
        print("tmux_socket_ambiguous:\t1")
        print("tip:\tpass --tmux-socket <path> to target a specific tmux server")
    print(f"selected_mode:\t{data['selected_mode'] or '(none)'}")
    print(f"mouse_mode:\t{data['mouse_mode'] or '(unknown)'}")
    if data["loaded_mode"]:
        print(f"loaded_mode:\t{data['loaded_mode']}")
    print(f"helper_kind:\t{data['helper_kind']}")
    if data["helper_health"] is not None:
        print(f"helper_health:\t{int(bool(data['helper_health']))}")
    verification = data["verification"]
    print(f"emission_verified:\t{int(bool(verification['emission_verified']))}")
    print(f"clipboard_verified:\t{int(bool(verification['clipboard_verified']))}")
    if data["reasons"]:
        print(f"reasons:\t{', '.join(data['reasons'])}")
    return 0


def cmd_clipboard_verify(args: argparse.Namespace) -> int:
    try:
        data = clipboard.verify(strict=args.strict)
    except clipboard.ClipboardError as e:
        print(str(e))
        return 1

    print(f"emission_verified:\t{int(bool(data['emission_verified']))}")
    print(f"clipboard_verified:\t{int(bool(data['clipboard_verified']))}")
    if data["emission_verified"] and not data["clipboard_verified"]:
        print("tip:\tOSC52 emitted, but clipboard was not confirmed")
        return 1
    return 0


def cmd_clipboard_uninstall(args: argparse.Namespace) -> int:
    try:
        result = clipboard.uninstall(
            tmux_conf=args.tmux_conf,
            remove_helper=args.remove_helper,
            follow_symlink=args.follow_symlink,
        )
    except clipboard.ClipboardError as e:
        print(str(e))
        return 1

    if not result["removed"]:
        print(result["message"])
        return 0

    print("clipboard config removed")
    print(f"restored_backup:\t{int(bool(result['restored_backup']))}")
    print(f"removed_marker_block:\t{int(bool(result['removed_block']))}")
    if args.remove_helper:
        print(f"removed_helper:\t{int(bool(result['removed_helper']))}")
    return 0


def _bash_completion_script() -> str:
    cmds = _fmt_cmds_for_shell(COMMANDS)
    template = """# occtl bash completion
_occtl_tmux_sessions() {
  tmux list-sessions -F '#{session_name}' 2>/dev/null
}

_occtl_complete() {
  local cur prev
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"

  if [[ $COMP_CWORD -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "{cmds}" -- "$cur") )
    return 0
  fi

  if [[ "${COMP_WORDS[1]}" == "clipboard" ]]; then
    if [[ $COMP_CWORD -eq 2 ]]; then
      COMPREPLY=( $(compgen -W "setup status verify uninstall" -- "$cur") )
      return 0
    fi

    case "$prev" in
      setup)
        local clipboard_setup_opts
        clipboard_setup_opts="--mode --tmux-conf --tmux-socket --dry-run --print-snippet"
        clipboard_setup_opts+=" --reload --bind-keys --mouse-mode --follow-symlink"
        COMPREPLY=( $(compgen -W "$clipboard_setup_opts" -- "$cur") )
        ;;
      status)
        COMPREPLY=( $(compgen -W "--json --tmux-socket" -- "$cur") )
        ;;
      verify)
        COMPREPLY=( $(compgen -W "--strict" -- "$cur") )
        ;;
      uninstall)
        COMPREPLY=( $(compgen -W "--tmux-conf --remove-helper --follow-symlink" -- "$cur") )
        ;;
    esac
    return 0
  fi

  if [[ "${COMP_WORDS[1]}" == "mailbox" ]]; then
    if [[ $COMP_CWORD -eq 2 ]]; then
      COMPREPLY=( $(compgen -W "link" -- "$cur") )
      return 0
    fi
    if [[ "${COMP_WORDS[2]}" == "link" ]]; then
      case "$prev" in
        --rig|--runtime|--workspace|--window)
          return 0
          ;;
        *)
          local mailbox_link_opts
          mailbox_link_opts="$(_occtl_tmux_sessions) --rig --runtime"
          mailbox_link_opts+=" --workspace --window"
          COMPREPLY=( $(compgen -W "$mailbox_link_opts" -- "$cur") )
          ;;
      esac
      return 0
    fi
  fi

    case "$prev" in
    attach|focus|kill)
      COMPREPLY=( $(compgen -W "$(_occtl_tmux_sessions)" -- "$cur") )
      ;;
    watch)
      COMPREPLY+=( $(compgen -W "--name --idle-seconds --capture-lines" -- "$cur") )
      ;;
    --name|--session)
      COMPREPLY=( $(compgen -W "$(_occtl_tmux_sessions)" -- "$cur") )
      ;;
    set-webhook|set-alert-router|set-relay-token)
      return 0
      ;;
    completion)
      COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
      ;;
  esac
}

complete -F _occtl_complete oc
    """
    return template.replace("{cmds}", cmds)


def _zsh_completion_script() -> str:
    cmds = _fmt_cmds_for_shell(COMMANDS)
    template = """#compdef oc

_occtl() {
  local -a commands
  local -a sessions
  commands=(
    __CMD_LIST__
  )
  sessions=(${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null)"})

  if (( CURRENT == 2 )); then
    compadd -a commands
    return
  fi

  case "$words[2]" in
    clipboard)
      if (( CURRENT == 3 )); then
        compadd -- setup status verify uninstall
      elif [[ "$words[3]" == "setup" ]]; then
        local -a clip_setup_opts
        clip_setup_opts=(
          --mode --tmux-conf --tmux-socket --dry-run --print-snippet
          --reload --bind-keys --mouse-mode --follow-symlink
        )
        compadd -- $clip_setup_opts
      elif [[ "$words[3]" == "status" ]]; then
        compadd -- --json --tmux-socket
      elif [[ "$words[3]" == "verify" ]]; then
        compadd -- --strict
      elif [[ "$words[3]" == "uninstall" ]]; then
        compadd -- --tmux-conf --remove-helper --follow-symlink
      fi
      ;;
    mailbox)
      if (( CURRENT == 3 )); then
        compadd -- link
      elif [[ "$words[3]" == "link" ]]; then
        local prev_word
        prev_word="$words[CURRENT-1]"
        if [[ "$prev_word" == "--rig" || "$prev_word" == "--runtime" ]]; then
          return
        fi
        if [[ "$prev_word" == "--workspace" || "$prev_word" == "--window" ]]; then
          return
        fi
        compadd -- $sessions --rig --runtime --workspace --window
      fi
      ;;
    attach|focus|kill)
      compadd -a sessions
      ;;
    watch)
      if [[ "$words[CURRENT-1]" == "--name" ]]; then
        compadd -a sessions
      else
        compadd -- --name --idle-seconds --capture-lines
      fi
      ;;
    say|enter)
      if [[ "$words[CURRENT-1]" == "--session" ]]; then
        compadd -a sessions
      else
        compadd -- --session
      fi
      ;;
    completion)
      compadd -- bash zsh fish
      ;;
  esac
}

compdef _occtl oc
    """
    return template.replace("__CMD_LIST__", cmds)


def _fish_completion_script() -> str:
    cmds = _fmt_cmds_for_shell(COMMANDS)
    template = """# occtl fish completion
function __occtl_tmux_sessions
  tmux list-sessions -F '#{session_name}' 2>/dev/null
end

complete -c oc -f
complete -c oc -n '__fish_use_subcommand' -a "{cmds}"
complete -c oc -n "__fish_seen_subcommand_from attach focus kill" -a "(__occtl_tmux_sessions)"
complete -c oc -n "__fish_seen_subcommand_from watch" -l name -r -a "(__occtl_tmux_sessions)"
complete -c oc -n "__fish_seen_subcommand_from watch" -l idle-seconds -r
complete -c oc -n "__fish_seen_subcommand_from watch" -l capture-lines -r
complete -c oc -n "__fish_seen_subcommand_from say enter" -l session -r -a "(__occtl_tmux_sessions)"
complete -c oc -n "__fish_seen_subcommand_from completion" -f -a "bash zsh fish"
complete -c oc -n "__fish_seen_subcommand_from mailbox" -f -a "link"
complete -c oc -n "__fish_seen_subcommand_from mailbox link" -l rig -r
complete -c oc -n "__fish_seen_subcommand_from mailbox link" -l runtime -r
complete -c oc -n "__fish_seen_subcommand_from mailbox link" -l workspace -r
complete -c oc -n "__fish_seen_subcommand_from mailbox link" -l window -r
complete -c oc -n "__fish_seen_subcommand_from clipboard" -f -a "setup status verify uninstall"
complete -c oc -n "__fish_seen_subcommand_from clipboard setup" -l mode -r -a "auto osc52 native"
complete -c oc -n "__fish_seen_subcommand_from clipboard setup" -l tmux-conf -r
complete -c oc -n "__fish_seen_subcommand_from clipboard setup" -l tmux-socket -r
complete -c oc -n "__fish_seen_subcommand_from clipboard setup" -l dry-run
complete -c oc -n "__fish_seen_subcommand_from clipboard setup" -l print-snippet
complete -c oc -n "__fish_seen_subcommand_from clipboard setup" -l reload
complete -c oc -n "__fish_seen_subcommand_from clipboard setup" -l bind-keys -r \
  -a "minimal copy-mode-y none"
complete -c oc -n "__fish_seen_subcommand_from clipboard setup" -l mouse-mode -r \
  -a "terminal tmux scroll"
complete -c oc -n "__fish_seen_subcommand_from clipboard setup" -l follow-symlink
complete -c oc -n "__fish_seen_subcommand_from clipboard status" -l json
complete -c oc -n "__fish_seen_subcommand_from clipboard status" -l tmux-socket -r
complete -c oc -n "__fish_seen_subcommand_from clipboard verify" -l strict
complete -c oc -n "__fish_seen_subcommand_from clipboard uninstall" -l tmux-conf -r
complete -c oc -n "__fish_seen_subcommand_from clipboard uninstall" -l remove-helper
complete -c oc -n "__fish_seen_subcommand_from clipboard uninstall" -l follow-symlink
    """
    return template.replace("{cmds}", cmds)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oc", description="occtl — tmux + OpenCode command center")
    sub = p.add_subparsers(dest="cmd", required=False)

    sp = sub.add_parser("map", help="map session name to directory")
    sp.add_argument("name")
    sp.add_argument("path")
    sp.set_defaults(fn=cmd_map)

    sp = sub.add_parser("maps", help="list mappings")
    sp.set_defaults(fn=cmd_maps)

    sp = sub.add_parser("new", help="create session and start opencode (focuses)")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("ensure", help="create if missing, then focus")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_ensure)

    sp = sub.add_parser("ls", help="list tmux sessions")
    sp.set_defaults(fn=cmd_ls)

    sp = sub.add_parser("focus", help="set focused session")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_focus)

    sp = sub.add_parser("focused", help="print focused session")
    sp.set_defaults(fn=cmd_focused)

    sp = sub.add_parser("status", help="show focus + mapping + idle seconds")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("say", help="send text to OpenCode (focused session by default)")
    sp.add_argument("text", nargs="+")
    sp.add_argument("--session", default=None)
    sp.set_defaults(
        fn=lambda a: cmd_say(argparse.Namespace(session=a.session, text=" ".join(a.text)))
    )

    sp = sub.add_parser("enter", help="send Enter (focused session by default)")
    sp.add_argument("--session", default=None)
    sp.set_defaults(fn=cmd_enter)

    sp = sub.add_parser("attach", help="attach to a session (interactive picker when omitted)")
    sp.add_argument("name", nargs="?", default=None)
    sp.add_argument(
        "--cc",
        action="store_true",
        help="use iTerm2 control mode (tmux -CC) when attaching",
    )
    sp.set_defaults(fn=cmd_attach)

    sp = sub.add_parser("kill", help="kill a session (focused session by default)")
    sp.add_argument("name", nargs="?", default=None)
    sp.set_defaults(fn=cmd_kill)

    sp = sub.add_parser("watch", help="prompt-aware waiting alert (focused session by default)")
    sp.add_argument("--name", default=None)
    sp.add_argument("--idle-seconds", type=int, default=90)
    sp.add_argument("--capture-lines", type=int, default=120)
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("set-webhook", help="set Discord webhook URL for alerts (optional)")
    sp.add_argument("url")
    sp.set_defaults(fn=cmd_set_webhook)

    sp = sub.add_parser(
        "set-alert-router",
        help="set homelab alert-router webhook URL for alerts (optional)",
    )
    sp.add_argument("url")
    sp.set_defaults(fn=cmd_set_alert_router)

    sp = sub.add_parser("set-relay-token", help="set token used by oc relay API")
    sp.add_argument("token")
    sp.set_defaults(fn=cmd_set_relay_token)

    sp = sub.add_parser("relay", help="run local relay API for Discord button actions")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8878)
    sp.add_argument("--token", default="")
    sp.set_defaults(fn=cmd_relay)

    sp = sub.add_parser("voice", help="parse a voice phrase and execute (Shortcuts)")
    sp.add_argument("phrase", nargs="+")
    sp.set_defaults(fn=lambda a: cmd_voice(argparse.Namespace(phrase=" ".join(a.phrase))))

    sp = sub.add_parser("completion", help="print shell completion script")
    sp.add_argument("shell", choices=("bash", "zsh", "fish"))
    sp.set_defaults(fn=cmd_completion)

    sp = sub.add_parser("mailbox", help="interactive rig mailbox tmux setup")
    mailbox_sub = sp.add_subparsers(dest="mailbox_cmd", required=False)

    mailbox_link = mailbox_sub.add_parser(
        "link",
        help="link a rig label to a tmux session target in .rig-mailbox/rigs.toml",
    )
    mailbox_link.add_argument("session", help="tmux session name managed by oc")
    mailbox_link.add_argument("--rig", required=True, help='rig label, e.g. "Rig B"')
    mailbox_link.add_argument(
        "--runtime", default="codex", help="runtime label stored in rigs.toml"
    )
    mailbox_link.add_argument(
        "--workspace",
        default=None,
        help="workspace containing .rig-mailbox; defaults to session mapping or cwd",
    )
    mailbox_link.add_argument("--window", default="auto", help="tmux window name or auto")
    mailbox_link.set_defaults(fn=cmd_mailbox_link)

    sp.set_defaults(fn=cmd_mailbox_wizard)

    sp = sub.add_parser("clipboard", help="configure tmux clipboard integration")
    clip_sub = sp.add_subparsers(dest="clipboard_cmd", required=False)

    clip_setup = clip_sub.add_parser("setup", help="install managed tmux clipboard config")
    clip_setup.add_argument("--mode", choices=("auto", "osc52", "native"), default="auto")
    clip_setup.add_argument("--tmux-conf", default=None)
    clip_setup.add_argument("--tmux-socket", default=None)
    clip_setup.add_argument("--dry-run", action="store_true")
    clip_setup.add_argument("--print-snippet", action="store_true")
    clip_setup.add_argument("--reload", action="store_true")
    clip_setup.add_argument(
        "--bind-keys",
        choices=("minimal", "copy-mode-y", "none"),
        default="copy-mode-y",
        help="copy-mode-y binds lowercase y in copy mode; minimal binds uppercase Y",
    )
    clip_setup.add_argument(
        "--mouse-mode",
        choices=("terminal", "tmux", "scroll"),
        default="scroll",
        help=(
            "scroll (default) enables mouse scrolling while leaving drag to the terminal — "
            "use Option-drag in iTerm2 or Prefix [ copy mode; "
            "tmux captures mouse drag for copy-on-release; "
            "terminal disables mouse entirely (no scrolling)"
        ),
    )
    clip_setup.add_argument("--follow-symlink", action="store_true")
    clip_setup.set_defaults(fn=cmd_clipboard_setup)

    clip_status = clip_sub.add_parser("status", help="show clipboard integration status")
    clip_status.add_argument("--json", action="store_true")
    clip_status.add_argument("--tmux-socket", default=None)
    clip_status.set_defaults(fn=cmd_clipboard_status)

    clip_verify = clip_sub.add_parser("verify", help="verify OSC52 emission and clipboard paste")
    clip_verify.add_argument("--strict", action="store_true")
    clip_verify.set_defaults(fn=cmd_clipboard_verify)

    clip_uninstall = clip_sub.add_parser("uninstall", help="remove managed clipboard configuration")
    clip_uninstall.add_argument("--tmux-conf", default=None)
    clip_uninstall.add_argument("--remove-helper", action="store_true")
    clip_uninstall.add_argument("--follow-symlink", action="store_true")
    clip_uninstall.set_defaults(fn=cmd_clipboard_uninstall)

    sp.set_defaults(fn=cmd_clipboard_status, json=False, tmux_socket=None)

    return p


def main() -> None:
    config.ensure_config_dir()
    parser = build_parser()
    parser.set_defaults(fn=cmd_status)
    args = parser.parse_args()
    try:
        rc = args.fn(args)
    except tmux.TmuxError as e:
        print(str(e))
        rc = 1
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
