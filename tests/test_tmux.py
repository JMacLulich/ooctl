from __future__ import annotations

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
