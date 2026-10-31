"""FX-C06 regression: generators cannot be attached as effects.

Audit FX-C06: generator definitions (adjustment_clip / solid_color) are
clip TEMPLATES — dragging them onto the timeline creates a new clip.
But the panel double-click path used to emit ``effect_add_requested``
for ANY registered effect_type, including generators, which created a
useless EffectInstance the renderer silently drops.

The fix blocks the attach at two layers (defence in depth):
1. ``AddEffectCommand.create()`` raises ValueError for generator types.
2. The panel double-click handler shows a drag-to-timeline hint instead
   of emitting the attach signal.

This test pins layer 1 (the command-level guard) directly; the panel
guard is Qt-UI and covered by the structural test in
test_effect_catalog_capabilities.py.
"""

from __future__ import annotations

import pytest

from src.features.edit.commands_facade import get_command_classes
from src.features.edit.core.effect_registry import GENERATOR_EFFECT_TYPES

pytestmark = pytest.mark.edit_core


@pytest.fixture(scope="module")
def add_effect_command_cls():
    classes = get_command_classes()
    assert classes is not None, "edit commands must be importable"
    return classes["AddEffectCommand"]


@pytest.mark.parametrize("gen_type", sorted(GENERATOR_EFFECT_TYPES))
def test_create_rejects_generator(add_effect_command_cls, gen_type) -> None:
    """AddEffectCommand.create must refuse generator effect types."""
    with pytest.raises(ValueError, match="generator"):
        add_effect_command_cls.create(
            track_id="t1", clip_id="c1", effect_type=gen_type,
        )


def test_create_accepts_real_video_filter(add_effect_command_cls) -> None:
    """A genuine catalog video filter (blur) is accepted as before."""
    cmd = add_effect_command_cls.create(
        track_id="t1", clip_id="c1", effect_type="blur",
    )
    assert cmd.effect_type == "blur"
