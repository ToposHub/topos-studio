from __future__ import annotations

import sys
import math
import copy
import numpy as np
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, QSignalBlocker, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont, QPolygonF, QStandardItemModel
)

from src.features.edit.core.audio_effects.modulation_analysis import (
    modulation_delay_response,
    phaser_response,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


# =====================================================================
# 1. 全局色彩系统与声学常量（统一 Fairlight 深色哑光视觉规范）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":     "#28D8F0",  # 主控 / A-B 激活 / 立体声扩展指示
    "chorus":   "#4CD490",  # Chorus 模块主题色（翡翠绿：加厚 / 人声 Doubles）
    "flanger":  "#F0A848",  # Flanger 模块主题色（琥珀金：MXR 喷气 / 金属梳状）
    "phaser":   "#B67CF8",  # Phaser 模块主题色（紫罗兰：2/4/6/8 级全通运动）
    "coral":    "#F26D6D",  # 削波 / 右声道相位对比色
}

FREQ_MIN = 40.0
FREQ_MAX = 16000.0


def format_hz(val: float) -> str:
    if val >= 1000.0:
        return f"{val / 1000.0:.2f} kHz"
    if val < 1.0:
        return f"{val:.2f} Hz"
    if val < 10.0:
        return f"{val:.1f} Hz"
    return f"{int(round(val))} Hz"


def format_pct(val: float) -> str:
    return f"{int(round(val))}%"


def format_signed_pct(val: float) -> str:
    iv = int(round(val))
    if iv == 0:
        return "0%"
    return f"{iv:+d}%"


def format_ms(val: float) -> str:
    if val < 10.0:
        return f"{val:.1f} ms"
    return f"{int(round(val))} ms"


def parse_numeric(text: str, fallback: float) -> float:
    s = (
        text.strip()
        .lower()
        .replace("khz", "")
        .replace("hz", "")
        .replace("ms", "")
        .replace("%", "")
        .replace("deg", "")
        .replace("°", "")
        .strip()
    )
    try:
        val = float(s)
        if "k" in text.lower():
            val *= 1000.0
        return val
    except ValueError:
        return fallback


def default_mod_demo() -> dict:
    """默认演示状态：合成器/吉他经典立体声加厚 (Arturia JUN-6 风格 Chorus)"""
    return {
        "enabled": True,
        "routing": "Serial",      # "Serial" | "Parallel"
        "output_gain": 0.0,
        "chorus": {
            "enabled": True,
            "voicing": "Analog",  # "Analog" | "Dim-D" | "Dual"
            "rate": 0.55,         # 0.05 ~ 10.0 Hz (LFO 调制速率)
            "depth": 62.0,        # 0 ~ 100 % (调制深度)
            "delay_ms": 9.5,      # 2.0 ~ 35.0 ms (紧致加厚 2~10ms / 人声 Double 12~28ms)
            "spread": 145.0,      # 0 ~ 200 % (立体声扩展宽度 / L-R 相位差)
            "tone": 8.0,          # -50 ~ +50 % (BBD 模拟暖度到高频明亮度)
            "mix": 58.0,          # 0 ~ 100 % (干湿比混合)
        },
        "flanger": {
            "enabled": False,
            "mode": "Classic",    # "Classic" | "Thru-0" (MXR M117R / NI Flair)
            "rate": 0.28,         # 0.05 ~ 10.0 Hz
            "depth": 78.0,        # 0 ~ 100 %
            "feedback": 68.0,     # -100 ~ +100 % (正反馈喷气共鸣 / 负反馈中空金属质感)
            "manual_ms": 2.4,     # 0.2 ~ 15.0 ms (基准梳状延迟时间)
            "spread": 135.0,      # 0 ~ 200 % (立体声扩展)
            "mix": 50.0,          # 0 ~ 100 %
        },
        "phaser": {
            "enabled": False,
            "stages": 4,          # 2 | 4 | 6 | 8 (全通滤波器级数)
            "rate": 0.42,         # 0.05 ~ 10.0 Hz
            "depth": 72.0,        # 0 ~ 100 %
            "feedback": 48.0,     # -100 ~ +100 % (极点共振反馈)
            "center_hz": 880.0,   # 100 ~ 4000 Hz (扫频中心频率)
            "spread": 140.0,      # 0 ~ 200 % (立体声扩展)
            "mix": 55.0,          # 0 ~ 100 %
        },
    }


def default_mod_neutral() -> dict:
    st = default_mod_demo()
    st["chorus"]["enabled"] = True
    st["chorus"]["rate"] = 0.40
    st["chorus"]["depth"] = 0.0
    st["chorus"]["spread"] = 100.0
    st["chorus"]["tone"] = 0.0
    st["chorus"]["mix"] = 50.0

    st["flanger"]["enabled"] = False
    st["flanger"]["depth"] = 0.0
    st["flanger"]["feedback"] = 0.0
    st["flanger"]["spread"] = 100.0
    st["flanger"]["mix"] = 50.0

    st["phaser"]["enabled"] = False
    st["phaser"]["stages"] = 4
    st["phaser"]["depth"] = 0.0
    st["phaser"]["feedback"] = 0.0
    st["phaser"]["spread"] = 100.0
    st["phaser"]["mix"] = 50.0
    st["output_gain"] = 0.0
    return st


# =====================================================================
# 2. 控件：发光电源开关（支持模块专属主题色）
# =====================================================================
class PowerToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True, accent_color: str = "#28D8F0", parent=None):
        super().__init__(parent)
        self._checked = checked
        self._accent = QColor(accent_color)
        self.setFixedSize(34, 17)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

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


# =====================================================================
# 3. 控件：内嵌可编辑数字胶囊
# =====================================================================
class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 64, parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._active = True
        self.setFixedSize(width, 21)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(0, 0, width, 21)
        self._editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._editor.setStyleSheet(f"""
            QLineEdit {{
                background-color: #151518;
                color: {accent_color};
                border: 1px solid {accent_color};
                border-radius: 3px;
                font-family: {MONO_FONT_CSS};
                font-size: 11px;
                font-weight: 600;
                padding: 0px;
            }}
        """)
        self._editor.hide()
        self._editor.returnPressed.connect(self._commit_edit)
        self._editor.editingFinished.connect(self._commit_edit)

    def setText(self, text: str):
        if self._text != text:
            self._text = text
            self.update()

    def setModuleActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            clean = (
                self._text.replace("kHz", "")
                .replace("Hz", "")
                .replace("ms", "")
                .replace("%", "")
                .replace("+", "")
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


# =====================================================================
# 4. 控件：拉丝阳极铝质感旋钮（支持 linear / bipolar / log / power）
# =====================================================================
class ProRotaryKnob(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value: float,
        default_value: float,
        accent_color: str = "#28D8F0",
        mode: str = "linear",
        parent=None,
    ):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._mode = mode
        self._active = True
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(36, 36)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

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
        if self._mode == "log":
            v = max(1e-3, val)
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return math.log(v / mn) / math.log(mx / mn)
        if self._mode == "power":
            ratio = max(0.0, min(1.0, (val - self._min) / (self._max - self._min)))
            return ratio ** (1.0 / 2.0)
        if self._mode == "bipolar" and self._min < 0.0 < self._max:
            if val < 0.0:
                return 0.5 * (val - self._min) / (0.0 - self._min)
            return 0.5 + 0.5 * (val / self._max)
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        n = max(0.0, min(1.0, norm))
        if self._mode == "log":
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return mn * ((mx / mn) ** n)
        if self._mode == "power":
            return self._min + (n ** 2.0) * (self._max - self._min)
        if self._mode == "bipolar" and self._min < 0.0 < self._max:
            if n < 0.5:
                return self._min + (n / 0.5) * (0.0 - self._min)
            return ((n - 0.5) / 0.5) * self._max
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
            sens = 0.0015 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.0065
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
        step = 0.01 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.03
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

        arc_r = 14.8
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深色凹槽底环
        p.setPen(QPen(QColor("#141417"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性模式在 12 点钟方向绘制中心基准刻度点
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 2. 模块主题色光环
        arc_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(QPen(arc_col, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._mode == "bipolar":
            zero_deg = 90.0
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        # 3. 旋钮黑边凹槽 + 哑光浅灰旋钮顶盖
        knob_r = 11.0
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 4. 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.2
        iy1 = cy - math.sin(rad) * 3.2
        ix2 = cx + math.cos(rad) * (knob_r - 1.4)
        iy2 = cy - math.sin(rad) * (knob_r - 1.4)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 5. 控件：垂直高精电平/调制活跃度指示条
# =====================================================================
class ProVerticalMeter(QWidget):
    def __init__(
        self,
        title: str = "",
        mode: str = "level",      # "level" (-55~0 dB) | "activity" (0~100%)
        accent_color: str = "#28D8F0",
        show_ticks: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._title = title
        self._mode = mode
        self._accent = QColor(accent_color)
        self._show_ticks = show_ticks

        self._value = -55.0 if mode == "level" else 0.0
        self._peak = self._value
        self._clipped = False
        self._active = True

        self.setFixedWidth(40 if show_ticks else 24)
        self.setMinimumHeight(180)

    def setValue(self, val: float):
        if self._mode == "level":
            self._value = max(-55.0, min(0.0, val))
            if self._value >= self._peak:
                self._peak = self._value
            else:
                self._peak = max(self._value, self._peak - 0.4)
            if val >= -0.1:
                self._clipped = True
        else:
            self._value = max(0.0, min(100.0, val))
            if self._value >= self._peak:
                self._peak = self._value
            else:
                self._peak = max(self._value, self._peak - 0.8)
        self.update()

    def setActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._clipped = False
            self._peak = self._value
            self.update()

    def _val_to_norm(self, v: float) -> float:
        if self._mode == "level":
            if v >= -20.0:
                return 0.52 + ((v + 20.0) / 20.0) * 0.48
            return ((v + 55.0) / 35.0) * 0.52
        return v / 100.0

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 10.0
        bot_pad = 18.0 if self._title else 6.0
        bar_w = 9.0
        bar_x = (self.width() - bar_w - 3.0) if self._show_ticks else (self.width() - bar_w) / 2.0
        bar = QRectF(bar_x, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))

        if self._show_ticks:
            p.setFont(QFont(MONO_FONT_FAM, 7))
            ticks = [0, -10, -20, -35, -50] if self._mode == "level" else [100, 75, 50, 25, 0]
            for t in ticks:
                norm = self._val_to_norm(float(t))
                ty = bar.bottom() - norm * bar.height()
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
            norm = max(0.0, min(1.0, self._val_to_norm(self._value)))
            fill_h = norm * bar.height()
            if fill_h > 1.0:
                fill_rect = QRectF(bar.left() + 1.0, bar.bottom() - fill_h, bar.width() - 2.0, fill_h - 0.5)
                if self._mode == "level":
                    grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                    grad.setColorAt(0.0,  QColor("#26C281"))
                    grad.setColorAt(0.52, QColor("#4CD490"))
                    grad.setColorAt(0.75, QColor("#F0A848"))
                    grad.setColorAt(1.0,  QColor("#FF4848"))
                    p.setBrush(grad)
                else:
                    p.setBrush(self._accent)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(fill_rect, 1.2, 1.2)

            p_norm = max(0.0, min(1.0, self._val_to_norm(self._peak)))
            if p_norm > 0.02:
                py = bar.bottom() - p_norm * bar.height()
                p.setPen(QPen(QColor("#ECECF0"), 1.3))
                p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

            if self._mode == "level":
                clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
                p.drawRoundedRect(clip_rect, 1.0, 1.0)

        p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
        gy = bar.top() + 3.0
        while gy < bar.bottom():
            p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
            gy += 3.0

        if self._title:
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
            p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, self._title)


# =====================================================================
# 6. 控件：右侧 Output 推子
# =====================================================================
class MasterOutputStrip(QWidget):
    gainChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gain = 0.0
        self._dragging = False
        self._hovered = False
        self.setFixedWidth(48)
        self.setMinimumHeight(180)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setGain(self, val: float, emit: bool = False):
        clamped = max(-20.0, min(20.0, round(val, 1)))
        if abs(clamped - self._gain) > 1e-4:
            self._gain = clamped
            self.update()
            if emit:
                self.gainChanged.emit(self._gain)

    def _track_rect(self) -> QRectF:
        return QRectF(22.0, 10.0, 4.0, max(40.0, self.height() - 36.0))

    def _y_to_gain(self, y: float) -> float:
        tr = self._track_rect()
        norm = max(0.0, min(1.0, (y - tr.top()) / tr.height()))
        return 20.0 - norm * 40.0

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.setGain(self._y_to_gain(event.position().y()), emit=True)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.setGain(self._y_to_gain(event.position().y()), emit=True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setGain(0.0, emit=True)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        self.setGain(self._gain + delta * 0.5, emit=True)
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        tr = self._track_rect()
        cx = tr.center().x()

        p.setPen(QPen(QColor("#52525B"), 1.0))
        for i in range(5):
            ty = tr.top() + (i / 4.0) * tr.height()
            p.drawLine(QPointF(cx - 13, ty), QPointF(cx - 9, ty))
            p.drawLine(QPointF(cx + 9, ty), QPointF(cx + 13, ty))

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        norm = (20.0 - self._gain) / 40.0
        cy = tr.top() + norm * tr.height()
        cap_w, cap_h = 18.0, 22.0
        cap_rect = QRectF(cx - cap_w / 2.0, cy - cap_h / 2.0, cap_w, cap_h)

        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawRoundedRect(cap_rect, 3.0, 3.0)

        p.setPen(QPen(QColor("#28D8F0"), 1.6))
        p.drawLine(QPointF(cap_rect.left() + 3, cy), QPointF(cap_rect.right() - 3, cy))

        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor("#28D8F0"))
        val_str = f"{self._gain:+.1f}" if abs(self._gain) >= 0.05 else "0.0"
        p.drawText(QRectF(0, self.height() - 21, self.width(), 18), Qt.AlignmentFlag.AlignCenter, val_str)


# =====================================================================
# 7. 控件：核心交互式“梳状/全通扫频响应 + L/R 立体声相位示波 + 2D Rate×Depth 画布”
# =====================================================================
class ProModulationVisualizerWidget(QWidget):
    # 直接在画布上拖拽 2D Rate(X) × Depth(Y) 坐标锚点的双向信号
    rateDepthDragged = pyqtSignal(str, float, float, bool, bool)  # module_key, rate_hz, depth_pct, r_chg, d_chg
    spreadScrolled = pyqtSignal(str, float)                        # module_key, spread_pct
    rateDepthResetRequested = pyqtSignal(str)                      # module_key

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(460, 230)
        self.setMouseTracking(True)

        self._state = default_mod_demo()
        self._processor_controls = False
        self._focused_module = "chorus"  # "chorus" | "flanger" | "phaser"
        self._show_comb_curve = True
        self._show_stereo_lfo = True
        self._show_xy_puck = True

        self._hover_puck = False
        self._dragging_puck = False
        self._last_drag_pos = QPointF()
        self._accum_rate_norm = 0.3
        self._accum_depth = 60.0
        self._mouse_pos: QPointF | None = None

        self._btn_comb_rect = QRectF()
        self._btn_lfo_rect = QRectF()
        self._btn_xy_rect = QRectF()

        self._ml = 34.0
        self._mr = 16.0
        self._mt = 10.0
        self._mb = 24.0

        self._num_bins = 260
        self._freqs = [
            FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** (i / (self._num_bins - 1)))
            for i in range(self._num_bins)
        ]
        self._comb_curve_db = [0.0] * self._num_bins
        self._lfo_left = [0.0] * 140
        self._lfo_right = [0.0] * 140
        self._phase_spread_deg = 120.0
        self._processor_legacy = False
        self._phase = 0.0
        self._sample_rate = 48000.0
        self._response_key = ()

    def setState(self, state: dict):
        self._state = state
        self.update()

    def set_sample_rate(self, sample_rate: float) -> None:
        if sample_rate > 0.0 and sample_rate != self._sample_rate:
            self._sample_rate = sample_rate
            self.update()

    def set_processor_phase(self, phase: float) -> None:
        if phase != self._phase:
            self._phase = phase
            if self._show_comb_curve or self._show_stereo_lfo:
                self.update()

    def _update_processor_plot(self) -> None:
        mod = self._state[self._focused_module]
        key = (self._focused_module, self._processor_legacy, self._sample_rate,
               self._phase, self._show_comb_curve, self._show_stereo_lfo,
               tuple(mod.items()))
        if key == self._response_key:
            return
        self._response_key = key
        phaser = self._focused_module == "phaser"
        if self._processor_legacy and not phaser:
            self._freqs, self._comb_curve_db = [], []
            self._lfo_left, self._lfo_right = [], []
            return
        feedback = mod.get("feedback", 0.0) / 100.0
        mix = mod["mix"] / 100.0
        if self._processor_legacy:
            feedback = max(0.0, feedback)
        voices = max(1, min(4, int(mod.get("voices", 2))))
        if self._show_comb_curve:
            upper = min(FREQ_MAX, self._sample_rate * 0.49)
            freqs = np.geomspace(FREQ_MIN, upper, self._num_bins)
            if phaser:
                stages = min(4 if self._processor_legacy else 12, mod["stages"])
                curve = phaser_response(
                    freqs, self._sample_rate, self._phase, base_hz=mod["center_hz"],
                    sweep_octaves=0.5 + 4.5 * mod["depth"] / 100.0,
                    stages=stages, feedback=feedback, mix=mix)
            else:
                delay = mod["delay_ms"] if self._focused_module == "chorus" else mod["manual_ms"]
                curve = modulation_delay_response(
                    freqs, self._sample_rate, self._phase, base_ms=delay,
                    depth_ms=mod["depth"] / 10.0, voices=voices,
                    feedback=feedback, mix=mix)
            self._freqs, self._comb_curve_db = freqs.tolist(), curve.tolist()
        if self._show_stereo_lfo:
            phase = self._phase + np.linspace(0.0, 2.0 * np.pi, 100)
            self._lfo_left = np.sin(phase).tolist()
            self._lfo_right = (np.sin(phase + 2.0 * np.pi / voices).tolist()
                               if not phaser and voices > 1 else [])

    def setFocusedModule(self, mod_key: str):
        if mod_key in ("chorus", "flanger", "phaser") and self._focused_module != mod_key:
            self._focused_module = mod_key
            self.update()

    def updateAnalysis(
        self,
        freqs_hz: list[float],
        comb_curve_db: list[float],
        lfo_left: list[float],
        lfo_right: list[float],
        phase_spread_deg: float,
    ):
        self._freqs = freqs_hz
        self._comb_curve_db = comb_curve_db
        self._lfo_left = lfo_left
        self._lfo_right = lfo_right
        self._phase_spread_deg = phase_spread_deg
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(100.0, self.width() - self._ml - self._mr),
            max(100.0, self.height() - self._mt - self._mb),
        )

    def _freq_to_x(self, freq: float, plot: QRectF) -> float:
        f = max(FREQ_MIN, min(FREQ_MAX, freq))
        norm = math.log10(f / FREQ_MIN) / math.log10(FREQ_MAX / FREQ_MIN)
        return plot.left() + norm * plot.width()

    def _db_to_y(self, db: float, plot: QRectF) -> float:
        clamped = max(-18.0, min(18.0, db))
        norm = (18.0 - clamped) / 36.0
        return plot.top() + norm * plot.height()

    def _rate_to_norm(self, rate_hz: float) -> float:
        minimum, maximum = (0.01, 20.0) if self._processor_controls else (0.05, 10.0)
        rate = max(minimum, min(maximum, rate_hz))
        return math.log(rate / minimum) / math.log(maximum / minimum)

    def _norm_to_rate(self, norm: float) -> float:
        minimum, maximum = (0.01, 20.0) if self._processor_controls else (0.05, 10.0)
        return minimum * ((maximum / minimum) ** max(0.0, min(1.0, norm)))

    # 2D 调制坐标锚点映射 (当前选中模块)：
    # X 轴：Rate  (0.05 Hz ~ 10.0 Hz, 对数映射)
    # Y 轴：Depth (0% ~ 100%)
    def _puck_pos(self, plot: QRectF) -> QPointF:
        mod = self._state[self._focused_module]
        nx = self._rate_to_norm(mod["rate"])
        ny = 1.0 - (mod["depth"] / 100.0)
        px = plot.left() + plot.width() * (0.10 + nx * 0.80)
        py = plot.top() + plot.height() * (0.14 + ny * 0.72)
        return QPointF(px, py)

    # -----------------------------------------------------------------
    # 交互事件：画布内拖拽 2D Rate×Depth 锚点 / 滚轮调 Stereo Spread / 双击复位
    # -----------------------------------------------------------------
    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_puck = False
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        plot = self._plot_rect()
        pp = self._puck_pos(plot)
        on_puck = (
            self._show_xy_puck
            and self._state["enabled"]
            and (math.hypot(pos.x() - pp.x(), pos.y() - pp.y()) <= 16.0)
        )

        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_comb_rect.contains(pos):
                self._show_comb_curve = not self._show_comb_curve
                self.update()
                return
            if self._btn_lfo_rect.contains(pos):
                self._show_stereo_lfo = not self._show_stereo_lfo
                self.update()
                return
            if self._btn_xy_rect.contains(pos):
                self._show_xy_puck = not self._show_xy_puck
                self.update()
                return

            if on_puck:
                mod = self._state[self._focused_module]
                self._dragging_puck = True
                self._last_drag_pos = pos
                self._accum_rate_norm = self._rate_to_norm(mod["rate"])
                self._accum_depth = float(mod["depth"])
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()

        elif event.button() == Qt.MouseButton.RightButton and on_puck:
            self.rateDepthResetRequested.emit(self._focused_module)

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._dragging_puck:
            dx = event.position().x() - self._last_drag_pos.x()
            dy = event.position().y() - self._last_drag_pos.y()
            self._last_drag_pos = event.position()

            mods = event.modifiers()
            scale = 0.22 if (mods & Qt.KeyboardModifier.ShiftModifier) else 1.0
            span_x = max(1.0, plot.width() * 0.80)
            span_y = max(1.0, plot.height() * 0.72)

            lock_d = bool(mods & Qt.KeyboardModifier.ControlModifier)
            lock_r = bool(mods & Qt.KeyboardModifier.AltModifier)

            if not lock_r:
                self._accum_rate_norm = max(0.0, min(1.0, self._accum_rate_norm + (dx / span_x) * scale))
            if not lock_d:
                self._accum_depth = max(0.0, min(100.0, self._accum_depth - (dy / span_y) * 100.0 * scale))

            rate_val = round(self._norm_to_rate(self._accum_rate_norm), 2)
            depth_val = round(self._accum_depth, 0)
            self.rateDepthDragged.emit(self._focused_module, rate_val, depth_val, not lock_r, not lock_d)
        else:
            if self._show_xy_puck and self._state["enabled"]:
                pp = self._puck_pos(plot)
                hit = math.hypot(event.position().x() - pp.x(), event.position().y() - pp.y()) <= 16.0
                self._hover_puck = hit
                self.setCursor(Qt.CursorShape.OpenHandCursor if hit else Qt.CursorShape.CrossCursor)
            else:
                self._hover_puck = False
                self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_puck:
            self._dragging_puck = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._hover_puck else Qt.CursorShape.CrossCursor)
            self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._hover_puck:
            self.rateDepthResetRequested.emit(self._focused_module)

    def wheelEvent(self, event):
        # 画布内滚轮直接微调当前焦点模块的 Stereo Spread（立体声扩展）
        if not self._processor_controls and self._plot_rect().contains(event.position()):
            delta = event.angleDelta().y() / 120.0
            step = 2.0 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 8.0
            cur_sp = self._state[self._focused_module]["spread"]
            new_sp = max(0.0, min(200.0, round(cur_sp + delta * step, 0)))
            self.spreadScrolled.emit(self._focused_module, new_sp)
            event.accept()

    # -----------------------------------------------------------------
    # 绘制网格、L/R 立体声 LFO 相位示波、梳状/全通扫频曲线与 2D 锚点
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 画布哑光深底
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 水平 dB 网格 (+18 ~ -18 dB)
        grid_pen = QPen(QColor("#26262A"), 1.0)
        zero_pen = QPen(QColor("#3A3A40"), 1.1)
        p.setFont(QFont(MONO_FONT_FAM, 8))
        for db in (15, 10, 5, 0, -5, -10, -15):
            y = self._db_to_y(float(db), plot)
            p.setPen(zero_pen if db == 0 else grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor("#9A9AA2"))
            lbl = f"+{db}" if db > 0 else str(db)
            p.drawText(QRectF(0, y - 8, self._ml - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        # 3. 垂直对数频率刻度线
        freq_ticks = [
            (60.0, "60"), (125.0, "125"), (250.0, "250"), (500.0, "500"),
            (1000.0, "1K"), (2000.0, "2K"), (4000.0, "4K"), (8000.0, "8K"), (14000.0, "14K")
        ]
        p.setPen(QColor("#9A9AA2"))
        p.drawText(QRectF(plot.left(), plot.bottom() + 5, 22, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Hz")
        for f_val, f_lbl in freq_ticks:
            x = self._freq_to_x(f_val, plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#9A9AA2"))
            p.drawText(QRectF(x - 18, plot.bottom() + 5, 36, 16), Qt.AlignmentFlag.AlignCenter, f_lbl)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        st = self._state
        enabled = st["enabled"]
        if enabled and self._processor_controls:
            self._update_processor_plot()
        focus_col = QColor(COLORS[self._focused_module])

        # 4. 立体声 L / R 双通道 LFO 相位波形叠层 (直观反映 Stereo Spread 扩展宽度)
        if enabled and self._show_stereo_lfo and len(self._lfo_left) > 2:
            lfo_mid_y = plot.bottom() - 34.0
            lfo_amp = 20.0
            path_l = QPainterPath()
            path_r = QPainterPath()
            n_pts = len(self._lfo_left)
            for i in range(n_pts):
                lx = plot.left() + (i / (n_pts - 1)) * plot.width()
                yl = lfo_mid_y - self._lfo_left[i] * lfo_amp
                if i == 0:
                    path_l.moveTo(lx, yl)
                else:
                    path_l.lineTo(lx, yl)
                if self._lfo_right:
                    yr = lfo_mid_y - self._lfo_right[i] * lfo_amp
                    if i == 0:
                        path_r.moveTo(lx, yr)
                    else:
                        path_r.lineTo(lx, yr)

            p.setPen(QPen(QColor(40, 216, 240, 125), 1.3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path_l)

            if self._lfo_right:
                p.setPen(QPen(QColor(242, 109, 109, 125), 1.3, Qt.PenStyle.DashLine))
                p.drawPath(path_r)

            if not self._processor_controls:
                p.setFont(QFont(MONO_FONT_FAM, 7))
                p.setPen(QColor(175, 182, 198, 120))
                p.drawText(
                    QRectF(plot.left() + 8, plot.bottom() - 16, 240, 14),
                    Qt.AlignmentFlag.AlignLeft,
                    f"STEREO LFO PHASE Δ: {int(round(self._phase_spread_deg))}°  (L=CYAN / R=CORAL)",
                )

        # 5. 梳状滤波 / 全通移相动态频响扫频曲线
        if self._show_comb_curve and len(self._freqs) > 1:
            zero_y = self._db_to_y(0.0, plot)
            curve_path = QPainterPath()
            fill_poly = QPolygonF()
            first_x = self._freq_to_x(self._freqs[0], plot)
            fill_poly.append(QPointF(first_x, zero_y))

            for i, f in enumerate(self._freqs):
                sx = self._freq_to_x(f, plot)
                db_v = self._comb_curve_db[i] if enabled else 0.0
                sy = self._db_to_y(db_v, plot)
                if i == 0:
                    curve_path.moveTo(sx, sy)
                else:
                    curve_path.lineTo(sx, sy)
                fill_poly.append(QPointF(sx, sy))

            last_x = self._freq_to_x(self._freqs[-1], plot)
            fill_poly.append(QPointF(last_x, zero_y))

            main_col = focus_col if enabled else QColor("#6E6E76")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(main_col.red(), main_col.green(), main_col.blue(), 34))
            p.drawPolygon(fill_poly)

            p.setPen(QPen(QColor(main_col.red(), main_col.green(), main_col.blue(), 52), 4.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(curve_path)

            p.setPen(QPen(main_col, 1.85))
            p.drawPath(curve_path)

            # 当 Phaser 启用时，标注当前级数对应的全通陷波器徽标 (2/4/6/8 Stages -> 1/2/3/4 Notches)
            if enabled and st["phaser"]["enabled"] and not self._processor_controls:
                stages = int(st["phaser"]["stages"])
                num_notches = max(1, stages // 2)
                c_hz = float(st["phaser"]["center_hz"])
                p.setFont(QFont(MONO_FONT_FAM, 7))
                for n_i in range(num_notches):
                    spread_factor = 0.42 + (n_i / max(1, num_notches - 1)) * 1.65 if num_notches > 1 else 1.0
                    nf = max(FREQ_MIN, min(FREQ_MAX, c_hz * spread_factor))
                    nx = self._freq_to_x(nf, plot)
                    badge = QRectF(nx - 12, plot.top() + 34, 24, 13)
                    p.setPen(QPen(QColor(COLORS["phaser"]), 1.0))
                    p.setBrush(QColor(22, 18, 30, 220))
                    p.drawRoundedRect(badge, 3.0, 3.0)
                    p.setPen(QColor("#E0C8FF"))
                    p.drawText(badge, Qt.AlignmentFlag.AlignCenter, f"N{n_i + 1}")

        # 6. 2D 调制坐标锚点 (X: Rate 速率, Y: Depth 深度)
        if enabled and self._show_xy_puck:
            pp = self._puck_pos(plot)
            is_hi = self._hover_puck or self._dragging_puck
            r = 12.0 if is_hi else 9.8

            if is_hi:
                p.setFont(QFont(MONO_FONT_FAM, 7))
                p.setPen(QColor(170, 178, 196, 135))
                p.drawText(QRectF(plot.left() + 10, pp.y() - 16, 110, 14), Qt.AlignmentFlag.AlignLeft, "← SLOW LUSH")
                p.drawText(QRectF(plot.right() - 115, pp.y() - 16, 105, 14), Qt.AlignmentFlag.AlignRight, "FAST VIBRATO →")

            p.setPen(QPen(QColor(focus_col.red(), focus_col.green(), focus_col.blue(), 95 if is_hi else 36), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(pp.x(), plot.top()), QPointF(pp.x(), plot.bottom()))
            p.drawLine(QPointF(plot.left(), pp.y()), QPointF(plot.right(), pp.y()))

            if is_hi:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(focus_col.red(), focus_col.green(), focus_col.blue(), 58))
                p.drawEllipse(pp, r + 6.5, r + 6.5)

            p.setPen(QPen(focus_col, 2.0))
            p.setBrush(QColor(21, 21, 24, 225))
            p.drawEllipse(pp, r, r)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#28D8F0"))
            p.drawEllipse(pp, 3.4, 3.4)

            if self._dragging_puck:
                fmod = st[self._focused_module]
                tip_txt = f"RATE {format_hz(fmod['rate'])}  |  DEPTH {int(fmod['depth'])}%"
                tip_w, tip_h = 156.0, 18.0
                tx = min(plot.right() - tip_w - 6, max(plot.left() + 6, pp.x() + 16))
                ty = max(plot.top() + 32, pp.y() - 24)
                tip_rect = QRectF(tx, ty, tip_w, tip_h)
                p.setPen(QPen(focus_col, 1.0))
                p.setBrush(QColor(14, 15, 20, 235))
                p.drawRoundedRect(tip_rect, 3.5, 3.5)
                p.setPen(QColor("#ECECF0"))
                p.setFont(QFont(MONO_FONT_FAM, 7))
                p.drawText(tip_rect, Qt.AlignmentFlag.AlignCenter, tip_txt)

        # 7. 右上角视觉图层切换胶囊 (SWEEP / LFO / 2D XY)
        analysis_visible = not (self._processor_legacy and self._focused_module != "phaser")
        self._btn_comb_rect = (QRectF(plot.right() - 134, plot.top() + 8, 42, 18)
                               if analysis_visible else QRectF())
        self._btn_lfo_rect = (QRectF(plot.right() - 88, plot.top() + 8, 36, 18)
                              if analysis_visible else QRectF())
        self._btn_xy_rect = QRectF(plot.right() - 48, plot.top() + 8, 40, 18)

        for rect, label, active in (
            (self._btn_comb_rect, "SWEEP", self._show_comb_curve),
            (self._btn_lfo_rect, "LFO", self._show_stereo_lfo),
            (self._btn_xy_rect, "2D XY", self._show_xy_puck),
        ):
            if rect.isEmpty():
                continue
            p.setPen(QPen(QColor("#3A4252") if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 46, 56, 210) if active else QColor(21, 21, 24, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#6E6E76"))
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 8. 左上角实时调制遥测 HUD
        fmod = st[self._focused_module]
        mod_title = self._focused_module.upper()
        hud_str = f"Rate {format_hz(fmod['rate'])}  •  Depth {int(fmod['depth'])}%"
        if self._processor_controls:
            if analysis_visible:
                response = I18N.tr("mixer.effect_panel.modulation_response", "Estimated sweep response")
                hud_str = f"{response}  •  {hud_str}"
        else:
            hud_str = f"{mod_title}  •  {hud_str}  •  Spread {int(fmod['spread'])}%"
        p.setFont(QFont(MONO_FONT_FAM, 8))
        first_button = self._btn_comb_rect if analysis_visible else self._btn_xy_rect
        available = max(0.0, first_button.left() - plot.left() - 16)
        hud_width = min(p.fontMetrics().horizontalAdvance(hud_str) + 12, available)
        hud_str = p.fontMetrics().elidedText(hud_str, Qt.TextElideMode.ElideRight, int(hud_width) - 10)
        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_width, 19)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(focus_col if enabled else QColor("#8A8A92"))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 8. 通用旋钮参数单元（标签 + 旋钮 + 上下限提示 + 可编辑数值胶囊）
# =====================================================================
class KnobParameterCell(QWidget):
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
        mode: str = "linear",
        pill_width: int = 64,
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
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)

        self.title_lbl = QLabel(label)
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet("color: #9A9AA2; font-size: 10px; font-weight: 600;")
        lay.addWidget(self.title_lbl)

        self.knob = ProRotaryKnob(min_val, max_val, default_val, default_val, accent_color, mode=mode)
        self.knob.valueChanged.connect(self._on_knob_changed)
        lay.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(2, 0, 2, 0)
        hints_row.setSpacing(2)
        self._hints: list[QLabel] = []
        for h_text, align in (
            (min_hint, Qt.AlignmentFlag.AlignLeft),
            (mid_hint, Qt.AlignmentFlag.AlignCenter),
            (max_hint, Qt.AlignmentFlag.AlignRight),
        ):
            hl = QLabel(h_text)
            hl.setAlignment(align)
            hl.setStyleSheet("color: #787882; font-size: 9px;")
            self._hints.append(hl)
            hints_row.addWidget(hl, 1)
        lay.addLayout(hints_row)

        self.pill = EditableValuePill(formatter(default_val), accent_color, width=pill_width)
        self.pill.valueEdited.connect(self._on_pill_edited)
        lay.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignCenter)

    def set_control_range(self, minimum: float, maximum: float, left: str, right: str) -> None:
        self._min = minimum
        self._max = maximum
        self.knob._min = minimum
        self.knob._max = maximum
        self._hints[0].setText(left)
        self._hints[2].setText(right)
        self.setValue(self._val)

    def setValue(self, val: float):
        self._updating = True
        self._val = max(self._min, min(self._max, val))
        self.knob.setValue(self._val)
        self.pill.setText(self._formatter(self._val))
        self._updating = False

    def setModuleActive(self, active: bool):
        self.title_lbl.setStyleSheet(f"color: {'#9A9AA2' if active else '#6E6E76'}; font-size: 10px; font-weight: 600;")
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


# =====================================================================
# 9. 模块卡片 1：Chorus Card（吉他/合成器加厚 & 人声 Doubles · Arturia / Dimension 灵感）
# =====================================================================
class _ModulationVoicesCard(QFrame):
    def _add_voices(self, accent: str) -> None:
        voices = KnobParameterCell(
            I18N.tr("audio.fx.voices", "Voices"), 1.0, 4.0, 2.0, accent, "1", "", "4",
            lambda v: str(round(v)), parse_numeric,
        )
        voices.valueChanged.connect(lambda v: self._set_field("voices", round(v)))
        self._voices_row.addWidget(voices, 1)
        self.c_voices = voices

    def _sync_processor_voices(self, enabled: bool) -> None:
        voices = getattr(self, "c_voices", None)
        if voices is not None:
            voices.setValue(self._data.get("voices", 2))
            voices.setModuleActive(enabled)


class ChorusModuleCard(_ModulationVoicesCard):
    changed = pyqtSignal(dict)
    selected = pyqtSignal(str)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["chorus"]
        self._updating = False
        self._selected = True

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 7, 10, 6)
        h_lay.setSpacing(6)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Chorus")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # Analog (JUN-6) / Dim-D / Dual (Vocal Double) 三态引擎切换
        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.voicing_btns: dict[str, QPushButton] = {}
        self.voicing_selector = seg_wrap
        for v_name in ("Analog", "Dim-D", "Dual"):
            btn = QPushButton(v_name)
            btn.setCheckable(True)
            btn.setFixedSize(44, 19)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, vn=v_name: self._on_voicing(vn))
            seg_lay.addWidget(btn)
            self.voicing_btns[v_name] = btn

        h_lay.addWidget(seg_wrap)
        root.addWidget(header)

        # Body (2x3 旋钮矩阵)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_rate = KnobParameterCell(
            "Rate", 0.05, 10.0, 0.55, self._accent,
            "0.05", "Hz", "10.0",
            format_hz, parse_numeric, mode="log"
        )
        self.c_depth = KnobParameterCell(
            "Depth", 0.0, 100.0, 62.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_spread = KnobParameterCell(
            "Stereo Spread", 0.0, 200.0, 145.0, self._accent,
            "Mono", "100%", "Wide",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_rate, self.c_depth, self.c_spread):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_delay = KnobParameterCell(
            "Double Delay", 2.0, 35.0, 9.5, self._accent,
            "Tight", "ms", "Double",
            format_ms, parse_numeric, mode="power"
        )
        self.c_tone = KnobParameterCell(
            "BBD Tone", -50.0, 50.0, 8.0, self._accent,
            "Warm", "", "Air",
            format_signed_pct, parse_numeric, mode="bipolar"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 58.0, self._accent,
            "Dry", "%", "Wet",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_delay, self.c_tone, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_rate.valueChanged.connect(lambda v: self._set_field("rate", round(v, 2)))
        self.c_depth.valueChanged.connect(lambda v: self._set_field("depth", round(v, 0)))
        self.c_spread.valueChanged.connect(lambda v: self._set_field("spread", round(v, 0)))
        self.c_delay.valueChanged.connect(lambda v: self._set_field("delay_ms", round(v, 1)))
        self.c_tone.valueChanged.connect(lambda v: self._set_field("tone", round(v, 0)))
        self._voices_row = row1
        self._chorus_mix_row = row2
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def mousePressEvent(self, event):
        self.selected.emit("chorus")
        super().mousePressEvent(event)

    def setSelected(self, selected: bool):
        if self._selected != selected:
            self._selected = selected
            self._apply_card_border()

    def _apply_card_border(self):
        en = bool(self._data["enabled"])
        top_border = f"2px solid {self._accent}" if (self._selected and en) else "1px solid #141416"
        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: {top_border};
                border-radius: 0px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        voicing = self._data["voicing"]

        self._apply_card_border()
        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#6E6E76'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        active_seg = f"""
            QPushButton {{
                background-color: #162E24;
                color: {self._accent if en else '#9A9AA2'};
                border: 1px solid {self._accent if en else '#3A3A40'};
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 700;
            }}
        """
        inactive_seg = """
            QPushButton {
                background-color: transparent;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
        """
        for vn, btn in self.voicing_btns.items():
            is_cur = (vn == voicing)
            btn.setChecked(is_cur)
            btn.setStyleSheet(active_seg if is_cur else inactive_seg)

        self.c_rate.setValue(self._data["rate"])
        self.c_depth.setValue(self._data["depth"])
        self.c_spread.setValue(self._data["spread"])
        self.c_delay.setValue(self._data["delay_ms"])
        self.c_tone.setValue(self._data["tone"])
        self._sync_processor_voices(en)
        feedback = getattr(self, "c_processor_feedback", None)
        if feedback is not None:
            feedback.setValue(self._data.get("feedback", 0.0))
            feedback.setModuleActive(en)
        self.c_mix.setValue(self._data["mix"])

        for cell in (self.c_rate, self.c_depth, self.c_spread, self.c_delay, self.c_tone, self.c_mix):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.selected.emit("chorus")
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_voicing(self, voicing: str):
        if self._data["voicing"] != voicing:
            self._data["voicing"] = voicing
            self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.selected.emit("chorus")
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 10. 模块卡片 2：Flanger Card（质感运动 / MXR M117R & NI Flair 灵感）
# =====================================================================
class FlangerModuleCard(_ModulationVoicesCard):
    changed = pyqtSignal(dict)
    selected = pyqtSignal(str)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["flanger"]
        self._updating = False
        self._selected = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 7, 10, 6)
        h_lay.setSpacing(6)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Flanger")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # Classic (MXR) / Thru-0 (Zero-Crossing Tape) 双模切换
        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.btn_classic = QPushButton("Classic")
        self.btn_thru0 = QPushButton("Thru-0")
        self.mode_selector = seg_wrap
        for b in (self.btn_classic, self.btn_thru0):
            b.setCheckable(True)
            b.setFixedSize(48, 19)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_lay.addWidget(b)

        self.btn_classic.clicked.connect(lambda: self._on_mode("Classic"))
        self.btn_thru0.clicked.connect(lambda: self._on_mode("Thru-0"))
        h_lay.addWidget(seg_wrap)
        root.addWidget(header)

        # Body (2x3 旋钮矩阵)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_rate = KnobParameterCell(
            "Rate", 0.05, 10.0, 0.28, self._accent,
            "0.05", "Hz", "10.0",
            format_hz, parse_numeric, mode="log"
        )
        self.c_depth = KnobParameterCell(
            "Depth", 0.0, 100.0, 78.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_feedback = KnobParameterCell(
            "Feedback", -100.0, 100.0, 68.0, self._accent,
            "-100%", "Regen", "+100%",
            format_signed_pct, parse_numeric, mode="bipolar"
        )
        for c in (self.c_rate, self.c_depth, self.c_feedback):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_manual = KnobParameterCell(
            "Manual", 0.2, 15.0, 2.4, self._accent,
            "0.2", "ms", "15.0",
            format_ms, parse_numeric, mode="log"
        )
        self.c_spread = KnobParameterCell(
            "Stereo Spread", 0.0, 200.0, 135.0, self._accent,
            "Mono", "100%", "Wide",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 50.0, self._accent,
            "Dry", "%", "Wet",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_manual, self.c_spread, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_rate.valueChanged.connect(lambda v: self._set_field("rate", round(v, 2)))
        self.c_depth.valueChanged.connect(lambda v: self._set_field("depth", round(v, 0)))
        self.c_feedback.valueChanged.connect(lambda v: self._set_field("feedback", round(v, 0)))
        self.c_manual.valueChanged.connect(lambda v: self._set_field("manual_ms", round(v, 1)))
        self._voices_row = row2
        self.c_spread.valueChanged.connect(lambda v: self._set_field("spread", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def mousePressEvent(self, event):
        self.selected.emit("flanger")
        super().mousePressEvent(event)

    def setSelected(self, selected: bool):
        if self._selected != selected:
            self._selected = selected
            self._apply_card_border()

    def _apply_card_border(self):
        en = bool(self._data["enabled"])
        top_border = f"2px solid {self._accent}" if (self._selected and en) else "1px solid #141416"
        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: {top_border};
                border-radius: 0px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        is_classic = (self._data["mode"] == "Classic")

        self._apply_card_border()
        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#6E6E76'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        self.btn_classic.setChecked(is_classic)
        self.btn_thru0.setChecked(not is_classic)
        active_seg = f"""
            QPushButton {{
                background-color: #2A2318;
                color: {self._accent if en else '#9A9AA2'};
                border: 1px solid {self._accent if en else '#3A3A40'};
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 700;
            }}
        """
        inactive_seg = """
            QPushButton {
                background-color: transparent;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
        """
        self.btn_classic.setStyleSheet(active_seg if is_classic else inactive_seg)
        self.btn_thru0.setStyleSheet(active_seg if not is_classic else inactive_seg)

        self.c_rate.setValue(self._data["rate"])
        self.c_depth.setValue(self._data["depth"])
        self.c_feedback.setValue(self._data["feedback"])
        self.c_manual.setValue(self._data["manual_ms"])
        self._sync_processor_voices(en)
        self.c_spread.setValue(self._data["spread"])
        self.c_mix.setValue(self._data["mix"])

        for cell in (self.c_rate, self.c_depth, self.c_feedback, self.c_manual, self.c_spread, self.c_mix):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.selected.emit("flanger")
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_mode(self, mode: str):
        if self._data["mode"] != mode:
            self._data["mode"] = mode
            self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.selected.emit("flanger")
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 11. 模块卡片 3：Phaser Card（2 / 4 / 6 / 8 级全通相位运动 · NI Phasis 灵感）
# =====================================================================
class PhaserModuleCard(QFrame):
    changed = pyqtSignal(dict)
    selected = pyqtSignal(str)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["phaser"]
        self._updating = False
        self._selected = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header: 电源 + 标题 + 相位器级数分段选择器 (2 / 4 / 6 / 8)
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 7, 10, 6)
        h_lay.setSpacing(6)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Phaser")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        self.stereo_button = QPushButton(
            I18N.tr("mixer.effect_panel.phaser_stereo", "Stereo"), header)
        self.stereo_button.setObjectName("ToolBtn")
        self.stereo_button.setCheckable(True)
        self.stereo_button.setToolTip(I18N.tr(
            "mixer.effect_panel.phaser_stereo_hint", "Apply the phaser separately to each channel"))
        self.stereo_button.clicked.connect(self._set_stereo)
        self.stereo_button.hide()
        h_lay.addWidget(self.stereo_button)

        stg_lbl = QLabel("STG")
        stg_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700;")
        h_lay.addWidget(stg_lbl)

        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.stage_btns: dict[int, QPushButton] = {}
        for stg in (2, 4, 6, 8):
            btn = QPushButton(str(stg))
            btn.setCheckable(True)
            btn.setFixedSize(22, 19)
            btn.setToolTip(I18N.tr(
                "mixer.effect_panel.phaser_stages", "{count} all-pass stages").format(count=stg))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, s=stg: self._on_stage(s))
            seg_lay.addWidget(btn)
            self.stage_btns[stg] = btn

        h_lay.addWidget(seg_wrap)
        self._stage_selector = seg_lay
        root.addWidget(header)

        # Body (2x3 旋钮矩阵)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_rate = KnobParameterCell(
            "Rate", 0.05, 10.0, 0.42, self._accent,
            "0.05", "Hz", "10.0",
            format_hz, parse_numeric, mode="log"
        )
        self.c_depth = KnobParameterCell(
            "Depth", 0.0, 100.0, 72.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_feedback = KnobParameterCell(
            "Feedback", -100.0, 100.0, 48.0, self._accent,
            "-100%", "Reso", "+100%",
            format_signed_pct, parse_numeric, mode="bipolar"
        )
        for c in (self.c_rate, self.c_depth, self.c_feedback):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_center = KnobParameterCell(
            "Center Freq", 100.0, 4000.0, 880.0, self._accent,
            "100", "Hz", "4.0k",
            format_hz, parse_numeric, mode="log"
        )
        self.c_spread = KnobParameterCell(
            "Stereo Spread", 0.0, 200.0, 140.0, self._accent,
            "Mono", "100%", "Wide",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 55.0, self._accent,
            "Dry", "%", "Wet",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_center, self.c_spread, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_rate.valueChanged.connect(lambda v: self._set_field("rate", round(v, 2)))
        self.c_depth.valueChanged.connect(lambda v: self._set_field("depth", round(v, 0)))
        self.c_feedback.valueChanged.connect(lambda v: self._set_field("feedback", round(v, 0)))
        self.c_center.valueChanged.connect(lambda v: self._set_field("center_hz", round(v, 0)))
        self.c_spread.valueChanged.connect(lambda v: self._set_field("spread", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def mousePressEvent(self, event):
        self.selected.emit("phaser")
        super().mousePressEvent(event)

    def setSelected(self, selected: bool):
        if self._selected != selected:
            self._selected = selected
            self._apply_card_border()

    def _apply_card_border(self):
        en = bool(self._data["enabled"])
        top_border = f"2px solid {self._accent}" if (self._selected and en) else "1px solid #141416"
        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: {top_border};
                border-radius: 0px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        cur_stg = int(self._data["stages"])

        self._apply_card_border()
        self.power_sw.setChecked(en)
        self.stereo_button.setChecked(bool(self._data.get("stereo_processing", False)))
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#6E6E76'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        active_seg = f"""
            QPushButton {{
                background-color: #261E34;
                color: {self._accent if en else '#9A9AA2'};
                border: 1px solid {self._accent if en else '#3A3A40'};
                border-radius: 3px;
                font-family: {MONO_FONT_CSS};
                font-size: 9.5px;
                font-weight: 700;
            }}
        """
        inactive_seg = f"""
            QPushButton {{
                background-color: transparent;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-family: {MONO_FONT_CSS};
                font-size: 9.5px;
                font-weight: 600;
            }}
            QPushButton:hover {{ color: #FFFFFF; }}
        """
        for stg, btn in self.stage_btns.items():
            is_cur = (stg == cur_stg)
            btn.setChecked(is_cur)
            btn.setStyleSheet(active_seg if is_cur else inactive_seg)

        self.c_rate.setValue(self._data["rate"])
        self.c_depth.setValue(self._data["depth"])
        self.c_feedback.setValue(self._data["feedback"])
        self.c_center.setValue(self._data["center_hz"])
        self.c_spread.setValue(self._data["spread"])
        self.c_mix.setValue(self._data["mix"])

        for cell in (self.c_rate, self.c_depth, self.c_feedback, self.c_center, self.c_spread, self.c_mix):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.selected.emit("phaser")
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _set_stereo(self, enabled: bool) -> None:
        self._data["stereo_processing"] = enabled
        self._emit()

    def _on_stage(self, stages: int):
        if self._data["stages"] != stages:
            self._data["stages"] = stages
            self._emit()

    def _set_field(self, key: str, val: float | int):
        self._data[key] = val
        if not self._updating:
            self.selected.emit("phaser")
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 12. 主面板：调制三件套 (Studio Modulation Trio: Chorus / Flanger / Phaser)
# =====================================================================
class StudioModulationTrioWidget(QWidget):
    """
    调制三件套主界面（一个 UI 三模式：Chorus / Flanger / Phaser / Rack 叠加），
    提供完整信号与 API 供后端音频 DSP 引擎直接订阅：
      - bypassChanged(bool)
      - modeChanged(str)               # "Chorus" | "Flanger" | "Phaser" | "Rack"
      - routingChanged(str)            # "Serial" | "Parallel"
      - chorusChanged(dict)
      - flangerChanged(dict)
      - phaserChanged(dict)
      - outputGainChanged(float)
      - auditionToggled(bool)
      - stateChanged(dict)
    """
    bypassChanged = pyqtSignal(bool)
    modeChanged = pyqtSignal(str)
    processorModeChanged = pyqtSignal(str)
    routingChanged = pyqtSignal(str)
    chorusChanged = pyqtSignal(dict)
    flangerChanged = pyqtSignal(dict)
    phaserChanged = pyqtSignal(dict)
    outputGainChanged = pyqtSignal(float)
    auditionToggled = pyqtSignal(bool)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Synth / Vocal Bus", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Modulation Trio — {track_name}")
        self.resize(1040, 700)
        self.setMinimumSize(940, 620)

        self._processor_effect = ""
        self._processor_mode = "chorus"
        self._active_bank = "A"
        self._focused_module = "chorus"
        self._banks = {
            "A": default_mod_demo(),
            "B": default_mod_neutral(),
        }

        self._presets = {
            # ================= 1. 吉他 / 合成器加厚 (Guitar & Synth Thickening) =================
            "JUN-6 Lush Chorus (合成器经典加厚)": copy.deepcopy(self._banks["A"]),
            "Dimension-D Stereo (吉他宽体加厚)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": 0.0,
                "chorus": {"enabled": True, "voicing": "Dim-D", "rate": 0.42, "depth": 56.0, "delay_ms": 8.0, "spread": 185.0, "tone": 4.0, "mix": 55.0},
                "flanger": {"enabled": False, "mode": "Classic", "rate": 0.30, "depth": 50.0, "feedback": 20.0, "manual_ms": 2.5, "spread": 120.0, "mix": 40.0},
                "phaser": {"enabled": False, "stages": 4, "rate": 0.40, "depth": 50.0, "feedback": 25.0, "center_hz": 900.0, "spread": 120.0, "mix": 40.0},
            },
            "Analog Pad Ensemble (模拟弦乐群厚重)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": 0.0,
                "chorus": {"enabled": True, "voicing": "Analog", "rate": 0.68, "depth": 74.0, "delay_ms": 12.5, "spread": 170.0, "tone": -12.0, "mix": 65.0},
                "flanger": {"enabled": False, "mode": "Classic", "rate": 0.25, "depth": 40.0, "feedback": 15.0, "manual_ms": 3.0, "spread": 130.0, "mix": 30.0},
                "phaser": {"enabled": True, "stages": 4, "rate": 0.18, "depth": 42.0, "feedback": 22.0, "center_hz": 680.0, "spread": 150.0, "mix": 28.0},
            },

            # ================= 2. 人声 Doubles (Vocal Doubling) =================
            "Vocal Tight Double (自然人声贴耳加厚)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": 0.5,
                "chorus": {"enabled": True, "voicing": "Dual", "rate": 0.34, "depth": 38.0, "delay_ms": 16.5, "spread": 135.0, "tone": 10.0, "mix": 42.0},
                "flanger": {"enabled": False, "mode": "Classic", "rate": 0.30, "depth": 40.0, "feedback": 10.0, "manual_ms": 2.0, "spread": 110.0, "mix": 30.0},
                "phaser": {"enabled": False, "stages": 4, "rate": 0.35, "depth": 40.0, "feedback": 15.0, "center_hz": 1200.0, "spread": 110.0, "mix": 30.0},
            },
            "Pop Wide Vocal Chorus (流行宽声场人声)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": 0.5,
                "chorus": {"enabled": True, "voicing": "Dual", "rate": 0.48, "depth": 52.0, "delay_ms": 22.0, "spread": 180.0, "tone": 14.0, "mix": 54.0},
                "flanger": {"enabled": False, "mode": "Classic", "rate": 0.30, "depth": 40.0, "feedback": 10.0, "manual_ms": 2.0, "spread": 110.0, "mix": 30.0},
                "phaser": {"enabled": False, "stages": 4, "rate": 0.35, "depth": 40.0, "feedback": 15.0, "center_hz": 1200.0, "spread": 110.0, "mix": 30.0},
            },

            # ================= 3. 质感运动 (Textural Motion / MXR / NI Mod Pack) =================
            "MXR Jet Flanger (经典喷气机扫频)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": 0.0,
                "chorus": {"enabled": False, "voicing": "Analog", "rate": 0.50, "depth": 50.0, "delay_ms": 8.0, "spread": 120.0, "tone": 0.0, "mix": 50.0},
                "flanger": {"enabled": True, "mode": "Classic", "rate": 0.24, "depth": 85.0, "feedback": 78.0, "manual_ms": 1.8, "spread": 155.0, "mix": 55.0},
                "phaser": {"enabled": False, "stages": 4, "rate": 0.40, "depth": 60.0, "feedback": 40.0, "center_hz": 850.0, "spread": 130.0, "mix": 50.0},
            },
            "Thru-0 Metallic Comb (过零金属梳状质感)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": 0.0,
                "chorus": {"enabled": False, "voicing": "Analog", "rate": 0.50, "depth": 50.0, "delay_ms": 8.0, "spread": 120.0, "tone": 0.0, "mix": 50.0},
                "flanger": {"enabled": True, "mode": "Thru-0", "rate": 0.62, "depth": 72.0, "feedback": -74.0, "manual_ms": 1.1, "spread": 170.0, "mix": 62.0},
                "phaser": {"enabled": False, "stages": 4, "rate": 0.40, "depth": 60.0, "feedback": 40.0, "center_hz": 850.0, "spread": 130.0, "mix": 50.0},
            },
            "2-Stage Subtle Phaser (二级轻盈律动)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": 0.0,
                "chorus": {"enabled": False, "voicing": "Analog", "rate": 0.50, "depth": 50.0, "delay_ms": 8.0, "spread": 120.0, "tone": 0.0, "mix": 50.0},
                "flanger": {"enabled": False, "mode": "Classic", "rate": 0.30, "depth": 50.0, "feedback": 30.0, "manual_ms": 2.4, "spread": 120.0, "mix": 50.0},
                "phaser": {"enabled": True, "stages": 2, "rate": 0.85, "depth": 64.0, "feedback": 28.0, "center_hz": 1100.0, "spread": 125.0, "mix": 50.0},
            },
            "4-Stage Classic Phaser (经典四级移相)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": 0.0,
                "chorus": {"enabled": False, "voicing": "Analog", "rate": 0.50, "depth": 50.0, "delay_ms": 8.0, "spread": 120.0, "tone": 0.0, "mix": 50.0},
                "flanger": {"enabled": False, "mode": "Classic", "rate": 0.30, "depth": 50.0, "feedback": 30.0, "manual_ms": 2.4, "spread": 120.0, "mix": 50.0},
                "phaser": {"enabled": True, "stages": 4, "rate": 0.45, "depth": 76.0, "feedback": 54.0, "center_hz": 860.0, "spread": 145.0, "mix": 58.0},
            },
            "8-Stage Liquid Sweep (八级深邃液态律动)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": 0.0,
                "chorus": {"enabled": False, "voicing": "Analog", "rate": 0.50, "depth": 50.0, "delay_ms": 8.0, "spread": 120.0, "tone": 0.0, "mix": 50.0},
                "flanger": {"enabled": False, "mode": "Classic", "rate": 0.30, "depth": 50.0, "feedback": 30.0, "manual_ms": 2.4, "spread": 120.0, "mix": 50.0},
                "phaser": {"enabled": True, "stages": 8, "rate": 0.21, "depth": 88.0, "feedback": 68.0, "center_hz": 740.0, "spread": 185.0, "mix": 64.0},
            },
            "Mod Trio Nebula Stack (三件套串联质感运动)": {
                "enabled": True,
                "routing": "Serial",
                "output_gain": -0.5,
                "chorus": {"enabled": True, "voicing": "Dim-D", "rate": 0.45, "depth": 48.0, "delay_ms": 10.0, "spread": 160.0, "tone": 6.0, "mix": 42.0},
                "flanger": {"enabled": True, "mode": "Classic", "rate": 0.19, "depth": 55.0, "feedback": 42.0, "manual_ms": 3.2, "spread": 140.0, "mix": 35.0},
                "phaser": {"enabled": True, "stages": 6, "rate": 0.31, "depth": 68.0, "feedback": 45.0, "center_hz": 920.0, "spread": 175.0, "mix": 45.0},
            },
            "Clean Neutral (初始直通)": copy.deepcopy(self._banks["B"]),
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 演示实时调制波形定时器（接入真实后端 update_analysis / update_meters 后自动停止）
        self._anim_t = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_simulation)
        self._demo_timer.start(38)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ProModulationVisualizerWidget, ProVerticalMeter, MasterOutputStrip {{
                background-color: transparent;
            }}
            QComboBox {{
                background-color: #18181B;
                color: #DCDCE2;
                border: 1px solid #121214;
                border-radius: 3px;
                padding: 0px 5px;
                font-size: 10px;
                font-weight: 600;
                min-height: 19px;
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
        # 1. 顶部主控栏（电源 + 预设 + A/B 对比 + 三模式快捷切换 + 路由 + Reset）
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

        title_lbl = QLabel(f"MODULATION TRIO  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(8)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存或覆盖调制预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        del_preset_btn = QPushButton("−")
        del_preset_btn.setObjectName("ToolBtn")
        del_preset_btn.setFixedSize(22, 22)
        del_preset_btn.setToolTip("删除当前自定义预设")
        del_preset_btn.clicked.connect(self._delete_current_preset)
        tb_lay.addWidget(del_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(210)
        self._builtin_preset_names = set(self._presets.keys())
        self.preset_combo.addItems(list(self._presets.keys()))
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        tb_lay.addWidget(self.preset_combo)

        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setFixedSize(20, 22)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb_lay.addWidget(prev_btn)

        next_btn = QPushButton("›")
        next_btn.setObjectName("ToolBtn")
        next_btn.setFixedSize(20, 22)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb_lay.addWidget(next_btn)

        tb_lay.addSpacing(8)

        # A / B 对比槽
        ab_lay = QHBoxLayout()
        ab_lay.setSpacing(2)
        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(24, 22)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton("→")
        self.btn_copy_ab.setFixedSize(24, 22)
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

        tb_lay.addSpacing(8)

        # 一个 UI 三模式快捷切换栏 (CHORUS | FLANGER | PHASER | RACK)
        mode_lay = QHBoxLayout()
        mode_lay.setSpacing(2)
        self.mode_btns: dict[str, QPushButton] = {}
        mode_specs = [
            ("Chorus", "CHR", "单开 Chorus 模式（吉他/合成器加厚、人声 Doubles）"),
            ("Flanger", "FLG", "单开 Flanger 模式（MXR 喷气扫频、金属梳状运动）"),
            ("Phaser", "PHS", "单开 Phaser 模式（2/4/6/8 级全通相位运动）"),
            ("Rack", "RACK", "三件套多模块叠加机架模式"),
        ]
        for m_key, m_lbl, m_tip in mode_specs:
            btn = QPushButton(m_lbl)
            btn.setFixedSize(38 if m_key == "Rack" else 34, 22)
            btn.setToolTip(m_tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, mk=m_key: self._select_quick_mode(mk))
            mode_lay.addWidget(btn)
            self.mode_btns[m_key] = btn
        tb_lay.addLayout(mode_lay)

        tb_lay.addStretch()

        # 串联 / 并联拓扑切换按钮
        self.routing_btn = QPushButton("Serial")
        self.routing_btn.setObjectName("ToolBtn")
        self.routing_btn.setFixedSize(54, 22)
        self.routing_btn.setToolTip("切换多模块信号链拓扑：Serial (串联) / Parallel (并联)")
        self.routing_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.routing_btn.clicked.connect(self._toggle_routing)
        tb_lay.addWidget(self.routing_btn)

        # 干声试听按钮
        self.audition_btn = QPushButton("▶ Audition")
        self.audition_btn.setCheckable(True)
        self.audition_btn.setFixedSize(74, 22)
        self.audition_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.audition_btn.setToolTip("触发内置测试干声循环试听")
        self.audition_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: #3C3C44; color: #FFFFFF; }
            QPushButton:checked {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
                font-weight: 700;
            }
        """)
        self.audition_btn.toggled.connect(self.auditionToggled.emit)
        tb_lay.addWidget(self.audition_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(46, 22)
        reset_btn.clicked.connect(self.reset_modulation)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：Input | 梳状扫频与L/R相位画布 | 4通道状态表 | GAIN推子 | Output
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
        in_col = self._build_meter_column("IN")
        self.meter_input = ProVerticalMeter(mode="level", show_ticks=True)
        in_col.addWidget(self.meter_input, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(in_col)
        up_lay.addWidget(_vsep())

        # 2.2 核心调制可视化与 2D Rate×Depth 坐标画布
        self.mod_visualizer = ProModulationVisualizerWidget()
        self.mod_visualizer.rateDepthDragged.connect(self._on_canvas_rate_depth_dragged)
        self.mod_visualizer.spreadScrolled.connect(self._on_canvas_spread_scrolled)
        self.mod_visualizer.rateDepthResetRequested.connect(self._on_canvas_rate_depth_reset)
        up_lay.addWidget(self.mod_visualizer, 5)
        up_lay.addWidget(_vsep())

        # 2.3 四通道调制活跃度与立体声宽度指示条 (CHR · FLG · PHS · WID)
        stat_col = self._build_meter_column("MOD ENGINE")
        stat_row = QHBoxLayout()
        stat_row.setSpacing(4)
        self.meter_chr = ProVerticalMeter(title="CHR", mode="activity", show_ticks=False, accent_color=COLORS["chorus"])
        self.meter_flg = ProVerticalMeter(title="FLG", mode="activity", show_ticks=False, accent_color=COLORS["flanger"])
        self.meter_phs = ProVerticalMeter(title="PHS", mode="activity", show_ticks=False, accent_color=COLORS["phaser"])
        self.meter_wid = ProVerticalMeter(title="WID", mode="activity", show_ticks=False, accent_color=COLORS["cyan"])
        for m in (self.meter_chr, self.meter_flg, self.meter_phs, self.meter_wid):
            stat_row.addWidget(m)
        stat_col.addLayout(stat_row, 1)
        stat_section = QWidget()
        stat_section.setLayout(stat_col)
        stat_divider = _vsep()
        up_lay.addWidget(stat_section)
        up_lay.addWidget(stat_divider)

        # 2.4 Output 推子 + 电平表
        fader_col = self._build_meter_column("GAIN")
        self.out_fader = MasterOutputStrip()
        self.out_fader.gainChanged.connect(self._on_output_gain_changed)
        fader_col.addWidget(self.out_fader, 1, Qt.AlignmentFlag.AlignHCenter)
        fader_section = QWidget()
        fader_section.setLayout(fader_col)
        fader_divider = _vsep()
        up_lay.addWidget(fader_section)
        up_lay.addWidget(fader_divider)
        self._design_meter_sections = (stat_section, stat_divider, fader_section, fader_divider)

        out_col = self._build_meter_column("OUT")
        self.meter_output = ProVerticalMeter(mode="level", show_ticks=True)
        out_col.addWidget(self.meter_output, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部三模块卡片矩阵 (Chorus | Flanger | Phaser)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        st = self._current_state()
        self.chorus_card = ChorusModuleCard(st["chorus"])
        self.chorus_card.changed.connect(self._on_chorus_changed)
        self.chorus_card.selected.connect(self._set_focused_module)

        self.flanger_card = FlangerModuleCard(st["flanger"])
        self.flanger_card.changed.connect(self._on_flanger_changed)
        self.flanger_card.selected.connect(self._set_focused_module)

        self.phaser_card = PhaserModuleCard(st["phaser"])
        self.phaser_card.changed.connect(self._on_phaser_changed)
        self.phaser_card.selected.connect(self._set_focused_module)

        cards_row.addWidget(self.chorus_card, 1)
        cards_row.addWidget(self.flanger_card, 1)
        cards_row.addWidget(self.phaser_card, 1)

        main_lay.addLayout(cards_row, 4)

    def use_processor_controls(self, effect_type: str) -> None:
        self._processor_effect = effect_type
        self.mod_visualizer._processor_controls = True
        self.out_fader.setEnabled(False)
        for section in self._design_meter_sections:
            section.hide()
        self.routing_btn.hide()
        self.audition_btn.hide()
        self.mode_btns["Rack"].hide()
        supported_modes = ("Phaser",) if effect_type == "phaser" else ("Chorus", "Flanger")
        for mode in ("Chorus", "Flanger", "Phaser"):
            card = getattr(self, f"{mode.lower()}_card")
            self.mode_btns[mode].setVisible(mode in supported_modes)
            card.power_sw.hide()
            card.c_spread.hide()
            card.c_rate.set_control_range(0.01, 20.0, "0.01", "20.0")
            card.c_depth.setToolTip("Controls modulation sweep depth")
        for card in (self.flanger_card, self.phaser_card):
            card.c_feedback.set_control_range(-95.0, 95.0, "-95%", "95%")
        self.chorus_card.c_delay.set_control_range(0.5, 40.0, "0.5", "40.0")
        self.flanger_card.c_manual.set_control_range(0.5, 40.0, "0.5", "40.0")
        if effect_type == "phaser":
            self._processor_mode = "phaser"
            self.phaser_card.c_depth.setToolTip("Sweep width from 0.5 to 5 octaves")
            self._configure_phaser_stages()
            card = self.phaser_card
            card.c_center.set_control_range(10.0, 20000.0, "10", "20k")
            base = self._phaser_factory_state()
            self._presets = {}
            for title, updates in (
                (I18N.tr("mixer.effect_panel.phaser_preset_gentle", "Gentle Sweep"),
                 {"rate": 0.3, "depth": 28.0, "feedback": 15.0, "mix": 36.0}),
                (I18N.tr("mixer.effect_panel.phaser_preset_feedback", "Resonant Sweep"),
                 {"rate": 0.6, "feedback": 62.0, "mix": 56.0}),
                (I18N.tr("mixer.effect_panel.phaser_preset_deep", "Deep 12-Stage"),
                 {"rate": 0.25, "stages": 12, "center_hz": 440.0, "depth": 66.0}),
            ):
                preset = copy.deepcopy(base)
                preset["phaser"].update(updates)
                self._presets[title] = preset
            with QSignalBlocker(self.preset_combo):
                self.preset_combo.clear()
                self.preset_combo.addItems(self._presets.keys())
            self._compact_card(card, (card.c_rate, card.c_depth, card.c_center,
                                      card.c_feedback, card.c_mix))
        else:
            self.chorus_card.voicing_selector.hide()
            self.flanger_card.mode_selector.hide()
            for card in (self.chorus_card, self.flanger_card):
                depth = card.c_depth
                depth._formatter = lambda units: f"{units * 0.1:.1f} ms"
                depth._parser = lambda text, fallback: 10.0 * parse_numeric(text, fallback * 0.1)
                depth._hints[1].setText("ms")
                depth.set_control_range(0.0, 500.0, "0", "50")
                card.c_rate._formatter = lambda value: f"{value:.2f} Hz"
            self.chorus_card.c_delay._formatter = lambda value: f"{value:.1f} ms"
            self.flanger_card.c_manual._formatter = self.chorus_card.c_delay._formatter
            self.chorus_card.c_delay.set_control_range(0.5, 100.0, "0.5", "100")
            self.flanger_card.c_manual.set_control_range(0.5, 100.0, "0.5", "100")
            self.chorus_card.c_tone.hide()
            self.chorus_card._add_voices(COLORS["chorus"])
            self.flanger_card._add_voices(COLORS["flanger"])
            feedback = KnobParameterCell(
                I18N.tr("audio.fx.feedback", "Feedback"), -95.0, 95.0, 0.0, COLORS["chorus"],
                "-95", "%", "95", format_pct, parse_numeric,
            )
            feedback.valueChanged.connect(self._on_chorus_feedback)
            self.chorus_card._chorus_mix_row.addWidget(feedback, 1)
            self.chorus_card.c_processor_feedback = feedback
            chorus = self.chorus_card
            self._compact_card(chorus, (chorus.c_rate, chorus.c_depth, chorus.c_delay,
                                        chorus.c_voices, feedback, chorus.c_mix))
            flanger = self.flanger_card
            self._compact_card(flanger, (flanger.c_rate, flanger.c_depth, flanger.c_manual,
                                         flanger.c_voices, flanger.c_feedback, flanger.c_mix))
            self._install_chorus_flanger_presets()
        self._refresh_all_views()

    def _install_chorus_flanger_presets(self) -> None:
        specifications = {
            I18N.tr("audio.fx.chorus_vocal_double", "Vocal Double"): (
                "chorus", {"rate": 0.34, "depth": 24.0, "delay_ms": 18.0, "mix": 38.0, "voices": 2}),
            I18N.tr("audio.fx.chorus_ensemble", "Ensemble Chorus"): (
                "chorus", {"rate": 0.72, "depth": 46.0, "delay_ms": 26.0, "mix": 58.0, "voices": 4}),
            I18N.tr("audio.fx.flanger_jet", "Jet Flanger"): (
                "flanger", {"rate": 0.24, "depth": 18.0, "manual_ms": 2.8, "feedback": 76.0,
                            "mix": 55.0, "voices": 1}),
            I18N.tr("audio.fx.flanger_inverted", "Inverted Flanger"): (
                "flanger", {"rate": 0.62, "depth": 19.0, "manual_ms": 3.4, "feedback": -68.0,
                            "mix": 54.0, "voices": 1}),
        }
        self._presets = {}
        for name, (mode, values) in specifications.items():
            state = default_mod_neutral()
            state[mode].update(values, enabled=True)
            self._presets[name] = state
        self._builtin_preset_names = set(self._presets)
        with QSignalBlocker(self.preset_combo):
            self.preset_combo.clear()
            self.preset_combo.addItems(self._presets.keys())

    def _compact_card(
        self, card: ChorusModuleCard | FlangerModuleCard | PhaserModuleCard,
        controls: tuple[KnobParameterCell, ...],
    ) -> None:
        body = card.c_rate.parentWidget().layout()
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        for control in controls:
            control.setMaximumWidth(98)
            row.addWidget(control, 1)
        row.addStretch()
        previous_rows = [body.itemAt(i).layout() for i in range(body.count())]
        for previous in previous_rows:
            body.removeItem(previous)
        body.addLayout(row)

    def _configure_phaser_stages(self) -> None:
        card = self.phaser_card
        for stage in (1, 3, 5, 7, 9, 10, 11, 12):
            button = QPushButton(str(stage), card)
            button.setCheckable(True)
            button.setFixedSize(22, 19)
            button.setToolTip(I18N.tr(
                "mixer.effect_panel.phaser_stages", "{count} all-pass stages").format(count=stage))
            button.clicked.connect(lambda checked=False, value=stage: card._on_stage(value))
            card.stage_btns[stage] = button
            card._stage_selector.insertWidget(stage - 1, button)

    @staticmethod
    def _phaser_factory_state() -> dict:
        from src.features.edit.core.timeline_v2 import PhaserParams

        model = PhaserParams.default()
        state = default_mod_neutral()
        state["phaser"].update(
            enabled=True, stages=model.stages, rate=model.rate_hz, center_hz=model.base_hz,
            depth=(model.sweep_octaves - 0.5) * 100.0 / 4.5, feedback=model.feedback * 100.0,
            mix=model.mix * 100.0, stereo_processing=model.stereo_processing)
        return state

    def set_processor_algorithm_version(self, version: int | None) -> None:
        legacy = version is not None and version <= 4
        self.mod_visualizer._processor_legacy = legacy
        if self._processor_effect == "phaser":
            card = self.phaser_card
            card.stereo_button.setVisible(not legacy)
            for stage, button in card.stage_btns.items():
                button.setVisible(not legacy or stage <= 4)
            card.c_feedback.set_control_range(
                0.0 if legacy else -95.0, 95.0, "0%" if legacy else "-95%", "95%")
            self._refresh_all_views()
        elif self._processor_effect == "chorus_flanger":
            for control in (self.chorus_card.c_processor_feedback, self.flanger_card.c_feedback):
                control.set_control_range(
                    0.0 if legacy else -95.0, 95.0, "0%" if legacy else "-95%", "95%")
            for control in (self.chorus_card.c_delay, self.flanger_card.c_manual):
                control.set_control_range(0.5, 50.0 if legacy else 100.0,
                                          "0.5", "50" if legacy else "100")
            presets = self.preset_combo.model()
            if isinstance(presets, QStandardItemModel):
                for index in range(self.preset_combo.count()):
                    preset = self._presets.get(self.preset_combo.itemText(index))
                    if preset is not None and preset["flanger"]["feedback"] < 0.0:
                        presets.item(index).setEnabled(not legacy)
        self.mod_visualizer.update()

    def set_sample_rate(self, sample_rate: float) -> None:
        self.mod_visualizer.set_sample_rate(sample_rate)

    def update_processor_telemetry(self, input_db: float, output_db: float,
                                   analysis: dict) -> None:
        self.meter_input.setValue(input_db)
        self.meter_output.setValue(output_db)
        rate = analysis.get("sample_rate")
        if rate is not None:
            self.set_sample_rate(float(rate))
        self.mod_visualizer.set_processor_phase(float(analysis.get("phase", 0.0)))

    def _on_chorus_feedback(self, feedback: float) -> None:
        self.chorus_card._set_field("feedback", round(feedback, 1))

    def _build_meter_column(self, header_text: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        lbl = QLabel(header_text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #9A9AA2; font-size: 9px; font-weight: 700; letter-spacing: 0.7px;")
        col.addWidget(lbl)
        return col

    # -----------------------------------------------------------------
    # 状态同步与三模式联动逻辑
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _detect_current_mode(self) -> str:
        st = self._current_state()
        active_mods = [k for k in ("chorus", "flanger", "phaser") if st[k]["enabled"]]
        if len(active_mods) == 1:
            return active_mods[0].capitalize()
        return "Rack"

    def _update_top_bar_styles(self):
        st = self._current_state()

        active_ab = """
            QPushButton {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 700;
            }
        """
        inactive_ab = """
            QPushButton {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_ab if self._active_bank == "A" else inactive_ab)
        self.btn_b.setStyleSheet(active_ab if self._active_bank == "B" else inactive_ab)
        self.btn_copy_ab.setStyleSheet(inactive_ab)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

        cur_mode = self._detect_current_mode()
        mode_colors = {
            "Chorus": COLORS["chorus"],
            "Flanger": COLORS["flanger"],
            "Phaser": COLORS["phaser"],
            "Rack": COLORS["cyan"],
        }
        for mk, btn in self.mode_btns.items():
            is_cur = (mk == cur_mode)
            col = mode_colors[mk]
            if is_cur:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: #182228;
                        color: {col};
                        border: 1px solid {col};
                        border-radius: 3px;
                        font-size: 9.5px;
                        font-weight: 700;
                    }}
                """)
            else:
                btn.setStyleSheet(inactive_ab)

        self.routing_btn.setText(st["routing"])

    def _set_focused_module(self, mod_key: str):
        if mod_key not in ("chorus", "flanger", "phaser"):
            return
        self._focused_module = mod_key
        self.chorus_card.setSelected(mod_key == "chorus")
        self.flanger_card.setSelected(mod_key == "flanger")
        self.phaser_card.setSelected(mod_key == "phaser")
        self.mod_visualizer.setFocusedModule(mod_key)

    def _select_quick_mode(self, mode_name: str):
        """一键切换三模式（Chorus / Flanger / Phaser 单模式聚焦，或 Rack 多模块叠加）"""
        if self._processor_effect:
            if self._processor_effect == "chorus_flanger" and mode_name in ("Chorus", "Flanger"):
                self.processorModeChanged.emit(mode_name.lower())
            return
        st = self._current_state()
        if mode_name == "Chorus":
            st["chorus"]["enabled"] = True
            st["flanger"]["enabled"] = False
            st["phaser"]["enabled"] = False
            self._focused_module = "chorus"
        elif mode_name == "Flanger":
            st["chorus"]["enabled"] = False
            st["flanger"]["enabled"] = True
            st["phaser"]["enabled"] = False
            self._focused_module = "flanger"
        elif mode_name == "Phaser":
            st["chorus"]["enabled"] = False
            st["flanger"]["enabled"] = False
            st["phaser"]["enabled"] = True
            self._focused_module = "phaser"
        elif mode_name == "Rack":
            if sum(1 for k in ("chorus", "flanger", "phaser") if st[k]["enabled"]) < 2:
                st["chorus"]["enabled"] = True
                st["phaser"]["enabled"] = True

        self._refresh_all_views()
        self.modeChanged.emit(self._detect_current_mode())
        self.stateChanged.emit(self.get_state())

    def _toggle_routing(self):
        st = self._current_state()
        st["routing"] = "Parallel" if st["routing"] == "Serial" else "Serial"
        self.routing_btn.setText(st["routing"])
        self.routingChanged.emit(st["routing"])
        self.stateChanged.emit(self.get_state())

    def _refresh_all_views(self):
        st = self._current_state()
        # 若当前焦点模块未启用而其他模块已启用，自动将焦点切换到已启用模块
        if not st[self._focused_module]["enabled"]:
            for k in ("chorus", "flanger", "phaser"):
                if st[k]["enabled"]:
                    self._focused_module = k
                    break

        if self._processor_effect:
            if self._processor_effect == "chorus_flanger":
                self._processor_mode = "flanger" if st["flanger"]["enabled"] else "chorus"
            active = self._processor_mode
            for key in ("chorus", "flanger", "phaser"):
                st[key]["enabled"] = key == active
                getattr(self, f"{key}_card").setVisible(key == active)
            self._focused_module = active
            if active == "phaser" and self.mod_visualizer._processor_legacy:
                module = st["phaser"]
                module.update(stages=min(module["stages"], 4),
                              feedback=max(module["feedback"], 0.0), stereo_processing=False)

        self._update_top_bar_styles()
        self.power_switch.setChecked(st["enabled"])
        self.out_fader.setGain(st["output_gain"])

        self.chorus_card.syncFromData(st["chorus"])
        self.flanger_card.syncFromData(st["flanger"])
        self.phaser_card.syncFromData(st["phaser"])
        self._set_focused_module(self._focused_module)

        self.meter_chr.setActive(st["enabled"] and st["chorus"]["enabled"])
        self.meter_flg.setActive(st["enabled"] and st["flanger"]["enabled"])
        self.meter_phs.setActive(st["enabled"] and st["phaser"]["enabled"])
        self.meter_wid.setActive(st["enabled"])
        self.mod_visualizer.setState(st)

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

    def _on_chorus_changed(self, data: dict):
        st = self._current_state()
        st["chorus"] = data
        self._update_top_bar_styles()
        self.meter_chr.setActive(st["enabled"] and data["enabled"])
        self.mod_visualizer.setState(st)
        self.chorusChanged.emit(copy.deepcopy(data))
        self.modeChanged.emit(self._detect_current_mode())
        self.stateChanged.emit(self.get_state())

    def _on_flanger_changed(self, data: dict):
        st = self._current_state()
        st["flanger"] = data
        self._update_top_bar_styles()
        self.meter_flg.setActive(st["enabled"] and data["enabled"])
        self.mod_visualizer.setState(st)
        self.flangerChanged.emit(copy.deepcopy(data))
        self.modeChanged.emit(self._detect_current_mode())
        self.stateChanged.emit(self.get_state())

    def _on_phaser_changed(self, data: dict):
        st = self._current_state()
        st["phaser"] = data
        self._update_top_bar_styles()
        self.meter_phs.setActive(st["enabled"] and data["enabled"])
        self.mod_visualizer.setState(st)
        self.phaserChanged.emit(copy.deepcopy(data))
        self.modeChanged.emit(self._detect_current_mode())
        self.stateChanged.emit(self.get_state())

    def _on_output_gain_changed(self, gain: float):
        st = self._current_state()
        st["output_gain"] = gain
        self.outputGainChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _on_canvas_rate_depth_dragged(self, mod_key: str, rate_hz: float, depth_pct: float, r_chg: bool, d_chg: bool):
        st = self._current_state()
        mod = st[mod_key]
        mod["enabled"] = True
        if r_chg:
            mod["rate"] = rate_hz
        if d_chg:
            mod["depth"] = depth_pct

        self._refresh_all_views()
        if mod_key == "chorus":
            self.chorusChanged.emit(copy.deepcopy(mod))
        elif mod_key == "flanger":
            self.flangerChanged.emit(copy.deepcopy(mod))
        elif mod_key == "phaser":
            self.phaserChanged.emit(copy.deepcopy(mod))
        self.stateChanged.emit(self.get_state())

    def _on_canvas_spread_scrolled(self, mod_key: str, spread_pct: float):
        if self._processor_effect:
            return
        st = self._current_state()
        mod = st[mod_key]
        mod["spread"] = spread_pct
        self._refresh_all_views()
        if mod_key == "chorus":
            self.chorusChanged.emit(copy.deepcopy(mod))
        elif mod_key == "flanger":
            self.flangerChanged.emit(copy.deepcopy(mod))
        elif mod_key == "phaser":
            self.phaserChanged.emit(copy.deepcopy(mod))
        self.stateChanged.emit(self.get_state())

    def _on_canvas_rate_depth_reset(self, mod_key: str):
        defaults = default_mod_demo()[mod_key]
        st = self._current_state()
        st[mod_key]["rate"] = defaults["rate"]
        st[mod_key]["depth"] = defaults["depth"]
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def _load_preset(self, name: str):
        if name in self._presets:
            preset = copy.deepcopy(self._presets[name])
            if self._processor_effect == "chorus_flanger":
                preset["enabled"] = self._current_state()["enabled"]
            self._banks[self._active_bank] = preset
            self._refresh_all_views()
            self.modeChanged.emit(self._detect_current_mode())
            self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        cur_name = self.preset_combo.currentText()
        default_name = "" if cur_name in self._builtin_preset_names else cur_name
        name, ok = QInputDialog.getText(self, "保存调制器预设", "输入预设名称（同名自定义预设将覆盖）:", text=default_name)
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentText(clean)
            self.preset_combo.blockSignals(False)

    def _delete_current_preset(self):
        cur_name = self.preset_combo.currentText()
        if cur_name in self._builtin_preset_names or cur_name not in self._presets:
            return
        del self._presets[cur_name]
        idx = self.preset_combo.currentIndex()
        self.preset_combo.removeItem(idx)

    # -----------------------------------------------------------------
    # 演示实时梳状扫频与 L/R LFO 相位模拟（接入真实引擎后自动停止）
    # -----------------------------------------------------------------
    def _tick_demo_simulation(self):
        self._anim_t += 0.038
        st = self._current_state()
        freqs = self.mod_visualizer._freqs
        en = st["enabled"]

        c_on = en and st["chorus"]["enabled"]
        f_on = en and st["flanger"]["enabled"]
        p_on = en and st["phaser"]["enabled"]

        c_lfo = math.sin(2.0 * math.pi * st["chorus"]["rate"] * self._anim_t)
        f_lfo = math.sin(2.0 * math.pi * st["flanger"]["rate"] * self._anim_t)
        p_lfo = math.sin(2.0 * math.pi * st["phaser"]["rate"] * self._anim_t)

        # 生成合成梳状/全通扫频频响曲线 (dB)
        comb_db = []
        for f in freqs:
            db = 0.0
            if c_on:
                c_depth = st["chorus"]["depth"] / 100.0
                c_mix = st["chorus"]["mix"] / 100.0
                tau = (st["chorus"]["delay_ms"] + 2.5 * c_depth * c_lfo) / 1000.0
                comb = math.cos(2.0 * math.pi * f * tau)
                tilt = (st["chorus"]["tone"] / 50.0) * 2.5 * math.log10(max(80.0, f) / 800.0)
                db += (3.2 * comb * c_depth + tilt) * c_mix

            if f_on:
                f_depth = st["flanger"]["depth"] / 100.0
                f_fb = st["flanger"]["feedback"] / 100.0
                f_mix = st["flanger"]["mix"] / 100.0
                man = st["flanger"]["manual_ms"]
                sweep_ms = max(0.15, man * (1.0 + 0.72 * f_depth * f_lfo))
                tau_f = sweep_ms / 1000.0
                phase = 2.0 * math.pi * f * tau_f
                # 正反馈增强谐波峰，负反馈产生中空陷波
                denom = max(0.18, 1.0 - 0.78 * f_fb * math.cos(phase))
                mag_db = 8.0 * math.log10(1.0 / denom)
                db += mag_db * f_mix

            if p_on:
                stages = int(st["phaser"]["stages"])
                num_notches = max(1, stages // 2)
                p_depth = st["phaser"]["depth"] / 100.0
                p_fb = st["phaser"]["feedback"] / 100.0
                p_mix = st["phaser"]["mix"] / 100.0
                c_hz = st["phaser"]["center_hz"] * (2.0 ** (1.35 * p_depth * p_lfo))
                notch_sum = 0.0
                for n_i in range(num_notches):
                    ratio = 0.42 + (n_i / max(1, num_notches - 1)) * 1.65 if num_notches > 1 else 1.0
                    nf = c_hz * ratio
                    dist = math.log2(max(20.0, f) / max(20.0, nf))
                    q_w = max(0.08, 0.24 - abs(p_fb) * 0.12)
                    notch_dip = -11.5 * math.exp(-(dist * dist) / (2.0 * q_w * q_w))
                    res_bump = (4.5 * p_fb) * math.exp(-((abs(dist) - q_w * 1.5) ** 2) / 0.03)
                    notch_sum += (notch_dip + res_bump)
                db += (notch_sum / math.sqrt(num_notches)) * p_mix

            comb_db.append(max(-17.5, min(17.5, db)))

        # 生成 L/R 双声道 LFO 波形及立体声扩展相位差
        fmod = st[self._focused_module]
        spread_pct = fmod["spread"]
        phase_offset_rad = (spread_pct / 200.0) * math.pi
        lfo_l, lfo_r = [], []
        n_pts = 140
        for i in range(n_pts):
            ang = (i / (n_pts - 1)) * 4.0 * math.pi + self._anim_t * fmod["rate"] * 5.0
            lfo_l.append(math.sin(ang))
            lfo_r.append(math.sin(ang + phase_offset_rad))

        phase_deg = (spread_pct / 200.0) * 180.0
        self.mod_visualizer.updateAnalysis(freqs, comb_db, lfo_l, lfo_r, phase_deg)

        # 更新电平表与四通道引擎活跃度
        in_lvl = -19.0 + 5.5 * math.sin(self._anim_t * 2.2)
        out_lvl = max(-55.0, min(-1.0, in_lvl + st["output_gain"]))
        self.meter_input.setValue(in_lvl)
        self.meter_output.setValue(out_lvl)

        self.meter_chr.setValue(min(100.0, (st["chorus"]["depth"] * 0.6 + st["chorus"]["mix"] * 0.4) * (0.7 + 0.3 * abs(c_lfo))) if c_on else 0.0)
        self.meter_flg.setValue(min(100.0, (st["flanger"]["depth"] * 0.6 + abs(st["flanger"]["feedback"]) * 0.4) * (0.7 + 0.3 * abs(f_lfo))) if f_on else 0.0)
        self.meter_phs.setValue(min(100.0, (st["phaser"]["depth"] * 0.65 + (st["phaser"]["stages"] / 8.0) * 35.0) * (0.7 + 0.3 * abs(p_lfo))) if p_on else 0.0)
        self.meter_wid.setValue(min(100.0, spread_pct / 2.0) if en else 0.0)

    # -----------------------------------------------------------------
    # 公开 API：供后端音频 DSP 引擎与工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_modulation(self):
        if self._processor_effect == "phaser":
            state = self._phaser_factory_state()
        else:
            state = default_mod_neutral()
        if self._processor_effect == "chorus_flanger":
            state["enabled"] = self._current_state()["enabled"]
            mode = self._processor_mode
            state[mode].update(enabled=True, rate=0.6, depth=30.0,
                               feedback=0.0, mix=50.0, voices=2)
            state[mode]["delay_ms" if mode == "chorus" else "manual_ms"] = 18.0
        self._banks[self._active_bank] = state
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        state = copy.deepcopy(self._current_state())
        if self._processor_effect == "chorus_flanger":
            state["mode"] = self._processor_mode
        state["chorus"].setdefault("voices", 2)
        state["chorus"].setdefault("feedback", 0.0)
        state["flanger"].setdefault("voices", 2)
        state["phaser"].setdefault("stereo_processing", False)
        return state

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_analysis(
        self,
        freqs_hz: list[float],
        comb_curve_db: list[float],
        lfo_left: list[float],
        lfo_right: list[float],
        phase_spread_deg: float = 120.0,
    ):
        """接入真实音频引擎的实时梳状/全通扫频曲线与 L/R 立体声 LFO 采样"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.mod_visualizer.updateAnalysis(freqs_hz, comb_curve_db, lfo_left, lfo_right, phase_spread_deg)

    def update_meters(
        self,
        input_db: float,
        output_db: float,
        chorus_activity: float = 0.0,
        flanger_activity: float = 0.0,
        phaser_activity: float = 0.0,
        stereo_width_pct: float = 50.0,
    ):
        """接入真实音频引擎的输入/输出电平与三模块活跃度/立体声宽度 (0~100%)"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.meter_input.setValue(input_db)
        self.meter_output.setValue(output_db)
        self.meter_chr.setValue(chorus_activity)
        self.meter_flg.setValue(flanger_activity)
        self.meter_phs.setValue(phaser_activity)
        self.meter_wid.setValue(stereo_width_pct)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioModulationTrioWidget("Synth / Vocal Bus")
    win.show()
    sys.exit(app.exec())
