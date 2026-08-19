from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from occtl import cli, config, rigby


def _marker(project_root: Path, config_path: Path) -> None:
    (project_root / ".rigby-enabled").write_text(
        json.dumps({"schema_version": 2, "config_path": str(config_path)}),
        encoding="utf-8",
    )


def _metadata(project_root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "lullafi-123",
        "roles": {
            "rig-a": {
                "runtime": "claude",
                "tmux_target": "rigby-lullafi-rig-a:0",
                "worktree": str(project_root / "worktrees" / "rig-a"),
            },
            "rig-b": {
                "runtime": "codex",
                "tmux_target": "rigby-lullafi-rig-b:0",
                "worktree": str(project_root / "worktrees" / "rig-b"),
            },
            "rig-c": {
                "runtime": "opencode",
                "tmux_target": "rigby-lullafi-rig-c:0",
                "worktree": str(project_root / "worktrees" / "rig-c"),
            },
        },
    }


def test_attach_metadata_uses_public_rigby_command_and_typed_roles(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "state" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}", encoding="utf-8")
    _marker(tmp_path, config_path)
    calls: list[tuple[list[str], str]] = []

    def fake_run(argv, *, cwd, check, capture_output, text):
        calls.append((argv, cwd))
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(_metadata(tmp_path)), stderr=""
        )

    monkeypatch.setattr(rigby, "_command_path", lambda name: f"/bin/{name}")
    monkeypatch.setattr(rigby.subprocess, "run", fake_run)

    project = rigby.attach_metadata(tmp_path)

    assert project is not None
    assert project.role("rig-c") is not None
    assert project.role("rig-c").runtime == "opencode"
    assert calls == [
        (
            ["/bin/rigby", "attach-metadata", "--config", str(config_path)],
            str(tmp_path.resolve()),
        )
    ]


def test_canonical_mailbox_uses_public_rig_path(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "state" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}", encoding="utf-8")
    _marker(tmp_path, config_path)
    mailbox_path = tmp_path.parent / "lullafi-rig-mailbox"
    mailbox_path.mkdir()
    calls: list[list[str]] = []

    def fake_run(argv, *, cwd, check, capture_output, text):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=f"{mailbox_path}\n", stderr="")

    monkeypatch.setattr(rigby, "_command_path", lambda name: f"/bin/{name}")
    monkeypatch.setattr(rigby.subprocess, "run", fake_run)

    assert rigby.canonical_mailbox(tmp_path) == mailbox_path.resolve()
    assert calls == [["/bin/rig", "path"]]


def test_session_inventory_uses_rigby_metadata_instead_of_shadow_mailbox(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "state" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}", encoding="utf-8")
    _marker(tmp_path, config_path)
    project = rigby.RigbyProject(
        project_root=tmp_path.resolve(),
        config_path=config_path.resolve(),
        roles=tuple(
            rigby.RigbyRole(
                key=key,
                runtime=role["runtime"],
                tmux_target=role["tmux_target"],
                worktree=Path(role["worktree"]),
            )
            for key, role in _metadata(tmp_path)["roles"].items()
        ),
    )
    monkeypatch.setattr(config, "load_mappings", lambda: {"lullafi": str(tmp_path)})
    monkeypatch.setattr(cli.rigby, "attach_metadata", lambda _path: project)
    monkeypatch.setattr(
        cli.mailbox,
        "linked_targets",
        lambda _path: (_ for _ in ()).throw(AssertionError("shadow mailbox read")),
    )
    monkeypatch.setattr(
        cli.tmux,
        "list_sessions_with_paths",
        lambda: [
            {
                "name": "rigby-lullafi-rig-a",
                "attached": False,
                "windows": 1,
                "path": str(tmp_path / "worktrees" / "rig-a"),
            },
            {
                "name": "rigby-lullafi-rig-b",
                "attached": False,
                "windows": 1,
                "path": str(tmp_path / "worktrees" / "rig-b"),
            },
            {
                "name": "rigby-lullafi-rig-c",
                "attached": False,
                "windows": 1,
                "path": str(tmp_path / "worktrees" / "rig-c"),
            },
        ],
    )

    inventory = cli._session_inventory()

    assert {row["role"] for row in inventory} == {"Rig A", "Rig B", "Rig C"}
    assert {row["mailbox_target"] for row in inventory} == {
        "rigby-lullafi-rig-a:0",
        "rigby-lullafi-rig-b:0",
        "rigby-lullafi-rig-c:0",
    }


def test_cmd_new_reconciles_rigby_project_without_plain_tmux_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "state" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}", encoding="utf-8")
    _marker(tmp_path, config_path)
    monkeypatch.setattr(config, "get_mapping", lambda _name: str(tmp_path))
    monkeypatch.setattr(cli.rigby, "is_enabled", lambda _path: True)
    monkeypatch.setattr(
        cli.rigby,
        "reconcile",
        lambda _path: rigby.RigbyProject(
            project_root=tmp_path,
            config_path=config_path,
            roles=(
                rigby.RigbyRole(
                    key="rig-a",
                    runtime="claude",
                    tmux_target="rigby-lullafi-rig-a:0",
                    worktree=tmp_path / "worktrees" / "rig-a",
                ),
            ),
        ),
    )
    monkeypatch.setattr(config, "set_focus", lambda value: setattr(args, "focused", value))
    monkeypatch.setattr(
        cli,
        "_create_project_session",
        lambda *_args: (_ for _ in ()).throw(AssertionError("plain session created")),
    )
    args = argparse.Namespace(name="lullafi", focused=None)

    assert cli.cmd_new(args) == 0
    assert args.focused == "rigby-lullafi-rig-a"
    assert "reconciled+focused: lullafi" in capsys.readouterr().out
