from __future__ import annotations

import sys
import math
import copy
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QSizePolicy, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont
)

from src.shared.ui.widgets.chevron_combo import ChevronComboBox


# =====================================================================
# 1. 全局色彩系统与默认参数配置（与 EQ-UI.py 保持同一视觉规范）
# =====================================================================
# 与 AudioClipEditor-UI / EQ-UI 统一的达芬奇 Fairlight 视觉系统：
# #27272B 哑光中性灰基底 + 1px #141416 纯黑细线切分，字体栈一致
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":    "#28D8F0",  # 主控 / A-B 激活 / Make Up 指示
    "eg":      "#4CD490",  # Expander / Gate 模块主题色（翡翠绿）
    "comp":    "#F0A848",  # Compressor 模块主题色（琥珀金）
    "lim":     "#F26D6D",  # Limiter 模块主题色（珊瑚红）
    "sc":      "#6495FA",  # Sidechain 侧链主题色（钴蓝）
}


def default_dynamics_demo() -> dict:
    return {
        "enabled": True,
        "makeup": 0.0,
        "eg": {
            "enabled": True,
            "mode": "Expander",   # "Expander" | "Gate"
            "threshold": -35.0,   # -50.0 ~ 0.0 dB
            "range": 18.0,        # 0.0 ~ 60.0 dB
            "ratio": 1.8,         # 1:1.1 ~ 1:3.0 (Gate 模式下自动为高比率)
            "attack": 1.4,        # 0.50 ~ 100.0 ms
            "hold": 0.0,          # 0.00 ~ 4000.0 ms
            "release": 93.0,      # 50.0 ~ 4000.0 ms
        },
        "comp": {
            "enabled": True,
            "threshold": -15.0,   # -50.0 ~ 0.0 dB
            "ratio": 2.5,         # 1.2:1 ~ 20.0:1
            "knee": 15.0,         # 0 ~ 100
            "mix": 100.0,         # 0 ~ 100 % (Wet)
            "attack": 1.4,        # 0.70 ~ 100.0 ms
            "hold": 0.0,          # 0.00 ~ 4000.0 ms
            "release": 93.0,      # 50.0 ~ 4000.0 ms
            "sc_on": False,
            "sc_listen": False,
            "sc_source": "Internal",
        },
        "lim": {
            "enabled": True,
            "threshold": -6.0,    # -30.0 ~ 0.0 dB
            "attack": 0.71,       # 0.70 ~ 30.0 ms
            "hold": 0.0,          # 0.00 ~ 4000.0 ms
            "release": 93.0,      # 50.0 ~ 4000.0 ms
        },
    }


def default_dynamics_flat() -> dict:
    st = default_dynamics_demo()
    st["eg"]["enabled"] = False
    st["comp"]["enabled"] = False
    st["lim"]["enabled"] = False
    st["makeup"] = 0.0
    return st


def format_ms(val: float) -> str:
    if val < 1.0:
        return f"{val:.2f}"
    if val < 10.0:
        return f"{val:.1f}"
    return f"{int(round(val))}"


def format_ratio_comp(val: float) -> str:
    if val >= 10.0:
        return f"{val:.0f}:1"
    return f"{val:.1f}:1"


def format_ratio_eg(val: float) -> str:
    return f"1:{val:.1f}"


def parse_ratio_value(text: str, is_eg: bool, fallback: float) -> float:
    s = text.strip().replace(" ", "")
    try:
        if ":" in s:
            left, right = s.split(":", 1)
            return float(right) if is_eg else float(left)
        return float(s)
    except ValueError:
        return fallback


# =====================================================================
# 2. 控件：现代发光电源开关（支持模块专属主题色）
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

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 58, parent=None):
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
            clean = self._text.replace("dB", "").replace("%", "").replace("ms", "").strip()
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
# 4. 控件：拉丝阳极铝质感旋钮（支持 linear / log / power 映射）
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
            # 适合 0 ~ 4000 ms 这种起点为 0 的大跨度时间参数
            ratio = max(0.0, min(1.0, (val - self._min) / (self._max - self._min)))
            return ratio ** (1.0 / 2.6)
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        n = max(0.0, min(1.0, norm))
        if self._mode == "log":
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return mn * ((mx / mn) ** n)
        if self._mode == "power":
            return self._min + (n ** 2.6) * (self._max - self._min)
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

        # 2. 模块主题色光环
        arc_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(QPen(arc_col, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        span_deg = curr_deg - start_deg
        if abs(span_deg) > 0.5:
            p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        # 3. 旋钮黑边凹槽 + 达芬奇风格哑光浅灰旋钮顶盖
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
# 5. 控件：垂直高精电平表（支持 Input/Output 三色梯度 & Gain Reduction 衰减表）
# =====================================================================
class ProVerticalMeter(QWidget):
    def __init__(
        self,
        title: str = "",
        mode: str = "level",      # "level" (自下而上) | "gr" (自上而下衰减)
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
        self._ticks = ticks or ([0, -5, -10, -15, -20, -30, -40, -50] if mode == "level" else [0, -3, -6, -10, -14, -20, -30, -50])
        self._show_ticks = show_ticks
        self._accent = QColor(accent_color)

        self._value_db = min_db if mode == "level" else 0.0
        self._peak_db = self._value_db
        self._clipped = False
        self._active = True

        self.setFixedWidth(44 if show_ticks else 18)
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
            # Gain reduction: 0 dB (无衰减) 到 -50 dB (最大衰减)
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
        # 对 -20 ~ 0 dB 区间给予略高的视觉分辨率，贴近专业调音台表头手感
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
        bar_w = 10.0
        bar_x = (self.width() - bar_w - 3.0) if self._show_ticks else (self.width() - bar_w) / 2.0
        bar = QRectF(bar_x, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))

        # 1. 刻度文字
        if self._show_ticks:
            p.setFont(QFont(MONO_FONT_FAM, 7))
            for t in self._ticks:
                ty = self._db_to_y(float(t), bar)
                p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
                lbl = f"{t} -" if t == 0 else f"{t} -"
                p.drawText(
                    QRectF(0, ty - 7, bar.left() - 3, 14),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    lbl,
                )

        # 2. 表槽底色
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        # 3. 电平填充
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

                # 峰值保持线
                if self._peak_db > self._min_db + 0.5:
                    py = self._db_to_y(self._peak_db, bar)
                    p.setPen(QPen(QColor("#F26D6D") if self._peak_db > -10.0 else QColor("#F0F2F6"), 1.4))
                    p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

                # 顶部削波指示灯
                clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
                p.drawRoundedRect(clip_rect, 1.0, 1.0)
            else:
                # Gain Reduction 自上而下
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

        # 4. 精细横向刻度栅格遮罩（保留科技感但区别于达芬奇粗块）
        p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
        step_y = 3.0
        gy = bar.top() + step_y
        while gy < bar.bottom():
            p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
            gy += step_y

        # 5. 底部标识（如 E/G, C, L）
        if self._title:
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
            p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, self._title)


# =====================================================================
# 6. 控件：Make Up 增益推子（风格统一自 EQ-UI.py MasterOutputStrip）
# =====================================================================
class MakeUpFaderStrip(QWidget):
    gainChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gain = 0.0
        self._dragging = False
        self.setFixedWidth(56)
        self.setMinimumHeight(180)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setGain(self, val: float, emit: bool = False):
        clamped = max(0.0, min(20.0, round(val, 1)))
        if abs(clamped - self._gain) > 1e-4:
            self._gain = clamped
            self.update()
            if emit:
                self.gainChanged.emit(self._gain)

    def _track_rect(self) -> QRectF:
        return QRectF(34.0, 10.0, 4.0, max(40.0, self.height() - 36.0))

    def _y_to_gain(self, y: float) -> float:
        tr = self._track_rect()
        norm = max(0.0, min(1.0, (y - tr.top()) / tr.height()))
        return 20.0 * (1.0 - norm)

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

        # 左侧刻度 (+20, +15, +10, +5, 0)
        p.setFont(QFont(MONO_FONT_FAM, 7))
        for val in (20, 15, 10, 5, 0):
            norm = (20.0 - val) / 20.0
            ty = tr.top() + norm * tr.height()
            p.setPen(QColor("#9A9AA2"))
            lbl = f"+{val} -" if val > 0 else "0 -"
            p.drawText(QRectF(0, ty - 7, tr.left() - 9, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        # 推子凹槽
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        # 增益激活亮条
        norm = (20.0 - self._gain) / 20.0
        cy = tr.top() + norm * tr.height()
        if self._gain > 0.05:
            active_rect = QRectF(tr.left() + 0.5, cy, tr.width() - 1.0, tr.bottom() - cy)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(40, 216, 240, 150))
            p.drawRoundedRect(active_rect, 1.5, 1.5)

        # 推子帽
        cap_w, cap_h = 18.0, 22.0
        cap_rect = QRectF(cx - cap_w / 2.0, cy - cap_h / 2.0, cap_w, cap_h)
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if self._dragging else "#7A7C82"))
        p.drawRoundedRect(cap_rect, 3.0, 3.0)

        p.setPen(QPen(QColor("#28D8F0"), 1.6))
        p.drawLine(QPointF(cap_rect.left() + 3, cy), QPointF(cap_rect.right() - 3, cy))

        # 底部数字读数
        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor("#28D8F0"))
        val_str = f"+{self._gain:.1f}" if self._gain >= 0.05 else "0.0"
        p.drawText(QRectF(4, self.height() - 21, self.width() - 4, 18), Qt.AlignmentFlag.AlignCenter, val_str)


# =====================================================================
# 7. 控件：交互式动态传输特性曲线图（I/O Transfer Curve + 阈值拖拽 + 实时样本点）
# =====================================================================
class ProDynamicsGraphWidget(QWidget):
    egThresholdDragged = pyqtSignal(float)
    compThresholdDragged = pyqtSignal(float)
    limThresholdDragged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(250, 220)
        self.setMouseTracking(True)

        self._state = default_dynamics_demo()
        self.makeup_after_limiter = True
        self._dragging_target = ""   # "eg" | "comp" | "lim" | ""
        self._hover_target = ""
        self._mouse_pos: QPointF | None = None

        # 实时样本历史轨迹 [(in_db, out_db), ...]
        self._trail_points: list[tuple[float, float]] = []

        self._ml = 32.0
        self._mr = 12.0
        self._mt = 10.0
        self._mb = 22.0

    def setState(self, state: dict):
        self._state = state
        self.update()

    def pushSample(self, input_db: float, output_db: float) -> None:
        if input_db <= -100.0 and output_db <= -100.0:
            if self._trail_points:
                self._trail_points.clear()
                self.update()
            return
        in_clamped = max(-80.0, min(0.0, input_db))
        out_clamped = max(-80.0, min(0.0, output_db))
        self._trail_points.append((in_clamped, out_clamped))
        if len(self._trail_points) > 26:
            self._trail_points.pop(0)
        self.update()

    def _plot_rect(self) -> QRectF:
        # 保持接近正方形的高精度 1:1 视觉比例并居中
        avail_w = max(80.0, self.width() - self._ml - self._mr)
        avail_h = max(80.0, self.height() - self._mt - self._mb)
        side = min(avail_w, avail_h * 1.15)
        left = self._ml + (avail_w - side) / 2.0
        return QRectF(left, self._mt, side, avail_h)

    def _db_to_x(self, db: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (db + 80.0) / 80.0))
        return plot.left() + norm * plot.width()

    def _x_to_db(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return -80.0 + norm * 80.0

    def _db_to_y(self, db: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (0.0 - db) / 80.0))
        return plot.top() + norm * plot.height()

    def _y_to_db(self, y: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (y - plot.top()) / plot.height()))
        return -norm * 80.0

    # -----------------------------------------------------------------
    # Steady-state transfer. The measured trail may differ during attack/hold/release.
    # -----------------------------------------------------------------
    def evaluate_transfer(self, in_db: float) -> float:
        st = self._state
        if not st["enabled"]:
            return in_db

        y = in_db

        # 1. Expander / Gate
        eg = st["eg"]
        if eg["enabled"]:
            t_eg = eg["threshold"]
            if y < t_eg:
                if eg["mode"] == "Gate":
                    y -= eg["range"]
                else:
                    y -= min(eg["range"], (t_eg - y) * (eg["ratio"] - 1.0))

        # 2. Compressor (含 Soft Knee 与 Dry/Wet Mix)
        comp = st["comp"]
        if comp["enabled"]:
            t_c = comp["threshold"]
            r_c = max(1.01, comp["ratio"])
            knee_db = (comp["knee"] / 100.0) * 24.0
            mix = comp["mix"] / 100.0
            d = y - t_c

            if knee_db <= 0.1:
                y_comp = (t_c + d / r_c) if d > 0.0 else y
            else:
                if 2.0 * d < -knee_db:
                    y_comp = y
                elif 2.0 * abs(d) <= knee_db:
                    y_comp = y + (1.0 / r_c - 1.0) * ((d + knee_db / 2.0) ** 2) / (2.0 * knee_db)
                else:
                    y_comp = t_c + d / r_c

            wet_gain = 10.0 ** ((y_comp - y) / 20.0)
            y += 20.0 * math.log10((1.0 - mix) + mix * wet_gain)

        if not self.makeup_after_limiter and comp["enabled"]:
            y += st["makeup"]

        lim = st["lim"]
        if lim["enabled"]:
            y = min(y, lim["threshold"])
        if self.makeup_after_limiter:
            y += st["makeup"]

        return max(-80.0, min(0.0, y))

    # -----------------------------------------------------------------
    # 阈值线命中检测与拖拽交互
    # -----------------------------------------------------------------
    def _hit_test_threshold(self, pos: QPointF, plot: QRectF) -> str:
        st = self._state
        if not st["enabled"]:
            return ""
        tol = 7.0
        # 优先检测 Limiter（水平线）
        if st["lim"]["enabled"]:
            ly = self._db_to_y(st["lim"]["threshold"], plot)
            if abs(pos.y() - ly) <= tol and plot.left() <= pos.x() <= plot.right():
                return "lim"
        # 检测 Compressor（垂直线）
        if st["comp"]["enabled"]:
            cx = self._db_to_x(st["comp"]["threshold"], plot)
            if abs(pos.x() - cx) <= tol and plot.top() <= pos.y() <= plot.bottom():
                return "comp"
        # 检测 Expander / Gate（垂直线）
        if st["eg"]["enabled"]:
            ex = self._db_to_x(st["eg"]["threshold"], plot)
            if abs(pos.x() - ex) <= tol and plot.top() <= pos.y() <= plot.bottom():
                return "eg"
        return ""

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_target = ""
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            plot = self._plot_rect()
            hit = self._hit_test_threshold(event.position(), plot)
            if hit:
                self._dragging_target = hit

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._dragging_target == "eg":
            val = max(-50.0, min(0.0, round(self._x_to_db(event.position().x(), plot), 1)))
            self.egThresholdDragged.emit(val)
        elif self._dragging_target == "comp":
            val = max(-60.0, min(0.0, round(self._x_to_db(event.position().x(), plot), 1)))
            self.compThresholdDragged.emit(val)
        elif self._dragging_target == "lim":
            val = max(-30.0, min(0.0, round(self._y_to_db(event.position().y(), plot), 1)))
            self.limThresholdDragged.emit(val)
        else:
            hit = self._hit_test_threshold(event.position(), plot)
            self._hover_target = hit
            if hit in ("eg", "comp"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif hit == "lim":
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging_target = ""
            self.update()

    # -----------------------------------------------------------------
    # 绘制网格、1:1 基准线、阈值准星、主响应曲线与动态信号点
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 背景渐变（与 EQ-UI.py 完全一致）
        bg_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#1D1D20"))
        bg_grad.setColorAt(1.0, QColor("#1D1D20"))
        p.fillRect(plot, bg_grad)

        # 2. 网格线 (-60, -40, -20, 0 dB)
        grid_pen = QPen(QColor("#18181C"), 1.0)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        for db in (0, -20, -40, -60):
            y = self._db_to_y(float(db), plot)
            x = self._db_to_x(float(db), plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

            p.setPen(QColor("#9A9AA2"))
            p.drawText(QRectF(0, y - 8, plot.left() - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(db))
            p.drawText(QRectF(x - 16, plot.bottom() + 4, 32, 16), Qt.AlignmentFlag.AlignCenter, str(db))

        p.setPen(QColor("#9A9AA2"))
        p.drawText(QRectF(plot.left() - 22, plot.bottom() + 4, 20, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "dB")
        p.drawText(QRectF(plot.right() + 6, plot.top() - 2, 24, 16), Qt.AlignmentFlag.AlignLeft, "1:1")

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        # 3. 1:1 对角基准线
        p.setPen(QPen(QColor("#3A3A40"), 1.1, Qt.PenStyle.DashLine))
        p.drawLine(plot.bottomLeft(), plot.topRight())

        st = self._state
        enabled = st["enabled"]

        # 4. 模块阈值参考线（可拖拽交互）
        if enabled:
            if st["eg"]["enabled"]:
                ex = self._db_to_x(st["eg"]["threshold"], plot)
                hi = (self._hover_target == "eg" or self._dragging_target == "eg")
                col = QColor(COLORS["eg"])
                col.setAlpha(230 if hi else 135)
                p.setPen(QPen(col, 1.8 if hi else 1.1))
                p.drawLine(QPointF(ex, plot.top()), QPointF(ex, plot.bottom()))

            if st["comp"]["enabled"]:
                cx = self._db_to_x(st["comp"]["threshold"], plot)
                hi = (self._hover_target == "comp" or self._dragging_target == "comp")
                col = QColor(COLORS["comp"])
                col.setAlpha(230 if hi else 135)
                p.setPen(QPen(col, 1.8 if hi else 1.1))
                p.drawLine(QPointF(cx, plot.top()), QPointF(cx, plot.bottom()))

            if st["lim"]["enabled"]:
                ly = self._db_to_y(st["lim"]["threshold"], plot)
                hi = (self._hover_target == "lim" or self._dragging_target == "lim")
                col = QColor(COLORS["lim"])
                col.setAlpha(230 if hi else 135)
                p.setPen(QPen(col, 1.8 if hi else 1.1))
                p.drawLine(QPointF(plot.left(), ly), QPointF(plot.right(), ly))

        # 5. 动态传输主曲线 + 柔和渐变填充
        curve_path = QPainterPath()
        fill_path = QPainterPath()
        steps = 200
        for i in range(steps + 1):
            in_db = -80.0 + (i / steps) * 80.0
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
            area_grad.setColorAt(0.0, QColor(40, 216, 240, 32))
            area_grad.setColorAt(1.0, QColor(40, 216, 240, 4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(area_grad)
            p.drawPath(fill_path)

            # 发光外晕 + 锐利主线
            p.setPen(QPen(QColor(40, 216, 240, 55), 4.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(curve_path)
            p.setPen(QPen(QColor("#28D8F0"), 2.0))
            p.drawPath(curve_path)
        else:
            p.setPen(QPen(QColor("#8A8A92"), 1.6))
            p.drawPath(curve_path)

        # 6. 阈值交点控制手柄（便于用户直观发现可拖拽点）
        if enabled:
            handles = []
            if st["eg"]["enabled"]:
                tx = st["eg"]["threshold"]
                handles.append(("eg", COLORS["eg"], QPointF(self._db_to_x(tx, plot), self._db_to_y(self.evaluate_transfer(tx), plot))))
            if st["comp"]["enabled"]:
                tx = st["comp"]["threshold"]
                handles.append(("comp", COLORS["comp"], QPointF(self._db_to_x(tx, plot), self._db_to_y(self.evaluate_transfer(tx), plot))))
            if st["lim"]["enabled"]:
                ty = st["lim"]["threshold"]
                handles.append(("lim", COLORS["lim"], QPointF(self._db_to_x(ty, plot), self._db_to_y(ty, plot))))

            for tag, hex_col, pt in handles:
                is_hi = (self._hover_target == tag or self._dragging_target == tag)
                r = 5.5 if is_hi else 4.2
                c = QColor(hex_col)
                if is_hi:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(c.red(), c.green(), c.blue(), 70))
                    p.drawEllipse(pt, r + 4.0, r + 4.0)
                p.setPen(QPen(c, 1.6))
                p.setBrush(QColor("#151518"))
                p.drawEllipse(pt, r, r)

        # 7. 实时信号动态轨迹点 + 坐标轴分布刻度
        if enabled and self._trail_points:
            n = len(self._trail_points)
            for idx, (s_in, s_out) in enumerate(self._trail_points):
                age = (idx + 1) / n
                sx = self._db_to_x(s_in, plot)
                sy = self._db_to_y(s_out, plot)

                # 底部输入与左侧输出瞬时脉冲刻度
                tick_col = QColor(40, 216, 240, int(180 * age))
                p.setPen(QPen(tick_col, 1.2))
                p.drawLine(QPointF(sx, plot.bottom() - 5.0), QPointF(sx, plot.bottom()))
                p.drawLine(QPointF(plot.left(), sy), QPointF(plot.left() + 5.0, sy))

                # 曲线上的信号轨迹点
                dot_r = 1.6 + 2.4 * age
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(175, 245, 255, int(220 * age)))
                p.drawEllipse(QPointF(sx, sy), dot_r, dot_r)

        # 8. 鼠标悬停 / 拖拽实时 HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            if self._dragging_target == "eg":
                hud_str = f"Exp/Gate Thresh: {st['eg']['threshold']:.1f} dB"
                hud_col = QColor(COLORS["eg"])
            elif self._dragging_target == "comp":
                hud_str = f"Comp Thresh: {st['comp']['threshold']:.1f} dB"
                hud_col = QColor(COLORS["comp"])
            elif self._dragging_target == "lim":
                hud_str = f"Limiter Ceiling: {st['lim']['threshold']:.1f} dB"
                hud_col = QColor(COLORS["lim"])
            else:
                in_v = self._x_to_db(mx, plot)
                out_v = self.evaluate_transfer(in_v)
                hud_str = f"In {in_v:.1f} dB  →  Out {out_v:.1f} dB"
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
# 8. 辅助工厂：构建统一风格的“标签 + 旋钮 + 上下限提示 + 可编辑数值胶囊”单元
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
        pill_width: int = 58,
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
        self.title_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        lay.addWidget(self.title_lbl)

        self.knob = ProRotaryKnob(min_val, max_val, default_val, default_val, accent_color, mode=mode)
        self.knob.valueChanged.connect(self._on_knob_changed)
        lay.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(2, 0, 2, 0)
        hints_row.setSpacing(4)
        hints = QWidget()
        hints.setLayout(hints_row)
        hints.setMaximumWidth(90)
        for h_text, align in (
            (min_hint, Qt.AlignmentFlag.AlignLeft),
            (mid_hint, Qt.AlignmentFlag.AlignCenter),
            (max_hint, Qt.AlignmentFlag.AlignRight),
        ):
            hl = QLabel(h_text)
            hl.setAlignment(align)
            hl.setStyleSheet("color: #787882; font-size: 9px;")
            hints_row.addWidget(hl, 1)
        lay.addWidget(hints)

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
# 9. 模块卡片 1：Expander / Gate Card
# =====================================================================
class ExpanderGateCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["eg"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 7, 10, 6)
        h_lay.setSpacing(8)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        # Expander / Gate 双态切换分段按钮
        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.btn_exp = QPushButton("Expander")
        self.btn_gate = QPushButton("Gate")
        for b in (self.btn_exp, self.btn_gate):
            b.setCheckable(True)
            b.setFixedHeight(20)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_lay.addWidget(b, 1)

        self.btn_exp.clicked.connect(lambda: self._on_mode("Expander"))
        self.btn_gate.clicked.connect(lambda: self._on_mode("Gate"))
        self._mode_segment = seg_wrap
        h_lay.addWidget(seg_wrap, 1)
        root.addWidget(header)

        # Body (2x3 旋钮矩阵)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_thresh = KnobParameterCell(
            "Threshold", -50.0, 0.0, -35.0, self._accent,
            "-50.0", "dB", "0.0",
            lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="linear"
        )
        self.c_range = KnobParameterCell(
            "Range", 0.0, 60.0, 18.0, self._accent,
            "0.0", "dB", "60.0",
            lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="linear"
        )
        self.c_ratio = KnobParameterCell(
            "Ratio", 1.1, 3.0, 1.8, self._accent,
            "1:1.1", "", "1:3.0",
            format_ratio_eg, lambda s, fb: parse_ratio_value(s, True, fb), mode="linear"
        )
        for c in (self.c_thresh, self.c_range, self.c_ratio):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_attack = KnobParameterCell(
            "Attack", 0.50, 100.0, 1.4, self._accent,
            "0.50", "ms", "100",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_hold = KnobParameterCell(
            "Hold", 0.0, 4000.0, 0.0, self._accent,
            "0.00", "ms", "4000",
            format_ms, lambda s, fb: float(s), mode="power"
        )
        self.c_release = KnobParameterCell(
            "Release", 50.0, 4000.0, 93.0, self._accent,
            "50", "ms", "4000",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        for c in (self.c_attack, self.c_hold, self.c_release):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        self._settings_layout = b_lay
        self._setting_rows = (row1, row2)
        root.addWidget(body, 1)

        self.c_thresh.valueChanged.connect(lambda v: self._set_field("threshold", round(v, 1)))
        self.c_range.valueChanged.connect(lambda v: self._set_field("range", round(v, 1)))
        self.c_ratio.valueChanged.connect(lambda v: self._set_field("ratio", round(v, 1)))
        self.c_attack.valueChanged.connect(lambda v: self._set_field("attack", round(v, 2)))
        self.c_hold.valueChanged.connect(lambda v: self._set_field("hold", round(v, 2)))
        self.c_release.valueChanged.connect(lambda v: self._set_field("release", round(v, 1)))

        self.syncFromData(self._data)

    def use_single_row(self) -> None:
        first, second = self._setting_rows
        for cell in (self.c_attack, self.c_hold, self.c_release):
            second.removeWidget(cell)
            first.addWidget(cell, 1)
        self._settings_layout.removeItem(second)
        self._mode_segment.setMaximumWidth(240)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        is_exp = (self._data["mode"] == "Expander")

        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
                border-radius: 0px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)

        self.power_sw.setChecked(en)
        self.btn_exp.setChecked(is_exp)
        self.btn_gate.setChecked(not is_exp)

        active_seg = f"""
            QPushButton {{
                background-color: #182E38;
                color: {self._accent if en else '#9AA0B0'};
                border: 1px solid {self._accent if en else '#3A3A40'};
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
            }}
        """
        inactive_seg = """
            QPushButton {
                background-color: transparent;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
        """
        self.btn_exp.setStyleSheet(active_seg if is_exp else inactive_seg)
        self.btn_gate.setStyleSheet(active_seg if not is_exp else inactive_seg)

        self.c_thresh.setValue(self._data["threshold"])
        self.c_range.setValue(self._data["range"])
        self.c_ratio.setValue(self._data["ratio"])
        self.c_attack.setValue(self._data["attack"])
        self.c_hold.setValue(self._data["hold"])
        self.c_release.setValue(self._data["release"])

        for cell in (self.c_thresh, self.c_range, self.c_ratio, self.c_attack, self.c_hold, self.c_release):
            cell.setModuleActive(en)
        # Gate 模式下 Ratio 自动锁死为门限陡降，视觉弱化提示
        if not is_exp:
            self.c_ratio.setModuleActive(False)

        self._updating = False

    def _emit(self):
        if not self._updating:
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
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 10. 模块卡片 2：Compressor Card (含 Sidechain 控制区)
# =====================================================================
class CompressorCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["comp"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 7, 12, 6)
        h_lay.setSpacing(8)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Compressor")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()
        root.addWidget(header)

        # Body (2x4 网格)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_thresh = KnobParameterCell(
            "Threshold", -60.0, 0.0, -15.0, self._accent,
            "-60.0", "dB", "0.0",
            lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="linear"
        )
        self.c_ratio = KnobParameterCell(
            "Ratio", 1.2, 20.0, 2.5, self._accent,
            "1.2:1", "", "20:1",
            format_ratio_comp, lambda s, fb: parse_ratio_value(s, False, fb), mode="log"
        )
        self.c_knee = KnobParameterCell(
            "Knee", 0.0, 100.0, 15.0, self._accent,
            "0", "", "100",
            lambda v: f"{int(round(v))}", lambda s, fb: float(s), mode="linear"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 100.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        for c in (self.c_thresh, self.c_ratio, self.c_knee, self.c_mix):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_attack = KnobParameterCell(
            "Attack", 0.70, 1000.0, 1.4, self._accent,
            "0.70", "ms", "1000",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_hold = KnobParameterCell(
            "Hold", 0.0, 4000.0, 0.0, self._accent,
            "0.00", "ms", "4000",
            format_ms, lambda s, fb: float(s), mode="power"
        )
        self.c_release = KnobParameterCell(
            "Release", 50.0, 5000.0, 93.0, self._accent,
            "50", "ms", "5000",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        for c in (self.c_attack, self.c_hold, self.c_release):
            row2.addWidget(c, 1)

        # Sidechain 控制列
        sc_col = QVBoxLayout()
        sc_col.setContentsMargins(4, 2, 2, 2)
        sc_col.setSpacing(5)

        sc_lbl = QLabel("Sidechain")
        sc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sc_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        sc_col.addWidget(sc_lbl)

        self.btn_sc_on = QPushButton("On")
        self.btn_sc_on.setCheckable(True)
        self.btn_sc_on.setFixedHeight(20)
        self.btn_sc_on.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sc_on.clicked.connect(self._on_sc_on)
        sc_col.addWidget(self.btn_sc_on)

        self.btn_sc_listen = QPushButton("Listen")
        self.btn_sc_listen.setCheckable(True)
        self.btn_sc_listen.setFixedHeight(20)
        self.btn_sc_listen.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sc_listen.clicked.connect(self._on_sc_listen)
        sc_col.addWidget(self.btn_sc_listen)

        self.sc_source_combo = ChevronComboBox()
        self.sc_source_combo.setFixedHeight(21)
        self.sc_source_combo.addItems(["Internal", "Dialogue Bus", "Music Bus", "Audio 2", "Audio 3"])
        self.sc_source_combo.currentIndexChanged.connect(self._on_sc_source)
        sc_col.addWidget(self.sc_source_combo)

        row2.addLayout(sc_col, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_thresh.valueChanged.connect(lambda v: self._set_field("threshold", round(v, 1)))
        self.c_ratio.valueChanged.connect(lambda v: self._set_field("ratio", round(v, 1)))
        self.c_knee.valueChanged.connect(lambda v: self._set_field("knee", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))
        self.c_attack.valueChanged.connect(lambda v: self._set_field("attack", round(v, 2)))
        self.c_hold.valueChanged.connect(lambda v: self._set_field("hold", round(v, 2)))
        self.c_release.valueChanged.connect(lambda v: self._set_field("release", round(v, 1)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])

        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
                border-radius: 0px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)

        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        self.c_thresh.setValue(self._data["threshold"])
        self.c_ratio.setValue(self._data["ratio"])
        self.c_knee.setValue(self._data["knee"])
        self.c_mix.setValue(self._data["mix"])
        self.c_attack.setValue(self._data["attack"])
        self.c_hold.setValue(self._data["hold"])
        self.c_release.setValue(self._data["release"])

        for cell in (self.c_thresh, self.c_ratio, self.c_knee, self.c_mix, self.c_attack, self.c_hold, self.c_release):
            cell.setModuleActive(en)

        sc_on = bool(self._data["sc_on"])
        sc_listen = bool(self._data["sc_listen"])
        self.btn_sc_on.setChecked(sc_on)
        self.btn_sc_listen.setChecked(sc_listen)
        source_index = self.sc_source_combo.findData(self._data["sc_source"])
        if source_index >= 0:
            self.sc_source_combo.setCurrentIndex(source_index)
        else:
            self.sc_source_combo.setCurrentText(self._data["sc_source"])

        pill_btn_css = f"""
            QPushButton {{
                background-color: #323238;
                color: #7E8392;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #182E38;
                color: {COLORS['sc']};
                border: 1px solid {COLORS['sc']};
            }}
        """
        self.btn_sc_on.setStyleSheet(pill_btn_css)
        self.btn_sc_listen.setStyleSheet(pill_btn_css.replace(COLORS["sc"], "#E8B838"))

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_sc_on(self, checked: bool):
        self._data["sc_on"] = checked
        self._emit()

    def _on_sc_listen(self, checked: bool):
        self._data["sc_listen"] = checked
        self._emit()

    def _on_sc_source(self, _index: int) -> None:
        self._data["sc_source"] = self.sc_source_combo.currentData() or self.sc_source_combo.currentText()
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 11. 模块卡片 3：Limiter Card
# =====================================================================
class LimiterCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["lim"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 7, 12, 6)
        h_lay.setSpacing(8)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Limiter")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        badge = QLabel("BRICKWALL")
        badge.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.6px;")
        h_lay.addWidget(badge)
        root.addWidget(header)

        # Body
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        self.c_thresh = KnobParameterCell(
            "Threshold", -30.0, 0.0, -6.0, self._accent,
            "-30.0", "dB", "0.0",
            lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="linear"
        )
        row1.addWidget(self.c_thresh, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_attack = KnobParameterCell(
            "Attack", 0.70, 30.0, 0.71, self._accent,
            "0.70", "ms", "30",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_hold = KnobParameterCell(
            "Hold", 0.0, 4000.0, 0.0, self._accent,
            "0.00", "ms", "4000",
            format_ms, lambda s, fb: float(s), mode="power"
        )
        self.c_release = KnobParameterCell(
            "Release", 50.0, 4000.0, 93.0, self._accent,
            "50", "ms", "4000",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        row1.addWidget(self.c_attack, 1)
        row2.addWidget(self.c_hold, 1)
        row2.addWidget(self.c_release, 1)
        b_lay.addLayout(row2)
        self._settings_layout = b_lay
        self._setting_rows = (row1, row2)
        root.addWidget(body, 1)

        self.c_thresh.valueChanged.connect(lambda v: self._set_field("threshold", round(v, 1)))
        self.c_attack.valueChanged.connect(lambda v: self._set_field("attack", round(v, 2)))
        self.c_hold.valueChanged.connect(lambda v: self._set_field("hold", round(v, 2)))
        self.c_release.valueChanged.connect(lambda v: self._set_field("release", round(v, 1)))

        self.syncFromData(self._data)

    def use_single_row(self) -> None:
        first, second = self._setting_rows
        for cell in (self.c_hold, self.c_release):
            second.removeWidget(cell)
            first.addWidget(cell, 1)
        self._settings_layout.removeItem(second)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])

        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
                border-radius: 0px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)

        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        self.c_thresh.setValue(self._data["threshold"])
        self.c_attack.setValue(self._data["attack"])
        self.c_hold.setValue(self._data["hold"])
        self.c_release.setValue(self._data["release"])

        for cell in (self.c_thresh, self.c_attack, self.c_hold, self.c_release):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 12. 主面板：自主旗舰动态处理器 (Studio Dynamics Processor)
# =====================================================================
class StudioDynamicsWidget(QWidget):
    # 对接后端音频引擎的完整信号接口
    bypassChanged = pyqtSignal(bool)
    expanderChanged = pyqtSignal(dict)
    compressorChanged = pyqtSignal(dict)
    limiterChanged = pyqtSignal(dict)
    makeUpChanged = pyqtSignal(float)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Audio 1", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Dynamics Processor — {track_name}")
        self.resize(980, 690)
        self.setMinimumSize(900, 620)

        self._active_bank = "A"
        self._single_stage: str | None = None
        self._banks = {
            "A": default_dynamics_demo(),
            "B": default_dynamics_flat(),
        }

        self._presets = {
            "Dialogue Control": copy.deepcopy(self._banks["A"]),
            "Flat Bypass": copy.deepcopy(self._banks["B"]),
            "Broadcast Voice Leveler": {
                "enabled": True,
                "makeup": 3.5,
                "eg": {"enabled": True, "mode": "Gate", "threshold": -42.0, "range": 24.0, "ratio": 2.5, "attack": 1.0, "hold": 45.0, "release": 140.0},
                "comp": {"enabled": True, "threshold": -18.0, "ratio": 3.2, "knee": 25.0, "mix": 100.0, "attack": 2.5, "hold": 0.0, "release": 110.0, "sc_on": False, "sc_listen": False, "sc_source": "Internal"},
                "lim": {"enabled": True, "threshold": -1.5, "attack": 0.70, "hold": 0.0, "release": 60.0},
            },
            "Music Ducking (Sidechain)": {
                "enabled": True,
                "makeup": 0.0,
                "eg": {"enabled": False, "mode": "Expander", "threshold": -35.0, "range": 18.0, "ratio": 1.5, "attack": 1.4, "hold": 0.0, "release": 93.0},
                "comp": {"enabled": True, "threshold": -24.0, "ratio": 4.5, "knee": 35.0, "mix": 100.0, "attack": 5.0, "hold": 20.0, "release": 220.0, "sc_on": True, "sc_listen": False, "sc_source": "Dialogue Bus"},
                "lim": {"enabled": True, "threshold": -3.0, "attack": 0.80, "hold": 0.0, "release": 80.0},
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
                font-family: 'Helvetica Neue', 'PingFang SC';
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ProVerticalMeter, MakeUpFaderStrip, ProDynamicsGraphWidget {{
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
        # 1. 顶部整合工具栏（与 EQ-UI.py 完全统一）
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

        title_lbl = QLabel(f"DYNAMICS PROCESSOR  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(12)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存为新预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(195)
        self.preset_combo.addItems(list(self._presets.keys()))
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        tb_lay.addWidget(self.preset_combo)

        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setFixedSize(21, 22)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb_lay.addWidget(prev_btn)

        next_btn = QPushButton("›")
        next_btn.setObjectName("ToolBtn")
        next_btn.setFixedSize(21, 22)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb_lay.addWidget(next_btn)

        tb_lay.addSpacing(10)

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
        # 箭头按钮样式由 _update_ab_styles 统一设置（勿在入布局前绑定 objectName QSS，
        # 否则 Qt6 基线对齐分组会把它排到行底，相对 A/B 下沉 2px）

        tb_lay.addStretch()

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_dynamics)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：Input | I/O 传输曲线图 | Gain Reduction | Make Up | Output | Sidechain
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

        # 2.2 动态传输特性曲线图
        self.dyn_graph = ProDynamicsGraphWidget()
        self.dyn_graph.egThresholdDragged.connect(self._on_graph_eg_thresh)
        self.dyn_graph.compThresholdDragged.connect(self._on_graph_comp_thresh)
        self.dyn_graph.limThresholdDragged.connect(self._on_graph_lim_thresh)
        up_lay.addWidget(self.dyn_graph, 4)
        up_lay.addWidget(_vsep())

        # 2.3 Gain Reduction 三通道衰减表 (E/G · C · L)
        gr_col = self._build_meter_column("Gain Reduction")
        gr_meters_row = QHBoxLayout()
        gr_meters_row.setSpacing(6)
        self.meter_gr_eg = ProVerticalMeter(title="E/G", mode="gr", show_ticks=True, accent_color=COLORS["eg"])
        self.meter_gr_comp = ProVerticalMeter(title="C", mode="gr", show_ticks=True, accent_color=COLORS["comp"])
        self.meter_gr_lim = ProVerticalMeter(title="L", mode="gr", show_ticks=True, accent_color=COLORS["lim"])
        gr_meters_row.addWidget(self.meter_gr_eg)
        gr_meters_row.addWidget(self.meter_gr_comp)
        gr_meters_row.addWidget(self.meter_gr_lim)
        gr_col.addLayout(gr_meters_row, 1)
        up_lay.addLayout(gr_col)
        up_lay.addWidget(_vsep())

        # 2.4 Make Up 补偿增益推子
        mu_col = self._build_meter_column("Make Up")
        self.makeup_fader = MakeUpFaderStrip()
        self.makeup_fader.gainChanged.connect(self._on_makeup_changed)
        mu_col.addWidget(self.makeup_fader, 1, Qt.AlignmentFlag.AlignHCenter)
        self._makeup_column = QWidget()
        self._makeup_column.setLayout(mu_col)
        up_lay.addWidget(self._makeup_column)
        self._makeup_separator = _vsep()
        up_lay.addWidget(self._makeup_separator)

        # 2.5 Output 与 Sidechain 电平表
        out_col = self._build_meter_column("Output")
        self.meter_output = ProVerticalMeter(mode="level", show_ticks=True)
        out_col.addWidget(self.meter_output, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)
        self._sidechain_separator = _vsep()
        up_lay.addWidget(self._sidechain_separator)

        sc_col = self._build_meter_column("Sidechain")
        self.meter_sidechain = ProVerticalMeter(mode="level", show_ticks=True)
        sc_col.addWidget(self.meter_sidechain, 1, Qt.AlignmentFlag.AlignHCenter)
        self._sidechain_column = QWidget()
        self._sidechain_column.setLayout(sc_col)
        up_lay.addWidget(self._sidechain_column)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部三模块处理卡片矩阵 (Expander/Gate | Compressor | Limiter)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        st = self._current_state()
        self.eg_card = ExpanderGateCard(st["eg"])
        self.eg_card.changed.connect(self._on_eg_changed)

        self.comp_card = CompressorCard(st["comp"])
        self.comp_card.changed.connect(self._on_comp_changed)

        self.lim_card = LimiterCard(st["lim"])
        self.lim_card.changed.connect(self._on_lim_changed)

        # 比例按旋钮列数分配 (3列 : 4列 : 3列)，视觉重心高度均衡
        cards_row.addWidget(self.eg_card, 3)
        cards_row.addWidget(self.comp_card, 4)
        cards_row.addWidget(self.lim_card, 3)

        main_lay.addLayout(cards_row)

    def show_stage(self, stage: str) -> None:
        """Show one processor with its curve, controls and corresponding meters."""
        self._single_stage = stage
        cards = {"eg": self.eg_card, "comp": self.comp_card, "lim": self.lim_card}
        meters = {"eg": self.meter_gr_eg, "comp": self.meter_gr_comp, "lim": self.meter_gr_lim}
        for name, card in cards.items():
            card.setVisible(name == stage)
            card.power_sw.hide()
            meters[name].setVisible(name == stage)

        for widget in (self._makeup_column, self._makeup_separator, self._sidechain_column, self._sidechain_separator):
            widget.setVisible(stage == "comp")
        if stage == "eg":
            self.eg_card.use_single_row()
        elif stage == "lim":
            self.lim_card.use_single_row()

    def _build_meter_column(self, header_text: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(3, 0, 3, 0)
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

    def _update_ab_styles(self):
        active_css = """
            QPushButton {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
            }
        """
        inactive_css = """
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
        self.btn_a.setStyleSheet(active_css if self._active_bank == "A" else inactive_css)
        self.btn_b.setStyleSheet(active_css if self._active_bank == "B" else inactive_css)
        self.btn_copy_ab.setStyleSheet(inactive_css)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

    def _refresh_all_views(self):
        st = self._current_state()
        if self._single_stage is not None:
            for stage in ("eg", "comp", "lim"):
                st[stage]["enabled"] = stage == self._single_stage
            if self._single_stage != "comp":
                st["makeup"] = 0.0
        self._update_ab_styles()
        self.power_switch.setChecked(st["enabled"])
        self.makeup_fader.setGain(st["makeup"])
        self.eg_card.syncFromData(st["eg"])
        self.comp_card.syncFromData(st["comp"])
        self.lim_card.syncFromData(st["lim"])
        self.meter_sidechain.setActive(bool(st["comp"]["sc_on"]))
        self.dyn_graph.setState(st)

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
        self.dyn_graph.setState(st)
        self.bypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_eg_changed(self, eg_data: dict):
        st = self._current_state()
        st["eg"] = eg_data
        self.dyn_graph.setState(st)
        self.expanderChanged.emit(copy.deepcopy(eg_data))
        self.stateChanged.emit(self.get_state())

    def _on_comp_changed(self, comp_data: dict):
        st = self._current_state()
        st["comp"] = comp_data
        self.meter_sidechain.setActive(bool(comp_data["sc_on"]))
        self.dyn_graph.setState(st)
        self.compressorChanged.emit(copy.deepcopy(comp_data))
        self.stateChanged.emit(self.get_state())

    def _on_lim_changed(self, lim_data: dict):
        st = self._current_state()
        st["lim"] = lim_data
        self.dyn_graph.setState(st)
        self.limiterChanged.emit(copy.deepcopy(lim_data))
        self.stateChanged.emit(self.get_state())

    def _on_makeup_changed(self, gain: float):
        st = self._current_state()
        st["makeup"] = gain
        self.dyn_graph.setState(st)
        self.makeUpChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _on_graph_eg_thresh(self, val: float):
        st = self._current_state()
        st["eg"]["threshold"] = val
        self.eg_card.syncFromData(st["eg"])
        self.dyn_graph.setState(st)
        self.expanderChanged.emit(copy.deepcopy(st["eg"]))
        self.stateChanged.emit(self.get_state())

    def _on_graph_comp_thresh(self, val: float):
        st = self._current_state()
        st["comp"]["threshold"] = val
        self.comp_card.syncFromData(st["comp"])
        self.dyn_graph.setState(st)
        self.compressorChanged.emit(copy.deepcopy(st["comp"]))
        self.stateChanged.emit(self.get_state())

    def _on_graph_lim_thresh(self, val: float):
        st = self._current_state()
        st["lim"]["threshold"] = val
        self.lim_card.syncFromData(st["lim"])
        self.dyn_graph.setState(st)
        self.limiterChanged.emit(copy.deepcopy(st["lim"]))
        self.stateChanged.emit(self.get_state())

    def _load_preset(self, name: str):
        if name in self._presets:
            self._banks[self._active_bank] = copy.deepcopy(self._presets[name])
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存 Dynamics 预设", "输入预设名称:")
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
        self._anim_t += 0.09
        st = self._current_state()
        # 模拟人声/音乐起伏动态 (-42 dBFS ~ -4 dBFS)
        wave = (
            -19.0
            + 11.5 * math.sin(self._anim_t * 1.1)
            + 5.5 * math.cos(self._anim_t * 2.7)
            + random.uniform(-2.2, 2.2)
        )
        in_db = max(-55.0, min(-2.0, wave))
        out_db = self.dyn_graph.evaluate_transfer(in_db)

        gr_eg = max(0.0, (st["eg"]["threshold"] - in_db) * 0.5) if (st["enabled"] and st["eg"]["enabled"] and in_db < st["eg"]["threshold"]) else 0.0
        gr_comp = max(0.0, (in_db - st["comp"]["threshold"]) * (1.0 - 1.0 / st["comp"]["ratio"])) if (st["enabled"] and st["comp"]["enabled"] and in_db > st["comp"]["threshold"]) else 0.0
        pre_lim = in_db - gr_eg - gr_comp + st["makeup"]
        gr_lim = max(0.0, pre_lim - st["lim"]["threshold"]) if (st["enabled"] and st["lim"]["enabled"] and pre_lim > st["lim"]["threshold"]) else 0.0

        sc_db = in_db - 4.0 if st["comp"]["sc_on"] else -55.0

        self.meter_input.setValue(in_db)
        self.meter_gr_eg.setValue(gr_eg)
        self.meter_gr_comp.setValue(gr_comp)
        self.meter_gr_lim.setValue(gr_lim)
        self.meter_output.setValue(out_db)
        self.meter_sidechain.setValue(sc_db)
        self.dyn_graph.pushSample(in_db, out_db)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_dynamics(self):
        self._banks[self._active_bank] = default_dynamics_flat()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_meters(
        self,
        input_db: float,
        gr_eg_db: float,
        gr_comp_db: float,
        gr_lim_db: float,
        output_db: float,
        sidechain_db: float = -60.0,
    ):
        """接入真实音频引擎实时电平与增益衰减值 (dBFS / dB)"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.meter_input.setValue(input_db)
        self.meter_gr_eg.setValue(gr_eg_db)
        self.meter_gr_comp.setValue(gr_comp_db)
        self.meter_gr_lim.setValue(gr_lim_db)
        self.meter_output.setValue(output_db)
        self.meter_sidechain.setValue(sidechain_db)
        self.dyn_graph.pushSample(input_db, output_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioDynamicsWidget("Audio 1")
    win.show()
    sys.exit(app.exec())
