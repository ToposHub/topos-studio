from __future__ import annotations

import sys
import math
import copy
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont
)

from src.shared.ui.widgets.chevron_combo import ChevronComboBox

# =====================================================================
# 1. 全局色彩系统与默认参数配置（与 Dyamics-UI / multiband_comp_ui 保持同一视觉规范）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":   "#28D8F0",  # 主控 / A-B 激活 / 基波频谱主色
    "band_0": "#6495FA",  # Band 1 (Low-Mid 厚度) 钴蓝
    "band_1": "#4CD490",  # Band 2 (Presence 临场感) 翡翠绿
    "band_2": "#F0A848",  # Band 3 (Air 空气感) 琥珀金
    "even":   "#F0A848",  # 偶次谐波 (2nd/4th 温暖)
    "odd":    "#F26D6D",  # 奇次谐波 (3rd/5th 穿透)
}

BAND_PALETTE = [
    COLORS["band_0"],
    COLORS["band_1"],
    COLORS["band_2"],
]


def _make_exciter_band(
    enabled: bool = True,
    mode: str = "Aural",         # "Aural" | "Tube" | "Tape" | "Triode"
    amount: float = 35.0,        # 0.0 ~ 100.0 % (激励驱动量)
    harmonics: float = 0.0,      # -100.0 (Pure Even) ~ +100.0 (Pure Odd)
    bias: float = 15.0,          # 0.0 ~ 100.0 % (高次谐波延伸度 / 锐度)
    mix: float = 65.0,           # 0.0 ~ 100.0 % (单频段干湿比)
    output: float = 0.0,         # -12.0 ~ +12.0 dB (频段输出补偿)
    solo: bool = False,
    mute: bool = False,
) -> dict:
    return {
        "enabled": enabled,
        "mode": mode,
        "amount": amount,
        "harmonics": harmonics,
        "bias": bias,
        "mix": mix,
        "output": output,
        "solo": solo,
        "mute": mute,
    }


def default_exciter_demo() -> dict:
    """默认 3 频段演示配置：中频饱和厚度 + 临场感清晰度 + 空气感高频开口"""
    return {
        "enabled": True,
        "ui_mode": "Standalone",          # "Standalone" | "Sub-Module"
        "band_count": 3,                  # 1 (单段经典 Aphex) | 2 | 3 (多段 Ozone)
        "crossovers": [850.0, 4800.0],    # 最多 2 个分频点 (Hz)
        "oversampling": "4x HQ",          # "1x Off" | "2x" | "4x HQ" | "8x Ultra"
        "global_mix": 100.0,              # 0.0 ~ 100.0 % (全局总干湿比)
        "output_gain": 0.0,               # -12.0 ~ +12.0 dB
        "auto_gain": True,                # 自动响度补偿开关
        "bands": [
            _make_exciter_band(
                enabled=False, mode="Tube", amount=22.0, harmonics=-45.0,
                bias=20.0, mix=40.0, output=0.0
            ),
            _make_exciter_band(
                enabled=True, mode="Aural", amount=42.0, harmonics=15.0,
                bias=35.0, mix=60.0, output=0.5
            ),
            _make_exciter_band(
                enabled=True, mode="Tape", amount=58.0, harmonics=30.0,
                bias=55.0, mix=75.0, output=1.0
            ),
        ],
    }


def default_exciter_flat() -> dict:
    st = default_exciter_demo()
    st["output_gain"] = 0.0
    st["global_mix"] = 100.0
    for b in st["bands"]:
        b["enabled"] = False
        b["amount"] = 0.0
        b["harmonics"] = 0.0
        b["bias"] = 25.0
        b["mix"] = 50.0
        b["output"] = 0.0
        b["solo"] = False
        b["mute"] = False
    return st


def format_freq(hz: float) -> str:
    if hz >= 10000.0:
        return f"{hz / 1000.0:.1f}k"
    if hz >= 1000.0:
        return f"{hz / 1000.0:.2f}k"
    return f"{int(round(hz))}"


def parse_freq(text: str, fallback: float) -> float:
    s = text.strip().lower().replace("hz", "").strip()
    try:
        if s.endswith("k"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def format_harmonics(val: float) -> str:
    """格式化奇偶谐波比例 (-100 Even ~ 0 Bal ~ +100 Odd)"""
    v = int(round(val))
    if abs(v) <= 2:
        return "50/50"
    if v < 0:
        return f"E {abs(v)}%"
    return f"O {v}%"


def parse_harmonics(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("%", "").strip()
    try:
        if s.startswith("E"):
            return -abs(float(s[1:].strip()))
        if s.startswith("O"):
            return abs(float(s[1:].strip()))
        if "/" in s:
            return 0.0
        return float(s)
    except ValueError:
        return fallback


def format_db_signed(val: float) -> str:
    if abs(val) < 0.05:
        return "0.0"
    return f"{val:+.1f}"


# =====================================================================
# 2. 基础定制控件：发光电源开关 / 可编辑数字胶囊 / 拉丝阳极铝旋钮 / 电平表
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


class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 56, parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._active = True
        self.setFixedSize(width, 20)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(0, 0, width, 20)
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
            clean = self._text.replace("dB", "").replace("%", "").replace("Hz", "").strip()
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


class ProRotaryKnob(QWidget):
    """支持单向 (0->max) 与双向对称 (-min <-> +max，如奇偶谐波平衡) 两种光环绘制模式"""
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value: float,
        default_value: float,
        accent_color: str = "#28D8F0",
        bipolar: bool = False,
        neg_color: str = "#F0A848",
        pos_color: str = "#F26D6D",
        parent=None,
    ):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._bipolar = bipolar
        self._neg_color = QColor(neg_color)
        self._pos_color = QColor(pos_color)
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

        arc_r = 14.5
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 底环凹槽
        p.setPen(QPen(QColor("#141417"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 激活光环
        if self._bipolar:
            zero_deg = 90.0  # 12点钟方向为 50/50 平衡点
            span_deg = curr_deg - zero_deg
            if self._active:
                arc_col = self._neg_color if self._value < 0 else self._pos_color
            else:
                arc_col = QColor("#4A4A52")
            p.setPen(QPen(arc_col, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            arc_col = self._accent if self._active else QColor("#4A4A52")
            p.setPen(QPen(arc_col, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        # 阳极铝旋钮顶盖
        knob_r = 10.6
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.0
        iy1 = cy - math.sin(rad) * 3.0
        ix2 = cx + math.cos(rad) * (knob_r - 1.4)
        iy2 = cy - math.sin(rad) * (knob_r - 1.4)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


class ProVerticalMeter(QWidget):
    """垂直电平表 / 谐波激励能量表"""
    def __init__(
        self,
        title: str = "",
        mode: str = "level",      # "level" (dBFS) | "harmonic" (0~100% THD 激励度)
        min_val: float = -55.0,
        max_val: float = 0.0,
        ticks: list[int] | None = None,
        show_ticks: bool = True,
        accent_color: str = "#28D8F0",
        parent=None,
    ):
        super().__init__(parent)
        self._title = title
        self._mode = mode
        self._min_val = min_val
        self._max_val = max_val
        self._ticks = ticks or (
            [0, -5, -10, -15, -20, -30, -40, -50]
            if mode == "level"
            else [100, 75, 50, 25, 10, 0]
        )
        self._show_ticks = show_ticks
        self._accent = QColor(accent_color)

        self._value = min_val
        self._peak = min_val
        self._clipped = False
        self._active = True

        self.setFixedWidth(40 if show_ticks else 16)
        self.setMinimumHeight(175)

    def setValue(self, val: float):
        self._value = max(self._min_val, min(self._max_val, val))
        if self._value >= self._peak:
            self._peak = self._value
        else:
            decay = 0.35 if self._mode == "level" else 1.2
            self._peak = max(self._value, self._peak - decay)
        if self._mode == "level" and val >= -0.1:
            self._clipped = True
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

    def _val_to_y(self, v: float, bar: QRectF) -> float:
        clamped = max(self._min_val, min(self._max_val, v))
        if self._mode == "level":
            if clamped >= -20.0:
                norm = 0.52 + ((clamped + 20.0) / 20.0) * 0.48
            else:
                norm = ((clamped - self._min_val) / (-20.0 - self._min_val)) * 0.52
        else:
            norm = (clamped - self._min_val) / max(1e-5, self._max_val - self._min_val)
        return bar.bottom() - norm * bar.height()

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
            for t in self._ticks:
                ty = self._val_to_y(float(t), bar)
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
            y_val = self._val_to_y(self._value, bar)
            fill_h = bar.bottom() - y_val
            if fill_h > 1.0:
                fill_rect = QRectF(bar.left() + 1.0, y_val, bar.width() - 2.0, fill_h - 0.5)
                grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                if self._mode == "level":
                    grad.setColorAt(0.0,  QColor("#26C281"))
                    grad.setColorAt(0.50, QColor("#4CD490"))
                    grad.setColorAt(0.53, QColor("#E8C538"))
                    grad.setColorAt(0.75, QColor("#F0A848"))
                    grad.setColorAt(0.78, QColor("#F26D6D"))
                    grad.setColorAt(1.0,  QColor("#FF4848"))
                else:
                    # 谐波激励能量表：从冷青色到琥珀金再到珊瑚红饱和
                    grad.setColorAt(0.0,  QColor("#28D8F0"))
                    grad.setColorAt(0.55, QColor("#F0A848"))
                    grad.setColorAt(1.0,  QColor("#F26D6D"))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(grad)
                p.drawRoundedRect(fill_rect, 1.2, 1.2)

            if self._peak > self._min_val + 0.5:
                py = self._val_to_y(self._peak, bar)
                p.setPen(QPen(QColor("#F0F2F6"), 1.3))
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
            p.setPen(self._accent if self._active else QColor("#55555C"))
            p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, self._title)


class OutputFaderStrip(QWidget):
    """主输出双向增益推子 (-12.0 ~ +12.0 dB)"""
    gainChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gain = 0.0
        self._dragging = False
        self.setFixedWidth(54)
        self.setMinimumHeight(175)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setGain(self, val: float, emit: bool = False):
        clamped = max(-12.0, min(12.0, round(val, 1)))
        if abs(clamped - self._gain) > 1e-4:
            self._gain = clamped
            self.update()
            if emit:
                self.gainChanged.emit(self._gain)

    def _track_rect(self) -> QRectF:
        return QRectF(33.0, 10.0, 4.0, max(40.0, self.height() - 36.0))

    def _y_to_gain(self, y: float) -> float:
        tr = self._track_rect()
        norm = max(0.0, min(1.0, (y - tr.top()) / tr.height()))
        return 12.0 - norm * 24.0

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
        mid_y = tr.center().y()

        p.setFont(QFont(MONO_FONT_FAM, 7))
        for val in (12, 6, 0, -6, -12):
            norm = (12.0 - val) / 24.0
            ty = tr.top() + norm * tr.height()
            p.setPen(QColor("#9A9AA2"))
            lbl = f"+{val} -" if val > 0 else ("0 -" if val == 0 else f"{val} -")
            p.drawText(QRectF(0, ty - 7, tr.left() - 9, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        norm = (12.0 - self._gain) / 24.0
        cy = tr.top() + norm * tr.height()
        if abs(self._gain) > 0.05:
            top_y = min(cy, mid_y)
            bot_y = max(cy, mid_y)
            active_rect = QRectF(tr.left() + 0.5, top_y, tr.width() - 1.0, bot_y - top_y)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(40, 216, 240, 150))
            p.drawRoundedRect(active_rect, 1.5, 1.5)

        cap_w, cap_h = 18.0, 20.0
        cap_rect = QRectF(cx - cap_w / 2.0, cy - cap_h / 2.0, cap_w, cap_h)
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if self._dragging else "#7A7C82"))
        p.drawRoundedRect(cap_rect, 3.0, 3.0)

        p.setPen(QPen(QColor("#28D8F0"), 1.6))
        p.drawLine(QPointF(cap_rect.left() + 3, cy), QPointF(cap_rect.right() - 3, cy))

        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor("#28D8F0"))
        p.drawText(QRectF(4, self.height() - 21, self.width() - 4, 18), Qt.AlignmentFlag.AlignCenter, format_db_signed(self._gain))


# =====================================================================
# 3. 控件：交互式高频谐波激励频谱与包络响应画布 (ProExciterGraphWidget)
# =====================================================================
class ProExciterGraphWidget(QWidget):
    crossoverDragged = pyqtSignal(int, float)    # xover_idx, freq_hz
    bandAmountDragged = pyqtSignal(int, float)     # band_idx, amount_pct
    bandSelected = pyqtSignal(int)
    bandSoloToggled = pyqtSignal(int)
    bandMuteToggled = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 220)
        self.setMouseTracking(True)

        self._state = default_exciter_demo()
        self._selected_band = 2  # 默认选中高频 Air 段

        # 实时基波频谱与激励后生成的谐波泛音频谱 (64 频点)
        self._spectrum_freqs = [20.0 * ((20000.0 / 20.0) ** (i / 63.0)) for i in range(64)]
        self._dry_dbs = [-45.0] * 64
        self._wet_dbs = [-45.0] * 64

        self._drag_mode = ""        # "xover" | "node" | ""
        self._drag_index = -1
        self._hover_mode = ""       # "xover" | "node" | "solo" | "mute" | ""
        self._hover_index = -1
        self._mouse_pos: QPointF | None = None

        self._solo_rects: list[QRectF] = []
        self._mute_rects: list[QRectF] = []

        self._ml = 34.0
        self._mr = 12.0
        self._mt = 12.0
        self._mb = 22.0

    def setState(self, state: dict):
        self._state = state
        self.update()

    def setSelectedBand(self, idx: int):
        if self._selected_band != idx:
            self._selected_band = idx
            self.update()

    def setRealtimeSpectrum(self, dry_dbs: list[float], wet_dbs: list[float] | None = None):
        if len(dry_dbs) >= 2:
            self._dry_dbs = list(dry_dbs)
            if len(self._spectrum_freqs) != len(dry_dbs):
                n = len(dry_dbs)
                self._spectrum_freqs = [20.0 * ((20000.0 / 20.0) ** (i / (n - 1))) for i in range(n)]
            self._wet_dbs = list(wet_dbs) if wet_dbs is not None else list(dry_dbs)
            self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(120.0, self.width() - self._ml - self._mr),
            max(100.0, self.height() - self._mt - self._mb),
        )

    def _freq_to_x(self, freq: float, plot: QRectF) -> float:
        f = max(20.0, min(20000.0, freq))
        norm = math.log10(f / 20.0) / 3.0
        return plot.left() + norm * plot.width()

    def _x_to_freq(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return 20.0 * (10.0 ** (norm * 3.0))

    def _db_to_y(self, db: float, plot: QRectF) -> float:
        clamped = max(-60.0, min(18.0, db))
        norm = (18.0 - clamped) / 78.0
        return plot.top() + norm * plot.height()

    def _amount_to_y(self, amount: float, plot: QRectF) -> float:
        # 0% 位于 0 dB 基线，100% 对应 +15 dB 视觉激励高度
        boost_db = (max(0.0, min(100.0, amount)) / 100.0) * 15.0
        return self._db_to_y(boost_db, plot)

    def _y_to_amount(self, y: float, plot: QRectF) -> float:
        y0 = self._db_to_y(0.0, plot)
        y100 = self._db_to_y(15.0, plot)
        norm = max(0.0, min(1.0, (y0 - y) / max(10.0, y0 - y100)))
        return norm * 100.0

    def _band_x_ranges(self, plot: QRectF) -> list[tuple[float, float]]:
        cnt = self._state["band_count"]
        xovers = self._state["crossovers"][: cnt - 1]
        edges = [plot.left()] + [self._freq_to_x(f, plot) for f in xovers] + [plot.right()]
        return [(edges[i], edges[i + 1]) for i in range(cnt)]

    def _band_node_pos(self, idx: int, plot: QRectF) -> QPointF:
        ranges = self._band_x_ranges(plot)
        x1, x2 = ranges[idx]
        cx = (x1 + x2) / 2.0
        cy = self._amount_to_y(self._state["bands"][idx]["amount"], plot)
        return QPointF(cx, cy)

    def _hit_test(self, pos: QPointF, plot: QRectF) -> tuple[str, int]:
        for idx, r in enumerate(self._solo_rects):
            if r.contains(pos):
                return "solo", idx
        for idx, r in enumerate(self._mute_rects):
            if r.contains(pos):
                return "mute", idx

        cnt = self._state["band_count"]
        # 优先命中频段激励量控制节点 (半径 11px)
        for i in range(cnt):
            pt = self._band_node_pos(i, plot)
            if math.hypot(pos.x() - pt.x(), pos.y() - pt.y()) <= 11.0:
                return "node", i

        # 命中分频线 (容差 7px)
        xovers = self._state["crossovers"][: cnt - 1]
        for i, xf in enumerate(xovers):
            xx = self._freq_to_x(xf, plot)
            if abs(pos.x() - xx) <= 7.0 and plot.top() <= pos.y() <= plot.bottom():
                return "xover", i

        return "", -1

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_mode = ""
        self._hover_index = -1
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            plot = self._plot_rect()
            mode, idx = self._hit_test(event.position(), plot)
            if mode == "solo":
                self.bandSoloToggled.emit(idx)
                return
            if mode == "mute":
                self.bandMuteToggled.emit(idx)
                return
            if mode in ("xover", "node"):
                self._drag_mode = mode
                self._drag_index = idx
                if mode == "node":
                    self.bandSelected.emit(idx)
                return

            ranges = self._band_x_ranges(plot)
            for i, (x1, x2) in enumerate(ranges):
                if x1 <= event.position().x() <= x2 and plot.top() <= event.position().y() <= plot.bottom():
                    self.bandSelected.emit(i)
                    break

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._drag_mode == "xover" and self._drag_index >= 0:
            idx = self._drag_index
            cnt = self._state["band_count"]
            xovers = self._state["crossovers"]
            low_bound = 80.0 if idx == 0 else xovers[idx - 1] * 1.25
            high_bound = 16000.0 if idx >= cnt - 2 else xovers[idx + 1] / 1.25
            raw_hz = self._x_to_freq(event.position().x(), plot)
            clamped_hz = max(low_bound, min(high_bound, raw_hz))
            rounded = round(clamped_hz, 0) if clamped_hz < 1000.0 else round(clamped_hz, -1)
            self.crossoverDragged.emit(idx, float(rounded))
            return

        if self._drag_mode == "node" and self._drag_index >= 0:
            amt = round(self._y_to_amount(event.position().y(), plot), 1)
            self.bandAmountDragged.emit(self._drag_index, amt)
            return

        mode, idx = self._hit_test(event.position(), plot)
        self._hover_mode = mode
        self._hover_index = idx
        if mode == "xover":
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif mode == "node":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif mode in ("solo", "mute"):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_mode = ""
            self._drag_index = -1
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 画布背景
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 对数频率与 dB 网格
        grid_pen = QPen(QColor("#18181C"), 1.0)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        for db in (12, 0, -12, -24, -36, -48, -60):
            y = self._db_to_y(float(db), plot)
            p.setPen(QPen(QColor("#303038"), 1.1) if db == 0 else grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

            p.setPen(QColor("#9A9AA2"))
            lbl = f"+{db}" if db > 0 else str(db)
            p.drawText(QRectF(0, y - 8, plot.left() - 5, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        freq_ticks = [
            (20, "20"), (50, "50"), (100, "100"), (200, "200"), (500, "500"),
            (1000, "1k"), (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k")
        ]
        for f_val, f_lbl in freq_ticks:
            x = self._freq_to_x(float(f_val), plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#9A9AA2"))
            p.drawText(QRectF(x - 16, plot.bottom() + 4, 32, 16), Qt.AlignmentFlag.AlignCenter, f_lbl)

        p.save()
        p.setClipRect(plot)

        st = self._state
        master_on = st["enabled"]
        cnt = st["band_count"]
        bands = st["bands"][:cnt]
        any_solo = any(b["solo"] for b in bands)
        ranges = self._band_x_ranges(plot)
        y_zero = self._db_to_y(0.0, plot)

        # 3. 实时高频谐波延伸频谱（琥珀/珊瑚发光层）与原始基波频谱（青色底层）
        if len(self._dry_dbs) >= 2:
            # 3.1 激励后的高频谐波扩展频谱
            if master_on and len(self._wet_dbs) == len(self._dry_dbs):
                wet_path = QPainterPath()
                wet_fill = QPainterPath()
                for i, (sf, wdb) in enumerate(zip(self._spectrum_freqs, self._wet_dbs)):
                    sx = self._freq_to_x(sf, plot)
                    sy = self._db_to_y(wdb, plot)
                    if i == 0:
                        wet_path.moveTo(sx, sy)
                        wet_fill.moveTo(sx, plot.bottom())
                        wet_fill.lineTo(sx, sy)
                    else:
                        wet_path.lineTo(sx, sy)
                        wet_fill.lineTo(sx, sy)
                wet_fill.lineTo(plot.right(), plot.bottom())
                wet_fill.closeSubpath()

                wet_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
                wet_grad.setColorAt(0.0, QColor(240, 168, 72, 55))
                wet_grad.setColorAt(1.0, QColor(242, 109, 109, 6))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(wet_grad)
                p.drawPath(wet_fill)

                p.setPen(QPen(QColor(240, 168, 72, 145), 1.3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(wet_path)

            # 3.2 原始基波频谱
            dry_path = QPainterPath()
            dry_fill = QPainterPath()
            for i, (sf, ddb) in enumerate(zip(self._spectrum_freqs, self._dry_dbs)):
                sx = self._freq_to_x(sf, plot)
                sy = self._db_to_y(ddb, plot)
                if i == 0:
                    dry_path.moveTo(sx, sy)
                    dry_fill.moveTo(sx, plot.bottom())
                    dry_fill.lineTo(sx, sy)
                else:
                    dry_path.lineTo(sx, sy)
                    dry_fill.lineTo(sx, sy)
            dry_fill.lineTo(plot.right(), plot.bottom())
            dry_fill.closeSubpath()

            dry_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            dry_grad.setColorAt(0.0, QColor(40, 216, 240, 38 if master_on else 18))
            dry_grad.setColorAt(1.0, QColor(40, 216, 240, 4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dry_grad)
            p.drawPath(dry_fill)

            p.setPen(QPen(QColor(40, 216, 240, 95 if master_on else 45), 1.1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(dry_path)

        # 4. 绘制各频段区域与 S / M 快捷按钮
        self._solo_rects = []
        self._mute_rects = []
        band_names = ["WARMTH", "PRESENCE", "AIR"] if cnt == 3 else (["BODY", "EXCITER"] if cnt == 2 else ["FULL EXCITER"])

        for i, (x1, x2) in enumerate(ranges):
            b = bands[i]
            col = QColor(BAND_PALETTE[i])
            band_w = x2 - x1
            is_audible = master_on and (not b["mute"]) and (not any_solo or b["solo"])
            is_selected = (i == self._selected_band)

            bg_alpha = 24 if b["solo"] else (16 if is_selected else 6)
            if not is_audible:
                bg_alpha = 2
            p.fillRect(QRectF(x1, plot.top(), band_w, plot.height()), QColor(col.red(), col.green(), col.blue(), bg_alpha))

            if band_w >= 64.0:
                p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
                p.setPen(col if is_audible else QColor("#666670"))
                p.drawText(
                    QRectF(x1 + 7, plot.top() + 5, band_w - 50, 16),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    f"B{i + 1} · {band_names[i]}"
                )

                m_rect = QRectF(x2 - 21.0, plot.top() + 5.0, 16.0, 15.0)
                s_rect = QRectF(x2 - 40.0, plot.top() + 5.0, 16.0, 15.0)
                self._solo_rects.append(s_rect)
                self._mute_rects.append(m_rect)

                p.setPen(QPen(QColor("#F0A848") if b["solo"] else QColor("#3A3A42"), 1.0))
                p.setBrush(QColor("#3A2E18") if b["solo"] else QColor("#18181C"))
                p.drawRoundedRect(s_rect, 2.5, 2.5)
                p.setPen(QColor("#F0A848") if b["solo"] else QColor("#9A9AA2"))
                p.drawText(s_rect, Qt.AlignmentFlag.AlignCenter, "S")

                p.setPen(QPen(QColor("#F26D6D") if b["mute"] else QColor("#3A3A42"), 1.0))
                p.setBrush(QColor("#381C1E") if b["mute"] else QColor("#18181C"))
                p.drawRoundedRect(m_rect, 2.5, 2.5)
                p.setPen(QColor("#F26D6D") if b["mute"] else QColor("#9A9AA2"))
                p.drawText(m_rect, Qt.AlignmentFlag.AlignCenter, "M")
            else:
                self._solo_rects.append(QRectF())
                self._mute_rects.append(QRectF())

        # 5. 绘制平滑的谐波激励响应包络曲线 + 控制节点
        node_points: list[tuple[int, QPointF, bool]] = []
        for i, (x1, x2) in enumerate(ranges):
            b = bands[i]
            is_audible = master_on and (not b["mute"]) and (not any_solo or b["solo"])
            is_active = is_audible and b["enabled"]
            pt = self._band_node_pos(i, plot)
            node_points.append((i, pt, is_active))

        # 生成平滑插值曲线展示各频段激励抬升轮廓
        env_path = QPainterPath()
        fill_path = QPainterPath()
        steps = 160
        for s in range(steps + 1):
            px = plot.left() + (s / steps) * plot.width()
            # 高斯权重平滑叠加各频段激励抬升
            lift_y = y_zero
            for idx, pt, is_active in node_points:
                if is_active:
                    x1, x2 = ranges[idx]
                    sigma = max(28.0, (x2 - x1) * 0.48)
                    w = math.exp(-0.5 * ((px - pt.x()) / sigma) ** 2)
                    lift_y -= (y_zero - pt.y()) * w
            if s == 0:
                env_path.moveTo(px, lift_y)
                fill_path.moveTo(px, y_zero)
                fill_path.lineTo(px, lift_y)
            else:
                env_path.lineTo(px, lift_y)
                fill_path.lineTo(px, lift_y)
        fill_path.lineTo(plot.right(), y_zero)
        fill_path.closeSubpath()

        if master_on:
            env_grad = QLinearGradient(plot.topLeft(), QPointF(plot.left(), y_zero))
            env_grad.setColorAt(0.0, QColor(40, 216, 240, 48))
            env_grad.setColorAt(1.0, QColor(40, 216, 240, 4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(env_grad)
            p.drawPath(fill_path)

            p.setPen(QPen(QColor(40, 216, 240, 60), 4.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(env_path)

            p.setPen(QPen(QColor("#28D8F0"), 1.8))
            p.drawPath(env_path)

        # 绘制频段控制节点（内环显示奇偶谐波色彩倾向）
        for idx, pt, is_active in node_points:
            b = bands[idx]
            col = QColor(BAND_PALETTE[idx])
            is_hi = (
                (self._hover_mode == "node" and self._hover_index == idx)
                or (self._drag_mode == "node" and self._drag_index == idx)
                or (idx == self._selected_band)
            )
            r = 6.2 if is_hi else 4.8
            if is_hi and is_active:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(col.red(), col.green(), col.blue(), 65))
                p.drawEllipse(pt, r + 4.5, r + 4.5)

            p.setPen(QPen(col if is_active else QColor("#585862"), 1.8))
            p.setBrush(QColor("#151518"))
            p.drawEllipse(pt, r, r)

            # 节点中心小点根据奇偶谐波极性着色（偶次琥珀金 / 奇次珊瑚红）
            if is_active:
                core_col = QColor(COLORS["even"]) if b["harmonics"] < -5 else (
                    QColor(COLORS["odd"]) if b["harmonics"] > 5 else QColor("#28D8F0")
                )
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(core_col)
                p.drawEllipse(pt, 2.2, 2.2)

        # 6. 绘制交叉频率分割线与胶囊标签
        xovers = st["crossovers"][: cnt - 1]
        for i, xf in enumerate(xovers):
            xx = self._freq_to_x(xf, plot)
            is_xo_hi = (
                (self._hover_mode == "xover" and self._hover_index == i)
                or (self._drag_mode == "xover" and self._drag_index == i)
            )
            line_col = QColor("#28D8F0") if is_xo_hi else QColor("#6E707A")
            p.setPen(QPen(line_col, 2.0 if is_xo_hi else 1.2))
            p.drawLine(QPointF(xx, plot.top()), QPointF(xx, plot.bottom()))

            mid_y = plot.bottom() - 24.0
            pill_w, pill_h = 44.0, 17.0
            pill_rect = QRectF(xx - pill_w / 2.0, mid_y - pill_h / 2.0, pill_w, pill_h)
            p.setPen(QPen(QColor("#28D8F0") if is_xo_hi else QColor("#3A3A42"), 1.0))
            p.setBrush(QColor("#151518"))
            p.drawRoundedRect(pill_rect, 3.5, 3.5)
            p.setPen(QColor("#28D8F0") if is_xo_hi else QColor("#DCDCE2"))
            p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
            p.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, format_freq(xf))

        # 7. 实时拖拽 HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            hud_text = ""
            hud_col = QColor("#28D8F0")
            if self._drag_mode == "xover" and 0 <= self._drag_index < len(xovers):
                hz = xovers[self._drag_index]
                hud_text = f"Split {self._drag_index + 1}: {int(round(hz))} Hz"
            elif self._drag_mode == "node" and 0 <= self._drag_index < cnt:
                amt = bands[self._drag_index]["amount"]
                hud_text = f"Band {self._drag_index + 1} Drive: {amt:.1f}%"
                hud_col = QColor(BAND_PALETTE[self._drag_index])

            if hud_text:
                hud_rect = QRectF(plot.left() + 8.0, plot.bottom() - 28.0, 175.0, 19.0)
                p.setPen(QPen(QColor("#141416"), 1.0))
                p.setBrush(QColor(21, 21, 24, 225))
                p.drawRoundedRect(hud_rect, 3.5, 3.5)
                p.setPen(hud_col)
                p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
                p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_text)

        p.restore()

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)


# =====================================================================
# 4. 旋钮参数单元（标签 + 旋钮 + 上下限提示 + 可编辑数值胶囊）
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
        bipolar: bool = False,
        neg_color: str = "#F0A848",
        pos_color: str = "#F26D6D",
        pill_width: int = 56,
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
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(2)

        self.title_lbl = QLabel(label)
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        lay.addWidget(self.title_lbl)

        self.knob = ProRotaryKnob(
            min_val, max_val, default_val, default_val, accent_color,
            bipolar=bipolar, neg_color=neg_color, pos_color=pos_color
        )
        self.knob.valueChanged.connect(self._on_knob_changed)
        lay.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(2, 0, 2, 0)
        hints_row.setSpacing(1)
        for h_text, align in (
            (min_hint, Qt.AlignmentFlag.AlignLeft),
            (mid_hint, Qt.AlignmentFlag.AlignCenter),
            (max_hint, Qt.AlignmentFlag.AlignRight),
        ):
            hl = QLabel(h_text)
            hl.setAlignment(align)
            hl.setStyleSheet("color: #787882; font-size: 8.5px;")
            hints_row.addWidget(hl, 1)
        lay.addLayout(hints_row)

        self.pill = EditableValuePill(formatter(default_val), accent_color, width=pill_width)
        self.pill.valueEdited.connect(self._on_pill_edited)
        lay.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignCenter)

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
# 5. 底部单频段谐波激励卡片 (ExciterBandCard)
# =====================================================================
class ExciterBandCard(QFrame):
    bandChanged = pyqtSignal(int, dict)
    crossoverEdited = pyqtSignal(int, float)
    selected = pyqtSignal(int)

    def __init__(self, band_index: int, data: dict, parent=None):
        super().__init__(parent)
        self._idx = band_index
        self._accent = BAND_PALETTE[band_index]
        self._data = copy.deepcopy(data)
        self._updating = False
        self._selected = False

        self.setObjectName("BandCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 1. Header（电源开关 + 频段名 + 谐波模型选择 + Solo/Mute）
        self.header = QWidget()
        self.header.setFixedHeight(36)
        h_lay = QHBoxLayout(self.header)
        h_lay.setContentsMargins(8, 6, 8, 5)
        h_lay.setSpacing(6)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel(f"BAND {band_index + 1}")
        h_lay.addWidget(self.title_lbl)

        self.range_lbl = QLabel("4.8k–20kHz")
        self.range_lbl.setStyleSheet(f"color: #8A8A94; font-family: {MONO_FONT_CSS}; font-size: 9.5px;")
        h_lay.addWidget(self.range_lbl)

        h_lay.addStretch()

        # 谐波发生算法模型选择
        self.mode_combo = ChevronComboBox()
        self.mode_combo.setFixedWidth(90)
        self.mode_combo.setFixedHeight(24)
        self.mode_combo.addItems(["Aural", "Tube", "Tape", "Triode"])
        self.mode_combo.setToolTip("选择谐波生成拓扑：Aural(经典高频激励) / Tube(偶次电子管) / Tape(磁带饱和) / Triode(三极管)")
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        h_lay.addWidget(self.mode_combo)

        self.btn_solo = QPushButton("S")
        self.btn_solo.setCheckable(True)
        self.btn_solo.setFixedSize(21, 20)
        self.btn_solo.setToolTip("Solo 监听当前频段激励效果")
        self.btn_solo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_solo.clicked.connect(self._on_solo)
        h_lay.addWidget(self.btn_solo)

        self.btn_mute = QPushButton("M")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setFixedSize(21, 20)
        self.btn_mute.setToolTip("静音当前频段")
        self.btn_mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mute.clicked.connect(self._on_mute)
        h_lay.addWidget(self.btn_mute)

        root.addWidget(self.header)

        # 2. Body：核心四大参数旋钮（量 Amount / 奇偶阶次 Odd-Even / 泛音锐度 Bias / 干湿比 Mix）+ Output
        body = QFrame()
        body.setObjectName("BandBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(6, 8, 6, 8)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_amount = KnobParameterCell(
            "Amount", 0.0, 100.0, 35.0, self._accent,
            "0", "%", "100",
            lambda v: f"{v:.1f}%", lambda s, fb: float(s)
        )
        self.c_harmonics = KnobParameterCell(
            "Odd / Even", -100.0, 100.0, 0.0, self._accent,
            "Even", "Bal", "Odd",
            format_harmonics, parse_harmonics,
            bipolar=True, neg_color=COLORS["even"], pos_color=COLORS["odd"]
        )
        self.c_bias = KnobParameterCell(
            "Harmonics", 0.0, 100.0, 35.0, self._accent,
            "2nd", "Order", "5th+",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s)
        )
        for c in (self.c_amount, self.c_harmonics, self.c_bias):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_mix = KnobParameterCell(
            "Band Mix", 0.0, 100.0, 65.0, self._accent,
            "Dry", "%", "Wet",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s)
        )
        self.c_output = KnobParameterCell(
            "Trim", -12.0, 12.0, 0.0, self._accent,
            "-12", "dB", "+12",
            format_db_signed, lambda s, fb: float(s)
        )
        row2.addWidget(self.c_mix, 1)
        row2.addWidget(self.c_output, 1)

        # 右下角放置分频点快捷微调控件，保持 3 列网格完美对称
        xover_box = QVBoxLayout()
        xover_box.setContentsMargins(2, 2, 2, 2)
        xover_box.setSpacing(4)
        self.xover_lbl = QLabel("Split Freq")
        self.xover_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.xover_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        xover_box.addWidget(self.xover_lbl)

        xover_box.addStretch(1)
        self.xover_pill = EditableValuePill("4800 Hz", self._accent, width=66)
        self.xover_pill.valueEdited.connect(self._on_xover_pill_edited)
        xover_box.addWidget(self.xover_pill, 0, Qt.AlignmentFlag.AlignCenter)

        self.xover_hint = QLabel("Crossover")
        self.xover_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.xover_hint.setStyleSheet("color: #787882; font-size: 8.5px;")
        xover_box.addWidget(self.xover_hint)
        row2.addLayout(xover_box, 1)

        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        # 信号绑定
        self.c_amount.valueChanged.connect(lambda v: self._set_field("amount", round(v, 1)))
        self.c_harmonics.valueChanged.connect(lambda v: self._set_field("harmonics", round(v, 1)))
        self.c_bias.valueChanged.connect(lambda v: self._set_field("bias", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))
        self.c_output.valueChanged.connect(lambda v: self._set_field("output", round(v, 1)))

        self.syncFromData(self._data, 4800.0, 20000.0, has_upper_xover=False)

    def mousePressEvent(self, event):
        self.selected.emit(self._idx)
        super().mousePressEvent(event)

    def setSelected(self, selected: bool):
        if self._selected != selected:
            self._selected = selected
            self._apply_card_style()

    def _apply_card_style(self):
        top_border = f"2px solid {self._accent}" if self._selected else "1px solid #141416"
        self.setStyleSheet(f"""
            QFrame#BandCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: {top_border};
                border-radius: 0px;
            }}
            QFrame#BandBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)

    def syncFromData(self, data: dict, low_hz: float, high_hz: float, has_upper_xover: bool):
        self._updating = True
        self._data = copy.deepcopy(data)
        self._apply_card_style()

        en = bool(self._data["enabled"]) and (not bool(self._data["mute"]))
        self.power_sw.setChecked(bool(self._data["enabled"]))
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 10.5px; font-weight: 700; letter-spacing: 0.3px;"
        )
        self.range_lbl.setText(f"{format_freq(low_hz)}–{format_freq(high_hz)}Hz")

        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentText(self._data["mode"])
        self.mode_combo.blockSignals(False)

        self.btn_solo.setChecked(bool(self._data["solo"]))
        self.btn_mute.setChecked(bool(self._data["mute"]))

        self.btn_solo.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #3A2E18;
                color: #F0A848;
                border: 1px solid #F0A848;
            }
        """)
        self.btn_mute.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #381C1E;
                color: #F26D6D;
                border: 1px solid #F26D6D;
            }
        """)

        self.c_amount.setValue(self._data["amount"])
        self.c_harmonics.setValue(self._data["harmonics"])
        self.c_bias.setValue(self._data["bias"])
        self.c_mix.setValue(self._data["mix"])
        self.c_output.setValue(self._data["output"])

        for cell in (self.c_amount, self.c_harmonics, self.c_bias, self.c_mix, self.c_output):
            cell.setModuleActive(en)

        if has_upper_xover:
            self.xover_lbl.setText(f"Split {self._idx + 1}")
            self.xover_pill.setText(f"{int(round(high_hz))} Hz")
            self.xover_pill.setModuleActive(True)
            self.xover_pill.setEnabled(True)
            self.xover_hint.setText("Upper Freq")
        else:
            self.xover_lbl.setText("Top Ceiling")
            self.xover_pill.setText("20.0 kHz")
            self.xover_pill.setModuleActive(False)
            self.xover_pill.setEnabled(False)
            self.xover_hint.setText("Nyquist Air")

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.bandChanged.emit(self._idx, copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_mode_changed(self, mode: str):
        self._data["mode"] = mode
        self._emit()

    def _on_solo(self, checked: bool):
        self._data["solo"] = checked
        self._emit()

    def _on_mute(self, checked: bool):
        self._data["mute"] = checked
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        self._emit()

    def _on_xover_pill_edited(self, text: str):
        hz = parse_freq(text, 2500.0)
        self.crossoverEdited.emit(self._idx, hz)


# =====================================================================
# 6. 主面板：谐波激励器 (Studio Harmonic Exciter)
# =====================================================================
class StudioExciterWidget(QWidget):
    """
    高频谐波激励器主界面（支持独立模式与饱和器子模块折叠模式），提供完整信号接口供后端 DSP 引擎订阅：
      - bypassChanged(bool)
      - uiModeChanged(str)             # "Standalone" | "Sub-Module"
      - bandCountChanged(int)          # 1 | 2 | 3
      - crossoversChanged(list)        # [hz, ...]
      - oversamplingChanged(str)       # "1x Off" | "2x" | "4x HQ" | "8x Ultra"
      - bandChanged(int, dict)         # (band_index, band_params)
      - soloChanged(list)              # 当前 Solo 监听的频段索引列表
      - globalMixChanged(float)        # 全局总干湿比 (0~100%)
      - outputGainChanged(float)       # 输出增益 (-12~+12 dB)
      - autoGainChanged(bool)          # 自动响度补偿开关
      - stateChanged(dict)             # 完整参数快照
    """
    bypassChanged = pyqtSignal(bool)
    uiModeChanged = pyqtSignal(str)
    bandCountChanged = pyqtSignal(int)
    crossoversChanged = pyqtSignal(list)
    oversamplingChanged = pyqtSignal(str)
    bandChanged = pyqtSignal(int, dict)
    soloChanged = pyqtSignal(list)
    globalMixChanged = pyqtSignal(float)
    outputGainChanged = pyqtSignal(float)
    autoGainChanged = pyqtSignal(bool)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Vocal Lead", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Harmonic Exciter — {track_name}")
        self.resize(1040, 680)
        self.setMinimumSize(880, 340)

        self._active_bank = "A"
        self._selected_band = 2
        self._banks = {
            "A": default_exciter_demo(),
            "B": default_exciter_flat(),
        }

        self._presets = {
            # --- 1. 基础与经典参考 ---
            "Vocal Air & Presence (3-Band)": copy.deepcopy(self._banks["A"]),
            "Flat Bypass": copy.deepcopy(self._banks["B"]),
            "Aphex Classic Top Shine (1-Band)": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 1,
                "crossovers": [3200.0, 8000.0],
                "oversampling": "4x HQ",
                "global_mix": 55.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Aural", amount=64.0, harmonics=25.0, bias=60.0, mix=100.0, output=0.0),
                    _make_exciter_band(False),
                    _make_exciter_band(False),
                ],
            },

            # --- 2. 人声与对白处理 (Vocal & Dialogue) ---
            "Vocal Silk & De-Mud (3-Band)": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 3,
                "crossovers": [650.0, 5200.0],
                "oversampling": "4x HQ",
                "global_mix": 80.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Tube", amount=18.0, harmonics=-60.0, bias=15.0, mix=35.0, output=-0.5),
                    _make_exciter_band(True, mode="Triode", amount=38.0, harmonics=10.0, bias=30.0, mix=55.0, output=0.5),
                    _make_exciter_band(True, mode="Aural", amount=56.0, harmonics=20.0, bias=55.0, mix=75.0, output=1.0),
                ],
            },
            "Broadcast & Podcast Clarity (2-Band)": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 2,
                "crossovers": [1800.0, 8000.0],
                "oversampling": "4x HQ",
                "global_mix": 75.0,
                "output_gain": 0.5,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Tube", amount=24.0, harmonics=-40.0, bias=20.0, mix=45.0, output=0.0),
                    _make_exciter_band(True, mode="Aural", amount=52.0, harmonics=30.0, bias=45.0, mix=70.0, output=0.5),
                    _make_exciter_band(False),
                ],
            },
            "Intimate Whisper & Breath (Air)": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 2,
                "crossovers": [6800.0, 12000.0],
                "oversampling": "8x Ultra",
                "global_mix": 85.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(False),
                    _make_exciter_band(True, mode="Aural", amount=72.0, harmonics=15.0, bias=70.0, mix=85.0, output=1.5),
                    _make_exciter_band(False),
                ],
            },

            # --- 3. 暗沉素材拯救 (Dull / Muffled Rescue) ---
            "Dull Source Rescue (Heavy Opener)": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 3,
                "crossovers": [1100.0, 4200.0],
                "oversampling": "8x Ultra",
                "global_mix": 90.0,
                "output_gain": 0.5,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Triode", amount=30.0, harmonics=-20.0, bias=25.0, mix=50.0, output=0.0),
                    _make_exciter_band(True, mode="Aural", amount=65.0, harmonics=35.0, bias=55.0, mix=80.0, output=1.0),
                    _make_exciter_band(True, mode="Aural", amount=82.0, harmonics=45.0, bias=75.0, mix=90.0, output=1.5),
                ],
            },
            "Dull Acoustic Guitar Opener": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 2,
                "crossovers": [2400.0, 8000.0],
                "oversampling": "4x HQ",
                "global_mix": 80.0,
                "output_gain": 0.5,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Triode", amount=25.0, harmonics=-35.0, bias=25.0, mix=45.0, output=0.0),
                    _make_exciter_band(True, mode="Aural", amount=68.0, harmonics=35.0, bias=65.0, mix=85.0, output=0.5),
                    _make_exciter_band(False),
                ],
            },
            "Dark Room Overhead / Piano Sparkle": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 3,
                "crossovers": [500.0, 3800.0],
                "oversampling": "4x HQ",
                "global_mix": 75.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Tube", amount=22.0, harmonics=-55.0, bias=18.0, mix=40.0, output=0.0),
                    _make_exciter_band(True, mode="Tape", amount=35.0, harmonics=-10.0, bias=30.0, mix=55.0, output=0.0),
                    _make_exciter_band(True, mode="Aural", amount=62.0, harmonics=28.0, bias=60.0, mix=80.0, output=1.0),
                ],
            },

            # --- 4. 鼓组与低频谐波塑形 (Drums & Bass Translation) ---
            "Drum Bus Punch & Cymbal Sheen": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 3,
                "crossovers": [280.0, 4500.0],
                "oversampling": "4x HQ",
                "global_mix": 65.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Tape", amount=45.0, harmonics=40.0, bias=30.0, mix=60.0, output=0.5),
                    _make_exciter_band(True, mode="Triode", amount=32.0, harmonics=25.0, bias=35.0, mix=45.0, output=0.0),
                    _make_exciter_band(True, mode="Aural", amount=50.0, harmonics=15.0, bias=55.0, mix=70.0, output=0.5),
                ],
            },
            "808 & Sub-Bass Phone Translation": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 2,
                "crossovers": [260.0, 2000.0],
                "oversampling": "4x HQ",
                "global_mix": 85.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Triode", amount=68.0, harmonics=55.0, bias=65.0, mix=80.0, output=0.5),
                    _make_exciter_band(True, mode="Tape", amount=28.0, harmonics=-20.0, bias=25.0, mix=40.0, output=0.0),
                    _make_exciter_band(False),
                ],
            },
            "Snare Snap & Hi-Hat Crisp": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 2,
                "crossovers": [3000.0, 8000.0],
                "oversampling": "4x HQ",
                "global_mix": 70.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Tape", amount=36.0, harmonics=30.0, bias=35.0, mix=50.0, output=0.0),
                    _make_exciter_band(True, mode="Aural", amount=60.0, harmonics=40.0, bias=60.0, mix=75.0, output=0.5),
                    _make_exciter_band(False),
                ],
            },

            # --- 5. 合成器、弦乐与母带总线 (Synths, Strings & Mastering) ---
            "Mix Bus Silk & Warmth (3-Band)": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 3,
                "crossovers": [450.0, 5500.0],
                "oversampling": "8x Ultra",
                "global_mix": 70.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Tube", amount=28.0, harmonics=-65.0, bias=20.0, mix=50.0, output=0.0),
                    _make_exciter_band(True, mode="Tape", amount=32.0, harmonics=-15.0, bias=30.0, mix=55.0, output=0.0),
                    _make_exciter_band(True, mode="Aural", amount=48.0, harmonics=20.0, bias=50.0, mix=75.0, output=0.5),
                ],
            },
            "Mastering Subtle Air Sheen (Gentle)": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 3,
                "crossovers": [320.0, 7500.0],
                "oversampling": "8x Ultra",
                "global_mix": 45.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Tube", amount=15.0, harmonics=-75.0, bias=12.0, mix=35.0, output=0.0),
                    _make_exciter_band(True, mode="Tube", amount=18.0, harmonics=-45.0, bias=18.0, mix=40.0, output=0.0),
                    _make_exciter_band(True, mode="Aural", amount=34.0, harmonics=10.0, bias=45.0, mix=60.0, output=0.3),
                ],
            },
            "Analog Tape Console Glue (3-Band)": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 3,
                "crossovers": [380.0, 3600.0],
                "oversampling": "4x HQ",
                "global_mix": 75.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Tape", amount=40.0, harmonics=35.0, bias=25.0, mix=65.0, output=0.0),
                    _make_exciter_band(True, mode="Tape", amount=34.0, harmonics=25.0, bias=30.0, mix=60.0, output=0.0),
                    _make_exciter_band(True, mode="Tape", amount=42.0, harmonics=15.0, bias=40.0, mix=65.0, output=0.5),
                ],
            },
            "Synth Pad Wide Shimmer & Richness": {
                "enabled": True,
                "ui_mode": "Standalone",
                "band_count": 3,
                "crossovers": [550.0, 4200.0],
                "oversampling": "4x HQ",
                "global_mix": 80.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "bands": [
                    _make_exciter_band(True, mode="Tube", amount=38.0, harmonics=-70.0, bias=25.0, mix=60.0, output=0.0),
                    _make_exciter_band(True, mode="Triode", amount=44.0, harmonics=-25.0, bias=40.0, mix=65.0, output=0.0),
                    _make_exciter_band(True, mode="Aural", amount=66.0, harmonics=30.0, bias=68.0, mix=85.0, output=0.8),
                ],
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 演示动态模拟定时器（接入真实后端 update_meters 后自动停止）
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
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ProVerticalMeter, OutputFaderStrip, ProExciterGraphWidget {{
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
        # 1. 顶部整合控制栏
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

        title_lbl = QLabel(f"HARMONIC EXCITER  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(8)

        # 预设管理
        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存当前激励器配置为预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(190)
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

        # 1-3 频段拓扑选择器（1段经典 Aphex / 2段 / 3段 Ozone）
        bands_lbl = QLabel("Bands:")
        bands_lbl.setStyleSheet("color: #8A8A94; font-size: 10px; font-weight: 600;")
        tb_lay.addWidget(bands_lbl)

        self._band_count_btns: dict[int, QPushButton] = {}
        bc_wrap = QFrame()
        bc_wrap.setStyleSheet("background-color: #18181B; border: 1px solid #121214; border-radius: 3px;")
        bc_lay = QHBoxLayout(bc_wrap)
        bc_lay.setContentsMargins(2, 2, 2, 2)
        bc_lay.setSpacing(2)
        for cnt in (1, 2, 3):
            btn = QPushButton(str(cnt))
            btn.setFixedSize(22, 18)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, c=cnt: self._on_band_count_changed(c))
            bc_lay.addWidget(btn)
            self._band_count_btns[cnt] = btn
        tb_lay.addWidget(bc_wrap)

        tb_lay.addSpacing(8)

        # 过采样抗混叠选择（非线性谐波生成核心保护）
        os_lbl = QLabel("Oversample:")
        os_lbl.setStyleSheet("color: #8A8A94; font-size: 10px; font-weight: 600;")
        tb_lay.addWidget(os_lbl)

        self.os_combo = ChevronComboBox()
        self.os_combo.setFixedWidth(100)
        self.os_combo.addItems(["1x Off", "2x", "4x HQ", "8x Ultra"])
        self.os_combo.setToolTip("非线性谐波抗混叠过采样倍率（抑制高频镜像混叠）")
        self.os_combo.currentTextChanged.connect(self._on_os_changed)
        tb_lay.addWidget(self.os_combo)

        # 自动增益补偿按钮
        self.btn_auto_gain = QPushButton("Auto Gain")
        self.btn_auto_gain.setCheckable(True)
        self.btn_auto_gain.setFixedSize(64, 22)
        self.btn_auto_gain.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_gain.setToolTip("自动匹配谐波激励前后的主观响度，防止大音量误判")
        self.btn_auto_gain.clicked.connect(self._on_auto_gain_toggled)
        tb_lay.addWidget(self.btn_auto_gain)

        tb_lay.addStretch()

        # 独立模式 / 饱和器子模块折叠切换
        self.btn_sub_mode = QPushButton("Sub-Module")
        self.btn_sub_mode.setCheckable(True)
        self.btn_sub_mode.setFixedSize(76, 22)
        self.btn_sub_mode.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sub_mode.setToolTip("切换为紧凑子模块视图（适合作为 Saturator 饱和器的高频激励子模块嵌入）")
        self.btn_sub_mode.clicked.connect(self._on_ui_mode_toggled)
        tb_lay.addWidget(self.btn_sub_mode)

        tb_lay.addSpacing(6)

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

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_exciter)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：Input | 谐波频谱画布 | 谐波激励能量表 | 全局 Mix | Output | Out Gain
        # -------------------------------------------------------------
        self.upper_panel = QFrame()
        up_lay = QHBoxLayout(self.upper_panel)
        up_lay.setContentsMargins(0, 0, 0, 0)
        up_lay.setSpacing(0)

        def _vsep() -> QFrame:
            f = QFrame()
            f.setFixedWidth(1)
            f.setStyleSheet("background-color: #141416; border: none;")
            return f

        # 2.1 Input 电平表
        in_col = self._build_meter_column("Input")
        self.meter_input = ProVerticalMeter(mode="level", show_ticks=True)
        in_col.addWidget(self.meter_input, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(in_col)
        up_lay.addWidget(_vsep())

        # 2.2 高频谐波频谱与包络画布
        self.exciter_graph = ProExciterGraphWidget()
        self.exciter_graph.crossoverDragged.connect(self._on_graph_xover_dragged)
        self.exciter_graph.bandAmountDragged.connect(self._on_graph_amount_dragged)
        self.exciter_graph.bandSelected.connect(self._on_band_selected)
        self.exciter_graph.bandSoloToggled.connect(self._on_graph_solo_toggled)
        self.exciter_graph.bandMuteToggled.connect(self._on_graph_mute_toggled)
        up_lay.addWidget(self.exciter_graph, 1)
        up_lay.addWidget(_vsep())

        # 2.3 各频段谐波发生能量表 (Harmonics %)
        harm_col = self._build_meter_column("Harmonics")
        harm_row = QHBoxLayout()
        harm_row.setContentsMargins(4, 0, 4, 0)
        harm_row.setSpacing(4)
        self.band_harm_meters: list[ProVerticalMeter] = []
        for i in range(3):
            m = ProVerticalMeter(
                title=f"B{i + 1}",
                mode="harmonic",
                min_val=0.0,
                max_val=100.0,
                show_ticks=(i == 0),
                accent_color=BAND_PALETTE[i],
            )
            self.band_harm_meters.append(m)
            harm_row.addWidget(m)
        harm_col.addLayout(harm_row, 1)
        up_lay.addLayout(harm_col)
        up_lay.addWidget(_vsep())

        # 2.4 全局干湿比 Master Mix 控制列
        mix_col = self._build_meter_column("Global Mix")
        mix_col.addStretch(1)
        self.c_global_mix = KnobParameterCell(
            "Dry / Wet", 0.0, 100.0, 100.0, COLORS["cyan"],
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), pill_width=54
        )
        self.c_global_mix.valueChanged.connect(self._on_global_mix_changed)
        mix_col.addWidget(self.c_global_mix, 0, Qt.AlignmentFlag.AlignCenter)
        mix_col.addStretch(1)
        up_lay.addLayout(mix_col)
        up_lay.addWidget(_vsep())

        # 2.5 Output 电平表
        out_col = self._build_meter_column("Output")
        self.meter_output = ProVerticalMeter(mode="level", show_ticks=True)
        out_col.addWidget(self.meter_output, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)
        up_lay.addWidget(_vsep())

        # 2.6 Output 增益推子
        og_col = self._build_meter_column("Out Gain")
        self.out_fader = OutputFaderStrip()
        self.out_fader.gainChanged.connect(self._on_output_gain_changed)
        og_col.addWidget(self.out_fader, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(og_col)

        main_lay.addWidget(self.upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部 1–3 频段独立谐波激励卡片矩阵
        # -------------------------------------------------------------
        cards_wrap = QWidget()
        cards_lay = QHBoxLayout(cards_wrap)
        cards_lay.setContentsMargins(0, 0, 0, 0)
        cards_lay.setSpacing(0)

        self.band_cards: list[ExciterBandCard] = []
        st = self._current_state()
        for i in range(3):
            card = ExciterBandCard(i, st["bands"][i])
            card.bandChanged.connect(self._on_band_card_changed)
            card.crossoverEdited.connect(self._on_card_xover_edited)
            card.selected.connect(self._on_band_selected)
            self.band_cards.append(card)
            cards_lay.addWidget(card, 1)

        main_lay.addWidget(cards_wrap, 4)

    def _build_meter_column(self, header_text: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(4, 4, 4, 2)
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

    def _update_top_bar_styles(self):
        st = self._current_state()
        cnt = st["band_count"]

        for c, btn in self._band_count_btns.items():
            if c == cnt:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #182E38;
                        color: #28D8F0;
                        border: 1px solid #24788C;
                        border-radius: 2.5px;
                        font-size: 10px;
                        font-weight: 700;
                    }
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: transparent;
                        color: #9A9AA2;
                        border: none;
                        font-size: 10px;
                        font-weight: 600;
                    }
                    QPushButton:hover { color: #FFFFFF; }
                """)

        toggle_btn_css = """
            QPushButton {
                background-color: #323238;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
            }
        """
        self.btn_auto_gain.setChecked(bool(st["auto_gain"]))
        self.btn_auto_gain.setStyleSheet(toggle_btn_css)

        is_sub = (st["ui_mode"] == "Sub-Module")
        self.btn_sub_mode.setChecked(is_sub)
        self.btn_sub_mode.setStyleSheet(toggle_btn_css)
        self.upper_panel.setVisible(not is_sub)

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

    def _refresh_all_views(self):
        st = self._current_state()
        cnt = st["band_count"]
        xovers = st["crossovers"]

        self.power_switch.setChecked(st["enabled"])
        self.os_combo.blockSignals(True)
        self.os_combo.setCurrentText(st["oversampling"])
        self.os_combo.blockSignals(False)

        self.c_global_mix.setValue(st["global_mix"])
        self.c_global_mix.setModuleActive(st["enabled"])
        self.out_fader.setGain(st["output_gain"])
        self._update_top_bar_styles()

        edges = [20.0] + xovers[: cnt - 1] + [20000.0]
        any_solo = any(st["bands"][i]["solo"] for i in range(cnt))

        for i in range(3):
            visible = (i < cnt)
            self.band_cards[i].setVisible(visible)
            self.band_harm_meters[i].setVisible(visible)
            if visible:
                b = st["bands"][i]
                audible = st["enabled"] and (not b["mute"]) and (not any_solo or b["solo"])
                self.band_harm_meters[i].setActive(audible and b["enabled"])
                self.band_cards[i].syncFromData(
                    b,
                    low_hz=edges[i],
                    high_hz=edges[i + 1],
                    has_upper_xover=(i < cnt - 1),
                )
                self.band_cards[i].setSelected(i == self._selected_band)

        self.exciter_graph.setSelectedBand(self._selected_band)
        self.exciter_graph.setState(st)

    def _emit_solo_list(self):
        st = self._current_state()
        cnt = st["band_count"]
        solos = [i for i in range(cnt) if st["bands"][i]["solo"]]
        self.soloChanged.emit(solos)

    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self._emit_solo_list()
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

    def _on_ui_mode_toggled(self, is_sub: bool):
        st = self._current_state()
        mode = "Sub-Module" if is_sub else "Standalone"
        st["ui_mode"] = mode
        self._update_top_bar_styles()
        if is_sub:
            self.resize(self.width(), 345)
        else:
            self.resize(self.width(), 680)
        self.uiModeChanged.emit(mode)
        self.stateChanged.emit(self.get_state())

    def _on_band_count_changed(self, count: int):
        st = self._current_state()
        if st["band_count"] != count:
            st["band_count"] = count
            if self._selected_band >= count:
                self._selected_band = count - 1
            self._refresh_all_views()
            self.bandCountChanged.emit(count)
            self._emit_solo_list()
            self.stateChanged.emit(self.get_state())

    def _on_os_changed(self, os_mode: str):
        st = self._current_state()
        st["oversampling"] = os_mode
        self.oversamplingChanged.emit(os_mode)
        self.stateChanged.emit(self.get_state())

    def _on_auto_gain_toggled(self, checked: bool):
        st = self._current_state()
        st["auto_gain"] = checked
        self._update_top_bar_styles()
        self.autoGainChanged.emit(checked)
        self.stateChanged.emit(self.get_state())

    def _on_global_mix_changed(self, mix: float):
        st = self._current_state()
        st["global_mix"] = round(mix, 1)
        self.globalMixChanged.emit(st["global_mix"])
        self.stateChanged.emit(self.get_state())

    def _on_output_gain_changed(self, gain: float):
        st = self._current_state()
        st["output_gain"] = gain
        self.outputGainChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _on_band_selected(self, idx: int):
        self._selected_band = idx
        for i, card in enumerate(self.band_cards):
            card.setSelected(i == idx)
        self.exciter_graph.setSelectedBand(idx)

    def _on_band_card_changed(self, idx: int, band_data: dict):
        st = self._current_state()
        st["bands"][idx] = band_data
        self._refresh_all_views()
        self.bandChanged.emit(idx, copy.deepcopy(band_data))
        self._emit_solo_list()
        self.stateChanged.emit(self.get_state())

    def _on_graph_amount_dragged(self, idx: int, amt: float):
        st = self._current_state()
        st["bands"][idx]["amount"] = amt
        self._refresh_all_views()
        self.bandChanged.emit(idx, copy.deepcopy(st["bands"][idx]))
        self.stateChanged.emit(self.get_state())

    def _on_graph_solo_toggled(self, idx: int):
        st = self._current_state()
        st["bands"][idx]["solo"] = not st["bands"][idx]["solo"]
        self._refresh_all_views()
        self.bandChanged.emit(idx, copy.deepcopy(st["bands"][idx]))
        self._emit_solo_list()
        self.stateChanged.emit(self.get_state())

    def _on_graph_mute_toggled(self, idx: int):
        st = self._current_state()
        st["bands"][idx]["mute"] = not st["bands"][idx]["mute"]
        self._refresh_all_views()
        self.bandChanged.emit(idx, copy.deepcopy(st["bands"][idx]))
        self.stateChanged.emit(self.get_state())

    def _on_graph_xover_dragged(self, xover_idx: int, freq_hz: float):
        st = self._current_state()
        st["crossovers"][xover_idx] = freq_hz
        self._refresh_all_views()
        cnt = st["band_count"]
        self.crossoversChanged.emit(list(st["crossovers"][: cnt - 1]))
        self.stateChanged.emit(self.get_state())

    def _on_card_xover_edited(self, xover_idx: int, raw_hz: float):
        st = self._current_state()
        cnt = st["band_count"]
        xovers = st["crossovers"]
        low_bound = 80.0 if xover_idx == 0 else xovers[xover_idx - 1] * 1.25
        high_bound = 16000.0 if xover_idx >= cnt - 2 else xovers[xover_idx + 1] / 1.25
        clamped = max(low_bound, min(high_bound, raw_hz))
        xovers[xover_idx] = round(clamped, 1)
        self._refresh_all_views()
        self.crossoversChanged.emit(list(xovers[: cnt - 1]))
        self.stateChanged.emit(self.get_state())

    def _load_preset(self, name: str):
        if name in self._presets:
            curr_mode = self._current_state()["ui_mode"]
            self._banks[self._active_bank] = copy.deepcopy(self._presets[name])
            self._banks[self._active_bank]["ui_mode"] = curr_mode
            self._selected_band = min(self._selected_band, self._banks[self._active_bank]["band_count"] - 1)
            self._refresh_all_views()
            self._emit_solo_list()
            self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存 Exciter 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 演示动态模拟（接入真实音频引擎后自动停止）
    # -----------------------------------------------------------------
    def _tick_demo_meters(self):
        self._anim_t += 0.08
        st = self._current_state()
        cnt = st["band_count"]
        any_solo = any(st["bands"][i]["solo"] for i in range(cnt))
        edges = [20.0] + st["crossovers"][: cnt - 1] + [20000.0]

        # 生成模拟偏暗原始频谱（高频自然滚降）与激励后的谐波扩展频谱
        dry_dbs = []
        wet_dbs = []
        g_mix = (st["global_mix"] / 100.0) if st["enabled"] else 0.0

        for i in range(64):
            norm = i / 63.0
            hz = 20.0 * (1000.0 ** norm)
            # 模拟偏闷素材：3kHz 以上快速衰减
            base = -14.0 - (norm ** 1.6) * 32.0
            ripple = (
                4.2 * math.sin(self._anim_t * 1.3 + norm * 10.0)
                + 2.8 * math.cos(self._anim_t * 2.2 - norm * 14.0)
                + random.uniform(-1.4, 1.4)
            )
            dry_val = max(-58.0, min(-2.0, base + ripple))
            dry_dbs.append(dry_val)

            # 计算对应频段的谐波泛音抬升
            harm_boost = 0.0
            if st["enabled"]:
                for b_idx in range(cnt):
                    b = st["bands"][b_idx]
                    audible = (not b["mute"]) and (not any_solo or b["solo"])
                    if audible and b["enabled"] and edges[b_idx] <= hz <= edges[b_idx + 1]:
                        b_mix = b["mix"] / 100.0
                        amt = b["amount"] / 100.0
                        bias_fac = 0.6 + 0.6 * (b["bias"] / 100.0)
                        harm_boost = amt * b_mix * g_mix * 15.5 * bias_fac
                        break
            wet_dbs.append(max(-58.0, min(6.0, dry_val + harm_boost)))

        in_db = -15.0 + 7.2 * math.sin(self._anim_t * 1.15) + random.uniform(-1.1, 1.1)
        total_lift = 0.0

        for i in range(cnt):
            b = st["bands"][i]
            audible = st["enabled"] and (not b["mute"]) and (not any_solo or b["solo"])
            if audible and b["enabled"]:
                pulse = 0.72 + 0.28 * math.sin(self._anim_t * (1.4 + i * 0.4))
                thd_pct = b["amount"] * (b["mix"] / 100.0) * g_mix * pulse
                total_lift += thd_pct * 0.03
            else:
                thd_pct = 0.0
            self.band_harm_meters[i].setValue(thd_pct)

        auto_comp = -total_lift * 0.65 if st["auto_gain"] else 0.0
        out_db = in_db + total_lift + auto_comp + (st["output_gain"] if st["enabled"] else 0.0)

        self.meter_input.setValue(in_db)
        self.meter_output.setValue(out_db)
        self.exciter_graph.setRealtimeSpectrum(dry_dbs, wet_dbs)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 饱和器母舰 / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_exciter(self):
        curr_mode = self._current_state()["ui_mode"]
        self._banks[self._active_bank] = default_exciter_flat()
        self._banks[self._active_bank]["ui_mode"] = curr_mode
        self._refresh_all_views()
        self._emit_solo_list()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def set_sub_module_mode(self, enabled: bool):
        """供 Saturator 饱和器主界面嵌入调用：切换紧凑子模块布局"""
        self._on_ui_mode_toggled(enabled)

    def update_meters(
        self,
        input_db: float,
        band_harmonics_pct: list[float],
        output_db: float,
        dry_spectrum_dbs: list[float] | None = None,
        wet_spectrum_dbs: list[float] | None = None,
    ):
        """
        接入真实音频引擎实时数据 (每帧 30~60Hz 调用)：
          - input_db: 输入总电平 (dBFS)
          - band_harmonics_pct: 各频段谐波发生能量百分比 [b1_pct, b2_pct, b3_pct] (0~100%)
          - output_db: 输出总电平 (dBFS)
          - dry_spectrum_dbs: 原始基波对数频谱 (dBFS)
          - wet_spectrum_dbs: 谐波激励后的输出对数频谱 (dBFS)
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()

        self.meter_input.setValue(input_db)
        self.meter_output.setValue(output_db)
        for i in range(min(3, len(band_harmonics_pct))):
            self.band_harm_meters[i].setValue(band_harmonics_pct[i])
        if dry_spectrum_dbs is not None:
            self.exciter_graph.setRealtimeSpectrum(dry_spectrum_dbs, wet_spectrum_dbs)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioExciterWidget("Vocal Lead")
    win.show()
    sys.exit(app.exec())
