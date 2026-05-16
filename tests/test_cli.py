from __future__ import annotations

import argparse
import builtins
import os
from pathlib import Path

import pytest

from occtl import cli, config, tmux
from occtl.cli import _bash_completion_script, _fish_completion_script, _zsh_completion_script


def test_cli_completion_bash_contains_commands() -> None:
    script = _bash_completion_script()
    assert "_occtl_complete" in script
    assert "complete -F _occtl_complete oc" in script
    assert "_occtl_tmux_sessions" in script
    assert "attach|focus|kill" in script


def test_cli_completion_zsh_contains_compdef() -> None:
    script = _zsh_completion_script()
    assert "#compdef oc" in script
    assert "compdef _occtl oc" in script
    assert "tmux list-sessions" in script


def test_cli_completion_fish_contains_command() -> None:
    script = _fish_completion_script()
    assert "complete -c oc -f" in script
    assert "attach focus kill" in script


def test_map_command_allows_spaced_session_names() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["map", "gig guide", "/tmp/gig"])

    assert args.name == "gig guide"
    assert args.path == "/tmp/gig"


def test_attach_command_name_is_optional() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["attach"])

    assert args.name is None


def test_attach_command_supports_cc_flag() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["attach", "--cc"])

    assert args.cc is True


def test_clipboard_setup_parser_accepts_flags() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "clipboard",
            "setup",
            "--mode",
            "osc52",
            "--dry-run",
            "--bind-keys",
            "minimal",
            "--mouse-mode",
            "tmux",
            "--reload",
        ]
    )

    assert args.mode == "osc52"
    assert args.dry_run is True
    assert args.bind_keys == "minimal"
    assert args.mouse_mode == "tmux"
    assert args.reload is True


def test_clipboard_setup_parser_defaults_to_scroll_mouse_mode() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["clipboard", "setup"])

    assert args.bind_keys == "copy-mode-y"
    assert args.mouse_mode == "scroll"


def test_clipboard_status_parser_supports_json() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["clipboard", "status", "--json"])

    assert args.json is True


def test_mailbox_link_parser_accepts_rig_and_session() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["mailbox", "link", "cash-claw-rig-b", "--rig", "Rig B"])

    assert args.session == "cash-claw-rig-b"
    assert args.rig == "Rig B"
    assert args.window == "auto"


def test_cmd_mailbox_link_updates_rigs_toml(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "cash-claw"
    mailbox_dir = workspace / ".rig-mailbox"
    mailbox_dir.mkdir(parents=True)
    rigs_file = mailbox_dir / "rigs.toml"
    rigs_file.write_text(
        "\n".join(
            [
                "# existing config",
                "",
                '[rigs."rig-b"]',
                'runtime = "codex"',
                'notifier = "applescript-iterm"',
                'session_name = "old-title"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.config, "get_mapping", lambda _name: None)
    monkeypatch.setattr(cli.tmux, "list_window_details", lambda: {})
    env_calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        cli.tmux,
        "set_session_environment",
        lambda session, values: env_calls.append((session, values)),
    )

    rc = cli.cmd_mailbox_link(
        argparse.Namespace(
            workspace=str(workspace),
            rig="Rig B",
            session="cash-claw-rig-b",
            runtime="codex",
            window="auto",
        )
    )

    assert rc == 0
    text = rigs_file.read_text(encoding="utf-8")
    assert '[rigs."rig-b"]' in text
    assert 'notifier = "tmux"' in text
    assert 'tmux_target = "cash-claw-rig-b:main"' in text
    assert "session_name" not in text
    assert env_calls == [
        (
            "cash-claw-rig-b",
            {
                "RIG_NAME": "Rig B",
                "RIG_WORKSPACE": str(workspace.resolve()),
            },
        )
    ]
    assert "linked:\tRig B -> cash-claw-rig-b:main" in capsys.readouterr().out


def test_cmd_mailbox_link_auto_targets_agent_window(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "cash-claw"
    (workspace / ".rig-mailbox").mkdir(parents=True)
    monkeypatch.setattr(
        cli.tmux,
        "list_window_details",
        lambda: {
            "cash-claw-rig-b": {
                "active_window": "shell",
                "active_command": "node",
                "main_command": "opencode",
                "window_list": [
                    {"name": "main", "active": False, "command": "opencode"},
                    {"name": "shell", "active": True, "command": "node"},
                ],
            }
        },
    )
    monkeypatch.setattr(cli.tmux, "set_session_environment", lambda *_args: None)

    rc = cli.cmd_mailbox_link(
        argparse.Namespace(
            workspace=str(workspace),
            rig="Rig B",
            session="cash-claw-rig-b",
            runtime="codex",
            window="auto",
        )
    )

    assert rc == 0
    text = (workspace / ".rig-mailbox" / "rigs.toml").read_text(encoding="utf-8")
    assert 'tmux_target = "cash-claw-rig-b:shell"' in text
    assert "linked:\tRig B -> cash-claw-rig-b:shell" in capsys.readouterr().out


def test_mailbox_without_subcommand_runs_wizard() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["mailbox"])

    assert args.fn == cli.cmd_mailbox_wizard


def test_cmd_mailbox_wizard_creates_missing_sessions_and_links(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    workspace = tmp_path / "zoom-mvps"
    (workspace / ".rig-mailbox").mkdir(parents=True)
    answers = iter(
        [
            str(workspace),
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
    )
    created: list[tuple[str, str]] = []
    sent: list[tuple[str, list[str]]] = []
    env_calls: list[tuple[str, dict[str, str]]] = []
    existing: set[str] = set()

    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(cli.tmux, "list_sessions", lambda: [])
    monkeypatch.setattr(cli.tmux, "has_session", lambda name: name in existing)

    def _new_session(name: str, workdir: str) -> None:
        created.append((name, workdir))
        existing.add(name)

    monkeypatch.setattr(cli.tmux, "new_session", _new_session)
    monkeypatch.setattr(cli.tmux, "send_keys", lambda target, keys: sent.append((target, keys)))
    monkeypatch.setattr(
        cli.tmux,
        "set_session_environment",
        lambda session, values: env_calls.append((session, values)),
    )

    rc = cli.cmd_mailbox_wizard(argparse.Namespace())

    assert rc == 0
    assert created == [
        ("zoom-mvps-rig-a", str(workspace.resolve())),
        ("zoom-mvps-rig-b", str(workspace.resolve())),
    ]
    assert sent == [
        ("zoom-mvps-rig-a:main", ["claude", "Enter"]),
        ("zoom-mvps-rig-b:main", ["codex", "Enter"]),
    ]
    assert env_calls[:2] == [
        (
            "zoom-mvps-rig-a",
            {
                "RIG_NAME": "Rig A",
                "RIG_WORKSPACE": str(workspace.resolve()),
            },
        ),
        (
            "zoom-mvps-rig-b",
            {
                "RIG_NAME": "Rig B",
                "RIG_WORKSPACE": str(workspace.resolve()),
            },
        ),
    ]
    text = (workspace / ".rig-mailbox" / "rigs.toml").read_text(encoding="utf-8")
    assert 'tmux_target = "zoom-mvps-rig-a:main"' in text
    assert 'tmux_target = "zoom-mvps-rig-b:main"' in text
    assert "linked:\tRig A -> zoom-mvps-rig-a:main" in capsys.readouterr().out


def test_setting_and_loading_spaced_mapping(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / ".config" / "occtl"
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "MAPPINGS_FILE", config_dir / "mappings.toml")
    monkeypatch.setattr(config, "STATE_FILE", config_dir / "state.json")

    config.set_mapping("gig guide", str(tmp_path / "target-dir"))

    mappings = config.load_mappings()
    assert mappings["gig guide"] == str((tmp_path / "target-dir").resolve())


def test_main_handles_tmux_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "cmd_status",
        lambda _: (_ for _ in ()).throw(tmux.TmuxError("tmux missing")),
    )

    with monkeypatch.context() as m:
        m.setattr("sys.argv", ["oc"])
        with pytest.raises(SystemExit) as exc:
            cli.main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "tmux missing" in out


def test_cmd_status_prints_relay_state(monkeypatch, capsys) -> None:
    project_path = str(Path("/tmp") / "audience" / "prs" / "EC-3620" / "server")
    monkeypatch.setattr(cli.config, "get_focus", lambda: "filter2")
    monkeypatch.setattr(cli.config, "get_webhook", lambda: "")
    monkeypatch.setattr(
        cli.config, "get_alert_router", lambda: "http://n100alerts:3000/webhook/infra"
    )
    monkeypatch.setattr(cli.config, "get_relay_token", lambda: "token")
    monkeypatch.setattr(
        cli.config,
        "load_mappings",
        lambda: {"filter2": project_path},
    )
    monkeypatch.setattr(cli.tmux, "has_session", lambda _: False)
    monkeypatch.setattr(cli, "_relay_status", lambda: "up")

    rc = cli.cmd_status(argparse.Namespace())

    assert rc == 0
    out = capsys.readouterr().out
    assert "relay:\tup" in out


def test_kill_uses_focused_session_and_clears_focus(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.config, "get_focus", lambda: "cash claw")
    monkeypatch.setattr(cli.tmux, "has_session", lambda name: name == "cash claw")

    called: dict[str, str | None] = {"killed": None, "focus": None}

    def _kill_session(name: str) -> None:
        called["killed"] = name

    def _set_focus(name: str) -> None:
        called["focus"] = name

    monkeypatch.setattr(cli.tmux, "kill_session", _kill_session)
    monkeypatch.setattr(cli.config, "set_focus", _set_focus)

    rc = cli.cmd_kill(argparse.Namespace(name=None))

    assert rc == 0
    assert called["killed"] == "cash claw"
    assert called["focus"] == ""
    assert "killed: cash claw" in capsys.readouterr().out


def test_kill_requires_name_or_focus(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")

    rc = cli.cmd_kill(argparse.Namespace(name=None))

    assert rc == 1
    assert "no session provided and nothing focused" in capsys.readouterr().out


def test_cmd_attach_uses_interactive_choice_when_name_missing(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_choose_attach_session_interactive", lambda: "filter2")
    monkeypatch.setattr(cli, "_ensure_clipboard_for_attach", lambda: [])
    monkeypatch.setattr(cli, "_clipboard_attach_hints", lambda: [])
    monkeypatch.setattr(cli.tmux, "has_session", lambda name: name == "filter2")

    called: dict[str, str | None | bool] = {
        "focus": None,
        "attach": None,
        "recent": None,
        "cc": False,
    }

    monkeypatch.setattr(cli.config, "set_focus", lambda name: called.__setitem__("focus", name))
    monkeypatch.setattr(
        cli.config,
        "touch_recent_attach",
        lambda name: called.__setitem__("recent", name),
    )
    monkeypatch.setattr(
        cli.tmux,
        "attach",
        lambda name, control_mode=False: (
            called.__setitem__("attach", name),
            called.__setitem__("cc", control_mode),
        ),
    )

    rc = cli.cmd_attach(argparse.Namespace(name=None))

    assert rc == 0
    assert called["focus"] == "filter2"
    assert called["recent"] == "filter2"
    assert called["attach"] == "filter2"
    assert called["cc"] is False


def test_cmd_attach_starts_mapped_session_when_not_running(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_choose_attach_session_interactive", lambda: "gig guide")
    monkeypatch.setattr(cli, "_ensure_clipboard_for_attach", lambda: [])
    monkeypatch.setattr(cli, "_clipboard_attach_hints", lambda: [])
    monkeypatch.setattr(
        cli.config, "get_mapping", lambda name: "/tmp/gig" if name == "gig guide" else ""
    )
    started = {"value": False}

    def _has_session(_name: str) -> bool:
        return started["value"]

    def _cmd_new(_args: argparse.Namespace) -> int:
        started["value"] = True
        return 0

    monkeypatch.setattr(cli.tmux, "has_session", _has_session)
    monkeypatch.setattr(cli, "cmd_new", _cmd_new)

    called: dict[str, str | None | bool] = {
        "focus": None,
        "attach": None,
        "recent": None,
        "cc": False,
    }
    monkeypatch.setattr(cli.config, "set_focus", lambda name: called.__setitem__("focus", name))
    monkeypatch.setattr(
        cli.config,
        "touch_recent_attach",
        lambda name: called.__setitem__("recent", name),
    )
    monkeypatch.setattr(
        cli.tmux,
        "attach",
        lambda name, control_mode=False: (
            called.__setitem__("attach", name),
            called.__setitem__("cc", control_mode),
        ),
    )

    rc = cli.cmd_attach(argparse.Namespace(name=None))

    assert rc == 0
    assert started["value"] is True
    assert called["focus"] == "gig guide"
    assert called["recent"] == "gig guide"
    assert called["attach"] == "gig guide"
    assert called["cc"] is False


def test_cmd_attach_passes_cc_flag_to_tmux(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_ensure_clipboard_for_attach", lambda: [])
    monkeypatch.setattr(cli, "_clipboard_attach_hints", lambda: [])
    monkeypatch.setattr(cli.tmux, "has_session", lambda name: name == "filter2")

    called: dict[str, str | None | bool] = {
        "focus": None,
        "attach": None,
        "recent": None,
        "cc": False,
    }
    monkeypatch.setattr(cli.config, "set_focus", lambda name: called.__setitem__("focus", name))
    monkeypatch.setattr(
        cli.config,
        "touch_recent_attach",
        lambda name: called.__setitem__("recent", name),
    )
    monkeypatch.setattr(
        cli.tmux,
        "attach",
        lambda name, control_mode=False: (
            called.__setitem__("attach", name),
            called.__setitem__("cc", control_mode),
        ),
    )

    rc = cli.cmd_attach(argparse.Namespace(name="filter2", cc=True))

    assert rc == 0
    assert called["focus"] == "filter2"
    assert called["recent"] == "filter2"
    assert called["attach"] == "filter2"
    assert called["cc"] is True


def test_cmd_attach_prints_setup_hint_for_ssh_when_clipboard_unconfigured(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli.tmux, "has_session", lambda name: name == "filter2")
    monkeypatch.setattr(cli.config, "set_focus", lambda _name: None)
    monkeypatch.setattr(cli.config, "touch_recent_attach", lambda _name: None)
    monkeypatch.setattr(cli.tmux, "attach", lambda _name, control_mode=False: None)
    monkeypatch.setattr(cli.clipboard, "setup", lambda **_kwargs: {})
    monkeypatch.setattr(
        cli.clipboard,
        "status",
        lambda tmux_socket=None: {
            "configured_on_disk": False,
            "selected_mode": "",
            "loaded_in_tmux": None,
            "helper_health": None,
            "tmux_socket_ambiguous": False,
            "verification": {
                "emission_verified": False,
                "clipboard_verified": False,
                "verified_at": 0,
            },
            "reasons": ["not_fully_configured_on_disk"],
        },
    )
    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")

    rc = cli.cmd_attach(argparse.Namespace(name="filter2", cc=False))

    assert rc == 0
    out = capsys.readouterr().out
    assert "oc clipboard setup --mode auto --reload" in out
    assert "oc clipboard verify" in out
    assert "Option-drag" in out
    assert "Ctrl-b` `[`" in out


def test_cmd_attach_prints_reload_hint_when_clipboard_not_loaded(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.tmux, "has_session", lambda name: name == "filter2")
    monkeypatch.setattr(cli.config, "set_focus", lambda _name: None)
    monkeypatch.setattr(cli.config, "touch_recent_attach", lambda _name: None)
    monkeypatch.setattr(cli.tmux, "attach", lambda _name, control_mode=False: None)
    monkeypatch.setattr(cli.clipboard, "setup", lambda **_kwargs: {})
    monkeypatch.setattr(
        cli.clipboard,
        "status",
        lambda tmux_socket=None: {
            "configured_on_disk": True,
            "selected_mode": "osc52",
            "loaded_in_tmux": False,
            "helper_health": True,
            "tmux_socket_ambiguous": False,
            "verification": {
                "emission_verified": False,
                "clipboard_verified": False,
                "verified_at": 0,
            },
            "reasons": ["tmux_not_loaded"],
        },
    )

    rc = cli.cmd_attach(argparse.Namespace(name="filter2", cc=False))

    assert rc == 0
    out = capsys.readouterr().out
    assert "not loaded in tmux" in out
    assert "tmux source-file ~/.tmux.conf" in out


def test_cmd_attach_prints_terminal_fix_hint_when_osc52_emits_but_paste_fails(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli.tmux, "has_session", lambda name: name == "filter2")
    monkeypatch.setattr(cli.config, "set_focus", lambda _name: None)
    monkeypatch.setattr(cli.config, "touch_recent_attach", lambda _name: None)
    monkeypatch.setattr(cli.tmux, "attach", lambda _name, control_mode=False: None)
    monkeypatch.setattr(
        cli.clipboard,
        "status",
        lambda tmux_socket=None: {
            "configured_on_disk": True,
            "selected_mode": "osc52",
            "mouse_mode": "terminal",
            "loaded_in_tmux": True,
            "helper_health": True,
            "tmux_socket_ambiguous": False,
            "verification": {
                "emission_verified": True,
                "clipboard_verified": False,
                "verified_at": 123,
            },
            "reasons": [],
        },
    )
    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")

    rc = cli.cmd_attach(argparse.Namespace(name="filter2", cc=False))

    assert rc == 0
    out = capsys.readouterr().out
    assert "enable OSC52 clipboard access in your terminal" in out
    assert "Option-drag" in out
    assert "Ctrl-b` `[`" in out


def test_cmd_attach_stays_quiet_when_clipboard_looks_healthy(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.tmux, "has_session", lambda name: name == "filter2")
    monkeypatch.setattr(cli.config, "set_focus", lambda _name: None)
    monkeypatch.setattr(cli.config, "touch_recent_attach", lambda _name: None)
    monkeypatch.setattr(cli.tmux, "attach", lambda _name, control_mode=False: None)
    monkeypatch.setattr(
        cli.clipboard,
        "status",
        lambda tmux_socket=None: {
            "configured_on_disk": True,
            "selected_mode": "osc52",
            "mouse_mode": "terminal",
            "loaded_in_tmux": True,
            "helper_health": True,
            "tmux_socket_ambiguous": False,
            "verification": {
                "emission_verified": True,
                "clipboard_verified": True,
                "verified_at": 123,
            },
            "reasons": [],
        },
    )

    rc = cli.cmd_attach(argparse.Namespace(name="filter2", cc=False))

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_attach_auto_setup_repairs_terminal_mouse_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.clipboard,
        "status",
        lambda tmux_socket=None: {
            "configured_on_disk": True,
            "selected_mode": "native",
            "mouse_mode": "terminal",
            "loaded_in_tmux": True,
            "helper_health": None,
            "tmux_socket_ambiguous": False,
            "verification": {
                "emission_verified": False,
                "clipboard_verified": False,
                "verified_at": 0,
            },
            "reasons": [],
        },
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli.clipboard, "setup", lambda **kwargs: calls.append(kwargs) or {})

    warnings = cli._ensure_clipboard_for_attach()

    assert warnings == []
    assert calls == [
        {
            "mode": "auto",
            "tmux_conf": None,
            "tmux_socket": None,
            "dry_run": False,
            "print_snippet": False,
            "reload_tmux": True,
            "bind_keys": "copy-mode-y",
            "follow_symlink": False,
                "mouse_mode": "tmux",
        }
    ]


def test_attach_menu_sorts_recent_sessions_first(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.config,
        "load_mappings",
        lambda: {
            "alpha": "/tmp/alpha",
            "beta": "/tmp/beta",
            "charlie": "/tmp/charlie",
        },
    )
    monkeypatch.setattr(cli.tmux, "list_sessions_with_paths", lambda: [])
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(cli.config, "get_recent_attaches", lambda: ["charlie", "alpha"])

    rows = cli._build_attach_menu_rows()

    ordered_names = [str(row["name"]) for row in rows if not row["exit"]]
    assert ordered_names == ["charlie", "alpha", "beta"]


def test_attach_menu_contains_exit_option(monkeypatch) -> None:
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"filter2": "/tmp/filter2"})
    monkeypatch.setattr(cli.tmux, "list_sessions_with_paths", lambda: [])
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(cli.config, "get_recent_attaches", lambda: [])

    rows = cli._build_attach_menu_rows()

    assert rows[-1]["name"] == "Exit"
    assert rows[-1]["exit"] is True


def test_fit_text_truncates_with_ellipsis() -> None:
    assert cli._fit_text("abcdefghijklmnopqrstuvwxyz", 10) == "abcdefg..."
    assert cli._fit_text("short", 10) == "short"


def test_compact_path_shortens_long_path(monkeypatch) -> None:
    monkeypatch.setattr(cli.Path, "home", lambda: Path("/home/testuser"))
    out = cli._compact_path("/home/testuser/projects/audience/prs/EC-3620/server")
    assert out.endswith("/prs/EC-3620/server")
    assert out.startswith(".../")


def test_menu_row_handles_ansi_without_border_shift() -> None:
    row = cli._menu_row("state \033[32mRUNNING\033[0m", 20)
    assert row.startswith("│")
    assert row.endswith("│")
    visible = cli.ANSI_RE.sub("", row)
    assert len(visible) == 22


def test_version_string_appears_in_version_constant() -> None:
    assert cli._VERSION == "0.8.0"


def test_read_menu_key_recognizes_csi_arrow_sequences() -> None:
    r, w = os.pipe()
    os.write(w, b"\x1b[A\x1b[B")
    os.close(w)
    assert cli._read_menu_key(r) == "up"
    assert cli._read_menu_key(r) == "down"
    os.close(r)


def test_read_menu_key_recognizes_ss3_arrow_sequences() -> None:
    r, w = os.pipe()
    os.write(w, b"\x1bOA\x1bOB")
    os.close(w)
    assert cli._read_menu_key(r) == "up"
    assert cli._read_menu_key(r) == "down"
    os.close(r)


def test_read_menu_key_recognizes_mailbox_mode() -> None:
    r, w = os.pipe()
    os.write(w, b"m")
    os.close(w)
    assert cli._read_menu_key(r) == "mailbox"
    os.close(r)


def test_read_menu_key_recognizes_kill_window() -> None:
    r, w = os.pipe()
    os.write(w, b"x")
    os.close(w)
    assert cli._read_menu_key(r) == "kill-window"
    os.close(r)


def test_match_wait_pattern_detects_prompt() -> None:
    pane = "Agent paused\n? Continue (y/n):"
    assert cli._match_wait_pattern(pane.lower()) == r"\bcontinue\b"


def test_match_stall_pattern_detects_planner_spinner() -> None:
    pane = "Thinking: Planning research content loading\n◆ Spawning planner..."
    assert cli._match_stall_pattern(pane.lower()) == r"thinking:\s+planning"


def test_session_context_falls_back_to_focus_mapping(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.config, "get_mapping", lambda name: {"filter2": "/tmp/filter2"}.get(name)
    )
    monkeypatch.setattr(cli.config, "get_focus", lambda: "filter2")
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "studio.home")

    project, host = cli._session_context("oc-final-test")

    assert project == "/tmp/filter2 (focus:filter2)"
    assert host == "studio.home"


def test_cmd_watch_triggers_pattern_alert_first(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.config, "get_focus", lambda: "infra")
    monkeypatch.setattr(cli.tmux, "has_session", lambda _: True)
    monkeypatch.setattr(cli.tmux, "capture_last_lines", lambda *_args, **_kwargs: "Press Enter")

    def _pane_last_activity(*_args, **_kwargs):
        raise AssertionError("idle fallback should not run when prompt pattern matches")

    monkeypatch.setattr(cli.tmux, "pane_last_activity", _pane_last_activity)

    calls: dict[str, str | None] = {
        "title": None,
        "body": None,
        "discord": None,
        "router_service": None,
        "router_message": None,
    }

    def _mac_notify(title: str, body: str) -> None:
        calls["title"] = title
        calls["body"] = body

    def _discord_webhook(_url: str, content: str) -> None:
        calls["discord"] = content

    def _alert_router_webhook(_url: str, **kwargs) -> None:
        calls["router_service"] = kwargs.get("service_name")
        calls["router_message"] = kwargs.get("message")

    monkeypatch.setattr(cli, "mac_notify", _mac_notify)
    monkeypatch.setattr(cli, "discord_webhook", _discord_webhook)
    monkeypatch.setattr(cli, "alert_router_webhook", _alert_router_webhook)
    monkeypatch.setattr(cli.config, "get_webhook", lambda: "https://example.invalid/webhook")
    monkeypatch.setattr(
        cli.config, "get_alert_router", lambda: "http://n100alerts:3000/webhook/infra"
    )
    monkeypatch.setattr(cli.config, "get_mapping", lambda _: "/tmp/infra")

    rc = cli.cmd_watch(argparse.Namespace(name=None, idle_seconds=90, capture_lines=120))

    assert rc == 0
    assert calls["title"] == "OpenCode awaiting input"
    assert calls["body"] is not None
    assert "AI agent waiting for input" in calls["body"]
    assert "session=infra" in calls["body"]
    assert "project=/tmp/infra" in calls["body"]
    assert "prompt pattern 'press enter' matched" in calls["body"]
    assert "snippet=Press Enter" in calls["body"]
    assert calls["discord"] is not None
    assert "OpenCode awaiting input" in calls["discord"]
    assert calls["router_service"] == "oc-watch:infra"
    assert calls["router_message"] is not None
    assert "session=infra" in calls["router_message"]
    assert "pattern=press enter" in capsys.readouterr().out


def test_cmd_watch_triggers_stall_alert_when_idle(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.config, "get_focus", lambda: "infra")
    monkeypatch.setattr(cli.tmux, "has_session", lambda _: True)
    monkeypatch.setattr(
        cli.tmux,
        "capture_last_lines",
        lambda *_args, **_kwargs: (
            "Thinking: Planning research content loading\n◆ Spawning planner..."
        ),
    )
    monkeypatch.setattr(cli.tmux, "pane_last_activity", lambda *_args, **_kwargs: 100)
    monkeypatch.setattr(cli.time, "time", lambda: 220)

    calls: dict[str, str | None] = {
        "title": None,
        "body": None,
        "discord": None,
        "router_service": None,
        "router_message": None,
    }

    def _mac_notify(title: str, body: str) -> None:
        calls["title"] = title
        calls["body"] = body

    def _discord_webhook(_url: str, content: str) -> None:
        calls["discord"] = content

    def _alert_router_webhook(_url: str, **kwargs) -> None:
        calls["router_service"] = kwargs.get("service_name")
        calls["router_message"] = kwargs.get("message")

    monkeypatch.setattr(cli, "mac_notify", _mac_notify)
    monkeypatch.setattr(cli, "discord_webhook", _discord_webhook)
    monkeypatch.setattr(cli, "alert_router_webhook", _alert_router_webhook)
    monkeypatch.setattr(cli.config, "get_webhook", lambda: "https://example.invalid/webhook")
    monkeypatch.setattr(
        cli.config, "get_alert_router", lambda: "http://n100alerts:3000/webhook/infra"
    )
    monkeypatch.setattr(cli.config, "get_mapping", lambda _: "/tmp/infra")

    rc = cli.cmd_watch(argparse.Namespace(name=None, idle_seconds=90, capture_lines=120))

    assert rc == 0
    assert calls["title"] == "OpenCode stalled?"
    assert calls["body"] is not None
    assert "AI agent appears stalled" in calls["body"]
    assert "session=infra" in calls["body"]
    assert "project=/tmp/infra" in calls["body"]
    assert "stall pattern 'thinking:\\s+planning' matched" in calls["body"]
    assert "snippet=Thinking: Planning research content loading" in calls["body"]
    assert calls["discord"] is not None
    assert "OpenCode stalled?" in calls["discord"]
    assert calls["router_service"] == "oc-watch:infra"
    assert calls["router_message"] is not None
    assert "session=infra" in calls["router_message"]
    assert "stall_pattern=" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _build_attach_menu_rows — multi-instance support
# ---------------------------------------------------------------------------


def _make_session(name: str, path: str, attached: bool = False, windows: int = 1) -> dict:
    return {"name": name, "attached": attached, "windows": windows, "path": path}


def test_build_attach_menu_rows_groups_sessions_by_mapped_path(monkeypatch, tmp_path: Path) -> None:
    """Multiple sessions sharing a mapped path produce a single collapsed group row."""
    proj = str(tmp_path / "myproject")
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"myproject": proj})
    monkeypatch.setattr(cli.config, "get_recent_attaches", lambda: [])
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(
        cli.tmux,
        "list_sessions_with_paths",
        lambda: [
            _make_session("myproject", proj),
            _make_session("myproject-worker", proj),
        ],
    )

    rows = cli._build_attach_menu_rows()
    data = [r for r in rows if not r["exit"]]

    # One group row (collapsed) with 2 children
    assert len(data) == 1
    assert data[0]["row_type"] == "group"
    assert data[0]["name"] == "myproject"
    assert data[0]["expanded"] is False
    children = data[0]["children"]
    assert len(children) == 2
    assert {c["name"] for c in children} == {"myproject", "myproject-worker"}


def test_build_attach_menu_rows_group_expands_to_child_rows(monkeypatch, tmp_path: Path) -> None:
    """Passing the mapping name in expanded set adds child rows after the group."""
    proj = str(tmp_path / "myproject")
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"myproject": proj})
    monkeypatch.setattr(cli.config, "get_recent_attaches", lambda: [])
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(
        cli.tmux,
        "list_sessions_with_paths",
        lambda: [
            _make_session("myproject", proj),
            _make_session("myproject-worker", proj),
        ],
    )

    rows = cli._build_attach_menu_rows(expanded={"myproject"})
    data = [r for r in rows if not r["exit"]]

    # Group row + 2 child rows
    assert len(data) == 3
    assert data[0]["row_type"] == "group"
    assert data[0]["expanded"] is True
    assert data[1]["row_type"] == "child"
    assert data[2]["row_type"] == "child"
    child_names = {data[1]["name"], data[2]["name"]}
    assert child_names == {"myproject", "myproject-worker"}


def test_build_attach_menu_rows_shows_mailbox_roles(monkeypatch, tmp_path: Path) -> None:
    proj_path = tmp_path / "myproject"
    mailbox_dir = proj_path / ".rig-mailbox"
    mailbox_dir.mkdir(parents=True)
    (mailbox_dir / "rigs.toml").write_text(
        "\n".join(
            [
                '[rigs."rig-a"]',
                'runtime = "claude-code"',
                'notifier = "tmux"',
                'tmux_target = "myproject:main"',
                f'workspace = "{proj_path}"',
                "",
                '[rigs."rig-b"]',
                'runtime = "codex"',
                'notifier = "tmux"',
                'tmux_target = "myproject-worker:main"',
                f'workspace = "{proj_path}"',
            ]
        ),
        encoding="utf-8",
    )
    proj = str(proj_path)
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"myproject": proj})
    monkeypatch.setattr(cli.config, "get_recent_attaches", lambda: [])
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(
        cli.tmux,
        "list_sessions_with_paths",
        lambda: [
            _make_session("myproject", proj),
            _make_session("myproject-worker", proj),
        ],
    )
    monkeypatch.setattr(
        cli.tmux,
        "list_window_details",
        lambda: {
            "myproject": {
                "active_window": "shell",
                "active_command": "zsh",
                "main_command": "opencode",
                "window_list": [
                    {"name": "main", "active": False, "command": "opencode"},
                    {"name": "shell", "active": True, "command": "zsh"},
                ],
            },
            "myproject-worker": {
                "active_window": "main",
                "active_command": "codex",
                "main_command": "codex",
                "window_list": [
                    {"name": "main", "active": True, "command": "codex"},
                ],
            },
        },
    )

    rows = cli._build_attach_menu_rows(expanded={"myproject"})
    roles = {str(row["name"]): row["mailbox_role"] for row in rows if not row["exit"]}
    details = {str(row["name"]): row for row in rows if not row["exit"]}

    assert roles["myproject"] == "Rig A"
    assert roles["myproject-worker"] == "Rig B"
    assert details["myproject"]["main_command"] == "opencode"
    assert details["myproject"]["active_window"] == "shell"
    assert details["myproject-worker"]["active_window"] == "main"


def test_build_attach_menu_rows_expands_windows_for_single_session(
    monkeypatch, tmp_path: Path
) -> None:
    proj = str(tmp_path / "myproject")
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"myproject": proj})
    monkeypatch.setattr(cli.config, "get_recent_attaches", lambda: [])
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(
        cli.tmux,
        "list_sessions_with_paths",
        lambda: [_make_session("myproject", proj, windows=2)],
    )
    monkeypatch.setattr(
        cli.tmux,
        "list_window_details",
        lambda: {
            "myproject": {
                "active_window": "shell",
                "active_command": "claude",
                "main_command": "opencode",
                "window_list": [
                    {"name": "main", "active": False, "command": "opencode"},
                    {"name": "shell", "active": True, "command": "claude"},
                ],
            }
        },
    )

    rows = cli._build_attach_menu_rows(expanded_sessions={"myproject"})
    data = [r for r in rows if not r["exit"]]

    assert [r["row_type"] for r in data] == ["leaf", "window", "window"]
    assert data[1]["name"] == "myproject:main"
    assert data[1]["command"] == "opencode"
    assert data[2]["name"] == "myproject:shell"
    assert data[2]["window_active"] is True


def test_build_attach_menu_rows_single_session_one_row(monkeypatch, tmp_path: Path) -> None:
    """A mapping with one running session produces exactly one row."""
    proj = str(tmp_path / "myproject")
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"myproject": proj})
    monkeypatch.setattr(cli.config, "get_recent_attaches", lambda: [])
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(
        cli.tmux,
        "list_sessions_with_paths",
        lambda: [_make_session("myproject", proj)],
    )

    rows = cli._build_attach_menu_rows()
    data = [r for r in rows if not r["exit"]]

    assert len(data) == 1
    assert data[0]["name"] == "myproject"
    assert data[0]["running"] is True


def test_build_attach_menu_rows_unmapped_session_appears_standalone(
    monkeypatch, tmp_path: Path
) -> None:
    """A running session whose path doesn't match any mapping appears as its own row."""
    proj = str(tmp_path / "myproject")
    other = str(tmp_path / "other")
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"myproject": proj})
    monkeypatch.setattr(cli.config, "get_recent_attaches", lambda: [])
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(
        cli.tmux,
        "list_sessions_with_paths",
        lambda: [
            _make_session("myproject", proj),
            _make_session("orphan", other),
        ],
    )

    rows = cli._build_attach_menu_rows()
    data = [r for r in rows if not r["exit"]]

    names = [r["name"] for r in data]
    assert "myproject" in names
    assert "orphan" in names
    assert len(data) == 2


def test_build_attach_menu_rows_stopped_mapping_has_one_row(monkeypatch, tmp_path: Path) -> None:
    """A mapping with no running sessions produces a single stopped row."""
    proj = str(tmp_path / "myproject")
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"myproject": proj})
    monkeypatch.setattr(cli.config, "get_recent_attaches", lambda: [])
    monkeypatch.setattr(cli.config, "get_focus", lambda: "")
    monkeypatch.setattr(cli.tmux, "list_sessions_with_paths", lambda: [])

    rows = cli._build_attach_menu_rows()
    data = [r for r in rows if not r["exit"]]

    assert len(data) == 1
    assert data[0]["name"] == "myproject"
    assert data[0]["running"] is False


def test_auto_link_two_session_mailboxes_links_exactly_two_sessions(
    monkeypatch, tmp_path: Path
) -> None:
    proj_path = tmp_path / "zoom-mvps"
    proj_path.mkdir(parents=True)
    proj = str(proj_path)
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"zoom-mvps": proj})
    monkeypatch.setattr(
        cli.tmux,
        "list_sessions_with_paths",
        lambda: [
            _make_session("zoom-mvps-a", proj),
            _make_session("zoom-mvps-b", proj),
        ],
    )
    ensured: list[str] = []

    def fake_ensure_mailbox(workspace: str) -> Path:
        ensured.append(workspace)
        mailbox_dir = Path(workspace) / ".rig-mailbox"
        mailbox_dir.mkdir(parents=True)
        return mailbox_dir

    monkeypatch.setattr(cli.mailbox, "ensure_mailbox", fake_ensure_mailbox)
    env_calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        cli.tmux,
        "set_session_environment",
        lambda session, values: env_calls.append((session, values)),
    )

    cli._auto_link_two_session_mailboxes()

    assert ensured == [proj]
    assert env_calls == [
        ("zoom-mvps-a", {"RIG_NAME": "Rig A", "RIG_WORKSPACE": str(proj_path.resolve())}),
        ("zoom-mvps-b", {"RIG_NAME": "Rig B", "RIG_WORKSPACE": str(proj_path.resolve())}),
    ]
    text = (proj_path / ".rig-mailbox" / "rigs.toml").read_text(encoding="utf-8")
    assert 'tmux_target = "zoom-mvps-a:main"' in text
    assert 'tmux_target = "zoom-mvps-b:main"' in text


def test_auto_link_preserves_existing_rig_assignment_for_recreated_pair(
    monkeypatch, tmp_path: Path
) -> None:
    proj_path = tmp_path / "zoom-rag-mvp"
    mailbox_dir = proj_path / ".rig-mailbox"
    mailbox_dir.mkdir(parents=True)
    (mailbox_dir / "rigs.toml").write_text(
        "\n".join(
            [
                '[rigs."rig-b"]',
                'runtime = "codex"',
                'notifier = "tmux"',
                'tmux_target = "zoom rag 2:main"',
                f'workspace = "{proj_path}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    proj = str(proj_path)
    monkeypatch.setattr(cli.config, "load_mappings", lambda: {"zoom rag": proj})
    monkeypatch.setattr(
        cli.tmux,
        "list_sessions_with_paths",
        lambda: [
            _make_session("zoom rag 2", proj),
            _make_session("zoom rag 3", proj),
        ],
    )
    monkeypatch.setattr(cli.tmux, "list_window_details", lambda: {})
    env_calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        cli.tmux,
        "set_session_environment",
        lambda session, values: env_calls.append((session, values)),
    )

    cli._auto_link_two_session_mailboxes()

    text = (mailbox_dir / "rigs.toml").read_text(encoding="utf-8")
    assert 'tmux_target = "zoom rag 3:main"' in text
    assert 'tmux_target = "zoom rag 2:main"' in text
    assert env_calls == [
        ("zoom rag 2", {"RIG_NAME": "Rig B", "RIG_WORKSPACE": str(proj_path.resolve())}),
        ("zoom rag 3", {"RIG_NAME": "Rig A", "RIG_WORKSPACE": str(proj_path.resolve())}),
    ]


def test_manual_mailbox_link_requires_same_workspace(tmp_path: Path) -> None:
    left = str(tmp_path / "left")
    right = str(tmp_path / "right")
    rows = [
        {
            "name": "one",
            "running": True,
            "mapped_dir": left,
            "exit": False,
        },
        {
            "name": "two",
            "running": True,
            "mapped_dir": right,
            "exit": False,
        },
    ]

    message = cli._link_selected_mailbox_sessions(rows, ["one", "two"])

    assert message == "selected sessions must share one mapped mailbox workspace"


def test_manual_mailbox_link_writes_pair(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "cash-claw"
    workspace.mkdir(parents=True)
    rows = [
        {
            "name": "cash-claw-rig-a",
            "running": True,
            "mapped_dir": str(workspace),
            "exit": False,
        },
        {
            "name": "cash-claw-rig-b",
            "running": True,
            "mapped_dir": str(workspace),
            "exit": False,
        },
    ]

    def fake_ensure_mailbox(path: str) -> Path:
        mailbox_dir = Path(path) / ".rig-mailbox"
        mailbox_dir.mkdir(parents=True)
        return mailbox_dir

    monkeypatch.setattr(cli.mailbox, "ensure_mailbox", fake_ensure_mailbox)
    env_calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        cli.tmux,
        "set_session_environment",
        lambda session, values: env_calls.append((session, values)),
    )

    message = cli._link_selected_mailbox_sessions(rows, ["cash-claw-rig-a", "cash-claw-rig-b"])

    assert message == "linked mailbox: cash-claw-rig-a <-> cash-claw-rig-b"
    assert env_calls == [
        ("cash-claw-rig-a", {"RIG_NAME": "Rig A", "RIG_WORKSPACE": str(workspace.resolve())}),
        ("cash-claw-rig-b", {"RIG_NAME": "Rig B", "RIG_WORKSPACE": str(workspace.resolve())}),
    ]
    text = (workspace / ".rig-mailbox" / "rigs.toml").read_text(encoding="utf-8")
    assert 'tmux_target = "cash-claw-rig-a:main"' in text
    assert 'tmux_target = "cash-claw-rig-b:main"' in text
