"""Show source waveforms and timeline clips in the professional audio editor."""
from __future__ import annotations

import hashlib
import math
import os
from typing import TYPE_CHECKING

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QHideEvent, QKeySequence, QMouseEvent, QResizeEvent, QShortcut, QWheelEvent
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from src.features.edit.core.audio_sync import binding_for_asset, bound_audio_frame, resolve_audio_source
from src.features.edit.core.audio_waveform import (
    WaveformData, WaveformExtractor, get_cached_waveform, is_waveform_pending,
)
from src.features.edit.core.clip_pcm import (
    ClipPcmProvider, ClipPcmResult, compute_spectrogram, compute_waveform_stats,
    find_zero_crossing, resolve_asset_audio_spec, resolve_clip_audio_spec,
)
from src.features.edit.core.sample_time import AUDIO_REFERENCE_SAMPLE_RATE, TimelineSampleClock
from src.features.mixer.effects.panels.clip_editor import StudioAudioClipEditor
from src.shared.i18n.i18n import I18N

from .clip_audition import ClipAuditionSession
from .workspace_style import PANEL_QSS

if TYPE_CHECKING:
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_v2 import ClipInstance, TimelineSequence


_FADE_CURVES = {"Linear": "linear", "S-Curve": "smooth", "Log": "equal_power"}
_DISPLAY_FADE_CURVES = {"linear": "Linear", "smooth": "S-Curve", "equal_power": "Equal Power"}


class _HeaderLabel(QLabel):
    """Keep long filenames and status messages on one accessible line."""

    def __init__(self, elide: Qt.TextElideMode) -> None:
        super().__init__()
        self._full_text = ""
        self._elide = elide
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setAccessibleName(text)
        self.setToolTip(text)
        self._refresh_text()
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self._full_text) + 2, metrics.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_text()

    def _refresh_text(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full_text, self._elide, self.width()))


class _TimelineClipPanel(StudioAudioClipEditor):
    gain_requested = pyqtSignal(float)

    def __init__(self) -> None:
        super().__init__()
        # The inspector already scrolls independently. Keep the waveform in
        # view when the editor shares a short dock with the audio timeline.
        self.setMinimumHeight(0)
        self.canvas.setMinimumHeight(142)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._play_timer.stop()
        self._fade_editable = False
        self._pending_fade: tuple[float, float] | None = None
        self.canvas.installEventFilter(self)
        self.canvas._env_enabled = False
        self.canvas._magnetic_snap = False
        self.canvas.previewGainChanged.disconnect(self._on_preview_gain_changed)
        self.gain_knob._min = -48.0
        self.gain_knob._max = 12.0
        self.gain_knob.setToolTip(I18N.tr("mixer.clip_editor.gain_range", "Clip gain (−48 to +12 dB)"))
        self.gain_pill.setToolTip(self.gain_knob.toolTip())
        self.gain_knob.valueCommitted.connect(self._request_gain)
        self._disable_prototype_controls()
        self._fade_curve_btns["Log"].setText(I18N.tr("mixer.clip_editor.equal_power", "Equal power"))
        # P0-2 §2.3：PCM 试听会话——播放、循环、跳选区、电平表共用一个
        # 设备会话；设备失效/离开时自动停止并释放。
        self._pcm: np.ndarray | None = None
        self._pcm_rate = AUDIO_REFERENCE_SAMPLE_RATE
        self._audition = ClipAuditionSession(self)
        self._audition.position_changed.connect(self._on_audition_position)
        self._audition.playing_changed.connect(self._on_audition_playing_changed)
        self._zero_snap_armed = False
        self._suppress_zero_snap = False
        self.canvas.selectionChanged.connect(self._on_pcm_selection_zero_snap)
        # 原型的磁性吸附（播放头/标记）与 PCM 过零吸附互补，两者都随开关。
        self.btn_snap_zero.toggled.connect(self._on_zero_snap_toggled)

    def _on_zero_snap_toggled(self, enabled: bool) -> None:
        self._zero_snap_armed = enabled

    def _init_demo_audio(self) -> None:
        pass

    def _setup_shortcuts(self) -> None:
        for key, callback in (
            (QKeySequence.StandardKey.SelectAll, self.canvas.selectAll),
            (Qt.Key.Key_Escape, self.canvas.clearSelection),
            (Qt.Key.Key_E, self._zoom_to_selection),
            (Qt.Key.Key_Space, self._toggle_play),
            (QKeySequence("Ctrl+Space"), self._toggle_skip_play),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)

    # ------------------------------------------------------------------
    # PCM 试听（P0-2 §2.3）
    # ------------------------------------------------------------------
    def attach_pcm(self, pcm: np.ndarray, sample_rate: int) -> None:
        """装载真实 PCM：启用播放/循环/频谱/过零吸附并复位播放状态。"""
        self._pcm = pcm
        self._pcm_rate = int(sample_rate) if sample_rate > 0 else AUDIO_REFERENCE_SAMPLE_RATE
        self._audition.load(pcm, sample_rate)
        self._is_playing = False
        self._skip_selection_mode = False
        self.btn_play.setChecked(False)
        self.btn_play.setText("▶ 播放")
        for widget in (self.btn_play, self.btn_loop, self.btn_snap_zero,
                       self.btn_spectral, self.fft_combo):
            widget.setEnabled(True)
            widget.setToolTip("")
        self.btn_play.setToolTip(I18N.tr("mixer.clip_editor.play_audition", "Audition this clip (Space)"))
        self.btn_loop.setToolTip(I18N.tr("mixer.clip_editor.loop_audition", "Loop the selection, or the whole clip"))

    def detach_pcm(self) -> None:
        """释放试听（重开/重连/隐藏时调用）；播放控件回到禁用态。"""
        self._audition.stop()
        self._pcm = None
        self._is_playing = False
        self._skip_selection_mode = False

    def current_fft_size(self) -> int:
        try:
            return int(str(self.fft_combo.currentText()).split()[0])
        except (ValueError, IndexError):
            return 2048

    def _audition_start_sample(self) -> int:
        rate = self._pcm_rate
        head = max(0.0, min(self._duration, self._playhead))
        return int(round(head * rate))

    def _toggle_play(self) -> None:
        if self._pcm is None:
            return
        if self._audition.is_playing:
            self._audition.stop()
            return
        t0, t1, _, _ = self.canvas.getSelection()
        start = self._audition_start_sample()
        rate = self._pcm_rate
        if t1 - t0 > 0.01:
            sel0, sel1 = int(round(t0 * rate)), int(round(t1 * rate))
            self._audition.set_loop(self._is_looping, sel0, sel1)
            if not sel0 <= start < sel1:
                start = sel0
        else:
            self._audition.set_loop(self._is_looping, 0, self._pcm.shape[1])
        if not self._audition.play(start):
            self.info_label.setText(I18N.tr(
                "mixer.clip_editor.no_audio_device", "No audio device available for audition"))

    def _stop_playback(self) -> None:
        # 停止回到开始试听的游标（会话负责回到 return_sample）。
        self._skip_selection_mode = False
        self._audition.stop()
        self._is_playing = False

    def _toggle_skip_play(self) -> None:
        """跳过选区试听 (Ctrl+Space)：从选区前 1.2 s 起播，越过 [t0, t1)。"""
        if self._pcm is None:
            return
        t0, t1, _, _ = self.canvas.getSelection()
        if t1 - t0 <= 0.01:
            self._toggle_play()
            return
        rate = self._pcm_rate
        self._skip_selection_mode = True
        start = max(0, int(round((t0 - 1.2) * rate)))
        self._audition.set_loop(False)
        if not self._audition.play(start):
            self._skip_selection_mode = False

    def _on_audition_playing_changed(self, playing: bool) -> None:
        self._is_playing = playing
        self.btn_play.setChecked(playing)
        self.btn_play.setText("❚❚ 暂停" if playing else "▶ 播放")
        self.playStateChanged.emit(playing)
        if not playing:
            self._skip_selection_mode = False
            if self._pcm is not None:
                self.update_playhead(self._audition.return_sample() / self._pcm_rate)
            self.update_output_meter(-60.0, -60.0)

    def _on_audition_position(self, sample: int) -> None:
        if not self._audition.is_playing or self._pcm is None:
            return
        rate = self._pcm_rate
        sec = sample / rate
        if self._skip_selection_mode:
            t0, t1, _, _ = self.canvas.getSelection()
            if t1 - t0 > 0.01:
                s0, s1 = int(round(t0 * rate)), int(round(t1 * rate))
                if s0 <= sample < s1:
                    self._audition.seek(s1)
                    return
                if sample >= min(self._pcm.shape[1], int(round((t1 + 1.2) * rate))):
                    self._audition.stop()
                    return
        # 播放头超出右视口时平滑翻页跟随（与原型一致）。
        span = self.canvas._view_end - self.canvas._view_start
        if span < self._duration and sec > self.canvas._view_end:
            new_start = min(self._duration - span, sec - span * 0.15)
            self._sync_viewport(max(0.0, new_start), max(0.0, new_start) + span)
        self.update_playhead(sec)
        self._update_pcm_meters(sample)

    def _update_pcm_meters(self, sample: int) -> None:
        if self._pcm is None or self._pcm.shape[1] == 0:
            return
        half = max(1, int(round(0.012 * self._pcm_rate)))
        lo = max(0, sample - half)
        hi = min(self._pcm.shape[1], sample + half)
        window = self._pcm[:, lo:hi]
        levels = []
        for ch in (0, min(1, window.shape[0] - 1)):
            if ch >= window.shape[0] or self.canvas._ch_muted[min(ch, len(self.canvas._ch_muted) - 1)]:
                levels.append(-60.0)
                continue
            peak = float(np.max(np.abs(window[ch]))) if window.shape[1] else 0.0
            levels.append(20.0 * math.log10(max(1e-5, peak)))
        left = levels[0]
        right = levels[1] if len(levels) > 1 else left
        self.update_output_meter(left, right)

    def _on_canvas_playhead_moved(self, sec: float) -> None:
        super()._on_canvas_playhead_moved(sec)
        if self._audition.is_playing:
            self._audition.seek(int(round(sec * self._pcm_rate)))

    def _on_minimap_playhead_moved(self, sec: float) -> None:
        super()._on_minimap_playhead_moved(sec)
        if self._audition.is_playing:
            self._audition.seek(int(round(sec * self._pcm_rate)))

    # ------------------------------------------------------------------
    # 过零吸附（P0-2 §2.4）
    # ------------------------------------------------------------------
    def _on_pcm_selection_zero_snap(self, t0: float, t1: float, _fl: float, _fh: float) -> None:
        if (not self._zero_snap_armed or self._pcm is None
                or self._suppress_zero_snap or t1 - t0 <= 0.0):
            return
        rate = self._pcm_rate
        total = self._pcm.shape[1]
        tol = max(1, int(round(0.003 * rate)))
        s0 = max(0, min(total - 1, int(round(t0 * rate))))
        s1 = max(s0 + 1, min(total, int(round(t1 * rate))))
        n0 = find_zero_crossing(self._pcm, s0, tolerance_samples=tol, bounds=(0, total))
        n1 = find_zero_crossing(self._pcm, s1 - 1, tolerance_samples=tol, bounds=(0, total))
        snapped0 = n0 / rate if n0 is not None else t0
        snapped1 = n1 / rate if n1 is not None else t1
        if (abs(snapped0 - t0) > 1e-9 or abs(snapped1 - t1) > 1e-9) and snapped1 - snapped0 > 1e-9:
            self._suppress_zero_snap = True
            try:
                self.canvas.setSelection(snapped0, snapped1)
            finally:
                self._suppress_zero_snap = False

    def _disable_prototype_controls(self) -> None:
        unavailable = I18N.tr("mixer.clip_editor.unavailable", "This tool requires an audio processing backend.")
        self.btn_spectral.setChecked(False)
        self.btn_snap_zero.setChecked(False)
        self.btn_apply_lufs.setText(I18N.tr("mixer.clip_editor.match_loudness", "Match target loudness"))
        buttons_by_text = {
            "■", "静音", "裁剪", "删除", "标准化", "修复", "防爆20m", "应用时间与音高",
            "应用至选区", "重置", "人声", "流媒体", "广播", "电影",
        }
        widgets = (
            self.btn_play, self.btn_loop, self.btn_tool_brush, self.btn_tool_envelope,
            self.btn_snap_zero, self.btn_spectral, self.btn_noise_print, self.btn_denoise,
            self.btn_bypass, self.btn_env_smooth, self.btn_env_reset, self.btn_apply_lufs,
            self.btn_clear_stack, self.spec_boost_pill, self.spec_boost_knob, self.denoise_pill,
            self.denoise_knob, self.fft_combo, self.preserve_pitch_sw, self.stretch_ratio_pill, self.target_dur_pill,
            self.stretch_knob, self.pitch_pill, self.pitch_knob, self.stretch_algo_combo,
            self.target_lufs_pill, self.tp_ceil_pill, *self._edit_ch_btns.values(),
            *self._lufs_preset_btns.values(),
        )
        for widget in widgets:
            widget.setEnabled(False)
            widget.setToolTip(unavailable)
        self._fade_presets: list[QPushButton] = []
        self._apply_gain_button: QPushButton | None = None
        self._history_buttons: list[QPushButton] = []
        self._return_button: QPushButton | None = None
        for button in self.findChildren(QPushButton):
            if button.text() in buttons_by_text:
                button.setEnabled(False)
                button.setToolTip(unavailable)
            elif button.text() == "应用增益到选区":
                self._apply_gain_button = button
                button.setText(I18N.tr("mixer.clip_editor.apply_gain", "Apply clip gain"))
                button.setToolTip(self.gain_knob.toolTip())
            elif button.text() == "归零":
                self._fade_presets.append(button)
            elif button.text() in {"↶", "↷"}:
                self._history_buttons.append(button)
            elif button.objectName() == "PrimaryBtn":
                self._return_button = button
            if not button.isEnabled():
                button.setStyleSheet("color: #797981; background-color: #29292D; border-color: #323237;")

        self._mode_label: QLabel | None = None
        for label in self.findChildren(QLabel):
            if label.text() == "● DSP READY":
                self._mode_label = label
            elif label.text() == "滚轮缩放 · Alt+滚轮放大波形 · 右键声道独奏/反相":
                label.setText(I18N.tr("mixer.clip_editor.waveform_tip", "Scroll to zoom · Alt+scroll to scale waveform"))
            elif label.text() == "⌨ 快捷键":
                label.setToolTip(I18N.tr("mixer.clip_editor.keys", "Ctrl+A selects all · E zooms to selection · Esc clears selection"))
        for label in (self.peak_val_lbl, self.rms_val_lbl, self.noise_val_lbl, self.lufs_val_lbl, self.true_peak_lbl):
            label.setText("—")
        self.peak_val_lbl.setToolTip(I18N.tr("mixer.clip_editor.source_peak", "Estimated peak from the source waveform"))
        self.update_output_meter(-60.0, -60.0)

    def set_source_mode(self, source_mode: bool) -> None:
        if self._mode_label is not None:
            self._mode_label.setText(
                I18N.tr("mixer.clip_editor.source_mode", "● Source audio")
                if source_mode else I18N.tr("mixer.clip_editor.timeline_mode", "● Timeline clip")
            )
        for button in self._history_buttons:
            button.setEnabled(not source_mode)
        if self._return_button is not None:
            self._return_button.setText(
                I18N.tr("mixer.clip_editor.close", "Close")
                if source_mode else I18N.tr("mixer.clip_editor.back", "Return to timeline")
            )

    def sync_gain_fade(
        self, gain_db: float, fade_in: float, fade_out: float,
        fade_in_curve: str, fade_out_curve: str, *, locked: bool,
    ) -> None:
        self.gain_knob.setValue(gain_db)
        self.gain_pill.setText(f"{gain_db:+.1f} dB")
        self.gain_knob.setEnabled(not locked)
        self.gain_pill.setEnabled(not locked)
        if self._apply_gain_button is not None:
            self._apply_gain_button.setEnabled(not locked)
        self._fade_editable = not locked
        for control in (self.fi_pill, self.fo_pill, *self._fade_presets, *self._fade_curve_btns.values()):
            control.setEnabled(self._fade_editable)
        self.canvas._fade_in_sec = fade_in
        self.canvas._fade_out_sec = fade_out
        self.canvas.setFadeCurves(_DISPLAY_FADE_CURVES[fade_in_curve], _DISPLAY_FADE_CURVES[fade_out_curve])
        for name, button in self._fade_curve_btns.items():
            button.setChecked(fade_in_curve == fade_out_curve == _FADE_CURVES[name])
        self._update_fade_display(fade_in, fade_out)

    def _on_fade_curve_selected(self, curve_name: str) -> None:
        if self._fade_editable:
            self.fadeCurveChanged.emit(_FADE_CURVES[curve_name])

    def _request_gain(self, value: float) -> None:
        if self.gain_knob.isEnabled() and math.isfinite(value):
            self.gain_requested.emit(round(value, 1))

    def _on_gain_knob_changed(self, gain_db: float) -> None:
        self.gain_pill.setText(f"{gain_db:+.1f} dB")

    def _on_gain_pill_edited(self, text: str) -> None:
        try:
            value = float(text.lower().removesuffix("db").strip())
        except ValueError:
            return
        if math.isfinite(value):
            self.gain_knob.setValue(value, emit=True)
            self._request_gain(self.gain_knob.value())

    def _on_gain_committed(self, t0: float, t1: float, gain_db: float) -> None:
        # The toolbar knob sets the whole clip value. The selection HUD
        # operates in relative dB and has no timeline equivalent.
        if self.sender() is not self.canvas:
            self._request_gain(gain_db)

    def _update_fade_display(self, fade_in: float, fade_out: float) -> None:
        self.fi_pill.setText(f"{fade_in:.3f}")
        self.fo_pill.setText(f"{fade_out:.3f}")
        self.fade_preview.setState(self.canvas._fade_curve_type, fade_in, fade_out,
                                  out_curve=self.canvas._fade_out_curve_type)

    def _on_fade_changed(self, fi: float, fo: float) -> None:
        if not self._fade_editable:
            return
        self._update_fade_display(fi, fo)
        if self.canvas._drag_mode in {"fade_in", "fade_out"}:
            self._pending_fade = (fi, fo)
        else:
            self.fadeApplied.emit(fi, fo)

    def _on_fi_edited(self, text: str) -> None:
        self._commit_fade_text(text, fade_in=True)

    def _on_fo_edited(self, text: str) -> None:
        self._commit_fade_text(text, fade_in=False)

    def _commit_fade_text(self, text: str, *, fade_in: bool) -> None:
        fi, fo = self.canvas._fade_in_sec, self.canvas._fade_out_sec
        try:
            seconds = float(text.strip().lower().removesuffix("s"))
        except ValueError:
            seconds = math.nan
        if not self._fade_editable or not math.isfinite(seconds):
            self._update_fade_display(fi, fo)
            return
        if fade_in:
            fi = max(0.0, min(self._duration - fo, seconds))
        else:
            fo = max(0.0, min(self._duration - fi, seconds))
        self._on_fade_changed(fi, fo)

    def _apply_symmetric_fade(self, seconds: float) -> None:
        if self._fade_editable:
            super()._apply_symmetric_fade(seconds)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.canvas and isinstance(event, QWheelEvent):
            if self.canvas._gain_hud_rect.contains(event.position()):
                return True
        if watched is self.canvas and isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.RightButton:
                    return True
                if event.button() == Qt.MouseButton.LeftButton:
                    pos = event.position()
                    if (pos.x() < self.canvas._ml or self.canvas._gain_hud_rect.contains(pos)
                            or self.canvas._gain_commit_rect.contains(pos)):
                        return True
                    if not self._fade_editable:
                        wave, _, _ = self.canvas._wave_and_spec_rects()
                        fade_in_x = max(self.canvas._time_to_x(self.canvas._fade_in_sec, wave), wave.left() + 6.0)
                        fade_out_x = min(self.canvas._time_to_x(self._duration - self.canvas._fade_out_sec, wave), wave.right() - 6.0)
                        near_handle = min(abs(pos.x() - fade_in_x), abs(pos.x() - fade_out_x)) <= 8.0
                        if near_handle and abs(pos.y() - (wave.top() + 8.0)) <= 9.0:
                            return True
            if event.type() == QEvent.Type.MouseButtonRelease and self._pending_fade is not None:
                fade, self._pending_fade = self._pending_fade, None
                self.fadeApplied.emit(*fade)
        return super().eventFilter(watched, event)


class ProjectClipEditor(QWidget):
    """A dock body that opens an audio source or a clip from the active sequence."""

    gain_committed = pyqtSignal(str, float)
    fade_committed = pyqtSignal(str, int, int)
    sample_fade_committed = pyqtSignal(str, int, int)
    fade_curve_committed = pyqtSignal(str, str)
    # P0-2：试听占用输出设备时通知宿主暂停时间线播放。
    audition_active_changed = pyqtSignal(bool)
    close_requested = pyqtSignal()
    float_requested = pyqtSignal()
    tab_requested = pyqtSignal()
    timeline_requested = pyqtSignal()
    undo_requested = pyqtSignal()
    redo_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MixerClipEditorHost")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_QSS + """
            #ClipEditorHeader { background: #25252A; border-bottom: 1px solid #17171A; }
            #ClipEditorHeader QLabel { background: transparent; }
            #ClipEditorTitle { color: #E2E2E8; font-weight: 600; }
            #ClipEditorStatus, #ClipEditorEmpty { color: #AAABB5; }
            #ClipEditorHeader QToolButton {
                background: #2E2F35; border: 1px solid #3C3D45; border-radius: 4px;
                color: #CBCCD3; padding: 2px 6px;
            }
            #ClipEditorHeader QToolButton:hover { background: #3A3B43; color: #F2F2F5; }
        """)
        self._clip: ClipInstance | None = None
        self._timeline: TimelineSequence | None = None
        self._timeline_id: str | None = None
        self._project: EditProject | None = None
        self._project_id: str | None = None
        self._panel: _TimelineClipPanel | None = None
        self._extractor: WaveformExtractor | None = None
        # P0-2：PCM 取样的代际标识与最近一次结果（频谱重算复用）。
        self._pcm_provider: ClipPcmProvider | None = None
        self._pcm_spec_key: tuple | None = None
        self._last_pcm: ClipPcmResult | None = None
        self._pcm_applied = False
        self._asset: MediaAsset | None = None
        self._waveform_asset: MediaAsset | None = None
        self._waveform_asset_id: str | None = None
        self._source_changed = False
        self._waveform_path = ""
        self._offset_seconds = 0.0
        self._fps = 24.0
        self._sample_rate = AUDIO_REFERENCE_SAMPLE_RATE
        self._locked = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QWidget()
        header.setObjectName("ClipEditorHeader")
        header.setFixedHeight(30)
        toolbar = QHBoxLayout(header)
        toolbar.setContentsMargins(8, 2, 6, 2)
        toolbar.setSpacing(6)
        self._title = _HeaderLabel(Qt.TextElideMode.ElideMiddle)
        self._title.setObjectName("ClipEditorTitle")
        self._title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._title.setMaximumWidth(300)
        toolbar.addWidget(self._title)
        self._status = _HeaderLabel(Qt.TextElideMode.ElideRight)
        self._status.setObjectName("ClipEditorStatus")
        toolbar.addWidget(self._status, 1)
        self._header_actions: dict[str, QToolButton] = {}
        for key, text, signal in (
            ("float", "Float", self.float_requested),
            ("tab", "Dock with timeline", self.tab_requested),
            ("timeline", "Timeline", self.timeline_requested),
            ("close", "Close", self.close_requested),
        ):
            button = QToolButton()
            button.setText(I18N.tr(f"mixer.clip_editor.{key}", text))
            button.clicked.connect(signal.emit)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._header_actions[key] = button
            toolbar.addWidget(button)
        layout.addWidget(header)
        self._empty = QLabel(I18N.tr(
            "mixer.clip_editor.empty", "Double-click an audio clip in the timeline to edit it.",
        ))
        self._empty.setObjectName("ClipEditorEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty, 1)
        self._scroll = QScrollArea()
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        layout.addWidget(self._scroll, 1)
        self.reset()

    @property
    def clip_id(self) -> str | None:
        return self._clip.clip_id if self._clip is not None else None

    def set_dock_mode(self, *, floating: bool, tabbed: bool) -> None:
        self._header_actions["float"].setVisible(not floating)
        self._header_actions["tab"].setVisible(not tabbed)
        self._header_actions["close"].setVisible(not floating)

    def focus_waveform(self) -> None:
        if self._panel is not None and not self._scroll.isHidden():
            self._panel.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _ensure_panel(self) -> _TimelineClipPanel:
        if self._panel is None:
            self._panel = _TimelineClipPanel()
            self._panel.gain_requested.connect(self._on_gain_requested)
            self._panel.fadeApplied.connect(self._on_fade_requested)
            self._panel.fadeCurveChanged.connect(self._on_fade_curve_requested)
            self._panel.undoRequested.connect(self._on_undo_requested)
            self._panel.redoRequested.connect(self._on_redo_requested)
            self._panel.commitAndCloseRequested.connect(self._return_to_timeline)
            self._panel.fftSizeChanged.connect(self._on_fft_size_changed)
            self._panel._audition.playing_changed.connect(self.audition_active_changed.emit)
            self._scroll.setWidget(self._panel)
        return self._panel

    def open_clip(
        self, timeline: TimelineSequence, clip: ClipInstance, asset: MediaAsset, *,
        waveform_asset_id: str | None = None, project: EditProject | None = None,
    ) -> None:
        """Open an audio clip by model identity and request its source envelope."""
        if (self.clip_id == clip.clip_id and self._timeline_id == timeline.timeline_id
                and self._asset is not None and self._asset.asset_id == asset.asset_id
                and self._project_id == (project.project_id if project is not None else None)):
            if project is not None:
                self.sync_project(project)
            self.sync_clip(timeline)
            return
        self._clip = clip
        self._timeline = timeline
        self._timeline_id = timeline.timeline_id
        self._prepare_source(asset, project, waveform_asset_id)
        self._source_changed = True
        self.sync_clip(timeline)

    def open_source(self, asset: MediaAsset, *, project: EditProject | None = None) -> None:
        """Inspect an audio asset without creating a timeline clip."""
        if not asset.is_audio:
            raise ValueError("The source must be an audio asset")
        self._clip = None
        self._timeline = None
        self._timeline_id = None
        self._sample_rate = AUDIO_REFERENCE_SAMPLE_RATE
        self._prepare_source(asset, project)
        self._request_waveform()
        self._sync_gain_fade()

    def _prepare_source(
        self, asset: MediaAsset, project: EditProject | None, waveform_asset_id: str | None = None,
    ) -> None:
        panel = self._ensure_panel()
        panel.set_source_mode(self._clip is None)
        panel._pending_fade = None
        panel.canvas._drag_mode = ""
        panel.canvas._physics_timer.stop()
        if self._extractor is None:
            self._extractor = WaveformExtractor.instance()
            self._extractor.waveform_ready.connect(self._waveform_ready)
            self._extractor.extraction_failed.connect(self._waveform_failed)
        if self._pcm_provider is None:
            self._pcm_provider = ClipPcmProvider.instance()
        self._project = project
        self._project_id = project.project_id if project is not None else None
        self._waveform_asset_id = None
        self._set_source(asset, project, waveform_asset_id)
        self._empty.hide()
        self._scroll.show()
        self._title.setText(asset.display_name)
        self._title.show()

    def _set_source(
        self, asset: MediaAsset, project: EditProject | None, waveform_asset_id: str | None = None,
    ) -> bool:
        path, offset = resolve_audio_source(project, asset)
        path_changed = path != self._waveform_path
        source_changed = (
            path_changed or offset != self._offset_seconds
            or (self._asset is not None and self._asset.fps != asset.fps)
        )
        self._asset = asset
        self._waveform_path, self._offset_seconds = path, offset
        parent_id = asset.metadata.get("parent_asset_id") if asset.metadata.get("is_subclip") else None
        asset_id = waveform_asset_id or str(parent_id or asset.asset_id)
        self._waveform_asset = asset
        if project is not None and path != asset.path:
            binding = binding_for_asset(project.audio_sync_bindings, asset.asset_id)
            if binding is not None:
                self._waveform_asset = project.assets.get(binding.wav_asset_id)
                asset_id = binding.wav_asset_id
        if self._waveform_asset_id is None:
            self._waveform_asset_id = asset_id
        elif path_changed:
            self._waveform_asset_id = self._path_cache_id(asset_id)
        return source_changed

    def sync_project(self, project: EditProject | None) -> None:
        """Refresh audio provenance when the parent project changes its media."""
        if self._asset is None:
            return
        if project is None or (self._project_id is not None and project.project_id != self._project_id):
            self.reset()
            return
        asset = project.assets.get(self._asset.asset_id)
        if asset is None or (self._clip is None and not asset.is_audio):
            self.reset()
            return
        self._project = project
        source_changed = self._set_source(asset, project)
        self._title.setText(asset.display_name)
        if source_changed:
            self._request_waveform()
            self._sync_gain_fade()

    def sync_clip(self, timeline: TimelineSequence | None) -> None:
        """Refresh from the live sequence after an edit, undo, or selection change."""
        if self._clip is None or self._panel is None:
            return
        clip = timeline.get_clip_by_id(self._clip.clip_id) if (
            timeline is not None and timeline.timeline_id == self._timeline_id
        ) else None
        track = timeline.get_track_by_id(clip.track_id) if clip is not None else None
        if (track is None or track.track_type != "audio" or self._asset is None
                or clip.asset_id != self._asset.asset_id):
            self.reset()
            return
        if timeline is not None:
            self._timeline = timeline
        previous = self._clip
        old_fps, self._fps = self._fps, float(timeline.fps)
        self._sample_rate = timeline.sample_rate
        self._clip = clip
        self._locked = track.is_locked
        geometry_changed = (
            self._source_changed
            or old_fps != self._fps
            or (previous.source_start, previous.source_duration, previous.timeline_duration,
                previous.source_sample_offset, previous.is_reverse_playback()) != (
                clip.source_start, clip.source_duration, clip.timeline_duration,
                clip.source_sample_offset, clip.is_reverse_playback(),
            )
        )
        if geometry_changed:
            self._source_changed = False
            self._request_waveform()
        self._sync_gain_fade()

    def reset(self) -> None:
        self._clip = None
        self._timeline = None
        self._timeline_id = None
        self._project = None
        self._project_id = None
        self._asset = None
        self._waveform_asset = None
        self._waveform_asset_id = None
        self._source_changed = False
        self._pcm_spec_key = None
        self._last_pcm = None
        self._pcm_applied = False
        if self._panel is not None:
            self._panel._pending_fade = None
            self._panel.canvas._drag_mode = ""
            self._panel.canvas._physics_timer.stop()
            self._panel.detach_pcm()
        self._scroll.hide()
        self._title.setText("")
        self._title.hide()
        self._status.setText("")
        self._empty.show()

    def _return_to_timeline(self) -> None:
        if self._clip is None:
            self.close_requested.emit()
        else:
            self.timeline_requested.emit()

    def _on_undo_requested(self) -> None:
        if self._clip is not None:
            self.undo_requested.emit()

    def _on_redo_requested(self) -> None:
        if self._clip is not None:
            self.redo_requested.emit()

    def hideEvent(self, event: QHideEvent) -> None:
        if self._panel is not None:
            self._panel._pending_fade = None
            self._panel.canvas._physics_timer.stop()
            self._panel._audition.stop()
            self._sync_gain_fade()
        super().hideEvent(event)

    def _request_waveform(self) -> None:
        asset = self._asset
        if asset is None or self._panel is None:
            return
        self._pcm_applied = False
        duration = self._clip.timeline_duration / self._fps if self._clip is not None else asset.duration_seconds
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0.0:
            duration = 0.01
        self._panel.load_audio_data(self._sample_rate, duration, [], [], [], clip_name=asset.display_name)
        self._panel.update_playhead(0.0)
        self._panel.peak_val_lbl.setText("—")
        self._panel.sr_badge.setText("—")
        if not (self._waveform_asset_id and self._waveform_path):
            self._status.setText(I18N.tr("mixer.clip_editor.no_waveform", "Source waveform unavailable."))
            return
        waveform = self._cached_source_waveform()
        if waveform is not None and waveform.is_complete:
            self._apply_waveform(waveform)
        else:
            self._status.setText(I18N.tr("mixer.clip_editor.loading_waveform", "Loading source waveform…"))
            if self._extractor is not None:
                self._extractor.request_waveform(self._waveform_asset_id, self._waveform_path)
        # P0-2：真实 PCM（波形/RMS/频谱/试听共用同一数据源）。
        self._request_pcm()

    def _request_pcm(self) -> None:
        asset, panel = self._asset, self._panel
        if asset is None or panel is None or self._pcm_provider is None:
            return
        clip = self._clip
        if clip is not None and self._timeline is not None:
            source_asset = self._project.assets.get(clip.asset_id) if self._project is not None else None
            spec = resolve_clip_audio_spec(
                self._project, self._timeline, clip, source_asset or asset)
        else:
            spec = resolve_asset_audio_spec(self._project, asset)
        if spec is None:
            return
        self._pcm_spec_key = spec.cache_key()

        def on_pcm(result: ClipPcmResult) -> None:
            # 结果返回前校验代际：切片段/重连/撤销后旧结果不得覆盖新窗口。
            if result.spec_key == self._pcm_spec_key:
                self._apply_pcm(result)

        self._pcm_provider.request(spec, on_pcm)

    def _apply_pcm(self, result: ClipPcmResult) -> None:
        asset, panel = self._asset, self._panel
        if asset is None or panel is None:
            return
        self._last_pcm = result
        self._pcm_applied = True
        pcm, rate = result.pcm, result.sample_rate
        duration = (
            self._clip.timeline_duration / self._fps
            if self._clip is not None and self._fps > 0
            else pcm.shape[1] / rate
        )
        duration = max(0.01, duration)
        bins = min(8192, max(64, pcm.shape[1] // 64))
        mins, maxs, rmss = compute_waveform_stats(pcm, bins)
        spectrogram = compute_spectrogram(
            pcm, rate, nfft=panel.current_fft_size(), time_bins=320, freq_bins=96)
        report = self._measure_cleanliness(pcm, rate)
        panel.attach_pcm(pcm, rate)
        panel.load_audio_data(
            rate, duration, mins, maxs, rmss, spectrogram,
            clip_name=asset.display_name, cleanliness_report=report,
        )
        panel.update_playhead(0.0)
        peak_db = report.get("peak_db")
        panel.peak_val_lbl.setText(
            f"{peak_db:+.1f} dBFS" if isinstance(peak_db, (int, float)) else "—")
        self._sync_gain_fade()

    def _on_fft_size_changed(self, _size: int) -> None:
        if self._last_pcm is not None and self._panel is not None and self._pcm_applied:
            # 只重算频谱矩阵，波形包络与读数保持不变。
            spectrogram = compute_spectrogram(
                self._last_pcm.pcm, self._last_pcm.sample_rate,
                nfft=self._panel.current_fft_size(), time_bins=320, freq_bins=96)
            self._panel.canvas.loadAudioData(
                self._panel._sample_rate, self._panel._duration,
                self._panel.canvas._peaks_min, self._panel.canvas._peaks_max,
                self._panel.canvas._rms, spectrogram)

    @staticmethod
    def _measure_cleanliness(pcm: np.ndarray, rate: int) -> dict:
        """从有效 PCM 实测峰值/RMS/噪声底；长素材跳过 BS.1770 离线分析。"""
        data = pcm.astype(np.float64, copy=False)
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        rms = float(np.sqrt(np.mean(data ** 2))) if data.size else 0.0
        report: dict = {
            "peak_db": 20.0 * math.log10(peak) if peak > 0 else -120.0,
            "rms_db": 20.0 * math.log10(rms) if rms > 0 else -120.0,
        }
        # 噪声底：50 ms 窗 RMS 的第 10 百分位（cheap、单调、可复现）。
        window = max(1, int(round(0.05 * rate)))
        if data.shape[1] >= window * 2:
            mono = data if data.shape[0] == 1 else data.mean(axis=0)
            blocks = mono[: (mono.size // window) * window].reshape(-1, window)
            block_rms = np.sqrt(np.mean(blocks ** 2, axis=1))
            floor = float(np.percentile(block_rms, 10))
            report["noise_floor_db"] = 20.0 * math.log10(floor) if floor > 0 else -120.0
        else:
            report["noise_floor_db"] = report["rms_db"]
        if 0 < data.shape[1] <= rate * 60:
            try:
                from src.features.edit.core.loudness_analyzer import analyze_loudness
                loudness = analyze_loudness(pcm, float(rate))
                if getattr(loudness, "backend_available", False):
                    report["integrated_lufs"] = float(loudness.integrated_lufs)
                report["true_peak_dbtp"] = float(loudness.true_peak_dbtp)
            except Exception:  # noqa: BLE001 - 响度分析失败不影响波形
                pass
        return report

    def _path_cache_id(self, asset_id: str) -> str:
        path = os.path.abspath(self._waveform_path)
        digest = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
        return f"{asset_id}:mixer:{digest}"

    def _cached_source_waveform(self) -> WaveformData | None:
        asset_id = self._waveform_asset_id
        if asset_id is None:
            return None
        waveform = get_cached_waveform(asset_id)
        if waveform is None:
            return None
        path = os.path.abspath(self._waveform_path)
        if waveform.source_path is not None and os.path.abspath(waveform.source_path) == path:
            return waveform
        # An in-flight decode or cached envelope for a relinked asset can
        # still have its old path. Leave other timeline consumers alone and
        # request the current source under a path-specific key.
        if ":mixer:" not in asset_id:
            self._waveform_asset_id = self._path_cache_id(asset_id)
            return self._cached_source_waveform()
        return None

    def _waveform_ready(self, asset_id: str) -> None:
        if asset_id == self._waveform_asset_id:
            waveform = self._cached_source_waveform()
            if waveform is not None and waveform.is_complete:
                self._apply_waveform(waveform)
            elif asset_id != self._waveform_asset_id and self._extractor is not None:
                self._extractor.request_waveform(self._waveform_asset_id, self._waveform_path)

    def _waveform_failed(self, asset_id: str, error: str) -> None:
        if asset_id == self._waveform_asset_id and not is_waveform_pending(asset_id):
            self._status.setText(I18N.tr("mixer.clip_editor.no_waveform", "Source waveform unavailable."))
            self._status.setToolTip(error)

    def _apply_waveform(self, waveform: WaveformData) -> None:
        clip, asset, panel = self._clip, self._asset, self._panel
        if asset is None or panel is None:
            return
        if self._pcm_applied:
            # 真实 PCM 已提供波形/RMS/频谱；源包络只是更快的首帧绘制，
            # 不能在 PCM 之后回头覆盖成零 RMS 的包络数据。
            return
        if clip is None:
            start, end = 0.0, waveform.duration_seconds
            duration = end
        else:
            source_fps = asset.fps
            if not isinstance(source_fps, (int, float)) or not math.isfinite(source_fps) or source_fps <= 0.0:
                source_fps = self._fps
            start_frame = bound_audio_frame(clip.source_start, self._offset_seconds, source_fps)
            start = start_frame / source_fps + clip.source_sample_offset / AUDIO_REFERENCE_SAMPLE_RATE
            end = (start_frame + clip.source_duration) / source_fps
            duration = clip.timeline_duration / self._fps
        content_start, content_end = max(0.0, start), min(waveform.duration_seconds, end)
        if content_end <= content_start or not waveform.num_samples or not waveform.channels:
            self._status.setText(I18N.tr("mixer.clip_editor.no_waveform", "Source waveform unavailable."))
            return
        bins = min(16384, max(2, round((end - start) * waveform.samples_per_second)))
        leading = min(bins - 1, round((content_start - start) * bins / (end - start)))
        trailing = min(bins - leading - 1, round((end - content_end) * bins / (end - start)))
        peaks_min, peaks_max = [], []
        for channel in range(min(2, waveform.channels)):
            low, high = waveform.get_samples_for_range(
                content_start, content_end, bins - leading - trailing, channel,
            )
            low = [0.0] * leading + low.tolist() + [0.0] * trailing
            high = [0.0] * leading + high.tolist() + [0.0] * trailing
            if clip is not None and clip.is_reverse_playback():
                low, high = low[::-1], high[::-1]
            peaks_min.append(low)
            peaks_max.append(high)
        info = self._waveform_asset.metadata.get("audio_info") if self._waveform_asset is not None else None
        source_rate = info.get("sample_rate") if isinstance(info, dict) else None
        rate = source_rate if isinstance(source_rate, int) and source_rate > 0 else self._sample_rate
        panel.load_audio_data(rate, duration, peaks_min, peaks_max,
                              [[0.0] * bins for _ in peaks_max], clip_name=asset.display_name)
        panel.update_playhead(0.0)
        panel.sr_badge.setText(f"{source_rate / 1000:g} kHz" if rate == source_rate else "—")
        peak = max(max(map(max, peaks_max)), -min(map(min, peaks_min)))
        panel.peak_val_lbl.setText(f"{20.0 * math.log10(peak):+.1f} dBFS" if peak > 0.0 else "−∞ dBFS")
        self._status.setText(
            I18N.tr("mixer.clip_editor.source_read_only", "Source waveform · Add the audio to a timeline to edit gain and fades.")
            if clip is None else I18N.tr("mixer.clip_editor.source_waveform", "Source waveform · Clip gain and fades are saved to the timeline.")
        )
        self._sync_gain_fade()

    def _sync_gain_fade(self) -> None:
        clip, panel = self._clip, self._panel
        if panel is None:
            return
        if clip is None:
            panel.sync_gain_fade(0.0, 0.0, 0.0, "equal_power", "equal_power", locked=True)
            return
        fade_in = (clip.fade_in_samples / AUDIO_REFERENCE_SAMPLE_RATE if clip.fade_in_samples is not None
                   else clip.fade_in_duration / self._fps)
        fade_out = (clip.fade_out_samples / AUDIO_REFERENCE_SAMPLE_RATE if clip.fade_out_samples is not None
                    else clip.fade_out_duration / self._fps)
        panel.sync_gain_fade(clip.audio_gain_db, fade_in, fade_out,
                             clip.effective_fade_curve_in or "equal_power",
                             clip.effective_fade_curve_out or "equal_power",
                             locked=self._locked)

    def _on_gain_requested(self, value: float) -> None:
        if self._clip is not None and not self._locked and value != self._clip.audio_gain_db:
            self.gain_committed.emit(self._clip.clip_id, value)
        self._sync_gain_fade()

    def _on_fade_requested(self, fade_in: float, fade_out: float) -> None:
        clip = self._clip
        if clip is not None and not self._locked and math.isfinite(fade_in) and math.isfinite(fade_out):
            if clip.fade_in_samples is not None or clip.fade_out_samples is not None:
                clock = TimelineSampleClock(self._fps)
                limit = int(clock.frame_start_sample(clip.timeline_duration))
                current_in = (clip.fade_in_samples if clip.fade_in_samples is not None
                              else round(clock.frame_start_sample(clip.fade_in_duration)))
                current_out = (clip.fade_out_samples if clip.fade_out_samples is not None
                               else round(clock.frame_start_sample(clip.fade_out_duration)))
                fi = max(0, min(limit, round(fade_in * AUDIO_REFERENCE_SAMPLE_RATE)))
                fo = max(0, min(limit - fi, round(fade_out * AUDIO_REFERENCE_SAMPLE_RATE)))
                if (fi, fo) != (current_in, current_out):
                    # A mixed frame/sample clip keeps the frame-backed end if it was untouched.
                    self.sample_fade_committed.emit(clip.clip_id, fi if fi != current_in else -1,
                                                    fo if fo != current_out else -1)
            else:
                fi = max(0, min(clip.timeline_duration, round(fade_in * self._fps)))
                fo = max(0, min(clip.timeline_duration - fi, round(fade_out * self._fps)))
                if (fi, fo) != (clip.fade_in_duration, clip.fade_out_duration):
                    self.fade_committed.emit(clip.clip_id, fi, fo)
        self._sync_gain_fade()

    def _on_fade_curve_requested(self, curve: str) -> None:
        clip = self._clip
        if clip is not None and not self._locked and curve in _DISPLAY_FADE_CURVES:
            if not (clip.effective_fade_curve_in == clip.effective_fade_curve_out == curve):
                self.fade_curve_committed.emit(clip.clip_id, curve)
        self._sync_gain_fade()
