from __future__ import annotations

ROLE_ALIASES = {
    "a": "Rig A",
    "rig-a": "Rig A",
    "rig a": "Rig A",
    "head": "Rig A",
    "claude": "Rig A",
    "b": "Rig B",
    "rig-b": "Rig B",
    "rig b": "Rig B",
    "codex": "Rig B",
    "c": "Rig C",
    "rig-c": "Rig C",
    "rig c": "Rig C",
    "opencode": "Rig C",
    "loop": "Loop Controller",
    "controller": "Loop Controller",
    "loop-controller": "Loop Controller",
    "loop controller": "Loop Controller",
}


def resolve_role(value: object) -> str | None:
    """Resolve every supported role spelling to its canonical display label."""
    normalized = " ".join(str(value or "").strip().casefold().replace("_", "-").split())
    return ROLE_ALIASES.get(normalized) or ROLE_ALIASES.get(normalized.replace(" ", "-"))
