"""EditControllerMixin — edit/timeline/clips handlers extracted from MainWindow.

职责（Responsibilities）:
    - 剪辑页（Edit）控制层，MainWindow 最大的功能 Mixin：时间线切换/
      创建、素材入轨、工程文件操作（新建/保存/另存/导入导出）、
      Comp Clip 显式管理与嵌套序列（Compound Clip）、多机位（含波形
      自动同步）、剪辑 → Color 页联动与 ColorStack/分级 Copy-Paste、
      Edit Properties 面板、音频混音器与 ADR take sheet、音频 → 字幕
      转写（ASR worker）、字幕 → 配音（TTS worker）、字幕剪辑操作与
      画布变换手柄（SubtitleTransformSession）、场景剪切检测、
      Render Cache 智能自动评估、Motion Graphic 空闲缓存调度。
    - 本 Mixin 不持有状态模型：所有 ``self.*`` 属性引用解析到宿主
      MainWindow 实例（edit_page、timeline_panel、engine、
      _current_edit_project 等）。

与引擎/页面的边界（Boundary）:
    - 工程数据与命令归 edit feature（commands/selection via
      commands_facade）：本层组装参数并派发命令对象，不直接改写
      剪辑/轨道结构；渲染预览经 timeline_preview_renderer 请求，
      不直接操作 OpenGL。
    - 时间线/媒体池 UI 归 EditPage 与 TimelinePanel：本层通过
      signal_hub 信号和面板 API 同步状态。
    - ``src/features/edit`` 为可选依赖（color-only 构建可物理缺失），
      facade 导入失败时以 RuntimeError 占位。

信号协议（Signal protocol）:
    - 入站：EditPage.signal_hub 的 timeline_switched 等信号、
      时间线上下文菜单动作、ASR/TTS worker 的 completed/failed/
      progress 信号。
    - 出站：无自定义 Qt 信号（worker 类除外）；经命令栈、
      ``_refresh_edit_preview`` 类辅助方法和状态栏消息驱动 UI。
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from fractions import Fraction

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from PyQt6.QtWidgets import QMessageBox, QFileDialog
from PyQt6.QtCore import QObject, QThread, pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QPixmap

from src.features.edit.core.playback_debug import playback_trace
from src.shared.i18n.i18n import I18N

try:
    # E01: append 拒绝（区间重叠/无目标轨）的领域错误类型
    from src.features.edit.core.timeline_ops import TimelineOperationError
except ImportError:  # pragma: no cover - color-only builds
    class TimelineOperationError(ValueError):
        """Fallback when the edit feature is unavailable."""


# Conditional import via facade -- avoid deep feature internal import
try:
    from src.features.edit.commands_facade import get_project_io_classes
    _io = get_project_io_classes()
    EDIT_PROJECT_EXTENSION = _io["EDIT_PROJECT_EXTENSION"] if _io else None
except ImportError:
    EDIT_PROJECT_EXTENSION = None

# Facade imports - consolidate deep edit imports behind the facade boundary.
# Keep this optional so color-only builds can physically omit src/features/edit.
try:
    from src.features.edit.commands_facade import (
        get_command_classes,
        get_clip_params_classes,
        get_selection_classes,
        get_effect_instance_class,
    )
except ImportError:
    def _missing_edit_facade(*args, **kwargs):
        raise RuntimeError("Edit feature is unavailable")

    get_command_classes = _missing_edit_facade
    get_clip_params_classes = _missing_edit_facade
    get_selection_classes = _missing_edit_facade
    get_effect_instance_class = _missing_edit_facade

logger = logging.getLogger(__name__)

#: 建栈时需完整携带的 ColorStackData v2/v3 内容字段清单（审查 S1：
#: 手抄清单曾漂移——本地副本与片段版本空域初始化共用此常量，
#: 覆盖曲线侧栏 v3 键 curves_soft_clip/curves_edit 与 video_denoise）。
V2_SNAPSHOT_CONTENT_FIELDS = (
    "grading_params", "curves_data", "hsl_curve_data",
    "mixer_params", "soft_clip_params", "lut_path",
    "lut_intensity", "layers", "active_layer_index",
    "trackings", "raw_params", "video_denoise",
    "curves_soft_clip", "curves_edit",
)


def _render_cache_manifest_from_value(value: Any) -> Any | None:
    """Normalize a project cache entry without coupling callers to storage."""
    try:
        from src.features.edit.core.render_cache import RenderCacheManifest
        if isinstance(value, dict):
            return RenderCacheManifest.from_dict(value)
        return value if isinstance(value, RenderCacheManifest) else None
    except (ImportError, TypeError, ValueError):
        return None


def _remove_render_cache_file(path: str) -> None:
    """Remove one generated cache file and its interrupted encoder sidecar."""
    raw = str(path or "").strip()
    if not raw:
        return
    targets = {os.path.abspath(raw)}
    extension = os.path.splitext(raw)[1] or ".mp4"
    targets.add(os.path.abspath(raw + ".tmp" + extension))
    # Older realtime-cache builds used a separate suffix.  Remove it too so
    # interrupted recordings from those builds do not survive a re-render.
    targets.add(os.path.abspath(raw + ".realtime.tmp" + extension))
    for target in targets:
        try:
            if os.path.isfile(target) or os.path.islink(target):
                os.remove(target)
                logger.info("[RenderCache] Removed obsolete cache file: %s", target)
        except OSError as exc:
            logger.debug("[RenderCache] Could not remove obsolete cache %s: %s", target, exc)


@dataclass
class _CreateStampedColorStackCommand:
    """Project-level command: create a ColorStackData with stamped fields.

    AUD-2708: 调色栈创建与 ownership 盖章（以及本地副本的 v2 快照复制）
    此前拆成「命令栈建栈 + 裸 ``replace`` 盖章」两段非原子操作——盖章
    不经任何命令栈；媒体池撤销建栈后 clip 绑定悬空，redo 重建的栈还
    丢盖章（``CreateColorStackCommand`` 每次重放生成全新 stack_id，redo
    后绑定必然悬空）。本命令把两段并为一个事务：

    - ``apply``: 建栈一次并按 ``stamp_fields`` 盖章，stack_id 在
      undo/redo 间保持稳定（重放插回同一 ``ColorStackData`` 对象）；
    - ``undo``: 整栈移除；若同 id 的栈已被后续操作替换，则记录并在
      undo 时恢复原栈，不破坏他人状态。

    与 ``CreateColorStackCommand`` 同域（EditProject），经媒体池命令栈
    提交，构成单一撤销单元。
    """

    command_id: str
    label: str
    asset_id: str | None = None
    name: str = "Default"
    stamp_fields: dict[str, Any] = field(default_factory=dict)
    # 创建出的栈对象（redo 复用，保证 stack_id 稳定）。
    _stack: Any = None
    # apply 时同 id 已存在的栈（碰撞守卫，undo 时恢复）。
    _displaced: Any = None

    @property
    def created_stack_id(self) -> str | None:
        """ID of the created stack (available after apply)."""
        return getattr(self._stack, "stack_id", None)

    def apply(self, project: Any) -> Any:
        """Create (or re-insert on redo) the stamped color stack."""
        from src.shared.project.color_stack import ColorStackData

        if self._stack is None:
            base = ColorStackData.create(
                asset_id=self.asset_id, name=self.name)
            stamped = (
                replace(base, **self.stamp_fields)
                if self.stamp_fields else base)
            object.__setattr__(self, "_stack", stamped)
        stack = self._stack
        displaced = project.color_stacks.get(stack.stack_id)
        object.__setattr__(
            self, "_displaced",
            None if displaced is stack else displaced)
        new_stacks = dict(project.color_stacks)
        new_stacks[stack.stack_id] = stack
        logger.debug(
            "CreateStampedColorStackCommand applied: %s (id=%s)",
            self.name, stack.stack_id)
        return replace(project, color_stacks=new_stacks)

    def undo(self, project: Any) -> Any:
        """Remove the created stack, restoring any displaced original."""
        stack = self._stack
        if stack is None:
            return project
        if project.color_stacks.get(stack.stack_id) is not stack:
            # 另一个操作已替换该 id 的栈——不要动它。
            return project
        new_stacks = dict(project.color_stacks)
        if self._displaced is not None:
            new_stacks[stack.stack_id] = self._displaced
        else:
            del new_stacks[stack.stack_id]
        logger.debug(
            "CreateStampedColorStackCommand undone: removed stack %s",
            stack.stack_id)
        return replace(project, color_stacks=new_stacks)

    @staticmethod
    def create(
        asset_id: str | None,
        name: str,
        stamp_fields: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> "_CreateStampedColorStackCommand":
        """Factory: build a stamped-create command."""
        return _CreateStampedColorStackCommand(
            command_id=str(uuid.uuid4()),
            label=label or f"Create Color Stack '{name}'",
            asset_id=asset_id,
            name=name,
            stamp_fields=dict(stamp_fields or {}),
        )


def _fps_token(value: Any) -> str:
    """Normalize timeline FPS values for render-cache contract checks."""
    if value is None:
        return ""
    if hasattr(value, "num") and hasattr(value, "den"):
        try:
            return f"{int(value.num)}/{int(value.den)}"
        except (TypeError, ValueError, ZeroDivisionError):
            return ""
    raw = str(value).strip()
    if "/" in raw:
        try:
            num, den = raw.split("/", 1)
            return f"{int(num)}/{int(den)}"
        except (TypeError, ValueError, ZeroDivisionError):
            return raw
    try:
        fraction = Fraction(float(value)).limit_denominator(100000)
        return f"{fraction.numerator}/{fraction.denominator}"
    except (TypeError, ValueError, ZeroDivisionError):
        return raw


def _preview_content_aspect(engine: Any) -> float | None:
    """Return the source/display aspect for a captured Color preview.

    ``QWidget.grab()`` captures the whole viewport, including any matte that
    the viewer uses while fitting a source into a differently-shaped window.
    Prefer the renderer's presentation geometry, which is independent of the
    widget size, and fall back to the loaded image/scene geometry.
    """
    renderer = getattr(engine, "renderer", None)
    if bool(getattr(engine, "_is_video_mode", False)):
        candidates = (
            getattr(renderer, "current_video_display_size", None),
            getattr(renderer, "video_source_size", None),
            getattr(engine, "scene_size", None),
        )
    else:
        # A renderer can retain the last video's metadata after an image is
        # loaded. Do not let that stale aspect ratio crop the new image.
        candidates = (getattr(engine, "scene_size", None),)
    for size in candidates:
        try:
            width, height = float(size[0]), float(size[1])
        except (TypeError, IndexError, KeyError, ValueError):
            continue
        if width > 0.0 and height > 0.0:
            return width / height

    source = getattr(engine, "img_source_full", None)
    shape = getattr(source, "shape", None)
    if shape is not None and len(shape) >= 2:
        try:
            height, width = float(shape[0]), float(shape[1])
        except (TypeError, ValueError):
            return None
        if width > 0.0 and height > 0.0:
            return width / height
    return None


def _crop_pixmap_to_aspect(pixmap: QPixmap, target_aspect: float | None) -> QPixmap:
    """Remove viewer matte from a captured preview without distorting it."""
    if pixmap.isNull() or target_aspect is None or target_aspect <= 0.0:
        return pixmap

    source_width = pixmap.width()
    source_height = pixmap.height()
    if source_width <= 0 or source_height <= 0:
        return pixmap

    source_aspect = source_width / source_height
    if abs(source_aspect - target_aspect) <= 0.01:
        return pixmap

    if source_aspect > target_aspect:
        crop_width = max(1, min(source_width, round(source_height * target_aspect)))
        x = (source_width - crop_width) // 2
        return pixmap.copy(x, 0, crop_width, source_height)

    crop_height = max(1, min(source_height, round(source_width / target_aspect)))
    y = (source_height - crop_height) // 2
    return pixmap.copy(0, y, source_width, crop_height)


class _SpeechToTextWorker(QThread):
    """Run the registered local transcript provider without blocking Qt."""

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(object, str)  # fraction (0..1 or None), stage message

    def __init__(self, service: Any, asset_id: str, fingerprint: Any,
                 fps: str, parent: Any = None, *,
                 options: dict[str, Any] | None = None) -> None:
        """保存转写请求参数（service/asset/fingerprint/fps/options）。"""
        super().__init__(parent)
        self._service = service
        self._asset_id = asset_id
        self._fingerprint = fingerprint
        self._fps = fps
        self._options = dict(options or {})

    def run(self) -> None:
        """线程体：请求 adaptive 链转写分析，经 Qt 信号回报结果。

        成功 ``completed``（analysis 对象），失败 ``failed``（错误文本），
        期间经 ``progress`` 上报进度；被请求中断时静默返回。
        """
        try:
            def on_progress(fraction: float | None, message: str | None) -> None:
                self.progress.emit(fraction, str(message or "正在处理…"))

            job, error = self._service.request_analysis(
                self._asset_id,
                self._fingerprint,
                fps=self._fps,
                # The clip action uses the adaptive chain: Qwen3-ASR first,
                # then optional Demucs/WhisperX fallbacks when quality is low.
                provider_ids=("transcript_auto",),
                options=self._options,
                on_progress=on_progress,
            )
            if error:
                self.failed.emit(str(error))
                return
            status = getattr(getattr(job, "status", None), "value", "")
            if status in {"failed", "cancelled"}:
                message = getattr(job, "error_message", None) or "分析任务失败"
                self.failed.emit(str(message))
                return
            if self.isInterruptionRequested():
                return
            analysis = self._service.get_analysis(self._asset_id)
            if analysis is None or not getattr(analysis, "transcript", ()):
                self.failed.emit("语音转文字没有产生可用的转录结果")
                return
            self.completed.emit(analysis)
        except Exception as exc:  # provider/model errors become user-visible UI errors
            logger.exception("Speech-to-text worker failed")
            self.failed.emit(str(exc))


class _AdrCalibrationWorker(QThread):
    """AUD-2713: 在工作线程执行 ADR 往返延迟校准，结果经信号回主线程。

    ``measure_round_trip_latency`` 的往返 click 测量通常耗时数百毫秒以上，
    直接在按钮槽里同步执行会冻结主线程事件循环；音频回调时序也可能受
    阻塞影响导致测量失真。失败时 emit None（面板显示未校准）。
    """

    completed = pyqtSignal(object)

    def __init__(self, device_id: str | None, parent: Any = None) -> None:
        super().__init__(parent)
        self._device_id = device_id

    def run(self) -> None:
        from src.features.edit.core.recording_manager import (
            measure_round_trip_latency,
        )
        try:
            calibration = measure_round_trip_latency(device_id=self._device_id)
        except Exception as exc:
            logger.warning("Latency calibration failed: %s", exc)
            calibration = None
        self.completed.emit(calibration)


class _SubtitleToSpeechWorker(QThread):
    """Synthesize a subtitle track off the UI thread."""

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, provider: Any, cues: tuple[Any, ...], fps: float,
                 settings: Any = None, parent: Any = None) -> None:
        """保存合成请求参数（provider/cues/fps/settings）。"""
        super().__init__(parent)
        self._provider = provider
        self._cues = cues
        self._fps = fps
        self._settings = settings

    def run(self) -> None:
        """线程体：把字幕 cues 合成为语音，经 Qt 信号回报结果。"""
        try:
            from src.features.edit.ai_editing.tts_provider import SpeechCue
            result = self._provider.synthesize(
                tuple(SpeechCue(text=str(c.text), start_frame=int(c.timeline_start))
                      for c in self._cues),
                fps=self._fps,
                settings=self._settings,
            )
            self.completed.emit(result)
        except Exception as exc:
            logger.exception("Subtitle-to-speech synthesis failed")
            self.failed.emit(str(exc))


# Fields that may be modified by a transform-handle drag. Captured at drag
# start so the release-time command carries a correct pre-drag snapshot.
_SUBTITLE_TRANSFORM_FIELDS: tuple[str, ...] = (
    "translate_x", "translate_y",
    "scale_x", "scale_y",
    "rotation",
    "pivot_mode", "pivot_x", "pivot_y",
)


class SubtitleTransformSession:
    """Tracks a subtitle transform-handle drag for correct undo semantics.

    SUB-P0-02: previously the live drag mutated the timeline directly and the
    pointer-up handler pushed a command whose ``_previous`` was derived from
    the already-final clip — making Undo a no-op. This session captures the
    immutable *before* state at drag start; the release handler builds a single
    ``UpdateSubtitleClipCommand.create_with_previous(before → after)`` so Undo
    restores the pre-drag transform.

    The session is intentionally lightweight (no Qt dependency) so it can be
    unit-tested in isolation.
    """

    __slots__ = ("clip_id", "before", "_accumulated", "scope",
                 "track_id", "before_track_style")

    def __init__(self, clip_id: str, before: dict, scope: str = "custom",
                 track_id: str = "", before_track_style: Any = None) -> None:
        """记录 clip id、拖拽前快照与作用范围（custom/track）。"""
        self.clip_id: str = clip_id
        self.before: dict = dict(before)
        self._accumulated: dict = {}
        self.scope = scope
        self.track_id = track_id
        self.before_track_style = before_track_style

    @classmethod
    def begin(cls, clip: Any, style: dict | None = None,
              scope: str = "custom", before_track_style: Any = None
              ) -> "SubtitleTransformSession":
        """Snapshot the transform-relevant fields of a subtitle clip."""
        style = style or {}
        before = {
            name: style.get(name, getattr(clip, name))
            for name in _SUBTITLE_TRANSFORM_FIELDS
            if hasattr(clip, name)
        }
        before["style_override"] = getattr(clip, "style_override", None)
        return cls(
            getattr(clip, "clip_id"), before, scope=scope,
            track_id=getattr(clip, "track_id", ""),
            before_track_style=before_track_style,
        )

    def accumulate(self, delta: dict) -> None:
        """Record the latest transient delta for diagnostics/metrics.

        The model is updated live by the controller; this keeps a running view
        of the net delta applied during the drag. Values present in ``delta``
        overwrite previous accumulated values (last-write-wins per field).
        """
        for key, value in delta.items():
            self._accumulated[key] = value

    @property
    def accumulated(self) -> dict:
        """拖拽期间净累计增量的副本（每字段 last-write-wins）。"""
        return dict(self._accumulated)


# DaVinci-style idle refresh: a grade commit defers the clip-strip rebuild by
# this long (and skips it entirely while playback is running), so switching
# clips or crossing a clip boundary never blanks the strip's thumbnails.
_IDLE_CLIP_STRIP_REFRESH_MS = 400


class EditControllerMixin:
    """Mixin providing edit project, timeline, clip, and effect handlers."""

    # ------------------------------------------------------------------
    # Timeline switching / creation
    # ------------------------------------------------------------------

    def _on_timeline_switched(self, timeline_id: str) -> None:
        """
        Handle timeline switch — load the timeline into TimelinePanel.

        Connected to EditPage.signal_hub.timeline_switched.
        (V-10, UI-27) Syncs active timeline highlight in MediaPoolPanel.
        """
        if not self._current_edit_project:
            logger.warning("Timeline switch: no edit project")
            return

        # Update project's active timeline
        if hasattr(self._current_edit_project, 'set_active_timeline'):
            try:
                self._current_edit_project = self._current_edit_project.set_active_timeline(timeline_id)
            except ValueError:
                # Timeline not in project's dict yet — use dataclass replace
                from dataclasses import replace as _dc_replace
                self._current_edit_project = _dc_replace(
                    self._current_edit_project,
                    active_timeline_id=timeline_id,
                )
        elif hasattr(self._current_edit_project, '__dataclass_fields__'):
            from dataclasses import replace as _dc_replace
            self._current_edit_project = _dc_replace(
                self._current_edit_project,
                active_timeline_id=timeline_id,
            )

        # The pool renders TimelineSequenceRef values from the project, not
        # the full sequence cache.  Publish the active-project replacement so
        # a switch/delete cannot leave the pool showing stale timeline cards.
        self.media_pool_panel.set_project(self._current_edit_project)
        self.timeline_panel.set_project(self._current_edit_project)
        self.media_pool_panel.set_current_timeline(timeline_id)

        # Load full TimelineSequence (with tracks/clips) from cached sequences.
        # A timeline card is a lightweight project reference, so lazily create
        # its editable sequence the first time it is opened.
        full_sequence = self._current_timeline_sequences.get(timeline_id)
        if full_sequence is None:
            timeline_ref = self._current_edit_project.get_timeline_by_id(timeline_id)
            if timeline_ref is None:
                logger.warning(f"Timeline switch: timeline {timeline_id} not found")
                return
            full_sequence = self._create_empty_timeline_sequence(timeline_ref)
            if full_sequence is None:
                return
            self._current_timeline_sequences[timeline_id] = full_sequence

        # Reuse existing renderer if possible (avoids GPU thread crash on 2nd timeline)
        if self.timeline_preview_renderer is not None:
            # Update the renderer before EditPage requests the new playhead
            # frame. Otherwise an empty timeline can synchronously hit the old
            # renderer cache and paint the previous timeline one more time.
            self.timeline_preview_renderer.set_sequence(full_sequence)
            if hasattr(self.timeline_preview_renderer, 'set_project'):
                self.timeline_preview_renderer.set_project(self._current_edit_project)
        self.timeline_panel.load_timeline(full_sequence)
        self.edit_page.set_timeline(full_sequence)
        # Deliver hosts the same Program viewer and timeline.  If the user
        # switches timelines from the Program monitor while on Deliver, keep
        # the export draft bound to that newly active sequence so queued jobs
        # never retain the previous hidden timeline selection.
        deliver_page = getattr(self, "deliver_page", None)
        if (
            getattr(self, "current_page", None) == "Deliver"
            and deliver_page is not None
        ):
            set_sequence = getattr(deliver_page, "set_sequence", None)
            if callable(set_sequence):
                set_sequence(full_sequence)
        if self.timeline_preview_renderer is not None:
            self._request_current_timeline_preview_frame()
        else:
            self._init_timeline_preview_renderer()
        logger.info("Timeline switched and loaded: %s", timeline_id)

    def _on_timeline_switch_requested(self, timeline_id: str) -> None:
        """Forward timeline switch request to _on_timeline_switched."""
        self._on_timeline_switched(timeline_id)

    @staticmethod
    def _rational_for_timeline_fps(fps_value: float):
        """Build an exact Rational for the supported sequence timebases."""
        from src.shared.media.metadata import Rational

        for expected, numerator, denominator in (
            (23.976, 24000, 1001),
            (29.97, 30000, 1001),
            (59.94, 60000, 1001),
        ):
            if abs(fps_value - expected) < 0.001:
                return Rational(numerator, denominator)
        if abs(fps_value - round(fps_value)) < 0.001:
            return Rational(int(round(fps_value)), 1)
        return Rational(int(round(fps_value * 1000)), 1000)

    def _create_empty_timeline_sequence(self, timeline_ref):
        """Materialize an editable sequence for a project-level timeline ref."""
        try:
            from src.features.edit.commands_facade import get_timeline_classes

            timeline_classes = get_timeline_classes()
            if timeline_classes is None:
                raise ImportError
            TimelineSequence = timeline_classes["TimelineSequence"]
            EditTrack = timeline_classes["EditTrack"]
        except ImportError:
            logger.warning("Edit module unavailable; cannot materialize timeline")
            return None

        v1_track = EditTrack(
            track_id=str(uuid.uuid4()), name="V1", track_type="video", index=0,
            clips=(), transitions=(), is_enabled=True, is_locked=False,
            is_muted=False, is_solo=False,
        )
        a1_track = EditTrack(
            track_id=str(uuid.uuid4()), name="A1", track_type="audio", index=0,
            clips=(), transitions=(), is_enabled=True, is_locked=False,
            is_muted=False, is_solo=False,
        )
        return TimelineSequence(
            timeline_id=timeline_ref.timeline_id,
            name=timeline_ref.name,
            fps=self._rational_for_timeline_fps(timeline_ref.fps),
            width=timeline_ref.width,
            height=timeline_ref.height,
            sample_rate=timeline_ref.sample_rate,
            format_settings=dict(getattr(timeline_ref, "format_settings", {})),
            video_tracks=(v1_track,),
            audio_tracks=(a1_track,),
            markers=(),
        )

    @staticmethod
    def _format_settings_from_dialog(settings: dict, existing: Optional[dict] = None) -> dict:
        """Keep non-timing sequence format options together and serializable."""
        result = dict(existing or {})
        for key in (
            "pixel_aspect_ratio",
            "field_order",
            "display_format",
            "audio_track_type",
            "audio_display_format",
            "start_timecode",
        ):
            if key in settings:
                result[key] = settings[key]
        return result

    @staticmethod
    def _timeline_settings_for_dialog(
        sequence,
        ref_format_settings: Optional[dict] = None,
    ) -> dict:
        """Adapt a TimelineSequence into the shared create/edit dialog format."""
        fps = sequence.fps
        settings = dict(ref_format_settings or {})
        settings.update(getattr(sequence, "format_settings", {}))
        settings.update(
            {
                "name": sequence.name,
                "fps": float(fps),
                "fps_num": getattr(fps, "num", int(round(float(fps)))),
                "fps_den": getattr(fps, "den", 1),
                "width": sequence.width,
                "height": sequence.height,
                "sample_rate": sequence.sample_rate,
            }
        )
        return settings

    @staticmethod
    def _scale_timeline_frame(frame: int, old_fps, new_fps) -> int:
        return int(round(frame * new_fps.num * old_fps.den / (new_fps.den * old_fps.num)))

    def _with_timeline_fps(self, sequence, new_fps):
        """Retain absolute timing while changing the sequence timebase."""
        old_fps = sequence.fps
        if not hasattr(old_fps, "num"):
            old_fps = self._rational_for_timeline_fps(float(old_fps))

        def scale_frame(frame: int) -> int:
            if old_fps == new_fps:
                return frame
            return self._scale_timeline_frame(frame, old_fps, new_fps)

        from fractions import Fraction

        # FE-B01：帧比例（Fraction），动画关键帧按比例缩放帧号
        frame_ratio = Fraction(new_fps.num * old_fps.den, new_fps.den * old_fps.num)

        def scale_animatable(param):
            if param is None or not hasattr(param, "time_scaled"):
                return param
            return param.time_scaled(frame_ratio)

        def scale_clip(clip):
            start = scale_frame(clip.timeline_start)
            end = scale_frame(clip.timeline_end)
            scaled = replace(
                clip,
                timeline_start=start,
                timeline_duration=max(1, end - start),
                fade_in_duration=scale_frame(clip.fade_in_duration),
                fade_out_duration=scale_frame(clip.fade_out_duration),
                animated_params={
                    name: scale_animatable(param)
                    for name, param in clip.animated_params.items()
                },
                effects=tuple(
                    getattr(effect, "with_animated_params_scaled", lambda _f: effect)(
                        frame_ratio
                    )
                    for effect in clip.effects
                ),
                _timeline_fps_num=new_fps.num,
                _timeline_fps_den=new_fps.den,
            )
            return scaled

        def scale_track(track):
            clips = [scale_clip(clip) for clip in track.clips]
            transitions = tuple(
                replace(
                    transition,
                    center_frame=scale_frame(transition.center_frame),
                    duration=max(1, scale_frame(transition.duration)),
                )
                for transition in track.transitions
            )
            # FE-B01：轨道 automation 关键帧同域缩放
            automation = getattr(track, "automation", None) or {}
            scaled_automation = {
                name: track_kf.time_scaled(frame_ratio)
                if hasattr(track_kf, "time_scaled") else track_kf
                for name, track_kf in automation.items()
            }
            def _scale_sub(sub):
                updates = dict(
                    timeline_start=scale_frame(sub.timeline_start),
                    timeline_duration=max(
                        1,
                        scale_frame(sub.timeline_end)
                        - scale_frame(sub.timeline_start),
                    ),
                )
                # F-R01/R20 + N17/RF01：词级时间码真实存放在
                # caption_metadata.word_timings（timeline_v2 的
                # SubtitleClip 没有顶层 word_timings 字段——旧代码读
                # getattr(sub, ...) 永远 None，24→48 后 cue 48–96 而
                # 词级仍 24–48，首词高亮在 cue 出现前结束）。显式重建
                # metadata 并保留其余字段（speaker/status/extra 等）；
                # 极短词降帧后 start==end 时保底 1 帧（词至少显示 1 帧）。
                metadata = getattr(sub, "caption_metadata", None)
                if metadata is not None and metadata.word_timings:
                    scaled_words = []
                    for word in metadata.word_timings:
                        start = scale_frame(word.start_frame)
                        end = max(start + 1, scale_frame(word.end_frame))
                        scaled_words.append(replace(
                            word, start_frame=start, end_frame=end))
                    updates["caption_metadata"] = replace(
                        metadata, word_timings=tuple(scaled_words))
                return replace(sub, **updates)

            subtitle_clips = tuple(
                _scale_sub(sub)
                for sub in getattr(track, "subtitle_clips", ()) or ()
            )
            rebuilt = replace(
                track,
                clips=tuple(clips),
                transitions=transitions,
            )
            if scaled_automation:
                rebuilt = replace(rebuilt, automation=scaled_automation)
            if subtitle_clips:
                rebuilt = replace(rebuilt, subtitle_clips=subtitle_clips)
            return rebuilt

        # FE-B01：序列级 markers 与 master automation 同域缩放——审计实测
        # 24→48 后字幕/marker/动画仍留 24fps 帧号，实时时间错位一倍。
        scaled_markers = tuple(
            replace(marker, frame=scale_frame(marker.frame))
            for marker in sequence.markers
        )
        master_automation = getattr(sequence, "master_automation", None) or {}
        scaled_master = {
            name: track_kf.time_scaled(frame_ratio)
            if hasattr(track_kf, "time_scaled") else track_kf
            for name, track_kf in master_automation.items()
        }

        rebuilt = replace(
            sequence,
            fps=new_fps,
            video_tracks=tuple(scale_track(track) for track in sequence.video_tracks),
            audio_tracks=tuple(scale_track(track) for track in sequence.audio_tracks),
            # F-R01/R20：真实字幕轨必须参与重建（模型字幕严格放在
            # subtitle_tracks，此前漏传使 24→48 后 cue 仍 24fps 帧号）
            subtitle_tracks=tuple(
                scale_track(track) for track in sequence.subtitle_tracks
            ),
            range_start=(scale_frame(sequence.range_start) if sequence.range_start is not None else None),
            range_end=(scale_frame(sequence.range_end) if sequence.range_end is not None else None),
            markers=scaled_markers,
        )
        if scaled_master:
            rebuilt = replace(rebuilt, master_automation=scaled_master)
        return rebuilt

    def _on_timeline_settings_requested(self, timeline_id: str) -> None:
        """Edit one timeline through the same sequence state used by playback."""
        project = self._current_edit_project
        if project is None:
            return

        timeline_ref = project.timelines.get(timeline_id)
        if timeline_ref is None:
            logger.warning("Timeline settings: timeline not found: %s", timeline_id)
            return

        sequence = self._current_timeline_sequences.get(timeline_id)
        if sequence is None:
            sequence = self._create_empty_timeline_sequence(timeline_ref)
            if sequence is None:
                return
            self._current_timeline_sequences[timeline_id] = sequence

        from PyQt6.QtWidgets import QDialog
        from src.features.edit.ui.new_timeline_dialog import NewTimelineDialog

        existing_names = {
            ref.name for current_id, ref in project.timelines.items()
            if current_id != timeline_id
        }
        dialog = NewTimelineDialog(
            getattr(self, "config_manager", None),
            parent=self,
            existing_names=existing_names,
            initial_settings=self._timeline_settings_for_dialog(
                sequence,
                getattr(timeline_ref, "format_settings", {}),
            ),
            edit_mode=True,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # The modal dialog runs an event loop. Background edits may publish
        # while it is open; transform the latest canonical content and keep
        # that content as the Undo snapshot instead of resurrecting the old
        # sequence captured when the dialog opened.
        current_project = self._current_edit_project
        if (current_project is None or current_project.project_id != project.project_id
                or timeline_id not in current_project.timelines):
            logger.info("Discarded timeline settings after project/target changed")
            return
        project = current_project
        timeline_ref = project.timelines[timeline_id]
        sequence = self._current_timeline_sequences.get(timeline_id)
        if sequence is None:
            return

        settings = dialog.get_settings()
        new_fps = self._rational_for_timeline_fps(float(settings["fps"]))
        try:
            updated_sequence = self._with_timeline_fps(sequence, new_fps)
        except ValueError as exc:
            # A lower frame rate can collapse adjacent one-frame edits onto
            # the same tick. Reject the entire proposal before switching the
            # active timeline or touching its history; never let the Qt slot
            # propagate the model's overlap error.
            self.status_bar.showMessage(
                I18N.tr(
                    "status.timeline_settings_rejected",
                    "Timeline settings cannot preserve clips at the selected frame rate: {reason}",
                ).format(reason=str(exc)), 8000)
            return
        format_settings = self._format_settings_from_dialog(
            settings,
            getattr(sequence, "format_settings", {}),
        )
        updated_sequence = replace(
            updated_sequence,
            name=settings["name"],
            width=settings["width"],
            height=settings["height"],
            sample_rate=settings["sample_rate"],
            format_settings=format_settings,
        )
        # Settings and clip edits share the same timeline history. A separate
        # project snapshot history could overwrite clips edited after this
        # dialog, or apply old frame-number commands in the new FPS domain.
        if updated_sequence.to_dict() == sequence.to_dict():
            return
        from src.features.edit.core.timeline_settings_command import TimelineSettingsCommand

        if project.active_timeline_id != timeline_id:
            self._on_timeline_switched(timeline_id)
        command = TimelineSettingsCommand.create(sequence, updated_sequence)
        self._execute_timeline_edit_command(command, sequence)

        logger.info(
            "Timeline settings updated: %s (%sx%s @ %.3ffps)",
            timeline_id,
            updated_sequence.width,
            updated_sequence.height,
            float(updated_sequence.fps),
        )

    def _on_timeline_delete_requested(self, timeline_id: str) -> None:
        """Confirm and remove a timeline through the controller-owned state."""
        project = self._current_edit_project
        if project is None:
            return
        timeline_ref = project.timelines.get(timeline_id)
        if timeline_ref is None:
            return

        reply = QMessageBox.question(
            self,
            I18N.tr("edit.media_pool.delete_timeline_title", "Delete Timeline"),
            I18N.tr(
                "edit.media_pool.delete_timeline_message",
                "Are you sure you want to delete timeline '{name}'?",
                name=timeline_ref.name,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._remove_timeline_state(timeline_id)

    def _remove_timeline_state(self, timeline_id: str) -> bool:
        """Remove the ref and full sequence together, then select a valid successor."""
        project = self._current_edit_project
        if project is None or timeline_id not in project.timelines:
            return False

        updated_refs = dict(project.timelines)
        del updated_refs[timeline_id]
        updated_sequences = dict(self._current_timeline_sequences)
        updated_sequences.pop(timeline_id, None)

        active_timeline_id = project.active_timeline_id
        if active_timeline_id == timeline_id:
            active_timeline_id = next(iter(updated_refs), None)

        self._current_timeline_sequences = updated_sequences
        self._current_edit_project = replace(
            project,
            timelines=updated_refs,
            active_timeline_id=active_timeline_id,
            modified_at=time.time(),
        )
        self.media_pool_panel.set_project(self._current_edit_project)
        self.media_pool_panel.set_current_timeline(active_timeline_id)
        self.edit_project_dirty = True

        if active_timeline_id is not None:
            self._on_timeline_switched(active_timeline_id)
        else:
            self.timeline_panel.clear()
            show_provisional = getattr(self.timeline_panel, "show_provisional_timeline", None)
            if callable(show_provisional):
                show_provisional()
            if self.edit_page is not None:
                self.edit_page.set_timeline(None)
            reinitialize_renderer = getattr(self, "_init_timeline_preview_renderer", None)
            if callable(reinitialize_renderer):
                reinitialize_renderer()

        logger.info("Timeline deleted: %s", timeline_id)
        return True

    def _on_timeline_command_snapshot_requested(
        self,
        timeline_ids: list,
        callback,
    ) -> None:
        """Provide full TimelineSequence snapshots for batch-delete undo support.

        Connected to ``MediaPoolPanel.timeline_command_snapshot_requested``.
        The panel emits this signal right before pushing a batch-delete macro
        to its command stack, so we can capture the live TimelineSequence
        objects (with all tracks/clips) before they are removed.

        Args:
            timeline_ids: Timeline IDs that are about to be deleted.
            callback: ``dict.update``-compatible callable that receives a
                ``{timeline_id: TimelineSequence}`` mapping. The panel stores
                these snapshots so the edit controller can restore them when
                an undo re-introduces the timeline id.
        """
        snapshots: dict = {}
        sequences = getattr(self, "_current_timeline_sequences", None) or {}
        for tid in timeline_ids:
            sequence = sequences.get(tid)
            if sequence is not None:
                snapshots[tid] = sequence
        try:
            callback(snapshots)
        except Exception:
            logger.exception("timeline snapshot callback failed")

    def _on_asset_imported_renderer_sync(self, asset_id: str, metadata: dict) -> None:
        """UI-34: Sync renderer project reference after new asset import."""
        if self.timeline_preview_renderer and self._current_edit_project:
            self.timeline_preview_renderer.set_project(self._current_edit_project)
            prewarm_asset = getattr(self.timeline_preview_renderer, "prewarm_imported_asset", None)
            if callable(prewarm_asset):
                prewarm_asset(asset_id, metadata)
            logger.debug(f"[Renderer] Project ref refreshed after asset import: {asset_id}")

    def _on_timeline_create_requested(self) -> None:
        """
        Handle new timeline creation request from MediaPoolPanel.

        Shows NewTimelineDialog with full settings (fps, resolution, audio, etc.)
        and creates a TimelineSequence from the user's choices.
        (V-10, UI-32)
        """
        if not self._current_edit_project:
            logger.warning("Cannot create timeline: no edit project")
            return

        # Show the full NewTimelineDialog
        from PyQt6.QtWidgets import QDialog
        from src.features.edit.ui.new_timeline_dialog import NewTimelineDialog
        from src.shared.media.metadata import Rational

        # Collect existing timeline names to avoid duplicates
        existing_names = set()
        if hasattr(self._current_edit_project, 'timelines'):
            for tl_ref in self._current_edit_project.timelines.values():
                if hasattr(tl_ref, 'name'):
                    existing_names.add(tl_ref.name)

        config_manager = getattr(self, 'config_manager', None)
        dialog = NewTimelineDialog(config_manager, parent=self, existing_names=existing_names)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        settings = dialog.get_settings()
        name = settings["name"]
        fps = Rational(settings["fps_num"], settings["fps_den"])
        width = settings["width"]
        height = settings["height"]
        sample_rate = settings["sample_rate"]

        timeline_id = str(uuid.uuid4())

        # R-001: Delayed import for EditTrack, TimelineSequence via facade
        try:
            from src.features.edit.commands_facade import get_timeline_classes
            _tl = get_timeline_classes()
            if _tl is None:
                raise ImportError
            EditTrack = _tl["EditTrack"]
            TimelineSequence = _tl["TimelineSequence"]
        except ImportError:
            raise RuntimeError("Edit feature is unavailable")

        # Create default V1 and A1 tracks
        v1_track = EditTrack(
            track_id=str(uuid.uuid4()),
            name="V1",
            track_type="video",
            index=0,
            clips=(),
            transitions=(),
            is_enabled=True,
            is_locked=False,
            is_muted=False,
            is_solo=False,
        )
        a1_track = EditTrack(
            track_id=str(uuid.uuid4()),
            name="A1",
            track_type="audio",
            index=0,
            clips=(),
            transitions=(),
            is_enabled=True,
            is_locked=False,
            is_muted=False,
            is_solo=False,
        )

        new_timeline = TimelineSequence(
            timeline_id=timeline_id,
            name=name,
            fps=fps,
            width=width,
            height=height,
            sample_rate=sample_rate,
            format_settings=self._format_settings_from_dialog(settings),
            video_tracks=(v1_track,),
            audio_tracks=(a1_track,),
            markers=(),
        )

        # Add to cached sequences
        self._current_timeline_sequences[timeline_id] = new_timeline

        # Create a lightweight TimelineSequenceRef for the project
        import time as _time
        try:
            from src.features.edit.commands_facade import get_project_classes
            _proj = get_project_classes()
            if _proj is None:
                raise ImportError
            TimelineSequenceRef = _proj["TimelineSequenceRef"]
        except ImportError:
            raise RuntimeError("Edit feature is unavailable")

        fps_float = fps.to_fps() if hasattr(fps, 'to_fps') else float(fps)
        timeline_ref = TimelineSequenceRef(
            timeline_id=timeline_id,
            name=name,
            fps=fps_float,
            width=width,
            height=height,
            sample_rate=sample_rate,
            format_settings=self._format_settings_from_dialog(settings),
            created_at=_time.time(),
        )

        # Update project: add timeline ref + set as active
        new_timelines = dict(self._current_edit_project.timelines)
        new_timelines[timeline_id] = timeline_ref
        self._current_edit_project = replace(
            self._current_edit_project,
            timelines=new_timelines,
            active_timeline_id=timeline_id,
            modified_at=_time.time(),
        )

        # Update MediaPoolPanel project reference (also triggers _refresh_tree internally)
        self.media_pool_panel.set_project(self._current_edit_project)

        # Switch to new timeline
        self._on_timeline_switched(timeline_id)

        logger.info(f"Created new timeline: {name} ({timeline_id}), "
                     f"{width}x{height} @ {fps_float:.3f}fps")

    # ------------------------------------------------------------------
    # Audio clip → subtitle transcription
    # ------------------------------------------------------------------

    def _get_speech_timeline_view(self) -> Any | None:
        panel = getattr(self, "timeline_panel", None)
        if panel is None:
            return None
        getter = getattr(panel, "get_timeline_view", None)
        return getter() if callable(getter) else getattr(panel, "timeline_view", None)

    def _on_speech_to_text_requested(
        self, clip_id: str, track_id: str, asset_id: str
    ) -> None:
        """Start ASR for one audio clip selected from its context menu."""
        if getattr(self, "_speech_to_text_worker", None) is not None:
            worker = self._speech_to_text_worker
            if worker.isRunning():
                self._show_speech_to_text_message("已有语音转文字任务正在运行", warning=True)
                return

        timeline_view = self._get_speech_timeline_view()
        timeline = getattr(timeline_view, "_timeline", None)
        if timeline is None:
            timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
            if timeline is not None and timeline_view is not None:
                loader = getattr(timeline_view, "load_timeline", None)
                if callable(loader):
                    loader(timeline)
        clip = timeline.get_clip_by_id(clip_id) if timeline is not None else None
        track = timeline.get_track_by_id(track_id) if timeline is not None else None
        if clip is None or track is None or track.track_type != "audio":
            self._show_speech_to_text_message("请选择时间线上的音频片段", warning=True)
            return
        if asset_id and clip.asset_id != asset_id:
            asset_id = clip.asset_id
        project = getattr(self, "_current_edit_project", None)
        asset = getattr(project, "assets", {}).get(asset_id) if project is not None else None
        path = str(getattr(asset, "path", "") or "")
        if not path or not os.path.isfile(path):
            self._show_speech_to_text_message("找不到该音频素材文件", warning=True)
            return
        service = getattr(self, "_analysis_service", None)
        if service is None:
            self._show_speech_to_text_message(
                "媒体分析服务尚未初始化，请先打开或重新打开编辑页面", warning=True
            )
            return

        try:
            from src.features.edit.ai_editing.analysis_models import AssetFingerprint

            stat = os.stat(path)
            fingerprint = AssetFingerprint(size=int(stat.st_size), mtime_ns=int(stat.st_mtime_ns))
            fps_obj = getattr(timeline, "fps", 24.0)
            fps = (
                f"{fps_obj.num}/{fps_obj.den}"
                if hasattr(fps_obj, "num") and hasattr(fps_obj, "den")
                else f"{float(fps_obj):.6f}"
            )
        except (OSError, TypeError, ValueError) as exc:
            self._show_speech_to_text_message(f"无法读取音频信息：{exc}", warning=True)
            return

        try:
            from src.features.edit.ui.speech_to_text_dialog import SpeechToTextSettingsDialog

            dialog_parent = timeline_view.window() if timeline_view is not None else None
            settings_dialog = SpeechToTextSettingsDialog(dialog_parent)
            if settings_dialog.exec() != settings_dialog.DialogCode.Accepted:
                self._show_speech_to_text_message("已取消语音转文字")
                return
            transcript_options = settings_dialog.settings()
        except Exception as exc:
            # Keep the action usable for headless hosts and lightweight test
            # shells that do not expose a Qt window.
            logger.warning("Unable to open speech-to-text settings dialog: %s", exc)
            transcript_options = {
                "model_size": "auto",
                "background_music": "keep",
                "language": "auto",
                "timestamps": "auto",
                "speakers": True,
                "max_new_tokens": 8192,
            }

        router = getattr(self, "_transcript_router", None)
        remote_profile = getattr(router, "active_profile", None)
        if getattr(getattr(remote_profile, "data_policy", None), "execution", "local") == "cloud":
            if not bool(getattr(remote_profile, "allows_media_upload", False)):
                self._show_speech_to_text_message(
                    "当前 ASR Profile 未允许素材上传，请先在 Preferences → AI editing → 语音模型中开启上传策略",
                    warning=True,
                )
                return
            try:
                consent = QMessageBox.question(
                    self,
                    "确认云端语音识别",
                    f"将当前音频发送到“{getattr(remote_profile, 'display_name', '第三方 ASR')}”进行识别。\n"
                    "请确认已了解上传范围、服务商保留策略及可能产生的费用。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
            except Exception:
                consent = QMessageBox.StandardButton.No
            if consent != QMessageBox.StandardButton.Yes:
                self._show_speech_to_text_message("已取消云端语音识别")
                return
        validator = getattr(router, "validate_options", None)
        if callable(validator):
            validation_error = validator(transcript_options)
            if validation_error:
                self._show_speech_to_text_message(validation_error, warning=True)
                return

        worker = _SpeechToTextWorker(
            service, asset_id, fingerprint, fps,
            options=transcript_options,
            parent=self,
        )
        self._speech_to_text_worker = worker
        worker.completed.connect(
            lambda analysis, cid=clip_id, tid=track_id, aid=asset_id:
            self._on_speech_to_text_completed(cid, tid, aid, analysis)
        )
        worker.progress.connect(self._on_speech_to_text_progress)
        worker.failed.connect(self._on_speech_to_text_failed)
        worker.finished.connect(self._on_speech_to_text_finished)
        self._show_speech_to_text_message("正在转录音频…")
        self._on_speech_to_text_progress(0.0, "正在启动语音转文字")
        worker.start()

    def _show_speech_to_text_message(self, message: str, *, warning: bool = False) -> None:
        logger.warning(message) if warning else logger.info(message)
        status_bar = getattr(self, "statusBar", None)
        if callable(status_bar):
            try:
                status_bar().showMessage(message, 8000)
            except Exception:
                pass
        if warning:
            try:
                QMessageBox.warning(self, "语音转文字", message)
            except Exception:
                pass

    def _on_speech_to_text_failed(self, message: str) -> None:
        self._show_speech_to_text_message(f"语音转文字失败：{message}", warning=True)

    def _on_speech_to_text_progress(
        self, fraction: float | None, message: str,
    ) -> None:
        """把转写阶段状态同步到底部状态栏和全局进度条。"""
        progress_bar = getattr(self, "progress_bar", None)
        if progress_bar is not None:
            if fraction is None:
                # 模型加载/推理/ffmpeg 没有可靠的细粒度百分比，显示动画进度，
                # 让用户明确知道后台任务仍在运行。
                progress_bar.setRange(0, 0)
            else:
                progress_bar.setRange(0, 100)
                progress_bar.setValue(max(0, min(100, round(float(fraction) * 100))))
            progress_bar.show()

        status_bar = getattr(self, "status_bar", None)
        if status_bar is None:
            status_bar_getter = getattr(self, "statusBar", None)
            status_bar = status_bar_getter() if callable(status_bar_getter) else None
        if status_bar is not None:
            status_bar.showMessage(f"语音转文字：{message}", 0)

    def _on_speech_to_text_finished(self) -> None:
        """任务线程退出后收起进度条，但保留最后的结果/错误消息。"""
        progress_bar = getattr(self, "progress_bar", None)
        if progress_bar is not None:
            progress_bar.hide()
            progress_bar.setRange(0, 100)
        self._speech_to_text_worker = None

    def _on_speech_to_text_completed(
        self, clip_id: str, track_id: str, asset_id: str, analysis: Any
    ) -> bool:
        """Create a deterministic subtitle track and one undoable macro.

        The analysis worker and TimelineView can finish on different event-loop
        turns. Re-sync a view that has not loaded the active sequence yet, and
        treat track creation plus cue insertion as a transaction so a failed
        cue batch never leaves an empty generated track behind.
        """
        self._on_speech_to_text_progress(0.96, "正在写入字幕轨道")
        timeline_view = self._get_speech_timeline_view()
        timeline = getattr(timeline_view, "_timeline", None)
        if timeline is None:
            timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
            if timeline is not None and timeline_view is not None:
                loader = getattr(timeline_view, "load_timeline", None)
                if callable(loader):
                    loader(timeline)
                timeline = getattr(timeline_view, "_timeline", timeline)
        clip = timeline.get_clip_by_id(clip_id) if timeline is not None else None
        track = timeline.get_track_by_id(track_id) if timeline is not None else None
        if timeline_view is None or clip is None or track is None or track.track_type != "audio":
            self._show_speech_to_text_message("音频片段已被移除，未生成字幕", warning=True)
            return False

        from src.features.edit.ai_editing.subtitle_placement import build_subtitle_placements

        placements = build_subtitle_placements(analysis.transcript, clip)
        if not placements:
            self._show_speech_to_text_message("没有检测到可生成的字幕内容", warning=True)
            return False

        from src.features.edit.core.commands import (
            AddSubtitleClipCommand,
            AddTrackCommand,
            CaptionEditMode,
            MacroCommand,
        )

        track_name = f"AI 字幕 ({asset_id[:8]})"
        subtitle_track = next(
            (candidate for candidate in timeline.subtitle_tracks if candidate.name == track_name),
            None,
        )
        if subtitle_track is not None and subtitle_track.is_locked:
            self._show_speech_to_text_message("AI 字幕轨道已锁定，未写入字幕", warning=True)
            return False
        if subtitle_track is not None:
            # Rerunning the same clip is idempotent.  Do not create overlapping
            # cues or silently overwrite the user's edits on this generated track.
            if any(
                cue.timeline_start < clip.timeline_end
                and cue.timeline_end > clip.timeline_start
                for cue in subtitle_track.subtitle_clips
            ):
                self._show_speech_to_text_message("该音频片段已经生成过字幕")
                return False
        created_track = False
        macro_committed = False
        phase = "准备字幕轨道"
        try:
            if subtitle_track is None:
                add_track = AddTrackCommand.create(
                    "subtitle", track_name, len(timeline.subtitle_tracks)
                )
                timeline_view.execute_command(add_track)
                created_track = True
                timeline = getattr(timeline_view, "_timeline", None)
                if timeline is None:
                    raise RuntimeError("时间线视图没有接收到新时间线")
                subtitle_track = next(
                    (candidate for candidate in timeline.subtitle_tracks
                     if candidate.name == track_name),
                    None,
                )
                if subtitle_track is None:
                    raise RuntimeError("字幕轨道创建命令未生效")

            phase = "准备字幕片段"
            commands = [
                AddSubtitleClipCommand.create(
                    subtitle_track.track_id,
                    placement.start_frame,
                    placement.duration_frames,
                    placement.text,
                    mode=CaptionEditMode.OVERWRITE,
                    word_timings=placement.word_timings,
                )
                for placement in placements
            ]
            phase = f"写入第 1-{len(commands)} 条字幕"
            timeline_view.execute_command(MacroCommand.create("生成 AI 字幕", commands))
            macro_committed = True

            # execute_command intentionally has a void UI API. Verify its
            # observable result so a stale/provisional TimelineView cannot
            # report success while the model still contains no cues.
            committed_timeline = getattr(timeline_view, "_timeline", None)
            committed_track = (
                committed_timeline.get_track_by_id(subtitle_track.track_id)
                if committed_timeline is not None else None
            )
            actual_count = (
                len(committed_track.subtitle_clips)
                if committed_track is not None else 0
            )
            if actual_count < len(commands):
                raise RuntimeError(
                    f"字幕命令执行后仅找到 {actual_count}/{len(commands)} 条"
                )
        except Exception as exc:
            # MacroCommand is internally atomic. If validation failed after a
            # successful macro, undo it first; then remove a newly-created
            # empty/broken track. Existing user tracks are never touched here.
            if macro_committed:
                try:
                    timeline_view.undo()
                except Exception:
                    logger.exception("Failed to roll back subtitle macro")
            if created_track:
                try:
                    timeline_view.undo()
                except Exception:
                    logger.exception("Failed to roll back subtitle track")
            logger.exception("Failed to create subtitle clips")
            self._show_speech_to_text_message(
                f"字幕写入失败（{phase}）：{exc}", warning=True
            )
            return False
        self._show_speech_to_text_message(f"已生成 {len(commands)} 条字幕")
        return True

    # ------------------------------------------------------------------
    # Subtitle track → generated voice-over
    # ------------------------------------------------------------------

    def _on_subtitle_to_speech_requested(self, track_id: str) -> None:
        """Generate one timed WAV from every cue on a subtitle track."""
        worker = getattr(self, "_subtitle_to_speech_worker", None)
        if worker is not None and worker.isRunning():
            self._show_speech_to_text_message("已有字幕转语音任务正在运行", warning=True)
            return
        timeline_view = self._get_speech_timeline_view()
        timeline = getattr(timeline_view, "_timeline", None)
        track = timeline.get_track_by_id(track_id) if timeline is not None else None
        if timeline_view is None or timeline is None or track is None or track.track_type != "subtitle":
            self._show_speech_to_text_message("请选择有效的字幕轨道", warning=True)
            return
        if track.is_locked:
            self._show_speech_to_text_message("字幕轨道已锁定，无法生成配音", warning=True)
            return
        cues = tuple(cue for cue in track.subtitle_clips if cue.text and cue.text.strip())
        if not cues:
            self._show_speech_to_text_message("字幕轨道没有可合成的文本", warning=True)
            return
        provider = getattr(self, "_tts_provider", None)
        if provider is None:
            self._show_speech_to_text_message("TTS provider 尚未初始化，请重新打开编辑页面", warning=True)
            return
        try:
            fps = float(timeline.fps)
        except (TypeError, ValueError):
            fps = 24.0

        # 弹出 TTS 设置对话框采集参数（音色/语言/instruct/克隆/高级）
        tts_settings = self._collect_tts_settings(provider)
        if tts_settings is None:
            return  # 用户取消

        worker = _SubtitleToSpeechWorker(provider, cues, fps, settings=tts_settings, parent=self)
        self._subtitle_to_speech_worker = worker
        worker.completed.connect(
            lambda result, tid=track_id: self._on_subtitle_to_speech_completed(tid, result)
        )
        worker.failed.connect(
            lambda message: self._show_speech_to_text_message(
                f"字幕转语音失败：{message}", warning=True
            )
        )
        worker.finished.connect(lambda: setattr(self, "_subtitle_to_speech_worker", None))
        active_profile = getattr(provider, "active_profile", None)
        provider_label = getattr(active_profile, "display_name", "当前 TTS Profile")
        self._show_speech_to_text_message(f"正在用 {provider_label} 生成配音…")
        worker.start()

    def _collect_tts_settings(self, provider: Any) -> Any:
        """弹出 TTS 设置对话框采集合成参数；用户取消返回 None。"""
        from src.features.edit.ai_editing.tts_settings import DEFAULT_TTS_SETTINGS
        from src.features.edit.ui.tts_settings_dialog import TtsSettingsDialog

        # 检测模型是否就绪（用于对话框显示警告）
        model_ready = True
        missing_hint = ""
        try:
            # 先用默认 settings 探测（custom_voice 模式）
            model_ready = provider.is_model_ready(DEFAULT_TTS_SETTINGS)
        except Exception:
            pass
        if not model_ready:
            missing_hint = (
                "⚠️ CustomVoice 模型未安装。请先下载模型：\n"
                "  modelscope download Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice "
                "--local_dir models/ai_editing/qwen3_tts_customvoice"
            )

        dialog = TtsSettingsDialog(
            parent=self,
            initial=DEFAULT_TTS_SETTINGS,
            model_ready=model_ready,
            missing_hint=missing_hint,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return None
        return dialog.get_settings()

    def _on_subtitle_to_speech_completed(self, subtitle_track_id: str, result: Any) -> None:
        """Import synthesized WAV and add it as an undoable audio clip."""
        timeline_view = self._get_speech_timeline_view()
        timeline = getattr(timeline_view, "_timeline", None)
        if timeline_view is None or timeline is None:
            self._show_speech_to_text_message("时间线已关闭，生成的配音未写入", warning=True)
            return
        subtitle_track = timeline.get_track_by_id(subtitle_track_id)
        if subtitle_track is None or subtitle_track.track_type != "subtitle":
            self._show_speech_to_text_message("字幕轨道已不存在，生成的配音未写入", warning=True)
            return
        try:
            from src.features.edit.core.project import _probe_audio_info
            project = getattr(self, "_current_edit_project", None)
            if project is None:
                raise RuntimeError("当前没有打开的工程")
            audio_info = _probe_audio_info(result.path) or {
                "channels": 1, "sample_rate": int(result.sample_rate),
            }
            new_project = project.import_asset(
                path=result.path,
                media_type="audio",
                metadata={
                    "audio_info": audio_info,
                    "duration": float(result.duration_seconds),
                    "generated_by": result.model,
                    "source_subtitle_track_id": subtitle_track_id,
                },
                display_name=f"AI 配音 - {subtitle_track.name}.wav",
            )
            asset = new_project.get_asset_by_path(result.path)
            if asset is None:
                raise RuntimeError("生成的配音未能导入媒体池")
            if new_project is not project:
                self._apply_project_change(new_project)

            fps = float(timeline.fps)
            start_frame = int(result.first_frame)
            duration_frames = max(1, int(round(float(result.duration_seconds) * fps)))
            from src.features.edit.core.commands import AddClipCommand, AddTrackCommand
            from src.features.edit.core.timeline_v2 import ClipInstance

            # Prefer the current target audio track; if it overlaps, create a
            # dedicated AI VO track so the operation remains deterministic.
            audio_track = next(
                (candidate for candidate in timeline.audio_tracks
                 if not any(start_frame < c.timeline_end and
                            start_frame + duration_frames > c.timeline_start
                            for c in candidate.clips)),
                None,
            )
            if audio_track is None:
                add_track = AddTrackCommand.create("audio", "AI 配音")
                timeline_view.execute_command(add_track)
                timeline = getattr(timeline_view, "_timeline", timeline)
                audio_track = timeline.get_track_by_id(add_track._created_track_id or "")
            if audio_track is None:
                raise RuntimeError("无法创建音频轨道")
            clip = ClipInstance(
                clip_id=str(uuid.uuid4()),
                asset_id=asset.asset_id,
                track_id=audio_track.track_id,
                timeline_start=start_frame,
                timeline_duration=duration_frames,
                source_start=0,
                source_duration=duration_frames,
                speed=1.0,
            )
            timeline_view.execute_command(
                AddClipCommand.create(audio_track.track_id, clip)
            )

            # 按 TTS 实际音频时长自适应调整字幕片段的 timeline_duration，
            # 使字幕与语音精确对齐（加 7 帧尾部停顿让语句间有自然间隙）。
            cue_durations = getattr(result, "cue_durations", ())
            if cue_durations:
                from src.features.edit.core.commands import (
                    UpdateSubtitleClipCommand, MacroCommand,
                )
                sub_clips = [c for c in subtitle_track.subtitle_clips
                             if c.text and c.text.strip()]
                pause_frames = max(1, int(fps * 0.3))  # 约 0.3 秒停顿
                updates: list[Any] = []
                for i, sub_clip in enumerate(sub_clips):
                    if i >= len(cue_durations):
                        break
                    audio_frames = int(cue_durations[i] * fps) + pause_frames
                    # 不超过下一个字幕片段的起始位置（防重叠）
                    if i + 1 < len(sub_clips):
                        next_start = sub_clips[i + 1].timeline_start
                        max_dur = next_start - sub_clip.timeline_start
                        audio_frames = min(audio_frames, max(max_dur, 1))
                    if audio_frames != sub_clip.timeline_duration:
                        updates.append(UpdateSubtitleClipCommand(
                            command_id=str(uuid.uuid4()),
                            label="同步字幕时长",
                            clip_id=sub_clip.clip_id,
                            updates={"timeline_duration": audio_frames},
                        ))
                if updates:
                    timeline_view.execute_command(
                        MacroCommand.create("同步字幕时长到配音", updates)
                    )

            self._show_speech_to_text_message(
                f"已将 {len(subtitle_track.subtitle_clips)} 条字幕生成配音并放入音轨"
            )
        except Exception as exc:
            logger.exception("Failed to insert synthesized subtitle audio")
            self._show_speech_to_text_message(f"配音写入失败：{exc}", warning=True)

    # ------------------------------------------------------------------
    # Clip → Color page integration (T7: right-click only, no auto-jump)
    # ------------------------------------------------------------------

    def _on_open_in_color_requested(self, clip_id: str, track_id: str, stack_id: str) -> None:
        """
        Handle "Open in Color" right-click action from Edit timeline clip (T7).

        Resolves the active timeline ID and delegates to
        open_timeline_clip_in_color for precise frame mapping.

        Args:
            clip_id: Clip ID that was right-clicked
            track_id: Track ID containing the clip
            stack_id: Color stack ID (may be empty)
        """
        # Get the active timeline ID from the current edit project
        timeline_id = None
        if self._current_edit_project and hasattr(self._current_edit_project, 'active_timeline_id'):
            timeline_id = self._current_edit_project.active_timeline_id

        if not timeline_id:
            logger.warning("_on_open_in_color_requested: no active timeline")
            return

        logger.info(f"[T7] Open in Color: clip={clip_id} timeline={timeline_id}")
        self.open_timeline_clip_in_color(timeline_id, clip_id)

    def _ensure_clip_has_color_stack(self, clip_id: str, asset_id: str) -> str | None:
        """
        确保 clip 有关联的 ColorStack，如果没有则自动创建 (P9.1)。

        Args:
            clip_id: Clip ID
            asset_id: Asset ID

        Returns:
            Color stack ID, or None if creation failed
        """
        if not self._current_edit_project:
            logger.warning("No EditProject for color stack creation")
            return None

        if not self.edit_page or not self.edit_page._current_timeline:
            logger.warning("No timeline for color stack creation")
            return None

        timeline = self.edit_page._current_timeline

        # Find clip
        clip = None
        track_id = None
        for track in timeline.all_tracks:
            c = track.get_clip_by_id(clip_id)
            if c:
                clip = c
                track_id = track.track_id
                break

        if clip is None:
            logger.warning(f"Clip not found for color stack creation: {clip_id}")
            return None

        # A scene split can preserve a clip-local stack reference on the new
        # right-hand segment.  Once ownership metadata is present, that
        # reference belongs to the original clip and must be replaced with a
        # fresh local stack before this segment is edited.  Explicit shared
        # grades are the exception: they intentionally remain shared.
        if clip.color_stack_id:
            stack = self._current_edit_project.color_stacks.get(clip.color_stack_id)
            stack_scope = getattr(stack, "scope", "clip_local") if stack else "clip_local"
            stack_owner = getattr(stack, "clip_id", None) if stack else None
            if (
                getattr(clip, "color_shared", False)
                or stack_scope != "clip_local"
                or not stack_owner
                or stack_owner == clip_id
            ):
                return clip.color_stack_id
            logger.info(
                "Replacing inherited local color stack %s for split clip %s "
                "(owner=%s)",
                clip.color_stack_id,
                clip_id,
                stack_owner,
            )

        # Always create a clip-local color stack so each clip grades
        # independently.  Shared grading requires explicit user action.
        #
        # AUD-2708: creation + ownership stamping used to be two non-atomic
        # steps — a command-stack ``CreateColorStackCommand`` followed by a
        # bare ``replace`` on the project that bypassed every undo stack —
        # and the clip binding was applied directly to the timeline.
        # Undoing the creation in the media pool left the clip referencing
        # a deleted stack, and redo re-created a *different* stack id with
        # the stamp lost.  ``_CreateStampedColorStackCommand`` commits the
        # create + stamp as ONE project-level undo unit with a redo-stable
        # stack id, and the binding below goes through the timeline command
        # stack (single entry, undoable like every other timeline edit).
        command = _CreateStampedColorStackCommand.create(
            asset_id=asset_id,
            name=f"Clip_{clip_id[:8]}",
            stamp_fields={
                "timeline_id": timeline.timeline_id,
                "clip_id": clip_id,
                "scope": "clip_local",
                "source_kind": "timeline_clip",
            },
            label=f"Create clip color stack ({clip_id[:8]})",
        )
        cmd_stack = self.media_pool_panel.get_command_stack()
        new_project = cmd_stack.execute(command, self._current_edit_project)
        self._current_edit_project = new_project

        stack_id = command.created_stack_id
        if not stack_id:
            logger.warning(f"Failed to create color stack for clip {clip_id}")
            return None
        logger.info(f"Created clip-local color stack {stack_id} for clip {clip_id}")

        # Bind the clip to the new stack.  AUD-2708: recorded on the
        # timeline command stack (TimelineView.execute_command), with a
        # direct-apply + view-sync fallback for hosts without a view —
        # previously this bypassed the stack entirely, so the binding had
        # no undo record anywhere.
        _cmds2 = get_command_classes()
        if _cmds2 is None:
            return stack_id
        UpdateClipColorStackCommand = _cmds2["UpdateClipColorStackCommand"]

        bind_command = UpdateClipColorStackCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            new_color_stack_id=stack_id,
            new_color_shared=False,
        )
        self._execute_timeline_edit_command(bind_command, timeline)

        # The renderer owns an immutable project snapshot. Publish the new
        # project stack before entering Color so Edit cannot continue
        # rendering a stale raw clip. (The new timeline itself is published
        # by the command path above — view path via the timeline_changed
        # signal chain, fallback via _sync_timeline_to_views.)
        if self.timeline_preview_renderer is not None:
            update_color_stacks = getattr(
                self.timeline_preview_renderer,
                "update_color_stacks",
                None,
            )
            if callable(update_color_stacks):
                update_color_stacks(self._current_edit_project)
            else:
                self.timeline_preview_renderer.set_project(self._current_edit_project)

        return stack_id

    def _on_open_asset_grade_in_color(self, asset_id: str) -> None:
        """E08: 媒体池图片右键 "Open Asset Grade in Color" 的宿主处理。

        此前 ``open_asset_grade_requested`` 全仓 0 接收者，菜单点击无任何
        反应。流程：校验图片素材 → 复用/创建 asset 级 ColorStack（经媒体
        池命令栈，可撤销）→ 切到 Color 页 → 加载该栈到调色面板。
        """
        project = getattr(self, "_current_edit_project", None)
        asset = project.get_asset_by_id(asset_id) if project is not None else None
        if asset is None:
            self._show_edit_status_message(f"调色失败：素材不存在 {asset_id}")
            return
        if getattr(asset, "media_type", "") != "image":
            self._show_edit_status_message("只有图片素材支持直接进入调色；视频请通过时间线片段进入")
            return

        stack = project.get_color_stack_for_asset(asset_id)
        if stack is None:
            command = _CreateStampedColorStackCommand.create(
                asset_id=asset_id,
                name=f"Asset_{asset_id[:8]}",
                stamp_fields={
                    "asset_id": asset_id,
                    "scope": "asset",
                    "source_kind": "media_pool_asset",
                },
                label=f"Create asset color stack ({asset_id[:8]})",
            )
            cmd_stack = self.media_pool_panel.get_command_stack()
            self._current_edit_project = cmd_stack.execute(
                command, self._current_edit_project
            )
            stack_id = command.created_stack_id
            if not stack_id:
                self._show_edit_status_message("创建调色栈失败")
                return
            # 渲染器持有工程快照，新栈要先发布（同 _ensure_clip_has_color_stack）
            renderer = getattr(self, "timeline_preview_renderer", None)
            if renderer is not None:
                update_stacks = getattr(renderer, "update_color_stacks", None)
                if callable(update_stacks):
                    update_stacks(self._current_edit_project)
                else:
                    renderer.set_project(self._current_edit_project)
        else:
            stack_id = stack.stack_id

        # 切到 Color 页并加载该栈（PageManagementMixin / PageController 提供）
        self.switch_to_page("Color")
        load = getattr(self, "_load_color_stack_to_grading_panel", None)
        if callable(load):
            load(stack_id)
        logger.info(
            "Opened asset grade in Color: asset=%s stack=%s", asset_id, stack_id
        )

    # ------------------------------------------------------------------
    # Grading / ColorStack helpers
    # ------------------------------------------------------------------

    def _flush_active_grading_stack_before_switch(self, target_stack_id: str | None) -> None:
        """Commit the visible grade before replacing the Color-page context.

        Grading controls deliberately coalesce drag updates on a short timer.
        A clip-card click can arrive before that timer fires, and loading the
        next stack then replaces the old control values.  Saving explicitly at
        this boundary makes a clip switch an atomic ``save current -> load
        target`` transaction rather than relying on timer timing.
        """
        if not target_stack_id or getattr(self, '_color_stack_hydration_in_progress', False):
            return

        panel = getattr(self, 'grading_panel', None)
        if panel is None:
            return

        active_stack_id = getattr(panel, 'current_stack_id', None)
        bound_stack_id = getattr(self, '_grading_save_stack_id', None)
        if not active_stack_id or active_stack_id == target_stack_id:
            return

        # Do not risk writing a panel into a stack when its callback belongs
        # to a different context. Normal activation keeps these IDs identical.
        if bound_stack_id and bound_stack_id != active_stack_id:
            logger.warning(
                'Skipped ColorStack flush for %s: panel callback is bound to %s',
                active_stack_id,
                bound_stack_id,
            )
            return

        trigger_save = getattr(panel, 'trigger_save', None)
        if callable(trigger_save):
            trigger_save()
            logger.debug(
                'Flushed ColorStack %s before switching to %s',
                active_stack_id,
                target_stack_id,
            )

    def _setup_grading_save_callback(self, stack_id: str) -> None:
        """
        设置 GradingPanel 的保存回调，实现双向绑定 (P9.1)。

        当 GradingPanel 参数改变时，自动保存到 ColorStack。

        Args:
            stack_id: Color stack ID to save to
        """
        if not hasattr(self, 'grading_panel') or not self.grading_panel:
            return

        # Set up callback
        self.grading_panel.set_save_callback(
            lambda params: self._on_grading_changed_for_color_stack(stack_id, params)
        )
        self._grading_save_stack_id = stack_id
        self._pending_grading_color_stack = None

        # Track live edits separately from persistence.  ``grading_changed`` is
        # emitted at up to 60 Hz while a wheel or slider is dragged, so saving a
        # project command and rebuilding the Edit preview for every tick makes
        # the Color render thread fall behind the pointer.  The Color engine
        # still receives the live signal through MainWindow; we commit one undo
        # entry and refresh the Edit preview when the gesture ends.
        # Disconnect only our own previous connections (not others' connections).
        if hasattr(self, '_grading_panel_changed_slot') and self._grading_panel_changed_slot:
            try:
                self.grading_panel.grading_changed.disconnect(self._grading_panel_changed_slot)
            except (RuntimeError, TypeError):
                pass
        if hasattr(self, '_grading_panel_interaction_ended_slot') and self._grading_panel_interaction_ended_slot:
            try:
                self.grading_panel.interaction_ended.disconnect(self._grading_panel_interaction_ended_slot)
            except (RuntimeError, TypeError):
                pass
        if hasattr(self, '_grading_panel_reset_slot') and self._grading_panel_reset_slot:
            try:
                self.grading_panel.reset_clicked.disconnect(self._grading_panel_reset_slot)
            except (RuntimeError, TypeError):
                pass

        # Create a bound method with the current stack_id so we can disconnect later
        def _make_slot(sid: str):
            def slot(lift, gamma, gain, exp, temp, tint, contrast, shadow, sat, vibrance, mid_det, hue, highlights=0.0) -> None:
                self._on_grading_panel_changed(sid)
            return slot

        self._grading_panel_changed_slot = _make_slot(stack_id)
        self.grading_panel.grading_changed.connect(self._grading_panel_changed_slot)
        self._grading_panel_interaction_ended_slot = (
            lambda sid=stack_id: self._commit_pending_grading_color_stack(sid)
        )
        self.grading_panel.interaction_ended.connect(
            self._grading_panel_interaction_ended_slot
        )
        self._grading_panel_reset_slot = (
            lambda sid=stack_id: self._commit_pending_grading_color_stack(sid)
        )
        self.grading_panel.reset_clicked.connect(self._grading_panel_reset_slot)

        logger.debug(f"Set up save callback for color stack {stack_id}")

    def _on_grading_panel_changed(self, stack_id: str) -> None:
        """
        GradingPanel 参数改变时的处理函数 (P9.1)。

        使用节流机制，通过 save_callback 触发保存。

        Args:
            stack_id: Color stack ID
        """
        if getattr(self, '_color_stack_hydration_in_progress', False):
            return

        panel = getattr(self, 'grading_panel', None)
        if panel is None:
            return

        active_stack_id = getattr(panel, 'current_stack_id', None)
        if active_stack_id and active_stack_id != stack_id:
            logger.debug(
                'Ignoring stale grading signal for %s while %s is active',
                stack_id,
                active_stack_id,
            )
            return

        if panel._save_callback:
            # Keep only the latest value for this gesture.  The regular Color
            # handler has already updated the active engine layer and submitted
            # a mergeable ``grading`` preview render.
            self._pending_grading_color_stack = (
                stack_id,
                panel.get_current_params(),
            )

    def _commit_pending_grading_color_stack(self, stack_id: str) -> None:
        """Persist the latest live grading gesture once it has completed."""
        pending = getattr(self, '_pending_grading_color_stack', None)
        if pending is None:
            return

        pending_stack_id, grading_params = pending
        if pending_stack_id != stack_id:
            return

        panel = getattr(self, 'grading_panel', None)
        if panel is None or getattr(panel, 'current_stack_id', None) not in (None, stack_id):
            return

        self._pending_grading_color_stack = None
        self._on_grading_changed_for_color_stack(stack_id, grading_params)

    def _on_grading_changed_for_color_stack(
        self,
        stack_id: str,
        grading_params: dict[str, Any]
    ) -> None:
        """
        保存 GradingPanel 修改到 ColorStack (P9.1).

        使用 UpdateColorStackCommand 通过 command stack 执行，
        支持 undo/redo.

        Also collects curves, HSL, mixer, soft_clip, and LUT params
        from engine layers so the full color stack state is persisted.

        Args:
            stack_id: Color stack ID to update
            grading_params: New grading parameters
        """
        if not self._current_edit_project:
            logger.warning("No EditProject for color stack update")
            return

        if getattr(self, '_color_stack_hydration_in_progress', False):
            return

        _cmds = get_command_classes()
        if _cmds is None:
            return
        UpdateColorStackCommand = _cmds["UpdateColorStackCommand"]

        # Collect additional params from engine layers for full-stack save
        curves_data = None
        hsl_curve_data = None
        mixer_params = None
        soft_clip_params = None
        lut_path = None
        lut_intensity = None
        # P0-COLOR-02：完整 Layer 快照（权威状态）随每次栈提交刷新，
        # 保证蒙版/限定器/变形器/效果/插件等高级状态进入工程数据
        layers_snapshot = None
        active_layer_index = None
        raw_params = None
        # AUD-2704: trackings/video_denoise were previously assigned only
        # inside the ``if self.engine.layers:`` block below, so committing a
        # grade with no active layer (source unloaded/cleared) raised
        # UnboundLocalError here and the whole stack commit was lost.
        # Initialize them alongside the other snapshot fields.
        trackings = None
        video_denoise = None
        video_stabilization = (self.engine.get_video_stabilization_state()
            if callable(getattr(self.engine, "get_video_stabilization_state", None)) else None)

        if self.engine.layers:
            layer = self.engine.layers[0]
            if getattr(layer, 'curves_data', None):
                curves_data = dict(layer.curves_data)
            if getattr(layer, 'hsl_curve_data', None):
                hsl_curve_data = dict(layer.hsl_curve_data)
            if getattr(layer, 'mixer_params', None):
                mixer_params = dict(layer.mixer_params)
            if getattr(layer, 'soft_clip_params', None):
                soft_clip_params = dict(layer.soft_clip_params)
            lut_path = getattr(layer, 'active_lut_path', None)
            lut_intensity = getattr(layer, 'lut_intensity', None)
            # AUD-24202 收尾：timeline clip 栈提交也走 inline 通道——
            # Layer.to_dict(None) 不携带 BITMAP 蒙版 bitmap_data/AI matte，
            # 普通调色提交会把权威快照里的位图载荷冲掉，切走再切回蒙版
            # 静默变空、存盘后永久丢失。
            from src.features.color.adapters.color_stack_hydrator import (
                attach_inline_bitmaps,
            )
            layers_snapshot = []
            for l in self.engine.layers:
                d = l.to_dict(None)
                attach_inline_bitmaps(l, d)
                layers_snapshot.append(d)
            idx = getattr(self.engine, 'active_layer_index', 0) or 0
            active_layer_index = (
                idx if 0 <= idx < len(layers_snapshot) else 0
            )
            raw_params = dict(
                getattr(self.engine, 'current_raw_params', None) or {}
            ) or None
            # P0-COLOR-02 复查：蒙版跟踪数据必须随栈提交持久化。此前
            # 只有 standalone 单素材保存写 trackings，timeline clip /
            # 共享 asset 场景保存会丢跟踪结果。
            tracking_store = getattr(self.engine, 'tracking_store', None)
            trackings = tracking_store.to_dict() if tracking_store else None
            # P1-COLOR-13: per-clip 视频降噪参数随栈提交持久化
            # （None = 从未开启，不写字段；开启过即写入含关闭状态）
            vd_params = getattr(self.engine, 'video_denoise_params', None)
            video_denoise = (
                vd_params.to_dict() if vd_params is not None else None)

        command = UpdateColorStackCommand.create(
            stack_id=stack_id,
            grading_params=grading_params,
            curves_data=curves_data,
            hsl_curve_data=hsl_curve_data,
            mixer_params=mixer_params,
            soft_clip_params=soft_clip_params,
            lut_path=lut_path,
            lut_intensity=lut_intensity,
            layers=layers_snapshot,
            active_layer_index=active_layer_index,
            raw_params=raw_params,
            trackings=trackings,
            video_denoise=video_denoise,
            video_stabilization=video_stabilization,
        )
        cmd_stack = self.media_pool_panel.get_command_stack()
        new_project = cmd_stack.execute(command, self._current_edit_project)
        self._current_edit_project = new_project
        self.edit_project_dirty = True

        # Update engine layers
        if self.engine.layers:
            for layer in self.engine.layers:
                layer.grading_params = dict(grading_params)

        # The live preview has already rendered through the mergeable grading
        # lane.  The final ColorStack commit must not enqueue a full FIFO frame
        # that delays the next interaction; render_pipeline still marks the
        # ColorOutput revision dirty for Composite consumers.
        self.engine.render_pipeline(render_reason='grading')

        # The Edit preview renderer and its worker intentionally hold their
        # own project snapshot.  Keep this update lightweight: a grading drag
        # must invalidate displayed frames, but must not reopen decoders or
        # discard the timeline's warmed proxy/ring-buffer state.
        if self.timeline_preview_renderer is not None:
            update_color_stacks = getattr(
                self.timeline_preview_renderer,
                "update_color_stacks",
                None,
            )
            if callable(update_color_stacks):
                update_color_stacks(new_project)
            else:
                self.timeline_preview_renderer.set_project(new_project)
            self._request_current_timeline_preview_frame()

        logger.debug(f"Saved grading params to color stack {stack_id}")

        # Keep the Color page clip strip's visual grade marker in sync with
        # the newly committed immutable project snapshot — at idle, not now.
        # A grade commit never changes the clip list, and ``_rebuild_cards``
        # destroys and recreates every card (each thumbnail only re-arrives
        # asynchronously), which is the black flash on every clip switch and
        # cross-clip playback advance: both flush the active stack and land
        # here.  The graded look still reaches the active card immediately
        # through the preview-sync capture scheduled below.
        self._schedule_idle_color_timeline_clip_strip_refresh()
        strip = getattr(self, "color_timeline_clip_strip", None)
        # A strip click selects the incoming clip before the outgoing stack is
        # flushed. The loaded playback context still identifies that stack's
        # owner; never capture its frame into the newly selected card.
        context = getattr(self, "_color_timeline_clip_playback_context", None) or {}
        target_clip_id = getattr(context.get("clip"), "clip_id", None)
        if not target_clip_id:
            target_clip_id = getattr(strip, "_active_clip_id", None)
        self._schedule_color_timeline_clip_preview_sync(target_clip_id)

        # Phase 7: Color → Composite revision sync.
        # When a color stack is updated, check whether any composite graph's
        # TimelineClipInput nodes reference a clip that uses this color_stack_id.
        # If so, the composite's cached input texture is stale and needs refresh.
        self._notify_composite_inputs_stale(stack_id)

    def _schedule_color_timeline_clip_preview_sync(self, clip_id: str | None) -> None:
        """Capture one finished Color frame and feed it to the active clip card.

        The capture reuses the already-painted Color viewport. It does not
        decode media or submit another render, and a generation token prevents
        a late frame from replacing a newer clip's preview.
        """
        if not clip_id:
            return
        strip = getattr(self, "color_timeline_clip_strip", None)
        canvas = getattr(self, "canvas", None)
        engine = getattr(self, "engine", None)
        frame_ready = getattr(engine, "frame_ready", None)
        if strip is None or canvas is None or frame_ready is None:
            return

        project = getattr(self, "_current_edit_project", None)
        has_grade = getattr(strip, "stack_has_grade", None)
        capture_needed = True
        if project is not None and callable(has_grade):
            clip = self._find_timeline_clip(clip_id)
            if clip is not None:
                stack_id = getattr(clip, "color_stack_id", None)
                stack = project.color_stacks.get(stack_id)
                if not has_grade(stack, clip_id, bool(getattr(clip, "color_shared", False))):
                    # Neutral clips use their source thumbnail. Capturing a
                    # switch-time viewport here could cache the departing shot.
                    strip.invalidate_graded_preview_stacks({stack_id} if stack_id else set())
                    capture_needed = False

        previous = getattr(self, "_timeline_clip_preview_subscription", None)
        if previous is not None:
            try:
                previous[0].frame_ready.disconnect(previous[1])
            except (TypeError, RuntimeError):
                pass
            self._timeline_clip_preview_subscription = None

        token = int(getattr(self, "_timeline_clip_preview_token", 0) or 0) + 1
        self._timeline_clip_preview_token = token
        if not capture_needed:
            return
        captured = False

        def _capture() -> None:
            nonlocal captured
            if captured or token != getattr(self, "_timeline_clip_preview_token", None):
                return
            renderer = getattr(engine, "renderer", None)
            if bool(getattr(renderer, "video_texture_pending", False)):
                return
            captured = True
            try:
                frame_ready.disconnect(_on_frame_ready)
            except (TypeError, RuntimeError):
                pass
            self._timeline_clip_preview_subscription = None

            if getattr(self, "current_page", None) != "Color":
                return
            if getattr(strip, "_active_clip_id", None) != clip_id:
                return
            context = getattr(self, "_color_timeline_clip_playback_context", None) or {}
            loaded_clip = context.get("clip")
            if loaded_clip is not None and getattr(loaded_clip, "clip_id", None) != clip_id:
                return
            viewport = getattr(canvas, "gl_viewport", None)
            if viewport is None:
                viewport = canvas.viewport()
            grab = getattr(viewport, "grab", None)
            if not callable(grab):
                return
            try:
                pixmap = grab()
            except (RuntimeError, TypeError):
                return
            if pixmap is None or pixmap.isNull():
                return
            # The GL viewport is often wider/taller than the rendered source.
            # Crop its fit-to-window matte before caching the card preview;
            # otherwise the card receives the viewer's black bars as pixels.
            pixmap = _crop_pixmap_to_aspect(
                pixmap,
                _preview_content_aspect(engine),
            )
            update_preview = getattr(strip, "set_graded_preview", None)
            if callable(update_preview):
                update_preview(str(clip_id), pixmap)

        def _on_frame_ready(_texture_id: int) -> None:
            if token == getattr(self, "_timeline_clip_preview_token", None):
                # Let the completed frame reach the viewport before grabbing it.
                QTimer.singleShot(40, _capture)

        try:
            frame_ready.connect(_on_frame_ready)
            self._timeline_clip_preview_subscription = (engine, _on_frame_ready)
        except (TypeError, RuntimeError):
            return

        # Capture only a completed frame. A time-based fallback could grab the
        # departing shot while the new clip's decode/render is still queued.

    def _schedule_idle_color_timeline_clip_strip_refresh(self) -> None:
        """Rebuild the Color clip strip at idle instead of on every commit.

        ``_rebuild_cards`` destroys and recreates every card, so calling it
        from a grading commit makes the whole strip flash its dark
        placeholder until the asynchronous thumbnails return — once per clip
        switch and once per cross-clip playback advance (both flush the
        active stack, which lands in the commit path).  A commit does not
        change the clip list, so the rebuild can wait: never while playback
        is running, and debounced while paused so a burst of commits
        collapses into one refresh.
        """
        timer = getattr(self, "_idle_clip_strip_refresh_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(_IDLE_CLIP_STRIP_REFRESH_MS)
            timer.timeout.connect(self._on_idle_clip_strip_refresh_timeout)
            self._idle_clip_strip_refresh_timer = timer
        timer.start()

    def _on_idle_clip_strip_refresh_timeout(self) -> None:
        """Run the deferred clip-strip refresh once playback has stopped."""
        video_controller = getattr(self, "video_controller", None)
        if getattr(video_controller, "is_playing", False):
            # 播放中重建会让整条缩略图闪黑；稍后重试，直到播放停止。
            timer = getattr(self, "_idle_clip_strip_refresh_timer", None)
            if timer is not None:
                timer.start()
            return
        refresh = getattr(self, "refresh_color_timeline_clip_strip", None)
        if callable(refresh):
            refresh()

    def _notify_composite_inputs_stale(self, stack_id: str) -> None:
        """
        Phase 5: Notify composite graphs that their input texture may be stale
        after a ColorStack change.

        Scans project compositions for ToposColor nodes using the changed
        color_stack_id, triggers cache invalidation, and requests re-render.

        Args:
            stack_id: The color_stack_id that was just updated
        """
        if not self._current_edit_project:
            return

        project = self._current_edit_project
        compositions = getattr(project, 'compositions', {})
        if not compositions:
            return

        affected_composites = []

        # Find compositions that have ToposColor nodes using this stack_id
        for composite_id, comp_data in compositions.items():
            if hasattr(comp_data, 'nodes'):
                nodes = comp_data.nodes
            elif isinstance(comp_data, dict):
                nodes = comp_data.get('nodes', [])
            else:
                continue

            for node in nodes:
                node_type = node.get('type') or node.get('node_type', '')
                if node_type == 'topos_color':
                    params = node.get('parameters', {})
                    if params.get('color_stack_id') == stack_id:
                        affected_composites.append(composite_id)
                        logger.debug(
                            f"[Phase5] ToposColor using stale stack: "
                            f"composite={composite_id}, stack_id={stack_id}"
                        )
                        break

        # Trigger cache invalidation for affected compositions
        if affected_composites and hasattr(self, 'composite_page'):
            if self.composite_page and hasattr(self.composite_page, '_on_color_dirty'):
                self.composite_page._on_color_dirty()
                logger.info(
                    f"[Phase5] Invalidated {len(affected_composites)} compositions "
                    f"for color_stack {stack_id}"
                )

    # ------------------------------------------------------------------
    # Edit Properties Panel Handlers
    # ------------------------------------------------------------------

    def _on_edit_clip_param_changed(
        self, clip_id: str, param_type: str, param_path: str, value: object
    ) -> None:
        """
        Handle clip parameter change from EditPropertiesPanel.

        This handler creates and executes an UpdateClipParamCommand to update
        the clip's parameters (transform/crop/speed/opacity).

        Args:
            clip_id: Clip ID being modified
            param_type: Parameter type ('transform', 'crop', 'speed', 'opacity')
            param_path: Specific parameter path (e.g., 'translate_x', 'scale_x')
            value: New value (TransformParams, CropParams, SpeedParams, or float)
        """
        logger.debug(f"_on_edit_clip_param_changed: {clip_id} {param_type}.{param_path}")

        # Get current timeline from edit_page
        if self.edit_page is None or not self.edit_page._current_timeline:
            logger.warning("No current timeline for clip param update")
            return

        timeline = self.edit_page._current_timeline

        # Find the clip to get its track_id and old value
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        track_id = clip.track_id

        # A1: 混合模式切换 → SetClipBlendModeCommand（多选时批量作用于所选片段）
        if param_type == "blend_mode":
            from src.features.edit.core.commands import SetClipBlendModeCommand

            target_ids = [clip_id]
            panel = getattr(self, 'timeline_panel', None)
            view_getter = getattr(panel, 'get_timeline_view', None)
            view = view_getter() if callable(view_getter) else None
            sel_ids = getattr(getattr(view, '_selection', None), 'selected_clip_ids', None)
            if sel_ids and clip_id in sel_ids and len(sel_ids) > 1:
                target_ids = list(sel_ids)
            command = SetClipBlendModeCommand.create(target_ids, value)
            # 统一走 TimelineView.execute_command（命令栈/增量更新/自动
            # 保存的唯一入口）。EditPage 本身没有 _command_stack，旧代码
            # 在 hasattr 为假时直接引用未赋值的 new_timeline 而崩溃。
            new_timeline = None
            if view is not None and hasattr(view, 'execute_command'):
                view.execute_command(command)
                new_timeline = getattr(view, '_timeline', None)
            elif timeline is not None:
                new_timeline = command.apply(timeline)
                self.edit_page._current_timeline = new_timeline
            updated_clip = (new_timeline or timeline).get_clip_by_id(clip_id)
            properties_panel = getattr(self, 'edit_properties_panel', None)
            update_current_clip = getattr(properties_panel, 'update_current_clip', None)
            if updated_clip is not None and callable(update_current_clip):
                update_current_clip(updated_clip)
            return

        # Import command via facade
        _cmds = get_command_classes()
        if _cmds is None:
            return
        UpdateClipParamCommand = _cmds["UpdateClipParamCommand"]

        # R-001: Delayed import for SpeedParams via facade
        _params = get_clip_params_classes()
        if _params is None:
            return
        SpeedParams = _params["SpeedParams"]

        # Get old value for undo
        old_value = None
        if param_type == "transform":
            old_value = clip.transform
        elif param_type == "crop":
            old_value = clip.crop
        elif param_type == "speed":
            old_value = clip.speed if isinstance(clip.speed, SpeedParams) else SpeedParams(speed=float(clip.speed))
        elif param_type == "opacity":
            old_value = clip.opacity
        elif param_type == "raw":
            # TRAW（批 2）：非破坏 RAW 参数
            old_value = clip.raw
        elif param_type in ("audio_sample_offset", "source_sample_offset"):
            # M14.3 采样级 UI：clip 级子帧偏移（@48k 参考率样本）
            old_value = int(getattr(clip, param_type, 0) or 0)

        # RI-04/R15：Speed 走统一 ChangeClipSpeedCommand（速度点积分、
        # FPS、linked 伙伴同步、轨锁守卫）——此前 UpdateClipParamCommand
        # 的第二套 int(source/speed) 公式使 30fps 源 2x 在 24fps 序列得
        # 60 而非 48，linked 伙伴也不随动。
        command: Any
        if param_type == "speed" and isinstance(value, SpeedParams):
            ChangeClipSpeedCommand = _cmds.get("ChangeClipSpeedCommand")
            if ChangeClipSpeedCommand is not None:
                command = ChangeClipSpeedCommand(
                    command_id=str(uuid.uuid4()),
                    label=f"Update speed.{param_path}",
                    clip_id=clip_id,
                    track_id=track_id,
                    new_speed=value,
                )
            else:  # pragma: no cover - facade 兜底
                command = UpdateClipParamCommand(
                    command_id=str(uuid.uuid4()),
                    label=f"Update {param_type}.{param_path}",
                    track_id=track_id,
                    clip_id=clip_id,
                    param_type=param_type,
                    new_value=value,
                    old_value=old_value,
                )
        else:
            command = UpdateClipParamCommand(
                command_id=str(uuid.uuid4()),
                label=f"Update {param_type}.{param_path}",
                track_id=track_id,
                clip_id=clip_id,
                param_type=param_type,
                new_value=value,
                old_value=old_value
            )

        # AUD-2707: EditPage has no ``_command_stack`` — the old hasattr
        # branch was dead code and the direct apply bypassed the undo stack.
        # Commit through the TimelineView command stack instead.
        # N09/T3-01：领域拒绝（轨锁 assert、碰撞、源界校验抛
        # ValueError/TimelineOperationError）在 Qt 信号提交边界收敛为结
        # 构化拒绝——回显原因并从 canonical model 恢复 Inspector 快照
        # 与控件显示，不再把异常抛成未捕获 Qt 槽错误、留下假值控件。
        try:
            new_timeline = self._execute_timeline_edit_command(command, timeline)
        except ValueError as exc:
            logger.info("Clip param change rejected: %s", exc)
            self._restore_properties_panel_from_timeline(timeline, [clip_id])
            return

        # Keep the properties panel's immutable clip snapshot in step with
        # the command result.  Crop/Transform controls emit a complete
        # dataclass built from this snapshot; without replacing it here, the
        # next side edit starts from the pre-edit CropParams and overwrites
        # the previous left/right/top/bottom/feather value.
        updated_clip = new_timeline.get_clip_by_id(clip_id)
        properties_panel = getattr(self, 'edit_properties_panel', None)
        update_current_clip = getattr(properties_panel, 'update_current_clip', None)
        if updated_clip is not None and callable(update_current_clip):
            update_current_clip(updated_clip)

        # A parameter-panel edit replaces the immutable TransformParams on the
        # timeline, but the visible on-canvas controller deliberately keeps its
        # own snapshot while it is drawn.  Refresh that snapshot from the new
        # timeline immediately, otherwise the image updates while its handles
        # remain at the previous transform until the selection changes.
        if param_type == "transform":
            viewer = getattr(self.edit_page, '_program_viewer', None)
            is_tool_active = getattr(viewer, 'is_transform_tool_active', None)
            if viewer is not None and callable(is_tool_active) and is_tool_active():
                self._push_selected_clip_to_transform_viewer()

        # The timeline view itself is updated by the command path (view
        # path: incremental update via execute_command; fallback:
        # _sync_timeline_to_views inside the helper) — no extra full reload.

        # Hot-swap the sequence in the preview renderer so the next
        # request_render picks up the new transform/opacity/crop values.
        # Without this the renderer keeps its stale _timeline_sequence and
        # re-emits the cached pre-edit frame (preview appears frozen).
        if self.timeline_preview_renderer is not None:
            silent = getattr(self.timeline_preview_renderer,
                             'update_sequence_silent', None)
            if callable(silent):
                silent(new_timeline)
            else:
                self.timeline_preview_renderer.set_sequence(new_timeline)

        # Request frame update for program viewer
        self._request_program_viewer_update()

    def _on_edit_track_param_changed(self, track_id: str, param_name: str, value: object) -> None:
        """Handle track parameter change from EditPropertiesPanel.

        Routes the change to the appropriate track command (volume/pan/mute/
        solo/lock/name). These are the same commands the track-header controls
        use, so the header and the properties panel stay in sync automatically
        via the incremental timeline update.
        """
        logger.debug(f"_on_edit_track_param_changed: {track_id} {param_name}={value}")

        if self.edit_page is None or not self.edit_page._current_timeline:
            logger.warning("No current timeline for track param update")
            return

        # Resolve the timeline_view to execute commands (same path as header).
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            logger.warning("No timeline_view for track param update")
            return

        from src.features.edit.core.commands import (
            SetTrackVolumeCommand, SetTrackPanCommand, SetTrackMuteCommand,
            SetTrackSoloCommand, SetTrackLockCommand, RenameTrackCommand,
            SetTrackVisibilityCommand, SetTrackSyncLockCommand,
        )

        try:
            if param_name == "volume":
                cmd = SetTrackVolumeCommand.create(track_id, float(value))
            elif param_name == "pan":
                cmd = SetTrackPanCommand.create(track_id, float(value))
            elif param_name == "is_muted":
                cmd = SetTrackMuteCommand.create(track_id, bool(value))
            elif param_name == "is_solo":
                cmd = SetTrackSoloCommand.create(track_id, bool(value))
            elif param_name == "is_locked":
                cmd = SetTrackLockCommand.create(track_id, bool(value))
            elif param_name == "is_sync_locked":
                cmd = SetTrackSyncLockCommand.create(track_id, bool(value))
            elif param_name == "is_visible":
                cmd = SetTrackVisibilityCommand.create(track_id, bool(value))
            elif param_name == "name":
                cmd = RenameTrackCommand.create(track_id, str(value))
            else:
                logger.warning(f"Unknown track param: {param_name}")
                return
        except (TypeError, ValueError) as e:
            logger.warning(f"Invalid track param value {param_name}={value}: {e}")
            return

        tview._execute_command(cmd)

    def _on_edit_track_param_preview(self, track_id: str, param_name: str, value: object) -> None:
        """Handle track parameter *preview* (drag-in-progress) from EditPropertiesPanel.

        与 _on_edit_track_param_changed 不同：这里只更新轨道头的视觉显示，
        不进入命令栈、不重建 timeline。这样拖动滑块时轨道头视觉实时跟手，
        而昂贵的命令链只在松手时（track_param_changed）执行一次。

        目前只有 volume 需要轨道头视觉同步（TrackHeaderItem 的 set_volume
        仅重绘，不发信号，无环路风险）；pan 在轨道头无视觉控件，忽略。
        volume 预览同时等比缩放该轨波形显示（纯视觉，不进命令栈）。
        """
        if param_name != "volume":
            return
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        try:
            # 波形显示随音量实时缩放（与轨头滑块同源的纯视觉预览）
            if hasattr(tview, 'set_track_waveform_gain_preview'):
                tview.set_track_waveform_gain_preview(track_id, float(value))
        except Exception:
            pass
        header = tview._track_headers.get(track_id)
        if header is None:
            return
        try:
            # set_volume 是纯视觉更新（self._volume = clamp(...); self.update()）
            header.set_volume(float(value))
        except RuntimeError:
            # header 已被 sip 释放（场景重建中），忽略本次预览
            pass

    # ------------------------------------------------------------------
    # Audio Mixer panel: sync + command dispatch
    # ------------------------------------------------------------------

    def _sync_audio_mixer_from_timeline(self, timeline) -> None:
        """根据 timeline 的音频轨列表同步 Audio Mixer 面板。

        在 _on_edit_timeline_changed 末尾调用，保证混音器与轨道增删、
        音量/声像/静音/独奏 变化（来自任意来源）保持一致。
        """
        panel = getattr(self, 'audio_mixer_panel', None)
        if panel is None or timeline is None:
            return
        audio_tracks = getattr(timeline, 'audio_tracks', ())
        if not audio_tracks:
            # 没有音频轨：清空（保留 Master 条）
            if hasattr(panel, 'clear_all_tracks'):
                panel.clear_all_tracks()
            return
        tracks = []
        for idx, track in enumerate(audio_tracks, start=1):
            tracks.append({
                "track_id": track.track_id,
                "name": getattr(track, 'name', '') or '',
                "tag": f"A{idx}",
                "volume": float(getattr(track, 'volume', 1.0)),
                "pan": float(getattr(track, 'pan', 0.0)),
                "is_muted": bool(getattr(track, 'is_muted', False)),
                "is_solo": bool(getattr(track, 'is_solo', False)),
                # W8: Strip EQ 参数（None=禁用）
                "strip_eq": getattr(track, 'strip_eq', None),
                # W26: FX 机架效果器链（None=未使用）。EQ/Dynamics/FX 列表效果器都存这里。
                "effect_chain": getattr(track, 'effect_chain', None),
                # W16: 轨道颜色
                "color": getattr(track, 'color', None),
            })
        try:
            panel.sync_tracks(tracks)
            # W12: 同步 Master 总线音量/静音（持久化字段，重开恢复）
            if hasattr(panel, 'set_master_volume_linear'):
                panel.set_master_volume_linear(float(getattr(timeline, 'master_volume', 1.0)))
            if hasattr(panel, 'set_master_muted'):
                panel.set_master_muted(bool(getattr(timeline, 'master_muted', False)))
            mgr = self._get_mixer_audio_manager()
            # AM-05: 引擎同步的真相源现在是 edit_page._sync_audio_tracks_from_timeline
            # （它已同步 master/automation/routing/EQ/FX 到引擎）。此处的引擎同步是
            # 幂等补充，覆盖首次显示 mixer dock（edit_page 尚未触发同步）的时序窗口。
            # 引擎方法为覆盖式 set_params，重复调用无副作用。
            if mgr is not None:
                try:
                    mgr.set_master_volume(float(getattr(timeline, 'master_volume', 1.0)))
                    mgr.set_master_muted(bool(getattr(timeline, 'master_muted', False)))
                except Exception as e:
                    logger.debug("master state restore to engine failed: %s", e)
            # W21: 同步 automation 到音频引擎（Read 模式下回放按曲线驱动）
            if mgr is not None and hasattr(mgr, 'set_track_automation'):
                for track in audio_tracks:
                    try:
                        if track.automation:
                            mgr.set_track_automation(track.track_id, dict(track.automation))
                    except Exception as e:
                        logger.debug("automation sync failed: %s", e)
                    # W24/W25: 同步路由信息（output_bus_id + sends + is_bus）
                    try:
                        sends_list = [(s.target_bus_id, s.level) for s in track.sends if s.enabled]
                        mgr.set_track_routing(track.track_id, track.output_bus_id,
                                              sends_list, is_bus=track.is_bus)
                    except Exception as e:
                        logger.debug("routing sync failed: %s", e)
            # W22: 同步已打开的 Automation Lane 对话框
            for dlg in getattr(self, '_automation_lanes', {}).values():
                if dlg.isVisible():
                    track = timeline.get_track_by_id(dlg._track_id)
                    if track is not None:
                        dlg.update_track_data(dict(track.automation))
        except Exception as e:
            logger.debug("Failed to sync audio mixer: %s", e)

    def _sync_audio_mixer_from_current_timeline(self) -> None:
        """首次显示 mixer dock 时由 TimelinePanel 调用，用当前 timeline 填充。"""
        timeline = getattr(getattr(self, 'edit_page', None), '_current_timeline', None)
        if timeline is not None:
            self._sync_audio_mixer_from_timeline(timeline)

    def _get_mixer_audio_manager(self):
        """获取 EditPage 的 AudioPlaybackManager（懒加载），用于实时听感预览。"""
        edit_page = getattr(self, 'edit_page', None)
        if edit_page is None:
            return None
        # EditPage._get_audio_manager 懒加载；直接访问属性以避免触发创建
        return getattr(edit_page, '_audio_manager', None)

    def _on_mixer_volume_preview(self, track_id: str, db: float) -> None:
        """混音器推子拖动期：实时听感 + 视觉联动，不进命令栈。

        - 更新 AudioPlaybackManager 该轨音量（听到效果）
        - 同步轨道头滑块视觉（TrackHeaderItem.set_volume 纯视觉）
        - §3.1: Write/Touch/Latch 模式下采集 fader 样本（自动化录制）
        """
        from src.shared.ui.audio_mixer.track_mixer_widget import db_to_linear
        linear = db_to_linear(float(db))
        # §3.1: 自动化录制采集（拖动期 = fader_down=True）
        self._record_fader_sample(track_id, "volume", linear, fader_down=True)
        # 实时听感
        mgr = self._get_mixer_audio_manager()
        if mgr is not None:
            try:
                mgr.set_track_volume(track_id, linear)
            except Exception as e:
                logger.debug("mixer volume preview (audio) failed: %s", e)
        # 轨道头视觉同步（纯视觉，无信号回环）
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is not None:
            # 波形显示随推子实时等比缩放（纯视觉，path 缓存保持有效）
            try:
                if hasattr(tview, 'set_track_waveform_gain_preview'):
                    tview.set_track_waveform_gain_preview(track_id, linear)
            except Exception:
                pass
            header = tview._track_headers.get(track_id)
            if header is not None:
                try:
                    header.set_volume(linear)
                except RuntimeError:
                    pass

    # ── §3.1: 自动化录制（Write/Touch/Latch）──

    def _get_automation_recorder(self):
        """惰性获取 automation recorder（per track/param）。"""
        if not hasattr(self, '_automation_recorders'):
            self._automation_recorders = {}
        return self._automation_recorders

    def _edit_playback_state(self) -> tuple[bool, int]:
        """E05: 时间线播放的权威 (is_playing, current_frame)。

        EditPage 从未定义 ``_is_playing``（旧读取恒为 False，自动化录制
        永不启动），也没有 ``_current_frame`` 的播放期权威值。唯一事实源
        是 TimelinePanel 共享的 ``EditPlaybackController``。
        """
        panel = getattr(self, 'timeline_panel', None)
        controller = (
            panel.get_playback_controller()
            if panel is not None and hasattr(panel, 'get_playback_controller')
            else None
        )
        if controller is None:
            return False, 0
        try:
            is_playing = controller.is_playing
            frame = getattr(controller, 'current_frame', 0)
            # 测试替身/异常桥可能给出非布尔/非数值状态——视为不可信，
            # 不据此启动录制。
            if not isinstance(is_playing, bool):
                return False, 0
            if not isinstance(frame, (int, float)) or isinstance(frame, bool):
                return is_playing, 0
            return is_playing, int(frame or 0)
        except (RuntimeError, ValueError, TypeError):
            return False, 0

    def _record_fader_sample(self, track_id: str, param: str, value: float,
                             fader_down: bool = True) -> None:
        """在 Write/Touch/Latch 模式下采集 fader 样本。

        录制器按 (track_id, param) 缓存；模式切换/播放停止时停止录制并提交命令。
        """
        mode = str(getattr(self, '_automation_mode', 'Off')).lower()
        if mode not in ("write", "touch", "latch"):
            return
        # E05: 需在播放中才录制（权威状态见 _edit_playback_state）
        is_playing, frame = self._edit_playback_state()
        if not is_playing:
            return
        recorders = self._get_automation_recorder()
        key = (track_id, param)
        rec = recorders.get(key)
        if rec is None or not rec.is_recording:
            from src.features.edit.core.automation_recorder import AutomationRecorder
            rec = AutomationRecorder()
            # E16: Touch 松手 release 需要底层曲线在松手帧的原值——从当前
            # timeline 的轨级 automation 读取（录制样本未落库，读到的即
            # pre-touch 曲线）。
            rec.start(
                param, mode, track_id,
                release_provider=self._make_automation_release_provider(
                    track_id, param
                ),
            )
            recorders[key] = rec
        rec.record(frame, value, fader_down=fader_down)

    def _make_automation_release_provider(self, track_id: str, param: str):
        """E16: 构造 Touch release 的底层曲线取值回调（不可得返回 None）。"""
        def _provider(frame: int) -> Optional[float]:
            timeline = getattr(getattr(self, 'edit_page', None),
                               '_current_timeline', None)
            track = (timeline.get_track_by_id(track_id)
                     if timeline is not None else None)
            kf_track = (getattr(track, 'automation', {}) or {}).get(param)
            if kf_track is None or not getattr(kf_track, 'keyframes', ()):
                return None
            bounds = {
                "volume": (0.0, 2.0),
                "pan": (-1.0, 1.0),
                "mute": (0.0, 1.0),
            }
            default = bounds.get(param, (0.0, 1.0))[0]
            return float(kf_track.get_value(int(frame), default=default))
        return _provider

    def _commit_automation_recordings(self) -> None:
        """停止播放时提交所有活跃的自动化录制（单次 undo each）。"""
        if not hasattr(self, '_automation_recorders'):
            return
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        recorders = self._automation_recorders
        self._automation_recorders = {}
        for (track_id, param), rec in recorders.items():
            if not rec.is_recording:
                continue
            try:
                cmd = rec.stop()
                if cmd is not None and tview is not None:
                    tview._execute_command(cmd)
            except Exception as e:
                logger.warning("Failed to commit automation recording %s/%s: %s",
                               track_id, param, e)

    def _on_playback_stopped_commit_automation(self) -> None:
        """§3.1: 播放停止信号回调——提交 Write/Latch 模式的自动化录制。

        Touch 模式的录制在松手（committed）时即提交；Write/Latch 持续到播放停止。
        """
        self._commit_automation_recordings()

    def _on_mixer_volume_committed(self, track_id: str, db: float) -> None:
        """混音器推子松手：提交一次 SetTrackVolumeCommand。"""
        from src.shared.ui.audio_mixer.track_mixer_widget import db_to_linear
        linear = db_to_linear(float(db))
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        from src.features.edit.core.commands import SetTrackVolumeCommand
        try:
            cmd = SetTrackVolumeCommand.create(track_id, linear)
        except (TypeError, ValueError) as e:
            logger.warning("Invalid mixer volume %s: %s", db, e)
            return
        tview._execute_command(cmd)

    def _on_mixer_balance_preview(self, track_id: str, balance: int) -> None:
        """混音器 balance 推子拖动期：实时听感预览。"""
        from src.shared.ui.audio_mixer.track_mixer_widget import balance_to_pan
        pan = balance_to_pan(int(balance))
        # §3.1: 自动化录制采集 pan
        self._record_fader_sample(track_id, "pan", pan, fader_down=True)
        mgr = self._get_mixer_audio_manager()
        if mgr is not None:
            try:
                mgr.set_track_pan(track_id, pan)
            except Exception as e:
                logger.debug("mixer pan preview failed: %s", e)

    def _on_mixer_balance_committed(self, track_id: str, balance: int) -> None:
        """混音器 balance 推子松手：提交一次 SetTrackPanCommand。"""
        from src.shared.ui.audio_mixer.track_mixer_widget import balance_to_pan
        pan = balance_to_pan(int(balance))
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        from src.features.edit.core.commands import SetTrackPanCommand
        try:
            cmd = SetTrackPanCommand.create(track_id, pan)
        except (TypeError, ValueError) as e:
            logger.warning("Invalid mixer balance %s: %s", balance, e)
            return
        tview._execute_command(cmd)

    def _on_mixer_mute_toggled(self, track_id: str, muted: bool) -> None:
        """混音器 M 按钮：实时听感 + 提交 SetTrackMuteCommand。

        B6: 乐观更新 —— 立即调 mgr.set_track_mute 让引擎响应（消除命令往返延迟），
        同时提交命令保证持久化和 undo。引擎是幂等的，命令执行后的 sync 会覆盖为相同值。
        """
        # B6: 实时听感（乐观更新）
        mgr = self._get_mixer_audio_manager()
        if mgr is not None:
            try:
                mgr.set_track_mute(track_id, bool(muted))
            except Exception as e:
                logger.debug("mgr.set_track_mute preview failed: %s", e)

        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        from src.features.edit.core.commands import SetTrackMuteCommand
        try:
            cmd = SetTrackMuteCommand.create(track_id, bool(muted))
        except (TypeError, ValueError) as e:
            logger.warning("Invalid mixer mute %s: %s", muted, e)
            return
        tview._execute_command(cmd)

    def _on_mixer_solo_toggled(self, track_id: str, solo: bool) -> None:
        """混音器 S 按钮：实时听感 + 提交 SetTrackSoloCommand。

        面板内部已做 UI 层静音联动（其他轨视觉静音），真正的 solo 语义
        通过这里落库，引擎在下次混音时根据 is_solo 字段处理。

        B6: 乐观更新 —— 立即调 mgr.set_track_solo 让引擎响应（消除命令往返延迟），
        同时提交命令保证持久化和 undo。
        """
        # B6: 实时听感（乐观更新）
        mgr = self._get_mixer_audio_manager()
        if mgr is not None:
            try:
                mgr.set_track_solo(track_id, bool(solo))
            except Exception as e:
                logger.debug("mgr.set_track_solo preview failed: %s", e)

        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        from src.features.edit.core.commands import SetTrackSoloCommand
        try:
            cmd = SetTrackSoloCommand.create(track_id, bool(solo))
        except (TypeError, ValueError) as e:
            logger.warning("Invalid mixer solo %s: %s", solo, e)
            return
        tview._execute_command(cmd)

    def _on_mixer_batch_solo_cleared(self, track_ids: list) -> None:
        """C4/AM-12: Clear All Solo —— 用 MacroCommand 聚合为单次 undo。

        AM-12: 此前逐条 _execute_command 导致每个命令独立入栈，恢复需多次 Ctrl+Z，
        与"单次 undo"语义不符。现用 MacroCommand 包裹所有 SetTrackSoloCommand。
        实时听感已由 panel.clear_all_solo 内部 set_solo + 乐观更新处理。
        """
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        from src.features.edit.core.commands import SetTrackSoloCommand
        mgr = self._get_mixer_audio_manager()
        # 实时听感同步引擎（乐观更新已在 panel 做了视觉）
        if mgr is not None:
            for tid in track_ids:
                try:
                    mgr.set_track_solo(tid, False)
                except Exception as e:
                    logger.debug("clear solo (audio) failed for %s: %s", tid, e)
        # AM-12: 聚合为单个 MacroCommand（一次 undo）
        sub_commands = []
        for tid in track_ids:
            try:
                sub_commands.append(SetTrackSoloCommand.create(tid, False))
            except (TypeError, ValueError) as e:
                logger.warning("batch clear solo %s: %s", tid, e)
        self._execute_batch_macro(tview, sub_commands, "Clear All Solo")

    def _on_mixer_batch_mute_changed(self, changes: list) -> None:
        """C4/AM-12: Mute All / Unmute All —— 用 MacroCommand 聚合为单次 undo。

        changes 是 [(track_id, old_muted), ...]，新状态总是目标状态
        （mute_all → 全 True，unmute_all → 全 False）。
        AM-12: 用 MacroCommand 包裹所有 SetTrackMuteCommand，一次 undo 恢复。
        """
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        from src.features.edit.core.commands import SetTrackMuteCommand
        mgr = self._get_mixer_audio_manager()
        # 判断目标状态：若 changes 里第一个 old_muted 是 False，说明是从 unmute→mute（mute_all）
        target_muted = not changes[0][1] if changes else False
        # 实时听感同步引擎
        if mgr is not None:
            for tid, _old in changes:
                try:
                    mgr.set_track_mute(tid, target_muted)
                except Exception as e:
                    logger.debug("batch mute (audio) failed for %s: %s", tid, e)
        # AM-12: 聚合为单个 MacroCommand
        sub_commands = []
        for tid, _old_muted in changes:
            try:
                sub_commands.append(SetTrackMuteCommand.create(tid, target_muted))
            except (TypeError, ValueError) as e:
                logger.warning("batch mute %s: %s", tid, e)
        self._execute_batch_macro(tview, sub_commands, "Batch Mute")

    def _execute_batch_macro(self, tview, sub_commands: list, label: str) -> None:
        """AM-12: 把多个子命令聚合为单个 MacroCommand 执行（一次 undo）。

        单个命令时直接执行（无需 macro 开销）；多个命令时包裹为 MacroCommand。
        """
        if not sub_commands:
            return
        from src.features.edit.core.commands import MacroCommand
        try:
            if len(sub_commands) == 1:
                tview._execute_command(sub_commands[0])
            else:
                macro = MacroCommand.create(label, sub_commands)
                tview._execute_command(macro)
        except Exception as e:  # noqa: BLE001
            logger.warning("batch macro execute failed (%s): %s", label, e)

    def _on_mixer_master_volume(self, db: float) -> None:
        """Master 推子松手：提交 SetMasterVolumeCommand（持久化）+ 实时听感。

        W12: Master 音量现在落库（重开项目恢复），同时实时调 AudioPlaybackManager。
        修复：原实现 clamp 到 [0,1]（适配已废弃的 QAudioSink.setVolume 上限），
        导致 >0dB 正增益被截断。现走 AudioMixer sample 级（支持 >1.0），
        保留完整线性值（db_to_linear 已 clamp 到 [VOLUME_LINEAR_MIN, VOLUME_LINEAR_MAX]=[0,2]）。
        """
        from src.shared.ui.audio_mixer.track_mixer_widget import db_to_linear
        # db_to_linear 已 clamp 到 [0.0, 2.0]（VOLUME_LINEAR_MAX），无需再 clamp
        linear = db_to_linear(float(db))

        # 实时听感
        mgr = self._get_mixer_audio_manager()
        if mgr is not None:
            try:
                mgr.set_master_volume(linear)
            except Exception as e:
                logger.debug("mixer master volume (audio) failed: %s", e)

        # 持久化命令（W12）
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is not None:
            try:
                from src.features.edit.core.commands import SetMasterVolumeCommand
                cmd = SetMasterVolumeCommand.create(linear)
                tview._execute_command(cmd)
            except Exception as e:
                logger.warning("Failed to execute master volume command: %s", e)

    def _on_mixer_master_mute(self, muted: bool) -> None:
        """Master Mute：提交 SetMasterMuteCommand（持久化）+ 实时静音。

        修复：原用 set_master_volume(0/1) 模拟静音（会丢失原 volume 值、且依赖失效的
        QAudioSink 路径），改用语义化的 set_master_muted，AudioMixer 在 mix_samples
        末端清零输出，恢复时用原 master_volume（不再被覆盖为 1.0）。
        """
        # 实时静音
        mgr = self._get_mixer_audio_manager()
        if mgr is not None:
            try:
                mgr.set_master_muted(bool(muted))
            except Exception as e:
                logger.debug("mixer master mute (audio) failed: %s", e)

        # 持久化命令（W12）
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is not None:
            try:
                from src.features.edit.core.commands import SetMasterMuteCommand
                cmd = SetMasterMuteCommand.create(muted)
                tview._execute_command(cmd)
            except Exception as e:
                logger.warning("Failed to execute master mute command: %s", e)

    def _on_mixer_show_effects(self, track_id: str) -> None:
        """混音器 FX 按钮：打开 Strip EQ 对话框（W8）。"""
        # Strip EQ 对话框由 UI 层直接弹出（track_mixer_widget 内部处理），
        # controller 这里只做日志，避免重复弹窗。
        logger.debug("mixer FX/Strip EQ requested for track %s", track_id)

    def _on_mixer_record_arm_toggled(self, track_id: str, armed: bool) -> None:
        """§4.4: 混音器 R 按钮（Record Arm）→ RecordingManager 录音。

        arm=True：启动录音（pre-roll 倒计时后采集输入设备）。
        arm=False：停止录音，落盘 take，导入为新 asset。
        """
        if armed:
            self._start_recording(track_id)
        else:
            self._stop_recording_and_import(track_id)

    def _start_recording(self, track_id: str) -> None:
        """启动录音（§4.4 / P1-10：input patch、时间码对齐、监听、电平）。

        P1-10 增强：
        - input patch（设备/声道/pre-roll/post-roll）从 ADR Take Sheet
          面板读取（面板缺失时用保守默认值）；
        - 打点 = 当前播放头帧，随序列 fps 写进 take 的
          time_reference_samples / start_timecode（bext TimeReference）；
        - 输入监听（QAudioSink 回放）与输入电平表接到面板；
        - 延迟校准结果（已"校准"过）应用到时间基准。
        """
        from src.features.edit.core.recording_manager import (
            RecordingManager, RecordingConfig,
        )
        # 获取输出目录（工程目录下的 recordings/，或临时目录）
        edit_page = getattr(self, 'edit_page', None)
        project = getattr(self, '_current_edit_project', None)
        if project is not None and getattr(project, 'project_dir', ''):
            output_dir = os.path.join(project.project_dir, 'recordings')
        else:
            import tempfile
            output_dir = tempfile.gettempdir()
        # 获取 track 名作为场次
        timeline = getattr(edit_page, '_current_timeline', None) if edit_page else None
        track_name = track_id
        if timeline is not None:
            track = timeline.get_track_by_id(track_id)
            if track is not None:
                track_name = getattr(track, 'name', '') or track_id

        # 防御：同轨已 armed（manager 仍在采）不得覆盖，否则旧采集成孤儿
        existing = getattr(self, '_recording_managers', {}).get(track_id)
        if existing is not None and getattr(existing, 'is_recording', False):
            logger.warning("Track %s already recording", track_id)
            return

        # P1-10：input patch / pre-post roll / loop / 监听
        self._wire_adr_panel()
        settings = self._adr_settings_dict()
        fps, in_point_frame = self._adr_timeline_context()

        config = RecordingConfig(
            device_id=settings.get('device_id') or None,
            output_dir=output_dir,
            scene_name=track_name,
            pre_roll_seconds=float(settings.get('pre_roll_seconds', 1.0)),
            post_roll_seconds=float(settings.get('post_roll_seconds', 1.0)),
            channels=int(settings.get('channels', 1) or 1),  # VO/ADR mono
            bit_depth=24,
            fps=fps,
            latency_ms=float(getattr(self, '_adr_latency_ms', 0.0) or 0.0),
        )
        mgr = RecordingManager(config)
        if getattr(self, '_adr_calibration', None) is not None:
            mgr.set_calibration(self._adr_calibration)
        mgr.enable_live_post_roll = True  # 停止后采集 post-roll 尾巴再落盘
        # 状态回调（携带录音时的工程标识：post-roll 秒内切工程防错投）
        project_id = getattr(project, 'project_id', None)
        mgr.on_take_completed = (
            lambda take, _pid=project_id: self._on_take_recorded(track_id, take, _pid))
        mgr.on_error = lambda msg: logger.error("Recording error: %s", msg)
        panel = self._adr_panel()
        if panel is not None:
            mgr.on_level = panel.set_input_level
            mgr.on_state_changed = panel.set_recording_state
            if settings.get('monitor') and panel.begin_monitor(
                    config.sample_rate, config.channels):
                mgr.on_monitor_block = panel.monitor_write_block()
        if not mgr.start_recording(in_point_frame=in_point_frame):
            logger.warning("Failed to start recording for track %s", track_id)
            return
        if not hasattr(self, '_recording_managers'):
            self._recording_managers = {}
        self._recording_managers[track_id] = mgr
        # loop 录音（审计修复：按轨状态 + In/Out 范围截止定时，真正可达。
        # 旧实现 take 只能靠 disarm 结束，而 disarm 必清全局 loop 标志，
        # 续录分支在真实 UI 流程中不可达）
        if not hasattr(self, '_adr_loop_tracks'):
            self._adr_loop_tracks = set()
            self._adr_in_points = {}
        self._adr_in_points[track_id] = in_point_frame
        if settings.get('loop', False):
            loop_seconds = self._adr_loop_window_seconds(fps)
            if loop_seconds and loop_seconds > 0:
                self._adr_loop_tracks.add(track_id)
                self._schedule_loop_deadline(
                    track_id, mgr, loop_seconds,
                    pre_roll_seconds=config.pre_roll_seconds)
            else:
                status_bar = getattr(self, 'status_bar', None)
                if status_bar is not None:
                    status_bar.showMessage(
                        "Loop record needs a timeline In/Out range", 4000)
                logger.info("Loop record ignored: no timeline In/Out range")
        logger.info("Recording started for track %s (pre-roll %.1fs)",
                    track_id, config.pre_roll_seconds)

    def _stop_recording_and_import(self, track_id: str) -> None:
        """停止录音并导入 take（§4.4 / RMA-11）。

        take 完成只有一个事件源：`mgr.stop_recording()` 内部的 `_finalize_take`
        会在 take 成功落盘后调用 `on_take_completed` 回调（即 `_on_take_recorded`）。
        因此这里**不得**再次显式调用 `_on_take_recorded`，否则同一 take 会被
        导入两次（虽然 import_asset 的路径去重能兜底成单 asset，但会跑两遍
        探测/日志，属冗余且掩盖了真正的事务边界）。
        """
        if hasattr(self, '_adr_loop_tracks'):
            self._adr_loop_tracks.discard(track_id)  # disarm 终止该轨 loop
        if not hasattr(self, '_recording_managers'):
            return
        mgr = self._recording_managers.pop(track_id, None)
        if mgr is None:
            return
        mgr.on_monitor_block = None
        panel = self._adr_panel()
        if panel is not None:
            panel.end_monitor()
        # stop_recording → _finalize_take → on_take_completed → _on_take_recorded
        # 这是 take 完成的唯一事件源；不再在此二次调用 _on_take_recorded。
        mgr.stop_recording()

    def _on_take_recorded(self, track_id: str, take,
                          recorded_project_id=None) -> None:
        """录音 take 完成 → 导入为新 asset（§4.4 / RMA-11）。

        将录制的 WAV 文件作为 audio asset 导入工程，并把 `import_asset` 返回的
        新 EditProject 回写到会话（之前 new_project 被丢弃，take 无法可靠进入工程）。
        回写走与 media_pool 单文件导入一致的三步：import → set_project →
        project_changed.emit，确保 bin/timeline/renderer 同步刷新。
        """
        if take is None or take.status not in ("completed", "completed_partial"):
            logger.info("Take discarded (status=%s)", getattr(take, 'status', 'unknown'))
            return
        try:
            # 工程引用真源是 self._current_edit_project。
            project = getattr(self, '_current_edit_project', None)
            if project is None:
                logger.warning("No project to import take into")
                return
            # 审计修复：post-roll 秒内切换工程 → take 不导入别的工程
            # （文件保留在磁盘，由原工程重开或手动导入）
            current_pid = getattr(project, 'project_id', None)
            if (recorded_project_id is not None and current_pid is not None
                    and recorded_project_id != current_pid):
                logger.warning(
                    "Take recorded for project %s but current project is %s; "
                    "skip import (file remains on disk)",
                    recorded_project_id, current_pid)
                return
            # 探测 take 元数据；take 字段随 asset metadata 持久化
            # （P1-10 take sheet 重开工程可从素材重建）
            from src.features.edit.core.project import _probe_audio_info
            from src.features.edit.core.take_sheet import take_metadata_for_asset
            audio_info = _probe_audio_info(take.file_path) or {
                "channels": take.channels, "sample_rate": take.sample_rate,
            }
            metadata = {"audio_info": audio_info, **take_metadata_for_asset(take)}

            # P1-10：一次 Undo 的导入 —— ImportMediaAssetCommand 进
            # media_pool 的 ProjectCommandStack；无栈环境（测试桩）回退
            # 直接 import_asset（路径去重天然幂等）。
            new_project = None
            stack_getter = getattr(self.media_pool_panel, "get_command_stack", None)
            if callable(stack_getter):
                try:
                    from src.features.edit.core.commands import (
                        ImportMediaAssetCommand,
                    )
                    cmd = ImportMediaAssetCommand.create(
                        path=take.file_path,
                        media_type="audio",
                        metadata=metadata,
                        display_name=take.name or os.path.basename(take.file_path),
                        label=f"Record Take {take.take_number:02d}",
                    )
                    new_project = stack_getter().execute(cmd, project)
                except Exception as e:
                    logger.warning("Command-stack take import failed: %s", e)
                    new_project = None
            if new_project is None:
                new_project = project.import_asset(
                    path=take.file_path,
                    media_type="audio",
                    metadata=metadata,
                    display_name=take.name or os.path.basename(take.file_path),
                )
            # 回写新工程：仅在 import 真正产生新实例时刷新（去重时 new_project is project）。
            if new_project is not project:
                self._apply_project_change(new_project)
                logger.info("Take imported as asset: %s (%.2fs)",
                            take.name, take.duration_seconds)
            else:
                logger.info("Take asset already present (dedup): %s", take.name)

            # P1-10：take sheet 条目（含 asset_id 反查，供标记最佳/替换）
            asset = new_project.get_asset_by_path(take.file_path)
            panel = self._adr_panel()
            if panel is not None:
                panel.add_take_entry(
                    take,
                    asset_id=asset.asset_id if asset is not None else "",
                    track_id=track_id,
                )

            # P1-10 loop record：In/Out 截止的 take 在完成回调后同打点
            # 续录。disarm（pop manager / 清 _adr_loop_tracks）自然停止；
            # 审计修复：身份+在录守卫防 post-roll 秒内 disarm→再 arm 时
            # 错抓新 manager 重启。
            loop_tracks = getattr(self, '_adr_loop_tracks', None)
            if (loop_tracks and track_id in loop_tracks
                    and getattr(self, '_recording_managers', {}).get(track_id) is not None):
                mgr = self._recording_managers[track_id]
                if not getattr(mgr, 'is_recording', False):
                    in_point = getattr(self, '_adr_in_points', {}).get(track_id)
                    fps, _unused = self._adr_timeline_context()
                    loop_seconds = self._adr_loop_window_seconds(fps)
                    try:
                        if (mgr.start_recording(in_point_frame=in_point)
                                and loop_seconds and loop_seconds > 0):
                            self._schedule_loop_deadline(
                                track_id, mgr, loop_seconds,
                                pre_roll_seconds=getattr(
                                    mgr._config, 'pre_roll_seconds', 1.0))
                    except Exception as e:
                        logger.warning("Loop restart failed: %s", e)
        except Exception as e:
            logger.warning("Failed to import take %s: %s", take.file_path, e)

    # ------------------------------------------------------------------
    # P1-10: ADR take sheet / input patch / 延迟校准 接线
    # ------------------------------------------------------------------

    def _adr_panel(self):
        """ADR Take Sheet 面板（未创建时 None；所有接线防御式）。"""
        return getattr(self, 'adr_take_sheet_panel', None)

    def _wire_adr_panel(self) -> None:
        """面板信号一次性接线（幂等）。"""
        if getattr(self, '_adr_panel_wired', False):
            return
        panel = self._adr_panel()
        if panel is None:
            return
        try:
            panel.calibrate_requested.connect(self._on_adr_calibrate)
            panel.mark_best_requested.connect(self._on_adr_mark_best)
            panel.replace_clip_requested.connect(self._on_adr_replace_clip)
            self._adr_panel_wired = True
        except Exception as e:
            logger.warning("ADR panel wiring failed: %s", e)

    def _adr_settings_dict(self) -> dict:
        """读取面板 input patch / 录音设置（面板缺失返回空 → 用默认值）。"""
        panel = self._adr_panel()
        getter = getattr(panel, 'adr_settings', None)
        if callable(getter):
            try:
                return dict(getter())
            except Exception as e:
                logger.warning("ADR settings read failed: %s", e)
        return {}

    def _adr_timeline_context(self):
        """(fps, in_point_frame)：录音打点 = 当前播放头帧（P1-10 时间码对齐）。"""
        edit_page = getattr(self, 'edit_page', None)
        timeline = getattr(edit_page, '_current_timeline', None) if edit_page else None
        fps = getattr(timeline, 'fps', None) if timeline is not None else None
        in_point = None
        if edit_page is not None:
            frame = getattr(edit_page, '_current_frame', None)
            if frame is not None:
                try:
                    in_point = int(frame)
                except (TypeError, ValueError):
                    in_point = None
        return fps, in_point

    def _adr_loop_window_seconds(self, fps) -> Optional[float]:
        """loop 窗口 = 时间线 In/Out 范围时长（秒）；无范围返回 None。"""
        timeline = getattr(getattr(self, 'edit_page', None), '_current_timeline', None)
        if timeline is None:
            return None
        range_start = getattr(timeline, 'range_start', None)
        range_end = getattr(timeline, 'range_end', None)
        if range_start is None or range_end is None or int(range_end) <= int(range_start):
            return None
        try:
            fps_value = float(fps if fps is not None
                              else getattr(timeline, 'fps', 24.0))
        except (TypeError, ValueError):
            fps_value = 24.0
        if fps_value <= 0:
            return None
        return (int(range_end) - int(range_start)) / fps_value

    def _schedule_loop_deadline(self, track_id: str, mgr,
                                loop_seconds: float,
                                pre_roll_seconds: float = 0.0) -> None:
        """loop take 截止定时：到点只结束当前 take（manager 留缓存，
        on_take_completed 的续录分支重新接管；disarm pop 后自然失效）。"""
        try:
            from PyQt6.QtCore import QTimer
            timer = QTimer(self)

            def _deadline() -> None:
                current = getattr(self, '_recording_managers', {}).get(track_id)
                if current is mgr and getattr(mgr, 'is_recording', False):
                    mgr.stop_recording()  # 不 pop：loop 续录分支仍可达

            timer.setSingleShot(True)
            timer.timeout.connect(_deadline)
            timer.start(int((max(0.0, pre_roll_seconds) + loop_seconds) * 1000) + 100)
            self._adr_loop_timers = getattr(self, '_adr_loop_timers', {})
            self._adr_loop_timers[track_id] = timer  # 持引用防 GC
        except Exception as e:
            logger.warning("Loop deadline schedule failed: %s", e)

    def _adr_resync_sheet(self, project) -> None:
        """take sheet 与当前工程同步重建（外部 undo/redo 后调用）。"""
        panel = self._adr_panel()
        if panel is None:
            return
        try:
            from src.features.edit.core.take_sheet import (
                build_take_sheet_from_project,
            )
            panel.set_take_sheet(build_take_sheet_from_project(project))
            self._adr_sheet_project = project
        except Exception as e:
            logger.warning("Take sheet resync failed: %s", e)

    def _on_adr_calibrate(self) -> None:
        """P1-10：点击"校准" → 往返 click 测量 → 保存并显示（≤10ms 合格）。

        AUD-2713: 测量移到 ``_AdrCalibrationWorker``（QThread）执行，完成后
        经 queued 信号回主线程写 ``_adr_calibration`` 并刷新面板；测量期间
        防重入（重复点击直接忽略）。
        """
        worker = getattr(self, "_adr_calibration_worker", None)
        if worker is not None and worker.isRunning():
            return
        worker = _AdrCalibrationWorker(self._adr_settings_dict().get('device_id') or None)
        worker.completed.connect(self._on_adr_calibration_ready)
        self._adr_calibration_worker = worker
        worker.start()

    def _on_adr_calibration_ready(self, calibration: Any) -> None:
        """AUD-2713: 主线程应用校准结果（含失败 → None 的路径）。"""
        self._adr_calibration_worker = None
        self._adr_calibration = calibration
        self._adr_latency_ms = (
            float(calibration.calibrated_offset_ms)
            if calibration is not None else 0.0
        )
        panel = self._adr_panel()
        if panel is not None:
            panel.set_calibration(calibration)

    def _on_adr_mark_best(self, take_id: str) -> None:
        """take sheet ★ → MarkBestTakeCommand（同场次互斥、可撤销）。"""
        panel = self._adr_panel()
        sheet = getattr(panel, 'take_sheet', lambda: None)() if panel else None
        entry = sheet.entry_for_take(take_id) if sheet is not None else None
        if entry is None or not entry.asset_id:
            logger.info("Mark best: take %s has no imported asset", take_id)
            return
        project = getattr(self, '_current_edit_project', None)
        stack_getter = getattr(self.media_pool_panel, 'get_command_stack', None)
        if project is None or not callable(stack_getter):
            logger.warning("Mark best: no project or command stack")
            return
        # 审计修复：媒体池 undo 等路径可能已移除 take asset——面板与工程
        # 同步重建后再拒绝，不给不存在的 take 打 ★
        if entry.asset_id not in project.assets:
            self._adr_resync_sheet(project)
            logger.info("Mark best: asset %s no longer in project",
                        entry.asset_id)
            return
        try:
            from src.features.edit.core.commands import MarkBestTakeCommand
            cmd = MarkBestTakeCommand.create(entry.asset_id)
            new_project = stack_getter().execute(cmd, project)
            self._apply_project_change(new_project)
        except Exception as e:
            logger.warning("Mark best take failed: %s", e)
            return
        if panel is not None:
            panel.mark_best_locally(take_id)

    def _on_adr_replace_clip(self, take_id: str) -> None:
        """take → 替换时间线所选片段（UpdateClipSourceCommand，时间码对齐）。

        take 首样本的时间线位置 = time_reference_samples / 48000 * fps
        （含 pre-roll 与延迟补偿）；替换后 clip 起点的 source_start 按
        clip.timeline_start 与该位置对齐，打点即对位。
        """
        from src.features.edit.core.commands import UpdateClipSourceCommand
        panel = self._adr_panel()
        sheet = getattr(panel, 'take_sheet', lambda: None)() if panel else None
        entry = sheet.entry_for_take(take_id) if sheet is not None else None
        if entry is None or not entry.asset_id:
            return
        project = getattr(self, '_current_edit_project', None)
        # 审计修复：UpdateClipSourceCommand 不校验 asset 存在——先确认
        # take asset 仍在当前工程，防媒体池 undo 后悬空引用
        if project is not None and entry.asset_id not in project.assets:
            self._adr_resync_sheet(project)
            logger.info("Replace clip: asset %s no longer in project",
                        entry.asset_id)
            return
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        timeline = getattr(getattr(self, 'edit_page', None), '_current_timeline', None)
        if tview is None or timeline is None:
            return
        selected_ids = list(getattr(
            getattr(tview, '_selection', None), 'selected_clip_ids', ()) or ())
        if not selected_ids:
            status_bar = getattr(self, 'status_bar', None)
            if status_bar is not None:
                status_bar.showMessage(
                    "Select a timeline clip to replace with the take", 3000)
            return
        try:
            fps_f = float(getattr(timeline, 'fps', 24.0) or 24.0)
        except (TypeError, ValueError):
            fps_f = 24.0
        first_sample_frame = None
        if entry.in_point_frame >= 0 and entry.time_reference_samples > 0:
            # take 文件采样率固定 48000（RecordingConfig 默认，不允许改）
            first_sample_frame = (
                entry.time_reference_samples / 48000.0 * fps_f)
        for clip_id in selected_ids[:1]:  # 替换第一个选中片段
            for track in timeline.all_tracks:
                clip = track.get_clip_by_id(clip_id)
                if clip is None:
                    continue
                source_start = 0
                if first_sample_frame is not None:
                    source_start = max(
                        0, int(round(clip.timeline_start - first_sample_frame)))
                cmd = UpdateClipSourceCommand.create(
                    track.track_id, clip.clip_id, entry.asset_id, source_start)
                tview._execute_command(cmd)
                break

    def _apply_project_change(self, new_project) -> None:
        """把新的 EditProject 回写到会话（RMA-11）。

        统一的项目级回写：更新 _current_edit_project、同步 timeline_view/media_pool、
        发射 project_changed 让 file_controller 落库并刷新窗口标题。与
        timeline_view._handle_external_file_drop、media_pool._import_single_file
        走一致的信号链。
        """
        self._current_edit_project = new_project
        mpp = getattr(self, 'media_pool_panel', None)
        if mpp is not None and hasattr(mpp, 'set_project'):
            mpp.set_project(new_project)
        tpanel = getattr(self, 'timeline_panel', None)
        tview = getattr(tpanel, 'timeline_view', None) if tpanel else None
        if tview is not None and hasattr(tview, 'set_project'):
            tview.set_project(new_project)
            if hasattr(tview, 'project_changed'):
                tview.project_changed.emit(new_project)
        tpr = getattr(self, 'timeline_preview_renderer', None)
        if tpr is not None and hasattr(tpr, 'set_project'):
            tpr.set_project(new_project)
        # P1-10：工程变更后重建 take sheet（单一真值 = asset metadata；
        # 同一实例重复回写不重建，录音导入后的 add_take_entry 不受影响）
        panel = getattr(self, 'adr_take_sheet_panel', None)
        if (panel is not None
                and getattr(self, '_adr_sheet_project', None) is not new_project):
            try:
                from src.features.edit.core.take_sheet import (
                    build_take_sheet_from_project,
                )
                panel.set_take_sheet(build_take_sheet_from_project(new_project))
                self._adr_sheet_project = new_project
            except Exception as e:
                logger.warning("Take sheet rebuild failed: %s", e)

    def _on_mixer_track_name_changed(self, track_id: str, new_name: str) -> None:
        """W16: strip 标签重命名 → 提交 RenameTrackCommand。"""
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            logger.warning("No timeline_view for track rename")
            return
        try:
            from src.features.edit.core.commands import RenameTrackCommand
            cmd = RenameTrackCommand.create(track_id, str(new_name))
            tview._execute_command(cmd)
        except Exception as e:
            logger.warning("Failed to execute track rename command: %s", e)

    def _on_mixer_track_color_changed(self, track_id: str, color) -> None:
        """W16: strip 颜色变更 → 提交 SetTrackColorCommand。"""
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            logger.warning("No timeline_view for track color")
            return
        try:
            from src.features.edit.core.commands import SetTrackColorCommand
            cmd = SetTrackColorCommand.create(track_id, color)
            tview._execute_command(cmd)
        except Exception as e:
            logger.warning("Failed to execute track color command: %s", e)

    # ── W22/W23: Automation Lane ──

    def _on_mixer_show_automation(self, track_id: str) -> None:
        """W22: 打开 Automation Lane 对话框。"""
        # 延迟导入
        from src.shared.ui.audio_mixer.automation_lane import AutomationLane

        # 若已打开则聚焦
        dlg = getattr(self, '_automation_lanes', {}).get(track_id)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return

        # 取轨道名 + 总帧数
        timeline = getattr(getattr(self, 'edit_page', None), '_current_timeline', None)
        track_name = track_id
        total_frames = 1000
        if timeline is not None:
            track = timeline.get_track_by_id(track_id)
            if track is not None:
                track_name = track.name or track_id
            total_frames = max(1, getattr(timeline, 'duration_frames', 1000) or 1000)

        dlg = AutomationLane(track_id, track_name, total_frames=total_frames, parent=self._main_window_ref())
        # 连接信号
        dlg.add_keyframe.connect(self._on_mixer_automation_add)
        dlg.remove_keyframe.connect(self._on_mixer_automation_remove)
        dlg.clear_param.connect(self._on_mixer_automation_clear)
        dlg.automation_mode_changed.connect(self._on_mixer_automation_mode_changed)
        # AUD-37702: 双击切换关键帧插值（此前发射侧已就绪但无消费者，
        # 功能静默失效）
        dlg.set_keyframe_interp.connect(self._on_mixer_automation_set_interp)
        # 初始同步当前 automation 数据
        if timeline is not None:
            track = timeline.get_track_by_id(track_id)
            if track is not None:
                dlg.update_track_data(dict(track.automation))
        # 存储 + 显示
        if not hasattr(self, '_automation_lanes'):
            self._automation_lanes = {}
        self._automation_lanes[track_id] = dlg
        dlg.show()

    def _main_window_ref(self):
        """获取主窗口引用（Automation Lane 的 parent）。"""
        return getattr(self, 'main_window', None) or None

    def _on_mixer_automation_add(self, track_id: str, param: str, frame: int, value: float) -> None:
        """W20: 添加 automation 关键帧。"""
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        try:
            from src.features.edit.core.commands import AddAutomationKeyframeCommand
            # 参数值域
            min_v, max_v = {"volume": (0.0, 2.0), "pan": (-1.0, 1.0), "mute": (0.0, 1.0)}.get(param, (0.0, 1.0))
            cmd = AddAutomationKeyframeCommand.create(track_id, param, frame, value,
                                                     min_val=min_v, max_val=max_v)
            tview._execute_command(cmd)
        except Exception as e:
            logger.warning("Failed to execute automation add: %s", e)

    def _on_mixer_automation_remove(self, track_id: str, param: str, frame: int) -> None:
        """W20: 移除 automation 关键帧。"""
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        try:
            from src.features.edit.core.commands import RemoveAutomationKeyframeCommand
            cmd = RemoveAutomationKeyframeCommand.create(track_id, param, frame)
            tview._execute_command(cmd)
        except Exception as e:
            logger.warning("Failed to execute automation remove: %s", e)

    def _on_mixer_automation_set_interp(self, track_id: str, param: str,
                                        frame: int, interp: str) -> None:
        """AUD-37702: 双击切换关键帧插值类型（linear/bezier/hold）。

        消费 AutomationLane.set_keyframe_interp，提交
        SetAutomationKeyframeInterpolationCommand（进撤销栈，一次 undo
        恢复原插值；值与帧号不变）。
        """
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        try:
            from src.features.edit.core.commands import (
                SetAutomationKeyframeInterpolationCommand,
            )
            cmd = SetAutomationKeyframeInterpolationCommand.create(
                track_id, param, frame, interp)
            tview._execute_command(cmd)
        except ValueError as e:
            # 关键帧不存在（如已被并发编辑移除）：不提交空命令、不崩溃
            logger.warning("Failed to set automation interpolation: %s", e)

    def _on_mixer_automation_clear(self, track_id: str, param: str) -> None:
        """W20: 清空某参数的 automation。"""
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        try:
            from src.features.edit.core.commands import ClearAutomationCommand
            cmd = ClearAutomationCommand.create(track_id, param)
            tview._execute_command(cmd)
        except Exception as e:
            logger.warning("Failed to execute automation clear: %s", e)

    def _on_mixer_automation_mode_changed(self, mode: str) -> None:
        """W23: 全局 automation 模式切换（Off/Read/Write/Touch/Latch）。

        本次实现 Read（回放时按曲线驱动）和 Write（录制推子移动）的核心；
        Touch/Latch 是 Write 的变体（停止写入的时机不同）。
        """
        mgr = self._get_mixer_audio_manager()
        # 同步 automation 数据到引擎（Read 模式必须）
        timeline = getattr(getattr(self, 'edit_page', None), '_current_timeline', None)
        if timeline is not None and mgr is not None and hasattr(mgr, 'set_track_automation'):
            for track in timeline.audio_tracks:
                try:
                    mgr.set_track_automation(track.track_id, dict(track.automation))
                except Exception as e:
                    logger.debug("automation sync failed: %s", e)
        # 记录当前模式（Write/Touch/Latch 时由播放循环写入关键帧）
        if not hasattr(self, '_automation_mode'):
            self._automation_mode = "Off"
        self._automation_mode = mode
        logger.info("automation mode changed to: %s", mode)

    def _on_mixer_strip_eq_preview(self, track_id: str, params) -> None:
        """Strip EQ 对话框拖动期：实时更新 AudioPlaybackManager 的 EQ 滤波器（听感）。

        不进命令栈，参数变化时 DSP 系数立即重算。
        """
        mgr = self._get_mixer_audio_manager()
        if mgr is not None and hasattr(mgr, 'set_track_strip_eq'):
            try:
                mgr.set_track_strip_eq(track_id, params)
            except Exception as e:
                logger.debug("mixer strip eq preview failed: %s", e)

    def _on_mixer_strip_eq_committed(self, track_id: str, params) -> None:
        """Strip EQ 松手/关闭：提交 SetTrackStripEQCommand（一次 undo）。

        同时更新 AudioPlaybackManager 做实时听感（与命令栈解耦）。
        """
        # 实时听感
        mgr = self._get_mixer_audio_manager()
        if mgr is not None and hasattr(mgr, 'set_track_strip_eq'):
            try:
                mgr.set_track_strip_eq(track_id, params)
            except Exception as e:
                logger.debug("mixer strip eq apply failed: %s", e)

        # 提交命令
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            logger.warning("No timeline_view for strip eq commit")
            return
        try:
            from src.features.edit.core.commands import SetTrackStripEQCommand
            cmd = SetTrackStripEQCommand.create(track_id, params)
            tview._execute_command(cmd)
        except Exception as e:
            logger.warning("Failed to execute strip eq command: %s", e)

    # ── AM-08 阶段 4.2: Routing UI ──

    def _on_mixer_show_routing(self, track_id: str) -> None:
        """AM-08 阶段 4.2: 打开 Routing 对话框。

        从当前 timeline 读取该轨的 output_bus_id / sends，列出候选 Bus，
        打开非模态 RoutingDialog。
        """
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        try:
            sequence = tview.timeline
        except Exception:
            return
        track = sequence.get_track_by_id(track_id) if hasattr(sequence, 'get_track_by_id') else None
        if track is None:
            return
        # 候选 Bus 列表
        bus_tracks = [
            (t.track_id, t.name or t.track_id)
            for t in getattr(sequence, "audio_tracks", ())
            if getattr(t, "is_bus", False) and t.track_id != track_id
        ]
        # AUD-37810: 当前轨本身是 Bus 时也纳入图节点 —— 环检测只把
        # bus_tracks 中的轨道建为图节点，若正在编辑的 Bus 不在其中，
        # 经它构成的 Bus→Bus 环（如 B1→B2→B1 中正在编辑 B1）不可检出。
        if getattr(track, "is_bus", False):
            bus_tracks.append((track.track_id, track.name or track.track_id))
        # AUD-37810: 全量路由快照（其他轨道的真实路由）注入对话框校验图。
        # 此前不传时校验退化为仅含当前轨道的简化图：其余 Bus 的 output
        # 一律为 None，Bus 间既有环完全不可见。
        routing_snapshot = {
            t.track_id: {
                "output_bus_id": getattr(t, "output_bus_id", None),
                "sends": [
                    (s.target_bus_id, s.level)
                    for s in (getattr(t, "sends", ()) or ())
                    if getattr(s, "enabled", True)
                ],
                "is_bus": bool(getattr(t, "is_bus", False)),
            }
            for t in getattr(sequence, "audio_tracks", ())
        }
        from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog
        # 单例：已打开则前置
        existing = getattr(self, "_routing_dialog", None)
        if existing is not None:
            try:
                if existing.isVisible() and existing._track_id == track_id:
                    existing.raise_()
                    existing.activateWindow()
                    return
                existing.close()
                # AUD-2717: 旧实例 parent 是主窗口，只 close 不销毁时 C++
                # 对象作为子节点常驻到窗口销毁；反复开关 Routing 会累积。
                # close 后 deleteLater 在事件循环安全释放。
                existing.deleteLater()
            except RuntimeError:
                # isVisible 触达已销毁的 C++ 对象（wrapped C/C++ object
                # deleted）——按无旧实例处理并留痕，不再吞掉其他异常。
                logger.debug("old routing dialog already destroyed",
                             exc_info=True)
            self._routing_dialog = None
        dialog = RoutingDialog(
            track_id=track_id,
            track_name=getattr(track, "name", track_id),
            output_bus_id=getattr(track, "output_bus_id", None),
            sends=getattr(track, "sends", ()) or (),
            bus_tracks=bus_tracks,
            parent=self.window if hasattr(self, "window") else None,
            routing_snapshot=routing_snapshot,
        )
        dialog.routing_committed.connect(self._on_mixer_routing_committed)
        # E18: 拖动期实时听感预览（此前 routing_preview 全仓无接收者）。
        # 关闭对话框时把引擎恢复到最后一次提交（无提交则恢复打开前原路由）。
        dialog.routing_preview.connect(self._on_mixer_routing_preview)
        self._routing_dialog_baseline = {
            "output_bus_id": getattr(track, "output_bus_id", None),
            "sends": [
                (s.target_bus_id, s.level)
                for s in (getattr(track, "sends", ()) or ())
                if getattr(s, "enabled", True)
            ],
        }
        self._routing_dialog_preview_dirty = False
        dialog.finished.connect(self._on_mixer_routing_dialog_finished)
        self._routing_dialog = dialog
        dialog.show()

    def _build_routing_validation_graph(self, sequence, track_id: str,
                                        output_bus_id, sends) -> "tuple[list, dict]":
        """AUD-37810: 构建完整路由校验图（供提交门控复核）。

        以当前 timeline 的全部 audio_tracks 的真实路由为基础，最后写入
        当前轨道的拟定路由（覆盖其既有状态），返回
        (bus_ids, routing)，格式与 validate_routing_graph 入参一致。
        """
        bus_ids = [
            t.track_id for t in getattr(sequence, "audio_tracks", ())
            if getattr(t, "is_bus", False)
        ]
        routing: dict = {}
        for t in getattr(sequence, "audio_tracks", ()):
            routing[t.track_id] = {
                "output_bus_id": getattr(t, "output_bus_id", None),
                "sends": [
                    (s.target_bus_id, s.level)
                    for s in (getattr(t, "sends", ()) or ())
                    if getattr(s, "enabled", True)
                ],
                "is_bus": bool(getattr(t, "is_bus", False)),
            }
        # 当前轨的拟定路由最后写入（覆盖快照中该轨的旧状态）
        routing[track_id] = {
            "output_bus_id": output_bus_id,
            "sends": [
                (s.target_bus_id, s.level)
                for s in (sends or ())
                if getattr(s, "enabled", True)
            ],
            "is_bus": track_id in set(bus_ids),
        }
        return bus_ids, routing

    def _on_mixer_routing_preview(self, track_id: str, payload) -> None:
        """E18: Routing 拖动期实时听感预览（不进命令栈）。

        ``routing_committed`` 仍是唯一提交。预览与提交同样过 AUD-37810
        路由图校验——非法图（环/未知目标）不进引擎；对话框发射侧已
        拦截无效 payload，此处为防御纵深。
        """
        if not isinstance(payload, dict):
            return
        output_bus_id = payload.get("output_bus_id")
        new_sends = payload.get("sends", []) or []

        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        sequence = getattr(tview, 'timeline', None) if tview is not None else None
        if sequence is None:
            return
        try:
            from src.features.edit.core.audio_playback import validate_routing_graph
            bus_ids, routing = self._build_routing_validation_graph(
                sequence, track_id, output_bus_id, new_sends)
            is_valid, _graph_errors = validate_routing_graph(bus_ids, routing)
        except (ImportError, AttributeError, TypeError) as e:
            logger.debug("routing preview validation unavailable: %s", e)
            return
        if not is_valid:
            logger.debug("routing preview blocked (AUD-37810): invalid graph")
            return

        mgr = self._get_mixer_audio_manager()
        if mgr is not None and hasattr(mgr, 'set_track_routing'):
            try:
                sends_list = [(s.target_bus_id, s.level) for s in new_sends if getattr(s, "enabled", True)]
                mgr.set_track_routing(track_id, output_bus_id, sends_list, is_bus=False)
                self._routing_dialog_preview_dirty = True
            except Exception as e:
                logger.debug("routing preview apply failed: %s", e)

    def _on_mixer_routing_dialog_finished(self, _result: int) -> None:
        """E18: Routing 对话框关闭——回滚未提交的预览路由（引擎侧 only）。"""
        dialog = getattr(self, "_routing_dialog", None)
        dirty = getattr(self, "_routing_dialog_preview_dirty", False)
        baseline = getattr(self, "_routing_dialog_baseline", None)
        self._routing_dialog_preview_dirty = False
        self._routing_dialog_baseline = None
        if not dirty or baseline is None or dialog is None:
            return
        mgr = self._get_mixer_audio_manager()
        if mgr is not None and hasattr(mgr, 'set_track_routing'):
            try:
                mgr.set_track_routing(
                    dialog._track_id, baseline.get("output_bus_id"),
                    list(baseline.get("sends", [])), is_bus=False,
                )
            except Exception as e:
                logger.debug("routing preview rollback failed: %s", e)

    def _on_mixer_routing_committed(self, track_id: str, payload) -> None:
        """AM-08 阶段 4.2: Routing 提交（output_bus + sends 整体替换）。

        实时听感：mgr.set_track_routing（与命令栈解耦）。
        命令栈：SetTrackOutputBusCommand + AddSendCommand/RemoveSendCommand/UpdateSendCommand。
        AUD-37810: 提交前以完整路由图复核 —— 校验失败（路由环/未知目标）
        必须阻止提交（实时听感与命令栈都不落），不得静默通过。
        """
        if not isinstance(payload, dict):
            return
        output_bus_id = payload.get("output_bus_id")
        new_sends = payload.get("sends", []) or []

        # E18: 提交后基线更新为提交值——此后关闭对话框不再回滚到
        # 对话框打开前的旧路由。
        try:
            self._routing_dialog_baseline = {
                "output_bus_id": output_bus_id,
                "sends": [
                    (s.target_bus_id, s.level)
                    for s in new_sends if getattr(s, "enabled", True)
                ],
            }
            self._routing_dialog_preview_dirty = False
        except Exception:  # noqa: BLE001 基线更新失败不影响提交链路
            logger.debug("routing baseline update failed", exc_info=True)

        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            return
        sequence = getattr(tview, 'timeline', None)
        if sequence is None:
            return

        # AUD-37810 消费端提交门控（防御纵深）：发射侧 RoutingDialog 已按
        # 校验结果阻止发射，此处再以完整图复核，保证任何发射路径都无法
        # 把非法路由写入引擎或命令栈。
        try:
            from src.features.edit.core.audio_playback import validate_routing_graph
            bus_ids, routing = self._build_routing_validation_graph(
                sequence, track_id, output_bus_id, new_sends)
            is_valid, graph_errors = validate_routing_graph(bus_ids, routing)
        except (ImportError, AttributeError, TypeError) as e:
            logger.warning("Routing validation unavailable, commit blocked: %s", e)
            return
        if not is_valid:
            logger.warning(
                "Routing commit blocked (AUD-37810): %s", "; ".join(graph_errors))
            status_bar = getattr(self, 'status_bar', None)
            if status_bar is not None:
                status_bar.showMessage(I18N.tr(
                    "warning.audio_routing_commit_blocked",
                    "Routing change rejected: routing cycle or unknown bus target."
                ), 5000)
            return

        # 实时听感
        mgr = self._get_mixer_audio_manager()
        if mgr is not None and hasattr(mgr, 'set_track_routing'):
            try:
                sends_list = [(s.target_bus_id, s.level) for s in new_sends if getattr(s, "enabled", True)]
                mgr.set_track_routing(track_id, output_bus_id, sends_list, is_bus=False)
            except Exception as e:
                logger.debug("routing apply failed: %s", e)

        # 命令栈：先 output_bus，再 sends（diff 当前 vs 新）
        try:
            from src.features.edit.core.commands import (
                SetTrackOutputBusCommand, AddSendCommand, RemoveSendCommand, UpdateSendCommand,
            )
            track = sequence.get_track_by_id(track_id)
            if track is None:
                return

            # output_bus 变更
            if getattr(track, "output_bus_id", None) != output_bus_id:
                cmd = SetTrackOutputBusCommand.create(track_id, output_bus_id)
                tview._execute_command(cmd)
                # 重新获取 track（命令执行后 timeline 已变）
                track = tview.timeline.get_track_by_id(track_id)

            # sends diff：以 target_bus_id 为 key 比较（简化：整体替换）
            old_sends = {s.target_bus_id: s for s in (track.sends or ())}
            new_sends_map = {s.target_bus_id: s for s in new_sends}

            # 删除不再存在的
            for target, old_s in old_sends.items():
                if target not in new_sends_map:
                    cmd = RemoveSendCommand.create(track_id, old_s.send_id)
                    tview._execute_command(cmd)
            # 重新获取
            track = tview.timeline.get_track_by_id(track_id)
            old_sends = {s.target_bus_id: s for s in (track.sends or ())}

            # 添加新的 + 更新已变化的
            for target, new_s in new_sends_map.items():
                if target not in old_sends:
                    cmd = AddSendCommand.create(track_id, target, level=float(new_s.level))
                    tview._execute_command(cmd)
                else:
                    old_s = old_sends[target]
                    if (abs(old_s.level - new_s.level) > 1e-6
                            or old_s.enabled != new_s.enabled):
                        cmd = UpdateSendCommand.create(
                            track_id, old_s.send_id,
                            level=float(new_s.level),
                            enabled=bool(new_s.enabled),
                        )
                        tview._execute_command(cmd)
        except Exception as e:
            logger.warning("Failed to commit routing: %s", e)

    def _on_mixer_effects_preview(self, track_id: str, chain) -> None:
        """W26: FX 机架拖动期：实时更新 AudioPlaybackManager 的效果器链（听感）。

        不进命令栈，参数变化时 DSP 立即重算。
        """
        mgr = self._get_mixer_audio_manager()
        if mgr is not None and hasattr(mgr, 'set_track_effect_chain'):
            try:
                mgr.set_track_effect_chain(track_id, chain)
            except Exception as e:
                logger.debug("mixer effects preview failed: %s", e)

    def _on_mixer_effects_committed(self, track_id: str, chain) -> None:
        """W26: FX 机架松手/关闭：提交 SetTrackEffectChainCommand（一次 undo）。

        同时更新 AudioPlaybackManager 做实时听感（与命令栈解耦）。
        """
        # 实时听感
        mgr = self._get_mixer_audio_manager()
        if mgr is not None and hasattr(mgr, 'set_track_effect_chain'):
            try:
                mgr.set_track_effect_chain(track_id, chain)
            except Exception as e:
                logger.debug("mixer effects apply failed: %s", e)

        # 提交命令
        tview = getattr(getattr(self, 'timeline_panel', None), 'timeline_view', None)
        if tview is None:
            logger.warning("No timeline_view for effects commit")
            return
        try:
            from src.features.edit.core.commands import SetTrackEffectChainCommand
            cmd = SetTrackEffectChainCommand.create(track_id, chain)
            tview._execute_command(cmd)
        except Exception as e:
            logger.warning("Failed to execute effects command: %s", e)

    # E17: _on_mixer_purge_cache 已删除——信号全仓无 emit、处理器在
    # PURGE_CACHE_IMPLEMENTED=False 下仅记日志（RMA-12 no-op）。缓存占用
    # 的可见性由音频健康报告承担；真正清理待实现时经新入口接入。

    def _on_edit_clips_param_batch_changed(self, batch: list) -> None:
        """RI-05/R16：多选批量参数——锁预检 + 单宏一次提交。

        batch: [(clip_id, param_type, param_path, value)]，value 已按各
        片自身原值派生（面板侧）。任一目标轨锁定整组拒绝（不半提交）；
        speed 组复用 ChangeClipSpeedCommand 统一 retime 语义。
        """
        if not batch:
            return
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        _cmds = get_command_classes()
        _params = get_clip_params_classes()
        if _cmds is None or _params is None:
            return
        UpdateClipParamCommand = _cmds["UpdateClipParamCommand"]
        ChangeClipSpeedCommand = _cmds.get("ChangeClipSpeedCommand")
        SpeedParams = _params["SpeedParams"]

        timeline = self.edit_page._current_timeline
        # 锁预检：任一目标轨锁定 → 整组拒绝
        from src.features.edit.core.commands import assert_track_editable

        for clip_id, _group, _path, _value in batch:
            clip = timeline.get_clip_by_id(clip_id)
            if clip is None:
                continue
            try:
                assert_track_editable(timeline, clip.track_id, "Batch param")
            except ValueError as exc:
                logger.info("Batch clip param rejected: %s", exc)
                # N09/T3-01：预检拒绝同样从 canonical 恢复控件假值。
                self._restore_properties_panel_from_timeline(
                    timeline, [c for c, _, _, _ in batch])
                return

        commands = []
        batch_clip_ids: list = []
        for clip_id, group, path, value in batch:
            clip = timeline.get_clip_by_id(clip_id)
            if clip is None:
                continue
            if group == "speed" and ChangeClipSpeedCommand is not None:
                current_speed = (
                    clip.speed if isinstance(clip.speed, SpeedParams)
                    else SpeedParams(speed=float(clip.speed))
                )
                if value == current_speed:
                    continue  # no-op：同值不产生历史/revision
                commands.append(ChangeClipSpeedCommand(
                    command_id=str(uuid.uuid4()),
                    label=f"Update speed.{path}",
                    clip_id=clip_id,
                    track_id=clip.track_id,
                    new_speed=value,
                ))
                batch_clip_ids.append(clip_id)
                continue
            old_value = {
                "transform": lambda c: c.transform,
                "crop": lambda c: c.crop,
                "opacity": lambda c: c.opacity,
                "raw": lambda c: c.raw,
                # N11/T3-03：facade 缺命令回退时 speed 也必须携带完整
                # 旧 SpeedParams——此前 None 使 Undo 读"当前 speed"当旧
                # 值，历史弹掉而内容不恢复（命令层已在 apply 兜底捕获，
                # 这里显式传值保持载荷完整）。
                "speed": lambda c: (
                    c.speed if isinstance(c.speed, SpeedParams)
                    else SpeedParams(speed=float(c.speed))
                ),
            }.get(group, lambda c: None)(clip)
            if old_value is not None and value == old_value:
                continue  # no-op：同值不产生历史/revision
            commands.append(UpdateClipParamCommand(
                command_id=str(uuid.uuid4()),
                label=f"Update {group}.{path}",
                track_id=clip.track_id,
                clip_id=clip_id,
                param_type=group,
                new_value=value,
                old_value=old_value,
            ))
            batch_clip_ids.append(clip_id)
        if not commands:
            return
        # N09/T3-01：批量提交的领域拒绝同样在信号边界收敛，不冒成
        # 未捕获 Qt 异常；被拒后面板从 canonical（未变）恢复。
        try:
            if len(commands) == 1:
                timeline = self._execute_timeline_edit_command(
                    commands[0], timeline
                )
            else:
                MacroCommand = _cmds.get("MacroCommand")
                macro = MacroCommand.create("Batch clip param", commands)
                timeline = self._execute_timeline_edit_command(macro, timeline)
        except ValueError as exc:
            logger.info("Batch clip param rejected: %s", exc)
            self._restore_properties_panel_from_timeline(
                timeline, batch_clip_ids)
            return
        self.edit_page._current_timeline = timeline
        # N12/T3-04：提交成功后按 stable ID 从 canonical 刷新面板全部
        # 快照与控件——此前面板留旧快照，下一个字段以旧值为基派生，
        # 静默覆盖刚提交的字段（X→Y 后 X 回原值）。
        self._restore_properties_panel_from_timeline(
            timeline, batch_clip_ids)

    def _on_edit_reset_transform(self, clip_ids: list) -> None:
        """
        Handle reset transform request for multiple clips.

        Args:
            clip_ids: List of clip IDs to reset transform
        """
        logger.debug(f"_on_edit_reset_transform: {clip_ids}")

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return

        _params = get_clip_params_classes()
        _cmds = get_command_classes()
        if _params is None or _cmds is None:
            return
        TransformParams = _params["TransformParams"]
        UpdateClipParamCommand = _cmds["UpdateClipParamCommand"]

        timeline = self.edit_page._current_timeline
        default_transform = TransformParams()

        # RI-05/R16：锁预检 + 单宏一次提交——此前逐片命令多次提交，
        # 一次 Undo 只恢复一片；选区含锁轨时整组拒绝（不半提交）。
        from src.features.edit.core.commands import assert_track_editable

        commands = []
        for clip_id in clip_ids:
            clip = timeline.get_clip_by_id(clip_id)
            if not clip:
                continue
            try:
                assert_track_editable(timeline, clip.track_id, "Reset transform")
            except ValueError as exc:
                logger.info("Batch reset rejected: %s", exc)
                return
            commands.append(UpdateClipParamCommand.create_transform(
                track_id=clip.track_id,
                clip_id=clip_id,
                new_transform=default_transform,
                old_transform=clip.transform
            ))
        if not commands:
            return
        # N09/T3-01：领域拒绝在信号边界收敛（锁轨等 ValueError 不再冒
        # 成未捕获 Qt 异常），被拒后整组零提交。
        try:
            if len(commands) == 1:
                timeline = self._execute_timeline_edit_command(
                    commands[0], timeline
                )
            else:
                MacroCommand = _cmds.get("MacroCommand")
                macro = MacroCommand.create("Reset transform", commands)
                timeline = self._execute_timeline_edit_command(macro, timeline)
        except ValueError as exc:
            logger.info("Reset transform rejected: %s", exc)
            self._restore_properties_panel_from_timeline(timeline, clip_ids)
            return

        # The helper publishes each committed timeline to the edit page,
        # the timeline view and the preview renderer (view path via
        # execute_command's signal chain; fallback via
        # _sync_timeline_to_views) — no extra full reload here.
        self.edit_page._current_timeline = timeline

        # N14/T3-06：Reset 后按完整 SelectionContext 刷新 Inspector——
        # 此前 _show_clip_in_properties(first) 把面板收缩成单选，时间线
        # 仍是多选，下一个共享字段只写 primary。刷新保留全部 IDs 与
        # primary 顺序，多选标题/上下文一致。
        if clip_ids:
            clips = [
                c for c in (
                    timeline.get_clip_by_id(cid) for cid in clip_ids
                ) if c is not None
            ]
            if clips:
                panel = getattr(self, 'edit_properties_panel', None)
                set_clips = getattr(panel, 'set_clips', None)
                fps = getattr(getattr(self, 'edit_page', None), '_fps', 24.0)
                if callable(set_clips):
                    try:
                        set_clips(clips, fps)
                    except TypeError:
                        for clip in clips:
                            self._show_clip_in_properties(clip)
                            break
                else:
                    self._show_clip_in_properties(clips[0])

        # The reset path bypasses the normal per-parameter signal handler.
        # Push the new default TransformParams explicitly, otherwise the
        # preview image resets while ProgramViewer keeps its old handle
        # geometry until the clip is reselected.
        viewer = getattr(self.edit_page, '_program_viewer', None)
        is_tool_active = getattr(viewer, 'is_transform_tool_active', None)
        if viewer is not None and callable(is_tool_active) and is_tool_active():
            self._push_selected_clip_to_transform_viewer()

        # Sync renderer so the reset transform is visible immediately.
        if self.timeline_preview_renderer is not None:
            silent = getattr(self.timeline_preview_renderer,
                             'update_sequence_silent', None)
            if callable(silent):
                silent(timeline)
            else:
                self.timeline_preview_renderer.set_sequence(timeline)

        self._request_program_viewer_update()

    def _show_clip_in_properties(self, clip) -> None:
        """Show a clip in the properties panel with generator metadata resolved.

        Resolves the clip's asset ``media_type`` (and, for Solid generators,
        the current colour and full generator params) from the active project
        so the panel can tailor its sections (skip Transform/Crop for adjustment,
        show full generator UI for solid).
        Falls back to the plain ``set_clip`` call when the project/asset is
        unavailable.
        """
        media_type = None
        solid_color = None
        generator_params = None
        asset = None
        project = getattr(self, '_current_edit_project', None)
        if project is not None and getattr(clip, 'asset_id', ''):
            asset = project.assets.get(clip.asset_id)
            if asset is not None:
                media_type = getattr(asset, 'media_type', None)
                sc = getattr(asset, 'solid_color', None)
                if sc is not None:
                    solid_color = sc
                gp = getattr(asset, 'generator_params', None)
                if gp is not None:
                    generator_params = gp
        try:
            self.edit_properties_panel.set_clip(
                clip, self.edit_page._fps,
                media_type=media_type, solid_color=solid_color,
                generator_params=generator_params,
            )
        except TypeError:
            # Older panel signature without the new kwargs — fall back.
            self.edit_properties_panel.set_clip(clip, self.edit_page._fps)
            return

        info_panel = getattr(self, "edit_info_panel", None)
        if info_panel is not None:
            info_panel.set_clip(clip, asset if project is not None else None, self.edit_page._fps)

    def _show_clip_in_info(self, clip) -> None:
        """Refresh the Edit Info dock for the current timeline clip."""
        info_panel = getattr(self, "edit_info_panel", None)
        if info_panel is None:
            return
        project = getattr(self, "_current_edit_project", None)
        asset = project.get_asset_by_id(clip.asset_id) if project is not None else None
        info_panel.set_clip(clip, asset, self.edit_page._fps)

    def _clear_edit_info(self) -> None:
        """Clear media metadata when the selection is no longer a clip/asset."""
        info_panel = getattr(self, "edit_info_panel", None)
        if info_panel is not None:
            info_panel.clear()

    def _show_timeline_in_info(self, timeline) -> None:
        """Refresh the Edit Info dock for the selected timeline."""
        info_panel = getattr(self, "edit_info_panel", None)
        if info_panel is not None:
            info_panel.set_timeline(timeline)

    def _on_media_pool_timeline_selected(self, timeline_id: str) -> None:
        """Show a Media Pool timeline's settings without switching timelines."""
        project = getattr(self, "_current_edit_project", None)
        if project is None:
            self._clear_edit_info()
            return

        timeline = getattr(self, "_current_timeline_sequences", {}).get(timeline_id)
        current_timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
        if timeline is None and current_timeline is not None:
            if getattr(current_timeline, "timeline_id", None) == timeline_id:
                timeline = current_timeline
        if timeline is None:
            timeline = project.get_timeline_by_id(timeline_id)

        if timeline is None:
            self._clear_edit_info()
            return
        self._show_timeline_in_info(timeline)

    def _on_edit_generator_color_changed(self, clip_id: str, asset_id: str, rgb: object) -> None:
        """Update a Solid Color generator's colour on its asset.

        The colour lives on the MediaAsset (metadata["color"]), so we rebuild
        the asset via the project and propagate the new project to the
        timeline view + media pool so all consumers stay in sync.
        """
        project = getattr(self, '_current_edit_project', None)
        if project is None or not asset_id:
            return
        try:
            r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
        except Exception:  # noqa: BLE001
            return
        asset = project.assets.get(asset_id)
        if asset is None:
            return
        new_meta = dict(asset.metadata or {})
        new_meta["color"] = [r, g, b]
        # MediaAsset is frozen; rebuild via dataclasses.replace so fields
        # added after the original set (keywords/rating/flag/color_label/
        # comment, ...) are carried over instead of silently reset (AUD-2702).
        new_asset = replace(asset, metadata=new_meta)
        new_assets = dict(project.assets)
        new_assets[asset_id] = new_asset
        import time as _time
        # AUD-2701: dataclasses.replace instead of a manual constructor —
        # the constructor dropped smart_bins and reset revision to 0 on
        # every generator colour edit.
        new_project = replace(
            project,
            assets=new_assets,
            modified_at=_time.time(),
        )
        self._current_edit_project = new_project
        # Propagate to timeline view + media pool so swatches/previews refresh.
        if hasattr(self, 'timeline_panel') and hasattr(self.timeline_panel, 'timeline_view'):
            self.timeline_panel.timeline_view.set_project(new_project)
            self.timeline_panel.timeline_view.project_changed.emit(new_project)
        # Trigger a viewer refresh so the solid colour updates immediately.
        self._request_program_viewer_update()

    def _on_edit_generator_params_changed(self, clip_id: str, asset_id: str, params: object) -> None:
        """Update a Solid Color generator's full parameter set on its asset.

        Persists the complete generator params (mode, colors, checker/gradient/
        radial settings) to ``asset.metadata["generator_params"]`` and keeps
        ``metadata["color"]`` in sync for backward compatibility with the
        legacy solid_color property and existing render paths.
        """
        project = getattr(self, '_current_edit_project', None)
        if project is None or not asset_id:
            return
        if not isinstance(params, dict):
            return
        asset = project.assets.get(asset_id)
        if asset is None:
            return
        new_meta = dict(asset.metadata or {})
        # Store full generator params
        new_meta["generator_params"] = dict(params)
        # Keep legacy "color" in sync (primary color RGB)
        new_meta["color"] = [
            float(params.get("color_r", 0.0)),
            float(params.get("color_g", 0.0)),
            float(params.get("color_b", 0.0)),
        ]
        # MediaAsset is frozen; rebuild via dataclasses.replace so fields
        # added after the original set (keywords/rating/flag/color_label/
        # comment, ...) are carried over instead of silently reset (AUD-2702).
        new_asset = replace(asset, metadata=new_meta)
        new_assets = dict(project.assets)
        new_assets[asset_id] = new_asset
        import time as _time
        # AUD-2701: dataclasses.replace instead of a manual constructor —
        # the constructor dropped smart_bins and reset revision to 0 on
        # every generator parameter edit.
        new_project = replace(
            project,
            assets=new_assets,
            modified_at=_time.time(),
        )
        self._current_edit_project = new_project
        # Update the panel's cached generator_params so subsequent rebuilds
        # (e.g. mode change) read the latest state.
        self.edit_properties_panel._current_generator_params = dict(params)
        # Propagate to timeline view + media pool so swatches/previews refresh.
        if hasattr(self, 'timeline_panel') and hasattr(self.timeline_panel, 'timeline_view'):
            tv = self.timeline_panel.timeline_view
            tv.set_project(new_project)
            tv.project_changed.emit(new_project)
            # Directly update the clip item's swatch colour so the timeline
            # reflects the new primary colour without a full rebuild.
            item = tv._items_map.get(clip_id)
            new_sc = (
                float(params.get("color_r", 0.0)),
                float(params.get("color_g", 0.6)),
                float(params.get("color_b", 0.8)),
            )
            if item is not None and hasattr(item, '_solid_color'):
                item._solid_color = new_sc
                item._media_type = "solid"
                item.update()
                # Force scene-level repaint in case item.update() is not enough
                if item.scene() is not None:
                    item.scene().update()
        # Trigger a viewer refresh so the generator output updates immediately.
        self._request_program_viewer_update()

    def _on_edit_transition_param_changed(
        self, transition_id: str, param_name: str, value: object
    ) -> None:
        """Commit a native transition inspector change through undo/redo."""
        if self.edit_page is None or not self.edit_page._current_timeline:
            logger.warning("No current timeline for transition param update")
            return

        timeline = self.edit_page._current_timeline
        transition = self._find_transition_by_id(timeline, transition_id)
        if transition is None:
            logger.warning("Transition not found: %s", transition_id)
            return

        commands = get_command_classes()
        if not commands:
            return
        UpdateTransitionCommand = commands["UpdateTransitionCommand"]

        try:
            if param_name == "transition_type":
                transition_type = str(value)
                from src.features.edit.core.transition_catalog import TRANSITION_TYPE_OPTIONS
                from src.features.edit.core.audio_transition_catalog import (
                    AUDIO_TRANSITION_TYPE_OPTIONS,
                )
                if (
                    transition_type not in TRANSITION_TYPE_OPTIONS
                    and transition_type not in AUDIO_TRANSITION_TYPE_OPTIONS
                ):
                    logger.warning("Unsupported native transition type: %s", transition_type)
                    return
                command = UpdateTransitionCommand.create_type(
                    timeline, transition_id, transition.track_id, transition_type
                )
            elif param_name == "duration":
                command = UpdateTransitionCommand.create_duration(
                    timeline, transition_id, transition.track_id, max(1, int(value))
                )
            elif param_name == "alignment":
                alignment = str(value)
                if alignment not in {"centered", "start_on_cut", "end_on_cut"}:
                    return
                command = UpdateTransitionCommand(
                    command_id=str(uuid.uuid4()),
                    label="Change transition alignment",
                    transition_id=transition_id,
                    track_id=transition.track_id,
                    new_alignment=alignment,
                    _old_transition=transition,
                )
            else:
                params = dict(transition.params or {})
                if param_name == "curve":
                    if str(value) not in {"linear", "ease_in", "ease_out", "ease_in_out"}:
                        return
                    params["curve"] = str(value)
                elif param_name == "direction":
                    if str(value) not in {"left", "right", "up", "down"}:
                        return
                    params["direction"] = str(value)
                elif param_name == "softness":
                    params["softness"] = max(0.0, min(0.5, float(value)))
                else:
                    return
                command = UpdateTransitionCommand.create_params(
                    timeline, transition_id, transition.track_id, params
                )
        except (TypeError, ValueError) as exc:
            logger.warning("Transition parameter update rejected: %s", exc)
            return

        # AUD-2707: commit through the TimelineView command stack (undo +
        # view/renderer sync are handled by the helper's two paths).
        self._execute_timeline_edit_command(command, timeline)

        self._request_program_viewer_update()

    def _on_edit_effect_param_changed(
        self, clip_id: str, effect_id: str, param_name: str, value: object
    ) -> None:
        """
        Handle effect parameter change from EditPropertiesPanel.

        Args:
            clip_id: Clip ID containing the effect
            effect_id: Effect ID to update
            param_name: Parameter name (e.g., 'intensity', 'radius')
            value: New parameter value
        """
        logger.debug(f"_on_edit_effect_param_changed: {clip_id}/{effect_id}.{param_name} = {value}")

        # Get current timeline from edit_page
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for effect param update")
            return

        timeline = self.edit_page._current_timeline

        # Find the clip to get its track_id and old value
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        track_id = clip.track_id

        # Get the effect and old value
        effect = clip.get_effect_by_id(effect_id)
        if not effect:
            logger.warning(f"Effect not found: {effect_id}")
            return

        # R-001: Delayed import for EffectInstance via facade
        EffectInstance = get_effect_instance_class()

        # Get old value for undo
        old_value = None
        if EffectInstance is not None and isinstance(effect, EffectInstance):
            old_value = effect.params.get(param_name)
        elif isinstance(effect, dict):
            old_value = effect.get("params", {}).get(param_name)

        # Import command via facade
        _cmds = get_command_classes()
        if _cmds is None:
            return
        UpdateEffectParamCommand = _cmds["UpdateEffectParamCommand"]

        # Create command
        command = UpdateEffectParamCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            effect_id=effect_id,
            param_name=param_name,
            new_value=value,
            old_value=old_value
        )

        # AUD-2707: EditPage has no ``_command_stack`` — commit through the
        # TimelineView command stack instead. The helper also syncs the
        # mutated timeline into the preview renderer + TimelineView (each
        # holds its own copy; see _sync_timeline_to_views for why).
        self._execute_timeline_edit_command(command, timeline)

        # FX-H02: an effect param change alters the rendered output, so the
        # clip's render cache must be marked stale (else the viewer keeps the
        # pre-edit frame).
        self._invalidate_clip_render_cache(clip_id)

        # NOTE: deliberately do NOT rebuild the properties panel here.
        # The slider/spinbox the user is dragging lives in the current
        # panel; calling set_clip() would tear it down mid-drag and
        # abort the interaction (the original "params won't drag" bug).
        # The widget already shows the value the user just set; the only
        # thing left is to refresh the viewer, which the next line does.

        # P-clip-FX: 若被拖动的是 mixer audio 效果参数，重同步音频引擎
        # （effect 命令路径不自动触发 audio signature 检测）。
        if effect is not None:
            _et = getattr(effect, "effect_type", "")
            from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES
            if _et in AUDIO_MIXER_EFFECT_TYPES:
                self._resync_audio_after_effect_change()

        # Request program viewer update
        self._request_program_viewer_update()

    def _on_edit_effect_params_changed(
        self, clip_id: str, effect_id: str, changes: list
    ) -> None:
        """Batched effect param change → single MacroCommand (one undo step).

        Fired by X/Y chain-linked rows (e.g. blur radius_x/radius_y) where one
        user drag should update two params atomically. ``changes`` is a list
        of ``(param_name, value)`` tuples; each becomes an
        ``UpdateEffectParamCommand`` and the set is wrapped in a MacroCommand
        so Ctrl+Z reverts the whole drag at once.

        Args:
            clip_id: Clip ID containing the effect.
            effect_id: Effect ID to update.
            changes: List of ``(param_name, new_value)`` tuples.
        """
        if not changes:
            return

        logger.debug(
            f"_on_edit_effect_params_changed: {clip_id}/{effect_id} "
            f"{[(n, v) for n, v in changes]}"
        )

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for batched effect param update")
            return

        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        track_id = clip.track_id
        effect = clip.get_effect_by_id(effect_id)
        if not effect:
            logger.warning(f"Effect not found: {effect_id}")
            return

        EffectInstance = get_effect_instance_class()
        _cmds = get_command_classes()
        if _cmds is None:
            return
        UpdateEffectParamCommand = _cmds["UpdateEffectParamCommand"]
        MacroCommand = _cmds["MacroCommand"]

        # Build one atomic sub-command per changed param, capturing each old
        # value against the (mutating) live effect so undo restores correctly.
        sub_commands = []
        params_src = (
            effect.params
            if (EffectInstance is not None and isinstance(effect, EffectInstance))
            else effect.get("params", {})
        )
        for param_name, new_value in changes:
            old_value = params_src.get(param_name) if params_src else None
            sub_commands.append(UpdateEffectParamCommand.create(
                track_id=track_id,
                clip_id=clip_id,
                effect_id=effect_id,
                param_name=param_name,
                new_value=new_value,
                old_value=old_value,
            ))

        macro = MacroCommand.create(
            label=I18N.tr("edit.command.update_effect_params", "Update effect parameters"),
            commands=sub_commands,
        )

        # AUD-2707: EditPage has no ``_command_stack`` — the old hasattr
        # branch was dead code.  Commit through the TimelineView command
        # stack; the helper's fallback unpacks MacroCommand's
        # ``(new_timeline, updated_command)`` tuple and syncs the mutated
        # timeline into the preview renderer + TimelineView (each holds its
        # own copy; see _sync_timeline_to_views for why).
        self._execute_timeline_edit_command(macro, timeline)

        # FX-H02: invalidate the clip's render cache so the viewer no
        # longer shows the pre-edit frame after this effect change.
        self._invalidate_clip_render_cache(clip_id)

        # See NOTE in _on_edit_effect_param_changed: do not rebuild the panel
        # mid-drag — the chain row keeps both axes' displays in sync at the
        # widget layer. Only refresh the viewer.

        # P-clip-FX: mixer audio effect params need an audio resync.
        _et = getattr(effect, "effect_type", "")
        from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES
        if _et in AUDIO_MIXER_EFFECT_TYPES:
            self._resync_audio_after_effect_change()

        self._request_program_viewer_update()

    def _on_edit_clip_keyframe_toggled(
        self, clip_id: str, param_name: str, frame: int, value: float,
    ) -> None:
        """Handle clip-level keyframe toggle from the properties panel (E02).

        ``EditPropertiesPanel.keyframe_toggled`` 一直只 emit 没有宿主接收；
        本槽复用效果关键帧相同的命令栈、缓存失效和面板刷新流程，但把
        关键帧写到片段自身的 ``animated_params``（不带 effect_id）。
        """
        logger.debug(
            f"_on_edit_clip_keyframe_toggled: {clip_id}.{param_name} "
            f"@F{frame}={value}"
        )

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for clip keyframe toggle")
            return

        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        track_id = clip.track_id

        has_kf = (hasattr(clip, "has_keyframe_at")
                  and clip.has_keyframe_at(param_name, frame))

        _cmds = get_command_classes()
        if _cmds is None:
            self._show_edit_status_message("编辑命令模块不可用")
            return
        remove_kf_cls = _cmds.get("RemoveKeyframeCommand")
        add_kf_cls = _cmds.get("AddKeyframeCommand")
        if has_kf:
            if remove_kf_cls is None:
                self._show_edit_status_message(
                    "关键帧命令不可用：RemoveKeyframeCommand 未注册"
                )
                return
            command = remove_kf_cls.create(
                track_id=track_id, clip_id=clip_id,
                param_name=param_name, frame=frame,
            )
        else:
            kf_value = value
            if kf_value is None:
                kf_value = clip.get_param_at_frame(param_name, frame)
            if add_kf_cls is None:
                self._show_edit_status_message(
                    "关键帧命令不可用：AddKeyframeCommand 未注册"
                )
                return
            command = add_kf_cls.create(
                track_id=track_id, clip_id=clip_id,
                param_name=param_name, frame=frame, value=float(kf_value),
            )

        try:
            new_timeline = self._execute_timeline_edit_command(command, timeline)
        except (TimelineOperationError, ValueError) as exc:
            logger.warning("Clip keyframe toggle rejected for %s: %s", clip_id, exc)
            self._show_edit_status_message(f"关键帧操作失败：{exc}")
            return
        if new_timeline is None:
            return

        self._invalidate_clip_render_cache(clip_id)
        new_clip = new_timeline.get_clip_by_id(clip_id)
        if new_clip is not None:
            self.edit_properties_panel.update_current_clip(new_clip)
        self._request_program_viewer_update()

    def _on_edit_animated_params_edited(
        self, clip_id: str, edits: list, frame: int,
    ) -> None:
        """R10（第七轮审计）：已动画参数的数值编辑 → 当前帧关键帧更新。

        旧行为只写静态 base（UpdateClipParamCommand），关键帧求值覆盖
        静态值——用户在 0→100 动画中输入 100 渲染仍为 0。专业剪辑语义：
        参数已动画时，数值编辑 = 在播放头帧更新/插入关键帧
        （``KeyframeTrack.add_keyframe`` 同帧原位替换）。多条目
        （scale 锁联动双轴）合并为单次 Undo 的 MacroCommand。
        """
        if not edits:
            return
        logger.debug(
            f"_on_edit_animated_params_edited: {clip_id}.{edits} @F{frame}"
        )
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for animated param edit")
            return
        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        _cmds = get_command_classes()
        add_kf_cls = _cmds.get("AddKeyframeCommand") if _cmds else None
        if add_kf_cls is None:
            self._show_edit_status_message(
                "关键帧命令不可用：AddKeyframeCommand 未注册")
            return
        sub_commands = [
            add_kf_cls.create(
                track_id=clip.track_id, clip_id=clip_id,
                param_name=str(param_name), frame=int(frame),
                value=float(value),
            )
            for param_name, value in edits
        ]
        if len(sub_commands) == 1:
            command = sub_commands[0]
        else:
            macro_cls = _cmds.get("MacroCommand")
            if macro_cls is None:
                self._show_edit_status_message(
                    "关键帧命令不可用：MacroCommand 未注册")
                return
            command = macro_cls.create(
                label="Update animated parameters", commands=sub_commands)
        try:
            new_timeline = self._execute_timeline_edit_command(command, timeline)
        except (TimelineOperationError, ValueError) as exc:
            logger.warning(
                "Animated param edit rejected for %s: %s", clip_id, exc)
            self._show_edit_status_message(f"动画参数编辑失败：{exc}")
            return
        if new_timeline is None:
            return
        self._invalidate_clip_render_cache(clip_id)
        new_clip = new_timeline.get_clip_by_id(clip_id)
        if new_clip is not None:
            self.edit_properties_panel.update_current_clip(new_clip)
        self._request_program_viewer_update()

    def _on_edit_effect_keyframe_toggled(
        self, clip_id: str, effect_id: str, param_name: str,
        frame: int, value: float,
    ) -> None:
        """Handle per-effect keyframe toggle from the properties panel.

        Toggle semantics: if a keyframe already exists at ``frame`` for
        this (effect, param) it is removed; otherwise one is added with
        the current value. The command carries ``effect_id`` so
        ``AddKeyframeCommand`` / ``RemoveKeyframeCommand`` write to
        ``EffectInstance.animated_params`` (not the clip's).

        Args:
            clip_id: Clip containing the effect.
            effect_id: Effect whose parameter is being animated.
            param_name: Effect parameter name (e.g. 'radius').
            frame: Timeline frame for the keyframe.
            value: Parameter value at that frame (from the ◆ button's
                last known slider value).
        """
        logger.debug(
            f"_on_edit_effect_keyframe_toggled: {clip_id}/{effect_id}."
            f"{param_name} @F{frame}={value}"
        )

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for effect keyframe toggle")
            return

        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        effect = clip.get_effect_by_id(effect_id)
        if effect is None:
            logger.warning(f"Effect not found: {effect_id}")
            return

        track_id = clip.track_id

        # Toggle direction: existing keyframe at frame → remove; else add.
        has_kf = (hasattr(effect, "has_keyframe_at")
                  and effect.has_keyframe_at(param_name, frame))

        _cmds = get_command_classes()
        if _cmds is None:
            return

        # E02: facade 缺失时结构化诊断，不把 KeyError 抛进 Qt 槽。
        remove_kf_cls = _cmds.get("RemoveKeyframeCommand") if _cmds else None
        add_kf_cls = _cmds.get("AddKeyframeCommand") if _cmds else None
        if has_kf:
            if remove_kf_cls is None:
                self._show_edit_status_message(
                    "关键帧命令不可用：RemoveKeyframeCommand 未注册"
                )
                return
            command = remove_kf_cls.create(
                track_id=track_id, clip_id=clip_id,
                param_name=param_name, frame=frame,
                effect_id=effect_id,
            )
        else:
            # Use the effect's current value at this frame if the button
            # didn't capture a meaningful one (e.g. fresh param). The
            # static params dict is the source of truth when not animated.
            kf_value = value
            if kf_value is None:
                kf_value = effect.get_param_at_frame(param_name, frame)
            if add_kf_cls is None:
                self._show_edit_status_message(
                    "关键帧命令不可用：AddKeyframeCommand 未注册"
                )
                return
            command = add_kf_cls.create(
                track_id=track_id, clip_id=clip_id,
                param_name=param_name, frame=frame, value=float(kf_value),
                effect_id=effect_id,
            )

        # AUD-2707: commit through the TimelineView command stack (helper
        # handles undo entry + preview renderer/TimelineView sync).
        new_timeline = self._execute_timeline_edit_command(command, timeline)

        # FX-H02: invalidate the clip's render cache so the viewer no
        # longer shows the pre-edit frame after this effect change.
        self._invalidate_clip_render_cache(clip_id)

        # Sync the mutated clip into the panel so the ◆ button reflects
        # the just-added/removed keyframe immediately. Without this the
        # panel would still hold the pre-command clip reference and
        # _update_keyframe_buttons would query stale keyframe state.
        new_clip = new_timeline.get_clip_by_id(clip_id)
        if new_clip is not None:
            self.edit_properties_panel.update_current_clip(new_clip)
        self._request_program_viewer_update()

    def _on_edit_effect_enabled_changed(
        self, clip_id: str, effect_id: str, enabled: bool
    ) -> None:
        """
        Handle effect enabled state change from EditPropertiesPanel.

        Creates and executes a SetEffectEnabledCommand to toggle
        the effect's enabled state.

        Args:
            clip_id: Clip ID containing the effect
            effect_id: Effect ID to toggle
            enabled: New enabled state
        """
        logger.debug(f"_on_edit_effect_enabled_changed: {clip_id}/{effect_id} = {enabled}")

        # Get current timeline from edit_page
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for effect enabled toggle")
            return

        timeline = self.edit_page._current_timeline

        # Find the clip to get its track_id and old state
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        track_id = clip.track_id

        # Get the effect and old enabled state
        effect = clip.get_effect_by_id(effect_id)
        if not effect:
            logger.warning(f"Effect not found: {effect_id}")
            return

        # R-001: Delayed import for EffectInstance via facade
        EffectInstance = get_effect_instance_class()

        # Get old enabled state for undo
        old_enabled = None
        if EffectInstance is not None and isinstance(effect, EffectInstance):
            old_enabled = effect.is_enabled
        elif isinstance(effect, dict):
            old_enabled = effect.get("is_enabled", True)

        # Import command via facade
        _cmds = get_command_classes()
        if _cmds is None:
            return
        SetEffectEnabledCommand = _cmds["SetEffectEnabledCommand"]

        # Create command
        command = SetEffectEnabledCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            effect_id=effect_id,
            new_enabled=enabled,
            old_enabled=old_enabled
        )

        # AUD-2707: commit through the TimelineView command stack (helper
        # handles undo entry + preview renderer/TimelineView sync).
        self._execute_timeline_edit_command(command, timeline)

        # FX-H02: invalidate the clip's render cache so the viewer no
        # longer shows the pre-edit frame after this effect change.
        self._invalidate_clip_render_cache(clip_id)

        # Update properties panel to reflect change
        self._update_properties_panel_after_effect_change(clip_id)

        # Request program viewer update
        self._request_program_viewer_update()

    def _on_edit_effect_remove_requested(self, clip_id: str, effect_id: str) -> None:
        """
        Handle effect removal request from EditPropertiesPanel.

        Creates and executes a RemoveEffectCommand to remove
        the effect from the clip.

        Args:
            clip_id: Clip ID containing the effect
            effect_id: Effect ID to remove
        """
        logger.debug(f"_on_edit_effect_remove_requested: {clip_id}/{effect_id}")

        # Get current timeline from edit_page
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for effect removal")
            return

        timeline = self.edit_page._current_timeline

        # Find the clip to get its track_id
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        track_id = clip.track_id

        # Import command via facade
        _cmds = get_command_classes()
        if _cmds is None:
            return
        RemoveEffectCommand = _cmds["RemoveEffectCommand"]

        # Create command
        command = RemoveEffectCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            effect_id=effect_id
        )

        # AUD-2707: commit through the TimelineView command stack (helper
        # handles undo entry + preview renderer/TimelineView sync).
        self._execute_timeline_edit_command(command, timeline)

        # FX-H02: invalidate the clip's render cache so the viewer no
        # longer shows the pre-edit frame after this effect change.
        self._invalidate_clip_render_cache(clip_id)

        # Update properties panel (effect removed)
        # Clear effect selection since the effect was removed
        self.edit_properties_panel._selected_effect_id = None
        self._update_properties_panel_after_effect_change(clip_id)

        # Request program viewer update
        self._request_program_viewer_update()

    def _on_edit_effect_reorder_requested(
        self, clip_id: str, effect_id: str, new_order: int
    ) -> None:
        """FX-H04-UI: Handle effect reorder from drag-drop in Properties panel.

        Creates and executes a ReorderEffectCommand exactly once per drop.
        The command renumbers all effects to compact 0..n-1 array positions.

        Args:
            clip_id: Clip containing the effects
            effect_id: Effect being moved
            new_order: Target index (0-based) after removal of source
        """
        logger.debug(f"_on_edit_effect_reorder_requested: {clip_id}/{effect_id} -> {new_order}")

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for effect reorder")
            return

        timeline = self.edit_page._current_timeline

        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found for reorder: {clip_id}")
            return

        track_id = clip.track_id

        _cmds = get_command_classes()
        if _cmds is None:
            return
        ReorderEffectCommand = _cmds["ReorderEffectCommand"]

        command = ReorderEffectCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            effect_id=effect_id,
            new_order=new_order,
            timeline=timeline,
        )

        # AUD-2707: commit through the TimelineView command stack (helper
        # handles undo entry + preview renderer/TimelineView sync).
        self._execute_timeline_edit_command(command, timeline)

        self._invalidate_clip_render_cache(clip_id)
        self._update_properties_panel_after_effect_change(clip_id)
        self._request_program_viewer_update()

    def _get_clip_audio_effect_dialog_manager(self):
        """P-clip-FX: 懒初始化 clip-level audio 效果对话框管理器。

        Dialog manager 按 (clip_id, effect_id) 缓存独立参数对话框，避免重复弹出。
        懒初始化因为 controller 是 mixin，没有自己的 __init__。
        """
        mgr = getattr(self, "_clip_audio_fx_dialog_mgr", None)
        if mgr is None:
            from src.features.edit.ui.clip_audio_effect_dialog_adapter import (
                ClipAudioEffectDialogManager,
            )
            mgr = ClipAudioEffectDialogManager(parent=self.edit_properties_panel)
            # dialog 提交 → 批量落库
            mgr.clip_effect_params_committed.connect(
                self._on_edit_clip_effect_params_committed
            )
            self._clip_audio_fx_dialog_mgr = mgr
        return mgr

    def _on_edit_effect_open_dialog_requested(
        self, clip_id: str, effect_id: str, effect_type: str,
    ) -> None:
        """P-clip-FX: 「打开面板」按钮 → 打开 clip-level audio 效果独立对话框。

        复用 mixer 的 8 个 dialog（StripEQDialog / ReverbDialog / ...），
        通过 ClipAudioEffectDialogManager 适配 clip-level 的 dict 参数表示。

        Args:
            clip_id: 片段 ID
            effect_id: 效果实例 ID
            effect_type: mixer audio effect_type（strip_eq/reverb/...）
        """
        logger.debug(f"_on_edit_effect_open_dialog_requested: {clip_id}/{effect_id} ({effect_type})")

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for effect dialog")
            return

        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id)
        if clip is None:
            logger.warning(f"Clip not found: {clip_id}")
            return

        effect = clip.get_effect_by_id(effect_id)
        if effect is None:
            logger.warning(f"Effect not found: {effect_id}")
            return

        params_dict = dict(getattr(effect, "params", {}) or {})
        # 片段名用于窗口标题（优先用 clip 名，回退 clip_id）
        clip_name = getattr(clip, "name", "") or clip_id

        mgr = self._get_clip_audio_effect_dialog_manager()
        mgr.open_dialog(
            effect_type=effect_type,
            clip_id=clip_id,
            effect_id=effect_id,
            params_dict=params_dict,
            clip_name=clip_name,
            parent=self.edit_properties_panel,
        )

    def _on_edit_clip_effect_params_committed(
        self, clip_id: str, effect_id: str, full_params: dict,
    ) -> None:
        """P-clip-FX: dialog committed → 批量更新 clip 效果参数（单步撤销）。

        dialog 回传完整的 ``*Params.to_dict()``；与当前 effect.params 对比，把
        变化的字段逐个包成 UpdateEffectParamCommand，再用 MacroCommand 聚合成
        一个撤销步骤（避免拖一下旋钮产生 N 步撤销）。

        Args:
            clip_id: 片段 ID
            effect_id: 效果实例 ID
            full_params: 完整参数 dict（来自 dialog 的 *Params.to_dict()）
        """
        logger.debug(f"_on_edit_clip_effect_params_committed: {clip_id}/{effect_id}")

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return

        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id)
        if clip is None:
            return

        effect = clip.get_effect_by_id(effect_id)
        if effect is None:
            return

        track_id = clip.track_id
        old_params = dict(getattr(effect, "params", {}) or {})

        # 计算变化字段（新增或值不同）
        changes = [
            (k, v) for k, v in full_params.items()
            if k not in old_params or old_params[k] != v
        ]
        if not changes:
            return  # 无变化（例如仅切换开关到原状态）

        _cmds = get_command_classes()
        if _cmds is None:
            return
        UpdateEffectParamCommand = _cmds["UpdateEffectParamCommand"]
        MacroCommand = _cmds.get("MacroCommand")

        sub_commands = []
        for param_name, new_value in changes:
            old_value = old_params.get(param_name)
            sub_commands.append(UpdateEffectParamCommand.create(
                track_id=track_id,
                clip_id=clip_id,
                effect_id=effect_id,
                param_name=param_name,
                new_value=new_value,
                old_value=old_value,
            ))

        if MacroCommand is not None and len(sub_commands) > 1:
            command = MacroCommand(
                command_id=str(uuid.uuid4()),
                label=f"Edit clip audio effect params ({effect.effect_type})",
                sub_commands=tuple(sub_commands),
            )
        else:
            # 单字段变化或无 MacroCommand → 直接用原子命令
            command = sub_commands[0]

        # AUD-2707: EditPage has no ``_command_stack`` — the old hasattr
        # branch was dead code (and never unpacked MacroCommand's
        # ``(new_timeline, updated_command)`` tuple).  Commit through the
        # TimelineView command stack instead.
        self._execute_timeline_edit_command(command, timeline)

        # P-clip-FX: 无 TimelineView 的 fallback 路径不经过 timeline_view
        # 的命令栈，``timeline_changed`` 不会发射，需要手动重同步音频引擎；
        # 视图路径虽然发射信号，但 signature 去重保证这里二次调用无副作用。
        # 仅 audio 类效果变化才真正重建，video 类无副作用。
        self._resync_audio_after_effect_change()

        # 这里不重建属性面板（dialog 仍打开，重建会打断交互）。

    def _resync_audio_after_effect_change(self) -> None:
        """P-clip-FX: effect 参数变化后重新同步音频引擎。

        无 TimelineView 的 fallback 路径（直接 ``command.apply``）不经过
        timeline_view 的命令栈，``timeline_changed`` 信号不会发射，
        edit_page 的 audio signature 检测也不会被触发。本方法显式调用
        ``_sync_audio_tracks_from_timeline`` 补上这一环：它会比对
        signature，仅当 audio 图（含 clip effects）真正变化时才重建
        tracks 并刷新 set_clip_effects（因此对已发射过信号的视频路径
        二次调用也无副作用）。
        """
        sync = getattr(self.edit_page, "_sync_audio_tracks_from_timeline", None)
        if callable(sync):
            try:
                sync()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"audio resync after effect change failed: {e}")

    def _on_edit_effect_add_requested(self, effect_type: str, clip_id: str) -> None:
        """
        Handle effect add request from EditEffectsPanel.

        Creates and executes an AddEffectCommand to add
        the effect to the clip.

        Args:
            effect_type: Effect type identifier (e.g., 'gaussian_blur')
            clip_id: Clip ID to add effect to
        """
        logger.debug(f"_on_edit_effect_add_requested: {effect_type} -> clip {clip_id}")

        # Get current timeline from edit_page
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("No current timeline for effect add")
            return

        timeline = self.edit_page._current_timeline

        # Find the clip to get its track_id
        clip = timeline.get_clip_by_id(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        track_id = clip.track_id

        # Import command via facade
        _cmds = get_command_classes()
        if _cmds is None:
            return
        AddEffectCommand = _cmds["AddEffectCommand"]

        # Pre-generate the effect_id so we can scroll the properties panel
        # to the new section immediately after apply — same pattern as the
        # timeline drag-drop path (see _handle_effect_drop).
        import uuid as _uuid
        new_effect_id = str(_uuid.uuid4())

        # Create command
        command = AddEffectCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            effect_type=effect_type,
            effect_id=new_effect_id,
        )

        # AUD-2707: commit through the TimelineView command stack (helper
        # handles undo entry + preview renderer/TimelineView sync).
        self._execute_timeline_edit_command(command, timeline)

        # FX-H02: invalidate the clip's render cache so the viewer no
        # longer shows the pre-edit frame after this effect change.
        self._invalidate_clip_render_cache(clip_id)

        # Update properties panel (effect added) and auto-focus the new section.
        self._update_properties_panel_after_effect_change(clip_id)
        self._auto_focus_new_effect(clip_id, new_effect_id)

        # Request program viewer update
        self._request_program_viewer_update()

    def _on_edit_transition_apply_requested(self, transition_payload: str, clip_id: str) -> None:
        """
        Handle transition double-click from EditEffectsPanel (P1-06).

        Delegates drop-point resolution to
        TimelineView.apply_transition_from_panel, which reuses the exact same
        add_transition_at_cut_point / add_transition_at_clip_edge command
        path as a drag-drop. Failure reasons are routed back to the effects
        panel info label so a double-click can never silently do nothing.

        Args:
            transition_payload: Transition type or "wipe:<direction>" preset
            clip_id: Selected clip ID, or "" to fall back to the playhead
        """
        logger.debug(
            f"_on_edit_transition_apply_requested: {transition_payload} "
            f"clip={clip_id or 'playhead'}"
        )

        timeline_panel = getattr(self, 'timeline_panel', None)
        timeline_view = getattr(timeline_panel, 'timeline_view', None) if timeline_panel else None
        if timeline_view is None:
            return

        try:
            playhead = timeline_panel.get_playhead_frame()
        except Exception:  # noqa: BLE001
            playhead = 0

        try:
            reason = timeline_view.apply_transition_from_panel(
                transition_payload, clip_id, playhead_frame=playhead
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"apply_transition_from_panel failed: {e}")
            reason = str(e)

        if reason:
            effects_panel = getattr(self, 'edit_effects_panel', None)
            if effects_panel is not None:
                try:
                    effects_panel.show_transition_feedback(reason)
                except Exception:  # noqa: BLE001
                    logger.debug("transition feedback could not be shown")

    def _edit_timeline_view(self):
        """Resolve the current TimelineView, or None when edit is inactive."""
        timeline_panel = getattr(self, 'timeline_panel', None)
        return getattr(timeline_panel, 'timeline_view', None) if timeline_panel else None

    def on_timeline_delete(self) -> None:
        """Edit > Timeline > Delete menu callback.

        Routes through TimelineView.handle_delete_request so the menu and
        the Delete key share one dispatcher: selected clips are deleted,
        and with an In/Out range set (and no clip selection) the range is
        Lifted — content removed, gap kept (P1-03).
        """
        view = self._edit_timeline_view()
        if view is not None:
            view.handle_delete_request(ripple=False)

    def on_timeline_ripple_delete(self) -> None:
        """Edit > Timeline > Ripple Delete menu callback.

        Same dispatcher as Shift+Delete: ripple-deletes the selection, or
        with an In/Out range set (and no clip selection) Extracts the range
        — content removed and the gap closed by shifting later clips left
        (P1-03).
        """
        view = self._edit_timeline_view()
        if view is not None:
            view.handle_delete_request(ripple=True)

    def on_timeline_cut(self) -> None:
        """Edit > Timeline > Cut menu callback (E03).

        与 TimelineView 的 Ctrl+X 硬编码分支共用 ``_cut_selected_clips``，
        保证菜单与键盘结果一致；无选中时给出状态提示而非静默 no-op。
        """
        view = self._edit_timeline_view()
        if view is None:
            return
        if not getattr(getattr(view, "_selection", None), "selected_clip_ids", None):
            self._show_edit_status_message("剪切：时间线没有选中的片段")
            return
        view._cut_selected_clips()

    def on_timeline_copy(self) -> None:
        """Edit > Timeline > Copy menu callback (E03)。"""
        view = self._edit_timeline_view()
        if view is None:
            return
        if not getattr(getattr(view, "_selection", None), "selected_clip_ids", None):
            self._show_edit_status_message("复制：时间线没有选中的片段")
            return
        view._copy_selected_clips()

    def on_timeline_paste(self) -> None:
        """Edit > Timeline > Paste menu callback (E03)。"""
        view = self._edit_timeline_view()
        if view is None:
            return
        if getattr(view, "_clipboard", None) is None:
            self._show_edit_status_message("粘贴：剪贴板为空")
            return
        view._paste_clips_at_playhead()

    def on_timeline_split(self) -> None:
        """Edit > Timeline > Split at Playhead menu callback (E03)。"""
        view = self._edit_timeline_view()
        if view is not None:
            view._split_all_at_playhead()

    def on_timeline_insert(self) -> None:
        """Edit > Timeline > Insert menu callback (E03)。

        与 TimelinePanel 工具栏 Insert 按钮同一桥接：读取 Source Viewer 的
        当前素材与 In/Out，ripple 插入播放头处。
        """
        page = getattr(self, "edit_page", None)
        if page is not None and hasattr(page, "_on_timeline_source_insert"):
            page._on_timeline_source_insert()

    def on_timeline_overwrite(self) -> None:
        """Edit > Timeline > Overwrite menu callback (E03)。"""
        page = getattr(self, "edit_page", None)
        if page is not None and hasattr(page, "_on_timeline_source_overwrite"):
            page._on_timeline_source_overwrite()

    def on_timeline_append(self) -> None:
        """Edit > Timeline > Append menu callback (E03)。"""
        page = getattr(self, "edit_page", None)
        if page is not None and hasattr(page, "_on_timeline_source_append"):
            page._on_timeline_source_append()

    def _on_edit_effect_added(self, clip_id: str, effect_id: str) -> None:
        """Handle effect_added signal from timeline drag-drop.

        The panel has already been rebuilt by the command stack side effect
        inside ``TimelineView._handle_effect_drop``, so here we only need to
        auto-focus the new effect section. Mirrors the double-click path.
        """
        self._auto_focus_new_effect(clip_id, effect_id)

    def _auto_focus_new_effect(self, clip_id: str, effect_id: str) -> None:
        """Ensure the clip is selected and scroll the panel to the new effect.

        Drag-drop may target a clip that isn't currently selected, so we
        re-select it first (which rebuilds the panel with the new effect
        section), then ask the panel to scroll to that section.
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        # If the targeted clip isn't the one currently shown in the panel,
        # rebuild the panel for it (this also picks up the new effect).
        current_shown = (
            self.edit_properties_panel._current_clips[0]
            if self.edit_properties_panel._current_clips else None
        )
        if current_shown is None or current_shown.clip_id != clip_id:
            self._show_clip_in_properties(clip)
        # Scroll + flash the new effect's section header.
        self.edit_properties_panel.scroll_to_effect(effect_id)

    def _update_properties_panel_after_effect_change(self, clip_id: str) -> None:
        """Update properties panel after an effect change."""
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return

        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id)

        if clip:
            self._show_clip_in_properties(clip)

    def _on_timeline_clip_selected(self, clip_id: str) -> None:
        """Handle clip selection from timeline view."""
        logger.debug(f"_on_timeline_clip_selected: {clip_id}")

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return

        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id)

        if clip:
            self._show_clip_in_properties(clip)
            # If the Transform tool is active, push the new selection so the
            # handles track the newly selected clip.
            viewer = getattr(self.edit_page, '_program_viewer', None)
            if viewer is not None and viewer.is_transform_tool_active():
                self._push_selected_clip_to_transform_viewer()

    def _on_timeline_selection_changed(self, selection: object) -> None:
        """
        Handle selection change from timeline view (TimelineSelection object).

        P5.1: Supports different selection types:
        - CLIP: Single or multiple clips
        - TRANSITION: Transition between clips
        - TRACK: Track header selection
        - TIMELINE: Empty timeline (show timeline settings)
        - NONE: No selection (clear panel)
        """
        logger.debug(f"_on_timeline_selection_changed: {selection}")

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            self.edit_properties_panel.clear()
            self._clear_edit_info()
            return

        timeline = self.edit_page._current_timeline

        # Check if it's a TimelineSelection object via facade
        _sel = get_selection_classes()
        if _sel is None:
            # Fallback: treat as clip IDs
            if isinstance(selection, list):
                clips = [timeline.get_clip_by_id(cid) for cid in selection]
                clips = [c for c in clips if c is not None]
                if clips:
                    self.edit_properties_panel.set_clips(clips, self.edit_page._fps)
                    self._show_clip_in_info(clips[0])
                else:
                    self.edit_properties_panel.clear()
                    self._clear_edit_info()
            else:
                self.edit_properties_panel.clear()
                self._clear_edit_info()
            return
        TimelineSelection = _sel["TimelineSelection"]
        SelectionType = _sel["SelectionType"]

        if not isinstance(selection, TimelineSelection):
            # Legacy fallback: treat as clip IDs
            if isinstance(selection, list):
                clips = [timeline.get_clip_by_id(cid) for cid in selection]
                clips = [c for c in clips if c is not None]
                if clips:
                    self.edit_properties_panel.set_clips(clips, self.edit_page._fps)
                    self._show_clip_in_info(clips[0])
                else:
                    self.edit_properties_panel.clear()
                    self._clear_edit_info()
            else:
                self.edit_properties_panel.clear()
                self._clear_edit_info()
            return

        # Handle selection based on type
        sel_type = selection.selection_type

        if sel_type == SelectionType.NONE:
            # No selection: clear panel
            self.edit_properties_panel.clear()
            self._clear_edit_info()
            self.edit_effects_panel.set_selected_clip(None)
            self._clear_subtitle_viewer_selection()

        elif sel_type == SelectionType.CLIP:
            # Clip selection (single or multiple)
            clip_ids = list(selection.selected_clip_ids)
            clips = [timeline.get_clip_by_id(cid) for cid in clip_ids]
            clips = [c for c in clips if c is not None]

            # P10.3: 选了视频/音频 clip 时清除字幕手柄
            self._clear_subtitle_viewer_selection()

            if clips:
                # 解析每个 clip 所在的 track，用于音频片段的音量/声像显示
                tracks = []
                for c in clips:
                    t = timeline.get_track_by_id(c.track_id) if c.track_id else None
                    tracks.append(t)
                # 解析生成器元数据（media_type / solid_color / generator_params），
                # 使属性面板能识别 solid 片段并显示完整的生成器参数 section。
                media_type = None
                solid_color = None
                generator_params = None
                project = getattr(self, '_current_edit_project', None)
                first_asset_id = getattr(clips[0], 'asset_id', '')
                if project is not None and first_asset_id:
                    asset = project.assets.get(first_asset_id)
                    if asset is not None:
                        media_type = getattr(asset, 'media_type', None)
                        solid_color = getattr(asset, 'solid_color', None)
                        generator_params = getattr(asset, 'generator_params', None)
                self.edit_properties_panel.set_clips(
                    clips, self.edit_page._fps, tracks=tracks,
                    media_type=media_type, solid_color=solid_color,
                    generator_params=generator_params,
                )
                self._show_clip_in_info(clips[0])

                # Update edit_effects_panel with selected clip ID for double-click effect add
                # If single clip selected, pass clip_id; else None
                if len(clip_ids) == 1:
                    self.edit_effects_panel.set_selected_clip(clip_ids[0])
                else:
                    self.edit_effects_panel.set_selected_clip(None)
            else:
                self.edit_properties_panel.clear()
                self._clear_edit_info()
                self.edit_effects_panel.set_selected_clip(None)

        elif sel_type == SelectionType.TRANSITION:
            # Transition selection
            self._clear_subtitle_viewer_selection()
            self._clear_edit_info()
            transition_ids = list(selection.selected_transition_ids)
            if transition_ids:
                # Get transition from track
                trans_id = transition_ids[0]
                transition = self._find_transition_by_id(timeline, trans_id)
                if transition:
                    self.edit_properties_panel.set_transition(transition)
                else:
                    self.edit_properties_panel.clear()
            else:
                self.edit_properties_panel.clear()

        elif sel_type == SelectionType.TRACK:
            # Track selection
            self._clear_subtitle_viewer_selection()
            self._clear_edit_info()
            track_ids = list(selection.selected_track_ids)
            if track_ids:
                track_id = track_ids[0]
                track = timeline.get_track_by_id(track_id)
                if track:
                    self.edit_properties_panel.set_track(track)
                else:
                    self.edit_properties_panel.clear()
            else:
                self.edit_properties_panel.clear()

        elif sel_type == SelectionType.TIMELINE:
            # Timeline selection (when clicking empty area)
            self._clear_subtitle_viewer_selection()
            self.edit_properties_panel.set_timeline(timeline)
            self._show_timeline_in_info(timeline)

        else:
            # Unknown selection type
            self._clear_subtitle_viewer_selection()
            self._clear_edit_info()
            self.edit_properties_panel.clear()

    def _find_transition_by_id(
        self,
        timeline,
        transition_id: str
    ):
        """
        Find a transition by ID in the timeline.

        Args:
            timeline: TimelineSequence to search
            transition_id: ID of the transition to find

        Returns:
            TransitionInstance if found, None otherwise
        """
        for track in timeline.all_tracks:
            for transition in track.transitions:
                if transition.transition_id == transition_id:
                    return transition
        return None

    def _on_assets_selection_changed(self, asset_ids: list) -> None:
        """
        Handle asset selection change from EditPage (P5.1).

        Shows asset properties in the Properties panel when
        assets are selected in the MediaPoolPanel.

        Args:
            asset_ids: List of selected asset IDs
        """
        logger.debug(f"_on_assets_selection_changed: {asset_ids}")

        if not hasattr(self, '_current_edit_project') or not self._current_edit_project:
            self.edit_properties_panel.clear()
            self._clear_edit_info()
            return

        if not asset_ids:
            self.edit_properties_panel.clear()
            self._clear_edit_info()
            return

        # Get asset from project
        if len(asset_ids) == 1:
            # Single asset selection
            asset = self._current_edit_project.get_asset_by_id(asset_ids[0])
            if asset:
                self.edit_properties_panel.set_asset(asset)
                info_panel = getattr(self, "edit_info_panel", None)
                if info_panel is not None:
                    info_panel.set_asset(asset)
            else:
                self.edit_properties_panel.clear()
                self._clear_edit_info()
        else:
            # E19: 多选素材——批量属性暂不支持（产品边界），但给出明确的
            # 批量状态提示，不再静默清空面板造成"坏了"的印象。
            if hasattr(self.edit_properties_panel, "set_multi_asset_selection"):
                self.edit_properties_panel.set_multi_asset_selection(len(asset_ids))
            else:
                self.edit_properties_panel.clear()
            self._clear_edit_info()

    def _on_open_in_composite_requested(self, clip_id: str, track_id: str) -> None:
        """
        Handle "Open in Composite" request from clip context menu.

        Behavior:
        1. Existing Comp Clip or composite: opens directly
        2. Normal clip without composite: opens the unbound Composite
           workspace without mutating the timeline/project

        Args:
            clip_id: Clip ID that was right-clicked
            track_id: Track ID containing the clip
        """
        logger.info(
            f"[Phase1] Open in Composite requested: "
            f"clip={clip_id}, track={track_id}"
        )

        if not hasattr(self, '_current_edit_project') or not self._current_edit_project:
            logger.warning("Cannot open composite: no project")
            return

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("Cannot open composite: no timeline")
            return

        timeline = self.edit_page._current_timeline

        # Find the clip
        track = timeline.get_track_by_id(track_id)
        if track is None:
            logger.warning(f"Track not found: {track_id}")
            return

        clip = track.get_clip_by_id(clip_id)
        if clip is None:
            logger.warning(f"Clip not found: {clip_id}")
            return

        # Case 1: Clip already has composite_id - open it directly
        if clip.composite_id:
            logger.info(f"[Phase1] Opening existing composite: {clip.composite_id}")
            self._switch_to_composite_page(clip.composite_id)
            return

        # Case 2: Asset is comp_clip - find the graph and open
        asset = self._current_edit_project.get_asset_by_id(clip.asset_id)
        if asset is not None and asset.is_comp_clip:
            graph_id = asset.metadata.get("graph_id")
            if graph_id:
                logger.info(f"[Phase1] Opening comp clip composite: {graph_id}")
                self._switch_to_composite_page(graph_id)
                return

        # Case 3: a normal clip has no persistent composite yet.  Enter the
        # scratch workspace; the page-level enter hook clears any previously
        # opened graph, and the user can create a persistent Comp Clip from
        # the shared Media Pool when ready.
        logger.info(
            "[Composite] Opening unbound workspace for normal clip %s",
            clip_id,
        )
        self._suppress_composite_auto_bind_once = True
        self.switch_to_page("Composite")

    def _on_open_comp_clip_in_composite(self, asset_id: str) -> None:
        """
        Handle comp_clip double-click or "Open in Composite" from media pool.

        Resolves the asset to its graph_id and switches to the Composite page.
        """
        if not self._current_edit_project:
            logger.warning("No active project for comp clip open")
            return

        asset = self._current_edit_project.get_asset_by_id(asset_id)
        if asset is None or not asset.is_comp_clip:
            logger.warning(f"Asset {asset_id} is not a comp_clip")
            return

        graph_id = asset.metadata.get("graph_id")
        if graph_id:
            logger.info(f"[P5] Opening comp clip composite: {graph_id}")
            self._switch_to_composite_page(graph_id)
        else:
            logger.warning(f"Comp clip asset {asset_id} has no graph_id")

    def _on_create_empty_comp_clip(self) -> None:
        """
        Handle "New Comp Clip" request from media pool context menu.

        Pops up a settings dialog (name / resolution / fps / duration /
        background color), creates the empty Comp Clip, refreshes the media
        pool, and switches to the Composite page for editing.

        If no project exists yet, one is auto-created so the user can start
        making a comp clip immediately (AE-style) without first setting up a
        timeline-based edit project.
        """
        from PyQt6.QtWidgets import QDialog
        from src.features.edit.ui.comp_clip_settings_dialog import (
            CompClipSettingsDialog,
        )

        # Auto-create a project if none exists, so a user who just wants to
        # build a single effect shot isn't forced to set up a timeline.
        if not self._current_edit_project:
            if hasattr(self, "_ensure_edit_project"):
                if not self._ensure_edit_project():
                    logger.warning("Cannot create comp clip: no project and auto-create failed")
                    return
            else:
                logger.warning("No active project for creating empty comp clip")
                return

        dialog = CompClipSettingsDialog(parent=self._get_dialog_parent())
        self._comp_clip_settings_dialog = dialog

        def _on_finished(result: int) -> None:
            try:
                if result != QDialog.DialogCode.Accepted:
                    return

                settings = dialog.get_settings()
                name = settings["name"]
                width = settings["width"]
                height = settings["height"]
                fps = settings["fps"]
                # duration: seconds -> frames
                duration_frames = max(
                    1, int(round(settings["duration_seconds"] * fps)))
                background_color = settings["background_color"]

                new_project, comp_clip_id, comp_asset_id, graph_id = \
                    self._current_edit_project.create_empty_comp_clip(
                        name=name,
                        width=width,
                        height=height,
                        fps=fps,
                        duration_frames=duration_frames,
                        background_color=background_color,
                    )

                self._current_edit_project = new_project

                # Refresh media pool
                if hasattr(self, 'media_pool_panel') and self.media_pool_panel:
                    self.media_pool_panel.set_project(new_project)

                logger.info(
                    f"[P5] Created empty Comp Clip: comp_clip_id={comp_clip_id}, "
                    f"asset_id={comp_asset_id}, graph_id={graph_id}"
                )

                # B: creation is an explicit intent to edit — jump to Composite.
                self._switch_to_composite_page(graph_id)
            finally:
                if getattr(self, "_comp_clip_settings_dialog", None) is dialog:
                    self._comp_clip_settings_dialog = None
                dialog.deleteLater()

        dialog.finished.connect(_on_finished)
        # Keep this non-modal: QDialog.exec()/open() can create a native modal
        # session on macOS, which blocks the standalone Color Inspector window.
        dialog.setModal(False)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_edit_comp_clip_settings(self, asset_id: str) -> None:
        """
        Handle "Comp Clip Settings..." request from media pool context menu.

        Opens the settings dialog pre-filled with the existing Comp Clip's
        values and applies changes (resolution / fps / duration / background
        color) to the project data.
        """
        from PyQt6.QtWidgets import QDialog
        from src.features.edit.ui.comp_clip_settings_dialog import (
            CompClipSettingsDialog,
        )

        if not self._current_edit_project:
            logger.warning("No active project for editing comp clip settings")
            return

        asset = self._current_edit_project.get_asset_by_id(asset_id)
        if asset is None or not getattr(asset, "is_comp_clip", False):
            logger.warning(f"Asset {asset_id} is not a comp_clip")
            return

        comp_clip_id = asset.metadata.get("comp_clip_id")
        graph_id = asset.metadata.get("graph_id")
        if not comp_clip_id or not graph_id:
            logger.warning(f"Comp clip asset {asset_id} missing ids")
            return

        # Collect current settings from CompClipData / graph
        initial = self._collect_comp_clip_settings(comp_clip_id, graph_id)

        dialog = CompClipSettingsDialog(
            parent=self._get_dialog_parent(),
            edit_mode=True,
            initial_settings=initial,
        )
        self._comp_clip_settings_dialog = dialog

        def _on_finished(result: int) -> None:
            try:
                if result != QDialog.DialogCode.Accepted:
                    return

                settings = dialog.get_settings()
                duration_frames = max(
                    1, int(round(settings["duration_seconds"] * settings["fps"])))

                self._current_edit_project = self._current_edit_project.update_comp_clip(
                    comp_clip_id=comp_clip_id,
                    name=settings["name"],
                    width=settings["width"],
                    height=settings["height"],
                    fps=settings["fps"],
                    duration_frames=duration_frames,
                    background_color=settings["background_color"],
                )

                # Refresh media pool
                if hasattr(self, 'media_pool_panel') and self.media_pool_panel:
                    self.media_pool_panel.set_project(self._current_edit_project)

                # Reload composite graph if currently on Composite page
                if getattr(self, "composite_page", None) and \
                        getattr(self, "_current_composite_id", None) == graph_id:
                    comp_data = self._current_edit_project.get_composition_by_id(graph_id)
                    if comp_data is not None:
                        self.composite_page.load_composite_data(comp_data)

                logger.info(f"[P5] Updated comp clip settings: {comp_clip_id}")
            finally:
                if getattr(self, "_comp_clip_settings_dialog", None) is dialog:
                    self._comp_clip_settings_dialog = None
                dialog.deleteLater()

        dialog.finished.connect(_on_finished)
        # Keep this non-modal for the same macOS reason as the New Comp Clip
        # flow above: the standalone Color Inspector must receive mouse input.
        dialog.setModal(False)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _collect_comp_clip_settings(
        self, comp_clip_id: str, graph_id: str
    ) -> dict:
        """Gather current Comp Clip settings for the edit dialog."""
        from src.features.edit.core.comp_clip_data import CompClipData
        from src.features.edit.core.composite_graph_data import (
            PROJECT_BACKGROUND_PARAM,
            CONSTANT_COLOR_SPACE_PARAM,
        )

        settings: dict = {}
        cc_dict = self._current_edit_project.comp_clips.get(comp_clip_id)
        if cc_dict is not None:
            cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict
            settings["name"] = cc.name
            settings["width"] = cc.width
            settings["height"] = cc.height
            settings["fps"] = cc.fps
            settings["duration_seconds"] = (
                cc.duration_frames / cc.fps if cc.fps else 3.0
            )

        # Background color from the constant node in the graph
        comp_data = self._current_edit_project.get_composition_by_id(graph_id)
        if comp_data is not None:
            nodes = comp_data.get("nodes", []) if isinstance(comp_data, dict) \
                else getattr(comp_data, "nodes", [])
            constant_nodes = [
                node for node in nodes if node.get("type") == "constant"
            ]
            background_nodes = [
                node for node in constant_nodes
                if bool(node.get("parameters", {}).get(
                    PROJECT_BACKGROUND_PARAM, False))
            ]
            source_kind = (
                comp_data.get("source_kind")
                if isinstance(comp_data, dict)
                else getattr(comp_data, "source_kind", "")
            )
            if not background_nodes and source_kind == "empty_comp_clip":
                background_nodes = constant_nodes[:1]
            if background_nodes:
                params = background_nodes[0].get("parameters", {})
                from src.features.composite.nodes.constant_node import ConstantNode
                legacy_srgb = params.get(CONSTANT_COLOR_SPACE_PARAM) != "linear"
                params = ConstantNode.normalize_color_parameters(
                    params, legacy_srgb=legacy_srgb)
                settings["background_color"] = (
                    float(params.get("color_r", 1.0)),
                    float(params.get("color_g", 1.0)),
                    float(params.get("color_b", 1.0)),
                    float(params.get("color_a", 1.0)),
                )
        return settings

    def _get_dialog_parent(self):
        """Return the parent widget for modal dialogs (the main window)."""
        # MainWindow mixes in this controller and IS a QWidget.
        return self

    def on_program_guide_changed(
        self,
        guide_type: str,
        guide_id: str,
        params: dict[str, object],
    ) -> None:
        """Persist a Program monitor guide as timeline format metadata.

        Outline guides remain monitor-only.  A black aspect-ratio matte is
        rendered into exports only when its ``burn_in`` flag is enabled.
        """
        timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
        if timeline is None:
            return

        format_settings = dict(getattr(timeline, "format_settings", {}) or {})
        if guide_id == "default":
            format_settings.pop("program_guide", None)
        else:
            format_settings["program_guide"] = {
                "type": guide_type,
                "id": guide_id,
                "params": dict(params),
            }

        updated_timeline = replace(timeline, format_settings=format_settings)
        self._on_edit_timeline_changed(updated_timeline)
        self.edit_project_dirty = True

    # ------------------------------------------------------------------
    # B3: Render Cache smart 自动评估器
    # ------------------------------------------------------------------

    _RENDER_CACHE_AUTO_DEBOUNCE_MS = 5000
    #: 单次评估最多自动入队的 clip 数（配合 worker 并发 1）。
    _RENDER_CACHE_AUTO_BATCH = 1

    def _render_cache_auto_mode(self) -> str:
        """读取偏好中的自动缓存模式（off | smart | user）。"""
        try:
            from src.shared.config.config_manager import ConfigManager

            return str(ConfigManager().get("render_cache_auto_mode") or "off")
        except Exception:  # noqa: BLE001
            return "off"

    def _render_cache_auto_threshold(self) -> int:
        """smart 模式的分数阈值（偏好可调，默认 6）。"""
        try:
            from src.shared.config.config_manager import ConfigManager

            value = ConfigManager().get("render_cache_auto_threshold")
            return int(value) if value else 6
        except Exception:  # noqa: BLE001
            return 6

    def _schedule_auto_render_cache(self) -> None:
        """命令提交后 debounce 5s 再评估（合并连续编辑）。"""
        if self._render_cache_auto_mode() != "smart":
            return
        timer = getattr(self, "_render_cache_auto_timer", None)
        if timer is None:
            from PyQt6.QtCore import QTimer

            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self._RENDER_CACHE_AUTO_DEBOUNCE_MS)
            timer.timeout.connect(self._auto_evaluate_render_cache)
            self._render_cache_auto_timer = timer
        timer.start()

    def _auto_evaluate_render_cache(self) -> None:
        """smart 模式：扫描时间线，对达标片段按既有手动路径自动入队。"""
        from src.features.edit.core.render_cache_evaluator import (
            select_auto_cache_clips,
        )

        if self._render_cache_auto_mode() != "smart":
            return
        timeline = (
            getattr(self.edit_page, "_current_timeline", None)
            if getattr(self, "edit_page", None) is not None else None)
        if timeline is None:
            return
        clips = []
        for track in timeline.all_tracks:
            clips.extend(track.clips)
        fps = 24.0
        raw_fps = getattr(timeline, "fps", None)
        if raw_fps is not None:
            fps = float(raw_fps.num if hasattr(raw_fps, "num") else raw_fps) or 24.0
        scored = select_auto_cache_clips(
            clips, threshold=self._render_cache_auto_threshold(), fps=fps)
        dispatched = 0
        for clip, score in scored:
            if dispatched >= self._RENDER_CACHE_AUTO_BATCH:
                break
            inflight = getattr(self, "_render_cache_inflight_clips", None) or set()
            key = (str(getattr(timeline, "timeline_id", "")), str(clip.clip_id))
            if key in inflight:
                continue  # in-flight 去重（与手动路径共用守卫）
            # 渲染缓存管线当前只覆盖合成片段（Comp Clip）与 Motion
            # Graphic；普通片段即便分数达标也不会命中，只会反复弹
            # "No composite to cache" 的状态栏提示。smart 派发前先按
            # 手动路径同口径筛掉。
            _asset = (
                self._current_edit_project.assets.get(clip.asset_id)
                if self._current_edit_project else None)
            _is_mg = getattr(_asset, "media_type", "") == "motion_graphic"
            if not getattr(clip, "composite_id", None) and not _is_mg:
                continue
            ok = self._on_render_cache_clip_requested(
                clip.clip_id, clip.track_id)
            if ok:
                logger.info(
                    "[RenderCache] auto-enqueued clip %s (score=%d)",
                    clip.clip_id, score)
                dispatched += 1

    def _on_edit_timeline_changed(self, timeline, *, _from_project_transaction: bool = False) -> None:
        """
        Handle timeline change from TimelineView (after command execution).

        Updates edit_page._current_timeline and caches the new timeline
        so Properties panel and project saving have the latest state.

        Args:
            timeline: Updated TimelineSequence
        """
        if timeline is None:
            return

        # B3: smart 模式下命令提交后 debounce 触发自动缓存评估
        self._schedule_auto_render_cache()

        # E11: 时间线命令入栈/撤销后刷新全局 Edit 菜单的 Undo/Redo 状态
        try:
            self.update_undo_redo_actions()
        except Exception:  # noqa: BLE001 菜单刷新失败不阻断编辑链路
            logger.debug("update_undo_redo_actions after timeline change failed",
                         exc_info=True)

        previous_timeline = None
        if hasattr(self, '_current_timeline_sequences'):
            previous_timeline = self._current_timeline_sequences.get(timeline.timeline_id)

        # Update edit_page's current timeline reference
        if self.edit_page is not None:
            self.edit_page.set_timeline(timeline)

        # Undo/redo and edits from other surfaces must replace the Inspector's
        # immutable snapshots too. Otherwise the next field edit resurrects
        # the pre-undo values from its cached full parameter dataclass.
        properties_panel = getattr(self, "edit_properties_panel", None)
        selected_clips = getattr(properties_panel, "_current_clips", ())
        if isinstance(selected_clips, (list, tuple)):
            self._restore_properties_panel_from_timeline(
                timeline, [clip.clip_id for clip in selected_clips])

        # Cache the updated timeline for project saving
        if hasattr(self, '_current_timeline_sequences'):
            # APP-01：序列内容版本前进（UI 命令提交边界 bump 过）时同步
            # 推进工程 revision，保证 AI plan 的 base_revision 乐观锁能看
            # 见人工修改；内容未变化（纯预览/重复信号）不增。
            prev_revision = (
                previous_timeline.revision
                if previous_timeline is not None else None
            )
            if (not _from_project_transaction and prev_revision is not None
                    and timeline.revision > prev_revision):
                project_before = self._current_edit_project
                if project_before is not None:
                    self._current_edit_project = replace(
                        project_before,
                        revision=project_before.revision + 1,
                    )
                    self.edit_project_dirty = True
            self._current_timeline_sequences[timeline.timeline_id] = timeline

        # Keep the media-pool TimelineSequenceRef aligned with the full
        # sequence. The pool renders this lightweight ref, so leaving it at
        # the provisional 24fps default makes the UI disagree with playback.
        project = self._current_edit_project
        if project is not None:
            try:
                timeline_fps = float(timeline.fps)
                existing_ref = project.timelines.get(timeline.timeline_id)
                timeline_format_settings = dict(
                    getattr(timeline, "format_settings", {})
                )
                format_settings = (
                    timeline_format_settings
                    or dict(getattr(existing_ref, "format_settings", {}))
                )
                ref_needs_update = (
                    existing_ref is None
                    or existing_ref.name != timeline.name
                    or existing_ref.fps != timeline_fps
                    or existing_ref.width != timeline.width
                    or existing_ref.height != timeline.height
                    or existing_ref.sample_rate != timeline.sample_rate
                    or existing_ref.format_settings != format_settings
                )
                if ref_needs_update:
                    from src.features.edit.commands_facade import get_project_classes

                    project_classes = get_project_classes()
                    if project_classes is None:
                        raise RuntimeError("Edit feature is unavailable")
                    TimelineSequenceRef = project_classes["TimelineSequenceRef"]
                    timeline_ref = TimelineSequenceRef(
                        timeline_id=timeline.timeline_id,
                        name=timeline.name,
                        fps=timeline_fps,
                        width=timeline.width,
                        height=timeline.height,
                        sample_rate=timeline.sample_rate,
                        format_settings=format_settings,
                        created_at=(
                            existing_ref.created_at if existing_ref is not None else 0.0
                        ),
                    )
                    updated_refs = dict(project.timelines)
                    updated_refs[timeline.timeline_id] = timeline_ref
                    # If there is no active timeline yet (e.g. a freshly
                    # created project that defers timeline creation until
                    # the first media drop), activate this one so the
                    # preview renderer can bind to it.
                    new_active = project.active_timeline_id or timeline.timeline_id
                    self._current_edit_project = replace(
                        project,
                        timelines=updated_refs,
                        active_timeline_id=new_active,
                    )
                    if hasattr(self, "media_pool_panel") and self.media_pool_panel:
                        self.media_pool_panel.set_project(self._current_edit_project)
            except Exception as error:
                logger.debug("Failed to sync media-pool timeline metadata: %s", error)

        # Motion Graphic caches are tied to the clip's source mapping and the
        # current sequence contract.  Normal trim/split/move reuse the same
        # recording; Shift-retiming changes its duration and therefore marks
        # that clip stale, as do resolution/FPS changes.
        self._invalidate_motion_graphic_caches_for_timeline_change(
            previous_timeline, timeline,
        )

        # Update renderer with new timeline sequence so it picks up added/removed clips
        # Lazy init: when a project starts with no default timeline, the renderer
        # is not created until the first timeline arrives here (e.g. the user
        # drags media onto the timeline, which triggers _create_default_timeline
        # → timeline_changed → this handler). Without this, the Program viewer
        # stays black because there is no renderer to produce frames.
        if self.timeline_preview_renderer is None and hasattr(self, "_init_timeline_preview_renderer"):
            try:
                self._init_timeline_preview_renderer()
            except Exception as error:
                logger.debug("Lazy TimelinePreviewRenderer init failed: %s", error)

        if self.timeline_preview_renderer is not None:
            preview_changed = True
            build_signature = getattr(
                self.timeline_preview_renderer,
                "_build_sequence_render_signature",
                None,
            )
            if callable(build_signature) and previous_timeline is not None:
                try:
                    preview_changed = (
                        build_signature(previous_timeline)
                        != build_signature(timeline)
                    )
                except Exception as error:
                    preview_changed = True
                    logger.debug("Failed to compare timeline render signature: %s", error)

            self.timeline_preview_renderer.set_sequence(timeline)

            # A timeline change can be part of a project transaction that also
            # changes the immutable project snapshot.  Creating a Comp Clip is
            # one such transaction: the new graph/CompClipData/MediaAsset are
            # published to ``_current_edit_project`` immediately before this
            # callback, while the preview renderer still holds the previous
            # project object.  If we only update the sequence, the worker keeps
            # an AssetResolver backed by the old ``compositions`` dictionary
            # and every composite-bound frame is reported as a render failure.
            # Publish the project after the sequence so set_project() keeps the
            # new sequence as well as the new graph snapshot.
            preview_project = getattr(self.timeline_preview_renderer, "_project", None)
            set_preview_project = getattr(
                self.timeline_preview_renderer, "set_project", None
            )
            if (
                preview_project is not None
                and self._current_edit_project is not None
                and preview_project is not self._current_edit_project
                and callable(set_preview_project)
            ):
                set_preview_project(self._current_edit_project)

            if preview_changed:
                self._request_current_timeline_preview_frame()
            else:
                playback_trace(
                    "edit_controller.preview_request_skipped",
                    timeline_id=getattr(timeline, "timeline_id", ""),
                )

        # Note: EditProjectIO uses autosave timer, no manual dirty flag needed

        # Sync Audio Mixer panel with the (possibly changed) audio tracks.
        # This keeps mixer strips in sync when tracks are added/removed or
        # audio params (volume/pan/mute/solo) change from any source
        # (track header, properties panel, undo/redo, etc.).
        # A6: debounce 100ms —— 高频 timeline 变更（拖色轮、连续 undo 等）会合并成
        # 一次 sync。sync_tracks 内部已有值变化检测，这里省的是 controller 层的
        # dict 构造 + panel 层的 diff 遍历开销。timer 回调总是用最新的 _current_timeline。
        self._schedule_audio_mixer_sync()

        # SUB-P2-03: refresh the Caption Workspace cue table after any commit
        # (add/edit/split/merge/undo/redo). set_sequence rebuilds the model and
        # preserves the current selection by clip_id, so focus is not lost.
        cwp = getattr(self, 'caption_workspace_panel', None)
        if cwp is not None:
            try:
                fps = float(getattr(timeline, 'fps', 24.0) or 24.0)
                if hasattr(fps, 'to_fps'):
                    fps = float(fps.to_fps())
                cwp.set_sequence(timeline, fps)
            except Exception:
                logger.debug("Caption workspace set_sequence failed", exc_info=True)

        logger.debug(f"_on_edit_timeline_changed: timeline {timeline.timeline_id} updated")

    def _schedule_audio_mixer_sync(self, delay_ms: int = 100) -> None:
        """A6: 防抖触发 Audio Mixer 同步。

        连续调用只会在最后一次调用后 delay_ms 触发一次。timer 回调使用
        edit_page._current_timeline（最新值），避免闭包捕获过期 timeline 对象。
        首次显示 mixer dock 时应直接调 _sync_audio_mixer_from_current_timeline
        （立即生效），不走防抖。
        """
        if not hasattr(self, '_mixer_sync_timer') or self._mixer_sync_timer is None:
            self._mixer_sync_timer = QTimer()
            self._mixer_sync_timer.setSingleShot(True)
            self._mixer_sync_timer.timeout.connect(self._fire_audio_mixer_sync)
        self._mixer_sync_timer.start(delay_ms)

    def _fire_audio_mixer_sync(self) -> None:
        """A6: _mixer_sync_timer 回调，用最新 timeline 同步 mixer。"""
        timeline = getattr(getattr(self, 'edit_page', None), '_current_timeline', None)
        if timeline is not None:
            self._sync_audio_mixer_from_timeline(timeline)

    def _request_program_viewer_update(self) -> None:
        """Request program viewer to update frame display."""
        if self.edit_page is not None and hasattr(self.edit_page, 'request_current_timeline_frame'):
            self.edit_page.request_current_timeline_frame()

    def _restore_properties_panel_from_timeline(self, timeline: Any,
                                                clip_ids: list) -> None:
        """N09/N12：从 canonical timeline 恢复/刷新 Inspector。

        领域拒绝后（模型未变）把控件假值归回 canonical；批量提交成功
        后（模型已变）把面板快照与控件同步到新值。走面板的
        ``update_clips``（按 stable clip_id 替换快照 + 刷新控件显示，
        不重建面板、不打断用户输入焦点），无该方法的旧宿主回退
        ``update_current_clip``。
        """
        if timeline is None or not clip_ids:
            return
        panel = getattr(self, 'edit_properties_panel', None)
        if panel is None:
            return
        clips = [
            c for c in (
                timeline.get_clip_by_id(cid) for cid in clip_ids
            ) if c is not None
        ]
        if not clips:
            return
        update_clips = getattr(panel, 'update_clips', None)
        if callable(update_clips):
            update_clips(clips)
            return
        update_current_clip = getattr(panel, 'update_current_clip', None)
        if callable(update_current_clip):
            for clip in clips:
                update_current_clip(clip)

    def _on_edit_project_timeline_transaction_committed(
        self, project: Any, timeline: Any, child_updates: dict[str, Any],
    ) -> None:
        """Publish a structural history edit as one project/sequence state."""
        self._current_edit_project = project
        for key, child in child_updates.items():
            if child is None:
                self._current_timeline_sequences.pop(key, None)
            else:
                self._current_timeline_sequences[key] = child
        self.edit_project_dirty = True
        self._on_edit_timeline_changed(timeline, _from_project_transaction=True)
        media_pool = getattr(self, "media_pool_panel", None)
        if media_pool is not None:
            media_pool.set_project(self._current_edit_project)
        panel = getattr(self, "timeline_panel", None)
        get_view = getattr(panel, "get_timeline_view", None)
        view = get_view() if callable(get_view) else None
        if view is not None:
            view.set_project(self._current_edit_project)
        refresh = getattr(panel, "refresh_committed_timeline", None)
        if callable(refresh):
            refresh(timeline)

    def _execute_timeline_edit_command(self, command: Any, timeline: Any) -> Any:
        """Commit a timeline command through the TimelineView command stack.

        AUD-2707: ``EditPage`` has no ``_command_stack`` (the timeline undo
        stack lives on the TimelineView), so the previous
        ``hasattr(self.edit_page, '_command_stack')`` branches in the
        properties-panel handlers were dead code — every clip param /
        effect / transition edit silently bypassed undo and landed only on
        ``edit_page._current_timeline``.  Route through
        ``TimelineView.execute_command`` instead (the single entry that
        keeps the command stack, incremental view update, project cache and
        autosave on one sequence revision — same path as the AUD-2703
        fix).  Hosts without a TimelineView (test shells / degraded
        environments) fall back to a direct apply — unpacking compound
        commands' ``(new_timeline, updated_command)`` tuple — and sync the
        mutated timeline to the views manually.

        Returns the committed timeline, or the pre-edit timeline when the
        view could not execute the command.
        """
        view_getter = getattr(getattr(self, "timeline_panel", None),
                              "get_timeline_view", None)
        view = view_getter() if callable(view_getter) else None
        view_timeline = (
            getattr(view, "_timeline", None) if view is not None else None)
        if (view is not None and view_timeline is not None
                and hasattr(view, "execute_command")):
            configure = getattr(view, "configure_project_transaction_context", None)
            if callable(configure):
                configure(lambda: self._current_edit_project,
                          lambda: self._current_timeline_sequences)
                if getattr(view, "_project_transaction_controller", None) is not self:
                    view.project_timeline_transaction_committed.connect(
                        self._on_edit_project_timeline_transaction_committed)
                    view._project_transaction_controller = self
            view.execute_command(command)
            return getattr(view, "_timeline", None) or timeline
        # A combined transaction must never silently degrade to sequence-only
        # apply when the history/atomic publication bridge is unavailable.
        from src.features.edit.core.timeline_project_transaction import TimelineProjectTransactionCommand
        if isinstance(command, TimelineProjectTransactionCommand):
            raise ValueError("Project transactions require an active timeline history")
        result = command.apply(timeline)
        new_timeline = result if not isinstance(result, tuple) else result[0]
        edit_page = getattr(self, "edit_page", None)
        if edit_page is not None:
            edit_page._current_timeline = new_timeline
        self._sync_timeline_to_views(new_timeline)
        return new_timeline

    def _sync_timeline_to_views(self, timeline: Any) -> None:
        """Sync a mutated timeline into both the preview renderer and TimelineView.

        Both keep their own timeline copies: the renderer holds
        ``worker._timeline_sequence`` (updated only via :meth:`set_sequence`),
        and TimelineView holds ``_timeline`` (updated via :meth:`load_timeline`
        which also refreshes per-clip items — including the fx badge driven
        by ``clip_item._effects``). An effect edit that only updates
        ``edit_page._current_timeline`` leaves both stale: the renderer keeps
        rendering the pre-edit timeline (edits look invisible — e.g. removing
        a blur still shows blur), and the timeline's fx badge / clip
        indicators don't update (removed effect stays marked with fx).
        """
        if self.timeline_preview_renderer is not None:
            self.timeline_preview_renderer.set_sequence(timeline)
        if hasattr(self, 'timeline_panel') and hasattr(self.timeline_panel, 'timeline_view'):
            self.timeline_panel.timeline_view.load_timeline(timeline)

    def _invalidate_clip_render_cache(self, clip_id: str) -> None:
        """FX-H02: invalidate the render cache for a clip after an effect edit.

        Effect edits (add/remove/update/toggle/reorder/keyframe) change the
        rendered output, so any previously-VALID cache for that clip must be
        marked STALE — otherwise the viewer keeps showing the pre-edit frame.
        Adjustment clips affect every clip below them in their time range, so
        an adjustment edit also invalidates those; here we only mark the clip
        itself (the common, scoped case) and rely on the manifest's
        effect_signature mismatch to catch the rest at lookup time.
        """
        project = getattr(self, '_current_edit_project', None)
        if project is None or clip_id is None:
            return
        if not hasattr(project, 'invalidate_render_cache_for_clip'):
            return
        try:
            cleanup = getattr(self, "_cleanup_obsolete_render_cache_files", None)
            if callable(cleanup):
                cleanup(
                    project,
                    clip_id=str(clip_id),
                    include_rendering=False,
                    preserve_valid_fingerprinted=True,
                )
            new_project = project.invalidate_render_cache_for_clip(clip_id)
            if new_project is not project:
                self._current_edit_project = new_project
        except Exception:  # noqa: BLE001 — cache invalidation must never break editing
            logger.debug(f"render-cache invalidation skipped for clip {clip_id}", exc_info=True)

    def _invalidate_motion_graphic_caches_for_timeline_change(
        self, previous_timeline: Any | None, timeline: Any,
    ) -> None:
        """Invalidate MG caches when the timeline output contract changes.

        Trimming, splitting, moving, and duplicating a clip only change which
        range of the same scene recording is used.  Shift-retiming changes the
        recording duration and a dimension/FPS change alters its delivery
        contract; both cases must be rendered again.
        """
        project = getattr(self, "_current_edit_project", None)
        if project is None or previous_timeline is None or timeline is None:
            return
        try:
            from src.features.teaching_animation.host_adapters import is_motion_graphic_asset_id

            old_clips = {
                str(clip.clip_id): clip
                for track in previous_timeline.all_tracks
                for clip in track.clips
            }
            new_clips = {
                str(clip.clip_id): clip
                for track in timeline.all_tracks
                for clip in track.clips
            }
            contract_changed = any(
                getattr(previous_timeline, field, None)
                != getattr(timeline, field, None)
                for field in ("width", "height", "fps")
            )
            # A normal trim/split/move only changes which range of the same
            # scene cache is used.  Shift-retiming is different: the cache's
            # frame count and the scene-to-timeline mapping both change, so
            # it must be rendered again just like a dimension/FPS change.
            duration_changed = {
                clip_id
                for clip_id, new_clip in new_clips.items()
                if clip_id in old_clips
                and is_motion_graphic_asset_id(getattr(new_clip, "asset_id", ""))
                and (
                    int(getattr(new_clip, "timeline_duration", 0))
                    != int(getattr(old_clips[clip_id], "timeline_duration", 0))
                )
            }
            if not contract_changed and not duration_changed:
                return
            candidates: set[str] = duration_changed if not contract_changed else {
                str(clip.clip_id)
                for clip in list(new_clips.values()) + list(old_clips.values())
                if is_motion_graphic_asset_id(getattr(clip, "asset_id", ""))
            }
            if duration_changed and not contract_changed:
                # Timeline duplicates intentionally share a scene recording.
                # Invalidate every instance of the changed scene as a group;
                # otherwise the preview fallback could silently pick a valid
                # sibling manifest and skip the requested retime render.
                changed_assets = {
                    str(new_clips[clip_id].asset_id)
                    for clip_id in duration_changed
                    if clip_id in new_clips
                }
                candidates.update(
                    str(clip.clip_id)
                    for clip in new_clips.values()
                    if str(getattr(clip, "asset_id", "")) in changed_assets
                )
            if not candidates:
                return

            # Do not let an encoder finish an old contract after the timeline
            # format changed. Its output would otherwise overwrite the
            # replacement cache and appear valid again for the edited clip.
            for worker in tuple(
                (getattr(self, "_render_cache_workers", {}) or {}).values()
            ):
                manifest = getattr(worker, "_manifest", None)
                if str(getattr(manifest, "clip_id", "")) not in candidates:
                    continue
                try:
                    worker.cancel()
                except Exception:
                    logger.debug("Could not cancel stale Motion Graphic worker", exc_info=True)

            updated = project
            timeline_id = str(getattr(timeline, "timeline_id", "") or "")
            for clip_id in candidates:
                self._cleanup_obsolete_render_cache_files(
                    updated,
                    clip_id=clip_id,
                    include_rendering=True,
                    preserve_valid_fingerprinted=True,
                )
                updated = updated.invalidate_render_cache_for_clip(
                    timeline_id, clip_id,
                )
            if updated is project:
                return
            self._current_edit_project = updated
            panel = getattr(self, "timeline_panel", None)
            if panel is not None and hasattr(panel, "set_project"):
                panel.set_project(updated)
            media_pool = getattr(self, "media_pool_panel", None)
            if media_pool is not None and hasattr(media_pool, "set_project"):
                media_pool.set_project(updated)
        except Exception:  # noqa: BLE001 — cache invalidation must not block edits
            logger.debug(
                "Motion Graphic timeline-cache invalidation skipped",
                exc_info=True,
            )

    @staticmethod
    def _cleanup_obsolete_render_cache_files(
        project: Any,
        *,
        clip_id: str | None = None,
        scene_id: str | None = None,
        include_rendering: bool = False,
        preserve_valid_fingerprinted: bool = False,
    ) -> None:
        """Delete stale generated cache files while preserving shared paths.

        Render-cache manifests are immutable project data, so file cleanup is
        deliberately performed at the host/controller boundary.  A path is
        removed only when no other manifest references it; active encoders are
        kept unless the caller explicitly confirms it has cancelled them.
        """
        manifests = [
            manifest
            for raw in (getattr(project, "render_cache_manifests", {}) or {}).values()
            if (manifest := _render_cache_manifest_from_value(raw)) is not None
        ]

        def _is_target(manifest: Any) -> bool:
            return (
                (clip_id is None or str(getattr(manifest, "clip_id", "")) == str(clip_id))
                and (scene_id is None or str(getattr(manifest, "scene_id", "")) == str(scene_id))
                and (
                    include_rendering
                    or str(getattr(manifest, "status", ""))
                    != "rendering"
                )
                and not (
                    preserve_valid_fingerprinted
                    and str(getattr(manifest, "status", "")) == "valid"
                    and bool(getattr(manifest, "render_fingerprint", ""))
                )
            )

        target_paths = {
            str(getattr(manifest, "path", "") or "")
            for manifest in manifests
            if _is_target(manifest)
        }
        target_paths.discard("")
        if not target_paths:
            return
        # R2-014：保护集按 manifest 身份（过滤谓词的补集）划分，不得按
        # "路径不在目标集合"排除——与目标共享同一路径的有效 manifest 会因
        # 路径重合被误排除出保护集，共享路径保护恒不生效，删除后留下
        # 悬空 manifest。过滤条件只依赖 manifest 字段，值相等的 manifest
        # 必然落在同侧，谓词补集即"非目标 manifest"的准确刻画。
        referenced_elsewhere = {
            os.path.abspath(str(getattr(manifest, "path", "") or ""))
            for manifest in manifests
            if not _is_target(manifest)
            and str(getattr(manifest, "path", "") or "")
        }
        for path in target_paths:
            if os.path.abspath(path) not in referenced_elsewhere:
                _remove_render_cache_file(path)

    @staticmethod
    def _prune_render_cache_variants(
        project: Any,
        *,
        timeline_id: str,
        clip_id: str,
        max_variants: int = 8,
    ) -> Any:
        """Bound semantic cache history and remove files no longer referenced."""
        prune = getattr(project, "prune_render_cache_variants", None)
        if not callable(prune):
            return project
        try:
            pruned_project, removed_paths = prune(
                timeline_id, clip_id, max_variants=max_variants,
            )
        except (TypeError, ValueError, AttributeError):
            logger.debug("Could not prune render-cache variants", exc_info=True)
            return project
        if removed_paths:
            referenced_paths = {
                os.path.abspath(str(getattr(manifest, "path", "") or ""))
                for raw in (getattr(pruned_project, "render_cache_manifests", {}) or {}).values()
                if (manifest := _render_cache_manifest_from_value(raw)) is not None
                and getattr(manifest, "path", "")
            }
            for path in removed_paths:
                if os.path.abspath(str(path)) not in referenced_paths:
                    _remove_render_cache_file(str(path))
        return pruned_project

    # ------------------------------------------------------------------
    # Add asset to timeline
    # ------------------------------------------------------------------

    def _on_add_asset_to_timeline(self, asset_id: str) -> None:
        """Handle right-click 'Add to Timeline' from MediaPoolPanel."""
        if not self._ensure_edit_project():
            return

        asset = self._current_edit_project.get_asset_by_id(asset_id)
        if asset is None:
            logger.warning(f"Add to timeline: asset {asset_id} not found")
            return

        if getattr(asset, "is_motion_graphic", False):
            # A Motion Graphic is a virtual Media Pool asset.  Resolve its
            # shared scene and insert a normal undoable graphics clip; do not
            # route it through file-backed media append/decode logic.
            from src.features.teaching_animation.host_adapters import (
                MotionGraphicClipCommand,
                motion_graphic_scene_id,
            )
            from src.features.teaching_animation.scene_store import get_shared_scene_store

            scene_id = str(asset.metadata.get("scene_id", "")) or motion_graphic_scene_id(asset_id)
            scene = get_shared_scene_store().get(scene_id) if scene_id else None
            if scene is None:
                logger.warning("Motion Graphic scene not registered: %s", scene_id)
                return
            view = self.timeline_panel.get_timeline_view()
            if getattr(view, "_timeline", None) is None:
                self.timeline_panel.show_provisional_timeline()
            timeline = getattr(view, "_timeline", None)
            if timeline is None:
                logger.warning("Cannot add Motion Graphic: no timeline")
                return
            graphics_track = next(
                (
                    track for track in timeline.video_tracks
                    if getattr(track, "track_role", "") == "graphics"
                ),
                None,
            )
            frame = int(self.timeline_panel.get_current_frame())
            command = MotionGraphicClipCommand(
                scene,
                max(0, frame),
                max(1, int(scene.duration_frames)),
                getattr(graphics_track, "track_id", None),
                "overwrite",
                asset_id=asset.asset_id,
            )
            view.execute_command(command)
            self.edit_project_dirty = True
            logger.info(
                "Added Motion Graphic asset to timeline: %s [%s frames]",
                asset_id,
                scene.duration_frames,
            )
            return

        if asset.media_type == "video":
            total = asset.total_frames
            if total and total > 0:
                in_frame, out_frame = 0, total - 1
            else:
                fps = asset.fps or 24.0
                duration = asset.duration_seconds or 1.0
                total = int(fps * duration)
                in_frame, out_frame = 0, max(0, total - 1)
        elif asset.media_type == "image":
            in_frame, out_frame = 0, 0
        else:
            return

        # E01: 有声视频的音频由 TimelineView.append_clip() 的 A/V 宏一次性
        # 落轨（V+A+双向链接、共同落点）。旧代码在此后又按 audio_track_id
        # 手动追加一次音频，第二次 append 落在与刚插入的 A/V 组相同的音频
        # 轨区间，必然抛 ``ValueError: New clip overlaps`` 且无用户反馈。
        self._append_media_to_timeline_once(asset, asset_id, in_frame, out_frame)

    def _append_media_to_timeline_once(
        self, asset, asset_id: str, in_frame: int, out_frame: int
    ) -> bool:
        """调用一次 ``append_clip`` 并把拒绝原因暴露到状态栏（E01）。

        ``append_clip`` 对有声视频自动带链接音频，对纯音频自动选目标音频轨；
        失败（无目标轨/区间重叠）时 MacroCommand 事务回滚，这里捕获异常转成
        状态栏提示，不再向 Qt 槽外抛。
        """
        try:
            added = self.timeline_panel.append_clip(
                asset_id=asset_id,
                in_frame=in_frame,
                out_frame=out_frame,
            )
        except (TimelineOperationError, ValueError) as exc:
            logger.warning("Add to timeline rejected for %s: %s", asset_id, exc)
            self._show_edit_status_message(f"无法添加到时间线：{exc}")
            return False
        if added:
            logger.info(
                f"Added {asset.media_type} to timeline: {asset_id} [{in_frame}-{out_frame}]"
            )
        else:
            self._show_edit_status_message(
                "无法添加到时间线：没有可用的目标轨道"
            )
        return bool(added)

    def _show_edit_status_message(self, message: str, timeout_ms: int = 5000) -> None:
        """把编辑操作的结构化失败提示写状态栏 + 日志（E01/E04）。"""
        logger.warning(message)
        status_bar = getattr(self, "status_bar", None)
        if status_bar is None:
            status_bar_getter = getattr(self, "statusBar", None)
            status_bar = status_bar_getter() if callable(status_bar_getter) else None
        if status_bar is not None:
            try:
                status_bar.showMessage(message, timeout_ms)
            except Exception:  # noqa: BLE001 状态栏不可用不阻断操作
                pass

    def _on_timeline_status_message(self, message: str) -> None:
        """E04: TimelineView.status_message_requested → 状态栏。

        TimelineView 无状态栏引用，锁轨 trim、Undo/Redo 事务拒绝、
        Slip/Slide 预检拒绝等 18 个发射点此前全部无人接收。错误文本
        同时进日志，避免 5 秒超时后信息丢失。
        """
        self._show_edit_status_message(str(message))

    def _auto_add_media_to_timeline(
        self,
        project,
        asset_id: str,
        timeline_id: str,
    ) -> None:
        """
        Automatically add media asset to the active timeline.

        Called after quick project creation when auto_add_media is enabled.
        """
        asset = project.get_asset_by_id(asset_id)
        if asset is None:
            logger.warning(f"Auto-add: asset {asset_id} not found")
            return

        # Determine frame range
        if asset.media_type == "video":
            total = asset.total_frames
            if total and total > 0:
                in_frame, out_frame = 0, total - 1
            else:
                # Fallback: 1 second at timeline fps
                fps = asset.fps or 24.0
                duration = asset.duration_seconds or 1.0
                total = int(fps * duration)
                in_frame, out_frame = 0, max(0, total - 1)
        elif asset.media_type == "image":
            # Still image: 1 frame (user can extend)
            in_frame, out_frame = 0, 0
        else:
            return

        # E01: 同 _on_add_asset_to_timeline——A/V 宏已在 append_clip 内部
        # 处理，不再二次追加音频。
        self._append_media_to_timeline_once(asset, asset_id, in_frame, out_frame)

    # ------------------------------------------------------------------
    # Edit project file operations
    # ------------------------------------------------------------------

    def _collect_audio_routing_errors(self) -> tuple:
        """P0-02: 收集当前音频路由图校验错误（空元组=图合法）。

        edit_page 在路由门阻断时冻结预览音频在上一张有效图，并把可定位
        错误（含轨道/Bus 名称）暴露在 ``audio_routing_errors``。保存前
        读取并展示，保证“保存工程也显示可定位错误”（计划 P0-02 验收）。
        """
        edit_page = getattr(self, "edit_page", None)
        if edit_page is None:
            return ()
        try:
            errors = edit_page.audio_routing_errors
        except Exception as exc:
            logger.debug("audio_routing_errors read failed: %s", exc)
            return ()
        return tuple(errors or ())

    def _warn_audio_routing_errors_on_save(self) -> None:
        """保存前显示音频路由错误（不阻断保存，只提示可定位问题）。"""
        errors = self._collect_audio_routing_errors()
        if not errors:
            return
        preview = "\n".join(errors[:8])
        if len(errors) > 8:
            preview += f"\n… (+{len(errors) - 8})"
        QMessageBox.warning(
            self,
            I18N.tr("warning.audio_routing_title", "Audio Routing Issues"),
            I18N.tr(
                "warning.audio_routing_save_body",
                "The project contains invalid audio routing. Preview audio is "
                "frozen on the last valid graph.\nThe project will still be "
                "saved.\n\n{errors}",
            ).format(errors=preview),
        )

    def _save_current_project(self) -> None:
        """
        Save current Edit project - 使用 SaveRouter。

        统一保存到 .topos 工程包。
        如果项目未保存过（无 filepath），自动触发另存为对话框。
        """
        if self._current_edit_project is None:
            QMessageBox.warning(
                self,
                I18N.tr("warning.title", "Warning"),
                I18N.tr("warning.no_edit_project", "No edit project to save. Create or open a project first.")
            )
            return

        # Unsaved project → redirect to save-as so user picks name + location
        if not self._current_edit_project_filepath:
            self._save_current_project_as()
            return

        # P0-02：保存前显示音频路由图可定位错误（不阻断保存）
        self._warn_audio_routing_errors_on_save()

        # 使用 SaveRouter 统一保存
        # 临时切换到 Edit 页面以触发正确的路由
        original_page = self.current_page
        self.current_page = "Edit"

        success = self.save_router.save()

        # 恢复原始页面
        self.current_page = original_page

        if success:
            # Reset dirty flag and update window title
            self._clear_all_dirty_states()  # P0-5: Saved successfully, clear all dirty states
            self.setWindowTitle(f"Topos - {self._current_edit_project.name}")

            # 获取保存路径
            project_path = self.save_router.get_current_project_path()
            if project_path:
                self.status_bar.showMessage(
                    I18N.tr("status.edit_project_saved", f"Edit project saved: {os.path.basename(project_path)}"),
                    3000
                )
        else:
            # SaveRouter 失败时，显示错误并保持 dirty 状态
            # 不使用后备路径，确保唯一入口
            QMessageBox.critical(
                self,
                I18N.tr("error.title", "Error"),
                I18N.tr("error.save_project", "Failed to save project.")
            )

    def _save_current_project_as(self) -> None:
        """Save the active project with a new name."""
        if self._current_edit_project is None:
            QMessageBox.warning(
                self,
                I18N.tr("warning.title", "Warning"),
                I18N.tr("warning.no_edit_project", "No edit project to save. Create or open a project first.")
            )
            return

        # Get last save path
        last_path = self.config_manager.get("last_project_path") or ""

        # Generate default filename from project name
        default_filename = self._current_edit_project.name + EDIT_PROJECT_EXTENSION

        filepath, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr("action.save_project_as", "Save Project As..."),
            os.path.join(last_path, default_filename),
            f"Topos Projects (*{EDIT_PROJECT_EXTENSION});;All Files (*)"
        )

        if filepath:
            # Ensure the user-selected path has exactly one .topos suffix.
            from src.shared.project.library import ensure_project_path
            filepath = ensure_project_path(filepath)

            # P0-02：保存前显示音频路由图可定位错误（不阻断保存）
            self._warn_audio_routing_errors_on_save()

            # Get UI state and timeline sequences
            ui_state = self._get_edit_ui_state()
            timeline_sequences = self._get_timeline_sequences()

            # Save project
            success = self.edit_project_io.save_project_as(
                filepath,
                self._current_edit_project,
                ui_state,
                timeline_sequences,
                force_project_mode=(
                    "edit_project"
                    if self._get_work_mode() == "video"
                    else "standalone_image"
                ),
            )

            if success:
                # Update state
                self._current_edit_project_filepath = filepath
                self.save_router.set_project_path(filepath)
                self._sync_mg_preset_project_root(filepath)
                self._clear_all_dirty_states()  # P0-5: Saved as successfully, clear all dirty states

                # Add to recent projects
                self._add_to_recent_projects(filepath)
                self.config_manager.set("last_project_path", os.path.dirname(filepath))
                self.config_manager.save_config()

                # Update window title
                self.setWindowTitle(f"Topos - {self._current_edit_project.name}")

                # Update status
                self.status_bar.showMessage(
                    I18N.tr("status.edit_project_saved_as", f"Edit project saved: {os.path.basename(filepath)}"),
                    3000
                )

                logger.info(f"Edit project saved as: {filepath}")
            else:
                QMessageBox.critical(
                    self,
                    I18N.tr("error.title", "Error"),
                    I18N.tr("error.save_project", "Failed to save project.")
                )

    def _close_current_project(self) -> bool:
        """Close the active project through the unified lifecycle."""
        if self._current_edit_project is None:
            return True

        if self._has_unsaved_changes():
            reply = QMessageBox.question(
                self,
                I18N.tr("dialog.unsaved_changes", "Unsaved Changes"),
                I18N.tr(
                    "dialog.unsaved_changes_message",
                    "The project has unsaved changes.\nDo you want to save before closing?"
                ),
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Save:
                self._save_current_project()
                if self._has_unsaved_changes():
                    return False
            elif reply == QMessageBox.StandardButton.Cancel:
                return False

        # Stop source playback without routing another save after the project
        # reference is cleared.  This is important when closing a timeline
        # project while its Color page still displays a source clip.
        if hasattr(self, "unload_video"):
            self.unload_video(save_before_unload=False)

        # Closing a project is an explicit decision to leave it. Its recovery
        # snapshot must not survive into a later open, otherwise a user who
        # chose Discard is prompted to restore those discarded changes. Detach
        # the long-lived autosave manager before another project becomes active.
        project_filepath = self._current_edit_project_filepath
        edit_project_io = getattr(self, "edit_project_io", None)
        if edit_project_io is not None:
            if project_filepath:
                edit_project_io.discard_autosave(project_filepath)
            edit_project_io.set_current_filepath(None)

        # Clear state
        # M1.2：拆除 AI 装配（撤销 grants、unbind session）—— 必须在清 project 之前，
        # 因为 teardown 需要 project_id 撤销对应 grants。
        self._teardown_ai_editing()
        # 空闲 MG 缓存调度随工程一起拆除：停掉定时器并取消在跑的空闲录制。
        # 部分拼装的宿主（测试替身）没有 MG 缓存装配，按定时器同样的
        # 防御式访问处理。
        cancel_idle_renders = getattr(
            self, "_cancel_idle_mg_cache_renders", None
        )
        if callable(cancel_idle_renders):
            cancel_idle_renders()
        idle_timer = getattr(self, "_mg_idle_cache_timer", None)
        if idle_timer is not None:
            idle_timer.stop()
        scene_cut_controller = getattr(
            getattr(self, "edit_page", None),
            "_scene_cut_controller",
            None,
        )
        if scene_cut_controller is not None:
            try:
                scene_cut_controller.close_active_dialog()
                scene_cut_controller.clear_session_cache()
            except Exception:
                logger.debug("Failed to clear scene-cut session cache", exc_info=True)
        self._current_edit_project = None
        self._current_edit_project_filepath = None
        self._current_timeline_sequences = {}
        # M19.2b：工程关闭即清空进程级色彩覆盖层——上一工程的作者
        # 色彩设定不得泄漏到无工程/下一工程状态（回退用户全局偏好）。
        try:
            from src.shared.color.color_context_lifecycle import (
                on_project_closed as _clear_color_context,
            )
            _clear_color_context()
        except ImportError:
            logger.debug(
                "color_context_lifecycle unavailable; project color "
                "overrides left as-is on close",
            )
        self.save_router.clear_cache()
        self._sync_mg_preset_project_root(None)

        # Clear panels
        self.media_pool_panel.clear_project()
        self.timeline_panel.clear()

        # Reset window title
        self.setWindowTitle(I18N.tr("app.brand", "Topos"))

        # Update status
        self.status_bar.showMessage(
            I18N.tr("status.edit_project_closed", "Edit project closed."),
            3000
        )

        logger.info("Edit project closed")
        return True

    # ================================================================
    # Phase 2: Explicit Comp Clip handlers
    # ================================================================

    # ------------------------------------------------------------------
    # P1-02: 嵌套序列（Nested Sequence / Compound Clip）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # P1-01: 多机位（Multicam）
    # ------------------------------------------------------------------

    def _on_create_multicam_group(self, asset_ids: list) -> None:
        """媒体池多选 → 创建多机位组并在 playhead 插入多机位片段。

        组定义序列化进每个成员素材的 metadata（asset-carried），时间线
        片段直接用角度 0 的真实素材——渲染零改动，后续 split/trim/换源
        全部经既有命令保持组归属。插入用 InsertClipCommand（可撤销，
        同步锁轨道跨轨跟随）。
        """
        if not self._current_edit_project or not asset_ids:
            return
        timeline = getattr(self.edit_page, "_current_timeline", None) if self.edit_page else None
        if timeline is None:
            return

        # Resolve the UI/track and build the complete immutable insertion
        # before publishing any group metadata. Rejected creation is zero-write.
        tview = self.timeline_panel.get_timeline_view() if self.timeline_panel else None
        target_track = next((track for track in timeline.video_tracks if not track.is_locked), None)
        if tview is None or target_track is None:
            self.status_bar.showMessage(
                I18N.tr("status.multicam_no_track", "No unlocked video track"), 4000)
            return
        from src.features.edit.core.multicam import plan_multicam_insert
        from src.features.edit.core.timeline_project_transaction import TimelineProjectTransactionCommand
        project_before = self._current_edit_project
        playhead = int(getattr(self.edit_page, "_current_frame", 0) or 0)
        try:
            plan = plan_multicam_insert(
                project_before, timeline, asset_ids, target_track.track_id, playhead)
            after_sequence, _prepared = plan.command.apply(timeline)
            after_project = project_before
            for asset in plan.assets:
                after_project = after_project.with_media_asset_added(asset)
            command = TimelineProjectTransactionCommand.create(
                timeline, after_sequence,
                before_project=project_before, after_project=after_project,
                label="Create Multicam Group")
            self._execute_timeline_edit_command(command, timeline)
        except ValueError as exc:
            self.status_bar.showMessage(str(exc), 4000)
            return
        # The view bridge publishes sequence and project deltas together and
        # keeps them in the same history as subsequent cuts/trims.
        published = getattr(self.edit_page, "_current_timeline", None)
        # F2.5：插入命令两种契约——InsertClipCommand 带 _inserted_clip_id，
        # A/V 波纹命令带 _video_clip_id（apply 返回裸 sequence 的同型）。
        inserted_clip_id = getattr(
            plan.command, "_inserted_clip_id",
            getattr(plan.command, "_video_clip_id", None))
        if published is None or inserted_clip_id is None \
                or published.get_clip_by_id(inserted_clip_id) is None:
            return
        group = plan.group

        self._refresh_multicam_panel()
        self._on_multicam_playhead_frame(
            int(getattr(self.edit_page, "_current_frame", 0) or 0)
            if self.edit_page else 0)
        self.status_bar.showMessage(
            I18N.tr(
                "status.multicam_created",
                f"Multicam group created: {group.name} (switch with 1-{min(9, len(group.angles))})",
            ),
            5000,
        )
        logger.info(
            "P1-01 multicam group created: %s angles=%d playhead=%d",
            group.group_id, len(group.angles), playhead,
        )

    def _multicam_clip_at_playhead(self, group_id: str = ""):
        """playhead 覆盖的多机位视频片段（可指定组）；无则 None。"""
        timeline = getattr(self.edit_page, "_current_timeline", None) if self.edit_page else None
        if timeline is None:
            return None
        playhead = int(getattr(self.edit_page, "_current_frame", 0) or 0)
        from src.features.edit.core.multicam import multicam_group_from_asset
        for track in timeline.video_tracks:
            for clip in track.clips:
                if clip.timeline_start <= playhead < clip.timeline_end:
                    asset = self._current_edit_project.assets.get(clip.asset_id) \
                        if self._current_edit_project else None
                    group = multicam_group_from_asset(asset) if asset is not None else None
                    if group is not None and (not group_id or group.group_id == group_id):
                        return clip
        return None

    def _switch_multicam_angle(
        self, group_id: str, angle_asset_id: str, sync_offset: int = 0,
    ) -> None:
        """在 playhead 把多机位片段切到指定角度（可撤销的切换点）。"""
        clip = self._multicam_clip_at_playhead(group_id)
        if clip is None:
            self.status_bar.showMessage(
                I18N.tr(
                    "status.multicam_no_clip",
                    "No multicam clip under the playhead",
                ),
                3000,
            )
            return
        if clip.asset_id == angle_asset_id and clip.source_start == max(0, sync_offset):
            return  # 已在该角度（同偏移）——无操作

        from src.features.edit.core.commands import SwitchMulticamAngleCommand
        from src.features.edit.core.multicam import multicam_group_from_asset
        playhead = int(getattr(self.edit_page, "_current_frame", 0) or 0)
        label = ""
        if self._current_edit_project:
            asset = self._current_edit_project.assets.get(angle_asset_id)
            label = getattr(asset, "display_name", "") or ""
        # FE-B06：解析当前角度（正在播放素材）在组里的同步偏移——切角
        # 换算要先减它得到组公共时间，否则偏移逐次累计。
        active_sync_offset = 0
        if self._current_edit_project:
            current_asset = self._current_edit_project.assets.get(clip.asset_id)
            current_group = (
                multicam_group_from_asset(current_asset)
                if current_asset is not None else None
            )
            if current_group is not None:
                for angle in current_group.angles:
                    if angle.asset_id == clip.asset_id:
                        active_sync_offset = angle.sync_offset
                        break
        # F-R04/R23：目标角度 fps 元数据随切换换装（24fps 组切 48fps
        # 角度不再沿用旧时基）
        angle_fps_num = angle_fps_den = None
        if self._current_edit_project:
            angle_asset = self._current_edit_project.assets.get(angle_asset_id)
            angle_fps = getattr(angle_asset, "fps", None) if angle_asset else None
            if angle_fps and angle_fps > 0:
                from fractions import Fraction as _Fr

                _frac = _Fr(float(angle_fps)).limit_denominator(100000)
                angle_fps_num = _frac.numerator
                angle_fps_den = _frac.denominator
        command = SwitchMulticamAngleCommand.create(
            track_id=clip.track_id,
            clip_id=clip.clip_id,
            switch_frame=playhead,
            angle_asset_id=angle_asset_id,
            angle_sync_offset=sync_offset,
            angle_label=label,
            active_sync_offset=active_sync_offset,
            angle_fps_num=angle_fps_num,
            angle_fps_den=angle_fps_den,
        )
        # F2.5：音频跟随模式——链接音频片段与视频同帧切到目标角度
        # （同一宏单步撤销；目标角度无音轨/无链接对则只切视频）。
        commands = [command]
        timeline = getattr(self.edit_page, "_current_timeline", None) \
            if self.edit_page else None
        if timeline is not None and clip.linked_clip_id \
                and self._current_edit_project is not None:
            group = None
            if current_group is not None:
                group = current_group
            else:
                target_asset = self._current_edit_project.assets.get(
                    angle_asset_id)
                group = (
                    multicam_group_from_asset(target_asset)
                    if target_asset is not None else None)
            policy = getattr(group, "audio_policy", "fixed_main")
            target_asset = self._current_edit_project.assets.get(angle_asset_id)
            target_has_audio = bool(
                getattr(target_asset, "audio_channels", None)) \
                if target_asset is not None else False
            if policy == "follow_angle" and target_has_audio:
                audio_clip = timeline.get_clip_by_id(clip.linked_clip_id)
                if audio_clip is not None:
                    commands.append(SwitchMulticamAngleCommand.create(
                        track_id=audio_clip.track_id,
                        clip_id=audio_clip.clip_id,
                        switch_frame=playhead,
                        angle_asset_id=angle_asset_id,
                        angle_sync_offset=sync_offset,
                        angle_label=label,
                        active_sync_offset=active_sync_offset,
                        angle_fps_num=angle_fps_num,
                        angle_fps_den=angle_fps_den,
                    ))
        if len(commands) == 1:
            final_command = command
        else:
            from src.features.edit.core.commands import MacroCommand
            final_command = MacroCommand.create("Switch angle (A/V)", commands)
        tview = self.timeline_panel.get_timeline_view() if self.timeline_panel else None
        if tview is None:
            return
        try:
            tview.execute_command(final_command)
        except ValueError as exc:
            self.status_bar.showMessage(str(exc), 4000)
        finally:
            # The angle button can highlight optimistically. A locked-track
            # or unsupported-timebase refusal must restore the model's angle.
            self._refresh_multicam_panel()
            self._on_multicam_playhead_frame(
                int(getattr(self.edit_page, "_current_frame", 0) or 0)
                if self.edit_page else 0)

    def _on_multicam_angle_key(self, index: int) -> None:
        """P1-01: 键盘 1-9 切换 playhead 多机位片段的角度。"""
        clip = self._multicam_clip_at_playhead()
        if clip is None or not self._current_edit_project:
            return
        from src.features.edit.core.multicam import multicam_group_from_asset
        asset = self._current_edit_project.assets.get(clip.asset_id)
        group = multicam_group_from_asset(asset) if asset is not None else None
        if group is None or not (1 <= index <= len(group.angles)):
            return
        angle = group.angles[index - 1]
        self._switch_multicam_angle(group.group_id, angle.asset_id, angle.sync_offset)

    def _refresh_multicam_panel(self) -> None:
        """按 playhead 多机位片段刷新角度面板（组 + 当前角度高亮 + 取帧信息）。"""
        panel = getattr(self, "multicam_panel", None)
        if panel is None:
            return
        from src.features.edit.core.multicam import multicam_group_from_asset
        clip = self._multicam_clip_at_playhead()
        group = None
        active_asset_id = ""
        asset_infos: dict = {}
        if clip is not None and self._current_edit_project:
            asset = self._current_edit_project.assets.get(clip.asset_id)
            group = multicam_group_from_asset(asset) if asset is not None else None
            active_asset_id = clip.asset_id
            if group is not None:
                for angle in group.angles:
                    ma = self._current_edit_project.assets.get(angle.asset_id)
                    if ma is not None:
                        asset_infos[angle.asset_id] = {
                            "path": ma.path,
                            "total_frames": int(ma.total_frames or 0),
                        }
        panel.set_group(group, active_asset_id, asset_infos)

    def _on_multicam_playhead_frame(self, frame: int) -> None:
        """playhead 变化 → 角度网格各角度应显示的源帧（面板内去抖合并）。

        帧号映射与切换命令语义严格一致：当前片段 source_start 已含当前
        角度的 sync_offset，目标角度 = source_start + (p - start)
        + (target_offset - active_offset)。
        """
        panel = getattr(self, "multicam_panel", None)
        if panel is None or not self._current_edit_project:
            return
        clip = self._multicam_clip_at_playhead()
        if clip is None:
            return
        from src.features.edit.core.multicam import (
            angle_by_asset_id,
            multicam_group_from_asset,
            map_angle_source_frame,
        )
        from fractions import Fraction
        from src.features.edit.core.media_replacement import source_fps_from_metadata
        asset = self._current_edit_project.assets.get(clip.asset_id)
        group = multicam_group_from_asset(asset) if asset is not None else None
        if group is None:
            return
        active_angle = angle_by_asset_id(group, clip.asset_id)
        active_offset = active_angle.sync_offset if active_angle else 0
        active_fps = (
            Fraction(clip._source_fps_num, clip._source_fps_den or 1)
            if clip._source_fps_num else source_fps_from_metadata(asset.metadata))
        source_frame = clip.timeline_to_source_frame(int(frame))
        frames: dict = {}
        infos: dict = {}
        for angle in group.angles:
            ma = self._current_edit_project.assets.get(angle.asset_id)
            if ma is None:
                continue
            total = int(ma.total_frames or 0)
            mapped = map_angle_source_frame(
                source_frame,
                active_offset,
                angle.sync_offset,
                active_fps,
                source_fps_from_metadata(ma.metadata),
            )
            frames[angle.asset_id] = min(mapped, total - 1) if total > 0 else 0
            infos[angle.asset_id] = {"path": ma.path, "total_frames": total}
        panel.request_angle_frames(frames, infos, timeline_frame=int(frame))

    # ------------------------------------------------------------------
    # P1-01: 多机位波形自动同步
    # ------------------------------------------------------------------

    #: F2.6：每段分析时长与取消入口
    _SYNC_SEGMENT_SECONDS = 30.0

    def _cancel_multicam_sync(self) -> None:
        """F2.6：分析中途取消——requestInterruption 后零写入。"""
        worker = getattr(self, "_multicam_sync_worker", None)
        if worker is None:
            return
        worker.requestInterruption()
        self.status_bar.showMessage(
            I18N.tr("status.multicam_sync_cancelling", "正在取消多机位同步…"),
            3000,
        )

    def _on_multicam_tc_sync_requested(self, reference_asset_id: str) -> None:
        """F2.6：时间码同步——无开头共同音频时的替代方式。

        读各角度素材 metadata 的 timecode（探测自流/容器/tmcd 轨）求差，
        结果同样经确认对话框预览再事务提交；缺 TC/解析失败按错误行呈现
        且不写回。即时计算（无音频解码，无 worker）。
        """
        clip = self._multicam_clip_at_playhead()
        if clip is None or not self._current_edit_project:
            return
        from src.features.edit.core.multicam import multicam_group_from_asset
        asset = self._current_edit_project.assets.get(clip.asset_id)
        group = multicam_group_from_asset(asset) if asset is not None else None
        if group is None or len(group.angles) < 2:
            return
        if not any(a.asset_id == reference_asset_id for a in group.angles):
            return
        timeline_fps = float(
            getattr(self.edit_page, "_fps", 0) or 0) \
            if self.edit_page else 0.0
        from src.features.edit.core.multicam_sync import (
            compute_timecode_alignments,
        )
        metadata_by_asset = {}
        fps_by_asset = {}
        for angle in group.angles:
            ma = self._current_edit_project.assets.get(angle.asset_id)
            metadata_by_asset[angle.asset_id] = (
                getattr(ma, "metadata", {}) if ma is not None else {})
            fps_by_asset[angle.asset_id] = float(
                getattr(ma, "fps", 0) or 0) or timeline_fps or 24.0
        alignments = compute_timecode_alignments(
            metadata_by_asset, reference_asset_id, fps_by_asset)
        payload = {
            aid: {
                "offset_frames": align.offset_frames,
                "lag_seconds": align.lag_seconds,
                "confidence": align.confidence,
                "error": align.error,
            }
            for aid, align in alignments.items()
        }
        from src.features.edit.ui.multicam_sync_result_dialog import (
            MulticamSyncResultDialog,
            MulticamSyncResultRow,
        )
        rows = []
        for angle in group.angles:
            item = payload.get(angle.asset_id, {})
            rows.append(MulticamSyncResultRow(
                asset_id=angle.asset_id,
                label=angle.label or angle.asset_id[:8],
                offset_frames=int(item.get("offset_frames", 0)),
                lag_seconds=float(item.get("lag_seconds", 0.0)),
                confidence=float(item.get("confidence", 0.0)),
                error=str(item.get("error", "") or ""),
            ))
        from PyQt6.QtWidgets import QWidget as _QWidget
        dialog = MulticamSyncResultDialog(
            rows, parent=self if isinstance(self, _QWidget) else None)
        if dialog.exec() != MulticamSyncResultDialog.DialogCode.Accepted:
            self.status_bar.showMessage(
                I18N.tr("status.multicam_sync_aborted", "已放弃本次时间码同步结果"),
                4000,
            )
            return
        self._apply_multicam_sync_offsets(group, payload)

    def _on_bind_external_audio(self, video_asset_id: str) -> None:
        """F2.10：视频素材绑定外录 WAV（波形/TC/手动 + 确认 + 单事务）。

        非破坏：只写 project.audio_sync_bindings（UpdateAudioSyncBindingCommand
        单步撤销）；低置信/反相/歧义/采样率不可归一 → 拒绝绑定并给原因；
        对话框取消零写入。波形法同步解码（30s×2 段，秒级；后台 worker
        登记后续片）。
        """
        if not self._current_edit_project:
            return
        project = self._current_edit_project
        video = project.assets.get(video_asset_id)
        if video is None or getattr(video, "media_type", "") != "video":
            return
        audio_assets = [
            (a.asset_id, a.display_name or a.asset_id[:8])
            for a in project.assets.values()
            if getattr(a, "media_type", "") == "audio"
        ]
        if not audio_assets:
            self.status_bar.showMessage(
                I18N.tr("status.bind_audio_no_wav",
                        "媒体池中没有音频素材——请先导入外录 WAV"), 4000)
            return
        from PyQt6.QtWidgets import QInputDialog
        labels = [label for _aid, label in audio_assets]
        label, ok = QInputDialog.getItem(
            None, I18N.tr("status.bind_audio_pick", "选择外录音频素材"),
            "WAV:", labels, 0, editable=False)
        if not ok:
            return
        wav_asset_id = next(
            aid for aid, lbl in audio_assets if lbl == label)
        wav = project.assets.get(wav_asset_id)
        method, ok = QInputDialog.getItem(
            None, I18N.tr("status.bind_audio_method", "同步方式"),
            I18N.tr("status.bind_audio_method_prompt", "方式："),
            ["波形（自动）", "时间码", "手动输入偏移"], 0, editable=False)
        if not ok:
            return
        offset_seconds = 0.0
        confidence = 1.0
        if method.startswith("手动"):
            offset_seconds, ok = QInputDialog.getDouble(
                None, I18N.tr("status.bind_audio_offset", "WAV 提前开录量"),
                I18N.tr("status.bind_audio_offset_prompt",
                        "提前量（秒，WAV 更早开录为正）："),
                0.0, -86400.0, 86400.0, 3)
            if not ok:
                return
        elif method.startswith("时间码"):
            from src.features.edit.core.audio_sync import (
                timecode_seconds_from_meta,
                wav_timecode_seconds,
            )
            wav_tc = wav_timecode_seconds(wav, wav.path)
            video_meta = getattr(video, "metadata", None) or {}
            # M03（S06）：视频侧 TC 与 WAV 侧同一折算（帧率随素材
            # metadata.fps，缺省 25——此前硬编码 /25.0）
            video_tc = timecode_seconds_from_meta(video_meta)
            if wav_tc is None or video_tc is None:
                self.status_bar.showMessage(I18N.tr(
                    "status.bind_audio_no_tc",
                    "时间码不可用（视频或 WAV 缺 TC/bext）——请用波形或手动"),
                    5000)
                return
            # M03（S06）：offset 契约 = "WAV 提前开录为正"（视频第 t 秒
            # 内容出现在 WAV 第 t+offset 秒）。WAV 开录更早 → WAV 的 TC
            # 更小 → offset = video_tc - wav_tc（摄影机晚 10 秒开录得
            # 正 10s；此前 wav_tc - video_tc 方向相反）。
            offset_seconds = video_tc - wav_tc
        else:
            from src.features.edit.core.multicam_sync import (
                compute_audio_binding_alignment,
            )
            alignment = compute_audio_binding_alignment(
                video.path, wav.path,
                max_shift_seconds=300.0,
                segment_starts=(0.0, 30.0))
            if alignment.error or alignment.rejection:
                self.status_bar.showMessage(I18N.tr(
                    "status.bind_audio_rejected",
                    "波形对齐不可靠，未绑定：{reason}",
                ).format(reason=alignment.error or alignment.rejection), 6000)
                return
            offset_seconds = alignment.lag_seconds
            confidence = abs(alignment.confidence)

        from PyQt6.QtWidgets import QMessageBox
        existing = project.audio_sync_bindings.get(video_asset_id)
        if existing is not None:
            reply = QMessageBox.question(
                None, I18N.tr("status.bind_audio_replace", "已有绑定"),
                I18N.tr(
                    "status.bind_audio_replace_prompt",
                    "该素材已有外录绑定，替换为新结果？"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if reply != QMessageBox.StandardButton.Yes:
                return
        keep_reply = QMessageBox.question(
            None, I18N.tr("status.bind_audio_title", "确认绑定"),
            I18N.tr(
                "status.bind_audio_confirm",
                "绑定外录音频：{name}\n提前开录 {offset:.3f}s（置信度 {conf:.2f}）\n"
                "\n保留机内参考声？（否=播放/导出音频切换到外录）",
            ).format(name=label, offset=offset_seconds, conf=confidence),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.No)
        if keep_reply == QMessageBox.StandardButton.Cancel:
            return
        keep_camera = keep_reply == QMessageBox.StandardButton.Yes

        from src.features.edit.core.audio_sync import AudioSyncBinding
        from src.features.edit.core.commands import UpdateAudioSyncBindingCommand
        binding = AudioSyncBinding(
            wav_asset_id=wav_asset_id,
            offset_seconds=offset_seconds,
            method="manual" if method.startswith("手动")
            else ("timecode" if method.startswith("时间码") else "waveform"),
            keep_camera_audio=keep_camera,
            confidence=confidence)
        pool = getattr(self, "media_pool_panel", None)
        if pool is None:
            return
        try:
            new_project = pool.get_command_stack().execute(
                UpdateAudioSyncBindingCommand.create(
                    video_asset_id, binding), project)
        except ValueError as exc:
            self.status_bar.showMessage(str(exc), 5000)
            return
        self._current_edit_project = new_project
        self.edit_project_dirty = True
        pool.set_project(new_project)
        pool.project_changed.emit(new_project)
        self.status_bar.showMessage(I18N.tr(
            "status.bind_audio_done",
            "已绑定外录音频（{method}，提前 {offset:.3f}s，可撤销）",
        ).format(method=binding.method, offset=offset_seconds), 5000)

    def _on_interpretation_applied(self, asset_ids: tuple, fps: float) -> None:
        """F2.7：解释覆盖已提交——提示把帧率解释应用到当前时间线片段。

        重锚语义 = DaVinci "Use frame rate"：保持源帧窗，仅改 fps 快照并
        重算时间线时长（fade 钳制）；单 MacroCommand 单步撤销。其他时间
        线的片段需各自打开后应用（跨时间线单事务在本架构不存在，诚实
        提示而非静默）。
        """
        panel = getattr(self, "timeline_panel", None)
        view_getter = getattr(panel, "get_timeline_view", None)
        timeline_view = view_getter() if callable(view_getter) else None
        timeline = getattr(self.edit_page, "_current_timeline", None) \
            if self.edit_page else None
        if timeline_view is None or timeline is None:
            return
        from src.features.edit.core.commands import (
            MacroCommand,
            UpdateClipFpsInterpretationCommand,
        )
        from src.features.edit.core.interpretation import fps_as_fraction

        fps_num, fps_den = fps_as_fraction(fps)
        targets = [
            (track.track_id, clip)
            for track in timeline.all_tracks
            for clip in track.clips
            if clip.asset_id in asset_ids
        ]
        if not targets:
            return
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            None,
            I18N.tr("edit.interpret.apply_title", "应用到既有片段"),
            I18N.tr(
                "edit.interpret.apply_prompt",
                "将 {fps} fps 解释应用到当前时间线的 {n} 个片段？"
                "（保持源帧窗，按新解释重算时长；单步撤销）",
            ).format(fps=fps, n=len(targets)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        commands = [
            UpdateClipFpsInterpretationCommand.create(
                track_id, clip.clip_id, fps_num, fps_den)
            for track_id, clip in targets
        ]
        try:
            timeline_view._execute_command(
                MacroCommand.create("按解释重锚片段时基", commands))
        except ValueError as exc:
            self.status_bar.showMessage(str(exc), 4000)
            return
        self.status_bar.showMessage(I18N.tr(
            "edit.interpret.applied",
            "已将 {n} 个片段重锚为 {fps} fps（可撤销）",
        ).format(n=len(commands), fps=fps), 4000)

    def _on_multicam_sync_requested(self, reference_asset_id: str) -> None:
        """角度面板"波形同步"：后台互相关各角度音频 → 偏移写回组。"""
        if getattr(self, "_multicam_sync_worker", None) is not None:
            return  # 已在同步中
        clip = self._multicam_clip_at_playhead()
        if clip is None or not self._current_edit_project:
            return
        from src.features.edit.core.multicam import multicam_group_from_asset
        asset = self._current_edit_project.assets.get(clip.asset_id)
        group = multicam_group_from_asset(asset) if asset is not None else None
        if group is None or len(group.angles) < 2:
            return

        import os
        timeline_fps = float(
            getattr(self.edit_page, "_fps", 0) or 0) \
            if self.edit_page else 0.0
        from src.features.edit.core.multicam_sync import (
            MulticamSyncJob,
            MulticamSyncWorker,
        )
        jobs: list = []
        for angle in group.angles:
            ma = self._current_edit_project.assets.get(angle.asset_id)
            path = getattr(ma, "path", "") if ma is not None else ""
            if not path or not os.path.exists(path):
                continue
            fps = float(getattr(ma, "fps", 0) or 0) or timeline_fps or 24.0
            jobs.append(MulticamSyncJob(
                asset_id=angle.asset_id, path=path, fps=fps))
        if len(jobs) < 2 or not any(
                j.asset_id == reference_asset_id for j in jobs):
            self.status_bar.showMessage(
                I18N.tr(
                    "status.multicam_sync_no_audio",
                    "波形同步需要至少两个可读取音轨的角度素材",
                ),
                4000,
            )
            return

        panel = getattr(self, "multicam_panel", None)
        from src.features.edit.core.multicam import MulticamSyncIdentity
        try:
            identity = MulticamSyncIdentity.capture(self._current_edit_project, group)
        except ValueError as exc:
            self.status_bar.showMessage(str(exc), 4000)
            return
        # F2.6：长录制首/中/末三段采样（段起点按各素材时长中位数），
        # 多段一致才写回；发散 → 速率漂移诊断拒绝（不伪称已同步）。
        durations = [
            float((getattr(
                self._current_edit_project.assets.get(j.asset_id),
                "metadata", {}) or {}).get("duration") or 0.0)
            for j in jobs
        ]
        known = [d for d in durations if d > self._SYNC_SEGMENT_SECONDS * 4]
        segment_starts: tuple = ()
        if known:
            span = min(known)
            tail = max(0.0, span - self._SYNC_SEGMENT_SECONDS)
            segment_starts = tuple(sorted({
                0.0, round(tail / 2.0, 3), round(tail, 3)}))
        request_id = uuid.uuid4().hex
        self._multicam_sync_request = (request_id, identity)
        worker = MulticamSyncWorker(
            jobs, reference_asset_id, segment_starts=segment_starts)
        worker.sync_finished.connect(
            lambda payload, token=request_id: self._on_multicam_sync_done(payload, token))
        worker.sync_failed.connect(
            lambda message, token=request_id: self._on_multicam_sync_failed(message, token))
        self._multicam_sync_worker = worker
        # 快照发起时的组素材：同步是长耗时后台任务，期间用户可能把播放
        # 头挪离多机位片段——完成回调若重推播放头会找不到组而把几分钟的
        # 计算结果静默丢弃。
        self._multicam_sync_group_asset_id = clip.asset_id
        if panel is not None:
            panel.set_sync_running(True)
            panel.store_sync_worker(worker)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        logger.info(
            "P1-01 multicam waveform sync started: group=%s angles=%d ref=%s",
            group.group_id, len(jobs), reference_asset_id,
        )

    def _on_multicam_sync_done(self, payload: dict, request_id: str | None = None) -> None:
        """Apply only a current request, after the result dialog is accepted."""
        request = getattr(self, "_multicam_sync_request", None)
        if request_id is not None and (request is None or request[0] != request_id):
            return  # A late callback must not clear a newer worker or its UI.
        self._multicam_sync_worker = None
        panel = getattr(self, "multicam_panel", None)
        if panel is not None:
            panel.set_sync_running(False)
            panel.store_sync_worker(None)

        if request is not None and not request[1].matches(self._current_edit_project):
            self._multicam_sync_request = None
            self._multicam_sync_group_asset_id = None
            logger.info("Discarded multicam sync after source/project changed")
            return

        # 用发起时的快照定位组（播放头可能已离开多机位片段）；
        # 快照丢失（旧会话兼容）才回退按播放头重推。
        group_asset_id = getattr(self, "_multicam_sync_group_asset_id", None)
        self._multicam_sync_group_asset_id = None
        if group_asset_id and self._current_edit_project:
            group_asset = self._current_edit_project.assets.get(group_asset_id)
        else:
            clip = self._multicam_clip_at_playhead()
            group_asset = (
                self._current_edit_project.assets.get(clip.asset_id)
                if clip is not None and self._current_edit_project else None)
        if group_asset is None or not self._current_edit_project:
            return
        from src.features.edit.core.multicam import (
            multicam_group_from_asset,
        )
        group = multicam_group_from_asset(group_asset)
        if group is None:
            return

        # C4: 结果确认行（含出界警告）——|offset| 超过素材总帧数时标注
        from src.features.edit.ui.multicam_sync_result_dialog import (
            MulticamSyncResultDialog,
            MulticamSyncResultRow,
        )
        rows: list = []
        for angle in group.angles:
            item = payload.get(angle.asset_id) or {}
            ma = self._current_edit_project.assets.get(angle.asset_id)
            total_frames = int(getattr(ma, "total_frames", 0) or 0)
            offset = int(item.get("offset_frames", 0))
            rows.append(MulticamSyncResultRow(
                asset_id=angle.asset_id,
                label=angle.label or angle.asset_id[:8],
                offset_frames=offset,
                lag_seconds=float(item.get("lag_seconds", 0.0)),
                confidence=float(item.get("confidence", 0.0)),
                # F2.6：rejection（低置信/反相/歧义/漂移）按错误行呈现，
                # Apply 侧 filter_writable_offsets 同步拒绝写回
                error=str(
                    item.get("error", "") or item.get("rejection", "") or ""),
                out_of_range=(
                    total_frames > 0 and abs(offset) >= total_frames
                ),
            ))
        from PyQt6.QtWidgets import QWidget as _QWidget
        dialog = MulticamSyncResultDialog(
            rows, parent=self if isinstance(self, _QWidget) else None
        )
        if dialog.exec() != MulticamSyncResultDialog.DialogCode.Accepted:
            if getattr(self, "_multicam_sync_request", None) == request:
                self._multicam_sync_request = None
            self.status_bar.showMessage(
                I18N.tr(
                    "status.multicam_sync_discarded",
                    "已放弃本次波形同步结果",
                ),
                4000,
            )
            return
        # Modal dialogs run an event loop: sources/project may change while
        # the user reads the result, so validate the same snapshot a second time.
        if request is not None and (
                getattr(self, "_multicam_sync_request", None) != request
                or not request[1].matches(self._current_edit_project)):
            if getattr(self, "_multicam_sync_request", None) == request:
                self._multicam_sync_request = None
            return
        self._multicam_sync_request = None
        self._apply_multicam_sync_offsets(group, payload)

    def _apply_multicam_sync_offsets(self, group: Any, payload: dict) -> None:
        """把确认过的同步偏移写回组 metadata 并刷新面板/状态栏。"""
        from src.features.edit.core.multicam import (
            group_with_sync_offsets,
            UpdateMulticamSyncCommand,
        )
        from src.features.edit.core.multicam_sync import filter_writable_offsets
        # F2.6：防错匹配门槛——error/rejection（低置信/反相/歧义/出界/
        # 漂移）的角度不写回，摘要里逐条给出原因。
        offsets, skipped = filter_writable_offsets(payload)
        if skipped:
            self.status_bar.showMessage(
                I18N.tr(
                    "status.multicam_sync_rejected",
                    "同步拒绝写回：{items}",
                ).format(items="；".join(skipped)),
                6000,
            )
        if not offsets:
            return  # 全部拒绝：保留原片（零写入）
        new_group = group_with_sync_offsets(group, offsets)
        if new_group != group:
            panel = getattr(self, "media_pool_panel", None)
            if panel is None:
                self.status_bar.showMessage("无法获取工程历史，已保留原同步设置。", 4000)
                return
            try:
                command = UpdateMulticamSyncCommand.create(self._current_edit_project, new_group)
                new_project = panel.get_command_stack().execute(command, self._current_edit_project)
            except ValueError as exc:
                self.status_bar.showMessage(str(exc), 4000)
                return
            self._current_edit_project = new_project
            self.edit_project_dirty = True
            panel.set_project(new_project)
            panel.project_changed.emit(new_project)
        self._refresh_multicam_panel()
        self._on_multicam_playhead_frame(
            int(getattr(self.edit_page, "_current_frame", 0) or 0)
            if self.edit_page else 0)

        # 摘要：每角度偏移 + 置信度 + 错误
        label_by_id = {
            a.asset_id: (a.label or a.asset_id[:8]) for a in new_group.angles
        }
        parts: list[str] = []
        errors = 0
        for aid, item in payload.items():
            name = label_by_id.get(aid, aid[:8])
            if item.get("error") or item.get("rejection"):
                errors += 1
                parts.append(f"{name}: {item.get('error') or item.get('rejection')}")
                continue
            off = int(item.get("offset_frames", 0))
            conf = float(item.get("confidence", 0.0))
            if off == 0:
                parts.append(f"{name}: 已对齐")
            else:
                sign = "+" if off > 0 else ""
                parts.append(
                    f"{name}: {sign}{off}f "
                    f"({item.get('lag_seconds', 0):+.2f}s, 置信{conf:.2f})")
        summary = I18N.tr(
            "status.multicam_sync_done", "波形同步完成") + " — " + "; ".join(parts)
        if errors:
            summary += f"（{errors} 个角度失败）"
        self.status_bar.showMessage(summary, 8000)
        logger.info("P1-01 multicam waveform sync done: %s", summary)

    def _on_multicam_sync_failed(self, message: str, request_id: str | None = None) -> None:
        """Clear only the failed current worker; late failures are harmless."""
        request = getattr(self, "_multicam_sync_request", None)
        if request_id is not None and (request is None or request[0] != request_id):
            return
        self._multicam_sync_request = None
        self._multicam_sync_group_asset_id = None
        self._multicam_sync_worker = None
        panel = getattr(self, "multicam_panel", None)
        if panel is not None:
            panel.set_sync_running(False)
            panel.store_sync_worker(None)
        if message == "cancelled":
            # F2.6：用户主动取消（requestInterruption）——非失败语气
            self.status_bar.showMessage(
                I18N.tr(
                    "status.multicam_sync_cancelled",
                    "已取消多机位同步（未做任何写入）",
                ),
                4000,
            )
            return
        self.status_bar.showMessage(
            I18N.tr(
                "status.multicam_sync_failed",
                f"波形同步失败：{message}",
            ),
            6000,
        )
        logger.warning("P1-01 multicam waveform sync failed: %s", message)

    def _on_create_nested_from_selection(self, clip_ids: list) -> None:
        """右键"从选择创建嵌套序列"。

        事务原子提交：选择片段移入新子序列（注册进 project.timelines 与
        序列缓存），父时间线原位替换为一个嵌套片段。循环嵌套在提交前阻断。
        与 comp_clip 创建同构——直接事务 + _on_edit_timeline_changed 统一发布
        （project / renderer / autosave / media pool 同步一致）。
        """
        import time as _time

        if not self._current_edit_project or not clip_ids:
            return
        timeline = getattr(self.edit_page, "_current_timeline", None) if self.edit_page else None
        if timeline is None:
            return

        from src.features.edit.core.nested_sequence import (
            build_nested_from_selection,
            would_create_cycle,
        )
        selected_clips = tuple(
            c for c in (timeline.get_clip_by_id(cid) for cid in clip_ids)
            if c is not None
        )
        if not selected_clips:
            return

        nested_child_by_asset_id = {
            a.asset_id: a.nested_timeline_id
            for a in self._current_edit_project.assets.values()
            if getattr(a, "is_nested_sequence", False) and a.nested_timeline_id
        }
        bad_tid = would_create_cycle(
            timeline, selected_clips,
            dict(self._current_timeline_sequences), nested_child_by_asset_id,
        )
        if bad_tid:
            QMessageBox.warning(
                self,
                I18N.tr("warning.nested_cycle_title", "Circular Nesting"),
                I18N.tr(
                    "warning.nested_cycle_body",
                    "The selection contains a nested sequence whose content "
                    "already includes this timeline. Nesting it here would "
                    "create a circular reference (sequence {tid}).",
                ).format(tid=bad_tid[:8]),
            )
            return

        try:
            txn = build_nested_from_selection(timeline, tuple(clip_ids))
        except ValueError as exc:
            self.status_bar.showMessage(str(exc), 4000)
            return

        from src.features.edit.core.project import TimelineSequenceRef
        child = txn.child_timeline
        fps_value = child.fps.to_float() if hasattr(child.fps, "to_float") else float(child.fps)
        child_ref = TimelineSequenceRef(
            timeline_id=child.timeline_id,
            name=child.name,
            fps=fps_value,
            width=child.width,
            height=child.height,
            sample_rate=child.sample_rate,
            format_settings=dict(child.format_settings),
            created_at=_time.time(),
        )
        new_project = (
            self._current_edit_project
            .with_media_asset_added(txn.nested_asset)
            .with_timeline_ref_added(child_ref, set_active=False)
        )
        from src.features.edit.core.timeline_project_transaction import TimelineProjectTransactionCommand
        command = TimelineProjectTransactionCommand.create(
            timeline, txn.new_timeline,
            before_project=self._current_edit_project, after_project=new_project,
            before_child_sequences={}, after_child_sequences={child.timeline_id: child},
            label="Create Nested Sequence")
        try:
            self._execute_timeline_edit_command(command, timeline)
        except ValueError as exc:
            self.status_bar.showMessage(str(exc), 4000)
            return

        self.status_bar.showMessage(
            I18N.tr(
                "status.nested_created",
                f"Nested sequence created: {child.name}",
            ),
            4000,
        )
        logger.info(
            "P1-02 nested sequence created: child=%s clips=%d parent_clip=%s",
            child.timeline_id, len(txn.removed_clip_ids), txn.nested_clip_id,
        )

    def _on_enter_nested_sequence(self, child_timeline_id: str, clip_id: str) -> None:
        """双击/右键"进入嵌套序列"——切换活动时间线到子序列。

        退出路径复用现有的时间线下拉/媒体池 Open Timeline（切换回父序列）。
        """
        _ = clip_id
        if not self._current_edit_project:
            return
        if child_timeline_id not in self._current_edit_project.timelines:
            logger.warning(
                "Enter nested sequence: timeline %s not in project", child_timeline_id
            )
            return
        self._on_timeline_switched(child_timeline_id)

    def _on_create_comp_clip_requested(self, clip_id: str, track_id: str) -> None:
        """Handle explicit Create Comp Clip from context menu.

        After creating (or reusing) the Comp Clip, switches to the Composite
        page so the user can immediately edit the graph — the creation is an
        explicit user intent to start editing, not just a silent project
        mutation (see docs/explicit_comp_clip_and_topos_color_unification_plan).
        """
        logger.info(f"[Phase2] Create Comp Clip requested: clip={clip_id}, track={track_id}")
        graph_id = self.create_comp_clip_for_timeline_clip(clip_id=clip_id, track_id=track_id)
        if graph_id:
            # B: creation is an explicit intent to edit — jump to Composite.
            self._switch_to_composite_page(graph_id)

    def _on_reset_composite_requested(self, clip_id: str, track_id: str) -> None:
        """Handle Reset Composite from context menu."""
        logger.info(f"[Phase2] Reset Composite requested: clip={clip_id}, track={track_id}")
        self.reset_composite_for_clip(clip_id=clip_id)

    def _on_remove_composite_binding_requested(self, clip_id: str, track_id: str) -> None:
        """Handle Remove Composite Binding from context menu."""
        logger.info(f"[Phase2] Remove Composite Binding requested: clip={clip_id}, track={track_id}")
        self.remove_composite_binding_for_clip(clip_id=clip_id)

    def _on_bypass_composite_requested(self, clip_id: str, track_id: str) -> None:
        """Handle Bypass Composite from context menu (R-1).

        Toggles the composite_bypassed state on the clip. When bypassed,
        Edit preview shows the original source + color instead of composite output.
        """
        logger.info(f"[R-1] Bypass Composite requested: clip={clip_id}, track={track_id}")

        if not self._current_edit_project:
            logger.warning("No project for bypass composite")
            return

        clip = self._find_timeline_clip(clip_id)
        if not clip:
            logger.warning(f"Clip not found: {clip_id}")
            return

        if not clip.composite_id:
            self.status_bar.showMessage("No composite to bypass", 2000)
            return

        # Toggle bypass state
        new_bypassed = not clip.composite_bypassed
        new_clip = clip.with_composite_bypassed(new_bypassed)

        # AUD-2700: 旧代码引用全库不存在的 ``self._current_edit_page`` /
        # ``EditPage.current_timeline()``/``load_timeline()``、
        # ``TimelineSequence.replace_clip``、``EditProject.with_timeline``，
        # 第一条语句即 AttributeError。改为既有不可变更新模式（与
        # ``remove_composite_binding_for_clip`` 同构）：在
        # ``edit_page._current_timeline`` 上以 track/clip 级 replace 重建，
        # 再同步页面时间线、序列缓存（保存取值源）、TimelineView，并经
        # ``_refresh_edit_preview_after_composite_change`` 刷新预览（bypass
        # 切换改变 Edit 预览的取材：composite 输出 ↔ 原始源 + 调色）。
        timeline = (
            getattr(self.edit_page, '_current_timeline', None)
            if hasattr(self, 'edit_page') and self.edit_page else None
        )
        if not timeline:
            logger.warning("Bypass Composite: no current timeline")
            return

        track = None
        for t in timeline.all_tracks:
            if t.get_clip_by_id(clip_id) is not None:
                track = t
                break
        if track is None:
            logger.warning(f"Bypass Composite: track not found for clip {clip_id}")
            return

        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)

        # Update state
        if hasattr(self, 'edit_page') and self.edit_page:
            self.edit_page._current_timeline = new_timeline
        if hasattr(self, '_current_timeline_sequences'):
            self._current_timeline_sequences[new_timeline.timeline_id] = new_timeline

        # Refresh UI + preview renderer
        self._refresh_edit_preview_after_composite_change()

        state_str = "bypassed" if new_bypassed else "enabled"
        self.status_bar.showMessage(f"Composite {state_str}", 2000)
        logger.info(f"[R-1] Composite {state_str} for clip {clip_id}")

    def _on_reveal_comp_clip_in_media_pool(self, clip_id: str, track_id: str) -> None:
        """Handle Reveal Comp Clip in Media Pool (R-3).

        Resolves the clip's composite_id to its comp_asset_id, then
        uses MediaPoolPanel.reveal_asset() to select and scroll to the
        asset in the media pool tree.

        Args:
            clip_id: Timeline clip ID that was right-clicked
            track_id: Track ID containing the clip
        """
        logger.info(f"[R-3] Reveal in Media Pool: clip={clip_id}")

        if not self._current_edit_project:
            logger.warning("No project for reveal in media pool")
            return

        # Resolve clip -> composite_id -> comp_asset_id
        comp_asset_id = None

        # Look up clip from the current timeline
        clip = self._find_timeline_clip(clip_id)
        if clip and getattr(clip, 'composite_id', None):
            comp_data = self._current_edit_project.get_composition_by_id(clip.composite_id)
            if comp_data:
                from src.features.edit import facade as edit_facade
                if isinstance(comp_data, dict):
                    graph = edit_facade.parse_composite_graph_data(comp_data)
                else:
                    graph = comp_data
                comp_asset_id = graph.comp_asset_id

        # Fallback: if clip's asset is already a comp_clip, use its asset_id
        if not comp_asset_id and clip:
            asset = self._current_edit_project.get_asset_by_id(clip.asset_id)
            if asset and getattr(asset, 'is_comp_clip', False):
                comp_asset_id = clip.asset_id

        if not comp_asset_id:
            self.status_bar.showMessage(
                I18N.tr("status.no_comp_clip_found", "No Comp Clip asset found for this clip"),
                3000,
            )
            return

        # Reveal in media pool
        found = self.media_pool_panel.reveal_asset(comp_asset_id)
        if found:
            # Switch to media pool focus
            self.media_pool_panel.setFocus()
            self.status_bar.showMessage(
                I18N.tr("status.revealed_comp_clip", f"Revealed Comp Clip: {comp_asset_id[:8]}"),
                3000,
            )
        else:
            self.status_bar.showMessage(
                I18N.tr("status.comp_clip_not_in_pool", "Comp Clip asset not found in media pool"),
                3000,
            )

    def _find_timeline_clip(self, clip_id: str):
        """Find a clip by ID from the current edit page timeline.

        Convenience wrapper that searches the timeline attached to
        edit_page (the one the user is currently viewing).

        Args:
            clip_id: Clip ID to find

        Returns:
            ClipInstance or None
        """
        if not hasattr(self, 'edit_page') or not self.edit_page:
            return None
        timeline = getattr(self.edit_page, '_current_timeline', None)
        if not timeline:
            return None
        for track in timeline.all_tracks:
            clip = track.get_clip_by_id(clip_id)
            if clip:
                return clip
        return None

    def _apply_clip_color_stack_change(
        self, track_id: str, clip_id: str, stack_id: str, *, shared: bool,
    ) -> bool:
        """Switch a clip to ``stack_id`` and propagate to renderer caches.

        AUD-2708: the switch previously applied the timeline command
        directly with no command-stack record, so the documented "切换本身
        可经时间线命令撤销" intent was never honoured.  It now commits
        through the TimelineView command stack (single undo entry; view /
        renderer / sequence cache sync via the command path), with a
        direct-apply fallback for hosts without a TimelineView.  The
        project's color stacks are republished to the preview renderer in
        both paths.
        """
        _cmds = get_command_classes()
        if _cmds is None:
            return False
        timeline = getattr(self.edit_page, "_current_timeline", None)
        if timeline is None:
            return False
        command = _cmds["UpdateClipColorStackCommand"].create(
            track_id=track_id,
            clip_id=clip_id,
            new_color_stack_id=stack_id,
            new_color_shared=shared,
        )
        self._execute_timeline_edit_command(command, timeline)
        if self.timeline_preview_renderer is not None:
            update_color_stacks = getattr(
                self.timeline_preview_renderer, "update_color_stacks", None,
            )
            if callable(update_color_stacks):
                update_color_stacks(self._current_edit_project)
        return True

    def _on_use_shared_grade_requested(self, clip_id: str, track_id: str) -> None:
        """P9.1: 右键「Use Shared Asset Grade」— 切到素材默认调色栈。

        素材默认栈不存在时按需创建（scope=asset_default），同素材的其他
        clip 之后可共享同一栈；切换本身可经时间线命令撤销。
        """
        logger.info(f"[Grade] Use shared grade requested: clip={clip_id}")
        project = self._current_edit_project
        timeline = getattr(self.edit_page, "_current_timeline", None)
        if project is None or timeline is None:
            logger.warning("[Grade] No project/timeline for shared grade request")
            return
        clip = self._find_timeline_clip(clip_id)
        if clip is None or not getattr(clip, "asset_id", ""):
            logger.warning(f"[Grade] Clip/asset not found: {clip_id}")
            return

        stack_id = next(
            (
                sid for sid, stack in project.color_stacks.items()
                if getattr(stack, "asset_id", "") == clip.asset_id
                and getattr(stack, "scope", "") == "asset_default"
            ),
            "",
        )
        if not stack_id:
            # AUD-2708: create + scope stamp as ONE project-level undo unit.
            # The old path executed ``CreateColorStackCommand`` on the media
            # pool stack and then stamped ``scope=asset_default`` with a bare
            # ``replace`` that no stack recorded — undo removed the stack but
            # left the stamp unrecoverable, and redo rebuilt an unstamped
            # stack under a different id.
            command = _CreateStampedColorStackCommand.create(
                asset_id=clip.asset_id,
                name=f"Asset_{clip.asset_id[:8]}",
                stamp_fields={
                    "scope": "asset_default",
                    "source_kind": "asset_default",
                },
                label=f"Create asset default color stack ({clip.asset_id[:8]})",
            )
            cmd_stack = self.media_pool_panel.get_command_stack()
            self._current_edit_project = cmd_stack.execute(
                command, self._current_edit_project)
            stack_id = command.created_stack_id or ""
            if not stack_id:
                logger.warning("[Grade] Failed to create asset default stack")
                return

        if self._apply_clip_color_stack_change(track_id, clip_id, stack_id, shared=True):
            logger.info(f"[Grade] Clip {clip_id} now shares asset stack {stack_id}")
            # M07：分享后自动锁——共享 look 默认防误改（此前
            # lock_shared_stacks 零生产调用）。逐栈经命令栈可撤销；
            # 自动锁失败不回滚分享本身（锁仍可手动补）。
            try:
                from src.features.edit.core.color_versions import (
                    SetStackLockCommand,
                    shared_stack_ids,
                    versions_state,
                )
                lock_stack = self.media_pool_panel.get_command_stack()
                for shared_sid in shared_stack_ids(self._current_edit_project):
                    if versions_state(self._current_edit_project,
                                      shared_sid)["locked"]:
                        continue
                    self._current_edit_project = lock_stack.execute(
                        SetStackLockCommand.create(
                            self._current_edit_project, shared_sid, True),
                        self._current_edit_project)
                    logger.info("[Grade] Auto-locked shared stack %s "
                                "(share default)", shared_sid)
            except Exception:  # noqa: BLE001 — 自动锁失败不阻断分享
                logger.warning("[Grade] share auto-lock failed",
                               exc_info=True)

    def _on_make_local_grade_copy_requested(self, clip_id: str, track_id: str) -> None:
        """P9.1: 右键「Make Local Grade Copy」— 复制当前调色为独立本地栈。"""
        logger.info(f"[Grade] Make local grade copy requested: clip={clip_id}")
        project = self._current_edit_project
        timeline = getattr(self.edit_page, "_current_timeline", None)
        if project is None or timeline is None:
            logger.warning("[Grade] No project/timeline for local grade copy")
            return
        clip = self._find_timeline_clip(clip_id)
        if clip is None:
            logger.warning(f"[Grade] Clip not found: {clip_id}")
            return

        source = (
            project.color_stacks.get(clip.color_stack_id)
            if getattr(clip, "color_stack_id", "") else None
        )
        # AUD-2708: 本地副本此前用 ``project.create_color_stack`` 直接建栈，
        # 完全不经命令栈（整个操作不可撤销），随后的快照复制也是裸
        # ``replace``。改为单个 ``_CreateStampedColorStackCommand`` 事务：
        # 建栈 + P1-COLOR-16 完整 v2 快照复制 + ownership 盖章一次提交，
        # undo/redo 单步一致且 stack_id 稳定。
        stamp_fields: dict[str, Any] = {
            "scope": "clip_local",
            "source_kind": "timeline_clip",
            "clip_id": clip_id,
            "timeline_id": timeline.timeline_id,
        }
        if source is not None:
            # P1-COLOR-16：本地副本必须携带完整 v2 快照（layers/
            # active_layer_index/trackings/raw_params 及平铺键），此前只
            # 复制平铺键，蒙版/限定器可视化/插件/跟踪/RAW 在本地化时
            # 静默丢失。审查 S1：清单抽到 V2_SNAPSHOT_CONTENT_FIELDS
            # 共享（补曲线侧栏 v3 键），防止两处再漂移。
            stamp_fields.update({
                field_name: getattr(source, field_name)
                for field_name in V2_SNAPSHOT_CONTENT_FIELDS
            })
        command = _CreateStampedColorStackCommand.create(
            asset_id=None,
            name=f"Local_{clip_id[:8]}",
            stamp_fields=stamp_fields,
            label=f"Make local grade copy ({clip_id[:8]})",
        )
        cmd_stack = self.media_pool_panel.get_command_stack()
        self._current_edit_project = cmd_stack.execute(
            command, self._current_edit_project)
        stack_id = command.created_stack_id or ""
        if not stack_id:
            logger.warning("[Grade] Failed to create local grade copy")
            return

        if self._apply_clip_color_stack_change(track_id, clip_id, stack_id, shared=False):
            logger.info(f"[Grade] Clip {clip_id} now uses local copy {stack_id}")

    # ------------------------------------------------------------------
    # P1-COLOR-16: 分类 Copy/Paste Grade（clip 级，单一 undo）
    # ------------------------------------------------------------------

    def _get_grade_clipboard(self):
        """分类调色剪贴板（惰性单例，内存态）。"""
        from src.features.color.core.grade_clipboard import GradeClipboard
        if getattr(self, "_grade_clipboard", None) is None:
            self._grade_clipboard = GradeClipboard()
        return self._grade_clipboard

    def _grade_clip_layers(self, stack) -> list:
        """把调色栈重建为 Layer 列表（v2 快照优先，v1 平铺键回退单层）。"""
        from src.features.color.adapters.color_stack_hydrator import (
            restore_inline_bitmaps,
        )
        from src.features.color.core.layer import Layer
        snapshots = [dict(d) for d in (getattr(stack, "layers", None) or ())
                     if isinstance(d, dict)]
        if snapshots:
            # AUD-24202：恢复 inline 位图载荷，clip 级 Copy Grade 的
            # "windows" 类别才能带上 BITMAP 蒙版数据
            layers = []
            for d in snapshots:
                layer = Layer.from_dict(d)
                restore_inline_bitmaps(layer, d)
                layers.append(layer)
            return layers
        layer = Layer("Layer 1")
        if getattr(stack, "grading_params", None):
            for k, v in stack.grading_params.items():
                layer.grading_params[k] = tuple(v) if isinstance(v, list) else v
        if getattr(stack, "mixer_params", None):
            layer.mixer_params = dict(stack.mixer_params)
        if getattr(stack, "soft_clip_params", None):
            layer.soft_clip_params.update(dict(stack.soft_clip_params))
        if getattr(stack, "lut_path", None):
            layer.active_lut_path = stack.lut_path
            layer.lut_intensity = getattr(stack, "lut_intensity", 1.0) or 1.0
        if getattr(stack, "curves_data", None):
            layer.curves_data = dict(stack.curves_data)
        if getattr(stack, "hsl_curve_data", None):
            layer.hsl_curve_data = dict(stack.hsl_curve_data)
        return [layer]

    def _on_grade_copy_requested(self, timeline_id: str, clip_id: str) -> None:
        """P1-COLOR-16：从 clip 的调色栈捕获分类快照（全类别）。"""
        from src.shared.i18n.i18n import I18N

        project = self._current_edit_project
        clip = self._find_timeline_clip(clip_id)
        if project is None or clip is None:
            logger.warning(f"[Grade] copy: no project/clip for {clip_id}")
            return
        stack = (
            project.color_stacks.get(clip.color_stack_id)
            if getattr(clip, "color_stack_id", "") else None
        )
        if stack is None:
            try:
                self.status_bar.showMessage(
                    I18N.tr("color.grade_copy_empty",
                            "该片段没有调色可复制"), 3000)
            except AttributeError:
                pass
            return

        clipboard = self._get_grade_clipboard()
        clipboard.copy_grade(
            layers=self._grade_clip_layers(stack),
            trackings=dict(getattr(stack, "trackings", None) or {}) or None,
            raw_params=dict(getattr(stack, "raw_params", None) or {}) or None,
        )
        try:
            self.status_bar.showMessage(
                I18N.tr(
                    "color.grade_copy_done",
                    "已复制调色：{count} 个类别（{items}）",
                ).format(
                    count=len(clipboard.copied_categories),
                    items="、".join(clipboard.describe()[:4]),
                ), 4000)
        except AttributeError:
            pass
        logger.info(
            f"[Grade] copied {len(clipboard.copied_categories)} categories "
            f"from clip {clip_id}"
        )

    def _on_grade_paste_requested(self, timeline_id: str, clip_id: str) -> None:
        """P1-COLOR-16：弹类别对话框并粘贴到 clip（单 undo 命令）。"""
        clipboard = self._get_grade_clipboard()
        if clipboard.is_empty():
            from src.shared.i18n.i18n import I18N
            try:
                self.status_bar.showMessage(
                    I18N.tr("color.grade_paste_empty",
                            "剪贴板没有调色，先在片段上右键 Copy Grade"), 3000)
            except AttributeError:
                pass
            return

        categories = None
        try:
            from src.features.color.ui.grade_paste_dialog import GradePasteDialog
            categories = GradePasteDialog.get_categories(
                None, set(clipboard.copied_categories))
        except Exception as e:  # 无头/测试环境退化为全类别直贴
            logger.debug(f"GradePasteDialog unavailable ({e}); pasting all")
            categories = set(clipboard.copied_categories)
        if not categories:
            return
        self._paste_grade_to_clip(clip_id, set(categories))

    def _on_grade_paste_all_requested(self, timeline_id: str) -> None:
        """P1-COLOR-16 复查：粘贴到当前时间线的全部视频镜头（批量单 undo）。

        类别选择与单目标粘贴共用对话框。逐 clip 构建
        ``UpdateColorStackCommand``（``_paste_grade_to_clip`` 收集模式），
        汇成 ``ProjectMacroCommand`` 单步提交——undo/redo 一次回退/重做
        全部镜头（同 ``_paste_assets`` 模式）。无调色栈的镜头先走既有
        建栈路径（建栈命令独立 undo；绝大多数批量粘贴场景所有镜头
        已有栈，整体保持单步）。
        """
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        from src.features.edit.core.commands import ProjectMacroCommand
        from src.shared.i18n.i18n import I18N

        clipboard = self._get_grade_clipboard()
        if clipboard.is_empty():
            try:
                self.status_bar.showMessage(
                    I18N.tr("color.grade_paste_empty",
                            "剪贴板没有调色，先在片段上右键 Copy Grade"), 3000)
            except AttributeError:
                pass
            return

        if not hasattr(self, 'edit_page') or not self.edit_page:
            return
        timeline = getattr(self.edit_page, '_current_timeline', None)
        if not timeline:
            return
        clip_ids = [
            clip.clip_id
            for track in timeline.all_tracks
            if getattr(track, 'track_type', 'video') == 'video'
            for clip in track.clips
        ]
        if not clip_ids:
            return

        categories = None
        try:
            from src.features.color.ui.grade_paste_dialog import GradePasteDialog
            categories = GradePasteDialog.get_categories(
                None, set(clipboard.copied_categories))
        except Exception as e:  # 无头/测试环境退化为全类别直贴
            logger.debug(f"GradePasteDialog unavailable ({e}); pasting all")
            categories = set(clipboard.copied_categories)
        if not categories:
            return

        sink: list = []
        applied = 0
        for clip_id in clip_ids:
            clip = self._find_timeline_clip(clip_id)
            if clip is None:
                continue
            if not (getattr(clip, "color_stack_id", "") or ""):
                if not self._ensure_clip_has_color_stack(
                        clip_id, clip.asset_id):
                    continue
            if self._paste_grade_to_clip(
                    clip_id, set(categories), command_sink=sink):
                applied += 1
        if not sink:
            return

        macro = ProjectMacroCommand.create(
            I18N.tr("color.grade_paste_all_macro",
                    "Paste grade to all clips"), sink)
        cmd_stack = self.media_pool_panel.get_command_stack()
        new_project = cmd_stack.execute(macro, self._current_edit_project)
        self._current_edit_project = new_project
        self.edit_project_dirty = True
        logger.info(
            f"[Grade] pasted {len(categories)} categories onto {applied}/"
            f"{len(clip_ids)} clips via single macro undo command")

        # 若 Color 页活动 clip 在本时间线中：统一同步引擎并重渲染一次
        strip = getattr(self, "color_timeline_clip_strip", None)
        active_clip_id = getattr(strip, "_active_clip_id", None)
        invalidate_previews = getattr(strip, "invalidate_graded_preview_stacks", None)
        if callable(invalidate_previews):
            changed_stack_ids = {
                clip.color_stack_id for clip_id in clip_ids
                if (clip := self._find_timeline_clip(clip_id)) is not None
                and clip.color_stack_id
            }
            invalidate_previews(changed_stack_ids)
        if active_clip_id and getattr(self, "engine", None):
            active_clip = self._find_timeline_clip(active_clip_id)
            active_stack_id = (
                getattr(active_clip, "color_stack_id", "") or ""
                if active_clip is not None else "")
            new_stack = (
                new_project.color_stacks.get(active_stack_id)
                if active_stack_id else None)
            if new_stack is not None:
                self._color_stack_hydration_in_progress = True
                try:
                    hydrator = ColorStackHydrator()
                    hydrator.apply_stack_to_engine(new_stack, self.engine)
                    collect_panels = getattr(self, "_collect_color_panels", None)
                    panels = collect_panels() if callable(collect_panels) else {
                        "grading_panel": getattr(self, "grading_panel", None),
                    }
                    hydrator.apply_stack_to_panels(new_stack, panels)
                    self._pending_grading_color_stack = None
                finally:
                    self._color_stack_hydration_in_progress = False
                self.engine.render_pipeline(render_reason='grading')

                self._schedule_color_timeline_clip_preview_sync(active_clip_id)

        self._schedule_idle_color_timeline_clip_strip_refresh()
        try:
            self.status_bar.showMessage(
                I18N.tr("color.grade_paste_all_done",
                        "已粘贴调色到 {count}/{total} 个镜头",
                        count=applied, total=len(clip_ids)), 4000)
        except AttributeError:
            pass

    def _paste_grade_to_clip(
        self, clip_id: str, categories: set, clipboard=None,
        command_sink: list | None = None) -> bool:
        """把剪贴板类别应用到目标 clip 的调色栈（单一 undo 命令）。

        走 ``UpdateColorStackCommand`` 的 v2 完整通道（layers 快照 +
        trackings + raw_params），一次粘贴一个 undo 单元。若目标是
        Color 页当前活动 clip，同步刷新引擎并重渲染。
        ``clipboard`` 允许调用方传临时实例（Auto Color / Gallery 分类
        应用），避免写脏用户的全局 Grade 剪贴板。
        ``command_sink`` 非 None 时进入收集模式：只构建命令并 append
        到 sink（不 execute、无引擎/刷新副作用），由批量调用方汇成
        ``ProjectMacroCommand`` 单步提交；此模式下要求 clip 已有调色栈。
        """
        import copy as _copy

        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        from src.features.edit.core.commands import UpdateColorStackCommand
        from src.shared.i18n.i18n import I18N

        if clipboard is None:
            clipboard = self._get_grade_clipboard()
        project = self._current_edit_project
        clip = self._find_timeline_clip(clip_id)
        if project is None or clip is None:
            logger.warning(f"[Grade] paste: no project/clip for {clip_id}")
            return False
        stack_id = getattr(clip, "color_stack_id", "") or ""
        if not stack_id:
            if command_sink is not None:
                # 收集模式由调用方负责建栈；未建栈的镜头直接跳过
                return False
            # 目标 clip 尚无调色栈：先确保存在（复用既有创建路径）
            ensured = self._ensure_clip_has_color_stack(clip_id, clip.asset_id)
            if not ensured:
                return False
            project = self._current_edit_project
            clip = self._find_timeline_clip(clip_id)
            stack_id = getattr(clip, "color_stack_id", "") or ensured
        stack = project.color_stacks.get(stack_id)
        if stack is None:
            logger.warning(f"[Grade] paste: stack {stack_id} not found")
            return False

        # 1. 目标栈 → Layer 列表，应用类别补丁
        layers = self._grade_clip_layers(stack)
        applied = clipboard.paste_onto_layers(layers, categories=categories)

        # 2. 栈级类别（tracking / raw）
        trackings = dict(getattr(stack, "trackings", None) or {})
        if clipboard.trackings and "tracking" in categories:
            trackings = _copy.deepcopy(clipboard.trackings)
        raw_params = dict(getattr(stack, "raw_params", None) or {})
        if clipboard.raw_params and "raw" in categories:
            raw_params = _copy.deepcopy(clipboard.raw_params)

        # 3. 从活动层派生平铺键 + 完整快照，单命令提交
        active_index = getattr(stack, "active_layer_index", 0) or 0
        if not (0 <= active_index < len(layers)):
            active_index = 0
        head = layers[active_index]
        # AUD-24202：粘贴落库的快照同样补 inline 位图通道
        from src.features.color.adapters.color_stack_hydrator import (
            attach_inline_bitmaps,
        )
        layers_snapshot = []
        for l in layers:
            d = l.to_dict(None)
            attach_inline_bitmaps(l, d)
            layers_snapshot.append(d)

        command = UpdateColorStackCommand.create(
            stack_id=stack_id,
            grading_params=dict(getattr(head, "grading_params", {}) or {}),
            curves_data=dict(getattr(head, "curves_data", {}) or {}),
            hsl_curve_data=dict(getattr(head, "hsl_curve_data", {}) or {}),
            mixer_params=dict(getattr(head, "mixer_params", {}) or {}),
            soft_clip_params=dict(getattr(head, "soft_clip_params", {}) or {}),
            lut_path=getattr(head, "active_lut_path", None),
            lut_intensity=getattr(head, "lut_intensity", None),
            layers=layers_snapshot,
            active_layer_index=active_index,
            raw_params=dict(raw_params) or None,
            trackings=dict(trackings) or None,
        )
        if command_sink is not None:
            # 收集模式：命令交给批量调用方汇成 macro，副作用（execute/
            # 引擎刷新/状态栏）由其在收尾统一执行一次
            command_sink.append(command)
            return True
        cmd_stack = self.media_pool_panel.get_command_stack()
        new_project = cmd_stack.execute(command, project)
        self._current_edit_project = new_project
        self.edit_project_dirty = True
        logger.info(
            f"[Grade] pasted {len(categories)} categories onto clip "
            f"{clip_id} ({applied} layers) via single undo command"
        )

        # 4. 若目标是 Color 页当前活动 clip：同步引擎并重渲染
        strip = getattr(self, "color_timeline_clip_strip", None)
        active_clip_id = getattr(strip, "_active_clip_id", None)
        invalidate_previews = getattr(strip, "invalidate_graded_preview_stacks", None)
        if callable(invalidate_previews):
            invalidate_previews({stack_id})
        active_clip = self._find_timeline_clip(active_clip_id) if active_clip_id else None
        active_uses_stack = (
            active_clip_id == clip_id
            or (active_clip is not None and active_clip.color_stack_id == stack_id)
        )
        if active_uses_stack and getattr(self, "engine", None):
            new_stack = new_project.color_stacks.get(stack_id)
            if new_stack is not None:
                self._color_stack_hydration_in_progress = True
                try:
                    hydrator = ColorStackHydrator()
                    hydrator.apply_stack_to_engine(new_stack, self.engine)
                    collect_panels = getattr(self, "_collect_color_panels", None)
                    panels = collect_panels() if callable(collect_panels) else {
                        "grading_panel": getattr(self, "grading_panel", None),
                    }
                    hydrator.apply_stack_to_panels(new_stack, panels)
                    self._pending_grading_color_stack = None
                finally:
                    self._color_stack_hydration_in_progress = False
                self.engine.render_pipeline(render_reason='grading')

                self._schedule_color_timeline_clip_preview_sync(active_clip_id)

        self._schedule_idle_color_timeline_clip_strip_refresh()
        try:
            self.status_bar.showMessage(
                I18N.tr("color.grade_paste_done",
                        "已粘贴调色（{count} 个类别）").format(
                    count=len(categories)), 3000)
        except AttributeError:
            pass
        return True

    def request_render_cache_for_clip(self, clip_id: str, track_id: str) -> bool:
        """Public host bridge used by Motion Graphic editor cache actions."""
        return self._on_render_cache_clip_requested(clip_id, track_id)

    def request_rerender_cache_for_clip(self, clip_id: str, track_id: str) -> bool:
        """Force a fresh cache for an existing Motion Graphic clip."""
        return self._on_render_cache_clip_requested(clip_id, track_id, force=True)

    def _on_rerender_cache_clip_requested(self, clip_id: str, track_id: str) -> bool:
        """Handle the explicit right-click “重新渲染缓存” command."""
        return self._on_render_cache_clip_requested(clip_id, track_id, force=True)

    def _on_render_cache_clip_requested(
        self, clip_id: str, track_id: str, *, force: bool = False,
    ) -> bool:
        """Handle Render Cache Clip from context menu."""
        logger.info(
            "[RenderCache] %s cache requested: clip=%s",
            "Force re-render" if force else "Render",
            clip_id,
        )

        if not self._current_edit_project:
            logger.warning("[RenderCache] No project for render cache request")
            return False

        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("[RenderCache] No timeline for render cache request")
            return False

        timeline = self.edit_page._current_timeline

        # A cache request can arrive from more than one UI path at once (for
        # example the Space auto-cache guard and the Motion Graphic editor's
        # explicit render command).  Guard by timeline/clip, not only by the
        # manifest id: during recovery the old manifest may still be visible
        # to a re-entrant signal while the replacement worker is starting.
        inflight_key = (str(getattr(timeline, "timeline_id", "")), str(clip_id))
        inflight_clips = getattr(self, "_render_cache_inflight_clips", None)
        if inflight_clips is None:
            inflight_clips = set()
            self._render_cache_inflight_clips = inflight_clips
        if inflight_key in inflight_clips:
            self.status_bar.showMessage("Render cache is already rendering", 2000)
            logger.info("[RenderCache] Clip %s already has an in-flight request", clip_id)
            return True

        # Find the clip
        clip = None
        for track in timeline.all_tracks:
            c = track.get_clip_by_id(clip_id)
            if c:
                clip = c
                break

        if clip is None:
            logger.warning(f"[RenderCache] Clip not found: {clip_id}")
            return False

        # Composite clips and Motion Graphic clips share the same disk-cache
        # pipeline.  Motion Graphic scenes use their stable scene reference
        # instead of a composite graph id.  A generator drop publishes the
        # immutable asset snapshot just after the timeline command signal;
        # accept the panel/view snapshot when it is the one that actually
        # contains this clip's asset so Space does not race that signal order.
        asset = self._current_edit_project.assets.get(clip.asset_id)
        if asset is None:
            for candidate in (
                getattr(getattr(self, "timeline_panel", None), "_project", None),
                getattr(
                    getattr(getattr(self, "timeline_panel", None), "timeline_view", None),
                    "_project",
                    None,
                ),
            ):
                candidate_asset = (
                    getattr(candidate, "assets", {}).get(clip.asset_id)
                    if candidate is not None
                    else None
                )
                if candidate_asset is not None:
                    self._current_edit_project = candidate
                    asset = candidate_asset
                    break
        motion_scene_id = ""
        if asset is not None and getattr(asset, "media_type", "") == "motion_graphic":
            try:
                from src.features.teaching_animation.host_adapters import motion_graphic_scene_id
                motion_scene_id = motion_graphic_scene_id(clip.asset_id) or str(
                    (getattr(asset, "metadata", {}) or {}).get("scene_id", "")
                )
            except ImportError:
                motion_scene_id = str((getattr(asset, "metadata", {}) or {}).get("scene_id", ""))
        is_motion_graphic = bool(motion_scene_id)
        if not clip.composite_id and not is_motion_graphic:
            self.status_bar.showMessage("No composite to cache - create a Comp Clip first", 3000)
            logger.info(f"[RenderCache] Clip {clip_id} has no composite_id")
            return False

        from src.features.edit import facade as edit_facade
        RenderCacheStatus = edit_facade.get_render_cache_status()
        from src.shared.media.motion_graphic_delivery import (
            MOTION_GRAPHIC_CACHE_CODEC,
            MOTION_GRAPHIC_CACHE_CONTAINER,
            MOTION_GRAPHIC_CACHE_PATH_TAG,
            motion_graphic_preview_size,
        )
        from src.features.edit.core.render_cache import build_render_cache_fingerprint

        # Resolve the delivery contract before looking at an existing
        # manifest.  Otherwise a previously valid H.264 file would be
        # accepted forever even after the Motion Graphic cache contract was
        # changed to high-quality VP8/WebM.
        config = getattr(self.config_manager, "config", {})
        if is_motion_graphic:
            cache_codec = MOTION_GRAPHIC_CACHE_CODEC
            cache_container = MOTION_GRAPHIC_CACHE_CONTAINER
        else:
            cache_codec = str(config.get("render_cache_codec", "topos_proxy") or "topos_proxy")
            # Container is intentionally derived from the codec.  The cache
            # UI no longer exposes a second format selector because Topos and
            # DNxHR are MOV-only while H.264 is MP4-backed.
            cache_container = (
                "mov"
                if cache_codec.startswith(("dnxhr", "topos"))
                else "mp4"
            )

        cache_root = str(
            config.get("render_cache_directory", "")
            or config.get("cache_directory", "")
            or "Cache"
        )
        if not os.path.isabs(cache_root):
            project_path = getattr(self, "_current_edit_project_filepath", None)
            base_dir = os.path.dirname(project_path) if project_path else os.getcwd()
            cache_root = os.path.join(base_dir, cache_root)
        cache_width, cache_height = int(timeline.width), int(timeline.height)
        if is_motion_graphic:
            cache_width, cache_height = motion_graphic_preview_size(cache_width, cache_height)
        render_fingerprint = build_render_cache_fingerprint(
            self._current_edit_project, timeline.timeline_id, clip
        )

        # The first cache keeps the historical short filename for project
        # compatibility.  Once a semantic variant already exists, append a
        # fingerprint token so two undo/redo states never write the same file.
        history_manifests = [
            _render_cache_manifest_from_value(raw)
            for raw in (
                getattr(self._current_edit_project, "render_cache_manifests", {}) or {}
            ).values()
        ]
        has_fingerprinted_history = any(
            bool(getattr(history_manifest, "render_fingerprint", ""))
            and str(getattr(history_manifest, "timeline_id", ""))
            == str(timeline.timeline_id)
            and str(getattr(history_manifest, "clip_id", "")) == str(clip_id)
            for history_manifest in history_manifests
            if history_manifest is not None
        )
        variant_token = f".{render_fingerprint[:12]}" if has_fingerprinted_history else ""
        cache_filename = (
            f"clip_{clip_id[:8]}{variant_token}.{MOTION_GRAPHIC_CACHE_PATH_TAG}.{cache_container}"
            if is_motion_graphic
            else f"clip_{clip_id[:8]}{variant_token}.{cache_container}"
        )
        desired_cache_path = os.path.join(cache_root, cache_filename)
        legacy_cache_filename = (
            f"clip_{clip_id[:8]}.{MOTION_GRAPHIC_CACHE_PATH_TAG}.{cache_container}"
            if is_motion_graphic
            else f"clip_{clip_id[:8]}.{cache_container}"
        )
        legacy_desired_cache_path = os.path.join(cache_root, legacy_cache_filename)

        # Check the semantic variant first.  If an earlier edit is still
        # rendering, inspect the latest unqualified entry as well so the
        # replacement job can cancel that obsolete worker instead of running
        # two encoders for one clip.
        existing = self._current_edit_project.get_render_cache_for_clip(
            timeline.timeline_id,
            clip_id,
            render_fingerprint=render_fingerprint,
        )
        latest_existing = self._current_edit_project.get_render_cache_for_clip(
            timeline.timeline_id, clip_id,
        )
        if (
            latest_existing is not None
            and latest_existing.status == RenderCacheStatus.RENDERING
            and (
                existing is None
                or getattr(existing, "cache_id", "")
                != getattr(latest_existing, "cache_id", "")
            )
        ):
            active_workers = getattr(self, "_render_cache_workers", {}) or {}
            old_worker = active_workers.pop(
                getattr(latest_existing, "cache_id", ""), None,
            )
            if old_worker is not None:
                try:
                    old_worker.cancel()
                except Exception:
                    logger.debug("Could not cancel superseded cache worker", exc_info=True)
            inflight_cache_keys = getattr(self, "_render_cache_inflight_cache_keys", None)
            if inflight_cache_keys is not None:
                inflight_cache_keys.pop(getattr(latest_existing, "cache_id", ""), None)
            inflight_clips = getattr(self, "_render_cache_inflight_clips", None)
            if inflight_clips is not None:
                inflight_clips.discard((timeline.timeline_id, str(clip_id)))
            self._cleanup_obsolete_render_cache_files(
                self._current_edit_project,
                clip_id=str(clip_id),
                include_rendering=True,
                preserve_valid_fingerprinted=True,
            )
            existing = None

        # A second click while the first job is still running must not replace
        # its manifest and launch a competing encoder.  Keeping the original
        # RENDERING manifest also lets the timeline indicator continue to
        # report the correct in-progress clip until the worker emits finished.
        # After an app crash/restart, however, a persisted RENDERING manifest
        # can outlive its QThread.  Treat that orphan as stale instead of
        # making Space wait forever for a worker that no longer exists.
        if existing and existing.status == RenderCacheStatus.RENDERING:
            active_workers = getattr(self, "_render_cache_workers", {}) or {}
            active_worker = active_workers.get(getattr(existing, "cache_id", ""))
            worker_is_active = active_worker is not None
            if active_worker is not None:
                # ``QThread.isRunning()`` can briefly be false between
                # ``start()`` and the thread entering its event loop.  Treat
                # that object as active unless Qt explicitly reports it has
                # finished; otherwise a second request launches a competing
                # FFmpeg process against the same cache path.
                is_finished = getattr(active_worker, "isFinished", None)
                try:
                    worker_is_active = not bool(is_finished()) if callable(is_finished) else True
                except RuntimeError:
                    # A deleted/finished Qt thread must not block a new cache.
                    worker_is_active = False
            if worker_is_active:
                self.status_bar.showMessage("Render cache is already rendering", 2000)
                logger.info(f"[RenderCache] Clip {clip_id} is already rendering")
                return True

            if active_worker is not None:
                active_workers.pop(getattr(existing, "cache_id", ""), None)
            logger.warning(
                "[RenderCache] Recovering orphaned RENDERING manifest: clip=%s cache=%s",
                clip_id, str(getattr(existing, "cache_id", ""))[:8],
            )
            self._cleanup_obsolete_render_cache_files(
                self._current_edit_project,
                clip_id=str(clip_id),
                include_rendering=True,
                preserve_valid_fingerprinted=True,
            )

        if existing and existing.status == RenderCacheStatus.VALID and not force:
            # A project can be moved or its cache directory cleaned outside
            # the app.  Treat a missing file as stale instead of claiming a
            # green cache that playback cannot actually open.
            existing_path = str(getattr(existing, "path", "") or "")
            # P2-17 避免误命中：composite 与 MG 走同一输出契约校验。
            # 此前 composite 分支恒 True——时间线分辨率/帧率、缓存编码
            # 设置变化后旧文件仍被当有效缓存复用。旧契约文件（如遗留
            # ProRes）会被判过期并触发一次重渲染，符合保守语义。
            format_matches = (
                str(getattr(existing, "codec", "")).lower() == str(cache_codec).lower()
                and str(getattr(existing, "container", "")).lower() == str(cache_container).lower()
                and os.path.abspath(existing_path) in {
                    os.path.abspath(desired_cache_path),
                    os.path.abspath(legacy_desired_cache_path),
                }
                and int(getattr(existing, "width", 0) or 0) == cache_width
                and int(getattr(existing, "height", 0) or 0) == cache_height
                and _fps_token(getattr(existing, "timeline_fps", ""))
                == _fps_token(getattr(timeline, "fps", ""))
                and (
                    not str(getattr(existing, "render_fingerprint", "") or "")
                    or str(existing.render_fingerprint) == render_fingerprint
                )
            )
            if format_matches and (not existing_path or os.path.exists(existing_path)):
                self.status_bar.showMessage("Render cache is already valid", 2000)
                logger.info(f"[RenderCache] Clip {clip_id} already has valid cache")
                return True
            logger.info(
                "[RenderCache] Existing cache format is obsolete; re-rendering %s",
                clip_id,
            )

        # STALE/FAILED manifests point at outputs that will never be reused.
        # Remove them before allocating a new UUID/path so repeated edits do
        # not accumulate orphaned video files.
        if latest_existing is not None:
            self._cleanup_obsolete_render_cache_files(
                self._current_edit_project,
                clip_id=str(clip_id),
                include_rendering=False,
                preserve_valid_fingerprinted=True,
            )

        # Create new manifest
        fps = timeline.fps
        fps_str = f"{fps.num}/{fps.den}" if hasattr(fps, 'num') else "24000/1001"
        manifest = edit_facade.create_render_cache_manifest_new(
            timeline_id=timeline.timeline_id,
            clip_id=clip_id,
            composite_id=clip.composite_id or f"teaching_scene:{motion_scene_id}",
            start_frame=clip.timeline_start,
            duration_frames=clip.timeline_duration,
            width=cache_width,
            height=cache_height,
            fps=fps_str,
            render_kind="motion_graphic" if is_motion_graphic else "composite",
            scene_id=motion_scene_id,
        )

        # Keep the resolved path in the manifest so the background worker and
        # a later project reload use exactly the same location.  The fixed
        # Motion Graphic suffix also invalidates old 20 Mbps cache files.
        cache_path = desired_cache_path
        manifest = replace(
            manifest,
            codec=cache_codec,
            container=cache_container,
            path=cache_path,
            render_fingerprint=render_fingerprint,
        )

        # Mark as RENDERING
        rendering_manifest = edit_facade.create_render_cache_manifest(
            cache_id=manifest.cache_id,
            timeline_id=manifest.timeline_id,
            clip_id=manifest.clip_id,
            composite_id=manifest.composite_id,
            color_stack_revision=0,
            composite_graph_revision=0,
            source_asset_revision="",
            render_fingerprint=manifest.render_fingerprint,
            timeline_fps=manifest.timeline_fps,
            start_frame=manifest.start_frame,
            duration_frames=manifest.duration_frames,
            path=manifest.path,
            codec=manifest.codec,
            container=manifest.container,
            render_kind=manifest.render_kind,
            scene_id=manifest.scene_id,
            width=manifest.width,
            height=manifest.height,
            status=RenderCacheStatus.RENDERING,
        )

        # Set the clip-level guard before starting QThread.  This closes the
        # small re-entrant window between manifest persistence and worker
        # registration, and is cleared only by the terminal callbacks below.
        inflight_clips.add(inflight_key)
        inflight_cache_keys = getattr(self, "_render_cache_inflight_cache_keys", None)
        if inflight_cache_keys is None:
            inflight_cache_keys = {}
            self._render_cache_inflight_cache_keys = inflight_cache_keys
        inflight_cache_keys[rendering_manifest.cache_id] = inflight_key

        # Store in project
        self._current_edit_project = self._current_edit_project.set_render_cache_manifest(
            rendering_manifest
        )
        prune_variants = getattr(
            self,
            "_prune_render_cache_variants",
            EditControllerMixin._prune_render_cache_variants,
        )
        self._current_edit_project = prune_variants(
            self._current_edit_project,
            timeline_id=timeline.timeline_id,
            clip_id=str(clip_id),
        )
        self.timeline_panel.set_project(self._current_edit_project)

        logger.info(
            f"[RenderCache] Starting render cache for clip {clip_id}: "
            f"cache_id={rendering_manifest.cache_id[:8]}, "
            f"frames={rendering_manifest.duration_frames}"
        )

        # Start background render worker
        if not self._start_render_cache_worker(rendering_manifest):
            inflight_clips.discard(inflight_key)
            inflight_cache_keys.pop(rendering_manifest.cache_id, None)
            self._on_render_cache_failed(
                rendering_manifest.cache_id,
                "Render-cache worker is unavailable",
            )
            return False
        return True

    def _start_render_cache_worker(self, manifest: Any) -> bool:
        """Start a RenderCacheWorker for the given manifest."""
        from src.features.edit import facade as edit_facade

        # Build timeline_sequences dict for the renderer
        timeline = self.edit_page._current_timeline
        timeline_sequences = {timeline.timeline_id: timeline} if timeline else {}

        worker = edit_facade.create_render_cache_worker(
            manifest=manifest,
            edit_project=self._current_edit_project,
            timeline_sequences=timeline_sequences,
        )
        if worker is None:
            logger.warning("[RenderCache] Cannot create worker; module unavailable")
            return False
        worker.progress_changed.connect(self._on_render_cache_progress)
        worker.finished_ok.connect(self._on_render_cache_finished)
        worker.failed.connect(self._on_render_cache_failed)
        # AUD-2706: the worker is created without a Qt parent, so the dict
        # entry below is its only hard reference.  ``finished_ok``/``failed``
        # are emitted from inside ``run()`` and delivered queued to the main
        # thread — the receiving slot can run before the QThread has fully
        # exited, and dropping the last reference there used to destroy a
        # still-running QThread ("QThread: Destroyed while thread is still
        # running" → process abort).  ``finished → deleteLater`` is the
        # canonical self-owning QThread pattern (already used for
        # MulticamSyncWorker): the connection keeps the wrapper alive past
        # the dict removal and defers C++ destruction to the main-thread
        # event loop once the thread has really finished.
        worker.finished.connect(worker.deleteLater)
        if isinstance(self, QObject):
            # QObject hosts (MainWindow) additionally get the dict entry
            # swept on the real ``finished`` signal (queued to the main
            # thread), so a not-yet-exited worker is never left registered.
            worker.finished.connect(
                self._on_render_cache_worker_thread_finished)

        # Track active workers (for cancellation on project close)
        if not hasattr(self, '_render_cache_workers'):
            self._render_cache_workers = {}
        self._render_cache_workers[manifest.cache_id] = worker

        worker.start()
        return True

    def _on_render_cache_worker_thread_finished(self) -> None:
        """AUD-2706: remove the registry entry of the thread that finished.

        Connected to ``QThread.finished`` (only emitted once the thread has
        really exited), so sweeping here can never pull the reference out
        from under a running thread.  ``sender()`` identifies the worker.
        """
        workers = getattr(self, "_render_cache_workers", None)
        if not isinstance(workers, dict) or not workers:
            return
        sender_getter = getattr(self, "sender", None)
        worker = sender_getter() if callable(sender_getter) else None
        if worker is None:
            return
        for cache_id, registered in list(workers.items()):
            if registered is worker:
                del workers[cache_id]
                break

    def _release_render_cache_worker_slot(self, cache_id: str) -> None:
        """AUD-2706: drop a worker's registry entry only once it finished.

        ``_on_render_cache_finished``/``_on_render_cache_failed`` run queued
        and may execute while the QThread is still unwinding; keep the entry
        in that case (the ``finished``-driven sweep above removes it later).
        The ``finished → deleteLater`` connection keeps the wrapper alive
        either way, so a running thread is never destroyed by this call.
        """
        workers = getattr(self, "_render_cache_workers", None)
        if not isinstance(workers, dict) or cache_id not in workers:
            return
        worker = workers[cache_id]
        is_finished = getattr(worker, "isFinished", None)
        try:
            finished = bool(is_finished()) if callable(is_finished) else True
        except RuntimeError:
            # Wrapper's C++ object is already gone — nothing left to protect.
            finished = True
        if finished:
            del workers[cache_id]

    def _on_render_cache_progress(self, cache_id: str, done: int, total: int) -> None:
        """Handle render cache progress update."""
        pct = int(done / total * 100) if total > 0 else 0
        self.status_bar.showMessage(f"Rendering cache: {pct}% ({done}/{total})", 2000)

    def _on_render_cache_finished(self, cache_id: str, final_path: str) -> None:
        """Handle render cache completion."""
        from src.features.edit import facade as edit_facade
        RenderCacheStatus = edit_facade.get_render_cache_status()

        inflight_cache_keys = getattr(self, "_render_cache_inflight_cache_keys", None)
        inflight_key = (
            inflight_cache_keys.pop(cache_id, None)
            if inflight_cache_keys is not None else None
        )
        inflight_clips = getattr(self, "_render_cache_inflight_clips", None)
        if inflight_key is not None and inflight_clips is not None:
            inflight_clips.discard(inflight_key)

        if not self._current_edit_project:
            return

        # Update manifest status to VALID
        existing = self._current_edit_project.render_cache_manifests.get(cache_id)
        if existing:
            updated = edit_facade.create_render_cache_manifest(
                cache_id=existing['cache_id'],
                timeline_id=existing['timeline_id'],
                clip_id=existing['clip_id'],
                composite_id=existing['composite_id'],
                color_stack_revision=existing.get('color_stack_revision', 0),
                composite_graph_revision=existing.get('composite_graph_revision', 0),
                source_asset_revision=existing.get('source_asset_revision', ''),
                render_fingerprint=existing.get('render_fingerprint', ''),
                timeline_fps=existing['timeline_fps'],
                start_frame=existing['start_frame'],
                duration_frames=existing['duration_frames'],
                path=final_path,
                codec=existing['codec'],
                container=existing.get('container', 'mov'),
                render_kind=existing.get('render_kind', 'composite'),
                scene_id=existing.get('scene_id', ''),
                width=existing['width'],
                height=existing['height'],
                status=RenderCacheStatus.VALID,
            )
            self._current_edit_project = self._current_edit_project.set_render_cache_manifest(updated)
            prune_variants = getattr(
                self,
                "_prune_render_cache_variants",
                EditControllerMixin._prune_render_cache_variants,
            )
            self._current_edit_project = prune_variants(
                self._current_edit_project,
                timeline_id=str(existing.get("timeline_id", "")),
                clip_id=str(existing.get("clip_id", "")),
            )
            # TimelinePanel keeps its own project snapshot for cache-status
            # and transport decisions, while TimelinePreviewRenderer owns a
            # separate immutable snapshot for frame decoding.  Publishing the
            # manifest only to the panel leaves the renderer on the old
            # RENDERING entry; after the cache finishes, a single
            # Motion Graphic Clip then has no readable source until another
            # timeline edit happens to refresh the renderer.  Keep both
            # snapshots in lock-step before auto-play resumes.
            preview_renderer = getattr(self, "timeline_preview_renderer", None)
            set_preview_project = getattr(preview_renderer, "set_project", None)
            if callable(set_preview_project):
                set_preview_project(self._current_edit_project)
            # Do this after the renderer update: ``TimelinePanel.set_project``
            # polls a pending Space request synchronously and may start
            # playback immediately when the new manifest is VALID.
            self.timeline_panel.set_project(self._current_edit_project)

        # Clean up worker reference (AUD-2706: only once the thread really
        # exited; otherwise the finished-signal sweep removes it later).
        self._release_render_cache_worker_slot(cache_id)
        getattr(self, "_mg_idle_cache_ids", set()).discard(cache_id)

        self.status_bar.showMessage(f"Render cache complete: {final_path}", 5000)
        logger.info("[RenderCache] Cache completed: %s → %s", cache_id[:8], final_path)

    def _on_render_cache_failed(self, cache_id: str, error: str) -> None:
        """Handle render cache failure."""
        from src.features.edit import facade as edit_facade
        RenderCacheStatus = edit_facade.get_render_cache_status()

        inflight_cache_keys = getattr(self, "_render_cache_inflight_cache_keys", None)
        inflight_key = (
            inflight_cache_keys.pop(cache_id, None)
            if inflight_cache_keys is not None else None
        )
        inflight_clips = getattr(self, "_render_cache_inflight_clips", None)
        if inflight_key is not None and inflight_clips is not None:
            inflight_clips.discard(inflight_key)

        if not self._current_edit_project:
            return

        # Update manifest status to FAILED
        existing = self._current_edit_project.render_cache_manifests.get(cache_id)
        if existing:
            updated = edit_facade.create_render_cache_manifest(
                cache_id=existing['cache_id'],
                timeline_id=existing['timeline_id'],
                clip_id=existing['clip_id'],
                composite_id=existing['composite_id'],
                color_stack_revision=existing.get('color_stack_revision', 0),
                composite_graph_revision=existing.get('composite_graph_revision', 0),
                source_asset_revision=existing.get('source_asset_revision', ''),
                render_fingerprint=existing.get('render_fingerprint', ''),
                timeline_fps=existing['timeline_fps'],
                start_frame=existing['start_frame'],
                duration_frames=existing['duration_frames'],
                path=existing['path'],
                codec=existing['codec'],
                container=existing.get('container', 'mov'),
                render_kind=existing.get('render_kind', 'composite'),
                scene_id=existing.get('scene_id', ''),
                width=existing['width'],
                height=existing['height'],
                status=RenderCacheStatus.FAILED,
            )
            self._current_edit_project = self._current_edit_project.set_render_cache_manifest(updated)
            prune_variants = getattr(
                self,
                "_prune_render_cache_variants",
                EditControllerMixin._prune_render_cache_variants,
            )
            self._current_edit_project = prune_variants(
                self._current_edit_project,
                timeline_id=str(existing.get("timeline_id", "")),
                clip_id=str(existing.get("clip_id", "")),
            )
            # Keep the renderer snapshot aligned on failure as well.  It may
            # fall back to the deterministic scene renderer, and must not
            # retain a stale RENDERING manifest that suppresses that fallback.
            preview_renderer = getattr(self, "timeline_preview_renderer", None)
            set_preview_project = getattr(preview_renderer, "set_project", None)
            if callable(set_preview_project):
                set_preview_project(self._current_edit_project)
            self.timeline_panel.set_project(self._current_edit_project)

        # Clean up worker reference (AUD-2706: only once the thread really
        # exited; otherwise the finished-signal sweep removes it later).
        self._release_render_cache_worker_slot(cache_id)
        getattr(self, "_mg_idle_cache_ids", set()).discard(cache_id)

        self.status_bar.showMessage(f"Render cache failed: {error}", 5000)
        logger.error("[RenderCache] Cache failed: %s → %s", cache_id[:8], error)

    # ================================================================
    # Motion Graphic idle cache scheduling
    # ================================================================

    def _ensure_mg_idle_cache_scheduler(self) -> None:
        """Lazily create the idle-scheduler state (mixin has no __init__)."""
        if getattr(self, "_mg_idle_cache_timer", None) is not None:
            return
        timer = QTimer(self)
        timer.setInterval(3000)
        timer.timeout.connect(self._maybe_start_idle_mg_cache)
        self._mg_idle_cache_timer = timer
        self._mg_idle_cache_ids: set[str] = set()
        timer.start()

    # ================================================================
    # N09 · 自动备份调度（工程级命名快照，与磁盘自动保存互不替代）
    # ================================================================

    def _ensure_snapshot_backup(self) -> None:
        """懒建自动备份调度器 + 心跳 QTimer（N09 生产接线）。"""
        if getattr(self, "_snapshot_backup_timer", None) is not None:
            return
        from src.features.edit.core.snapshot_backup_scheduler import (
            SnapshotBackupScheduler,
        )
        scheduler = SnapshotBackupScheduler()
        timer = QTimer(self)
        timer.setInterval(int(scheduler.interval_seconds * 1000))
        timer.timeout.connect(self._auto_backup_heartbeat)
        self._snapshot_backup_scheduler = scheduler
        self._snapshot_backup_timer = timer
        timer.start()

    def _auto_backup_heartbeat(self, now: float | None = None) -> None:
        """心跳：拍备成功的新工程写回在册工程并确认落库（N09）。

        auto_backup 快照不是用户编辑——不过命令栈（不可撤销、不进
        revision），但必须回写在册工程 + dirty，下次保存才持久化
        （"保存重开仍在"的验收口径）。写回失败不 confirm（不虚计）。
        ``now`` 直通调度器时钟（确定性测试用；生产 QTimer 路径走真时）。
        """
        scheduler = getattr(self, "_snapshot_backup_scheduler", None)
        project = getattr(self, "_current_edit_project", None)
        if scheduler is None or project is None:
            return
        new_project = scheduler.tick(project, now=now)
        if new_project is None:
            return
        try:
            self._current_edit_project = new_project
            self.edit_project_dirty = True
            panel = getattr(self, "media_pool_panel", None)
            if panel is not None:
                panel.set_project(new_project)
                panel.project_changed.emit(new_project)
        except Exception:  # noqa: BLE001 — 写回失败：不 confirm，留诊断
            logger.exception("[AutoBackup] 写回在册工程失败，本次不计落库")
            return
        scheduler.confirm_written()
        logger.info(
            "[AutoBackup] 备份落库 revision=%s meta=%s",
            getattr(new_project, "revision", "?"),
            scheduler.last_backup_meta)

    def _on_playback_state_for_mg_idle(
        self, is_playing: bool, speed: float = 0.0, direction: int = 1,
    ) -> None:
        """Keep idle Motion Graphic cache renders away from playback.

        Playback must keep the machine's decode/encode headroom, so any
        idle-started Chromium/FFmpeg recording is cancelled the moment the
        transport starts; when it stops again, a delayed check restarts the
        background render once the session is quiet.
        """
        if is_playing:
            self._cancel_idle_mg_cache_renders()
            return
        # E05: 播放停止（含空格/传输条暂停路径，不经过 EditPage hub 时）
        # 也要提交 Write/Latch 自动化录制。
        self._commit_automation_recordings()
        self._ensure_mg_idle_cache_scheduler()
        QTimer.singleShot(3000, self._maybe_start_idle_mg_cache)

    def _edit_session_is_quiet_for_mg_cache(self) -> bool:
        """Whether playback/scrub activity allows a background cache render."""
        # E05: 权威状态来自播放控制器；edit_page._is_playing 作为兼容回退
        # （生产代码从不设置该属性，测试/嵌入宿主可能直接注入）。
        if self._edit_playback_state()[0]:
            return False
        edit_page = getattr(self, "edit_page", None)
        if edit_page is not None and bool(getattr(edit_page, "_is_playing", False)):
            return False
        # The Color page has its own playback engine; its clips must not
        # compete with a cache recording either.
        video_controller = getattr(self, "video_controller", None)
        if video_controller is not None and bool(getattr(video_controller, "is_playing", False)):
            return False
        renderer = getattr(self, "timeline_preview_renderer", None)
        is_idle = getattr(renderer, "is_render_idle", None)
        if callable(is_idle):
            try:
                if not is_idle():
                    return False
            except Exception:
                logger.debug("MG idle cache: renderer idle probe failed", exc_info=True)
        return True

    def _maybe_start_idle_mg_cache(self) -> None:
        """Render one uncached Motion Graphic clip while the session is idle.

        Uncached clips already play as static frames, so the cache render is
        purely a background quality upgrade.  It runs one clip at a time and
        only when the session is quiet, mirroring the bookkeeping of the
        manual Render Cache Clip command.
        """
        timer = getattr(self, "_mg_idle_cache_timer", None)
        if timer is None:
            return
        if not self._edit_session_is_quiet_for_mg_cache():
            return
        timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
        project = getattr(self, "_current_edit_project", None)
        if timeline is None or project is None:
            return
        workers = getattr(self, "_render_cache_workers", None) or {}
        for worker in tuple(workers.values()):
            manifest = getattr(worker, "_manifest", None)
            if str(getattr(manifest, "render_kind", "")) == "motion_graphic":
                # 串行：一次只跑一个 MG 缓存录制，避免多个 Chromium 实例。
                return

        from src.features.edit.core.render_cache_status import (
            STATUS_CACHED, cache_status_for_clip,
        )

        assets = getattr(project, "assets", {}) or {}
        timeline_id = str(getattr(timeline, "timeline_id", "") or "")
        inflight = getattr(self, "_render_cache_inflight_clips", None) or set()
        for track in getattr(timeline, "all_tracks", ()):
            for clip in getattr(track, "clips", ()):
                clip_id = str(getattr(clip, "clip_id", ""))
                asset = assets.get(getattr(clip, "asset_id", ""))
                if asset is None or str(getattr(asset, "media_type", "")) != "motion_graphic":
                    continue
                if (timeline_id, clip_id) in inflight:
                    continue
                if cache_status_for_clip(project, timeline_id, clip) == STATUS_CACHED:
                    continue
                before = set(getattr(self, "_render_cache_workers", None) or {})
                started = self._on_render_cache_clip_requested(
                    clip_id, str(getattr(track, "track_id", "")),
                )
                if started:
                    new_ids = set(
                        getattr(self, "_render_cache_workers", None) or {}
                    ) - before
                    self._mg_idle_cache_ids.update(new_ids)
                    if new_ids:
                        self.status_bar.showMessage(
                            "空闲时正在后台渲染 Motion Graphic 缓存", 3000,
                        )
                # One clip per tick; the next quiet tick picks up the rest.
                return

    def _cancel_idle_mg_cache_renders(self) -> None:
        """Cancel idle-started MG cache renders and clean their manifests.

        The realtime recorder stops through its progress callback and removes
        its temp file, but it emits neither ``finished_ok`` nor ``failed`` on
        cancellation, so the inflight/manifest cleanup is done here mirroring
        the timeline-invalidation path.
        """
        idle_ids = getattr(self, "_mg_idle_cache_ids", None)
        if not idle_ids:
            return
        workers = getattr(self, "_render_cache_workers", None)
        if not workers:
            self._mg_idle_cache_ids.clear()
            return
        project = getattr(self, "_current_edit_project", None)
        inflight_cache_keys = getattr(self, "_render_cache_inflight_cache_keys", None) or {}
        inflight_clips = getattr(self, "_render_cache_inflight_clips", None) or set()
        for cache_id in tuple(idle_ids):
            worker = workers.pop(cache_id, None)
            if worker is not None:
                try:
                    worker.cancel()
                except Exception:
                    logger.debug("Could not cancel idle Motion Graphic worker", exc_info=True)
            inflight_key = inflight_cache_keys.pop(cache_id, None)
            if inflight_key is not None:
                inflight_clips.discard(inflight_key)
                if project is not None:
                    self._cleanup_obsolete_render_cache_files(
                        project,
                        clip_id=inflight_key[1],
                        include_rendering=True,
                        preserve_valid_fingerprinted=True,
                    )
                    project = project.invalidate_render_cache_for_clip(
                        inflight_key[0], inflight_key[1],
                    )
        self._mg_idle_cache_ids.clear()
        if project is not None and project is not self._current_edit_project:
            self._current_edit_project = project
            panel = getattr(self, "timeline_panel", None)
            if panel is not None and hasattr(panel, "set_project"):
                panel.set_project(project)
            media_pool = getattr(self, "media_pool_panel", None)
            if media_pool is not None and hasattr(media_pool, "set_project"):
                media_pool.set_project(project)
            preview_renderer = getattr(self, "timeline_preview_renderer", None)
            set_preview_project = getattr(preview_renderer, "set_project", None)
            if callable(set_preview_project):
                set_preview_project(project)

    def create_comp_clip_for_timeline_clip(self, clip_id: str, track_id: str = "") -> str | None:
        """
        Explicitly create a Comp Clip for a timeline clip.

        This is the Phase 2 unified entry point. Does NOT auto-create via page switch.
        Creates: composition graph + comp clip asset + media pool entry.

        Args:
            clip_id: The timeline clip ID.
            track_id: Optional track ID hint.

        Returns:
            composite_id on success, None on failure.
        """
        if not self._current_edit_project:
            logger.warning("[Phase2] No project for Comp Clip creation")
            return None

        timeline = self.edit_page._current_timeline if hasattr(self, 'edit_page') and self.edit_page else None
        if not timeline:
            logger.warning("[Phase2] No timeline for Comp Clip creation")
            return None

        # Find clip and track
        clip = None
        track = None
        for t in timeline.all_tracks:
            c = t.get_clip_by_id(clip_id)
            if c:
                clip = c
                track = t
                break

        if clip is None:
            logger.warning(f"[Phase2] Clip not found: {clip_id}")
            return None

        # Already has composite_id?
        if clip.composite_id:
            logger.info(f"[Phase2] Clip already has composite: {clip.composite_id}")
            return clip.composite_id

        # Ensure clip has color_stack_id
        color_stack_id = getattr(clip, 'color_stack_id', None)
        if not color_stack_id:
            color_stack_id = self._ensure_clip_has_color_stack(
                clip_id, clip.asset_id
            )
            if color_stack_id:
                # Reload clip after ensuring color stack
                timeline = self.edit_page._current_timeline
                for t in timeline.all_tracks:
                    c = t.get_clip_by_id(clip_id)
                    if c:
                        clip = c
                        track = t
                        break

        # Get real asset metadata
        asset = self._current_edit_project.get_asset_by_id(clip.asset_id)
        if asset is None:
            logger.warning(f"[Phase2] Asset not found: {clip.asset_id}")
            return None

        # Create Comp Clip using EditProject method
        try:
            new_project, comp_clip_id, comp_asset_id, graph_id = \
                self._current_edit_project.create_comp_clip_from_timeline_clip(
                    timeline_id=timeline.timeline_id,
                    clip_id=clip.clip_id,
                    timeline=timeline,
                    # The timeline-facing name is intentionally independent
                    # of the source asset.  It identifies the new graph and
                    # remains readable even when the source is renamed.
                    name=None,
                )
        except Exception as e:
            logger.error(f"[Phase2] Failed to create Comp Clip: {e}")
            return None

        # Update clip: set composite_id, keep original asset_id (Phase 2 recommendation)
        new_clip = clip.with_composite_id(graph_id)
        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)

        # Update state.  The explicit conversion must update the immutable
        # timeline/project snapshot as one operation so autosave, undo/redo,
        # the renderer and the media pool all see the same clip binding.
        self._current_edit_project = new_project
        if hasattr(self, 'edit_page') and self.edit_page:
            self.edit_page._current_timeline = new_timeline
        if hasattr(self, '_current_timeline_sequences'):
            self._current_timeline_sequences[new_timeline.timeline_id] = new_timeline

        # create_comp_clip_from_timeline_clip returns a project whose timeline
        # refs still describe the pre-binding sequence.  Publish the new
        # sequence through the normal timeline-change path so the project
        # reference, renderer and autosave state cannot drift apart.
        self._on_edit_timeline_changed(new_timeline)

        # Refresh UI
        if hasattr(self, 'timeline_panel') and self.timeline_panel:
            timeline_view = self.timeline_panel.get_timeline_view()
            if timeline_view:
                timeline_view.load_timeline(new_timeline)

        if hasattr(self, 'media_pool_panel') and self.media_pool_panel:
            self.media_pool_panel.set_project(self._current_edit_project)


        logger.info(
            f"[Phase2] Created Comp Clip: comp_clip_id={comp_clip_id}, "
            f"graph_id={graph_id}, asset_id={comp_asset_id}"
        )
        return graph_id

    def reset_composite_for_clip(self, clip_id: str) -> None:
        """Reset a Comp Clip's graph to default (TimelineClipInput -> Topos Color -> Output)."""
        clip = self._get_clip_by_id(clip_id)
        if not clip or not clip.composite_id:
            logger.warning(f"[Phase2] Cannot reset: no composite for clip {clip_id}")
            return

        # Use EditProject.reset_comp_clip_graph
        new_project = self._current_edit_project.reset_comp_clip_graph(clip.composite_id)
        self._current_edit_project = new_project

        self._refresh_edit_preview_after_composite_change()
        self.status_bar.showMessage("Composite reset to default", 2000)

        logger.info(f"[Phase2] Reset composite graph for clip {clip_id}")

    def remove_composite_binding_for_clip(self, clip_id: str) -> None:
        """Remove composite binding from a clip (keeps clip on timeline, removes comp association)."""
        if not self._current_edit_project:
            return

        timeline = self.edit_page._current_timeline if hasattr(self, 'edit_page') and self.edit_page else None
        if not timeline:
            return

        clip = None
        track = None
        for t in timeline.all_tracks:
            c = t.get_clip_by_id(clip_id)
            if c:
                clip = c
                track = t
                break

        if not clip or not clip.composite_id:
            return

        # Remove composite_id from clip
        new_clip = clip.with_composite_id(None) if hasattr(clip, 'with_composite_id') else clip
        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)

        # Update state
        self._current_edit_project = self._current_edit_project  # keep project data for now
        if hasattr(self, 'edit_page') and self.edit_page:
            self.edit_page._current_timeline = new_timeline
        if hasattr(self, '_current_timeline_sequences'):
            self._current_timeline_sequences[new_timeline.timeline_id] = new_timeline

        # Refresh UI
        if hasattr(self, 'timeline_panel') and self.timeline_panel:
            timeline_view = self.timeline_panel.get_timeline_view()
            if timeline_view:
                timeline_view.load_timeline(new_timeline)

        self._refresh_edit_preview_after_composite_change()
        self.status_bar.showMessage("Composite binding removed", 2000)

        logger.info(f"[Phase2] Removed composite binding from clip {clip_id}")

    # ------------------------------------------------------------------
    # P9: Scene Cut Detection — menu action callback
    # ------------------------------------------------------------------

    def on_timeline_add_marker(self) -> None:
        """Add a marker at the active Edit timeline playhead."""
        timeline_panel = getattr(self, "timeline_panel", None)
        timeline_view = getattr(timeline_panel, "timeline_view", None)
        if timeline_view is None:
            logger.warning("Add marker: no timeline view available")
            return
        timeline_view.add_marker_at_playhead()

    def on_toggle_playback_hud(self) -> None:
        """
        Handle View → Timeline → Playback HUD (P1-23 性能 HUD).

        Delegates to TimelinePanel.toggle_playback_hud().
        Registered via callback_name="on_toggle_playback_hud" in module.py.
        """
        panel = getattr(self, "timeline_panel", None)
        if panel is None or not hasattr(panel, "toggle_playback_hud"):
            logger.warning("Playback HUD: no timeline panel available")
            return
        panel.toggle_playback_hud()

    def on_export_performance_report(self) -> None:
        """
        Handle File → Export 脱敏性能报告 (P0-10 诊断导出).

        Delegates to TimelinePanel.export_performance_report()（含保存
        对话框）。导出内容：帧 trace + 引擎遥测 + GPU pass 时间 + 拷贝
        字节 + 资源快照摘要；素材/工程路径已遮蔽。
        Registered via callback_name="on_export_performance_report" in module.py.
        """
        panel = getattr(self, "timeline_panel", None)
        if panel is None or not hasattr(panel, "export_performance_report"):
            logger.warning("Performance report: no timeline panel available")
            return
        try:
            exported = panel.export_performance_report()
        except Exception as e:
            logger.error(f"Performance report export failed: {e}")
            return
        if exported:
            logger.info(f"Performance report exported -> {exported}")

    # ── View → Timeline 缩放动作（AUD-19302）──────────────────────────
    # module.py 注册的 edit.timeline.zoom_in / zoom_out / fit 三个动作此前
    # 没有可解析的 callback（MenuBuilder._build_action 因回调缺失整条跳过），
    # 菜单路径在 AUD-3600 修复后仍不可达。三个处理器委托 TimelineView 的
    # 公共缩放 API（与工具栏按钮同一实现；滑块经 _on_view_zoom_changed
    # 信号自动同步）。

    def on_timeline_zoom_in(self) -> None:
        """
        Handle View → Timeline → Zoom In.

        Delegates to TimelineView.zoom_in()（步进放大，与工具栏 Zoom In
        按钮一致）。
        Registered via callback_name="on_timeline_zoom_in" in module.py.
        """
        timeline_view = self._resolve_timeline_view("Zoom in")
        if timeline_view is not None:
            timeline_view.zoom_in()

    def on_timeline_zoom_out(self) -> None:
        """
        Handle View → Timeline → Zoom Out.

        Delegates to TimelineView.zoom_out()（步进缩小，与工具栏 Zoom Out
        按钮一致）。
        Registered via callback_name="on_timeline_zoom_out" in module.py.
        """
        timeline_view = self._resolve_timeline_view("Zoom out")
        if timeline_view is not None:
            timeline_view.zoom_out()

    def on_timeline_fit(self) -> None:
        """
        Handle View → Timeline → Fit to Window.

        Delegates to TimelineView.zoom_fit_content()（整段时间线适配当前
        视口，与工具栏 Fit 按钮一致）。
        Registered via callback_name="on_timeline_fit" in module.py.
        """
        timeline_view = self._resolve_timeline_view("Fit to window")
        if timeline_view is not None:
            timeline_view.zoom_fit_content()

    def _resolve_timeline_view(self, action_label: str):
        """定位当前 TimelineView；缺失时记 warning 并返回 None。"""
        panel = getattr(self, "timeline_panel", None)
        timeline_view = getattr(panel, "timeline_view", None) if panel else None
        if timeline_view is None or not hasattr(timeline_view, "zoom_in"):
            logger.warning("%s: no timeline view available", action_label)
            return None
        return timeline_view

    def on_scene_cut_detect(self) -> None:
        """
        Handle Scene Cut Detection menu action (Edit → Timeline → Scene Cut Detection).

        Delegates to EditPage.on_scene_cut_detect() which runs detection
        on the currently selected timeline clip.

        Registered via callback_name="on_scene_cut_detect" in module.py.
        """
        if not hasattr(self, 'edit_page') or self.edit_page is None:
            logger.warning("Scene cut detect: no edit page available")
            return

        if hasattr(self.edit_page, 'on_scene_cut_detect'):
            self.edit_page.on_scene_cut_detect()
        else:
            logger.warning("EditPage does not support scene cut detection")

    def on_toggle_scene_cut_overlay(self, checked: bool = False) -> None:
        """
        Handle View → Timeline → Scene Cut Overlay checkable action (B1).

        Delegates to EditPage.set_scene_cut_overlay_visible(), which owns the
        persisted switch shared with the SceneCutDialog "Show on Timeline"
        toggle.  The overlay itself shows the latest detection candidates as
        presentation-only markers on the timeline.

        Registered via callback_name="on_toggle_scene_cut_overlay" in module.py.

        Args:
            checked: New checked state emitted by the QAction.
        """
        page = getattr(self, "edit_page", None)
        if page is None or not hasattr(page, "set_scene_cut_overlay_visible"):
            logger.warning("Scene cut overlay: no edit page available")
            return
        page.set_scene_cut_overlay_visible(bool(checked))

    def on_create_placeholder_clip(self) -> None:
        """Edit→Timeline→New Placeholder...（B7）：创建占位素材并落到目标轨。"""
        from PyQt6.QtWidgets import QDialog

        from src.features.edit.core.commands import AddClipCommand
        from src.features.edit.core.project import create_placeholder_asset
        from src.features.edit.core.timeline_v2 import ClipInstance
        from src.features.edit.ui.placeholder_dialog import PlaceholderDialog

        timeline = (
            getattr(self.edit_page, "_current_timeline", None)
            if getattr(self, "edit_page", None) is not None else None)
        project = getattr(self, "_current_edit_project", None)
        if timeline is None or project is None:
            return
        fps = float(getattr(timeline, "fps", 24.0).num
                    if hasattr(getattr(timeline, "fps", None), "num")
                    else getattr(timeline, "fps", 24.0) or 24.0)

        dialog = PlaceholderDialog(default_fps=fps, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        new_project, asset = create_placeholder_asset(
            project,
            dialog.placeholder_name,
            width=int(getattr(timeline, "width", 1920) or 1920),
            height=int(getattr(timeline, "height", 1080) or 1080),
            fps=fps,
            duration_frames=dialog.duration_frames,
            note=dialog.note,
        )
        self._current_edit_project = new_project

        track_id = None
        playhead = 0
        view_getter = getattr(getattr(self, "timeline_panel", None),
                              "get_timeline_view", None)
        view = view_getter() if callable(view_getter) else None
        if view is not None:
            track_id = view.get_target_track_id()
            playhead = int(getattr(view, "_playhead_frame", 0))
        if not track_id:
            track_id = next(iter(new_project.timelines[
                new_project.active_timeline_id].video_tracks)).track_id \
                if new_project.timelines else "v1"
        try:
            timeline_obj = self.edit_page._current_timeline
        except AttributeError:
            timeline_obj = None
        clip = ClipInstance(
            asset_id=asset.asset_id, track_id=track_id,
            timeline_start=max(0, playhead),
            timeline_duration=dialog.duration_frames,
            source_start=0, source_duration=dialog.duration_frames,
        )
        command = AddClipCommand.create(track_id, clip)
        # EditPage 没有 _command_stack：旧代码 hasattr 恒为假导致命令从未
        # 执行（素材创建了但没落轨）。统一走 TimelineView.execute_command。
        if view is not None and hasattr(view, "execute_command"):
            view.execute_command(command)
        elif timeline_obj is not None:
            new_timeline = command.apply(timeline_obj)
            self.edit_page._current_timeline = new_timeline

    def _on_title_template_selected(self, preset_id: str) -> None:
        """C1: 在播放头处创建应用标题模板样式的字幕 clip。"""

        from src.features.edit.core.captions.model import CaptionStyle

        # 查找预设（内置 + 用户模板）
        from src.features.edit.core.captions.style_preset import BUILTIN_PRESETS
        from src.features.edit.core.title_templates import (
            list_user_title_templates,
        )

        preset = None
        for candidate in list(BUILTIN_PRESETS) + list_user_title_templates():
            if candidate.preset_id == preset_id:
                preset = candidate
                break
        if preset is None:
            return
        timeline = (
            getattr(self.edit_page, "_current_timeline", None)
            if getattr(self, "edit_page", None) is not None else None)
        if timeline is None:
            return

        from src.features.edit.core.commands import (
            AddSubtitleClipCommand,
            AddTrackCommand,
            MacroCommand,
            UpdateSubtitleClipCommand,
        )

        playhead = int(getattr(
            getattr(getattr(self, "timeline_panel", None), "timeline_view", None),
            "_playhead_frame", 0) or 0)

        # 字幕轨：无则先建一条
        track_id = (
            timeline.subtitle_tracks[0].track_id
            if timeline.subtitle_tracks else None)
        commands: list = []
        if track_id is None:
            track_id = f"sub_{uuid.uuid4().hex[:8]}"
            commands.append(AddTrackCommand.create(
                "subtitle", "Titles", len(timeline.subtitle_tracks), track_id))

        add_cmd = AddSubtitleClipCommand.create(
            track_id=track_id, timeline_start=playhead, text=preset.label)
        commands.append(add_cmd)
        macro = MacroCommand.create(
            f"Apply title template {preset.label}", commands)
        # EditPage 没有 _command_stack（AttributeError 即崩）；统一走
        # TimelineView.execute_command 以保持命令栈/视图/自动保存一致。
        view_getter = getattr(getattr(self, "timeline_panel", None),
                              "get_timeline_view", None)
        view = view_getter() if callable(view_getter) else None
        if view is not None and hasattr(view, "execute_command"):
            view.execute_command(macro)
            new_timeline = getattr(view, "_timeline", None) or timeline
        else:
            # AUD-2705: MacroCommand.apply (and any compound command)
            # returns a ``(new_timeline, updated_command)`` tuple — unlike
            # leaf commands which return the timeline directly. Unpack it,
            # otherwise the tuple gets stored as the "current timeline" and
            # the next get_clip_by_id crashes with AttributeError: 'tuple'
            # object has no attribute 'get_clip_by_id' (same contract note
            # as _on_edit_effect_params_changed).
            result = macro.apply(timeline)
            new_timeline = (
                result[0] if isinstance(result, tuple) else result)
            self.edit_page._current_timeline = new_timeline

        created_id = getattr(add_cmd, "_created_clip_id", None)
        if created_id and preset.style is not None:
            style_updates = {
                field: value
                for field, value in vars(preset.style).items()
                if value is not None
            }
            if style_updates:
                update_cmd = UpdateSubtitleClipCommand(
                    clip_id=created_id,
                    label=f"Apply title style {preset.label}",
                    updates={"style_override": CaptionStyle(**style_updates)},
                )
                if view is not None and hasattr(view, "execute_command"):
                    view.execute_command(update_cmd)
                    new_timeline = getattr(view, "_timeline", None) or new_timeline
                else:
                    new_timeline = update_cmd.apply(new_timeline)
        if new_timeline is not None:
            self.edit_page._current_timeline = new_timeline
        self.status_bar.showMessage(
            I18N.tr("edit.effects.titles_created", "已创建标题（{name}）").format(
                name=preset.label), 3000)

    def _on_scene_cut_overlay_visibility_changed(self, visible: bool) -> None:
        """Keep the checkable Scene Cut Overlay menu action in sync (B1)."""
        actions = getattr(self, "_module_actions", None) or {}
        action = actions.get("edit.timeline.scene_cut_overlay")
        if action is not None:
            action.setChecked(bool(visible))

    # ------------------------------------------------------------------
    # P10.3: Subtitle clip operations
    # ------------------------------------------------------------------

    def _get_subtitle_command_classes(self):
        """Lazily import subtitle command classes (avoids import cycles)."""
        from src.features.edit.core.commands import (
            AddSubtitleClipCommand,
            CopySubtitleClipCommand,
            UpdateSubtitleClipCommand,
            DeleteSubtitleClipCommand,
            MoveSubtitleClipCommand,
        )
        return {
            "AddSubtitleClipCommand": AddSubtitleClipCommand,
            "CopySubtitleClipCommand": CopySubtitleClipCommand,
            "UpdateSubtitleClipCommand": UpdateSubtitleClipCommand,
            "DeleteSubtitleClipCommand": DeleteSubtitleClipCommand,
            "MoveSubtitleClipCommand": MoveSubtitleClipCommand,
        }

    def _execute_subtitle_command(self, command) -> None:
        """Commit a subtitle command through the unified timeline entry.

        SUB-P0-01: subtitle edits must NOT mutate ``edit_page._current_timeline``
        directly nor force a private ``load_timeline``. They now go through
        ``TimelineView.execute_command``, which is the single commit path
        shared with regular clip edits. That path atomically:
          - pushes the command onto the shared undo/redo stack,
          - updates ``TimelineView._timeline`` and (via ``timeline_changed``)
            ``edit_page._current_timeline`` and the project sequence cache,
          - syncs the preview renderer,
          - rebuilds/updates the timeline view items, and
          - marks the project dirty for autosave.

        ``EditPage.set_timeline`` (invoked from ``_on_edit_timeline_changed``)
        is responsible for refreshing the subtitle overlay and invalidating
        the glyph cache, so this method no longer does that manually.

        Failures (e.g. overlap validation) surface as a status-bar message.
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            logger.warning("Subtitle command: no active timeline")
            return
        timeline_view = None
        if hasattr(self, 'timeline_panel') and hasattr(self.timeline_panel, 'timeline_view'):
            timeline_view = self.timeline_panel.timeline_view
        if timeline_view is None:
            logger.warning("Subtitle command: no timeline view to commit through")
            return
        try:
            timeline_view.execute_command(command)
        except Exception as exc:
            logger.exception(f"Subtitle command failed: {exc}")
            if hasattr(self, 'status_bar') and self.status_bar is not None:
                self.status_bar.showMessage(f"Subtitle operation failed: {exc}", 4000)
            return False
        return True

    def _commit_subtitle_drag_or_restore(
        self,
        command,
        clip_id: str,
        original_start: int,
        original_duration: int,
    ) -> bool:
        """Commit a subtitle move/trim; restore the item geometry on failure.

        SUB-P0-04: a release-time move/trim can still fail overlap validation
        even after a live drag (e.g. a clamp gap existed in the item but the
        committed value collides with a neighbour). When that happens the
        SubtitleClipItem must snap back to its pre-drag (start, duration)
        instead of lingering in an illegal position until the next reload.
        Returns True on success, False if the commit failed and was restored.
        """
        ok = self._execute_subtitle_command(command)
        if ok:
            return True
        # Locate the SubtitleClipItem and restore its geometry.
        try:
            from src.features.edit.core.captions.edit_policy import (
                SubtitleEditPolicy,
            )
            timeline_view = None
            if hasattr(self, 'timeline_panel') and hasattr(self.timeline_panel, 'timeline_view'):
                timeline_view = self.timeline_panel.timeline_view
            if timeline_view is not None:
                item = timeline_view._items_map.get(clip_id)
                SubtitleEditPolicy.restore_on_failure(item, original_start, original_duration)
                # Cross-track preview may have moved the item's Y position;
                # restore it to the source lane as well as its geometry.
                timeline_view._layout_track_items()
        except Exception:
            logger.exception("Failed to restore subtitle item geometry after drag failure")
        return False

    def _get_subtitle_clip_field(self, clip_id: str, field: str, default):
        """Read a SubtitleClip field from the current timeline (best-effort)."""
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return default
        clip = self.edit_page._current_timeline.get_subtitle_clip_by_id(clip_id)
        if clip is None:
            return default
        return getattr(clip, field, default)

    def _get_subtitle_clip_duration(self, clip_id: str) -> int:
        return int(self._get_subtitle_clip_field(clip_id, "timeline_duration", 72))

    def _get_subtitle_clip_start(self, clip_id: str) -> int:
        return int(self._get_subtitle_clip_field(clip_id, "timeline_start", 0))

    def on_add_subtitle_clip(self, track_id: str, at_frame: int) -> None:
        """Handle 'Add Subtitle Clip' request from subtitle track header.

        SUB-P1-07: defaults to OVERWRITE mode; the created cue is auto-selected
        (shown in the properties panel + ProgramViewer handles) so the user can
        immediately type its text, matching the professional-NLE workflow.
        """
        logger.info(f"[P10.3] Add subtitle clip: track={track_id} at={at_frame}")
        cmds = self._get_subtitle_command_classes()
        command = cmds["AddSubtitleClipCommand"].create(
            track_id=track_id,
            timeline_start=max(0, at_frame),
        )
        ok = self._execute_subtitle_command(command)
        self.status_bar.showMessage("Subtitle clip added", 2000)
        if ok:
            # Auto-select the newly created cue for immediate editing.
            created_id = getattr(command, "_created_clip_id", None)
            if created_id is not None:
                self.on_subtitle_clip_clicked(created_id, track_id)

    def on_subtitle_clip_drag_finished(
        self,
        clip_id: str,
        track_id: str,
        old_start: int,
        new_start: int,
        target_track_id: str,
        copy_requested: bool,
    ) -> None:
        """Commit a subtitle move or Alt-drag copy through one command.

        A cross-track move needs an atomic remove/add command because updating
        ``track_id`` in place would violate the immutable owner-track model.
        Alt-drag preserves the source cue and creates a new cue on the target.
        """
        logger.debug(
            "[P10.3] Subtitle drag: %s %s→%s track %s→%s copy=%s",
            clip_id, old_start, new_start, track_id, target_track_id,
            copy_requested,
        )
        cmds = self._get_subtitle_command_classes()
        # Duration is unchanged for a move; fetch it for a faithful restore.
        duration = self._get_subtitle_clip_duration(clip_id)
        if copy_requested:
            command = cmds["CopySubtitleClipCommand"].create(
                source_clip_id=clip_id,
                target_track_id=target_track_id,
                timeline_start=new_start,
            )
            if self._commit_subtitle_drag_or_restore(
                command, clip_id, old_start, duration,
            ):
                copied_id = getattr(command, "_copied_clip_id", None)
                if copied_id is not None:
                    self.on_subtitle_clip_clicked(copied_id, target_track_id)
            return

        command = cmds["MoveSubtitleClipCommand"].create(
            clip_id=clip_id,
            target_track_id=target_track_id,
            timeline_start=new_start,
            overwrite_overlaps=(target_track_id == track_id),
        )
        if self._commit_subtitle_drag_or_restore(
            command, clip_id, old_start, duration,
        ):
            # The cue's target track can provide a different default caption
            # style. Refresh panel and viewer selection after the command has
            # committed so their immutable clip reference follows the move.
            self.on_subtitle_clip_clicked(clip_id, target_track_id)

    def on_subtitle_clip_trim_left_finished(self, clip_id: str, track_id: str,
                                             old_start: int, old_dur: int,
                                             new_start: int, new_dur: int) -> None:
        """Handle subtitle clip left-trim completion.

        SUB-P0-04: restore to (old_start, old_dur) on commit failure.
        """
        logger.debug(f"[P10.3] Subtitle trim-L: {clip_id} start {old_start}→{new_start}, dur {old_dur}→{new_dur}")
        cmds = self._get_subtitle_command_classes()
        command = cmds["UpdateSubtitleClipCommand"].create(
            clip_id=clip_id,
            updates={"timeline_start": new_start, "timeline_duration": new_dur},
            label="Trim Subtitle Left",
        )
        self._commit_subtitle_drag_or_restore(command, clip_id, old_start, old_dur)

    def on_subtitle_clip_trim_right_finished(self, clip_id: str, track_id: str,
                                              old_dur: int, new_dur: int) -> None:
        """Handle subtitle clip right-trim completion.

        SUB-P0-04: restore to the clip's current start with old_dur on failure.
        """
        logger.debug(f"[P10.3] Subtitle trim-R: {clip_id} dur {old_dur}→{new_dur}")
        cmds = self._get_subtitle_command_classes()
        command = cmds["UpdateSubtitleClipCommand"].create(
            clip_id=clip_id,
            updates={"timeline_duration": new_dur},
            label="Trim Subtitle Right",
        )
        start = self._get_subtitle_clip_start(clip_id)
        self._commit_subtitle_drag_or_restore(command, clip_id, start, old_dur)

    def on_subtitle_clip_delete(self, clip_id: str, track_id: str) -> None:
        """Handle subtitle clip delete."""
        logger.info(f"[P10.3] Delete subtitle clip: {clip_id}")
        cmds = self._get_subtitle_command_classes()
        command = cmds["DeleteSubtitleClipCommand"].create(clip_id=clip_id)
        self._execute_subtitle_command(command)
        self.status_bar.showMessage("Subtitle clip deleted", 2000)

    def on_caption_graphic_action(self, clip_id: str, action: str) -> None:
        """Route caption-graphic context-menu actions through CommandStack."""
        if action == "open_composite":
            timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
            # The context menu is attached to the materialized graphic clip,
            # while get_caption_graphic_binding() expects the source cue id.
            # Resolve by graphic clip id so the P4 open-in-Composite flow
            # works for both live and detached bindings.
            binding = (
                timeline.get_caption_graphic_binding_by_clip(clip_id)
                if timeline is not None
                and hasattr(timeline, "get_caption_graphic_binding_by_clip")
                else None
            )
            if binding is not None and binding.composite_id:
                self._switch_to_composite_page(binding.composite_id)
            else:
                logger.warning("Caption graphic has no Composite instance: %s", clip_id)
            return
        from src.features.edit.core.captions.commands import (
            DemoteGraphicToSubtitleCommand,
            DetachCaptionGraphicCommand,
            PromoteSubtitleToGraphicCommand,
        )
        commands = {
            "promote": PromoteSubtitleToGraphicCommand.create(clip_id),
            "detach": DetachCaptionGraphicCommand.create(clip_id),
            "demote": DemoteGraphicToSubtitleCommand.create(clip_id),
        }
        command = commands.get(action)
        if command is None:
            logger.warning("Unknown caption graphic action: %s", action)
            return
        if self._execute_subtitle_command(command):
            if hasattr(self, "status_bar") and self.status_bar is not None:
                self.status_bar.showMessage(command.label, 2000)

    def on_caption_track_graphic_template(self, track_id: str, template_id: str) -> None:
        """Apply/remove a lazy caption track template through CommandStack."""
        from src.features.edit.core.captions.commands import (
            ApplyCaptionTrackTemplateCommand,
        )
        command = ApplyCaptionTrackTemplateCommand.create(
            track_id, template_id or None,
            label=("Remove Caption Track Template" if not template_id
                   else "Apply Caption Track Template"),
        )
        if self._execute_subtitle_command(command):
            if hasattr(self, "status_bar") and self.status_bar is not None:
                self.status_bar.showMessage(command.label, 2000)

    def on_caption_effect_apply(
        self,
        template_id: str,
        clip_id: str,
        parameters: dict,
    ) -> None:
        """Apply or replace one non-destructive animated subtitle effect.

        A cue is promoted only once.  Later effect changes update its existing
        binding in place, preserving its graphics layer, sync policy and a
        single undo step.  The effect registry is the validation boundary so
        a stale UI or project file cannot attach an unknown template.
        """
        timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
        if timeline is None:
            return
        cue = timeline.get_subtitle_clip_by_id(clip_id)
        owner = timeline.find_subtitle_clip_owner_track(clip_id)
        if cue is None or owner is None:
            logger.warning("Caption effect target is missing: %s", clip_id)
            return
        if owner.is_locked or cue.is_edit_locked():
            if hasattr(self, "status_bar") and self.status_bar is not None:
                self.status_bar.showMessage("Subtitle is locked", 2500)
            return

        registry = timeline.get_caption_graphic_template_registry()
        template = registry.get(template_id)
        if template is None:
            logger.warning("Unknown caption graphic template: %s", template_id)
            if hasattr(self, "status_bar") and self.status_bar is not None:
                self.status_bar.showMessage("Subtitle effect is unavailable", 2500)
            return

        # Only persist parameters explicitly published by the selected template.
        # Defaults stay on the immutable template descriptor, keeping projects
        # compact and allowing a future template version to supply new defaults.
        overrides = {
            key: value for key, value in dict(parameters or {}).items()
            if key in template.published_parameters
        }
        from src.features.edit.core.captions.commands import (
            PromoteSubtitleToGraphicCommand,
            UpdateCaptionGraphicBindingCommand,
        )

        binding = timeline.get_caption_graphic_binding(clip_id)
        if binding is None:
            command = PromoteSubtitleToGraphicCommand.create(
                clip_id,
                template_id=template.template_id,
                template_version=template.version,
                parameters=overrides,
            )
        else:
            command = UpdateCaptionGraphicBindingCommand.create(
                clip_id,
                overrides,
                template_id=template.template_id,
                template_version=template.version,
            )
        if self._execute_subtitle_command(command):
            self.on_subtitle_clip_clicked(clip_id, owner.track_id)
            if hasattr(self, "status_bar") and self.status_bar is not None:
                self.status_bar.showMessage(
                    f"Applied subtitle effect: {template.label or template.template_id}",
                    2500,
                )

    def on_subtitle_clip_edit_text(self, clip_id: str, track_id: str, new_text: str) -> None:
        """Handle subtitle text edit (from double-click inline dialog)."""
        logger.info(f"[P10.3] Edit subtitle text: {clip_id}")
        cmds = self._get_subtitle_command_classes()
        command = cmds["UpdateSubtitleClipCommand"].create(
            clip_id=clip_id,
            updates={"text": new_text},
            label="Edit Subtitle Text",
        )
        self._execute_subtitle_command(command)

    def on_subtitle_find_hit(self, clip_id: str, track_id: str) -> None:
        """U05（第六轮审计）：字幕 Find 命中 → 播放头/画面定位到 cue 开始。

        普通行选择（``on_subtitle_clip_clicked``）仍只做选择；Find 是用户
        主动定位动作，二者分离。播放头已在 cue 内时不跳（避免逐条替换时
        画面抖动）。seek 经 playback controller → frame_changed，U04 修复
        后可见播放头/EditPage/预览四端同步；cue_selected 的选择路径不发
        seek，无信号递归。
        """
        _ = track_id
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        timeline = self.edit_page._current_timeline
        clip = timeline.get_subtitle_clip_by_id(clip_id)
        if clip is None:
            return
        panel = getattr(self, "timeline_panel", None)
        if panel is None:
            return
        controller = panel.get_playback_controller()
        if controller is None:
            return
        current = int(getattr(controller, "current_frame", 0) or 0)
        start = int(clip.timeline_start)
        end = int(clip.timeline_end)
        if start <= current < end:
            return  # 已在该 cue 内，不重复 seek
        try:
            if getattr(controller, "is_playing", False):
                controller.pause()
            controller.seek(start)
        except Exception:
            logger.exception("Caption find-hit seek failed for %s", clip_id)

    def on_subtitle_clip_clicked(self, clip_id: str, track_id: str) -> None:
        """Handle subtitle clip selection — show in properties panel + handles."""
        logger.debug(f"[P10.3] Subtitle clip selected: {clip_id}")
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        timeline = self.edit_page._current_timeline
        clip = timeline.get_subtitle_clip_by_id(clip_id)
        if clip is None:
            return
        effects_panel = getattr(self, "edit_effects_panel", None)
        if effects_panel is not None and hasattr(effects_panel, "set_selected_subtitle_clip"):
            effects_panel.set_selected_subtitle_clip(clip_id)
        # 显示字幕片段参数面板
        try:
            panel = getattr(self, "edit_properties_panel", None)
            current_clip = getattr(panel, "_current_subtitle_clip", None)
            is_same_selection = (
                current_clip is not None
                and getattr(current_clip, "clip_id", None) == clip.clip_id
                and getattr(current_clip, "track_id", None) == clip.track_id
            )
            if panel is not None and not is_same_selection:
                fps = float(getattr(timeline, 'fps', 24.0))
                if hasattr(fps, 'to_fps'):
                    fps = float(fps.to_fps())
                canvas_height = int(getattr(timeline, "height", 1080) or 1080)
                try:
                    panel.set_subtitle_clip(
                        clip, fps, canvas_height,
                        resolved_style=self._resolve_subtitle_style(timeline, clip),
                    )
                except TypeError:
                    # Keep compatibility with lightweight panel adapters and
                    # older extensions that still expose the original
                    # (clip, fps) signature.  The built-in panel accepts the
                    # canvas height and uses it to resolve automatic font size.
                    panel.set_subtitle_clip(clip, fps)
            elif panel is not None:
                # Timeline items emit a selection signal on every drag press.
                # Refresh the model reference without tearing down/rebuilding
                # native combo-box popups for the already selected subtitle.
                panel._current_subtitle_clip = clip
        except Exception:
            logger.exception(f"Failed to show subtitle clip in properties panel: {clip_id}")
        # 在 ProgramViewer 显示变换手柄
        try:
            viewer = getattr(self.edit_page, '_program_viewer', None)
            if viewer is not None:
                viewer.set_selected_subtitle_clip(
                    clip, style=self._resolve_subtitle_style(timeline, clip)
                )
        except Exception:
            logger.exception(f"Failed to set selected subtitle clip on viewer: {clip_id}")
        # SUB-P2-03: reverse-direction sync — highlight the cue row in the
        # Caption Workspace. select_clip is guarded by _building_ui so it does
        # NOT re-emit cue_selected (no feedback loop).
        cwp = getattr(self, 'caption_workspace_panel', None)
        if cwp is not None:
            try:
                cwp.select_clip(clip_id)
            except Exception:
                logger.debug("Caption workspace select_clip failed", exc_info=True)

    def on_subtitle_param_changed(self, clip_id: str, track_id: str,
                                  param_name: str, value: object) -> None:
        """Handle subtitle parameter change from properties panel.

        构造 UpdateSubtitleClipCommand 把单字段更新写入 timeline。
        不重建面板（避免输入框失焦）；面板的 widget 值已是用户输入的最新值，
        timeline 命令栈会持久化。下次选择该 clip 时会读取最新值。
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        logger.debug(f"[P10.3] Subtitle param: {clip_id}.{param_name} = {value}")
        panel = getattr(self, "edit_properties_panel", None)
        scope = getattr(panel, "_subtitle_scope", "legacy") if panel is not None else "legacy"
        timeline = self.edit_page._current_timeline
        clip = timeline.get_subtitle_clip_by_id(clip_id)
        if clip is None:
            return
        if scope == "global" and param_name in self._subtitle_style_fields():
            self.on_subtitle_track_param_changed(track_id, param_name, value)
            return
        cmds = self._get_subtitle_command_classes()
        if scope == "custom" and param_name in self._subtitle_style_fields():
            updates = self._custom_subtitle_style_updates(timeline, clip, {param_name: value})
        else:
            updates = {param_name: value}
        command = cmds["UpdateSubtitleClipCommand"].create(
            clip_id=clip_id,
            updates=updates,
            label=f"Edit Subtitle {param_name}",
        )
        self._execute_subtitle_command(command)
        # 同步 ProgramViewer 手柄（参数变化后位置可能变）
        self._refresh_subtitle_handle_selection(clip_id)

    def on_subtitle_params_changed(self, clip_id: str, track_id: str,
                                   updates: dict) -> None:
        """Handle a multi-field subtitle edit from the properties panel.

        SUB-P1-01: text debounce commit and RGBA color picker both submit one
        merged ``UpdateSubtitleClipCommand`` here, so a whole text edit or a
        single color pick produces exactly one undo entry instead of one per
        keystroke / per channel.
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        if not updates:
            return
        logger.debug(f"[P10.3] Subtitle params: {clip_id} fields={list(updates)}")
        panel = getattr(self, "edit_properties_panel", None)
        scope = getattr(panel, "_subtitle_scope", "legacy") if panel is not None else "legacy"
        timeline = self.edit_page._current_timeline
        clip = timeline.get_subtitle_clip_by_id(clip_id)
        if clip is None:
            return
        if scope == "global":
            global_updates = {
                key: value for key, value in updates.items()
                if key in self._subtitle_style_fields()
            }
            if global_updates:
                self.on_subtitle_track_params_changed(track_id, global_updates)
                remaining = {
                    key: value for key, value in updates.items()
                    if key not in global_updates
                }
                if not remaining:
                    return
                updates = remaining
        cmds = self._get_subtitle_command_classes()
        command_updates = dict(updates)
        if scope == "custom":
            style_updates = {
                key: value for key, value in updates.items()
                if key in self._subtitle_style_fields()
            }
            if style_updates:
                command_updates = self._custom_subtitle_style_updates(
                    timeline, clip, style_updates
                ) | {key: value for key, value in updates.items()
                     if key not in style_updates}
        command = cmds["UpdateSubtitleClipCommand"].create(
            clip_id=clip_id,
            updates=command_updates,
            label="Edit Subtitle",
        )
        self._execute_subtitle_command(command)
        self._refresh_subtitle_handle_selection(clip_id)

    @staticmethod
    def _subtitle_style_fields() -> frozenset[str]:
        from src.features.edit.core.captions.model import STYLE_FIELDS
        return frozenset(STYLE_FIELDS)

    def _custom_subtitle_style_updates(self, timeline, clip, updates: dict) -> dict:
        """Freeze the cue's effective style, then apply the requested fields."""
        from src.features.edit.core.captions.model import CaptionStyle
        resolved = self._resolve_subtitle_style(timeline, clip) or {}
        values = {
            key: resolved.get(key, getattr(clip, key))
            for key in self._subtitle_style_fields()
            if hasattr(clip, key)
        }
        values.update(updates)
        override = CaptionStyle(**values)
        return {
            **updates,
            "style_override": override,
        }

    def on_subtitle_track_param_changed(self, track_id: str,
                                        param_name: str, value: object) -> None:
        """Apply one global subtitle style parameter to the whole track."""
        self.on_subtitle_track_params_changed(track_id, {param_name: value})

    def on_subtitle_track_params_changed(self, track_id: str,
                                         updates: dict) -> None:
        """Update the track default style; inherited cues follow automatically."""
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        from src.features.edit.core.captions.commands import UpdateSubtitleTrackStyleCommand
        from src.features.edit.core.captions.model import CaptionStyle, EMPTY_STYLE

        timeline = self.edit_page._current_timeline
        track = timeline.get_track_by_id(track_id)
        if track is None or track.track_type != "subtitle":
            return
        style_updates = {
            key: value for key, value in updates.items()
            if key in self._subtitle_style_fields()
        }
        if not style_updates:
            return
        current = (
            track.caption_metadata.default_style
            if track.caption_metadata is not None
            else EMPTY_STYLE
        )
        new_style = CaptionStyle(**style_updates).merge_over(current)
        self._execute_subtitle_command(UpdateSubtitleTrackStyleCommand.create(
            track_id, new_style, previous_style=current,
        ))

    def on_subtitle_scope_changed(self, clip_id: str, track_id: str,
                                  scope: str) -> None:
        """Toggle a cue between inherited/global and frozen/custom style."""
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        from src.features.edit.core.captions.model import (
            CaptionStyle, EMPTY_STYLE, resolve_style, style_to_legacy_fields,
        )

        timeline = self.edit_page._current_timeline
        clip = timeline.get_subtitle_clip_by_id(clip_id)
        track = timeline.get_track_by_id(track_id)
        if clip is None or track is None or track.track_type != "subtitle":
            return
        resolved = self._resolve_subtitle_style(timeline, clip) or {}
        effective = CaptionStyle(**{
            key: resolved.get(key, getattr(clip, key))
            for key in self._subtitle_style_fields()
            if hasattr(clip, key)
        })
        update_clip = self._get_subtitle_command_classes()["UpdateSubtitleClipCommand"]
        if scope == "custom":
            updates = {"style_override": effective}
            updates.update(style_to_legacy_fields(effective))
            self._execute_subtitle_command(update_clip.create(
                clip_id, updates, label="Make Subtitle Custom"
            ))
            return

        current_default = (
            track.caption_metadata.default_style
            if track.caption_metadata is not None
            else EMPTY_STYLE
        )
        # Returning to global mode must only affect this cue. Keep the
        # existing track default untouched, then dual-write its resolved
        # values into the legacy flat fields so the cleared override cannot
        # accidentally keep the former custom appearance.
        global_style = CaptionStyle(**resolve_style(current_default, EMPTY_STYLE))
        clear_override = update_clip.create(
            clip_id,
            {"style_override": None, **style_to_legacy_fields(global_style)},
            label="Use Global Subtitle Style",
        )
        self._execute_subtitle_command(clear_override)

    # ==================== SUB-P2-03: Caption Workspace handlers ====================

    def _on_caption_command_requested(self, command) -> None:
        """Execute a caption command emitted by the Caption Workspace.

        Split/Merge/UpdateCaptionMetadata/UpdateCaptionStyleOverride all follow
        the unified ``(new_timeline, updated_cmd)`` contract, so they flow
        straight through the single subtitle commit path (SUB-P0-01). The
        resulting ``timeline_changed`` refreshes the cue table via
        ``_on_edit_timeline_changed``.
        """
        self._execute_subtitle_command(command)

    def _on_caption_find_requested(self, needle: str, replacement: str,
                                   scope: str) -> None:
        """Replace All across captions, as requested by the workspace.

        Builds one :class:`UpdateSubtitleClipCommand` per matched cue (its text
        with every needle occurrence replaced) and submits them as a single
        MacroCommand so the whole replace-all is one undo entry. Empty needle
        or no active timeline is a no-op.
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        if not needle:
            return
        timeline = self.edit_page._current_timeline
        try:
            from src.features.edit.ui.caption_workspace import (
                find_in_clips, build_replace_all_text,
            )
        except Exception:
            logger.exception("Failed to import caption workspace find helpers")
            return
        # Gather every cue clip (mirrors the workspace model's all_clips).
        try:
            clips = [c for tr in timeline.subtitle_tracks for c in tr.subtitle_clips]
        except Exception:
            logger.exception("Failed to collect subtitle clips for replace-all")
            return
        matches = find_in_clips(clips, needle)
        if not matches:
            if hasattr(self, 'status_bar') and self.status_bar is not None:
                self.status_bar.showMessage(
                    f"No matches for '{needle}' in captions", 3000
                )
            return
        # Group original-offset matches by cue, then apply each group from the
        # end so longer/shorter replacements cannot shift remaining offsets.
        cmds = self._get_subtitle_command_classes()
        update_cls = cmds["UpdateSubtitleClipCommand"]
        matches_by_id: dict[str, list[Any]] = {}
        for match in matches:
            matches_by_id.setdefault(match.clip_id, []).append(match)
        new_text_by_id: dict[str, str] = {}
        sub_commands = []
        for clip_id, clip_matches in matches_by_id.items():
            clip = timeline.get_subtitle_clip_by_id(clip_id) if hasattr(
                timeline, "get_subtitle_clip_by_id") else None
            if clip is None:
                continue
            new_text_by_id[clip_id] = build_replace_all_text(
                clip.text or "", clip_matches, replacement,
            )
        for clip_id, new_text in new_text_by_id.items():
            sub_commands.append(update_cls.create(
                clip_id=clip_id,
                updates={"text": new_text},
                label="Replace All in Captions",
            ))
        if not sub_commands:
            return
        tview = getattr(self.timeline_panel, 'timeline_view', None) \
            if hasattr(self, 'timeline_panel') else None
        if tview is None:
            logger.warning("Replace-all: no timeline view to commit through")
            return
        self._execute_batch_macro(tview, sub_commands, "Replace All in Captions")

    # ==================== P10.3: Subtitle Handle Interaction ====================

    @staticmethod
    def _resolve_subtitle_style(timeline, clip):
        """Resolve a SubtitleClip's layered style (track-default ⊕ cue-override).

        Returns the flattened dict from captions.model.resolve_style, or ``None``
        on any error (caller falls back to the legacy flat-field render path).
        Used to keep preview handles + overlay pixel-consistent with the
        layered caption style (SUB-P2-01/06).
        """
        try:
            from src.features.edit.core.captions.model import (
                resolve_style, EMPTY_STYLE, clip_style_base,
            )
            track = None
            getter = getattr(timeline, "get_track_by_id", None)
            if callable(getter):
                track = getter(clip.track_id)
            track_default = (
                track.caption_metadata.default_style
                if track is not None and getattr(track, "caption_metadata", None) is not None
                and getattr(track.caption_metadata, "default_style", None) is not None
                else EMPTY_STYLE
            )
            return resolve_style(track_default, getattr(clip, "style_override", None),
                                 base=clip_style_base(clip))
        except Exception:
            return None

    def _clear_subtitle_viewer_selection(self) -> None:
        """清除 ProgramViewer 上选中的字幕（隐藏变换手柄）。

        在选择非字幕元素（视频 clip / 轨道 / 空白）时调用，
        避免字幕手柄残留显示。
        """
        try:
            viewer = getattr(self.edit_page, '_program_viewer', None)
            if viewer is not None:
                viewer.set_selected_subtitle_clip(None)
        except Exception:
            logger.exception("Failed to clear subtitle viewer selection")

    def _refresh_subtitle_handle_selection(self, clip_id: str) -> None:
        """参数变化后重新设置 viewer 选中的 clip（让手柄跟随新参数）。"""
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        timeline = self.edit_page._current_timeline
        new_clip = timeline.get_subtitle_clip_by_id(clip_id)
        if new_clip is None:
            return
        viewer = getattr(self.edit_page, '_program_viewer', None)
        if viewer is not None:
            try:
                viewer.set_selected_subtitle_clip(
                    new_clip, style=self._resolve_subtitle_style(timeline, new_clip)
                )
            except Exception:
                logger.exception("Failed to refresh subtitle handle selection")

    def on_subtitle_interaction_started(self, clip_id: str) -> None:
        """手柄 drag 开始：开启一个 SubtitleTransformSession 记录 before 状态。

        SUB-P0-02: 拖拽期间只维护 transient preview（不入命令栈），释放时
        才提交一个 before→after 命令，使 Undo 能回到拖拽前。
        """
        logger.debug(f"[P10.3] Subtitle handle drag start: {clip_id}")
        self._subtitle_transform_session = self._begin_subtitle_transform_session(clip_id)

    def on_subtitle_interaction_updated(self, clip_id: str, delta: dict) -> None:
        """手柄 drag 进行中。delta 是参数变化字典。

        SUB-P1-05: 实时把 delta apply 到 timeline（保证下一次 delta 基于最新
        状态），但把昂贵的 overlay 重建 + viewer 重新注入合并到一个 ~16ms
        (60Hz) 的 single-shot QTimer —— 高 DPI 鼠标可达 1000+ 事件/秒，逐事件
        重建 overlay 会与播放/seek 竞争 UI 线程。timer 回调总是处理最新 clip。
        这些变更不入命令栈；最终的 before→after 命令在 pointer-up 提交。
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        logger.debug(f"[P10.3] Subtitle handle drag update: {clip_id} {delta}")
        if not delta:
            return
        cmds = self._get_subtitle_command_classes()
        # 直接 apply（不入栈）—— 必须同步，下一个 delta 依赖最新 timeline。
        try:
            timeline = self.edit_page._current_timeline
            session = getattr(self, '_subtitle_transform_session', None)
            if session is not None and session.scope == "global":
                from src.features.edit.core.captions.model import CaptionStyle, EMPTY_STYLE
                track = timeline.get_track_by_id(session.track_id)
                if track is None or track.track_type != "subtitle" or track.is_locked:
                    return
                changed_fields = set(delta)
                locked_affected = any(
                    cue.is_edit_locked()
                    and (
                        cue.style_override is None
                        or any(
                            getattr(cue.style_override, field_name) is None
                            for field_name in changed_fields
                        )
                    )
                    for cue in track.subtitle_clips
                )
                if locked_affected:
                    # Do not let a transient global drag alter the in-memory
                    # timeline only to be rejected at pointer-up.
                    return
                current = (
                    track.caption_metadata.default_style
                    if track.caption_metadata is not None
                    else EMPTY_STYLE
                )
                new_style = CaptionStyle(**dict(delta)).merge_over(current)
                metadata = track.caption_metadata
                if metadata is None:
                    from src.features.edit.core.captions.model import CaptionTrackMetadata
                    metadata = CaptionTrackMetadata()
                new_track = track.with_caption_metadata(
                    metadata.with_default_style(new_style)
                )
                result = timeline.with_track_updated(new_track)
            else:
                clip = timeline.get_subtitle_clip_by_id(clip_id)
                live_updates = dict(delta)
                if session is not None and session.scope == "custom" and clip is not None:
                    live_updates = self._custom_subtitle_style_updates(
                        timeline, clip, dict(delta)
                    )
                command = cmds["UpdateSubtitleClipCommand"].create(
                    clip_id=clip_id,
                    updates=live_updates,
                    label="Drag Subtitle",
                )
                result = command.apply(timeline)
            new_timeline = result if not isinstance(result, tuple) else result[0]
            self.edit_page._current_timeline = new_timeline
            if session is not None and session.clip_id == clip_id:
                session.accumulate(delta)
        except Exception:
            logger.exception("Subtitle handle drag update failed")
        # 记录待刷新的 clip，合并 overlay/repaint 到 60Hz。
        self._subtitle_drag_dirty_clip_id = clip_id
        self._schedule_subtitle_drag_preview_flush()

    def _schedule_subtitle_drag_preview_flush(self, delay_ms: int = 16) -> None:
        """SUB-P1-05: coalesce drag preview overlay refreshes to ~60Hz.

        连续 mouse-move 只会在最后一次到达后约 delay_ms 触发一次 overlay 重建
        + viewer 重新注入。回调使用最新的 _subtitle_drag_dirty_clip_id，避免
        闭包捕获过期值。镜像 _schedule_audio_mixer_sync 的 single-shot restart
        模式。
        """
        if not hasattr(self, '_subtitle_drag_preview_timer') \
                or self._subtitle_drag_preview_timer is None:
            self._subtitle_drag_preview_timer = QTimer()
            self._subtitle_drag_preview_timer.setSingleShot(True)
            self._subtitle_drag_preview_timer.timeout.connect(
                self._flush_subtitle_drag_preview
            )
        self._subtitle_drag_preview_timer.start(delay_ms)

    def _cancel_subtitle_drag_preview_flush(self) -> None:
        """Stop the coalesced drag-preview timer and clear the dirty clip id.

        Called on pointer-up so the synchronous commit (which refreshes the
        overlay) is the single source of truth for the final state.
        """
        timer = getattr(self, '_subtitle_drag_preview_timer', None)
        if timer is not None:
            timer.stop()
        self._subtitle_drag_dirty_clip_id = None

    def _flush_subtitle_drag_preview(self) -> None:
        """Apply the latest coalesced drag overlay refresh."""
        clip_id = getattr(self, '_subtitle_drag_dirty_clip_id', None)
        if clip_id is None:
            return
        self._subtitle_drag_dirty_clip_id = None
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        try:
            timeline = self.edit_page._current_timeline
            new_clip = timeline.get_subtitle_clip_by_id(clip_id)
            viewer = getattr(self.edit_page, '_program_viewer', None)
            if viewer is not None and new_clip is not None:
                viewer.set_selected_subtitle_clip(
                    new_clip, style=self._resolve_subtitle_style(timeline, new_clip)
                )
            self._sync_subtitle_transform_properties(new_clip)
            if hasattr(self.edit_page, '_update_subtitle_overlays'):
                frame = int(getattr(self.edit_page, '_current_frame', 0) or 0)
                self.edit_page._update_subtitle_overlays(frame)
        except Exception:
            logger.exception("Subtitle drag preview flush failed")

    def on_subtitle_interaction_ended(self, clip_id: str, final_delta: dict,
                                      original: dict) -> None:
        """手柄 drag 结束：提交一个 before→after 命令（支持 undo/redo）。

        SUB-P0-02: ``original`` 是 handle controller 在 drag 开始时快照的
        pre-drag 字段值。用它作为命令的显式 previous snapshot，使 Undo 恢复
        到拖拽前；而不是从已被 transient 预览改写的 timeline 重新推导（那样
        undo 会变成 no-op）。
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            self._subtitle_transform_session = None
            self._cancel_subtitle_drag_preview_flush()
            return
        logger.debug(f"[P10.3] Subtitle handle drag end: {clip_id} delta={final_delta}")
        session = getattr(self, '_subtitle_transform_session', None)
        self._subtitle_transform_session = None
        # Cancel any pending coalesced preview flush; the commit below refreshes
        # the overlay synchronously via the unified commit path.
        self._cancel_subtitle_drag_preview_flush()
        # before 快照优先用 session 捕获的完整字段，回退到 controller 传入的 original
        before = original
        if session is not None and session.clip_id == clip_id and session.before:
            before = session.before
        # The handle controller's selected clip is refreshed by the coalesced
        # preview timer.  A fast move -> release can therefore report an empty
        # or stale final_delta before that timer fires.  The transform session
        # and its transient timeline are updated synchronously for every
        # accepted mouse move, so the transient clip is authoritative at release.
        updates = dict(final_delta)
        if session is not None and session.scope == "global":
            track = self.edit_page._current_timeline.get_track_by_id(session.track_id)
            current_style = (
                track.caption_metadata.default_style
                if track is not None and track.caption_metadata is not None
                else None
            )
            if current_style is not None:
                updates = {
                    key: getattr(current_style, key)
                    for key in _SUBTITLE_TRANSFORM_FIELDS
                    if hasattr(current_style, key)
                    and getattr(current_style, key) is not None
                    and before.get(key) != getattr(current_style, key)
                }
            else:
                updates = session.accumulated
        elif session is not None and session.clip_id == clip_id:
            # The transient timeline is updated synchronously on every accepted
            # move and is therefore the final authority.  Reading the clip also
            # remains correct if transform helpers later emit sparse deltas or
            # the model normalizes/clamps a value during apply.
            transient_clip = self.edit_page._current_timeline.get_subtitle_clip_by_id(
                clip_id
            )
            if transient_clip is not None:
                updates = {
                    key: getattr(transient_clip, key)
                    for key in _SUBTITLE_TRANSFORM_FIELDS
                    if hasattr(transient_clip, key)
                    and before.get(key) != getattr(transient_clip, key)
                }
            else:
                updates = session.accumulated
        updates = {
            key: value for key, value in updates.items()
            if key in _SUBTITLE_TRANSFORM_FIELDS and before.get(key) != value
        }
        if not updates:
            return
        if session is not None and session.scope == "global":
            from src.features.edit.core.captions.commands import UpdateSubtitleTrackStyleCommand
            from src.features.edit.core.captions.model import CaptionStyle, EMPTY_STYLE
            track = self.edit_page._current_timeline.get_track_by_id(session.track_id)
            if track is None or track.track_type != "subtitle":
                return
            current = (
                track.caption_metadata.default_style
                if track.caption_metadata is not None
                else EMPTY_STYLE
            )
            final_style = CaptionStyle(**updates).merge_over(current)
            previous_style = session.before_track_style or EMPTY_STYLE
            command = UpdateSubtitleTrackStyleCommand.create(
                session.track_id, final_style,
                previous_style=previous_style,
                label="Transform Global Subtitles",
            )
            try:
                self._execute_subtitle_command(command)
                timeline = self.edit_page._current_timeline
                self._sync_subtitle_transform_properties(
                    timeline.get_subtitle_clip_by_id(clip_id)
                )
            except Exception:
                logger.exception("Global subtitle transform commit failed")
            return
        cmds = self._get_subtitle_command_classes()
        command_updates = updates
        if session is not None and session.scope == "custom":
            current_clip = self.edit_page._current_timeline.get_subtitle_clip_by_id(clip_id)
            if current_clip is not None:
                command_updates = self._custom_subtitle_style_updates(
                    self.edit_page._current_timeline, current_clip, updates
                )
        command = cmds["UpdateSubtitleClipCommand"].create_with_previous(
            clip_id=clip_id,
            updates=command_updates,
            previous=dict(before),
            label="Transform Subtitle",
        )
        try:
            self._execute_subtitle_command(command)
            timeline = self.edit_page._current_timeline
            self._sync_subtitle_transform_properties(
                timeline.get_subtitle_clip_by_id(clip_id)
            )
        except Exception:
            logger.exception("Subtitle handle drag end command failed")

    def _sync_subtitle_transform_properties(self, clip: object) -> None:
        """Push a handle-drag transform into the existing properties widgets."""
        panel = getattr(self, "edit_properties_panel", None)
        updater = getattr(panel, "update_subtitle_transform_values", None)
        if callable(updater):
            # Global transforms live on track metadata, so the selected cue's
            # flat legacy fields can be stale even though the viewer is right.
            # Feed the properties panel the same resolved style as preview.
            display_clip = clip
            timeline = getattr(getattr(self, "edit_page", None),
                               "_current_timeline", None)
            if timeline is not None and clip is not None:
                resolved = self._resolve_subtitle_style(timeline, clip)
                if resolved:
                    try:
                        display_clip = replace(
                            clip,
                            **{
                                key: value for key, value in resolved.items()
                                if key in _SUBTITLE_TRANSFORM_FIELDS
                            },
                        )
                    except (TypeError, ValueError):
                        display_clip = clip
            updater(display_clip)

    def _begin_subtitle_transform_session(self, clip_id: str):
        """Capture the immutable pre-drag state of a subtitle clip.

        Returns a SubtitleTransformSession (or None if the clip cannot be
        resolved), recording the transform-relevant fields so the release-time
        command carries a correct undo snapshot.
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return None
        timeline = self.edit_page._current_timeline
        clip = timeline.get_subtitle_clip_by_id(clip_id)
        if clip is None:
            return None
        panel = getattr(self, "edit_properties_panel", None)
        scope = getattr(panel, "_subtitle_scope", "legacy") if panel is not None else "legacy"
        track = timeline.get_track_by_id(clip.track_id)
        before_track_style = None
        if track is not None and track.caption_metadata is not None:
            before_track_style = track.caption_metadata.default_style
        return SubtitleTransformSession.begin(
            clip, self._resolve_subtitle_style(timeline, clip),
            scope=scope, before_track_style=before_track_style,
        )

    # ================================================================
    # Clip Transform on-canvas handle interaction
    # ================================================================

    def on_transform_tool_toggled(self, active: bool) -> None:
        """Transform tool button toggled in the viewer toolbar.

        Syncs the toolbar state to the viewer and, when turning on, pushes the
        currently selected clip (if any) so handles appear immediately.
        """
        viewer = getattr(self.edit_page, '_program_viewer', None) if hasattr(self, 'edit_page') else None
        if viewer is None:
            return
        # Keep the toolbar button + viewer flag in lockstep (the toolbar may
        # be the originator; the viewer also has the flag, this is idempotent).
        if active:
            self._push_selected_clip_to_transform_viewer()

    def _push_selected_clip_to_transform_viewer(self) -> None:
        """Feed the currently selected clip's transform + canvas size to the
        viewer so the Transform tool can show handles."""
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        viewer = getattr(self.edit_page, '_program_viewer', None)
        if viewer is None:
            return
        # edit_properties_panel stores the selection as a list (_current_clips),
        # not a single id. Read the first selected clip's id (single-selection
        # is the common case for transform editing).
        clips = getattr(self.edit_properties_panel, '_current_clips', None) or [] \
            if hasattr(self, 'edit_properties_panel') else []
        clip_id = clips[0].clip_id if clips else None
        timeline = self.edit_page._current_timeline
        # Canvas = sequence resolution (fallback 1920×1080).
        canvas_w, canvas_h = 1920, 1080
        seq = getattr(timeline, 'sequence', None) or timeline
        try:
            if getattr(seq, 'width', None):
                canvas_w = int(seq.width)
            if getattr(seq, 'height', None):
                canvas_h = int(seq.height)
        except Exception:
            pass
        if clip_id is None:
            viewer.set_selected_clip_transform(None, None, canvas_w, canvas_h)
            return
        clip = timeline.get_clip_by_id(clip_id) if hasattr(timeline, 'get_clip_by_id') else None
        if clip is None:
            viewer.set_selected_clip_transform(None, None, canvas_w, canvas_h)
            return
        viewer.set_selected_clip_transform(clip.clip_id, clip.transform, canvas_w, canvas_h)

    def _on_edit_playhead_changed(self, frame: int) -> None:
        """Keep Transform handles scoped to the selected clip under the playhead.

        Selection is intentionally preserved while scrubbing, but a selected
        clip must not leave stale handles visible after the playhead moves to a
        different clip.  Re-pushing the transform when the playhead returns
        also restores the handles without requiring the user to reselect the
        clip or toggle the tool.
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        viewer = getattr(self.edit_page, '_program_viewer', None)
        if viewer is None or not viewer.is_transform_tool_active():
            return

        timeline = self.edit_page._current_timeline
        clips = (
            getattr(self.edit_properties_panel, '_current_clips', None) or []
            if hasattr(self, 'edit_properties_panel') else []
        )
        selected_clip = clips[0] if clips else None

        if selected_clip is None:
            self._push_selected_clip_to_transform_viewer()
            return

        selected_clip = (
            timeline.get_clip_by_id(selected_clip.clip_id)
            if hasattr(timeline, 'get_clip_by_id') else None
        )
        if selected_clip is None:
            self._push_selected_clip_to_transform_viewer()
            return

        current_frame = int(frame)
        clip_start = int(selected_clip.timeline_start)
        clip_end = int(selected_clip.timeline_end)
        if clip_start <= current_frame < clip_end:
            self._push_selected_clip_to_transform_viewer()
            return

        # Clear only the viewer binding.  Keep the properties selection intact
        # so scrubbing back onto the clip restores the same transform target.
        canvas_w, canvas_h = 1920, 1080
        seq = getattr(timeline, 'sequence', None) or timeline
        try:
            if getattr(seq, 'width', None):
                canvas_w = int(seq.width)
            if getattr(seq, 'height', None):
                canvas_h = int(seq.height)
        except Exception:
            pass
        viewer.set_selected_clip_transform(None, None, canvas_w, canvas_h)

    def on_clip_transform_drag_updated(self, clip_id: str, new_transform) -> None:
        """Clip transform handle drag in progress.

        Applies the new transform live (without entering the undo stack — the
        drag fires ~60×/s). The final value is committed to undo on drag end
        (see ``on_clip_transform_drag_ended``). Mirrors the subtitle drag
        pattern: direct apply + viewer/panel refresh.
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id) if hasattr(timeline, 'get_clip_by_id') else None
        if clip is None:
            return
        _cmds = get_command_classes()
        if _cmds is None:
            return
        UpdateClipParamCommand = _cmds["UpdateClipParamCommand"]
        command = UpdateClipParamCommand(
            command_id=str(uuid.uuid4()),
            label="Drag Transform",
            track_id=clip.track_id,
            clip_id=clip_id,
            param_type="transform",
            new_value=new_transform,
            old_value=clip.transform,
        )
        try:
            # Direct apply (no stack push) — frequent updates during drag.
            result = command.apply(timeline)
            new_timeline = result if not isinstance(result, tuple) else result[0]
            self.edit_page._current_timeline = new_timeline
            # Keep handle positions in sync with the just-applied transform.
            viewer = getattr(self.edit_page, '_program_viewer', None)
            if viewer is not None:
                viewer.update_clip_transform(new_transform)
            # CRITICAL: do NOT call _sync_timeline_to_views / set_sequence here.
            # set_sequence bumps the generation id (transform is part of the
            # render signature), which discards every in-flight render at 60Hz
            # and freezes the preview (the still-rendering frame is dropped as
            # stale before it can display). Instead, hot-swap the sequence in
            # place without bumping generation, then request a re-render.
            if self.timeline_preview_renderer is not None:
                silent = getattr(self.timeline_preview_renderer,
                                 'update_sequence_silent', None)
                if callable(silent):
                    silent(new_timeline)
                else:
                    # Fallback for older renderers without the silent path.
                    self.timeline_preview_renderer.set_sequence(new_timeline)
            self._request_program_viewer_update()
        except Exception:
            logger.exception("Clip transform drag update failed")

    def on_clip_transform_drag_ended(self, clip_id: str, final_transform,
                                     original_transform) -> None:
        """Clip transform handle drag ended.

        Commits the final transform via the command stack so the whole drag is
        a single undoable step. The drag-update path applied transforms
        directly (off-stack); this push establishes the undo record using the
        drag's net result (final vs original).
        """
        if not hasattr(self, 'edit_page') or not self.edit_page._current_timeline:
            return
        timeline = self.edit_page._current_timeline
        clip = timeline.get_clip_by_id(clip_id) if hasattr(timeline, 'get_clip_by_id') else None
        if clip is None:
            return
        _cmds = get_command_classes()
        if _cmds is None:
            return
        UpdateClipParamCommand = _cmds["UpdateClipParamCommand"]
        # Push an undoable command capturing the net change (original → final).
        command = UpdateClipParamCommand(
            command_id=str(uuid.uuid4()),
            label="Transform Clip",
            track_id=clip.track_id,
            clip_id=clip_id,
            param_type="transform",
            new_value=final_transform,
            old_value=original_transform,
        )
        try:
            # AUD-2703: EditPage 没有 ``_command_stack``（全库无赋值点），
            # 旧 ``hasattr`` 分支恒为假且无 else——拖拽结束的提交命令被静默
            # 丢弃：变换不进 undo 栈，``TimelineView._timeline`` 与
            # ``_current_timeline_sequences``（保存/自动保存取值源）仍是
            # 拖拽前状态，保存工程即丢失编辑。统一走
            # ``TimelineView.execute_command``（命令栈/增量视图更新/项目
            # 缓存/自动保存的唯一入口，经 ``timeline_changed`` 同步
            # ``edit_page._current_timeline`` 与序列缓存）；无 TimelineView
            # 时按 ``_on_edit_clip_param_changed`` 的 else 模式直接 apply
            # 并同步视图。
            view_getter = getattr(getattr(self, "timeline_panel", None),
                                  "get_timeline_view", None)
            view = view_getter() if callable(view_getter) else None
            new_timeline = None
            if view is not None and hasattr(view, "execute_command"):
                view.execute_command(command)
                new_timeline = getattr(view, '_timeline', None)
            else:
                result = command.apply(timeline)
                new_timeline = (result if not isinstance(result, tuple)
                                else result[0])
                self.edit_page._current_timeline = new_timeline
                self._sync_timeline_to_views(new_timeline)
            # Refresh the properties panel so position/scale/rotation numbers
            # reflect the dragged values. Full reload is fine here — drag end
            # is a one-shot, low-frequency event.
            refreshed_clip = ((new_timeline or self.edit_page._current_timeline)
                              .get_clip_by_id(clip_id))
            if refreshed_clip is not None:
                self._show_clip_in_properties(refreshed_clip)
        except Exception:
            logger.exception("Clip transform drag end command failed")

    # ------------------------------------------------------------------
    # T02/G1a: 只读洞察工具宿主入口（菜单 edit.insights.open 的回调）。
    # 边界纪律：本段只做只读读取与既有路径导航——不造空序列、不新增
    # 导航旁路、不注册全局快捷键；工程/目标变化一律结构化拒绝。
    # ------------------------------------------------------------------

    def on_insights_open(self) -> None:
        """打开（或激活）只读洞察窗口；宿主能力经注入式 adapter 进入。"""
        from src.features.edit.insights.factory import open_insights_window
        from src.features.edit.insights.host_adapter import InjectedHostAdapter

        adapter = InjectedHostAdapter(
            capture_state_fn=self._insights_capture_state,
            navigate_fn=self._insights_navigate,
            thumbnail_fn=None,  # G1a 未接缩略图：联系表占位降级（T13 后接）
        )
        open_insights_window(adapter, parent=self)

    def _insights_capture_state(self):
        """两层状态只读读取（与 get_project_sequences 同纪律）；无工程=None。"""
        project = getattr(self, "_current_edit_project", None)
        sequences = getattr(self, "_current_timeline_sequences", None)
        if project is None or sequences is None:
            return None
        from src.features.edit.ai_editing.edit_project_service import (
            ProjectSequences,
        )
        return ProjectSequences(project=project, sequences=dict(sequences))

    def _insights_navigate(self, request):
        """洞察结果定位：校验身份后走既有序列切换 + 播放头路径。"""
        from src.features.edit.insights.host_protocol import NavigateResult

        project = getattr(self, "_current_edit_project", None)
        if project is None:
            return NavigateResult(
                ok=False, status="rejected", reason="no_project")
        if request.project_id != project.project_id:
            return NavigateResult(
                ok=False, status="rejected", reason="project_changed")
        sequences = getattr(self, "_current_timeline_sequences", None) or {}
        sequence = sequences.get(request.sequence_id)
        if sequence is None:
            # 含未物化 ref：明确拒绝（不建空序列——那是用户切换路径的职责）
            return NavigateResult(
                ok=False, status="rejected", reason="target_missing")
        if (request.expected_sequence_revision is not None
                and sequence.revision != request.expected_sequence_revision):
            return NavigateResult(
                ok=False, status="rejected", reason="sequence_changed")
        frame = max(0, int(request.target_frame))
        # 既有路径：timeline_switched 的完整切换逻辑（active_timeline 高亮/
        # 媒体池同步/面板加载/Deliver 绑定同步——active_timeline_id 属允许
        # 变化的导航状态，编辑 revision/Undo 不动）。
        self._on_timeline_switched(request.sequence_id)
        edit_page = getattr(self, "edit_page", None)
        if edit_page is not None and hasattr(edit_page, "seek_to_frame"):
            edit_page.seek_to_frame(frame)
        return NavigateResult(
            ok=True, status="navigated",
            actual_sequence_id=request.sequence_id,
            actual_frame=frame,
        )
