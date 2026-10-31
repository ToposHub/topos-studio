"""Regression tests for effect drops on overlapping timeline clips."""

from __future__ import annotations

from dataclasses import dataclass

from src.features.edit.ui.timeline_view import TimelineView


@dataclass
class _FakeClip:
    clip_id: str
    start: int = 0
    end: int = 100


class _FakeTrack:
    def __init__(self, track_id: str, track_type: str, clip: _FakeClip) -> None:
        self.track_id = track_id
        self.track_type = track_type
        self._clip = clip

    def get_clip_at(self, frame: int) -> _FakeClip | None:
        if self._clip.start <= frame < self._clip.end:
            return self._clip
        return None


class _FakeTimelineView:
    """Only the visual-index lookup needed by the resolver."""

    def __init__(self, tracks: list[_FakeTrack]) -> None:
        self._tracks = tracks

    def _track_at_visual_index(self, index: int) -> _FakeTrack | None:
        if 0 <= index < len(self._tracks):
            return self._tracks[index]
        return None


def test_effect_drop_uses_hovered_adjustment_track() -> None:
    """An upper adjustment clip wins over an overlapping lower media clip."""
    adjustment = _FakeTrack("V2", "video", _FakeClip("adjustment"))
    media = _FakeTrack("V1", "video", _FakeClip("media"))
    view = _FakeTimelineView([adjustment, media])

    track, clip = TimelineView._resolve_effect_drop_target(view, 0, 24, False)

    assert track is adjustment
    assert clip is adjustment._clip


def test_effect_drop_does_not_cross_to_another_track() -> None:
    """An empty hovered track must not attach the effect elsewhere by frame."""
    empty_adjustment = _FakeTrack("V2", "video", _FakeClip("adjustment", 50, 100))
    media = _FakeTrack("V1", "video", _FakeClip("media"))
    view = _FakeTimelineView([empty_adjustment, media])

    track, clip = TimelineView._resolve_effect_drop_target(view, 0, 24, False)

    assert track is empty_adjustment
    assert clip is None


def test_effect_drop_rejects_wrong_track_type() -> None:
    audio = _FakeTrack("A1", "audio", _FakeClip("audio"))
    view = _FakeTimelineView([audio])

    track, clip = TimelineView._resolve_effect_drop_target(view, 0, 24, False)

    assert track is None
    assert clip is None
