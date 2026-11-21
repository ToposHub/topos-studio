"""User-authored console controls, copied without paint changes from mixer_console.py.
Only the importable control classes are included; no main window or demo timer.
"""
import sys
import math
import random
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QScrollArea, QLabel, QPushButton, QLineEdit, QComboBox,
    QFrame, QSizePolicy, QColorDialog, QMenu
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QLinearGradient,
    QPainterPath, QFont, QAction, QHideEvent
)

from src.shared.ui.rotary_knob_style import paint_rotary_knob

# ============================================================================
# 1. 全局色彩系统与跨平台字体栈 (与 AudioClipEditor-UI.py 保持完全一致)
# ============================================================================
ACCENT_CYAN    = "#28D8F0"  # 主激活高亮 / 0dB 基准 / 声像
ACCENT_GREEN   = "#4CD490"  # 安全电平 / 信号激活 / 自动化 READ
ACCENT_AMBER   = "#F0A848"  # 警告电平 / Solo / 增益衰减 (GR)
ACCENT_RED     = "#F26D6D"  # 削波过载 (>0dBFS) / Mute / Record
ACCENT_BLUE    = "#6495FA"  # 辅助路由 / HPF 滤波
ACCENT_VIOLET  = "#B67CF8"  # Aux 发送 / 立体声宽度 / 总线

UI_FONT_CSS   = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"


def db_to_fader_pos(db: float, max_db: float = 12.0) -> float:
    """将 dB 值 (-60.0 ~ +12.0, -inf) 映射到推子归一化位置 (0.0 ~ 1.0)"""
    if db <= -60.0:
        return 0.0
    if db >= max_db:
        return 1.0
    # 分段曲线：让 0dB 位于约 75% 高度，提供极佳的常用音量区分辨率
    if db >= -10.0:
        return 0.55 + ((db + 10.0) / (max_db + 10.0)) * 0.45
    elif db >= -30.0:
        return 0.25 + ((db + 30.0) / 20.0) * 0.30
    else:
        return ((db + 60.0) / 30.0) * 0.25


def fader_pos_to_db(pos: float, max_db: float = 12.0) -> float:
    """将推子归一化位置 (0.0 ~ 1.0) 映射回 dB 值"""
    pos = max(0.0, min(1.0, pos))
    if pos == 0.0:
        return -float('inf')
    if pos >= 0.55:
        return -10.0 + ((pos - 0.55) / 0.45) * (max_db + 10.0)
    elif pos >= 0.25:
        return -30.0 + ((pos - 0.25) / 0.30) * 20.0
    else:
        return -60.0 + (pos / 0.25) * 30.0


def format_db(db: float) -> str:
    if db <= -60.0 or math.isinf(db):
        return "-∞"
    return f"{db:+.1f}" if db > 0 else f"{db:.1f}"


# ============================================================================
# 2. 自定义控件: EditableValuePill (达芬奇风格深黑内凹数值胶囊)
# ============================================================================
class EditableValuePill(QWidget):
    """悬停边框点亮，点击可直接键盘输入精确值"""
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = ACCENT_CYAN, height: int = 18, parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._hovered = False
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_editor_color()
        self._editor.hide()
        self._editor.returnPressed.connect(self._commit_edit)
        self._editor.editingFinished.connect(self._commit_edit)

    def set_accent_color(self, color: str) -> None:
        if color != self._accent:
            self._accent = color
            self._apply_editor_color()
            self.update()

    def _apply_editor_color(self) -> None:
        self._editor.setStyleSheet(f"""
            QLineEdit {{
                background-color: #0E0E11;
                color: {self._accent};
                border: 1px solid {self._accent};
                border-radius: 3px;
                font-family: {MONO_FONT_CSS};
                font-size: 9px;
                font-weight: 700;
                padding: 0px;
            }}
        """)

    def resizeEvent(self, event):
        self._editor.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)

    def setText(self, text: str):
        # 编辑中不覆盖用户正在键入的内容
        if not self._editor.isVisible():
            self._text = text
        self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            clean = self._text.replace("+", "").replace("dB", "").replace("%", "").strip()
            self._editor.setText("" if clean == "-∞" else clean)
            self._editor.show()
            self._editor.setFocus()
            self._editor.selectAll()
            self.update()
            event.accept()

    def _commit_edit(self):
        if self._editor.isVisible():
            val = self._editor.text()
            self._editor.hide()
            self.valueEdited.emit(val)
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        border_col = QColor(self._accent) if self._hovered else QColor("#121214")
        p.setPen(QPen(border_col, 1.0))
        p.setBrush(QColor("#151518"))
        p.drawRoundedRect(rect, 3.0, 3.0)

        p.setPen(QColor("#ECECF0"))
        font = QFont(MONO_FONT_FAM, 8)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


# ============================================================================
# 3. 自定义控件: RotaryKnob (复刻 ProRotaryKnob 哑光阳极铝旋钮)
# ============================================================================
class RotaryKnob(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        min_val: float = -100.0,
        max_val: float = 100.0,
        default_val: float = 0.0,
        bipolar: bool = True,
        accent_color: str = ACCENT_CYAN,
        size: int = 30,
        parent=None
    ):
        super().__init__(parent)
        self.min_val = min_val
        self.max_val = max_val
        self.default_val = default_val
        self.value = default_val
        self.bipolar = bipolar
        self.accent_color = QColor(accent_color)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

    def setValue(self, val: float, emit_signal: bool = True):
        val = max(self.min_val, min(self.max_val, val))
        if abs(val - self.value) > 1e-4:
            self.value = val
            self.update()
            if emit_signal:
                self.valueChanged.emit(self.value)

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_y = event.position().y()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            # 右键就地归位默认值
            self.setValue(self.default_val)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            dy = self._last_y - event.position().y()
            self._last_y = event.position().y()
            sensitivity = 0.2 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 1.0
            span = self.max_val - self.min_val
            self.setValue(self.value + (dy / 120.0) * span * sensitivity)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self.default_val)
            event.accept()

    def wheelEvent(self, event):
        step = (self.max_val - self.min_val) * 0.02
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            step *= 0.2
        delta = step if event.angleDelta().y() > 0 else -step
        self.setValue(self.value + delta)
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        norm = (self.value - self.min_val) / max(1e-6, self.max_val - self.min_val)
        paint_rotary_knob(
            p, QRectF(self.rect()), norm, self.accent_color,
            bipolar=self.bipolar, highlighted=self._hovered or self._dragging,
        )


# ============================================================================
# 4. 自定义控件: LevelMeter (立体声电平表 + 峰值保持 + 压缩衰减 GR 表)
# ============================================================================
class LevelMeter(QWidget):
    peakReset = pyqtSignal()

    def __init__(self, stereo: bool = True, show_gr: bool = True, parent=None):
        super().__init__(parent)
        self.stereo = stereo
        self.show_gr = show_gr
        base_w = 16 if stereo else 9
        self.setFixedWidth(base_w + (6 if show_gr else 0))
        self.setMinimumHeight(130)
        self.setToolTip("实时峰值电平表 & 压缩衰减 GR 表 (点击重置峰值保持与削波灯)")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.level_l = -60.0
        self.level_r = -60.0
        self.peak_l = -60.0
        self.peak_r = -60.0
        self.gr_db = 0.0  # 压缩器增益衰减量 (0.0 ~ -20.0 dB)
        self._hold_ticks_l = 0
        self._hold_ticks_r = 0
        self.clipped_l = False
        self.clipped_r = False

    def set_levels(self, db_l: float, db_r: float = None, gr_db: float = 0.0):
        """由外部音频引擎调用，更新瞬时电平与压缩衰减量 (gr_db <= 0)"""
        if db_r is None:
            db_r = db_l

        # 瞬时上升，平滑衰减
        self.level_l = max(db_l, self.level_l - 1.8)
        self.level_r = max(db_r, self.level_r - 1.8)
        # GR 表：压下去要快，松开要慢
        self.gr_db = min(0.0, max(-20.0, min(gr_db, self.gr_db + 0.8)))

        self._advance_peak_hold()
        if db_l >= self.peak_l:
            self.peak_l = db_l
            self._hold_ticks_l = 45  # 约 1.5 秒 (30fps)
        if db_r >= self.peak_r:
            self.peak_r = db_r
            self._hold_ticks_r = 45

        if db_l >= -0.1:
            self.clipped_l = True
        if db_r >= -0.1:
            self.clipped_r = True

        self.update()

    def decay_step(self):
        """定时衰落步进，确保无信号时平稳归零"""
        changed = False
        if self.level_l > -60.0:
            self.level_l = max(-60.0, self.level_l - 1.5)
            changed = True
        if self.level_r > -60.0:
            self.level_r = max(-60.0, self.level_r - 1.5)
            changed = True
        if self.gr_db < -0.05:
            self.gr_db = min(0.0, self.gr_db + 0.9)
            changed = True

        if self._advance_peak_hold() or changed:
            self.update()

    def _advance_peak_hold(self) -> bool:
        changed = False
        if self._hold_ticks_l > 0:
            self._hold_ticks_l -= 1
        elif self.peak_l > self.level_l:
            self.peak_l = max(self.level_l, self.peak_l - 1.2)
            changed = True

        if self._hold_ticks_r > 0:
            self._hold_ticks_r -= 1
        elif self.peak_r > self.level_r:
            self.peak_r = max(self.level_r, self.peak_r - 1.2)
            changed = True

        return changed

    def reset_clip(self) -> None:
        self.clipped_l = False
        self.clipped_r = False
        self.peak_l = self.level_l
        self.peak_r = self.level_r
        self._hold_ticks_l = self._hold_ticks_r = 0
        self.update()
        self.peakReset.emit()

    def clear_levels(self) -> None:
        self.level_l = self.level_r = -60.0
        self.gr_db = 0.0
        self._hold_ticks_l = self._hold_ticks_r = 0
        self.reset_clip()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_clip()
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = float(self.width()), float(self.height())
        clip_h = 5.0
        gap = 2.0
        meter_top = clip_h + gap
        meter_h = h - meter_top

        grad = QLinearGradient(0, h, 0, meter_top)
        grad.setColorAt(0.0, QColor(ACCENT_GREEN))
        grad.setColorAt(0.76, QColor(ACCENT_AMBER))
        grad.setColorAt(1.0, QColor(ACCENT_RED))

        channels = [
            (self.level_l, self.peak_l, self.clipped_l),
            (self.level_r, self.peak_r, self.clipped_r),
        ] if self.stereo else [
            (self.level_l, self.peak_l, self.clipped_l),
        ]

        gr_reserve = 6.0 if self.show_gr else 0.0
        ch_count = len(channels)
        bar_w = (w - gr_reserve - (ch_count - 1) * 2.0) / ch_count

        for idx, (lvl, pk, clipped) in enumerate(channels):
            x = idx * (bar_w + 2.0)

            # 1. 顶部 Clip LED (带外发光)
            if clipped:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 72, 72, 75))
                p.drawRoundedRect(QRectF(x - 0.5, 0.0, bar_w + 1.0, clip_h + 1.0), 1.5, 1.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FF5252") if clipped else QColor("#151518"))
            p.drawRoundedRect(QRectF(x, 0.5, bar_w, clip_h - 0.5), 1.2, 1.2)

            # 2. 深黑内凹电平槽
            track = QRectF(x, meter_top, bar_w, meter_h)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor("#0E0E11"))
            p.drawRoundedRect(track, 1.5, 1.5)

            # 3. 实时电平条
            norm_lvl = max(0.0, min(1.0, (lvl + 60.0) / 60.0))
            if norm_lvl > 0.01:
                fill_h = norm_lvl * (meter_h - 1.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(grad)
                p.drawRoundedRect(QRectF(x + 1.0, h - fill_h - 0.5, bar_w - 2.0, fill_h), 1.0, 1.0)

            # 4. 硬件 LED 栅格纹理
            p.setPen(QPen(QColor(9, 11, 15, 185), 1.0))
            y_grid = h - 3.5
            while y_grid > meter_top + 2.0:
                p.drawLine(QPointF(x + 1.0, y_grid), QPointF(x + bar_w - 1.0, y_grid))
                y_grid -= 3.5

            # 5. 峰值保持游标线
            if pk > -59.0:
                norm_pk = max(0.0, min(1.0, (pk + 60.0) / 60.0))
                pk_y = h - norm_pk * meter_h
                pk_col = QColor("#FFFFFF") if pk < -3.0 else QColor(ACCENT_RED)
                p.setPen(QPen(pk_col, 1.5))
                p.drawLine(
                    QPointF(x + 1.0, max(meter_top + 1.0, pk_y)),
                    QPointF(x + bar_w - 1.0, max(meter_top + 1.0, pk_y))
                )

        # 6. 右侧压缩器增益衰减 (GR) 指示条 (从顶部向下生长)
        if self.show_gr:
            gx = w - 4.0
            gr_track = QRectF(gx, meter_top, 4.0, meter_h)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor("#0E0E11"))
            p.drawRoundedRect(gr_track, 1.5, 1.5)

            if self.gr_db < -0.1:
                gr_norm = min(1.0, abs(self.gr_db) / 20.0)
                gr_h = gr_norm * (meter_h - 2.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(ACCENT_AMBER))
                p.drawRoundedRect(QRectF(gx + 0.8, meter_top + 1.0, 2.4, gr_h), 1.0, 1.0)


# ============================================================================
# 5. 自定义控件: StudioFader (Fairlight 凹面阳极铝推子 + 精密刻度尺)
# ============================================================================
class StudioFader(QWidget):
    dbChanged = pyqtSignal(float)
    dragStarted = pyqtSignal()
    dragFinished = pyqtSignal()

    def __init__(
        self, initial_db: float = 0.0, accent_color: str = ACCENT_CYAN,
        parent: QWidget | None = None, *, max_db: float = 12.0,
    ) -> None:
        super().__init__(parent)
        self.db = initial_db
        self.max_db = max_db
        self.accent_color = QColor(accent_color)
        self.setFixedWidth(44)
        self.setMinimumHeight(130)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

        self._dragging = False
        self._hovered = False
        self._last_y = 0.0
        self._cap_height = 24.0
        self._cap_width = 21.0

    def setDb(self, db: float, emit_signal: bool = True):
        db = max(-60.0, min(self.max_db, db))
        if db != self.db:
            self.db = db
            self.update()
            if emit_signal:
                self.dbChanged.emit(self.db)

    def _y_to_pos(self, y: float) -> float:
        margin = self._cap_height / 2.0 + 4.0
        track_h = self.height() - 2 * margin
        if track_h <= 0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - ((y - margin) / track_h)))

    def _pos_to_y(self, pos: float) -> float:
        margin = self._cap_height / 2.0 + 4.0
        track_h = self.height() - 2 * margin
        return margin + (1.0 - pos) * track_h

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.dragStarted.emit()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            # 点击滑槽空白处平滑跳到该位置；点在推子帽上保持相对位置防止跳变
            cap_y = self._pos_to_y(db_to_fader_pos(self.db, self.max_db))
            if abs(event.position().y() - cap_y) > self._cap_height / 2.0:
                self.setDb(fader_pos_to_db(self._y_to_pos(event.position().y()), self.max_db))
            self._last_y = event.position().y()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            y = event.position().y()
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Shift 精细微调模式
                dy = self._last_y - y
                cur_pos = db_to_fader_pos(self.db, self.max_db)
                track_h = max(1.0, self.height() - self._cap_height - 8.0)
                self.setDb(fader_pos_to_db(cur_pos + (dy / track_h) * 0.2, self.max_db))
            else:
                self.setDb(fader_pos_to_db(self._y_to_pos(y), self.max_db))
            self._last_y = y
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_drag()
            event.accept()

    def hideEvent(self, event: QHideEvent) -> None:
        self._finish_drag()
        super().hideEvent(event)

    def _finish_drag(self) -> None:
        if self._dragging:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.dragFinished.emit()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # 双击瞬间归位 0.0 dB (Unity Gain)
            self.setDb(0.0)
            event.accept()

    def wheelEvent(self, event):
        step_db = 0.2 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 1.0
        base = -60.0 if math.isinf(self.db) else self.db
        new_db = base + (step_db if event.angleDelta().y() > 0 else -step_db)
        self.setDb(-float('inf') if new_db <= -60.0 else new_db)
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        h = float(self.height())
        margin = self._cap_height / 2.0 + 4.0
        track_x = 12.5

        # 1. Menlo 精密 dB 刻度尺
        ticks = ([12] if self.max_db >= 12.0 else []) + [6, 0, -5, -10, -20, -30, -45, -60]
        p.setFont(QFont(MONO_FONT_FAM, 6))

        for t in ticks:
            ty = self._pos_to_y(db_to_fader_pos(float(t), self.max_db))
            is_unity = (t == 0)
            pen_col = self.accent_color if is_unity else QColor("#4A4A54")
            p.setPen(QPen(pen_col, 1.3 if is_unity else 1.0))
            p.drawLine(QPointF(track_x + 7.5, ty), QPointF(track_x + (13.0 if is_unity else 11.0), ty))

            p.setPen(self.accent_color if is_unity else QColor("#787882"))
            lbl = "0" if t == 0 else ("-∞" if t == -60 else f"{abs(t)}")
            p.drawText(QRectF(track_x + 14.5, ty - 6.0, 18.0, 12.0),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)

        # 2. 纯黑内凹滑槽
        slot_rect = QRectF(track_x - 2.0, margin, 4.0, h - 2 * margin)
        p.setBrush(QColor("#0E0E11"))
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.drawRoundedRect(slot_rect, 2.0, 2.0)

        # 3. 推子位置指示发光带
        cap_y = self._pos_to_y(db_to_fader_pos(self.db, self.max_db))
        active_rect = QRectF(track_x - 1.2, cap_y, 2.4, (h - margin) - cap_y)
        glow = QColor(self.accent_color)
        glow.setAlpha(155)
        p.fillRect(active_rect, glow)

        # 4. 达芬奇风格哑光阳极铝凹面推子帽
        cap_rect = QRectF(
            track_x - self._cap_width / 2.0,
            cap_y - self._cap_height / 2.0,
            self._cap_width,
            self._cap_height,
        )

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(cap_rect.translated(0, 2.0), 2.5, 2.5)

        cap_grad = QLinearGradient(cap_rect.topLeft(), cap_rect.bottomLeft())
        hi = "#96989E" if (self._hovered or self._dragging) else "#84868C"
        cap_grad.setColorAt(0.0, QColor(hi))
        cap_grad.setColorAt(0.22, QColor("#5A5C62"))
        cap_grad.setColorAt(0.50, QColor("#45474D"))
        cap_grad.setColorAt(0.78, QColor("#5A5C62"))
        cap_grad.setColorAt(1.0, QColor("#74767C"))

        p.setBrush(QBrush(cap_grad))
        p.setPen(QPen(QColor("#111114"), 1.0))
        p.drawRoundedRect(cap_rect, 2.5, 2.5)

        # 推子帽防滑横纹 + 中心高对比定位线
        p.setPen(QPen(QColor("#2A2B30"), 1.0))
        for offset in (-6.0, -3.5, 3.5, 6.0):
            p.drawLine(QPointF(cap_rect.left() + 3.0, cap_y + offset),
                       QPointF(cap_rect.right() - 3.0, cap_y + offset))

        p.setPen(QPen(QColor("#ECECF0"), 1.6))
        p.drawLine(QPointF(cap_rect.left() + 1.5, cap_y), QPointF(cap_rect.right() - 1.5, cap_y))


# ============================================================================
# 6. 自定义控件: MiniEQCurve (支持 HPF 高通联动 + 3 段交互频响曲线)
# ============================================================================
class MiniEQCurve(QWidget):
    eqChanged = pyqtSignal(float, float, float)

    def __init__(self, accent_color: str = ACCENT_CYAN, parent=None):
        super().__init__(parent)
        self.accent_color = QColor(accent_color)
        self.setFixedHeight(40)
        self.setToolTip("实时 EQ 预览 (拖拽左/中/右频段调整增益，双击复位)")
        self.setCursor(Qt.CursorShape.SizeVerCursor)

        self.gains = [0.0, 0.0, 0.0]
        self.hpf_enabled = False
        self._active_band = -1
        self._last_y = 0.0
        self.bypassed = False

    def setHpfEnabled(self, enabled: bool):
        self.hpf_enabled = enabled
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            x = event.position().x()
            w = self.width()
            self._active_band = 0 if x < w * 0.33 else (1 if x < w * 0.66 else 2)
            self._last_y = event.position().y()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._active_band != -1:
            dy = self._last_y - event.position().y()
            self._last_y = event.position().y()
            self.gains[self._active_band] = max(-12.0, min(12.0, self.gains[self._active_band] + dy * 0.35))
            self.update()
            self.eqChanged.emit(*self.gains)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._active_band = -1

    def mouseDoubleClickEvent(self, event):
        self.gains = [0.0, 0.0, 0.0]
        self.update()
        self.eqChanged.emit(*self.gains)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = float(self.width()), float(self.height())
        cy = h / 2.0

        # 达芬奇内凹纯黑频响屏
        p.setBrush(QColor("#151518"))
        p.setPen(QPen(QColor("#121214"), 1.0))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 3.0, 3.0)

        p.setPen(QPen(QColor("#242429"), 1.0, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(2.0, cy), QPointF(w - 2.0, cy))
        p.drawLine(QPointF(w * 0.33, 2.0), QPointF(w * 0.33, h - 2.0))
        p.drawLine(QPointF(w * 0.66, 2.0), QPointF(w * 0.66, h - 2.0))

        def gain_to_y(g: float) -> float:
            return cy - (g / 12.0) * (cy - 5.0)

        y_low = gain_to_y(self.gains[0])
        y_mid = gain_to_y(self.gains[1])
        y_high = gain_to_y(self.gains[2])

        path = QPainterPath()
        if self.hpf_enabled:
            # 80Hz 高通：低频端起手陡降
            path.moveTo(2.0, h - 3.0)
            path.cubicTo(w * 0.08, h - 4.0, w * 0.12, y_low, w * 0.20, y_low)
        else:
            path.moveTo(2.0, y_low)
            path.lineTo(w * 0.20, y_low)

        path.cubicTo(w * 0.32, y_low, w * 0.38, y_mid, w * 0.50, y_mid)
        path.cubicTo(w * 0.62, y_mid, w * 0.68, y_high, w - 2.0, y_high)

        fill_path = QPainterPath(path)
        fill_path.lineTo(w - 2.0, cy)
        fill_path.lineTo(2.0, cy)
        fill_path.closeSubpath()

        line_col = QColor("#666670") if self.bypassed else self.accent_color
        fill_col = QColor(line_col)
        fill_col.setAlpha(42)
        p.fillPath(fill_path, fill_col)

        p.setPen(QPen(line_col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPath(path)

        p.setBrush(line_col)
        p.setPen(Qt.PenStyle.NoPen)
        for px, py in [(w * 0.20, y_low), (w * 0.50, y_mid), (w * 0.80, y_high)]:
            p.drawEllipse(QPointF(px, py), 2.2, 2.2)


# ============================================================================
# 7. 通道条组件 (ChannelStrip): 支持 Audio / Bus / Master 三种轨道类型
# ============================================================================
