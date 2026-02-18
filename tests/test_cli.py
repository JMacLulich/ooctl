from __future__ import annotations

from occtl.cli import _bash_completion_script, _fish_completion_script, _zsh_completion_script


def test_cli_completion_bash_contains_commands() -> None:
    script = _bash_completion_script()
    assert "_occtl_complete" in script
    assert "complete -F _occtl_complete oc" in script


def test_cli_completion_zsh_contains_compdef() -> None:
    script = _zsh_completion_script()
    assert "#compdef oc" in script
    assert "compdef _occtl oc" in script


def test_cli_completion_fish_contains_command() -> None:
    script = _fish_completion_script()
    assert "complete -c oc -f" in script
