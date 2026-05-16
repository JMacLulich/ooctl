from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import config, tmux

MARKER_BEGIN = "# >>> occtl clipboard (managed) >>>"
MARKER_END = "# <<< occtl clipboard (managed) <<<"
STATUS_SCHEMA_VERSION = 1
LOCK_STALE_SECONDS = 300


class ClipboardError(RuntimeError):
    pass


def _state_file() -> Path:
    return config.CONFIG_DIR / "clipboard.json"


def _lock_file() -> Path:
    return config.CONFIG_DIR / "clipboard.lock"


def _include_file() -> Path:
    return config.CONFIG_DIR / "tmux-clipboard.conf"


def _helper_file() -> Path:
    return Path.home() / ".local" / "share" / "occtl" / "bin" / "oc-osc52-copy"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    _atomic_write(path, json.dumps(payload, indent=2) + "\n", mode=0o600)


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    elif path.exists():
        tmp.chmod(path.stat().st_mode & 0o777)
    os.replace(tmp, path)


@contextmanager
def _operation_lock() -> Iterator[None]:
    config.ensure_config_dir()
    lock_path = _lock_file()
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as e:
            existing = _read_json(lock_path)
            created_at = existing.get("created_at") if isinstance(existing, dict) else None
            if not isinstance(created_at, int):
                try:
                    created_at = int(lock_path.stat().st_mtime)
                except OSError:
                    created_at = None
            now = int(time.time())
            if created_at is not None and (now - created_at) > LOCK_STALE_SECONDS:
                lock_path.unlink(missing_ok=True)
                continue
            raise ClipboardError(
                f"clipboard operation already in progress (lock: {lock_path})"
            ) from e
    else:
        raise ClipboardError(f"failed to acquire clipboard lock (lock: {lock_path})")

    try:
        payload = {"pid": os.getpid(), "created_at": int(time.time())}
        os.write(fd, (json.dumps(payload) + "\n").encode("utf-8"))
        os.close(fd)
        yield None
    finally:
        lock_path.unlink(missing_ok=True)


def _remove_managed_block(text: str) -> tuple[str, bool]:
    pattern = re.compile(rf"\n?{re.escape(MARKER_BEGIN)}.*?{re.escape(MARKER_END)}\n?", re.DOTALL)
    updated, count = pattern.subn("\n", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", updated).strip("\n")
    if cleaned:
        cleaned += "\n"
    return cleaned, count > 0


def _source_block(include_path: Path) -> str:
    include = str(include_path)
    return "\n".join(
        [
            MARKER_BEGIN,
            f"if-shell '[ -f \"{include}\" ]' 'source-file \"{include}\"'",
            MARKER_END,
        ]
    )


def _upsert_source_block(text: str, include_path: Path) -> str:
    cleaned, _ = _remove_managed_block(text)
    block = _source_block(include_path)
    if not cleaned:
        return block + "\n"
    return cleaned + "\n" + block + "\n"


def _detect_native_clipboard_cmd() -> list[str] | None:
    if shutil.which("pbcopy"):
        return ["pbcopy"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-in"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def _resolve_mode(requested: str) -> tuple[str, list[str]]:
    if requested != "auto":
        return requested, ["mode_forced"]

    reasons: list[str] = []
    in_ssh = bool(
        os.environ.get("SSH_CONNECTION")
        or os.environ.get("SSH_CLIENT")
        or os.environ.get("SSH_TTY")
    )
    if in_ssh:
        reasons.append("ssh_detected")
        return "osc52", reasons

    native = _detect_native_clipboard_cmd()
    if native:
        reasons.append(f"native_tool:{native[0]}")
        return "native", reasons

    reasons.append("default_osc52")
    return "osc52", reasons


def _render_include(
    *,
    mode: str,
    bind_keys: str,
    mouse_mode: str,
    helper_path: Path | None,
    native_cmd: list[str] | None,
) -> str:
    if mode == "osc52":
        if helper_path is None:
            raise ClipboardError("osc52 mode requires helper path")
        pipe_cmd = shlex.quote(str(helper_path))
    elif mode == "native":
        if not native_cmd:
            raise ClipboardError("native mode requested but no native clipboard command found")
        pipe_cmd = " ".join(shlex.quote(part) for part in native_cmd)
    else:
        raise ClipboardError(f"unsupported clipboard mode: {mode}")

    key = "Y" if bind_keys == "minimal" else "y"
    # "tmux" and "scroll" both enable tmux mouse; only "terminal" disables it.
    # "scroll" keeps mouse on for scrolling but skips the MouseDrag bindings so
    # the terminal can handle direct selection/copy (e.g. Shift-drag in iTerm2).
    tmux_mouse = "on" if mouse_mode in {"tmux", "scroll"} else "off"
    lines = [
        "# Managed by occtl. Re-run `oc clipboard setup` to update.",
        "set -s set-clipboard on",
        "set -g allow-passthrough on",
        "set -as terminal-features ',xterm*:clipboard'",
        f'set -g mouse "{tmux_mouse}"',
        'set -g @oc_clipboard_loaded "1"',
        f'set -g @oc_clipboard_mode "{mode}"',
        f'set -g @oc_clipboard_mouse_mode "{mouse_mode}"',
        f'set -g @oc_clipboard_pipe "{pipe_cmd}"',
    ]

    escaped = pipe_cmd.replace('"', '\\"')
    if mouse_mode == "tmux":
        lines.extend(
            [
                (
                    'bind-key -n MouseDrag1Pane if-shell -F "#{mouse_any_flag}" '
                    '"send-keys -M" "copy-mode -M"'
                ),
                (
                    f"bind-key -T copy-mode-vi MouseDragEnd1Pane "
                    f'send-keys -X copy-pipe-and-cancel "{escaped}"'
                ),
                (
                    f"bind-key -T copy-mode MouseDragEnd1Pane "
                    f'send-keys -X copy-pipe-and-cancel "{escaped}"'
                ),
            ]
        )

    if bind_keys != "none":
        lines.extend(
            [
                (f'bind-key -T copy-mode-vi {key} send-keys -X copy-pipe-and-cancel "{escaped}"'),
                (f'bind-key -T copy-mode {key} send-keys -X copy-pipe-and-cancel "{escaped}"'),
            ]
        )

    return "\n".join(lines) + "\n"


def _ensure_osc52_helper(path: Path) -> None:
    script = """#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import sys


VERSION = "1"
MAX_BYTES = 100000


def _emit(payload: bytes) -> int:
    if len(payload) > MAX_BYTES:
        sys.stderr.write("oc-osc52-copy: input too large\\n")
        return 1
    encoded = base64.b64encode(payload).decode("ascii")
    sys.stdout.write(f"\\033]52;c;{encoded}\\a")
    sys.stdout.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="oc-osc52-copy")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--emit-test", default="")
    args = parser.parse_args()

    if args.version:
        print(f"oc-osc52-copy {VERSION}")
        return 0

    if args.emit_test:
        return _emit(args.emit_test.encode("utf-8"))

    data = sys.stdin.buffer.read()
    return _emit(data)


if __name__ == "__main__":
    raise SystemExit(main())
"""
    _atomic_write(path, script, mode=0o700)


def _select_tmux_conf(override: str | None) -> Path:
    if override:
        return Path(override).expanduser()

    candidates = [
        Path.home() / ".config" / "tmux" / "tmux.conf",
        Path.home() / ".tmux.conf",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def _resolve_edit_target(path: Path, *, follow_symlink: bool) -> Path:
    if path.is_symlink() and not follow_symlink:
        raise ClipboardError(
            f"refusing to edit symlinked tmux config: {path} (pass --follow-symlink)"
        )
    return path.resolve() if path.is_symlink() else path


def _tmux_env_socket() -> str:
    raw = (os.environ.get("TMUX") or "").strip()
    if not raw:
        return ""
    return raw.split(",", 1)[0].strip()


def _candidate_tmux_sockets(explicit_socket: str | None) -> list[str]:
    if explicit_socket:
        return [explicit_socket]

    candidates: list[str] = []

    env_socket = _tmux_env_socket()
    if env_socket:
        candidates.append(env_socket)

    default_dir = Path("/tmp") / f"tmux-{os.getuid()}"
    if default_dir.exists():
        try:
            for entry in sorted(default_dir.iterdir()):
                if entry.is_socket() or entry.name == "default":
                    candidates.append(str(entry))
        except OSError:
            pass

    default_socket = str(default_dir / "default")
    candidates.append(default_socket)

    deduped: list[str] = []
    seen: set[str] = set()
    for socket_path in candidates:
        if not socket_path or socket_path in seen:
            continue
        seen.add(socket_path)
        deduped.append(socket_path)
    return deduped


def _probe_socket_loaded(socket_path: str) -> tuple[bool | None, str]:
    try:
        loaded = tmux.show_global_option("@oc_clipboard_loaded", socket_path=socket_path)
        mode = tmux.show_global_option("@oc_clipboard_mode", socket_path=socket_path)
        return loaded == "1", mode
    except tmux.TmuxError:
        return None, ""


def _resolve_tmux_loaded_state(
    explicit_socket: str | None,
) -> tuple[bool | None, str, str, list[str], list[str], bool]:
    candidates = _candidate_tmux_sockets(explicit_socket)
    socket_states: dict[str, tuple[bool | None, str]] = {}
    reachable: list[str] = []
    for socket_path in candidates:
        loaded, mode = _probe_socket_loaded(socket_path)
        socket_states[socket_path] = (loaded, mode)
        if loaded is not None:
            reachable.append(socket_path)

    ambiguous = False
    used_socket = ""
    loaded_in_tmux: bool | None = None
    loaded_mode = ""
    env_socket = _tmux_env_socket()

    if explicit_socket:
        used_socket = explicit_socket
    elif env_socket and env_socket in reachable:
        used_socket = env_socket
    elif len(reachable) == 1:
        used_socket = reachable[0]
    elif len(reachable) > 1:
        ambiguous = True

    if used_socket and used_socket in socket_states:
        loaded_value, mode_value = socket_states[used_socket]
        loaded_in_tmux = loaded_value
        loaded_mode = mode_value

    return loaded_in_tmux, loaded_mode, used_socket, candidates, reachable, ambiguous


def load_state() -> dict:
    return _read_json(_state_file())


def setup(
    *,
    mode: str,
    tmux_conf: str | None,
    tmux_socket: str | None,
    dry_run: bool,
    print_snippet: bool,
    reload_tmux: bool,
    bind_keys: str,
    follow_symlink: bool,
    mouse_mode: str = "tmux",
) -> dict:
    with _operation_lock():
        if bind_keys not in {"minimal", "copy-mode-y", "none"}:
            raise ClipboardError(f"unsupported bind mode: {bind_keys}")
        if mouse_mode not in {"terminal", "tmux", "scroll"}:
            raise ClipboardError(f"unsupported mouse mode: {mouse_mode}")

        selected_mode, reasons = _resolve_mode(mode)
        native_cmd = _detect_native_clipboard_cmd() if selected_mode == "native" else None

        config.ensure_config_dir()
        requested_conf = _select_tmux_conf(tmux_conf)
        edit_conf = _resolve_edit_target(requested_conf, follow_symlink=follow_symlink)

        include_path = _include_file()
        helper_path = _helper_file() if selected_mode == "osc52" else None

        include_text = _render_include(
            mode=selected_mode,
            bind_keys=bind_keys,
            mouse_mode=mouse_mode,
            helper_path=helper_path,
            native_cmd=native_cmd,
        )
        snippet = _source_block(include_path)

        if print_snippet:
            return {
                "mode": selected_mode,
                "mouse_mode": mouse_mode,
                "snippet": snippet,
                "include_text": include_text,
                "reasons": reasons,
                "tmux_conf": str(edit_conf),
                "dry_run": dry_run,
            }

        original = edit_conf.read_text(encoding="utf-8") if edit_conf.exists() else ""
        updated = _upsert_source_block(original, include_path)

        if dry_run:
            return {
                "mode": selected_mode,
                "mouse_mode": mouse_mode,
                "snippet": snippet,
                "include_text": include_text,
                "reasons": reasons,
                "tmux_conf": str(edit_conf),
                "dry_run": True,
                "changes": {
                    "tmux_conf_changed": updated != original,
                    "include_changed": True,
                },
            }

        backup_file = config.CONFIG_DIR / f"tmux.conf.backup.{int(time.time())}"
        target_existed_before = edit_conf.exists()
        if target_existed_before:
            _atomic_write(backup_file, original, mode=0o600)

        if selected_mode == "osc52" and helper_path is not None:
            _ensure_osc52_helper(helper_path)

        _atomic_write(include_path, include_text, mode=0o600)
        _atomic_write(edit_conf, updated)

        reload_error = ""
        if reload_tmux:
            try:
                tmux.source_file(str(edit_conf), socket_path=tmux_socket)
            except tmux.TmuxError as e:
                reload_error = str(e)

        state = {
            "schema_version": 1,
            "mode": selected_mode,
            "bind_keys": bind_keys,
            "mouse_mode": mouse_mode,
            "tmux_conf": str(edit_conf),
            "include_file": str(include_path),
            "helper_file": str(helper_path) if helper_path else "",
            "backup_file": str(backup_file) if target_existed_before else "",
            "target_existed_before": target_existed_before,
            "before_hash": _sha256(original),
            "after_hash": _sha256(updated),
            "installed_at": int(time.time()),
            "emission_verified": False,
            "clipboard_verified": False,
            "verified_at": 0,
        }
        _write_json(_state_file(), state)

        return {
            "mode": selected_mode,
            "mouse_mode": mouse_mode,
            "tmux_conf": str(edit_conf),
            "include_file": str(include_path),
            "helper_file": str(helper_path) if helper_path else "",
            "reasons": reasons,
            "reload_error": reload_error,
            "dry_run": False,
        }


def status(*, tmux_socket: str | None) -> dict:
    state = load_state()
    tmux_conf = Path(state.get("tmux_conf", "")).expanduser() if state.get("tmux_conf") else None
    include_file = (
        Path(state.get("include_file", "")).expanduser()
        if state.get("include_file")
        else _include_file()
    )

    reasons: list[str] = []
    configured_on_disk = False
    marker_present = False
    if tmux_conf and tmux_conf.exists():
        marker_present = MARKER_BEGIN in tmux_conf.read_text(encoding="utf-8")
    include_exists = include_file.exists()
    configured_on_disk = marker_present and include_exists
    if not configured_on_disk:
        reasons.append("not_fully_configured_on_disk")

    (
        loaded_in_tmux,
        loaded_mode,
        tmux_socket_used,
        tmux_socket_candidates,
        tmux_socket_reachable,
        tmux_socket_ambiguous,
    ) = _resolve_tmux_loaded_state(tmux_socket)
    if tmux_socket_ambiguous:
        reasons.append("tmux_socket_ambiguous")
    elif loaded_in_tmux is None:
        reasons.append("tmux_unreachable")
    elif not loaded_in_tmux:
        reasons.append("tmux_not_loaded")

    helper_file = (
        Path(state.get("helper_file", "")).expanduser() if state.get("helper_file") else None
    )
    helper_health: bool | None = None
    helper_kind = "none"
    if state.get("mode") == "osc52":
        helper_kind = "standalone"
        if helper_file and helper_file.exists() and os.access(helper_file, os.X_OK):
            try:
                out = subprocess.check_output(
                    [str(helper_file), "--version"],
                    text=True,
                    stderr=subprocess.PIPE,
                ).strip()
                helper_health = out.startswith("oc-osc52-copy")
            except (OSError, subprocess.CalledProcessError):
                helper_health = False
        else:
            helper_health = False
        if helper_health is False:
            reasons.append("helper_unhealthy")

    verification = {
        "emission_verified": bool(state.get("emission_verified")),
        "clipboard_verified": bool(state.get("clipboard_verified")),
        "verified_at": int(state.get("verified_at") or 0),
    }

    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "configured_on_disk": configured_on_disk,
        "loaded_in_tmux": loaded_in_tmux,
        "selected_mode": state.get("mode") or "",
        "mouse_mode": state.get("mouse_mode") or "",
        "loaded_mode": loaded_mode,
        "tmux_socket_used": tmux_socket_used,
        "tmux_socket_candidates": tmux_socket_candidates,
        "tmux_socket_reachable": tmux_socket_reachable,
        "tmux_socket_ambiguous": tmux_socket_ambiguous,
        "helper_kind": helper_kind,
        "helper_health": helper_health,
        "tmux_conf": str(tmux_conf) if tmux_conf else "",
        "include_file": str(include_file),
        "verification": verification,
        "reasons": reasons,
    }


def _run_emission_check(mode: str, helper_file: str, token: str) -> bool:
    if mode != "osc52":
        return True
    if not helper_file:
        return False
    try:
        out = subprocess.check_output(
            [helper_file, "--emit-test", token],
            text=False,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return False

    encoded = base64.b64encode(token.encode("utf-8"))
    return b"]52;c;" in out and encoded in out


def verify(*, strict: bool) -> dict:
    with _operation_lock():
        state = load_state()
        if not state:
            raise ClipboardError("clipboard is not configured; run: oc clipboard setup")

        token = f"oc-{secrets.token_hex(4)}"
        emission_ok = _run_emission_check(
            state.get("mode", ""), state.get("helper_file", ""), token
        )
        state["emission_verified"] = emission_ok

        if not emission_ok:
            state["clipboard_verified"] = False
            state["verified_at"] = int(time.time())
            _write_json(_state_file(), state)
            return {
                "emission_verified": False,
                "clipboard_verified": False,
                "token": token,
                "strict": strict,
            }

        if not os.isatty(0):
            state["clipboard_verified"] = False
            state["verified_at"] = int(time.time())
            _write_json(_state_file(), state)
            return {
                "emission_verified": True,
                "clipboard_verified": False,
                "token": token,
                "strict": strict,
            }

        pasted = input("Paste the clipboard token now: ").strip()
        clipboard_ok = pasted == token

        if strict and clipboard_ok and state.get("mode") == "osc52":
            token2 = f"oc-{secrets.token_hex(4)}"
            emitted2 = _run_emission_check("osc52", state.get("helper_file", ""), token2)
            if emitted2:
                pasted2 = input("Strict mode: paste second token: ").strip()
                clipboard_ok = pasted2 == token2
            else:
                clipboard_ok = False

        state["clipboard_verified"] = clipboard_ok
        state["verified_at"] = int(time.time())
        _write_json(_state_file(), state)
        return {
            "emission_verified": True,
            "clipboard_verified": clipboard_ok,
            "token": token,
            "strict": strict,
        }


def uninstall(
    *,
    tmux_conf: str | None,
    remove_helper: bool,
    follow_symlink: bool,
) -> dict:
    with _operation_lock():
        state = load_state()
        if not state:
            return {"removed": False, "message": "clipboard not configured"}

        configured_tmux_conf = state.get("tmux_conf", "")
        if not tmux_conf and not configured_tmux_conf:
            raise ClipboardError("missing tmux config path in clipboard state")
        target_path = Path(tmux_conf).expanduser() if tmux_conf else Path(configured_tmux_conf)
        target = _resolve_edit_target(target_path, follow_symlink=follow_symlink)

        include_file = Path(state.get("include_file", "")).expanduser()
        helper_file = (
            Path(state.get("helper_file", "")).expanduser() if state.get("helper_file") else None
        )

        restored = False
        removed_block = False
        if target.exists():
            current = target.read_text(encoding="utf-8")
            current_hash = _sha256(current)
            after_hash = state.get("after_hash", "")
            backup_file = (
                Path(state.get("backup_file", "")).expanduser()
                if state.get("backup_file")
                else None
            )
            target_existed_before = bool(state.get("target_existed_before"))

            if current_hash == after_hash:
                if target_existed_before and backup_file and backup_file.exists():
                    backup_text = backup_file.read_text(encoding="utf-8")
                    _atomic_write(target, backup_text)
                    restored = True
                elif not target_existed_before:
                    target.unlink(missing_ok=True)
                    restored = True

            if not restored and target.exists():
                cleaned, had_block = _remove_managed_block(target.read_text(encoding="utf-8"))
                if had_block:
                    _atomic_write(target, cleaned)
                    removed_block = True

        include_file.unlink(missing_ok=True)
        if remove_helper and helper_file is not None:
            helper_file.unlink(missing_ok=True)

        _state_file().unlink(missing_ok=True)

        return {
            "removed": True,
            "restored_backup": restored,
            "removed_block": removed_block,
            "removed_include": True,
            "removed_helper": bool(remove_helper and helper_file),
        }
