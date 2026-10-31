# -*- coding: utf-8 -*-
"""
Scene Cut Graph Widget.

Detection score visualization graph showing:
- Green vertical bars: Detection peaks (score/confidence)
- Purple horizontal line: Threshold level
- Red playhead: Current frame position
- Warning markers: Orange for flash/transition/fast motion

## Design Principles

- Following .clinerules: strong typing, no OpenGL in UI thread
- Pure QPainter rendering (no GPU)
- Efficient rendering with minimal updates
- Click-to-select candidate interaction

## Layout

    ┌─────────────────────────────────────────────────────────────────┐
    │  [Graph Area - Black Background]                               │
    │                                                                 │
    │   ▲ ▲ ▲  (Green peaks for detected cuts)                       │
    │   │ │ │                                                         │
    │   ─────── (Purple threshold line)                              │
    │                                                                 │
    │   │ (Red playhead for current frame)                            │
    └─────────────────────────────────────────────────────────────────┘
    │  [Mini Timeline Scrubber]                                      │
    └─────────────────────────────────────────────────────────────────┘

## Reference

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section 2.1
"""

from __future__ import annotations

import logging
from typing import Any, Optional, List

from PyQt6.QtWidgets import (
    QWidget,
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QSlider,
    QSizePolicy,
)
from PyQt6.QtCore import (
    Qt,
    pyqtSignal,
    QRect,
)
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QFont,
    QBrush,
    QPolygonF,
    QMouseEvent,
)

from src.features.edit.core.scene_cut.models import (
    SceneCutCandidate,
)


logger = logging.getLogger(__name__)


# ================================================================
# Constants
# ================================================================

# Graph colors
GRAPH_COLORS = {
    "bg_graph": "#000000",
    "peak_normal": "#50FA7B",  # Green for normal cuts
    "peak_warning": "#FF9500",  # Orange for warnings
    "peak_accepted": "#73b66b",  # Green tint for accepted
    "peak_rejected": "#555555",  # Gray for rejected
    "peak_below_threshold": "#3f4545",  # Muted candidates below the line
    "threshold_line": "#9B59B6",  # Purple
    "playhead": "#FF3333",  # Red
    "playhead_triangle": "#FF3333",
    "text": "#D6D8DE",
    "text_muted": "#8E929A",
    "border": "#2a2a2a",
    "scrubber_bg": "#17181c",
}

# Graph dimensions
GRAPH_DIMENSIONS = {
    "min_height": 100,
    "peak_width": 1,
    "threshold_line_width": 1,
    "playhead_width": 2,
    "triangle_size": 8,
    "margin_top": 20,
    "margin_bottom": 10,
    "margin_left": 40,
    "margin_right": 20,
}


# ================================================================
# Graph Scale Helper
# ================================================================

class GraphScale:
    """
    Helper for mapping values to graph coordinates.

    Handles the transformation from data coordinates to widget coordinates.
    """

    def __init__(
        self,
        widget_width: int,
        widget_height: int,
        data_min: float,
        data_max: float,
        value_min: float = 0.0,
        value_max: float = 1.0,
        margin_top: int = 20,
        margin_bottom: int = 10,
        margin_left: int = 40,
        margin_right: int = 20,
    ) -> None:
        """
        Initialize graph scale.

        Args:
            widget_width: Widget width in pixels.
            widget_height: Widget height in pixels.
            data_min: Minimum data index (e.g., frame 0).
            data_max: Maximum data index (e.g., frame 1000).
            value_min: Minimum value (0.0 for score).
            value_max: Maximum value (1.0 for score).
            margin_top: Top margin in pixels.
            margin_bottom: Bottom margin in pixels.
            margin_left: Left margin in pixels.
            margin_right: Right margin in pixels.
        """
        self._widget_width = widget_width
        self._widget_height = widget_height
        self._data_min = data_min
        self._data_max = data_max
        self._value_min = value_min
        self._value_max = value_max
        self._margin_top = margin_top
        self._margin_bottom = margin_bottom
        self._margin_left = margin_left
        self._margin_right = margin_right

        # Calculate drawable area
        self._draw_width = widget_width - margin_left - margin_right
        self._draw_height = widget_height - margin_top - margin_bottom

    def data_to_x(self, data_index: float) -> int:
        """
        Map data index to x coordinate.

        Args:
            data_index: Data index (e.g., frame number).

        Returns:
            X coordinate in pixels.
        """
        if self._data_max <= self._data_min:
            return self._margin_left

        ratio = (data_index - self._data_min) / (self._data_max - self._data_min)
        return int(self._margin_left + ratio * self._draw_width)

    def value_to_y(self, value: float) -> int:
        """
        Map value to y coordinate.

        Args:
            value: Value (e.g., score 0-1).

        Returns:
            Y coordinate in pixels (top is low value).
        """
        if self._value_max <= self._value_min:
            return self._widget_height - self._margin_bottom

        ratio = (value - self._value_min) / (self._value_max - self._value_min)
        # Invert: high value = low y (near top)
        return int(self._widget_height - self._margin_bottom - ratio * self._draw_height)

    def y_to_value(self, y: float) -> float:
        """Map a graph y coordinate back to a value."""
        if self._draw_height <= 0:
            return self._value_min

        ratio = (self._widget_height - self._margin_bottom - y) / self._draw_height
        value = self._value_min + ratio * (self._value_max - self._value_min)
        return max(self._value_min, min(self._value_max, value))

    def x_to_data(self, x: int) -> float:
        """
        Map x coordinate to data index.

        Args:
            x: X coordinate in pixels.

        Returns:
            Data index.
        """
        if self._draw_width <= 0:
            return self._data_min

        ratio = (x - self._margin_left) / self._draw_width
        return self._data_min + ratio * (self._data_max - self._data_min)

    def get_draw_rect(self) -> QRect:
        """Get drawable rectangle."""
        return QRect(
            self._margin_left,
            self._margin_top,
            self._draw_width,
            self._draw_height,
        )


# ================================================================
# Graph Area Widget
# ================================================================

class SceneCutGraphArea(QWidget):
    """
    Main graph area for displaying detection scores.

    Features:
    - Black background
    - Green peaks for detected cuts
    - Orange peaks for warnings
    - Purple threshold line
    - Red playhead
    - Click-to-select interaction

    Signals:
        candidate_clicked: Emitted when a peak is clicked.
            Parameters: (candidate_index)
        frame_clicked: Emitted when graph area is clicked.
            Parameters: (frame_number)
        threshold_changed: Emitted while the purple threshold line is dragged.
            Parameters: (threshold value)
    """

    candidate_clicked = pyqtSignal(int)  # candidate_index
    frame_clicked = pyqtSignal(int)  # frame_number
    threshold_changed = pyqtSignal(float)  # threshold value

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initialize graph area.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)

        # Data
        self._candidates: List[SceneCutCandidate] = []
        self._total_frames: int = 1000
        self._threshold: float = 0.55
        self._display_value_max: float = 1.0

        # Current selection
        self._current_frame: int = 0
        self._current_index: int = -1

        # Scale helper
        self._scale: Optional[GraphScale] = None

        # UI
        self.setMinimumHeight(GRAPH_DIMENSIONS["min_height"])
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setMouseTracking(True)

        # Tooltip candidate
        self._hover_index: int = -1
        self._dragging_threshold: bool = False
        self._threshold_hit_tolerance: int = 8

        logger.debug("SceneCutGraphArea initialized")

    # ==================== Data Management ====================

    def set_candidates(
        self,
        candidates: List[SceneCutCandidate],
        total_frames: int,
    ) -> None:
        """
        Set detection data.

        Args:
            candidates: List of SceneCutCandidate.
            total_frames: Total number of frames in media.
        """
        self._candidates = candidates
        self._total_frames = max(100, total_frames)
        self._update_scale()
        self.update()

    def set_threshold(self, threshold: float) -> None:
        """
        Set threshold level.

        Args:
            threshold: Threshold value (0-1).
        """
        self._threshold = max(0.0, min(1.0, threshold))
        self._update_scale()
        self.update()

    def _is_near_threshold(self, x: float, y: float) -> bool:
        """Return whether a point is inside the draggable threshold hit area."""
        if self._scale is None:
            return False

        draw_rect = self._scale.get_draw_rect()
        threshold_y = self._scale.value_to_y(self._threshold)
        return (
            draw_rect.left() <= x <= draw_rect.right()
            and abs(y - threshold_y) <= self._threshold_hit_tolerance
        )

    def _update_threshold_from_y(self, y: float) -> None:
        """Update the threshold from a dragged mouse position."""
        if self._scale is None:
            return

        threshold = self._scale.y_to_value(y)
        if abs(threshold - self._threshold) < 0.0001:
            return

        self._threshold = threshold
        self.update()
        self.threshold_changed.emit(threshold)

    def set_current_frame(self, frame: int) -> None:
        """
        Set current playhead frame.

        Args:
            frame: Frame number.
        """
        self._current_frame = max(0, min(frame, self._total_frames))
        self.update()

    def set_current_candidate_index(self, index: int) -> None:
        """
        Set current selected candidate index.

        Args:
            index: Candidate index, -1 for none.
        """
        self._current_index = index

        # Update playhead to match candidate
        if 0 <= index < len(self._candidates):
            self._current_frame = self._candidates[index].source_frame

        self.update()

    def clear(self) -> None:
        """Clear all data."""
        self._candidates = []
        self._total_frames = 1000
        self._current_frame = 0
        self._current_index = -1
        self._hover_index = -1
        self.update()

    # ==================== Scale Management ====================

    def _update_scale(self) -> None:
        """Update graph scale helper."""
        dims = GRAPH_DIMENSIONS
        max_score = max(
            (candidate.score for candidate in self._candidates),
            default=0.0,
        )

        # Detection scores for real footage often occupy only the lower half
        # of the 0..1 range. Anchor the observed tallest peak at about 90% of
        # the drawable height, like Resolve's peak display, while retaining
        # enough headroom for the threshold.
        peak_anchor_max = max_score / 0.90 if max_score > 0.0 else 0.0
        self._display_value_max = min(
            1.0,
            max(0.05, peak_anchor_max, self._threshold * 1.10),
        )
        self._scale = GraphScale(
            widget_width=self.width(),
            widget_height=self.height(),
            data_min=0.0,
            data_max=float(self._total_frames),
            value_min=0.0,
            value_max=self._display_value_max,
            margin_top=dims["margin_top"],
            margin_bottom=dims["margin_bottom"],
            margin_left=dims["margin_left"],
            margin_right=dims["margin_right"],
        )

    def resizeEvent(self, event: Any) -> None:
        """Handle resize event."""
        super().resizeEvent(event)
        self._update_scale()

    # ==================== Painting ====================

    def paintEvent(self, event: Any) -> None:
        """Paint the graph."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        painter.fillRect(self.rect(), QColor(GRAPH_COLORS["bg_graph"]))

        if self._scale is None:
            self._update_scale()

        if self._scale is None:
            painter.end()
            return

        # Draw components
        self._draw_threshold_line(painter)
        self._draw_peaks(painter)
        self._draw_playhead(painter)
        self._draw_frame_labels(painter)

        painter.end()

    def _draw_threshold_line(self, painter: QPainter) -> None:
        """Draw horizontal threshold line."""
        if self._scale is None:
            return

        y = self._scale.value_to_y(self._threshold)
        left = self._scale.get_draw_rect().left()
        right = self._scale.get_draw_rect().right()

        # A thin solid line matches the Resolve-style threshold guide and is
        # easier to grab because the visual line does not disappear between
        # dash segments.
        pen = QPen(QColor(GRAPH_COLORS["threshold_line"]))
        pen.setWidth(GRAPH_DIMENSIONS["threshold_line_width"])
        pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(pen)

        painter.drawLine(left, y, right, y)

        # Label on left
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QColor(GRAPH_COLORS["threshold_line"]))
        painter.drawText(
            5, y + 4,
            f"{self._threshold:.2f}",
        )

    def _draw_peaks(self, painter: QPainter) -> None:
        """Draw detection peaks."""
        if self._scale is None or not self._candidates:
            return

        for i, candidate in enumerate(self._candidates):
            x = self._scale.data_to_x(candidate.source_frame)
            y_top = self._scale.value_to_y(candidate.score)
            y_bottom = self.height() - GRAPH_DIMENSIONS["margin_bottom"]
            active = candidate.score >= self._threshold

            # Choose color based on status
            if not active:
                color = QColor(GRAPH_COLORS["peak_below_threshold"])
            elif candidate.is_accepted:
                color = QColor(GRAPH_COLORS["peak_accepted"])
            elif candidate.is_rejected:
                color = QColor(GRAPH_COLORS["peak_rejected"])
            elif candidate.is_warning:
                color = QColor(GRAPH_COLORS["peak_warning"])
            else:
                color = QColor(GRAPH_COLORS["peak_normal"])

            # Highlight current selection
            if i == self._current_index:
                color = QColor("#FFFFFF")  # White highlight

            # Keep the selected width local. The previous implementation
            # mutated the width used by every later candidate in the same
            # paint pass, making dense graphs look progressively thicker.
            peak_width = 2 if i == self._current_index else GRAPH_DIMENSIONS["peak_width"]

            # Draw vertical bar
            pen = QPen(color)
            pen.setWidth(peak_width)
            painter.setPen(pen)

            painter.drawLine(x, y_top, x, y_bottom)

            # Small triangle at top for detected peaks
            if active:
                triangle_size = 4
                points = [
                    QPointF(x, y_top - triangle_size),
                    QPointF(x - triangle_size // 2, y_top),
                    QPointF(x + triangle_size // 2, y_top),
                ]
                painter.setBrush(QBrush(color))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawPolygon(QPolygonF(points))

    def _draw_playhead(self, painter: QPainter) -> None:
        """Draw red playhead line."""
        if self._scale is None:
            return

        x = self._scale.data_to_x(self._current_frame)
        top = GRAPH_DIMENSIONS["margin_top"]
        bottom = self.height() - GRAPH_DIMENSIONS["margin_bottom"]

        # Red line
        pen = QPen(QColor(GRAPH_COLORS["playhead"]))
        pen.setWidth(GRAPH_DIMENSIONS["playhead_width"])
        painter.setPen(pen)

        painter.drawLine(x, top, x, bottom)

        # Triangle at top
        triangle_size = GRAPH_DIMENSIONS["triangle_size"]
        points = [
            QPointF(x, top - triangle_size),
            QPointF(x - triangle_size // 2, top),
            QPointF(x + triangle_size // 2, top),
        ]
        painter.setBrush(QBrush(QColor(GRAPH_COLORS["playhead_triangle"])))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF(points))

    def _draw_frame_labels(self, painter: QPainter) -> None:
        """Draw frame number labels on bottom."""
        if self._scale is None:
            return

        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QColor(GRAPH_COLORS["text_muted"]))

        # Draw start and end frame numbers
        draw_rect = self._scale.get_draw_rect()

        # Start frame
        painter.drawText(draw_rect.left(), self.height() - 2, "0")

        # End frame
        painter.drawText(
            draw_rect.right() - 30,
            self.height() - 2,
            str(self._total_frames),
        )

        # Current frame at playhead
        painter.setPen(QColor(GRAPH_COLORS["text"]))
        x = self._scale.data_to_x(self._current_frame)
        painter.drawText(x - 15, GRAPH_DIMENSIONS["margin_top"] - 5, str(self._current_frame))

    # ==================== Mouse Interaction ====================

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse click."""
        if self._scale is None:
            return

        x = event.position().x()
        y = event.position().y()

        # Give the threshold guide priority over candidate selection. The
        # hit area is intentionally wider than the 1px painted line so it is
        # practical to drag even when many candidates are close together.
        if event.button() == Qt.MouseButton.LeftButton and self._is_near_threshold(x, y):
            self._dragging_threshold = True
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            self._update_threshold_from_y(y)
            event.accept()
            return

        # Check if clicked on a candidate peak
        for i, candidate in enumerate(self._candidates):
            peak_x = self._scale.data_to_x(candidate.source_frame)
            if abs(x - peak_x) <= 5:
                self.candidate_clicked.emit(i)
                event.accept()
                return

        # Otherwise, treat as frame click
        frame = int(self._scale.x_to_data(x))
        frame = max(0, min(frame, self._total_frames))
        self.frame_clicked.emit(frame)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move for hover."""
        if self._scale is None:
            return

        x = event.position().x()
        y = event.position().y()

        if self._dragging_threshold:
            self._update_threshold_from_y(y)
            event.accept()
            return

        if self._is_near_threshold(x, y):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        # Check if hovering over a candidate
        new_hover = -1
        for i, candidate in enumerate(self._candidates):
            peak_x = self._scale.data_to_x(candidate.source_frame)
            if abs(x - peak_x) <= 5:
                new_hover = i
                break

        if new_hover != self._hover_index:
            self._hover_index = new_hover
            if new_hover >= 0:
                candidate = self._candidates[new_hover]
                tooltip = f"Frame: {candidate.source_frame}\nScore: {candidate.score:.2f}\nKind: {candidate.kind.value}"
                self.setToolTip(tooltip)
            else:
                self.setToolTip("")
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Finish a threshold drag."""
        if self._dragging_threshold and event.button() == Qt.MouseButton.LeftButton:
            self._dragging_threshold = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        super().mouseReleaseEvent(event)


# ================================================================
# Mini Scrubber Widget
# ================================================================

class MiniScrubber(QSlider):
    """
    Mini scrubber slider for frame navigation.

    Custom styled slider that follows the graph timeline.
    """

    frame_changed = pyqtSignal(int)  # frame

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initialize mini scrubber.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(Qt.Orientation.Horizontal, parent)

        self._updating: bool = False
        self._total_frames: int = 1000

        self.setMinimum(0)
        self.setMaximum(1000)
        self.setValue(0)
        self.setFixedHeight(24)

        self._apply_style()

        # Connect internal signal
        self.valueChanged.connect(self._on_value_changed)

        logger.debug("MiniScrubber initialized")

    def _apply_style(self) -> None:
        """Apply scrubber style."""
        self.setStyleSheet(f"""
            QSlider {{
                background-color: {GRAPH_COLORS["scrubber_bg"]};
                border: none;
                height: 24px;
            }}

            QSlider::groove:horizontal {{
                background-color: {GRAPH_COLORS["scrubber_bg"]};
                height: 4px;
                border: none;
            }}

            QSlider::handle:horizontal {{
                background-color: {GRAPH_COLORS["playhead"]};
                width: 8px;
                height: 16px;
                margin: -6px 0;
                border-radius: 4px;
            }}

            QSlider::handle:horizontal:hover {{
                background-color: #FF5555;
            }}

            QSlider::sub-page:horizontal {{
                background-color: {GRAPH_COLORS["threshold_line"]};
                height: 4px;
            }}
        """)

    def set_total_frames(self, total_frames: int) -> None:
        """
        Set total frames range.

        Args:
            total_frames: Total frame count.
        """
        self._total_frames = max(100, total_frames)
        # AUD-20700: 新最大值小于当前值时，setMaximum 会钳制 value 并
        # 发出 valueChanged；未持锁时 _on_value_changed 会把这次程序化
        # 钳制当作用户操作泄漏为 frame_changed（clear() 把 max 归位、
        # 换载更短视频时引发意外跳帧）。范围变更与 set_frame 一样持
        # _updating 锁，且用 try/finally 保证异常路径也能解锁。
        self._updating = True
        try:
            self.setMaximum(self._total_frames)
        finally:
            self._updating = False

    def set_frame(self, frame: int) -> None:
        """
        Set current frame position.

        Args:
            frame: Frame number.
        """
        if self._updating:
            return

        # AUD-20702: try/finally so an exception mid-update (e.g. RuntimeError
        # from an already-destroyed Qt object) cannot leave the lock raised
        # and silently freeze all further programmatic updates.
        self._updating = True
        try:
            self.setValue(frame)
        finally:
            self._updating = False

    def _on_value_changed(self, value: int) -> None:
        """Handle value change."""
        if not self._updating:
            self.frame_changed.emit(value)


# ================================================================
# Scene Cut Graph Widget (Combined)
# ================================================================

class SceneCutGraphWidget(QFrame):
    """
    Combined graph widget with graph area and mini scrubber.

    Layout:
        ┌─────────────────────────────────────────────────────────────┐
        │  [SceneCutGraphArea - Main visualization]                   │
        │                                                             │
        │   Peaks, threshold, playhead                               │
        │                                                             │
        └─────────────────────────────────────────────────────────────┘
        ┌─────────────────────────────────────────────────────────────┐
        │  [MiniScrubber - Frame navigation]                          │
        └─────────────────────────────────────────────────────────────┘

    Signals:
        candidate_clicked: Forwarded from graph area.
        frame_clicked: Forwarded from graph area/scrubber.
        threshold_changed: Emitted while the graph threshold is dragged.
        sensitivity_changed: Kept for compatibility with existing callers.

    Usage:
        ```python
        graph = SceneCutGraphWidget()

        # Set data
        graph.set_candidates(candidates, total_frames)

        # Connect signals
        graph.candidate_clicked.connect(self._on_candidate_selected)
        graph.frame_clicked.connect(self._on_frame_seek)
        ```

    Following .clinerules:
        - Strong typing
        - Signal anti-recursion
    """

    candidate_clicked = pyqtSignal(int)  # candidate_index
    frame_clicked = pyqtSignal(int)  # frame_number
    threshold_changed = pyqtSignal(float)  # graph threshold value
    sensitivity_changed = pyqtSignal(float)  # threshold value

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initialize graph widget.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)

        self._updating: bool = False
        self._threshold: float = 0.55

        self._setup_ui()
        self._apply_style()

        logger.debug("SceneCutGraphWidget initialized")

    def _setup_ui(self) -> None:
        """Set up UI layout."""
        self.setMinimumSize(400, GRAPH_DIMENSIONS["min_height"] + 24)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.MinimumExpanding,
        )

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Graph area
        self._graph_area = SceneCutGraphArea(self)
        layout.addWidget(self._graph_area, stretch=1)

        # Mini scrubber
        self._scrubber = MiniScrubber(self)
        layout.addWidget(self._scrubber)

        # Connect internal signals
        self._graph_area.candidate_clicked.connect(self.candidate_clicked.emit)
        self._graph_area.frame_clicked.connect(self._on_graph_frame_clicked)
        self._graph_area.threshold_changed.connect(self._on_graph_threshold_changed)
        self._scrubber.frame_changed.connect(self._on_scrubber_frame_changed)

    def _apply_style(self) -> None:
        """Apply widget style."""
        self.setStyleSheet(f"""
            SceneCutGraphWidget {{
                background-color: {GRAPH_COLORS["bg_graph"]};
                border: none;
            }}
        """)

    # ==================== Public Methods ====================

    def set_candidates(
        self,
        candidates: List[SceneCutCandidate],
        total_frames: int,
    ) -> None:
        """
        Set detection data.

        Args:
            candidates: List of SceneCutCandidate.
            total_frames: Total number of frames.
        """
        self._graph_area.set_candidates(candidates, total_frames)
        self._scrubber.set_total_frames(total_frames)

    def set_threshold(self, threshold: float) -> None:
        """
        Set threshold level.

        Args:
            threshold: Threshold value (0-1).
        """
        if self._updating:
            return

        # AUD-20702: try/finally guards the anti-recursion lock.
        self._updating = True
        try:
            self._threshold = threshold
            self._graph_area.set_threshold(threshold)
        finally:
            self._updating = False

    def set_current_frame(self, frame: int) -> None:
        """
        Set current frame position.

        Args:
            frame: Frame number.
        """
        if self._updating:
            return

        # AUD-20702: try/finally guards the anti-recursion lock.
        self._updating = True
        try:
            self._graph_area.set_current_frame(frame)
            self._scrubber.set_frame(frame)
        finally:
            self._updating = False

    def set_current_candidate_index(self, index: int) -> None:
        """
        Set current selected candidate.

        Args:
            index: Candidate index.
        """
        self._graph_area.set_current_candidate_index(index)

        # Update scrubber to match
        self._graph_area.set_current_frame(
            self._graph_area._current_frame
        )
        self._scrubber.set_frame(self._graph_area._current_frame)

    def clear(self) -> None:
        """Clear all data."""
        self._graph_area.clear()
        self._scrubber.set_total_frames(1000)
        self._scrubber.set_frame(0)

    def get_threshold(self) -> float:
        """Get current threshold."""
        return self._threshold

    def get_graph_area(self) -> SceneCutGraphArea:
        """Get graph area widget."""
        return self._graph_area

    def get_scrubber(self) -> MiniScrubber:
        """Get mini scrubber widget."""
        return self._scrubber

    # ==================== Signal Handlers ====================

    def _on_graph_frame_clicked(self, frame: int) -> None:
        """Handle frame click from graph."""
        if not self._updating:
            # AUD-20702: try/finally guards the anti-recursion lock.
            self._updating = True
            try:
                self._scrubber.set_frame(frame)
            finally:
                self._updating = False
            self.frame_clicked.emit(frame)

    def _on_graph_threshold_changed(self, threshold: float) -> None:
        """Forward a threshold drag without changing detection sensitivity."""
        self._threshold = threshold
        self.threshold_changed.emit(threshold)

    def _on_scrubber_frame_changed(self, frame: int) -> None:
        """Handle frame change from scrubber."""
        if not self._updating:
            # AUD-20702: try/finally guards the anti-recursion lock.
            self._updating = True
            try:
                self._graph_area.set_current_frame(frame)
            finally:
                self._updating = False
            self.frame_clicked.emit(frame)


# ================================================================
# Threshold Slider Widget
# ================================================================

class ThresholdSlider(QFrame):
    """
    Slider widget for adjusting detection threshold.

    Combines label, slider, and value display.

    Signals:
        threshold_changed: Emitted when threshold changes.
    """

    threshold_changed = pyqtSignal(float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initialize threshold slider.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)

        self._updating: bool = False
        self._threshold: float = 0.55

        self._setup_ui()
        self._apply_style()

    def _setup_ui(self) -> None:
        """Set up UI."""
        self.setFixedHeight(30)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # Label
        from src.shared.i18n.i18n import I18N
        label_text = I18N.tr("scene_cut.sensitivity", default="Sensitivity")
        self._label = QLabel(label_text)
        self._label.setStyleSheet(f"""
            QLabel {{
                color: {GRAPH_COLORS["text"]};
                font-size: 11px;
            }}
        """)
        layout.addWidget(self._label)

        # Slider
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(35)
        self._slider.setMaximum(75)
        self._slider.setValue(55)
        self._slider.setFixedWidth(120)
        self._slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background-color: {GRAPH_COLORS["scrubber_bg"]};
                height: 4px;
                border-radius: 2px;
            }}

            QSlider::handle:horizontal {{
                background-color: {GRAPH_COLORS["threshold_line"]};
                width: 12px;
                height: 12px;
                margin: -4px 0;
                border-radius: 6px;
            }}

            QSlider::sub-page:horizontal {{
                background-color: {GRAPH_COLORS["peak_normal"]};
                height: 4px;
                border-radius: 2px;
            }}
        """)
        layout.addWidget(self._slider)

        # Value display
        self._value_label = QLabel("0.55")
        self._value_label.setStyleSheet(f"""
            QLabel {{
                color: {GRAPH_COLORS["threshold_line"]};
                font-size: 11px;
                font-weight: bold;
                min-width: 40px;
            }}
        """)
        layout.addWidget(self._value_label)

        # Connect signal
        self._slider.valueChanged.connect(self._on_slider_changed)

    def _apply_style(self) -> None:
        """Apply style."""
        self.setStyleSheet(f"""
            ThresholdSlider {{
                background-color: {GRAPH_COLORS["scrubber_bg"]};
                border: none;
                border-radius: 4px;
            }}
        """)

    def set_threshold(self, threshold: float) -> None:
        """
        Set threshold value.

        Args:
            threshold: Threshold (0.35-0.75).
        """
        if self._updating:
            return

        # AUD-20702: try/finally guards the anti-recursion lock.
        self._updating = True
        try:
            self._threshold = threshold
            value = int(threshold * 100)
            self._slider.setValue(value)
            self._value_label.setText(f"{threshold:.2f}")
        finally:
            self._updating = False

    def get_threshold(self) -> float:
        """Get current threshold."""
        return self._threshold

    def _on_slider_changed(self, value: int) -> None:
        """Handle slider change."""
        if not self._updating:
            threshold = value / 100.0
            self._threshold = threshold
            self._value_label.setText(f"{threshold:.2f}")
            self.threshold_changed.emit(threshold)

    def set_i18n_resolver(self, resolver: Any) -> None:
        """
        Set i18n resolver for updating label.

        Args:
            resolver: Function or object with t() method.
        """
        if hasattr(resolver, 't'):
            label_text = resolver.t("scene_cut.sensitivity", default="Sensitivity")
            self._label.setText(label_text)
