from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class MailboxError(RuntimeError):
    pass


def rig_key(label: str) -> str:
    key = "-".join(label.lower().split())
    if not key:
        raise MailboxError("rig label is required")
    return key


def tmux_target(session: str, window: str = "main") -> str:
    session = session.strip()
    window = window.strip()
    if not session:
        raise MailboxError("tmux session is required")
    if not window:
        raise MailboxError("tmux window is required")
    return f"{session}:{window}"


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _section_header(key: str) -> str:
    return f'[rigs."{key}"]'


def mailbox_exists(workspace: str | Path) -> bool:
    return (Path(workspace).expanduser().resolve() / ".rig-mailbox").exists()


def _installer_path() -> str | None:
    discovered = shutil.which("install-rig-mailbox")
    if discovered:
        return discovered

    candidate = Path.home() / "dev/system-playbooks/bin/install-rig-mailbox"
    if candidate.exists():
        return str(candidate)
    return None


def ensure_mailbox(workspace: str | Path) -> Path:
    workspace_path = Path(workspace).expanduser().resolve()
    mailbox_dir = workspace_path / ".rig-mailbox"
    if mailbox_dir.exists():
        return mailbox_dir

    installer = _installer_path()
    if not installer:
        raise MailboxError(
            f"mailbox not found and install-rig-mailbox is unavailable: {mailbox_dir}"
        )

    try:
        subprocess.run(
            [installer, str(workspace_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        raise MailboxError(f"failed to create mailbox at {mailbox_dir}{suffix}") from e

    if not mailbox_dir.exists():
        raise MailboxError(f"installer did not create mailbox: {mailbox_dir}")
    return mailbox_dir


def _render_section(
    *,
    label: str,
    runtime: str,
    target: str,
    workspace: Path,
) -> list[str]:
    key = rig_key(label)
    return [
        _section_header(key),
        f'runtime = "{_quote(runtime)}"',
        'notifier = "tmux"',
        f'tmux_target = "{_quote(target)}"',
        f'workspace = "{_quote(str(workspace.expanduser().resolve()))}"',
    ]


def _replace_section(text: str, key: str, section: list[str]) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    replaced = False
    wanted = _section_header(key)

    while i < len(lines):
        if lines[i].strip() == wanted:
            if out and out[-1].strip():
                out.append("")
            out.extend(section)
            replaced = True
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("["):
                i += 1
            continue
        out.append(lines[i])
        i += 1

    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.extend(section)

    return "\n".join(out).rstrip() + "\n"


def link_rig(
    *,
    workspace: str | Path,
    label: str,
    session: str,
    runtime: str,
    window: str = "main",
) -> Path:
    workspace_path = Path(workspace).expanduser().resolve()
    mailbox_dir = workspace_path / ".rig-mailbox"
    if not mailbox_dir.exists():
        raise MailboxError(f"mailbox not found: {mailbox_dir}")

    rigs_file = mailbox_dir / "rigs.toml"
    target = tmux_target(session, window)
    section = _render_section(
        label=label,
        runtime=runtime,
        target=target,
        workspace=workspace_path,
    )
    original = rigs_file.read_text(encoding="utf-8") if rigs_file.exists() else ""
    rigs_file.write_text(_replace_section(original, rig_key(label), section), encoding="utf-8")
    return rigs_file


def link_pair(
    *,
    workspace: str | Path,
    rig_a_session: str,
    rig_b_session: str,
    rig_a_runtime: str = "claude-code",
    rig_b_runtime: str = "codex",
    window: str = "main",
) -> Path:
    link_rig(
        workspace=workspace,
        label="Rig A",
        session=rig_a_session,
        runtime=rig_a_runtime,
        window=window,
    )
    return link_rig(
        workspace=workspace,
        label="Rig B",
        session=rig_b_session,
        runtime=rig_b_runtime,
        window=window,
    )


def linked_targets(workspace: str | Path) -> dict[str, str]:
    """Return tmux_target -> rig label for a workspace mailbox."""
    workspace_path = Path(workspace).expanduser().resolve()
    rigs_file = workspace_path / ".rig-mailbox" / "rigs.toml"
    if not rigs_file.exists():
        return {}

    current_label = ""
    current_target = ""
    out: dict[str, str] = {}
    for raw in rigs_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[rigs.") and line.endswith("]"):
            if current_label and current_target:
                out[current_target] = current_label
            current_target = ""
            key = line.removeprefix("[rigs.").removesuffix("]").strip('"')
            current_label = " ".join(part.capitalize() for part in key.split("-"))
            continue
        if line.startswith("tmux_target") and "=" in line:
            value = line.split("=", 1)[1].strip()
            if value.startswith(("'", '"')) and value.endswith(("'", '"')):
                value = value[1:-1]
            current_target = value

    if current_label and current_target:
        out[current_target] = current_label
    return out
