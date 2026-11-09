"""
Timeline View Module - QGraphicsView for Timeline Visualization (P6).

This module provides the main timeline view widget using QGraphicsView/QGraphicsScene.
Manages the visual representation of timeline elements and editing tools.

Design Principles:
- QGraphicsView/QGraphicsScene for performance and built-in features
- Items are created via timeline_items.py
- Tool switching handled here (Select, Blade, Trim, Hand, Zoom)
- Snap toggle controlled here
- All state changes emit signals (no direct model mutation)
- Anti-recursion locks for signal connections

Features:
- Zoom with wheel or Z key
- Scroll/pan with Hand tool or middle mouse
- Snap toggle with N key
- Tool shortcuts: A (Select), B (Blade), T (Trim), H (Hand), Z (Zoom)
- Keyboard: Delete removes selected clips

Reference:
- docs/edit_page_timeline_upgrade_plan_2026-05-17.md P6
- .clinerules: UI thread safety, signal anti-recursion
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.features.edit.ui.trim_numeric_dialog import TrimMonitorDialog
    from src.features.edit.ui.program_viewer import ProgramViewerWidget

import logging
import json
import math
import os
import random
import uuid
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Optional, Dict, Any, List, NamedTuple, Callable, Mapping

from PyQt6.QtWidgets import (
    QGraphicsView,
    QGraphicsScene,
    QWidget,
    QScrollBar,
    QStyleOptionGraphicsItem,
    QRubberBand,
    QLabel,
)
from PyQt6.QtCore import (
    QEvent,
    Qt,
    pyqtSignal,
    pyqtSlot,
    QThread,
    QTimer,
    QPointF,
    QRectF,
    QRect,
    QPoint,
)
from PyQt6.QtCore import QObject
from PyQt6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QFont,
    QWheelEvent,
    QResizeEvent,
    QKeyEvent,
    QMouseEvent,
    QDragMoveEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QPaintEvent,
)
from PyQt6.QtWidgets import QApplication

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    TransitionInstance,
    TimelineMarker,
    TimelineClipboardPayload,
    SubtitleClip,
    resolve_timeline_in_out,
    effective_timeline_duration_frames,
    MEDIA_POOL_PREVIEW_ASSET_ID_KEY,
    MEDIA_POOL_PREVIEW_FRAME_KEY,
)
from src.features.edit.core.playback_debug import playback_trace
from src.features.edit.core.project import MediaAsset, get_media_type
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.source_drag_payload import (
    MIME_TYPE_SOURCE_CLIP,
    decode_source_drag_payload,
)
from src.features.edit.ui.timeline_edit_affordances import edit_cursor
from src.features.edit.core.render_cache_status import build_cache_indicator_segments
from src.features.edit.core.transition_catalog import TRANSITION_LABELS
from src.features.edit.core.audio_transition_catalog import is_audio_transition
from src.features.teaching_animation.core.motion_graphic_generator_registry import (
    MOTION_GRAPHIC_GENERATOR_MIME,
)
from src.shared.media.metadata import Rational


# ================================================================
# Phase 7.2: Incremental Update - Change Detection Types
# ================================================================

class TimelineChanges(NamedTuple):
    """
    Timeline changes detected for incremental update (Phase 7.2).

    Contains the changes needed to update the timeline view
    without rebuilding all items.

    Attributes:
        added_clips: New clips to create
        removed_clip_ids: Clip IDs to remove
        modified_clips: Clips with changed properties
        added_transitions: New transitions to create
        removed_transition_ids: Transition IDs to remove
        modified_transitions: Transitions with changed properties
        added_markers: New markers to create
        removed_marker_ids: Marker IDs to remove
        modified_markers: Markers with changed properties
        added_track_ids: Empty tracks added without clip changes
        removed_track_ids: Empty tracks removed without clip changes
        track_changes: Track header changes (mute/solo/lock/volume)
        added_subtitle_clips: New subtitle cues to create (SUB-P1-02)
        removed_subtitle_clip_ids: Subtitle cue IDs to remove (SUB-P1-02)
        modified_subtitle_clips: Subtitle cues with changed properties (SUB-P1-02)
        subtitle_track_changes: Subtitle-track item state changes (locked/visible)
        full_reload_needed: True if changes are too complex for incremental update
    """
    # BUG-07 fix: Use immutable tuple defaults instead of mutable list defaults
    added_clips: tuple[ClipInstance, ...] = ()
    removed_clip_ids: tuple[str, ...] = ()
    modified_clips: tuple[ClipInstance, ...] = ()
    added_transitions: tuple[TransitionInstance, ...] = ()
    removed_transition_ids: tuple[str, ...] = ()
    modified_transitions: tuple[TransitionInstance, ...] = ()
    added_markers: tuple[TimelineMarker, ...] = ()
    removed_marker_ids: tuple[str, ...] = ()
    modified_markers: tuple[TimelineMarker, ...] = ()
    added_track_ids: tuple[str, ...] = ()
    removed_track_ids: tuple[str, ...] = ()
    track_changes: Dict[str, Dict[str, Any]] = {}  # track_id -> {muted, solo, locked, volume}
    # SUB-P1-02: subtitle-specific incremental diff. Cue ids live in the same
    # _items_map namespace (UUIDs), but the diff is computed separately because
    # SubtitleClip is a different type with its own item class.
    added_subtitle_clips: tuple[SubtitleClip, ...] = ()
    removed_subtitle_clip_ids: tuple[str, ...] = ()
    modified_subtitle_clips: tuple[SubtitleClip, ...] = ()
    subtitle_track_changes: Dict[str, Dict[str, Any]] = {}
    full_reload_needed: bool = False


from src.features.edit.core.timeline_ops import (
    SnapManager,
    SnapPointType,
    TimelineOperationError,
)
from src.features.edit.core.selection import TimelineSelection, SelectionType
from src.features.edit.core.commands import (
    CommandStack,
    AddClipCommand,
    DeleteClipCommand,
    MoveClipCommand,
    MoveClipsOverwriteCommand,
    MoveClipToTrackCommand,  # P4: Cross-track move with auto-create track
    TrimClipCommand,
    RetimeMotionGraphicClipCommand,
    SplitClipCommand,
    BatchDeleteCommand,
    # Track commands
    SetTrackMuteCommand,
    SetTrackSoloCommand,
    SetTrackVolumeCommand,
    SetTrackLockCommand,
    AddTrackCommand,
    AddSubtitleClipCommand,
    DeleteTrackCommand,
    RenameTrackCommand,
    MoveTrackCommand,
    # Transition commands
    AddTransitionCommand,
    RemoveTransitionCommand,
    UpdateTransitionCommand,
    # Slip command (P4.4)
    SlipClipCommand,
    # Roll command (P4.3)
    SlideClipCommand,
    # Ripple trim command (P4.2)
    RippleTrimCommand,
    # Marker commands (Phase 5.1)
    AddMarkerCommand,
    DeleteMarkerCommand,
    MoveMarkerCommand,
    EditMarkerCommand,
    # Batch operations
    MacroCommand,
)
from src.features.edit.core.source_patch import (
    DEFAULT_SOURCE_PATCH,
    SourcePatchSpec,
    patch_for_asset,
)
from src.features.edit.ui.timeline_items import (
    ClipItem,
    TransitionItem,
    PlayheadItem,
    TrackHeaderItem,
    TimecodeRulerItem,
    TimelineRangeMaskItem,
    TimelineRangeItem,
    SnapIndicatorItem,
    EditTool,
    TRACK_HEADER_WIDTH,
    TRACK_HEIGHT,
    TRACK_GAP,
    RULER_HEIGHT,
    # Roll handle (P4.3)
    RollHandleItem,
    _trim_cursor,
    # Enhanced drag preview (参考 kdenlive dragProxy)
    DragPreviewItem,
    # Gap indicator (Phase 3.3)
    GapIndicatorItem,
    # Marker item (Phase 5.1)
    MarkerItem,
)
from src.features.edit.ui.subtitle_clip_item import SubtitleClipItem
from src.features.edit.ui.timeline_interaction import InteractionState, InteractionSession, ClipDragSession, MoveGroup, MoveGroupMember
from src.features.edit.ui.edit_style import EDIT_SIZES, get_color
from src.features.edit.ui.timeline_layout import (
    TimelineLayoutEngine,
    TimelineLayoutState,
    TimelineDropTarget,
    TrackInfo,
    SPLITTER_HEIGHT,
    DEFAULT_SUBTITLE_TRACK_HEIGHT,
    MIN_SUBTITLE_TRACK_HEIGHT,
    MAX_SUBTITLE_TRACK_HEIGHT,
)
from src.shared.i18n.i18n import I18N
from src.shared.media.thumbnail_provider import ThumbnailProvider
from src.shared.media.format_registry import is_media
from src.features.edit.core.captions.importer import (
    SUPPORTED_SUBTITLE_EXTENSIONS,
    parse_subtitle_file,
    parse_subtitle_file_language,
    partition_subtitle_cues,
)

logger = logging.getLogger(__name__)

# B5: 迁移的工具键 action → handler 名称（keyPressEvent 匹配层用）。
# 处理函数通过 getattr 延迟解析，避免模块级循环引用。
_TOOL_ACTION_HANDLERS = {
    "edit.timeline.tool.select": "set_tool_select",
    "edit.timeline.tool.trim": "set_tool_trim",
    "edit.timeline.tool.blade": "set_tool_blade",
    "edit.timeline.tool.ripple": "set_tool_ripple",
    "edit.timeline.tool.rate_stretch": "set_tool_rate_stretch",
    "edit.timeline.tool.roll": "set_tool_roll",
    "edit.timeline.tool.slip": "set_tool_slip",
    "edit.timeline.tool.slide": "set_tool_slide",
    "edit.timeline.tool.hand": "set_tool_hand",
    "edit.timeline.tool.zoom": "set_tool_zoom",
    "edit.timeline.tool.next_enable": "toggle_snap_action",
    "edit.timeline.tool.disable": "toggle_enabled_action",
    "edit.timeline.add_marker": "add_marker_action",
    "edit.timeline.add_chapter": "add_chapter_action",
    "edit.timeline.jump_prev_marker": "jump_prev_action",
    "edit.timeline.jump_next_marker": "jump_next_action",
    "edit.timeline.mark_in": "mark_in_action",
    "edit.timeline.mark_out": "mark_out_action",
}


def _exec_tool_action(view, handler_name: str) -> None:
    """把 action_id 分派到 TimelineView 上的既有处理方法。"""
    if handler_name == "set_tool_select":
        view.set_tool(EditTool.SELECT)
    elif handler_name == "set_tool_trim":
        view.set_tool(EditTool.TRIM)
    elif handler_name == "set_tool_blade":
        view.set_tool(EditTool.BLADE)
    elif handler_name == "set_tool_ripple":
        view.set_tool(EditTool.RIPPLE)
    elif handler_name == "set_tool_rate_stretch":
        view.set_tool(EditTool.RATE_STRETCH)
    elif handler_name == "set_tool_roll":
        view.set_tool(EditTool.ROLL)
    elif handler_name == "set_tool_slip":
        view.set_tool(EditTool.SLIP)
    elif handler_name == "set_tool_slide":
        view.set_tool(EditTool.SLIDE)
    elif handler_name == "set_tool_hand":
        view.set_tool(EditTool.HAND)
    elif handler_name == "set_tool_zoom":
        view.set_tool(EditTool.ZOOM)
    elif handler_name == "toggle_snap_action":
        view.set_snap_enabled(not view._snap_enabled)
    elif handler_name == "toggle_enabled_action":
        view._toggle_selected_clips_enabled()
    elif handler_name == "add_marker_action":
        view.add_marker_at_playhead()
    elif handler_name == "add_chapter_action":
        view.add_chapter_marker_at_playhead()
    elif handler_name == "jump_prev_action":
        view.jump_to_prev_marker()
    elif handler_name == "jump_next_action":
        view.jump_to_next_marker()
    elif handler_name == "mark_in_action":
        view._mark_in_at_playhead()
    elif handler_name == "mark_out_action":
        view._mark_out_at_playhead()


def _tool_action_match(event) -> Optional[tuple]:
    """返回 (action_id, handler_name) 或 None。"""
    from src.features.edit.ui.timeline_shortcuts import (
        DEFAULT_KEY_BY_ACTION,
        matches_key_event,
    )

    for action_id, handler in _TOOL_ACTION_HANDLERS.items():
        if matches_key_event(
                event, action_id,
                default_key=DEFAULT_KEY_BY_ACTION.get(action_id)):
            return (action_id, handler)
    return None


# ================================================================
# Constants
# ================================================================

# Low-saturation marker colors.  The generated UUID chooses a stable palette
# entry for each new marker, so colors vary without changing on redraw/undo.
MUTED_MARKER_COLORS: tuple[str, ...] = (
    "#c66e6e",  # muted red
    "#c38b5f",  # muted orange
    "#b8a85e",  # muted yellow
    "#79aa7f",  # muted green
    "#62a3a3",  # muted cyan
    "#718fbd",  # muted blue
    "#9278ab",  # muted purple
    "#b77b9a",  # muted pink
)

# C5: chapter markers use a distinct blue so they read as delivery chapters
# (standard markers rotate through the muted palette above).
CHAPTER_MARKER_COLOR: str = "#4a9edb"

# One pixel represents up to 100 frames at the overview limit.  At 25 fps a
# 1440 px timeline viewport can therefore show roughly 100 minutes, instead
# of the previous roughly 10 minutes at 0.1 px/frame.
MIN_ZOOM = 0.01  # Minimum pixels per frame
MAX_ZOOM = 50.0  # Maximum pixels per frame
DEFAULT_ZOOM = 5.0  # Default pixels per frame
ZOOM_STEP = 1.2  # Zoom multiplier per wheel step
# Small forgiving hit band for the playhead, used only when the pointer is
# over a gap.  Clip content keeps its normal, more precise hit testing.
PLAYHEAD_EMPTY_HIT_TOLERANCE = 5.0
TIMELINE_EXTENSION_SECONDS = 10  # Extend editable timeline in usable chunks
EMPTY_TIMELINE_PREVIEW_SECONDS = 10  # Preview span shown before a timeline exists
EMPTY_TIMELINE_FPS = 25.0
CONTENT_FIT_FILL_RATIO = 0.80  # Shift+Z leaves comfortable timeline margins

SNAP_THRESHOLD_FRAMES = 6  # Legacy: Snap distance threshold (frames) - kept for backward compat
SNAP_THRESHOLD_PIXELS = EDIT_SIZES.get("snap_threshold_px", 10)  # Pixel-based snap threshold (DaVinci-like)

# Navigation constants
FRAME_STEP = 1  # Single frame step for left/right arrows
MULTI_FRAME_STEP = 10  # Multi-frame step for Shift+left/right
PAGE_STEP_SECONDS = 1.0  # Page Up/Down step in seconds
PLAYHEAD_FOLLOW_ENABLED = False  # Playhead centering during playback (default off, avoid disrupting manual viewing)

BACKGROUND_COLOR = QColor(get_color("timeline_canvas_bg"))
GRID_COLOR = QColor(40, 40, 50)
TRACK_AREA_COLOR = QColor(get_color("timeline_canvas_bg"))
# V/A distinction comes from clips and headers, never from mismatched canvas
# blocks. The shared neutral canvas is lifted to match the Color page's panel
# hierarchy instead of reading as a near-black void.
TRACK_AREA_VIDEO_BG = QColor(get_color("timeline_canvas_bg"))
TRACK_AREA_AUDIO_BG = QColor(get_color("timeline_canvas_bg"))

# Drag and drop MIME type (same as in media_pool_panel.py)
MIME_TYPE_MEDIA_POOL_ASSETS = "application/x-topos-media-pool-assets"
MIME_TYPE_TRANSITION = "application/x-topos-transition"
# Default transition length in frames shared by the drag-drop path and the
# effects-panel double-click path (P1-06).
DEFAULT_TRANSITION_DURATION = 12
# Audio transitions share the same _drag_transition_type pipeline but carry this
# MIME so the drop handlers route to audio tracks instead of video tracks.
MIME_TYPE_AUDIO_TRANSITION = "application/x-topos-audio-transition"
MIME_TYPE_EFFECT = "application/x-topos-effect"  # Effect drag from EffectsPanel
MIME_TYPE_GENERATOR = "application/x-topos-generator"  # Generator (Adjustment/Solid) drag — creates a clip

# Insert preview colors
INSERT_PREVIEW_COLOR = QColor(100, 180, 255, 150)  # Semi-transparent blue
INSERT_PREVIEW_BORDER_COLOR = QColor(100, 180, 255)
INSERT_PREVIEW_BORDER_WIDTH = 2

# Transition preview colors
TRANSITION_PREVIEW_COLOR = QColor(180, 140, 80, 150)  # Semi-transparent orange/brown
TRANSITION_PREVIEW_BORDER_COLOR = QColor(140, 100, 60)


# ================================================================
# TimelineView
# ================================================================


class _TimelineTimecodeLabel(QLabel):
    """Fixed timecode label with deterministic opaque painting.

    Qt's stylesheet-backed ``QLabel`` can leave an opaque child widget black
    when ``WA_OpaquePaintEvent`` is enabled, depending on the platform style.
    The timeline header is performance-sensitive, so paint this tiny surface
    directly instead of relying on that backend-specific auto-fill behavior.
    """

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(get_color("ruler_bg")))
        painter.setPen(QPen(QColor(get_color("timecode_panel_text"))))
        painter.setFont(self.font())
        painter.drawText(self.rect(), self.alignment(), self.text())
        painter.setPen(QPen(QColor(get_color("timecode_panel_bottom_line")), 1))
        painter.drawLine(0, self.height() - 1, self.width() - 1, self.height() - 1)
        painter.end()


class _TimelineRubberBand(QRubberBand):
    """Neutral marquee drawn in the same coordinate space as mouse events."""

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(200, 208, 220, 24))
        painter.setPen(QColor(184, 189, 198, 200))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()


@dataclass(frozen=True)
class SelectionDiagnosticEntry:
    """Why one selected clip would carry other objects (P2-03)."""

    clip_id: str
    track_id: str
    track_kind: str
    track_locked: bool
    linked_clip_id: str | None = None
    linked_track_id: str | None = None
    moves_together: bool = False


@dataclass(frozen=True)
class SelectionDiagnostics:
    """Selection Inspector summary for the current selection (P2-03)."""

    entries: tuple[SelectionDiagnosticEntry, ...] = ()
    linked_selection_enabled: bool = True

    @property
    def linked_entries(self) -> tuple[SelectionDiagnosticEntry, ...]:
        return tuple(e for e in self.entries if e.linked_clip_id is not None)

    @property
    def locked_entries(self) -> tuple[SelectionDiagnosticEntry, ...]:
        return tuple(e for e in self.entries if e.track_locked)

    def to_dict(self) -> dict:
        return {
            "linked_selection_enabled": self.linked_selection_enabled,
            "entries": [
                {
                    "clip_id": e.clip_id,
                    "track_id": e.track_id,
                    "track_kind": e.track_kind,
                    "track_locked": e.track_locked,
                    "linked_clip_id": e.linked_clip_id,
                    "linked_track_id": e.linked_track_id,
                    "moves_together": e.moves_together,
                }
                for e in self.entries
            ],
        }


@dataclass(frozen=True)
class _LinkClipsAfterApplyCommand:
    """FE-B03：源 Overwrite/Append 后回填 A/V 双向链接。

    Overwrite/Append 命令内部生成视频片段 ID（无法预指定），宏内此命令
    在两者 apply 之后按 (asset, 起始帧, 轨道) 解析出视频片段并委托
    SetClipLinkedCommand 建立链接；解析失败（视频侧未落地）时为 no-op，
    不破坏宏的原子性。undo 依赖两片段自身命令的回滚（链接随片段消失）。
    """

    asset_id: str
    video_track_id: str
    video_start: int
    audio_clip_id: str
    audio_track_id: str
    command_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    label: str = "Link A/V after source edit"

    def apply(self, timeline):
        track = timeline.get_track_by_id(self.video_track_id)
        video = None
        if track is not None:
            for c in track.clips:
                if c.asset_id == self.asset_id and c.timeline_start == self.video_start:
                    video = c
                    break
        audio = timeline.get_clip_by_id(self.audio_clip_id)
        if audio is None:
            # R22：Overwrite 等命令内部生成音频片段 ID，预置 ID 解析不到
            # 时按 (asset, 起始帧, 轨道) 回退定位（与视频侧同规则）。
            audio_track = timeline.get_track_by_id(self.audio_track_id)
            if audio_track is not None:
                for c in audio_track.clips:
                    if (
                        c.asset_id == self.asset_id
                        and c.timeline_start == self.video_start
                    ):
                        audio = c
                        break
        if video is None or audio is None:
            return timeline
        from src.features.edit.core.commands import SetClipLinkedCommand

        cmd = SetClipLinkedCommand.create_link(
            clip_a_id=video.clip_id,
            clip_a_track_id=video.track_id,
            clip_b_id=audio.clip_id,
            clip_b_track_id=audio.track_id,
        )
        return cmd.apply(timeline)

    def undo(self, timeline):
        return timeline

    def to_dict(self):
        return {
            "command_id": self.command_id,
            "command_type": "_LinkClipsAfterApplyCommand",
            "asset_id": self.asset_id,
            "video_track_id": self.video_track_id,
            "video_start": self.video_start,
            "audio_clip_id": self.audio_clip_id,
            "audio_track_id": self.audio_track_id,
        }


class _NormalizeGainWorker(QObject):
    """D02/D03（第六轮审计）：片段响度归一化分析 worker。

    测量正确性（D02）：
    - 用生产 ``AudioDecoder``（PyAV）解码，保留素材真实声道布局与采样率，
      BS.1770/true-peak 按实际声道能量测量——此前 ffmpeg ``-ac 1`` 先混
      单声道，反相立体声被抵消成"静音"，反而建议 +47dB 增益。
    - 低于测量门限（接近静音）时拒绝正增益建议，静音 floor 不当作响度。

    响应性（D03）：解码与分析在 worker 线程执行；``cancel()`` 置位后逐块
    退出（每块 ~1s 音频），不阻塞 UI 事件循环。
    """

    finished = pyqtSignal(bool, str, object)  # ok, text, gain(None=不应用)

    # 静音测量门限（LUFS）：低于此值视为无可信响度，拒绝正增益建议
    SILENCE_FLOOR_LUFS = -60.0

    def __init__(
        self,
        asset_path,
        source_start_frame: int = 0,
        source_fps: float = 25.0,
        duration_frames: int = 0,
        start_seconds: float | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        super().__init__()
        self._path = asset_path
        self._source_start = max(0, int(source_start_frame))
        self._source_fps = float(source_fps) if source_fps > 0 else 25.0
        self._duration_frames = max(0, int(duration_frames))
        # 秒驱动模式（兼容入口）：直接用秒定位，帧换算由调用方完成
        self._start_seconds = start_seconds
        self._duration_seconds = duration_seconds
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self.analyze_sync(cancel_check=lambda: self._cancelled)
        except Exception as exc:  # noqa: BLE001 — 任何失败都以文本反馈
            result = (False, f"分析失败：{exc}", None)
        self.finished.emit(*result)

    def analyze_sync(self, cancel_check=None) -> tuple[bool, str, float | None]:
        """同步执行完整分析（worker 线程或兼容入口直调）。"""
        import numpy as np

        from src.features.edit.core.loudness_analyzer import (
            analyze_loudness,
            compute_normalize_gain_db,
            is_scipy_available_for_dsp,
        )

        samples, sample_rate = self._decode_source(cancel_check)
        if samples is None:
            return False, "分析已取消", None
        if samples.shape[-1] == 0:
            return False, "音频解码失败（无输出）", None

        if not is_scipy_available_for_dsp():
            return False, (
                "响度后端（scipy）不可用，无法按 LUFS 归一化"
                "（真峰值数据可信但目标响度不可信）"), None

        def _progress(_fraction: float) -> bool:
            if cancel_check is not None and cancel_check():
                return False
            return True

        try:
            report = analyze_loudness(
                samples, sample_rate=float(sample_rate), progress=_progress)
        except Exception:  # noqa: BLE001 — 取消或失败：零提交
            return False, "分析已取消", None

        target_lufs = -23.0
        peak_ceiling_dbtp = -3.0
        integrated = float(report.integrated_lufs)
        true_peak = float(report.true_peak_dbtp)
        gain_lufs = compute_normalize_gain_db(integrated, target_lufs)

        # D02：反相/静音素材低于测量门限时不得建议正增益——
        # 此前 -70 LUFS/-inf dBTP 仍算出 +47dB 并被 UI 夹到 +12dB 放大。
        silent = (
            not np.isfinite(integrated)
            or integrated < self.SILENCE_FLOOR_LUFS
            or not np.isfinite(true_peak)
        )
        if silent and (gain_lufs is None or float(gain_lufs) > 0.0):
            return True, (
                f"Integrated：{integrated:.1f} LUFS（低于测量门限，疑似静音或反相抵消）\n"
                "已拒绝正增益建议：请先检查素材实际内容"
            ), None

        gain = float(gain_lufs) if gain_lufs is not None else 0.0
        peak_note = ""
        if np.isfinite(true_peak):
            peak_gain = peak_ceiling_dbtp - true_peak
            if peak_gain < gain:
                gain = peak_gain
                peak_note = "（受峰值上限约束）"
        return True, (
            f"Integrated：{integrated:.1f} LUFS"
            f"（目标 {target_lufs:.0f}）\n"
            f"True Peak：{true_peak:.1f} dBTP"
            f"（上限 {peak_ceiling_dbtp:.0f}）\n"
            f"建议增益：{gain:+.1f} dB{peak_note}"
        ), float(gain)

    def _decode_source(self, cancel_check=None):
        """解码片段源音频为 (channels, N)（素材原始采样率/声道布局）。"""
        from src.features.edit.core.audio_playback import AudioDecoder

        decoder = AudioDecoder()
        if not decoder.open(self._path, timeline_fps=self._source_fps):
            return None, 0
        try:
            if self._start_seconds is not None:
                start_sec = float(self._start_seconds)
                duration_sec = float(self._duration_seconds or 0.05)
            else:
                start_sec = self._source_start / max(self._source_fps, 0.001)
                duration_sec = (
                    self._duration_frames / max(self._source_fps, 0.001)
                    if self._duration_frames > 0 else 0.05
                )
            if not decoder.seek_to_time(max(0.0, start_sec)):
                return None, 0

            sample_rate = float(decoder._sample_rate)
            block_samples = max(1, int(sample_rate))  # ~1s/块
            total_samples = int(duration_sec * sample_rate)
            collected = 0
            blocks = []
            while collected < total_samples:
                if cancel_check is not None and cancel_check():
                    return None, 0
                want = min(block_samples, total_samples - collected)
                block = decoder.decode_next_samples(want)
                if block is None or block.shape[-1] == 0:
                    break
                blocks.append(block)
                collected += block.shape[-1]
            if not blocks:
                return None, 0
            import numpy as _np

            pcm = blocks[0] if len(blocks) == 1 else _np.concatenate(blocks, axis=-1)
            if pcm.ndim == 1:
                pcm = pcm.reshape(1, -1)
            return _np.ascontiguousarray(pcm, dtype=_np.float32), sample_rate
        finally:
            decoder.close()


# W02：蓝徽章源目的轨三态判别哨兵——区分"无条目"与"显式关闭 (None)"。
_UNSET = object()

# B07：presentation payload 版本。export 写当前版本；apply 遇到其他
# 版本（未来/历史）时保留原值回写，已知键按当前语义尽力解释。
PRESENTATION_STATE_VERSION = 1


class TimelineView(QGraphicsView):
    """
    Main timeline view widget using QGraphicsView.

    Manages the visual timeline and editing interactions.

    Signals:
        playhead_changed: Playhead position changed
        selection_changed: Selection changed
        tool_changed: Active editing tool changed
        snap_changed: Snap setting changed
        zoom_changed: Zoom level changed

    Attributes:
        _scene: QGraphicsScene for items
        _timeline: Current TimelineSequence data
        _command_stack: Undo/redo stack
        _selection: Current selection state
        _playhead_frame: Current playhead frame
        _pixels_per_frame: Zoom scale
        _active_tool: Current editing tool
        _snap_enabled: Snap toggle
        _items_map: Mapping from IDs to QGraphicsItems
        _updating: Anti-recursion lock flag
    """

    # Signals
    playhead_changed = pyqtSignal(int)  # frame
    # P1-04：trim 进行中的帧预览请求（cut 邻近帧），上层接到 scrub 预览管线
    trim_frame_preview_requested = pyqtSignal(int)
    # F2.3：trim 会话结束（提交/取消/失焦）——让复用的 scrub 管线退出
    # 量化态（finish_scrub），否则 renderer 滞留 scrub 态到下次拖拽。
    trim_frame_preview_finished = pyqtSignal(int)
    scrub_started = pyqtSignal(int)  # frame
    scrub_moved = pyqtSignal(int)  # frame
    scrub_finished = pyqtSignal(int)  # frame
    selection_changed = pyqtSignal(object)  # TimelineSelection
    tool_changed = pyqtSignal(str)  # tool name
    snap_changed = pyqtSignal(bool)  # enabled
    linked_selection_changed = pyqtSignal(bool)
    position_lock_changed = pyqtSignal(bool)
    source_patch_changed = pyqtSignal(str, str, bool)  # type, destination track, enabled; UI request
    # E22: command_executed 已删除（1 个 emit、0 个 connect 的死信号；
    # timeline_changed 是宿主消费的活链路）
    # 操作失败等状态栏提示（如冻结帧失败）——面板/主窗口接到状态栏展示。
    status_message_requested = pyqtSignal(str)
    clip_speed_settings_requested = pyqtSignal(str, str, object)  # advanced speed edit draft for host
    zoom_changed = pyqtSignal(float)  # pixels_per_frame
    effect_added = pyqtSignal(str, str)  # clip_id, effect_id (pre-generated so the panel can scroll to it)
    # E09: 双击片段 → 在 Source Viewer 打开（clip_id, 源帧号）
    clip_open_in_source_requested = pyqtSignal(str, int)
    timeline_changed = pyqtSignal(object)  # TimelineSequence - emitted after command execution
    timeline_duration_extended = pyqtSignal(int)  # Runtime editable duration in frames
    project_changed = pyqtSignal(object)  # EditProject - emitted after external file import
    project_timeline_transaction_committed = pyqtSignal(object, object, object)
    project_needed = pyqtSignal()  # Request auto-creation before an external file drop
    # P9.2: Edit → Composite page linkage
    open_in_composite_requested = pyqtSignal(str, str)  # clip_id, track_id
    # T7: Edit → Color page linkage via right-click
    open_in_color_requested = pyqtSignal(str, str, str)  # clip_id, track_id, stack_id
    # Phase 6: Comp Clip context menu signals
    convert_to_comp_clip_requested = pyqtSignal(str, str)  # clip_id, track_id
    remove_composite_binding_requested = pyqtSignal(str, str)  # clip_id, track_id
    bypass_composite_requested = pyqtSignal(str, str)  # clip_id, track_id
    reset_composite_requested = pyqtSignal(str, str)  # clip_id, track_id
    reveal_comp_clip_in_media_pool_requested = pyqtSignal(str, str)  # clip_id, track_id
    render_cache_clip_requested = pyqtSignal(str, str)  # clip_id, track_id
    rerender_cache_clip_requested = pyqtSignal(str, str)  # clip_id, track_id
    # P9.1: 共享素材调色 / 本地调色副本（此前只有菜单项发射，没有接收者）
    use_shared_grade_requested = pyqtSignal(str, str)  # clip_id, track_id
    make_local_grade_copy_requested = pyqtSignal(str, str)  # clip_id, track_id
    teaching_animation_open_requested = pyqtSignal(str, str)  # clip_id, track_id
    # Scene Cut Detection (P9)
    scene_cut_detect_requested = pyqtSignal(str, str, str)  # clip_id, track_id, asset_id
    # P1-02: 嵌套序列工作流
    create_nested_from_selection_requested = pyqtSignal(list)  # clip_ids
    enter_nested_sequence_requested = pyqtSignal(str, str)  # child_timeline_id, clip_id
    # P1-01: 多机位角度键（1..9），playhead 下是多机位片段时生效
    multicam_angle_key_requested = pyqtSignal(int)
    # Audio clip transcription → subtitle track generation.
    speech_to_text_requested = pyqtSignal(str, str, str)  # clip_id, track_id, asset_id
    # P10.3: Subtitle clip operations — forwarded to controller for command execution
    add_subtitle_clip_requested = pyqtSignal(str, int)  # track_id, at_frame
    subtitle_to_speech_requested = pyqtSignal(str)  # track_id
    subtitle_export_requested = pyqtSignal(str, str)  # track_id, format
    subtitle_quality_requested = pyqtSignal(str)  # track_id
    subtitle_language_requested = pyqtSignal(str)  # track_id
    subtitle_clip_clicked = pyqtSignal(str, str)  # clip_id, track_id
    subtitle_clip_drag_finished = pyqtSignal(str, str, int, int, str, bool)
    # clip_id, source_track_id, old_start, new_start, target_track_id, copy_requested
    subtitle_clip_trim_left_finished = pyqtSignal(str, str, int, int, int, int)  # clip_id, track_id, old_start, old_dur, new_start, new_dur
    subtitle_clip_trim_right_finished = pyqtSignal(str, str, int, int)  # clip_id, track_id, old_dur, new_dur
    subtitle_clip_delete = pyqtSignal(str, str)  # clip_id, track_id
    caption_graphic_action_requested = pyqtSignal(str, str)  # clip_id, action
    caption_track_graphic_template_requested = pyqtSignal(str, str)  # track_id, template_id
    # P10.3: edit text via double-click dialog
    subtitle_clip_edit_text_requested = pyqtSignal(str, str, str)  # clip_id, track_id, new_text
    # Top-left DaVinci-like timecode panel, painted in the same view as the ruler.
    corner_timecode_clicked = pyqtSignal()
    # B8: global edit mode switched ("overwrite" | "insert")
    edit_mode_changed = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        # Layout engine — single source of truth for all coordinates
        self._layout_engine = TimelineLayoutEngine()
        self._layout_state = TimelineLayoutState()
        self._layout_config_signature: tuple[Any, ...] | None = None

        # Anti-recursion lock
        self._updating: bool = False
        # AUD-21400: 间隙指示器专用防重入锁。_update_gap_indicators 会被
        # load_timeline / update_timeline_incremental 在 self._updating 锁内
        # 调用；若复用同一把锁，入口守卫恒真，功能整体失效。
        self._updating_gaps: bool = False

        # State
        self._timeline: Optional[TimelineSequence] = None
        # A newly opened project gets a UI-only empty V1/A1 layout.  It is
        # promoted to a real project timeline when the first asset is dropped.
        self._provisional_timeline: bool = False
        # Empty sequences derive a duration of zero from their clips.  Keep a
        # view-owned extent so they can still be extended interactively.
        self._runtime_duration_frames: int = 0
        # A timeline can be loaded while its dock/page is hidden.  In that
        # state QGraphicsView has not finished laying out the scrollbar yet,
        # so resetting it immediately is not reliable.  Keep the request
        # pending until the view becomes visible and Qt has applied the final
        # scrollbar range.
        self._timeline_start_reset_pending: bool = False
        self._project: Optional[Any] = None  # EditProject reference for drag operations
        # Motion Graphic frames are cached asynchronously in the shared scene
        # cache, so a small UI-only poll lets the ruler turn green immediately
        # after background/first-playback rendering completes.  Disk cache
        # completion still performs an explicit project refresh as well.
        self._cache_indicator_timer = QTimer(self)
        self._cache_indicator_timer.setInterval(500)
        self._cache_indicator_timer.timeout.connect(self._update_cache_indicator)
        self._command_stack = CommandStack()
        self._timeline_command_stacks: dict[str, CommandStack] = {}
        self._timeline_history_snapshots: dict[str, TimelineSequence] = {}
        self._project_transaction_sequences: dict[str, TimelineSequence] = {}
        self._transaction_project_provider: Optional[Callable[[], Any]] = None
        self._transaction_sequences_provider: Optional[Callable[[], Mapping[str, TimelineSequence]]] = None
        # A passive Tool monitor may already have taken focus from this view
        # on some platforms. A later real window switch then sends no second
        # focusOutEvent here, so also observe the application window boundary.
        QApplication.instance().focusWindowChanged.connect(
            self._on_application_focus_window_changed)
        QApplication.instance().applicationStateChanged.connect(
            self._on_application_state_changed)
        self._selection = TimelineSelection()
        self._selected_edit_edges: dict[str, str] = {}
        self._playhead_frame = 0
        # Retime controls are transient presentation state, just as in
        # Resolve.  The speed points themselves are persisted in SpeedParams.
        self._retime_control_clip_ids: set[str] = set()
        self._trim_monitor_host: Optional[ProgramViewerWidget] = None
        self._corner_timecode_text: str = "00:00:00:00"
        self._pixels_per_frame = DEFAULT_ZOOM
        self._active_tool = EditTool.SELECT
        self._position_lock_enabled: bool = False
        self._thumbnail_display_mode: str = ClipItem._thumbnail_display_mode
        self._waveforms_visible: bool = True
        self._source_patch_routes: dict[tuple[str, str], Optional[str]] = {}
        # B07：presentation 原始 payload 的未知字段原样保留（apply 捕获、
        # export 回写），未来工程重存不丢扩展配置；路由嵌套层的未知
        # 流类型键同样保留。
        self._presentation_extras: dict[str, Any] = {}
        self._presentation_route_extras: dict[str, dict[str, Any]] = {}
        self._snap_enabled = True
        self._interaction = InteractionSession()  # Interaction state machine
        self._current_drag_session: Optional[ClipDragSession] = None  # P2-2: Clip drag session
        self._independent_selection: bool = False
        self._linked_selection_enabled: bool = True  # P3: Toggle linked A/V selection (on by default)
        self._playhead_follow_enabled = PLAYHEAD_FOLLOW_ENABLED  # Playhead centering during playback
        self._target_track_id: Optional[str] = None  # UI-09: Target track for insert operations (video)
        self._target_audio_track_id: Optional[str] = None  # P2: Separate audio target track

        # P2: Clipboard for copy/paste
        self._clipboard: Optional[TimelineClipboardPayload] = None

        # Rubber band selection state
        self._rubber_band: Optional[QRubberBand] = None
        self._rubber_band_origin: Optional[QPoint] = None
        self._rubber_band_active: bool = False
        self._rubber_band_start_scene: Optional[QPointF] = None

        # UI-10: Playhead drag state
        self._playhead_dragging: bool = False
        self._playhead_drag_start_frame: int = 0

        # INT-03: 手型/中键平移状态（viewport 像素 → 滚动条原点）
        self._pan_origin_scroll: tuple[int, int] = (0, 0)
        self._pan_origin_global: 'QPoint' = QPoint()
        self._pan_prev_cursor: Optional[Qt.CursorShape] = None

        # Splitter drag state (V/A area divider + S/V subtitle divider)
        self._splitter_dragging: bool = False
        self._splitter_drag_kind: str = "video"  # "video" (V/A) or "subtitle" (S/V)
        self._splitter_drag_start_y: float = 0.0
        self._splitter_drag_start_ratio: float = 0.5
        self._splitter_keep_video_bottom: bool = True
        self._syncing_split_scrollbars: bool = False
        # AUD-21410: coalesce the full relayout triggered by the three
        # split-scrollbar handlers to at most one per display frame instead
        # of one per scrollbar tick.
        self._scroll_layout_timer = QTimer(self)
        self._scroll_layout_timer.setSingleShot(True)
        self._scroll_layout_timer.setInterval(16)
        self._scroll_layout_timer.timeout.connect(self._apply_scroll_layout_update)

        # Track boundary resize state
        self._track_resize_dragging: bool = False
        self._track_resize_track_id: Optional[str] = None
        self._track_resize_start_y: float = 0.0
        self._track_resize_start_height: int = 0
        self._track_resize_is_audio: bool = False

        # Drag and drop state (参考 kdenlive dragProxy)
        self._drag_preview_item: Optional[DragPreviewItem] = None  # Enhanced drag preview
        # Alt-drag subtitle copies use a separate graphics item so the source
        # cue remains visible at its original position throughout the drag.
        self._subtitle_copy_drag_preview_item: Optional[SubtitleClipItem] = None
        self._subtitle_copy_drag_preview_source_id: Optional[str] = None
        # Normal subtitle drags can carry all selected cues on the same
        # subtitle lane.  The session is created lazily on the first move so
        # a click/trim never accidentally becomes a group operation.
        self._subtitle_drag_session: Optional[dict[str, Any]] = None
        # Alt-drag copy ghosts for regular clips (clip_id -> ghost ClipItem)
        self._alt_copy_ghosts: dict[str, ClipItem] = {}
        self._drag_asset_ids: list[str] = []
        self._drag_assets: list[MediaAsset] = []
        # VP-B08: Source 拖放载荷（viewer 源区间 + 流意图），优先于通用
        # media-pool asset MIME 解析。
        self._drag_source_payload = None
        self._drag_render_mode_active: bool = False
        self._drag_transition_type: Optional[str] = None  # For transition drag
        self._drag_effect_type: Optional[str] = None  # For effect drag (NEW)
        self._drag_generator_type: Optional[str] = None  # For generator (Adjustment/Solid) drag — creates a clip
        self._drag_motion_graphic_generator_params: dict[str, Any] = {}
        self._drag_motion_graphic_generator_id: Optional[str] = None
        self._drag_file_paths: List[str] = []  # For external file drag (fallback)
        self._drag_file_previews: list[tuple[str, str, int | None]] = []
        # During a multi-asset drop, collect the already-built clip commands
        # and publish them as one MacroCommand. This prevents one timeline
        # reload/waveform refresh per clip from monopolising the UI thread.
        self._batch_drop_commands: Optional[list[Any]] = None

        # Thumbnail provider for drag preview
        self._thumb_provider = ThumbnailProvider.instance()
        self._thumb_provider.frame_thumbnail_ready.connect(self._on_frame_thumbnail_ready)

        # Items mapping
        self._items_map: Dict[str, Any] = {}  # clip_id -> ClipItem
        self._roll_handles: Dict[str, Any] = {}  # clip_id -> RollHandle
        self._roll_preview_state: Optional[dict[str, Any]] = None
        # RI-02/R14：修剪/Slide 预览直接改写的非主对象（linked 伙伴、邻
        # 片、波纹跟随）登记处——取消时统一从不可变模型重投影，此前
        # cancel 只恢复 primary，伙伴/邻片残留错误源区间与几何。
        self._preview_affected_item_ids: set[str] = set()
        self._source_edit_linked_at_press: bool = True
        self._source_edit_timeline_at_press: Optional[TimelineSequence] = None
        # RI-08/R19：亚像素锚定残差挂点——当前缩放锚定用连续 double 帧
        # + round 补偿（≤1px 量化误差来自整数滚动条）；未来接入
        # 布局/viewport 亚像素原点机制时由此字段承载。
        self._scene_origin_residual: float = 0.0
        # N16/T3-08：缩放手势级连续锚帧。每次 wheel 事件从已量化滚动条
        # 重取锚会把 ≤0.5px 量化误差回灌进锚（20+20 次实测累计 2.10px
        # 单向漂移）；手势窗口内锚定帧只在起点测一次，其后从缓存推导。
        self._zoom_gesture_anchor_frame: float | None = None
        self._zoom_gesture_last_t: float = 0.0
        self._track_headers: Dict[str, TrackHeaderItem] = {}
        self._gap_items: Dict[str, GapIndicatorItem] = {}  # gap_id -> GapIndicatorItem (Phase 3.3)
        self._marker_items: Dict[str, MarkerItem] = {}  # marker_id -> MarkerItem (Phase 5.1)
        self._playhead_item: Optional[PlayheadItem] = None
        self._ruler_item: Optional[TimecodeRulerItem] = None
        self._snap_indicator: Optional[SnapIndicatorItem] = None  # Snap visual feedback
        self._range_mask_item: Optional[TimelineRangeMaskItem] = None
        self._range_item: Optional[TimelineRangeItem] = None  # P4: Range bar
        # Deliver can temporarily hide the model's range visualization when
        # the export mode is Entire Timeline.  None restores normal Edit
        # behavior; True/False are display-only overrides.
        self._deliver_range_visibility: Optional[bool] = None

        # B1: scene cut detection candidate overlay (view-level, lazy).
        # Candidates are presentation-only visuals and never enter the model.
        self._scene_cut_overlay: Optional[Any] = None
        self._scene_cut_overlay_visible: bool = False
        self._scene_cut_candidates: tuple[Any, ...] = ()

        # B8: global edit mode ("overwrite" | "insert").  Governs media-pool
        # drops; explicit Insert/Overwrite buttons stay mode-independent.
        self._edit_mode: str = "overwrite"

        # Snap configuration
        self._snap_threshold_frames: int = SNAP_THRESHOLD_FRAMES  # Configurable threshold

        # Enhanced Snap System (参考 kdenlive SnapModel)
        self._snap_manager = SnapManager()  # 吸附点管理器
        self._snap_temporarily_disabled: bool = False  # Shift 键临时禁用吸附
        self._snap_excluded_types: set[SnapPointType] = set()  # 排除的吸附点类型

        # Create scene
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        # View settings
        self.setBackgroundBrush(QBrush(BACKGROUND_COLOR))
        self.setRenderHint(QPainter.RenderHint.Antialiasing, False)  # Faster
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.horizontalScrollBar().setStyleSheet(f"""
            QScrollBar:horizontal {{ height: 8px; margin: 0; border: none;
                background: {get_color('bg_app')}; }}
            QScrollBar::handle:horizontal {{ min-width: 40px; margin: 1px;
                border: none; border-radius: 3px; background: {get_color('border')}; }}
            QScrollBar::handle:horizontal:hover {{ background: {get_color('accent')}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0; border: none; background: transparent; }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                border: none; background: transparent; }}
        """)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)  # We handle drag manually
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Enable drop for media pool assets
        self.setAcceptDrops(True)

        # The timeline is a 2D QGraphicsView. Using QOpenGLWidget as its
        # viewport makes Qt's scene teardown/repaint path native-GL backed on
        # macOS; replacing clips while async pixmaps are arriving can then
        # crash inside QGraphicsView::paintEvent. Keep OpenGL for the actual
        # preview viewers, but use Qt's default raster viewport here so item
        # replacement and overlay painting have one stable paint target.
        self._video_v_scroll = QScrollBar(Qt.Orientation.Vertical, self.viewport())
        self._audio_v_scroll = QScrollBar(Qt.Orientation.Vertical, self.viewport())
        # SUB-P1-06: dedicated subtitle-area scrollbar so multi-language cue
        # stacks can be browsed when they overflow the subtitle lane.
        self._subtitle_v_scroll = QScrollBar(Qt.Orientation.Vertical, self.viewport())
        for bar in (self._video_v_scroll, self._audio_v_scroll, self._subtitle_v_scroll):
            bar.setObjectName("timelineSplitVerticalScrollBar")
            bar.setFixedWidth(11)
            bar.setVisible(False)
            bar.setStyleSheet("""
                QScrollBar:vertical {
                    background: #202025;
                    border: 1px solid #34343A;
                    width: 11px;
                    margin: 0;
                }
                QScrollBar::handle:vertical {
                    background: #5A6068;
                    border: 1px solid #777E88;
                    border-radius: 2px;
                    min-height: 28px;
                }
                QScrollBar::handle:vertical:hover {
                    background: #747B84;
                }
                QScrollBar::add-line:vertical,
                QScrollBar::sub-line:vertical {
                    height: 0;
                    border: none;
                    background: transparent;
                }
                QScrollBar::add-page:vertical,
                QScrollBar::sub-page:vertical {
                    background: transparent;
                }
            """)
        self._video_v_scroll.valueChanged.connect(self._on_video_scrollbar_changed)
        self._audio_v_scroll.valueChanged.connect(self._on_audio_scrollbar_changed)
        self._subtitle_v_scroll.valueChanged.connect(self._on_subtitle_scrollbar_changed)
        self._hide_split_vertical_scrollbars()

        # Viewport optimization flags (参考 kdenlive/QGraphicsView best practices)
        # MinimalViewportUpdate: 播放头每 tick 移动只重绘新旧位置窄条
        # （实测 FullViewportUpdate 下 25fps 播放时每次 paintEvent 全量重绘
        # ~22.5ms，UI 线程 50% 时间耗在时间线重绘上，预览呈现被压到 ~21fps）。
        # 固定表头列是 paintEvent 里画的 overlay，MinimalViewportUpdate 的
        # bitblt 滚动会把表头内容一起平移产生残影——滚动时通过
        # _on_viewport_scrolled 强制全量重绘来保持表头正确（滚动非热路径）。
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.horizontalScrollBar().valueChanged.connect(self._on_viewport_scrolled)
        self.verticalScrollBar().valueChanged.connect(self._on_viewport_scrolled)

        # 角落时间码独立子 widget：若在 viewport overlay 里画，播放时它
        # （x=0 处）与播放头窄条的脏区会在同一重绘周期合并成从左边缘直
        # 到播放头位置的全高大区域（实测 478×932px、~26ms/次，25fps 播放
        # 下 UI 线程被时间线重绘饱和）。独立 widget 的重绘只涉及自身。
        self._corner_timecode_widget = _TimelineTimecodeLabel("00:00:00:00", self)
        _tc_font = QFont(
            "SF Mono, Menlo, Consolas, Courier New, monospace", 17,
            QFont.Weight.DemiBold)
        _tc_font.setStyleHint(QFont.StyleHint.Monospace)
        self._corner_timecode_widget.setFont(_tc_font)
        self._corner_timecode_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._corner_timecode_widget.setStyleSheet(
            f"background-color: {get_color('timecode_panel_bg')};"
            f"color: {get_color('timecode_panel_text')};"
            f"border-bottom: 1px solid {get_color('timecode_panel_bottom_line')};"
        )
        self._corner_timecode_widget.setGeometry(
            0, 0, TRACK_HEADER_WIDTH, RULER_HEIGHT)
        # 必须声明为不透明：否则 label 每次文本更新都会连带重绘其下方
        # 的 viewport 区域（x=0 处），与同周期的播放头窄条合并成大脏区。
        self._corner_timecode_widget.setAttribute(
            Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._corner_timecode_widget.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._corner_timecode_widget.raise_()
        # Keep painter state isolation. Several timeline items use clip regions;
        # disabling Qt's save/restore lets one item's clip affect the ruler,
        # playhead, and following clips.
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontSavePainterState, False)
        # DontAdjustForAntialiasing: faster when antialiasing is disabled
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)

        # Thumbnail scheduler for viewport-aware batch decoding
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        self._thumb_scheduler = TimelineThumbnailScheduler()
        self._thumb_scheduler.set_batch_callback(self._on_thumbnail_batch_ready)
        self._pending_thumb_jobs: dict[tuple[str, int], Any] = {}  # (file_path, frame) -> job
        # AUD-21403: 防抖用单例 single-shot QTimer，__init__ 创建后仅
        # start/stop 复用——旧实现每次调用 new 一个 QTimer（父对象是
        # 视图、从不 deleteLater），滚动/缩放期间对象随事件线性累积。
        self._scroll_debounce_timer = QTimer(self)
        self._scroll_debounce_timer.setSingleShot(True)
        self._scroll_debounce_timer.setInterval(80)
        self._scroll_debounce_timer.timeout.connect(self._submit_viewport_thumbnails)

        # Initialize empty view
        self._initialize_scene()

        logger.debug("TimelineView initialized")

    # ==================== Initialization ====================

    def set_corner_timecode(self, timecode: str) -> None:
        """Update the fixed top-left timecode text painted over the header column."""
        if self._corner_timecode_text == timecode:
            return
        self._corner_timecode_text = timecode
        # 独立 QLabel 自己重绘自身（不再污染 viewport 脏区）。
        self._corner_timecode_widget.setText(timecode)

    def _initialize_scene(self) -> None:
        """Initialize empty scene with basic elements."""
        self._remove_persistent_items()
        self._hide_split_vertical_scrollbars()

        # Add ruler placeholder
        self._ruler_item = TimecodeRulerItem(
            self._pixels_per_frame,
            EMPTY_TIMELINE_FPS,
            0,
        )
        self._scene.addItem(self._ruler_item)

        # Connect ruler signals for playhead interaction
        self._ruler_item.ruler_clicked.connect(self._on_ruler_clicked)
        self._ruler_item.ruler_dragged.connect(self._on_ruler_dragged)
        self._ruler_item.ruler_scrub_finished.connect(self._on_ruler_scrub_finished)
        self._ruler_item.mark_in_requested.connect(self._mark_in_at_frame)
        self._ruler_item.mark_out_requested.connect(self._mark_out_at_frame)
        self._ruler_item.clear_range_requested.connect(self._clear_timeline_range)

        # Add placeholder playhead (P1.4 - with fps for timecode display)
        # 播放头不作为场景 item 绘制：场景 item 移动会把脏区扩散到所有
        # 相交的 clip/ruler（每 tick ~7ms 重绘风暴，9轨时占主线程 ~30%）。
        # 它保留为几何/命中测试参照（setPos 仍维护 scenePos 供
        # _is_playhead_hit / _ensure_playhead_visible 使用），视觉由
        # drawForeground 覆盖层绘制，移动只刷新播放头窄竖条。
        self._playhead_item = PlayheadItem(
            self._playhead_frame,
            self._pixels_per_frame,
            1,  # At least 1 track for height
            EMPTY_TIMELINE_FPS,  # Updated when a timeline loads
        )

        # Add snap indicator (initially hidden)
        self._snap_indicator = SnapIndicatorItem(
            0,
            self._pixels_per_frame,
            1,
        )
        self._scene.addItem(self._snap_indicator)

        # Add enhanced drag preview item (参考 kdenlive dragProxy)
        self._drag_preview_item = DragPreviewItem(self._pixels_per_frame)
        self._scene.addItem(self._drag_preview_item)

        # P4: Add range bar item
        self._range_mask_item = TimelineRangeMaskItem(self._pixels_per_frame)
        self._scene.addItem(self._range_mask_item)

        self._range_item = TimelineRangeItem(
            self._pixels_per_frame,
            EMPTY_TIMELINE_FPS,
        )
        self._scene.addItem(self._range_item)
        self._range_item.range_handle_dragged.connect(self._on_range_handle_dragged)
        self._range_item.range_change_finished.connect(self._on_range_change_finished)

        # There is no model timeline yet, but the Edit page should still show
        # a complete, useful empty workspace instead of only a short ruler.
        self._update_empty_scene_geometry()

    def _update_empty_scene_geometry(self) -> None:
        """Configure the visible placeholder workspace when no timeline exists."""
        if self._timeline is not None:
            return

        viewport = self.viewport()
        viewport_width = max(1, viewport.width()) if viewport is not None else 800
        viewport_height = max(1, viewport.height()) if viewport is not None else 400
        preview_frames = max(
            1,
            int(round(EMPTY_TIMELINE_FPS * EMPTY_TIMELINE_PREVIEW_SECONDS)),
        )

        if self._ruler_item is not None:
            self._ruler_item.update_fps(EMPTY_TIMELINE_FPS)
            self._ruler_item.update_duration(preview_frames)
            self._ruler_item.set_viewport_width(
                max(0.0, float(viewport_width - TRACK_HEADER_WIDTH))
            )

        # No timeline means there is no meaningful content to scroll. Keep the
        # complete placeholder visible in the available panel instead of
        # exposing a scrollbar for an artificial scene extent.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scene_width = max(
            float(viewport_width),
            float(TRACK_HEADER_WIDTH + preview_frames * self._pixels_per_frame + 100),
        )
        scene_height = max(
            float(viewport_height),
            float(RULER_HEIGHT + TRACK_HEIGHT * 2 + SPLITTER_HEIGHT),
        )
        self._apply_scene_rect(0.0, 0.0, scene_width, scene_height)

        # The placeholder still uses a PlayheadItem so the zero-frame marker
        # remains consistent with a real timeline.  Give it the complete
        # V/A workspace height; the default constructor value only spans one
        # track, which makes the red line stop halfway through the empty view.
        if self._playhead_item is not None:
            self._playhead_item.update_num_tracks(2)
            self._playhead_item.update_track_area_height(
                max(0.0, scene_height - float(RULER_HEIGHT))
            )

    def _remove_persistent_items(self) -> None:
        """Remove ruler, playhead, snap indicator, and drag preview items."""
        for item in (
            self._ruler_item,
            self._playhead_item,
            self._snap_indicator,
            self._drag_preview_item,
            self._range_mask_item,
            self._range_item,
        ):
            if item is not None and item.scene() is self._scene:
                self._scene.removeItem(item)

        self._ruler_item = None
        self._playhead_item = None
        self._snap_indicator = None
        self._drag_preview_item = None
        self._range_mask_item = None
        self._range_item = None

    def _effective_timeline_duration(self, timeline: Optional[TimelineSequence] = None) -> int:
        """
        Return a UI-safe timeline duration based on sequence and clip ends.

        Guarantees an initial visible duration so the ruler is never blank,
        then retains any runtime extent created by user interaction.
        """
        seq = timeline or self._timeline
        if seq is None:
            return 0

        # Keep the media-only model duration separate from the usable canvas
        # duration.  An empty sequence is a real black timeline and must have
        # a range that can be scrubbed, marked and exported.
        duration = effective_timeline_duration_frames(
            seq,
            empty_duration_seconds=TIMELINE_EXTENSION_SECONDS,
        )
        fps_value = getattr(seq, "fps", EMPTY_TIMELINE_FPS) or EMPTY_TIMELINE_FPS
        try:
            fps = float(fps_value.to_float() if hasattr(fps_value, "to_float") else fps_value)
        except (TypeError, ValueError):
            fps = EMPTY_TIMELINE_FPS
        min_duration = max(1, int(round(max(1.0, fps) * TIMELINE_EXTENSION_SECONDS)))
        return max(duration, min_duration, self._runtime_duration_frames)

    def _extend_runtime_duration(self, frame: int) -> None:
        """Extend an empty or short timeline when the playhead reaches its end.

        ``TimelineSequence`` derives its duration from clips, so persisting an
        empty clip purely to make blank space would be incorrect.  The view
        instead grows a runtime extent in ten-second chunks and tells the
        playback panel to use the same newly available range.
        """
        if self._timeline is None:
            return

        required_duration = max(1, int(frame) + 1)
        if required_duration <= self._effective_timeline_duration():
            return

        fps = float(self._timeline.fps) if self._timeline.fps else EMPTY_TIMELINE_FPS
        extension_chunk = max(1, int(round(fps * TIMELINE_EXTENSION_SECONDS)))
        self._runtime_duration_frames = max(
            self._runtime_duration_frames,
            ((required_duration + extension_chunk - 1) // extension_chunk)
            * extension_chunk,
        )

        duration = self._effective_timeline_duration()
        if self._ruler_item is not None:
            self._ruler_item.update_duration(duration)
            self._update_ruler_viewport()
        self._update_scene_rect()
        self.timeline_duration_extended.emit(duration)

    def _ordered_tracks(self) -> List[EditTrack]:
        """Return tracks in visual order (matching layout engine's all_ordered_tracks).

        Visual order top→bottom: subtitle (overlay), video (reversed), audio.
        Subtitles sit on top because they are visual overlays rendered above
        the picture — matching DaVinci Resolve / Final Cut Pro.
        """
        if self._timeline is None:
            return []
        return (
            list(self._timeline.subtitle_tracks)
            + list(reversed(self._timeline.video_tracks))
            + list(self._timeline.audio_tracks)
        )

    def _apply_scene_rect(self, x: float, y: float, w: float, h: float) -> None:
        """应用 sceneRect（R19：亚像素原点机制的统一入口，当前恒等）。"""
        self.setSceneRect(x, y, w, h)

    def _configure_layout_engine(self) -> None:
        """Sync layout engine with current timeline data and viewport size."""
        tracks = []
        for t in self._timeline.all_tracks if self._timeline is not None else ():
            tracks.append(TrackInfo(
                track_id=t.track_id,
                track_type=t.track_type,
                default_height=self._default_track_height(t),
            ))
        # Retime title unfolds upward; keep the saved track sizes untouched.
        expanded_tracks = {
            track.track_id for track in self._timeline.all_tracks
            if track.track_type in {"video", "audio"}
            and self._retime_control_clip_ids
            and any(clip.clip_id in self._retime_control_clip_ids for clip in track.clips)
        } if self._timeline is not None else set()
        effective_heights = dict(self._layout_state.track_heights)
        for track in tracks:
            if track.track_id in expanded_tracks:
                effective_heights[track.track_id] = effective_heights.get(track.track_id, track.default_height) + 24
        layout_state = replace(self._layout_state, track_heights=effective_heights)
        vp = self.viewport()
        w = float(vp.width()) if vp else 800.0
        h = float(vp.height()) if vp else 400.0
        signature = (
            tuple((t.track_id, t.track_type, t.default_height) for t in tracks),
            round(w, 3),
            round(h, 3),
            round(float(self._pixels_per_frame), 6),
            round(float(self._layout_state.video_area_ratio), 6),
            round(float(self._layout_state.subtitle_area_ratio), 6),
            int(self._layout_state.video_scroll_offset),
            int(self._layout_state.audio_scroll_offset),
            int(self._layout_state.subtitle_scroll_offset),
            tuple(sorted((str(k), int(v)) for k, v in effective_heights.items())),
        )
        if signature == self._layout_config_signature:
            return
        self._layout_config_signature = signature
        self._layout_engine.configure(
            tracks=tracks,
            state=layout_state,
            viewport_width=w,
            viewport_height=h,
            pixels_per_frame=self._pixels_per_frame,
        )

    def _default_track_height(self, track: EditTrack) -> int:
        """Return default visual height for a track."""
        if track.track_type == "audio":
            return int(EDIT_SIZES["track_h_audio"])
        if track.track_type == "subtitle":
            # Subtitle lanes are fixed and compact by design.
            return DEFAULT_SUBTITLE_TRACK_HEIGHT
        return int(EDIT_SIZES["track_h_video"])

    def _track_height_for(self, track: EditTrack) -> int:
        """Return current visual height for a track."""
        return self._layout_engine.track_height(track.track_id)

    def _track_top_for_index(self, target_index: int) -> float:
        """Return cumulative top y for the visual track index."""
        self._configure_layout_engine()
        all_ordered = self._layout_engine.all_ordered_tracks()
        if 0 <= target_index < len(all_ordered):
            return self._layout_engine.track_top(all_ordered[target_index].track_id)
        return float(EDIT_SIZES["track_area_top"])

    def _visual_index_for_track_id(self, track_id: str) -> int:
        """Return the visual (top-to-bottom) index for a track_id, or -1."""
        self._configure_layout_engine()
        all_ordered = self._layout_engine.all_ordered_tracks()
        for i, t in enumerate(all_ordered):
            if t.track_id == track_id:
                return i
        return -1

    def _track_index_at_y(self, scene_y: float) -> int:
        """Return visual track index for a scene y coordinate, or -1 outside tracks."""
        # Use layout engine for area-aware hit testing
        self._configure_layout_engine()
        track_id, area = self._layout_engine.track_at_y_with_area(scene_y)
        if track_id is None:
            return -1  # ruler, splitter, or empty
        # Find visual index
        all_ordered = self._layout_engine.all_ordered_tracks()
        for i, t in enumerate(all_ordered):
            if t.track_id == track_id:
                return i
        return -1

    def _track_at_visual_index(self, visual_index: int) -> Optional[EditTrack]:
        """Return the EditTrack at the given visual index, or None."""
        if self._timeline is None or visual_index < 0:
            return None
        self._configure_layout_engine()
        ordered = self._layout_engine.all_ordered_tracks()
        if visual_index >= len(ordered):
            return None
        return self._timeline.get_track_by_id(ordered[visual_index].track_id)

    def _total_track_area_height(self) -> float:
        """Return total height occupied by visible tracks."""
        total = 0.0
        for track in self._ordered_tracks():
            total += self._track_height_for(track) + TRACK_GAP
        return total

    def _layout_track_items(self) -> None:
        """Position track-bound items using the layout engine."""
        if self._timeline is None:
            return

        self._configure_layout_engine()
        engine = self._layout_engine

        # Determine topmost track IDs for top separator line
        topmost_video_id = self._timeline.video_tracks[-1].track_id if self._timeline.video_tracks else None
        topmost_audio_id = self._timeline.audio_tracks[0].track_id if self._timeline.audio_tracks else None
        active_roll_keys: set[str] = set()

        # AUD-21410: resolve the visual track order once per layout pass —
        # recomputing it inside the per-track loop made layout O(n^2).
        all_ordered = engine.all_ordered_tracks()
        ordered_index = {ti.track_id: i for i, ti in enumerate(all_ordered)}

        for track in self._timeline.video_tracks + self._timeline.audio_tracks:
            track_id = track.track_id
            track_top = engine.track_top(track_id)
            track_h = engine.track_height(track_id)

            # Find the visual index for this track
            track_index = ordered_index.get(track_id, 0)

            # Position header
            header = self._track_headers.get(track_id)
            if header is not None:
                header.set_source_patch_enabled(self.source_patch_track_id(track.track_type) == track_id)
                header.set_track_height(track_h)
                header.set_track_index(track_index)
                header.set_clip_count(len(track.clips))
                if hasattr(header, "set_badge_index"):
                    type_tracks = (
                        list(self._timeline.video_tracks)
                        if track.track_type == "video"
                        else list(self._timeline.audio_tracks)
                    )
                    badge_index = next(
                        (i for i, t in enumerate(type_tracks) if t.track_id == track_id),
                        track_index,
                    )
                    header.set_badge_index(badge_index)
                header.setPos(0, track_top)
                header._is_topmost = (track_id == topmost_video_id or track_id == topmost_audio_id)

            # Position clips (skip clips being actively dragged OR linked clips in active drag)
            expanded = any(c.clip_id in self._retime_control_clip_ids for c in track.clips)
            base_height = self._layout_state.track_heights.get(track_id, self._default_track_height(track))
            if expanded:
                from src.features.edit.ui.timeline_layout import (
                    MIN_VIDEO_TRACK_HEIGHT, MAX_VIDEO_TRACK_HEIGHT,
                    MIN_AUDIO_TRACK_HEIGHT, MAX_AUDIO_TRACK_HEIGHT,
                )
                lo, hi = ((MIN_VIDEO_TRACK_HEIGHT, MAX_VIDEO_TRACK_HEIGHT)
                          if track.track_type == "video" else (MIN_AUDIO_TRACK_HEIGHT, MAX_AUDIO_TRACK_HEIGHT))
                base_height = max(lo, min(hi, base_height))
            expansion = max(0, track_h - base_height) if expanded else 0
            for clip in track.clips:
                clip_item = self._items_map.get(clip.clip_id)
                if clip_item and isinstance(clip_item, ClipItem):
                    extra = expansion if clip.clip_id not in self._retime_control_clip_ids else 0
                    clip_item.set_track_height(track_h - extra)
                    clip_item.update_track_index(track_index)
                    # Don't reset position of clips being dragged
                    if clip_item._drag_pending or clip_item._is_dragging:
                        continue
                    # Don't reset linked/selected clips that are part of an active drag session
                    if (self._current_drag_session is not None and
                            clip.clip_id in self._current_drag_session.original_positions):
                        continue
                    x = engine.frame_to_x(clip.timeline_start)
                    clip_item.setPos(x, track_top + extra)

            # A cut between adjacent pieces of one source is visually softer
            # than a cut between different media.  Resolve expresses this as
            # a dashed seam; the model's asset_id is the reliable source
            # identity, while timeline adjacency prevents dashes across gaps.
            ordered_clips: list[ClipInstance] = sorted(
                track.clips,
                key=lambda candidate: (candidate.timeline_start, candidate.clip_id),
            )
            for clip_index, clip in enumerate(ordered_clips):
                clip_item = self._items_map.get(clip.clip_id)
                if not isinstance(clip_item, ClipItem):
                    continue
                next_clip: Optional[ClipInstance] = (
                    ordered_clips[clip_index + 1]
                    if clip_index + 1 < len(ordered_clips)
                    else None
                )
                is_same_source_cut = bool(
                    next_clip is not None
                    and clip.timeline_end == next_clip.timeline_start
                    and clip.asset_id
                    and clip.asset_id == next_clip.asset_id
                )
                clip_item.set_right_seam_is_same_source(is_same_source_cut)

            self._sync_roll_handles_for_track(
                track=track,
                track_top=track_top,
                track_height=track_h,
                track_index=track_index,
                active_keys=active_roll_keys,
            )

            # Position transitions
            for trans in track.transitions:
                trans_item = self._items_map.get(trans.transition_id)
                if trans_item and isinstance(trans_item, TransitionItem):
                    trans_item.set_track_height(track_h)
                    trans_item.update_track_index(track_index)
                    # TransitionItem computes the visible start from the
                    # model alignment (centered/start_on_cut/end_on_cut).
                    # Using center-duration/2 here made the card drift away
                    # from the actual render range for non-centered modes.
                    x = engine.frame_to_x(trans_item.start_frame)
                    trans_item.setPos(x, track_top)
                    min_center, max_center = self._transition_center_bounds(track, trans)
                    trans_item.set_move_bounds(min_center, max_center)

            # Position gap items
            for gap_item in self._gap_items.values():
                if gap_item.track_id == track_id:
                    gap_item._track_index = track_index
                    gap_item.update_track_height(track_h)
                    x = engine.frame_to_x(gap_item.start_frame)
                    gap_item.setPos(x, track_top)

            # Position roll handles
            for roll_item in self._roll_handles.values():
                if getattr(roll_item, "track_id", None) == track_id:
                    roll_item.set_track_height(track_h)
                    roll_item.update_track_index(track_index)
                    x = engine.frame_to_x(roll_item.cut_frame)
                    roll_item.setPos(x, track_top)

        # Remove seam handles that no longer correspond to adjacent clips.
        for key, handle in list(self._roll_handles.items()):
            if key in active_roll_keys:
                continue
            self._scene.removeItem(handle)
            handle.deleteLater()
            del self._roll_handles[key]

        # P10.3: Position subtitle track headers + clips
        topmost_sub_id = self._timeline.subtitle_tracks[0].track_id if self._timeline.subtitle_tracks else None
        for track in self._timeline.subtitle_tracks:
            track_id = track.track_id
            track_top = engine.track_top(track_id)
            track_h = engine.track_height(track_id)

            # AUD-21410: reuse the per-pass ordered index (no per-track
            # all_ordered_tracks() recomputation).
            track_index = ordered_index.get(track_id, 0)

            # Position header
            header = self._track_headers.get(track_id)
            if header is not None:
                header.set_track_height(track_h)
                header.set_track_index(track_index)
                header.set_clip_count(len(track.subtitle_clips))
                if hasattr(header, "set_badge_index"):
                    badge_index = next(
                        (i for i, t in enumerate(self._timeline.subtitle_tracks)
                         if t.track_id == track_id),
                        track_index,
                    )
                    header.set_badge_index(badge_index)
                header.setPos(0, track_top)
                header._is_topmost = (track_id == topmost_sub_id)

            # Position subtitle clips
            for sub in track.subtitle_clips:
                sub_item = self._items_map.get(sub.clip_id)
                if sub_item and isinstance(sub_item, SubtitleClipItem):
                    sub_item.set_track_height(track_h)
                    # Skip if this item is being actively dragged
                    if sub_item._drag_mode != "none":
                        continue
                    sub_item.update_clip_data(
                        text=sub.text,
                        start_frame=sub.timeline_start,
                        duration_frames=sub.timeline_duration,
                        is_enabled=sub.is_enabled,
                    )
                    x = engine.frame_to_x(sub.timeline_start)
                    sub_item.setPos(x, track_top)

        # Update persistent items with new area info
        area = engine.area_layout()
        total_height = area.total_track_area.height()
        all_tracks_count = len(engine.all_ordered_tracks())
        if self._playhead_item:
            self._playhead_item.update_num_tracks(all_tracks_count)
            self._playhead_item.update_track_area_height(total_height)
        if self._snap_indicator:
            self._snap_indicator.update_num_tracks(all_tracks_count)
            self._snap_indicator.update_track_area_height(total_height)

        # Keep clip-scoped markers attached to their track when the timeline
        # is resized, split, or its track heights change. Timeline-scoped
        # markers stay anchored to the ruler.
        for marker in self._timeline.markers:
            marker_item = self._marker_items.get(marker.marker_id)
            if marker_item is not None:
                marker_item.update_target_y(self._marker_target_y(marker))

        # Pin track headers to viewport left edge
        self._pin_track_headers()

        # Update scene rect
        base_rect = engine.scene_rect()
        self._scene.setSceneRect(
            base_rect.x(), base_rect.y(),
            base_rect.width(), base_rect.height(),
        )
        self._update_range_visuals(self._timeline)
        self._sync_split_vertical_scrollbars()

    def _sync_roll_handles_for_track(
        self,
        track: EditTrack,
        track_top: float,
        track_height: int,
        track_index: int,
        active_keys: set[str],
    ) -> None:
        """Create and position roll handles for every occupied cut point.

        Roll handles are deliberately independent graphics items.  That gives
        the seam a hit target spanning both clips without changing either
        clip's normal body drag/trim hit testing.
        """
        ordered_clips: list[ClipInstance] = sorted(
            track.clips,
            key=lambda candidate: (candidate.timeline_start, candidate.clip_id),
        )
        for left_clip, right_clip in zip(ordered_clips, ordered_clips[1:]):
            if left_clip.timeline_end != right_clip.timeline_start:
                continue

            key = f"{track.track_id}:{left_clip.clip_id}:{right_clip.clip_id}"
            active_keys.add(key)
            handle = self._roll_handles.get(key)
            if handle is None:
                handle = RollHandleItem(
                    left_clip_id=left_clip.clip_id,
                    right_clip_id=right_clip.clip_id,
                    cut_frame=left_clip.timeline_end,
                    track_id=track.track_id,
                    pixels_per_frame=self._pixels_per_frame,
                    track_index=track_index,
                    track_height=track_height,
                )
                self._scene.addItem(handle)
                handle.roll_trim_started.connect(self._on_roll_trim_started)
                handle.roll_trim_dragged.connect(self._on_roll_trim_dragged)
                handle.roll_trim_finished.connect(self._on_roll_trim_finished)
                self._roll_handles[key] = handle

            handle.update_zoom(self._pixels_per_frame)
            handle.set_active_tool(self._active_tool)
            handle._is_selected = (
                self._selected_edit_edges.get(left_clip.clip_id) == "right"
                and self._selected_edit_edges.get(right_clip.clip_id) == "left"
            )
            handle.set_track_height(track_height)
            neighbors = [self._items_map.get(c.clip_id) for c in (left_clip, right_clip)]
            top_inset = max(
                (26.0 if getattr(item, "_retime_controls_visible", False) else 13.0)
                + max(0.0, getattr(item, "y", lambda: track_top)() - track_top)
                for item in neighbors
            )
            bottom_inset = 20.0 if any(getattr(item, "_retime_controls_visible", False) for item in neighbors) else 0.0
            handle.set_hit_insets(top_inset, bottom_inset)
            handle.update_track_index(track_index)
            handle.update_cut_frame(left_clip.timeline_end)
            handle.set_track_locked(bool(getattr(track, "is_locked", False)))
            handle.setPos(
                self._layout_engine.frame_to_x(left_clip.timeline_end),
                track_top,
            )

    def _sync_split_vertical_scrollbars(self) -> None:
        """Update the independent video/audio vertical scrollbar geometry and ranges."""
        if self._timeline is None:
            self._hide_split_vertical_scrollbars()
            return

        self._configure_layout_engine()
        area = self._layout_engine.area_layout()
        bar_w = self._video_v_scroll.width()
        right_x = max(0, self.viewport().width() - bar_w - 2)

        self._syncing_split_scrollbars = True
        try:
            for track_type, bar in (
                ("video", self._video_v_scroll),
                ("audio", self._audio_v_scroll),
                ("subtitle", self._subtitle_v_scroll),
            ):
                minimum, maximum, page_step = self._layout_engine.scroll_range_for_area(track_type)
                if track_type == "video":
                    current = self._layout_state.video_scroll_offset
                elif track_type == "subtitle":
                    current = self._layout_state.subtitle_scroll_offset
                else:
                    current = self._layout_state.audio_scroll_offset
                if current < minimum and track_type == "video":
                    current = maximum
                else:
                    current = max(minimum, min(maximum, current))
                if track_type == "video":
                    self._layout_state.video_scroll_offset = current
                    rect = area.video_area
                elif track_type == "subtitle":
                    self._layout_state.subtitle_scroll_offset = current
                    rect = area.subtitle_area
                else:
                    self._layout_state.audio_scroll_offset = current
                    rect = area.audio_area

                top = self.mapFromScene(QPointF(0, rect.top())).y()
                bottom = self.mapFromScene(QPointF(0, rect.bottom())).y()
                height = max(0, int(bottom - top))
                bar.setGeometry(right_x, int(top), bar_w, height)
                bar.setRange(minimum, maximum)
                bar.setSingleStep(24)
                bar.setPageStep(page_step)
                bar.setValue(current)
                can_scroll = maximum > minimum and height >= 32
                bar.setEnabled(can_scroll)
                bar.setVisible(can_scroll)
                if can_scroll:
                    bar.raise_()
        finally:
            self._syncing_split_scrollbars = False

    def _hide_split_vertical_scrollbars(self) -> None:
        """Hide split V/A/S scrollbars when there is no timeline or no overflow."""
        for bar in (self._video_v_scroll, self._audio_v_scroll, self._subtitle_v_scroll):
            bar.hide()
            bar.setEnabled(False)
            bar.setRange(0, 0)
            bar.setGeometry(-10000, -10000, 0, 0)

    def _is_video_stack_aligned_to_splitter(self, tolerance: int = 2) -> bool:
        """Return True when the video scroll state keeps V1 next to the splitter."""
        if self._timeline is None:
            return True
        self._configure_layout_engine()
        _, maximum, _ = self._layout_engine.scroll_range_for_area("video")
        current = self._layout_state.video_scroll_offset
        return current < 0 or maximum <= tolerance or current >= maximum - tolerance

    def _align_video_stack_to_splitter(self) -> None:
        """Keep the bottom of the video stack anchored above the V/A splitter."""
        if self._timeline is None:
            return
        self._configure_layout_engine()
        _, maximum, _ = self._layout_engine.scroll_range_for_area("video")
        self._layout_state.video_scroll_offset = maximum

    def _on_video_scrollbar_changed(self, value: int) -> None:
        if self._syncing_split_scrollbars:
            return
        self._layout_state.video_scroll_offset = value
        # AUD-21410: coalesce full relayouts to one per display frame while
        # the scrollbar emits per-tick updates during a drag.
        self._schedule_scroll_layout_update()

    def _on_audio_scrollbar_changed(self, value: int) -> None:
        if self._syncing_split_scrollbars:
            return
        self._layout_state.audio_scroll_offset = value
        # AUD-21410: see _schedule_scroll_layout_update.
        self._schedule_scroll_layout_update()

    def _on_subtitle_scrollbar_changed(self, value: int) -> None:
        """SUB-P1-06: scroll the subtitle cue stack independently."""
        if self._syncing_split_scrollbars:
            return
        self._layout_state.subtitle_scroll_offset = value
        # AUD-21410: see _schedule_scroll_layout_update.
        self._schedule_scroll_layout_update()

    def _schedule_scroll_layout_update(self) -> None:
        """AUD-21410: run at most one full relayout per display frame.

        The three split scrollbar handlers fire on every tick during a drag;
        a full ``_layout_track_items()`` + viewport repaint per tick makes
        scrolling janky on dense timelines.  A short single-shot timer
        collapses the burst into a single layout pass that uses the latest
        scroll offsets.
        """
        if not self._scroll_layout_timer.isActive():
            self._scroll_layout_timer.start()

    def _apply_scroll_layout_update(self) -> None:
        """Apply the coalesced scrollbar relayout (AUD-21410)."""
        if self._timeline is None:
            return
        self._layout_track_items()
        self.viewport().update()

    # ==================== Project Reference ====================

    def set_project(self, project: Any) -> None:
        """
        Set the project reference for drag operations.

        Args:
            project: EditProject instance
        """
        previous_id = getattr(self._project, "project_id", None)
        next_id = getattr(project, "project_id", None)
        if previous_id is not None and previous_id != next_id:
            if not self._interaction.is_idle:
                self._cancel_current_interaction()
            self._hide_trim_monitor()
            self._timeline_command_stacks.clear()
            self._timeline_history_snapshots.clear()
            self._project_transaction_sequences.clear()
            self._command_stack = CommandStack()
            # B09：源目的轨路由与 presentation 扩展字段按 project 上下文
            # 隔离——timeline ID 只在工程内唯一，跨工程沿用会把旧工程的
            # 禁用路由/偏好带进新工程（新工程自己的 payload 存在时由
            # 宿主 apply 完整替换）。
            self._source_patch_routes.clear()
            self._presentation_extras = {}
            self._presentation_route_extras = {}
        self._project = project
        self._update_cache_indicator()

    def _timeline_has_motion_graphic_clips(self) -> bool:
        """Whether any loaded clip plays a motion-graphic (virtual) asset.

        AUD-17614: the cache-indicator 500ms poll exists solely to observe
        the async shared scene cache turning motion-graphic ranges green.
        Disk cache completion already performs an explicit
        ``set_project`` refresh (EditController._on_render_cache_finished),
        and every edit path refreshes the strip synchronously in
        ``_execute_command`` — so timelines without motion-graphic clips
        gain nothing from polling and it becomes pure UI-thread churn.
        """
        if self._timeline is None or self._project is None:
            return False
        assets = getattr(self._project, "assets", {}) or {}
        for track in getattr(self._timeline, "all_tracks", ()):
            for clip in getattr(track, "clips", ()):
                asset = assets.get(getattr(clip, "asset_id", ""))
                if asset is not None and str(
                    getattr(asset, "media_type", "") or ""
                ) == "motion_graphic":
                    return True
        return False

    def _update_cache_indicator(self) -> None:
        """Refresh the cache strip below the timecode ruler."""
        if self._ruler_item is None:
            return
        segments = build_cache_indicator_segments(self._timeline, self._project)
        self._ruler_item.set_cache_segments(segments)
        # AUD-17614: 轮询门控——仅存在 motion-graphic 片段（异步共享场景
        # 缓存会自主转绿）时保持 500ms 轮询；其余时间线（普通视频剪辑的
        # 绝大多数）磁盘缓存完成与编辑路径均有显式刷新，停掉轮询以消除
        # 主线程上的周期性全量重建。
        if (
            self._project is not None
            and self._timeline is not None
            and self._timeline_has_motion_graphic_clips()
        ):
            if not self._cache_indicator_timer.isActive():
                self._cache_indicator_timer.start()
        else:
            self._cache_indicator_timer.stop()

    # ==================== Timeline Loading ====================

    def load_timeline(self, timeline: TimelineSequence) -> None:
        """
        Load a timeline into the view.

        Creates visual items for all tracks, clips, transitions.

        Args:
            timeline: TimelineSequence to load
        """
        if self._updating:
            return

        # F2.3：切换序列——旧素材双帧解码器不跨序列复用（切源不显示旧
        # generation：bump + 关解码器，线程保留待下次会话）。
        if not self._interaction.is_idle:
            self._cancel_current_interaction()
        self._hide_trim_monitor()
        provider = getattr(self, "_trim_frame_provider_instance", None)
        if provider is not None:
            provider.close_decoders()

        self._updating = True
        try:
            previous_id = getattr(self._timeline, "timeline_id", None)
            if previous_id != timeline.timeline_id:
                self._selected_edit_edges.clear()
            known_snapshot = (
                self._timeline if previous_id == timeline.timeline_id
                else self._timeline_history_snapshots.get(timeline.timeline_id)
            )
            if (known_snapshot is not None and known_snapshot is not timeline
                    and known_snapshot.to_dict() != timeline.to_dict()):
                # External replacement invalidates commands recorded against
                # that content. Our own execute/undo/redo publish _timeline
                # before loading and therefore preserve their ordered stack.
                self._timeline_command_stacks.pop(timeline.timeline_id, None)
                if previous_id == timeline.timeline_id:
                    self._command_stack = CommandStack()
            if previous_id is not None and previous_id != timeline.timeline_id:
                self._timeline_command_stacks[previous_id] = self._command_stack
                self._timeline_history_snapshots[previous_id] = self._timeline
            if previous_id != timeline.timeline_id:
                self._command_stack = self._timeline_command_stacks.setdefault(
                    timeline.timeline_id,
                    self._command_stack if previous_id is None else CommandStack(),
                )
            else:
                self._timeline_command_stacks[timeline.timeline_id] = self._command_stack
            self._timeline_history_snapshots[timeline.timeline_id] = timeline
            self._hide_drag_preview()
            self._subtitle_drag_session = None

            # Clear existing items
            self._clear_items()

            # Store timeline
            self._timeline = timeline
            self._provisional_timeline = False
            self._runtime_duration_frames = 0
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

            # Configure before constructing graphics items. Subtitle tracks
            # have their own compact default height; querying an unconfigured
            # engine here falls back to the video-track height for the first
            # layout pass.
            self._configure_layout_engine()
            if self._drag_preview_item is not None:
                # The proxy is persistent across timeline reloads.  A project
                # loaded after a zoom/fit operation must still use the current
                # frame-to-pixel scale before the next drag begins.
                self._drag_preview_item.set_pixels_per_frame(self._pixels_per_frame)

            # Update snap points from timeline (参考 kdenlive SnapModel)
            self._snap_manager.update_from_timeline(
                timeline,
                self._playhead_frame
            )

            # Create track headers with state
            for i, track in enumerate(timeline.video_tracks):
                header = TrackHeaderItem(
                    track.track_id,
                    track.name,
                    i,
                    track.track_type,
                    track_height=self._default_track_height(track),
                    is_muted=track.is_muted,
                    is_solo=track.is_solo,
                    is_locked=track.is_locked,
                    is_sync_locked=getattr(track, "is_sync_locked", True),
                    is_visible=track.is_visible,
                    track_role=getattr(track, "track_role", "standard"),
                )
                self._scene.addItem(header)
                self._track_headers[track.track_id] = header

                # Connect track header signals
                header.mute_changed.connect(self._on_track_mute_changed)
                header.solo_changed.connect(self._on_track_solo_changed)
                header.lock_changed.connect(self._on_track_lock_changed)
                header.sync_lock_changed.connect(self._on_track_sync_lock_changed)
                self._configure_source_patch_header(header)
                header.visibility_changed.connect(self._on_track_visibility_changed)
                header.rename_requested.connect(self._on_track_rename_requested)
                header.delete_requested.connect(self._on_track_delete_requested)
                header.add_track_requested.connect(self._on_add_track_requested)
                header.move_track_requested.connect(self._on_move_track_requested)
                header.height_changed.connect(self._on_track_height_changed)
                header.height_preset_requested.connect(self.set_track_height_preset)
                header.target_track_changed.connect(self._on_target_track_changed)  # UI-09: Target track

            for i, track in enumerate(timeline.audio_tracks):
                header = TrackHeaderItem(
                    track.track_id,
                    track.name,
                    i + len(timeline.video_tracks),
                    track.track_type,
                    track_height=self._default_track_height(track),
                    is_muted=track.is_muted,
                    is_solo=track.is_solo,
                    is_locked=track.is_locked,
                    is_sync_locked=getattr(track, "is_sync_locked", True),
                    is_visible=track.is_visible,
                    volume=track.volume,
                )
                self._scene.addItem(header)
                self._track_headers[track.track_id] = header

                # Connect track header signals
                header.mute_changed.connect(self._on_track_mute_changed)
                header.solo_changed.connect(self._on_track_solo_changed)
                header.lock_changed.connect(self._on_track_lock_changed)
                header.sync_lock_changed.connect(self._on_track_sync_lock_changed)
                self._configure_source_patch_header(header)
                header.visibility_changed.connect(self._on_track_visibility_changed)
                header.volume_changed.connect(self._on_track_volume_changed)
                header.rename_requested.connect(self._on_track_rename_requested)
                header.delete_requested.connect(self._on_track_delete_requested)
                header.add_track_requested.connect(self._on_add_track_requested)
                header.move_track_requested.connect(self._on_move_track_requested)
                header.height_changed.connect(self._on_track_height_changed)
                header.height_preset_requested.connect(self.set_track_height_preset)
                header.target_track_changed.connect(self._on_target_track_changed)  # UI-09: Target track

            # P10.3: Create subtitle track headers
            for i, track in enumerate(timeline.subtitle_tracks):
                header = TrackHeaderItem(
                    track.track_id,
                    track.name,
                    i + len(timeline.video_tracks) + len(timeline.audio_tracks),
                    track.track_type,
                    track_height=self._default_track_height(track),
                    is_muted=track.is_muted,
                    is_solo=track.is_solo,
                    is_locked=track.is_locked,
                    is_sync_locked=getattr(track, "is_sync_locked", True),
                    is_visible=track.is_visible,
                )
                self._scene.addItem(header)
                self._track_headers[track.track_id] = header

                # Caption tracks expose only visibility + lock.  Audio mute
                # and solo have no defined caption preview/deliver semantics.
                header.lock_changed.connect(self._on_track_lock_changed)
                header.sync_lock_changed.connect(self._on_track_sync_lock_changed)
                self._configure_source_patch_header(header)
                header.visibility_changed.connect(self._on_track_visibility_changed)
                header.rename_requested.connect(self._on_track_rename_requested)
                header.delete_requested.connect(self._on_track_delete_requested)
                header.add_track_requested.connect(self._on_add_track_requested)
                header.move_track_requested.connect(self._on_move_track_requested)
                header.height_changed.connect(self._on_track_height_changed)
                header.height_preset_requested.connect(self.set_track_height_preset)
                header.target_track_changed.connect(self._on_target_track_changed)
                # P10.3: subtitle-specific — add clip via right-click
                header.add_subtitle_clip_requested.connect(self._on_add_subtitle_clip_requested)
                header.subtitle_quality_requested.connect(self.subtitle_quality_requested.emit)
                header.subtitle_language_requested.connect(self.subtitle_language_requested.emit)
                header.subtitle_to_speech_requested.connect(self.subtitle_to_speech_requested.emit)
                header.subtitle_export_requested.connect(self.subtitle_export_requested.emit)
                header.caption_track_graphic_template_requested.connect(
                    self.caption_track_graphic_template_requested.emit
                )

            # Initialize target tracks (video and audio independently)
            if timeline.video_tracks:
                self._target_track_id = timeline.video_tracks[0].track_id
                first_header = self._track_headers.get(self._target_track_id)
                if first_header:
                    first_header._is_target = True
                    first_header.update()
            if timeline.audio_tracks:
                self._target_audio_track_id = timeline.audio_tracks[0].track_id
                first_audio_header = self._track_headers.get(self._target_audio_track_id)
                if first_audio_header:
                    first_audio_header._is_target = True
                    first_audio_header.update()

            # Create clip items with track state
            # P10.3: regular clips come from video+audio tracks; subtitle clips
            # are handled in a separate loop below (different item type).
            all_tracks = timeline.video_tracks + timeline.audio_tracks
            for track_index, track in enumerate(all_tracks):
                for clip in track.clips:
                    # Get asset display name from project
                    asset_name = self._get_clip_display_name(clip)

                    # Get media_type: clips on audio tracks always show as audio
                    if track.track_type == "audio":
                        media_type = "audio"
                    else:
                        media_type = self._get_asset_media_type(clip.asset_id)

                    # Get asset path and fps for waveform / thumbnail lookup
                    asset_path = self._get_asset_path(clip.asset_id)
                    asset_fps = self._get_asset_fps(clip.asset_id)
                    waveform_asset_id = self._get_waveform_asset_id(clip.asset_id)

                    item = ClipItem(
                        clip.clip_id,
                        track.track_id,
                        asset_name,
                        clip.timeline_start,
                        clip.timeline_duration,
                        self._pixels_per_frame,
                        track_index,
                        source_start=clip.source_start,
                        source_duration=clip.source_duration,
                        max_source_frames=self._get_asset_duration(clip.asset_id),
                        track_height=self._track_height_for(track),
                        is_track_locked=track.is_locked,
                        media_type=media_type,
                        linked_clip_id=self._get_valid_linked_clip_id(clip),
                        asset_id=clip.asset_id,
                        asset_path=asset_path,
                        waveform_asset_id=waveform_asset_id,
                        fps=asset_fps,
                        effects=clip.effects,
                        fade_in_duration=clip.fade_in_duration,
                        fade_out_duration=clip.fade_out_duration,
                        speed=clip.speed if isinstance(clip.speed, (int, float)) else clip.speed.speed,
                        retime_points=(
                            tuple((point.source_offset, point.speed) for point in clip.speed.speed_points)
                            if isinstance(clip.speed, SpeedParams) else ()
                        ),
                        retime_controls_visible=clip.clip_id in self._retime_control_clip_ids,
                        opacity=clip.opacity,
                        # RI-06/R17：完整状态投影——首次加载必须携带 clip
                        # 自身 enabled（此前默认 True，disabled 模型被构造成
                        # enabled Item，菜单显示 Disable、点击反而启用）
                        is_enabled=clip.is_enabled,
                        label_color=clip.label_color,
                        flag_color=getattr(clip, "flag_color", None),
                        waveform_show_channels=getattr(clip, "waveform_show_channels", False),
                    )
                    self._scene.addItem(item)
                    self._items_map[clip.clip_id] = item
                    self._configure_clip_presentation(item)
                    # 轨道音量驱动的波形显示缩放（W-gain: 视觉联动）
                    item.set_waveform_gain(getattr(track, 'volume', 1.0))

                    # Phase 6: Propagate comp clip state from ClipInstance to ClipItem
                    if hasattr(clip, 'composite_id') and clip.composite_id:
                        item._composite_id = clip.composite_id
                    if media_type == "comp_clip":
                        item._is_comp_clip = True
                    if self._project is not None and clip.asset_id:
                        asset = self._project.assets.get(clip.asset_id)
                        if asset is not None:
                            meta = asset.metadata or {}
                            comp_enabled = meta.get("comp_enabled", None)
                            comp_cache_status = meta.get("comp_cache_status", None)
                            if comp_enabled is not None:
                                item._comp_enabled = bool(comp_enabled)
                            if comp_cache_status is not None:
                                item._comp_cache_status = str(comp_cache_status)
                            # Solid Color generator: fix media_type and carry
                            # the asset's RGB so the clip paints correctly.
                            if getattr(asset, "media_type", None) == "solid":
                                item._media_type = "solid"
                                sc = getattr(asset, "solid_color", None)
                                if sc is not None:
                                    item._solid_color = sc

                    # Connect signals (live preview)
                    item.clip_clicked.connect(self._on_clip_clicked)
                    # INT-02：片段拖拽/修剪纳入 InteractionSession（Esc 取消）
                    item.interaction_started.connect(self._on_item_interaction_started)
                    item.interaction_finished.connect(self._on_item_interaction_finished)
                    item.clip_moved.connect(self._on_clip_moved)
                    item.clip_trim_left.connect(self._on_clip_trim_left)
                    item.clip_trim_right.connect(self._on_clip_trim_right)
                    item.motion_graphic_duration_changed.connect(
                        self._on_motion_graphic_duration_changed
                    )
                    item.motion_graphic_duration_change_finished.connect(
                        self._on_motion_graphic_duration_change_finished
                    )
                    item.clip_delete.connect(self._on_clip_delete)
                    item.clip_split.connect(self._on_clip_split_requested)
                    item.clip_duplicate.connect(self._on_clip_duplicate_requested)

                    # Connect command signals (sent on mouseRelease)
                    item.drag_finished.connect(self._on_drag_finished)
                    item.trim_left_finished.connect(self._on_trim_left_finished)
                    item.trim_right_finished.connect(self._on_trim_right_finished)
                    item.rate_stretch_finished.connect(self._on_rate_stretch_finished)
                    item.retime_stretch_finished.connect(self._on_retime_stretch_finished)
                    item.retime_action_requested.connect(self._on_retime_action_requested)
                    item.retime_point_dragged.connect(self._on_retime_point_dragged)
                    item.retime_point_move_finished.connect(self._on_retime_point_move_finished)
                    item.fade_end_curve_requested.connect(self._on_fade_end_curve_requested)

                    # Slip signals (P4.4)
                    item.clip_slip.connect(self._on_clip_slip)
                    item.slip_finished.connect(self._on_slip_finished)

                    # Slide signals (P4.5)
                    item.clip_slide.connect(self._on_clip_slide)
                    item.slide_finished.connect(self._on_slide_finished)

                    # Ripple trim signals (P4.2)
                    item.ripple_trim_left.connect(self._on_ripple_trim_left)
                    item.ripple_trim_right.connect(self._on_ripple_trim_right)
                    item.ripple_trim_left_finished.connect(self._on_ripple_trim_left_finished)
                    item.ripple_trim_right_finished.connect(self._on_ripple_trim_right_finished)

                    # Connect transition request signal
                    item.add_transition_requested.connect(self._on_add_transition_requested)

                    # E09: 双击片段 → Source Viewer（此前 0 接收者）
                    item.clip_double_clicked.connect(self._on_clip_double_clicked)

                    # P9.2: Connect open in composite request signal
                    item.open_in_composite_requested.connect(self._on_open_in_composite_requested)

                    # T7: Connect open in color request signal
                    item.open_in_color_requested.connect(self.open_in_color_requested.emit)

                    # Scene Cut Detection (P9): forward clip scene cut request
                    item.scene_cut_detect_requested.connect(self.scene_cut_detect_requested.emit)
                    item.speech_to_text_requested.connect(self.speech_to_text_requested.emit)

                    # P2: Label color change
                    item.label_color_change_requested.connect(self._on_label_color_change_requested)
                    item.waveform_display_mode_requested.connect(
                        self._on_waveform_display_mode_requested
                    )

                    # Enable/Disable and Link/Unlink
                    item.clip_enable_toggle_requested.connect(self._on_clip_enable_toggle)
                    item.clip_unlink_requested.connect(self._on_clip_unlink)
                    item.clip_link_requested.connect(self._on_clip_link)
                    item.numeric_trim_requested.connect(
                        self._on_clip_numeric_trim_requested)
                    # P1-02: 嵌套序列入口
                    item.create_nested_from_selection_requested.connect(
                        self._on_create_nested_menu_requested)
                    item.enter_nested_sequence_requested.connect(
                        self._on_enter_nested_clip_requested)
                    item.change_speed_dialog_requested.connect(self._on_change_clip_speed_dialog_requested)
                    item.speed_change_requested.connect(self._on_clip_speed_change)
                    item.retime_controls_toggled.connect(self._on_retime_controls_toggled)
                    item.retime_speed_point_requested.connect(self._on_retime_speed_point_requested)
                    item.freeze_frame_requested.connect(self._on_freeze_frame_requested)
                    item.retime_point_easing_requested.connect(self._on_retime_point_easing_requested)
                    item.replace_placeholder_requested.connect(self._on_replace_placeholder_requested)
                    item.audio_gain_dialog_requested.connect(self._on_audio_gain_dialog_requested)
                    item.retime_speed_points_clear_requested.connect(self._on_retime_speed_points_clear_requested)
                    item.retime_segment_speed_requested.connect(self._on_retime_segment_speed_requested)

                    # Phase 1.2: Connect fade signals
                    item.fade_in_changed.connect(self._on_fade_in_preview)
                    item.fade_out_changed.connect(self._on_fade_out_preview)
                    item.fade_finished.connect(self._on_fade_finished)

                    # Phase 6: Connect Comp Clip context menu signals
                    item.convert_to_comp_clip_requested.connect(self.convert_to_comp_clip_requested.emit)
                    item.remove_composite_binding_requested.connect(self.remove_composite_binding_requested.emit)
                    item.bypass_composite_requested.connect(self.bypass_composite_requested.emit)
                    item.reset_composite_requested.connect(self.reset_composite_requested.emit)
                    item.reveal_comp_clip_in_media_pool_requested.connect(self.reveal_comp_clip_in_media_pool_requested.emit)
                    item.render_cache_clip_requested.connect(self.render_cache_clip_requested.emit)
                    item.rerender_cache_clip_requested.connect(self.rerender_cache_clip_requested.emit)
                    item.use_shared_grade_requested.connect(self.use_shared_grade_requested.emit)
                    item.make_local_grade_copy_requested.connect(self.make_local_grade_copy_requested.emit)

                    # Phase 6.1-6.2: Connect linked group and track highlight signals
                    item.linked_group_hovered.connect(self._on_linked_group_hovered)
                    item.track_highlight_requested.connect(self._on_track_highlight_requested)

            # P10.3: Create subtitle clip items (separate from regular clips
            # because SubtitleClip uses a different item type and has no asset).
            for sub_track in timeline.subtitle_tracks:
                track_top = self._layout_engine.track_top(sub_track.track_id)
                track_h = self._track_height_for(sub_track)
                for sub in sub_track.subtitle_clips:
                    sub_item = SubtitleClipItem(
                        clip_id=sub.clip_id,
                        track_id=sub_track.track_id,
                        text=sub.text,
                        start_frame=sub.timeline_start,
                        duration_frames=sub.timeline_duration,
                        pixels_per_frame=self._pixels_per_frame,
                        track_height=track_h,
                        is_enabled=sub.is_enabled,
                        is_track_locked=sub_track.is_locked,
                        label_color=sub.label_color,
                    )
                    # Position on the timeline (X from frame, Y from track top)
                    x = self._layout_engine.frame_to_x(sub.timeline_start)
                    sub_item.setPos(x, track_top)
                    self._scene.addItem(sub_item)
                    self._items_map[sub.clip_id] = sub_item

                    # SUB-P0-04: inject live drag-clamp callback.
                    sub_item.set_drag_clamp_callback(
                        self._make_subtitle_drag_clamp_callback(sub_track.track_id, sub.clip_id)
                    )

                    # Connect subtitle clip signals
                    sub_item.subtitle_clip_clicked.connect(self._on_subtitle_clip_clicked)
                    sub_item.subtitle_clip_moved.connect(self._on_subtitle_clip_moved)
                    sub_item.subtitle_clip_drag_preview.connect(self._on_subtitle_clip_drag_preview)
                    sub_item.subtitle_clip_drag_finished.connect(self._on_subtitle_clip_drag_finished)
                    sub_item.subtitle_clip_trim_left.connect(self._on_subtitle_clip_trim_left)
                    sub_item.subtitle_clip_trim_right.connect(self._on_subtitle_clip_trim_right)
                    sub_item.subtitle_clip_trim_left_finished.connect(self._on_subtitle_clip_trim_left_finished)
                    sub_item.subtitle_clip_trim_right_finished.connect(self._on_subtitle_clip_trim_right_finished)
                    sub_item.subtitle_clip_delete.connect(self._on_subtitle_clip_delete)
                    sub_item.subtitle_clip_context_menu_requested.connect(
                        self._on_subtitle_clip_context_menu_requested
                    )
                    sub_item.subtitle_clip_edit_text_requested.connect(self._on_subtitle_clip_edit_text)

            # Create transition items
            for track_index, track in enumerate(all_tracks):
                for trans in track.transitions:
                    item = TransitionItem(
                        trans.transition_id,
                        track.track_id,
                        trans.center_frame,
                        trans.duration,
                        self._pixels_per_frame,
                        track_index,
                        trans.transition_type,
                        trans.alignment,
                        is_track_locked=track.is_locked,  # AUD-21102
                    )
                    self._scene.addItem(item)
                    self._items_map[trans.transition_id] = item

                    # Connect transition signals
                    item.transition_clicked.connect(self._on_transition_clicked)
                    item.transition_delete.connect(self._on_transition_delete)
                    # Connect trim signals
                    item.transition_trim_left.connect(self._on_transition_trim_left)
                    item.transition_trim_right.connect(self._on_transition_trim_right)
                    item.trim_left_finished.connect(self._on_transition_trim_left_finished)
                    item.trim_right_finished.connect(self._on_transition_trim_right_finished)
                    item.transition_moved.connect(self._on_transition_moved)
                    item.move_finished.connect(self._on_transition_move_finished)

            # Create marker items (Phase 5.1)
            fps = float(timeline.fps) if timeline.fps else EMPTY_TIMELINE_FPS
            for marker in timeline.markers:
                marker_item = MarkerItem(
                    marker.marker_id,
                    marker.frame,
                    marker.name,
                    marker.color,
                    self._pixels_per_frame,
                    len(all_tracks),
                    fps,
                    clip_id=marker.clip_id,
                    track_id=marker.track_id,
                    kind=getattr(marker, "kind", "standard"),
                )
                marker_item.update_target_y(self._marker_target_y(marker))
                self._scene.addItem(marker_item)
                self._marker_items[marker.marker_id] = marker_item

                # Connect marker signals
                marker_item.marker_clicked.connect(self._on_marker_clicked)
                marker_item.marker_moved.connect(self._on_marker_moved)
                marker_item.marker_move_finished.connect(self._on_marker_move_finished)
                marker_item.marker_double_clicked.connect(self._on_marker_double_clicked)
                marker_item.marker_context_menu_requested.connect(
                    self._show_marker_context_menu
                )

            # Update playhead (P1.4 - also update fps for timecode display)
            if self._playhead_item:
                self._playhead_item.update_num_tracks(len(all_tracks))
                fps = float(timeline.fps) if timeline.fps else EMPTY_TIMELINE_FPS
                self._playhead_item.update_fps(fps)

            # Update snap indicator
            if self._snap_indicator:
                self._snap_indicator.update_num_tracks(len(all_tracks))

            # Update ruler
            if self._ruler_item:
                self._ruler_item.update_fps(timeline.fps or EMPTY_TIMELINE_FPS)
                self._ruler_item.update_sample_rate(timeline.sample_rate)
                self._ruler_item.update_duration(self._effective_timeline_duration(timeline))
                self._update_ruler_viewport()
                self._update_cache_indicator()

            # Phase 6.1: Assign colors to linked groups
            self._assign_linked_group_colors()

            self._update_range_visuals(timeline)

            # Apply cumulative y positions after headers and clip items exist.
            self._layout_track_items()

            # Update scene rect
            self._update_scene_rect()

            # Initialize clip visibility (new: visibility culling optimization)
            self._update_clip_visibility()

            # Update gap indicators (Phase 3.3)
            self._update_gap_indicators()

            # Loading a sequence starts the editing viewport at frame zero.
            # The deferred pass also covers direct TimelineView callers that
            # do not go through TimelinePanel.load_timeline().
            self.scroll_to_timeline_start(deferred=True)

            # Re-sync the scene cut overlay with the new tracks/fps geometry.
            self._sync_scene_cut_overlay()

            logger.info(f"TimelineView loaded timeline: {timeline.name}")

        finally:
            self._updating = False

    def _clear_items(self) -> None:
        """Clear all timeline items from scene."""
        self._clear_subtitle_copy_drag_preview()

        # Stop accepting results for the old timeline before the scene is
        # mutated.  This prevents a queued thumbnail completion from touching
        # an item while it is being replaced by a model refresh.
        for item in list(self._items_map.values()):
            if isinstance(item, ClipItem):
                item.dispose()
        self._invalidate_timeline_media_requests()

        # Remove all clip and transition items
        for item in list(self._items_map.values()):
            self._scene.removeItem(item)

        # Remove track headers
        for header in list(self._track_headers.values()):
            self._scene.removeItem(header)

        # Remove gap indicators (Phase 3.3)
        for gap_item in list(self._gap_items.values()):
            self._scene.removeItem(gap_item)

        # Remove marker items (Phase 5.1)
        for marker_item in list(self._marker_items.values()):
            self._scene.removeItem(marker_item)

        # Remove roll handles (BUG-01 fix: prevent memory leak)
        for handle in list(self._roll_handles.values()):
            self._scene.removeItem(handle)

        # Clear mappings
        self._items_map.clear()
        self._track_headers.clear()
        self._gap_items.clear()
        self._marker_items.clear()
        self._roll_handles.clear()
        self._roll_preview_state = None
        self._preview_affected_item_ids.clear()

    def _invalidate_timeline_media_requests(self) -> None:
        """Invalidate thumbnail work tied to the previous timeline state."""
        self._thumb_scheduler.invalidate()
        self._pending_thumb_jobs.clear()
        self._thumb_provider.invalidate_pending_work()

    def _create_track_header_item(self, track: EditTrack, track_index: int) -> TrackHeaderItem:
        """Create, add, and connect a TrackHeaderItem for one timeline track."""
        header = TrackHeaderItem(
            track.track_id,
            track.name,
            track_index,
            track.track_type,
            track_height=self._default_track_height(track),
            is_muted=track.is_muted,
            is_solo=track.is_solo,
            is_locked=track.is_locked,
            is_sync_locked=getattr(track, "is_sync_locked", True),
            is_visible=track.is_visible,
            volume=track.volume,
        )
        self._scene.addItem(header)
        self._track_headers[track.track_id] = header

        if track.track_type != "subtitle":
            header.mute_changed.connect(self._on_track_mute_changed)
            header.solo_changed.connect(self._on_track_solo_changed)
        header.lock_changed.connect(self._on_track_lock_changed)
        header.sync_lock_changed.connect(self._on_track_sync_lock_changed)
        self._configure_source_patch_header(header)
        header.visibility_changed.connect(self._on_track_visibility_changed)
        if track.track_type == "audio":
            header.volume_changed.connect(self._on_track_volume_changed)
        header.rename_requested.connect(self._on_track_rename_requested)
        header.delete_requested.connect(self._on_track_delete_requested)
        header.add_track_requested.connect(self._on_add_track_requested)
        header.move_track_requested.connect(self._on_move_track_requested)
        header.height_changed.connect(self._on_track_height_changed)
        header.height_preset_requested.connect(self.set_track_height_preset)
        header.target_track_changed.connect(self._on_target_track_changed)
        if track.track_type == "subtitle":
            header.add_subtitle_clip_requested.connect(self._on_add_subtitle_clip_requested)
            header.subtitle_to_speech_requested.connect(self.subtitle_to_speech_requested.emit)
            header.subtitle_export_requested.connect(self.subtitle_export_requested.emit)
            header.subtitle_quality_requested.connect(self.subtitle_quality_requested.emit)
            header.subtitle_language_requested.connect(self.subtitle_language_requested.emit)

        return header

    def _sync_track_headers_incremental(self) -> None:
        """Synchronize track headers after an empty-track-only structural edit."""
        if self._timeline is None:
            return

        current_ids = {track.track_id for track in self._timeline.all_tracks}
        for track_id, header in list(self._track_headers.items()):
            if track_id not in current_ids:
                self._scene.removeItem(header)
                del self._track_headers[track_id]

        all_tracks = (
            list(self._timeline.video_tracks)
            + list(self._timeline.audio_tracks)
            + list(self._timeline.subtitle_tracks)
        )
        for index, track in enumerate(all_tracks):
            header = self._track_headers.get(track.track_id)
            if header is None:
                self._create_track_header_item(track, index)
                continue
            header._track_name = track.name
            header._track_type = track.track_type
            header.set_muted(track.is_muted)
            header.set_solo(track.is_solo)
            header.set_locked(track.is_locked)
            header.set_visible(track.is_visible)
            if track.track_type == "audio":
                header.set_volume(track.volume)

        if self._target_track_id not in {track.track_id for track in self._timeline.video_tracks}:
            self._target_track_id = self._timeline.video_tracks[0].track_id if self._timeline.video_tracks else None
        if self._target_audio_track_id not in {track.track_id for track in self._timeline.audio_tracks}:
            self._target_audio_track_id = self._timeline.audio_tracks[0].track_id if self._timeline.audio_tracks else None

    def _update_scene_rect(self) -> None:
        """Update scene rectangle based on timeline content."""
        if self._timeline is None:
            return

        duration = self._effective_timeline_duration()

        # Scene width: header + timeline duration at current zoom
        width = TRACK_HEADER_WIDTH + duration * self._pixels_per_frame + 1000  # Extra space

        # Scene height: ruler + actual cumulative track heights.
        height = RULER_HEIGHT + self._total_track_area_height() + 100

        self._apply_scene_rect(0.0, 0.0, width, height)

    def _update_range_visuals(self, timeline: TimelineSequence) -> None:
        """Refresh In/Out handles and the non-interactive track highlight."""
        duration = self._effective_timeline_duration(timeline)
        if self._deliver_range_visibility is False:
            if self._range_item is not None:
                self._range_item.set_range(None, None)
            if self._range_mask_item is not None:
                self._configure_layout_engine()
                track_area = self._layout_engine.area_layout().total_track_area
                self._range_mask_item.set_range(
                    None,
                    None,
                    duration,
                    track_area.top(),
                    track_area.height(),
                )
            return

        resolved_range = resolve_timeline_in_out(
            timeline.range_start,
            timeline.range_end,
            duration,
        )
        if resolved_range is None and int(getattr(timeline, "duration_frames", 0) or 0) <= 0:
            # Visual-only default for a newly created empty sequence.  Do not
            # mutate the model until the user moves a handle or marks In/Out.
            # This exposes the full black canvas as an immediately usable
            # output range while preserving the model's ``None`` semantics.
            range_start, range_end = 0, duration - 1
        else:
            range_start, range_end = resolved_range or (None, None)

        if self._range_item is not None:
            self._range_item.set_pixels_per_frame(self._pixels_per_frame)
            self._range_item.set_range(range_start, range_end)
        if self._range_mask_item is not None:
            self._configure_layout_engine()
            track_area = self._layout_engine.area_layout().total_track_area
            self._range_mask_item.set_pixels_per_frame(self._pixels_per_frame)
            self._range_mask_item.set_range(
                range_start,
                range_end,
                duration,
                track_area.top(),
                track_area.height(),
            )

    def set_deliver_range_visibility(self, visible: Optional[bool]) -> None:
        """Set a temporary Deliver-only range-bar visibility override."""
        self._deliver_range_visibility = visible
        if self._timeline is not None:
            self._update_range_visuals(self._timeline)

    def _update_ruler_viewport(self) -> None:
        """Propagate current visible area width to the ruler so its boundingRect
        always covers the full viewport (even when scrolled past content)."""
        if self._ruler_item is None:
            return
        scroll_x = self.horizontalScrollBar().value()
        needed = scroll_x + self.viewport().width() - TRACK_HEADER_WIDTH
        self._ruler_item.set_viewport_width(max(0.0, needed))

    def scroll_to_timeline_start(self, deferred: bool = False) -> None:
        """Align the viewport and scrollbar with timeline frame zero.

        ``QGraphicsView`` can retain a non-zero horizontal offset while its
        scene and viewport are being created.  A deferred second pass is used
        by the initial project load, after Qt has finished applying scrollbar
        ranges for the new scene rectangle.
        """
        self._timeline_start_reset_pending = True

        def reset_scroll_position() -> None:
            scroll_bar = self.horizontalScrollBar()
            scroll_bar.setValue(scroll_bar.minimum())
            # If the view is still hidden, the parent dock may apply another
            # geometry/range update when it is shown.  Leave the request
            # pending so showEvent() can perform the authoritative reset.
            if not self.isVisible():
                return
            self._adjust_scrollbar_geometry()
            self._update_ruler_viewport()
            self._update_clip_visibility()
            self._timeline_start_reset_pending = False

        reset_scroll_position()
        if deferred:
            QTimer.singleShot(0, reset_scroll_position)

    # ==================== Zoom ====================

    def set_zoom(self, pixels_per_frame: float) -> None:
        """
        Set zoom level (pixels per frame).

        Args:
            pixels_per_frame: Zoom scale
        """
        if self._updating:
            return

        # Clamp to valid range
        pixels_per_frame = max(MIN_ZOOM, min(MAX_ZOOM, pixels_per_frame))

        if pixels_per_frame == self._pixels_per_frame:
            return

        self._updating = True
        try:
            self._pixels_per_frame = pixels_per_frame

            # Update layout engine zoom
            self._layout_engine.set_pixels_per_frame(pixels_per_frame)

            # Update all clip items
            for item in self._items_map.values():
                if isinstance(item, ClipItem):
                    item.update_zoom(pixels_per_frame)
                elif isinstance(item, TransitionItem):
                    item.update_zoom(pixels_per_frame)
                elif isinstance(item, SubtitleClipItem):
                    # Subtitle geometry and drag deltas are both expressed in
                    # pixels-per-frame.  Keeping this in sync with the layout
                    # engine is essential: otherwise the item is repositioned
                    # at the new zoom while its width (and drag conversion)
                    # still use the previous zoom level.
                    item.set_pixels_per_frame(pixels_per_frame)
                    # Active subtitle drags are intentionally skipped by
                    # _layout_track_items() so it does not overwrite the
                    # pointer-driven lane.  Reproject their X coordinate
                    # here, otherwise a zoom during a drag leaves position
                    # at the old scale while width has already changed.
                    if item._drag_mode != "none":
                        item.setX(self._layout_engine.frame_to_x(item.start_frame))

            # The Alt-drag ghost is intentionally outside ``_items_map`` so
            # it needs the same explicit scale update.
            if self._subtitle_copy_drag_preview_item is not None:
                preview = self._subtitle_copy_drag_preview_item
                preview.set_pixels_per_frame(
                    pixels_per_frame
                )
                preview.setX(self._layout_engine.frame_to_x(preview.start_frame))

            # Alt-copy ghosts for regular clips also need zoom updates.
            for ghost in self._alt_copy_ghosts.values():
                ghost.update_zoom(pixels_per_frame)
                ghost.setX(self._layout_engine.frame_to_x(ghost._start_frame))

            # Zoom the persistent drop footprint around its current frame.
            if self._drag_preview_item is not None:
                self._drag_preview_item.set_pixels_per_frame(pixels_per_frame)

            # Update roll handles (P4.3)
            for handle in self._roll_handles.values():
                handle.update_zoom(pixels_per_frame)

            # Update playhead
            if self._playhead_item:
                self._playhead_item.update_zoom(pixels_per_frame)

            # Update snap indicator
            if self._snap_indicator:
                self._snap_indicator.update_zoom(pixels_per_frame)

            # Update ruler
            if self._ruler_item:
                self._ruler_item.update_zoom(pixels_per_frame)
                self._update_ruler_viewport()

            if self._timeline is not None:
                self._update_range_visuals(self._timeline)

            # Update gap indicators (Phase 3.3)
            for gap_item in self._gap_items.values():
                gap_item.update_zoom(pixels_per_frame)

            # Update marker items (Phase 5.1)
            for marker_item in self._marker_items.values():
                marker_item.update_zoom(pixels_per_frame)

            self._layout_track_items()

            # Update scene rect
            self._update_scene_rect()

            # Update clip visibility (new: visibility culling optimization)
            self._update_clip_visibility()

            # Keep the scene cut overlay aligned with the new zoom
            self._sync_scene_cut_overlay()

            # Emit signal
            self.zoom_changed.emit(pixels_per_frame)

        finally:
            self._updating = False

        # _update_clip_visibility deliberately avoids running while the view
        # is mutating its layout.  Refresh once that lock is released so a
        # zoomed viewport never keeps clips hidden according to the previous
        # visible frame range.
        self._update_clip_visibility()

        # Debounce thumbnail schedule after zoom change
        self._debounce_thumbnail_schedule()

    # ------------------------------------------------------------------
    # B1: Scene cut detection candidate overlay
    # ------------------------------------------------------------------

    def set_scene_cut_candidates(self, candidates: tuple) -> None:
        """Store the latest detection candidates and refresh the overlay.

        The overlay itself is only created while it is visible; storing the
        candidates here lets a later toggle show them immediately.

        Args:
            candidates: Tuple of SceneCutCandidate from the latest analysis.
        """
        self._scene_cut_candidates = tuple(candidates or ())
        if self._scene_cut_overlay is not None:
            self._scene_cut_overlay.set_candidates(self._scene_cut_candidates)

    def is_scene_cut_overlay_visible(self) -> bool:
        """Return whether the scene cut overlay is currently shown."""
        return self._scene_cut_overlay_visible

    def set_scene_cut_overlay_visible(self, visible: bool) -> None:
        """Show or hide the scene cut candidate overlay.

        Empty candidate lists are allowed: the overlay stays registered so a
        later detection result appears without re-toggling.

        Args:
            visible: True to show candidate markers, False to hide.
        """
        visible = bool(visible)
        if visible == self._scene_cut_overlay_visible and (
            visible is False or self._scene_cut_overlay is not None
        ):
            return
        self._scene_cut_overlay_visible = visible
        if visible:
            self._sync_scene_cut_overlay()
            if self._scene_cut_overlay is not None:
                self._scene_cut_overlay.show_overlay()
        elif self._scene_cut_overlay is not None:
            self._scene_cut_overlay.hide_overlay()

    def _sync_scene_cut_overlay(self) -> None:
        """Create/refresh the overlay so it matches current view geometry."""
        if not self._scene_cut_overlay_visible:
            return
        if self._scene_cut_overlay is None:
            try:
                from src.features.edit.ui.scene_cut_timeline_overlay import (
                    create_scene_cut_overlay,
                )
            except ImportError:
                logger.warning("Scene cut overlay module unavailable", exc_info=True)
                self._scene_cut_overlay_visible = False
                return
            self._scene_cut_overlay = create_scene_cut_overlay(
                layout_engine=self._layout_engine,
                pixels_per_frame=self._pixels_per_frame,
                num_tracks=self._scene_cut_track_count(),
                fps=self._scene_cut_fps(),
            )
            self._scene.addItem(self._scene_cut_overlay)
            self._scene_cut_overlay.set_candidates(self._scene_cut_candidates)
            return
        sync = self._scene_cut_overlay
        sync.update_layout_engine(self._layout_engine)
        sync.update_zoom(self._pixels_per_frame)
        sync.update_num_tracks(self._scene_cut_track_count())
        sync.update_fps(self._scene_cut_fps())

    def _scene_cut_track_count(self) -> int:
        """Total track rows the overlay lines should span."""
        timeline = self._timeline
        if timeline is None:
            return 1
        return max(
            1,
            len(timeline.video_tracks)
            + len(timeline.audio_tracks)
            + len(timeline.subtitle_tracks),
        )

    def _scene_cut_fps(self) -> float:
        """Timeline fps used for overlay tooltips."""
        timeline = self._timeline
        fps = getattr(timeline, "fps", None) if timeline is not None else None
        try:
            value = float(fps) if fps else float(EMPTY_TIMELINE_FPS)
        except (TypeError, ValueError):
            value = float(EMPTY_TIMELINE_FPS)
        return value

    def layout_state(self) -> dict:
        """SUB-P1-06: snapshot the current layout for project persistence.

        Returns a dict (``TimelineLayoutState.to_dict()``) capturing track
        heights, scroll offsets and area ratios. Callers store it in UIState.
        """
        return self._layout_state.to_dict()

    def set_layout_state(self, data: dict | None) -> None:
        """SUB-P1-06: restore a persisted layout, then re-layout the view.

        Must be called after ``load_timeline`` so the engine knows the tracks.
        Re-applies state, reconfigures the engine and re-lays-out items so the
        restored heights/scroll take effect.
        """
        if not data:
            return
        new_state = TimelineLayoutState.from_dict(data)
        # Preserve the runtime default of bottom-aligning the video stack on
        # first layout unless the saved state explicitly chose otherwise.
        self._layout_state = new_state
        self._configure_layout_engine()
        self._layout_track_items()
        self._sync_split_vertical_scrollbars()
        self._update_scene_rect()
        self._sync_scene_cut_overlay()
        self.viewport().update()

    def zoom_in(self) -> None:
        """Zoom in by one step."""
        self.set_zoom(self._pixels_per_frame * ZOOM_STEP)

    def zoom_out(self) -> None:
        """Zoom out by one step."""
        self.set_zoom(self._pixels_per_frame / ZOOM_STEP)

    # ==================== Visibility Culling (参考 kdenlive) ====================

    def _calculate_visible_frame_range(self) -> tuple[int, int]:
        """
        Calculate current visible frame range in viewport.

        参考 kdenlive timeline2/view/qml/Clip.qml 的 visible 属性计算:
        - scrollView.firstVisibleFrame / lastVisibleFrame

        Returns:
            Tuple of (start_frame, end_frame)
        """
        scroll_x = self.horizontalScrollBar().value()
        viewport_width = self.viewport().width()

        # Convert scene X through the layout engine so the fixed track-header
        # offset is handled exactly like every clip's frame_to_x() mapping.
        # Keep a one-frame buffer on each edge to avoid culling an item on a
        # fractional-pixel boundary.
        start_frame = max(0, self._layout_engine.x_to_frame(float(scroll_x)) - 1)
        end_frame = max(
            start_frame,
            self._layout_engine.x_to_frame(float(scroll_x + viewport_width)) + 1,
        )

        return start_frame, end_frame

    def _pin_track_headers(self) -> None:
        """Pin track headers to the left edge of the viewport so they don't scroll horizontally."""
        # Get the scene X coordinate of the viewport's left edge
        origin = self.mapToScene(0, 0)
        pin_x = origin.x()
        for header in self._track_headers.values():
            if header.x() != pin_x:
                header.setX(pin_x)
        self._pin_top_timeline_items(origin.y())

    def _pin_top_timeline_items(self, scene_top: float | None = None) -> None:
        """Keep the ruler and playhead head visible during vertical scrolling."""
        if scene_top is None:
            scene_top = self.mapToScene(0, 0).y()

        if self._ruler_item is not None and self._ruler_item.y() != scene_top:
            self._ruler_item.setY(scene_top)

        if self._playhead_item is not None and self._playhead_item.y() != scene_top:
            self._playhead_item.setY(scene_top)

        if self._snap_indicator is not None and self._snap_indicator.y() != scene_top:
            self._snap_indicator.setY(scene_top)

    def _update_clip_visibility(self) -> None:
        """
        Update all clip and transition visibility based on viewport.

        参考 kdenlive 的可见性裁剪优化：
        - 只绘制可见区域内的片段
        - 提升大时间线的渲染性能
        """
        if self._updating:
            return

        visible_start, visible_end = self._calculate_visible_frame_range()

        # 更新所有片段和转场的可见性
        for item in self._items_map.values():
            if isinstance(item, ClipItem):
                # Viewport culling must not override the track-level eye
                # control.  The old order made a hidden video track visible
                # again on the next scroll/zoom refresh.
                track = self._timeline.get_track_by_id(item.track_id) if self._timeline else None
                if track is not None and not track.is_visible:
                    item.setVisible(False)
                else:
                    item.update_visibility(visible_start, visible_end)
            elif isinstance(item, TransitionItem):
                item.update_visibility(visible_start, visible_end)

        # 更新间隙指示器可见性 (Phase 3.3)
        self.update_gap_visibility(visible_start, visible_end)

        # 更新 marker items 可见性 (Phase 5.1)
        for marker_item in self._marker_items.values():
            marker_item.update_visibility(visible_start, visible_end)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep ruler width and layout engine in sync when the widget is resized."""
        super().resizeEvent(event)
        if self._timeline is None:
            self._update_empty_scene_geometry()
        vp = self.viewport()
        if vp:
            self._layout_engine.set_viewport_size(float(vp.width()), float(vp.height()))
        self._update_ruler_viewport()
        self._layout_track_items()
        self._sync_split_vertical_scrollbars()
        if self._ruler_item:
            self._ruler_item.update()
        # P0-G: Adjust horizontal scrollbar geometry to start at track header right edge
        self._adjust_scrollbar_geometry()
        if self._timeline_start_reset_pending and self.isVisible():
            QTimer.singleShot(0, self._apply_pending_timeline_start)

    def showEvent(self, event: Any) -> None:
        """Finalize the initial timeline position after the view is shown."""
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_pending_timeline_start)

    def _apply_pending_timeline_start(self) -> None:
        """Apply the initial frame-zero position after Qt finishes layout."""
        if not self.isVisible():
            return

        # The scrollbar can become visible only after the scene range is
        # recalculated.  Align its visual start after that transition too.
        self._adjust_scrollbar_geometry()
        if not self._timeline_start_reset_pending:
            return

        scroll_bar = self.horizontalScrollBar()
        scroll_bar.setValue(scroll_bar.minimum())
        self._update_ruler_viewport()
        self._update_clip_visibility()
        self._timeline_start_reset_pending = False

    def _adjust_scrollbar_geometry(self) -> None:
        """P0-G: Adjust horizontal scrollbar to start from track header right edge.

        This makes the horizontal scrollbar left edge align with the track header
        column's right edge, matching DaVinci Resolve's layout.
        """
        h_bar = self.horizontalScrollBar()
        if h_bar and h_bar.isVisible():
            geo = h_bar.geometry()
            # Move scrollbar left edge to track header right edge
            new_x = TRACK_HEADER_WIDTH
            if geo.x() != new_x:
                h_bar.setGeometry(new_x, geo.y(), self.width() - new_x, geo.height())

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        """
        Handle scroll event to update clip visibility.

        参考 kdenlive: 滚动时更新片段可见性以优化渲染
        """
        super().scrollContentsBy(dx, dy)
        # 仅在水平滚动时更新可见性（性能优化）
        if dx != 0:
            self._update_clip_visibility()
        # Pin track headers to left edge of viewport
        self._pin_track_headers()
        # Force repaint of header column area after scroll optimization
        # moves stale pixels into the pinned region
        if (dx != 0 or dy != 0) and self._track_headers:
            self.viewport().update()
        # Update ruler so it repaints for the new visible area
        self._update_ruler_viewport()
        if self._ruler_item:
            self._ruler_item.update()
        # Debounce thumbnail scheduler on scroll
        if dx != 0:
            self._debounce_thumbnail_schedule()

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Handle mouse wheel / trackpad for zoom and scrolling.

        INT-06：触控板 pixelDelta 优先（angleDelta/120 回退）；无 Ctrl 时
        x 轴增量优先做水平滚动（触控板两指横扫），y 轴映射水平（鼠标滚
        轮习惯）；Ctrl 只对非零有效 zoom delta 做指数平滑缩放并锚定鼠标
        下的时间线帧（旧行为 y=0 的横向事件走 zoom_out 分支意外缩小）。

        Behavior:
        - Ctrl + wheel: smooth zoom anchored at the cursor's frame
        - Shift + wheel: vertical scroll of the area under the cursor
        - No modifier: horizontal scroll
        """
        modifiers = event.modifiers()
        pixel = event.pixelDelta()
        angle = event.angleDelta()

        # 归一化：触控板像素增量优先；没有时用角度增量/120 折算
        def _axis(pix: int, ang: int) -> float:
            if pix != 0:
                return float(pix)
            return float(ang) / 8.0  # 角度→近似像素（120 单位≈15px）

        dx = _axis(pixel.x() if pixel is not None else 0, angle.x())
        dy = _axis(pixel.y() if pixel is not None else 0, angle.y())

        if modifiers & Qt.KeyboardModifier.ControlModifier:
            # Ctrl = 缩放。y 优先、x 兜底（触控板捏合/横滚轮）；
            # 零有效增量只 accept 不缩放（审计实测 Ctrl+横向事件意外缩小）
            zoom_units = dy if dy != 0 else dx
            if zoom_units == 0:
                event.accept()
                return
            self._zoom_anchored_at(
                event.position().toPoint(), zoom_units
            )
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            # Shift = 光标所在 V/A/字幕区域各自垂直滚动
            self._scroll_split_area_at(event.position().toPoint(), int(round(dy if dy else dx)))
        else:
            scroll_bar = self.horizontalScrollBar()
            if scroll_bar is None:
                super().wheelEvent(event)
                return
            if dx != 0:
                # 触控板水平两指：直接水平滚
                scroll_bar.setValue(int(scroll_bar.value() - dx))
            elif dy != 0:
                # 鼠标滚轮：垂直增量映射水平（上滚=时间前进）
                scroll_bar.setValue(int(scroll_bar.value() - dy))
            else:
                event.accept()
                return

    def _zoom_anchored_at(self, viewport_pos: QPoint, zoom_units: float) -> None:
        """INT-06：指数平滑缩放并保持鼠标下的帧在相同屏幕 x。

        zoom_units 为像素级增量（触控板）或 120 制角度折算值；每 360 单
        位翻一倍。锚定帧重定位通过水平滚动条补偿完成。
        """
        import time

        hbar = self.horizontalScrollBar()
        # N16/T3-08：手势窗口（0.8s）内复用起点连续锚帧，量化误差不回
        # 灌；超窗（新手势/停顿后的首次事件）重测。锚帧只从量化视图测
        # 一次，之后所有目标滚动都由缓存锚解析推导。
        now = time.monotonic()
        if (self._zoom_gesture_anchor_frame is None
                or now - self._zoom_gesture_last_t > 0.8):
            anchor_scene_x = self.mapToScene(viewport_pos).x()
            self._zoom_gesture_anchor_frame = (
                anchor_scene_x - TRACK_HEADER_WIDTH
            ) / self._pixels_per_frame
        self._zoom_gesture_last_t = now
        anchor_frame_f = self._zoom_gesture_anchor_frame

        scale = 2.0 ** (float(zoom_units) / 360.0)
        new_ppf = max(
            MIN_ZOOM, min(MAX_ZOOM, self._pixels_per_frame * scale)
        )
        if abs(new_ppf - self._pixels_per_frame) < 1e-9:
            return
        self.set_zoom(new_ppf)

        # 把锚定帧拉回光标下方：scene(anchor) = viewport_x + scroll（线
        # 性、由滚动条驱动）。目标滚动值由缓存锚帧**解析**求出——连续
        # float 直达 setValue 的 round，整数滚动条的量化残差不进入下一
        # 次计算的输入，长手势不再累计漂移。
        self._configure_layout_engine()
        target_scene_x = (
            TRACK_HEADER_WIDTH + anchor_frame_f * self._pixels_per_frame
        )
        current_scene_x = self.mapToScene(viewport_pos).x()
        if hbar is not None:
            # scene 位移 → 滚动条单位：滚动条以视图像素计（随 transform
            # 缩放）。目标由缓存锚帧**绝对**给出，本事件把量化残差一并
            # 校正（不累计）。
            view_scale = self.transform().m11() or 1.0
            delta_units = (target_scene_x - current_scene_x) * view_scale
            hbar.setValue(int(round(hbar.value() + delta_units)))

    def _scroll_split_area_at(self, pos: QPoint, wheel_delta_y: int) -> None:
        """Scroll video / audio / subtitle tracks independently by cursor position."""
        if self._timeline is None or wheel_delta_y == 0:
            return

        self._configure_layout_engine()
        scene_pos = self.mapToScene(pos)
        area = self._layout_engine.area_layout()
        if area.video_area.top() <= scene_pos.y() < area.video_area.bottom():
            bar = self._video_v_scroll
        elif area.audio_area.top() <= scene_pos.y() < area.audio_area.bottom():
            bar = self._audio_v_scroll
        elif area.subtitle_area.top() <= scene_pos.y() < area.subtitle_area.bottom():
            # SUB-P1-06: route wheel events over the subtitle lane to its own
            # scrollbar so a tall cue stack can be browsed.
            bar = self._subtitle_v_scroll
        else:
            return

        steps = max(24, abs(wheel_delta_y) // 3)
        direction = -1 if wheel_delta_y > 0 else 1
        bar.setValue(bar.value() + direction * steps)

    # ==================== Thumbnail Scheduler ====================

    def _debounce_thumbnail_schedule(self) -> None:
        """Debounce 80ms then submit thumbnail batch to scheduler.

        AUD-21403: 复用 __init__ 创建的单例 single-shot timer；对已激活
        的 single-shot 计时器 start() 即重启，防抖语义与旧实现一致。
        """
        self._scroll_debounce_timer.start()

    def _submit_viewport_thumbnails(self) -> None:
        """Refresh visible ClipItems after navigation settles.

        ClipItem's density-aware thumbnail-set pipeline is the canonical
        timeline implementation. The former scheduler also decoded individual
        frames for the same clips, doubling disk/CPU work and occasionally
        stretching a single cached frame across a filmstrip.
        """
        if self._timeline is None:
            return

        visible_clips = self._compute_visible_clip_info()
        for clip_info in visible_clips:
            clip_item = self._items_map.get(clip_info["clip_id"])
            if isinstance(clip_item, ClipItem) and clip_item.isVisible():
                clip_item.update()

    def _compute_visible_clip_info(self) -> list[dict]:
        """Build clip info list for visible clips in the current viewport."""
        if self._timeline is None:
            return []

        visible_start, visible_end = self._calculate_visible_frame_range()
        result = []
        for track in self._timeline.video_tracks:
            for clip in track.clips:
                clip_right = clip.timeline_start + clip.timeline_duration
                # Skip clips outside visible range
                if clip_right < visible_start or clip.timeline_start > visible_end:
                    continue

                # Pixel coordinates
                clip_x = clip.timeline_start * self._pixels_per_frame
                clip_w = clip.timeline_duration * self._pixels_per_frame
                vis_px_start = max(clip_x, visible_start * self._pixels_per_frame)
                vis_px_end = min(clip_x + clip_w, visible_end * self._pixels_per_frame)

                asset_path = self._get_asset_path(clip.asset_id)
                if not asset_path:
                    continue

                result.append({
                    "asset_id": clip.asset_id,
                    "file_path": asset_path,
                    "clip_id": clip.clip_id,
                    "source_start": clip.source_start,
                    "source_duration": clip.source_duration,
                    "visible_px_start": int(vis_px_start),
                    "visible_px_end": int(vis_px_end),
                    "mode": "filmstrip",
                })
        return result

    def _on_thumbnail_batch_ready(self, jobs) -> None:
        """Scheduler batch callback — dispatch to ThumbnailProvider."""
        for job in jobs:
            if not self._thumb_scheduler.is_job_valid(job):
                continue
            key = (job.file_path, job.source_frame)
            self._pending_thumb_jobs[key] = job
            self._thumb_provider.request_frame_pixmap_async(
                asset_id=job.asset_id,
                file_path=job.file_path,
                frame_number=job.source_frame,
                total_frames=job.source_frame + 1,
                target_height=job.target_height,
            )

    def _on_frame_thumbnail_ready(self, file_path: str, frame_index: int, pixmap) -> None:
        """Handle async thumbnail decode result from ThumbnailProvider signal."""
        key = (file_path, frame_index)
        job = self._pending_thumb_jobs.pop(key, None)
        if job is None:
            return
        if not self._thumb_scheduler.is_job_valid(job):
            return
        if pixmap is not None:
            self._thumb_scheduler.cache_thumbnail(
                job.asset_id, job.source_frame, job.target_height, pixmap,
            )
            clip_item = self._items_map.get(job.clip_id)
            if clip_item is not None:
                clip_item.update()

    def set_playback_active_for_thumbnails(self, active: bool) -> None:
        """Give playback priority over queued and running thumbnail decoding."""
        self._thumb_scheduler.set_playback_active(active)
        self._thumb_provider.set_playback_active(active)
        if active:
            if self._scroll_debounce_timer is not None:
                self._scroll_debounce_timer.stop()
            self._pending_thumb_jobs.clear()
            return

        # Resume lazily after playback stops; repainting visible items lets
        # their density-aware requests rebuild only what is currently needed.
        self._debounce_thumbnail_schedule()
        for item in self._items_map.values():
            if isinstance(item, ClipItem) and item.isVisible():
                item.update()

    def get_scheduler_cached_thumbnail(self, asset_id: str, source_frame: int, height: int):
        """Get cached thumbnail from scheduler (used by ClipItem as L2 cache)."""
        return self._thumb_scheduler.get_cached_thumbnail(asset_id, source_frame, height)

    # ==================== Playhead ====================

    def set_playhead(self, frame: int) -> None:
        """
        Set playhead position.

        Args:
            frame: Frame number
        """
        if self._updating:
            playback_trace("timeline_view.set_playhead_skip_updating", frame=frame)
            return

        frame = max(0, int(frame))
        self._extend_runtime_duration(frame)

        self._updating = True
        try:
            old_frame = self._playhead_frame
            self._playhead_frame = frame

            if self._playhead_item:
                old_scene_x = self._playhead_item.scenePos().x()
                self._playhead_item.set_frame(frame)
                new_scene_x = self._playhead_item.scenePos().x()
                if old_scene_x != new_scene_x:
                    self._invalidate_playhead_strips(old_scene_x, new_scene_x)

            # Update snap points for new playhead position (参考 kdenlive SnapModel)
            if self._timeline is not None:
                self._snap_manager.update_playhead(frame)

            playback_trace("timeline_view.playhead_changed_emit", old_frame=old_frame, frame=frame)
            self.playhead_changed.emit(frame)

        finally:
            self._updating = False

    # ==================== Selection ====================

    def set_selection(self, selection: TimelineSelection) -> None:
        """
        Set selection state.

        Args:
            selection: New selection
        """
        if self._updating:
            return

        if not selection.items:
            self._set_selected_edit_edges({})

        # P10.3: SubtitleClipItem is also a selectable timeline item
        # (imported at module top)

        self._updating = True
        try:
            # Clear previous selection visuals
            for item in self._items_map.values():
                if isinstance(item, ClipItem):
                    item.set_selected(False)
                    item.set_linked_to_selection(False)
                elif isinstance(item, TransitionItem):
                    item.set_selected(False)
                elif isinstance(item, SubtitleClipItem):
                    item.set_selected_state(False)

            # Apply new selection visuals
            for clip_id in selection.selected_clip_ids:
                item = self._items_map.get(clip_id)
                if item and isinstance(item, ClipItem):
                    item.set_selected(True)
                elif item and isinstance(item, SubtitleClipItem):
                    item.set_selected_state(True)

            # P2-03: light up every linked group that the selection touches.
            # While either member of a reciprocal pair is selected, both
            # borders glow — the user sees which clips an edit will carry
            # (move/delete/trim) before performing it.
            for clip_id in selection.selected_clip_ids:
                clip = (
                    self._timeline.get_clip_by_id(clip_id)
                    if self._timeline is not None else None
                )
                partner = (
                    self._get_reciprocal_linked_clip(clip) if clip is not None else None
                )
                if partner is None or not self._effective_linked_selection():
                    continue
                for member_id in (clip.clip_id, partner.clip_id):
                    item = self._items_map.get(member_id)
                    if isinstance(item, ClipItem):
                        item.set_selected(True)
                        item.set_linked_to_selection(True)

            for trans_id in selection.selected_transition_ids:
                item = self._items_map.get(trans_id)
                if item and isinstance(item, TransitionItem):
                    item.set_selected(True)

            self._selection = selection
            self.selection_changed.emit(selection)

        finally:
            self._updating = False

    def get_selection_diagnostics(self) -> "SelectionDiagnostics":
        """Summarize why an edit would carry other objects (P2-03).

        Programmatic Selection Inspector: for each selected clip, report
        its reciprocal linked partner, whether edits move them together
        (linked selection toggle), and whether its track is locked. This
        answers "why is this clip being driven?" without inspecting
        internal state.
        """
        entries: list[SelectionDiagnosticEntry] = []
        if self._timeline is not None:
            for clip_id in self._selection.selected_clip_ids:
                clip = self._timeline.get_clip_by_id(clip_id)
                if clip is None:
                    continue
                track = self._timeline.get_track_by_id(clip.track_id)
                partner = self._get_reciprocal_linked_clip(clip)
                entries.append(
                    SelectionDiagnosticEntry(
                        clip_id=clip.clip_id,
                        track_id=clip.track_id,
                        track_kind=getattr(track, "track_type", "video"),
                        track_locked=bool(getattr(track, "is_locked", False)),
                        linked_clip_id=partner.clip_id if partner else None,
                        linked_track_id=partner.track_id if partner else None,
                        moves_together=(
                            partner is not None and self._linked_selection_enabled
                        ),
                    )
                )
        return SelectionDiagnostics(
            entries=tuple(entries),
            linked_selection_enabled=self._linked_selection_enabled,
        )

    def _effective_linked_selection(self) -> bool:
        """Option-click selects/edits one side without altering the global toggle."""
        return self._linked_selection_enabled and not self._independent_selection

    def _set_selected_edit_edges(self, edges: Mapping[str, str]) -> None:
        """Project selected edit points independently of whole-clip selection."""
        self._selected_edit_edges = dict(edges)
        for clip_id, item in self._items_map.items():
            if isinstance(item, ClipItem):
                item._selected_edit_edge = edges.get(clip_id)
                item.update()
        for handle in self._roll_handles.values():
            handle._is_selected = (
                edges.get(handle.left_clip_id) == "right"
                and edges.get(handle.right_clip_id) == "left"
            )
            handle.update()

    def _on_clip_clicked(self, clip_id: str, track_id: str) -> None:
        """Select a linked editing unit; Option-click temporarily isolates a side."""
        if self._updating or self._timeline is None:
            return
        if not (self._interaction.is_dragging and self._interaction.primary_clip_id == clip_id):
            self._set_selected_edit_edges({})
        modifiers = QApplication.keyboardModifiers()
        self._independent_selection = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        clip = self._timeline.get_clip_by_id(clip_id)
        partner = self._get_reciprocal_linked_clip(clip)
        members = [(clip_id, track_id)]
        if self._effective_linked_selection() and partner is not None:
            members.append((partner.clip_id, partner.track_id))
        additive = bool(modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier))
        if additive:
            selection = self._selection
            remove = all(selection.contains_clip(cid) for cid, _tid in members)
            for cid, tid in members:
                if remove or not selection.contains_clip(cid):
                    selection = selection.toggle_clip(cid, tid, self._timeline.timeline_id)
        elif (not self._independent_selection and self._selection.is_multi
              and self._selection.contains_clip(clip_id)):
            selection = self._selection
        else:
            selection = TimelineSelection()
            for cid, tid in members:
                selection = selection.with_clip_selected(cid, tid, self._timeline.timeline_id, replace=False)
        if selection.contains_clip(clip_id):
            selection = selection.with_clip_selected(clip_id, track_id, self._timeline.timeline_id, replace=False)
        self.set_selection(selection)

    # ==================== P10.3: Subtitle Clip Handlers ====================

    def _on_add_subtitle_clip_requested(self, track_id: str) -> None:
        """Handle "Add Subtitle Clip" from subtitle track header context menu.

        Creates a new subtitle clip at the current playhead position. The
        command layer reads the default duration from config.
        """
        if self._timeline is None:
            return
        # Use playhead as the insertion point
        self.add_subtitle_clip_requested.emit(track_id, self._playhead_frame)

    def contextMenuEvent(self, event: Any) -> None:
        """Add a subtitle cue at the clicked frame from any subtitle lane.

        Track headers retain their own context menu.  This handler applies
        only to the timeline content area, including empty parts of a subtitle
        lane, which makes creating a cue at a precise timeline position fast.
        """
        if self._timeline is None or event.pos().x() < TRACK_HEADER_WIDTH:
            super().contextMenuEvent(event)
            return

        scene_pos = self.mapToScene(event.pos())
        track_id, area = self._layout_engine.track_at_y_with_area(scene_pos.y())
        if area == "video" and track_id is not None and self._timeline is not None:
            track = self._timeline.get_track_by_id(track_id)
            if track is not None and track.track_type == "video":
                clicked = self.itemAt(event.pos())
                while clicked is not None and not isinstance(clicked, ClipItem):
                    clicked = clicked.parentItem()
                from src.features.teaching_animation.host_adapters import is_motion_graphic_asset_id
                if isinstance(clicked, ClipItem) and is_motion_graphic_asset_id(clicked.asset_id):
                    from PyQt6.QtWidgets import QMenu
                    from src.shared.i18n.i18n import I18N

                    menu = QMenu(self)
                    open_action = menu.addAction(
                        I18N.tr("timeline.menu.open_motion_graphic_editor", "Open Motion Graphic Editor"))
                    selected = menu.exec(event.globalPos())
                    if selected is open_action:
                        self.teaching_animation_open_requested.emit(
                            clicked.clip_id, clicked.track_id
                        )
                    event.accept()
                    return
        if area != "subtitle" or track_id is None:
            super().contextMenuEvent(event)
            return
        item_at = self._scene.itemAt(scene_pos, self.viewportTransform())
        if isinstance(item_at, SubtitleClipItem):
            super().contextMenuEvent(event)
            return
        track = self._timeline.get_track_by_id(track_id)
        if track is None or track.track_type != "subtitle":
            super().contextMenuEvent(event)
            return

        from PyQt6.QtWidgets import QMenu
        from src.shared.i18n.i18n import I18N

        menu = QMenu(self)
        add_action = menu.addAction(
            I18N.tr("timeline.subtitle.add_clip", "Add Subtitle Clip")
        )
        add_action.setEnabled(not track.is_locked)
        selected = menu.exec(event.globalPos())
        if selected == add_action and not track.is_locked:
            self.add_subtitle_clip_requested.emit(
                track_id, self._frame_from_scene_x(scene_pos.x())
            )
        event.accept()

    def _on_subtitle_clip_clicked(self, clip_id: str, track_id: str) -> None:
        """Select a subtitle clip via the standard selection flow.

        This mirrors _on_clip_clicked so subtitle clips get the same selection
        semantics (single-click = replace, Shift/Ctrl = multi-select) and the
        selection_changed signal fires so the controller/properties panel react.
        """
        if self._updating:
            return
        if self._timeline is None:
            return

        modifiers = QApplication.keyboardModifiers()
        if modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            new_selection = self._selection.toggle_clip(
                clip_id, track_id, self._timeline.timeline_id
            )
        else:
            new_selection = self._selection.with_clip_selected(
                clip_id, track_id, self._timeline.timeline_id, replace=True
            )
        self.set_selection(new_selection)
        # Also forward so the controller can react (e.g. show subtitle editor)
        self.subtitle_clip_clicked.emit(clip_id, track_id)

    def _subtitle_snap_active(self) -> bool:
        """SUB-P1-08: snapping is on unless disabled or a modifier bypasses it.

        Shift or Ctrl held during a subtitle drag temporarily disables snap
        (matching the regular clip convention). Reads live keyboard modifiers
        because the subtitle item signals don't carry them.
        """
        if not self._snap_enabled:
            return False
        from PyQt6.QtGui import QGuiApplication
        mods = QGuiApplication.queryKeyboardModifiers()
        if mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            return False
        return True

    def _on_subtitle_clip_moved(self, clip_id: str, track_id: str, new_start: int) -> None:
        """Live drag preview — snap to nearby edges and update item position."""
        item = self._items_map.get(clip_id)
        if item is None or self._timeline is None:
            return
        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return
        # SUB-P1-08: snap the cue start to clip/marker/playhead/cue edges.
        snapped_start = new_start
        if self._subtitle_snap_active():
            snapped_start, _ = self._snap_frame(
                new_start,
                tuple(),
                show_indicator=True,
                exclude_source_ids={clip_id},
            )
            # Push the snapped value back into the active drag state.  An
            # Alt-copy must leave the source item in place, so its destination
            # frame is deliberately stored separately.
            if item._drag_copy_requested:
                item.set_drag_copy_preview_start(snapped_start)
            else:
                item._start_frame = int(snapped_start)
        else:
            if self._snap_indicator is not None:
                self._snap_indicator.hide_indicator()
        if isinstance(item, SubtitleClipItem) and item._drag_copy_requested:
            return
        x = self._layout_engine.frame_to_x(snapped_start)
        y = self._layout_engine.track_top(track_id)
        item.setPos(x, y)

    def _subtitle_target_track_at_y(self, scene_y: float) -> Optional[str]:
        """Resolve an unlocked subtitle destination track for a vertical drag."""
        if self._timeline is None:
            return None
        track_id, area = self._layout_engine.track_at_y_with_area(scene_y)
        if area != "subtitle" or track_id is None:
            return None
        track = self._timeline.get_track_by_id(track_id)
        if track is None or track.track_type != "subtitle" or track.is_locked:
            return None
        return track_id

    def _resolve_subtitle_drop_start(
        self,
        track_id: str,
        start: int,
        duration: int,
        excluded_clip_id: Optional[str] = None,
    ) -> int:
        """Find the first non-overlapping drop point on a subtitle track."""
        if self._timeline is None:
            return max(0, int(start))
        track = self._timeline.get_track_by_id(track_id)
        if track is None or track.track_type != "subtitle":
            return max(0, int(start))
        resolved = max(0, int(start))
        for _ in range(len(track.subtitle_clips) + 1):
            overlap = next((cue for cue in track.subtitle_clips
                            if cue.clip_id != excluded_clip_id
                            and resolved < cue.timeline_end
                            and resolved + duration > cue.timeline_start), None)
            if overlap is None:
                return resolved
            resolved = max(resolved, int(overlap.timeline_end))
        return resolved

    def _on_subtitle_clip_drag_preview(
        self, clip_id: str, source_track_id: str, start: int, scene_y: float,
    ) -> None:
        """Place the dragged cue over its subtitle destination lane visually."""
        item = self._items_map.get(clip_id)
        if not isinstance(item, SubtitleClipItem):
            return
        target_track_id = self._subtitle_target_track_at_y(scene_y) or source_track_id

        # A box-selected group moves as one unit.  Keep this deliberately
        # limited to same-lane movement; cross-lane subtitle moves still use
        # the existing single-cue overwrite workflow and remain unambiguous.
        if self._subtitle_drag_session is None and not item._drag_copy_requested:
            selected_subtitles = []
            for selected_id in self._selection.selected_clip_ids:
                selected_clip = self._timeline.get_subtitle_clip_by_id(selected_id) if self._timeline else None
                owner = (
                    self._timeline.find_subtitle_clip_owner_track(selected_id)
                    if self._timeline and selected_clip is not None
                    else None
                )
                if (
                    selected_clip is not None
                    and owner is not None
                    and owner.track_id == source_track_id
                    and not owner.is_locked
                    and not selected_clip.is_edit_locked()
                ):
                    selected_subtitles.append(selected_clip)
            if any(clip.clip_id == clip_id for clip in selected_subtitles) and len(selected_subtitles) > 1:
                self._subtitle_drag_session = {
                    "primary_clip_id": clip_id,
                    "source_track_id": source_track_id,
                    "original_positions": {
                        selected_clip.clip_id: int(selected_clip.timeline_start)
                        for selected_clip in selected_subtitles
                    },
                }

        group = self._subtitle_drag_session
        if (
            group is not None
            and group.get("primary_clip_id") == clip_id
            and target_track_id == group.get("source_track_id")
        ):
            original_positions = group["original_positions"]
            delta = int(start) - int(original_positions[clip_id])
            delta = max(-min(original_positions.values()), delta)
            for selected_id, original_start in original_positions.items():
                selected_item = self._items_map.get(selected_id)
                if not isinstance(selected_item, SubtitleClipItem):
                    continue
                new_start = max(0, int(original_start) + delta)
                selected_item._start_frame = new_start
                selected_item.set_drag_target_track(group["source_track_id"])
                selected_item.setPos(
                    self._layout_engine.frame_to_x(new_start),
                    self._layout_engine.track_top(group["source_track_id"]),
                )
                selected_item.update()
            return

        # If the selected primary leaves its original lane, fall back to the
        # existing single-cue flow rather than silently changing every cue's
        # track.  The next drag can create a fresh group session.
        if group is not None and target_track_id != group.get("source_track_id"):
            self._subtitle_drag_session = None

        item.set_drag_target_track(target_track_id)
        duration = int(getattr(item, "_duration_frames", 1))
        # Alt-copy and a cross-track move remain non-overlapping. A normal
        # same-track move previews at the pointer; its overwrite command will
        # eat occupied cue spans on drop.
        if item._drag_copy_requested or target_track_id != source_track_id:
            final_start = self._resolve_subtitle_drop_start(
                target_track_id, start, duration,
            )
        else:
            final_start = max(0, int(start))
        if item._drag_copy_requested:
            item.set_drag_copy_preview_start(final_start)
            preview = self._ensure_subtitle_copy_drag_preview(item)
            preview.update_clip_data(
                text=item._text,
                start_frame=final_start,
                duration_frames=duration,
                is_enabled=item._is_enabled,
            )
            preview.set_track_height(self._layout_engine.track_height(target_track_id))
            preview.setPos(
                self._layout_engine.frame_to_x(final_start),
                self._layout_engine.track_top(target_track_id),
            )
            return
        item._start_frame = final_start
        item.setPos(
            self._layout_engine.frame_to_x(final_start),
            self._layout_engine.track_top(target_track_id),
        )

    def _on_subtitle_clip_drag_finished(self, clip_id: str, track_id: str,
                                         old_start: int, new_start: int,
                                         scene_y: float, copy_requested: bool) -> None:
        """Emit drag-finished signal so the controller can issue a command."""
        # Use the item's snapped start (may differ from the raw new_start).
        item = self._items_map.get(clip_id)
        final_start = item.drag_release_start() if isinstance(item, SubtitleClipItem) else new_start
        self._clear_subtitle_copy_drag_preview()
        if self._snap_indicator is not None:
            self._snap_indicator.hide_indicator()
        target_track_id = self._subtitle_target_track_at_y(scene_y) or track_id

        group = self._subtitle_drag_session
        if (
            group is not None
            and group.get("primary_clip_id") == clip_id
            and not copy_requested
            and target_track_id == group.get("source_track_id")
        ):
            original_positions = dict(group["original_positions"])
            self._subtitle_drag_session = None
            delta = int(final_start) - int(original_positions[clip_id])
            delta = max(-min(original_positions.values()), delta)
            if delta == 0:
                self._layout_track_items()
                return
            payload = (original_positions, delta, group["source_track_id"])
            QTimer.singleShot(
                0,
                lambda emitted_payload=payload: self._commit_subtitle_group_drag(
                    *emitted_payload
                ),
            )
            return

        has_destination_change = (
            old_start != final_start or target_track_id != track_id
        )
        # A no-op Alt drag would create a perfectly overlapping duplicate and
        # always fail model validation. Treat it as a no-op instead.
        if has_destination_change:
            # A cross-track move removes this exact QGraphicsObject from the
            # source lane and creates it in the destination. Emitting the
            # command synchronously from its mouseReleaseEvent therefore lets
            # the command delete the active event receiver before Qt has
            # finished dispatching the release. Post the commit to the next
            # event-loop turn so the item is no longer on Qt's call stack.
            payload = (
                clip_id, track_id, old_start, final_start,
                target_track_id, copy_requested,
            )
            QTimer.singleShot(
                0,
                lambda emitted_payload=payload: self.subtitle_clip_drag_finished.emit(
                    *emitted_payload
                ),
            )

    def _commit_subtitle_group_drag(
        self,
        original_positions: dict[str, int],
        delta: int,
        track_id: str,
    ) -> None:
        """Commit one undoable move for a box-selected subtitle group."""
        if self._timeline is None or not original_positions or delta == 0:
            return
        from src.features.edit.core.commands import (
            MacroCommand,
            MoveSubtitleClipCommand,
        )

        # Move in the direction of travel so a selected cue never temporarily
        # collides with another selected cue still at its old position.
        ordered_ids = sorted(
            original_positions,
            key=lambda clip_id: original_positions[clip_id],
            reverse=delta > 0,
        )
        commands = []
        for clip_id in ordered_ids:
            clip = self._timeline.get_subtitle_clip_by_id(clip_id)
            if clip is None:
                continue
            owner = self._timeline.find_subtitle_clip_owner_track(clip_id)
            if owner is None or owner.track_id != track_id:
                continue
            commands.append(MoveSubtitleClipCommand.create(
                clip_id=clip_id,
                target_track_id=track_id,
                timeline_start=max(0, original_positions[clip_id] + delta),
                overwrite_overlaps=True,
                label="Move subtitle group",
            ))
        if not commands:
            self._layout_track_items()
            return
        command = commands[0] if len(commands) == 1 else MacroCommand.create(
            f"Move {len(commands)} subtitle clips",
            commands,
        )
        try:
            self._execute_command(command)
        except Exception:
            logger.exception("Failed to move selected subtitle clips")
            self._layout_track_items()

    def _ensure_subtitle_copy_drag_preview(
        self, source: SubtitleClipItem,
    ) -> SubtitleClipItem:
        """Return the non-interactive ghost used while Alt-copying a cue."""
        preview = self._subtitle_copy_drag_preview_item
        if (
            preview is not None
            and self._subtitle_copy_drag_preview_source_id == source.clip_id
        ):
            return preview
        self._clear_subtitle_copy_drag_preview()
        preview = SubtitleClipItem(
            clip_id=source.clip_id,
            track_id=source.track_id,
            text=source._text,
            start_frame=source.start_frame,
            duration_frames=source.duration_frames,
            pixels_per_frame=self._pixels_per_frame,
            track_height=source._track_height,
            is_enabled=source._is_enabled,
            is_track_locked=False,
            label_color=source._label_color,
        )
        preview.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        preview.setAcceptHoverEvents(False)
        preview.setOpacity(0.58)
        preview.setZValue(source.zValue() + 1.0)
        self._scene.addItem(preview)
        self._subtitle_copy_drag_preview_item = preview
        self._subtitle_copy_drag_preview_source_id = source.clip_id
        return preview

    def _clear_subtitle_copy_drag_preview(self) -> None:
        """Remove the transient Alt-copy ghost without touching model state."""
        preview = self._subtitle_copy_drag_preview_item
        self._subtitle_copy_drag_preview_item = None
        self._subtitle_copy_drag_preview_source_id = None
        if preview is None:
            return
        preview.hide()
        if preview.scene() is not None:
            preview.scene().removeItem(preview)
        preview.deleteLater()

    def _create_alt_copy_ghosts(self, session) -> None:
        """Create translucent ghost ClipItems for each clip in the Alt-drag session."""
        self._clear_alt_copy_ghosts()
        for cid in session.original_positions:
            item = self._items_map.get(cid)
            if not (item and isinstance(item, ClipItem)):
                continue
            ghost = ClipItem(
                clip_id=item._clip_id,
                track_id=item._track_id,
                asset_name=item._asset_name,
                start_frame=item._start_frame,
                duration_frames=item._duration_frames,
                pixels_per_frame=item._pixels_per_frame,
                track_index=item._track_index,
                source_start=getattr(item, '_source_start', 0),
                source_duration=getattr(item, '_source_duration', 0),
                max_source_frames=getattr(item, '_max_source_frames', 0),
                track_height=item._track_height,
                is_track_locked=False,
                media_type=item._media_type,
                linked_clip_id=item._linked_clip_id,
                asset_id=item._asset_id,
                asset_path=getattr(item, '_asset_path', None),
                fps=getattr(item, '_fps', EMPTY_TIMELINE_FPS),
                color_stack_id=item._color_stack_id,
                color_shared=item._color_shared,
                fade_in_duration=getattr(item, '_fade_in_duration', 0),
                fade_out_duration=getattr(item, '_fade_out_duration', 0),
                speed=getattr(item, '_speed', 1.0),
                opacity=item._opacity,
                effects=item._effects,
                label_color=item._label_color,
                is_enabled=item._is_enabled,
            )
            ghost.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            ghost.setAcceptHoverEvents(False)
            ghost.setOpacity(0.55)
            ghost.setZValue(item.zValue() + 1.0)
            ghost.setPos(item.pos())
            self._scene.addItem(ghost)
            self._alt_copy_ghosts[cid] = ghost

    def _clear_alt_copy_ghosts(self) -> None:
        """Remove all Alt-drag copy ghosts from the scene."""
        for ghost in self._alt_copy_ghosts.values():
            ghost.hide()
            if ghost.scene() is not None:
                ghost.scene().removeItem(ghost)
            ghost.deleteLater()
        self._alt_copy_ghosts.clear()

    def _on_subtitle_clip_trim_left(self, clip_id: str, track_id: str, new_start: int) -> None:
        """Live left-trim preview — snap start edge, update item position + geometry."""
        item = self._items_map.get(clip_id)
        if item is None:
            return
        snapped_start = new_start
        if self._subtitle_snap_active():
            snapped_start, _ = self._snap_frame(
                new_start,
                tuple(),
                show_indicator=True,
                exclude_source_ids={clip_id},
            )
            # Keep end fixed: adjust duration to match the snapped start.
            end = int(getattr(item, "_drag_orig_start", 0)) + int(getattr(item, "_drag_orig_duration", 0))
            new_dur = max(1, end - int(snapped_start))
            item.prepareGeometryChange()
            item._start_frame = int(snapped_start)
            item._duration_frames = new_dur
            item.update()
        else:
            if self._snap_indicator is not None:
                self._snap_indicator.hide_indicator()
        x = self._layout_engine.frame_to_x(snapped_start)
        y = self._layout_engine.track_top(track_id)
        item.setPos(x, y)

    def _on_subtitle_clip_trim_right(self, clip_id: str, track_id: str, new_duration: int) -> None:
        """Live right-trim preview — snap end edge, update geometry."""
        item = self._items_map.get(clip_id)
        if item is None:
            return
        snapped_dur = new_duration
        if self._subtitle_snap_active():
            start = int(getattr(item, "_start_frame", 0))
            end = start + int(new_duration)
            snapped_end, _ = self._snap_frame(
                end,
                tuple(),
                show_indicator=True,
                exclude_source_ids={clip_id},
            )
            snapped_dur = max(1, int(snapped_end) - start)
            item.prepareGeometryChange()
            item._duration_frames = snapped_dur
            item.update()
        else:
            if self._snap_indicator is not None:
                self._snap_indicator.hide_indicator()

    def _on_subtitle_clip_trim_left_finished(self, clip_id: str, track_id: str,
                                              old_start: int, old_dur: int,
                                              new_start: int, new_dur: int) -> None:
        item = self._items_map.get(clip_id)
        if self._snap_indicator is not None:
            self._snap_indicator.hide_indicator()
        # Honor the snapped geometry the item now holds.
        if item is not None:
            new_start = int(getattr(item, "_start_frame", new_start))
            new_dur = int(getattr(item, "_duration_frames", new_dur))
        if old_start != new_start or old_dur != new_dur:
            self.subtitle_clip_trim_left_finished.emit(
                clip_id, track_id, old_start, old_dur, new_start, new_dur
            )

    def _on_subtitle_clip_trim_right_finished(self, clip_id: str, track_id: str,
                                               old_dur: int, new_dur: int) -> None:
        item = self._items_map.get(clip_id)
        if self._snap_indicator is not None:
            self._snap_indicator.hide_indicator()
        if item is not None:
            new_dur = int(getattr(item, "_duration_frames", new_dur))
        if old_dur != new_dur:
            self.subtitle_clip_trim_right_finished.emit(clip_id, track_id, old_dur, new_dur)

    def _on_subtitle_clip_delete(self, clip_id: str, track_id: str) -> None:
        self.subtitle_clip_delete.emit(clip_id, track_id)

    def _save_cue_as_title_template(self, clip) -> None:
        """F2.9：把字幕 cue 的当前样式保存为用户标题模板。

        样式取 cue 的 style_override（无覆盖=继承轨默认，则取轨默认
        样式），类别经五类下拉选择；保存原子写、同名拒绝并状态栏反馈。
        """
        from PyQt6.QtWidgets import QInputDialog

        from src.features.edit.core.captions.model import CaptionStyle
        from src.features.edit.core.title_templates import (
            save_title_template,
        )

        style = getattr(clip, "style_override", None)
        if style is None:
            # 无 cue 覆盖 = 继承轨默认样式（caption_metadata.default_style）
            owner = self._timeline.find_subtitle_clip_owner_track(clip.clip_id) \
                if hasattr(self._timeline, "find_subtitle_clip_owner_track") \
                else None
            style = getattr(
                getattr(owner, "caption_metadata", None),
                "default_style", None)
        if style is None:
            style = CaptionStyle()
        name, ok = QInputDialog.getText(
            self.window(),
            I18N.tr("timeline.subtitle.save_as_title_template",
                    "Save as Title Template…"),
            I18N.tr("timeline.subtitle.template_name", "Template name:"),
            text=(clip.text or "").strip().splitlines()[0][:40] if clip.text else "")
        if not ok or not name.strip():
            return
        categories = ("title", "lower_third", "callout", "credit", "social")
        labels = {
            "title": "Title", "lower_third": "Lower Third",
            "callout": "Callout", "credit": "Credits",
            "social": "Social Vertical",
        }
        category, ok = QInputDialog.getItem(
            self.window(),
            I18N.tr("timeline.subtitle.template_name", "Template name:"),
            I18N.tr("timeline.subtitle.template_category", "Category:"),
            [labels[c] for c in categories], 0, editable=False)
        if not ok:
            return
        category_key = next(
            (c for c in categories if labels[c] == category), "title")
        try:
            preset = save_title_template(
                name.strip(), style, category=category_key)
        except ValueError as exc:
            self.status_message_requested.emit(str(exc))
            return
        self.status_message_requested.emit(I18N.tr(
            "timeline.subtitle.template_saved",
            "标题模板已保存：{name}（Titles → My Templates）",
        ).format(name=preset.label))

    def _on_subtitle_clip_context_menu_requested(
        self, clip_id: str, track_id: str, screen_pos: Any
    ) -> None:
        """Show actions for a subtitle clip after a right-click.

        Deletion is forwarded through ``subtitle_clip_delete`` so the
        controller commits the existing DeleteSubtitleClipCommand.  Keyboard
        and context-menu deletion therefore share the same undo/autosave path.
        """
        if self._timeline is None:
            return

        clip = self._timeline.get_subtitle_clip_by_id(clip_id)
        track = self._timeline.get_track_by_id(track_id)
        if clip is None or track is None or track.track_type != "subtitle":
            return

        from PyQt6.QtWidgets import QMenu
        from src.shared.i18n.i18n import I18N

        editable = not track.is_locked and not clip.is_edit_locked()
        menu = QMenu(self)
        # F2.9：cue 当前样式保存为用户标题模板（跨时间线复用的模板生命
        # 周期入口；存储走 title_templates 原子写）。
        save_template_action = menu.addAction(
            I18N.tr("timeline.subtitle.save_as_title_template",
                    "Save as Title Template…"))
        save_template_action.setEnabled(editable)
        binding = self._timeline.get_caption_graphic_binding(clip_id)
        if binding is None:
            delete_action = menu.addAction(
                I18N.tr("timeline.subtitle.delete_clip", "Delete Subtitle Clip")
            )
            delete_action.setEnabled(editable)
            promote_action = None
            if hasattr(menu, "addMenu"):
                graphic_menu = menu.addMenu("Caption Graphic")
                promote_action = graphic_menu.addAction(
                    I18N.tr("timeline.menu.convert_to_caption_graphic", "Convert to Caption Graphic"))
                promote_action.setEnabled(editable)
            selected = menu.exec(screen_pos)
            if selected == save_template_action and editable:
                self._save_cue_as_title_template(clip)
            elif promote_action is not None and selected == promote_action and editable:
                self.caption_graphic_action_requested.emit(clip_id, "promote")
            elif selected == delete_action and editable:
                self.subtitle_clip_delete.emit(clip_id, track_id)
            return

        detach_action = menu.addAction(
            I18N.tr("timeline.menu.detach_caption_graphic", "Detach Caption Graphic"))
        demote_action = menu.addAction(
            I18N.tr("timeline.menu.convert_back_to_subtitle", "Convert Back to Subtitle"))
        open_composite_action = None
        if binding.composite_id:
            open_composite_action = menu.addAction(
                I18N.tr("timeline.menu.open_in_composite", "Open in Composite"))
        detach_action.setEnabled(editable)
        demote_action.setEnabled(editable)
        delete_action = menu.addAction(
            I18N.tr("timeline.subtitle.delete_clip", "Delete Subtitle Clip")
        )
        delete_action.setEnabled(editable)

        selected = menu.exec(screen_pos)
        if selected == save_template_action and editable:
            self._save_cue_as_title_template(clip)
        elif selected == detach_action and editable:
            self.caption_graphic_action_requested.emit(clip_id, "detach")
        elif selected == demote_action and editable:
            self.caption_graphic_action_requested.emit(clip_id, "demote")
        elif open_composite_action is not None and selected == open_composite_action:
            self.caption_graphic_action_requested.emit(clip_id, "open_composite")
        elif selected == delete_action and editable:
            self.subtitle_clip_delete.emit(clip_id, track_id)

    def _nudge_selected_subtitle_clips(self, delta_frames: int) -> bool:
        """SUB-P1-08: nudge selected subtitle cues by ±frames (Alt+Arrow).

        Returns True if any cue was nudged (so the caller can swallow the key),
        False when no subtitle cue is selected (fall through to playhead move).
        The whole selection is prevalidated and committed as one macro so
        adjacent cues move atomically and one Undo restores the entire nudge.
        """
        if self._timeline is None or delta_frames == 0:
            return False
        from src.features.edit.core.commands import (
            MacroCommand,
            UpdateSubtitleClipCommand,
        )

        selected: list[SubtitleClip] = []
        for item in self._items_map.values():
            if not isinstance(item, SubtitleClipItem) or not item.isSelected():
                continue
            clip_id = item._clip_id
            clip = self._timeline.get_subtitle_clip_by_id(clip_id) if hasattr(self._timeline, "get_subtitle_clip_by_id") else None
            if clip is None:
                continue
            owner = self._timeline.find_subtitle_clip_owner_track(clip_id)
            if clip.is_edit_locked() or owner is None or owner.is_locked:
                return False
            selected.append(clip)
        if not selected:
            return False

        # Clamp the group as one unit so spacing is preserved at frame zero.
        effective_delta = int(delta_frames)
        if effective_delta < 0:
            effective_delta = max(
                effective_delta,
                -min(int(clip.timeline_start) for clip in selected),
            )
        if effective_delta == 0:
            return False

        proposed_starts: dict[str, int] = {
            clip.clip_id: int(clip.timeline_start) + effective_delta
            for clip in selected
        }

        # Validate the final geometry before touching the command stack.  This
        # prevents a macro from partially applying when an unselected neighbour
        # blocks one member of the selection.
        for track in self._timeline.subtitle_tracks:
            proposed: list[tuple[int, int, str]] = sorted(
                (
                    proposed_starts.get(clip.clip_id, int(clip.timeline_start)),
                    proposed_starts.get(clip.clip_id, int(clip.timeline_start))
                    + int(clip.timeline_duration),
                    clip.clip_id,
                )
                for clip in track.subtitle_clips
            )
            for current, following in zip(proposed, proposed[1:]):
                if current[1] > following[0]:
                    return False

        # Positive moves must update right-to-left; negative moves left-to-right
        # so intermediate immutable timelines remain overlap-free.
        ordered = sorted(
            selected,
            key=lambda clip: int(clip.timeline_start),
            reverse=effective_delta > 0,
        )
        commands: list[Any] = [
            UpdateSubtitleClipCommand.create(
                clip_id=clip.clip_id,
                updates={"timeline_start": proposed_starts[clip.clip_id]},
                label="Nudge Subtitle",
            )
            for clip in ordered
        ]
        macro = MacroCommand.create("Nudge Subtitles", commands)
        self._execute_command(macro)
        return True

    def _on_subtitle_clip_edit_text(self, clip_id: str, track_id: str, new_text: str) -> None:
        """Forward text-edit (from double-click dialog) to controller as a command."""
        self.subtitle_clip_edit_text_requested.emit(clip_id, track_id, new_text)

    # ==================== Phase 6.1-6.2: Linked Group & Track Highlight ====================

    def _on_linked_group_hovered(self, clip_id: str, linked_clip_id: str, is_hovered: bool) -> None:
        """
        Handle linked group hover signal (Phase 6.1).

        参考 Kdenlive: hover 一个 clip 时，同组的所有 clip 边框同时高亮。

        Args:
            clip_id: The clip being hovered
            linked_clip_id: The linked clip ID
            is_hovered: True if entering hover, False if leaving
        """
        if self._updating:
            return

        # Set highlight state on the linked clip item
        linked_item = self._items_map.get(linked_clip_id)
        if linked_item and isinstance(linked_item, ClipItem):
            linked_item.set_group_highlighted(is_hovered)

        # Also set highlight state on the source clip (self)
        source_item = self._items_map.get(clip_id)
        if source_item and isinstance(source_item, ClipItem):
            source_item.set_group_highlighted(is_hovered)

        logger.debug(f"Linked group hover: {clip_id} -> {linked_clip_id}, hovered={is_hovered}")

    def _on_track_highlight_requested(self, track_id: str, is_highlighted: bool) -> None:
        """
        Handle track highlight request signal (Phase 6.2).

        当 hover clip 时，对应的轨道头显示高亮效果。

        Args:
            track_id: The track to highlight
            is_highlighted: True to highlight, False to clear
        """
        if self._updating:
            return

        # Set highlight state on the track header
        track_header = self._track_headers.get(track_id)
        if track_header and isinstance(track_header, TrackHeaderItem):
            track_header.set_highlighted(is_highlighted)

        logger.debug(f"Track highlight: {track_id}, highlighted={is_highlighted}")

    def _assign_linked_group_colors(self) -> None:
        """
        Assign colors to linked groups during timeline loading (Phase 6.1).

        为同一链接组分配相同的颜色索引。
        使用 LINKED_GROUP_COLORS 色盘循环分配。
        """
        from src.features.edit.ui.timeline_items import LINKED_GROUP_COLORS

        # Build a map of linked groups (clip_id -> linked_clip_id -> group)
        # Each linked pair forms a group; both clips get the same color
        linked_pairs: Dict[str, str] = {}  # clip_id -> linked_clip_id

        for clip_id, item in self._items_map.items():
            if isinstance(item, ClipItem) and item.linked_clip_id:
                linked_pairs[clip_id] = item.linked_clip_id

        # Assign colors to groups
        color_index = 0
        assigned_colors: Dict[str, int] = {}  # clip_id -> color_index

        for clip_id, linked_clip_id in linked_pairs.items():
            # Check if either clip already has a color assigned
            if clip_id in assigned_colors:
                # Use same color for linked clip
                color = assigned_colors[clip_id]
            elif linked_clip_id in assigned_colors:
                # Use same color for this clip
                color = assigned_colors[linked_clip_id]
            else:
                # Assign new color
                color = color_index % len(LINKED_GROUP_COLORS)
                color_index += 1

            assigned_colors[clip_id] = color
            assigned_colors[linked_clip_id] = color

        # Apply colors to clip items
        for clip_id, color_idx in assigned_colors.items():
            item = self._items_map.get(clip_id)
            if item and isinstance(item, ClipItem):
                item.set_linked_group_color_index(color_idx)

    # ==================== Tools ====================

    def set_tool(self, tool: EditTool) -> None:
        """
        Set active editing tool.

        If an edit interaction (trim / slip / slide / drag) is in progress
        when the tool changes — e.g. the user pressed A/T/B during a drag —
        cancel it first so the in-flight drag is not committed under the
        newly-active tool's semantics. The cancel restores previewed clip
        positions to their originals (no command is pushed).

        Args:
            tool: New tool
        """
        # Cancel any in-progress interaction before switching tools to avoid
        # the drag being finalized under the new tool's handler on mouseRelease.
        if not self._interaction.is_idle:
            self._cancel_current_interaction()

        self._active_tool = tool

        # Adjust view behavior based on tool
        if tool == EditTool.HAND:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        elif tool == EditTool.ZOOM:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            # Zoom tool uses click to zoom in, Shift+click to zoom out
        else:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)

        # Notify all clip items about tool change (for slip tool)
        for item in self._items_map.values():
            if isinstance(item, ClipItem):
                item.set_active_tool(tool)
        for handle in self._roll_handles.values():
            handle.set_active_tool(tool)

        self.tool_changed.emit(tool.value)
        logger.debug(f"Tool changed to {tool.value}")

    def _configure_clip_presentation(self, item: ClipItem) -> None:
        """Project view-only preferences onto newly created graphics items."""
        item._thumbnail_display_mode = self._thumbnail_display_mode
        item._waveforms_visible = self._waveforms_visible
        item._position_lock_enabled = self._position_lock_enabled
        item._selected_edit_edge = self._selected_edit_edges.get(item.clip_id)
        item._timeline_display_fps = float(self._timeline.fps) if self._timeline is not None else EMPTY_TIMELINE_FPS
        clip = self._timeline.get_clip_by_id(item.clip_id) if self._timeline is not None else None
        if clip is not None:
            item.set_retime_presentation(clip.speed)

    def thumbnail_display_mode(self) -> str:
        return self._thumbnail_display_mode

    def set_thumbnail_display_mode(self, mode: str) -> None:
        """Change thumbnail presentation within this view, without model writes."""
        if mode not in {"auto", "filmstrip", "start_end", "none"}:
            raise ValueError(f"Unknown timeline thumbnail mode: {mode!r}")
        if mode == self._thumbnail_display_mode:
            return
        self._thumbnail_display_mode = mode
        for item in self._items_map.values():
            if isinstance(item, ClipItem):
                item._thumbnail_display_mode = mode
                item._cached_thumbnail_set = None
                item._thumbnail_request_key = None
                item._thumbnail_pending = False
                item.update()
        self.viewport().update()

    def waveforms_visible(self) -> bool:
        return self._waveforms_visible

    def set_waveforms_visible(self, visible: bool) -> None:
        """Hide/show waveform graphics without changing audio or clip content."""
        if self._waveforms_visible == bool(visible):
            return
        self._waveforms_visible = bool(visible)
        for item in self._items_map.values():
            if isinstance(item, ClipItem):
                item._waveforms_visible = self._waveforms_visible
                item.update()
        self.viewport().update()

    def set_position_lock_enabled(self, enabled: bool) -> None:
        """Set the UI position-lock guard and cancel any gesture in flight.

        This is a view preference, not a persistent editing permission. Host
        and command-level enforcement is documented in the wiring plan.
        """
        enabled = bool(enabled)
        if self._position_lock_enabled == enabled:
            return
        if not self._interaction.is_idle:
            self._cancel_current_interaction()
        self._position_lock_enabled = enabled
        for item in self._items_map.values():
            if isinstance(item, ClipItem):
                item._position_lock_enabled = enabled
                item.update()
        self.position_lock_changed.emit(enabled)

    def is_position_lock_enabled(self) -> bool:
        return self._position_lock_enabled

    # ==================== W07: Presentation state (no commands) ====================

    def export_presentation_state(self) -> dict:
        """W07/B07：导出 view 级展示偏好（保存/恢复用；零命令副作用）。

        ``source_destination_ui``：{timeline_id: {track_type: track_id|None}}；
        值为 None 表示蓝徽章显式关闭该流，键缺席 = 无显式条目（回退
        红 target）。B07：写入 ``version``；apply 捕获的未知顶层字段与
        路由嵌套未知键原样回写，未来工程经旧版本重存不丢扩展配置。
        """
        import copy as _copy

        route_extras = self._presentation_route_extras
        timeline_ids: set[str] = {str(tid) for (tid, _t) in
                                  self._source_patch_routes}
        timeline_ids.update(route_extras)
        routes: dict[str, dict[str, Any]] = {}
        for tid in sorted(timeline_ids):
            entry = dict(_copy.deepcopy(route_extras.get(tid, {})))
            for (timeline_id, track_type), track_id in (
                    self._source_patch_routes.items()):
                if str(timeline_id) == tid:
                    entry[track_type] = track_id
            routes[tid] = entry
        result: dict[str, Any] = {
            "version": PRESENTATION_STATE_VERSION,
            "thumbnail_mode": self._thumbnail_display_mode,
            "waveforms_visible": self._waveforms_visible,
            "position_lock": self._position_lock_enabled,
            "source_destination_ui": routes,
        }
        result.update(_copy.deepcopy(self._presentation_extras))
        # 未来版本号优先于本地版本（载荷身份属于原作者）
        if "version" in self._presentation_extras:
            result["version"] = self._presentation_extras["version"]
        return result

    def apply_presentation_state(self, state: dict) -> None:
        """恢复展示偏好（零命令、零 revision；非法值安全回退）。

        恢复顺序遵循计划 §6：先展示开关，后源目的轨（徽章重投影）。
        不触发内容命令、不推进 revision；轨删除后的陈旧路由由
        ``resolve_source_destination_track_id`` 惰性清理。B07：未知的
        顶层字段与路由嵌套未知键捕获保留（export 原样回写）；高于
        当前版本的 version 标记同样保留，已知键按当前语义尽力解释。
        """
        import copy as _copy

        if not isinstance(state, dict):
            return
        known = {
            "version", "thumbnail_mode", "waveforms_visible",
            "position_lock", "source_destination_ui",
        }
        extras: dict[str, Any] = {}
        for key, value in state.items():
            if key == "version":
                if value != PRESENTATION_STATE_VERSION:
                    extras["version"] = value
                continue
            if key not in known:
                extras[key] = _copy.deepcopy(value)
        self._presentation_extras = extras
        self._presentation_route_extras = {}
        mode = state.get("thumbnail_mode")
        if isinstance(mode, str) and mode in {
                "auto", "filmstrip", "start_end", "none"}:
            self.set_thumbnail_display_mode(mode)
        waveforms = state.get("waveforms_visible")
        if isinstance(waveforms, bool):
            self.set_waveforms_visible(waveforms)
        lock = state.get("position_lock")
        if isinstance(lock, bool):
            self.set_position_lock_enabled(lock)
        routes = state.get("source_destination_ui")
        if isinstance(routes, dict):
            for timeline_id, per_type in routes.items():
                if not isinstance(per_type, dict):
                    continue
                nested_extras = {
                    key: _copy.deepcopy(value)
                    for key, value in per_type.items()
                    if key not in {"video", "audio"}
                }
                if nested_extras:
                    self._presentation_route_extras[str(timeline_id)] = (
                        nested_extras)
                for track_type, track_id in per_type.items():
                    if track_type not in {"video", "audio"}:
                        continue
                    if track_id is None or isinstance(track_id, str):
                        self._source_patch_routes[
                            (str(timeline_id), track_type)] = track_id
            for header in self._track_headers.values():
                if header.track_type in {"video", "audio"}:
                    header.set_source_patch_enabled(
                        self.source_patch_track_id(header.track_type)
                        == header.track_id)

    def set_track_height_for_type(self, track_type: str, height: int) -> None:
        """Resize a track group in one layout pass using the shared engine."""
        from src.features.edit.ui.timeline_layout import (
            MIN_VIDEO_TRACK_HEIGHT, MAX_VIDEO_TRACK_HEIGHT,
            MIN_AUDIO_TRACK_HEIGHT, MAX_AUDIO_TRACK_HEIGHT,
        )
        limits = {
            "video": (MIN_VIDEO_TRACK_HEIGHT, MAX_VIDEO_TRACK_HEIGHT),
            "audio": (MIN_AUDIO_TRACK_HEIGHT, MAX_AUDIO_TRACK_HEIGHT),
            "subtitle": (MIN_SUBTITLE_TRACK_HEIGHT, MAX_SUBTITLE_TRACK_HEIGHT),
        }
        if track_type not in limits:
            raise ValueError(f"Unknown track type: {track_type!r}")
        if self._timeline is None:
            return
        lo, hi = limits[track_type]
        clamped = max(lo, min(hi, int(height)))
        tracks = getattr(self._timeline, f"{track_type}_tracks")
        for track in tracks:
            self._layout_state.track_heights[track.track_id] = clamped
            self._layout_engine.set_track_height(track.track_id, clamped)
        self._layout_track_items()
        self.viewport().update()

    def source_patch_track_id(self, track_type: str) -> Optional[str]:
        """Paint the same effective destination the source-edit resolver uses."""
        return self.resolve_source_destination_track_id(track_type)

    def source_destination_stream_disabled(self, track_type: str) -> bool:
        """W02：该流类型是否被蓝徽章显式关闭。

        区分三种状态：显式关闭（True）、显式选中/无条目（False）。
        "无条目"（旧工程/从未设置）不是关闭——回退红 target 语义。
        """
        if self._timeline is None or track_type not in {"video", "audio"}:
            return False
        return self._source_patch_routes.get(
            (self._timeline.timeline_id, track_type), _UNSET) is None

    def resolve_source_destination_track_id(self, track_type: str) -> Optional[str]:
        """W02：解析源编辑（Insert/Overwrite/Append/拖放配对）的有效目的轨。

        显式蓝徽章选中且轨仍存在 → 该轨；显式关闭 → None（该流不落，
        配合 ``source_destination_stream_disabled`` 区分"关闭"与"无轨"）；
        无条目或轨已删（陈旧条目清除）→ 回退红 target 语义
        （``get_target_track_id``/``get_target_audio_track_id``）。
        红 target 逻辑不受蓝徽章影响（契约 D2）。
        """
        if self._timeline is None or track_type not in {"video", "audio"}:
            return None
        key = (self._timeline.timeline_id, track_type)
        if key in self._source_patch_routes:
            selected = self._source_patch_routes[key]
            if selected is None:
                return None
            tracks = getattr(self._timeline, f"{track_type}_tracks")
            if any(track.track_id == selected for track in tracks):
                return selected
            # 陈旧条目（轨已删除）：清除后回退红 target，不留悬空 ID。
            del self._source_patch_routes[key]
        if track_type == "video":
            return self.get_target_track_id()
        return self.get_target_audio_track_id()

    def _configure_source_patch_header(self, header: TrackHeaderItem) -> None:
        header.set_source_patch_enabled(self.source_patch_track_id(header.track_type) == header.track_id)
        header.source_patch_requested.connect(self._on_source_patch_requested)

    def _on_source_patch_requested(self, track_id: str, enabled: bool) -> None:
        """Update badges and expose a host request; do not alter source routing."""
        if self._timeline is None:
            return
        track = self._timeline.get_track_by_id(track_id)
        if track is None or track.track_type not in {"video", "audio"}:
            return
        self._source_patch_routes[(self._timeline.timeline_id, track.track_type)] = track_id if enabled else None
        for header in self._track_headers.values():
            if header.track_type == track.track_type:
                header.set_source_patch_enabled(enabled and header.track_id == track_id)
        self.source_patch_changed.emit(track.track_type, track_id, enabled)

    def set_snap_enabled(self, enabled: bool) -> None:
        """
        Toggle snap on/off.

        Args:
            enabled: Whether snap is enabled
        """
        self._snap_enabled = enabled
        self.snap_changed.emit(enabled)

        # Hide indicator when snap disabled
        if not enabled and self._snap_indicator:
            self._snap_indicator.hide_indicator()

    def set_snap_threshold(self, threshold_frames: int) -> None:
        """
        Set snap threshold in frames.

        The threshold determines how close a frame must be to a snap point
        before snapping occurs.

        Args:
            threshold_frames: Number of frames for snap threshold (default: 6)
        """
        self._snap_threshold_frames = max(1, min(20, threshold_frames))

    def get_snap_threshold(self) -> int:
        """Get current snap threshold in frames."""
        return self._snap_threshold_frames

    def is_snap_enabled(self) -> bool:
        """Check if snap is currently enabled."""
        return self._snap_enabled

    # ------------------------------------------------------------------
    # P1-04: Trim Monitor / 数值 trim
    # ------------------------------------------------------------------

    def _tc(self, frame: int) -> str:
        """帧号 → SMPTE 时间码字符串（随序列帧率，DF 自动判定）。"""
        if self._timeline is None:
            return ""
        from src.features.edit.core.interchange import frames_to_timecode
        from src.features.edit.core.recording_manager import is_drop_frame_rate
        fps = getattr(self._timeline, "fps", 24) or 24
        try:
            return frames_to_timecode(max(0, int(frame)), fps,
                                      is_drop_frame_rate(fps))
        except (TypeError, ValueError):
            return str(frame)

    def set_trim_monitor_host(self, host: Optional[ProgramViewerWidget]) -> None:
        """Bind a viewer presentation host; a timeline alone never opens a window."""
        if host is self._trim_monitor_host:
            return
        if not self._interaction.is_idle:
            self._cancel_current_interaction()
        self._hide_trim_monitor()
        old = self._trim_monitor_host
        if old is not None:
            old.set_trim_monitor(None)
            old.destroyed.disconnect(self._on_trim_monitor_host_destroyed)
        monitor = getattr(self, "_trim_monitor_dialog", None)
        if monitor is not None:
            monitor.setParent(self)
        self._trim_monitor_host = host
        if host is not None:
            host.destroyed.connect(self._on_trim_monitor_host_destroyed)
            if monitor is not None:
                host.set_trim_monitor(monitor)

    def _on_trim_monitor_host_destroyed(self) -> None:
        """A host's children are being deleted; never invoke it during teardown."""
        from PyQt6 import sip
        monitor = getattr(self, "_trim_monitor_dialog", None)
        if monitor is not None and not sip.isdeleted(monitor):
            # The canvas may currently live in a fullscreen window, outside
            # the host's child tree. Retire that overlay explicitly as well.
            monitor.hide()
            # Retired overlays must not clear the cache of a replacement
            # attached before this deferred deletion reaches the event loop.
            monitor.destroyed.disconnect(self._on_trim_monitor_destroyed)
            monitor.deleteLater()
        self._trim_monitor_host = None
        self._trim_monitor_dialog = None
        if not self._interaction.is_idle:
            self._cancel_current_interaction()
        self._hide_trim_monitor()

    def _on_trim_monitor_destroyed(self) -> None:
        self._trim_monitor_dialog = None

    def _trim_monitor(self) -> "TrimMonitorDialog":
        """Lazily create a viewer child, hidden when no viewer is attached."""
        monitor = getattr(self, "_trim_monitor_dialog", None)
        if monitor is None:
            from src.features.edit.ui.trim_numeric_dialog import TrimMonitorWidget
            monitor = TrimMonitorWidget(self)
            monitor.hide()
            monitor.destroyed.connect(self._on_trim_monitor_destroyed)
            # Delete an externally parented overlay when its timeline is gone.
            self.destroyed.connect(monitor.deleteLater)
            self._trim_monitor_dialog = monitor
            if self._trim_monitor_host is not None:
                self._trim_monitor_host.set_trim_monitor(monitor)
        return monitor

    def _hide_trim_monitor(self) -> None:
        monitor = getattr(self, "_trim_monitor_dialog", None)
        if monitor is not None:
            monitor.hide()
        # F2.3：会话结束——bump generation 丢弃在途双帧请求与迟到投递；
        # 并通知 scrub 管线 finish（清除快速拖拽量化步长）。
        self._invalidate_trim_frame_requests()
        last_frame = getattr(self, "_last_trim_preview_frame", None)
        if last_frame is not None:
            self._last_trim_preview_frame = None
            # Trim previews do not move the playhead. Restore its actual
            # program frame through the existing exact scrub-finish path.
            self.trim_frame_preview_finished.emit(int(self._playhead_frame))

    def _trim_frame_provider(self):
        """F2.3：惰性启动双帧供应器（会话期间常驻，close 时 stop）。"""
        provider = getattr(self, "_trim_frame_provider_instance", None)
        if provider is None:
            from src.features.edit.ui.trim_frame_provider import (
                TrimDualFrameProvider,
            )
            provider = TrimDualFrameProvider()
            provider.signals.frame_ready.connect(self._on_trim_frame_ready)
            provider.signals.decode_failed.connect(self._on_trim_frame_failed)
            provider.start()
            # 视图销毁时停线程（daemon 线程持有 self 引用，不 stop 会
            # 随视图实例泄漏常驻轮询）
            self.destroyed.connect(provider.stop)
            self._trim_frame_provider_instance = provider
            self._trim_frame_generation = 1
        return provider

    def _invalidate_trim_frame_requests(self) -> None:
        """bump generation + 清空待处理请求（取消/结束/切源）。"""
        provider = getattr(self, "_trim_frame_provider_instance", None)
        self._trim_frame_generation = \
            getattr(self, "_trim_frame_generation", 0) + 1
        if provider is not None:
            provider.invalidate()

    def _authoritative_boundary(self, clip, offset_from_clip_start: int):
        """R07：clip 起点偏移 offset 个时间线 tick 处的**显示**源帧。

        走与播放/预览/导出相同的权威映射 ``timeline_to_source_frame``
        （虚拟延伸几何覆盖越界 offset——roll/trim 拖拽查询会越过提交
        几何）。恒速/混fps/相位、reverse、速度点曲线、冻结帧全部给出
        与导出采样一致的帧；映射失败（ValueError）诚实退 None 保持
        占位，不冒充。
        """
        from dataclasses import replace as _dc_replace
        try:
            virtual = _dc_replace(
                clip, timeline_start=0,
                timeline_duration=max(1, int(offset_from_clip_start) + 1))
            return int(virtual.timeline_to_source_frame(
                int(offset_from_clip_start)))
        except (ValueError, ZeroDivisionError, OverflowError):
            return None

    def _request_trim_dual_frames(
        self, left_clip, right_clip, delta: int,
        *, left_offset: int | None = None, right_offset: int | None = None,
    ) -> None:
        """F2.3/R07：请求左右侧真实保留边界帧（权威映射 + 请求身份）。

        roll：左 = 左片新 cut 前一帧（offset = duration+delta−1），
        右 = 右片新 cut 处（offset = delta）；单边 trim/slip 由调用方
        显式给 ``left_offset``/``right_offset``。每条请求携带单调递增
        ``request_id``——同 generation 内慢请求迟到不得覆盖新位置。
        素材缺失/映射失败侧传 None 保持占位。
        """
        if self._timeline is None:
            return
        provider = self._trim_frame_provider()
        monitor = self._trim_monitor()

        def _asset_path(clip):
            if self._project is None:
                return None
            asset = self._project.assets.get(clip.asset_id)
            return getattr(asset, "path", None) if asset is not None else None

        left_frame = self._authoritative_boundary(
            left_clip, left_clip.timeline_duration + delta - 1
            if left_offset is None else left_offset)
        right_frame = self._authoritative_boundary(
            right_clip, delta if right_offset is None else right_offset)

        monitor.set_frames_pending()
        left_path = _asset_path(left_clip) if left_frame is not None else None
        right_path = _asset_path(right_clip) if right_frame is not None else None
        if left_path is None:
            monitor.set_frame_unavailable("left")
        if right_path is None:
            monitor.set_frame_unavailable("right")
        request_id = getattr(self, "_trim_frame_request_seq", 0) + 1
        self._trim_frame_request_seq = request_id
        provider.request(
            left_path,
            left_frame,
            right_path,
            right_frame,
            self._trim_frame_generation,
            request_id)

    def _on_trim_frame_ready(
        self, side: str, frame: int, image, generation: int,
        request_id: int | None = None,
    ):
        """双帧到达：generation + request_id 双重校验（旧帧不覆盖新位置）。"""
        if generation != getattr(self, "_trim_frame_generation", 0):
            return
        if (request_id is not None
                and request_id != getattr(self, "_trim_frame_request_seq", None)):
            return
        monitor = getattr(self, "_trim_monitor_dialog", None)
        if monitor is None or not monitor.isVisible():
            return
        monitor.update_frame(side, image)

    def _on_trim_frame_failed(
        self, side: str, reason: str, generation: int,
        request_id: int | None = None,
    ) -> None:
        """Keep a failed decoder from leaving the in-view panel loading forever."""
        if generation != getattr(self, "_trim_frame_generation", 0):
            return
        if (request_id is not None
                and request_id != getattr(self, "_trim_frame_request_seq", None)):
            return
        monitor = getattr(self, "_trim_monitor_dialog", None)
        if monitor is not None and monitor.isVisible():
            logger.debug("Trim preview unavailable (%s): %s", side, reason)
            monitor.set_frame_unavailable(side)

    def _show_roll_trim_monitor(self, left_clip, right_clip,
                                cut_frame: int, delta: int) -> None:
        """roll trim 的双画面上下文（TC A/B + Δ + 手柄余量 + 真实边界帧）。"""
        try:
            from src.features.edit.core.timeline_ops import clip_handle_headroom
            monitor = self._trim_monitor()
            left_item = self._items_map.get(left_clip.clip_id)
            left_max = getattr(left_item, "_max_source_frames", 0) or 0
            left_tail = clip_handle_headroom(left_clip, left_max)[1]
            right_head = max(0, int(right_clip.source_start))
            monitor.update_roll(
                self._tc(cut_frame - 1), self._tc(cut_frame + max(0, delta)),
                delta, headroom=(left_tail, right_head))
            self._last_trim_preview_frame = cut_frame + max(0, delta)
            if self._trim_monitor_host is not None and monitor.isHidden():
                monitor.show()
                monitor.raise_()
            # F2.3：请求双画面真实边界帧（latest-wins，generation 校验）
            self._request_trim_dual_frames(left_clip, right_clip, delta)
        except Exception as e:
            logger.debug("Trim monitor update skipped: %s", e)

    def _show_single_trim_monitor(
        self, clip, edge: str, new_edge: int,
    ) -> None:
        """R07①：单边 trim 监视器——被修边真实帧 + 邻片/空位。

        模式定义（与 roll 的双邻片模式相对）：

        - 左修（``edge="left"``，new_edge=新入点帧）：A 面板 = 被修片
          新入点首帧；B 面板 = 覆盖 cut−1 帧的左邻片边界帧，无邻片
          覆盖（空位）时显式占位；
        - 右修（``edge="right"``，new_edge=新出点帧）：A 面板 = 被修片
          最后保留帧（new_edge−1）；B 面板 = 覆盖 new_edge 的右邻片
          首帧，空位显式占位。

        边界帧经权威映射（``_authoritative_boundary``），与导出采样
        一致；被修侧的源窗用方向感知的 ``with_*_edge_source_values``
        （RI-07/R18 同源）。
        """
        from dataclasses import replace as _dc_replace
        from src.features.edit.core.timeline_ops import clip_handle_headroom
        if self._timeline is None:
            return
        track = self._timeline.get_track_by_id(clip.track_id)
        if track is None:
            return
        if edge == "left":
            delta = new_edge - clip.timeline_start
            if delta == 0:
                return
            new_duration = clip.timeline_end - new_edge
            if new_duration <= 0:
                return
            src_start, src_dur = clip.with_left_edge_source_values(new_edge)
            edited = _dc_replace(
                clip, timeline_start=0, timeline_duration=new_duration,
                source_start=src_start, source_duration=src_dur)
            left_offset = 0
            neighbor = next((
                c for c in track.clips
                if c.clip_id != clip.clip_id
                and c.timeline_start <= new_edge - 1 < c.timeline_end), None)
            right_offset = (
                new_edge - 1 - neighbor.timeline_start
                if neighbor is not None else None)
            tc = self._tc(new_edge)
        else:
            delta = new_edge - clip.timeline_end
            if delta == 0:
                return
            new_duration = new_edge - clip.timeline_start
            if new_duration <= 0:
                return
            src_start, src_dur = clip.with_right_edge_source_values(new_duration)
            edited = _dc_replace(
                clip, timeline_start=0, timeline_duration=new_duration,
                source_start=src_start, source_duration=src_dur)
            left_offset = new_duration - 1
            neighbor = next((
                c for c in track.clips
                if c.clip_id != clip.clip_id
                and c.timeline_start <= new_edge < c.timeline_end), None)
            right_offset = (
                new_edge - neighbor.timeline_start
                if neighbor is not None else None)
            tc = self._tc(new_edge - 1)
        try:
            monitor = self._trim_monitor()
            item = self._items_map.get(clip.clip_id)
            max_frames = getattr(item, "_max_source_frames", 0) or 0
            head, tail = clip_handle_headroom(clip, max_frames)
            monitor.update_trim(tc, delta, headroom=(head, tail))
            self._last_trim_preview_frame = new_edge
            if self._trim_monitor_host is not None and monitor.isHidden():
                monitor.show()
                monitor.raise_()
            self._request_trim_dual_frames(
                edited, neighbor if neighbor is not None else clip, delta,
                left_offset=left_offset, right_offset=right_offset)
        except Exception as e:
            logger.debug("Trim monitor update skipped: %s", e)

    def _show_slip_monitor(self, clip, delta: int) -> None:
        """R07①：slip 监视器——新源窗首末帧（同片双画面）。

        slip 只平移源窗（时间线几何不动）：A = 新窗口首帧、B = 新窗口
        末帧，Δ = 源帧位移。滑出可用范围由 slip 边界预检在预览层拒绝，
        这里不做第二次钳制。
        """
        from dataclasses import replace as _dc_replace
        from src.features.edit.core.timeline_ops import clip_handle_headroom
        if self._timeline is None or clip is None or delta == 0:
            return
        try:
            slipped = _dc_replace(
                clip, source_start=max(0, clip.source_start + delta))
            monitor = self._trim_monitor()
            item = self._items_map.get(clip.clip_id)
            max_frames = getattr(item, "_max_source_frames", 0) or 0
            head, tail = clip_handle_headroom(clip, max_frames)
            last = max(0, slipped.source_start + slipped.source_duration - 1)
            monitor.update_roll(
                self._tc(slipped.source_start), self._tc(last),
                delta, headroom=(head, tail))
            self._last_trim_preview_frame = clip.timeline_end
            if self._trim_monitor_host is not None and monitor.isHidden():
                monitor.show()
                monitor.raise_()
            self._request_trim_dual_frames(
                slipped, slipped, delta,
                left_offset=0,
                right_offset=clip.timeline_duration - 1)
        except Exception as e:
            logger.debug("Slip monitor update skipped: %s", e)

    def _on_clip_numeric_trim_requested(self, clip_id: str, track_id: str) -> None:
        """P1-04 数值 trim：对话框输入帧增量（含对齐标记），执行命令。"""
        from src.features.edit.core.timeline_ops import (
            build_numeric_trim_command,
            clip_handle_headroom,
            nearest_marker_frame,
            numeric_trim_bounds,
        )
        from src.features.edit.ui.trim_numeric_dialog import TrimNumericDialog
        if self._timeline is None:
            return
        track = self._timeline.get_track_by_id(track_id)
        clip = track.get_clip_by_id(clip_id) if track is not None else None
        if clip is None or track is None:
            return
        if self._is_track_locked(track_id):  # 审计修复：锁轨不绕过
            # AUD-21408: TimelineView has no status_bar attribute — the old
            # getattr() path never showed anything.  Emit the existing
            # status_message_requested signal like the other feedback paths.
            self.status_message_requested.emit(
                I18N.tr("timeline.track_locked", "Track is locked")
            )
            return
        max_frames = self._get_asset_duration(clip.asset_id)

        has_right = any(
            other.clip_id != clip.clip_id
            and other.timeline_start >= clip.timeline_start + clip.timeline_duration
            for other in track.clips
        )

        def _bounds_for(edge: str, mode: str):
            if mode == "roll":
                right = None
                clip_end = clip.timeline_start + clip.timeline_duration
                for other in track.clips:
                    if (other.clip_id != clip.clip_id
                            and other.timeline_start >= clip_end
                            and (right is None
                                 or other.timeline_start < right.timeline_start)):
                        right = other
                if right is None:
                    return (0, 0)
                from src.features.edit.core.linked_roll import (
                    linked_roll_bounds, linked_roll_pairs,
                )
                try:
                    pairs = linked_roll_pairs(
                        self._timeline, clip.clip_id, right.clip_id,
                        self._linked_selection_enabled)
                    return linked_roll_bounds(pairs, self._roll_clip_limit)
                except ValueError:
                    return (0, 0)
            return numeric_trim_bounds(
                track, clip, edge, max_source_frames=max_frames,
                ripple=(mode == "ripple"),
            )

        dialog = TrimNumericDialog(
            self.window(),
            bounds_for=_bounds_for,
            headroom=clip_handle_headroom(clip, max_frames),
            duration_frames=int(clip.timeline_duration),
            can_roll=has_right,
            markers_available=bool(self._timeline.markers),
        )
        if dialog.exec() != TrimNumericDialog.DialogCode.Accepted:
            return
        edge, mode, delta, align_marker = dialog.values()

        # 对齐到最近标记：目标边缘吸附到方向上最近标记帧
        if align_marker and self._timeline.markers:
            edge_frame = (
                clip.timeline_start if edge == "in"
                else clip.timeline_start + clip.timeline_duration)
            direction = 1 if delta >= 0 else -1
            marker_frame = nearest_marker_frame(
                self._timeline.markers, edge_frame, direction)
            if marker_frame is not None:
                delta = marker_frame - edge_frame

        if mode == "roll":
            from src.features.edit.core.linked_roll import build_linked_roll_command
            right = next((candidate for candidate in track.clips
                          if candidate.clip_id != clip_id
                          and candidate.timeline_start == clip.timeline_end), None)
            if right is None:
                return
            try:
                command, clamped = build_linked_roll_command(
                    self._timeline, clip_id, right.clip_id,
                    delta, self._roll_clip_limit, self._linked_selection_enabled)
            except ValueError as exc:
                logger.warning("Numeric Roll rejected: %s", exc)
                self.status_message_requested.emit(str(exc))
                return
        else:
            command, clamped = build_numeric_trim_command(
                self._timeline, track_id, clip_id,
                "left" if edge == "in" else "right", delta,
                mode=mode, max_source_frames=max_frames,
            )
        if command is None:
            return
        # 审计修复：linked 联动走 B19 域级共同边界计划（UI/service 同一
        # 语义）；linked 共同钳制失败整体拒绝
        if getattr(self, "_linked_selection_enabled", False):
            linked = self._timeline.get_reciprocal_linked_clip(clip_id)
            if linked is not None and mode != "roll":
                from src.features.edit.core.timeline_ops import (
                    build_linked_numeric_trim_command,
                )
                linked_command, _common = build_linked_numeric_trim_command(
                    self._timeline, track_id, clip_id,
                    "left" if edge == "in" else "right", clamped,
                    mode=mode, max_source_frames=max_frames,
                    linked_max_source_frames=(
                        self._get_asset_duration(linked.asset_id)),
                    mismatch="reject",
                )
                if linked_command is None:
                    self.status_message_requested.emit(I18N.tr(
                        "timeline.linked_trim_limit", "Linked clips cannot be trimmed by the same amount."))
                    return
                command = linked_command
        self._execute_command(command)

    # ------------------------------------------------------------------
    # P1-02: 嵌套序列（Nested Sequence / Compound Clip）
    # ------------------------------------------------------------------

    def _on_create_nested_menu_requested(self) -> None:
        """右键"从选择创建嵌套序列"——收集当前选择的媒体片段并上抛。

        排除字幕片段（走独立模型）、锁定轨道与陈旧选择；至少一个合法
        片段才发信号，控制器负责循环嵌套校验与事务。
        """
        if self._timeline is None:
            return
        locked = self._get_locked_track_ids()
        clip_ids: list[str] = []
        for clip_id in getattr(self._selection, "selected_clip_ids", ()) or ():
            if self._timeline.get_clip_by_id(clip_id) is None:
                continue
            item = self._items_map.get(clip_id)
            track_id = getattr(item, "_track_id", None)
            if track_id in locked:
                continue
            clip_ids.append(clip_id)
        if clip_ids:
            self.create_nested_from_selection_requested.emit(clip_ids)

    def _on_enter_nested_clip_requested(self, clip_id: str, track_id: str) -> None:
        """右键"进入嵌套序列"——解析子序列 id 并请求切换时间线。"""
        _ = track_id
        self._dispatch_enter_nested(clip_id)

    def _dispatch_enter_nested(self, clip_id: str) -> None:
        project = getattr(self, "_project", None)
        clip = self._timeline.get_clip_by_id(clip_id) if self._timeline is not None else None
        if project is None or clip is None:
            return
        asset = project.assets.get(clip.asset_id)
        child_tid = getattr(asset, "nested_timeline_id", None) if asset is not None else None
        if child_tid:
            self.enter_nested_sequence_requested.emit(child_tid, clip_id)

    def _multicam_clip_under_playhead(self) -> bool:
        """P1-01: playhead 覆盖的视频片段是否属于多机位组（素材 metadata）。"""
        if self._timeline is None:
            return False
        project = getattr(self, "_project", None)
        if project is None:
            return False
        from src.features.edit.core.multicam import multicam_group_from_asset
        frame = int(self._playhead_frame or 0)
        for track in self._timeline.video_tracks:
            for clip in track.clips:
                if clip.timeline_start <= frame < clip.timeline_end:
                    asset = project.assets.get(clip.asset_id)
                    if asset is not None and multicam_group_from_asset(asset) is not None:
                        return True
        return False

    # ------------------------------------------------------------------
    # P1-04: 键盘步进 trim（,/. 单帧步进 + 修饰键组合）
    # ------------------------------------------------------------------

    def _apply_step_trim(
        self, edge: str, delta_frames: int, *, clip_id: str | None = None,
        mode: str | None = None,
    ) -> bool:
        """键盘步进 trim：唯一选中媒体片段的入/出边缘移动 ±N 帧。

        Trim 工具 → TrimClipCommand（邻居/手柄钳制）；
        Ripple 工具 → RippleTrimCommand（后续片段平移）。
        A/V 链接开启时联动 linked 片段（一次 Undo 的宏，与鼠标 trim 一致）。

        Returns:
            True = 已执行命令；False = 不适用（无单选媒体片段/钳制为 0）。
        """
        from src.features.edit.core.timeline_ops import (
            build_numeric_trim_command,
        )
        if self._timeline is None or edge not in ("in", "out"):
            return False
        selection = getattr(self, "_selection", None)
        selected = list(getattr(selection, "selected_clip_ids", ()) or ())
        if clip_id is None and len(selected) == 2:
            first = self._timeline.get_clip_by_id(selected[0])
            partner = self._get_reciprocal_linked_clip(first)
            if partner is not None and partner.clip_id in selected:
                primary = selection.primary_item
                selected = [primary.item_id if primary is not None else first.clip_id]
        if clip_id is None and len(selected) != 1:
            return False
        clip = self._timeline.get_clip_by_id(clip_id if clip_id is not None else selected[0])
        if clip is None:
            return False
        track = None
        for candidate in self._timeline.all_tracks:
            if candidate.get_clip_by_id(clip.clip_id) is not None:
                track = candidate
                break
        if track is None or track.track_type == "subtitle":
            return False

        if self._is_track_locked(track.track_id):  # 审计修复：锁轨不绕过
            return False
        mode = mode or ("ripple" if self._active_tool == EditTool.RIPPLE else "trim")
        trim_side = "left" if edge == "in" else "right"
        max_frames = self._get_asset_duration(clip.asset_id)

        command, clamped = build_numeric_trim_command(
            self._timeline, track.track_id, clip.clip_id, trim_side,
            delta_frames, mode=mode, max_source_frames=max_frames,
        )
        if command is None:
            return False

        # A/V 链接联动走 B19 域级共同边界计划（审计修复：linked 片段
        # 钳制失败 → 整体拒绝，与鼠标 trim 路径一致，不再静默单边执行
        # 造成 A/V 失步）
        if self._effective_linked_selection():
            linked = self._timeline.get_reciprocal_linked_clip(clip.clip_id)
            if linked is not None:
                linked_track = None
                for candidate in self._timeline.all_tracks:
                    if candidate.get_clip_by_id(linked.clip_id) is not None:
                        linked_track = candidate
                        break
                if linked_track is None or self._is_track_locked(linked_track.track_id):
                    return False
                from src.features.edit.core.timeline_ops import (
                    build_linked_numeric_trim_command,
                )
                linked_command, _common = build_linked_numeric_trim_command(
                    self._timeline, track.track_id, clip.clip_id,
                    trim_side, clamped, mode=mode,
                    max_source_frames=max_frames,
                    linked_max_source_frames=(
                        self._get_asset_duration(linked.asset_id)),
                    mismatch="reject",
                )
                if linked_command is None:
                    self.status_message_requested.emit(I18N.tr(
                        "timeline.linked_trim_limit", "Linked clips cannot be trimmed by the same amount."))
                    return False
                command = linked_command
        self._execute_command(command)
        return True

    def _nudge_selected_edit_point(self, delta_frames: int) -> None:
        """Route a selected end or cut through the existing trim commands."""
        if self._timeline is None or not self._selected_edit_edges:
            return
        from src.features.edit.core.linked_roll import build_linked_roll_command
        try:
            for track in self._timeline.all_tracks:
                clips = sorted(track.clips, key=lambda clip: clip.timeline_start)
                for left, right in zip(clips, clips[1:]):
                    if (left.timeline_end == right.timeline_start
                            and self._selected_edit_edges.get(left.clip_id) == "right"
                            and self._selected_edit_edges.get(right.clip_id) == "left"):
                        command, _ = build_linked_roll_command(
                            self._timeline, left.clip_id, right.clip_id, delta_frames,
                            self._roll_clip_limit, self._effective_linked_selection())
                        if command is not None:
                            self._execute_command(command)
                        return
            clip_id, edge = next(iter(self._selected_edit_edges.items()))
            self._apply_step_trim(
                "in" if edge == "left" else "out", delta_frames, clip_id=clip_id,
                mode="ripple" if self._active_tool in {EditTool.TRIM, EditTool.RIPPLE} else "trim",
            )
        except (ValueError, RuntimeError) as exc:
            self.status_message_requested.emit(str(exc))

    def event(self, event: QEvent) -> bool:
        """Keep dynamic-trim keys ahead of the parent transport QShortcuts."""
        interaction = getattr(self, "_interaction", None)
        if (event.type() == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent)
                and interaction is not None and interaction.state == InteractionState.ROLLING
                and getattr(self, "_roll_preview_state", None) is not None
                and event.key() in {Qt.Key.Key_J, Qt.Key.Key_K, Qt.Key.Key_L,
                                    Qt.Key.Key_Return, Qt.Key.Key_Enter}
                and event.modifiers() in {Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier}):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        from src.features.edit.core.position_lock import PositionLockViolation
        try:
            self._handle_timeline_key_press(event)
        except PositionLockViolation:
            # _execute_command already publishes the rejection to the status bar.
            event.accept()

    def _handle_timeline_key_press(self, event: QKeyEvent) -> None:
        """Handle keyboard shortcuts for tools and navigation.

        Navigation:
        - Home: Jump to timeline start (frame 0)
        - End: Jump to timeline end (last frame)
        - Left/Right: Move playhead by 1 frame
        - Shift+Left/Right: Move playhead by 10 frames
        - Up/Down: Jump to previous/next clip boundary
        - Page Up/Down: Jump by 1 second (or configured step)

        Zoom:
        - Z: Zoom in
        - Shift+Z: Fit all timeline content to about 80% of the viewport
        - F: Zoom fit all (show entire timeline)
        - Shift+F: Zoom fit selection
        - Ctrl+Z: Undo
        - Ctrl+Shift+Z: Redo

        Tools:
        - A: Select tool
        - B: Blade tool
        - T: Trim tool
        - H: Hand tool
        - N: Toggle snap
        - D: Toggle enabled state for every selected clip

        Delete:
        - Delete/Backspace: Delete selected (leave gap)
        - Shift+Delete/Backspace: Ripple delete (close gap)
        """
        key = event.key()
        modifiers = event.modifiers()
        # macOS delivers the shifted punctuation symbol (< or >) as the Qt
        # key; QTest and some layouts deliver comma/period plus Shift.
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            key = {Qt.Key.Key_Less: Qt.Key.Key_Comma,
                   Qt.Key.Key_Greater: Qt.Key.Key_Period}.get(key, key)
        from src.features.edit.ui.timeline_shortcuts import matches_key_event
        if (key in {Qt.Key.Key_Comma, Qt.Key.Key_Period}
                and self._selected_edit_edges
                and self._active_tool in {EditTool.SELECT, EditTool.TRIM, EditTool.RIPPLE, EditTool.ROLL}
                and modifiers in {Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier}):
            step = 5 if modifiers & Qt.KeyboardModifier.ShiftModifier else 1
            if self._interaction.is_idle:
                self._nudge_selected_edit_point(-step if key == Qt.Key.Key_Comma else step)
            event.accept()
            return
        if matches_key_event(event, "edit.timeline.linked_selection", "Ctrl+Shift+L"):
            self.set_linked_selection_enabled(not self._linked_selection_enabled)
            event.accept()
            return
        primary = self._selection.primary_item
        primary_clip = (self._timeline.get_clip_by_id(primary.item_id)
                        if self._timeline is not None and primary is not None else None)
        if matches_key_event(event, "edit.timeline.retime_controls", "Ctrl+R"):
            if primary_clip is not None:
                self._on_retime_controls_toggled(primary_clip.clip_id, primary_clip.track_id,
                    primary_clip.clip_id not in self._retime_control_clip_ids)
            event.accept()
            return
        if matches_key_event(event, "edit.timeline.link_clips", "Ctrl+Alt+L"):
            if primary_clip is not None:
                if self._get_reciprocal_linked_clip(primary_clip) is not None:
                    self._on_clip_unlink(primary_clip.clip_id, primary_clip.track_id)
                else:
                    self._on_clip_link(primary_clip.clip_id, primary_clip.track_id)
            event.accept()
            return

        # F2.3：ROLLING 会话中的 JKL 动态修剪（键盘步进形态）。
        # L/J = cut 右/左步进（Shift ×10），K = 暂停（停留），Enter = 以
        # 当前 delta 提交（同一 RollTrimCommand 单步 Undo），Esc 走既有
        # 会话取消（零 revision）。会话外不消费，透传给传输层 JKL。
        if self._interaction.state == InteractionState.ROLLING \
                and self._roll_preview_state is not None:
            if key in (Qt.Key.Key_L, Qt.Key.Key_J, Qt.Key.Key_K,
                       Qt.Key.Key_Enter, Qt.Key.Key_Return):
                step = MULTI_FRAME_STEP \
                    if modifiers & Qt.KeyboardModifier.ShiftModifier \
                    else FRAME_STEP
                if key == Qt.Key.Key_L:
                    self._on_roll_trim_dragged(
                        int(self._roll_preview_state.get("delta", 0)) + step)
                    event.accept()
                    return
                if key == Qt.Key.Key_J:
                    self._on_roll_trim_dragged(
                        int(self._roll_preview_state.get("delta", 0)) - step)
                    event.accept()
                    return
                if key == Qt.Key.Key_K:
                    event.accept()  # 暂停：保持当前 delta
                    return
                if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                    self._on_roll_trim_finished(
                        str(self._roll_preview_state.get("left_clip_id", "")),
                        str(self._roll_preview_state.get("right_clip_id", "")),
                        int(self._roll_preview_state.get("delta", 0)))
                    event.accept()
                    return

        # P1-01: 数字键 1-9 实时切换多机位角度（playhead 下是多机位片段时
        # 才消费按键，否则透传给其他快捷键处理）。
        if (
            not modifiers
            and Qt.Key.Key_1 <= key <= Qt.Key.Key_9
            and self._multicam_clip_under_playhead()
        ):
            self.multicam_angle_key_requested.emit(int(key) - int(Qt.Key.Key_1) + 1)
            event.accept()
            return

        # ==================== Navigation ====================

        # Home: Jump to timeline start
        if key == Qt.Key.Key_Home:
            self._jump_to_start()
            return

        # End: Jump to timeline end
        if key == Qt.Key.Key_End:
            self._jump_to_end()
            return

        # Left arrow: Move playhead backward (Alt = nudge selected subtitle cues)
        if key == Qt.Key.Key_Left:
            if modifiers & Qt.KeyboardModifier.AltModifier and self._nudge_selected_subtitle_clips(-FRAME_STEP):
                return
            step = MULTI_FRAME_STEP if modifiers & Qt.KeyboardModifier.ShiftModifier else FRAME_STEP
            self._move_playhead(-step)
            return

        # Right arrow: Move playhead forward (Alt = nudge selected subtitle cues)
        if key == Qt.Key.Key_Right:
            if modifiers & Qt.KeyboardModifier.AltModifier and self._nudge_selected_subtitle_clips(FRAME_STEP):
                return
            step = MULTI_FRAME_STEP if modifiers & Qt.KeyboardModifier.ShiftModifier else FRAME_STEP
            self._move_playhead(step)
            return

        # Up arrow: Jump to previous clip boundary
        if key == Qt.Key.Key_Up:
            self._jump_to_prev_clip_boundary()
            return

        # Down arrow: Jump to next clip boundary
        if key == Qt.Key.Key_Down:
            self._jump_to_next_clip_boundary()
            return

        # Page Up: Jump backward by large step (1 second)
        if key == Qt.Key.Key_PageUp:
            self._jump_by_seconds(-PAGE_STEP_SECONDS)
            return

        # Page Down: Jump forward by large step (1 second)
        if key == Qt.Key.Key_PageDown:
            self._jump_by_seconds(PAGE_STEP_SECONDS)
            return

        # ==================== P1-04: 键盘步进 trim ====================
        # , / .        出点 −1 / +1 帧
        # Shift + ,/.  入点 −1 / +1 帧（入点左移=延长）
        # Alt  + ,/.   出点 ∓/±10 帧；Shift+Alt 入点 ∓/±10 帧
        # 仅 Trim/Ripple 工具 + 唯一选中媒体片段时消费。
        if key in (Qt.Key.Key_Comma, Qt.Key.Key_Period) and self._active_tool in (
                EditTool.TRIM, EditTool.RIPPLE):
            step = MULTI_FRAME_STEP if modifiers & Qt.KeyboardModifier.AltModifier else FRAME_STEP
            if key == Qt.Key.Key_Comma:
                step = -step
            edge = "in" if modifiers & Qt.KeyboardModifier.ShiftModifier else "out"
            if self._apply_step_trim(edge, step):
                return

        # ==================== Zoom Fit ====================

        # F: Zoom fit all (show entire timeline)
        if key == Qt.Key.Key_F and not modifiers:
            self.zoom_fit_all()
            return

        # Shift+F: Zoom fit selection (show selected clips)
        if key == Qt.Key.Key_F and modifiers & Qt.KeyboardModifier.ShiftModifier and not modifiers & Qt.KeyboardModifier.ControlModifier:
            self.zoom_fit_selection()
            return

        # Shift+Z: fit all timeline content with breathing room.  Keep this
        # before the plain Z tool shortcut so Shift+Z never switches to the
        # Zoom tool or falls through to a parent widget.
        if key == Qt.Key.Key_Z and modifiers == Qt.KeyboardModifier.ShiftModifier:
            self.zoom_fit_content(CONTENT_FIT_FILL_RATIO)
            return

        # ==================== Tool Shortcuts ====================

        # B5: 工具/标记/IO 键的"键 → action"匹配层迁移到 ShortcutManager
        # （用户改键即生效）；处理逻辑（状态机）保持不变。未迁移键：
        # Home/End/方向键/Space/Delete/Ctrl 组合（全局菜单 QAction 已覆盖）。
        _matched = _tool_action_match(event)
        if _matched is not None:
            _exec_tool_action(self, _matched[1])
            return

        # ==================== Marker Navigation ====================

        # M: Add a marker at the current playhead. Marker items are anchored
        # to the ruler/clip boundary, so a selected clip receives the label
        # above its content while an empty selection creates a timeline marker.
        elif key == Qt.Key.Key_M and not modifiers:
            self.add_marker_at_playhead()

        # Alt+M: Add chapter marker (C5; Shift+M is taken by jump-to-next)
        elif key == Qt.Key.Key_M and modifiers == Qt.KeyboardModifier.AltModifier:
            self.add_chapter_marker_at_playhead()

        # Ctrl+Shift+M: Jump to previous marker (check first, more specific)
        elif key == Qt.Key.Key_M and (
            modifiers & Qt.KeyboardModifier.ShiftModifier
            and modifiers & Qt.KeyboardModifier.ControlModifier
        ):
            self.jump_to_prev_marker()

        # Shift+M: Jump to next marker
        elif key == Qt.Key.Key_M and modifiers == Qt.KeyboardModifier.ShiftModifier:
            self.jump_to_next_marker()

        # ==================== Delete Actions ====================

        # Delete key: Normal delete (leave gap)
        # Shift+Delete: Ripple delete (close gap)
        elif key == Qt.Key.Key_Delete or key == Qt.Key.Key_Backspace:
            ripple = modifiers & Qt.KeyboardModifier.ShiftModifier
            self.handle_delete_request(ripple=ripple)

        # ==================== Selection & Edit ====================

        # Ctrl+A: Select all clips
        elif key == Qt.Key.Key_A and modifiers & Qt.KeyboardModifier.ControlModifier:
            self._select_all_clips()

        # Escape: Cancel interaction if active, otherwise clear selection
        elif key == Qt.Key.Key_Escape:
            if not self._interaction.is_idle:
                self._cancel_current_interaction()
            else:
                self._clear_selection()

        # Undo/Redo: Ctrl+Z (undo), Ctrl+Shift+Z (redo)
        elif key == Qt.Key.Key_Z and modifiers & Qt.KeyboardModifier.ControlModifier:
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                self.redo()
            else:
                self.undo()

        # Ctrl+B: Split all clips at playhead position
        elif key == Qt.Key.Key_B and modifiers & Qt.KeyboardModifier.ControlModifier:
            self._split_all_at_playhead()

        # P2: Copy/Paste/Cut/Duplicate
        # E03: Ctrl+X 必须排除 Shift——Split 默认键是 Ctrl+Shift+X，
        # 旧条件只查 Control 使 Split 键先命中 Cut。
        elif key == Qt.Key.Key_C and modifiers & Qt.KeyboardModifier.ControlModifier and not modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._copy_selected_clips()
        elif key == Qt.Key.Key_V and modifiers & Qt.KeyboardModifier.ControlModifier and not modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._paste_clips_at_playhead()
        elif key == Qt.Key.Key_X and modifiers & Qt.KeyboardModifier.ControlModifier and not modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._cut_selected_clips()
        elif key == Qt.Key.Key_D and modifiers & Qt.KeyboardModifier.ControlModifier and not modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._duplicate_selected_clips()

        # P4: Mark In/Out (I, O) and Clear Range (Alt+X)
        elif key == Qt.Key.Key_I and not modifiers:
            self._mark_in_at_playhead()
        elif key == Qt.Key.Key_O and not modifiers:
            self._mark_out_at_playhead()

        # Space: Hand tool temporary toggle (future)
        elif key == Qt.Key.Key_Space:
            # Don't consume Space key - let TimelinePanel handle play/pause
            event.ignore()
            return

        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event: Any) -> None:
        """INT-02：拖拽/修剪中失焦（窗口切换、浮动面板抢焦点）时取消
        当前片段交互并恢复预览，避免 release 时提交一个已被中断的操作。
        只处理片段级拖拽状态，不影响选框/播放头等轻量交互。
        INT-03：平移会话同样在此收尾并恢复光标。
        Trim Monitor 是不接收焦点的 viewer 子控件；真正失焦继续取消。
        """
        if self._interaction.is_dragging:
            self._cancel_current_interaction()
        elif self._interaction.state == InteractionState.PANNING:
            if self._pan_prev_cursor is not None:
                self.setCursor(self._pan_prev_cursor)
                self._pan_prev_cursor = None
            self._interaction.finish()
        super().focusOutEvent(event)

    def _on_application_focus_window_changed(self, window: Any) -> None:
        """Cancel a clip gesture when another real window becomes active."""
        if window is None or not self._interaction.is_dragging:
            return
        if window is self.window().windowHandle():
            return
        self._cancel_current_interaction()

    def _on_application_state_changed(self, state: Qt.ApplicationState) -> None:
        """A switch to another application also ends a pending gesture."""
        if state != Qt.ApplicationState.ApplicationActive and self._interaction.is_dragging:
            self._cancel_current_interaction()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press for blade, zoom, rubber band selection, splitter, and playhead drag."""
        # I/O belongs to the most recently used edit surface.  Take focus as
        # soon as the user touches the timeline, including the ruler and its
        # handles, so subsequent Mark In/Out keystrokes cannot target source.
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        # P10.3: Right-click in the track-header column's empty area (below all
        # track headers) shows a "create track" context menu so users can add
        # any track type without needing an existing track to right-click on.
        # We must NOT intercept right-clicks that land on an existing
        # TrackHeaderItem — those have their own rich context menu (delete,
        # rename, add track, etc.) handled in TrackHeaderItem.mousePressEvent.
        if (event.button() == Qt.MouseButton.RightButton
                and event.pos().x() < TRACK_HEADER_WIDTH):
            # Check if there's a TrackHeaderItem under the cursor; if so, let
            # the scene dispatch the event to it instead of showing our menu.
            # TrackHeaderItem has its own rich context menu (delete/rename/...).
            scene_pos = self.mapToScene(event.pos())
            item_at = self._scene.itemAt(scene_pos, self.viewportTransform())
            if not isinstance(item_at, TrackHeaderItem):
                self._show_empty_area_track_menu(event.globalPos(), scene_pos)
                event.accept()
                return

        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.pos().x() < TRACK_HEADER_WIDTH
            and event.pos().y() < RULER_HEIGHT
        ):
            self.corner_timecode_clicked.emit()
            event.accept()
            return

        # INT-03：手型工具左键 / 任意工具中键 = 平移视图，优先于所有编辑
        # 路由（框选、刀片、片段拖动、ruler scrub）。平移绝不改 selection、
        # 模型或 undo。轨道头区域不劫持左键（按钮/菜单仍可用），中键仍可
        # 垂直平移。
        middle_pan = event.button() == Qt.MouseButton.MiddleButton
        hand_pan = (
            event.button() == Qt.MouseButton.LeftButton
            and self._active_tool == EditTool.HAND
            and event.pos().x() >= TRACK_HEADER_WIDTH
        )
        if middle_pan or hand_pan:
            self._pan_origin_scroll = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self._pan_origin_global = event.globalPosition().toPoint()
            self._pan_prev_cursor = self.cursor().shape()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self._interaction.begin(
                InteractionState.PANNING, self.mapToScene(event.pos())
            )
            event.accept()
            return

        # Check track boundary resize (drag between tracks)
        if event.button() == Qt.MouseButton.LeftButton and self._handle_track_boundary_press(event):
            scene_pos = self.mapToScene(event.pos())
            self._interaction.begin(
                InteractionState.RESIZING_TRACK,
                scene_pos,
                original_value=self._track_resize_start_height,
            )
            return

        # Range handles sit on the ruler, so route them before the view's
        # ruler-scrubbing handler claims every left click in that band.
        if self._handle_range_handle_press(event):
            return

        # UI-10: Check if clicking on playhead first
        if self._handle_playhead_press(event):
            scene_pos = self.mapToScene(event.pos())
            self._interaction.begin(
                InteractionState.DRAGGING_PLAYHEAD,
                scene_pos,
                original_start=self._playhead_frame,
            )
            return

        # Check splitter press after the playhead.  The playhead spans the
        # track area, including the V/A divider; when both overlap, the red
        # playhead's horizontal scrub affordance must match the event target.
        if self._handle_splitter_press(event):
            scene_pos = self.mapToScene(event.pos())
            self._interaction.begin(
                InteractionState.RESIZING_AREA,
                scene_pos,
                original_value=self._layout_state.video_area_ratio,
            )
            return

        if self._handle_ruler_press(event):
            scene_pos = self.mapToScene(event.pos())
            self._interaction.begin(
                InteractionState.DRAGGING_RULER,
                scene_pos,
                original_start=self._playhead_frame,
            )
            return

        if self._active_tool == EditTool.BLADE:
            # Blade tool: split clip at click position
            self._interaction.begin(
                InteractionState.SPLITTING,
                self.mapToScene(event.pos()),
            )
            self._handle_blade_click(event)
        elif self._active_tool == EditTool.ZOOM:
            # Zoom tool: click to zoom
            self._interaction.begin(
                InteractionState.ZOOMING,
                self.mapToScene(event.pos()),
            )
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.zoom_out()
            else:
                self.zoom_in()
        else:
            # Check if clicking on a clip item
            scene_pos = self.mapToScene(event.pos())
            items_at_pos = self._scene.items(scene_pos)

            # Check if any clip, subtitle clip, or transition is at this position
            clicked_item = None
            for item in items_at_pos:
                if isinstance(item, (ClipItem, TransitionItem, SubtitleClipItem)):
                    clicked_item = item
                    break

            # Let TrackHeaderItem handle its own events (buttons, resize, etc.)
            header_at_pos = None
            for item in items_at_pos:
                if isinstance(item, TrackHeaderItem):
                    header_at_pos = item
                    break

            if header_at_pos is not None:
                # Pass event to scene so TrackHeaderItem receives it
                super().mousePressEvent(event)
            elif clicked_item is None and event.button() == Qt.MouseButton.LeftButton:
                # Click on empty area: start rubber band selection
                self._interaction.begin(
                    InteractionState.SELECTING,
                    scene_pos,
                )
                self._start_rubber_band_selection(event)
            else:
                # Click on clip: let the clip handle it
                super().mousePressEvent(event)

    def _start_rubber_band_selection(self, event: QMouseEvent) -> None:
        """Start rubber band selection in empty area."""
        self._independent_selection = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        self._set_selected_edit_edges({})
        # Create rubber band if not exists
        if self._rubber_band is None:
            self._rubber_band = _TimelineRubberBand(QRubberBand.Shape.Rectangle, self.viewport())

        # Store origin point (widget coordinates)
        self._rubber_band_origin = event.pos()
        self._rubber_band_start_scene = self.mapToScene(event.pos())
        self._rubber_band_active = True

        # Show rubber band at origin
        self._rubber_band.setGeometry(QRect(event.pos(), event.pos()))
        self._rubber_band.show()

    # ==================== Ruler Scrubbing ====================

    def _frame_from_scene_x(self, scene_x: float) -> int:
        """
        Convert a scene X coordinate to a timeline frame.

        Args:
            scene_x: Scene-space X coordinate

        Returns:
            Timeline frame. Moving beyond the current extent expands it.
        """
        frame = int((scene_x - TRACK_HEADER_WIDTH) / self._pixels_per_frame)
        return max(0, frame)

    def _handle_ruler_press(self, event: QMouseEvent) -> bool:
        """
        Start playhead scrubbing when the user clicks the time ruler.

        TimelineView handles blank-area clicks itself, so the ruler item does
        not reliably receive mouse events. Handling the ruler band here keeps
        click-to-seek and drag-to-scrub responsive.

        Args:
            event: Mouse press event

        Returns:
            True when the press was handled as ruler scrubbing
        """
        if event.button() != Qt.MouseButton.LeftButton:
            return False

        scene_pos = self.mapToScene(event.pos())
        if scene_pos.x() < TRACK_HEADER_WIDTH:
            return False
        scene_top = self.mapToScene(0, 0).y()
        if scene_pos.y() < scene_top or scene_pos.y() > scene_top + RULER_HEIGHT:
            return False

        frame = self._frame_from_scene_x(scene_pos.x())
        self._playhead_dragging = True
        self._playhead_drag_start_frame = self._playhead_frame
        self.setCursor(Qt.CursorShape.SizeHorCursor)

        if self._playhead_item:
            self._playhead_item.set_dragging(True)

        self.scrub_started.emit(frame)
        self.set_playhead(frame)
        event.accept()
        logger.debug(f"Ruler scrub started at frame {frame}")
        return True

    def _handle_range_handle_press(self, event: QMouseEvent) -> bool:
        """Give an In/Out handle precedence over ruler scrubbing."""
        if (
            event.button() != Qt.MouseButton.LeftButton
            or self._range_item is None
            or not self._range_item.isVisible()
        ):
            return False
        scene_pos = self.mapToScene(event.pos())
        if not self._range_item.sceneBoundingRect().contains(scene_pos):
            return False
        if self._range_item._hit_edge(scene_pos) is None:
            return False
        # Dispatch through QGraphicsView so TimelineRangeItem becomes the
        # mouse grabber and receives matching move/release events.
        super().mousePressEvent(event)
        return event.isAccepted()

    # ==================== UI-10: Playhead Drag ====================

    def _set_cursor_for_active_clip_trim(self) -> bool:
        """Keep the trim cursor stable while a clip owns the mouse grab."""
        grabber = self._scene.mouseGrabberItem()
        if not isinstance(grabber, ClipItem):
            return False

        drag_mode = grabber._drag_mode
        if drag_mode == "trim_left":
            self.setCursor(_trim_cursor("left"))
            return True
        if drag_mode == "trim_right":
            self.setCursor(_trim_cursor("right"))
            return True
        kind = {"fade_in": "fade", "fade_out": "fade",
                "ripple_trim_left": "ripple_left", "ripple_trim_right": "ripple_right",
                "roll_trim_left": "roll", "roll_trim_right": "roll"}.get(drag_mode)
        if kind is not None:
            self.setCursor(edit_cursor(kind))
            return True
        return False

    def _scene_has_clip_content(self, scene_pos: QPointF) -> bool:
        """Return whether a timeline clip owns the point under the pointer."""
        return any(
            isinstance(item, (ClipItem, TransitionItem, SubtitleClipItem))
            for item in self._scene.items(scene_pos)
        )

    def _is_playhead_hit(self, scene_pos: QPointF) -> bool:
        """Test the playhead with a narrow gap-only forgiveness band."""
        playhead = self._playhead_item
        if playhead is None:
            return False

        # Keep exact shape hit testing first.  This preserves the existing
        # behaviour when the pointer is on the painted head/stem, including
        # when the playhead crosses a clip.
        playhead_pos = playhead.mapFromScene(scene_pos)
        if playhead.shape().contains(playhead_pos):
            return True

        # Only widen the invisible hit target in a gap.  A nearby clip must
        # retain its own trim/move interaction and cursor feedback.
        if self._scene_has_clip_content(scene_pos):
            return False

        playhead_x = playhead.scenePos().x()
        return (
            abs(scene_pos.x() - playhead_x) <= PLAYHEAD_EMPTY_HIT_TOLERANCE
            and playhead.sceneBoundingRect().top()
            <= scene_pos.y()
            <= playhead.sceneBoundingRect().bottom()
        )

    def _handle_playhead_press(self, event: QMouseEvent) -> bool:
        """
        Check if mouse press is on playhead and start drag if so.

        Args:
            event: Mouse press event

        Returns:
            True if playhead drag started, False otherwise
        """
        if event.button() != Qt.MouseButton.LeftButton:
            return False

        scene_pos = self.mapToScene(event.pos())
        if not self._is_playhead_hit(scene_pos):
            return False

        # Start playhead drag
        self._playhead_dragging = True
        self._playhead_drag_start_frame = self._playhead_frame
        self.setCursor(Qt.CursorShape.SizeHorCursor)

        # Set dragging state on playhead item (shows timecode label)
        if self._playhead_item:
            self._playhead_item.set_dragging(True)

        self.scrub_started.emit(self._playhead_frame)
        logger.debug(f"Playhead drag started at frame {self._playhead_frame}")
        return True

    def _handle_playhead_drag(self, event: QMouseEvent) -> None:
        """
        Handle playhead drag movement.

        Args:
            event: Mouse move event
        """
        scene_pos = self.mapToScene(event.pos())

        # Keep the scrub cursor stable while the pointer crosses track
        # boundaries.  Those boundaries use a vertical resize cursor, but a
        # playhead drag is a horizontal timeline operation.
        self.setCursor(Qt.CursorShape.SizeHorCursor)

        frame = self._frame_from_scene_x(scene_pos.x())

        # Update playhead position (live preview)
        self.set_playhead(frame)
        self.scrub_moved.emit(frame)

        logger.debug(f"Playhead dragged to frame {frame}")

    def _finish_playhead_drag(self) -> None:
        """Complete playhead drag."""
        self._playhead_dragging = False

        # Clear dragging state on playhead item
        if self._playhead_item:
            self._playhead_item.set_dragging(False)
        self._refresh_playhead_visual()

        self.scrub_finished.emit(self._playhead_frame)
        logger.debug(f"Playhead drag finished at frame {self._playhead_frame}")

    # ==================== Splitter (V/A Area Divider) ====================

    def _handle_splitter_press(self, event: QMouseEvent) -> bool:
        """Check if press is on a splitter (V/A or S/V) and start drag."""
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if self._timeline is None:
            return False

        scene_pos = self.mapToScene(event.pos())
        self._configure_layout_engine()
        # Subtitle tracks are fixed-height. Their separator is visual only;
        # the V/A splitter below remains resizable and moves both stacks.
        if self._layout_engine.is_in_subtitle_splitter(scene_pos.y()):
            return False
        if self._layout_engine.is_in_splitter(scene_pos.y()):
            self._splitter_dragging = True
            self._splitter_drag_kind = "video"
            self._splitter_drag_start_y = scene_pos.y()
            self._splitter_drag_start_ratio = self._layout_state.video_area_ratio
            self._splitter_keep_video_bottom = self._is_video_stack_aligned_to_splitter()
            event.accept()
            return True
        return False

    def _handle_splitter_drag(self, event: QMouseEvent) -> None:
        """Handle splitter drag to resize areas."""
        scene_pos = self.mapToScene(event.pos())
        if getattr(self, '_splitter_drag_kind', 'video') == "subtitle":
            # S/V splitter: adjust subtitle area ratio (fraction of non-video space)
            new_ratio = self._layout_engine.subtitle_splitter_ratio_from_y(scene_pos.y())
            self._layout_state.subtitle_area_ratio = new_ratio
            self._layout_engine.set_subtitle_splitter_ratio(new_ratio)
        else:
            # V/A splitter: adjust video area ratio
            new_ratio = self._layout_engine.splitter_ratio_from_y(scene_pos.y())
            self._layout_state.video_area_ratio = new_ratio
            self._layout_engine.set_splitter_ratio(new_ratio)
            if self._splitter_keep_video_bottom:
                self._align_video_stack_to_splitter()
        self._layout_track_items()
        self.viewport().update()

    def _finish_splitter_drag(self) -> None:
        """Complete splitter drag."""
        self._splitter_dragging = False
        logger.debug(f"Splitter drag finished, ratio={self._layout_state.video_area_ratio:.2f}")

    # ── Track boundary resize ────────────────────────────────────────────────

    def _find_audio_track_above(self, track_id: str) -> Optional[str]:
        """Return the audio track immediately above the given one, or None."""
        if self._timeline is None:
            return None
        tracks = self._timeline.audio_tracks
        for i, t in enumerate(tracks):
            if t.track_id == track_id and i > 0:
                return tracks[i - 1].track_id
        return None

    def _find_video_track_below(self, track_id: str) -> Optional[str]:
        """Return the video track immediately below the given one, or None."""
        if self._timeline is None:
            return None
        tracks = self._timeline.video_tracks
        for i, t in enumerate(tracks):
            if t.track_id == track_id and i > 0:
                return tracks[i - 1].track_id
        return None

    def _handle_track_boundary_press(self, event: QMouseEvent) -> bool:
        """Check if press is on a track boundary and start resize.

        Only works in the track header area.
        """
        if self._timeline is None:
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False

        scene_pos = self.mapToScene(event.pos())

        # Only allow resize in header area
        if scene_pos.x() >= TRACK_HEADER_WIDTH:
            return False

        self._configure_layout_engine()

        # Don't interfere with splitter
        if self._layout_engine.is_in_splitter(scene_pos.y()):
            return False

        result = self._layout_engine.track_boundary_at_y(scene_pos.y())
        if result is None:
            return False

        track_id, boundary_y, edge = result
        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return False
        # SUB-P1-06: subtitle tracks are now resizable within their compact
        # range, so no longer reject the press for track_type == "subtitle".
        # The height change handler clamps to MIN/MAX_SUBTITLE_TRACK_HEIGHT.

        # Detect if this is an audio track
        is_audio = track.track_type == "audio"

        # Normalize: always resize the same track for a given separator,
        # regardless of whether we hit the top or bottom edge.
        #   Video: separator always resizes the track BELOW it
        #   Audio: separator always resizes the track ABOVE it
        if is_audio:
            if edge == "top":
                above_id = self._find_audio_track_above(track_id)
                if above_id is not None:
                    track_id = above_id
        else:
            if edge == "bottom":
                below_id = self._find_video_track_below(track_id)
                if below_id is not None:
                    track_id = below_id

        self._track_resize_dragging = True
        self._track_resize_track_id = track_id
        self._track_resize_start_y = scene_pos.y()
        self._track_resize_start_height = self._layout_engine.track_height(track_id)
        self._track_resize_is_audio = is_audio
        event.accept()
        return True

    def _handle_track_boundary_drag(self, event: QMouseEvent) -> None:
        """Handle track boundary drag to resize track height."""
        if not self._track_resize_dragging or self._track_resize_track_id is None:
            return

        scene_pos = self.mapToScene(event.pos())
        delta_y = scene_pos.y() - self._track_resize_start_y

        # Video: drag up = taller (subtract delta)
        # Audio: drag down = taller (add delta)
        if self._track_resize_is_audio:
            new_height = self._track_resize_start_height + int(delta_y)
        else:
            new_height = self._track_resize_start_height - int(delta_y)

        # Clamp track height
        # BUG-09 fix: Use centralized constants instead of hardcoded values
        new_height = max(EDIT_SIZES["track_h_min"], min(EDIT_SIZES["track_h_max"], new_height))

        if new_height != self._layout_engine.track_height(self._track_resize_track_id):
            self._on_track_height_changed(self._track_resize_track_id, new_height)

        # Keep cursor
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def _finish_track_boundary_resize(self) -> None:
        """Complete track boundary resize."""
        track_id = self._track_resize_track_id
        self._track_resize_dragging = False
        self._track_resize_track_id = None
        if track_id:
            logger.debug(f"Track boundary resize finished: {track_id} height={self._layout_engine.track_height(track_id)}")

    def _update_cursor_for_position(self, scene_pos: QPointF) -> None:
        """Update cursor based on position (splitter hover, track boundary, etc.)."""
        if scene_pos.x() >= TRACK_HEADER_WIDTH and self._active_tool in {EditTool.HAND, EditTool.ZOOM}:
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._active_tool == EditTool.HAND
                           else Qt.CursorShape.CrossCursor)
            return
        if self._timeline is not None:
            self._configure_layout_engine()

            # Match the same narrow, gap-only forgiveness band used by mouse
            # press handling.  Do not use the playhead boundingRect directly:
            # it is intentionally wider for paint invalidation and would
            # steal cursor feedback from nearby clip edges.
            if self._is_playhead_hit(scene_pos):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return

            if self._layout_engine.is_in_subtitle_splitter(scene_pos.y()):
                self.setCursor(Qt.CursorShape.ArrowCursor)
                return
            if self._layout_engine.is_in_splitter(scene_pos.y()):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                return
            # Track boundary resize only in header area
            if scene_pos.x() < TRACK_HEADER_WIDTH:
                boundary = self._layout_engine.track_boundary_at_y(scene_pos.y())
                if boundary is not None:
                    track = self._timeline.get_track_by_id(boundary[0])
                    if track is not None and track.track_type != "subtitle":
                        self.setCursor(Qt.CursorShape.SizeVerCursor)
                        return
        # Scene items own their hover cursor. Setting Arrow here before
        # dispatching every move fights the item's hand/trim cursor and can
        # leave the viewport showing a stale shape when the item skips an
        # unchanged setCursor call.
        item = self.itemAt(self.mapFromScene(scene_pos))
        while item is not None:
            if (isinstance(item, (ClipItem, SubtitleClipItem, TrackHeaderItem, TransitionItem))
                    or item.hasCursor()):
                if self.testAttribute(Qt.WidgetAttribute.WA_SetCursor):
                    self.unsetCursor()
                return
            item = item.parentItem()
        if self.cursor().shape() != Qt.CursorShape.ArrowCursor:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move for rubber band selection, playhead drag, and splitter drag."""
        scene_pos = self.mapToScene(event.pos())

        # INT-03：平移会话按 viewport 像素位移滚动两个滚动条
        if self._interaction.state == InteractionState.PANNING:
            delta = event.globalPosition().toPoint() - self._pan_origin_global
            hbar = self.horizontalScrollBar()
            vbar = self.verticalScrollBar()
            hbar.setValue(self._pan_origin_scroll[0] - delta.x())
            vbar.setValue(self._pan_origin_scroll[1] - delta.y())
            event.accept()
            return

        # Update interaction position tracking
        if not self._interaction.is_idle:
            self._interaction.update_pos(scene_pos)

        # Handle splitter drag (check both legacy flag and state machine)
        if self._splitter_dragging or self._interaction.state == InteractionState.RESIZING_AREA:
            self._handle_splitter_drag(event)
            return

        # Handle track boundary resize drag
        if self._track_resize_dragging or self._interaction.state == InteractionState.RESIZING_TRACK:
            self._handle_track_boundary_drag(event)
            return

        # UI-10: Handle playhead drag (check both legacy flag and state machine)
        if self._playhead_dragging or self._interaction.state in (
            InteractionState.DRAGGING_PLAYHEAD,
            InteractionState.DRAGGING_RULER,
        ):
            self._handle_playhead_drag(event)
            return

        # Update cursor for splitter hover
        if not self._playhead_dragging and not self._rubber_band_active and self._interaction.is_idle:
            if not self._set_cursor_for_active_clip_trim():
                self._update_cursor_for_position(scene_pos)

        if self._rubber_band_active and self._rubber_band is not None:
            # Update rubber band geometry
            origin = self._rubber_band_origin
            current = event.pos()
            rect = QRect(origin, current).normalized()
            self._rubber_band.setGeometry(rect)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release to complete rubber band selection, playhead drag, or splitter drag."""
        # INT-03：结束平移会话并恢复光标
        if self._interaction.state == InteractionState.PANNING and event.button() in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.MiddleButton,
        ):
            if self._pan_prev_cursor is not None:
                self.setCursor(self._pan_prev_cursor)
                self._pan_prev_cursor = None
            self._interaction.finish()
            self._update_cursor_for_position(self.mapToScene(event.pos()))
            event.accept()
            return

        # Finish splitter drag
        if self._splitter_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._finish_splitter_drag()
            self._interaction.finish()
            return

        # Finish track boundary resize
        if self._track_resize_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._finish_track_boundary_resize()
            self._interaction.finish()
            return

        # UI-10: Finish playhead drag
        if self._playhead_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._finish_playhead_drag()
            self._interaction.finish()
            self._update_cursor_for_position(self.mapToScene(event.pos()))
            return

        if self._rubber_band_active and event.button() == Qt.MouseButton.LeftButton:
            self._finish_rubber_band_selection(event)
            self._interaction.finish()
        else:
            # IMPORTANT: Deliver event to scene BEFORE clearing drag session.
            # super() routes the release to ClipItem, which emits drag_finished.
            # Roll also checks that its session is still ROLLING at commit.
            # Finishing here before scene dispatch rejected every real gesture,
            # even though directly calling the roll handler appeared to work.
            try:
                super().mouseReleaseEvent(event)
            finally:
                if not self._interaction.is_idle:
                    self._interaction.finish()
                self._current_drag_session = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Handle double-click for splitter reset."""
        scene_pos = self.mapToScene(event.pos())
        if self._timeline and self._layout_engine.is_in_splitter(scene_pos.y()):
            self.reset_splitter_ratio()
            return
        clicked = self.itemAt(event.pos())
        while clicked is not None and not isinstance(clicked, ClipItem):
            clicked = clicked.parentItem()
        from src.features.teaching_animation.host_adapters import is_motion_graphic_asset_id
        if isinstance(clicked, ClipItem) and is_motion_graphic_asset_id(clicked.asset_id):
            self.teaching_animation_open_requested.emit(clicked.clip_id, clicked.track_id)
            event.accept()
            return
        # P1-02: 双击嵌套片段进入子序列（与右键"进入嵌套序列"同一命令路径）
        if isinstance(clicked, ClipItem):
            project = getattr(self, "_project", None)
            if project is not None:
                asset = project.assets.get(clicked.asset_id)
                child_tid = getattr(asset, "nested_timeline_id", None) if asset is not None else None
                if child_tid:
                    self._dispatch_enter_nested(clicked.clip_id)
                    event.accept()
                    return
        super().mouseDoubleClickEvent(event)

    def _finish_rubber_band_selection(self, event: QMouseEvent) -> None:
        """
        Complete rubber band selection and select clips in range (UI-12).

        Uses event scene position instead of global cursor.
        Supports Shift/Ctrl for additive selection.

        Args:
            event: Mouse release event
        """
        if self._rubber_band is None or self._rubber_band_start_scene is None:
            return

        # Hide rubber band
        self._rubber_band.hide()
        self._rubber_band_active = False

        if self._timeline is None:
            return

        # UI-12: Use event scene position instead of global cursor
        current_scene = self.mapToScene(event.pos())
        start_scene = self._rubber_band_start_scene

        # Create selection rect in scene coordinates
        selection_rect = QRectF(start_scene, current_scene).normalized()

        # Find all clips within the selection rectangle
        selected_clip_ids: list[str] = []
        selected_track_ids: list[str] = []

        for clip_id, item in self._items_map.items():
            if isinstance(item, (ClipItem, SubtitleClipItem)):
                # Check if item intersects with selection rect
                item_rect = item.sceneBoundingRect()
                if selection_rect.intersects(item_rect):
                    selected_clip_ids.append(clip_id)
                    selected_track_ids.append(item.track_id)

        if self._effective_linked_selection():
            for clip_id in tuple(selected_clip_ids):
                partner = self._get_reciprocal_linked_clip(self._timeline.get_clip_by_id(clip_id))
                if partner is not None and partner.clip_id not in selected_clip_ids:
                    selected_clip_ids.append(partner.clip_id)
                    selected_track_ids.append(partner.track_id)

        # UI-12: Check for Shift/Ctrl modifiers for additive selection
        modifiers = event.modifiers()
        is_additive = modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)

        # Create selection with all selected clips
        if selected_clip_ids:
            # Build selection items
            items = []
            for clip_id, track_id in zip(selected_clip_ids, selected_track_ids):
                from src.features.edit.core.selection import SelectionItem, SelectionType
                items.append(SelectionItem(
                    item_type=SelectionType.CLIP,
                    item_id=clip_id,
                    track_id=track_id,
                    timeline_id=self._timeline.timeline_id,
                ))

            new_selection = TimelineSelection(
                items=tuple(items),
                is_range=False,
                primary_item=items[0] if items else None,
            )

            # Handle additive selection
            if is_additive and self._selection.items:
                # Merge with existing selection（INT-09：按身份键合并去重）
                def _ident(sel_item) -> tuple:
                    return (
                        sel_item.item_type, sel_item.item_id, sel_item.timeline_id
                    )

                existing_ids = {_ident(item) for item in self._selection.items}
                new_ids = {_ident(item) for item in new_selection.items}

                # Ctrl: toggle selection (remove if already selected, add if not)
                if modifiers & Qt.KeyboardModifier.ControlModifier:
                    # Toggle: remove items that were in rubber band AND existing selection
                    # Add items that were in rubber band AND NOT in existing selection
                    toggled_items = []
                    for item in self._selection.items:
                        if _ident(item) not in new_ids:
                            toggled_items.append(item)  # Keep items not in rubber band
                    for item in new_selection.items:
                        if _ident(item) not in existing_ids:
                            toggled_items.append(item)  # Add new items from rubber band

                    if toggled_items:
                        merged_selection = TimelineSelection(
                            items=tuple(toggled_items),
                            is_range=False,
                            primary_item=toggled_items[0] if toggled_items else None,
                        )
                    else:
                        merged_selection = TimelineSelection()
                else:
                    # Shift: additive selection (union)；重复对象由
                    # TimelineSelection 构造不变量去重，保持原有顺序
                    merged_items = list(self._selection.items) + list(new_selection.items)
                    merged_selection = TimelineSelection(
                        items=tuple(merged_items),
                        is_range=False,
                        primary_item=self._selection.primary_item if self._selection.primary_item else new_selection.primary_item,
                    )

                self.set_selection(merged_selection)
            else:
                # Normal selection: replace existing
                self.set_selection(new_selection)
        else:
            # No clips in selection area
            if is_additive:
                # Keep existing selection when Shift/Ctrl pressed
                pass
            else:
                # Clear selection
                self.set_selection(TimelineSelection())

    def _handle_blade_click(self, event: QMouseEvent) -> None:
        """Handle blade tool click to split clip.

        P5a: Uses layout engine hit-test on Y coordinate to determine which
        track was clicked. Only splits clips on THAT track. Hold Shift to
        split all tracks at the click frame (mirroring split-all behaviour).

        INT-04：仅左键执行切割——右键保留给上下文菜单、中键平移；局部
        刀片与 Shift 全轨一样尊重轨道锁；轨道解析失败时不再 fallback
        逐轨扫描（可能误切别轨），记诊断直接返回。
        """
        if self._timeline is None:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        # Get scene position
        scene_pos = self.mapToScene(event.pos())

        # Calculate frame position (exclude header)
        frame = int((scene_pos.x() - TRACK_HEADER_WIDTH) / self._pixels_per_frame)

        if frame < 0:
            return

        modifiers = QApplication.keyboardModifiers()
        split_all = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        if split_all:
            # Shift+click: split all tracks at this frame（已按锁轨过滤）
            self._split_all_at_frame(frame)
            return

        # Normal blade: determine which track was hit from Y position
        self._configure_layout_engine()
        hit_track_id, _ = self._layout_engine.track_at_y_with_area(scene_pos.y())

        if not hit_track_id:
            logger.debug(
                "Blade click at scene y=%s did not resolve to a track; no split",
                scene_pos.y(),
            )
            return
        if self._is_track_locked(hit_track_id):
            logger.debug("Blade click on locked track %s ignored", hit_track_id)
            return
        track = self._timeline.get_track_by_id(hit_track_id)
        if track is None:
            return
        clip = track.get_clip_at(frame)
        if clip is not None and clip.timeline_start < frame < clip.timeline_end:
            cmd = SplitClipCommand.create(track.track_id, clip.clip_id, frame)
            self._execute_command(cmd)

    def _split_all_at_playhead(self) -> None:
        """
        Split all clips at playhead position (Ctrl+B).

        P5b: Collects all splittable clips first, then creates ONE
        MacroCommand containing all SplitClipCommands. Executed atomically
        via _execute_command so one undo restores all splits at once.
        """
        if self._timeline is None:
            return

        frame = self._playhead_frame
        if frame < 0:
            return

        self._split_all_at_frame(frame)

    def _split_all_at_frame(self, frame: int) -> None:
        """
        P5a/P5b: Split all clips at the given frame position.

        Collects all splittable clips, creates a single MacroCommand,
        and executes atomically. One undo restores all splits.
        """
        if self._timeline is None:
            return

        # Find all clips at the given frame position
        clips_to_split: list[tuple[str, str]] = []  # (track_id, clip_id)

        locked_ids = self._get_locked_track_ids()
        all_tracks = self._timeline.video_tracks + self._timeline.audio_tracks
        for track in all_tracks:
            if track.track_id in locked_ids:
                continue
            clip = track.get_clip_at(frame)
            if clip is not None:
                # Verify clip is strictly inside the frame
                # (not at start or end, which would be invalid split)
                if clip.timeline_start < frame < clip.timeline_end:
                    clips_to_split.append((track.track_id, clip.clip_id))

        if not clips_to_split:
            logger.debug(f"No clips to split at frame {frame}")
            return

        # P5b: Build one MacroCommand containing all splits
        split_commands = []
        for track_id, clip_id in clips_to_split:
            cmd = SplitClipCommand.create(track_id, clip_id, frame)
            split_commands.append(cmd)

        macro = MacroCommand.create(
            f"Split {len(split_commands)} clips at frame {frame}",
            split_commands,
        )
        self._execute_command(macro)

        logger.info(f"Split {len(clips_to_split)} clips at frame {frame}")

    # ==================== Command Execution ====================

    def execute_command(self, command: Any) -> None:
        """
        Unified public entry for committing a timeline command.

        All timeline mutations (regular clips AND subtitles) MUST go through
        here so that the command stack, incremental view update, project
        cache, renderer sync, and autosave stay on a single sequence
        revision. Callers should never mutate ``edit_page._current_timeline``
        directly. This is a thin wrapper around :meth:`_execute_command`
        that keeps the private/protected boundary while exposing the
        contract referenced by SUB-P0-01.
        """
        self._execute_command(command)

    def configure_project_transaction_context(
        self, project_provider: Callable[[], Any],
        sequences_provider: Callable[[], Mapping[str, TimelineSequence]],
    ) -> None:
        """Read current controller snapshots before a combined history edit."""
        self._transaction_project_provider = project_provider
        self._transaction_sequences_provider = sequences_provider

    def _prepare_project_transaction(self, command: Any, *, undo: bool = False) -> Any:
        from src.features.edit.core.timeline_project_transaction import TimelineProjectTransactionCommand

        def contains_transaction(candidate: Any) -> bool:
            return (isinstance(candidate, TimelineProjectTransactionCommand)
                    or any(contains_transaction(child)
                           for child in getattr(candidate, "sub_commands", ())))

        if not isinstance(command, TimelineProjectTransactionCommand):
            if contains_transaction(command):
                raise ValueError("Project transactions must be committed directly, outside a Macro")
            return None
        project = (self._transaction_project_provider()
                   if self._transaction_project_provider is not None else self._project)
        if project is None:
            raise ValueError("A project is required for this timeline transaction")
        if self._transaction_sequences_provider is not None:
            sequences = dict(self._transaction_sequences_provider())
        else:
            sequences = {**self._project_transaction_sequences, **self._timeline_history_snapshots}
        if self._timeline is not None:
            sequences[self._timeline.timeline_id] = self._timeline
            # Validate the active snapshot before CommandStack pops history.
            if undo:
                command.undo(self._timeline)
            else:
                command.apply(self._timeline)
        return command.prepare_project(project, sequences, undo=undo)

    def _publish_timeline_history_change(self, timeline: TimelineSequence, transaction: Any) -> None:
        if transaction is None:
            self.timeline_changed.emit(timeline)
            return
        project, child_updates = transaction
        self._project = project
        for key, child in child_updates.items():
            if child is None:
                self._project_transaction_sequences.pop(key, None)
                self._timeline_command_stacks.pop(key, None)
                self._timeline_history_snapshots.pop(key, None)
            else:
                self._project_transaction_sequences[key] = child
        self.project_timeline_transaction_committed.emit(project, timeline, child_updates)

    def _execute_command(self, command: Any) -> None:
        """
        Execute a command and update view.

        Phase 7.2: 使用增量更新代替全量重建，
        避免闪烁和性能问题。

        Args:
            command: Command to execute
        """
        if self._timeline is None:
            return

        # _add_asset_as_clip still owns command construction (including the
        # linked A/V invariants), but batch drops defer execution until all
        # commands have been collected. MacroCommand then applies them to the
        # immutable timeline in one command-stack transaction and triggers a
        # single view/project refresh.
        if self._batch_drop_commands is not None:
            self._batch_drop_commands.append(command)
            return

        # Store old timeline for change detection
        old_timeline = self._timeline

        # W06：Position Lock 提交预检（契约 D3）。footprint 级判定——
        # 覆盖鼠标/键盘/菜单/拖放全部经此入口的编辑，undo/redo 不经
        # 此路径（历史恢复优先）。拒绝 = 命令未执行、零 revision、
        # 零 history；异常沿既有 ValueError 反馈路径显示。
        if self._position_lock_enabled:
            from src.features.edit.core.position_lock import (
                PositionLockViolation, evaluate_command_footprint,
            )
            try:
                evaluate_command_footprint(command, self._timeline)
            except PositionLockViolation as exc:
                self.status_message_requested.emit(str(exc))
                raise

        transaction = self._prepare_project_transaction(command)

        # Execute command (CommandStack handles tuple unwrapping)
        new_timeline = self._command_stack.execute(command, self._timeline)

        if new_timeline is not None and new_timeline is not old_timeline:
            # APP-01：手动编辑提交边界推进 sequence revision。命令本身
            # 保留旧 revision（快照），此前 UI 直连 CommandStack 只 apply/
            # push 不 bump，project/timeline revision 恒 0，AI 乐观锁看不
            # 见人工修改。undo/redo 同样单调 +1；同一内容重复提交或 no-op
            # （返回同一对象/相等内容）不增。
            new_timeline = self._bump_revision_at_commit(new_timeline)
            self._timeline = new_timeline

            # Range changes use the same command path as clip edits, but they
            # do not alter clip geometry and therefore are invisible to the
            # incremental clip-change detector. Refresh range UI explicitly.
            self._update_range_visuals(new_timeline)

            # Phase 7.2: Detect changes and use incremental update
            changes = self._clip_change_detector(old_timeline, new_timeline)

            if changes.full_reload_needed:
                # Full reload required (track structure changed, FPS changed, etc.)
                self.load_timeline(new_timeline)
            else:
                # Incremental update for better performance
                self.update_timeline_incremental(changes)

            # E22: command_executed emit 已删除（无接收者）
            # Emit timeline_changed so MainWindow/EditPage stay synchronized
            self._publish_timeline_history_change(new_timeline, transaction)

            # AUD-17614: 编辑路径即时刷新缓存状态条。旧实现只在 500ms
            # 轮询里刷新（见 _update_cache_indicator 的轮询门控说明），
            # 命令执行后状态条最多滞后半个周期。
            self._update_cache_indicator()

            # FX-H03: executing a new command clears the redo stack, so a
            # previously-undone generator-clip-add can no longer be redone.
            # Garbage-collect any generator assets (Adjustment/Solid) that no
            # clip references — they are orphans from an undo. We do NOT clean
            # on undo itself so redo stays possible. Only unreferenced generator
            # assets are touched; a legitimate second clip is never orphaned.
            self._gc_orphan_generator_assets()

    def _gc_orphan_generator_assets(self) -> None:
        """Remove generator assets no clip references (FX-H03 orphan cleanup).

        Safe by construction: only touches adjustment/solid assets, only when
        zero clips reference them. Called on each new command (when the redo
        stack is cleared) and safe to call any time.
        """
        if self._project is None or self._timeline is None:
            return
        # This view only owns the active TimelineSequence. In a multi-
        # timeline project, deleting from this partial view can remove a
        # generator still referenced elsewhere. Save-time GC has the full
        # sequence map and is the only safe reclamation point in that case.
        if len(getattr(self._project, "timelines", {}) or {}) > 1:
            logger.debug(
                "Skipping runtime generator GC for multi-timeline project; "
                "save preflight will scan all sequences"
            )
            return
        try:
            referenced: set[str] = set()
            for track in self._timeline.all_tracks:
                for clip in track.clips:
                    if clip.asset_id:
                        referenced.add(clip.asset_id)
            from src.features.edit.core.generator_clip_transaction import (
                undo_generator_clip_transaction,
            )
            project = self._project
            changed = False
            for asset_id, asset in list(project.assets.items()):
                if (getattr(asset, 'media_type', '') in ('adjustment', 'solid')
                        and asset_id not in referenced):
                    new_project = undo_generator_clip_transaction(
                        project, asset_id, referenced_asset_ids=referenced,
                    )
                    if new_project is not project:
                        project = new_project
                        changed = True
            if changed:
                self._project = project
                self.project_changed.emit(self._project)
        except Exception:  # noqa: BLE001 — GC must never break editing
            logger.debug("orphan generator asset GC skipped", exc_info=True)

    def undo(self) -> None:
        """Undo last command."""
        if self._timeline is None:
            return

        if not self._command_stack.can_undo():
            return
        try:
            transaction = self._prepare_project_transaction(
                self._command_stack._undo_stack[-1], undo=True)
        except ValueError as exc:
            self.status_message_requested.emit(str(exc))
            return
        new_timeline = self._command_stack.undo(self._timeline)
        if new_timeline is not None:
            # APP-01：undo 恢复内容快照但 revision 继续单调 +1，不回旧值
            new_timeline = self._bump_revision_at_commit(new_timeline)
            self._timeline = new_timeline
            self.load_timeline(new_timeline)
            # Emit timeline_changed so MainWindow/EditPage stay synchronized
            self._publish_timeline_history_change(new_timeline, transaction)

    def redo(self) -> None:
        """Redo last undone command."""
        if self._timeline is None:
            return

        if not self._command_stack.can_redo():
            return
        try:
            transaction = self._prepare_project_transaction(
                self._command_stack._redo_stack[-1])
        except ValueError as exc:
            self.status_message_requested.emit(str(exc))
            return
        new_timeline = self._command_stack.redo(self._timeline)
        if new_timeline is not None:
            new_timeline = self._bump_revision_at_commit(new_timeline)
            self._timeline = new_timeline
            self.load_timeline(new_timeline)
            # Emit timeline_changed so MainWindow/EditPage stay synchronized
            self._publish_timeline_history_change(new_timeline, transaction)

    def _bump_revision_at_commit(self, new_timeline) -> Any:
        """APP-01：统一提交边界的 revision 单调推进。

        新 revision = 提交前 revision + 1（内容快照恢复的旧 revision 被
        覆盖，保证 undo/redo 也不回退版本号）。TimelineSequence.__eq__ 只
        比较 timeline_id，不能用作内容比较；no-op 判定采用同一性——
        领域命令对无效目标约定返回原实例（ToggleEnabled 等已是如此），
        返回同一对象即未变更、不增。
        """
        from dataclasses import replace as _dc_replace

        if self._timeline is None:
            return new_timeline
        if new_timeline is self._timeline:
            return new_timeline
        return _dc_replace(
            new_timeline, revision=self._timeline.revision + 1
        )

    def can_undo(self) -> bool:
        """Check if undo is available."""
        return self._command_stack.can_undo()

    def can_redo(self) -> bool:
        """Check if redo is available."""
        return self._command_stack.can_redo()

    # ==================== Phase 7.2: Incremental Update ====================

    def _clip_change_detector(
        self,
        old_timeline: Optional[TimelineSequence],
        new_timeline: TimelineSequence
    ) -> TimelineChanges:
        """
        Detect changes between old and new timeline for incremental update.

        参考 Kdenlive 的增量更新策略：
        - 比较新旧 clip 数据，识别变化类型
        - 只更新变化的 clip，避免全量重建

        Args:
            old_timeline: Previous timeline state (can be None for first load)
            new_timeline: New timeline state

        Returns:
            TimelineChanges with added/removed/modified items
        """
        if old_timeline is None:
            # First load - need full reload
            return TimelineChanges(full_reload_needed=True)

        # Build maps of old items
        old_clips: Dict[str, ClipInstance] = {}
        old_transitions: Dict[str, TransitionInstance] = {}
        old_markers: Dict[str, TimelineMarker] = {}

        for track in old_timeline.all_tracks:
            for clip in track.clips:
                old_clips[clip.clip_id] = clip
            for trans in track.transitions:
                old_transitions[trans.transition_id] = trans

        for marker in old_timeline.markers:
            old_markers[marker.marker_id] = marker

        # Build maps of new items
        new_clips: Dict[str, ClipInstance] = {}
        new_transitions: Dict[str, TransitionInstance] = {}
        new_markers: Dict[str, TimelineMarker] = {}

        for track in new_timeline.all_tracks:
            for clip in track.clips:
                new_clips[clip.clip_id] = clip
            for trans in track.transitions:
                new_transitions[trans.transition_id] = trans

        for marker in new_timeline.markers:
            new_markers[marker.marker_id] = marker

        # Detect added items (in new but not in old)
        added_clips = [new_clips[cid] for cid in new_clips if cid not in old_clips]
        added_transitions = [new_transitions[tid] for tid in new_transitions if tid not in old_transitions]
        added_markers = [new_markers[mid] for mid in new_markers if mid not in old_markers]

        # Detect removed items (in old but not in new)
        removed_clip_ids = [cid for cid in old_clips if cid not in new_clips]
        removed_transition_ids = [tid for tid in old_transitions if tid not in new_transitions]
        removed_marker_ids = [mid for mid in old_markers if mid not in new_markers]

        # Detect modified items (in both but different)
        modified_clips = []
        modified_transitions = []
        modified_markers = []

        for cid, new_clip in new_clips.items():
            if cid in old_clips:
                old_clip = old_clips[cid]
                if self._clip_has_changed(old_clip, new_clip):
                    modified_clips.append(new_clip)

        for tid, new_trans in new_transitions.items():
            if tid in old_transitions:
                old_trans = old_transitions[tid]
                if self._transition_has_changed(old_trans, new_trans):
                    modified_transitions.append(new_trans)

        for mid, new_marker in new_markers.items():
            if mid in old_markers:
                old_marker = old_markers[mid]
                if self._marker_has_changed(old_marker, new_marker):
                    modified_markers.append(new_marker)

        # Detect track changes
        track_changes: Dict[str, Dict[str, Any]] = {}

        old_tracks = {t.track_id: t for t in old_timeline.all_tracks}
        new_tracks = {t.track_id: t for t in new_timeline.all_tracks}
        added_track_ids = tuple(tid for tid in new_tracks if tid not in old_tracks)
        removed_track_ids = tuple(tid for tid in old_tracks if tid not in new_tracks)

        for track_id, new_track in new_tracks.items():
            if track_id in old_tracks:
                old_track = old_tracks[track_id]
                changes = {}

                if old_track.is_muted != new_track.is_muted:
                    changes['is_muted'] = new_track.is_muted
                if old_track.is_solo != new_track.is_solo:
                    changes['is_solo'] = new_track.is_solo
                if old_track.is_locked != new_track.is_locked:
                    changes['is_locked'] = new_track.is_locked
                if old_track.is_visible != new_track.is_visible:
                    changes['is_visible'] = new_track.is_visible
                # 审计修复：sync-lock 纳入增量 diff（undo/另一视图切换后
                # 表头勾选态与模型失步，直到全量 reload 才恢复）
                if (hasattr(old_track, 'is_sync_locked')
                        and old_track.is_sync_locked != new_track.is_sync_locked):
                    changes['is_sync_locked'] = new_track.is_sync_locked
                if hasattr(old_track, 'volume') and hasattr(new_track, 'volume'):
                    if old_track.volume != new_track.volume:
                        changes['volume'] = new_track.volume
                if hasattr(old_track, 'pan') and hasattr(new_track, 'pan'):
                    if old_track.pan != new_track.pan:
                        changes['pan'] = new_track.pan

                if changes:
                    track_changes[track_id] = changes

        # Check if full reload is needed.
        #
        # Cross-track clip moves are deliberately incremental: ClipItem now
        # updates its internal track_id and _layout_track_items() repositions it
        # from the new timeline model. Full reload here makes moving material to
        # a newly created empty track feel like the whole editor stalls.

        old_track_count = len(old_timeline.all_tracks)
        new_track_count = len(new_timeline.all_tracks)

        # Check track order
        old_track_ids = [t.track_id for t in old_timeline.all_tracks]
        new_track_ids = [t.track_id for t in new_timeline.all_tracks]
        track_order_changed = old_track_ids != new_track_ids

        # Check FPS
        fps_changed = False
        if hasattr(old_timeline, 'fps') and hasattr(new_timeline, 'fps'):
            fps_changed = old_timeline.fps != new_timeline.fps

        # P5: Check if any clip moved to a different track
        clip_track_changed = False
        for cid, new_clip in new_clips.items():
            if cid in old_clips:
                if old_clips[cid].track_id != new_clip.track_id:
                    clip_track_changed = True
                    break

        item_change_count = (
            len(added_clips)
            + len(removed_clip_ids)
            + len(modified_clips)
            + len(added_transitions)
            + len(removed_transition_ids)
            + len(modified_transitions)
            + len(added_markers)
            + len(removed_marker_ids)
            + len(modified_markers)
        )
        empty_track_structure_only = (
            (bool(added_track_ids) or bool(removed_track_ids) or track_order_changed)
            and item_change_count == 0
            and not fps_changed
            and not clip_track_changed
        )
        track_structure_changed = (
            old_track_count != new_track_count
            or track_order_changed
            or bool(added_track_ids)
            or bool(removed_track_ids)
        )
        disruptive_track_structure_change = (
            track_structure_changed
            and not empty_track_structure_only
            and (
                bool(added_clips)
                or bool(removed_clip_ids)
                or bool(added_transitions)
                or bool(removed_transition_ids)
                or bool(modified_transitions)
                or bool(added_markers)
                or bool(removed_marker_ids)
                or bool(modified_markers)
            )
        )

        full_reload_needed = (
            not empty_track_structure_only
            and (
                fps_changed or
                disruptive_track_structure_change or
                # Too many changes - fallback to full reload
                (len(added_clips) + len(removed_clip_ids) + len(modified_clips) > 50)
            )
        )

        # SUB-P1-02: compute a real incremental diff for subtitle cues. A
        # structural change (subtitle-track count/order changed) still forces a
        # full reload because the track headers/lanes must be rebuilt; ordinary
        # cue add/remove/modify/track-state edits are applied incrementally.
        (
            sub_added,
            sub_removed_ids,
            sub_modified,
            sub_track_changes,
            subtitle_force_reload,
        ) = self._subtitle_diff(old_timeline, new_timeline)
        if subtitle_force_reload:
            full_reload_needed = True

        return TimelineChanges(
            # BUG-07 fix: Convert lists to tuples for immutable TimelineChanges
            added_clips=tuple(added_clips),
            removed_clip_ids=tuple(removed_clip_ids),
            modified_clips=tuple(modified_clips),
            added_transitions=tuple(added_transitions),
            removed_transition_ids=tuple(removed_transition_ids),
            modified_transitions=tuple(modified_transitions),
            added_markers=tuple(added_markers),
            removed_marker_ids=tuple(removed_marker_ids),
            modified_markers=tuple(modified_markers),
            added_track_ids=added_track_ids,
            removed_track_ids=removed_track_ids,
            track_changes=track_changes,
            added_subtitle_clips=tuple(sub_added),
            removed_subtitle_clip_ids=tuple(sub_removed_ids),
            modified_subtitle_clips=tuple(sub_modified),
            subtitle_track_changes=sub_track_changes,
            full_reload_needed=full_reload_needed
        )

    def _subtitle_diff(
        self,
        old_timeline: Optional[TimelineSequence],
        new_timeline: TimelineSequence,
    ) -> tuple:
        """Compute an incremental diff for subtitle cues & track item state.

        Returns ``(added, removed_ids, modified, track_state_changes,
        force_full_reload)``. A structural change (subtitle-track count or
        ordering) forces a full reload because track headers/lanes must be
        rebuilt. Per-cue add/remove/modify and track lock/visibility changes
        are applied incrementally via :meth:`update_timeline_incremental`.

        SubtitleClip equality is overridden to clip_id only, so content/style
        comparison uses ``to_dict()`` to catch every visual field edit.
        """
        added: list[SubtitleClip] = []
        removed_ids: list[str] = []
        modified: list[SubtitleClip] = []
        track_state_changes: Dict[str, Dict[str, Any]] = {}

        old_sub_tracks = getattr(old_timeline, "subtitle_tracks", ()) or ()
        new_sub_tracks = getattr(new_timeline, "subtitle_tracks", ()) or ()

        if old_timeline is None:
            # First load: every cue is "added"; full reload already triggered
            # by the regular detector (old_timeline is None path).
            for track in new_sub_tracks:
                for sub in track.subtitle_clips:
                    added.append(sub)
            return added, removed_ids, modified, track_state_changes, False

        # Structural change → full reload (headers/lanes need rebuilding).
        if len(old_sub_tracks) != len(new_sub_tracks):
            return added, removed_ids, modified, track_state_changes, True
        for old_track, new_track in zip(old_sub_tracks, new_sub_tracks):
            if old_track.track_id != new_track.track_id:
                return added, removed_ids, modified, track_state_changes, True

        # Build per-track old/new cue maps keyed by clip_id.
        for old_track, new_track in zip(old_sub_tracks, new_sub_tracks):
            old_clips = {c.clip_id: c for c in old_track.subtitle_clips}
            new_clips = {c.clip_id: c for c in new_track.subtitle_clips}

            for cid, new_clip in new_clips.items():
                if cid not in old_clips:
                    added.append(new_clip)
                else:
                    old_clip = old_clips[cid]
                    try:
                        changed = old_clip.to_dict() != new_clip.to_dict()
                    except Exception:
                        changed = old_clip != new_clip
                    if changed:
                        modified.append(new_clip)
            for cid in old_clips:
                if cid not in new_clips:
                    removed_ids.append(cid)

            # Track-level item state (lock/visibility) affects existing cues.
            state: Dict[str, Any] = {}
            if old_track.is_locked != new_track.is_locked:
                state["is_locked"] = new_track.is_locked
            if old_track.is_visible != new_track.is_visible:
                state["is_visible"] = new_track.is_visible
            if old_track.is_enabled != new_track.is_enabled:
                state["is_enabled"] = new_track.is_enabled
            if state:
                track_state_changes[new_track.track_id] = state

        return added, removed_ids, modified, track_state_changes, False

    def _clip_has_changed(self, old_clip: ClipInstance, new_clip: ClipInstance) -> bool:
        """
        Check if clip properties have changed.

        Args:
            old_clip: Previous clip state
            new_clip: New clip state

        Returns:
            True if any property changed
        """
        # Check basic properties
        if old_clip.timeline_start != new_clip.timeline_start:
            return True
        if old_clip.timeline_duration != new_clip.timeline_duration:
            return True
        if old_clip.source_start != new_clip.source_start:
            return True
        if old_clip.source_duration != new_clip.source_duration:
            return True
        if old_clip.track_id != new_clip.track_id:
            return True

        # Check fade in/out
        if old_clip.fade_in_duration != new_clip.fade_in_duration:
            return True
        if old_clip.fade_out_duration != new_clip.fade_out_duration:
            return True

        # Check opacity
        if hasattr(old_clip, 'opacity') and hasattr(new_clip, 'opacity'):
            if old_clip.opacity != new_clip.opacity:
                return True

        # SpeedParams is frozen and therefore comparable as a complete value.
        # Comparing a removed ``stretch_factor`` property previously caused
        # speed-point-only edits to be missed by incremental rendering.
        if old_clip.speed != new_clip.speed:
            return True

        # Check effects
        if old_clip.effects != new_clip.effects:
            return True

        # Check linked clip
        if old_clip.linked_clip_id != new_clip.linked_clip_id:
            return True

        # Check color stack
        if hasattr(old_clip, 'color_stack_id') and hasattr(new_clip, 'color_stack_id'):
            if old_clip.color_stack_id != new_clip.color_stack_id:
                return True
        if hasattr(old_clip, 'color_shared') and hasattr(new_clip, 'color_shared'):
            if old_clip.color_shared != new_clip.color_shared:
                return True

        # Check enabled state
        if old_clip.is_enabled != new_clip.is_enabled:
            return True

        # Check label color
        if old_clip.label_color != new_clip.label_color:
            return True
        if getattr(old_clip, "flag_color", None) != getattr(
                new_clip, "flag_color", None):
            return True

        # Check audio waveform display mode
        if getattr(old_clip, "waveform_show_channels", False) != getattr(
            new_clip, "waveform_show_channels", False
        ):
            return True

        return False

    def _transition_has_changed(
        self,
        old_trans: TransitionInstance,
        new_trans: TransitionInstance
    ) -> bool:
        """
        Check if transition properties have changed.

        Args:
            old_trans: Previous transition state
            new_trans: New transition state

        Returns:
            True if any property changed
        """
        if old_trans.center_frame != new_trans.center_frame:
            return True
        if old_trans.duration != new_trans.duration:
            return True
        if old_trans.track_id != new_trans.track_id:
            return True
        if hasattr(old_trans, 'transition_type') and hasattr(new_trans, 'transition_type'):
            if old_trans.transition_type != new_trans.transition_type:
                return True
        if getattr(old_trans, 'alignment', None) != getattr(new_trans, 'alignment', None):
            return True
        if getattr(old_trans, 'params', None) != getattr(new_trans, 'params', None):
            return True
        return False

    def _marker_has_changed(
        self,
        old_marker: TimelineMarker,
        new_marker: TimelineMarker
    ) -> bool:
        """
        Check if marker properties have changed.

        Args:
            old_marker: Previous marker state
            new_marker: New marker state

        Returns:
            True if any property changed
        """
        if old_marker.frame != new_marker.frame:
            return True
        if old_marker.name != new_marker.name:
            return True
        if old_marker.color != new_marker.color:
            return True
        if old_marker.comment != new_marker.comment:
            return True
        if old_marker.clip_id != new_marker.clip_id:
            return True
        if old_marker.track_id != new_marker.track_id:
            return True
        return False

    def update_timeline_incremental(self, changes: TimelineChanges) -> None:
        """
        Update timeline incrementally based on detected changes (Phase 7.2).

        避免全量重建导致的闪烁和性能问题。
        只更新变化的 clip/transition/marker/track。

        Args:
            changes: TimelineChanges detected from _clip_change_detector
        """
        if self._updating:
            return

        # If full reload needed, fall back to load_timeline
        if changes.full_reload_needed:
            if self._timeline is not None:
                self.load_timeline(self._timeline)
            return

        self._updating = True
        try:
            timeline_media_changed = bool(
                changes.added_clips
                or changes.removed_clip_ids
                or changes.modified_clips
            )

            if timeline_media_changed:
                # Disconnect removed ClipItems before the provider publishes
                # cancellation results. Modified items remain connected but
                # will request their new range after this invalidation.
                for clip_id in changes.removed_clip_ids:
                    item = self._items_map.get(clip_id)
                    if isinstance(item, ClipItem):
                        item.dispose()
                self._invalidate_timeline_media_requests()

            # Delete removed clips
            for clip_id in changes.removed_clip_ids:
                if clip_id in self._items_map:
                    item = self._items_map[clip_id]
                    self._scene.removeItem(item)
                    del self._items_map[clip_id]
                    logger.debug(f"Incremental update: removed clip {clip_id}")

            # Delete removed transitions
            for trans_id in changes.removed_transition_ids:
                if trans_id in self._items_map:
                    item = self._items_map[trans_id]
                    self._scene.removeItem(item)
                    del self._items_map[trans_id]
                    logger.debug(f"Incremental update: removed transition {trans_id}")

            # Delete removed markers
            for marker_id in changes.removed_marker_ids:
                if marker_id in self._marker_items:
                    item = self._marker_items[marker_id]
                    self._scene.removeItem(item)
                    del self._marker_items[marker_id]
                    logger.debug(f"Incremental update: removed marker {marker_id}")

            # SUB-P1-02: Delete removed subtitle cues.
            for sub_id in changes.removed_subtitle_clip_ids:
                if sub_id in self._items_map:
                    item = self._items_map[sub_id]
                    self._scene.removeItem(item)
                    del self._items_map[sub_id]
                    logger.debug(f"Incremental update: removed subtitle cue {sub_id}")

            # Create new clips
            for clip in changes.added_clips:
                self._create_clip_item(clip)
                logger.debug(f"Incremental update: added clip {clip.clip_id}")

            # Layout all items after adding new clips (fixes Y=0 positioning)
            if changes.added_clips:
                self._layout_track_items()

            # SUB-P1-02: Create new subtitle cues.
            if changes.added_subtitle_clips and self._timeline is not None:
                sub_track_by_id = {
                    t.track_id: t for t in self._timeline.subtitle_tracks
                }
                for sub in changes.added_subtitle_clips:
                    owner = sub_track_by_id.get(sub.track_id)
                    if owner is None:
                        # Fallback: search by presence (defensive).
                        for t in self._timeline.subtitle_tracks:
                            if any(c.clip_id == sub.clip_id for c in t.subtitle_clips):
                                owner = t
                                break
                    if owner is not None:
                        self._create_subtitle_item(sub, owner)
                        logger.debug(f"Incremental update: added subtitle cue {sub.clip_id}")
                self._layout_track_items()

            # Create new transitions
            for trans in changes.added_transitions:
                self._create_transition_item(trans)
                logger.debug(f"Incremental update: added transition {trans.transition_id}")

            # Layout all items after adding transitions
            if changes.added_transitions:
                self._layout_track_items()

            # Create new markers
            for marker in changes.added_markers:
                self._create_marker_item(marker)
                logger.debug(f"Incremental update: added marker {marker.marker_id}")

            track_structure_changed = bool(changes.added_track_ids or changes.removed_track_ids)
            if track_structure_changed:
                self._sync_track_headers_incremental()
                logger.debug(
                    "Incremental update: track headers synced "
                    f"added={changes.added_track_ids}, removed={changes.removed_track_ids}"
                )

            # Update modified clips
            for clip in changes.modified_clips:
                if clip.clip_id in self._items_map:
                    item = self._items_map[clip.clip_id]
                    if isinstance(item, ClipItem):
                        item.update_clip_properties(
                            clip.with_linked_clip_id(self._get_valid_linked_clip_id(clip))
                        )
                        logger.debug(f"Incremental update: modified clip {clip.clip_id}")

            # SUB-P1-02: Update modified subtitle cues in place. update_clip_data
            # refreshes text/start/duration/enabled; the subsequent layout pass
            # repositions the chip.
            for sub in changes.modified_subtitle_clips:
                item = self._items_map.get(sub.clip_id)
                if isinstance(item, SubtitleClipItem):
                    item.update_clip_data(
                        text=sub.text,
                        start_frame=sub.timeline_start,
                        duration_frames=sub.timeline_duration,
                        is_enabled=sub.is_enabled,
                    )
                    logger.debug(f"Incremental update: modified subtitle cue {sub.clip_id}")

            # SUB-P1-02: Apply subtitle-track item-state changes (lock/visibility/
            # enabled) to existing cues without rebuilding the scene.
            if changes.subtitle_track_changes:
                for track in getattr(self._timeline, "subtitle_tracks", ()) or ():
                    state = changes.subtitle_track_changes.get(track.track_id)
                    if not state:
                        continue
                    for sub in track.subtitle_clips:
                        item = self._items_map.get(sub.clip_id)
                        if not isinstance(item, SubtitleClipItem):
                            continue
                        if "is_locked" in state:
                            item.set_track_locked(state["is_locked"])
                    # Visibility/enabled propagate via the layout pass below
                    # (which reads track.is_visible/is_enabled).

            # Re-layout if clips were modified (position may have changed)
            if changes.modified_clips or changes.modified_subtitle_clips or changes.subtitle_track_changes:
                self._layout_track_items()

            # Update modified transitions
            for trans in changes.modified_transitions:
                if trans.transition_id in self._items_map:
                    item = self._items_map[trans.transition_id]
                    if isinstance(item, TransitionItem):
                        # Transition update method
                        item.update_transition_properties(trans)
                        logger.debug(f"Incremental update: modified transition {trans.transition_id}")

            # Update modified markers
            for marker in changes.modified_markers:
                if marker.marker_id in self._marker_items:
                    item = self._marker_items[marker.marker_id]
                    if isinstance(item, MarkerItem):
                        # MarkerItem accepts individual visual properties;
                        # passing the model object as ``frame`` causes a
                        # later frame * pixels-per-frame TypeError.
                        item.update_marker(
                            frame=marker.frame,
                            name=marker.name,
                            color=marker.color,
                        )
                        logger.debug(f"Incremental update: modified marker {marker.marker_id}")

            # Update track headers
            for track_id, track_changes in changes.track_changes.items():
                # AUD-21102: 锁轨变化同步到该轨的片段与转场 item
                if 'is_locked' in track_changes:
                    lock_state = bool(track_changes['is_locked'])
                    for item in self._items_map.values():
                        if (
                            isinstance(item, (ClipItem, TransitionItem))
                            and item.track_id == track_id
                        ):
                            item.set_track_locked(lock_state)
                if track_id in self._track_headers:
                    header = self._track_headers[track_id]
                    if 'is_muted' in track_changes:
                        header.set_muted(track_changes['is_muted'])
                    if 'is_solo' in track_changes:
                        header.set_solo(track_changes['is_solo'])
                    if 'is_locked' in track_changes:
                        header.set_locked(track_changes['is_locked'])
                    # AUD-21401: sync-lock 是独立变更键（变更检测器单独写
                    # 入），必须与 is_muted/is_solo/is_locked 平级处理；
                    # 此前嵌套在 is_locked 分支内，仅改 sync-lock 的命令
                    # （如撤销 SetTrackSyncLockCommand）永不刷新表头。
                    if 'is_sync_locked' in track_changes and hasattr(
                            header, 'set_sync_locked'):
                        header.set_sync_locked(track_changes['is_sync_locked'])
                    if 'is_visible' in track_changes:
                        header.set_visible(track_changes['is_visible'])
                    if 'volume' in track_changes:
                        header.set_volume(track_changes['volume'])
                        # 音量变化同步到该轨波形显示（等比缩放，不重生成）
                        self.set_track_waveform_gain_preview(
                            track_id, track_changes['volume'])
                    logger.debug(f"Incremental update: modified track {track_id}")

            if track_structure_changed:
                self._layout_track_items()

            # Update scene rect if needed
            if (
                changes.added_clips
                or changes.removed_clip_ids
                or changes.added_subtitle_clips
                or changes.removed_subtitle_clip_ids
                or track_structure_changed
            ):
                self._update_scene_rect()

            # Update clip visibility
            self._update_clip_visibility()

            # Update gap indicators
            self._update_gap_indicators()

            # Update snap points
            if self._timeline is not None:
                self._snap_manager.update_from_timeline(
                    self._timeline,
                    self._playhead_frame
                )

            logger.info(
                f"Incremental update completed: "
                f"{len(changes.added_clips)} added, "
                f"{len(changes.removed_clip_ids)} removed, "
                f"{len(changes.modified_clips)} modified clips"
            )

        finally:
            self._updating = False

    def _create_clip_item(self, clip: ClipInstance) -> None:
        """
        Create a single ClipItem and add to scene.

        Used for incremental update when adding new clips.

        Args:
            clip: ClipInstance to create visual item for
        """
        if self._timeline is None:
            return

        # Find track index
        all_tracks = self._timeline.all_tracks
        track_index = 0
        track = None
        for i, t in enumerate(all_tracks):
            if t.track_id == clip.track_id:
                track_index = i
                track = t
                break

        if track is None:
            logger.warning(f"Track not found for clip {clip.clip_id}")
            return

        # Get asset info
        asset_name = self._get_clip_display_name(clip)
        media_type = self._get_asset_media_type(clip.asset_id)
        if track.track_type == "audio":
            media_type = "audio"

        asset_path = self._get_asset_path(clip.asset_id)
        asset_fps = self._get_asset_fps(clip.asset_id)
        waveform_asset_id = self._get_waveform_asset_id(clip.asset_id)

        # Create item
        item = ClipItem(
            clip.clip_id,
            track.track_id,
            asset_name,
            clip.timeline_start,
            clip.timeline_duration,
            self._pixels_per_frame,
            track_index,
            source_start=clip.source_start,
            source_duration=clip.source_duration,
            max_source_frames=self._get_asset_duration(clip.asset_id),
            is_track_locked=track.is_locked,
            media_type=media_type,
            linked_clip_id=self._get_valid_linked_clip_id(clip),
            asset_id=clip.asset_id,
            asset_path=asset_path,
            waveform_asset_id=waveform_asset_id,
            fps=asset_fps,
            fade_in_duration=clip.fade_in_duration,
            fade_out_duration=clip.fade_out_duration,
            speed=clip.speed if isinstance(clip.speed, (int, float)) else clip.speed.speed,
            retime_points=(
                tuple((point.source_offset, point.speed) for point in clip.speed.speed_points)
                if isinstance(clip.speed, SpeedParams) else ()
            ),
            retime_controls_visible=clip.clip_id in self._retime_control_clip_ids,
            # RI-06/R17：增量创建同样携带 clip 自身 enabled
            is_enabled=clip.is_enabled,
            color_stack_id=clip.color_stack_id if hasattr(clip, 'color_stack_id') else None,
            color_shared=clip.color_shared if hasattr(clip, 'color_shared') else False,
            label_color=clip.label_color if hasattr(clip, 'label_color') else None,
            flag_color=getattr(clip, "flag_color", None),
            waveform_show_channels=getattr(clip, "waveform_show_channels", False),
        )

        # Set visual properties
        if hasattr(clip, 'opacity'):
            item._opacity = clip.opacity
        if hasattr(clip, 'speed'):
            speed = clip.speed
            if isinstance(speed, (int, float)):
                item._speed = speed
            else:
                item._speed = getattr(speed, 'speed', 1.0)
                item._retime_points = tuple(
                    (point.source_offset, point.speed)
                    for point in getattr(speed, 'speed_points', ())
                )
        if hasattr(clip, 'effects'):
            item._effects = clip.effects

        # Phase 6: Propagate comp clip state from ClipInstance to ClipItem
        if hasattr(clip, 'composite_id') and clip.composite_id:
            item._composite_id = clip.composite_id
        # Determine if this clip's asset is a comp_clip
        if media_type == "comp_clip":
            item._is_comp_clip = True
        # Check project for comp_enabled and comp_cache_status via asset metadata
        if self._project is not None and clip.asset_id:
            asset = self._project.assets.get(clip.asset_id)
            if asset is not None:
                meta = asset.metadata or {}
                comp_enabled = meta.get("comp_enabled", None)
                comp_cache_status = meta.get("comp_cache_status", None)
                if comp_enabled is not None:
                    item._comp_enabled = bool(comp_enabled)
                if comp_cache_status is not None:
                    item._comp_cache_status = str(comp_cache_status)
                # Solid Color generator: carry the asset's RGB so the badge
                # swatch and paint path can render it.
                if media_type == "solid":
                    sc = getattr(asset, "solid_color", None)
                    if sc is not None:
                        item._solid_color = sc

        self._scene.addItem(item)
        self._items_map[clip.clip_id] = item
        self._configure_clip_presentation(item)
        # 轨道音量驱动的波形显示缩放（W-gain: 视觉联动）
        item.set_waveform_gain(getattr(track, 'volume', 1.0))

        # Connect signals
        item.clip_clicked.connect(self._on_clip_clicked)
        # INT-02：片段拖拽/修剪纳入 InteractionSession（Esc 取消）
        item.interaction_started.connect(self._on_item_interaction_started)
        item.interaction_finished.connect(self._on_item_interaction_finished)
        item.clip_moved.connect(self._on_clip_moved)
        item.clip_trim_left.connect(self._on_clip_trim_left)
        item.clip_trim_right.connect(self._on_clip_trim_right)
        item.motion_graphic_duration_changed.connect(
            self._on_motion_graphic_duration_changed
        )
        item.motion_graphic_duration_change_finished.connect(
            self._on_motion_graphic_duration_change_finished
        )
        item.clip_delete.connect(self._on_clip_delete)
        item.clip_split.connect(self._on_clip_split_requested)
        item.clip_duplicate.connect(self._on_clip_duplicate_requested)
        item.drag_finished.connect(self._on_drag_finished)
        item.trim_left_finished.connect(self._on_trim_left_finished)
        item.trim_right_finished.connect(self._on_trim_right_finished)
        item.rate_stretch_finished.connect(self._on_rate_stretch_finished)
        item.retime_stretch_finished.connect(self._on_retime_stretch_finished)
        item.retime_action_requested.connect(self._on_retime_action_requested)
        item.retime_point_dragged.connect(self._on_retime_point_dragged)
        item.retime_point_move_finished.connect(self._on_retime_point_move_finished)
        item.fade_end_curve_requested.connect(self._on_fade_end_curve_requested)
        item.clip_slip.connect(self._on_clip_slip)
        item.slip_finished.connect(self._on_slip_finished)
        item.clip_slide.connect(self._on_clip_slide)
        item.slide_finished.connect(self._on_slide_finished)
        item.ripple_trim_left.connect(self._on_ripple_trim_left)
        item.ripple_trim_right.connect(self._on_ripple_trim_right)
        item.ripple_trim_left_finished.connect(self._on_ripple_trim_left_finished)
        item.ripple_trim_right_finished.connect(self._on_ripple_trim_right_finished)
        item.add_transition_requested.connect(self._on_add_transition_requested)
        item.open_in_composite_requested.connect(self._on_open_in_composite_requested)
        item.open_in_color_requested.connect(self.open_in_color_requested.emit)
        # Phase 1.2: fade handles must also work for clips created through the
        # incremental path (for example, newly inserted audio clips).
        item.fade_in_changed.connect(self._on_fade_in_preview)
        item.fade_out_changed.connect(self._on_fade_out_preview)
        item.fade_finished.connect(self._on_fade_finished)
        # Scene Cut Detection (P9): forward clip scene cut request
        item.scene_cut_detect_requested.connect(self.scene_cut_detect_requested.emit)
        item.speech_to_text_requested.connect(self.speech_to_text_requested.emit)
        item.linked_group_hovered.connect(self._on_linked_group_hovered)
        item.track_highlight_requested.connect(self._on_track_highlight_requested)
        # Phase 6: Connect Comp Clip context menu signals
        item.convert_to_comp_clip_requested.connect(self.convert_to_comp_clip_requested.emit)
        item.remove_composite_binding_requested.connect(self.remove_composite_binding_requested.emit)
        item.bypass_composite_requested.connect(self.bypass_composite_requested.emit)
        item.reset_composite_requested.connect(self.reset_composite_requested.emit)
        item.reveal_comp_clip_in_media_pool_requested.connect(self.reveal_comp_clip_in_media_pool_requested.emit)
        item.render_cache_clip_requested.connect(self.render_cache_clip_requested.emit)
        item.rerender_cache_clip_requested.connect(self.rerender_cache_clip_requested.emit)
        item.use_shared_grade_requested.connect(self.use_shared_grade_requested.emit)
        item.make_local_grade_copy_requested.connect(self.make_local_grade_copy_requested.emit)
        # P2: Label color change
        item.label_color_change_requested.connect(self._on_label_color_change_requested)
        item.waveform_display_mode_requested.connect(
            self._on_waveform_display_mode_requested
        )
        # Enable/Disable and Link/Unlink
        item.clip_enable_toggle_requested.connect(self._on_clip_enable_toggle)
        item.clip_unlink_requested.connect(self._on_clip_unlink)
        item.clip_link_requested.connect(self._on_clip_link)
        item.numeric_trim_requested.connect(
            self._on_clip_numeric_trim_requested)
        # P1-02: 嵌套序列入口
        item.create_nested_from_selection_requested.connect(
            self._on_create_nested_menu_requested)
        item.enter_nested_sequence_requested.connect(
            self._on_enter_nested_clip_requested)
        item.change_speed_dialog_requested.connect(self._on_change_clip_speed_dialog_requested)
        item.speed_change_requested.connect(self._on_clip_speed_change)
        item.retime_controls_toggled.connect(self._on_retime_controls_toggled)
        item.retime_speed_point_requested.connect(self._on_retime_speed_point_requested)
        item.freeze_frame_requested.connect(self._on_freeze_frame_requested)
        item.retime_point_easing_requested.connect(self._on_retime_point_easing_requested)
        item.replace_placeholder_requested.connect(self._on_replace_placeholder_requested)
        item.audio_gain_dialog_requested.connect(self._on_audio_gain_dialog_requested)
        # E09: 增量添加的片段同样接双击 → Source Viewer（与整建路径一致）
        item.clip_double_clicked.connect(self._on_clip_double_clicked)
        item.retime_speed_points_clear_requested.connect(self._on_retime_speed_points_clear_requested)
        item.retime_segment_speed_requested.connect(self._on_retime_segment_speed_requested)

    def _apply_subtitle_drag_clamp(
        self,
        track_id: str,
        clip_id: str,
        mode: str,
        orig_start: int,
        orig_dur: int,
        proposed_start: int,
        proposed_dur: int,
    ) -> tuple[int, int]:
        """SUB-P0-04: clamp a live subtitle drag to the legal range.

        Reads the current neighbours + lock state from ``self._timeline`` so
        the clamp is correct even after an undo/reorder. Returns
        ``(clamped_start, clamped_duration)``. When the policy cannot resolve
        (no timeline / unknown track), the proposal is returned unchanged.
        """
        if self._timeline is None:
            return proposed_start, proposed_dur
        from src.features.edit.core.captions.edit_policy import SubtitleEditPolicy

        track = self._timeline.get_track_by_id(track_id)
        if track is None or track.track_type != "subtitle":
            return proposed_start, proposed_dur
        neighbours = tuple(track.subtitle_clips)
        locked = bool(getattr(track, "is_locked", False))

        if mode == "move":
            # Normal same-track moves now use overwrite semantics on release;
            # do not clamp their visual preview away from the occupied span.
            # A locked track still refuses the drag immediately.
            if locked:
                return int(orig_start), int(orig_dur)
            return max(0, int(proposed_start)), int(orig_dur)

        # trim (left or right) share the trim range.
        rng = SubtitleEditPolicy.compute_trim_range(
            clip_id=clip_id, current_start=int(orig_start),
            current_duration=int(orig_dur),
            neighbours=neighbours, track_locked=locked,
        )
        if not rng.editable:
            return int(orig_start), int(orig_dur)
        start = rng.clamp_start(int(proposed_start))
        dur = rng.clamp_duration(int(proposed_dur))
        return start, dur

    def _make_subtitle_drag_clamp_callback(self, track_id: str, clip_id: str):
        """Build a drag-clamp closure bound to a specific cue for an item."""
        def _clamp(mode, orig_start, orig_dur, proposed_start, proposed_dur):
            return self._apply_subtitle_drag_clamp(
                track_id, clip_id, mode,
                orig_start, orig_dur, proposed_start, proposed_dur,
            )
        return _clamp

    def _create_subtitle_item(self, sub: SubtitleClip, sub_track: EditTrack) -> None:
        """Create a single SubtitleClipItem and add it to the scene.

        SUB-P1-02: incremental creation path mirroring the load_timeline block.
        The item is registered in the shared ``_items_map`` (cue ids are UUIDs
        so they cannot collide with regular clip ids). Positioning is left to
        the subsequent ``_layout_track_items()`` pass, consistent with how
        regular added clips are handled.
        """
        track_h = self._track_height_for(sub_track)
        sub_item = SubtitleClipItem(
            clip_id=sub.clip_id,
            track_id=sub_track.track_id,
            text=sub.text,
            start_frame=sub.timeline_start,
            duration_frames=sub.timeline_duration,
            pixels_per_frame=self._pixels_per_frame,
            track_height=track_h,
            is_enabled=sub.is_enabled,
            is_track_locked=sub_track.is_locked,
            label_color=sub.label_color,
        )
        x = self._layout_engine.frame_to_x(sub.timeline_start)
        track_top = self._layout_engine.track_top(sub_track.track_id)
        sub_item.setPos(x, track_top)
        self._scene.addItem(sub_item)
        self._items_map[sub.clip_id] = sub_item

        # SUB-P0-04: inject the live drag-clamp callback so a move/trim stays
        # within the legal range in real time (not only restored on failure).
        sub_item.set_drag_clamp_callback(
            self._make_subtitle_drag_clamp_callback(sub_track.track_id, sub.clip_id)
        )

        # Connect the same subtitle signals wired during load_timeline.
        sub_item.subtitle_clip_clicked.connect(self._on_subtitle_clip_clicked)
        sub_item.subtitle_clip_moved.connect(self._on_subtitle_clip_moved)
        sub_item.subtitle_clip_drag_preview.connect(self._on_subtitle_clip_drag_preview)
        sub_item.subtitle_clip_drag_finished.connect(self._on_subtitle_clip_drag_finished)
        sub_item.subtitle_clip_trim_left.connect(self._on_subtitle_clip_trim_left)
        sub_item.subtitle_clip_trim_right.connect(self._on_subtitle_clip_trim_right)
        sub_item.subtitle_clip_trim_left_finished.connect(self._on_subtitle_clip_trim_left_finished)
        sub_item.subtitle_clip_trim_right_finished.connect(self._on_subtitle_clip_trim_right_finished)
        sub_item.subtitle_clip_delete.connect(self._on_subtitle_clip_delete)
        sub_item.subtitle_clip_context_menu_requested.connect(
            self._on_subtitle_clip_context_menu_requested
        )
        sub_item.subtitle_clip_edit_text_requested.connect(self._on_subtitle_clip_edit_text)

    def _create_transition_item(self, trans: TransitionInstance) -> None:
        """
        Create a single TransitionItem and add to scene.

        Used for incremental update when adding new transitions.

        Args:
            trans: TransitionInstance to create visual item for
        """
        if self._timeline is None:
            return

        # Find track index
        all_tracks = self._timeline.all_tracks
        track_index = 0
        track = None
        for i, t in enumerate(all_tracks):
            if t.track_id == trans.track_id:
                track_index = i
                track = t
                break

        item = TransitionItem(
            trans.transition_id,
            trans.track_id,
            trans.center_frame,
            trans.duration,
            self._pixels_per_frame,
            track_index,
            trans.transition_type,
            trans.alignment,
            is_track_locked=bool(track.is_locked) if track is not None else False,  # AUD-21102
        )

        self._scene.addItem(item)
        self._items_map[trans.transition_id] = item

        # Connect signals
        item.transition_clicked.connect(self._on_transition_clicked)
        item.transition_delete.connect(self._on_transition_delete)
        item.transition_trim_left.connect(self._on_transition_trim_left)
        item.transition_trim_right.connect(self._on_transition_trim_right)
        item.trim_left_finished.connect(self._on_transition_trim_left_finished)
        item.trim_right_finished.connect(self._on_transition_trim_right_finished)
        item.transition_moved.connect(self._on_transition_moved)
        item.move_finished.connect(self._on_transition_move_finished)

    def _create_marker_item(self, marker: TimelineMarker) -> None:
        """
        Create a single MarkerItem and add to scene.

        Used for incremental update when adding new markers.

        Args:
            marker: TimelineMarker to create visual item for
        """
        if self._timeline is None:
            return

        fps = float(self._timeline.fps) if self._timeline.fps else EMPTY_TIMELINE_FPS
        num_tracks = len(self._timeline.all_tracks)

        marker_item = MarkerItem(
            marker.marker_id,
            marker.frame,
            marker.name,
            marker.color,
            self._pixels_per_frame,
            num_tracks,
            fps,
            clip_id=marker.clip_id,
            track_id=marker.track_id,
        )
        marker_item.update_target_y(self._marker_target_y(marker))

        self._scene.addItem(marker_item)
        self._marker_items[marker.marker_id] = marker_item

        # Connect signals
        marker_item.marker_clicked.connect(self._on_marker_clicked)
        marker_item.marker_moved.connect(self._on_marker_moved)
        marker_item.marker_move_finished.connect(self._on_marker_move_finished)
        marker_item.marker_double_clicked.connect(self._on_marker_double_clicked)
        marker_item.marker_context_menu_requested.connect(
            self._show_marker_context_menu
        )

    def _marker_target_y(self, marker: TimelineMarker) -> float:
        """Return the ruler or an interior clip-lane Y coordinate for a marker."""
        if marker.track_id and self._timeline is not None:
            track = self._timeline.get_track_by_id(marker.track_id)
            if track is not None:
                self._configure_layout_engine()
                track_top = self._layout_engine.track_top(track.track_id)
                return track_top + MarkerItem.CLIP_MARKER_INSET_Y
        return float(RULER_HEIGHT)

    def _preview_scoped_marker_move(
        self,
        clip_id: str,
        old_start: int,
        new_start: int,
        target_track_id: str,
    ) -> None:
        """Move scoped MarkerItems during a clip drag preview.

        The timeline model is updated only when the drag is committed.  Keep
        the visual marker in sync during the drag, then resync from the model
        when the drag finishes or is rejected.
        """
        if self._timeline is None:
            return

        delta = new_start - old_start
        preview_marker = None
        for marker in self._timeline.markers:
            if marker.clip_id != clip_id:
                continue
            marker_item = self._marker_items.get(marker.marker_id)
            if marker_item is None:
                continue
            preview_marker = replace(
                marker,
                frame=max(0, marker.frame + delta),
                track_id=target_track_id,
            )
            marker_item.update_marker(frame=preview_marker.frame)
            marker_item.update_target_y(self._marker_target_y(preview_marker))

    def _sync_marker_items_to_timeline(self) -> None:
        """Restore MarkerItem geometry from the committed timeline model."""
        if self._timeline is None:
            return
        for marker in self._timeline.markers:
            marker_item = self._marker_items.get(marker.marker_id)
            if marker_item is None:
                continue
            marker_item.update_marker(
                frame=marker.frame,
                name=marker.name,
                color=marker.color,
            )
            marker_item.update_target_y(self._marker_target_y(marker))

    # ==================== Clip Action Handlers ====================

    def _get_asset_name(self, asset_id: str) -> str:
        """
        Get asset display name from project assets.

        Args:
            asset_id: Asset identifier

        Returns:
            Display name or path basename if display_name is empty
        """
        if self._project is None:
            return asset_id[:12]

        asset = self._project.assets.get(asset_id)
        if asset is None:
            return asset_id[:12]

        if asset.display_name:
            return asset.display_name

        import os
        return os.path.basename(asset.path) if asset.path else asset_id[:12]

    def _get_clip_display_name(self, clip: Any) -> str:
        """Return the timeline label for a clip, including Comp Clip names.

        A timeline-created Comp Clip deliberately keeps the original
        ``asset_id`` as its source reference.  Looking only at that asset
        would therefore render the source filename and make the converted
        clip indistinguishable from a normal clip.  Resolve the graph name
        first, then fall back to the comp asset metadata for older projects.
        """
        composite_id = getattr(clip, "composite_id", None)
        if composite_id and self._project is not None:
            graph = self._project.compositions.get(str(composite_id))
            graph_name = getattr(graph, "name", None)
            if graph_name is None and isinstance(graph, dict):
                graph_name = graph.get("name")
            if graph_name:
                return str(graph_name)

            for value in self._project.comp_clips.values():
                graph_id = getattr(value, "graph_id", None)
                name = getattr(value, "name", None)
                if isinstance(value, dict):
                    graph_id = value.get("graph_id")
                    name = value.get("name")
                if str(graph_id or "") == str(composite_id) and name:
                    return str(name)

        return self._get_asset_name(clip.asset_id)

    def _get_asset_duration(self, asset_id: str) -> int:
        """
        Get asset duration from asset metadata.

        Args:
            asset_id: Asset identifier

        Returns:
            Maximum source frames available, or DEFAULT_FALLBACK_DURATION if not found

        Note:
            DEFAULT_FALLBACK_DURATION (300 frames ≈ 12s at 25fps) is used as a
            reasonable default for images/unknown media. Very long fallbacks
            like 100000 frames could allow trimming beyond actual content.
        """
        # BUG-10 fix: Use reasonable fallback instead of arbitrary 100000
        DEFAULT_FALLBACK_DURATION = 300  # ~12 seconds at 25fps

        if self._project is None:
            logger.debug(f"_get_asset_duration: project is None, using fallback {DEFAULT_FALLBACK_DURATION}")
            return DEFAULT_FALLBACK_DURATION

        asset = self._project.assets.get(asset_id)
        if asset is None:
            logger.debug(f"_get_asset_duration: asset {asset_id} not found, using fallback {DEFAULT_FALLBACK_DURATION}")
            return DEFAULT_FALLBACK_DURATION

        # Motion Graphic assets are virtual scene references and deliberately
        # have no media-file ``total_frames`` field.  Their authored duration
        # lives in metadata; use it as the trim ceiling so a 15-second scene
        # is not incorrectly capped by the generic 12-second fallback.
        metadata_duration = int(
            (getattr(asset, "metadata", {}) or {}).get("duration_frames", 0) or 0
        )
        if metadata_duration > 0:
            return metadata_duration

        if asset.total_frames is not None and asset.total_frames > 0:
            return asset.total_frames

        if asset.duration_seconds is not None and asset.duration_seconds > 0:
            fps = asset.fps or (float(self._timeline.fps) if self._timeline else EMPTY_TIMELINE_FPS)
            return int(asset.duration_seconds * fps)

        logger.debug(f"_get_asset_duration: asset {asset_id} has no duration info, using fallback {DEFAULT_FALLBACK_DURATION}")
        return DEFAULT_FALLBACK_DURATION

    def _get_asset_media_type(self, asset_id: str) -> str:
        """
        Get asset media type from project assets.

        Args:
            asset_id: Asset identifier

        Returns:
            Media type ('video', 'audio', 'image', 'image_sequence'), default 'video'
        """
        if self._project is None:
            return "video"  # Default to video

        asset = self._project.assets.get(asset_id)
        if asset is None:
            return "video"  # Default to video

        return asset.media_type

    def _get_asset_path(self, asset_id: str) -> Optional[str]:
        """
        Get asset file path from project assets.

        Args:
            asset_id: Asset identifier

        Returns:
            File path or None
        """
        if self._project is None:
            return None

        asset = self._project.assets.get(asset_id)
        if asset is None:
            return None

        return asset.effective_path

    def _get_waveform_asset_id(self, asset_id: str) -> str:
        """Return the cache identity used for source waveform extraction.

        Scene-cut subclips are virtual assets: each has a unique media-pool
        identity but all reference the same parent media file.  Waveform
        extraction is for the source file, so using the parent ID prevents a
        batch drop from starting one full decode per subclip.
        """
        if self._project is None:
            return asset_id
        asset = self._project.assets.get(asset_id)
        if asset is None:
            return asset_id
        metadata = getattr(asset, "metadata", {}) or {}
        if metadata.get("is_subclip"):
            parent_asset_id = str(metadata.get("parent_asset_id", "") or "")
            if parent_asset_id:
                return parent_asset_id
        return asset_id

    def _get_asset_fps(self, asset_id: str) -> float:
        """
        Get asset frame rate from project assets.

        Args:
            asset_id: Asset identifier

        Returns:
            Frame rate (default 25.0)
        """
        if self._project is None:
            return EMPTY_TIMELINE_FPS

        asset = self._project.assets.get(asset_id)
        if asset is None:
            return float(self._timeline.fps) if self._timeline is not None else EMPTY_TIMELINE_FPS

        if asset.fps and asset.fps > 0:
            return float(asset.fps)

        if self._timeline is not None:
            return float(self._timeline.fps)

        return EMPTY_TIMELINE_FPS

    def _on_clip_moved(self, clip_id: str, track_id: str, new_start: int, scene_y: float) -> None:
        """Handle clip moved during drag (live preview only, P2-3 / P3 / P4).

        P3: Builds a MoveGroup with all linked and selected clips so that
        preview positions are updated simultaneously for every member.

        P4: Handles cross-track drag detection and auto-create track logic.
        - Video clips dragged above top video track → auto-create new video track
        - Audio clips dragged below bottom audio track → auto-create new audio track
        """
        if self._timeline is None or self._updating:
            return

        try:
            self._on_clip_moved_impl(clip_id, track_id, new_start, scene_y)
        except Exception:
            logger.exception(f"Error in _on_clip_moved for clip {clip_id}")

    def _on_clip_moved_impl(
        self,
        clip_id: str,
        track_id: str,
        new_start: int,
        scene_y: float,
    ) -> None:
        """Implementation of _on_clip_moved with error handling."""
        # Check for Shift key to temporarily disable snap (参考 kdenlive)
        modifiers = QApplication.keyboardModifiers()
        self._snap_temporarily_disabled = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        item = self._items_map.get(clip_id)
        if not (item and isinstance(item, ClipItem)):
            return

        # P2-2 / P3 / P4: Create drag session on first move if not yet created
        if self._current_drag_session is None:
            clip = self._timeline.get_clip_by_id(clip_id)
            linked_ids: list[str] = []
            selected_ids: list[str] = []
            original_positions: dict[str, int] = {clip_id: item._start_frame}
            original_track_ids: dict[str, str] = {clip_id: track_id}
            current_track_ids: dict[str, str] = {clip_id: track_id}

            # Collect linked clip (if linked selection is enabled)
            linked_clip_obj = self._get_reciprocal_linked_clip(clip)
            if linked_clip_obj is not None and (
                    self._linked_selection_enabled if item._copy_mode else self._effective_linked_selection()):
                linked_ids.append(linked_clip_obj.clip_id)
                linked_item = self._items_map.get(linked_clip_obj.clip_id)
                if linked_item and isinstance(linked_item, ClipItem):
                    original_positions[linked_clip_obj.clip_id] = linked_item._start_frame
                    original_track_ids[linked_clip_obj.clip_id] = linked_item._track_id
                    current_track_ids[linked_clip_obj.clip_id] = linked_item._track_id

            # P3: Collect multi-selected clips (excluding primary and linked)
            if len(self._selection.selected_clip_ids) > 1:
                for sel_id in self._selection.selected_clip_ids:
                    if sel_id == clip_id or sel_id in linked_ids:
                        continue
                    sel_item = self._items_map.get(sel_id)
                    if sel_item and isinstance(sel_item, ClipItem):
                        selected_ids.append(sel_id)
                        original_positions[sel_id] = sel_item._start_frame
                        original_track_ids[sel_id] = sel_item._track_id
                        current_track_ids[sel_id] = sel_item._track_id

            self._current_drag_session = ClipDragSession(
                primary_clip_id=clip_id,
                linked_clip_ids=linked_ids,
                selected_clip_ids=selected_ids,
                start_frame=item._start_frame,
                original_positions=original_positions,
                original_track_ids=original_track_ids,
                current_track_ids=current_track_ids,
                copy_mode=bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier),
            )
            self._configure_layout_engine()
            self._cache_drag_track_geometry(self._current_drag_session)
            # Alt-drag copy: create ghosts so originals stay in place.
            if self._current_drag_session.copy_mode:
                self._create_alt_copy_ghosts(self._current_drag_session)

        # Switch to the drag-specific repaint policy once, on the first move.
        # Each moved QGraphicsItem invalidates its old and new bounding rect;
        # forcing a full viewport repaint here made pointer feedback lag.
        self._set_drag_render_mode(True)

        # Apply snap if enabled (visual preview)
        # 注意：_snap_frame 内部会检查 _snap_temporarily_disabled
        session = self._current_drag_session
        if self._snap_enabled:
            # SnapManager already maintains an indexed point set from the
            # loaded timeline. Avoid rebuilding the legacy tuple on the first
            # move; that scan made the beginning of a drag feel sticky.
            if self._snap_manager is None and not session.snap_points:
                session.snap_points = self._get_snap_points(exclude_clip_id=clip_id)
            # AUD-21402: SnapManager 路径必须显式排除自身，否则移动中的
            # 片段把自身原始入/出点当作吸附点（微小拖拽被吸回原位）。
            snapped_start, did_snap = self._snap_frame(
                new_start, session.snap_points, exclude_source_ids={clip_id})
            if did_snap:
                new_start = snapped_start

        # Compute delta from original position stored in session
        orig_start = session.original_positions.get(clip_id, item._start_frame)
        delta = new_start - orig_start

        # Boundary constraint: prevent clips from moving before time 00:00
        # Find the minimum original start position among all members
        min_old_start = min(session.original_positions.values())
        if delta < 0:
            # When moving left, ensure the earliest clip stays at >= 0
            max_negative_delta = -min_old_start
            if delta < max_negative_delta:
                delta = max_negative_delta

        # Use item._media_type which is correctly set based on track type
        # (clips on audio tracks always have _media_type="audio", even if the asset is video+audio)
        # BUG-05 fix: Include comp_clip in video types (matches timeline_items.py pattern)
        clip_track_type = "video" if item._media_type in ("video", "image", "comp_clip", "motion_graphic") else "audio"

        # Get current target track based on Y coordinate
        target_track_id = self._resolve_drag_target_track(clip_id, track_id, scene_y, clip_track_type)
        session.current_track_ids[clip_id] = target_track_id
        item._drag_target_track_id = target_track_id if target_track_id != track_id else ""

        # Keep the whole drag group in sync with the primary's track change so
        # box-selected / linked clips follow to the target track (applies to
        # both normal moves and Alt-drag copies). When the primary returns to
        # its original track, every member snaps back to its own track.
        if len(session.original_positions) > 1:
            if target_track_id.startswith("__new_"):
                # Auto-create zone. Keep ordinary same-type selections on
                # their original lanes, but resolve a linked A/V partner by
                # the same visual offset in the opposite direction. If that
                # destination is outside the current stack, carry a matching
                # create-zone pseudo target into the commit phase.
                primary_ids = self._visual_track_ids(clip_track_type)
                primary_index = (
                    primary_ids.index(track_id)
                    if track_id in primary_ids
                    else 0
                )
                virtual_target_index = (
                    -1
                    if target_track_id == "__new_video_track_above__"
                    else len(primary_ids)
                )
                visual_offset = virtual_target_index - primary_index
                linked_ids = set(session.linked_clip_ids)
                for cid in session.original_positions:
                    if cid == clip_id:
                        continue
                    orig_tid = session.original_track_ids.get(cid, "")
                    member_track = (
                        self._timeline.get_track_by_id(orig_tid)
                        if orig_tid and not orig_tid.startswith("__new_")
                        else None
                    )
                    if member_track is None:
                        continue
                    if cid not in linked_ids or member_track.track_type == clip_track_type:
                        session.current_track_ids[cid] = orig_tid
                        continue

                    member_ids = self._visual_track_ids(member_track.track_type)
                    if orig_tid not in member_ids:
                        session.current_track_ids[cid] = orig_tid
                        continue
                    member_index = member_ids.index(orig_tid)
                    desired_index = member_index - visual_offset
                    if 0 <= desired_index < len(member_ids):
                        session.current_track_ids[cid] = member_ids[desired_index]
                    elif member_track.track_type == "audio" and desired_index >= len(member_ids):
                        session.current_track_ids[cid] = "__new_audio_track_below__"
                    elif member_track.track_type == "video" and desired_index < 0:
                        session.current_track_ids[cid] = "__new_video_track_above__"
                    else:
                        session.current_track_ids[cid] = orig_tid
            elif target_track_id != track_id:
                self._propagate_track_change_to_group(
                    session, clip_id, track_id, target_track_id
                )
            else:
                for cid in list(session.current_track_ids):
                    if cid == clip_id:
                        continue
                    session.current_track_ids[cid] = session.original_track_ids.get(
                        cid, session.current_track_ids[cid]
                    )

        # P3: Build MoveGroup with all members
        members: list[MoveGroupMember] = []
        for cid, old_s in session.original_positions.items():
            orig_tid = session.original_track_ids.get(cid, track_id)
            target_tid = session.current_track_ids.get(cid, orig_tid)
            members.append(MoveGroupMember(
                clip_id=cid,
                track_id=orig_tid,
                old_start=old_s,
                new_start=old_s + delta,
                new_track_id=target_tid if target_tid != orig_tid else "",
            ))
        preview_key = (
            delta,
            tuple((m.clip_id, m.new_start, m.new_track_id or m.track_id) for m in members),
        )
        if session.last_preview_key == preview_key:
            return
        session.last_preview_key = preview_key
        session.move_group = MoveGroup(
            primary_clip_id=clip_id,
            members=tuple(members),
            delta_frames=delta,
        )

        # Update ALL members' preview positions simultaneously.
        # In copy_mode, update ghosts instead of actual items so originals stay put.
        use_ghosts = session.copy_mode and self._alt_copy_ghosts
        copy_reserved: dict[str, list[tuple[int, int]]] = {}
        for member in session.move_group.members:
            if use_ghosts:
                m_item = self._alt_copy_ghosts.get(member.clip_id)
                # Keep the real item's start frame in sync (data only — its
                # visual position stays put) so mouseReleaseEvent detects the
                # change and emits drag_finished with the ghost's final
                # position. Restored in _on_drag_finished_impl (copy branch).
                real_item = self._items_map.get(member.clip_id)
                if real_item and isinstance(real_item, ClipItem):
                    real_item._start_frame = member.new_start
            else:
                m_item = self._items_map.get(member.clip_id)
            if not (m_item and isinstance(m_item, ClipItem)):
                continue
            # Moving cached graphics items can leave their old device-pixel
            # edges behind. Render directly for the duration of the drag.
            m_item.set_drag_cache_mode(True)
            preview_start = member.new_start
            target_id = member.new_track_id or member.track_id
            if use_ghosts and not target_id.startswith("__new_"):
                target = self._timeline.get_track_by_id(target_id)
                if target is not None:
                    occupied = copy_reserved.setdefault(target_id, [])
                    preview_start = self._resolve_copy_start(target, preview_start, m_item._duration_frames, occupied=occupied)
                    occupied.append((preview_start, preview_start+m_item._duration_frames))
            m_item._start_frame = preview_start
            m_x = self._layout_engine.frame_to_x(preview_start)
            # Use new_track_id if track changed, otherwise original track_id
            display_track_id = member.new_track_id if member.new_track_id else member.track_id
            # Calculate Y position — use each member's own media type
            if display_track_id.startswith("__new_"):
                m_type = "video" if m_item._media_type in ("video", "image", "comp_clip", "motion_graphic") else "audio"
                m_y = self._compute_new_track_preview_y(display_track_id, m_type)
            else:
                m_y = self._layout_engine.track_top(display_track_id)
                m_item.set_track_height(self._layout_engine.track_height(display_track_id))
            # Guard against invalid positions
            if m_y != m_y:  # NaN check
                m_y = 0.0
            m_item.setPos(m_x, m_y)
            m_item.update()
            preview_track_id = (
                display_track_id
                if not display_track_id.startswith("__new_")
                else member.track_id
            )
            self._preview_scoped_marker_move(
                member.clip_id,
                member.old_start,
                member.new_start,
                preview_track_id,
            )

        # setPos() and update() already invalidate the old/new item bounds.
        # Avoid viewport().update() here: it repaints the entire timeline for
        # every mouse event and defeats the local-update policy above.

    def _cache_drag_track_geometry(self, session: ClipDragSession) -> None:
        """Cache track bounds used by the current cross-track drag."""
        if self._timeline is None:
            return

        cached: dict[str, tuple[tuple[str, float, float], ...]] = {}
        for track_type, tracks in (
            ("video", self._layout_engine.ordered_video_tracks()),
            ("audio", self._layout_engine.ordered_audio_tracks()),
        ):
            cached[track_type] = tuple(
                (
                    track.track_id,
                    float(self._layout_engine.track_rect(track.track_id).top()),
                    float(self._layout_engine.track_rect(track.track_id).bottom()),
                )
                for track in tracks
            )
        session.drag_track_bounds_by_type = cached

    def _resolve_drag_target_track(
        self,
        clip_id: str,
        original_track_id: str,
        scene_y: float,
        clip_track_type: str
    ) -> str:
        """
        P4: Resolve target track for drag based on Y coordinate.

        Rules:
        - Video clips in video track area → move to that video track
        - Video clips above all video tracks → create new video track
        - Audio clips in audio track area → move to that audio track
        - Audio clips below all audio tracks → create new audio track
        """
        if self._timeline is None:
            return original_track_id

        hit_track_id, hit_area = self._layout_engine.track_at_y_with_area(scene_y)

        # Get track boundaries for this clip's type
        session = self._current_drag_session
        cached_bounds = (
            session.drag_track_bounds_by_type.get(clip_track_type, ())
            if session is not None
            else ()
        )

        if clip_track_type == "video":
            video_tracks = self._timeline.video_tracks
            if not video_tracks:
                return original_track_id

            video_track_ids = {track_id for track_id, _top, _bottom in cached_bounds}
            if not cached_bounds:
                ordered_video_tracks = self._layout_engine.ordered_video_tracks()
                cached_bounds = tuple(
                    (
                        track.track_id,
                        float(self._layout_engine.track_rect(track.track_id).top()),
                        float(self._layout_engine.track_rect(track.track_id).bottom()),
                    )
                    for track in ordered_video_tracks
                )
                video_track_ids = {track_id for track_id, _top, _bottom in cached_bounds}
            if not cached_bounds:
                return original_track_id
            top_video_top = cached_bounds[0][1]
            max_video_track_height = max(
                bottom - top for _track_id, top, bottom in cached_bounds
            )
            nearest_entry = min(
                cached_bounds,
                key=lambda entry: abs(scene_y - ((entry[1] + entry[2]) * 0.5)),
            )
            nearest_distance = abs(
                scene_y - ((nearest_entry[1] + nearest_entry[2]) * 0.5)
            )
            nearest = (
                nearest_entry[0]
                if nearest_distance <= max_video_track_height * 1.5
                else None
            )
            if nearest and nearest in video_track_ids:
                if nearest_entry[1] <= scene_y < nearest_entry[2]:
                    return nearest

            # Check if dragging above video track area (create new track)
            if hit_area in ("ruler", "empty") and scene_y < self._layout_engine.area_layout().video_area.top():
                return "__new_video_track_above__"

            # Trigger new track only after leaving the existing video stack.
            # Edge bands inside the top track make normal cross-track drags feel
            # heavy because the target flips to a pseudo track too early.
            if hit_track_id is None and hit_area in ("video", "empty") and scene_y < top_video_top:
                return "__new_video_track_above__"

            if nearest and nearest in video_track_ids:
                return nearest

        elif clip_track_type == "audio":
            audio_tracks = self._timeline.audio_tracks
            if not audio_tracks:
                return original_track_id

            audio_track_ids = {track_id for track_id, _top, _bottom in cached_bounds}
            if not cached_bounds:
                ordered_audio_tracks = self._layout_engine.ordered_audio_tracks()
                cached_bounds = tuple(
                    (
                        track.track_id,
                        float(self._layout_engine.track_rect(track.track_id).top()),
                        float(self._layout_engine.track_rect(track.track_id).bottom()),
                    )
                    for track in ordered_audio_tracks
                )
                audio_track_ids = {track_id for track_id, _top, _bottom in cached_bounds}
            if not cached_bounds:
                return original_track_id
            bottom_audio_bottom = cached_bounds[-1][2]
            max_audio_track_height = max(
                bottom - top for _track_id, top, bottom in cached_bounds
            )
            nearest_entry = min(
                cached_bounds,
                key=lambda entry: abs(scene_y - ((entry[1] + entry[2]) * 0.5)),
            )
            nearest_distance = abs(
                scene_y - ((nearest_entry[1] + nearest_entry[2]) * 0.5)
            )
            nearest = (
                nearest_entry[0]
                if nearest_distance <= max_audio_track_height * 1.5
                else None
            )
            if nearest and nearest in audio_track_ids:
                if nearest_entry[1] <= scene_y < nearest_entry[2]:
                    return nearest

            # Check if dragging below audio track area (create new track)
            audio_area = self._layout_engine.area_layout().audio_area
            if hit_area == "empty" and scene_y > audio_area.bottom():
                return "__new_audio_track_below__"

            # Trigger new track only after leaving the existing audio stack.
            if hit_track_id is None and hit_area in ("audio", "empty") and scene_y > bottom_audio_bottom:
                return "__new_audio_track_below__"

            if nearest and nearest in audio_track_ids:
                return nearest

        # Stay in original track if no valid target found
        return original_track_id

    def _visual_track_ids(self, track_type: str) -> list[str]:
        """Return track IDs in their top-to-bottom visual order."""
        if track_type == "video":
            tracks = self._layout_engine.ordered_video_tracks()
        else:
            tracks = self._layout_engine.ordered_audio_tracks()
        return [track.track_id for track in tracks]

    def _propagate_track_change_to_group(
        self,
        session,
        primary_clip_id: str,
        primary_track_id: str,
        target_track_id: str,
    ) -> None:
        """Apply the primary clip's track change to the rest of the drag group.

        Same-type selected clips follow the primary clip by the same visual
        offset. A linked clip on the other media type follows the opposite
        visual offset because the track stacks face opposite directions:
        video V1/V2/V3 are laid out bottom-to-top, while audio A1/A2/A3 are
        laid out top-to-bottom. Therefore moving video up one lane moves its
        linked audio down one lane, preserving the A/V relationship.

        Members whose shifted index would fall outside the available tracks
        stay on their original track.
        """
        if self._timeline is None:
            return
        target_track = self._timeline.get_track_by_id(target_track_id)
        primary_track = self._timeline.get_track_by_id(primary_track_id)
        if target_track is None or primary_track is None:
            return
        if target_track.track_type != primary_track.track_type:
            return

        primary_ids = self._visual_track_ids(target_track.track_type)
        if primary_track_id not in primary_ids or target_track_id not in primary_ids:
            return
        visual_offset = primary_ids.index(target_track_id) - primary_ids.index(primary_track_id)
        if visual_offset == 0:
            return

        linked_ids = set(session.linked_clip_ids)
        for cid in session.original_positions:
            if cid == primary_clip_id:
                continue
            orig_tid = session.original_track_ids.get(cid, "")
            if not orig_tid or orig_tid.startswith("__new_"):
                continue
            member_track = self._timeline.get_track_by_id(orig_tid)
            if member_track is None or member_track.track_type not in ("video", "audio"):
                continue
            member_ids = self._visual_track_ids(member_track.track_type)
            if orig_tid not in member_ids:
                continue
            member_visual_offset = visual_offset
            if (
                cid in linked_ids
                and member_track.track_type != primary_track.track_type
            ):
                member_visual_offset = -visual_offset
            new_idx = member_ids.index(orig_tid) + member_visual_offset
            if 0 <= new_idx < len(member_ids):
                session.current_track_ids[cid] = member_ids[new_idx]
            else:
                # A linked partner may also need a newly-created edge track:
                # audio grows downward, video grows upward.
                if (
                    cid in linked_ids
                    and member_track.track_type == "audio"
                    and new_idx >= len(member_ids)
                ):
                    session.current_track_ids[cid] = "__new_audio_track_below__"
                elif (
                    cid in linked_ids
                    and member_track.track_type == "video"
                    and new_idx < 0
                ):
                    session.current_track_ids[cid] = "__new_video_track_above__"
                else:
                    session.current_track_ids[cid] = orig_tid

    def _compute_new_track_preview_y(self, pseudo_track_id: str, clip_track_type: str) -> float:
        """P5: Calculate preview Y position for a clip being dragged to a new track.

        Places the clip above all existing video tracks or below all audio tracks,
        with appropriate spacing so the user sees where the new track will be created.
        """
        from src.features.edit.ui.timeline_layout import TRACK_GAP, DEFAULT_VIDEO_TRACK_HEIGHT
        if self._timeline is None:
            return 0.0

        self._configure_layout_engine()

        if "above" in pseudo_track_id:
            # New video track above top video track
            video_tracks = list(self._timeline.video_tracks)
            if video_tracks:
                top_track = video_tracks[-1]
                top_rect = self._layout_engine.track_rect(top_track.track_id)
                return top_rect.top() - DEFAULT_VIDEO_TRACK_HEIGHT - TRACK_GAP
            return self._layout_engine.area_layout().video_area.top()

        elif "below" in pseudo_track_id:
            # New audio track below bottom audio track
            audio_tracks = list(self._timeline.audio_tracks)
            if audio_tracks:
                bottom_track = audio_tracks[-1]
                bottom_rect = self._layout_engine.track_rect(bottom_track.track_id)
                return bottom_rect.bottom() + TRACK_GAP
            return self._layout_engine.area_layout().audio_area.top()

        return 0.0

    def _find_target_track_for_type(self, track_type: str, scene_y: float) -> Optional[str]:
        """
        P4: Find appropriate target track for a given track type at Y coordinate.
        Used for linked clips to find their own type's track.
        """
        if self._timeline is None:
            return None

        tracks = list(self._timeline.video_tracks if track_type == "video" else self._timeline.audio_tracks)
        if not tracks:
            return None

        # Return the top/bottom track for preview purposes
        # Video tracks: last in original order is visually top
        # Audio tracks: last in order is visually bottom
        if track_type == "video":
            return tracks[-1].track_id  # Top video track (V3 or highest)
        else:
            return tracks[-1].track_id  # Bottom audio track (A3 or highest)

    def _on_drag_finished(
        self,
        clip_id: str,
        track_id: str,
        old_start: int,
        new_start: int,
        new_track_id: str
    ) -> None:
        """Handle drag finished: create and execute atomic MacroCommand (P3 / P4).

        P3: Uses MoveGroup from the drag session to validate ALL members
        atomically. If ANY member is invalid, the WHOLE group is rejected.
        A single MacroCommand ensures one undo reverts all clips at once.

        P4: Handles cross-track drag with auto-create track support.
        - Video clips dragged above top video track → create new video track and move
        - Audio clips dragged below bottom audio track → create new audio track and move

        P5c: Wrapped in try/finally to ensure _current_drag_session is always
        cleaned up, even on early returns or command failure.
        """
        drag_session = self._current_drag_session
        try:
            self._on_drag_finished_impl(
                clip_id, track_id, old_start, new_start, new_track_id
            )
        finally:
            # P5c: Always clear drag session and ghosts, even on exceptions or early returns.
            # In copy mode the real items' _start_frame mirrored the ghost
            # preview during the drag; originals never move, so restore them
            # here no matter which exit path was taken.
            session = self._current_drag_session
            if session is not None and session.copy_mode:
                for cid, orig_s in session.original_positions.items():
                    real_item = self._items_map.get(cid)
                    if real_item and isinstance(real_item, ClipItem):
                        real_item._start_frame = orig_s
                        real_item._drag_target_track_id = ""
            self._clear_alt_copy_ghosts()
            if self._current_drag_session is not None:
                self._current_drag_session = None
            self._restore_drag_cache_modes(drag_session)
            self._sync_marker_items_to_timeline()

    def _restore_drag_cache_modes(
        self,
        session: Optional[ClipDragSession],
    ) -> None:
        """Restore cached painting after a live clip drag completes."""
        if session is None:
            self._set_drag_render_mode(False)
            return
        for clip_id in session.original_positions:
            item = self._items_map.get(clip_id)
            if isinstance(item, ClipItem):
                item.set_drag_cache_mode(False)
        self._set_drag_render_mode(False)
        self.viewport().update()

    def _set_drag_render_mode(self, active: bool) -> None:
        """Use conservative repaint settings while clips are moving."""
        active = bool(active)
        if self._drag_render_mode_active == active:
            return
        self._drag_render_mode_active = active
        if active:
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate
            )
            self.setOptimizationFlag(
                QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing,
                True,
            )
        else:
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate
            )
            self.setOptimizationFlag(
                QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing,
                True,
            )
        # Do not force a full repaint when entering drag mode. The first item
        # update below will schedule the local bounding-rect repaint; forcing
        # the viewport here was the visible one-frame hitch at drag start.
        if not active:
            self.viewport().update()

    def _on_drag_finished_impl(
        self,
        clip_id: str,
        track_id: str,
        old_start: int,
        new_start: int,
        new_track_id: str
    ) -> None:
        """Implementation of drag finished, called by _on_drag_finished with cleanup."""
        if self._timeline is None or self._updating:
            return

        # P3: Only process primary clip - linked/selected clips are handled as part of the group
        if self._current_drag_session is not None:
            if clip_id != self._current_drag_session.primary_clip_id:
                logger.debug(f"Skipping non-primary clip drag_finished: {clip_id}")
                return

        # Hide snap indicator after drag finishes
        if self._snap_indicator:
            self._snap_indicator.hide_indicator()

        # Apply snap if enabled (final position)
        if self._snap_enabled:
            snap_points = self._get_snap_points(exclude_clip_id=clip_id)
            # AUD-21402: SnapManager 路径显式排除自身（见 _on_clip_moved_impl）
            new_start, _ = self._snap_frame(
                new_start, snap_points, show_indicator=False,
                exclude_source_ids={clip_id})

        # Alt-drag copy mode: create copies instead of moving
        is_copy_mode = (self._current_drag_session is not None
                        and self._current_drag_session.copy_mode)
        if is_copy_mode:
            # Ghost-drag data sync: real items' _start_frame mirrored the
            # preview during the drag so the release signal would fire.
            # Restore them now — originals never actually move in copy mode.
            for cid, orig_s in self._current_drag_session.original_positions.items():
                real_item = self._items_map.get(cid)
                if real_item and isinstance(real_item, ClipItem):
                    real_item._start_frame = orig_s
                    real_item._drag_target_track_id = ""
            self._handle_copy_drag_finished(clip_id, track_id, old_start, new_start)
            return

        # P3: Use MoveGroup from drag session if available
        group = None
        if self._current_drag_session is not None:
            group = self._current_drag_session.move_group

        # P4: Resolve actual target track from ClipDragSession (may override signal parameter)
        actual_target_track = new_track_id
        if self._current_drag_session is not None:
            session_target = self._current_drag_session.current_track_ids.get(clip_id)
            if session_target:
                actual_target_track = session_target

        # P4: Check for cross-track move
        needs_new_track = actual_target_track.startswith("__new_")
        has_track_change = actual_target_track != track_id and not needs_new_track
        has_paired_create_zone = bool(
            group is not None
            and any(
                member.new_track_id.startswith("__new_")
                for member in group.members
                if member.new_track_id
            )
        )

        if has_track_change and not has_paired_create_zone:
            # Cross-track move onto an existing track. Create-zone moves are
            # handled by the MoveGroup branch below so linked partners keep
            # their paired create-zone target as well.
            # The live preview already calculated the complete target-track
            # mapping (including the opposite visual offset for linked A/V).
            # Rebuild the release group from that mapping instead of adding
            # only the primary clip plus a same-track linked fallback; the
            # latter made the linked audio jump back as soon as the mouse was
            # released.
            if self._current_drag_session is not None:
                session = self._current_drag_session
                orig_start = session.original_positions.get(clip_id, old_start)
                delta = new_start - orig_start
                members = []
                for cid, old_s in session.original_positions.items():
                    orig_tid = session.original_track_ids.get(cid, track_id)
                    target_tid = session.current_track_ids.get(cid, orig_tid)
                    members.append(MoveGroupMember(
                        clip_id=cid,
                        track_id=orig_tid,
                        old_start=old_s,
                        new_start=old_s + delta,
                        new_track_id=(
                            target_tid if target_tid != orig_tid else ""
                        ),
                    ))
            else:
                members = [
                    MoveGroupMember(
                        clip_id=clip_id,
                        track_id=track_id,
                        old_start=old_start,
                        new_start=new_start,
                        new_track_id=actual_target_track,
                    )
                ]
                clip = self._timeline.get_clip_by_id(clip_id)
                linked_clip = self._get_reciprocal_linked_clip(clip)
                if linked_clip is not None and self._effective_linked_selection():
                    members.append(MoveGroupMember(
                        clip_id=linked_clip.clip_id,
                        track_id=linked_clip.track_id,
                        old_start=linked_clip.timeline_start,
                        new_start=linked_clip.timeline_start + (new_start - old_start),
                    ))
            overwrite_group = MoveGroup(
                primary_clip_id=clip_id,
                members=tuple(members),
                delta_frames=new_start - old_start,
            )
            if not self._validate_move_group(overwrite_group, allow_overwrite=True):
                logger.warning(f"Overwrite move rejected: group validation failed for {clip_id}")
                return
            self._current_drag_session = None
            self._execute_command(
                self._create_overwrite_move_command(
                    overwrite_group.members,
                    label="Move clips across tracks with overwrite",
                )
            )
            return

        elif group is not None and len(group.members) > 0:
            # Recompute members with snapped new_start
            orig_start = self._current_drag_session.original_positions.get(
                clip_id, old_start
            )
            delta = new_start - orig_start
            members: list[MoveGroupMember] = []
            for cid, old_s in self._current_drag_session.original_positions.items():
                orig_tid = self._current_drag_session.original_track_ids.get(cid, track_id)
                target_tid = self._current_drag_session.current_track_ids.get(cid, orig_tid)
                members.append(MoveGroupMember(
                    clip_id=cid,
                    track_id=orig_tid,
                    old_start=old_s,
                    new_start=old_s + delta,
                    new_track_id=target_tid if target_tid != orig_tid else "",
                ))
            group = MoveGroup(
                primary_clip_id=clip_id,
                members=tuple(members),
                delta_frames=delta,
            )

            # Atomic validation: if ANY member is invalid, reject ALL
            if not self._validate_move_group(group, allow_overwrite=True):
                logger.warning(f"Move rejected: group validation failed for {clip_id}")
                # Revert all members to original positions
                for member in group.members:
                    m_item = self._items_map.get(member.clip_id)
                    if m_item and isinstance(m_item, ClipItem):
                        m_item._start_frame = member.old_start
                        m_x = self._layout_engine.frame_to_x(member.old_start)
                        m_y = self._layout_engine.track_top(member.track_id)
                        m_item.setPos(m_x, m_y)
                        m_item._update_geometry()
                        m_item.update()
                return

            # Check if position actually changed
            if delta == 0 and not group.has_track_change:
                return

            # P4: Handle track changes in group
            if group.has_track_change:
                # Build commands for track changes
                commands = []
                overwrite_members: list[MoveGroupMember] = []
                for member in group.members:
                    if member.new_track_id and member.new_track_id != member.track_id:
                        # Cross-track move needed
                        # Check if the target is a pseudo "create zone" or an existing track
                        if member.new_track_id.startswith("__new_"):
                            # Create new track and move
                            source_track = self._timeline.get_track_by_id(member.track_id) if self._timeline else None
                            track_type = source_track.track_type if source_track else "video"
                            position = "above" if track_type == "video" else "below"

                            cmd = MoveClipToTrackCommand.create_for_new_track(
                                source_track_id=member.track_id,
                                clip_id=member.clip_id,
                                target_track_type=track_type,
                                new_track_position=position,
                                new_start=member.new_start,
                                old_start=member.old_start,
                            )
                            commands.append(cmd)
                        else:
                            overwrite_members.append(member)
                    else:
                        overwrite_members.append(member)

                if overwrite_members:
                    commands.append(
                        self._create_overwrite_move_command(
                            tuple(overwrite_members),
                            label="Move clips with overwrite",
                        )
                    )

                # P5: Clear drag session BEFORE executing command
                self._current_drag_session = None

                # Execute as single atomic MacroCommand
                if len(commands) == 1:
                    self._execute_command(commands[0])
                else:
                    macro = MacroCommand.create("Move clips (track change)", commands)
                    self._execute_command(macro)
            else:
                self._execute_command(
                    self._create_overwrite_move_command(
                        group.members,
                        label="Move clips with overwrite",
                    )
                )
        else:
            # Fallback: no MoveGroup from drag session — build one from linked clips
            if new_start == old_start and actual_target_track == track_id:
                return

            delta = new_start - old_start
            members: list[MoveGroupMember] = [
                MoveGroupMember(
                    clip_id=clip_id,
                    track_id=track_id,
                    old_start=old_start,
                    new_start=new_start,
                    new_track_id=actual_target_track if actual_target_track != track_id else ""
                )
            ]

            # Collect linked clip if applicable
            clip = self._timeline.get_clip_by_id(clip_id)
            linked_clip = self._get_reciprocal_linked_clip(clip)
            if linked_clip is not None and self._effective_linked_selection():
                members.append(MoveGroupMember(
                    clip_id=linked_clip.clip_id,
                    track_id=linked_clip.track_id,
                    old_start=linked_clip.timeline_start,
                    new_start=linked_clip.timeline_start + delta,
                ))

            fallback_group = MoveGroup(
                primary_clip_id=clip_id,
                members=tuple(members),
                delta_frames=delta,
            )

            if not self._validate_move_group(fallback_group, allow_overwrite=True):
                # Revert ALL members (including linked clips) to original positions
                for member in fallback_group.members:
                    m_item = self._items_map.get(member.clip_id)
                    if m_item and isinstance(m_item, ClipItem):
                        m_item._start_frame = member.old_start
                        m_x = self._layout_engine.frame_to_x(member.old_start)
                        m_y = self._layout_engine.track_top(member.track_id)
                        m_item.setPos(m_x, m_y)
                        m_item._update_geometry()
                        m_item.update()
                logger.warning(f"Move rejected: overlap detected at position {new_start}")
                return

            commands = []
            overwrite_members: list[MoveGroupMember] = []
            for member in fallback_group.members:
                if member.new_track_id:
                    # Cross-track move: check if existing track or create zone
                    if member.new_track_id.startswith("__new_"):
                        source_track = self._timeline.get_track_by_id(member.track_id) if self._timeline else None
                        track_type = source_track.track_type if source_track else "video"
                        position = "above" if track_type == "video" else "below"

                        cmd = MoveClipToTrackCommand.create_for_new_track(
                            source_track_id=member.track_id,
                            clip_id=member.clip_id,
                            target_track_type=track_type,
                            new_track_position=position,
                            new_start=member.new_start,
                            old_start=member.old_start,
                        )
                        commands.append(cmd)
                    else:
                        overwrite_members.append(member)
                else:
                    overwrite_members.append(member)

            if overwrite_members:
                commands.append(
                    self._create_overwrite_move_command(
                        tuple(overwrite_members),
                        label="Move linked clips with overwrite",
                    )
                )

            # P5: Clear drag session BEFORE executing command
            self._current_drag_session = None

            if len(commands) == 1:
                self._execute_command(commands[0])
            else:
                macro = MacroCommand.create("Move linked clips", commands)
                self._execute_command(macro)

    def _resolve_copy_start(
        self,
        track,
        desired_start: int,
        duration: int,
        exclude_clip_id: Optional[str] = None,
        occupied: Optional[list[tuple[int, int]]] = None,
    ) -> int:
        """Find the first non-overlapping start for a copied clip on *track*.

        ``occupied`` holds (start, end) intervals already reserved by the
        current copy batch, so batch copies never collide with each other.
        """
        resolved = max(0, desired_start)
        clips = sorted(track.clips, key=lambda c: c.timeline_start)
        occupied_list = list(occupied or [])
        for _ in range(len(clips) + len(occupied_list) + 2):
            overlap = next(
                (c for c in clips
                 if c.clip_id != exclude_clip_id
                 and resolved < c.timeline_end
                 and resolved + duration > c.timeline_start),
                None,
            )
            if overlap is not None:
                resolved = max(resolved, int(overlap.timeline_end))
                continue
            batch_overlap = next(
                ((s, e) for s, e in occupied_list
                 if resolved < e and resolved + duration > s),
                None,
            )
            if batch_overlap is not None:
                resolved = max(resolved, batch_overlap[1])
                continue
            return resolved
        return resolved

    def _plan_group_copy(
        self, session, delta: int,
    ) -> Optional[dict[str, tuple[str, int, bool]]]:
        """B11：整组复制的共同落位计划（共同 delta + 共同避让位移）。

        互链/多选成员不得逐片独立避让——那会产出"互链却错位 80 帧"的
        副本对。计划约束：

        - 所有成员共用同一 delta，且避让时共用同一**共同位移**
          （任一成员轨上有 blocker → 整组一起后移）；
        - 每条目标轨的既有 clip 是障碍；同批成员互相也是障碍
          （保持相对几何，正常拖拽不会触发，防御异常 track 映射）；
        - 任一成员的目标轨锁定、目标不可解析、或迭代上限内找不到
          全员同时无冲突的位移 → 返回 None，调用方整次拒绝。

        Returns:
            ``{clip_id: (target_track_id, resolved_start, goes_to_new_track)}``；
            ``target_track_id`` 为真实轨 ID 或 ``__new_*`` 伪 ID。
        """
        if self._timeline is None:
            return None
        desired: dict[str, int] = {}
        meta: dict[str, tuple[str, bool]] = {}
        durations: dict[str, int] = {}
        obstacles_by_track: dict[str, list[tuple[int, int]]] = {}
        for cid, orig_start in session.original_positions.items():
            clip = self._timeline.get_clip_by_id(cid)
            if clip is None:
                continue
            target_tid = session.current_track_ids.get(
                cid, session.original_track_ids.get(cid, clip.track_id))
            goes_to_new = target_tid.startswith("__new_")
            if not goes_to_new:
                track = self._timeline.get_track_by_id(target_tid)
                if track is None:
                    target_tid = session.original_track_ids.get(
                        cid, clip.track_id)
                    goes_to_new = target_tid.startswith("__new_")
                    track = self._timeline.get_track_by_id(target_tid)
                if track is None:
                    return None
                if self._is_track_locked(track.track_id):
                    return None
                obstacles_by_track.setdefault(track.track_id, [
                    (c.timeline_start, c.timeline_end) for c in track.clips
                ])
                target_tid = track.track_id
            desired[cid] = max(0, orig_start + delta)
            durations[cid] = clip.timeline_duration
            meta[cid] = (target_tid, goes_to_new)

        shift = 0
        max_iters = sum(len(v) for v in obstacles_by_track.values()) \
            + len(desired) + 2
        for _ in range(max_iters):
            required = shift
            placed: dict[str, tuple[int, int]] = {}
            for cid in desired:
                track_tid, goes_to_new = meta[cid]
                start = desired[cid] + shift
                end = start + durations[cid]
                intervals = ([] if goes_to_new
                             else list(obstacles_by_track.get(track_tid, ())))
                intervals += [iv for other, iv in placed.items()
                              if meta[other][0] == track_tid]
                conflict = next(
                    ((s, e) for s, e in intervals
                     if start < e and end > s), None)
                if conflict is not None:
                    required = max(required, conflict[1] - desired[cid])
                else:
                    placed[cid] = (start, end)
            if required == shift and len(placed) == len(desired):
                return {
                    cid: (meta[cid][0], placed[cid][0], meta[cid][1])
                    for cid in desired
                }
            shift = required
        return None

    def _handle_copy_drag_finished(
        self,
        primary_clip_id: str,
        track_id: str,
        old_start: int,
        new_start: int,
    ) -> None:
        """Alt-drag copy: create new clips at the drop position, keeping originals intact."""
        from src.features.edit.core.commands import (
            AddClipCommand,
            CopyClipsToNewTrackCommand,
            MacroCommand,
        )
        from src.features.edit.core.captions.commands import (
            AddCaptionGraphicBindingCommand,
        )
        from src.features.edit.core.captions.graphics import (
            clone_caption_graphic_binding,
        )

        if self._timeline is None:
            return

        session = self._current_drag_session
        self._current_drag_session = None

        # Clean up ghost preview items (originals never moved).
        self._clear_alt_copy_ghosts()

        if session is None:
            return

        # Calculate delta from original to new position
        orig_primary_start = session.original_positions.get(primary_clip_id, old_start)
        delta = new_start - orig_primary_start

        # A pure cross-track drop keeps delta == 0 but is still a valid copy.
        any_track_change = any(
            session.current_track_ids.get(cid, orig_tid) != orig_tid
            for cid, orig_tid in session.original_track_ids.items()
        )
        if delta == 0 and not any_track_change:
            return

        # B11：先为整组求共同落位计划——所有成员共同 delta 与共同避让
        # 位移；任一成员轨被锁或找不到共同落位时整次拒绝，绝不逐片
        # 独立避让产出互链却错位的副本对。
        plan = self._plan_group_copy(session, delta)
        if plan is None:
            self.status_message_requested.emit(I18N.tr(
                "timeline.copy.group_blocked",
                "Copy: no common placement for the whole group"))
            logger.warning(
                "Alt-drag copy rejected: no common placement (group blocked)")
            return

        # Build old_id -> new_id mapping for linked remapping
        old_to_new = {}
        for cid in session.original_positions:
            old_to_new[cid] = str(uuid.uuid4())

        # AUD-21404: 构建循环会为 solid 片段副本替换 self._project（新
        # asset + bin 条目）。宏命令被模型校验拒绝（如重叠）时必须回滚
        # 到执行前引用，否则新 solid asset 已进入 project.assets 却无
        # clip 引用（孤儿 asset，会被持久化），且媒体池等监听者收不到
        # project_changed 而状态失步（FX-H03 generator 路径同型问题）。
        project_before_copy = self._project

        commands = []
        # R06：自动建轨副本**按目标伪轨 ID 分桶**——互链 A/V 对可能同时
        # 目标 __new_video_track_above__ 与 __new_audio_track_below__，
        # 各建一条轨各自放片（旧实现单一列表 + 最后伪 ID 把两副本挤进
        # 同一条轨，重叠拒绝且无诊断）。
        new_track_buckets: dict[str, list] = {}
        new_track_binding_commands: dict[str, list] = {}
        for cid, orig_start in session.original_positions.items():
            clip = self._timeline.get_clip_by_id(cid)
            if clip is None:
                continue
            planned = plan.get(cid)
            if planned is None:
                continue
            target_tid, resolved_start, goes_to_new_track = planned
            if goes_to_new_track:
                # 类型预检（整次拒绝）：伪轨类型必须与成员源轨类型一致
                pseudo_type = (
                    "video" if "video" in target_tid else "audio")
                source_track = self._timeline.get_track_by_id(clip.track_id)
                if (source_track is None
                        or source_track.track_type != pseudo_type):
                    self._project = project_before_copy
                    message = I18N.tr(
                        "timeline.copy.track_type_mismatch",
                        "Copy: clip and target track types do not match; "
                        "nothing was copied")
                    self.status_message_requested.emit(message)
                    logger.warning(
                        "Alt-drag copy rejected: clip %s (%s) cannot go to "
                        "new %s track", cid,
                        getattr(source_track, "track_type", "?"),
                        pseudo_type)
                    return

            clip_dict = clip.to_dict()
            new_clip_id = old_to_new[cid]
            clip_dict["clip_id"] = new_clip_id
            clip_dict["timeline_start"] = resolved_start

            # Remap linked_clip_id within the copy batch
            old_linked = clip_dict.get("linked_clip_id")
            if old_linked and old_linked in old_to_new:
                clip_dict["linked_clip_id"] = old_to_new[old_linked]
            elif old_linked:
                # Linked pair not fully copied — clear to avoid shared link
                clip_dict["linked_clip_id"] = None

            # Solid clips: create an independent asset with a new numbered
            # name so each copy has its own identity and colour parameters.
            if self._project is not None:
                orig_asset = self._project.assets.get(clip.asset_id)
                if orig_asset is not None and getattr(orig_asset, "media_type", None) == "solid":
                    import time as _time
                    new_asset_id = str(uuid.uuid4())
                    src_meta = dict(orig_asset.metadata or {})
                    gp = src_meta.get("generator_params")
                    if gp:
                        src_meta["generator_params"] = dict(gp)
                    new_asset = MediaAsset(
                        asset_id=new_asset_id,
                        path=f"topos://solid/{new_asset_id}",
                        media_type="solid",
                        display_name=self._project._next_solid_color_name(),
                        metadata=src_meta,
                        created_at=_time.time(),
                    )
                    new_assets = dict(self._project.assets)
                    new_assets[new_asset_id] = new_asset
                    new_bins = dict(self._project.bins)
                    root_bin = self._project.bins.get(self._project.root_bin_id)
                    if root_bin is not None:
                        new_bins[self._project.root_bin_id] = root_bin.with_asset_added(new_asset_id)
                    # M02：replace 语义保留全部字段——显式构造会抹掉
                    # color_groups/版本/绑定/快照等较新表
                    from dataclasses import replace as _dc_replace
                    self._project = _dc_replace(
                        self._project,
                        assets=new_assets,
                        bins=new_bins,
                        modified_at=_time.time(),
                    )
                    clip_dict["asset_id"] = new_asset_id

            new_clip = ClipInstance.from_dict(clip_dict)
            source_binding = (
                self._timeline.get_caption_graphic_binding_by_clip(cid)
                if hasattr(self._timeline, "get_caption_graphic_binding_by_clip")
                else None
            )
            binding_command = None
            if source_binding is not None:
                target_track_for_binding = (
                    self._timeline.get_track_by_id(target_tid)
                    if not goes_to_new_track else None
                )
                if target_track_for_binding is not None and (
                    target_track_for_binding.track_role != "graphics"
                ):
                    logger.warning(
                        "Skipping caption graphic copy to non-graphics track: %s",
                        target_tid,
                    )
                    continue
                source_cue = self._timeline.get_subtitle_clip_by_id(
                    source_binding.caption_cue_id
                )
                copied_binding = clone_caption_graphic_binding(
                    source_binding, new_clip.clip_id, source_cue)
                binding_command = AddCaptionGraphicBindingCommand.create(
                    copied_binding)
            if goes_to_new_track:
                new_track_buckets.setdefault(target_tid, []).append(new_clip)
                if binding_command is not None:
                    new_track_binding_commands.setdefault(
                        target_tid, []).append(binding_command)
            else:
                cmd = AddClipCommand.create(target_tid, new_clip)
                commands.append(cmd)
                if binding_command is not None:
                    commands.append(binding_command)

        # One command per pseudo track: creates that track and places every
        # copy headed there (V/A 双轨 = 两条子命令，同一宏 = 单步 undo).
        if new_track_buckets:
            for pseudo_id, bucket_clips in new_track_buckets.items():
                nt_type = "video" if "video" in pseudo_id else "audio"
                nt_position = "above" if "above" in pseudo_id else "below"
                bindings = new_track_binding_commands.get(pseudo_id, [])
                nt_role = "graphics" if bindings else "standard"
                commands.insert(
                    0,
                    CopyClipsToNewTrackCommand.create(
                        nt_type, nt_position, bucket_clips,
                        track_role=nt_role),
                )
                commands[1:1] = bindings

        if not commands:
            return

        try:
            if len(commands) == 1:
                self._execute_command(commands[0])
            else:
                macro = MacroCommand.create(f"Copy {len(commands)} clips (Alt+drag)", commands)
                self._execute_command(macro)
        except ValueError as exc:
            logger.warning(f"Alt-drag copy rejected: {exc}")
            # R06：拒绝必须发 UI 诊断——静默失败让"松手后没有副本"无法
            # 与成功区分（双自动轨反例即在此路径无提示丢失整次操作）。
            self.status_message_requested.emit(I18N.tr(
                "timeline.copy.rejected",
                "Copy rejected: {reason}").format(reason=exc))
            # AUD-21404: 回滚执行前项目引用——见上方 project_before_copy
            # 注释；回滚后状态与失败前一致，无需（也不应）广播
            # project_changed。
            self._project = project_before_copy
            return
        except Exception:
            # 非预期失败同样不能留下孤儿 asset；异常按原语义继续上抛，
            # 由上层 Qt 槽的兜底处理接住。
            self._project = project_before_copy
            raise
        # Notify listeners when new assets were created (solid clip copies).
        if self._project is not None:
            self.project_changed.emit(self._project)
        logger.info(f"Alt-drag copy: {len(commands)} clips copied, delta={delta}")

    def _on_clip_trim_left(self, clip_id: str, track_id: str, new_start: int) -> None:
        """Handle left trim during drag (live preview only, with linked clip sync)."""
        if self._timeline is None:
            return
        if self._is_track_locked(track_id):
            return

        # Check for Shift key to temporarily disable snap (参考 kdenlive)
        modifiers = QApplication.keyboardModifiers()
        self._snap_temporarily_disabled = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        item = self._items_map.get(clip_id)
        if not (item and isinstance(item, ClipItem)):
            return

        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return

        # Apply snap if enabled (visual preview)
        if self._snap_enabled:
            snap_points = self._get_snap_points(exclude_clip_id=clip_id)
            # AUD-21402: SnapManager 路径显式排除自身，避免左/右修剪时
            # 边缘吸附到本片段另一条边缘。
            snapped_start, did_snap = self._snap_frame(
                new_start, snap_points, exclude_source_ids={clip_id})
            if did_snap:
                new_start = snapped_start

        # Boundary constraint: prevent trim from moving clip before time 00:00
        if new_start < 0:
            new_start = 0

        # Always update clip item visual position (snapped or raw)
        new_duration = clip.timeline_end - new_start
        if new_duration > 0:
            # Calculate delta for linked clip sync
            delta = new_start - clip.timeline_start

            item._start_frame = new_start
            item._duration_frames = new_duration
            # RI-07/R18：预览与 release 共用同一方向/速度/FPS 感知映射
            #（此前 timeline delta 直接当源 delta，2x 拖中 190 释放跳 180）
            pv_source_start, pv_source_duration = (
                clip.with_left_edge_source_values(new_start)
            )
            item.update_trim_preview_source_range(
                pv_source_start, pv_source_duration
            )
            item._update_geometry()
            # Update X position for left trim (clip moves left/right)
            self._configure_layout_engine()
            x = self._layout_engine.frame_to_x(new_start)
            item.setPos(x, item.y())
            item.update()

            # R07①：单边 trim 监视器（被修边真实帧 + 邻片/空位）
            self._show_single_trim_monitor(clip, "left", new_start)

            # Sync linked clip visual preview if applicable
            if self._get_reciprocal_linked_clip(clip) is not None and self._effective_linked_selection():
                linked_item = self._items_map.get(clip.linked_clip_id)
                linked_clip = self._timeline.get_clip_by_id(clip.linked_clip_id)
                if linked_item and linked_clip:
                    # RI-02/R14：linked 伙伴被直接改写——登记，取消时重投影
                    self._preview_affected_item_ids.add(linked_clip.clip_id)
                    linked_new_start = linked_clip.timeline_start + delta
                    linked_new_duration = linked_clip.timeline_end - linked_new_start
                    if linked_new_duration > 0:
                        linked_item._start_frame = linked_new_start
                        linked_item._duration_frames = linked_new_duration
                        # R18：linked 伙伴按自身速度/FPS 换算源区间
                        lp_source_start, lp_source_duration = (
                            linked_clip.with_left_edge_source_values(
                                linked_new_start
                            )
                        )
                        linked_item.update_trim_preview_source_range(
                            lp_source_start, lp_source_duration
                        )
                        linked_item._update_geometry()
                        linked_x = self._layout_engine.frame_to_x(linked_new_start)
                        linked_item.setPos(linked_x, linked_item.y())
                        linked_item.update()

    def _motion_graphic_page_count(self, clip: ClipInstance) -> int:
        """Resolve the authored page count used by Shift-drag retiming."""
        try:
            from src.features.teaching_animation.host_adapters import motion_graphic_scene_id
            from src.features.teaching_animation.scene_store import get_shared_scene_store
            from src.features.teaching_animation.core.html_page_deck import detect_page_count

            scene_id = motion_graphic_scene_id(getattr(clip, "asset_id", ""))
            scene = get_shared_scene_store().get(scene_id) if scene_id else None
            asset = (
                self._project.assets.get(str(getattr(clip, "asset_id", "")))
                if self._project is not None else None
            )
            asset_metadata = getattr(asset, "metadata", {}) or {}
            try:
                asset_count = int(asset_metadata.get("page_count", 0) or 0)
            except (TypeError, ValueError):
                asset_count = 0
            if scene is None:
                return max(1, asset_count)
            layer = getattr(scene, "html_layer", None)
            layer_metadata = getattr(layer, "metadata", {}) or {}
            for raw in (
                layer_metadata.get("page_count"),
                (getattr(scene, "metadata", {}) or {}).get("page_count"),
                asset_count,
            ):
                try:
                    count = int(raw)
                except (TypeError, ValueError):
                    count = 0
                if count > 0:
                    return count
            html = str(getattr(layer, "sanitized_html", "") or "")
            return max(1, int(detect_page_count(html))) if html else 1
        except Exception:  # pragma: no cover - optional teaching feature
            return 1

    def _motion_graphic_min_duration_frames(self, clip: ClipInstance) -> int:
        """At least 0.5 seconds per authored page, in timeline frames."""
        fps = float(getattr(self._timeline, "fps", EMPTY_TIMELINE_FPS) or EMPTY_TIMELINE_FPS)
        return max(1, int(math.ceil(self._motion_graphic_page_count(clip) * 0.5 * fps)))

    def _on_motion_graphic_duration_changed(
        self, clip_id: str, track_id: str, new_duration: int,
    ) -> None:
        """Live-preview Shift+right-edge retiming without changing source range."""
        if self._timeline is None or self._updating or self._is_track_locked(track_id):
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        try:
            from src.features.teaching_animation.host_adapters import is_motion_graphic_asset_id
            if not is_motion_graphic_asset_id(getattr(clip, "asset_id", "")):
                return
        except Exception:
            return
        item = self._items_map.get(clip_id)
        if not isinstance(item, ClipItem):
            return
        clamped = max(self._motion_graphic_min_duration_frames(clip), int(new_duration))
        if clamped <= 0:
            return
        item._duration_frames = clamped
        item._update_geometry()
        item.update()

    def _on_motion_graphic_duration_change_finished(
        self, clip_id: str, track_id: str, old_duration: int, new_duration: int,
    ) -> None:
        """Commit Shift-drag retiming as one undoable cache-invalidating edit."""
        if self._timeline is None or self._updating or self._is_track_locked(track_id):
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        item = self._items_map.get(clip_id)
        if clip is None or not isinstance(item, ClipItem):
            return
        try:
            from src.features.teaching_animation.host_adapters import is_motion_graphic_asset_id
            if not is_motion_graphic_asset_id(getattr(clip, "asset_id", "")):
                return
        except Exception:
            return

        new_duration = max(
            self._motion_graphic_min_duration_frames(clip), int(new_duration)
        )
        old_duration = max(1, int(old_duration))
        if new_duration == old_duration:
            item._duration_frames = old_duration
            item._update_geometry()
            item.update()
            return
        if not self._validate_trim_right(clip_id, track_id, new_duration):
            item._duration_frames = old_duration
            item._update_geometry()
            item.update()
            logger.warning("Motion Graphic retime rejected: overlap with next clip")
            return

        command = RetimeMotionGraphicClipCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            new_duration=new_duration,
            old_duration=old_duration,
        )
        self._execute_command(command)

    def _on_clip_trim_right(self, clip_id: str, track_id: str, new_duration: int) -> None:
        """Handle right trim during drag (live preview only, with linked clip sync)."""
        if self._timeline is None:
            return
        if self._is_track_locked(track_id):
            return

        # Check for Shift key to temporarily disable snap (参考 kdenlive)
        modifiers = QApplication.keyboardModifiers()
        self._snap_temporarily_disabled = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return

        item = self._items_map.get(clip_id)
        if not (item and isinstance(item, ClipItem)):
            return

        # Apply snap if enabled (visual preview)
        if self._snap_enabled:
            # Snap the end frame (start + duration)
            new_end = clip.timeline_start + new_duration
            snap_points = self._get_snap_points(exclude_clip_id=clip_id)
            # AUD-21402: SnapManager 路径显式排除自身（见 _on_clip_moved_impl）
            snapped_end, did_snap = self._snap_frame(
                new_end, snap_points, exclude_source_ids={clip_id})
            if did_snap:
                new_duration = snapped_end - clip.timeline_start

        # Always update clip item visual duration (snapped or raw)
        if new_duration > 0:
            # Calculate duration delta for linked clip sync
            duration_delta = new_duration - clip.timeline_duration

            item._duration_frames = new_duration
            # RI-07/R18：预览与 release 共用同一映射（方向/速度/FPS）
            pv_source_start, pv_source_duration = (
                clip.with_right_edge_source_values(new_duration)
            )
            item.update_trim_preview_source_range(
                pv_source_start, pv_source_duration
            )
            item._update_geometry()
            item.update()

            # Sync linked clip visual preview if applicable
            if self._get_reciprocal_linked_clip(clip) is not None and self._effective_linked_selection():
                linked_item = self._items_map.get(clip.linked_clip_id)
                linked_clip = self._timeline.get_clip_by_id(clip.linked_clip_id)
                if linked_item and linked_clip:
                        # RI-02/R14：linked 伙伴被直接改写——登记，取消时重投影
                        self._preview_affected_item_ids.add(linked_clip.clip_id)
                        linked_new_duration = linked_clip.timeline_duration + duration_delta
                        if linked_new_duration > 0:
                            linked_item._duration_frames = linked_new_duration
                            # R18：linked 伙伴按自身速度/FPS 换算
                            rp_source_start, rp_source_duration = (
                                linked_clip.with_right_edge_source_values(
                                    linked_new_duration
                                )
                            )
                            linked_item.update_trim_preview_source_range(
                                rp_source_start, rp_source_duration
                            )
                            linked_item._update_geometry()
                        linked_item.update()

            # R07①：单边 trim 监视器（被修边真实帧 + 邻片/空位）
            self._show_single_trim_monitor(
                clip, "right", clip.timeline_start + new_duration)

    def _revert_trim_visuals_to_model(self, clip_id: str,
                                      include_linked: bool = True) -> None:
        """N09/T3-01：把 trim 相关 Item 的几何/源区间归回 canonical 模型。

        链接伙伴轨锁等领域拒绝发生时模型未被修改，Item 却已带着拖拽
        预览；从当前（未变的）模型重投影主片与链接伙伴，不留假预览。
        """
        revert_ids = [clip_id]
        if include_linked and self._timeline is not None:
            clip = self._timeline.get_clip_by_id(clip_id)
            if (clip is not None and clip.linked_clip_id
                    and self._get_reciprocal_linked_clip(clip) is not None
                    and self._effective_linked_selection()):
                revert_ids.append(clip.linked_clip_id)
        for target_id in revert_ids:
            model_clip = (self._timeline.get_clip_by_id(target_id)
                          if self._timeline is not None else None)
            item = self._items_map.get(target_id)
            if item is None or not isinstance(item, ClipItem):
                continue
            if model_clip is None:
                continue
            item._start_frame = model_clip.timeline_start
            item._duration_frames = model_clip.timeline_duration
            item.update_source_range(
                model_clip.source_start,
                self._get_clip_source_duration(target_id),
            )
            item._update_geometry()
            item.update()

    def _on_trim_left_finished(
        self,
        clip_id: str,
        track_id: str,
        old_start: int,
        old_duration: int,
        old_source_start: int,
        new_start: int,
        new_source_start: int
    ) -> None:
        """Handle left trim finished: create and execute TrimClipCommand (with linked clip sync)."""
        if self._timeline is None or self._updating:
            return

        # Hide snap indicator after trim finishes
        if self._snap_indicator:
            self._snap_indicator.hide_indicator()

        # Apply snap if enabled (final position)
        if self._snap_enabled:
            snap_points = self._get_snap_points(exclude_clip_id=clip_id)
            # AUD-21402: SnapManager 路径显式排除自身（见 _on_clip_moved_impl）
            new_start, _ = self._snap_frame(
                new_start, snap_points, show_indicator=False,
                exclude_source_ids={clip_id})
            # Recalculate source start based on snapped position
            # C06/INT-12：经模型的方向/速度感知映射换算源区间——此前
            # source += timeline_delta 把时间线帧当素材帧（2x 修 10 帧
            # 只裁 10 源帧，应 20；snap 还会覆盖正确输入）
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip:
                new_source_start, new_left_source_duration = (
                    clip.with_left_edge_source_values(new_start)
                )
        else:
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip is not None:
                new_source_start, new_left_source_duration = (
                    clip.with_left_edge_source_values(new_start)
                )
            else:
                new_left_source_duration = None

        # Check trim constraints (cannot exceed source bounds)
        if new_source_start < 0:
            # Invalid trim: revert visual
            item = self._items_map.get(clip_id)
            if item and isinstance(item, ClipItem):
                item._start_frame = old_start
                item._duration_frames = old_duration
                item.update_source_range(old_source_start, self._get_clip_source_duration(clip_id))
                item._update_geometry()
                item.update()
            # Also revert linked clip if applicable
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip and self._get_reciprocal_linked_clip(clip) is not None and self._effective_linked_selection():
                linked_item = self._items_map.get(clip.linked_clip_id)
                linked_clip = self._timeline.get_clip_by_id(clip.linked_clip_id)
                if linked_item and linked_clip:
                    linked_item._start_frame = linked_clip.timeline_start
                    linked_item._duration_frames = linked_clip.timeline_duration
                    linked_item.update_source_range(
                        linked_clip.source_start, linked_clip.source_duration
                    )
                    linked_item._update_geometry()
                    linked_item.update()
            logger.warning("Trim left rejected: would exceed source bounds")
            return

        # Check timeline boundary: cannot move before time 00:00
        if new_start < 0:
            # Invalid trim: revert visual
            item = self._items_map.get(clip_id)
            if item and isinstance(item, ClipItem):
                item._start_frame = old_start
                item._duration_frames = old_duration
                item.update_source_range(old_source_start, self._get_clip_source_duration(clip_id))
                item._update_geometry()
                item.update()
            # Also revert linked clip if applicable
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip and self._get_reciprocal_linked_clip(clip) is not None and self._effective_linked_selection():
                linked_item = self._items_map.get(clip.linked_clip_id)
                linked_clip = self._timeline.get_clip_by_id(clip.linked_clip_id)
                if linked_item and linked_clip:
                    linked_item._start_frame = linked_clip.timeline_start
                    linked_item._duration_frames = linked_clip.timeline_duration
                    linked_item.update_source_range(
                        linked_clip.source_start, linked_clip.source_duration
                    )
                    linked_item._update_geometry()
                    linked_item.update()
            logger.warning("Trim left rejected: would move clip before time 00:00")
            return

        # Check for overlap with previous clip
        if not self._validate_trim_left(clip_id, track_id, new_start):
            # Invalid trim: revert visual
            item = self._items_map.get(clip_id)
            if item and isinstance(item, ClipItem):
                item._start_frame = old_start
                item._duration_frames = old_duration
                item.update_source_range(old_source_start, self._get_clip_source_duration(clip_id))
                item._update_geometry()
                item.update()
            # Also revert linked clip if applicable
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip and self._get_reciprocal_linked_clip(clip) is not None and self._effective_linked_selection():
                linked_item = self._items_map.get(clip.linked_clip_id)
                linked_clip = self._timeline.get_clip_by_id(clip.linked_clip_id)
                if linked_item and linked_clip:
                    linked_item._start_frame = linked_clip.timeline_start
                    linked_item._duration_frames = linked_clip.timeline_duration
                    linked_item.update_source_range(
                        linked_clip.source_start, linked_clip.source_duration
                    )
                    linked_item._update_geometry()
                    linked_item.update()
            logger.warning("Trim left rejected: overlap with previous clip")
            return

        # Build commands (primary + linked clips if applicable)
        commands = []
        delta = new_start - old_start

        # Primary clip trim command
        cmd = TrimClipCommand.create_left_trim(
            track_id=track_id,
            clip_id=clip_id,
            new_start=new_start,
            new_source_start=new_source_start,
            old_start=old_start,
            old_source_start=old_source_start,
            old_duration=old_duration,
            old_source_duration=self._get_clip_source_duration(clip_id),
            new_source_duration=new_left_source_duration,
        )
        commands.append(cmd)

        # Sync linked clip if applicable
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip and self._get_reciprocal_linked_clip(clip) is not None and self._effective_linked_selection():
            linked_clip = self._timeline.get_clip_by_id(clip.linked_clip_id)
            linked_item = self._items_map.get(clip.linked_clip_id)
            if linked_clip and linked_item:
                # N09/T3-01：锁伙伴在组内预检——开放 V 链接锁 A 时整组
                # 拒绝（链接组完整性），不再让宏在 apply 中途抛
                # ValueError 且 release 后预览残留。
                if self._is_track_locked(linked_clip.track_id):
                    self._revert_trim_visuals_to_model(
                        clip_id, include_linked=True)
                    logger.warning(
                        "Trim left rejected: linked track %s is locked",
                        linked_clip.track_id)
                    return
                # C06：链接伙伴按自身 timebase（速度/fps/方向）换算源区间
                linked_new_start = linked_clip.timeline_start + delta
                (
                    linked_new_source_start,
                    linked_new_source_duration,
                ) = linked_clip.with_left_edge_source_values(linked_new_start)
                linked_old_start = linked_clip.timeline_start
                linked_old_duration = linked_clip.timeline_duration
                linked_old_source_start = linked_clip.source_start

                # Validate linked clip trim constraints
                if linked_new_source_start >= 0 and self._validate_trim_left(
                    linked_clip.clip_id, linked_clip.track_id, linked_new_start
                ):
                    linked_cmd = TrimClipCommand.create_left_trim(
                        track_id=linked_clip.track_id,
                        clip_id=linked_clip.clip_id,
                        new_start=linked_new_start,
                        new_source_start=linked_new_source_start,
                        old_start=linked_old_start,
                        old_source_start=linked_old_source_start,
                        old_duration=linked_old_duration,
                        old_source_duration=self._get_clip_source_duration(linked_clip.clip_id),
                        new_source_duration=linked_new_source_duration,
                    )
                    commands.append(linked_cmd)
                else:
                    # Linked clip trim invalid - revert primary and linked
                    item = self._items_map.get(clip_id)
                    if item and isinstance(item, ClipItem):
                        item._start_frame = old_start
                        item._duration_frames = old_duration
                        item.update_source_range(old_source_start, self._get_clip_source_duration(clip_id))
                        item._update_geometry()
                        item.update()
                    linked_item._start_frame = linked_clip.timeline_start
                    linked_item._duration_frames = linked_clip.timeline_duration
                    linked_item.update_source_range(
                        linked_clip.source_start, linked_clip.source_duration
                    )
                    linked_item._update_geometry()
                    linked_item.update()
                    logger.warning("Trim left rejected: linked clip constraints invalid")
                    return

        # Execute as single atomic MacroCommand (one undo reverts all)
        # N09/T3-01：领域拒绝（锁轨/校验 ValueError）在提交边界收敛，
        # Item 从 canonical 恢复，不再冒未捕获异常。
        try:
            if len(commands) == 1:
                self._execute_command(commands[0])
            else:
                macro = MacroCommand.create("Trim linked clips left", commands)
                self._execute_command(macro)
        except (TimelineOperationError, ValueError) as exc:
            self._revert_trim_visuals_to_model(clip_id, include_linked=True)
            logger.warning("Trim left rejected: %s", exc)

    def _on_trim_right_finished(
        self,
        clip_id: str,
        track_id: str,
        old_duration: int,
        old_source_duration: int,
        new_duration: int,
        new_source_duration: int
    ) -> None:
        """Handle right trim finished: create and execute TrimClipCommand (with linked clip sync)."""
        if self._timeline is None or self._updating:
            return

        # Hide snap indicator after trim finishes
        if self._snap_indicator:
            self._snap_indicator.hide_indicator()

        # Apply snap if enabled (final position)
        if self._snap_enabled:
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip:
                new_end = clip.timeline_start + new_duration
                snap_points = self._get_snap_points(exclude_clip_id=clip_id)
                # AUD-21402: SnapManager 路径显式排除自身（见 _on_clip_moved_impl）
                snapped_end, _ = self._snap_frame(
                    new_end, snap_points, show_indicator=False,
                    exclude_source_ids={clip_id})
                new_duration = snapped_end - clip.timeline_start
                # Recalculate source range（C06/INT-12：模型映射）
                new_source_start_r, new_source_duration = (
                    clip.with_right_edge_source_values(new_duration)
                )
            else:
                new_source_start_r = None
        else:
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip is not None:
                new_source_start_r, new_source_duration = (
                    clip.with_right_edge_source_values(new_duration)
                )
            else:
                new_source_start_r = None

        # Check trim constraints (cannot exceed source bounds)
        # C17：上界校验必须含 source_start——只比 duration<=max 会把非零
        # In 点的右延长放到素材之外（总源 200、In100、延长 150：exclusive
        # end 250 > 200 仍放行）。
        item = self._items_map.get(clip_id)
        if item and isinstance(item, ClipItem):
            max_source = item._max_source_frames
            check_start = (
                new_source_start_r
                if new_source_start_r is not None
                else item._source_start
            )
            exceeds_source = (
                max_source > 0
                and (
                    new_source_duration > max_source
                    or check_start + new_source_duration > max_source
                )
            )
            if exceeds_source:
                # Invalid trim: revert visual
                item._duration_frames = old_duration
                item.update_source_range(item._source_start, old_source_duration)
                item._update_geometry()
                item.update()
                # Also revert linked clip if applicable
                clip = self._timeline.get_clip_by_id(clip_id)
                if clip and self._get_reciprocal_linked_clip(clip) is not None and self._effective_linked_selection():
                    linked_item = self._items_map.get(clip.linked_clip_id)
                    linked_clip = self._timeline.get_clip_by_id(clip.linked_clip_id)
                    if linked_item and linked_clip:
                        linked_item._duration_frames = linked_clip.timeline_duration
                        linked_item.update_source_range(
                            linked_clip.source_start, linked_clip.source_duration
                        )
                        linked_item._update_geometry()
                        linked_item.update()
                logger.warning("Trim right rejected: would exceed source bounds")
                return

        # Check for overlap with next clip
        if not self._validate_trim_right(clip_id, track_id, new_duration):
            # Invalid trim: revert visual
            item = self._items_map.get(clip_id)
            if item and isinstance(item, ClipItem):
                item._duration_frames = old_duration
                item.update_source_range(item._source_start, old_source_duration)
                item._update_geometry()
                item.update()
            # Also revert linked clip if applicable
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip and self._get_reciprocal_linked_clip(clip) is not None and self._effective_linked_selection():
                linked_item = self._items_map.get(clip.linked_clip_id)
                linked_clip = self._timeline.get_clip_by_id(clip.linked_clip_id)
                if linked_item and linked_clip:
                    linked_item._duration_frames = linked_clip.timeline_duration
                    linked_item.update_source_range(
                        linked_clip.source_start, linked_clip.source_duration
                    )
                    linked_item._update_geometry()
                    linked_item.update()
            logger.warning("Trim right rejected: overlap with next clip")
            return

        # Build commands (primary + linked clips if applicable)
        commands = []
        duration_delta = new_duration - old_duration

        # Primary clip trim command
        clip_for_right = self._timeline.get_clip_by_id(clip_id)
        if new_source_start_r is None and clip_for_right is not None:
            new_source_start_r, _ = clip_for_right.with_right_edge_source_values(
                new_duration
            )
        cmd = TrimClipCommand.create_right_trim(
            track_id=track_id,
            clip_id=clip_id,
            new_duration=new_duration,
            new_source_duration=new_source_duration,
            old_duration=old_duration,
            old_source_duration=old_source_duration,
            new_source_start=new_source_start_r,
        )
        commands.append(cmd)

        # Sync linked clip if applicable
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip and self._get_reciprocal_linked_clip(clip) is not None and self._effective_linked_selection():
            linked_clip = self._timeline.get_clip_by_id(clip.linked_clip_id)
            linked_item = self._items_map.get(clip.linked_clip_id)
            if linked_clip and linked_item:
                # N09/T3-01：锁伙伴组内预检，整组拒绝（同左侧 trim）。
                if self._is_track_locked(linked_clip.track_id):
                    self._revert_trim_visuals_to_model(
                        clip_id, include_linked=True)
                    logger.warning(
                        "Trim right rejected: linked track %s is locked",
                        linked_clip.track_id)
                    return
                # C06：链接伙伴按自身 timebase 换算源区间
                linked_new_duration = linked_clip.timeline_duration + duration_delta
                (
                    linked_new_source_start,
                    linked_new_source_duration,
                ) = linked_clip.with_right_edge_source_values(linked_new_duration)
                linked_old_duration = linked_clip.timeline_duration
                linked_old_source_duration = linked_clip.source_duration

                # Validate linked clip trim constraints
                max_linked_source = linked_item._max_source_frames
                linked_valid = (
                    linked_new_duration > 0 and
                    linked_new_source_duration > 0 and
                    (max_linked_source <= 0 or linked_new_source_duration <= max_linked_source) and
                    self._validate_trim_right(linked_clip.clip_id, linked_clip.track_id, linked_new_duration)
                )

                if linked_valid:
                    linked_cmd = TrimClipCommand.create_right_trim(
                        track_id=linked_clip.track_id,
                        clip_id=linked_clip.clip_id,
                        new_duration=linked_new_duration,
                        new_source_duration=linked_new_source_duration,
                        old_duration=linked_old_duration,
                        old_source_duration=linked_old_source_duration,
                        new_source_start=linked_new_source_start,
                    )
                    commands.append(linked_cmd)
                else:
                    # Linked clip trim invalid - revert primary and linked
                    item = self._items_map.get(clip_id)
                    if item and isinstance(item, ClipItem):
                        item._duration_frames = old_duration
                        item.update_source_range(item._source_start, old_source_duration)
                        item._update_geometry()
                        item.update()
                    linked_item._duration_frames = linked_clip.timeline_duration
                    linked_item.update_source_range(
                        linked_clip.source_start, linked_clip.source_duration
                    )
                    linked_item._update_geometry()
                    linked_item.update()
                    logger.warning("Trim right rejected: linked clip constraints invalid")
                    return

        # Execute as single atomic MacroCommand (one undo reverts all)
        # N09/T3-01：领域拒绝在提交边界收敛（同左侧 trim）。
        try:
            if len(commands) == 1:
                self._execute_command(commands[0])
            else:
                macro = MacroCommand.create("Trim linked clips right", commands)
                self._execute_command(macro)
        except (TimelineOperationError, ValueError) as exc:
            self._revert_trim_visuals_to_model(clip_id, include_linked=True)
            logger.warning("Trim right rejected: %s", exc)

    # ==================== Slip Handlers (P4.4) ====================

    def _source_edit_plan(
        self, clip_id: str, track_id: str, delta_frames: int, *, slide: bool,
    ) -> tuple[Any, list[Any], int, set[str]]:
        """Preflight the complete linked gesture against one immutable timeline."""
        from src.features.edit.core.timeline_ops import (
            get_slide_bounds, get_slip_bounds, slide_clip, slip_clip,
        )
        sequence = self._timeline
        if sequence is not self._source_edit_timeline_at_press:
            raise ValueError("Timeline changed during the Slip/Slide gesture")
        clip = sequence.get_clip_by_id(clip_id)
        if clip is None or clip.track_id != track_id:
            raise ValueError("Slip/Slide clip no longer exists on this track")
        if self._source_edit_linked_at_press != self._effective_linked_selection():
            raise ValueError("Linked selection changed during the gesture")
        clips = [clip]
        if self._effective_linked_selection() and clip.linked_clip_id:
            partner = sequence.get_reciprocal_linked_clip(clip_id)
            if (partner is None or partner.track_id == track_id
                    or (partner.timeline_start, partner.timeline_end)
                    != (clip.timeline_start, clip.timeline_end)):
                raise ValueError("Linked clip boundaries differ; disable linked selection to edit independently")
            if not slide:
                from fractions import Fraction
                def source_fps(member: Any) -> Any:
                    return Fraction(member._source_fps_num or sequence.fps.num,
                                    (member._source_fps_den or 1) if member._source_fps_num else sequence.fps.den)
                if source_fps(clip) != source_fps(partner):
                    raise ValueError("Linked Slip requires matching source frame rates")
            clips.append(partner)
        minimum, maximum = -(2**31), 2**31 - 1
        limits = []
        affected: set[str] = set()
        primary_neighbors = sequence.get_track_by_id(track_id).get_adjacent_clips(clip_id)
        for member in clips:
            track = sequence.get_track_by_id(member.track_id)
            if track is None or track.is_locked:
                raise ValueError(f"Track is locked: {member.track_id}")
            affected.add(member.clip_id)
            if slide:
                neighbors = track.get_adjacent_clips(member.clip_id)
                if len(clips) > 1 and member is not clip:
                    for first, second in zip(primary_neighbors, neighbors):
                        if ((first is None) != (second is None)
                                or (first is not None and (
                                    sequence.get_reciprocal_linked_clip(first.clip_id) != second
                                    or (first.timeline_start, first.timeline_end)
                                    != (second.timeline_start, second.timeline_end)))):
                            raise ValueError("Linked Slide requires matching neighboring boundaries")
                bounds = tuple(self._roll_clip_limit(n) if n is not None else None
                               for n in neighbors)
                lo, hi = get_slide_bounds(sequence, member.clip_id, *bounds)
                affected.update(n.clip_id for n in neighbors if n is not None)
            else:
                bounds = (self._roll_clip_limit(member),)
                lo, hi = get_slip_bounds(sequence, member.clip_id, bounds[0])
            if any(not isinstance(self._items_map.get(cid), ClipItem) for cid in affected):
                raise ValueError("Slip/Slide preview items are unavailable")
            minimum, maximum = max(minimum, lo), min(maximum, hi)
            limits.append(bounds)
        if minimum > maximum:
            raise ValueError("Linked clips have no common edit range")
        delta = max(minimum, min(maximum, int(delta_frames)))
        preview, commands = sequence, []
        if delta:
            for member, bounds in zip(clips, limits):
                if slide:
                    preview = slide_clip(preview, member.clip_id, delta, *bounds)
                    commands.append(SlideClipCommand.create(
                        sequence, member.track_id, member.clip_id, delta,
                        prev_max_source_frames=bounds[0], next_max_source_frames=bounds[1]))
                else:
                    preview = slip_clip(preview, member.clip_id, delta, bounds[0])
                    commands.append(SlipClipCommand.create(
                        member.track_id, member.clip_id, delta, bounds[0]))
        return preview, commands, delta, affected

    def _preview_source_edit(
        self, clip_id: str, track_id: str, delta_frames: int, *, slide: bool,
    ) -> None:
        """Project all linked/neighbor source windows; never publish a preview model."""
        state = InteractionState.SLIDING if slide else InteractionState.SLIPPING
        if (self._timeline is None or self._updating or self._interaction.state != state
                or self._interaction.primary_clip_id != clip_id):
            return
        try:
            preview, _, delta, affected = self._source_edit_plan(
                clip_id, track_id, delta_frames, slide=slide)
        except (TimelineOperationError, ValueError) as exc:
            self.status_message_requested.emit(str(exc))
            logger.warning("Slip/Slide preview rejected: %s", exc)
            self._cancel_current_interaction()
            return
        self._preview_affected_item_ids.update(affected)
        for cid in affected:
            clip, item = preview.get_clip_by_id(cid), self._items_map[cid]
            if not item._trim_preview_active:
                item.begin_trim_preview()
            item._start_frame, item._duration_frames = clip.timeline_start, clip.timeline_duration
            item.update_trim_preview_source_range(clip.source_start, clip.source_duration)
            item._update_geometry()
            item.setPos(self._layout_engine.frame_to_x(clip.timeline_start), item.y())
            item.update()
        self._items_map[clip_id]._slip_delta = delta
        # R07①：slip 监视器——新源窗首末帧（slide 不改源窗，不进监视器）
        if not slide:
            self._show_slip_monitor(preview.get_clip_by_id(clip_id), delta)

    def _finish_source_edit(
        self, clip_id: str, track_id: str, delta_frames: int, *, slide: bool,
    ) -> None:
        """Revalidate release and publish one command after restoring preview items."""
        state = InteractionState.SLIDING if slide else InteractionState.SLIPPING
        if (self._timeline is None or self._updating or self._interaction.state != state
                or self._interaction.primary_clip_id != clip_id):
            return
        try:
            _, commands, _, _ = self._source_edit_plan(
                clip_id, track_id, delta_frames, slide=slide)
            self._reproject_preview_affected_items()
            if commands:
                command = commands[0] if len(commands) == 1 else MacroCommand.create(
                    "Slide linked clips" if slide else "Slip linked clips", commands)
                self._execute_command(command)
        except (TimelineOperationError, ValueError) as exc:
            self.status_message_requested.emit(str(exc))
            logger.warning("Slip/Slide rejected: %s", exc)
        finally:
            self._reproject_preview_affected_items()
            self._interaction.finish()

    def _on_clip_slip(self, clip_id: str, track_id: str, delta_frames: int) -> None:
        """Preview Slip with the common linked source-frame offset."""
        self._preview_source_edit(clip_id, track_id, delta_frames, slide=False)

    def _on_slip_finished(
        self, clip_id: str, track_id: str, old_source_start: int,
        new_source_start: int, max_source_frames: int,
    ) -> None:
        """Commit the active Slip gesture as one undo step."""
        self._finish_source_edit(
            clip_id, track_id, new_source_start - old_source_start, slide=False)

    # ==================== Roll Trim Handlers (P4.3) ====================

    def _on_roll_trim_started(
        self,
        left_clip_id: str,
        right_clip_id: str,
        cut_frame: int
    ) -> None:
        """
        Handle roll trim started: prepare for live preview.

        Roll trim moves the cut point between two adjacent clips.
        """
        if self._timeline is None:
            return
        self._independent_selection = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier)

        left_clip = self._timeline.get_clip_by_id(left_clip_id)
        right_clip = self._timeline.get_clip_by_id(right_clip_id)
        if left_clip is None or right_clip is None:
            return

        left_item = self._items_map.get(left_clip_id)
        right_item = self._items_map.get(right_clip_id)
        if not isinstance(left_item, ClipItem) or not isinstance(right_item, ClipItem):
            return

        from src.features.edit.core.linked_roll import linked_roll_pairs
        try:
            pairs = linked_roll_pairs(
                self._timeline, left_clip_id, right_clip_id,
                self._effective_linked_selection())
        except ValueError as exc:
            logger.warning("Roll rejected: %s", exc)
            self.status_message_requested.emit(str(exc))
            return
        # Selecting an edit point replaces the former body selection. Keep
        # the two edges as the UI target, rather than leaving another clip
        # looking selected while the pointer edits this seam.
        self.set_selection(TimelineSelection())
        self._set_selected_edit_edges({
            clip.clip_id: edge for pair in pairs
            for clip, edge in ((pair[0], "right"), (pair[1], "left"))
        })
        for pair in pairs:
            for clip in pair:
                item = self._items_map.get(clip.clip_id)
                if isinstance(item, ClipItem):
                    item.begin_trim_preview()

        handle = next(
            (
                candidate
                for candidate in self._roll_handles.values()
                if candidate.left_clip_id == left_clip_id
                and candidate.right_clip_id == right_clip_id
            ),
            None,
        )
        self._roll_preview_state = {
            "left_clip_id": left_clip_id,
            "right_clip_id": right_clip_id,
            "left_item": left_item,
            "right_item": right_item,
            "handle": handle,
            "delta": 0,
            "pair_ids": tuple((a.clip_id, b.clip_id) for a, b in pairs),
        }

        # RI-01/R13：Roll 正式进入交互会话——Esc/切工具/失焦由此能取消
        # 整个双片预览。此前状态机保持 IDLE，取消路径全部不触发，
        # release 仍照常提交。
        self._interaction.begin(
            InteractionState.ROLLING,
            QPointF(float(cut_frame), 0.0),
        )

        logger.debug(
            f"Roll trim started at cut frame {cut_frame} "
            f"between {left_clip_id} and {right_clip_id}"
        )

        # P1-04 Trim Monitor：cut 两侧 TC + 手柄诊断 + 帧预览请求
        self._roll_preview_state["cut_frame"] = int(cut_frame)
        self._show_roll_trim_monitor(left_clip, right_clip, int(cut_frame), 0)
        self.trim_frame_preview_requested.emit(int(cut_frame))

    def _on_roll_trim_dragged(self, delta_frames: int) -> None:
        """
        Handle roll trim dragging: live preview of both clips.

        Roll trim preview:
        - Left clip extends/shortens from right edge
        - Right clip shifts start and shortens/extends opposite
        """
        if self._timeline is None or self._updating:
            return

        state = self._roll_preview_state
        if state is None:
            return

        left_clip = self._timeline.get_clip_by_id(state["left_clip_id"])
        right_clip = self._timeline.get_clip_by_id(state["right_clip_id"])
        left_item = state["left_item"]
        right_item = state["right_item"]
        if (
            left_clip is None
            or right_clip is None
            or not isinstance(left_item, ClipItem)
            or not isinstance(right_item, ClipItem)
        ):
            return

        # The domain bounds use each clip's own speed/FPS/direction and
        # source window. Timeline ticks are not interchangeable source frames.
        from src.features.edit.core.linked_roll import linked_roll_bounds, linked_roll_pairs
        try:
            pairs = linked_roll_pairs(
                self._timeline, left_clip.clip_id, right_clip.clip_id,
                self._effective_linked_selection())
            min_delta, max_delta = linked_roll_bounds(pairs, self._roll_clip_limit)
        except ValueError as exc:
            logger.warning("Roll preview rejected: %s", exc)
            self.status_message_requested.emit(str(exc))
            self._restore_roll_preview()
            self._roll_preview_state = None
            self._hide_trim_monitor()
            self._interaction.finish()
            return
        clamped_delta = max(min_delta, min(max_delta, int(delta_frames)))

        handle = state.get("handle")
        if handle is not None and clamped_delta != int(delta_frames):
            handle.set_preview_delta(clamped_delta)
        state["delta"] = clamped_delta

        # P1-04：Trim Monitor 跟随 + cut 邻近帧预览
        self._show_roll_trim_monitor(left_clip, right_clip,
                                     int(state.get("cut_frame",
                                                   left_clip.timeline_start
                                                   + left_clip.timeline_duration)),
                                     clamped_delta)
        self.trim_frame_preview_requested.emit(
            int(state.get("cut_frame",
                          left_clip.timeline_start + left_clip.timeline_duration))
            + clamped_delta)

        left_item._duration_frames = left_clip.timeline_duration + clamped_delta
        left_item.update_trim_preview_source_range(
            *left_clip.with_right_edge_source_values(
                left_clip.timeline_duration + clamped_delta),
        )
        left_item._update_geometry()
        left_item.setPos(
            self._layout_engine.frame_to_x(left_clip.timeline_start),
            left_item.y(),
        )
        left_item.update()

        right_item._start_frame = right_clip.timeline_start + clamped_delta
        right_item._duration_frames = right_clip.timeline_duration - clamped_delta
        right_item.update_trim_preview_source_range(
            *right_clip.with_left_edge_source_values(
                right_clip.timeline_start + clamped_delta),
        )
        right_item._update_geometry()
        right_item.setPos(
            self._layout_engine.frame_to_x(right_clip.timeline_start + clamped_delta),
            right_item.y(),
        )
        right_item.update()

        for linked_left, linked_right in pairs[1:]:
            for clip, is_left in ((linked_left, True), (linked_right, False)):
                item = self._items_map.get(clip.clip_id)
                if not isinstance(item, ClipItem):
                    continue
                item._start_frame = clip.timeline_start + (0 if is_left else clamped_delta)
                item._duration_frames = clip.timeline_duration + (clamped_delta if is_left else -clamped_delta)
                source_values = (
                    clip.with_right_edge_source_values(item._duration_frames)
                    if is_left else clip.with_left_edge_source_values(item._start_frame))
                item.update_trim_preview_source_range(*source_values)
                item._update_geometry()
                item.setPos(self._layout_engine.frame_to_x(item._start_frame), item.y())
                item.update()

        logger.debug(f"Roll trim preview: delta = {clamped_delta} frames")

    @staticmethod
    def _roll_source_limit(clip: Any, item: Any) -> Optional[int]:
        """Ignore placeholder source bounds smaller than the current window."""
        maximum = int(getattr(item, "_max_source_frames", 0) or 0)
        return maximum if maximum >= clip.source_start + clip.source_duration else None

    def _roll_clip_limit(self, clip: Any) -> Optional[int]:
        """Resolve the same source limit for numeric and linked mouse Roll."""
        item = self._items_map.get(clip.clip_id)
        limit = self._roll_source_limit(clip, item)
        if limit is not None:
            return limit
        maximum = int(self._get_asset_duration(clip.asset_id) or 0)
        return maximum if maximum >= clip.source_start + clip.source_duration else None

    def _restore_roll_preview(self) -> None:
        """Restore both seam clips to model state before command execution."""
        state = self._roll_preview_state
        if state is None or self._timeline is None:
            return

        clip_ids = tuple(cid for pair in state.get("pair_ids", (
            (state["left_clip_id"], state["right_clip_id"]),)) for cid in pair)
        for clip_id in clip_ids:
            clip = self._timeline.get_clip_by_id(clip_id)
            item = self._items_map.get(clip_id)
            if clip is None or not isinstance(item, ClipItem):
                continue
            item._start_frame = clip.timeline_start
            item._duration_frames = clip.timeline_duration
            item.update_source_range(clip.source_start, clip.source_duration)
            item.end_trim_preview()
            item._update_geometry()
            item.setPos(
                self._layout_engine.frame_to_x(clip.timeline_start),
                item.y(),
            )
            item.update()

    def _on_roll_trim_finished(
        self,
        left_clip_id: str,
        right_clip_id: str,
        delta_frames: int
    ) -> None:
        """
        Handle roll trim finished: create and execute RollTrimCommand.

        Roll trim operation:
        - Left clip's duration changes (positive delta = longer)
        - Right clip's source_start and duration change opposite
        - Timeline total duration remains unchanged
        """
        # RI-01/R13：release 只有在会话仍有效时才允许提交。取消路径
        # （Esc/切工具/失焦）已恢复双片并清空状态，迟到的 release 在此
        # 直接丢弃。
        if self._interaction.state != InteractionState.ROLLING:
            return
        try:
            self._on_roll_trim_finished_active(
                left_clip_id, right_clip_id, delta_frames
            )
        finally:
            if self._roll_preview_state is not None:
                self._restore_roll_preview()
                self._roll_preview_state = None
            self._hide_trim_monitor()
            self._interaction.finish()

    def _on_roll_trim_finished_active(
        self,
        left_clip_id: str,
        right_clip_id: str,
        delta_frames: int,
    ) -> None:
        """RI-01/R13：会话校验后的原 roll 提交逻辑。"""
        if self._timeline is None or self._updating:
            return

        state = self._roll_preview_state
        if state is not None:
            delta_frames = int(state.get("delta", delta_frames))
            self._restore_roll_preview()
            self._roll_preview_state = None
        self._hide_trim_monitor()

        if delta_frames == 0:
            return

        # Get track ID from one of the clips
        left_clip = self._timeline.get_clip_by_id(left_clip_id)
        if left_clip is None:
            logger.warning(f"Roll trim failed: left clip {left_clip_id} not found")
            return


        # Create and execute RollTrimCommand
        right_clip = self._timeline.get_clip_by_id(right_clip_id)
        if right_clip is None:
            return
        try:
            from src.features.edit.core.linked_roll import build_linked_roll_command
            cmd, _ = build_linked_roll_command(
                self._timeline, left_clip_id, right_clip_id, delta_frames,
                self._roll_clip_limit, self._effective_linked_selection())
            if cmd is not None:
                self._execute_command(cmd)
        except (TimelineOperationError, ValueError) as exc:
            # A stale seam or a concurrent timeline refresh can make the
            # command invalid after the preview has already been restored.
            # Reject the edit cleanly instead of letting a domain validation
            # error abort the whole application.
            logger.warning(
                f"Roll trim rejected: {exc} "
                f"(left={left_clip_id}, right={right_clip_id}, delta={delta_frames})"
            )
            self.status_message_requested.emit(str(exc))
            return

        logger.debug(
            f"Roll trim completed: delta = {delta_frames} frames "
            f"between {left_clip_id} and {right_clip_id}"
        )

    # ==================== Slide Handlers (P4.5) ====================

    def _on_clip_slide(self, clip_id: str, track_id: str, delta_frames: int) -> None:
        """Preview Slide across matching linked seams using domain source mapping."""
        self._preview_source_edit(clip_id, track_id, delta_frames, slide=True)

    def _on_slide_finished(
        self, clip_id: str, track_id: str, delta_frames: int,
        prev_clip_id: str, next_clip_id: str,
    ) -> None:
        """Commit the complete Slide group as one undo step."""
        self._finish_source_edit(clip_id, track_id, delta_frames, slide=True)

    # ==================== Ripple Trim Handlers (P4.2) ====================

    def _on_ripple_trim_left(
        self,
        clip_id: str,
        track_id: str,
        new_start: int
    ) -> None:
        """
        Handle ripple trim left during drag (live preview).

        Ripple trim left: clip's start changes, subsequent clips shift.
        This is called during drag for live preview.
        """
        if self._timeline is None or self._updating:
            return

        # Live preview: update visual items
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        # Calculate delta
        delta = clip.timeline_start - new_start

        item = self._items_map.get(clip_id)
        if item and isinstance(item, ClipItem):
            source_delta = int(delta * clip.effective_speed())
            item.update_trim_preview_source_range(
                clip.source_start - source_delta,
                clip.source_duration + source_delta,
            )

        # Preview subsequent clips shifting
        clips_after = [c for c in track.clips if c.timeline_start > clip.timeline_start]
        for after_clip in clips_after:
            after_item = self._items_map.get(after_clip.clip_id)
            if after_item and isinstance(after_item, ClipItem):
                # P1-04 审计修复：与命令语义一致——缩短（delta<0）时后续
                # 左移收拢，延长时不动（旧预览方向反了，与提交结果跳变）
                # RI-02/R14：跟随片段被直接改写——登记，取消时重投影
                self._preview_affected_item_ids.add(after_clip.clip_id)
                preview_start = after_clip.timeline_start + (delta if delta < 0 else 0)
                after_item._start_frame = preview_start
                after_item._update_geometry()
                after_item.update()

        logger.debug(f"Ripple trim left preview: clip {clip_id} new_start {new_start}")

    def _on_ripple_trim_right(
        self,
        clip_id: str,
        track_id: str,
        new_end: int
    ) -> None:
        """
        Handle ripple trim right during drag (live preview).

        Ripple trim right: clip's end changes, subsequent clips shift.
        This is called during drag for live preview.
        """
        if self._timeline is None or self._updating:
            return

        # Live preview: update visual items
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        # Calculate delta
        delta = new_end - clip.timeline_end

        item = self._items_map.get(clip_id)
        if item and isinstance(item, ClipItem):
            source_delta = int(delta * clip.effective_speed())
            item.update_trim_preview_source_range(
                clip.source_start,
                clip.source_duration + source_delta,
            )

        # Preview subsequent clips shifting
        clips_after = [c for c in track.clips if c.timeline_start >= clip.timeline_end]
        for after_clip in clips_after:
            after_item = self._items_map.get(after_clip.clip_id)
            if after_item and isinstance(after_item, ClipItem):
                # Preview: shift by delta
                # RI-02/R14：跟随片段被直接改写——登记，取消时重投影
                self._preview_affected_item_ids.add(after_clip.clip_id)
                preview_start = after_clip.timeline_start + delta
                after_item._start_frame = preview_start
                after_item._update_geometry()
                after_item.update()

        logger.debug(f"Ripple trim right preview: clip {clip_id} new_end {new_end}")

    def _on_ripple_trim_left_finished(
        self,
        clip_id: str,
        track_id: str,
        old_start: int,
        new_start: int
    ) -> None:
        """
        Handle ripple trim left finished: create and execute RippleTrimCommand.

        Ripple trim left operation:
        - Clip's timeline_start changes to new_start
        - All subsequent clips shift by the delta
        - No gap is created
        """
        if self._timeline is None or self._updating:
            return

        # Restore subsequent clips visuals before command execution
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        clips_after = [c for c in track.clips if c.timeline_start > clip.timeline_start]
        for after_clip in clips_after:
            after_item = self._items_map.get(after_clip.clip_id)
            if after_item and isinstance(after_item, ClipItem):
                after_item._start_frame = after_clip.timeline_start
                after_item._duration_frames = after_clip.timeline_duration
                after_item._update_geometry()
                after_item.update()

        # Apply snap if enabled
        if self._snap_enabled:
            snap_points = self._get_snap_points(exclude_clip_id=clip_id)
            # AUD-21402: SnapManager 路径显式排除自身（见 _on_clip_moved_impl）
            new_start, _ = self._snap_frame(
                new_start, snap_points, show_indicator=False,
                exclude_source_ids={clip_id})

        if new_start == old_start:
            return

        # INT-01 防护：无效目标（负起点 / 与右端重合的零时长）在领域
        # 边界直接拒绝并恢复预览，不进入命令路径。
        if new_start < 0 or new_start >= clip.timeline_end:
            logger.warning(
                "Ripple trim left rejected for clip %s: invalid new_start %s "
                "(end=%s)", clip_id, new_start, clip.timeline_end
            )
            return

        # Get clip data for undo
        affected_clips = tuple(
            c for c in track.clips
            if c.timeline_start > clip.timeline_start
        )

        # C08：A/V editing-unit 原子波纹。主片段有双向链接且伙伴轨未锁
        # 时，伙伴同步波纹到相同新起点（第二条命令不重复 sync 跟随），
        # 宏内任一边失败则整体不提交。
        commands = [
            RippleTrimCommand.create_left_trim(
                track_id=track_id,
                clip_id=clip_id,
                new_start=new_start,
                original_clip=clip,
                affected_clips=affected_clips,
            )
        ]
        linked = (
            self._timeline.get_reciprocal_linked_clip(clip)
            if self._effective_linked_selection()
            else None
        )
        if linked is not None and not self._is_track_locked(linked.track_id):
            if new_start < linked.timeline_end:
                linked_track = self._timeline.get_track_by_id(linked.track_id)
                linked_affected = tuple(
                    c for c in (linked_track.clips if linked_track else ())
                    if c.timeline_start > linked.timeline_start
                )
                commands.append(
                    RippleTrimCommand.create_left_trim(
                        track_id=linked.track_id,
                        clip_id=linked.clip_id,
                        new_start=new_start,
                        original_clip=linked,
                        affected_clips=linked_affected,
                        apply_sync_lock=False,
                    )
                )
            else:
                logger.warning(
                    "Linked ripple trim left rejected: new_start %s beyond "
                    "linked clip %s end %s", new_start, linked.clip_id,
                    linked.timeline_end,
                )
                self.status_message_requested.emit(
                    self.tr("链接片段无法波纹到该位置，已取消整组操作")
                )
                return
        elif linked is not None and self._is_track_locked(linked.track_id):
            self.status_message_requested.emit(
                self.tr("链接轨道已锁定，无法整组波纹修剪")
            )
            return

        cmd = commands[0] if len(commands) == 1 else MacroCommand.create(
            "Linked ripple trim left", commands
        )
        self._execute_command(cmd)

        logger.debug(
            f"Ripple trim left completed: clip {clip_id} "
            f"start {old_start} -> {new_start}"
        )

    def _on_ripple_trim_right_finished(
        self,
        clip_id: str,
        track_id: str,
        old_end: int,
        new_end: int
    ) -> None:
        """
        Handle ripple trim right finished: create and execute RippleTrimCommand.

        Ripple trim right operation:
        - Clip's timeline_end changes to new_end
        - All subsequent clips shift by the delta
        - No gap is created
        """
        if self._timeline is None or self._updating:
            return

        # Restore subsequent clips visuals before command execution
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        clips_after = [c for c in track.clips if c.timeline_start >= clip.timeline_end]
        for after_clip in clips_after:
            after_item = self._items_map.get(after_clip.clip_id)
            if after_item and isinstance(after_item, ClipItem):
                after_item._start_frame = after_clip.timeline_start
                after_item._duration_frames = after_clip.timeline_duration
                after_item._update_geometry()
                after_item.update()

        # Apply snap if enabled
        if self._snap_enabled:
            snap_points = self._get_snap_points(exclude_clip_id=clip_id)
            # Snap the end frame
            # AUD-21402: SnapManager 路径显式排除自身（见 _on_clip_moved_impl）
            new_end, _ = self._snap_frame(
                new_end, snap_points, show_indicator=False,
                exclude_source_ids={clip_id})

        if new_end == old_end:
            return

        # INT-01 防护：无效目标（不大于 start 的零/负时长）在领域边界
        # 直接拒绝并恢复预览，不进入命令路径。
        if new_end <= clip.timeline_start:
            logger.warning(
                "Ripple trim right rejected for clip %s: invalid new_end %s "
                "(start=%s)", clip_id, new_end, clip.timeline_start
            )
            return

        # Get clip data for undo
        affected_clips = tuple(
            c for c in track.clips
            if c.timeline_start >= clip.timeline_end
        )

        # C08：A/V editing-unit 原子波纹（与 left 对称）：链接伙伴同缩/同
        # 延到相同新终点，任一边无法满足时整组拒绝。
        commands = [
            RippleTrimCommand.create_right_trim(
                track_id=track_id,
                clip_id=clip_id,
                new_end=new_end,
                original_clip=clip,
                affected_clips=affected_clips,
            )
        ]
        linked = (
            self._timeline.get_reciprocal_linked_clip(clip)
            if self._effective_linked_selection()
            else None
        )
        if linked is not None and not self._is_track_locked(linked.track_id):
            if new_end > linked.timeline_start:
                linked_track = self._timeline.get_track_by_id(linked.track_id)
                linked_affected = tuple(
                    c for c in (linked_track.clips if linked_track else ())
                    if c.timeline_start >= linked.timeline_end
                )
                commands.append(
                    RippleTrimCommand.create_right_trim(
                        track_id=linked.track_id,
                        clip_id=linked.clip_id,
                        new_end=new_end,
                        original_clip=linked,
                        affected_clips=linked_affected,
                        apply_sync_lock=False,
                    )
                )
            else:
                logger.warning(
                    "Linked ripple trim right rejected: new_end %s not beyond "
                    "linked clip %s start %s", new_end, linked.clip_id,
                    linked.timeline_start,
                )
                self.status_message_requested.emit(
                    self.tr("链接片段无法波纹到该位置，已取消整组操作")
                )
                return
        elif linked is not None and self._is_track_locked(linked.track_id):
            self.status_message_requested.emit(
                self.tr("链接轨道已锁定，无法整组波纹修剪")
            )
            return

        cmd = commands[0] if len(commands) == 1 else MacroCommand.create(
            "Linked ripple trim right", commands
        )
        self._execute_command(cmd)

        logger.debug(
            f"Ripple trim right completed: clip {clip_id} "
            f"end {old_end} -> {new_end}"
        )

    def _on_clip_delete(self, clip_id: str, track_id: str) -> None:
        """Handle clip delete request."""
        if self._is_track_locked(track_id):
            return
        self._delete_clip(clip_id, track_id)

    def _on_clip_split_requested(self, clip_id: str, track_id: str, split_frame: int) -> None:
        """Split a clip from its context menu at the requested playhead frame."""
        if self._timeline is None or self._is_track_locked(track_id):
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        frame = int(split_frame)
        if not (clip.timeline_start < frame < clip.timeline_end):
            logger.info(
                "Split ignored: playhead %s is outside clip %s [%s, %s)",
                frame, clip_id, clip.timeline_start, clip.timeline_end,
            )
            return
        self._execute_command(SplitClipCommand.create(track_id, clip_id, frame))

    def _on_clip_duplicate_requested(self, clip_id: str, track_id: str) -> None:
        """Duplicate the clip under the context-menu pointer."""
        if self._timeline is None or self._is_track_locked(track_id):
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        self.set_selection(self._selection.with_clip_selected(
            clip_id, track_id, self._timeline.timeline_id, replace=True,
        ))
        self._duplicate_selected_clips()

    def handle_delete_request(self, ripple: bool = False) -> None:
        """Unified delete dispatcher (keyboard Delete / Edit menu).

        Priority order (P1-03):
        1. Selected transitions are deleted.
        2. With an In/Out range set and no clip selection, the range is
           Lifted (ripple=False, gap kept) or Extracted (ripple=True, gap
           closed).
        3. Otherwise the selected clips are deleted.

        Args:
            ripple: Close gaps (Extract / ripple delete) when True.
        """
        if self._selection.selected_transition_ids:
            self._delete_selected_transitions()
            return

        if not self._selection.selected_clip_ids and self._timeline is not None:
            start = self._timeline.range_start
            end = self._timeline.range_end
            # Timeline In/Out points are inclusive on both ends (the range
            # item renders through end + 1); the delete builder wants an
            # exclusive end.
            if start is not None and end is not None and end >= start:
                self._delete_range(start, end + 1, ripple=ripple)
                return

        self._delete_selected_clips(ripple=ripple)

    def _delete_range(self, start_frame: int, end_frame: int, ripple: bool) -> bool:
        """Lift/Extract the In/Out range across all unlocked A/V tracks (P1-03).

        Called with an exclusive-end frame interval. Linked A/V pairs stay
        aligned because every scoped track receives the same trims at the
        same frames and the same ripple shift.

        Returns:
            True when a command was executed.
        """
        if self._timeline is None:
            return False

        track_ids = [
            t.track_id
            for t in self._timeline.video_tracks + self._timeline.audio_tracks
            if not t.is_locked
        ]

        from src.features.edit.core.timeline_ops import build_range_delete_commands
        from src.features.edit.core.commands import MacroCommand

        commands = build_range_delete_commands(
            self._timeline, track_ids, start_frame, end_frame, ripple=ripple
        )
        if not commands:
            return False

        label = "Extract range" if ripple else "Lift range"
        self._execute_command(MacroCommand.create(label, commands))
        return True

    def _delete_selected_clips(self, ripple: bool = False) -> None:
        """
        Delete all selected media and subtitle clips.

        Args:
            ripple: If True, close gaps by moving subsequent clips forward.
                    If False, leave gaps after deletion.

        Behavior:
        - Single clip deleted: Select adjacent clip (next if exists, else previous)
        - Multiple clips deleted: Clear selection
        - After deletion, clear visual selection state
        """
        if self._timeline is None:
            return

        selected_clip_ids = list(self._selection.selected_clip_ids)
        if not selected_clip_ids:
            return

        # ``TimelineSelection`` intentionally uses one CLIP type for both
        # regular and subtitle clips.  Split the IDs before creating a
        # command: BatchDeleteCommand only knows about media clips, while
        # DeleteSubtitleClipCommand owns subtitle cues.
        locked_ids = self._get_locked_track_ids()
        regular_clip_ids: list[str] = []
        subtitle_clip_ids: list[str] = []
        had_selected_subtitle = False
        for clip_id in selected_clip_ids:
            subtitle = self._timeline.get_subtitle_clip_by_id(clip_id)
            if subtitle is not None:
                had_selected_subtitle = True
                owner = self._timeline.find_subtitle_clip_owner_track(clip_id)
                if (
                    owner is not None
                    and owner.track_id not in locked_ids
                    and not subtitle.is_edit_locked()
                ):
                    subtitle_clip_ids.append(clip_id)
                continue

            item = self._items_map.get(clip_id)
            if item is not None and getattr(item, "_track_id", None) not in locked_ids:
                # Only regular items should reach BatchDeleteCommand.  An
                # unknown stale selection is ignored instead of making Delete
                # raise and leaving the UI looking unchanged.
                if self._timeline.get_clip_by_id(clip_id) is not None:
                    regular_clip_ids.append(clip_id)

        if not regular_clip_ids and not subtitle_clip_ids:
            return

        # Determine adjacent clip for post-delete selection
        adjacent_clip_id: Optional[str] = None
        if len(regular_clip_ids) == 1 and not had_selected_subtitle:
            # Single clip: find adjacent clip for selection
            clip_id = regular_clip_ids[0]
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip is not None:
                track = self._timeline.get_track_by_id(clip.track_id)
                if track is not None:
                    # Find clips on same track
                    track_clips = sorted(track.clips, key=lambda c: c.timeline_start)
                    clip_index = next(
                        (i for i, c in enumerate(track_clips) if c.clip_id == clip_id),
                        None
                    )
                    if clip_index is not None:
                        # Try to select next clip first, then previous
                        if clip_index + 1 < len(track_clips):
                            adjacent_clip_id = track_clips[clip_index + 1].clip_id
                        elif clip_index - 1 >= 0:
                            adjacent_clip_id = track_clips[clip_index - 1].clip_id

        from src.features.edit.core.commands import DeleteSubtitleClipCommand, MacroCommand

        commands = []
        if regular_clip_ids:
            commands.append(BatchDeleteCommand.create(
                self._timeline,
                regular_clip_ids,
                ripple=ripple,
            ))
        commands.extend(
            DeleteSubtitleClipCommand.create(clip_id)
            for clip_id in subtitle_clip_ids
        )
        cmd = commands[0] if len(commands) == 1 else MacroCommand.create(
            "Delete selected clips",
            commands,
        )
        self._execute_command(cmd)

        # Update selection after deletion
        if (
            not had_selected_subtitle
            and adjacent_clip_id
            and adjacent_clip_id not in regular_clip_ids
        ):
            # Select adjacent clip
            adjacent_clip = self._timeline.get_clip_by_id(adjacent_clip_id)
            if adjacent_clip is not None:
                new_selection = self._selection.with_clip_selected(
                    adjacent_clip_id,
                    adjacent_clip.track_id,
                    self._timeline.timeline_id,
                    replace=True
                )
                self.set_selection(new_selection)
        else:
            # Multiple clips deleted or no adjacent: clear selection
            self.set_selection(TimelineSelection())

    def _clear_selection(self) -> None:
        """Clear all selection (Escape key)."""
        self.set_selection(TimelineSelection())

    # INT-02：片段级交互的 drag_mode → InteractionState 映射。状态本身
    # 仅用于让 Esc/换工具/失焦的取消路径非 IDLE；恢复细节由
    # ClipItem.cancel_active_drag 与 ClipDragSession 快照负责。
    _ITEM_DRAG_MODE_TO_STATE = {
        "move": InteractionState.DRAGGING_CLIP,
        "trim_left": InteractionState.TRIMMING_LEFT,
        "trim_right": InteractionState.TRIMMING_RIGHT,
        "fade_in": InteractionState.DRAGGING_FADE_IN,
        "fade_out": InteractionState.DRAGGING_FADE_OUT,
        "slip": InteractionState.SLIPPING,
        "slide": InteractionState.SLIDING,
        "ripple_trim_left": InteractionState.RIPPLE_TRIMMING_LEFT,
        "ripple_trim_right": InteractionState.RIPPLE_TRIMMING_RIGHT,
        "roll_trim_left": InteractionState.ROLLING,
        "roll_trim_right": InteractionState.ROLLING,
        "rate_stretch_left": InteractionState.TRIMMING_LEFT,
        "rate_stretch_right": InteractionState.TRIMMING_RIGHT,
        "motion_graphic_duration": InteractionState.TRIMMING_RIGHT,
        "retime_stretch": InteractionState.TRIMMING_RIGHT,
    }

    def _on_item_interaction_started(
        self, clip_id: str, track_id: str, drag_mode: str
    ) -> None:
        """ClipItem press 已确定 drag_mode 时登记 InteractionSession。"""
        self._independent_selection = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier)
        edges: dict[str, str] = {}
        if drag_mode in {"trim_left", "trim_right", "ripple_trim_left", "ripple_trim_right",
                         "roll_trim_left", "roll_trim_right", "rate_stretch_left", "rate_stretch_right"}:
            edge = "left" if drag_mode.endswith("left") else "right"
            edges[clip_id] = edge
            clip = self._timeline.get_clip_by_id(clip_id) if self._timeline is not None else None
            partner = self._get_reciprocal_linked_clip(clip)
            if partner is not None and self._effective_linked_selection():
                edges[partner.clip_id] = edge
        self._set_selected_edit_edges(edges)
        if drag_mode in {"slip", "slide"}:
            self._source_edit_linked_at_press = self._effective_linked_selection()
            self._source_edit_timeline_at_press = self._timeline
        state = self._ITEM_DRAG_MODE_TO_STATE.get(drag_mode)
        if state is None:
            return
        item = self._items_map.get(clip_id)
        self._interaction.begin(
            state,
            item.mapToScene(0.0, 0.0) if item is not None else QPointF(0.0, 0.0),
            primary_clip_id=clip_id,
            primary_track_id=track_id,
            original_start=getattr(item, "_original_start", 0) if item is not None else 0,
            original_duration=getattr(item, "_original_duration", 0) if item is not None else 0,
            original_source_start=getattr(item, "_original_source_start", 0) if item is not None else 0,
            original_source_duration=getattr(item, "_original_source_duration", 0) if item is not None else 0,
        )

    def _on_item_interaction_finished(self, clip_id: str) -> None:
        """ClipItem 正常 release：结束本片段启动的会话。"""
        if self._interaction.primary_clip_id == clip_id:
            self._interaction.finish()

    def _reproject_preview_affected_items(self) -> None:
        """RI-02/R14：把预览期间被直接改写的 Item 从不可变模型重投影。

        恢复几何、源区间与修剪预览状态，并清空登记——下一次点击伙伴/
        邻片时 press 保存的 original 即为正确值，不会把残留 preview 当
        original 提交。
        """
        if not self._preview_affected_item_ids:
            return
        if self._timeline is None:
            self._preview_affected_item_ids.clear()
            return
        for clip_id in tuple(self._preview_affected_item_ids):
            clip = self._timeline.get_clip_by_id(clip_id)
            item = self._items_map.get(clip_id)
            if clip is None or not isinstance(item, ClipItem):
                continue
            item._start_frame = clip.timeline_start
            item._duration_frames = clip.timeline_duration
            item.update_source_range(clip.source_start, clip.source_duration)
            item.end_trim_preview()
            item._update_geometry()
            item.setPos(
                self._layout_engine.frame_to_x(clip.timeline_start),
                item.y(),
            )
            item.update()
        self._preview_affected_item_ids.clear()

    def _cancel_current_interaction(self) -> None:
        """Cancel current interaction and restore state (P2-5: Esc cancel support).

        Restores all preview-modified clip positions back to their originals,
        including linked clips and multi-selected clips tracked in ClipDragSession.
        INT-02：现在同样覆盖 ClipItem 自身驱动的拖拽/修剪——先取消 item
        的 grab/pending 并恢复其预览，release 因 drag_mode 已清空不再提交。
        """
        # P2-2: Restore all clips from ClipDragSession if present
        if self._current_drag_session is not None:
            # Clean up Alt-drag copy ghosts if present.
            self._clear_alt_copy_ghosts()
            for clip_id, orig_start in self._current_drag_session.original_positions.items():
                item = self._items_map.get(clip_id)
                if item and isinstance(item, ClipItem):
                    item._start_frame = orig_start
                    item._drag_target_track_id = ""  # P5: Reset track change tracking
                    x = self._layout_engine.frame_to_x(orig_start)
                    # Restore original track Y position
                    orig_tid = self._current_drag_session.original_track_ids.get(clip_id, "")
                    if orig_tid:
                        y = self._layout_engine.track_top(orig_tid)
                        item.set_track_height(self._layout_engine.track_height(orig_tid))
                    else:
                        y = item.y()
                    item.setPos(x, y)
                    item._update_geometry()
                    item.set_drag_cache_mode(False)
                    item.update()
            self._current_drag_session = None
            self._set_drag_render_mode(False)
            self.viewport().update()

        # Subtitle group drags preview geometry directly on SubtitleClipItems
        # because they use a dedicated interaction path.  Escape must restore
        # those items from the immutable timeline too.
        if self._subtitle_drag_session is not None:
            self._subtitle_drag_session = None
            self._layout_track_items()

        # RI-01/R13：Roll seam 拖拽由 RollHandle 持有，不在 ClipDragSession
        # 内——单独取消：恢复双片模型投影、复位 handle 状态与抓取，迟
        # 到的 release 因会话已失效不再提交。
        if self._roll_preview_state is not None:
            handle = self._roll_preview_state.get("handle")
            if handle is not None:
                handle.cancel_drag()
            self._restore_roll_preview()
            self._roll_preview_state = None
            self._hide_trim_monitor()

        # RI-02/R14：修剪/Slide 预览直接改写的伙伴/邻片/跟随片段统一从
        # 不可变模型重投影（此前只恢复 primary，linked trim/Slide 取消
        # 后伙伴留错误源区间与几何）。
        self._reproject_preview_affected_items()

        # Fallback: restore primary clip from interaction session
        if self._interaction.primary_clip_id:
            item = self._items_map.get(self._interaction.primary_clip_id)
            if (
                item
                and isinstance(item, ClipItem)
                # RI-02 护栏：只有真实按下的片段才有 press 保存的
                # _original_*；对未经 press 的会话宿主调用
                # cancel_active_drag 会把几何重置成 0 默认值。
                and (
                    item._is_dragging
                    or item._drag_pending
                    or item._drag_mode is not None
                )
            ):
                # INT-02：交由 item 的统一取消方法恢复几何/源区间/fade、
                # 清 grab 与 pending、结束修剪预览；release 因此不再提交。
                item.cancel_active_drag()
                x = self._layout_engine.frame_to_x(item._start_frame)
                item.setPos(x, item.y())
                item._update_geometry()
                item.update()
        # Hide snap indicator
        if self._snap_indicator:
            self._snap_indicator.hide_indicator()
        # INT-03：Esc 取消平移时恢复光标
        if self._interaction.state == InteractionState.PANNING and self._pan_prev_cursor is not None:
            self.setCursor(self._pan_prev_cursor)
            self._pan_prev_cursor = None
        self._interaction.cancel()
        self.viewport().update()

    def _select_all_clips(self) -> None:
        """Select all clips in timeline (Ctrl+A)."""
        if self._timeline is None:
            return

        # Collect all clips from all tracks
        items = []
        for track in self._timeline.all_tracks:
            for clip in track.clips:
                from src.features.edit.core.selection import SelectionItem, SelectionType
                items.append(SelectionItem(
                    item_type=SelectionType.CLIP,
                    item_id=clip.clip_id,
                    track_id=track.track_id,
                    timeline_id=self._timeline.timeline_id,
                ))
            for subtitle_clip in track.subtitle_clips:
                from src.features.edit.core.selection import SelectionItem, SelectionType
                items.append(SelectionItem(
                    item_type=SelectionType.CLIP,
                    item_id=subtitle_clip.clip_id,
                    track_id=track.track_id,
                    timeline_id=self._timeline.timeline_id,
                ))

        if items:
            new_selection = TimelineSelection(
                items=tuple(items),
                is_range=False,
                primary_item=items[0],
            )
            self.set_selection(new_selection)

    def _delete_clip(self, clip_id: str, track_id: str) -> None:
        """Delete a single clip."""
        if self._timeline is None:
            return

        cmd = DeleteClipCommand.create(track_id, clip_id)
        self._execute_command(cmd)

    # ==================== P2: Copy/Paste/Duplicate ====================

    def _copy_selected_clips(self) -> None:
        """P2: Copy selected clips to internal clipboard."""
        if self._timeline is None:
            return
        selected_ids = self._selection.selected_clip_ids
        if not selected_ids:
            return

        clips_data = []
        anchor_frame = None
        # R9/F2.1：跨序列粘贴需要轨道结构位置（类型+同类型序号+角色）——
        # 不同序列的 track_id 互不相同，只存 id 会导致整批静默跳过。
        track_groups = {
            "video": self._timeline.video_tracks,
            "audio": self._timeline.audio_tracks,
            "subtitle": self._timeline.subtitle_tracks,
        }
        for clip_id in selected_ids:
            clip = self._timeline.get_clip_by_id(clip_id)
            kind = "clip"
            if clip is None:
                clip = self._timeline.get_subtitle_clip_by_id(clip_id)
                kind = "subtitle"
            if clip is None:
                continue
            if anchor_frame is None or clip.timeline_start < anchor_frame:
                anchor_frame = clip.timeline_start
            source_track = next(
                (t for t in self._timeline.all_tracks
                 if t.track_id == clip.track_id), None)
            track_type = getattr(source_track, "track_type", None)
            track_index = None
            if source_track is not None and track_type in track_groups:
                track_index = next(
                    (i for i, t in enumerate(track_groups[track_type])
                     if t.track_id == source_track.track_id), None)
            clips_data.append({
                "clip": clip.to_dict(),
                "track_id": clip.track_id,
                "kind": kind,
                "relative_start": 0,  # will be computed after anchor
                "track_type": track_type,
                "track_index": track_index,
                "track_role": getattr(source_track, "track_role", None),
            })

        if not clips_data:
            return

        # Compute relative offsets from anchor
        for entry in clips_data:
            clip_dict = entry["clip"]
            entry["relative_start"] = clip_dict["timeline_start"] - anchor_frame

        # Store in clipboard (typed payload)
        self._clipboard = TimelineClipboardPayload(
            fps=float(self._timeline.fps) if self._timeline.fps else EMPTY_TIMELINE_FPS,
            anchor_frame=anchor_frame,
            clips=tuple(clips_data),
        )
        logger.info(f"Copied {len(clips_data)} clips to clipboard")

    def _paste_clips_at_playhead(self) -> None:
        """P2: Paste clips from clipboard at playhead position."""
        self._paste_clips_at_frame(self._playhead_frame)

    def _paste_clips_at_frame(self, paste_start: int) -> None:
        """P2: Paste clips from clipboard at a specific frame position."""
        if self._timeline is None:
            return
        if not hasattr(self, '_clipboard') or self._clipboard is None:
            return
        if not isinstance(self._clipboard, TimelineClipboardPayload):
            return

        from src.features.edit.core.commands import (
            AddClipCommand,
            CopySubtitleClipCommand,
            MacroCommand,
        )
        from src.features.edit.core.captions.commands import (
            AddCaptionGraphicBindingCommand,
        )
        from src.features.edit.core.captions.graphics import (
            clone_caption_graphic_binding,
        )
        import copy

        clips_data = self._clipboard.clips

        # Build old_id -> new_id mapping for linked clip remapping
        old_to_new_id = {}
        for entry in clips_data:
            old_id = entry["clip"].get("clip_id")
            if old_id:
                old_to_new_id[old_id] = str(uuid.uuid4())

        # R9/F2.1：跨序列粘贴轨道重映射——同 id 优先；否则按复制时记录的
        # （track_type, 同类型序号, track_role）在目标序列解析对应轨。
        # 解析不到（目标缺该类型轨）才跳过该条，不再整批静默丢失。
        def _resolve_target_track(entry: dict) -> EditTrack | None:
            source_track_id = entry.get("track_id")
            direct = self._timeline.get_track_by_id(source_track_id) \
                if source_track_id else None
            if direct is not None:
                return direct
            track_type = entry.get("track_type")
            groups = {
                "video": self._timeline.video_tracks,
                "audio": self._timeline.audio_tracks,
                "subtitle": self._timeline.subtitle_tracks,
            }
            group = groups.get(track_type, ())
            if not group:
                return None
            role = entry.get("track_role")
            if role:
                for t in group:
                    if getattr(t, "track_role", None) == role:
                        return t
                return None
            idx = entry.get("track_index")
            if isinstance(idx, int) and 0 <= idx < len(group):
                return group[idx]
            return None

        resolved_tracks: dict[int, EditTrack | None] = {}
        for i, entry in enumerate(clips_data):
            resolved_tracks[i] = _resolve_target_track(entry)

        # Detect overlap: if any pasted clip would overlap existing clips,
        # shift the entire paste batch right until a free slot is found.
        def _clips_overlap_on_track(track: EditTrack, start: int, duration: int,
                                    exclude_ids: set[str] | None = None) -> bool:
            end = start + duration
            for c in track.clips:
                if exclude_ids and c.clip_id in exclude_ids:
                    continue
                c_end = c.timeline_start + c.timeline_duration
                if start < c_end and end > c.timeline_start:
                    return True
            return False

        def _subtitles_overlap_on_track(track: EditTrack, start: int, duration: int) -> bool:
            end = start + duration
            return any(
                start < cue.timeline_end and end > cue.timeline_start
                for cue in track.subtitle_clips
            )

        # Check if the initial paste position has overlaps
        has_overlap = False
        for i, entry in enumerate(clips_data):
            target_track = resolved_tracks.get(i)
            if target_track is None:
                continue
            rel_start = entry["relative_start"]
            clip_dur = entry["clip"].get("timeline_duration", 0)
            candidate_start = paste_start + rel_start
            overlaps = (
                _subtitles_overlap_on_track(target_track, candidate_start, clip_dur)
                if entry.get("kind") == "subtitle"
                else _clips_overlap_on_track(target_track, candidate_start, clip_dur)
            )
            if overlaps:
                has_overlap = True
                break

        # If overlap detected, shift right to the first free slot
        effective_paste = paste_start
        if has_overlap:
            max_shift = 0
            for i, entry in enumerate(clips_data):
                target_track = resolved_tracks.get(i)
                if target_track is None:
                    continue
                rel_start = entry["relative_start"]
                clip_dur = entry["clip"].get("timeline_duration", 0)
                candidate = paste_start + rel_start
                while (
                    _subtitles_overlap_on_track(target_track, candidate, clip_dur)
                    if entry.get("kind") == "subtitle"
                    else _clips_overlap_on_track(target_track, candidate, clip_dur)
                ):
                    candidate += 1
                shift = candidate - (paste_start + rel_start)
                if shift > max_shift:
                    max_shift = shift
            effective_paste = paste_start + max_shift

        commands = []
        for i, entry in enumerate(clips_data):
            clip_dict = copy.deepcopy(entry["clip"])
            relative_start = entry["relative_start"]
            is_subtitle = entry.get("kind") == "subtitle"

            # Determine target track（R9：跨序列按结构位置重映射）
            target_track = resolved_tracks.get(i)
            if target_track is None:
                continue
            if self._is_track_locked(target_track.track_id):
                continue

            # Calculate new timeline start
            new_start = effective_paste + relative_start
            if is_subtitle:
                if target_track.track_type != "subtitle":
                    continue
                cmd = CopySubtitleClipCommand.create(
                    source_clip_id=clip_dict["clip_id"],
                    target_track_id=target_track.track_id,
                    timeline_start=new_start,
                )
            else:
                if target_track.track_type == "subtitle":
                    continue
                # Use pre-generated new clip ID
                old_clip_id = clip_dict.get("clip_id")
                new_clip_id = old_to_new_id.get(old_clip_id, str(uuid.uuid4()))
                clip_dict["clip_id"] = new_clip_id
                clip_dict["track_id"] = target_track.track_id
                clip_dict["timeline_start"] = new_start

                # Remap linked_clip_id to the new pair if both were copied
                old_linked = clip_dict.get("linked_clip_id")
                if old_linked:
                    if old_linked in old_to_new_id:
                        clip_dict["linked_clip_id"] = old_to_new_id[old_linked]
                    else:
                        clip_dict["linked_clip_id"] = None

                clip = ClipInstance.from_dict(clip_dict)
                cmd = AddClipCommand.create(target_track.track_id, clip)
            commands.append(cmd)
            if not is_subtitle:
                source_binding = (
                    self._timeline.get_caption_graphic_binding_by_clip(
                        old_clip_id)
                    if hasattr(self._timeline, "get_caption_graphic_binding_by_clip")
                    else None
                )
                if source_binding is not None:
                    if target_track.track_role != "graphics":
                        commands.pop()
                        continue
                    source_cue = self._timeline.get_subtitle_clip_by_id(
                        source_binding.caption_cue_id)
                    commands.append(AddCaptionGraphicBindingCommand.create(
                        clone_caption_graphic_binding(
                            source_binding, clip.clip_id, source_cue)))

        if not commands:
            return

        if len(commands) == 1:
            self._execute_command(commands[0])
        else:
            macro = MacroCommand.create(f"Paste {len(commands)} clips", commands)
            self._execute_command(macro)
        logger.info(f"Pasted {len(commands)} clips at frame {effective_paste}")

    def _cut_selected_clips(self) -> None:
        """P2: Cut (copy + delete) selected clips."""
        self._copy_selected_clips()
        self._delete_selected_clips(ripple=False)

    def _duplicate_selected_clips(self) -> None:
        """P2: Duplicate selected clips (paste after selection end)."""
        if self._timeline is None:
            return

        selected_ids = self._selection.selected_clip_ids
        if not selected_ids:
            return

        # Find the end of the rightmost selected clip
        max_end = 0
        for clip_id in selected_ids:
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip is None:
                clip = self._timeline.get_subtitle_clip_by_id(clip_id)
            if clip is not None:
                end = clip.timeline_start + clip.timeline_duration
                if end > max_end:
                    max_end = end

        # Copy, then paste at max_end
        self._copy_selected_clips()
        if hasattr(self, '_clipboard') and self._clipboard is not None:
            # Paste at the end of selection without modifying playhead
            self._paste_clips_at_frame(max_end)

    # ==================== P4: Mark In/Out Range ====================

    def _mark_in_at_playhead(self) -> None:
        """P4: Set In point at current playhead position."""
        self._mark_in_at_frame(self._playhead_frame)

    def _mark_in_at_frame(self, frame: int) -> None:
        """Set the timeline In point at ``frame`` through the command stack."""
        if self._timeline is None:
            return
        from src.features.edit.core.commands import SetTimelineRangeCommand
        cmd = SetTimelineRangeCommand.create(
            new_start=frame,
            new_end=self._timeline.range_end,
            old_start=self._timeline.range_start,
            old_end=self._timeline.range_end,
        )
        self._execute_command(cmd)
        logger.info(f"Mark In at frame {frame}")

    def _mark_out_at_playhead(self) -> None:
        """P4: Set Out point at current playhead position."""
        self._mark_out_at_frame(self._playhead_frame)

    def _mark_out_at_frame(self, frame: int) -> None:
        """Set the timeline Out point at ``frame`` through the command stack."""
        if self._timeline is None:
            return
        from src.features.edit.core.commands import SetTimelineRangeCommand
        cmd = SetTimelineRangeCommand.create(
            new_start=self._timeline.range_start,
            new_end=frame,
            old_start=self._timeline.range_start,
            old_end=self._timeline.range_end,
        )
        self._execute_command(cmd)
        logger.info(f"Mark Out at frame {frame}")

    def _clear_timeline_range(self) -> None:
        """Clear the active In/Out selection through the command stack."""
        if self._timeline is None:
            return
        from src.features.edit.core.commands import SetTimelineRangeCommand
        cmd = SetTimelineRangeCommand.create(
            new_start=None,
            new_end=None,
            old_start=self._timeline.range_start,
            old_end=self._timeline.range_end,
        )
        self._execute_command(cmd)
        logger.info("Cleared timeline In/Out range")

    def _on_range_change_finished(self, new_start: int, new_end: int) -> None:
        """Commit a ruler-handle range edit as one undoable timeline command."""
        if self._timeline is None:
            return
        from src.features.edit.core.commands import SetTimelineRangeCommand
        cmd = SetTimelineRangeCommand.create(
            new_start=new_start,
            new_end=new_end,
            old_start=self._timeline.range_start,
            old_end=self._timeline.range_end,
        )
        self._execute_command(cmd)

    def _on_range_handle_dragged(self, edge: str, frame: int) -> None:
        """Update range handles and mask live; commit only on mouse release."""
        if self._timeline is None or self._range_item is None:
            return
        start = frame if edge == "in" else self._range_item._range_start
        end = frame if edge == "out" else self._range_item._range_end
        if start is None or end is None:
            return
        self._range_item.set_range(start, end)
        if self._range_mask_item is not None:
            self._configure_layout_engine()
            track_area = self._layout_engine.area_layout().total_track_area
            self._range_mask_item.set_range(
                start,
                end,
                self._effective_timeline_duration(self._timeline),
                track_area.top(),
                track_area.height(),
            )

    # ==================== Transition Handlers ====================

    def _on_transition_clicked(self, transition_id: str, track_id: str) -> None:
        """Handle transition click from TransitionItem."""
        if self._updating:
            return

        if self._timeline is None:
            return

        # Get current keyboard modifiers
        modifiers = QApplication.keyboardModifiers()

        if modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            # Shift+click or Ctrl+click: Toggle selection
            new_selection = self._selection.toggle_transition(
                transition_id,
                track_id,
                self._timeline.timeline_id
            )
        else:
            # Normal click: Replace selection with transition
            new_selection = self._selection.with_transition_selected(
                transition_id,
                track_id,
                self._timeline.timeline_id,
                replace=True
            )
        self.set_selection(new_selection)

    def _on_transition_delete(self, transition_id: str, track_id: str) -> None:
        """Handle transition delete request."""
        if self._is_track_locked(track_id):
            return
        self._delete_transition(transition_id, track_id)

    def _delete_transition(self, transition_id: str, track_id: str) -> None:
        """Delete a single transition."""
        if self._timeline is None:
            return

        cmd = RemoveTransitionCommand.create(
            self._timeline,
            transition_id,
            track_id
        )
        self._execute_command(cmd)

        # Clear selection after deletion
        self.set_selection(TimelineSelection())

    def _delete_selected_transitions(self) -> None:
        """Delete all selected transitions."""
        if self._timeline is None:
            return

        selected_transition_ids = list(self._selection.selected_transition_ids)
        if not selected_transition_ids:
            return

        # Locked tracks are read-only for both the Delete key and context menu.
        locked_ids = self._get_locked_track_ids()
        selected_transition_ids = [
            trans_id
            for trans_id in selected_transition_ids
            if any(
                trans.transition_id == trans_id and track.track_id not in locked_ids
                for track in self._timeline.all_tracks
                for trans in track.transitions
            )
        ]
        if not selected_transition_ids:
            return

        # BUG-02 fix: Collect all remove commands and execute via _execute_command
        # to ensure proper signal emission and incremental update
        sub_commands = []
        for trans_id in selected_transition_ids:
            # Find track_id for this transition
            for track in self._timeline.all_tracks:
                for trans in track.transitions:
                    if trans.transition_id == trans_id:
                        cmd = RemoveTransitionCommand.create(
                            self._timeline,
                            trans_id,
                            track.track_id
                        )
                        sub_commands.append(cmd)
                        break

        if not sub_commands:
            return

        # Execute commands atomically via _execute_command
        if len(sub_commands) == 1:
            # Single transition: execute directly
            self._execute_command(sub_commands[0])
        else:
            # Multiple transitions: use MacroCommand for atomic batch operation
            macro = MacroCommand(
                command_id=f"delete_transitions_{selected_transition_ids[0][:8]}",
                label="Delete transitions",
                sub_commands=tuple(sub_commands),
            )
            self._execute_command(macro)

        # Clear selection
        self.set_selection(TimelineSelection())

    def add_transition_at_cut_point(
        self,
        track_id: str,
        left_clip_id: str,
        right_clip_id: str,
        duration: int = 12,
        transition_type: str = "cross_dissolve",
        transition_params: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Add a transition at a cut point between two clips.

        Args:
            track_id: Track ID containing the clips
            left_clip_id: Left (outgoing) clip ID
            right_clip_id: Right (incoming) clip ID
            duration: Transition duration in frames (default: 12 frames)
            transition_type: Type of transition (default: cross_dissolve)
        """
        if self._timeline is None:
            return

        try:
            track = self._timeline.get_track_by_id(track_id)
            if track is None or track.is_locked:
                return

            # A cut can own only one transition. Re-adding a transition at
            # the same clip pair replaces its visual/effect parameters while
            # preserving the existing transition ID, position, alignment and
            # duration. The duration argument is intentionally ignored here
            # when replacing, so a second drop cannot resize the transition.
            existing = next(
                (
                    transition
                    for transition in track.transitions
                    if transition.left_clip_id == left_clip_id
                    and transition.right_clip_id == right_clip_id
                ),
                None,
            )
            if existing is not None:
                command = UpdateTransitionCommand(
                    command_id=str(uuid.uuid4()),
                    label=f"Replace transition with {transition_type}",
                    transition_id=existing.transition_id,
                    track_id=track_id,
                    new_transition_type=transition_type,
                    new_params=dict(transition_params or {}),
                    _old_transition=existing,
                )
                self._execute_command(command)
                return

            cmd = AddTransitionCommand.create_from_clip_gap(
                timeline=self._timeline,
                track_id=track_id,
                left_clip_id=left_clip_id,
                right_clip_id=right_clip_id,
                duration=duration,
                transition_type=transition_type,
                params=transition_params,
            )
            self._execute_command(cmd)
            return None
        except ValueError as e:
            logger.warning(f"Cannot add transition: {e}")
            return str(e)  # 审计修复：失败原因上报（不再静默当成功）

    def add_transition_at_clip_edge(
        self,
        track_id: str,
        clip_id: str,
        edge: str,
        duration: int = 12,
        transition_type: str = "cross_dissolve",
        transition_params: Optional[dict[str, Any]] = None,
    ) -> None:
        """Add or replace a one-sided transition at a clip edge."""
        if self._timeline is None:
            return
        try:
            track = self._timeline.get_track_by_id(track_id)
            if track is None or track.is_locked:
                return
            existing = next(
                (
                    transition for transition in track.transitions
                    if (
                        (edge == "left" and transition.left_clip_id == "" and transition.right_clip_id == clip_id)
                        or (edge == "right" and transition.left_clip_id == clip_id and transition.right_clip_id == "")
                    )
                ),
                None,
            )
            if existing is not None:
                self._execute_command(UpdateTransitionCommand(
                    command_id=str(uuid.uuid4()),
                    label=f"Replace edge transition with {transition_type}",
                    transition_id=existing.transition_id,
                    track_id=track_id,
                    new_transition_type=transition_type,
                    new_params=dict(transition_params or {}),
                    _old_transition=existing,
                ))
                return

            self._execute_command(AddTransitionCommand.create_from_clip_edge(
                timeline=self._timeline,
                track_id=track_id,
                clip_id=clip_id,
                edge=edge,
                duration=duration,
                transition_type=transition_type,
                params=transition_params,
            ))
        except ValueError as e:
            logger.warning(f"Cannot add edge transition: {e}")
            return str(e)  # 审计修复：失败原因上报

    def apply_transition_from_panel(
        self,
        transition_payload: str,
        clip_id: str,
        playhead_frame: int = 0,
        duration: int = DEFAULT_TRANSITION_DURATION,
    ) -> str:
        """Apply a transition triggered from the effects panel (double-click).

        P1-06: mirrors the drop-point decision in ``_handle_transition_drop``
        so double-click and drag-drop execute the exact same commands
        (``add_transition_at_cut_point`` / ``add_transition_at_clip_edge``).
        Targeting order with a selected clip: its out-cut first, its in-cut
        second, a one-sided edge transition for isolated clips. Without a
        selection the nearest cut to the playhead on an unlocked track of the
        matching kind wins.

        Args:
            transition_payload: Transition type, optionally ``wipe:<direction>``.
            clip_id: Selected clip ID, or empty string for playhead fallback.
            playhead_frame: Current playhead frame for the fallback path.
            duration: Transition duration in frames.

        Returns:
            Empty string on success, otherwise a human-readable reason why
            no transition could be applied (no silent failure).
        """
        from src.features.edit.core.timeline_ops import find_adjacent_clips_at_frame

        if self._timeline is None:
            return I18N.tr(
                "edit.transition.no_timeline", "No timeline is open"
            )

        # Split the wipe preset payload exactly like the drop handler does.
        params: dict[str, Any] = {}
        transition_type = transition_payload
        if transition_payload.startswith("wipe:"):
            params["direction"] = transition_payload.split(":", 1)[1]
            transition_type = "wipe"
        wants_audio_track = is_audio_transition(transition_type)
        timeline = self._timeline

        clip = timeline.get_clip_by_id(clip_id) if clip_id else None
        if clip is None and clip_id:
            return I18N.tr(
                "edit.transition.clip_not_found", "Selected clip no longer exists"
            )

        if clip is not None:
            track = timeline.get_track_by_id(clip.track_id)
            if track is None:
                return I18N.tr(
                    "edit.transition.clip_not_found", "Selected clip no longer exists"
                )
            if track.is_locked:
                return I18N.tr(
                    "edit.transition.track_locked",
                    f"Track '{track.name}' is locked",
                )
            if wants_audio_track != (track.track_type == "audio"):
                kind = I18N.tr("edit.transition.audio", "an audio")
                if wants_audio_track:
                    return I18N.tr(
                        "edit.transition.track_kind_mismatch",
                        f"Audio transitions need {kind} track — selected clip is on "
                        f"'{track.name}'",
                    )
                return I18N.tr(
                    "edit.transition.track_kind_mismatch",
                    f"Video transitions need a video track — selected clip is on "
                    f"'{track.name}'",
                )

            # Out-cut: selected clip as the left side of the transition.
            left, right = find_adjacent_clips_at_frame(
                timeline, track.track_id, clip.timeline_end, threshold=1
            )
            if left is not None and right is not None and left.clip_id == clip.clip_id:
                return self.add_transition_at_cut_point(
                    track.track_id, left.clip_id, right.clip_id,
                    duration, transition_type, params,
                ) or ""

            # In-cut: selected clip as the right side.
            left, right = find_adjacent_clips_at_frame(
                timeline, track.track_id, clip.timeline_start, threshold=1
            )
            if left is not None and right is not None and right.clip_id == clip.clip_id:
                return self.add_transition_at_cut_point(
                    track.track_id, left.clip_id, right.clip_id,
                    duration, transition_type, params,
                ) or ""

            # Isolated clip: one-sided fade at its tail edge.
            return self.add_transition_at_clip_edge(
                track.track_id, clip.clip_id, "right",
                duration, transition_type, params,
            ) or ""

        # No selection: nearest cut to the playhead on an unlocked track of
        # the matching kind.
        tracks = timeline.audio_tracks if wants_audio_track else timeline.video_tracks
        best: tuple[int, Any, int] | None = None
        for track in tracks:
            if track.is_locked:
                continue
            cut_points = self._get_cut_points_for_track(track.track_id)
            if not cut_points:
                continue
            nearest = min(cut_points, key=lambda c: abs(playhead_frame - c))
            distance = abs(playhead_frame - nearest)
            if best is None or distance < best[0]:
                best = (distance, track, nearest)

        if best is None:
            if wants_audio_track:
                return I18N.tr(
                    "edit.transition.no_audio_clips",
                    "No unlocked audio track with clips to transition between",
                )
            return I18N.tr(
                "edit.transition.no_video_clips",
                "No unlocked video track with clips to transition between",
            )

        distance, track, nearest = best
        if distance > DEFAULT_TRANSITION_DURATION:
            return I18N.tr(
                "edit.transition.no_cut_near_playhead",
                "No cut point near the playhead — select a clip or move the playhead",
            )

        left, right = find_adjacent_clips_at_frame(
            timeline, track.track_id, nearest, threshold=1
        )
        if left is not None and right is not None:
            return self.add_transition_at_cut_point(
                track.track_id, left.clip_id, right.clip_id,
                duration, transition_type, params,
            ) or ""

        # Sequence edge: one-sided transition on the clip starting or ending
        # exactly at the nearest edge point.
        for edge_clip in track.clips:
            if edge_clip.timeline_start == nearest:
                self.add_transition_at_clip_edge(
                    track.track_id, edge_clip.clip_id, "left",
                    duration, transition_type, params,
                )
                return ""
            if edge_clip.timeline_end == nearest:
                self.add_transition_at_clip_edge(
                    track.track_id, edge_clip.clip_id, "right",
                    duration, transition_type, params,
                )
                return ""

        return I18N.tr(
            "edit.transition.no_target_clip",
            "No clip found at the nearest edit point",
        )

    def _on_label_color_change_requested(self, clip_id: str, track_id: str, color: str | None) -> None:
        """P2: Handle label color change for a clip (or all selected clips)."""
        if self._timeline is None:
            return
        from src.features.edit.core.commands import SetClipLabelColorCommand, MacroCommand

        # If the right-clicked clip is part of a multi-selection, apply to all.
        selected_ids = self._selection.selected_clip_ids
        if clip_id in selected_ids and len(selected_ids) > 1:
            commands = []
            for sel_id in selected_ids:
                sel_clip = self._timeline.get_clip_by_id(sel_id)
                if sel_clip is None:
                    continue
                track = self._timeline.get_track_by_id(sel_clip.track_id)
                if track is not None and not track.is_locked:
                    commands.append(SetClipLabelColorCommand.create(
                        track_id=sel_clip.track_id,
                        clip_id=sel_id,
                        new_color=color,
                    ))
            if not commands:
                return
            if len(commands) == 1:
                self._execute_command(commands[0])
            else:
                self._execute_command(MacroCommand.create(
                    f"Set label color for {len(commands)} clips", commands))
            return

        # Single clip path
        cmd = SetClipLabelColorCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            new_color=color,
        )
        self._execute_command(cmd)

    def _on_waveform_display_mode_requested(
        self,
        clip_id: str,
        track_id: str,
        show_channels: bool,
    ) -> None:
        """Apply mono/stereo waveform display through the command stack."""
        if self._timeline is None:
            return

        from src.features.edit.core.commands import (
            MacroCommand,
            SetClipWaveformDisplayCommand,
        )

        selected_ids = self._selection.selected_clip_ids
        if clip_id in selected_ids and len(selected_ids) > 1:
            commands: list[Any] = []
            for selected_id in selected_ids:
                selected_clip = self._timeline.get_clip_by_id(selected_id)
                if selected_clip is None:
                    continue
                track = self._timeline.get_track_by_id(selected_clip.track_id)
                if track is None or track.is_locked or track.track_type != "audio":
                    continue
                commands.append(SetClipWaveformDisplayCommand.create(
                    track_id=selected_clip.track_id,
                    clip_id=selected_id,
                    new_show_channels=show_channels,
                ))
            if not commands:
                return
            command = (
                commands[0]
                if len(commands) == 1
                else MacroCommand.create(
                    f"Set waveform display for {len(commands)} clips",
                    commands,
                )
            )
            self._execute_command(command)
            return

        clip = self._timeline.get_clip_by_id(clip_id)
        track = self._timeline.get_track_by_id(track_id)
        if clip is None or track is None or track.is_locked:
            return
        self._execute_command(SetClipWaveformDisplayCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            new_show_channels=show_channels,
        ))

    def _on_clip_enable_toggle(self, clip_id: str, track_id: str) -> None:
        """Toggle clip enabled/disabled state."""
        if self._timeline is None:
            return
        # C09/INT-05：锁轨入口直接拒绝（菜单已同步灰显）
        if self._is_track_locked(track_id):
            self.status_message_requested.emit(
                self.tr("轨道已锁定，无法启用/禁用片段")
            )
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        from src.features.edit.core.commands import ToggleClipEnabledCommand
        cmd = ToggleClipEnabledCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            new_enabled=not clip.is_enabled,
            old_enabled=clip.is_enabled,
        )
        self._execute_command(cmd)

    def _toggle_selected_clips_enabled(self) -> None:
        """Toggle enabled state for all selected video, audio, and subtitle clips.

        Standard timeline clips, including adjustment clips, share
        :class:`ClipInstance`; subtitle clips use their dedicated immutable
        update command.  Grouping the changes in one macro gives the user one
        undo step regardless of how many clip types are selected.
        """
        if self._timeline is None:
            return

        from src.features.edit.core.commands import (
            ToggleClipEnabledCommand,
            UpdateSubtitleClipCommand,
        )

        commands: list[Any] = []
        for selection_item in self._selection.items:
            if selection_item.item_type != SelectionType.CLIP:
                continue

            clip_id = selection_item.item_id
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip is not None:
                track = self._timeline.get_track_by_id(clip.track_id)
                if track is not None and not track.is_locked:
                    commands.append(ToggleClipEnabledCommand.create(
                        track_id=clip.track_id,
                        clip_id=clip.clip_id,
                        new_enabled=not clip.is_enabled,
                        old_enabled=clip.is_enabled,
                    ))
                continue

            subtitle_clip = self._timeline.get_subtitle_clip_by_id(clip_id)
            if subtitle_clip is None:
                continue
            track = self._timeline.find_subtitle_clip_owner_track(clip_id)
            if track is None or track.is_locked or subtitle_clip.is_edit_locked():
                continue
            commands.append(UpdateSubtitleClipCommand(
                command_id=str(uuid.uuid4()),
                label=("Enable subtitle clip" if not subtitle_clip.is_enabled
                       else "Disable subtitle clip"),
                clip_id=subtitle_clip.clip_id,
                updates={"is_enabled": not subtitle_clip.is_enabled},
            ))

        if not commands:
            return
        if len(commands) == 1:
            self._execute_command(commands[0])
        else:
            self._execute_command(MacroCommand.create(
                f"Toggle enabled for {len(commands)} clips", commands
            ))

    def _on_clip_unlink(self, clip_id: str, track_id: str) -> None:
        """Unlink a clip from its linked partner."""
        if self._timeline is None:
            return
        # C09/INT-05：任一侧轨道锁定时拒绝解链（命令层守卫兜底）
        clip = self._timeline.get_clip_by_id(clip_id)
        linked = self._get_reciprocal_linked_clip(clip)
        if clip is None or linked is None:
            return
        if self._is_track_locked(track_id) or self._is_track_locked(linked.track_id):
            self.status_message_requested.emit(
                self.tr("轨道已锁定，无法链接/解链")
            )
            return
        from src.features.edit.core.commands import SetClipLinkedCommand
        cmd = SetClipLinkedCommand.create_unlink(
            clip_a_id=clip_id,
            clip_a_track_id=track_id,
            clip_b_id=linked.clip_id,
            clip_b_track_id=linked.track_id,
        )
        self._execute_command(cmd)

    def _on_clip_link(self, clip_id: str, track_id: str) -> None:
        """Link selected clips together (requires exactly 2 clips selected)."""
        if self._timeline is None:
            return
        selected_ids = list(self._selection.selected_clip_ids)
        if len(selected_ids) != 2:
            logger.info("Link requires exactly 2 selected clips")
            return
        clip_a = self._timeline.get_clip_by_id(selected_ids[0])
        clip_b = self._timeline.get_clip_by_id(selected_ids[1])
        if clip_a is None or clip_b is None:
            return
        from src.features.edit.core.commands import SetClipLinkedCommand
        cmd = SetClipLinkedCommand.create_link(
            clip_a_id=clip_a.clip_id,
            clip_a_track_id=clip_a.track_id,
            clip_b_id=clip_b.clip_id,
            clip_b_track_id=clip_b.track_id,
        )
        try:
            self._execute_command(cmd)
        except ValueError as exc:
            logger.warning("Cannot link selected clips: %s", exc)

    def _on_change_clip_speed_dialog_requested(self, clip_id: str, track_id: str) -> None:
        """Edit speed in a Resolve-style dialog using the existing command adapter.

        Options whose semantics are absent from the current command remain
        explicit host requests. Never silently drop ripple/freeze/timing intent.
        """
        from src.features.edit.ui.change_clip_speed_dialog import ChangeClipSpeedDialog
        from src.shared.i18n.i18n import I18N
        from PyQt6.QtWidgets import QDialog
        if self._timeline is None:
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None or self._is_track_locked(track_id):
            return
        params = self._retime_speed_params_for_clip(clip)
        dialog = ChangeClipSpeedDialog(
            speed=params.speed, source_fps=float(self._get_asset_fps(clip.asset_id)),
            timeline_fps=float(self._timeline.fps), duration_frames=clip.timeline_duration,
            reverse=params.reverse, preserve_pitch=params.preserve_pitch, parent=self,
            freeze=clip.freeze_source_frame is not None,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        settings = dialog.settings()
        animated = bool(clip.animated_params) or any(
            bool(getattr(effect, "animated_params", None)) for effect in clip.effects)
        # B14：高级意图（ripple/freeze/maintain/既有冻结/速度点/动画）
        # 走宿主接收链——契约校验/能力预检/单事务执行或明确诊断都在
        # 接收层完成；无消费者的独立预览场景保持"不支持"反馈。
        advanced = (settings["ripple"] or settings["freeze"]
                    or settings["keyframe_timing"] == "maintain"
                    or clip.freeze_source_frame is not None
                    or params.speed_points or animated)
        if advanced:
            if not self.receivers(self.clip_speed_settings_requested):
                self.status_message_requested.emit(I18N.tr(
                    "edit.speed_dialog.unsupported",
                    "The selected speed options are not supported yet. "
                    "The clip is unchanged."))
                return
            self.clip_speed_settings_requested.emit(clip_id, track_id, settings)
            return
        new_params = params.with_speed(settings["speed"]).with_reverse(settings["reverse"]).with_preserve_pitch(settings["preserve_pitch"])
        if new_params == params:
            return
        try:
            self._commit_retime_params(clip, new_params, "Change clip speed")
        except (ValueError, RuntimeError) as exc:
            self.status_message_requested.emit(str(exc))

    def _on_clip_speed_change(self, clip_id: str, track_id: str, speed: float) -> None:
        """Handle clip speed change from context menu."""
        from src.features.edit.core.commands import ChangeClipSpeedCommand
        # C09/INT-05：锁轨（含锁定链接伙伴轨）拒绝变速；命令层守卫兜底
        if self._is_track_locked(track_id):
            self.status_message_requested.emit(
                self.tr("轨道已锁定，无法变速")
            )
            return
        clip = self._timeline.get_clip_by_id(clip_id) if self._timeline is not None else None
        if clip is None:
            return
        params = self._retime_speed_params_for_clip(clip).with_speed(speed)
        cmd = ChangeClipSpeedCommand.create(clip_id, track_id, params)
        try:
            self._execute_command(cmd)
        except ValueError as exc:
            logger.warning("Speed change rejected: %s", exc)
            self.status_message_requested.emit(str(exc))

    def _plan_rate_stretch_speed(
        self, clip: ClipInstance, new_duration: int,
    ) -> "tuple[float, int, str | None] | None":
        """R04：比例拉伸统一 planner——用权威源时基反解速度。

        旧换算 ``s' = s × old/new`` 拿**已舍入**的旧时长推速（源 5 帧、
        3x、时间线 2 帧拉到 10 帧得 0.6x → 实际 8 帧）。这里从源帧数
        与源/时间线 fps 的精确积分（Fraction 无舍入）反解
        ``s = base_ticks / target``，未钳时提交时长 == 松手目标
        （图形边缘 = 提交边缘）；越界钳到 [MIN, MAX] 并返回实际可
        实现时长与钳制原因供高级请求/诊断使用。速度无变化返回 None
        （零 history）。
        """
        from src.features.edit.core.timeline_ops import (
            solve_speed_for_timeline_duration,
        )

        seq_fps = getattr(self._timeline, "fps", None)
        speed, achievable, reason = solve_speed_for_timeline_duration(
            clip.source_duration, new_duration,
            clip._source_fps_num, clip._source_fps_den,
            clip._timeline_fps_num or getattr(seq_fps, "num", None),
            clip._timeline_fps_den or getattr(seq_fps, "den", None),
        )
        params = self._retime_speed_params_for_clip(clip)
        if speed == params.speed:
            return None  # 零变化：不入历史
        return speed, achievable, reason

    def _on_rate_stretch_finished(
        self, clip_id: str, track_id: str,
        original_duration: int, new_duration: int,
    ) -> None:
        """B4/B18: 比例拉伸结束——时长变化换算为速度并提交既有速度命令。

        R04：速度由统一 planner 从权威源时基反解（不拿已舍入时长推
        速）。非目标字段（reverse/preserve_pitch/插值等）经
        ``params.with_speed`` 保留；复杂 curve/freeze/动画（含 linked
        伙伴）预检不过时发高级请求并明确反馈，不降级执行；零变化
        不入历史。
        """
        clip = self._timeline.get_clip_by_id(clip_id) if self._timeline else None
        if (clip is None or new_duration <= 0
                or new_duration == original_duration
                or clip.timeline_duration != original_duration
                or self._position_lock_enabled
                or self._is_track_locked(track_id)):
            return
        plan = self._plan_rate_stretch_speed(clip, new_duration)
        if plan is None:
            return
        speed, achievable, reason = plan
        if self._retime_controls_need_backend(clip):
            self._request_advanced_retime(clip, "stretch", 0, {
                "original_duration": original_duration,
                "duration": new_duration,
                "speed": speed,
                "achievable_duration": achievable,
                "clamped": reason,
            })
            return
        params = self._retime_speed_params_for_clip(clip)
        try:
            self._commit_retime_params(
                clip, params.with_speed(speed), "Rate stretch")
        except (RuntimeError, ValueError) as exc:
            self.status_message_requested.emit(str(exc))
            return
        if reason is not None:
            logger.info(
                "Rate stretch clamped to %.2fx (%s); achievable %d frames "
                "(requested %d)", speed, reason, achievable, new_duration,
            )

    def _retime_controls_need_backend(self, clip: ClipInstance) -> bool:
        """Keep unsafe advanced timing edits out of the basic UI adapter."""
        members = [clip]
        partner = self._get_reciprocal_linked_clip(clip)
        if partner is not None:
            members.append(partner)
        return any(
            self._retime_speed_params_for_clip(member).speed_points
            or member.freeze_source_frame is not None
            or member.animated_params
            or any(getattr(effect, "animated_params", None) for effect in member.effects)
            for member in members
        )

    def _request_advanced_retime(
        self, clip: ClipInstance, action: str, source_offset: int,
        value: object, reason: "str | None" = None,
    ) -> None:
        """Expose the full menu intent without silently changing its meaning.

        R10：逐项诊断——每个不支持的动作给出**具体**原因（Rewind 不是
        整片倒放、曲线分段事务缺模型等），不再一律"not supported yet"。
        """
        from src.shared.i18n.i18n import I18N
        if reason is None:
            reasons = {
                "rewind": I18N.tr(
                    "edit.retime.rewind_unsupported",
                    "Rewind needs negative segment speeds, which the retime "
                    "model does not support yet — it is not whole-clip "
                    "reverse; the clip is unchanged."),
                "stretch": I18N.tr(
                    "edit.retime.stretch_curves_unsupported",
                    "Stretching across speed-point curves or animated clips "
                    "is not supported yet; the clip is unchanged."),
                "reverse": I18N.tr(
                    "edit.retime.reverse_curves_unsupported",
                    "Reversing a clip with speed-point curves is not "
                    "supported yet; the clip is unchanged."),
                "freeze": I18N.tr(
                    "edit.retime.freeze_segment_unsupported",
                    "Inserting a frozen segment is a separate transaction; "
                    "whole-clip freeze is available from the speed dialog."),
            }
            reason = reasons.get(action) or I18N.tr(
                "edit.speed_dialog.unsupported",
                "The selected speed options are not supported yet. The "
                "clip is unchanged.")
        self.clip_speed_settings_requested.emit(clip.clip_id, clip.track_id, {
            "operation": "retime_controls", "action": action,
            "source_offset": source_offset, "value": value,
        })
        self.status_message_requested.emit(reason)

    def _on_retime_stretch_finished(
        self, clip_id: str, track_id: str, original_duration: int, new_duration: int,
    ) -> None:
        """R04：retime 顶部拉伸与比例拉伸共用统一 planner（权威源时基）。"""
        clip = self._timeline.get_clip_by_id(clip_id) if self._timeline else None
        if (clip is None or new_duration <= 0 or new_duration == original_duration
                or clip.timeline_duration != original_duration
                or self._position_lock_enabled or self._is_track_locked(track_id)):
            return
        plan = self._plan_rate_stretch_speed(clip, new_duration)
        if plan is None:
            return
        speed, achievable, reason = plan
        if self._retime_controls_need_backend(clip):
            self._request_advanced_retime(clip, "stretch", 0, {
                "original_duration": original_duration, "duration": new_duration,
                "speed": speed, "achievable_duration": achievable,
                "clamped": reason})
            return
        params = self._retime_speed_params_for_clip(clip)
        try:
            self._commit_retime_params(clip, params.with_speed(speed), "Change clip speed")
        except (ValueError, RuntimeError) as exc:
            self.status_message_requested.emit(str(exc))

    def _on_fade_end_curve_requested(
        self, clip_id: str, track_id: str, end: str, curve: object,
    ) -> None:
        """R09：单端淡化曲线菜单 → 单命令提交（另一端/共享字段不动）。"""
        if self._timeline is None:
            return
        if self._is_track_locked(track_id):
            return
        from src.features.edit.core.commands import SetFadeEndCurveCommand
        try:
            self._execute_command(SetFadeEndCurveCommand.create(
                clip_id, track_id, end, curve))
        except (ValueError, RuntimeError) as exc:
            self.status_message_requested.emit(str(exc))

    def _on_retime_point_dragged(
        self, clip_id: str, track_id: str,
        origin_offset: int, preview_offset: int,
    ) -> None:
        """R08：把手拖动预览反馈（本地几何预览在 item 内，这里只报状态）。"""
        self.status_message_requested.emit(I18N.tr(
            "edit.retime.point_drag",
            "Speed point: {origin} → {preview} (source frames)"
        ).format(origin=origin_offset, preview=preview_offset))

    def _on_retime_point_move_finished(
        self, clip_id: str, track_id: str,
        origin_offset: int, new_offset: int,
    ) -> None:
        """R08：松手 → 单命令提交（MoveSpeedPointCommand，B15/R02 语义）。

        冻结片段明确拒绝并提示（能力矩阵外零写入）；邻点交叉等非法
        落点由命令的严格开区间校验抛错 → 状态栏诊断。
        """
        if self._timeline is None:
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        if self._is_track_locked(track_id):
            return
        if self._position_lock_enabled:
            self.status_message_requested.emit(I18N.tr(
                "edit.retime.point_lock_blocked",
                "Position lock is on; speed point unchanged"))
            return
        if clip.freeze_source_frame is not None:
            self.status_message_requested.emit(I18N.tr(
                "edit.retime.point_frozen",
                "The clip is frozen; speed points cannot be moved"))
            return
        from src.features.edit.core.commands import MoveSpeedPointCommand
        try:
            self._execute_command(MoveSpeedPointCommand.create(
                clip_id, track_id, int(origin_offset), int(new_offset)))
        except (ValueError, RuntimeError) as exc:
            self.status_message_requested.emit(str(exc))

    def _on_retime_action_requested(
        self, clip_id: str, track_id: str, action: str, source_offset: int, value: object,
    ) -> None:
        """Dispatch supported settings; retain advanced segment intent for B14.

        R10 收编的事务（stage-2/4/5 中模型可承接的部分）：

        - ``reset_clip``：**不**走能力门——重置正是复杂状态的出口；
          曲线/动画/互链经 ``ChangeClipSpeedCommand`` 的 B17/R05/R02
          快照语义单步还原到 1x；
        - ``freeze``：整片定格在 source_offset 映射到的源帧
          （``SetClipFreezeCommand``；曲线参数保留在底层，解冻恢复）；
        - 其余（曲线上的分段 stretch/reverse、rewind、曲线 ramp）保持
          **显式不支持 + 逐项诊断**（``_request_advanced_retime``）。
        """
        clip = self._timeline.get_clip_by_id(clip_id) if self._timeline else None
        if clip is None or self._is_track_locked(track_id):
            return
        params = self._retime_speed_params_for_clip(clip)
        # B14 stage-3：单点删除——被删段的边界点移除后并入前段速度，
        # 不是抹平整条曲线（without_speed_points 禁止冒充）。
        if action == "clear_point":
            try:
                updated = params.with_speed_point_removed(source_offset)
            except ValueError as exc:
                self.status_message_requested.emit(str(exc))
                return
            if updated is params:
                return
            try:
                self._commit_retime_params(clip, updated, "Clear speed point")
            except (ValueError, RuntimeError) as exc:
                self.status_message_requested.emit(str(exc))
                return
            self._retime_control_clip_ids.add(clip_id)
            return
        # R10：重置不受能力门限制（复杂状态的出口）
        if action == "reset_clip":
            updated = params.with_speed(1.0).with_reverse(False)
            if isinstance(updated, SpeedParams) and updated.speed_points:
                updated = SpeedParams(speed=1.0)  # 清空全部曲线意图
            if params == updated:
                return
            try:
                self._commit_retime_params(clip, updated, "Reset clip speed")
            except (ValueError, RuntimeError) as exc:
                self.status_message_requested.emit(str(exc))
                return
            self._retime_control_clip_ids.add(clip_id)
            self.status_message_requested.emit(I18N.tr(
                "edit.retime.reset_done",
                "Clip speed reset to 100% (single undo)"))
            return
        # R10：整片定格（源帧 = source_offset 的权威映射；时长保留）
        if action == "freeze":
            if clip.freeze_source_frame is not None:
                self._request_advanced_retime(
                    clip, action, source_offset, value,
                    reason=I18N.tr(
                        "edit.retime.already_frozen",
                        "The clip is already frozen; unfreeze first"))
                return
            from src.features.edit.core.commands import SetClipFreezeCommand
            source_frame = self._authoritative_boundary(clip, source_offset)
            if source_frame is None:
                self._request_advanced_retime(
                    clip, action, source_offset, value,
                    reason=I18N.tr(
                        "edit.retime.freeze_unmapped",
                        "Freeze point could not be mapped to a source "
                        "frame; the clip is unchanged"))
                return
            try:
                self._execute_command(SetClipFreezeCommand.create(
                    clip_id, track_id, int(source_frame)))
            except (ValueError, RuntimeError) as exc:
                self.status_message_requested.emit(str(exc))
                return
            self._retime_control_clip_ids.add(clip_id)
            self.status_message_requested.emit(I18N.tr(
                "edit.retime.freeze_done",
                "Froze the clip at source frame {frame} (single undo)"
            ).format(frame=int(source_frame)))
            return
        if action not in {"reverse", "pitch"} or self._retime_controls_need_backend(clip):
            self._request_advanced_retime(clip, action, source_offset, value)
            return
        if action == "reverse":
            updated = params.with_reverse(not params.reverse)
        else:
            updated = params.with_preserve_pitch(bool(value))
        if params == updated:
            return
        try:
            self._commit_retime_params(clip, updated, "Change clip speed")
        except (ValueError, RuntimeError) as exc:
            self.status_message_requested.emit(str(exc))

    def _on_retime_controls_toggled(
        self, clip_id: str, track_id: str, visible: bool,
    ) -> None:
        """Toggle the transient Resolve-style retime strip for one clip."""
        _ = track_id
        if self._interaction.primary_clip_id == clip_id:
            self._cancel_current_interaction()
        keep_video_bottom = self._is_video_stack_aligned_to_splitter()
        if visible:
            self._retime_control_clip_ids.add(clip_id)
        else:
            self._retime_control_clip_ids.discard(clip_id)
        item = self._items_map.get(clip_id)
        if isinstance(item, ClipItem):
            item.set_retime_controls_visible(visible)
        if keep_video_bottom:
            self._align_video_stack_to_splitter()
        self._layout_track_items()
        self.viewport().update()

    def _retime_speed_params_for_clip(self, clip: ClipInstance) -> SpeedParams:
        """Normalize legacy numeric speed before a retime-control edit."""
        if isinstance(clip.speed, SpeedParams):
            return clip.speed
        return SpeedParams(speed=float(clip.speed))

    def _commit_retime_params(
        self, clip: ClipInstance, params: SpeedParams, label: str,
    ) -> None:
        """Commit retime data through the existing immutable command stack."""
        from src.features.edit.core.commands import ChangeClipSpeedCommand

        command = ChangeClipSpeedCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            clip_id=clip.clip_id,
            track_id=clip.track_id,
            new_speed=params,
        )
        self._execute_command(command)

    def _on_freeze_frame_requested(self, clip_id: str, track_id: str, playhead_frame: int) -> None:
        """A2: 在播放头处冻结（右键菜单）——分割 + 右段定格，单步可撤销。"""
        _ = track_id
        if self._timeline is None:
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None or not (
            clip.timeline_start < playhead_frame < clip.timeline_end
        ):
            return
        from src.features.edit.core.commands import FreezeFrameAtPlayheadCommand
        try:
            self._execute_command(FreezeFrameAtPlayheadCommand.create(clip_id, playhead_frame))
        except (RuntimeError, ValueError) as exc:
            from src.shared.i18n.i18n import I18N
            logger.warning("Freeze frame failed: %s", exc)
            self.status_message_requested.emit(
                I18N.tr("edit.clip.freeze_failed", "冻结帧失败：{msg}", msg=str(exc))
            )

    def _on_replace_placeholder_requested(self, clip_id: str, track_id: str) -> None:
        """B7: 占位片段"Replace With…"——选择真实素材并原子替换。"""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox

        if self._timeline is None or self._project is None:
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        from src.features.edit.core.placeholder_frame import is_placeholder_asset
        asset = self._project.get_asset_by_id(clip.asset_id)
        if asset is None or not is_placeholder_asset(asset):
            return

        # 候选 = 工程中的真实视觉素材（有路径的 video/image/image_sequence）
        from src.shared.i18n.i18n import I18N
        candidates = []
        for a in self._project.assets.values():
            if a.media_type in ("video", "image", "image_sequence") and a.path:
                candidates.append(a)
        if not candidates:
            QMessageBox.information(
                self,
                I18N.tr("edit.placeholder.replace_title", "替换占位片段"),
                I18N.tr("edit.placeholder.no_candidates", "工程中没有可用的真实素材。"),
            )
            return
        names = [f"{a.display_name}  ({a.media_type})" for a in candidates]
        choice, ok = QInputDialog.getItem(
            self,
            I18N.tr("edit.placeholder.replace_title", "替换占位片段"),
            I18N.tr("edit.placeholder.pick_asset", "选择真实素材："),
            names, 0, False,
        )
        if not ok:
            return
        new_asset = candidates[names.index(choice)]
        from src.features.edit.core.commands import ReplaceClipAssetCommand

        # B7: 真素材较短时按其时长截断（total_frames 已知才传；未知的
        # 素材类型保持原时长，由解码层诚实降级）。
        real_duration = int(getattr(new_asset, "total_frames", 0) or 0)
        try:
            self._execute_command(
                ReplaceClipAssetCommand.create(
                    clip_id, track_id, new_asset.asset_id,
                    clamp_source_duration=real_duration or None))
        except (RuntimeError, ValueError) as exc:
            logger.warning("Replace placeholder failed: %s", exc)

    @staticmethod
    def _finalize_normalize_analysis(progress, thread, result_label, spin,
                                     ok: bool, text: str, gain) -> None:
        """响度分析收尾：关进度框、停线程并回写结果；对话框已销毁时丢弃。

        D03/F1.1 重放发现：分析 worker 在后台线程运行期间用户可关闭增益
        对话框，QProgressDialog/QThread/QLabel/QDoubleSpinBox 的 C++ 对象
        随对话框销毁（PyQt6 无 QPointer 绑定，PyQt 包装器仍存活），旧
        ``_on_finished`` 直接 ``setText``/``setValue`` 会抛 RuntimeError，
        并把过时增益写回已销毁 UI。此处按本代码库既有的窄域
        ``except RuntimeError``（已删除 Qt 对象）守卫惯用法逐控件防护，
        过时结果丢弃、线程引用仍尽力 ``quit()`` 收尾。
        """
        for finish_call in (lambda: progress.close(),
                            lambda: thread.quit() if thread is not None else None):
            try:
                finish_call()
            except RuntimeError:
                pass
        try:
            result_label.setText(text)
        except RuntimeError:
            return  # 标签已销毁——过时结果整体丢弃
        if ok and gain is not None:
            try:
                spin.setValue(max(-48.0, min(12.0, float(gain))))
            except RuntimeError:
                pass

    def _on_audio_gain_dialog_requested(self, clip_id: str, track_id: str) -> None:
        """C3/E14: Audio Gain 对话框（手输 dB + 分析并归一化）。

        “分析并归一化”接响度管线：解码片段源音频 → BS.1770 分析
        LUFS/真峰值（可取消）→ compute_normalize_gain_db 给出目标增益
        （-23 LUFS，真峰值上限 -3 dBTP）→ 用户 OK 后经
        SetClipAudioGainCommand 一次提交；取消/失败零提交。
        """
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QDoubleSpinBox,
            QLabel,
            QProgressDialog,
            QPushButton,
            QVBoxLayout,
        )

        from src.shared.i18n.i18n import I18N

        if self._timeline is None:
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        current_db = float(getattr(clip, "audio_gain_db", 0.0) or 0.0)

        dialog = QDialog(self)
        dialog.setWindowTitle(I18N.tr("edit.clip.audio_gain", "Audio Gain…"))
        layout = QVBoxLayout(dialog)
        spin = QDoubleSpinBox()
        spin.setRange(-48.0, 12.0)
        spin.setDecimals(1)
        spin.setSuffix(I18N.tr("edit.clip.db_suffix", " dB"))
        spin.setValue(current_db)
        layout.addWidget(QLabel(I18N.tr(
            "edit.clip.audio_gain_prompt",
            "增益（dB，范围 -48 ~ +12，0 = 不变）：",
        )))
        layout.addWidget(spin)

        result_label = QLabel("")
        result_label.setWordWrap(True)
        layout.addWidget(result_label)

        def _run_analysis() -> None:
            asset_path = self._get_asset_path(clip.asset_id)
            if not asset_path:
                result_label.setText(
                    I18N.tr("edit.clip.normalize_asset_missing", "无法定位素材文件，无法分析"))
                return
            fps = self._get_asset_fps(clip.asset_id) or 25.0
            worker = _NormalizeGainWorker(
                asset_path,
                source_start_frame=int(clip.source_start),
                source_fps=float(fps),
                duration_frames=int(clip.source_duration),
            )
            thread = QThread(dialog)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)

            progress = QProgressDialog("正在分析片段响度…", "取消", 0, 0, dialog)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            # R09（第七轮审计）：取消必须在 GUI 线程直接置位——连接到
            # worker.cancel 的跨线程信号是排队调用，worker 忙于 run 的
            # 解码循环时排在后面，取消到分析结束才生效。lambda 无线程
            # 亲和，取消按钮点击时同步置位原子布尔，worker 在下一解码
            # 块（≤1s 音频）退出。
            progress.canceled.connect(lambda: worker.cancel())

            def _on_finished(ok: bool, text: str, gain) -> None:
                # D03/F1.1：对话框可能已被关闭——过时结果经守卫丢弃。
                TimelineView._finalize_normalize_analysis(
                    progress, thread, result_label, spin, ok, text, gain)

            worker.finished.connect(_on_finished)
            # 线程生命周期异步收尾：finished→deleteLater；取消后 worker
            # 在下一块退出，UI 不做同步 wait（旧实现取消后还阻塞
            # thread.wait(5000)）。
            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            # D03（第六轮审计）：解码/DSP 全部在 worker 线程执行，UI 线程
            # 只跑对话框事件循环——此前同步 subprocess+分析会阻塞 120s，
            # 取消按钮无事件机会。
            thread.start()
            progress.exec()
            thread.quit()

        analyze_btn = QPushButton(I18N.tr(
            "edit.clip.audio_gain_analyze",
            "分析并归一化（-23 LUFS / 峰值 -3 dBTP）",
        ))
        analyze_btn.clicked.connect(_run_analysis)
        layout.addWidget(analyze_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if not dialog.exec():
            return  # 取消/关闭：零提交
        gain = float(spin.value())
        if abs(gain - current_db) < 1e-9:
            return  # 无变化：不产生历史/revision 副作用
        from src.features.edit.core.commands import SetClipAudioGainCommand
        try:
            self._execute_command(
                SetClipAudioGainCommand.create([clip_id], gain))
        except (RuntimeError, ValueError) as exc:
            logger.warning("Set clip gain failed: %s", exc)
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, I18N.tr(
                "edit.clip.audio_gain", "Audio Gain…"), str(exc))

    def _analyze_clip_normalize_gain(
        self,
        asset_path: str,
        start_sec: float,
        duration_sec: float,
        progress_cancel=None,
    ) -> tuple[bool, str, float | None]:
        """E14 兼容入口：同步分析（保留给旧调用/测试）。

        D02/D03 之后生产 UI 走 :class:`_NormalizeGainWorker` 后台线程；
        本方法保留同步语义，内部复用同一测量实现。
        """
        worker = _NormalizeGainWorker(
            asset_path,
            source_start_frame=0,
            source_fps=25.0,
            duration_frames=0,  # 0 = 按 start/duration 秒驱动
            start_seconds=max(0.0, float(start_sec)),
            duration_seconds=max(0.05, float(duration_sec)),
        )
        if progress_cancel is not None and progress_cancel():
            return False, "分析已取消", None
        return worker.analyze_sync(cancel_check=progress_cancel)

    def _on_retime_speed_point_requested(self, clip_id: str, track_id: str) -> None:
        """Insert a speed point at the playhead, matching Resolve's command."""
        _ = track_id
        if self._timeline is None:
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None or not (clip.timeline_start < self._playhead_frame < clip.timeline_end):
            return
        params = self._retime_speed_params_for_clip(clip)
        physical_offset = clip.timeline_to_source_frame(self._playhead_frame) - clip.source_start
        source_offset = physical_offset
        if params.reverse:
            source_offset = clip.source_duration - 1 - physical_offset
        try:
            updated = params.with_speed_point(source_offset, clip.source_duration)
        except ValueError:
            return
        self._retime_control_clip_ids.add(clip_id)
        self._commit_retime_params(clip, updated, "Add speed point")

    def _on_retime_speed_points_clear_requested(self, clip_id: str, track_id: str) -> None:
        """Remove all speed-point boundaries without changing base speed."""
        _ = track_id
        if self._timeline is None:
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        self._commit_retime_params(
            clip,
            self._retime_speed_params_for_clip(clip).without_speed_points(),
            "Clear speed points",
        )

    def _on_retime_segment_speed_requested(
        self, clip_id: str, track_id: str, source_offset: int, speed: float,
    ) -> None:
        """Set the selected retime segment's speed from its percentage menu."""
        _ = track_id
        if self._timeline is None:
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        params = self._retime_speed_params_for_clip(clip)
        try:
            updated = params.with_speed_point_speed(source_offset, speed)
        except ValueError:
            return
        self._retime_control_clip_ids.add(clip_id)
        self._commit_retime_params(clip, updated, f"Change retime segment to {speed * 100.0:.0f}%")

    def _on_retime_point_easing_requested(
        self, clip_id: str, track_id: str, source_offset: int, easing: str,
    ) -> None:
        """A3.1: 切换速度点斜坡插值（并入既有 retime 提交路径）。"""
        _ = track_id
        if self._timeline is None:
            return
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return
        params = self._retime_speed_params_for_clip(clip)
        try:
            updated = params.with_point_easing(source_offset, easing)
        except ValueError:
            return
        self._retime_control_clip_ids.add(clip_id)
        self._commit_retime_params(
            clip, updated, f"Set speed ramp {easing} at {source_offset}")

    def _on_fade_in_preview(self, clip_id: str, track_id: str, new_fade_in: int) -> None:
        """Keep fade preview local while the pointer is moving.

        ``ClipItem`` updates its own geometry before emitting this signal.
        Broadcasting ``timeline_changed`` for every mouse event makes the
        controller refresh the preview renderer and other panels.  The final
        mouse-release signal commits the value through the command stack.
        """
        _ = (clip_id, track_id, new_fade_in)

    def _on_fade_out_preview(self, clip_id: str, track_id: str, new_fade_out: int) -> None:
        """Keep fade preview local while the pointer is moving."""
        _ = (clip_id, track_id, new_fade_out)

    def _on_fade_finished(
        self, clip_id: str, track_id: str,
        old_fade_in: int, old_fade_out: int,
        new_fade_in: int, new_fade_out: int,
    ) -> None:
        """Fade drag finished — commit via command stack for undo support."""
        from src.features.edit.core.commands import ChangeClipFadeCommand
        cmd = ChangeClipFadeCommand.create(
            track_id=track_id,
            clip_id=clip_id,
            new_fade_in=new_fade_in,
            new_fade_out=new_fade_out,
            old_fade_in=old_fade_in,
            old_fade_out=old_fade_out,
        )
        try:
            self._execute_command(cmd)
        except (ValueError, RuntimeError) as exc:
            clip = self._timeline.get_clip_by_id(clip_id) if self._timeline is not None else None
            item = self._items_map.get(clip_id)
            if clip is not None and isinstance(item, ClipItem):
                item.update_clip_properties(clip)
            self.status_message_requested.emit(str(exc))

    def _on_clip_double_clicked(self, clip_id: str, track_id: str) -> None:
        """E09/VP-B06: 双击片段 → 发出在 Source Viewer 打开的请求。

        源帧走权威映射（``resolve_match_frame`` →
        ``clip.timeline_to_source_frame``，覆盖 source_start/trim/retime
        曲线/reverse/freeze/混合帧率）；此前的朴素公式
        ``source_start + (播放头 − 片段起点)`` 对变速/倒放片段给出错帧。
        转场覆盖剪辑点时双击侧即选中侧。Motion Graphic 等虚拟素材无源
        媒体可开 → 状态栏回因、不发打开请求（零写入）。
        """
        if self._timeline is None:
            return
        from src.features.edit.core.match_frame import (
            describe_match_frame_reason,
            resolve_match_frame,
        )
        playhead = max(0, int(self._playhead_frame))
        result = resolve_match_frame(self._timeline, clip_id, playhead)
        if not result.ok:
            self.status_message_requested.emit(
                describe_match_frame_reason(result.reason or ""))
            return
        self.clip_open_in_source_requested.emit(clip_id, result.source_frame)

    def _on_add_transition_requested(
        self,
        clip_id: str,
        track_id: str,
        edge: str,
        transition_type: str = "cross_dissolve",
    ) -> None:
        """
        Handle add transition request from clip context menu.

        Args:
            clip_id: Clip ID that was right-clicked
            track_id: Track ID containing the clip
            edge: Which edge ('left' or 'right')
            transition_type: 'cross_dissolve' 或 'dip_to_color'（E10）
        """
        if self._timeline is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        clip = track.get_clip_by_id(clip_id)
        if clip is None:
            return

        if edge == "right":
            # Find next clip (right side)
            prev, next_clip = track.get_adjacent_clips(clip_id)
            if next_clip is None:
                self.add_transition_at_clip_edge(
                    track_id=track_id,
                    clip_id=clip_id,
                    edge="right",
                    transition_type=transition_type,
                )
                return

            self.add_transition_at_cut_point(
                track_id=track_id,
                left_clip_id=clip_id,
                right_clip_id=next_clip.clip_id,
                transition_type=transition_type,
            )

        elif edge == "left":
            # Find previous clip (left side)
            prev_clip, next_ = track.get_adjacent_clips(clip_id)
            if prev_clip is None:
                self.add_transition_at_clip_edge(
                    track_id=track_id,
                    clip_id=clip_id,
                    edge="left",
                    transition_type=transition_type,
                )
                return

            self.add_transition_at_cut_point(
                track_id=track_id,
                left_clip_id=prev_clip.clip_id,
                right_clip_id=clip_id,
                transition_type=transition_type,
            )

    def _on_open_in_composite_requested(
        self,
        clip_id: str,
        track_id: str,
    ) -> None:
        """
        Handle "Open in Composite" request from clip context menu (P9.2).

        Emits signal to EditPage to create composite and switch pages.

        Args:
            clip_id: Clip ID that was right-clicked
            track_id: Track ID containing the clip
        """
        if self._timeline is None:
            return

        # Emit signal to EditPage (which will handle creation and page switch)
        self.open_in_composite_requested.emit(clip_id, track_id)

        logger.info(f"[P9.2] Open in Composite requested: clip={clip_id}, track={track_id}")

    def _on_transition_trim_left(
        self,
        transition_id: str,
        track_id: str,
        new_duration: int
    ) -> None:
        """Handle transition left trim during drag (live preview)."""
        # Visual preview only - TransitionItem handles internal geometry update
        # Command is created in _on_transition_trim_left_finished

    def _on_transition_trim_right(
        self,
        transition_id: str,
        track_id: str,
        new_duration: int
    ) -> None:
        """Handle transition right trim during drag (live preview)."""
        # Visual preview only - TransitionItem handles internal geometry update
        # Command is created in _on_transition_trim_right_finished

    def _on_transition_trim_left_finished(
        self,
        transition_id: str,
        track_id: str,
        old_duration: int,
        new_duration: int
    ) -> None:
        """Handle left trim finished: create and execute UpdateTransitionCommand."""
        # AUD-21102: 锁定轨道上的转场不可提交修剪（item 侧已拦截，此处兜底）
        if self._timeline is None or self._updating or self._is_track_locked(track_id):
            return

        if new_duration == old_duration:
            return

        cmd = UpdateTransitionCommand.create_duration(
            timeline=self._timeline,
            transition_id=transition_id,
            track_id=track_id,
            new_duration=new_duration,
        )
        self._execute_command(cmd)

    def _on_transition_trim_right_finished(
        self,
        transition_id: str,
        track_id: str,
        old_duration: int,
        new_duration: int
    ) -> None:
        """Handle right trim finished: create and execute UpdateTransitionCommand."""
        # AUD-21102: 锁定轨道上的转场不可提交修剪（item 侧已拦截，此处兜底）
        if self._timeline is None or self._updating or self._is_track_locked(track_id):
            return

        if new_duration == old_duration:
            return

        cmd = UpdateTransitionCommand.create_duration(
            timeline=self._timeline,
            transition_id=transition_id,
            track_id=track_id,
            new_duration=new_duration,
        )
        self._execute_command(cmd)

    def _on_transition_moved(
        self,
        transition_id: str,
        track_id: str,
        new_center_frame: int,
    ) -> None:
        """Handle live transition-position preview."""
        # TransitionItem owns the transient geometry. The command is committed
        # once on mouse release by _on_transition_move_finished.

    def _on_transition_move_finished(
        self,
        transition_id: str,
        track_id: str,
        old_center_frame: int,
        new_center_frame: int,
    ) -> None:
        """Commit a transition-position drag as one undoable command."""
        # AUD-21102: 锁定轨道上的转场不可提交移动（item 侧已拦截，此处兜底）
        if self._timeline is None or self._updating or self._is_track_locked(track_id):
            return
        if old_center_frame == new_center_frame:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return
        transition = next(
            (item for item in track.transitions if item.transition_id == transition_id),
            None,
        )
        if transition is None:
            return
        min_center, max_center = self._transition_center_bounds(track, transition)
        if not min_center <= new_center_frame <= max_center:
            return

        cmd = UpdateTransitionCommand.create_center_frame(
            timeline=self._timeline,
            transition_id=transition_id,
            track_id=track_id,
            new_center_frame=new_center_frame,
        )
        self._execute_command(cmd)

    @staticmethod
    def _transition_center_bounds(
        track: EditTrack,
        transition: TransitionInstance,
    ) -> tuple[int, int]:
        """Return the legal body-drag range for a transition.

        A two-sided transition may slide asymmetrically around its cut.  The
        only hard rule is that its rendered range still touches/crosses the
        cut and stays inside both source clips.  One-sided edge transitions
        remain anchored to their clip edge.
        """
        left = track.get_clip_by_id(transition.left_clip_id) if transition.left_clip_id else None
        right = track.get_clip_by_id(transition.right_clip_id) if transition.right_clip_id else None
        duration = transition.duration
        if left is None and right is None:
            return transition.center_frame, transition.center_frame
        if left is None or right is None:
            return transition.center_frame, transition.center_frame
        if left.timeline_end != right.timeline_start:
            return transition.center_frame, transition.center_frame

        cut = left.timeline_end
        min_start = max(left.timeline_start, cut - duration)
        max_start = min(cut, right.timeline_end - duration)
        if min_start > max_start:
            return transition.center_frame, transition.center_frame

        if transition.alignment == "start_on_cut":
            return min_start, max_start
        if transition.alignment == "end_on_cut":
            return min_start + duration, max_start + duration
        return min_start + duration // 2, max_start + duration // 2

    # ==================== Snap Helpers ====================

    def _get_snap_points(
        self,
        exclude_clip_id: Optional[str] = None,
        include_transitions: bool = True,
        include_markers: bool = True,
    ) -> tuple[int, ...]:
        """
        Get current snap points for timeline.

        Includes:
        - Playhead position
        - Clip start/end edges
        - Transition boundaries (center + edges)
        - Timeline markers (if any)

        Args:
            exclude_clip_id: Clip ID to exclude (for moving clips)
            include_transitions: Whether to include transition boundaries
            include_markers: Whether to include timeline markers

        Returns:
            Tuple of snap frame positions (sorted)
        """
        if self._timeline is None:
            return tuple()

        points = [0]  # Timeline start

        # Playhead position
        points.append(self._playhead_frame)

        # Clip edges from all tracks
        for track in self._timeline.all_tracks:
            for clip in track.clips:
                # Optionally exclude a specific clip (for moving clips)
                if exclude_clip_id and clip.clip_id == exclude_clip_id:
                    continue
                points.append(clip.timeline_start)
                points.append(clip.timeline_end)

            # Transition boundaries
            if include_transitions:
                for trans in track.transitions:
                    # Transition center frame
                    points.append(trans.center_frame)
                    # Transition start/end
                    half_duration = trans.duration // 2
                    points.append(trans.center_frame - half_duration)
                    points.append(trans.center_frame + half_duration)

        # Timeline markers
        if include_markers:
            for marker in self._timeline.markers:
                points.append(marker.frame)

        return tuple(sorted(set(points)))

    def _snap_frame(
        self,
        frame: int,
        snap_points: tuple[int, ...],
        show_indicator: bool = True,
        exclude_types: Optional[set] = None,
        exclude_source_ids: Optional[set[str]] = None,
    ) -> tuple[int, bool]:
        """
        Snap frame to nearest snap point within pixel-based threshold.

        Uses the SnapManager for proper pixel-based snapping that works
        consistently across zoom levels.

        Returns the snapped frame and whether snapping occurred.
        Also manages the snap indicator visibility.

        Args:
            frame: Frame position to snap
            snap_points: Available snap points (legacy, used if SnapManager not available)
            show_indicator: Whether to show/hide snap indicator
            exclude_types: SnapPointType set to exclude from snapping
            exclude_source_ids: clip/source IDs to ignore while snapping

        Returns:
            Tuple of (snapped_frame, did_snap)
        """
        if not self._snap_enabled:
            # Hide indicator when snap disabled
            if show_indicator and self._snap_indicator:
                self._snap_indicator.hide_indicator()
            return (frame, False)

        # P0-D: Use SnapManager with pixel-based threshold for consistent zoom behavior
        if self._timeline is not None and self._snap_manager:
            from src.features.edit.core.timeline_ops import SnapPointType
            snapped_frame, did_snap = self._snap_manager.find_nearest_frame(
                frame,
                self._pixels_per_frame,
                exclude_types=exclude_types,
                exclude_source_ids=exclude_source_ids,
            )

            # Update snap indicator visibility
            if show_indicator and self._snap_indicator:
                if did_snap:
                    snap_point = self._snap_manager.find_nearest(
                        frame,
                        self._pixels_per_frame,
                        exclude_types=exclude_types,
                        exclude_source_ids=exclude_source_ids,
                    )
                    snap_type_label = ""
                    if snap_point:
                        type_labels = {
                            SnapPointType.CLIP_START: "Cut",
                            SnapPointType.CLIP_END: "Cut",
                            SnapPointType.PLAYHEAD: "Playhead",
                            SnapPointType.MARKER: "Marker",
                            SnapPointType.SEQUENCE_START: "Start",
                            SnapPointType.TRANSITION_START: "Transition",
                            SnapPointType.TRANSITION_CENTER: "Transition",
                            SnapPointType.TRANSITION_END: "Transition",
                        }
                        snap_type_label = type_labels.get(snap_point.type, "")
                    self._snap_indicator.show_indicator(snapped_frame, snap_type_label)
                else:
                    self._snap_indicator.hide_indicator()

            return (snapped_frame, did_snap)

        # Legacy fallback using frame-based threshold
        if not snap_points:
            if show_indicator and self._snap_indicator:
                self._snap_indicator.hide_indicator()
            return (frame, False)

        threshold_frames = self._snap_threshold_frames

        # Find nearest snap point within threshold
        min_distance = threshold_frames + 1
        snapped_frame = frame

        for snap_point in snap_points:
            distance = abs(frame - snap_point)
            if distance < min_distance:
                min_distance = distance
                snapped_frame = snap_point

        did_snap = min_distance <= threshold_frames

        if show_indicator and self._snap_indicator:
            if did_snap:
                self._snap_indicator.show_indicator(snapped_frame)
            else:
                self._snap_indicator.hide_indicator()

        return (snapped_frame, did_snap)

    # ==================== Validation Helpers ====================

    def _validate_move_group(self, group: MoveGroup, *, allow_overwrite: bool = False) -> bool:
        """P3: Validate that all members in a MoveGroup can move atomically.

        Checks:
        - All members have non-negative new_start
        - No overlaps with non-group clips on each track unless overwrite is allowed
        - No clips land on locked tracks

        Uses the target track (new_track_id) if set, otherwise the old track.

        Returns:
            True if the entire group move is valid, False otherwise.
        """
        if not group.is_valid:
            return False

        # Collect member clip IDs to exclude from overlap checks
        member_ids = {m.clip_id for m in group.members}

        # Group members by their TARGET track for overlap checking
        track_members: dict[str, list[MoveGroupMember]] = {}
        for m in group.members:
            # Use target track if available, otherwise old track
            target_tid = m.new_track_id if m.new_track_id and m.new_track_id != m.track_id else m.track_id
            # Skip pseudo track IDs that don't exist yet
            if target_tid.startswith("__new_"):
                # New track will be empty — no overlap possible
                continue
            track_members.setdefault(target_tid, []).append(m)

        # Check overlaps per track
        for tid, members in track_members.items():
            track = self._timeline.get_track_by_id(tid)
            if track is None:
                return False
            # Check locked tracks
            if getattr(track, 'is_locked', False):
                return False

            existing_clips = [c for c in track.clips if c.clip_id not in member_ids]
            for m in members:
                clip = self._timeline.get_clip_by_id(m.clip_id)
                if clip is None:
                    return False
                new_end = m.new_start + clip.timeline_duration
                for other_member in members:
                    if other_member.clip_id == m.clip_id:
                        continue
                    other_clip = self._timeline.get_clip_by_id(other_member.clip_id)
                    if other_clip is None:
                        return False
                    other_end = other_member.new_start + other_clip.timeline_duration
                    if m.new_start < other_end and new_end > other_member.new_start:
                        return False
                if allow_overwrite:
                    continue
                for other in existing_clips:
                    if m.new_start < other.timeline_end and new_end > other.timeline_start:
                        return False

        return True

    def set_linked_selection_enabled(self, enabled: bool) -> None:
        """P3: Toggle linked selection. When on, dragging video moves linked audio."""
        if (enabled != self._linked_selection_enabled and self._interaction.state in
                {InteractionState.SLIPPING, InteractionState.SLIDING}):
            self._cancel_current_interaction()
        self._linked_selection_enabled = enabled
        self._independent_selection = False
        self.set_selection(self._selection)
        # Sync all ClipItems so their visual indicators reflect the current state
        for item in self._items_map.values():
            if isinstance(item, ClipItem):
                item._linked_selection_enabled = enabled
                item.update()
        self.linked_selection_changed.emit(enabled)

    def _get_reciprocal_linked_clip(self, clip: Optional[ClipInstance]) -> Optional[ClipInstance]:
        """Return linked clip only when the link is a valid two-way relationship."""
        if self._timeline is None:
            return None
        return self._timeline.get_reciprocal_linked_clip(clip)

    def _get_valid_linked_clip_id(self, clip: Optional[ClipInstance]) -> Optional[str]:
        """Return a link ID suitable for UI display and hover behavior."""
        linked = self._get_reciprocal_linked_clip(clip)
        return linked.clip_id if linked is not None else None

    def _create_overwrite_move_command(
        self,
        members: tuple[MoveGroupMember, ...] | list[MoveGroupMember],
        label: str = "Move clips with overwrite",
    ) -> MoveClipsOverwriteCommand:
        """Create a move command that overwrites existing material on target tracks."""
        moves: list[dict[str, Any]] = []
        for member in members:
            target_track_id = (
                member.new_track_id
                if member.new_track_id and not member.new_track_id.startswith("__new_")
                else member.track_id
            )
            moves.append({
                "clip_id": member.clip_id,
                "source_track_id": member.track_id,
                "target_track_id": target_track_id,
                "new_start": member.new_start,
                "old_start": member.old_start,
            })
        return MoveClipsOverwriteCommand.create(moves, label=label)

    def _validate_move_position(
        self,
        clip_id: str,
        track_id: str,
        new_start: int
    ) -> bool:
        """
        Validate that moving clip to new position won't overlap with others.

        Args:
            clip_id: Clip to move
            track_id: Track containing clip
            new_start: New timeline start position

        Returns:
            True if position is valid, False if overlap detected
        """
        if self._timeline is None:
            return False

        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return False

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return False

        new_end = new_start + clip.timeline_duration

        # Check overlap with other clips
        for other in track.clips:
            if other.clip_id == clip_id:
                continue
            if new_start < other.timeline_end and new_end > other.timeline_start:
                return False

        return True

    def _validate_trim_left(
        self,
        clip_id: str,
        track_id: str,
        new_start: int
    ) -> bool:
        """
        Validate that left trim won't overlap with previous clip.

        Args:
            clip_id: Clip to trim
            track_id: Track containing clip
            new_start: New timeline start position

        Returns:
            True if trim is valid, False if overlap detected
        """
        if self._timeline is None:
            return False

        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return False

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return False

        # Find previous clip
        prev_clip = None
        for other in track.clips:
            if other.clip_id == clip_id:
                continue
            if other.timeline_end <= clip.timeline_start:
                if prev_clip is None or other.timeline_end > prev_clip.timeline_end:
                    prev_clip = other

        if prev_clip is None:
            return True  # No previous clip, always valid

        # Check if new start would overlap
        if new_start < prev_clip.timeline_end:
            return False

        return True

    def _validate_trim_right(
        self,
        clip_id: str,
        track_id: str,
        new_duration: int
    ) -> bool:
        """
        Validate that right trim won't overlap with next clip.

        Args:
            clip_id: Clip to trim
            track_id: Track containing clip
            new_duration: New timeline duration

        Returns:
            True if trim is valid, False if overlap detected
        """
        if self._timeline is None:
            return False

        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return False

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return False

        new_end = clip.timeline_start + new_duration

        # Find next clip
        next_clip = None
        for other in track.clips:
            if other.clip_id == clip_id:
                continue
            if other.timeline_start >= clip.timeline_end:
                if next_clip is None or other.timeline_start < next_clip.timeline_start:
                    next_clip = other

        if next_clip is None:
            return True  # No next clip, always valid

        # Check if new end would overlap
        if new_end > next_clip.timeline_start:
            return False

        return True

    def _get_clip_source_duration(self, clip_id: str) -> int:
        """Get clip source duration from timeline."""
        if self._timeline is None:
            return 0
        clip = self._timeline.get_clip_by_id(clip_id)
        if clip is None:
            return 0
        return clip.source_duration

    def _move_selected_clips(
        self,
        primary_clip_id: str,
        old_start: int,
        new_start: int
    ) -> None:
        """
        Move all selected clips together when primary clip is dragged.
        Uses MacroCommand for atomic batch operation.

        Args:
            primary_clip_id: The clip being actively dragged
            old_start: Original position of primary clip
            new_start: New position of primary clip
        """
        if self._timeline is None:
            return

        delta = new_start - old_start
        sub_commands = []

        # Collect move commands for all selected clips
        for clip_id in self._selection.selected_clip_ids:
            if clip_id == primary_clip_id:
                continue  # Primary clip already moved

            clip = self._timeline.get_clip_by_id(clip_id)
            if clip is None:
                continue

            clip_old_start = clip.timeline_start
            clip_new_start = clip_old_start + delta

            # Validate position
            if not self._validate_move_position(clip_id, clip.track_id, clip_new_start):
                logger.warning(f"Multi-move rejected for {clip_id}: overlap detected")
                continue

            # Create move command for this clip
            cmd = MoveClipCommand.create(
                track_id=clip.track_id,
                clip_id=clip_id,
                new_start=clip_new_start,
                old_start=clip_old_start
            )
            sub_commands.append(cmd)

        if not sub_commands:
            return

        # Execute as single atomic MacroCommand via _execute_command
        # BUG-03 fix: Use _execute_command for proper signal emission and incremental update
        macro = MacroCommand(
            command_id=f"multi_move_{primary_clip_id[:8]}",
            label="Move selected clips",
            sub_commands=tuple(sub_commands),
        )
        self._execute_command(macro)

    # ==================== Paint ====================

    def _on_viewport_scrolled(self, _value: int) -> None:
        """滚动后全量重绘，覆盖 bitblt 平移造成的固定表头列残影。"""
        self.viewport().update()

    def paintEvent(self, event: QPaintEvent) -> None:
        """Override paintEvent to draw fixed header column overlay."""
        super().paintEvent(event)
        # 局部更新（播放头移动）时跳过与表头列不相交的重绘，
        # 避免 25fps 播放下每次 playhead tick 都重建 painter/字体。
        vp = self.viewport()
        xf = self.transform()
        col_w = int(TRACK_HEADER_WIDTH * xf.m11())
        if col_w > 0 and not event.rect().intersects(QRect(0, 0, col_w, vp.height())):
            return
        self._paint_fixed_header_column()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """Draw the playhead as a scene-space overlay（播放头覆盖层）.

        播放头视觉在这里而不是 PlayheadItem.paint：覆盖层不参与场景
        索引/脏区计算，移动播放头只需 viewport().update 窄竖条（见
        _invalidate_playhead_strips），所有 clip/ruler item 都不必重绘。
        PlayheadItem 本体不在场景中，仅作几何与命中测试参照。
        """
        playhead = self._playhead_item
        if playhead is None:
            return
        strip = playhead.sceneBoundingRect()
        if strip.isEmpty() or not rect.intersects(strip):
            return
        painter.save()
        painter.translate(playhead.scenePos())
        playhead.paint(painter, None, None)
        painter.restore()

    def _invalidate_playhead_strips(
        self, old_scene_x: float, new_scene_x: float
    ) -> None:
        """Repaint only the playhead's old/new vertical strips（视口坐标）.

        覆盖层视觉不自动跟随数据变化，移动/变形后必须显式刷新对应竖条。
        """
        playhead = self._playhead_item
        vp = self.viewport()
        if playhead is None or vp is None:
            return
        half_w = 16.0
        top = playhead.y()
        height = playhead.boundingRect().height()
        for x in {old_scene_x, new_scene_x}:
            scene_rect = QRectF(x - half_w, top, half_w * 2.0, height)
            vp.update(self.mapFromScene(scene_rect).boundingRect())

    def _refresh_playhead_visual(self) -> None:
        """Repaint the playhead strip after a visual-state change."""
        playhead = self._playhead_item
        if playhead is None:
            return
        self._invalidate_playhead_strips(playhead.scenePos().x(), playhead.scenePos().x())

    def _paint_fixed_header_column(self) -> None:
        """Paint the track header column as a fixed overlay on the viewport.

        This draws an opaque background + re-renders header items at pinned
        viewport positions so the header column never scrolls horizontally.
        """
        vp = self.viewport()
        painter = QPainter(vp)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # View scale factors (zoom)
        xf = self.transform()
        sx = xf.m11()
        sy = xf.m22()
        col_w = int(TRACK_HEADER_WIDTH * sx)

        # 1. Opaque header column background
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(get_color("track_header_bg"))))
        painter.drawRect(0, 0, col_w, vp.height())

        # 2. Top-left timecode panel is a dedicated child widget
        #    (self._corner_timecode_widget) — do NOT paint it here.
        ruler_h = int(RULER_HEIGHT * sy)
        painter.setPen(QPen(QColor("#0A0A0C"), 1))
        painter.drawLine(col_w - 1, 0, col_w - 1, vp.height())

        if self._timeline is None:
            # Keep the empty project state structurally recognizable: the
            # model has no real tracks yet, but the user can still see the
            # normal V/A workspace and understand where media will appear.
            track_top = ruler_h
            track_bottom = vp.height()
            split_y = track_top + max(0, track_bottom - track_top) // 2
            divider_pen = QPen(QColor(get_color("track_header_divider")), 1)
            painter.setPen(divider_pen)
            painter.drawLine(0, split_y, col_w, split_y)

            label_font = QFont("SF Mono, Menlo, Consolas, Courier New, monospace", 12)
            label_font.setStyleHint(QFont.StyleHint.Monospace)
            label_font.setBold(True)
            painter.setFont(label_font)
            painter.setPen(QPen(QColor(get_color("text_muted"))))
            painter.drawText(
                QRect(12, track_top, max(0, col_w - 24), max(0, split_y - track_top)),
                Qt.AlignmentFlag.AlignVCenter,
                "V1",
            )
            painter.drawText(
                QRect(12, split_y, max(0, col_w - 24), max(0, track_bottom - split_y)),
                Qt.AlignmentFlag.AlignVCenter,
                "A1",
            )
            painter.end()
            return

        if not self._track_headers:
            painter.end()
            return

        # 3. Re-render each header item at its pinned viewport position.
        # Guard against headers whose underlying C++ QGraphicsObject was just
        # removed by an incremental update interleaved with this paint —
        # accessing a deleted item (y()/boundingRect()/paint()) raises
        # RuntimeError and, in release builds without the except, segfaults.
        option = QStyleOptionGraphicsItem()
        painter.save()
        painter.setClipRect(QRect(0, ruler_h, col_w, max(0, vp.height() - ruler_h)))
        for header in list(self._track_headers.values()):
            try:
                # scene() returns None once the item has been removed from the
                # scene and its C++ object torn down — skip stale wrappers.
                if header.scene() is not self._scene:
                    continue
                vp_y = self.mapFromScene(QPointF(0, header.y())).y()
                option.exposedRect = header.boundingRect()
                painter.save()
                painter.translate(0, vp_y)
                painter.scale(sx, sy)
                header.paint(painter, option, vp)
                painter.restore()
            except RuntimeError:
                # Wrapped C/C++ object deleted mid-iteration — skip safely.
                continue
        painter.restore()

        painter.end()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        """
        Draw timeline grid background with V/A area split and alternating track colors.

        Layout:
        - Video area (top): V3, V2, V1 (V1 near splitter)
        - Splitter (middle): draggable divider
        - Audio area (bottom): A1, A2, A3 (A1 near splitter)

        Reference: DaVinci-like V/A area split design
        """
        super().drawBackground(painter, rect)

        if self._timeline is None:
            self._draw_empty_timeline_background(painter, rect)
            return

        self._configure_layout_engine()
        engine = self._layout_engine
        area = engine.area_layout()

        painter.setPen(QPen(Qt.PenStyle.NoPen))

        # Draw video area background (P1: DaVinci-like slightly warm gray)
        va = area.video_area
        if va.isValid():
            painter.setBrush(QBrush(TRACK_AREA_VIDEO_BG))
            painter.drawRect(QRectF(
                max(TRACK_HEADER_WIDTH, rect.left()),
                max(va.top(), rect.top()),
                min(rect.right(), va.right()) - max(TRACK_HEADER_WIDTH, rect.left()),
                min(rect.bottom(), va.bottom()) - max(va.top(), rect.top()),
            ))

        # Draw audio area background (P1: DaVinci-like slightly cooler gray)
        aa = area.audio_area
        if aa.isValid():
            painter.setBrush(QBrush(TRACK_AREA_AUDIO_BG))
            painter.drawRect(QRectF(
                max(TRACK_HEADER_WIDTH, rect.left()),
                max(aa.top(), rect.top()),
                min(rect.right(), aa.right()) - max(TRACK_HEADER_WIDTH, rect.left()),
                min(rect.bottom(), aa.bottom()) - max(aa.top(), rect.top()),
            ))

        # Draw locked track overlays (only locked tracks need per-track rects)
        all_ordered = engine.all_ordered_tracks()
        for vis_idx, tinfo in enumerate(all_ordered):
            track_top = engine.track_top(tinfo.track_id)
            track_h = engine.track_height(tinfo.track_id)

            track_rect = QRectF(
                TRACK_HEADER_WIDTH,
                track_top,
                rect.width(),
                track_h,
            )
            if not rect.intersects(track_rect):
                continue

            # Only draw for locked tracks (semi-transparent overlay)
            track_data = None
            for t in self._timeline.video_tracks + self._timeline.audio_tracks:
                if t.track_id == tinfo.track_id:
                    track_data = t
                    break
            if track_data and track_data.is_locked:
                painter.setBrush(QBrush(QColor(0, 0, 0, 100)))
                painter.setPen(QPen(Qt.PenStyle.NoPen))
                painter.drawRect(track_rect)

        # Keep the V/A splitter's generous hit target, while making its visible
        # boundaries continuous with the two dark divider lines in the pinned
        # track-header column.  This leaves the panel visually quiet without
        # making the V/A resize affordance harder to use.
        sr = area.splitter_rect
        if sr.isValid() and rect.intersects(sr):
            # The interior blends into the timeline canvas; only its two
            # boundaries should be visible.
            painter.setBrush(QBrush(TRACK_AREA_COLOR))
            painter.drawRect(QRectF(
                max(TRACK_HEADER_WIDTH, rect.left()),
                sr.top(),
                min(rect.right(), sr.right()) - max(TRACK_HEADER_WIDTH, rect.left()),
                sr.height(),
            ))

            # Match TrackHeaderItem's top/bottom separators exactly.  The
            # lines stop at the content boundary because the fixed header
            # overlay paints the matching segments in the header column.
            divider_pen = QPen(QColor(get_color("track_header_divider")), 1)
            painter.setPen(divider_pen)
            painter.drawLine(
                QPointF(TRACK_HEADER_WIDTH, sr.top() - 0.5),
                QPointF(rect.right(), sr.top() - 0.5),
            )
            painter.drawLine(
                QPointF(TRACK_HEADER_WIDTH, sr.bottom() + 0.5),
                QPointF(rect.right(), sr.bottom() + 0.5),
            )
            painter.setPen(QPen(Qt.PenStyle.NoPen))

        # Draw vertical time markers
        self._draw_vertical_time_markers(painter, rect)

    def _draw_empty_timeline_background(self, painter: QPainter, rect: QRectF) -> None:
        """Draw V/A lanes and an instructional empty-state message."""
        content_left = max(float(TRACK_HEADER_WIDTH), rect.left())
        content_right = rect.right()
        if content_right <= content_left:
            return

        viewport_height = max(1, self.viewport().height())
        track_top = float(RULER_HEIGHT)
        track_bottom = max(float(rect.bottom()), float(viewport_height))
        split_y = track_top + max(0.0, track_bottom - track_top) * 0.5

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(TRACK_AREA_VIDEO_BG))
        painter.drawRect(
            QRectF(content_left, track_top, content_right - content_left, split_y - track_top)
        )
        painter.setBrush(QBrush(TRACK_AREA_AUDIO_BG))
        painter.drawRect(
            QRectF(content_left, split_y, content_right - content_left, track_bottom - split_y)
        )

        divider_pen = QPen(QColor(get_color("track_header_divider")), 1)
        painter.setPen(divider_pen)
        painter.drawLine(QPointF(content_left, split_y), QPointF(content_right, split_y))

        self._draw_vertical_time_markers(
            painter, QRectF(content_left, track_top, content_right - content_left, track_bottom - track_top)
        )

        visible_rect = self.mapToScene(self.viewport().rect()).boundingRect()
        center_x = min(max(visible_rect.center().x(), content_left + 220.0), content_right - 220.0)
        center_y = track_top + max(0.0, track_bottom - track_top) * 0.5
        message_rect = QRectF(center_x - 220.0, center_y - 42.0, 440.0, 84.0)

        title_font = QFont("SF Pro Display, Helvetica, Arial, sans-serif", 15)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QPen(QColor(get_color("text"))))
        painter.drawText(
            message_rect.adjusted(0, 0, 0, -22),
            Qt.AlignmentFlag.AlignCenter,
            I18N.tr("timeline.empty_title", "尚未创建时间线"),
        )

        hint_font = QFont("SF Pro Display, Helvetica, Arial, sans-serif", 11)
        painter.setFont(hint_font)
        painter.setPen(QPen(QColor(get_color("text_muted"))))
        painter.drawText(
            message_rect.adjusted(0, 28, 0, 0),
            Qt.AlignmentFlag.AlignCenter,
            I18N.tr("timeline.empty_hint", "创建时间线或将素材拖到此处开始编辑"),
        )

    def _draw_vertical_time_markers(self, painter: QPainter, rect: QRectF) -> None:
        """Align the track grid with the ruler's labeled timecode ticks."""
        if self._ruler_item is None:
            return
        interval = self._ruler_item.major_tick_interval
        first = max(0, int((rect.left() - TRACK_HEADER_WIDTH) / self._pixels_per_frame))
        last = math.ceil((rect.right() - TRACK_HEADER_WIDTH) / self._pixels_per_frame)
        painter.setPen(QPen(QColor(90, 95, 105, 42), 1))
        for frame in range((first // interval) * interval, last + 1, interval):
            x = int(TRACK_HEADER_WIDTH + frame * self._pixels_per_frame)
            painter.drawLine(x, int(rect.top()), x, int(rect.bottom()))

    # ==================== Clear ====================

    def show_provisional_timeline(self) -> TimelineSequence:
        """Show an empty V1/A1 layout without publishing a project timeline.

        This gives a brand-new project real track geometry for drag previews
        and hit testing while keeping the project model timeline-free until
        the first media drop is committed.
        """
        timeline = self._create_default_timeline()
        if timeline is None:  # pragma: no cover - defensive; helper is local
            raise RuntimeError("Failed to create provisional timeline")
        self.load_timeline(timeline)
        self._provisional_timeline = True
        return timeline

    def has_provisional_timeline(self) -> bool:
        """Return whether the current V1/A1 layout is UI-only."""
        return self._provisional_timeline

    def clear(self) -> None:
        """Clear the timeline view."""
        if not self._interaction.is_idle:
            self._cancel_current_interaction()
        self._hide_trim_monitor()
        self._clear_items()
        self._remove_persistent_items()
        self._timeline = None
        self._provisional_timeline = False
        self._runtime_duration_frames = 0
        self._playhead_frame = 0
        self._corner_timecode_text = "00:00:00:00"
        self._corner_timecode_widget.setText("00:00:00:00")
        self._timeline_start_reset_pending = False
        self._selection = TimelineSelection()
        self._timeline_command_stacks.clear()
        self._timeline_history_snapshots.clear()
        self._project_transaction_sequences.clear()
        self._source_patch_routes.clear()
        # B07/B09：clear 同步丢弃 presentation 未知字段缓存（完整卸载，
        # 不跨 clear 继承旧 payload 的扩展键）
        self._presentation_extras = {}
        self._presentation_route_extras = {}
        self._selected_edit_edges.clear()
        self._independent_selection = False
        self._target_track_id = None
        self._target_audio_track_id = None
        self._retime_control_clip_ids.clear()
        self.set_position_lock_enabled(False)
        self._command_stack = CommandStack()
        self._initialize_scene()
        self.selection_changed.emit(self._selection)

    # ==================== Track Management ====================

    def _on_track_mute_changed(self, track_id: str, mute: bool) -> None:
        """Handle track mute state change."""
        if self._timeline is None:
            return

        cmd = SetTrackMuteCommand.create(track_id, mute)
        self._execute_command(cmd)
        logger.debug(f"Track {track_id} mute changed to {mute}")

    def _on_track_solo_changed(self, track_id: str, solo: bool) -> None:
        """Handle track solo state change."""
        if self._timeline is None:
            return

        cmd = SetTrackSoloCommand.create(track_id, solo)
        self._execute_command(cmd)
        logger.debug(f"Track {track_id} solo changed to {solo}")

    def _on_track_lock_changed(self, track_id: str, lock: bool) -> None:
        """Handle track lock state change."""
        if self._timeline is None:
            return

        cmd = SetTrackLockCommand.create(track_id, lock)
        self._execute_command(cmd)

        # Update all clip items on this track
        track = self._timeline.get_track_by_id(track_id)
        if track:
            for clip in track.clips:
                clip_item = self._items_map.get(clip.clip_id)
                if clip_item and isinstance(clip_item, ClipItem):
                    clip_item.set_track_locked(lock)
            # AUD-21102: 转场卡与片段共用锁轨语义
            for trans in getattr(track, "transitions", ()) or ():
                trans_item = self._items_map.get(trans.transition_id)
                if isinstance(trans_item, TransitionItem):
                    trans_item.set_track_locked(lock)

        logger.debug(f"Track {track_id} lock changed to {lock}")

    def _on_track_sync_lock_changed(self, track_id: str, sync_lock: bool) -> None:
        """Handle track sync lock change (P1-05).

        与编辑锁不同：同步锁不影响 clip 可编辑性，只影响波纹/提取操作
        是否带动本轨，因此无需刷新 clip items 的锁定视觉。
        """
        if self._timeline is None:
            return

        from src.features.edit.core.commands import SetTrackSyncLockCommand

        cmd = SetTrackSyncLockCommand.create(track_id, sync_lock)
        self._execute_command(cmd)
        logger.debug(f"Track {track_id} sync lock changed to {sync_lock}")

    def _on_track_volume_changed(self, track_id: str, volume: float) -> None:
        """Handle track volume change."""
        if self._timeline is None:
            return

        cmd = SetTrackVolumeCommand.create(track_id, volume)
        self._execute_command(cmd)
        logger.debug(f"Track {track_id} volume changed to {volume:.2f}")

    def set_track_waveform_gain_preview(self, track_id: str, gain: float) -> None:
        """实时预览轨道音量对该轨波形显示的缩放（W-gain）。

        纯视觉更新：ClipItem 等比缩放缓存的波形 path，不重建路径、不
        重提取峰值。提交后的最终一致性由增量更新的 track_changes 路径
        保证（同样调用本方法），因此拖动/undo/mixer 三条路径全部覆盖。
        """
        for item in list(self._items_map.values()):
            if isinstance(item, ClipItem) and item.track_id == track_id:
                item.set_waveform_gain(gain)

    def _on_track_visibility_changed(self, track_id: str, visible: bool) -> None:
        """Handle track visibility change."""
        if self._timeline is None:
            return

        from src.features.edit.core.commands import SetTrackVisibilityCommand
        cmd = SetTrackVisibilityCommand.create(track_id, visible)
        self._execute_command(cmd)

        # Show/hide clip items on this track
        track = self._timeline.get_track_by_id(track_id)
        if track:
            for clip in track.clips:
                clip_item = self._items_map.get(clip.clip_id)
                if clip_item and isinstance(clip_item, ClipItem):
                    clip_item.setVisible(visible)

        # Update header state
        header = self._track_headers.get(track_id)
        if header:
            header.set_visible(visible)

        logger.debug(f"Track {track_id} visibility changed to {visible}")

    def _on_track_rename_requested(self, track_id: str) -> None:
        """Handle track rename request."""
        from PyQt6.QtWidgets import QInputDialog

        if self._timeline is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        # Show input dialog
        current_name = track.name
        new_name, accepted = QInputDialog.getText(
            self,
            "Rename Track",
            "Enter new track name:",
            text=current_name
        )

        if accepted and new_name and new_name != current_name:
            cmd = RenameTrackCommand.create(track_id, new_name)
            self._execute_command(cmd)
            logger.debug(f"Track {track_id} renamed to '{new_name}'")

    def _on_track_delete_requested(self, track_id: str) -> None:
        """Handle track delete request."""
        from PyQt6.QtWidgets import QMessageBox

        if self._timeline is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        # INT-11：内容统计含字幕 cue 与转场——审计实测只查 clips，含
        # 1 cue 的字幕轨不询问直接删除（A/V 轨却询问）。
        subtitle_count = len(getattr(track, "subtitle_clips", ()) or ())
        transition_count = len(getattr(track, "transitions", ()) or ())
        clip_count = len(track.clips)
        if clip_count or subtitle_count or transition_count:
            parts = []
            if clip_count:
                parts.append(f"{clip_count} clips")
            if subtitle_count:
                parts.append(f"{subtitle_count} subtitle cues")
            if transition_count:
                parts.append(f"{transition_count} transitions")
            summary = " + ".join(parts)
            reply = QMessageBox.question(
                self,
                "Delete Track",
                f"Track '{track.name}' contains {summary}.\n"
                "Are you sure you want to delete this track?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        cmd = DeleteTrackCommand.create(track_id)
        self._execute_command(cmd)
        logger.debug(f"Track {track_id} deleted")

    def _on_add_track_requested(self, track_type: str, position: str, track_index: int) -> None:
        """Handle add track request.

        The position index is resolved per-type (video tracks index among
        video tracks, audio among audio, subtitle among subtitle), so adding
        a subtitle track from a video track's context menu still works.
        """
        if self._timeline is None:
            return

        # Calculate position index relative to the target type's own track list
        if track_type == "video":
            type_count = len(self._timeline.video_tracks)
        elif track_type == "audio":
            type_count = len(self._timeline.audio_tracks)
        else:  # subtitle
            type_count = len(self._timeline.subtitle_tracks)

        if position == "above":
            # Clamp the requested index to the target type's range. For
            # cross-type adds (e.g. subtitle from a video track context menu)
            # the global track_index doesn't map directly, so we just append.
            insert_position = max(0, min(track_index, type_count)) if track_index < type_count + len(self._timeline.video_tracks) + len(self._timeline.audio_tracks) else type_count
            # Simpler + safer: append when the source track type differs
            insert_position = min(insert_position, type_count)
        else:  # below
            insert_position = type_count

        cmd = AddTrackCommand.create(track_type, None, insert_position)
        self._execute_command(cmd)
        logger.debug(f"Added {track_type} track at position {insert_position}")

    def _show_empty_area_track_menu(self, global_pos: QPoint, scene_pos: QPointF) -> None:
        """P10.3: Show a 'create track' context menu when right-clicking the
        empty area of the track-header column (below all existing headers).

        Lets users create video/audio/subtitle tracks without needing an
        existing track to right-click on first.
        """
        from PyQt6.QtWidgets import QMenu
        from src.shared.i18n.i18n import I18N

        menu = QMenu()
        add_v = menu.addAction(I18N.tr("timeline.track.type_video", "Add Video Track"))
        add_a = menu.addAction(I18N.tr("timeline.track.type_audio", "Add Audio Track"))
        add_s = menu.addAction(I18N.tr("timeline.track.type_subtitle", "Add Subtitle Track"))
        action = menu.exec(global_pos)

        if action == add_v:
            self._on_add_track_requested("video", "below", len(self._timeline.video_tracks) if self._timeline else 0)
        elif action == add_a:
            self._on_add_track_requested("audio", "below", len(self._timeline.audio_tracks) if self._timeline else 0)
        elif action == add_s:
            # Subtitle tracks append at the end of the subtitle group
            sub_count = len(self._timeline.subtitle_tracks) if self._timeline else 0
            cmd = AddTrackCommand.create("subtitle", None, sub_count)
            self._execute_command(cmd)

    def _on_move_track_requested(self, track_id: str, direction: str) -> None:
        """Handle move track request."""
        if self._timeline is None:
            return

        cmd = MoveTrackCommand.create(track_id, direction)
        self._execute_command(cmd)
        logger.debug(f"Track {track_id} moved {direction}")

    def _on_track_height_changed(self, track_id: str, new_height: int) -> None:
        """Handle track height change — sync with layout engine."""
        if self._timeline is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        # SUB-P1-06: subtitle tracks are resizable within their compact range
        # (MIN_SUBTITLE_TRACK_HEIGHT..MAX_SUBTITLE_TRACK_HEIGHT); clamp instead
        # of forcing DEFAULT. The engine also clamps, but we keep the stored
        # state consistent for save/restore.
        if track.track_type == "subtitle":
            new_height = max(MIN_SUBTITLE_TRACK_HEIGHT,
                             min(MAX_SUBTITLE_TRACK_HEIGHT, int(new_height)))

        # Store height in layout state (persists across reloads)
        self._layout_state.track_heights[track_id] = new_height
        self._layout_engine.set_track_height(track_id, new_height)

        # Update clip items on this track. Subtitle clips use their own
        # compact item type and must receive height changes as well.
        if track.track_type == "subtitle":
            for sub in track.subtitle_clips:
                sub_item = self._items_map.get(sub.clip_id)
                if sub_item and isinstance(sub_item, SubtitleClipItem):
                    sub_item.set_track_height(new_height)
        else:
            for clip in track.clips:
                clip_item = self._items_map.get(clip.clip_id)
                if clip_item and isinstance(clip_item, ClipItem):
                    clip_item.set_track_height(new_height)

        # Update track header height
        header = self._track_headers.get(track_id)
        if header:
            header.set_track_height(new_height)

        # Re-layout all tracks (engine handles cumulative positioning)
        self._layout_track_items()

        logger.debug(f"Track {track_id} height changed to {new_height}")

    def set_track_height_preset(self, preset: str) -> None:
        """Set all track heights to a preset: 'small', 'medium', 'large', 'default'."""
        if self._timeline is None:
            return

        # P10.3: include subtitle track heights in presets
        heights = {
            "small": {"video": 36, "audio": 28, "subtitle": 28},
            "medium": {"video": 58, "audio": 46, "subtitle": 36},
            "large": {"video": 90, "audio": 70, "subtitle": 50},
            "default": {
                "video": EDIT_SIZES["track_h_video"],
                "audio": EDIT_SIZES["track_h_audio"],
                "subtitle": 40,
            },
        }

        h = heights.get(preset, heights["default"])
        # P10.3: iterate all track types including subtitles.
        # SUB-P1-06: subtitle tracks now honor the preset height (28/36/50)
        # instead of being forced back to DEFAULT_SUBTITLE_TRACK_HEIGHT.
        for track in (self._timeline.video_tracks
                      + self._timeline.audio_tracks
                      + self._timeline.subtitle_tracks):
            track_type = track.track_type
            new_h = h.get(track_type, h["video"])
            self._layout_state.track_heights[track.track_id] = new_h
            self._layout_engine.set_track_height(track.track_id, new_h)
            header = self._track_headers.get(track.track_id)
            if header:
                header.set_track_height(new_h)

        self._layout_track_items()
        self.viewport().update()

    def reset_splitter_ratio(self) -> None:
        """Reset V/A area splitter to 50/50."""
        self._layout_state.video_area_ratio = 0.5
        self._layout_engine.set_splitter_ratio(0.5)
        self._align_video_stack_to_splitter()
        self._layout_track_items()
        self.viewport().update()

    def _reposition_tracks_after_index(self, start_index: int) -> None:
        """
        Reposition all tracks after a given index (UI-08).

        When track height changes, all subsequent tracks need their y
        coordinates recalculated to avoid overlapping.

        Args:
            start_index: Index of the track that changed (reposition from this index onward)
        """
        if self._timeline is None:
            return

        self._layout_track_items()

        logger.debug(f"Repositioned tracks from index {start_index}")

    def _on_target_track_changed(self, track_id: str) -> None:
        """
        Handle target track change from track header click (UI-09).

        Updates the target track ID and visual indicator on all headers.

        Args:
            track_id: New target track ID
        """
        if self._timeline is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        if track.track_type == "audio":
            previous_id = self._target_audio_track_id
            self._target_audio_track_id = track_id
        else:
            previous_id = self._target_track_id
            self._target_track_id = track_id

        # Clear target indicator from previous target of the same media type
        if previous_id and previous_id != track_id:
            old_header = self._track_headers.get(previous_id)
            if old_header:
                old_header._is_target = False
                old_header.update()

        # Set target indicator on new target
        new_header = self._track_headers.get(track_id)
        if new_header:
            new_header._is_target = True
            new_header.update()

        for header in self._track_headers.values():
            if header.track_type in {"video", "audio"}:
                header.set_source_patch_enabled(
                    self.source_patch_track_id(header.track_type) == header.track_id)

        logger.debug(f"Target {track.track_type} track changed to {track_id}")

    def _update_track_clip_states(self, track_id: str) -> None:
        """Update all clip items on a track with current track state."""
        if self._timeline is None:
            return

        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return

        # Update clip items with track state
        for clip in track.clips:
            clip_item = self._items_map.get(clip.clip_id)
            if clip_item and isinstance(clip_item, ClipItem):
                clip_item.set_track_locked(track.is_locked)
                # RI-06/R17：clip 自身 enabled 与轨道 mute/visibility 分开。
                # 此前用 track.is_enabled and not track.is_muted 覆盖 Item
                # 状态，把 clip own enabled 污染成轨道 effective 状态，
                # Enable/Disable 菜单随之读错。
                clip_item.set_enabled(clip.is_enabled)
        # AUD-21102: 转场卡与片段共用锁轨语义
        for trans in getattr(track, "transitions", ()) or ():
            trans_item = self._items_map.get(trans.transition_id)
            if isinstance(trans_item, TransitionItem):
                trans_item.set_track_locked(track.is_locked)

    # ==================== Marker Signal Handlers (Phase 5.1) ====================

    def _show_marker_context_menu(self, marker_id: str, global_pos: QPoint) -> None:
        """Show marker actions at the marker's screen position."""
        if self._timeline is None or marker_id not in self._marker_items:
            return

        from PyQt6.QtWidgets import QMenu
        from src.shared.i18n.i18n import I18N

        menu = QMenu(self)
        delete_action = menu.addAction(
            I18N.tr("edit.marker_delete", "Delete Marker")
        )
        selected = menu.exec(global_pos)
        if selected == delete_action:
            self._execute_command(DeleteMarkerCommand.create(marker_id))
            logger.info("Deleted marker %s from context menu", marker_id)

    def _on_marker_clicked(self, marker_id: str) -> None:
        """
        Handle marker click.

        Args:
            marker_id: ID of clicked marker
        """
        # Deselect all other markers
        for m_id, m_item in self._marker_items.items():
            m_item.set_selected(m_id == marker_id)

        logger.debug(f"Marker clicked: {marker_id}")

    def _on_marker_moved(self, marker_id: str, new_frame: int) -> None:
        """
        Handle marker drag (live preview).

        Args:
            marker_id: ID of moved marker
            new_frame: New frame position
        """
        # Live preview - marker item already updated visually
        logger.debug(f"Marker {marker_id} moved preview to frame {new_frame}")

    def _on_marker_move_finished(self, marker_id: str, old_frame: int, new_frame: int) -> None:
        """
        Handle marker move finished (execute command).

        Args:
            marker_id: ID of moved marker
            old_frame: Original frame position
            new_frame: New frame position
        """
        if self._timeline is None:
            return

        # Execute move command
        cmd = MoveMarkerCommand.create(marker_id, new_frame)
        self._execute_command(cmd)
        logger.debug(f"Marker {marker_id} moved from {old_frame} to {new_frame}")

    def _on_marker_double_clicked(self, marker_id: str) -> None:
        """
        Handle marker double-click for editing.

        Args:
            marker_id: ID of marker to edit
        """
        if self._timeline is None:
            return

        # Find marker data
        marker = None
        for m in self._timeline.markers:
            if m.marker_id == marker_id:
                marker = m
                break

        if marker is None:
            return

        # Show edit dialog
        from PyQt6.QtWidgets import (
            QDialog,
            QVBoxLayout,
            QLineEdit,
            QComboBox,
            QDialogButtonBox,
            QLabel,
        )
        from src.shared.i18n.i18n import I18N

        dialog = QDialog(self)
        dialog.setWindowTitle(I18N.tr("edit.marker_title", "Edit Marker"))
        dialog.setMinimumWidth(300)

        layout = QVBoxLayout(dialog)

        # Name input (stored, but intentionally not rendered on the timeline)
        layout.addWidget(QLabel(I18N.tr("edit.marker_name_label", "Marker name")))
        name_edit = QLineEdit()
        name_edit.setText(marker.name)
        name_edit.setPlaceholderText(I18N.tr("edit.marker_name_placeholder", "Marker name"))
        layout.addWidget(name_edit)

        # Color combo box
        layout.addWidget(QLabel(I18N.tr("edit.marker_color_label", "Marker color")))
        color_combo = QComboBox()
        predefined_colors = [
            (I18N.tr("edit.marker_color_red", "Red"), MUTED_MARKER_COLORS[0]),
            (I18N.tr("edit.marker_color_orange", "Orange"), MUTED_MARKER_COLORS[1]),
            (I18N.tr("edit.marker_color_yellow", "Yellow"), MUTED_MARKER_COLORS[2]),
            (I18N.tr("edit.marker_color_green", "Green"), MUTED_MARKER_COLORS[3]),
            (I18N.tr("edit.marker_color_cyan", "Cyan"), MUTED_MARKER_COLORS[4]),
            (I18N.tr("edit.marker_color_blue", "Blue"), MUTED_MARKER_COLORS[5]),
            (I18N.tr("edit.marker_color_purple", "Purple"), MUTED_MARKER_COLORS[6]),
            (I18N.tr("edit.marker_color_pink", "Pink"), MUTED_MARKER_COLORS[7]),
        ]
        for color_name, color_hex in predefined_colors:
            color_combo.addItem(color_name, color_hex)

        # Keep an existing custom color selectable instead of silently
        # replacing it with the first preset when the dialog is confirmed.
        if marker.color not in {color_hex for _, color_hex in predefined_colors}:
            color_combo.addItem(
                I18N.tr("edit.marker_color_custom", "Custom ({})").format(marker.color),
                marker.color,
            )

        # Set current color
        for i, (_, color_hex) in enumerate(predefined_colors):
            if color_hex == marker.color:
                color_combo.setCurrentIndex(i)
                break

        layout.addWidget(color_combo)

        # C5: marker kind (standard | chapter)
        layout.addWidget(QLabel(I18N.tr("edit.marker_kind_label", "Marker type")))
        kind_combo = QComboBox()
        kind_combo.addItem(I18N.tr("edit.marker_kind_standard", "Standard"), "standard")
        kind_combo.addItem(I18N.tr("edit.marker_kind_chapter", "Chapter"), "chapter")
        kind_combo.setCurrentIndex(
            1 if getattr(marker, "kind", "standard") == "chapter" else 0
        )
        layout.addWidget(kind_combo)

        # Comment input (optional)
        layout.addWidget(QLabel(I18N.tr("edit.marker_comment_label", "Comment")))
        comment_edit = QLineEdit()
        comment_edit.setText(marker.comment)
        comment_edit.setPlaceholderText(I18N.tr("edit.marker_comment_placeholder", "Comment (optional)"))
        layout.addWidget(comment_edit)

        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        # Show dialog
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_name = name_edit.text()
            new_color = color_combo.currentData()
            new_comment = comment_edit.text()
            new_kind = kind_combo.currentData() or "standard"

            # Only create command if something changed
            if (new_name != marker.name or new_color != marker.color
                    or new_comment != marker.comment
                    or new_kind != getattr(marker, "kind", "standard")):
                cmd = EditMarkerCommand.create(
                    marker_id, new_name, new_color, new_comment,
                    new_kind=new_kind,
                )
                self._execute_command(cmd)
                logger.debug(f"Marker {marker_id} edited: name={new_name}, color={new_color}")

    def add_marker_at_playhead(self) -> None:
        """
        Add a new marker at current playhead position.

        A single selected clip scopes the marker to that clip when the
        playhead is inside it; otherwise the marker is timeline-scoped and is
        rendered on the ruler. This keeps the same M shortcut useful in both
        contexts while preserving the scope in the project file.
        """
        if self._timeline is None:
            return

        clip_id: str | None = None
        track_id: str | None = None
        selected_clip_ids = self._selection.selected_clip_ids
        if len(selected_clip_ids) == 2:
            first = self._timeline.get_clip_by_id(selected_clip_ids[0])
            partner = self._get_reciprocal_linked_clip(first)
            if partner is not None and partner.clip_id in selected_clip_ids:
                primary = self._selection.primary_item
                selected_clip_ids = [primary.item_id if primary is not None else first.clip_id]
        if len(selected_clip_ids) == 1:
            selected_clip = self._timeline.get_clip_by_id(selected_clip_ids[0])
            if (
                selected_clip is not None
                and selected_clip.timeline_start <= self._playhead_frame
                < selected_clip.timeline_end
            ):
                clip_id = selected_clip.clip_id
                track_id = selected_clip.track_id

        # Generate a stable but varied low-saturation color for this marker.
        marker_id = str(uuid.uuid4())
        marker_color = MUTED_MARKER_COLORS[
            int(marker_id[:8], 16) % len(MUTED_MARKER_COLORS)
        ]

        # Create new marker
        marker = TimelineMarker(
            marker_id=marker_id,
            frame=self._playhead_frame,
            name=f"Marker {len(self._timeline.markers) + 1}",
            color=marker_color,
            comment="",
            clip_id=clip_id,
            track_id=track_id,
        )

        # Execute add command
        cmd = AddMarkerCommand.create(marker)
        self._execute_command(cmd)
        logger.info(f"Added marker at frame {self._playhead_frame}")

    def add_chapter_marker_at_playhead(self) -> None:
        """Add a chapter marker at the playhead (C5, Alt+M).

        Chapters are always timeline-scoped (``clip_id=None``) so they can be
        exported as YouTube chapter text / FFMetadata; a clip-scoped marker
        would follow the clip and break the chapter timeline.
        """
        if self._timeline is None:
            return

        marker_id = str(uuid.uuid4())
        marker = TimelineMarker(
            marker_id=marker_id,
            frame=self._playhead_frame,
            name=f"Chapter {sum(1 for m in self._timeline.markers if getattr(m, 'kind', 'standard') == 'chapter') + 1}",
            color=CHAPTER_MARKER_COLOR,
            comment="",
            clip_id=None,
            track_id=None,
            kind="chapter",
        )
        cmd = AddMarkerCommand.create(marker)
        self._execute_command(cmd)
        logger.info(f"Added chapter marker at frame {self._playhead_frame}")

    def jump_to_next_marker(self) -> None:
        """Jump playhead to the next marker after current position (Shift+M)."""
        if self._timeline is None or not self._timeline.markers:
            return

        current = self._playhead_frame
        # Find first marker with frame > current
        next_markers = [m for m in self._timeline.markers if m.frame > current]
        if next_markers:
            target = min(next_markers, key=lambda m: m.frame)
            self._move_playhead_to(target.frame)
        else:
            # Wrap to first marker
            first = min(self._timeline.markers, key=lambda m: m.frame)
            self._move_playhead_to(first.frame)

    def jump_to_prev_marker(self) -> None:
        """Jump playhead to the previous marker before current position (Ctrl+Shift+M)."""
        if self._timeline is None or not self._timeline.markers:
            return

        current = self._playhead_frame
        # Find last marker with frame < current
        prev_markers = [m for m in self._timeline.markers if m.frame < current]
        if prev_markers:
            target = max(prev_markers, key=lambda m: m.frame)
            self._move_playhead_to(target.frame)
        else:
            # Wrap to last marker
            last = max(self._timeline.markers, key=lambda m: m.frame)
            self._move_playhead_to(last.frame)

    def _move_playhead_to(self, frame: int) -> None:
        """Move playhead to specified frame."""
        duration = self._effective_timeline_duration(self._timeline) if self._timeline else 0
        self._playhead_frame = max(0, min(frame, duration - 1 if duration > 0 else 0))
        # C5 fix: this helper previously called a nonexistent
        # `_update_playhead_position`, so Shift+M / Ctrl+Shift+M marker jumps
        # crashed with AttributeError. Route through the public setter, which
        # updates the playhead item and emits the change.
        self.set_playhead(self._playhead_frame)

    def seek_to_frame(self, frame: int) -> None:
        """Public API: seek playhead to a specific frame (e.g. from marker list)."""
        self._move_playhead_to(frame)

    def delete_selected_marker(self) -> None:
        """
        Delete the currently selected marker.

        Finds the selected marker and executes delete command.
        """
        # Find selected marker
        selected_marker_id = None
        for marker_id, marker_item in self._marker_items.items():
            if marker_item._is_selected:
                selected_marker_id = marker_id
                break

        if selected_marker_id is None:
            logger.debug("No marker selected for deletion")
            return

        # Execute delete command
        cmd = DeleteMarkerCommand.create(selected_marker_id)
        self._execute_command(cmd)
        logger.info(f"Deleted marker {selected_marker_id}")

    def get_selected_marker_id(self) -> Optional[str]:
        """
        Get ID of currently selected marker.

        Returns:
            Marker ID if selected, None otherwise
        """
        for marker_id, marker_item in self._marker_items.items():
            if marker_item._is_selected:
                return marker_id
        return None

    # ==================== Navigation Methods ====================

    def _jump_to_start(self) -> None:
        """Jump playhead to timeline start (frame 0)."""
        self.set_playhead(0)
        self._ensure_playhead_visible()

    def _jump_to_end(self) -> None:
        """Jump playhead to timeline end (last frame)."""
        if self._timeline is None:
            return
        end_frame = max(0, self._effective_timeline_duration() - 1)
        self.set_playhead(end_frame)
        self._ensure_playhead_visible()

    def _move_playhead(self, delta_frames: int) -> None:
        """Move playhead by specified frame delta.

        Args:
            delta_frames: Number of frames to move (positive = forward, negative = backward)
        """
        new_frame = self._playhead_frame + delta_frames

        new_frame = max(0, new_frame)

        self.set_playhead(new_frame)
        self._ensure_playhead_visible()

    def _iter_timeline_object_boundaries(self) -> list:
        """INT-10：统一时间线对象边界（媒体 + 字幕）。

        旧的上/下边界跳转与 Fit Selection 只遍历 track.clips，字幕 cue
        的开始/结束被忽略（审计实测 subtitle[100,200) 唯一选中后 Down 从
        50 跳到 239，忽略 cue 起点 100）。
        """
        boundaries: list = []
        if self._timeline is None:
            return boundaries
        for track in self._timeline.all_tracks:
            for clip in track.clips:
                boundaries.append(clip.timeline_start)
                boundaries.append(clip.timeline_end)
            for sub in getattr(track, "subtitle_clips", ()) or ():
                boundaries.append(sub.timeline_start)
                boundaries.append(sub.timeline_end)
        return sorted(set(boundaries))

    def _jump_to_prev_clip_boundary(self) -> None:
        """Jump playhead to the nearest previous clip start or end boundary."""
        if self._timeline is None:
            return

        boundaries = self._iter_timeline_object_boundaries()

        if not boundaries:
            # No clips, jump to start
            self._jump_to_start()
            return

        # Sort boundaries
        boundaries = sorted(set(boundaries))

        # Find the nearest boundary before current playhead
        prev_boundary = None
        for boundary in boundaries:
            if boundary < self._playhead_frame:
                prev_boundary = boundary
            else:
                break

        if prev_boundary is not None:
            self.set_playhead(prev_boundary)
        else:
            # No boundary before playhead, jump to start
            self._jump_to_start()

        self._ensure_playhead_visible()

    def _jump_to_next_clip_boundary(self) -> None:
        """Jump playhead to the nearest next clip start or end boundary."""
        if self._timeline is None:
            return

        boundaries = self._iter_timeline_object_boundaries()

        if not boundaries:
            # No clips, jump to end
            self._jump_to_end()
            return

        # Sort boundaries
        boundaries = sorted(set(boundaries))

        # Find the nearest boundary after current playhead
        next_boundary = None
        for boundary in boundaries:
            if boundary > self._playhead_frame:
                next_boundary = boundary
                break

        if next_boundary is not None:
            self.set_playhead(next_boundary)
        else:
            # No boundary after playhead, jump to end
            self._jump_to_end()

        self._ensure_playhead_visible()

    def _jump_by_seconds(self, delta_seconds: float) -> None:
        """Jump playhead by specified seconds.

        Args:
            delta_seconds: Seconds to jump (positive = forward, negative = backward)
        """
        if self._timeline is None:
            return

        fps = float(self._timeline.fps) if self._timeline.fps else EMPTY_TIMELINE_FPS
        delta_frames = int(delta_seconds * fps)

        self._move_playhead(delta_frames)

    def _ensure_playhead_visible(self) -> None:
        """Ensure playhead is visible in viewport (scroll if needed)."""
        if self._playhead_item is None:
            return

        # Calculate playhead position in scene
        playhead_x = TRACK_HEADER_WIDTH + self._playhead_frame * self._pixels_per_frame

        # Get visible viewport area
        visible_rect = self.mapToScene(self.viewport().rect()).boundingRect()

        # Check if playhead is outside visible area
        viewport_width = visible_rect.width()
        playhead_visible_left = visible_rect.left()
        playhead_visible_right = visible_rect.right()

        # If playhead is not visible, center it
        if playhead_x < playhead_visible_left or playhead_x > playhead_visible_right:
            # Calculate target position to center playhead
            target_center_x = playhead_x - viewport_width / 2

            # Scroll to center playhead
            scroll_bar = self.horizontalScrollBar()
            if scroll_bar:
                # Convert to scrollbar value (accounting for header offset)
                scroll_value = int(target_center_x)
                scroll_bar.setValue(scroll_value)

    def center_on_playhead(self) -> None:
        """Center viewport on playhead position (public API)."""
        self._ensure_playhead_visible()

    # ==================== Ruler Interaction ====================

    def _on_ruler_clicked(self, frame: int) -> None:
        """Handle ruler click to set playhead position with snap support (P0-D).

        When snap is enabled, the playhead will snap to the nearest clip edge,
        marker, or sequence start. Hold Shift to temporarily disable snapping.

        Args:
            frame: Frame position from ruler click
        """
        # Reset dragging state (hide timecode label)
        if self._playhead_item:
            self._playhead_item.set_dragging(False)

        # P0-D: Apply snap (Shift temporarily disables)
        from src.features.edit.core.timeline_ops import SnapPointType
        shift_held = QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        if self._snap_enabled and not shift_held and self._timeline is not None:
            # Exclude playhead type when moving playhead (don't snap to self)
            snapped_frame, did_snap = self._snap_frame(
                frame,
                tuple(),
                show_indicator=True,
                exclude_types={SnapPointType.PLAYHEAD}
            )
            if did_snap:
                frame = snapped_frame
        elif self._snap_indicator:
            self._snap_indicator.hide_indicator()

        self.set_playhead(frame)
        self._ensure_playhead_visible()

    def _on_ruler_dragged(self, frame: int) -> None:
        """Handle ruler drag (scrubbing) to update playhead position with snap (P0-D).

        When snap is enabled, the playhead will snap to the nearest clip edge.
        Hold Shift to temporarily disable snapping.

        Args:
            frame: Frame position during scrub
        """
        # Set dragging state (show timecode label)
        if self._playhead_item:
            self._playhead_item.set_dragging(True)

        # P0-D: Apply snap during scrubbing (Shift temporarily disables)
        from src.features.edit.core.timeline_ops import SnapPointType
        shift_held = QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        if self._snap_enabled and not shift_held and self._timeline is not None:
            snapped_frame, did_snap = self._snap_frame(
                frame,
                tuple(),
                show_indicator=True,
                exclude_types={SnapPointType.PLAYHEAD}
            )
            if did_snap:
                frame = snapped_frame
        elif self._snap_indicator:
            self._snap_indicator.hide_indicator()

        self.set_playhead(frame)
        # During scrubbing, follow playhead
        self.center_on_playhead()

    def _on_ruler_scrub_finished(self) -> None:
        """Clear transient snap feedback once ruler scrubbing is complete."""
        if self._playhead_item:
            self._playhead_item.set_dragging(False)
        if self._snap_indicator:
            self._snap_indicator.hide_indicator()
        self._refresh_playhead_visual()

    # ==================== Zoom Fit Methods ====================

    def _timeline_content_end_frame(self) -> int:
        """Return the furthest end of any clip, including subtitle clips."""
        if self._timeline is None:
            return 0

        content_end = 0
        track_groups = (
            self._timeline.video_tracks,
            self._timeline.audio_tracks,
            self._timeline.subtitle_tracks,
        )
        for tracks in track_groups:
            for track in tracks:
                for clip in getattr(track, "clips", ()) or ():
                    content_end = max(content_end, int(clip.timeline_end))
                for clip in getattr(track, "subtitle_clips", ()) or ():
                    content_end = max(content_end, int(clip.timeline_end))
        return content_end

    def zoom_fit_content(self, fill_ratio: float = CONTENT_FIT_FILL_RATIO) -> None:
        """Fit all timeline content to a percentage of the visible width.

        ``Shift+Z`` uses an 80% fill ratio, leaving room on both sides for
        context while keeping the complete edit visible.  The track-header
        column is excluded from the width calculation.
        """
        if self._timeline is None:
            return

        content_end = self._timeline_content_end_frame()
        if content_end <= 0:
            return

        ratio = max(0.05, min(1.0, float(fill_ratio)))
        content_width = max(1, self.viewport().width() - TRACK_HEADER_WIDTH)
        target_zoom = content_width * ratio / float(content_end)
        target_zoom = max(MIN_ZOOM, min(MAX_ZOOM, target_zoom))

        self.set_zoom(target_zoom)
        self.scroll_to_timeline_start(deferred=True)
        logger.debug(
            "Zoom fit content: %.0f%% fill, end=%d frames, %.2f pixels/frame",
            ratio * 100.0,
            content_end,
            target_zoom,
        )

    def zoom_fit_all(self) -> None:
        """Zoom to fit all timeline content in visible viewport.

        Calculates the zoom level that shows the entire timeline duration
        within the visible timeline area.
        """
        if self._timeline is None:
            return

        # Get visible viewport width (excluding header)
        viewport_width = self.viewport().width() - TRACK_HEADER_WIDTH

        # Calculate target zoom to fit entire timeline
        duration = self._effective_timeline_duration()
        if duration <= 0:
            return

        # Target pixels per frame = viewport width / duration
        target_zoom = viewport_width / float(duration)

        # Clamp to valid range
        target_zoom = max(MIN_ZOOM, min(MAX_ZOOM, target_zoom))

        # Set zoom
        self.set_zoom(target_zoom)

        # Scroll to start
        scroll_bar = self.horizontalScrollBar()
        if scroll_bar:
            scroll_bar.setValue(0)

        logger.debug(f"Zoom fit all: {target_zoom:.2f} pixels/frame")

    def zoom_fit_selection(self) -> None:
        """Zoom to fit selected clips in visible viewport.

        Calculates the zoom level that shows only the selected clip range.
        """
        if self._timeline is None:
            return

        # Get selected clips
        selected_clip_ids = self._selection.selected_clip_ids
        if not selected_clip_ids:
            # No selection: fit to all
            self.zoom_fit_all()
            return

        # Find the time range of selected clips（INT-10：含字幕 cue）
        min_start = float('inf')
        max_end = 0

        for clip_id in selected_clip_ids:
            clip = self._timeline.get_clip_by_id(clip_id)
            if clip is not None:
                min_start = min(min_start, clip.timeline_start)
                max_end = max(max_end, clip.timeline_end)
                continue
            for track in self._timeline.all_tracks:
                for sub in getattr(track, "subtitle_clips", ()) or ():
                    if sub.clip_id == clip_id:
                        min_start = min(min_start, sub.timeline_start)
                        max_end = max(max_end, sub.timeline_end)

        if min_start >= max_end:
            return

        # Get visible viewport width
        viewport_width = self.viewport().width() - TRACK_HEADER_WIDTH

        # Calculate target zoom
        duration = max_end - min_start
        if duration <= 0:
            return

        # Add some padding (10% on each side)
        padding = duration * 0.2
        padded_duration = duration + padding

        target_zoom = viewport_width / float(padded_duration)
        target_zoom = max(MIN_ZOOM, min(MAX_ZOOM, target_zoom))

        # Set zoom
        self.set_zoom(target_zoom)

        # Scroll to center on selection
        center_frame = min_start + duration / 2 - padding / 4
        scroll_bar = self.horizontalScrollBar()
        if scroll_bar:
            scroll_value = int(TRACK_HEADER_WIDTH + center_frame * target_zoom - viewport_width / 2)
            scroll_bar.setValue(scroll_value)

        logger.debug(f"Zoom fit selection: {target_zoom:.2f} pixels/frame, range: {min_start}-{max_end}")

    # ==================== Playhead Follow Mode ====================

    def set_playhead_follow_enabled(self, enabled: bool) -> None:
        """
        Enable or disable playhead follow mode.

        When enabled, the viewport will automatically scroll to keep
        the playhead centered during playback.

        Args:
            enabled: Whether to enable follow mode
        """
        self._playhead_follow_enabled = enabled

    def is_playhead_follow_enabled(self) -> bool:
        """Check if playhead follow mode is enabled."""
        return self._playhead_follow_enabled

    def update_playhead_for_playback(self, frame: int) -> None:
        """
        Update playhead position during playback with optional follow.

        This method should be called by the playback system to update
        the playhead position. If follow mode is enabled, the viewport
        will scroll to keep the playhead centered.

        Args:
            frame: Current playback frame position
        """
        # Update playhead position
        self.set_playhead(frame)

        # Follow playhead if enabled
        if self._playhead_follow_enabled:
            self.center_on_playhead()

    # ==================== Drag and Drop from Media Pool ====================

    @staticmethod
    def _filter_external_media_paths(paths: List[str]) -> List[str]:
        """Return readable local media files, normalized and de-duplicated."""
        valid_paths: List[str] = []
        seen: set[str] = set()
        for path in paths:
            if not path:
                continue
            normalized_path = os.path.abspath(os.path.expanduser(path))
            if normalized_path in seen:
                continue
            seen.add(normalized_path)
            if not os.path.isfile(normalized_path):
                continue
            if not os.access(normalized_path, os.R_OK):
                continue
            if not is_media(normalized_path):
                continue
            valid_paths.append(normalized_path)
        return valid_paths

    @staticmethod
    def _is_subtitle_asset(asset: MediaAsset) -> bool:
        """Return whether an imported media-pool asset is a subtitle file."""
        return asset.media_type == "subtitle" or (
            os.path.splitext(asset.path)[1].lower() in SUPPORTED_SUBTITLE_EXTENSIONS
        )

    def _handle_subtitle_drag_move(self, event: QDragMoveEvent) -> None:
        """Accept subtitle drags over the timeline and show no media preview."""
        if not self._drag_assets or not all(
            self._is_subtitle_asset(asset) for asset in self._drag_assets
        ):
            event.ignore()
            return
        # Subtitle timing comes from the file, so the drop X is intentionally
        # not used as a cue offset. Any timeline panel area is a valid target;
        # each file becomes a new subtitle track with its original timecodes.
        event.acceptProposedAction()

    def _ensure_timeline_for_subtitle_import(self) -> bool:
        """Ensure a real sequence exists before adding imported subtitle tracks."""
        if self._timeline is None:
            self._timeline = self._create_default_timeline()
            if self._timeline is None:
                return False
            self._provisional_timeline = False
            self.load_timeline(self._timeline)
            self.timeline_changed.emit(self._timeline)
        elif self._provisional_timeline:
            self._provisional_timeline = False
        return True

    def _handle_subtitle_asset_drop(self, event: QDropEvent) -> None:
        """Create one new subtitle track per dragged subtitle asset."""
        assets = list(self._drag_assets)
        if not assets or not all(self._is_subtitle_asset(asset) for asset in assets):
            logger.warning("Subtitle drop cannot be mixed with video/audio assets")
            self._drag_assets = []
            self._drag_asset_ids = []
            event.ignore()
            return
        if not self._ensure_timeline_for_subtitle_import():
            event.ignore()
            return

        commands: list[Any] = []
        subtitle_position = len(self._timeline.subtitle_tracks)
        imported_tracks = 0
        for asset in assets:
            try:
                cues = parse_subtitle_file(asset.effective_path, self._timeline.fps)
            except (OSError, UnicodeError, ValueError) as exc:
                logger.warning("Subtitle import failed for %s: %s", asset.path, exc)
                continue
            if not cues:
                logger.warning("Subtitle file contains no readable cues: %s", asset.path)
                continue

            import uuid
            base_name = Path(asset.display_name or asset.path).stem or "Subtitles"
            cue_groups = partition_subtitle_cues(cues)
            for group_index, cue_group in enumerate(cue_groups, start=1):
                track_id = str(uuid.uuid4())
                track_name = (
                    base_name if len(cue_groups) == 1
                    else f"{base_name} {group_index}"
                )
                commands.append(AddTrackCommand.create(
                    "subtitle", track_name,
                    subtitle_position + imported_tracks, track_id
                ))
                language = parse_subtitle_file_language(asset.effective_path)
                if language:
                    from src.features.edit.core.captions.commands import (
                        UpdateSubtitleTrackMetadataCommand,
                    )
                    commands.append(UpdateSubtitleTrackMetadataCommand.create(
                        track_id, {"language": language},
                    ))
                for cue in cue_group:
                    commands.append(AddSubtitleClipCommand.create(
                        track_id,
                        cue.start_frame,
                        cue.duration_frames,
                        cue.text,
                    ))
                imported_tracks += 1

        self._drag_assets = []
        self._drag_asset_ids = []
        self._hide_drag_preview()
        if not imported_tracks:
            event.ignore()
            return

        self._execute_command(MacroCommand.create(
            f"Import {imported_tracks} subtitle track(s)", commands
        ))
        event.acceptProposedAction()
        logger.info("Imported %d subtitle track(s) from media pool", imported_tracks)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """
        Handle drag enter event from media pool, effects panel, or external files.

        Accepts drags that contain:
        - Asset IDs from the media pool (JSON format)
        - Transition type from the effects panel
        - Effect type from the effects panel
        - File URLs from external sources (fallback)
        """
        self._clear_drop_drag()
        mime_data = event.mimeData()

        # ── Application-specific MIME types MUST be checked before the
        # generic ``hasUrls()`` fallback below. QMimeData carried by an
        # internal QDrag (effect/transition/media-pool) may also report
        # hasUrls() == True on some platforms, which would otherwise
        # misroute the drop into the external-file-import branch and fail
        # with "no project set". Effect/transition drags come from inside
        # the app and must never be treated as external file imports.
        if mime_data.hasFormat(MIME_TYPE_EFFECT):
            # Parse effect type
            effect_data = mime_data.data(MIME_TYPE_EFFECT)
            effect_type = str(effect_data.data(), encoding="utf-8")

            if effect_type:
                self._drag_effect_type = effect_type
                self._drag_asset_ids = []
                self._drag_assets = []
                self._drag_file_paths = []  # Clear file paths
                self._drag_transition_type = None
                self._drag_generator_type = None

                event.acceptProposedAction()
                logger.debug(f"Drag enter: effect type {effect_type}")
            else:
                event.ignore()
            return

        # Generators (Adjustment Clip / Solid Color) — dragged to CREATE a
        # new clip, so they carry a distinct MIME routed to a separate handler.
        if mime_data.hasFormat(MIME_TYPE_GENERATOR):
            gen_data = mime_data.data(MIME_TYPE_GENERATOR)
            generator_type = str(gen_data.data(), encoding="utf-8")

            if generator_type:
                self._drag_generator_type = generator_type
                self._drag_asset_ids = []
                self._drag_assets = []
                self._drag_file_paths = []
                self._drag_effect_type = None
                self._drag_transition_type = None

                event.acceptProposedAction()
                logger.debug(f"Drag enter: generator type {generator_type}")
            else:
                event.ignore()
            return

        if mime_data.hasFormat(MOTION_GRAPHIC_GENERATOR_MIME):
            raw_payload = bytes(mime_data.data(MOTION_GRAPHIC_GENERATOR_MIME)).decode("utf-8")
            template_id = raw_payload.strip()
            params: dict[str, Any] = {}
            try:
                payload = json.loads(raw_payload)
                if isinstance(payload, dict):
                    template_id = str(payload.get("template_id", "")).strip()
                    raw_params = payload.get("params", {})
                    if isinstance(raw_params, dict):
                        params = dict(raw_params)
            except (TypeError, ValueError, json.JSONDecodeError):
                # Older Effects panels sent a plain template id; retain that
                # wire format as a compatible fallback.
                pass
            if template_id:
                self._drag_motion_graphic_generator_id = template_id
                self._drag_motion_graphic_generator_params = params
                self._drag_generator_type = f"motion_graphic_template:{template_id}"
                self._drag_asset_ids = []
                self._drag_assets = []
                self._drag_file_paths = []
                self._drag_effect_type = None
                self._drag_transition_type = None
                event.acceptProposedAction()
            else:
                event.ignore()
            return

        if mime_data.hasFormat(MIME_TYPE_TRANSITION):
            # Parse transition type
            transition_data = mime_data.data(MIME_TYPE_TRANSITION)
            transition_type = str(transition_data.data(), encoding="utf-8")

            if transition_type:
                self._drag_transition_type = transition_type
                self._drag_asset_ids = []
                self._drag_assets = []
                self._drag_file_paths = []  # Clear file paths

                event.acceptProposedAction()
                logger.debug(f"Drag enter: transition type {transition_type}")
            else:
                event.ignore()
            return

        # Audio transitions ride a distinct MIME so the drop handlers can target
        # audio tracks; the payload (audio:<curve>) is stored in the same
        # _drag_transition_type field and dispatched by the same handlers.
        if mime_data.hasFormat(MIME_TYPE_AUDIO_TRANSITION):
            transition_data = mime_data.data(MIME_TYPE_AUDIO_TRANSITION)
            transition_type = str(transition_data.data(), encoding="utf-8")

            if transition_type:
                self._drag_transition_type = transition_type
                self._drag_asset_ids = []
                self._drag_assets = []
                self._drag_file_paths = []

                event.acceptProposedAction()
                logger.debug(f"Drag enter: audio transition type {transition_type}")
            else:
                event.ignore()
            return

        # VP-B08: Source 拖放载荷（viewer 源区间 + 流意图）——契约比通用
        # asset MIME 更严格（显式源窗口 + stream intent），优先解析。
        if mime_data.hasFormat(MIME_TYPE_SOURCE_CLIP):
            payload = decode_source_drag_payload(
                mime_data.data(MIME_TYPE_SOURCE_CLIP))
            asset = (
                self._project.assets.get(payload.asset_id)
                if payload is not None and self._project is not None
                else None
            )
            if payload is None or asset is None:
                event.ignore()
                return
            # I04-2：进入即按可信 metadata 校验（指纹/区间/流）——
            # 跨工程同名 ID、越界区间、不存在的流在 enter 处即拒。
            reason = self._validate_source_drag_payload(payload, asset)
            if reason is not None:
                self.status_message_requested.emit(
                    f"Source clip drag rejected: {reason}")
                event.ignore()
                return
            self._drag_source_payload = payload
            self._drag_asset_ids = [payload.asset_id]
            self._drag_assets = [asset]
            self._drag_file_paths = []
            self._drag_transition_type = None
            self._drag_effect_type = None
            self._drag_generator_type = None
            self.dragMoveEvent(event)
            event.acceptProposedAction()
            logger.info(
                "Drag enter accepted: source clip %s [%d..%d] intent=%s",
                payload.asset_id, payload.in_frame, payload.out_frame,
                payload.stream_intent)
            return

        # Check for media pool assets MIME type (JSON format)
        if mime_data.hasFormat(MIME_TYPE_MEDIA_POOL_ASSETS):
            # Parse asset IDs from JSON
            asset_ids_data = mime_data.data(MIME_TYPE_MEDIA_POOL_ASSETS)
            asset_ids_str = str(asset_ids_data.data(), encoding="utf-8")

            # Try JSON format first (new unified format)
            try:
                asset_ids = json.loads(asset_ids_str)
                logger.debug(f"Drag enter: parsed JSON asset IDs: {asset_ids}")
            except json.JSONDecodeError:
                # Fallback: comma-separated format (legacy)
                asset_ids = asset_ids_str.split(",")
                logger.debug(f"Drag enter: parsed comma-separated asset IDs: {asset_ids}")

            logger.debug(f"Drag enter: received {len(asset_ids)} asset IDs")

            # Validate that assets exist in project
            if self._project is None:
                logger.warning("Drag enter: no project set, cannot validate assets")
                event.ignore()
                return

            valid_assets: list[MediaAsset] = []
            for asset_id in asset_ids:
                asset = self._project.assets.get(asset_id)
                if asset:
                    valid_assets.append(asset)
                    logger.debug(f"Drag enter: validated asset {asset_id} (type={asset.media_type})")
                else:
                    logger.warning(f"Drag enter: asset {asset_id} not found in project")

            if not valid_assets:
                logger.warning("Drag enter: no valid assets found")
                event.ignore()
                return

            # Store drag state
            self._drag_asset_ids = asset_ids
            self._drag_assets = valid_assets
            self._drag_transition_type: Optional[str] = None

            self.dragMoveEvent(event)
            event.acceptProposedAction()
            logger.info(f"Drag enter accepted: {len(valid_assets)} assets, timeline={self._timeline is not None}")

        # Check for text/uri-list MIME type (external file drag)
        elif mime_data.hasUrls():
            urls = mime_data.urls()
            if not urls:
                event.ignore()
                return

            # Extract readable local media paths from URLs.
            raw_file_paths = []
            for url in urls:
                if url.isLocalFile():
                    path = url.toLocalFile()
                    if path:
                        raw_file_paths.append(path)
            file_paths = self._filter_external_media_paths(raw_file_paths)

            if not file_paths:
                logger.warning("Drag enter: no valid readable media files in URLs")
                event.ignore()
                return

            logger.debug(f"Drag enter: received {len(file_paths)} file paths from URLs")

            # A user may start by dropping a Finder file directly on the
            # timeline. Ask the controller to create the default project
            # synchronously, matching the Media Pool import flow.
            if self._project is None:
                self.project_needed.emit()
            if self._project is None:
                logger.warning("Drag enter: unable to create project for file import")
                event.ignore()
                return

            # Import files as assets (if not already in project)
            # This will be handled in dropEvent
            self._drag_asset_ids = []  # Empty - will be filled during drop
            self._drag_assets = []  # Empty - will be filled during drop
            self._drag_file_paths = file_paths  # Store paths for later import
            for path in file_paths:
                asset = self._project.get_asset_by_path(path)
                self._drag_file_previews.append((
                    asset.display_name if asset else Path(path).name,
                    asset.media_type if asset else get_media_type(path),
                    self._get_asset_duration_frames(asset) if asset else None,
                ))
            self._drag_transition_type = None

            self.dragMoveEvent(event)
            event.acceptProposedAction()
            logger.info(f"Drag enter accepted: {len(file_paths)} external files")

        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        """
        Handle drag move event to show insert preview.

        Updates the position of the insert preview rectangle.
        For transitions, shows preview at nearest cut point.
        """
        # Handle transition drag
        if self._drag_transition_type:
            self._handle_transition_drag_move(event)
            return

        # Handle generator drag — generators are video clips, so reuse the
        # asset-drag preview path with media_type="video".
        if self._drag_generator_type:
            self._handle_generator_drag_move(event)
            return

        # Handle effect drag — effects attach to an existing clip at the
        # cursor position; accept the move so the user gets drop feedback
        # and dropEvent actually fires. Without this branch the effect drag
        # fell through to the asset-drag path, where ``_drag_assets`` is
        # empty (cleared in dragEnterEvent) and the event got ignored —
        # which is why dropping an effect never worked (only double-click).
        if self._drag_effect_type:
            self._handle_effect_drag_move(event)
            return

        if self._drag_file_paths:
            self._handle_external_drag_move(event)
            return
        if not self._drag_assets:
            event.ignore()
            return
        if any(self._is_subtitle_asset(asset) for asset in self._drag_assets):
            self._handle_subtitle_drag_move(event)
            return

        first = self._drag_assets[0]
        fps = None
        if self._timeline is None or self._timeline_is_empty(self._timeline):
            fps = float(self._timeline_fps_for_first_asset(first))
        self._show_media_drop_preview(
            event,
            [asset.display_name for asset in self._drag_assets],
            [asset.media_type for asset in self._drag_assets],
            [self._get_asset_duration_frames(asset, fps=fps) for asset in self._drag_assets],
            thumbnail_path=first.path if not first.is_virtual else None,
            allow_overlap=self._edit_mode == "insert",
        )

    def _handle_external_drag_move(self, event: QDragMoveEvent) -> None:
        """Preview a Finder drop without probing or importing its files."""
        names, types, durations = zip(*self._drag_file_previews)
        if "subtitle" in types:
            if all(media_type == "subtitle" for media_type in types):
                event.acceptProposedAction()
            else:
                event.ignore()
            return

        # Unknown file lengths get a visible placeholder until the drop
        # imports their metadata. The hover path never probes media.
        placeholder = max(1, math.ceil(64.0 / self._pixels_per_frame))
        self._show_media_drop_preview(
            event, list(names), list(types),
            [duration if duration is not None else placeholder for duration in durations],
        )

    def _resolve_media_drop_target(
        self, position: QPointF, media_type: str, timeline_was_missing: bool
    ) -> TimelineDropTarget:
        """Resolve the same viewport location during hover and commit."""
        if position.x() < TRACK_HEADER_WIDTH * self.transform().m11():
            return TimelineDropTarget(0, is_valid=False, reason="Track header")

        scene_pos = self.mapToScene(position.toPoint())
        self._configure_layout_engine()
        drop = self._layout_engine.resolve_drop_target(
            scene_pos.x(), scene_pos.y(), media_type, self._get_locked_track_ids()
        )
        return self._prefer_lazy_default_track(drop, media_type, timeline_was_missing)

    def _show_media_drop_preview(
        self,
        event: QDragMoveEvent,
        names: list[str],
        media_types: list[str],
        durations: list[int],
        *,
        thumbnail_path: str | None = None,
        allow_overlap: bool = False,
    ) -> None:
        """Place the footprint on the track resolved by the normal drop path."""
        preview = self._drag_preview_item
        if preview is None:
            event.ignore()
            return
        drop = self._resolve_media_drop_target(
            event.position(), media_types[0], self._timeline is None or self._provisional_timeline
        )
        if not drop.is_valid and not drop.create_track:
            self._hide_drag_preview()
            event.ignore()
            return

        frame = self._calculate_insert_frame(drop.frame, show_indicator=True)
        track_top, track_height = self._drop_preview_lane(drop)
        if track_height <= 0:
            self._hide_drag_preview()
            event.acceptProposedAction()
            return

        track = self._timeline.get_track_by_id(drop.track_id) if self._timeline and drop.track_id else None
        total_duration = sum(durations)
        valid = track is None or allow_overlap or not any(
            frame < clip.timeline_end and frame + total_duration > clip.timeline_start
            for clip in track.clips
        )
        thumbnail = (
            self._thumb_provider.get_cached_thumbnail(thumbnail_path, 80)
            if thumbnail_path else None
        )
        preview.show_preview(
            frame, track_top, track_height, names, media_types, total_duration,
            thumbnails={names[0]: thumbnail} if thumbnail else None,
            durations=durations,
            is_valid=valid,
        )
        event.acceptProposedAction()

    def _drop_preview_lane(self, drop: TimelineDropTarget) -> tuple[float, float]:
        """Return the visible destination lane, including a prospective new track."""
        engine = self._layout_engine
        area = engine.area_layout()
        is_video = drop.track_type == "video"
        panel = area.video_area if is_video else area.audio_area
        if drop.track_id:
            lane = engine.track_rect(drop.track_id)
            top, height = lane.top(), lane.height()
        elif is_video:
            height = float(EDIT_SIZES["track_h_video"])
            top = area.splitter_rect.top() - height
            if self._timeline and self._timeline.video_tracks:
                top = self._compute_new_track_preview_y("__new_video_track_above__", "video")
        else:
            height = float(EDIT_SIZES["track_h_audio"])
            top = panel.top()
            if self._timeline and self._timeline.audio_tracks:
                top = self._compute_new_track_preview_y("__new_audio_track_below__", "audio")

        visible_top = max(panel.top(), top)
        visible_bottom = min(panel.bottom(), top + height)
        return visible_top, max(0.0, visible_bottom - visible_top)

    def _select_transition_target_track(self, track_index: int) -> Optional[object]:
        """Pick the track a transition drop should land on.

        Video transitions target ``ordered_video_tracks``; audio transitions
        (``audio:*`` payload in ``_drag_transition_type``) target
        ``ordered_audio_tracks``.  Returns ``None`` when the visual index falls
        outside the relevant track group and no fallback track exists.
        """
        if self._timeline is None:
            return None
        self._configure_layout_engine()
        is_audio = is_audio_transition(self._drag_transition_type or "")
        ordered = (
            self._layout_engine.ordered_audio_tracks() if is_audio
            else self._layout_engine.ordered_video_tracks()
        )
        if track_index < len(ordered):
            return self._timeline.get_track_by_id(ordered[track_index].track_id)
        # Fallback to the first track of the matching kind.
        tracks = self._timeline.audio_tracks if is_audio else self._timeline.video_tracks
        return tracks[0] if tracks else None

    def _handle_transition_drag_move(self, event: QDragMoveEvent) -> None:
        """Handle transition drag move event."""
        if self._timeline is None:
            event.ignore()
            return

        # Get scene position (convert from viewport to scene coordinates)
        scene_pos = self.mapToScene(event.position().toPoint())

        # Calculate track and frame
        track_index, frame = self._get_track_and_frame_from_pos(scene_pos)
        if track_index < 0:
            self._hide_drag_preview()
            event.ignore()
            return

        # Find target track using visual index — video or audio depending on
        # whether the dragged transition is an audio crossfade.
        target_track = self._select_transition_target_track(track_index)

        if target_track is None:
            self._hide_drag_preview()
            event.ignore()
            return

        # Find nearest cut point or an available clip edge.  At a real cut the
        # two-sided transition wins; isolated sequence edges create a
        # one-sided transition.
        cut_points = self._get_cut_points_for_track(target_track.track_id)
        nearest_cut = self._find_nearest_cut_point(frame, cut_points)

        if nearest_cut is not None:
            # Update transition preview at cut point using visual index
            all_ordered = self._layout_engine.all_ordered_tracks()
            visual_idx = next(
                (i for i, t in enumerate(all_ordered) if t.track_id == target_track.track_id),
                0,
            )
            self._update_transition_drag_preview(visual_idx, nearest_cut)
            event.acceptProposedAction()
        else:
            self._hide_drag_preview()
            event.ignore()

    def _handle_effect_drag_move(self, event: QDragMoveEvent) -> None:
        """Accept an effect drag only when the cursor sits over a clip.

        Mirrors the clip-targeting logic in :meth:`_handle_effect_drop`
        so the accepted/ignored decision here matches exactly what the
        drop will accept. Effects bind to existing clips (they do not
        create new clips), so an empty region must reject the drop.
        """
        if self._timeline is None or not self._drag_effect_type:
            event.ignore()
            return

        effect_type = self._drag_effect_type
        from src.features.edit.core.effect_registry import (
            get_effect_definition, AUDIO_MIXER_EFFECT_TYPES,
        )
        _def = get_effect_definition(effect_type)
        is_audio_effect = (
            effect_type in AUDIO_MIXER_EFFECT_TYPES
            or (_def is not None and _def.category == "audio")
        )

        scene_pos = self.mapToScene(event.position().toPoint())
        track_index, frame = self._get_track_and_frame_from_pos(scene_pos)
        if track_index < 0:
            event.ignore()
            return

        _target_track, target_clip = self._resolve_effect_drop_target(
            track_index, frame, is_audio_effect
        )

        if target_clip is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def _get_cut_points_for_track(self, track_id: str) -> tuple[int, ...]:
        """Get cut points plus clip edges accepted by transition drops."""
        from src.features.edit.core.timeline_ops import get_all_cut_points
        if self._timeline is None:
            return tuple()
        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return tuple()
        points = set(get_all_cut_points(self._timeline, track_id))
        for clip in track.clips:
            points.add(clip.timeline_start)
            points.add(clip.timeline_end)
        return tuple(sorted(points))

    def _find_nearest_cut_point(
        self,
        frame: int,
        cut_points: tuple[int, ...],
        threshold: int = 6
    ) -> Optional[int]:
        """Find nearest cut point within threshold."""
        if not cut_points:
            return None

        # Only accept a drop when it is close enough to a real edit point.
        nearest = min(cut_points, key=lambda c: abs(frame - c))
        if abs(frame - nearest) <= threshold:
            return nearest

        return None

    def _update_transition_drag_preview(
        self,
        track_index: int,
        cut_frame: int
    ) -> None:
        """Update transition drag preview rectangle.

        Note: Transition preview uses a simpler visualization than asset drag,
        since transitions are small strips between clips.
        """
        # Use drag_preview_item for transition preview (with transition style)
        if self._drag_preview_item is None:
            return

        # Position at a centered cut or inside a one-sided clip edge.
        # DragPreviewItem has a dedicated transition painter; using the generic
        # asset painter here used to collapse the preview into only a snap
        # marker/plus sign.
        default_duration = 12  # Default transition duration
        start_frame = cut_frame - default_duration // 2
        target_track = self._track_at_visual_index(track_index)
        if target_track is not None:
            from src.features.edit.core.timeline_ops import find_adjacent_clips_at_frame
            left_clip, right_clip = find_adjacent_clips_at_frame(
                self._timeline, target_track.track_id, cut_frame, threshold=1
            )
            if left_clip is None and right_clip is not None:
                start_frame = right_clip.timeline_start
            elif left_clip is not None and right_clip is None:
                start_frame = left_clip.timeline_end - default_duration
        transition_base_type = (self._drag_transition_type or "").split(":", 1)[0]
        transition_label = TRANSITION_LABELS.get(transition_base_type, "Transition")

        # 设置过渡预览样式（使用 'transition' 类型）
        self._drag_preview_item.show_preview(
            frame=start_frame,
            track_top=self._track_top_for_index(track_index),
            track_height=self._track_height_for(target_track) if target_track else TRACK_HEIGHT,
            asset_names=[transition_label],
            asset_types=['transition'],
            total_duration=default_duration,
            thumbnails=None,
            is_valid=True
        )

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        """Clear preview state when the drag leaves the timeline."""
        self._clear_drop_drag()

    def _clear_drop_drag(self) -> None:
        """Reset the MIME payload and all transient drop visuals."""
        self._hide_drag_preview()
        self._drag_asset_ids = []
        self._drag_assets = []
        self._drag_source_payload = None
        self._drag_file_paths = []
        self._drag_file_previews = []
        self._drag_transition_type = None
        self._drag_effect_type = None
        self._drag_generator_type = None
        self._drag_motion_graphic_generator_params = {}
        self._drag_motion_graphic_generator_id = None

    def dropEvent(self, event: QDropEvent) -> None:
        """
        Handle drop event to add clips, transitions, or effects to timeline.

        For assets: Creates ClipInstances and executes AddClipCommand.
        For transitions: Finds adjacent clips and executes AddTransitionCommand.
        For effects: Finds clip at drop position and executes AddEffectCommand.
        For external files: Imports files as assets and adds to timeline.
        """
        try:
            self._commit_drop(event)
        finally:
            self._clear_drop_drag()

    def _validate_source_drag_payload(self, payload, asset) -> Optional[str]:
        """I04-2：载荷 × 当前项目**可信 metadata** 的校验（零写入）。

        - 指纹：载荷声明了指纹（I04 生产端必带）而与当前项目素材指纹
          不符 → 跨工程同名 ID / 素材换版，整单拒绝；
        - 源区间：按素材自身 total_frames 校验（不信载荷自报范围）；
        - 流可用性：video/audio 意图按素材实际流裁决；
        - 载荷的 source_fps 只是冗余信息——所有时长换算一律用素材
          metadata 的 fps（忽略任意覆盖）。
        """
        from src.features.edit.core.source_time_domain import (
            media_fingerprint_for_path,
        )
        fingerprint = getattr(payload, "media_fingerprint", None)
        if fingerprint:
            if fingerprint != media_fingerprint_for_path(str(asset.path)):
                return ("asset fingerprint mismatch: different media under "
                        "asset id '%s'" % payload.asset_id)
        total = asset.total_frames
        if total and int(payload.out_frame) >= int(total):
            return ("source range [%d..%d] is past the end of this asset "
                    "(%d frames)" % (payload.in_frame, payload.out_frame,
                                     int(total)))
        has_video = asset.media_type != "audio"
        has_audio = (asset.media_type == "audio"
                     or bool(getattr(asset, "audio_channels", None)))
        intent = payload.stream_intent
        if intent == "video" and not has_video:
            return "video stream unavailable for this asset"
        if intent == "audio" and not has_audio:
            return "audio stream unavailable for this asset"
        return None

    def _handle_source_clip_drop(self, event: QDropEvent) -> None:
        """VP-B08: Source 拖放（viewer 源区间 + 流意图）→ 单次既有编辑链。

        - 源区间是 **Out 包含**语义（in..out），走与 Source 监视器插入相
          同的 planner/命令链（insert 模式直连 ``insert_clip``）；
        - stream intent：av（默认配对）/ video（只视频）/ audio（只音频，
          落音频目标轨）；
        - 锁轨 / 无可用目标轨 / 素材不在本项目 → 状态栏回因并拒绝
          （零写入，不留半条 clip）。
        """
        payload = self._drag_source_payload
        if payload is None or not self._drag_assets:
            event.ignore()
            return
        asset = (self._project.assets.get(payload.asset_id)
                 if self._project is not None else None)
        if asset is None:
            self.status_message_requested.emit(
                "Source drop rejected: asset is no longer in the project")
            event.ignore()
            return
        # I04-2：drop 处复验（enter 后工程/素材可能变化；零写入前置）
        reason = self._validate_source_drag_payload(payload, asset)
        if reason is not None:
            self.status_message_requested.emit(f"Source drop rejected: {reason}")
            event.ignore()
            return
        timeline_was_missing = (
            self._timeline is None or self._provisional_timeline)
        if not self._ensure_first_asset_timeline_settings(asset):
            event.ignore()
            return
        media_type = ("audio" if payload.stream_intent == "audio"
                      else asset.media_type)
        drop = self._resolve_media_drop_target(
            event.position(), media_type, timeline_was_missing)
        if not drop.is_valid and not drop.create_track:
            self.status_message_requested.emit(
                f"Source drop rejected: {drop.reason or 'invalid target'}")
            event.ignore()
            return
        target_track_type = (
            "audio" if media_type == "audio"
            else "video" if media_type in (
                "video", "image", "comp_clip", "motion_graphic")
            else "video")
        target_track = None
        if drop.track_id and self._timeline is not None:
            target_track = self._timeline.get_track_by_id(drop.track_id)
        track_created_here = False
        if target_track is None and drop.create_track and \
                self._timeline is not None:
            add_track_cmd = AddTrackCommand.create(target_track_type)
            self._execute_command(add_track_cmd)
            if add_track_cmd._created_track_id:
                target_track = self._get_track_by_id(
                    add_track_cmd._created_track_id)
                track_created_here = True
        if target_track is None:
            self.status_message_requested.emit(
                "Source drop rejected: no target track")
            event.ignore()
            return
        if getattr(target_track, "is_locked", False):
            self.status_message_requested.emit(
                "Source drop rejected: target track is locked")
            event.ignore()
            return
        insert_frame = self._calculate_insert_frame(drop.frame)
        source_range = (int(payload.in_frame), int(payload.out_frame))
        try:
            if self._edit_mode == "insert":
                # I04-3：insert 分支同样消费流意图（此前直连 insert_clip
                # 丢掉 intent，video 意图插出配对、audio 意图插出两条）
                success = self.insert_clip(
                    asset_id=asset.asset_id,
                    in_frame=source_range[0],
                    out_frame=source_range[1],
                    playhead=insert_frame,
                    track_id=target_track.track_id,
                    stream_intent=payload.stream_intent,
                )
            else:
                success = self._add_asset_as_clip(
                    asset, target_track, insert_frame,
                    source_range=source_range,
                    stream_intent=payload.stream_intent)
        except (ValueError, RuntimeError) as exc:
            self.status_message_requested.emit(f"Source drop rejected: {exc}")
            success = False
        if not success and track_created_here:
            # I04-4：建轨后插入失败 → 回滚本次建轨（不留孤立轨与 Undo
            # 垃圾；此时插入未产生任何命令，栈顶必为建轨命令）
            try:
                self.undo()
                self.status_message_requested.emit(
                    "Source drop rejected: insert failed; created track "
                    "was rolled back")
            except Exception:
                logger.exception("Failed to roll back created track")
        if success:
            event.acceptProposedAction()
        else:
            event.ignore()

    def _commit_drop(self, event: QDropEvent) -> None:
        # Clear feedback before a drop opens dialogs or runs the import path.
        self._hide_drag_preview()
        logger.debug(f"DropEvent: drag_assets={len(self._drag_assets)}, drag_file_paths={len(self._drag_file_paths)}, timeline={self._timeline is not None}")

        # Handle generator drop (Adjustment Clip / Solid Color) — creates a clip
        if self._drag_generator_type:
            self._handle_generator_drop(event)
            return

        # Handle effect drop (NEW)
        if self._drag_effect_type:
            self._handle_effect_drop(event)
            return

        # Handle transition drop
        if self._drag_transition_type:
            self._handle_transition_drop(event)
            return

        # VP-B08: Source 拖放（viewer 源区间 + 流意图）优先于通用资产拖放
        if self._drag_source_payload is not None:
            self._handle_source_clip_drop(event)
            return

        # Handle external file drop (import and add)
        if self._drag_assets and any(
            self._is_subtitle_asset(asset) for asset in self._drag_assets
        ):
            self._handle_subtitle_asset_drop(event)
            return
        if not self._drag_assets and self._drag_file_paths:
            logger.info(f"DropEvent: importing {len(self._drag_file_paths)} external files")
            self._handle_external_file_drop(event)
            return

        # Handle asset drop
        if not self._drag_assets:
            logger.warning("DropEvent: no assets in drag state")
            event.ignore()
            return

        timeline_was_missing = self._timeline is None or self._provisional_timeline
        if not self._ensure_first_asset_timeline_settings(self._drag_assets[0]):
            event.ignore()
            return

        media_type = self._drag_assets[0].media_type
        drop = self._resolve_media_drop_target(event.position(), media_type, timeline_was_missing)

        if not drop.is_valid and not drop.create_track:
            logger.debug(f"DropEvent: invalid drop target, reason={drop.reason}")
            self._hide_drag_preview()
            event.ignore()
            return

        logger.debug(
            f"DropEvent: drop valid={drop.is_valid}, track_id={drop.track_id}, "
            f"frame={drop.frame}, create_track={drop.create_track}, reason={drop.reason}"
        )

        # Resolve target track
        target_track = None
        target_track_type = "video" if media_type in ("video", "image", "comp_clip", "motion_graphic") else "audio"
        if drop.track_id:
            target_track = self._timeline.get_track_by_id(drop.track_id)

        if target_track is None and drop.create_track:
            logger.debug(f"DropEvent: creating new {target_track_type} track")
            add_track_cmd = AddTrackCommand.create(target_track_type)
            self._execute_command(add_track_cmd)

            # Get the newly created track by ID (stored during apply())
            if add_track_cmd._created_track_id:
                target_track = self._get_track_by_id(add_track_cmd._created_track_id)
                logger.debug(f"DropEvent: got new track by ID: {target_track.track_id if target_track else 'None'}")

            if target_track is None:
                # Fallback: find track by type (should not happen)
                logger.warning("DropEvent: Failed to get newly created track by ID, falling back to type lookup")
                target_track = self._find_target_track(0, target_track_type)
                if target_track is None:
                    logger.warning("DropEvent: Fallback lookup also failed")
                    event.ignore()
                    return

        if target_track is None:
            logger.warning("DropEvent: no target track resolved")
            self._hide_drag_preview()
            event.ignore()
            return

        logger.debug(f"DropEvent: target_track={target_track.name}, frame={drop.frame}")

        # Calculate insert position
        insert_frame = self._calculate_insert_frame(drop.frame)
        logger.debug(f"DropEvent: insert_frame={insert_frame}")

        # Add clips for all assets
        current_frame = insert_frame
        assets_to_add = self._drag_assets.copy()  # Copy because we clear _drag_assets after
        added_count = 0
        batch_commands: list[Any] = []
        self._batch_drop_commands = batch_commands
        try:
            for asset in assets_to_add:
                if self._edit_mode == "insert":
                    # B8: insert mode — ripple at drop point.  Commands are
                    # executed individually (not batched): each ripple shifts
                    # downstream clips before the next asset lands.
                    success = self._insert_asset_as_clip(
                        asset, target_track, current_frame
                    )
                else:
                    success = self._add_asset_as_clip(asset, target_track, current_frame)
                if success:
                    added_count += 1
                    # Advance frame position for next clip
                    duration_frames = self._get_asset_duration_frames(asset)
                    current_frame += duration_frames
                    logger.debug(
                        f"DropEvent: queued asset {asset.asset_id} at frame "
                        f"{current_frame - duration_frames}, duration={duration_frames}"
                    )
                else:
                    logger.warning(f"DropEvent: failed to queue asset {asset.asset_id}")
        finally:
            self._batch_drop_commands = None

        if batch_commands:
            try:
                self._execute_command(MacroCommand.create(
                    f"Add {len(batch_commands)} media pool clip(s)",
                    batch_commands,
                ))
            except (RuntimeError, ValueError) as exc:
                # MacroCommand rolls back all subcommands on failure. Keep the
                # drop interaction recoverable instead of letting one bad
                # asset terminate the UI event handler.
                logger.warning("Batch media pool drop failed: %s", exc)
                added_count = 0

        # Hide preview
        self._hide_drag_preview()
        self._drag_asset_ids = []
        self._drag_assets = []

        event.acceptProposedAction()
        logger.info(f"DropEvent completed: added {added_count}/{len(assets_to_add)} assets to track {target_track.name if target_track else 'unknown'}")

    def _handle_transition_drop(self, event: QDropEvent) -> None:
        """
        Handle transition drop at cut point.

        Finds adjacent clips at the drop position and adds transition.
        """
        if self._timeline is None:
            event.ignore()
            return

        transition_payload = self._drag_transition_type
        if not transition_payload:
            event.ignore()
            return

        transition_type = transition_payload
        transition_params: dict[str, Any] = {}
        # Audio transitions carry their full type in the payload
        # (e.g. "audio:constant_power") — the colon there is part of the type
        # namespace, not a wipe direction separator. Only the video wipe preset
        # uses "wipe:<direction>" shorthand.
        if transition_payload.startswith("wipe:"):
            transition_type, direction = transition_payload.split(":", 1)
            if transition_type == "wipe" and direction:
                transition_params["direction"] = direction

        # Get scene position (convert from viewport to scene coordinates)
        scene_pos = self.mapToScene(event.position().toPoint())

        # Calculate track and frame
        track_index, frame = self._get_track_and_frame_from_pos(scene_pos)
        if track_index < 0:
            self._hide_drag_preview()
            event.ignore()
            return

        # Find target track using visual index — video or audio depending on
        # whether the dragged transition is an audio crossfade.
        target_track = self._select_transition_target_track(track_index)
        if target_track is None:
            event.ignore()
            return

        # Find nearest cut point or sequence edge
        cut_points = self._get_cut_points_for_track(target_track.track_id)
        nearest_cut = self._find_nearest_cut_point(frame, cut_points)

        if nearest_cut is None:
            logger.warning("No valid cut point for transition drop")
            event.ignore()
            return

        # Find adjacent clips at cut point
        from src.features.edit.core.timeline_ops import find_adjacent_clips_at_frame
        left_clip, right_clip = find_adjacent_clips_at_frame(
            self._timeline,
            target_track.track_id,
            nearest_cut,
            threshold=1  # Allow 1 frame gap
        )

        if left_clip is not None and right_clip is not None:
            self.add_transition_at_cut_point(
                track_id=target_track.track_id,
                left_clip_id=left_clip.clip_id,
                right_clip_id=right_clip.clip_id,
                duration=12,
                transition_type=transition_type,
                transition_params=transition_params,
            )
        else:
            edge_clip = right_clip or left_clip
            edge = "left" if right_clip is not None else "right"
            if edge_clip is None:
                logger.warning("No clip found at transition drop point")
                event.ignore()
                return
            self.add_transition_at_clip_edge(
                track_id=target_track.track_id,
                clip_id=edge_clip.clip_id,
                edge=edge,
                duration=12,
                transition_type=transition_type,
                transition_params=transition_params,
            )

        # Hide preview and clear state
        self._hide_drag_preview()
        self._drag_transition_type = None

        event.acceptProposedAction()
        logger.info(
            f"Added {transition_type} transition at "
            f"{left_clip.clip_id if left_clip else right_clip.clip_id} edge"
        )

    def _handle_external_file_drop(self, event: QDropEvent) -> None:
        """
        Handle external file drop - import files and add to timeline.

        This handles the text/uri-list fallback when dragging files from
        outside the application (e.g., from Finder/Desktop).
        """
        if self._project is None:
            logger.warning("Handle external file drop: no project set")
            event.ignore()
            self._drag_file_paths = []
            return

        file_paths = self._filter_external_media_paths(self._drag_file_paths.copy())
        if not file_paths:
            logger.warning("Handle external file drop: no file paths")
            event.ignore()
            self._drag_file_paths = []
            return

        # Import files through EditProject so assets, bins, modified_at, and
        # external listeners stay synchronized.
        # AUD-21412: probe only the files that are not yet in the project,
        # using a bounded worker pool so a multi-file drop blocks the UI
        # thread for ~(N/4)×probe instead of N×probe.  The drop itself stays
        # synchronous (Qt owns the QDropEvent only for the duration of this
        # handler, so the import cannot be deferred past it).
        probe_results: Dict[str, Dict[str, Any]] = {}
        new_paths = [
            p for p in file_paths
            if self._project.get_asset_by_path(p) is None
        ]
        if new_paths:
            from concurrent.futures import ThreadPoolExecutor

            def _probe_one(drop_path: str) -> tuple:
                from src.features.edit.core.project import probe_media_metadata
                return drop_path, (probe_media_metadata(drop_path) or {})

            with ThreadPoolExecutor(
                max_workers=min(4, len(new_paths)),
                thread_name_prefix="drop-probe",
            ) as pool:
                futures = {p: pool.submit(_probe_one, p) for p in new_paths}
                for probe_path, future in futures.items():
                    try:
                        _, metadata = future.result()
                        probe_results[probe_path] = metadata
                    except Exception as e:
                        # Probed value stays absent → the import loop falls
                        # back to the synchronous probe, which will raise
                        # and be logged/skipped there (old behaviour).
                        logger.error(
                            f"Handle external file drop: probe failed for "
                            f"{probe_path}: {e}"
                        )

        imported_assets: List[MediaAsset] = []
        project_changed = False
        for path in file_paths:
            # Check if asset already exists in project (by path)
            existing_asset = self._project.get_asset_by_path(path)

            if existing_asset:
                # Asset already exists, use it
                imported_assets.append(existing_asset)
                logger.debug(f"Handle external file drop: using existing asset {existing_asset.asset_id}")
            else:
                # Import new asset
                try:
                    metadata = probe_results.get(path)
                    if metadata is None:
                        # AUD-21412 fallback: probe failed off-thread —
                        # retry synchronously so the per-path error handling
                        # below keeps the old behaviour.
                        from src.features.edit.core.project import probe_media_metadata
                        metadata = probe_media_metadata(path)
                    media_type = get_media_type(path)

                    new_project = self._project.import_asset(
                        path=path,
                        media_type=media_type,
                        metadata=metadata or {},
                    )
                    asset = new_project.get_asset_by_path(path)
                    if asset is None:
                        logger.warning(f"Handle external file drop: imported asset missing after project update: {path}")
                        continue
                    if new_project is not self._project:
                        self._project = new_project
                        project_changed = True
                    imported_assets.append(asset)
                    logger.info(f"Handle external file drop: imported new asset {asset.asset_id} from {path}")
                except Exception as e:
                    logger.error(f"Handle external file drop: failed to import {path}: {e}")

        if not imported_assets:
            logger.warning("Handle external file drop: no assets imported")
            event.ignore()
            self._drag_file_paths = []
            return

        if project_changed and self._project is not None:
            self.project_changed.emit(self._project)

        # Subtitle files are imported as text assets and become subtitle
        # tracks, not media clips. Mixed subtitle/video drops are rejected so
        # a subtitle file can never accidentally become an audio/image clip.
        if any(self._is_subtitle_asset(asset) for asset in imported_assets):
            if not all(self._is_subtitle_asset(asset) for asset in imported_assets):
                logger.warning("External drop cannot mix subtitle and media files")
                self._drag_file_paths = []
                event.ignore()
                return
            self._drag_assets = imported_assets
            self._drag_asset_ids = [asset.asset_id for asset in imported_assets]
            self._drag_file_paths = []
            self._handle_subtitle_asset_drop(event)
            return

        # Set drag assets and proceed with normal drop logic
        self._drag_assets = imported_assets
        self._drag_asset_ids = [a.asset_id for a in imported_assets]
        self._drag_file_paths = []

        # Call the normal asset drop handling (by recursively calling dropEvent with updated state)
        # But we need to avoid recursion, so we'll handle it inline here

        timeline_was_missing = self._timeline is None or self._provisional_timeline
        if not self._ensure_first_asset_timeline_settings(imported_assets[0]):
            event.ignore()
            return

        media_type = imported_assets[0].media_type
        drop = self._resolve_media_drop_target(event.position(), media_type, timeline_was_missing)

        if not drop.is_valid and not drop.create_track:
            logger.debug(f"External file drop: invalid drop target, reason={drop.reason}")
            self._hide_drag_preview()
            event.ignore()
            return

        # Resolve target track
        target_track_type = "video" if media_type in ("video", "image", "comp_clip", "motion_graphic") else "audio"
        target_track = None
        if drop.track_id:
            target_track = self._timeline.get_track_by_id(drop.track_id)

        if target_track is None and drop.create_track:
            logger.debug(f"External file drop: creating new {target_track_type} track")
            add_track_cmd = AddTrackCommand.create(target_track_type)
            self._execute_command(add_track_cmd)
            if add_track_cmd._created_track_id:
                target_track = self._get_track_by_id(add_track_cmd._created_track_id)
            if target_track is None:
                target_track = self._find_target_track(0, target_track_type)
                if target_track is None:
                    logger.warning("External file drop: failed to create/find track")
                    event.ignore()
                    return

        if target_track is None:
            logger.warning("External file drop: no target track resolved")
            self._hide_drag_preview()
            event.ignore()
            return

        # Calculate insert position
        insert_frame = self._calculate_insert_frame(drop.frame)

        # Add clips for all imported assets
        current_frame = insert_frame
        added_count = 0
        for asset in imported_assets:
            success = self._add_asset_as_clip(asset, target_track, current_frame)
            if success:
                added_count += 1
                duration_frames = self._get_asset_duration_frames(asset)
                current_frame += duration_frames

        # Hide preview
        self._hide_drag_preview()
        self._drag_asset_ids = []
        self._drag_assets = []

        event.acceptProposedAction()
        logger.info(f"Handle external file drop completed: imported and added {added_count}/{len(imported_assets)} assets")

    def _handle_effect_drop(self, event: QDropEvent) -> None:
        """
        Handle effect drop on a clip.

        Finds the clip at the drop position and adds the effect to it.
        """
        if self._timeline is None:
            event.ignore()
            return

        effect_type = self._drag_effect_type
        if not effect_type:
            event.ignore()
            return

        # P-clip-FX: 按效果类别决定可投放的轨道类型。
        # audio 效果（mixer 8 种）只能落到音频片段；其余（视频/变换等）
        # 只能落到视频片段。避免 audio 效果误落到上层 video clip。
        from src.features.edit.core.effect_registry import (
            get_effect_definition, AUDIO_MIXER_EFFECT_TYPES,
        )
        _def = get_effect_definition(effect_type)
        is_audio_effect = (
            effect_type in AUDIO_MIXER_EFFECT_TYPES
            or (_def is not None and _def.category == "audio")
        )

        # Get scene position (convert from viewport to scene coordinates)
        scene_pos = self.mapToScene(event.position().toPoint())

        # Calculate track and frame
        track_index, frame = self._get_track_and_frame_from_pos(scene_pos)
        if track_index < 0:
            event.ignore()
            return

        # Resolve the clip on the visual track under the cursor.  Do not scan
        # every track by frame only: an adjustment clip commonly overlaps a
        # lower media clip, and the old scan attached the effect to whichever
        # track happened to come first in ``video_tracks``.
        target_track, target_clip = self._resolve_effect_drop_target(
            track_index, frame, is_audio_effect
        )

        if target_clip is None or target_track is None:
            logger.warning("No clip found at drop position")
            event.ignore()
            return

        # Import AddEffectCommand
        from src.features.edit.core.commands import AddEffectCommand

        # Pre-generate the effect_id so we can scroll the properties panel
        # to the new section immediately after apply (the panel reads the
        # id from the emitted signal). Without this the id would only exist
        # inside the command and we'd have to grep the timeline to find it.
        import uuid as _uuid
        new_effect_id = str(_uuid.uuid4())

        # Create and execute AddEffectCommand.
        # NOTE: create() takes track_id (not timeline) — the previous call
        # passed ``timeline=`` which raised TypeError, leaving drag-drop
        # silently broken. Fixed here.
        add_effect_cmd = AddEffectCommand.create(
            track_id=target_track.track_id,
            clip_id=target_clip.clip_id,
            effect_type=effect_type,
            effect_id=new_effect_id,
        )

        # Execute command
        self._execute_command(add_effect_cmd)

        # Clear state
        self._drag_effect_type = None

        event.acceptProposedAction()
        logger.info(
            f"Added {effect_type} effect (id={new_effect_id}) to clip {target_clip.clip_id}"
        )

        # Emit signal for UI update — second arg is now the effect_id (was
        # effect_type) so the controller can scroll_to_effect(id).
        self.effect_added.emit(target_clip.clip_id, new_effect_id)

    def _resolve_effect_drop_target(
        self,
        track_index: int,
        frame: int,
        is_audio_effect: bool,
    ) -> tuple[Optional[EditTrack], Optional[ClipInstance]]:
        """Resolve an effect drop against the track beneath the cursor.

        Effects must target the clip on the visual track where the pointer is
        released.  Looking up clips by frame across all tracks is ambiguous
        when an adjustment clip overlays a media clip and can silently attach
        the effect to the wrong clip.
        """
        target_track = self._track_at_visual_index(track_index)
        expected_type = "audio" if is_audio_effect else "video"
        if target_track is None or target_track.track_type != expected_type:
            return None, None

        target_clip = target_track.get_clip_at(frame)
        if target_clip is None:
            return target_track, None
        return target_track, target_clip

    # ==================== Generator (Adjustment / Solid) Drop ====================

    def _commit_generator_clip_transaction(
        self,
        new_project: Any,
        generator_clip: ClipInstance,
        track_id: str,
    ) -> None:
        """Atomically publish a generator asset and its AddClip command."""
        from src.features.edit.core.generator_transaction_guard import (
            GeneratorTransactionGuard,
        )
        from src.features.edit.core.commands import AddClipCommand

        guard = GeneratorTransactionGuard(self._project)
        try:
            with guard:
                guard.set_project(new_project)
                # Update self._project BEFORE execute so that _add_clip_item →
                # _get_asset_name can find the new generator asset and display
                # the proper name (e.g. "Solid Color 01") instead of asset_id[:12].
                self._project = new_project
                add_cmd = AddClipCommand.create(track_id, generator_clip)
                self._execute_command(add_cmd)
        except Exception:
            # The guard deliberately re-raises so callers can handle the
            # failed drop.  Restore the project before that exception leaves
            # this method; otherwise the newly-created generator asset leaks
            # into the project even though its clip was never committed.
            self._project = guard.project
            raise
        self._project = guard.project
        self.project_changed.emit(self._project)

    def _handle_motion_graphic_generator_drop(
        self, event: QDropEvent, template_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        """Instantiate a reusable HTML generator as a scene-backed clip."""
        from src.features.teaching_animation.core.html_template import HtmlTemplate
        from src.features.teaching_animation.core.model import TeachingSceneDocument
        from src.features.teaching_animation.core.motion_graphic_generator_registry import (
            get_motion_graphic_generator_registry,
        )
        from src.features.teaching_animation.host_adapters import MotionGraphicClipCommand
        from src.features.teaching_animation.render.html_layer.template_promote import (
            apply_html_template,
        )
        from src.features.teaching_animation.scene_store import get_shared_scene_store

        if self._timeline is None or self._project is None:
            event.ignore()
            return
        template = get_motion_graphic_generator_registry().get(template_id)
        if not isinstance(template, HtmlTemplate):
            logger.warning("Motion Graphic generator template unavailable: %s", template_id)
            self._hide_drag_preview()
            event.ignore()
            return

        drop = self._resolve_media_drop_target(event.position(), "video", False)
        if not drop.is_valid and not drop.create_track:
            self._hide_drag_preview()
            event.ignore()
            return
        target_track = (
            self._timeline.get_track_by_id(drop.track_id) if drop.track_id else None
        )
        if target_track is None and drop.create_track:
            add_track_cmd = AddTrackCommand.create("video")
            self._execute_command(add_track_cmd)
            if add_track_cmd._created_track_id:
                target_track = self._get_track_by_id(add_track_cmd._created_track_id)
            if target_track is None:
                target_track = self._find_target_track(0, "video")
        if target_track is None:
            self._hide_drag_preview()
            event.ignore()
            return

        insert_frame = self._calculate_insert_frame(drop.frame)
        try:
            resolved_params = dict(params or template.default_params())
            validation_errors = template.validate_params(resolved_params)
            if validation_errors:
                raise ValueError("; ".join(validation_errors))
            layer = apply_html_template(
                template,
                resolved_params,
                layer_id=f"html_{uuid.uuid4().hex}",
            )
            base = TeachingSceneDocument.create_default(
                name=template.display_name,
                width=template.viewport_width,
                height=template.viewport_height,
                fps_num=template.fps_num,
                fps_den=template.fps_den,
                duration_frames=template.duration_frames,
            )
            scene = replace(
                base,
                elements=(),
                animations=(),
                html_layer=layer,
                metadata={
                    **dict(base.metadata),
                    "motion_graphic_generator": {
                        "template_id": template.template_id,
                        "version": template.version,
                        "params": resolved_params,
                        "params_schema": [
                            param.to_dict() for param in template.params_schema
                        ],
                    },
                },
            )
            new_project, asset_id = self._project.create_motion_graphic_asset(
                scene, name=template.display_name,
            )
            get_shared_scene_store().register(scene)
            command = MotionGraphicClipCommand(
                scene,
                insert_frame,
                scene.duration_frames,
                target_track.track_id,
                "overwrite",
                asset_id=asset_id,
            )
            self._execute_command(command)
            self._project = new_project
            self.project_changed.emit(new_project)
            self._hide_drag_preview()
            self._drag_generator_type = None
            self._drag_motion_graphic_generator_params = {}
            self._drag_motion_graphic_generator_id = None
            event.acceptProposedAction()
            logger.info(
                "Created Motion Graphic generator clip %s from template %s",
                command._clip_id, template_id,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Motion Graphic generator drop failed: %s", exc)
            self._hide_drag_preview()
            self._drag_generator_type = None
            self._drag_motion_graphic_generator_params = {}
            self._drag_motion_graphic_generator_id = None
            event.ignore()

    def _handle_generator_drag_move(self, event: QDragMoveEvent) -> None:
        """Accept generator drops on valid video targets."""
        if self._timeline is None:
            event.ignore()
            return

        drop = self._resolve_media_drop_target(event.position(), "video", False)
        if drop.is_valid or drop.create_track:
            event.acceptProposedAction()
        else:
            event.ignore()

    def _handle_generator_drop(self, event: QDropEvent) -> None:
        """Handle drop of a generator template (Adjustment Clip / Solid Color).

        Unlike ``_handle_effect_drop`` (which attaches an effect to an existing
        clip), this CREATES a new clip. The generator's source asset is built
        via the EditProject factory (``create_adjustment_clip`` /
        ``create_solid_color_asset``), then a ``ClipInstance`` referencing it
        is added to the resolved video track via ``AddClipCommand``.
        """
        import uuid

        if self._timeline is None:
            event.ignore()
            return

        generator_type = self._drag_generator_type
        if not generator_type:
            event.ignore()
            return

        if generator_type.startswith("motion_graphic_template:"):
            self._handle_motion_graphic_generator_drop(
                event,
                generator_type.split(":", 1)[1],
                self._drag_motion_graphic_generator_params,
            )
            return

        if self._project is None:
            logger.warning("Generator drop: no project set, cannot create asset")
            self._hide_drag_preview()
            self._drag_generator_type = None
            event.ignore()
            return

        # Resolve drop target (generators are video clips).
        scene_pos = self.mapToScene(event.position().toPoint())
        locked_ids = self._get_locked_track_ids()
        self._configure_layout_engine()
        drop = self._layout_engine.resolve_drop_target(
            scene_pos.x(), scene_pos.y(), "video", locked_ids
        )

        if not drop.is_valid and not drop.create_track:
            logger.debug(f"Generator drop: invalid target, reason={drop.reason}")
            self._hide_drag_preview()
            self._drag_generator_type = None
            event.ignore()
            return

        # Resolve / create target video track (mirrors asset-drop path).
        target_track = None
        if drop.track_id:
            target_track = self._timeline.get_track_by_id(drop.track_id)
        if target_track is None and drop.create_track:
            add_track_cmd = AddTrackCommand.create("video")
            self._execute_command(add_track_cmd)
            if add_track_cmd._created_track_id:
                target_track = self._get_track_by_id(add_track_cmd._created_track_id)
            if target_track is None:
                target_track = self._find_target_track(0, "video")
        if target_track is None:
            logger.warning("Generator drop: no target video track resolved")
            self._hide_drag_preview()
            self._drag_generator_type = None
            event.ignore()
            return

        insert_frame = self._calculate_insert_frame(drop.frame)

        # Default generator duration: 5 seconds at timeline fps (clamped to ≥1).
        tl_fps = float(self._timeline.fps) if self._timeline.fps else EMPTY_TIMELINE_FPS
        duration_frames = max(1, int(round(tl_fps * 5.0)))

        # FX-H03: build the generator asset AND the referencing clip as ONE
        # atomic project+timeline transaction, so undo can remove the asset
        # when the clip is gone (no orphan assets polluting the saved file).
        # The transaction returns the new project (asset + bin entry) and the
        # clip; we apply both and stash the asset_id on the command for undo.
        clip_id = str(uuid.uuid4())
        try:
            from src.features.edit.core.generator_clip_transaction import (
                build_generator_clip_transaction,
            )
            color_rgb = (0.0, 0.6, 0.8) if generator_type == "solid_color" else (0.0, 0.0, 0.0)
            txn = build_generator_clip_transaction(
                self._project,
                self._timeline,
                generator_type=generator_type,
                track_id=target_track.track_id,
                timeline_start=insert_frame,
                timeline_duration=duration_frames,
                clip_id=clip_id,
                color_rgb=color_rgb,
            )
            new_project = txn.project
            generator_clip = txn.clip
        except ValueError as exc:
            logger.warning(f"Generator drop: transaction rejected: {exc}")
            self._hide_drag_preview()
            self._drag_generator_type = None
            event.ignore()
            return
        except Exception as exc:  # noqa: BLE001 — defensive: never crash UI
            logger.error(f"Generator drop: transaction failed: {exc}")
            self._hide_drag_preview()
            self._drag_generator_type = None
            event.ignore()
            return

        # Publish project asset/bin only after AddClip succeeds. This closes
        # the old two-phase gap where a failed command left an orphan asset,
        # and consumes txn.clip verbatim (including its stable clip_id).
        try:
            self._commit_generator_clip_transaction(
                new_project,
                generator_clip,
                target_track.track_id,
            )
        except Exception as exc:  # noqa: BLE001 — drop must fail atomically
            logger.error("Generator drop: AddClip transaction failed: %s", exc)
            self._hide_drag_preview()
            self._drag_generator_type = None
            event.ignore()
            return

        # Select the new clip so the properties panel opens immediately.
        # Mirrors the mouse-click selection path (_on_clip_clicked).
        try:
            new_selection = self._selection.with_clip_selected(
                clip_id,
                target_track.track_id,
                self._timeline.timeline_id,
                replace=True,
            )
            self.set_selection(new_selection)
        except Exception:  # noqa: BLE001 — selection is best-effort, never block drop
            pass

        # Clear state.
        self._hide_drag_preview()
        self._drag_generator_type = None
        event.acceptProposedAction()
        logger.info(
            f"Created {generator_type} clip (id={clip_id}) on track "
            f"{target_track.name} at frame {insert_frame}"
        )

    # ==================== Drag Preview Helpers ====================

    def _get_locked_track_ids(self) -> set[str]:
        """Return set of currently locked track IDs from the timeline model."""
        if self._timeline is None:
            return set()
        locked = set()
        for track in self._timeline.all_tracks:
            if getattr(track, 'is_locked', False):
                locked.add(track.track_id)
        return locked

    def _prefer_lazy_default_track(
        self,
        drop: TimelineDropTarget,
        media_type: str,
        timeline_was_missing: bool,
    ) -> TimelineDropTarget:
        """Keep the first lazy drop on the default track of its media type.

        Before a timeline exists, the drag move handler has no track geometry
        to hit-test.  After the drop creates the default V1/A1 pair, a cursor
        over the otherwise empty part of the matching area would normally
        produce ``create_track=True``.  Creating that extra track makes the
        first clip appear at the top of the video stack instead of on V1.
        For this one lazy-creation path, use the track that was just created;
        explicit new-track drops on an existing timeline remain unchanged.
        """
        if not timeline_was_missing or not drop.create_track or self._timeline is None:
            return drop

        target_type = (
            "video"
            if media_type in ("video", "image", "comp_clip", "motion_graphic")
            else "audio"
        )
        tracks = (
            self._timeline.video_tracks
            if target_type == "video"
            else self._timeline.audio_tracks
        )
        if not tracks:
            return drop

        default_track = tracks[0]
        return replace(
            drop,
            track_id=default_track.track_id,
            track_type=target_type,
            is_valid=True,
            create_track=False,
            reason="Using the default track created for the first media drop",
        )

    def _is_track_locked(self, track_id: str) -> bool:
        """Check if a specific track is locked."""
        return track_id in self._get_locked_track_ids()

    def _get_clip_id_at_pos(self, scene_x: float, scene_y: float, track_id: str) -> str | None:
        """Callback for resolve_timeline_hit to check for clip at position."""
        for item in self.scene().items(QPointF(scene_x, scene_y)):
            from src.features.edit.ui.timeline_items import ClipItem
            if isinstance(item, ClipItem) and item.track_id == track_id:
                return item.clip_id
        return None

    def _get_track_and_frame_from_pos(self, pos: QPointF) -> tuple[int, int]:
        """
        Calculate track index and frame from scene position.

        Args:
            pos: Scene position ( QPointF)

        Returns:
            (track_index, frame) tuple
        """
        # Calculate frame (exclude header width)
        x = pos.x() - TRACK_HEADER_WIDTH
        frame = int(x / self._pixels_per_frame)
        frame = max(0, frame)

        # Calculate track index from the real cumulative layout. Ruler/header
        # and gaps deliberately return -1 so drops do not silently hit V1.
        track_index = self._track_index_at_y(pos.y())

        return track_index, frame

    def _get_target_track_type(self, asset: MediaAsset) -> str:
        """
        Determine target track type based on asset type.

        Args:
            asset: MediaAsset being dragged

        Returns:
            'video' or 'audio' track type
        """
        if asset.media_type == "audio":
            return "audio"
        else:
            # Video and image assets go to video tracks
            return "video"

    def _find_target_track(self, track_index: int, track_type: str) -> Optional[EditTrack]:
        """
        Find appropriate track for drop using area-aware hit testing.

        If the hit track type doesn't match the media type, the drop is
        invalid (returns None) rather than silently redirecting.

        Args:
            track_index: Visual track index
            track_type: 'video' or 'audio'

        Returns:
            EditTrack if found, None otherwise
        """
        if self._timeline is None:
            return None
        if track_index < 0:
            return None

        locked_ids = self._get_locked_track_ids()

        # Use layout engine's all_ordered_tracks for visual index lookup
        self._configure_layout_engine()
        all_ordered = self._layout_engine.all_ordered_tracks()
        if track_index < len(all_ordered):
            tinfo = all_ordered[track_index]
            # Verify media type matches
            if tinfo.track_type != track_type:
                # Wrong area: audio dropped on video area or vice versa
                # Drop is invalid — do NOT silently redirect
                return None
            # Skip locked tracks — find first unlocked track of matching type
            track = self._timeline.get_track_by_id(tinfo.track_id)
            if track and track.track_id not in locked_ids:
                return track
            # Track at index is locked; fall through to find another
            tracks = self._timeline.video_tracks if track_type == "video" else self._timeline.audio_tracks
            for t in tracks:
                if t.track_id not in locked_ids:
                    return t
            return None

        # Fallback: if index is beyond all tracks, try first matching, unlocked type
        tracks = self._timeline.video_tracks if track_type == "video" else self._timeline.audio_tracks
        for t in tracks:
            if t.track_id not in locked_ids:
                return t
        return None

    def _get_track_by_id(self, track_id: str) -> Optional[EditTrack]:
        """
        Get track by track ID.

        Args:
            track_id: Track ID to find

        Returns:
            EditTrack if found, None otherwise
        """
        if self._timeline is None:
            return None

        for track in self._timeline.all_tracks:
            if track.track_id == track_id:
                return track

        return None

    def _find_free_frame_on_track(
        self, track: EditTrack, preferred_frame: int, duration: int,
    ) -> int:
        """
        Find the nearest free start frame on *track* for a clip of *duration* frames.

        Tries *preferred_frame* first.  If it overlaps existing clips, scans
        forward (after existing clips) until a gap is found.
        """
        new_end = preferred_frame + duration
        # Check if preferred_frame is free
        overlaps = False
        for clip in track.clips:
            if preferred_frame < clip.timeline_end and new_end > clip.timeline_start:
                overlaps = True
                break
        if not overlaps:
            return preferred_frame

        # Find the end of the overlapping clip, then keep scanning forward
        candidate = preferred_frame
        for _ in range(50):  # safety limit
            candidate_end = candidate + duration
            blocked = False
            for clip in track.clips:
                if candidate < clip.timeline_end and candidate_end > clip.timeline_start:
                    candidate = clip.timeline_end
                    blocked = True
                    break
            if not blocked:
                return candidate
        return candidate

    def _find_or_create_audio_track(self) -> Optional[EditTrack]:
        """
        Find the first existing audio track or create one if none exists.

        参考 kdenlive timeline model: 视频素材拖入时间线时自动提取
        音频并放到独立的音频轨道上。

        Returns:
            EditTrack for audio, or None if creation fails
        """
        if self._timeline is None:
            return None

        # Try to find an existing audio track
        if self._timeline.audio_tracks:
            return self._timeline.audio_tracks[0]

        # Create a new audio track via AddTrackCommand
        add_track_cmd = AddTrackCommand.create("audio")
        self._execute_command(add_track_cmd)

        if add_track_cmd._created_track_id:
            track = self._get_track_by_id(add_track_cmd._created_track_id)
            if track:
                logger.info(f"_find_or_create_audio_track: created new audio track {track.name}")
                return track

        logger.warning("_find_or_create_audio_track: failed to create audio track")
        return None

    def _create_default_timeline(
        self,
        first_asset: Optional[MediaAsset] = None,
    ) -> Optional[TimelineSequence]:
        """
        Create a default timeline with one video track and one audio track.

        This is called when assets are dropped but no timeline exists. A video
        first asset establishes the sequence frame-rate so source cadence is
        preserved instead of silently forcing media into the default timebase.

        Returns:
            TimelineSequence with default settings, or None if creation fails
        """
        import uuid

        # Create default video track
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

        # Create default audio track
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

        # The first video establishes the sequence cadence. This same helper
        # also handles the provisional empty 25fps sequence created before a
        # project has any imported media.
        timeline_fps = self._timeline_fps_for_first_asset(first_asset)
        format_settings = self._media_pool_preview_settings(first_asset)

        timeline = TimelineSequence(
            timeline_id=str(uuid.uuid4()),
            # Keep lazy creation consistent with NewTimelineDialog, whose
            # first generated name is "Timeline 1".
            name="Timeline 1",
            fps=timeline_fps,
            width=1920,
            height=1080,
            sample_rate=48000,
            format_settings=format_settings,
            video_tracks=(v1_track,),
            audio_tracks=(a1_track,),
            markers=(),
        )

        logger.info(
            f"_create_default_timeline: created timeline '{timeline.name}' "
            f"at {float(timeline.fps):.3f}fps with V1 and A1 tracks"
        )
        return timeline

    @staticmethod
    def _media_pool_preview_settings(asset: Optional[MediaAsset]) -> dict[str, Any]:
        """Return a random source frame reference for a media-pool preview."""
        if asset is None or asset.media_type not in ("video", "image"):
            return {}

        total_frames = 0
        if asset.media_type == "video":
            try:
                total_frames = int(asset.total_frames or 0)
            except (TypeError, ValueError):
                total_frames = 0
            if total_frames <= 0 and asset.duration_seconds and asset.fps:
                total_frames = max(1, int(round(asset.duration_seconds * asset.fps)))

        frame_index = random.randrange(total_frames) if total_frames > 0 else 0
        return {
            MEDIA_POOL_PREVIEW_ASSET_ID_KEY: asset.asset_id,
            MEDIA_POOL_PREVIEW_FRAME_KEY: frame_index,
        }

    def _ensure_media_pool_preview_source(self, asset: MediaAsset) -> bool:
        """Attach a preview source to a timeline that does not have one yet."""
        if self._timeline is None:
            return False
        settings = dict(self._timeline.format_settings or {})
        existing_asset_id = settings.get(MEDIA_POOL_PREVIEW_ASSET_ID_KEY)
        existing_frame = settings.get(MEDIA_POOL_PREVIEW_FRAME_KEY)
        if existing_asset_id and existing_frame is not None:
            return False

        preview_settings = self._media_pool_preview_settings(asset)
        if not preview_settings:
            return False
        settings.update(preview_settings)
        self._timeline = replace(self._timeline, format_settings=settings)
        self.load_timeline(self._timeline)
        self.timeline_changed.emit(self._timeline)
        return True

    def _timeline_fps_for_first_asset(self, asset: Optional[MediaAsset]) -> Rational:
        """Return a precise sequence FPS for the first video asset."""
        if asset is None or asset.media_type != "video":
            return Rational(25, 1)

        metadata = asset.metadata or {}
        try:
            numerator = int(metadata.get("fps_num", 0) or 0)
            denominator = int(metadata.get("fps_den", 0) or 0)
            if numerator > 0 and denominator > 0:
                return Rational(numerator, denominator)
        except (TypeError, ValueError):
            pass

        raw_fps = asset.fps
        if raw_fps is None:
            return Rational(25, 1)
        if isinstance(raw_fps, Rational):
            return raw_fps

        try:
            fps_value = float(raw_fps)
        except (TypeError, ValueError):
            return Rational(25, 1)
        if not math.isfinite(fps_value) or fps_value <= 0.0:
            return Rational(25, 1)

        # Floats such as 23.976/29.97/59.94 often originated from an exact
        # 1001-based stream rate. Restore that common cadence before falling
        # back to a bounded rational approximation.
        for numerator, denominator in (
            (24000, 1001),
            (30000, 1001),
            (60000, 1001),
            (120000, 1001),
        ):
            if abs(fps_value - numerator / denominator) < 0.002:
                return Rational(numerator, denominator)

        fps_fraction = Fraction(str(fps_value)).limit_denominator(100000)
        return Rational(fps_fraction.numerator, fps_fraction.denominator)

    def _timeline_is_empty(self, timeline: TimelineSequence) -> bool:
        """Return whether a sequence has no clips and may adopt first-media FPS."""
        tracks = tuple(timeline.video_tracks) + tuple(timeline.audio_tracks)
        return all(not track.clips for track in tracks)

    def _ensure_first_asset_timeline_settings(self, first_asset: MediaAsset) -> bool:
        """Create or conform an empty sequence before its first media insert."""
        if self._timeline is None or self._provisional_timeline:
            logger.info("Timeline drop: creating default sequence from first asset")
            self._timeline = self._create_default_timeline(first_asset)
            if self._timeline is None:
                logger.warning("Timeline drop: failed to create default timeline")
                return False
            self._provisional_timeline = False
            self.load_timeline(self._timeline)
            self.timeline_changed.emit(self._timeline)
            logger.info(
                "Timeline drop: created default timeline '%s' at %.3ffps",
                self._timeline.name,
                float(self._timeline.fps),
            )
            return True

        # A manually created timeline can exist without a preview source. Set
        # it on the first media drop as well, regardless of whether the
        # sequence already contains clips.
        self._ensure_media_pool_preview_source(first_asset)

        if not self._timeline_is_empty(self._timeline):
            return True

        target_fps = self._timeline_fps_for_first_asset(first_asset)
        if float(self._timeline.fps) == float(target_fps):
            return True

        self._timeline = replace(self._timeline, fps=target_fps)
        self.load_timeline(self._timeline)
        self.timeline_changed.emit(self._timeline)
        logger.info(
            "Timeline drop: conformed empty timeline '%s' to first-video %.3ffps",
            self._timeline.name,
            float(target_fps),
        )
        return True

    def _calculate_insert_frame(self, frame: int, *, show_indicator: bool = False) -> int:
        """Use the same snap calculation for a drop and its preview."""
        if self._timeline is not None:
            snap_points = () if self._snap_manager else self._get_snap_points()
            frame, _ = self._snap_frame(frame, snap_points, show_indicator=show_indicator)
        return max(0, frame)

    def _get_asset_duration_frames(self, asset: MediaAsset, *, fps: float | None = None) -> int:
        """Get the asset's duration at the current or prospective timeline rate."""
        if fps is None:
            fps = float(self._timeline.fps) if self._timeline else EMPTY_TIMELINE_FPS
        duration = asset.duration_seconds
        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 and fps > 0:
            return max(1, round(duration * fps))

        frame_count = asset.total_frames
        if isinstance(frame_count, int) and frame_count > 0:
            source_rate = asset.fps
            source_fps = float(source_rate) if isinstance(source_rate, (int, float, Rational)) else fps
            if math.isfinite(source_fps) and source_fps > 0 and fps > 0:
                return max(1, round(frame_count * fps / source_fps))
            return frame_count

        return max(1, int(fps * 5)) if fps > 0 else 100

    def _get_asset_source_duration_frames(self, asset: MediaAsset) -> int:
        """
        Get asset duration in source frames.

        Source duration must stay in the media's native frame domain so
        waveform ranges, trim limits, and timeline-to-source mapping stay sane
        when asset fps differs from timeline fps.
        """
        if asset.total_frames is not None:
            return max(1, int(asset.total_frames))

        asset_fps = asset.fps
        if asset.duration_seconds is not None and asset_fps and asset_fps > 0:
            return max(1, int(round(asset.duration_seconds * asset_fps)))

        return self._get_asset_duration_frames(asset)

    def _get_asset_playback_speed(self, asset: MediaAsset, timeline_duration: int, source_duration: int) -> float:
        """
        Return the user retime speed for a newly inserted clip.

        Source and timeline FPS are mapped independently by
        ``timeline_frame_mapper``. Deriving speed from frame counts here would
        apply the mixed-FPS conversion twice, making normal media run too fast
        and clamp at its last frame.
        """
        del asset, timeline_duration, source_duration
        return 1.0

    def _add_asset_as_clip(self, asset: MediaAsset, track: Optional[EditTrack], frame: int,
                           *, source_range: Optional[tuple[int, int]] = None,
                           stream_intent: str = "av") -> bool:
        """
        Add asset as clip to timeline.

        For video files with audio, atomically inserts both the video clip
        and a linked audio clip at the same timeline_start using
        InsertLinkedAssetCommand.  This guarantees:
        - No A/V desync (audio never silently moved to a different frame).
        - No half-finished state (either both succeed or neither changes timeline).

        VP-B08: ``source_range`` (Out-inclusive) restricts the insert to the
        viewer's current source window — it rides the same subclip time-domain
        conversion (cross-fps timeline duration).  ``stream_intent`` ("av" /
        "video" / "audio") forces the stream gating for source-monitor drags.

        Args:
            asset: MediaAsset to add
            track: Target track
            frame: Start frame position

        Returns:
            True if clip was added successfully, False otherwise
        """
        import uuid

        if track is None:
            logger.warning(f"_add_asset_as_clip: track is None, cannot add asset {asset.asset_id}")
            return False

        if self._timeline is None:
            logger.warning(f"_add_asset_as_clip: timeline is None, cannot add asset {asset.asset_id}")
            return False

        if getattr(asset, "is_motion_graphic", False):
            # Media Pool drags must preserve the scene-backed clip contract;
            # inserting a generic ClipInstance here would lose the graphics
            # manifest and make the virtual asset look like a missing file.
            from src.features.teaching_animation.host_adapters import MotionGraphicClipCommand
            from src.features.teaching_animation.scene_store import get_shared_scene_store

            scene_id = str(asset.metadata.get("scene_id", ""))
            scene = get_shared_scene_store().get(scene_id) if scene_id else None
            if scene is None:
                logger.warning(
                    "_add_asset_as_clip: Motion Graphic scene is unavailable: %s",
                    scene_id,
                )
                return False
            duration_frames = max(1, self._get_asset_duration_frames(asset))
            command = MotionGraphicClipCommand(
                scene,
                max(0, int(frame)),
                duration_frames,
                track.track_id,
                "overwrite",
                asset_id=asset.asset_id,
            )
            try:
                self._execute_command(command)
            except (RuntimeError, ValueError) as exc:
                logger.warning("_add_asset_as_clip: Motion Graphic insert failed: %s", exc)
                return False
            return command._clip_id is not None

        # Check if asset is a subclip (scene cut sub-clip from media pool)
        is_subclip = bool(asset.metadata.get("is_subclip", False))
        if source_range is not None:
            # VP-B08: Source 拖放的显式源窗口走 subclip 同款时间域换算
            is_subclip = True
            subclip_source_start = int(source_range[0])
            source_duration_frames = (
                int(source_range[1]) - int(source_range[0]) + 1)  # Out 包含
        elif is_subclip:
            subclip_source_start = int(asset.metadata.get("source_start", 0))
            subclip_source_duration = int(asset.metadata.get("source_duration", 0))
            source_duration_frames = (
                subclip_source_duration if subclip_source_duration > 0
                else self._get_asset_source_duration_frames(asset)
            )
        else:
            subclip_source_start = 0
            source_duration_frames = self._get_asset_source_duration_frames(asset)
        if is_subclip:
            # Timeline duration: source range mapped to timeline fps
            if self._timeline is not None:
                tl_fps = float(self._timeline.fps) if hasattr(self._timeline.fps, 'num') else self._timeline.fps
                asset_fps = asset.fps or tl_fps
                if asset_fps > 0 and tl_fps > 0:
                    duration_frames = max(1, int(round(source_duration_frames * tl_fps / asset_fps)))
                else:
                    duration_frames = source_duration_frames
            else:
                duration_frames = source_duration_frames
        else:
            duration_frames = self._get_asset_duration_frames(asset)
            source_duration_frames = self._get_asset_source_duration_frames(asset)

        playback_speed = self._get_asset_playback_speed(
            asset,
            duration_frames,
            source_duration_frames,
        )
        source_fps_num: int | None = None
        source_fps_den: int | None = None
        timeline_fps_num: int | None = None
        timeline_fps_den: int | None = None
        if asset.fps and asset.fps > 0:
            source_fps = self._timeline_fps_for_first_asset(asset)
            source_fps_num = source_fps.num
            source_fps_den = source_fps.den
        if self._timeline is not None:
            timeline_fps_num = self._timeline.fps.num
            timeline_fps_den = self._timeline.fps.den

        logger.debug(
            f"_add_asset_as_clip: asset={asset.asset_id}, track={track.track_id}, "
            f"frame={frame}, duration={duration_frames}, subclip={is_subclip}"
        )

        # Check if asset has audio（F2.1：Source Patch 的 audio_enabled 并入
        # ——关音频的有声源按 video-only 落轨，不再强制成对）
        patch = self._source_patch_for(asset.asset_id)
        has_audio = (
            asset.media_type == "video"
            and hasattr(asset, 'audio_channels')
            and asset.audio_channels is not None
            and asset.audio_channels > 0
        )
        # W02：蓝徽章显式关闭与素材级流开关同语义（契约 D2）——拖放的
        # 视频落点尊重鼠标显式轨道，音频配对侧走源目的轨解析。
        video_off = (
            not patch.video_enabled
            or self.source_destination_stream_disabled("video"))
        audio_off = (
            not patch.audio_enabled
            or self.source_destination_stream_disabled("audio"))
        if stream_intent == "video":
            audio_off = True   # VP-B08: Source 拖放只视频
        elif stream_intent == "audio":
            video_off = True   # VP-B08: Source 拖放只音频
        audio_destination = self.resolve_source_destination_track_id("audio")
        if has_audio and audio_off and video_off:
            logger.warning(
                "_add_asset_as_clip: both source patch streams off for %s",
                asset.asset_id)
            return False
        effective_has_audio = has_audio and patch.audio_enabled and not audio_off
        if (
            asset.media_type == "video"
            and video_off
            and not effective_has_audio
        ):
            # 视频源关 video 且无有效音频（无声源或 audio 也关）——无流可落
            logger.warning(
                "_add_asset_as_clip: no enabled stream for video asset %s",
                asset.asset_id)
            return False

        # F2.1：video patch 关闭的有声源按 audio-only 落音频目标轨
        # （与源监视器插入/覆盖的 audio-only 语义一致）。
        if has_audio and not audio_off and video_off:
            audio_track_id = audio_destination
            if not audio_track_id:
                logger.warning(
                    "_add_asset_as_clip: audio-only patch but no audio "
                    "target track for %s", asset.asset_id)
                return False
            audio_only_clip = ClipInstance(
                clip_id=str(uuid.uuid4()),
                asset_id=asset.asset_id,
                track_id=audio_track_id,
                timeline_start=frame,
                timeline_duration=duration_frames,
                source_start=subclip_source_start,
                source_duration=source_duration_frames,
                speed=playback_speed,
            )
            try:
                self._execute_command(
                    AddClipCommand.create(audio_track_id, audio_only_clip))
            except ValueError as e:
                logger.warning(
                    f"_add_asset_as_clip: audio-only insert failed: {e}")
                return False
            return True

        # Pre-generate clip IDs for linking
        video_clip_id = str(uuid.uuid4())
        audio_clip_id = str(uuid.uuid4()) if effective_has_audio else None

        # Create video ClipInstance
        video_clip = ClipInstance(
            clip_id=video_clip_id,
            asset_id=asset.asset_id,
            track_id=track.track_id,
            timeline_start=frame,
            timeline_duration=duration_frames,
            source_start=subclip_source_start,
            source_duration=source_duration_frames,
            speed=playback_speed,
            linked_clip_id=audio_clip_id,
            _source_fps_num=source_fps_num,
            _source_fps_den=source_fps_den,
            _timeline_fps_num=timeline_fps_num,
            _timeline_fps_den=timeline_fps_den,
        )

        if effective_has_audio and audio_clip_id:
            # P3: Atomic A/V insertion using InsertLinkedAssetCommand.
            # Both video and audio clips will start at the same timeline_start.
            # If audio track has overlap, a new track is auto-created (no desync).
            # If the command fails, no clips are added (no half-finished state).
            # W02：音频配对落轨走源目的轨（None 时命令原子自建轨）。
            audio_track_id = audio_destination
            audio_clip = ClipInstance(
                clip_id=audio_clip_id,
                asset_id=asset.asset_id,
                # The command allocates an audio track atomically when the
                # timeline does not have one yet.  ClipInstance still needs a
                # non-empty placeholder track ID until that command runs.
                track_id=audio_track_id or "__pending_audio_track__",
                timeline_start=frame,  # SAME start as video — no desync
                timeline_duration=duration_frames,
                source_start=subclip_source_start,
                source_duration=source_duration_frames,
                speed=playback_speed,
                linked_clip_id=video_clip_id,
                _source_fps_num=source_fps_num,
                _source_fps_den=source_fps_den,
                _timeline_fps_num=timeline_fps_num,
                _timeline_fps_den=timeline_fps_den,
            )

            from src.features.edit.core.commands import InsertLinkedAssetCommand
            cmd = InsertLinkedAssetCommand.create(
                video_track_id=track.track_id,
                video_clip=video_clip,
                audio_track_id=audio_track_id,
                audio_clip=audio_clip,
            )
            try:
                self._execute_command(cmd)
            except ValueError as e:
                logger.warning(f"_add_asset_as_clip: atomic A/V insert failed: {e}")
                return False
        else:
            # No audio — simple video-only insert
            cmd = AddClipCommand.create(track.track_id, video_clip)
            try:
                self._execute_command(cmd)
            except ValueError as e:
                logger.warning(f"_add_asset_as_clip: video insert failed: {e}")
                return False

        logger.info(f"_add_asset_as_clip: successfully added clip {video_clip_id} to track {track.name}")
        return True

    def _hide_drag_preview(self) -> None:
        """Clear both parts of the drop feedback."""
        if self._drag_preview_item is not None:
            self._drag_preview_item.hide_preview()
        if self._snap_indicator is not None:
            self._snap_indicator.hide_indicator()

    # ==================== P3.2: Insert/Overwrite/Append Operations ====================

    def get_timeline(self) -> Optional[TimelineSequence]:
        """
        Get current timeline sequence.

        Returns:
            TimelineSequence or None if not loaded
        """
        return self._timeline

    def get_playhead_frame(self) -> int:
        """
        Get current playhead frame position.

        Returns:
            Frame number
        """
        return self._playhead_frame

    def get_target_track_id(self) -> Optional[str]:
        """
        Get target track ID for insert/overwrite operations (UI-09).

        Returns:
            Track ID of user-selected target track, or first video track as default
        """
        if self._timeline is None:
            return None

        # Return user-selected target track if set
        if self._target_track_id:
            return self._target_track_id

        # Default to first video track
        if self._timeline.video_tracks:
            return self._timeline.video_tracks[0].track_id

        return None

    def get_target_audio_track_id(self) -> Optional[str]:
        """
        Get target audio track ID for audio clip operations (P2: independent from video target).

        Pure query: no side effects, no tracks created.
        Returns None if no audio track exists.

        Returns:
            Track ID of user-selected audio target, or first audio track, or None.
        """
        if self._timeline is None:
            return None

        # Return user-selected audio target if set
        if self._target_audio_track_id:
            # Verify track still exists
            for t in self._timeline.audio_tracks:
                if t.track_id == self._target_audio_track_id:
                    return self._target_audio_track_id

        # Default to first audio track
        if self._timeline.audio_tracks:
            return self._timeline.audio_tracks[0].track_id

        return None

    def ensure_target_audio_track(self) -> Optional[str]:
        """
        Ensure an audio track exists, creating one if needed.

        This is the side-effect variant: only call during command execution
        or when you explicitly want track creation.

        Returns:
            Track ID of the (possibly newly created) audio track, or None.
        """
        existing = self.get_target_audio_track_id()
        if existing is not None:
            return existing

        audio_track = self._find_or_create_audio_track()
        if audio_track is not None:
            return audio_track.track_id

        return None

    def is_track_locked(self, track_id: str) -> bool:
        """B02：宿主可用的公共锁轨查询（委托既有判定，不改语义）。"""
        return self._is_track_locked(track_id)

    # ------------------------------------------------------------------
    # B8: global edit mode (governs media-pool drop semantics)
    # ------------------------------------------------------------------

    def edit_mode(self) -> str:
        """Return the current global edit mode ("overwrite" | "insert")."""
        return self._edit_mode

    def set_edit_mode(self, mode: str) -> None:
        """Set the global edit mode and notify listeners.

        Args:
            mode: "overwrite" (default) or "insert" (ripple at drop point).
        """
        mode = "insert" if mode == "insert" else "overwrite"
        if mode == self._edit_mode:
            return
        self._edit_mode = mode
        self.edit_mode_changed.emit(mode)

    def _resolve_source_fps_metadata(
        self, asset_id: str
    ) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        """R22/F-R03：源/时间线 fps 元数据（num/den 四元组）。

        素材 fps 缺失（合成工程/无探测元数据）时对应项为 None，时长
        helper 退化为纯速度语义。三处源编辑入口共用，避免各自维护一套
        换算。
        """
        source_fps_num = source_fps_den = None
        if self._project is not None:
            asset = self._project.assets.get(asset_id)
            # F2.7：解释覆盖优先（新插入按用户解释的帧率建时基快照）
            from src.features.edit.core.interpretation import (
                effective_asset_fps,
            )
            # W07：工程替身/旧会话对象可能没有 F2.7 覆盖表——缺席按
            # 无解释覆盖处理（getattr 防御，不改真实 EditProject 行为）
            effective_fps = effective_asset_fps(
                asset,
                getattr(self._project, "interpretation_overrides", None)) \
                if asset is not None else None
            if effective_fps and effective_fps > 0:
                from fractions import Fraction
                fps_frac = Fraction(float(effective_fps)).limit_denominator(100000)
                source_fps_num = fps_frac.numerator
                source_fps_den = fps_frac.denominator
        timeline_fps_num = timeline_fps_den = None
        if self._timeline is not None:
            from fractions import Fraction
            tl_fps = Fraction(float(self._timeline.fps)).limit_denominator(100000)
        timeline_fps_num = tl_fps.numerator
        timeline_fps_den = tl_fps.denominator
        return source_fps_num, source_fps_den, timeline_fps_num, timeline_fps_den

    def _source_patch_for(self, asset_id: str) -> SourcePatchSpec:
        """F2.1：素材生效的 Source Patch（无工程/无覆盖 = 默认全开直通）。

        插入/覆盖/追加/拖放四个入口共用，决定有声源是否成对落轨
        （video_enabled/audio_enabled）。声道映射（audio_channel_map）
        由渲染消费侧取用，不经本方法。
        """
        if self._project is not None:
            # W07：同上——覆盖表缺席按默认源补丁（getattr 防御）
            return patch_for_asset(
                getattr(self._project, "source_patch_overrides", None)
                or {}, asset_id)
        return DEFAULT_SOURCE_PATCH

    def _apply_source_edit_plan(
        self,
        asset_id: str,
        in_frame: int,
        out_frame: int,
        playhead: int,
        source_fps_num: int | None,
        source_fps_den: int | None,
        timeline_fps_num: int | None,
        timeline_fps_den: int | None,
    ) -> Optional[tuple]:
        """F2.2：三/四点/backtime 判定并解析落点（读时间线 In/Out range）。

        无 range → 播放头三点；仅 In → 落 In；仅 Out → backtime（恰好
        结束在 Out）；双边四点（等长直落，冲突弹三选：变速填充/裁剪源/
        取消）。返回 ``(playhead, in_frame, out_frame, speed)``；用户取消
        或所选策略不可行返回 None（调用方按失败处理并已给状态栏反馈）。
        """
        from src.features.edit.core.timeline_ops import (
            SourceEditConflict,
            resolve_source_edit_plan,
        )

        range_start = getattr(self._timeline, "range_start", None)
        range_end = getattr(self._timeline, "range_end", None)
        if range_start is None and range_end is None:
            return playhead, in_frame, out_frame, 1.0

        def _resolve(fit_policy: str = "ask"):
            return resolve_source_edit_plan(
                in_frame, out_frame, range_start, range_end,
                source_fps_num=source_fps_num,
                source_fps_den=source_fps_den,
                timeline_fps_num=timeline_fps_num,
                timeline_fps_den=timeline_fps_den,
                fit_policy=fit_policy,
                default_playhead=playhead,
            )

        try:
            result = _resolve()
            if isinstance(result, SourceEditConflict):
                choice = self._ask_four_point_conflict(result)
                if choice is None:
                    return None
                result = _resolve(fit_policy=choice)
        except ValueError as exc:
            self.status_message_requested.emit(str(exc))
            return None

        messages = {
            "backtime": self.tr(
                "Backtime：片段恰好结束于时间线 Out（{end} 帧）").format(
                end=result.playhead + result.timeline_duration - 1),
            "four_point_match": self.tr(
                "四点编辑：时长匹配（{n} 帧）").format(
                n=result.timeline_duration),
            "four_point_fit": self.tr(
                "四点编辑：变速填充（源 {m} 帧 → 目标 {n} 帧，"
                "speed {s}）").format(
                m=result.source_out - result.source_in + 1,
                n=result.timeline_duration, s=round(result.speed, 4)),
            "four_point_trim": self.tr(
                "四点编辑：源窗裁剪至 {n} 帧").format(
                n=result.timeline_duration),
        }
        message = messages.get(result.mode)
        if message:
            self.status_message_requested.emit(message)
        return result.playhead, result.source_in, result.source_out, result.speed

    def _ask_four_point_conflict(self, conflict) -> Optional[str]:
        """四点时长冲突三选；返回 fit_policy 或 None（取消）。

        目标比源长时裁剪不可选（无法反向延长源窗）；建议速度越界时
        变速不可选。均不可选直接反馈并返回 None。
        """
        from PyQt6.QtWidgets import QMessageBox

        options: list = []
        if conflict.speed_in_range:
            options.append(("fit_speed", self.tr("变速填充（speed {s}）")
                            .format(s=round(conflict.suggested_speed, 4))))
        if conflict.trim_available:
            options.append(("trim_source", self.tr("裁剪源范围")))
        if not options:
            self.status_message_requested.emit(self.tr(
                "四点冲突无可用策略：源 {m} 帧 ≠ 目标 {n} 帧，且变速/裁剪"
                "均不可行（速度越界且目标更长）；请调整时间线 In/Out 或"
                "源范围").format(m=conflict.source_duration,
                                  n=conflict.target_duration))
            return None
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("四点编辑时长冲突"))
        box.setText(self.tr(
            "源片段 {m} 帧 ≠ 时间线范围 {n} 帧。请选择解决方式：").format(
            m=conflict.source_duration, n=conflict.target_duration))
        for policy, label in options:
            button = box.addButton(
                label, QMessageBox.ButtonRole.AcceptRole)
            button.setProperty("fit_policy", policy)
        box.addButton(self.tr("取消"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None:
            return None
        return clicked.property("fit_policy")

    def _insert_asset_as_clip(
        self, asset: MediaAsset, track: EditTrack, frame: int
    ) -> bool:
        """Ripple-insert one dropped asset at *frame* (B8 insert mode).

        Delegates to the source-monitor insert path (InsertClipCommand), so
        sync-locked linked audio follows the ripple exactly like the explicit
        Insert button.  Returns True on success.
        """
        is_subclip = bool(getattr(asset, "metadata", {}).get("is_subclip", False))
        metadata = getattr(asset, "metadata", {}) or {}
        if is_subclip:
            in_frame = int(metadata.get("source_start", 0))
            source_duration = int(metadata.get("source_duration", 0))
            if source_duration <= 0:
                source_duration = self._get_asset_source_duration_frames(asset)
        else:
            in_frame = 0
            source_duration = self._get_asset_source_duration_frames(asset)
        if source_duration <= 0:
            logger.warning(
                "Insert-mode drop: no source duration for asset %s", asset.asset_id
            )
            return False
        return self.insert_clip(
            asset_id=asset.asset_id,
            in_frame=in_frame,
            out_frame=in_frame + source_duration - 1,
            playhead=frame,
            track_id=track.track_id,
        )

    def insert_clip(
        self,
        asset_id: str,
        in_frame: int,
        out_frame: int,
        playhead: Optional[int] = None,
        track_id: Optional[str] = None,
        speed: float = 1.0,
        source_fps_num: int | None = None,
        source_fps_den: int | None = None,
        timeline_fps_num: int | None = None,
        timeline_fps_den: int | None = None,
        stream_intent: str = "av",
    ) -> bool:
        """
        Insert clip at playhead with ripple.

        Args:
            asset_id: Source asset ID
            in_frame: Source start frame
            out_frame: Source end frame (inclusive)
            playhead: Timeline position (defaults to current playhead)
            track_id: Target track (defaults to first video track)
            speed: Playback speed
            source_fps_num: Source fps numerator (optional, for frame mapper)
            source_fps_den: Source fps denominator (optional)
            timeline_fps_num: Timeline fps numerator (optional)
            timeline_fps_den: Timeline fps denominator (optional)
            stream_intent: I04-3 流意图（"av"/"video"/"audio"）。非 "av"
                时覆盖 Source Patch/蓝徽章路由：video 只落视频轨，
                audio 只落音频轨——拖拽意图与监视器插入共享同一条
                planner 判定，insert 分支不再丢掉意图。

        Returns:
            True if successful, False otherwise
        """
        from src.features.edit.core.commands import InsertClipCommand

        if self._timeline is None:
            logger.warning("Cannot insert: no timeline loaded")
            return False

        # Use defaults if not provided
        if playhead is None:
            playhead = self._playhead_frame
        # W02：源编辑默认落轨走蓝徽章源目的轨（无显式路由时回退红 target）。
        video_destination = self.resolve_source_destination_track_id("video")
        audio_destination = self.resolve_source_destination_track_id("audio")
        if track_id is None:
            track_id = video_destination

        # Create and execute command
        # Auto-detect fps metadata if not explicitly provided
        # （F2.7：经 _resolve_source_fps_metadata 统一走解释覆盖优先）
        if source_fps_num is None and self._project is not None:
            fps_meta = self._resolve_source_fps_metadata(asset_id)
            source_fps_num, source_fps_den = fps_meta[0], fps_meta[1]
        if source_fps_num is None and self._project is not None:
            asset = self._project.assets.get(asset_id)
            if asset and asset.fps and asset.fps > 0:
                from fractions import Fraction
                fps_frac = Fraction(asset.fps).limit_denominator(100000)
                source_fps_num = fps_frac.numerator
                source_fps_den = fps_frac.denominator

        if timeline_fps_num is None and self._timeline is not None:
            from fractions import Fraction
            tl_fps = Fraction(float(self._timeline.fps)).limit_denominator(100000)
            timeline_fps_num = tl_fps.numerator
            timeline_fps_den = tl_fps.denominator

        # F2.2：三/四点/backtime 判定（读时间线 In/Out range；显式变速
        # 调用保持既有落点语义，不参与判定）
        if speed == 1.0:
            planned = self._apply_source_edit_plan(
                asset_id, in_frame, out_frame, playhead,
                source_fps_num, source_fps_den,
                timeline_fps_num, timeline_fps_den)
            if planned is None:
                return False
            playhead, in_frame, out_frame, speed = planned

        # R22/F-R03：有声视频的源插入统一走 InsertLinkedAssetRippleCommand
        # ——同步切分+波纹，播放头落在片段内部不再回退 video-only（新素材
        # 无音频），混合帧率时长由统一 helper 求得；纯音频落音频目标轨。
        # F2.1：Source Patch 并入判定——audio_enabled=False 的有声源按
        # video-only 落轨；video_enabled=False 按 audio-only 重定向音频
        # 目标轨；双关则拒绝插入（无流可落）。
        # W02：蓝徽章显式关闭与素材级流开关同语义（契约 D2——任一层
        # 关闭该流即不落）；成对落轨的音频侧也走源目的轨解析。
        asset = self._project.assets.get(asset_id) if self._project else None
        if asset is not None:
            patch = self._source_patch_for(asset_id)
            video_off = (
                not patch.video_enabled
                or self.source_destination_stream_disabled("video"))
            audio_off = (
                not patch.audio_enabled
                or self.source_destination_stream_disabled("audio"))
            # I04-3：显式流意图（拖拽生产端）覆盖 patch 路由——
            # "video" 等价音频侧关、"audio" 等价视频侧关，两者共用
            # 下方的统一判定（配对/单流/拒绝）。
            if stream_intent == "video":
                audio_off = True
            elif stream_intent == "audio":
                video_off = True
            if video_off and audio_off:
                logger.warning(
                    "Insert clip blocked: both source patch streams off "
                    "for asset %s", asset_id)
                return False
            has_audio = (
                asset.media_type == "video"
                and getattr(asset, "audio_channels", None)
            )
            if has_audio and not video_off and not audio_off:
                from src.features.edit.core.commands import (
                    InsertLinkedAssetRippleCommand,
                )

                cmd = InsertLinkedAssetRippleCommand.create(
                    asset_id=asset_id,
                    in_frame=in_frame,
                    out_frame=out_frame,
                    playhead=playhead,
                    video_track_id=track_id,
                    audio_track_id=audio_destination,
                    speed=speed,
                    source_fps_num=source_fps_num,
                    source_fps_den=source_fps_den,
                    timeline_fps_num=timeline_fps_num,
                    timeline_fps_den=timeline_fps_den,
                )
                try:
                    self._execute_command(cmd)
                except ValueError as exc:
                    self.status_message_requested.emit(str(exc))
                    return False
                logger.debug(
                    f"Insert clip (A/V ripple): asset={asset_id}, in={in_frame}, "
                    f"out={out_frame}, playhead={playhead}"
                )
                return True
            if asset.media_type == "audio" or (has_audio and video_off):
                if asset.media_type == "audio" and audio_off:
                    # 纯音频源且音频流被关：无流可落，拒绝
                    logger.warning(
                        "Insert clip blocked: audio stream off for audio "
                        "asset %s", asset_id)
                    return False
                audio_target = audio_destination
                # I04-3：拖拽显式命中的音频目标轨优先于蓝徽章重定向
                # （用户放到哪条轨就插哪条轨；显式非音频轨仍按目的轨走）。
                explicit_track = (self._timeline.get_track_by_id(track_id)
                                  if track_id and self._timeline is not None
                                  else None)
                explicit_is_audio = (
                    explicit_track is not None
                    and getattr(explicit_track, "track_type", "") == "audio")
                if explicit_is_audio:
                    pass  # 保留显式目标
                elif audio_target:
                    track_id = audio_target
                elif has_audio and video_off:
                    # audio-only 路由但无音频目的轨：明确拒绝而非悄悄
                    # 落到视频轨（与双关拒绝同语义）
                    logger.warning(
                        "Insert clip blocked: audio-only routing but no "
                        "audio destination track for asset %s", asset_id)
                    return False
            elif asset.media_type == "video" and not has_audio and video_off:
                # 无声视频源且视频流被关：无流可落，拒绝（与
                # _add_asset_as_clip 的 F2.1 判定对齐）
                logger.warning(
                    "Insert clip blocked: video stream off for silent "
                    "video asset %s", asset_id)
                return False
        elif track_id is None:
            logger.warning("Cannot insert: no target track")
            return False

        # F5.4：VFR 生产绑定——导入时抽取并存于素材 metadata 的 PTS 秒表
        # （仅 VFR 素材携带；CFR 缺省 None 走既有数学零开销）
        _pts_index = None
        if self._project is not None:
            _asset = self._project.assets.get(asset_id)
            _pts_raw = (getattr(_asset, "metadata", None) or {}).get(
                "pts_index") if _asset is not None else None
            if _pts_raw:
                _pts_index = tuple(float(v) for v in _pts_raw)
        if track_id is None:
            logger.warning("Cannot insert: no destination track")
            return False
        cmd = InsertClipCommand.create(
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            playhead=playhead,
            track_id=track_id,
            speed=speed,
            source_fps_num=source_fps_num,
            source_fps_den=source_fps_den,
            timeline_fps_num=timeline_fps_num,
            timeline_fps_den=timeline_fps_den,
            source_pts_index=_pts_index,
        )

        try:
            self._execute_command(cmd)
        except ValueError as exc:
            self.status_message_requested.emit(str(exc))
            return False
        logger.debug(f"Insert clip: asset={asset_id}, in={in_frame}, out={out_frame}, playhead={playhead}")
        return True

    def overwrite_clip(
        self,
        asset_id: str,
        in_frame: int,
        out_frame: int,
        playhead: Optional[int] = None,
        track_id: Optional[str] = None,
        speed: float = 1.0
    ) -> bool:
        """
        Overwrite clip at playhead (no ripple).

        Args:
            asset_id: Source asset ID
            in_frame: Source start frame
            out_frame: Source end frame (inclusive)
            playhead: Timeline position (defaults to current playhead)
            track_id: Target track (defaults to first video track)
            speed: Playback speed

        Returns:
            True if successful, False otherwise
        """
        from src.features.edit.core.commands import OverwriteClipCommand

        if self._timeline is None:
            logger.warning("Cannot overwrite: no timeline loaded")
            return False

        # Use defaults if not provided
        if playhead is None:
            playhead = self._playhead_frame
        # W02：源编辑默认落轨走蓝徽章源目的轨（无显式路由时回退红 target）。
        video_destination = self.resolve_source_destination_track_id("video")
        audio_destination = self.resolve_source_destination_track_id("audio")
        if track_id is None:
            track_id = video_destination

        # F2.2：三/四点/backtime 判定（读时间线 In/Out range；显式变速
        # 调用保持既有落点语义）
        if speed == 1.0:
            fps_meta = self._resolve_source_fps_metadata(asset_id)
            planned = self._apply_source_edit_plan(
                asset_id, in_frame, out_frame, playhead, *fps_meta)
            if planned is None:
                return False
            playhead, in_frame, out_frame, speed = planned

        # FE-B03：有声视频的覆盖编辑同步在音频目标轨放置链接片段（同一
        # playhead），宏内单步 Undo 移除 V/A；纯音频落到音频目标轨。
        # F2.1：Source Patch 并入判定（语义同 insert_clip——audio off =
        # video-only 单轨覆盖；video off = audio-only 重定向；双关拒绝）。
        # W02：蓝徽章显式关闭与素材级流开关同语义（契约 D2）。
        asset = self._project.assets.get(asset_id) if self._project else None
        if asset is not None:
            patch = self._source_patch_for(asset_id)
            video_off = (
                not patch.video_enabled
                or self.source_destination_stream_disabled("video"))
            audio_off = (
                not patch.audio_enabled
                or self.source_destination_stream_disabled("audio"))
            if video_off and audio_off:
                logger.warning(
                    "Overwrite clip blocked: both source patch streams off "
                    "for asset %s", asset_id)
                return False
            if asset.media_type == "audio" or (
                    asset.media_type == "video"
                    and getattr(asset, "audio_channels", None)
                    and video_off
            ):
                if asset.media_type == "audio" and audio_off:
                    logger.warning(
                        "Overwrite clip blocked: audio stream off for audio "
                        "asset %s", asset_id)
                    return False
                audio_target = audio_destination
                if audio_target:
                    track_id = audio_target
                elif asset.media_type == "video":
                    logger.warning(
                        "Overwrite clip blocked: audio-only routing but no "
                        "audio destination track for asset %s", asset_id)
                    return False
                elif asset.media_type == "audio":
                    # 无音频轨的纯音频源：保持既有回退（视频 target 落轨）
                    pass
            elif (asset.media_type == "video"
                    and not getattr(asset, "audio_channels", None)
                    and video_off):
                logger.warning(
                    "Overwrite clip blocked: video stream off for silent "
                    "video asset %s", asset_id)
                return False
            asset_has_audio_for_pair = (
                asset.media_type == "video"
                and getattr(asset, "audio_channels", None)
                and not audio_off
                and not video_off
            )
        else:
            asset_has_audio_for_pair = False
        if track_id is None:
            logger.warning("Cannot overwrite: no destination track")
            return False
        if asset is not None and asset_has_audio_for_pair:
            from src.features.edit.core.commands import MacroCommand

            audio_track_id = audio_destination
            # R22/F-R03：音频侧同样用覆盖命令（切分/修剪既有音床片段），
            # 此前 AddClip 在音床占用位置抛 ValueError 使正常覆盖失败。
            # fps 元数据两侧一致，混合帧率覆盖时长统一。
            (
                source_fps_num,
                source_fps_den,
                timeline_fps_num,
                timeline_fps_den,
            ) = self._resolve_source_fps_metadata(asset_id)
            commands = [
                OverwriteClipCommand.create(
                    asset_id=asset_id,
                    in_frame=in_frame,
                    out_frame=out_frame,
                    playhead=playhead,
                    track_id=track_id,
                    speed=speed,
                    source_fps_num=source_fps_num,
                    source_fps_den=source_fps_den,
                    timeline_fps_num=timeline_fps_num,
                    timeline_fps_den=timeline_fps_den,
                )
            ]
            if audio_track_id:
                audio_clip_id = str(uuid.uuid4())
                commands.append(OverwriteClipCommand.create(
                    asset_id=asset_id,
                    in_frame=in_frame,
                    out_frame=out_frame,
                    playhead=playhead,
                    track_id=audio_track_id,
                    speed=speed,
                    source_fps_num=source_fps_num,
                    source_fps_den=source_fps_den,
                    timeline_fps_num=timeline_fps_num,
                    timeline_fps_den=timeline_fps_den,
                ))
                # OverwriteClipCommand 生成自己的视频/音频片段 ID；宏后
                # 按 (asset, start, track) 回填双向链接
                commands.append(_LinkClipsAfterApplyCommand(
                    asset_id=asset_id,
                    video_track_id=track_id,
                    video_start=playhead,
                    audio_clip_id=audio_clip_id,
                    audio_track_id=audio_track_id,
                ))
            try:
                self._execute_command(MacroCommand.create(
                    "Overwrite clip (A/V)", commands
                ))
            except ValueError as exc:
                self.status_message_requested.emit(str(exc))
                return False
            logger.debug(
                f"Overwrite clip (A/V): asset={asset_id}, in={in_frame}, "
                f"out={out_frame}, playhead={playhead}"
            )
            return True
        # 纯音频源的落轨重定向已在上方 W02 资产块完成（蓝路由优先），
        # 此处不再用红 target 二次覆盖。

        # N18/RF02：非 A/V 分支（无声视频/纯音频）同样带完整 fps 时基
        # ——此前 create 不传 fps，48fps 无声源 24fps 序列 duration 48
        # 且逐帧 1:1 采样（应 duration 24 / 采样 2）。
        (
            source_fps_num,
            source_fps_den,
            timeline_fps_num,
            timeline_fps_den,
        ) = self._resolve_source_fps_metadata(asset_id)
        cmd = OverwriteClipCommand.create(
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            playhead=playhead,
            track_id=track_id,
            speed=speed,
            source_fps_num=source_fps_num,
            source_fps_den=source_fps_den,
            timeline_fps_num=timeline_fps_num,
            timeline_fps_den=timeline_fps_den,
        )

        try:
            self._execute_command(cmd)
        except ValueError as exc:
            self.status_message_requested.emit(str(exc))
            return False
        logger.debug(f"Overwrite clip: asset={asset_id}, in={in_frame}, out={out_frame}, playhead={playhead}")
        return True

    def append_clip(
        self,
        asset_id: str,
        in_frame: int,
        out_frame: int,
        track_id: Optional[str] = None,
        speed: float = 1.0
    ) -> bool:
        """
        Append clip to timeline end.

        Args:
            asset_id: Source asset ID
            in_frame: Source start frame
            out_frame: Source end frame (inclusive)
            track_id: Target track (defaults to first video track)
            speed: Playback speed

        Returns:
            True if successful, False otherwise
        """
        from src.features.edit.core.commands import AppendClipCommand

        if self._timeline is None:
            logger.warning("Cannot append: no timeline loaded")
            return False

        # Use default if not provided
        # W02：源编辑默认落轨走蓝徽章源目的轨（无显式路由时回退红 target）。
        video_destination = self.resolve_source_destination_track_id("video")
        audio_destination = self.resolve_source_destination_track_id("audio")
        if track_id is None:
            track_id = video_destination

        # R22/F-R03：有声视频的追加在 V/A **共同落点**放置链接片段——
        # L = max(V 轨尾, A 轨尾)。此前各轨各用自身末尾，错开的轨尾使
        # 双向链接的两片立即错位（审计实测 V 尾 96/A 尾 48 差 48 帧）。
        # 时长经统一 helper（混合帧率一致）。纯音频落到音频目标轨。
        # F2.1：Source Patch 并入判定（语义同 insert/overwrite）。
        # W02：蓝徽章显式关闭与素材级流开关同语义（契约 D2）。
        asset = self._project.assets.get(asset_id) if self._project else None
        if asset is not None:
            patch = self._source_patch_for(asset_id)
            video_off = (
                not patch.video_enabled
                or self.source_destination_stream_disabled("video"))
            audio_off = (
                not patch.audio_enabled
                or self.source_destination_stream_disabled("audio"))
            if video_off and audio_off:
                logger.warning(
                    "Append clip blocked: both source patch streams off "
                    "for asset %s", asset_id)
                return False
            asset_has_audio_for_pair = (
                asset.media_type == "video"
                and getattr(asset, "audio_channels", None)
                and not audio_off
                and not video_off
            )
            if asset.media_type == "audio" or (
                    asset.media_type == "video"
                    and getattr(asset, "audio_channels", None)
                    and video_off
            ):
                if asset.media_type == "audio" and audio_off:
                    logger.warning(
                        "Append clip blocked: audio stream off for audio "
                        "asset %s", asset_id)
                    return False
                audio_target = audio_destination
                if audio_target:
                    track_id = audio_target
                elif asset.media_type == "video":
                    logger.warning(
                        "Append clip blocked: audio-only routing but no "
                        "audio destination track for asset %s", asset_id)
                    return False
            elif (asset.media_type == "video"
                    and not getattr(asset, "audio_channels", None)
                    and video_off):
                logger.warning(
                    "Append clip blocked: video stream off for silent "
                    "video asset %s", asset_id)
                return False
        else:
            asset_has_audio_for_pair = False
        if track_id is None:
            logger.warning("Cannot append: no destination track")
            return False
        if asset is not None and asset_has_audio_for_pair:
            from src.features.edit.core.commands import AddClipCommand, MacroCommand
            from src.features.edit.core.timeline_ops import (
                timeline_duration_for_source,
            )

            (
                source_fps_num,
                source_fps_den,
                timeline_fps_num,
                timeline_fps_den,
            ) = self._resolve_source_fps_metadata(asset_id)
            audio_track_id = audio_destination

            video_track = self._timeline.get_track_by_id(track_id)
            video_end = max(
                (c.timeline_end for c in video_track.clips), default=0,
            ) if video_track else 0
            audio_track = (
                self._timeline.get_track_by_id(audio_track_id)
                if audio_track_id else None
            )
            audio_end = max(
                (c.timeline_end for c in audio_track.clips), default=0,
            ) if audio_track else 0

            source_duration = out_frame - in_frame + 1
            duration_frames = timeline_duration_for_source(
                source_duration, speed,
                source_fps_num, source_fps_den,
                timeline_fps_num, timeline_fps_den,
            )

            if audio_track is not None:
                # 共同落点：两条轨同一 L 起始，双向链接不互相拉错位
                landing = max(video_end, audio_end)
                video_clip_id = str(uuid.uuid4())
                audio_clip_id = str(uuid.uuid4())
                common = dict(
                    asset_id=asset_id,
                    timeline_start=landing,
                    timeline_duration=duration_frames,
                    source_start=in_frame,
                    source_duration=source_duration,
                    speed=speed,
                    # N18/RF02：完整 fps 时基随片段落库——此前 common 只带
                    # duration 不带 fps，48fps 源 24 帧时长在 24fps 序列
                    # 每时间线帧只采 1 源帧（应 2），播放/导出只消耗前
                    # 一半源窗。
                    _source_fps_num=source_fps_num,
                    _source_fps_den=source_fps_den,
                    _timeline_fps_num=timeline_fps_num,
                    _timeline_fps_den=timeline_fps_den,
                )
                video_clip = ClipInstance(
                    clip_id=video_clip_id, track_id=track_id,
                    linked_clip_id=audio_clip_id, **common,
                )
                audio_clip = ClipInstance(
                    clip_id=audio_clip_id, track_id=audio_track_id,
                    linked_clip_id=video_clip_id, **common,
                )
                commands = [
                    AddClipCommand.create(track_id, video_clip),
                    AddClipCommand.create(audio_track_id, audio_clip),
                    _LinkClipsAfterApplyCommand(
                        asset_id=asset_id,
                        video_track_id=track_id,
                        video_start=landing,
                        audio_clip_id=audio_clip_id,
                        audio_track_id=audio_track_id,
                    ),
                ]
            else:
                commands = [
                    AppendClipCommand.create(
                        asset_id=asset_id,
                        in_frame=in_frame,
                        out_frame=out_frame,
                        track_id=track_id,
                        speed=speed,
                        source_fps_num=source_fps_num,
                        source_fps_den=source_fps_den,
                        timeline_fps_num=timeline_fps_num,
                        timeline_fps_den=timeline_fps_den,
                    )
                ]
            self._execute_command(MacroCommand.create(
                "Append clip (A/V)", commands
            ))
            logger.debug(
                f"Append clip (A/V): asset={asset_id}, in={in_frame}, out={out_frame}"
            )
            return True
        # 纯音频源的落轨重定向已在上方 W02 资产块完成（蓝路由优先），
        # 此处不再用红 target 二次覆盖。

        # N18/RF02：非 A/V 分支同样带完整 fps 时基（同 Overwrite 兜底）
        (
            source_fps_num,
            source_fps_den,
            timeline_fps_num,
            timeline_fps_den,
        ) = self._resolve_source_fps_metadata(asset_id)
        cmd = AppendClipCommand.create(
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            track_id=track_id,
            speed=speed,
            source_fps_num=source_fps_num,
            source_fps_den=source_fps_den,
            timeline_fps_num=timeline_fps_num,
            timeline_fps_den=timeline_fps_den,
        )

        self._execute_command(cmd)
        logger.debug(f"Append clip: asset={asset_id}, in={in_frame}, out={out_frame}")
        return True

    # ==================== W01: Replace Edit (wiring plan D1) ====================

    def replace_clip_with_source(
        self,
        asset_id: str,
        in_frame: int,
        out_frame: int,
    ) -> bool:
        """Resolve 风格 Replace Edit（接线计划 W01 / 契约 D1）。

        读取最新时间线与选区快照：目标 = 唯一选中 clip，否则播放头下
        有效视频目的轨上的 clip；源对齐帧 = ``in_frame``（inclusive，
        Out 不决定时长）；保留目标足迹/速度/效果/链接，仅换素材与源窗。
        linked 伙伴存在且新源为有声视频时成对替换（宏事务、单步 undo）。
        源不足/多选/无目标 → 明确拒绝，零写入。

        Returns:
            True if a replace transaction was committed, False otherwise.
        """
        from src.features.edit.core.commands import (
            MacroCommand, ReplaceClipWithSourceCommand,
        )

        if self._timeline is None:
            logger.warning("Cannot replace: no timeline loaded")
            return False
        asset = self._project.assets.get(asset_id) if self._project else None
        if asset is None:
            self.status_message_requested.emit(I18N.tr(
                "timeline.replace.asset_missing",
                "Replace: source asset is not in the project"))
            logger.warning("Replace blocked: asset %s not in project", asset_id)
            return False

        target = self._resolve_replace_target_clip()
        if target is None:
            self.status_message_requested.emit(I18N.tr(
                "timeline.replace.no_target",
                "Replace: select a clip or park the playhead over one"))
            return False
        track_id, clip = target

        # B05：目标轨类型 × 源流兼容性预检——缺流整次拒绝、零写入
        if not self._replace_stream_compatible(asset, track_id):
            return False

        # 契约 D1-5：linked 伙伴（互链有效且新源为有声视频）成对替换
        partner = self._find_linked_partner(clip)
        pair_replace = partner is not None and (
            asset.media_type == "video"
            and getattr(asset, "audio_channels", None))
        members: list[tuple[str, Any]] = [(track_id, clip)]
        if pair_replace:
            members.append(partner)

        # B03：逐成员按完整速度映射求源窗并独立预检
        (s_num, s_den, t_num, t_den) = self._resolve_source_fps_metadata(asset_id)
        available = self._get_asset_source_duration_frames(asset) - int(in_frame)
        needed_by_member: dict[str, int] = {}
        for member_track_id, member_clip in members:
            if not self._replace_stream_compatible(asset, member_track_id):
                return False
            needed = self._replace_needed_source_frames(
                member_clip, s_num, s_den, t_num, t_den)
            if available < needed:
                self.status_message_requested.emit(I18N.tr(
                    "timeline.replace.source_too_short",
                    "Replace: source range is shorter than the target clip"))
                logger.warning(
                    "Replace blocked: source %s has %d frames from %d, "
                    "member %s needs %d",
                    asset_id, max(available, 0), in_frame,
                    member_clip.clip_id, needed)
                return False
            needed_by_member[member_clip.clip_id] = needed

        # VFR 生产绑定的 PTS 表随新素材刷新
        pts_raw = (getattr(asset, "metadata", None) or {}).get("pts_index")
        pts_index = tuple(float(v) for v in pts_raw) if pts_raw else None
        fps_meta = (s_num, s_den, t_num, t_den, pts_index)

        commands = [ReplaceClipWithSourceCommand.create(
            clip_id=clip.clip_id,
            track_id=track_id,
            new_asset_id=asset_id,
            new_source_start=int(in_frame),
            new_source_duration=needed_by_member[clip.clip_id],
            new_fps_meta=fps_meta,
        )]

        if pair_replace:
            p_track_id, p_clip = partner
            commands.append(ReplaceClipWithSourceCommand.create(
                clip_id=p_clip.clip_id,
                track_id=p_track_id,
                new_asset_id=asset_id,
                new_source_start=int(in_frame),
                new_source_duration=needed_by_member[p_clip.clip_id],
                new_fps_meta=fps_meta,
            ))
            command = MacroCommand.create("Replace edit (A/V)", commands)
        else:
            command = commands[0]
            if clip.linked_clip_id:
                logger.debug(
                    "Replace: linked partner kept (partner missing or "
                    "source has no audio)")

        try:
            self._execute_command(command)
        except ValueError as exc:
            self.status_message_requested.emit(str(exc))
            return False
        logger.debug(
            "Replace edit: clip=%s asset=%s source_start=%d source_duration=%s",
            clip.clip_id, asset_id, in_frame,
            needed_by_member.get(clip.clip_id))
        return True

    def _replace_stream_compatible(self, asset: MediaAsset, track_id: str) -> bool:
        """B05：目标轨类型 × 源流兼容性预检。

        视频轨需要可视流（video/image/image_sequence），音频轨需要
        声流（audio_info.channels）。不支持的组合整次拒绝零写入，
        不再把纯音频素材写上视频轨（或无声源写上音频轨）后交给
        解码端"发现"缺流。
        """
        if self._timeline is None:
            return False
        track = self._timeline.get_track_by_id(track_id)
        if track is None:
            return False
        if track.track_type == "video":
            if asset.media_type not in ("video", "image", "image_sequence"):
                self.status_message_requested.emit(I18N.tr(
                    "timeline.replace.no_video_stream",
                    "Replace: source has no video stream for a video track"))
                logger.warning(
                    "Replace blocked: asset %s (%s) has no video stream "
                    "for track %s", asset.asset_id, asset.media_type, track_id)
                return False
        elif track.track_type == "audio":
            if not getattr(asset, "audio_channels", None):
                self.status_message_requested.emit(I18N.tr(
                    "timeline.replace.no_audio_stream",
                    "Replace: source has no audio stream for an audio track"))
                logger.warning(
                    "Replace blocked: asset %s (%s) has no audio stream "
                    "for track %s", asset.asset_id, asset.media_type, track_id)
                return False
        return True

    def _replace_needed_source_frames(
        self, clip: Any,
        s_num: Optional[int], s_den: Optional[int],
        t_num: Optional[int], t_den: Optional[int],
    ) -> int:
        """B03/B04：Replace 逐成员源窗帧数——完整速度映射。

        - 冻结片（B04）整段只播一帧：源窗 = 1，冻结帧重映射到新源
          对齐帧（命令层完成），不再用 duration×speed 夸大需求；
        - 速度点片按曲线窗口积分（与采样路径同一映射，含窗口/
          曲线坐标时基），取代旧"仅 base 速度"标量换算；
        - 恒速片保持既有有理数公式；混合帧率统一折入源/时间线
          fps 比，不二次换算。
        """
        if clip.freeze_source_frame is not None:
            return 1
        ratio = Fraction(1, 1)
        if s_num and t_num:
            source_fps = Fraction(s_num, s_den or 1)
            timeline_fps = Fraction(t_num, t_den or 1)
            if source_fps > 0 and timeline_fps > 0:
                ratio = source_fps / timeline_fps
        speed = clip.speed
        if (isinstance(speed, SpeedParams)
                and speed.is_variable_speed):
            # 等效速度域时间（timeline_to_source_frame 同一换算）后的
            # 曲线终点位置 = 从窗口起点消费的源帧总数
            t_curve = float(clip.timeline_duration * ratio)
            position = speed.source_position_at_time(
                t_curve, clip.source_duration)
            return max(1, int(math.ceil(round(position, 9))))
        factor = Fraction(
            abs(clip.effective_speed()) or 1.0).limit_denominator(100000)
        return max(1, int(round(clip.timeline_duration * factor * ratio)))

    def _resolve_replace_target_clip(self) -> Optional[tuple[str, Any]]:
        """契约 D1-1：唯一选中 clip → 播放头下有效视频目的轨 clip → None。"""
        if self._timeline is None:
            return None
        selected = list(getattr(self._selection, "selected_clip_ids", ()) or ())
        if len(selected) == 2:
            first = self._timeline.get_clip_by_id(selected[0])
            partner = self._get_reciprocal_linked_clip(first)
            if partner is not None and partner.clip_id in selected:
                primary = self._selection.primary_item
                selected = [primary.item_id if primary is not None else first.clip_id]
        if len(selected) > 1:
            # 多选不隐式批量替换（契约 D1-1）——按无目标处理并在上层提示
            self.status_message_requested.emit(I18N.tr(
                "timeline.replace.multi_selection",
                "Replace: select exactly one clip"))
            return None
        all_tracks = list(self._timeline.video_tracks) + list(self._timeline.audio_tracks)
        if len(selected) == 1:
            for track in all_tracks:
                clip = track.get_clip_by_id(selected[0])
                if clip is not None:
                    return (track.track_id, clip)
            return None
        # 无选中：播放头下、有效视频目的轨上的 clip
        destination = self.resolve_source_destination_track_id("video")
        if destination is None:
            return None
        track = self._timeline.get_track_by_id(destination)
        if track is None:
            return None
        frame = self._playhead_frame
        for clip in track.clips:
            if clip.timeline_start <= frame < clip.timeline_start + clip.timeline_duration:
                return (track.track_id, clip)
        return None

    def _find_linked_partner(self, clip: Any) -> Optional[tuple[str, Any]]:
        """返回 (track_id, partner_clip)；链接不存在或无效返回 None。

        B03 补充：Replace 消费互链伙伴前必须通过
        ``get_reciprocal_linked_clip`` 校验——双向链接 + 视频/音频异轨，
        悬空/单向引用不得把无关片段拉进替换事务。
        """
        if self._timeline is None or not clip.linked_clip_id:
            return None
        partner = self._timeline.get_reciprocal_linked_clip(clip)
        if partner is None:
            return None
        partner_track = self._timeline.get_track_by_id(partner.track_id)
        if partner_track is None:
            return None
        return (partner_track.track_id, partner)

    # ==================== W05: Clip Flags (wiring plan D4) ====================

    def set_clip_flags(self, color: str | None) -> bool:
        """为选中 clip（无选中时播放头下 clip）设置/清除彩色旗标。

        契约 D4：``color`` 为白名单 "#RRGGBB" 或 None（面板的 "" 清除
        信号在 EditPage 归一为 None）；多选批量一条命令单步 undo；不随
        linked 伙伴传播；无目标 → 状态栏提示、零写入。全部目标已是
        目标色时命令 no-op（不入历史）。

        Returns:
            True if a flag transaction was committed (含 no-op), False on rejection.
        """
        from src.features.edit.core.commands import SetClipFlagCommand
        from src.features.edit.core.timeline_v2 import CLIP_FLAG_COLORS

        if self._timeline is None:
            logger.warning("Cannot flag: no timeline loaded")
            return False
        if color is not None and color not in CLIP_FLAG_COLORS:
            self.status_message_requested.emit(I18N.tr(
                "timeline.flag.unknown_color",
                "Flag: unknown color"))
            return False

        targets: list[tuple[str, str]] = []
        selected = list(getattr(self._selection, "selected_clip_ids", ()) or ())
        all_tracks = (list(self._timeline.video_tracks)
                      + list(self._timeline.audio_tracks))
        if selected:
            for track in all_tracks:
                for clip in track.clips:
                    if clip.clip_id in selected:
                        targets.append((track.track_id, clip.clip_id))
        else:
            frame = self._playhead_frame
            for track in all_tracks:
                for clip in track.clips:
                    if (clip.timeline_start <= frame
                            < clip.timeline_start + clip.timeline_duration):
                        targets.append((track.track_id, clip.clip_id))
        if not targets:
            self.status_message_requested.emit(I18N.tr(
                "timeline.flag.no_target",
                "Flag: select clips or park the playhead over one"))
            return False

        cmd = SetClipFlagCommand.create(targets, color)
        try:
            self._execute_command(cmd)
        except ValueError as exc:
            self.status_message_requested.emit(str(exc))
            return False
        return True

    # ==================== Gap Indicators (Phase 3.3) ====================

    def _update_gap_indicators(self) -> None:
        """
        Update gap indicator items for all tracks.

        参考 kdenlive 时间线间隙显示：
        - 遍历每个 track 的所有 clip
        - 排序后找到相邻 clip 之间的间隙
        - 创建 GapIndicatorItem 放置在间隙位置
        - clip 移动/添加/删除时更新

        Performance considerations:
        - 只在 load_timeline() 和 clip 操作后调用
        - 避过于频繁的更新
        - 最小间隙宽度才显示（MIN_GAP_DISPLAY_FRAMES）

        AUD-21400: 全部生产调用点（load_timeline / update_timeline_
        incremental）都在 self._updating 锁内；此前入口守卫复用同一把锁，
        恒真返回使 GapIndicatorItem 永不创建。改用专用防重入锁
        _updating_gaps，允许在 _updating 锁内执行。
        """
        if self._updating_gaps:
            return

        if self._timeline is None:
            # 清除所有间隙指示器
            for gap_item in list(self._gap_items.values()):
                self._scene.removeItem(gap_item)
            self._gap_items.clear()
            return

        self._updating_gaps = True
        try:
            # 清除旧的间隙指示器
            for gap_item in list(self._gap_items.values()):
                self._scene.removeItem(gap_item)
            self._gap_items.clear()

            # 遍历所有 track
            all_tracks = self._ordered_tracks()
            min_gap_frames = GapIndicatorItem.MIN_GAP_DISPLAY_FRAMES

            for track_index, track in enumerate(all_tracks):
                # 获取该 track 的所有 clip 并按 start_frame 排序
                clips = sorted(track.clips, key=lambda c: c.timeline_start)

                if len(clips) < 2:
                    # 单个 clip 或空 track，没有间隙
                    continue

                # 获取 track 高度
                track_height = TRACK_HEIGHT
                if track.track_type == "audio":
                    track_height = EDIT_SIZES["track_h_audio"]

                # 遍历相邻 clip，找间隙
                for i in range(len(clips) - 1):
                    current_clip = clips[i]
                    next_clip = clips[i + 1]

                    # 计算间隙：下一个 clip 的开始 - 当前 clip 的结束
                    current_end = current_clip.timeline_start + current_clip.timeline_duration
                    gap_start = current_end
                    gap_end = next_clip.timeline_start
                    gap_duration = gap_end - gap_start

                    # 只显示大于最小阈值的间隙
                    if gap_duration >= min_gap_frames:
                        # 生成唯一 gap_id
                        gap_id = f"gap_{track.track_id}_{gap_start}_{gap_duration}"

                        # 创建 GapIndicatorItem
                        gap_item = GapIndicatorItem(
                            track_id=track.track_id,
                            track_index=track_index,
                            start_frame=gap_start,
                            duration_frames=gap_duration,
                            pixels_per_frame=self._pixels_per_frame,
                            track_height=track_height,
                        )
                        self._scene.addItem(gap_item)
                        self._gap_items[gap_id] = gap_item

                        logger.debug(f"Gap indicator created: track={track.track_id}, "
                                     f"start={gap_start}, duration={gap_duration}")

            self._layout_track_items()
            logger.debug(f"Updated {len(self._gap_items)} gap indicators")

        finally:
            self._updating_gaps = False

    def _update_gap_indicators_for_track(self, track_id: str) -> None:
        """
        Update gap indicators for a specific track.

        用于局部更新，避免全量重建。

        Args:
            track_id: Track ID to update gaps for
        """
        # AUD-21400: 与 _update_gap_indicators 一致使用专用锁
        if self._updating_gaps or self._timeline is None:
            return

        self._updating_gaps = True
        try:
            # 移除该 track 的旧间隙指示器
            gaps_to_remove = [
                gap_id for gap_id, gap_item in self._gap_items.items()
                if gap_item.track_id == track_id
            ]
            for gap_id in gaps_to_remove:
                gap_item = self._gap_items.pop(gap_id)
                self._scene.removeItem(gap_item)

            # 找到该 track
            track = None
            track_index = 0
            all_tracks = self._timeline.video_tracks + self._timeline.audio_tracks
            for idx, t in enumerate(all_tracks):
                if t.track_id == track_id:
                    track = t
                    track_index = idx
                    break

            if track is None:
                return

            # 获取 track 高度
            track_height = self._track_height_for(track)

            # 重新计算间隙
            clips = sorted(track.clips, key=lambda c: c.timeline_start)
            min_gap_frames = GapIndicatorItem.MIN_GAP_DISPLAY_FRAMES

            if len(clips) >= 2:
                for i in range(len(clips) - 1):
                    current_clip = clips[i]
                    next_clip = clips[i + 1]

                    current_end = current_clip.timeline_start + current_clip.timeline_duration
                    gap_start = current_end
                    gap_end = next_clip.timeline_start
                    gap_duration = gap_end - gap_start

                    if gap_duration >= min_gap_frames:
                        gap_id = f"gap_{track_id}_{gap_start}_{gap_duration}"

                        gap_item = GapIndicatorItem(
                            track_id=track_id,
                            track_index=track_index,
                            start_frame=gap_start,
                            duration_frames=gap_duration,
                            pixels_per_frame=self._pixels_per_frame,
                            track_height=track_height,
                        )
                        self._scene.addItem(gap_item)
                        self._gap_items[gap_id] = gap_item

            self._layout_track_items()

        finally:
            self._updating_gaps = False

    def update_gap_visibility(self, visible_start: int, visible_end: int) -> None:
        """
        Update gap indicator visibility based on viewport.

        Args:
            visible_start: First visible frame
            visible_end: Last visible frame
        """
        for gap_item in self._gap_items.values():
            gap_item.update_visibility(visible_start, visible_end)
