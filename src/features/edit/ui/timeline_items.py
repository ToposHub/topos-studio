"""
Timeline Items Module - QGraphicsItem subclasses for timeline visualization (P6).

This module provides the visual representation of timeline elements:
- ClipItem: Visual clip on timeline track
- TransitionItem: Visual transition between clips
- PlayheadItem: Current playhead position indicator
- TrackHeaderItem: Track header with controls

Design Principles:
- All items use QGraphicsItem/QGraphicsObject for Qt integration
- Signal-safe: No direct rendering calls, emit signals for state changes
- Anti-recursion locks for any bidirectional signal connections
- Items are visual only; data model lives in timeline_v2.py
- Visual style follows edit_style.py design tokens (P1.4)

Reference:
- docs/edit_page_timeline_upgrade_plan_2026-05-17.md P6, P1.4
- .clinerules: UI thread safety
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PyQt6.QtGui import QMouseEvent

import logging
import math
from collections import OrderedDict
from typing import Optional, Any
from enum import Enum
from pathlib import Path

from src.shared.media.metadata import Rational
from src.shared.media.timecode import Timecode

from PyQt6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsRectItem,
    QStyleOptionGraphicsItem,
)
from PyQt6.QtCore import (
    Qt,
    QRectF,
    QPointF,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QPainter,
    QColor,
    QCursor,
    QPen,
    QBrush,
    QFont,
    QPainterPath,
    QPolygonF,
    QLinearGradient,
    QPixmap,
    QIcon,
)

from src.features.edit.ui.edit_style import EDIT_SIZES, get_color
from src.features.edit.core.audio_waveform import (
    WaveformData,
    WaveformExtractor,
    get_cached_waveform,
    request_waveform_async,
    request_effect_waveform,
    ClipEffectWaveformRenderer,
    EFFECT_WAVEFORM_DEFERRED_ERROR,
)
from src.features.edit.core.transition_catalog import TRANSITION_LABELS
from src.features.edit.core.audio_transition_catalog import (
    AUDIO_TRANSITION_LABEL_MAP,
    is_audio_transition,
)

logger = logging.getLogger(__name__)


# ================================================================
# Constants (P1.4 - Using edit_style.py design tokens)
# ================================================================

# Track geometry (from EDIT_SIZES)
TRACK_HEADER_WIDTH = EDIT_SIZES["track_header_w"]
TRACK_HEIGHT = EDIT_SIZES["track_h_video"]  # Default, actual varies by track type
TRACK_GAP = 2
TRACK_RESIZE_HANDLE_HEIGHT = 6

# Timeline layout constants (坐标模型修复)
RULER_HEIGHT = EDIT_SIZES["ruler_h"]  # 时间码标尺高度 (36)
TRACK_AREA_TOP = EDIT_SIZES["track_area_top"]  # 轨道内容区起始 y 坐标 (36)

# Clip visual settings
CLIP_BORDER_WIDTH = 1
CLIP_SELECTED_BORDER_WIDTH = 1  # Thin accent outline for selected clips
HANDLE_SIZE = 10  # Size of trim handles on clip edges
TRANSITION_ITEM_HEIGHT = 28
TRANSITION_EDGE_HANDLE_WIDTH = 6

# Colors from EDIT_COLORS (design tokens)
CLIP_VIDEO_COLOR = QColor(get_color("video_clip"))  # #2D4B5F
CLIP_AUDIO_COLOR = QColor(get_color("audio_clip"))  # #2D5F3A
CLIP_TRANSITION_COLOR = QColor(get_color("transition"))  # #ba8b45

# P4: Gradient colors for clip backgrounds (DaVinci Resolve professional NLE style)
# Video: professional blue tones matching DaVinci Resolve clip colors
CLIP_VIDEO_GRADIENT_START = QColor(0x45, 0x92, 0xB2)   # lively Resolve blue highlight
CLIP_VIDEO_GRADIENT_END = QColor(0x36, 0x79, 0x99)      # clear blue body
CLIP_VIDEO_LABEL_BAR = QColor(0x21, 0x4F, 0x68)         # dark blue information strip
# Audio: professional green tones
CLIP_AUDIO_GRADIENT_START = QColor(0x43, 0xB0, 0x70)   # lively green highlight
CLIP_AUDIO_GRADIENT_END = QColor(0x32, 0x94, 0x5D)      # clear green body
CLIP_AUDIO_LABEL_BAR = QColor(0x20, 0x5B, 0x39)         # dark green information strip
# Transition: warm amber tones (unchanged)
CLIP_TRANSITION_GRADIENT_START = QColor(140, 100, 50)  # Orange/brown start
CLIP_TRANSITION_GRADIENT_END = QColor(180, 130, 70)    # Lighter end
# Generators — distinct hues so generator clips read at a glance:
#   Adjustment Clip → purple (effect-bearing, no source)
#   Solid Color     → slate grey (flat generator)
CLIP_ADJUSTMENT_GRADIENT_START = QColor(0x6B, 0x3F, 0x9A)   # #6B3F9A purple start
CLIP_ADJUSTMENT_GRADIENT_END = QColor(0x54, 0x32, 0x7B)     # #54327B purple end
CLIP_SOLID_GRADIENT_START = QColor(0x4A, 0x4A, 0x55)        # #4A4A55 slate start
CLIP_SOLID_GRADIENT_END = QColor(0x3A, 0x3A, 0x44)          # #3A3A44 slate end

# P4: Waveform colors.  Resolve keeps the waveform brighter and more neutral
# than the clip body; that contrast is especially important when a custom
# label color is applied to an audio clip.
WAVEFORM_COLOR = QColor(get_color("waveform_line"))
WAVEFORM_COLOR_AUDIO_TRACK = QColor(0xB7, 0xDE, 0xCC)
WAVEFORM_COLOR_STEREO_L = QColor(0xA9, 0xD8, 0xC2)
WAVEFORM_COLOR_STEREO_R = QColor(0xD0, 0xEC, 0xDD)

# Bright-but-controlled label colors. The palette is deliberately spread
# across warm and cool hues so users can distinguish clips at a glance
# without resorting to fully saturated neon colors.
TIMELINE_LABEL_COLORS: tuple[tuple[str, str], ...] = (
    ("Blue", "#5A8FC0"),
    ("Sky", "#5FA6B8"),
    ("Cyan", "#55A594"),
    ("Teal", "#4F9C8C"),
    ("Mint", "#77B87D"),
    ("Olive", "#9EAF61"),
    ("Yellow", "#C7AB58"),
    ("Gold", "#C59A4F"),
    ("Orange", "#C18A54"),
    ("Coral", "#C87367"),
    ("Red", "#B65F68"),
    ("Rose", "#B96882"),
    ("Pink", "#BB6692"),
    ("Magenta", "#B95E9D"),
    ("Purple", "#9574B2"),
    ("Violet", "#8175B3"),
    ("Indigo", "#6D82B7"),
    ("Sand", "#C0A274"),
    ("Slate", "#718B98"),
    ("Gray", "#808895"),
)


def _derive_label_palette(
    label_color: Optional[str],
    ) -> Optional[tuple[QColor, QColor, QColor, QColor]]:
    """Turn a user label color into a restrained timeline palette.

    Label colors are persisted as user data and older projects commonly
    contain fully saturated colors. Using those values directly for the
    whole clip makes thumbnails and waveforms compete with the color. Keep
    the hue as an identity cue, but constrain saturation/lightness for the
    body and reserve a near-neutral light color for the waveform.

    Returns ``(gradient_start, gradient_end, label_bar, waveform_fill)`` or
    ``None`` for an invalid value.
    """
    if not label_color:
        return None

    color = QColor(label_color)
    if not color.isValid():
        return None

    hue, saturation, _lightness, _alpha = color.getHslF()
    if hue < 0.0:  # QColor uses -1 for achromatic colors.
        hue = 0.0
    body_saturation = (
        min(0.76, max(0.40, saturation * 0.72))
        if saturation >= 0.05
        else 0.0
    )
    waveform_saturation = min(0.36, body_saturation * 0.48)
    bar_saturation = min(0.52, body_saturation * 0.84)

    return (
        QColor.fromHslF(hue, body_saturation, 0.48),
        QColor.fromHslF(hue, body_saturation * 0.92, 0.37),
        QColor.fromHslF(hue, bar_saturation, 0.21),
        QColor.fromHslF(hue, waveform_saturation, 0.77),
    )

# Accent colors
ACCENT_COLOR = QColor(get_color("accent"))  # #E29227
ACCENT_HOVER_COLOR = QColor(get_color("accent_hover"))  # #f09a3e

# P4: Selected/hover border colors
CLIP_SELECTED_BORDER = QColor(0xF1, 0xB4, 0x4B)   # bright Resolve-like amber selection
CLIP_HOVER_BORDER = QColor(0x6B, 0x7B, 0x8D)       # #6B7B8D light blue-gray for hover
# A selected clip should read as selected without looking like it is already
# in a trim operation.  These short corner marks replace the old full-height
# amber edge bars; the stronger, filled handles are reserved for edge hover.
SELECTED_TRIM_MARKER_COLOR = QColor(0xF2, 0xB0, 0x4A, 175)
SELECTED_TRIM_MARKER_HIGHLIGHT = QColor(0xFF, 0xE0, 0x9A, 185)
# Trim edges use distinct colours so the in/out points remain legible over a
# thumbnail or waveform while the pointer is on the narrow hit target.
TRIM_IN_HOVER_COLOR = QColor(0x4F, 0xC3, 0xF7)     # cyan: trim in / left edge
TRIM_OUT_HOVER_COLOR = QColor(0xFF, 0xB7, 0x4D)    # amber: trim out / right edge
TRIM_EDGE_ACTIVE_COLOR = QColor(0xFF, 0xE0, 0x8A)


_TRIM_CURSOR_CACHE: dict[str, QCursor] = {}


def _trim_cursor(edge: str) -> QCursor:
    """Return an edge-specific cursor (in-point or out-point)."""
    cached = _TRIM_CURSOR_CACHE.get(edge)
    if cached is not None:
        return cached

    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    direction = -1 if edge == "left" else 1
    boundary_x = 16 if direction < 0 else 8
    arrow_start = boundary_x + direction * 2
    arrow_end = boundary_x + direction * 11

    def draw_trim_mark(pen: QPen) -> None:
        painter.setPen(pen)
        painter.drawLine(QPointF(boundary_x, 4), QPointF(boundary_x, 20))
        painter.drawLine(QPointF(arrow_start, 12), QPointF(arrow_end, 12))
        painter.drawLine(
            QPointF(arrow_end, 12),
            QPointF(arrow_end - direction * 4, 8),
        )
        painter.drawLine(
            QPointF(arrow_end, 12),
            QPointF(arrow_end - direction * 4, 16),
        )

    draw_trim_mark(QPen(QColor(0, 0, 0, 230), 4, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    draw_trim_mark(QPen(QColor(255, 255, 255, 245), 2, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.end()

    cursor = QCursor(pixmap, 12, 12)
    _TRIM_CURSOR_CACHE[edge] = cursor
    return cursor

# Phase 6: Comp Clip visual language.  Comp Clips use a violet/indigo family
# so they remain recognizable even when the source thumbnail is unchanged.
CLIP_COMP_GRADIENT_START = QColor(0x6B, 0x58, 0xA8)
CLIP_COMP_GRADIENT_END = QColor(0x48, 0x3A, 0x79)
CLIP_COMP_LABEL_BAR = QColor(0x32, 0x29, 0x57)
COMP_BADGE_TEXT_COLOR = QColor(0xF3, 0xEE, 0xFF, 245)
COMP_BADGE_BG_COLOR = QColor(0x3E, 0x2D, 0x72, 225)
COMP_BADGE_ICON_COLOR = QColor(0xD8, 0xCA, 0xFF, 245)
COMP_CACHE_DIRTY_COLOR = QColor(0xFF, 0x8C, 0x00)           # Orange
COMP_CACHE_RENDERING_COLOR = QColor(0x4A, 0x90, 0xD9)       # Blue
COMP_CACHE_READY_COLOR = QColor(0x4C, 0xAF, 0x50)           # Green
COMP_CACHE_FAILED_COLOR = QColor(0xE5, 0x39, 0x35)          # Red
COMP_CACHE_LINE_HEIGHT = 2  # pixels

# Text colors
TEXT_COLOR = QColor(get_color("text"))  # #d8d9de
TEXT_MUTED_COLOR = QColor(get_color("text_muted"))  # #8f939c

# Border colors
BORDER_COLOR = QColor(get_color("border"))  # #333333
BORDER_SUBTLE_COLOR = QColor(get_color("border_subtle"))  # #2a2c32

# P4: Playhead
PLAYHEAD_COLOR = QColor(0xFF, 0x33, 0x33)       # #FF3333 red (DaVinci Resolve style)
PLAYHEAD_LINE_WIDTH = 1

# Snap indicator
SNAP_INDICATOR_COLOR = QColor(255, 220, 50, 200)  # Yellow (kept for visibility)
SNAP_INDICATOR_LINE_WIDTH = 2
SNAP_INDICATOR_TIMECODE_COLOR = QColor(255, 255, 255, 230)

# P4: Track type colors (DaVinci Resolve badge colors — reddish-brown)
VIDEO_TRACK_COLOR = QColor(get_color("track_header_badge_video"))  # #8B3A3A reddish-brown
VIDEO_TRACK_LOCKED_COLOR = QColor(0x1A, 0x1A, 0x1D) # Darker when locked
VIDEO_TRACK_HEADER_BG = QColor(get_color("track_header_bg"))  # #1D1E24 header bg
AUDIO_TRACK_COLOR = QColor(get_color("track_header_badge_audio"))  # #8B3A3A reddish-brown
AUDIO_TRACK_LOCKED_COLOR = QColor(0x1A, 0x1A, 0x1D) # Darker when locked
AUDIO_TRACK_HEADER_BG = QColor(get_color("track_header_bg"))  # #1D1E24 header bg
# P10.3: Subtitle track colors — teal/cyan to distinguish from V (red) and A (green)
SUBTITLE_TRACK_COLOR = QColor(0x2A, 0x6E, 0x8B)  # muted teal
SUBTITLE_TRACK_LOCKED_COLOR = QColor(0x1A, 0x1A, 0x1D)
SUBTITLE_TRACK_HEADER_BG = QColor(get_color("track_header_bg"))
# P10.3: Subtitle clip body color on the timeline
SUBTITLE_CLIP_BG = QColor(0x3A, 0x5A, 0x6E)
SUBTITLE_CLIP_BG_SELECTED = QColor(0x4A, 0x7A, 0x8E)
SUBTITLE_CLIP_TEXT_COLOR = QColor(0xE8, 0xE8, 0xE8)

# P4: Target track indicator (active track) - orange left border
TARGET_TRACK_INDICATOR_COLOR = QColor(get_color("track_header_target_bar"))  # #FF8C00 orange
TARGET_TRACK_BORDER_WIDTH = 3  # 3px orange bar

# Button colors (enhanced)
MUTE_ACTIVE_COLOR = QColor(200, 80, 80)  # Red when muted
SOLO_ACTIVE_COLOR = QColor(80, 180, 80)  # Green when solo
LOCK_ACTIVE_COLOR = QColor(180, 140, 60)  # Orange/amber when locked
BUTTON_INACTIVE_COLOR = QColor(128, 133, 143)  # icon-only idle state
BUTTON_HOVER_COLOR = QColor(55, 58, 67)        # understated hover well

BUTTON_SIZE = EDIT_SIZES["track_button_size"]  # 18 (DaVinci-like)
BUTTON_MARGIN = EDIT_SIZES["track_button_margin"]  # 3 (DaVinci-like)
ICON_SIZE = 16  # Size for SVG icons

# ================================================================
# TimelineCoordinateManager (Gemini suggestion: 统一坐标换算逻辑)
# ================================================================
# 数据驱动渲染：所有坐标计算通过此管理器统一处理
# ClipItem 只负责显示，不负责坐标计算逻辑

class TimelineCoordinateManager:
    """
    DEPRECATED: Use TimelineLayoutEngine from timeline_layout.py instead.

    This class is kept for backward compatibility with helper items that
    haven't been migrated yet. DO NOT use for new code.

    The layout engine (TimelineLayoutEngine) is the single source of truth.
    """
    
    def __init__(self, pixels_per_frame: float = 5.0) -> None:
        self._pixels_per_frame = pixels_per_frame
    
    def frame_to_x(self, frame: int) -> float:
        """Convert frame number to X pixel position."""
        return TRACK_HEADER_WIDTH + frame * self._pixels_per_frame
    
    def x_to_frame(self, x: float) -> int:
        """Convert X pixel position to frame number (rounded)."""
        return max(0, round((x - TRACK_HEADER_WIDTH) / self._pixels_per_frame))
    
    def track_to_y(self, track_index: int, track_height: int = TRACK_HEIGHT) -> float:
        """Convert track index to Y pixel position (轨道锁定)."""
        return TRACK_AREA_TOP + track_index * (track_height + TRACK_GAP)
    
    def frames_to_width(self, frames: int) -> float:
        """Convert frame count to width in pixels."""
        return frames * self._pixels_per_frame
    
    def width_to_frames(self, width: float) -> int:
        """Convert width in pixels to frame count."""
        return max(1, round(width / self._pixels_per_frame))
    
    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale."""
        self._pixels_per_frame = pixels_per_frame
    
    @property
    def pixels_per_frame(self) -> float:
        return self._pixels_per_frame


# Global coordinate manager instance (shared by all timeline items)
_coord_manager: Optional[TimelineCoordinateManager] = None

def get_coord_manager() -> TimelineCoordinateManager:
    """Get global coordinate manager instance."""
    global _coord_manager
    if _coord_manager is None:
        _coord_manager = TimelineCoordinateManager()
    return _coord_manager

def set_coord_manager(manager: TimelineCoordinateManager) -> None:
    """Set global coordinate manager instance."""
    global _coord_manager
    _coord_manager = manager


# ================================================================
# Linked Group Colors (Phase 6.1)
# ================================================================

# 预定义的链接组颜色盘 (参考 Kdenlive groupColor)
# 用于为同一链接组的 clip 分配相同颜色的底部边框线
LINKED_GROUP_COLORS = [
    QColor(94, 164, 192),   # muted cyan
    QColor(111, 166, 126),  # muted green
    QColor(114, 132, 194),  # muted indigo
    QColor(185, 161, 92),   # muted amber
    QColor(154, 119, 184),  # muted violet
    QColor(93, 168, 170),   # muted teal
    QColor(183, 130, 91),   # muted orange
    QColor(151, 108, 185),  # muted purple
]

# Linked group border settings
LINKED_GROUP_BORDER_WIDTH = 1  # Bottom border width in pixels
LINKED_GROUP_BORDER_HIGHLIGHT_WIDTH = 3  # Highlighted border width

# ================================================================
# Fade Handle Constants (Phase 1.2)
# ================================================================

FADE_HANDLE_SIZE = 10  # Compact top-mounted DaVinci-style handle
FADE_HANDLE_HIT_RADIUS = 12  # Forgiving, but kept clear of trim edges
FADE_HANDLE_HIT_HEIGHT = 24  # Compact top control band
FADE_HANDLE_COLOR = QColor(236, 239, 237, 215)  # Light neutral handle face
FADE_HANDLE_HOVER_COLOR = QColor(255, 255, 255, 242)  # Crisp active face
FADE_HANDLE_OUTLINE = QColor(255, 190, 82, 225)  # Fine amber outline
FADE_HANDLE_SHADOW = QColor(5, 8, 12, 145)  # Keeps the small mark readable
FADE_RAMP_COLOR = QColor(255, 194, 82, 210)  # Fine diagonal ramp line
FADE_REGION_COLOR = QColor(8, 12, 16, 108)  # Resolve-like dark translucent range
MIN_CLIP_WIDTH_FOR_FADE_HANDLES = 100  # Only show fade handles for clips > 100px wide

# ================================================================
# Transition Type Constants (Phase 4.1)
# ================================================================

TRANSITION_TYPE_COLORS = {
    'cross_dissolve': QColor(217, 130, 43),   # Orange (same as accent)
    'dip_to_color': QColor(160, 115, 210),
    'wipe': QColor(90, 200, 250),        # Blue
}
TRANSITION_TYPE_COLORS.update({
    transition_type: QColor(90, 170, 210)
    for transition_type in TRANSITION_LABELS
    if transition_type not in TRANSITION_TYPE_COLORS
})
# Audio transitions get a distinct teal so they're visually separable from video
# transitions on the timeline (video = blue/orange family, audio = teal).
TRANSITION_TYPE_COLORS.update({
    ttype: QColor(45, 170, 165)
    for ttype in AUDIO_TRANSITION_LABEL_MAP
    if ttype not in TRANSITION_TYPE_COLORS
})
TRANSITION_TYPE_LABELS = dict(TRANSITION_LABELS)
# Merge audio labels so the card shows "Constant Power" etc. instead of the
# generic "Transition" fallback.
TRANSITION_TYPE_LABELS.update(AUDIO_TRANSITION_LABEL_MAP)

# ================================================================
# Drag Frame Label Constants (Phase 2.5)
# ================================================================

DRAG_FRAME_LABEL_HEIGHT = 20  # Height of frame number label
DRAG_FRAME_LABEL_BG_COLOR = QColor(30, 30, 35, 200)  # Dark semi-transparent background
DRAG_FRAME_LABEL_TEXT_COLOR = QColor(255, 255, 255, 230)  # White text
DRAG_FRAME_LABEL_SNAP_COLOR = QColor(80, 180, 80, 200)  # Green background for snap
DRAG_FRAME_LABEL_SNAP_TEXT_COLOR = QColor(255, 255, 255, 255)  # White text for snap

# ================================================================
# P2-6: Drag threshold in pixels before a mouse press becomes a drag
# Keep the threshold low enough that an intentional drag starts immediately,
# while retaining a small click-vs-drag guard for selection.
DRAG_THRESHOLD_PX: float = 2.0


def _is_menu_action_selected(action: Any, candidate: Any) -> bool:
    """Return whether a real QAction was chosen from a context menu.

    ``QMenu.exec`` returns ``None`` when dismissed and optional menu entries
    also use ``None`` as their sentinel.  Identity plus a non-None candidate
    prevents those two states from being mistaken for an action click.
    """
    return candidate is not None and action is candidate


# Visibility Culling Constants (参考 kdenlive Clip.qml)
# ================================================================

# UI-11: Clip narrow width display thresholds (用户需求调整)
MIN_CLIP_WIDTH_FOR_DETAILS = 30  # 最小宽度才显示波形/缩略图 (< 30: 只显示颜色块和选中边框)
MIN_CLIP_WIDTH_FOR_NAME = 30     # 最小宽度才显示名称 (>= 30: 显示名称)
MIN_CLIP_WIDTH_FOR_BADGES = 80   # 最小宽度才显示 badges/indicators (>= 80: 显示全部内容)

# P5: Video Filmstrip LOD thresholds
THUMBNAIL_MODE_NONE = "none"           # < 40px: Hide thumbnails entirely
THUMBNAIL_MODE_SINGLE = "single"       # 40-100px: Single first-frame thumbnail
THUMBNAIL_MODE_START_END = "start_end" # 100-200px: First and last frame
THUMBNAIL_MODE_FILMSTRIP = "filmstrip" # > 200px: Evenly spaced filmstrip frames

# P5: Thumbnail LOD width thresholds
THUMBNAIL_WIDTH_NONE = 40        # Below: hide thumbnails
THUMBNAIL_WIDTH_SINGLE = 120     # Below: single thumbnail
THUMBNAIL_WIDTH_START_END = 260  # Below: start+end; above: filmstrip

# P5: Filmstrip frame sizing.  Cells keep a stable display aspect instead of
# becoming near-square at common zoom levels, which otherwise crops each frame
# differently and makes neighbouring thumbnails look mismatched in scale.
FILMSTRIP_FRAME_MIN_WIDTH = 60
FILMSTRIP_CELL_ASPECT = 16.0 / 9.0
FILMSTRIP_FRAME_MAX_COUNT = 10   # Maximum frames in filmstrip mode

# P5: Cache bucket size for zoom-level grouping
THUMBNAIL_CACHE_BUCKET_SIZE = 50  # Group zoom levels in 50px buckets

# P5: Loading/failed state animation
THUMBNAIL_LOADING_COLOR = QColor(40, 40, 40)          # Dark gray for loading
THUMBNAIL_FAILED_COLOR = QColor(50, 30, 30)            # Dark red tint for failed
THUMBNAIL_LOADING_DOT_COLOR = QColor(120, 120, 140)    # Dot color for loading animation
THUMBNAIL_FAILED_ICON_COLOR = QColor(200, 80, 80)      # X icon color for failed state


# ================================================================
# Edit Tool Enum
# ================================================================

class EditTool(Enum):
    """Available editing tools.
    
    Tool order: Select(A) Trim(T) Blade(B) Ripple(R) Roll(W) Slip(Y) Slide(U) Hand(H) Zoom(Z)
    """
    SELECT = "select"
    TRIM = "trim"
    BLADE = "blade"
    RIPPLE = "ripple"
    ROLL = "roll"
    SLIP = "slip"
    SLIDE = "slide"
    HAND = "hand"
    ZOOM = "zoom"
    # B4: 比例拉伸——拖边缘改变时长，速度按 源时长/新时长 换算
    RATE_STRETCH = "rate_stretch"


# ================================================================
# ClipItem
# ================================================================

class ClipItem(QGraphicsObject):
    """
    Visual representation of a clip on the timeline.

    Signals:
        clip_clicked: Clip was clicked (clip_id, track_id)
        clip_double_clicked: Clip was double-clicked
        clip_moved: Clip position changed during drag (live preview)
        clip_trim_left: Left edge trim during drag (live preview)
        clip_trim_right: Right edge trim during drag (live preview)
        motion_graphic_duration_changed: Shift+right-edge duration preview
        motion_graphic_duration_change_finished: committed MG duration edit
        drag_finished: Drag completed, ready for command (clip_id, track_id, old_start, new_start)
        trim_left_finished: Left trim completed (clip_id, track_id, old_start, old_duration, old_source_start, new_start, new_source_start)
        trim_right_finished: Right trim completed (clip_id, track_id, old_duration, old_source_duration, new_duration, new_source_duration)
        clip_split: Split requested at position
        clip_delete: Delete requested
        add_transition_requested: Add transition requested (clip_id, track_id, edge)

    Attributes (visual):
        _clip_id: Unique clip identifier
        _track_id: Track containing this clip
        _asset_name: Display name for the clip
        _start_frame: Timeline start frame
        _duration_frames: Duration in frames
        _pixels_per_frame: Zoom scale
        _track_index: Track vertical position
        _is_selected: Selection state
        _is_enabled: Enabled state
        _is_track_locked: Whether the track is locked (prevents editing)
        _hover_edge: Which edge is hovered (None, 'left', 'right')
        _source_start: Source material start frame
        _source_duration: Source material duration
        _max_source_frames: Maximum source frames available (for trim constraints)
        _track_height: Current track height (for variable height support)
        _media_type: Media type ('video', 'audio', 'image')
        _linked_clip_id: Linked clip ID (for audio-video sync)
    """

    # P5: Class-level thumbnail/waveform cache (shared across all ClipItems)
    _thumbnail_cache: dict[str, QPixmap] = {}
    # QPainterPath objects are costly and the old unbounded dict grew for every
    # trim/zoom combination. Keep a small LRU: paths are rebuilt from cached
    # peak data when revisiting an evicted zoom level.
    _waveform_cache_shared: OrderedDict[str, list[QPainterPath]] = OrderedDict()
    _waveform_cache_shared_max_size: int = 256
    # User thumbnail display preference: "auto" (LOD-based), "none", "start_end", "filmstrip"
    _thumbnail_display_mode: str = "auto"
    # Cached diagonal stripe tile for _draw_disabled_pattern (avoids per-paint recreation)
    _disabled_pattern_tile: QPixmap | None = None

    # Signals
    clip_clicked = pyqtSignal(str, str)  # clip_id, track_id
    clip_double_clicked = pyqtSignal(str, str)
    clip_moved = pyqtSignal(str, str, int, float)  # clip_id, track_id, new_start, scene_y (live preview, supports track change)
    clip_trim_left = pyqtSignal(str, str, int)  # clip_id, track_id, new_start (live preview)
    clip_trim_right = pyqtSignal(str, str, int)  # clip_id, track_id, new_duration (live preview)
    # Motion Graphic retiming changes the timeline span while preserving the
    # authored scene range; it is intentionally separate from source trim.
    motion_graphic_duration_changed = pyqtSignal(str, str, int)  # live preview
    motion_graphic_duration_change_finished = pyqtSignal(str, str, int, int)  # old_duration, new_duration
    # Command signals (sent on mouseRelease)
    drag_finished = pyqtSignal(str, str, int, int, str)  # clip_id, track_id, old_start, new_start, new_track_id
    # INT-02：片段自身拖拽/修剪也纳入 view 的 InteractionSession，使
    # Esc/换工具/失焦能统一取消；release 正常结束会话。
    interaction_started = pyqtSignal(str, str, str)  # clip_id, track_id, drag_mode
    interaction_finished = pyqtSignal(str)  # clip_id
    trim_left_finished = pyqtSignal(str, str, int, int, int, int, int)  # clip_id, track_id, old_start, old_duration, old_source_start, new_start, new_source_start
    trim_right_finished = pyqtSignal(str, str, int, int, int, int)  # clip_id, track_id, old_duration, old_source_duration, new_duration, new_source_duration
    clip_split = pyqtSignal(str, str, int)  # clip_id, track_id, split_frame
    clip_duplicate = pyqtSignal(str, str)  # clip_id, track_id
    clip_delete = pyqtSignal(str, str)  # clip_id, track_id
    # E10: 第 4 参为显式 transition_type（cross_dissolve/dip_to_color），
    # Dip To Color 不再与 Cross Dissolve 发相同载荷。
    add_transition_requested = pyqtSignal(str, str, str, str)  # clip_id, track_id, edge, transition_type
    linked_clip_moved = pyqtSignal(str, str, int)  # linked_clip_id, track_id, new_start (for sync)
    # Slip signals (P4.4)
    clip_slip = pyqtSignal(str, str, int)  # clip_id, track_id, delta_frames (live preview)
    slip_finished = pyqtSignal(str, str, int, int, int)  # clip_id, track_id, old_source_start, new_source_start, max_source_frames
    # Slide signals (P4.5)
    clip_slide = pyqtSignal(str, str, int)  # clip_id, track_id, delta_frames (live preview)
    slide_finished = pyqtSignal(str, str, int, str, str)  # clip_id, track_id, delta_frames, prev_clip_id, next_clip_id
    # Ripple trim signals (P4.2)
    ripple_trim_left = pyqtSignal(str, str, int)  # clip_id, track_id, new_start (live preview)
    ripple_trim_right = pyqtSignal(str, str, int)  # clip_id, track_id, new_end (live preview)
    ripple_trim_left_finished = pyqtSignal(str, str, int, int)  # clip_id, track_id, old_start, new_start
    ripple_trim_right_finished = pyqtSignal(str, str, int, int)  # clip_id, track_id, old_end, new_end
    # B4: 比例拉伸结束（clip_id, track_id, original_duration, new_duration）
    rate_stretch_finished = pyqtSignal(str, str, int, int)
    # P9.2: Edit → Composite page linkage
    open_in_composite_requested = pyqtSignal(str, str)  # clip_id, track_id
    # Phase 6: Comp Clip context menu signals
    convert_to_comp_clip_requested = pyqtSignal(str, str)  # clip_id, track_id
    remove_composite_binding_requested = pyqtSignal(str, str)  # clip_id, track_id
    bypass_composite_requested = pyqtSignal(str, str)  # clip_id, track_id
    reset_composite_requested = pyqtSignal(str, str)  # clip_id, track_id
    reveal_comp_clip_in_media_pool_requested = pyqtSignal(str, str)  # clip_id, track_id
    render_cache_clip_requested = pyqtSignal(str, str)  # clip_id, track_id
    rerender_cache_clip_requested = pyqtSignal(str, str)  # clip_id, track_id
    # P9.1: Grade mode signals (Shared/Local Grade)
    use_shared_grade_requested = pyqtSignal(str, str)  # clip_id, track_id
    make_local_grade_copy_requested = pyqtSignal(str, str)  # clip_id, track_id
    open_in_color_requested = pyqtSignal(str, str, str)  # clip_id, track_id, stack_id
    # Phase 1.2: Fade in/out signals
    fade_in_changed = pyqtSignal(str, str, int)  # clip_id, track_id, new_fade_in (live preview)
    fade_out_changed = pyqtSignal(str, str, int)  # clip_id, track_id, new_fade_out (live preview)
    fade_finished = pyqtSignal(str, str, int, int, int, int)  # clip_id, track_id, old_fade_in, old_fade_out, new_fade_in, new_fade_out
    # Phase 6.1: Linked group hover signal (for group highlight sync)
    linked_group_hovered = pyqtSignal(str, str, bool)  # clip_id, linked_clip_id, is_hovered
    # Phase 6.2: Track highlight request signal
    track_highlight_requested = pyqtSignal(str, bool)  # track_id, is_highlighted
    # P9: Scene Cut Detection signal
    scene_cut_detect_requested = pyqtSignal(str, str, str)  # clip_id, track_id, asset_id
    # Audio clip transcription → automatically create timeline subtitles.
    speech_to_text_requested = pyqtSignal(str, str, str)  # clip_id, track_id, asset_id
    # P2: Label color change signal
    label_color_change_requested = pyqtSignal(str, str, object)  # clip_id, track_id, color (str|None)
    # Audio waveform display mode: False = mixed mono envelope, True = L/R.
    waveform_display_mode_requested = pyqtSignal(str, str, bool)
    # Enable/Disable, Link/Unlink signals
    clip_enable_toggle_requested = pyqtSignal(str, str)  # clip_id, track_id
    clip_unlink_requested = pyqtSignal(str, str)  # clip_id, track_id
    clip_link_requested = pyqtSignal(str, str)  # clip_id, track_id
    numeric_trim_requested = pyqtSignal(str, str)  # P1-04 数值修剪
    # P1-02: 嵌套序列入口（裸请求——视图负责收集当前选择）
    create_nested_from_selection_requested = pyqtSignal()
    enter_nested_sequence_requested = pyqtSignal(str, str)  # clip_id, track_id
    speed_change_requested = pyqtSignal(str, str, float)  # clip_id, track_id, speed
    retime_controls_toggled = pyqtSignal(str, str, bool)
    retime_speed_point_requested = pyqtSignal(str, str)
    # A2: 在播放头处冻结（clip_id, track_id, playhead_frame）
    freeze_frame_requested = pyqtSignal(str, str, int)
    retime_speed_points_clear_requested = pyqtSignal(str, str)
    retime_segment_speed_requested = pyqtSignal(str, str, int, float)
    # A3.1: 切换速度点斜坡插值（clip_id, track_id, source_offset, easing）
    retime_point_easing_requested = pyqtSignal(str, str, int, str)
    # B7: 占位片段替换请求（clip_id, track_id）
    replace_placeholder_requested = pyqtSignal(str, str)
    # C3: 片段级音频增益对话框请求（clip_id, track_id）
    audio_gain_dialog_requested = pyqtSignal(str, str)

    def __init__(
        self,
        clip_id: str,
        track_id: str,
        asset_name: str,
        start_frame: int,
        duration_frames: int,
        pixels_per_frame: float,
        track_index: int,
        source_start: int = 0,
        source_duration: int = 0,
        max_source_frames: int = 0,  # Maximum frames in source asset
        track_height: int = TRACK_HEIGHT,  # Variable track height
        is_track_locked: bool = False,  # Track lock state
        media_type: str = "video",  # Media type ('video', 'audio', 'image')
        linked_clip_id: Optional[str] = None,  # Linked clip ID for audio-video sync
        asset_id: Optional[str] = None,  # Asset ID for waveform lookup (P8)
        asset_path: Optional[str] = None,  # Asset path for waveform extraction (P8)
        waveform_asset_id: Optional[str] = None,  # Shared source ID for virtual subclips
        fps: float = 24.0,  # Frame rate for time conversion (P8)
        color_stack_id: Optional[str] = None,  # P9.1: Color stack reference ID
        color_shared: bool = False,  # P9.1: Whether using shared asset grade
        fade_in_duration: int = 0,  # Phase 1.2: Fade in duration (frames)
        fade_out_duration: int = 0,  # Phase 1.2: Fade out duration (frames)
        # Phase 2: 片段徽章和标记属性
        speed: float = 1.0,  # Phase 2.2: 播放速度倍率
        retime_points: tuple[tuple[int, float], ...] = (),
        retime_controls_visible: bool = False,
        opacity: float = 1.0,  # Phase 2.4: 透明度 (0.0 - 1.0)
        effects: Optional[tuple] = None,  # Phase 2.1: 效果列表
        label_color: Optional[str] = None,  # P2: Clip label color
        waveform_show_channels: bool = False,
        is_enabled: bool = True,  # Clip enabled/disabled state
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        # Without ItemUsesExtendedStyleOption Qt passes the full boundingRect
        # as exposedRect, so the waveform's exposed-rect clipping is inert and
        # every playhead-strip update redraws the whole waveform.
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)
        # 播放头竖条/滚动重绘时，item 只需从缓存 blit 而不必重跑全部
        # Python 绘制逻辑（波形/缩略图/标签）。视觉状态变化（选中/hover/
        # 缩略图异步到达）调用 update() 会自动重建缓存。
        self.setCacheMode(
            QGraphicsItem.CacheMode.DeviceCoordinateCache)

        self._clip_id = clip_id
        self._track_id = track_id
        self._asset_name = asset_name
        self._start_frame = start_frame
        self._duration_frames = duration_frames
        self._pixels_per_frame = pixels_per_frame
        self._track_index = track_index
        self._is_selected = False
        self._is_enabled = is_enabled
        self._is_track_locked = is_track_locked
        self._hover_edge: Optional[str] = None  # 'left', 'right', 'fade_in', 'fade_out', or None
        self._track_height = track_height
        self._media_type = media_type
        self._linked_clip_id = linked_clip_id
        # Set by TimelineView from adjacent clips on the same track.  A
        # dashed seam indicates a cut between two pieces of one source asset.
        self._right_seam_is_same_source: bool = False
        self._label_color = label_color  # P2: User-set label color

        # P9.1: Color stack info for Grade mode
        self._color_stack_id = color_stack_id
        self._color_shared = color_shared

        # Waveform data (P8)
        self._asset_id = asset_id
        self._asset_path = asset_path
        # Virtual scene-cut subclips have their own asset IDs but point to the
        # same source file.  Keep the clip ID for thumbnails/identity while
        # sharing the parent ID for waveform extraction and its cache.
        self._waveform_asset_id = waveform_asset_id or asset_id
        self._fps = fps
        self._waveform_cache_key: Optional[str] = None  # Key for cached waveform pixmap
        self._waveform_pixmap: Optional[QPixmap] = None  # Cached waveform pixmap
        self._waveform_requested = False  # Whether waveform request was sent
        self._waveform_failed = False  # Whether waveform extraction failed
        self._waveform_extractor_ref: Optional[Any] = None
        self._waveform_signal_connected = False
        # 轨道音量（线性 0.0-2.0）驱动的波形显示缩放。绘制时围绕波形中线
        # 等比缩放已有 QPainterPath，不重建路径、不重提取峰值——音量拖动
        # 的视觉反馈成本仅为一次 update()。
        self._waveform_gain: float = 1.0
        # 效果器处理后的 per-clip 波形（覆盖 asset 原始波形显示）。
        # _effect_waveform_fp 是当前生效效果链的指纹；request_key 额外含
        # 源区间，修剪后自动重渲染。
        self._effect_waveform: Optional[WaveformData] = None
        self._effect_waveform_fp: Optional[str] = None
        self._effect_waveform_request_key: Optional[tuple] = None
        self._effect_waveform_renderer_ref: Optional[Any] = None
        self._effect_waveform_signal_connected = False
        self._disposed = False
        # Waveform path cache (参考 kdenlive: avoid per-frame redraw)
        self._waveform_path_cache: Optional[QPainterPath] = None  # Cached waveform path
        self._waveform_path_cache_key: Optional[str] = None  # Cache key (based on source range + display width)
        self._waveform_show_channels: bool = bool(waveform_show_channels)

        # Visibility culling state (参考 kdenlive: hideClipViews)
        self._hide_details: bool = False  # Hide waveform/thumbnails when clip width small

        # Phase 1.1: Clip thumbnail set cache
        self._thumbnail_request_key: Optional[str] = None  # Cache key for current request
        self._cached_thumbnail_set: Optional[Any] = None  # Cached ClipThumbnailSet
        self._thumbnail_signal_connected: bool = False  # Track signal connection
        self._thumbnail_provider_ref: Optional[Any] = None
        self._last_thumbnail_ppf: float = 0.0  # Last pixels_per_frame for thumbnail request
        self._thumbnail_pending: bool = False  # Track pending request state (UI-08: avoid frequent paint checks)

        # During a trim drag the model still contains the old source range.
        # Keep the original thumbnail set available as a temporary preview,
        # while waveform rendering and frame selection use the live range.
        self._trim_preview_active: bool = False
        self._trim_preview_source_start: int = source_start
        self._trim_preview_source_duration: int = source_duration

        # Source material info (for trim constraints)
        self._source_start = source_start
        self._source_duration = source_duration
        self._max_source_frames = max_source_frames

        # Active tool (for slip tool behavior)
        self._active_tool: EditTool = EditTool.SELECT

        # Slip drag state (P4.4)
        self._slip_delta: int = 0  # Current slip delta during drag

        # Drag state
        self._is_dragging = False
        self._drag_pending = False  # P2-6: True after press, before threshold is passed
        self._drag_start_pos = QPointF()
        self._drag_start_frame = 0
        self._drag_mode: Optional[str] = None  # 'move', 'trim_left', 'trim_right', 'slip'
        self._copy_mode = False  # Alt-drag copy mode
        # P5: Track change tracking during drag (set by TimelineView._on_clip_moved)
        self._drag_target_track_id: str = ""  # Non-empty = clip target track changed during drag
        self._last_move_emit: tuple[int, int] | None = None

        # Original state storage (for undo)
        self._original_start: int = 0
        self._original_duration: int = 0
        self._original_source_start: int = 0
        self._original_source_duration: int = 0
        # Final state storage (for ripple trim)
        self._final_new_start: int = 0  # Final new start frame for ripple trim left
        self._final_new_end: int = 0  # Final new end frame for ripple trim right

        # Phase 1.2: Fade in/out state
        self._fade_in_duration = fade_in_duration
        self._fade_out_duration = fade_out_duration
        self._fade_in_duration, self._fade_out_duration = self._clamp_fade_pair(
            self._fade_in_duration,
            self._fade_out_duration,
        )
        self._original_fade_in: int = 0  # Original fade in for undo
        self._original_fade_out: int = 0  # Original fade out for undo
        self._hover_fade_handle: Optional[str] = None  # 'fade_in' or 'fade_out' when hovering

        # Phase 2: 片段徽章和标记属性
        self._speed = speed  # 播放速度倍率
        self._retime_points = tuple(retime_points)
        self._retime_controls_visible = retime_controls_visible
        self._opacity = opacity  # 透明度
        self._effects = effects or ()  # 效果列表（默认空元组）
        # A2: 冻结帧（None = 正常播放）
        self._freeze_source_frame: Optional[int] = None
        # A1: 图层混合模式（"normal" = 无徽章）
        self._blend_mode: str = "normal"

        # Phase 2.5: 拖动帧号显示属性
        self._drag_frame_label_visible: bool = False  # 是否显示拖动帧号标签
        self._drag_target_frame: int = 0  # 拖动时的目标帧号
        self._is_snapping: bool = False  # 是否处于吸附状态

        # Phase 6.1: Linked group highlight state
        self._is_group_highlighted: bool = False  # Whether this clip is highlighted as part of a linked group
        # P2-03: persistent highlight when this clip (or its reciprocal
        # partner) is part of the current selection — shows which clips an
        # edit would carry, without fighting the transient hover highlight.
        self._linked_to_selection: bool = False
        self._linked_group_color_index: int = -1  # Color index for linked group (assigned by TimelineView)
        self._linked_selection_enabled: bool = True  # Whether linked A/V clips move together

        # Phase 6: Comp Clip visual indicator fields
        self._composite_id: str | None = None  # Composite graph reference ID
        self._is_comp_clip: bool = False  # Whether clip is a Comp Clip (media_type == "comp_clip")
        self._composite_bypassed: bool = False  # Whether composite is bypassed
        self._comp_enabled: bool = False  # Whether composite processing is enabled

        # Generator (Adjustment / Solid Color) indicator fields. _media_type
        # already distinguishes them for the paint gradient; these carry the
        # extra payload (solid colour swatch) needed by _draw_generator_badge.
        self._solid_color: tuple[float, float, float] | None = None  # RGB 0..1 for solid clips
        self._comp_cache_status: str = "none"  # none, dirty, rendering, ready, failed

        # Initial width (set before _update_geometry so the comparison works)
        self._width: float = duration_frames * pixels_per_frame

        # Enable hover events for edge detection (trim cursor, linked group highlight)
        self.setAcceptHoverEvents(True)

        self._update_geometry()

        logger.debug(f"ClipItem created: {clip_id} at frame {start_frame}")

    def dispose(self) -> None:
        """Detach asynchronous producers before removing this item.

        Timeline clips are frequently replaced by immutable model updates
        (for example, one clip becomes two after a blade cut).  The waveform
        extractor and thumbnail provider are shared objects, so their signals
        otherwise retain old ``ClipItem`` instances and continue calling
        ``update()`` after those items have left the scene.

        This method is intentionally idempotent and does not remove the item
        from the scene; the owning ``TimelineView`` performs that operation.
        """
        if self._disposed:
            return
        self._disposed = True

        extractor = self._waveform_extractor_ref
        if extractor is not None:
            try:
                extractor.waveform_ready.disconnect(self._on_waveform_ready)
            except (TypeError, RuntimeError):
                pass
            try:
                extractor.extraction_failed.disconnect(
                    self._on_waveform_extraction_failed
                )
            except (TypeError, RuntimeError):
                pass
        self._waveform_extractor_ref = None

        renderer = self._effect_waveform_renderer_ref
        if renderer is not None:
            try:
                renderer.effect_waveform_ready.disconnect(
                    self._on_effect_waveform_ready)
            except (TypeError, RuntimeError):
                pass
            try:
                renderer.effect_waveform_failed.disconnect(
                    self._on_effect_waveform_failed)
            except (TypeError, RuntimeError):
                pass
        self._effect_waveform_renderer_ref = None
        self._effect_waveform_signal_connected = False
        self._effect_waveform = None

        provider = self._thumbnail_provider_ref
        if provider is not None:
            try:
                provider.clip_thumbnail_set_ready.disconnect(
                    self._on_clip_thumbnails_ready
                )
            except (TypeError, RuntimeError):
                pass
        self._thumbnail_provider_ref = None
        self._waveform_signal_connected = False
        self._thumbnail_signal_connected = False
        self._waveform_requested = False
        self._thumbnail_pending = False

    def set_waveform_gain(self, gain: float) -> None:
        """Set the track-volume driven waveform display scaling.

        纯视觉更新：波形 path 缓存保持有效，paint 时围绕中线等比缩放。
        """
        clamped = max(0.0, min(2.0, float(gain)))
        if abs(clamped - self._waveform_gain) < 1e-4:
            return
        self._waveform_gain = clamped
        self.update()

    def set_drag_cache_mode(self, dragging: bool) -> None:
        """Disable the device cache while a clip is being repositioned.

        A cached QGraphicsItem can leave old device-pixel edges behind when
        its position changes repeatedly during a live drag.  Static clips
        keep the fast cache; moving clips render directly until the gesture
        finishes.
        """
        cache_mode = (
            QGraphicsItem.CacheMode.NoCache
            if dragging
            else QGraphicsItem.CacheMode.DeviceCoordinateCache
        )
        if self.cacheMode() != cache_mode:
            self.setCacheMode(cache_mode)
            self.update()

    def _clip_effect_entries(self) -> list:
        """Extract this clip's mixer-audio effect entries (EffectChain format).

        与 edit_page._sync_clip_audio_effects 相同的过滤/排序规则，保证
        离线波形听到的与实时引擎处理的一致。
        """
        effects = self._effects or ()
        if not effects:
            return []
        try:
            from src.features.edit.core.effect_registry import (
                AUDIO_MIXER_EFFECT_TYPES,
            )
        except Exception:
            return []
        entries: list = []
        for effect in sorted(effects, key=lambda ef: getattr(ef, "order", 0)):
            effect_type = getattr(effect, "effect_type", "")
            if effect_type not in AUDIO_MIXER_EFFECT_TYPES:
                continue
            params = getattr(effect, "params", None) or {}
            inst_enabled = bool(getattr(effect, "is_enabled", True))
            dsp_enabled = inst_enabled and bool(params.get("enabled", True))
            if dsp_enabled:
                entries.append((effect_type, dict(params), True))
        return entries

    def _clip_effect_fingerprint(self) -> str:
        """Stable fingerprint of the (enabled) clip audio effect chain."""
        entries = self._clip_effect_entries()
        if not entries:
            return ""
        import hashlib
        import json

        try:
            payload = json.dumps(entries, sort_keys=True, default=str)
        except Exception:
            payload = repr(entries)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _maybe_request_effect_waveform(self) -> None:
        """Queue an offline effect-waveform render when the chain changes.

        幂等：request_key（指纹+源区间+文件）未变时直接返回。效果清空时
        撤销覆盖波形，回到 asset 原始显示。
        """
        fingerprint = self._clip_effect_fingerprint()
        if not fingerprint or not self._asset_path or self._disposed:
            self._effect_waveform_fp = fingerprint or None
            if self._effect_waveform is not None:
                self._effect_waveform = None
                self._effect_waveform_request_key = None
                self._waveform_path_cache = None
                self._waveform_path_cache_key = None
                self.update()
            return
        self._effect_waveform_fp = fingerprint
        fps = self._fps if self._fps > 0 else 30.0
        request_key = (
            fingerprint, self._asset_path, self._source_start, self._source_duration)
        if request_key == self._effect_waveform_request_key and (
                self._effect_waveform is not None):
            return
        if request_key != self._effect_waveform_request_key:
            self._effect_waveform_request_key = request_key
            # 指纹/区间变化：旧覆盖立即失效，等待重渲染期间显示原始波形。
            self._effect_waveform = None
            self._waveform_path_cache = None
            self._waveform_path_cache_key = None
            self.update()
        try:
            renderer = ClipEffectWaveformRenderer.instance()
        except Exception:
            return
        if not self._effect_waveform_signal_connected:
            renderer.effect_waveform_ready.connect(self._on_effect_waveform_ready)
            renderer.effect_waveform_failed.connect(self._on_effect_waveform_failed)
            self._effect_waveform_signal_connected = True
            self._effect_waveform_renderer_ref = renderer
        request_effect_waveform(
            self._clip_id,
            self._asset_path,
            self._source_start / fps,
            self._source_duration / fps,
            self._clip_effect_entries(),
        )

    def _on_effect_waveform_ready(self, clip_id: str, waveform: WaveformData) -> None:
        """Swap in the rendered effect waveform and repaint."""
        if self._disposed or clip_id != self._clip_id:
            return
        fingerprint = self._clip_effect_fingerprint()
        if not fingerprint or fingerprint != (self._effect_waveform_fp or ""):
            return  # 渲染期间效果又被改掉了
        self._effect_waveform = waveform
        self._waveform_path_cache = None
        self._waveform_path_cache_key = None
        self.update()
        logger.debug(f"Effect waveform ready for clip {self._clip_id}")

    def _on_effect_waveform_failed(self, clip_id: str, error: str) -> None:
        """Handle effect-waveform render failure."""
        if self._disposed or clip_id != self._clip_id:
            return
        if error == EFFECT_WAVEFORM_DEFERRED_ERROR:
            # 播放抢占：清 key 让下一次 paint 重新排队（播放结束后执行）。
            self._effect_waveform_request_key = None
            return
        # 真实失败：保持显示 asset 波形，保留 request_key 防止每帧重试。
        logger.debug(f"Effect waveform failed for clip {self._clip_id}: {error}")

    # ==================== Properties ====================

    @property
    def clip_id(self) -> str:
        return self._clip_id

    @property
    def track_id(self) -> str:
        return self._track_id

    @property
    def start_frame(self) -> int:
        return self._start_frame

    @property
    def end_frame(self) -> int:
        return self._start_frame + self._duration_frames

    @property
    def duration_frames(self) -> int:
        return self._duration_frames

    @property
    def media_type(self) -> str:
        return self._media_type
    
    @property
    def fixed_y(self) -> float:
        """
        Get fixed Y coordinate for this track (轨道锁定).

        NOTE: This is an approximation. The actual Y is set by
        TimelineView._layout_track_items() via the layout engine.
        """
        # Approximate Y for compatibility; view overrides via setPos()
        return TRACK_AREA_TOP + self._track_index * (self._track_height + TRACK_GAP)

    @property
    def linked_clip_id(self) -> Optional[str]:
        return self._linked_clip_id

    @property
    def asset_id(self) -> Optional[str]:
        return self._asset_id

    @property
    def asset_path(self) -> Optional[str]:
        return self._asset_path

    # P9.1: Color stack properties for Grade mode
    @property
    def color_stack_id(self) -> Optional[str]:
        """Get color stack reference ID."""
        return self._color_stack_id

    @property
    def color_shared(self) -> bool:
        """Get whether using shared asset grade."""
        return self._color_shared

    @property
    def grade_mode(self) -> str:
        """Get grade mode string for UI display."""
        if self._color_shared:
            return "shared"
        return "local"

    # Phase 1.2: Fade in/out properties
    @property
    def fade_in_duration(self) -> int:
        """Get fade in duration in frames."""
        return self._fade_in_duration

    @property
    def fade_out_duration(self) -> int:
        """Get fade out duration in frames."""
        return self._fade_out_duration

    def _clamp_fade_pair(self, fade_in: int, fade_out: int) -> tuple[int, int]:
        """Keep the two fade ramps within the clip without crossing."""
        duration = max(0, int(self._duration_frames))
        clamped_in = max(0, min(int(fade_in), duration))
        clamped_out = max(0, min(int(fade_out), duration - clamped_in))
        return clamped_in, clamped_out

    def set_fade_in_duration(self, duration: int) -> None:
        """Set fade in duration (for live preview during drag)."""
        self._fade_in_duration, self._fade_out_duration = self._clamp_fade_pair(
            duration,
            self._fade_out_duration,
        )
        self.update()

    def set_fade_out_duration(self, duration: int) -> None:
        """Set fade out duration (for live preview during drag)."""
        self._fade_in_duration, self._fade_out_duration = self._clamp_fade_pair(
            self._fade_in_duration,
            duration,
        )
        self.update()

    # Phase 2: 片段徽章和标记属性
    @property
    def speed(self) -> float:
        """Get playback speed multiplier."""
        return self._speed

    @property
    def opacity(self) -> float:
        """Get opacity value (0.0 - 1.0)."""
        return self._opacity

    @property
    def effects(self) -> tuple:
        """Get effects list (tuple of effect instances)."""
        return self._effects

    def has_effects(self) -> bool:
        """Check if clip has any effects applied."""
        return len(self._effects) > 0

    def is_speed_modified(self) -> bool:
        """Check if speed is not default (1.0)."""
        return self._speed != 1.0 or bool(self._retime_points)

    def set_retime_controls_visible(self, visible: bool) -> None:
        """Show or hide this clip's Resolve-style retime control strip."""
        visible = bool(visible)
        if self._retime_controls_visible == visible:
            return
        self._retime_controls_visible = visible
        self.update()

    def is_opacity_modified(self) -> bool:
        """Check if opacity is not default (1.0)."""
        return self._opacity < 1.0

    # Phase 6: Comp Clip properties
    @property
    def composite_id(self) -> str | None:
        """Get composite graph reference ID."""
        return self._composite_id

    @property
    def is_comp_clip(self) -> bool:
        """Check if this clip is a Comp Clip."""
        return self._is_comp_clip

    @property
    def comp_enabled(self) -> bool:
        """Check if composite processing is enabled."""
        return self._comp_enabled

    @property
    def comp_cache_status(self) -> str:
        """Get composite cache status (none, dirty, rendering, ready, failed)."""
        return self._comp_cache_status

    def set_comp_state(
        self,
        composite_id: str | None = None,
        is_comp_clip: bool | None = None,
        comp_enabled: bool | None = None,
        comp_cache_status: str | None = None,
        composite_bypassed: bool | None = None,
    ) -> None:
        """Update comp clip visual state and trigger repaint."""
        if composite_id is not None:
            self._composite_id = composite_id
        if is_comp_clip is not None:
            self._is_comp_clip = is_comp_clip
        if comp_enabled is not None:
            self._comp_enabled = comp_enabled
        if comp_cache_status is not None:
            self._comp_cache_status = comp_cache_status
        if composite_bypassed is not None:
            self._composite_bypassed = composite_bypassed
        self.update()

    # ==================== Geometry ====================

    def _update_geometry(self) -> None:
        """
        Update item width only. Position (setPos) is managed by TimelineView.

        NOTE: This method only updates the local width. Y position and
        final X position are set by TimelineView._layout_track_items()
        through the layout engine.
        """
        width = self._duration_frames * self._pixels_per_frame
        if abs(self._width - width) > 0.5:
            self.prepareGeometryChange()
            self._width = width
            self._update_cache_mode()

    def _update_cache_mode(self) -> None:
        """按像素宽度开关 DeviceCoordinateCache（内存护栏）。

        极端缩放下超长 clip 的设备像素图可达几十 MB；超过阈值时关闭
        缓存退回直接绘制（播放头竖条场景很少与超长 clip 同时高缩放）。
        """
        try:
            item_px = self._width * max(
                1.0,
                self.scene().views()[0].transform().m11() if self.scene()
                and self.scene().views() else 1.0,
            )
        except Exception:
            item_px = self._width
        want_cache = item_px <= 8192.0
        has_cache = (
            self.cacheMode()
            == QGraphicsItem.CacheMode.DeviceCoordinateCache
        )
        if want_cache != has_cache:
            self.setCacheMode(
                QGraphicsItem.CacheMode.DeviceCoordinateCache
                if want_cache
                else QGraphicsItem.CacheMode.NoCache
            )

    def boundingRect(self) -> QRectF:
        """Return bounding rectangle for the item.

        Gemini suggestion: Include border width to prevent ghosting/residual artifacts.
        Selected border width is 2px, so expand by 2 on each side.
        """
        # Use the maximum cosmetic border width for safe invalidation bounds.
        border_margin = max(CLIP_BORDER_WIDTH, CLIP_SELECTED_BORDER_WIDTH)
        return QRectF(
            -border_margin,
            -border_margin,
            self._width + 2 * border_margin,
            self._track_height + 2 * border_margin
        )

    def shape(self) -> QPainterPath:
        """Return only the logical clip area for hit testing.

        ``boundingRect()`` intentionally includes a small paint invalidation
        margin for the selected border.  That margin must not become part of
        the mouse hit target: adjacent clips otherwise overlap by a few
        pixels and the following clip can steal clicks at the preceding
        clip's out-point.
        """
        path = QPainterPath()
        path.addRect(QRectF(0, 0, self._width, self._track_height))
        return path

    # ==================== P5: Thumbnail LOD & Cache ====================

    @staticmethod
    def _calculate_thumbnail_mode(clip_width: float) -> str:
        """
        P5-1: Determine thumbnail rendering mode based on clip width.

        Implements Level-of-Detail (LOD) for video thumbnails:
        - Very narrow (< 40px): No thumbnails, only label bar
        - Narrow (40-120px): Single first-frame thumbnail
        - Medium (120-260px): First and last frame only
        - Wide (> 260px): Filmstrip with evenly spaced frames

        Args:
            clip_width: Current clip width in pixels

        Returns:
            One of THUMBNAIL_MODE_NONE/SINGLE/START_END/FILMSTRIP
        """
        if clip_width < THUMBNAIL_WIDTH_NONE:
            return THUMBNAIL_MODE_NONE
        elif clip_width < THUMBNAIL_WIDTH_SINGLE:
            return THUMBNAIL_MODE_SINGLE
        elif clip_width < THUMBNAIL_WIDTH_START_END:
            return THUMBNAIL_MODE_START_END
        else:
            return THUMBNAIL_MODE_FILMSTRIP

    def _cache_key(self) -> str:
        """
        P5-4: Generate cache key for thumbnail/waveform lookup.

        Includes asset_id, visible range (source start/duration), render height,
        and zoom bucket to avoid cache misses on minor zoom changes.
        """
        zoom_bucket = int(self._width / THUMBNAIL_CACHE_BUCKET_SIZE) * THUMBNAIL_CACHE_BUCKET_SIZE
        return (
            f"{self._asset_id}_{self._source_start}_{self._source_duration}"
            f"_{self._track_height}_{zoom_bucket}"
        )

    @staticmethod
    def _static_cache_key(clip_id: str, width: int, height: int) -> str:
        """Generate cache key for static lookups."""
        return f"{clip_id}_{width}_{height}"

    @classmethod
    def invalidate_cache(
        cls,
        clip_id: Optional[str] = None,
        asset_id: Optional[str] = None,
    ) -> None:
        """
        P5-4: Invalidate thumbnail/waveform cache.

        Args:
            clip_id: If provided, only invalidate entries for this clip.
                     If None, clear entire cache.
        """
        if clip_id or asset_id:
            keys_to_remove = [
                k for k in cls._thumbnail_cache
                if clip_id and k.startswith(clip_id)
            ]
            for k in keys_to_remove:
                del cls._thumbnail_cache[k]
            # Thumbnail entries are clip-id keyed, waveform entries are
            # asset-id keyed. Previously waveform-ready invalidation passed a
            # clip id and therefore never removed stale waveform paths.
            waveform_prefix = asset_id or clip_id
            wave_keys = [
                k for k in cls._waveform_cache_shared
                if waveform_prefix and k.startswith(waveform_prefix)
            ]
            for k in wave_keys:
                del cls._waveform_cache_shared[k]
        else:
            cls._thumbnail_cache.clear()
            cls._waveform_cache_shared.clear()

    @classmethod
    def _get_shared_waveform_paths(cls, key: str) -> Optional[list[QPainterPath]]:
        """Return a shared waveform path entry and refresh its LRU position."""
        paths = cls._waveform_cache_shared.get(key)
        if paths is not None:
            cls._waveform_cache_shared.move_to_end(key)
        return paths

    @classmethod
    def _cache_shared_waveform_paths(cls, key: str, paths: list[QPainterPath]) -> None:
        """Store shared waveform paths with a bounded LRU memory budget."""
        cls._waveform_cache_shared[key] = paths
        cls._waveform_cache_shared.move_to_end(key)
        while len(cls._waveform_cache_shared) > cls._waveform_cache_shared_max_size:
            cls._waveform_cache_shared.popitem(last=False)

    def _waveform_start_offset_samples(self) -> int:
        """
        P5-3: Calculate start offset in samples from the source asset.

        Maps the source in-point (clip_start_in_source / source_start) to
        an offset in the waveform sample array. This ensures the waveform
        correctly shows only the visible portion when a clip is trimmed.

        Returns:
            Sample offset in the waveform data array
        """
        clip_start_frames = self._source_start  # Source in-point
        fps = self._fps if self._fps > 0 else 30.0
        sample_rate = 44100  # Standard; could be from asset metadata
        return int(clip_start_frames * sample_rate / fps)

    # ==================== Painting ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """
        Paint the clip item with professional video editor styling.

        IMPORTANT: paint() must NOT call setPos(). Position is managed
        exclusively by TimelineView._layout_track_items() via the layout engine.

        参考 kdenlive Clip.qml 设计：
        - 渐变背景（视频蓝色、音频绿色、转场特殊）
        - 片段名称显示（左上角）
        - 源信息显示（底部）
        - 裁剪手柄（左右边缘）
        - 选中/禁用/锁定状态视觉指示
        """
        # NOTE: Do NOT call setPos() or any position-changing method in paint().
        # Position is set by TimelineView._layout_track_items() only.
        painter.save()
        is_move_drag = self._is_dragging and self._drag_mode == "move"
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, not is_move_drag)
        self._apply_track_area_clip(painter)

        # LOD: Level of detail based on clip width (DaVinci-like adaptive rendering)
        clip_width = self._width
        is_narrow = clip_width < 36      # Very narrow: color block only
        is_medium = clip_width < 90      # Medium: color + short name
        is_wide = clip_width >= 90       # Wide: full details with thumbnails

        # P5-1: Thumbnail mode — use user preference if set, otherwise auto LOD
        if ClipItem._thumbnail_display_mode == "auto":
            thumbnail_mode = self._calculate_thumbnail_mode(clip_width)
        elif ClipItem._thumbnail_display_mode == "none":
            thumbnail_mode = THUMBNAIL_MODE_NONE
        elif ClipItem._thumbnail_display_mode == "start_end":
            # Force start_end mode, but respect minimum width for single
            if clip_width < THUMBNAIL_WIDTH_NONE:
                thumbnail_mode = THUMBNAIL_MODE_NONE
            else:
                thumbnail_mode = THUMBNAIL_MODE_START_END
        else:
            thumbnail_mode = ClipItem._thumbnail_display_mode  # "filmstrip" etc.

        # Determine colors based on track role. Linked A/V clips can carry a
        # video asset type while being placed on an audio track.
        is_audio = self._is_audio_track()
        is_transition = self._media_type == "transition"
        is_composite_clip = bool(self._is_comp_clip or self._composite_id)
        # Generators get their own distinct hues (purple = adjustment,
        # slate = solid) so they read at a glance on the timeline.
        is_adjustment = self._media_type == "adjustment"
        is_solid = self._media_type == "solid"
        is_motion_graphic = self._media_type == "motion_graphic"

        # Solid clips: derive body gradient from the actual colour so the
        # timeline visually reflects the user's chosen colour.
        if is_solid and self._solid_color:
            _sc = QColor.fromRgbF(
                max(0.0, min(1.0, self._solid_color[0])),
                max(0.0, min(1.0, self._solid_color[1])),
                max(0.0, min(1.0, self._solid_color[2])),
            )
            _solid_grad_start = _sc.lighter(110)
            _solid_grad_end = _sc.darker(110)
            _solid_base = _sc
        else:
            _solid_grad_start = CLIP_SOLID_GRADIENT_START
            _solid_grad_end = CLIP_SOLID_GRADIENT_END
            _solid_base = CLIP_SOLID_GRADIENT_START

        # Setup gradient colors based on media type. A custom label color
        # contributes hue only; its original saturation/lightness is not used
        # directly, which prevents neon clip bodies in older projects.
        label_palette = _derive_label_palette(self._label_color)
        if label_palette is not None:
            gradient_start, gradient_end = label_palette[0], label_palette[1]
        elif is_audio:
            gradient_start = CLIP_AUDIO_GRADIENT_START
            gradient_end = CLIP_AUDIO_GRADIENT_END
        elif is_adjustment:
            gradient_start = CLIP_ADJUSTMENT_GRADIENT_START
            gradient_end = CLIP_ADJUSTMENT_GRADIENT_END
        elif is_solid:
            gradient_start = _solid_grad_start
            gradient_end = _solid_grad_end
        elif is_motion_graphic:
            gradient_start = QColor("#8d62c4")
            gradient_end = QColor("#3b245d")
        elif is_composite_clip:
            gradient_start = CLIP_COMP_GRADIENT_START
            gradient_end = CLIP_COMP_GRADIENT_END
        elif is_transition:
            gradient_start = CLIP_TRANSITION_GRADIENT_START
            gradient_end = CLIP_TRANSITION_GRADIENT_END
        else:
            gradient_start = CLIP_VIDEO_GRADIENT_START
            gradient_end = CLIP_VIDEO_GRADIENT_END

        # Apply selection state for border
        CLIP_NORMAL_BORDER = QColor(0x1A, 0x1A, 0x2A)  # #1A1A2A dark border for normal state
        if self._hover_edge == "left" and not self._is_track_locked:
            border_color = TRIM_IN_HOVER_COLOR
            border_width = CLIP_SELECTED_BORDER_WIDTH
        elif self._hover_edge == "right" and not self._is_track_locked:
            border_color = TRIM_OUT_HOVER_COLOR
            border_width = CLIP_SELECTED_BORDER_WIDTH
        elif self._is_selected:
            border_color = CLIP_SELECTED_BORDER  # Orange/amber highlight
            border_width = CLIP_SELECTED_BORDER_WIDTH  # 2px
        elif self._hover_edge and not self._is_track_locked:
            border_color = CLIP_HOVER_BORDER  # Light blue-gray for hover
            border_width = CLIP_SELECTED_BORDER_WIDTH
        else:
            border_color = CLIP_NORMAL_BORDER
            border_width = CLIP_BORDER_WIDTH

        # Check for disabled/muted/locked states
        is_disabled = not self._is_enabled
        is_locked = self._is_track_locked

        # Draw only inside the logical clip span. boundingRect() is larger for
        # invalidation margins; using it for visuals lets adjacent clips overlap.
        clip_rect = QRectF(0, 0, self._width, self._track_height)

        # Apply transparency for disabled/muted states
        if is_disabled or is_locked:
            painter.setOpacity(0.5)

        # Keep neighbouring clip bodies continuous and reserve exactly one
        # cosmetic-pixel separator at each cut.  The previous inset on both
        # clips combined with their outlines into a distracting black column.
        content_rect = clip_rect.adjusted(0.0, 0.5, -0.5, -0.5)
        if content_rect.width() <= 0 or content_rect.height() <= 0:
            painter.restore()
            return

        # Create and draw gradient background (DaVinci-like vertical gradient)
        gradient = QLinearGradient(0, 0, 0, content_rect.height())
        gradient.setColorAt(0.0, gradient_start)
        gradient.setColorAt(1.0, gradient_end)

        painter.setBrush(QBrush(gradient))
        if self._is_selected or self._hover_edge:
            painter.setPen(QPen(border_color, border_width))
            painter.drawRoundedRect(content_rect, 2, 2)
        else:
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawRoundedRect(content_rect, 2, 2)

        # A restrained upper edge gives un-thumbnailed clips depth without
        # turning every clip into a glossy card.
        painter.setPen(QPen(QColor(255, 255, 255, 12), 1))
        painter.drawLine(
            QPointF(content_rect.left() + 2.0, content_rect.top() + 0.5),
            QPointF(content_rect.right() - 2.0, content_rect.top() + 0.5),
        )

        # One subtle cut line, owned by the clip on its left.  It stays clear
        # over busy thumbnails without looking like a wide black gap.
        if not self._is_selected and not self._hover_edge:
            seam_pen = QPen(QColor(8, 12, 16, 175), 1)
            if self._right_seam_is_same_source:
                seam_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(seam_pen)
            painter.drawLine(
                QPointF(content_rect.right(), content_rect.top() + 1.0),
                QPointF(content_rect.right(), content_rect.bottom() - 1.0),
            )

        # During a move drag, paint only a lightweight card. Thumbnails,
        # waveforms, badges and fade handles are expensive and do not help the
        # user place a clip. The normal detailed path resumes after release.
        if is_move_drag:
            self._paint_move_drag_preview(painter, clip_rect, is_locked, is_audio)
            painter.restore()
            return

        # LOD: Skip all details for very narrow clips
        if is_narrow:
            # Just draw color block with selection border (already done above)
            if is_disabled or is_locked:
                painter.setOpacity(1.0)
                self._draw_disabled_pattern(painter, clip_rect)
            painter.restore()
            return

        # Skip details if hide_details is True (参考 kdenlive: hideClipViews)
        hide_details = getattr(self, '_hide_details', False)

        # Draw video thumbnails for video/image clips (P5-1: LOD mode)
        # P5: Use thumbnail_mode instead of simple is_wide check.
        # Generators (adjustment / solid / motion_graphic) have no source
        # frames to thumbnail —
        # skip the draw entirely so no background request is ever scheduled
        # (avoids the "Unknown format" warning spam for topos:// paths).
        if (
            not is_audio and not is_transition and not hide_details
            and thumbnail_mode != THUMBNAIL_MODE_NONE
            and not is_adjustment and not is_solid and not is_motion_graphic
        ):
            self._draw_video_thumbnails(painter, clip_rect, thumbnail_mode)

        # Keep the source thumbnail, but tint it very lightly so a Comp Clip
        # still reads as a different timeline object instead of only relying
        # on the small badge in the corner.
        if is_composite_clip and not is_audio and not hide_details:
            painter.setBrush(QBrush(QColor(93, 74, 150, 34)))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawRect(clip_rect)

        # Draw diagonal stripe pattern for disabled/muted
        if is_disabled or is_locked:
            # AUD-21110: the pattern itself looks best fully opaque, but a
            # bare setOpacity(1.0) leaked into the waveform / label bar below
            # so disabled/locked clips lost their translucent look.  Draw the
            # pattern inside save()/restore() to keep the 0.5 opacity for the
            # remaining elements.
            painter.save()
            painter.setOpacity(1.0)  # Reset for pattern
            self._draw_disabled_pattern(painter, clip_rect)
            painter.restore()

        # Draw waveform for every readable audio clip.  ``is_medium`` means
        # width < 90px, so using it here accidentally suppressed waveforms on
        # the wide clips where users most expect to see them.
        if is_audio and not hide_details and not is_narrow:
            # Give the envelope a quiet, translucent analysis bed. This is
            # independent of the label hue so the waveform remains readable
            # on every custom clip color.
            waveform_panel = QRectF(
                3.0,
                4.0,
                max(0.0, clip_width - 6.0),
                max(0.0, self._track_height - 26.0),
            )
            if waveform_panel.width() > 1.0 and waveform_panel.height() > 1.0:
                painter.setBrush(QBrush(QColor(5, 10, 13, 28)))
                painter.setPen(QPen(Qt.PenStyle.NoPen))
                painter.drawRoundedRect(waveform_panel, 2.0, 2.0)
            self._draw_waveform(painter, option.exposedRect)

        # Draw asset name with text protection bar (DaVinci-like label bar)
        if not hide_details or is_wide:
            # P4: Use media-type-specific label bar color for professional look
            # When a label color is set, derive the bar from it
            if label_palette is not None:
                label_bar_bg = label_palette[2]
            elif is_audio:
                label_bar_bg = CLIP_AUDIO_LABEL_BAR
            elif is_transition:
                label_bar_bg = QColor(0x1E, 0x2A, 0x1E)  # Dark warm for transition
            elif is_composite_clip:
                label_bar_bg = CLIP_COMP_LABEL_BAR
            else:
                label_bar_bg = CLIP_VIDEO_LABEL_BAR

            # Both media types use a shared bottom information strip.  This
            # gives the waveform the same clear main area as video thumbnails
            # and makes linked A/V clips read as one coherent unit.
            label_bar_rect = QRectF(0.5, clip_rect.height() - 19, clip_width - 1.0, 18)
            label_bar_color = QColor(label_bar_bg)
            label_bar_color.setAlpha(212)
            painter.setBrush(QBrush(label_bar_color))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawRoundedRect(label_bar_rect, 2, 2)

            name_font = QFont("Arial", 8)
            name_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(name_font)
            name_color = TEXT_COLOR if not is_locked else TEXT_MUTED_COLOR
            painter.setPen(QPen(name_color))

            # Clip name area with elide (leave space for linked indicator)
            text_offset = 6
            if self._linked_clip_id:
                text_offset = 22

            name_rect = QRectF(
                text_offset,
                clip_rect.height() - 17,
                clip_width - text_offset - 6,
                13,
            )
            # Use font metrics to elide text that doesn't fit
            from PyQt6.QtGui import QFontMetrics
            metrics = QFontMetrics(name_font)
            elided = metrics.elidedText(self._asset_name, Qt.TextElideMode.ElideRight, int(name_rect.width()))
            painter.drawText(name_rect, Qt.TextFlag.TextSingleLine, elided)

            # Do not stack source in/out text inside this compact strip.  It
            # competes with the clip name and makes dense edits feel noisy.

        # Draw linked clip indicator in the label strip, immediately before
        # the clip name.  This follows the Resolve-style timeline treatment;
        # the link state belongs with the clip identity rather than floating
        # in the top-right over the picture/waveform.
        if self._linked_clip_id and not is_narrow:
            self._draw_linked_indicator(painter)

        # Phase 6.1: Draw linked group border (bottom colored line)
        # Only show when linked selection is enabled (clips move together)
        if self._linked_clip_id and not is_narrow and self._linked_selection_enabled:
            self._draw_linked_group_border(painter, clip_rect)

        # Draw lock indicator in top-right if locked
        if is_locked and is_medium:
            self._draw_locked_indicator(painter)

        # Calculate dynamic handle size based on zoom level
        dynamic_handle_size = max(6, min(15, int(10 * self._pixels_per_frame / 0.5)))

        # Draw trim handles based on active tool and hover state
        if not is_locked and (is_medium or self._hover_edge is not None or self._is_selected):
            self._draw_trim_handles(painter, dynamic_handle_size)

        # Phase 1.2: Draw fade handles and overlay for both video and audio
        # clips.  Audio clips use the same timeline-frame duration as video
        # clips; their audio envelope is applied by the audio mixer.
        if (
            not is_transition
            and not is_locked
            and (not hide_details or self._is_selected)
            and is_wide
        ):
            self._draw_fade_handles(painter, clip_rect)

        # Phase 2: Draw badge and indicators (only for wide clips)
        if not is_locked and not hide_details and is_wide:
            # Phase 2.3: Draw clip type icon
            self._draw_clip_type_icon(painter, clip_rect)

            # Phase 2.1: Draw effects badge
            self._draw_effects_badge(painter, clip_rect)

            # Phase 2.2: Draw speed indicator
            self._draw_speed_indicator(painter, clip_rect)

            # A2: Draw freeze badge ("FZ") when the clip is frozen
            self._draw_freeze_badge(painter, clip_rect)

            # A1: Draw blend-mode abbreviation badge when non-normal
            self._draw_blend_badge(painter, clip_rect)

            # Phase 2.4: Draw opacity indicator
            self._draw_opacity_indicator(painter, clip_rect)

        # Resolve-style retime controls intentionally sit on the clip instead
        # of opening a modal editor.  The strip remains a UI-only overlay;
        # every actual timing edit is emitted back to TimelineView as a
        # command, preserving undo/redo and preview/export parity.
        if self._retime_controls_visible and not is_locked and is_wide:
            self._draw_retime_controls(painter, clip_rect)

        # Phase 6: Draw COMP badge (for comp clips or clips with composite_id)
        if is_composite_clip and clip_width >= MIN_CLIP_WIDTH_FOR_NAME and not hide_details:
            self._draw_comp_badge(painter, clip_rect)

        # Generator badge (Adjustment / Solid Color) — distinct from COMP.
        if (is_adjustment or is_solid or is_motion_graphic) and is_medium and not hide_details:
            self._draw_generator_badge(painter, clip_rect)

        # Phase 6: Draw cache status line at bottom of clip
        if self._comp_cache_status != "none" and self._comp_cache_status:
            self._draw_comp_cache_status(painter, clip_rect)

        # Phase 2.5: Draw drag frame label (if visible during dragging)
        if self._drag_frame_label_visible:
            self._draw_drag_frame_label(painter, clip_rect)

        # Restore painter state (matches save() at start of paint())
        painter.restore()

    def _paint_move_drag_preview(
        self,
        painter: QPainter,
        clip_rect: QRectF,
        is_locked: bool,
        is_audio: bool,
    ) -> None:
        """Paint the minimal visual needed while a clip is being moved."""
        label_rect = QRectF(0.5, clip_rect.height() - 19, self._width - 1.0, 18)
        label_color = QColor(
            CLIP_AUDIO_LABEL_BAR if is_audio else CLIP_VIDEO_LABEL_BAR
        )
        label_color.setAlpha(220)
        painter.setBrush(QBrush(label_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRect(label_rect)

        label = self._asset_name
        if len(label) > 36:
            label = f"{label[:33]}..."
        painter.setFont(QFont("Arial", 8))
        painter.setPen(QPen(TEXT_MUTED_COLOR if is_locked else TEXT_COLOR))
        painter.drawText(
            QRectF(6.0, clip_rect.height() - 17.0, max(1.0, self._width - 12.0), 13.0),
            Qt.TextFlag.TextSingleLine,
            label,
        )

    def _apply_track_area_clip(self, painter: QPainter) -> None:
        """Clip scrolling track content to its V/A area so split scrolling feels continuous."""
        if self._is_dragging or self._drag_pending:
            return
        try:
            view = self.scene().views()[0] if self.scene() and self.scene().views() else None
            engine = getattr(view, "_layout_engine", None)
            if engine is None:
                return
            area = engine.area_layout()
            scene_clip = (
                area.audio_area
                if self._is_audio_track()
                else area.video_area
            )
            scene_y = self.scenePos().y()
            local_clip = QRectF(
                -100000.0,
                scene_clip.top() - scene_y,
                200000.0,
                scene_clip.height(),
            )
            painter.setClipRect(local_clip, Qt.ClipOperation.IntersectClip)
        except (RuntimeError, IndexError, AttributeError):
            return

    def _is_audio_track(self) -> bool:
        """Return True when this item currently belongs to an audio track."""
        try:
            view = self.scene().views()[0] if self.scene() and self.scene().views() else None
            timeline = getattr(view, "_timeline", None)
            track = timeline.get_track_by_id(self._track_id) if timeline is not None else None
            if track is not None:
                return getattr(track, "track_type", "") == "audio"
        except (RuntimeError, IndexError, AttributeError):
            pass
        return self._media_type == "audio" or self._track_id.lower().startswith("a")

    def _draw_fade_handles(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw Resolve-style fade ramps and endpoint handles (Phase 1.2).

        The diagonal ramp makes the actual fade range visible.  The endpoint
        handle moves with the range, so the user can grab the same visual
        boundary they are adjusting instead of hunting for a fixed corner.

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
        """
        # Only show fade handles for clips wider than minimum width
        if self._width < MIN_CLIP_WIDTH_FOR_FADE_HANDLES:
            return

        fade_in_duration, fade_out_duration = self._clamp_fade_pair(
            self._fade_in_duration,
            self._fade_out_duration,
        )

        # Draw the ranges first so the handles and ramps stay crisp above the
        # thumbnails/waveform.  The pair is clamped so the diagonals can meet
        # at most at the clip center and never cross.
        if fade_in_duration > 0:
            fade_in_width = self._fade_visual_width(fade_in_duration)
            self._draw_fade_overlay(painter, clip_rect, fade_in_width, "in")

        if fade_out_duration > 0:
            fade_out_width = self._fade_visual_width(fade_out_duration)
            self._draw_fade_overlay(painter, clip_rect, fade_out_width, "out")

        # Selected clips expose both controls even before a fade exists.
        show_fade_in_handle = fade_in_duration > 0 or self._is_selected or self._hover_fade_handle == "fade_in"
        show_fade_out_handle = fade_out_duration > 0 or self._is_selected or self._hover_fade_handle == "fade_out"

        if show_fade_in_handle:
            fade_in_x, _ = self._fade_handle_position(clip_rect, "in")
            self._draw_fade_triangle(
                painter, fade_in_x - FADE_HANDLE_SIZE / 2.0,
                clip_rect.top() + 2.0,
                "down",
                hover=self._hover_fade_handle == "fade_in",
            )

        if show_fade_out_handle:
            fade_out_x, _ = self._fade_handle_position(clip_rect, "out")
            self._draw_fade_triangle(
                painter, fade_out_x - FADE_HANDLE_SIZE / 2.0,
                clip_rect.top() + 2.0,
                "down",
                hover=self._hover_fade_handle == "fade_out",
            )

    def _fade_visual_width(self, duration: int) -> float:
        """Return the on-screen width for a fade, leaving room for handles."""
        max_width = max(0.0, self._width - float(FADE_HANDLE_SIZE + 6))
        raw_width = max(0.0, float(duration) * self._pixels_per_frame)
        if duration > 0:
            # Keep a one-frame fade visible and grabbable at normal zoom.
            raw_width = max(raw_width, FADE_HANDLE_SIZE / 2.0 + 1.0)
        return min(max_width, raw_width)

    def _fade_handle_position(self, clip_rect: QRectF, fade_type: str) -> tuple[float, float]:
        """Return the center point of a fade endpoint handle in item coords."""
        inset = max(FADE_HANDLE_SIZE / 2.0 + 1.0, 8.0)
        fade_in_duration, fade_out_duration = self._clamp_fade_pair(
            self._fade_in_duration,
            self._fade_out_duration,
        )
        fade_duration = fade_in_duration if fade_type == "in" else fade_out_duration
        fade_width = self._fade_visual_width(fade_duration)
        if fade_type == "in":
            x = clip_rect.left() + (fade_width if fade_duration > 0 else inset)
        else:
            x = clip_rect.right() - (fade_width if fade_duration > 0 else inset)
        return x, clip_rect.top() + FADE_HANDLE_SIZE / 2.0 + 1.0

    def _fade_handle_at(self, pos: QPointF) -> Optional[str]:
        """Return the fade handle under ``pos`` using a forgiving hit area."""
        if self._media_type == "transition" or self._width < MIN_CLIP_WIDTH_FOR_FADE_HANDLES:
            return None

        clip_rect = QRectF(0.0, 0.0, self._width, self._track_height)
        candidates: list[tuple[float, str]] = []
        for fade_type in ("in", "out"):
            x, y = self._fade_handle_position(clip_rect, fade_type)
            hit_rect = QRectF(
                x - FADE_HANDLE_HIT_RADIUS,
                clip_rect.top(),
                FADE_HANDLE_HIT_RADIUS * 2.0,
                min(float(FADE_HANDLE_HIT_HEIGHT), clip_rect.height()),
            )
            if hit_rect.contains(pos):
                candidates.append((abs(pos.x() - x), fade_type))

        if not candidates:
            return None
        return min(candidates, key=lambda candidate: candidate[0])[1]

    def _draw_fade_overlay(
        self,
        painter: QPainter,
        clip_rect: QRectF,
        fade_width: float,
        fade_type: str
    ) -> None:
        """
        Draw fade overlay gradient (Phase 1.2).

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
            fade_width: Width of fade region in pixels
            fade_type: 'in' or 'out'
        """
        if fade_width <= 0:
            return

        painter.save()

        if fade_type == "in":
            start = QPointF(clip_rect.left(), clip_rect.bottom())
            end = QPointF(clip_rect.left() + fade_width, clip_rect.top())
            region = QPainterPath()
            region.moveTo(clip_rect.left(), clip_rect.top())
            region.lineTo(end)
            region.lineTo(start)
            region.closeSubpath()
        else:
            start = QPointF(clip_rect.right() - fade_width, clip_rect.top())
            end = QPointF(clip_rect.right(), clip_rect.bottom())
            region = QPainterPath()
            region.moveTo(start)
            region.lineTo(clip_rect.right(), clip_rect.top())
            region.lineTo(end)
            region.closeSubpath()

        # A restrained translucent wedge shows the affected range without
        # hiding the image or waveform underneath it.
        painter.setBrush(QBrush(FADE_REGION_COLOR))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPath(region)

        active = self._is_selected or self._hover_fade_handle == ("fade_in" if fade_type == "in" else "fade_out")
        ramp_color = QColor(FADE_RAMP_COLOR)
        ramp_color.setAlpha(235 if active else 175)
        painter.setPen(QPen(ramp_color, 1.35 if active else 1.0))
        painter.drawLine(start, end)
        painter.restore()

    def _draw_fade_triangle(
        self,
        painter: QPainter,
        x: float,
        y: float,
        direction: str,
        hover: bool = False
    ) -> None:
        """
        Draw fade handle triangle (Phase 1.2).

        Args:
            painter: QPainter instance
            x: X position of triangle corner
            y: Y position of triangle corner
            direction: 'down' for the compact top-mounted marker (▼)
            hover: Whether handle is being hovered
        """
        size = float(FADE_HANDLE_SIZE)
        color = FADE_HANDLE_HOVER_COLOR if hover else FADE_HANDLE_COLOR

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        def make_triangle(offset_x: float, offset_y: float) -> QPolygonF:
            if direction == "down":
                return QPolygonF([
                    QPointF(x + offset_x, y + offset_y),
                    QPointF(x + size + offset_x, y + offset_y),
                    QPointF(x + size / 2.0 + offset_x, y + size + offset_y),
                ])
            return QPolygonF([
                QPointF(x + offset_x, y + offset_y),
                QPointF(x + offset_x, y + size + offset_y),
                QPointF(x - size + offset_x, y + size / 2.0 + offset_y),
            ])

        # The shadow is intentionally a separate, low-contrast layer.  It
        # gives the small top-mounted tag enough separation from bright
        # thumbnails without turning it into a heavy white badge.
        painter.setBrush(QBrush(FADE_HANDLE_SHADOW))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(make_triangle(0.8, 1.0))

        painter.setBrush(QBrush(color))
        painter.setPen(QPen(
            FADE_HANDLE_OUTLINE,
            0.9,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        ))
        painter.drawPolygon(make_triangle(0.0, 0.0))

        # A short inner glint keeps the inactive handle legible at low zoom,
        # while remaining much quieter than the previous solid-white triangle.
        highlight = QColor(255, 255, 255, 105 if not hover else 150)
        painter.setPen(QPen(highlight, 0.7, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        if direction == "down":
            painter.drawLine(QPointF(x + 1.5, y + 1.4),
                             QPointF(x + size - 1.5, y + 1.4))
        elif direction == "right":
            painter.drawLine(QPointF(x + 1.4, y + 1.5),
                             QPointF(x + 1.4, y + size - 1.5))
        else:
            painter.drawLine(QPointF(x - 1.4, y + 1.5),
                             QPointF(x - 1.4, y + size - 1.5))
        painter.restore()

    def _draw_trim_handles(self, painter: QPainter, handle_size: int) -> None:
        """
        Draw trim handles based on active tool and hover state.

        参考 kdenlive: 手柄颜色和样式随工具不同
        - Ripple: 橙色-棕色，带波纹指示器
        - Roll: 蓝色，带双向箭头
        - Trim/Select: 黄色/橙色高亮

        Args:
            painter: QPainter instance
            handle_size: Dynamic handle size based on zoom
        """
        # Ripple trim handles (P4.2): distinct visual indicator
        if self._active_tool == EditTool.RIPPLE:
            if self._hover_edge == "ripple_left":
                # Ripple left handle - orange with ripple indicator
                ripple_handle_color = QColor(180, 100, 30)
                painter.setBrush(QBrush(ripple_handle_color))
                painter.setPen(QPen(Qt.PenStyle.NoPen))
                painter.drawRect(QRectF(0, 0, handle_size, self._track_height))
                # Draw ripple indicator (small wave symbol)
                painter.setPen(QPen(QColor(255, 255, 255), 2))
                y_mid = int(self._track_height / 2)
                painter.drawLine(2, y_mid, int(handle_size / 2), y_mid - 4)
                painter.drawLine(int(handle_size / 2), y_mid - 4, handle_size - 2, y_mid)
            if self._hover_edge == "ripple_right":
                # Ripple right handle
                ripple_handle_color = QColor(180, 100, 30)
                painter.setBrush(QBrush(ripple_handle_color))
                painter.setPen(QPen(Qt.PenStyle.NoPen))
                painter.drawRect(QRectF(self._width - handle_size, 0, handle_size, self._track_height))
                # Draw ripple indicator
                painter.setPen(QPen(QColor(255, 255, 255), 2))
                y_mid = int(self._track_height / 2)
                painter.drawLine(int(self._width - handle_size + 2), y_mid,
                                int(self._width - handle_size / 2), y_mid - 4)
                painter.drawLine(int(self._width - handle_size / 2), y_mid - 4,
                                int(self._width - 2), y_mid)

        # Standard trim handles (for TRIM tool or SELECT tool).  A selected
        # clip gets quiet corner markers; a filled edge is only shown while
        # the pointer is actually over that trim target.
        elif self._active_tool in (EditTool.TRIM, EditTool.SELECT):
            if self._hover_edge == "left":
                painter.setBrush(QBrush(TRIM_IN_HOVER_COLOR))
                painter.setPen(QPen(Qt.PenStyle.NoPen))
                painter.drawRect(QRectF(0, 0, handle_size, self._track_height))
                self._draw_trim_edge_glyph(painter, handle_size, "left")
            elif self._hover_edge == "right":
                painter.setBrush(QBrush(TRIM_OUT_HOVER_COLOR))
                painter.setPen(QPen(Qt.PenStyle.NoPen))
                painter.drawRect(QRectF(self._width - handle_size, 0,
                                        handle_size, self._track_height))
                self._draw_trim_edge_glyph(painter, handle_size, "right")
            elif self._is_selected:
                self._draw_selected_trim_marker(painter, "left")
                self._draw_selected_trim_marker(painter, "right")

    def _draw_selected_trim_marker(self, painter: QPainter, edge: str) -> None:
        """Draw a restrained corner marker for a selected clip endpoint."""
        if self._track_height <= 4.0 or self._width <= 2.0:
            return

        x = 0.8 if edge == "left" else self._width - 0.8
        direction = 1.0 if edge == "left" else -1.0
        marker_length = min(11.0, max(6.0, self._track_height * 0.2))
        top = 2.0
        bottom = self._track_height - 2.0

        painter.save()
        painter.setPen(QPen(
            SELECTED_TRIM_MARKER_COLOR,
            1.35,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        ))
        painter.drawLine(QPointF(x, top), QPointF(x, top + marker_length))
        painter.drawLine(QPointF(x, bottom - marker_length), QPointF(x, bottom))
        painter.drawLine(QPointF(x, top), QPointF(x + direction * 4.5, top))
        painter.drawLine(QPointF(x, bottom), QPointF(x + direction * 4.5, bottom))

        # A very soft highlight on the inside edge makes the bracket survive
        # over pale frames without adding back the old yellow bar.
        painter.setPen(QPen(
            SELECTED_TRIM_MARKER_HIGHLIGHT,
            0.65,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
        ))
        inner_x = x + direction * 1.4
        painter.drawLine(QPointF(inner_x, top + 1.0),
                         QPointF(inner_x, top + marker_length - 1.0))
        painter.restore()

    def _draw_trim_edge_glyph(
        self,
        painter: QPainter,
        handle_size: int,
        edge: str,
    ) -> None:
        """Draw a compact directional cue inside the active trim edge."""
        x = handle_size / 2 if edge == "left" else self._width - handle_size / 2
        direction = -1 if edge == "left" else 1
        y = self._track_height / 2

        painter.save()
        painter.setPen(
            QPen(
                TRIM_EDGE_ACTIVE_COLOR,
                1.5,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawLine(QPointF(x, 4), QPointF(x, self._track_height - 4))
        arrow_x = x + direction * 2
        tip_x = x + direction * 7
        painter.drawLine(QPointF(arrow_x, y), QPointF(tip_x, y))
        painter.drawLine(QPointF(tip_x, y), QPointF(tip_x - direction * 3, y - 3))
        painter.drawLine(QPointF(tip_x, y), QPointF(tip_x - direction * 3, y + 3))
        painter.restore()

    def _draw_source_info(self, painter: QPainter) -> None:
        """Draw source in/out or duration at bottom of clip (P1.4)."""
        info_font = QFont("Arial", 8)
        painter.setFont(info_font)
        painter.setPen(QPen(TEXT_MUTED_COLOR))

        # Format: show source_in -> source_out or just duration
        source_in = self._source_start
        source_out = source_in + self._source_duration

        # Use timeline fps (self._fps) instead of hardcoded 24fps
        fps = self._fps if self._fps > 0 else 24.0

        # Format timecode-style
        def format_frames(frames: int) -> str:
            seconds = frames / fps  # Use actual fps
            if frames < fps:
                return f"{frames}f"
            elif seconds < 60:
                return f"{int(seconds)}s"
            else:
                mins = int(seconds / 60)
                secs = int(seconds % 60)
                return f"{mins}:{secs:02d}"

        # Show "in -> out" if we have source info, else duration
        if self._source_duration > 0:
            info_text = f"{format_frames(source_in)} - {format_frames(source_out)}"
        else:
            info_text = format_frames(self._duration_frames)

        info_rect = QRectF(6, self._track_height - 16, self._width - 12, 12)
        painter.drawText(info_rect, Qt.TextFlag.TextSingleLine, info_text)

    def _draw_disabled_pattern(self, painter: QPainter, rect: QRectF) -> None:
        """Draw diagonal stripe pattern for disabled/muted clips (P1.4).

        Uses a tiled pixmap brush for seamless, continuous diagonal lines
        instead of manual per-line drawing which suffers from integer
        truncation and clipping artifacts.
        """
        # Lazily create and cache the tile pixmap (class-level, created once)
        if ClipItem._disabled_pattern_tile is None:
            tile_size = 10  # Must be > spacing to tile correctly
            spacing = 5     # Stripe spacing within the tile
            tile = QPixmap(tile_size, tile_size)
            tile.fill(Qt.GlobalColor.transparent)

            tile_painter = QPainter(tile)
            tile_painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            stripe_color = QColor(40, 40, 40, 100)
            tile_painter.setPen(QPen(stripe_color, 1))
            # Draw diagonal lines that tile seamlessly (top-left → bottom-right)
            for offset in range(-tile_size, tile_size * 2, spacing):
                tile_painter.drawLine(offset, 0, offset + tile_size, tile_size)
            tile_painter.end()
            ClipItem._disabled_pattern_tile = tile

        # Fill the clip rect using the tiled brush — guarantees continuous stripes
        painter.save()
        painter.setClipRect(rect)
        painter.setBrush(QBrush(ClipItem._disabled_pattern_tile))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(rect)
        painter.restore()

    def _draw_video_thumbnails(
        self,
        painter: QPainter,
        clip_rect: QRectF,
        thumbnail_mode: str = THUMBNAIL_MODE_FILMSTRIP
    ) -> None:
        """
        Draw per-frame thumbnails tiled across clip width.

        参考 Kdenlive ClipThumbs.qml:
        - 每帧对应一个缩略图，不重复
        - 帧精确显示（seek 到帧位置）
        - 按需生成 + 惰性加载

        Phase 1.1 改造:
        - 使用 ClipThumbnailRequest/ClipThumbnailSet
        - 异步加载，未命中时触发请求并显示占位
        - 按显示宽度和轨道高度的密度桶缓存，放大时按需提升质量

        P5-1: Thumbnail LOD modes:
        - filmstrip: Multiple evenly spaced frames (width > 200px)
        - start_end: First and last frame only (100-200px)
        - single: Single first-frame thumbnail (40-100px)
        - none: No thumbnails (handled by paint() before calling this)

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
            thumbnail_mode: LOD mode from _calculate_thumbnail_mode()
        """
        if not self._asset_id or not self._asset_path:
            return

        # P5-1: Minimum width check based on LOD mode
        if thumbnail_mode == THUMBNAIL_MODE_SINGLE and self._width < THUMBNAIL_WIDTH_NONE:
            return
        elif thumbnail_mode != THUMBNAIL_MODE_SINGLE and self._width < MIN_CLIP_WIDTH_FOR_BADGES:
            return

        # Import thumbnail provider components
        from src.shared.media.thumbnail_provider import (
            ClipThumbnailRequest,
            ClipThumbnailCache
        )

        # P5-1: Compute request parameters based on LOD mode
        clip_width_px = int(self._width)
        thumbnail_height = int(clip_rect.height() - 4)  # Leave margin for borders

        # During a live trim, keep the pre-trim cache key stable.  The
        # current source range is still passed to the drawing code below so
        # decoded frames are cropped rather than remapped to the new width.
        thumbnail_source_start = self._source_start
        thumbnail_source_duration = self._source_duration
        if self._trim_preview_active:
            thumbnail_source_start = self._trim_preview_source_start
            thumbnail_source_duration = self._trim_preview_source_duration

        request = ClipThumbnailRequest(
            asset_id=self._asset_id,
            asset_path=self._asset_path,
            clip_width_px=clip_width_px,
            source_start=thumbnail_source_start,
            source_duration=thumbnail_source_duration,
            pixels_per_frame=self._pixels_per_frame,
            thumbnail_height=thumbnail_height,
            display_mode=thumbnail_mode,
        )
        # The request key contains a bounded density bucket and target height,
        # so zooming or resizing a track upgrades quality instead of stretching
        # a low-resolution set extracted for the old geometry.
        request_key = request.get_cache_key()

        request_changed = self._thumbnail_request_key != request_key
        if request_changed:
            self._thumbnail_request_key = request_key
            self._cached_thumbnail_set = None
            self._thumbnail_pending = False

        # Try global cache first (survives across ClipItem instances)
        cache = ClipThumbnailCache()
        cached_set = cache.get(request_key)

        if cached_set is not None and cached_set.num_frames > 0:
            self._cached_thumbnail_set = cached_set
            self._draw_cached_thumbnails(
                painter, clip_rect, cached_set, thumbnail_mode,
                self._source_start, self._source_duration,
            )
            return

        # Also try instance-level cache (fast path after first paint)
        if self._cached_thumbnail_set is not None and self._cached_thumbnail_set.num_frames > 0:
            self._draw_cached_thumbnails(
                painter, clip_rect, self._cached_thumbnail_set, thumbnail_mode,
                self._source_start, self._source_duration,
            )
            return

        # Keep the prior density bucket visible while a zoom-in requests a
        # sharper set.  The exact key remains uncached, so the code below
        # still submits the upgraded request; this is display continuity only.
        compatible_set = cache.get_compatible(request)

        # A scheduler L2 entry is exactly one source frame. It is useful for a
        # narrow single-thumbnail clip, but stretching it over start/end or a
        # filmstrip produces the mosaic seen after zooming. Those modes wait
        # for the density-aware ClipThumbnailSet above instead.
        try:
            view = self.scene().views()[0] if self.scene() and self.scene().views() else None
            if (
                thumbnail_mode == THUMBNAIL_MODE_SINGLE
                and view
                and hasattr(view, 'get_scheduler_cached_thumbnail')
            ):
                from src.features.edit.core.timeline_thumbnail_scheduler import THUMBNAIL_DEFAULT_HEIGHT
                cached_pixmap = view.get_scheduler_cached_thumbnail(
                    self._asset_id, self._source_start, THUMBNAIL_DEFAULT_HEIGHT
                )
                if cached_pixmap is not None:
                    self._draw_single_cached_thumbnail(painter, clip_rect, cached_pixmap, thumbnail_mode)
                    return
        except (IndexError, RuntimeError):
            pass

        # Subscribe before inspecting the shared pending cache.  Several
        # adjacent clips can need the same density-aware thumbnail set; every
        # waiting ClipItem must receive the eventual completion signal, not
        # only the item which started the extraction.
        provider = self._thumbnail_provider()

        # Playback, scrubbing, and zoom transitions intentionally pause the
        # provider.  A request made in this interval is not queued, so keeping
        # a local pending flag would leave this clip permanently blank after
        # the provider resumes.  TimelineView repaints visible clips on
        # resume, which will submit the request then.
        if provider.is_thumbnail_work_suspended:
            self._thumbnail_pending = False
            if compatible_set is not None:
                self._draw_cached_thumbnails(
                    painter, clip_rect, compatible_set, thumbnail_mode,
                    self._source_start, self._source_duration,
                )
            else:
                self._draw_thumbnail_placeholder(painter, clip_rect, state="loading")
            return

        # The shared cache is the source of truth for an in-flight request.
        # A local pending flag without a matching cache entry is stale (for
        # example, after a playback cancellation), so clear it and retry.
        if cache.is_pending(request_key):
            self._thumbnail_pending = True
            if compatible_set is not None:
                self._draw_cached_thumbnails(
                    painter, clip_rect, compatible_set, thumbnail_mode,
                    self._source_start, self._source_duration,
                )
            else:
                self._draw_thumbnail_placeholder(painter, clip_rect, state="loading")
            return

        # No cache and not pending — trigger async request.  Do not enqueue a
        # request for every mouse pixel during trim; the committed update will
        # request the exact post-trim range once.
        if self._trim_preview_active:
            if compatible_set is not None:
                self._draw_cached_thumbnails(
                    painter, clip_rect, compatible_set, thumbnail_mode,
                    self._source_start, self._source_duration,
                )
            else:
                self._draw_thumbnail_placeholder(painter, clip_rect, state="loading")
            return

        self._thumbnail_pending = True
        self._request_clip_thumbnails_async(request, provider)
        if compatible_set is not None:
            self._draw_cached_thumbnails(
                painter, clip_rect, compatible_set, thumbnail_mode,
                self._source_start, self._source_duration,
            )
        else:
            self._draw_thumbnail_placeholder(painter, clip_rect, state="loading")

    def _draw_single_cached_thumbnail(
        self,
        painter: QPainter,
        clip_rect: QRectF,
        pixmap: QPixmap,
        thumbnail_mode: str
    ) -> None:
        """
        P5-4: Draw a single cached thumbnail pixmap.

        Used when the class-level cache has a pre-rendered thumbnail for
        the current clip state.

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
            pixmap: Cached QPixmap to draw
            thumbnail_mode: Current LOD mode
        """
        inner_rect = QRectF(2, 2, clip_rect.width() - 4, clip_rect.height() - 4)
        painter.save()
        painter.setClipRect(inner_rect)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        # Keep the source aspect ratio.  The scheduler cache is a single
        # frame, but it must not be stretched to the clip's arbitrary aspect.
        self._draw_single_thumbnail_at(painter, pixmap, inner_rect, inner_rect)
        painter.restore()

    def _draw_cached_thumbnails(
        self,
        painter: QPainter,
        clip_rect: QRectF,
        thumbnail_set: Any,
        thumbnail_mode: str = THUMBNAIL_MODE_FILMSTRIP,
        visible_source_start: Optional[int] = None,
        visible_source_duration: Optional[int] = None,
    ) -> None:
        """
        Draw cached clip thumbnails at correct positions with LOD mode.

        P5-1: Supports multiple rendering modes:
        - filmstrip: All frames evenly spaced across clip width
        - start_end: Only first and last frame, stretched to half-width each
        - single: Only first frame, stretched to full clip width

        Gemini suggestion: Use KeepAspectRatioByExpanding to prevent stretching,
        then clip excess with setClipRect.

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
            thumbnail_set: ClipThumbnailSet with frame-indexed thumbnails
            thumbnail_mode: LOD mode from _calculate_thumbnail_mode()
        """
        # Inner clip area (leave margin for borders)
        inner_rect = QRectF(2, 2, clip_rect.width() - 4, clip_rect.height() - 4)

        painter.save()
        painter.setClipRect(inner_rect)

        visible_start = (
            self._source_start
            if visible_source_start is None
            else max(0, int(visible_source_start))
        )
        visible_duration = (
            self._source_duration
            if visible_source_duration is None
            else max(1, int(visible_source_duration))
        )
        visible_end = visible_start + visible_duration
        all_valid_frames = [
            (frame_idx, pixmap)
            for frame_idx, pixmap in thumbnail_set.frames
            if not pixmap.isNull()
        ]
        # A live trim can reuse a set from the old range.  Filter by frame
        # index so the removed material is not compressed into the new clip.
        # If the user extends beyond the decoded range, keep the nearest
        # available frames until the committed request finishes.
        valid_frames_for_display = [
            (frame_idx, pixmap)
            for frame_idx, pixmap in all_valid_frames
            if visible_start <= frame_idx < visible_end
        ] or all_valid_frames

        # P5-1: Select frames based on LOD mode
        if thumbnail_mode == THUMBNAIL_MODE_SINGLE:
            # Single mode: only draw the first frame, stretched to full width
            frames_to_draw = []
            if valid_frames_for_display:
                frames_to_draw.append((0, valid_frames_for_display[0][1]))
            if not frames_to_draw:
                painter.restore()
                return
            # Draw single frame stretched to full width
            for _, pixmap in frames_to_draw:
                self._draw_single_thumbnail_at(
                    painter, pixmap, inner_rect, inner_rect
                )

        elif thumbnail_mode == THUMBNAIL_MODE_START_END:
            # Start/end mode: first and last frame, each taking half width
            valid_frames = [
                (i, (frame_idx, pixmap))
                for i, (frame_idx, pixmap) in enumerate(valid_frames_for_display)
            ]
            if not valid_frames:
                painter.restore()
                return

            half_width = inner_rect.width() / 2
            left_rect = QRectF(
                inner_rect.left(), inner_rect.top(),
                half_width, inner_rect.height()
            )
            right_rect = QRectF(
                inner_rect.left() + half_width, inner_rect.top(),
                half_width, inner_rect.height()
            )

            # Draw first frame on left half
            first_pixmap = valid_frames[0][1][1]
            self._draw_single_thumbnail_at(
                painter, first_pixmap, left_rect, inner_rect
            )

            # Draw last frame on right half
            if len(valid_frames) > 1:
                last_pixmap = valid_frames[-1][1][1]
            else:
                last_pixmap = first_pixmap  # Fallback if only one frame
            self._draw_single_thumbnail_at(
                painter, last_pixmap, right_rect, inner_rect
            )

            # P5-1: Draw subtle divider line between start/end thumbnails
            divider_x = inner_rect.left() + half_width
            painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
            painter.drawLine(
                int(divider_x), int(inner_rect.top()),
                int(divider_x), int(inner_rect.bottom())
            )

        else:
            # Filmstrip mode: keep each decoded frame at its source-time
            # position.  This is the important distinction between trimming
            # and scaling: when the clip is extended beyond the held cache,
            # the undecoded tail stays empty instead of stretching the old
            # last frame across it.
            cache_source_start = (
                self._trim_preview_source_start
                if self._trim_preview_active
                else visible_start
            )
            cache_source_end = cache_source_start + (
                self._trim_preview_source_duration
                if self._trim_preview_active
                else visible_duration
            )
            draw_start = max(visible_start, cache_source_start)
            draw_end = min(visible_end, cache_source_end)
            valid_frames = [
                (frame_idx, pixmap)
                for frame_idx, pixmap in valid_frames_for_display
                if draw_start <= frame_idx < draw_end
            ]
            if not valid_frames:
                painter.restore()
                return

            # Use source-time boundaries to keep neighbouring cells aligned
            # with the visible in/out range.  The first/last cell may be
            # clipped by the live trim range, exactly like an NLE filmstrip.
            for i, (frame_idx, pixmap) in enumerate(valid_frames):
                left_frame = draw_start if i == 0 else frame_idx
                right_frame = (
                    draw_end
                    if i == len(valid_frames) - 1
                    else valid_frames[i + 1][0]
                )
                left = round(
                    inner_rect.left()
                    + (left_frame - visible_start) / visible_duration * inner_rect.width()
                )
                right = round(
                    inner_rect.left()
                    + (right_frame - visible_start) / visible_duration * inner_rect.width()
                )
                right = max(left + 1, right)
                target_cell = QRectF(
                    float(left), inner_rect.top(), float(right - left), inner_rect.height())
                self._draw_single_thumbnail_at(
                    painter, pixmap, target_cell, inner_rect
                )

        painter.restore()

    def _draw_single_thumbnail_at(
        self,
        painter: QPainter,
        pixmap: QPixmap,
        target_cell: QRectF,
        clip_rect: QRectF
    ) -> None:
        """
        P5-1: Draw a single thumbnail at a target position with aspect ratio preserved.

        Uses KeepAspectRatioByExpanding approach: scales the pixmap to cover
        the target cell area while maintaining aspect ratio, then clips excess.

        P0-E: Prevents >1.25x upscaling to avoid mosaic artifacts. If the cached
        pixmap is too small, shows a loading placeholder instead.

        Args:
            painter: QPainter instance (must already have clip set)
            pixmap: QPixmap to draw
            target_cell: The target rectangle for this thumbnail
            clip_rect: The clipping rectangle (inner_rect)
        """
        thumb_w = target_cell.width()
        thumb_h = target_cell.height()

        # P0-E: Check if upscaling would exceed 1.25x (mosaic prevention)
        max_scale = 1.25
        scale_x = thumb_w / pixmap.width() if pixmap.width() > 0 else 999
        scale_y = thumb_h / pixmap.height() if pixmap.height() > 0 else 999
        if max(scale_x, scale_y) > max_scale:
            # Cached pixmap is too low-res; draw subtle placeholder instead
            placeholder_color = QColor(40, 45, 55, 80)
            painter.fillRect(target_cell, placeholder_color)
            return

        # Calculate aspect-ratio preserving rect
        pixmap_aspect = pixmap.width() / pixmap.height() if pixmap.height() > 0 else 1.0
        target_aspect = thumb_w / thumb_h if thumb_h > 0 else 1.0

        if pixmap_aspect > target_aspect:
            scaled_height = thumb_h
            scaled_width = thumb_h * pixmap_aspect
        else:
            scaled_width = thumb_w
            scaled_height = thumb_w / pixmap_aspect

        # Center the scaled pixmap in the target cell
        scaled_x = target_cell.left() + (thumb_w - scaled_width) / 2
        scaled_y = target_cell.top() + (thumb_h - scaled_height) / 2

        target_rect = QRectF(scaled_x, scaled_y, scaled_width, scaled_height)
        source_rect = QRectF(0, 0, pixmap.width(), pixmap.height())
        painter.drawPixmap(target_rect, pixmap, source_rect)

    def _draw_loading_thumbnails(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw loading placeholder when thumbnails are being extracted.

        参考 Kdenlive: loading state indicator
        """
        inner_rect = QRectF(2, 2, clip_rect.width() - 4, clip_rect.height() - 4)

        painter.save()
        painter.setClipRect(inner_rect)

        # Semi-transparent loading background
        loading_color = QColor(60, 60, 80, 100)
        painter.fillRect(inner_rect, loading_color)

        # Loading stripes pattern (visual feedback)
        stripe_width = 8
        stripe_color = QColor(80, 80, 100, 150)

        x = 0.0
        while x < inner_rect.width():
            stripe_rect = QRectF(
                inner_rect.left() + x,
                inner_rect.top(),
                stripe_width - 1,
                inner_rect.height()
            )
            painter.fillRect(stripe_rect, stripe_color)
            x += stripe_width * 2

        painter.restore()

    def _draw_thumbnail_placeholder(
        self,
        painter: QPainter,
        clip_rect: QRectF,
        state: str = "loading"
    ) -> None:
        """
        P5-5: Draw visual placeholder when thumbnails are loading or failed to load.

        Provides clear visual feedback to the user about the thumbnail state:
        - "loading": Dark background with animated-looking dots pattern
        - "failed": Red-tinted background with X icon indicating error

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
            state: Either "loading" or "failed"
        """
        inner_rect = QRectF(2, 2, clip_rect.width() - 4, clip_rect.height() - 4)

        painter.save()
        painter.setClipRect(inner_rect)

        if state == "loading":
            # Use quiet filmstrip cells instead of a large three-dot loader.
            # They preserve the visual rhythm of a populated timeline while
            # thumbnail decoding completes, without competing with the clip
            # name or the red playhead.
            painter.fillRect(inner_rect, QColor(29, 45, 56, 160))
            cell_width = max(42.0, inner_rect.height() * FILMSTRIP_CELL_ASPECT)
            cell_color = QColor(83, 111, 125, 52)
            divider_color = QColor(8, 12, 16, 100)
            x = inner_rect.left()
            while x < inner_rect.right():
                cell_rect = QRectF(x + 1.0, inner_rect.top() + 1.0, cell_width - 2.0, inner_rect.height() - 2.0)
                painter.fillRect(cell_rect, cell_color)
                painter.setPen(QPen(divider_color, 1))
                painter.drawLine(
                    QPointF(min(x + cell_width, inner_rect.right()), inner_rect.top()),
                    QPointF(min(x + cell_width, inner_rect.right()), inner_rect.bottom()),
                )
                x += cell_width

        elif state == "failed":
            # Red-tinted failure background
            painter.fillRect(inner_rect, THUMBNAIL_FAILED_COLOR)

            # Draw X icon centered in the clip
            center_x = inner_rect.center().x()
            center_y = inner_rect.center().y()
            icon_size = min(16.0, inner_rect.width() / 3, inner_rect.height() / 3)

            painter.setPen(QPen(THUMBNAIL_FAILED_ICON_COLOR, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Draw X
            half = icon_size / 2
            painter.drawLine(
                int(center_x - half), int(center_y - half),
                int(center_x + half), int(center_y + half)
            )
            painter.drawLine(
                int(center_x + half), int(center_y - half),
                int(center_x - half), int(center_y + half)
            )

        painter.restore()

    def _thumbnail_provider(self) -> Any:
        """Return the shared provider and subscribe this item once."""
        from src.shared.media.thumbnail_provider import ThumbnailProvider

        provider = ThumbnailProvider.instance()
        if not self._thumbnail_signal_connected:
            provider.clip_thumbnail_set_ready.connect(self._on_clip_thumbnails_ready)
            self._thumbnail_signal_connected = True
            self._thumbnail_provider_ref = provider
        return provider

    def _request_clip_thumbnails_async(self, request: Any, provider: Optional[Any] = None) -> None:
        """
        Request clip thumbnails asynchronously.

        使用 ThumbnailProvider 的异步提取服务。
        结果通过 clip_thumbnail_set_ready 信号返回。

        Args:
            request: ClipThumbnailRequest with frame parameters
        """
        if provider is None:
            provider = self._thumbnail_provider()

        # Request thumbnails
        provider.request_clip_thumbnails(request)

        # Track last pixels_per_frame for change detection
        self._last_thumbnail_ppf = self._pixels_per_frame

    def _on_clip_thumbnails_ready(self, request_key: str, thumbnail_set: Any) -> None:
        """
        Handle clip thumbnails ready signal.

        Callback from ThumbnailProvider when thumbnails are extracted.
        Triggers repaint if the request matches current clip state.

        Args:
            request_key: Cache key from ClipThumbnailRequest
            thumbnail_set: ClipThumbnailSet with extracted thumbnails
        """
        if self._disposed:
            return
        # Only update if this matches our current request
        if request_key == self._thumbnail_request_key:
            self._cached_thumbnail_set = thumbnail_set
            self._thumbnail_pending = False  # UI-08: Clear pending flag
            # Trigger repaint
            self.update()

    def _draw_waveform(
        self,
        painter: QPainter,
        exposed_rect: Optional[QRectF] = None,
    ) -> None:
        """
        Draw waveform for audio clips with caching.

        参考 kdenlive ClipAudioThumbs.qml:
        - 按像素聚合采样点，避免每帧重绘
        - 使用 QPainterPath 缓存
        - 支持分离声道显示（立体声双通道）

        Phase 1.3: Stereo dual-channel rendering
        - 检测波形数据通道数
        - 单声道：保持现有逻辑
        - 立体声：上半部左声道(L)，下半部右声道(R)，中间分割线

        P5-3: Improved time mapping
        - Waveform uses source_start (in-point) for correct segment display
        - Proper sample offset calculation via _waveform_start_offset_samples()
        - When clip is trimmed, only the visible audio portion is shown
        """
        # Waveform area bounds
        # Keep the waveform visually dominant; the shared bottom information
        # strip starts at y=track_height-19 and must not cover its peaks.
        waveform_top = 6
        waveform_bottom = self._track_height - 22
        waveform_height = waveform_bottom - waveform_top
        if waveform_height <= 1:
            return

        # Keep only a hairline inset at audio clip edges. The previous 4px
        # inset on both sides made adjacent edits look acoustically severed
        # even when they came from one continuous source.
        full_x_start = 0.75
        full_x_end = max(full_x_start, self._width - 0.75)
        # A long clip can be tens of thousands of pixels wide. QGraphicsView
        # already supplies the exposed local rect, so only build the envelope
        # visible on screen instead of a path for the entire clip every zoom.
        if exposed_rect is not None and not exposed_rect.isEmpty():
            x_start = max(full_x_start, float(exposed_rect.left()))
            x_end = min(full_x_end, float(exposed_rect.right()))
        else:
            x_start, x_end = full_x_start, full_x_end
        display_width = x_end - x_start

        if display_width <= 1.0:
            return

        full_width = max(1.0, full_x_end - full_x_start)
        visible_start_fraction = max(0.0, min(1.0, (x_start - full_x_start) / full_width))
        visible_end_fraction = max(visible_start_fraction, min(
            1.0, (x_end - full_x_start) / full_width))
        visible_source_start = self._source_start + int(round(
            self._source_duration * visible_start_fraction))
        visible_source_duration = max(1, int(round(
            self._source_duration * (visible_end_fraction - visible_start_fraction))))

        # Check for real waveform data
        waveform_data = None
        if self._waveform_asset_id:
            waveform_data = get_cached_waveform(self._waveform_asset_id)
        # 效果器处理后的 per-clip 波形优先于 asset 原始波形。
        if self._effect_waveform is not None:
            waveform_data = self._effect_waveform
        elif waveform_data is not None and waveform_data.is_complete:
            # 原始波形可用后才有"效果后波形"的渲染意义；该调用幂等。
            self._maybe_request_effect_waveform()

        # Determine if stereo rendering is needed. Mono mixdown is the
        # default even for stereo source media; the explicit per-clip toggle
        # opts into the more detailed two-path rendering.
        is_stereo = False
        if waveform_data and waveform_data.is_complete:
            is_stereo = (
                self._waveform_show_channels
                and waveform_data.channels >= 2
            )

        # Include source range, geometry and track height. QPainterPath stores
        # absolute Y coordinates; omitting height made waveform shapes stay at
        # their old size after the user resized an audio track.
        sample_offset = int(visible_source_start * (self._fps if self._fps > 0 else 30.0))
        cache_key = (
            f"{visible_source_start}_{visible_source_duration}_{x_start:.1f}_{display_width:.1f}"
            f"_h_{self._track_height}_fps_{self._fps:.4f}_stereo_{is_stereo}_soff_{sample_offset}"
            f"_fx_{self._effect_waveform_fp or ''}"
        )

        # P5-4: Check class-level shared cache
        shared_cache_key = f"{self._waveform_asset_id}_{cache_key}"
        cached_paths = ClipItem._get_shared_waveform_paths(shared_cache_key)
        if cached_paths is not None:
            if is_stereo:
                self._draw_cached_stereo_waveform(
                    painter, cached_paths,
                    x_start, x_end, waveform_top, waveform_height
                )
            else:
                y_center = self._track_height / 2
                self._draw_cached_waveform(
                    painter, cached_paths[0] if isinstance(cached_paths, list) else cached_paths,
                    x_start, x_end, y_center, waveform_height
                )
            return

        # Check for cached waveform paths (per-instance cache)
        if self._waveform_path_cache_key == cache_key and self._waveform_path_cache:
            # Use cached paths for fast redraw
            if is_stereo:
                self._draw_cached_stereo_waveform(
                    painter, self._waveform_path_cache,
                    x_start, x_end, waveform_top, waveform_height
                )
            else:
                y_center = self._track_height / 2
                self._draw_cached_waveform(
                    painter, self._waveform_path_cache[0] if isinstance(self._waveform_path_cache, list) else self._waveform_path_cache,
                    x_start, x_end, y_center, waveform_height
                )
            return

        if waveform_data and waveform_data.is_complete:
            # Build and cache real waveform paths
            if is_stereo:
                # Stereo: build paths for left and right channels
                paths = self._build_stereo_waveform_paths(
                    waveform_data, x_start, x_end, waveform_top, waveform_height,
                    visible_source_start, visible_source_duration,
                )
                if paths:
                    self._waveform_path_cache = paths
                    self._waveform_path_cache_key = cache_key
                    # P5-4: Store in shared cache
                    ClipItem._cache_shared_waveform_paths(shared_cache_key, paths)
                    self._draw_cached_stereo_waveform(
                        painter, paths, x_start, x_end, waveform_top, waveform_height
                    )
            else:
                # Mono: single path
                y_center = self._track_height / 2
                path = self._build_real_waveform_path(
                    waveform_data, x_start, x_end, y_center, waveform_height,
                    channel=None,
                    source_start=visible_source_start,
                    source_duration=visible_source_duration,
                )
                if path:
                    self._waveform_path_cache = [path]  # Store as list for consistency
                    self._waveform_path_cache_key = cache_key
                    # P5-4: Store in shared cache
                    ClipItem._cache_shared_waveform_paths(shared_cache_key, [path])
                    self._draw_cached_waveform(painter, path, x_start, x_end, y_center, waveform_height)
        elif self._waveform_asset_id and self._asset_path and not self._waveform_requested:
            # Request waveform async; show placeholder if we already know it failed
            request_sent = self._request_waveform_async()
            y_center = self._track_height / 2
            if request_sent:
                self._draw_loading_waveform(painter, x_start, x_end, y_center, waveform_height)
            elif getattr(self, '_waveform_failed', False):
                # P5-5: Show failed state instead of generic placeholder
                self._draw_placeholder_waveform(painter, x_start, x_end, y_center, waveform_height, failed=True)
            else:
                self._draw_placeholder_waveform(painter, x_start, x_end, y_center, waveform_height)
        else:
            # Placeholder waveform
            y_center = self._track_height / 2
            failed = getattr(self, '_waveform_failed', False)
            self._draw_placeholder_waveform(painter, x_start, x_end, y_center, waveform_height, failed=failed)

    def _build_real_waveform_path(
        self,
        waveform: WaveformData,
        x_start: float,
        x_end: float,
        y_center: float,
        waveform_height: float,
        channel: Optional[int] = None,
        source_start: Optional[int] = None,
        source_duration: Optional[int] = None,
    ) -> Optional[QPainterPath]:
        """
        Build waveform path from real data (参考 kdenlive timelinewaveform.h).

        按像素聚合采样点，取最大最小值绘制波形条。

        Args:
            waveform: WaveformData from cache
            x_start: Left edge x coordinate
            x_end: Right edge x coordinate
            y_center: Y center coordinate
            waveform_height: Height of waveform area
            channel: Optional channel index (0=left, 1=right for stereo).
                     If None, returns mixed (all channels combined).

        Returns:
            QPainterPath for cached waveform, or None if no data
        """
        # Calculate time range
        source_fps = self._fps if self._fps > 0 else 30.0
        range_start = self._source_start if source_start is None else source_start
        range_duration = self._source_duration if source_duration is None else source_duration
        if range_duration <= 0:
            return None
        start_time = range_start / source_fps
        end_time = (range_start + range_duration) / source_fps

        display_width = int(x_end - x_start)
        peaks_min, peaks_max = waveform.get_samples_for_range(start_time, end_time, display_width, channel)

        if len(peaks_max) == 0:
            return None

        # Build a filled envelope that reaches both clip edges. Closing via
        # the center line created a visible zero-amplitude notch at every
        # edit point; using the first/last peak instead keeps neighboring
        # clips visually connected like Resolve's timeline waveform.
        waveform_path = QPainterPath()
        first_upper = waveform_height * float(peaks_max[0]) / 2.0
        last_lower = waveform_height * abs(float(peaks_min[-1])) / 2.0
        waveform_path.moveTo(x_start, y_center - first_upper)

        # Upper envelope
        for i, peak in enumerate(peaks_max):
            x = x_start + (x_end - x_start) * i / len(peaks_max)
            y_offset = waveform_height * peak / 2
            waveform_path.lineTo(x, y_center - y_offset)

        waveform_path.lineTo(x_end, y_center + last_lower)

        # Lower envelope
        for i in range(len(peaks_min) - 1, -1, -1):
            x = x_start + (x_end - x_start) * i / len(peaks_min)
            peak = peaks_min[i]
            y_offset = waveform_height * abs(peak) / 2
            waveform_path.lineTo(x, y_center + y_offset)

        waveform_path.closeSubpath()
        return waveform_path

    def _build_stereo_waveform_paths(
        self,
        waveform: WaveformData,
        x_start: float,
        x_end: float,
        waveform_top: float,
        waveform_height: float,
        source_start: Optional[int] = None,
        source_duration: Optional[int] = None,
    ) -> Optional[list[QPainterPath]]:
        """
        Build stereo waveform paths for left and right channels.

        参考 Kdenlive timelinewaveform.cpp:
        - 每个通道占用一半高度
        - 上半部分：左声道 (channel=0)
        - 下半部分：右声道 (channel=1)

        Args:
            waveform: WaveformData from cache (must have channels >= 2)
            x_start: Left edge x coordinate
            x_end: Right edge x coordinate
            waveform_top: Top edge y coordinate
            waveform_height: Total waveform height

        Returns:
            List of two QPainterPaths [left_path, right_path], or None if no data
        """
        if waveform.channels < 2:
            return None

        # Each channel gets half the height
        channel_height = waveform_height / 2

        # Left channel (channel=0) - top half
        # Center of top half is at waveform_top + channel_height / 2
        left_y_center = waveform_top + channel_height / 2
        left_path = self._build_real_waveform_path(
            waveform, x_start, x_end, left_y_center, channel_height,
            channel=0,
            source_start=source_start,
            source_duration=source_duration,
        )

        # Right channel (channel=1) - bottom half
        # Center of bottom half is at waveform_top + channel_height + channel_height / 2
        # = waveform_top + 3 * channel_height / 2
        right_y_center = waveform_top + channel_height + channel_height / 2
        right_path = self._build_real_waveform_path(
            waveform, x_start, x_end, right_y_center, channel_height,
            channel=1,
            source_start=source_start,
            source_duration=source_duration,
        )

        if left_path and right_path:
            return [left_path, right_path]
        return None

    def _draw_cached_waveform(
        self,
        painter: QPainter,
        path: QPainterPath,
        x_start: float,
        x_end: float,
        y_center: float,
        waveform_height: float
    ) -> None:
        """
        Draw cached waveform path with proper colors.

        Args:
            painter: QPainter instance
            path: Cached QPainterPath
            x_start: Left edge x coordinate
            x_end: Right edge x coordinate
            y_center: Y center coordinate
            waveform_height: Height of waveform area
        """
        # Keep the waveform brighter and calmer than the clip body. The
        # custom palette preserves hue without allowing a saturated label to
        # make the envelope disappear into its background.
        label_palette = _derive_label_palette(self._label_color)
        if label_palette is not None:
            waveform_fill_color = label_palette[3]
        else:
            waveform_fill_color = WAVEFORM_COLOR

        # Track-volume preview: scale the cached path around its center line
        # instead of rebuilding it. A cosmetic stroke keeps the outline width
        # constant under the vertical scale.
        gain = self._waveform_gain
        if abs(gain - 1.0) > 1e-3:
            painter.save()
            painter.setClipRect(
                QRectF(x_start - 1.0, y_center - waveform_height / 2 - 0.5,
                       (x_end - x_start) + 2.0, waveform_height + 1.0),
                Qt.ClipOperation.IntersectClip)
            painter.translate(0.0, y_center)
            painter.scale(1.0, gain)
            painter.translate(0.0, -y_center)
            scaled = True
        else:
            scaled = False

        # DaVinci-style waveform: one clean filled envelope with no outline.
        # The clip body and the waveform fill provide the contrast; adding a
        # dark keyline makes the waveform look embossed and visually noisy.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(waveform_fill_color))
        painter.drawPath(path)

        if scaled:
            painter.restore()

    def _draw_cached_stereo_waveform(
        self,
        painter: QPainter,
        paths: list[QPainterPath],
        x_start: float,
        x_end: float,
        waveform_top: float,
        waveform_height: float
    ) -> None:
        """
        Draw cached stereo waveform paths with distinct colors for each channel.

        参考 Kdenlive timelinewaveform.cpp:
        - 上半部分：左声道 (WAVEFORM_COLOR_STEREO_L)
        - 下半部分：右声道 (WAVEFORM_COLOR_STEREO_R)
        - 中间分割线 (1px BORDER_SUBTLE_COLOR)

        Args:
            painter: QPainter instance
            paths: List of two QPainterPaths [left_path, right_path]
            x_start: Left edge x coordinate
            x_end: Right edge x coordinate
            waveform_top: Top edge y coordinate
            waveform_height: Total waveform height
        """
        if len(paths) != 2:
            return

        left_path = paths[0]
        right_path = paths[1]

        # Channel height (each gets half)
        channel_height = waveform_height / 2

        # Keep stereo channels distinct, but use a pale, low-saturation pair
        # so both channels remain visible on dark or custom-colored clips.
        label_palette = _derive_label_palette(self._label_color)
        if label_palette is not None:
            stereo_l_color = label_palette[3]
            stereo_r_color = QColor(label_palette[3]).lighter(112)
        else:
            stereo_l_color = WAVEFORM_COLOR_STEREO_L
            stereo_r_color = WAVEFORM_COLOR_STEREO_R

        # Track-volume preview scales each channel around its own center line.
        gain = self._waveform_gain
        scaled = abs(gain - 1.0) > 1e-3
        left_y_center = waveform_top + channel_height / 2
        right_y_center = waveform_top + channel_height + channel_height / 2
        if scaled:
            painter.save()
            painter.setClipRect(
                QRectF(x_start - 1.0, waveform_top - 0.5,
                       (x_end - x_start) + 2.0, waveform_height + 1.0),
                Qt.ClipOperation.IntersectClip)

        def _draw_one(channel_path: QPainterPath, y_center: float,
                      fill_color: QColor) -> None:
            if scaled:
                painter.translate(0.0, y_center)
                painter.scale(1.0, gain)
                painter.translate(0.0, -y_center)
            # Stereo follows the same flat-fill treatment as mono; the two
            # channel colors are the only distinction.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(fill_color))
            painter.drawPath(channel_path)
            if scaled:
                # Undo the per-channel vertical scale before the next channel.
                painter.translate(0.0, y_center)
                painter.scale(1.0, 1.0 / gain)
                painter.translate(0.0, -y_center)

        # Left channel (top half)
        _draw_one(left_path, left_y_center, stereo_l_color)

        # Right channel (bottom half)
        _draw_one(right_path, right_y_center, stereo_r_color)

        if scaled:
            painter.restore()

        # Draw center divider line (1px)
        mid_y = int(waveform_top + channel_height)
        painter.setPen(QPen(QColor(220, 235, 228, 72), 1))
        painter.drawLine(int(x_start), mid_y, int(x_end), mid_y)

    def _draw_placeholder_waveform(
        self,
        painter: QPainter,
        x_start: float,
        x_end: float,
        y_center: float,
        waveform_height: float,
        failed: bool = False
    ) -> None:
        """
        Draw placeholder waveform (fallback when no real data).

        P5-5: Added 'failed' parameter to visually distinguish extraction failures.

        Args:
            painter: QPainter instance
            x_start: Left edge x coordinate
            x_end: Right edge x coordinate
            y_center: Y center coordinate
            waveform_height: Height of waveform area
            failed: If True, draw with error styling (red tint)
        """
        # P5-5: Use different color for failed state
        if failed:
            waveform_color = QColor(100, 50, 50, 150)  # Dim red for failed
        elif (label_palette := _derive_label_palette(self._label_color)) is not None:
            waveform_color = label_palette[3]
        else:
            waveform_color = WAVEFORM_COLOR_AUDIO_TRACK

        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(waveform_color))

        # Create pseudo-random waveform shape
        waveform_path = QPainterPath()
        seed_hash = hash(self._clip_id) % 100
        num_points = max(4, int((x_end - x_start) / 4))

        waveform_path.moveTo(x_start, y_center)

        for i in range(num_points):
            x = x_start + (x_end - x_start) * i / num_points
            height_factor = 0.3 + 0.7 * abs((seed_hash + i * 7) % 100 - 50) / 50
            y_offset = waveform_height * height_factor / 2
            waveform_path.lineTo(x, y_center - y_offset)

        waveform_path.lineTo(x_end, y_center)

        for i in range(num_points - 1, -1, -1):
            x = x_start + (x_end - x_start) * i / num_points
            height_factor = 0.3 + 0.7 * abs((seed_hash + i * 7) % 100 - 50) / 50
            y_offset = waveform_height * height_factor / 2
            waveform_path.lineTo(x, y_center + y_offset)

        waveform_path.closeSubpath()
        painter.drawPath(waveform_path)

    def _draw_loading_waveform(
        self,
        painter: QPainter,
        x_start: float,
        x_end: float,
        y_center: float,
        waveform_height: float
    ) -> None:
        """
        Draw loading indicator (waveform being extracted).
        """
        loading_color = WAVEFORM_COLOR_AUDIO_TRACK
        loading_color.setAlpha(150)

        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(loading_color))

        # Loading stripes
        display_width = x_end - x_start
        stripe_width = 8
        num_strips = int(display_width / stripe_width)

        for i in range(num_strips):
            x = x_start + i * stripe_width
            alpha = 100 + (i % 3) * 50
            stripe_color = QColor(loading_color)
            stripe_color.setAlpha(alpha)
            painter.setBrush(QBrush(stripe_color))
            painter.drawRect(int(x), int(y_center - waveform_height / 4), stripe_width - 2, int(waveform_height / 2))

        # Loading text
        painter.setPen(QPen(TEXT_MUTED_COLOR))
        loading_font = QFont("Arial", 8)
        painter.setFont(loading_font)
        text_rect = QRectF(x_start, y_center - 8, display_width, 16)
        painter.drawText(text_rect, Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter, "...")

    def _request_waveform_async(self) -> bool:
        """Request waveform extraction asynchronously.

        Returns:
            True if a new extraction request was dispatched, False if skipped.
        """
        if not self._waveform_asset_id or not self._asset_path:
            return False
        if self._waveform_requested:
            return False

        # Guard against retrying every paint after a confirmed failure.
        # A failure is confirmed when:
        #   1. We previously attempted extraction (_waveform_failed flag)
        #   2. The asset is no longer pending (worker finished)
        #   3. No cached waveform exists (extraction did not succeed)
        if getattr(self, '_waveform_failed', False):
            from src.features.edit.core.audio_waveform import is_waveform_pending
            if (not is_waveform_pending(self._waveform_asset_id)
                    and get_cached_waveform(self._waveform_asset_id) is None):
                return False  # Previously failed, don't retry every paint

        self._waveform_requested = True
        self._waveform_failed = False
        extractor = WaveformExtractor.instance()
        if not self._waveform_signal_connected:
            extractor.waveform_ready.connect(self._on_waveform_ready)
            extractor.extraction_failed.connect(self._on_waveform_extraction_failed)
            self._waveform_signal_connected = True
            self._waveform_extractor_ref = extractor

        request_waveform_async(self._waveform_asset_id, self._asset_path)
        logger.debug(f"Requested waveform for asset {self._waveform_asset_id}")
        return True

    def _on_waveform_ready(self, asset_id: str) -> None:
        """Handle waveform ready signal - clear cache and repaint."""
        if self._disposed:
            return
        if asset_id == self._waveform_asset_id:
            self._waveform_path_cache = None
            self._waveform_path_cache_key = None
            self._waveform_pixmap = None
            # A cache entry may later be evicted. Resetting this flag lets the
            # item request extraction again instead of showing a permanent
            # placeholder after the WaveformCache LRU discards its data.
            self._waveform_requested = False
            self._waveform_failed = False
            # P5-4: Invalidate shared cache entries for this asset
            self.invalidate_cache(clip_id=self._clip_id, asset_id=self._waveform_asset_id)
            self.update()
            logger.debug(f"Waveform ready for clip {self._clip_id}")

            # If waveform data is still None after ready signal, extraction failed
            waveform_data = get_cached_waveform(self._waveform_asset_id)
            if waveform_data is None:
                self._waveform_requested = False
                self._waveform_failed = True

    def _on_waveform_extraction_failed(self, asset_id: str, error: str) -> None:
        """Handle waveform extraction failure - reset state so placeholder is shown."""
        if self._disposed:
            return
        if asset_id == self._waveform_asset_id:
            self._waveform_requested = False
            self._waveform_failed = True
            self._waveform_path_cache = None
            self._waveform_path_cache_key = None
            self._waveform_pixmap = None
            # P5-4: Invalidate shared cache entries for this clip
            self.invalidate_cache(clip_id=self._clip_id, asset_id=self._waveform_asset_id)
            self.update()
            logger.debug(f"Waveform extraction failed for clip {self._clip_id}: {error}")


    def _draw_linked_indicator(self, painter: QPainter) -> None:
        """Draw a Resolve-style chain icon before the clip name.

        The icon lives inside the bottom information strip instead of the
        thumbnail/waveform area.  Its position matches the ``text_offset``
        reserved by the label renderer above.
        """
        icon_center = QPointF(10.0, float(self._track_height) - 10.0)
        link_color = (
            QColor(245, 245, 245, 230)
            if self._linked_selection_enabled
            else QColor(170, 175, 180, 150)
        )

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(
            link_color,
            1.35,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        ))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Two overlapping rounded links, rotated to form a compact chain.
        painter.translate(icon_center)
        painter.rotate(-42.0)
        painter.drawRoundedRect(QRectF(-7.0, -2.5, 8.0, 5.0), 2.2, 2.2)
        painter.drawRoundedRect(QRectF(-1.0, -2.5, 8.0, 5.0), 2.2, 2.2)

        if not self._linked_selection_enabled:
            # Preserve the broken-link cue without restoring the old badge.
            painter.drawLine(QPointF(-4.5, -4.5), QPointF(4.5, 4.5))

        painter.restore()

    def _draw_linked_group_border(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw linked group border (bottom colored line) for Phase 6.1.

        参考 Kdenlive groupColor: 为同一链接组的 clip 绘制相同颜色的底部边框线。

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
        """
        # Get color index for this linked group
        color_index = self._linked_group_color_index

        # If no color assigned, use a default color (first in palette)
        if color_index < 0 or color_index >= len(LINKED_GROUP_COLORS):
            color_index = 0

        # Get base color from palette
        base_color = LINKED_GROUP_COLORS[color_index]

        # Determine if highlighted (hover on this or linked clip, or
        # linked-to-selection — P2-03 selection diagnostic).
        border_width = LINKED_GROUP_BORDER_WIDTH
        if self._is_group_highlighted or self._linked_to_selection:
            # Use brighter color for highlight
            border_color = base_color.lighter(130)
            border_width = LINKED_GROUP_BORDER_HIGHLIGHT_WIDTH
        else:
            border_color = base_color

        # Draw bottom border line
        painter.save()
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(border_color))

        # Bottom border rectangle (full width of clip)
        border_rect = QRectF(
            clip_rect.left(),
            clip_rect.bottom() - border_width,
            clip_rect.width(),
            border_width
        )
        painter.drawRect(border_rect)
        painter.restore()

    def _draw_locked_indicator(self, painter: QPainter) -> None:
        """Draw a small lock indicator on the clip using design tokens."""
        # Draw lock icon in top-right corner
        lock_x = self._width - 18
        lock_y = 6

        # Use lock color from constants
        painter.setPen(QPen(LOCK_ACTIVE_COLOR, 1))
        painter.setBrush(QBrush(LOCK_ACTIVE_COLOR))

        # Draw simple lock shape (circle + rect)
        painter.drawEllipse(int(lock_x), int(lock_y), 10, 10)
        painter.drawRect(int(lock_x + 2), int(lock_y + 7), 6, 8)

    # ==================== Phase 2: Badge & Indicator Drawing ====================

    def _draw_effects_badge(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw effects badge on clip (Phase 2.1).

        参考 Kdenlive: clip 底部显示效果数量或图标。
        - 位置：clip 底部居中，高度 16px
        - 背景：半透明黑色圆角条 rgba(0,0,0,120)
        - 图标：⚡ 或效果数量文字 "3 effects"
        - 仅在 clip.effects 非空时绘制
        - 最小 clip 宽度 80px 以上才显示

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
        """
        # Check conditions for drawing
        if not self.has_effects():
            return

        # Check minimum width (UI-11: use MIN_CLIP_WIDTH_FOR_BADGES = 80)
        if self._width < MIN_CLIP_WIDTH_FOR_BADGES:
            return

        # Check if details are hidden
        if self._hide_details:
            return

        painter.save()

        # Badge dimensions from design tokens
        badge_h = EDIT_SIZES.get("badge_h", 16)
        badge_radius = EDIT_SIZES.get("badge_radius", 3)
        font_size = EDIT_SIZES.get("badge_font_size", 8)

        # Badge position: bottom center
        effects_count = len(self._effects)
        # Text width estimation (rough)
        text_width = 50 if effects_count > 3 else (effects_count * 8 + 20)
        badge_w = max(text_width + 8, 30)

        badge_x = (clip_rect.width() - badge_w) / 2
        badge_y = clip_rect.height() - badge_h - 2

        badge_rect = QRectF(badge_x, badge_y, badge_w, badge_h)

        # Background: semi-transparent black rounded rect
        badge_bg_color = QColor(get_color("badge_bg"))
        badge_bg_color.setAlpha(120)
        painter.setBrush(QBrush(badge_bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(badge_rect, badge_radius, badge_radius)

        # Text color
        badge_text_color = QColor(get_color("badge_text"))

        # Draw icon or text
        painter.setFont(QFont("Arial", font_size))
        painter.setPen(QPen(badge_text_color))

        # Show effects count: "3 effects" or just count with icon
        if effects_count <= 3:
            # Show count with simple text
            text = f"{effects_count} fx"
        else:
            # Show count only
            text = f"{effects_count} effects"

        # Draw text centered in badge
        text_rect = badge_rect.adjusted(4, 2, -4, -2)
        painter.drawText(
            text_rect,
            Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            text
        )

        painter.restore()

    def _draw_speed_indicator(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw speed indicator on clip (Phase 2.2).

        参考 Kdenlive: clip 右上角显示速度倍率。
        - 位置：clip 右上角（fade-out 手柄下方）
        - 显示 "2.0x" / "0.5x" 等速度倍率
        - 颜色：橙色（加速）/ 蓝色（慢放）
        - 背景：半透明深色圆角标签
        - 仅在 clip.speed != 1.0 时绘制

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
        """
        # Check conditions for drawing
        if not self.is_speed_modified():
            return

        # Check minimum width (UI-11: use MIN_CLIP_WIDTH_FOR_BADGES = 80)
        if self._width < MIN_CLIP_WIDTH_FOR_BADGES:
            return

        # Check if details are hidden
        if self._hide_details:
            return

        painter.save()

        # Indicator dimensions from design tokens
        indicator_h = EDIT_SIZES.get("indicator_h", 14)
        indicator_radius = EDIT_SIZES.get("indicator_radius", 2)
        font_size = EDIT_SIZES.get("indicator_font_size", 7)

        # Calculate indicator width based on text
        speed_text = f"{self._speed:.1f}x"
        text_width = len(speed_text) * 5 + 6
        indicator_w = max(text_width, 20)

        # Position: top-right, below fade handle if present
        # Start position accounting for fade handle space
        fade_handle_space = 14 if self._fade_out_duration > 0 else 0
        indicator_x = clip_rect.width() - indicator_w - 4
        indicator_y = 4 + fade_handle_space

        indicator_rect = QRectF(indicator_x, indicator_y, indicator_w, indicator_h)

        # Background: semi-transparent dark rounded rect
        bg_color = QColor(get_color("bg_input"))
        bg_color.setAlpha(180)
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(indicator_rect, indicator_radius, indicator_radius)

        # Speed color: orange for fast, blue for slow
        if self._speed > 1.0:
            speed_color = QColor(get_color("speed_fast"))  # Orange
        else:
            speed_color = QColor(get_color("speed_slow"))  # Blue

        # Draw text
        painter.setFont(QFont("Arial", font_size, QFont.Weight.Bold))
        painter.setPen(QPen(speed_color))

        text_rect = indicator_rect.adjusted(2, 1, -2, -1)
        painter.drawText(
            text_rect,
            Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            speed_text
        )

        painter.restore()

    def _draw_freeze_badge(self, painter: QPainter, clip_rect: QRectF) -> None:
        """A2: 冻结帧徽章（"FZ"），绘制在速度指示器旁，样式复用同一套 token。"""
        if getattr(self, "_freeze_source_frame", None) is None:
            return
        if self._width < MIN_CLIP_WIDTH_FOR_BADGES or self._hide_details:
            return

        painter.save()

        indicator_h = EDIT_SIZES.get("indicator_h", 14)
        indicator_radius = EDIT_SIZES.get("indicator_radius", 2)
        font_size = EDIT_SIZES.get("indicator_font_size", 7)

        freeze_text = "FZ"
        text_width = len(freeze_text) * 5 + 6
        indicator_w = max(text_width, 20)

        fade_handle_space = 14 if self._fade_out_duration > 0 else 0
        speed_space = indicator_h + 2 if self.is_speed_modified() else 0
        indicator_x = clip_rect.width() - indicator_w - 4
        indicator_y = 4 + fade_handle_space + speed_space

        indicator_rect = QRectF(indicator_x, indicator_y, indicator_w, indicator_h)

        bg_color = QColor(get_color("bg_input"))
        bg_color.setAlpha(180)
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(indicator_rect, indicator_radius, indicator_radius)

        # 雪蓝配色呼应冻结语义
        painter.setFont(QFont("Arial", font_size, QFont.Weight.Bold))
        painter.setPen(QPen(QColor(126, 190, 225)))
        painter.drawText(
            indicator_rect.adjusted(2, 1, -2, -1),
            Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            freeze_text,
        )

        painter.restore()

    def _draw_blend_badge(self, painter: QPainter, clip_rect: QRectF) -> None:
        """A1: 非 normal 混合模式的缩写徽章（左上角，如 "MUL"）。"""
        mode = getattr(self, "_blend_mode", "normal")
        if not mode or mode == "normal":
            return
        if self._width < MIN_CLIP_WIDTH_FOR_BADGES or self._hide_details:
            return

        painter.save()

        indicator_h = EDIT_SIZES.get("indicator_h", 14)
        indicator_radius = EDIT_SIZES.get("indicator_radius", 2)
        font_size = EDIT_SIZES.get("indicator_font_size", 7)

        badge_text = mode[:3].upper()
        text_width = len(badge_text) * 5 + 6
        indicator_w = max(text_width, 20)

        indicator_rect = QRectF(4, 4, indicator_w, indicator_h)

        bg_color = QColor(get_color("bg_input"))
        bg_color.setAlpha(180)
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(indicator_rect, indicator_radius, indicator_radius)

        painter.setFont(QFont("Arial", font_size, QFont.Weight.Bold))
        painter.setPen(QPen(QColor(170, 200, 255)))
        painter.drawText(
            indicator_rect.adjusted(2, 1, -2, -1),
            Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            badge_text,
        )

        painter.restore()

    def _retime_segments(self) -> tuple[tuple[int, int, float, int], ...]:
        """Return source and timeline geometry for the overlay's segments."""
        valid_points = tuple(
            (offset, speed)
            for offset, speed in self._retime_points
            if 0 < offset < self._source_duration
        )
        starts = (0,) + tuple(offset for offset, _ in valid_points)
        ends = tuple(offset for offset, _ in valid_points) + (self._source_duration,)
        speeds = (self._speed,) + tuple(speed for _, speed in valid_points)
        timeline_start = 0
        segments: list[tuple[int, int, float, int]] = []
        for source_start, source_end, speed in zip(starts, ends, speeds):
            timeline_length = max(1, round((source_end - source_start) / speed))
            segments.append((source_start, source_end, speed, timeline_start))
            timeline_start += timeline_length
        return tuple(segments)

    def _draw_retime_controls(self, painter: QPainter, clip_rect: QRectF) -> None:
        """Paint the in-clip retime strip used by Resolve's Edit timeline."""
        control_h = min(22.0, max(16.0, clip_rect.height() * 0.34))
        control_rect = QRectF(1.0, 1.0, max(0.0, clip_rect.width() - 2.0), control_h)
        painter.save()
        painter.setBrush(QBrush(QColor(22, 37, 49, 235)))
        painter.setPen(QPen(QColor(126, 190, 225, 220), 1.0))
        painter.drawRect(control_rect)

        segments = self._retime_segments()
        total_timeline = max(1, sum(
            max(1, round((end - start) / speed))
            for start, end, speed, _ in segments
        ))
        font = QFont("Arial", 7)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        for index, (source_start, source_end, speed, timeline_start) in enumerate(segments):
            timeline_length = max(1, round((source_end - source_start) / speed))
            x1 = control_rect.left() + control_rect.width() * timeline_start / total_timeline
            x2 = control_rect.left() + control_rect.width() * (timeline_start + timeline_length) / total_timeline
            segment_rect = QRectF(x1, control_rect.top(), max(1.0, x2 - x1), control_rect.height())
            shade = QColor(51, 105, 132, 215) if speed >= 1.0 else QColor(45, 85, 118, 215)
            painter.fillRect(segment_rect.adjusted(1.0, 1.0, -1.0, -1.0), shade)
            if index > 0:
                painter.setPen(QPen(QColor(235, 201, 72), 1.0))
                painter.drawLine(QPointF(x1, control_rect.top()), QPointF(x1, control_rect.bottom()))
                triangle = QPolygonF([
                    QPointF(x1 - 4.0, control_rect.top() + 1.0),
                    QPointF(x1 + 4.0, control_rect.top() + 1.0),
                    QPointF(x1, control_rect.top() + 6.0),
                ])
                painter.setBrush(QBrush(QColor(235, 201, 72)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawPolygon(triangle)
            if segment_rect.width() >= 38.0:
                painter.setPen(QPen(QColor(232, 238, 242)))
                label = f"{speed * 100.0:.0f}% ▾"
                painter.drawText(
                    segment_rect.adjusted(3.0, 1.0, -3.0, -1.0),
                    Qt.AlignmentFlag.AlignCenter,
                    label,
                )
        painter.restore()

    def _draw_clip_type_icon(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw clip type icon on clip (Phase 2.3).

        参考 Kdenlive: clip 左上角显示类型图标。
        - 位置：clip 左上角（名称上方或左侧）
        - 图标：视频/音频/图片/文字
        - 使用 Unicode 字符作为图标（简单方案）
        - 尺寸：14x14px
        - 仅在 clip 宽度 > 60px 时显示

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
        """
        # Comp Clips have their own graph icon; do not paint the ordinary
        # source-media letter underneath it.
        if self._is_comp_clip or self._composite_id:
            return

        # Check minimum width (UI-11: use MIN_CLIP_WIDTH_FOR_BADGES = 80)
        if self._width < MIN_CLIP_WIDTH_FOR_BADGES:
            return

        # Check if details are hidden
        if self._hide_details:
            return

        painter.save()

        # Icon dimensions from design tokens
        icon_size = EDIT_SIZES.get("clip_icon_size", 14)
        font_size = 10

        # Position: top-left, accounting for linked indicator and fade handle
        # Linked indicator takes ~16px if present
        linked_space = 16 if self._linked_clip_id else 0
        # Fade handle takes ~14px if present
        fade_handle_space = 14 if self._fade_in_duration > 0 else 0

        icon_x = 4 + linked_space
        icon_y = 4 + fade_handle_space

        # Icon color based on media type
        icon_colors = {
            "video": QColor(get_color("video_clip")).lighter(150),
            "audio": QColor(get_color("audio_clip")).lighter(150),
            "image": QColor(get_color("accent")).lighter(120),
            "text": QColor(get_color("text")),
        }

        # Media type text labels (avoid emoji — inconsistent cross-platform rendering)
        media_icons = {
            "video": "V",
            "audio": "A",
            "image": "I",
            "text": "T",
        }

        icon_color = icon_colors.get(self._media_type, QColor(get_color("text")))
        icon_char = media_icons.get(self._media_type, "")

        if icon_char:
            # Draw icon as text (Unicode emoji)
            painter.setFont(QFont("Arial", font_size))
            painter.setPen(QPen(icon_color))
            icon_rect = QRectF(icon_x, icon_y, icon_size, icon_size)
            painter.drawText(
                icon_rect,
                Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                icon_char
            )

        painter.restore()

    def _draw_opacity_indicator(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw opacity indicator on clip (Phase 2.4).

        参考 Kdenlive: clip 右上角显示透明度百分比。
        - 当 clip.opacity < 1.0 时显示 "80%" 透明度百分比
        - 半透明深色背景标签
        - 与 speed indicator 上下排列

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
        """
        # Check conditions for drawing
        if not self.is_opacity_modified():
            return

        # Check minimum width (UI-11: use MIN_CLIP_WIDTH_FOR_BADGES = 80)
        if self._width < MIN_CLIP_WIDTH_FOR_BADGES:
            return

        # Check if details are hidden
        if self._hide_details:
            return

        painter.save()

        # Indicator dimensions from design tokens
        indicator_h = EDIT_SIZES.get("indicator_h", 14)
        indicator_radius = EDIT_SIZES.get("indicator_radius", 2)
        font_size = EDIT_SIZES.get("indicator_font_size", 7)

        # Calculate indicator width based on text
        opacity_percent = int(self._opacity * 100)
        opacity_text = f"{opacity_percent}%"
        text_width = len(opacity_text) * 5 + 6
        indicator_w = max(text_width, 20)

        # Position: top-right, below speed indicator if present, or fade handle
        fade_handle_space = 14 if self._fade_out_duration > 0 else 0
        speed_indicator_space = indicator_h + 2 if self.is_speed_modified() else 0

        indicator_x = clip_rect.width() - indicator_w - 4
        indicator_y = 4 + fade_handle_space + speed_indicator_space

        indicator_rect = QRectF(indicator_x, indicator_y, indicator_w, indicator_h)

        # Background: semi-transparent dark rounded rect
        bg_color = QColor(get_color("bg_input"))
        bg_color.setAlpha(180)
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(indicator_rect, indicator_radius, indicator_radius)

        # Opacity color: gray/subtle
        opacity_color = QColor(get_color("opacity_indicator"))

        # Draw text
        painter.setFont(QFont("Arial", font_size))
        painter.setPen(QPen(opacity_color))

        text_rect = indicator_rect.adjusted(2, 1, -2, -1)
        painter.drawText(
            text_rect,
            Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            opacity_text
        )

        painter.restore()

    def _draw_comp_badge(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw COMP badge in top-left area of clip (Phase 6).

        Shows a small "C" badge when the clip has composite data.
        For Comp Clips (media_type == "comp_clip"), shows "COMP".
        Uses subtle colors that don't overwhelm the clip appearance.

        Badge position: top-left, after linked indicator and fade handle space.

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
        """
        if not self._is_comp_clip and not self._composite_id:
            return

        if self._width < MIN_CLIP_WIDTH_FOR_NAME:
            return

        painter.save()

        # Keep the graph icon visible at every useful clip width.  On a narrow
        # clip it becomes a compact node glyph; wider clips also show COMP.
        show_label = self._width >= 56
        badge_text = "COMP"
        font_size = 7
        badge_h = 15
        badge_w = 43 if show_label else 18
        badge_radius = 3

        # Position: top-left, after linked indicator and clip type icon
        linked_space = 16 if self._linked_clip_id else 0
        fade_handle_space = 14 if self._fade_in_duration > 0 else 0
        badge_x = 4 + linked_space
        badge_y = 4 + fade_handle_space

        # If the badge would overlap with the name, shift it up
        if badge_x + badge_w > clip_rect.width() - 4:
            painter.restore()
            return

        badge_rect = QRectF(badge_x, badge_y, badge_w, badge_h)

        # Background: semi-transparent dark
        painter.setBrush(QBrush(COMP_BADGE_BG_COLOR))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(badge_rect, badge_radius, badge_radius)

        # Draw a pair of offset layers rather than a tiny abstract node graph.
        # The stacked-frame silhouette remains legible at timeline scale and
        # matches the Comp marker used by the Media Pool.
        icon_center_y = badge_rect.center().y()
        icon_left = badge_rect.left() + 5
        painter.setPen(QPen(COMP_BADGE_ICON_COLOR, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            QRectF(icon_left, icon_center_y - 5, 9, 6), 1.5, 1.5
        )
        painter.drawRoundedRect(
            QRectF(icon_left + 3, icon_center_y - 1, 9, 6), 1.5, 1.5
        )

        if show_label:
            painter.setFont(QFont("Arial", font_size, QFont.Weight.Bold))
            painter.setPen(QPen(COMP_BADGE_TEXT_COLOR))
            text_rect = badge_rect.adjusted(20, 1, -2, -1)
            painter.drawText(
                text_rect,
                Qt.TextFlag.TextSingleLine
                | Qt.AlignmentFlag.AlignHCenter
                | Qt.AlignmentFlag.AlignVCenter,
                badge_text,
            )

        painter.restore()

    def _draw_generator_badge(self, painter: QPainter, clip_rect: QRectF) -> None:
        """Draw a badge for generator clips (Adjustment / Solid Color).

        Mirrors :meth:`_draw_comp_badge`'s layout. Shows "ADJ" for adjustment
        clips and "SOLID" (with a small colour swatch) for solid-color clips,
        so generators are distinguishable from regular video/audio clips.
        """
        is_adjustment = self._media_type == "adjustment"
        is_solid = self._media_type == "solid"
        is_motion_graphic = self._media_type == "motion_graphic"
        if not (is_adjustment or is_solid or is_motion_graphic):
            return

        if self._width < MIN_CLIP_WIDTH_FOR_NAME:
            return

        painter.save()

        badge_text = "ADJ" if is_adjustment else ("SOLID" if is_solid else "MOTION")
        font_size = 7 if is_adjustment else (6 if is_solid else 5)
        badge_h = 14
        badge_w = 24 if is_adjustment else (34 if is_solid else 42)
        badge_radius = 3

        linked_space = 16 if self._linked_clip_id else 0
        fade_handle_space = 14 if self._fade_in_duration > 0 else 0
        badge_x = 4 + linked_space + 14
        badge_y = 4 + fade_handle_space

        if badge_x + badge_w > clip_rect.width() - 4:
            painter.restore()
            return

        badge_rect = QRectF(badge_x, badge_y, badge_w, badge_h)

        painter.setBrush(QBrush(COMP_BADGE_BG_COLOR))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(badge_rect, badge_radius, badge_radius)

        # For solid clips, draw a small colour swatch on the left of the badge.
        text_rect = badge_rect.adjusted(2, 1, -2, -1)
        if is_solid:
            swatch_size = 8
            swatch_rect = QRectF(
                badge_x + 3,
                badge_y + (badge_h - swatch_size) / 2,
                swatch_size,
                swatch_size,
            )
            swatch_qc = QColor.fromRgbF(
                max(0.0, min(1.0, self._solid_color[0])),
                max(0.0, min(1.0, self._solid_color[1])),
                max(0.0, min(1.0, self._solid_color[2])),
            ) if self._solid_color else QColor(0, 153, 204)
            painter.setBrush(QBrush(swatch_qc))
            painter.drawRoundedRect(swatch_rect, 2, 2)
            text_rect.adjust(swatch_size + 2, 0, 0, 0)

        painter.setFont(QFont("Arial", font_size, QFont.Weight.Bold))
        painter.setPen(QPen(COMP_BADGE_TEXT_COLOR))
        painter.drawText(
            text_rect,
            Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            badge_text,
        )

        painter.restore()

    def _draw_comp_cache_status(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw cache status line at bottom of clip (Phase 6).

        A thin (2px) colored line at the bottom indicates the render cache status:
        - dirty: orange (#FF8C00)
        - rendering: blue (#4A90D9)
        - ready: green (#4CAF50)
        - failed: red (#E53935)

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
        """
        status = self._comp_cache_status
        if not status or status == "none":
            return

        # Map status to color
        status_colors = {
            "dirty": COMP_CACHE_DIRTY_COLOR,
            "rendering": COMP_CACHE_RENDERING_COLOR,
            "ready": COMP_CACHE_READY_COLOR,
            "failed": COMP_CACHE_FAILED_COLOR,
        }
        color = status_colors.get(status)
        if color is None:
            return

        painter.save()

        # Draw thin line at bottom of clip, inside the border
        line_y = clip_rect.height() - COMP_CACHE_LINE_HEIGHT - 1
        line_rect = QRectF(1, line_y, clip_rect.width() - 2, COMP_CACHE_LINE_HEIGHT)

        painter.setBrush(QBrush(color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRect(line_rect)

        painter.restore()

    def _draw_drag_frame_label(self, painter: QPainter, clip_rect: QRectF) -> None:
        """
        Draw drag frame number label above clip (Phase 2.5).

        参考 Kdenlive: 拖动 clip 时在上方显示目标帧号或吸附状态。
        - 位置：clip 上方居中
        - 显示 "f{target_frame}" 帧号
        - 如果有吸附，显示绿色 "snap" 标签代替帧号
        - 标签高度 20px
        - 背景深色半透明

        Args:
            painter: QPainter instance
            clip_rect: Clip bounding rectangle
        """
        painter.save()

        # Label dimensions
        label_h = DRAG_FRAME_LABEL_HEIGHT
        label_radius = 3

        # Determine label content and colors
        if self._is_snapping:
            # Snap state: green background with "snap" text
            bg_color = DRAG_FRAME_LABEL_SNAP_COLOR
            text_color = DRAG_FRAME_LABEL_SNAP_TEXT_COLOR
            label_text = "snap"
        else:
            # Normal state: dark background with frame number
            bg_color = DRAG_FRAME_LABEL_BG_COLOR
            text_color = DRAG_FRAME_LABEL_TEXT_COLOR
            label_text = f"f{self._drag_target_frame}"

        # Calculate label width based on text
        text_width = len(label_text) * 8 + 12
        label_w = max(text_width, 40)

        # Position: above clip, centered horizontally
        label_x = (clip_rect.width() - label_w) / 2
        label_y = -label_h - 4  # Above clip with small gap

        label_rect = QRectF(label_x, label_y, label_w, label_h)

        # Draw background
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(label_rect, label_radius, label_radius)

        # Draw text
        painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        painter.setPen(QPen(text_color))

        text_rect = label_rect.adjusted(4, 2, -4, -2)
        painter.drawText(
            text_rect,
            Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            label_text
        )

        painter.restore()

    def set_drag_frame_label(
        self,
        visible: bool,
        target_frame: int = 0,
        is_snapping: bool = False
    ) -> None:
        """
        Set drag frame label visibility and state (Phase 2.5).

        Called by TimelineView during drag operations to update
        the frame number display.

        Args:
            visible: Whether to show the label
            target_frame: Target frame number to display
            is_snapping: Whether currently in snap state
        """
        self._drag_frame_label_visible = visible
        self._drag_target_frame = target_frame
        self._is_snapping = is_snapping
        self.update()

    # ==================== Interaction ====================

    def hoverMoveEvent(self, event: Any) -> None:
        """Handle hover move to detect edge proximity and set cursor.

        AUD-21103: 先把新的 hover 状态（edge/fade/tooltip/cursor）算入局部
        变量，只在实际变化时才写回并 update()——本 item 开启
        DeviceCoordinateCache，任何 update() 都会使缓存失效并整卡重建
        （filmstrip 缩略图/波形/徽章全量重绘），此前每次鼠标移动（≥60Hz）
        无条件 update 曾造成持续掉帧。写法参照 TrackHeaderItem.hoverMoveEvent
        的差异检测。
        """
        pos = event.pos()

        old_edge = self._hover_edge
        old_fade = self._hover_fade_handle

        new_edge: Optional[str]
        new_fade: Optional[str]
        new_tooltip: str
        new_cursor: Any  # Qt.CursorShape 或 QCursor（_trim_cursor 为自定义位标）

        # If track is locked, show forbidden cursor and don't allow editing
        if self._is_track_locked:
            new_edge = None
            new_fade = None
            new_tooltip = f"{self._asset_name}\nFrame: {self._start_frame}\n(Track Locked)"
            new_cursor = Qt.CursorShape.ForbiddenCursor

        # Slip tool (P4.4): entire clip becomes slip handle
        elif self._active_tool == EditTool.SLIP:
            new_edge = "slip"
            new_fade = None
            new_tooltip = f"{self._asset_name}\nSlip: drag to change source range"
            new_cursor = Qt.CursorShape.SizeHorCursor

        # Slide tool (P4.5): entire clip becomes slide handle
        elif self._active_tool == EditTool.SLIDE:
            new_edge = "slide"
            new_fade = None
            new_tooltip = f"{self._asset_name}\nSlide: drag to move clip, adjusts adjacent clips"
            new_cursor = Qt.CursorShape.SizeAllCursor

        # Ripple tool (P4.2): edges become ripple trim handles
        elif self._active_tool == EditTool.RIPPLE:
            if pos.x() < HANDLE_SIZE:
                new_edge = "ripple_left"
                new_tooltip = "Ripple Trim In (drag to adjust, shifts subsequent clips)"
                new_cursor = Qt.CursorShape.SizeHorCursor
            elif pos.x() > self._width - HANDLE_SIZE:
                new_edge = "ripple_right"
                new_tooltip = "Ripple Trim Out (drag to adjust, shifts subsequent clips)"
                new_cursor = Qt.CursorShape.SizeHorCursor
            else:
                new_edge = None
                new_tooltip = f"{self._asset_name}\nFrame: {self._start_frame}"
                new_cursor = Qt.CursorShape.OpenHandCursor
            new_fade = None

        # Roll tool (P4.3): edges become roll trim handles (only at cut points)
        elif self._active_tool == EditTool.ROLL:
            if pos.x() < HANDLE_SIZE:
                new_edge = "roll_left"
                new_tooltip = "Roll Trim: adjust cut point between this and previous clip"
                new_cursor = Qt.CursorShape.SizeHorCursor
            elif pos.x() > self._width - HANDLE_SIZE:
                new_edge = "roll_right"
                new_tooltip = "Roll Trim: adjust cut point between this and next clip"
                new_cursor = Qt.CursorShape.SizeHorCursor
            else:
                new_edge = None
                new_tooltip = f"{self._asset_name}\nFrame: {self._start_frame}"
                new_cursor = Qt.CursorShape.OpenHandCursor
            new_fade = None

        else:
            # Phase 1.2: Check the moving fade endpoints for video and audio
            # clips.  The hit area is deliberately larger than the painted
            # triangle, matching professional NLE interaction targets.
            fade_handle = self._fade_handle_at(pos)
            if fade_handle == "in":
                new_fade = "fade_in"
                new_edge = "fade_in"
                current_fade = self._fade_in_duration
                new_tooltip = f"Fade In: {current_fade} frames (drag to adjust)"
                new_cursor = Qt.CursorShape.SizeHorCursor
            elif fade_handle == "out":
                new_fade = "fade_out"
                new_edge = "fade_out"
                current_fade = self._fade_out_duration
                new_tooltip = f"Fade Out: {current_fade} frames (drag to adjust)"
                new_cursor = Qt.CursorShape.SizeHorCursor
            else:
                new_fade = None
                # Check if near edges (for trim)
                if pos.x() < HANDLE_SIZE:
                    new_edge = "left"
                    new_tooltip = "Trim in point (drag to adjust)"
                    new_cursor = _trim_cursor("left")
                elif pos.x() > self._width - HANDLE_SIZE:
                    new_edge = "right"
                    new_tooltip = "Trim out point (drag to adjust)"
                    new_cursor = _trim_cursor("right")
                else:
                    new_edge = None
                    new_tooltip = f"{self._asset_name}\nFrame: {self._start_frame}"
                    # P2-7: Open hand cursor for clip body (indicates draggability)
                    new_cursor = Qt.CursorShape.OpenHandCursor

        # 只在实际变化时写回（避免 DeviceCoordinateCache 整卡重建）
        if new_edge != old_edge or new_fade != old_fade:
            self._hover_edge = new_edge
            self._hover_fade_handle = new_fade
            self.update()
        if self.toolTip() != new_tooltip:
            self.setToolTip(new_tooltip)
        if isinstance(new_cursor, Qt.CursorShape):
            if self.cursor().shape() != new_cursor:
                self.setCursor(new_cursor)
        elif self.cursor() != new_cursor:
            self.setCursor(new_cursor)

        super().hoverMoveEvent(event)

    def hoverEnterEvent(self, event: Any) -> None:
        """
        Handle hover enter to emit linked group and track highlight signals (Phase 6.1-6.2).

        参考 Kdenlive: hover 一个 clip 时，同组的所有 clip 边框同时高亮。
        """
        # Phase 6.1: Emit linked group hover signal to highlight linked clip
        if self._linked_clip_id:
            self.linked_group_hovered.emit(self._clip_id, self._linked_clip_id, True)

        # Phase 6.2: Emit track highlight request signal
        self.track_highlight_requested.emit(self._track_id, True)

        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: Any) -> None:
        """
        Handle hover leave to clear linked group and track highlight (Phase 6.1-6.2).

        参考 Kdenlive: hover 离开 clip 时，同组的所有 clip 边框取消高亮。
        """
        self._hover_edge = None
        self._hover_fade_handle = None  # Phase 1.2: Clear fade hover state
        # Reset cursor (P2-7: back to default)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        # Phase 6.1: Emit linked group hover signal to clear highlight on linked clip
        if self._linked_clip_id:
            self.linked_group_hovered.emit(self._clip_id, self._linked_clip_id, False)

        # Phase 6.2: Emit track highlight request signal (clear)
        self.track_highlight_requested.emit(self._track_id, False)

        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        """Handle mouse press for selection and interaction."""
        if event.button() == Qt.MouseButton.LeftButton:
            # Always allow selection, but check lock for editing operations
            pos = event.pos()

            # If track is locked, only allow selection (no drag/trim)
            if self._is_track_locked:
                # Still emit click for selection
                self.clip_clicked.emit(self._clip_id, self._track_id)
                super().mousePressEvent(event)
                return

            # In Resolve the percentage label is itself the segment editor.
            # Keep this before clip drag handling so a click never turns into
            # an accidental move of the clip.
            if (
                self._retime_controls_visible
                and pos.y() <= min(22.0, max(16.0, self._track_height * 0.34))
            ):
                self._show_retime_segment_speed_menu(event.screenPos(), pos.x())
                event.accept()
                return

            # Store original state for undo
            self._original_start = self._start_frame
            self._original_duration = self._duration_frames
            self._original_source_start = self._source_start
            self._original_source_duration = self._source_duration
            # Phase 1.2: Store original fade state for undo
            self._original_fade_in = self._fade_in_duration
            self._original_fade_out = self._fade_out_duration
            # INT-01（审计 A 批）：波纹边缘单击（未拖动）此前沿用构造时
            # 的 0 或上一次拖拽残留的 final 值，release 把 new_end=0 发给
            # 命令并抛 TimelineOperationError。每次按下先把 final 重置为
            # 当前几何，release 只在 move 真正更新过 final 且与原值不同
            # 时才提交。
            self._final_new_start = self._start_frame
            self._final_new_end = self._start_frame + self._duration_frames

            # Shift+right-edge is reserved for Motion Graphic duration
            # retiming.  Check it before the fade handle so a clip that also
            # has an authored fade remains predictable.
            motion_graphic_retime = (
                self._media_type == "motion_graphic"
                and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                and pos.x() > self._width - HANDLE_SIZE
            )
            # Phase 1.2: Check the moving fade endpoint before the other
            # tool-specific edge handling.  This is intentionally larger than
            # the visible triangle so audio waveform clips remain easy to use.
            fade_handle = self._fade_handle_at(pos)
            if motion_graphic_retime:
                self._drag_mode = "motion_graphic_duration"
                self._drag_start_frame = self._duration_frames
            elif fade_handle == "in":
                self._drag_mode = "fade_in"
                self._drag_start_pos = event.scenePos()
            elif fade_handle == "out":
                self._drag_mode = "fade_out"
                self._drag_start_pos = event.scenePos()
            # Slip tool (P4.4): entire clip is slip handle
            elif self._active_tool == EditTool.SLIP:
                self._drag_mode = "slip"
                self._slip_delta = 0  # Reset slip delta
                self._drag_start_pos = event.scenePos()
            # Slide tool (P4.5): entire clip is slide handle
            elif self._active_tool == EditTool.SLIDE:
                self._drag_mode = "slide"
                self._drag_start_frame = self._start_frame  # Store original start for delta calculation
            # Ripple tool (P4.2): edges are ripple trim handles
            elif self._active_tool == EditTool.RIPPLE:
                if pos.x() < HANDLE_SIZE:
                    self._drag_mode = "ripple_trim_left"
                    self._drag_start_frame = self._start_frame
                elif pos.x() > self._width - HANDLE_SIZE:
                    self._drag_mode = "ripple_trim_right"
                    self._drag_start_frame = self._start_frame + self._duration_frames  # Store end frame
                else:
                    self._drag_mode = "move"
                    self._drag_start_frame = self._start_frame
            # Roll tool (P4.3): edges are roll trim handles
            elif self._active_tool == EditTool.ROLL:
                if pos.x() < HANDLE_SIZE:
                    self._drag_mode = "roll_trim_left"
                    self._drag_start_frame = self._start_frame
                elif pos.x() > self._width - HANDLE_SIZE:
                    self._drag_mode = "roll_trim_right"
                    self._drag_start_frame = self._start_frame + self._duration_frames
                else:
                    self._drag_mode = "move"
                    self._drag_start_frame = self._start_frame
            # Rate stretch (B4): edges resize duration; speed is derived on
            # release (source_duration / new_duration, clamped 0.1-8.0)
            elif self._active_tool == EditTool.RATE_STRETCH:
                if pos.x() < HANDLE_SIZE:
                    self._drag_mode = "rate_stretch_left"
                    self._drag_start_frame = self._start_frame
                elif pos.x() > self._width - HANDLE_SIZE:
                    self._drag_mode = "rate_stretch_right"
                    self._drag_start_frame = self._start_frame + self._duration_frames
                else:
                    self._drag_mode = "move"
                    self._drag_start_frame = self._start_frame
            # Standard trim or select tool
            elif pos.x() < HANDLE_SIZE:
                self._drag_mode = "trim_left"
                self._drag_start_frame = self._start_frame
            elif pos.x() > self._width - HANDLE_SIZE:
                self._drag_mode = "trim_right"
                self._drag_start_frame = self._duration_frames
            else:
                self._drag_mode = "move"
                self._drag_start_frame = self._start_frame

            # INT-02：让 view 的 InteractionSession 感知片段级拖拽/修剪，
            # Esc/换工具/失焦才能取消；此前媒体 item press 从不 begin，
            # Esc 只会清空选区，松手仍提交命令。
            if self._drag_mode:
                self.interaction_started.emit(
                    self._clip_id, self._track_id, self._drag_mode
                )

            if self._drag_mode in {
                "trim_left",
                "trim_right",
                "motion_graphic_duration",
                "ripple_trim_left",
                "ripple_trim_right",
            }:
                self.begin_trim_preview()

            self._is_dragging = False  # P2-6: Not dragging yet, wait for threshold
            self._drag_pending = True  # P2-6: Press detected, awaiting threshold
            self._copy_mode = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
            self._drag_start_pos = event.scenePos()
            self._last_move_emit = None
            self.clip_clicked.emit(self._clip_id, self._track_id)
            event.accept()  # Ensure mouse grab is established
            return

        elif event.button() == Qt.MouseButton.RightButton:
            # Show context menu
            # Right-click also selects the clip, matching normal NLE behavior
            # and ensuring Duplicate acts on the item under the pointer.
            self.clip_clicked.emit(self._clip_id, self._track_id)
            self._show_clip_context_menu(event.screenPos(), event.pos())
            event.accept()
            return

        super().mousePressEvent(event)

    def _show_clip_context_menu(self, screen_pos: QPointF, item_pos: QPointF) -> None:
        """Show clip context menu."""
        from PyQt6.QtWidgets import QLabel, QMenu, QWidgetAction
        from src.shared.i18n.i18n import I18N

        # Create menu
        menu = QMenu()

        # Initialize action variables (to avoid NameError when referenced later)
        add_transition_after = None
        add_transition_after_dip = None
        add_transition_before = None
        open_in_composite_action = None
        use_shared_action = None
        make_local_action = None
        open_in_color_action = None
        render_cache_media_action = None
        speech_to_text_action = None
        # Phase 6: Composite submenu action variables
        comp_convert_action = None
        comp_remove_binding_action = None
        comp_bypass_action = None
        comp_reset_action = None
        comp_reveal_action = None
        comp_render_cache_action = None
        duplicate_action = None
        rerender_cache_action = None
        retime_toggle_action = None
        add_speed_point_action = None
        clear_speed_points_action = None
        freeze_action = None
        replace_placeholder_action = None
        audio_gain_action = None
        waveform_mono_action = None
        waveform_stereo_action = None
        create_nested_action = None

        # Determine if near an edge (for transition options)
        is_near_right_edge = item_pos.x() > self._width - HANDLE_SIZE * 2
        is_near_left_edge = item_pos.x() < HANDLE_SIZE * 2

        # Add transition options
        if not self._is_track_locked:
            if is_near_right_edge:
                # Add transition after this clip
                add_transition_after = menu.addAction(
                    I18N.tr("timeline.menu.add_transition_dissolve", "Add Transition (Cross Dissolve)"))
                add_transition_after_dip = menu.addAction(
                    I18N.tr("timeline.menu.add_transition_dip", "Add Transition (Dip To Color)"))

            if is_near_left_edge:
                # Add transition before this clip
                add_transition_before = menu.addAction(
                    I18N.tr("timeline.menu.add_transition_before", "Add Transition Before..."))

        menu.addSeparator()

        # Retime Controls match Resolve's clip-context workflow: show the
        # in-clip strip, add a point at the playhead, then edit a segment from
        # its percentage dropdown.  Points only apply to visual clips; audio
        # remains linked by the existing constant-speed playback path.
        if self._media_type in ("video", "image", "comp_clip", "motion_graphic"):
            retime_toggle_action = menu.addAction(
                I18N.tr("edit.clip.retime_controls", "Retime Controls")
            )
            retime_toggle_action.setCheckable(True)
            retime_toggle_action.setChecked(self._retime_controls_visible)
            add_speed_point_action = menu.addAction(
                I18N.tr("edit.clip.add_speed_point", "Add Speed Point")
            )
            add_speed_point_action.setEnabled(not self._is_track_locked)
            clear_speed_points_action = menu.addAction(
                I18N.tr("edit.clip.clear_speed_points", "Clear Speed Points")
            )
            clear_speed_points_action.setEnabled(
                bool(self._retime_points) and not self._is_track_locked
            )
            # A2: 冻结帧（播放头严格位于片段内时可用）
            freeze_action = menu.addAction(
                I18N.tr("edit.clip.freeze_frame", "Freeze Frame at Playhead")
            )
            try:
                _fw_view = self.scene().views()[0] if self.scene() and self.scene().views() else None
                _fw_playhead = getattr(_fw_view, "_playhead_frame", None)
            except Exception:
                _fw_playhead = None
            inside = (
                _fw_playhead is not None
                and self._start_frame < _fw_playhead < self._start_frame + self._duration_frames
            )
            freeze_action.setEnabled(inside and not self._is_track_locked)

            # B7: 占位片段的"替换为真实素材"入口（可用性由视图按素材类型判断）
            _rv = self.scene().views()[0] if self.scene() and self.scene().views() else None
            _is_placeholder = False
            try:
                _asset = _rv._project.get_asset_by_id(self._asset_id) if _rv is not None and getattr(_rv, "_project", None) is not None else None
                _is_placeholder = _asset is not None and (
                    getattr(_asset, "media_type", "") == "placeholder")
            except Exception:
                _is_placeholder = False
            replace_placeholder_action = menu.addAction(
                I18N.tr("edit.clip.replace_with", "Replace With…"))
            replace_placeholder_action.setEnabled(
                _is_placeholder and not self._is_track_locked)

        # Keep a direct, top-level cache command as a reliable fallback for
        # Motion Graphic/Composite clips.  The same request is also available
        # inside the Composite submenu, but a visible top-level action is much
        # easier to discover when Space-triggered automatic caching is not
        # available (for example after a keyboard-focus conflict).
        if (
            self._media_type == "motion_graphic"
            or bool(self._composite_id)
            or self._is_comp_clip
        ):
            render_cache_media_action = menu.addAction(
                I18N.tr("edit.clip.generate_cache_media", "生成缓存媒体")
            )
            render_cache_media_action.setEnabled(not self._is_track_locked)
            rerender_cache_action = menu.addAction(
                I18N.tr("edit.clip.rerender_cache", "重新渲染缓存")
            )
            rerender_cache_action.setEnabled(not self._is_track_locked)

        menu.addSeparator()

        # Composite submenu (for video/image/comp_clip media types).
        #
        # Workflow contract: a clip must be a composite clip (have a
        # composite_id, or be a comp_clip asset) before it can be opened
        # in the Composite page. The two entries below are mutually
        # exclusive based on that state, so the user always has a single
        # unambiguous next step:
        #   - normal clip      -> "Open in Composite" disabled, "Create Comp
        #                          Clip" enabled (create first, then open)
        #   - composite clip   -> "Open in Composite" enabled,  "Create Comp
        #                          Clip" disabled (already created)
        if self._media_type in ("video", "image", "comp_clip", "motion_graphic"):
            is_composite_clip = bool(self._composite_id or self._is_comp_clip)

            comp_menu = menu.addMenu(
                I18N.tr("edit.clip.composite_menu", "Composite")
            )

            # Open in Composite — enabled only when the clip already has a
            # composite graph to open.
            open_in_composite_action = comp_menu.addAction(
                I18N.tr("edit.clip.open_in_composite", "Open in Composite...")
            )
            open_in_composite_action.setEnabled(is_composite_clip)

            # Create Comp Clip — enabled only when the clip is not yet a
            # composite clip (idempotent operation otherwise, so disable it
            # to avoid confusing the user with a no-op).
            comp_convert_action = comp_menu.addAction(
                I18N.tr("edit.clip.create_comp_clip", "Create Comp Clip")
            )
            comp_convert_action.setEnabled(not is_composite_clip)

            if self._is_comp_clip or self._media_type == "motion_graphic":
                # Comp Clip/Motion Graphic cache management items
                comp_menu.addSeparator()
                if self._is_comp_clip:
                    bypass_label = (
                        I18N.tr("edit.clip.enable_composite", "Enable Composite")
                        if self._composite_bypassed
                        else I18N.tr("edit.clip.bypass_composite", "Bypass Composite")
                    )
                    comp_bypass_action = comp_menu.addAction(bypass_label)
                    comp_reset_action = comp_menu.addAction(
                        I18N.tr("edit.clip.reset_composite", "Reset Composite")
                    )
                    comp_remove_binding_action = comp_menu.addAction(
                        I18N.tr("edit.clip.remove_composite_binding", "Remove Composite Binding")
                    )
                    comp_reveal_action = comp_menu.addAction(
                        I18N.tr("edit.clip.reveal_in_media_pool", "Reveal Comp Clip in Media Pool")
                    )
                comp_render_cache_action = comp_menu.addAction(
                    I18N.tr("edit.clip.render_cache_clip", "Render Cache This Clip")
                )

        menu.addSeparator()

        # Audio clip transcription.  Keep this on the clip menu so the source
        # trim and timeline placement are unambiguous; the handler performs the
        # expensive ASR work off the UI thread and writes subtitles via commands.
        if self._media_type == "audio" and not self._is_track_locked:
            speech_to_text_action = menu.addAction(
                I18N.tr("edit.clip.speech_to_text", "语音转文字")
            )

        # P9.1: Grade options (for video/image clips only)
        if self._media_type in ("video", "image", "comp_clip", "motion_graphic") and not self._is_track_locked:
            # Grade submenu
            grade_menu = menu.addMenu(
                I18N.tr("edit.clip.grade_menu", "Grade")
            )

            # Show current grade mode status
            if self._color_shared:
                # Currently using shared asset grade
                use_shared_action = grade_menu.addAction(
                    I18N.tr("edit.clip.use_shared_grade", "Use Shared Asset Grade")
                )
                use_shared_action.setEnabled(False)  # Already using shared
                use_shared_action.setCheckable(True)
                use_shared_action.setChecked(True)

                make_local_action = grade_menu.addAction(
                    I18N.tr("edit.clip.make_local_grade_copy", "Make Local Grade Copy")
                )
            else:
                # Currently using local grade
                use_shared_action = grade_menu.addAction(
                    I18N.tr("edit.clip.use_shared_grade", "Use Shared Asset Grade")
                )

                make_local_action = grade_menu.addAction(
                    I18N.tr("edit.clip.make_local_grade_copy", "Make Local Grade Copy")
                )
                make_local_action.setEnabled(False)  # Already local
                make_local_action.setCheckable(True)
                make_local_action.setChecked(True)

            # Open in Color action (opens current stack in Color page)
            grade_menu.addSeparator()
            open_in_color_action = grade_menu.addAction(
                I18N.tr("edit.clip.open_in_color", "Open in Color...")
            )

        menu.addSeparator()

        # P9-2: Scene Cut Detection (for video clips only)
        scene_cut_detect_action = None
        if self._media_type == "video" and not self._is_track_locked:
            scene_cut_detect_action = menu.addAction(
                I18N.tr("edit.clip.scene_cut_detect", "Scene Cut Detection...")
            )

        menu.addSeparator()

        # P2: Label Color submenu
        label_color_actions = {}
        label_menu = menu.addMenu(
            I18N.tr("edit.clip.label_color", "Label Color")
        )
        # Default (no color) option
        default_action = label_menu.addAction(
            I18N.tr("edit.clip.label_default", "Default")
        )
        label_color_actions[default_action] = None

        label_menu.addSeparator()

        # Use colored text instead of plain names so the menu itself gives a
        # reliable preview of the clip color. Transparent mouse handling lets
        # QMenu keep normal QAction hover/selection behavior.
        for name, hex_color in TIMELINE_LABEL_COLORS:
            color_action = QWidgetAction(label_menu)
            color_name = QLabel(name)
            color_name.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                True,
            )
            color_name.setStyleSheet(
                "QLabel { "
                f"color: {hex_color}; "
                "padding: 4px 18px 4px 12px; "
                "font-weight: 600; "
                "}"
            )
            color_action.setDefaultWidget(color_name)
            label_menu.addAction(color_action)
            label_color_actions[color_action] = hex_color

        menu.addSeparator()

        # Thumbnail display mode submenu (for video/image clips only)
        thumb_off_action = None
        thumb_start_end_action = None
        thumb_filmstrip_action = None
        if self._media_type in ("video", "image", "comp_clip", "motion_graphic"):
            thumb_menu = menu.addMenu(
                I18N.tr("edit.clip.thumbnail_menu", "Thumbnails")
            )

            current_mode = ClipItem._thumbnail_display_mode

            thumb_off_action = thumb_menu.addAction(
                I18N.tr("edit.clip.thumbnail_off", "Off")
            )
            thumb_off_action.setCheckable(True)
            thumb_off_action.setChecked(current_mode == "none")

            thumb_start_end_action = thumb_menu.addAction(
                I18N.tr("edit.clip.thumbnail_start_end", "First & Last Frame")
            )
            thumb_start_end_action.setCheckable(True)
            thumb_start_end_action.setChecked(current_mode == "start_end")

            thumb_filmstrip_action = thumb_menu.addAction(
                I18N.tr("edit.clip.thumbnail_filmstrip", "All Frames")
            )
            thumb_filmstrip_action.setCheckable(True)
            thumb_filmstrip_action.setChecked(current_mode in ("filmstrip", "auto"))

        menu.addSeparator()

        # Enable/Disable clip toggle
        # INT-08：旧代码读不存在的 _clip_enabled 字段导致禁用片段菜单
        # 仍显示 "Disable Clip"；统一用唯一 is_enabled 状态。
        clip_enabled = self._is_enabled
        enable_label = (
            I18N.tr("edit.clip.disable", "Disable Clip")
            if clip_enabled
            else I18N.tr("edit.clip.enable", "Enable Clip")
        )
        enable_action = menu.addAction(enable_label)
        # C09/INT-05：锁轨时启用/禁用与菜单灰显策略一致（命令层亦有守卫）
        enable_action.setEnabled(not self._is_track_locked)

        # Link/Unlink clips
        clip_obj = None
        # Try to get clip from timeline to check linked_clip_id
        timeline_view = None
        if self.scene() and self.scene().views():
            timeline_view = self.scene().views()[0]
        if timeline_view and hasattr(timeline_view, '_timeline') and timeline_view._timeline:
            clip_obj = timeline_view._timeline.get_clip_by_id(self._clip_id)

        # C09/INT-05：链接/解链影响两条轨——自身或伙伴轨锁定都不可用
        link_blocked_tracks: list[str] = []
        if self._is_track_locked:
            link_blocked_tracks.append(self._track_id)
        partner_obj = None
        if clip_obj is not None and clip_obj.linked_clip_id and timeline_view is not None:
            partner_obj = timeline_view._timeline.get_clip_by_id(clip_obj.linked_clip_id)
        if partner_obj is not None:
            partner_track = timeline_view._timeline.get_track_by_id(partner_obj.track_id)
            if partner_track is not None and partner_track.is_locked:
                link_blocked_tracks.append(partner_track.track_id)
        link_editable = not link_blocked_tracks

        has_link = clip_obj and clip_obj.linked_clip_id
        if has_link:
            unlink_action = menu.addAction(
                I18N.tr("edit.clip.unlink", "Unlink Clips")
            )
            unlink_action.setEnabled(link_editable)
        link_action = menu.addAction(
            I18N.tr("edit.clip.link", "Link Clips")
        )
        link_action.setEnabled(link_editable)
        if not link_editable:
            tip = I18N.tr(
                "edit.clip.locked_track_tooltip",
                "轨道已锁定，无法编辑",
            )
            link_action.setToolTip(tip)
            if has_link:
                unlink_action.setToolTip(tip)

        # P1-04: 数值修剪（Trim Monitor 语义的键盘/数值入口）
        numeric_trim_action = menu.addAction(
            I18N.tr("edit.clip.numeric_trim", "Numeric Trim…")
        )
        numeric_trim_action.setEnabled(not self._is_track_locked)

        # P1-02: 嵌套序列。普通媒体片段提供"从选择创建"；嵌套片段提供"进入"。
        enter_nested_action = None
        if self._media_type == "nested_sequence":
            enter_nested_action = menu.addAction(
                I18N.tr("edit.clip.enter_nested", "Enter Nested Sequence")
            )
            enter_nested_action.setToolTip(
                I18N.tr(
                    "edit.clip.enter_nested_tip",
                    "双击片段等效；编辑子序列后父序列画面即时更新",
                )
            )
        elif self._media_type in ("video", "image", "comp_clip", "motion_graphic", "audio"):
            create_nested_action = menu.addAction(
                I18N.tr("edit.clip.create_nested", "Create Nested Sequence from Selection")
            )
            create_nested_action.setEnabled(not self._is_track_locked)

        menu.addSeparator()

        # Change Speed submenu
        speed_menu = menu.addMenu(
            I18N.tr("edit.clip.change_speed", "Change Speed")
        )
        # C09/INT-05：锁轨禁用变速菜单（命令层 apply 同步拒绝）
        speed_menu.setEnabled(not self._is_track_locked)
        speed_actions = {}
        for speed_val, speed_label in [
            (0.10, "10%"), (0.25, "25%"), (0.50, "50%"), (0.75, "75%"),
            (1.0, "100%"), (1.10, "110%"), (1.50, "150%"),
            (2.0, "200%"), (4.0, "400%"), (8.0, "800%"),
        ]:
            act = speed_menu.addAction(speed_label)
            speed_actions[speed_val] = act

        menu.addSeparator()

        # Resolve-style audio waveform display. Mono mixdown is the default
        # because it is compact and avoids building two painter paths for the
        # common case; stereo remains available per clip when channel detail
        # is needed.
        if self._media_type == "audio":
            waveform_menu = menu.addMenu(
                I18N.tr("edit.clip.waveform_menu", "Waveform Display")
            )
            waveform_mono_action = waveform_menu.addAction(
                I18N.tr("edit.clip.waveform_mono", "Mono (Mixdown)")
            )
            waveform_mono_action.setCheckable(True)
            waveform_mono_action.setChecked(not self._waveform_show_channels)
            waveform_stereo_action = waveform_menu.addAction(
                I18N.tr("edit.clip.waveform_stereo", "Stereo (L/R)")
            )
            waveform_stereo_action.setCheckable(True)
            waveform_stereo_action.setChecked(self._waveform_show_channels)

            # C3: 音频片段的 Audio Gain 对话框入口。
            # AUD-21101: addAction 必须在 menu.exec() 之前——exec 是模态阻塞
            # 调用，菜单关闭后再 addAction 的动作永远不会显示，该入口
            # 此前完全不可达。
            audio_gain_action = menu.addAction(
                I18N.tr("edit.clip.audio_gain", "Audio Gain…"))
            audio_gain_action.setEnabled(not self._is_track_locked)

        menu.addSeparator()

        # Delete action
        duplicate_action = menu.addAction(
            I18N.tr("edit.clip.duplicate", "复制片段")
        )
        duplicate_action.setEnabled(not self._is_track_locked)

        # Delete action
        delete_action = menu.addAction(
            I18N.tr("edit.clip.delete", "Delete Clip")
        )

        # Split action
        split_action = menu.addAction(
            I18N.tr("edit.clip.split", "Split at Playhead")
        )

        # Add Float/Dock option for timeline panel
        menu.addSeparator()
        from PyQt6.QtWidgets import QDockWidget
        timeline_dock = None
        # Find the dock widget containing this timeline
        parent_widget = self.scene().views()[0] if self.scene().views() else None
        if parent_widget:
            for p in [parent_widget.parent(), parent_widget.parent().parent() if parent_widget.parent() else None]:
                if isinstance(p, QDockWidget):
                    timeline_dock = p
                    break
            # Also check through parent chain
            if not timeline_dock:
                p = parent_widget.parent()
                while p:
                    if isinstance(p, QDockWidget):
                        timeline_dock = p
                        break
                    p = p.parent()

        if timeline_dock:
            is_floating = timeline_dock.isFloating()
            float_text = I18N.tr("dock.dock", "Dock") if is_floating else I18N.tr("dock.float", "Float")
            float_action = menu.addAction(float_text)

        # Execute menu
        action = menu.exec(screen_pos)

        # QMenu.exec() returns None when the menu is dismissed.  Optional
        # actions (audio transcription, scene cut, retime, composite entries)
        # are also represented by None when they are not applicable.  Never
        # compare those sentinels directly: ``None == None`` used to turn a
        # harmless right-click on a Motion Graphic into an audio request.
        def selected(candidate: Any) -> bool:
            return _is_menu_action_selected(action, candidate)

        # Handle Float/Dock action
        if timeline_dock and selected(float_action):
            timeline_dock.setFloating(not timeline_dock.isFloating())
            return  # Don't process other actions

        # Handle actions
        if self._media_type in ("video", "image", "comp_clip", "motion_graphic"):
            # Handle thumbnail mode change
            new_thumb_mode = None
            if selected(thumb_off_action):
                new_thumb_mode = "none"
            elif selected(thumb_start_end_action):
                new_thumb_mode = "start_end"
            elif selected(thumb_filmstrip_action):
                new_thumb_mode = "filmstrip"
            if new_thumb_mode and new_thumb_mode != ClipItem._thumbnail_display_mode:
                ClipItem._thumbnail_display_mode = new_thumb_mode
                # Clear thumbnail cache so clips re-render with new mode
                ClipItem._thumbnail_cache.clear()
                # Trigger repaint of all clip items in the scene
                if self.scene():
                    for item in self.scene().items():
                        if isinstance(item, ClipItem):
                            item._cached_thumbnail_set = None
                            item._thumbnail_request_key = None
                            item._thumbnail_pending = False
                            item.update()
                return

        # Audio waveform display mode. The actual change goes through the
        # timeline command path so undo/redo and project persistence remain
        # consistent with the rest of the clip state.
        # （AUD-21101: Audio Gain 动作已随波形菜单在 menu.exec() 之前添加。）
        if self._media_type == "audio":
            show_channels = None
            if selected(waveform_mono_action):
                show_channels = False
            elif selected(waveform_stereo_action):
                show_channels = True
            if (
                show_channels is not None
                and show_channels != self._waveform_show_channels
            ):
                self.waveform_display_mode_requested.emit(
                    self._clip_id,
                    self._track_id,
                    show_channels,
                )
                return

        # P2: Handle label color change
        if action in label_color_actions:
            color = label_color_actions[action]
            self.label_color_change_requested.emit(self._clip_id, self._track_id, color)
            return

        # Handle Enable/Disable toggle
        if selected(enable_action):
            self.clip_enable_toggle_requested.emit(self._clip_id, self._track_id)
            return

        # Handle Link/Unlink
        if has_link and selected(unlink_action):
            self.clip_unlink_requested.emit(self._clip_id, self._track_id)
            return
        if selected(link_action):
            self.clip_link_requested.emit(self._clip_id, self._track_id)
            return
        if selected(numeric_trim_action):
            self.numeric_trim_requested.emit(self._clip_id, self._track_id)
            return
        if enter_nested_action is not None and selected(enter_nested_action):
            self.enter_nested_sequence_requested.emit(self._clip_id, self._track_id)
            return
        if create_nested_action is not None and selected(create_nested_action):
            self.create_nested_from_selection_requested.emit()
            return

        # Check speed actions
        if selected(retime_toggle_action):
            self.retime_controls_toggled.emit(
                self._clip_id, self._track_id, retime_toggle_action.isChecked()
            )
            return
        if selected(add_speed_point_action):
            self.retime_speed_point_requested.emit(self._clip_id, self._track_id)
            return
        if selected(clear_speed_points_action):
            self.retime_speed_points_clear_requested.emit(self._clip_id, self._track_id)
            return
        if audio_gain_action is not None and selected(audio_gain_action):
            self.audio_gain_dialog_requested.emit(self._clip_id, self._track_id)
            return
        if replace_placeholder_action is not None and selected(replace_placeholder_action):
            self.replace_placeholder_requested.emit(self._clip_id, self._track_id)
            return
        if freeze_action is not None and selected(freeze_action):
            # 分割点 = 当前时间线播放头（该 action 仅在播放头位于片段内时可用）
            _view = self.scene().views()[0] if self.scene() and self.scene().views() else None
            playhead = int(getattr(_view, "_playhead_frame",
                                   self._start_frame + self._duration_frames // 2))
            self.freeze_frame_requested.emit(self._clip_id, self._track_id, playhead)
            return
        for speed_val, speed_act in speed_actions.items():
            if selected(speed_act):
                self.speed_change_requested.emit(self._clip_id, self._track_id, speed_val)
                return

        if selected(delete_action):
            self.clip_delete.emit(self._clip_id, self._track_id)
        elif selected(render_cache_media_action) and not self._is_track_locked:
            self.render_cache_clip_requested.emit(self._clip_id, self._track_id)
        elif selected(rerender_cache_action) and not self._is_track_locked:
            self.rerender_cache_clip_requested.emit(self._clip_id, self._track_id)
        elif selected(speech_to_text_action) and not self._is_track_locked:
            self.speech_to_text_requested.emit(
                self._clip_id, self._track_id, self._asset_id or ""
            )
        elif selected(duplicate_action) and not self._is_track_locked:
            self.clip_duplicate.emit(self._clip_id, self._track_id)
        elif selected(split_action):
            # Split at the actual timeline playhead.  Fall back to the middle
            # only for standalone ClipItem hosts that do not expose a view.
            playhead = getattr(timeline_view, "_playhead_frame", None)
            try:
                split_frame = int(playhead)
            except (TypeError, ValueError):
                split_frame = self._start_frame + self._duration_frames // 2
            self.clip_split.emit(self._clip_id, self._track_id, split_frame)
        elif self._media_type in ("video", "image", "comp_clip", "motion_graphic") and selected(open_in_composite_action):
            # P9.2: Handle "Open in Composite"
            self.open_in_composite_requested.emit(self._clip_id, self._track_id)
        elif self._media_type in ("video", "image", "comp_clip", "motion_graphic"):
            # Phase 2: Handle Composite submenu actions
            if selected(comp_convert_action):
                self.convert_to_comp_clip_requested.emit(self._clip_id, self._track_id)
            elif selected(comp_bypass_action):
                self.bypass_composite_requested.emit(self._clip_id, self._track_id)
            elif selected(comp_reset_action):
                self.reset_composite_requested.emit(self._clip_id, self._track_id)
            elif selected(comp_remove_binding_action):
                self.remove_composite_binding_requested.emit(self._clip_id, self._track_id)
            elif selected(comp_reveal_action):
                self.reveal_comp_clip_in_media_pool_requested.emit(self._clip_id, self._track_id)
            elif selected(comp_render_cache_action):
                self.render_cache_clip_requested.emit(self._clip_id, self._track_id)
            # P9.1: Handle Grade actions
            elif selected(use_shared_action) and not self._is_track_locked:
                self.use_shared_grade_requested.emit(self._clip_id, self._track_id)
            elif selected(make_local_action) and not self._is_track_locked:
                self.make_local_grade_copy_requested.emit(self._clip_id, self._track_id)
            elif selected(open_in_color_action) and not self._is_track_locked:
                # Open current stack in Color page
                stack_id = self._color_stack_id or ""
                self.open_in_color_requested.emit(self._clip_id, self._track_id, stack_id)
        elif scene_cut_detect_action is not None and action == scene_cut_detect_action and not self._is_track_locked:
            # Emit signal with clip_id, track_id, and asset_id.
            self.scene_cut_detect_requested.emit(
                self._clip_id, self._track_id, self._asset_id or ""
            )
        elif is_near_right_edge and not self._is_track_locked:
            if selected(add_transition_after):
                self.add_transition_requested.emit(
                    self._clip_id, self._track_id, "right", "cross_dissolve")
            elif selected(add_transition_after_dip):
                self.add_transition_requested.emit(
                    self._clip_id, self._track_id, "right", "dip_to_color")
        elif is_near_left_edge and not self._is_track_locked:
            # E10: "Add Transition Before..." 此前只建菜单不处理结果，
            # 点击无效；补上左侧转场请求。
            if selected(add_transition_before):
                self.add_transition_requested.emit(
                    self._clip_id, self._track_id, "left", "cross_dissolve")

    def _show_retime_segment_speed_menu(self, screen_pos: QPointF, x: float) -> None:
        """Open the Resolve-style percentage menu for the clicked segment."""
        from PyQt6.QtWidgets import QMenu

        from src.shared.i18n.i18n import I18N

        local_timeline_frame = max(
            0,
            min(self._duration_frames - 1, int(x / max(self._width, 1.0) * self._duration_frames)),
        )
        segment_offset = 0
        for source_start, source_end, speed, timeline_start in self._retime_segments():
            segment_duration = max(1, round((source_end - source_start) / speed))
            if timeline_start <= local_timeline_frame < timeline_start + segment_duration:
                segment_offset = source_start
                break

        menu = QMenu()
        actions: dict[Any, float] = {}
        for speed in (0.10, 0.25, 0.50, 0.75, 1.0, 1.10, 1.50, 2.0, 4.0, 8.0):
            action = menu.addAction(f"{speed * 100.0:.0f}%")
            actions[action] = speed

        # A3.1: Smooth Ramp 切换（本段起点速度点的 easing）
        current_easing = "linear"
        for offset, _speed, easing in getattr(self, "_retime_point_easings", ()) or ():
            if offset == segment_offset:
                current_easing = easing
                break
        menu.addSeparator()
        smooth_action = menu.addAction(
            I18N.tr("edit.clip.smooth_ramp", "Smooth Ramp"))
        smooth_action.setCheckable(True)
        smooth_action.setChecked(current_easing == "smooth")

        action = menu.exec(screen_pos)
        if action in actions:
            self.retime_segment_speed_requested.emit(
                self._clip_id, self._track_id, segment_offset, actions[action]
            )
            return
        if action is smooth_action:
            new_easing = "linear" if current_easing == "smooth" else "smooth"
            self.retime_point_easing_requested.emit(
                self._clip_id, self._track_id, segment_offset, new_easing
            )

    def mouseMoveEvent(self, event: Any) -> None:
        """Handle mouse move for dragging.

        P2-6: Enforces a drag threshold before entering drag mode.
        Mouse must move > DRAG_THRESHOLD_PX pixels before drag signals are emitted.
        """
        if self._drag_pending and not self._is_dragging:
            # P2-6: Check if threshold has been passed
            delta = event.scenePos() - self._drag_start_pos
            dist = math.sqrt(delta.x() * delta.x() + delta.y() * delta.y())
            if dist < DRAG_THRESHOLD_PX:
                # Below threshold, don't start drag yet
                super().mouseMoveEvent(event)
                return
            # Threshold passed: enter drag mode
            self._is_dragging = True
            self._drag_pending = False
            self.setZValue(200)  # Above track headers during drag
            # P2-7: Update cursor to closed hand for move mode
            if self._drag_mode == "move":
                self.setCursor(Qt.CursorShape.ClosedHandCursor)

        if self._is_dragging:
            delta = event.scenePos() - self._drag_start_pos
            delta_frames = int(delta.x() / self._pixels_per_frame)
            scene_y = event.scenePos().y()  # Track Y for cross-track drag detection

            if self._drag_mode == "move":
                new_start = self._drag_start_frame + delta_frames
                # Don't update geometry here - let the view handler apply snap
                # and update the final position (avoids snap flicker)
                emit_key = (new_start, int(scene_y))
                if emit_key != self._last_move_emit:
                    self._last_move_emit = emit_key
                    self.clip_moved.emit(self._clip_id, self._track_id, new_start, scene_y)

            elif self._drag_mode == "fade_in":
                # Phase 1.2: Adjust fade in duration
                # Positive delta increases fade (drag right), negative decreases (drag left)
                max_fade_in = max(0, self._duration_frames - self._fade_out_duration)
                new_fade_in = max(0, min(
                    self._original_fade_in + delta_frames,
                    max_fade_in,
                ))
                # Mouse events can arrive several times for the same frame.
                # Avoid emitting/repainting when the visible value did not
                # change; this is especially noticeable at high zoom levels.
                if new_fade_in != self._fade_in_duration:
                    self._fade_in_duration = new_fade_in
                    self.fade_in_changed.emit(self._clip_id, self._track_id, new_fade_in)
                    self.update()  # Repaint to show new fade overlay

            elif self._drag_mode == "fade_out":
                # Phase 1.2: Adjust fade out duration
                # Negative delta increases fade (drag left), positive decreases (drag right)
                max_fade_out = max(0, self._duration_frames - self._fade_in_duration)
                new_fade_out = max(0, min(
                    self._original_fade_out - delta_frames,
                    max_fade_out,
                ))
                if new_fade_out != self._fade_out_duration:
                    self._fade_out_duration = new_fade_out
                    self.fade_out_changed.emit(self._clip_id, self._track_id, new_fade_out)
                    self.update()  # Repaint to show new fade overlay

            elif self._drag_mode == "slip":
                # The view owns the common linked bounds and final preview value.
                self.clip_slip.emit(self._clip_id, self._track_id, delta_frames)

            elif self._drag_mode == "slide":
                self.clip_slide.emit(self._clip_id, self._track_id, delta_frames)

            elif self._drag_mode == "ripple_trim_left":
                # Ripple trim left (P4.2): adjust start, emit ripple signal
                new_start = self._drag_start_frame + delta_frames
                # Clamp to prevent negative start
                if new_start >= 0 and new_start < self._start_frame + self._duration_frames:
                    self._start_frame = new_start
                    # Duration changes as well for visual preview
                    self._duration_frames = self._original_duration - (new_start - self._original_start)
                    self._update_geometry()
                    # Save final state for mouseRelease
                    self._final_new_start = new_start
                    self.ripple_trim_left.emit(self._clip_id, self._track_id, new_start)

            elif self._drag_mode == "ripple_trim_right":
                # Ripple trim right (P4.2): adjust end, emit ripple signal
                new_end = self._drag_start_frame + delta_frames
                # Clamp to prevent zero/negative duration
                if new_end > self._start_frame:
                    self._duration_frames = new_end - self._start_frame
                    self._update_geometry()
                    # Save final state for mouseRelease
                    self._final_new_end = new_end
                    self.ripple_trim_right.emit(self._clip_id, self._track_id, new_end)

            elif self._drag_mode == "trim_left":
                new_start = self._drag_start_frame + delta_frames
                new_duration = self._original_duration - delta_frames
                if new_duration > 0:
                    # Don't update geometry here - let the view handler apply snap
                    self.clip_trim_left.emit(self._clip_id, self._track_id, new_start)

            elif self._drag_mode == "trim_right":
                new_duration = self._drag_start_frame + delta_frames
                if new_duration > 0:
                    # Don't update geometry here - let the view handler apply snap
                    self.clip_trim_right.emit(self._clip_id, self._track_id, new_duration)

            elif self._drag_mode == "rate_stretch_left":
                # B4: live preview identical to trim (view snaps/repaints);
                # release converts the resulting duration into a speed change.
                new_start = self._drag_start_frame + delta_frames
                if new_start < self._start_frame + self._duration_frames:
                    self.clip_trim_left.emit(self._clip_id, self._track_id, new_start)

            elif self._drag_mode == "rate_stretch_right":
                new_end = self._drag_start_frame + delta_frames
                if new_end > self._start_frame:
                    self.clip_trim_right.emit(self._clip_id, self._track_id, new_end - self._start_frame)

            elif self._drag_mode == "motion_graphic_duration":
                new_duration = self._drag_start_frame + delta_frames
                if new_duration > 0:
                    # TimelineView applies the per-page minimum and updates
                    # geometry; this keeps the ClipItem independent of scene
                    # storage and makes the gesture testable in isolation.
                    self.motion_graphic_duration_changed.emit(
                        self._clip_id, self._track_id, new_duration
                    )

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        """Handle mouse release to finish interaction and emit command signals."""
        if event.button() == Qt.MouseButton.LeftButton:
            # IMPORTANT: Reset drag state BEFORE emitting signals.
            # _on_drag_finished -> _execute_command -> _layout_track_items needs
            # _is_dragging == False to correctly position the primary clip.
            # Without this, _layout_track_items skips the clip being dragged.
            was_dragging = self._is_dragging
            self._is_dragging = False
            self._drag_pending = False

            # Send finished signals for command execution
            if self._drag_mode == "move":
                # P5: Emit if position OR track changed (supports pure vertical drag)
                position_changed = self._start_frame != self._original_start
                track_changed = bool(self._drag_target_track_id) and self._drag_target_track_id != self._track_id
                # Alt-drag copy: always notify the view once a real drag
                # happened, even if it ended where it started, so ghost
                # previews and the drag session are cleaned up reliably.
                copy_drag_finished = self._copy_mode and was_dragging
                # Emit even when the clip returns to its original position so
                # TimelineView can release drag-only repaint/cache state.
                if position_changed or track_changed or copy_drag_finished or was_dragging:
                    # P5: Pass resolved target track_id if track changed during drag
                    new_track_id = self._drag_target_track_id if track_changed else self._track_id
                    self.drag_finished.emit(
                        self._clip_id,
                        self._track_id,
                        self._original_start,
                        self._start_frame,
                        new_track_id  # P5: May be a __new_* pseudo-track or existing track_id
                    )

            elif self._drag_mode == "fade_in":
                # Phase 1.2: Emit fade finished signal if fade changed
                if self._fade_in_duration != self._original_fade_in:
                    self.fade_finished.emit(
                        self._clip_id,
                        self._track_id,
                        self._original_fade_in,
                        self._original_fade_out,
                        self._fade_in_duration,
                        self._fade_out_duration
                    )

            elif self._drag_mode == "fade_out":
                # Phase 1.2: Emit fade finished signal if fade changed
                if self._fade_out_duration != self._original_fade_out:
                    self.fade_finished.emit(
                        self._clip_id,
                        self._track_id,
                        self._original_fade_in,
                        self._original_fade_out,
                        self._fade_in_duration,
                        self._fade_out_duration
                    )

            elif self._drag_mode == "slip":
                # Restore before the synchronous command signal; never overwrite
                # the newly committed source window after the slot returns.
                delta_frames = self._slip_delta
                self._source_start = self._original_source_start
                self.slip_finished.emit(
                    self._clip_id, self._track_id, self._original_source_start,
                    self._original_source_start + delta_frames, self._max_source_frames)

            elif self._drag_mode == "slide":
                delta_frames = self._start_frame - self._original_start
                self._start_frame = self._original_start
                self._update_geometry()
                self.slide_finished.emit(self._clip_id, self._track_id, delta_frames, "", "")

            elif self._drag_mode == "trim_left":
                # Only emit if trim actually changed
                if self._start_frame != self._original_start:
                    # Calculate source change
                    delta_frames = self._start_frame - self._original_start
                    new_source_start = self._original_source_start + delta_frames
                    self.trim_left_finished.emit(
                        self._clip_id,
                        self._track_id,
                        self._original_start,
                        self._original_duration,
                        self._original_source_start,
                        self._start_frame,
                        new_source_start
                    )

            elif self._drag_mode == "trim_right":
                # Only emit if trim actually changed
                if self._duration_frames != self._original_duration:
                    # Calculate source change
                    delta_frames = self._duration_frames - self._original_duration
                    new_source_duration = self._original_source_duration + delta_frames
                    self.trim_right_finished.emit(
                        self._clip_id,
                        self._track_id,
                        self._original_duration,
                        self._original_source_duration,
                        self._duration_frames,
                        new_source_duration
                    )

            elif self._drag_mode == "motion_graphic_duration":
                if self._duration_frames != self._original_duration:
                    self.motion_graphic_duration_change_finished.emit(
                        self._clip_id,
                        self._track_id,
                        self._original_duration,
                        self._duration_frames,
                    )

            elif self._drag_mode == "rate_stretch_left":
                if self._start_frame != self._original_start:
                    original_duration = self._original_duration
                    new_duration = (self._original_start + self._original_duration) - self._start_frame
                    # 还原几何与源区间——速度命令会从模型重建视觉；拖拽
                    # 预览改过 _source_start/_source_duration，命令失败
                    # （异常被 view 吞掉）时不还原会一直错位到下次全量
                    # reload。slip 分支同款处理。
                    self._start_frame = self._original_start
                    self._duration_frames = original_duration
                    self._source_start = self._original_source_start
                    self._source_duration = self._original_source_duration
                    self._update_geometry()
                    self.rate_stretch_finished.emit(
                        self._clip_id, self._track_id,
                        original_duration, new_duration,
                    )

            elif self._drag_mode == "rate_stretch_right":
                if self._duration_frames != self._original_duration:
                    new_duration = self._duration_frames
                    # 还原几何与源区间（同 rate_stretch_left 注释）。
                    self._start_frame = self._original_start
                    self._duration_frames = self._original_duration
                    self._source_start = self._original_source_start
                    self._source_duration = self._original_source_duration
                    self._update_geometry()
                    self.rate_stretch_finished.emit(
                        self._clip_id, self._track_id,
                        self._original_duration, new_duration,
                    )

            elif self._drag_mode == "ripple_trim_left":
                # Ripple trim left (P4.2): emit ripple trim finished signal
                if self._final_new_start != self._original_start:
                    # Restore original state before command execution
                    self._start_frame = self._original_start
                    self._duration_frames = self._original_duration
                    self._update_geometry()
                    # Emit signal with saved final position
                    self.ripple_trim_left_finished.emit(
                        self._clip_id,
                        self._track_id,
                        self._original_start,
                        self._final_new_start
                    )

            elif self._drag_mode == "ripple_trim_right":
                # Ripple trim right (P4.2): emit ripple trim finished signal
                original_end = self._original_start + self._original_duration
                if self._final_new_end != original_end:
                    # Restore original state before command execution
                    self._start_frame = self._original_start
                    self._duration_frames = self._original_duration
                    self._update_geometry()
                    self.ripple_trim_right_finished.emit(
                        self._clip_id,
                        self._track_id,
                        original_end,
                        self._final_new_end
                    )

            if self._drag_mode in {
                "trim_left",
                "trim_right",
                "motion_graphic_duration",
                "ripple_trim_left",
                "ripple_trim_right",
            }:
                self.end_trim_preview()

            self._drag_mode = None
            self._drag_target_track_id = ""  # P5: Reset track change tracking
            self._last_move_emit = None
            self._slip_delta = 0  # Reset slip delta
            self.setZValue(0)  # Reset to normal z-value
            self.update()
            # INT-02：正常 release 结束 view 侧 InteractionSession
            self.interaction_finished.emit(self._clip_id)

        super().mouseReleaseEvent(event)

    def cancel_active_drag(self) -> None:
        """INT-02：取消当前片段交互并完整恢复预览状态。

        由 TimelineView 在 Esc/换工具/失焦时调用。恢复所有 press 时保存
        的原始几何/源区间/fade，清除拖拽标志（release 因 _drag_mode 为
        None 不再发任何 finished 命令信号），并结束修剪预览。view 负责
        恢复 linked/multi/ghost 等跨对象状态。
        """
        self._is_dragging = False
        self._drag_pending = False
        self._drag_target_track_id = ""
        self._last_move_emit = None
        self._slip_delta = 0
        self._copy_mode = False
        self._start_frame = self._original_start
        self._duration_frames = self._original_duration
        self._source_start = self._original_source_start
        self._source_duration = self._original_source_duration
        self._fade_in_duration = self._original_fade_in
        self._fade_out_duration = self._original_fade_out
        self._final_new_start = self._original_start
        self._final_new_end = self._original_start + self._original_duration
        if self._drag_mode in {
            "trim_left",
            "trim_right",
            "motion_graphic_duration",
            "ripple_trim_left",
            "ripple_trim_right",
        }:
            self.end_trim_preview()
        self._drag_mode = None
        self.setZValue(0)
        self._update_geometry()
        self.update()

    def mouseDoubleClickEvent(self, event: Any) -> None:
        """Handle double-click to open clip in source viewer."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clip_double_clicked.emit(self._clip_id, self._track_id)

        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        """Handle key press for deletion."""
        if event.key() == Qt.Key.Key_Delete:
            self.clip_delete.emit(self._clip_id, self._track_id)

        super().keyPressEvent(event)

    # ==================== State Updates ====================

    def set_selected(self, selected: bool) -> None:
        """Set selection state (external call)."""
        self._is_selected = selected
        self.setSelected(selected)
        self.update()

    def set_enabled(self, enabled: bool) -> None:
        """Set enabled state."""
        self._is_enabled = enabled
        self.update()

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale."""
        self._pixels_per_frame = pixels_per_frame
        self._update_geometry()
        self.update()

    def update_visibility(self, visible_start_frame: int, visible_end_frame: int) -> bool:
        """
        Update visibility state based on visible frame range.

        参考 kdenlive Clip.qml 的 visible 属性计算:
        - 片段与可见区域有交集才显示
        - 宽度过小时隐藏内部元素（波形、缩略图）

        Args:
            visible_start_frame: First visible frame in viewport
            visible_end_frame: Last visible frame in viewport

        Returns:
            True if clip should be visible, False otherwise
        """
        clip_start = self._start_frame
        clip_end = clip_start + self._duration_frames

        # 片段与可见区域有交集 (参考 kdenlive)
        # visible: scrollView.lastVisibleFrame > clipRoot.modelStart
        #          && scrollView.firstVisibleFrame <= (clipRoot.modelStart + clipRoot.clipDuration)
        is_visible = visible_end_frame > clip_start and visible_start_frame < clip_end

        # 宽度过小时隐藏内部元素（波形、缩略图）
        # hideClipViews: !visible || clipRoot.width < root.minClipWidthForViews
        clip_width = self._duration_frames * self._pixels_per_frame
        hide_details = clip_width < MIN_CLIP_WIDTH_FOR_DETAILS

        # 设置可见性状态
        self.setVisible(is_visible)
        self._hide_details = hide_details

        # 如果变为可见，触发重绘（波形可能需要加载）
        if is_visible:
            self.update()

        return is_visible

    def is_hide_details(self) -> bool:
        """Check if details (waveform/thumbnails) should be hidden."""
        return getattr(self, '_hide_details', False)

    def update_track_index(self, track_index: int) -> None:
        """Update track vertical position."""
        self._track_index = track_index
        self._update_geometry()
        self.update()

    def set_track_locked(self, locked: bool) -> None:
        """Set track lock state (external call)."""
        if self._is_track_locked != locked:
            self._is_track_locked = locked
            # No need to toggle ItemIsMovable — we never set it.
            # Drag prevention is handled in mousePressEvent by checking _is_track_locked.
            self.update()

    def set_track_height(self, height: int) -> None:
        """Set track height (external call)."""
        if self._track_height != height:
            self._track_height = height
            self._update_geometry()
            self.update()

    def set_right_seam_is_same_source(self, is_same_source: bool) -> None:
        """Set whether the cut on this clip's right shares its source asset."""
        if self._right_seam_is_same_source != is_same_source:
            self._right_seam_is_same_source = is_same_source
            self.update()

    def set_active_tool(self, tool: EditTool) -> None:
        """Set active editing tool (external call from TimelineView)."""
        self._active_tool = tool
        self.update()

    def update_source_range(self, source_start: int, source_duration: int) -> None:
        """Update source range (external call after slip command)."""
        self._source_start = source_start
        self._source_duration = source_duration
        # Invalidate waveform cache (source range changed)
        self._waveform_path_cache = None
        self._waveform_path_cache_key = None
        # Invalidate thumbnail cache
        self._cached_thumbnail_set = None
        self._thumbnail_request_key = None
        self.update()

    def begin_trim_preview(self) -> None:
        """Hold the current thumbnail set while a trim preview is active."""
        self._trim_preview_active = True
        self._trim_preview_source_start = self._source_start
        self._trim_preview_source_duration = self._source_duration

    def update_trim_preview_source_range(
        self,
        source_start: int,
        source_duration: int,
    ) -> None:
        """Update the visible source range without committing model state.

        The timeline view owns the live trim transaction.  This method only
        changes the visual range used by waveform/thumbnail painting; the
        command emitted on mouse release remains the source of truth.
        """
        source_start = max(0, int(source_start))
        source_duration = max(1, int(source_duration))
        if (
            self._source_start == source_start
            and self._source_duration == source_duration
        ):
            return
        self._source_start = source_start
        self._source_duration = source_duration
        self._waveform_path_cache = None
        self._waveform_path_cache_key = None
        self.update()

    def end_trim_preview(self) -> None:
        """Drop the temporary thumbnail range after a trim transaction."""
        if not self._trim_preview_active:
            return
        self._trim_preview_active = False
        self._trim_preview_source_start = self._source_start
        self._trim_preview_source_duration = self._source_duration
        # The held set belongs to the pre-trim range.  Let the next paint use
        # the exact post-trim cache key instead of showing stale material.
        self._cached_thumbnail_set = None
        self._thumbnail_request_key = None

    # ==================== Phase 7.2: Incremental Update ====================

    def update_clip_properties(self, clip: Any) -> None:
        """
        Update ClipItem properties without rebuilding (Phase 7.2).

        用于增量更新，避免全量重建导致的闪烁和性能问题。
        只更新变化的属性，保持 QGraphicsItem 对象不变。

        Args:
            clip: ClipInstance data model with new values

        更新的属性：
        - timeline_start: 新的起始帧位置
        - track_id: 新的轨道 ID
        - timeline_duration: 新的时长
        - source_start: 新的素材起始帧
        - source_duration: 新的素材时长
        - fade_in_duration: 淡入时长
        - fade_out_duration: 淡出时长
        - opacity: 透明度
        - speed: 播放速度
        - effects: 效果列表
        - linked_clip_id: 链接的 clip ID
        - color_stack_id: 调色栈引用 ID
        - color_shared: 是否共享素材调色
        - is_enabled: 是否启用
        """
        # Update position and duration
        self._track_id = clip.track_id
        self._start_frame = clip.timeline_start
        self._duration_frames = clip.timeline_duration

        # Update source range
        self._source_start = clip.source_start
        self._source_duration = clip.source_duration

        # Update fade in/out
        self._fade_in_duration = clip.fade_in_duration
        self._fade_out_duration = clip.fade_out_duration

        # Update visual properties
        self._opacity = clip.opacity if hasattr(clip, 'opacity') else 1.0

        # Handle speed (can be SpeedParams or float)
        if hasattr(clip, 'speed'):
            speed = clip.speed
            if isinstance(speed, (int, float)):
                self._speed = speed
            else:
                self._speed = getattr(speed, 'speed', 1.0)
            self._retime_points = tuple(
                (point.source_offset, point.speed)
                for point in getattr(speed, 'speed_points', ())
            )
            # A3.1: 每速度点的 easing（Smooth Ramp 菜单状态）
            self._retime_point_easings = tuple(
                (point.source_offset, point.speed, point.easing)
                for point in getattr(speed, 'speed_points', ())
            )

        # Update effects
        self._effects = clip.effects if hasattr(clip, 'effects') else ()

        # Update linked clip
        self._linked_clip_id = clip.linked_clip_id

        # Update color stack info
        self._color_stack_id = clip.color_stack_id if hasattr(clip, 'color_stack_id') else None
        self._color_shared = clip.color_shared if hasattr(clip, 'color_shared') else False

        # Update enabled state
        self._is_enabled = clip.is_enabled if hasattr(clip, 'is_enabled') else True

        # Update label color (P2)
        self._label_color = clip.label_color if hasattr(clip, 'label_color') else None
        self._waveform_show_channels = bool(
            getattr(clip, "waveform_show_channels", False)
        )
        # A2: 冻结帧状态（用于徽章绘制）
        self._freeze_source_frame = getattr(clip, "freeze_source_frame", None)
        # A1: 图层混合模式（用于徽章绘制）
        _bm = getattr(clip, "blend_mode", None)
        self._blend_mode = getattr(_bm, "value", "normal") or "normal"

        # Invalidate caches (source range or display size may have changed)
        self._waveform_path_cache = None
        self._waveform_path_cache_key = None
        self._cached_thumbnail_set = None
        self._thumbnail_request_key = None

        # 效果链/源区间可能变化：重新评估效果后波形（幂等，指纹或区间
        # 变化才会真正触发后台渲染）。
        self._maybe_request_effect_waveform()

        # Update geometry and repaint
        self._update_geometry()
        self.update()

    # ==================== Phase 6.1: Linked Group Highlight ====================

    def set_group_highlighted(self, highlighted: bool) -> None:
        """
        Set linked group highlight state (external call from TimelineView).

        参考 Kdenlive: hover 一个 clip 时，同组的所有 clip 边框同时高亮。

        Args:
            highlighted: True if this clip should be highlighted as part of linked group
        """
        if self._is_group_highlighted != highlighted:
            self._is_group_highlighted = highlighted
            self.update()

    def set_linked_to_selection(self, linked: bool) -> None:
        """Mark this clip as linked to the current selection (P2-03).

        Persistent counterpart of the hover-driven group highlight: the
        border glows for every member of a linked pair while either member
        is selected, so the user can see which clips an edit will carry
        before performing it.
        """
        if self._linked_to_selection != linked:
            self._linked_to_selection = linked
            self.update()

    def set_linked_group_color_index(self, color_index: int) -> None:
        """
        Set linked group color index (external call from TimelineView).

        为同一链接组分配相同的颜色索引。

        Args:
            color_index: Index into LINKED_GROUP_COLORS palette (0-7)
        """
        if self._linked_group_color_index != color_index:
            self._linked_group_color_index = color_index
            self.update()


# ================================================================
# TransitionItem
# ================================================================

class TransitionItem(QGraphicsObject):
    """
    Visual representation of a transition between clips.

    Signals:
        transition_clicked: Transition was clicked
        transition_delete: Delete requested
        transition_trim_left: Left edge trim during drag (live preview)
        transition_trim_right: Right edge trim during drag (live preview)
        trim_left_finished: Left trim completed (old_duration, new_duration)
        trim_right_finished: Right trim completed (old_duration, new_duration)

    Attributes:
        _transition_id: Unique transition identifier
        _track_id: Track containing transition
        _center_frame: Center frame of transition
        _duration_frames: Duration of transition
        _pixels_per_frame: Zoom scale
        _track_index: Track vertical position
        _is_selected: Selection state
        _hover_edge: Which edge is hovered (None, 'left', 'right')
    """

    transition_clicked = pyqtSignal(str, str)  # transition_id, track_id
    transition_delete = pyqtSignal(str, str)
    transition_trim_left = pyqtSignal(str, str, int)  # transition_id, track_id, new_duration (live preview)
    transition_trim_right = pyqtSignal(str, str, int)  # transition_id, track_id, new_duration (live preview)
    trim_left_finished = pyqtSignal(str, str, int, int)  # transition_id, track_id, old_duration, new_duration
    trim_right_finished = pyqtSignal(str, str, int, int)  # transition_id, track_id, old_duration, new_duration
    # Phase 4.2: 转场位置移动信号
    transition_moved = pyqtSignal(str, str, int)  # transition_id, track_id, new_center_frame (live preview)
    move_finished = pyqtSignal(str, str, int, int)  # transition_id, track_id, old_center_frame, new_center_frame

    def __init__(
        self,
        transition_id: str,
        track_id: str,
        center_frame: int,
        duration_frames: int,
        pixels_per_frame: float,
        track_index: int,
        transition_type: str = "cross_dissolve",
        alignment: str = "centered",
        is_track_locked: bool = False,
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._transition_id = transition_id
        self._track_id = track_id
        self._center_frame = center_frame
        self._duration_frames = duration_frames
        self._pixels_per_frame = pixels_per_frame
        self._track_index = track_index
        self._track_height = TRACK_HEIGHT
        self._is_selected = False
        self._hover_edge: Optional[str] = None  # 'left', 'right', 'center', or None
        # AUD-21102: 轨道锁状态——锁定轨道上的转场不再允许修剪/移动/删除
        # （与 ClipItem 的锁语义一致，此前 TransitionItem 完全绕过锁轨）。
        self._is_track_locked: bool = bool(is_track_locked)

        # Phase 4.1: 转场类型
        self._transition_type: str = transition_type
        self._alignment: str = alignment

        # Drag state
        self._is_dragging = False
        self._drag_start_pos = QPointF()
        self._drag_start_duration = 0
        self._drag_start_center = 0  # Phase 4.2: 用于移动转场位置
        self._drag_mode: Optional[str] = None  # 'trim_left', 'trim_right', 'move'
        self._move_min_center: Optional[int] = None
        self._move_max_center: Optional[int] = None

        # Original state storage (for undo)
        self._original_duration: int = 0
        self._original_center: int = 0  # Phase 4.2: 用于移动转场位置

        # Phase 4.2: 转场帧号显示
        self._drag_duration_label_visible: bool = False
        self._drag_duration_value: int = 0

        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        # Transition cards sit above the clip bodies so their translucent
        # surface and handles remain clickable without covering the ruler or
        # neighbouring audio track.
        self.setZValue(80)

        self._update_geometry()

    @property
    def transition_id(self) -> str:
        return self._transition_id

    @property
    def track_id(self) -> str:
        return self._track_id

    @property
    def transition_type(self) -> str:
        """Get transition type (Phase 4.1)."""
        return self._transition_type

    @property
    def start_frame(self) -> int:
        if self._alignment == "start_on_cut":
            return self._center_frame
        if self._alignment == "end_on_cut":
            return self._center_frame - self._duration_frames
        return self._center_frame - self._duration_frames // 2

    @property
    def end_frame(self) -> int:
        return self.start_frame + self._duration_frames

    def set_move_bounds(self, min_center: int, max_center: int) -> None:
        """Set the legal center-frame range for body dragging."""
        self._move_min_center = min(int(min_center), int(max_center))
        self._move_max_center = max(int(min_center), int(max_center))

    def set_track_locked(self, locked: bool) -> None:
        """Set track lock state (external call).

        AUD-21102: 与 ClipItem.set_track_locked 对齐——锁定轨道上的转场
        拒绝修剪/移动/删除交互；编辑防护在 mousePressEvent 内检查
        _is_track_locked。
        """
        locked = bool(locked)
        if self._is_track_locked != locked:
            self._is_track_locked = locked
            self.update()

    @property
    def duration_frames(self) -> int:
        return self._duration_frames

    def _update_geometry(self) -> None:
        """
        Update item geometry.

        NOTE: Y position is set by TimelineView._layout_track_items().
        This only updates X and width.
        """
        self.prepareGeometryChange()
        x = TRACK_HEADER_WIDTH + self.start_frame * self._pixels_per_frame
        width = self._duration_frames * self._pixels_per_frame

        # Only update X; Y is managed externally by _layout_track_items()
        self.setX(x)
        self._width = width

    def boundingRect(self) -> QRectF:
        """Return bounding rectangle."""
        # A small invalidation margin keeps the selected border and rounded
        # corners from being clipped during a drag.
        return QRectF(-1, 0, self._width + 2, max(16.0, float(self._track_height)))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """Paint the transition item with type-specific visuals (Phase 4.1)."""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Get type-specific base color (Phase 4.1). The transition surface is
        # intentionally translucent so the cut and underlying thumbnails stay
        # visible, matching the visual language of Resolve's transition card.
        type_color = TRANSITION_TYPE_COLORS.get(self._transition_type, CLIP_TRANSITION_COLOR)
        base_color = type_color if not self._is_selected else type_color.lighter(130)
        top_color = QColor(base_color)
        top_color.setAlpha(190 if self._is_selected else 145)
        bottom_color = QColor(base_color.darker(125))
        bottom_color.setAlpha(115 if self._is_selected else 85)

        # Selection border uses accent color
        if self._is_selected:
            border_color = ACCENT_COLOR
            border_width = CLIP_SELECTED_BORDER_WIDTH
        else:
            border_color = BORDER_COLOR
            border_width = 1

        border_color = QColor(border_color)
        border_color.setAlpha(235 if self._is_selected else 185)
        painter.setPen(QPen(border_color, border_width))

        # Draw as rounded rect with gradient suggestion
        card_height = max(16.0, float(self._track_height) - 1.0)
        card_rect = QRectF(0, 0, self._width, card_height)
        gradient = QLinearGradient(0, 0, 0, card_rect.height())
        gradient.setColorAt(0.0, top_color)
        gradient.setColorAt(1.0, bottom_color)
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(card_rect, 4, 4)

        # A subtle diagonal hatch communicates that this is a composited
        # region, while the thumbnails remain readable through it.
        hatch_color = QColor(255, 255, 255, 26 if not self._is_selected else 38)
        painter.setPen(QPen(hatch_color, 1))
        painter.save()
        painter.setClipRect(card_rect, Qt.ClipOperation.IntersectClip)
        hatch_span = int(card_rect.height())
        for i in range(-hatch_span, int(self._width) + hatch_span, 8):
            painter.drawLine(
                QPointF(float(i), card_rect.bottom()),
                QPointF(float(i + hatch_span), card_rect.top()),
            )
        painter.restore()

        # Draw the transition name and a compact type icon.
        self._draw_transition_type(painter, card_rect)

        # Always show a quiet pair of edge grips; hover/selection promotes
        # them to a high-contrast draggable affordance.
        handle_color = ACCENT_COLOR if self._is_selected else QColor(255, 210, 95)
        handle_color = QColor(handle_color)
        handle_color.setAlpha(235 if self._hover_edge or self._is_selected else 150)
        painter.setBrush(QBrush(handle_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(
            QRectF(0, 3, TRANSITION_EDGE_HANDLE_WIDTH, card_rect.height() - 6), 2, 2
        )
        painter.drawRoundedRect(
            QRectF(
                self._width - TRANSITION_EDGE_HANDLE_WIDTH,
                3,
                TRANSITION_EDGE_HANDLE_WIDTH,
                card_rect.height() - 6,
            ),
            2,
            2,
        )

        # Phase 4.2: Draw duration label if visible during drag
        if self._drag_duration_label_visible:
            self._draw_drag_duration_label(painter, card_rect)

        painter.restore()

    def set_track_height(self, height: int) -> None:
        """Match the transition card height to its owning video track."""
        new_height = max(16, int(height))
        if self._track_height == new_height:
            return
        self.prepareGeometryChange()
        self._track_height = new_height
        self.update()

    def _draw_transition_type(self, painter: QPainter, rect: QRectF) -> None:
        """
        Draw transition type icon/text (Phase 4.1).

        参考 Kdenlive: 不同转场类型有不同视觉呈现。
        - crossfade: ✕ 或 "X" 图标
        - dissolve: 渐变矩形
        - wipe: 斜箭头
        - fade: 渐变条

        Args:
            painter: QPainter instance
            rect: Transition bounding rectangle
        """
        # Only draw if width is sufficient for a readable label.
        if self._width < 24:
            return

        painter.save()

        label = TRANSITION_TYPE_LABELS.get(self._transition_type, "Transition")
        text_color = QColor(255, 255, 255, 235)
        painter.setPen(QPen(text_color))
        painter.setFont(QFont("Arial", 8, QFont.Weight.DemiBold))

        # Keep a compact icon on wider cards; narrow cards still display an
        # elided name instead of becoming a nameless yellow stripe.
        icon_width = 18 if self._width >= 72 else 0
        text_rect = QRectF(7 + icon_width, 0, max(8.0, self._width - 14 - icon_width), rect.height())
        metrics = painter.fontMetrics()
        text = metrics.elidedText(label, Qt.TextElideMode.ElideRight, max(8, int(text_rect.width())))
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            text,
        )

        draw_icon = icon_width > 0
        center_x = 11
        center_y = rect.height() / 2

        if draw_icon and self._transition_type == "cross_dissolve":
            # Draw "X" symbol for two-source blending.
            painter.setPen(QPen(QColor(255, 255, 255, 210), 1.5))
            size = 3
            painter.drawLine(int(center_x - size), int(center_y - size),
                            int(center_x + size), int(center_y + size))
            painter.drawLine(int(center_x - size), int(center_y + size),
                            int(center_x + size), int(center_y - size))

        elif draw_icon and self._transition_type == "wipe":
            # Draw the incoming wipe arrow.
            painter.setPen(QPen(QColor(255, 255, 255, 210), 1.5))
            # Arrow line
            painter.drawLine(int(center_x - 3), int(center_y + 2),
                            int(center_x + 3), int(center_y - 2))
            # Arrow head
            painter.drawLine(int(center_x + 3), int(center_y - 2),
                            int(center_x + 1), int(center_y - 2))
            painter.drawLine(int(center_x + 3), int(center_y - 2),
                            int(center_x + 3), int(center_y))

        elif draw_icon and self._transition_type == "dip_to_color":
            # Draw a small color-dip marker.
            painter.setBrush(QBrush(QColor(255, 255, 255, 210)))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawEllipse(QRectF(center_x - 3, center_y - 3, 6, 6))

        elif draw_icon and is_audio_transition(self._transition_type):
            # Audio crossfade: two intersecting arcs (outgoing + incoming),
            # visually distinct from the video dissolve "X".
            painter.setPen(QPen(QColor(255, 255, 255, 210), 1.4))
            # Outgoing (left→down)
            painter.drawLine(
                int(center_x - 4), int(center_y - 3),
                int(center_x), int(center_y + 3),
            )
            # Incoming (up→right)
            painter.drawLine(
                int(center_x), int(center_y - 3),
                int(center_x + 4), int(center_y + 3),
            )

        painter.restore()

    def _draw_drag_duration_label(self, painter: QPainter, rect: QRectF) -> None:
        """
        Draw duration label during drag (Phase 4.2).

        显示 "8 frames" 浮窗标签，实时更新。

        Args:
            painter: QPainter instance
            rect: Transition bounding rectangle
        """
        painter.save()

        # Label dimensions
        label_h = 16
        label_radius = 2

        # Format duration text
        duration_text = f"{self._drag_duration_value} frames"
        text_width = len(duration_text) * 6 + 8
        label_w = max(text_width, 40)

        # Position: above transition, centered
        label_x = (rect.width() - label_w) / 2
        label_y = -label_h - 2  # Above transition bar

        label_rect = QRectF(label_x, label_y, label_w, label_h)

        # Background: semi-transparent dark
        bg_color = QColor(30, 30, 35, 200)
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(label_rect, label_radius, label_radius)

        # Text: white
        painter.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        painter.setPen(QPen(QColor(255, 255, 255, 230)))

        text_rect = label_rect.adjusted(4, 2, -4, -2)
        painter.drawText(
            text_rect,
            Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            duration_text
        )

        painter.restore()

    def hoverMoveEvent(self, event: Any) -> None:
        """Handle hover move to detect edge proximity and set cursor (Phase 4.1)."""
        pos = event.pos()

        # AUD-21102: 锁定轨道上显示禁止光标，不进入修剪/移动 hover 态
        from src.shared.i18n.i18n import I18N

        if self._is_track_locked:
            self._hover_edge = None
            self.setToolTip(I18N.tr("timeline.transition.locked_tooltip", "Transition (Track Locked)"))
            self.setCursor(Qt.CursorShape.ForbiddenCursor)
            super().hoverMoveEvent(event)
            return

        # Check if near edges
        if pos.x() < HANDLE_SIZE:
            self._hover_edge = "left"
            self.setToolTip(I18N.tr("timeline.transition.trim_tooltip", "Trim transition duration (drag to adjust)"))
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif pos.x() > self._width - HANDLE_SIZE:
            self._hover_edge = "right"
            self.setToolTip(I18N.tr("timeline.transition.trim_tooltip", "Trim transition duration (drag to adjust)"))
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif self._width > HANDLE_SIZE * 2:
            self._hover_edge = "center"
            self.setToolTip(
                f"{TRANSITION_TYPE_LABELS.get(self._transition_type, 'Transition')} "
                "（拖动主体调整位置，至少一侧保持覆盖分割点；拖动边缘调整时长）"
            )
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self._hover_edge = None
            self.setToolTip(f"Transition (duration: {self._duration_frames} frames)")
            self.setCursor(Qt.CursorShape.ArrowCursor)

        self.update()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event: Any) -> None:
        """Handle hover leave."""
        self._hover_edge = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        """Handle mouse press for selection and trim/move start (Phase 4.2)."""
        # AUD-21102: 锁定轨道上只允许选中，不允许修剪/移动/删除（与
        # ClipItem.mousePressEvent 的锁守卫一致）。
        if self._is_track_locked:
            if event.button() in (
                Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton
            ):
                self._is_selected = True
                self.setSelected(True)
                self.transition_clicked.emit(self._transition_id, self._track_id)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.pos()

            # Store original state for undo
            self._original_duration = self._duration_frames
            self._original_center = self._center_frame  # Phase 4.2

            if pos.x() < HANDLE_SIZE:
                self._drag_mode = "trim_left"
                self._drag_start_duration = self._duration_frames
                self._is_dragging = True
                self._drag_start_pos = event.scenePos()
                self.setZValue(200)  # Above track headers during drag
                # Show duration label - Phase 4.2
                self._drag_duration_label_visible = True
                self._drag_duration_value = self._duration_frames
            elif pos.x() > self._width - HANDLE_SIZE:
                self._drag_mode = "trim_right"
                self._drag_start_duration = self._duration_frames
                self._is_dragging = True
                self._drag_start_pos = event.scenePos()
                self.setZValue(200)  # Above track headers during drag
                # Show duration label - Phase 4.2
                self._drag_duration_label_visible = True
                self._drag_duration_value = self._duration_frames
            else:
                self._drag_mode = "move"
                self._is_dragging = True
                self._drag_start_pos = event.scenePos()
                self._drag_start_center = self._center_frame
                self.setZValue(200)
                self._is_selected = True
                self.setSelected(True)
                self.transition_clicked.emit(self._transition_id, self._track_id)

        elif event.button() == Qt.MouseButton.RightButton:
            # Right-click selects the transition first, then exposes the
            # transition-specific context menu (including Delete Transition).
            self._is_selected = True
            self.setSelected(True)
            self.transition_clicked.emit(self._transition_id, self._track_id)
            self._show_transition_context_menu(event.screenPos())
            event.accept()
            return

        super().mousePressEvent(event)

    def _show_transition_context_menu(self, screen_pos: Any) -> None:
        """Show the context menu for this transition card."""
        from PyQt6.QtWidgets import QMenu
        from src.shared.i18n.i18n import I18N

        menu = QMenu()
        delete_action = menu.addAction(
            I18N.tr("edit.transition.delete", "删除转场")
        )
        selected = menu.exec(screen_pos)
        if selected == delete_action:
            self.transition_delete.emit(self._transition_id, self._track_id)

    def mouseMoveEvent(self, event: Any) -> None:
        """Handle mouse move for trim/move (Phase 4.2)."""
        if self._is_dragging:
            delta = event.scenePos() - self._drag_start_pos
            delta_frames = int(delta.x() / self._pixels_per_frame)

            if self._drag_mode == "trim_left":
                # Trim left: decreasing duration increases from left side
                new_duration = self._drag_start_duration - delta_frames
                if new_duration > 0:
                    self._duration_frames = new_duration
                    self._drag_duration_value = new_duration  # Phase 4.2
                    self._update_geometry()
                    self.transition_trim_left.emit(self._transition_id, self._track_id, new_duration)

            elif self._drag_mode == "trim_right":
                # Trim right: increasing duration extends to right
                new_duration = self._drag_start_duration + delta_frames
                if new_duration > 0:
                    self._duration_frames = new_duration
                    self._drag_duration_value = new_duration  # Phase 4.2
                    self._update_geometry()
                    self.transition_trim_right.emit(self._transition_id, self._track_id, new_duration)

            elif self._drag_mode == "move":
                # Move transition position - Phase 4.2
                new_center = self._drag_start_center + delta_frames
                if self._move_min_center is not None:
                    new_center = max(self._move_min_center, new_center)
                if self._move_max_center is not None:
                    new_center = min(self._move_max_center, new_center)
                if new_center >= 0:
                    self._center_frame = new_center
                    self._update_geometry()
                    self.transition_moved.emit(self._transition_id, self._track_id, new_center)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        """Handle mouse release to finish trim/move (Phase 4.2)."""
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag_mode == "trim_left":
                if self._duration_frames != self._original_duration:
                    self.trim_left_finished.emit(
                        self._transition_id,
                        self._track_id,
                        self._original_duration,
                        self._duration_frames
                    )

            elif self._drag_mode == "trim_right":
                if self._duration_frames != self._original_duration:
                    self.trim_right_finished.emit(
                        self._transition_id,
                        self._track_id,
                        self._original_duration,
                        self._duration_frames
                    )

            elif self._drag_mode == "move":
                # Phase 4.2: emit move finished signal
                if self._center_frame != self._original_center:
                    self.move_finished.emit(
                        self._transition_id,
                        self._track_id,
                        self._original_center,
                        self._center_frame
                    )

            # Reset drag state
            self._is_dragging = False
            self._drag_mode = None
            self._drag_duration_label_visible = False  # Phase 4.2
            self.setZValue(80)  # Reset to normal z-value
            self.update()

        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        """Handle key press for deletion."""
        if event.key() == Qt.Key.Key_Delete:
            self.transition_delete.emit(self._transition_id, self._track_id)

        super().keyPressEvent(event)

    def set_selected(self, selected: bool) -> None:
        """Set selection state."""
        self._is_selected = selected
        self.setSelected(selected)
        self.update()

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale."""
        self._pixels_per_frame = pixels_per_frame
        self._update_geometry()
        self.update()

    def update_visibility(self, visible_start_frame: int, visible_end_frame: int) -> bool:
        """
        Update visibility state based on visible frame range.

        Args:
            visible_start_frame: First visible frame in viewport
            visible_end_frame: Last visible frame in viewport

        Returns:
            True if transition should be visible, False otherwise
        """
        trans_start = self.start_frame
        trans_end = self.end_frame

        # 片段与可见区域有交集
        is_visible = visible_end_frame > trans_start and visible_start_frame < trans_end

        self.setVisible(is_visible)
        if is_visible:
            self.update()

        return is_visible

    def update_track_index(self, track_index: int) -> None:
        """Update track vertical position."""
        self._track_index = track_index
        self._update_geometry()
        self.update()

    def update_transition_properties(self, trans: Any) -> None:
        """
        Update TransitionItem properties without rebuilding (Phase 7.2).

        用于增量更新，避免全量重建导致的闪烁和性能问题。

        Args:
            trans: TransitionInstance data model with new values

        更新的属性：
        - center_frame: 中心帧位置
        - duration: 持续帧数
        - track_id: 轨道 ID
        - transition_type: 转场类型
        """
        # Update center frame
        self._center_frame = trans.center_frame

        # Update duration
        self._duration_frames = trans.duration

        # Update track ID if changed (rare but possible)
        if hasattr(trans, 'track_id'):
            self._track_id = trans.track_id

        # Update transition type if changed
        if hasattr(trans, 'transition_type'):
            self._transition_type = trans.transition_type
        if hasattr(trans, 'alignment'):
            self._alignment = trans.alignment

        # Update geometry and repaint
        self._update_geometry()
        self.update()


# ================================================================
# PlayheadItem (P1.4 - Enhanced with triangle head)
# ================================================================

class PlayheadItem(QGraphicsObject):
    """
    Visual representation of the playhead (current frame position) (P1.4).

    Features:
    - Triangle head at top (red #ff4d4d)
    - Vertical line spanning all tracks
    - Floating timecode label during drag

    Signals are handled by TimelineView, not this item directly.

    Attributes:
        _frame: Current playhead frame
        _pixels_per_frame: Zoom scale
        _num_tracks: Number of tracks to span
        _fps: Frames per second (for timecode formatting)
        _is_dragging: Whether user is dragging the playhead
    """

    def __init__(
        self,
        frame: int,
        pixels_per_frame: float,
        num_tracks: int,
        fps: float = 24.0,
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._frame = frame
        self._pixels_per_frame = pixels_per_frame
        self._num_tracks = num_tracks
        self._fps = fps
        self._is_dragging = False
        # Actual cumulative track area height (set by TimelineView)
        self._track_area_height: Optional[float] = None

        self._update_position()

        # Z-value to be on top of clips
        self.setZValue(1000)

    @property
    def frame(self) -> int:
        return self._frame

    # ==================== Geometry ====================

    def _effective_track_height(self) -> float:
        """Return the actual track area height if set, else estimate from num_tracks."""
        if self._track_area_height is not None:
            return self._track_area_height
        return self._num_tracks * (TRACK_HEIGHT + TRACK_GAP) + TRACK_HEIGHT

    def _update_position(self) -> None:
        """
        Update position based on frame.

        Gemini suggestion: 使用统一的坐标管理器。
        """
        x = TRACK_HEADER_WIDTH + self._frame * self._pixels_per_frame
        # Y may be pinned by TimelineView so the playhead head stays visible
        # during vertical scrolling.
        self.setPos(x, self.y())

    def boundingRect(self) -> QRectF:
        """Return bounding rectangle including triangle head and track area."""
        head_width = 12
        # Use actual cumulative track height when available
        track_height = self._effective_track_height()
        line_height = RULER_HEIGHT + track_height

        # Triangle extends left from playhead position
        return QRectF(-head_width / 2, 0, head_width, line_height)

    def shape(self) -> QPainterPath:
        """Return shape for collision detection."""
        path = QPainterPath()
        head_height = 10
        path.addPath(self._head_path())

        # Keep the stem connected to the triangle tip.  The ruler is visually
        # transparent to the playhead, so a gap between the head and line is
        # both unnecessary and noticeably less precise.
        track_height = self._effective_track_height()
        path.addRect(-PLAYHEAD_LINE_WIDTH / 2, head_height, PLAYHEAD_LINE_WIDTH, track_height + RULER_HEIGHT - head_height)

        return path

    @staticmethod
    def _head_path() -> QPainterPath:
        """Build the compact, softly-cornered playhead pointer silhouette."""
        path = QPainterPath()
        path.moveTo(-4.5, 0.0)
        path.quadTo(-6.0, 0.0, -6.0, 1.5)
        path.lineTo(-6.0, 2.5)
        path.lineTo(0.0, 10.0)
        path.lineTo(6.0, 2.5)
        path.lineTo(6.0, 1.5)
        path.quadTo(6.0, 0.0, 4.5, 0.0)
        path.closeSubpath()
        return path

    # ==================== Painting ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """Paint the playhead with DaVinci Resolve-style professional look."""
        head_height = 10
        track_height = self._effective_track_height()
        line_height = int(RULER_HEIGHT + track_height)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # A one-device-pixel cosmetic stem stays sharp regardless of monitor
        # scaling or timeline zoom.  It begins inside the pointer tip so the
        # cursor reads as a single, continuous instrument marker.
        stem_pen = QPen(QColor(0xFF, 0x3E, 0x46), PLAYHEAD_LINE_WIDTH)
        stem_pen.setCosmetic(True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(stem_pen)
        painter.drawLine(QPointF(0.5, head_height - 1.0), QPointF(0.5, line_height))

        # One unified silhouette avoids the thick cap + overlapping triangle
        # that made the previous marker look bulky at small panel heights.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        head_gradient = QLinearGradient(0.0, 0.0, 0.0, head_height)
        head_gradient.setColorAt(0.0, QColor(0xFF, 0x5A, 0x60))
        head_gradient.setColorAt(1.0, QColor(0xF2, 0x2F, 0x38))
        painter.setBrush(QBrush(head_gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(self._head_path())

        # A restrained top glint gives the handle definition without a dark
        # outline that would turn into a jagged edge on high-DPI displays.
        painter.setPen(QPen(QColor(255, 255, 255, 100), 1))
        painter.drawLine(QPointF(-4.0, 0.75), QPointF(4.0, 0.75))
        painter.restore()

        # Draw floating timecode label during drag
        if self._is_dragging:
            self._draw_timecode_label(painter)

    def _draw_timecode_label(self, painter: QPainter) -> None:
        """Draw floating timecode label during drag (P1.4)."""
        # Format timecode HH:MM:SS:FF
        total_seconds = self._frame / self._fps
        hours = int(total_seconds / 3600)
        minutes = int((total_seconds % 3600) / 60)
        seconds = int(total_seconds % 60)
        frames = self._frame % int(self._fps)

        if hours > 0:
            timecode = f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"
        elif minutes > 0:
            timecode = f"{minutes:02d}:{seconds:02d}:{frames:02d}"
        else:
            timecode = f"{seconds:02d}:{frames:02d}"

        # Draw label above playhead head
        label_rect = QRectF(-40, -25, 80, 20)
        painter.setBrush(QBrush(QColor(40, 40, 45, 200)))
        painter.setPen(QPen(BORDER_COLOR, 1))
        painter.drawRoundedRect(label_rect, 4, 4)

        painter.setPen(QPen(PLAYHEAD_COLOR))
        font = QFont("Arial", 9, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, timecode)

    # ==================== State Updates ====================

    def set_frame(self, frame: int) -> None:
        """Set playhead frame position."""
        self._frame = frame
        self._update_position()
        self.update()

    def set_dragging(self, dragging: bool) -> None:
        """Set dragging state for timecode label display."""
        self._is_dragging = dragging
        self.update()

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale."""
        self._pixels_per_frame = pixels_per_frame
        self._update_position()
        self.update()

    def update_num_tracks(self, num_tracks: int) -> None:
        """Update number of tracks."""
        self._num_tracks = num_tracks
        self.update()

    def update_track_area_height(self, height: float) -> None:
        """Update the actual cumulative track area height from the layout."""
        if self._track_area_height != height:
            self.prepareGeometryChange()
            self._track_area_height = height
            self.update()

    def update_fps(self, fps: float) -> None:
        """Update frames per second."""
        self._fps = fps
        self.update()


# ================================================================
# TrackHeaderItem (Enhanced - kdenlive style)
# ================================================================

class TrackHeaderItem(QGraphicsObject):
    """
    Visual representation of track header with full track management controls.

    Enhanced features (kdenlive-style):
    - Track type badge (V1/V2/V3 or A1/A2/A3) with colored background
    - SVG icon buttons (mute/hidden, solo, lock) - fallback to text
    - Double-click to rename track
    - Target track indicator (active track border)
    - Height resize handle at bottom
    - Right-click context menu

    Signals:
        mute_changed: Mute state changed (track_id, mute)
        solo_changed: Solo state changed (track_id, solo)
        lock_changed: Lock state changed (track_id, lock)
        volume_changed: Volume changed (track_id, volume)
        rename_requested: Rename requested (track_id)
        delete_requested: Delete requested (track_id)
        add_track_requested: Add track requested (track_type, position, track_index)
        move_track_requested: Move track requested (track_id, direction)
        height_changed: Track height changed (track_id, new_height)
        target_track_changed: Target track changed (track_id)

    Attributes:
        _track_id: Track identifier
        _track_name: Display name
        _track_index: Vertical position index
        _track_type: 'video' or 'audio'
        _is_muted: Mute state
        _is_solo: Solo state
        _is_locked: Lock state
        _is_target: Target track state (active for editing)
        _volume: Volume level (0.0-2.0)
        _track_height: Current track height
        _hover_button: Which button is hovered
        _is_resizing: Whether height resize is active
        _is_dragging_volume: Whether volume slider is being dragged
        _icons: Cached SVG icons
    """

    # Signals
    mute_changed = pyqtSignal(str, bool)  # track_id, mute
    solo_changed = pyqtSignal(str, bool)  # track_id, solo
    lock_changed = pyqtSignal(str, bool)  # track_id, lock
    sync_lock_changed = pyqtSignal(str, bool)  # track_id, sync_lock (P1-05)
    visibility_changed = pyqtSignal(str, bool)  # track_id, visible
    volume_changed = pyqtSignal(str, float)  # track_id, volume
    rename_requested = pyqtSignal(str)  # track_id
    delete_requested = pyqtSignal(str)  # track_id
    add_track_requested = pyqtSignal(str, str, int)  # track_type, position ('above'/'below'), track_index
    move_track_requested = pyqtSignal(str, str)  # track_id, direction ('up'/'down')
    height_changed = pyqtSignal(str, int)  # track_id, new_height
    height_preset_requested = pyqtSignal(str)  # preset name: 'small', 'medium', 'large', 'default'
    target_track_changed = pyqtSignal(str)  # track_id (set as target)
    # E22: track_double_clicked 已删除——双击重命名的活链路是紧随其后的
    # rename_requested（TimelineView._on_track_rename_requested）
    # P10.3: request to add a subtitle clip on this track (at playhead)
    add_subtitle_clip_requested = pyqtSignal(str)  # track_id
    # AI voice-over: synthesize every cue on this subtitle track.
    subtitle_to_speech_requested = pyqtSignal(str)  # track_id
    # Export the complete subtitle track in a standard interchange format.
    subtitle_export_requested = pyqtSignal(str, str)  # track_id, format
    subtitle_quality_requested = pyqtSignal(str)  # track_id
    subtitle_language_requested = pyqtSignal(str)  # track_id
    caption_track_graphic_template_requested = pyqtSignal(str, str)  # track_id, template_id (empty removes)

    # Icon paths (static class attribute)
    _PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
    ICON_DIR = _PROJECT_ROOT + "/src/shared/assets/icons/video-editing-icons"

    def __init__(
        self,
        track_id: str,
        track_name: str,
        track_index: int,
        track_type: str,
        track_height: int = TRACK_HEIGHT,
        is_muted: bool = False,
        is_solo: bool = False,
        is_locked: bool = False,
        is_sync_locked: bool = True,  # P1-05: 波纹同步锁
        is_visible: bool = True,
        is_target: bool = False,  # NEW: Target track indicator
        volume: float = 1.0,  # Volume level (0.0-2.0)
        track_role: str = "standard",
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._track_id = track_id
        self._track_name = track_name
        self._track_index = track_index
        self._badge_index = track_index
        self._track_type = track_type
        self._track_role = track_role
        self._track_height = track_height
        self._is_muted = is_muted
        self._is_solo = is_solo
        self._is_locked = is_locked
        self._is_sync_locked = is_sync_locked
        self._is_visible = is_visible
        self._is_target = is_target  # NEW
        self._volume = max(0.0, min(2.0, volume))  # Clamp to valid range
        self._clip_count: int = 0  # Number of clips on this track (displayed in header)

        # Interaction state
        self._hover_button: Optional[str] = None  # 'mute', 'solo', 'lock', 'volume', 'resize', 'name', or None
        self._is_resizing: bool = False
        self._resize_start_y: int = 0
        self._resize_start_height: int = 0
        self._is_dragging_volume: bool = False

        # Phase 6.2: Clip hover highlight state (when a clip on this track is hovered)
        self._is_clip_hover_highlighted: bool = False

        # Whether this is the topmost track in its group (video or audio)
        self._is_topmost: bool = False

        # NEW: Load SVG icons (cached)
        self._icons: dict[str, Optional[QIcon]] = {}
        self._load_icons()

        # Enable hover and mouse events
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)

        # Z-value above clips so headers are always visible
        self.setZValue(100)

        # Update position
        self._update_position()

        logger.debug(f"TrackHeaderItem created: {track_id} ({track_name})")

    # ==================== Icon Loading (NEW) ====================

    def _load_icons(self) -> None:
        """Load SVG icons for buttons."""
        import os

        icon_files = {
            "speaker": os.path.join(self.ICON_DIR, "speaker-symbolic.svg"),
            "visible": os.path.join(self.ICON_DIR, "view-visible-symbolic.svg"),
            "visible_off": os.path.join(self.ICON_DIR, "view-visible-off-symbolic.svg"),
            "layer_visible": os.path.join(self.ICON_DIR, "layer-visible-on-symbolic.svg"),
            "layer_hidden": os.path.join(self.ICON_DIR, "layer-visible-off-symbolic.svg"),
        }

        for name, path in icon_files.items():
            if os.path.exists(path):
                self._icons[name] = QIcon(path)
            else:
                self._icons[name] = None
                logger.warning(f"Icon not found: {path}")

    # ==================== Properties ====================

    @property
    def track_id(self) -> str:
        return self._track_id

    @property
    def track_name(self) -> str:
        return self._track_name

    @property
    def track_index(self) -> int:
        return self._track_index

    @property
    def track_type(self) -> str:
        return self._track_type

    @property
    def track_height(self) -> int:
        return self._track_height

    @property
    def is_muted(self) -> bool:
        return self._is_muted

    @property
    def is_solo(self) -> bool:
        return self._is_solo

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    @property
    def is_target(self) -> bool:
        """NEW: Target track state (active for editing)."""
        return self._is_target

    @property
    def volume(self) -> float:
        return self._volume

    # ==================== Geometry ====================

    def _update_position(self) -> None:
        """
        Update item Y position based on track index and height.

        NOTE: X position is managed by TimelineView._pin_track_headers() to keep
        headers fixed on the left edge. This method only updates Y.
        """
        y = TRACK_AREA_TOP + self._track_index * (self._track_height + TRACK_GAP)
        self.setY(y)

    def boundingRect(self) -> QRectF:
        """Return bounding rectangle."""
        return QRectF(0, 0, TRACK_HEADER_WIDTH, self._track_height + TRACK_RESIZE_HANDLE_HEIGHT)

    def shape(self) -> QPainterPath:
        """Return shape for collision detection."""
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    # ==================== Button Areas (Enhanced) ====================

    def _get_track_badge_rect(self) -> QRectF:
        """Get track type badge rect (V1/A1).

        In two-row layout: top-left corner.
        In single-row layout: left side, vertically centered.
        """
        badge_w = EDIT_SIZES["track_badge_w"]   # 32
        badge_h = EDIT_SIZES["track_badge_h"]   # 20
        use_two_rows = self._track_height >= 40

        if use_two_rows:
            # DaVinci-like: leave room at left for track info text, then badge.
            return QRectF(54, 6, badge_w, badge_h)
        else:
            # Single row: vertically centered
            y = (self._track_height - badge_h) / 2
            return QRectF(6, y, badge_w, badge_h)

    def _get_name_rect(self) -> QRectF:
        """Get clickable area for name/badge (for target/double-click detection)."""
        use_two_rows = self._track_height >= 40
        badge_rect = self._get_track_badge_rect()

        if use_two_rows:
            # Track label area + badge both set target track.
            return QRectF(8, 4, max(88, badge_rect.right() - 8), self._track_height - 8)
        else:
            return badge_rect.adjusted(-2, -2, 2, 2)

    def _get_lock_button_rect(self) -> QRectF:
        """Get lock button bounding rect (leftmost of three buttons)."""
        use_two_rows = self._track_height >= 40
        if use_two_rows:
            y = 7
            x = self._get_track_badge_rect().right() + 10
        else:
            y = (self._track_height - BUTTON_SIZE) / 2
            x = TRACK_HEADER_WIDTH - 3 * BUTTON_SIZE - 3 * BUTTON_MARGIN
        return QRectF(
            x,
            y,
            BUTTON_SIZE,
            BUTTON_SIZE
        )

    def _get_solo_button_rect(self) -> QRectF:
        """Get solo button bounding rect (middle button)."""
        use_two_rows = self._track_height >= 40
        if use_two_rows:
            y = 7
            x = self._get_lock_button_rect().right() + BUTTON_MARGIN + 3
        else:
            y = (self._track_height - BUTTON_SIZE) / 2
            x = TRACK_HEADER_WIDTH - 2 * BUTTON_SIZE - 2 * BUTTON_MARGIN
        return QRectF(
            x,
            y,
            BUTTON_SIZE,
            BUTTON_SIZE
        )

    def _get_mute_button_rect(self) -> QRectF:
        """Get mute/hide button bounding rect (rightmost button)."""
        use_two_rows = self._track_height >= 40
        if use_two_rows:
            y = 7
            x = self._get_solo_button_rect().right() + BUTTON_MARGIN + 3
        else:
            y = (self._track_height - BUTTON_SIZE) / 2
            x = TRACK_HEADER_WIDTH - BUTTON_SIZE - BUTTON_MARGIN
        return QRectF(
            x,
            y,
            BUTTON_SIZE,
            BUTTON_SIZE
        )

    def _get_resize_handle_rect(self) -> QRectF:
        """Get resize handle bounding rect (at bottom of header)."""
        return QRectF(
            0,
            self._track_height,
            TRACK_HEADER_WIDTH,
            TRACK_RESIZE_HANDLE_HEIGHT
        )

    def _get_volume_slider_rect(self) -> QRectF:
        """Get volume slider bounding rect (only for audio tracks).

        In two-row layout: below the button row, if height allows.
        In single-row layout: below buttons.
        """
        use_two_rows = self._track_height >= 40
        if use_two_rows:
            slider_y = max(30, self._track_height - 14)
            slider_x = 112
            slider_width = TRACK_HEADER_WIDTH - slider_x - 10
        else:
            slider_y = BUTTON_MARGIN + BUTTON_SIZE + 2
            slider_x = BUTTON_MARGIN + 2
            slider_width = TRACK_HEADER_WIDTH - 3 * BUTTON_SIZE - 3 * BUTTON_MARGIN - 10
        slider_height = 10
        return QRectF(
            slider_x,
            slider_y,
            max(slider_width, 40),
            slider_height
        )

    def _get_button_at_pos(self, pos: QPointF) -> Optional[str]:
        """Determine which button is at the given position."""
        if self._track_type == "subtitle":
            if self._get_mute_button_rect().contains(pos):
                return "visibility"
            if self._get_lock_button_rect().contains(pos):
                return "lock"
            if self._get_resize_handle_rect().contains(pos):
                return "resize"
            if self._get_name_rect().contains(pos):
                return "name"
            return None
        # Video tracks use the eye glyph as a visibility control.  It used to
        # be painted as an eye but dispatched the audio mute command, which
        # made the control appear broken and left the clips visible.
        if self._track_type == "video" and self._get_mute_button_rect().contains(pos):
            return "visibility"
        if self._get_mute_button_rect().contains(pos):
            return "mute"
        if self._get_solo_button_rect().contains(pos):
            return "solo"
        if self._get_lock_button_rect().contains(pos):
            return "lock"
        if self._get_resize_handle_rect().contains(pos):
            return "resize"
        # NEW: Name area for double-click detection
        if self._get_name_rect().contains(pos):
            return "name"
        # Volume slider only for audio tracks
        if self._track_type == "audio" and self._get_volume_slider_rect().contains(pos):
            return "volume"
        return None

    # ==================== Painting (Enhanced - kdenlive style) ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """Paint the track header with DaVinci Resolve-style two-row layout."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._apply_track_area_clip(painter)

        use_two_rows = self._track_height >= 40

        # Background color
        bg_color = QColor(get_color("track_header_bg"))  # #1D1E24
        if self._is_locked:
            bg_color = QColor(0x1A, 0x1A, 0x1D)

        if self._is_clip_hover_highlighted:
            bg_color = bg_color.lighter(130)

        # Main background
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRect(QRectF(0, 0, TRACK_HEADER_WIDTH, self._track_height))

        # Subtle top-row band, like Resolve's control strip inside each header.
        if use_two_rows:
            painter.setBrush(QBrush(QColor(get_color("track_header_bg_alt"))))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawRect(QRectF(0, 0, TRACK_HEADER_WIDTH, min(32, self._track_height)))
            painter.setPen(QPen(QColor(255, 255, 255, 10), 1))
            painter.drawLine(QPointF(0, 31.5), QPointF(TRACK_HEADER_WIDTH, 31.5))

        # Bottom separator line (#0A0A0C)
        painter.setPen(QPen(QColor(get_color("track_header_divider")), 1))
        painter.drawLine(
            QPointF(0, self._track_height - 0.5),
            QPointF(TRACK_HEADER_WIDTH, self._track_height - 0.5)
        )

        # Strong right boundary: the timeline ruler/content starts immediately after this.
        painter.setPen(QPen(QColor("#0A0A0C"), 1))
        painter.drawLine(
            QPointF(TRACK_HEADER_WIDTH - 0.5, 0),
            QPointF(TRACK_HEADER_WIDTH - 0.5, self._track_height),
        )

        # Top separator line for the topmost track
        if self._is_topmost:
            painter.setPen(QPen(QColor(get_color("track_header_divider")), 1))
            painter.drawLine(QPointF(0, 0.5), QPointF(TRACK_HEADER_WIDTH, 0.5))

        # Clip hover highlight: keep the relationship visible without drawing
        # a bright rectangle around every header touched by linked clips.
        if self._is_clip_hover_highlighted:
            highlight_color = QColor(ACCENT_COLOR)
            highlight_color.setAlpha(150)
            painter.setBrush(QBrush(highlight_color))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawRect(QRectF(0, 0, 2, self._track_height))

        # Target track indicator (3px orange bar on left edge)
        if self._is_target:
            self._draw_target_indicator(painter)

        # Two-row layout vs single-row layout
        if use_two_rows:
            self._paint_two_rows(painter)
        else:
            self._paint_single_row(painter)

        # Locked overlay
        if self._is_locked:
            self._draw_locked_overlay(painter)

    def _apply_track_area_clip(self, painter: QPainter) -> None:
        """Clip pinned track headers to their own V/A area during split scrolling."""
        try:
            view = self.scene().views()[0] if self.scene() and self.scene().views() else None
            engine = getattr(view, "_layout_engine", None)
            if engine is None:
                return
            area = engine.area_layout()
            if self._track_type == "audio":
                scene_clip = area.audio_area
            elif self._track_type == "subtitle":
                scene_clip = area.subtitle_area
            else:
                scene_clip = area.video_area
            scene_y = self.scenePos().y()
            local_clip = QRectF(
                -100000.0,
                scene_clip.top() - scene_y,
                200000.0,
                scene_clip.height(),
            )
            painter.setClipRect(local_clip, Qt.ClipOperation.IntersectClip)
        except (RuntimeError, IndexError, AttributeError):
            return

    def _paint_two_rows(self, painter: QPainter) -> None:
        """Paint two-row DaVinci layout: Row 1 = badge+name+clip_count, Row 2 = buttons."""
        # Top strip: track badge + compact control buttons
        self._draw_track_badge(painter)
        self._draw_buttons(painter)

        # Fixed-height subtitle tracks only need the S1 badge and controls;
        # a second "Subtitle 1" label would be clipped by the compact lane.
        if self._track_type != "subtitle":
            self._draw_track_name(painter)
            self._draw_clip_count(painter)

        # Volume slider for audio (if height allows)
        if self._track_type == "audio":
            slider_rect = self._get_volume_slider_rect()
            if slider_rect.bottom() < self._track_height - 2:
                self._draw_volume_slider(painter)

    def _paint_single_row(self, painter: QPainter) -> None:
        """Paint single-row collapsed layout: badge + buttons only."""
        self._draw_track_badge(painter)
        self._draw_buttons(painter)

        # Volume slider for audio (if height allows)
        if self._track_type == "audio":
            slider_rect = self._get_volume_slider_rect()
            if slider_rect.bottom() < self._track_height - 2:
                self._draw_volume_slider(painter)

    def _draw_target_indicator(self, painter: QPainter) -> None:
        """Draw target track indicator as a precise, low-noise edge bar."""
        painter.setBrush(QBrush(TARGET_TRACK_INDICATOR_COLOR))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRect(QRectF(0, 0, 2, self._track_height))

    def _draw_track_badge(self, painter: QPainter) -> None:
        """Draw track type badge (V1/A1/S1) in DaVinci reddish-brown color."""
        badge_rect = self._get_track_badge_rect()
        badge_radius = EDIT_SIZES["track_badge_radius"]  # 3

        # Badge background — type-specific color (DaVinci Resolve style)
        if self._track_type == "video" and self._track_role == "graphics":
            badge_color = QColor("#8c5bd6")
        elif self._track_type == "video":
            badge_color = VIDEO_TRACK_COLOR
        elif self._track_type == "subtitle":
            badge_color = SUBTITLE_TRACK_COLOR
        else:
            badge_color = AUDIO_TRACK_COLOR

        if self._is_target:
            badge_color = badge_color.lighter(130)  # Brighten when target
        if self._is_locked:
            badge_color = badge_color.darker(140)

        # Badge text
        if self._track_type == "video" and self._track_role == "graphics":
            prefix = f"G{self._badge_index + 1}"
        elif self._track_type == "video":
            prefix = f"V{self._badge_index + 1}"
        elif self._track_type == "subtitle":
            prefix = f"S{self._badge_index + 1}"
        else:
            prefix = f"A{self._badge_index + 1}"

        # Draw badge rounded rect
        painter.setBrush(QBrush(badge_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(badge_rect, badge_radius, badge_radius)

        # Badge text (white, bold, centered)
        painter.setPen(QPen(QColor(255, 255, 255)))
        font = QFont("Arial", 9, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, prefix)

    def _draw_track_name(self, painter: QPainter) -> None:
        """Draw DaVinci-like track label in the lower body."""
        use_two_rows = self._track_height >= 40
        if use_two_rows:
            y = 34 if self._track_height >= 52 else 30
            name_rect = QRectF(16, y, 90, 16)
            from src.shared.i18n.i18n import I18N as _I18N
            if self._track_type == "video" and self._track_role == "graphics":
                display_name = _I18N.tr(
                    "timeline.track.name_graphics",
                    "图形 {index}",
                    index=self._badge_index + 1,
                )
            elif self._track_type == "video":
                display_name = _I18N.tr(
                    "timeline.track.name_video",
                    "视频 {index}",
                    index=self._badge_index + 1,
                )
            elif self._track_type == "subtitle":
                display_name = _I18N.tr(
                    "timeline.track.name_subtitle",
                    "字幕 {index}",
                    index=self._badge_index + 1,
                )
            else:
                display_name = _I18N.tr(
                    "timeline.track.name_audio",
                    "音频 {index}",
                    index=self._badge_index + 1,
                )
        else:
            badge_rect = self._get_track_badge_rect()
            name_rect = QRectF(badge_rect.right() + 5, badge_rect.top(), 70, badge_rect.height())
            display_name = self._track_name

        # Name background (optional hover highlight)
        if self._hover_button == "name":
            painter.setBrush(QBrush(QColor(50, 50, 55)))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawRoundedRect(name_rect.adjusted(-2, -1, 2, 1), 2, 2)

        # Name text
        name_color = TEXT_COLOR if not self._is_locked else TEXT_MUTED_COLOR
        painter.setPen(QPen(name_color))
        font = QFont("Arial", 9)
        painter.setFont(font)
        # Elide text if too long
        metrics = painter.fontMetrics()
        elided = metrics.elidedText(display_name, Qt.TextElideMode.ElideRight, int(name_rect.width()))
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

    def _draw_clip_count(self, painter: QPainter) -> None:
        """Draw clip count under the track label."""
        if self._clip_count <= 0:
            return

        if self._track_height < 52:
            return

        count_text = f"{self._clip_count} 个片段"

        count_rect = QRectF(16, 48, 90, 14)

        painter.setPen(QPen(TEXT_MUTED_COLOR))
        font = QFont("Arial", 8)
        painter.setFont(font)
        painter.drawText(count_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, count_text)

    def _draw_buttons(self, painter: QPainter) -> None:
        """Draw lock, solo/visibility, and mute buttons with vector icons."""
        # Lock button (L - amber when active)
        self._draw_button(
            painter,
            self._get_lock_button_rect(),
            "lock",
            self._is_locked,
            LOCK_ACTIVE_COLOR,
            "L"
        )

        if self._track_type == "subtitle":
            # Caption tracks have no audio mute/solo semantics.  Their right
            # control is the preview visibility eye; the middle slot stays
            # intentionally empty until Deliver owns an export policy.
            self._draw_button(
                painter,
                self._get_mute_button_rect(),
                "visibility",
                not self._is_visible,
                MUTE_ACTIVE_COLOR,
                "H",
            )
            return

        # Solo button (S - green when active)
        self._draw_button(
            painter,
            self._get_solo_button_rect(),
            "solo",
            self._is_solo,
            SOLO_ACTIVE_COLOR,
            "S"
        )

        if self._track_type == "video":
            # Video tracks expose visibility through an eye, not audio mute.
            self._draw_button(
                painter,
                self._get_mute_button_rect(),
                "visibility",
                not self._is_visible,
                MUTE_ACTIVE_COLOR,
                "H",
            )
        else:
            # Audio mute uses the speaker glyph and a clear red active state.
            self._draw_button(
                painter,
                self._get_mute_button_rect(),
                "mute",
                self._is_muted,
                MUTE_ACTIVE_COLOR,
                "M",
            )

    def _draw_button(
        self,
        painter: QPainter,
        rect: QRectF,
        button_type: str,
        is_active: bool,
        active_color: QColor,
        fallback_text: str
    ) -> None:
        """Draw a single button with crisp vector icons."""
        # Enable antialiasing for smooth vector icons
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Determine button color
        if is_active:
            color = active_color
        elif self._hover_button == button_type:
            color = BUTTON_HOVER_COLOR
        else:
            color = BUTTON_INACTIVE_COLOR

        # Idle controls deliberately have no filled pill.  Keeping only the
        # icon makes the header read as one instrument panel rather than a
        # row of unrelated mini-buttons.  Hover and active states add the
        # minimum surface needed for discoverability.
        if is_active:
            well_color = QColor(active_color)
            well_color.setAlpha(175)
            painter.setBrush(QBrush(well_color))
            painter.setPen(QPen(QColor(255, 255, 255, 24), 1))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
        elif self._hover_button == button_type:
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor(255, 255, 255, 14), 1))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)

        # Icon color
        icon_color = TEXT_COLOR if is_active else BUTTON_INACTIVE_COLOR
        painter.setPen(QPen(icon_color))
        painter.setBrush(QBrush(icon_color))

        cx = rect.center().x()
        cy = rect.center().y()
        # Keep the compact optical size used by the original track header.
        s = min(rect.width(), rect.height()) * 0.42

        if button_type == "lock":
            self._draw_lock_icon(painter, cx, cy, s, is_active)
        elif button_type == "solo":
            self._draw_solo_icon(painter, cx, cy, s, is_active)
        elif button_type == "mute":
            if self._track_type == "audio":
                self._draw_speaker_icon(painter, cx, cy, s, is_active)
            else:
                # Video tracks use an eye icon for their legacy hide control.
                self._draw_eye_icon(painter, cx, cy, s, is_active)
        elif button_type == "visibility":
            self._draw_eye_icon(painter, cx, cy, s, is_active)

    def _draw_lock_icon(self, painter: QPainter, cx: float, cy: float, s: float, is_active: bool) -> None:
        """Draw a padlock icon using QPainterPath for crisp rendering."""
        icon_color = TEXT_COLOR if is_active else TEXT_MUTED_COLOR
        painter.setPen(QPen(icon_color, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Shackle (arch at top)
        shackle_w = s * 1.22
        shackle_h = s * 1.02
        shackle_top = cy - s * 0.6
        shackle_rect = QRectF(cx - shackle_w / 2, shackle_top, shackle_w, shackle_h)
        painter.drawArc(shackle_rect, 30 * 16, 120 * 16)

        # Body (rounded rectangle at bottom)
        body_w = s * 1.6
        body_h = s * 1.0
        body_rect = QRectF(cx - body_w / 2, cy - s * 0.1, body_w, body_h)
        painter.setBrush(QBrush(icon_color))
        painter.drawRoundedRect(body_rect, 1.5, 1.5)

        # Keyhole
        painter.setBrush(QBrush(QColor(0x2A, 0x2A, 0x2A)))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawEllipse(QPointF(cx, cy + s * 0.15), s * 0.25, s * 0.25)

    def _draw_solo_icon(self, painter: QPainter, cx: float, cy: float, s: float, is_active: bool) -> None:
        """Draw a solo (headphone) icon."""
        icon_color = TEXT_COLOR if is_active else TEXT_MUTED_COLOR
        painter.setPen(QPen(icon_color, 1.3))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Headband arc
        arc_rect = QRectF(cx - s * 1.2, cy - s * 1.2, s * 2.4, s * 1.8)
        painter.drawArc(arc_rect, 40 * 16, 100 * 16)

        # Left ear cup
        painter.setBrush(QBrush(icon_color))
        ear_w = s * 0.6
        ear_h = s * 0.9
        painter.drawRoundedRect(QRectF(cx - s * 1.3, cy - s * 0.1, ear_w, ear_h), 2, 2)

        # Right ear cup
        painter.drawRoundedRect(QRectF(cx + s * 0.7, cy - s * 0.1, ear_w, ear_h), 2, 2)

    def _draw_speaker_icon(self, painter: QPainter, cx: float, cy: float, s: float, is_active: bool) -> None:
        """Draw a speaker icon for audio mute."""
        icon_color = TEXT_COLOR if is_active else TEXT_MUTED_COLOR
        painter.setPen(QPen(icon_color, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Speaker body (small rectangle)
        body_w = s * 0.6
        body_h = s * 1.0
        body_left = cx - s * 1.0
        body_top = cy - body_h / 2
        painter.setBrush(QBrush(icon_color))
        painter.drawRect(QRectF(body_left, body_top, body_w, body_h))

        # Speaker cone (triangle)
        painter.setBrush(QBrush(icon_color))
        path = QPainterPath()
        path.moveTo(body_left + body_w, cy - body_h / 2)
        path.lineTo(cx + s * 0.5, cy - s * 1.0)
        path.lineTo(cx + s * 0.5, cy + s * 1.0)
        path.lineTo(body_left + body_w, cy + body_h / 2)
        path.closeSubpath()
        painter.drawPath(path)

        # Sound waves or X for muted
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if not is_active:
            # Sound wave arcs
            for i in range(2):
                wave_rect = QRectF(cx + s * 0.6 + i * s * 0.3, cy - s * (0.8 - i * 0.2), s * 0.6, s * (1.6 - i * 0.4))
                painter.drawArc(wave_rect, -50 * 16, 100 * 16)
        else:
            # X mark for muted
            painter.setPen(QPen(icon_color, 1.5))
            x_cx = cx + s * 0.8
            x_r = s * 0.5
            painter.drawLine(QPointF(x_cx - x_r, cy - x_r), QPointF(x_cx + x_r, cy + x_r))
            painter.drawLine(QPointF(x_cx + x_r, cy - x_r), QPointF(x_cx - x_r, cy + x_r))

    def _draw_eye_icon(self, painter: QPainter, cx: float, cy: float, s: float, is_active: bool) -> None:
        """Draw an eye icon for video visibility."""
        icon_color = TEXT_COLOR if is_active else TEXT_MUTED_COLOR
        painter.setPen(QPen(icon_color, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Eye outline (almond shape)
        eye_path = QPainterPath()
        eye_w = s * 2.0
        eye_h = s * 1.0
        eye_path.moveTo(cx - eye_w / 2, cy)
        eye_path.cubicTo(cx - eye_w / 4, cy - eye_h, cx + eye_w / 4, cy - eye_h, cx + eye_w / 2, cy)
        eye_path.cubicTo(cx + eye_w / 4, cy + eye_h, cx - eye_w / 4, cy + eye_h, cx - eye_w / 2, cy)
        painter.drawPath(eye_path)

        if not is_active:
            # Pupil (circle)
            painter.setBrush(QBrush(icon_color))
            painter.drawEllipse(QPointF(cx, cy), s * 0.4, s * 0.4)
        else:
            # Hidden: diagonal line through eye
            painter.setPen(QPen(icon_color, 1.5))
            painter.drawLine(QPointF(cx - eye_w / 2, cy), QPointF(cx + eye_w / 2, cy))

    def _get_icon_key(self, button_type: str, is_active: bool) -> str:
        """Legacy icon key lookup — no longer used (vector icons instead)."""
        return ""

    def _draw_volume_slider(self, painter: QPainter) -> None:
        """Draw a compact audio-gain readout without a progress-bar look."""
        slider_rect = self._get_volume_slider_rect()

        # A fine baseline communicates the drag affordance without becoming a
        # large green widget in every audio header.
        center_y = slider_rect.center().y()
        painter.setPen(QPen(QColor(76, 81, 91), 1))
        painter.drawLine(
            QPointF(slider_rect.left(), center_y),
            QPointF(slider_rect.right(), center_y),
        )

        fill_width = slider_rect.width() * (self._volume / 2.0)
        fill_color = QColor(CLIP_AUDIO_COLOR.lighter(118))
        if self._volume > 1.0:
            fill_color = QColor(176, 101, 68)
        if self._is_muted:
            fill_color = QColor(82, 84, 91)
        painter.setPen(QPen(fill_color, 2))
        painter.drawLine(
            QPointF(slider_rect.left(), center_y),
            QPointF(slider_rect.left() + fill_width, center_y),
        )
        painter.setBrush(QBrush(fill_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawEllipse(
            QPointF(slider_rect.left() + fill_width, center_y),
            2.5,
            2.5,
        )

        vol_text = f"{int(self._volume * 100)}%"
        painter.setPen(QPen(TEXT_MUTED_COLOR))
        font = QFont("Arial", 8)
        painter.setFont(font)
        painter.drawText(
            slider_rect.adjusted(0, 2, 0, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            vol_text,
        )

    def _draw_resize_handle(self, painter: QPainter) -> None:
        """Draw track height resize handle at bottom."""
        resize_rect = self._get_resize_handle_rect()

        # Handle background
        hover_color = QColor(100, 100, 100) if self._hover_button == "resize" else QColor(50, 50, 50)
        painter.setBrush(QBrush(hover_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRect(resize_rect)

        # Draw grip lines (horizontal lines pattern)
        painter.setPen(QPen(QColor(80, 80, 80), 1))
        y_center = self._track_height + TRACK_RESIZE_HANDLE_HEIGHT // 2
        grip_width = TRACK_HEADER_WIDTH - 20
        start_x = 10

        # Multiple grip lines
        for i in range(3):
            y_offset = y_center - 2 + i * 2
            painter.drawLine(start_x, y_offset, start_x + grip_width, y_offset)

    def _draw_locked_overlay(self, painter: QPainter) -> None:
        """Draw locked overlay on track header (subtle dim, no diagonal stripes).

        Diagonal stripe pattern is only drawn on clips (ClipItem._draw_disabled_pattern),
        not on the track header — the header just gets a semi-transparent dim to
        indicate the track is locked.
        """
        overlay_color = QColor(0, 0, 0, 40)
        painter.setBrush(QBrush(overlay_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRect(QRectF(0, 0, TRACK_HEADER_WIDTH, self._track_height))

    # ==================== Interaction (Enhanced) ====================

    def hoverMoveEvent(self, event: Any) -> None:
        """Handle hover move to detect button proximity and boundary resize."""
        pos = event.pos()
        button = self._get_button_at_pos(pos)

        # Check if near bottom boundary (separator line area)
        near_bottom = pos.y() >= self._track_height - 4

        if button != self._hover_button:
            self._hover_button = button
            self.update()

        # Give each compact control a useful explanation on hover.  This is
        # especially important for the solo headphone glyph, which has no
        # text label in the Resolve-style header.
        self.setToolTip(self._button_tooltip(button))

        # Update cursor based on hover area
        if near_bottom and button is None:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif button == "resize":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif button == "name":
            self.setCursor(Qt.CursorShape.IBeamCursor)
        elif button:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()

        super().hoverMoveEvent(event)

    def _button_tooltip(self, button: Optional[str]) -> str:
        """Return the current action label for a hovered header control."""
        if button == "lock":
            return "解锁轨道" if self._is_locked else "锁定轨道"
        if button == "solo":
            return "取消独奏" if self._is_solo else "独奏轨道"
        if button == "mute":
            return "取消静音" if self._is_muted else "静音轨道"
        if button == "visibility":
            return "显示轨道" if not self._is_visible else "隐藏轨道"
        if button == "volume":
            return "轨道音量"
        if button == "resize":
            return "调整轨道高度"
        if button == "name":
            return "点击设为目标轨道，双击重命名"
        return ""

    def hoverLeaveEvent(self, event: Any) -> None:
        """Handle hover leave."""
        self._hover_button = None
        self.setToolTip("")
        self.unsetCursor()
        self.update()

        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        """Handle mouse press for button clicks, volume slider, and resize."""
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.pos()
            button = self._get_button_at_pos(pos)

            if button == "mute":
                self._toggle_mute()
                event.accept()
                return
            elif button == "visibility":
                self._toggle_visibility()
                event.accept()
                return
            elif button == "solo":
                self._toggle_solo()
                event.accept()
                return
            elif button == "lock":
                self._toggle_lock()
                event.accept()
                return
            elif button == "name":
                self._set_as_target()
                event.accept()
                return
            elif button == "volume":
                self._start_volume_drag(event)
                event.accept()
                return
            elif button == "resize":
                self._start_resize(event)
                event.accept()
                return

        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.screenPos())
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        """NEW: Handle double-click for track rename."""
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.pos()
            button = self._get_button_at_pos(pos)

            if button == "name":
                # Emit rename request（E22: 冗余的 track_double_clicked emit
                # 已删除，rename_requested 是双击重命名的唯一活链路）
                self.rename_requested.emit(self._track_id)
                return

        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        """Handle mouse move for resize and volume slider."""
        if self._is_resizing:
            self._update_resize(event)
            event.accept()
            return
        elif self._is_dragging_volume:
            self._update_volume_drag(event)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        """Handle mouse release to finish resize or volume drag."""
        if event.button() == Qt.MouseButton.LeftButton:
            if self._is_resizing:
                self._finish_resize()
                event.accept()
                return
            elif self._is_dragging_volume:
                self._finish_volume_drag()
                event.accept()
                return

        super().mouseReleaseEvent(event)

    # ==================== Button Actions ====================

    def _toggle_mute(self) -> None:
        """Toggle mute state.

        P4: Only emits the requested state — does NOT update _is_muted.
        The actual visual state is set from outside (by
        update_timeline_incremental or load_timeline) AFTER the command
        succeeds, preventing UI/model desync on command failure or undo.
        """
        new_mute = not self._is_muted
        self.mute_changed.emit(self._track_id, new_mute)
        logger.debug(f"Track {self._track_id} mute requested: {new_mute}")

    def _toggle_solo(self) -> None:
        """Toggle solo state.

        P4: Only emits the requested state — does NOT update _is_solo.
        Visual state is applied from outside after command succeeds.
        """
        new_solo = not self._is_solo
        self.solo_changed.emit(self._track_id, new_solo)
        logger.debug(f"Track {self._track_id} solo requested: {new_solo}")

    def _toggle_visibility(self) -> None:
        """Request a preview-visibility change without mutating local state."""
        new_visible = not self._is_visible
        self.visibility_changed.emit(self._track_id, new_visible)
        logger.debug(
            f"Track {self._track_id} visibility requested: {new_visible}"
        )

    def _toggle_lock(self) -> None:
        """Toggle lock state.

        P4: Only emits the requested state — does NOT update _is_locked.
        Visual state is applied from outside after command succeeds.
        """
        new_lock = not self._is_locked
        self.lock_changed.emit(self._track_id, new_lock)
        logger.debug(f"Track {self._track_id} lock requested: {new_lock}")

    def _set_as_target(self) -> None:
        """Set this track as the target track.

        P4: Only emits the request — does NOT update _is_target.
        Visual state is applied from outside after the host processes the signal.
        """
        self.target_track_changed.emit(self._track_id)
        logger.debug(f"Track {self._track_id} target requested")

    def set_volume(self, volume: float) -> None:
        """Set volume level (external call)."""
        self._volume = max(0.0, min(2.0, volume))
        self.update()

    # ==================== Phase 6.2: Clip Hover Highlight ====================

    def set_highlighted(self, highlighted: bool) -> None:
        """
        Set clip hover highlight state (Phase 6.2).

        当 hover clip 时，对应的轨道头显示高亮效果。
        使用更亮的背景色 + padding 边框。

        Args:
            highlighted: True when a clip on this track is being hovered
        """
        if self._is_clip_hover_highlighted != highlighted:
            self._is_clip_hover_highlighted = highlighted
            self.update()

    @property
    def is_clip_hover_highlighted(self) -> bool:
        """Get clip hover highlight state."""
        return self._is_clip_hover_highlighted

    # ==================== Volume Actions ====================

    # P4: Store the volume at drag start so we can emit one final command on release
    _volume_drag_start: float = 0.0

    def _start_volume_drag(self, event: Any) -> None:
        """Start volume slider drag.

        P4: Records the initial volume. During drag only the visual/tooltip
        is updated — no command is emitted until mouse release.
        """
        self._is_dragging_volume = True
        self._volume_drag_start = self._volume
        self._update_volume_visual_from_pos(event.pos())
        self.grabMouse()

    def _update_volume_drag(self, event: Any) -> None:
        """Update volume visual during slider drag.

        P4: Only updates the temporary visual state, does NOT emit
        volume_changed. The final value is emitted on mouse release.
        """
        self._update_volume_visual_from_pos(event.pos())

    def _update_volume_visual_from_pos(self, pos: QPointF) -> None:
        """Calculate and update temporary volume visual from mouse position.

        P4: Updates _volume for visual feedback only. The command is
        committed only on mouse release via _finish_volume_drag.
        """
        slider_rect = self._get_volume_slider_rect()
        # Calculate relative position within slider
        rel_x = pos.x() - slider_rect.left()
        rel_x = max(0, min(slider_rect.width(), rel_x))
        # Convert to volume (0.0 to 2.0)
        new_volume = (rel_x / slider_rect.width()) * 2.0

        if new_volume != self._volume:
            self._volume = new_volume
            self.update()

    def _finish_volume_drag(self) -> None:
        """Finish volume slider drag.

        P4: Emits volume_changed only once with the final value. The host
        (TimelineView) should create a single SetTrackVolumeCommand from
        the old value to this final value, avoiding undo-stack pollution.
        """
        self._is_dragging_volume = False
        self.ungrabMouse()
        final_volume = self._volume
        if final_volume != self._volume_drag_start:
            self.volume_changed.emit(self._track_id, final_volume)
        logger.debug(f"Track {self._track_id} volume drag finished: {final_volume:.2f}")

    # ==================== Resize Actions ====================

    def _start_resize(self, event: Any) -> None:
        """Start track height resize."""
        self._is_resizing = True
        self._resize_start_y = event.scenePos().y()
        self._resize_start_height = self._track_height

        # Capture mouse
        self.grabMouse()

        # Set resize cursor
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                view.setCursor(Qt.CursorShape.SizeVerCursor)

    def _update_resize(self, event: Any) -> None:
        """Update track height during resize."""
        delta_y = event.scenePos().y() - self._resize_start_y
        new_height = self._resize_start_height + int(delta_y)

        # Clamp height (minimum 20, maximum 150)
        new_height = max(20, min(150, new_height))

        # Maintain resize cursor during drag
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                view.setCursor(Qt.CursorShape.SizeVerCursor)

        if new_height != self._track_height:
            self._track_height = new_height
            self._update_position()
            self.update()

            # Emit height change
            self.height_changed.emit(self._track_id, new_height)

    def _finish_resize(self) -> None:
        """Finish track height resize."""
        self._is_resizing = False
        self.ungrabMouse()
        logger.debug(f"Track {self._track_id} height changed: {self._track_height}")

    # ==================== Context Menu ====================

    def _show_context_menu(self, screen_pos: QPointF) -> None:
        """Show track context menu."""
        from PyQt6.QtWidgets import QMenu
        from src.shared.i18n.i18n import I18N

        menu = QMenu()

        # P10.3: For subtitle tracks, show "Add Subtitle Clip" at the top
        add_sub_clip_action = None
        if self._track_type == "subtitle":
            add_sub_clip_action = menu.addAction(
                I18N.tr("timeline.subtitle.add_clip", "Add Subtitle Clip"))
            subtitle_to_speech_action = menu.addAction(
                I18N.tr("timeline.subtitle.to_speech", "Convert Subtitle Track to Speech"))
            export_subtitle_menu = QMenu(
                I18N.tr("timeline.subtitle.export_menu", "Export Subtitles"), menu)
            export_srt_action = export_subtitle_menu.addAction(
                I18N.tr("timeline.subtitle.export_srt", "SRT (.srt)"))
            export_vtt_action = export_subtitle_menu.addAction(
                I18N.tr("timeline.subtitle.export_vtt", "WebVTT (.vtt)"))
            export_ass_action = export_subtitle_menu.addAction(
                I18N.tr("timeline.subtitle.export_ass", "ASS/SSA (.ass)"))
            export_ttml_action = export_subtitle_menu.addAction(
                I18N.tr("timeline.subtitle.export_ttml", "TTML/DFXP (.ttml)"))
            export_scc_action = export_subtitle_menu.addAction(
                I18N.tr("timeline.subtitle.export_scc", "SCC / CEA-608 (.scc)"))
            menu.addSeparator()
            quality_action = menu.addAction(
                I18N.tr("timeline.subtitle.quality_check", "Check Subtitle Quality"))
            language_action = menu.addAction(
                I18N.tr("timeline.subtitle.set_language", "Set Caption Language..."))
            menu.addMenu(export_subtitle_menu)
            graphic_template_menu = QMenu(
                I18N.tr("timeline.subtitle.graphic_template", "Caption Graphic Template"), menu)
            graphic_fade_action = graphic_template_menu.addAction(
                I18N.tr("timeline.subtitle.graphic_fade", "Fade"))
            graphic_typewriter_action = graphic_template_menu.addAction(
                I18N.tr("timeline.subtitle.graphic_typewriter", "Typewriter"))
            graphic_scale_pop_action = graphic_template_menu.addAction(
                I18N.tr("timeline.subtitle.graphic_scale_pop", "Scale Pop"))
            graphic_remove_action = graphic_template_menu.addAction(
                I18N.tr("timeline.subtitle.graphic_remove", "Remove Template"))
            menu.addMenu(graphic_template_menu)
            menu.addSeparator()
        else:
            subtitle_to_speech_action = None
            export_subtitle_menu = None
            export_srt_action = None
            export_vtt_action = None
            export_ass_action = None
            export_ttml_action = None
            export_scc_action = None
            quality_action = None
            language_action = None
            graphic_fade_action = None
            graphic_typewriter_action = None
            graphic_scale_pop_action = None
            graphic_remove_action = None

        # Rename action
        rename_action = menu.addAction(
            I18N.tr("timeline.menu.rename_track", "Rename Track..."))

        # Delete action
        delete_action = menu.addAction(
            I18N.tr("timeline.menu.delete_track", "Delete Track"))

        menu.addSeparator()

        # Add track submenu — supports all track types (video/audio/subtitle)
        from PyQt6.QtWidgets import QMenu as _QMenu
        add_track_menu = _QMenu(
            I18N.tr("timeline.track.add_track_menu", "Add Track"), menu)

        def _build_type_submenus(label_text: str, position: str) -> _QMenu:
            """Build a submenu with Video/Audio/Subtitle entries for the given position."""
            sub = _QMenu(label_text, add_track_menu)
            sub_v = sub.addAction(I18N.tr("timeline.track.type_video", "Video Track"))
            sub_a = sub.addAction(I18N.tr("timeline.track.type_audio", "Audio Track"))
            sub_s = sub.addAction(I18N.tr("timeline.track.type_subtitle", "Subtitle Track"))
            sub.aboutToShow.connect(lambda: None)  # keep reference alive
            return sub, sub_v, sub_a, sub_s

        above_menu, above_v, above_a, above_s = _build_type_submenus(
            I18N.tr("timeline.track.add_above", "Add Above"), "above")
        below_menu, below_v, below_a, below_s = _build_type_submenus(
            I18N.tr("timeline.track.add_below", "Add Below"), "below")
        add_track_menu.addMenu(above_menu)
        add_track_menu.addMenu(below_menu)
        menu.addMenu(add_track_menu)

        menu.addSeparator()

        # Move track actions
        move_up_action = menu.addAction(
            I18N.tr("timeline.menu.move_up", "Move Track Up"))
        move_down_action = menu.addAction(
            I18N.tr("timeline.menu.move_down", "Move Track Down"))

        menu.addSeparator()

        # Height preset submenu
        from PyQt6.QtWidgets import QMenu as _QMenu
        height_menu = _QMenu("Track Height Preset", menu)
        small_action = height_menu.addAction(
            I18N.tr("timeline.track.height_small", "Small"))
        medium_action = height_menu.addAction(
            I18N.tr("deliver.quality_medium", "Medium"))
        large_action = height_menu.addAction(
            I18N.tr("timeline.track.height_large", "Large"))
        default_action = height_menu.addAction(
            I18N.tr("deliver.quality_default", "Default"))
        menu.addMenu(height_menu)

        menu.addSeparator()

        # NEW: Target track action
        target_action = menu.addAction(
            I18N.tr("timeline.menu.set_target_track", "Set as Target Track"))
        target_action.setCheckable(True)
        target_action.setChecked(self._is_target)

        # P1-05: Sync Lock —— 关闭后本轨不跟随波纹/提取位移（允许失步）
        from src.shared.i18n.i18n import I18N as _I18N
        sync_action = menu.addAction(
            _I18N.tr("edit.track.sync_lock", "Sync Lock")
        )
        sync_action.setCheckable(True)
        sync_action.setChecked(self._is_sync_locked)
        sync_action.setToolTip(
            _I18N.tr(
                "edit.track.sync_lock_tip",
                "开启：波纹/提取操作带动本轨；关闭：本轨保持位置（有意失步）",
            )
        )

        # Visibility toggle
        menu.addSeparator()
        visibility_action = menu.addAction("Hide Track" if self._is_visible else "Show Track")

        # Add Float/Dock option for timeline panel
        menu.addSeparator()
        from PyQt6.QtWidgets import QDockWidget
        from src.shared.i18n.i18n import I18N
        timeline_dock = None
        # Find the dock widget containing this timeline
        parent_widget = self.scene().views()[0] if self.scene().views() else None
        if parent_widget:
            for p in [parent_widget.parent(), parent_widget.parent().parent() if parent_widget.parent() else None]:
                if isinstance(p, QDockWidget):
                    timeline_dock = p
                    break
            # Also check through parent chain
            if not timeline_dock:
                p = parent_widget.parent()
                while p:
                    if isinstance(p, QDockWidget):
                        timeline_dock = p
                        break
                    p = p.parent()

        if timeline_dock:
            is_floating = timeline_dock.isFloating()
            float_text = I18N.tr("dock.dock", "Dock") if is_floating else I18N.tr("dock.float", "Float")
            float_action = menu.addAction(float_text)

        # Execute menu
        action = menu.exec(screen_pos)

        # Handle Float/Dock action first
        if timeline_dock and action == float_action:
            timeline_dock.setFloating(not timeline_dock.isFloating())
            return

        if action == rename_action:
            self.rename_requested.emit(self._track_id)
        elif action == delete_action:
            self.delete_requested.emit(self._track_id)
        elif add_sub_clip_action is not None and action == add_sub_clip_action:
            self.add_subtitle_clip_requested.emit(self._track_id)
        elif subtitle_to_speech_action is not None and action == subtitle_to_speech_action:
            self.subtitle_to_speech_requested.emit(self._track_id)
        elif export_srt_action is not None and action == export_srt_action:
            self.subtitle_export_requested.emit(self._track_id, "srt")
        elif export_vtt_action is not None and action == export_vtt_action:
            self.subtitle_export_requested.emit(self._track_id, "vtt")
        elif export_ass_action is not None and action == export_ass_action:
            self.subtitle_export_requested.emit(self._track_id, "ass")
        elif export_ttml_action is not None and action == export_ttml_action:
            self.subtitle_export_requested.emit(self._track_id, "ttml")
        elif export_scc_action is not None and action == export_scc_action:
            self.subtitle_export_requested.emit(self._track_id, "scc")
        elif quality_action is not None and action == quality_action:
            self.subtitle_quality_requested.emit(self._track_id)
        elif language_action is not None and action == language_action:
            self.subtitle_language_requested.emit(self._track_id)
        elif graphic_fade_action is not None and action == graphic_fade_action:
            self.caption_track_graphic_template_requested.emit(self._track_id, "fade")
        elif graphic_typewriter_action is not None and action == graphic_typewriter_action:
            self.caption_track_graphic_template_requested.emit(self._track_id, "typewriter")
        elif graphic_scale_pop_action is not None and action == graphic_scale_pop_action:
            self.caption_track_graphic_template_requested.emit(self._track_id, "scale_pop")
        elif graphic_remove_action is not None and action == graphic_remove_action:
            self.caption_track_graphic_template_requested.emit(self._track_id, "")
        # P10.3: Add track submenu — Video/Audio/Subtitle, Above/Below
        elif action == above_v:
            self.add_track_requested.emit("video", "above", self._track_index)
        elif action == above_a:
            self.add_track_requested.emit("audio", "above", self._track_index)
        elif action == above_s:
            self.add_track_requested.emit("subtitle", "above", self._track_index)
        elif action == below_v:
            self.add_track_requested.emit("video", "below", self._track_index)
        elif action == below_a:
            self.add_track_requested.emit("audio", "below", self._track_index)
        elif action == below_s:
            self.add_track_requested.emit("subtitle", "below", self._track_index)
        elif action == move_up_action:
            self.move_track_requested.emit(self._track_id, "up")
        elif action == move_down_action:
            self.move_track_requested.emit(self._track_id, "down")
        elif action == small_action:
            self.height_preset_requested.emit("small")
        elif action == medium_action:
            self.height_preset_requested.emit("medium")
        elif action == large_action:
            self.height_preset_requested.emit("large")
        elif action == default_action:
            self.height_preset_requested.emit("default")
        elif action == target_action:
            # AUD-21104: P4 提交一致性——只 emit 请求，不改本地状态。
            # 宿主（TimelineView._on_target_track_changed）负责回写新旧
            # 两个表头的 _is_target，命令失败/撤销时表头不会与模型脱节。
            self.target_track_changed.emit(self._track_id)
        elif action == sync_action:
            # AUD-21104: 同上——由 SetTrackSyncLockCommand 成功后的增量
            # 更新经 set_sync_locked() 回写本表头。
            self.sync_lock_changed.emit(self._track_id, sync_action.isChecked())
        elif action == visibility_action:
            self.visibility_changed.emit(self._track_id, not self._is_visible)

    # ==================== State Updates ====================

    def set_muted(self, muted: bool) -> None:
        """Set mute state (external call)."""
        if self._is_muted != muted:
            self._is_muted = muted
            self.update()

    def set_solo(self, solo: bool) -> None:
        """Set solo state (external call)."""
        if self._is_solo != solo:
            self._is_solo = solo
            self.update()

    def set_locked(self, locked: bool) -> None:
        """Set lock state (external call)."""
        if self._is_locked != locked:
            self._is_locked = locked
            self.update()

    def set_sync_locked(self, sync_locked: bool) -> None:
        """Set sync lock state (external call, P1-05)."""
        if self._is_sync_locked != sync_locked:
            self._is_sync_locked = sync_locked
            self.update()

    def set_visible(self, visible: bool) -> None:
        """Set visibility state (external call)."""
        if self._is_visible != visible:
            self._is_visible = visible
            self.update()

    def set_target(self, is_target: bool) -> None:
        """NEW: Set target track state (external call)."""
        if self._is_target != is_target:
            self._is_target = is_target
            self.update()

    def set_track_index(self, index: int) -> None:
        """Set track index (for reordering)."""
        self._track_index = index
        self._update_position()
        self.update()

    def set_badge_index(self, index: int) -> None:
        """Set type-local badge index (V1/V2 or A1/A2)."""
        if self._badge_index != index:
            self._badge_index = index
            self.update()

    def set_track_height(self, height: int) -> None:
        """Set track height (external call)."""
        if self._track_height != height:
            self._track_height = height
            self._update_position()
            self.update()

    def set_track_name(self, name: str) -> None:
        """Set track name (external call)."""
        self._track_name = name
        self.update()

    def set_clip_count(self, count: int) -> None:
        """Set number of clips on this track (displayed in header).

        Args:
            count: Number of clips, 0 hides the count.
        """
        if self._clip_count != count:
            self._clip_count = max(0, count)
            self.update()


# ================================================================
# TimecodeRulerItem
# ================================================================

class TimecodeRulerItem(QGraphicsObject):
    """
    Timecode ruler at the top of timeline with playhead interaction.

    Shows frame numbers, timecode, or audio samples at regular intervals.
    Supports click to set playhead position and drag scrubbing.
    Supports display mode toggle between frames and audio samples.

    Features (reference: kdenlive timeline2/view/qml/Ruler.qml):
    - Dynamic tick spacing based on zoom level
    - Full timecode labels on major ticks, with unlabeled subdivisions
    - SMPTE timecode format support
    - Efficient rendering (only visible area)

    Signals:
        ruler_clicked: User clicked on ruler at frame position
        ruler_dragged: User dragging on ruler (scrubbing) at frame position
        display_mode_changed: Display mode changed (mode)

    Attributes:
        _pixels_per_frame: Zoom scale
        _fps: Frames per second
        _sample_rate: Audio sample rate (for audio display mode)
        _duration_frames: Timeline duration
        _display_mode: Display mode ('frames' or 'samples')
        _is_dragging: Whether user is dragging on ruler
        _ruler_height: Height of the ruler (default 25)
    """

    # Signals
    ruler_clicked = pyqtSignal(int)  # frame
    ruler_dragged = pyqtSignal(int)  # frame (continuous during scrub)
    ruler_scrub_finished = pyqtSignal()
    display_mode_changed = pyqtSignal(str)  # display_mode ('frames' or 'samples')
    mark_in_requested = pyqtSignal(int)  # frame
    mark_out_requested = pyqtSignal(int)  # frame
    clear_range_requested = pyqtSignal()

    LABEL_GUTTER = 110  # Logical pixels reserved for a full timecode and its gap.

    def __init__(
        self,
        pixels_per_frame: float,
        fps: float,
        duration_frames: int,
        sample_rate: int = 48000,  # Default sample rate
        display_mode: str = "frames",  # 'frames' or 'samples'
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._pixels_per_frame = pixels_per_frame
        self._fps = fps
        self._fps_rational = Rational.from_float(fps)
        self._timecode_type = Timecode.auto_detect_type(self._fps_rational)
        self._sample_rate = sample_rate
        self._duration_frames = duration_frames
        self._display_mode = display_mode
        self._is_dragging = False
        # 修复：使用 RULER_HEIGHT 常量
        self._ruler_height = RULER_HEIGHT
        self._viewport_width: float = 0.0  # Updated by TimelineView to cover visible area
        # Half-open (start, end, status) ranges supplied by TimelineView.
        # Keeping them on the ruler makes the indicator scroll/zoom together
        # with the time scale and avoids a second overlay widget.
        self._cache_segments: tuple[tuple[int, int, str], ...] = ()

        # Cached tick calculations
        self._tick_interval = 1
        self._major_tick_interval = 1
        self._medium_tick_interval = 1

        # Enable mouse events
        self.setAcceptHoverEvents(True)

        # 让 paint() 收到真实的 exposedRect（播放头窄条更新时只重画相交
        # 区段的刻度），否则每次 playhead 移动都会全宽重绘标尺刻度。
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)

        self.setZValue(900)

        # Initial tick calculation
        self._recalculate_ticks()

    # ==================== Geometry ====================

    def boundingRect(self) -> QRectF:
        """Return bounding rectangle.

        Width covers max(content width, viewport width) so ticks render
        across the full visible area even when zoomed out.
        """
        content_width = self._duration_frames * self._pixels_per_frame + 100
        width = max(content_width, self._viewport_width)
        return QRectF(TRACK_HEADER_WIDTH, 0, width, self._ruler_height)

    def shape(self) -> QPainterPath:
        """Return shape for collision detection."""
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    # ==================== Tick Calculation (DaVinci-like LOD) ====================

    def _calculate_tick_interval(self, pixels_per_frame: float, fps: float) -> int:
        """Calculate minor tick interval based on zoom level.

        Uses DaVinci-like LOD targets:
        - Minor ticks target 10-24px spacing
        - Candidates are nice divisors of common time intervals

        Args:
            pixels_per_frame: Current zoom scale (pixels per frame)
            fps: Frames per second

        Returns:
            Tick interval in frames
        """
        fps_int = max(1, round(fps))
        target_minor_min_px = EDIT_SIZES["ruler_target_minor_min_px"]  # 10

        # Build candidate minor steps: frame-level, seconds, and long overview
        # intervals.  The latter are important at the minimum timeline zoom:
        # falling back to a one-second tick there would attempt to paint
        # thousands of sub-pixel ticks per viewport.
        candidate_minor = [1, 2, 5, 10, 15]
        if fps_int > 1:
            candidate_minor.extend([
                fps_int // 2,
                fps_int,
                fps_int * 2,
                fps_int * 5,
                fps_int * 10,
                fps_int * 30,
                fps_int * 60,
                fps_int * 300,
                fps_int * 600,
            ])
        candidate_minor = sorted(set(candidate_minor))

        for step in candidate_minor:
            px = step * pixels_per_frame
            if px >= target_minor_min_px:
                return step

        # Fallback: largest candidate keeps the ruler bounded even for zoom
        # values below the supported minimum.
        return candidate_minor[-1] if candidate_minor else fps_int

    def _calculate_major_tick_interval(self, fps: float) -> int:
        """Calculate major tick interval for timecode labels.

        Uses DaVinci-like LOD targets:
        - Major ticks target 90-150px spacing
        - Candidate steps are meaningful time intervals (1s, 2s, 5s, 10s, ...)

        Args:
            fps: Frames per second

        Returns:
            Major tick interval in frames
        """
        fps_int = max(1, round(fps))
        target_major_min_px = self.LABEL_GUTTER
        ppf = self._pixels_per_frame

        # Keep major ticks on whole seconds until the ruler is zoomed in
        # enough to show useful sub-second labels.
        candidate_major_steps = [
            fps_int * seconds for seconds in (1, 2, 5, 10, 30, 60, 300, 600, 1800, 3600)
        ]
        if fps_int * ppf >= 3 * target_major_min_px:
            subdivisions = {fps_int // divisor for divisor in (2, 5) if fps_int % divisor == 0}
            candidate_major_steps = sorted(subdivisions) + candidate_major_steps

        for step in candidate_major_steps:
            px = step * ppf
            if px >= target_major_min_px:
                return step

        return math.ceil(target_major_min_px / (fps_int * ppf)) * fps_int

    def _calculate_medium_tick_interval(self, tick_interval: int, major_tick_interval: int) -> int:
        """Calculate medium tick interval between major ticks.

        Finds a sub-step of major_tick_interval that yields 10-24px spacing,
        producing visual hierarchy between major and minor ticks.

        Args:
            tick_interval: Current minor tick interval in frames
            major_tick_interval: Major tick interval in frames
            fps: Frames per second

        Returns:
            Medium tick interval in frames
        """
        target_minor_min_px = EDIT_SIZES["ruler_target_minor_min_px"]  # 10
        target_minor_max_px = EDIT_SIZES["ruler_target_minor_max_px"]  # 24
        ppf = self._pixels_per_frame

        if major_tick_interval <= tick_interval:
            return major_tick_interval

        # Try common divisors of the major interval to find one in 10-24px range
        nice_divisors = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30]
        best_step = major_tick_interval  # fallback: no subdivision

        for d in nice_divisors:
            sub_step = major_tick_interval // d
            if sub_step <= 0:
                continue
            px = sub_step * ppf
            if target_minor_min_px <= px <= target_minor_max_px:
                best_step = sub_step
                break
            # Accept first step that gives at least the min px (but prefer ones in range)
            if px >= target_minor_min_px and best_step == major_tick_interval:
                best_step = sub_step

        # Ensure medium is a proper divisor of major for clean alignment
        if major_tick_interval % best_step != 0:
            best_step = major_tick_interval // nice_divisors[0]
            if best_step <= 0:
                best_step = major_tick_interval

        return max(tick_interval, best_step)

    def _recalculate_ticks(self) -> None:
        """Recalculate all tick intervals based on current zoom."""
        self._tick_interval = self._calculate_tick_interval(self._pixels_per_frame, self._fps)
        self._major_tick_interval = self._calculate_major_tick_interval(self._fps)
        self._medium_tick_interval = self._calculate_medium_tick_interval(
            self._tick_interval, self._major_tick_interval
        )

    @property
    def major_tick_interval(self) -> int:
        """Frame interval shared by the ruler and its track grid."""
        return self._major_tick_interval

    def _format_timecode(self, frame: int) -> str:
        """Use the same SMPTE convention as the transport timecode."""
        nominal_fps = round(self._fps)
        if nominal_fps > 0 and frame < nominal_fps * 86400:
            return Timecode.from_frame_number(frame, self._fps_rational, self._timecode_type).to_string()
        seconds, frames = divmod(frame, max(1, nominal_fps))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    # ==================== Frame Calculation ====================

    def _get_frame_at_pos(self, scene_x: float) -> int:
        """Calculate frame number from scene X position.

        Args:
            scene_x: Scene X coordinate

        Returns:
            Frame number (clamped to valid range)
        """
        # Subtract header offset
        x = scene_x - TRACK_HEADER_WIDTH

        # Calculate frame
        frame = int(x / self._pixels_per_frame)

        # Clamp to valid range
        frame = max(0, min(self._duration_frames, frame))

        return frame

    def _frame_to_samples(self, frame: int) -> int:
        """Convert frame to audio samples."""
        return int(frame * self._sample_rate / self._fps)

    def _samples_to_frame(self, samples: int) -> int:
        """Convert audio samples to frame."""
        return int(samples * self._fps / self._sample_rate)

    # ==================== Properties ====================

    @property
    def display_mode(self) -> str:
        return self._display_mode

    def set_display_mode(self, mode: str) -> None:
        """Set display mode ('frames' or 'samples')."""
        if mode in ("frames", "samples") and mode != self._display_mode:
            self._display_mode = mode
            self.display_mode_changed.emit(mode)
            self.update()

    def set_cache_segments(self, segments: Any) -> None:
        """Set red/green cache ranges drawn below the time ticks."""
        normalized: list[tuple[int, int, str]] = []
        for segment in segments or ():
            try:
                if hasattr(segment, "start_frame"):
                    start = int(segment.start_frame)
                    end = int(segment.end_frame)
                    status = str(segment.status)
                else:
                    start = int(segment[0])
                    end = int(segment[1])
                    status = str(segment[2])
            except (IndexError, TypeError, ValueError):
                continue
            if end > start and status in {"cached", "uncached"}:
                normalized.append((start, end, status))
        value = tuple(normalized)
        if value != self._cache_segments:
            self._cache_segments = value
            self.update()

    # ==================== Painting ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """Paint the timecode ruler with DaVinci Resolve-style dark background."""
        # Ruler background: #17181D (darker than track area)
        bg_color = QColor(get_color("ruler_bg"))
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRect(self.boundingRect())

        # Bottom separator line: #292B31 1px
        bottom_line_color = QColor(get_color("ruler_bottom_line"))
        painter.setPen(QPen(bottom_line_color, 1))
        painter.drawLine(
            int(self.boundingRect().left()), self._ruler_height - 1,
            int(self.boundingRect().right()), self._ruler_height - 1,
        )

        # Get visible rect for efficient rendering
        exposed_rect = option.exposedRect.intersected(self.boundingRect())
        visible_left = max(0.0, exposed_rect.left() - TRACK_HEADER_WIDTH)
        visible_right = exposed_rect.right() - TRACK_HEADER_WIDTH

        # Include labels that start left of the damage region. Their anchor
        # and visibility must be identical for a full or partial repaint.
        start_frame = max(0, math.floor((visible_left - self.LABEL_GUTTER) / self._pixels_per_frame))
        end_frame = math.ceil(visible_right / self._pixels_per_frame)

        if self._display_mode == "samples":
            # Audio sample rate display
            self._paint_samples_mode(painter, start_frame, end_frame)
        else:
            # Frame/timecode display (default) with dynamic ticks
            self._paint_dynamic_ticks(painter, start_frame, end_frame)

        # Resolve-style render-cache indicator.  It lives in the last few
        # pixels of the ruler, directly below the tick labels.  Direct source
        # media has no segment and therefore leaves this strip untouched.
        indicator_y = self._ruler_height - 4
        indicator_h = 3
        for segment_start, segment_end, status in self._cache_segments:
            x = TRACK_HEADER_WIDTH + segment_start * self._pixels_per_frame
            width = max(1.0, (segment_end - segment_start) * self._pixels_per_frame)
            if status == "cached":
                color = QColor("#4FBF73")
            else:
                color = QColor("#D94A4A")
            painter.fillRect(QRectF(x, indicator_y, width, indicator_h), color)

    def _paint_dynamic_ticks(self, painter: QPainter, start_frame: int, end_frame: int) -> None:
        """Paint ticks and timecodes at fixed positions anchored to frame zero."""
        major = self._major_tick_interval
        medium = self._medium_tick_interval
        for step, token, height_token in (
            (self._tick_interval, "ruler_minor_tick", "ruler_minor_tick_h"),
            (medium, "ruler_medium_tick", "ruler_medium_tick_h"),
            (major, "ruler_major_tick", "ruler_major_tick_h"),
        ):
            painter.setPen(QPen(QColor(get_color(token)), 1))
            height = EDIT_SIZES[height_token]
            for frame in range((start_frame // step) * step, end_frame + 1, step):
                if step != major and frame % major == 0:
                    continue
                x = int(TRACK_HEADER_WIDTH + frame * self._pixels_per_frame)
                painter.drawLine(x, 2, x, 2 + height)

        font = QFont("Arial")
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor(get_color("ruler_text")))
        for frame in range((start_frame // major) * major, end_frame + 1, major):
            x = TRACK_HEADER_WIDTH + frame * self._pixels_per_frame
            painter.drawText(QPointF(x + 4, 23), self._format_timecode(frame))

    def _paint_samples_mode(self, painter: QPainter, start_frame: int, end_frame: int) -> None:
        """Draw sample labels at a fixed stride, independent of repaint bounds."""
        samples_per_frame = self._sample_rate / self._fps
        if self._pixels_per_frame < 5:
            sample_interval = self._sample_rate
        elif self._pixels_per_frame < 20:
            sample_interval = self._sample_rate // 2
        else:
            sample_interval = 1000

        step = max(self._tick_interval, int(sample_interval / samples_per_frame))
        label_step = step * max(1, math.ceil(self.LABEL_GUTTER / (step * self._pixels_per_frame)))
        painter.setPen(QPen(QColor(get_color("ruler_medium_tick")), 1))
        for frame in range((start_frame // step) * step, end_frame + 1, step):
            x = int(TRACK_HEADER_WIDTH + frame * self._pixels_per_frame)
            painter.drawLine(x, 2, x, 2 + EDIT_SIZES["ruler_medium_tick_h"])

        font = QFont("Arial")
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor(get_color("ruler_text")))
        for frame in range((start_frame // label_step) * label_step, end_frame + 1, label_step):
            samples = self._frame_to_samples(frame)
            text = f"{samples // 1000}k" if samples >= 1000 else str(samples)
            x = TRACK_HEADER_WIDTH + frame * self._pixels_per_frame
            painter.drawText(QPointF(x + 4, 23), text)

    # ==================== Interaction ====================

    def hoverEnterEvent(self, event: Any) -> None:
        """Handle hover enter."""
        # Set cursor to indicate clickable
        for view in self.scene().views():
            view.setCursor(Qt.CursorShape.CrossCursor)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: Any) -> None:
        """Handle hover leave."""
        # Reset cursor
        for view in self.scene().views():
            view.setCursor(Qt.CursorShape.ArrowCursor)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        """Handle mouse press to set playhead."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = True
            scene_x = event.scenePos().x()
            frame = self._get_frame_at_pos(scene_x)
            self.ruler_clicked.emit(frame)
            self.grabMouse()

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        """Handle mouse move for scrubbing."""
        if self._is_dragging:
            scene_x = event.scenePos().x()
            frame = self._get_frame_at_pos(scene_x)
            self.ruler_dragged.emit(frame)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        """Handle mouse release to end scrub."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = False
            self.ungrabMouse()
            self.ruler_scrub_finished.emit()

        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event: Any) -> None:
        """Show range and display-mode actions for the clicked ruler frame."""
        from PyQt6.QtWidgets import QMenu
        from src.shared.i18n.i18n import I18N

        menu = QMenu()
        frame = self._get_frame_at_pos(event.scenePos().x())

        mark_in_action = menu.addAction(
            I18N.tr("timeline.range.set_in", "设置入点")
        )
        mark_out_action = menu.addAction(
            I18N.tr("timeline.range.set_out", "设置出点")
        )
        clear_range_action = menu.addAction(
            I18N.tr("timeline.range.clear", "取消选择区域")
        )
        menu.addSeparator()

        # Display mode options
        frames_action = menu.addAction(
            I18N.tr("timeline.menu.show_frames", "Show Frames"))
        samples_action = menu.addAction(
            I18N.tr("timeline.menu.show_audio_samples", "Show Audio Samples"))

        # Set check marks
        frames_action.setCheckable(True)
        samples_action.setCheckable(True)
        frames_action.setChecked(self._display_mode == "frames")
        samples_action.setChecked(self._display_mode == "samples")

        # Execute menu
        action = menu.exec(event.screenPos())

        # Handle action
        if action == mark_in_action:
            self.mark_in_requested.emit(frame)
        elif action == mark_out_action:
            self.mark_out_requested.emit(frame)
        elif action == clear_range_action:
            self.clear_range_requested.emit()
        elif action == frames_action:
            self.set_display_mode("frames")
        elif action == samples_action:
            self.set_display_mode("samples")

        event.accept()

    # ==================== State Updates ====================

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale and recalculate tick intervals."""
        if pixels_per_frame == self._pixels_per_frame:
            return
        self.prepareGeometryChange()
        self._pixels_per_frame = pixels_per_frame
        self._recalculate_ticks()
        self.update()

    def update_duration(self, duration_frames: int) -> None:
        """Update timeline duration."""
        if duration_frames == self._duration_frames:
            return
        self.prepareGeometryChange()
        self._duration_frames = duration_frames
        self.update()

    def update_fps(self, fps: float | Rational) -> None:
        """Update frames per second and recalculate tick intervals."""
        rational = fps if isinstance(fps, Rational) else Rational.from_float(fps)
        if rational == self._fps_rational:
            return
        self._fps_rational = rational
        self._fps = float(rational)
        self._timecode_type = Timecode.auto_detect_type(rational)
        self._recalculate_ticks()
        self.update()

    def update_sample_rate(self, sample_rate: int) -> None:
        """Update audio sample rate."""
        self._sample_rate = sample_rate
        if self._display_mode == "samples":
            self.update()

    def set_viewport_width(self, width: float) -> None:
        """Update viewport width so boundingRect covers the visible area.

        Args:
            width: Minimum ruler width needed (accounting for scroll position).
                   Pass (scroll_x + viewport_widget_width - TRACK_HEADER_WIDTH).
        """
        if width <= self._viewport_width:
            return
        self.prepareGeometryChange()
        self._viewport_width = width


# ================================================================
# SnapIndicatorItem (P1.4 - Enhanced with snap point text)
# ================================================================

class SnapIndicatorItem(QGraphicsObject):
    """
    Visual indicator shown when snapping is active (P1.4).

    Features:
    - Yellow vertical line spanning all tracks
    - Glow effect for visibility
    - Snap point text: "Clip Start", "Clip End", etc.

    Attributes:
        _frame: Snap frame position
        _pixels_per_frame: Zoom scale
        _num_tracks: Number of tracks to span
        _visible: Whether the indicator is shown
        _snap_type: Type of snap point ('clip_start', 'clip_end', 'playhead', etc.)
    """

    # Snap type labels (参考 kdenlive SnapModel 类型)
    SNAP_TYPE_LABELS = {
        'clip_start': 'Clip Start',
        'clip_end': 'Clip End',
        'playhead': 'Playhead',
        'marker': 'Marker',
        'guide': 'Guide',
        'trans_start': 'Trans Start',
        'trans_center': 'Trans Center',
        'trans_end': 'Trans End',
        'seq_start': 'Seq Start',
    }

    def __init__(
        self,
        frame: int = 0,
        pixels_per_frame: float = 5.0,
        num_tracks: int = 1,
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._frame = frame
        self._pixels_per_frame = pixels_per_frame
        self._num_tracks = num_tracks
        self._visible = False
        self._snap_type: Optional[str] = None  # Type of snap point
        # Actual cumulative track area height (set by TimelineView)
        self._track_area_height: Optional[float] = None

        # Set Z-value above clips but below playhead
        self.setZValue(600)

        # Initially hidden
        self.hide()

        self._update_geometry()

    # ==================== Geometry ====================

    def _effective_track_height(self) -> float:
        """Return the actual track area height if set, else estimate from num_tracks."""
        if self._track_area_height is not None:
            return self._track_area_height
        return self._num_tracks * (TRACK_HEIGHT + TRACK_GAP)

    def _update_geometry(self) -> None:
        """Update X from frame. Y is set externally."""
        x = TRACK_HEADER_WIDTH + self._frame * self._pixels_per_frame
        self.setX(x)
        # Y is set by TimelineView to TRACK_AREA_TOP

    def boundingRect(self) -> QRectF:
        """Return bounding rectangle including label."""
        height = self._effective_track_height()
        label_width = 80  # Width for snap type label

        return QRectF(-label_width / 2, 0, label_width, height)

    def shape(self) -> QPainterPath:
        """Return shape for collision detection."""
        path = QPainterPath()
        height = self._effective_track_height()
        path.addRect(-SNAP_INDICATOR_LINE_WIDTH / 2, 0, SNAP_INDICATOR_LINE_WIDTH, height)
        return path

    # ==================== State Updates ====================

    def set_frame(self, frame: int) -> None:
        """Set snap indicator position."""
        self._frame = frame
        self._update_geometry()

    def set_snap_type(self, snap_type: str) -> None:
        """Set the type of snap point for label display (P1.4)."""
        self._snap_type = snap_type
        self.update()

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale."""
        self._pixels_per_frame = pixels_per_frame
        self._update_geometry()

    def update_num_tracks(self, num_tracks: int) -> None:
        """Update number of tracks to span."""
        self._num_tracks = num_tracks
        self._update_geometry()

    def update_track_area_height(self, height: float) -> None:
        """Update the actual cumulative track area height from the layout."""
        if self._track_area_height != height:
            self.prepareGeometryChange()
            self._track_area_height = height
            self.update()

    def show_indicator(self, frame: int, snap_type: Optional[str] = None) -> None:
        """Show the snap indicator at a specific frame with optional snap type."""
        self._frame = frame
        self._snap_type = snap_type
        self._visible = True
        self._update_geometry()
        self.show()

    def hide_indicator(self) -> None:
        """Hide the snap indicator."""
        self._visible = False
        self._snap_type = None
        self.hide()

    def is_visible(self) -> bool:
        """Check if indicator is visible."""
        return self._visible

    # ==================== Painting ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """Paint the snap indicator with enhanced visibility and snap type label (P1.4)."""
        if not self._visible:
            return

        height = int(self._effective_track_height())

        # A precise amber hairline is enough feedback.  The old broad glow
        # could be mistaken for a persistent guide at sequence start.
        painter.setPen(QPen(QColor(224, 177, 61, 235), 1))
        painter.drawLine(0, 0, 0, height)

        # Snap type is kept as state for future status-bar feedback.  It is
        # intentionally not rendered in the scene: a label below the track
        # area can leak through the frozen header and look like a stray yellow
        # box after the snap line has disappeared.

# ================================================================
# RollHandleItem (P4.3 - Roll Trim Handle)
# ================================================================

# The handle remains an invisible hit target. Roll feedback is supplied by
# Qt's native horizontal resize cursor rather than a second painted arrow.
ROLL_HANDLE_SIZE = 20  # Width of roll handle and hit target


class RollHandleItem(QGraphicsObject):
    """
    Visual handle for roll trim at cut point between adjacent clips (P4.3).

    Roll trim moves the cut point between two clips:
    - Left clip's right edge moves
    - Right clip's left edge moves opposite
    - Timeline duration remains constant

    The handle is active at the junction between two adjacent clips only
    while the Roll tool is selected. The native horizontal resize cursor is
    the sole visual feedback.

    Signals:
        roll_trim_started: Roll trim drag started (left_clip_id, right_clip_id, cut_frame)
        roll_trim_dragged: Roll trim dragging (delta_frames) - live preview
        roll_trim_finished: Roll trim drag finished (left_clip_id, right_clip_id, delta_frames)
        roll_handle_clicked: Handle clicked for selection

    Attributes:
        _left_clip_id: Left (earlier) clip ID
        _right_clip_id: Right (later) clip ID
        _cut_frame: Frame position of the cut point
        _pixels_per_frame: Zoom scale
        _track_index: Track vertical position
        _track_height: Height of the track
        _is_hovered: Whether handle is hovered
        _is_dragging: Whether handle is being dragged
        _drag_start_frame: Frame where drag started
        _drag_start_pos: Scene position where drag started
    """

    # Signals
    roll_trim_started = pyqtSignal(str, str, int)  # left_clip_id, right_clip_id, cut_frame
    roll_trim_dragged = pyqtSignal(int)  # delta_frames (live preview)
    roll_trim_finished = pyqtSignal(str, str, int)  # left_clip_id, right_clip_id, delta_frames
    roll_handle_clicked = pyqtSignal(str, str)  # left_clip_id, right_clip_id

    def __init__(
        self,
        left_clip_id: str,
        right_clip_id: str,
        cut_frame: int,
        track_id: str,
        pixels_per_frame: float,
        track_index: int,
        track_height: int = TRACK_HEIGHT,
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._left_clip_id = left_clip_id
        self._right_clip_id = right_clip_id
        self._cut_frame = cut_frame
        self._track_id = track_id
        self._pixels_per_frame = pixels_per_frame
        self._track_index = track_index
        self._track_height = track_height

        self._is_hovered = False
        self._is_dragging = False
        self._drag_start_frame = 0
        self._drag_start_pos = QPointF()
        self._current_delta = 0  # Current delta during drag
        self._is_track_locked = False
        self._active_tool = EditTool.SELECT

        # Original state storage (for command)
        self._original_cut_frame = cut_frame

        # Enable hover events
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        # Set Z-value above clips
        self.setZValue(500)

        # Update position
        self._update_geometry()

        logger.debug(
            f"RollHandleItem created at cut point {cut_frame} "
            f"between {left_clip_id} and {right_clip_id}"
        )

    # ==================== Properties ====================

    @property
    def left_clip_id(self) -> str:
        return self._left_clip_id

    @property
    def right_clip_id(self) -> str:
        return self._right_clip_id

    @property
    def cut_frame(self) -> int:
        return self._cut_frame

    @property
    def track_id(self) -> str:
        return self._track_id

    # ==================== Geometry ====================

    def _update_geometry(self) -> None:
        """Update X position based on cut frame.

        NOTE: Y position is set by TimelineView._layout_track_items().
        """
        x = TRACK_HEADER_WIDTH + self._cut_frame * self._pixels_per_frame
        # Only update X; Y is managed externally by _layout_track_items()
        self.setX(x)

    def boundingRect(self) -> QRectF:
        """Return bounding rectangle for the handle."""
        # Centered at cut point, extends into both clips slightly
        return QRectF(-ROLL_HANDLE_SIZE / 2, 0, ROLL_HANDLE_SIZE, self._track_height)

    def shape(self) -> QPainterPath:
        """Return shape for collision detection."""
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    # ==================== Painting ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """Keep the roll seam visually clean; Qt owns the cursor feedback."""
        return

    # ==================== Interaction ====================

    def _update_cursor(self) -> None:
        """Show horizontal trim feedback for tools that can edit a seam."""
        if self._active_tool in {
            EditTool.SELECT,
            EditTool.TRIM,
            EditTool.RIPPLE,
            EditTool.ROLL,
        }:
            self.setCursor(
                Qt.CursorShape.ForbiddenCursor
                if self._is_track_locked
                else Qt.CursorShape.SizeHorCursor
            )
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def hoverEnterEvent(self, event: Any) -> None:
        """Handle hover enter - show handle and change cursor."""
        if self._active_tool not in {
            EditTool.SELECT,
            EditTool.TRIM,
            EditTool.RIPPLE,
            EditTool.ROLL,
        }:
            self._is_hovered = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.ignore()
            return
        self._is_hovered = True
        self._update_cursor()
        if self._active_tool == EditTool.ROLL:
            self.setToolTip(
                f"Roll Trim\n"
                f"Cut point: frame {self._cut_frame}\n"
                f"Drag to move cut point between clips"
            )
        else:
            self.setToolTip(
                f"Trim edit point\n"
                f"Cut point: frame {self._cut_frame}\n"
                f"Drag to adjust"
            )
        self.update()
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event: Any) -> None:
        """Handle hover move."""
        if self._active_tool not in {
            EditTool.SELECT,
            EditTool.TRIM,
            EditTool.RIPPLE,
            EditTool.ROLL,
        }:
            self._is_hovered = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.ignore()
            return
        self._is_hovered = True
        self._update_cursor()
        self.update()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event: Any) -> None:
        """Handle hover leave."""
        self._is_hovered = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("")
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        """Handle mouse press to start roll trim drag."""
        if event.button() == Qt.MouseButton.LeftButton:
            if self._active_tool != EditTool.ROLL or self._is_track_locked:
                event.ignore()
                return
            # Store original state
            self._original_cut_frame = self._cut_frame
            self._drag_start_frame = self._cut_frame
            self._drag_start_pos = event.scenePos()
            self._current_delta = 0
            self._is_dragging = True

            # Emit started signal
            self.roll_trim_started.emit(
                self._left_clip_id,
                self._right_clip_id,
                self._cut_frame
            )

            logger.debug(
                f"Roll trim started at frame {self._cut_frame} "
                f"between {self._left_clip_id} and {self._right_clip_id}"
            )
            # Do not pass the press to QGraphicsObject's default handler.
            # The RollHandle owns the drag and must keep the scene mouse grab
            # so subsequent move events continue to arrive here.
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        """Handle mouse move for roll trim dragging."""
        if self._is_dragging:
            delta = event.scenePos() - self._drag_start_pos
            delta_frames = int(delta.x() / self._pixels_per_frame)

            # Update position for visual feedback
            self._current_delta = delta_frames
            self._cut_frame = self._original_cut_frame + delta_frames
            self._update_geometry()

            # Emit dragged signal for live preview
            self.roll_trim_dragged.emit(delta_frames)

            logger.debug(f"Roll trim dragging: delta = {delta_frames} frames")
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        """Handle mouse release to finish roll trim."""
        if event.button() == Qt.MouseButton.LeftButton and not self._is_dragging:
            # A canceled session can still receive its delayed release.
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._is_dragging:
            self._is_dragging = False

            # A click or a drag returning to the original seam still ends the
            # session and closes the monitor, without adding a command.
            self.roll_trim_finished.emit(
                self._left_clip_id,
                self._right_clip_id,
                self._current_delta
            )

            if self._current_delta != 0:
                logger.debug(
                    f"Roll trim finished: delta = {self._current_delta} frames "
                    f"between {self._left_clip_id} and {self._right_clip_id}"
                )

            # Reset visual position (timeline will reload with actual changes)
            self._cut_frame = self._original_cut_frame
            self._current_delta = 0
            self._update_geometry()
            self.update()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    # ==================== State Updates ====================

    def set_active_tool(self, tool: EditTool) -> None:
        """Enable Roll interaction only while the Roll tool is selected."""
        self._active_tool = tool
        self.setAcceptedMouseButtons(
            Qt.MouseButton.LeftButton
            if tool == EditTool.ROLL
            else Qt.MouseButton.NoButton
        )
        if tool != EditTool.ROLL and not self._is_dragging:
            self._is_hovered = False
            self.setToolTip("")
        if not self._is_dragging:
            self._update_cursor()
        self.update()

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale."""
        self._pixels_per_frame = pixels_per_frame
        self._update_geometry()
        self.update()

    def update_track_index(self, track_index: int) -> None:
        """Update track vertical position."""
        self._track_index = track_index
        self._update_geometry()
        self.update()

    def update_cut_frame(self, cut_frame: int) -> None:
        """Update cut frame position (after timeline reload)."""
        self._cut_frame = cut_frame
        self._original_cut_frame = cut_frame
        self._update_geometry()
        self.update()

    def set_preview_delta(self, delta_frames: int) -> None:
        """Apply a clamped live-preview delta supplied by TimelineView."""
        self._current_delta = int(delta_frames)
        self._cut_frame = self._original_cut_frame + self._current_delta
        self._update_geometry()
        self.update()

    def cancel_drag(self) -> None:
        """RI-01/R13：外部取消（Esc/切工具/失焦）——复位拖拽状态与鼠标
        抓取且不发 finished。此前取消只复位 view 侧状态，handle 仍持抓
        取并在 mouseReleaseEvent 提交 delta（审计实测 Esc→release 仍提
        交，cut 149→188）。
        """
        if not self._is_dragging:
            return
        self._is_dragging = False
        self._current_delta = 0
        self._cut_frame = self._original_cut_frame
        try:
            self.ungrabMouse()
        except RuntimeError:
            # 抓取可能已被场景释放（如窗口失焦时 Qt 主动回收）
            pass
        self._update_geometry()
        self.update()

    def set_track_locked(self, locked: bool) -> None:
        """Disable roll trim on locked tracks while retaining the hit target."""
        self._is_track_locked = bool(locked)
        if not self._is_dragging:
            self._update_cursor()
        self.update()

    def set_track_height(self, height: int) -> None:
        """Update track height."""
        self._track_height = height
        self._update_geometry()
        self.update()


# ================================================================
# DragPreviewItem - Enhanced Drag Proxy (参考 kdenlive dragProxy)
# ================================================================

class DragPreviewItem(QGraphicsObject):
    """Translucent media or transition footprint on the destination lane."""

    MIN_WIDTH = 8.0
    INVALID_COLOR = QColor(150, 62, 62, 190)
    INVALID_BORDER = QColor(222, 112, 105)

    # 缩略图尺寸
    THUMBNAIL_WIDTH = 60
    LABEL_HEIGHT = 16
    ITEM_MARGIN = 4

    def __init__(
        self,
        pixels_per_frame: float = 5.0,
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._pixels_per_frame = pixels_per_frame
        self._clip_height = float(TRACK_HEIGHT) - 2.0
        self._frame = 0
        self._is_valid = True

        # 素材数据
        self._asset_names: list[str] = []
        self._asset_types: list[str] = []  # 'video', 'audio', 'image'
        self._asset_durations: list[int] = []  # 每个素材的真实时长（帧）
        self._total_duration: int = 0
        self._thumbnails: dict[str, QPixmap] = {}  # asset_name -> pixmap

        # Z-value above clips
        self.setZValue(700)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        # Initially hidden
        self.hide()

    # ==================== Geometry ====================

    def boundingRect(self) -> QRectF:
        if not self._asset_names:
            return QRectF()
        width = max(self.MIN_WIDTH, self._total_duration * self._pixels_per_frame)
        return QRectF(0, 0, width, self._clip_height)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def set_track_height(self, height: float) -> None:
        clip_height = max(1.0, height - 2.0)
        if self._clip_height != clip_height:
            self.prepareGeometryChange()
            self._clip_height = clip_height
            self.update()

    # ==================== State Updates ====================

    def set_assets(
        self,
        asset_names: list[str],
        asset_types: list[str],
        total_duration: int,
        thumbnails: dict[str, QPixmap] | None = None,
        durations: list[int] | None = None
    ) -> None:
        """
        Set the assets being dragged.

        Args:
            asset_names: List of asset display names
            asset_types: List of media types ('video', 'audio', 'image')
            total_duration: Total duration in frames
            thumbnails: Optional dict of cached thumbnails (asset_name -> QPixmap)
            durations: Each asset's duration in frames, matching asset_names.
        """
        if durations and len(durations) == len(asset_names):
            asset_durations = [max(1, int(duration)) for duration in durations]
        elif asset_names:
            fallback = max(1, total_duration // len(asset_names))
            asset_durations = [fallback] * len(asset_names)
        else:
            asset_durations = []
        thumbnails = thumbnails or {}
        if (
            asset_names == self._asset_names
            and asset_types == self._asset_types
            and total_duration == self._total_duration
            and asset_durations == self._asset_durations
            and thumbnails == self._thumbnails
        ):
            return
        if bool(asset_names) != bool(self._asset_names) or total_duration != self._total_duration:
            self.prepareGeometryChange()
        self._asset_names = asset_names
        self._asset_types = asset_types
        self._asset_durations = asset_durations
        self._total_duration = total_duration
        self._thumbnails = thumbnails
        self.update()

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale."""
        if pixels_per_frame == self._pixels_per_frame:
            return
        self.prepareGeometryChange()
        self._pixels_per_frame = pixels_per_frame
        self._update_geometry()
        self.update()

    # Keep the drag proxy on the same zoom scale as clips, transitions, and
    # the layout engine.  Without this, zooming left the proxy at its
    # constructor default (5 px/frame).
    def set_pixels_per_frame(self, pixels_per_frame: float) -> None:
        """Set the frame-to-pixel scale used by the drag proxy."""
        self.update_zoom(float(pixels_per_frame))

    def _update_geometry(self) -> None:
        """Update position based on track and frame.

        NOTE: Y position is set by TimelineView via setPos() during layout.
        This only computes X from frame. If called independently (e.g. during
        drag preview), Y uses a rough estimate — the view will correct it.
        """
        x = TRACK_HEADER_WIDTH + self._frame * self._pixels_per_frame
        # Only update X; Y should be set by the view's layout engine
        self.setX(x)

    # ==================== Visibility ====================

    def show_preview(
        self,
        frame: int,
        track_top: float,
        track_height: float,
        asset_names: list[str],
        asset_types: list[str],
        total_duration: int,
        thumbnails: dict[str, QPixmap] | None = None,
        durations: list[int] | None = None,
        is_valid: bool = True
    ) -> None:
        """Show preview with all parameters."""
        self.set_assets(asset_names, asset_types, total_duration, thumbnails, durations)
        self._frame = frame
        if self._is_valid != is_valid:
            self._is_valid = is_valid
            self.update()
        self.set_track_height(track_height)
        self.setY(track_top)
        self._update_geometry()
        self.show()

    def hide_preview(self) -> None:
        """Hide the preview and invalidate its former footprint."""
        if not self._asset_names:
            return
        self.prepareGeometryChange()
        self._asset_names = []
        self._asset_types = []
        self._asset_durations = []
        self._total_duration = 0
        self._thumbnails = {}
        self.hide()

    # ==================== Painting ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """Paint the drag preview with enhanced visuals."""
        if not self._asset_names:
            return

        bounds = self.boundingRect()
        painter.save()
        painter.setClipRect(bounds, Qt.ClipOperation.IntersectClip)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._asset_types and self._asset_types[0] == "transition":
            self._draw_transition_preview(painter)
        else:
            pixels_per_frame = bounds.width() / max(1, self._total_duration)
            x = 0.0
            for i, (name, duration) in enumerate(zip(self._asset_names, self._asset_durations)):
                media_type = self._asset_types[i] if i < len(self._asset_types) else "video"
                width = duration * pixels_per_frame
                self._draw_single_preview(
                    painter, x, 0.0, width, bounds.height(), name, media_type, i == 0
                )
                x += width

            if len(self._asset_names) > 1 and bounds.width() >= 80 and bounds.height() >= 28:
                self._draw_count_badge(painter, bounds.width(), bounds.height(), len(self._asset_names))
        painter.restore()

    def _draw_transition_preview(self, painter: QPainter) -> None:
        """Draw the translucent transition range shown while dragging."""
        rect = self.boundingRect()
        width = rect.width()
        height = rect.height()
        top = QColor(231, 181, 84, 170)
        bottom = QColor(122, 82, 31, 125)

        painter.save()
        painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        painter.setPen(QPen(QColor(255, 218, 120, 220), 1))
        gradient = QLinearGradient(0, 0, 0, height)
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, bottom)
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(rect, 4, 4)

        painter.setPen(QPen(QColor(255, 255, 255, 36), 1))
        span = int(height)
        for i in range(-span, int(width) + span, 8):
            painter.drawLine(
                QPointF(float(i), rect.bottom()),
                QPointF(float(i + span), rect.top()),
            )

        painter.setPen(QPen(QColor(255, 255, 255, 235), 1))
        painter.setFont(QFont("Arial", 8, QFont.Weight.DemiBold))
        label = self._asset_names[0] if self._asset_names else "Transition"
        text_rect = rect.adjusted(7, 0, -7, 0)
        text = painter.fontMetrics().elidedText(
            label, Qt.TextElideMode.ElideRight, max(8, int(text_rect.width()))
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)

        handle = QColor(255, 210, 95, 230)
        painter.setBrush(QBrush(handle))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(QRectF(0, 3, 4, height - 6), 2, 2)
        painter.drawRoundedRect(QRectF(width - 4, 3, 4, height - 6), 2, 2)
        painter.restore()

    def _draw_single_preview(
        self,
        painter: QPainter,
        x: float,
        y: float,
        width: float,
        height: float,
        name: str,
        media_type: str,
        show_content: bool
    ) -> None:
        """Draw a single preview item with thumbnail and label."""
        rect = QRectF(x, y, width, height)
        color = QColor(CLIP_AUDIO_COLOR if media_type == "audio" else CLIP_VIDEO_COLOR)
        border = color.lighter(180)
        color.setAlpha(215)
        if not self._is_valid:
            color, border = self.INVALID_COLOR, self.INVALID_BORDER

        painter.fillRect(rect, color)
        if width > 1.0 and height > 1.0:
            painter.setPen(QPen(border, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))

        if show_content and width > 30 and height >= self.LABEL_HEIGHT + self.ITEM_MARGIN * 2:
            thumb = self._thumbnails.get(name)
            if thumb and width > self.THUMBNAIL_WIDTH + self.ITEM_MARGIN * 2:
                self._draw_thumbnail(painter, x, y, height, thumb)
            self._draw_label(painter, x, y, width, height, name, media_type)

    def _draw_thumbnail(
        self,
        painter: QPainter,
        x: float,
        y: float,
        height: float,
        pixmap: QPixmap
    ) -> None:
        """Draw thumbnail in the preview item."""
        # AUD-21105: 拖拽预览缩略图来自外部媒体池，提取失败时可能传入
        # null/零尺寸 QPixmap；paint 内除零会中断绘制（对照 ClipItem.
        # _draw_single_thumbnail_at 的防护）。跳过缩略图只画标签。
        if pixmap.width() <= 0 or pixmap.height() <= 0:
            return
        avail_h = height - self.ITEM_MARGIN * 2 - self.LABEL_HEIGHT
        if avail_h <= 0:
            return

        # 缩略图位置（左上角）
        thumb_x = x + self.ITEM_MARGIN
        thumb_y = y + self.ITEM_MARGIN

        # 计算缩放比例，保持比例
        thumb_scale = min(
            self.THUMBNAIL_WIDTH / pixmap.width(),
            avail_h / pixmap.height()
        )
        scaled_width = int(pixmap.width() * thumb_scale)
        scaled_height = int(pixmap.height() * thumb_scale)

        if scaled_width > 0 and scaled_height > 0:
            scaled_pixmap = pixmap.scaled(
                scaled_width, scaled_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            painter.drawPixmap(int(thumb_x), int(thumb_y), scaled_pixmap)

    def _draw_label(
        self,
        painter: QPainter,
        x: float,
        y: float,
        width: float,
        height: float,
        name: str,
        media_type: str
    ) -> None:
        """Draw asset name label at the bottom of preview."""
        # 标签位置（底部）
        label_y = y + height - self.LABEL_HEIGHT - self.ITEM_MARGIN
        label_width = min(width - self.ITEM_MARGIN * 2, 100)

        # 背景
        label_rect = QRectF(x + self.ITEM_MARGIN, label_y, label_width, self.LABEL_HEIGHT)

        # 根据媒体类型选择颜色
        if media_type == 'audio':
            label_bg_color = QColor(50, 100, 70, 180)
        else:
            label_bg_color = QColor(50, 80, 130, 180)

        painter.setBrush(QBrush(label_bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(label_rect, 2, 2)

        # 文字
        painter.setPen(QPen(QColor(255, 255, 255, 230)))
        font = QFont("Arial", 9)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)

        text_rect = label_rect.adjusted(3, 0, -3, 0)
        text = painter.fontMetrics().elidedText(
            name, Qt.TextElideMode.ElideRight, int(text_rect.width())
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, text)

    def _draw_count_badge(
        self,
        painter: QPainter,
        width: float,
        height: float,
        count: int
    ) -> None:
        """Draw count badge when dragging multiple assets."""
        badge_size = 20
        badge_x = width - badge_size - 4
        badge_y = height - badge_size - 4

        # 绘制徽章背景（圆形）
        painter.setBrush(QBrush(QColor(255, 136, 0)))  # Orange accent
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawEllipse(QRectF(badge_x, badge_y, badge_size, badge_size))

        # 绘制数量文字
        painter.setPen(QPen(QColor(255, 255, 255)))
        font = QFont("Arial", 10)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(badge_x, badge_y, badge_size, badge_size),
                        Qt.AlignmentFlag.AlignCenter, str(count))


# ================================================================
# GapIndicatorItem - 片段间隙可视化 (Phase 3.3)
# ================================================================

class GapIndicatorItem(QGraphicsRectItem):
    """
    Visual indicator for gaps between clips on a track.

    参考 kdenlive 时间线间隙显示：
    - 半透明斜线图案背景
    - 虚线边框
    - 位于 clips 下方作为背景
    - 不响应鼠标事件（pass through）

    Attributes:
        _track_id: Track containing this gap
        _track_index: Track vertical position
        _start_frame: Gap start frame
        _duration_frames: Gap duration in frames
        _pixels_per_frame: Zoom scale
        _track_height: Track height for vertical sizing
    """

    # Z-value: 在 clips (z=200) 下方，作为背景
    GAP_Z_VALUE = 100

    # 最小间隙宽度才显示（避免过小的间隙创建过多 item）
    MIN_GAP_DISPLAY_FRAMES = 3

    def __init__(
        self,
        track_id: str,
        track_index: int,
        start_frame: int,
        duration_frames: int,
        pixels_per_frame: float,
        track_height: int = TRACK_HEIGHT,
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._track_id = track_id
        self._track_index = track_index
        self._start_frame = start_frame
        self._duration_frames = duration_frames
        self._pixels_per_frame = pixels_per_frame
        self._track_height = track_height

        # Set Z-value (below clips)
        self.setZValue(self.GAP_Z_VALUE)

        # 不响应鼠标事件（pass through to track background）
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

        # 更新几何
        self._update_geometry()

        logger.debug(f"GapIndicatorItem created: track={track_id}, "
                     f"start={start_frame}, duration={duration_frames}")

    # ==================== Properties ====================

    @property
    def track_id(self) -> str:
        return self._track_id

    @property
    def start_frame(self) -> int:
        return self._start_frame

    @property
    def end_frame(self) -> int:
        return self._start_frame + self._duration_frames

    @property
    def duration_frames(self) -> int:
        return self._duration_frames

    # ==================== Geometry ====================

    def _update_geometry(self) -> None:
        """Update item geometry based on frame position and zoom.

        NOTE: Y position is set by TimelineView._layout_track_items().
        """
        x = TRACK_HEADER_WIDTH + self._start_frame * self._pixels_per_frame
        width = self._duration_frames * self._pixels_per_frame
        # Only update X; Y is managed externally by _layout_track_items()
        self.setX(x)
        self.setRect(0, 0, width, self._track_height)

    def update_zoom(self, pixels_per_frame: float) -> None:
        """
        Update zoom scale.

        Args:
            pixels_per_frame: New zoom scale
        """
        self._pixels_per_frame = pixels_per_frame
        self._update_geometry()

    def update_track_height(self, track_height: int) -> None:
        """
        Update track height.

        Args:
            track_height: New track height
        """
        self._track_height = track_height
        self._update_geometry()

    def update_gap(self, start_frame: int, duration_frames: int) -> None:
        """
        Update gap position and duration.

        Args:
            start_frame: New start frame
            duration_frames: New duration
        """
        self._start_frame = start_frame
        self._duration_frames = duration_frames
        self._update_geometry()

    # ==================== Painting ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """
        Paint gap indicator with diagonal stripe pattern.

        参考 kdenlive 时间线间隙显示：
        - bg_input 颀色半透明填充
        - 斜线图案（45度角，间距 4px）
        - border_subtle 虚线边框
        """
        gap_rect = self.rect()

        if gap_rect.width() <= 0 or gap_rect.height() <= 0:
            return

        # 使用设计 token（bg_input 半透明 + border_subtle）
        bg_color = QColor(get_color("bg_input"))
        bg_color.setAlpha(180)  # 半透明

        border_color = QColor(get_color("border_subtle"))
        border_color.setAlpha(150)

        # 填充背景（半透明）
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRect(gap_rect)

        # 绘制斜线图案（参考 kdenlive 的间隙显示风格）
        stripe_color = QColor(80, 80, 85, 60)  # 半透明灰色
        painter.setPen(QPen(stripe_color, 1))

        # 斜线间距 4px，从左上到右下
        spacing = 4
        width = int(gap_rect.width())
        height = int(gap_rect.height())

        # 绘制斜线（45度，从左上到右下）
        for offset in range(-height, width + height, spacing):
            # 斜线方程: y = -x + offset  =>  从 (max(0, offset-height), min(height, offset)) 到 (min(width, offset), max(0, offset-width))
            x1 = max(0, offset - height)
            y1 = min(height, offset)
            x2 = min(width, offset)
            y2 = max(0, offset - width)
            if x1 < width and x2 > 0:
                painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        # 绘制虚线边框
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        dashed_pen = QPen(border_color, 1)
        dashed_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(dashed_pen)
        painter.drawRect(gap_rect)

    # ==================== Visibility ====================

    def update_visibility(self, visible_start: int, visible_end: int) -> None:
        """
        Update visibility based on viewport frame range.

        Args:
            visible_start: First visible frame
            visible_end: Last visible frame
        """
        # 检查间隙是否在可见范围内
        gap_start = self._start_frame
        gap_end = self._start_frame + self._duration_frames

        is_visible = gap_end >= visible_start and gap_start <= visible_end
        self.setVisible(is_visible)


# ================================================================
# MarkerItem - 项目标记可视化 (Phase 5.1)
# ================================================================

class MarkerItem(QGraphicsObject):
    """
    Visual representation of a timeline marker.

    参考 Kdenlive 的 marker 设计：
    - 位置：时间线 ruler 下方，轨道区域顶部
    - 外观：彩色倒三角 + marker 名称标签
    - Z-value: 950（在 clips 和 ruler 之上）
    - 可拖动调整位置
    - 双击编辑名称/颜色

    Signals:
        marker_clicked: Marker was clicked (marker_id)
        marker_moved: Marker position changed during drag (marker_id, new_frame)
        marker_double_clicked: Marker was double-clicked for editing (marker_id)
        marker_delete_requested: Delete marker requested (marker_id)

    Attributes:
        _marker_id: Unique marker identifier
        _frame: Timeline frame position
        _name: Marker display name
        _color: Marker color (hex string)
        _pixels_per_frame: Zoom scale
        _num_tracks: Number of tracks (for positioning)
        _is_selected: Selection state
        _is_dragging: Dragging state
    """

    # Signals
    marker_clicked = pyqtSignal(str)  # marker_id
    marker_moved = pyqtSignal(str, int)  # marker_id, new_frame (live preview)
    marker_move_finished = pyqtSignal(str, int, int)  # marker_id, old_frame, new_frame (command)
    marker_double_clicked = pyqtSignal(str)  # marker_id - for editing
    marker_delete_requested = pyqtSignal(str)  # marker_id
    marker_context_menu_requested = pyqtSignal(str, object)  # marker_id, global QPoint

    # Marker visual constants
    MARKER_Z_VALUE = 950  # 在 clips 和 ruler 之上，保证标签不会被标尺覆盖
    TRIANGLE_SIZE = 12  # 倒三角尺寸
    CLIP_MARKER_INSET_Y = 18  # 片段 Marker 从轨道顶部向片段内部偏移的距离
    LABEL_HEIGHT = 16  # 标签高度
    LABEL_MAX_WIDTH = 80  # 标签最大宽度
    LABEL_OFFSET_Y = 2  # 标签与三角的间距
    HANDLE_MARGIN = 8  # 拖拽响应区域边距

    # Colors (使用设计 token)
    MARKER_LABEL_BG = QColor(30, 30, 35, 200)  # 标签背景半透明
    MARKER_LABEL_TEXT = QColor(255, 255, 255, 230)  # 标签文字
    MARKER_SELECTED_BORDER = QColor(255, 136, 0)  # 选中边框橙色

    def __init__(
        self,
        marker_id: str,
        frame: int,
        name: str,
        color: str,
        pixels_per_frame: float,
        num_tracks: int = 1,
        fps: float = 24.0,
        clip_id: str | None = None,
        track_id: str | None = None,
        kind: str = "standard",
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._marker_id = marker_id
        self._frame = frame
        self._name = name
        self._color = color  # hex string like "#ff0000"
        self._pixels_per_frame = pixels_per_frame
        self._num_tracks = num_tracks
        self._fps = fps
        self._clip_id = clip_id
        self._track_id = track_id
        self._kind = kind  # "standard" | "chapter" (C5)
        self._target_y: float = float(RULER_HEIGHT)
        self._is_selected: bool = False
        self._is_dragging: bool = False
        self._drag_start_frame: int = 0
        self._drag_start_pos: QPointF = QPointF()

        # Parse color
        self._marker_color = QColor(color)
        if not self._marker_color.isValid():
            self._marker_color = QColor("#ff0000")  # Default red

        # Set Z-value
        self.setZValue(self.MARKER_Z_VALUE)

        # Enable interactions
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        # Update geometry
        self._update_geometry()

        logger.debug(f"MarkerItem created: {marker_id} at frame {frame}")

    # ==================== Properties ====================

    @property
    def marker_id(self) -> str:
        return self._marker_id

    @property
    def frame(self) -> int:
        return self._frame

    @property
    def name(self) -> str:
        return self._name

    @property
    def marker_color(self) -> QColor:
        return self._marker_color

    # ==================== Geometry ====================

    def boundingRect(self) -> QRectF:
        """
        Return the compact hit rectangle for the colored marker tag.

        Marker 位于 ruler 下方，轨道区域顶部。
        垂直位置固定在 ruler 底部。
        """
        # Keep the name in the model for editing, but do not render it on the
        # timeline itself.
        triangle_height = self.TRIANGLE_SIZE
        width = self.TRIANGLE_SIZE + self.HANDLE_MARGIN
        return QRectF(
            -self.HANDLE_MARGIN / 2,
            -triangle_height,
            width,
            triangle_height + self.HANDLE_MARGIN,
        )

    def shape(self) -> QPainterPath:
        """Return the triangle hit shape."""
        path = QPainterPath()
        triangle_height = self.TRIANGLE_SIZE
        half_size = self.TRIANGLE_SIZE / 2

        # Triangle shape
        triangle = QPolygonF([
            QPointF(-half_size, -triangle_height),
            QPointF(half_size, -triangle_height),
            QPointF(0, 0)
        ])
        path.addPolygon(triangle)

        return path

    def _update_geometry(self) -> None:
        """Update position from frame/zoom and the current vertical anchor."""
        # Timeline markers use the ruler bottom; clip markers use their lane top.
        x = TRACK_HEADER_WIDTH + self._frame * self._pixels_per_frame
        y = self._target_y

        self.setPos(x, y)

    # ==================== State Updates ====================

    def update_frame(self, frame: int) -> None:
        """Update marker frame position."""
        self._frame = frame
        self._update_geometry()
        self.update()

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale."""
        self._pixels_per_frame = pixels_per_frame
        self._update_geometry()
        self.update()

    def update_num_tracks(self, num_tracks: int) -> None:
        """Update number of tracks (for positioning)."""
        self._num_tracks = num_tracks
        self.update()

    def update_target_y(self, target_y: float) -> None:
        """Move the marker to the ruler or its scoped clip lane."""
        target_y = float(target_y)
        if self._target_y == target_y:
            return
        self._target_y = target_y
        self._update_geometry()
        self.update()

    def update_fps(self, fps: float) -> None:
        """Update frame rate for timecode display."""
        self._fps = fps
        self.update()

    def update_marker(
        self,
        frame: int | None = None,
        name: str | None = None,
        color: str | None = None
    ) -> None:
        """
        Update marker properties.

        Args:
            frame: New frame position (optional)
            name: New name (optional)
            color: New color hex string (optional)
        """
        if frame is not None:
            self._frame = frame
        if name is not None:
            self._name = name
        if color is not None:
            self._color = color
            self._marker_color = QColor(color)
            if not self._marker_color.isValid():
                self._marker_color = QColor("#ff0000")

        self._update_geometry()
        self.update()

    def set_selected(self, selected: bool) -> None:
        """Set selection state."""
        self._is_selected = selected
        self.update()

    # ==================== Painting ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[Any] = None
    ) -> None:
        """
        Paint the marker as a compact colored tag.

        绘制内容：
        1. 倒三角（彩色，指向下方）
        2. 选中状态边框
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 绘制倒三角
        self._draw_triangle(painter)

        # 绘制选中边框（如果选中）
        if self._is_selected:
            self._draw_selection_border(painter)

    def _draw_triangle(self, painter: QPainter) -> None:
        """Draw downward pointing triangle."""
        half_size = self.TRIANGLE_SIZE / 2
        triangle_height = self.TRIANGLE_SIZE

        # Triangle points (pointing downward)
        triangle = QPolygonF([
            QPointF(-half_size, -triangle_height),
            QPointF(half_size, -triangle_height),
            QPointF(0, 0)
        ])

        # Fill with marker color
        painter.setBrush(QBrush(self._marker_color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(triangle)

        # Draw outline (slightly darker)
        outline_color = QColor(self._marker_color)
        outline_color = outline_color.darker(150)
        painter.setPen(QPen(outline_color, 1))
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawPolygon(triangle)

        # C5: chapter markers carry a white "C" badge in the triangle
        if self._kind == "chapter":
            painter.setPen(QPen(QColor(255, 255, 255, 235)))
            font = QFont("Arial", 7)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(
                QRectF(-half_size, -triangle_height, self.TRIANGLE_SIZE,
                       triangle_height - 2),
                Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignHCenter,
                "C",
            )

    def _draw_label(self, painter: QPainter) -> None:
        """Draw name label below triangle."""
        label_width = min(self.LABEL_MAX_WIDTH, len(self._name) * 8 + 8)
        label_rect = QRectF(-label_width / 2, self.LABEL_OFFSET_Y,
                            label_width, self.LABEL_HEIGHT)

        # Background (semi-transparent dark)
        painter.setBrush(QBrush(self.MARKER_LABEL_BG))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(label_rect, 3, 3)

        # Text
        painter.setPen(QPen(self.MARKER_LABEL_TEXT))
        font = QFont("Arial", 9)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)

        # Truncate name if too long
        display_name = self._truncate_name(self._name, label_width - 4)
        painter.drawText(label_rect, Qt.TextFlag.TextSingleLine |
                         Qt.AlignmentFlag.AlignHCenter |
                         Qt.AlignmentFlag.AlignVCenter, display_name)

    def _draw_selection_border(self, painter: QPainter) -> None:
        """Draw selection highlight border around marker."""
        # Highlight triangle
        half_size = self.TRIANGLE_SIZE / 2 + 2
        triangle_height = self.TRIANGLE_SIZE + 2

        # Draw glow effect around triangle
        glow_color = QColor(self.MARKER_SELECTED_BORDER)
        glow_color.setAlpha(100)
        painter.setPen(QPen(glow_color, 3))
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))

        triangle = QPolygonF([
            QPointF(-half_size, -triangle_height),
            QPointF(half_size, -triangle_height),
            QPointF(0, 0)
        ])
        painter.drawPolygon(triangle)

    def _truncate_name(self, name: str, max_width: float) -> str:
        """Truncate name to fit in label width."""
        char_width = 8  # Approximate character width
        max_chars = int(max_width / char_width)
        if len(name) <= max_chars:
            return name
        return name[:max_chars - 2] + ".."

    # ==================== Interaction ====================

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press for selection and drag start."""
        if event.button() == Qt.MouseButton.LeftButton:
            # Start drag
            self._is_dragging = True
            self._drag_start_frame = self._frame
            self._drag_start_pos = event.pos()

            # Select
            self._is_selected = True
            self.marker_clicked.emit(self._marker_id)

            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            # Select first, then let TimelineView provide the context menu.
            self.marker_clicked.emit(self._marker_id)
            self.marker_context_menu_requested.emit(
                self._marker_id,
                event.screenPos(),
            )
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move for dragging."""
        if self._is_dragging and event.buttons() & Qt.MouseButton.LeftButton:
            # Calculate new frame position
            delta_x = event.pos().x() - self._drag_start_pos.x()
            delta_frames = int(delta_x / self._pixels_per_frame)

            new_frame = max(0, self._drag_start_frame + delta_frames)

            if new_frame != self._frame:
                self._frame = new_frame
                self._update_geometry()
                self.update()

                # Emit live preview signal
                self.marker_moved.emit(self._marker_id, new_frame)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release to end drag."""
        if event.button() == Qt.MouseButton.LeftButton and self._is_dragging:
            self._is_dragging = False

            # Final position update
            self._update_geometry()
            self.update()

            # Emit move finished signal for command (if position changed)
            if self._frame != self._drag_start_frame:
                self.marker_move_finished.emit(
                    self._marker_id,
                    self._drag_start_frame,
                    self._frame
                )

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Handle double-click to edit marker name/color."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.marker_double_clicked.emit(self._marker_id)

        super().mouseDoubleClickEvent(event)

    def hoverEnterEvent(self, event: Any) -> None:
        """Handle hover enter to show highlight."""
        # Highlight on hover
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: Any) -> None:
        """Handle hover leave."""
        self.update()
        super().hoverLeaveEvent(event)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:
        """Handle item changes."""
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            # Constrain to valid horizontal position (keep Y fixed)
            new_pos = value
            if isinstance(new_pos, QPointF):
                # Keep Y position fixed at the current ruler/clip anchor.
                # The old hard-coded 25 placed the marker inside the ruler
                # and allowed the ruler (Z=900) to cover it.
                new_pos = QPointF(new_pos.x(), self._target_y)
                return new_pos
        return super().itemChange(change, value)

    # ==================== Visibility ====================

    def update_visibility(self, visible_start: int, visible_end: int) -> None:
        """
        Update visibility based on viewport frame range.

        Args:
            visible_start: First visible frame
            visible_end: Last visible frame
        """
        is_visible = self._frame >= visible_start - 10 and self._frame <= visible_end + 10
        self.setVisible(is_visible)


# ================================================================
# P4: TimelineRangeMaskItem — dim outside the In/Out selection
# ================================================================

class TimelineRangeMaskItem(QGraphicsObject):
    """Non-interactive highlight over the selected In/Out span."""

    Z_VALUE = 150  # Above clips, below the ruler, range handles, and playhead.
    HIGHLIGHT_COLOR = QColor(210, 214, 226, 16)

    def __init__(
        self,
        pixels_per_frame: float,
        parent: Optional[QGraphicsItem] = None,
    ) -> None:
        super().__init__(parent)
        self._pixels_per_frame = pixels_per_frame
        self._range_start: Optional[int] = None
        self._range_end: Optional[int] = None
        self._duration_frames: int = 0
        self._track_area_top: float = float(TRACK_AREA_TOP)
        self._track_area_height: float = 0.0
        self.setZValue(self.Z_VALUE)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setVisible(False)

    def set_range(
        self,
        start: Optional[int],
        end: Optional[int],
        duration_frames: int,
        track_area_top: float,
        track_area_height: float,
    ) -> None:
        """Update the selection and its vertical span across the track area."""
        self.prepareGeometryChange()
        self._range_start = start
        self._range_end = end
        self._duration_frames = max(0, duration_frames)
        self._track_area_top = max(float(TRACK_AREA_TOP), track_area_top)
        self._track_area_height = max(0.0, track_area_height)
        self.setVisible(
            start is not None
            and end is not None
            and end >= start
            and self._duration_frames > 0
            and self._track_area_height > 0.0
        )
        self.update()

    def set_pixels_per_frame(self, pixels_per_frame: float) -> None:
        """Update horizontal scale after a timeline zoom change."""
        if pixels_per_frame == self._pixels_per_frame:
            return
        self.prepareGeometryChange()
        self._pixels_per_frame = pixels_per_frame
        self.update()

    def boundingRect(self) -> QRectF:
        if self._range_start is None or self._range_end is None:
            return QRectF()
        start = max(0, self._range_start)
        end = min(self._duration_frames, self._range_end + 1)
        return QRectF(
            TRACK_HEADER_WIDTH + start * self._pixels_per_frame,
            self._track_area_top,
            max(0, end - start) * self._pixels_per_frame,
            self._track_area_height,
        )

    def paint(
        self,
        painter: QPainter,
        option: Any,
        widget: Optional[Any] = None,
    ) -> None:
        painter.fillRect(self.boundingRect(), self.HIGHLIGHT_COLOR)


# ================================================================
# P4: TimelineRangeItem — In/Out range bar on the ruler
# ================================================================

class TimelineRangeItem(QGraphicsObject):
    """Visual In/Out range bar drawn at the bottom of the ruler area.

    Shows a semi-transparent colored bar between range_start and range_end,
    with draggable handles at each end.

    Signals:
        range_handle_dragged: (edge, new_frame) during live drag
        range_change_finished: (new_start, new_end) on mouse release
    """

    range_handle_dragged = pyqtSignal(str, int)  # edge ('in'/'out'), frame
    range_change_finished = pyqtSignal(int, int)  # new_start, new_end

    BAR_HEIGHT = 4
    HANDLE_RADIUS = 3.5
    HANDLE_WIDTH = 9
    HANDLE_HEIGHT = 16
    Z_VALUE = 1001  # Above ruler and playhead so handles always win the hit test.

    def __init__(
        self,
        pixels_per_frame: float,
        fps: float = 24.0,
        parent: Optional[QGraphicsItem] = None,
    ) -> None:
        super().__init__(parent)
        self._pixels_per_frame = pixels_per_frame
        self._fps = fps
        self._range_start: Optional[int] = None
        self._range_end: Optional[int] = None
        self._dragging_edge: Optional[str] = None  # 'in' or 'out'
        self._ruler_height = RULER_HEIGHT
        self._bar_y = self._ruler_height - self.BAR_HEIGHT - 3
        self.setZValue(self.Z_VALUE)
        self.setAcceptHoverEvents(True)
        self.setVisible(False)

    def set_range(self, start: Optional[int], end: Optional[int]) -> None:
        """Update range boundaries."""
        self.prepareGeometryChange()
        self._range_start = start
        self._range_end = end
        self.setVisible(
            start is not None and end is not None and end >= start
        )
        self.update()

    def set_pixels_per_frame(self, ppf: float) -> None:
        if ppf == self._pixels_per_frame:
            return
        self.prepareGeometryChange()
        self._pixels_per_frame = ppf
        self.update()

    def boundingRect(self) -> QRectF:
        if self._range_start is None or self._range_end is None:
            return QRectF()
        x_start = TRACK_HEADER_WIDTH + self._range_start * self._pixels_per_frame - self.HANDLE_WIDTH
        x_end = TRACK_HEADER_WIDTH + (self._range_end + 1) * self._pixels_per_frame + self.HANDLE_WIDTH
        handle_top = self._bar_y - (self.HANDLE_HEIGHT - self.BAR_HEIGHT) / 2
        return QRectF(
            x_start,
            handle_top,
            x_end - x_start,
            self.HANDLE_HEIGHT,
        )

    def paint(self, painter: QPainter, option: Any, widget: Optional[Any] = None) -> None:
        if self._range_start is None or self._range_end is None:
            return

        x_start = TRACK_HEADER_WIDTH + self._range_start * self._pixels_per_frame
        x_end = TRACK_HEADER_WIDTH + (self._range_end + 1) * self._pixels_per_frame

        bar_rect = QRectF(x_start, self._bar_y, x_end - x_start, self.BAR_HEIGHT)
        painter.fillRect(bar_rect, QColor("#82848B"))

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for x, edge in ((x_start, "in"), (x_end, "out")):
            color = QColor("#D0D1D5" if edge == self._dragging_edge else "#A4A6AC")
            painter.setBrush(color)
            painter.setPen(QPen(QColor("#313237"), 1))
            painter.drawEllipse(
                QPointF(x, bar_rect.center().y()), self.HANDLE_RADIUS, self.HANDLE_RADIUS
            )
        painter.restore()

    def _hit_edge(self, pos: QPointF) -> Optional[str]:
        if self._range_start is None or self._range_end is None:
            return None
        x_start = TRACK_HEADER_WIDTH + self._range_start * self._pixels_per_frame
        x_end = TRACK_HEADER_WIDTH + (self._range_end + 1) * self._pixels_per_frame
        in_distance = abs(pos.x() - x_start)
        out_distance = abs(pos.x() - x_end)
        if min(in_distance, out_distance) < self.HANDLE_WIDTH:
            return "in" if in_distance <= out_distance else "out"
        return None

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._hit_edge(event.pos())
            if edge:
                self._dragging_edge = edge
                self.update()
                event.accept()
                return
        super().mousePressEvent(event)

    def _dragged_frame_at(self, x: float) -> int:
        frame = max(0, int((x - TRACK_HEADER_WIDTH) / self._pixels_per_frame))
        if self._dragging_edge == "out" and self._range_start is not None:
            return max(self._range_start, frame - 1)
        if self._range_end is not None:
            return min(self._range_end, frame)
        return frame

    def mouseMoveEvent(self, event: Any) -> None:
        if self._dragging_edge and self._pixels_per_frame > 0:
            self.range_handle_dragged.emit(self._dragging_edge, self._dragged_frame_at(event.pos().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._dragging_edge and event.button() == Qt.MouseButton.LeftButton:
            frame = self._dragged_frame_at(event.pos().x())
            new_start = frame if self._dragging_edge == "in" else self._range_start
            new_end = frame if self._dragging_edge == "out" else self._range_end
            self._dragging_edge = None
            self.update()
            if new_start is not None and new_end is not None:
                self.range_change_finished.emit(new_start, new_end)
            event.accept()
            return
        super().mouseReleaseEvent(event)
