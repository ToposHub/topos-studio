from __future__ import annotations

import sys
import math
import copy
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QFrame, QLineEdit, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont
)


# =====================================================================
# 1. 全局色彩系统与默认参数配置（与 Dyamics-UI / multiband_comp_ui 统一）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":    "#28D8F0",  # 主控 / A-B 激活 / 干声与相位指示
    "voice_0": "#6495FA",  # Voice 1 (Double L) 钴蓝
    "voice_1": "#4CD490",  # Voice 2 (Double R) 翡翠绿
    "voice_2": "#F0A848",  # Voice 3 (Harmony +3rd / Wide L) 琥珀金
    "voice_3": "#F26D6D",  # Voice 4 (Harmony +5th / Wide R) 珊瑚红
}

VOICE_PALETTE = [
    COLORS["voice_0"],
    COLORS["voice_1"],
    COLORS["voice_2"],
    COLORS["voice_3"],
]

HARMONY_INTERVALS = [
    "Unison (0)",
    "-1 Oct (-12)",
    "-5th (-7)",
    "-4th (-5)",
    "-3rd (Scale)",
    "+3rd (Scale)",
    "+4th (+5)",
    "+5th (+7)",
    "+6th (Scale)",
    "+1 Oct (+12)",
]

MUSICAL_KEYS = [
    "Chromatic",
    "C Major", "C# Major", "D Major", "Eb Major", "E Major", "F Major",
    "F# Major", "G Major", "Ab Major", "A Major", "Bb Major", "B Major",
    "C Minor", "C# Minor", "D Minor", "Eb Minor", "E Minor", "F Minor",
    "F# Minor", "G Minor", "Ab Minor", "A Minor", "Bb Minor", "B Minor",
]


def _make_voice(
    enabled: bool = True,
    interval: str = "Unison (0)",
    detune: float = 0.0,     # -50.0 ~ +50.0 cents
    delay: float = 12.0,     # 0.0 ~ 100.0 ms
    pan: float = 0.0,        # -100.0 (L100) ~ +100.0 (R100)
    gain: float = -3.0,      # -24.0 ~ +6.0 dB
    solo: bool = False,
    mute: bool = False,
) -> dict:
    return {
        "enabled": enabled,
        "interval": interval,
        "detune": detune,
        "delay": delay,
        "pan": pan,
        "gain": gain,
        "solo": solo,
        "mute": mute,
    }


def default_doubler_demo() -> dict:
    """默认 4 声部配置：经典左右微失谐双倍 + 三度/五度智能和声"""
    return {
        "enabled": True,
        "voice_count": 4,               # 2 | 4
        "key_scale": "C Major",         # 与修音器共享音高/调性引擎
        "formant_preserve": True,       # 共振峰保护（防花栗鼠效应）
        "humanize": 25.0,               # 0 ~ 100 % 动态时差与音高微漂移
        "stereo_width": 100.0,          # 0 ~ 150 % 总声像展开倍率
        "dry_gain": 0.0,                # -60.0 ~ +6.0 dB 直达干声电平
        "wet_mix": 65.0,                # 0 ~ 100 % 效果总混合比
        "output_gain": 0.0,             # -12.0 ~ +12.0 dB 总输出增益
        "voices": [
            _make_voice(True, interval="Unison (0)",   detune=-11.0, delay=14.0, pan=-78.0, gain=-2.5),
            _make_voice(True, interval="Unison (0)",   detune=+11.0, delay=21.0, pan=+78.0, gain=-2.5),
            _make_voice(True, interval="+3rd (Scale)", detune=-5.0,  delay=28.0, pan=-45.0, gain=-6.0),
            _make_voice(True, interval="+5th (+7)",    detune=+5.0,  delay=34.0, pan=+45.0, gain=-7.5),
        ],
    }


def default_doubler_flat() -> dict:
    st = default_doubler_demo()
    st["voice_count"] = 2
    st["humanize"] = 0.0
    st["stereo_width"] = 100.0
    st["dry_gain"] = 0.0
    st["wet_mix"] = 50.0
    st["output_gain"] = 0.0
    for i, v in enumerate(st["voices"]):
        v["enabled"] = False
        v["interval"] = "Unison (0)"
        v["detune"] = 0.0
        v["delay"] = 10.0 + i * 5.0
        v["pan"] = -50.0 if i % 2 == 0 else 50.0
        v["gain"] = -6.0
        v["solo"] = False
        v["mute"] = False
    return st


def format_ms(val: float) -> str:
    if val < 10.0:
        return f"{val:.1f}"
    return f"{int(round(val))}"


def format_cents(val: float) -> str:
    if abs(val) < 0.05:
        return "0.0"
    return f"{val:+.1f}"


def format_pan(val: float) -> str:
    v = int(round(val))
    if v == 0:
        return "C"
    return f"L{abs(v)}" if v < 0 else f"R{v}"


def parse_pan(text: str, fallback: float) -> float:
    s = text.strip().upper()
    if s in ("C", "CENTER", "0"):
        return 0.0
    try:
        if s.startswith("L"):
            return -abs(float(s[1:]))
        if s.startswith("R"):
            return abs(float(s[1:]))
        return float(s)
    except ValueError:
        return fallback


def format_db_signed(val: float) -> str:
    if val <= -59.5:
        return "-inf"
    if abs(val) < 0.05:
        return "0.0"
    return f"{val:+.1f}"


# =====================================================================
# 2. 基础定制控件：电源开关 / 可编辑数字胶囊 / 阳极铝旋钮 / 电平表 / 推子
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

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 54, parent=None):
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
            clean = (
                self._text.replace("dB", "")
                .replace("%", "")
                .replace("ms", "")
                .replace("ct", "")
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
        bipolar: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._mode = mode
        self._bipolar = bipolar
        self._active = True
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(34, 34)
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
            return ratio ** (1.0 / 1.8)
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        n = max(0.0, min(1.0, norm))
        if self._mode == "log":
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return mn * ((mx / mn) ** n)
        if self._mode == "power":
            return self._min + (n ** 1.8) * (self._max - self._min)
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

        arc_r = 14.0
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        p.setPen(QPen(QColor("#141417"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        arc_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(QPen(arc_col, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._bipolar:
            zero_deg = 90.0  # 12点钟方向为双极零点 (Detune=0 / Pan=C)
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        knob_r = 10.2
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.0
        iy1 = cy - math.sin(rad) * 3.0
        ix2 = cx + math.cos(rad) * (knob_r - 1.4)
        iy2 = cy - math.sin(rad) * (knob_r - 1.4)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 1.9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


class ProVerticalMeter(QWidget):
    def __init__(
        self,
        title: str = "",
        min_db: float = -55.0,
        max_db: float = 0.0,
        ticks: list[int] | None = None,
        show_ticks: bool = True,
        accent_color: str = "#28D8F0",
        parent=None,
    ):
        super().__init__(parent)
        self._title = title
        self._min_db = min_db
        self._max_db = max_db
        self._ticks = ticks or [0, -5, -10, -15, -20, -30, -40, -50]
        self._show_ticks = show_ticks
        self._accent = QColor(accent_color)

        self._value_db = min_db
        self._peak_db = min_db
        self._clipped = False
        self._active = True

        self.setFixedWidth(40 if show_ticks else 16)
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
            norm = 0.52 + ((clamped + 20.0) / 20.0) * 0.48
        else:
            norm = ((clamped - self._min_db) / (-20.0 - self._min_db)) * 0.52
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
                grad.setColorAt(0.50, QColor("#4CD490"))
                grad.setColorAt(0.53, QColor("#E8C538"))
                grad.setColorAt(0.75, QColor("#F0A848"))
                grad.setColorAt(0.78, QColor("#F26D6D"))
                grad.setColorAt(1.0,  QColor("#FF4848"))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(grad)
                p.drawRoundedRect(fill_rect, 1.2, 1.2)

            if self._peak_db > self._min_db + 0.5:
                py = self._db_to_y(self._peak_db, bar)
                p.setPen(QPen(QColor("#F26D6D") if self._peak_db > -10.0 else QColor("#F0F2F6"), 1.4))
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

        if self._title:
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.setPen(self._accent if self._active else QColor("#55555C"))
            p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, self._title)


class PhaseCorrelationMeter(QWidget):
    """立体声相位相关度垂直表 (+1 单声道同相 ~ 0 宽广立体声 ~ -1 反相警告)"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._corr = 0.65
        self._active = True
        self.setFixedWidth(40)
        self.setMinimumHeight(180)

    def setCorrelation(self, corr: float):
        self._corr = max(-1.0, min(1.0, corr))
        self.update()

    def setActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 10.0
        bot_pad = 18.0
        bar_w = 9.0
        bar_x = self.width() - bar_w - 3.0
        bar = QRectF(bar_x, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))
        mid_y = bar.center().y()

        p.setFont(QFont(MONO_FONT_FAM, 7))
        for val, lbl in ((1.0, "+1 -"), (0.5, "+.5 -"), (0.0, "0 -"), (-0.5, "-.5 -"), (-1.0, "-1 -")):
            norm = (1.0 - val) / 2.0
            ty = bar.top() + norm * bar.height()
            p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
            p.drawText(QRectF(0, ty - 7, bar.left() - 3, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        if self._active:
            norm = (1.0 - self._corr) / 2.0
            cy = bar.top() + norm * bar.height()
            top_y = min(cy, mid_y)
            bot_y = max(cy, mid_y)
            fill_rect = QRectF(bar.left() + 1.0, top_y, bar.width() - 2.0, max(2.0, bot_y - top_y))
            col = QColor("#4CD490") if self._corr >= 0.0 else QColor("#F26D6D")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(fill_rect, 1.2, 1.2)

            p.setPen(QPen(QColor("#FFFFFF"), 1.4))
            p.drawLine(QPointF(bar.left() + 1.0, cy), QPointF(bar.right() - 1.0, cy))

        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.setPen(QColor("#28D8F0") if self._active else QColor("#55555C"))
        p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, f"{self._corr:+.2f}")


class OutputFaderStrip(QWidget):
    """主输出双向增益推子 (-12.0 ~ +12.0 dB)"""
    gainChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gain = 0.0
        self._dragging = False
        self.setFixedWidth(54)
        self.setMinimumHeight(180)
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
# 3. 控件：交互式立体声场与音高拓扑画布 (Waves Doubler + iZotope Vocal Doubler 融合)
# =====================================================================
class ProDoublerStageWidget(QWidget):
    """
    二维声场舞台 (X: Pan L100~R100, Y: Delay 0~100 ms):
      - 直接拖拽声部圆点：同步调节 Pan 与 Delay
      - 在节点上滑动鼠标滚轮：直接调节微失谐 Detune (cents)
      - 实时李萨如立体声示波轨迹 + 干声中心锚点 + 快捷 Solo/Mute 按钮
    """
    voiceNodeDragged = pyqtSignal(int, float, float)  # voice_idx, pan, delay_ms
    voiceDetuneWheeled = pyqtSignal(int, float)       # voice_idx, detune_cents
    voiceSelected = pyqtSignal(int)
    voiceSoloToggled = pyqtSignal(int)
    voiceMuteToggled = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(430, 230)
        self.setMouseTracking(True)

        self._state = default_doubler_demo()
        self._selected_voice = 0
        self._lissajous_pts: list[tuple[float, float]] = []
        self._env_energy = 0.35

        self._drag_index = -1
        self._hover_mode = ""   # "node" | "solo" | "mute" | ""
        self._hover_index = -1
        self._mouse_pos: QPointF | None = None

        self._solo_rects: list[QRectF] = []
        self._mute_rects: list[QRectF] = []

        self._ml = 36.0
        self._mr = 14.0
        self._mt = 12.0
        self._mb = 22.0

    def setState(self, state: dict):
        self._state = state
        self.update()

    def setSelectedVoice(self, idx: int):
        if self._selected_voice != idx:
            self._selected_voice = idx
            self.update()

    def setRealtimeVisuals(self, lissajous_pts: list[tuple[float, float]], energy: float = 0.5):
        self._lissajous_pts = lissajous_pts
        self._env_energy = max(0.0, min(1.0, energy))
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(120.0, self.width() - self._ml - self._mr),
            max(100.0, self.height() - self._mt - self._mb),
        )

    def _pan_to_x(self, pan: float, plot: QRectF) -> float:
        # 应用总 Stereo Width 展开倍率并限制在舞台边界内
        width_scale = self._state.get("stereo_width", 100.0) / 100.0
        eff_pan = max(-100.0, min(100.0, pan * width_scale))
        norm = (eff_pan + 100.0) / 200.0
        return plot.left() + norm * plot.width()

    def _x_to_pan(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        eff_pan = -100.0 + norm * 200.0
        width_scale = max(0.1, self._state.get("stereo_width", 100.0) / 100.0)
        return max(-100.0, min(100.0, eff_pan / width_scale))

    def _delay_to_y(self, delay_ms: float, plot: QRectF) -> float:
        # 顶部为 0 ms (紧贴直达声)，底部为 100 ms (远景延迟)
        clamped = max(0.0, min(100.0, delay_ms))
        norm = clamped / 100.0
        return plot.top() + norm * plot.height()

    def _y_to_delay(self, y: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (y - plot.top()) / plot.height()))
        return norm * 100.0

    def _node_radius(self, gain_db: float) -> float:
        # Gain (-24 ~ +6 dB) 映射为节点视觉半径 (7.5 ~ 14.5 px)
        norm = max(0.0, min(1.0, (gain_db + 24.0) / 30.0))
        return 7.5 + norm * 7.0

    def _hit_test(self, pos: QPointF, plot: QRectF) -> tuple[str, int]:
        for idx, r in enumerate(self._solo_rects):
            if r.contains(pos):
                return "solo", idx
        for idx, r in enumerate(self._mute_rects):
            if r.contains(pos):
                return "mute", idx

        cnt = self._state["voice_count"]
        voices = self._state["voices"][:cnt]
        # 优先检测选中声部，再逆序检测上层节点
        order = [self._selected_voice] + [i for i in range(cnt - 1, -1, -1) if i != self._selected_voice]
        for i in order:
            if not (0 <= i < cnt):
                continue
            v = voices[i]
            nx = self._pan_to_x(v["pan"], plot)
            ny = self._delay_to_y(v["delay"], plot)
            rad = self._node_radius(v["gain"]) + 5.0
            if math.hypot(pos.x() - nx, pos.y() - ny) <= rad:
                return "node", i

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
                self.voiceSoloToggled.emit(idx)
                return
            if mode == "mute":
                self.voiceMuteToggled.emit(idx)
                return
            if mode == "node":
                self._drag_index = idx
                self.voiceSelected.emit(idx)
                return

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._drag_index >= 0:
            new_pan = round(self._x_to_pan(event.position().x(), plot), 0)
            new_delay = round(self._y_to_delay(event.position().y(), plot), 1)
            self.voiceNodeDragged.emit(self._drag_index, new_pan, new_delay)
            return

        mode, idx = self._hit_test(event.position(), plot)
        self._hover_mode = mode
        self._hover_index = idx
        if mode == "node":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif mode in ("solo", "mute"):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_index = -1
            self.update()

    def wheelEvent(self, event):
        # 在声部节点上方滚动鼠标滚轮，微调该声部的 Detune (cents)
        if self._hover_mode == "node" and self._hover_index >= 0:
            delta = event.angleDelta().y() / 120.0
            step = 0.5 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 2.0
            curr = self._state["voices"][self._hover_index]["detune"]
            new_detune = max(-50.0, min(50.0, round(curr + delta * step, 1)))
            self.voiceDetuneWheeled.emit(self._hover_index, new_detune)
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 画布底色与立体声扇形辐射网格
        p.fillRect(plot, QColor("#1D1D20"))

        p.save()
        p.setClipRect(plot)

        cx = plot.center().x()
        top_y = plot.top()

        # 立体声扇形角度引导线 (L100 / L50 / C / R50 / R100)
        p.setPen(QPen(QColor("#242429"), 1.0, Qt.PenStyle.DashLine))
        for pan_mark in (-100, -50, 50, 100):
            bx = plot.left() + ((pan_mark + 100.0) / 200.0) * plot.width()
            p.drawLine(QPointF(cx, top_y), QPointF(bx, plot.bottom()))

        # 中央单声道基准轴 (Center)
        p.setPen(QPen(QColor("#32323A"), 1.2))
        p.drawLine(QPointF(cx, plot.top()), QPointF(cx, plot.bottom()))

        # 水平延时刻度网格 (0, 20, 40, 60, 80, 100 ms)
        grid_pen = QPen(QColor("#18181C"), 1.0)
        for ms in (20, 40, 60, 80):
            y = self._delay_to_y(float(ms), plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        # 2. iZotope 风格实时李萨如 (Lissajous) 立体声场动态示波光晕
        st = self._state
        master_on = st["enabled"]
        if master_on and len(self._lissajous_pts) >= 2:
            liss_path = QPainterPath()
            scale_x = plot.width() * 0.42
            scale_y = plot.height() * 0.42
            mid_y = plot.center().y()
            for idx, (lx, ly) in enumerate(self._lissajous_pts):
                px = cx + lx * scale_x
                py = mid_y - ly * scale_y
                if idx == 0:
                    liss_path.moveTo(px, py)
                else:
                    liss_path.lineTo(px, py)
            p.setPen(QPen(QColor(40, 216, 240, 38), 3.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(liss_path)
            p.setPen(QPen(QColor(40, 216, 240, 95), 1.1))
            p.drawPath(liss_path)

        # 3. 直达干声 (Direct Dry) 顶部中心锚点
        dry_y = top_y + 12.0
        dry_r = 7.0 + self._env_energy * 4.0 if master_on else 6.0
        if master_on and st["dry_gain"] > -59.0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(40, 216, 240, 32))
            p.drawEllipse(QPointF(cx, dry_y), dry_r + 6.0, dry_r + 6.0)

        p.setPen(QPen(QColor("#28D8F0") if master_on else QColor("#666670"), 1.5))
        p.setBrush(QColor("#151518"))
        p.drawEllipse(QPointF(cx, dry_y), 6.5, 6.5)
        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        p.setPen(QColor("#28D8F0") if master_on else QColor("#787882"))
        p.drawText(QRectF(cx - 24, dry_y + 8, 48, 13), Qt.AlignmentFlag.AlignCenter, "DIRECT")

        # 4. 绘制各声部节点、微失谐扩散波纹与智能和声音程标签
        cnt = st["voice_count"]
        voices = st["voices"][:cnt]
        any_solo = any(v["solo"] for v in voices)

        for i, v in enumerate(voices):
            col = QColor(VOICE_PALETTE[i])
            is_audible = master_on and (not v["mute"]) and (not any_solo or v["solo"])
            is_active = is_audible and v["enabled"]
            is_selected = (i == self._selected_voice)
            is_hi = (self._hover_mode == "node" and self._hover_index == i) or (self._drag_index == i)

            nx = self._pan_to_x(v["pan"], plot)
            ny = self._delay_to_y(v["delay"], plot)
            rad = self._node_radius(v["gain"])

            # 4.1 从 Direct 锚点到声部节点的哈斯效应引导线
            line_col = QColor(col)
            line_col.setAlpha(130 if is_active else 35)
            p.setPen(QPen(line_col, 1.3 if is_selected else 1.0, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(cx, dry_y), QPointF(nx, ny))

            # 4.2 微失谐 (Detune cents) 音高偏移光晕弧
            if is_active and abs(v["detune"]) > 0.2:
                det_norm = min(1.0, abs(v["detune"]) / 50.0)
                halo_r = rad + 4.0 + det_norm * 11.0 + self._env_energy * 3.0
                halo_grad = QRadialGradient(QPointF(nx, ny), halo_r)
                c_inner = QColor(col.red(), col.green(), col.blue(), 85)
                c_outer = QColor(col.red(), col.green(), col.blue(), 0)
                halo_grad.setColorAt(0.2, c_inner)
                halo_grad.setColorAt(1.0, c_outer)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(halo_grad)
                p.drawEllipse(QPointF(nx, ny), halo_r, halo_r)

                # 上下音高偏移方向指示弧 (正 cents 偏上，负 cents 偏下)
                arc_rect = QRectF(nx - rad - 3.5, ny - rad - 3.5, (rad + 3.5) * 2, (rad + 3.5) * 2)
                p.setPen(QPen(col, 1.6))
                start_a = 35 if v["detune"] > 0 else 215
                p.drawArc(arc_rect, start_a * 16, 110 * 16)

            # 4.3 选中高亮外圈
            if is_selected or is_hi:
                p.setPen(QPen(QColor("#FFFFFF") if is_hi else col, 1.2, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(nx, ny), rad + 3.5, rad + 3.5)

            # 4.4 声部实体圆点
            node_col = col if is_active else QColor("#5A5A64")
            p.setPen(QPen(node_col, 2.0))
            p.setBrush(QColor(node_col.red(), node_col.green(), node_col.blue(), 65 if is_active else 25))
            p.drawEllipse(QPointF(nx, ny), rad, rad)

            # 节点内编号 (1~4)
            p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
            p.setPen(QColor("#FFFFFF") if is_active else QColor("#8A8A94"))
            p.drawText(QRectF(nx - rad, ny - rad, rad * 2, rad * 2), Qt.AlignmentFlag.AlignCenter, str(i + 1))

            # 4.5 节点下方紧凑微标签（显示和声音程或微失谐 cents）
            short_iv = v["interval"].split(" ")[0]
            sub_txt = f"{short_iv} · {format_cents(v['detune'])}ct" if short_iv != "Unison" else f"{format_cents(v['detune'])}ct"
            lbl_rect = QRectF(nx - 42, ny + rad + 3.0, 84, 14)
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.setPen(col if is_active else QColor("#6E6E78"))
            p.drawText(lbl_rect, Qt.AlignmentFlag.AlignCenter, sub_txt)

        # 5. 画布右上角快捷声部状态胶囊 (V1~V4 S/M)
        self._solo_rects = []
        self._mute_rects = []
        chip_w = 56.0
        chip_h = 17.0
        start_x = plot.right() - cnt * (chip_w + 4.0) - 4.0
        for i in range(cnt):
            v = voices[i]
            col = QColor(VOICE_PALETTE[i])
            bx = start_x + i * (chip_w + 4.0)
            by = plot.top() + 6.0

            bg_rect = QRectF(bx, by, chip_w, chip_h)
            p.setPen(QPen(col if i == self._selected_voice else QColor("#2E2E35"), 1.0))
            p.setBrush(QColor(21, 21, 24, 215))
            p.drawRoundedRect(bg_rect, 3.0, 3.0)

            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.setPen(col if v["enabled"] else QColor("#6E6E76"))
            p.drawText(QRectF(bx + 4, by, 18, chip_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"V{i + 1}")

            s_rect = QRectF(bx + 22, by + 1.5, 14, 14)
            m_rect = QRectF(bx + 38, by + 1.5, 14, 14)
            self._solo_rects.append(s_rect)
            self._mute_rects.append(m_rect)

            p.setPen(QPen(QColor("#F0A848") if v["solo"] else QColor("#3A3A42"), 1.0))
            p.setBrush(QColor("#3A2E18") if v["solo"] else QColor("#18181C"))
            p.drawRoundedRect(s_rect, 2.0, 2.0)
            p.setPen(QColor("#F0A848") if v["solo"] else QColor("#8E8E98"))
            p.drawText(s_rect, Qt.AlignmentFlag.AlignCenter, "S")

            p.setPen(QPen(QColor("#F26D6D") if v["mute"] else QColor("#3A3A42"), 1.0))
            p.setBrush(QColor("#381C1E") if v["mute"] else QColor("#18181C"))
            p.drawRoundedRect(m_rect, 2.0, 2.0)
            p.setPen(QColor("#F26D6D") if v["mute"] else QColor("#8E8E98"))
            p.drawText(m_rect, Qt.AlignmentFlag.AlignCenter, "M")

        # 6. 拖拽 / 悬停实时参数 HUD
        target_idx = self._drag_index if self._drag_index >= 0 else (self._hover_index if self._hover_mode == "node" else -1)
        if 0 <= target_idx < cnt:
            tv = voices[target_idx]
            hud_str = (
                f"V{target_idx + 1} [{tv['interval'].split(' ')[0]}]  "
                f"Pan:{format_pan(tv['pan'])}  Delay:{tv['delay']:.1f}ms  Detune:{format_cents(tv['detune'])}ct"
            )
            hud_rect = QRectF(plot.left() + 8.0, plot.bottom() - 24.0, 285.0, 18.0)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor(21, 21, 24, 225))
            p.drawRoundedRect(hud_rect, 3.5, 3.5)
            p.setPen(QColor(VOICE_PALETTE[target_idx]))
            p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()

        # 7. 外框与坐标轴标注 (左侧 Delay ms, 底部 Pan L100~R100)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.setFont(QFont(MONO_FONT_FAM, 8))
        for ms in (0, 20, 40, 60, 80, 100):
            y = self._delay_to_y(float(ms), plot)
            p.setPen(QColor("#9A9AA2"))
            p.drawText(QRectF(0, y - 8, plot.left() - 5, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{ms}ms" if ms == 0 else str(ms))

        for pan_v, pan_lbl in ((-100, "L100"), (-50, "L50"), (0, "CENTER"), (50, "R50"), (100, "R100")):
            x = plot.left() + ((pan_v + 100.0) / 200.0) * plot.width()
            p.setPen(QColor("#28D8F0") if pan_v == 0 else QColor("#9A9AA2"))
            p.drawText(QRectF(x - 24, plot.bottom() + 4, 48, 16), Qt.AlignmentFlag.AlignCenter, pan_lbl)


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
        mode: str = "linear",
        bipolar: bool = False,
        pill_width: int = 54,
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
            min_val, max_val, default_val, default_val, accent_color, mode=mode, bipolar=bipolar
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
# 5. 底部单声部与智能和声卡片 (DoublerVoiceCard)
# =====================================================================
class DoublerVoiceCard(QFrame):
    voiceChanged = pyqtSignal(int, dict)
    selected = pyqtSignal(int)

    def __init__(self, voice_index: int, data: dict, parent=None):
        super().__init__(parent)
        self._idx = voice_index
        self._accent = VOICE_PALETTE[voice_index]
        self._data = copy.deepcopy(data)
        self._updating = False
        self._selected = False

        self.setObjectName("VoiceCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 1. Header
        header = QWidget()
        header.setFixedHeight(36)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(8, 6, 8, 5)
        h_lay.setSpacing(6)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel(f"VOICE {voice_index + 1}")
        h_lay.addWidget(self.title_lbl)

        h_lay.addStretch()

        self.btn_solo = QPushButton("S")
        self.btn_solo.setCheckable(True)
        self.btn_solo.setFixedSize(21, 20)
        self.btn_solo.setToolTip("Solo 监听当前声部")
        self.btn_solo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_solo.clicked.connect(self._on_solo)
        h_lay.addWidget(self.btn_solo)

        self.btn_mute = QPushButton("M")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setFixedSize(21, 20)
        self.btn_mute.setToolTip("静音当前声部")
        self.btn_mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mute.clicked.connect(self._on_mute)
        h_lay.addWidget(self.btn_mute)

        root.addWidget(header)

        # 2. Body: 智能和声音程选择条 + 2x2 核心参数旋钮矩阵
        body = QFrame()
        body.setObjectName("VoiceBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 7, 8, 8)
        b_lay.setSpacing(8)

        # 智能和声音程选择行
        harm_row = QHBoxLayout()
        harm_row.setSpacing(6)
        harm_lbl = QLabel("Harmony")
        harm_lbl.setStyleSheet("color: #8A8A94; font-size: 9.5px; font-weight: 600;")
        harm_row.addWidget(harm_lbl)

        self.interval_combo = QComboBox()
        self.interval_combo.setFixedHeight(21)
        self.interval_combo.addItems(HARMONY_INTERVALS)
        self.interval_combo.setToolTip("智能和声音程：跟随顶部 Key/Scale 自动计算三度/五度等自然音阶移调")
        self.interval_combo.currentTextChanged.connect(self._on_interval_changed)
        harm_row.addWidget(self.interval_combo, 1)
        b_lay.addLayout(harm_row)

        # Row 1: Detune (cents) & Delay (ms)
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_detune = KnobParameterCell(
            "Detune", -50.0, 50.0, 0.0, self._accent,
            "-50", "cents", "+50",
            format_cents, lambda s, fb: float(s), mode="linear", bipolar=True
        )
        self.c_delay = KnobParameterCell(
            "Delay", 0.0, 100.0, 12.0, self._accent,
            "0.0", "ms", "100",
            format_ms, lambda s, fb: float(s), mode="power"
        )
        row1.addWidget(self.c_detune, 1)
        row1.addWidget(self.c_delay, 1)
        b_lay.addLayout(row1)

        # Row 2: Pan & Gain (dB)
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_pan = KnobParameterCell(
            "Pan", -100.0, 100.0, 0.0, self._accent,
            "L100", "C", "R100",
            format_pan, parse_pan, mode="linear", bipolar=True
        )
        self.c_gain = KnobParameterCell(
            "Level", -24.0, 6.0, -3.0, self._accent,
            "-24", "dB", "+6.0",
            format_db_signed, lambda s, fb: float(s), mode="linear"
        )
        row2.addWidget(self.c_pan, 1)
        row2.addWidget(self.c_gain, 1)
        b_lay.addLayout(row2)

        root.addWidget(body, 1)

        # 连接信号
        self.c_detune.valueChanged.connect(lambda v: self._set_field("detune", round(v, 1)))
        self.c_delay.valueChanged.connect(lambda v: self._set_field("delay", round(v, 1)))
        self.c_pan.valueChanged.connect(lambda v: self._set_field("pan", round(v, 0)))
        self.c_gain.valueChanged.connect(lambda v: self._set_field("gain", round(v, 1)))

        self.syncFromData(self._data)

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
            QFrame#VoiceCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: {top_border};
                border-radius: 0px;
            }}
            QFrame#VoiceBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        self._apply_card_style()

        en = bool(self._data["enabled"]) and (not bool(self._data["mute"]))
        self.power_sw.setChecked(bool(self._data["enabled"]))
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 10.5px; font-weight: 700; letter-spacing: 0.3px;"
        )

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

        self.interval_combo.blockSignals(True)
        self.interval_combo.setCurrentText(self._data["interval"])
        self.interval_combo.setEnabled(en)
        self.interval_combo.blockSignals(False)

        self.c_detune.setValue(self._data["detune"])
        self.c_delay.setValue(self._data["delay"])
        self.c_pan.setValue(self._data["pan"])
        self.c_gain.setValue(self._data["gain"])

        for cell in (self.c_detune, self.c_delay, self.c_pan, self.c_gain):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.voiceChanged.emit(self._idx, copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_solo(self, checked: bool):
        self._data["solo"] = checked
        self._emit()

    def _on_mute(self, checked: bool):
        self._data["mute"] = checked
        self._emit()

    def _on_interval_changed(self, text: str):
        self._data["interval"] = text
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        self._emit()


# =====================================================================
# 6. 主面板：Doubler / 智能和声器 (Studio Doubler & Harmonizer)
# =====================================================================
class StudioDoublerWidget(QWidget):
    """
    Doubler / 智能和声器主界面，提供完整信号接口供后端 DSP 引擎（与修音器共享基建）订阅：
      - bypassChanged(bool)
      - voiceCountChanged(int)
      - harmonyScaleChanged(str, bool)      # (key_scale, formant_preserve)
      - globalParamsChanged(dict)           # {humanize, stereo_width, dry_gain, wet_mix}
      - voiceChanged(int, dict)             # (voice_index, voice_params)
      - soloChanged(list)                   # 当前处于 Solo 状态的声部索引列表
      - outputGainChanged(float)
      - stateChanged(dict)                  # 完整参数树快照
    """
    bypassChanged = pyqtSignal(bool)
    voiceCountChanged = pyqtSignal(int)
    harmonyScaleChanged = pyqtSignal(str, bool)
    globalParamsChanged = pyqtSignal(dict)
    voiceChanged = pyqtSignal(int, dict)
    soloChanged = pyqtSignal(list)
    outputGainChanged = pyqtSignal(float)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Lead Vocal", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Doubler & Harmonizer — {track_name}")
        self.resize(1120, 720)
        self.setMinimumSize(980, 640)

        self._active_bank = "A"
        self._selected_voice = 0
        self._banks = {
            "A": default_doubler_demo(),
            "B": default_doubler_flat(),
        }

        self._presets = {
            # --- 1. 基础与经典主唱加倍 (Lead Vocal Doubling) ---
            "Vocal Quad + 3rd/5th Harmony": copy.deepcopy(self._banks["A"]),
            "Flat Bypass": copy.deepcopy(self._banks["B"]),
            "Tight Stereo Vocal Doubler (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "Chromatic",
                "formant_preserve": True,
                "humanize": 32.0,
                "stereo_width": 110.0,
                "dry_gain": 0.0,
                "wet_mix": 55.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-9.5,  delay=11.0, pan=-85.0, gain=-2.0),
                    _make_voice(True, interval="Unison (0)", detune=+9.5,  delay=17.5, pan=+85.0, gain=-2.0),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },
            "Subtle Pop Lead Polish (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "Chromatic",
                "formant_preserve": True,
                "humanize": 28.0,
                "stereo_width": 95.0,
                "dry_gain": 0.0,
                "wet_mix": 35.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-5.5,  delay=7.5,  pan=-65.0, gain=-4.5),
                    _make_voice(True, interval="Unison (0)", detune=+5.5,  delay=12.0, pan=+65.0, gain=-4.5),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },
            "Intimate Whisper Double (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "Chromatic",
                "formant_preserve": True,
                "humanize": 45.0,
                "stereo_width": 100.0,
                "dry_gain": 0.0,
                "wet_mix": 48.0,
                "output_gain": 0.5,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-7.0,  delay=15.0, pan=-55.0, gain=-3.0),
                    _make_voice(True, interval="Unison (0)", detune=+7.0,  delay=23.0, pan=+55.0, gain=-3.0),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },
            "Wide Chorus Vocal Wall (4V)": {
                "enabled": True,
                "voice_count": 4,
                "key_scale": "Chromatic",
                "formant_preserve": True,
                "humanize": 42.0,
                "stereo_width": 125.0,
                "dry_gain": -1.0,
                "wet_mix": 75.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-8.0,  delay=10.0, pan=-50.0,  gain=-2.0),
                    _make_voice(True, interval="Unison (0)", detune=+8.0,  delay=15.0, pan=+50.0,  gain=-2.0),
                    _make_voice(True, interval="Unison (0)", detune=-16.5, delay=24.0, pan=-100.0, gain=-3.5),
                    _make_voice(True, interval="Unison (0)", detune=+16.5, delay=31.0, pan=+100.0, gain=-3.5),
                ],
            },
            "R&B / Hip-Hop Thick Hook (4V)": {
                "enabled": True,
                "voice_count": 4,
                "key_scale": "Chromatic",
                "formant_preserve": True,
                "humanize": 35.0,
                "stereo_width": 115.0,
                "dry_gain": 0.0,
                "wet_mix": 68.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)",   detune=-11.0, delay=13.0, pan=-80.0, gain=-2.0),
                    _make_voice(True, interval="Unison (0)",   detune=+11.0, delay=19.0, pan=+80.0, gain=-2.0),
                    _make_voice(True, interval="-1 Oct (-12)", detune=-4.0,  delay=9.0,  pan=-25.0, gain=-7.5),
                    _make_voice(True, interval="-1 Oct (-12)", detune=+4.0,  delay=14.0, pan=+25.0, gain=-7.5),
                ],
            },

            # --- 2. 智能和声编排 (Smart Scale Harmonizer) ---
            "Octave & 3rd Choir Stack (4V)": {
                "enabled": True,
                "voice_count": 4,
                "key_scale": "A Minor",
                "formant_preserve": True,
                "humanize": 40.0,
                "stereo_width": 115.0,
                "dry_gain": 0.0,
                "wet_mix": 72.0,
                "output_gain": -0.5,
                "voices": [
                    _make_voice(True, interval="Unison (0)",   detune=-14.0, delay=16.0, pan=-90.0, gain=-3.0),
                    _make_voice(True, interval="Unison (0)",   detune=+14.0, delay=24.0, pan=+90.0, gain=-3.0),
                    _make_voice(True, interval="+3rd (Scale)", detune=-6.0,  delay=32.0, pan=-55.0, gain=-5.5),
                    _make_voice(True, interval="-1 Oct (-12)", detune=+4.0,  delay=12.0, pan=+20.0, gain=-6.5),
                ],
            },
            "Stereo 3rds Harmony Pair (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "C Major",
                "formant_preserve": True,
                "humanize": 30.0,
                "stereo_width": 105.0,
                "dry_gain": 0.0,
                "wet_mix": 58.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="+3rd (Scale)", detune=-5.0, delay=18.0, pan=-68.0, gain=-3.5),
                    _make_voice(True, interval="-3rd (Scale)", detune=+5.0, delay=24.0, pan=+68.0, gain=-4.0),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },
            "Fifths Power Duet (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "C Major",
                "formant_preserve": True,
                "humanize": 22.0,
                "stereo_width": 100.0,
                "dry_gain": 0.0,
                "wet_mix": 52.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="+5th (+7)", detune=-6.0, delay=15.0, pan=-60.0, gain=-4.5),
                    _make_voice(True, interval="-4th (-5)", detune=+6.0, delay=21.0, pan=+60.0, gain=-5.0),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },
            "Gospel 4-Part Harmony (4V)": {
                "enabled": True,
                "voice_count": 4,
                "key_scale": "F Major",
                "formant_preserve": True,
                "humanize": 48.0,
                "stereo_width": 120.0,
                "dry_gain": -0.5,
                "wet_mix": 78.0,
                "output_gain": -0.5,
                "voices": [
                    _make_voice(True, interval="+3rd (Scale)", detune=-7.0, delay=20.0, pan=-75.0, gain=-3.5),
                    _make_voice(True, interval="+5th (+7)",    detune=+7.0, delay=26.0, pan=+75.0, gain=-4.0),
                    _make_voice(True, interval="-3rd (Scale)", detune=-5.0, delay=33.0, pan=-40.0, gain=-4.5),
                    _make_voice(True, interval="-1 Oct (-12)", detune=+4.0, delay=14.0, pan=+15.0, gain=-5.5),
                ],
            },
            "High Angelic Air Octave (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "C Major",
                "formant_preserve": True,
                "humanize": 25.0,
                "stereo_width": 115.0,
                "dry_gain": 0.0,
                "wet_mix": 42.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="+1 Oct (+12)", detune=-8.0, delay=22.0, pan=-72.0, gain=-6.5),
                    _make_voice(True, interval="+1 Oct (+12)", detune=+8.0, delay=29.0, pan=+72.0, gain=-6.5),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },
            "Deep Sub-Octave Villain (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "Chromatic",
                "formant_preserve": False,
                "humanize": 15.0,
                "stereo_width": 90.0,
                "dry_gain": 0.0,
                "wet_mix": 60.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="-1 Oct (-12)", detune=-9.0, delay=10.0, pan=-45.0, gain=-2.5),
                    _make_voice(True, interval="-1 Oct (-12)", detune=+9.0, delay=16.0, pan=+45.0, gain=-2.5),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },

            # --- 3. 吉他、合成器与贝斯乐器扩展 (Guitar, Synth & Bass) ---
            "Acoustic Guitar Wide Haas (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "Chromatic",
                "formant_preserve": False,
                "humanize": 15.0,
                "stereo_width": 120.0,
                "dry_gain": -1.5,
                "wet_mix": 70.0,
                "output_gain": 0.5,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-6.0, delay=8.0,  pan=-100.0, gain=-1.5),
                    _make_voice(True, interval="Unison (0)", detune=+6.0, delay=19.0, pan=+100.0, gain=-1.5),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },
            "12-String Acoustic Simulator (4V)": {
                "enabled": True,
                "voice_count": 4,
                "key_scale": "Chromatic",
                "formant_preserve": False,
                "humanize": 20.0,
                "stereo_width": 110.0,
                "dry_gain": 0.0,
                "wet_mix": 65.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)",   detune=-4.5, delay=6.0,  pan=-70.0, gain=-3.0),
                    _make_voice(True, interval="Unison (0)",   detune=+4.5, delay=11.0, pan=+70.0, gain=-3.0),
                    _make_voice(True, interval="+1 Oct (+12)", detune=-7.0, delay=15.0, pan=-45.0, gain=-6.0),
                    _make_voice(True, interval="+1 Oct (+12)", detune=+7.0, delay=20.0, pan=+45.0, gain=-6.0),
                ],
            },
            "Dual-Tracked Metal Rhythm Guitar (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "Chromatic",
                "formant_preserve": False,
                "humanize": 38.0,
                "stereo_width": 125.0,
                "dry_gain": -60.0,
                "wet_mix": 100.0,
                "output_gain": 1.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-12.0, delay=14.0, pan=-100.0, gain=0.0),
                    _make_voice(True, interval="Unison (0)", detune=+12.0, delay=25.0, pan=+100.0, gain=0.0),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },
            "Clean Guitar 80s Chorus (4V)": {
                "enabled": True,
                "voice_count": 4,
                "key_scale": "Chromatic",
                "formant_preserve": False,
                "humanize": 55.0,
                "stereo_width": 130.0,
                "dry_gain": -1.0,
                "wet_mix": 72.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-14.0, delay=12.0, pan=-90.0, gain=-3.0),
                    _make_voice(True, interval="Unison (0)", detune=+14.0, delay=18.0, pan=+90.0, gain=-3.0),
                    _make_voice(True, interval="Unison (0)", detune=-22.0, delay=26.0, pan=-50.0, gain=-5.0),
                    _make_voice(True, interval="Unison (0)", detune=+22.0, delay=33.0, pan=+50.0, gain=-5.0),
                ],
            },
            "Supersaw Synth Unison Stack (4V)": {
                "enabled": True,
                "voice_count": 4,
                "key_scale": "Chromatic",
                "formant_preserve": False,
                "humanize": 12.0,
                "stereo_width": 135.0,
                "dry_gain": -2.0,
                "wet_mix": 82.0,
                "output_gain": -0.5,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-18.0, delay=5.0,  pan=-65.0,  gain=-2.0),
                    _make_voice(True, interval="Unison (0)", detune=+18.0, delay=8.0,  pan=+65.0,  gain=-2.0),
                    _make_voice(True, interval="Unison (0)", detune=-34.0, delay=13.0, pan=-100.0, gain=-3.0),
                    _make_voice(True, interval="Unison (0)", detune=+34.0, delay=17.0, pan=+100.0, gain=-3.0),
                ],
            },
            "Stereo Bass Widener (Safe Low-End)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "Chromatic",
                "formant_preserve": False,
                "humanize": 10.0,
                "stereo_width": 95.0,
                "dry_gain": 0.0,
                "wet_mix": 38.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-6.5, delay=9.0,  pan=-75.0, gain=-5.0),
                    _make_voice(True, interval="Unison (0)", detune=+6.5, delay=14.0, pan=+75.0, gain=-5.0),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },

            # --- 4. 创意空间与复古质感 (Creative & Slapback) ---
            "Classic Rockabilly Slapback (2V)": {
                "enabled": True,
                "voice_count": 2,
                "key_scale": "Chromatic",
                "formant_preserve": True,
                "humanize": 18.0,
                "stereo_width": 85.0,
                "dry_gain": 0.0,
                "wet_mix": 50.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)", detune=-4.0, delay=68.0, pan=-40.0, gain=-4.5),
                    _make_voice(True, interval="Unison (0)", detune=+4.0, delay=82.0, pan=+40.0, gain=-5.5),
                    _make_voice(False),
                    _make_voice(False),
                ],
            },
            "Dreamy Detuned Lo-Fi Ensemble (4V)": {
                "enabled": True,
                "voice_count": 4,
                "key_scale": "D Minor",
                "formant_preserve": True,
                "humanize": 78.0,
                "stereo_width": 125.0,
                "dry_gain": -2.5,
                "wet_mix": 80.0,
                "output_gain": 0.0,
                "voices": [
                    _make_voice(True, interval="Unison (0)",   detune=-26.0, delay=22.0, pan=-85.0, gain=-3.0),
                    _make_voice(True, interval="Unison (0)",   detune=+26.0, delay=34.0, pan=+85.0, gain=-3.0),
                    _make_voice(True, interval="+5th (+7)",    detune=-15.0, delay=48.0, pan=-50.0, gain=-6.5),
                    _make_voice(True, interval="-1 Oct (-12)", detune=+12.0, delay=56.0, pan=+50.0, gain=-7.0),
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
            ProVerticalMeter, PhaseCorrelationMeter, OutputFaderStrip, ProDoublerStageWidget {{
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
        # 1. 顶部主控、声部拓扑与智能和声调性栏
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

        title_lbl = QLabel(f"DOUBLER / HARMONIZER  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(8)

        # 预设管理
        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存当前加倍与和声配置为预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = QComboBox()
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

        # 2 / 4 声道数分段选择器
        vc_lbl = QLabel("Voices:")
        vc_lbl.setStyleSheet("color: #8A8A94; font-size: 10px; font-weight: 600;")
        tb_lay.addWidget(vc_lbl)

        self._voice_count_btns: dict[int, QPushButton] = {}
        vc_wrap = QFrame()
        vc_wrap.setStyleSheet("background-color: #18181B; border: 1px solid #121214; border-radius: 3px;")
        vc_lay = QHBoxLayout(vc_wrap)
        vc_lay.setContentsMargins(2, 2, 2, 2)
        vc_lay.setSpacing(2)
        for cnt in (2, 4):
            btn = QPushButton(f"{cnt}V")
            btn.setFixedSize(28, 18)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, c=cnt: self._on_voice_count_changed(c))
            vc_lay.addWidget(btn)
            self._voice_count_btns[cnt] = btn
        tb_lay.addWidget(vc_wrap)

        tb_lay.addSpacing(8)

        # 智能和声调性锁定 & 共振峰保护（与修音器共享 DSP 基建）
        scale_lbl = QLabel("Scale:")
        scale_lbl.setStyleSheet("color: #8A8A94; font-size: 10px; font-weight: 600;")
        tb_lay.addWidget(scale_lbl)

        self.scale_combo = QComboBox()
        self.scale_combo.setFixedWidth(96)
        self.scale_combo.addItems(MUSICAL_KEYS)
        self.scale_combo.setToolTip("与修音器共享音高分析基建：自动锁定三度/五度智能和声音阶")
        self.scale_combo.currentTextChanged.connect(self._on_scale_changed)
        tb_lay.addWidget(self.scale_combo)

        self.btn_formant = QPushButton("Formant")
        self.btn_formant.setCheckable(True)
        self.btn_formant.setFixedSize(58, 22)
        self.btn_formant.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_formant.setToolTip("开启共振峰保护：移调生成和声时保持真实人声喉部音色")
        self.btn_formant.clicked.connect(self._on_formant_toggled)
        tb_lay.addWidget(self.btn_formant)

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
        reset_btn.clicked.connect(self.reset_doubler)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控与全局调制区：
        #    Input | Doubler Stereo Stage | Global Mod & Mix | Phase | Output L/R | Out Gain
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
        self.meter_input = ProVerticalMeter(show_ticks=True)
        in_col.addWidget(self.meter_input, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(in_col)
        up_lay.addWidget(_vsep())

        # 2.2 立体声声场与微失谐拓扑舞台
        self.stage_widget = ProDoublerStageWidget()
        self.stage_widget.voiceNodeDragged.connect(self._on_stage_node_dragged)
        self.stage_widget.voiceDetuneWheeled.connect(self._on_stage_detune_wheeled)
        self.stage_widget.voiceSelected.connect(self._on_voice_selected)
        self.stage_widget.voiceSoloToggled.connect(self._on_stage_solo_toggled)
        self.stage_widget.voiceMuteToggled.connect(self._on_stage_mute_toggled)
        up_lay.addWidget(self.stage_widget, 1)
        up_lay.addWidget(_vsep())

        # 2.3 全局调制与干湿混合控制塔 (Humanize / Width / Dry / Wet Mix)
        global_col = self._build_meter_column("Global Blend")
        g_grid = QVBoxLayout()
        g_grid.setContentsMargins(6, 4, 6, 6)
        g_grid.setSpacing(6)

        g_row1 = QHBoxLayout()
        g_row1.setSpacing(4)
        self.c_humanize = KnobParameterCell(
            "Humanize", 0.0, 100.0, 25.0, COLORS["cyan"],
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), pill_width=50
        )
        self.c_width = KnobParameterCell(
            "Width", 0.0, 150.0, 100.0, COLORS["cyan"],
            "0", "%", "150",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), pill_width=50
        )
        g_row1.addWidget(self.c_humanize)
        g_row1.addWidget(self.c_width)
        g_grid.addLayout(g_row1)

        g_row2 = QHBoxLayout()
        g_row2.setSpacing(4)
        self.c_dry = KnobParameterCell(
            "Direct", -60.0, 6.0, 0.0, COLORS["cyan"],
            "-inf", "dB", "+6",
            format_db_signed, lambda s, fb: float(s), pill_width=50
        )
        self.c_mix = KnobParameterCell(
            "Wet Mix", 0.0, 100.0, 65.0, COLORS["cyan"],
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), pill_width=50
        )
        g_row2.addWidget(self.c_dry)
        g_row2.addWidget(self.c_mix)
        g_grid.addLayout(g_row2)

        self.c_humanize.valueChanged.connect(lambda v: self._on_global_param("humanize", round(v, 0)))
        self.c_width.valueChanged.connect(lambda v: self._on_global_param("stereo_width", round(v, 0)))
        self.c_dry.valueChanged.connect(lambda v: self._on_global_param("dry_gain", round(v, 1)))
        self.c_mix.valueChanged.connect(lambda v: self._on_global_param("wet_mix", round(v, 0)))

        global_col.addLayout(g_grid, 1)
        up_lay.addLayout(global_col)
        up_lay.addWidget(_vsep())

        # 2.4 Phase 相位相关度表
        ph_col = self._build_meter_column("Phase")
        self.meter_phase = PhaseCorrelationMeter()
        ph_col.addWidget(self.meter_phase, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(ph_col)
        up_lay.addWidget(_vsep())

        # 2.5 Output 立体声 L / R 电平表
        out_col = self._build_meter_column("Output L/R")
        out_row = QHBoxLayout()
        out_row.setContentsMargins(3, 0, 3, 0)
        out_row.setSpacing(3)
        self.meter_out_l = ProVerticalMeter(title="L", show_ticks=True)
        self.meter_out_r = ProVerticalMeter(title="R", show_ticks=False)
        out_row.addWidget(self.meter_out_l)
        out_row.addWidget(self.meter_out_r)
        out_col.addLayout(out_row, 1)
        up_lay.addLayout(out_col)
        up_lay.addWidget(_vsep())

        # 2.6 Output 主增益推子
        og_col = self._build_meter_column("Out Gain")
        self.out_fader = OutputFaderStrip()
        self.out_fader.gainChanged.connect(self._on_output_gain_changed)
        og_col.addWidget(self.out_fader, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(og_col)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部 2–4 声部独立加倍与和声卡片矩阵
        # -------------------------------------------------------------
        cards_wrap = QWidget()
        cards_lay = QHBoxLayout(cards_wrap)
        cards_lay.setContentsMargins(0, 0, 0, 0)
        cards_lay.setSpacing(0)

        self.voice_cards: list[DoublerVoiceCard] = []
        st = self._current_state()
        for i in range(4):
            card = DoublerVoiceCard(i, st["voices"][i])
            card.voiceChanged.connect(self._on_voice_card_changed)
            card.selected.connect(self._on_voice_selected)
            self.voice_cards.append(card)
            cards_lay.addWidget(card, 1)

        main_lay.addWidget(cards_wrap, 4)

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

    def _update_top_bar_styles(self):
        st = self._current_state()
        cnt = st["voice_count"]

        for c, btn in self._voice_count_btns.items():
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

        self.btn_formant.setChecked(bool(st["formant_preserve"]))
        self.btn_formant.setStyleSheet("""
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
                color: #4CD490;
                border: 1px solid #4CD490;
                font-weight: 700;
            }
        """)

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
        cnt = st["voice_count"]
        master_on = bool(st["enabled"])

        self.power_switch.setChecked(master_on)

        self.scale_combo.blockSignals(True)
        self.scale_combo.setCurrentText(st["key_scale"])
        self.scale_combo.blockSignals(False)

        self.c_humanize.setValue(st["humanize"])
        self.c_width.setValue(st["stereo_width"])
        self.c_dry.setValue(st["dry_gain"])
        self.c_mix.setValue(st["wet_mix"])
        for cell in (self.c_humanize, self.c_width, self.c_dry, self.c_mix):
            cell.setModuleActive(master_on)

        self.meter_phase.setActive(master_on)
        self.meter_out_l.setActive(master_on)
        self.meter_out_r.setActive(master_on)
        self.out_fader.setGain(st["output_gain"])
        self._update_top_bar_styles()

        for i in range(4):
            visible = (i < cnt)
            self.voice_cards[i].setVisible(visible)
            if visible:
                self.voice_cards[i].syncFromData(st["voices"][i])
                self.voice_cards[i].setSelected(i == self._selected_voice)

        self.stage_widget.setSelectedVoice(self._selected_voice)
        self.stage_widget.setState(st)

    def _emit_solo_list(self):
        st = self._current_state()
        cnt = st["voice_count"]
        solos = [i for i in range(cnt) if st["voices"][i]["solo"]]
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

    def _on_voice_count_changed(self, count: int):
        st = self._current_state()
        if st["voice_count"] != count:
            st["voice_count"] = count
            if self._selected_voice >= count:
                self._selected_voice = count - 1
            self._refresh_all_views()
            self.voiceCountChanged.emit(count)
            self._emit_solo_list()
            self.stateChanged.emit(self.get_state())

    def _on_scale_changed(self, scale_name: str):
        st = self._current_state()
        st["key_scale"] = scale_name
        self.harmonyScaleChanged.emit(scale_name, bool(st["formant_preserve"]))
        self.stateChanged.emit(self.get_state())

    def _on_formant_toggled(self, checked: bool):
        st = self._current_state()
        st["formant_preserve"] = checked
        self._update_top_bar_styles()
        self.harmonyScaleChanged.emit(st["key_scale"], checked)
        self.stateChanged.emit(self.get_state())

    def _on_global_param(self, key: str, val: float):
        st = self._current_state()
        st[key] = val
        self.stage_widget.setState(st)
        self.globalParamsChanged.emit({
            "humanize": st["humanize"],
            "stereo_width": st["stereo_width"],
            "dry_gain": st["dry_gain"],
            "wet_mix": st["wet_mix"],
        })
        self.stateChanged.emit(self.get_state())

    def _on_output_gain_changed(self, gain: float):
        st = self._current_state()
        st["output_gain"] = gain
        self.outputGainChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _on_voice_selected(self, idx: int):
        self._selected_voice = idx
        for i, card in enumerate(self.voice_cards):
            card.setSelected(i == idx)
        self.stage_widget.setSelectedVoice(idx)

    def _on_voice_card_changed(self, idx: int, voice_data: dict):
        st = self._current_state()
        st["voices"][idx] = voice_data
        self._refresh_all_views()
        self.voiceChanged.emit(idx, copy.deepcopy(voice_data))
        self._emit_solo_list()
        self.stateChanged.emit(self.get_state())

    def _on_stage_node_dragged(self, idx: int, pan: float, delay_ms: float):
        st = self._current_state()
        st["voices"][idx]["pan"] = pan
        st["voices"][idx]["delay"] = delay_ms
        self._refresh_all_views()
        self.voiceChanged.emit(idx, copy.deepcopy(st["voices"][idx]))
        self.stateChanged.emit(self.get_state())

    def _on_stage_detune_wheeled(self, idx: int, detune_cents: float):
        st = self._current_state()
        st["voices"][idx]["detune"] = detune_cents
        self._refresh_all_views()
        self.voiceChanged.emit(idx, copy.deepcopy(st["voices"][idx]))
        self.stateChanged.emit(self.get_state())

    def _on_stage_solo_toggled(self, idx: int):
        st = self._current_state()
        st["voices"][idx]["solo"] = not st["voices"][idx]["solo"]
        self._refresh_all_views()
        self.voiceChanged.emit(idx, copy.deepcopy(st["voices"][idx]))
        self._emit_solo_list()
        self.stateChanged.emit(self.get_state())

    def _on_stage_mute_toggled(self, idx: int):
        st = self._current_state()
        st["voices"][idx]["mute"] = not st["voices"][idx]["mute"]
        self._refresh_all_views()
        self.voiceChanged.emit(idx, copy.deepcopy(st["voices"][idx]))
        self._emit_solo_list()
        self.stateChanged.emit(self.get_state())

    def _load_preset(self, name: str):
        if name in self._presets:
            self._banks[self._active_bank] = copy.deepcopy(self._presets[name])
            self._selected_voice = 0
            self._refresh_all_views()
            self._emit_solo_list()
            self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存 Doubler / 和声预设", "输入预设名称:")
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
        cnt = st["voice_count"]
        voices = st["voices"][:cnt]
        any_solo = any(v["solo"] for v in voices)

        in_db = -16.0 + 7.5 * math.sin(self._anim_t * 1.15) + random.uniform(-1.1, 1.1)
        energy = max(0.05, min(1.0, (in_db + 45.0) / 40.0))

        # 计算加倍声部对立体声宽度与左右电平的动态贡献
        active_voices = [
            v for v in voices
            if st["enabled"] and v["enabled"] and (not v["mute"]) and (not any_solo or v["solo"])
        ]
        spread = 0.15
        for v in active_voices:
            spread += (abs(v["pan"]) / 100.0) * (st["stereo_width"] / 100.0) * 0.28
        spread = min(0.95, spread * (st["wet_mix"] / 100.0 + 0.25))

        # 生成李萨如立体声示波轨迹
        liss_pts = []
        for k in range(48):
            ph = self._anim_t * 2.5 + k * 0.14
            mid = math.sin(ph) * energy
            side = math.cos(ph * 1.37 + 0.6) * energy * spread
            liss_pts.append((max(-1.0, min(1.0, side)), max(-1.0, min(1.0, mid))))

        corr = max(-0.2, min(0.98, 0.92 - spread * 0.72 + 0.06 * math.sin(self._anim_t)))
        wet_boost = (len(active_voices) * 0.9) * (st["wet_mix"] / 100.0) if st["enabled"] else 0.0
        out_l = in_db + wet_boost + (st["output_gain"] if st["enabled"] else 0.0) + random.uniform(-0.6, 0.6)
        out_r = in_db + wet_boost + (st["output_gain"] if st["enabled"] else 0.0) + random.uniform(-0.6, 0.6)

        self.meter_input.setValue(in_db)
        self.meter_phase.setCorrelation(corr if st["enabled"] else 1.0)
        self.meter_out_l.setValue(out_l)
        self.meter_out_r.setValue(out_r)
        self.stage_widget.setRealtimeVisuals(liss_pts, energy)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP（与修音器共享基建）及工程序列化调用
    # -----------------------------------------------------------------
    def reset_doubler(self):
        self._banks[self._active_bank] = default_doubler_flat()
        self._refresh_all_views()
        self._emit_solo_list()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_meters(
        self,
        input_db: float,
        output_l_db: float,
        output_r_db: float,
        phase_correlation: float = 0.5,
        lissajous_points: list[tuple[float, float]] | None = None,
    ):
        """
        接入真实音频引擎实时数据 (每帧 30~60Hz 调用)：
          - input_db: 输入总电平 (dBFS)
          - output_l_db / output_r_db: 立体声左右通道输出电平 (dBFS)
          - phase_correlation: 立体声相位相关度 [-1.0, +1.0]
          - lissajous_points: 可选，归一化立体声 M/S 示波采样点 [(x, y), ...]
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()

        self.meter_input.setValue(input_db)
        self.meter_out_l.setValue(output_l_db)
        self.meter_out_r.setValue(output_r_db)
        self.meter_phase.setCorrelation(phase_correlation)
        energy = max(0.0, min(1.0, (input_db + 50.0) / 48.0))
        if lissajous_points is not None:
            self.stage_widget.setRealtimeVisuals(lissajous_points, energy)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioDoublerWidget("Lead Vocal")
    win.show()
    sys.exit(app.exec())