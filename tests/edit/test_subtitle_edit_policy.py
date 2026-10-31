"""SUB-P0-04 contract: subtitle edit policy legal-range & rollback.

``SubtitleEditPolicy`` computes the legal placement range for a subtitle
move/trim given neighbours and track lock state, and provides the restore
helper that snaps a SubtitleClipItem back when a release-time commit fails.

These tests are pure-logic (no Qt event loop needed for the policy itself).
The restore contract is verified against a lightweight fake item.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.features.edit.core.captions.edit_policy import (
    SubtitleEditPolicy,
    SubtitleLegalRange,
)

pytestmark = pytest.mark.edit_core


@dataclass(frozen=True)
class _Cue:
    """Lightweight cue stand-in (dict-form also supported by the policy)."""
    clip_id: str
    timeline_start: int
    timeline_duration: int

    @property
    def timeline_end(self) -> int:
        return self.timeline_start + self.timeline_duration


def _cue(cid: str, start: int, duration: int) -> _Cue:
    return _Cue(cid, start, duration)


# ── Move range ─────────────────────────────────────────────────────────────


def test_move_range_unbounded_without_right_neighbour() -> None:
    # cue 'a' currently at start=10, left neighbour 'b' [0,5); no right nb.
    rng = SubtitleEditPolicy.compute_move_range(
        clip_id="a", duration=10, neighbours=(_cue("b", 0, 5),),
        current_start=10,
    )
    # 'a' can start anywhere from end of 'b' (5) onward.
    assert rng.editable is True
    assert rng.min_start == 5
    assert rng.clamp_start(3) == 5
    assert rng.clamp_start(100) == 100


def test_move_range_bounded_by_right_neighbour() -> None:
    # gap between b [0,20) and c [50,60); 'a' currently at start=25, dur 10.
    rng = SubtitleEditPolicy.compute_move_range(
        clip_id="a", duration=10,
        neighbours=(_cue("b", 0, 20), _cue("c", 50, 10)),
        current_start=25,
    )
    assert rng.editable is True
    assert rng.min_start == 20
    # max_start so that a+duration <= 50 → 40
    assert rng.max_start == 40
    assert rng.clamp_start(45) == 40
    assert rng.clamp_start(20) == 20


def test_move_range_locked_track_not_editable() -> None:
    rng = SubtitleEditPolicy.compute_move_range(
        clip_id="a", duration=10, neighbours=(), track_locked=True
    )
    assert rng.editable is False


def test_move_range_duration_filling_gap_is_single_point() -> None:
    # 'a' duration 5 exactly fills the [20,25) gap → only legal start is 20.
    rng = SubtitleEditPolicy.compute_move_range(
        clip_id="a", duration=5,
        neighbours=(_cue("b", 0, 20), _cue("c", 25, 10)),
        current_start=20,
    )
    assert rng.editable is True
    assert rng.min_start == 20
    assert rng.max_start == 20  # 25 - 5


# ── Trim range ─────────────────────────────────────────────────────────────


def test_trim_range_left_and_right_bounds() -> None:
    # current cue a [20,40); left neighbour b [0,10); right neighbour c [60,70).
    rng = SubtitleEditPolicy.compute_trim_range(
        clip_id="a", current_start=20, current_duration=20,
        neighbours=(_cue("b", 0, 10), _cue("c", 60, 10)),
    )
    assert rng.editable is True
    # left-trim: start cannot go below left neighbour end (10)
    assert rng.min_start == 10
    # right-trim duration max: right neighbour start (60) - current start (20) = 40
    assert rng.max_duration == 40


def test_trim_range_locked_track_not_editable() -> None:
    rng = SubtitleEditPolicy.compute_trim_range(
        clip_id="a", current_start=0, current_duration=10,
        neighbours=(), track_locked=True,
    )
    assert rng.editable is False


def test_clamp_duration_never_below_one() -> None:
    rng = SubtitleLegalRange(
        editable=True, min_start=0, max_start=100,
        min_duration=1, max_duration=5,
    )
    assert rng.clamp_duration(0) == 1
    assert rng.clamp_duration(3) == 3
    assert rng.clamp_duration(99) == 5


# ── Restore on failure ─────────────────────────────────────────────────────


class _FakeItem:
    """Records restore_geometry calls; stands in for SubtitleClipItem."""

    def __init__(self) -> None:
        self.restored: list[tuple[int, int]] = []

    def restore_geometry(self, start: int, duration: int) -> None:
        self.restored.append((start, duration))


def test_restore_on_failure_calls_restore_geometry() -> None:
    item = _FakeItem()
    SubtitleEditPolicy.restore_on_failure(item, 12, 34)
    assert item.restored == [(12, 34)]


def test_restore_on_failure_noop_without_method() -> None:
    # An object without restore_geometry must not crash.
    SubtitleEditPolicy.restore_on_failure(object(), 0, 10)


def test_restore_on_failure_none_item_is_safe() -> None:
    SubtitleEditPolicy.restore_on_failure(None, 0, 10)
