import math
import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QConicalGradient

class ColorWarper(QWidget):
    # Emits the grid point data as (delta_hue, delta_sat) offsets
    changed = pyqtSignal(np.ndarray)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(300, 220)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.grid_size = 6 # Default 6x6
        # AUD-38209: 内边距在构造即初始化为与 paintEvent 相同的值，
        # 保证首帧前命中测试/拖拽几何与绘制一致（旧代码 fallback 30 ≠ 35）。
        self._padding = 35
        self.reset_grid()

        self.dragging_point = None # (ring_idx, ray_idx)
        self.hover_point = None
        self._pending_deltas = None
        self._changed_timer = QTimer(self)
        self._changed_timer.setSingleShot(True)
        self._changed_timer.setInterval(16)
        self._changed_timer.timeout.connect(self._emit_pending_change)

    def set_grid_size(self, size):
        self.grid_size = size
        self.reset_grid()
        self.update()

    def _initial_position(self, r, t):
        """Compute the initial Cartesian position for ring r, angle index t."""
        radius = (r + 1) / self.grid_size
        angle = (t / self.grid_size) * 2 * math.pi
        visual_angle = -angle - math.pi / 2
        return np.array([radius * math.cos(visual_angle), radius * math.sin(visual_angle)], dtype=np.float32)

    def _compute_deltas(self):
        """Compute (delta_hue, delta_sat) for each grid point from current positions."""
        deltas = np.zeros((self.grid_size, self.grid_size, 2), dtype=np.float32)
        for r in range(self.grid_size):
            for t in range(self.grid_size):
                init = self._initial_position(r, t)
                cur = self.points[r, t]
                # Decode initial hue/sat
                init_hue = self._hue_from_pos(init)
                init_sat = np.linalg.norm(init)
                # Decode current hue/sat
                cur_hue = self._hue_from_pos(cur)
                cur_sat = np.linalg.norm(cur)
                # Delta (with hue wrap-around)
                dh = cur_hue - init_hue
                if dh > 0.5: dh -= 1.0
                if dh < -0.5: dh += 1.0
                ds = cur_sat - init_sat
                deltas[r, t] = [dh, ds]
        return deltas

    @staticmethod
    def _hue_from_pos(pos):
        """Decode hue from a Cartesian position (matches the shader encoding)."""
        PI = math.pi
        hue = (-math.atan2(pos[1], pos[0]) - PI / 2) / (2 * PI)
        return hue % 1.0

    def reset_grid(self):
        """Initializes the grid with uniform distribution."""
        # A reset or grid-size change must not later publish an obsolete drag
        # sample that was waiting for the 60 Hz coalescing timer.
        changed_timer = getattr(self, '_changed_timer', None)
        if changed_timer is not None:
            changed_timer.stop()
        self._pending_deltas = None
        self.points = np.zeros((self.grid_size, self.grid_size, 2), dtype=np.float32)

        for r in range(self.grid_size):
            for t in range(self.grid_size):
                self.points[r, t] = self._initial_position(r, t)

        # Emit identity deltas (all zeros) — no color change
        self.changed.emit(np.zeros((self.grid_size, self.grid_size, 2), dtype=np.float32))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        w, h = self.width(), self.height()
        # 使用统一的边距变量，方便后续调整
        self._padding = 35 
        side = min(w, h) - self._padding 
        radius = side / 2
        center = QPointF(w / 2, h / 2)

        # 1. Draw Background Color Wheel (faded)
        # QConicalGradient draws counter-clockwise.
        grad = QConicalGradient(center, 90)
        for i in range(7):
            grad.setColorAt(i/6, QColor.fromHsvF(i/6, 0.5, 0.4))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, radius, radius)

        # 2. Draw Grid Lines
        painter.setBrush(Qt.BrushStyle.NoBrush)
        
        # Rays
        painter.setPen(QPen(QColor(100, 100, 100, 150), 1))
        for t in range(self.grid_size):
            # Center to first ring
            p1 = center + QPointF(self.points[0, t][0], self.points[0, t][1]) * radius
            painter.drawLine(center, p1)
            # Connecting rings
            for r in range(self.grid_size - 1):
                p_curr = center + QPointF(self.points[r, t][0], self.points[r, t][1]) * radius
                p_next = center + QPointF(self.points[r+1, t][0], self.points[r+1, t][1]) * radius
                painter.drawLine(p_curr, p_next)

        # Rings
        for r in range(self.grid_size):
            painter.setPen(QPen(QColor(150, 150, 150, 180), 1))
            for t in range(self.grid_size):
                t_next = (t + 1) % self.grid_size
                p_curr = center + QPointF(self.points[r, t][0], self.points[r, t][1]) * radius
                p_next = center + QPointF(self.points[r, t_next][0], self.points[r, t_next][1]) * radius
                painter.drawLine(p_curr, p_next)

        # 3. Draw Nodes
        for r in range(self.grid_size):
            for t in range(self.grid_size):
                pos = center + QPointF(self.points[r, t][0], self.points[r, t][1]) * radius
                
                is_selected = (self.dragging_point == (r, t))
                is_hover = (self.hover_point == (r, t))
                is_outer = (r == self.grid_size - 1)
                
                color = QColor("#cc6600") if is_selected else (QColor("#ffffff") if is_hover else QColor("#aaaaaa"))
                
                # 最外圈控制点调大一点，方便操控
                base_size = 8 if is_outer else 5
                size = (base_size + 2) if (is_selected or is_hover) else base_size
                
                painter.setPen(QPen(Qt.GlobalColor.black, 1))
                painter.setBrush(QBrush(color))
                painter.drawEllipse(pos, size/2, size/2)

    def _get_point_at(self, pos):
        w, h = self.width(), self.height()
        # AUD-38209: 直接读取构造时初始化的内边距（与 paintEvent 一致）
        padding = self._padding
        side = min(w, h) - padding
        radius = side / 2
        center = QPointF(w / 2, h / 2)
        
        for r in range(self.grid_size):
            for t in range(self.grid_size):
                node_pos = center + QPointF(self.points[r, t][0], self.points[r, t][1]) * radius
                diff = pos - node_pos
                
                # 针对不同大小的控制点调整点击触发区域
                hit_radius = 10 if (r == self.grid_size - 1) else 8
                
                dist = abs(diff.x()) + abs(diff.y())
                if dist < hit_radius + 4:
                    d = math.sqrt(diff.x()**2 + diff.y()**2)
                    if d < hit_radius:
                        return (r, t)
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pt = self._get_point_at(event.position())
            if pt:
                self.dragging_point = pt
                self.interaction_started.emit()
                self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        
        if self.dragging_point:
            r, t = self.dragging_point
            w, h = self.width(), self.height()
            padding = self._padding
            side = min(w, h) - padding
            radius = side / 2
            center = QPointF(w / 2, h / 2)
            
            # New normalized position
            new_rel_x = (pos.x() - center.x()) / radius
            new_rel_y = (pos.y() - center.y()) / radius
            
            # Constrain magnitude
            mag = math.sqrt(new_rel_x**2 + new_rel_y**2)
            if mag > 1.1:
                new_rel_x /= (mag/1.1)
                new_rel_y /= (mag/1.1)

            # If dragging the outermost ring, move all points on this ray proportionally
            if r == self.grid_size - 1:
                # Calculate the delta movement from the original ray position
                # But since we update every frame, it's easier to calculate new polar coords
                # and apply to all rings
                new_angle = math.atan2(new_rel_y, new_rel_x)
                new_mag = math.sqrt(new_rel_x**2 + new_rel_y**2)
                
                for ring_idx in range(self.grid_size):
                    # Scale factor for internal rings
                    scale = (ring_idx + 1) / self.grid_size
                    # Apply the same angle and scaled magnitude
                    self.points[ring_idx, t] = [
                        new_mag * scale * math.cos(new_angle),
                        new_mag * scale * math.sin(new_angle)
                    ]
            else:
                self.points[r, t] = [new_rel_x, new_rel_y]
            
            self._pending_deltas = self._compute_deltas()
            if not self._changed_timer.isActive():
                self._changed_timer.start()
            self.update()
        else:
            # Hover check
            pt = self._get_point_at(pos)
            if pt != self.hover_point:
                self.hover_point = pt
                self.update()

    def mouseReleaseEvent(self, event):
        if self.dragging_point:
            self.dragging_point = None
            if self._changed_timer.isActive():
                self._changed_timer.stop()
                self._emit_pending_change()
            self.interaction_ended.emit()
            self.update()

    def _emit_pending_change(self):
        """Publish only the most recent grid state during a drag."""
        if self._pending_deltas is None:
            return
        self.changed.emit(self._pending_deltas)
        self._pending_deltas = None

    def reset(self):
        self.reset_grid()
        self.update()

    def apply_deltas(self, deltas):
        """Reconstruct Cartesian points from (delta_hue, delta_sat) offsets."""
        for r in range(self.grid_size):
            for t in range(self.grid_size):
                init = self._initial_position(r, t)
                init_hue = self._hue_from_pos(init)
                init_sat = np.linalg.norm(init)
                # Apply delta to get target hue/sat
                dh, ds = deltas[r, t]
                target_hue = (init_hue + dh) % 1.0
                target_sat = max(0.0, min(init_sat + ds, 1.0))
                # Convert back to Cartesian
                PI = math.pi
                visual_angle = -(target_hue * 2 * PI) - PI / 2
                self.points[r, t] = [target_sat * math.cos(visual_angle),
                                     target_sat * math.sin(visual_angle)]
