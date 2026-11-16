"""Resize keeps cut interactions intact without rebuilding their metadata."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QPointF

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_ui


def _sequence() -> TimelineSequence:
    tracks = []
    for kind in ("video", "audio"):
        for index in range(2):
            track_id = f"{kind}-{index}"
            clips = tuple(ClipInstance(
                f"{track_id}-{i}", "source", track_id, i * 100, 100, 0, 100,
            ) for i in range(6))
            tracks.append(EditTrack(track_id, track_id, kind, index, clips=clips))
    return TimelineSequence("resize", video_tracks=tuple(tracks[:2]), audio_tracks=tuple(tracks[2:]))


@pytest.mark.parametrize("tool", [EditTool.SELECT, EditTool.ROLL, EditTool.BLADE])
def test_resize_preserves_cut_metadata_and_positions_without_resynchronizing_content(
    qapp: Any, qtbot: Any, monkeypatch: pytest.MonkeyPatch, tool: EditTool,
) -> None:
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_sequence())
    view.resize(1000, 500)
    view.show()
    qapp.processEvents()
    view.set_tool(tool)
    qapp.processEvents()
    handles = dict(view._roll_handles)
    metadata = {key: (h.cut_frame, h._original_cut_frame, h._active_tool,
                      h.acceptedMouseButtons(), h._top_hit_inset, h._bottom_hit_inset)
                for key, h in handles.items()}
    synchronize = Mock(wraps=view._sync_roll_handles_for_track)
    monkeypatch.setattr(view, "_sync_roll_handles_for_track", synchronize)
    for width, height in ((1100, 560), (920, 390), (1040, 620)):
        view.resize(width, height)
        qapp.processEvents()
        assert view._roll_handles == handles
        assert synchronize.call_count == 0
        for key, handle in handles.items():
            assert (handle.cut_frame, handle._original_cut_frame, handle._active_tool,
                    handle.acceptedMouseButtons(), handle._top_hit_inset, handle._bottom_hit_inset) == metadata[key]
            engine = view._layout_engine
            assert handle.pos() == QPointF(engine.frame_to_x(handle.cut_frame), engine.track_top(handle.track_id))
            assert handle._track_height == engine.track_height(handle.track_id)

    # A content-changing full reload still removes an obsolete cut. Only the
    # resize path skips metadata work; the editing path remains authoritative.
    timeline = view.get_timeline()
    track = timeline.video_tracks[0]
    view.load_timeline(replace(timeline, video_tracks=(replace(track, clips=track.clips[:2]),)
                              + timeline.video_tracks[1:]))
    assert synchronize.call_count > 0
    assert len(view._roll_handles) == len(handles) - 4
