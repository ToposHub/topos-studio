"""Track identity colors reach both views through the real command/sync path."""

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QStyleOptionGraphicsItem

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.commands import MoveTrackCommand
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.track_colors import resolve_track_color
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel
from src.shared.ui.style_sheet import DARK_THEME


def _assert_colors(qapp, view, panel, track_id, expected) -> None:
    qapp.processEvents()
    header = view._track_headers[track_id]
    image = QImage(220, header._track_height, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    header.paint(painter, QStyleOptionGraphicsItem())
    painter.end()
    assert image.pixelColor(0, 30) == QColor(expected)
    strip = panel._track_mixers[track_id]
    label = strip._track_tag_label
    origin = label.mapTo(panel, QPoint(10, 0))
    image = panel.grab().toImage()
    dpr = image.devicePixelRatio()
    assert image.pixelColor(round(origin.x() * dpr), round(origin.y() * dpr)) == QColor(expected)


@pytest.mark.parametrize("saved_color", [None, "#aa85be"])
def test_color_command_undo_redo_and_reorder_keep_timeline_and_mixer_in_sync(qapp, qtbot, saved_color) -> None:
    timeline = TimelineSequence("colors", "Colors", audio_tracks=(
        EditTrack("dialogue", "Dialogue", "audio", 0, color=saved_color, is_locked=True),
        EditTrack("music", "Music", "audio", 1, color="#739bc5"),
    ))
    view = TimelineView()
    panel = AudioMixerPanel()
    qtbot.addWidget(view)
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    view.resize(900, 650)
    panel.resize(266, 500)
    host = SimpleNamespace(audio_mixer_panel=panel, timeline_panel=SimpleNamespace(timeline_view=view),
                           _get_mixer_audio_manager=lambda: None)
    sync = lambda snapshot: EditControllerMixin._sync_audio_mixer_from_timeline(host, snapshot)
    view.timeline_changed.connect(sync)
    panel.track_color_changed.connect(
        lambda track_id, color: EditControllerMixin._on_mixer_track_color_changed(host, track_id, color))
    view.load_timeline(timeline)
    sync(timeline)
    view.show()
    panel.show()
    initial = resolve_track_color("dialogue", saved_color)
    _assert_colors(qapp, view, panel, "dialogue", initial)

    # The Mixer action must update the existing header through incremental diff.
    header = view._track_headers["dialogue"]
    panel.track_color_changed.emit("dialogue", "#c68d70")
    assert view._track_headers["dialogue"] is header
    _assert_colors(qapp, view, panel, "dialogue", "#c68d70")
    view.undo()
    assert view._timeline.audio_tracks[0].color == saved_color
    _assert_colors(qapp, view, panel, "dialogue", initial)
    view.redo()
    _assert_colors(qapp, view, panel, "dialogue", "#c68d70")
    panel.track_color_changed.emit("dialogue", None)
    _assert_colors(qapp, view, panel, "dialogue", resolve_track_color("dialogue", None))
    view._execute_command(MoveTrackCommand.create("dialogue", "down"))
    _assert_colors(qapp, view, panel, "dialogue", resolve_track_color("dialogue", None))
    assert view._timeline.get_track_by_id("dialogue").color is None
    # Full reload uses the same saved color/fallback as incremental updates.
    view.load_timeline(TimelineSequence.from_dict(view._timeline.to_dict()))
    _assert_colors(qapp, view, panel, "dialogue", resolve_track_color("dialogue", None))
