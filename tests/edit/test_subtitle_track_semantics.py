"""Subtitle-track controls use caption-specific visibility semantics."""

from __future__ import annotations

from PyQt6.QtWidgets import QLabel

from src.features.edit.core.timeline_v2 import EditTrack
from src.features.edit.core.commands import SetTrackVisibilityCommand
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.features.edit.ui.timeline_items import TrackHeaderItem
from src.features.edit.ui.timeline_view import TimelineView


def _subtitle_track() -> EditTrack:
    return EditTrack(
        track_id="s1",
        name="English",
        track_type="subtitle",
        index=0,
        is_visible=True,
    )


def test_subtitle_header_exposes_visibility_not_mute_or_solo(qapp) -> None:
    header = TrackHeaderItem(
        "s1", "English", 0, "subtitle", is_visible=True,
    )

    assert header._get_button_at_pos(
        header._get_mute_button_rect().center()
    ) == "visibility"
    assert header._get_button_at_pos(
        header._get_solo_button_rect().center()
    ) is None

    visibility_events: list[tuple[str, bool]] = []
    mute_events: list[tuple[str, bool]] = []
    solo_events: list[tuple[str, bool]] = []
    header.visibility_changed.connect(
        lambda track_id, visible: visibility_events.append((track_id, visible))
    )
    header.mute_changed.connect(
        lambda track_id, muted: mute_events.append((track_id, muted))
    )
    header.solo_changed.connect(
        lambda track_id, solo: solo_events.append((track_id, solo))
    )

    header._toggle_visibility()

    assert visibility_events == [("s1", False)]
    assert mute_events == []
    assert solo_events == []


def test_subtitle_properties_show_visible_and_lock_only(qapp) -> None:
    panel = EditPropertiesPanel()
    panel.set_track(_subtitle_track())
    section = panel._sections["track:s1"]
    labels = {
        label.text() for label in section.findChildren(QLabel)
    }

    assert "Visible" in labels
    assert "Lock" in labels
    assert "Mute" not in labels
    assert "Solo" not in labels


def test_visibility_command_keeps_subtitle_header_in_sync(qapp) -> None:
    sequence_track = _subtitle_track()
    from src.features.edit.core.timeline_v2 import TimelineSequence
    sequence = TimelineSequence(
        timeline_id="seq", name="Sequence", width=1920, height=1080, fps=24,
        subtitle_tracks=(sequence_track,),
    )
    view = TimelineView()
    view.load_timeline(sequence)

    view._execute_command(SetTrackVisibilityCommand.create("s1", False))

    assert view._timeline.get_track_by_id("s1").is_visible is False
    assert view._track_headers["s1"]._is_visible is False
