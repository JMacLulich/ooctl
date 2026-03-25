from __future__ import annotations

import resource
import subprocess
from collections.abc import Sequence


class TmuxError(RuntimeError):
    pass


def _tmux_missing() -> TmuxError:
    return TmuxError("tmux is not installed or not on PATH")


def _with_socket(cmd: list[str], socket_path: str | None) -> list[str]:
    if not socket_path:
        return cmd
    if cmd and cmd[0] == "tmux":
        return ["tmux", "-S", socket_path, *cmd[1:]]
    return ["tmux", "-S", socket_path, *cmd]


def run(cmd: Sequence[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE).strip()
    except FileNotFoundError as e:
        raise _tmux_missing() from e
    except subprocess.CalledProcessError as e:
        details = (e.stderr or "").strip()
        if details:
            raise TmuxError(f"Command failed: {' '.join(cmd)} ({details})") from e
        raise TmuxError(f"Command failed: {' '.join(cmd)}") from e


def has_session(name: str) -> bool:
    try:
        p = subprocess.run(
            ["tmux", "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as e:
        raise _tmux_missing() from e
    return p.returncode == 0


def list_sessions() -> list[dict]:
    try:
        out = run(
            [
                "tmux",
                "list-sessions",
                "-F",
                "#{session_name}\t#{session_attached}\t#{session_windows}",
            ]
        )
    except TmuxError:
        return []
    rows = []
    for line in out.splitlines():
        name, attached, windows = line.split("\t")
        rows.append({"name": name, "attached": attached == "1", "windows": int(windows)})
    return rows


def list_sessions_with_paths() -> list[dict]:
    """Like list_sessions() but also returns the session working directory."""
    try:
        out = run(
            [
                "tmux",
                "list-sessions",
                "-F",
                "#{session_name}\t#{session_attached}\t#{session_windows}\t#{session_path}",
            ]
        )
    except TmuxError:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        name, attached, windows, path = parts
        rows.append(
            {
                "name": name,
                "attached": attached == "1",
                "windows": int(windows),
                "path": path,
            }
        )
    return rows


def new_session(name: str, workdir: str) -> None:
    run(["tmux", "new-session", "-d", "-s", name, "-n", "main", "-c", workdir])


def new_window(name: str, window: str, workdir: str) -> None:
    run(["tmux", "new-window", "-t", name, "-n", window, "-c", workdir])


def send_keys(target: str, keys: list[str]) -> None:
    run(["tmux", "send-keys", "-t", target, *keys])


def _ensure_attach_nofile_limit(minimum: int = 1024) -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (AttributeError, OSError, ValueError):
        return

    target = max(soft, minimum)
    if hard != resource.RLIM_INFINITY:
        target = min(target, hard)

    if target <= soft:
        return

    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError):
        return


def attach(name: str, control_mode: bool = False) -> None:
    _ensure_attach_nofile_limit()
    cmd = ["tmux"]
    if control_mode:
        cmd.append("-CC")
    cmd.extend(["attach", "-t", name])
    try:
        subprocess.check_call(cmd)
    except FileNotFoundError as e:
        raise _tmux_missing() from e
    except subprocess.CalledProcessError as e:
        rendered_cmd = " ".join(cmd)
        raise TmuxError(f"Command failed: {rendered_cmd}") from e


def kill_session(name: str) -> None:
    run(["tmux", "kill-session", "-t", name])


def pane_last_activity(session: str, window: str = "main") -> int:
    out = run(
        ["tmux", "display-message", "-p", "-t", f"{session}:{window}", "#{pane_last_activity}"]
    )
    try:
        return int(out)
    except ValueError:
        return 0


def capture_last_lines(session: str, window: str = "main", lines: int = 120) -> str:
    try:
        return run(
            [
                "tmux",
                "capture-pane",
                "-p",
                "-t",
                f"{session}:{window}",
                "-S",
                f"-{lines}",
            ]
        )
    except TmuxError:
        return ""


def source_file(path: str, socket_path: str | None = None) -> None:
    run(_with_socket(["tmux", "source-file", path], socket_path))


def show_global_option(name: str, socket_path: str | None = None) -> str:
    return run(_with_socket(["tmux", "show-options", "-gqv", name], socket_path)).strip()


def version(socket_path: str | None = None) -> str:
    out = run(_with_socket(["tmux", "-V"], socket_path))
    return out.strip()
