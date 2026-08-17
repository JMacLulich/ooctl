from __future__ import annotations

import pytest

from occtl import mailbox
from occtl.roles import resolve_role


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("rig-a", "Rig A"),
        ("Rig A", "Rig A"),
        ("RIG_A", "Rig A"),
        ("a", "Rig A"),
        ("rig-b", "Rig B"),
        ("Rig B", "Rig B"),
        ("RIG_B", "Rig B"),
        ("b", "Rig B"),
        ("rig-c", "Rig C"),
        ("Rig C", "Rig C"),
        ("RIG_C", "Rig C"),
        ("c", "Rig C"),
        ("loop-controller", "Loop Controller"),
        ("Loop Controller", "Loop Controller"),
        ("LOOP_CONTROLLER", "Loop Controller"),
        ("loop", "Loop Controller"),
    ],
)
def test_resolve_role_uses_one_case_and_separator_insensitive_contract(
    alias: str, canonical: str
) -> None:
    assert resolve_role(alias) == canonical


@pytest.mark.parametrize("alias", ["rig-b", "Rig B", "RIG_B", "b"])
def test_mailbox_role_aliases_share_one_canonical_key(alias: str) -> None:
    assert mailbox.rig_key(alias) == "rig-b"
