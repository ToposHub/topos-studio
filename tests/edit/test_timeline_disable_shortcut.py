"""Regression tests for the timeline's local D enable/disable shortcut."""

from __future__ import annotations

from unittest.mock import MagicMock

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent

from src.features.edit.core.commands import MacroCommand
from src.features.edit.core.selection import (
    SelectionItem,
    SelectionType,
    TimelineSelection,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.ui.timeline_view import TimelineView


def _clip(clip_id: str, track_id: str, start: int = 0) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=48,
        source_start=0,
        source_duration=48,
    )


def test_d_toggles_every_selected_clip_type_in_one_undoable_command() -> None:
    """Video, audio, adjustment, and subtitle selections toggle together."""
    video = EditTrack(track_id="video", name="V1", track_type="video", index=0)
    audio = EditTrack(track_id="audio", name="A1", track_type="audio", index=0)
    subtitles = EditTrack(track_id="subtitles", name="S1", track_type="subtitle", index=0)
    timeline = TimelineSequence(timeline_id="timeline", name="Test", fps=24.0)
    timeline = timeline.with_track_added(video).with_track_added(audio).with_track_added(subtitles)
    timeline = timeline.with_clip_added(video.track_id, _clip("video-clip", video.track_id))
    timeline = timeline.with_clip_added(
        video.track_id,
        _clip("adjustment-clip", video.track_id, start=48),
    )
    timeline = timeline.with_clip_added(audio.track_id, _clip("audio-clip", audio.track_id))
    timeline = timeline.with_subtitle_clip_added(
        subtitles.track_id,
        SubtitleClip(
            clip_id="subtitle-clip",
            track_id=subtitles.track_id,
            timeline_start=0,
            timeline_duration=48,
            text="Subtitle",
        ),
    )
    selected_ids = ("video-clip", "adjustment-clip", "audio-clip", "subtitle-clip")
    selection = TimelineSelection(
        items=tuple(
            SelectionItem(
                item_type=SelectionType.CLIP,
                item_id=clip_id,
                timeline_id=timeline.timeline_id,
            )
            for clip_id in selected_ids
        ),
    )
    view = MagicMock()
    view._timeline = timeline
    view._selection = selection
    executed: list[object] = []
    view._execute_command.side_effect = executed.append

    TimelineView._toggle_selected_clips_enabled(view)

    assert len(executed) == 1
    assert isinstance(executed[0], MacroCommand)
    updated, applied_macro = executed[0].apply(timeline)
    for clip_id in selected_ids[:-1]:
        assert updated.get_clip_by_id(clip_id).is_enabled is False
    assert updated.get_subtitle_clip_by_id("subtitle-clip").is_enabled is False

    restored = applied_macro.undo(updated)
    for clip_id in selected_ids[:-1]:
        assert restored.get_clip_by_id(clip_id).is_enabled is True
    assert restored.get_subtitle_clip_by_id("subtitle-clip").is_enabled is True


def test_plain_d_dispatches_the_selected_clip_enable_toggle() -> None:
    """Ctrl+D remains duplicate; only unmodified D toggles clip enablement."""
    view = MagicMock()
    event = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_D,
        Qt.KeyboardModifier.NoModifier,
    )

    TimelineView.keyPressEvent(view, event)

    view._toggle_selected_clips_enabled.assert_called_once_with()
