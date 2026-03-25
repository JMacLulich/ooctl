from __future__ import annotations

import resource
import subprocess

import pytest

from occtl import tmux


def test_run_raises_tmux_error_when_tmux_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_file_not_found(*_: object, **__: object) -> str:
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(subprocess, "check_output", _raise_file_not_found)

    with pytest.raises(tmux.TmuxError, match="not installed"):
        tmux.run(["tmux", "list-sessions"])


def test_has_session_raises_tmux_error_when_tmux_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_file_not_found(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(subprocess, "run", _raise_file_not_found)

    with pytest.raises(tmux.TmuxError, match="not installed"):
        tmux.has_session("infra")


def test_capture_last_lines_returns_empty_on_tmux_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_tmux_error(_: object) -> str:
        raise tmux.TmuxError("capture failed")

    monkeypatch.setattr(tmux, "run", _raise_tmux_error)

    assert tmux.capture_last_lines("infra") == ""


def test_attach_raises_nofile_limit_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    monkeypatch.setattr(resource, "getrlimit", lambda _kind: (256, 4096))

    def _setrlimit(_kind: int, limits: tuple[int, int]) -> None:
        seen["limits"] = limits

    def _check_call(cmd: list[str]) -> None:
        seen["cmd"] = cmd

    monkeypatch.setattr(resource, "setrlimit", _setrlimit)
    monkeypatch.setattr(subprocess, "check_call", _check_call)

    tmux.attach("infra")

    assert seen["limits"] == (1024, 4096)
    assert seen["cmd"] == ["tmux", "attach", "-t", "infra"]


def test_attach_keeps_existing_nofile_limit_when_already_high_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"setrlimit": False}

    monkeypatch.setattr(resource, "getrlimit", lambda _kind: (2048, 4096))
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda _kind, _limits: called.__setitem__("setrlimit", True),
    )
    monkeypatch.setattr(subprocess, "check_call", lambda _cmd: None)

    tmux.attach("infra")

    assert called["setrlimit"] is False


def test_show_global_option_uses_socket_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, list[str]] = {}

    def _run(cmd: list[str]) -> str:
        seen["cmd"] = cmd
        return "1"

    monkeypatch.setattr(tmux, "run", _run)

    out = tmux.show_global_option("@oc_clipboard_loaded", socket_path="/tmp/tmux-test.sock")

    assert out == "1"
    assert seen["cmd"][:3] == ["tmux", "-S", "/tmp/tmux-test.sock"]


def test_version_trims_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux, "run", lambda _cmd: "tmux 3.4\n")

    assert tmux.version() == "tmux 3.4"


def test_list_sessions_with_paths_returns_path_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tmux,
        "run",
        lambda _cmd: "myproject\t1\t3\t/home/user/myproject\nworker\t0\t1\t/home/user/myproject",
    )

    rows = tmux.list_sessions_with_paths()

    assert len(rows) == 2
    assert rows[0] == {
        "name": "myproject",
        "attached": True,
        "windows": 3,
        "path": "/home/user/myproject",
    }
    assert rows[1] == {
        "name": "worker",
        "attached": False,
        "windows": 1,
        "path": "/home/user/myproject",
    }


def test_list_sessions_with_paths_returns_empty_on_tmux_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_cmd: object) -> str:
        raise tmux.TmuxError("no server")

    monkeypatch.setattr(tmux, "run", _raise)

    assert tmux.list_sessions_with_paths() == []
