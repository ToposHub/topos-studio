# B1(2026-09-07): 生产入口已接线 — Edit→View→Timeline→Scene Cut Overlay
# checkable 菜单 + SceneCutDialog"标注到时间线"按钮（EditPage 统一开关与持久化）。
"""
Scene Cut Timeline Overlay - 临时 overlay 显示检测候选点.

This module provides a temporary visual overlay for displaying scene cut
detection candidates on the timeline without modifying the actual timeline data.

Design Principles:
- Overlay items are NOT timeline markers or clips, only temporary visuals
- Syncs with zoom/scroll using layout engine's frame_to_x
- Different colors for high confidence vs warning candidates
- Supports show/hide control from Scene Cut dialog
- Supports click on candidate to select for review
- Anti-recursion locks for signal connections
- No OpenGL calls (pure Qt painting)

Features:
- Vertical lines at candidate positions
- Small triangle markers at ruler bottom
- Color coding: green (accepted), yellow (pending), red (warning)
- Click to select candidate for review
- Hover tooltips showing candidate info

Reference:
- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md P5-1
- .clinerules: UI thread safety, signal anti-recursion
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, List

from PyQt6.QtWidgets import (
    QGraphicsObject,
    QGraphicsItem,
    QStyleOptionGraphicsItem,
    QWidget,
)
from PyQt6.QtCore import (
    Qt,
    QRectF,
    QPointF,
    pyqtSignal,
    QEvent,
)
from PyQt6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QFont,
    QPainterPath,
    QPolygonF,
)

from src.features.edit.ui.edit_style import EDIT_SIZES
from src.features.edit.ui.timeline_items import (
    RULER_HEIGHT,
)
from src.features.edit.ui.timeline_layout import (
    TimelineLayoutEngine,
)
from src.features.edit.core.scene_cut.models import (
    SceneCutCandidate,
    SceneCutDecision,
    SceneCutKind,
)

logger = logging.getLogger(__name__)


# ================================================================
# Constants
# ================================================================

# Overlay Z-value (above clips, below playhead)
OVERLAY_Z_VALUE = 520

# Marker visual sizes
CANDIDATE_LINE_WIDTH = 2
CANDIDATE_TRIANGLE_SIZE = 10
CANDIDATE_HANDLE_MARGIN = 8

# Colors for different states
# High confidence accepted: green
COLOR_ACCEPTED_HIGH = QColor(80, 200, 80)  # #50C880 bright green
COLOR_ACCEPTED_HIGH_HOVER = QColor(100, 255, 100)  # brighter on hover

# Accepted (normal): green-ish
COLOR_ACCEPTED = QColor(60, 180, 60)  # #3CB43C green

# Pending (normal): yellow
COLOR_PENDING = QColor(200, 180, 50)  # #C8B432 yellow/amber

# Warning candidates: orange/red
COLOR_WARNING = QColor(255, 120, 40)  # #FF7828 orange
COLOR_WARNING_HOVER = QColor(255, 150, 60)  # brighter on hover

# Rejected: gray (dimmed)
COLOR_REJECTED = QColor(100, 100, 100)  # gray, low opacity

# Hard cut indicator: dashed line
COLOR_HARD_CUT = QColor(80, 200, 80)  # green dashed

# Soft transition: purple
COLOR_SOFT_TRANSITION = QColor(180, 120, 200)  # #B478C8 purple

# Black gap: dark gray
COLOR_BLACK_GAP = QColor(80, 80, 90)  # dark gray

# Flash: cyan warning
COLOR_FLASH = QColor(200, 80, 200)  # #C850C8 magenta (warning)

# Fast motion: blue warning
COLOR_FAST_MOTION = QColor(100, 150, 200)  # #6496C8 blue (warning)

# Tooltip label
TOOLTIP_BG_COLOR = QColor(30, 30, 35, 220)  # semi-transparent dark
TOOLTIP_TEXT_COLOR = QColor(255, 255, 255, 240)
TOOLTIP_MAX_WIDTH = 150


# ================================================================
# CandidateMarkerItem
# ================================================================

class CandidateMarkerItem(QGraphicsObject):
    """
    Visual representation of a single scene cut candidate on timeline overlay.

    外观：
    - 竖线从 ruler 底部延伸到轨道区域
    - 倒三角在 ruler 底部
    - 不同状态用不同颜色
    - hover 时显示 tooltip

    Signals:
        candidate_clicked: Candidate was clicked (candidate_id)
        candidate_double_clicked: Candidate was double-clicked for quick accept/reject
        candidate_hovered: Candidate hover state changed (candidate_id, is_hovered)

    Attributes:
        _candidate: SceneCutCandidate data
        _layout_engine: Layout engine for frame_to_x
        _is_selected: Whether this candidate is currently selected for review
        _is_hovered: Hover state for visual feedback

    AUD-20818: ``pixels_per_frame``/``fps`` 构造参数仅为 API 兼容保留，
    几何一律经 ``_layout_engine.frame_to_x``，不再存储死状态。
    """

    # Signals
    candidate_clicked = pyqtSignal(str)  # candidate_id (source_frame as string)
    candidate_double_clicked = pyqtSignal(str)  # candidate_id
    candidate_hovered = pyqtSignal(str, bool)  # candidate_id, is_hovered

    def __init__(
        self,
        candidate: SceneCutCandidate,
        pixels_per_frame: float,
        layout_engine: TimelineLayoutEngine,
        num_tracks: int = 1,
        fps: float = 24.0,
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._candidate = candidate
        # AUD-20818: pixels_per_frame/fps 为存而不用的死状态，不再存储
        # （几何真值来自 layout_engine）；参数仅为 API 兼容保留。
        self._layout_engine = layout_engine
        self._num_tracks = num_tracks
        self._is_selected: bool = False
        self._is_hovered: bool = False
        # Actual cumulative track area height (AUD-20812; None = derive
        # from the layout engine, falling back to the num_tracks estimate)
        self._track_area_height: Optional[float] = None

        # Cache candidate_id for fast access (using source_frame as identifier)
        self._candidate_id = str(candidate.source_frame)

        # Determine color based on candidate state
        self._base_color = self._determine_color()

        # Set Z-value
        self.setZValue(OVERLAY_Z_VALUE)

        # Enable interactions
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        # Update geometry
        self._update_geometry()

        logger.debug(
            f"CandidateMarkerItem created: frame {candidate.source_frame}, "
            f"decision={candidate.decision.value}, kind={candidate.kind.value}"
        )

    # ==================== Properties ====================

    @property
    def candidate(self) -> SceneCutCandidate:
        return self._candidate

    @property
    def candidate_id(self) -> str:
        return self._candidate_id

    @property
    def source_frame(self) -> int:
        return self._candidate.source_frame

    @property
    def timeline_frame(self) -> int | None:
        return self._candidate.timeline_frame

    @property
    def is_selected(self) -> bool:
        return self._is_selected

    @property
    def is_warning(self) -> bool:
        return self._candidate.is_warning

    @property
    def decision(self) -> SceneCutDecision:
        return self._candidate.decision

    # ==================== Color Determination ====================

    def _determine_color(self) -> QColor:
        """
        Determine the base color based on candidate state.

        优先级：
        1. Rejected -> gray (dimmed)
        2. Warning -> orange/red
        3. Accepted high confidence -> bright green
        4. Accepted -> green
        5. Pending -> yellow
        """
        cand = self._candidate

        # Rejected: gray dimmed
        if cand.is_rejected:
            return COLOR_REJECTED

        # Warning states
        if cand.is_warning:
            # Different warning colors by kind
            if cand.kind == SceneCutKind.FLASH:
                return COLOR_FLASH
            elif cand.kind == SceneCutKind.FAST_MOTION:
                return COLOR_FAST_MOTION
            elif cand.kind == SceneCutKind.SOFT_TRANSITION:
                return COLOR_SOFT_TRANSITION
            elif cand.kind == SceneCutKind.BLACK_GAP:
                return COLOR_BLACK_GAP
            else:
                return COLOR_WARNING

        # Accepted high confidence: bright green
        if cand.is_accepted and cand.is_high_confidence:
            return COLOR_ACCEPTED_HIGH

        # Accepted: green
        if cand.is_accepted:
            return COLOR_ACCEPTED

        # Pending: yellow
        return COLOR_PENDING

    # ==================== Geometry ====================

    def _effective_track_height(self) -> float:
        """Return the track area height the vertical line should span.

        AUD-20812: 竖线长度此前按 ``num_tracks × 视频轨高(82px)`` 估算，
        含音频/字幕轨或轨道手动调高时线长越过真实轨道区。优先级：
        显式设置的精确高度 > 布局引擎的 total_track_area 真值（与
        PlayheadItem 同源）> num_tracks 估算兜底。
        """
        if self._track_area_height is not None and self._track_area_height > 0:
            return self._track_area_height
        try:
            engine_height = (
                self._layout_engine.area_layout().total_track_area.height()
            )
        except (AttributeError, RuntimeError):
            engine_height = 0.0
        if engine_height > 0:
            return engine_height
        return self._num_tracks * EDIT_SIZES["track_h_video"]

    def _marker_base_rect(self) -> QRectF:
        """Return the base rect (triangle + line + handle margin)."""
        triangle_height = CANDIDATE_TRIANGLE_SIZE
        half_size = CANDIDATE_TRIANGLE_SIZE / 2
        track_area_height = self._effective_track_height()
        width = CANDIDATE_TRIANGLE_SIZE + CANDIDATE_HANDLE_MARGIN
        total_height = triangle_height + track_area_height
        return QRectF(
            -half_size - CANDIDATE_HANDLE_MARGIN / 2,
            -triangle_height,
            width,
            total_height + CANDIDATE_HANDLE_MARGIN
        )

    def boundingRect(self) -> QRectF:
        """
        Return bounding rectangle including line, triangle and tooltip.

        Marker 位于 ruler 底部，竖线延伸到轨道区域。hover 时 tooltip
        绘制在 marker 上方，必须并入包围盒（AUD-20814），否则该区域
        不参与重绘，tooltip 出现/消失时留下残影。
        """
        rect = self._marker_base_rect()
        if self._is_hovered:
            rect = rect.united(self._tooltip_rect())
        return rect

    def shape(self) -> QPainterPath:
        """Return shape for collision detection (triangle area)."""
        path = QPainterPath()
        triangle_height = CANDIDATE_TRIANGLE_SIZE
        half_size = CANDIDATE_TRIANGLE_SIZE / 2

        # Triangle shape (pointing down)
        triangle = QPolygonF([
            QPointF(-half_size, -triangle_height),
            QPointF(half_size, -triangle_height),
            QPointF(0, 0)
        ])
        path.addPolygon(triangle)

        return path

    def _update_geometry(self) -> None:
        """Update X from frame and zoom. Y is fixed at ruler bottom."""
        # AUD-20810: timeline_frame 为 0 是合法帧位，`or` 会把第 0 帧
        # 当作假值回退到 source_frame，子片段/带偏移探测时标记错位。
        frame = (
            self._candidate.timeline_frame
            if self._candidate.timeline_frame is not None
            else self._candidate.source_frame
        )

        # Use layout engine for frame_to_x (consistent with timeline)
        x = self._layout_engine.frame_to_x(frame)
        y = float(RULER_HEIGHT)

        self.setPos(x, y)

    # ==================== State Updates ====================

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale.

        AUD-20818: zoom 值不再存储（几何真值来自 layout_engine），仅
        重算几何并重绘。
        """
        self._update_geometry()
        self.update()

    def update_layout_engine(self, engine: TimelineLayoutEngine) -> None:
        """Update layout engine reference.

        引擎决定 frame_to_x 与轨道区高度真值（AUD-20812），包围盒
        可能随之变化，先 prepareGeometryChange。
        """
        self.prepareGeometryChange()
        self._layout_engine = engine
        self._update_geometry()
        self.update()

    def update_num_tracks(self, num_tracks: int) -> None:
        """Update number of tracks (for line height)."""
        if num_tracks == self._num_tracks:
            return
        # AUD-20811: boundingRect 高度依赖轨道数，几何变化前必须
        # prepareGeometryChange（与 timeline_items 的 PlayheadItem 模式
        # 一致），否则场景 BSP 索引与缓存包围盒过期，重绘出现残影、
        # 命中测试在新区域失效。
        self.prepareGeometryChange()
        self._num_tracks = num_tracks
        self.update()

    def update_track_area_height(self, height: float) -> None:
        """Update the actual cumulative track area height (AUD-20812).

        布局真值入口（与 PlayheadItem 的同名方法对齐）；未设置时
        _effective_track_height 会向布局引擎查询。
        """
        if self._track_area_height != height:
            self.prepareGeometryChange()
            self._track_area_height = height
            self.update()

    def update_fps(self, fps: float) -> None:
        """Update frame rate for timecode display.

        AUD-20818: 本类不再渲染时间码（tooltip 由 candidate 数据驱动），
        fps 无消费方，仅保留 API 兼容；保留重绘以维持既有视觉刷新时机。
        """
        self.update()

    def update_candidate(self, candidate: SceneCutCandidate) -> None:
        """Update candidate data (e.g., after decision change)."""
        self._candidate = candidate
        self._base_color = self._determine_color()
        self._update_geometry()
        self.update()

    def set_selected(self, selected: bool) -> None:
        """Set selection state for visual highlight."""
        self._is_selected = selected
        self.update()

    # ==================== Painting ====================

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[QWidget] = None
    ) -> None:
        """
        Paint the candidate marker.

        Draws:
        1. Vertical line from ruler bottom to track area
        2. Triangle marker at ruler bottom
        3. Selection highlight if selected
        4. Tooltip on hover
        """
        painter.save()

        # Get color with selection/hover adjustments
        color = self._base_color
        if self._is_selected:
            # Brighter and thicker for selected
            color = color.lighter(130)
        elif self._is_hovered:
            color = color.lighter(110)

        # Opacity: rejected items are dimmed
        opacity = 1.0 if not self._candidate.is_rejected else 0.4
        painter.setOpacity(opacity)

        triangle_height = CANDIDATE_TRIANGLE_SIZE
        half_size = CANDIDATE_TRIANGLE_SIZE / 2

        # Track area height for line (AUD-20812: 布局真值，非估算)
        track_area_height = self._effective_track_height()

        # Draw vertical line
        line_pen = QPen(color, CANDIDATE_LINE_WIDTH)
        if self._candidate.kind == SceneCutKind.HARD_CUT:
            # Hard cuts use solid line
            line_pen.setStyle(Qt.PenStyle.SolidLine)
        else:
            # Other types use dashed or dotted
            line_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(line_pen)
        painter.drawLine(
            QPointF(0, 0),
            QPointF(0, track_area_height)
        )

        # Draw triangle (pointing down)
        painter.setPen(QPen(color, 1))
        painter.setBrush(QBrush(color))

        triangle = QPolygonF([
            QPointF(-half_size, -triangle_height),
            QPointF(half_size, -triangle_height),
            QPointF(0, 0)
        ])
        painter.drawPolygon(triangle)

        # Selection highlight border
        if self._is_selected:
            sel_pen = QPen(QColor(255, 136, 0), 2)  # Orange selection border
            painter.setPen(sel_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._marker_base_rect().adjusted(2, 2, -2, -2))

        # Hover tooltip
        if self._is_hovered:
            self._draw_tooltip(painter)

        painter.restore()

    def _tooltip_lines(self) -> List[str]:
        """Return tooltip text lines for the current candidate."""
        cand = self._candidate
        return [
            f"Frame: {cand.source_frame}",
            f"Time: {cand.seconds:.2f}s",
            f"Score: {cand.score:.2f}",
            f"Conf: {cand.confidence:.2f}",
            f"Kind: {cand.kind.value}",
            f"Decision: {cand.decision.value}",
        ]

    def _tooltip_rect(self) -> QRectF:
        """Return the tooltip rect in item coordinates (above the marker)."""
        lines = self._tooltip_lines()
        max_line_width = max(len(line) for line in lines)
        tooltip_width = min(TOOLTIP_MAX_WIDTH, max_line_width * 7 + 10)
        line_height = 14
        tooltip_height = len(lines) * line_height + 8
        tooltip_x = -tooltip_width / 2
        tooltip_y = -CANDIDATE_TRIANGLE_SIZE - tooltip_height - 5
        return QRectF(tooltip_x, tooltip_y, tooltip_width, tooltip_height)

    def _draw_tooltip(self, painter: QPainter) -> None:
        """Draw hover tooltip with candidate info."""
        lines = self._tooltip_lines()

        # Calculate tooltip size
        font = QFont("Arial", 9)
        painter.setFont(font)

        line_height = 14
        tooltip_rect = self._tooltip_rect()
        tooltip_x = tooltip_rect.x()
        tooltip_y = tooltip_rect.y()

        # Draw tooltip background
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(TOOLTIP_BG_COLOR))
        painter.drawRoundedRect(tooltip_rect, 4, 4)

        # Draw text
        painter.setPen(QPen(TOOLTIP_TEXT_COLOR))
        text_y = tooltip_y + 5
        for line in lines:
            painter.drawText(
                QPointF(tooltip_x + 5, text_y),
                line
            )
            text_y += line_height

    # ==================== Interaction ====================

    def mousePressEvent(self, event: QEvent) -> None:
        """Handle mouse press for selection."""
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            # Will emit clicked on release
        else:
            event.ignore()

    def mouseReleaseEvent(self, event: QEvent) -> None:
        """Handle mouse release - emit clicked signal."""
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.candidate_clicked.emit(self._candidate_id)
            logger.debug(f"Candidate clicked: {self._candidate_id}")
        else:
            event.ignore()

    def mouseDoubleClickEvent(self, event: QEvent) -> None:
        """Handle double-click for quick accept/reject."""
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.candidate_double_clicked.emit(self._candidate_id)
            logger.debug(f"Candidate double-clicked: {self._candidate_id}")
        else:
            event.ignore()

    def hoverEnterEvent(self, event: QEvent) -> None:
        """Handle hover enter.

        AUD-20814: hover 使 boundingRect 扩张以覆盖 tooltip 区域，
        必须先 prepareGeometryChange 再切换状态。
        """
        self.prepareGeometryChange()
        self._is_hovered = True
        self.update()
        self.candidate_hovered.emit(self._candidate_id, True)

    def hoverLeaveEvent(self, event: QEvent) -> None:
        """Handle hover leave (boundingRect 收缩回基础矩形)."""
        self.prepareGeometryChange()
        self._is_hovered = False
        self.update()
        self.candidate_hovered.emit(self._candidate_id, False)


# ================================================================
# SceneCutTimelineOverlay
# ================================================================

class SceneCutTimelineOverlay(QGraphicsObject):
    """
    Manager for all scene cut candidate overlay items on timeline.

    功能：
    - 显示/隐藏所有候选点 overlay
    - 管理候选点 marker items
    - 同步 zoom/scroll 更新
    - 处理候选点选择
    - 不写入 timeline 数据

    Signals:
        candidate_selected: Candidate selected for review (candidate_id)
        candidate_double_clicked: Candidate double-clicked for quick action
        overlay_visibility_changed: Overlay visibility changed (visible)

    Attributes:
        _candidates: Current candidate list
        _marker_items: Dict of CandidateMarkerItem
        _layout_engine: Layout engine for frame_to_x
        _pixels_per_frame: Current zoom scale
        _num_tracks: Number of tracks for line height
        _fps: Timeline frame rate
        _is_visible: Overlay visibility state
        _selected_candidate_id: Currently selected candidate
        _updating: Anti-recursion lock
    """

    # Signals
    candidate_selected = pyqtSignal(str)  # candidate_id
    candidate_double_clicked = pyqtSignal(str)  # candidate_id
    overlay_visibility_changed = pyqtSignal(bool)  # visible

    def __init__(
        self,
        layout_engine: TimelineLayoutEngine,
        pixels_per_frame: float = 5.0,
        num_tracks: int = 1,
        fps: float = 24.0,
        parent: Optional[QGraphicsItem] = None
    ) -> None:
        super().__init__(parent)

        self._layout_engine = layout_engine
        self._pixels_per_frame = pixels_per_frame
        self._num_tracks = num_tracks
        self._fps = fps
        self._is_visible: bool = False
        self._selected_candidate_id: Optional[str] = None
        self._updating: bool = False  # Anti-recursion lock

        # Candidate data and marker items
        self._candidates: tuple[SceneCutCandidate, ...] = ()
        self._marker_items: Dict[str, CandidateMarkerItem] = {}

        # Set Z-value (above clips, below playhead)
        self.setZValue(OVERLAY_Z_VALUE - 10)

        logger.debug("SceneCutTimelineOverlay created")

    # ==================== Properties ====================

    @property
    def is_visible(self) -> bool:
        return self._is_visible

    @property
    def candidates(self) -> tuple[SceneCutCandidate, ...]:
        return self._candidates

    @property
    def selected_candidate_id(self) -> Optional[str]:
        return self._selected_candidate_id

    # ==================== Candidate Management ====================

    def set_candidates(
        self,
        candidates: tuple[SceneCutCandidate, ...]
    ) -> None:
        """
        Set candidate list and create/update marker items.

        Args:
            candidates: Tuple of SceneCutCandidate from detection analysis
        """
        if self._updating:
            return

        self._updating = True
        try:
            # Store candidates
            self._candidates = candidates

            # Clear existing markers
            self._clear_markers()

            # Create new markers
            for cand in candidates:
                self._create_marker_item(cand)

            # Update visibility
            self._update_visibility()

            logger.debug(f"Set {len(candidates)} candidates in overlay")
        finally:
            self._updating = False

    def update_candidates(
        self,
        candidates: tuple[SceneCutCandidate, ...]
    ) -> None:
        """
        Update candidate data without recreating all markers.

        Used when decisions change during review.

        Args:
            candidates: Updated tuple of SceneCutCandidate
        """
        if self._updating:
            return

        self._updating = True
        try:
            self._candidates = candidates

            # Update existing markers
            for cand in candidates:
                cand_id = str(cand.source_frame)
                marker = self._marker_items.get(cand_id)
                if marker:
                    marker.update_candidate(cand)
                else:
                    # New candidate, create marker
                    self._create_marker_item(cand)

            # Remove markers for candidates no longer in list
            current_ids = {str(c.source_frame) for c in candidates}
            for cand_id in list(self._marker_items.keys()):
                if cand_id not in current_ids:
                    self._remove_marker_item(cand_id)

            self.update()
        finally:
            self._updating = False

    def _create_marker_item(self, candidate: SceneCutCandidate) -> None:
        """Create a single marker item for a candidate."""
        cand_id = str(candidate.source_frame)

        marker = CandidateMarkerItem(
            candidate=candidate,
            pixels_per_frame=self._pixels_per_frame,
            layout_engine=self._layout_engine,
            num_tracks=self._num_tracks,
            fps=self._fps,
            parent=self
        )

        # Connect signals
        marker.candidate_clicked.connect(self._on_candidate_clicked)
        marker.candidate_double_clicked.connect(self._on_candidate_double_clicked)
        marker.candidate_hovered.connect(self._on_candidate_hovered)

        self._marker_items[cand_id] = marker

        # Set visibility
        marker.setVisible(self._is_visible)

    def _remove_marker_item(self, candidate_id: str) -> None:
        """Remove a marker item."""
        marker = self._marker_items.pop(candidate_id, None)
        if marker and marker.scene():
            marker.scene().removeItem(marker)

    def _clear_markers(self) -> None:
        """Remove all marker items."""
        for cand_id in list(self._marker_items.keys()):
            self._remove_marker_item(cand_id)
        self._marker_items.clear()

    # ==================== Selection ====================

    def select_candidate(self, candidate_id: str) -> None:
        """
        Select a candidate for review.

        Args:
            candidate_id: Candidate ID (source_frame as string)
        """
        if self._updating:
            return

        self._updating = True
        try:
            # Clear previous selection
            if self._selected_candidate_id:
                prev_marker = self._marker_items.get(self._selected_candidate_id)
                if prev_marker:
                    prev_marker.set_selected(False)

            # Set new selection
            self._selected_candidate_id = candidate_id
            marker = self._marker_items.get(candidate_id)
            if marker:
                marker.set_selected(True)
        finally:
            self._updating = False
        # AUD-20813: 直连信号槽为同步调用，锁内 emit 会使处理器内对本
        # overlay 的回写（select/update/set_candidates 均以 _updating
        # 拦截）被静默丢弃；与 SceneCutResultModel"锁内改状态、锁外发
        # 信号"的时序对齐，先释放锁再发射。
        self.candidate_selected.emit(candidate_id)

    def clear_selection(self) -> None:
        """Clear candidate selection."""
        if self._selected_candidate_id:
            marker = self._marker_items.get(self._selected_candidate_id)
            if marker:
                marker.set_selected(False)
            self._selected_candidate_id = None

    def _on_candidate_clicked(self, candidate_id: str) -> None:
        """Handle candidate click from marker."""
        self.select_candidate(candidate_id)

    def _on_candidate_double_clicked(self, candidate_id: str) -> None:
        """Handle candidate double-click."""
        self.candidate_double_clicked.emit(candidate_id)

    def _on_candidate_hovered(self, candidate_id: str, is_hovered: bool) -> None:
        """Handle candidate hover state."""
        # Could use for additional visual feedback
        pass

    # ==================== Visibility ====================

    def show_overlay(self) -> None:
        """Show all candidate markers."""
        # AUD-20815: guard with the same _updating anti-recursion lock used by
        # set_candidates/select_candidate so a listener wired back into this
        # overlay cannot loop through overlay_visibility_changed.
        if self._updating:
            return
        self._updating = True
        try:
            self._is_visible = True
            self._update_visibility()
        finally:
            self._updating = False
        self.overlay_visibility_changed.emit(True)

    def hide_overlay(self) -> None:
        """Hide all candidate markers."""
        # AUD-20815: same anti-recursion guard as show_overlay.
        if self._updating:
            return
        self._updating = True
        try:
            self._is_visible = False
            self._update_visibility()
        finally:
            self._updating = False
        self.overlay_visibility_changed.emit(False)

    def toggle_overlay(self) -> None:
        """Toggle overlay visibility."""
        if self._is_visible:
            self.hide_overlay()
        else:
            self.show_overlay()

    def _update_visibility(self) -> None:
        """Update marker item visibility."""
        for marker in self._marker_items.values():
            marker.setVisible(self._is_visible)
        self.update()

    # ==================== Zoom/Scroll Sync ====================

    def update_zoom(self, pixels_per_frame: float) -> None:
        """Update zoom scale for all markers."""
        self._pixels_per_frame = pixels_per_frame
        for marker in self._marker_items.values():
            marker.update_zoom(pixels_per_frame)

    def update_layout_engine(self, engine: TimelineLayoutEngine) -> None:
        """Update layout engine reference for all markers."""
        self._layout_engine = engine
        for marker in self._marker_items.values():
            marker.update_layout_engine(engine)

    def update_num_tracks(self, num_tracks: int) -> None:
        """Update number of tracks for line height."""
        self._num_tracks = num_tracks
        for marker in self._marker_items.values():
            marker.update_num_tracks(num_tracks)

    def update_track_area_height(self, height: float) -> None:
        """Update the actual cumulative track area height (AUD-20812)."""
        for marker in self._marker_items.values():
            marker.update_track_area_height(height)

    def update_fps(self, fps: float) -> None:
        """Update timeline frame rate."""
        self._fps = fps
        for marker in self._marker_items.values():
            marker.update_fps(fps)

    # ==================== Geometry ====================

    def boundingRect(self) -> QRectF:
        """Return bounding rect encompassing all markers."""
        # Overlay doesn't paint itself, children do
        return QRectF(0, 0, 0, 0)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[QWidget] = None
    ) -> None:
        """Overlay manager doesn't paint itself, children paint."""
        # Children (CandidateMarkerItem) paint themselves
        pass

    # ==================== Cleanup ====================

    def clear(self) -> None:
        """Clear all candidates and markers."""
        self._clear_markers()
        self._candidates = ()
        self._selected_candidate_id = None

    def cleanup(self) -> None:
        """Cleanup before destruction."""
        self._clear_markers()


# ================================================================
# Factory and Integration Helpers
# ================================================================

def create_scene_cut_overlay(
    layout_engine: TimelineLayoutEngine,
    pixels_per_frame: float = 5.0,
    num_tracks: int = 1,
    fps: float = 24.0
) -> SceneCutTimelineOverlay:
    """
    Factory function to create a scene cut timeline overlay.

    Args:
        layout_engine: Timeline layout engine for frame_to_x
        pixels_per_frame: Current zoom scale
        num_tracks: Number of tracks for line height
        fps: Timeline frame rate

    Returns:
        SceneCutTimelineOverlay instance
    """
    return SceneCutTimelineOverlay(
        layout_engine=layout_engine,
        pixels_per_frame=pixels_per_frame,
        num_tracks=num_tracks,
        fps=fps
    )


def sync_overlay_with_timeline(
    overlay: SceneCutTimelineOverlay,
    layout_engine: TimelineLayoutEngine,
    pixels_per_frame: float,
    num_tracks: int,
    fps: float,
    track_area_height: Optional[float] = None,
) -> None:
    """
    Sync overlay with timeline zoom/scroll state.

    Args:
        overlay: SceneCutTimelineOverlay instance
        layout_engine: Layout engine
        pixels_per_frame: Current zoom scale
        num_tracks: Current track count
        fps: Timeline frame rate
        track_area_height: Actual cumulative track area height
            (AUD-20812; omitted/None 时 marker 自行向布局引擎查询)
    """
    overlay.update_layout_engine(layout_engine)
    overlay.update_zoom(pixels_per_frame)
    overlay.update_num_tracks(num_tracks)
    overlay.update_fps(fps)
    if track_area_height is not None:
        overlay.update_track_area_height(track_area_height)