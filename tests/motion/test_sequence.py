"""W1 Sequence/Series: layout, ripple, gap/overlap, parallel, stagger."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.motion import (
    SequenceChild, SequenceGroup, SeriesGroup, SeriesStep, TimeRange, stagger_starts,
)


class TestSeriesLayout:
    def test_back_to_back_layout(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 10), SeriesStep("b", 8), SeriesStep("c", 6)))
        layout = dict(series.layout())
        assert layout["a"].start == 0
        assert layout["b"].start == 10
        assert layout["c"].start == 18
        assert layout["c"].end == 24

    def test_duration_and_end(self) -> None:
        series = SeriesGroup(start=5, steps=(SeriesStep("a", 10), SeriesStep("b", 8)))
        assert series.end == 5 + 10 + 8
        assert series.duration == 18

    def test_gap_between_steps(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 10, gap_after=4), SeriesStep("b", 8)))
        layout = dict(series.layout())
        assert layout["b"].start == 14   # 10 + gap 4

    def test_negative_gap_overlaps(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 10, gap_after=-4), SeriesStep("b", 8)))
        layout = dict(series.layout())
        assert layout["b"].start == 6    # overlap of 4 frames
        # both visible during overlap
        assert "a" in series.visible_steps(7)
        assert "b" in series.visible_steps(7)


class TestSeriesRipple:
    def test_changing_step_duration_ripples_to_later(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 10), SeriesStep("b", 8), SeriesStep("c", 6)))
        grown = series.with_step_duration("b", 20)
        layout = dict(grown.layout())
        assert layout["b"].start == 10
        assert layout["c"].start == 30   # 10 + 20 (b grew), c shifted
        assert layout["a"].start == 0    # earlier unaffected

    def test_changing_gap_ripples(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 10), SeriesStep("b", 8), SeriesStep("c", 6)))
        with_gap = series.with_step_gap("a", 5)
        layout = dict(with_gap.layout())
        assert layout["b"].start == 15
        assert layout["c"].start == 23

    def test_invalid_duration_rejected(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 10),))
        with pytest.raises(ValueError):
            series.with_step_duration("a", 0)


class TestSeriesLocalFrame:
    def test_local_frame_resolves_per_step(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 10), SeriesStep("b", 8)))
        assert series.local_frame("a", 0) == 0
        assert series.local_frame("a", 9) == 9
        assert series.local_frame("b", 10) == 0
        assert series.local_frame("b", 17) == 7
        assert series.local_frame("a", 10) is None   # b's window

    def test_step_range_lookup(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 10),))
        assert series.step_range("a").start == 0
        assert series.step_range("missing") is None

    def test_duplicate_step_ids_rejected(self) -> None:
        with pytest.raises(ValueError):
            SeriesGroup(steps=(SeriesStep("a", 5), SeriesStep("a", 5)))


class TestSequenceParallel:
    def test_overlapping_children(self) -> None:
        seq = SequenceGroup(children=(
            SequenceChild("title", TimeRange(start=0, duration=30)),
            SequenceChild("card", TimeRange(start=6, duration=24)),
        ))
        # both visible at frame 10
        assert "title" in seq.visible_children(10)
        assert "card" in seq.visible_children(10)
        # only title at frame 2
        assert seq.visible_children(2) == ("title",)

    def test_local_frame(self) -> None:
        seq = SequenceGroup(children=(SequenceChild("c", TimeRange(start=5, duration=10)),))
        assert seq.local_frame("c", 5) == 0
        assert seq.local_frame("c", 12) == 7
        assert seq.local_frame("c", 4) is None

    def test_end_is_max_of_children(self) -> None:
        seq = SequenceGroup(children=(
            SequenceChild("a", TimeRange(start=0, duration=10)),
            SequenceChild("b", TimeRange(start=5, duration=40)),
        ))
        assert seq.end == 45


class TestStaggerStarts:
    def test_stagger_builds_parallel_sequence(self) -> None:
        seq = stagger_starts(("c1", "c2", "c3", "c4"), interval=6, duration=24)
        layout = dict(seq.layout())
        assert layout["c1"].start == 0
        assert layout["c2"].start == 6
        assert layout["c3"].start == 12
        assert layout["c4"].start == 18
        assert all(tr.duration == 24 for tr in layout.values())

    def test_stagger_rejects_invalid_params(self) -> None:
        with pytest.raises(ValueError):
            stagger_starts(("a",), interval=-1, duration=10)
        with pytest.raises(ValueError):
            stagger_starts(("a",), interval=1, duration=0)
