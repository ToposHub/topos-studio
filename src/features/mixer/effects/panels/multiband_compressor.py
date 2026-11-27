from __future__ import annotations

import sys
import math
import copy
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer, QSignalBlocker
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont
)

from src.shared.ui.widgets.chevron_combo import ChevronComboBox

# =====================================================================
# 1. 全局色彩系统与默认参数配置（与 Dyamics-UI.py 保持同一视觉规范）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":   "#28D8F0",  # 主控 / A-B 激活 / 频谱主色
    "band_0": "#6495FA",  # Band 1 (Low) 钴蓝
    "band_1": "#4CD490",  # Band 2 (Low-Mid) 翡翠绿
    "band_2": "#F0A848",  # Band 3 (High-Mid) 琥珀金
    "band_3": "#F26D6D",  # Band 4 (High) 珊瑚红
}

BAND_PALETTE = [
    COLORS["band_0"],
    COLORS["band_1"],
    COLORS["band_2"],
    COLORS["band_3"],
]


def _make_band(
    enabled: bool = True,
    threshold: float = -18.0,
    ratio: float = 2.5,
    knee: float = 20.0,
    attack: float = 10.0,
    release: float = 120.0,
    makeup: float = 0.0,
    solo: bool = False,
    mute: bool = False,
) -> dict:
    return {
        "enabled": enabled,
        "threshold": threshold,   # -60.0 ~ 0.0 dB
        "ratio": ratio,           # 1.0 ~ 20.0 (:1)
        "knee": knee,             # 0 ~ 100 %
        "attack": attack,         # 0.10 ~ 200.0 ms
        "release": release,       # 10.0 ~ 2000.0 ms
        "makeup": makeup,         # -12.0 ~ +18.0 dB
        "solo": solo,
        "mute": mute,
    }


def default_multiband_demo() -> dict:
    """默认 3 频段演示配置：总线低频控制 + 人声中频密度 + 高频空气感"""
    return {
        "enabled": True,
        "band_count": 3,                        # 2 | 3 | 4
        "crossovers": [140.0, 2200.0, 8000.0],  # 最多 3 个交叉点 (Hz)
        "phase_mode": "Linear Phase",           # "Min Phase" | "Linear Phase"
        "slope": "24 dB/oct",                   # "12 dB/oct" | "24 dB/oct" | "48 dB/oct"
        "output_gain": 0.0,                     # -12.0 ~ +12.0 dB
        "bands": [
            _make_band(True, threshold=-20.0, ratio=3.5, knee=25.0, attack=18.0, release=140.0, makeup=1.0),
            _make_band(True, threshold=-16.0, ratio=2.4, knee=30.0, attack=8.0,  release=110.0, makeup=0.5),
            _make_band(True, threshold=-22.0, ratio=2.0, knee=20.0, attack=3.0,  release=85.0,  makeup=1.5),
            _make_band(True, threshold=-24.0, ratio=2.2, knee=20.0, attack=1.5,  release=70.0,  makeup=0.0),
        ],
    }


def default_multiband_flat() -> dict:
    st = default_multiband_demo()
    st["output_gain"] = 0.0
    for b in st["bands"]:
        b["enabled"] = False
        b["threshold"] = -12.0
        b["ratio"] = 1.5
        b["knee"] = 0.0
        b["makeup"] = 0.0
        b["solo"] = False
        b["mute"] = False
    return st


def format_ms(val: float) -> str:
    if val < 1.0:
        return f"{val:.2f}"
    if val < 10.0:
        return f"{val:.1f}"
    return f"{int(round(val))}"


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


def format_ratio(val: float) -> str:
    if val >= 10.0:
        return f"{val:.0f}:1"
    return f"{val:.1f}:1"


def parse_ratio(text: str, fallback: float) -> float:
    s = text.strip().replace(" ", "")
    try:
        if ":" in s:
            left, _ = s.split(":", 1)
            return float(left)
        return float(s)
    except ValueError:
        return fallback


def format_db_signed(val: float) -> str:
    if abs(val) < 0.05:
        return "0.0"
    return f"{val:+.1f}"


# =====================================================================
# 2. 基础定制控件：电源开关 / 可编辑数字胶囊 / 阳极铝旋钮 / 电平表
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
            clean = self._text.replace("dB", "").replace("%", "").replace("ms", "").replace("Hz", "").strip()
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
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        n = max(0.0, min(1.0, norm))
        if self._mode == "log":
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return mn * ((mx / mn) ** n)
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
        mode: str = "level",      # "level" | "gr"
        min_db: float = -55.0,
        max_db: float = 0.0,
        ticks: list[int] | None = None,
        show_ticks: bool = True,
        accent_color: str = "#F0A848",
        parent=None,
    ):
        super().__init__(parent)
        self._title = title
        self._mode = mode
        self._min_db = min_db
        self._max_db = max_db
        self._ticks = ticks or (
            [0, -5, -10, -15, -20, -30, -40, -50]
            if mode == "level"
            else [0, -3, -6, -10, -14, -20, -30]
        )
        self._show_ticks = show_ticks
        self._accent = QColor(accent_color)

        self._value_db = min_db if mode == "level" else 0.0
        self._peak_db = self._value_db
        self._clipped = False
        self._active = True

        self.setFixedWidth(40 if show_ticks else 16)
        self.setMinimumHeight(180)

    def setValue(self, db: float):
        if self._mode == "level":
            self._value_db = max(self._min_db, min(self._max_db, db))
            if self._value_db >= self._peak_db:
                self._peak_db = self._value_db
            else:
                self._peak_db = max(self._value_db, self._peak_db - 0.35)
            if db >= -0.1:
                self._clipped = True
        else:
            val = -abs(db)
            self._value_db = max(self._min_db, min(0.0, val))
            if self._value_db <= self._peak_db:
                self._peak_db = self._value_db
            else:
                self._peak_db = min(self._value_db, self._peak_db + 0.35)
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
        if self._mode == "level":
            if clamped >= -20.0:
                norm = 0.52 + ((clamped + 20.0) / 20.0) * 0.48
            else:
                norm = ((clamped - self._min_db) / (-20.0 - self._min_db)) * 0.52
        else:
            if clamped >= -14.0:
                norm = 0.55 + ((clamped + 14.0) / 14.0) * 0.45
            else:
                norm = ((clamped - self._min_db) / (-14.0 - self._min_db)) * 0.55
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
            if self._mode == "level":
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
            else:
                y_val = self._db_to_y(self._value_db, bar)
                fill_h = y_val - bar.top()
                if fill_h > 1.0:
                    fill_rect = QRectF(bar.left() + 1.0, bar.top() + 0.5, bar.width() - 2.0, fill_h)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(self._accent)
                    p.drawRoundedRect(fill_rect, 1.2, 1.2)

                if self._peak_db < -0.3:
                    py = self._db_to_y(self._peak_db, bar)
                    p.setPen(QPen(QColor("#FFFFFF"), 1.3))
                    p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

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
# 3. 控件：交互式多段压缩频率与动态响应画布 (Spectrum + Xover + Band Threshold/GR)
# =====================================================================
class ProMultibandGraphWidget(QWidget):
    crossoverDragged = pyqtSignal(int, float)    # xover_idx, freq_hz
    bandThresholdDragged = pyqtSignal(int, float)  # band_idx, thresh_db
    bandSelected = pyqtSignal(int)
    bandSoloToggled = pyqtSignal(int)
    bandMuteToggled = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 230)
        self.setMouseTracking(True)

        self._state = default_multiband_demo()
        self._selected_band = 0
        self._band_grs = [0.0, 0.0, 0.0, 0.0]
        self._show_audition_controls = True

        # 实时 FFT 频谱数据 (64 频点, -60 ~ 0 dBFS)
        self._spectrum_freqs = [20.0 * ((20000.0 / 20.0) ** (i / 63.0)) for i in range(64)]
        self._spectrum_dbs = [-45.0] * 64

        # 交互状态
        self._drag_mode = ""        # "xover" | "thresh" | ""
        self._drag_index = -1
        self._hover_mode = ""       # "xover" | "thresh" | "solo" | "mute" | ""
        self._hover_index = -1
        self._mouse_pos: QPointF | None = None

        # 每个频段顶部的快捷 Solo/Mute 按钮命中矩形缓存
        self._solo_rects: list[QRectF] = []
        self._mute_rects: list[QRectF] = []

        self._ml = 34.0
        self._mr = 12.0
        self._mt = 12.0
        self._mb = 22.0

    def setState(self, state: dict):
        self._state = state
        self.update()

    def use_processor_controls(self) -> None:
        self._show_audition_controls = False
        self._spectrum_dbs = []

    def setSelectedBand(self, idx: int):
        if self._selected_band != idx:
            self._selected_band = idx
            self.update()

    def setRealtimeData(self, band_grs: list[float], spectrum_dbs: list[float] | None = None):
        for i in range(min(4, len(band_grs))):
            self._band_grs[i] = max(0.0, band_grs[i])
        if spectrum_dbs is not None and len(spectrum_dbs) >= 2:
            self._spectrum_dbs = list(spectrum_dbs)
            if len(self._spectrum_freqs) != len(spectrum_dbs):
                n = len(spectrum_dbs)
                self._spectrum_freqs = [20.0 * ((20000.0 / 20.0) ** (i / (n - 1))) for i in range(n)]
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
        norm = math.log10(f / 20.0) / 3.0  # log10(20000/20) = 3.0
        return plot.left() + norm * plot.width()

    def _x_to_freq(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return 20.0 * (10.0 ** (norm * 3.0))

    def _db_to_y(self, db: float, plot: QRectF) -> float:
        # 纵轴范围: +12 dB (顶) ~ -60 dB (底)
        clamped = max(-60.0, min(12.0, db))
        norm = (12.0 - clamped) / 72.0
        return plot.top() + norm * plot.height()

    def _y_to_db(self, y: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (y - plot.top()) / plot.height()))
        return 12.0 - norm * 72.0

    def _band_x_ranges(self, plot: QRectF) -> list[tuple[float, float]]:
        cnt = self._state["band_count"]
        xovers = self._state["crossovers"][: cnt - 1]
        edges = [plot.left()] + [self._freq_to_x(f, plot) for f in xovers] + [plot.right()]
        return [(edges[i], edges[i + 1]) for i in range(cnt)]

    def _hit_test(self, pos: QPointF, plot: QRectF) -> tuple[str, int]:
        # 1. 优先命中频段右上角的 S / M 快捷胶囊
        for idx, r in enumerate(self._solo_rects):
            if r.contains(pos):
                return "solo", idx
        for idx, r in enumerate(self._mute_rects):
            if r.contains(pos):
                return "mute", idx

        cnt = self._state["band_count"]
        xovers = self._state["crossovers"][: cnt - 1]

        # 2. 命中垂直交叉频率线 (容差 7px)
        for i, xf in enumerate(xovers):
            xx = self._freq_to_x(xf, plot)
            if abs(pos.x() - xx) <= 7.0 and plot.top() <= pos.y() <= plot.bottom():
                return "xover", i

        # 3. 命中各频段水平阈值线 (容差 7px)
        ranges = self._band_x_ranges(plot)
        for i, (x1, x2) in enumerate(ranges):
            if x1 <= pos.x() <= x2:
                ty = self._db_to_y(self._state["bands"][i]["threshold"], plot)
                if abs(pos.y() - ty) <= 7.0:
                    return "thresh", i

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
            if mode in ("xover", "thresh"):
                self._drag_mode = mode
                self._drag_index = idx
                if mode == "thresh":
                    self.bandSelected.emit(idx)
                return

            # 点击空白区域选中对应频段
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
            low_bound = 30.0 if idx == 0 else xovers[idx - 1] * 1.18
            high_bound = 18000.0 if idx >= cnt - 2 else xovers[idx + 1] / 1.18
            raw_hz = self._x_to_freq(event.position().x(), plot)
            clamped_hz = max(low_bound, min(high_bound, raw_hz))
            # 四舍五入到整十或整百
            rounded = round(clamped_hz, 0) if clamped_hz < 1000.0 else round(clamped_hz, -1)
            self.crossoverDragged.emit(idx, float(rounded))
            return

        if self._drag_mode == "thresh" and self._drag_index >= 0:
            raw_db = self._y_to_db(event.position().y(), plot)
            clamped_db = max(-60.0, min(0.0, round(raw_db, 1)))
            self.bandThresholdDragged.emit(self._drag_index, clamped_db)
            return

        mode, idx = self._hit_test(event.position(), plot)
        self._hover_mode = mode
        self._hover_index = idx
        if mode == "xover":
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif mode == "thresh":
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

        # 1. 画布底色
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 对数频率与 dB 网格
        grid_pen = QPen(QColor("#18181C"), 1.0)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        for db in (12, 0, -12, -24, -36, -48, -60):
            y = self._db_to_y(float(db), plot)
            if db == 0:
                p.setPen(QPen(QColor("#2E2E35"), 1.1))
            else:
                p.setPen(grid_pen)
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

        # 3. 实时输入频谱填充曲线
        if len(self._spectrum_dbs) >= 2:
            spec_path = QPainterPath()
            spec_fill = QPainterPath()
            for i, (sf, sdb) in enumerate(zip(self._spectrum_freqs, self._spectrum_dbs)):
                sx = self._freq_to_x(sf, plot)
                sy = self._db_to_y(sdb, plot)
                if i == 0:
                    spec_path.moveTo(sx, sy)
                    spec_fill.moveTo(sx, plot.bottom())
                    spec_fill.lineTo(sx, sy)
                else:
                    spec_path.lineTo(sx, sy)
                    spec_fill.lineTo(sx, sy)
            spec_fill.lineTo(plot.right(), plot.bottom())
            spec_fill.closeSubpath()

            spec_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            spec_grad.setColorAt(0.0, QColor(40, 216, 240, 42 if master_on else 18))
            spec_grad.setColorAt(1.0, QColor(40, 216, 240, 4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(spec_grad)
            p.drawPath(spec_fill)

            p.setPen(QPen(QColor(40, 216, 240, 95 if master_on else 45), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(spec_path)

        # 4. 绘制各频段区域、动态增益/衰减色带与阈值线
        self._solo_rects = []
        self._mute_rects = []

        for i, (x1, x2) in enumerate(ranges):
            b = bands[i]
            col = QColor(BAND_PALETTE[i])
            band_w = x2 - x1

            # 判断当前频段是否处于有效发声/激活状态
            is_audible = master_on and (not b["mute"]) and (not any_solo or b["solo"])
            is_active = is_audible and b["enabled"]
            is_selected = (i == self._selected_band)

            # 4.1 频段背景微光晕（选中或 Solo 时略微提亮）
            bg_alpha = 26 if b["solo"] else (18 if is_selected else 8)
            if not is_audible:
                bg_alpha = 3
            p.fillRect(QRectF(x1, plot.top(), band_w, plot.height()), QColor(col.red(), col.green(), col.blue(), bg_alpha))

            # 4.2 静态 Make-Up + 实时 Gain Reduction 动态包络带（以 0 dB 为基准线）
            if is_active:
                gr_val = self._band_grs[i]
                net_db = b["makeup"] - gr_val
                y_net = self._db_to_y(net_db, plot)
                top_y = min(y_zero, y_net)
                bot_y = max(y_zero, y_net)
                if abs(bot_y - top_y) > 0.8:
                    ribbon = QRectF(x1 + 1.0, top_y, max(2.0, band_w - 2.0), bot_y - top_y)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(col.red(), col.green(), col.blue(), 62))
                    p.drawRect(ribbon)

                    p.setPen(QPen(col, 1.8))
                    p.drawLine(QPointF(x1 + 1.0, y_net), QPointF(x2 - 1.0, y_net))

            # 4.3 阈值水平虚线 + 中央拖拽手柄
            ty = self._db_to_y(b["threshold"], plot)
            is_th_hi = (
                (self._hover_mode == "thresh" and self._hover_index == i)
                or (self._drag_mode == "thresh" and self._drag_index == i)
            )
            th_col = QColor(col)
            th_col.setAlpha(240 if is_th_hi else (175 if is_active else 65))
            pen_style = Qt.PenStyle.SolidLine if is_th_hi else Qt.PenStyle.DashLine
            p.setPen(QPen(th_col, 1.8 if is_th_hi else 1.2, pen_style))
            p.drawLine(QPointF(x1 + 2.0, ty), QPointF(x2 - 2.0, ty))

            # 阈值手柄胶囊
            if band_w > 36.0:
                hw = min(26.0, band_w * 0.35)
                h_rect = QRectF((x1 + x2) / 2.0 - hw / 2.0, ty - 3.5, hw, 7.0)
                p.setPen(QPen(th_col, 1.2))
                p.setBrush(QColor("#151518"))
                p.drawRoundedRect(h_rect, 3.0, 3.0)

            # 4.4 频段顶部标识与 S / M 快捷交互按钮
            if band_w >= 54.0:
                p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
                p.setPen(col if is_audible else QColor("#666670"))
                p.drawText(QRectF(x1 + 6, plot.top() + 5, 42, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"B{i + 1}")

            if self._show_audition_controls and band_w >= 54.0:
                # Mute 按钮
                m_rect = QRectF(x2 - 21.0, plot.top() + 5.0, 16.0, 15.0)
                s_rect = QRectF(x2 - 40.0, plot.top() + 5.0, 16.0, 15.0)
                self._solo_rects.append(s_rect)
                self._mute_rects.append(m_rect)

                # 绘制 Solo 按钮
                p.setPen(QPen(QColor("#F0A848") if b["solo"] else QColor("#3A3A42"), 1.0))
                p.setBrush(QColor("#3A2E18") if b["solo"] else QColor("#18181C"))
                p.drawRoundedRect(s_rect, 2.5, 2.5)
                p.setPen(QColor("#F0A848") if b["solo"] else QColor("#9A9AA2"))
                p.drawText(s_rect, Qt.AlignmentFlag.AlignCenter, "S")

                # 绘制 Mute 按钮
                p.setPen(QPen(QColor("#F26D6D") if b["mute"] else QColor("#3A3A42"), 1.0))
                p.setBrush(QColor("#381C1E") if b["mute"] else QColor("#18181C"))
                p.drawRoundedRect(m_rect, 2.5, 2.5)
                p.setPen(QColor("#F26D6D") if b["mute"] else QColor("#9A9AA2"))
                p.drawText(m_rect, Qt.AlignmentFlag.AlignCenter, "M")
            else:
                self._solo_rects.append(QRectF())
                self._mute_rects.append(QRectF())

        # 5. 绘制交叉频率分割线与顶部频率胶囊
        xovers = st["crossovers"][: cnt - 1]
        for i, xf in enumerate(xovers):
            xx = self._freq_to_x(xf, plot)
            is_xo_hi = (
                (self._hover_mode == "xover" and self._hover_index == i)
                or (self._drag_mode == "xover" and self._drag_index == i)
            )
            line_col = QColor("#28D8F0") if is_xo_hi else QColor("#6E707A")
            p.setPen(QPen(line_col, 2.0 if is_xo_hi else 1.3))
            p.drawLine(QPointF(xx, plot.top()), QPointF(xx, plot.bottom()))

            # 交叉点菱形节点与频率标签
            mid_y = plot.bottom() - 26.0
            pill_w, pill_h = 44.0, 17.0
            pill_rect = QRectF(xx - pill_w / 2.0, mid_y - pill_h / 2.0, pill_w, pill_h)
            p.setPen(QPen(QColor("#28D8F0") if is_xo_hi else QColor("#3A3A42"), 1.0))
            p.setBrush(QColor("#151518"))
            p.drawRoundedRect(pill_rect, 3.5, 3.5)
            p.setPen(QColor("#28D8F0") if is_xo_hi else QColor("#DCDCE2"))
            p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
            p.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, format_freq(xf))

        # 6. 实时拖拽 / 悬停 HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            hud_text = ""
            hud_col = QColor("#28D8F0")
            if self._drag_mode == "xover" and 0 <= self._drag_index < len(xovers):
                hz = xovers[self._drag_index]
                hud_text = f"Xover {self._drag_index + 1}: {int(round(hz))} Hz"
            elif self._drag_mode == "thresh" and 0 <= self._drag_index < cnt:
                db = bands[self._drag_index]["threshold"]
                hud_text = f"Band {self._drag_index + 1} Threshold: {db:.1f} dB"
                hud_col = QColor(BAND_PALETTE[self._drag_index])

            if hud_text:
                hud_rect = QRectF(plot.left() + 8.0, plot.bottom() - 28.0, 185.0, 19.0)
                p.setPen(QPen(QColor("#141416"), 1.0))
                p.setBrush(QColor(21, 21, 24, 225))
                p.drawRoundedRect(hud_rect, 3.5, 3.5)
                p.setPen(hud_col)
                p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
                p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_text)

        p.restore()

        # 画布外框
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
        mode: str = "linear",
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

        self.knob = ProRotaryKnob(min_val, max_val, default_val, default_val, accent_color, mode=mode)
        self.knob.valueChanged.connect(self._on_knob_changed)
        lay.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(2, 0, 2, 0)
        hints_row.setSpacing(1)
        self._range_labels: list[QLabel] = []
        for h_text, align in (
            (min_hint, Qt.AlignmentFlag.AlignLeft),
            (mid_hint, Qt.AlignmentFlag.AlignCenter),
            (max_hint, Qt.AlignmentFlag.AlignRight),
        ):
            hl = QLabel(h_text)
            hl.setAlignment(align)
            hl.setStyleSheet("color: #787882; font-size: 8.5px;")
            hints_row.addWidget(hl, 1)
            self._range_labels.append(hl)
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

    def setRange(self, minimum: float, maximum: float, min_hint: str, max_hint: str) -> None:
        self._min = self.knob._min = minimum
        self._max = self.knob._max = maximum
        self._range_labels[0].setText(min_hint)
        self._range_labels[-1].setText(max_hint)
        self.setValue(self._val)

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
# 5. 底部单频段独立压缩器卡片 (MultibandBandCard)
# =====================================================================
class MultibandBandCard(QFrame):
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

        # 1. Header
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

        self.range_lbl = QLabel("20 – 140 Hz")
        self.range_lbl.setStyleSheet(f"color: #8A8A94; font-family: {MONO_FONT_CSS}; font-size: 9.5px;")
        h_lay.addWidget(self.range_lbl)

        h_lay.addStretch()

        self.btn_solo = QPushButton("S")
        self.btn_solo.setCheckable(True)
        self.btn_solo.setFixedSize(21, 20)
        self.btn_solo.setToolTip("Solo 监听当前频段")
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

        # 2. Body (2x3 旋钮矩阵)
        body = QFrame()
        body.setObjectName("BandBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(6, 7, 6, 7)
        b_lay.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(2)
        self.c_thresh = KnobParameterCell(
            "Threshold", -60.0, 0.0, -18.0, self._accent,
            "-60", "dB", "0.0",
            lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="linear"
        )
        self.c_ratio = KnobParameterCell(
            "Ratio", 1.0, 20.0, 2.5, self._accent,
            "1.0:1", "", "20:1",
            format_ratio, lambda s, fb: parse_ratio(s, fb), mode="log"
        )
        self.c_knee = KnobParameterCell(
            "Knee", 0.0, 100.0, 20.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        for c in (self.c_thresh, self.c_ratio, self.c_knee):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(2)
        self.c_attack = KnobParameterCell(
            "Attack", 0.10, 200.0, 10.0, self._accent,
            "0.10", "ms", "200",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_release = KnobParameterCell(
            "Release", 10.0, 2000.0, 120.0, self._accent,
            "10", "ms", "2000",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_makeup = KnobParameterCell(
            "Gain", -12.0, 18.0, 0.0, self._accent,
            "-12", "dB", "+18",
            format_db_signed, lambda s, fb: float(s), mode="linear"
        )
        for c in (self.c_attack, self.c_release, self.c_makeup):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)

        # 3. 底部交叉点微调条（除最后一个频段外均显示下上限交叉点编辑胶囊）
        xover_bar = QHBoxLayout()
        xover_bar.setContentsMargins(4, 2, 4, 0)
        xover_bar.setSpacing(6)
        self.xover_lbl = QLabel("High Xover")
        self.xover_lbl.setStyleSheet("color: #787882; font-size: 9.5px; font-weight: 600;")
        self.xover_pill = EditableValuePill("140 Hz", self._accent, width=68)
        self.xover_pill.valueEdited.connect(self._on_xover_pill_edited)
        xover_bar.addWidget(self.xover_lbl)
        xover_bar.addStretch()
        xover_bar.addWidget(self.xover_pill)
        b_lay.addLayout(xover_bar)

        root.addWidget(body, 1)

        # 连接信号
        self.c_thresh.valueChanged.connect(lambda v: self._set_field("threshold", round(v, 1)))
        self.c_ratio.valueChanged.connect(lambda v: self._set_field("ratio", round(v, 1)))
        self.c_knee.valueChanged.connect(lambda v: self._set_field("knee", round(v, 0)))
        self.c_attack.valueChanged.connect(lambda v: self._set_field("attack", round(v, 2)))
        self.c_release.valueChanged.connect(lambda v: self._set_field("release", round(v, 1)))
        self.c_makeup.valueChanged.connect(lambda v: self._set_field("makeup", round(v, 1)))

        self.syncFromData(self._data, 20.0, 140.0, has_upper_xover=True)

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

        self.c_thresh.setValue(self._data["threshold"])
        self.c_ratio.setValue(self._data["ratio"])
        self.c_knee.setValue(self._data["knee"])
        self.c_attack.setValue(self._data["attack"])
        self.c_release.setValue(self._data["release"])
        self.c_makeup.setValue(self._data["makeup"])

        for cell in (self.c_thresh, self.c_ratio, self.c_knee, self.c_attack, self.c_release, self.c_makeup):
            cell.setModuleActive(en)

        if has_upper_xover:
            self.xover_lbl.setText(f"Xover {self._idx + 1}")
            self.xover_pill.setText(f"{int(round(high_hz))} Hz")
            self.xover_pill.setModuleActive(True)
            self.xover_pill.setEnabled(True)
        else:
            self.xover_lbl.setText("Top Band")
            self.xover_pill.setText("20.0 kHz")
            self.xover_pill.setModuleActive(False)
            self.xover_pill.setEnabled(False)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.bandChanged.emit(self._idx, copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
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
        hz = parse_freq(text, 1000.0)
        self.crossoverEdited.emit(self._idx, hz)


# =====================================================================
# 6. 主面板：多段压缩器 (Studio Multiband Compressor)
# =====================================================================
class StudioMultibandCompWidget(QWidget):
    """
    多段压缩器主界面，提供完整信号接口供后端 DSP 引擎订阅：
      - bypassChanged(bool)
      - bandCountChanged(int)
      - crossoversChanged(list)
      - phaseModeChanged(str, str)     # (phase_mode, slope)
      - bandChanged(int, dict)         # (band_index, band_params)
      - soloChanged(list)              # 当前处于 Solo 状态的频段索引列表
      - outputGainChanged(float)
      - stateChanged(dict)             # 完整参数树快照
    """
    bypassChanged = pyqtSignal(bool)
    bandCountChanged = pyqtSignal(int)
    crossoversChanged = pyqtSignal(list)
    phaseModeChanged = pyqtSignal(str, str)
    bandChanged = pyqtSignal(int, dict)
    soloChanged = pyqtSignal(list)
    outputGainChanged = pyqtSignal(float)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Master Bus", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Multiband Compressor — {track_name}")
        self.resize(1120, 720)
        self.setMinimumSize(980, 640)

        self._active_bank = "A"
        self._selected_band = 0
        self._processor_controls = False
        self._banks = {
            "A": default_multiband_demo(),
            "B": default_multiband_flat(),
        }

        self._presets = {
            "Bus Low-End & Air (3-Band)": copy.deepcopy(self._banks["A"]),
            "Flat Bypass": copy.deepcopy(self._banks["B"]),
            "Vocal Mid Density (3-Band)": {
                "enabled": True,
                "band_count": 3,
                "crossovers": [180.0, 3200.0, 8000.0],
                "phase_mode": "Min Phase",
                "slope": "24 dB/oct",
                "output_gain": 1.0,
                "bands": [
                    _make_band(True, threshold=-24.0, ratio=3.0, knee=20.0, attack=15.0, release=120.0, makeup=-0.5),
                    _make_band(True, threshold=-18.0, ratio=2.8, knee=35.0, attack=6.0,  release=95.0,  makeup=1.5),
                    _make_band(True, threshold=-21.0, ratio=3.2, knee=15.0, attack=1.2,  release=65.0,  makeup=0.0),
                    _make_band(True, threshold=-24.0, ratio=2.0, knee=20.0, attack=1.5,  release=70.0,  makeup=0.0),
                ],
            },
            "Mastering Glue (4-Band Linear)": {
                "enabled": True,
                "band_count": 4,
                "crossovers": [110.0, 650.0, 4200.0],
                "phase_mode": "Linear Phase",
                "slope": "48 dB/oct",
                "output_gain": 0.5,
                "bands": [
                    _make_band(True, threshold=-18.0, ratio=2.2, knee=30.0, attack=25.0, release=160.0, makeup=0.5),
                    _make_band(True, threshold=-16.0, ratio=1.8, knee=40.0, attack=15.0, release=130.0, makeup=0.0),
                    _make_band(True, threshold=-17.0, ratio=2.0, knee=35.0, attack=10.0, release=100.0, makeup=0.5),
                    _make_band(True, threshold=-20.0, ratio=2.4, knee=25.0, attack=4.0,  release=80.0,  makeup=0.8),
                ],
            },
            "2-Band Tight Bass & Top": {
                "enabled": True,
                "band_count": 2,
                "crossovers": [220.0, 2200.0, 8000.0],
                "phase_mode": "Linear Phase",
                "slope": "24 dB/oct",
                "output_gain": 0.0,
                "bands": [
                    _make_band(True, threshold=-19.0, ratio=4.0, knee=20.0, attack=20.0, release=150.0, makeup=1.0),
                    _make_band(True, threshold=-16.0, ratio=2.0, knee=30.0, attack=8.0,  release=100.0, makeup=0.5),
                    _make_band(True, threshold=-22.0, ratio=2.0, knee=20.0, attack=3.0,  release=85.0,  makeup=0.0),
                    _make_band(True, threshold=-24.0, ratio=2.0, knee=20.0, attack=1.5,  release=70.0,  makeup=0.0),
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
            ProVerticalMeter, OutputFaderStrip, ProMultibandGraphWidget {{
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
        # 1. 顶部主控与多段拓扑配置栏
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

        title_lbl = QLabel(f"MULTIBAND COMP  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(8)

        # 预设管理
        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存当前多段配置为预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(185)
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

        tb_lay.addSpacing(10)

        # 2-4 频段数量分段选择器
        bands_lbl = QLabel("Bands:")
        bands_lbl.setStyleSheet("color: #8A8A94; font-size: 10px; font-weight: 600;")
        tb_lay.addWidget(bands_lbl)

        self._band_count_btns: dict[int, QPushButton] = {}
        bc_wrap = QFrame()
        bc_wrap.setStyleSheet("background-color: #18181B; border: 1px solid #121214; border-radius: 3px;")
        bc_lay = QHBoxLayout(bc_wrap)
        bc_lay.setContentsMargins(2, 2, 2, 2)
        bc_lay.setSpacing(2)
        for cnt in (2, 3, 4):
            btn = QPushButton(str(cnt))
            btn.setFixedSize(24, 18)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, c=cnt: self._on_band_count_changed(c))
            bc_lay.addWidget(btn)
            self._band_count_btns[cnt] = btn
        tb_lay.addWidget(bc_wrap)

        tb_lay.addSpacing(10)

        # 段间相位线性化与分频斜率控制
        phase_lbl = QLabel("Phase:")
        phase_lbl.setStyleSheet("color: #8A8A94; font-size: 10px; font-weight: 600;")
        tb_lay.addWidget(phase_lbl)

        self.phase_combo = ChevronComboBox()
        self.phase_combo.setFixedWidth(104)
        self.phase_combo.addItems(["Min Phase", "Linear Phase"])
        self.phase_combo.setToolTip("选择分频网络相位模式：最小相位（零延迟）或线性相位（消除段间相位失真）")
        self.phase_combo.currentTextChanged.connect(self._on_phase_changed)
        tb_lay.addWidget(self.phase_combo)

        self.slope_combo = ChevronComboBox()
        self.slope_combo.setFixedWidth(82)
        self.slope_combo.addItems(["12 dB/oct", "24 dB/oct", "48 dB/oct"])
        self.slope_combo.setToolTip("分频滤波器滚降斜率")
        self.slope_combo.currentTextChanged.connect(self._on_slope_changed)
        tb_lay.addWidget(self.slope_combo)

        self.latency_badge = QLabel("FIR · 23.2 ms")
        self.latency_badge.setStyleSheet(f"color: #28D8F0; font-family: {MONO_FONT_CSS}; font-size: 9.5px; font-weight: 600;")
        tb_lay.addWidget(self.latency_badge)
        self._design_toolbar_controls = (bands_lbl, bc_wrap, phase_lbl, self.phase_combo, self.slope_combo,
                                         self.latency_badge)

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
        # 箭头按钮样式由 _update_top_bar_styles 统一设置（勿在入布局前绑定 objectName QSS，
        # 否则 Qt6 基线对齐分组会把它排到行底，相对 A/B 下沉 2px）

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_multiband)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：Input | Multiband Spectrum & Graph | Band GRs | Output | Out Gain
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
        self.meter_input = ProVerticalMeter(mode="level", show_ticks=True)
        in_col.addWidget(self.meter_input, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(in_col)
        up_lay.addWidget(_vsep())

        # 2.2 多段频谱与动态响应画布
        self.mb_graph = ProMultibandGraphWidget()
        self.mb_graph.crossoverDragged.connect(self._on_graph_xover_dragged)
        self.mb_graph.bandThresholdDragged.connect(self._on_graph_thresh_dragged)
        self.mb_graph.bandSelected.connect(self._on_band_selected)
        self.mb_graph.bandSoloToggled.connect(self._on_graph_solo_toggled)
        self.mb_graph.bandMuteToggled.connect(self._on_graph_mute_toggled)
        up_lay.addWidget(self.mb_graph, 1)
        up_lay.addWidget(_vsep())

        # 2.3 各频段独立 Gain Reduction 衰减表 (B1 ~ B4)
        gr_col = self._build_meter_column("Band GR")
        gr_row = QHBoxLayout()
        gr_row.setContentsMargins(4, 0, 4, 0)
        gr_row.setSpacing(4)
        self.band_gr_meters: list[ProVerticalMeter] = []
        for i in range(4):
            m = ProVerticalMeter(
                title=f"B{i + 1}",
                mode="gr",
                show_ticks=(i == 0),
                accent_color=BAND_PALETTE[i],
            )
            self.band_gr_meters.append(m)
            gr_row.addWidget(m)
        gr_col.addLayout(gr_row, 1)
        up_lay.addLayout(gr_col)
        up_lay.addWidget(_vsep())

        # 2.4 Output 电平表
        out_col = self._build_meter_column("Output")
        self.meter_output = ProVerticalMeter(mode="level", show_ticks=True)
        out_col.addWidget(self.meter_output, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)
        gain_separator = _vsep()
        up_lay.addWidget(gain_separator)

        # 2.5 Output 增益推子
        og_col = self._build_meter_column("Out Gain")
        self.out_fader = OutputFaderStrip()
        self.out_fader.gainChanged.connect(self._on_output_gain_changed)
        og_col.addWidget(self.out_fader, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(og_col)
        self._design_output_controls = (gain_separator, og_col.itemAt(0).widget(), self.out_fader)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部 2–4 频段独立压缩器卡片矩阵
        # -------------------------------------------------------------
        cards_wrap = QWidget()
        cards_lay = QHBoxLayout(cards_wrap)
        cards_lay.setContentsMargins(0, 0, 0, 0)
        cards_lay.setSpacing(0)

        self.band_cards: list[MultibandBandCard] = []
        st = self._current_state()
        for i in range(4):
            card = MultibandBandCard(i, st["bands"][i])
            card.bandChanged.connect(self._on_band_card_changed)
            card.crossoverEdited.connect(self._on_card_xover_edited)
            card.selected.connect(self._on_band_selected)
            self.band_cards.append(card)
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

    def use_processor_controls(self) -> None:
        self._processor_controls = True
        self.set_sample_rate(48000.0)
        self.mb_graph.use_processor_controls()
        for control in (*self._design_toolbar_controls[2:], *self._design_output_controls):
            control.hide()
        with QSignalBlocker(self.preset_combo):
            self.preset_combo.clear()
            self.preset_combo.addItems(list(self._presets))
        for card in self.band_cards:
            card.btn_solo.hide()
            card.c_ratio.setRange(1.2, 20.0, "1.2:1", "20:1")
            card.c_attack.setRange(0.7, 1000.0, "0.7", "1000")
            card.c_release.setRange(50.0, 5000.0, "50", "5000")
            card.c_makeup.setRange(0.0, 24.0, "0", "+24")
        self._refresh_all_views()

    def set_sample_rate(self, sample_rate: float) -> None:
        self._crossover_ceiling_hz = min(20000.0, sample_rate * 0.45)

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

        is_lin = (st["phase_mode"] == "Linear Phase")
        self.latency_badge.setText("FIR · 23.2 ms" if is_lin else "ZERO · 0.0 ms")
        self.latency_badge.setStyleSheet(
            f"color: {'#28D8F0' if is_lin else '#787882'}; font-family: {MONO_FONT_CSS}; font-size: 9.5px; font-weight: 600;"
        )

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
        self.phase_combo.blockSignals(True)
        self.phase_combo.setCurrentText(st["phase_mode"])
        self.phase_combo.blockSignals(False)

        self.slope_combo.blockSignals(True)
        self.slope_combo.setCurrentText(st["slope"])
        self.slope_combo.blockSignals(False)

        self.out_fader.setGain(st["output_gain"])
        self._update_top_bar_styles()

        # 更新各频段频率区间与卡片可见性
        edges = [20.0] + xovers[: cnt - 1] + [20000.0]
        any_solo = any(st["bands"][i]["solo"] for i in range(cnt))

        for i in range(4):
            visible = (i < cnt)
            self.band_cards[i].setVisible(visible)
            self.band_gr_meters[i].setVisible(visible)
            if visible:
                b = st["bands"][i]
                audible = st["enabled"] and (not b["mute"]) and (not any_solo or b["solo"])
                self.band_gr_meters[i].setActive(audible and b["enabled"])
                self.band_cards[i].syncFromData(
                    b,
                    low_hz=edges[i],
                    high_hz=edges[i + 1],
                    has_upper_xover=(i < cnt - 1),
                )
                self.band_cards[i].setSelected(i == self._selected_band)

        self.mb_graph.setSelectedBand(self._selected_band)
        self.mb_graph.setState(st)

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

    def _on_band_count_changed(self, count: int):
        st = self._current_state()
        if st["band_count"] != count:
            if self._processor_controls and count > st["band_count"]:
                crossovers = st["crossovers"]
                lower = 30.0
                for index in range(count - 1):
                    upper = self._crossover_ceiling_hz - (count - 2 - index)
                    frequency = crossovers[index]
                    if index >= st["band_count"] - 1 and frequency < lower:
                        frequency = (lower + upper) / 2.0
                    crossovers[index] = min(max(frequency, lower), upper)
                    lower = crossovers[index] + 1.0
            st["band_count"] = count
            if self._selected_band >= count:
                self._selected_band = count - 1
            self._refresh_all_views()
            self.bandCountChanged.emit(count)
            self._emit_solo_list()
            self.stateChanged.emit(self.get_state())

    def _on_phase_changed(self, mode: str):
        st = self._current_state()
        st["phase_mode"] = mode
        self._update_top_bar_styles()
        self.phaseModeChanged.emit(mode, st["slope"])
        self.stateChanged.emit(self.get_state())

    def _on_slope_changed(self, slope: str):
        st = self._current_state()
        st["slope"] = slope
        self.phaseModeChanged.emit(st["phase_mode"], slope)
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
        self.mb_graph.setSelectedBand(idx)

    def _on_band_card_changed(self, idx: int, band_data: dict):
        st = self._current_state()
        st["bands"][idx] = band_data
        self._refresh_all_views()
        self.bandChanged.emit(idx, copy.deepcopy(band_data))
        self._emit_solo_list()
        self.stateChanged.emit(self.get_state())

    def _on_graph_thresh_dragged(self, idx: int, thresh_db: float):
        st = self._current_state()
        st["bands"][idx]["threshold"] = thresh_db
        self._refresh_all_views()
        self.bandChanged.emit(idx, copy.deepcopy(st["bands"][idx]))
        self.stateChanged.emit(self.get_state())

    def _on_graph_solo_toggled(self, idx: int):
        if self._processor_controls:
            return
        st = self._current_state()
        st["bands"][idx]["solo"] = not st["bands"][idx]["solo"]
        self._refresh_all_views()
        self.bandChanged.emit(idx, copy.deepcopy(st["bands"][idx]))
        self._emit_solo_list()
        self.stateChanged.emit(self.get_state())

    def _on_graph_mute_toggled(self, idx: int):
        if self._processor_controls:
            return
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
        low_bound = 30.0 if xover_idx == 0 else xovers[xover_idx - 1] * 1.18
        high_bound = 18000.0 if xover_idx >= cnt - 2 else xovers[xover_idx + 1] / 1.18
        clamped = max(low_bound, min(high_bound, raw_hz))
        xovers[xover_idx] = round(clamped, 1)
        self._refresh_all_views()
        self.crossoversChanged.emit(list(xovers[: cnt - 1]))
        self.stateChanged.emit(self.get_state())

    def _load_preset(self, name: str):
        if name in self._presets:
            self._banks[self._active_bank] = copy.deepcopy(self._presets[name])
            self._selected_band = 0
            self._refresh_all_views()
            self._emit_solo_list()
            self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存 Multiband 预设", "输入预设名称:")
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

        # 生成模拟 64 点粉红噪声风格音乐频谱
        spec_dbs = []
        for i in range(64):
            norm = i / 63.0
            base = -14.0 - norm * 22.0
            ripple = (
                4.5 * math.sin(self._anim_t * 1.4 + norm * 9.0)
                + 3.0 * math.cos(self._anim_t * 2.3 - norm * 15.0)
                + random.uniform(-1.5, 1.5)
            )
            spec_dbs.append(max(-58.0, min(-2.0, base + ripple)))

        in_db = -14.0 + 7.5 * math.sin(self._anim_t * 1.2) + random.uniform(-1.2, 1.2)
        band_grs = [0.0, 0.0, 0.0, 0.0]

        for i in range(cnt):
            b = st["bands"][i]
            audible = st["enabled"] and (not b["mute"]) and (not any_solo or b["solo"])
            if audible and b["enabled"]:
                band_energy = in_db - i * 2.2 + 2.5 * math.sin(self._anim_t * (1.1 + i * 0.35))
                over = max(0.0, band_energy - b["threshold"])
                gr = over * (1.0 - 1.0 / max(1.01, b["ratio"]))
                band_grs[i] = gr
            self.band_gr_meters[i].setValue(band_grs[i])

        avg_gr = sum(band_grs[:cnt]) / max(1, cnt)
        out_db = in_db - avg_gr + (st["output_gain"] if st["enabled"] else 0.0)

        self.meter_input.setValue(in_db)
        self.meter_output.setValue(out_db)
        self.mb_graph.setRealtimeData(band_grs, spec_dbs)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_multiband(self):
        self._banks[self._active_bank] = default_multiband_flat()
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
        band_grs_db: list[float],
        output_db: float,
        spectrum_dbs: list[float] | None = None,
    ):
        """
        接入真实音频引擎实时数据 (每帧 30~60Hz 调用)：
          - input_db: 输入总电平 (dBFS)
          - band_grs_db: 各频段增益衰减量列表 [gr_b1, gr_b2, ...] (正值 dB)
          - output_db: 输出总电平 (dBFS)
          - spectrum_dbs: 可选，对数分布的实时 FFT 频谱幅值列表 (dBFS)
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()

        self.meter_input.setValue(input_db)
        self.meter_output.setValue(output_db)
        for i in range(min(4, len(band_grs_db))):
            self.band_gr_meters[i].setValue(band_grs_db[i])
        self.mb_graph.setRealtimeData(band_grs_db, spectrum_dbs)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioMultibandCompWidget("Master Bus")
    win.show()
    sys.exit(app.exec())
