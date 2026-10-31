"""
TimelineClipStrip — Color page timeline clip strip panel.

Displays a horizontal scrollable strip of clip cards representing
all video/image clips from the active Edit timeline. Clicking a card
opens the clip in the Color grading page.

Part of P9.1: Edit-Color page round-trip integration.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum

from PyQt6.QtCore import pyqtSignal, Qt, QPoint, QSettings, QTimer
from PyQt6.QtGui import QShortcut, QKeySequence
from PyQt6.QtGui import QColor, QPainter, QPixmap, QFont, QResizeEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.features.color.core.curves_soft_clip import curves_soft_clip_is_identity
from src.shared.i18n.i18n import I18N
from src.shared.media.thumbnail_provider import ThumbnailProvider

logger = logging.getLogger(__name__)
_COLOR_CLIP_DEBUG_ENABLED = os.environ.get("TOPOS_COLOR_CLIP_DEBUG", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _color_clip_debug_enabled() -> bool:
    return _COLOR_CLIP_DEBUG_ENABLED or logger.isEnabledFor(logging.DEBUG)


def _emit_color_clip_debug(message: str, *args: object) -> None:
    if not _color_clip_debug_enabled():
        return
    logger.debug(message, *args)

# ---------------------------------------------------------------------------
# Dark-theme palette constants (matching the rest of the app)
# ---------------------------------------------------------------------------
_BG_DARK = "#27272b"
_BG_CARD = "#2f2f33"
_BG_CARD_HOVER = "#3a3a40"
_BG_CARD_SELECTED = "#3d5a80"
_BORDER_NORMAL = "#444444"
_BORDER_SELECTED = "#5b9bd5"
_TEXT_PRIMARY = "#cccccc"
_TEXT_SECONDARY = "#888888"
_TEXT_ACCENT = "#5b9bd5"
_TEXT_OVERLAY = "#ffffff"
_GRADED_LABEL_GRADIENT = (
    "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
    "stop:0 #315c68, stop:0.48 #5b477a, stop:1 #8a5a42)"
)

# The strip is height-driven. These values are only compact lower bounds;
# normal card geometry is calculated from the strip viewport at runtime.
_MIN_CARD_HEIGHT = 72
_CARD_VERTICAL_MARGINS = 8
_CARD_CONTENT_SPACING = 2
_THUMBNAIL_ASPECT_RATIO = 16.0 / 9.0

# Card dimensions (new)
_THUMBNAIL_HEIGHT = 72
_BOTTOM_LABEL_HEIGHT = 18
_CARD_HEIGHT_NEW = _THUMBNAIL_HEIGHT + _BOTTOM_LABEL_HEIGHT + 6  # 96

# AUD-25702：viewport 高度 ≤ 0（隐藏/零高度 dock）时自适应重试的上限；
# 超过后停止空转，等待下一次 resizeEvent/卡片重建重新启动。
_MAX_VIEWPORT_FIT_RETRIES = 16


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ClipPreviewMode(Enum):
    """Preview frame position for clip thumbnails."""
    FIRST_FRAME = "first"
    MIDDLE_FRAME = "middle"
    LAST_FRAME = "last"
    NO_PREVIEW = "none"


class BottomLabelMode(Enum):
    """What to display at the bottom of the card."""
    CLIP_NAME = "name"
    CODEC = "codec"


class CardSize(Enum):
    """Card display size presets."""
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


# Card size presets
CARD_SIZE_PRESETS = {
    CardSize.SMALL: {
        "width": 90,
        "thumb_height": 48,
        "bottom_height": 16,
        "total_height": 70,  # 48 + 16 + 6
        "font_size": 9,
        "bottom_font_size": 9,
    },
    CardSize.MEDIUM: {
        "width": 132,
        "thumb_height": 72,
        "bottom_height": 18,
        "total_height": 96,  # 72 + 18 + 6
        "font_size": 11,
        "bottom_font_size": 11,
    },
    CardSize.LARGE: {
        "width": 164,
        "thumb_height": 90,
        "bottom_height": 20,
        "total_height": 116,  # 90 + 20 + 6
        "font_size": 12,
        "bottom_font_size": 12,
    },
}


# ---------------------------------------------------------------------------
# Settings Helpers
# ---------------------------------------------------------------------------

def _get_preview_mode_setting() -> ClipPreviewMode:
    settings = QSettings("ToposColor", "TimelineClipStrip")
    mode_str = settings.value("previewMode", "middle")
    try:
        return ClipPreviewMode(mode_str)
    except ValueError:
        return ClipPreviewMode.MIDDLE_FRAME


def _save_preview_mode_setting(mode: ClipPreviewMode) -> None:
    settings = QSettings("ToposColor", "TimelineClipStrip")
    settings.setValue("previewMode", mode.value)


def _get_bottom_label_mode_setting() -> BottomLabelMode:
    settings = QSettings("ToposColor", "TimelineClipStrip")
    mode_str = settings.value("bottomLabelMode", "name")
    try:
        return BottomLabelMode(mode_str)
    except ValueError:
        return BottomLabelMode.CLIP_NAME


def _save_bottom_label_mode_setting(mode: BottomLabelMode) -> None:
    settings = QSettings("ToposColor", "TimelineClipStrip")
    settings.setValue("bottomLabelMode", mode.value)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _format_timecode(frame: int, fps: float = 24.0) -> str:
    """Convert frame number to timecode string."""
    if frame < 0:
        return "--:--:--:--"
    total_seconds = frame / fps
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    frames = int(frame % int(fps))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def _format_clip_index(index: int, total: int) -> str:
    """Format clip index for display."""
    if total < 10:
        return f"{index}"
    elif total < 100:
        return f"{index:02d}"
    else:
        return f"{index:03d}"


def _get_video_codec(asset_path: str) -> str:
    """Get video codec from file extension."""
    ext = os.path.splitext(asset_path)[1].lower()
    codec_map = {
        ".mp4": "H.264",
        ".mov": "ProRes",
        ".avi": "AVI",
        ".mkv": "MKV",
        ".webm": "VP9",
        ".mxf": "MXF",
    }
    return codec_map.get(ext, ext.upper().replace(".", "") if ext else "VIDEO")


def _compute_preview_frame(entry, mode: ClipPreviewMode) -> int:
    """Compute the frame number to display based on preview mode."""
    if mode == ClipPreviewMode.NO_PREVIEW:
        return -1
    if entry.source_duration <= 0:
        return entry.source_start
    if mode == ClipPreviewMode.FIRST_FRAME:
        return entry.source_start
    elif mode == ClipPreviewMode.LAST_FRAME:
        return entry.source_start + entry.source_duration - 1
    else:
        return entry.source_start + entry.source_duration // 2


def _metadata_int(metadata: object, key: str, default: int = 0) -> int:
    try:
        if isinstance(metadata, dict):
            value = metadata.get(key, default)
        else:
            value = getattr(metadata, key, default)
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return int(default)


def _is_subclip_asset(asset: object) -> bool:
    metadata = getattr(asset, "metadata", None) or {}
    if isinstance(metadata, dict):
        return bool(metadata.get("is_subclip", False))
    return bool(getattr(metadata, "is_subclip", False))


def _effective_clip_source_range(clip: object, asset: object) -> tuple[int, int]:
    """Return the absolute source range used by this timeline clip.

    Scene-cut subclip assets reference the parent media path and store their
    real source offset in asset metadata.  Older or imported projects can still
    have timeline ClipInstance.source_start=0 for those assets, which would make
    Color preview the parent material's first frame.  Prefer an explicit clip
    source_start when present; otherwise use the subclip metadata range.
    """
    clip_start = _metadata_int(clip, "source_start", 0)
    clip_duration = _metadata_int(clip, "source_duration", 0)
    metadata = getattr(asset, "metadata", None) or {}
    if _is_subclip_asset(asset) and clip_start <= 0:
        meta_start = _metadata_int(metadata, "source_start", 0)
        meta_duration = _metadata_int(metadata, "source_duration", 0)
        if meta_start > 0:
            clip_start = meta_start
        if clip_duration <= 0 and meta_duration > 0:
            clip_duration = meta_duration
    return clip_start, clip_duration


def _values_differ(left: object, right: object) -> bool:
    """Compare serialized color values while tolerating list/tuple changes."""
    if isinstance(left, dict) and isinstance(right, dict):
        keys = set(left) | set(right)
        return any(_values_differ(left.get(key), right.get(key)) for key in keys)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            return True
        return any(_values_differ(a, b) for a, b in zip(left, right))
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) > 1e-6
    return left != right


def _stack_field(stack: object, key: str, default: object = None) -> object:
    """Read a ColorStack field from either a dataclass or serialized dict."""
    if isinstance(stack, dict):
        return stack.get(key, default)
    return getattr(stack, key, default)


def _mapping_differs_from_defaults(value: object, defaults: dict[str, object]) -> bool:
    """Return whether a mapping contains a meaningful non-default value.

    Color panels serialize a few compatibility/default fields that are not in
    ``ColorStackData``'s canonical defaults (for example ``temperature`` and
    ``shadows``), and some reset paths serialize all-zero mixer values.  An
    unknown key is therefore only a grade when its value is itself meaningful;
    its mere presence must not turn the clip marker on.
    """
    if not isinstance(value, dict):
        return _values_differ(value, defaults)

    for key, item in value.items():
        if key in defaults:
            if _values_differ(item, defaults[key]):
                return True
        elif _has_meaningful_value(item):
            return True
    return False


def _has_meaningful_value(value: object) -> bool:
    """Return whether *value* can represent a non-default effect."""
    if isinstance(value, dict):
        return any(_has_meaningful_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_meaningful_value(item) for item in value)
    if isinstance(value, (int, float)):
        return abs(float(value)) > 1e-6
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _color_stack_has_grade(stack: object | None) -> bool:
    """Return whether a ColorStack contains a meaningful user grade."""
    if stack is None:
        return False

    default_grading = {
        "lift": (0, 0, 0),
        "gamma": (0, 0, 0),
        "gain": (0, 0, 0),
        "exposure": 0,
        "temp": 0,
        "tint": 0,
        "contrast": 0,
        "shadow": 0,
        "highlights": 0,
        "saturation": 0,
        "vibrance": 0,
        "mid_detail": 0,
        "sharpness": 0,
        "hue_shift": 0,
    }
    default_soft_clip = {
        "enabled": False,
        "high": 1.0,
        "white_point": 1.1,
        "shoulder_strength": 1.0,
        "low": 0.0,
        "black_point": -0.1,
        "toe_strength": 1.0,
        "exposure": 0.0,
    }

    grading_params = _stack_field(stack, "grading_params") or {}
    if _mapping_differs_from_defaults(grading_params, default_grading):
        return True
    # P1-COLOR-03: 纯关键帧调色（无静态调色值）也是已调色，否则镜头
    # 过滤/调色徽标/脚本 API has_grade 会把它误判为未调色。
    for layer_dict in _stack_field(stack, "layers") or ():
        if isinstance(layer_dict, dict) and layer_dict.get("grade_keyframes"):
            return True
    if _stack_field(stack, "curves_data"):
        return True
    if _stack_field(stack, "hsl_curve_data"):
        return True
    # MixerPanel keeps all eight colour rows in the serialized payload even
    # after reset.  Only a non-zero component is an actual mixer adjustment.
    if _has_meaningful_value(_stack_field(stack, "mixer_params")):
        return True
    soft_clip_params = _stack_field(stack, "soft_clip_params") or {}
    if _mapping_differs_from_defaults(soft_clip_params, default_soft_clip):
        return True
    # 曲线面板侧栏：四参数柔化裁切非恒等即已调色（Edit 强度已折算进
    # curves LUT，经上方 curves_data 覆盖）
    if not curves_soft_clip_is_identity(_stack_field(stack, "curves_soft_clip")):
        return True
    if _stack_field(stack, "lut_path"):
        return True
    return _values_differ(_stack_field(stack, "lut_intensity", 1.0), 1.0)


def _clip_has_explicit_grade(
    stack: object | None,
    clip_id: str,
    color_shared: bool,
) -> bool:
    """Return whether a grade belongs explicitly to this timeline clip.

    A scene split keeps the source clip's local stack reference for backward
    compatibility.  Once ownership metadata is available, that inherited
    reference must not make every new segment look graded; shared asset grades
    remain intentionally visible on every clip marked as shared.
    """
    if not _color_stack_has_grade(stack):
        return False
    if color_shared:
        return True

    scope = _stack_field(stack, "scope", "clip_local")
    owner_clip_id = _stack_field(stack, "clip_id")
    if scope == "clip_local" and owner_clip_id and owner_clip_id != clip_id:
        return False
    return True


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ColorClipEntry:
    """Lightweight data-transfer object for a single clip in the strip."""

    timeline_id: str
    clip_id: str
    track_id: str
    track_name: str
    asset_id: str
    asset_name: str
    asset_path: str
    media_type: str  # "video" or "image"
    timeline_start: int
    timeline_duration: int
    source_start: int
    source_duration: int
    color_stack_id: str | None
    color_shared: bool
    color_graded: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_duration(frames: int) -> str:
    """Return a short duration string."""
    if frames < 0:
        return "?"
    return f"{frames}f"


def _normalize_fps(value: object, default: float = 24.0) -> float:
    """Normalize timeline fps metadata (Rational/int/float) to a positive float.

    AUD-25701：卡片时间码此前硬编码 24 fps；时间线模型携带的 fps 可能
    是 Rational（num/den，如 24000/1001）、int 或 float，统一在此归一。
    """
    if value is None:
        return default
    if hasattr(value, "to_fps"):
        value = value.to_fps()
    elif hasattr(value, "to_float"):
        value = value.to_float()
    elif hasattr(value, "num") and hasattr(value, "den"):
        den = getattr(value, "den", 0) or 1
        value = getattr(value, "num", 0) / den
    try:
        fps = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return fps if fps > 0.0 else default


def build_clip_entries(
    project,
    timeline,
    timeline_id: str,
) -> list[ColorClipEntry]:
    """
    Build a sorted list of ColorClipEntry from project + timeline.

    Only video/image clips are included. Entries are sorted by
    ``timeline_start`` ascending.

    Args:
        project: EditProject instance (must have ``assets`` dict)
        timeline: TimelineSequence instance
        timeline_id: Timeline sequence ID string

    Returns:
        Sorted list of ColorClipEntry
    """
    if project is None or timeline is None:
        return []

    entries: list[ColorClipEntry] = []

    # Only consider video tracks
    for track in timeline.video_tracks:
        for clip in track.clips:
            # Look up asset
            asset = project.assets.get(clip.asset_id)
            if asset is None:
                continue

            # Filter: only video and image
            if not (asset.is_video or asset.is_image):
                continue

            source_start, source_duration = _effective_clip_source_range(clip, asset)
            entries.append(
                ColorClipEntry(
                    timeline_id=timeline_id,
                    clip_id=clip.clip_id,
                    track_id=track.track_id,
                    track_name=track.name or f"V{track.index + 1}",
                    asset_id=asset.asset_id,
                    asset_name=asset.display_name or os.path.basename(asset.path),
                    asset_path=asset.path,
                    media_type=asset.media_type,
                    timeline_start=clip.timeline_start,
                    timeline_duration=clip.timeline_duration,
                    source_start=source_start,
                    source_duration=source_duration,
                    color_stack_id=clip.color_stack_id,
                    color_shared=clip.color_shared,
                    color_graded=_clip_has_explicit_grade(
                        (getattr(project, "color_stacks", None) or {}).get(
                            clip.color_stack_id,
                        )
                        if clip.color_stack_id else None,
                        clip.clip_id,
                        clip.color_shared,
                    ),
                )
            )

    # Sort by timeline_start ascending
    entries.sort(key=lambda e: e.timeline_start)
    return entries


# ---------------------------------------------------------------------------
# ThumbnailWidget - Custom widget for thumbnail with overlay text
# ---------------------------------------------------------------------------

class ThumbnailWidget(QWidget):
    """Widget that draws thumbnail with overlay info (no background boxes)."""

    def __init__(self, card_size: CardSize = CardSize.MEDIUM, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._clip_index: str = ""
        self._timecode: str = ""
        self._track_name: str = ""
        self._card_size = card_size
        self._font_size = CARD_SIZE_PRESETS[card_size]["font_size"]
        self._apply_size()

    def _apply_size(self) -> None:
        """Apply size based on card_size preset."""
        preset = CARD_SIZE_PRESETS[self._card_size]
        self.setFixedHeight(preset["thumb_height"])
        self.setMinimumWidth(preset["width"] - 4)

    def set_card_size(self, size: CardSize) -> None:
        """Update card size and reapply."""
        self._card_size = size
        self._apply_size()
        self.update()

    def set_display_size(self, width: int, height: int, font_size: int) -> None:
        """Set the runtime size used by the height-adaptive clip strip."""
        self._font_size = max(8, int(font_size))
        self.setFixedSize(max(1, int(width)), max(1, int(height)))
        self.update()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def set_info(self, clip_index: str, timecode: str, track_name: str) -> None:
        self._clip_index = clip_index
        self._timecode = timecode
        self._track_name = track_name
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Background
        painter.fillRect(self.rect(), QColor("#1a1a1a"))

        # Thumbnail
        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                self.width(), self.height(),
                # Fill the card without stretching.  Any aspect-ratio
                # mismatch is handled by a centered crop, so source matte
                # or viewer black bars cannot become part of the card UI.
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.setPen(QColor("#666666"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "...")

        # Overlay text with shadow
        font = QFont()
        font.setFamily("Helvetica")
        font.setPointSize(self._font_size)
        font.setBold(True)
        painter.setFont(font)
        fm = painter.fontMetrics()

        margin = 5
        y = margin + fm.ascent()

        # Left: clip index (with shadow)
        if self._clip_index:
            painter.setPen(QColor(0, 0, 0, 180))
            painter.drawText(margin + 1, y + 1, self._clip_index)
            painter.setPen(QColor(_TEXT_OVERLAY))
            painter.drawText(margin, y, self._clip_index)

        # Center: timecode (with shadow)
        if self._timecode:
            x = (self.width() - fm.horizontalAdvance(self._timecode)) // 2
            painter.setPen(QColor(0, 0, 0, 180))
            painter.drawText(x + 1, y + 1, self._timecode)
            painter.setPen(QColor(_TEXT_OVERLAY))
            painter.drawText(x, y, self._timecode)

        # Right: track name (with shadow)
        if self._track_name:
            x = self.width() - margin - fm.horizontalAdvance(self._track_name)
            painter.setPen(QColor(0, 0, 0, 180))
            painter.drawText(x + 1, y + 1, self._track_name)
            painter.setPen(QColor(_TEXT_OVERLAY))
            painter.drawText(x, y, self._track_name)

        painter.end()


# ---------------------------------------------------------------------------
# TimelineClipCard
# ---------------------------------------------------------------------------

class TimelineClipCard(QFrame):
    """A single clip card widget with thumbnail preview.

    Signals:
        clicked(str, str, int, int): Emits
            (timeline_id, clip_id, timeline_start, source_start) on mouse press.
    """

    clicked = pyqtSignal(str, str, int, int)

    def __init__(
        self,
        entry: ColorClipEntry,
        clip_index: int = 1,
        total_clips: int = 1,
        preview_mode: ClipPreviewMode = ClipPreviewMode.MIDDLE_FRAME,
        bottom_mode: BottomLabelMode = BottomLabelMode.CLIP_NAME,
        fps: float = 24.0,
        card_size: CardSize = CardSize.MEDIUM,
        parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._entry = entry
        self._clip_index = clip_index
        self._total_clips = total_clips
        self._preview_mode = preview_mode
        self._bottom_mode = bottom_mode
        self._fps = fps
        self._card_size = card_size
        self._selected = False
        self._pixmap: QPixmap | None = None
        self._thumbnail_requested: bool = False
        self._preview_override_active: bool = False
        self._display_height = CARD_SIZE_PRESETS[card_size]["total_height"]
        self._bottom_height = CARD_SIZE_PRESETS[card_size]["bottom_height"]
        self._bottom_font_size = CARD_SIZE_PRESETS[card_size]["bottom_font_size"]
        self._typography_scale = {
            CardSize.SMALL: 0.88,
            CardSize.MEDIUM: 1.0,
            CardSize.LARGE: 1.12,
        }[card_size]

        # Apply size preset
        preset = CARD_SIZE_PRESETS[card_size]
        self.setFixedSize(preset["width"], preset["total_height"])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._build_ui()
        self._apply_style()

    # ---- UI construction ----

    def _build_ui(self) -> None:
        preset = CARD_SIZE_PRESETS[self._card_size]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        # Thumbnail area (top) - with overlay info
        self._thumb_widget = ThumbnailWidget(self._card_size, self)
        self._thumb_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._thumb_widget)

        # Bottom row: codec or name
        self._bottom_label = QLabel()
        self._bottom_label.setObjectName("bottomLabel")
        self._bottom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bottom_label.setFixedHeight(preset["bottom_height"])
        self._bottom_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._update_bottom_label()
        layout.addWidget(self._bottom_label)

        self._update_overlay_info()

    def _update_overlay_info(self) -> None:
        """Update overlay info on thumbnail."""
        index_text = _format_clip_index(self._clip_index, self._total_clips)
        timecode = _format_timecode(self._entry.timeline_start, self._fps)
        self._thumb_widget.set_info(index_text, timecode, self._entry.track_name)

    def set_fps(self, fps: float) -> None:
        """Update the card's timecode frame rate and refresh the overlay.

        AUD-25701：项目帧率变化时无需重建卡片即可修正时间码叠层。
        """
        normalized = _normalize_fps(fps)
        if normalized == self._fps:
            return
        self._fps = normalized
        self._update_overlay_info()

    def _update_bottom_label(self) -> None:
        """Update bottom label text based on mode."""
        if self._bottom_mode == BottomLabelMode.CODEC:
            if self._entry.media_type == "image":
                ext = os.path.splitext(self._entry.asset_path)[1].upper()
                text = ext.replace(".", "") if ext else "IMAGE"
            else:
                text = _get_video_codec(self._entry.asset_path)
        else:
            name = self._entry.asset_name
            text = name[:20] if len(name) > 20 else name
        self._bottom_label.setText(text)
        if self._entry.color_graded:
            background = _GRADED_LABEL_GRADIENT
            text_color = "#f4f4f4"
            border = "1px solid #74658f"
            tooltip = "Color graded"
        else:
            background = "transparent"
            text_color = _TEXT_SECONDARY
            border = "none"
            tooltip = ""
        self._bottom_label.setToolTip(tooltip)
        self._bottom_label.setStyleSheet(f"""
            QLabel#bottomLabel {{
                color: {text_color};
                font-size: {self._bottom_font_size}px;
                background: {background};
                border: {border};
                border-radius: 2px;
            }}
        """)

    def _apply_style(self) -> None:
        bg = _BG_CARD_SELECTED if self._selected else _BG_CARD
        border = _BORDER_SELECTED if self._selected else _BORDER_NORMAL
        self.setStyleSheet(f"""
            TimelineClipCard {{
                background: {bg};
                border: 1px solid {border};
                border-radius: 4px;
            }}
            TimelineClipCard:hover {{
                background: {_BG_CARD_HOVER};
                border-color: #555;
            }}
            QLabel#thumbLabel {{
                background: #1a1a1a;
                border-radius: 2px;
            }}
        """)

    # ---- Thumbnail handling ----

    def ensure_thumbnail_requested(self) -> None:
        """Request thumbnail if not already requested.

        Called lazily by TimelineClipStrip based on scroll position
        rather than automatically in the constructor.
        """
        if self._thumbnail_requested or self._preview_override_active:
            return
        self._thumbnail_requested = True
        self._request_thumbnail()

    def _request_thumbnail(self) -> None:
        """Request thumbnail from provider.

        This method no longer connects to provider signals directly.
        The parent TimelineClipStrip manages signal routing via pending
        maps and calls the card's update methods directly.
        """
        provider = ThumbnailProvider.instance()
        if self._entry.media_type == "image":
            # Check cache first - get_thumbnail returns cached pixmap if available
            cached = provider.get_thumbnail(self._entry.asset_path, icon_size=144)
            if cached:
                # Use cached result directly
                self._pixmap = cached
                self._thumb_widget.set_pixmap(cached)
            else:
                # Trigger async load; strip will route the result back to us
                provider.get_thumbnail(self._entry.asset_path, icon_size=144)
        else:
            frame_num = _compute_preview_frame(self._entry, self._preview_mode)
            if frame_num < 0:
                return
            total_frames = max(1, self._entry.source_start + self._entry.source_duration)
            provider.get_frame_thumbnail(
                self._entry.asset_path, frame_num, total_frames, icon_size=144,
                asset_id=self._entry.asset_id,
            )

    def _on_thumbnail_ready(self, path: str, pixmap: QPixmap) -> None:
        if not self._preview_override_active and path == self._entry.asset_path and pixmap:
            self._pixmap = pixmap
            self._thumb_widget.set_pixmap(pixmap)

    def _on_frame_thumbnail_ready(self, path: str, frame_index: int, pixmap: QPixmap) -> None:
        if self._preview_override_active or path != self._entry.asset_path:
            return
        expected = _compute_preview_frame(self._entry, self._preview_mode)
        if abs(frame_index - expected) > 5:
            return
        if pixmap:
            self._pixmap = pixmap
            self._thumb_widget.set_pixmap(pixmap)

    def set_preview_override(self, pixmap: QPixmap) -> None:
        """Use a finished graded preview instead of the raw source thumbnail."""
        if pixmap.isNull():
            return
        self._preview_override_active = True
        self._thumbnail_requested = True
        self._pixmap = pixmap
        self._thumb_widget.set_pixmap(pixmap)

    def update_bottom_mode(self, mode: BottomLabelMode) -> None:
        self._bottom_mode = mode
        self._update_bottom_label()

    def set_card_size(self, size: CardSize) -> None:
        """Update typography preset without discarding the current thumbnail."""
        self._card_size = size
        self._typography_scale = {
            CardSize.SMALL: 0.88,
            CardSize.MEDIUM: 1.0,
            CardSize.LARGE: 1.12,
        }[size]
        self._apply_display_geometry(self.width(), self._display_height)

    def _apply_display_geometry(self, width: int, height: int) -> None:
        """Resize the card and its children while preserving the thumbnail."""
        card_width = max(1, int(width))
        card_height = max(_MIN_CARD_HEIGHT, int(height))
        bottom_height = min(
            max(12, round(card_height * 0.15)),
            max(12, card_height - _CARD_VERTICAL_MARGINS - _CARD_CONTENT_SPACING - 22),
        )
        thumbnail_height = max(
            24,
            card_height - _CARD_VERTICAL_MARGINS - _CARD_CONTENT_SPACING - bottom_height,
        )
        overlay_font_size = max(
            9, min(18, round(thumbnail_height * 0.15 * self._typography_scale)),
        )
        bottom_font_size = max(
            9, min(15, round(bottom_height * 0.62 * self._typography_scale)),
        )

        self._display_height = card_height
        self._bottom_height = bottom_height
        self._bottom_font_size = bottom_font_size
        self.setFixedSize(card_width, card_height)
        self._thumb_widget.set_display_size(
            max(1, card_width - 4), thumbnail_height, overlay_font_size,
        )
        self._bottom_label.setFixedHeight(bottom_height)
        self._update_bottom_label()

    # ---- Selection ----

    @property
    def entry(self) -> ColorClipEntry:
        return self._entry

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        """Update visual selection state."""
        if self._selected == selected:
            return
        self._selected = selected
        self._apply_style()

    # ---- Mouse events ----

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            _emit_color_clip_debug(
                "[COLOR_CLIP_DEBUG] card.mouse_press timeline=%s clip=%s "
                "timeline_start=%s source_start=%s source_duration=%s asset=%s path=%s",
                self._entry.timeline_id,
                self._entry.clip_id,
                self._entry.timeline_start,
                self._entry.source_start,
                self._entry.source_duration,
                self._entry.asset_id,
                self._entry.asset_path,
            )
            self.clicked.emit(
                self._entry.timeline_id,
                self._entry.clip_id,
                int(self._entry.timeline_start),
                int(self._entry.source_start),
            )
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# TimelineClipStrip
# ---------------------------------------------------------------------------

class TimelineClipStrip(QWidget):
    """Horizontal scrollable strip of TimelineClipCards.

    Displays all video/image clips from the active Edit timeline.
    Clicking a card emits ``clip_activated`` which should be wired
    to ``open_timeline_clip_in_color``.

    Signals:
        clip_activated(str, str, int, int): Emits
            (timeline_id, clip_id, timeline_start, source_start).
        float_requested(): Request to float the panel.
        close_requested(): Request to close the panel.
    """

    clip_activated = pyqtSignal(str, str, int, int)
    float_requested = pyqtSignal()
    close_requested = pyqtSignal()
    # P1-COLOR-16 分类调色复制/粘贴（timeline_id, clip_id）
    grade_copy_requested = pyqtSignal(str, str)
    grade_paste_requested = pyqtSignal(str, str)
    # P1-COLOR-16 复查：批量粘贴（timeline_id）
    grade_paste_all_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[ColorClipEntry] = []
        self._cards: list[TimelineClipCard] = []
        self._active_clip_id: str | None = None
        self._current_project = None
        self._current_timeline = None
        self._current_timeline_id: str | None = None
        self._graded_preview_cache: dict[str, QPixmap] = {}
        # P1-COLOR-20: 按调色状态过滤（all / ungraded / graded）。
        # _all_entries 是过滤前的完整清单，过滤只影响显示与导航。
        self._all_entries: list[ColorClipEntry] = []
        self._grade_filter = "all"

        # Settings
        self._preview_mode = _get_preview_mode_setting()
        self._bottom_mode = _get_bottom_label_mode_setting()
        # Card dimensions are controlled by the dock height. CardSize remains
        # available for standalone TimelineClipCard callers, but is not a
        # second sizing control for the adaptive strip.
        self._card_size = CardSize.MEDIUM
        self._fps = 24.0

        # Lazy thumbnail loading state
        self._playback_active: bool = False
        self._previews_suppressed: bool = False  # True when 500+ clips forces NO_PREVIEW
        self._user_enabled_previews: bool = False  # User override for large timelines

        # Signal deduplication: generation token + pending maps
        self._thumbnail_generation: int = 0
        self._pending_image_cards: dict[tuple[int, str], list[TimelineClipCard]] = {}
        self._pending_frame_cards: dict[tuple[int, str, int], list[TimelineClipCard]] = {}
        self._geometry_followup_passes = 0
        # AUD-25702：viewport 高度 ≤ 0 时的自适应重试计数（隐藏/零高度
        # dock 下限制空转次数，resizeEvent 会在重新可见时重置预算）。
        self._viewport_fit_retries: int = 0

        self._build_ui()
        self._setup_context_menu()

        # Thumbnail debounce timer — fires after scroll settles
        self._thumbnail_debounce_timer = QTimer(self)
        self._thumbnail_debounce_timer.setSingleShot(True)
        self._thumbnail_debounce_timer.setInterval(120)
        self._thumbnail_debounce_timer.timeout.connect(self._request_visible_thumbnails)

        # Coalesce splitter resize events until Qt has updated the viewport.
        self._geometry_update_timer = QTimer(self)
        self._geometry_update_timer.setSingleShot(True)
        self._geometry_update_timer.timeout.connect(self._fit_cards_to_viewport)

        # Monitor scroll position for lazy loading
        self._scroll_area.horizontalScrollBar().valueChanged.connect(
            self._on_scroll_changed,
        )

        # Single connection to provider signals (instead of per-card connections)
        provider = ThumbnailProvider.instance()
        provider.thumbnail_ready.connect(self._on_strip_thumbnail_ready)
        provider.frame_thumbnail_ready.connect(self._on_strip_frame_thumbnail_ready)

        # Clean up signal connections when this widget is destroyed
        self.destroyed.connect(self._cleanup_signal_connections)

    # ---- UI construction ----

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # Scroll area
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(False)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn,
        )
        self._scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self._scroll_area.setStyleSheet(f"""
            QScrollArea {{
                background: {_BG_DARK};
                border: none;
            }}
            QScrollBar:horizontal {{
                background: #29292f;
                height: 9px;
                border: none;
                margin: 0 4px 1px 4px;
                border-radius: 4px;
            }}
            QScrollBar::groove:horizontal {{
                background: #29292f;
                height: 8px;
                border-radius: 4px;
            }}
            QScrollBar::handle:horizontal {{
                background: #72727c;
                border-radius: 3px;
                min-width: 48px;
                margin: 1px 0;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: #a0a0ab;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: #29292f;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0px;
            }}
        """)
        # Keep the native scrollbar in the layout even on macOS styles that
        # otherwise collapse an always-on bar to an almost invisible overlay.
        self._scroll_area.horizontalScrollBar().setMinimumHeight(9)
        self._scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        # Container widget inside scroll area
        self._container = QWidget()
        self._container.setStyleSheet(f"background: {_BG_DARK};")
        self._card_layout = QHBoxLayout(self._container)
        self._card_layout.setContentsMargins(4, 4, 4, 4)
        self._card_layout.setSpacing(4)
        self._card_layout.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )

        self._scroll_area.setWidget(self._container)
        # The scroll area fills the dock; cards are fitted to its viewport.
        outer_layout.addWidget(self._scroll_area)

        # Empty state label (shown when no clips)
        self._empty_label = QLabel(I18N.tr("clipstrip.no_clips", "No timeline clips"))
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(f"""
            QLabel {{
                color: {_TEXT_SECONDARY};
                font-size: 11px;
                background: {_BG_DARK};
                padding: 20px;
            }}
        """)
        self._empty_label.setVisible(False)
        outer_layout.addWidget(self._empty_label)

        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._update_strip_height()

        # P1-COLOR-20: 浮动镜头导航（上一镜/下一镜 + 位置标签）
        self._nav_container = QWidget(self)
        self._nav_container.setStyleSheet(
            "QWidget { background-color: rgba(255, 255, 255, 20);"
            " border-radius: 4px; }")
        nav_layout = QHBoxLayout(self._nav_container)
        nav_layout.setContentsMargins(3, 1, 3, 1)
        nav_layout.setSpacing(2)
        self._prev_clip_btn = self._make_nav_button("◀", -1)
        self._next_clip_btn = self._make_nav_button("▶", 1)
        self._nav_position_label = QLabel("")
        self._nav_position_label.setStyleSheet(
            "QLabel { background: transparent; color: rgba(255,255,255,170);"
            " font-size: 10px; padding: 0 2px; }")
        nav_layout.addWidget(self._prev_clip_btn)
        nav_layout.addWidget(self._nav_position_label)
        nav_layout.addWidget(self._next_clip_btn)
        self._nav_container.adjustSize()
        self._nav_container.move(self.width() - self._nav_container.width() - 14, 4)
        self._nav_container.raise_()

        # 键盘：↑/↓ 切镜（strip 聚焦时；DaVinci 同款 shot navigation）
        QShortcut(QKeySequence(Qt.Key.Key_Up), self,
                  activated=lambda: self.navigate_clip(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Down), self,
                  activated=lambda: self.navigate_clip(1))
        self._update_nav_buttons()
        self._update_nav_position_label()

    def _update_nav_position_label(self) -> None:
        label = getattr(self, "_nav_position_label", None)
        if label is None:
            return
        pos = self.active_clip_position()
        if pos is None:
            label.setText("")
        else:
            label.setText(f"{pos[0]}/{pos[1]}")

    def _update_strip_height(self) -> None:
        """Keep a compact minimum; normal height is supplied by the dock."""
        self.setMinimumHeight(_MIN_CARD_HEIGHT + _CARD_VERTICAL_MARGINS)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Refit cards after the dock or its splitter changes size."""
        super().resizeEvent(event)
        nav = getattr(self, "_nav_container", None)
        if nav is not None:
            nav.move(self.width() - nav.width() - 14, 4)
        geometry_timer = getattr(self, "_geometry_update_timer", None)
        if geometry_timer is not None:
            self._geometry_followup_passes = 1
            # 重新拿到真实 resize 意味着布局恢复——重置 AUD-25702 的
            # 零高度重试预算。
            self._viewport_fit_retries = 0
            geometry_timer.start(0)

    def _fit_cards_to_viewport(self) -> None:
        """Fit every card to the available panel height.

        Width follows a 16:9 preview ratio, so increasing the dock height
        enlarges both the thumbnail and the horizontal footprint of each clip.
        """
        if not self._cards:
            return

        viewport_height = self._scroll_area.viewport().height()
        if viewport_height <= 0:
            # AUD-25702：0 ms 定时器自续期会在隐藏/零高度 dock 下无限
            # 空转事件循环；限制重试次数，resizeEvent/重建卡片会重新
            # 启动一次完整的自适应流程。
            if self._viewport_fit_retries >= _MAX_VIEWPORT_FIT_RETRIES:
                return
            self._viewport_fit_retries += 1
            self._geometry_update_timer.start(0)
            return
        self._viewport_fit_retries = 0

        card_height = max(
            _MIN_CARD_HEIGHT,
            viewport_height - _CARD_VERTICAL_MARGINS,
        )
        bottom_height = min(
            max(12, round(card_height * 0.15)),
            max(12, card_height - _CARD_VERTICAL_MARGINS - _CARD_CONTENT_SPACING - 22),
        )
        thumbnail_height = max(
            24,
            card_height - _CARD_VERTICAL_MARGINS - _CARD_CONTENT_SPACING - bottom_height,
        )
        card_width = max(84, round(thumbnail_height * _THUMBNAIL_ASPECT_RATIO) + 4)

        for card in self._cards:
            card._apply_display_geometry(card_width, card_height)

        margins = 8
        total_width = margins + len(self._cards) * card_width + max(0, len(self._cards) - 1) * 4
        self._container.setFixedWidth(total_width)
        self._container.setFixedHeight(card_height + _CARD_VERTICAL_MARGINS)

        # The slim scrollbar consumes a few pixels of viewport height. A second
        # pass keeps the cards flush with the final viewport.
        if self._geometry_followup_passes > 0:
            self._geometry_followup_passes -= 1
            self._geometry_update_timer.start(0)

    # ---- Context Menu ----

    def _setup_context_menu(self) -> None:
        """Setup right-click context menu."""
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos: QPoint) -> None:
        """Show context menu with preview and panel options."""
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {_BG_DARK};
                border: 1px solid #444;
            }}
            QMenu::item {{
                color: {_TEXT_PRIMARY};
                padding: 6px 20px;
            }}
            QMenu::item:selected {{
                background: {_BG_CARD_HOVER};
            }}
            QMenu::separator {{
                height: 1px;
                background: #444;
                margin: 4px 10px;
            }}
        """)

        # Preview Frame submenu
        preview_menu = menu.addMenu("Preview Frame")
        for mode in [ClipPreviewMode.FIRST_FRAME, ClipPreviewMode.MIDDLE_FRAME, ClipPreviewMode.LAST_FRAME]:
            action = preview_menu.addAction(mode.value.replace("_", " ").title())
            action.setCheckable(True)
            action.setChecked(self._preview_mode == mode)
            action.triggered.connect(lambda _, m=mode: self._set_preview_mode(m))

        # Enable previews when suppressed (500+ clips)
        if self._previews_suppressed:
            menu.addSeparator()
            enable_action = menu.addAction(
                I18N.tr("clipstrip.enable_previews",
                        "Enable Previews (Large Timeline)"))
            enable_action.setToolTip(I18N.tr(
                "clipstrip.previews_disabled_tooltip",
                "Previews are disabled for performance. Click to enable."
            ))
            enable_action.triggered.connect(self._enable_previews_for_large_timeline)

        # Bottom Label submenu
        menu.addSeparator()
        bottom_menu = menu.addMenu("Bottom Label")
        name_action = bottom_menu.addAction(
            I18N.tr("clipstrip.label_clip_name", "Clip Name"))
        name_action.setCheckable(True)
        name_action.setChecked(self._bottom_mode == BottomLabelMode.CLIP_NAME)
        name_action.triggered.connect(lambda: self._set_bottom_mode(BottomLabelMode.CLIP_NAME))

        codec_action = bottom_menu.addAction(
            I18N.tr("clipstrip.label_codec", "Video Codec"))
        codec_action.setCheckable(True)
        codec_action.setChecked(self._bottom_mode == BottomLabelMode.CODEC)
        codec_action.triggered.connect(lambda: self._set_bottom_mode(BottomLabelMode.CODEC))

        # P1-COLOR-20: 按调色状态过滤镜头列表
        menu.addSeparator()
        filter_menu = menu.addMenu(
            I18N.tr("clipstrip.filter_menu", "镜头过滤"))
        filter_labels = {
            "all": I18N.tr("clipstrip.filter_all", "全部镜头"),
            "ungraded": I18N.tr("clipstrip.filter_ungraded", "未调色"),
            "graded": I18N.tr("clipstrip.filter_graded", "已调色"),
        }
        for mode in self.GRADE_FILTER_MODES:
            action = filter_menu.addAction(filter_labels[mode])
            action.setCheckable(True)
            action.setChecked(self._grade_filter == mode)
            action.triggered.connect(
                lambda _, m=mode: self.set_grade_filter(m))

        # P1-COLOR-16: grade copy/paste on the active clip
        active_entry = None
        if self._active_clip_id:
            for entry in self._entries:
                if entry.clip_id == self._active_clip_id:
                    active_entry = entry
                    break
        if active_entry is not None:
            menu.addSeparator()
            menu.addAction(
                I18N.tr("clipstrip.copy_grade", "Copy Grade"),
                lambda: self.grade_copy_requested.emit(
                    active_entry.timeline_id, active_entry.clip_id)
            )
            menu.addAction(
                I18N.tr("clipstrip.paste_grade_attrs",
                        "Paste Grade Attributes..."),
                lambda: self.grade_paste_requested.emit(
                    active_entry.timeline_id, active_entry.clip_id)
            )

        # P1-COLOR-16 复查：批量粘贴不依赖活动 clip——应用到当前
        # （过滤后）列表涉及的全部镜头
        if self._entries:
            target_timeline_id = self._entries[0].timeline_id
            menu.addAction(
                I18N.tr("clipstrip.paste_grade_all",
                        "Paste Grade to All Clips"),
                lambda: self.grade_paste_all_requested.emit(target_timeline_id))

        # Panel options
        menu.addSeparator()
        menu.addAction(I18N.tr("clipstrip.float_panel", "Float Panel"),
                       self.float_requested.emit)
        menu.addAction(I18N.tr("clipstrip.close_panel", "Close Panel"),
                       self.close_requested.emit)

        menu.exec(self.mapToGlobal(pos))

    # ---- Lazy thumbnail loading ----

    def _on_scroll_changed(self, _value: int) -> None:
        """Restart debounce timer on scroll; thumbnails load after it settles."""
        self._thumbnail_debounce_timer.start()

    def _request_visible_thumbnails(self) -> None:
        """Request thumbnails only for cards in/near the visible scroll area.

        Prefetch range is [start - 8, end + 8] to allow smooth scrolling.
        During playback, only consumes already-cached thumbnails and does
        not issue new decode requests.
        """
        if not self._cards:
            return

        card_w = max(1, self._cards[0].width())
        spacing = 4

        scroll_val = self._scroll_area.horizontalScrollBar().value()
        viewport_w = self._scroll_area.viewport().width()

        # Calculate visible index range
        start_idx = max(0, (scroll_val // (card_w + spacing)) - 1)
        end_idx = min(
            len(self._cards) - 1,
            ((scroll_val + viewport_w) // (card_w + spacing)) + 1,
        )

        # Prefetch window: 8 cards on each side
        prefetch_start = max(0, start_idx - 8)
        prefetch_end = min(len(self._cards) - 1, end_idx + 8)

        # AUD-25700：一次请求只判定一次播放状态（显式标记 + 全局注册表）。
        playback_active = self._playback_in_progress()

        for i in range(prefetch_start, prefetch_end + 1):
            card = self._cards[i]
            if playback_active:
                # During playback only consume cache, no new requests
                if card._thumbnail_requested:
                    continue
                # Allow cache-only resolution but skip network/decode
                provider = ThumbnailProvider.instance()
                if card._entry.media_type == "image":
                    cached = provider.get_cached_thumbnail(card._entry.asset_path, icon_size=144)
                    if cached:
                        card._thumbnail_requested = True
                        card._pixmap = cached
                        card._thumb_widget.set_pixmap(cached)
                # Skip video frame decode during playback
                continue

            # If thumbnail was already served from cache, nothing to register
            was_requested_before = card._thumbnail_requested
            if was_requested_before:
                continue

            # Register in pending maps BEFORE requesting, so that synchronous
            # cache-hit signals (e.g. get_frame_thumbnail emits frame_thumbnail_ready
            # immediately on cache hit) can find the card in the pending map.
            if card._entry.media_type == "image":
                self._register_image_card(card)
            else:
                frame_num = _compute_preview_frame(
                    card._entry, card._preview_mode
                )
                if frame_num >= 0:
                    self._register_frame_card(card, frame_num)

            card.ensure_thumbnail_requested()

            # If the card was served from cache synchronously, clean up
            # the pending map entry to avoid stale references.
            if card._pixmap is not None:
                if card._entry.media_type == "image":
                    key = (self._thumbnail_generation, card._entry.asset_path)
                    cards_list = self._pending_image_cards.get(key)
                    if cards_list and card in cards_list:
                        cards_list.remove(card)
                        if not cards_list:
                            self._pending_image_cards.pop(key, None)
                else:
                    frame_num = _compute_preview_frame(
                        card._entry, card._preview_mode
                    )
                    key = (self._thumbnail_generation, card._entry.asset_path, frame_num)
                    cards_list = self._pending_frame_cards.get(key)
                    if cards_list and card in cards_list:
                        cards_list.remove(card)
                        if not cards_list:
                            self._pending_frame_cards.pop(key, None)

    def set_playback_active(self, active: bool) -> None:
        """Notify the strip whether playback is currently active.

        When active, thumbnail requests are suppressed to avoid competing
        with real-time video decode.

        AUD-25700：该显式入口此前在全库无调用方（Edit 页同类防护由
        timeline_thumbnail_scheduler 接线，Color 页漏接），抑制分支是
        死代码。现在 ``_request_visible_thumbnails`` 在请求时刻还会读
        共享播放引擎注册表兜底（见 ``_playback_in_progress``），即使
        没有外部调用 set_playback_active，播放期间的解码抑制也生效。
        """
        self._playback_active = active

    def _playback_in_progress(self) -> bool:
        """Return whether playback is active for thumbnail suppression.

        显式标记优先；否则查询共享播放引擎注册表（任何引擎在播即视为
        播放中）。注册表读操作带轻量锁，仅在滚动防抖回调（120 ms）里
        调用一次，开销可忽略。
        """
        if self._playback_active:
            return True
        from src.shared.video.video_playback import _is_any_engine_playing
        return _is_any_engine_playing()

    # ---- Signal deduplication ----

    def _register_image_card(self, card: TimelineClipCard) -> None:
        """Register a card waiting for an image thumbnail result."""
        key = (self._thumbnail_generation, card._entry.asset_path)
        if key not in self._pending_image_cards:
            self._pending_image_cards[key] = []
        self._pending_image_cards[key].append(card)

    def _register_frame_card(self, card: TimelineClipCard, frame_index: int) -> None:
        """Register a card waiting for a video frame thumbnail result."""
        key = (self._thumbnail_generation, card._entry.asset_path, frame_index)
        if key not in self._pending_frame_cards:
            self._pending_frame_cards[key] = []
        self._pending_frame_cards[key].append(card)

    def _on_strip_thumbnail_ready(self, path: str, pixmap: QPixmap) -> None:
        """Handle image thumbnail ready — route to registered cards only.

        Only cards registered in the current generation receive the result.
        Stale results from previous generations are silently discarded.
        """
        if not pixmap:
            return
        gen = self._thumbnail_generation
        key = (gen, path)
        cards = self._pending_image_cards.pop(key, None)
        if cards is None:
            return
        for card in cards:
            card._on_thumbnail_ready(path, pixmap)

    def _on_strip_frame_thumbnail_ready(
        self, path: str, frame_index: int, pixmap: QPixmap
    ) -> None:
        """Handle video frame thumbnail ready — route to registered cards only.

        Only cards registered in the current generation receive the result.
        Stale results from previous generations are silently discarded.
        """
        if not pixmap:
            return
        gen = self._thumbnail_generation
        key = (gen, path, frame_index)
        cards = self._pending_frame_cards.pop(key, None)
        if cards is None:
            # No exact match — try path-only lookup for cards that may
            # have a slightly different frame index (within tolerance).
            matched_keys: list[tuple[int, str, int]] = []
            remaining_map: dict[tuple[int, str, int], list[TimelineClipCard]] = {}
            for k, card_list in self._pending_frame_cards.items():
                if k[0] != gen or k[1] != path:
                    continue
                accepted = []
                rejected = []
                for card in card_list:
                    expected = _compute_preview_frame(card._entry, card._preview_mode)
                    if abs(frame_index - expected) <= 5:
                        card._on_frame_thumbnail_ready(path, frame_index, pixmap)
                        accepted.append(card)
                    else:
                        rejected.append(card)
                if rejected:
                    remaining_map[k] = rejected
                else:
                    matched_keys.append(k)
            # Only remove keys where ALL cards accepted the result
            for k in matched_keys:
                self._pending_frame_cards.pop(k, None)
            # Keep unfulfilled cards in the pending map
            self._pending_frame_cards.update(remaining_map)
            return
        for card in cards:
            card._on_frame_thumbnail_ready(path, frame_index, pixmap)

    def _cleanup_signal_connections(self) -> None:
        """Disconnect from provider signals to prevent leaks on destruction."""
        self._thumbnail_debounce_timer.stop()
        self._geometry_update_timer.stop()
        provider = ThumbnailProvider.instance()
        try:
            provider.thumbnail_ready.disconnect(self._on_strip_thumbnail_ready)
        except (TypeError, RuntimeError):
            pass
        try:
            provider.frame_thumbnail_ready.disconnect(self._on_strip_frame_thumbnail_ready)
        except (TypeError, RuntimeError):
            pass

    # ---- Settings ----

    def _set_preview_mode(self, mode: ClipPreviewMode) -> None:
        self._preview_mode = mode
        _save_preview_mode_setting(mode)
        self._rebuild_cards()

    def _enable_previews_for_large_timeline(self) -> None:
        """User explicitly enables previews on a large (500+) timeline."""
        self._user_enabled_previews = True
        self._previews_suppressed = False
        self._rebuild_cards()

    def _set_bottom_mode(self, mode: BottomLabelMode) -> None:
        self._bottom_mode = mode
        _save_bottom_label_mode_setting(mode)
        for card in self._cards:
            card.update_bottom_mode(mode)

    # ---- Public API ----

    def set_fps(self, fps: float) -> bool:
        """Set the frame rate used for card timecode overlays.

        AUD-25701：时间码叠层此前硬编码 24 fps，25/30/50/60 fps 项目
        显示错误。帧率变化时就地刷新现有卡片的时间码，无需重建。

        Returns:
            True if the normalized fps differed from the current one.
        """
        normalized = _normalize_fps(fps)
        if normalized == self._fps:
            return False
        self._fps = normalized
        for card in self._cards:
            card.set_fps(normalized)
        return True

    def fps(self) -> float:
        """Return the frame rate currently used for timecode overlays."""
        return self._fps

    def set_project_and_timeline(self, project, timeline_sequence) -> None:
        """Build clip entries from the given project and timeline.

        Args:
            project: EditProject instance or None
            timeline_sequence: TimelineSequence instance or None
        """
        previous_timeline_id = self._current_timeline_id
        self._current_project = project
        self._current_timeline = timeline_sequence

        # AUD-25701：项目/时间线加载时直接从时间线序列读取实际帧率
        # （模型携带 Rational/int/float，_normalize_fps 统一归一）。
        if timeline_sequence is not None:
            self.set_fps(getattr(timeline_sequence, "fps", self._fps))

        # Determine timeline_id
        timeline_id: str = ""
        if timeline_sequence is not None:
            timeline_id = timeline_sequence.timeline_id
        elif project is not None and project.active_timeline_id:
            timeline_id = project.active_timeline_id
        self._current_timeline_id = timeline_id or None
        if previous_timeline_id != self._current_timeline_id:
            self._graded_preview_cache.clear()

        self._all_entries = build_clip_entries(project, timeline_sequence, timeline_id)
        self._apply_grade_filter()

    # ---- P1-COLOR-20: 按调色状态过滤 ----

    GRADE_FILTER_MODES = ("all", "ungraded", "graded")

    def set_grade_filter(self, mode: str) -> bool:
        """切换调色过滤（all/ungraded/graded）；非法值或无变化返回 False。"""
        if mode not in self.GRADE_FILTER_MODES or mode == self._grade_filter:
            return False
        self._grade_filter = mode
        self._apply_grade_filter()
        return True

    @property
    def grade_filter(self) -> str:
        return self._grade_filter

    def _apply_grade_filter(self) -> None:
        """按当前过滤模式从完整清单重建显示列表与卡片。"""
        if self._grade_filter == "ungraded":
            self._entries = [e for e in self._all_entries if not e.color_graded]
        elif self._grade_filter == "graded":
            self._entries = [e for e in self._all_entries if e.color_graded]
        else:
            self._entries = list(self._all_entries)
        self._rebuild_cards()
        self._update_nav_buttons()

    def set_graded_preview(self, clip_id: str, pixmap: QPixmap) -> None:
        """Replace one clip's raw thumbnail with a low-cost graded snapshot."""
        if not clip_id or pixmap.isNull():
            return
        cached = pixmap.scaled(
            360,
            203,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._graded_preview_cache[clip_id] = cached
        while len(self._graded_preview_cache) > 16:
            self._graded_preview_cache.pop(next(iter(self._graded_preview_cache)))
        for card in self._cards:
            if card.entry.clip_id == clip_id:
                card.set_preview_override(cached)
                break

    def set_active_clip(self, clip_id: str) -> None:
        """Highlight the card matching *clip_id* and clear previous highlight.

        Args:
            clip_id: Clip ID to activate, or empty string to clear.
        """
        self._active_clip_id = clip_id or None
        for card in self._cards:
            card.set_selected(card.entry.clip_id == clip_id)
        self._update_nav_buttons()

    # ---- P1-COLOR-20: 镜头导航 ----

    def navigate_clip(self, direction: int = 1) -> bool:
        """切换到上/下一个镜头（direction: -1 前 / +1 后）。

        复用点击激活链路（set_active_clip + clip_activated），因此
        grade context 加载、预览刷新与手动点击完全一致；切换后滚动
        到目标卡片。边界处返回 False（不循环）。
        """
        if not self._entries:
            return False
        ids = [entry.clip_id for entry in self._entries]
        if self._active_clip_id in ids:
            index = ids.index(self._active_clip_id) + direction
            if index < 0 or index >= len(ids):
                return False
        else:
            index = 0 if direction > 0 else len(ids) - 1
        entry = self._entries[index]
        self.set_active_clip(entry.clip_id)
        self.clip_activated.emit(
            entry.timeline_id,
            entry.clip_id,
            int(entry.timeline_start),
            int(entry.source_start),
        )
        if 0 <= index < len(self._cards):
            self._scroll_area.ensureWidgetVisible(self._cards[index], 80, 0)
        return True

    def active_clip_position(self) -> tuple[int, int] | None:
        """返回 (当前镜头序号, 总镜头数)，1-based；无激活时 None。"""
        if not self._entries:
            return None
        ids = [entry.clip_id for entry in self._entries]
        if self._active_clip_id not in ids:
            return None
        return ids.index(self._active_clip_id) + 1, len(ids)

    def _make_nav_button(self, arrow: str, direction: int) -> QPushButton:
        btn = QPushButton(arrow)
        btn.setFixedSize(20, 20)
        btn.setToolTip(
            I18N.tr("colorclip.prev_shot", "上一镜 (Up)")
            if direction < 0 else
            I18N.tr("colorclip.next_shot", "下一镜 (Down)"))
        btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(255, 255, 255, 26);
                color: rgba(255, 255, 255, 200);
                border: none;
                border-radius: 3px;
                font-size: 12px;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 50);
            }
            QPushButton:disabled {
                color: rgba(255, 255, 255, 60);
            }
        """)
        btn.clicked.connect(lambda: self.navigate_clip(direction))
        return btn

    def _update_nav_buttons(self) -> None:
        prev_btn = getattr(self, "_prev_clip_btn", None)
        next_btn = getattr(self, "_next_clip_btn", None)
        if prev_btn is None or next_btn is None:
            return
        ids = [entry.clip_id for entry in self._entries]
        has_active = bool(self._entries) and self._active_clip_id in ids
        index = ids.index(self._active_clip_id) if has_active else -1
        prev_btn.setEnabled(has_active and index > 0)
        next_btn.setEnabled(has_active and index < len(ids) - 1)
        self._update_nav_position_label()

    def clear(self) -> None:
        """Remove all clip cards and reset state."""
        self._all_entries.clear()
        self._entries.clear()
        self._active_clip_id = None
        self._graded_preview_cache.clear()
        self._rebuild_cards()
        self._update_nav_buttons()

    def refresh(self) -> None:
        """Rebuild clip entries from current project/timeline data."""
        self.set_project_and_timeline(
            self._current_project,
            self._current_timeline,
        )
        # Re-apply active clip selection after refresh
        if self._active_clip_id:
            self.set_active_clip(self._active_clip_id)

    # ---- Internal ----

    def _rebuild_cards(self) -> None:
        """Clear existing cards and create new ones from ``self._entries``."""
        # Stop debounce timer to prevent double-invocation against stale state
        self._thumbnail_debounce_timer.stop()

        # Remove old cards (no per-card signal disconnects needed —
        # cards no longer connect to provider signals directly).
        for card in self._cards:
            self._card_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

        # Increment generation token so stale thumbnail results are discarded.
        # Pending maps are cleared because old cards are destroyed.
        self._thumbnail_generation += 1
        self._pending_image_cards.clear()
        self._pending_frame_cards.clear()

        if not self._entries:
            self._scroll_area.setVisible(False)
            self._empty_label.setVisible(True)
            self.setMinimumHeight(50)
            return

        # Large timeline degradation
        total_clips = len(self._entries)
        if total_clips >= 500 and not self._user_enabled_previews:
            self._previews_suppressed = True
            effective_preview_mode = ClipPreviewMode.NO_PREVIEW
        elif total_clips >= 200:
            self._previews_suppressed = False
            effective_preview_mode = self._preview_mode
        else:
            self._previews_suppressed = False
            effective_preview_mode = self._preview_mode

        self._scroll_area.setVisible(True)
        self._empty_label.setVisible(False)
        self._update_strip_height()

        # Create new cards — thumbnails are NOT requested here
        for i, entry in enumerate(self._entries):
            card = TimelineClipCard(
                entry, i + 1, total_clips,
                effective_preview_mode, self._bottom_mode,
                self._fps, self._card_size, self._container,
            )
            card.clicked.connect(self._on_card_clicked)
            if self._active_clip_id and entry.clip_id == self._active_clip_id:
                card.set_selected(True)
            # Restore already-known thumbnails in the same pass so a rebuild
            # never flashes the dark placeholder for clips the user has
            # already seen; uncached cards backfill asynchronously.
            self._prime_card_from_thumbnail_cache(card)
            cached_preview = self._graded_preview_cache.get(entry.clip_id)
            if cached_preview is not None and not cached_preview.isNull():
                card.set_preview_override(cached_preview)
            self._card_layout.addWidget(card)
            self._cards.append(card)

        self._geometry_followup_passes = 1
        self._fit_cards_to_viewport()

        # Request visible thumbnails after layout settles.
        # Use the instance-owned debounce timer (parented to self) instead of
        # QTimer.singleShot to avoid GC issues per project MEMORY.md guidelines.
        self._thumbnail_debounce_timer.start(0)

    def _prime_card_from_thumbnail_cache(self, card: TimelineClipCard) -> None:
        """Synchronously apply a cached thumbnail to a freshly built card.

        The provider keeps in-memory pixmap caches for both video frames and
        images.  Reading them during ``_rebuild_cards`` means a refresh (page
        entry, timeline edit, deferred grade-sync) repaints the strip with
        the previous thumbnails instead of blanking to the dark placeholder
        and waiting for the async decode round-trip.
        """
        entry = card._entry
        if getattr(card, "_preview_override_active", False):
            return
        try:
            provider = ThumbnailProvider.instance()
            if entry.media_type == "image":
                cached = provider.get_cached_thumbnail(
                    entry.asset_path, icon_size=144,
                )
            else:
                frame_num = _compute_preview_frame(entry, card._preview_mode)
                if frame_num < 0:
                    return
                # The same frame may have been decoded under any of the
                # provider's key schemes (asset-id key, legacy tuple, or
                # the oldest (path, frame) store) depending on which
                # component requested it first.
                cached = provider.get_cached_frame_pixmap_any(
                    entry.asset_path, frame_num,
                    target_height=144, asset_id=entry.asset_id,
                )
        except Exception:
            return
        if cached is not None and not cached.isNull():
            card._pixmap = cached
            card._thumb_widget.set_pixmap(cached)

    def _on_card_clicked(
        self,
        timeline_id: str,
        clip_id: str,
        timeline_start: int,
        source_start: int,
    ) -> None:
        """Handle card click: update selection and emit signal."""
        _emit_color_clip_debug(
            "[COLOR_CLIP_DEBUG] strip.card_clicked timeline=%s clip=%s "
            "timeline_start=%s source_start=%s",
            timeline_id,
            clip_id,
            timeline_start,
            source_start,
        )
        self.set_active_clip(clip_id)
        self.clip_activated.emit(
            timeline_id,
            clip_id,
            int(timeline_start),
            int(source_start),
        )
