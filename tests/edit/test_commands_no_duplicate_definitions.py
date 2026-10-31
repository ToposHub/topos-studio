"""FX-M10 regression: no duplicate command class definitions.

The audit (FX-M10) found that ``commands.py`` carried TWO copies of
``AddEffectCommand`` / ``RemoveEffectCommand`` / ``UpdateEffectParamCommand``
/ ``SetEffectEnabledCommand`` plus a dead ``ReorderEffectsCommand`` (note
the plural). Python silently keeps the last definition, so the first
block — including a buggy hand-written ``ClipInstance`` constructor —
was dead code that confused maintainers and made undo/serialisation
behaviour hard to audit.

This test parses the package sources and asserts each command class name
appears exactly once at definition (``^class Name``), so a future
duplicate is caught immediately rather than shadowing the live class.
(P6 split: commands.py became a package; the guard now counts definitions
across every first-level module of the package, so a duplicate hidden in
a sibling submodule is still caught.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import src.features.edit.core.commands as commands_mod

pytestmark = pytest.mark.edit_core

_COMMANDS_PATH = Path(commands_mod.__file__)
_COMMANDS_PACKAGE_DIR = _COMMANDS_PATH.parent


def _package_sources() -> list[str]:
    """Sources of every first-level module in the commands package."""
    return [p.read_text(encoding="utf-8")
            for p in sorted(_COMMANDS_PACKAGE_DIR.glob("*.py"))]


def _count_class_definitions(source: str, class_name: str) -> int:
    """Count top-level ``class <name>:`` definitions (not mentions)."""
    pattern = re.compile(rf"^class {re.escape(class_name)}\b", re.MULTILINE)
    return len(pattern.findall(source))


@pytest.mark.parametrize("class_name", [
    "AddEffectCommand",
    "RemoveEffectCommand",
    "UpdateEffectParamCommand",
    "SetEffectEnabledCommand",
    "ReorderEffectCommand",
])
def test_command_class_defined_once(class_name: str) -> None:
    """Each effect command must have exactly ONE definition in the package.

    A second definition would shadow the first (Python keeps the last),
    hiding dead/buggy code and making serialisation/undo behaviour
    ambiguous. If you legitimately need to rename, delete the old one.
    """
    count = sum(
        _count_class_definitions(source, class_name)
        for source in _package_sources()
    )
    assert count == 1, (
        f"{class_name} is defined {count} time(s) across the "
        f"{_COMMANDS_PACKAGE_DIR.name}/ package; "
        f"expected exactly 1. A duplicate shadows the live class — "
        f"remove the stale definition."
    )


def test_dead_reorder_effects_command_is_gone() -> None:
    """The dead plural ``ReorderEffectsCommand`` must not return.

    It was a leftover from the duplicate block and was never registered;
    the live class is the singular ``ReorderEffectCommand``.
    """
    assert not hasattr(commands_mod, "ReorderEffectsCommand"), (
        "ReorderEffectsCommand (plural) reappeared — this is dead code "
        "from the duplicate block. Use ReorderEffectCommand (singular)."
    )


def test_effect_commands_importable_via_facade() -> None:
    """The facade must resolve the LIVE effect command classes.

    Guards against the deletion of the active block while leaving the
    dead one (which would break undo/redo at runtime).
    """
    from src.features.edit.commands_facade import get_command_classes

    classes = get_command_classes()
    assert classes is not None, "edit command module must be importable"
    for name in (
        "AddEffectCommand",
        "RemoveEffectCommand",
        "UpdateEffectParamCommand",
        "SetEffectEnabledCommand",
    ):
        assert name in classes, f"{name} missing from command facade"

    # The live AddEffectCommand takes effect_type (not a pre-built effect),
    # confirming the active (second) block survived the cleanup.
    add_cls = classes["AddEffectCommand"]
    field_names = {f.name for f in __import__("dataclasses").fields(add_cls)}
    assert "effect_type" in field_names, (
        "AddEffectCommand must take effect_type — the active block was "
        "accidentally deleted and the dead block (effect field) is now live"
    )
