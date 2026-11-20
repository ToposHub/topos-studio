"""Show source waveforms and timeline clips in the professional audio editor."""
from __future__ import annotations

import hashlib
import math
import os
from typing import TYPE_CHECKING, Any

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QHideEvent, QKeySequence, QMouseEvent, QResizeEvent, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from src.features.edit.core.audio_sync import binding_for_asset, bound_audio_frame, resolve_audio_source
from src.features.edit.core.audio_waveform import (
    WaveformData, WaveformExtractor, get_cached_waveform, is_waveform_pending,
)
from src.features.edit.core.clip_pcm import (
    ClipPcmProvider, ClipPcmResult, compute_spectrogram, compute_waveform_stats,
    find_zero_crossing, resolve_asset_audio_spec, resolve_clip_audio_spec,
)
from src.features.edit.core.audio_edit import (
    LENGTH_CHANGING_PROCESSORS, append_step, canvas_markers_from,
    canvas_points_from_envelope, clear_steps, envelope_from_canvas_points, envelope_nodes,
    make_step, marker_list, markers_from_canvas, output_length_after_steps, remove_step,
    replace_processor_step, replace_step_enabled, requires_pre_render,
    set_envelope, set_markers, steps_signature, suffix_audio_edit,
)
from src.features.edit.core.sample_time import AUDIO_REFERENCE_SAMPLE_RATE, TimelineSampleClock
from src.features.edit.core.fade_curve_spec import is_fade_curve
from src.features.mixer.effects.panels.clip_editor import ProcessStepRow, StudioAudioClipEditor
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import glyph_icon

from .clip_audition import ClipAuditionSession
from .clip_preview_worker import ClipPreviewResult, ClipPreviewWorker
from .workspace_style import PANEL_QSS


class _PanelStatusForwarder:
    """处理器状态消息的最小 setText 代理（转发到包装层状态行）。"""

    def __init__(self, panel: "_TimelineClipPanel") -> None:
        self._panel = panel

    def setText(self, text: str) -> None:
        self._panel.set_status_message(text)

    def setToolTip(self, text: str) -> None:
        self._panel.noise_status_lbl.setToolTip(text)

if TYPE_CHECKING:
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_v2 import ClipInstance, TimelineSequence


_FADE_CURVES = {"Linear": "linear", "S-Curve": "smooth", "Log": "equal_power"}
_DISPLAY_FADE_CURVES = {"linear": "Linear", "smooth": "S-Curve", "equal_power": "Equal Power"}


class _StatusLabel(QLabel):
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
    selection_gain_requested = pyqtSignal(float, float, float, int)
    # P0-3：清空处理栈走工程命令（原型本地清空在生产行程外）。
    stack_clear_requested = pyqtSignal()
    # §6.2：变速/音高/删除区间/裁剪的改长度提交（走项目/序列事务）。
    time_pitch_commit_requested = pyqtSignal()
    # §5 画笔：频谱修复提交（载荷已换算到数据域：秒/Hz/半径/带宽）。
    repair_commit_requested = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__(show_commit_button=False)
        self.btn_timeline = QPushButton(
            glyph_icon("arrow-left", "#CFCFD6"),
            I18N.tr("mixer.clip_editor.timeline", "Return to timeline"),
        )
        self.btn_timeline.setObjectName("GroupBtn")
        self.btn_timeline.setIconSize(QSize(14, 14))
        self.btn_timeline.setCursor(Qt.CursorShape.PointingHandCursor)
        self._transport_layout.insertWidget(0, self.btn_timeline)
        self.btn_tab = QPushButton(I18N.tr("mixer.clip_editor.tab", "Dock with timeline"))
        self.btn_tab.setObjectName("GroupBtn")
        self.btn_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_tab.hide()
        self._transport_layout.insertWidget(self._transport_layout.indexOf(self.btn_loop) + 1, self.btn_tab)
        # Let the scroll area use the toolbar's minimum width so labels stay readable.
        # The inspector already scrolls independently. Keep the waveform in
        # view when the editor shares a short dock with the audio timeline.
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
        self.canvas.apply_envelope_to_waveform = False  # 工程包络已包含在处理后 PCM 中。
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
        self._model_audio_edit: dict | None = None   # 工程模型驱动的处理栈
        # P0 · 预览 worker 状态：代际（旧发布丢弃）、spec 键、缓存计划
        # 钩子（外层注入）、失败留痕、离线模式（测试同步计算）。
        self._preview_generation = 0
        self._preview_request_key: tuple | None = None
        self._preview_worker: ClipPreviewWorker | None = None
        self._preview_spec_key: tuple | str = ""
        self._pre_render_plan_for_preview: Any = lambda: None
        self._preview_failure_note = ""
        self._preview_offline = False
        # P1-6：噪声特征与降噪预览（试听态；入栈需预渲染缓存，后续批次）。
        self._noise_profile: Any = None
        self._denoise_preview: tuple[tuple[int, int], tuple[int, int], float, int] | None = None
        # P2-7：变速/音高预览（改长度的事务提交需输出长度映射，后续批次）。
        self._timepitch_pcm: np.ndarray | None = None
        self._audition = ClipAuditionSession(self)
        self._audition.position_changed.connect(self._on_audition_position)
        self._audition.playing_changed.connect(self._on_audition_playing_changed)
        self._zero_snap_armed = False
        self._suppress_zero_snap = False
        self._stack_editable_hint = False
        self.canvas.selectionChanged.connect(self._on_pcm_selection_zero_snap)
        # 原型的磁性吸附（播放头/标记）与 PCM 过零吸附互补，两者都随开关。
        self.btn_snap_zero.toggled.connect(self._on_zero_snap_toggled)
        # A/B 旁路只切换试听与机架的显示状态。
        self.btn_bypass.toggled.connect(self._on_preview_bypass_toggled)
        # 处理器状态消息转发到包装层状态行（无包装层时退噪声标签）。
        self._status_hook: Any = None
        self.info_label = _PanelStatusForwarder(self)
        # §6.2：待提交的变速/音高参数（预览时记录；提交按钮消费）。
        self._pending_timepitch: tuple | None = None
        self._timepitch_base_duration: float | None = None
        self._setup_timepitch_actions()
        # §5 画笔：挂起的频谱修复笔迹（数据域载荷；修复按钮/H 键消费）。
        self._pending_repair: dict | None = None
        self.canvas.spectralBrushApplied.connect(self._on_canvas_brush_applied)

    def _on_preview_bypass_toggled(self, _bypassed: bool) -> None:
        self._rebuild_process_stack_ui()
        self.set_stack_editable(self._stack_editable_hint)
        self._refresh_stack_preview()

    def set_status_message(self, text: str) -> None:
        hook = self._status_hook
        if hook is not None:
            hook(text)
        else:
            self.noise_status_lbl.setText(text)

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
        self._pending_timepitch = pending
        has_preview = pending is not None
        self._timepitch_commit_button.setEnabled(
            has_preview and self._pcm is not None and self._stack_editable_hint)
        self._timepitch_discard_button.setVisible(has_preview)
        self._timepitch_summary.setVisible(has_preview)
        if pending is not None:
            _lo, _hi, ratio, pitch, preserve = pending
            if not preserve:
                pitch = 12.0 * math.log2(ratio / 100.0)
            self._timepitch_summary.setText(I18N.tr(
                "mixer.clip_editor.timepitch_draft", "Preview · {rate:.1f}% · {pitch:+.1f} st"
            ).format(rate=ratio, pitch=pitch))
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
        self.canvas._env_enabled = mode == "envelope" or bool(self.canvas._env_points)
        if mode == "brush":
            self.btn_spectral.setChecked(True)

    def _trigger_quick_heal(self) -> None:
        """修复按钮/H 键：优先消费挂起笔迹；否则用频谱选区合成修复载荷。"""
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
        self._timepitch_summary = QLabel()
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
        """装载真实 PCM：启用播放/循环/频谱/过零吸附并复位播放状态。

        P0：有工程处理栈时试听装载**处理后**数据（由预览发布驱动），
        不再用源 PCM 当栈处理结果——重开静音/降噪栈首次试听即正确。
        """
        self._pcm = pcm
        self._raw_pcm = pcm
        self._on_edit_channel_selected(-1)
        self._pcm_rate = int(sample_rate) if sample_rate > 0 else AUDIO_REFERENCE_SAMPLE_RATE
        self._timepitch_pcm = None
        self._timepitch_base_duration = None
        self._denoise_preview = None
        self.set_pending_timepitch(None)
        self._preview_generation += 1  # 旧片段在途预览全部作废
        if self._model_audio_edit is None:
            self._audition.load(pcm, sample_rate)
        else:
            self._audition.clear()
        self._is_playing = False
        self._skip_selection_mode = False
        self._update_audition_button(False)
        self._set_pcm_tools_enabled(True)
        self.set_stack_editable(self._stack_editable_hint)

    def _set_pcm_tools_enabled(self, available: bool) -> None:
        unavailable = I18N.tr("mixer.clip_editor.pcm_required", "Wait for the audio samples to load.")
        for widget in (self.btn_play, self._stop_button, self.btn_loop, self.btn_snap_zero,
                       self.btn_spectral, self.fft_combo, self.btn_noise_print, self.btn_denoise,
                       self.denoise_knob, self.denoise_pill, self.stretch_knob, self.pitch_knob,
                       self.pitch_pill, self.stretch_ratio_pill, self.target_dur_pill,
                       self.time_pitch_reset_button, self.preserve_pitch_sw, self._timepitch_button):
            widget.setEnabled(available)
            if not available:
                widget.setToolTip(unavailable)
        if available:
            self.btn_play.setToolTip(I18N.tr("mixer.clip_editor.play_audition", "Start or stop clip audition (Space)"))
            self.btn_loop.setToolTip(I18N.tr("mixer.clip_editor.loop_audition", "Loop the selection, or the whole clip"))
            self._timepitch_button.setToolTip(I18N.tr(
                "mixer.clip_editor.timepitch_preview_tip", "Audition the settings for the selection or the whole clip"))

    def detach_pcm(self) -> None:
        """Release samples and audition controls before loading a new audio range."""
        self._audition.clear()
        self._pcm = None
        self._raw_pcm = None
        self._timepitch_pcm = None
        self._timepitch_base_duration = None
        self._denoise_preview = None
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
        if not active and self.canvas._tool_mode in {"envelope", "brush"}:
            self._set_tool_mode("select")
        for widget in (self.btn_bypass, self.btn_tool_envelope, self.btn_tool_brush,
                       self.btn_apply_lufs, self.target_lufs_pill, self.tp_ceil_pill,
                       *self._lufs_preset_btns.values(), *self._selection_edit_buttons):
            widget.setEnabled(active)
        self.btn_clear_stack.setEnabled(active and bool(self._process_steps))
        for index in range(self.stack_list_layout.count()):
            row = self.stack_list_layout.itemAt(index).widget()
            if isinstance(row, ProcessStepRow):
                row.setEnabled(active)
        # 变速预览与选区增益使用不同的时间域。
        self.set_pending_timepitch(self._pending_timepitch)

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

    def sync_audio_edit(self, audio_edit: dict | None,
                        output_total_samples: int | None = None) -> None:
        """处理栈机架由工程模型驱动（命令提交后刷新；P0-3 §控件映射）。"""
        if audio_edit == self._model_audio_edit:
            return  # 任何时间线编辑都会走 sync_clip——栈没变不重绘/不打断选区
        self._model_audio_edit = audio_edit
        # P1-4：包络节点装载到画布（模型 → 视图，域转换与提交互逆）。
        nodes = envelope_nodes(audio_edit)
        self.canvas._env_enabled = self.canvas._tool_mode == "envelope" or bool(nodes)
        self.canvas._env_points = canvas_points_from_envelope(
            nodes, self._duration,
            output_total_samples or max(1, int(round(self._duration * self._pcm_rate))))
        # P1-5：标记点装载（局部样本 → 显示秒）；随片段几何变化重映射。
        self.canvas._markers = canvas_markers_from(
            marker_list(audio_edit), self._duration,
            output_total_samples or max(1, int(round(self._duration * self._pcm_rate))))
        self.canvas.update()
        # P1-5：工程级反极性按 channel mask 步骤驱动画布的 Ø 状态。
        polarity_channels = set()
        for step in (audio_edit or {}).get("steps", []) or []:
            if (isinstance(step, dict) and step.get("enabled", True)
                    and step.get("processor_id") == "channel_polarity"):
                polarity_channels.update(step.get("params", {}).get("channels", ()))
        for ch in range(len(self.canvas._ch_phase_inv)):
            self.canvas._ch_phase_inv[ch] = ch in polarity_channels
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

    def _effective_audio_edit(self) -> dict | None:
        """A/B 全局旁路是临时监听状态，不改工程。"""
        if self._model_audio_edit is None or self.btn_bypass.isChecked():
            return None
        return self._model_audio_edit

    def _refresh_stack_preview(self) -> None:
        """P0 · 预览走后台 worker：与播放同一份已处理数据。

        请求键 = (spec, steps, 视图态, fft, generation)。job 在后台线程
        计算 基底（沿用预渲染缓存）→ 后缀栈 → 波形/频谱/计量，结果经
        信号回主线程差分投影；同 key 合并、新请求取消旧发布。
        ``_preview_offline`` 为真（测试/无事件循环）时同步计算直接投影。
        """
        inputs = self._collect_preview_inputs()
        if inputs is None:
            return
        self._preview_request_key = inputs["key"]
        self._preview_failure_note = ""
        if getattr(self, "_preview_offline", False):
            self._publish_preview(self._compute_preview(inputs))
            return
        worker = self._ensure_preview_worker()
        if worker is None:
            self._publish_preview(self._compute_preview(inputs))
            return
        key = inputs["key"]
        worker.submit(key, lambda: self._compute_preview(inputs))

    def _ensure_preview_worker(self) -> ClipPreviewWorker | None:
        worker = getattr(self, "_preview_worker", None)
        if worker is None:
            try:
                worker = ClipPreviewWorker()
                worker.preview_ready.connect(self._on_preview_ready)
                worker.preview_failed.connect(self._on_preview_failed)
                self._preview_worker = worker
            except Exception:  # noqa: BLE001 - 无线程环境回退同步
                return None
        return worker

    def shutdown_preview(self) -> None:
        """编辑器关闭：停止预览 worker（可结束）。"""
        worker = getattr(self, "_preview_worker", None)
        if worker is not None:
            worker.finish()
            self._preview_worker = None

    def _collect_preview_inputs(self) -> dict | None:
        raw = self._raw_pcm
        if raw is None:
            return None
        edit = self._effective_audio_edit()
        duration = self._duration
        fft = self.current_fft_size()
        generation = self._preview_generation
        key = (
            self._preview_spec_key, steps_signature(edit) if edit else (),
            self._denoise_preview, fft, generation,
            id(self._timepitch_pcm),
        )
        # 深拷贝编辑字典：后台计算期间工程命令可能替换模型对象。
        import copy as _copy
        return {
            "key": key,
            "raw": raw,
            "rate": self._pcm_rate,
            "duration": duration,
            "edit": _copy.deepcopy(edit) if edit is not None else None,
            "denoise_preview": self._denoise_preview,
            "timepitch_pcm": self._timepitch_pcm,
            "fft": fft,
            "bypass": self.btn_bypass.isChecked(),
        }

    def _compute_preview(self, inputs: dict) -> ClipPreviewResult:
        """纯计算（后台线程安全）：基底 → 后缀栈 → 波形/频谱/计量。"""
        from src.features.edit.core.audio_edit import apply_audio_edit_to_pcm

        base = self._compute_base_pcm(inputs)
        if base is None:
            base = inputs["raw"]
        edit = inputs["edit"]
        if edit is not None and not inputs["bypass"]:
            # P0-3 前缀语义：基底已含截至最后预渲染步骤的全部前缀（含
            # 混排 inline），这里只应用后缀——inline 不再双重应用。
            edited = apply_audio_edit_to_pcm(
                base, suffix_audio_edit(edit), sample_rate=inputs["rate"])
        else:
            edited = base
        bins = min(8192, max(64, edited.shape[1] // 64))
        mins, maxs, rmss = compute_waveform_stats(edited, bins)
        spectrogram = compute_spectrogram(
            edited, inputs["rate"], nfft=inputs["fft"],
            time_bins=320, freq_bins=96)
        report = self._measure_cleanliness(edited, inputs["rate"])
        return ClipPreviewResult(
            key=inputs["key"], edited=edited, sample_rate=inputs["rate"],
            duration=inputs["duration"], mins=mins, maxs=maxs, rmss=rmss,
            spectrogram=spectrogram, report=report)

    def _compute_base_pcm(self, inputs: dict) -> np.ndarray | None:
        """基底（后台线程）：优先沿用预渲染缓存，缺席再本地烘焙。"""
        raw = inputs["raw"]
        if inputs["timepitch_pcm"] is not None:
            return inputs["timepitch_pcm"]
        edit = inputs["edit"]
        if edit is None or inputs["bypass"] or not requires_pre_render(edit):
            base = raw
        else:
            base = None
            plan = getattr(self, "_pre_render_plan_for_preview", lambda: None)()
            if plan is not None:
                try:
                    base = plan.lookup_cached()
                except Exception:  # noqa: BLE001 - 缓存读失败走本地烘焙
                    base = None
            if base is None:
                from src.features.edit.core.audio_edit import render_pre_render_steps
                try:
                    base = render_pre_render_steps(
                        raw, edit, sample_rate=inputs["rate"])
                except Exception as exc:  # noqa: BLE001 - 烘焙失败保持原声
                    base = raw
                    self._note_preview_failure(str(exc))
            if base is None:
                base = raw
        if inputs["denoise_preview"] is None:
            return base
        from src.features.edit.core.audio_denoise import denoise_spectral_subtraction
        noise_range, target_range, reduction_db, nfft = inputs["denoise_preview"]
        try:
            return denoise_spectral_subtraction(
                base, inputs["rate"], noise_range, target_range,
                reduction_db, nfft=nfft)
        except Exception as exc:  # noqa: BLE001 - 预览失败保持原声
            self._note_preview_failure(str(exc))
            return base

    @staticmethod
    def _measure_cleanliness(pcm: np.ndarray, rate: int) -> dict:
        """从有效 PCM 实测峰值/RMS/噪声底；长素材跳过 BS.1770 离线分析。

        P0：计量基于**处理后**结果（预览 worker 内调用，非 UI 回调）。
        """
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
            block_count = data.shape[1] // window
            blocks = data[:, :block_count * window].reshape(data.shape[0], block_count, window)
            # Average channel powers so inverted stereo does not read as silence.
            block_rms = np.sqrt(np.einsum("cbn,cbn->b", blocks, blocks) / (data.shape[0] * window))
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

    def _note_preview_failure(self, message: str) -> None:
        """失败留痕（线程安全：文本只做提示，不触控件）。"""
        self._preview_failure_note = str(message)

    def _on_preview_ready(self, result: ClipPreviewResult) -> None:
        # 代际校验：切片段/重连/退出后旧结果不得覆盖新窗口。
        if result.key != self._preview_request_key or result.key[4] != self._preview_generation:
            return
        if self._raw_pcm is None:
            return
        self._publish_preview(result)

    def _on_preview_failed(self, key: tuple, error: str) -> None:
        if key == self._preview_request_key and key[4] == self._preview_generation:
            self._note_preview_failure(error)
            self.set_status_message(I18N.tr(
                "mixer.clip_editor.preview_failed", "Audio preview failed · {error}"
            ).format(error=error))

    def _publish_preview(self, result: ClipPreviewResult | None) -> None:
        if result is None:
            return
        self._pcm = result.edited
        preserve_view = math.isclose(result.duration, self.canvas._duration)
        playhead = self._playhead
        self._audition.load(result.edited, result.sample_rate)
        fade_times = (self.canvas._fade_in_sec, self.canvas._fade_out_sec)
        self.load_audio_data(
            result.sample_rate, result.duration, result.mins, result.maxs,
            result.rmss, result.spectrogram, clip_name=self._clip_name,
            cleanliness_report=result.report or {}, preserve_edit_view=preserve_view)
        self.canvas._fade_in_sec, self.canvas._fade_out_sec = fade_times
        if preserve_view:
            self.update_playhead(playhead)
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
        self.btn_noise_print.setProperty("captured", self._has_noise_print)
        self.btn_noise_print.style().unpolish(self.btn_noise_print)
        self.btn_noise_print.style().polish(self.btn_noise_print)
        if profile is None:
            self.noise_status_lbl.setText(I18N.tr("mixer.clip_editor.no_noise_print", "No noise print"))
            self.noise_status_lbl.setToolTip("")

    def _emit_capture_noise(self) -> None:
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

    def set_timepitch_preview(self, pcm: np.ndarray) -> None:
        """P2-7：变速/音高后的 PCM 作为预览基底（时长随之变化）。"""
        if self._timepitch_pcm is None:
            self._timepitch_base_duration = self._duration
        self._timepitch_pcm = pcm
        self._duration = max(0.01, pcm.shape[1] / self._pcm_rate)
        self._refresh_stack_preview()

    def clear_timepitch_preview(self) -> None:
        if self._timepitch_pcm is None:
            return
        self._timepitch_pcm = None
        if self._timepitch_base_duration is not None:
            self._duration = self._timepitch_base_duration
        self._timepitch_base_duration = None
        self._refresh_stack_preview()

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
            if not sel0 <= start < sel1:
                start = sel0
        self._update_audition_loop()
        if not self._audition.play(start):
            self._update_audition_button(False)
            self.info_label.setText(I18N.tr(
                "mixer.clip_editor.no_audio_device", "No audio device available for audition"))

    def _toggle_loop(self, checked: bool) -> None:
        super()._toggle_loop(checked)
        self._update_audition_loop()

    def _update_audition_loop(self) -> None:
        if self._pcm is None or self._skip_selection_mode:
            return
        t0, t1, _, _ = self.canvas.getSelection()
        if t1 > t0:
            start, end = round(t0 * self._pcm_rate), round(t1 * self._pcm_rate)
            self._audition.set_loop(self._is_looping, start, end)
        else:
            self._audition.set_loop(self._is_looping)

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
        self._update_audition_button(playing)
        self.playStateChanged.emit(playing)
        if not playing:
            self._skip_selection_mode = False
            if self._pcm is not None:
                self.update_playhead(self._audition.return_sample() / self._pcm_rate)
            self.update_output_meter(-60.0, -60.0)

    def _update_audition_button(self, playing: bool) -> None:
        self.btn_play.setChecked(playing)
        text = (I18N.tr("mixer.clip_editor.stop_audition", "Stop audition") if playing
                else I18N.tr("mixer.clip_editor.play", "Play"))
        self.btn_play.setText("■" if playing else f"▶ {text}")
        self.btn_play.setAccessibleName(text)

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
        self.fade_preset_buttons[0.02].hide()
        self.btn_apply_lufs.setText(I18N.tr("mixer.clip_editor.match_loudness", "Match target loudness"))
        buttons_by_text = {
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
            elif button.text() in ("静音", "标准化", "裁剪", "删除", "修复"):
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
        for control in (self.fi_pill, self.fo_pill, self.fade_preset_buttons[0.0],
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
        if self._fade_editable:
            super()._apply_symmetric_fade(seconds)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.canvas and isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.RightButton:
                    return True
                if event.button() == Qt.MouseButton.LeftButton:
                    pos = event.position()
                    wave, _, _ = self.canvas._wave_and_spec_rects()
                    if self._fade_editable and (
                        self.canvas._fade_tab_at(pos, wave) or self.canvas._fade_curve_point_at(pos, wave)
                    ):
                        return False
                    if pos.x() < self.canvas._ml:
                        return True
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
        self._title = _StatusLabel(Qt.TextElideMode.ElideMiddle)
        self._title.setObjectName("ClipEditorTitle")
        self._title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._title.setMaximumWidth(300)
        self._title.setStyleSheet("color: #E2E2E8; font-size: 10px; font-weight: 600; border: none;")
        self._status = _StatusLabel(Qt.TextElideMode.ElideRight)
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
        if self._panel is not None:
            self._panel.btn_tab.setVisible(not tabbed)

    def focus_waveform(self) -> None:
        if self._panel is not None and not self._scroll.isHidden():
            self._panel.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _ensure_panel(self) -> _TimelineClipPanel:
        if self._panel is None:
            self._panel = _TimelineClipPanel()
            status_layout = self._panel._status_layout
            selection_index = status_layout.indexOf(self._panel.sel_info_lbl)
            status_layout.insertWidget(selection_index + 1, self._status, 1)
            status_layout.insertWidget(selection_index, self._title)
            self._panel._status_hook = self._status.setText
            self._panel.btn_timeline.clicked.connect(self.timeline_requested.emit)
            self._panel.btn_tab.clicked.connect(self.tab_requested.emit)
            self._panel.gain_requested.connect(self._on_gain_requested)
            self._panel.selection_gain_requested.connect(self._on_selection_gain_requested)
            self._panel.fadeApplied.connect(self._on_fade_requested)
            self._panel.fadeCurveChanged.connect(self._on_fade_curve_requested)
            self._panel.canvas.fadeCurveCommitted.connect(self._on_fade_end_curve_requested)
            self._panel.undoRequested.connect(self._on_undo_requested)
            self._panel.redoRequested.connect(self._on_redo_requested)
            self._panel.fftSizeChanged.connect(self._on_fft_size_changed)
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
        panel.canvas.cancel_fade_drag()
        panel.canvas._drag_mode = ""
        panel.canvas._physics_timer.stop()
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
        if self._panel is not None:
            self._panel.set_stack_editable_hint(not self._locked)
            self._panel.sync_audio_edit(
                self._clip.audio_edit if self._clip is not None else None,
                output_total_samples=self._output_total_samples())

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
        self._pcm_load_failed = False
        self._last_pcm = None
        self._pcm_applied = False
        if self._panel is not None:
            self._panel._pending_fade = None
            self._panel.canvas.cancel_fade_drag()
            self._panel.canvas._drag_mode = ""
            self._panel.canvas._physics_timer.stop()
            self._panel.detach_pcm()
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

    def hideEvent(self, event: QHideEvent) -> None:
        if self._panel is not None:
            self._panel._pending_fade = None
            self._panel.canvas.cancel_fade_drag()
            self._panel.canvas._physics_timer.stop()
            self._panel._audition.stop()
            self._sync_gain_fade()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        """P0 · 关闭编辑器：结束预览 worker（可结束，在途结果不发布）。"""
        if self._panel is not None:
            self._panel.shutdown_preview()
        super().closeEvent(event)

    def _request_waveform(self) -> None:
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
        if self._panel is not None:
            self._panel._preview_spec_key = self._pcm_spec_key
            self._panel._pre_render_plan_for_preview = self._build_preview_plan

        def on_pcm(result: ClipPcmResult) -> None:
            # 结果返回前校验代际：切片段/重连/撤销后旧结果不得覆盖新窗口。
            if result.spec_key == self._pcm_spec_key:
                self._apply_pcm(result)

        self._pcm_provider.request(spec, on_pcm)

    def _on_pcm_failed(self, key: tuple, error: str) -> None:
        if key != self._pcm_spec_key or self._pcm_applied:
            return
        self._pcm_load_failed = True
        message = I18N.tr("mixer.clip_editor.pcm_failed", "Audio samples could not be loaded.")
        self._status.setText(f"{message} {error}" if error else message)
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
        worker 以 (spec, steps, view, fft, generation) 键计算发布，重开
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
            else pcm.shape[1] / rate
        )
        panel._duration = max(0.01, duration)
        panel._preview_spec_key = self._pcm_spec_key
        panel._pre_render_plan_for_preview = self._build_preview_plan
        panel.attach_pcm(pcm, rate)
        # 源监视模式（无工程栈）：预览是恒等计算——同步发布（读数即时），
        # 有栈时仍走 worker。
        if panel._model_audio_edit is None:
            panel._preview_offline = True
            panel._refresh_stack_preview()
            panel._preview_offline = False
        else:
            panel._refresh_stack_preview()
        panel.update_playhead(0.0)
        self._sync_gain_fade()

    def _on_fft_size_changed(self, _size: int) -> None:
        panel = self._panel
        if panel is None or not self._pcm_applied or panel._pcm is None:
            return
        canvas = panel.canvas
        spectrogram = compute_spectrogram(
            panel._pcm, panel._pcm_rate,
            nfft=panel.current_fft_size(), time_bins=320, freq_bins=96)
        canvas.loadAudioData(
            panel._sample_rate, panel._duration, canvas._peaks_min, canvas._peaks_max,
            canvas._rms, spectrogram, preserve_edit_view=True)

    @staticmethod
    def _measure_cleanliness(pcm: np.ndarray, rate: int) -> dict:
        """兼容入口：委托面板实现（P0-5 起计量在预览 worker 内调用）。"""
        return _TimelineClipPanel._measure_cleanliness(pcm, rate)

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

    def _sync_gain_fade(self) -> None:
        clip, panel = self._clip, self._panel
        if panel is None:
            return
        if clip is None:
            panel.sync_gain_fade(0.0, 0.0, 0.0, "equal_power", "equal_power", locked=True)
            panel.set_stack_editable_hint(False)
            panel.sync_audio_edit(None)
            return
        fade_in = (clip.fade_in_samples / AUDIO_REFERENCE_SAMPLE_RATE if clip.fade_in_samples is not None
                   else clip.fade_in_duration / self._fps)
        fade_out = (clip.fade_out_samples / AUDIO_REFERENCE_SAMPLE_RATE if clip.fade_out_samples is not None
                    else clip.fade_out_duration / self._fps)
        panel.sync_gain_fade(clip.audio_gain_db, fade_in, fade_out,
                             clip.effective_fade_curve_in or "equal_power",
                             clip.effective_fade_curve_out or "equal_power",
                             locked=self._locked)

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
        frac0 = max(0.0, min(1.0, t0 / panel._duration))
        frac1 = max(0.0, min(1.0, t1 / panel._duration))
        start = min(output_total - 1, int(round(frac0 * output_total)))
        end = int(round(frac1 * output_total))
        return (start, max(start + 1, end))

    def _target_channels(self) -> tuple[int, ...]:
        return tuple(range(min(2, self._pcm_channel_count())))

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
        start, end = self._selection_output_range()
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

    def _prepare_measurement(self, processor_id: str, operation: str,
                             start: int, end: int,
                             channels: tuple[int, ...]) -> dict | None:
        """P0-1 · 解析测量上下文：插入点、选区折算与被替换步骤。

        返回 dict（insertion_index / sel_start / sel_end / replace_index /
        migrated_edit），预渲染后端缺席时置 ``error`` 信息键由调用方提示。
        """
        clip, panel = self._clip, self._panel
        if clip is None or panel is None or panel._raw_pcm is None:
            return None
        from src.features.edit.core.audio_edit import (
            find_measurement_step_index,
            map_selection_to_insertion,
            tag_legacy_measurement_operations,
        )
        migrated = tag_legacy_measurement_operations(
            clip.audio_edit, legacy_labels=self._measurement_labels())
        replace_index = find_measurement_step_index(
            migrated, processor_id=processor_id, operation=operation,
            start=start, end=end, channels=channels,
            legacy_labels=self._measurement_labels())
        sel_start, sel_end = map_selection_to_insertion(
            migrated, start, end, replace_index)
        step_count = len((migrated or {}).get("steps", []) or []) if isinstance(migrated, dict) else 0
        return {
            "migrated_edit": migrated,
            "replace_index": replace_index,
            "insertion_index": replace_index,
            "sel_start": sel_start,
            "sel_end": sel_end,
            # 替换语义：已有同身份步骤时必须写入新系数（哪怕 ≈0），
            # 否则旧的远端增益会残留；只有新增时才允许"已在目标"跳过。
            "replacing": replace_index < step_count,
        }

    def _render_measurement_baseline(self, context: dict) -> np.ndarray | None:
        """插入点基准 PCM；预渲染后端缺席/渲染失败返回 None（调用方提示）。"""
        panel = self._panel
        if panel is None or panel._raw_pcm is None:
            return None
        from src.features.edit.core.audio_edit import render_to_step_insertion
        try:
            return render_to_step_insertion(
                panel._raw_pcm, context["migrated_edit"],
                insertion_index=context["insertion_index"],
                sample_rate=panel._pcm_rate)
        except Exception:  # noqa: BLE001 - 后端缺席/解码失败均按不可测量提示
            return None

    def _verify_candidate_measurement(self, candidate_edit: dict,
                                      start: int, end: int,
                                      channels: tuple[int, ...]) -> np.ndarray | None:
        """P0-1 · 用同一渲染路径重建候选栈后的选区 PCM（复测/回报用）。

        读数与测量基准同语义：乘候选步骤的 unity 包络——边缘 3 ms 是
        防爆音干湿过渡带，逐样本回退到原始电平，不参与目标读数。
        """
        panel = self._panel
        if panel is None or panel._raw_pcm is None:
            return None
        from src.features.edit.core.audio_edit import (
            render_audio_edit_full, selection_measurement_envelope,
        )
        try:
            full = render_audio_edit_full(
                panel._raw_pcm, candidate_edit, sample_rate=panel._pcm_rate)
        except Exception:  # noqa: BLE001 - 复测失败只影响回报精度
            return None
        lo = max(0, min(start, full.shape[1]))
        hi = max(lo, min(end, full.shape[1]))
        if hi <= lo:
            return None
        usable = [c for c in channels if c < full.shape[0]]
        if not usable:
            return None
        segment = full[usable, lo:hi] if len(usable) < full.shape[0] else full[:, lo:hi]
        env = selection_measurement_envelope(
            full.shape[1], start, end, sample_rate=panel._pcm_rate)
        if env is not None:
            segment = segment * env[lo:hi][None, :]
        return segment

    def _on_normalize_requested(self, t0: float, t1: float, target_db: float) -> None:
        """P0-1 · Normalize：对**步骤插入点**的数据测量，替换同身份旧系数。

        基准 = 按机架序渲染到目标步骤插入点的 PCM（替换时移除本次要
        替换的增益，不叠加）；提交后用同一渲染路径复测实际峰值并回报。
        静音无解提示；允许实际衰减或提升；同身份（处理器+操作+区间+
        声道）重复提交替换不叠加；旧工程无 operation 的步骤按迁移规则
        识别并沿用 step ID 与排序。
        """
        panel = self._panel
        if panel is None or panel._raw_pcm is None or self._clip is None:
            return
        from src.features.edit.core.audio_edit import (
            OPERATION_NORMALIZE_PEAK,
            upsert_step,
        )
        start, end = self._selection_output_range((t0, t1))
        channels = self._target_channels()
        context = self._prepare_measurement(
            "normalize", OPERATION_NORMALIZE_PEAK, start, end, channels)
        if context is None:
            return
        baseline = self._render_measurement_baseline(context)
        if baseline is None:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.measurement_unavailable",
                "Cannot measure this selection (render backend unavailable)"))
            return
        lo = max(0, min(context["sel_start"], baseline.shape[1]))
        hi = max(lo, min(context["sel_end"], baseline.shape[1]))
        usable = [c for c in channels if c < baseline.shape[0]]
        if hi <= lo or not usable:
            return
        from src.features.edit.core.audio_edit import (
            selection_measurement_envelope,
        )
        env = selection_measurement_envelope(
            baseline.shape[1], context["sel_start"], context["sel_end"],
            sample_rate=panel._pcm_rate)
        window = np.abs(baseline[usable, lo:hi])
        if env is not None:
            window = window * env[lo:hi][None, :]
        peak = float(window.max()) if window.size else 0.0
        if peak <= 1e-6:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.no_signal", "No signal to normalize"))
            return
        import math as _math
        gain_db = target_db - 20.0 * _math.log10(peak)
        if abs(gain_db) < 0.05 and not context["replacing"]:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.already_normalized",
                f"Selection already peaks at {target_db:+.1f} dBFS"))
            return
        step = make_step(
            "normalize", start=start, end=end, channels=channels,
            gain_db=round(gain_db, 2),
            operation=OPERATION_NORMALIZE_PEAK,
            label=I18N.tr("mixer.clip_editor.step_normalize", "Normalize"))
        candidate = upsert_step(context["migrated_edit"], step)
        # 复测：同一渲染路径重建候选栈，回报实际落点（非线性后级会改写
        # 插入点目标，此处按最终输出读数呈现）。
        achieved_db: float | None = None
        segment = self._verify_candidate_measurement(
            candidate, start, end, channels)
        if segment is not None and segment.size:
            peak_after = float(np.abs(segment).max())
            if peak_after > 1e-9:
                achieved_db = 20.0 * _math.log10(peak_after)
        self._commit_audio_edit(
            candidate,
            I18N.tr("mixer.clip_editor.normalize_selection", "Normalize selection"))
        if achieved_db is not None:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.normalize_measured",
                "Measured {achieved:+.1f} dBFS after normalize (target {target:+.1f})"
            ).format(achieved=achieved_db, target=target_db))
        else:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.normalize_submitted",
                "Peak normalization requested · target {target:+.1f} dBFS"
            ).format(target=target_db))

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
        if clip is None or self._locked or not 0 <= channel < 2:
            return
        edit = clip.audio_edit
        steps = (edit or {}).get("steps", []) or []
        has_polarity = any(
            isinstance(step, dict) and step.get("enabled", True)
            and step.get("processor_id") == "channel_polarity"
            and channel in ((step.get("params", {}) or {}).get("channels", ()))
            for step in steps)
        if has_polarity == bool(phase_inverted):
            return  # 已处于目标状态（sync 回填不重入）
        for step in list(steps):
            if (isinstance(step, dict) and step.get("processor_id") == "channel_polarity"
                    and channel in ((step.get("params", {}) or {}).get("channels", ()))):
                edit = remove_step(edit, step["step_id"])
        if phase_inverted:
            edit = append_step(edit, make_step(
                "channel_polarity", 0, self._output_total_samples(), (channel,),
                label=I18N.tr("mixer.clip_editor.polarity", "Invert polarity")))
        self._commit_audio_edit(
            edit, I18N.tr("mixer.clip_editor.polarity", "Invert polarity"))

    def _pcm_range(self, t0: float, t1: float) -> tuple[int, int]:
        """显示秒 → 原始 PCM 半开样本区间（PCM 与显示域同比例）。"""
        panel = self._panel
        if panel is None or panel._raw_pcm is None or panel._duration <= 0.0:
            return (0, 0)
        total = panel._raw_pcm.shape[1]
        lo = int(round(max(0.0, t0) / panel._duration * total))
        hi = int(round(min(panel._duration, t1) / panel._duration * total))
        return (lo, max(lo + 1, hi))

    def _on_noise_print_captured(self, t0: float, t1: float) -> None:
        """P1-6：锁定纯噪声区间的中位数功率谱（后续 DSP 用同一签名校验）。"""
        from src.features.edit.core.audio_denoise import (
            capture_noise_profile, pcm_signature, scipy_available,
        )
        panel = self._panel
        if panel is None or panel._raw_pcm is None:
            return
        if not scipy_available():
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.denoise_unavailable",
                "Denoise requires the SciPy DSP backend"))
            return
        lo, hi = self._pcm_range(t0, t1)
        try:
            profile = capture_noise_profile(
                panel._raw_pcm, panel._pcm_rate, lo, hi,
                nfft=panel.current_fft_size() if panel.current_fft_size() in (512, 1024, 2048, 4096) else 2048,
                reduction_db=float(panel.denoise_knob.value()),
            )
        except ValueError as exc:
            panel.info_label.setText(str(exc))
            return
        panel.set_noise_profile(profile)
        panel.noise_status_lbl.setText(I18N.tr(
            "mixer.clip_editor.noise_captured", f"● Captured {(hi - lo) / panel._pcm_rate:.2f} s"))

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
        speed = getattr(clip, "effective_speed", None)
        speed = float(speed() if callable(speed)
                      else (speed if speed is not None else 1.0))
        if abs(speed - 1.0) > 1e-9:
            panel.set_denoise_preview(
                (profile.noise_start, profile.noise_end), (lo, hi),
                float(reduction_db), nfft=profile.nfft)
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.denoise_preview_only",
                "Retime/pitch commit lands with the length transaction — "
                "denoise stays preview-only on this clip"))
            return
        start, end = self._selection_output_range()
        # 噪声采集区间与选区同在片段输出样本域（raw PCM 与输出域同比例）。
        noise_start, noise_end = self._raw_to_output_range(
            profile.noise_start, profile.noise_end)
        step = make_step(
            "spectral_denoise", start=start, end=end,
            channels=self._target_channels(),
            reduction_db=round(float(reduction_db), 2),
            noise_start=noise_start, noise_end=noise_end,
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
        output_total = self._output_total_samples()
        scale = output_total / panel._raw_pcm.shape[1]
        lo = int(round(raw_lo * scale))
        hi = int(round(raw_hi * scale))
        return (lo, max(lo + 1, hi))

    # ------------------------------------------------------------------
    # §6.2：改长度事务（变速/音高、删除区间、裁剪）
    # ------------------------------------------------------------------
    def _has_length_step(self) -> bool:
        edit = self._clip.audio_edit if self._clip is not None else None
        return any(
            isinstance(step, dict)
            and step.get("processor_id") in LENGTH_CHANGING_PROCESSORS
            for step in (edit or {}).get("steps", []) or [])

    def _guard_length_commit(self) -> bool:
        """改长度提交的公共前置（片段/锁定/域兼容/既有改长度步骤）。"""
        clip, panel = self._clip, self._panel
        if clip is None or self._locked or panel is None or panel._raw_pcm is None:
            return False
        speed = getattr(clip, "effective_speed", None)
        speed = float(speed() if callable(speed)
                      else (speed if speed is not None else 1.0))
        if abs(speed - 1.0) > 1e-9:
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
        from src.features.edit.core.audio_edit import time_stretch_output_length
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
        lo, hi, ratio_pct, semitones, preserve = pending
        old_total = self._output_total_samples()
        # output_length 是“本步骤后的定义域长度”（§3 映射合同）。
        segment_out = time_stretch_output_length(hi - lo, ratio_pct)
        output_length = old_total - (hi - lo) + segment_out
        step = make_step(
            "time_stretch", start=lo, end=hi, channels=self._target_channels(),
            ratio_pct=round(float(ratio_pct), 2),
            pitch_semitones=round(float(semitones), 2),
            preserve_pitch=bool(preserve),
            output_length=int(output_length),
            label=I18N.tr("mixer.clip_editor.step_timepitch",
                          "Speed / pitch"))
        self._commit_length_change(
            append_step(clip.audio_edit, step),
            I18N.tr("mixer.clip_editor.commit_timepitch",
                    "Commit speed & pitch"))

    def _on_delete_requested(self, t0: float, t1: float) -> None:
        """§3 控件映射：删除选区 —— delete_range 步骤 + 改长度事务。"""
        clip, panel = self._clip, self._panel
        if clip is None or panel is None:
            return
        if not self._guard_length_commit():
            return
        start, end = self._selection_output_range()
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
        start, end = self._selection_output_range()
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
        """P0-1 · BS.1770 实测响度匹配（插入点基准/声道/同一选区）。

        测量基于**步骤插入点**的渲染基准（替换时移除本次要替换的旧
        增益，不叠加）与目标声道；允许实际衰减或提升（−10 LUFS 素材
        对 −23 LUFS 目标应衰减）；升益受 dBTP 上限钳制；静音无解提示；
        同身份重复提交**替换**不叠加；写入后按候选栈同一渲染路径复测
        目标与 dBTP 并回报实测值。
        """
        clip, panel = self._clip, self._panel
        if clip is None or self._locked or panel is None or panel._raw_pcm is None:
            return
        from src.features.edit.core.audio_edit import (
            OPERATION_LOUDNESS_MATCH,
            upsert_step,
        )
        from src.features.edit.core.loudness_analyzer import analyze_loudness
        rate = panel._pcm_rate
        start, end = self._selection_output_range((t0, t1))
        channels = self._target_channels()
        context = self._prepare_measurement(
            "range_gain", OPERATION_LOUDNESS_MATCH, start, end, channels)
        if context is None:
            return
        baseline = self._render_measurement_baseline(context)
        if baseline is None:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.measurement_unavailable",
                "Cannot measure this selection (render backend unavailable)"))
            return
        usable = [c for c in channels if c < baseline.shape[0]]
        if not usable:
            return
        lo = max(0, min(context["sel_start"], baseline.shape[1]))
        hi = max(lo, min(context["sel_end"], baseline.shape[1]))
        if hi - lo < int(0.4 * rate):
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.selection_too_short",
                "Select at least 400 ms for a loudness measurement"))
            return
        segment = baseline[usable, lo:hi] if len(usable) < baseline.shape[0] else baseline[:, lo:hi]
        from src.features.edit.core.audio_edit import (
            selection_measurement_envelope,
        )
        env = selection_measurement_envelope(
            baseline.shape[1], context["sel_start"], context["sel_end"],
            sample_rate=panel._pcm_rate)
        if env is not None:
            segment = segment * env[lo:hi][None, :]
        report = analyze_loudness(np.ascontiguousarray(segment), float(rate))
        if not getattr(report, "backend_available", False):
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.loudness_unavailable",
                "Loudness backend unavailable for this format"))
            return
        integrated = float(report.integrated_lufs)
        true_peak = float(report.true_peak_dbtp)
        if not math.isfinite(integrated) or integrated <= -70.0:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.no_signal",
                "No signal to normalize"))
            return
        gain = target_lufs - integrated
        if gain > 0.0:
            # 升益受 dBTP 上限钳制（基准的 true peak + gain ≤ 上限）；
            # 衰减不受限（压低峰值只会更安全）。
            gain = min(gain, tp_ceiling - true_peak)
        if abs(gain) < 0.05 and not context["replacing"]:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.loudness_capped",
                f"Already at {integrated:+.1f} LUFS / {true_peak:+.1f} dBTP — "
                f"peak-limited, no gain applied"))
            return
        step = make_step(
            "range_gain", start, end, channels,
            gain_db=round(gain, 2),
            operation=OPERATION_LOUDNESS_MATCH,
            label=I18N.tr("mixer.clip_editor.step_loudness", "Loudness match"))
        candidate = upsert_step(context["migrated_edit"], step)
        # 复测：候选栈同一渲染路径重建后实测 LUFS/dBTP，回报实际落点。
        achieved_lufs: float | None = None
        achieved_tp: float | None = None
        verify_segment = self._verify_candidate_measurement(
            candidate, start, end, channels)
        if verify_segment is not None and verify_segment.shape[1] >= int(0.4 * rate):
            verify_report = analyze_loudness(
                np.ascontiguousarray(verify_segment), float(rate))
            if getattr(verify_report, "backend_available", False):
                candidate_lufs = float(verify_report.integrated_lufs)
                if math.isfinite(candidate_lufs) and candidate_lufs > -70.0:
                    achieved_lufs = candidate_lufs
                    achieved_tp = float(verify_report.true_peak_dbtp)
        self._commit_audio_edit(
            candidate,
            I18N.tr("mixer.clip_editor.match_loudness", "Match target loudness"))
        if achieved_lufs is not None:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.loudness_measured",
                "Measured {lufs:+.1f} LUFS / {tp:+.1f} dBTP after match "
                "(target {target:+.1f} LUFS)"
            ).format(lufs=achieved_lufs,
                     tp=achieved_tp if achieved_tp is not None else 0.0,
                     target=target_lufs))
        else:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.loudness_submitted",
                "Loudness match requested · target {target:+.1f} LUFS"
            ).format(target=target_lufs))

    def _on_time_pitch_requested(self, t0: float, t1: float, ratio_pct: float,
                                 pitch_semitones: float, preserve: bool,
                                 _algo: str) -> None:
        """P2-7：选区变速/音高——预览态（选区两侧上下文保持原样拼接）。"""
        panel = self._panel
        if panel is None or panel._raw_pcm is None:
            return
        from src.features.edit.core.audio_timepitch import (
            pitch_shift, retime_nopreserve, retime_preserve,
        )
        raw = panel._raw_pcm
        rate = panel._pcm_rate
        duration = max(1e-9, raw.shape[1] / rate)
        lo = int(round(max(0.0, t0) / duration * raw.shape[1]))
        hi = int(round(min(duration, t1) / duration * raw.shape[1]))
        lo, hi = max(0, lo), min(raw.shape[1], max(lo + 1, hi))
        segment = raw[:, lo:hi]
        try:
            if abs(pitch_semitones) > 1e-6:
                segment = pitch_shift(segment, rate, float(pitch_semitones))
            if abs(ratio_pct - 100.0) > 1e-6:
                segment = (retime_preserve if preserve else retime_nopreserve)(
                    segment, rate, float(ratio_pct))
        except ValueError as exc:
            panel.info_label.setText(str(exc))
            return
        preview = np.concatenate([raw[:, :lo], segment, raw[:, hi:]], axis=1)
        panel.set_timepitch_preview(np.ascontiguousarray(preview, dtype=np.float32))
        # §6.2：预览参数即待提交参数（选区/速率/音高在 raw 域）。
        panel.set_pending_timepitch(
            (lo, hi, float(ratio_pct), float(pitch_semitones), bool(preserve)))
        panel.info_label.setText(I18N.tr(
            "mixer.clip_editor.timepitch_preview_applied",
            f"Preview: {preview.shape[1] / rate:.3f} s "
            f"(was {duration:.3f} s) — commit with the length transaction"))

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
