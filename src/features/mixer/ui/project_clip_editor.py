"""Show source waveforms and timeline clips in the professional audio editor."""
from __future__ import annotations

import hashlib
import math
import os
from typing import TYPE_CHECKING, Any

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QHideEvent, QKeySequence, QMouseEvent, QResizeEvent, QShortcut, QWheelEvent
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
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
    canvas_points_from_envelope, envelope_from_canvas_points, envelope_nodes,
    inline_audio_edit, make_step, marker_list, markers_from_canvas, remove_step,
    replace_processor_step, replace_step_enabled, requires_pre_render,
    set_envelope, set_markers,
)
from src.features.edit.core.sample_time import AUDIO_REFERENCE_SAMPLE_RATE, TimelineSampleClock
from src.features.mixer.effects.panels.clip_editor import StudioAudioClipEditor
from src.shared.i18n.i18n import I18N

from .clip_audition import ClipAuditionSession
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
    # P0-3：清空处理栈走工程命令（原型本地清空在生产行程外）。
    stack_clear_requested = pyqtSignal()
    # §6.2：变速/音高/删除区间/裁剪的改长度提交（走项目/序列事务）。
    time_pitch_commit_requested = pyqtSignal()
    # §5 画笔：频谱修复提交（载荷已换算到数据域：秒/Hz/半径/带宽）。
    repair_commit_requested = pyqtSignal(object)

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
        self._raw_pcm: np.ndarray | None = None      # 处理栈前的原始 PCM
        self._model_audio_edit: dict | None = None   # 工程模型驱动的处理栈
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
        # A/B 全局旁路是临时监听状态：立即按原始 PCM 重建预览与试听。
        self.btn_bypass.toggled.connect(lambda _on: self._refresh_stack_preview())
        # 处理器状态消息转发到包装层状态行（无包装层时退噪声标签）。
        self._status_hook: Any = None
        self.info_label = _PanelStatusForwarder(self)
        # §6.2：待提交的变速/音高参数（预览时记录；提交按钮消费）。
        self._pending_timepitch: tuple | None = None
        self._timepitch_commit_button: QPushButton | None = None
        # §5 画笔：挂起的频谱修复笔迹（数据域载荷；修复按钮/H 键消费）。
        self._pending_repair: dict | None = None
        self.canvas.spectralBrushApplied.connect(self._on_canvas_brush_applied)

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
        self.timePitchApplied.emit(t0, t1, ratio_pct, pitch_st, preserve, "wsola")
        self._reset_time_pitch()

    def set_pending_timepitch(self, pending: tuple | None) -> None:
        """记录/清除待提交的变速参数并同步提交按钮可用性。"""
        self._pending_timepitch = pending
        button = self._timepitch_commit_button
        if button is not None:
            button.setEnabled(pending is not None and self._pcm is not None
                              and self._stack_editable_hint)
            button.setToolTip("" if pending is not None else I18N.tr(
                "mixer.clip_editor.timepitch_commit_hint",
                "Preview speed & pitch first, then commit the length change"))

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

    def ensure_timepitch_commit_button(self) -> None:
        """在时间/音高卡内补一个“提交入栈”按钮（只创建一次）。"""
        if self._timepitch_commit_button is not None:
            return
        apply_button = next(
            (button for button in self.findChildren(QPushButton)
             if button.text() == "应用时间与音高"), None)
        layout = (apply_button.parentWidget().layout()
                  if apply_button is not None else None)
        if layout is None:
            return
        button = QPushButton(I18N.tr(
            "mixer.clip_editor.timepitch_commit", "Commit to stack"))
        button.setObjectName("MiniCellBtn")
        button.setEnabled(False)
        button.clicked.connect(
            lambda _checked=False: self.time_pitch_commit_requested.emit())
        self._timepitch_commit_button = button
        self.set_pending_timepitch(self._pending_timepitch)
        layout.addWidget(button)

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
        self._raw_pcm = pcm
        self._pcm_rate = int(sample_rate) if sample_rate > 0 else AUDIO_REFERENCE_SAMPLE_RATE
        self._timepitch_pcm = None
        self._denoise_preview = None
        self.set_pending_timepitch(None)
        self._audition.load(pcm, sample_rate)
        self._is_playing = False
        self._skip_selection_mode = False
        self.btn_play.setChecked(False)
        self.btn_play.setText("▶ 播放")
        for widget in (self.btn_play, self.btn_loop, self.btn_snap_zero,
                       self.btn_spectral, self.fft_combo,
                       self.btn_noise_print, self.btn_denoise, self.denoise_knob,
                       self.stretch_knob, self.pitch_knob, self.pitch_pill,
                       self.stretch_ratio_pill, self.target_dur_pill,
                       self.stretch_algo_combo, self.preserve_pitch_sw):
            widget.setEnabled(True)
            widget.setToolTip("")
        timepitch_button = getattr(self, "_timepitch_button", None)
        if timepitch_button is not None:
            timepitch_button.setEnabled(True)
            timepitch_button.setToolTip(I18N.tr(
                "mixer.clip_editor.timepitch_preview",
                "Preview speed & pitch — committing length changes comes with "
                "the ripple transaction"))
        self.btn_play.setToolTip(I18N.tr("mixer.clip_editor.play_audition", "Audition this clip (Space)"))
        self.btn_loop.setToolTip(I18N.tr("mixer.clip_editor.loop_audition", "Loop the selection, or the whole clip"))

    def detach_pcm(self) -> None:
        """释放试听（重开/重连/隐藏时调用）；播放控件回到禁用态。"""
        self._audition.stop()
        self._pcm = None
        self._raw_pcm = None
        self._model_audio_edit = None
        self._pending_timepitch = None
        self._is_playing = False
        self._skip_selection_mode = False

    def set_stack_editable(self, editable: bool) -> None:
        """有可编辑时间线片段时启用处理栈相关控件（源监视模式禁用）。"""
        for widget in (self.btn_bypass, self.btn_clear_stack, self.btn_tool_envelope):
            widget.setEnabled(editable and self._pcm is not None)
            widget.setToolTip("" if editable else I18N.tr(
                "mixer.clip_editor.timeline_only",
                "Add the clip to a timeline to edit its processing stack"))
        for widget in (self.btn_apply_lufs, self.target_lufs_pill,
                       self.tp_ceil_pill, *self._lufs_preset_btns.values()):
            widget.setEnabled(editable and self._pcm is not None)
        for button in getattr(self, "_selection_edit_buttons", ()):
            button.setEnabled(editable and self._pcm is not None)
        # 变速/音高的提交按钮跟随同一可用性（§6.2 改长度提交）。
        self.set_pending_timepitch(self._pending_timepitch)

    def sync_audio_edit(self, audio_edit: dict | None,
                        output_total_samples: int | None = None) -> None:
        """处理栈机架由工程模型驱动（命令提交后刷新；P0-3 §控件映射）。"""
        if audio_edit == self._model_audio_edit:
            return  # 任何时间线编辑都会走 sync_clip——栈没变不重绘/不打断选区
        self._model_audio_edit = audio_edit
        # P1-4：包络节点装载到画布（模型 → 视图，域转换与提交互逆）。
        nodes = envelope_nodes(audio_edit)
        self.canvas._env_enabled = bool(nodes)
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
        """把处理栈（含降噪预览基底）应用到 PCM：波形/频谱/试听与播放同结果。

        预渲染步骤（spectral_denoise）在 ``_base_pcm`` 中整段烘焙（与缓存
        基底同一语义），这里只应用 inline 步骤 + 包络——编辑器预览与三条
        渲染路径（播放/成片/Stem）共用同一分派（§3.3）。
        """
        base = self._base_pcm()
        if base is None:
            return
        from src.features.edit.core.audio_edit import apply_audio_edit_to_pcm
        edited = apply_audio_edit_to_pcm(
            base, inline_audio_edit(self._effective_audio_edit()),
            sample_rate=self._pcm_rate)
        self._pcm = edited
        self._audition.load(edited, self._pcm_rate)
        bins = min(8192, max(64, edited.shape[1] // 64))
        mins, maxs, rmss = compute_waveform_stats(edited, bins)
        spectrogram = compute_spectrogram(
            edited, self._pcm_rate, nfft=self.current_fft_size(),
            time_bins=320, freq_bins=96)
        self.load_audio_data(
            self._pcm_rate, self._duration, mins, maxs, rmss, spectrogram,
            clip_name=self._clip_name)

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

    def set_denoise_preview(self, noise_range: tuple[int, int],
                            target_range: tuple[int, int],
                            reduction_db: float, nfft: int = 2048) -> None:
        """降噪预览：原始 PCM → 谱减 → 处理栈 → 波形/频谱/试听。"""
        self._denoise_preview = (noise_range, target_range, reduction_db, nfft)
        self._refresh_stack_preview()

    def clear_denoise_preview(self) -> None:
        self._denoise_preview = None
        self._refresh_stack_preview()

    def _base_pcm(self) -> np.ndarray | None:
        raw = self._raw_pcm
        if raw is None:
            return None
        if self._timepitch_pcm is not None:
            return self._timepitch_pcm
        # §3.3：已提交的预渲染步骤（spectral_denoise）先整段烘焙——与
        # 播放/导出消费的缓存基底同一语义，A/B 旁路时一并跳过。
        base = raw
        edit = self._effective_audio_edit()
        if edit is not None and requires_pre_render(edit):
            from src.features.edit.core.audio_edit import render_pre_render_steps
            try:
                base = render_pre_render_steps(
                    base, edit, sample_rate=self._pcm_rate)
            except Exception as exc:  # noqa: BLE001 - 烘焙失败保持原声并提示
                self.info_label.setText(str(exc))
                return raw
        if self._denoise_preview is None:
            return base
        from src.features.edit.core.audio_denoise import denoise_spectral_subtraction
        noise_range, target_range, reduction_db, nfft = self._denoise_preview
        try:
            return denoise_spectral_subtraction(
                base, self._pcm_rate, noise_range, target_range,
                reduction_db, nfft=nfft)
        except Exception as exc:  # noqa: BLE001 - 预览失败保持原声并提示
            self.info_label.setText(str(exc))
            return base

    def set_timepitch_preview(self, pcm: np.ndarray) -> None:
        """P2-7：变速/音高后的 PCM 作为预览基底（时长随之变化）。"""
        self._timepitch_pcm = pcm
        self._duration = max(0.01, pcm.shape[1] / self._pcm_rate)
        self._refresh_stack_preview()

    def clear_timepitch_preview(self) -> None:
        if self._timepitch_pcm is None:
            return
        self._timepitch_pcm = None
        if self._raw_pcm is not None:
            self._duration = max(0.01, self._raw_pcm.shape[1] / self._pcm_rate)
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
        # P0-3：选区处理（静音/标准化）随 PCM 与时间线片段可用性启用。
        self._selection_edit_buttons: list[QPushButton] = []
        for button in self.findChildren(QPushButton):
            if button.text() in buttons_by_text:
                button.setEnabled(False)
                button.setToolTip(unavailable)
            elif button.text() in ("静音", "标准化", "裁剪", "删除"):
                self._selection_edit_buttons.append(button)
                button.setEnabled(False)
                button.setToolTip(unavailable)
            elif button.text() == "应用时间与音高":
                # P2-7 + §6.2：预览按钮（提交按钮在包装层补建）。
                self._timepitch_button = button
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
    # P0-3：处理栈提交（clip_id, audio_edit, label）——由宿主执行工程命令。
    audio_edit_committed = pyqtSignal(str, object, str)
    # §6.2：改长度事务（计划已通过冲突检查；控制器构建项目/序列命令）。
    length_change_committed = pyqtSignal(str, object, str)
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
            self._panel._status_hook = self._status.setText
            self._panel.gain_requested.connect(self._on_gain_requested)
            self._panel.fadeApplied.connect(self._on_fade_requested)
            self._panel.fadeCurveChanged.connect(self._on_fade_curve_requested)
            self._panel.undoRequested.connect(self._on_undo_requested)
            self._panel.redoRequested.connect(self._on_redo_requested)
            self._panel.commitAndCloseRequested.connect(self._return_to_timeline)
            self._panel.fftSizeChanged.connect(self._on_fft_size_changed)
            self._panel._audition.playing_changed.connect(self.audition_active_changed.emit)
            # P0-3 控件映射：静音/标准化生成步骤；机架旁路/删除/清空走命令。
            self._panel.silenceRequested.connect(self._on_silence_requested)
            self._panel.normalizeRequested.connect(self._on_normalize_requested)
            self._panel.processStepToggled.connect(self._on_stack_step_toggled)
            self._panel.processStepRemoved.connect(self._on_stack_step_removed)
            # §6.2：改长度提交（变速/音高、删除区间、裁剪）走项目/序列事务。
            self._panel.ensure_timepitch_commit_button()
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

    def _selection_output_range(self) -> tuple[int, int]:
        """画布选区（显示秒）→ 片段输出域半开样本区间（processing_rate）。"""
        panel = self._panel
        if panel is None or panel._duration <= 0.0:
            return (0, 1)
        t0, t1, _, _ = panel.canvas.getSelection()
        output_total = self._output_total_samples()
        frac0 = max(0.0, min(1.0, t0 / panel._duration))
        frac1 = max(0.0, min(1.0, t1 / panel._duration))
        start = int(round(frac0 * output_total))
        end = int(round(frac1 * output_total))
        return (start, max(start + 1, end))

    def _target_channels(self) -> tuple[int, ...]:
        panel = self._panel
        channels = (-1,) if panel is None else (panel.canvas._edit_channel,)
        if channels[0] < 0:
            count = self._pcm_channel_count()
            return tuple(range(min(2, max(1, count))))
        return (min(1, channels[0]),)

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

    def _on_normalize_requested(self, t0: float, t1: float, target_db: float) -> None:
        panel = self._panel
        if panel is None or panel._raw_pcm is None or self._clip is None:
            return
        # 显示域与输出域同长时按下标直接取样；速度 ≠ 1 时按比例映射。
        raw = panel._raw_pcm
        duration = max(1e-9, panel._duration)
        src_total = raw.shape[1]
        lo = int(round(max(0.0, t0) / duration * src_total))
        hi = int(round(min(duration, t1) / duration * src_total))
        channels = self._target_channels()
        usable = [c for c in channels if c < raw.shape[0]]
        if hi <= lo or not usable:
            return
        window = np.abs(raw[usable, lo:hi])
        peak = float(window.max()) if window.size else 0.0
        if peak <= 1e-6:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.no_signal", "No signal to normalize"))
            return
        import math as _math
        gain_db = target_db - 20.0 * _math.log10(peak)
        start, end = self._selection_output_range()
        step = make_step(
            "normalize", start=start, end=end, channels=self._target_channels(),
            gain_db=round(gain_db, 2),
            label=I18N.tr("mixer.clip_editor.step_normalize", "Normalize"))
        self._commit_audio_edit(
            append_step(self._clip.audio_edit, step),
            I18N.tr("mixer.clip_editor.normalize_selection", "Normalize selection"))

    def _on_stack_step_toggled(self, step_id: str, enabled: bool) -> None:
        if self._clip is None or self._clip.audio_edit is None:
            return
        self._commit_audio_edit(
            replace_step_enabled(self._clip.audio_edit, step_id, enabled),
            I18N.tr("mixer.clip_editor.toggle_step", "Toggle audio edit step"))

    def _on_stack_step_removed(self, step_id: str) -> None:
        if self._clip is None or self._clip.audio_edit is None:
            return
        self._commit_audio_edit(
            remove_step(self._clip.audio_edit, step_id),
            I18N.tr("mixer.clip_editor.remove_step", "Remove audio edit step"))

    def _on_stack_cleared(self) -> None:
        if self._clip is None or self._clip.audio_edit is None:
            return
        self._commit_audio_edit(
            None, I18N.tr("mixer.clip_editor.clear_stack", "Clear audio edit stack"))

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

    def _on_channel_state_changed(self, channel: int, _muted: bool, phase_inverted: bool) -> None:
        """P1-5：反极性是工程状态（channel mask 步骤）；静音/独奏是监听态。"""
        clip, panel = self._clip, self._panel
        if clip is None or self._locked or panel is None or not 0 <= channel < 2:
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
        clip = self._clip
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
            independent = box.checkBox()
        if independent is not None:
            independent.setText(I18N.tr(
                "mixer.clip_editor.independent_audio",
                "Independent audio edit (linked video stays put)"))
            independent.setToolTip(I18N.tr(
                "mixer.clip_editor.independent_audio_tip",
                "By default the linked video clip keeps its sync — length "
                "changes that would break A/V alignment need this explicit "
                "choice"))
        box.exec()
        clicked = box.clickedButton()
        if clicked is gap_button:
            return False, bool(independent is not None and independent.isChecked())
        if clicked is ripple_button:
            return True, bool(independent is not None and independent.isChecked())
        return None, False

    def _commit_length_change(self, new_edit: dict, label: str,
                              *, old_output_samples: int | None = None) -> None:
        """统一改长度提交：ripple 选择 → 冲突计划 → 项目/序列事务。"""
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
            allow_independent_audio=independent)
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
        """P1-6：BS.1770 实测响度匹配 → 峰值受限的 range_gain 步骤提交。"""
        clip, panel = self._clip, self._panel
        if clip is None or self._locked or panel is None or panel._raw_pcm is None:
            return
        from src.features.edit.core.loudness_analyzer import analyze_loudness
        lo, hi = self._pcm_range(t0, t1)
        rate = panel._pcm_rate
        if hi - lo < int(0.4 * rate):
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.selection_too_short",
                "Select at least 400 ms for a loudness measurement"))
            return
        report = analyze_loudness(panel._raw_pcm[:, lo:hi], float(rate))
        if not getattr(report, "backend_available", False):
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.loudness_unavailable",
                "Loudness backend unavailable for this format"))
            return
        integrated = float(report.integrated_lufs)
        true_peak = float(report.true_peak_dbtp)
        gain = min(target_lufs - integrated, tp_ceiling - true_peak)
        if gain <= 0.05:
            panel.info_label.setText(I18N.tr(
                "mixer.clip_editor.loudness_capped",
                f"Already at {integrated:+.1f} LUFS / {true_peak:+.1f} dBTP — "
                f"peak-limited, no gain applied"))
            return
        start, end = self._selection_output_range()
        step = make_step(
            "range_gain", start, end, self._target_channels(),
            gain_db=round(gain, 2),
            label=I18N.tr("mixer.clip_editor.step_loudness", "Loudness match"))
        self._commit_audio_edit(
            append_step(clip.audio_edit, step),
            I18N.tr("mixer.clip_editor.match_loudness", "Match target loudness"))

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
