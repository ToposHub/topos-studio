"""SUB-P1-06 contract (gap close): timeline layout persistence.

Phase 1.3 first shipped resizable lanes + scrollbars but the layout was
session-only — reopening a project lost track heights / scroll / area ratios.
This test pins the round-trip: layout → UIState → JSON dict → UIState → view,
and that ``set_layout_state`` actually restores the track heights.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.serialization import UIState
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.ui.timeline_layout import (
    DEFAULT_SUBTITLE_TRACK_HEIGHT,
    MAX_SUBTITLE_TRACK_HEIGHT,
    TimelineLayoutState,
)
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_core


def test_layout_state_roundtrip() -> None:
    """to_dict → from_dict preserves every field."""
    state = TimelineLayoutState(
        video_area_ratio=0.62,
        subtitle_area_ratio=0.3,
        track_heights={"v1": 90, "s1": 50},
        video_scroll_offset=120,
        audio_scroll_offset=40,
        subtitle_scroll_offset=8,
    )
    restored = TimelineLayoutState.from_dict(state.to_dict())
    assert restored.video_area_ratio == 0.62
    assert restored.subtitle_area_ratio == 0.3
    assert restored.track_heights == {"v1": 90, "s1": 50}
    assert restored.video_scroll_offset == 120
    assert restored.audio_scroll_offset == 40
    assert restored.subtitle_scroll_offset == 8


def test_layout_state_from_none_returns_default() -> None:
    restored = TimelineLayoutState.from_dict(None)
    assert restored.track_heights == {}
    assert restored.video_scroll_offset == -1


def test_uistate_persists_timeline_layout() -> None:
    """UIState carries timeline_layout through its own to_dict/from_dict."""
    layout = TimelineLayoutState(track_heights={"s1": 50}).to_dict()
    ui = UIState(active_timeline_id="t1", timeline_layout=layout)
    data = ui.to_dict()
    assert data["timeline_layout"] == layout

    restored = UIState.from_dict(data)
    assert restored.timeline_layout == layout
    # Old projects without the field default to None.
    legacy = UIState.from_dict({"active_timeline_id": "t1"})
    assert legacy.timeline_layout is None


def _seq_with_subtitle() -> TimelineSequence:
    sub = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(clip_id="sub-1", track_id="s1",
                         timeline_start=0, timeline_duration=72, text="x"),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(sub,),
    )


def test_set_layout_state_restores_track_height(qapp) -> None:
    """After load + set_layout_state, a saved subtitle height takes effect."""
    view = TimelineView()
    view.resize(1000, 600)
    view.load_timeline(_seq_with_subtitle())

    # Default height before restore.
    assert view._layout_engine.track_height("s1") == DEFAULT_SUBTITLE_TRACK_HEIGHT

    saved = TimelineLayoutState(track_heights={"s1": MAX_SUBTITLE_TRACK_HEIGHT}).to_dict()
    view.set_layout_state(saved)

    assert view._layout_engine.track_height("s1") == MAX_SUBTITLE_TRACK_HEIGHT


def test_capture_then_restore_round_trips_through_view(qapp) -> None:
    """layout_state() snapshot → set_layout_state() restores the same heights."""
    view = TimelineView()
    view.resize(1000, 600)
    view.load_timeline(_seq_with_subtitle())

    view._on_track_height_changed("s1", 60)
    snapshot = view.layout_state()
    assert snapshot["track_heights"]["s1"] == 60

    # Simulate a reload: fresh view, load, then restore the snapshot.
    view2 = TimelineView()
    view2.resize(1000, 600)
    view2.load_timeline(_seq_with_subtitle())
    assert view2._layout_engine.track_height("s1") == DEFAULT_SUBTITLE_TRACK_HEIGHT

    view2.set_layout_state(snapshot)
    assert view2._layout_engine.track_height("s1") == 60
