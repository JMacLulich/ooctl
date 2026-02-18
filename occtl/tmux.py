from __future__ import annotations

import subprocess
from collections.abc import Sequence


class TmuxError(RuntimeError):
    pass


def run(cmd: Sequence[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True).strip()
    except subprocess.CalledProcessError as e:
        raise TmuxError(f"Command failed: {' '.join(cmd)}") from e


def has_session(name: str) -> bool:
    p = subprocess.run(
        ["tmux", "has-session", "-t", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
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


def new_session(name: str, workdir: str) -> None:
    run(["tmux", "new-session", "-d", "-s", name, "-n", "main", "-c", workdir])


def new_window(name: str, window: str, workdir: str) -> None:
    run(["tmux", "new-window", "-t", name, "-n", window, "-c", workdir])


def send_keys(target: str, keys: list[str]) -> None:
    run(["tmux", "send-keys", "-t", target, *keys])


def attach(name: str) -> None:
    subprocess.check_call(["tmux", "attach", "-t", name])


def pane_last_activity(session: str, window: str = "main") -> int:
    out = run(
        ["tmux", "display-message", "-p", "-t", f"{session}:{window}", "#{pane_last_activity}"]
    )
    try:
        return int(out)
    except ValueError:
        return 0
