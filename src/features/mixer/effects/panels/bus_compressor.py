from __future__ import annotations

import sys
import re
import math
import copy
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


# =====================================================================
# 1. 全局色彩系统与默认参数配置（与 Dyamics-UI / multiband_comp_ui 统一）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":   "#28D8F0",  # 主控 / A-B 激活 / SSL 主题色
    "amber":  "#F0A848",  # Auto-Release / API 2500 主题色 / 暖光指针
    "green":  "#4CD490",  # 正常电平 / 胶水微压缩安全区
    "red":    "#F26D6D",  # 过载 / 深度压缩警戒区
    "sc":     "#6495FA",  # Sidechain HPF 侧链高通主题色
}

# 经典总线压缩器物理步进档位定义
RATIO_STEPS = [1.5, 2.0, 4.0, 10.0]
ATTACK_STEPS = [0.1, 0.3, 1.0, 3.0, 10.0, 30.0]         # ms
RELEASE_STEPS = [0.1, 0.3, 0.6, 1.2, -1.0]              # s (-1.0 代表 Auto)
SC_HPF_STEPS = [0, 60, 90, 120, 185]                    # Hz (0 代表 Off)


def default_bus_comp_ssl() -> dict:
    """默认 SSL 经典总线胶水预设：4:1 比率，慢启动保留瞬态，Auto 释放"""
    return {
        "enabled": True,
        "topology": "SSL G-Glue",   # "SSL G-Glue" | "API 2500"
        "threshold": -12.0,         # -20.0 ~ +20.0 dB
        "ratio": 4.0,               # 1.5 | 2.0 | 4.0 | 10.0
        "attack": 10.0,             # 0.1 | 0.3 | 1.0 | 3.0 | 10.0 | 30.0 ms
        "release": -1.0,            # 0.1 | 0.3 | 0.6 | 1.2 | -1.0 (Auto)
        "makeup": 2.0,              # -5.0 ~ +15.0 dB
        "mix": 100.0,               # 0 ~ 100 % (并联纽约压缩)
        "sc_hpf": 60,               # 0 (Off) | 60 | 90 | 120 | 185 Hz
        "sc_listen": False,
        "ext_sc": False,
    }


def default_bus_comp_flat() -> dict:
    return {
        "enabled": False,
        "topology": "SSL G-Glue",
        "threshold": 0.0,
        "ratio": 2.0,
        "attack": 3.0,
        "release": 0.3,
        "makeup": 0.0,
        "mix": 100.0,
        "sc_hpf": 0,
        "sc_listen": False,
        "ext_sc": False,
    }


def parse_float_safe(text: str, fallback: float) -> float:
    """容错解析带单位或正负号的数字输入"""
    m = re.search(r"[-+]?\d*\.?\d+", text.strip())
    if not m:
        return fallback
    try:
        return float(m.group(0))
    except ValueError:
        return fallback


def format_db_signed(val: float) -> str:
    if abs(val) < 0.05:
        return "0.0 dB"
    return f"{val:+.1f} dB"


# =====================================================================
# 2. 基础控件：电源开关 / 可编辑数字胶囊 / 连续旋钮 / 步进控制台旋钮
# =====================================================================
class PowerToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True, accent_color: str = "#28D8F0", parent=None):
        super().__init__(parent)
        self._checked = checked
        self._accent = QColor(accent_color)
        self.setFixedSize(34, 17)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def setAccentColor(self, hex_col: str):
        self._accent = QColor(hex_col)
        self.update()

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, val: bool, emit: bool = False):
        if self._checked != val:
            self._checked = val
            self.update()
            if emit:
                self.toggled.emit(self._checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked, emit=True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        if self._checked:
            bg = QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 34)
            border = self._accent
            knob_color = self._accent
        else:
            bg = QColor("#121215")
            border = QColor("#36363D")
            knob_color = QColor("#666670")

        p.setPen(QPen(border, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 8.5, 8.5)

        r = 5.2
        cy = self.height() / 2.0
        cx = (self.width() - 8.8) if self._checked else 8.8
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob_color)
        p.drawEllipse(QPointF(cx, cy), r, r)


class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 72, editable: bool = True, parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._active = True
        self._editable = editable
        self.setFixedSize(width, 21)
        if editable:
            self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(0, 0, width, 21)
        self._editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_editor_css()
        self._editor.hide()
        self._editor.returnPressed.connect(self._commit_edit)
        self._editor.editingFinished.connect(self._commit_edit)

    def _update_editor_css(self):
        self._editor.setStyleSheet(f"""
            QLineEdit {{
                background-color: #151518;
                color: {self._accent};
                border: 1px solid {self._accent};
                border-radius: 3px;
                font-family: {MONO_FONT_CSS};
                font-size: 11px;
                font-weight: 600;
                padding: 0px;
            }}
        """)

    def setAccentColor(self, hex_col: str):
        self._accent = hex_col
        self._update_editor_css()
        self.update()

    def setText(self, text: str):
        if self._text != text:
            self._text = text
            self.update()

    def setModuleActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def mousePressEvent(self, event):
        if self._editable and event.button() == Qt.MouseButton.LeftButton:
            clean = (
                self._text.replace("dB", "")
                .replace("%", "")
                .replace("ms", "")
                .replace("Hz", "")
                .replace("s", "")
                .strip()
            )
            self._editor.setText(clean)
            self._editor.show()
            self._editor.setFocus()
            self._editor.selectAll()

    def _commit_edit(self):
        if self._editor.isVisible():
            val = self._editor.text()
            self._editor.hide()
            self.valueEdited.emit(val)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(QColor("#121214"), 1.0))
        p.setBrush(QColor("#151518"))
        p.drawRoundedRect(rect, 3.5, 3.5)

        p.setPen(QColor("#ECECF0") if self._active else QColor("#6E6E76"))
        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


class ContinuousRotaryKnob(QWidget):
    """连续平滑调节旋钮（Threshold / Make-Up / Mix）"""
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value: float,
        default_value: float,
        accent_color: str = "#28D8F0",
        bipolar: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._bipolar = bipolar
        self._active = True
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(46, 46)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setAccentColor(self, hex_col: str):
        self._accent = QColor(hex_col)
        self.update()

    def setValue(self, val: float, emit: bool = False):
        clamped = max(self._min, min(self._max, val))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def setModuleActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def _val_to_norm(self, val: float) -> float:
        if self._max <= self._min:
            return 0.0
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        n = max(0.0, min(1.0, norm))
        return self._min + n * (self._max - self._min)

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
        elif event.button() == Qt.MouseButton.RightButton:
            self.setValue(self._default, emit=True)

    def mouseMoveEvent(self, event):
        if self._dragging:
            dy = self._last_y - event.position().y()
            self._last_y = event.position().y()
            sens = 0.0015 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.006
            norm = self._val_to_norm(self._value) + dy * sens
            self.setValue(self._norm_to_val(norm), emit=True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._default, emit=True)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        step = 0.01 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.025
        norm = self._val_to_norm(self._value) + delta * step
        self.setValue(self._norm_to_val(norm), emit=True)
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        norm = max(0.0, min(1.0, self._val_to_norm(self._value)))

        start_deg = 225.0
        total_span = -270.0
        curr_deg = start_deg + norm * total_span

        arc_r = 18.5
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 底环凹槽
        p.setPen(QPen(QColor("#141417"), 2.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 2. 激活光弧（双极性旋钮从 0 点起算）
        arc_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(QPen(arc_col, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._bipolar and self._min < 0.0 < self._max:
            zero_norm = (0.0 - self._min) / (self._max - self._min)
            zero_deg = start_deg + zero_norm * total_span
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        # 3. 阳极铝旋钮顶盖（径向金属锥面质感 + 悬停外发光）
        knob_r = 13.8
        if self._hovered or self._dragging:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 32))
            p.drawEllipse(QPointF(cx, cy), knob_r + 3.2, knob_r + 3.2)

        cap_grad = QLinearGradient(QPointF(cx, cy - knob_r), QPointF(cx, cy + knob_r))
        if self._hovered or self._dragging:
            cap_grad.setColorAt(0.0, QColor("#A2A5AD"))
            cap_grad.setColorAt(1.0, QColor("#6E7078"))
        else:
            cap_grad.setColorAt(0.0, QColor("#8C8E96"))
            cap_grad.setColorAt(1.0, QColor("#5E6068"))

        p.setPen(QPen(QColor("#101013"), 1.3))
        p.setBrush(cap_grad)
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 内凹顶帽倒角环
        p.setPen(QPen(QColor(255, 255, 255, 36), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), knob_r - 1.5, knob_r - 1.5)

        # 4. 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.8
        iy1 = cy - math.sin(rad) * 3.8
        ix2 = cx + math.cos(rad) * (knob_r - 1.6)
        iy2 = cy - math.sin(rad) * (knob_r - 1.6)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


class SteppedRotaryKnob(QWidget):
    """SSL 控制台机械段落式步进旋钮（用于 Ratio / Attack / Release / SC HPF）"""
    indexChanged = pyqtSignal(int)

    def __init__(
        self,
        labels: list[str],
        current_index: int = 0,
        default_index: int = 0,
        accent_color: str = "#28D8F0",
        parent=None,
    ):
        super().__init__(parent)
        self._labels = labels
        self._index = max(0, min(len(labels) - 1, current_index))
        self._default_index = default_index
        self._accent = QColor(accent_color)
        self._active = True
        self._dragging = False
        self._hovered = False
        self._accum_dy = 0.0
        self._last_y = 0.0

        self.setFixedSize(46, 46)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip("垂直拖拽 / 滚轮切换档位 · 双击 / 右键恢复默认")

    def setAccentColor(self, hex_col: str):
        self._accent = QColor(hex_col)
        self.update()

    def setIndex(self, idx: int, emit: bool = False):
        clamped = max(0, min(len(self._labels) - 1, idx))
        if clamped != self._index:
            self._index = clamped
            self.update()
            if emit:
                self.indexChanged.emit(self._index)

    def setModuleActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

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
            self._accum_dy = 0.0
        elif event.button() == Qt.MouseButton.RightButton:
            self.setIndex(self._default_index, emit=True)

    def mouseMoveEvent(self, event):
        if self._dragging:
            dy = self._last_y - event.position().y()
            self._last_y = event.position().y()
            self._accum_dy += dy
            step_px = 18.0
            if abs(self._accum_dy) >= step_px:
                steps = int(self._accum_dy / step_px)
                self._accum_dy -= steps * step_px
                self.setIndex(self._index + steps, emit=True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setIndex(self._default_index, emit=True)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta > 0:
            self.setIndex(self._index + 1, emit=True)
        elif delta < 0:
            self.setIndex(self._index - 1, emit=True)
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        n = len(self._labels)
        start_deg = 225.0
        total_span = -270.0

        # 1. 绘制每个物理档位外圈刻度点
        dot_r = 19.2
        for i in range(n):
            norm = i / max(1, n - 1)
            deg = start_deg + norm * total_span
            rad = math.radians(deg)
            dx = cx + math.cos(rad) * dot_r
            dy = cy - math.sin(rad) * dot_r

            is_curr = (i == self._index)
            p.setPen(Qt.PenStyle.NoPen)
            if is_curr and self._active:
                p.setBrush(self._accent)
                p.drawEllipse(QPointF(dx, dy), 2.5, 2.5)
            else:
                p.setBrush(QColor("#5C5C66") if is_curr else QColor("#383840"))
                p.drawEllipse(QPointF(dx, dy), 1.6, 1.6)

        # 2. 控制台经典翼型旋钮帽（金属锥面质感 + 悬停微光，与连续旋钮统一）
        curr_norm = self._index / max(1, n - 1)
        curr_deg = start_deg + curr_norm * total_span

        knob_r = 13.8
        if self._hovered or self._dragging:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 32))
            p.drawEllipse(QPointF(cx, cy), knob_r + 3.2, knob_r + 3.2)

        cap_grad = QLinearGradient(QPointF(cx, cy - knob_r), QPointF(cx, cy + knob_r))
        if self._hovered or self._dragging:
            cap_grad.setColorAt(0.0, QColor("#A2A5AD"))
            cap_grad.setColorAt(1.0, QColor("#6E7078"))
        else:
            cap_grad.setColorAt(0.0, QColor("#8C8E96"))
            cap_grad.setColorAt(1.0, QColor("#5E6068"))

        p.setPen(QPen(QColor("#101013"), 1.3))
        p.setBrush(cap_grad)
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 内凹顶帽倒角环
        p.setPen(QPen(QColor(255, 255, 255, 36), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), knob_r - 1.5, knob_r - 1.5)

        # 3. 亮色粗指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 2.5
        iy1 = cy - math.sin(rad) * 2.5
        ix2 = cx + math.cos(rad) * (knob_r - 1.4)
        iy2 = cy - math.sin(rad) * (knob_r - 1.4)
        ptr_col = self._accent if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 3. 控件：垂直高精电平表 & 精密硬件级模拟指针 Gain Reduction 表头
# =====================================================================
class ProVerticalMeter(QWidget):
    def __init__(self, min_db: float = -48.0, max_db: float = 0.0, parent=None):
        super().__init__(parent)
        self._min_db = min_db
        self._max_db = max_db
        self._ticks = [0, -3, -6, -10, -15, -20, -30, -40]
        self._value_db = min_db
        self._peak_db = min_db
        self._clipped = False
        self._active = True

        self.setFixedWidth(44)
        self.setMinimumHeight(180)

    def setValue(self, db: float):
        self._value_db = max(self._min_db, min(self._max_db, db))
        if self._value_db >= self._peak_db:
            self._peak_db = self._value_db
        else:
            self._peak_db = max(self._value_db, self._peak_db - 0.35)
        if db >= -0.1:
            self._clipped = True
        self.update()

    def setActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._clipped = False
            self._peak_db = self._value_db
            self.update()

    def _db_to_y(self, db: float, bar: QRectF) -> float:
        clamped = max(self._min_db, min(self._max_db, db))
        if clamped >= -20.0:
            norm = 0.45 + ((clamped + 20.0) / 20.0) * 0.55
        else:
            norm = ((clamped - self._min_db) / (-20.0 - self._min_db)) * 0.45
        return bar.bottom() - norm * bar.height()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 10.0
        bot_pad = 8.0
        bar_w = 10.0
        bar_x = self.width() - bar_w - 4.0
        bar = QRectF(bar_x, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))

        p.setFont(QFont(MONO_FONT_FAM, 7))
        for t in self._ticks:
            ty = self._db_to_y(float(t), bar)
            p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
            p.drawText(
                QRectF(0, ty - 7, bar.left() - 3, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{t} -",
            )

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        if self._active:
            y_val = self._db_to_y(self._value_db, bar)
            fill_h = bar.bottom() - y_val
            if fill_h > 1.0:
                fill_rect = QRectF(bar.left() + 1.0, y_val, bar.width() - 2.0, fill_h - 0.5)
                grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                grad.setColorAt(0.0,  QColor("#26C281"))
                grad.setColorAt(0.55, QColor("#4CD490"))
                grad.setColorAt(0.75, QColor("#F0A848"))
                grad.setColorAt(0.92, QColor("#F26D6D"))
                grad.setColorAt(1.0,  QColor("#FF4848"))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(grad)
                p.drawRoundedRect(fill_rect, 1.2, 1.2)

            if self._peak_db > self._min_db + 0.5:
                py = self._db_to_y(self._peak_db, bar)
                p.setPen(QPen(QColor("#F26D6D") if self._peak_db > -6.0 else QColor("#F0F2F6"), 1.4))
                p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

            clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
            p.drawRoundedRect(clip_rect, 1.0, 1.0)

        p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
        gy = bar.top() + 3.0
        while gy < bar.bottom():
            p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
            gy += 3.0


class AnalogGainReductionMeter(QWidget):
    """精密硬件级动圈指针式 Gain Reduction 表头（0 ~ 20 dB 衰减，含机械阻尼弹道）"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._target_gr_db = 0.0    # 目标增益衰减 (正值 dB, 0 ~ 20)
        self._display_gr_db = 0.0   # 经过机械惯性平滑后的显示角度值
        self._peak_gr_db = 0.0      # 峰值保持衰减 (正值 dB)
        self._active = True
        self._accent = QColor(COLORS["cyan"])
        self._topology_label = "SSL G-SERIES BUS"
        self.setMinimumSize(270, 210)

    def setActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def setTopology(self, topology: str, label: str | None = None) -> None:
        if topology == "API 2500":
            self._accent = QColor(COLORS["amber"])
            self._topology_label = label or "API 2500 THRUST"
        else:
            self._accent = QColor(COLORS["cyan"])
            self._topology_label = label or "SSL G-SERIES BUS"
        self.update()

    def setGainReduction(self, gr_db: float):
        clamped = max(0.0, min(20.0, abs(gr_db)))
        self._target_gr_db = clamped
        # 模拟动圈表头物理弹道：上升快（0.72），回落略带机械阻尼（0.38）
        alpha = 0.72 if clamped >= self._display_gr_db else 0.38
        self._display_gr_db += (clamped - self._display_gr_db) * alpha

        if clamped >= self._peak_gr_db:
            self._peak_gr_db = clamped
        else:
            self._peak_gr_db = max(clamped, self._peak_gr_db - 0.14)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._peak_gr_db = self._display_gr_db
            self.update()

    def _gr_to_angle_deg(self, gr: float) -> float:
        """
        非线性精密表盘角度映射（0 dB 在右侧 44°，20 dB 在左侧 136°）：
        0 ~ 4 dB 胶水甜点区占据 45% 弧度分辨率，微压缩动态清晰可辨
        """
        g = max(0.0, min(20.0, gr))
        if g <= 4.0:
            norm = (g / 4.0) * 0.45
        elif g <= 10.0:
            norm = 0.45 + ((g - 4.0) / 6.0) * 0.33
        else:
            norm = 0.78 + ((g - 10.0) / 10.0) * 0.22
        return 44.0 + norm * 92.0

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        margin = 10.0
        box = QRectF(margin, 8.0, self.width() - margin * 2, self.height() - 16.0)

        # 1. 哑光精密仪表壳体（纯正深炭灰线性微渐变，彻底去除浑浊光斑）
        bg_grad = QLinearGradient(box.topLeft(), box.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#151518"))
        bg_grad.setColorAt(0.5, QColor("#18181C"))
        bg_grad.setColorAt(1.0, QColor("#131316"))

        p.setPen(QPen(QColor("#111113"), 1.2))
        p.setBrush(bg_grad)
        p.drawRoundedRect(box, 5.0, 5.0)

        # 内凹精密金属高光描边
        inner_box = box.adjusted(1.0, 1.0, -1.0, -1.0)
        p.setPen(QPen(QColor(255, 255, 255, 14), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(inner_box, 4.2, 4.2)

        p.save()
        p.setClipRect(box.adjusted(2.0, 2.0, -2.0, -2.0))

        # 2. 顶部极克制的玻璃倒角反射高光带
        glass_grad = QLinearGradient(box.topLeft(), QPointF(box.left(), box.top() + 26.0))
        glass_grad.setColorAt(0.0, QColor(255, 255, 255, 11))
        glass_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(box.left(), box.top(), box.width(), 26.0), glass_grad)

        # 2.5 动态背光氛围（随 GR 压缩深度产生极柔和的仪表内部漫反射）
        if self._active and self._display_gr_db > 0.1:
            glow_alpha = min(38, int(10 + self._display_gr_db * 2.2))
            glow_center = QPointF(box.center().x(), box.center().y() + 18.0)
            rad_glow = QRadialGradient(glow_center, box.width() * 0.55)
            rad_glow.setColorAt(0.0, QColor(self._accent.red(), self._accent.green(), self._accent.blue(), glow_alpha))
            rad_glow.setColorAt(1.0, QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 0))
            p.fillRect(box, rad_glow)

        # 3. 顶部左右铭文与状态呼吸灯（与刻度区彻底分离，互不遮挡）
        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        p.setPen(QColor("#787882"))
        p.drawText(
            QRectF(box.left() + 12, box.top() + 8, 130, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "GAIN REDUCTION",
        )

        # 右上角状态指示灯 + 拓扑名
        led_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(led_col)
        p.drawEllipse(QPointF(box.right() - 14.0, box.top() + 15.0), 2.6, 2.6)

        p.setPen(led_col if self._active else QColor("#5A5A64"))
        p.drawText(
            QRectF(box.right() - 145, box.top() + 8, 124, 14),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self._topology_label,
        )

        # 3.5 四角精密沉头螺丝细节
        screw_offset = 7.0
        for sx, sy in (
            (box.left() + screw_offset, box.top() + screw_offset),
            (box.right() - screw_offset, box.top() + screw_offset),
            (box.left() + screw_offset, box.bottom() - screw_offset),
            (box.right() - screw_offset, box.bottom() - screw_offset),
        ):
            p.setPen(QPen(QColor("#0E0E10"), 0.8))
            p.setBrush(QColor("#222227"))
            p.drawEllipse(QPointF(sx, sy), 2.1, 2.1)
            p.setPen(QPen(QColor("#121215"), 0.8))
            p.drawLine(QPointF(sx - 1.2, sy - 1.2), QPointF(sx + 1.2, sy + 1.2))

        # 4. 计算宽幅动圈圆弧几何中心（位于仪表底部下方，使指针扫掠更舒展优雅）
        pivot = QPointF(box.center().x(), box.bottom() + box.height() * 0.62)
        arc_r = min(box.width() * 0.68, box.height() * 1.24)
        arc_rect = QRectF(pivot.x() - arc_r, pivot.y() - arc_r, arc_r * 2, arc_r * 2)

        deg_0 = self._gr_to_angle_deg(0.0)
        deg_4 = self._gr_to_angle_deg(4.0)
        deg_8 = self._gr_to_angle_deg(8.0)
        deg_20 = self._gr_to_angle_deg(20.0)

        # 5. 双轨同心精密刻度弧（外细轨 + 内色域带）
        outer_r = arc_r + 3.0
        outer_rect = QRectF(pivot.x() - outer_r, pivot.y() - outer_r, outer_r * 2, outer_r * 2)
        p.setPen(QPen(QColor("#2A2A32"), 1.0))
        p.drawArc(outer_rect, int(deg_0 * 16), int((deg_20 - deg_0) * 16))

        alpha_band = 200 if self._active else 45
        p.setPen(QPen(QColor(76, 212, 144, alpha_band), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc_rect, int(deg_0 * 16), int((deg_4 - deg_0) * 16))

        p.setPen(QPen(QColor(240, 168, 72, alpha_band), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc_rect, int(deg_4 * 16), int((deg_8 - deg_4) * 16))

        p.setPen(QPen(QColor(242, 109, 109, alpha_band), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc_rect, int(deg_8 * 16), int((deg_20 - deg_8) * 16))

        # 6. 精密激光蚀刻感刻度线与数字标注
        major_ticks = [0, 1, 2, 3, 4, 6, 8, 10, 14, 20]
        minor_ticks = [0.5, 1.5, 2.5, 3.5, 5, 7, 9, 12, 16, 18]

        for mt in minor_ticks:
            deg = self._gr_to_angle_deg(mt)
            rad = math.radians(deg)
            p1 = QPointF(pivot.x() + math.cos(rad) * (arc_r - 4.0), pivot.y() - math.sin(rad) * (arc_r - 4.0))
            p2 = QPointF(pivot.x() + math.cos(rad) * (arc_r + 2.5), pivot.y() - math.sin(rad) * (arc_r + 2.5))
            p.setPen(QPen(QColor("#3E3E48"), 0.9))
            p.drawLine(p1, p2)

        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        for t in major_ticks:
            deg = self._gr_to_angle_deg(float(t))
            rad = math.radians(deg)
            p1 = QPointF(pivot.x() + math.cos(rad) * (arc_r - 7.5), pivot.y() - math.sin(rad) * (arc_r - 7.5))
            p2 = QPointF(pivot.x() + math.cos(rad) * (arc_r + 3.0), pivot.y() - math.sin(rad) * (arc_r + 3.0))

            is_red = (t >= 8)
            tick_col = QColor("#F26D6D") if (is_red and self._active) else (QColor("#C4C4CE") if self._active else QColor("#56565E"))
            p.setPen(QPen(tick_col, 1.2))
            p.drawLine(p1, p2)

            lbl_r = arc_r + 13.0
            lx = pivot.x() + math.cos(rad) * lbl_r
            ly = pivot.y() - math.sin(rad) * lbl_r
            p.setPen(tick_col)
            p.drawText(QRectF(lx - 13, ly - 7, 26, 14), Qt.AlignmentFlag.AlignCenter, str(t))

        # 7. 峰值保持游标（贴合刻度带运行）
        if self._active and self._peak_gr_db > 0.15:
            p_deg = self._gr_to_angle_deg(self._peak_gr_db)
            p_rad = math.radians(p_deg)
            pk1 = QPointF(pivot.x() + math.cos(p_rad) * (arc_r - 9.5), pivot.y() - math.sin(p_rad) * (arc_r - 9.5))
            pk2 = QPointF(pivot.x() + math.cos(p_rad) * (arc_r + 3.5), pivot.y() - math.sin(p_rad) * (arc_r + 3.5))
            p.setPen(QPen(QColor(242, 109, 109, 210), 1.3))
            p.drawLine(pk1, pk2)

        # 8. 精密细锐锥形动圈指针（带金属渐变针尖 + 底层阻尼投影）
        curr_gr = self._display_gr_db if self._active else 0.0
        n_deg = self._gr_to_angle_deg(curr_gr)
        n_rad = math.radians(n_deg)
        tip_r = arc_r + 2.2
        tail_r = arc_r * 0.42

        tip = QPointF(pivot.x() + math.cos(n_rad) * tip_r, pivot.y() - math.sin(n_rad) * tip_r)
        tail = QPointF(pivot.x() + math.cos(n_rad) * tail_r, pivot.y() - math.sin(n_rad) * tail_r)

        p.setPen(QPen(QColor(0, 0, 0, 140), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(tail.x() + 1.8, tail.y() + 2.2), QPointF(tip.x() + 1.8, tip.y() + 2.2))

        if self._active:
            needle_grad = QLinearGradient(tail, tip)
            needle_grad.setColorAt(0.0, QColor("#7E828E"))
            needle_grad.setColorAt(0.55, QColor("#E4E6EE"))
            needle_grad.setColorAt(1.0, QColor("#FFFFFF"))
            p.setPen(QPen(QBrush(needle_grad), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        else:
            p.setPen(QPen(QColor("#52525A"), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(tail, tip)

        # 9. 底部精密数字读数槽
        readout_rect = QRectF(box.center().x() - 36.0, box.bottom() - 23.0, 72.0, 17.0)
        p.setPen(QPen(QColor("#24242A"), 1.0))
        p.setBrush(QColor("#101013"))
        p.drawRoundedRect(readout_rect, 3.0, 3.0)

        p.setPen(QColor("#ECECF0") if self._active else QColor("#6E6E76"))
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        gr_str = f"-{curr_gr:.1f} dB" if curr_gr >= 0.05 else "0.0 dB"
        p.drawText(readout_rect, Qt.AlignmentFlag.AlignCenter, gr_str)

        p.restore()


# =====================================================================
# 4. 控件：交互式总线压缩传输曲线图（I/O Transfer Curve + 阈值拖拽）
# =====================================================================
class BusTransferGraphWidget(QWidget):
    thresholdDragged = pyqtSignal(float)
    ratioDragged = pyqtSignal(float)  # 垂直拖拽拐点调节 Ratio

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(250, 210)
        self.setMouseTracking(True)

        self._state = default_bus_comp_ssl()
        self._dragging = False
        self._drag_mode = "none"  # "thresh" | "knee"
        self._hovered = False
        self._mouse_pos: QPointF | None = None
        self._trail_points: list[tuple[float, float, float]] = []  # (in_db, out_db, uncomp_out_db)
        self._drag_start_y = 0.0
        self._drag_start_ratio_idx = 0

        self._ml = 34.0
        self._mr = 12.0
        self._mt = 10.0
        self._mb = 22.0

    def setState(self, state: dict):
        self._state = state
        self.update()

    def pushSample(self, input_db: float, output_db: float | None = None) -> None:
        in_c = max(-48.0, min(12.0, input_db))
        out_c = (self.evaluate_transfer(in_c) if output_db is None
                 else max(-48.0, min(12.0, output_db)))
        # 计算未压缩但含 Make-Up 的参考输出电平，用于绘制实时 GR 落差条
        uncomp_c = max(-48.0, min(12.0, in_c + (self._state["makeup"] if self._state["enabled"] else 0.0)))
        self._trail_points.append((in_c, out_c, uncomp_c))
        if len(self._trail_points) > 24:
            self._trail_points.pop(0)
        self.update()

    def _plot_rect(self) -> QRectF:
        avail_w = max(90.0, self.width() - self._ml - self._mr)
        avail_h = max(90.0, self.height() - self._mt - self._mb)
        side = min(avail_w, avail_h * 1.15)
        left = self._ml + (avail_w - side) / 2.0
        return QRectF(left, self._mt, side, avail_h)

    def _db_to_x(self, db: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (db + 48.0) / 60.0))
        return plot.left() + norm * plot.width()

    def _x_to_db(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return -48.0 + norm * 60.0

    def _db_to_y(self, db: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (12.0 - db) / 60.0))
        return plot.top() + norm * plot.height()

    def evaluate_transfer(self, in_db: float) -> float:
        st = self._state
        if not st["enabled"]:
            return in_db

        thresh = st["threshold"]
        ratio = max(1.01, st["ratio"])
        mix = st["mix"] / 100.0
        knee_db = st.get("processor_knee", 8.0 if st["topology"] == "SSL G-Glue" else 4.5)

        d = in_db - thresh
        if 2.0 * d < -knee_db:
            y_comp = in_db
        elif knee_db > 0.0 and 2.0 * abs(d) <= knee_db:
            y_comp = in_db + (1.0 / ratio - 1.0) * ((d + knee_db / 2.0) ** 2) / (2.0 * knee_db)
        else:
            y_comp = thresh + d / ratio

        if "processor_knee" in st:
            gain = (1.0 - mix) + mix * 10.0 ** ((y_comp - in_db) / 20.0)
            y = in_db + 20.0 * math.log10(gain) + st["makeup"]
        else:
            y = (1.0 - mix) * in_db + mix * (y_comp + st["makeup"])
        return max(-48.0, min(12.0, y))

    def _hit_test(self, pos: QPointF, plot: QRectF) -> str:
        """区分命中阈值拐点圆环（可调 Threshold + Ratio）还是阈值竖线（仅调 Threshold）"""
        if not self._state["enabled"]:
            return "none"
        t_val = self._state["threshold"]
        tx = self._db_to_x(t_val, plot)
        ty = self._db_to_y(self.evaluate_transfer(t_val), plot)
        if math.hypot(pos.x() - tx, pos.y() - ty) <= 10.0:
            return "knee"
        if abs(pos.x() - tx) <= 7.0 and plot.top() <= pos.y() <= plot.bottom():
            return "thresh"
        return "none"

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            plot = self._plot_rect()
            mode = self._hit_test(event.position(), plot)
            if mode != "none":
                self._dragging = True
                self._drag_mode = mode
                self._drag_start_y = event.position().y()
                try:
                    self._drag_start_ratio_idx = RATIO_STEPS.index(self._state["ratio"])
                except ValueError:
                    self._drag_start_ratio_idx = 1

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()
        if self._dragging:
            raw_db = self._x_to_db(event.position().x(), plot)
            clamped_t = max(-20.0, min(20.0, round(raw_db, 1)))
            self.thresholdDragged.emit(clamped_t)

            if self._drag_mode == "knee":
                # 向下拖拽增大压缩比，向上拖拽减小压缩比
                dy = event.position().y() - self._drag_start_y
                step_delta = int(round(dy / 22.0))
                new_idx = max(0, min(len(RATIO_STEPS) - 1, self._drag_start_ratio_idx + step_delta))
                new_ratio = RATIO_STEPS[new_idx]
                if abs(new_ratio - self._state["ratio"]) > 1e-4:
                    self.ratioDragged.emit(new_ratio)
        else:
            mode = self._hit_test(event.position(), plot)
            self._hovered = (mode != "none")
            if mode == "knee":
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            elif mode == "thresh":
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._drag_mode = "none"
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        p.fillRect(plot, QColor("#1D1D20"))

        grid_pen = QPen(QColor("#18181C"), 1.0)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        for db in (12, 0, -12, -24, -36, -48):
            y = self._db_to_y(float(db), plot)
            x = self._db_to_x(float(db), plot)
            p.setPen(QPen(QColor("#2E2E36"), 1.1) if db == 0 else grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

            p.setPen(QColor("#9A9AA2"))
            lbl = f"+{db}" if db > 0 else str(db)
            p.drawText(QRectF(0, y - 8, plot.left() - 5, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)
            p.drawText(QRectF(x - 16, plot.bottom() + 4, 32, 16), Qt.AlignmentFlag.AlignCenter, lbl)

        p.setPen(QColor("#787882"))
        p.drawText(QRectF(plot.left() - 24, plot.bottom() + 4, 20, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "dB")

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        # 1:1 基准对角线
        p.setPen(QPen(QColor("#3A3A42"), 1.1, Qt.PenStyle.DashLine))
        p.drawLine(plot.bottomLeft(), plot.topRight())

        st = self._state
        enabled = st["enabled"]
        accent = QColor(COLORS["amber"] if st["topology"] == "API 2500" else COLORS["cyan"])

        # 阈值参考线
        if enabled:
            tx = self._db_to_x(st["threshold"], plot)
            hi = (self._hovered or self._dragging)
            line_col = QColor(accent)
            line_col.setAlpha(235 if hi else 145)
            p.setPen(QPen(line_col, 1.8 if hi else 1.2))
            p.drawLine(QPointF(tx, plot.top()), QPointF(tx, plot.bottom()))

        # 压缩传输主曲线与渐变填充
        curve_path = QPainterPath()
        fill_path = QPainterPath()
        steps = 180
        for i in range(steps + 1):
            in_db = -48.0 + (i / steps) * 60.0
            out_db = self.evaluate_transfer(in_db)
            px = self._db_to_x(in_db, plot)
            py = self._db_to_y(out_db, plot)
            if i == 0:
                curve_path.moveTo(px, py)
                fill_path.moveTo(px, plot.bottom())
                fill_path.lineTo(px, py)
            else:
                curve_path.lineTo(px, py)
                fill_path.lineTo(px, py)
        fill_path.lineTo(plot.right(), plot.bottom())
        fill_path.closeSubpath()

        if enabled:
            area_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            area_grad.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 34))
            area_grad.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(area_grad)
            p.drawPath(fill_path)

            p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 55), 4.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(curve_path)
            p.setPen(QPen(accent, 2.0))
            p.drawPath(curve_path)

            # 阈值拐点手柄
            t_val = st["threshold"]
            pt = QPointF(self._db_to_x(t_val, plot), self._db_to_y(self.evaluate_transfer(t_val), plot))
            hi = (self._hovered or self._dragging)
            r = 5.5 if hi else 4.2
            if hi:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 75))
                p.drawEllipse(pt, r + 4.0, r + 4.0)
            p.setPen(QPen(accent, 1.7))
            p.setBrush(QColor("#151518"))
            p.drawEllipse(pt, r, r)
        else:
            p.setPen(QPen(QColor("#8A8A92"), 1.6))
            p.drawPath(curve_path)

        # 实时信号动态轨迹点 + GR 瞬态衰减落差线
        if enabled and self._trail_points:
            n = len(self._trail_points)
            for idx, (s_in, s_out, s_uncomp) in enumerate(self._trail_points):
                age = (idx + 1) / n
                sx = self._db_to_x(s_in, plot)
                sy = self._db_to_y(s_out, plot)
                sy_uncomp = self._db_to_y(s_uncomp, plot)

                # 最新一帧绘制从“未压缩目标点”到“实际压缩输出点”的垂直红色衰减标尺
                if idx == n - 1 and (sy - sy_uncomp) > 2.0:
                    p.setPen(QPen(QColor(242, 109, 109, 210), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    p.drawLine(QPointF(sx, sy_uncomp), QPointF(sx, sy))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(242, 109, 109, 220))
                    p.drawEllipse(QPointF(sx, sy_uncomp), 2.2, 2.2)

                p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), int(180 * age)), 1.2))
                p.drawLine(QPointF(sx, plot.bottom() - 4.5), QPointF(sx, plot.bottom()))
                p.drawLine(QPointF(plot.left(), sy), QPointF(plot.left() + 4.5, sy))

                dot_r = 1.6 + 2.3 * age
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(220, 248, 255, int(225 * age)))
                p.drawEllipse(QPointF(sx, sy), dot_r, dot_r)

        # 侧链监听激活警告角标（防止忘关）
        if enabled and st["sc_listen"]:
            warn_rect = QRectF(plot.right() - 86, plot.top() + 8, 78, 17)
            p.setPen(QPen(QColor("#E8B838"), 1.0))
            p.setBrush(QColor(45, 36, 16, 220))
            p.drawRoundedRect(warn_rect, 3.0, 3.0)
            p.setPen(QColor("#E8B838"))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(warn_rect, Qt.AlignmentFlag.AlignCenter, "SC LISTEN")

        # 悬停 / 拖拽 HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            if self._dragging:
                hud_str = f"Bus Thresh: {st['threshold']:+.1f} dB"
                hud_col = accent
            else:
                in_v = self._x_to_db(self._mouse_pos.x(), plot)
                out_v = self.evaluate_transfer(in_v)
                hud_str = f"In {in_v:+.1f} dB  →  Out {out_v:+.1f} dB"
                hud_col = QColor("#B4B4BE")

            hud_rect = QRectF(plot.left() + 8, plot.top() + 8, 178, 19)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor(21, 21, 24, 215))
            p.drawRoundedRect(hud_rect, 3.5, 3.5)
            p.setPen(hud_col)
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 5. 参数控制单元封装：连续旋钮单元 & 步进旋钮单元
# =====================================================================
class ContinuousKnobCell(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        label: str,
        min_val: float,
        max_val: float,
        default_val: float,
        accent_color: str,
        min_hint: str,
        mid_hint: str,
        max_hint: str,
        formatter,
        parser,
        bipolar: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._formatter = formatter
        self._parser = parser
        self._min = min_val
        self._max = max_val
        self._val = default_val
        self._updating = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        self.title_lbl = QLabel(label)
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet("color: #9AA0B0; font-size: 10.5px; font-weight: 700; letter-spacing: 0.3px;")
        lay.addWidget(self.title_lbl)

        self.knob = ContinuousRotaryKnob(min_val, max_val, default_val, default_val, accent_color, bipolar=bipolar)
        self.knob.valueChanged.connect(self._on_knob_changed)
        lay.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(4, 0, 4, 0)
        hints_row.setSpacing(2)
        for h_text, align in (
            (min_hint, Qt.AlignmentFlag.AlignLeft),
            (mid_hint, Qt.AlignmentFlag.AlignCenter),
            (max_hint, Qt.AlignmentFlag.AlignRight),
        ):
            hl = QLabel(h_text)
            hl.setAlignment(align)
            hl.setStyleSheet("color: #787882; font-size: 9px;")
            hints_row.addWidget(hl, 1)
        lay.addLayout(hints_row)

        self.pill = EditableValuePill(formatter(default_val), accent_color, width=72, editable=True)
        self.pill.valueEdited.connect(self._on_pill_edited)
        lay.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignCenter)

    def setAccentColor(self, hex_col: str):
        self.knob.setAccentColor(hex_col)
        self.pill.setAccentColor(hex_col)

    def setValue(self, val: float):
        self._updating = True
        self._val = max(self._min, min(self._max, val))
        self.knob.setValue(self._val)
        self.pill.setText(self._formatter(self._val))
        self._updating = False

    def setModuleActive(self, active: bool):
        self.title_lbl.setStyleSheet(f"color: {'#B0B4C0' if active else '#6E6E76'}; font-size: 10.5px; font-weight: 700;")
        self.knob.setModuleActive(active)
        self.pill.setModuleActive(active)

    def _on_knob_changed(self, val: float):
        if self._updating:
            return
        self._val = val
        self.pill.setText(self._formatter(val))
        self.valueChanged.emit(val)

    def _on_pill_edited(self, text: str):
        parsed = self._parser(text, self._val)
        clamped = max(self._min, min(self._max, parsed))
        self.setValue(clamped)
        self.valueChanged.emit(clamped)


class SteppedKnobCell(QWidget):
    valueChanged = pyqtSignal(object)

    def __init__(
        self,
        label: str,
        values: list,
        labels: list[str],
        default_index: int,
        accent_color: str,
        min_hint: str,
        mid_hint: str,
        max_hint: str,
        parent=None,
    ):
        super().__init__(parent)
        self._values = values
        self._labels = labels
        self._index = default_index
        self._updating = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        self.title_lbl = QLabel(label)
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet("color: #9AA0B0; font-size: 10.5px; font-weight: 700; letter-spacing: 0.3px;")
        lay.addWidget(self.title_lbl)

        self.knob = SteppedRotaryKnob(labels, default_index, default_index, accent_color)
        self.knob.indexChanged.connect(self._on_index_changed)
        lay.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(4, 0, 4, 0)
        hints_row.setSpacing(2)
        for h_text, align in (
            (min_hint, Qt.AlignmentFlag.AlignLeft),
            (mid_hint, Qt.AlignmentFlag.AlignCenter),
            (max_hint, Qt.AlignmentFlag.AlignRight),
        ):
            hl = QLabel(h_text)
            hl.setAlignment(align)
            hl.setStyleSheet("color: #787882; font-size: 9px;")
            hints_row.addWidget(hl, 1)
        lay.addLayout(hints_row)

        self.pill = EditableValuePill(labels[default_index], accent_color, width=72, editable=False)
        lay.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignCenter)

    def setAccentColor(self, hex_col: str):
        self.knob.setAccentColor(hex_col)
        self.pill.setAccentColor(hex_col)

    def setValue(self, val):
        self._updating = True
        best_idx = 0
        best_diff = 1e9
        for i, v in enumerate(self._values):
            if abs(v - val) < best_diff:
                best_diff = abs(v - val)
                best_idx = i
        self._index = best_idx
        self.knob.setIndex(best_idx)
        self.pill.setText(self._labels[best_idx])
        self._updating = False

    def setModuleActive(self, active: bool):
        self.title_lbl.setStyleSheet(f"color: {'#B0B4C0' if active else '#6E6E76'}; font-size: 10.5px; font-weight: 700;")
        self.knob.setModuleActive(active)
        self.pill.setModuleActive(active)

    def _on_index_changed(self, idx: int):
        if self._updating:
            return
        self._index = idx
        self.pill.setText(self._labels[idx])
        self.valueChanged.emit(self._values[idx])


# =====================================================================
# 6. 主面板：总线胶水压缩器 (Studio Bus Compressor)
# =====================================================================
class StudioBusCompWidget(QWidget):
    """
    总线胶水压缩器主界面，提供完整信号接口供后端 DSP 引擎订阅：
      - bypassChanged(bool)
      - topologyChanged(str)          # "SSL G-Glue" | "API 2500"
      - paramsChanged(dict)           # 当前全部总线压缩参数字典
      - stateChanged(dict)            # 完整状态快照
    """
    bypassChanged = pyqtSignal(bool)
    topologyChanged = pyqtSignal(str)
    paramsChanged = pyqtSignal(dict)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Master Bus", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Bus Glue Compressor — {track_name}")
        self.resize(860, 560)
        self.setMinimumSize(800, 520)

        self._active_bank = "A"
        self._banks = {
            "A": default_bus_comp_ssl(),
            "B": default_bus_comp_flat(),
        }

        self._presets = {
            # --- 1. 经典默认与直通 ---
            "SSL Drum Bus Glue (4:1 · Auto)": copy.deepcopy(self._banks["A"]),
            "Flat Bypass": copy.deepcopy(self._banks["B"]),

            # --- 2. 鼓组与打击乐总线 ---
            "Drum Bus · Snappy Punch (4:1 · 10ms)": {
                "enabled": True, "topology": "SSL G-Glue",
                "threshold": -11.0, "ratio": 4.0, "attack": 10.0, "release": 0.1,
                "makeup": 2.5, "mix": 100.0, "sc_hpf": 90, "sc_listen": False, "ext_sc": False,
            },
            "Drum Bus · Heavy Room Smash (10:1 · NY)": {
                "enabled": True, "topology": "API 2500",
                "threshold": -20.0, "ratio": 10.0, "attack": 0.3, "release": 0.1,
                "makeup": 6.0, "mix": 35.0, "sc_hpf": 120, "sc_listen": False, "ext_sc": False,
            },
            "Drum Bus · Vintage Warmth (2:1 · 30ms)": {
                "enabled": True, "topology": "API 2500",
                "threshold": -9.5, "ratio": 2.0, "attack": 30.0, "release": 0.3,
                "makeup": 1.8, "mix": 100.0, "sc_hpf": 60, "sc_listen": False, "ext_sc": False,
            },
            "Percussion & Loops Tightener (4:1 · 3ms)": {
                "enabled": True, "topology": "SSL G-Glue",
                "threshold": -13.5, "ratio": 4.0, "attack": 3.0, "release": 0.1,
                "makeup": 2.0, "mix": 80.0, "sc_hpf": 120, "sc_listen": False, "ext_sc": False,
            },

            # --- 3. 人声与和声总线 ---
            "Lead Vocal Bus · Silky Glue (4:1 · Auto)": {
                "enabled": True, "topology": "SSL G-Glue",
                "threshold": -14.5, "ratio": 4.0, "attack": 3.0, "release": -1.0,
                "makeup": 2.8, "mix": 90.0, "sc_hpf": 120, "sc_listen": False, "ext_sc": False,
            },
            "Backing Vocals & Choir Lock (4:1 · Fast)": {
                "enabled": True, "topology": "SSL G-Glue",
                "threshold": -16.0, "ratio": 4.0, "attack": 1.0, "release": 0.3,
                "makeup": 3.2, "mix": 100.0, "sc_hpf": 185, "sc_listen": False, "ext_sc": False,
            },
            "Pop Vocal · Upfront Parallel (10:1 · NY)": {
                "enabled": True, "topology": "API 2500",
                "threshold": -19.0, "ratio": 10.0, "attack": 1.0, "release": 0.1,
                "makeup": 5.0, "mix": 30.0, "sc_hpf": 120, "sc_listen": False, "ext_sc": False,
            },

            # --- 4. 乐器群组总线 ---
            "Guitars & Synths Wall of Sound (4:1)": {
                "enabled": True, "topology": "API 2500",
                "threshold": -13.0, "ratio": 4.0, "attack": 10.0, "release": 0.3,
                "makeup": 2.2, "mix": 85.0, "sc_hpf": 90, "sc_listen": False, "ext_sc": False,
            },
            "Acoustic & Strings Natural Cohesion (1.5:1)": {
                "enabled": True, "topology": "SSL G-Glue",
                "threshold": -10.0, "ratio": 1.5, "attack": 30.0, "release": 0.6,
                "makeup": 1.2, "mix": 100.0, "sc_hpf": 60, "sc_listen": False, "ext_sc": False,
            },
            "Keys & Pads Smooth Leveler (2:1 · 10ms)": {
                "enabled": True, "topology": "SSL G-Glue",
                "threshold": -12.5, "ratio": 2.0, "attack": 10.0, "release": 0.6,
                "makeup": 1.5, "mix": 100.0, "sc_hpf": 90, "sc_listen": False, "ext_sc": False,
            },

            # --- 5. 混音与母带总输出总线 ---
            "Master Bus · Classic 2dB Glue (2:1 · Auto)": {
                "enabled": True, "topology": "SSL G-Glue",
                "threshold": -7.5, "ratio": 2.0, "attack": 30.0, "release": -1.0,
                "makeup": 1.2, "mix": 100.0, "sc_hpf": 90, "sc_listen": False, "ext_sc": False,
            },
            "Master Bus · Transparent Mastering (1.5:1)": {
                "enabled": True, "topology": "SSL G-Glue",
                "threshold": -6.0, "ratio": 1.5, "attack": 30.0, "release": -1.0,
                "makeup": 0.8, "mix": 100.0, "sc_hpf": 60, "sc_listen": False, "ext_sc": False,
            },
            "Master Bus · EDM / Hip-Hop Pump (4:1 · 90Hz)": {
                "enabled": True, "topology": "API 2500",
                "threshold": -10.5, "ratio": 4.0, "attack": 10.0, "release": 0.1,
                "makeup": 2.0, "mix": 100.0, "sc_hpf": 120, "sc_listen": False, "ext_sc": False,
            },
            "Master Bus · Rock & Metal Aggression (4:1)": {
                "enabled": True, "topology": "API 2500",
                "threshold": -11.5, "ratio": 4.0, "attack": 3.0, "release": 0.3,
                "makeup": 2.4, "mix": 90.0, "sc_hpf": 90, "sc_listen": False, "ext_sc": False,
            },
            "Film Score & Orchestral Wide Bus (1.5:1 · 1.2s)": {
                "enabled": True, "topology": "SSL G-Glue",
                "threshold": -9.0, "ratio": 1.5, "attack": 30.0, "release": 1.2,
                "makeup": 1.0, "mix": 75.0, "sc_hpf": 60, "sc_listen": False, "ext_sc": False,
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 演示动态模拟定时器（接入真实音频引擎 update_meters 后自动停止）
        self._anim_t = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_meters)
        self._demo_timer.start(40)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ContinuousRotaryKnob, SteppedRotaryKnob, EditableValuePill,
            PowerToggleSwitch, ProVerticalMeter, AnalogGainReductionMeter, BusTransferGraphWidget {{
                background-color: transparent;
            }}
            QComboBox {{
                background-color: #18181B;
                color: #DCDCE2;
                border: 1px solid #121214;
                border-radius: 3px;
                padding: 0px 6px;
                font-size: 10px;
                font-weight: 600;
                min-height: 20px;
            }}
            QComboBox:hover {{ border-color: #484852; }}
            QComboBox::drop-down {{ border: none; width: 14px; }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 3.5px solid transparent;
                border-right: 3.5px solid transparent;
                border-top: 4.5px solid #9A9AA4;
                margin-right: 5px;
            }}
            QComboBox QAbstractItemView {{
                background-color: #222226;
                color: #DCDCE2;
                border: 1px solid #141416;
                selection-background-color: #35353D;
                selection-color: #FFFFFF;
                outline: none;
            }}
            QPushButton#ToolBtn {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton#ToolBtn:hover {{
                background-color: #3C3C44;
                color: #FFFFFF;
            }}
            QPushButton#ToolBtn:pressed {{
                background-color: #1D1D21;
                padding-top: 1px;
            }}
            QToolTip {{
                background-color: #1D1D21;
                color: #DCDCE2;
                border: 1px solid #141416;
                padding: 6px 9px;
                font-size: 11px;
            }}
        """)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # -------------------------------------------------------------
        # 1. 顶部整合工具栏
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setFixedHeight(38)
        top_bar.setObjectName("TopBar")
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True, COLORS["cyan"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"BUS GLUE COMP  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(8)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_preset_btn.setToolTip("保存当前总线压缩配置为预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(225)
        self._populate_grouped_presets()
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        tb_lay.addWidget(self.preset_combo)

        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setFixedSize(20, 22)
        prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb_lay.addWidget(prev_btn)

        next_btn = QPushButton("›")
        next_btn.setObjectName("ToolBtn")
        next_btn.setFixedSize(20, 22)
        next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb_lay.addWidget(next_btn)

        tb_lay.addSpacing(10)

        # SSL G-Glue / API 2500 经典拓扑切换分段器
        topo_wrap = QFrame()
        topo_wrap.setStyleSheet("background-color: #18181B; border: 1px solid #121214; border-radius: 3px;")
        topo_lay = QHBoxLayout(topo_wrap)
        topo_lay.setContentsMargins(2, 2, 2, 2)
        topo_lay.setSpacing(2)

        self.btn_ssl = QPushButton("SSL G-Glue")
        self.btn_api = QPushButton("API 2500")
        for b in (self.btn_ssl, self.btn_api):
            b.setFixedHeight(18)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            topo_lay.addWidget(b)
        self.btn_ssl.clicked.connect(lambda: self._on_topology_changed("SSL G-Glue"))
        self.btn_api.clicked.connect(lambda: self._on_topology_changed("API 2500"))
        tb_lay.addWidget(topo_wrap)

        tb_lay.addStretch()

        # A / B 对比槽
        ab_lay = QHBoxLayout()
        ab_lay.setSpacing(2)
        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(24, 22)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton("→")
        self.btn_copy_ab.setFixedSize(24, 22)
        self.btn_copy_ab.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_ab.setToolTip("复制当前参数到另一槽位")
        self.btn_copy_ab.clicked.connect(self._copy_bank)

        self.btn_b = QPushButton("B")
        self.btn_b.setFixedSize(24, 22)
        self.btn_b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_b.clicked.connect(lambda: self._switch_bank("B"))

        ab_lay.addWidget(self.btn_a)
        ab_lay.addWidget(self.btn_copy_ab)
        ab_lay.addWidget(self.btn_b)
        tb_lay.addLayout(ab_lay)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.clicked.connect(self.reset_bus_comp)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：Input | I/O 压缩曲线 | Analog GR VU 表 | Output
        # -------------------------------------------------------------
        upper_panel = QFrame()
        up_lay = QHBoxLayout(upper_panel)
        up_lay.setContentsMargins(0, 0, 0, 0)
        up_lay.setSpacing(0)

        def _vsep() -> QFrame:
            f = QFrame()
            f.setFixedWidth(1)
            f.setStyleSheet("background-color: #141416; border: none;")
            return f

        # 2.1 Input 电平表
        in_col = self._build_meter_column("Input")
        self.meter_input = ProVerticalMeter()
        in_col.addWidget(self.meter_input, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(in_col)
        up_lay.addWidget(_vsep())

        # 2.2 总线压缩传输特性曲线
        self.bus_graph = BusTransferGraphWidget()
        self.bus_graph.thresholdDragged.connect(self._on_graph_thresh_dragged)
        self.bus_graph.ratioDragged.connect(self._on_graph_ratio_dragged)
        up_lay.addWidget(self.bus_graph, 5)
        up_lay.addWidget(_vsep())

        # 2.3 精密指针式 Gain Reduction VU 表
        self.vu_meter = AnalogGainReductionMeter()
        up_lay.addWidget(self.vu_meter, 5)
        up_lay.addWidget(_vsep())

        # 2.4 Output 电平表
        out_col = self._build_meter_column("Output")
        self.meter_output = ProVerticalMeter()
        out_col.addWidget(self.meter_output, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部控制台主控旋钮矩阵
        # -------------------------------------------------------------
        bottom_card = QFrame()
        bottom_card.setObjectName("ConsoleDeck")
        bottom_card.setStyleSheet("""
            QFrame#ConsoleDeck {
                background-color: #27272B;
                border-top: 1px solid #141416;
            }
        """)
        deck_lay = QVBoxLayout(bottom_card)
        deck_lay.setContentsMargins(12, 8, 12, 10)
        deck_lay.setSpacing(8)

        # 顶部功能子栏
        sub_bar = QHBoxLayout()
        sub_bar.setSpacing(6)

        deck_tag = QLabel("BUS DYNAMICS CONSOLE")
        deck_tag.setStyleSheet("color: #787882; font-size: 10px; font-weight: 700; letter-spacing: 0.6px;")
        sub_bar.addWidget(deck_tag)

        # In / Out / Δ Gain 等响度校准实时读数胶囊（定宽防抖动，数字等宽格式化）
        sub_bar.addSpacing(12)
        self.lbl_io_readout = QLabel("IN  ---.- dB  │  OUT  ---.- dB  │  Δ  +0.0 dB")
        self.lbl_io_readout.setFixedSize(276, 21)
        self.lbl_io_readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_io_readout.setStyleSheet(f"""
            background-color: #16161A;
            color: #9AA0B0;
            border: 1px solid #121214;
            border-radius: 3px;
            padding: 1px 8px;
            font-family: {MONO_FONT_CSS};
            font-size: 9.5px;
            font-weight: 600;
        """)
        sub_bar.addWidget(self.lbl_io_readout)

        sub_bar.addStretch()

        self.btn_auto_gain = QPushButton("AUTO MAKE-UP")
        self.btn_auto_gain.setObjectName("ToolBtn")
        self.btn_auto_gain.setFixedSize(98, 21)
        self.btn_auto_gain.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_gain.setToolTip("根据当前 Threshold 与 Ratio 自动估算响度补偿增益")
        self.btn_auto_gain.clicked.connect(self._apply_auto_makeup)
        sub_bar.addWidget(self.btn_auto_gain)

        self.btn_auto_rel = QPushButton("AUTO RELEASE")
        self.btn_auto_rel.setCheckable(True)
        self.btn_auto_rel.setFixedSize(102, 21)
        self.btn_auto_rel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_rel.setToolTip("程序自适应双阶段释放（瞬态快释放 + 持续电平慢释放）")
        self.btn_auto_rel.clicked.connect(self._on_auto_release_toggled)
        sub_bar.addWidget(self.btn_auto_rel)

        self.btn_ext_sc = QPushButton("EXT SC")
        self.btn_ext_sc.setCheckable(True)
        self.btn_ext_sc.setFixedSize(62, 21)
        self.btn_ext_sc.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ext_sc.setToolTip("切换外部侧链触发输入")
        self.btn_ext_sc.clicked.connect(self._on_ext_sc_toggled)
        sub_bar.addWidget(self.btn_ext_sc)

        self.btn_sc_listen = QPushButton("SC LISTEN")
        self.btn_sc_listen.setCheckable(True)
        self.btn_sc_listen.setFixedSize(76, 21)
        self.btn_sc_listen.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sc_listen.setToolTip("监听侧链高通滤波后的触发信号")
        self.btn_sc_listen.clicked.connect(self._on_sc_listen_toggled)
        sub_bar.addWidget(self.btn_sc_listen)

        deck_lay.addLayout(sub_bar)

        # 7 个核心旋钮水平阵列
        knobs_row = QHBoxLayout()
        knobs_row.setSpacing(6)

        self.c_thresh = ContinuousKnobCell(
            "Threshold", -20.0, 20.0, -12.0, COLORS["cyan"],
            "-20", "0", "+20",
            format_db_signed, parse_float_safe, bipolar=True
        )
        self.c_ratio = SteppedKnobCell(
            "Ratio", RATIO_STEPS, ["1.5:1", "2:1", "4:1", "10:1"], 2, COLORS["cyan"],
            "1.5", "2 · 4", "10"
        )
        self.c_attack = SteppedKnobCell(
            "Attack", ATTACK_STEPS, ["0.1 ms", "0.3 ms", "1 ms", "3 ms", "10 ms", "30 ms"], 4, COLORS["cyan"],
            "0.1", "1 · 3", "30"
        )
        self.c_release = SteppedKnobCell(
            "Release", RELEASE_STEPS, ["0.1 s", "0.3 s", "0.6 s", "1.2 s", "AUTO"], 4, COLORS["amber"],
            "0.1", "0.6", "AUTO"
        )
        self.c_makeup = ContinuousKnobCell(
            "Make-Up", -5.0, 15.0, 2.0, COLORS["cyan"],
            "-5", "0", "+15",
            format_db_signed, parse_float_safe, bipolar=True
        )
        self.c_mix = ContinuousKnobCell(
            "Mix (Dry/Wet)", 0.0, 100.0, 100.0, COLORS["cyan"],
            "0%", "50%", "100%",
            lambda v: f"{int(round(v))}%", parse_float_safe, bipolar=False
        )
        self.c_sc_hpf = SteppedKnobCell(
            "SC HPF", SC_HPF_STEPS, ["OFF", "60 Hz", "90 Hz", "120 Hz", "185 Hz"], 1, COLORS["sc"],
            "OFF", "90", "185"
        )

        for cell in (
            self.c_thresh, self.c_ratio, self.c_attack,
            self.c_release, self.c_makeup, self.c_mix, self.c_sc_hpf
        ):
            knobs_row.addWidget(cell, 1)

        self._knob_row = knobs_row
        deck_lay.addLayout(knobs_row, 1)
        main_lay.addWidget(bottom_card, 4)

        # 信号绑定
        self.c_thresh.valueChanged.connect(lambda v: self._set_param("threshold", round(v, 1)))
        self.c_ratio.valueChanged.connect(lambda v: self._set_param("ratio", float(v)))
        self.c_attack.valueChanged.connect(lambda v: self._set_param("attack", float(v)))
        self.c_release.valueChanged.connect(lambda v: self._set_param("release", float(v)))
        self.c_makeup.valueChanged.connect(lambda v: self._set_param("makeup", round(v, 1)))
        self.c_mix.valueChanged.connect(lambda v: self._set_param("mix", round(v, 0)))
        self.c_sc_hpf.valueChanged.connect(lambda v: self._set_param("sc_hpf", int(v)))

    def use_processor_controls(self) -> None:
        from src.features.edit.core.audio_effects.bus_multiband import BUS_COMP_TEMPLATES

        self._processor_templates = BUS_COMP_TEMPLATES
        for state in (*self._banks.values(), *self._presets.values()):
            key = "mx_glue" if state["topology"] == "API 2500" else "dx_glue"
            template = BUS_COMP_TEMPLATES[key]
            if state["release"] < 0:
                state["release"] = template["release_ms"] / 1000.0
            state.setdefault("processor_knee", template["knee"] * 0.24)
        self.btn_ssl.setText("DX Glue")
        self.btn_api.setText("MX Glue")
        for attribute, field, label, lower, upper, default, color, unit in (
            ("c_thresh", "threshold", "Threshold", -60.0, 0.0, -22.0, "cyan", "dB"),
            ("c_ratio", "ratio", "Ratio", 1.2, 20.0, 2.5, "cyan", ":1"),
            ("c_attack", "attack", "Attack", 0.7, 1000.0, 12.0, "cyan", "ms"),
            ("c_release", "release", "Release", 0.05, 5.0, 0.14, "amber", "s"),
            ("c_makeup", "makeup", "Make-Up", 0.0, 24.0, 1.5, "cyan", "dB"),
            ("c_sc_hpf", "sc_hpf", I18N.tr("audio.fx.sc_hpf_hz", "SC HPF"), 0.0, 500.0, 120.0, "sc", "Hz"),
        ):
            old = getattr(self, attribute)
            cell = ContinuousKnobCell(
                label, lower, upper, default, COLORS[color], f"{lower:g}", unit, f"{upper:g}",
                lambda value, suffix=unit: f"{value:.3f}".rstrip("0").rstrip(".") + f" {suffix}",
                parse_float_safe,
            )
            cell.setMaximumWidth(135)
            self._knob_row.replaceWidget(old, cell)
            old.hide()
            old.deleteLater()
            setattr(self, attribute, cell)
            cell.valueChanged.connect(lambda value, key=field: self._set_param(key, round(value, 3)))
        self._knob_row.insertStretch(0)
        self._knob_row.addStretch()
        self.c_knee = ContinuousKnobCell(
            I18N.tr("audio.fx.knee", "Knee"), 0.0, 24.0, 4.32, COLORS["sc"],
            "0", "dB", "24", lambda v: f"{v:.1f} dB", parse_float_safe,
        )
        self.c_knee.setMaximumWidth(135)
        self._knob_row.insertWidget(self._knob_row.count() - 1, self.c_knee)
        self.c_knee.valueChanged.connect(lambda v: self._set_param("processor_knee", round(v, 1)))
        for button in (self.btn_auto_rel, self.btn_ext_sc, self.btn_sc_listen, self.btn_auto_gain):
            button.hide()
        self.bus_graph.setEnabled(False)
        self._refresh_all_views()

    def _build_meter_column(self, header_text: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(2, 4, 2, 2)
        col.setSpacing(4)
        lbl = QLabel(header_text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #787882; font-size: 10px; font-weight: 700; letter-spacing: 0.3px;")
        col.addWidget(lbl)
        return col

    # -----------------------------------------------------------------
    # 状态同步与双向交互逻辑
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _refresh_all_views(self):
        st = self._current_state()
        en = bool(st["enabled"])
        is_api = (st["topology"] == "API 2500")
        templates = getattr(self, "_processor_templates", None)
        main_accent = COLORS["amber"] if is_api else COLORS["cyan"]

        self.power_switch.setAccentColor(main_accent)
        self.power_switch.setChecked(en)

        active_seg = f"""
            QPushButton {{
                background-color: #182E38;
                color: {main_accent};
                border: 1px solid {main_accent};
                border-radius: 2.5px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 700;
            }}
        """
        inactive_seg = """
            QPushButton {
                background-color: transparent;
                color: #9A9AA2;
                border: none;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
        """
        self.btn_ssl.setStyleSheet(active_seg if not is_api else inactive_seg)
        self.btn_api.setStyleSheet(active_seg if is_api else inactive_seg)

        active_ab = """
            QPushButton {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
            }
        """
        inactive_ab = """
            QPushButton {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_ab if self._active_bank == "A" else inactive_ab)
        self.btn_b.setStyleSheet(active_ab if self._active_bank == "B" else inactive_ab)
        self.btn_copy_ab.setStyleSheet(inactive_ab)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

        for cell in (self.c_thresh, self.c_ratio, self.c_attack, self.c_makeup, self.c_mix):
            cell.setAccentColor(main_accent)

        self.c_thresh.setValue(st["threshold"])
        self.c_ratio.setValue(st["ratio"])
        self.c_attack.setValue(st["attack"])
        self.c_release.setValue(st["release"])
        self.c_makeup.setValue(st["makeup"])
        self.c_mix.setValue(st["mix"])
        self.c_sc_hpf.setValue(st["sc_hpf"])
        if templates is not None:
            template = templates["mx_glue" if is_api else "dx_glue"]
            self.c_knee.setValue(st.get("processor_knee", template["knee"] * 0.24))
            self.c_knee.setModuleActive(en)

        for cell in (
            self.c_thresh, self.c_ratio, self.c_attack,
            self.c_release, self.c_makeup, self.c_mix, self.c_sc_hpf
        ):
            cell.setModuleActive(en)

        is_auto = (st["release"] < 0)
        self.btn_auto_rel.setChecked(is_auto)
        self.btn_ext_sc.setChecked(bool(st["ext_sc"]))
        self.btn_sc_listen.setChecked(bool(st["sc_listen"]))

        def _toggle_btn_css(accent: str) -> str:
            return f"""
                QPushButton {{
                    background-color: #323238;
                    color: #8E92A0;
                    border: none;
                    border-radius: 3px;
                    font-size: 9.5px;
                    font-weight: 700;
                    letter-spacing: 0.4px;
                }}
                QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
                QPushButton:checked {{
                    background-color: #182E38;
                    color: {accent};
                    border: 1px solid {accent};
                }}
            """

        self.btn_auto_rel.setStyleSheet(_toggle_btn_css(COLORS["amber"]))
        self.btn_ext_sc.setStyleSheet(_toggle_btn_css(COLORS["sc"]))
        self.btn_sc_listen.setStyleSheet(_toggle_btn_css("#E8B838"))

        label = ("MX GLUE" if is_api else "DX GLUE") if templates is not None else None
        self.vu_meter.setTopology(st["topology"], label)
        self.vu_meter.setActive(en)
        self.bus_graph.setState(st)

    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _copy_bank(self):
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._banks[self._active_bank])

    def _on_power_toggled(self, enabled: bool):
        st = self._current_state()
        st["enabled"] = enabled
        self._refresh_all_views()
        self.bypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_topology_changed(self, topology: str):
        st = self._current_state()
        if st["topology"] != topology:
            st["topology"] = topology
            self._refresh_all_views()
            self.topologyChanged.emit(topology)
            self.paramsChanged.emit(self.get_state())
            self.stateChanged.emit(self.get_state())

    def _apply_auto_makeup(self):
        """基于参考节目基准 (-12 dBFS) 估算总线压缩补偿增益"""
        st = self._current_state()
        ref_level = -10.0
        over = max(0.0, ref_level - st["threshold"])
        est_gr = over * (1.0 - 1.0 / max(1.01, st["ratio"]))
        est_makeup = max(-5.0, min(15.0, round(est_gr * 0.85, 1)))
        st["makeup"] = est_makeup
        self.c_makeup.setValue(est_makeup)
        self.bus_graph.setState(st)
        self.paramsChanged.emit(self.get_state())
        self.stateChanged.emit(self.get_state())

    def _on_auto_release_toggled(self, checked: bool):
        st = self._current_state()
        st["release"] = -1.0 if checked else 0.3
        self._refresh_all_views()
        self.paramsChanged.emit(self.get_state())
        self.stateChanged.emit(self.get_state())

    def _on_ext_sc_toggled(self, checked: bool):
        st = self._current_state()
        st["ext_sc"] = checked
        self.paramsChanged.emit(self.get_state())
        self.stateChanged.emit(self.get_state())

    def _on_sc_listen_toggled(self, checked: bool):
        st = self._current_state()
        st["sc_listen"] = checked
        self.bus_graph.setState(st)
        self.paramsChanged.emit(self.get_state())
        self.stateChanged.emit(self.get_state())

    def _on_graph_thresh_dragged(self, val: float):
        st = self._current_state()
        st["threshold"] = val
        self.c_thresh.setValue(val)
        self.bus_graph.setState(st)
        self.paramsChanged.emit(self.get_state())
        self.stateChanged.emit(self.get_state())

    def _on_graph_ratio_dragged(self, val: float):
        st = self._current_state()
        st["ratio"] = val
        self.c_ratio.setValue(val)
        self.bus_graph.setState(st)
        self.paramsChanged.emit(self.get_state())
        self.stateChanged.emit(self.get_state())

    def _set_param(self, key: str, val):
        st = self._current_state()
        st[key] = val
        if key == "release":
            self.btn_auto_rel.setChecked(val < 0)
        self.bus_graph.setState(st)
        self.paramsChanged.emit(self.get_state())
        self.stateChanged.emit(self.get_state())

    def _populate_grouped_presets(self):
        """按用途分组填充预设下拉框，分组标题行禁用点击仅作分隔"""
        groups = [
            ("── DEFAULT & BYPASS ──", [
                "SSL Drum Bus Glue (4:1 · Auto)",
                "Flat Bypass",
            ]),
            ("── DRUMS & PERCUSSION ──", [
                "Drum Bus · Snappy Punch (4:1 · 10ms)",
                "Drum Bus · Heavy Room Smash (10:1 · NY)",
                "Drum Bus · Vintage Warmth (2:1 · 30ms)",
                "Percussion & Loops Tightener (4:1 · 3ms)",
            ]),
            ("── VOCAL BUS ──", [
                "Lead Vocal Bus · Silky Glue (4:1 · Auto)",
                "Backing Vocals & Choir Lock (4:1 · Fast)",
                "Pop Vocal · Upfront Parallel (10:1 · NY)",
            ]),
            ("── INSTRUMENTS & MIX BUS ──", [
                "Guitars & Synths Wall of Sound (4:1)",
                "Acoustic & Strings Natural Cohesion (1.5:1)",
                "Keys & Pads Smooth Leveler (2:1 · 10ms)",
                "Master Bus · Classic 2dB Glue (2:1 · Auto)",
                "Master Bus · Transparent Mastering (1.5:1)",
                "Master Bus · EDM / Hip-Hop Pump (4:1 · 90Hz)",
                "Master Bus · Rock & Metal Aggression (4:1)",
                "Film Score & Orchestral Wide Bus (1.5:1 · 1.2s)",
            ]),
        ]
        model = self.preset_combo.model()
        for header, items in groups:
            self.preset_combo.addItem(header)
            idx = self.preset_combo.count() - 1
            item = model.item(idx)
            item.setEnabled(False)
            item.setForeground(QColor("#6E7280"))
            for name in items:
                if name in self._presets:
                    self.preset_combo.addItem(name)
        self.preset_combo.setCurrentText("SSL Drum Bus Glue (4:1 · Auto)")

    def _load_preset(self, name: str):
        if name in self._presets:
            self._banks[self._active_bank] = copy.deepcopy(self._presets[name])
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        """‹ › 步进切换预设，自动跳过禁用的分组标题行"""
        cnt = self.preset_combo.count()
        if cnt <= 0:
            return
        idx = self.preset_combo.currentIndex()
        for _ in range(cnt):
            idx = (idx + step) % cnt
            item = self.preset_combo.model().item(idx)
            if item is not None and item.isEnabled():
                self.preset_combo.setCurrentIndex(idx)
                return

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存 Bus Compressor 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 演示动态模拟（接入真实音频引擎 update_meters 后自动停止）
    # -----------------------------------------------------------------
    def _update_io_badge(self, in_db: float, out_db: float):
        """刷新 In / Out / Δ Gain 等响度校准读数（Δ 超过 ±1.5 dB 时琥珀色警示）"""
        delta = out_db - in_db
        delta_col = "#F0A848" if abs(delta) > 1.5 else "#4CD490"
        self.lbl_io_readout.setText(
            f"IN {in_db:+5.1f} dB  │  OUT {out_db:+5.1f} dB  │  "
            f"<span style='color:{delta_col};'>Δ {delta:+4.1f} dB</span>"
        )

    def _tick_demo_meters(self):
        self._anim_t += 0.11
        st = self._current_state()

        beat_pulse = max(0.0, math.sin(self._anim_t * 2.2)) ** 3
        in_db = -14.5 + 10.5 * beat_pulse + 2.5 * math.cos(self._anim_t * 1.3) + random.uniform(-0.8, 0.8)
        in_db = max(-45.0, min(-1.0, in_db))

        out_db = self.bus_graph.evaluate_transfer(in_db)
        gr_db = max(0.0, (in_db + (st["makeup"] if st["enabled"] else 0.0)) - out_db) if st["enabled"] else 0.0

        self.meter_input.setValue(in_db)
        self.meter_output.setValue(out_db)
        self.vu_meter.setGainReduction(gr_db)
        self.bus_graph.pushSample(in_db)
        self._update_io_badge(in_db, out_db)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_bus_comp(self):
        self._banks[self._active_bank] = default_bus_comp_flat()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_meters(self, input_db: float, gr_db: float, output_db: float):
        """
        接入真实音频引擎实时电平与增益衰减值 (dBFS / dB)：
          - input_db: 输入总线电平 (dBFS)
          - gr_db: 增益衰减量 (正值 dB, 如 3.2 表示 -3.2 dB 衰减)
          - output_db: 输出总线电平 (dBFS)
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.meter_input.setValue(input_db)
        self.vu_meter.setGainReduction(gr_db)
        self.meter_output.setValue(output_db)
        self.bus_graph.pushSample(input_db, output_db)
        self._update_io_badge(input_db, output_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioBusCompWidget("Master Bus")
    win.show()
    sys.exit(app.exec())
