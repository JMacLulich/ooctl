from __future__ import annotations

import argparse
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
    monkeypatch.setattr(cli.config, "get_focus", lambda: "filter2")
    monkeypatch.setattr(cli.config, "get_webhook", lambda: "")
    monkeypatch.setattr(
        cli.config, "get_alert_router", lambda: "http://n100alerts:3000/webhook/infra"
    )
    monkeypatch.setattr(cli.config, "get_relay_token", lambda: "token")
    monkeypatch.setattr(
        cli.config,
        "load_mappings",
        lambda: {"filter2": "/Users/jasonmaclulich/dev/audience/prs/EC-3620/server"},
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
