"""W1 TimeRange: local-frame mapping, first/last/off-by-one, trim/freeze, nesting."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.motion import TimeRange, compose_local_frame, transform_to_parent


class TestBasicRange:
    def test_half_open_contains(self) -> None:
        tr = TimeRange(start=10, duration=5)
        assert tr.contains(10) is True
        assert tr.contains(14) is True
        assert tr.contains(15) is False   # half-open end
        assert tr.contains(9) is False

    def test_disabled_range_contains_nothing(self) -> None:
        tr = TimeRange(start=0, duration=10, enabled=False)
        for f in (0, 5, 9):
            assert tr.contains(f) is False

    def test_end_property(self) -> None:
        assert TimeRange(start=3, duration=7).end == 10

    def test_invalid_construction(self) -> None:
        with pytest.raises(ValueError):
            TimeRange(start=-1, duration=5)
        with pytest.raises(ValueError):
            TimeRange(start=0, duration=0)
        with pytest.raises(ValueError):
            TimeRange(start=0, duration=5, trim_in=-1)


class TestLocalFrame:
    def test_local_frame_without_trim(self) -> None:
        tr = TimeRange(start=10, duration=5)
        assert tr.local_frame(10) == 0
        assert tr.local_frame(14) == 4
        assert tr.local_frame(9) is None
        assert tr.local_frame(15) is None

    def test_local_frame_with_trim_in(self) -> None:
        tr = TimeRange(start=10, duration=5, trim_in=2)
        assert tr.local_frame(10) == 2
        assert tr.local_frame(14) == 6

    def test_first_and_last_frame_no_off_by_one(self) -> None:
        tr = TimeRange(start=0, duration=24)
        assert tr.local_frame(0) == 0       # first
        assert tr.local_frame(23) == 23     # last (half-open: 24 excluded)
        assert tr.local_frame(24) is None

    def test_freeze_holds_last_content_frame(self) -> None:
        tr = TimeRange(start=0, duration=20, freeze=True)
        # content only 10 frames; from frame 10..19 content exhausted -> frozen at 9
        assert tr.local_frame(9, content_duration=10) == 9
        assert tr.local_frame(15, content_duration=10) == 9
        assert tr.local_frame(19, content_duration=10) == 9

    def test_no_freeze_hides_when_content_exhausted(self) -> None:
        tr = TimeRange(start=0, duration=20, freeze=False)
        assert tr.local_frame(9, content_duration=10) == 9
        assert tr.local_frame(10, content_duration=10) is None


class TestTransforms:
    def test_shifted(self) -> None:
        tr = TimeRange(start=10, duration=5, trim_in=2)
        shifted = tr.shifted(8)
        assert shifted.start == 18
        assert shifted.trim_in == 2

    def test_with_duration(self) -> None:
        tr = TimeRange(start=10, duration=5).with_duration(20)
        assert tr.duration == 20
        with pytest.raises(ValueError):
            TimeRange(start=0, duration=1).with_duration(0)

    def test_transform_to_parent_offsets_start(self) -> None:
        parent = TimeRange(start=100, duration=50)
        child = TimeRange(start=10, duration=5, trim_in=1)
        combined = transform_to_parent(child, parent)
        assert combined.start == 110
        assert combined.trim_in == 1

    def test_transform_disabled_parent_disables_child(self) -> None:
        parent = TimeRange(start=0, duration=10, enabled=False)
        child = TimeRange(start=2, duration=3)
        assert transform_to_parent(child, parent).enabled is False

    def test_compose_local_frame_chains(self) -> None:
        parent = TimeRange(start=100, duration=50)
        child = TimeRange(start=10, duration=10)
        # grandparent 105 -> parent local 5 -> child window is [10,20) -> None
        assert compose_local_frame(105, parent, child) is None
        # grandparent 112 -> parent local 12 -> child local 2
        assert compose_local_frame(112, parent, child) == 2
