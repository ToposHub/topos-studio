# -*- coding: utf-8 -*-
"""
Scene Cut Preview Widget.

Three-panel preview display for scene cut detection:
- Left panel: Frame before cut point
- Center panel: Current/transition frame
- Right panel: Frame after cut point

## Design Principles

- Following .clinerules: signal anti-recursion, strong typing
- Aspect ratio preserved, black letterboxing
- Support placeholder state when no candidate selected
- Lazy thumbnail loading (thumbnails from cache or generated on demand)

## Layout

    ┌─────────────────────────────────────────────────────────────────┐
    │  Before Frame    │   Current Frame   │    After Frame          │
    │                  │                   │                         │
    │   [Image]        │    [Image]        │     [Image]             │
    │                  │                   │                         │
    │  TC: 00:00:10:00 │  TC: 00:00:10:01  │   TC: 00:00:10:02       │
    │  Frame: 240      │   Frame: 241      │    Frame: 242           │
    └─────────────────────────────────────────────────────────────────┘

## Reference

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section 2.1
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Callable, Tuple
from enum import Enum

from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QFrame,
    QSizePolicy,
)
from PyQt6.QtCore import (
    Qt,
    pyqtSignal,
    QObject,
    QRect,
)
from PyQt6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QFont,
    QImage,
    QPixmap,
    QMouseEvent,
)

from src.features.edit.core.scene_cut.models import (
    SceneCutCandidate,
)
from src.shared.i18n.i18n import I18N


logger = logging.getLogger(__name__)


# ================================================================
# Constants
# ================================================================

# Panel colors
COLORS = {
    "bg_panel": "#1F1F22",
    "bg_image_area": "#000000",
    "border_subtle": "#2a2a2a",
    "text": "#D6D8DE",
    "text_muted": "#8E929A",
    "accent": "#E29227",
    "warning": "#ff9500",
    "accepted": "#73b66b",
    "rejected": "#555555",
}

# Font sizes
FONT_SIZES = {
    "timecode": 10,
    "frame": 9,
    "label": 11,
}


# ================================================================
# Preview Panel Type
# ================================================================

class PreviewPanelType(Enum):
    """Preview panel position."""

    BEFORE = "before"
    """Frame before cut point."""

    CURRENT = "current"
    """Current/transition frame."""

    AFTER = "after"
    """Frame after cut point."""


# ================================================================
# Single Preview Panel
# ================================================================

class SinglePreviewPanel(QFrame):
    """
    Single preview panel for one frame.

    Displays frame image with aspect ratio preservation,
    timecode, and frame number labels.

    Signals:
        clicked: Emitted when the panel is clicked while a frame is
            displayed. Parameters: (panel_type, frame_number).

    Attributes:
        panel_type: Position type (before/current/after).
        image: Current displayed image (QPixmap).
        timecode: Timecode string.
        frame_number: Frame number.
    """

    clicked = pyqtSignal(object, int)  # PreviewPanelType, frame_number

    def __init__(
        self,
        panel_type: PreviewPanelType,
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        Initialize single preview panel.

        Args:
            panel_type: Position type.
            parent: Optional parent widget.
        """
        super().__init__(parent)

        self._panel_type = panel_type
        self._image: Optional[QPixmap] = None
        self._timecode: str = "--:--:--:--"
        self._frame_number: int = -1
        self._score: Optional[float] = None
        self._placeholder_text: str = self._get_placeholder_text()

        self._setup_ui()
        self._apply_style()

        logger.debug(f"SinglePreviewPanel initialized: {panel_type.value}")

    def _get_placeholder_text(self) -> str:
        """Get placeholder text based on panel type."""
        if self._panel_type == PreviewPanelType.BEFORE:
            return "Before"
        elif self._panel_type == PreviewPanelType.CURRENT:
            return "Cut Point"
        else:
            return "After"

    def _setup_ui(self) -> None:
        """Set up UI layout."""
        self.setMinimumSize(200, 120)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding
        )

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Image display area (custom widget)
        self._image_widget = PreviewImageWidget(self._placeholder_text, self)
        layout.addWidget(self._image_widget, stretch=1)

        # Info bar at bottom
        info_bar = QFrame()
        info_bar.setObjectName("infoBar")
        info_bar.setFixedHeight(32)
        info_bar.setStyleSheet(f"""
            QFrame#infoBar {{
                background-color: {COLORS["bg_panel"]};
                border: none;
                border-top: 1px solid {COLORS["border_subtle"]};
            }}
        """)
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(8, 4, 8, 4)
        info_layout.setSpacing(4)

        # Timecode label
        self._tc_label = QLabel("--:--:--:--")
        self._tc_label.setObjectName("tcLabel")
        self._tc_label.setStyleSheet(f"""
            QLabel#tcLabel {{
                color: {COLORS["text"]};
                font-size: {FONT_SIZES["timecode"]}px;
                font-family: monospace;
            }}
        """)
        info_layout.addWidget(self._tc_label)

        info_layout.addStretch()

        # Frame number label
        self._frame_label = QLabel(I18N.tr("scene_cut.frame_unset", "Frame: --"))
        self._frame_label.setObjectName("frameLabel")
        self._frame_label.setStyleSheet(f"""
            QLabel#frameLabel {{
                color: {COLORS["text_muted"]};
                font-size: {FONT_SIZES["frame"]}px;
            }}
        """)
        info_layout.addWidget(self._frame_label)

        layout.addWidget(info_bar)

    def _apply_style(self) -> None:
        """Apply widget style."""
        self.setStyleSheet(f"""
            SinglePreviewPanel {{
                background-color: {COLORS["bg_panel"]};
                border: none;
            }}
        """)

    # ==================== Public Methods ====================

    def set_image(self, image: Optional[QPixmap]) -> None:
        """
        Set preview image.

        Args:
            image: QPixmap to display, None for placeholder.
        """
        self._image = image
        self._image_widget.set_image(image)
        self.update()

    def set_image_from_qimage(self, image: Optional[QImage]) -> None:
        """
        Set preview image from QImage.

        Args:
            image: QImage to display, None for placeholder.
        """
        if image is not None:
            pixmap = QPixmap.fromImage(image)
            self.set_image(pixmap)
        else:
            self.set_image(None)

    def set_timecode(self, timecode: str) -> None:
        """
        Set timecode display.

        Args:
            timecode: Timecode string (HH:MM:SS:FF).
        """
        self._timecode = timecode
        self._tc_label.setText(timecode)

    def set_frame_number(self, frame: int) -> None:
        """
        Set frame number display.

        Args:
            frame: Frame number (source frame).
        """
        self._frame_number = frame
        if frame >= 0:
            self._frame_label.setText(f"Frame: {frame}")
        else:
            self._frame_label.setText(I18N.tr("scene_cut.frame_unset", "Frame: --"))

    def set_score(self, score: Optional[float]) -> None:
        """
        Set score display (optional, for current panel).

        Args:
            score: Confidence score (0-1).
        """
        self._score = score

    def clear(self) -> None:
        """Clear all display data."""
        self._image = None
        self._timecode = "--:--:--:--"
        self._frame_number = -1
        self._score = None

        self._image_widget.set_image(None)
        self._tc_label.setText("--:--:--:--")
        self._frame_label.setText(I18N.tr("scene_cut.frame_unset", "Frame: --"))

    def get_panel_type(self) -> PreviewPanelType:
        """Get panel type."""
        return self._panel_type

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Emit clicked with the displayed frame on a left click.

        AUD-20713: frame_clicked 曾在 SceneCutPreviewWidget 上声明并文档
        化"面板点击跳帧"，但无任何鼠标事件处理，信号永不发射。
        """
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._frame_number >= 0
        ):
            self.clicked.emit(self._panel_type, self._frame_number)
            event.accept()
            return
        super().mousePressEvent(event)


# ================================================================
# Preview Image Widget
# ================================================================

class PreviewImageWidget(QWidget):
    """
    Custom widget for displaying preview image.

    Features:
    - Aspect ratio preservation
    - Black letterboxing
    - Placeholder text when no image
    """

    def __init__(
        self,
        placeholder_text: str = "No Frame",
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        Initialize preview image widget.

        Args:
            placeholder_text: Text to show when no image.
            parent: Optional parent widget.
        """
        super().__init__(parent)

        self._placeholder_text = placeholder_text
        self._image: Optional[QPixmap] = None

        self.setMinimumSize(100, 60)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding
        )

    def set_image(self, image: Optional[QPixmap]) -> None:
        """
        Set image to display.

        Args:
            image: QPixmap or None.
        """
        self._image = image
        self.update()

    def paintEvent(self, event: Any) -> None:
        """Paint the image or placeholder."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Background
        painter.fillRect(self.rect(), QColor(COLORS["bg_image_area"]))

        if self._image is None or self._image.isNull():
            # Draw placeholder
            self._draw_placeholder(painter)
        else:
            # Draw image with aspect ratio
            self._draw_image(painter)

        painter.end()

    def _draw_placeholder(self, painter: QPainter) -> None:
        """Draw placeholder text."""
        # Centered text
        font = QFont()
        font.setPointSize(12)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text_muted"]))

        rect = self.rect()
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignCenter,
            self._placeholder_text,
        )

    def _draw_image(self, painter: QPainter) -> None:
        """Draw image preserving aspect ratio."""
        if self._image is None:
            return

        # Calculate scaled rect preserving aspect ratio
        img_size = self._image.size()
        widget_size = self.size()

        # Scale to fit within widget
        scale = min(
            widget_size.width() / img_size.width(),
            widget_size.height() / img_size.height(),
        )

        scaled_width = int(img_size.width() * scale)
        scaled_height = int(img_size.height() * scale)

        # Center the image
        x = (widget_size.width() - scaled_width) // 2
        y = (widget_size.height() - scaled_height) // 2

        target_rect = QRect(x, y, scaled_width, scaled_height)

        # Draw image
        painter.drawPixmap(target_rect, self._image)

        # Optional: draw border around image
        painter.setPen(QPen(QColor(COLORS["border_subtle"]), 1))
        painter.drawRect(target_rect)


# ================================================================
# Scene Cut Preview Widget (Three-Panel)
# ================================================================

class SceneCutPreviewWidget(QFrame):
    """
    Three-panel preview widget for scene cut detection.

    Displays before/current/after frames for selected candidate.

    Signals:
        frame_clicked: Emitted when a preview panel is clicked.
            Parameters: (panel_type, frame_number)

    Layout:
        ┌──────────────────────────────────────────────────────────┐
        │  [Before Panel] │ [Current Panel] │ [After Panel]        │
        └──────────────────────────────────────────────────────────┘

    Usage:
        ```python
        preview = SceneCutPreviewWidget()

        # Set candidate data
        preview.set_candidate(candidate, before_image, after_image)

        # Connect click signal
        preview.frame_clicked.connect(self._on_frame_clicked)
        ```

    Following .clinerules:
        - Strong typing
        - Signal anti-recursion with _updating flag
    """

    frame_clicked = pyqtSignal(object, int)  # PreviewPanelType, frame_number

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initialize three-panel preview widget.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)

        self._current_candidate: Optional[SceneCutCandidate] = None
        self._updating: bool = False

        self._setup_ui()
        self._apply_style()

        logger.debug("SceneCutPreviewWidget initialized")

    def _setup_ui(self) -> None:
        """Set up UI layout."""
        self.setMinimumSize(600, 200)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.MinimumExpanding,
        )

        # Main horizontal layout for three panels
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)  # Dividers provide the only separation pixels

        # Before panel
        self._before_panel = SinglePreviewPanel(PreviewPanelType.BEFORE, self)
        layout.addWidget(self._before_panel, stretch=1)

        # Divider line
        divider1 = QFrame()
        # Do not use the native VLine frame: on macOS it can render as a
        # bright beveled line even when the stylesheet color is dark.
        divider1.setFrameShape(QFrame.Shape.NoFrame)
        divider1.setFixedWidth(1)
        divider1.setStyleSheet(f"""
            QFrame {{
                background-color: {COLORS["border_subtle"]};
            }}
        """)
        layout.addWidget(divider1)

        # Current panel (center)
        self._current_panel = SinglePreviewPanel(PreviewPanelType.CURRENT, self)
        layout.addWidget(self._current_panel, stretch=1)

        # Divider line
        divider2 = QFrame()
        divider2.setFrameShape(QFrame.Shape.NoFrame)
        divider2.setFixedWidth(1)
        divider2.setStyleSheet(f"""
            QFrame {{
                background-color: {COLORS["border_subtle"]};
            }}
        """)
        layout.addWidget(divider2)

        # After panel
        self._after_panel = SinglePreviewPanel(PreviewPanelType.AFTER, self)
        layout.addWidget(self._after_panel, stretch=1)

        # AUD-20713: 接通面板点击 → frame_clicked（此前信号无发射点）
        for panel in (self._before_panel, self._current_panel, self._after_panel):
            panel.clicked.connect(self._on_panel_clicked)

    def _apply_style(self) -> None:
        """Apply widget style."""
        self.setStyleSheet(f"""
            SceneCutPreviewWidget {{
                background-color: {COLORS["bg_panel"]};
                border: none;
                border-bottom: 1px solid {COLORS["border_subtle"]};
            }}
        """)

    # ==================== Public Methods ====================

    def set_candidate(
        self,
        candidate: Optional[SceneCutCandidate],
        before_image: Optional[QImage] = None,
        after_image: Optional[QImage] = None,
        current_image: Optional[QImage] = None,
        fps: float = 24.0,
    ) -> None:
        """
        Set candidate and preview images.

        Args:
            candidate: SceneCutCandidate to display.
            before_image: QImage for frame before cut.
            after_image: QImage for frame after cut.
            current_image: QImage for cut point frame (optional).
            fps: Source frame rate for timecode/before-after offsets
                (AUD-20710: 此前硬编码 24fps，非 24fps 媒体的时间码与
                ±1 帧偏移全部错误；与 set_frame_preview 统一由调用方
                传入真实帧率).
        """
        if self._updating:
            return

        safe_fps = max(float(fps), 1.0)
        frame_duration = 1.0 / safe_fps

        # AUD-20717: try/finally so an exception mid-update (e.g. RuntimeError
        # from an already-destroyed Qt object) cannot leave the lock raised
        # and silently stop all further preview updates.
        self._updating = True
        try:
            self._current_candidate = candidate

            if candidate is None:
                self._clear_all_panels()
            else:
                # Before panel: frame - 1
                self._before_panel.set_image_from_qimage(before_image)
                self._before_panel.set_timecode(
                    self._format_timecode(candidate.seconds - frame_duration, safe_fps)
                )
                self._before_panel.set_frame_number(candidate.source_frame - 1)

                # Current panel: cut point frame
                self._current_panel.set_image_from_qimage(current_image)
                self._current_panel.set_timecode(
                    self._format_timecode(candidate.seconds, safe_fps)
                )
                self._current_panel.set_frame_number(candidate.source_frame)
                self._current_panel.set_score(candidate.confidence)

                # After panel: frame + 1
                self._after_panel.set_image_from_qimage(after_image)
                self._after_panel.set_timecode(
                    self._format_timecode(candidate.seconds + frame_duration, safe_fps)
                )
                self._after_panel.set_frame_number(candidate.source_frame + 1)

                # Highlight warning status
                if candidate.is_warning:
                    self._set_warning_highlight()
                elif candidate.is_accepted:
                    self._set_accepted_highlight()
                elif candidate.is_rejected:
                    self._set_rejected_highlight()
                else:
                    self._set_normal_style()
        finally:
            self._updating = False

    def set_before_image(self, image: Optional[QImage]) -> None:
        """
        Set before panel image.

        Args:
            image: QImage or None.
        """
        self._before_panel.set_image_from_qimage(image)

    def set_current_image(self, image: Optional[QImage]) -> None:
        """
        Set current panel image.

        Args:
            image: QImage or None.
        """
        self._current_panel.set_image_from_qimage(image)

    def set_after_image(self, image: Optional[QImage]) -> None:
        """
        Set after panel image.

        Args:
            image: QImage or None.
        """
        self._after_panel.set_image_from_qimage(image)

    def set_frame_preview(
        self,
        frame_number: int,
        fps: float,
        before_image: Optional[QImage],
        current_image: Optional[QImage],
        after_image: Optional[QImage],
    ) -> None:
        """Display an arbitrary frame triplet during frame-accurate seeking.

        This is separate from :meth:`set_candidate` because the current
        playback frame can sit between two detected cuts.
        """
        if self._updating:
            return

        safe_fps = max(float(fps), 1.0)
        center_seconds = max(frame_number, 0) / safe_fps
        # AUD-20717: try/finally guards the anti-recursion lock.
        self._updating = True
        try:
            self._before_panel.set_image_from_qimage(before_image)
            self._before_panel.set_timecode(
                self._format_timecode(
                    max(0.0, center_seconds - 1.0 / safe_fps),
                    safe_fps,
                )
            )
            self._before_panel.set_frame_number(frame_number - 1)
            self._current_panel.set_image_from_qimage(current_image)
            self._current_panel.set_timecode(
                self._format_timecode(center_seconds, safe_fps)
            )
            self._current_panel.set_frame_number(frame_number)
            self._after_panel.set_image_from_qimage(after_image)
            self._after_panel.set_timecode(
                self._format_timecode(center_seconds + 1.0 / safe_fps, safe_fps)
            )
            self._after_panel.set_frame_number(frame_number + 1)
        finally:
            self._updating = False

    def clear(self) -> None:
        """Clear all panels."""
        if self._updating:
            return

        # AUD-20717: try/finally guards the anti-recursion lock.
        self._updating = True
        try:
            self._clear_all_panels()
        finally:
            self._updating = False

    def get_current_candidate(self) -> Optional[SceneCutCandidate]:
        """Get current displayed candidate."""
        return self._current_candidate

    def get_before_panel(self) -> SinglePreviewPanel:
        """Get before panel widget."""
        return self._before_panel

    def get_current_panel(self) -> SinglePreviewPanel:
        """Get current panel widget."""
        return self._current_panel

    def get_after_panel(self) -> SinglePreviewPanel:
        """Get after panel widget."""
        return self._after_panel

    # ==================== Helper Methods ====================

    def _on_panel_clicked(
        self,
        panel_type: PreviewPanelType,
        frame_number: int,
    ) -> None:
        """Forward a single panel click as frame_clicked (panel click → jump)."""
        if not self._updating:
            self.frame_clicked.emit(panel_type, frame_number)

    def _clear_all_panels(self) -> None:
        """Clear all three panels."""
        self._before_panel.clear()
        self._current_panel.clear()
        self._after_panel.clear()
        self._set_normal_style()

    def _format_timecode(self, seconds: float, fps: float = 24.0) -> str:
        """
        Format seconds to timecode.

        Args:
            seconds: Time in seconds.
            fps: Frames per second.

        Returns:
            Timecode string.
        """
        if seconds < 0:
            return "00:00:00:00"

        # AUD-20711: 总帧数与 h/m/s 分桶必须使用同一份帧率——此前
        # total_frames 按 float fps 计算、除数按 int(fps)（29.97 截断为
        # 29）分桶，分数帧率下时间码随时长漂移（60s@29.97 显示
        # 00:01:02:00）。统一按四舍五入后的整数帧率换算（60s@29.97 →
        # 00:01:00:00），与整数帧率媒体完全一致。
        fps_int = max(1, int(round(fps)))
        total_frames = int(seconds * fps_int + 0.5)
        hours = total_frames // (3600 * fps_int)
        remaining = total_frames % (3600 * fps_int)
        minutes = remaining // (60 * fps_int)
        remaining = remaining % (60 * fps_int)
        secs = remaining // fps_int
        frames = remaining % fps_int

        return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"

    # ==================== Status Highlight Styles ====================

    def _apply_current_panel_status_border(self, border_color: Optional[str]) -> None:
        """Apply a status border to the current panel (None restores normal).

        AUD-20712: warning/accepted/rejected/normal 四个样式方法此前
        逐字节相同，候选状态在预览面板中完全不可见。现按状态施加
        COLORS 中已定义的边框色。
        """
        if border_color is None:
            border_css = "border: none;"
        else:
            border_css = f"border: 2px solid {border_color};"
        self._current_panel.setStyleSheet(f"""
            SinglePreviewPanel {{
                background-color: {COLORS["bg_panel"]};
                {border_css}
            }}
        """)

    def _set_warning_highlight(self) -> None:
        """Set warning highlight style (amber border)."""
        self._apply_current_panel_status_border(COLORS["warning"])

    def _set_accepted_highlight(self) -> None:
        """Set accepted highlight style (green border)."""
        self._apply_current_panel_status_border(COLORS["accepted"])

    def _set_rejected_highlight(self) -> None:
        """Set rejected highlight style (muted gray border)."""
        self._apply_current_panel_status_border(COLORS["rejected"])

    def _set_normal_style(self) -> None:
        """Set normal style (no status border)."""
        self._apply_current_panel_status_border(None)


# ================================================================
# Thumbnail Loader Helper
# ================================================================

class ThumbnailLoader(QObject):
    """
    Helper for loading thumbnails asynchronously.

    Provides callback-based thumbnail loading that can be
    integrated with the thumbnail cache or frame sampler.

    Signals:
        thumbnail_loaded: Emitted when thumbnail is loaded.
            Parameters: (panel_type, frame_number, image)

    Usage:
        ```python
        loader = ThumbnailLoader()
        loader.thumbnail_loaded.connect(preview.set_before_image)

        # Request load
        loader.load_thumbnail(PreviewPanelType.BEFORE, frame_number)
        ```
    """

    thumbnail_loaded = pyqtSignal(object, int, object)  # PreviewPanelType, frame, QImage

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """Initialize thumbnail loader."""
        super().__init__(parent)
        self._pending_requests: dict[Tuple[PreviewPanelType, int], bool] = {}

    def request_thumbnail(
        self,
        panel_type: PreviewPanelType,
        frame: int,
        loader_func: Optional[Callable[[int], Optional[QImage]]] = None,
    ) -> None:
        """
        Request thumbnail loading.

        Note:
            When ``loader_func`` is provided the load runs synchronously on
            the calling thread (AUD-20714); keep ``loader_func`` cheap or
            dispatch it onto a worker thread yourself.

        Args:
            panel_type: Panel type for the thumbnail.
            frame: Frame number.
            loader_func: Optional function to load thumbnail.
        """
        key = (panel_type, frame)
        self._pending_requests[key] = True

        try:
            if loader_func is not None:
                # Direct load (synchronous)
                image = loader_func(frame)
                self.thumbnail_loaded.emit(panel_type, frame, image)

            # For async loading, caller should connect to thumbnail_loaded
        finally:
            # AUD-20714: the request has completed (or the loader raised) —
            # drop the pending marker so the dict cannot grow without bound
            # and "pending" keeps its meaning.
            self._pending_requests.pop(key, None)

    def cancel_request(self, panel_type: PreviewPanelType, frame: int) -> None:
        """
        Cancel pending thumbnail request.

        Args:
            panel_type: Panel type.
            frame: Frame number.
        """
        key = (panel_type, frame)
        if key in self._pending_requests:
            del self._pending_requests[key]

    def clear_pending(self) -> None:
        """Clear all pending requests."""
        self._pending_requests.clear()
