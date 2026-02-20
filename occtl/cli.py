from __future__ import annotations

import argparse
import re
import shutil
import socket
import sys
import termios
import time
import tty
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from . import config, tmux
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
    session = args.name or _choose_attach_session_interactive()
    if not session:
        print("attach cancelled")
        return 1

    if not tmux.has_session(session):
        if config.get_mapping(session):
            rc = cmd_new(argparse.Namespace(name=session))
            if rc != 0:
                return rc
        else:
            print(f"session not found: {session}")
            return 1

    config.set_focus(session)
    tmux.attach(session)
    return 0


def _build_attach_menu_rows() -> list[dict[str, object]]:
    mappings = config.load_mappings()
    sessions = {row["name"]: row for row in tmux.list_sessions()}
    names = sorted(set(mappings.keys()) | set(sessions.keys()))

    focus = config.get_focus()
    rows: list[dict[str, object]] = []
    for name in names:
        live = sessions.get(name)
        mapped = mappings.get(name, "")
        rows.append(
            {
                "name": name,
                "mapped_dir": mapped,
                "running": bool(live),
                "attached": bool(live and live["attached"]),
                "windows": int(live["windows"]) if live else 0,
                "focused": name == focus,
                "exit": False,
            }
        )
    rows.append(
        {
            "name": "Exit",
            "mapped_dir": "",
            "running": False,
            "attached": False,
            "windows": 0,
            "focused": False,
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


def _menu_border(inner_width: int) -> str:
    return "+" + ("-" * inner_width) + "+"


def _menu_row(text: str, inner_width: int) -> str:
    return "|" + _fit_text(text, inner_width).ljust(inner_width) + "|"


def _session_status_text(row: dict[str, object]) -> str:
    if row["exit"]:
        return ""
    parts = ["RUNNING" if row["running"] else "STOPPED"]
    if row["focused"]:
        parts.append("FOCUS")
    if row["attached"]:
        parts.append("ATTACHED")
    if row["running"]:
        parts.append(f"WIN:{row['windows']}")
    return " ".join(parts)


def _render_attach_menu(rows: list[dict[str, object]], idx: int) -> None:
    print("\033[2J\033[H", end="")
    cols = shutil.get_terminal_size(fallback=(100, 30)).columns
    inner = max(72, min(120, cols - 2))
    name_w = max(14, int(inner * 0.24))
    state_w = 8
    flags_w = 14
    win_w = 5
    path_w = inner - (name_w + state_w + flags_w + win_w + 12)

    def _table_row(name: str, state: str, flags: str, win: str, path: str) -> str:
        c_name = _fit_text(name, name_w).ljust(name_w)
        c_state = _fit_text(state, state_w).ljust(state_w)
        c_flags = _fit_text(flags, flags_w).ljust(flags_w)
        c_win = _fit_text(win, win_w).rjust(win_w)
        c_path = _fit_text(path, path_w).ljust(path_w)
        return f"| {c_name} | {c_state} | {c_flags} | {c_win} | {c_path} |"

    print(_menu_border(inner))
    print(_menu_row(" OC SESSION MANAGER ", inner))
    print(_menu_border(inner))
    print(_menu_row(" Up/Down or j/k: move   Enter: attach/start   q/Esc: exit ", inner))
    print(_menu_border(inner))
    print(_table_row("SESSION", "STATE", "FLAGS", "WIN", "PROJECT"))
    print(_menu_border(inner))

    for i, row in enumerate(rows):
        if row["exit"]:
            line = _table_row("Exit", "", "", "", "")
            if i == idx:
                print(f"\033[7m{line}\033[0m")
            else:
                print(line)
            continue

        flags: list[str] = []
        if row["focused"]:
            flags.append("FOCUS")
        if row["attached"]:
            flags.append("ATTACHED")
        line = _table_row(
            str(row["name"]),
            "RUNNING" if bool(row["running"]) else "STOPPED",
            ",".join(flags) if flags else "-",
            str(row["windows"] if bool(row["running"]) else "-"),
            _compact_path(str(row["mapped_dir"])),
        )
        if i == idx:
            print(f"\033[7m{line}\033[0m")
        else:
            print(line)

    print(_menu_border(inner))


def _read_menu_key() -> str:
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        nxt = sys.stdin.read(1)
        if nxt == "[":
            third = sys.stdin.read(1)
            if third == "A":
                return "up"
            if third == "B":
                return "down"
        return "esc"
    if ch in {"k", "K"}:
        return "up"
    if ch in {"j", "J"}:
        return "down"
    if ch in {"\r", "\n"}:
        return "enter"
    if ch in {"q", "Q"}:
        return "quit"
    return "other"


def _choose_attach_session_interactive() -> str | None:
    rows = _build_attach_menu_rows()
    if not rows:
        print("no mapped or running sessions found")
        return None

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("attach requires a session name in non-interactive mode")
        return None

    idx = 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            _render_attach_menu(rows, idx)
            key = _read_menu_key()
            if key == "up":
                idx = (idx - 1) % len(rows)
            elif key == "down":
                idx = (idx + 1) % len(rows)
            elif key == "enter":
                print("\033[2J\033[H", end="")
                if rows[idx]["exit"]:
                    return None
                return str(rows[idx]["name"])
            elif key in {"quit", "esc"}:
                print("\033[2J\033[H", end="")
                return None
    finally:
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
