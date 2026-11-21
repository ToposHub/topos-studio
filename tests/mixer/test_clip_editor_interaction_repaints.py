from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import QApplication, QWidget
import pytest
from pytestqt.qtbot import QtBot

from src.features.mixer.effects.panels.clip_editor import WaveformSpectralCanvas
from src.features.mixer.ui import project_clip_editor


@pytest.fixture
def panel(qtbot: QtBot) -> project_clip_editor._TimelineClipPanel:
    editor = project_clip_editor._TimelineClipPanel()
    qtbot.addWidget(editor)
    editor.resize(1200, 620)
    editor.btn_spectral.setChecked(True)
    editor.load_audio_data(
        48000, 4.0, [[-0.4] * 256], [[0.5] * 256], [[0.22] * 256],
        [[0.1, 0.2, 0.3, 0.4] for _ in range(24)],
    )
    editor.show()
    QApplication.processEvents()
    return editor


def _mouse(widget: QWidget, kind: QEvent.Type, x: float, y: float) -> None:
    point = QPointF(x, y)
    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    event = QMouseEvent(kind, point, point, none if kind == QEvent.Type.MouseMove else left,
                        none if kind == QEvent.Type.MouseButtonRelease else left,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)
    QApplication.processEvents()


@pytest.mark.parametrize("spectral", [False, True])
def test_drag_selection_reuses_waveform_and_snaps_only_on_release(
    panel: project_clip_editor._TimelineClipPanel, monkeypatch: pytest.MonkeyPatch, spectral: bool,
) -> None:
    panel._pcm = np.zeros((1, 192000), dtype=np.float32)
    panel.btn_snap_zero.setChecked(True)
    candidates = []

    def snap(_pcm: np.ndarray, sample: int, **_options: object) -> int:
        candidates.append(sample)
        return sample + 5

    monkeypatch.setattr(project_clip_editor, "find_zero_crossing", snap)
    canvas = panel.canvas
    wave, spectrum, _ = canvas._wave_and_spec_rects()
    pane = spectrum if spectral else wave
    y0 = pane.top() + pane.height() * 0.25
    y1 = pane.top() + pane.height() * 0.70
    old_waveform = canvas._static_cache
    _mouse(canvas, QEvent.Type.MouseButtonPress, canvas._time_to_x(1.0, pane), y0)
    _mouse(canvas, QEvent.Type.MouseMove, canvas._time_to_x(2.0, pane), y1)
    _mouse(canvas, QEvent.Type.MouseMove, canvas._time_to_x(2.6, pane), y1)
    assert candidates == []
    assert canvas.getSelection()[:2] == pytest.approx((1.0, 2.6))
    assert canvas._static_cache is old_waveform
    band = canvas.getSelection()[2:]

    _mouse(canvas, QEvent.Type.MouseButtonRelease, canvas._time_to_x(2.6, pane), y1)
    assert len(candidates) == 2
    assert canvas.getSelection()[0] == pytest.approx((48000 + 5) / 48000)
    assert canvas.getSelection()[2:] == band
    assert canvas._is_2d_spectral_sel is spectral
    assert canvas._static_cache is old_waveform


def test_selection_handle_keeps_opposite_anchor_when_crossing(panel: project_clip_editor._TimelineClipPanel) -> None:
    canvas = panel.canvas
    wave, _, _ = canvas._wave_and_spec_rects()
    canvas.setSelection(1.0, 2.0)
    y = wave.top() + 20.0
    _mouse(canvas, QEvent.Type.MouseButtonPress, canvas._time_to_x(2.0, wave), y)
    _mouse(canvas, QEvent.Type.MouseMove, canvas._time_to_x(0.8, wave), y)
    _mouse(canvas, QEvent.Type.MouseMove, canvas._time_to_x(0.5, wave), y)
    _mouse(canvas, QEvent.Type.MouseButtonRelease, canvas._time_to_x(0.5, wave), y)
    assert canvas.getSelection()[:2] == pytest.approx((0.5, 1.0))


def test_minimap_handle_over_playhead_resizes_and_reuses_summary(panel: project_clip_editor._TimelineClipPanel) -> None:
    mini = panel.minimap
    summary = mini._summary_cache
    _mouse(mini, QEvent.Type.MouseButtonPress, mini._sec_to_x(0.0), 16.0)
    _mouse(mini, QEvent.Type.MouseMove, mini._sec_to_x(1.0), 16.0)
    _mouse(mini, QEvent.Type.MouseButtonRelease, mini._sec_to_x(1.0), 16.0)
    assert panel.canvas._view_start == pytest.approx(1.0)
    assert panel.canvas._view_end == pytest.approx(4.0)
    assert mini._playhead == 0.0
    assert mini._summary_cache is summary


def test_fractional_wheel_keeps_time_under_pointer(panel: project_clip_editor._TimelineClipPanel) -> None:
    canvas = panel.canvas
    panel._sync_viewport(1.0, 3.0)
    body = canvas._body_rect()
    point = QPointF(canvas._time_to_x(1.5, body), body.center().y())
    event = QWheelEvent(point, point, QPoint(0, 30), QPoint(0, 0), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
    QApplication.sendEvent(canvas, event)
    assert canvas._x_to_time(point.x(), body) == pytest.approx(1.5)
    assert canvas._view_end - canvas._view_start == pytest.approx(2.0 * 0.80 ** 0.25)


def test_spectral_contrast_and_playhead_keep_static_waveform(panel: project_clip_editor._TimelineClipPanel) -> None:
    canvas = panel.canvas
    cache = canvas._static_cache
    color = canvas._spec_image.pixelColor(0, 0)
    canvas.setSpectralContrastBoost(15.0)
    panel.update_playhead(1.5)
    QApplication.processEvents()
    assert canvas._spec_image.pixelColor(0, 0) != color
    assert canvas._static_cache is cache
    assert not canvas._spectrum_dirty


def test_playhead_drag_seeks_once_without_audio_clock_taking_over(
    panel: project_clip_editor._TimelineClipPanel, monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = panel.canvas
    panel._pcm = np.zeros((1, 192000), dtype=np.float32)
    panel.update_playhead(1.0)
    monkeypatch.setattr(panel._audition, "_playing", True)
    sought = []
    monkeypatch.setattr(panel._audition, "seek", sought.append)
    ruler = head._ruler_rect()
    _mouse(head, QEvent.Type.MouseButtonPress, head._time_to_x(1.0, ruler), 8)
    _mouse(head, QEvent.Type.MouseMove, head._time_to_x(1.4, ruler), 8)
    _mouse(head, QEvent.Type.MouseMove, head._time_to_x(2.5, ruler), 8)
    panel._on_audition_position(48000)
    assert head._playhead == pytest.approx(2.5)
    assert sought == []
    _mouse(head, QEvent.Type.MouseButtonRelease, head._time_to_x(2.5, ruler), 8)
    assert sought == [120000]


def test_audition_does_not_undo_manual_navigation(
    panel: project_clip_editor._TimelineClipPanel, monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel._pcm = np.zeros((1, 192000), dtype=np.float32)
    monkeypatch.setattr(panel._audition, "_playing", True)
    panel._sync_viewport(0.0, 1.0)
    panel.update_playhead(2.0)
    panel._on_audition_position(108000)
    assert (panel.canvas._view_start, panel.canvas._view_end) == (0.0, 1.0)
    panel._sync_viewport(1.5, 2.5)
    panel._on_audition_position(132000)
    assert panel.canvas._view_start < 2.75 < panel.canvas._view_end


def test_dense_waveform_keeps_peak_at_overlapping_and_repeated_bin_boundaries() -> None:
    samples = np.array([0.1, 0.3, 1.0, 0.2, 0.4])
    edges = np.array([0, 2, 2, 3, 4])
    extrema = WaveformSpectralCanvas._wave_column_extrema(samples, edges, np.maximum)
    np.testing.assert_array_equal(extrema, [1.0, 1.0, 1.0, 0.4])
