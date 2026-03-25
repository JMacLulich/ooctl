from __future__ import annotations

import os
from pathlib import Path

import pytest

from occtl import clipboard, config, tmux


def _set_config_paths(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / ".config" / "occtl"
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "MAPPINGS_FILE", config_dir / "mappings.toml")
    monkeypatch.setattr(config, "STATE_FILE", config_dir / "state.json")


def test_setup_dry_run_keeps_tmux_conf_unchanged(tmp_path: Path, monkeypatch) -> None:
    _set_config_paths(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(clipboard.Path, "home", lambda: home)

    tmux_conf = home / ".tmux.conf"
    tmux_conf.write_text("set -g mouse on\n", encoding="utf-8")

    result = clipboard.setup(
        mode="osc52",
        tmux_conf=str(tmux_conf),
        tmux_socket=None,
        dry_run=True,
        print_snippet=False,
        reload_tmux=False,
        bind_keys="minimal",
        follow_symlink=False,
    )

    assert result["mode"] == "osc52"
    assert result["changes"]["tmux_conf_changed"] is True
    assert clipboard.MARKER_BEGIN in result["snippet"]
    assert 'set -g mouse "on"' in result["include_text"]
    assert 'bind-key -n MouseDrag1Pane if-shell -F "#{mouse_any_flag}"' in result["include_text"]
    assert "bind-key -T copy-mode-vi MouseDragEnd1Pane" in result["include_text"]
    assert '@oc_clipboard_mode "osc52"' in result["include_text"]
    assert tmux_conf.read_text(encoding="utf-8") == "set -g mouse on\n"


def test_setup_is_idempotent_for_tmux_source_block(tmp_path: Path, monkeypatch) -> None:
    _set_config_paths(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(clipboard.Path, "home", lambda: home)

    tmux_conf = home / ".tmux.conf"
    tmux_conf.write_text("set -g mouse on\n", encoding="utf-8")

    clipboard.setup(
        mode="osc52",
        tmux_conf=str(tmux_conf),
        tmux_socket=None,
        dry_run=False,
        print_snippet=False,
        reload_tmux=False,
        bind_keys="minimal",
        follow_symlink=False,
    )
    clipboard.setup(
        mode="osc52",
        tmux_conf=str(tmux_conf),
        tmux_socket=None,
        dry_run=False,
        print_snippet=False,
        reload_tmux=False,
        bind_keys="minimal",
        follow_symlink=False,
    )

    text = tmux_conf.read_text(encoding="utf-8")
    assert text.count(clipboard.MARKER_BEGIN) == 1
    state = clipboard.load_state()
    assert state["mode"] == "osc52"
    assert Path(state["include_file"]).exists()
    assert Path(state["helper_file"]).exists()
    assert os.access(Path(state["helper_file"]), os.X_OK)


def test_status_reports_tmux_unreachable(tmp_path: Path, monkeypatch) -> None:
    _set_config_paths(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(clipboard.Path, "home", lambda: home)

    tmux_conf = home / ".tmux.conf"
    tmux_conf.write_text("set -g mouse on\n", encoding="utf-8")
    clipboard.setup(
        mode="osc52",
        tmux_conf=str(tmux_conf),
        tmux_socket=None,
        dry_run=False,
        print_snippet=False,
        reload_tmux=False,
        bind_keys="minimal",
        follow_symlink=False,
    )

    def _raise_tmux(*_args: object, **_kwargs: object) -> str:
        raise tmux.TmuxError("tmux down")

    monkeypatch.setattr(tmux, "show_global_option", _raise_tmux)

    result = clipboard.status(tmux_socket=None)
    assert result["configured_on_disk"] is True
    assert result["loaded_in_tmux"] is None
    assert "tmux_unreachable" in result["reasons"]


def test_uninstall_restores_previous_tmux_conf(tmp_path: Path, monkeypatch) -> None:
    _set_config_paths(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(clipboard.Path, "home", lambda: home)

    tmux_conf = home / ".tmux.conf"
    original = "set -g mouse on\nset -g history-limit 5000\n"
    tmux_conf.write_text(original, encoding="utf-8")
    clipboard.setup(
        mode="osc52",
        tmux_conf=str(tmux_conf),
        tmux_socket=None,
        dry_run=False,
        print_snippet=False,
        reload_tmux=False,
        bind_keys="minimal",
        follow_symlink=False,
    )

    result = clipboard.uninstall(
        tmux_conf=str(tmux_conf),
        remove_helper=True,
        follow_symlink=False,
    )

    assert result["removed"] is True
    assert result["restored_backup"] is True
    assert tmux_conf.read_text(encoding="utf-8") == original
    assert not (config.CONFIG_DIR / "clipboard.json").exists()


def test_setup_fails_when_lock_is_held(tmp_path: Path, monkeypatch) -> None:
    _set_config_paths(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(clipboard.Path, "home", lambda: home)
    config.ensure_config_dir()

    lock = config.CONFIG_DIR / "clipboard.lock"
    lock.write_text('{"pid": 123, "created_at": 9999999999}\n', encoding="utf-8")

    with pytest.raises(clipboard.ClipboardError, match="already in progress"):
        clipboard.setup(
            mode="osc52",
            tmux_conf=str(home / ".tmux.conf"),
            tmux_socket=None,
            dry_run=False,
            print_snippet=False,
            reload_tmux=False,
            bind_keys="minimal",
            follow_symlink=False,
        )


def test_setup_removes_stale_lock(tmp_path: Path, monkeypatch) -> None:
    _set_config_paths(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(clipboard.Path, "home", lambda: home)
    config.ensure_config_dir()

    lock = config.CONFIG_DIR / "clipboard.lock"
    lock.write_text('{"pid": 123, "created_at": 1}\n', encoding="utf-8")

    result = clipboard.setup(
        mode="osc52",
        tmux_conf=str(home / ".tmux.conf"),
        tmux_socket=None,
        dry_run=True,
        print_snippet=False,
        reload_tmux=False,
        bind_keys="minimal",
        follow_symlink=False,
    )

    assert result["mode"] == "osc52"
    assert not lock.exists()


def test_status_reports_socket_ambiguity(tmp_path: Path, monkeypatch) -> None:
    _set_config_paths(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(clipboard.Path, "home", lambda: home)

    tmux_conf = home / ".tmux.conf"
    tmux_conf.write_text("set -g mouse on\n", encoding="utf-8")
    clipboard.setup(
        mode="osc52",
        tmux_conf=str(tmux_conf),
        tmux_socket=None,
        dry_run=False,
        print_snippet=False,
        reload_tmux=False,
        bind_keys="minimal",
        follow_symlink=False,
    )

    monkeypatch.setattr(
        clipboard,
        "_candidate_tmux_sockets",
        lambda _socket: ["/tmp/tmux-a", "/tmp/tmux-b"],
    )

    def _probe(socket_path: str) -> tuple[bool | None, str]:
        if socket_path == "/tmp/tmux-a":
            return True, "osc52"
        return False, "native"

    monkeypatch.setattr(clipboard, "_probe_socket_loaded", _probe)

    data = clipboard.status(tmux_socket=None)
    assert data["loaded_in_tmux"] is None
    assert data["tmux_socket_ambiguous"] is True
    assert "tmux_socket_ambiguous" in data["reasons"]
    assert data["tmux_socket_reachable"] == ["/tmp/tmux-a", "/tmp/tmux-b"]
