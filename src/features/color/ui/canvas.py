from PyQt6.QtWidgets import (QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
                             QGraphicsRectItem, QWidget,
                             QHBoxLayout, QVBoxLayout, QPushButton, QLabel, QGraphicsEllipseItem, QGraphicsItem, QApplication, QMenu, QToolButton, QSizePolicy, QFrame, QSlider)
from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import (IconColors,
    icon_jump_start, icon_step_back, icon_play, icon_pause,
    icon_step_forward, icon_jump_end, icon_loop)
from PyQt6.QtCore import Qt, QRectF, pyqtSignal, pyqtSlot, QPoint, QPointF, QLineF, QTimer, QSize
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QPen, QColor, QBrush, QPainterPath,
    QTransform, QPainterPathStroker, QKeySequence, QPolygonF, QResizeEvent,
)
from typing import Any, List, Tuple, Optional
from src.features.color.ui.gl_viewport import GLCanvasViewport
from src.features.color.ui.tracker_overlay import TrackerOverlay, OverlayState
from src.features.color.core.layer import MaskGroup
from src.shared.utils.geometry_math import get_largest_inscribed_rect
from src.shared.ui.widgets.viewport_toolbar_base import (
    ViewportTimelineSelector,
    ViewportToolBarBase,
)
from src.shared.diagnostics import flash_diag as _flash_diag
import numpy as np
import math
import os
import logging

logger = logging.getLogger(__name__)


def _inject_menu_shortcuts(menu) -> None:
    """将 QAction 的快捷键以 \\t 格式追加到文本，确保弹出菜单中可见。"""
    for action in menu.actions():
        if action.menu():
            _inject_menu_shortcuts(action.menu())
        elif not action.isSeparator():
            seq = action.shortcut()
            if not seq.isEmpty() and '\t' not in action.text():
                action.setText(
                    action.text() + '\t' +
                    seq.toString(QKeySequence.SequenceFormat.NativeText)
                )


class MaskHandle(QGraphicsEllipseItem):
    """Handle for interacting with mask shapes.
    
    Optimized for smooth dragging performance:
    - During drag: Only updates Qt UI (no engine sync)
    - On release: Single sync to engine for high-res render
    """
    def __init__(self, handle_type, parent=None, index=-1, sub_type=None) -> None:
        super().__init__(parent)
        self.handle_type = handle_type # 'resize_w', 'resize_h', 'resize_w_neg', 'resize_h_neg', 'rotate', 'feather', 'node', 'ctrl', 'grad_start', 'grad_end', 'center'
        self.index = index
        self.sub_type = sub_type # 'c1', 'c2' for bezier
        
        # Performance optimization: Track dragging state to skip engine sync during drag
        self._is_dragging = False
        
        # DaVinci style: Larger, easier to grab handles
        size = 9 if handle_type in ['node', 'grad_start', 'grad_end', 'rotate'] else 8
        if handle_type == 'ctrl': size = 7
        if handle_type == 'feather_curve': size = 7
        if handle_type == 'center': size = 11
        self.setRect(-size/2, -size/2, size, size)
        
        # DaVinci 级别视觉：Cosmetic Pen 保证无论缩放级别，线条始终保持恒定物理像素宽度
        # 边框颜色：未激活时使用低对比度深灰，在极亮/极暗背景上都能看清
        border_pen = QPen(QColor(60, 60, 60), 1.0)
        border_pen.setCosmetic(True)  # 关键：装饰性画笔，不随缩放变化
        
        # 默认填充：白色带微透明，确保可见性
        self.setBrush(QBrush(QColor(255, 255, 255, 240)))
        self.setPen(border_pen)
        
        if handle_type in ('feather', 'feather_r', 'feather_l', 'feather_t', 'feather_b', 'feather_curve'):
            self.setBrush(QBrush(QColor(0, 255, 255, 220))) # Cyan for feather
        elif handle_type == 'ctrl':
            self.setBrush(QBrush(QColor(180, 180, 180, 200))) # Grey for bezier controls
        elif handle_type == 'center':
            self.setBrush(QBrush(QColor(255, 255, 255, 180)))
            
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | 
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges |
                      QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                      
        # Center handle is for pivot and movement, shouldn't move relative to parent itself
        if handle_type == 'center':
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

        self.setZValue(31 if handle_type in ['node', 'grad_start', 'grad_end', 'rotate'] else 30)
        
        # 根据 handle_type 设置精准的鼠标指针
        if handle_type in ('resize_w', 'resize_w_neg', 'feather_r', 'feather_l'):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif handle_type in ('resize_h', 'resize_h_neg', 'feather_t', 'feather_b'):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif handle_type == 'rotate':
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif handle_type == 'feather':
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            # 'node', 'center', 'ctrl', 'grad_start', 'grad_end' 等
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.set_video_window_style(getattr(parent, '_video_window_mode', False) is True)

    def set_video_window_style(self, enabled: bool) -> None:
        """Keep video controls small while providing a separate mouse target."""
        self.prepareGeometryChange()
        self._video_window_style = bool(enabled)
        if enabled:
            diameter = 7.0 if self.handle_type == 'center' else 6.0
            self.setRect(-diameter / 2.0, -diameter / 2.0, diameter, diameter)
        else:
            diameter = 9.0 if self.handle_type in ('node', 'grad_start', 'grad_end', 'rotate') else 8.0
            if self.handle_type in ('ctrl', 'feather_curve'):
                diameter = 7.0
            elif self.handle_type == 'center':
                diameter = 11.0
            self.setRect(-diameter / 2.0, -diameter / 2.0, diameter, diameter)
        if self.handle_type in ('resize_tl', 'resize_br'):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif self.handle_type in ('resize_tr', 'resize_bl'):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        self.update()

    def boundingRect(self) -> QRectF:
        if getattr(self, '_video_window_style', False):
            return QRectF(-8.0, -8.0, 16.0, 16.0)
        return super().boundingRect()

    def shape(self) -> QPainterPath:
        if getattr(self, '_video_window_style', False):
            path = QPainterPath()
            path.addEllipse(self.boundingRect())
            return path
        return super().shape()

    def paint(self, painter, option, widget) -> None:
        if self._video_window_style:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor(255, 136, 0), 1.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            hollow = self.handle_type in ('center', 'rotate') or self.handle_type.startswith('feather')
            painter.setBrush(Qt.BrushStyle.NoBrush if hollow else QBrush(QColor(255, 242, 220)))
            painter.drawEllipse(self.rect())
            painter.restore()
            return
        # Override to ensure crisp rendering
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # For center handle, add a subtle crosshair or dot to make it look like DaVinci
        if self.handle_type == 'center':
            # DaVinci 级别视觉：使用 Cosmetic Pen 保证十字线恒定像素宽度
            cross_pen = QPen(QColor(0, 0, 0, 200), 1.0)
            cross_pen.setCosmetic(True)
            painter.setPen(cross_pen)
            painter.drawLine(QPointF(-6, 0), QPointF(6, 0))
            painter.drawLine(QPointF(0, -6), QPointF(0, 6))
        super().paint(painter, option, widget)

    def mouseMoveEvent(self, event) -> None:
        # Requirement: Dragging center point should move the entire shape
        if self.handle_type == 'center' and self.parentItem():
            delta = event.scenePos() - event.lastScenePos()
            self.parentItem().moveBy(delta.x(), delta.y())
            return # Don't move the handle relative to parent

        # Rotation handle: calculate angle directly from mouse scene position
        if self.handle_type == 'rotate' and self.parentItem():
            parent = self.parentItem()
            scene_pos = event.scenePos()
            scene_center = parent.scenePos()
            rel = scene_pos - scene_center
            angle = math.degrees(math.atan2(rel.y(), rel.x())) + 90
            parent.rotation = angle
            parent.setRotation(angle)
            parent.update()
            if self._is_dragging:
                parent._schedule_sync()
            else:
                parent._sync_back()
            event.accept()
            return

        # Performance optimization: Let the handle move, but skip engine sync during drag
        super().mouseMoveEvent(event)

        # During drag: Only update UI (no engine sync)
        # The itemChange will handle the position update with skip_sync=True
        if self._is_dragging and self.parentItem():
            # Just trigger a visual update of the curve path
            self.parentItem().update()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.parentItem():
            parent = self.parentItem()
            # Safety: skip if parent is being destroyed or handles are locked
            if getattr(parent, '_is_updating_handles', False):
                return value
            # P1-COLOR-09: 父形状锁定时句柄冻结在原位
            if getattr(parent, '_locked', False):
                return self.pos()
            # Rotate handle is fully managed in mouseMoveEvent — don't move or constrain it
            if self.handle_type == 'rotate':
                return self.pos()
            if hasattr(parent, 'on_handle_moved'):
                # Pass skip_sync=True during drag to prevent engine sync on every frame
                parent.on_handle_moved(self, value, skip_sync=self._is_dragging)
            # Constrain handles to their correct axis-aligned positions
            if self.handle_type in ('resize_w', 'resize_w_neg', 'feather_r', 'feather_l'):
                # These handles only move along X axis
                value = QPointF(value.x(), 0)
            elif self.handle_type in ('resize_h', 'resize_h_neg', 'feather_t', 'feather_b'):
                # These handles only move along Y axis
                value = QPointF(0, value.y())
            # Force feather handles to exact outline positions
            if self.handle_type in ('feather_r', 'feather_l', 'feather_t', 'feather_b'):
                if getattr(parent, '_video_window_mode', False):
                    return parent._video_feather_position(self.handle_type)
                try:
                    if parent.shape_type == "circle":
                        fx = parent.feather / 100.0 * parent.size[0]
                        fy = parent.feather / 100.0 * parent.size[1]
                    else:
                        fx = parent.feather / 100.0 * (parent.size[0] / 2)
                        fy = parent.feather / 100.0 * (parent.size[1] / 2)
                    if self.handle_type == 'feather_r':
                        edge_x = parent.size[0] if parent.shape_type == "circle" else parent.size[0]/2
                        value = QPointF(edge_x + fx, 0)
                    elif self.handle_type == 'feather_l':
                        edge_x = parent.size[0] if parent.shape_type == "circle" else parent.size[0]/2
                        value = QPointF(-(edge_x + fx), 0)
                    elif self.handle_type == 'feather_t':
                        edge_y = parent.size[1] if parent.shape_type == "circle" else parent.size[1]/2
                        value = QPointF(0, -(edge_y + fy))
                    elif self.handle_type == 'feather_b':
                        edge_y = parent.size[1] if parent.shape_type == "circle" else parent.size[1]/2
                        value = QPointF(0, edge_y + fy)
                except (RuntimeError, AttributeError):
                    pass
            if self.handle_type in ('resize_tl', 'resize_tr', 'resize_bl', 'resize_br'):
                return parent._video_corner_position(self.handle_type)
        return super().itemChange(change, value)

    def mousePressEvent(self, event) -> None:
        # P1-COLOR-09: 父形状锁定时句柄不响应（含 Ctrl 删点）
        if self.parentItem() is not None and getattr(self.parentItem(), '_locked', False):
            event.ignore()
            return
        # Mark drag start for performance optimization
        self._is_dragging = True
        parent = self.parentItem()
        if parent is not None and self.handle_type in ('resize_tl', 'resize_tr', 'resize_bl', 'resize_br'):
            parent._corner_resize_start_size = tuple(parent.size)
        
        # Notify canvas about interaction and set type
        if self.parentItem() and hasattr(self.parentItem(), 'canvas'):
            self.parentItem().canvas.current_interaction_type = self.handle_type
            self.parentItem().canvas.interaction_started.emit()

        # Node deletion with Ctrl/Cmd + Click
        modifiers = QApplication.keyboardModifiers()
        is_ctrl = modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        
        if is_ctrl and self.handle_type == 'node' and hasattr(self.parentItem(), 'on_node_deleted'):
            self._is_dragging = False  # Reset state for deletion
            self.parentItem().on_node_deleted(self.index)
            if hasattr(self.parentItem(), 'canvas'):
                QTimer.singleShot(0, self.parentItem().canvas.interaction_ended.emit)
            event.accept()
            return
            
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        # End drag state
        self._is_dragging = False
        
        super().mouseReleaseEvent(event)
        
        # Performance: Stop throttled timer and force immediate final sync
        # This ensures the last position is always synchronized to the engine
        if self.parentItem():
            parent = self.parentItem()
            parent._corner_resize_start_size = None
            # Stop any pending throttled sync
            if hasattr(parent, '_sync_timer'):
                parent._sync_timer.stop()
            # Force immediate final sync
            if hasattr(parent, '_sync_back'):
                parent._sync_back()
            # Notify canvas that interaction ended
            if hasattr(parent, 'canvas'):
                parent.canvas.interaction_ended.emit()

    def mouseDoubleClickEvent(self, event) -> None:
        if self.handle_type == 'node' and hasattr(self.parentItem(), 'on_node_double_clicked'):
            self.parentItem().on_node_double_clicked(self.index)
        super().mouseDoubleClickEvent(event)

class CropHandle(QGraphicsRectItem):
    """Handle for cropping interaction."""
    def __init__(self, handle_type, parent=None) -> None:
        super().__init__(parent)
        self.handle_type = handle_type # 'tl', 'tr', 'bl', 'br', 't', 'b', 'l', 'r'
        size = 12
        self.setRect(-size/2, -size/2, size, size)
        self.setBrush(QBrush(QColor(255, 255, 255, 240)))
        # DaVinci 级别视觉：Cosmetic Pen 保证边框恒定像素宽度
        border_pen = QPen(QColor(60, 60, 60), 1.0)
        border_pen.setCosmetic(True)
        self.setPen(border_pen)
        # Remove ItemIsMovable and ItemSendsGeometryChanges to prevent native dragging conflicts
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setCursor(self._get_cursor())

    def _get_cursor(self):
        if self.handle_type in ['tl', 'br']: return Qt.CursorShape.SizeFDiagCursor
        if self.handle_type in ['tr', 'bl']: return Qt.CursorShape.SizeBDiagCursor
        if self.handle_type in ['t', 'b']: return Qt.CursorShape.SizeVerCursor
        if self.handle_type in ['l', 'r']: return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.PointingHandCursor

    def mousePressEvent(self, event) -> None:
        # Handle interaction state
        if self.parentItem() and hasattr(self.parentItem(), 'canvas'):
            self.parentItem().canvas.current_interaction_type = "crop_resize"
            self.parentItem().canvas.interaction_started.emit()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self.parentItem():
            # Map scene mouse pos to the parent's local space
            local_pos = self.parentItem().mapFromScene(event.scenePos())
            self.parentItem().on_handle_moved(self, local_pos)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self.parentItem() and hasattr(self.parentItem(), 'canvas'):
            self.parentItem().canvas.interaction_ended.emit()
        event.accept()


class CropRectItem(QGraphicsItem):
    """Visual representation of the crop area with handles and rotation support.
    
    Lightroom-style "Adaptive Inscribed Rectangle" rotation cropping:
    - CropRectItem always stays at rotation = 0 (never rotated)
    - When angle changes, the safe inscribed rectangle is recalculated
    - Crop box is constrained within the safe rectangle to prevent transparent edges
    - Aspect ratio is LOCKED during rotation to prevent shape deformation
    
    Features:
    - Auto-scaling crop box when rotation angle changes
    - Boundary constraints based on get_largest_inscribed_rect()
    - Clean separation between rotation and crop dimensions
    - Rotation maintains aspect ratio (Lightroom-style)
    """
    def __init__(self, canvas, parent=None) -> None:
        super().__init__(parent)
        self.canvas = canvas
        self.rect = QRectF(-50, -50, 100, 100) # Local coords centered at origin
        self.rotation_val = 0.0  # Logical rotation value (for calculation, not visual rotation)
        self.aspect_ratio = None # None for Free
        self._is_rotating = False
        self._start_rot_pos = None
        self._start_rotation = 0.0
        
        # Rotation-time aspect ratio locking (key for preventing deformation)
        # When rotation starts, we lock the current crop rect's aspect ratio
        # and use it throughout the rotation to maintain shape
        self._rotation_locked_aspect_ratio: Optional[float] = None
        
        # Lightroom-style adaptive inscribed rectangle
        self._safe_rect_size: Tuple[float, float] = (100.0, 100.0)  # (width, height) of safe rect
        
        # Canvas extension feature: Allow crop box to extend beyond image bounds
        # When False, crop box can be freely dragged outside the image (canvas extension)
        self._constrain_to_image: bool = True
        
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | 
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setZValue(100)
        self.setAcceptHoverEvents(True)

        # Create resize handles
        self.handles = {}
        for htype in ['tl', 'tr', 'bl', 'br', 't', 'b', 'l', 'r']:
            self.handles[htype] = CropHandle(htype, self)
            
        self._is_updating = False
        self._update_handles()
    
    def update_safe_rect(self, angle: float, aspect_ratio: float = None) -> Tuple[float, float]:
        """Calculate and store the largest inscribed rectangle for the given angle.
        
        This is the core of Lightroom-style rotation cropping:
        - When user rotates, we calculate the maximum crop box that fits
        - within the rotated image bounds (no transparent edges)
        - The aspect ratio is LOCKED during rotation to prevent shape deformation
        
        Args:
            angle: Rotation angle in degrees
            aspect_ratio: Target aspect ratio (width/height). If None, uses the
                         locked rotation aspect ratio or falls back to image ratio.
            
        Returns:
            Tuple of (safe_width, safe_height)
        """
        if not self.canvas.logical_size:
            return self._safe_rect_size
        
        img_w = self.canvas.logical_size.width()
        img_h = self.canvas.logical_size.height()
        
        # Determine which aspect ratio to use (priority order):
        # 1. Explicitly passed aspect_ratio parameter
        # 2. Current fixed aspect_ratio (from aspect mode like 16:9, 1:1)
        # 3. Locked rotation aspect ratio (set when rotation starts in Free mode)
        # 4. Current crop rect aspect ratio (for Free mode)
        # 5. Original image aspect ratio (fallback)
        
        # If no aspect_ratio passed, determine from current state
        if aspect_ratio is None:
            # Priority: Fixed aspect ratio mode > Locked rotation ratio > Current rect ratio
            if self.aspect_ratio is not None:
                # Fixed aspect ratio mode: always use the preset ratio
                aspect_ratio = self.aspect_ratio
            elif self._rotation_locked_aspect_ratio is not None:
                # Rotation started in Free mode: use locked ratio
                aspect_ratio = self._rotation_locked_aspect_ratio
            else:
                # Free mode without rotation: use current crop rect's aspect ratio
                current_w = self.rect.width()
                current_h = self.rect.height()
                if current_w > 0 and current_h > 0:
                    aspect_ratio = current_w / current_h
        
        # Use the geometry math utility to calculate inscribed rect with aspect ratio constraint
        safe_w, safe_h = get_largest_inscribed_rect(img_w, img_h, angle, aspect_ratio)
        
        self._safe_rect_size = (safe_w, safe_h)
        return self._safe_rect_size
    
    def _apply_crop_constraints(self) -> None:
        """统一的约束更新方法：确保裁切框在当前旋转角度和比例下合法存在。
        
        此方法必须在以下情况下被调用：
        1. aspect_ratio 改变时
        2. rotation 改变时
        3. 任何可能导致裁切框超出边界操作完成后
        
        Core Logic:
        - 获取原图绝对尺寸作为基准
        - 确定当前比例（固定模式 vs Free 模式）
        - 计算安全区域并执行约束 (Clamp)
        - 以图像中心为基准缩放/重置裁切框
        - 【关键修复】位置约束使用安全区域尺寸，而非原图尺寸
        """
        # 画布扩展功能：如果不需要限制在画内，完全跳过边界约束
        if not self._constrain_to_image:
            return
        
        if not self.canvas.logical_size:
            return
        
        # Step 1: 获取原图绝对尺寸（永远是原图尺寸）
        img_w = self.canvas.logical_size.width()
        img_h = self.canvas.logical_size.height()
        
        # Step 2: 确定当前比例
        if self.aspect_ratio is not None:
            # 固定比例模式（如 16:9, 1:1）
            current_ratio = self.aspect_ratio
        else:
            # Free 模式：使用当前裁切框的实际比例
            current_w = self.rect.width()
            current_h = self.rect.height()
            if current_w > 0 and current_h > 0:
                current_ratio = current_w / current_h
            else:
                current_ratio = img_w / img_h
        
        # Step 3: 计算安全区域（旋转后的最大内接矩形）
        safe_w, safe_h = get_largest_inscribed_rect(img_w, img_h, self.rotation_val, current_ratio)
        
        # Step 4: 执行尺寸约束 (Clamp)
        current_w = self.rect.width()
        current_h = self.rect.height()
        
        # 计算缩放因子：确保裁切框不超出安全区域
        scale_x = safe_w / current_w if current_w > 0 else 1.0
        scale_y = safe_h / current_h if current_h > 0 else 1.0
        scale = min(scale_x, scale_y)
        
        # 如果裁切框超出安全区域，需要缩放
        if scale < 1.0:
            new_w = current_w * scale
            new_h = current_h * scale
            
            # 以图像中心为基准重新设置裁切框
            self.rect = QRectF(-new_w / 2, -new_h / 2, new_w, new_h)
            self._update_handles()
        
        # Step 5: 【关键修复】位置约束使用安全区域尺寸
        # 计算裁切框中心点允许的最大偏移范围
        # safe_w 和 safe_h 是安全区域的尺寸
        # 裁切框宽度为 w，则中心点最大偏移为 (safe_w - w) / 2
        final_w = self.rect.width()
        final_h = self.rect.height()
        
        max_offset_x = max(0.0, (safe_w - final_w) / 2.0)
        max_offset_y = max(0.0, (safe_h - final_h) / 2.0)
        
        # 图像中心点
        img_center_x = img_w / 2.0
        img_center_y = img_h / 2.0
        
        # 获取当前位置，并强制 Clamping 到安全范围
        current_pos = self.pos()
        clamped_x = max(img_center_x - max_offset_x, min(img_center_x + max_offset_x, current_pos.x()))
        clamped_y = max(img_center_y - max_offset_y, min(img_center_y + max_offset_y, current_pos.y()))
        
        # 如果位置有偏移，重置位置
        if abs(clamped_x - current_pos.x()) > 0.1 or abs(clamped_y - current_pos.y()) > 0.1:
            self.setPos(clamped_x, clamped_y)
        
        self.update()
    
    def apply_rotation_constraint(self, angle: float) -> None:
        """Apply rotation and auto-scale crop box to fit within safe rect.
        
        Lightroom-style behavior:
        1. Update the logical rotation value
        2. Calculate new safe rectangle
        3. If current crop box exceeds safe rect, scale it down proportionally
        4. Constrain crop center within safe area bounds (key fix)
        
        Args:
            angle: New rotation angle in degrees
        """
        # Step 1: Update rotation value (logical, not visual)
        self.rotation_val = angle
        # IMPORTANT: CropRectItem stays at rotation = 0 for clean coordinate system
        
        # Step 2: Calculate safe rectangle
        safe_w, safe_h = self.update_safe_rect(angle)
        
        # Step 3: Check if current crop exceeds safe rect
        current_w = self.rect.width()
        current_h = self.rect.height()
        
        # Calculate scale factor needed to fit within safe rect
        scale_x = safe_w / current_w if current_w > 0 else 1.0
        scale_y = safe_h / current_h if current_h > 0 else 1.0
        
        # Use the smaller scale factor (more constraining)
        scale = min(scale_x, scale_y)
        
        # If crop box is too large, scale it down
        if scale < 1.0:
            new_w = current_w * scale
            new_h = current_h * scale
            
            # Keep the crop box centered
            self.rect = QRectF(-new_w / 2, -new_h / 2, new_w, new_h)
            self._update_handles()
        
        # Step 4: 【关键修复】位置约束使用安全区域尺寸
        # 计算裁切框中心点允许的最大偏移范围
        if self.canvas.logical_size:
            img_w = self.canvas.logical_size.width()
            img_h = self.canvas.logical_size.height()
            img_center_x = img_w / 2.0
            img_center_y = img_h / 2.0
            
            # 获取缩放后的最终尺寸
            final_w = self.rect.width()
            final_h = self.rect.height()
            
            # 使用安全区域尺寸计算最大偏移量
            max_offset_x = max(0.0, (safe_w - final_w) / 2.0)
            max_offset_y = max(0.0, (safe_h - final_h) / 2.0)
            
            # 获取当前位置，并强制 Clamping 到安全范围
            current_pos = self.pos()
            clamped_x = max(img_center_x - max_offset_x, min(img_center_x + max_offset_x, current_pos.x()))
            clamped_y = max(img_center_y - max_offset_y, min(img_center_y + max_offset_y, current_pos.y()))
            
            # 如果位置有偏移，重置位置
            if abs(clamped_x - current_pos.x()) > 0.1 or abs(clamped_y - current_pos.y()) > 0.1:
                self.setPos(clamped_x, clamped_y)
        
        self.update()

    def boundingRect(self):
        # Must be infinite to match the dimmed background painting to prevent culling
        return QRectF(-100000, -100000, 200000, 200000)

    def shape(self):
        # Only the actual crop box and its immediate outer rotation zone are clickable
        path = QPainterPath()
        # Expand by 400 pixels to cover the 80-400px rotation hover zone
        path.addRect(self.rect.adjusted(-400, -400, 400, 400))
        return path

    def paint(self, painter, option, widget) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # 1. Draw Dimmed background outside crop area
        # Use an "infinite" local rectangle to ensure dimming covers entire viewport
        # regardless of zoom level or rotation angle
        path = QPainterPath()
        path.addRect(QRectF(-100000, -100000, 200000, 200000))
        
        crop_path = QPainterPath()
        crop_path.addRect(self.rect)
        
        # Dimmed area is infinite rect minus crop rect
        dim_path = path - crop_path
        painter.setBrush(QBrush(QColor(0, 0, 0, 180))) # Slightly darker for better focus
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(dim_path)

        # 2. Draw Crop Outline - DaVinci 级别视觉：使用 Cosmetic Pen
        crop_pen = QPen(QColor(255, 255, 255), 2.0)
        crop_pen.setCosmetic(True)
        painter.setPen(crop_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect)

        # 3. Draw Rule of Thirds - Cosmetic Pen
        thirds_pen = QPen(QColor(255, 255, 255, 80), 1.0)
        thirds_pen.setCosmetic(True)
        painter.setPen(thirds_pen)
        w, h = self.rect.width(), self.rect.height()
        x, y = self.rect.left(), self.rect.top()
        painter.drawLine(QPointF(x + w/3, y), QPointF(x + w/3, y + h))
        painter.drawLine(QPointF(x + 2*w/3, y), QPointF(x + 2*w/3, y + h))
        painter.drawLine(QPointF(x, y + h/3), QPointF(x + w, y + h/3))
        painter.drawLine(QPointF(x, y + 2*h/3), QPointF(x + w, y + 2*h/3))

    def _update_handles(self) -> None:
        self._is_updating = True
        r = self.rect
        self.handles['tl'].setPos(r.topLeft())
        self.handles['tr'].setPos(r.topRight())
        self.handles['bl'].setPos(r.bottomLeft())
        self.handles['br'].setPos(r.bottomRight())
        self.handles['t'].setPos(0, r.top())
        self.handles['b'].setPos(0, r.bottom())
        self.handles['l'].setPos(r.left(), 0)
        self.handles['r'].setPos(r.right(), 0)
        
        self._is_updating = False

    def on_handle_moved(self, handle, local_pos) -> None:
        """
        Handle moved with centered rect algorithm.
        self.rect is always kept centered at (0,0) as QRectF(-w/2, -h/2, w, h).
        The item's setPos() determines the actual scene position.
        """
        if self._is_updating:
            return
        
        h = handle.handle_type
        new_rect = QRectF(self.rect)
        
        # Step 1: Apply drag - update the specific edge/corner using local_pos
        if h == 'tl':
            new_rect.setTopLeft(local_pos)
        elif h == 'tr':
            new_rect.setTopRight(local_pos)
        elif h == 'bl':
            new_rect.setBottomLeft(local_pos)
        elif h == 'br':
            new_rect.setBottomRight(local_pos)
        elif h == 't':
            new_rect.setTop(local_pos.y())
        elif h == 'b':
            new_rect.setBottom(local_pos.y())
        elif h == 'l':
            new_rect.setLeft(local_pos.x())
        elif h == 'r':
            new_rect.setRight(local_pos.x())
        
        # Step 2: Apply aspect ratio constraint if set
        if self.aspect_ratio is not None:
            new_rect = self._apply_aspect_ratio_centered(new_rect, h)
        
        # Step 3: Clamp size and normalize
        new_rect = self._clamp_rect(new_rect, 10.0).normalized()
        
        # Step 4: Calculate new center & recenter
        # Find the local center of the new rect
        local_center = new_rect.center()
        # Get the new width and height
        w, h_new = new_rect.width(), new_rect.height()
        # Map local_center to Scene Space BEFORE resetting the rect
        new_scene_pos = self.mapToScene(local_center)
        
        # Step 4.5: Calculate the final centered rect
        centered_rect = QRectF(-w/2, -h_new/2, w, h_new)
        
        # Step 4.6: Boundary constraint check
        if not self._is_valid_crop(new_scene_pos, centered_rect):
            return  # Ignore invalid movement
        
        # Step 5: Apply geometry change
        self.prepareGeometryChange()
        self.setPos(new_scene_pos)
        self.rect = centered_rect
        
        # Step 6: Update handles
        self._update_handles()
        self.update()
    
    def _apply_aspect_ratio_centered(self, rect, handle_type):
        """
        Apply aspect ratio constraint while keeping the rect centered.
        For edge handles: adjust both sides equally from center.
        For corner handles: clamp to the opposite anchor corner.
        """
        new_rect = QRectF(rect)
        aspect = self.aspect_ratio
        
        if aspect <= 0:
            return new_rect
        
        # Edge handles: adjust both sides equally from center
        if handle_type in ['t', 'b']:
            # Height drives width - adjust both left and right from center
            new_h = abs(new_rect.height())
            new_w = new_h * aspect
            center_x = new_rect.center().x()
            new_rect.setLeft(center_x - new_w / 2)
            new_rect.setRight(center_x + new_w / 2)
                
        elif handle_type in ['l', 'r']:
            # Width drives height - adjust both top and bottom from center
            new_w = abs(new_rect.width())
            new_h = new_w / aspect
            center_y = new_rect.center().y()
            new_rect.setTop(center_y - new_h / 2)
            new_rect.setBottom(center_y + new_h / 2)
                
        elif handle_type in ['tl', 'tr', 'bl', 'br']:
            # Corner handles: calculate both potential dimensions
            new_w = abs(new_rect.width())
            new_h = abs(new_rect.height())
            
            # Option 1: Width-driven (width determines height)
            w_driven_h = new_w / aspect
            # Option 2: Height-driven (height determines width)
            h_driven_w = new_h * aspect
            
            # Calculate original dimensions before this mouse movement
            orig_w = abs(self.rect.width())
            orig_h = abs(self.rect.height())
            
            # Calculate relative change for each approach
            w_driven_height_change = abs(w_driven_h - orig_h) / max(orig_h, 1e-6)
            h_driven_width_change = abs(h_driven_w - orig_w) / max(orig_w, 1e-6)
            
            # Choose the approach with smaller relative change
            if w_driven_height_change <= h_driven_width_change:
                final_w = new_w
                final_h = w_driven_h
            else:
                final_w = h_driven_w
                final_h = new_h
            
            # Apply the chosen dimensions while preserving the dragged corner
            # and keeping the opposite corner (anchor) fixed
            if handle_type == 'tl':
                br = new_rect.bottomRight()
                new_rect.setBottomRight(br)
                new_rect.setTopLeft(QPointF(br.x() - final_w, br.y() - final_h))
            elif handle_type == 'tr':
                bl = new_rect.bottomLeft()
                new_rect.setBottomLeft(bl)
                new_rect.setTopRight(QPointF(bl.x() + final_w, bl.y() - final_h))
            elif handle_type == 'bl':
                tr = new_rect.topRight()
                new_rect.setTopRight(tr)
                new_rect.setBottomLeft(QPointF(tr.x() - final_w, tr.y() + final_h))
            elif handle_type == 'br':
                tl = new_rect.topLeft()
                new_rect.setTopLeft(tl)
                new_rect.setBottomRight(QPointF(tl.x() + final_w, tl.y() + final_h))
        
        return new_rect
    
    def _clamp_rect(self, rect, min_size):
        """Clamp rectangle dimensions to minimum size to prevent flipping."""
        new_rect = QRectF(rect)
        
        # Clamp width
        if new_rect.width() < min_size:
            if new_rect.width() > 0:
                new_rect.setRight(new_rect.left() + min_size)
            else:
                new_rect.setLeft(new_rect.right() - min_size)
        
        # Clamp height
        if new_rect.height() < min_size:
            if new_rect.height() > 0:
                new_rect.setBottom(new_rect.top() + min_size)
            else:
                new_rect.setTop(new_rect.bottom() - min_size)
        
        return new_rect

    def hoverMoveEvent(self, event) -> None:
        pos = event.pos()
        self._hover_mode = "none"
        
        # 1. Check if over any handle (highest priority)
        over_handle = False
        for handle in self.handles.values():
            if handle.contains(handle.mapFromParent(pos)):
                over_handle = True
                break
        
        if over_handle:
            # Let the handle's own cursor show
            self.setCursor(Qt.CursorShape.ArrowCursor) # Item cursor is fallback
            self._hover_mode = "handle"
        else:
            # 2. Check for rotation zone (near corners but outside) - extra large area (80-400px)
            corners = [self.rect.topLeft(), self.rect.topRight(), self.rect.bottomLeft(), self.rect.bottomRight()]
            is_near_corner_outside = False
            for c in corners:
                dist = math.hypot(pos.x() - c.x(), pos.y() - c.y())
                # If outside rect but close to corner (80-400px from corner) - extra large area for easy rotation
                if 80 < dist < 400 and not self.rect.contains(pos): 
                    is_near_corner_outside = True
                    break
            
            if is_near_corner_outside:
                # Use SizeAllCursor for rotation - distinct from move cursor
                self.setCursor(Qt.CursorShape.SizeAllCursor)
                self._hover_mode = "rotate"
            elif self.rect.contains(pos):
                # 3. Check for movement zone (inside rect) - use pointing hand
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self._hover_mode = "move"
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
        
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if not hasattr(self, '_hover_mode'): 
            self._hover_mode = "none"
        
        if self._hover_mode == "rotate":
            self._is_rotating = True
            self._start_rot_pos = event.scenePos()
            self._start_rotation = self.rotation_val
            
            # KEY: Lock the aspect ratio at rotation start time
            # This prevents shape deformation during rotation (Lightroom-style behavior)
            # Priority: Fixed aspect ratio > Current crop rect aspect ratio > Image aspect ratio
            if self.aspect_ratio is not None:
                # Fixed aspect ratio mode: use the preset ratio
                self._rotation_locked_aspect_ratio = self.aspect_ratio
            else:
                # Free mode: lock to current crop rect's actual aspect ratio
                # This ensures the shape doesn't change during rotation
                current_w = self.rect.width()
                current_h = self.rect.height()
                if current_w > 0 and current_h > 0:
                    self._rotation_locked_aspect_ratio = current_w / current_h
                else:
                    self._rotation_locked_aspect_ratio = None
            
            if hasattr(self.canvas, 'interaction_started'):
                self.canvas.interaction_started.emit()
            event.accept()
            return
        elif self._hover_mode == "move":
            # ItemIsMovable handles the move, but we want to ensure focus
            super().mousePressEvent(event)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._is_rotating and self._start_rot_pos is not None:
            # === Lightroom-style "Adaptive Inscribed Rectangle" Rotation ===
            # Core principle: CropRectItem stays at rotation = 0 (never rotates visually)
            # The rotation is applied logically, and crop box auto-scales to fit safe rect
            
            # Step 1: Calculate angle using math.atan2 from mouse position relative to crop center
            # self.pos() returns the scene position of the crop box center
            crop_center_scene = self.pos()
            
            # Vector from crop center to start position
            p1 = self._start_rot_pos - crop_center_scene
            # Vector from crop center to current position
            p2 = event.scenePos() - crop_center_scene
            
            # Calculate angles using atan2 (returns radians, convert to degrees)
            angle1 = math.degrees(math.atan2(p1.y(), p1.x()))
            angle2 = math.degrees(math.atan2(p2.y(), p2.x()))
            
            # Delta angle (how much rotation since start)
            delta = angle2 - angle1
            
            # Step 2: Update logical rotation value
            self.rotation_val = self._start_rotation + delta
            
            # Step 3: Clamp rotation to reasonable range (-45° to 45° for crop)
            self.rotation_val = max(-45.0, min(45.0, self.rotation_val))
            
            # Step 4: Apply rotation constraint (auto-scale crop box to fit safe rect)
            # This is the key to Lightroom-style rotation - crop box shrinks as you rotate
            self.apply_rotation_constraint(self.rotation_val)
            
            # Step 5: Notify canvas/panel of rotation change
            # IMPORTANT: CropRectItem does NOT call setRotation() - it stays at rotation = 0
            # The rotation is handled logically, not visually on the crop item
            if hasattr(self.canvas, 'crop_rotation_changed'):
                self.canvas.crop_rotation_changed.emit(self.rotation_val)
            
            # Step 6: Update GPU preview angle for real-time rotation rendering
            # The GPU shader uses this angle to rotate the background texture
            self.canvas._crop_preview_angle = self.rotation_val
            
            # Step 7: Update engine crop params (for final crop application)
            if self.canvas.engine and self.canvas.engine.crop_params:
                self.canvas.engine.crop_params['rotation'] = self.rotation_val
            
            # Step 8: Trigger viewport update for GPU-rendered rotation preview
            self.canvas.viewport().update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._is_rotating:
            self._is_rotating = False
            self._start_rot_pos = None
            if hasattr(self.canvas, 'interaction_ended'):
                self.canvas.interaction_ended.emit()
        super().mouseReleaseEvent(event)

    def _is_valid_crop(self, proposed_scene_pos, proposed_rect):
        """Check if the proposed geometry stays within the image bounds."""
        # 画布扩展功能：如果不需要限制在画内，始终返回 True
        if not self._constrain_to_image:
            return True
        
        # Create a transform for the proposed state
        t = QTransform()
        t.translate(proposed_scene_pos.x(), proposed_scene_pos.y())
        t.rotate(self.rotation_val)
        
        # Get the 4 corners of the proposed_rect as QPointF list
        rect_corners = [
            proposed_rect.topLeft(),
            proposed_rect.topRight(),
            proposed_rect.bottomLeft(),
            proposed_rect.bottomRight()
        ]
        
        # Check if all 4 corners are inside the image bounds.
        # AUD-25201: sceneRect 被 _update_scene_pan_bounds 扩展成图像四周
        # 各 4 倍图像尺寸（或 ≥2048px）的大矩形以允许平移，拿它校验
        # "限制在画内"恒为真。与 _apply_crop_constraints 一致，改用
        # logical_size 的真实图像边界（logical_size 未就绪时退回旧行为）。
        logical = self.canvas.logical_size
        if logical is not None and logical.width() > 0 and logical.height() > 0:
            image_bounds = QRectF(0.0, 0.0, float(logical.width()), float(logical.height()))
        else:
            image_bounds = self.canvas.scene.sceneRect()
        # Task 1 Fix: Use 2px tolerance buffer to prevent floating-point edge locking
        expanded_bounds = image_bounds.adjusted(-2.0, -2.0, 2.0, 2.0)
        
        for corner in rect_corners:
            # Map the corner to scene coordinates
            scene_pt = t.map(corner)
            # Use QRectF.contains with proper overload
            if not expanded_bounds.contains(scene_pt):
                return False
        return True

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            # 'value' is the proposed new scene position
            if not self._is_valid_crop(value, self.rect):
                return self.pos() # Hit the brick wall, block movement
        return super().itemChange(change, value)
    
    def set_crop_constrain(self, constrain: bool) -> None:
        """设置裁切框是否限制在图像范围内。
        
        Args:
            constrain: True 为限制在画内（默认），False 为允许自由拖拽出画外（画布扩展）
        """
        self._constrain_to_image = constrain
        
        # 如果重新启用约束，立即触发一次约束检查，把出画的裁切框拽回来
        if self._constrain_to_image:
            self._apply_crop_constraints()

class MaskShapeItem(QGraphicsItem):
    """Visual representation of a mask shape on the canvas with interactive handles.

    Performance Optimization:
    - Uses QTimer throttling (16ms ≈ 60 FPS) for engine sync during drag
    - UI updates are immediate, engine sync is rate-limited
    - Ensures final sync on mouse release
    """
    # AUD-25207：笔刷覆盖层缓存的分配上限。超出即放弃缓存改为直接
    # 路径绘制，避免在 paint() 内出现巨型同步分配。
    _BRUSH_CACHE_MAX_SIDE = 8192        # 单边像素上限
    _BRUSH_CACHE_MAX_PIXELS = 1 << 24   # 总像素预算 16MP（ARGB32 ≈ 64MB）

    def __init__(self, shape_id, shape_type, canvas, parent=None) -> None:
        super().__init__(parent)
        self.shape_id = shape_id
        self.shape_type = shape_type
        self.canvas = canvas
        
        self.center = QPointF(0, 0)
        self.size = [100.0, 100.0]
        self.rotation = 0.0
        self.feather = 50.0

        self._video_window_mode = False
        self.edge_softness: Optional[List[float]] = None
        self._corner_resize_start_size: Optional[Tuple[float, float]] = None

        self.selected_node_idx = -1
        self._locked = False  # P1-COLOR-09: 窗口锁定（画布禁止编辑）
        
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | 
                      QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setZValue(20)
        
        # Task 4: Hover highlight effect
        self.setAcceptHoverEvents(True)
        self._is_hovered = False
        
        # Performance: Throttle engine sync to 60 FPS during drag
        self._sync_timer = QTimer()
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self._do_sync_back)
        
        # Handles
        self.handles = {}
        self.bezier_points = [] # For curve type
        
        if shape_type in ["circle", "rect", "curve"]:
            self.handles['center'] = MaskHandle('center', self)
            self.handles['rotate'] = MaskHandle('rotate', self)
            if shape_type in ["circle", "rect"]:
                self.handles['resize_w'] = MaskHandle('resize_w', self)          # right
                self.handles['resize_h'] = MaskHandle('resize_h', self)          # top
                self.handles['resize_w_neg'] = MaskHandle('resize_w_neg', self)  # left
                self.handles['resize_h_neg'] = MaskHandle('resize_h_neg', self)  # bottom
                self.handles['feather_r'] = MaskHandle('feather_r', self)   # feather right
                self.handles['feather_l'] = MaskHandle('feather_l', self)   # feather left
                self.handles['feather_t'] = MaskHandle('feather_t', self)   # feather top
                self.handles['feather_b'] = MaskHandle('feather_b', self)   # feather bottom
        elif shape_type == "gradient":
            self.handles['grad_start'] = MaskHandle('grad_start', self)
            self.handles['grad_end'] = MaskHandle('grad_end', self)
        
        self._is_updating_handles = False
        self.set_video_window_mode(getattr(canvas, '_video_window_mode', False) is True)

    def set_video_window_mode(self, enabled: bool) -> None:
        """Apply the video palette's transform cage without changing image tools."""
        self.prepareGeometryChange()
        self._video_window_mode = bool(enabled)
        if enabled and self.shape_type in ('circle', 'rect'):
            for corner in ('tl', 'tr', 'bl', 'br'):
                key = 'resize_' + corner
                if key not in self.handles:
                    self.handles[key] = MaskHandle(key, self)
        if enabled and self.shape_type == 'gradient' and 'center' not in self.handles:
            self.handles['center'] = MaskHandle('center', self)
        for key, handle in self.handles.items():
            handle.set_video_window_style(enabled)
            handle.setEnabled(not self._locked)
            if key in ('resize_tl', 'resize_tr', 'resize_bl', 'resize_br'):
                handle.setVisible(enabled)
            if self.shape_type == 'gradient' and key == 'center':
                handle.setVisible(enabled)
        self._update_handle_positions()
        self.update()

    def _video_half_size(self) -> Tuple[float, float]:
        factor = 1.0 if self.shape_type == 'circle' else 0.5
        return self.size[0] * factor, self.size[1] * factor

    def _video_corner_position(self, key: str) -> QPointF:
        half_w, half_h = self._video_half_size()
        return QPointF(-half_w if key.endswith(('tl', 'bl')) else half_w,
                       -half_h if key.endswith(('tl', 'tr')) else half_h)

    def _video_feather_offsets(self) -> Tuple[float, float, float, float]:
        width = self.canvas.logical_size.width() if getattr(self.canvas, 'logical_size', None) else 1000
        if self.shape_type == 'rect' and self.edge_softness is not None:
            return tuple(float(value) * width / 200.0 for value in self.edge_softness)
        # Half the existing feather kernel spans each side of the hard edge.
        offset = self.feather * width * 0.4 / 100.0
        return offset, offset, offset, offset

    def _video_feather_position(self, key: str) -> QPointF:
        half_w, half_h = self._video_half_size()
        left, right, top, bottom = self._video_feather_offsets()
        return {
            'feather_l': QPointF(-half_w - left, 0.0),
            'feather_r': QPointF(half_w + right, 0.0),
            'feather_t': QPointF(0.0, -half_h - top),
            'feather_b': QPointF(0.0, half_h + bottom),
        }[key]

    def _paint_video_transform_cage(self, painter: QPainter, color: QColor) -> None:
        half_w, half_h = self._video_half_size()
        pen = QPen(color, 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(-half_w, -half_h, half_w * 2.0, half_h * 2.0))
        left, right, top, bottom = self._video_feather_offsets()
        if max(left, right, top, bottom) <= 0.0:
            return
        if self.shape_type == 'circle':
            self._draw_shape_outline(painter, self.size, (left, top), False)
            self._draw_shape_outline(painter, self.size, (-left, -top), False)
        else:
            painter.drawRect(QRectF(-half_w - left, -half_h - top,
                                    half_w * 2.0 + left + right, half_h * 2.0 + top + bottom))
            inner_left, inner_right = min(left, half_w), min(right, half_w)
            inner_top, inner_bottom = min(top, half_h), min(bottom, half_h)
            painter.drawRect(QRectF(-half_w + inner_left, -half_h + inner_top,
                                    half_w * 2.0 - inner_left - inner_right,
                                    half_h * 2.0 - inner_top - inner_bottom))

    def set_locked(self, locked: bool) -> None:
        """P1-COLOR-09: 锁定窗口——画布禁止拖动/改形（面板参数仍可查看）。"""
        locked = bool(locked)
        if locked == getattr(self, '_locked', False):
            self._locked = locked
            return
        self._locked = locked
        # 拖动整体关闭；选择保留（仍可选中查看参数）
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not locked)
        for handle in self.handles.values():
            handle.setEnabled(not locked)

    def _rebuild_curve_handles(self) -> None:
        """重建曲线控制点 handles，用于加载工程后恢复可编辑状态。
        
        此方法会：
        1. 安全移除现有的动态 handles（node_* 和 ctrl_*）
        2. 根据 bezier_points 重新创建所有 node 和 ctrl handles
        3. 更新 handles 的位置
        """
        if self.shape_type != "curve":
            return
        
        # 检查是否真的需要重建对象（数量是否一致），避免销毁正在交互的实例
        current_node_count = sum(1 for k in self.handles.keys() if isinstance(k, str) and k.startswith('node_'))
        if current_node_count == len(self.bezier_points) and current_node_count > 0:
            return
        
        # 1. 安全移除现有的动态 handles
        keys_to_remove = []
        for key, handle in list(self.handles.items()):
            if isinstance(key, str) and (key.startswith('node_') or key.startswith('ctrl_')):
                # 先隐藏
                handle.setVisible(False)
                handle.setEnabled(False)
                keys_to_remove.append(key)
        
        # 从字典移除并从场景移除
        for key in keys_to_remove:
            handle = self.handles.pop(key, None)
            if handle:
                handle.setParentItem(None)
                if handle.scene():
                    handle.scene().removeItem(handle)
        
        # 2. 为每个 bezier_point 创建 handles
        for i, p in enumerate(self.bezier_points):
            # 创建 node handle
            node_key = f"node_{i}"
            node_handle = MaskHandle('node', self, i)
            self.handles[node_key] = node_handle
            
            # 创建 c1 和 c2 ctrl handles
            for sub in ['c1', 'c2']:
                ctrl_key = f"ctrl_{i}_{sub}"
                ctrl_handle = MaskHandle('ctrl', self, i, sub)
                self.handles[ctrl_key] = ctrl_handle
        
        # 3. 更新位置
        self._update_handle_positions()

    def boundingRect(self):
        if self._video_window_mode and self.shape_type in ('circle', 'rect'):
            half_w, half_h = self._video_half_size()
            left, right, top, bottom = self._video_feather_offsets()
            return QRectF(-half_w - left, -half_h - top,
                          half_w * 2.0 + left + right, half_h * 2.0 + top + bottom).adjusted(-2, -2, 2, 2)
        if self.shape_type == "curve":
            if not self.bezier_points: return QRectF()
            path = self._get_path()
            return path.boundingRect().adjusted(-20, -20, 20, 20)
        elif self.shape_type == "gradient":
            return QRectF(-500, -500, 1000, 1000)
        elif self.shape_type == "brush":
            return QRectF(-100000, -100000, 200000, 200000)
            
        w, h = self.size[0], self.size[1]
        # Expand slightly for line thickness
        return QRectF(-w, -h, 2*w, 2*h).adjusted(-5, -5, 5, 5)

    def shape(self):
        """Precise hit testing for overlapping shapes."""
        path = QPainterPath()
        if self.shape_type == "circle":
            path.addEllipse(QPointF(0, 0), self.size[0], self.size[1])
        elif self.shape_type == "rect":
            w, h = self.size[0], self.size[1]
            path.addRect(QRectF(-w/2, -h/2, w, h))
        elif self.shape_type == "curve":
            path = self._get_path()
            # Requirement: Clicking INSIDE the closed path should also select it
            # We create a result path that is the union of the fill and a thick stroke
            fill_path = QPainterPath(path)
            fill_path.closeSubpath()
            
            stroker = QPainterPathStroker()
            stroker.setWidth(10) 
            stroke_path = stroker.createStroke(path)
            
            return (fill_path + stroke_path).simplified()
        elif self.shape_type == "gradient":
            # Gradient selection is mainly via handles, but allow clicking the line
            if 'grad_start' in self.handles and 'grad_end' in self.handles:
                p1 = self.handles['grad_start'].pos()
                p2 = self.handles['grad_end'].pos()
                path.moveTo(p1)
                path.lineTo(p2)
                stroker = QPainterPathStroker()
                stroker.setWidth(20)
                path = stroker.createStroke(path)
        return path

    def _get_path(self):
        path = QPainterPath()
        if not self.bezier_points: return path

        pts = self.bezier_points
        path.moveTo(pts[0]['pos'])
        for i in range(len(pts)):
            p1 = pts[i]
            p2 = pts[(i + 1) % len(pts)]
            path.cubicTo(p1['c2'], p2['c1'], p2['pos'])
        path.closeSubpath()  # 确保首尾相连，形成闭合图形
        return path

    def _get_offset_curve_path(self, offset):
        """计算沿法线方向偏移的闭合曲线路径（用于绘制内外羽化虚线）。

        算法：对每个 bezier 控制点计算法线方向，沿法线偏移 offset 像素。
        """
        pts = self.bezier_points
        if not pts or len(pts) < 2:
            return QPainterPath()

        # 计算每个节点的法线方向
        normals = []
        for i in range(len(pts)):
            p = pts[i]
            prev = pts[(i - 1) % len(pts)]
            # 入方向和出方向的切线
            t_in = p['pos'] - prev['c2']
            t_out = p['c2'] - p['pos']
            tangent = (t_in + t_out) / 2  # 平均切线

            # 法线 = 切线旋转 90°（逆时针）
            normal = QPointF(-tangent.y(), tangent.x())
            length = math.sqrt(normal.x()**2 + normal.y()**2)
            if length > 0.001:
                normal = QPointF(normal.x() / length, normal.y() / length)
            normals.append(normal)

        # 构建偏移后的 bezier 路径
        offset_pts = []
        for i in range(len(pts)):
            p = pts[i]
            n = normals[i]
            offset_pts.append({
                'pos': QPointF(p['pos'].x() + n.x() * offset, p['pos'].y() + n.y() * offset),
                'c1': QPointF(p['c1'].x() + n.x() * offset, p['c1'].y() + n.y() * offset),
                'c2': QPointF(p['c2'].x() + n.x() * offset, p['c2'].y() + n.y() * offset),
            })

        path = QPainterPath()
        path.moveTo(offset_pts[0]['pos'])
        for i in range(len(offset_pts)):
            p1 = offset_pts[i]
            p2 = offset_pts[(i + 1) % len(offset_pts)]
            path.cubicTo(p1['c2'], p2['c1'], p2['pos'])
        path.closeSubpath()
        return path

    def paint(self, painter, option, widget) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Requirement: Persistent Orange highlight if active (from Windows list)
        is_active = (self.shape_id == self.canvas.active_shape_id)
        
        if is_active:
            # Colors like DaVinci: Orange for selected
            base_color = QColor(255, 136, 0) 
            feather_color = QColor(255, 136, 0, 180)
        elif self._is_hovered:
            # Task 4: Hover highlight - warm orange between unselected and selected
            base_color = QColor(255, 180, 100, 200)
            feather_color = QColor(255, 180, 100, 120)
        else:
            # Darker gray for unselected to create better contrast
            base_color = QColor(100, 100, 100, 180)
            feather_color = QColor(100, 100, 100, 100)
            
        # DaVinci 级别视觉：所有轮廓线使用 Cosmetic Pen，无论缩放级别保持恒定物理像素宽度
        
        # 1. Draw Feather Outlines (Inner + Outer, Dashed)
        if self._video_window_mode and self.shape_type in ('circle', 'rect'):
            self._paint_video_transform_cage(painter, base_color)
        elif self.shape_type in ["circle", "rect"] and self.feather > 0:
            # feather=100 → offset=shape radius, separate for X/Y to match ellipse
            if self.shape_type == "circle":
                fx = self.feather / 100.0 * self.size[0]
                fy = self.feather / 100.0 * self.size[1]
            else:
                fx = self.feather / 100.0 * (self.size[0] / 2)
                fy = self.feather / 100.0 * (self.size[1] / 2)
            # Outer feather outline (dashed, outside shape)
            outer_pen = QPen(feather_color, 1.0, Qt.PenStyle.DashLine)
            outer_pen.setCosmetic(True)
            outer_pen.setDashPattern([4, 4])
            painter.setPen(outer_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            self._draw_shape_outline(painter, self.size, (fx, fy), False)
            # Inner feather outline (dashed, inside shape)
            inner_pen = QPen(feather_color, 1.0, Qt.PenStyle.DashLine)
            inner_pen.setCosmetic(True)
            inner_pen.setDashPattern([4, 4])
            painter.setPen(inner_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            self._draw_shape_outline(painter, self.size, (-fx, -fy), False)
        elif self.shape_type == "curve" and self.feather > 0:
            # Curve: feather=100 时偏移量 = 曲线控制点到中心的平均距离（等效半径）
            avg_radius = 0
            if self.bezier_points:
                for p in self.bezier_points:
                    pos = p['pos']
                    avg_radius += math.sqrt(pos.x()**2 + pos.y()**2)
                avg_radius /= len(self.bezier_points)
            avg_radius = max(avg_radius, 10)  # 下限保护
            feather_px = self.feather / 100.0 * avg_radius

            if abs(feather_px) > 0.5:
                # Outer feather
                outer_pen = QPen(feather_color, 1.0, Qt.PenStyle.DashLine)
                outer_pen.setCosmetic(True)
                outer_pen.setDashPattern([4, 4])
                painter.setPen(outer_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(self._get_offset_curve_path(feather_px))

                # Inner feather
                inner_pen = QPen(feather_color, 1.0, Qt.PenStyle.DashLine)
                inner_pen.setCosmetic(True)
                inner_pen.setDashPattern([4, 4])
                painter.setPen(inner_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(self._get_offset_curve_path(-feather_px))

        # 3. Draw Main Outline (Solid for DaVinci feel)
        main_pen = QPen(base_color, 1.0 if self._video_window_mode else 2.0)
        main_pen.setCosmetic(True)
        painter.setPen(main_pen)
        # No fill inside the mask as requested (fully transparent)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        self._draw_shape_outline(painter, self.size, 0, True)
        
        # 4. Draw rotation handle connector (from shape top edge to rotate handle)
        if self.shape_type in ["circle", "rect", "curve"] and 'rotate' in self.handles:
            conn_pen = QPen(base_color, 1.0,
                           Qt.PenStyle.SolidLine if self._video_window_mode else Qt.PenStyle.DotLine)
            conn_pen.setCosmetic(True)
            painter.setPen(conn_pen)
            r_pos = self.handles['rotate'].pos()
            painter.drawLine(QPointF(0, 0), r_pos)
            
        # 5. Draw center crosshair if active
        if is_active and not self._video_window_mode and self.shape_type in ["circle", "rect", "curve"]:
            cross_pen = QPen(base_color, 1.0)
            cross_pen.setCosmetic(True)
            painter.setPen(cross_pen)
            p_len = 8
            painter.drawLine(QPointF(-p_len, 0), QPointF(p_len, 0))
            painter.drawLine(QPointF(0, -p_len), QPointF(0, p_len))

        # 6. Special drawing for curve/gradient
        if self.shape_type == "curve":
            # 只要激活且不在笔刷/SAM模式下，就绘制控制线
            is_this_active = (self.shape_id == getattr(self.canvas, 'active_shape_id', None))
            is_mutually_exclusive = getattr(self.canvas, 'brush_mode_active', False) or getattr(self.canvas, 'sam_mode_active', False)
            
            if is_this_active and not is_mutually_exclusive:
                # Draw control lines for selected nodes
                # Fix: 使用 selected_node_idx 而非 isSelected() 来判断选中状态
                # 因为 selected_node_idx 是实际控制选中状态的变量
                if self.selected_node_idx >= 0 and self.selected_node_idx < len(self.bezier_points):
                    i = self.selected_node_idx
                    p = self.bezier_points[i]
                    ctrl_pen = QPen(QColor(255, 255, 255, 180), 1.0, Qt.PenStyle.SolidLine)
                    ctrl_pen.setCosmetic(True)
                    painter.setPen(ctrl_pen)
                    painter.drawLine(p['pos'], p['c1'])
                    painter.drawLine(p['pos'], p['c2'])

                    # Draw feather lines for selected node (cyan dashed)
                    fi_key = f'fi_{i}'
                    fo_key = f'fo_{i}'
                    if fi_key in self.handles and self.handles[fi_key].isVisible():
                        feather_pen = QPen(base_color if self._video_window_mode else QColor(0, 255, 255, 180),
                                           1.0, Qt.PenStyle.SolidLine if self._video_window_mode else Qt.PenStyle.DashLine)
                        feather_pen.setCosmetic(True)
                        painter.setPen(feather_pen)
                        painter.drawLine(p['pos'], self.handles[fi_key].pos())
                    if fo_key in self.handles and self.handles[fo_key].isVisible():
                        if fi_key not in self.handles or not self.handles[fi_key].isVisible():
                            feather_pen = QPen(base_color if self._video_window_mode else QColor(0, 255, 255, 180),
                                               1.0, Qt.PenStyle.SolidLine if self._video_window_mode else Qt.PenStyle.DashLine)
                            feather_pen.setCosmetic(True)
                            painter.setPen(feather_pen)
                        painter.drawLine(p['pos'], self.handles[fo_key].pos())
        elif self.shape_type == "gradient":
            grad_pen = QPen(base_color, 1.0 if self._video_window_mode else 2.0)
            grad_pen.setCosmetic(True)
            painter.setPen(grad_pen)
            p1 = self.handles['grad_start'].pos()
            p2 = self.handles['grad_end'].pos()
            painter.drawLine(p1, p2)
            
            # Draw perpendicular boundary lines at start and end
            line = QLineF(p1, p2)
            normal = line.normalVector().unitVector()
            # Normal vector is rotated 90 degrees, we use it to draw the boundary
            bound_pen = QPen(base_color, 1.0,
                            Qt.PenStyle.SolidLine if self._video_window_mode else Qt.PenStyle.DashLine)
            bound_pen.setCosmetic(True)
            for pt in [p1, p2]:
                n_vec = QPointF(normal.dx() * 500, normal.dy() * 500)
                # Drawing a very long line to represent the gradient boundary
                painter.setPen(bound_pen)
                painter.drawLine(pt - n_vec, pt + n_vec)
        
        # 渲染已保存的笔刷覆盖层 - 使用局部包围盒缓存优化
        if self.shape_type == "brush":
            is_active = (self.shape_id == getattr(self.canvas, 'active_shape_id', None))
            show_overlay = self.canvas._brush_params.get("show_overlay", True)
            
            # 跨工具互斥逻辑：如果正在使用其他工具，强制隐藏笔刷预览
            if getattr(self.canvas, 'sam_mode_active', False):
                is_active = False
            
            if is_active and show_overlay and hasattr(self, 'brush_strokes') and self.brush_strokes:
                
                w = self.canvas.logical_size.width() if getattr(self.canvas, 'logical_size', None) else 1000
                h = self.canvas.logical_size.height() if getattr(self.canvas, 'logical_size', None) else 1000
                
                # 【局部包围盒缓存优化】：检查是否需要重新计算包围盒和重绘
                need_rebuild = (
                    not hasattr(self, '_brush_bbox') or
                    self._brush_bbox is None or
                    not hasattr(self, '_rendered_strokes_count') or
                    len(self.brush_strokes) < self._rendered_strokes_count
                )
                
                if need_rebuild:
                    # 计算所有笔触的局部包围盒
                    self._brush_bbox = self._calculate_strokes_bounding_box(w, h)
                    self._rendered_strokes_count = 0
                    
                    # 清理旧缓存
                    self._brush_cache_image = None
                    self._brush_cache_pixmap = None
                
                if self._brush_bbox is not None:
                    # 获取局部包围盒参数
                    bbox_x, bbox_y, bbox_w, bbox_h, max_brush_size = self._brush_bbox
                    safety_margin = max_brush_size + 20  # 安全余量

                    # 计算局部图像的实际区域
                    # 注意：不再限制 local_x/local_y 为非负数，允许它们保持负值
                    # 这样在画布外延绘制时坐标映射才能正确
                    local_x = int(bbox_x - safety_margin)
                    local_y = int(bbox_y - safety_margin)
                    local_w = int(bbox_w + 2 * safety_margin)
                    local_h = int(bbox_h + 2 * safety_margin)

                    # AUD-25207：不再允许 16384×16384（ARGB32 约 1GB）的
                    # 同步分配。超出像素预算/边长上限时放弃缓存，回退为
                    # 直接路径绘制（预览仍可见，只是无缓存加速）。
                    cache_too_large = (
                        local_w <= 0 or local_h <= 0
                        or local_w > self._BRUSH_CACHE_MAX_SIDE
                        or local_h > self._BRUSH_CACHE_MAX_SIDE
                        or local_w * local_h > self._BRUSH_CACHE_MAX_PIXELS
                    )

                    if cache_too_large:
                        self._paint_brush_strokes_direct(painter, w, h)
                    else:
                        self._paint_brush_strokes_cached(painter, w, h, local_x, local_y, local_w, local_h)

    def _paint_brush_strokes_cached(self, painter: QPainter, w: float, h: float,
                                    local_x: int, local_y: int, local_w: int, local_h: int) -> None:
        """把笔触栅格化到局部 QImage 缓存并以 0.4 透明度合成。

        支持增量绘制：缓存命中且仅追加新笔触时只绘制新增部分。
        """
        # 检查缓存是否需要更新
        cache_valid = (
            hasattr(self, '_brush_cache_image') and
            self._brush_cache_image is not None and
            self._brush_cache_image.width() == local_w and
            self._brush_cache_image.height() == local_h
        )

        if not cache_valid or len(self.brush_strokes) > self._rendered_strokes_count:
            # 创建局部尺寸的缓存图像
            cache_image = QImage(local_w, local_h, QImage.Format.Format_ARGB32_Premultiplied)
            if cache_image.isNull():
                # 分配失败（内存不足）：回退直接绘制，避免预览静默消失
                self._brush_cache_image = None
                self._brush_cache_pixmap = None
                self._paint_brush_strokes_direct(painter, w, h)
                return
            self._brush_cache_image = cache_image
            self._brush_cache_image.fill(Qt.GlobalColor.transparent)

            # 获取需要绘制的笔触（增量：仅绘制尚未栅格化的部分）
            strokes_to_draw = self.brush_strokes[self._rendered_strokes_count:]

            if strokes_to_draw:
                buf_painter = QPainter(self._brush_cache_image)
                buf_painter.setRenderHint(QPainter.RenderHint.Antialiasing)

                for stroke in strokes_to_draw:
                    pts = stroke.get('points', [])
                    if not pts: continue

                    size = stroke.get('size', 50)  # 半径
                    is_eraser = stroke.get('is_eraser', False)

                    # 使用曲线平滑算法生成路径
                    path = self._create_smooth_brush_path(pts, w, h, local_x, local_y)

                    pen = QPen()
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

                    if is_eraser:
                        buf_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
                        pen.setColor(QColor(0, 0, 0, 255))
                    else:
                        buf_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                        pen.setColor(QColor(255, 0, 0, 255))

                    pen.setWidthF(size * 2)  # Qt 绘制需要直径
                    buf_painter.strokePath(path, pen)

                buf_painter.end()

            self._rendered_strokes_count = len(self.brush_strokes)
            self._brush_cache_pixmap = QPixmap.fromImage(self._brush_cache_image)

        if hasattr(self, '_brush_cache_pixmap') and self._brush_cache_pixmap is not None:
            painter.save()
            painter.setOpacity(0.4)
            # 将局部缓存图绘制到正确的位置（场景坐标）
            # 局部图的左上角在场景中的位置是 (local_x, local_y)
            # 但 MaskShapeItem 的位置是 self.pos()，所以需要减去偏移
            scene_offset_x = local_x - self.pos().x()
            scene_offset_y = local_y - self.pos().y()
            painter.drawPixmap(QPointF(scene_offset_x, scene_offset_y), self._brush_cache_pixmap)
            painter.restore()

    def _paint_brush_strokes_direct(self, painter: QPainter, w: float, h: float) -> None:
        """超预算回退：直接在画布 painter 上绘制笔触预览路径（无缓存）。

        注意：直接绘制不能使用 DestinationOut（会在视图 backing store 上
        打洞擦掉图像本身），橡皮擦笔触改用蓝色区分。
        """
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(0.4)
        # 场景坐标 → 本 item 坐标
        offset_x = self.pos().x()
        offset_y = self.pos().y()
        for stroke in self.brush_strokes:
            pts = stroke.get('points', [])
            if not pts:
                continue
            size = stroke.get('size', 50)
            is_eraser = stroke.get('is_eraser', False)
            path = self._create_smooth_brush_path(pts, w, h, offset_x, offset_y)
            pen = QPen()
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setColor(QColor(0, 120, 255, 255) if is_eraser else QColor(255, 0, 0, 255))
            pen.setWidthF(size * 2)
            painter.strokePath(path, pen)
        painter.restore()

    def _calculate_strokes_bounding_box(self, w: float, h: float, strokes: Optional[list] = None):
        """计算给定笔触列表的最小外接矩形。

        Args:
            w, h: 画布尺寸（用于归一化坐标 → 场景坐标换算）
            strokes: 待计算的笔触列表；None 表示使用 self.brush_strokes

        Returns:
            tuple: (min_x, min_y, width, height, max_brush_size) 或 None
        """
        source = self.brush_strokes if strokes is None else strokes
        if not source:
            return None

        min_x = float('inf')
        min_y = float('inf')
        max_x = float('-inf')
        max_y = float('-inf')
        max_brush_size = 0

        for stroke in source:
            pts = stroke.get('points', [])
            size = stroke.get('size', 50)
            max_brush_size = max(max_brush_size, size)

            for nx, ny in pts:
                scene_x = nx * w
                scene_y = ny * h
                min_x = min(min_x, scene_x)
                min_y = min(min_y, scene_y)
                max_x = max(max_x, scene_x)
                max_y = max(max_y, scene_y)

        if min_x == float('inf'):
            return None

        return (min_x, min_y, max_x - min_x, max_y - min_y, max_brush_size)

    def _assign_brush_strokes(self, brush_strokes: list) -> None:
        """接收后端笔触数据，尽量保留覆盖层缓存（AUD-25206）。

        之前每次 update_from_data 都全量作废缓存并在 paint() 内整幅重建，
        增量绘制分支成为死码。现在：
        - 新数据是旧数据的"追加扩展"（前缀逐项同一对象，实时绘制路径
          color_controller.on_brush_stroke_added 正是原地 append）且新增
          笔触未超出当前缓存包围盒覆盖范围时，保留缓存，paint() 走
          增量分支只绘制新增笔触；
        - 其余情况（撤销/重做/编辑/清除/列表重建）仍全量作废缓存，
          确保显示正确性优先。
        """
        old_strokes = getattr(self, 'brush_strokes', None)
        old_list = old_strokes if isinstance(old_strokes, list) else []
        new_list = brush_strokes if isinstance(brush_strokes, list) else []
        old_len = len(old_list)

        self.brush_strokes = brush_strokes

        prefix_intact = old_len <= len(new_list) and all(
            new_list[i] is old_list[i] for i in range(old_len)
        )
        if not prefix_intact or old_len == len(new_list):
            # 非追加变化或内容未变之外的非追加情形：只有非追加变化才需要重建
            if not prefix_intact:
                self._invalidate_brush_cache()
            return

        # 追加了新笔触：增量扩展包围盒；只有新笔触超出当前缓存覆盖
        # 范围（或更大的笔刷尺寸）时才作废缓存
        w = self.canvas.logical_size.width() if getattr(self.canvas, 'logical_size', None) else 1000
        h = self.canvas.logical_size.height() if getattr(self.canvas, 'logical_size', None) else 1000
        appended_bbox = self._calculate_strokes_bounding_box(w, h, new_list[old_len:])
        old_bbox = getattr(self, '_brush_bbox', None)
        if appended_bbox is None:
            return  # 新增笔触无有效点，缓存无需变化
        if old_bbox is None:
            self._invalidate_brush_cache()
            return

        nx, ny, nw, nh, nsize = appended_bbox
        ox, oy, ow, oh, osize = old_bbox
        grows_cache = (
            nx < ox or ny < oy
            or nx + nw > ox + ow or ny + nh > oy + oh
            or nsize > osize
        )
        # 无论是否增长都更新 bbox 记录（保持覆盖全部笔触的不变量）
        self._brush_bbox = (
            min(ox, nx),
            min(oy, ny),
            max(ox + ow, nx + nw) - min(ox, nx),
            max(oy + oh, ny + nh) - min(oy, ny),
            max(osize, nsize),
        )
        if grows_cache:
            # 缓存图像几何不再匹配，必须整幅重建（paint 全量分支）
            self._invalidate_brush_cache(keep_bbox=True)

    def _invalidate_brush_cache(self, keep_bbox: bool = False) -> None:
        """作废笔刷覆盖层缓存；keep_bbox=True 时保留已扩展的包围盒记录。"""
        if not keep_bbox:
            self._brush_bbox = None
        self._brush_cache_image = None
        self._brush_cache_pixmap = None
        self._rendered_strokes_count = 0

    def _create_smooth_brush_path(self, pts: list, w: float, h: float, offset_x: float, offset_y: float) -> QPainterPath:
        """使用中点贝塞尔平滑创建笔刷路径。
        
        Args:
            pts: 归一化坐标点列表 [(nx, ny), ...]
            w, h: 画布尺寸
            offset_x, offset_y: 局部缓存偏移量
            
        Returns:
            QPainterPath: 平滑的绘制路径
        """
        path = QPainterPath()
        
        if not pts:
            return path
        
        # 转换为场景坐标并应用偏移
        scene_pts = []
        for nx, ny in pts:
            scene_x = nx * w - offset_x
            scene_y = ny * h - offset_y
            scene_pts.append((scene_x, scene_y))
        
        if len(scene_pts) == 1:
            # 单点 - 绘制最小点
            path.moveTo(scene_pts[0][0], scene_pts[0][1])
            path.lineTo(scene_pts[0][0] + 0.01, scene_pts[0][1])
        elif len(scene_pts) == 2:
            # 两点 - 直接连线
            path.moveTo(scene_pts[0][0], scene_pts[0][1])
            path.lineTo(scene_pts[1][0], scene_pts[1][1])
        else:
            # 三点或更多 - 应用中点贝塞尔平滑
            path.moveTo(scene_pts[0][0], scene_pts[0][1])
            
            for i in range(len(scene_pts) - 1):
                p0 = scene_pts[i]
                p1 = scene_pts[i + 1]
                
                # 计算中点
                mid_x = (p0[0] + p1[0]) / 2.0
                mid_y = (p0[1] + p1[1]) / 2.0
                
                # 使用二次贝塞尔：控制点是 p0，终点是中点
                path.quadTo(p0[0], p0[1], mid_x, mid_y)
            
            # 最后一段连接到终点
            last = scene_pts[-1]
            path.lineTo(last[0], last[1])
        
        return path

    def _draw_shape_outline(self, painter, size, offset, fill) -> None:
        if isinstance(offset, (list, tuple)):
            ox, oy = offset
        else:
            ox = oy = offset
        if self.shape_type == "circle":
            rx = max(0.1, size[0] + ox)
            ry = max(0.1, size[1] + oy)
            painter.drawEllipse(QPointF(0, 0), rx, ry)
        elif self.shape_type == "rect":
            w = max(0.1, size[0] + ox * 2)
            h = max(0.1, size[1] + oy * 2)
            painter.drawRect(QRectF(-w/2, -h/2, w, h))
        elif self.shape_type == "curve":
            path = self._get_path()
            painter.drawPath(path)

    def update_from_data(self, center, size, rotation, feather, points=None, brush_strokes=None, grad_points=None,
                         edge_softness: Optional[List[float]] = None) -> None:
        # 关键修复：如果当前有手柄正在被拖拽，绝对不能用后端的旧数据覆盖当前UI状态
        # 否则会导致鼠标拖拽事件丢失和控制点坐标强制回弹
        is_dragging = False
        for h in self.handles.values():
            if isinstance(h, MaskHandle) and getattr(h, '_is_dragging', False):
                is_dragging = True
                break
        if is_dragging:
            return

        self.prepareGeometryChange()
        self._is_updating_handles = True
        self.edge_softness = list(edge_softness) if edge_softness is not None else None
        self.setPos(center)
        self.size = size
        self.rotation = rotation
        # Now supporting rotation for all types including curves
        self.setRotation(rotation)
        # 如果当前是笔刷，强制忽略外部传来的 feather
        if self.shape_type == "brush":
            self.feather = 0.0
        else:
            self.feather = feather

        if brush_strokes is not None:
            self._assign_brush_strokes(brush_strokes)
        
        if self.shape_type == "curve" and points is not None:
            # 获取画布宽高，默认 1000
            w = self.canvas.logical_size.width() if getattr(self.canvas, 'logical_size', None) else 1000
            h = self.canvas.logical_size.height() if getattr(self.canvas, 'logical_size', None) else 1000
            
            # 解析传入的归一化 points 字典，还原为 QPointF 物理坐标
            self.bezier_points = []
            for p in points:
                self.bezier_points.append({
                    **p,
                    'pos': QPointF(p['pos'][0] * w - center.x(), p['pos'][1] * h - center.y()),
                    'c1': QPointF(p['c1'][0] * w - center.x(), p['c1'][1] * h - center.y()),
                    'c2': QPointF(p['c2'][0] * w - center.x(), p['c2'][1] * h - center.y()),
                    'type': p.get('type', 'smooth')
                })
            
            # 【关键】重建曲线控制点 handles，确保加载的数据能生成 UI 控制点
            self._rebuild_curve_handles()
        
        if self.shape_type == "gradient" and grad_points is not None:
            self.grad_points_data = grad_points
        
        self._update_handle_positions()
        self._is_updating_handles = False
        self.update()

    def _schedule_sync(self) -> None:
        """Schedule a throttled engine sync (max 60 FPS during drag).
        
        UI updates are immediate, but engine sync is rate-limited to prevent
        main thread blocking while maintaining smooth visual feedback.
        """
        if not self._sync_timer.isActive():
            self._sync_timer.start(16)  # 16ms ≈ 60 FPS
    
    def _do_sync_back(self) -> None:
        """Execute the actual engine sync (called by timer timeout)."""
        self._sync_back()
    
    def _update_handle_positions(self) -> None:
        self._is_updating_handles = True
        try:
            if self.shape_type in ["circle", "rect"]:
                if 'center' in self.handles:
                    self.handles['center'].setPos(0, 0)
                # Right side resize
                if 'resize_w' in self.handles:
                    self.handles['resize_w'].setPos(self.size[0]/2 if self.shape_type == "rect" else self.size[0], 0)
                # Left side resize
                if 'resize_w_neg' in self.handles:
                    self.handles['resize_w_neg'].setPos(-(self.size[0]/2 if self.shape_type == "rect" else self.size[0]), 0)
                # Top side resize
                if 'resize_h' in self.handles:
                    self.handles['resize_h'].setPos(0, -(self.size[1]/2 if self.shape_type == "rect" else self.size[1]))
                # Bottom side resize
                if 'resize_h_neg' in self.handles:
                    self.handles['resize_h_neg'].setPos(0, self.size[1]/2 if self.shape_type == "rect" else self.size[1])
                if 'rotate' in self.handles:
                    half_w, half_h = self._video_half_size()
                    self.handles['rotate'].setPos(0, -half_h * 0.5 if self._video_window_mode else -100)
                if 'feather_r' in self.handles:
                    if self.shape_type == "circle":
                        fx = self.feather / 100.0 * self.size[0]
                        fy = self.feather / 100.0 * self.size[1]
                        self.handles['feather_r'].setPos(self.size[0] + fx, 0)
                        self.handles['feather_l'].setPos(-(self.size[0] + fx), 0)
                        self.handles['feather_t'].setPos(0, -(self.size[1] + fy))
                        self.handles['feather_b'].setPos(0, self.size[1] + fy)
                    else:
                        fx = self.feather / 100.0 * (self.size[0] / 2)
                        fy = self.feather / 100.0 * (self.size[1] / 2)
                        self.handles['feather_r'].setPos(self.size[0]/2 + fx, 0)
                        self.handles['feather_l'].setPos(-(self.size[0]/2 + fx), 0)
                        self.handles['feather_t'].setPos(0, -(self.size[1]/2 + fy))
                        self.handles['feather_b'].setPos(0, self.size[1]/2 + fy)
                if self._video_window_mode:
                    for key in ('resize_tl', 'resize_tr', 'resize_bl', 'resize_br'):
                        self.handles[key].setPos(self._video_corner_position(key))
                    for key in ('feather_l', 'feather_r', 'feather_t', 'feather_b'):
                        self.handles[key].setPos(self._video_feather_position(key))
            elif self.shape_type == "curve":
                if 'center' in self.handles:
                    self.handles['center'].setPos(0, 0)
                if 'rotate' in self.handles:
                    path = self._get_path()
                    br = path.boundingRect()
                    top = br.top() if not br.isEmpty() else -100
                    self.handles['rotate'].setPos(0, top * 0.5 if self._video_window_mode else min(top - 40, -60))
                
                # 只有处于曲线绘制模式且当前形状为活跃状态时，才显示 node 和 ctrl handles
                is_this_active = (self.shape_id == getattr(self.canvas, 'active_shape_id', None))
                
                # 跨工具互斥逻辑：如果正在使用笔刷或智能蒙版，强制隐藏所有矢量控制点
                if getattr(self.canvas, 'brush_mode_active', False) or getattr(self.canvas, 'sam_mode_active', False):
                    is_this_active = False
                
                should_show_handles = is_this_active
                
                required_dynamic_keys = set()
                for i, p in enumerate(self.bezier_points):
                    # Node handle
                    node_key = f"node_{i}"
                    required_dynamic_keys.add(node_key)
                    if node_key not in self.handles:
                        self.handles[node_key] = MaskHandle('node', self, i)
                    else:
                        self.handles[node_key].index = i # Update index dynamically
                        
                    node_handle = self.handles[node_key]
                    node_handle.setPos(p['pos'])
                    node_handle.setBrush(QBrush(QColor(255, 136, 0) if i == self.selected_node_idx else QColor(255, 255, 255)))
                    # 控制可见性：只有活跃曲线才显示 handles
                    node_handle.setVisible(should_show_handles)
                    
                    # Control handles only for selected node
                    if i == self.selected_node_idx:
                        for sub in ['c1', 'c2']:
                            ctrl_key = f"ctrl_{i}_{sub}"
                            required_dynamic_keys.add(ctrl_key)
                            if ctrl_key not in self.handles:
                                self.handles[ctrl_key] = MaskHandle('ctrl', self, i, sub)
                            else:
                                self.handles[ctrl_key].index = i
                            self.handles[ctrl_key].setPos(p[sub])
                            # 控制可见性：只有活跃曲线才显示 handles
                            self.handles[ctrl_key].setVisible(should_show_handles)

                        # Feather handles for selected curve point
                        # Compute normal direction at this point
                        c2_pos = p.get('c2', p['pos'])
                        tangent = c2_pos - p['pos']
                        tlen = math.sqrt(tangent.x()**2 + tangent.y()**2)
                        if tlen > 1e-6:
                            normal = QPointF(-tangent.y() / tlen, tangent.x() / tlen)
                        else:
                            normal = QPointF(0, -1)

                        # Feather scale: same as image_engine
                        # AUD-25200: Canvas 从未定义 scene_width，旧 hasattr
                        # 恒回退 512（feather_scale 恒 4.096 px/单位）。引擎
                        # 侧公式以渲染目标宽度（image_engine.py
                        # ``4.0 * (target_w / 5.0) / 100.0``）为基准，此处
                        # 与本文件其他 MaskShapeItem 代码一致改读 logical_size。
                        canvas_w = self.canvas.logical_size.width() if getattr(self.canvas, 'logical_size', None) else 1000
                        feather_scale = 4.0 * (canvas_w / 5.0) / 100.0

                        fi_val = p.get('fi') if p.get('fi') is not None else self.feather / 2.0
                        fo_val = p.get('fo') if p.get('fo') is not None else self.feather / 2.0

                        for fkey, fval, direction in [
                            (f'fi_{i}', fi_val, 1),   # inward
                            (f'fo_{i}', fo_val, -1),  # outward
                        ]:
                            required_dynamic_keys.add(fkey)
                            if fkey not in self.handles:
                                self.handles[fkey] = MaskHandle('feather_curve', self, i, fkey[:2])
                            fpos = p['pos'] + normal * direction * fval * feather_scale
                            self.handles[fkey].setPos(fpos)
                            self.handles[fkey].setVisible(should_show_handles)
                
                # Cleanup old dynamic handles safely
                # SAFETY: 先隐藏 handles，然后延迟清理，避免在事件处理过程中销毁对象导致崩溃
                keys_to_remove = []
                for key, handle in list(self.handles.items()):
                    if isinstance(key, str) and (key.startswith('node_') or key.startswith('ctrl_') or key.startswith('fi_') or key.startswith('fo_')):
                        if key not in required_dynamic_keys:
                            # 先隐藏，防止在清理过程中仍然可见和交互
                            handle.setVisible(False)
                            handle.setEnabled(False)
                            keys_to_remove.append(key)
                
                # 从字典中移除引用
                for key in keys_to_remove:
                    handle = self.handles.pop(key, None)
                    if handle:
                        # 先断开父子关系
                        handle.setParentItem(None)
                        # 然后从场景移除（如果还在场景中）
                        if handle.scene():
                            self.scene().removeItem(handle)
            elif self.shape_type == "gradient":
                if 'grad_start' in self.handles and 'grad_end' in self.handles:
                    # 优先从加载的工程数据中恢复位置
                    if hasattr(self, 'grad_points_data') and self.grad_points_data:
                        w = self.canvas.logical_size.width() if getattr(self.canvas, 'logical_size', None) else 1000
                        h = self.canvas.logical_size.height() if getattr(self.canvas, 'logical_size', None) else 1000
                        
                        # 反解归一化绝对坐标到 Item 的局部相对坐标
                        p1_local = QPointF(self.grad_points_data[0][0] * w - self.pos().x(), self.grad_points_data[0][1] * h - self.pos().y())
                        p2_local = QPointF(self.grad_points_data[1][0] * w - self.pos().x(), self.grad_points_data[1][1] * h - self.pos().y())
                        
                        self.handles['grad_start'].setPos(p1_local)
                        self.handles['grad_end'].setPos(p2_local)
                        
                        # 消耗掉数据，防止影响后续的手动拖拽
                        self.grad_points_data = None
                    # 如果是新建时的默认状态
                    elif self.handles['grad_start'].pos() == QPointF(0, 0) and self.handles['grad_end'].pos() == QPointF(0, 0):
                        self.handles['grad_start'].setPos(-100, 0)
                        self.handles['grad_end'].setPos(100, 0)
                    if self._video_window_mode:
                        self.handles['center'].setPos((self.handles['grad_start'].pos()
                                                       + self.handles['grad_end'].pos()) / 2.0)
            if self._video_window_mode:
                active = self.shape_id == getattr(self.canvas, 'active_shape_id', None) or self.isSelected()
                if getattr(self.canvas, 'brush_mode_active', False) or getattr(self.canvas, 'sam_mode_active', False):
                    active = False
                for handle in self.handles.values():
                    if self.shape_type != 'curve' or handle.handle_type in ('center', 'rotate'):
                        handle.setVisible(active)
                    elif not active:
                        handle.setVisible(False)
        finally:
            self._is_updating_handles = False

    def on_handle_moved(self, handle, pos, skip_sync=False) -> None:
        """Handle movement of control points with throttled engine sync.
        
        Performance Optimization:
        - UI updates are immediate (instant visual feedback)
        - Engine sync is throttled to 60 FPS during drag via _schedule_sync()
        - On mouse release, _sync_back() is called directly for final sync
        
        Args:
            handle: The MaskHandle being moved
            pos: The new position
            skip_sync: If True, use throttled sync during drag instead of immediate sync
        """
        if self._is_updating_handles: return
        
        # Calculate delta for potential multi-move
        old_pos = handle.pos()
        delta = pos - old_pos

        modifiers = QApplication.keyboardModifiers()
        is_alt = modifiers & Qt.KeyboardModifier.AltModifier

        # When a node is moved, mark it as selected
        if handle.handle_type == 'node':
            # SAFETY: Check bounds to prevent crash if handle exists but data is gone
            if handle.index < 0 or handle.index >= len(self.bezier_points):
                return

            self.selected_node_idx = handle.index
            
            if is_alt:
                # Alt+Drag on Node: Pull handles out (DaVinci style)
                idx = handle.index
                # Reset node pos visually while dragging (we are dragging handles instead)
                self._is_updating_handles = True
                handle.setPos(self.bezier_points[idx]['pos'])
                self._is_updating_handles = False
                
                # Update handles symmetrically
                self.bezier_points[idx]['type'] = 'smooth'
                self.bezier_points[idx]['c2'] = self.bezier_points[idx]['pos'] + (pos - self.bezier_points[idx]['pos'])
                self.bezier_points[idx]['c1'] = self.bezier_points[idx]['pos'] - (pos - self.bezier_points[idx]['pos'])
            else:
                # Normal move logic: move node and its handles
                # Multi-move logic: if this node is selected, move all other selected nodes
                if handle.isSelected():
                    self._is_updating_handles = True
                    for child in self.childItems():
                        if isinstance(child, MaskHandle) and child.handle_type == 'node' and child != handle and child.isSelected():
                            idx = child.index
                            # SAFETY: Check bounds for other selected nodes
                            if 0 <= idx < len(self.bezier_points):
                                child.setPos(child.pos() + delta)
                                # Sync the data model for the other moved nodes
                                self.bezier_points[idx]['pos'] += delta
                                self.bezier_points[idx]['c1'] += delta
                                self.bezier_points[idx]['c2'] += delta
                    self._is_updating_handles = False
                
                # Update the dragged node itself
                idx = handle.index
                self.bezier_points[idx]['pos'] = pos
                self.bezier_points[idx]['c1'] += delta
                self.bezier_points[idx]['c2'] += delta
            
            self._update_handle_positions()
            # Immediate UI update for smooth visual feedback
            self.update()
            
            # Performance: Throttle engine sync during drag for real-time preview
            if skip_sync:
                self._schedule_sync()  # Throttled sync (max 60 FPS)
            else:
                self._sync_back()  # Immediate sync for non-drag interactions
            return

        if handle.handle_type.startswith('resize_') or handle.handle_type.startswith('feather'):
            self.prepareGeometryChange()
        if handle.handle_type in ('resize_tl', 'resize_tr', 'resize_bl', 'resize_br'):
            original = self._corner_resize_start_size or tuple(self.size)
            factor = 1.0 if self.shape_type == 'circle' else 0.5
            x_sign = -1.0 if handle.handle_type.endswith(('tl', 'bl')) else 1.0
            y_sign = -1.0 if handle.handle_type.endswith(('tl', 'tr')) else 1.0
            corner = QPointF(original[0] * factor * x_sign, original[1] * factor * y_sign)
            denominator = max(1.0, corner.x() ** 2 + corner.y() ** 2)
            scale = (pos.x() * corner.x() + pos.y() * corner.y()) / denominator
            scale = max(10.0 / max(1.0, min(original)), scale)
            self.size = [original[0] * scale, original[1] * scale]
        elif handle.handle_type in ('resize_w', 'resize_w_neg'):
            if self.shape_type == "circle":
                self.size[0] = max(10, abs(pos.x()))
            else:
                self.size[0] = max(10, abs(pos.x()) * 2)
        elif handle.handle_type in ('resize_h', 'resize_h_neg'):
            if self.shape_type == "circle":
                self.size[1] = max(10, abs(pos.y()))
            else:
                self.size[1] = max(10, abs(pos.y()) * 2)
        elif handle.handle_type == 'rotate':
            # Use local coordinates directly — handle is already constrained to fixed radius
            angle = math.degrees(math.atan2(pos.y(), pos.x())) + 90
            self.rotation = angle
            self.setRotation(angle)
        elif handle.handle_type in ('feather_r', 'feather_l', 'feather_t', 'feather_b'):
            if self._video_window_mode:
                half_w, half_h = self._video_half_size()
                horizontal = handle.handle_type in ('feather_l', 'feather_r')
                edge, distance = (half_w, abs(pos.x())) if horizontal else (half_h, abs(pos.y()))
                width = self.canvas.logical_size.width() if getattr(self.canvas, 'logical_size', None) else 1000
                if self.shape_type == 'rect' and self.edge_softness is not None:
                    index = ('feather_l', 'feather_r', 'feather_t', 'feather_b').index(handle.handle_type)
                    self.edge_softness[index] = max(0.0, min(100.0, (distance - edge) * 200.0 / width))
                else:
                    self.feather = max(0.0, min(100.0, (distance - edge) * 100.0 / (width * 0.4)))
            elif handle.handle_type in ('feather_r', 'feather_l'):
                if self.shape_type == "circle":
                    edge = self.size[0]
                else:
                    edge = self.size[0] / 2
                dist = abs(pos.x())
                self.feather = max(0, min(100.0, (dist - edge) / edge * 100.0))
            else:  # feather_t, feather_b
                if self.shape_type == "circle":
                    edge = self.size[1]
                else:
                    edge = self.size[1] / 2
                dist = abs(pos.y())
                self.feather = max(0, min(100.0, (dist - edge) / edge * 100.0))
        elif handle.handle_type == 'ctrl':
            idx = handle.index
            # SAFETY: Check bounds
            if idx < 0 or idx >= len(self.bezier_points):
                return

            if is_alt:
                self.bezier_points[idx]['type'] = 'corner'
            if handle.sub_type == 'c1':
                self.bezier_points[idx]['c1'] = pos
                if self.bezier_points[idx]['type'] == 'smooth':
                    rel = pos - self.bezier_points[idx]['pos']
                    self.bezier_points[idx]['c2'] = self.bezier_points[idx]['pos'] - rel
            else:
                self.bezier_points[idx]['c2'] = pos
                if self.bezier_points[idx]['type'] == 'smooth':
                    rel = pos - self.bezier_points[idx]['pos']
                    self.bezier_points[idx]['c1'] = self.bezier_points[idx]['pos'] - rel
            self._update_handle_positions()
            # Immediate UI update for smooth visual feedback
            self.update()
            # Use throttled sync for ctrl handle drag
            if skip_sync:
                self._schedule_sync()
            else:
                self._sync_back()
            return
        elif handle.handle_type == 'feather_curve':
            # Curve per-point feather handle drag
            idx = handle.index
            if idx < 0 or idx >= len(self.bezier_points):
                return
            p = self.bezier_points[idx]
            # Compute distance from point to handle along normal direction
            c2_pos = p.get('c2', p['pos'])
            tangent = c2_pos - p['pos']
            tlen = math.sqrt(tangent.x()**2 + tangent.y()**2)
            if tlen > 1e-6:
                normal = QPointF(-tangent.y() / tlen, tangent.x() / tlen)
            else:
                normal = QPointF(0, -1)

            # Project handle movement onto normal
            diff = pos - p['pos']
            dist = diff.x() * normal.x() + diff.y() * normal.y()

            # Convert pixel distance to feather value (0-100)
            # AUD-25200: 同上——scene_width 不存在，恒回退 512 使手柄距离
            # 与引擎实际羽化像素不符；改用 logical_size 宽度作基准。
            canvas_w = self.canvas.logical_size.width() if getattr(self.canvas, 'logical_size', None) else 1000
            feather_scale = 4.0 * (canvas_w / 5.0) / 100.0
            if feather_scale > 0.01:
                fval = max(0, min(100, abs(dist) / feather_scale))
            else:
                fval = 50

            if handle.sub_type == 'fi':
                p['fi'] = fval
            else:
                p['fo'] = fval

            self._update_handle_positions()
            self.update()
            if skip_sync:
                self._schedule_sync()
            else:
                self._sync_back()
            return
        elif handle.handle_type in ['grad_start', 'grad_end']:
            # Handled by _sync_back which pulls current handle positions
            if self._video_window_mode and 'center' in self.handles:
                other = self.handles['grad_end' if handle.handle_type == 'grad_start' else 'grad_start'].pos()
                self._is_updating_handles = True
                try:
                    self.handles['center'].setPos((other + pos) / 2.0)
                finally:
                    self._is_updating_handles = False

        if self._video_window_mode and self.shape_type in ('circle', 'rect'):
            self._update_handle_positions()
        
        # For resize, rotate, feather handles: use throttled sync during drag
        if skip_sync:
            self._schedule_sync()
        else:
            self._sync_back()

    def on_node_deleted(self, index) -> None:
        if len(self.bezier_points) > 2:
            self.bezier_points.pop(index)
            # Reset selection if we deleted the selected node
            if self.selected_node_idx == index:
                self.selected_node_idx = -1
            elif self.selected_node_idx > index:
                self.selected_node_idx -= 1
                
            # SAFETY: Defer handle update to avoid crashing if this was called during event processing
            # We use a singleShot to allow the event to finish processing before destroying objects
            QTimer.singleShot(0, self._deferred_update)

    def _deferred_update(self) -> None:
        # SAFETY: Check if the item is still in a scene before updating
        if not self.scene():
            return
        self._update_handle_positions()
        self._sync_back()
        self.update()

    def on_node_double_clicked(self, index) -> None:
        # SAFETY: Check bounds
        if index < 0 or index >= len(self.bezier_points):
            return
            
        t = self.bezier_points[index]['type']
        self.bezier_points[index]['type'] = 'corner' if t == 'smooth' else 'smooth'
        if self.bezier_points[index]['type'] == 'smooth':
            rel = self.bezier_points[index]['c1'] - self.bezier_points[index]['pos']
            # If handles are overlapping node, give default spread
            if math.hypot(rel.x(), rel.y()) < 2.0:
                rel = QPointF(30, 0)
            self.bezier_points[index]['c2'] = self.bezier_points[index]['pos'] - rel
            self.bezier_points[index]['c1'] = self.bezier_points[index]['pos'] + rel
        self._update_handle_positions()
        self._sync_back()
        self.update()

    def hoverEnterEvent(self, event) -> None:
        """Task 4: Handle hover enter for highlight effect."""
        self._is_hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        """Task 4: Handle hover leave for highlight effect."""
        self._is_hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if hasattr(self, 'canvas'):
            self.canvas.current_interaction_type = "general"
            self.canvas.interaction_started.emit()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if hasattr(self, 'canvas'):
            self.canvas.interaction_ended.emit()

    def itemChange(self, change, value):
        # SAFETY: Prevent updates if item is being removed or has no scene
        if not self.scene():
            return super().itemChange(change, value)

        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._sync_back()
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedChange:
            # Move selected shape to front
            if value is True:
                self.setZValue(25)
            else:
                self.setZValue(20)
            # Fix: Update handle positions when selection changes to restore visibility
            self._update_handle_positions()
            # Immediate visual update for highlight change
            self.update()
        return super().itemChange(change, value)

    def recenter(self) -> None:
        """Moves the item origin to the centroid of its points to make rotation/movement natural."""
        if self.shape_type != "curve" or not self.bezier_points:
            return

        # Calculate centroid of all points
        avg_x = sum(p['pos'].x() for p in self.bezier_points) / len(self.bezier_points)
        avg_y = sum(p['pos'].y() for p in self.bezier_points) / len(self.bezier_points)
        offset = QPointF(avg_x, avg_y)

        # If the offset is negligible, skip to avoid jitter
        if math.hypot(offset.x(), offset.y()) < 0.5:
            return

        self._is_updating_handles = True
        try:
            # Move item position
            self.setPos(self.pos() + offset)
            
            # Adjust all points (pos, c1, c2) relative to new center
            for p in self.bezier_points:
                p['pos'] -= offset
                p['c1'] -= offset
                p['c2'] -= offset
                
            self._update_handle_positions()
        finally:
            self._is_updating_handles = False
        
        self._sync_back()

    def _sync_back(self) -> None:
        if not self.canvas.logical_size or self._is_updating_handles: return
        w, h = self.canvas.logical_size.width(), self.canvas.logical_size.height()
        
        norm_center = [self.pos().x() / w, self.pos().y() / h]
        norm_size = [self.size[0] / w, self.size[1] / h]
        
        data = {
            "center": norm_center,
            "size": norm_size,
            "rotation": self.rotation,
            # 如果是笔刷类型，强制将传递给后端的全局羽化设为 0
            "feather": 0.0 if self.shape_type == "brush" else self.feather
        }
        if self._video_window_mode and self.shape_type == 'rect' and self.edge_softness is not None:
            data['edge_softness'] = list(self.edge_softness)
        
        if self.shape_type == "curve":
            norm_points = []
            for p in self.bezier_points:
                norm_points.append({
                    **p,
                    'pos': [(self.pos().x() + p['pos'].x()) / w, (self.pos().y() + p['pos'].y()) / h],
                    'c1': [(self.pos().x() + p['c1'].x()) / w, (self.pos().y() + p['c1'].y()) / h],
                    'c2': [(self.pos().x() + p['c2'].x()) / w, (self.pos().y() + p['c2'].y()) / h],
                    'type': p['type']
                })
            data['points'] = norm_points
        elif self.shape_type == "gradient":
            p1 = self.handles['grad_start'].pos() + self.pos()
            p2 = self.handles['grad_end'].pos() + self.pos()
            data['grad_points'] = [[p1.x()/w, p1.y()/h], [p2.x()/w, p2.y()/h]]
            
        self.canvas.mask_shape_changed.emit(self.shape_id, data)

class ViewportMenuBar(ViewportToolBarBase):
    """Color viewport toolbar with left, center, and right tool groups.

    View/Guides stay on the left, the active timeline sits at the exact
    center, and common display tools are grouped on the right.
    """

    capture_still_requested = pyqtSignal()
    timeline_switch_requested = pyqtSignal(str)
    # P1-COLOR-05: A/B 比较 / reference wipe 监看
    ab_toggle_requested = pyqtSignal()
    capture_reference_requested = pyqtSignal()
    clear_reference_requested = pyqtSignal()
    auto_color_requested = pyqtSignal()
    # P1-COLOR-19: Color workspace 预设
    workspace_selected = pyqtSignal(str)
    save_workspace_requested = pyqtSignal()
    delete_workspace_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__("viewportMenuBar", parent)
        self._canvas = parent

        # Left side - Menu dropdowns
        self.view_menu_btn = self._make_menu_button("View")
        self.guides_menu_btn = self._make_guides_btn()

        # Create menus
        self.view_menu = self._create_view_menu()

        # Spacer
        spacer_left = QWidget()
        spacer_left.setFixedWidth(2)
        self._layout.addWidget(spacer_left)

        # Left menus
        self._layout.addWidget(self.view_menu_btn)
        self._layout.addWidget(self.guides_menu_btn)

        # Keep the timeline selector out of the layout so it remains exactly
        # centered even when the left and right groups have different widths.
        self._timeline_selector = ViewportTimelineSelector(self)
        self._timeline_selector.timeline_switch_requested.connect(
            self.timeline_switch_requested.emit
        )
        self._timeline_selector.hide()
        self._position_timeline_selector()

        # Right-aligned common display tools.
        right_spacer = QWidget()
        right_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._layout.addWidget(right_spacer)

        self._add_toolbar_separator()
        self._layout.addWidget(self._make_mask_btn())
        self._layout.addWidget(self._make_fit_btn())
        self._layout.addWidget(self._make_split_btn())
        self._add_fps_indicator()

        # Sync menu states
        self._sync_button_states()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the timeline selector centered in the full toolbar row."""
        super().resizeEvent(event)
        self._position_timeline_selector()

    def _position_timeline_selector(self) -> None:
        """Position the borderless timeline selector at toolbar center."""
        chrome = getattr(self, "_color_chrome", None)
        if chrome is not None:
            chrome.position_title()
            return
        if not hasattr(self, "_timeline_selector"):
            return
        selector = self._timeline_selector
        width = min(320, max(80, selector.sizeHint().width()))
        x = max(0, (self.width() - width) // 2)
        selector.setGeometry(x, 0, width, self.height())
        selector.raise_()

    def set_timeline_choices(
        self,
        timelines: Any,
        active_timeline_id: Optional[str],
    ) -> None:
        """Show project timelines and mark the active one."""
        self._timeline_selector.set_timeline_choices(timelines, active_timeline_id)
        self._position_timeline_selector()

    def _add_toolbar_separator(self) -> None:
        """Add a subtle separator before the right-aligned tool group."""
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        separator.setFixedSize(1, 14)
        separator.setStyleSheet("color: #45464c;")
        self._layout.addWidget(separator)
    
    def _create_view_menu(self):
        """Create view menu (merged View + Display + Layout)."""
        menu = QMenu(self)

        # Zoom
        self.zoom_in_act = menu.addAction(I18N.tr("viewport.zoom_in", "Zoom In"),
            lambda: self._zoom_by_factor(1.25))
        self.zoom_in_act.setShortcut("Ctrl+=")

        self.zoom_out_act = menu.addAction(I18N.tr("viewport.zoom_out", "Zoom Out"),
            lambda: self._zoom_by_factor(1/1.25))
        self.zoom_out_act.setShortcut("Ctrl+-")

        menu.addSeparator()

        # Fit / Actual
        self.fit_act = menu.addAction(I18N.tr("viewport.fit", "Fit to Window"), self.fit_requested.emit)
        self.fit_act.setShortcut("0")

        self.actual_size_act = menu.addAction(I18N.tr("viewport.actual_size", "Actual Size"),
            lambda: self._set_actual_size())
        self.actual_size_act.setShortcut("1")

        menu.addSeparator()

        # Display toggles
        self.mask_act = menu.addAction(I18N.tr("viewport.mask", "Mask Preview"))
        self.mask_act.setCheckable(True)
        self.mask_act.setShortcut("Shift+H")
        self.mask_act.toggled.connect(lambda checked: self.mask_toggled.emit(checked))

        self.split_act = menu.addAction(I18N.tr("viewport.split", "Split View"))
        self.split_act.setCheckable(True)
        self.split_act.setShortcut("\\")
        self.split_act.toggled.connect(lambda checked: self.split_toggled.emit(checked))

        # P1-COLOR-05: A/B 比较 / 参考 wipe
        self.ab_toggle_act = menu.addAction(
            I18N.tr("viewport.ab_toggle", "A/B Toggle"),
            self.ab_toggle_requested.emit)
        self.ab_toggle_act.setShortcut("Ctrl+D")

        self.capture_reference_act = menu.addAction(
            I18N.tr("viewport.capture_reference", "Capture Reference Wipe"),
            self.capture_reference_requested.emit)
        # AUD-24601: 原 Ctrl+Shift+D 与 color.toggle_comparison 的默认键序
        # 冲突（Qt 歧义快捷键），改用与 ShortcutManager 注册一致的独占键序。
        self.capture_reference_act.setShortcut("Ctrl+Alt+D")

        self.clear_reference_act = menu.addAction(
            I18N.tr("viewport.clear_reference", "Clear Reference"),
            self.clear_reference_requested.emit)

        menu.addSeparator()

        # Layout
        self.fullscreen_act = menu.addAction(I18N.tr("viewport.fullscreen", "Fullscreen"),
            lambda: self._toggle_fullscreen())
        self.fullscreen_act.setShortcut("F11")

        self.focus_act = menu.addAction(I18N.tr("viewport.focus", "Focus Mode"),
            lambda: self._toggle_focus_mode())
        self.focus_act.setShortcut("Shift+F")

        menu.addSeparator()

        self.capture_still_act = menu.addAction(
            I18N.tr("viewport.capture_still", "Capture Still"),
            self.capture_still_requested.emit
        )
        # AUD-24601: 原 Ctrl+Shift+S 与 file.save_as 的 QAction 同键序
        # （menu_toolbar_controller 的 Save Project As），Qt 歧义快捷键
        # 会导致按键无响应或触发错误命令，改用独占键序。
        self.capture_still_act.setShortcut("Ctrl+Alt+S")

        # P1-COLOR-21: Auto Balance（分析未调色源，给出可撤销建议）
        self.auto_color_act = menu.addAction(
            I18N.tr("viewport.auto_color", "Auto Color…"),
            self.auto_color_requested.emit)
        self.auto_color_act.setShortcut("Ctrl+Shift+A")

        menu.addSeparator()

        # P1-COLOR-19: Color workspace 预设（一键切换面板组合）
        self.workspace_menu = self._create_workspace_menu()
        menu.addMenu(self.workspace_menu)

        _inject_menu_shortcuts(menu)

        self.view_menu_btn.clicked.connect(lambda: self._show_menu_at_button(self.view_menu, self.view_menu_btn))
        return menu

    # ------------------------------------------------------------------
    # P1-COLOR-19: Workspace preset menu
    # ------------------------------------------------------------------
    def set_workspace_provider(self, provider) -> None:
        """Set provider() -> (rows, active_id) used to rebuild the menu.

        rows: list of (preset_id, display_name, is_user); active_id: currently
        matching preset id or None. Called every time the menu opens so the
        entries always reflect the latest saved/deleted state.
        """
        self._workspace_provider = provider

    def refresh_workspace_menu(self) -> None:
        """Rebuild the workspace menu immediately (after apply/save/delete)."""
        self._rebuild_workspace_menu()

    def _create_workspace_menu(self):
        menu = QMenu(I18N.tr("viewport.workspace", "Workspace"), self)
        self._workspace_provider = None
        menu.aboutToShow.connect(self._rebuild_workspace_menu)
        return menu

    def _rebuild_workspace_menu(self) -> None:
        menu = self.workspace_menu
        menu.clear()
        provider = getattr(self, "_workspace_provider", None)
        rows, active_id = [], None
        if provider is not None:
            try:
                rows, active_id = provider()
            except Exception:
                rows, active_id = [], None
        has_user = any(is_user for _, _, is_user in rows)
        for preset_id, label, _is_user in rows:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(preset_id == active_id)
            act.triggered.connect(
                lambda _checked, pid=preset_id: self.workspace_selected.emit(pid))
        if rows:
            menu.addSeparator()
        save_act = menu.addAction(
            I18N.tr("workspace.save_current", "Save Current as Workspace..."))
        save_act.triggered.connect(self.save_workspace_requested.emit)
        delete_act = menu.addAction(
            I18N.tr("workspace.delete", "Delete Workspace..."))
        delete_act.setEnabled(has_user)
        delete_act.triggered.connect(self.delete_workspace_requested.emit)

    def _get_canvas(self):
        """Safely get the Canvas parent."""
        return self._canvas
    
    def _zoom_by_factor(self, factor) -> None:
        """Zoom the view by a given factor."""
        canvas = self._get_canvas()
        if canvas and hasattr(canvas, 'scale'):
            canvas.scale(factor, factor)
            if hasattr(canvas, '_update_lod'):
                canvas._update_lod()
            canvas.viewport().update()
    
    def _set_actual_size(self) -> None:
        """Set view to actual size (100% zoom)."""
        canvas = self._get_canvas()
        if canvas and hasattr(canvas, 'resetTransform'):
            canvas.resetTransform()
            if hasattr(canvas, '_update_lod'):
                canvas._update_lod()
            canvas.viewport().update()

    def _sync_button_states(self) -> None:
        """Sync button states with canvas state.

        AUD-25208：mask 按钮反映引擎 matte 预览而非覆盖层可见性，
        程序化 setChecked 用 blockSignals 状态锁防递归。
        """
        canvas = self._get_canvas()
        if not canvas:
            return

        mask_active = bool(
            getattr(getattr(canvas, 'engine', None), 'show_mask_mode', False)
        )
        self.mask_btn.blockSignals(True)
        self.mask_btn.setChecked(mask_active)
        self.mask_btn.blockSignals(False)

        if hasattr(canvas, 'engine') and canvas.engine:
            self.split_btn.blockSignals(True)
            self.split_btn.setChecked(canvas.engine.split_view_active)
            self.split_btn.blockSignals(False)
    
    def _toggle_fullscreen(self) -> None:
        """Toggle fullscreen mode."""
        window = self.window()
        if window:
            if window.isFullScreen():
                window.showNormal()
            else:
                window.showFullScreen()
    
    def _toggle_focus_mode(self) -> None:
        """Toggle focus mode (hide all panels except viewport)."""
        host = self._focus_mode_host()
        if host is not None:
            host.toggle_focus_mode()

    def _focus_mode_host(self) -> QWidget | None:
        """Resolve the application host even while its viewport dock floats."""
        candidate = self._get_canvas()
        while isinstance(candidate, QWidget):
            if callable(getattr(candidate, "toggle_focus_mode", None)):
                return candidate
            candidate = candidate.parentWidget()
        return None
    
    def sync_menu_state(self) -> None:
        """Sync menu states with canvas state.

        AUD-25208 修复：
        - mask 按钮反映的是引擎 matte 预览（engine.show_mask_mode），
          而非画布覆盖层可见性（canvas._show_mask_overlays）。两者是
          不同概念：toggled 信号链路 (_on_mask_preview_toggled) 写的是
          引擎 matte 预览，此前用覆盖层可见性回填会把引擎状态悄悄改写。
        - 程序化 setChecked 前后 blockSignals（状态锁），防止程序化
          更新触发 toggled 递归广播。
        """
        canvas = self._get_canvas()
        # Ensure parent is a Canvas instance with required attributes
        if not canvas or not hasattr(canvas, '_show_mask_overlays'):
            return

        # Sync mask state（来源：引擎 matte 预览状态）
        mask_active = bool(
            getattr(getattr(canvas, 'engine', None), 'show_mask_mode', False)
        )
        for widget in (self.mask_btn, getattr(self, 'mask_act', None)):
            if widget is None:
                continue
            widget.blockSignals(True)
            widget.setChecked(mask_active)
            widget.blockSignals(False)

        # Sync split state
        if hasattr(canvas, 'engine') and canvas.engine:
            split_active = canvas.engine.split_view_active
            for widget in (self.split_btn, getattr(self, 'split_act', None)):
                if widget is None:
                    continue
                widget.blockSignals(True)
                widget.setChecked(split_active)
                widget.blockSignals(False)



class _InOutSlider(QSlider):
    """QSlider that paints In/Out range markers on top of the native groove.

    Used by VideoControlBar. The In/Out points are stored as frame numbers and
    normalised against the slider's own maximum (which the bar keeps equal to
    total_frames - 1), so the markers stay aligned during scrubbing.
    """

    _IO_COLOR = QColor(255, 136, 0)          # primary marker line
    _IO_FILL = QColor(255, 136, 0, 40)       # translucent range fill

    def __init__(self, orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self._in_point: int | None = None
        self._out_point: int | None = None

    def set_in_out_points(self, in_point: int | None, out_point: int | None) -> None:
        self._in_point = in_point
        self._out_point = out_point
        self.update()

    def paintEvent(self, event) -> None:
        if self.property("colorMonitorScrubber"):
            from src.features.color.ui.color_viewport_chrome import paint_color_scrubber
            paint_color_scrubber(self)
            return
        # Draw the native slider first.
        super().paintEvent(event)
        if self._in_point is None and self._out_point is None:
            return
        from PyQt6.QtGui import QPainter
        max_val = max(1, self.maximum())
        w = self.width()
        h = self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        in_x = int((self._in_point / max_val) * w) if self._in_point is not None else 0
        out_x = int((self._out_point / max_val) * w) if self._out_point is not None else w
        # Range fill.
        if self._in_point is not None and self._out_point is not None:
            painter.fillRect(in_x, 0, max(1, out_x - in_x), h, self._IO_FILL)
        # Marker lines.
        pen = QPen(self._IO_COLOR)
        pen.setWidth(2)
        painter.setPen(pen)
        if self._in_point is not None:
            painter.drawLine(in_x, 0, in_x, h)
        if self._out_point is not None:
            painter.drawLine(out_x, 0, out_x, h)
        painter.end()


class _FusionFrameRulerSlider(QSlider):
    """Fusion-style frame ruler used by Composite playback controls."""

    _BG = QColor(20, 21, 24)
    _BORDER = QColor(54, 56, 60)
    _TICK = QColor(83, 87, 92)
    _MAJOR_TICK = QColor(126, 130, 136)
    _TEXT = QColor(150, 154, 160)
    _PLAYHEAD = QColor(236, 191, 0)
    _RANGE = QColor(43, 215, 96)
    _RANGE_FILL = QColor(43, 215, 96, 26)
    # P2: keyframe markers drawn as small diamonds on the ruler. Orange matches
    # the keyframe theme used in the curve editor / KeyframesPanel.
    _KEYFRAME = QColor(204, 102, 0)

    def __init__(self, orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self._in_point: int | None = None
        self._out_point: int | None = None
        self._keyframe_frames: list[int] = []
        self.setMouseTracking(True)
        self.setFixedHeight(38)

    def set_in_out_points(self, in_point: int | None, out_point: int | None) -> None:
        self._in_point = in_point
        self._out_point = out_point
        self.update()

    def set_keyframe_frames(self, frames) -> None:
        """P2: set the keyframe frame numbers to mark on the ruler.

        Pass an empty list/None to clear. Frames are normalised against the
        slider maximum the same way in/out points are, so they line up with
        the playhead and ticks.
        """
        self._keyframe_frames = sorted(set(int(f) for f in (frames or [])))
        self.update()

    def _value_to_x(self, value: int) -> int:
        max_val = max(1, self.maximum() - self.minimum())
        ratio = (int(value) - self.minimum()) / max_val
        return int(round(ratio * max(1, self.width() - 1)))

    def _x_to_value(self, x: int) -> int:
        width = max(1, self.width() - 1)
        x = max(0, min(int(x), width))
        span = self.maximum() - self.minimum()
        return self.minimum() + int(round((x / width) * span))

    def _tick_step(self) -> int:
        total = max(1, self.maximum() - self.minimum())
        target_ticks = max(2, self.width() // 48)
        raw = max(1.0, total / target_ticks)
        magnitude = 10 ** int(math.floor(math.log10(raw)))
        for mul in (1, 2, 5, 10):
            step = magnitude * mul
            if raw <= step:
                return max(1, int(step))
        return max(1, int(magnitude * 10))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self.setValue(self._x_to_value(event.position().x()))
            self.sliderPressed.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.isSliderDown():
            self.setValue(self._x_to_value(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setValue(self._x_to_value(event.position().x()))
            self.setSliderDown(False)
            self.sliderReleased.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect()
        painter.fillRect(rect, self._BG)
        painter.setPen(QPen(self._BORDER, 1))
        painter.drawLine(0, 0, rect.width(), 0)
        painter.drawLine(0, rect.height() - 1, rect.width(), rect.height() - 1)

        max_frame = max(0, self.maximum())
        range_start = self._in_point if self._in_point is not None else self.minimum()
        range_end = self._out_point if self._out_point is not None else max_frame
        range_start = max(self.minimum(), min(range_start, max_frame))
        range_end = max(self.minimum(), min(range_end, max_frame))
        if range_end < range_start:
            range_start, range_end = range_end, range_start
        x1 = self._value_to_x(range_start)
        x2 = self._value_to_x(range_end)
        painter.fillRect(x1, 16, max(1, x2 - x1), rect.height() - 20, self._RANGE_FILL)

        step = self._tick_step()
        minor_step = max(1, step // 4)
        painter.setFont(self.font())
        for frame in range(self.minimum(), max_frame + 1, minor_step):
            x = self._value_to_x(frame)
            is_major = frame % step == 0
            tick_h = 20 if is_major else 10
            painter.setPen(QPen(self._MAJOR_TICK if is_major else self._TICK, 1))
            painter.drawLine(x, rect.height() - 2, x, rect.height() - 2 - tick_h)
            if is_major:
                painter.setPen(self._TEXT)
                painter.drawText(QRectF(x + 3, 2, 42, 14), Qt.AlignmentFlag.AlignLeft, str(frame))

        painter.setPen(QPen(self._RANGE, 2))
        painter.drawLine(x1, rect.height() - 4, x2, rect.height() - 4)
        painter.drawLine(x1, rect.height() - 8, x1, rect.height() - 1)
        painter.drawLine(x2, rect.height() - 8, x2, rect.height() - 1)

        # P2: keyframe markers — small diamonds centred on the bottom tick band
        # so they read against the ruler without fighting the playhead.
        if self._keyframe_frames:
            kf_y = rect.height() - 4
            painter.setPen(QPen(self._KEYFRAME, 1))
            painter.setBrush(QBrush(self._KEYFRAME))
            for frame in self._keyframe_frames:
                if frame < self.minimum() or frame > max_frame:
                    continue
                kx = self._value_to_x(frame)
                # 3px half-width diamond.
                diamond = QPolygonF([
                    QPointF(kx, kf_y - 3),
                    QPointF(kx + 3, kf_y),
                    QPointF(kx, kf_y + 3),
                    QPointF(kx - 3, kf_y),
                ])
                painter.drawPolygon(diamond)

        playhead_x = self._value_to_x(self.value())
        painter.setPen(QPen(self._PLAYHEAD, 2))
        painter.drawLine(playhead_x, 0, playhead_x, rect.height())
        painter.fillRect(playhead_x - 3, 0, 7, 3, self._PLAYHEAD)
        painter.end()


class VideoControlBar(QFrame):
    """Video playback control bar at the bottom of the viewport."""

    # Signals
    seek_requested = pyqtSignal(int)
    timecode_changed = pyqtSignal(str)
    scrub_started = pyqtSignal(int)
    scrub_moved = pyqtSignal(int)
    scrub_finished = pyqtSignal(int)
    play_toggled = pyqtSignal()
    next_frame = pyqtSignal()
    prev_frame = pyqtSignal()
    first_frame = pyqtSignal()
    last_frame = pyqtSignal()
    # In/Out playback-range signals (used by the Composite page; the Color page
    # keeps using show_for_video() so these stay unconnected there).
    mark_in_requested = pyqtSignal()
    mark_out_requested = pyqtSignal()
    clear_in_out_requested = pyqtSignal()
    # Loop toggle signal (bool = loop enabled).
    loop_toggled = pyqtSignal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("videoControlBar")
        self.setFixedHeight(36)
        self.setMaximumHeight(36)
        self._fusion_mode = False

        # Main layout
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.ruler_slider = _FusionFrameRulerSlider(Qt.Orientation.Horizontal)
        self.ruler_slider.setRange(0, 100)
        self.ruler_slider.setValue(0)
        self.ruler_slider.sliderPressed.connect(self._on_slider_pressed)
        self.ruler_slider.sliderReleased.connect(self._on_slider_released)
        self.ruler_slider.valueChanged.connect(self._on_slider_changed)
        self.ruler_slider.setVisible(False)
        outer_layout.addWidget(self.ruler_slider)

        row_widget = QWidget(self)
        row_widget.setObjectName("videoControlRow")
        row_widget.setFixedHeight(36)
        layout = QHBoxLayout(row_widget)
        layout.setContentsMargins(8, 2, 8, 4)
        layout.setSpacing(3)
        outer_layout.addWidget(row_widget)

        button_size = 28
        icon_size = 15

        # ── In/Out range buttons (hidden by default; shown via show_for_video_with_io) ──
        self.btn_mark_in = QToolButton()
        self.btn_mark_in.setText(I18N.tr("video.mark_in_label", "I"))
        self.btn_mark_in.setToolTip(I18N.tr("video.mark_in", "设置入点 (I)"))
        self.btn_mark_in.clicked.connect(self.mark_in_requested.emit)
        self.btn_mark_in.setFixedSize(28, 24)
        self.btn_mark_in.setVisible(False)

        self.btn_mark_out = QToolButton()
        self.btn_mark_out.setText(I18N.tr("video.mark_out_label", "O"))
        self.btn_mark_out.setToolTip(I18N.tr("video.mark_out", "设置出点 (O)"))
        self.btn_mark_out.clicked.connect(self.mark_out_requested.emit)
        self.btn_mark_out.setFixedSize(28, 24)
        self.btn_mark_out.setVisible(False)

        self.btn_clear = QToolButton()
        self.btn_clear.setText(I18N.tr("video.clear_io", "Clear"))
        self.btn_clear.setToolTip(I18N.tr("video.clear_io_tip", "清除入/出点"))
        self.btn_clear.clicked.connect(self.clear_in_out_requested.emit)
        self.btn_clear.setFixedSize(46, 24)
        self.btn_clear.setEnabled(False)
        self.btn_clear.setVisible(False)

        layout.addWidget(self.btn_mark_in)
        layout.addWidget(self.btn_mark_out)
        layout.addWidget(self.btn_clear)

        self.label_range_in = QLabel("0.0")
        self.label_range_in.setObjectName("rangeFrameBox")
        self.label_range_in.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_range_in.setFixedSize(54, 22)
        self.label_range_in.setVisible(False)

        self.label_range_out = QLabel("100.0")
        self.label_range_out.setObjectName("rangeFrameBox")
        self.label_range_out.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_range_out.setFixedSize(54, 22)
        self.label_range_out.setVisible(False)

        layout.addWidget(self.label_range_in)
        layout.addWidget(self.label_range_out)

        # Spacer separating the I/O group from transport buttons.
        self._io_separator = QWidget()
        self._io_separator.setFixedWidth(10)
        self._io_separator.setVisible(False)
        layout.addWidget(self._io_separator)

        # Control buttons
        self.btn_first = QToolButton()
        self.btn_first.setToolTip(I18N.tr("video.first_frame", "跳到第一帧"))
        self.btn_first.setFixedSize(button_size, button_size)
        self.btn_first.setIconSize(QSize(icon_size, icon_size))
        self.btn_first.setIcon(icon_jump_start(IconColors.DEFAULT))
        self.btn_first.clicked.connect(self.first_frame.emit)

        self.btn_prev = QToolButton()
        self.btn_prev.setToolTip(I18N.tr("video.prev_frame", "上一帧 (←)"))
        self.btn_prev.setFixedSize(button_size, button_size)
        self.btn_prev.setIconSize(QSize(icon_size, icon_size))
        self.btn_prev.setIcon(icon_step_back(IconColors.DEFAULT))
        self.btn_prev.clicked.connect(self.prev_frame.emit)

        self.btn_play = QToolButton()
        self.btn_play.setToolTip(I18N.tr("video.play_pause", "播放/暂停 (Space)"))
        self.btn_play.setFixedSize(32, button_size)
        self.btn_play.setIconSize(QSize(icon_size + 3, icon_size + 3))
        self.btn_play.setIcon(icon_play(IconColors.ORANGE))
        self.btn_play.clicked.connect(self.play_toggled.emit)

        self.btn_next = QToolButton()
        self.btn_next.setToolTip(I18N.tr("video.next_frame", "下一帧 (→)"))
        self.btn_next.setFixedSize(button_size, button_size)
        self.btn_next.setIconSize(QSize(icon_size, icon_size))
        self.btn_next.setIcon(icon_step_forward(IconColors.DEFAULT))
        self.btn_next.clicked.connect(self.next_frame.emit)

        self.btn_last = QToolButton()
        self.btn_last.setToolTip(I18N.tr("video.last_frame", "跳到最后一帧"))
        self.btn_last.setFixedSize(button_size, button_size)
        self.btn_last.setIconSize(QSize(icon_size, icon_size))
        self.btn_last.setIcon(icon_jump_end(IconColors.DEFAULT))
        self.btn_last.clicked.connect(self.last_frame.emit)

        # Loop button — default ON for the Composite page.  Color resets this
        # to OFF when its timeline-clip preview starts (see FileOpsMixin), so
        # Composite keeps its in/out loop behaviour and Color defaults to
        # cross-clip playback until the user asks to loop the selected clip.
        self.btn_loop = QToolButton()
        self.btn_loop.setToolTip(I18N.tr("video.loop", "循环播放"))
        self.btn_loop.setCheckable(True)
        self.btn_loop.setChecked(True)
        self.btn_loop.setFixedSize(button_size, button_size)
        self.btn_loop.setIconSize(QSize(icon_size, icon_size))
        self.btn_loop.setIcon(icon_loop(IconColors.ORANGE))
        self.btn_loop.toggled.connect(self._on_loop_toggled)

        # Add buttons to layout.  The spacers are only shown in Composite's
        # Fusion-style mode so Color keeps its compact left-to-right layout.
        self._transport_left_spacer = QWidget()
        self._transport_left_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._transport_left_spacer.setVisible(False)
        self._transport_right_spacer = QWidget()
        self._transport_right_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._transport_right_spacer.setVisible(False)
        layout.addWidget(self._transport_left_spacer)
        layout.addWidget(self.btn_first)
        layout.addWidget(self.btn_prev)
        layout.addWidget(self.btn_play)
        layout.addWidget(self.btn_next)
        layout.addWidget(self.btn_last)
        layout.addWidget(self.btn_loop)
        layout.addWidget(self._transport_right_spacer)

        # Slider (custom subclass paints In/Out markers when present).
        self.slider = _InOutSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName("colorTransportSlider")
        self.slider.setRange(0, 100)
        self.slider.setValue(0)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(10)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.valueChanged.connect(self._on_slider_changed)

        # Timecode label: match Edit's transport styling.  Keep the legacy
        # label_time alias because Composite's Fusion mode still toggles its
        # visibility and supplies its own duration text.
        self.timecode_label = QLabel("00:00:00:00")
        self.timecode_label.setObjectName("colorTransportTimecode")
        self.timecode_label.setFixedWidth(110)
        self.timecode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timecode_label.setStyleSheet("""
            QLabel#colorTransportTimecode {
                color: #D6D8DE;
                font-size: 14px;
                font-weight: bold;
                background-color: #15161A;
                padding: 3px 8px;
                border-radius: 4px;
                font-family: 'Courier New', monospace;
            }
            QLabel#colorTransportTimecode:hover {
                background-color: #202126;
            }
        """)
        self.label_time = self.timecode_label

        # Frame label
        self.label_frame = QLabel("0 / 0")
        self.label_frame.setFixedWidth(72)
        self.label_frame.setStyleSheet("color: #aaa; font-size: 11px;")

        # Add to layout
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.label_frame)
        layout.addWidget(self.label_time)

        # State
        self._slider_dragging = False
        self.total_frames = 0
        self.current_frame = 0
        self.is_playing = False
        self._fps = 24.0
        # CV-B02：精确 TC 格式化器（SourceTimecodeContext 域）。None 时
        # 保持旧 float fps 行为（显示连续性优先，不因域缺席黑屏时间码）。
        self._timecode_formatter = None
        # In/Out range (None = unset). Drives the slider markers + Clear button.
        self._in_point: int | None = None
        self._out_point: int | None = None

        # Initially hidden
        self.setVisible(False)

        # Apply style
        self._apply_style()

    def _apply_style(self) -> None:
        """Apply styling to the control bar."""
        self.setStyleSheet("""
            QFrame#videoControlBar {
                /* Match Edit's Program TransportControlBar surface. */
                background-color: #1a1a1c;
                border-top: 1px solid #2a2a2a;
            }
            QWidget#videoControlRow {
                background-color: #1a1a1c;
                border: none;
            }
            QSlider#colorTransportSlider {
                background-color: #1a1a1c;
            }
            QToolButton {
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 3px;
                margin: 1px;
            }
            QToolButton:hover {
                background-color: rgba(255, 255, 255, 18);
            }
            QToolButton:pressed {
                background-color: rgba(255, 255, 255, 10);
            }
            QLabel {
                background: none;
                border: none;
                color: #aaa;
                font-size: 11px;
            }
            QLabel#rangeFrameBox {
                background-color: #111215;
                border: 1px solid #36383e;
                border-radius: 3px;
                color: #ededed;
                font-weight: 600;
                font-size: 11px;
            }
        """)
        
        self.layout().setContentsMargins(0, 0, 0, 0)

    def set_total_frames(self, total: int) -> None:
        """Set total number of frames."""
        self.total_frames = total
        maximum = max(0, total - 1)
        self.slider.setMaximum(maximum)
        self.ruler_slider.setMaximum(maximum)
        # Repaint I/O markers (they are normalised against the slider maximum).
        self.slider.set_in_out_points(self._in_point, self._out_point)
        self.ruler_slider.set_in_out_points(self._in_point, self._out_point)
        self._update_display()

    def set_current_frame(self, frame: int) -> None:
        """Set current frame number."""
        self.current_frame = frame
        if not self._slider_dragging:
            for slider in (self.slider, self.ruler_slider):
                was_blocked = slider.blockSignals(True)
                slider.setValue(frame)
                slider.blockSignals(was_blocked)
        self._update_display()

    def set_playing(self, playing: bool) -> None:
        """Set playing state."""
        self.is_playing = playing
        if playing:
            self.btn_play.setIcon(icon_pause(IconColors.ORANGE))
        else:
            self.btn_play.setIcon(icon_play(IconColors.ORANGE))
        self._sync_preview_fps_indicator()

    def set_time_info(self, time_str: str) -> None:
        """Set the legacy duration display used by Composite's Fusion mode."""
        self.timecode_label.setText(time_str)
        self.timecode_changed.emit(time_str)

    def set_frame_rate(self, fps: float) -> None:
        """Set the frame rate used to format the Color transport timecode."""
        try:
            value = float(fps)
        except (TypeError, ValueError):
            return
        if value <= 0.0:
            return
        self._fps = value
        self._update_timecode()
        self._sync_preview_fps_indicator()

    def set_timecode_formatter(self, formatter) -> None:
        """CV-B02：注入精确 TC 格式化器（TransportTimecodeFormatter）。

        formatter 为 None 时清除，回落旧 float fps 近似（独立视频无
        metadata、或探测失败时的显式回退）。只影响显示，不发 seek。
        """
        self._timecode_formatter = formatter
        self._update_timecode()

    def _sync_preview_fps_indicator(self) -> None:
        """Propagate transport state to the owning preview toolbar."""
        host = self.parentWidget()
        toolbar = getattr(host, "viewport_menu", None)
        if toolbar is None:
            toolbar = getattr(host, "_toolbar", None)
        if toolbar is not None and hasattr(toolbar, "set_playback_state"):
            toolbar.set_playback_state(self.is_playing, self._fps)

    def set_enabled(self, enabled: bool) -> None:
        """Enable/disable controls."""
        self.btn_first.setEnabled(enabled)
        self.btn_prev.setEnabled(enabled)
        self.btn_play.setEnabled(enabled)
        self.btn_next.setEnabled(enabled)
        self.btn_last.setEnabled(enabled)
        self.slider.setEnabled(enabled)
        self.ruler_slider.setEnabled(enabled)
        self.btn_mark_in.setEnabled(enabled)
        self.btn_mark_out.setEnabled(enabled)

    def show_for_video(self) -> None:
        """Show the control bar for video mode."""
        self.setVisible(True)
        self.set_enabled(True)
        self._set_fusion_mode(False)
        # 默认隐藏 In/Out 控件（Color 页面不需要 I/O 范围）。
        self._set_io_visible(False)
        # 延迟更新位置，确保父组件已完成布局
        QTimer.singleShot(0, self._update_position_delayed)

    def show_for_video_with_io(self) -> None:
        """Show the control bar with In/Out range controls (Composite page).

        Behaves like show_for_video() but also reveals the I / O / Clear buttons
        so the user can mark a loop range for playback.
        """
        self.setVisible(True)
        self.set_enabled(True)
        self._set_fusion_mode(True)
        self._set_io_visible(True)
        QTimer.singleShot(0, self._update_position_delayed)

    def _set_fusion_mode(self, enabled: bool) -> None:
        """Switch between Color's compact bar and Composite's Fusion-style bar."""
        chrome = getattr(self, "_color_chrome", None)
        if chrome is not None:
            chrome.suspend_transport()
        self._fusion_mode = bool(enabled)
        self.ruler_slider.setVisible(self._fusion_mode)
        self.slider.setVisible(not self._fusion_mode)
        self.label_time.setVisible(not self._fusion_mode)
        self.label_range_in.setVisible(self._fusion_mode)
        self.label_range_out.setVisible(self._fusion_mode)
        self._transport_left_spacer.setVisible(self._fusion_mode)
        self._transport_right_spacer.setVisible(self._fusion_mode)
        height = 74 if self._fusion_mode else 36
        self.setFixedHeight(height)
        self.setMaximumHeight(height)
        self.label_frame.setFixedSize(68 if self._fusion_mode else 72, 22 if self._fusion_mode else 18)
        self.label_frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self._fusion_mode:
            self.label_frame.setObjectName("rangeFrameBox")
            self.label_frame.setStyleSheet(
                "background-color: #111215; border: 1px solid #36383e; "
                "border-radius: 3px; color: #ededed; font-weight: 600; font-size: 12px;"
            )
        else:
            self.label_frame.setObjectName("")
            self.label_frame.setStyleSheet("color: #aaa; font-size: 11px;")
        self._update_display()
        if chrome is not None and not self._fusion_mode:
            chrome.activate_transport()
        self.updateGeometry()

    def _set_io_visible(self, visible: bool) -> None:
        """Toggle visibility of the In/Out range button group."""
        self.btn_mark_in.setVisible(visible)
        self.btn_mark_out.setVisible(visible)
        self.btn_clear.setVisible(visible)
        self._io_separator.setVisible(visible)

    def set_in_out_points(self, in_point: int | None, out_point: int | None) -> None:
        """Update the In/Out playback range markers.

        Stored as frame numbers; the slider normalises them against its maximum.
        Passing (None, None) clears the markers.
        """
        self._in_point = in_point
        self._out_point = out_point
        self.slider.set_in_out_points(in_point, out_point)
        self.ruler_slider.set_in_out_points(in_point, out_point)
        self.update_in_out_state(in_point, out_point)

    def set_keyframe_frames(self, frames) -> None:
        """P2: mark keyframe frames on the Fusion ruler.

        Only the ruler slider (Composite mode) supports markers today; the
        Color-mode _InOutSlider ignores them. Forwarded from CompositePage.
        """
        frames = list(frames or [])
        if hasattr(self.ruler_slider, 'set_keyframe_frames'):
            self.ruler_slider.set_keyframe_frames(frames)

    def update_in_out_state(self, in_point: int | None, out_point: int | None) -> None:
        """Refresh the I/O button highlights and Clear enablement."""
        self._in_point = in_point
        self._out_point = out_point
        # Enable Clear when at least one point is set.
        has_any = in_point is not None or out_point is not None
        self.btn_clear.setEnabled(has_any)
        effective_in = 0 if in_point is None else int(in_point)
        effective_out = max(0, self.total_frames - 1) if out_point is None else int(out_point)
        self.label_range_in.setText(f"{float(effective_in):.1f}")
        self.label_range_out.setText(f"{float(effective_out):.1f}")
        # Highlight a set point with the orange theme colour.
        in_style = ("QToolButton { color: #ff8800; font-weight: bold; }"
                    if in_point is not None else "")
        out_style = ("QToolButton { color: #ff8800; font-weight: bold; }"
                     if out_point is not None else "")
        self.btn_mark_in.setStyleSheet(in_style)
        self.btn_mark_out.setStyleSheet(out_style)

    def _update_position_delayed(self) -> None:
        """Delayed position update after parent layout is complete."""
        if self.parent():
            self.parent().update_video_control_bar_position()

    def hide_for_image(self) -> None:
        """Hide the control bar for image mode."""
        self.setVisible(False)
        self.set_enabled(False)
        # Reset state
        self.set_playing(False)
        self.set_current_frame(0)
        self.set_total_frames(0)
        self._set_fusion_mode(False)
        # Clear In/Out range markers.
        self.set_in_out_points(None, None)

    def _update_display(self) -> None:
        """Update the display."""
        self._update_timecode()
        if self._fusion_mode:
            self.label_frame.setText(f"{float(self.current_frame):.1f}")
            self.update_in_out_state(self._in_point, self._out_point)
        else:
            self.label_frame.setText(f"{self.current_frame + 1} / {max(1, self.total_frames)}")

    def _update_timecode(self) -> None:
        """Render the current frame as Edit's HH:MM:SS:FF timecode.

        CV-B02：有精确域（Rational fps／起始 TC／DF）时用域格式化器，
        否则保持旧 float 近似。镜像 ``timecode_changed`` 只发最终字符串。
        """
        formatter = self._timecode_formatter
        if formatter is not None:
            try:
                text = formatter.format(int(self.current_frame))
                self.timecode_label.setText(text)
                self.timecode_changed.emit(text)
                return
            except Exception:
                # 显示层兜底：域格式化异常时退回近似路径，不中断时间码。
                pass
        fps = max(1.0, float(self._fps))
        total_seconds = max(0, int(self.current_frame)) / fps
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = int(total_seconds % 60)
        frames = int(self.current_frame % max(1, int(round(fps))))
        self.timecode_label.setText(
            f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"
        )
        self.timecode_changed.emit(self.timecode_label.text())

    def _on_loop_toggled(self, checked: bool) -> None:
        """Keep the loop icon state in sync with Edit's transport bar."""
        color = IconColors.ORANGE if checked else IconColors.DEFAULT
        self.btn_loop.setIcon(icon_loop(color))
        self.loop_toggled.emit(checked)

    def _active_slider(self) -> QSlider:
        sender = self.sender()
        if sender in (self.slider, self.ruler_slider):
            return sender
        return self.ruler_slider if self._fusion_mode else self.slider

    def _sync_slider_values(self, value: int, source: QSlider | None = None) -> None:
        for slider in (self.slider, self.ruler_slider):
            if slider is source:
                continue
            was_blocked = slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(was_blocked)

    def _on_slider_pressed(self) -> None:
        """Handle slider pressed."""
        self._slider_dragging = True
        slider = self._active_slider()
        self._sync_slider_values(slider.value(), slider)
        self.scrub_started.emit(slider.value())

    def _on_slider_released(self) -> None:
        """Handle slider released."""
        self._slider_dragging = False
        slider = self._active_slider()
        value = slider.value()
        self._sync_slider_values(value, slider)
        self.current_frame = value
        self._update_display()
        self.scrub_finished.emit(value)
        self.seek_requested.emit(value)

    def _on_slider_changed(self, value: int) -> None:
        """Handle slider value changed."""
        if self._slider_dragging:
            slider = self._active_slider()
            self._sync_slider_values(value, slider)
            self.current_frame = value
            self.label_frame.setText(f"{value + 1} / {max(1, self.total_frames)}")
            if self._fusion_mode:
                self._update_display()
            else:
                self._update_timecode()
            self.scrub_moved.emit(value)

    @pyqtSlot(bool)
    def on_play_state_changed(self, is_playing: bool):
        """Handle play state changed."""
        self.set_playing(is_playing)

    @pyqtSlot(int)
    def on_frame_changed(self, frame: int):
        """Handle frame changed."""
        self.set_current_frame(frame)



class ViewportControls(QWidget):
    """DEPRECATED: Floating toolbar for viewport controls. Use ViewportMenuBar instead."""
    mask_toggled = pyqtSignal(bool)
    split_toggled = pyqtSignal(bool)
    fit_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        self.setStyleSheet("""
            QWidget { 
                background-color: rgba(40, 40, 40, 180); 
                border-radius: 6px;
                border: 1px solid rgba(100, 100, 100, 100);
            }
            QPushButton { 
                background-color: transparent; 
                border: none; 
                border-radius: 4px;
                padding: 4px 10px; 
                color: #ccc; 
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton:hover { 
                background-color: rgba(255, 255, 255, 30); 
                color: white;
            }
            QPushButton:checked { 
                background-color: #ff8800; 
                color: white;
            }
            QPushButton#maskBtn:checked {
                background-color: #ff8800;
            }
        """)

        self.mask_btn = QPushButton(I18N.tr("canvas.mask", "遮罩"))
        self.mask_btn.setObjectName("maskBtn")
        self.mask_btn.setCheckable(True)
        self.mask_btn.setChecked(False)
        self.mask_btn.setToolTip(I18N.tr("tooltip.toggle_mask", "切换 Alpha 遮罩预览 (Shift+H)"))
        self.mask_btn.toggled.connect(self.mask_toggled.emit)
        layout.addWidget(self.mask_btn)

        self.fit_btn = QPushButton(I18N.tr("canvas.fit", "适合"))
        self.fit_btn.setToolTip(I18N.tr("tooltip.fit_window", "适合窗口 (0)"))
        self.fit_btn.clicked.connect(self.fit_requested.emit)
        layout.addWidget(self.fit_btn)

        self.split_btn = QPushButton(I18N.tr("canvas.split", "对比"))
        self.split_btn.setCheckable(True)
        self.split_btn.setToolTip(I18N.tr("tooltip.toggle_split", r"切换擦除对比 (\\)"))
        self.split_btn.toggled.connect(self.split_toggled.emit)
        layout.addWidget(self.split_btn)

class BrushCursorItem(QGraphicsItem):
    """Visual cursor for brush tool showing size and feather.
    
    Fixed pixel size that doesn't change with canvas zoom.
    Supports both brush and eraser modes.
    """
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._size = 50
        self._feather = 50
        self._is_eraser = False
        self._opacity = 1.0
        self.setZValue(1000)  # Always on top
        self.setAcceptHoverEvents(False)
        self.setEnabled(False)
        self.setVisible(False)
        
        # 关键：忽略变换，保持固定像素大小
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        
    def boundingRect(self):
        # 固定像素大小的边界矩形
        r = self._size + self._feather + 10
        return QRectF(-r, -r, 2*r, 2*r)
    
    def set_params(self, size: int, feather: int) -> None:
        """Update brush parameters and redraw."""
        self.prepareGeometryChange()
        self._size = size
        self._feather = feather
        self.update()
    
    def set_eraser_mode(self, is_eraser: bool) -> None:
        """Toggle between brush (add) and eraser (subtract) mode."""
        # 强制转换为布尔值，防止 KeyboardModifier 等 PyQt 对象被错误存储
        self._is_eraser = bool(is_eraser)
        self.update()
    
    def set_opacity(self, opacity: float) -> None:
        """Set opacity for visual preview."""
        self._opacity = opacity
        self.update()
    
    def paint(self, painter, option, widget) -> None:
        # 注意：由于设置了 ItemIgnoresTransformations，这里直接使用像素坐标绘制
        # 圆环大小固定，不会随画面缩放变化
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Choose colors based on mode
        if self._is_eraser:
            # Eraser mode: Blue/cyan colors
            main_color = QColor(0, 150, 255)
            fill_color = QColor(0, 150, 255, 30)
            feather_color = QColor(0, 150, 255, 100)
        else:
            # Brush mode: Orange colors
            main_color = QColor(255, 136, 0)
            fill_color = QColor(255, 136, 0, int(30 * self._opacity))
            feather_color = QColor(255, 136, 0, 100)
        
        # Draw outer feather circle (dashed)
        if self._feather > 0:
            outer_r = self._size + self._feather
            pen = QPen(feather_color, 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), outer_r, outer_r)
        
        # Draw filled preview (shows opacity)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(fill_color))
        painter.drawEllipse(QPointF(0, 0), self._size, self._size)
        
        # Draw main brush circle
        pen = QPen(main_color, 2.0)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(0, 0), self._size, self._size)
        
        # Draw center crosshair
        pen = QPen(main_color, 1)
        painter.setPen(pen)
        cross_size = min(8, self._size * 0.3)
        painter.drawLine(QPointF(-cross_size, 0), QPointF(cross_size, 0))
        painter.drawLine(QPointF(0, -cross_size), QPointF(0, cross_size))
        
        # Draw mode indicator (small icon in center)
        if self._size > 15:
            painter.setPen(QPen(main_color, 1.5))
            if self._is_eraser:
                # Draw "X" for eraser
                indicator_size = min(5, self._size * 0.15)
                painter.drawLine(QPointF(-indicator_size, -indicator_size), QPointF(indicator_size, indicator_size))
                painter.drawLine(QPointF(-indicator_size, indicator_size), QPointF(indicator_size, -indicator_size))


class ColorInspectorOverlay(QWidget):
    """Floating QWidget overlay that displays pixel color and coordinate info.

    Uses QWidget instead of QGraphicsItem to avoid OpenGL text rendering issues.
    """
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(170, 62)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.hide()

        self._rgb = (0, 0, 0)
        self._rgb_f = (0.0, 0.0, 0.0)
        self._pos = (0, 0)

    def update_info(self, rgb_int, rgb_float, pixel_pos) -> None:
        """Update displayed info. rgb_int=(R,G,B) 0-255, rgb_float=(R,G,B) 0-1, pixel_pos=(x,y)."""
        self._rgb = rgb_int
        self._rgb_f = rgb_float
        self._pos = pixel_pos
        self.update()

    def paintEvent(self, event) -> None:
        from PyQt6.QtGui import QFont
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(30, 30, 30, 220))
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 4, 4)

        # Border
        painter.setPen(QPen(QColor(80, 80, 80, 180), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 4, 4)

        r, g, b = self._rgb
        rf, gf, bf = self._rgb_f

        # Color swatch
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(r, g, b))
        painter.drawRoundedRect(6, 5, 14, 14, 2, 2)

        # Text
        painter.setPen(QColor(210, 210, 210))
        font = QFont("Menlo, Monaco, monospace", 9)
        painter.setFont(font)

        x_text = 26
        painter.drawText(x_text, 15, f"R:{r:>3d}  ({rf:.3f})")
        painter.drawText(x_text, 29, f"G:{g:>3d}  ({gf:.3f})")
        painter.drawText(x_text, 43, f"B:{b:>3d}  ({bf:.3f})")

        # Position at bottom
        painter.setPen(QColor(140, 140, 140))
        painter.drawText(6, 57, f"Pos: {self._pos[0]}, {self._pos[1]}")

        painter.end()


class BrushStrokePreviewItem(QGraphicsItem):
    """Real-time brush stroke preview with curve smoothing.
    
    Uses midpoint quadratic Bezier smoothing to eliminate jagged edges
    during fast drawing, while maintaining performance with lightweight QPen rendering.
    """
    def __init__(self) -> None:
        super().__init__()
        self.path = QPainterPath()
        self.scene_size = 50.0
        self.is_eraser = False
        self.show_overlay = True
        # Store raw points for smoothing
        self._raw_points: list = []

    def boundingRect(self):
        # 使用真实的 scene_size 作为边距，防止笔刷过大时边缘被意外裁剪
        margin = self.scene_size + 10
        return self.path.boundingRect().adjusted(-margin, -margin, margin, margin)

    def set_show_overlay(self, show) -> None:
        self.show_overlay = show
        self.update()

    def start_stroke(self, pos, scene_size, is_eraser=False) -> None:
        self.scene_size = scene_size
        self.is_eraser = is_eraser
        self._raw_points = [(pos[0], pos[1])]
        self.prepareGeometryChange()
        self.path = QPainterPath()
        self.path.moveTo(pos[0], pos[1])
        self.update()

    def add_points(self, points) -> None:
        """Add points with midpoint Bezier smoothing.
        
        Algorithm: For each pair of consecutive points, use their midpoint
        as the target and the original point as control point for quadTo().
        This creates smooth curves even with widely spaced sample points.
        """
        if not points: 
            return
        
        self._raw_points.extend(points)
        self._rebuild_smooth_path()
        self.update()

    def _rebuild_smooth_path(self) -> None:
        """Rebuild the path with midpoint Bezier smoothing."""
        if not self._raw_points:
            return
            
        self.prepareGeometryChange()
        self.path = QPainterPath()
        pts = self._raw_points
        
        if len(pts) == 1:
            # Single point - draw a minimal dot
            self.path.moveTo(pts[0][0], pts[0][1])
            self.path.lineTo(pts[0][0] + 0.01, pts[0][1])
        elif len(pts) == 2:
            # Two points - direct line
            self.path.moveTo(pts[0][0], pts[0][1])
            self.path.lineTo(pts[1][0], pts[1][1])
        else:
            # Three or more points - apply midpoint smoothing
            self.path.moveTo(pts[0][0], pts[0][1])
            
            # For each segment, use midpoint as target, current point as control
            for i in range(len(pts) - 1):
                p0 = pts[i]
                p1 = pts[i + 1]
                
                # Calculate midpoint
                mid_x = (p0[0] + p1[0]) / 2.0
                mid_y = (p0[1] + p1[1]) / 2.0
                
                # Use quadratic Bezier: control point is p0, endpoint is midpoint
                self.path.quadTo(p0[0], p0[1], mid_x, mid_y)
            
            # Final segment to the last point
            last = pts[-1]
            self.path.lineTo(last[0], last[1])

    def clear_stroke(self) -> None:
        """Clear the current stroke preview."""
        self.prepareGeometryChange()
        self.path = QPainterPath()
        self._raw_points = []
        self.update()

    def paint(self, painter, option, widget) -> None:
        if self.path.isEmpty() or not self.show_overlay:
            return
        
        pen = QPen()
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        
        base_color = QColor(0, 150, 255) if self.is_eraser else QColor(255, 0, 0)
        base_color.setAlpha(180)
        pen.setColor(base_color)
        
        # 使用锁定的精确场景尺寸，避免反复计算缩放差
        pen.setWidthF(self.scene_size)
        painter.strokePath(self.path, pen)


class SAMPointItem(QGraphicsItem):
    """Visual marker for SAM point clicks - fixed pixel size regardless of zoom.
    
    Green circle with '+' for foreground points (label=1).
    Red circle with '-' for background points (label=0).
    
    Supports hover events for visual feedback and right-click deletion.
    """
    def __init__(self, is_foreground: bool = True) -> None:
        super().__init__()
        self._is_foreground = is_foreground
        self._radius = 12  # Fixed pixel radius
        
        # Always on top, ignore transformations for fixed pixel size
        self.setZValue(1000)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        
        # Enable hover events for cursor feedback
        self.setAcceptHoverEvents(True)
        
    def boundingRect(self) -> QRectF:
        r = self._radius + 2  # Small margin for pen width
        return QRectF(-r, -r, 2 * r, 2 * r)
    
    def hoverEnterEvent(self, event) -> None:
        """Change cursor to pointing hand when hovering over a SAM point."""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        super().hoverEnterEvent(event)
    
    def hoverLeaveEvent(self, event) -> None:
        """Restore cursor when leaving a SAM point."""
        # The canvas will restore the appropriate cursor based on mode
        self.unsetCursor()
        super().hoverLeaveEvent(event)
    
    def paint(self, painter, option, widget) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Colors based on point type
        if self._is_foreground:
            # Green for foreground (add)
            main_color = QColor(0, 200, 80)
            fill_color = QColor(0, 200, 80, 60)
        else:
            # Red for background (subtract)
            main_color = QColor(255, 60, 60)
            fill_color = QColor(255, 60, 60, 60)
        
        r = self._radius
        
        # Draw filled circle
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(fill_color))
        painter.drawEllipse(QPointF(0, 0), r, r)
        
        # Draw circle outline
        painter.setPen(QPen(main_color, 2.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(0, 0), r, r)
        
        # Draw center symbol (+ or -)
        painter.setPen(QPen(main_color, 2.0))
        cross_size = r * 0.5
        painter.drawLine(QPointF(-cross_size, 0), QPointF(cross_size, 0))
        if self._is_foreground:
            # Only draw vertical line for '+' symbol
            painter.drawLine(QPointF(0, -cross_size), QPointF(0, cross_size))


class Canvas(QGraphicsView):
    scopes_requested = pyqtSignal()
    stroke_finished = pyqtSignal(list, tuple, int)
    mask_shape_changed = pyqtSignal(str, dict)
    mask_shape_selected = pyqtSignal(str)
    color_picked = pyqtSignal(float, float, float, bool) # h, s, v(HSV), is_first_pick
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    sam_point_clicked = pyqtSignal(float, float, int) # x, y (0-1), label (0/1)
    crop_rotation_changed = pyqtSignal(float)
    crop_apply_requested = pyqtSignal()
    brush_stroke_added = pyqtSignal(list, dict)  # points, params
    brush_params_updated = pyqtSignal(dict)  # params
    # HSL Curves Picker signal - sends RGB (0-255) for adding control points
    hsl_color_for_curves = pyqtSignal(float, float, float)  # r, g, b (0-255)
    # RGB Curves Picker signal - sends RGB (0-255) for adding control points
    curves_color_for_curves = pyqtSignal(float, float, float)  # r, g, b (0-255)
    # Canvas context menu signal - right-click on image
    canvas_context_menu = pyqtSignal(object)  # QPoint (global position)
    # P1-COLOR-21: Auto Balance 请求（viewport 菜单转发，逻辑在控制器）
    auto_color_requested = pyqtSignal()
    # P1-COLOR-19: Color workspace 预设（viewport 菜单转发，逻辑在控制器）
    color_workspace_requested = pyqtSignal(str)
    color_workspace_save_requested = pyqtSignal()
    color_workspace_delete_requested = pyqtSignal()
    # Tracker overlay 提交信号（拖动释放才发出，归一化区域）
    tracker_pattern_committed = pyqtSignal(tuple)
    tracker_search_committed = pyqtSignal(tuple)
    tracker_current_committed = pyqtSignal(tuple)
    # 透视模式四角手动修正（归一化四角，顺序 TL/TR/BR/BL）
    tracker_homography_corners_committed = pyqtSignal(list)
    tracker_failed_marker_clicked = pyqtSignal(int)  # frame
    tracker_feature_selected = pyqtSignal(int)
    tracker_feature_inserted = pyqtSignal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.engine = None
        self.active_shape_id = None
        self.setAcceptDrops(False)
        
        # Remove frame border
        self.setFrameStyle(QGraphicsView.Shape.NoFrame)
        self.setContentsMargins(0, 0, 0, 0)
        
        # Middle mouse button panning state
        self._is_panning = False
        self._pan_start_pos = None
        
        # Space key + left mouse button panning state (Task 3)
        self._space_pressed = False
        
        # Mask overlay visibility state (Task 1)
        # 默认开启：窗口形状属于视图状态而非 Masks 面板状态，切换到
        # Tracker 等其他面板时必须保持可见（否则无法选形/调跟踪位置）。
        # 用户仍可通过视口菜单的 mask 开关显式关闭。
        self._show_mask_overlays = True
        self._video_window_mode = False
        
        # Grid and safe area visibility states


        # P4-2 SAFETY: Flag to detect render_pipeline calls during drawBackground
        # If render_pipeline is called while this flag is True, it's a bug
        self._in_draw_background = False
        
        self.gl_viewport = GLCanvasViewport()
        self.setViewport(self.gl_viewport)
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.pixmap_item = QGraphicsPixmapItem()
        self.scene.addItem(self.pixmap_item)
        self._timeline_preview_active = False
        self._timeline_preview_needs_fit = False
        self.logical_size = None 
        self._display_scene_size_override: Optional[Tuple[int, int]] = None
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # DaVinci 级别视觉优化：OpenGL 视口下使用全量更新，避免 CPU 计算脏矩形开销
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        # Match the Color viewer's #191919 native letterbox surround. This is
        # UI backdrop styling; the rendered media's color values are untouched.
        self.setBackgroundBrush(QBrush(QColor(25, 25, 25)))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        
        # Pen tool state
        self.is_drawing = False
        self._mask_creation_mode = "curve"
        self.is_picking = False
        self.inspector_mode_active = False
        self.hsl_picker_mode = False  # HSL Curves color picker mode
        self.curves_picker_mode = False  # RGB Curves color picker mode
        self.pen_dragging = False
        self.pen_start_pos = None
        
        self.mask_items = {}
        self.current_interaction_type = "general"
        self._needs_initial_fit = False

        # Viewer guide overlay
        self._guide_overlay = None
        self._current_guide_id = None

        # Viewport Menu Bar (replaces floating controls)
        self.viewport_menu = ViewportMenuBar(self)
        self.viewport_menu.mask_toggled.connect(self._on_mask_preview_toggled)
        self.viewport_menu.split_toggled.connect(self.set_split_view_mode)
        # P1-COLOR-05: A/B 比较 / reference wipe（显示层，不改工程状态）
        self.viewport_menu.ab_toggle_requested.connect(self.toggle_ab_view)
        self.viewport_menu.capture_reference_requested.connect(
            self.capture_reference_wipe)
        self.viewport_menu.clear_reference_requested.connect(
            self.clear_reference_wipe)
        self.viewport_menu.auto_color_requested.connect(
            self.auto_color_requested.emit)
        # P1-COLOR-19: workspace 预设（转发到控制器，菜单内容由 provider 提供）
        self.viewport_menu.workspace_selected.connect(
            self.color_workspace_requested.emit)
        self.viewport_menu.save_workspace_requested.connect(
            self.color_workspace_save_requested.emit)
        self.viewport_menu.delete_workspace_requested.connect(
            self.color_workspace_delete_requested.emit)
        self.viewport_menu.fit_requested.connect(self.fit_to_window)
        self.viewport_menu.guide_changed.connect(self._on_guide_changed)
        
        # DEPRECATED: Keep for backward compatibility, but hide by default
        self.controls = ViewportControls(self)
        self.controls.setVisible(False)
        self.controls.mask_toggled.connect(self._on_mask_preview_toggled)
        self.controls.split_toggled.connect(self.set_split_view_mode)
        self.controls.fit_requested.connect(self.fit_to_window)

        self.is_dragging_split = False
        self.sam_mode_active = False
        self.crop_item = None
        
        # Brush tool state
        self.brush_mode_active = False
        self.brush_cursor = BrushCursorItem()
        self.scene.addItem(self.brush_cursor)
        self.brush_cursor.setVisible(False)

        # Color inspector overlay (QWidget, not QGraphicsItem, to avoid OpenGL text issues)
        self.color_inspector = ColorInspectorOverlay(self.viewport())
        self.color_inspector.hide()

        # P1-COLOR-22: 色彩监看状态栏（viewer 底部常驻）
        from src.features.color.ui.color_status_bar import ColorMonitorStatusBar
        self.color_status_bar = ColorMonitorStatusBar(self.viewport())
        self.color_status_bar.hide()
        # P1-COLOR-13: 降噪档位探测（renderer 记录最近实际使用的质量档）
        self.color_status_bar.set_denoise_quality_provider(
            self._probe_denoise_quality)
        
        # Brush stroke preview (red overlay)
        self.brush_stroke_preview = BrushStrokePreviewItem()
        self.scene.addItem(self.brush_stroke_preview)
        self.brush_stroke_preview.setVisible(False)

        # Tracker overlay（独立图层，不向蒙版图形写入跟踪点）
        self.tracker_overlay = TrackerOverlay()
        self.scene.addItem(self.tracker_overlay)
        self.tracker_overlay.setVisible(False)
        self.tracker_overlay.pattern_region_committed.connect(
            self.tracker_pattern_committed.emit)
        self.tracker_overlay.search_region_committed.connect(
            self.tracker_search_committed.emit)
        self.tracker_overlay.current_region_committed.connect(
            self.tracker_current_committed.emit)
        self.tracker_overlay.homography_corners_committed.connect(
            self.tracker_homography_corners_committed.emit)
        self.tracker_overlay.failed_marker_clicked.connect(
            self.tracker_failed_marker_clicked.emit)
        self.tracker_overlay.feature_selected.connect(self.tracker_feature_selected.emit)
        self.tracker_overlay.feature_inserted.connect(self.tracker_feature_inserted.emit)
        
        self._brush_stroke_points = []  # Current stroke points
        self._brush_params = {"size": 50, "feather": 50, "opacity": 1.0, "flow": 1.0}
        # AUD-25213: set_brush_params 的防递归状态锁（外部驱动更新不再回发）
        self._brush_params_syncing = False
        
        # SAM point visual markers
        self.sam_points_ui: List[SAMPointItem] = []
        
        # SAM ready state (prevents clicks before encoding is complete)
        self.sam_ready = False
        
        # Crop rotation preview angle for GPU rendering
        self._crop_preview_angle = 0.0

        # Video Control Bar (shown only for videos)
        self.video_control_bar = VideoControlBar(self)
        self.video_control_bar.setVisible(False)  # Initially hidden
        from src.features.color.ui.color_viewport_chrome import ColorViewportChrome
        self.color_viewport_chrome = ColorViewportChrome(self)

    # ================================================================
    # Tracker overlay 控制（阶段 4）
    # ================================================================

    def show_tracker_overlay(self, width: float, height: float,
                             pattern_region=None, search_region=None) -> None:
        """激活 Tracker 图层（同步帧尺寸与初始区域）。"""
        self.tracker_overlay.set_frame_size(width, height)
        self.tracker_overlay.set_pattern_region(pattern_region)
        self.tracker_overlay.set_search_region(search_region)
        self.tracker_overlay.set_overlay_active(True)
        if self.tracker_overlay.state == OverlayState.IDLE:
            self.tracker_overlay.set_state(OverlayState.TRACKER_READY)

    def hide_tracker_overlay(self) -> None:
        """隐藏 Tracker 图层并重置交互状态（数据保留，供下次快速恢复）。"""
        self.tracker_overlay.set_overlay_active(False)
        self.tracker_overlay.set_state(OverlayState.IDLE)
        self.tracker_overlay.set_features([])
        self.tracker_overlay.set_path([])
        self.tracker_overlay.set_failed_markers({})
        self.tracker_overlay.set_current_transform(None)

    def set_display_scene_size_override(self, size: Optional[Tuple[int, int]]) -> None:
        """Override the viewer scene size while keeping the source texture intact."""
        if size is None:
            if self._display_scene_size_override is None:
                return
            self._display_scene_size_override = None
            # A timeline override may have been applied while this same video
            # was already loaded.  Restore the source display geometry now,
            # rather than waiting for a subsequent upload; otherwise playback
            # keeps rendering into the old (and possibly ultra-wide) FBO.
            source_size = None
            if self.engine and self.engine.img_full_original is not None:
                src_h, src_w = self.engine.img_full_original.shape[:2]
                source_size = (int(src_w), int(src_h))
            elif self.engine and getattr(self.engine, "renderer", None) is not None:
                src_w, src_h = getattr(self.engine.renderer, "video_source_size", (0, 0))
                if src_w > 0 and src_h > 0:
                    source_size = (int(src_w), int(src_h))
            if source_size is not None:
                width, height = source_size
                self.logical_size = QRectF(0, 0, width, height).size()
                self.scene.setSceneRect(QRectF(0, 0, width, height))
                if self.engine:
                    self.engine.scene_size = (float(width), float(height))
                if self._guide_overlay:
                    self._guide_overlay.update_image_size(float(width), float(height))
                self._update_scene_pan_bounds()
                self._update_lod()
            return

        width, height = int(size[0]), int(size[1])
        if width <= 0 or height <= 0:
            self._display_scene_size_override = None
            return

        new_size = (width, height)
        if self._display_scene_size_override == new_size:
            return
        self._display_scene_size_override = new_size

        self.logical_size = QRectF(0, 0, width, height).size()
        self.scene.setSceneRect(QRectF(0, 0, width, height))
        if self.engine:
            self.engine.scene_size = (float(width), float(height))
        if self._guide_overlay:
            self._guide_overlay.update_image_size(float(width), float(height))
        self._update_scene_pan_bounds()
        self._update_lod()

    def _display_scene_size_for_source(self, source_w: int, source_h: int) -> Tuple[int, int]:
        override = self._display_scene_size_override
        if override and override[0] > 0 and override[1] > 0:
            return override
        return int(source_w), int(source_h)

    def set_image(self, cv_image, is_new_source=False) -> None:
        if cv_image is None: return
        if self.engine and self.engine.img_full_original is not None:
            h, w = self.engine.img_full_original.shape[:2]
        else:
            h, w = cv_image.shape[:2]
        display_w, display_h = self._display_scene_size_for_source(w, h)
            
        # Check for dimension changes to ensure scene rect is always correct (e.g. after crop or undo)
        size_changed = False
        if self.logical_size:
            if self.logical_size.width() != display_w or self.logical_size.height() != display_h:
                size_changed = True

        if is_new_source or self.logical_size is None or size_changed:
            self.logical_size = QRectF(0, 0, display_w, display_h).size()
            self.scene.setSceneRect(QRectF(0, 0, display_w, display_h))
            if self.engine:
                self.engine.scene_size = (float(display_w), float(display_h))
            # Update guide overlay dimensions
            if self._guide_overlay:
                self._guide_overlay.update_image_size(float(display_w), float(display_h))
            
        # A Color timeline preview is a completed Edit frame.  Keep it above
        # the source renderer while the latter hydrates the selected clip;
        # otherwise a late source-frame repaint can flash through it.
        if not getattr(self, "_timeline_preview_active", False):
            self.pixmap_item.setVisible(False)
        if is_new_source:
            self.fit_to_window()
            if self.viewport().width() <= 100:
                self._needs_initial_fit = True
            # Sync menu states after loading new image
            if hasattr(self, 'viewport_menu'):
                self.viewport_menu.sync_menu_state()
        self._update_scene_pan_bounds()
        self._update_lod()
        if self.engine:
            self.engine.render_pipeline()
        self.viewport().update()

    def set_timeline_preview_image(self, image: QImage) -> None:
        """Present a completed Edit timeline preview without re-decoding it.

        The Color page uses this while timeline transport is playing.  The
        image comes from ``TimelinePreviewRenderer`` after its generation and
        playback-order guards, so a stale source decoder frame cannot replace
        the visible timeline frame between clips.
        """
        if image is None or image.isNull():
            return
        self._timeline_preview_active = True
        pixmap = QPixmap.fromImage(image)
        width, height = pixmap.width(), pixmap.height()
        if width <= 0 or height <= 0:
            return
        display_w, display_h = self._display_scene_size_for_source(width, height)
        if (
            self.logical_size is None
            or self.logical_size.width() != display_w
            or self.logical_size.height() != display_h
        ):
            self.logical_size = QRectF(0, 0, display_w, display_h).size()
            self.scene.setSceneRect(QRectF(0, 0, display_w, display_h))
            self._update_scene_pan_bounds()
            self._update_lod()
        self.pixmap_item.setPixmap(pixmap)
        self.pixmap_item.setPos(0.0, 0.0)
        # Fit the view BEFORE revealing the pixmap.  Showing it first left one
        # repaint where the new pixmap was laid out under a stale view transform
        # (often identity after the previous clip, or a leftover pan/zoom), which
        # read as a quick "small → large" zoom snap right after pressing play.
        if getattr(self, "_timeline_preview_needs_fit", False):
            # A previous source frame can leave a pan offset in the
            # QGraphicsView transform.  A timeline frame always opens fitted
            # and centred, rather than inheriting that offset.
            self._timeline_preview_needs_fit = False
            self.fit_to_window()
        self.pixmap_item.setVisible(True)
        self.viewport().update()

    def begin_timeline_preview(self) -> None:
        """Hide source-GL output until a complete timeline frame is ready.

        The timeline preview is painted by the QGraphics scene.  Its source
        renderer may still be loading the newly selected clip in the
        background, so it must never be composited with that renderer's last
        front texture.
        """
        self._timeline_preview_active = True
        self._timeline_preview_needs_fit = True
        self.pixmap_item.setVisible(False)
        self.viewport().update()

    def end_timeline_preview(self) -> None:
        """Return the canvas to the normal Color OpenGL presentation path."""
        self._timeline_preview_active = False
        self._timeline_preview_needs_fit = False
        self.pixmap_item.setVisible(False)
        self.viewport().update()

    def _update_lod(self):
        chrome = getattr(self, "color_viewport_chrome", None)
        if chrome is not None:
            chrome.update_zoom(self.transform().m11())
        if not self.engine or not self.logical_size: return False
        changed = False
        self.engine.scene_size = (float(self.logical_size.width()), float(self.logical_size.height()))
        scale = self.transform().m11()
        lod = 0
        if scale > 0:
            lod = max(0, int(math.log2(1.0 / scale + 0.1)))
            if self.engine.current_lod != lod:
                self.engine.current_lod = lod
                changed = True
        view_rect = self.viewport().rect()
        dpr = self.devicePixelRatio()
        v_size = (int(view_rect.width() * dpr), int(view_rect.height() * dpr))
        v_size = self._monitor_synced_viewport_size(v_size)

        if self.engine.viewport_size != v_size:
            self.engine.viewport_size = v_size
            changed = True
        scene_tl = self.mapToScene(view_rect.topLeft())
        scene_br = self.mapToScene(view_rect.bottomRight())
        v_rect = (scene_tl.x(), scene_tl.y(), scene_br.x() - scene_tl.x(), scene_br.y() - scene_tl.y())
        def rect_changed(r1, r2, tol=1e-3):
            return any(abs(a - b) > tol for a, b in zip(r1, r2))
        if rect_changed(self.engine.visible_rect, v_rect):
            self.engine.visible_rect = v_rect
            changed = True
        return changed

    def _monitor_synced_viewport_size(self, size: Tuple[int, int]) -> Tuple[int, int]:
        """Use a monitor-quality render target without changing viewport aspect."""
        width = max(1, int(size[0]))
        height = max(1, int(size[1]))
        try:
            window = self.window()
            config_manager = getattr(window, "config_manager", None)
            if config_manager is None:
                return width, height
            from src.shared.video.monitor_output import DeckLinkMonitorConfig

            config = DeckLinkMonitorConfig.from_config(config_manager)
        except Exception:
            logger.debug('DeckLink config unavailable, falling back', exc_info=True)
            return width, height
        if not getattr(config, "enabled", False):
            return width, height
        target_w = max(1, int(getattr(config, "width", 0) or 0))
        target_h = max(1, int(getattr(config, "height", 0) or 0))
        scale = max(1.0, target_w / float(width), target_h / float(height))
        return max(1, int(round(width * scale))), max(1, int(round(height * scale)))

    def _update_scene_pan_bounds(self, new_scale_factor: float = 1.0) -> None:
        """Expand scene bounds so small images/videos can be panned freely.

        Args:
            new_scale_factor: If provided, compute padding as if the transform
                will be multiplied by this factor.  This ensures the sceneRect
                is large enough BEFORE scale() is called, so that
                AnchorUnderMouse's internal centerOn() has a valid scroll range.
        """
        if not self.logical_size:
            return
        scale = abs(self.transform().m11()) * abs(new_scale_factor)
        if scale <= 1e-6:
            scale = 1.0
        view_rect = self.viewport().rect()
        img_w = float(self.logical_size.width())
        img_h = float(self.logical_size.height())
        view_w_scene = float(view_rect.width()) / scale if view_rect.width() > 0 else img_w
        view_h_scene = float(view_rect.height()) / scale if view_rect.height() > 0 else img_h
        pad_x = max(img_w * 4.0, view_w_scene * 2.0, 2048.0)
        pad_y = max(img_h * 4.0, view_h_scene * 2.0, 2048.0)
        target = QRectF(-pad_x, -pad_y, img_w + pad_x * 2.0, img_h + pad_y * 2.0)
        current = self.scene.sceneRect()
        if current.contains(target):
            return
        target = current.united(target)
        if (
            abs(current.x() - target.x()) > 0.5
            or abs(current.y() - target.y()) > 0.5
            or abs(current.width() - target.width()) > 0.5
            or abs(current.height() - target.height()) > 0.5
        ):
            self.scene.setSceneRect(target)

    def fit_to_window(self) -> None:
        if not self.logical_size: return
        view_rect = self.viewport().rect()
        if view_rect.width() <= 10 or view_rect.height() <= 10: return
        img_w, img_h = self.logical_size.width(), self.logical_size.height()
        # Color's transport overlays the QGraphicsView. Reserve its visible
        # height while fitting, without changing the shared GL viewport size.
        bar = getattr(self, "video_control_bar", None)
        reserved = bar.height() if (
            bar is not None and not bar.isHidden()
            and getattr(bar, "_color_chrome", None) is not None
            and not bar._fusion_mode
        ) else 0
        usable_height = max(1, view_rect.height() - reserved)
        scale = min(view_rect.width() / img_w, usable_height / img_h)
        self.resetTransform()
        self.scale(scale, scale)
        # P4-3: 先 centerOn，后扩展 pan bounds
        # 确保 centerOn 时 sceneRect 已包含目标中心点，避免跳位
        if reserved:
            self._update_scene_pan_bounds()
            self.centerOn(img_w / 2, img_h / 2 + reserved / (2.0 * scale))
        else:
            self.centerOn(img_w / 2, img_h / 2)
            self._update_scene_pan_bounds()
        if self._update_lod():
            if not self._refresh_video_frame_view():
                if self.engine:
                    self.engine.render_pipeline(render_reason='viewport_refresh')
        self.viewport().update()

    def _center_on_scene_point_without_drift(
        self, scene_point: QPointF, viewport_point: QPoint
    ) -> None:
        """Center on a scene point while compensating for scrollbar rounding.

        QGraphicsView stores its hidden scroll-bar positions as integers.  A
        plain ``centerOn`` can therefore leave the viewport one scroll-bar
        unit away from the requested point; repeating that during zoom makes
        the error accumulate and looks like a horizontal pan.  Choose the
        nearest valid horizontal and vertical positions after ``centerOn`` so
        every wheel event is anchored to the same actual scene point.
        """
        self.centerOn(scene_point)

        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()

        horizontal_value = horizontal.value()
        horizontal_candidates = range(
            max(horizontal.minimum(), horizontal_value - 2),
            min(horizontal.maximum(), horizontal_value + 2) + 1,
        )
        best_horizontal = horizontal_value
        best_horizontal_error = float("inf")
        for value in horizontal_candidates:
            horizontal.setValue(value)
            actual = self.mapToScene(viewport_point)
            error = abs(actual.x() - scene_point.x())
            if error < best_horizontal_error:
                best_horizontal = value
                best_horizontal_error = error
        horizontal.setValue(best_horizontal)

        vertical_value = vertical.value()
        vertical_candidates = range(
            max(vertical.minimum(), vertical_value - 2),
            min(vertical.maximum(), vertical_value + 2) + 1,
        )
        best_vertical = vertical_value
        best_vertical_error = float("inf")
        for value in vertical_candidates:
            vertical.setValue(value)
            actual = self.mapToScene(viewport_point)
            error = abs(actual.y() - scene_point.y())
            if error < best_vertical_error:
                best_vertical = value
                best_vertical_error = error
        vertical.setValue(best_vertical)

    def _refresh_video_frame_view(self):
        """Lightweight viewport refresh during video playback.

        Re-renders the current GPU texture with updated viewport params
        (zoom, pan, visible rect) without going through the full
        engine.render_pipeline().  This avoids expensive decode + GPU upload
        and prevents render-queue accumulation during interactive zoom/resize.

        Returns True if the lightweight refresh was performed, False otherwise
        (e.g. no video controller, no loaded video, or texture unavailable).
        """
        window = self.window()
        if window is None:
            return False
        vc = getattr(window, 'video_controller', None)
        if vc is None:
            return False
        if hasattr(vc, 'is_video_loaded') and not vc.is_video_loaded():
            return False
        try:
            return vc.refresh_current_frame_view()
        except Exception:
            logger.debug('refresh_current_frame_view failed', exc_info=True)
            return False

    def update_color_monitor(self, source: dict | None = None,
                             working: str | None = None,
                             display: str | None = None,
                             provider_diag: dict | None = None,
                             decode_path: str | None = None) -> None:
        """更新并显示色彩监看状态栏（P1-COLOR-22）。"""
        bar = getattr(self, 'color_status_bar', None)
        if bar is None:
            return
        if isinstance(source, dict):
            bar.set_source_state(
                transfer=source.get('transfer'),
                primaries=source.get('primaries'),
                range_tag=source.get('range'))
        if working is not None:
            bar.set_working_space(working)
        if display is not None:
            bar.set_display_space(display)
        if provider_diag is not None:
            bar.set_provider_diagnostics(provider_diag)
        if decode_path is not None:
            bar.set_decode_path(decode_path)
        bar.show()
        self.update_video_control_bar_position()
        bar.raise_()

    def _probe_denoise_quality(self) -> str | None:
        """P1-COLOR-13: 读取 renderer 最近一次降噪档位（供状态栏轮询）。"""
        engine = getattr(self, 'engine', None)
        renderer = getattr(engine, 'renderer', None)
        if renderer is None:
            return None
        pipeline = getattr(renderer, 'video_denoise_pipeline', None)
        if pipeline is None or not getattr(pipeline, 'is_active', lambda: False)():
            return None
        return getattr(renderer, 'last_video_denoise_quality', None)

    def capture_reference_wipe(self) -> None:
        """抓当前调色结果为参考图并开启 wipe（P1-COLOR-05）。"""
        if self.engine is None:
            return
        self.engine.capture_reference_still()
        self._sync_reference_menu_states()

    def clear_reference_wipe(self) -> None:
        """清除参考图，wipe 左侧回退为未调色原图。"""
        if self.engine is None:
            return
        self.engine.clear_reference_still()
        self._sync_reference_menu_states()

    def toggle_ab_view(self) -> None:
        """A/B 全屏切换（参考/原图 ↔ 当前）。"""
        if self.engine is None:
            return
        self.engine.toggle_ab_view()
        self._sync_reference_menu_states()

    def set_compare_mode(self, mode: str) -> None:
        """F4.3: 对比模式（"split" wipe / "difference" 全屏差值）。"""
        if self.engine is None:
            return
        set_mode = getattr(self.engine, "set_compare_mode", None)
        if set_mode is not None:
            set_mode(mode)
            self._sync_reference_menu_states()

    def _sync_reference_menu_states(self) -> None:
        """把 reference/A-B 状态同步到菜单可用性与提示。"""
        menu = getattr(self, 'viewport_menu', None)
        if menu is None or self.engine is None:
            return
        has_still = bool(getattr(self.engine, 'reference_is_still', False))
        try:
            menu.clear_reference_act.setEnabled(has_still)
            split_label = (I18N.tr("viewport.split", "Split View")
                           + (f" · {I18N.tr('viewport.reference_still', 'Reference')}"
                              if has_still else
                              f" · {I18N.tr('viewport.original', 'Original')}"))
            menu.split_act.setText(split_label)
        except Exception:
            logger.debug('split label update failed', exc_info=True)

    def refresh_color_workspace_menu(self) -> None:
        """立即重建 View > Workspace 菜单（P1-COLOR-19）。"""
        menu = getattr(self, 'viewport_menu', None)
        if menu is None or not hasattr(menu, 'refresh_workspace_menu'):
            return
        menu.refresh_workspace_menu()

    def set_split_view_mode(self, enabled) -> None:
        if self.engine:
            self.engine.set_split_view_mode(enabled)
        self.viewport().update()

    def _on_mask_preview_toggled(self, checked: bool) -> None:
        """P1-COLOR-08: viewport 菜单 mask toggle → engine + qualifier 面板按钮同步."""
        if self.engine:
            self.engine.set_mask_preview_mode(checked)
        window = self.window()
        panel = getattr(window, 'qualifier_panel', None)
        if panel is not None and hasattr(panel, 'set_matte_preview_active'):
            style = getattr(self.engine, 'mask_preview_style', 'highlight') if self.engine else 'highlight'
            panel.set_matte_preview_active(checked, style)

    def _on_guide_changed(self, guide_type: str, guide_id: str, params: dict) -> None:
        """Handle guide selection from the ViewerGuidesPanel."""
        from src.features.color.ui.viewer_guides import (
            GuideOverlayItem, GuideDefinition, GuideType,
        )

        # Clear guide if "default" selected
        if guide_id == "default":
            if self._guide_overlay:
                self._guide_overlay.set_guide(None)
                self._guide_overlay.setVisible(False)
            self._current_guide_id = None
            self.viewport().update()
            return

        # Create overlay if not exists
        if self._guide_overlay is None:
            if not self.logical_size:
                return
            self._guide_overlay = GuideOverlayItem(
                self.logical_size.width(), self.logical_size.height()
            )
            self.scene.addItem(self._guide_overlay)

        # Update image size
        if self.logical_size:
            self._guide_overlay.update_image_size(
                self.logical_size.width(), self.logical_size.height()
            )

        # Build GuideDefinition from signal data
        gt = GuideType(guide_type)
        guide = GuideDefinition(
            id=guide_id,
            label=guide_id,
            guide_type=gt,
            value=params.get("ratio", params.get("inset", 0.0)),
            secondary_value=params.get("secondary", 0.0),
        )

        self._guide_overlay.set_guide(guide)
        self._guide_overlay.setVisible(True)
        self._current_guide_id = guide_id
        self.viewport().update()

    def set_tool_mode(self, mode) -> None:
        """Switch between different tool modes."""
        # Reset all tool states
        self.brush_mode_active = False
        self.is_picking = False
        self.inspector_mode_active = False
        self.color_inspector.hide()
        self.hsl_picker_mode = False
        self.curves_picker_mode = False
        self.sam_mode_active = False

        # Set requested tool mode
        if mode == 'brush':
            self.brush_mode_active = True
            # 显式复位橡皮擦标志，避免上一次 eraser 会话残留加擦除语义
            self.set_brush_eraser_mode(False)
            self.brush_cursor.setVisible(True)
        elif mode == 'eraser':
            self.brush_mode_active = True
            # 必须同步写入 _brush_params["is_eraser"]，否则落笔路径
            # (_handle_brush_press) 读到 False，整笔记为加色笔触
            self.set_brush_eraser_mode(True)
            self.brush_cursor.setVisible(True)
        elif mode == 'picker':
            self.is_picking = True
        elif mode == 'inspector':
            self.inspector_mode_active = True
            self.color_inspector.show()
        elif mode == 'sam':
            self.sam_mode_active = True

        # Update cursor and drag mode
        if not self.brush_mode_active:
            self.brush_cursor.setVisible(False)
        if mode == 'inspector':
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self.viewport().update()

    def mouseMoveEvent(self, event) -> None:
        # Color Inspector: real-time pixel info overlay
        if self.inspector_mode_active:
            try:
                self._update_color_inspector(event.pos())
            except Exception as e:
                logger.error(f"Error: {e}")
            super().mouseMoveEvent(event)
            return

        # Absolute intercept: Panning (middle mouse OR space+left button)
        # Task 3: Extended to support space+left button
        if self._is_panning:
            # Check if either middle button or (space pressed + left button) is active
            is_middle = event.buttons() & Qt.MouseButton.MiddleButton
            is_space_drag = self._space_pressed and (event.buttons() & Qt.MouseButton.LeftButton)
            
            if is_middle or is_space_drag:
                delta = event.pos() - self._pan_start_pos
                
                h_bar = self.horizontalScrollBar()
                v_bar = self.verticalScrollBar()
                h_bar.setValue(h_bar.value() - delta.x())
                v_bar.setValue(v_bar.value() - delta.y())
                if self._update_lod():
                    if not self._refresh_video_frame_view():
                        if self.engine:
                            self.engine.render_pipeline(render_reason='viewport_refresh')
                self.viewport().update()
                
                # Update start position since scrollbars moved, coordinate system shifted
                self._pan_start_pos = event.pos()
                event.accept()
                return

        # Handle Brush cursor position
        if self.brush_mode_active:
            scene_pos = self.mapToScene(event.pos())
            self.brush_cursor.setPos(scene_pos)
            
            # Handle Brush drawing
            if event.buttons() & Qt.MouseButton.LeftButton:
                self._handle_brush_move(scene_pos)
            return

        # Handle Color Picker Drag
        if self.is_picking and (event.buttons() & Qt.MouseButton.LeftButton):
            self._handle_color_pick(event.pos(), first=False)
            return

        # Handle Split View Wipe interaction
        if self.engine and self.engine.split_view_active:
            pos = event.pos()
            view_w = self.viewport().width()
            split_px = self.engine.split_position * view_w
            
            if self.is_dragging_split:
                new_pos = max(0.0, min(1.0, pos.x() / view_w))
                self.engine.set_split_position(new_pos)
                self.viewport().setCursor(Qt.CursorShape.SplitHCursor)
                self.viewport().update()
                return
            
            # Change cursor when hovering over split line
            if abs(pos.x() - split_px) < 10:
                self.viewport().setCursor(Qt.CursorShape.SplitHCursor)
            else:
                if not self.is_drawing:
                    self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

        # Handle AI-style Pen drag (pulling control handles)
        if self.is_drawing and self.pen_dragging:
            pos = self.mapToScene(event.pos())
            self._handle_pen_drag(pos)
            return

        # Force cursor to remain Cross during drawing even if DragMode tries to change it
        if self.is_drawing:
            # If mouse is over an item, let it show its cursor
            if not self.itemAt(event.pos()):
                self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event) -> None:
        # Handle Space key: video playback toggle or panning
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            # 视频模式下，空格键用于播放/暂停
            if hasattr(self, 'video_control_bar') and self.video_control_bar.isVisible():
                self.video_control_bar.play_toggled.emit()
                event.accept()
                return
            # 图片模式下，空格键用于平移画布
            self._space_pressed = True
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        
        # Handle Brush tool shortcuts (only when brush mode is active)
        if self.brush_mode_active:
            if event.key() == Qt.Key.Key_BracketLeft:  # [ - Decrease brush size
                new_size = max(1, self._brush_params["size"] - 10)
                self._brush_params["size"] = new_size
                self.brush_cursor.set_params(new_size, self._brush_params["feather"])
                self.brush_params_updated.emit(self._brush_params)
                return
            elif event.key() == Qt.Key.Key_BracketRight:  # ] - Increase brush size
                new_size = min(500, self._brush_params["size"] + 10)
                self._brush_params["size"] = new_size
                self.brush_cursor.set_params(new_size, self._brush_params["feather"])
                self.brush_params_updated.emit(self._brush_params)
                return
            elif event.key() == Qt.Key.Key_E:  # E - Toggle eraser mode
                is_eraser = not self._brush_params.get("is_eraser", False)
                self._brush_params["is_eraser"] = is_eraser
                self.brush_cursor.set_eraser_mode(is_eraser)
                self.brush_params_updated.emit(self._brush_params)
                return
            elif event.key() == Qt.Key.Key_B:  # B - Exit brush mode
                self.set_brush_mode(False)
                # Also update the mask panel button
                if hasattr(self, 'brush_mode_toggled_callback'):
                    self.brush_mode_toggled_callback(False)
                return
        
        # Handle C key for toggling crop tool
        if event.key() == Qt.Key.Key_C:
            # Toggle crop mode
            if self.crop_item:
                # Toggle visibility
                new_state = not self.crop_item.isVisible()
                self.set_crop_mode(new_state)
            else:
                # Enable crop mode for first time
                self.set_crop_mode(True)
            return
        
        # Handle Enter key for applying crop
        if self.crop_item and self.crop_item.isVisible():
            if event.key() in [Qt.Key.Key_Return, Qt.Key.Key_Enter]:
                self.crop_apply_requested.emit()
                return

        # Handle Delete key for mask nodes (Enhanced for multiple selected nodes)
        if event.key() in [Qt.Key.Key_Delete, Qt.Key.Key_Backspace]:
            deleted_anything = False
            for item in list(self.mask_items.values()):
                if item.shape_type == "curve":
                    # Collect all handles that are selected
                    selected_indices = []
                    for child in item.childItems():
                        if isinstance(child, MaskHandle) and child.handle_type == 'node' and child.isSelected():
                            selected_indices.append(child.index)
                    
                    if selected_indices:
                        # Sort descending to delete without invalidating indices
                        selected_indices.sort(reverse=True)
                        for idx in selected_indices:
                            item.on_node_deleted(idx)
                        deleted_anything = True
            
            if deleted_anything:
                return

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        # Handle Space key release: video mode or panning
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            # 视频模式下，空格键不用于平移，无需恢复光标
            if hasattr(self, 'video_control_bar') and self.video_control_bar.isVisible():
                event.accept()
                return
            # 图片模式下，恢复光标状态
            self._space_pressed = False
            
            # Restore cursor based on current tool state
            if self.is_drawing:
                self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            elif self.is_picking:
                self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            elif self.sam_mode_active and self.sam_ready:
                self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            elif self.brush_mode_active:
                self.viewport().setCursor(Qt.CursorShape.BlankCursor)
            else:
                self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            
            event.accept()
            return
        
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event) -> None:
        # Absolute intercept: Middle mouse button OR Space+Left button for panning (Task 3)
        if event.button() == Qt.MouseButton.MiddleButton:
            self._update_scene_pan_bounds()
            self._is_panning = True
            self._pan_start_pos = event.pos()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        
        # Task 3: Space key + Left mouse button for panning
        if self._space_pressed and event.button() == Qt.MouseButton.LeftButton:
            self._update_scene_pan_bounds()
            self._is_panning = True
            self._pan_start_pos = event.pos()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        # Point editing is explicit and takes priority over cycling MaskShapeItem
        # selection. Ordinary clicks retain the existing mask interaction path.
        if self.tracker_overlay.wants_point_interaction(self.mapToScene(event.pos()), event.button()):
            super().mousePressEvent(event)
            return

        # Handle Brush drawing start
        if self.brush_mode_active and event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.pos())
            self._handle_brush_press(scene_pos)
            return

        # Handle Split View Wipe Drag Start
        if self.engine and self.engine.split_view_active:
            pos = event.pos()
            split_px = self.engine.split_position * self.viewport().width()
            if abs(pos.x() - split_px) < 10:
                self.is_dragging_split = True
                return

        # 1. Handle Shift + Drag for Box Selection (User requested Shift+Drag to box select)
        if event.button() == Qt.MouseButton.LeftButton and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            super().mousePressEvent(event)
            return
        else:
            # Revert to NoDrag (or previous) to ensure we don't start a box select when just clicking/drawing
            if self.is_drawing:
                self.setDragMode(QGraphicsView.DragMode.NoDrag)

        # Get all items at click position
        items = self.items(event.pos())
        
        # Check if we hit a handle first (highest priority)
        hit_handle = None
        for item in items:
            if isinstance(item, MaskHandle):
                hit_handle = item
                break
        
        if hit_handle:
            if hit_handle.parentItem() and isinstance(hit_handle.parentItem(), MaskShapeItem):
                parent = hit_handle.parentItem()
                # Ensure the parent shape is selected so it highlights orange
                if not parent.isSelected():
                    self.scene.clearSelection()
                    parent.setSelected(True)
                self.mask_shape_selected.emit(parent.shape_id)
            super().mousePressEvent(event)
            return

        # 2. Selection Sync: Cycle through overlapping shapes
        shape_items = [i for i in items if isinstance(i, MaskShapeItem)]
        if shape_items and event.button() == Qt.MouseButton.LeftButton:
            # Find currently selected shape among the clicked ones
            current_idx = -1
            for i, shape in enumerate(shape_items):
                if shape.isSelected():
                    current_idx = i
                    break
            
            # Select next shape (cycle), or first if none selected
            next_idx = (current_idx + 1) % len(shape_items)
            target_shape = shape_items[next_idx]
            
            # Clear previous selection and select the target
            self.scene.clearSelection()
            target_shape.setSelected(True)
            # Fix: Update active_shape_id immediately for canvas selection
            self.active_shape_id = target_shape.shape_id
            # Fix: Update all mask item handle visibilities
            for item in self.mask_items.values():
                item._update_handle_positions()
            self.mask_shape_selected.emit(target_shape.shape_id)
            
            # If not drawing, we handled everything. If drawing, we still might want to add points.
            if not self.is_drawing:
                return

        # 3. Handle Color Picker
        if self.is_picking and event.button() == Qt.MouseButton.LeftButton:
            self._handle_color_pick(event.pos(), first=True)
            return

        # 3.5. Handle HSL Curves Picker (eyedropper for adding control points)
        if self.hsl_picker_mode and event.button() == Qt.MouseButton.LeftButton:
            self._handle_hsl_color_pick(event.pos())
            return

        # 3.6. Handle RGB Curves Picker (eyedropper for adding control points)
        if self.curves_picker_mode and event.button() == Qt.MouseButton.LeftButton:
            self._handle_curves_color_pick(event.pos())
            return

        # 4. Handle SAM Point Click
        if self.sam_mode_active:
            # Anti-accidental touch: Only allow clicks when SAM is ready
            if not self.sam_ready:
                return
            
            # Hit Test: Check if we clicked on an existing SAM point (for deletion)
            hit_item = self.itemAt(event.pos())
            if isinstance(hit_item, SAMPointItem):
                # Right-click on existing point -> delete it
                if event.button() == Qt.MouseButton.RightButton:
                    try:
                        idx = self.sam_points_ui.index(hit_item)
                    except ValueError:
                        idx = -1
                    
                    if idx >= 0:
                        # Remove from scene and list
                        self.scene.removeItem(hit_item)
                        self.sam_points_ui.pop(idx)
                        # Notify engine to remove the point and refresh mask
                        if self.engine:
                            self.engine.remove_sam_point(idx)
                        return
            
            # Create new point
            if event.button() in [Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton]:
                pos = self.mapToScene(event.pos())
                if self.logical_size:
                    w, h = self.logical_size.width(), self.logical_size.height()
                    nx, ny = pos.x() / w, pos.y() / h
                    if 0 <= nx <= 1.0 and 0 <= ny <= 1.0:
                        label = 1 if event.button() == Qt.MouseButton.LeftButton else 0
                        
                        # Create visual marker before emitting signal
                        point_item = SAMPointItem(is_foreground=(label == 1))
                        point_item.setPos(pos)
                        self.scene.addItem(point_item)
                        self.sam_points_ui.append(point_item)
                        
                        self.sam_point_clicked.emit(nx, ny, label)
                        return

        # 5. Handle Drawing/Pen Tool
        if self.is_drawing and event.button() == Qt.MouseButton.LeftButton:
            pos = self.mapToScene(event.pos())
            self._handle_pen_press(pos)
            return
            
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        """Right-click menu for control points."""
        item = self.itemAt(event.pos())
        
        # If we right-click a node handle, show specific options
        if isinstance(item, MaskHandle) and item.handle_type == 'node':
            menu = QMenu(self)

            # Count selected nodes for context
            selected_nodes = []
            for child in item.parentItem().childItems():
                if isinstance(child, MaskHandle) and child.handle_type == 'node' and child.isSelected():
                    selected_nodes.append(child)
            
            if len(selected_nodes) > 1:
                del_act = menu.addAction(f"Delete {len(selected_nodes)} Selected Points")
                # Trigger mass deletion logic
                del_act.triggered.connect(lambda: self._delete_selected_nodes(item.parentItem()))
            else:
                del_act = menu.addAction(I18N.tr("canvas.delete_point", "Delete Point"))
                del_act.setShortcut("Del")
                del_act.triggered.connect(lambda: item.parentItem().on_node_deleted(item.index))
            
            menu.addSeparator()
            
            # Node Type Conversions
            smooth_act = menu.addAction(I18N.tr("canvas.convert_to_smooth", "Convert to Smooth"))
            smooth_act.setToolTip(I18N.tr("canvas.shortcut_double_click", "Shortcut: Double-click"))
            smooth_act.triggered.connect(lambda: self._set_node_type(item, 'smooth'))
            
            corner_act = menu.addAction(I18N.tr("canvas.convert_to_corner", "Convert to Corner"))
            corner_act.setToolTip(I18N.tr(
                "canvas.shortcut_double_click_alt_drag",
                "Shortcut: Double-click or Alt+Drag handle"))
            corner_act.triggered.connect(lambda: self._set_node_type(item, 'corner'))
            
            menu.exec(event.globalPos())
            return

        # Canvas image right-click context menu
        self._show_image_context_menu(event)

    def _show_image_context_menu(self, event) -> None:
        """Show right-click context menu on the viewport image area."""
        menu = QMenu(self)

        # Capture Still
        if hasattr(self, 'viewport_menu') and hasattr(self.viewport_menu, 'capture_still_requested'):
            capture_act = menu.addAction(
                I18N.tr("viewport.capture_still", "Capture Still"),
                self.viewport_menu.capture_still_requested.emit
            )
            # AUD-24601: 与 View 菜单 capture_still_act 一致的独占键序
            capture_act.setShortcut("Ctrl+Alt+S")
            menu.addSeparator()

        # Fit to Window
        fit_act = menu.addAction(I18N.tr("viewport.fit", "Fit to Window"), self.fit_to_window)
        fit_act.setShortcut("0")
        actual_act = menu.addAction(I18N.tr("viewport.actual_size", "Actual Size"),
                                    lambda: self.viewport_menu._set_actual_size() if hasattr(self, 'viewport_menu') else None)
        actual_act.setShortcut("1")

        menu.addSeparator()
        menu.addAction(I18N.tr("workspace.scopes", "Scopes"), self.scopes_requested.emit)

        menu.addSeparator()

        # Zoom
        zoom_in_act = menu.addAction(I18N.tr("viewport.zoom_in", "Zoom In"),
                                     lambda: self.viewport_menu._zoom_by_factor(1.25) if hasattr(self, 'viewport_menu') else None)
        zoom_in_act.setShortcut("Ctrl+=")
        zoom_out_act = menu.addAction(I18N.tr("viewport.zoom_out", "Zoom Out"),
                                      lambda: self.viewport_menu._zoom_by_factor(1/1.25) if hasattr(self, 'viewport_menu') else None)
        zoom_out_act.setShortcut("Ctrl+-")

        _inject_menu_shortcuts(menu)

        # Float/Dock toggle for the viewport panel
        from PyQt6.QtWidgets import QDockWidget
        widget = self.parent()
        while widget:
            if isinstance(widget, QDockWidget):
                menu.addSeparator()
                if widget.isFloating():
                    menu.addAction(
                        I18N.tr("viewport.dock", "Dock"),
                        lambda: widget.setFloating(False)
                    )
                else:
                    menu.addAction(
                        I18N.tr("viewport.float", "Float"),
                        lambda: widget.setFloating(True)
                    )
                break
            widget = widget.parent()

        menu.exec(event.globalPos())

    def _delete_selected_nodes(self, mask_item) -> None:
        """Deletes all currently selected node handles in a mask."""
        selected_indices = []
        for child in mask_item.childItems():
            if isinstance(child, MaskHandle) and child.handle_type == 'node' and child.isSelected():
                selected_indices.append(child.index)
        
        if selected_indices:
            selected_indices.sort(reverse=True)
            for idx in selected_indices:
                mask_item.on_node_deleted(idx)

    def _set_node_type(self, handle, node_type) -> None:
        parent = handle.parentItem()
        if parent and hasattr(parent, 'bezier_points'):
            parent.bezier_points[handle.index]['type'] = node_type
            if node_type == 'smooth':
                # Re-align handles
                rel = parent.bezier_points[handle.index]['c1'] - parent.bezier_points[handle.index]['pos']
                if math.hypot(rel.x(), rel.y()) < 2.0:
                    rel = QPointF(30, 0)
                parent.bezier_points[handle.index]['c2'] = parent.bezier_points[handle.index]['pos'] - rel
                parent.bezier_points[handle.index]['c1'] = parent.bezier_points[handle.index]['pos'] + rel
            parent._update_handle_positions()
            parent._sync_back()
            parent.update()

    def mouseReleaseEvent(self, event) -> None:
        # Absolute intercept: Panning release (middle mouse OR space+left button)
        if self._is_panning:
            # 只要松开了中键或左键，无条件解除平移状态，防止状态机死锁
            if event.button() in [Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton]:
                self._is_panning = False

                # Restore cursor based on current tool state
                if getattr(self, '_space_pressed', False):
                    # Still in space mode, show open hand
                    self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
                elif getattr(self, 'sam_mode_active', False) and getattr(self, 'sam_ready', False):
                    self.viewport().setCursor(Qt.CursorShape.CrossCursor)
                elif getattr(self, 'brush_mode_active', False):
                    self.viewport().setCursor(Qt.CursorShape.BlankCursor)  # Brush hides default cursor
                else:
                    self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

                # P4-1: Pan 结束时触发轻量视口刷新（复用 GPU 纹理，不重新解码）
                # 优先使用 video_controller.refresh_current_frame_view()，
                # 仅在无视频时回退到 engine.render_pipeline()
                if self._update_lod():
                    if not self._refresh_video_frame_view():
                        if self.engine:
                            self.engine.render_pipeline(render_reason='viewport_refresh')
                self.viewport().update()

                # Note: Composite page uses ColorOutput (independent of viewport),
                # so pan/zoom does NOT trigger Color re-render.

                event.accept()
                return

        # Handle Brush stroke finish
        if self.brush_mode_active and event.button() == Qt.MouseButton.LeftButton:
            self._finish_brush_stroke()
            return

        if self.is_dragging_split:
            self.is_dragging_split = False
            return

        if self.dragMode() == QGraphicsView.DragMode.RubberBandDrag:
            # Revert to standard mode or keep drawing mode
            if self.is_drawing:
                self.setDragMode(QGraphicsView.DragMode.RubberBandDrag) # Keep it for shift
            else:
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

        # Performance optimization: Removed unconditional recenter() call here
        # The recenter() is now only called when:
        # 1. Adding a new point (in _handle_pen_press)
        # 2. Closing a curve (in _handle_pen_press)
        # This prevents unnecessary centroid recalculation during normal node dragging

        if self.is_drawing and self.pen_dragging:
            self.pen_dragging = False
            self.pen_start_pos = None
            return
        super().mouseReleaseEvent(event)

    def _handle_pen_press(self, pos) -> None:
        active_item = None
        for item in self.mask_items.values():
            if item.isSelected() and item.shape_type == "curve":
                active_item = item
                break
        
        if not active_item: return

        local_pos = pos - active_item.pos()
        
        # 1. Check for adding a point on an existing segment
        if not self.pen_dragging and len(active_item.bezier_points) > 1:
            path = active_item._get_path()
            stroker = QPainterPathStroker()
            stroker.setWidth(10 / self.transform().m11())
            hit_path = stroker.createStroke(path)
            
            if hit_path.contains(local_pos):
                # Find the segment to insert into
                # We iterate through segments and find the closest one
                insert_idx = -1
                
                pts = active_item.bezier_points
                for i in range(len(pts)):
                    p1 = pts[i]
                    p2 = pts[(i + 1) % len(pts)]
                    
                    # Create a path for just this segment
                    seg_path = QPainterPath()
                    seg_path.moveTo(p1['pos'])
                    seg_path.cubicTo(p1['c2'], p2['c1'], p2['pos'])
                    
                    # Check if click is on this segment
                    seg_hit = stroker.createStroke(seg_path)
                    if seg_hit.contains(local_pos):
                        insert_idx = i + 1
                        break
                
                if insert_idx != -1:
                    new_pt = {
                        'pos': local_pos,
                        'c1': local_pos,
                        'c2': local_pos,
                        'type': 'corner' if self._mask_creation_mode == 'polygon' else 'smooth'
                    }
                    active_item.bezier_points.insert(insert_idx, new_pt)
                    active_item.selected_node_idx = insert_idx
                    active_item._update_handle_positions()
                    active_item._sync_back()
                    active_item.update()
                    return

        # 2. Check for closure (click on first point)
        if len(active_item.bezier_points) > 1:
            first_pt = active_item.bezier_points[0]['pos'] + active_item.pos()
            dist = math.sqrt((pos.x() - first_pt.x())**2 + (pos.y() - first_pt.y())**2)
            if dist < 15:
                # 仅结束当前的拖拽动作，保持编辑状态，不强制退出工具
                self.pen_dragging = False
                active_item._update_handle_positions()
                active_item.update()
                return

        # 3. Add new point (Default to Smooth with generous handles as requested)
        polygon = self._mask_creation_mode == 'polygon'
        handle_offset = QPointF() if polygon else QPointF(40, 0)
        new_pt = {
            'pos': pos - active_item.pos(),
            'c1': pos - active_item.pos() - handle_offset,
            'c2': pos - active_item.pos() + handle_offset,
            'type': 'corner' if polygon else 'smooth'
        }
        active_item.bezier_points.append(new_pt)
        active_item.selected_node_idx = len(active_item.bezier_points) - 1
        
        # Requirement: Keep center handle at shape center
        active_item.recenter()
        
        self.pen_dragging = not polygon
        self.pen_start_pos = pos
        
        active_item._update_handle_positions()
        active_item._sync_back()
        active_item.update()

    def _apply_raw_color_management(self, rgb_array):
        """
        将 RAW 面板的色彩空间、Gamma、曝光等核心参数应用到单个像素。
        确保取色器拿到的是"所见即所得"的色彩，而不阻塞 GPU 管线。
        """
        if not self.engine:
            return rgb_array

        params = self.engine.current_raw_params
        metadata = self.engine.current_raw_metadata
        
        is_raw = self.engine.current_file_path and os.path.splitext(self.engine.current_file_path)[1].lower() in self.engine.RAW_EXTENSIONS
        is_effectively_srgb = self.engine.is_current_image_srgb or not is_raw
        
        # 如果是普通图片且 RAW 控制未开启，直接返回
        if is_effectively_srgb and not params.get("enabled", False):
            return rgb_array

        color = np.array(rgb_array, dtype=np.float32)

        # 1. 如果源是 sRGB，反向退回线性 ProPhoto (匹配 Shader 逻辑)
        if is_effectively_srgb:
            color = np.where(color <= 0.04045, color / 12.92, np.power(np.maximum(color + 0.055, 0) / 1.055, 2.4))
            # sRGB (D65) -> ProPhoto RGB (D50), 正确的矩阵
            srgb_to_prophoto = np.array([
                [0.5292930, 0.0983601, 0.0168761],
                [0.3301161, 0.8734699, 0.1176587],
                [0.1405909, 0.0281700, 0.8654651]
            ])
            color = np.dot(srgb_to_prophoto, color)

        # 2. 曝光与白平衡增益
        last_decode = self.engine._last_applied_raw_params
        baseline_wb = metadata.get("camera_wb", [2.0, 1.0, 1.5, 1.0])
        last_wb = last_decode.get('user_wb', baseline_wb)
        curr_wb = params.get('user_wb', last_wb)

        gains = np.array([
            curr_wb[0] / max(1e-4, last_wb[0]),
            curr_wb[1] / max(1e-4, last_wb[1]),
            curr_wb[2] / max(1e-4, last_wb[2])
        ])

        color *= math.pow(2.0, params.get('exposure', 0.0))
        color *= gains

        # 3. Lift, Gain, Contrast
        color = color * params.get('gain', 1.0) + params.get('lift', 0.0)
        contrast = params.get('contrast', 1.0)
        if contrast != 1.0:
            color = (color - 0.18) * contrast + 0.18

        color = np.maximum(color, 0.0)

        # 4. 色彩空间转换 (ProPhoto 到目标空间，Python 采用行主序矩阵)
        cs = params.get('color_space', "sRGB")
        if cs in ["sRGB", "Rec.709"]:
            # ProPhoto RGB (D50) -> sRGB/Rec.709 (D65)
            matrix = np.array([
                [ 2.0342892, -0.7275078, -0.3067813],
                [-0.2288020,  1.2317255, -0.0029236],
                [-0.0085623, -0.1532653,  1.1618276]
            ])
        elif cs == "Rec.2020":
            # ProPhoto RGB (D50) -> Rec.2020 (D65)
            matrix = np.array([
                [ 1.2006095, -0.0574933, -0.1431162],
                [-0.0699261,  1.0806111, -0.0106850],
                [ 0.0055390, -0.0407803,  1.0352414]
            ])
        elif cs == "Adobe RGB":
            # ProPhoto RGB (D50) -> Adobe RGB (D65)
            matrix = np.array([
                [ 1.3895924, -0.1693724, -0.2202200],
                [-0.2288020,  1.2317255, -0.0029236],
                [-0.0176278, -0.0962564,  1.1138841]
            ])
        elif cs in ["DCI-P3", "Display P3"]:
            # ProPhoto RGB (D50) -> DCI-P3/Display P3 (D65)
            matrix = np.array([
                [ 1.6325044, -0.3796694, -0.2528350],
                [-0.1536805,  1.1666904, -0.0130099],
                [ 0.0103902, -0.0628051,  1.0524149]
            ])
        elif cs == "ACEScg":
            # ProPhoto RGB (D50) -> ACEScg (D60)
            matrix = np.array([
                [ 1.1691282, -0.0350954, -0.1340327],
                [-0.0669844,  1.0755682, -0.0085838],
                [ 0.0094207, -0.0133505,  1.0039298]
            ])
        else:
            # 默认 fallback 到 sRGB
            matrix = np.array([
                [ 2.0342892, -0.7275078, -0.3067813],
                [-0.2288020,  1.2317255, -0.0029236],
                [-0.0085623, -0.1532653,  1.1618276]
            ])
        
        color = np.dot(matrix, color)
        color = np.maximum(color, 0.0)

        # 5. Gamma 编码转换（与引擎 gamma_map 对齐，image_engine.py）
        # AUD-25202: "Linear" 引擎侧映射为 0（不做 OETF），此前与
        # "Gamma 2.2" 共用 pow(1/2.2) 编码导致取色读数偏亮；补齐
        # "Gamma 2.6" 分支。缺键默认与引擎一致按 Linear（不编码）。
        gamma = params.get('gamma', "Linear")
        if gamma == "sRGB":
            color = np.where(color <= 0.0031308, 12.92 * color, 1.055 * np.power(color, 1.0/2.4) - 0.055)
        elif gamma == "Rec.709":
            color = np.where(color <= 0.018, 4.5 * color, 1.099 * np.power(color, 0.45) - 0.099)
        elif gamma == "Gamma 2.2":
            color = np.power(color, 1.0/2.2)
        elif gamma == "Gamma 2.4":
            color = np.power(color, 1.0/2.4)
        elif gamma == "Gamma 2.6":
            color = np.power(color, 1.0/2.6)
        # "Linear" 及未识别值：不做编码（引擎 Linear=0 语义）
        
        return color

    def _scene_pixel_rgb(self, scene_pos):
        """读取场景坐标对应原图像素，应用与取色器一致的完整处理链。

        AUD-25203：颜色检查器与取色器共用本方法，保证坐标缩放映射
        （display→original；时间线预览 override 下 logical_size ≠ 原图
        尺寸）与 RAW 色彩管理完全一致。旧实现检查器直接把场景坐标当
        原图像素索引且不应用 RAW 色彩管理，与取色器读数互相矛盾。

        Returns:
            (rgb, img_x, img_y)：rgb 为 float32 RGB（已应用 RAW 色彩
            管理、非负保护与 HDR 等比保护）；像素越界时 rgb 为 None，
            img_x/img_y 仍为映射后坐标（供检查器越界显示）；引擎或显示
            图像不可用时返回 (None, -1, -1)。
        """
        if not self.engine or self.engine.img_full_original is None:
            return None, -1, -1
        disp_img = self.engine.get_display_image()
        if disp_img is None:
            return None, -1, -1

        disp_h, disp_w = disp_img.shape[:2]
        orig_h, orig_w = self.engine.img_full_original.shape[:2]

        scale_x = orig_w / float(disp_w) if disp_w > 0 else 1.0
        scale_y = orig_h / float(disp_h) if disp_h > 0 else 1.0

        img_x = int(scene_pos.x() * scale_x)
        img_y = int(scene_pos.y() * scale_y)
        if not (0 <= img_x < orig_w and 0 <= img_y < orig_h):
            return None, img_x, img_y

        # 提取像素，彻底剥离 Alpha 通道干扰
        pixel_data = self.engine.img_full_original[img_y, img_x]
        if isinstance(pixel_data, np.ndarray) and pixel_data.size >= 3:
            bgr = pixel_data[:3]
        else:
            bgr = np.array([pixel_data, pixel_data, pixel_data]) if np.asarray(pixel_data).size == 1 else pixel_data

        rgb = bgr[::-1].astype(np.float32)  # BGR to RGB

        # 基础归一化 (兼容各种位深)
        dtype = self.engine.img_full_original.dtype
        if dtype == np.uint8:
            c = rgb / 255.0
        elif dtype == np.uint16:
            c = rgb / 65535.0
        elif dtype == np.int16:
            c = rgb / 32767.0
        else:
            c = rgb

        # 应用 RAW 色彩管理映射（与取色器一致）
        c = self._apply_raw_color_management(c)

        # HDR 高光色彩等比保护（防止纯色截断导致色相丢失）
        c = np.maximum(c, 0.0)
        c_max_val = np.max(c)
        if c_max_val > 1.0:
            c = c / c_max_val
        return c, img_x, img_y

    def _update_color_inspector(self, pos) -> None:
        """Read pixel under cursor and update the floating inspector overlay."""
        if not self.engine or self.engine.img_full_original is None:
            return

        scene_pos = self.mapToScene(pos)
        # AUD-25203: 复用取色器的坐标映射与 RAW 色彩管理（同一处理链）
        rgb_f, img_x, img_y = self._scene_pixel_rgb(scene_pos)
        if rgb_f is not None:
            rgb_int = (np.clip(rgb_f, 0.0, 1.0) * 255.0 + 0.5).astype(int)
            self.color_inspector.update_info(
                (int(rgb_int[0]), int(rgb_int[1]), int(rgb_int[2])),
                (float(rgb_f[0]), float(rgb_f[1]), float(rgb_f[2])),
                (img_x, img_y)
            )
        elif img_x >= 0:
            # 越界：黑色 + 映射后坐标（保持原有越界显示行为）
            self.color_inspector.update_info((0, 0, 0), (0.0, 0.0, 0.0), (img_x, img_y))

        # Position overlay using viewport widget coordinates
        vp = self.viewport()
        vp_w, vp_h = vp.width(), vp.height()
        cursor_x, cursor_y = pos.x(), pos.y()
        off_x, off_y = 20, 20
        insp_w, insp_h = self.color_inspector.width(), self.color_inspector.height()
        if cursor_x + off_x + insp_w > vp_w:
            off_x = -insp_w - 10
        if cursor_y + off_y + insp_h > vp_h:
            off_y = -insp_h - 10

        self.color_inspector.move(int(cursor_x + off_x), int(cursor_y + off_y))

    def _handle_color_pick(self, pos, first=True) -> None:
        if not self.engine or self.engine.img_full_original is None:
            return

        # 1. 精准坐标映射：从 UI 视图坐标 -> 全分辨率原图真实坐标，
        #    并应用与颜色检查器一致的 RAW 色彩管理（AUD-25203 抽取的
        #    公共处理链 _scene_pixel_rgb）
        scene_pos = self.mapToScene(pos)
        c, img_x, img_y = self._scene_pixel_rgb(scene_pos)
        if c is None:
            return

        # 4. 与 shader 的 rgb_to_qualifier_hsv（shader_nodes.py Phase 3）保持
        #    同一 HSV 语义：v = min(c_max, 1.0)（RAW 高光钳制），s = delta/c_max。
        #    此前误用 HSL 的 l=(max+min)/2：高饱和色 v≈2l 会落在 lum_high 之外，
        #    吸管拾取后 matte≈0（自己点的颜色选不中）。
        c = np.clip(c, 0.0, None)
        c_min = np.min(c)
        c_max = np.max(c)
        delta = c_max - c_min

        s = 0.0
        h_val = 0.0

        if c_max > 0.00001:
            # HSV 饱和度公式 (delta / max)，恒映射在 0-1
            s = delta / c_max

        if delta > 0.00001:
            if c[0] == c_max:
                h_val = (c[1] - c[2]) / delta + (6.0 if c[1] < c[2] else 0.0)
            elif c[1] == c_max:
                h_val = (c[2] - c[0]) / delta + 2.0
            else:
                h_val = (c[0] - c[1]) / delta + 4.0
            h_val /= 6.0

        # RAW 高光亮度安全钳制在 1.0，UI 的 0.0-1.0 滑块才能捕获
        v = min(c_max, 1.0)

        # 安全钳制防溢出
        s = max(0.0, min(1.0, float(s)))
        v = max(0.0, min(1.0, float(v)))
        h_val = h_val % 1.0

        self.color_picked.emit(h_val, s, v, first)

    def _handle_hsl_color_pick(self, pos) -> None:
        """
        Handle HSL Curves color picker - extract RGB (0-255) from clicked pixel.
        This is used to add control points on HSL curves based on image colors.
        """
        if not self.engine or self.engine.img_full_original is None:
            return

        # 1. Map UI coordinates to full-resolution image coordinates
        scene_pos = self.mapToScene(pos)

        disp_img = self.engine.get_display_image()
        if disp_img is None:
            return

        disp_h, disp_w = disp_img.shape[:2]
        orig_h, orig_w = self.engine.img_full_original.shape[:2]

        scale_x = orig_w / float(disp_w) if disp_w > 0 else 1.0
        scale_y = orig_h / float(disp_h) if disp_h > 0 else 1.0

        img_x = int(scene_pos.x() * scale_x)
        img_y = int(scene_pos.y() * scale_y)

        if 0 <= img_x < orig_w and 0 <= img_y < orig_h:
            # 2. Extract pixel, strip Alpha channel
            pixel_data = self.engine.img_full_original[img_y, img_x]
            if isinstance(pixel_data, np.ndarray) and pixel_data.size >= 3:
                bgr = pixel_data[:3]
            else:
                bgr = np.array([pixel_data, pixel_data, pixel_data]) if np.asarray(pixel_data).size == 1 else pixel_data

            # 3. Convert BGR to RGB
            rgb = bgr[::-1].astype(np.float32)

            # 4. Normalize based on image dtype to 0-255 range
            dtype = self.engine.img_full_original.dtype
            if dtype == np.uint8:
                r, g, b = rgb[0], rgb[1], rgb[2]  # Already 0-255
            elif dtype == np.uint16:
                r, g, b = rgb[0] / 65535.0 * 255.0, rgb[1] / 65535.0 * 255.0, rgb[2] / 65535.0 * 255.0
            elif dtype == np.int16:
                r, g, b = rgb[0] / 32767.0 * 255.0, rgb[1] / 32767.0 * 255.0, rgb[2] / 32767.0 * 255.0
            else:
                # Assume float 0-1 or HDR
                max_val = max(1.0, np.max(rgb))
                r, g, b = rgb[0] / max_val * 255.0, rgb[1] / max_val * 255.0, rgb[2] / max_val * 255.0

            # Clamp to 0-255
            r = max(0.0, min(255.0, float(r)))
            g = max(0.0, min(255.0, float(g)))
            b = max(0.0, min(255.0, float(b)))

            # 5. Emit RGB signal for HSL curves
            self.hsl_color_for_curves.emit(r, g, b)

    def _handle_curves_color_pick(self, pos) -> None:
        """
        Handle RGB Curves color picker - extract RGB (0-255) from clicked pixel.
        This is used to add control points on RGB curves based on image colors.
        """
        if not self.engine or self.engine.img_full_original is None:
            return

        # 1. Map UI coordinates to full-resolution image coordinates
        scene_pos = self.mapToScene(pos)

        disp_img = self.engine.get_display_image()
        if disp_img is None:
            return

        disp_h, disp_w = disp_img.shape[:2]
        orig_h, orig_w = self.engine.img_full_original.shape[:2]

        scale_x = orig_w / float(disp_w) if disp_w > 0 else 1.0
        scale_y = orig_h / float(disp_h) if disp_h > 0 else 1.0

        img_x = int(scene_pos.x() * scale_x)
        img_y = int(scene_pos.y() * scale_y)

        if 0 <= img_x < orig_w and 0 <= img_y < orig_h:
            # 2. Extract pixel, strip Alpha channel
            pixel_data = self.engine.img_full_original[img_y, img_x]
            if isinstance(pixel_data, np.ndarray) and pixel_data.size >= 3:
                bgr = pixel_data[:3]
            else:
                bgr = np.array([pixel_data, pixel_data, pixel_data]) if np.asarray(pixel_data).size == 1 else pixel_data

            # 3. Convert BGR to RGB
            rgb = bgr[::-1].astype(np.float32)

            # 4. Normalize based on image dtype to 0-255 range
            dtype = self.engine.img_full_original.dtype
            if dtype == np.uint8:
                r, g, b = rgb[0], rgb[1], rgb[2]  # Already 0-255
            elif dtype == np.uint16:
                r, g, b = rgb[0] / 65535.0 * 255.0, rgb[1] / 65535.0 * 255.0, rgb[2] / 65535.0 * 255.0
            elif dtype == np.int16:
                r, g, b = rgb[0] / 32767.0 * 255.0, rgb[1] / 32767.0 * 255.0, rgb[2] / 32767.0 * 255.0
            else:
                # Assume float 0-1 or HDR
                max_val = max(1.0, np.max(rgb))
                r, g, b = rgb[0] / max_val * 255.0, rgb[1] / max_val * 255.0, rgb[2] / max_val * 255.0

            # Clamp to 0-255
            r = max(0.0, min(255.0, float(r)))
            g = max(0.0, min(255.0, float(g)))
            b = max(0.0, min(255.0, float(b)))

            # 5. Emit RGB signal for RGB curves
            self.curves_color_for_curves.emit(r, g, b)

    def _handle_pen_drag(self, pos) -> None:
        active_item = None
        for item in self.mask_items.values():
            if item.isSelected() and item.shape_type == "curve":
                active_item = item
                break
        if not active_item or not self.pen_dragging: return

        idx = len(active_item.bezier_points) - 1
        if idx < 0: return

        # Pull control handles
        delta = pos - self.pen_start_pos
        active_item.bezier_points[idx]['type'] = 'smooth'
        active_item.bezier_points[idx]['c1'] = active_item.bezier_points[idx]['pos'] - delta
        active_item.bezier_points[idx]['c2'] = active_item.bezier_points[idx]['pos'] + delta
        
        active_item._update_handle_positions()
        active_item._sync_back()
        active_item.update()

    def update_mask_overlay(self, layer):
        if not layer or not self.logical_size:
            for item in list(self.mask_items.values()):
                if item.scene() == self.scene:
                    item.setSelected(False)
                    item.clearFocus()
                    grabber = self.scene.mouseGrabberItem()
                    if grabber and (grabber == item or item.isAncestorOf(grabber)):
                        grabber.ungrabMouse()
                    self.scene.removeItem(item)
            self.mask_items.clear()
            return
        w, h = self.logical_size.width(), self.logical_size.height()
        
        # 递归函数：扁平化提取所有形状及其实际可见性/锁定（继承父群组）
        def get_all_shapes_and_visibility(items, parent_visible=True, parent_locked=False):
            result = []
            for item in items:
                is_visible = parent_visible and getattr(item, 'visible', True)
                is_locked = parent_locked or getattr(item, 'locked', False)
                if isinstance(item, MaskGroup):
                    result.extend(get_all_shapes_and_visibility(
                        item.children, is_visible, is_locked))
                else:
                    result.append((item, is_visible, is_locked))
            return result

        all_shapes_info = get_all_shapes_and_visibility(layer.mask_shapes)
        current_ids = set(shape.id for shape, _, _ in all_shapes_info)
        
        for sid in list(self.mask_items.keys()):
            if sid not in current_ids:
                item = self.mask_items[sid]
                
                # 1. 安全锁：阻止任何后续的 handle 更新或同步操作
                item._is_updating_handles = True
                
                # 2. 状态重置
                item.setSelected(False)
                item.setEnabled(False)
                if self.active_shape_id == sid:
                    self.active_shape_id = None
                
                # 3. 清除鼠标抓取（防止在销毁过程中仍处理鼠标事件）
                grabber = self.scene.mouseGrabberItem()
                if grabber and (grabber == item or item.isAncestorOf(grabber)):
                    grabber.ungrabMouse()
                
                # 4. 【关键修复】不要清空 handles 字典！
                # handles 是 QGraphicsItem 的子项，Qt 的父子关系会在父项移除时自动清理子项
                # 如果我们手动清空字典，Python 会释放对这些对象的引用
                # 但 Qt C++ 层仍然持有它们（作为子项），导致后续访问已释放内存
                # 正确做法：只从场景移除父项，让 Qt 自动处理子项清理
                
                # 5. 从场景移除（Qt 会自动处理所有子项的清理）
                if item.scene() == self.scene:
                    self.scene.removeItem(item)
                    
                del self.mask_items[sid]
                
        for shape, effective_visible, effective_locked in all_shapes_info:
            # shape 已经是扁平化的单个形状，不需要跳过 MaskGroup

            if shape.id not in self.mask_items:
                item = MaskShapeItem(shape.id, shape.type, self)
                self.scene.addItem(item)
                self.mask_items[shape.id] = item
            else:
                item = self.mask_items[shape.id]

            # 同步可见性：结合全局 overlay 开关和继承的可见性
            item.setVisible(self._show_mask_overlays and effective_visible)
            # P1-COLOR-09: 同步锁定状态（含群组继承）
            item.set_locked(effective_locked)
            
            # 跟踪集成：有跟踪绑定时轮廓按当前帧求值几何绘制（不修改原始形状）
            effective_shape = shape
            if self.engine is not None and hasattr(self.engine, "get_tracked_geometry"):
                geometry = self.engine.get_tracked_geometry(shape)
                if geometry is not None:
                    import copy as _copy
                    effective_shape = _copy.copy(shape)
                    effective_shape.center = list(geometry.get("center", shape.center))
                    effective_shape.size = list(geometry.get("size", shape.size))
                    effective_shape.rotation = float(geometry.get("rotation", shape.rotation) or 0.0)
                    if "points" in geometry:
                        effective_shape.points = geometry["points"]
                    if "grad_points" in geometry:
                        effective_shape.grad_points = geometry["grad_points"]

            center = QPointF(effective_shape.center[0] * w, effective_shape.center[1] * h)
            size = [effective_shape.size[0] * w, effective_shape.size[1] * h]
            item.update_from_data(center, size, effective_shape.rotation, shape.feather,
                                  points=getattr(effective_shape, 'points', None),
                                  brush_strokes=getattr(shape, 'brush_strokes', None),
                                  grad_points=getattr(effective_shape, 'grad_points', None),
                                  edge_softness=getattr(shape, 'edge_softness', None))

    def set_video_window_mode(self, enabled: bool) -> None:
        """Select video window handles without altering image mask tools."""
        self._video_window_mode = bool(enabled)
        for item in self.mask_items.values():
            item.set_video_window_mode(self._video_window_mode)
        self.viewport().update()

    def wheelEvent(self, event) -> None:
        speed = 100
        if hasattr(self, 'config_manager') and self.config_manager:
            speed = self.config_manager.get("zoom_speed") or 100

        # Use pixelDelta for smooth trackpad scrolling (macOS),
        # fall back to angleDelta for traditional mouse wheels.
        pixel_delta = event.pixelDelta().y()
        if pixel_delta:
            # pixelDelta is physical scroll pixels, not wheel notches. Normalize it
            # so a trackpad gesture produces many small zoom steps instead of jumps.
            steps = pixel_delta / 120.0
        else:
            # Standard mouse wheel: angleDelta is in 1/8 degree units.
            # Typical notch = 120 units → normalize to ~1.0
            steps = event.angleDelta().y() / 120.0

        # Skip truly zero deltas (can happen on macOS trackpad momentum)
        if abs(steps) < 0.001:
            event.accept()
            return

        steps = max(-4.0, min(4.0, steps))
        base_factor = 1.0 + (speed / 100.0) * 0.15  # speed=100 → one wheel notch = 1.15x
        z = base_factor ** steps

        # Wheel zoom is a zoom-only operation.  ``AnchorUnderMouse`` makes
        # QGraphicsView change the scroll position on every wheel event so the
        # pixel under the cursor stays fixed.  That is useful for some editors,
        # but here it looks like an accidental horizontal pan, especially when
        # several events arrive quickly.  Keep the current viewport center
        # fixed instead, while preserving any deliberate pan made with the
        # middle mouse button or Space+drag.
        viewport_center = self.viewport().rect().center()
        scene_center = self.mapToScene(viewport_center)
        previous_anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        try:
            # Expand sceneRect BEFORE scale() so that the view-center anchor
            # has a valid scroll range even when zooming out rapidly.
            self._update_scene_pan_bounds(new_scale_factor=z)
            # Changing sceneRect can change hidden scrollbar ranges. Restore
            # the saved scene center both before and after scaling to prevent
            # that range change from becoming a visible pan.
            self._center_on_scene_point_without_drift(
                scene_center, viewport_center
            )
            self.scale(z, z)
            self._center_on_scene_point_without_drift(
                scene_center, viewport_center
            )
        finally:
            self.setTransformationAnchor(previous_anchor)
        # 刷新选中遮罩的手柄位置（保持旋转手柄视觉长度不变）
        for item in self.mask_items.values():
            if item.isSelected():
                item._update_handle_positions()
        if self._update_lod():
            if not self._refresh_video_frame_view():
                # 播放中跳过完整渲染管线，下个 tick 会自动刷新
                is_playing = False
                win = self.window()
                if win and hasattr(win, 'video_controller') and win.video_controller:
                    is_playing = getattr(win.video_controller, 'is_playing', False)
                    if callable(is_playing):
                        is_playing = is_playing()
                if not is_playing and self.engine:
                    self.engine.render_pipeline(render_reason='viewport_refresh')
        self.viewport().update()
        event.accept()


    def set_active_shape(self, shape_id) -> None:
        """Sets the currently active shape for persistent highlighting."""
        self.active_shape_id = shape_id
        # Also ensure the corresponding QGraphicsItem is selected for handle visibility
        # blockSignals to avoid recursive selection signals back to MainWindow
        self.scene.blockSignals(True)
        for sid, item in self.mask_items.items():
            is_active = (sid == shape_id)
            item.setSelected(is_active)
            # FORCE Z-Order: Active shape MUST be on top of others to receive mouse events (fix for overlapping shapes)
            item.setZValue(25 if is_active else 20)
            # Fix: Force update handle positions to restore visibility when re-selecting curve
            item._update_handle_positions()
        self.scene.blockSignals(False)
        self.viewport().update()

    def set_overlay_visible(self, visible) -> None:
        """Toggle visibility of mask items and handles on the canvas.
        
        Task 1: 保存可见性状态并主动更新 mask overlay。
        """
        self._show_mask_overlays = visible
        # 主动调用 update_mask_overlay 以应用新的可见性状态
        self.update_mask_overlay(self.engine.get_active_layer() if self.engine else None)
        self.viewport().update()

    def cancel_active_tools(self) -> None:
        """安全重置所有工具状态，恢复默认视图模式。

        Task 2: 跨面板的工具状态安全重置。
        当用户切换页面或关闭 Mask 面板时调用，确保画布上不会残留意外的绘制状态。

        注意：只重置"工具"状态，保留形状选中（active_shape_id / 场景选中）。
        选中形状是模型级状态（决定参数面板与 Tracker 绑定的目标），
        面板可见性切换不允许破坏它。
        """
        # 重置所有工具状态
        self.is_drawing = False
        self.sam_mode_active = False
        self.is_picking = False
        self.brush_mode_active = False

        # 清理曲线编辑状态（保留选中形状）
        active_id = self.active_shape_id
        selected = self.scene.selectedItems()
        self._cleanup_curve_editing_state()
        self.active_shape_id = active_id
        for item in selected:
            item.setSelected(True)

        # 隐藏笔刷光标
        self.brush_cursor.setVisible(False)

        # 清理 SAM 点
        self.clear_sam_points()

        # 恢复默认视图模式
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def set_mask_creation_mode(self, mode: str) -> None:
        """Choose curved pen or corner-only polygon creation for curve masks."""
        if mode not in ("curve", "polygon"):
            raise ValueError(f"Unsupported mask creation mode: {mode}")
        self._mask_creation_mode = mode
        self.pen_dragging = False
        self.pen_start_pos = None
        self.set_drawing_mode(True)

    def set_drawing_mode(self, enabled: bool) -> None:
        """Toggle between Pen drawing and standard Hand pan modes."""
        self.is_drawing = enabled
        if enabled:
            self.is_picking = False
            # Use NoDrag by default in drawing mode to allow custom pen logic.
            # Shift key will trigger RubberBandDrag in mousePressEvent.
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            # 清理曲线编辑状态，防止切换工具后残留显示
            self._cleanup_curve_editing_state()
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def _cleanup_curve_editing_state(self) -> None:
        """清理曲线编辑的临时状态，防止切换工具后残留显示。
        
        当用户从曲线工具切换到其他工具时，必须调用此方法：
        1. 清除活跃形状 ID
        2. 清除所有曲线形状的选中节点索引
        3. 隐藏动态创建的 node 和 ctrl handles
        4. 清除场景选择状态
        5. 触发重绘以清除残留的控制线
        """
        # 清除活跃形状 ID
        self.active_shape_id = None
        
        # 清除所有曲线形状的选中节点索引，并隐藏动态 handles
        for item in self.mask_items.values():
            if item.shape_type == "curve":
                item.selected_node_idx = -1
                # 隐藏所有动态创建的 node 和 ctrl handles
                for key, handle in list(item.handles.items()):
                    if isinstance(key, str) and (key.startswith('node_') or key.startswith('ctrl_')):
                        handle.setVisible(False)
                # Fix: 触发重绘以清除残留的控制线
                item.update()
        
        # 清除场景选择状态
        self.scene.clearSelection()

    def set_picking_mode(self, enabled) -> None:
        """Toggle color picking mode."""
        self.is_picking = enabled
        if enabled:
            # 清理曲线编辑状态
            self._cleanup_curve_editing_state()
            self.is_drawing = False
            self.sam_mode_active = False
            self.hsl_picker_mode = False  # Disable HSL picker when regular picker is active
            self.inspector_mode_active = False
            self.color_inspector.hide()
            self.set_crop_mode(False)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def set_hsl_picker_mode(self, enabled) -> None:
        """Toggle HSL Curves color picker mode (eyedropper for adding control points)."""
        self.hsl_picker_mode = enabled
        if enabled:
            # Disable other modes
            self._cleanup_curve_editing_state()
            self.is_drawing = False
            self.sam_mode_active = False
            self.is_picking = False  # Disable regular picker
            self.curves_picker_mode = False  # Disable RGB Curves picker
            self.set_crop_mode(False)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            # Use CrossCursor for HSL picker (eyedropper-like)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def set_curves_picker_mode(self, enabled) -> None:
        """Toggle RGB Curves color picker mode (eyedropper for adding control points)."""
        self.curves_picker_mode = enabled
        if enabled:
            # Disable other modes
            self._cleanup_curve_editing_state()
            self.is_drawing = False
            self.sam_mode_active = False
            self.is_picking = False  # Disable regular picker
            self.hsl_picker_mode = False  # Disable HSL Curves picker
            self.set_crop_mode(False)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            # Use CrossCursor for RGB Curves picker (eyedropper-like)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def set_crop_mode(self, enabled) -> None:
        """Toggle professional cropping tool with Lightroom-style rotation.
        
        Key principle: CropRectItem NEVER calls setRotation() - it stays at rotation = 0.
        The rotation is stored in rotation_val (logical rotation) and applied by the
        GPU rendering pipeline, not the Qt item itself.
        """
        if self.engine:
            self.engine.set_interaction_mode(enabled, "crop" if enabled else "general")

        if enabled:
            self.is_drawing = False
            self.is_picking = False
            self.sam_mode_active = False
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            
            if not self.crop_item:
                self.crop_item = CropRectItem(self)
                self.scene.addItem(self.crop_item)
            
            # Initialize crop rect to current image bounds or existing crop
            if self.logical_size:
                w, h = self.logical_size.width(), self.logical_size.height()
                
                # Task 4 Fix: Use center-based logic to interpret restored crop params
                # If engine has crop params, use them, otherwise default to full screen
                if self.engine and self.engine.crop_params and self.engine.crop_params.get('rect') != (0.0, 0.0, 1.0, 1.0):
                    cp = self.engine.crop_params
                    nx, ny, nw, nh = cp['rect']
                    
                    # Calculate center X/Y based on the stored rect using center-based logic
                    # cx = x + w/2, cy = y + h/2 (in normalized coordinates)
                    cx = (nx + nw / 2) * w
                    cy = (ny + nh / 2) * h
                    
                    self.crop_item.setPos(cx, cy)
                    self.crop_item.rect = QRectF(-nw * w / 2, -nh * h / 2, nw * w, nh * h)
                    self.crop_item.rotation_val = cp.get('rotation', 0)
                    # IMPORTANT: Do NOT call setRotation() - CropRectItem stays at rotation = 0
                    # The rotation is handled logically and rendered by GPU
                else:
                    # Default to full image bounds
                    self.crop_item.setPos(w / 2, h / 2)
                    self.crop_item.rect = QRectF(-w / 2, -h / 2, w, h)
                    self.crop_item.rotation_val = 0
                    # IMPORTANT: Do NOT call setRotation() - CropRectItem stays at rotation = 0
                
                self.crop_item._update_handles()
            
            self.crop_item.setVisible(True)
        else:
            if self.crop_item:
                self.crop_item.setVisible(False)
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def get_crop_data(self):
        """Get crop data using normalized center coordinates.
        
        Returns a dictionary with:
        - center: (norm_cx, norm_cy) - normalized center position
        - size: (norm_w, norm_h) - normalized dimensions
        - rotation: current rotation in degrees
        - rect: calculated unrotated top-left tuple (cx - w/2, cy - h/2, w, h) for backward compatibility
        """
        if not self.crop_item or not self.logical_size: return None
        
        w, h = self.logical_size.width(), self.logical_size.height()
        
        # Task 2 Fix: Use center-based coordinates instead of calculating meaningless top-left of rotated rect
        # Get the geometric center from the item's scene position
        center_pos = self.crop_item.pos()
        
        # Get dimensions from the rect
        rect_w = self.crop_item.rect.width()
        rect_h = self.crop_item.rect.height()
        
        # Normalize everything against logical_size
        norm_cx = center_pos.x() / w
        norm_cy = center_pos.y() / h
        norm_w = rect_w / w
        norm_h = rect_h / h
        
        # Calculate unrotated top-left for backward compatibility (cx - w/2, cy - h/2)
        unrotated_tl_x = norm_cx - norm_w / 2
        unrotated_tl_y = norm_cy - norm_h / 2
        
        return {
            "center": (norm_cx, norm_cy),
            "size": (norm_w, norm_h),
            "rotation": self.crop_item.rotation_val,
            "rect": (unrotated_tl_x, unrotated_tl_y, norm_w, norm_h)
        }

    def reset_crop_to_image(self) -> None:
        """Reset the crop box to the full image view.
        
        Actions:
        1. Set position to image center (w/2, h/2)
        2. Set rect to full image size (-w/2, -h/2, w, h)
        3. Set rotation_val to 0.0 (logical, NOT setRotation())
        4. Reset aspect_ratio to None
        5. Clear engine crop_params to prevent old params from being reapplied
        6. Clear _crop_preview_angle for GPU rendering
        7. Call _update_handles() and viewport().update()
        """
        # Safety check: ensure logical_size exists
        if not self.logical_size or not self.crop_item:
            return
        
        w, h = self.logical_size.width(), self.logical_size.height()
        
        # Reset crop box to full image state
        self.crop_item.setPos(w / 2, h / 2)
        self.crop_item.rect = QRectF(-w / 2, -h / 2, w, h)
        self.crop_item.rotation_val = 0.0
        # IMPORTANT: Do NOT call setRotation() - CropRectItem stays at rotation = 0
        self.crop_item.aspect_ratio = None
        
        # Clear GPU preview angle (critical for preventing double rotation bug)
        self._crop_preview_angle = 0.0
        
        # Clear engine crop_params to prevent old params from being reapplied
        if self.engine:
            self.engine.crop_params = {
                "rect": (0.0, 0.0, 1.0, 1.0),
                "rotation": 0.0,
            }
        
        self.crop_item._update_handles()
        self.viewport().update()

    def confirm_crop(self) -> None:
        """确认裁切：清零预览角度、隐藏裁切框、刷新视图。
        
        此方法应在裁切操作被正式确认/应用后调用，确保：
        1. GPU 预览角度清零，避免对已裁切图像进行二次旋转
        2. 裁切框被隐藏
        3. 视图刷新以显示最终结果
        """
        # 1. 清零 GPU 预览角度（关键修复：防止二次旋转 Bug）
        self._crop_preview_angle = 0.0
        
        # 2. 清零 crop_item 的逻辑旋转值
        if self.crop_item:
            self.crop_item.rotation_val = 0.0
        
        # 3. 清零引擎中的裁切旋转参数
        if self.engine and self.engine.crop_params:
            self.engine.crop_params['rotation'] = 0.0
        
        # 4. 隐藏裁切框（退出裁切模式）
        self.set_crop_mode(False)
        
        # 5. 触发渲染更新
        self.viewport().update()

    def set_crop_params(self, params) -> None:
        """Update crop tool from panel parameters.

        Key principle: CropRectItem NEVER calls setRotation() - it stays at rotation = 0.
        The rotation is stored in rotation_val and applied by GPU rendering pipeline.

        Bug Fix: 当 aspect_ratio 或 rotation 改变时，必须调用 _apply_crop_constraints()
        确保裁切框在当前旋转角度和比例下合法存在，防止裁切框超出图像边界。
        """
        # If no crop_item, we're done
        if not self.crop_item:
            return

        # Track if aspect_ratio changed
        old_aspect_ratio = self.crop_item.aspect_ratio
        
        # 1. Update Aspect Ratio
        aspect_str = params.get("aspect", "Free")
        if aspect_str == "Free":
            self.crop_item.aspect_ratio = None
        elif aspect_str == "Original":
            if self.logical_size:
                self.crop_item.aspect_ratio = self.logical_size.width() / self.logical_size.height()
        elif aspect_str == "Custom":
            # 面板 Custom 模式带 custom_width/custom_height（QSpinBox 范围
            # 1..9999，无除零）；缺失时退回 Free 并告警
            try:
                cw = float(params["custom_width"])
                ch = float(params["custom_height"])
                self.crop_item.aspect_ratio = cw / ch
            except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
                logger.warning("Custom crop aspect missing/invalid size "
                               "fields (%r), falling back to Free: %s",
                               params, exc)
                self.crop_item.aspect_ratio = None
        else:
            try:
                parts = aspect_str.split(':')
                if len(parts) == 2:
                    self.crop_item.aspect_ratio = float(parts[0]) / float(parts[1])
                else:
                    self.crop_item.aspect_ratio = float(aspect_str)
            except (ValueError, TypeError, ZeroDivisionError, AttributeError) as exc:
                # AUD-25204: 此前的裸 except 会吞掉一切异常（含
                # KeyboardInterrupt/SystemExit），宽高比静默失效且无日志。
                # 收窄为解析期可预期的异常并记录原始值。
                # - ValueError/TypeError: 非数值字符串 / aspect_str 非 str
                # - ZeroDivisionError: "N:0" 形式的除零
                # - AttributeError: 非字符串输入缺 .split
                logger.warning("Invalid crop aspect ratio %r, falling back "
                               "to Free: %s", aspect_str, exc)
                self.crop_item.aspect_ratio = None

        # Check if aspect_ratio actually changed
        aspect_changed = (old_aspect_ratio != self.crop_item.aspect_ratio)

        # 2. Update Rotation - logical value only, NO setRotation() call
        rotation = params.get("rotation", 0.0)
        rotation_changed = abs(self.crop_item.rotation_val - rotation) > 0.01
        
        if rotation_changed:
            self.crop_item.rotation_val = rotation
            # IMPORTANT: Do NOT call setRotation() - CropRectItem stays at rotation = 0
            
            # Update engine for GPU rendering
            if self.engine and self.engine.crop_params:
                self.engine.crop_params['rotation'] = rotation
                self.engine.render_pipeline()

        # 3. 【关键修复】当 aspect_ratio 或 rotation 改变时，统一调用约束方法
        # 确保裁切框在当前旋转角度和比例下合法存在
        if aspect_changed or rotation_changed:
            self.crop_item._apply_crop_constraints()

        # 4. Trigger an update to snap current rect to new aspect (only for fixed aspect mode)
        if self.crop_item.aspect_ratio and aspect_changed:
            self.crop_item.on_handle_moved(self.crop_item.handles['br'], self.crop_item.handles['br'].pos())
        
        self.viewport().update()

    def set_crop_aspect(self, aspect_str) -> None:
        # Legacy method kept for compatibility or internal use
        self.set_crop_params({"aspect": aspect_str})
    
    def set_crop_constrain(self, constrain: bool) -> None:
        """设置裁切框是否限制在图像范围内。
        
        Args:
            constrain: True 为限制在画内（默认），False 为允许自由拖拽出画外（画布扩展）
        """
        if self.crop_item:
            self.crop_item.set_crop_constrain(constrain)

    def clear_sam_points(self) -> None:
        """Remove all SAM point markers from the scene."""
        for point_item in self.sam_points_ui:
            if point_item.scene() == self.scene:
                self.scene.removeItem(point_item)
        self.sam_points_ui.clear()

    def undo_sam_point_ui(self) -> bool:
        """Remove the last SAM point marker from the scene.
        
        Returns:
            bool: True if a point was removed, False if no points exist.
        """
        if self.sam_points_ui:
            point_item = self.sam_points_ui.pop()
            if point_item.scene() == self.scene:
                self.scene.removeItem(point_item)
            return True
        return False

    def set_sam_mode(self, enabled) -> None:
        """Toggle SAM Smart Brush mode."""
        self.sam_mode_active = enabled
        if enabled:
            # 清理曲线编辑状态
            self._cleanup_curve_editing_state()
            self.is_drawing = False
            self.is_picking = False
            self.sam_ready = False  # Reset ready state on activation
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.WaitCursor)  # Show wait cursor during encoding
        else:
            self.clear_sam_points()  # Clear visual markers when exiting SAM mode
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self.sam_ready = False  # Reset ready state on deactivation

    def set_sam_ready(self, ready: bool) -> None:
        """Set SAM ready state and update cursor accordingly.
        
        Args:
            ready: True if SAM encoding is complete and clicks are allowed.
        """
        self.sam_ready = ready
        if ready and self.sam_mode_active:
            # Switch from wait cursor to cross cursor for target selection
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def set_brush_mode(self, enabled) -> None:
        """Toggle Brush drawing mode for mask painting."""
        self.brush_mode_active = enabled
        if enabled:
            # 清理曲线编辑状态
            self._cleanup_curve_editing_state()
            # Disable other modes
            self.is_drawing = False
            self.is_picking = False
            self.sam_mode_active = False
            self.set_crop_mode(False)
            
            # Setup brush mode
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.brush_cursor.setVisible(True)
            self.brush_cursor.set_params(
                self._brush_params["size"],
                self._brush_params["feather"]
            )
            self.viewport().setCursor(Qt.CursorShape.BlankCursor)  # Hide system cursor
        else:
            self.brush_cursor.setVisible(False)
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            # Finish any ongoing stroke
            if self._brush_stroke_points:
                self._finish_brush_stroke()

    def set_brush_eraser_mode(self, is_eraser: bool) -> None:
        """Toggle between brush (add) and eraser (subtract) mode."""
        self._brush_params["is_eraser"] = is_eraser
        self.brush_cursor.set_eraser_mode(is_eraser)

    def set_brush_params(self, params: dict) -> None:
        """Update brush parameters from external source (mask panel).

        AUD-25213: 外部驱动的更新不再无条件回发 ``brush_params_updated``
        （信号回声——面板→canvas→控制器→面板的环是否成立此前完全依赖
        下游记得 blockSignals）。本方法只单向吸收外部参数；由画布内部
        发起的变化（快捷键）仍经 keyPressEvent 显式回发。另加实例状态
        锁防递归：回发槽若再进入本方法直接忽略。
        """
        if self._brush_params_syncing:
            return
        self._brush_params_syncing = True
        try:
            self._brush_params.update(params)
            self.brush_cursor.set_params(
                self._brush_params["size"],
                self._brush_params["feather"]
            )
            # Update stroke preview overlay setting
            if "show_overlay" in params:
                self.brush_stroke_preview.set_show_overlay(params["show_overlay"])
                self.scene.update()  # 触发已绘制笔触的重绘
        finally:
            self._brush_params_syncing = False

    def _handle_brush_press(self, pos) -> None:
        """Handle mouse press for brush drawing.
        
        Shift key temporarily switches to eraser mode.
        """
        if not self.logical_size:
            return
        
        # 检测 Shift 键：临时切换为橡皮擦模式
        modifiers = QApplication.keyboardModifiers()
        is_shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        
        # 确定当前是否为橡皮擦模式（Shift 优先级高于按钮状态）
        # 强制转换为布尔值，防止 KeyboardModifier 等 PyQt 对象被错误存储
        is_eraser = bool(is_shift or self._brush_params.get("is_eraser", False))
        
        # 更新光标和预览的橡皮擦状态
        self.brush_cursor.set_eraser_mode(is_eraser)
        
        # 保存临时橡皮擦状态
        self._temp_eraser_mode = is_shift
        
        self._last_brush_pos = pos
        self._brush_stroke_points = [(pos.x(), pos.y())]
        
        # 终极解决方案：直接将屏幕像素距离映射为场景物理距离，彻底免疫所有缩放和DPI带来的换算误差
        from PyQt6.QtCore import QPoint
        p1 = self.mapToScene(QPoint(0, 0))
        p2 = self.mapToScene(QPoint(int(self._brush_params["size"]), 0))
        self._current_scene_size = QLineF(p1, p2).length()
        
        self.interaction_started.emit()
        
        # Show and start stroke preview with locked scene size
        self.brush_stroke_preview.setVisible(True)
        self.brush_stroke_preview.start_stroke(
            (pos.x(), pos.y()), 
            self._current_scene_size * 2, 
            is_eraser
        )

    def _handle_brush_move(self, pos) -> None:
        """Handle mouse move for brush drawing with incremental interpolation.
        
        Note: Brush mode (add/eraser) is locked at stroke start in _handle_brush_press
        and stored in brush_stroke_preview.is_eraser. Do NOT recalculate during drag.
        """
        if not self._brush_stroke_points:
            return
        
        last_pos = getattr(self, '_last_brush_pos', pos)
        dx = pos.x() - last_pos.x()
        dy = pos.y() - last_pos.y()
        distance = (dx**2 + dy**2)**0.5
        
        scene_size = getattr(self, '_current_scene_size', self._brush_params["size"])
        min_spacing = max(1.0, scene_size / 15.0)
        
        if distance >= min_spacing:
            pt = (pos.x(), pos.y())
            self._brush_stroke_points.append(pt)
            self._last_brush_pos = pos
            self.brush_stroke_preview.add_points([pt])

    def _finish_brush_stroke(self) -> None:
        """Finish current brush stroke and emit signal.
        
        Ensures the correct eraser state is included in params.
        """
        if not self._brush_stroke_points or not self.logical_size:
            return
        
        w, h = self.logical_size.width(), self.logical_size.height()
        norm_points = [(p[0] / w, p[1] / h) for p in self._brush_stroke_points]
        
        # 获取最终参数，包含正确的橡皮擦状态
        final_params = self._brush_params.copy()
        
        # 如果存在临时橡皮擦模式（Shift 键），确保传递正确的状态
        # 注意：绘制过程中 Shift 状态可能已改变，使用笔触预览的最终状态
        final_params["is_eraser"] = self.brush_stroke_preview.is_eraser
        
        # 直接使用锁定的绝对尺寸保存，保证绘制预览与最终蒙版 100% 吻合
        final_params["size"] = getattr(self, '_current_scene_size', self._brush_params["size"])
        
        # Emit the finished stroke
        self.brush_stroke_added.emit(norm_points, final_params)
        self._brush_stroke_points = []
        
        # Hide and clear stroke preview
        self.brush_stroke_preview.setVisible(False)
        self.brush_stroke_preview.clear_stroke()
        
        # 恢复光标到原始橡皮擦按钮状态
        self.brush_cursor.set_eraser_mode(self._brush_params.get("is_eraser", False))
        
        self.interaction_ended.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # CV-B09：状态栏定位/折叠统一由 update_video_control_bar_position
        # 裁决（预留 transport 高度的安全区 + 窄窗折叠），此处不再单独
        # 搬移一份（旧路径不预留 footer，随后又被 update 覆盖）。

        # Position viewport controls at top-left
        self.controls.move(10, 10)

        # Update video control bar position
        self.update_video_control_bar_position()

        if self._needs_initial_fit and self.viewport().width() > 100:
            self.fit_to_window()
            self._needs_initial_fit = False
        self._update_scene_pan_bounds()
        # Update LOD and re-render
        if self._update_lod():
            if not self._refresh_video_frame_view():
                if self.engine:
                    self.engine.render_pipeline(render_reason='viewport_refresh')

    def showEvent(self, event) -> None:
        """Handle show event - position video control bar."""
        super().showEvent(event)
        self.update_video_control_bar_position()

    def update_video_control_bar_position(self) -> None:
        """Update video control bar position to bottom of canvas."""
        if hasattr(self, 'video_control_bar') and self.video_control_bar.isVisible():
            bar = self.video_control_bar
            bar_width = self.width()
            bar_height = bar.height()
            # 固定在底部，左对齐，宽度匹配 Canvas
            bar.setGeometry(0, self.height() - bar_height, bar_width, bar_height)
        status = getattr(self, "color_status_bar", None)
        if status is not None and not status.isHidden():
            transport = getattr(self, "video_control_bar", None)
            reserved = transport.height() if transport is not None and not transport.isHidden() else 0
            # CV-B09：状态栏按实际可用宽度折叠（文字进 tooltip，保留
            # provider 诊断），再以 transport 之下的安全区定位。
            apply_width = getattr(status, "apply_available_width", None)
            if callable(apply_width):
                apply_width(self.viewport().width())
            status.move(
                max(8, (self.viewport().width() - status.width()) // 2),
                max(4, self.viewport().height() - reserved - status.height() - 4))

    @staticmethod
    def _should_present_renderer_front_texture(renderer: object) -> bool:
        """Return whether the renderer's front texture is safe to present.

        A timeline-clip switch keeps the previous front texture alive until
        the requested master frame is uploaded.  During that interval it is a
        valid GL texture, but it belongs to the previous clip and must not be
        painted as the newly selected clip's still preview.
        """
        return not bool(getattr(renderer, "video_texture_pending", False))

    def drawBackground(self, painter, rect) -> None:
        # P4-2 SAFETY: drawBackground 必须只使用已完成的 front texture
        # 绝对禁止在此方法中调用 render_pipeline()，否则会形成：
        # paint -> submit render -> frame_ready -> repaint -> paint 循环
        # 这是视频卡顿和抖动的常见根因。
        self._in_draw_background = True
        _diag_blit_tex = None
        _diag_pending = None
        try:
            # Never mix the QGraphics timeline pixmap with the native OpenGL
            # source texture.  Besides producing the one-pixel seams visible
            # around a fitted frame, that mix is unsafe on macOS while the GL
            # front texture is replaced during a clip switch.
            if getattr(self, "_timeline_preview_active", False):
                # QGraphicsView's default background brush is not guaranteed
                # to clear a QOpenGLWidget framebuffer.  Explicitly paint the
                # whole exposed rect; otherwise the uninitialised GL buffer
                # leaks as bright-green bands around the fitted QImage.
                painter.fillRect(rect, QColor(25, 25, 25))
                self._record_presented_preview_frame()
                return
            if self.engine and self.engine.renderer:
                dpr = self.gl_viewport.devicePixelRatio()
                vw, vh = int(self.gl_viewport.width() * dpr), int(self.gl_viewport.height() * dpr)
                renderer = self.engine.renderer
                tex_id = renderer.current_front_tex_id

                painter.beginNativePainting()
                if (
                    tex_id is not None
                    and tex_id > 0
                    and self._should_present_renderer_front_texture(renderer)
                ):
                    did_draw = self.engine._draw_background_tex_id(
                        tex_id,
                        (vw, vh),
                        rotation_angle=self._crop_preview_angle,
                    )
                    if did_draw:
                        _diag_blit_tex = int(tex_id)
                        self._record_presented_preview_frame()
                else:
                    _diag_pending = bool(getattr(renderer, 'video_texture_pending', False))
                painter.endNativePainting()
            else:
                super().drawBackground(painter, rect)
        finally:
            self._in_draw_background = False
            # Flash diagnostics: record what the viewer actually blitted.
            # Log only state changes (or the first paint) so the trace stays
            # small while still catching a one-frame foreign blit.
            if _flash_diag.enabled():
                _key = (_diag_blit_tex, _diag_pending)
                if getattr(self, '_diag_last_blit_key', None) != _key:
                    self._diag_last_blit_key = _key
                    _flash_diag.event(
                        'blit', tex=_diag_blit_tex, pending=_diag_pending,
                    )

    def _record_presented_preview_frame(self) -> None:
        """Record a frame after the Color preview has painted it."""
        toolbar = getattr(self, "viewport_menu", None)
        bar = getattr(self, "video_control_bar", None)
        if toolbar is None or bar is None or not bar.is_playing:
            return
        toolbar.record_presented_frame(bar.current_frame)
