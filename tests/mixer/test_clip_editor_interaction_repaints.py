from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QImage, QMouseEvent,
    QPainter, QWheelEvent,
)
from PyQt6.QtWidgets import QApplication, QMenu, QPushButton, QScrollArea, QToolButton, QWidget
import pytest
from pytestqt.qtbot import QtBot

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.mixer.effects.panels.clip_editor import (
    WAVE_BACKGROUND, WAVE_MUTED, WAVE_PEAK, WaveformSpectralCanvas,
)
from src.features.mixer.spectrogram import DB_FLOOR, Spectrogram, analyze_spectrogram, fraction_frequency, frequency_fraction
from src.features.mixer.ui import project_clip_editor
from src.features.mixer.ui.clip_effect_rack import _REORDER_MIME
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import DARK_THEME


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


@pytest.mark.parametrize("locale", ["zh_CN", "en_US"])
def test_narrow_dock_tools_keep_controls_inside_the_viewport(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, locale: str,
) -> None:
    monkeypatch.setattr(I18N, "_translations", {})
    monkeypatch.setattr(I18N, "_fallback_translations", {})
    monkeypatch.setattr(I18N, "_module_locales_dirs", [])
    monkeypatch.setattr(I18N, "_current_locale", locale)
    I18N.load_module_translations(str(Path(project_clip_editor.__file__).resolve().parents[1] / "locales"))

    host = project_clip_editor.ProjectClipEditor()
    qtbot.addWidget(host)
    panel = host._ensure_panel()
    host._empty.hide()
    host._scroll.show()
    host.resize(880, 350)
    host.show()
    panel.noise_status_lbl.setText("底噪特征正在处理中 · " * 4)
    panel.peak_val_lbl.setText("+0.0 dBFS")
    panel.rms_val_lbl.setText("−17.2 dB")
    panel.noise_val_lbl.setText("−30.3 dB")
    panel._refresh_lufs_ui()
    panel.set_pending_timepitch((0, 4, 125.0, 1.5, True))
    panel._process_steps = [{
        "id": "gain", "label": "A long processing step " * 10, "detail": "Parameters " * 10,
    }]
    panel._rebuild_process_stack_ui()

    for scroll, controls in (
        (panel.tools_panel.widget(0),
         (panel.fo_pill, panel._fade_curve_btns["Log"], panel._timepitch_discard_button)),
        (panel.tools_panel.widget(1),
         (panel.fft_combo, panel._lufs_preset_btns[-23.0], panel.tp_ceil_pill)),
        (panel.process_stack_tab,
         (panel.stack_count_lbl, panel.btn_clear_stack, panel.stack_list_layout.itemAt(0).widget().sw)),
    ):
        panel.tools_panel.setCurrentWidget(scroll)
        QApplication.processEvents()
        assert isinstance(scroll, QScrollArea)
        assert scroll.widget().width() == scroll.viewport().width()
        assert scroll.horizontalScrollBar().maximum() == 0
        for control in controls:
            assert control.isVisible()
            scroll.ensureWidgetVisible(control, 0, 0)
            top_left = control.mapTo(scroll.viewport(), control.rect().topLeft())
            bottom_right = control.mapTo(scroll.viewport(), control.rect().bottomRight())
            assert scroll.viewport().rect().contains(top_left)
            assert scroll.viewport().rect().contains(bottom_right)

    rack = panel.effect_rack
    clip = ClipInstance(asset_id="audio", track_id="audio-track", timeline_duration=120,
                        source_duration=120, effects=tuple(
                            EffectInstance(effect_type="multiband_comp", order=index)
                            for index in range(12)))
    rack.set_clip(clip, editable=True)
    panel.tools_panel.setCurrentWidget(rack)
    QApplication.processEvents()
    rack_scroll = rack.findChild(QScrollArea)
    last_control = rack.findChildren(QToolButton, "ClipEffectRemove")[-1]
    assert rack_scroll.horizontalScrollBar().maximum() == 0
    assert rack_scroll.verticalScrollBar().maximum() > 0
    rack_scroll.ensureWidgetVisible(last_control, 0, 0)
    assert rack_scroll.viewport().rect().contains(
        last_control.mapTo(rack_scroll.viewport(), last_control.rect().bottomRight()))
    assert rack.add_button.isVisible()
    tabs = panel.tools_panel.tabBar()
    assert tabs.rect().contains(tabs.tabRect(tabs.count() - 1))

    assert host._scroll.horizontalScrollBar().maximum() == 0
    assert host._scroll.viewport().rect().contains(
        panel.btn_tools_panel.mapTo(host._scroll.viewport(), panel.btn_tools_panel.rect().bottomRight()))
    assert host._scroll.viewport().rect().contains(
        panel.output_meter.mapTo(host._scroll.viewport(), panel.output_meter.rect().bottomRight()))


def test_tools_tab_ends_remain_square_under_application_theme(
    panel: project_clip_editor._TimelineClipPanel, qapp: QApplication,
) -> None:
    previous_style = qapp.styleSheet()
    qapp.setStyleSheet(DARK_THEME)
    try:
        tabs = panel.tools_panel.tabBar()
        for selected in (0, tabs.count() - 1):
            panel.tools_panel.setCurrentIndex(selected)
            QApplication.processEvents()
            pixmap = tabs.grab()
            image = pixmap.toImage()
            dpr = pixmap.devicePixelRatio()
            ends = ((tabs.tabRect(0), 1), (tabs.tabRect(tabs.count() - 1), -1))
            for rect, step in ends:
                x = rect.left() if step == 1 else rect.right()
                for y in (rect.top(), rect.bottom()):
                    assert image.pixelColor(round(x * dpr), round(y * dpr)) == image.pixelColor(
                        round((x + step * 6) * dpr), round(y * dpr))
    finally:
        qapp.setStyleSheet(previous_style)


def test_effect_rack_scrolls_during_drag_and_leaving_cancels(
    panel: project_clip_editor._TimelineClipPanel, qtbot: QtBot,
) -> None:
    panel.resize(880, 350)
    rack = panel.effect_rack
    clip = ClipInstance(asset_id="audio", track_id="audio-track", timeline_duration=120,
                        source_duration=120, effects=tuple(
                            EffectInstance(effect_type="delay", order=index) for index in range(14)))
    rack.set_clip(clip, editable=True)
    panel.tools_panel.setCurrentWidget(rack)
    QApplication.processEvents()
    name = rack.findChildren(QPushButton, "ClipEffectName")[0]
    scroll = rack.findChild(QScrollArea)
    viewport = scroll.viewport()
    pos = viewport.mapTo(rack, QPoint(viewport.width() // 2, viewport.height() - 3))
    mime = QMimeData()
    mime.setData(_REORDER_MIME, b"1")
    args = (pos, Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    with qtbot.assertNotEmitted(rack.reorder_requested):
        for event in (QDragEnterEvent(*args), QDragMoveEvent(*args)):
            event.source = lambda: name
            QApplication.sendEvent(rack, event)
            assert event.isAccepted()
        qtbot.waitUntil(lambda: scroll.verticalScrollBar().value() > 0)
        QApplication.sendEvent(rack, QDragLeaveEvent())
        assert not rack._drag_scroll_timer.isActive()
        assert not rack._drop_line.isVisible()


def test_tools_panel_toggle_preserves_tab_canvas_selection_and_meter(
    panel: project_clip_editor._TimelineClipPanel, qtbot: QtBot,
) -> None:
    panel.tools_panel.setCurrentIndex(1)
    panel.canvas.setSelection(0.75, 2.25)
    panel._sync_viewport(0.5, 3.0)
    selection = panel.canvas.getSelection()
    viewport = (panel.canvas._view_start, panel.canvas._view_end)
    width = panel.canvas.width()
    meter = panel.output_meter.geometry()
    hide_tooltip = panel.btn_tools_panel.toolTip()

    qtbot.mouseClick(panel.btn_tools_panel, Qt.MouseButton.LeftButton)
    assert not panel.btn_tools_panel.isChecked()
    assert panel.btn_tools_panel.toolTip() != hide_tooltip
    assert panel.tools_panel.isHidden()
    assert panel.output_meter.isVisible()
    qtbot.waitUntil(lambda: panel.canvas.width() == width + panel.tools_panel.width())
    assert panel.output_meter.geometry() == meter
    assert panel.canvas.getSelection() == selection
    assert (panel.canvas._view_start, panel.canvas._view_end) == viewport

    qtbot.mouseClick(panel.btn_tools_panel, Qt.MouseButton.LeftButton)
    assert panel.tools_panel.isVisible()
    assert panel.tools_panel.currentIndex() == 1
    assert panel.btn_tools_panel.toolTip() == hide_tooltip
    qtbot.waitUntil(lambda: panel.canvas.width() == width)
    assert panel.canvas.getSelection() == selection
    assert (panel.canvas._view_start, panel.canvas._view_end) == viewport


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
    rebuilds = []
    rebuild = canvas._rebuild_static_cache

    def record_rebuild() -> None:
        rebuilds.append(True)
        rebuild()

    monkeypatch.setattr(canvas, "_rebuild_static_cache", record_rebuild)
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
    assert not rebuilds


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


def test_waveform_and_spectrum_start_with_equal_height(panel: project_clip_editor._TimelineClipPanel) -> None:
    wave, spectrum, _ = panel.canvas._wave_and_spec_rects()
    assert abs(wave.height() - spectrum.height()) <= 1.0


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
    samples = np.array([2.0, 0.1, 0.3, 1.0, 0.2, 0.4, 2.0])
    edges = np.array([1, 3, 3, 4, 5])
    extrema = WaveformSpectralCanvas._wave_column_extrema(samples, edges, np.maximum)
    np.testing.assert_array_equal(extrema, [1.0, 1.0, 1.0, 0.4])


def test_dense_waveform_draws_one_silhouette_without_a_reference_layer(
    panel: project_clip_editor._TimelineClipPanel,
) -> None:
    canvas = panel.canvas
    image = QImage(3, 41, QImage.Format.Format_RGB32)
    area = QRectF(0, 0, 3, 41)
    peak = np.array([1.0, 0.5, 0.25])
    with QPainter(image) as paint:
        canvas._draw_wave_columns(paint, area, peak, -peak, False)
    assert image.pixelColor(0, 0) == QColor(WAVE_PEAK)
    assert image.pixelColor(0, 40) == QColor(WAVE_PEAK)
    assert image.pixelColor(1, 20) == QColor(WAVE_PEAK)
    assert image.pixelColor(1, 5) == QColor(WAVE_BACKGROUND)
    assert image.pixelColor(2, 12) == QColor(WAVE_BACKGROUND)
    with QPainter(image) as paint:
        canvas._draw_wave_columns(paint, area, peak, -peak, True)
    assert image.pixelColor(1, 20) == QColor(WAVE_MUTED)


@pytest.mark.parametrize("sample_zoom", [False, True])
def test_stereo_waveform_keeps_both_channel_peaks_in_one_lane(qtbot: QtBot, sample_zoom: bool) -> None:
    canvas = WaveformSpectralCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(330, 364)
    pcm = np.array([[0.1, 0.1] * 32, [-0.6, -0.6] * 32], dtype=np.float32)
    canvas.loadAudioData(48000, 1.0, pcm.tolist(), pcm.tolist(), [])
    canvas.setSpectralVisible(False)
    if sample_zoom:
        canvas.setWaveformSamples(pcm)
    canvas.show()
    image = canvas.grab().toImage()
    wave, _, _ = canvas._wave_and_spec_rects()
    x = wave.center().x()
    y = wave.center().y() + wave.height() * 0.2
    dpr = image.devicePixelRatio()
    assert image.pixelColor(round(x * dpr), round(y * dpr)) == QColor(WAVE_PEAK)
    assert image.pixelColor(round(x * dpr), round((wave.center().y() - 35) * dpr)) == QColor(WAVE_BACKGROUND)
    assert len(canvas._channel_pills(wave)) == 2

    # The gain target selects the displayed channel; the mute badge is separate.
    canvas.setEditChannel(0)
    assert canvas._waveform_display_channels() == ((0,), False)
    canvas.setEditChannel(-1)
    right = canvas._channel_pills(wave)[1].center()
    _mouse(canvas, QEvent.Type.MouseButtonPress, right.x(), right.y())
    _mouse(canvas, QEvent.Type.MouseButtonRelease, right.x(), right.y())
    assert canvas._waveform_display_channels() == ((0,), False)


def test_live_envelope_repaints_waveform_without_rebuilding_spectrum(
    panel: project_clip_editor._TimelineClipPanel, monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel.set_source_mode(False)
    canvas = panel.canvas
    wave, _, _ = canvas._wave_and_spec_rects()
    x = wave.center().x()
    y = wave.center().y() - wave.height() * 0.17
    static_cache = canvas._static_cache
    calls: list[bool] = []
    original = canvas._rebuild_static_cache

    def record_rebuild() -> None:
        calls.append(True)
        original()

    def sample_wave() -> QColor:
        image = canvas.grab().toImage()
        ratio = image.devicePixelRatio()
        return image.pixelColor(round(x * ratio), round(y * ratio))

    monkeypatch.setattr(canvas, "_rebuild_static_cache", record_rebuild)
    assert sample_wave() == QColor(WAVE_PEAK)
    canvas._env_points = [{"t": 0.0, "db": -12.0, "interpolation": "bezier"}]
    canvas._drag_mode = "env_drag"
    canvas._refresh_processing_overlay()
    assert sample_wave() == QColor(WAVE_BACKGROUND)
    assert canvas._static_cache is static_cache
    assert not calls


def test_channel_display_menu_switches_both_panes_and_keeps_edit_view(
    panel: project_clip_editor._TimelineClipPanel, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pixels = np.array([[[220, 24, 16], [12, 80, 170]], [[5, 190, 216], [140, 96, 3]]], dtype=np.uint8)
    spectrogram = Spectrogram(pixels, 0, 192000, 192000, 2048)
    detail = Spectrogram(pixels, 48000, 144000, 192000, 2048)
    panel.load_audio_data(48000, 4.0, [[-0.4] * 64] * 2, [[0.4] * 64] * 2, [], spectrogram)
    canvas = panel.canvas
    canvas.setDetailedSpectrogram(detail)
    wave, pane, _ = canvas._wave_and_spec_rects()
    assert len(canvas._waveform_lanes(wave)) == len(canvas._spectral_bands(pane)) == 1
    combined, = canvas._spectrum_images
    combined_detail, = canvas._detail_images
    target = "splitDisplayChannels"
    seen = []

    def choose_mode(menu: QMenu, *_args: object) -> None:
        selected = next(action for action in menu.actions() if action.objectName() == target)
        assert selected.isEnabled() and not selected.isChecked()
        assert sum(action.isChecked() for action in menu.actions() if action.isCheckable()) == 1
        seen.append(target)
        selected.trigger()

    monkeypatch.setattr(QMenu, "exec", choose_mode)
    qtbot.mouseClick(canvas, Qt.MouseButton.RightButton, pos=wave.center().toPoint())
    assert len(canvas._waveform_lanes(wave)) == len(canvas._spectral_bands(pane)) == 2
    assert canvas._spectrogram is spectrogram
    assert canvas._detail_spectrogram is detail
    for y, row in enumerate(np.argmax(pixels, axis=0)):
        for x, channel in enumerate(row):
            assert combined.pixel(x, y) == canvas._spectrum_images[channel].pixel(x, y)
            assert combined_detail.pixel(x, y) == canvas._detail_images[channel].pixel(x, y)

    canvas.setSelection(1.25, 2.0, 300.0, 3500.0)
    panel._sync_viewport(1.0, 3.0)
    panel.update_playhead(1.5)
    selection = canvas.getSelection()
    target = "mergeDisplayChannels"
    qtbot.mouseClick(panel.btn_more, Qt.MouseButton.LeftButton)
    assert seen == ["splitDisplayChannels", "mergeDisplayChannels"]
    assert len(canvas._waveform_lanes(wave)) == len(canvas._spectral_bands(pane)) == 1
    assert canvas.getSelection() == selection
    assert (canvas._view_start, canvas._view_end, canvas._playhead) == (1.0, 3.0, 1.5)


def test_selection_can_start_on_playhead_and_finish_between_move_events(
    panel: project_clip_editor._TimelineClipPanel,
) -> None:
    canvas = panel.canvas
    wave, _, _ = canvas._wave_and_spec_rects()
    x = canvas._time_to_x(1.0, wave)
    y = wave.center().y()
    panel.update_playhead(1.0)
    _mouse(canvas, QEvent.Type.MouseButtonPress, x, y)
    assert canvas.is_selecting()
    _mouse(canvas, QEvent.Type.MouseButtonRelease, x + 2.0, y)
    assert canvas.getSelection()[:2] == pytest.approx((1.0, canvas._x_to_time(x + 2.0, wave)))


def test_project_envelope_editing_is_opt_in_and_releases_playhead(
    panel: project_clip_editor._TimelineClipPanel,
) -> None:
    panel.set_source_mode(False)
    panel.btn_tool_envelope.setEnabled(True)
    canvas = panel.canvas
    canvas.project_edits_enabled = True
    canvas._env_points = [{"t": 1.0, "db": 0.0}]
    commits: list[list] = []
    canvas.envelopeChanged.connect(commits.append)
    wave, _, _ = canvas._wave_and_spec_rects()
    y = canvas._env_db_to_y(0.0, wave)
    at_knot = canvas._time_to_x(1.0, wave)
    panel.update_playhead(1.0)

    _mouse(canvas, QEvent.Type.MouseButtonPress, at_knot, y)
    assert canvas.is_selecting()
    _mouse(canvas, QEvent.Type.MouseButtonRelease, at_knot, y)
    assert commits == [] and not panel.btn_tool_envelope.isChecked()

    panel.btn_tool_envelope.click()
    x = canvas._time_to_x(2.0, wave)
    _mouse(canvas, QEvent.Type.MouseButtonPress, x, y)
    _mouse(canvas, QEvent.Type.MouseButtonRelease, x, y)
    assert len(commits) == 1
    assert [point["t"] for point in commits[0]] == pytest.approx([1.0, 2.0])

    _mouse(canvas, QEvent.Type.MouseButtonPress, x, y)
    _mouse(canvas, QEvent.Type.MouseMove, x, y + 15.0)
    assert canvas._env_points[1]["db"] < 0.0
    panel._clear_selection_or_cancel_fade_curve()
    _mouse(canvas, QEvent.Type.MouseButtonRelease, x, y + 15.0)
    assert canvas._env_points[1]["db"] == 0.0 and len(commits) == 1

    panel.btn_tool_envelope.click()
    assert not panel.btn_tool_envelope.isChecked() and canvas._env_enabled
    ruler_y = canvas._ruler_rect().center().y()
    _mouse(canvas, QEvent.Type.MouseButtonPress, at_knot, ruler_y)
    _mouse(canvas, QEvent.Type.MouseMove, x, ruler_y)
    _mouse(canvas, QEvent.Type.MouseButtonRelease, x, ruler_y)
    assert canvas._playhead == pytest.approx(2.0)
    assert len(commits) == 1


def test_gain_preview_is_cached_as_the_selection_moves(panel: project_clip_editor._TimelineClipPanel) -> None:
    canvas = panel.canvas
    wave, _, _ = canvas._wave_and_spec_rects()
    canvas.setPreviewGain(6.0)
    QApplication.processEvents()
    background, gain = canvas._static_cache, canvas._gain_wave_cache
    assert gain is not None
    y = wave.center().y()
    _mouse(canvas, QEvent.Type.MouseButtonPress, canvas._time_to_x(1.0, wave), y)
    _mouse(canvas, QEvent.Type.MouseMove, canvas._time_to_x(2.0, wave), y)
    _mouse(canvas, QEvent.Type.MouseButtonRelease, canvas._time_to_x(2.5, wave), y)
    assert canvas._static_cache is background
    assert canvas._gain_wave_cache is gain


def test_selection_gain_confirm_button_fits_narrow_waveform_and_commits(qtbot: QtBot) -> None:
    canvas = WaveformSpectralCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(196, 340)
    canvas.show()
    canvas.setEditChannel(0)
    canvas.setSelection(8.0, 9.75)
    QApplication.processEvents()
    assert canvas._gain_commit_rect.isEmpty()

    # Wheel adjustment keeps a manual confirmation button.
    start = canvas._gain_hud_rect.center()
    scrolled = QWheelEvent(start, start, QPoint(0, -1440), QPoint(), Qt.MouseButton.NoButton,
                           Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
    QApplication.sendEvent(canvas, scrolled)
    QApplication.processEvents()

    wave, _, _ = canvas._wave_and_spec_rects()
    button = canvas._gain_commit_rect
    assert button.width() >= 38.0
    assert wave.contains(canvas._gain_hud_rect)
    x, y = button.left() + 4.0, button.center().y()
    _mouse(canvas, QEvent.Type.MouseMove, x, y)
    assert canvas.cursor().shape() == Qt.CursorShape.PointingHandCursor
    with qtbot.waitSignal(canvas.gainCommitRequested, timeout=1000) as commit:
        _mouse(canvas, QEvent.Type.MouseButtonPress, x, y)
        _mouse(canvas, QEvent.Type.MouseButtonRelease, x, y)
    assert commit.args == [8.0, 9.75, -6.0]
    assert canvas.getSelection()[:2] == (8.0, 9.75)


def test_spectrogram_levels_are_calibrated_and_keep_channels_separate() -> None:
    rate, nfft = 48000, 2048
    t = np.arange(rate) / rate
    left_hz, right_hz = 12 * rate / nfft, 128 * rate / nfft
    pcm = np.array([0.5 * np.sin(2 * np.pi * left_hz * t),
                    0.1 * np.sin(2 * np.pi * right_hz * t)], dtype=np.float32)
    spectrum = analyze_spectrogram(pcm, rate, nfft=nfft, columns=80)
    assert spectrum is not None
    levels = spectrum.pixels[:, :, 40]
    rows = levels.shape[1]
    for ch, hz, amp in [(0, left_hz, 0.5), (1, right_hz, 0.1)]:
        peak_row = np.argmax(levels[ch])
        target_row = rows - 1 - int(frequency_fraction(hz, rate / 2) * rows)
        assert abs(peak_row - target_row) <= 1
        level_db = DB_FLOOR * (1.0 - levels[ch, peak_row] / 255.0)
        assert level_db == pytest.approx(20 * np.log10(amp), abs=0.4)
        assert levels[1 - ch, target_row] < levels[ch, peak_row] * 0.25
        assert fraction_frequency(frequency_fraction(hz, rate / 2), rate / 2) == pytest.approx(hz)


def test_spectrogram_detail_preserves_transient_position_and_cancels() -> None:
    rate = 48000
    pcm = np.zeros((1, 3 * rate), dtype=np.float32)
    onset = int(1.25 * rate)
    pcm[0, onset] = 1.0
    overview = analyze_spectrogram(pcm, rate, nfft=512, columns=12)
    detail = analyze_spectrogram(pcm, rate, nfft=512, columns=1024, start=rate, end=2 * rate)
    assert overview is not None and detail is not None
    assert detail.samples_per_column < overview.samples_per_column / 30
    column = np.argmax(detail.pixels.max(axis=(0, 1)))
    sample = detail.start_sample + (column + 0.5) * detail.samples_per_column
    assert abs(sample - onset) < 256
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    assert analyze_spectrogram(pcm, rate, nfft=512, columns=1024, cancelled=cancelled) is None


def test_stereo_spectral_selection_anchors_to_the_pressed_channel(
    panel: project_clip_editor._TimelineClipPanel,
) -> None:
    canvas = panel.canvas
    canvas.set_channels_separate(True)
    pcm = np.zeros((2, 48000), dtype=np.float32)
    canvas.setSpectrogram(analyze_spectrogram(pcm, 48000, nfft=2048, columns=200))
    _, pane, _ = canvas._wave_and_spec_rects()
    left, right = canvas._spectral_bands(pane)
    _mouse(canvas, QEvent.Type.MouseButtonPress, canvas._time_to_x(1.0, pane), canvas._freq_to_spec_y(800, right))
    _mouse(canvas, QEvent.Type.MouseMove, canvas._time_to_x(1.5, pane), left.center().y())
    assert canvas.getSelection() == pytest.approx((1.0, 1.5, 800.0, 24000.0))
    _mouse(canvas, QEvent.Type.MouseButtonRelease, canvas._time_to_x(1.5, pane), left.center().y())


def test_zoom_detail_and_fft_switch_publish_without_replacing_waveform(
    panel: project_clip_editor._TimelineClipPanel, qtbot: QtBot,
) -> None:
    rate = 48000
    pcm = np.zeros((2, rate * 8), dtype=np.float32)
    panel._duration = 8.0
    panel.attach_pcm(pcm, rate)
    panel._refresh_stack_preview()
    try:
        qtbot.waitUntil(lambda: panel.canvas._spectrogram is not None, timeout=4000)
        canvas = panel.canvas
        initial = canvas._spectrogram
        assert initial is not None
        panel._sync_viewport(2.0, 3.5)
        canvas.setSelection(2.1, 2.9)
        qtbot.waitUntil(lambda: canvas._detail_spectrogram is not None, timeout=4000)
        assert canvas._detail_spectrogram.samples_per_column < initial.samples_per_column
        background = canvas._static_cache
        panel.fft_combo.setCurrentIndex(0)
        assert canvas._spectrogram is initial
        qtbot.waitUntil(lambda: canvas._spectrogram.nfft == 512, timeout=4000)
        assert canvas._static_cache is background
        assert canvas.getSelection()[:2] == pytest.approx((2.1, 2.9))
        assert canvas._waveform_samples is panel._pcm
        panel.detach_pcm()
        assert canvas._spectrogram is None and canvas._detail_spectrogram is None
    finally:
        panel.shutdown_preview()
