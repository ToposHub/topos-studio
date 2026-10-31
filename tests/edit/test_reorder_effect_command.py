"""FX-H04 regression: ReorderEffectCommand undo + compact order model.

The audit (FX-H04) found:

* ``ReorderEffectCommand.create()`` did not record ``old_order`` when the
  caller omitted it, so ``undo()`` raised ``ValueError`` and the reorder
  was effectively non-undoable.
* The reorder logic mixed tuple index ``i`` with the stored ``effect.order``
  field, so the resulting stack could end up with duplicate or gapped
  order values.

The fix defines order STRICTLY by array position (0..n-1): apply pops
the effect and re-inserts at the new position, then renumbers every
effect compactly; ``create()`` derives ``old_order`` from the timeline;
``apply`` records it on first run as a redo safety net.

These tests cover: forward reorder, undo round-trip, order stays compact
(0..n-1) after every op, and the order field matches the array index.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.commands import ReorderEffectCommand
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _effect(eid: str, etype: str = "blur") -> EffectInstance:
    return EffectInstance(effect_id=eid, effect_type=etype, params={"radius": 1.0})


def _timeline_with_effects(*effects: EffectInstance) -> TimelineSequence:
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="t1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
        effects=tuple(effects),
    )
    track = EditTrack(track_id="t1", name="V1", track_type="video",
                      index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id="seq", name="Seq",
        fps=Rational(24, 1), width=64, height=64, video_tracks=(track,),
    )


def _effect_ids(clip: ClipInstance) -> list[str]:
    return [e.effect_id for e in clip.effects]


def _orders(clip: ClipInstance) -> list[int]:
    return [e.order for e in clip.effects]


class TestReorderEffectCommand:
    def test_reorder_moves_effect_and_keeps_compact(self) -> None:
        seq = _timeline_with_effects(_effect("e0"), _effect("e1"), _effect("e2"))
        cmd = ReorderEffectCommand.create(
            track_id="t1", clip_id="c1", effect_id="e2", new_order=0,
            timeline=seq,
        )
        result = cmd.apply(seq)
        clip = result.get_clip_by_id("c1")
        assert _effect_ids(clip) == ["e2", "e0", "e1"]
        # Order is compact and matches array position.
        assert _orders(clip) == [0, 1, 2]

    def test_create_records_old_order_from_timeline(self) -> None:
        seq = _timeline_with_effects(_effect("e0"), _effect("e1"), _effect("e2"))
        cmd = ReorderEffectCommand.create(
            track_id="t1", clip_id="c1", effect_id="e1", new_order=0,
            timeline=seq,
        )
        # e1 is at index 1 → old_order must be recorded, no undo ValueError.
        assert cmd.old_order == 1

    def test_undo_restores_original_order(self) -> None:
        seq = _timeline_with_effects(_effect("e0"), _effect("e1"), _effect("e2"))
        cmd = ReorderEffectCommand.create(
            track_id="t1", clip_id="c1", effect_id="e1", new_order=0,
            timeline=seq,
        )
        moved = cmd.apply(seq)
        undone = cmd.undo(moved)
        clip = undone.get_clip_by_id("c1")
        assert _effect_ids(clip) == ["e0", "e1", "e2"]
        assert _orders(clip) == [0, 1, 2]

    def test_apply_records_old_order_when_create_omitted_timeline(self) -> None:
        """Redo safety net: if old_order was None, apply records it on first run."""
        seq = _timeline_with_effects(_effect("e0"), _effect("e1"))
        cmd = ReorderEffectCommand.create(
            track_id="t1", clip_id="c1", effect_id="e0", new_order=1,
        )  # no timeline → old_order is None
        assert cmd.old_order is None
        cmd.apply(seq)
        assert cmd.old_order == 0, "apply must record old_order for redo/undo safety"

    def test_order_field_matches_array_position_after_multiple_reorders(self) -> None:
        seq = _timeline_with_effects(
            _effect("a"), _effect("b"), _effect("c"), _effect("d"),
        )
        # Move d to front, then b to back.
        seq = ReorderEffectCommand.create(
            "t1", "c1", "d", 0, timeline=seq).apply(seq)
        seq = ReorderEffectCommand.create(
            "t1", "c1", "b", 3, timeline=seq).apply(seq)
        clip = seq.get_clip_by_id("c1")
        # Every effect.order equals its array index (compact, no gaps/dupes).
        assert _orders(clip) == list(range(len(clip.effects)))
        assert _effect_ids(clip) == ["d", "a", "c", "b"]

    def test_reorder_unknown_effect_raises(self) -> None:
        seq = _timeline_with_effects(_effect("e0"))
        cmd = ReorderEffectCommand.create(
            "t1", "c1", "nope", 0, timeline=seq)
        with pytest.raises(ValueError):
            cmd.apply(seq)
