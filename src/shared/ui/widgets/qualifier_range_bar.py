from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QRectF, pyqtSignal, QPointF
from PyQt6.QtGui import QPainter, QLinearGradient, QColor, QBrush, QPen

class QualifierRangeBar(QWidget):
    # Signals for interaction
    # For sat/lum: (low, high, s_low, s_high)
    range_changed = pyqtSignal(float, float, float, float)
    # For hue: (center, width, soft)
    hue_changed = pyqtSignal(float, float, float)
    
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, type="hue", parent=None) -> None:
        super().__init__(parent)
        self.type = type # "hue", "sat", "lum"
        self.setFixedHeight(18)
        self.setMouseTracking(True)
        
        # Range parameters [0, 1]
        self.low = 0.0
        self.high = 1.0
        self.soft_low = 0.0
        self.soft_high = 0.0
        
        # For Hue: center and width
        self.center = 0.5
        self.hue_width = 0.2
        self.soft = 0.1
        
        # Interaction state
        self.active_handle = None 
        self.mouse_pressed = False
        self.hover_handle = None

    def set_range(self, low, high, soft_low=0.0, soft_high=0.0):
        self.low = low
        self.high = high
        self.soft_low = soft_low
        self.soft_high = soft_high
        self.update()

    def set_hue_params(self, center, width, soft):
        self.center = center
        self.hue_width = width
        self.soft = soft
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        rect = self.rect().toRectF()
        
        # 1. Draw Background Gradient
        self.draw_background(painter, rect)
        
        # 2. Draw Selection Overlay
        self.draw_overlay(painter, rect)
        
        # 3. Draw Border
        painter.setPen(QPen(QColor(40, 40, 40), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))

    def draw_background(self, painter, rect):
        grad = QLinearGradient(rect.left(), 0, rect.right(), 0)
        
        if self.type == "hue":
            # Rainbow gradient for Hue (Shifted by 0.5 to keep Red in center)
            # 0.0=Cyan, 0.5=Red, 1.0=Cyan
            for i in range(13):
                pos = i / 12.0
                hue_val = (pos + 0.5) % 1.0
                color = QColor.fromHsvF(hue_val, 0.9, 0.7)
                grad.setColorAt(pos, color)
        elif self.type == "sat":
            grad.setColorAt(0, QColor(60, 60, 60))
            grad.setColorAt(1, QColor(0, 160, 255))
        else: # lum
            grad.setColorAt(0, QColor(20, 20, 20))
            grad.setColorAt(1, QColor(240, 240, 240))
            
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(rect)

    def draw_overlay(self, painter, rect):
        w = rect.width()
        h = rect.height()
        
        # Colors
        dim_color = QColor(0, 0, 0, 160)
        highlight_pen = QPen(QColor(255, 140, 0), 1.5)
        soft_brush = QBrush(QColor(255, 140, 0, 60))
        
        if self.type == "hue":
            half_w = self.hue_width * 0.5
            center_x = self.center * w
            start_x = ((self.center - half_w) % 1.0) * w
            end_x = ((self.center + half_w) % 1.0) * w
            soft_px = self.soft * 0.5 * w
            
            # Draw Dimmed Unselected Areas
            painter.setBrush(dim_color)
            painter.setPen(Qt.PenStyle.NoPen)
            if start_x < end_x:
                painter.drawRect(QRectF(0, 0, start_x, h))
                painter.drawRect(QRectF(end_x, 0, w - end_x, h))
            else:
                painter.drawRect(QRectF(end_x, 0, start_x - end_x, h))

            # Draw Softness (as gradient or semi-transparent)
            if self.soft > 0:
                painter.setBrush(soft_brush)
                # Simple visualization of softness boundaries
                if start_x < end_x:
                    painter.drawRect(QRectF(start_x - soft_px, 0, soft_px, h))
                    painter.drawRect(QRectF(end_x, 0, soft_px, h))
                else:
                    painter.drawRect(QRectF(start_x - soft_px, 0, soft_px, h))
                    painter.drawRect(QRectF(end_x, 0, soft_px, h))

            # Draw Main Range Highlight
            painter.setPen(highlight_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if start_x < end_x:
                painter.drawRect(QRectF(start_x, 0, end_x - start_x, h))
            else:
                painter.drawRect(QRectF(start_x, 0, w - start_x, h))
                painter.drawRect(QRectF(0, 0, end_x, h))
                
            # Draw Center Line
            painter.setPen(QPen(QColor(255, 255, 255, 180), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(center_x, 0), QPointF(center_x, h))

        else: # Sat / Lum
            low_x = self.low * w
            high_x = self.high * w
            sl_x = self.soft_low * w
            sh_x = self.soft_high * w

            # Dimming
            painter.setBrush(dim_color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(QRectF(0, 0, low_x - sl_x, h))
            painter.drawRect(QRectF(high_x + sh_x, 0, w - (high_x + sh_x), h))

            # Softness regions
            painter.setBrush(soft_brush)
            if sl_x > 0:
                painter.drawRect(QRectF(low_x - sl_x, 0, sl_x, h))
            if sh_x > 0:
                painter.drawRect(QRectF(high_x, 0, sh_x, h))

            # Highlight
            painter.setPen(highlight_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(low_x, 0, high_x - low_x, h))

    def _get_handle_at(self, pos):
        w = self.width()
        if w <= 0: return None
        x = pos.x() / w
        
        tol = 0.03 # Tolerance
        
        if self.type == "hue":
            half_w = self.hue_width * 0.5
            start = (self.center - half_w) % 1.0
            end = (self.center + half_w) % 1.0
            
            # Check boundaries (circular)
            def dist_c(a, b):
                d = abs(a - b)
                return min(d, 1.0 - d)

            if dist_c(x, start) < tol: return "start"
            if dist_c(x, end) < tol: return "end"
            if dist_c(x, self.center) < tol: return "center"
            
            # Softness handles
            soft_val = self.soft * 0.5
            if dist_c(x, (start - soft_val) % 1.0) < tol: return "soft_start"
            if dist_c(x, (end + soft_val) % 1.0) < tol: return "soft_end"
            
        else:
            if abs(x - self.low) < tol: return "low"
            if abs(x - self.high) < tol: return "high"
            if abs(x - (self.low - self.soft_low)) < tol: return "soft_low"
            if abs(x - (self.high + self.soft_high)) < tol: return "soft_high"
            if x > self.low and x < self.high: return "range"

        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouse_pressed = True
            self.active_handle = self._get_handle_at(event.pos())
            
            # If nothing hit, move center/range to click
            if not self.active_handle:
                val = max(0.0, min(1.0, event.pos().x() / self.width()))
                if self.type == "hue":
                    self.center = val
                    self.active_handle = "center"
                else:
                    # Move whole range to center on click
                    range_w = self.high - self.low
                    self.low = max(0.0, min(1.0 - range_w, val - range_w * 0.5))
                    self.high = self.low + range_w
                    self.active_handle = "range"
            
            self.interaction_started.emit()
            self._handle_mouse(event.pos())
            self.update()

    def mouseMoveEvent(self, event):
        self.hover_handle = self._get_handle_at(event.pos())
        if self.hover_handle:
            if self.hover_handle in ["center", "range"]:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        if self.mouse_pressed:
            self._handle_mouse(event.pos())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouse_pressed = False
            self.active_handle = None
            self.interaction_ended.emit()
            self.update()

    def _handle_mouse(self, pos):
        w = self.width()
        if w <= 0: return
        val = max(0.0, min(1.0, pos.x() / w))
        
        if self.type == "hue":
            if self.active_handle == "center":
                self.center = val
            elif self.active_handle == "start":
                # Adjust width via start edge
                diff = self.center - val
                while diff > 0.5: diff -= 1.0
                while diff < -0.5: diff += 1.0
                self.hue_width = max(0.01, min(1.0, diff * 2.0))
            elif self.active_handle == "end":
                # Adjust width via end edge
                diff = val - self.center
                while diff > 0.5: diff -= 1.0
                while diff < -0.5: diff += 1.0
                self.hue_width = max(0.01, min(1.0, diff * 2.0))
            elif self.active_handle in ["soft_start", "soft_end"]:
                # Adjust softness
                half_w = self.hue_width * 0.5
                edge = (self.center + half_w) if self.active_handle == "soft_end" else (self.center - half_w)
                diff = abs(val - edge)
                if diff > 0.5: diff = 1.0 - diff
                self.soft = max(0.0, min(1.0, diff * 2.0))
            
            self.hue_changed.emit(self.center, self.hue_width, self.soft)
        else:
            if self.active_handle == "low":
                self.low = min(val, self.high - 0.01)
            elif self.active_handle == "high":
                self.high = max(val, self.low + 0.01)
            elif self.active_handle == "soft_low":
                self.soft_low = max(0.0, self.low - val)
            elif self.active_handle == "soft_high":
                self.soft_high = max(0.0, val - self.high)
            elif self.active_handle == "range":
                range_w = self.high - self.low
                self.low = max(0.0, min(1.0 - range_w, val - range_w * 0.5))
                self.high = self.low + range_w
            
            self.range_changed.emit(self.low, self.high, self.soft_low, self.soft_high)
