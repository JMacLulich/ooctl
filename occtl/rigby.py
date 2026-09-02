from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class RigbyError(RuntimeError):
    """A public Rigby command or project contract failed."""


@dataclass(frozen=True)
class RigbyRole:
    key: str
    runtime: str
    tmux_target: str
    worktree: Path


@dataclass(frozen=True)
class RigbyProject:
    project_root: Path
    config_path: Path
    roles: tuple[RigbyRole, ...]

    def role(self, key: str) -> RigbyRole | None:
        wanted = key.strip().casefold()
        return next((role for role in self.roles if role.key.casefold() == wanted), None)


def _command_path(name: str) -> str | None:
    discovered = shutil.which(name)
    if discovered:
        return discovered

    installed = Path.home() / ".local" / "bin" / name
    return str(installed) if installed.is_file() else None


def _project_root(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve()


def _config_from_marker(project_root: Path) -> Path:
    marker = project_root / ".rigby-enabled"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        value = payload["config_path"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("config_path must be a non-empty string")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RigbyError(f"invalid Rigby marker: {marker}: {exc}") from exc

    config_path = Path(value).expanduser()
    return config_path if config_path.is_absolute() else (project_root / config_path).resolve()


def is_enabled(workspace: str | Path) -> bool:
    return (_project_root(workspace) / ".rigby-enabled").is_file()


def project(workspace: str | Path) -> RigbyProject | None:
    project_root = _project_root(workspace)
    marker = project_root / ".rigby-enabled"
    if not marker.is_file():
        return None
    return RigbyProject(
        project_root=project_root, config_path=_config_from_marker(project_root), roles=()
    )


def _run_public(
    command_name: str,
    args: list[str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    command = _command_path(command_name)
    if not command:
        raise RigbyError(f"{command_name} is unavailable; install Rigby and retry")
    try:
        return subprocess.run(
            [command, *args],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RigbyError(f"failed to run public Rigby command {command_name}: {exc}") from exc


def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stderr or result.stdout or "").strip()
    if not output:
        return "no diagnostic output"
    lines = output.splitlines()
    return " ".join(line.strip() for line in lines[-4:])


def attach_metadata(workspace: str | Path) -> RigbyProject | None:
    """Read authoritative role/session metadata through ``rigby attach-metadata``."""
    base = project(workspace)
    if base is None:
        return None

    result = _run_public(
        "rigby",
        ["attach-metadata", "--config", str(base.config_path)],
        cwd=base.project_root,
    )
    if result.returncode != 0:
        raise RigbyError(f"rigby attach-metadata failed: {_failure_detail(result)}")
    try:
        payload = json.loads(result.stdout)
        raw_roles = payload["roles"]
        if not isinstance(raw_roles, dict):
            raise ValueError("roles must be an object")
        roles: list[RigbyRole] = []
        for key, raw_role in raw_roles.items():
            if not isinstance(key, str) or not isinstance(raw_role, dict):
                raise ValueError("role metadata is malformed")
            runtime = raw_role["runtime"]
            tmux_target = raw_role["tmux_target"]
            worktree = raw_role["worktree"]
            if not all(
                isinstance(value, str) and value.strip()
                for value in (runtime, tmux_target, worktree)
            ):
                raise ValueError(f"role metadata is incomplete for {key}")
            roles.append(
                RigbyRole(
                    key=key,
                    runtime=runtime,
                    tmux_target=tmux_target,
                    worktree=Path(worktree).expanduser().resolve(),
                )
            )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RigbyError(f"invalid rigby attach metadata: {exc}") from exc

    return RigbyProject(
        project_root=base.project_root,
        config_path=base.config_path,
        roles=tuple(sorted(roles, key=lambda role: role.key)),
    )


def canonical_mailbox(workspace: str | Path) -> Path:
    """Resolve the mailbox selected by Rigby's public ``rig path`` command."""
    base = project(workspace)
    if base is None:
        raise RigbyError(f"not a Rigby project: {base.project_root if base else workspace}")
    result = _run_public("rig", ["path"], cwd=base.project_root)
    if result.returncode != 0:
        raise RigbyError(f"rig path failed: {_failure_detail(result)}")
    values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not values:
        raise RigbyError("rig path returned no mailbox path")
    mailbox_path = Path(values[-1]).expanduser().resolve()
    if not mailbox_path.is_dir():
        raise RigbyError(f"rig path returned a missing mailbox: {mailbox_path}")
    return mailbox_path


def reload_role(workspace: str | Path, role: str) -> dict[str, object]:
    """Reload one idle lane through Rigby's public custody-safe operation."""
    base = project(workspace)
    if base is None:
        raise RigbyError(f"not a Rigby project: {workspace}")
    result = _run_public(
        "rigby",
        [
            "interactive",
            "reload",
            "--config",
            str(base.config_path),
            "--role",
            role,
            "--apply",
            "--json",
        ],
        cwd=base.project_root,
    )
    if result.returncode != 0:
        raise RigbyError(f"rigby interactive reload failed: {_failure_detail(result)}")
    try:
        payload = json.loads(result.stdout)
        role_report = payload["roles"][role]
        status = payload["status"]
        if not isinstance(role_report, dict) or not isinstance(status, str):
            raise ValueError("reload report has invalid fields")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RigbyError(f"invalid rigby reload report: {exc}") from exc
    if status != "RELOADED" or role_report.get("action") != "RELOADED":
        reason = str(role_report.get("reason") or role_report.get("status") or status)
        raise RigbyError(f"Rigby protected {role} from restart: {reason}")
    return payload


def reconcile(workspace: str | Path) -> RigbyProject:
    """Create/reconcile Rigby sessions and mailbox through the canonical launcher."""
    base = project(workspace)
    if base is None:
        raise RigbyError(f"not a Rigby project: {workspace}")
    result = _run_public(
        "launch-rigby-iterm",
        ["--project-root", str(base.project_root), "--no-iterm"],
        cwd=base.project_root,
    )
    if result.returncode != 0:
        raise RigbyError(f"Rigby reconciliation failed: {_failure_detail(result)}")
    metadata = attach_metadata(base.project_root)
    if metadata is None:
        raise RigbyError("Rigby marker disappeared during reconciliation")
    return metadata
