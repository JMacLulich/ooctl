from __future__ import annotations

from occtl.voice import parse_voice


def test_parse_voice_status() -> None:
    intent = parse_voice("status")
    assert intent.action == "status"
    assert intent.session is None
    assert intent.text is None


def test_parse_voice_switch_to_session() -> None:
    intent = parse_voice("switch to infra")
    assert intent.action == "focus"
    assert intent.session == "infra"


def test_parse_voice_switch_to_spaced_session() -> None:
    intent = parse_voice("switch to gig guide")
    assert intent.action == "focus"
    assert intent.session == "gig guide"


def test_parse_voice_start_session() -> None:
    intent = parse_voice("start gig guide")
    assert intent.action == "new"
    assert intent.session == "gig guide"


def test_parse_voice_tell_session() -> None:
    intent = parse_voice("tell infra run terraform plan")
    assert intent.action == "say"
    assert intent.session == "infra"
    assert intent.text == "run terraform plan"


def test_parse_voice_open_spaced_session() -> None:
    intent = parse_voice("open gig guide")
    assert intent.action == "attach_or_focus"
    assert intent.session == "gig guide"


def test_parse_voice_default_to_focused_session() -> None:
    intent = parse_voice("run tests")
    assert intent.action == "say"
    assert intent.session is None
    assert intent.text == "run tests"
