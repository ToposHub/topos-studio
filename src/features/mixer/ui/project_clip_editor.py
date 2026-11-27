"""Show source waveforms and timeline clips in the professional audio editor."""
from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QHideEvent, QKeySequence, QMouseEvent, QShortcut, QShowEvent
from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from src.features.edit.core.audio_sync import binding_for_asset, bound_audio_frame, resolve_audio_source
from src.features.edit.ui.clip_envelope_overlay import envelope_accent
from src.shared.track_colors import resolve_track_color
from src.features.edit.core.audio_waveform import (
    WaveformData, WaveformExtractor, get_cached_waveform, is_waveform_pending,
)
from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES
from src.features.edit.core.clip_pcm import (
    ClipAudioRenderSpec, ClipPcmProvider, ClipPcmResult, compute_waveform_stats,
    find_zero_crossing, resolve_asset_audio_spec, resolve_clip_audio_spec,
    ClipWaveformWindow, decode_clip_waveform_window, full_clip_pcm_available,
)
from src.features.edit.core.audio_edit import (
    LENGTH_CHANGING_PROCESSORS, OPERATION_LOUDNESS_MATCH, OPERATION_NORMALIZE_PEAK,
    append_step, apply_audio_edit_to_pcm, canvas_markers_from, copy_audio_edit,
    canvas_points_from_envelope, clear_steps, envelope_from_canvas_points, envelope_nodes,
    make_step, marker_list, markers_from_canvas, output_length_after_steps, remove_step,
    remap_audio_edit_for_length_steps, render_audio_edit_full, render_pre_render_steps, time_stretch_output_length,
    replace_processor_step, replace_step_enabled, requires_pre_render,
    set_envelope, set_markers, steps_signature, suffix_audio_edit,
)
from src.features.edit.core.sample_time import (
    AUDIO_REFERENCE_SAMPLE_RATE, TimelineSampleClock, reference_position_to_rate,
)
from src.features.edit.core.fade_curve_spec import is_fade_curve
from src.features.mixer.effects.panel_style import style_reference_panel
from src.features.mixer.effects.panels.clip_editor import (
    ClipEditorElidedLabel, ProcessStepRow, StudioAudioClipEditor,
)
from src.features.mixer.spectrogram import analyze_spectrogram
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import glyph_icon, present_effect_window
from src.shared.ui.dock_context_menu import add_dock_management_actions, find_parent_dock

from .clip_analysis import (
    ClipAuditionCoordinates, GainMeasurementRequest, GainMeasurementResult,
    build_band_preview, make_timepitch_draft, measure_gain_edit, render_native_clip_pcm,
    render_audition_output,
)
from .clip_audition import ClipAuditionSession
from .clip_effect_rack import ClipEffectRack
from .clip_preview_worker import ClipPreviewResult, ClipPreviewWorker
from .selection_effect_dialog import SELECTION_EFFECTS, SelectionEffectDialog, selection_effect_name
from .workspace_style import PANEL_QSS


class _PanelStatusForwarder:
    """处理器状态消息的最小 setText 代理（转发到包装层状态行）。"""

    def __init__(self, panel: "_TimelineClipPanel") -> None:
        self._panel = panel

    def setText(self, text: str) -> None:
        self._panel.set_status_message(text)

    def setToolTip(self, text: str) -> None:
        self._panel.noise_status_lbl.setToolTip(text)


@dataclass(frozen=True)
class _SelectionEffectRender:
    audio_edit: dict[str, Any]
    base: np.ndarray
    wet: np.ndarray | None = None


if TYPE_CHECKING:
    from src.features.edit.core.effect_instance import EffectInstance
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_v2 import ClipInstance, TimelineSequence


_FADE_CURVES = {"Linear": "linear", "S-Curve": "smooth", "Log": "equal_power"}
_DISPLAY_FADE_CURVES = {"linear": "Linear", "smooth": "S-Curve", "equal_power": "Equal Power"}


class _TimelineClipPanel(StudioAudioClipEditor):
    gain_requested = pyqtSignal(float)
    microfade_requested = pyqtSignal()
    selection_gain_requested = pyqtSignal(float, float, float, int)
    # P0-3：清空处理栈走工程命令（原型本地清空在生产行程外）。
    stack_clear_requested = pyqtSignal()
    # §6.2：变速/音高/删除区间/裁剪的改长度提交（走项目/序列事务）。
    time_pitch_commit_requested = pyqtSignal()
    time_pitch_draft_changed = pyqtSignal()
    # §5 画笔：频谱修复提交（载荷已换算到数据域：秒/Hz/半径/带宽）。
    repair_commit_requested = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__(show_commit_button=False)
        style_reference_panel(self)
        self.btn_timeline = QPushButton(
            glyph_icon("arrow-left", "#CFCFD6"),
            I18N.tr("mixer.clip_editor.timeline", "Return to timeline"),
        )
        self.btn_timeline.setObjectName("GroupBtn")
        self.btn_timeline.setIconSize(QSize(14, 14))
        self.btn_timeline.setCursor(Qt.CursorShape.PointingHandCursor)
        self._transport_layout.insertWidget(0, self.btn_timeline)
        self.effect_rack = ClipEffectRack()
        self.tools_panel.insertTab(
            self.tools_panel.indexOf(self.process_stack_tab),
            self.effect_rack, I18N.tr("mixer.clip_editor.rack.tab", "Effects"))
        self.effect_rack.reveal_requested.connect(self._show_effect_rack)
        # Fit the dock around the canvas. The toolbar and tools scroll independently.
        self.setMinimumSize(0, 0)
        self.canvas.setMinimumHeight(142)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._play_timer.stop()
        self._fade_editable = False
        self._pending_fade: tuple[float, float] | None = None
        self.canvas.installEventFilter(self)
        self.canvas._env_enabled = False
        self.canvas._magnetic_snap = False
        self.canvas.selection_gain_editable = False
        self.canvas.waveform_includes_processing = True
        self.canvas._env_curve_mode = "model"
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
        self._raw_pcm: np.ndarray | None = None      # 处理栈前的原始 PCM
        self._clip_envelope_available = False
        self._envelope_projection: tuple[float, int | None] | None = None
        self._model_audio_edit: dict | None = None   # 工程模型驱动的处理栈
        # P0-3 · 片段级 gain/fade 快照（模型 → 预览/试听消费；None=无片段）：
        # (gain_db, fade_in_frames, fade_out_frames, fade_in_samples,
        #  fade_out_samples, curve_in, curve_out, fps, timeline_duration)。
        self._clip_gain_fade: tuple | None = None
        self._clip_fx: tuple[tuple[EffectInstance, ...], int, float] | None = None
        self._clip_fx_revision = 0
        # P0 · 预览 worker 状态：代际（旧发布丢弃）、spec 键、缓存计划
        # 钩子（外层注入）、失败留痕、离线模式（测试同步计算）。
        self._preview_generation = 0
        self._preview_request_key: tuple | None = None
        self._preview_worker: ClipPreviewWorker | None = None
        self._preview_spec_key: tuple | str = ""
        self._pre_render_plan_for_preview: Any = lambda: None
        self._preview_failure_note = ""
        self._last_preview: ClipPreviewResult | None = None
        self._dock_active = True
        self._spectrum_worker: ClipPreviewWorker | None = None
        self._spectrum_key: tuple | None = None
        self._loudness_worker: ClipPreviewWorker | None = None
        self._loudness_key: tuple | None = None
        self._spectrum_timer = QTimer(self)
        self._spectrum_timer.setSingleShot(True)
        self._spectrum_timer.setInterval(85)
        self._spectrum_timer.timeout.connect(self._request_spectrum)
        self.canvas.spectralViewportChanged.connect(self._schedule_spectrum)
        self.btn_spectral.toggled.connect(self._on_spectrum_settings_changed)
        self.fftSizeChanged.connect(self._on_spectrum_settings_changed)
        self._audition_coordinates = ClipAuditionCoordinates()
        self._preview_offline = False
        # P1-6：噪声特征与降噪预览（试听态；入栈需预渲染缓存，后续批次）。
        self._noise_profile: Any = None
        self._denoise_preview: tuple[tuple[int, int], tuple[int, int], float, int] | None = None
        # P2-7：变速/音高预览——P1-1/P1-2 起 DSP 在预览 worker 内计算
        # （请求快照 (lo, hi, ratio_pct, semitones, preserve)，可取消/
        # 替换），UI 线程不再同步做 retime/pitch。基准时长在进入预览
        # 时记录、清除时恢复。
        self._timepitch_request: tuple | None = None
        self._validated_timepitch: tuple | None = None
        self._timepitch_base_duration: float | None = None
        self._audition = ClipAuditionSession(self)
        self._band_preview: tuple[int, np.ndarray] | None = None
        self._pending_audition_seek: float | None = None
        self._audition.position_changed.connect(self._on_audition_position)
        self._audition.playing_changed.connect(self._on_audition_playing_changed)
        self._audition.device_failed.connect(self._show_device_failure)
        self._zero_snap_armed = False
        self._suppress_zero_snap = False
        self._stack_editable_hint = False
        self.canvas.selectionChanged.connect(self._on_pcm_selection_changed)
        self.canvas.selectionDragFinished.connect(self._finish_selection_drag)
        self.canvas.playheadDragFinished.connect(self._finish_audition_seek)
        self.minimap.playheadDragFinished.connect(self._finish_audition_seek)
        # 原型的磁性吸附（播放头/标记）与 PCM 过零吸附互补，两者都随开关。
        self.btn_snap_zero.toggled.connect(self._on_zero_snap_toggled)
        # A/B 旁路只切换试听与机架的显示状态。
        self.btn_bypass.toggled.connect(self._on_preview_bypass_toggled)
        # 处理器状态消息转发到包装层状态行（无包装层时退噪声标签）。
        self._status_hook: Any = None
        self.info_label = _PanelStatusForwarder(self)
        # §6.2：待提交的变速/音高参数（预览时记录；提交按钮消费）。
        self._pending_timepitch: tuple | None = None
        self._setup_timepitch_actions()
        # §5 画笔：挂起的频谱修复笔迹（数据域载荷；修复按钮/H 键消费）。
        self._pending_repair: dict | None = None
        self.canvas.spectralBrushApplied.connect(self._on_canvas_brush_applied)

    def _on_preview_bypass_toggled(self, _bypassed: bool) -> None:
        self._rebuild_process_stack_ui()
        self.set_stack_editable(self._stack_editable_hint)
        self._refresh_stack_preview()

    def _show_effect_rack(self) -> None:
        self.tools_panel.setCurrentWidget(self.effect_rack)
        self.btn_tools_panel.setChecked(True)

    def set_status_message(self, text: str) -> None:
        hook = self._status_hook
        if hook is not None:
            hook(text)
        else:
            self.noise_status_lbl.setText(text)

    def _refresh_lufs_ui(self) -> None:
        super()._refresh_lufs_ui()
        preview = getattr(self, "_last_preview", None)
        report = preview.report if preview is not None else {}
        if "integrated_lufs" not in report:
            self.lufs_val_lbl.setText("—")
            self.btn_apply_lufs.setText(I18N.tr("mixer.clip_editor.match_loudness", "Match target loudness"))
        if "true_peak_dbtp" not in report:
            self.true_peak_lbl.setText("—")

    def _commit_time_pitch(self) -> None:
        """生产路径：按钮 = 预览（记录待提交参数；demo 波形重采样不参与）。

        提交（改长度事务）由 ``_timepitch_commit_button`` 触发
        ``time_pitch_commit_requested``，参数取 ``_pending_timepitch``。
        """
        ratio_pct = round(self.stretch_knob.value(), 1)
        pitch_st = round(self.pitch_knob.value(), 1)
        if abs(ratio_pct - 100.0) < 0.05 and abs(pitch_st) < 0.05:
            return
        t0, t1, _, _ = self.canvas.getSelection()
        if t1 <= t0 or t0 < 0.0:
            t0, t1 = 0.0, self._duration
        preserve = self.preserve_pitch_sw.isChecked()
        # Tape speed changes pitch in the resampler itself.
        self.timePitchApplied.emit(t0, t1, ratio_pct, pitch_st if preserve else 0.0,
                                  preserve, "wsola")

    def set_pending_timepitch(self, pending: tuple | None) -> None:
        """记录/清除待提交的变速参数并同步提交按钮可用性。"""
        had_preview = self._pending_timepitch is not None
        self._pending_timepitch = pending
        has_preview = pending is not None
        self._timepitch_commit_button.setEnabled(
            has_preview and pending == self._validated_timepitch
            and self._pcm is not None and self._stack_editable_hint)
        self._timepitch_discard_button.setVisible(has_preview)
        self._timepitch_summary.setVisible(has_preview)
        if pending is not None:
            _lo, _hi, ratio, pitch, preserve = pending
            if not preserve:
                pitch = 12.0 * math.log2(ratio / 100.0)
            self._timepitch_summary.setText(I18N.tr(
                "mixer.clip_editor.timepitch_draft", "Preview · {rate:.1f}% · {pitch:+.1f} st"
            ).format(rate=ratio, pitch=pitch))
        self.canvas.project_edits_enabled = (
            self._stack_editable_hint and self._pcm is not None and not has_preview)
        if has_preview != had_preview:
            self.time_pitch_draft_changed.emit()
        self._update_stack_actions()
        self._refresh_selection_gain()

    def _discard_timepitch_preview(self) -> None:
        self.set_pending_timepitch(None)
        self.clear_timepitch_preview()
        self._reset_time_pitch()
        self.set_status_message(I18N.tr(
            "mixer.clip_editor.timepitch_discarded", "Speed and pitch preview discarded"))

    # ------------------------------------------------------------------
    # §5 画笔：频谱修复（生产路径）
    # ------------------------------------------------------------------
    def _apply_local_brush_heal(self, stroke: list[tuple[float, float]]) -> None:
        # 生产路径：demo 本地修复不参与（提交走 spectral_repair 步骤）。
        return

    def _on_canvas_brush_applied(self, stroke: list, radius_px: float) -> None:
        """笔迹松开 → 数据域载荷挂起（提交按钮或 H 键消费）。"""
        try:
            payload = self.canvas.brush_stroke_payload(stroke, radius_px)
        except Exception:  # noqa: BLE001 - 几何异常时忽略本次笔迹
            return
        if not payload.get("strokes"):
            return
        self._pending_repair = payload

    def _set_tool_mode(self, mode: str) -> None:
        super()._set_tool_mode(mode)
        self.btn_env_reset.setEnabled(self.canvas._can_edit_envelope())
        if mode == "brush":
            self.btn_spectral.setChecked(True)

    def _trigger_quick_heal(self) -> None:
        """修复按钮/H 键：优先消费挂起笔迹；否则用频谱选区合成修复载荷。"""
        if not self.canvas.project_edits_enabled:
            return
        payload = self._pending_repair
        if payload is None:
            t0, t1, f_low, f_high = self.canvas.getSelection()
            if t1 <= t0 or f_high <= f_low:
                self.set_status_message(I18N.tr(
                    "mixer.clip_editor.repair_needs_region",
                    "Brush over the spectrogram or set a time-frequency "
                    "selection first"))
                return
            payload = {
                "strokes": [((t0 + t1) / 2.0, (f_low * f_high) ** 0.5)],
                "time_radius_s": max(0.01, (t1 - t0) / 2.0),
                "bandwidth_oct": max(0.05, __import__("math").log2(
                    max(1.01, f_high / max(1.0, f_low)))),
            }
        payload = dict(payload)
        payload["mode"] = "heal" if payload.get("mode") in (None, "heal") \
            else payload["mode"]
        self._pending_repair = None
        self.repair_commit_requested.emit(payload)

    def _setup_timepitch_actions(self) -> None:
        self.stretch_algo_combo.hide()
        self._timepitch_button = self.time_pitch_action_button
        self._timepitch_button.setText(I18N.tr(
            "mixer.clip_editor.timepitch_preview", "Preview speed and pitch"))
        self._timepitch_button.setToolTip(I18N.tr(
            "mixer.clip_editor.timepitch_preview_tip", "Audition the settings for the selection or the whole clip"))
        layout = self._timepitch_button.parentWidget().layout()
        self._timepitch_summary = ClipEditorElidedLabel()
        self._timepitch_summary.setStyleSheet("color: #8BCBD4; font-size: 10px; border: none;")
        layout.addWidget(self._timepitch_summary)
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self._timepitch_commit_button = QPushButton(I18N.tr(
            "mixer.clip_editor.timepitch_commit", "Commit to stack"))
        self._timepitch_commit_button.setObjectName("MiniCellBtn")
        self._timepitch_commit_button.setAccessibleName(self._timepitch_commit_button.text())
        self._timepitch_commit_button.setToolTip(I18N.tr(
            "mixer.clip_editor.timepitch_commit_hint", "Apply the last preview to the timeline clip"))
        self._timepitch_commit_button.clicked.connect(self.time_pitch_commit_requested.emit)
        actions.addWidget(self._timepitch_commit_button, 1)
        self._timepitch_discard_button = QPushButton(I18N.tr(
            "mixer.clip_editor.timepitch_discard", "Discard preview"))
        self._timepitch_discard_button.setObjectName("MiniCellBtn")
        self._timepitch_discard_button.clicked.connect(self._discard_timepitch_preview)
        actions.addWidget(self._timepitch_discard_button, 1)
        layout.addLayout(actions)
        self.set_pending_timepitch(self._pending_timepitch)

    def _on_zero_snap_toggled(self, enabled: bool) -> None:
        self._zero_snap_armed = enabled

    def _init_demo_audio(self) -> None:
        pass

    def _setup_shortcuts(self) -> None:
        for key, callback in (
            (QKeySequence.StandardKey.SelectAll, self.canvas.selectAll),
            (Qt.Key.Key_Escape, self._clear_selection_or_cancel_fade_curve),
            (Qt.Key.Key_E, self._zoom_to_selection),
            (Qt.Key.Key_Space, self._toggle_play),
            (QKeySequence("Ctrl+Space"), self._toggle_skip_play),
            (Qt.Key.Key_M, lambda: self.canvas.addMarker() if self.canvas.project_edits_enabled else None),
            (Qt.Key.Key_A, self.btn_tool_select.click),
            (Qt.Key.Key_V, self.btn_tool_envelope.click),
            (Qt.Key.Key_B, self.btn_tool_brush.click),
            (Qt.Key.Key_H, self._trigger_quick_heal),
            (Qt.Key.Key_Z, self.btn_snap_zero.click),
            (Qt.Key.Key_Delete, self._delete_selection_or_node),
            (Qt.Key.Key_Backspace, self._delete_selection_or_node),
        ):
            shortcut = QShortcut(QKeySequence(key), self.canvas)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)

    def _delete_selection_or_node(self) -> None:
        if self.canvas.project_edits_enabled:
            super()._delete_selection_or_node()

    # ------------------------------------------------------------------
    # PCM 试听（P0-2 §2.3）
    # ------------------------------------------------------------------
    def attach_pcm(self, pcm: np.ndarray, sample_rate: int) -> None:
        """装载真实 PCM：启用播放/循环/频谱/过零吸附并复位播放状态。

        P0：有工程处理栈时试听装载**处理后**数据（由预览发布驱动），
        不再用源 PCM 当栈处理结果——重开静音/降噪栈首次试听即正确。
        """
        self._cancel_spectrum()
        self._cancel_loudness()
        self._pcm = pcm
        self._raw_pcm = pcm
        self.canvas.polarity_channel_count = pcm.shape[0]
        self._on_edit_channel_selected(-1)
        self._pcm_rate = int(sample_rate) if sample_rate > 0 else AUDIO_REFERENCE_SAMPLE_RATE
        self._timepitch_request = None
        self._validated_timepitch = None
        self._timepitch_base_duration = None
        self._denoise_preview = None
        self._last_preview = None
        self._refresh_lufs_ui()
        self._audition_coordinates = ClipAuditionCoordinates()
        self.set_pending_timepitch(None)
        self._preview_generation += 1  # 旧片段在途预览全部作废
        gain_fade = self._clip_gain_fade
        if (self._model_audio_edit is None and self._clip_fx is None
                and (gain_fade is None or not any(gain_fade[:5]))):
            self._audition.load(pcm, sample_rate)
        else:
            self._audition.clear()
        self._is_playing = False
        self._skip_selection_mode = False
        self._update_audition_button(False)
        self._set_pcm_tools_enabled(True)
        self.btn_play.setEnabled(self._audition.has_audio())
        self.set_stack_editable(self._stack_editable_hint)

    def _set_pcm_tools_enabled(self, available: bool) -> None:
        unavailable = I18N.tr("mixer.clip_editor.pcm_required", "Wait for the audio samples to load.")
        for widget in (self.btn_play, self._stop_button, self.btn_loop, self.btn_snap_zero,
                       self.btn_spectral, self.fft_combo, self.action_capture_noise, self.btn_denoise,
                       self.denoise_knob, self.denoise_pill, self.stretch_knob, self.pitch_knob,
                       self.pitch_pill, self.stretch_ratio_pill, self.target_dur_pill,
                       self.time_pitch_reset_button, self.preserve_pitch_sw, self._timepitch_button):
            widget.setEnabled(available)
            if not available:
                widget.setToolTip(unavailable)
        if available:
            self.btn_play.setToolTip(I18N.tr("mixer.clip_editor.play_audition", "Start or stop clip audition (Space)"))
            self.btn_loop.setToolTip(I18N.tr("mixer.clip_editor.loop_audition", "Loop the selection, or the whole clip"))
            self.btn_spectral.setToolTip(I18N.tr(
                "mixer.clip_editor.spectrogram_tip", "Show or hide the spectrogram (time, frequency, and level)"))
            self._timepitch_button.setToolTip(I18N.tr(
                "mixer.clip_editor.timepitch_preview_tip", "Audition the settings for the selection or the whole clip"))

    def detach_pcm(self) -> None:
        """Release samples and audition controls before loading a new audio range."""
        self._pending_audition_seek = None
        self._audition.clear()
        self.canvas._ch_muted[:] = [False] * len(self.canvas._ch_muted)
        self._audition.set_monitor(muted=(), solo=())
        if self._preview_worker is not None:
            self._preview_worker.cancel()
        self._cancel_spectrum()
        self._cancel_loudness()
        self.canvas.setSpectrogram(None)
        self.canvas.setWaveformSamples(None)
        self._pcm = None
        self._raw_pcm = None
        self.canvas.polarity_channel_count = 0
        self.btn_bypass.setChecked(False)
        self._timepitch_request = None
        self._validated_timepitch = None
        self._timepitch_base_duration = None
        self._denoise_preview = None
        self._last_preview = None
        self._refresh_lufs_ui()
        self._audition_coordinates = ClipAuditionCoordinates()
        self._pending_repair = None
        self.set_noise_profile(None)
        self._preview_generation += 1  # 在途预览作废，旧回调不误写
        self._set_pcm_tools_enabled(False)
        self.set_pending_timepitch(None)
        self.set_stack_editable(self._stack_editable_hint)
        self._is_playing = False
        self._skip_selection_mode = False
        self._update_audition_button(False)

    def set_stack_editable(self, editable: bool) -> None:
        """有可编辑时间线片段时启用处理栈相关控件（源监视模式禁用）。"""
        active = editable and self._pcm is not None
        self.canvas.project_edits_enabled = active and self._pending_timepitch is None
        if not self.canvas.project_edits_enabled:
            self.canvas.cancel_envelope_drag()
        self.canvas.channel_monitor_enabled = self._pcm is not None
        if not active and self.canvas._tool_mode == "brush":
            self._set_tool_mode("select")
        self.set_pending_timepitch(self._pending_timepitch)

    def _update_stack_actions(self) -> None:
        active = self.canvas.project_edits_enabled
        self.action_normalize.setEnabled(active)
        self.btn_bypass.setEnabled(self._pcm is not None and (
            self._model_audio_edit is not None or self._pending_timepitch is not None
            or self._clip_fx is not None))
        for widget in (self.btn_tool_envelope, self.btn_tool_brush,
                       self.btn_apply_lufs, self.target_lufs_pill, self.tp_ceil_pill,
                       *self._lufs_preset_btns.values(), *self._selection_edit_buttons):
            widget.setEnabled(active)
        self.btn_env_reset.setEnabled(self.canvas._can_edit_envelope())
        self.btn_clear_stack.setEnabled(active and bool(self._process_steps))
        for index in range(self.stack_list_layout.count()):
            row = self.stack_list_layout.itemAt(index).widget()
            if isinstance(row, ProcessStepRow):
                row.setEnabled(active)

    def _refresh_selection_gain(self) -> None:
        self.canvas.selection_gain_editable = (
            self._stack_editable_hint and self._raw_pcm is not None
            and self._pending_timepitch is None)
        for channel, button in self._edit_ch_btns.items():
            button.setEnabled(self.canvas.selection_gain_editable and (
                channel < 0 or self._raw_pcm is not None and channel < self._raw_pcm.shape[0]))
        if not self.canvas.selection_gain_editable:
            self.canvas.setPreviewGain(0.0)
        self.canvas.update()

    def _sync_channel_polarity(self) -> None:
        inverted = {
            channel
            for step in ((self._model_audio_edit or {}).get("steps") or ())
            if isinstance(step, dict) and step.get("enabled", True)
            and step.get("processor_id") == "channel_polarity"
            for channel in ((step.get("params") or {}).get("channels") or ())
        }
        for channel in range(len(self.canvas._ch_phase_inv)):
            self.canvas._ch_phase_inv[channel] = channel in inverted

    def sync_audio_edit(self, audio_edit: dict | None,
                        output_total_samples: int | None = None) -> None:
        """处理栈机架由工程模型驱动（命令提交后刷新；P0-3 §控件映射）。"""
        geometry = (self._duration, output_total_samples)
        if audio_edit == self._model_audio_edit:
            if geometry != self._envelope_projection:
                self._envelope_projection = geometry
                self._project_overlays(ClipAuditionCoordinates.from_edit(audio_edit), output_total_samples)
            return
        self._envelope_projection = geometry
        self._validated_timepitch = None
        self._model_audio_edit = audio_edit
        self._project_overlays(ClipAuditionCoordinates.from_edit(audio_edit), output_total_samples)
        self._sync_channel_polarity()
        view_steps = []
        for step in (audio_edit or {}).get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            params = step.get("params", {}) if isinstance(step.get("params"), dict) else {}
            processor = str(step.get("processor_id", ""))
            label = params.get("label") or {
                "range_gain": I18N.tr("mixer.clip_editor.step_gain", "Gain"),
                "mute": I18N.tr("mixer.clip_editor.step_mute", "Silence"),
                "normalize": I18N.tr("mixer.clip_editor.step_normalize", "Normalize"),
                "spectral_denoise": I18N.tr(
                    "mixer.clip_editor.step_denoise", "Denoise"),
                "channel_polarity": I18N.tr(
                    "mixer.clip_editor.polarity", "Invert polarity"),
                "time_stretch": I18N.tr(
                    "mixer.clip_editor.step_timepitch", "Speed / pitch"),
                "delete_range": I18N.tr(
                    "mixer.clip_editor.step_delete_range", "Delete range"),
                "spectral_repair": I18N.tr(
                    "mixer.clip_editor.step_repair", "Spectral repair"),
                "de_click": I18N.tr("mixer.clip_editor.de_click", "De-click"),
                "de_hum": I18N.tr("mixer.clip_editor.de_hum", "De-hum"),
                "pitch_correction": I18N.tr(
                    "mixer.clip_editor.pitch_correction", "Pitch correction"),
                "doubler": I18N.tr("mixer.clip_editor.doubler", "Doubler"),
            }.get(processor, processor)
            gain_db = params.get("gain_db")
            if processor == "spectral_denoise":
                reduction = params.get("reduction_db")
                detail = (
                    f"{params.get('start', 0)}-{params.get('end', 0)} smp · "
                    + (f"−{float(reduction):.1f} dB"
                       if isinstance(reduction, (int, float)) else "spectral")
                    + " · " + I18N.tr(
                        "mixer.clip_editor.step_pre_render", "pre-rendered"))
            elif processor == "time_stretch":
                detail = (
                    f"{params.get('start', 0)}-{params.get('end', 0)} smp · "
                    f"{float(params.get('ratio_pct', 100.0)):.1f}%"
                    + (f" · {float(params.get('pitch_semitones', 0.0)):+.1f} st"
                       if abs(float(params.get("pitch_semitones", 0.0))) > 1e-6 else "")
                    + " · " + I18N.tr(
                        "mixer.clip_editor.step_pre_render", "pre-rendered"))
            elif processor == "delete_range":
                detail = (
                    f"{params.get('start', 0)}-{params.get('end', 0)} smp · "
                    + I18N.tr("mixer.clip_editor.step_splice", "spliced")
                    + " · " + I18N.tr(
                        "mixer.clip_editor.step_pre_render", "pre-rendered"))
            elif processor == "spectral_repair":
                repair_strokes = params.get("strokes") or []
                detail = (
                    f"{len(repair_strokes)} pts · "
                    f"{str(params.get('mode', 'attenuate'))} · "
                    + I18N.tr(
                        "mixer.clip_editor.step_pre_render", "pre-rendered"))
            elif processor in {"de_click", "de_hum", "pitch_correction", "doubler"}:
                detail = (f"{params.get('start', 0)}-{params.get('end', 0)} smp · "
                          + I18N.tr("mixer.clip_editor.step_pre_render", "pre-rendered"))
            else:
                detail = (
                    f"{params.get('start', 0)}-{params.get('end', 0)} smp · "
                    f"{','.join('LR'[c] if c in (0, 1) else str(c) for c in params.get('channels', ()))}"
                    + (f" · {gain_db:+.1f} dB" if isinstance(gain_db, (int, float)) else "")
                )
            view_steps.append({
                "id": step.get("step_id"),
                "type": processor,
                "label": label,
                "detail": detail,
                "color": "#4BA6DD" if processor == "range_gain" else (
                    "#F26D6D" if processor == "mute" else (
                        "#B58BD8" if processor == "spectral_denoise" else (
                            "#5BC8C8" if processor == "time_stretch" else (
                                "#E0A458" if processor == "delete_range"
                                else "#7BC96F")))),
                "enabled": bool(step.get("enabled", True)),
                "params": params,
            })
        self._process_steps = view_steps
        self._rebuild_process_stack_ui()
        self.set_stack_editable(getattr(self, "_stack_editable_hint", False))
        self._refresh_stack_preview()

    def set_stack_editable_hint(self, editable: bool) -> None:
        self._stack_editable_hint = editable
        self.set_stack_editable(editable)

    def _project_overlays(self, coordinates: ClipAuditionCoordinates,
                          output_total_samples: int | None = None) -> None:
        edit = self._model_audio_edit
        origin = ClipAuditionCoordinates.from_edit(edit)
        nodes = envelope_nodes(edit)
        markers = marker_list(edit)
        if origin != coordinates:
            nodes = [{**node, "local_sample": origin.map_to(coordinates, node["local_sample"])}
                     for node in nodes]
            markers = [{**marker, "local_sample": origin.map_to(coordinates, marker["local_sample"])}
                       for marker in markers]
        total = output_total_samples or max(1, round(self._duration * self._pcm_rate))
        self.canvas.cancel_envelope_drag()
        self.canvas._env_enabled = self._clip_envelope_available
        self.canvas._env_points = canvas_points_from_envelope(nodes, self._duration, total)
        self.canvas._env_hover_idx = self.canvas._env_hover_seg = self.canvas._env_selected_idx = -1
        self.canvas._markers = canvas_markers_from(markers, self._duration, total)
        self.canvas._invalidate_cache()

    def _effective_audio_edit(self) -> dict | None:
        """A/B 全局旁路是临时监听状态，不改工程。"""
        if self._model_audio_edit is None or self.btn_bypass.isChecked():
            return None
        return self._model_audio_edit

    def _refresh_stack_preview(self) -> None:
        """Render audition PCM and waveform independently of the spectral display."""
        inputs = self._collect_preview_inputs()
        if inputs is None:
            return
        key = inputs["key"]
        if self._last_preview is not None and key == self._last_preview.key:
            if key != self._preview_request_key:
                self._preview_request_key = key
                if self._preview_worker is not None:
                    self._preview_worker.cancel()
            if self._preview_failure_note:
                self._preview_failure_note = ""
                self.set_status_message("")
            self._schedule_spectrum()
            self._request_loudness()
            return
        if key != self._preview_request_key:
            self._cancel_spectrum()
            self._cancel_loudness()
        if requires_pre_render(inputs["edit"]):
            inputs["plan"] = self._pre_render_plan_for_preview()
        self._preview_request_key = key
        self._preview_failure_note = ""
        if self._preview_offline:
            try:
                self._publish_preview(self._compute_preview(inputs))
            except (ValueError, RuntimeError, OSError) as exc:
                self._on_preview_failed(key, str(exc))
            return
        worker = self._ensure_preview_worker()
        worker.submit_cancellable(
            key, lambda cancelled: self._compute_preview({**inputs, "cancelled": cancelled}))

    def _ensure_preview_worker(self) -> ClipPreviewWorker:
        worker = self._preview_worker
        if worker is None:
            worker = ClipPreviewWorker()
            worker.preview_ready.connect(self._on_preview_ready)
            worker.preview_failed.connect(self._on_preview_failed)
            self.destroyed.connect(worker.finish)
            self._preview_worker = worker
        return worker

    def shutdown_preview(self) -> None:
        """编辑器关闭：停止预览 worker（可结束）。"""
        worker = getattr(self, "_preview_worker", None)
        if worker is not None:
            worker.finish()
            self._preview_worker = None
        self._cancel_spectrum()
        if self._spectrum_worker is not None:
            self._spectrum_worker.finish()
            self._spectrum_worker = None
        self._cancel_loudness()
        if self._loudness_worker is not None:
            self._loudness_worker.finish()
            self._loudness_worker = None
        self._preview_generation += 1
        self._preview_request_key = None

    def _on_spectrum_settings_changed(self) -> None:
        self._cancel_spectrum()
        self._schedule_spectrum()

    def set_dock_active(self, active: bool) -> None:
        self._dock_active = active
        if active:
            self._schedule_spectrum()
            self._request_loudness()
        else:
            self._cancel_spectrum()
            self._cancel_loudness()

    def _schedule_spectrum(self) -> None:
        if (self._dock_active and self.btn_spectral.isChecked()
                and self.isVisible() and self._last_preview is not None):
            self._spectrum_timer.start()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._schedule_spectrum()
        self._request_loudness()

    def hideEvent(self, event: QHideEvent) -> None:
        self.cancel_view_interaction()
        self._cancel_spectrum()
        self._cancel_loudness()
        super().hideEvent(event)

    def cancel_view_interaction(self) -> None:
        interrupted_seek = self._pending_audition_seek is not None
        playhead = self._playhead
        self._pending_fade = None
        self.canvas.cancel_gesture()
        self.minimap.cancel_gesture()
        self._stop_playback()
        if interrupted_seek:
            self.update_playhead(playhead)

    def _cancel_spectrum(self) -> None:
        self._spectrum_timer.stop()
        self._spectrum_key = None
        if self._spectrum_worker is not None:
            self._spectrum_worker.cancel()

    def _requested_spectrum_preview(self) -> ClipPreviewResult | None:
        preview = self._last_preview
        if (self._dock_active and preview is not None and preview.key == self._preview_request_key
                and self.btn_spectral.isChecked() and self.canvas.isVisible()):
            return preview
        return None

    def _request_spectrum(self) -> None:
        preview = self._requested_spectrum_preview()
        if preview is None:
            return
        nfft = self.current_fft_size()
        overview = self.canvas._spectrogram
        if overview is None or overview.nfft != nfft:
            start, end, columns = 0, None, 4096
        else:
            canvas = self.canvas
            samples_per_second = overview.total_samples / preview.duration
            start = math.floor(canvas._view_start * samples_per_second)
            end = math.ceil(canvas._view_end * samples_per_second)
            span = end - start
            target_hop = max(min(256, nfft // 4), span / max(1.0, canvas._body_rect().width()))
            detail = canvas._detail_spectrogram
            if ((start <= 0 and end >= overview.total_samples)
                    or overview.samples_per_column <= target_hop * 1.25
                    or (detail is not None and detail.start_sample <= start and end <= detail.end_sample
                        and detail.samples_per_column <= target_hop * 1.25)):
                return
            padding = math.ceil(span * 0.25)
            start = max(0, start - padding)
            end = min(overview.total_samples, end + padding)
            columns = min(4096, math.ceil((end - start) / target_hop))
        key = (preview.key, nfft, start, end, columns)
        if key == self._spectrum_key:
            return
        self._spectrum_key = key
        worker = self._spectrum_worker
        if worker is None:
            worker = ClipPreviewWorker()
            worker.preview_ready.connect(self._publish_spectrum)
            worker.preview_failed.connect(self._on_spectrum_failed)
            self.destroyed.connect(worker.finish)
            self._spectrum_worker = worker

        def build(cancelled: Callable[[], bool]) -> tuple | None:
            result = analyze_spectrogram(preview.edited, preview.sample_rate, nfft=nfft,
                                         start=start, end=end, columns=columns, cancelled=cancelled)
            return (key, result) if result is not None else None

        worker.submit_cancellable(key, build)

    def _publish_spectrum(self, completed: tuple) -> None:
        key, spectrum = completed
        preview = self._requested_spectrum_preview()
        if (key != self._spectrum_key or preview is None
                or key[0] != preview.key or key[1] != self.current_fft_size()):
            return
        self._spectrum_key = None
        if key[3] is None:
            self.canvas.setSpectrogram(spectrum)
            self._schedule_spectrum()
            return
        samples_per_second = spectrum.total_samples / preview.duration
        if (spectrum.start_sample <= self.canvas._view_start * samples_per_second
                and self.canvas._view_end * samples_per_second <= spectrum.end_sample):
            self.canvas.setDetailedSpectrogram(spectrum)
        else:
            self._schedule_spectrum()

    def _on_spectrum_failed(self, key: tuple, _message: str) -> None:
        if key == self._spectrum_key:
            self._spectrum_key = None

    def _request_loudness(self) -> None:
        preview = self._last_preview
        if (preview is None or preview.key != self._preview_request_key
                or preview.key == self._loudness_key or "true_peak_dbtp" in preview.report
                or not 0 < preview.edited.shape[1] <= preview.sample_rate * 60):
            return
        if not self._preview_offline and not (self._dock_active and self.isVisible()):
            return
        key = self._loudness_key = preview.key

        def measure(cancelled: Callable[[], bool]) -> tuple:
            return key, self._measure_loudness(preview.edited, preview.sample_rate, cancelled)

        if self._preview_offline:
            try:
                self._publish_loudness(measure(lambda: False))
            except (RuntimeError, ValueError, OSError) as exc:
                self._on_loudness_failed(key, str(exc))
            return
        worker = self._loudness_worker
        if worker is None:
            worker = ClipPreviewWorker()
            worker.preview_ready.connect(self._publish_loudness)
            worker.preview_failed.connect(self._on_loudness_failed)
            self.destroyed.connect(worker.finish)
            self._loudness_worker = worker
        worker.submit_cancellable(key, measure)

    def _publish_loudness(self, completed: tuple) -> None:
        key, report = completed
        preview = self._last_preview
        if (key != self._loudness_key or key != self._preview_request_key
                or preview is None or key != preview.key):
            return
        preview.report.update(report)
        if "integrated_lufs" in report:
            self._current_lufs = report["integrated_lufs"]
        if "true_peak_dbtp" in report:
            self._current_true_peak = report["true_peak_dbtp"]
        self._refresh_lufs_ui()

    def _on_loudness_failed(self, key: tuple, _message: str) -> None:
        if key == self._loudness_key:
            self._loudness_key = None

    def _cancel_loudness(self) -> None:
        self._loudness_key = None
        if self._loudness_worker is not None:
            self._loudness_worker.cancel()

    def set_clip_gain_fade(self, snapshot: tuple | None) -> None:
        """P0-3 · 片段级 gain/fade 快照（模型 → 预览/试听消费）。

        预览重算以快照为键：gain/淡化命令落地后 sync 路径调用本方法，
        变化即刷新（波形/频谱/试听与交付消费同一份片段级设置）。
        """
        if self._clip_gain_fade == snapshot:
            return
        self._clip_gain_fade = snapshot
        self._refresh_stack_preview()

    def set_clip_effects(self, clip: ClipInstance | None, fps: float) -> None:
        effects = tuple(
            effect for effect in clip.effects
            if effect.effect_type in AUDIO_MIXER_EFFECT_TYPES and effect.is_enabled
        ) if clip is not None else ()
        snapshot = (effects, clip.timeline_start, fps) if clip is not None and effects else None
        previous = self._clip_fx
        if previous is None and snapshot is None:
            return
        # EffectInstance equality compares IDs. Parameter edits replace the instance.
        if (previous is not None and snapshot is not None and previous[1:] == snapshot[1:]
                and len(previous[0]) == len(effects)
                and all(old is new for old, new in zip(previous[0], effects))):
            return
        self._clip_fx = snapshot
        self._clip_fx_revision += 1
        self._update_stack_actions()
        self._refresh_stack_preview()

    def _collect_preview_inputs(self) -> dict | None:
        raw = self._raw_pcm
        if raw is None:
            return None
        bypass = self.btn_bypass.isChecked()
        edit = self._effective_audio_edit()
        request = None if bypass else self._timepitch_request
        denoise = None if bypass else self._denoise_preview
        duration = self._timepitch_base_duration or self._duration
        if bypass and ClipAuditionCoordinates.from_edit(self._model_audio_edit).maps:
            duration = raw.shape[1] / self._pcm_rate
        content_key = (
            # Keep the effects snapshot in the job, and only its revision in the key.
            self._preview_spec_key, steps_signature(edit), denoise, request,
            self._clip_gain_fade, duration, bypass,
            self._clip_fx_revision if self._clip_fx is not None and not bypass else None,
        )
        key = (content_key, self._preview_generation)
        return {
            "key": key,
            "raw": raw, "rate": self._pcm_rate, "duration": duration,
            "edit": copy_audio_edit(edit), "denoise_preview": denoise, "bypass": bypass,
            "timepitch_request": request,
            "clip_gain_fade": self._clip_gain_fade,
            "clip_fx": None if bypass else self._clip_fx,
            "plan": None,
        }

    def _compute_preview(self, inputs: dict) -> ClipPreviewResult | None:
        from src.features.edit.core.audio_edit import apply_audio_edit_to_pcm

        cancelled = inputs.get("cancelled", lambda: False)
        duration = inputs["duration"]
        rate = inputs["rate"]
        edit = inputs["edit"]
        request = inputs["timepitch_request"]
        if request is not None:
            raw = inputs["raw"]
            edit, step = make_timepitch_draft(edit, request, raw.shape[1], tuple(range(raw.shape[0])))
            edit, _warnings = remap_audio_edit_for_length_steps(edit, [step])
            edited = render_audio_edit_full(raw, edit, sample_rate=rate, cancel_check=cancelled)
            duration = edited.shape[1] / rate
        else:
            base = self._compute_base_pcm(inputs)
            edited = apply_audio_edit_to_pcm(base, suffix_audio_edit(edit), sample_rate=rate) if edit else base
        if cancelled():
            return None
        edited = render_audition_output(
            edited, rate, duration, inputs["clip_gain_fade"], inputs["clip_fx"],
            cancelled, time_pitch_draft=request is not None)
        if cancelled():
            return None
        bins = min(8192, max(64, edited.shape[1] // 64))
        mins, maxs, rmss = compute_waveform_stats(edited, bins)
        if cancelled():
            return None
        waveform_envelope = None
        if not inputs.get("bypass", False):
            points = canvas_points_from_envelope(
                envelope_nodes(edit), duration, max(1, round(duration * AUDIO_REFERENCE_SAMPLE_RATE)),
            )
            waveform_envelope = tuple((point["t"], point["db"], point["interpolation"]) for point in points)
        report = self._measure_cleanliness(edited, rate)
        if cancelled():
            return None
        return ClipPreviewResult(
            inputs["key"], edited, rate, duration, mins, maxs, rmss, report,
            ClipAuditionCoordinates.from_edit(edit).maps,
            waveform_envelope)

    def _compute_base_pcm(self, inputs: dict) -> np.ndarray:
        from src.features.edit.core.audio_edit import render_pre_render_steps

        raw = inputs["raw"]
        edit = inputs["edit"]
        plan = inputs["plan"]
        cancelled = inputs.get("cancelled", lambda: False)
        if plan is not None and plan.blocked_reason:
            raise ValueError(plan.blocked_reason)
        base = plan.lookup_cached() if plan is not None else None
        if base is None:
            base = (render_pre_render_steps(raw, edit, sample_rate=inputs["rate"], cancel_check=cancelled)
                    if requires_pre_render(edit) else raw)
        if inputs["denoise_preview"] is None or cancelled():
            return base
        from src.features.edit.core.audio_denoise import denoise_spectral_subtraction

        noise_range, target_range, reduction_db, nfft = inputs["denoise_preview"]
        return denoise_spectral_subtraction(
            base, inputs["rate"], noise_range, target_range, reduction_db, nfft=nfft,
            progress=lambda _done: not cancelled())

    @staticmethod
    def _measure_cleanliness(pcm: np.ndarray, rate: int) -> dict:
        """Measure sample peak, RMS and noise floor of processed PCM."""
        peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
        rms = math.sqrt(float(np.einsum("cn,cn->", pcm, pcm, dtype=np.float64)) / pcm.size) if pcm.size else 0.0
        report: dict = {
            "peak_db": 20.0 * math.log10(peak) if peak > 0 else -120.0,
            "rms_db": 20.0 * math.log10(rms) if rms > 0 else -120.0,
        }
        # 噪声底：50 ms 窗 RMS 的第 10 百分位（cheap、单调、可复现）。
        window = max(1, int(round(0.05 * rate)))
        if pcm.shape[1] >= window * 2:
            block_count = pcm.shape[1] // window
            blocks = pcm[:, :block_count * window].reshape(pcm.shape[0], block_count, window)
            # Average channel powers so inverted stereo does not read as silence.
            energy = np.einsum("cbn,cbn->b", blocks, blocks, dtype=np.float64)
            block_rms = np.sqrt(energy / (pcm.shape[0] * window))
            floor = float(np.percentile(block_rms, 10))
            report["noise_floor_db"] = 20.0 * math.log10(floor) if floor > 0 else -120.0
        else:
            report["noise_floor_db"] = report["rms_db"]
        return report

    @staticmethod
    def _measure_loudness(
        pcm: np.ndarray, rate: int, cancelled: Callable[[], bool],
    ) -> dict:
        from src.features.edit.core.loudness_analyzer import LoudnessAnalysisCancelled, analyze_loudness

        try:
            loudness = analyze_loudness(pcm, float(rate), progress=lambda _fraction: not cancelled())
        except LoudnessAnalysisCancelled:
            return {}
        report = {"true_peak_dbtp": float(loudness.true_peak_dbtp)}
        if loudness.backend_available:
            report["integrated_lufs"] = float(loudness.integrated_lufs)
        return report

    def _note_preview_failure(self, message: str) -> None:
        """失败留痕（线程安全：文本只做提示，不触控件）。"""
        self._preview_failure_note = str(message)

    def _on_preview_ready(self, result: ClipPreviewResult) -> None:
        # 代际校验：切片段/重连/退出后旧结果不得覆盖新窗口。
        if result.key != self._preview_request_key or result.key[1] != self._preview_generation:
            return
        if self._raw_pcm is None:
            return
        self._publish_preview(result)

    def _on_preview_failed(self, key: tuple, error: str) -> None:
        if key == self._preview_request_key and key[1] == self._preview_generation:
            self._note_preview_failure(error)
            self.set_status_message(I18N.tr(
                "mixer.clip_editor.preview_failed", "Audio preview failed · {error}"
            ).format(error=error))

    def _publish_preview(self, result: ClipPreviewResult | None) -> None:
        if result is None:
            return
        previous = self._last_preview
        self._last_preview = result
        self._cancel_spectrum()
        coordinates = ClipAuditionCoordinates(result.length_maps)
        prior_coordinates = self._audition_coordinates
        preserve_view = math.isclose(result.duration, self.canvas._duration)
        playhead = self._playhead
        if previous is None or previous.edited is not result.edited:
            if self._band_preview is not None:
                self._audition.stop()
            if coordinates != prior_coordinates:
                self._audition.load(
                    result.edited, result.sample_rate,
                    position_map=lambda sample: prior_coordinates.map_to(coordinates, sample))
                playhead = prior_coordinates.map_to(coordinates, round(playhead * self._pcm_rate)) / self._pcm_rate
            else:
                self._audition.load(result.edited, result.sample_rate)
        self._pcm = result.edited
        self.canvas.setWaveformSamples(result.edited, baked_envelope=result.waveform_envelope)
        self.btn_play.setEnabled(self._audition.has_audio())
        self._audition_coordinates = coordinates
        fade_times = (self.canvas._fade_in_sec, self.canvas._fade_out_sec)
        self.load_audio_data(
            result.sample_rate, result.duration, result.mins, result.maxs,
            result.rmss, clip_name=self._clip_name,
            cleanliness_report=result.report or {}, preserve_edit_view=preserve_view)
        self._refresh_lufs_ui()
        self._sync_channel_polarity()
        self.canvas._fade_in_sec, self.canvas._fade_out_sec = fade_times
        self._schedule_spectrum()
        self._request_loudness()
        if not preserve_view or coordinates != prior_coordinates:
            self._project_overlays(coordinates)
        self.update_playhead(playhead)
        if self._timepitch_request is not None and not self.btn_bypass.isChecked():
            if self._validated_timepitch != self._timepitch_request:
                self.set_status_message(I18N.tr(
                    "mixer.clip_editor.timepitch_ready", "Speed and pitch preview ready · {duration:.3f} s"
                ).format(duration=result.duration))
            self._validated_timepitch = self._timepitch_request
            self.set_pending_timepitch(self._pending_timepitch)
        peak_db = (result.report or {}).get("peak_db")
        self.peak_val_lbl.setText(
            f"{peak_db:+.1f} dBFS" if isinstance(peak_db, (int, float)) else "—")
        note = getattr(self, "_preview_failure_note", "")
        if note:
            self.info_label.setText(note)

    def _on_process_step_toggled(self, step_id: str, enabled: bool) -> None:
        # 生产路径：旁路走工程命令（原型的本地快照栈不参与）；命令落地后
        # sync_audio_edit 全量刷新机架。
        self.processStepToggled.emit(step_id, enabled)

    def _on_process_step_removed(self, step_id: str) -> None:
        self.processStepRemoved.emit(step_id)

    def _clear_process_stack(self) -> None:
        self.stack_clear_requested.emit()

    def _push_process_step(self, step_type: str, label: str, detail: str,
                           color: str, params: dict) -> None:
        # 生产路径：机架只由工程模型驱动（demo 本地入栈不再参与）。
        return

    # ------------------------------------------------------------------
    # P1-6：噪声采集与降噪预览（试听态）
    # ------------------------------------------------------------------
    def set_noise_profile(self, profile: Any) -> None:
        self._noise_profile = profile
        self._has_noise_print = profile is not None
        if profile is None:
            self.noise_status_lbl.setText(I18N.tr("mixer.clip_editor.no_noise_print", "No noise print"))
            self.noise_status_lbl.setToolTip("")

    def _emit_capture_noise(self) -> None:
        if not self.canvas._sel_t1 > self.canvas._sel_t0 >= 0.0:
            self.set_status_message(I18N.tr(
                "mixer.clip_editor.noise_selection_required", "Select a section of background noise first"))
            return
        self.noisePrintCaptured.emit(*self.canvas.getSelection()[:2])

    def set_denoise_preview(self, noise_range: tuple[int, int],
                            target_range: tuple[int, int],
                            reduction_db: float, nfft: int = 2048) -> None:
        """降噪预览：原始 PCM → 谱减 → 处理栈 → 波形/频谱/试听。"""
        self._denoise_preview = (noise_range, target_range, reduction_db, nfft)
        self._refresh_stack_preview()

    def clear_denoise_preview(self) -> None:
        self._denoise_preview = None
        self._refresh_stack_preview()

    def set_timepitch_request(self, request: tuple | None) -> None:
        """P1-1/P1-2 · 变速/变调预览请求（DSP 在 worker 内计算）。

        ``request``: ``(lo, hi, ratio_pct, semitones, preserve)`` 选区与
        参数快照（None=取消预览）。进入预览时记录基准时长，清除时恢复
        ——发布结果携带合同长度（load_audio_data 依 result.duration 更
        新画布与 ``_duration``）。
        """
        if self._timepitch_request == request:
            return
        if request is not None and self._timepitch_request is None:
            self._timepitch_base_duration = self._duration
        if request is None and self._timepitch_request is not None \
                and self._timepitch_base_duration is not None:
            self._duration = self._timepitch_base_duration
            self._timepitch_base_duration = None
        self._timepitch_request = request
        self._validated_timepitch = None
        self.set_pending_timepitch(request)
        self._refresh_stack_preview()

    def clear_timepitch_preview(self) -> None:
        self.set_timepitch_request(None)

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
        if self._pcm is None or not self._audition.has_audio():
            return
        if self._audition.is_playing:
            if self._audition.is_paused:
                self._audition.resume()
            else:
                self._audition.pause()
            self._update_audition_button(self._audition.is_playing and not self._audition.is_paused)
            return
        self._restore_band_preview()
        self._audition.set_skip_range(None)
        t0, t1, _, _ = self.canvas.getSelection()
        start = self._audition_start_sample()
        rate = self._pcm_rate
        if t1 - t0 > 0.01:
            sel0, sel1 = int(round(t0 * rate)), int(round(t1 * rate))
            if not sel0 <= start < sel1:
                start = sel0
        self._update_audition_loop()
        if not self._audition.play(start):
            self._update_audition_button(False)
            self._show_device_failure()

    def _show_device_failure(self) -> None:
        self.set_status_message(I18N.tr(
            "mixer.clip_editor.no_audio_device", "No audio device available for audition"))

    def _toggle_loop(self, checked: bool) -> None:
        super()._toggle_loop(checked)
        self._update_audition_loop()

    def _update_audition_loop(self) -> None:
        if self._pcm is None or self._skip_selection_mode or self._band_preview is not None:
            return
        t0, t1 = self.canvas._sel_t0, self.canvas._sel_t1
        if t1 > t0 >= 0.0:
            start, end = round(t0 * self._pcm_rate), round(t1 * self._pcm_rate)
            self._audition.set_loop(self._is_looping, start, end)
        else:
            self._audition.set_loop(self._is_looping)

    def _stop_playback(self) -> None:
        self._pending_audition_seek = None
        # 停止回到开始试听的游标（会话负责回到 return_sample）。
        self._skip_selection_mode = False
        self._audition.set_skip_range(None)
        self._audition.stop()
        self._is_playing = False

    def _toggle_skip_play(self) -> None:
        """跳过选区试听 (Ctrl+Space)：从选区前 1.2 s 起播，越过 [t0, t1)。

        P0-4 · 跳过在供块层生效（``ClipAuditionSession.set_skip_range``）：
        选区内容不进设备缓冲，与旧的事后 seek 不同，选区开头不会先送
        入设备再跳；播放头经块记录映射直接落在选区后。
        """
        if self._pcm is None or not self._audition.has_audio():
            return
        if self._band_preview is not None:
            self._audition.stop()
            self._restore_band_preview()
        t0, t1, _, _ = self.canvas.getSelection()
        if not self.canvas._sel_t1 > self.canvas._sel_t0 >= 0.0 or t1 - t0 <= 0.01:
            self._toggle_play()
            return
        rate = self._pcm_rate
        self._skip_selection_mode = True
        start = max(0, int(round((t0 - 1.2) * rate)))
        s0, s1 = int(round(t0 * rate)), int(round(t1 * rate))
        self._audition.set_loop(False)
        self._audition.set_skip_range(s0, s1)
        if not self._audition.play(start, end_sample=s1 + round(1.2 * rate)):
            self._skip_selection_mode = False
            self._audition.set_skip_range(None)

    def _on_audition_playing_changed(self, playing: bool) -> None:
        self._is_playing = playing
        self._update_audition_button(playing)
        self.playStateChanged.emit(playing)
        if not playing:
            self._skip_selection_mode = False
            self._audition.set_skip_range(None)
            if self._pcm is not None and self._pending_audition_seek is None:
                offset = self._band_preview[0] if self._band_preview is not None else 0
                self.update_playhead((self._audition.return_sample() + offset) / self._pcm_rate)
            self._restore_band_preview()
            self.update_output_meter(-60.0, -60.0)

    def _update_audition_button(self, playing: bool) -> None:
        self.btn_play.setChecked(playing)
        text = (I18N.tr("mixer.clip_editor.pause_audition", "Pause") if playing
                else I18N.tr("mixer.clip_editor.play", "Play"))
        self.btn_play.setText("❚❚" if playing else f"▶ {text}")
        self.btn_play.setAccessibleName(text)

    def _on_audition_position(self, sample: int) -> None:
        if not self._audition.is_playing or self._pcm is None:
            return
        if self._pending_audition_seek is not None:
            self._update_pcm_meters(sample)
            return
        rate = self._pcm_rate
        offset = self._band_preview[0] if self._band_preview is not None else 0
        sec = (sample + offset) / rate
        start, end = self.canvas._view_start, self.canvas._view_end
        span = end - start
        # Follow only when playback crosses the visible edge, so navigation can
        # inspect an earlier section while audio continues outside the viewport.
        if (start <= self._playhead <= end < sec and not self.minimap._drag_mode
                and not self.canvas._drag_mode and abs(self.canvas._pan_vel_sec) <= 1e-4):
            new_start = min(self._duration - span, sec - span * 0.15)
            self._sync_viewport(max(0.0, new_start), max(0.0, new_start) + span)
        self.update_playhead(sec)
        self._update_pcm_meters(sample)

    def _update_pcm_meters(self, sample: int) -> None:
        pcm = self._band_preview[1] if self._band_preview is not None else self._pcm
        if pcm is None or pcm.shape[1] == 0:
            return
        half = max(1, int(round(0.012 * self._pcm_rate)))
        lo = max(0, sample - half)
        hi = min(pcm.shape[1], sample + half)
        window = pcm[:, lo:hi]
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
        self._request_audition_seek(
            sec, dragging=self.canvas.is_selecting() or self.canvas._drag_mode == "playhead")

    def start_band_preview(self, start: int, pcm: np.ndarray) -> bool:
        self._audition.stop()
        self._audition.load(pcm, self._pcm_rate)
        self._audition.set_skip_range(None)
        self._audition.set_loop(False)
        self._band_preview = (start, pcm)
        if self._audition.play(0):
            return True
        self._restore_band_preview()
        return False

    def _restore_band_preview(self) -> None:
        if self._band_preview is None:
            return
        self._band_preview = None
        if self._pcm is not None:
            self._audition.load(self._pcm, self._pcm_rate)

    def _on_minimap_playhead_moved(self, sec: float) -> None:
        super()._on_minimap_playhead_moved(sec)
        self._request_audition_seek(sec, dragging=self.minimap._drag_mode == "playhead")

    def _request_audition_seek(self, sec: float, *, dragging: bool) -> None:
        if dragging:
            if self._audition.is_playing or self._pending_audition_seek is not None:
                # The visual cursor follows the mouse. Reopen the audio buffer only
                # for the settled position, since device start/stop can block the UI.
                self._pending_audition_seek = sec
            return
        self._pending_audition_seek = None
        if self._audition.is_playing:
            offset = self._band_preview[0] if self._band_preview is not None else 0
            self._audition.seek(round(sec * self._pcm_rate) - offset)

    def _finish_audition_seek(self) -> None:
        if self._pending_audition_seek is not None:
            self._request_audition_seek(self._pending_audition_seek, dragging=False)

    # ------------------------------------------------------------------
    # 过零吸附（P0-2 §2.4）
    # ------------------------------------------------------------------
    def _finish_selection_drag(self, fixed_edge: float | None) -> None:
        if self.canvas._sel_t1 > self.canvas._sel_t0 >= 0.0:
            self._on_pcm_selection_zero_snap(*self.canvas.getSelection(), fixed_edge=fixed_edge)
        if self._is_looping:
            self._update_audition_loop()

    def _on_pcm_selection_changed(self, t0: float, t1: float, fl: float, fh: float) -> None:
        if self._suppress_zero_snap or self.canvas.is_selecting():
            return
        self._on_pcm_selection_zero_snap(t0, t1, fl, fh)
        if self._is_looping:
            self._update_audition_loop()

    def _on_pcm_selection_zero_snap(
        self, t0: float, t1: float, fl: float, fh: float, *, fixed_edge: float | None = None,
    ) -> None:
        if (not self._zero_snap_armed or self._pcm is None
                or self._suppress_zero_snap or self.canvas.is_selecting() or t1 - t0 <= 0.0):
            return
        rate = self._pcm_rate
        total = self._pcm.shape[1]
        tol = max(1, int(round(0.003 * rate)))
        s0 = max(0, min(total - 1, int(round(t0 * rate))))
        s1 = max(s0 + 1, min(total, int(round(t1 * rate))))
        n0 = (find_zero_crossing(self._pcm, s0, tolerance_samples=tol, bounds=(0, total))
              if t0 != fixed_edge else None)
        n1 = (find_zero_crossing(self._pcm, s1 - 1, tolerance_samples=tol, bounds=(0, total))
              if t1 != fixed_edge else None)
        snapped0 = n0 / rate if n0 is not None else t0
        snapped1 = n1 / rate if n1 is not None else t1
        if (abs(snapped0 - t0) > 1e-9 or abs(snapped1 - t1) > 1e-9) and snapped1 - snapped0 > 1e-9:
            self._suppress_zero_snap = True
            try:
                self.canvas.setSelection(
                    snapped0, snapped1, fl, fh, spectral=self.canvas._is_2d_spectral_sel)
            finally:
                self._suppress_zero_snap = False

    def _disable_prototype_controls(self) -> None:
        unavailable = I18N.tr("mixer.clip_editor.unavailable", "This tool requires an audio processing backend.")
        self.btn_spectral.setChecked(False)
        self.btn_snap_zero.setChecked(False)
        self.btn_env_smooth.hide()
        self.fade_preset_buttons[0.02].setText(I18N.tr("mixer.clip_editor.microfade", "20 ms"))
        self.btn_apply_lufs.setText(I18N.tr("mixer.clip_editor.match_loudness", "Match target loudness"))
        buttons_by_text = {
            "应用至选区", "重置", "人声", "流媒体", "广播", "电影",
        }
        widgets = (
            self.btn_play, self.btn_loop, self.btn_tool_brush, self.btn_tool_envelope,
            self.btn_snap_zero, self.btn_spectral, self.btn_denoise,
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
        self.action_capture_noise.setEnabled(False)
        self.action_normalize.setEnabled(False)
        self._apply_gain_button: QPushButton | None = None
        self._history_buttons: list[QPushButton] = []
        # P0-3：选区处理（静音/标准化）随 PCM 与时间线片段可用性启用。
        self._selection_edit_buttons: list[QPushButton] = []
        for button in self.findChildren(QPushButton):
            if button.text() == "■":
                self._stop_button = button
                button.setEnabled(False)
            elif button.text() in buttons_by_text:
                button.setEnabled(False)
                button.setToolTip(unavailable)
            elif button.text() in ("静音", "删除", "修复"):
                self._selection_edit_buttons.append(button)
                button.setEnabled(False)
                button.setToolTip(unavailable)
            elif button is self.time_pitch_action_button:
                button.setEnabled(False)
                button.setToolTip(unavailable)
            elif button.text() == "应用增益到选区":
                self._apply_gain_button = button
                button.setText(I18N.tr("mixer.clip_editor.apply_gain", "Apply clip gain"))
                button.setToolTip(self.gain_knob.toolTip())
            elif button.text() in {"↶", "↷"}:
                self._history_buttons.append(button)
            if not button.isEnabled():
                button.setStyleSheet(
                    "QPushButton:disabled { color: #797981; background-color: #29292D; border-color: #323237; }")

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
        self._clip_envelope_available = not source_mode
        self.canvas._env_enabled = self._clip_envelope_available
        self.canvas._env_points = []
        self._envelope_projection = None
        self._set_tool_mode("select")
        if self._mode_label is not None:
            self._mode_label.setText(
                I18N.tr("mixer.clip_editor.source_mode", "● Source audio")
                if source_mode else I18N.tr("mixer.clip_editor.timeline_mode", "● Timeline clip")
            )
        for button in self._history_buttons:
            button.setEnabled(not source_mode)

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
        self.canvas.fade_controls_editable = self._fade_editable
        if locked:
            self._pending_fade = None
            self.canvas.cancel_fade_drag()
        for control in (self.fi_pill, self.fo_pill, *self.fade_preset_buttons.values(),
                        *self._fade_curve_btns.values()):
            control.setEnabled(self._fade_editable)
        self.canvas._fade_in_sec = fade_in
        self.canvas._fade_out_sec = fade_out
        self.canvas.setFadeCurves(
            _DISPLAY_FADE_CURVES.get(fade_in_curve, fade_in_curve),
            _DISPLAY_FADE_CURVES.get(fade_out_curve, fade_out_curve),
        )
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
        if self.sender() is self.canvas:
            if self.canvas.selection_gain_editable and t1 > t0 and abs(gain_db) >= 0.05:
                self.canvas.setPreviewGain(0.0)
                self.selection_gain_requested.emit(t0, t1, gain_db, self.canvas._edit_channel)
        else:
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
        if not self._fade_editable:
            return
        if seconds == 0.02:
            self.microfade_requested.emit()
        else:
            super()._apply_symmetric_fade(seconds)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.canvas and isinstance(event, QMouseEvent):
            if (event.type() == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.LeftButton and self._pending_fade is not None):
                fade, self._pending_fade = self._pending_fade, None
                self.fadeApplied.emit(*fade)
        return super().eventFilter(watched, event)


class ProjectClipEditor(QWidget):
    """A dock body that opens an audio source or a clip from the active sequence."""

    gain_committed = pyqtSignal(str, float)
    fade_committed = pyqtSignal(str, int, int)
    sample_fade_committed = pyqtSignal(str, int, int)
    fade_curve_committed = pyqtSignal(str, str)
    fade_end_curve_committed = pyqtSignal(str, str, str)
    # P0-2：试听占用输出设备时通知宿主暂停时间线播放。
    audition_active_changed = pyqtSignal(bool)
    # P0-3：处理栈提交（clip_id, audio_edit, label）——由宿主执行工程命令。
    audio_edit_committed = pyqtSignal(str, object, str)
    # §6.2：改长度事务（计划已通过冲突检查；控制器构建项目/序列命令）。
    length_change_committed = pyqtSignal(str, object, str)
    effect_add_requested = pyqtSignal(str, str)
    effect_settings_requested = pyqtSignal(str, str, str)
    effect_enabled_changed = pyqtSignal(str, str, bool)
    effect_reorder_requested = pyqtSignal(str, str, int)
    effect_remove_requested = pyqtSignal(str, str)
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
            #ClipEditorEmpty { color: #AAABB5; }
        """)
        self._clip: ClipInstance | None = None
        self._timeline: TimelineSequence | None = None
        self._timeline_id: str | None = None
        self._project: EditProject | None = None
        self._project_id: str | None = None
        self._panel: _TimelineClipPanel | None = None
        self._dock_active = True
        self._analysis_worker: ClipPreviewWorker | None = None
        self._analysis_sequence = 0
        self._analysis_callback: Callable[[object, str | None], None] | None = None
        self._selection_effect_dialog: SelectionEffectDialog | None = None
        self._selection_effect_target: tuple[int, int, int] | None = None
        self._selection_effect_cache: tuple[tuple, _SelectionEffectRender] | None = None
        self._selection_effect_work = False
        self._selection_effect_audio: np.ndarray | None = None
        self._pcm_callback: Callable[[ClipPcmResult], None] | None = None
        self._native_pcm_worker: ClipPreviewWorker | None = None
        self._native_pcm_serial = 0
        self._source_detail_worker: ClipPreviewWorker | None = None
        self._source_detail_key: tuple | None = None
        self._windowed_waveform_spec: ClipAudioRenderSpec | None = None
        self._source_detail_timer = QTimer(self)
        self._source_detail_timer.setSingleShot(True)
        self._source_detail_timer.setInterval(85)
        self._source_detail_timer.timeout.connect(self._request_source_detail)
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
        self._tabbed_with_timeline = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._title = ClipEditorElidedLabel(Qt.TextElideMode.ElideMiddle)
        self._title.setObjectName("ClipEditorTitle")
        self._title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._title.setMaximumWidth(300)
        self._title.setStyleSheet("color: #E2E2E8; font-size: 10px; font-weight: 600; border: none;")
        self._status = ClipEditorElidedLabel()
        self._status.setObjectName("ClipEditorStatus")
        self._status.setStyleSheet("color: #AAABB5; font-size: 10px; border: none;")
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

    def set_dock_mode(self, *, tabbed: bool) -> None:
        self._tabbed_with_timeline = tabbed

    def set_dock_active(self, active: bool) -> None:
        self._dock_active = active
        if self._panel is not None:
            self._panel.set_dock_active(active)
        if active:
            self._schedule_source_detail()
        else:
            self._suspend_view()

    def show_unified_context_menu(self, global_pos: QPoint) -> None:
        if self._panel is not None and not self._scroll.isHidden():
            self._panel.canvas.show_context_menu(global_pos)
            return
        menu = QMenu(self)
        self._append_dock_menu_actions(menu)
        if menu.actions():
            menu.exec(global_pos)
        menu.deleteLater()

    def _append_dock_menu_actions(self, menu: QMenu) -> None:
        dock = find_parent_dock(self)
        if dock is None:
            return
        if menu.actions():
            menu.addSeparator()
        if not dock.isFloating() and not self._tabbed_with_timeline:
            menu.addAction(I18N.tr("mixer.clip_editor.tab", "Dock with timeline"), self.tab_requested.emit)
        toggle_floating = self.tab_requested.emit if dock.isFloating() else self.float_requested.emit
        add_dock_management_actions(menu, dock, toggle_floating=toggle_floating)

    def focus_waveform(self) -> None:
        if self._panel is not None and not self._scroll.isHidden():
            self._panel.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _ensure_panel(self) -> _TimelineClipPanel:
        if self._panel is None:
            self._panel = _TimelineClipPanel()
            self._panel.set_dock_active(self._dock_active)
            self._panel.canvas.spectralViewportChanged.connect(self._schedule_source_detail)
            status_layout = self._panel._status_layout
            selection_index = status_layout.indexOf(self._panel.sel_info_lbl)
            status_layout.insertWidget(selection_index + 1, self._status, 1)
            status_layout.insertWidget(selection_index, self._title)
            self._panel._status_hook = self._status.setText
            self._panel.btn_timeline.clicked.connect(self.timeline_requested.emit)
            rack = self._panel.effect_rack
            rack.add_requested.connect(self.effect_add_requested.emit)
            rack.settings_requested.connect(self.effect_settings_requested.emit)
            rack.enabled_changed.connect(self.effect_enabled_changed.emit)
            rack.reorder_requested.connect(self.effect_reorder_requested.emit)
            rack.remove_requested.connect(self.effect_remove_requested.emit)
            self._panel.canvas.populateContextMenu.connect(rack.add_to_context_menu)
            self._panel.canvas.populateContextMenu.connect(self._append_dock_menu_actions)
            self._panel.canvas.populateContextMenu.connect(self._append_offline_processor_actions)
            self._panel.canvas.selectionChanged.connect(self._refresh_selection_effect_target)
            self._panel.canvas.editChannelChanged.connect(self._refresh_selection_effect_target)
            self._panel.time_pitch_draft_changed.connect(self._refresh_selection_effect_target)
            self._panel.playStateChanged.connect(self._on_selection_effect_play_state)
            self._panel.gain_requested.connect(self._on_gain_requested)
            self._panel.microfade_requested.connect(self._on_microfade_requested)
            self._panel.selection_gain_requested.connect(self._on_selection_gain_requested)
            self._panel.fadeApplied.connect(self._on_fade_requested)
            self._panel.fadeCurveChanged.connect(self._on_fade_curve_requested)
            self._panel.canvas.fadeCurveCommitted.connect(self._on_fade_end_curve_requested)
            self._panel.undoRequested.connect(self._on_undo_requested)
            self._panel.redoRequested.connect(self._on_redo_requested)
            self._panel._audition.playing_changed.connect(self.audition_active_changed.emit)
            # P0-3 控件映射：静音/标准化生成步骤；机架旁路/删除/清空走命令。
            self._panel.silenceRequested.connect(self._on_silence_requested)
            self._panel.normalizeRequested.connect(self._on_normalize_requested)
            self._panel.processStepToggled.connect(self._on_stack_step_toggled)
            self._panel.processStepRemoved.connect(self._on_stack_step_removed)
            # §6.2：改长度提交（变速/音高、删除区间、裁剪）走项目/序列事务。
            self._panel.time_pitch_commit_requested.connect(
                self._on_time_pitch_commit_requested)
            self._panel.trimRequested.connect(self._on_trim_requested)
            self._panel.deleteRangeRequested.connect(self._on_delete_requested)
            # §5 画笔：频谱修复提交（spectral_repair 预渲染步骤）。
            self._panel.repair_commit_requested.connect(
                self._on_repair_commit_requested)
            self._panel.stack_clear_requested.connect(self._on_stack_cleared)
            # P1-4：包络节点提交（画布拖拽结束/增删时各发一次）。
            self._panel.envelopeChanged.connect(self._on_envelope_changed)
            # P1-5：标记点与工程级反极性提交；声道静音/独奏保持监听态。
            self._panel.markersChanged.connect(self._on_markers_changed)
            self._panel.channelStateChanged.connect(self._on_channel_state_changed)
            # P1-6：噪声采集/降噪预览与响度匹配提交。
            self._panel.noisePrintCaptured.connect(self._on_noise_print_captured)
            self._panel.denoiseRequested.connect(self._on_denoise_requested)
            self._panel.lufsNormalizeRequested.connect(self._on_lufs_normalize_requested)
            # P2-7：变速/音高预览（长度事务提交待输出映射合同）。
            self._panel.timePitchApplied.connect(self._on_time_pitch_requested)
            # P1-3：语图右键的 heal/衰减入修复处理器；仅试听该频段走
            # 带通试听（此前生产中信号未连接，菜单项被吞掉）。
            self._panel.spectralRepairRequested.connect(
                self._on_spectral_repair_requested)
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
        self._close_selection_effect()
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
        self._close_selection_effect()
        self._clip = None
        self._timeline = None
        self._timeline_id = None
        self._sample_rate = AUDIO_REFERENCE_SAMPLE_RATE
        self._prepare_source(asset, project)
        self._request_waveform()
        self._sync_gain_fade()
        self._sync_effect_rack()

    def _prepare_source(
        self, asset: MediaAsset, project: EditProject | None, waveform_asset_id: str | None = None,
    ) -> None:
        panel = self._ensure_panel()
        panel.set_source_mode(self._clip is None)
        panel.cancel_view_interaction()
        if self._extractor is None:
            self._extractor = WaveformExtractor.instance()
            self._extractor.waveform_ready.connect(self._waveform_ready)
            self._extractor.extraction_failed.connect(self._waveform_failed)
        if self._pcm_provider is None:
            self._pcm_provider = ClipPcmProvider.instance()
            self._pcm_provider.pcm_failed.connect(self._on_pcm_failed)
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
        spec = self._resolve_pcm_spec()
        if source_changed or (spec is not None and spec.cache_key() != self._pcm_spec_key):
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
        if previous is not clip or self._locked != track.is_locked:
            self._stop_selection_effect()
            self._selection_effect_cache = None
            self._cancel_analysis()
        self._clip = clip
        self._locked = track.is_locked
        label_color = QColor(clip.label_color or "")
        if not label_color.isValid():
            label_color = QColor(resolve_track_color(track.track_id, track.color))
        self._panel.canvas._env_accent = envelope_accent(label_color)
        self._panel.canvas.update()
        geometry_changed = (
            self._source_changed
            or old_fps != self._fps
            or (previous.source_start, previous.source_duration, previous.timeline_duration,
                previous.source_sample_offset, previous.audio_sample_offset, previous.speed,
                previous.freeze_source_frame) != (
                clip.source_start, clip.source_duration, clip.timeline_duration,
                clip.source_sample_offset, clip.audio_sample_offset, clip.speed, clip.freeze_source_frame,
            )
        )
        if geometry_changed:
            self._source_changed = False
            self._request_waveform()
        self._sync_gain_fade()
        self._sync_effect_rack()
        if self._panel is not None:
            self._panel.set_stack_editable_hint(not self._locked)
            self._panel.sync_audio_edit(
                self._clip.audio_edit if self._clip is not None else None,
                output_total_samples=self._output_total_samples())
        self._refresh_selection_effect_target()

    def _sync_effect_rack(self) -> None:
        if self._panel is not None:
            self._panel.effect_rack.set_clip(self._clip, editable=not self._locked)
            self._panel.set_clip_effects(self._clip, self._fps)

    def reset(self) -> None:
        self._close_selection_effect()
        self._cancel_analysis()
        self._cancel_pcm_request()
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
        self._pcm_load_failed = False
        self._last_pcm = None
        self._pcm_applied = False
        if self._panel is not None:
            self._panel.set_source_mode(True)
            self._panel.cancel_view_interaction()
            self._panel.detach_pcm()
            self._sync_effect_rack()
        self._scroll.hide()
        self._title.setText("")
        self._title.hide()
        self._status.setText("")
        self._empty.show()

    def _on_undo_requested(self) -> None:
        if self._clip is not None:
            self.undo_requested.emit()

    def _on_redo_requested(self) -> None:
        if self._clip is not None:
            self.redo_requested.emit()

    def _suspend_view(self) -> None:
        self._close_selection_effect()
        self._cancel_analysis()
        self._source_detail_timer.stop()
        self._source_detail_key = None
        if self._source_detail_worker is not None:
            self._source_detail_worker.cancel()
        if self._panel is not None:
            self._panel.cancel_view_interaction()
            self._sync_gain_fade()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._schedule_source_detail()

    def hideEvent(self, event: QHideEvent) -> None:
        self._suspend_view()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        """P0 · 关闭编辑器：结束预览 worker（可结束，在途结果不发布）。"""
        self._close_selection_effect()
        if self._panel is not None:
            self._panel.shutdown_preview()
        self._cancel_analysis()
        self._cancel_pcm_request()
        if self._analysis_worker is not None:
            self._analysis_worker.finish()
            self._analysis_worker = None
        if self._native_pcm_worker is not None:
            self._native_pcm_worker.finish()
            self._native_pcm_worker = None
        if self._source_detail_worker is not None:
            self._source_detail_worker.finish()
            self._source_detail_worker = None
        super().closeEvent(event)

    def _request_waveform(self) -> None:
        self._stop_selection_effect()
        self._selection_effect_cache = None
        self._cancel_analysis()
        self._cancel_pcm_request()
        asset = self._asset
        if asset is None or self._panel is None:
            return
        self._pcm_spec_key = None
        self._pcm_load_failed = False
        self._last_pcm = None
        self._pcm_applied = False
        self._panel.detach_pcm()
        duration = self._clip.timeline_duration / self._fps if self._clip is not None else asset.duration_seconds
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0.0:
            duration = 0.01
        self._panel.load_audio_data(self._sample_rate, duration, [], [], [], clip_name=asset.display_name)
        self._panel.update_playhead(0.0)
        self._panel.peak_val_lbl.setText("—")
        self._panel.sr_badge.setText("—")
        if not (self._waveform_asset_id and self._waveform_path):
            self._status.setText(I18N.tr("mixer.clip_editor.no_waveform", "Source waveform unavailable."))
            self._pcm_load_failed = True
            self._refresh_selection_effect_target()
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
        self._refresh_selection_effect_target()

    def _resolve_pcm_spec(self) -> ClipAudioRenderSpec | None:
        asset = self._asset
        if asset is None:
            return None
        clip = self._clip
        if clip is not None and self._timeline is not None:
            source_asset = self._project.assets.get(clip.asset_id) if self._project is not None else None
            return resolve_clip_audio_spec(self._project, self._timeline, clip, source_asset or asset)
        spec = resolve_asset_audio_spec(self._project, asset)
        if spec is None:
            waveform = self._cached_source_waveform()
            if waveform is not None and waveform.is_complete:
                spec = resolve_asset_audio_spec(
                    self._project, asset, duration_seconds=waveform.duration_seconds)
        return spec

    def _cancel_pcm_request(self) -> None:
        self._source_detail_timer.stop()
        self._source_detail_key = None
        self._windowed_waveform_spec = None
        if self._source_detail_worker is not None:
            self._source_detail_worker.cancel()
        self._native_pcm_serial += 1
        if self._native_pcm_worker is not None:
            self._native_pcm_worker.cancel()
        callback, self._pcm_callback = self._pcm_callback, None
        if callback is not None and self._pcm_provider is not None and self._pcm_spec_key is not None:
            self._pcm_provider.cancel_request(self._pcm_spec_key, callback)

    def _request_pcm(self) -> None:
        from src.features.edit.core.audio_retime import needs_retime

        spec = self._resolve_pcm_spec()
        panel = self._panel
        if spec is None or panel is None or self._pcm_provider is None:
            return
        key = self._pcm_spec_key = spec.cache_key()
        if not full_clip_pcm_available(spec):
            # Keep the full logical duration and source envelope. A bounded
            # display window is not a valid full processing/audition base.
            self._windowed_waveform_spec = spec
            self._pcm_load_failed = True
            message = I18N.tr(
                "mixer.clip_editor.long_source_view",
                "Full source waveform · Zoom for sample detail. Clip Edit audition and processing support up to 300 seconds. The full clip extent is preserved.")
            self._status.setText(message)
            self._status.setToolTip(message)
            panel.btn_play.setToolTip(message)
            self._schedule_source_detail()
            return
        panel._preview_spec_key = key
        panel._pre_render_plan_for_preview = self._build_preview_plan
        clip, project = self._clip, self._project
        if clip is not None and clip.freeze_source_frame is not None:
            self._on_pcm_failed(key, I18N.tr("mixer.clip_editor.frozen_audio", "Frozen clip has no audio playback."))
            return
        if clip is not None and project is not None and needs_retime(clip):
            fps, rate = self._fps, spec.sample_rate
            request_key = (self._native_pcm_serial, key)
            if self._native_pcm_worker is None:
                self._native_pcm_worker = ClipPreviewWorker()
                self._native_pcm_worker.preview_ready.connect(self._on_native_pcm_ready)
                self._native_pcm_worker.preview_failed.connect(self._on_native_pcm_failed)
                self.destroyed.connect(self._native_pcm_worker.finish)
            self._native_pcm_worker.submit_cancellable(
                request_key,
                lambda cancelled: (request_key, rate, render_native_clip_pcm(
                    project, clip, fps, rate, cancelled)))
            return

        def on_pcm(result: ClipPcmResult) -> None:
            if self._pcm_callback is on_pcm and result.spec_key == self._pcm_spec_key:
                self._pcm_callback = None
                self._apply_pcm(result)

        self._pcm_callback = on_pcm
        self._pcm_provider.request(spec, on_pcm)

    def _schedule_source_detail(self) -> None:
        if self._dock_active and self.isVisible() and self._windowed_waveform_spec is not None:
            self._source_detail_timer.start()

    def _request_source_detail(self) -> None:
        spec, panel, clip = self._windowed_waveform_spec, self._panel, self._clip
        if spec is None or panel is None or not self._dock_active or not self.isVisible():
            return
        if clip is not None:
            from src.features.edit.core.audio_retime import needs_retime
            # A source window cannot evaluate a whole stateful/length-changing
            # stack. Keep the existing source envelope clearly labelled.
            if clip.audio_edit or needs_retime(clip):
                return
        canvas = panel.canvas
        scale = spec.sample_count / max(0.01, panel._duration)
        start = max(0, math.floor(canvas._view_start * scale))
        end = min(spec.sample_count, math.ceil(canvas._view_end * scale))
        if end <= start or end - start > 262144:
            self._source_detail_key = None
            if self._source_detail_worker is not None:
                self._source_detail_worker.cancel()
            return
        key = (self._native_pcm_serial, spec.cache_key(), start, end)
        if key == self._source_detail_key:
            return
        self._source_detail_key = key
        if self._source_detail_worker is None:
            self._source_detail_worker = ClipPreviewWorker()
            self._source_detail_worker.preview_ready.connect(self._publish_source_detail)
            self._source_detail_worker.preview_failed.connect(self._source_detail_failed)
            self.destroyed.connect(self._source_detail_worker.finish)
        self._source_detail_worker.submit_cancellable(
            key, lambda cancelled: (key, scale, decode_clip_waveform_window(
                spec, start, end, cancel_check=cancelled)))

    def _publish_source_detail(self, payload: tuple) -> None:
        key, scale, detail = payload
        panel = self._panel
        if key != self._source_detail_key or panel is None or detail is None:
            return
        if not isinstance(detail, ClipWaveformWindow) or detail.spec_key != self._pcm_spec_key:
            return
        panel.canvas.setWaveformWindow(detail.pcm, detail.start_sample / scale, scale)

    def _source_detail_failed(self, key: tuple, error: str) -> None:
        if key == self._source_detail_key:
            self._source_detail_key = None
            self._status.setToolTip(error)

    def _on_native_pcm_ready(self, payload: tuple) -> None:
        key, rate, pcm = payload
        if pcm is not None and key == (self._native_pcm_serial, self._pcm_spec_key):
            self._apply_pcm(ClipPcmResult(key[1], pcm, rate, pcm.shape[0]))

    def _on_native_pcm_failed(self, key: tuple, error: str) -> None:
        if key == (self._native_pcm_serial, self._pcm_spec_key):
            self._on_pcm_failed(key[1], error)

    def _on_pcm_failed(self, key: tuple, error: str) -> None:
        if key != self._pcm_spec_key or self._pcm_applied:
            return
        self._pcm_callback = None
        self._pcm_load_failed = True
        message = I18N.tr("mixer.clip_editor.pcm_failed", "Audio samples could not be loaded.")
        self._status.setText(f"{message} {error}" if error else message)
        self._refresh_selection_effect_target()
        if self._panel is not None:
            self._panel.btn_play.setToolTip(message)

    def _build_preview_plan(self):
        """预览基底沿用预渲染缓存（与播放/导出同一成品）。"""
        try:
            from src.features.edit.core.pcm_render_cache import (
                build_pre_render_plan,
            )
            if (self._clip is None or self._timeline is None
                    or self._project is None):
                return None
            source_asset = self._project.assets.get(self._clip.asset_id)
            return build_pre_render_plan(
                self._project, self._timeline, self._clip,
                source_asset or self._asset)
        except Exception:  # noqa: BLE001 - 计划缺席走本地烘焙
            return None

    def _apply_pcm(self, result: ClipPcmResult) -> None:
        """P0 · 源 PCM 到达：装载原始数据并触发**处理后**预览。

        源 PCM 不再被当作栈处理结果——波形/频谱/计量/试听统一由预览
        worker 以片段内容与代际为键计算发布，重开
        已有静音/降噪栈首次试听即正确版本。
        """
        asset, panel = self._asset, self._panel
        if asset is None or panel is None:
            return
        self._last_pcm = result
        self._pcm_applied = True
        self._pcm_load_failed = False
        pcm, rate = result.pcm, result.sample_rate
        duration = (
            self._clip.timeline_duration / self._fps
            if self._clip is not None and self._fps > 0
            else (asset.duration_seconds or pcm.shape[1] / rate)
        )
        panel._duration = max(0.01, duration)
        panel._preview_spec_key = self._pcm_spec_key
        panel._pre_render_plan_for_preview = self._build_preview_plan
        self._sync_gain_fade()
        self._sync_effect_rack()
        if self._clip is not None:
            panel.set_stack_editable_hint(not self._locked)
            panel.sync_audio_edit(self._clip.audio_edit, self._output_total_samples())
        panel.attach_pcm(pcm, rate)
        panel.update_playhead(0.0)
        self._refresh_selection_effect_target()
        panel._refresh_stack_preview()

    @staticmethod
    def _measure_cleanliness(pcm: np.ndarray, rate: int) -> dict:
        """Measure a complete report for callers outside the preview lifecycle."""
        report = _TimelineClipPanel._measure_cleanliness(pcm, rate)
        if 0 < pcm.shape[1] <= rate * 60:
            try:
                report.update(_TimelineClipPanel._measure_loudness(pcm, rate, lambda: False))
            except (RuntimeError, ValueError, OSError):
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
        if (asset_id == self._waveform_asset_id and not self._pcm_load_failed
                and not self._pcm_applied and not is_waveform_pending(asset_id)):
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
            if not self._pcm_load_failed:
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
        panel.load_audio_data(
            rate, duration, peaks_min, peaks_max, [[0.0] * bins for _ in peaks_max],
            clip_name=asset.display_name, preserve_edit_view=math.isclose(duration, panel.canvas._duration))
        panel.sr_badge.setText(f"{source_rate / 1000:g} kHz" if rate == source_rate else "—")
        peak = max(max(map(max, peaks_max)), -min(map(min, peaks_min)))
        panel.peak_val_lbl.setText(f"{20.0 * math.log10(peak):+.1f} dBFS" if peak > 0.0 else "−∞ dBFS")
        if not self._pcm_load_failed:
            self._status.setText(
                I18N.tr("mixer.clip_editor.source_read_only", "Source waveform · Add the audio to a timeline to edit gain and fades.")
                if clip is None else I18N.tr("mixer.clip_editor.source_waveform", "Source waveform · Clip gain and fades are saved to the timeline.")
            )
        self._sync_gain_fade()
        if clip is None and self._pcm_spec_key is None:
            self._request_pcm()

    def _sync_gain_fade(self) -> None:
        clip, panel = self._clip, self._panel
        if panel is None:
            return
        if clip is None:
            panel.sync_gain_fade(0.0, 0.0, 0.0, "equal_power", "equal_power", locked=True)
            panel.set_stack_editable_hint(False)
            panel.sync_audio_edit(None)
            panel.set_clip_gain_fade(None)
            return
        fade_in = (clip.fade_in_samples / AUDIO_REFERENCE_SAMPLE_RATE if clip.fade_in_samples is not None
                   else clip.fade_in_duration / self._fps)
        fade_out = (clip.fade_out_samples / AUDIO_REFERENCE_SAMPLE_RATE if clip.fade_out_samples is not None
                    else clip.fade_out_duration / self._fps)
        panel.sync_gain_fade(clip.audio_gain_db, fade_in, fade_out,
                             clip.effective_fade_curve_in or "equal_power",
                             clip.effective_fade_curve_out or "equal_power",
                             locked=self._locked)
        # P0-3 · 快照驱动预览/试听消费片段级 gain/fade（模型参考域原样
        # 传递；apply_clip_fade 内部换算消费率）。
        panel.set_clip_gain_fade((
            float(clip.audio_gain_db or 0.0),
            int(clip.fade_in_duration or 0), int(clip.fade_out_duration or 0),
            clip.fade_in_samples, clip.fade_out_samples,
            clip.effective_fade_curve_in, clip.effective_fade_curve_out,
            float(self._fps), int(clip.timeline_duration or 0),
        ))

    # ------------------------------------------------------------------
    # P0-3：处理栈提交（控件 → 命令）
    # ------------------------------------------------------------------
    def _output_total_samples(self) -> int:
        """片段输出域总样本数（processing_rate；速度 ≠ 1 时为时间线时长域）。"""
        panel, clip = self._panel, self._clip
        if panel is None or panel._duration <= 0.0:
            return 1
        if clip is not None and self._fps > 0:
            return max(1, int(round(
                clip.timeline_duration / self._fps * AUDIO_REFERENCE_SAMPLE_RATE)))
        return max(1, int(round(panel._duration * AUDIO_REFERENCE_SAMPLE_RATE)))

    def _selection_output_range(self, selection: tuple[float, float] | None = None) -> tuple[int, int]:
        """画布选区（显示秒）→ 片段输出域半开样本区间（processing_rate）。"""
        panel = self._panel
        if panel is None or panel._duration <= 0.0:
            return (0, 1)
        t0, t1 = panel.canvas.getSelection()[:2] if selection is None else selection
        output_total = self._output_total_samples()
        target = ClipAuditionCoordinates.from_edit(self._clip.audio_edit if self._clip else None)
        origin = panel._audition_coordinates
        start = max(0, min(output_total - 1, origin.map_to(target, round(t0 * AUDIO_REFERENCE_SAMPLE_RATE))))
        end = max(0, min(output_total, origin.map_to(target, round(t1 * AUDIO_REFERENCE_SAMPLE_RATE))))
        return (start, max(start + 1, end))

    def _target_channels(self) -> tuple[int, ...]:
        return tuple(range(min(2, self._pcm_channel_count())))

    def open_selection_effect(self, effect_id: str) -> None:
        """Open the effect controls for the currently edited project clip."""
        if self._clip is None or self._panel is None or self._locked:
            return
        dialog = self._selection_effect_dialog
        if dialog is None or dialog.effect_id != effect_id:
            self._close_selection_effect()
            dialog = SelectionEffectDialog(effect_id, self)
            self._selection_effect_dialog = dialog
            dialog.requested.connect(self._request_selection_effect)
            dialog.draft_changed.connect(self._stop_selection_effect)
            dialog.stop_requested.connect(self._stop_selection_effect)
            dialog.dismissed.connect(self._stop_selection_effect)
            dialog.finished.connect(self._forget_selection_effect)
        self._refresh_selection_effect_target()
        present_effect_window(dialog)

    def _forget_selection_effect(self, _result: int) -> None:
        self._stop_selection_effect()
        self._selection_effect_dialog = None
        self._selection_effect_target = None
        self._selection_effect_cache = None

    def _close_selection_effect(self) -> None:
        dialog = self._selection_effect_dialog
        self._forget_selection_effect(0)
        if dialog is not None:
            dialog.close()

    def _stop_selection_effect(self) -> None:
        if self._selection_effect_work:
            self._cancel_analysis()
        panel = self._panel
        audio, self._selection_effect_audio = self._selection_effect_audio, None
        if panel is not None and panel._band_preview is not None and panel._band_preview[1] is audio:
            panel._audition.stop()
            panel._restore_band_preview()
        if self._selection_effect_dialog is not None:
            self._selection_effect_dialog.set_audition_active(False)

    def _on_selection_effect_play_state(self, playing: bool) -> None:
        if not playing:
            self._selection_effect_audio = None
            if self._selection_effect_dialog is not None:
                self._selection_effect_dialog.set_audition_active(False)

    def _selection_effect_blocker(self) -> str:
        panel = self._panel
        if not self._dock_active or panel is None or self._clip is None or self._locked:
            return I18N.tr("mixer.selection_effect.select_clip", "Select an unlocked audio clip first.")
        if panel._raw_pcm is None:
            if self._windowed_waveform_spec is not None:
                return self._status.text()
            if self._pcm_load_failed:
                return I18N.tr("mixer.clip_editor.pcm_failed", "Audio samples could not be loaded.")
            return I18N.tr("mixer.selection_effect.loading", "Loading audio samples…")
        return self._pre_render_blocker() or ""

    def _refresh_selection_effect_target(self, *_change: float) -> None:
        dialog, panel = self._selection_effect_dialog, self._panel
        if dialog is None or panel is None:
            return
        blocker = self._selection_effect_blocker()
        key = None
        stereo = False
        if blocker:
            label = blocker
        else:
            start, end = self._selection_output_range()
            channel = panel.canvas._edit_channel
            key = (start, end, channel)
            stereo = channel < 0 and self._pcm_channel_count() == 2
            channels = (
                I18N.tr("mixer.selection_effect.all_channels", "All channels")
                if channel < 0 else I18N.tr("mixer.selection_effect.channel", "Channel {number}").format(number=channel + 1)
            )
            if start == 0 and end == self._output_total_samples():
                interval = I18N.tr("mixer.selection_effect.whole_clip", "Entire clip")
            else:
                interval = I18N.tr("mixer.selection_effect.time_range", "{start:.3f} – {end:.3f} s").format(
                    start=start / AUDIO_REFERENCE_SAMPLE_RATE, end=end / AUDIO_REFERENCE_SAMPLE_RATE)
            label = " · ".join(value for value in (self._title.text(), interval, channels) if value)
        if key != self._selection_effect_target:
            self._selection_effect_target = key
            self._stop_selection_effect()
            dialog.set_status("")
        dialog.set_target(label, not blocker, stereo=stereo)

    def _append_offline_processor_actions(self, menu: QMenu) -> None:
        """Expose length-preserving processors through the clip transaction path."""
        panel = self._panel
        active = bool(panel is not None and self._clip is not None and not self._locked
                      and panel._raw_pcm is not None and panel.canvas.project_edits_enabled)
        root = menu.addMenu(I18N.tr("mixer.clip_editor.offline_processors", "Selection processing"))
        root.setEnabled(active)
        for effect_id in SELECTION_EFFECTS:
            root.addAction(selection_effect_name(effect_id),
                           lambda _checked=False, selected=effect_id: self.open_selection_effect(selected))

    def _request_selection_effect(
        self, mode: str, operations: tuple[tuple[str, dict[str, Any]], ...],
    ) -> None:
        """Analyze on the cancellable worker; stale previews cannot commit."""
        panel, clip = self._panel, self._clip
        dialog = self._selection_effect_dialog
        blocker = self._selection_effect_blocker()
        if blocker or panel is None or clip is None or panel._raw_pcm is None or dialog is None:
            if dialog is not None:
                dialog.set_status(blocker)
            return
        from src.features.edit.core.audio_edit import validate_audio_edit

        self._stop_selection_effect()
        start, end = self._selection_output_range()
        selected = panel.canvas._edit_channel
        channels = (selected,) if selected >= 0 else tuple(range(self._pcm_channel_count()))
        raw, rate = panel._raw_pcm, panel._pcm_rate
        cache_key = (id(raw), id(clip), panel._preview_generation, start, end, channels, operations)
        entry = self._selection_effect_cache
        cached = entry[1] if entry is not None and entry[0] == cache_key else None
        if cached is None:
            self._selection_effect_cache = None
        previous = copy_audio_edit(clip.audio_edit)
        candidate = cached.audio_edit if cached is not None else previous
        if cached is None:
            for processor, options in operations:
                label = {
                    "de_click": I18N.tr("mixer.selection_effect.de_click", "De-click"),
                    "de_hum": I18N.tr("mixer.selection_effect.de_hum", "De-hum"),
                }.get(processor) or selection_effect_name(dialog.effect_id)
                candidate = append_step(candidate, make_step(processor, start, end, channels, label=label, **options))
        errors = validate_audio_edit(candidate, clip_output_samples=self._output_total_samples(),
                                     channel_count=self._pcm_channel_count())
        if errors or candidate is None:
            dialog.set_status("\n".join(errors))
            return
        audition_start = reference_position_to_rate(start, rate)
        audition_end = reference_position_to_rate(end, rate)
        gain_fade, clip_fx = panel._clip_gain_fade, panel._clip_fx
        duration = clip.timeline_duration / self._fps
        project, timeline, asset, source_key = self._project, self._timeline, self._asset, self._pcm_spec_key
        changed_source = I18N.tr("mixer.selection_effect.source_changed", "The audio source changed. Reopen the clip.")
        baseline = None
        preview = panel._last_preview
        if mode == "delta" and preview is not None and (inputs := panel._collect_preview_inputs()):
            if (preview.key == inputs["key"] and not inputs["bypass"] and
                    inputs["denoise_preview"] is None and inputs["timepitch_request"] is None and
                    steps_signature(inputs["edit"]) == steps_signature(previous)):
                baseline = preview.edited[:, audition_start:audition_end]

        def audition_output(pcm: np.ndarray, cancelled: Callable[[], bool]) -> np.ndarray:
            output = render_audition_output(pcm, rate, duration, gain_fade, clip_fx, cancelled)
            return output[:, audition_start:audition_end]

        def compute(cancelled: Callable[[], bool]) -> tuple[_SelectionEffectRender, np.ndarray | None] | None:
            render = cached
            if render is None:
                base = render_pre_render_steps(raw, candidate, sample_rate=rate, cancel_check=cancelled)
                render = _SelectionEffectRender(candidate, base)
            if cancelled():
                return None
            if mode == "apply":
                if project is not None and timeline is not None and asset is not None:
                    from src.features.edit.core.pcm_render_cache import build_pre_render_plan, get_pcm_render_cache

                    source = resolve_clip_audio_spec(project, timeline, clip, asset, working_rate=rate)
                    if source is None or source.cache_key() != source_key:
                        raise ValueError(changed_source)
                    plan = build_pre_render_plan(
                        project, timeline, replace(clip, audio_edit=candidate), asset, working_rate=rate)
                    if plan is not None:
                        if plan.blocked_reason:
                            raise ValueError(plan.blocked_reason)
                        if plan.source_fingerprint != source.source_fingerprint:
                            raise ValueError(changed_source)
                        if cancelled():
                            return None
                        get_pcm_render_cache().remember_render(plan.spec(), render.base)
                return (render, None)
            selected_wet = render.wet
            if selected_wet is None:
                wet = apply_audio_edit_to_pcm(render.base, suffix_audio_edit(candidate), sample_rate=rate)
                selected_wet = audition_output(wet, cancelled)
                if cancelled():
                    return None
                selected_wet = (selected_wet.copy(order="C") if selected_wet.shape[1] != wet.shape[1]
                                else np.ascontiguousarray(selected_wet))
                render = replace(render, wet=selected_wet)
            if mode == "delta":
                selected_dry = baseline
                if selected_dry is None:
                    dry = render_audio_edit_full(raw, previous, sample_rate=rate, cancel_check=cancelled)
                    if cancelled():
                        return None
                    selected_dry = audition_output(dry, cancelled)
                return None if cancelled() else (render, selected_dry - selected_wet)
            return (render, selected_wet)

        def current() -> bool:
            return (self._selection_effect_dialog is dialog and dialog.isVisible() and not dialog.isMinimized()
                    and self._dock_active and not self._locked and panel._raw_pcm is raw
                    and self._selection_output_range() == (start, end)
                    and panel.canvas._edit_channel == selected)

        def publish(result: tuple[_SelectionEffectRender, np.ndarray | None]) -> None:
            render, pcm = result
            if mode == "apply":
                self._commit_audio_edit(render.audio_edit, selection_effect_name(dialog.effect_id))
                dialog.close()
                return
            self._selection_effect_cache = (cache_key, render)
            if pcm is not None and panel.start_band_preview(audition_start, pcm):
                self._selection_effect_audio = pcm
                dialog.set_audition_active(True)
            else:
                dialog.set_status(I18N.tr(
                    "mixer.clip_editor.preview_device_unavailable", "Audio output is unavailable"))

        def settled(error: str | None) -> None:
            self._selection_effect_work = False
            dialog.set_busy(False)
            dialog.set_status(error or "")

        dialog.set_busy(True)
        dialog.set_status(I18N.tr("mixer.clip_editor.processor_working", "Processing selection…"))
        self._selection_effect_work = True
        self._submit_analysis(
            compute, publish, current=current, settled=settled,
        )

    def _pcm_channel_count(self) -> int:
        if self._panel is not None and self._panel._raw_pcm is not None:
            return int(self._panel._raw_pcm.shape[0])
        return 2

    def _commit_audio_edit(self, audio_edit: dict | None, label: str) -> None:
        clip = self._clip
        if clip is None or self._locked or self._panel is None:
            return
        self.audio_edit_committed.emit(clip.clip_id, audio_edit, label)

    def _on_silence_requested(self, t0: float, t1: float) -> None:
        start, end = self._selection_output_range((t0, t1))
        step = make_step(
            "mute", start=start, end=end, channels=self._target_channels(),
            label=I18N.tr("mixer.clip_editor.step_mute", "Silence"))
        self._commit_audio_edit(
            append_step(self._clip.audio_edit if self._clip else None, step),
            I18N.tr("mixer.clip_editor.mute_selection", "Silence selection"))

    def _measurement_labels(self) -> tuple[str, ...]:
        """旧工程响度步骤的历史标签（含本地化变体），供迁移识别。"""
        return (
            "Loudness match",
            I18N.tr("mixer.clip_editor.step_loudness", "Loudness match"),
        )

    def _submit_analysis(
        self, job: Callable[[Callable[[], bool]], object],
        publish: Callable[[Any], None], *, current: Callable[[], bool] | None = None,
        busy_message: str = "", settled: Callable[[str | None], None] | None = None,
    ) -> None:
        panel = self._panel
        if panel is None:
            return
        prior, self._analysis_callback = self._analysis_callback, None
        if prior is not None:
            prior(None, None)
        self._analysis_sequence += 1
        key = (self._analysis_sequence,)
        generation = panel._preview_generation
        clip = self._clip
        if busy_message:
            panel.set_status_message(busy_message)

        def complete(result: object, error: str | None) -> None:
            if busy_message and self._status.accessibleName() == busy_message:
                panel.set_status_message("")
            is_current = (panel._preview_generation == generation and self._clip is clip
                          and (current is None or current()))
            if settled is not None:
                settled(error if is_current else None)
            if not is_current:
                return
            if error is not None:
                if settled is None:
                    self._report_analysis_failure(error)
            elif result is not None:
                publish(result)

        if panel._preview_offline:
            try:
                result = job(lambda: False)
            except (RuntimeError, ValueError, OSError) as exc:
                complete(None, str(exc))
            else:
                complete(result, None)
            return
        if self._analysis_worker is None:
            worker = ClipPreviewWorker()
            worker.preview_ready.connect(self._finish_analysis)
            worker.preview_failed.connect(self._fail_analysis)
            self.destroyed.connect(worker.finish)
            self._analysis_worker = worker
        self._analysis_callback = complete
        self._analysis_worker.submit_cancellable(key, lambda cancelled: (key, job(cancelled)))

    def _finish_analysis(self, payload: tuple) -> None:
        key, result = payload
        if key != (self._analysis_sequence,):
            return
        publish = self._analysis_callback
        self._analysis_callback = None
        if publish is not None:
            publish(result, None)

    def _fail_analysis(self, key: tuple, error: str) -> None:
        if key == (self._analysis_sequence,):
            publish = self._analysis_callback
            self._analysis_callback = None
            if publish is not None:
                publish(None, error)

    def _report_analysis_failure(self, error: str) -> None:
        self._status.setText(I18N.tr(
            "mixer.clip_editor.analysis_failed", "Audio analysis failed · {error}"
        ).format(error=error))

    def _cancel_analysis(self) -> None:
        self._analysis_sequence += 1
        callback, self._analysis_callback = self._analysis_callback, None
        if callback is not None:
            callback(None, None)
        if self._analysis_worker is not None:
            self._analysis_worker.cancel()

    def _on_normalize_requested(self, t0: float, t1: float, target_db: float) -> None:
        self._measure_selection(t0, t1, target_db, None)

    def _measure_selection(self, t0: float, t1: float, target: float,
                           ceiling: float | None) -> None:
        panel, clip = self._panel, self._clip
        if panel is None or clip is None or self._locked or panel._raw_pcm is None:
            return
        if not all(map(math.isfinite, (t0, t1, target))) or t1 <= t0:
            return
        if ceiling is not None and not math.isfinite(ceiling):
            return
        loudness = ceiling is not None
        start, end = self._selection_output_range((t0, t1))
        request = GainMeasurementRequest(
            panel._raw_pcm, panel._pcm_rate, copy_audio_edit(clip.audio_edit),
            self._target_channels(), start, end, target, ceiling,
            "range_gain" if loudness else "normalize",
            OPERATION_LOUDNESS_MATCH if loudness else OPERATION_NORMALIZE_PEAK,
            (I18N.tr("mixer.clip_editor.step_loudness", "Loudness match") if loudness else
             I18N.tr("mixer.clip_editor.step_normalize", "Normalize")),
            self._measurement_labels())
        panel.set_status_message(I18N.tr("mixer.clip_editor.measuring", "Measuring selection…"))
        self._submit_analysis(
            lambda cancelled: measure_gain_edit(request, cancelled),
            lambda result: self._apply_gain_measurement(request, result),
            current=lambda: not self._locked and clip.audio_edit == request.audio_edit)

    def _apply_gain_measurement(self, request: GainMeasurementRequest,
                                result: GainMeasurementResult) -> None:
        panel = self._panel
        if panel is None or self._locked:
            return
        if result.status == "ready":
            if request.true_peak_ceiling is None:
                label = I18N.tr("mixer.clip_editor.normalize_selection", "Normalize selection")
                message = I18N.tr(
                    "mixer.clip_editor.normalize_measured",
                    "Measured {achieved:+.1f} dBFS after normalize (target {target:+.1f})"
                ).format(achieved=result.peak_or_lufs, target=request.target)
            else:
                label = I18N.tr("mixer.clip_editor.match_loudness", "Match target loudness")
                message = I18N.tr(
                    "mixer.clip_editor.loudness_measured",
                    "Measured {lufs:+.1f} LUFS / {tp:+.1f} dBTP after match (target {target:+.1f} LUFS)"
                ).format(lufs=result.peak_or_lufs, tp=result.true_peak, target=request.target)
            self._commit_audio_edit(result.edit, label)
        elif result.status == "ceiling_not_met":
            message = I18N.tr(
                "mixer.clip_editor.ceiling_not_met",
                "True peak remains above {ceiling:+.1f} dBTP after the rack. No gain applied."
            ).format(ceiling=request.true_peak_ceiling)
        elif result.status == "too_short":
            message = I18N.tr("mixer.clip_editor.selection_too_short",
                              "Select at least 400 ms for a loudness measurement")
        elif result.status == "unchanged":
            message = I18N.tr("mixer.clip_editor.measurement_unchanged",
                              "No gain change needed for this selection")
        elif result.status == "no_signal":
            message = I18N.tr("mixer.clip_editor.no_signal", "No signal to normalize")
        else:
            message = I18N.tr("mixer.clip_editor.measurement_unavailable",
                              "Cannot measure this selection (render backend unavailable)")
        panel.set_status_message(message)

    def _on_stack_step_toggled(self, step_id: str, enabled: bool) -> None:
        if self._clip is None or self._clip.audio_edit is None:
            return
        self._commit_stack_change(
            replace_step_enabled(self._clip.audio_edit, step_id, enabled),
            I18N.tr("mixer.clip_editor.toggle_step", "Toggle audio edit step"))

    def _on_stack_step_removed(self, step_id: str) -> None:
        if self._clip is None or self._clip.audio_edit is None:
            return
        self._commit_stack_change(
            remove_step(self._clip.audio_edit, step_id),
            I18N.tr("mixer.clip_editor.remove_step", "Remove audio edit step"))

    def _on_stack_cleared(self) -> None:
        if self._clip is None or self._clip.audio_edit is None:
            return
        self._commit_stack_change(
            clear_steps(self._clip.audio_edit), I18N.tr("mixer.clip_editor.clear_stack", "Clear audio edit stack"))

    def _commit_stack_change(self, edit: dict | None, label: str) -> None:
        """P1-3 · 栈变更统一提交：长度失配走反向编辑事务（不再阻断）。

        旁路/删除/替换/清空改长度步骤时，old→new 的输出长度差经
        ``plan_clip_length_change`` 统一重算本轨（gap/ripple）、链接 A/V、
        Sync Lock 轨下游片段、后续自动化与序列标记；被移除步骤的坐标
        逆映射由计划完成（不可映射项点名提示），单条历史可撤销。
        """
        clip, panel = self._clip, self._panel
        if clip is None or self._locked or panel is None or panel._raw_pcm is None:
            return
        input_samples = panel._raw_pcm.shape[1]
        old_total = output_length_after_steps(clip.audio_edit, input_samples)
        new_total = output_length_after_steps(edit, input_samples)
        if old_total != new_total:
            # 被移除（旁路/删除/替换/清空）的改长度步骤集合。
            def _enabled_length_steps(source: Any) -> list[dict]:
                return [
                    step for step in ((source or {}).get("steps", []) or [])
                    if isinstance(step, dict) and step.get("enabled", True)
                    and step.get("processor_id") in LENGTH_CHANGING_PROCESSORS]

            old_ids = {step.get("step_id")
                       for step in _enabled_length_steps(clip.audio_edit)}
            new_map = {step.get("step_id"): step
                       for step in _enabled_length_steps(edit)}
            removed = [step for step in _enabled_length_steps(clip.audio_edit)
                       if step.get("step_id") not in new_map]
            del old_ids
            self._commit_length_change(
                edit, label, old_output_samples=old_total,
                removed_length_steps=tuple(removed),
                base_output_samples=input_samples)
            return
        self._commit_audio_edit(edit, label)

    def _on_envelope_changed(self, points: list) -> None:
        """P1-4：画布包络（拖拽结束/增删节点时发一次）→ 模型节点提交命令。"""
        clip, panel = self._clip, self._panel
        if clip is None or self._locked or panel is None or panel._duration <= 0.0:
            return
        nodes = envelope_from_canvas_points(
            points, panel._duration, self._output_total_samples())
        existing = envelope_nodes(clip.audio_edit)
        if [(n["local_sample"], n["gain_db"]) for n in nodes] == [
            (n["local_sample"], n["gain_db"]) for n in existing
        ]:
            return
        previous = {n["id"]: n for n in existing if n.get("id")}
        nodes = [{**previous.get(n["id"], {}), **n} for n in nodes]
        self._commit_audio_edit(
            set_envelope(clip.audio_edit, nodes),
            I18N.tr("mixer.clip_editor.edit_envelope", "Edit clip volume envelope"))

    def _on_markers_changed(self, points: list) -> None:
        """P1-5：标记点（稳定 ID、局部样本）持久化到片段处理栈结构。"""
        clip, panel = self._clip, self._panel
        if clip is None or self._locked or panel is None or panel._duration <= 0.0:
            return
        markers = markers_from_canvas(
            points, panel._duration, self._output_total_samples())
        self._commit_audio_edit(
            set_markers(clip.audio_edit, markers),
            I18N.tr("mixer.clip_editor.edit_markers", "Edit clip markers"))

    def _on_channel_state_changed(self, channel: int, muted: bool, phase_inverted: bool) -> None:
        """P1-5：反极性是工程状态（channel mask 步骤）；静音/独奏是监听态。

        P1 · 监听接真实设备 buffer：Mute/Solo 只改 ClipAuditionSession 的
        监听矩阵（送设备字节前置零），不入工程——导出/成片无副作用。
        """
        clip, panel = self._clip, self._panel
        if panel is None:
            return
        # 监听态（canvas 的 _ch_muted 即 Mute/Solo 的并集语义：独奏=
        # 其余声道全部静音）。
        ch_muted = getattr(panel.canvas, "_ch_muted", None) or []
        panel._audition.set_monitor(
            muted={index for index, flag in enumerate(ch_muted) if flag},
            solo=())
        if (clip is None or self._locked or not panel.canvas.project_edits_enabled
                or not 0 <= channel < self._pcm_channel_count()):
            return
        edit = clip.audio_edit
        steps = (edit or {}).get("steps", []) or []
        has_polarity = any(
            isinstance(step, dict) and step.get("enabled", True)
            and step.get("processor_id") == "channel_polarity"
            and channel in ((step.get("params") or {}).get("channels") or ())
            for step in steps)
        if has_polarity == bool(phase_inverted):
            return  # 已处于目标状态（sync 回填不重入）
        if phase_inverted:
            edit = append_step(edit, make_step(
                "channel_polarity", 0, self._output_total_samples(), (channel,),
                label=I18N.tr("mixer.clip_editor.polarity", "Invert polarity")))
        else:
            edit = copy_audio_edit(edit)
            remaining = []
            for step in edit["steps"]:
                if (isinstance(step, dict) and step.get("enabled", True)
                        and step.get("processor_id") == "channel_polarity"
                        and channel in ((step.get("params") or {}).get("channels") or ())):
                    params = step["params"]
                    params["channels"] = [other for other in params["channels"] if other != channel]
                    if not params["channels"]:
                        continue
                remaining.append(step)
            edit = {**edit, "steps": remaining} if remaining else clear_steps(edit)
        self._commit_audio_edit(
            edit, I18N.tr("mixer.clip_editor.polarity", "Invert polarity"))

    def _on_spectral_repair_requested(self, t0: float, t1: float,
                                      f_low: float, f_high: float,
                                      mode: str) -> None:
        """P1-3 · 语图右键接线：heal/衰减入修复处理器，仅试听该频段。

        能力门槛先行：锁轨/只读/PCM 未就绪/无选区/后端缺席均按实际
        能力拒绝并提示（菜单项不再静默无效果）。heal 与衰减提交
        ``spectral_repair`` 预渲染步骤（选区换算到输出域，笔迹承载
        时间/频带范围）；``solo_band`` 只做带通试听，不入工程。
        """
        clip, panel = self._clip, self._panel
        if panel is None:
            return
        if panel._raw_pcm is None or panel._pcm is None:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.pcm_required",
                "Wait for the audio samples to load."))
            return
        if t1 - t0 <= 1e-6:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.selection_required",
                "Select a time range first"))
            return
        if mode == "solo_band":
            self._audition_band(t0, t1, f_low, f_high)
            return
        if clip is None or self._locked or not self._can_append_pre_render():
            if self._locked:
                panel.set_status_message(I18N.tr(
                    "mixer.clip_editor.action_locked", "Track is locked"))
            return
        if f_high <= f_low or f_low <= 0.0:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.band_required",
                "Select a frequency band on the spectrogram first"))
            return
        start, end = self._selection_output_range((t0, t1))
        center = (start + end) // 2
        stroke = {
            "sample": int(center),
            "hz": math.sqrt(f_low * f_high),
            "time_radius": int(max(1, (end - start) // 2)),
            "bandwidth_oct": max(0.05, 0.5 * math.log2(f_high / f_low)),
        }
        step = make_step(
            "spectral_repair", start, end, self._target_channels(),
            mode=("heal" if mode == "heal" else "attenuate"),
            amount_db=12.0,
            strokes=[stroke],
            label=I18N.tr("mixer.clip_editor.step_repair", "Spectral repair"))
        self._commit_audio_edit(
            append_step(clip.audio_edit, step),
            I18N.tr("mixer.clip_editor.spectral_repair", "Spectral repair"))

    def _audition_band(self, t0: float, t1: float, f_low: float, f_high: float) -> None:
        panel = self._panel
        if panel is None or panel._pcm is None:
            return
        rate, pcm = panel._pcm_rate, panel._pcm
        start = max(0, round(t0 * rate))
        end = min(pcm.shape[1], round(t1 * rate))
        if end - start > 60 * rate:
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.band_preview_too_long",
                "Select up to 60 seconds for band preview"))
            return
        if end <= start or not 0.0 < f_low < f_high < rate / 2.0:
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.band_required", "Select a frequency band on the spectrogram first"))
            return

        def publish(filtered: np.ndarray) -> None:
            if not panel.start_band_preview(start, filtered):
                panel._show_device_failure()

        self._submit_analysis(
            lambda cancelled: None if cancelled() else build_band_preview(
                pcm, rate, start, end, f_low, f_high),
            publish, current=lambda: panel._pcm is pcm)

    def _pcm_range(self, t0: float, t1: float) -> tuple[int, int]:
        """显示秒 → 原始 PCM 半开样本区间（PCM 与显示域同比例）。"""
        panel = self._panel
        if panel is None or panel._raw_pcm is None or panel._duration <= 0.0:
            return (0, 0)
        total = panel._raw_pcm.shape[1]
        source = ClipAuditionCoordinates()
        origin = panel._audition_coordinates
        lo = max(0, min(total, origin.map_to(source, round(t0 * panel._pcm_rate))))
        hi = max(lo, min(total, origin.map_to(source, round(t1 * panel._pcm_rate))))
        return (lo, hi)

    def _on_noise_print_captured(self, t0: float, t1: float) -> None:
        from src.features.edit.core.audio_denoise import capture_noise_profile, scipy_available

        panel = self._panel
        if panel is None or panel._raw_pcm is None:
            return
        if not scipy_available():
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.denoise_unavailable", "Denoise requires the SciPy DSP backend"))
            return
        if panel._pending_timepitch is not None:
            return
        lo, hi = self._pcm_range(t0, t1)
        raw, rate = panel._raw_pcm, panel._pcm_rate
        fft = panel.current_fft_size()
        nfft = fft if fft in (512, 1024, 2048, 4096) else 2048
        reduction = float(panel.denoise_knob.value())

        def publish(profile: object) -> None:
            panel.set_noise_profile(profile)
            panel.noise_status_lbl.setText(I18N.tr(
                "mixer.clip_editor.noise_captured", "Captured {duration:.2f} s"
            ).format(duration=(hi - lo) / rate))
            panel.set_status_message(panel.noise_status_lbl.text())

        panel.set_status_message(I18N.tr("mixer.clip_editor.capturing_noise", "Capturing noise profile…"))
        self._submit_analysis(
            lambda cancelled: None if cancelled() else capture_noise_profile(
                raw, rate, lo, hi, nfft=nfft, reduction_db=reduction),
            publish)

    def _on_denoise_requested(self, t0: float, t1: float, reduction_db: float) -> None:
        """P1-6 + §3.3：谱减降噪——可编辑片段直接入栈（预渲染缓存承载）。

        源监视模式（无时间线片段）保持预览/试听态。入栈用
        ``replace_processor_step``：重复点击/调参重提交是幂等的（同一
        处理器只保留最新一步），撤销走同一条时间线历史。
        """
        from src.features.edit.core.audio_denoise import pcm_signature
        panel = self._panel
        if panel is None or panel._raw_pcm is None:
            return
        profile = panel._noise_profile
        if profile is None:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.capture_noise_first",
                "Capture a noise print before denoising"))
            return
        if profile.source_signature != pcm_signature(panel._raw_pcm):
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.noise_stale",
                "The audio changed — recapture the noise print"))
            panel.set_noise_profile(None)
            return
        lo, hi = self._pcm_range(t0, t1)
        clip = self._clip
        if clip is None or self._locked:
            panel.set_denoise_preview(
                (profile.noise_start, profile.noise_end), (lo, hi),
                float(reduction_db), nfft=profile.nfft)
            return
        from src.features.edit.core.audio_denoise import scipy_available
        if not scipy_available():
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.denoise_unavailable",
                "Denoise requires the SciPy DSP backend"))
            return
        # 与 build_pre_render_plan 同一阻断清单：变速片段的缓存基底与
        # 播放块域错位，入栈会永不生效——保持预览态并说明原因。
        from src.features.edit.core.audio_retime import needs_retime
        if needs_retime(clip):
            panel.set_denoise_preview(
                (profile.noise_start, profile.noise_end), (lo, hi),
                float(reduction_db), nfft=profile.nfft)
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.denoise_preview_only",
                "Retime/pitch commit lands with the length transaction — "
                "denoise stays preview-only on this clip"))
            return
        if not self._can_append_pre_render():
            return
        start, end = self._selection_output_range((t0, t1))
        step = make_step(
            "spectral_denoise", start=start, end=end,
            channels=self._target_channels(),
            reduction_db=round(float(reduction_db), 2),
            noise_start=profile.noise_start, noise_end=profile.noise_end,
            nfft=int(profile.nfft), link_stereo=True,
            label=I18N.tr("mixer.clip_editor.step_denoise", "Denoise"))
        panel.clear_denoise_preview()
        self._commit_audio_edit(
            replace_processor_step(clip.audio_edit, "spectral_denoise", step),
            I18N.tr("mixer.clip_editor.denoise_selection", "Denoise selection"))
        panel.info_label.setText(I18N.tr(
            "mixer.clip_editor.denoise_committed",
            "Denoise added to the stack — pre-rendering in the background"))

    def _raw_to_output_range(self, raw_lo: int, raw_hi: int) -> tuple[int, int]:
        """raw PCM 样本区间 → 片段输出域样本区间（速度 ≠ 1 时按比例映射）。"""
        panel = self._panel
        if panel is None or panel._raw_pcm is None or panel._raw_pcm.shape[1] <= 0:
            return (max(0, raw_lo), max(raw_lo + 1, raw_hi))
        source = ClipAuditionCoordinates()
        target = ClipAuditionCoordinates.from_edit(self._clip.audio_edit if self._clip else None)
        lo = source.map_to(target, raw_lo)
        hi = source.map_to(target, raw_hi)
        return (lo, max(lo + 1, hi))

    # ------------------------------------------------------------------
    # §6.2：改长度事务（变速/音高、删除区间、裁剪）
    # ------------------------------------------------------------------
    def _has_length_step(self) -> bool:
        edit = self._clip.audio_edit if self._clip is not None else None
        return any(
            isinstance(step, dict)
            and step.get("enabled", True)
            and step.get("processor_id") in LENGTH_CHANGING_PROCESSORS
            for step in (edit or {}).get("steps", []) or [])

    def _can_append_pre_render(self) -> bool:
        if self._panel is None:
            return False
        blocker = self._pre_render_blocker()
        if blocker:
            self._panel.set_status_message(blocker)
        return blocker is None

    def _pre_render_blocker(self) -> str | None:
        from src.features.edit.core.audio_denoise import scipy_available
        from src.features.edit.core.audio_retime import needs_retime

        clip, panel = self._clip, self._panel
        if clip is None or panel is None or self._locked or panel._raw_pcm is None:
            return I18N.tr("mixer.selection_effect.select_clip", "Select an unlocked audio clip first.")
        if panel._pending_timepitch is not None:
            return I18N.tr("mixer.selection_effect.finish_retime", "Finish or cancel the current speed preview.")
        if not scipy_available():
            return I18N.tr("mixer.selection_effect.scipy_required", "This process requires the SciPy DSP backend.")
        if needs_retime(clip) or self._has_length_step():
            return I18N.tr(
                "mixer.clip_editor.pre_render_after_retime",
                "Apply this process before changing the clip speed or length.")
        return None

    def _guard_length_commit(self) -> bool:
        """改长度提交的公共前置（片段/锁定/域兼容/既有改长度步骤）。"""
        clip, panel = self._clip, self._panel
        if clip is None or self._locked or panel is None or panel._raw_pcm is None:
            return False
        from src.features.edit.core.audio_retime import needs_retime
        if needs_retime(clip):
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.length_native_retime",
                "This clip already uses native retiming — clear its speed "
                "before committing stack length changes"))
            return False
        if self._has_length_step():
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.length_step_exists",
                "A length-changing step already exists — undo or clear the "
                "stack before committing another one"))
            return False
        return True

    def _ask_length_change_options(self, delta_frames: int, linked: bool,
                                   ) -> tuple[bool | None, bool]:
        """ripple/留空选择 + 独立音频编辑确认；取消返回 ``(None, _)``。"""
        message = I18N.tr(
            "mixer.clip_editor.length_change_prompt",
            "Commit changes the clip length by {delta} frames.\n"
            "Keep the gap, or ripple the following clips?").format(
                delta=delta_frames)
        box = QMessageBox(self)
        box.setWindowTitle(I18N.tr(
            "mixer.clip_editor.length_change_title", "Commit length change"))
        box.setText(message)
        box.setIcon(QMessageBox.Icon.Question)
        gap_button = box.addButton(
            I18N.tr("mixer.clip_editor.keep_gap", "Keep gap"),
            QMessageBox.ButtonRole.AcceptRole)
        ripple_button = box.addButton(
            I18N.tr("mixer.clip_editor.ripple_follow", "Ripple following clips"),
            QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        independent = None
        if linked:
            independent = QCheckBox(I18N.tr(
                "mixer.clip_editor.independent_audio",
                "Independent audio edit (linked video stays put)"), box)
            independent.setToolTip(I18N.tr(
                "mixer.clip_editor.independent_audio_tip",
                "By default the linked video clip keeps its sync — length "
                "changes that would break A/V alignment need this explicit "
                "choice"))
            box.setCheckBox(independent)
        box.exec()
        clicked = box.clickedButton()
        if clicked is gap_button:
            return False, bool(independent is not None and independent.isChecked())
        if clicked is ripple_button:
            return True, bool(independent is not None and independent.isChecked())
        return None, False

    def _commit_length_change(self, new_edit: dict, label: str,
                              *, old_output_samples: int | None = None,
                              removed_length_steps: tuple = (),
                              base_output_samples: int | None = None) -> None:
        """统一改长度提交：ripple 选择 → 冲突计划 → 项目/序列事务。

        P1-3 · ``removed_length_steps``：反向编辑（旁路/删除/替换/清空
        改长度步骤）时携带被移除步骤——计划内做坐标逆映射；
        ``base_output_samples``：长度步进基底（raw 域；缺省=旧输出）。
        """
        from src.features.edit.core.audio_length_transaction import (
            plan_clip_length_change,
        )
        clip, panel = self._clip, self._panel
        if clip is None or panel is None:
            return
        old_total = old_output_samples or self._output_total_samples()
        from src.features.edit.core.audio_edit import output_length_after_steps
        new_total = output_length_after_steps(new_edit, old_total)
        fps = self._fps if self._fps > 0 else 24.0
        delta_frames = int(round(
            (new_total - old_total) * fps / AUDIO_REFERENCE_SAMPLE_RATE))
        ripple, independent = self._ask_length_change_options(
            delta_frames, linked=bool(getattr(clip, "linked_clip_id", None)))
        if ripple is None:
            return  # 用户取消：零写入
        plan = plan_clip_length_change(
            self._timeline, clip.track_id, clip.clip_id,
            new_audio_edit=new_edit, old_output_samples=old_total,
            ripple=ripple, fps=fps, label=label,
            allow_independent_audio=independent,
            removed_length_steps=removed_length_steps,
            base_output_samples=base_output_samples)
        if plan.blocked:
            panel.set_status_message(" · ".join(plan.blocked))
            return
        panel.clear_timepitch_preview()
        panel.set_pending_timepitch(None)
        notes = list(plan.warnings) + list(plan.remap_warnings)
        if notes:
            panel.set_status_message(" · ".join(notes))
        self.length_change_committed.emit(clip.clip_id, plan, label)

    def _on_time_pitch_commit_requested(self) -> None:
        """§6.2：把预览的变速/音高作为 time_stretch 步骤提交。"""
        from src.features.edit.core.audio_denoise import scipy_available
        clip, panel = self._clip, self._panel
        if clip is None or panel is None:
            return
        pending = panel._pending_timepitch
        if pending is None:
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.timepitch_commit_hint",
                "Preview speed & pitch first, then commit the length change"))
            return
        if not self._guard_length_commit():
            return
        if not scipy_available():
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.denoise_unavailable",
                "Denoise requires the SciPy DSP backend"))
            return
        if pending != panel._validated_timepitch:
            return
        candidate, _ = make_timepitch_draft(
            clip.audio_edit, pending, self._output_total_samples(), self._target_channels())
        self._commit_length_change(candidate, I18N.tr(
            "mixer.clip_editor.commit_timepitch", "Commit speed & pitch"))

    def _on_delete_requested(self, t0: float, t1: float) -> None:
        """§3 控件映射：删除选区 —— delete_range 步骤 + 改长度事务。"""
        clip, panel = self._clip, self._panel
        if clip is None or panel is None:
            return
        if not self._guard_length_commit():
            return
        if panel._pending_timepitch is not None:
            return
        start, end = self._selection_output_range((t0, t1))
        total = self._output_total_samples()
        if start <= 0 and end >= total:
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.delete_whole_clip",
                "Deleting the whole clip is a timeline edit — use clip delete"))
            return
        step = make_step(
            "delete_range", start=start, end=end, channels=self._target_channels(),
            output_length=int(total - (end - start)),
            label=I18N.tr("mixer.clip_editor.step_delete", "Delete range"))
        self._commit_length_change(
            append_step(clip.audio_edit, step),
            I18N.tr("mixer.clip_editor.delete_selection", "Delete selection"))

    def _on_repair_commit_requested(self, payload: dict) -> None:
        """§5 画笔：把数据域修复载荷提交为 spectral_repair 步骤（保长）。"""
        from src.features.edit.core.audio_repair import scipy_available
        clip, panel = self._clip, self._panel
        if clip is None or self._locked or panel is None or panel._raw_pcm is None:
            return
        if not self._can_append_pre_render():
            return
        if not scipy_available():
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.repair_unavailable",
                "Spectral repair requires the SciPy DSP backend"))
            return
        rate = panel._pcm_rate
        strokes_payload = payload.get("strokes") or []
        if not strokes_payload:
            return
        radius_s = max(0.002, float(payload.get("time_radius_s", 0.02)))
        band_oct = max(0.02, float(payload.get("bandwidth_oct", 0.25)))
        mode = str(payload.get("mode", "heal"))
        strokes = []
        for t_seconds, hz in strokes_payload:
            raw = max(0, min(panel._raw_pcm.shape[1], int(round(t_seconds * rate))))
            start, end = self._raw_to_output_range(raw, raw + 1)
            strokes.append({
                "sample": int(start),
                "hz": float(max(20.0, min(rate / 2.0, float(hz)))),
                "time_radius": max(1, int(round(radius_s * rate))),
                "bandwidth_oct": float(band_oct),
            })
        span = max(1, int(round(radius_s * rate)))
        lo = max(0, min(s["sample"] for s in strokes) - span)
        hi = min(self._output_total_samples(),
                 max(s["sample"] for s in strokes) + span)
        if hi <= lo:
            return
        nfft = panel.current_fft_size()
        if nfft not in (512, 1024, 2048, 4096):
            nfft = 2048
        step = make_step(
            "spectral_repair", start=lo, end=hi, channels=self._target_channels(),
            strokes=strokes, amount_db=24.0, mode=mode, nfft=nfft,
            label=I18N.tr("mixer.clip_editor.step_repair", "Spectral repair"))
        self._commit_audio_edit(
            append_step(clip.audio_edit, step),
            I18N.tr("mixer.clip_editor.repair_region", "Spectral repair"))
        panel.set_status_message(I18N.tr(
            "mixer.clip_editor.repair_committed",
            "Repair added to the stack — pre-rendering in the background"))

    def _on_trim_requested(self, t0: float, t1: float) -> None:
        """§3 控件映射：裁剪保留选区 —— 头/尾两个 delete_range 步骤。

        v1 统一走处理栈（保持命令/撤销/渲染一条路径）；选区贴齐片段头尾
        时其中一段为空，退化为单段删除。native TrimClipCommand 复用留给
        后续批次（需与栈坐标域联动）。
        """
        clip, panel = self._clip, self._panel
        if clip is None or panel is None:
            return
        if not self._guard_length_commit():
            return
        if panel._pending_timepitch is not None:
            return
        start, end = self._selection_output_range((t0, t1))
        total = self._output_total_samples()
        if start <= 0 and end >= total:
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.trim_whole_clip",
                "Selection already covers the whole clip"))
            return
        edit = clip.audio_edit
        if start > 0:
            edit = append_step(edit, make_step(
                "delete_range", 0, start, self._target_channels(),
                output_length=int(total - start),
                label=I18N.tr("mixer.clip_editor.step_trim_head", "Trim head")))
            total -= start
            end = max(1, end - start)
        if end < total:
            edit = append_step(edit, make_step(
                "delete_range", end, total, self._target_channels(),
                output_length=int(end),
                label=I18N.tr("mixer.clip_editor.step_trim_tail", "Trim tail")))
        if edit is clip.audio_edit:
            return
        self._commit_length_change(
            edit, I18N.tr("mixer.clip_editor.trim_selection", "Trim to selection"),
            old_output_samples=self._output_total_samples())

    def _on_lufs_normalize_requested(self, t0: float, t1: float,
                                     target_lufs: float, tp_ceiling: float) -> None:
        self._measure_selection(t0, t1, target_lufs, tp_ceiling)

    def _on_time_pitch_requested(self, t0: float, t1: float, ratio_pct: float,
                                 pitch_semitones: float, preserve: bool,
                                 _algo: str) -> None:
        """P2-7/P1-1：选区变速/音高——预览请求快照（worker 内计算）。

        UI 线程不做 retime/pitch DSP；预览请求（选区/速率/音高/保音高）
        与待提交参数共用同一份快照，经预览 worker 的 generation 机制可
        取消/替换。时长按合同长度估算提示，实际长度随发布结果更新。
        """
        panel = self._panel
        if panel is None or panel._raw_pcm is None:
            return
        if (not all(map(math.isfinite, (t0, t1, ratio_pct, pitch_semitones)))
                or t1 <= t0 or not 50.0 <= ratio_pct <= 200.0
                or not -12.0 <= pitch_semitones <= 12.0):
            return
        if self._has_length_step():
            panel.set_status_message(I18N.tr(
                "mixer.clip_editor.length_step_exists",
                "Undo or disable the existing length-changing step first."))
            return
        raw = panel._raw_pcm
        rate = panel._pcm_rate
        duration = raw.shape[1] / rate
        lo, hi = self._pcm_range(t0, t1)
        if hi <= lo:
            return
        request = (lo, hi, float(ratio_pct), float(pitch_semitones), bool(preserve))
        if panel.btn_bypass.isChecked():
            panel.btn_bypass.setChecked(False)
        segment_out = time_stretch_output_length(hi - lo, ratio_pct)
        preview_seconds = (lo + segment_out + raw.shape[1] - hi) / rate
        panel.set_status_message(I18N.tr(
            "mixer.clip_editor.timepitch_preview_requested",
            "Preparing preview · {preview:.3f} s (was {original:.3f} s)"
        ).format(preview=preview_seconds, original=duration))
        panel.set_timepitch_request(request)

    def _on_gain_requested(self, value: float) -> None:
        if self._clip is not None and not self._locked and value != self._clip.audio_gain_db:
            self.gain_committed.emit(self._clip.clip_id, value)
        self._sync_gain_fade()

    def _on_selection_gain_requested(self, t0: float, t1: float, gain_db: float, channel: int) -> None:
        clip, panel = self._clip, self._panel
        if (clip is None or self._locked or panel is None or not panel.canvas.selection_gain_editable
                or not all(map(math.isfinite, (t0, t1, gain_db))) or t1 <= t0):
            return
        available_channels = self._target_channels()
        if channel != -1 and channel not in available_channels:
            return
        channels = available_channels if channel == -1 else (channel,)
        start, end = self._selection_output_range((t0, t1))
        label = I18N.tr("mixer.clip_editor.selection_gain", "Selection gain")
        step = make_step("range_gain", start, end, channels, gain_db=gain_db, label=label,
                         operation="selection_gain")
        self._commit_audio_edit(append_step(clip.audio_edit, step), label)

    def _on_microfade_requested(self) -> None:
        clip = self._clip
        if clip is None or self._locked:
            return
        clock = TimelineSampleClock(self._fps)
        each = min(960, int(clock.frame_start_sample(clip.timeline_duration)) // 2)
        if (clip.fade_in_samples, clip.fade_out_samples) != (each, each):
            self.sample_fade_committed.emit(clip.clip_id, each, each)
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

    def _on_fade_end_curve_requested(self, end: str, curve: str) -> None:
        clip = self._clip
        if clip is not None and not self._locked and end in ("in", "out") and is_fade_curve(curve):
            current = clip.effective_fade_curve_in if end == "in" else clip.effective_fade_curve_out
            if curve != (current or "equal_power"):
                self.fade_end_curve_committed.emit(clip.clip_id, end, curve)
        self._sync_gain_fade()
