"""Opening and activating timelines populate the mixer without a dummy edit."""

from __future__ import annotations

from unittest.mock import Mock

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QDockWidget, QMainWindow, QWidget
import pytest

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from src.shared.track_colors import TRACK_COLORS
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel


class _Page(QObject):
    timeline_display_changed = pyqtSignal(object)

    def __init__(self, timeline: TimelineSequence | None) -> None:
        super().__init__()
        self._current_timeline = timeline
        self._audio_manager = None

    def set_timeline(self, timeline: TimelineSequence | None) -> None:
        self._current_timeline = timeline
        self.timeline_display_changed.emit(timeline)


class _Host(QMainWindow, EditControllerMixin):
    def __init__(self, timeline: TimelineSequence | None) -> None:
        super().__init__()
        self.setCentralWidget(QWidget())
        self.resize(800, 540)
        self.edit_page = _Page(timeline)
        self.audio_mixer_panel = AudioMixerPanel()
        self.dock_audio_mixer = QDockWidget("Audio Mixer", self)
        self.dock_audio_mixer.setWidget(self.audio_mixer_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_audio_mixer)
        self.dock_audio_mixer.hide()
        self.menuBar().addMenu("Panels").addAction(self.dock_audio_mixer.toggleViewAction())


def _timeline(name: str, count: int) -> TimelineSequence:
    return TimelineSequence(
        timeline_id=name,
        audio_tracks=tuple(
            EditTrack(track_id=f"{name}-a{index}", name=f"Audio {index + 1}",
                      track_type="audio", index=index, color=TRACK_COLORS[index],
                      volume=0.5 + index * 0.1)
            for index in range(count)
        ),
    )


@pytest.fixture
def host(qtbot):
    window = _Host(_timeline("loaded", 4))
    qtbot.addWidget(window)
    yield window
    timer = getattr(window, "_mixer_sync_timer", None)
    if timer is not None:
        timer.stop()


def _assert_tracks(window: _Host, timeline: TimelineSequence) -> None:
    assert set(window.audio_mixer_panel._track_mixers) == {
        track.track_id for track in timeline.audio_tracks
    }
    for track in timeline.audio_tracks:
        strip = window.audio_mixer_panel._track_mixers[track.track_id]
        assert strip._track_color == track.color
        assert abs(strip.get_volume_linear() - track.volume) < 1e-6


def test_first_menu_open_populates_all_existing_tracks_without_a_command(host, qtbot) -> None:
    timeline = host.edit_page._current_timeline
    host._connect_audio_mixer_sync()
    host.show()
    qtbot.waitUntil(host.isVisible)
    assert not host.audio_mixer_panel._track_mixers
    host.dock_audio_mixer.toggleViewAction().trigger()
    assert host.dock_audio_mixer.isVisible()
    _assert_tracks(host, timeline)
    assert host.edit_page._current_timeline is timeline


def test_visible_restored_dock_is_reconciled_when_connections_are_installed(host, qtbot) -> None:
    host.show()
    host.dock_audio_mixer.show()
    qtbot.waitUntil(host.dock_audio_mixer.isVisible)
    assert not host.audio_mixer_panel._track_mixers
    host._connect_audio_mixer_sync()
    _assert_tracks(host, host.edit_page._current_timeline)


def test_color_defers_first_hidden_strips_and_syncs_engine_until_dock_opens(host, qtbot) -> None:
    host.current_page = "Color"
    host._connect_audio_mixer_sync()
    host.show()
    engine = Mock()
    host.edit_page._audio_manager = engine
    latest = _timeline("switched", 2)
    host.edit_page.set_timeline(latest)
    qtbot.waitUntil(lambda: engine.set_master_volume.called)
    engine.set_master_muted.assert_called_with(latest.master_muted)
    assert engine.set_track_routing.call_count == len(latest.audio_tracks)
    assert not host.audio_mixer_panel._track_mixers

    host.dock_audio_mixer.show()
    _assert_tracks(host, latest)
    host.dock_audio_mixer.hide()
    updated = _timeline("updated", 3)
    host.edit_page.set_timeline(updated)
    qtbot.waitUntil(lambda: updated.audio_tracks[0].track_id in host.audio_mixer_panel._track_mixers)
    _assert_tracks(host, updated)


def test_color_syncs_strips_for_a_visible_mixer_dialog(host, qtbot) -> None:
    host.current_page = "Color"
    host._connect_audio_mixer_sync()
    host.show()
    dialog = QDialog(host.audio_mixer_panel)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)
    host._fire_audio_mixer_sync()
    _assert_tracks(host, host.edit_page._current_timeline)


def test_timeline_activation_refreshes_an_already_visible_mixer(host, qtbot) -> None:
    host._connect_audio_mixer_sync()
    host.show()
    host.dock_audio_mixer.show()
    _assert_tracks(host, host.edit_page._current_timeline)
    switched = _timeline("switched", 2)
    host.edit_page.set_timeline(switched)
    qtbot.waitUntil(lambda: set(host.audio_mixer_panel._track_mixers) == {
        track.track_id for track in switched.audio_tracks
    })
    _assert_tracks(host, switched)


def test_pending_sync_uses_latest_canonical_sequence_and_clears_on_close(host, qtbot) -> None:
    host._connect_audio_mixer_sync()
    initial = host.edit_page._current_timeline
    host.edit_page.set_timeline(_timeline("intermediate", 2))
    latest = _timeline("latest", 3)
    host.edit_page.set_timeline(latest)
    # A delayed notification must not bring the old immutable snapshot back.
    host.edit_page.timeline_display_changed.emit(initial)
    qtbot.waitUntil(lambda: set(host.audio_mixer_panel._track_mixers) == {
        track.track_id for track in latest.audio_tracks
    })
    _assert_tracks(host, latest)
    host.edit_page.set_timeline(None)
    qtbot.waitUntil(lambda: not host.audio_mixer_panel._track_mixers)
    assert host.audio_mixer_panel._master_mixer is not None
