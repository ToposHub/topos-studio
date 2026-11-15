from __future__ import annotations
# ─────────────────────────────────────────────────────────────────────
# NF-02（2026-11-15 计划 §6/§7）：用户原型 VoiceChanger-UI.py 的生产
# 接入副本。视觉与交互保留用户原创风格；仅按计划 §6.2 做最小适配：
# - 构造增加 demo=False 参数（生产不跑假数据定时器）；
# - 移除独立运行入口（__main__）；
# - 嵌套 state ↔ 扁平 Params 的唯一映射表在 voice_changer_dialog.py。
# ─────────────────────────────────────────────────────────────────────

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
    QPainterPath, QFont, QPolygonF
)


# =====================================================================
# 1. 全局色彩系统与声学常量（与 EQ-UI.py / Dyamics-UI.py 完全统一）
# =====================================================================
COLORS = {
    "cyan":     "#28D8F0",  # 主控 / A-B 激活 / 可懂度指示
    "formant":  "#4CD490",  # Formant Shifter 模块主题色（翡翠绿）
    "pitch":    "#F0A848",  # Pitch Shifter 模块主题色（琥珀金）
    "vocoder":  "#B67CF8",  # Vocoder 模块主题色（紫罗兰）
    "coral":    "#F26D6D",  # 削波 / 高位警戒色
}

FREQ_MIN = 80.0
FREQ_MAX = 12000.0
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def freq_to_note(hz: float) -> str:
    if hz <= 0:
        return "--"
    midi = round(69 + 12 * math.log2(hz / 440.0))
    name = NOTE_NAMES[midi % 12]
    octave = (midi // 12) - 1
    return f"{name}{octave}"


def format_st(val: float) -> str:
    if abs(val) < 0.05:
        return "0.0 st"
    return f"{val:+.1f} st"


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


def format_hz(val: float) -> str:
    return f"{int(round(val))} Hz"


def parse_numeric(text: str, fallback: float) -> float:
    s = (
        text.strip()
        .lower()
        .replace("st", "")
        .replace("cents", "")
        .replace("ct", "")
        .replace("hz", "")
        .replace("ms", "")
        .replace("%", "")
        .strip()
    )
    try:
        return float(s)
    except ValueError:
        return fallback


def default_voice_demo() -> dict:
    return {
        "enabled": True,
        "output_gain": 0.0,
        "formant": {
            "enabled": True,
            "mode": "Natural",    # "Natural" | "Crisp"
            "shift": 4.2,         # -12.0 ~ +12.0 st (核心共振峰偏移)
            "throat": 20.0,       # -100 ~ +100 % (声道共鸣腔尺寸)
            "gender": 45.0,       # -100 ~ +100 % (非对称 F1/F2 性别塑形)
            "preserve": 90.0,     # 0 ~ 100 % (清辅音与齿音直通保护，保障语言可懂度)
            "timbre": 12.0,       # -50 ~ +50 % (频谱明暗倾斜)
            "mix": 100.0,         # 0 ~ 100 %
        },
        "pitch": {
            "enabled": True,
            "tracking": "Vocal",  # "Vocal" | "Wide"
            "semitones": 4.0,     # -24.0 ~ +24.0 st (基频 F0 移调)
            "fine": 0.0,          # -100 ~ +100 cents
            "formant_link": 0.0,  # 0 ~ 100 % (音调与共振峰联动比例)
            "glide": 18.0,        # 0 ~ 200 ms (基频过渡平滑)
            "jitter": 0.0,        # 0 ~ 100 % (声带微颤抖动，适合老人/怪兽)
            "mix": 100.0,         # 0 ~ 100 %
        },
        "vocoder": {
            "enabled": False,
            "carrier": "Saw",     # "Saw" | "Pulse" | "Choir" | "Noise"
            "bands": 16,          # 8 ~ 32 (频带数量，越高越清晰)
            "carrier_hz": 115.0,  # 50 ~ 440 Hz (机械载波基频)
            "formant": 0.0,       # -12.0 ~ +12.0 st (声码器滤波器偏移)
            "unvoiced": 72.0,     # 0 ~ 100 % (清音/气音穿透比例，保障机器人可懂度)
            "release": 24.0,      # 5 ~ 500 ms (包络跟随速度)
            "mix": 100.0,         # 0 ~ 100 %
        },
    }


def default_voice_neutral() -> dict:
    st = default_voice_demo()
    st["formant"]["enabled"] = True
    st["formant"]["shift"] = 0.0
    st["formant"]["throat"] = 0.0
    st["formant"]["gender"] = 0.0
    st["formant"]["preserve"] = 90.0
    st["formant"]["timbre"] = 0.0

    st["pitch"]["enabled"] = False
    st["pitch"]["semitones"] = 0.0
    st["pitch"]["fine"] = 0.0
    st["pitch"]["formant_link"] = 0.0
    st["pitch"]["jitter"] = 0.0

    st["vocoder"]["enabled"] = False
    st["output_gain"] = 0.0
    return st


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

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 62, parent=None):
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
                background-color: #0B0C0E;
                color: {accent_color};
                border: 1px solid {accent_color};
                border-radius: 3.5px;
                font-family: 'SF Pro Display', 'Segoe UI', 'PingFang SC', 'Microsoft YaHei';
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
                self._text.replace("st", "")
                .replace("ct", "")
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
        p.setPen(QPen(QColor("#282A32"), 1.0))
        p.setBrush(QColor("#101114"))
        p.drawRoundedRect(rect, 3.5, 3.5)

        p.setPen(QColor("#F0F2F6") if self._active else QColor("#7A7E8A"))
        font = QFont("SF Pro Display", 9)
        font.setWeight(QFont.Weight.DemiBold)
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
            return ratio ** (1.0 / 2.2)
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        n = max(0.0, min(1.0, norm))
        if self._mode == "log":
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return mn * ((mx / mn) ** n)
        if self._mode == "power":
            return self._min + (n ** 2.2) * (self._max - self._min)
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
        p.setPen(QPen(QColor("#111216"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性模式在 12 点钟方向绘制中心基准刻度点
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#6E7485"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 2. 模块主题色光环
        arc_col = self._accent if self._active else QColor("#565864")
        p.setPen(QPen(arc_col, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._mode == "bipolar":
            zero_norm = max(0.0, min(1.0, (0.0 - self._min) / max(1e-6, self._max - self._min)))
            zero_deg = start_deg + zero_norm * total_span
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        # 3. 旋钮主体（拉丝阳极铝径向渐变）
        knob_r = 11.0
        grad = QRadialGradient(cx - knob_r * 0.35, cy - knob_r * 0.35, knob_r * 1.45)
        if self._hovered or self._dragging:
            grad.setColorAt(0.0, QColor("#555966"))
            grad.setColorAt(0.65, QColor("#343742"))
            grad.setColorAt(1.0, QColor("#202229"))
        else:
            grad.setColorAt(0.0, QColor("#464954"))
            grad.setColorAt(0.65, QColor("#2B2D36"))
            grad.setColorAt(1.0, QColor("#1B1D23"))

        p.setPen(QPen(QColor("#101115"), 1.2))
        p.setBrush(grad)
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        p.setPen(QPen(QColor(255, 255, 255, 26), 0.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), knob_r - 1.2, knob_r - 1.2)

        # 4. 发光指针刻度线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.5
        iy1 = cy - math.sin(rad) * 3.5
        ix2 = cx + math.cos(rad) * (knob_r - 1.5)
        iy2 = cy - math.sin(rad) * (knob_r - 1.5)
        ptr_col = self._accent if self._active else QColor("#8A8C96")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 5. 控件：垂直高精电平/声学状态指示条
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
            p.setFont(QFont("SF Pro Display", 7))
            ticks = [0, -10, -20, -35, -50] if self._mode == "level" else [100, 75, 50, 25, 0]
            for t in ticks:
                norm = self._val_to_norm(float(t))
                ty = bar.bottom() - norm * bar.height()
                p.setPen(QColor("#787D8C") if self._active else QColor("#4A4D58"))
                p.drawText(
                    QRectF(0, ty - 7, bar.left() - 3, 14),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{t} -",
                )

        # 表槽底色
        p.setPen(QPen(QColor("#282B35"), 1.0))
        p.setBrush(QColor("#0D0E12"))
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
                p.setPen(QPen(QColor("#F0F2F6"), 1.3))
                p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

            if self._mode == "level":
                clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#FF4848") if self._clipped else QColor("#22252E"))
                p.drawRoundedRect(clip_rect, 1.0, 1.0)

        # 精细刻度栅格遮罩
        p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
        gy = bar.top() + 3.0
        while gy < bar.bottom():
            p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
            gy += 3.0

        if self._title:
            p.setFont(QFont("SF Pro Display", 8))
            p.setPen(QColor("#8E94A4") if self._active else QColor("#545864"))
            p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, self._title)


# =====================================================================
# 6. 控件：右侧 Output 推子（风格统一自 EQ-UI.py MasterOutputStrip）
# =====================================================================
class MasterOutputStrip(QWidget):
    gainChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gain = 0.0
        self._dragging = False
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

        p.setPen(QPen(QColor("#525664"), 1.0))
        for i in range(5):
            ty = tr.top() + (i / 4.0) * tr.height()
            p.drawLine(QPointF(cx - 13, ty), QPointF(cx - 9, ty))
            p.drawLine(QPointF(cx + 9, ty), QPointF(cx + 13, ty))

        p.setPen(QPen(QColor("#282B34"), 1.0))
        p.setBrush(QColor("#0E1014"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        norm = (20.0 - self._gain) / 40.0
        cy = tr.top() + norm * tr.height()
        cap_w, cap_h = 18.0, 22.0
        cap_rect = QRectF(cx - cap_w / 2.0, cy - cap_h / 2.0, cap_w, cap_h)

        grad = QLinearGradient(cap_rect.topLeft(), cap_rect.bottomLeft())
        grad.setColorAt(0.0, QColor("#565A68"))
        grad.setColorAt(0.5, QColor("#3A3D48"))
        grad.setColorAt(1.0, QColor("#4A4E5C"))
        p.setPen(QPen(QColor("#111216"), 1.2))
        p.setBrush(grad)
        p.drawRoundedRect(cap_rect, 3.0, 3.0)

        p.setPen(QPen(QColor("#28D8F0"), 1.6))
        p.drawLine(QPointF(cap_rect.left() + 3, cy), QPointF(cap_rect.right() - 3, cy))

        font = QFont("SF Pro Display", 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor("#28D8F0"))
        val_str = f"{self._gain:+.1f}" if abs(self._gain) >= 0.05 else "0.0"
        p.drawText(QRectF(0, self.height() - 21, self.width(), 18), Qt.AlignmentFlag.AlignCenter, val_str)


# =====================================================================
# 7. 控件：核心交互式“共振峰包络 + 声码器频带 + 2D声线操纵画布”
# =====================================================================
class ProVoiceVisualizerWidget(QWidget):
    # 直接在频谱画布上拖拽中心“声线坐标锚点”的双向信号
    identityDragged = pyqtSignal(float, float, bool, bool)  # f_st, p_st, f_changed, p_changed
    preserveScrolled = pyqtSignal(float)            # consonant_preserve_pct
    identityResetRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(460, 230)
        self.setMouseTracking(True)

        self._state = default_voice_demo()
        self._show_input_ghost = True
        self._show_xy_puck = True

        self._hover_puck = False
        self._dragging_puck = False
        self._last_drag_pos = QPointF()
        self._accum_f_st = 0.0
        self._accum_p_st = 0.0
        self._mouse_pos: QPointF | None = None

        self._btn_in_rect = QRectF()
        self._btn_xy_rect = QRectF()

        self._ml = 34.0
        self._mr = 16.0
        self._mt = 10.0
        self._mb = 24.0

        # 频率采样点 (80 Hz ~ 12000 Hz)
        self._num_bins = 220
        self._freqs = [
            FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** (i / (self._num_bins - 1)))
            for i in range(self._num_bins)
        ]
        self._in_spec = [-65.0] * self._num_bins
        self._out_spec = [-65.0] * self._num_bins
        self._f0_in_hz = 135.0
        self._f0_out_hz = 170.0
        self._clarity_pct = 90.0

    def setState(self, state: dict):
        self._state = state
        self.update()

    def updateAnalysis(
        self,
        freqs_hz: list[float],
        input_spec_db: list[float],
        output_spec_db: list[float],
        f0_in_hz: float,
        f0_out_hz: float,
        clarity_pct: float,
    ):
        self._freqs = freqs_hz
        self._in_spec = input_spec_db
        self._out_spec = output_spec_db
        self._f0_in_hz = f0_in_hz
        self._f0_out_hz = f0_out_hz
        self._clarity_pct = clarity_pct
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
        norm = (0.0 - max(-75.0, min(0.0, db))) / 75.0
        return plot.top() + norm * plot.height()

    # 2D 声线操纵锚点位置映射：
    # X 轴：Formant Shift (-12.0 ~ +12.0 st)
    # Y 轴：Pitch Shift   (-24.0 ~ +24.0 st)
    def _puck_pos(self, plot: QRectF) -> QPointF:
        f_st = self._state["formant"]["shift"] if self._state["formant"]["enabled"] else 0.0
        p_st = self._state["pitch"]["semitones"] if self._state["pitch"]["enabled"] else 0.0
        nx = (f_st + 12.0) / 24.0
        ny = (24.0 - p_st) / 48.0
        px = plot.left() + plot.width() * (0.12 + nx * 0.76)
        py = plot.top() + plot.height() * (0.14 + ny * 0.72)
        return QPointF(px, py)

    # -----------------------------------------------------------------
    # 交互事件：画布内直接拖拽 2D 声线锚点 / 滚轮调清音保护 / 双击复位
    # -----------------------------------------------------------------
    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_puck = False
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        plot = self._plot_rect()
        pp = self._puck_pos(plot)
        on_puck = self._show_xy_puck and self._state["enabled"] and (math.hypot(pos.x() - pp.x(), pos.y() - pp.y()) <= 16.0)

        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_in_rect.contains(pos):
                self._show_input_ghost = not self._show_input_ghost
                self.update()
                return
            if self._btn_xy_rect.contains(pos):
                self._show_xy_puck = not self._show_xy_puck
                self.update()
                return

            if on_puck:
                self._dragging_puck = True
                self._last_drag_pos = pos
                self._accum_f_st = float(self._state["formant"]["shift"] if self._state["formant"]["enabled"] else 0.0)
                self._accum_p_st = float(self._state["pitch"]["semitones"] if self._state["pitch"]["enabled"] else 0.0)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()

        elif event.button() == Qt.MouseButton.RightButton and on_puck:
            # 仅右键点击在 2D 锚点上才复位，防止画布空白处误触
            self.identityResetRequested.emit()

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._dragging_puck:
            # 基于帧间增量的浮点累加器：Shift 中途按下/松开不会瞬移，低速拖动也不丢步
            dx = event.position().x() - self._last_drag_pos.x()
            dy = event.position().y() - self._last_drag_pos.y()
            self._last_drag_pos = event.position()

            mods = event.modifiers()
            scale = 0.2 if (mods & Qt.KeyboardModifier.ShiftModifier) else 1.0
            span_x = max(1.0, plot.width() * 0.76)
            span_y = max(1.0, plot.height() * 0.72)

            lock_p = bool(mods & Qt.KeyboardModifier.ControlModifier)
            lock_f = bool(mods & Qt.KeyboardModifier.AltModifier)

            if not lock_f:
                self._accum_f_st = max(-12.0, min(12.0, self._accum_f_st + (dx / span_x) * 24.0 * scale))
            if not lock_p:
                self._accum_p_st = max(-24.0, min(24.0, self._accum_p_st - (dy / span_y) * 48.0 * scale))

            f_st = round(self._accum_f_st, 1)
            p_st = round(self._accum_p_st, 1)
            self.identityDragged.emit(f_st, p_st, not lock_f, not lock_p)
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
            self.identityResetRequested.emit()

    def wheelEvent(self, event):
        # 只要光标在频谱区域内即可滚轮微调辅音清晰度 Preserve（Shift 步进更细）
        if self._plot_rect().contains(event.position()):
            delta = event.angleDelta().y() / 120.0
            step = 1.0 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 4.0
            cur_p = self._state["formant"]["preserve"]
            new_p = max(0.0, min(100.0, round(cur_p + delta * step, 0)))
            self.preserveScrolled.emit(new_p)
            event.accept()

    # -----------------------------------------------------------------
    # 绘制网格、共振峰频带分区、频谱包络、声码器柱状与 2D 锚点
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 深邃夜空背景渐变
        bg_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#181A20"))
        bg_grad.setColorAt(1.0, QColor("#13151A"))
        p.fillRect(plot, bg_grad)

        # 2. 水平 dB 网格
        grid_pen = QPen(QColor("#242730"), 1.0)
        p.setFont(QFont("SF Pro Display", 8))
        for db in (0, -15, -30, -45, -60, -75):
            y = self._db_to_y(float(db), plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor("#7E828E"))
            p.drawText(QRectF(0, y - 8, self._ml - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(db))

        # 3. 垂直对数频率网格 (80Hz ~ 12kHz)
        freq_ticks = [
            (100.0, "100"), (200.0, "200"), (400.0, "400"), (800.0, "800"),
            (1500.0, "1.5K"), (3000.0, "3K"), (6000.0, "6K"), (10000.0, "10K")
        ]
        p.setPen(QColor("#7E828E"))
        p.drawText(QRectF(plot.left(), plot.bottom() + 5, 22, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Hz")
        for f_val, f_lbl in freq_ticks:
            x = self._freq_to_x(f_val, plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#7E828E"))
            p.drawText(QRectF(x - 18, plot.bottom() + 5, 36, 16), Qt.AlignmentFlag.AlignCenter, f_lbl)

        p.setPen(QPen(QColor("#2E323D"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        # 4. 人声声学功能分区淡标（Pitch F0 / Vowel Formants F1-F3 / Sibilance）
        zones = [
            (85.0, 260.0, "CHEST / PITCH F0"),
            (320.0, 2800.0, "VOCAL FORMANT F1–F3"),
            (3500.0, 10500.0, "CONSONANT CLARITY"),
        ]
        p.setFont(QFont("SF Pro Display", 7))
        for f_a, f_b, z_title in zones:
            xa = self._freq_to_x(f_a, plot)
            xb = self._freq_to_x(f_b, plot)
            p.setPen(QColor(255, 255, 255, 42))
            p.drawText(QRectF(xa, plot.bottom() - 18, xb - xa, 14), Qt.AlignmentFlag.AlignCenter, z_title)

        st = self._state
        enabled = st["enabled"]

        # 5. 输入人声基准频谱包络（暗灰蓝虚线/柔光）
        if self._show_input_ghost and len(self._freqs) > 1:
            in_path = QPainterPath()
            for i, f in enumerate(self._freqs):
                sx = self._freq_to_x(f, plot)
                sy = self._db_to_y(self._in_spec[i], plot)
                if i == 0:
                    in_path.moveTo(sx, sy)
                else:
                    in_path.lineTo(sx, sy)
            p.setPen(QPen(QColor(135, 145, 168, 110), 1.2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(in_path)

        # 6. 声码器频带调制柱状阵（仅当 Vocoder 激活时绘制）
        if enabled and st["vocoder"]["enabled"]:
            num_bands = int(st["vocoder"]["bands"])
            v_col = QColor(COLORS["vocoder"])
            for b_i in range(num_bands):
                n0 = b_i / num_bands
                n1 = (b_i + 0.78) / num_bands
                bx0 = plot.left() + n0 * plot.width()
                bx1 = plot.left() + n1 * plot.width()
                mid_idx = min(len(self._out_spec) - 1, int((b_i + 0.5) / num_bands * len(self._out_spec)))
                by = self._db_to_y(self._out_spec[mid_idx], plot)
                bar_rect = QRectF(bx0, by, max(2.0, bx1 - bx0), plot.bottom() - by)
                p.setPen(QPen(QColor(v_col.red(), v_col.green(), v_col.blue(), 95), 0.8))
                p.setBrush(QColor(v_col.red(), v_col.green(), v_col.blue(), 34))
                p.drawRect(bar_rect)

        # 7. 处理后人声共振峰主包络曲线
        if len(self._freqs) > 1:
            out_path = QPainterPath()
            out_poly = QPolygonF()
            first_x = self._freq_to_x(self._freqs[0], plot)
            out_poly.append(QPointF(first_x, plot.bottom()))

            for i, f in enumerate(self._freqs):
                sx = self._freq_to_x(f, plot)
                sy = self._db_to_y(self._out_spec[i] if enabled else self._in_spec[i], plot)
                if i == 0:
                    out_path.moveTo(sx, sy)
                else:
                    out_path.lineTo(sx, sy)
                out_poly.append(QPointF(sx, sy))

            last_x = self._freq_to_x(self._freqs[-1], plot)
            out_poly.append(QPointF(last_x, plot.bottom()))

            main_col = QColor(COLORS["formant"]) if (enabled and st["formant"]["enabled"]) else QColor(COLORS["cyan"])
            if not enabled:
                main_col = QColor("#686C78")

            area_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            area_grad.setColorAt(0.0, QColor(main_col.red(), main_col.green(), main_col.blue(), 52))
            area_grad.setColorAt(1.0, QColor(main_col.red(), main_col.green(), main_col.blue(), 6))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(area_grad)
            p.drawPolygon(out_poly)

            p.setPen(QPen(QColor(main_col.red(), main_col.green(), main_col.blue(), 55), 4.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(out_path)

            p.setPen(QPen(main_col, 1.9))
            p.drawPath(out_path)

            # 标注偏移后的 F1 / F2 / F3 共振峰胶囊标签
            if enabled and st["formant"]["enabled"]:
                ratio = 2.0 ** (st["formant"]["shift"] / 12.0)
                formant_peaks = [
                    ("F1", 520.0 * ratio),
                    ("F2", 1550.0 * ratio),
                    ("F3", 2650.0 * ratio),
                ]
                p.setFont(QFont("SF Pro Display", 7))
                for tag, f_hz in formant_peaks:
                    if FREQ_MIN <= f_hz <= FREQ_MAX:
                        fx = self._freq_to_x(f_hz, plot)
                        # 查找最接近频率点的 Y 坐标
                        idx_near = min(range(len(self._freqs)), key=lambda k: abs(self._freqs[k] - f_hz))
                        fy = self._db_to_y(self._out_spec[idx_near], plot)
                        p.setPen(QPen(QColor(76, 212, 144, 90), 1.0, Qt.PenStyle.DotLine))
                        p.drawLine(QPointF(fx, fy), QPointF(fx, plot.bottom() - 20))

                        badge = QRectF(fx - 11, max(plot.top() + 30, fy - 18), 22, 13)
                        p.setPen(QPen(QColor("#4CD490"), 1.0))
                        p.setBrush(QColor(16, 24, 22, 220))
                        p.drawRoundedRect(badge, 3.0, 3.0)
                        p.setPen(QColor("#A8F5CC"))
                        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, tag)

        # 8. 2D 声线坐标锚点（Formant X vs Pitch Y 直接拖拽控制）
        if enabled and self._show_xy_puck:
            pp = self._puck_pos(plot)
            is_hi = self._hover_puck or self._dragging_puck
            r = 12.0 if is_hi else 9.8

            # 悬停或拖拽时显示 2D 原点 (0, 0) 基准十字与四向语义提示
            if is_hi:
                cx0 = plot.left() + plot.width() * 0.50
                cy0 = plot.top() + plot.height() * 0.50
                p.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(cx0, plot.top() + 28), QPointF(cx0, plot.bottom() - 20))
                p.drawLine(QPointF(plot.left() + 8, cy0), QPointF(plot.right() - 8, cy0))

                p.setFont(QFont("SF Pro Display", 7))
                p.setPen(QColor(160, 172, 195, 130))
                p.drawText(QRectF(plot.left() + 10, cy0 - 15, 110, 14), Qt.AlignmentFlag.AlignLeft, "← COARSE / DEEP")
                p.drawText(QRectF(plot.right() - 110, cy0 - 15, 100, 14), Qt.AlignmentFlag.AlignRight, "FINE / YOUTH →")

            # 动态跟踪十字线
            p.setPen(QPen(QColor(40, 216, 240, 85 if is_hi else 32), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(pp.x(), plot.top()), QPointF(pp.x(), plot.bottom()))
            p.drawLine(QPointF(plot.left(), pp.y()), QPointF(plot.right(), pp.y()))

            if is_hi:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(40, 216, 240, 58))
                p.drawEllipse(pp, r + 6.5, r + 6.5)

            p.setPen(QPen(QColor("#28D8F0"), 2.0))
            p.setBrush(QColor(14, 20, 28, 225))
            p.drawEllipse(pp, r, r)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#4CD490"))
            p.drawEllipse(pp, 3.5, 3.5)

            # 拖拽时锚点旁实时浮动数值胶囊（无需盯左上角 HUD）
            if self._dragging_puck:
                f_cur = st["formant"]["shift"] if st["formant"]["enabled"] else 0.0
                p_cur = st["pitch"]["semitones"] if st["pitch"]["enabled"] else 0.0
                tip_txt = f"FMT {f_cur:+.1f}  |  PCH {p_cur:+.1f}"
                tip_w, tip_h = 128.0, 18.0
                tx = min(plot.right() - tip_w - 6, max(plot.left() + 6, pp.x() + 16))
                ty = max(plot.top() + 32, pp.y() - 24)
                tip_rect = QRectF(tx, ty, tip_w, tip_h)
                p.setPen(QPen(QColor("#28D8F0"), 1.0))
                p.setBrush(QColor(12, 16, 22, 235))
                p.drawRoundedRect(tip_rect, 3.5, 3.5)
                p.setPen(QColor("#E8F8FF"))
                p.setFont(QFont("SF Pro Display", 7))
                p.drawText(tip_rect, Qt.AlignmentFlag.AlignCenter, tip_txt)

        # 9. 右上角开关胶囊 (ORIG / XY PAD)
        self._btn_in_rect = QRectF(plot.right() - 94, plot.top() + 8, 42, 18)
        self._btn_xy_rect = QRectF(plot.right() - 48, plot.top() + 8, 40, 18)
        for rect, label, active in (
            (self._btn_in_rect, "ORIG", self._show_input_ghost),
            (self._btn_xy_rect, "2D XY", self._show_xy_puck),
        ):
            p.setPen(QPen(QColor("#3A4252") if active else QColor("#282B34"), 1.0))
            p.setBrush(QColor(24, 34, 46, 210) if active else QColor(18, 20, 25, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#6E727E"))
            p.setFont(QFont("SF Pro Display", 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 10. 左上角实时声学信息 HUD
        f_val = st["formant"]["shift"] if (enabled and st["formant"]["enabled"]) else 0.0
        p_val = st["pitch"]["semitones"] if (enabled and st["pitch"]["enabled"]) else 0.0
        hud_str = (
            f"Formant {f_val:+.1f} st  •  Pitch {p_val:+.1f} st  •  "
            f"F0: {int(self._f0_in_hz)}→{int(self._f0_out_hz)}Hz ({freq_to_note(self._f0_out_hz)})  •  "
            f"Clarity {int(self._clarity_pct)}%"
        )
        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, 355, 19)
        p.setPen(QPen(QColor("#323642"), 1.0))
        p.setBrush(QColor(16, 18, 23, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor("#28D8F0") if self._dragging_puck else QColor("#C6CCD8"))
        p.setFont(QFont("SF Pro Display", 8))
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
        pill_width: int = 62,
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
        hints_row.setSpacing(2)
        for h_text, align in (
            (min_hint, Qt.AlignmentFlag.AlignLeft),
            (mid_hint, Qt.AlignmentFlag.AlignCenter),
            (max_hint, Qt.AlignmentFlag.AlignRight),
        ):
            hl = QLabel(h_text)
            hl.setAlignment(align)
            hl.setStyleSheet("color: #666B78; font-size: 8.5px;")
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
        self.title_lbl.setStyleSheet(f"color: {'#9AA0B0' if active else '#585C68'}; font-size: 10px; font-weight: 600;")
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
# 9. 模块卡片 1：Formant Shifter Card（共振峰偏移器 · 首选身份变换）
# =====================================================================
class FormantShifterCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["formant"]
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
        h_lay.setSpacing(7)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Formant Shifter")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # Natural / Crisp 模式双态切换
        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #121318; border: 1px solid #2A2D37; border-radius: 4px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.btn_nat = QPushButton("Natural")
        self.btn_crisp = QPushButton("Crisp")
        for b in (self.btn_nat, self.btn_crisp):
            b.setCheckable(True)
            b.setFixedSize(48, 19)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_lay.addWidget(b)

        self.btn_nat.clicked.connect(lambda: self._on_mode("Natural"))
        self.btn_crisp.clicked.connect(lambda: self._on_mode("Crisp"))
        h_lay.addWidget(seg_wrap)
        root.addWidget(header)

        # Body (2x3 旋钮网格)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_shift = KnobParameterCell(
            "Shift", -12.0, 12.0, 0.0, self._accent,
            "-12", "st", "+12",
            format_st, parse_numeric, mode="bipolar"
        )
        self.c_throat = KnobParameterCell(
            "Throat", -100.0, 100.0, 0.0, self._accent,
            "-100", "%", "+100",
            format_signed_pct, parse_numeric, mode="bipolar"
        )
        self.c_gender = KnobParameterCell(
            "Gender", -100.0, 100.0, 0.0, self._accent,
            "Male", "", "Fem",
            format_signed_pct, parse_numeric, mode="bipolar"
        )
        for c in (self.c_shift, self.c_throat, self.c_gender):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_preserve = KnobParameterCell(
            "Preserve", 0.0, 100.0, 90.0, self._accent,
            "0", "Clarity", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_timbre = KnobParameterCell(
            "Timbre", -50.0, 50.0, 0.0, self._accent,
            "Dark", "", "Bright",
            format_signed_pct, parse_numeric, mode="bipolar"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 100.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_preserve, self.c_timbre, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_shift.valueChanged.connect(lambda v: self._set_field("shift", round(v, 1)))
        self.c_throat.valueChanged.connect(lambda v: self._set_field("throat", round(v, 0)))
        self.c_gender.valueChanged.connect(lambda v: self._set_field("gender", round(v, 0)))
        self.c_preserve.valueChanged.connect(lambda v: self._set_field("preserve", round(v, 0)))
        self.c_timbre.valueChanged.connect(lambda v: self._set_field("timbre", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        is_nat = (self._data["mode"] == "Natural")

        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: {'#1D1F25' if en else '#181A20'};
                border: 1px solid #2A2D37;
                border-top: 2px solid {self._accent if en else '#343844'};
                border-radius: 5px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #262932;
            }}
        """)

        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        self.btn_nat.setChecked(is_nat)
        self.btn_crisp.setChecked(not is_nat)
        active_seg = f"""
            QPushButton {{
                background-color: #1A2822;
                color: {self._accent if en else '#9AA0B0'};
                border: 1px solid {self._accent if en else '#3A3E4A'};
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 700;
            }}
        """
        inactive_seg = """
            QPushButton {
                background-color: transparent;
                color: #787D8C;
                border: none;
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 600;
            }
            QPushButton:hover { color: #D0D4E0; }
        """
        self.btn_nat.setStyleSheet(active_seg if is_nat else inactive_seg)
        self.btn_crisp.setStyleSheet(active_seg if not is_nat else inactive_seg)

        self.c_shift.setValue(self._data["shift"])
        self.c_throat.setValue(self._data["throat"])
        self.c_gender.setValue(self._data["gender"])
        self.c_preserve.setValue(self._data["preserve"])
        self.c_timbre.setValue(self._data["timbre"])
        self.c_mix.setValue(self._data["mix"])

        for cell in (self.c_shift, self.c_throat, self.c_gender, self.c_preserve, self.c_timbre, self.c_mix):
            cell.setModuleActive(en)

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
# 10. 模块卡片 2：Pitch Shifter Card（音调移调器 · 基频 F0 控制）
# =====================================================================
class PitchShifterCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["pitch"]
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
        h_lay.setSpacing(7)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Pitch Shifter")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # Vocal / Wide 音域跟踪模式
        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #121318; border: 1px solid #2A2D37; border-radius: 4px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.btn_vocal = QPushButton("Vocal")
        self.btn_wide = QPushButton("Wide")
        for b in (self.btn_vocal, self.btn_wide):
            b.setCheckable(True)
            b.setFixedSize(44, 19)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_lay.addWidget(b)

        self.btn_vocal.clicked.connect(lambda: self._on_tracking("Vocal"))
        self.btn_wide.clicked.connect(lambda: self._on_tracking("Wide"))
        h_lay.addWidget(seg_wrap)
        root.addWidget(header)

        # Body (2x3 旋钮网格)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_semi = KnobParameterCell(
            "Semitones", -24.0, 24.0, 0.0, self._accent,
            "-24", "st", "+24",
            format_st, parse_numeric, mode="bipolar"
        )
        self.c_fine = KnobParameterCell(
            "Fine", -100.0, 100.0, 0.0, self._accent,
            "-100", "ct", "+100",
            lambda v: f"{int(round(v)):+d} ct" if abs(v) >= 0.5 else "0 ct",
            parse_numeric, mode="bipolar"
        )
        self.c_link = KnobParameterCell(
            "Formant Link", 0.0, 100.0, 0.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_semi, self.c_fine, self.c_link):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_glide = KnobParameterCell(
            "Glide", 0.0, 200.0, 18.0, self._accent,
            "0", "ms", "200",
            format_ms, parse_numeric, mode="power"
        )
        self.c_jitter = KnobParameterCell(
            "Age Jitter", 0.0, 100.0, 0.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 100.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_glide, self.c_jitter, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_semi.valueChanged.connect(lambda v: self._set_field("semitones", round(v, 1)))
        self.c_fine.valueChanged.connect(lambda v: self._set_field("fine", round(v, 0)))
        self.c_link.valueChanged.connect(lambda v: self._set_field("formant_link", round(v, 0)))
        self.c_glide.valueChanged.connect(lambda v: self._set_field("glide", round(v, 0)))
        self.c_jitter.valueChanged.connect(lambda v: self._set_field("jitter", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        is_voc = (self._data["tracking"] == "Vocal")

        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: {'#1D1F25' if en else '#181A20'};
                border: 1px solid #2A2D37;
                border-top: 2px solid {self._accent if en else '#343844'};
                border-radius: 5px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #262932;
            }}
        """)

        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        self.btn_vocal.setChecked(is_voc)
        self.btn_wide.setChecked(not is_voc)
        active_seg = f"""
            QPushButton {{
                background-color: #2A2318;
                color: {self._accent if en else '#9AA0B0'};
                border: 1px solid {self._accent if en else '#3A3E4A'};
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 700;
            }}
        """
        inactive_seg = """
            QPushButton {
                background-color: transparent;
                color: #787D8C;
                border: none;
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 600;
            }
            QPushButton:hover { color: #D0D4E0; }
        """
        self.btn_vocal.setStyleSheet(active_seg if is_voc else inactive_seg)
        self.btn_wide.setStyleSheet(active_seg if not is_voc else inactive_seg)

        self.c_semi.setValue(self._data["semitones"])
        self.c_fine.setValue(self._data["fine"])
        self.c_link.setValue(self._data["formant_link"])
        self.c_glide.setValue(self._data["glide"])
        self.c_jitter.setValue(self._data["jitter"])
        self.c_mix.setValue(self._data["mix"])

        for cell in (self.c_semi, self.c_fine, self.c_link, self.c_glide, self.c_jitter, self.c_mix):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_tracking(self, mode: str):
        if self._data["tracking"] != mode:
            self._data["tracking"] = mode
            self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 11. 模块卡片 3：Vocoder Card（声码器 · 经典机械机器人）
# =====================================================================
class VocoderCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["vocoder"]
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
        h_lay.setSpacing(7)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Vocoder")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # Carrier 载波波形选择器
        self.carrier_combo = QComboBox()
        self.carrier_combo.setFixedSize(78, 21)
        self.carrier_combo.addItems(["Saw", "Pulse", "Choir", "Noise"])
        self.carrier_combo.currentTextChanged.connect(self._on_carrier)
        h_lay.addWidget(self.carrier_combo)
        root.addWidget(header)

        # Body (2x3 旋钮网格)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_bands = KnobParameterCell(
            "Bands", 8.0, 32.0, 16.0, self._accent,
            "8", "Ch", "32",
            lambda v: f"{int(round(v))} ch", parse_numeric, mode="linear"
        )
        self.c_carrier_hz = KnobParameterCell(
            "Carrier Hz", 50.0, 440.0, 115.0, self._accent,
            "50", "Hz", "440",
            format_hz, parse_numeric, mode="log"
        )
        self.c_formant = KnobParameterCell(
            "Formant", -12.0, 12.0, 0.0, self._accent,
            "-12", "st", "+12",
            format_st, parse_numeric, mode="bipolar"
        )
        for c in (self.c_bands, self.c_carrier_hz, self.c_formant):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_unvoiced = KnobParameterCell(
            "Unvoiced", 0.0, 100.0, 72.0, self._accent,
            "0", "Clarity", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_release = KnobParameterCell(
            "Release", 5.0, 500.0, 24.0, self._accent,
            "5", "ms", "500",
            format_ms, parse_numeric, mode="log"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 100.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_unvoiced, self.c_release, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_bands.valueChanged.connect(lambda v: self._set_field("bands", int(round(v))))
        self.c_carrier_hz.valueChanged.connect(lambda v: self._set_field("carrier_hz", round(v, 1)))
        self.c_formant.valueChanged.connect(lambda v: self._set_field("formant", round(v, 1)))
        self.c_unvoiced.valueChanged.connect(lambda v: self._set_field("unvoiced", round(v, 0)))
        self.c_release.valueChanged.connect(lambda v: self._set_field("release", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])

        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: {'#1D1F25' if en else '#181A20'};
                border: 1px solid #2A2D37;
                border-top: 2px solid {self._accent if en else '#343844'};
                border-radius: 5px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #262932;
            }}
        """)

        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )
        self.carrier_combo.setEnabled(en)
        self.carrier_combo.setCurrentText(self._data["carrier"])
        self.carrier_combo.setStyleSheet(
            "" if en else "QComboBox { color: #5E6270; border-color: #242730; }"
        )

        self.c_bands.setValue(self._data["bands"])
        self.c_carrier_hz.setValue(self._data["carrier_hz"])
        self.c_formant.setValue(self._data["formant"])
        self.c_unvoiced.setValue(self._data["unvoiced"])
        self.c_release.setValue(self._data["release"])
        self.c_mix.setValue(self._data["mix"])

        for cell in (self.c_bands, self.c_carrier_hz, self.c_formant, self.c_unvoiced, self.c_release, self.c_mix):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_carrier(self, carrier: str):
        if self._data["carrier"] != carrier:
            self._data["carrier"] = carrier
            self._emit()

    def _set_field(self, key: str, val: float | int):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 12. 主面板：自主旗舰人声变声器 (Studio Voice Changer)
# =====================================================================
class StudioVoiceChangerWidget(QWidget):
    # 对接后端 DSP 音频引擎的完整信号接口
    bypassChanged = pyqtSignal(bool)
    formantChanged = pyqtSignal(dict)
    pitchChanged = pyqtSignal(dict)
    vocoderChanged = pyqtSignal(dict)
    outputGainChanged = pyqtSignal(float)
    auditionToggled = pyqtSignal(bool)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Audio 1", parent=None,
                 demo: bool = False):
        super().__init__(parent)
        self._demo_mode = bool(demo)
        self.setWindowTitle(f"Voice Changer — {track_name}")
        self.resize(980, 690)
        self.setMinimumSize(900, 620)

        self._active_bank = "A"
        self._banks = {
            "A": default_voice_demo(),
            "B": default_voice_neutral(),
        }

        self._presets = {
            # ================= 1. 写实跨性别与年龄塑形 =================
            "Male → Female (自然女声)": copy.deepcopy(self._banks["A"]),
            "Female → Male (自然男声)": {
                "enabled": True,
                "output_gain": 0.5,
                "formant": {"enabled": True, "mode": "Natural", "shift": -3.8, "throat": -24.0, "gender": -45.0, "preserve": 92.0, "timbre": -10.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": -5.0, "fine": 0.0, "formant_link": 0.0, "glide": 20.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 95.0, "formant": 0.0, "unvoiced": 70.0, "release": 25.0, "mix": 100.0},
            },
            "Cute Kid / Child (萌娃童声)": {
                "enabled": True,
                "output_gain": 0.0,
                "formant": {"enabled": True, "mode": "Natural", "shift": 5.8, "throat": 38.0, "gender": 35.0, "preserve": 92.0, "timbre": 16.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": 3.5, "fine": 5.0, "formant_link": 10.0, "glide": 16.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 120.0, "formant": 0.0, "unvoiced": 75.0, "release": 20.0, "mix": 100.0},
            },
            "Sweet Anime Girl (元气少女)": {
                "enabled": True,
                "output_gain": 0.0,
                "formant": {"enabled": True, "mode": "Natural", "shift": 6.4, "throat": 44.0, "gender": 68.0, "preserve": 90.0, "timbre": 20.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": 5.0, "fine": 6.0, "formant_link": 0.0, "glide": 22.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 120.0, "formant": 0.0, "unvoiced": 75.0, "release": 20.0, "mix": 100.0},
            },
            "Warm Baritone (磁性播音低音炮)": {
                "enabled": True,
                "output_gain": 1.2,
                "formant": {"enabled": True, "mode": "Natural", "shift": -1.4, "throat": -28.0, "gender": -22.0, "preserve": 96.0, "timbre": -14.0, "mix": 85.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": -1.5, "fine": -4.0, "formant_link": 0.0, "glide": 15.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 90.0, "formant": 0.0, "unvoiced": 75.0, "release": 25.0, "mix": 100.0},
            },
            "Elderly / Grandpa (苍老长者)": {
                "enabled": True,
                "output_gain": 1.0,
                "formant": {"enabled": True, "mode": "Natural", "shift": -2.6, "throat": -38.0, "gender": -25.0, "preserve": 88.0, "timbre": -22.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": -2.5, "fine": -8.0, "formant_link": 0.0, "glide": 35.0, "jitter": 34.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 95.0, "formant": 0.0, "unvoiced": 70.0, "release": 25.0, "mix": 100.0},
            },
            "Fragile Grandma (慈祥老奶奶)": {
                "enabled": True,
                "output_gain": 1.0,
                "formant": {"enabled": True, "mode": "Natural", "shift": 1.8, "throat": 14.0, "gender": 28.0, "preserve": 86.0, "timbre": -12.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": -1.5, "fine": -6.0, "formant_link": 0.0, "glide": 32.0, "jitter": 40.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 110.0, "formant": 0.0, "unvoiced": 70.0, "release": 25.0, "mix": 100.0},
            },

            # ================= 2. 纪录片访谈与隐私声纹脱敏 =================
            "Anonymous Witness (匿名证人变声)": {
                "enabled": True,
                "output_gain": 0.8,
                "formant": {"enabled": True, "mode": "Natural", "shift": -3.2, "throat": -22.0, "gender": -30.0, "preserve": 95.0, "timbre": -8.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": -3.5, "fine": 0.0, "formant_link": 15.0, "glide": 18.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 95.0, "formant": 0.0, "unvoiced": 75.0, "release": 25.0, "mix": 100.0},
            },
            "Encrypted Informant (加密线人掩码)": {
                "enabled": True,
                "output_gain": 1.2,
                "formant": {"enabled": True, "mode": "Natural", "shift": -4.4, "throat": -34.0, "gender": -40.0, "preserve": 94.0, "timbre": -16.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": -5.5, "fine": -5.0, "formant_link": 10.0, "glide": 22.0, "jitter": 6.0, "mix": 100.0},
                "vocoder": {"enabled": True, "carrier": "Noise", "bands": 24, "carrier_hz": 85.0, "formant": -1.5, "unvoiced": 88.0, "release": 28.0, "mix": 24.0},
            },

            # ================= 3. 动漫、喜剧与角色夸张变形 =================
            "Helium Balloon (氦气搞笑)": {
                "enabled": True,
                "output_gain": 0.0,
                "formant": {"enabled": True, "mode": "Crisp", "shift": 7.8, "throat": 48.0, "gender": 55.0, "preserve": 90.0, "timbre": 18.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": 5.5, "fine": 0.0, "formant_link": 15.0, "glide": 12.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 120.0, "formant": 0.0, "unvoiced": 75.0, "release": 20.0, "mix": 100.0},
            },
            "Chipmunk (花栗鼠)": {
                "enabled": True,
                "output_gain": -0.5,
                "formant": {"enabled": True, "mode": "Crisp", "shift": 3.5, "throat": 25.0, "gender": 20.0, "preserve": 86.0, "timbre": 14.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Wide", "semitones": 11.0, "fine": 10.0, "formant_link": 60.0, "glide": 8.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 120.0, "formant": 0.0, "unvoiced": 75.0, "release": 20.0, "mix": 100.0},
            },
            "Dark Villain / Titan (暗黑反派 / 泰坦)": {
                "enabled": True,
                "output_gain": 1.5,
                "formant": {"enabled": True, "mode": "Natural", "shift": -4.2, "throat": -54.0, "gender": -52.0, "preserve": 88.0, "timbre": -22.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": -7.5, "fine": -10.0, "formant_link": 20.0, "glide": 28.0, "jitter": 10.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 80.0, "formant": 0.0, "unvoiced": 75.0, "release": 30.0, "mix": 100.0},
            },
            "Abyssal Monster (深渊怪兽)": {
                "enabled": True,
                "output_gain": 1.5,
                "formant": {"enabled": True, "mode": "Crisp", "shift": -5.5, "throat": -65.0, "gender": -60.0, "preserve": 82.0, "timbre": -30.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Wide", "semitones": -10.5, "fine": -15.0, "formant_link": 35.0, "glide": 40.0, "jitter": 18.0, "mix": 100.0},
                "vocoder": {"enabled": False, "carrier": "Saw", "bands": 16, "carrier_hz": 80.0, "formant": 0.0, "unvoiced": 70.0, "release": 30.0, "mix": 100.0},
            },
            "Whispering Ghost (幽灵低语 / 惊悚耳语)": {
                "enabled": True,
                "output_gain": 2.0,
                "formant": {"enabled": True, "mode": "Natural", "shift": 2.2, "throat": 18.0, "gender": 15.0, "preserve": 92.0, "timbre": 12.0, "mix": 80.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": -4.0, "fine": -12.0, "formant_link": 0.0, "glide": 45.0, "jitter": 28.0, "mix": 65.0},
                "vocoder": {"enabled": True, "carrier": "Noise", "bands": 24, "carrier_hz": 110.0, "formant": 1.8, "unvoiced": 94.0, "release": 65.0, "mix": 78.0},
            },

            # ================= 4. 科幻机械、声码器与和声质感 =================
            "AI Voice Assistant (AI 语音助手)": {
                "enabled": True,
                "output_gain": 0.5,
                "formant": {"enabled": True, "mode": "Natural", "shift": 1.2, "throat": 10.0, "gender": 18.0, "preserve": 95.0, "timbre": 6.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": 1.0, "fine": 0.0, "formant_link": 0.0, "glide": 75.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": True, "carrier": "Choir", "bands": 28, "carrier_hz": 175.0, "formant": 1.0, "unvoiced": 88.0, "release": 22.0, "mix": 32.0},
            },
            "Classic Robot (经典声码器)": {
                "enabled": True,
                "output_gain": 1.0,
                "formant": {"enabled": False, "mode": "Natural", "shift": 0.0, "throat": 0.0, "gender": 0.0, "preserve": 90.0, "timbre": 0.0, "mix": 100.0},
                "pitch": {"enabled": False, "tracking": "Vocal", "semitones": 0.0, "fine": 0.0, "formant_link": 0.0, "glide": 18.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": True, "carrier": "Saw", "bands": 18, "carrier_hz": 110.0, "formant": 0.0, "unvoiced": 76.0, "release": 20.0, "mix": 100.0},
            },
            "Cyberpunk Android (赛博机械人)": {
                "enabled": True,
                "output_gain": 1.0,
                "formant": {"enabled": True, "mode": "Crisp", "shift": -1.8, "throat": -15.0, "gender": -10.0, "preserve": 92.0, "timbre": 8.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": -2.0, "fine": 0.0, "formant_link": 0.0, "glide": 15.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": True, "carrier": "Pulse", "bands": 24, "carrier_hz": 130.0, "formant": 1.5, "unvoiced": 82.0, "release": 18.0, "mix": 72.0},
            },
            "Giant Mech PA (重装机甲广播)": {
                "enabled": True,
                "output_gain": 1.5,
                "formant": {"enabled": True, "mode": "Crisp", "shift": -3.6, "throat": -45.0, "gender": -40.0, "preserve": 88.0, "timbre": -12.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Wide", "semitones": -5.0, "fine": -8.0, "formant_link": 15.0, "glide": 20.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": True, "carrier": "Saw", "bands": 14, "carrier_hz": 72.0, "formant": -2.5, "unvoiced": 74.0, "release": 36.0, "mix": 84.0},
            },
            "8-Bit Retro Game (复古像素游戏音)": {
                "enabled": True,
                "output_gain": 0.0,
                "formant": {"enabled": True, "mode": "Crisp", "shift": 2.0, "throat": 15.0, "gender": 10.0, "preserve": 85.0, "timbre": 16.0, "mix": 100.0},
                "pitch": {"enabled": True, "tracking": "Wide", "semitones": 3.0, "fine": 0.0, "formant_link": 25.0, "glide": 0.0, "jitter": 0.0, "mix": 100.0},
                "vocoder": {"enabled": True, "carrier": "Pulse", "bands": 10, "carrier_hz": 145.0, "formant": 1.0, "unvoiced": 62.0, "release": 10.0, "mix": 92.0},
            },
            "Ethereal Choir (空灵圣咏和声)": {
                "enabled": True,
                "output_gain": 0.0,
                "formant": {"enabled": True, "mode": "Natural", "shift": 0.8, "throat": 8.0, "gender": 12.0, "preserve": 94.0, "timbre": 10.0, "mix": 70.0},
                "pitch": {"enabled": True, "tracking": "Vocal", "semitones": 7.0, "fine": 4.0, "formant_link": 0.0, "glide": 30.0, "jitter": 0.0, "mix": 42.0},
                "vocoder": {"enabled": True, "carrier": "Choir", "bands": 24, "carrier_hz": 220.0, "formant": 0.5, "unvoiced": 80.0, "release": 95.0, "mix": 48.0},
            },
            "Clean Neutral (原始人声)": copy.deepcopy(self._banks["B"]),
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # NF-02（计划 §6.2-1）：演示动画与真音频分离——生产构造
        # demo=False 不启动假数据定时器（频谱/电平保持地板值，
        # 由 update_analysis/update_meters 注入真实遥测）。
        self._anim_t = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_analysis)
        if self._demo_mode:
            self._demo_timer.start(42)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet("""
            QWidget {
                background-color: #17191E;
                color: #E2E5EE;
                font-family: 'SF Pro Display', 'Segoe UI', 'PingFang SC', 'Microsoft YaHei';
            }
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ProVerticalMeter, MasterOutputStrip, ProVoiceVisualizerWidget {
                background-color: transparent;
            }
            QComboBox {
                background-color: #111317;
                color: #E4E7F0;
                border: 1px solid #2E323E;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
                min-height: 20px;
            }
            QComboBox:hover { border-color: #424858; }
            QComboBox::drop-down { border: none; width: 16px; }
            QComboBox QAbstractItemView {
                background-color: #181A20;
                color: #E4E7F0;
                border: 1px solid #383D4C;
                selection-background-color: #262C3A;
            }
            QPushButton#ToolBtn {
                background-color: #22252E;
                color: #B4B9C8;
                border: 1px solid #303440;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#ToolBtn:hover {
                background-color: #2C303C;
                color: #FFFFFF;
                border-color: #424858;
            }
        """)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # -------------------------------------------------------------
        # 1. 顶部整合工具栏
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setFixedHeight(44)
        top_bar.setStyleSheet("background-color: #13151A; border-bottom: 1px solid #232630;")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(14, 6, 14, 6)
        tb_lay.setSpacing(8)

        self.power_switch = PowerToggleSwitch(True, COLORS["cyan"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"VOICE CHANGER  ·  {track_name}")
        title_lbl.setStyleSheet("color: #EAEDF6; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(12)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(23, 23)
        add_preset_btn.setToolTip("保存或覆盖声线预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        del_preset_btn = QPushButton("−")
        del_preset_btn.setObjectName("ToolBtn")
        del_preset_btn.setFixedSize(23, 23)
        del_preset_btn.setToolTip("删除当前自定义预设")
        del_preset_btn.clicked.connect(self._delete_current_preset)
        tb_lay.addWidget(del_preset_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedWidth(210)
        self._builtin_preset_names = set(self._presets.keys())
        self.preset_combo.addItems(list(self._presets.keys()))
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        tb_lay.addWidget(self.preset_combo)

        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setFixedSize(21, 23)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb_lay.addWidget(prev_btn)

        next_btn = QPushButton("›")
        next_btn.setObjectName("ToolBtn")
        next_btn.setFixedSize(21, 23)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb_lay.addWidget(next_btn)

        tb_lay.addSpacing(10)

        # A / B 对比槽
        ab_lay = QHBoxLayout()
        ab_lay.setSpacing(2)
        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(24, 23)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton("→")
        self.btn_copy_ab.setObjectName("ToolBtn")
        self.btn_copy_ab.setFixedSize(24, 23)
        self.btn_copy_ab.setToolTip("复制当前参数到另一槽位")
        self.btn_copy_ab.clicked.connect(self._copy_bank)

        self.btn_b = QPushButton("B")
        self.btn_b.setFixedSize(24, 23)
        self.btn_b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_b.clicked.connect(lambda: self._switch_bank("B"))

        ab_lay.addWidget(self.btn_a)
        ab_lay.addWidget(self.btn_copy_ab)
        ab_lay.addWidget(self.btn_b)
        tb_lay.addLayout(ab_lay)

        tb_lay.addStretch()

        # 一键干声试听按钮（剪辑静止时试听变声效果）
        self.audition_btn = QPushButton("▶ Audition")
        self.audition_btn.setCheckable(True)
        self.audition_btn.setFixedSize(78, 23)
        self.audition_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.audition_btn.setToolTip("触发内置人声干声循环试听")
        self.audition_btn.setStyleSheet("""
            QPushButton {
                background-color: #22252E;
                color: #B4B9C8;
                border: 1px solid #303440;
                border-radius: 4px;
                font-size: 10.5px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; border-color: #424858; }
            QPushButton:checked {
                background-color: #13242C;
                color: #28D8F0;
                border: 1px solid #28D8F0;
                font-weight: 700;
            }
        """)
        self.audition_btn.toggled.connect(self.auditionToggled.emit)
        tb_lay.addWidget(self.audition_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 23)
        reset_btn.clicked.connect(self.reset_voice_changer)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：Input | 共振峰包络与2D声线画布 | 4通道状态表 | OUT推子 | Output
        # -------------------------------------------------------------
        upper_panel = QFrame()
        upper_panel.setStyleSheet("background-color: #17191E;")
        up_lay = QHBoxLayout(upper_panel)
        up_lay.setContentsMargins(14, 12, 14, 8)
        up_lay.setSpacing(10)

        # 2.1 Input 电平表
        in_col = self._build_meter_column("IN")
        self.meter_input = ProVerticalMeter(mode="level", show_ticks=True)
        in_col.addWidget(self.meter_input, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(in_col)

        # 2.2 核心声学可视化与 2D 声线坐标画布
        self.voice_visualizer = ProVoiceVisualizerWidget()
        self.voice_visualizer.identityDragged.connect(self._on_canvas_identity_dragged)
        self.voice_visualizer.preserveScrolled.connect(self._on_canvas_preserve_scrolled)
        self.voice_visualizer.identityResetRequested.connect(self._on_canvas_identity_reset)
        up_lay.addWidget(self.voice_visualizer, 5)

        # 2.3 四通道声学特征与可懂度指示条 (FMT · PCH · VOC · CLR)
        stat_col = self._build_meter_column("VOICE ENGINE")
        stat_row = QHBoxLayout()
        stat_row.setSpacing(4)
        self.meter_fmt = ProVerticalMeter(title="FMT", mode="activity", show_ticks=False, accent_color=COLORS["formant"])
        self.meter_pch = ProVerticalMeter(title="PCH", mode="activity", show_ticks=False, accent_color=COLORS["pitch"])
        self.meter_voc = ProVerticalMeter(title="VOC", mode="activity", show_ticks=False, accent_color=COLORS["vocoder"])
        self.meter_clr = ProVerticalMeter(title="CLR", mode="activity", show_ticks=False, accent_color=COLORS["cyan"])
        for m in (self.meter_fmt, self.meter_pch, self.meter_voc, self.meter_clr):
            stat_row.addWidget(m)
        stat_col.addLayout(stat_row, 1)
        up_lay.addLayout(stat_col)

        # 2.4 Output 推子 + 电平表
        fader_col = self._build_meter_column("GAIN")
        self.out_fader = MasterOutputStrip()
        self.out_fader.gainChanged.connect(self._on_output_gain_changed)
        fader_col.addWidget(self.out_fader, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(fader_col)

        out_col = self._build_meter_column("OUT")
        self.meter_output = ProVerticalMeter(mode="level", show_ticks=True)
        out_col.addWidget(self.meter_output, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部三模块卡片矩阵 (Formant Shifter | Pitch Shifter | Vocoder)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(14, 4, 14, 14)
        cards_row.setSpacing(9)

        st = self._current_state()
        self.formant_card = FormantShifterCard(st["formant"])
        self.formant_card.changed.connect(self._on_formant_changed)

        self.pitch_card = PitchShifterCard(st["pitch"])
        self.pitch_card.changed.connect(self._on_pitch_changed)

        self.vocoder_card = VocoderCard(st["vocoder"])
        self.vocoder_card.changed.connect(self._on_vocoder_changed)

        cards_row.addWidget(self.formant_card, 1)
        cards_row.addWidget(self.pitch_card, 1)
        cards_row.addWidget(self.vocoder_card, 1)

        main_lay.addLayout(cards_row, 4)

    def _build_meter_column(self, header_text: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        lbl = QLabel(header_text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #8E94A4; font-size: 9px; font-weight: 700; letter-spacing: 0.7px;")
        col.addWidget(lbl)
        return col

    # -----------------------------------------------------------------
    # 状态同步与双向交互联动
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_ab_styles(self):
        active_css = """
            QPushButton {
                background-color: #13242C;
                color: #28D8F0;
                border: 1px solid #28D8F0;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
            }
        """
        inactive_css = """
            QPushButton {
                background-color: #22252E;
                color: #9096A6;
                border: 1px solid #303440;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_css if self._active_bank == "A" else inactive_css)
        self.btn_b.setStyleSheet(active_css if self._active_bank == "B" else inactive_css)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

    def _refresh_all_views(self):
        st = self._current_state()
        self._update_ab_styles()
        self.power_switch.setChecked(st["enabled"])
        self.out_fader.setGain(st["output_gain"])
        self.formant_card.syncFromData(st["formant"])
        self.pitch_card.syncFromData(st["pitch"])
        self.vocoder_card.syncFromData(st["vocoder"])
        self.meter_fmt.setActive(st["enabled"] and st["formant"]["enabled"])
        self.meter_pch.setActive(st["enabled"] and st["pitch"]["enabled"])
        self.meter_voc.setActive(st["enabled"] and st["vocoder"]["enabled"])
        self.meter_clr.setActive(st["enabled"])
        self.voice_visualizer.setState(st)

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

    def _on_formant_changed(self, data: dict):
        st = self._current_state()
        st["formant"] = data
        self.meter_fmt.setActive(st["enabled"] and data["enabled"])
        self.voice_visualizer.setState(st)
        self.formantChanged.emit(copy.deepcopy(data))
        self.stateChanged.emit(self.get_state())

    def _on_pitch_changed(self, data: dict):
        st = self._current_state()
        st["pitch"] = data
        self.meter_pch.setActive(st["enabled"] and data["enabled"])
        self.voice_visualizer.setState(st)
        self.pitchChanged.emit(copy.deepcopy(data))
        self.stateChanged.emit(self.get_state())

    def _on_vocoder_changed(self, data: dict):
        st = self._current_state()
        st["vocoder"] = data
        self.meter_voc.setActive(st["enabled"] and data["enabled"])
        self.voice_visualizer.setState(st)
        self.vocoderChanged.emit(copy.deepcopy(data))
        self.stateChanged.emit(self.get_state())

    def _on_output_gain_changed(self, gain: float):
        st = self._current_state()
        st["output_gain"] = gain
        self.outputGainChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _on_canvas_identity_dragged(self, formant_st: float, pitch_st: float, f_changed: bool, p_changed: bool):
        st = self._current_state()
        if f_changed:
            st["formant"]["enabled"] = True
            st["formant"]["shift"] = formant_st
            self.formant_card.syncFromData(st["formant"])
            self.meter_fmt.setActive(st["enabled"])
            self.formantChanged.emit(copy.deepcopy(st["formant"]))

        if p_changed:
            st["pitch"]["enabled"] = True
            st["pitch"]["semitones"] = pitch_st
            self.pitch_card.syncFromData(st["pitch"])
            self.meter_pch.setActive(st["enabled"])
            self.pitchChanged.emit(copy.deepcopy(st["pitch"]))

        self.voice_visualizer.setState(st)
        self.stateChanged.emit(self.get_state())

    def _on_canvas_preserve_scrolled(self, preserve_pct: float):
        st = self._current_state()
        st["formant"]["preserve"] = preserve_pct
        self.formant_card.syncFromData(st["formant"])
        self.voice_visualizer.setState(st)
        self.formantChanged.emit(copy.deepcopy(st["formant"]))
        self.stateChanged.emit(self.get_state())

    def _on_canvas_identity_reset(self):
        st = self._current_state()
        st["formant"]["shift"] = 0.0
        st["pitch"]["semitones"] = 0.0
        self.formant_card.syncFromData(st["formant"])
        self.pitch_card.syncFromData(st["pitch"])
        self.voice_visualizer.setState(st)
        self.formantChanged.emit(copy.deepcopy(st["formant"]))
        self.pitchChanged.emit(copy.deepcopy(st["pitch"]))
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
        cur_name = self.preset_combo.currentText()
        default_name = "" if cur_name in self._builtin_preset_names else cur_name
        name, ok = QInputDialog.getText(self, "保存变声器预设", "输入预设名称（同名自定义预设将覆盖）:", text=default_name)
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
    # 演示实时人声包络模拟（接入真实音频引擎后自动停止）
    # -----------------------------------------------------------------
    def _tick_demo_analysis(self):
        self._anim_t += 0.11
        st = self._current_state()
        freqs = self.voice_visualizer._freqs

        # 模拟语音音节起伏与辅音摩擦爆发
        syllable = 0.65 + 0.35 * math.sin(self._anim_t * 1.4)
        consonant_burst = max(0.0, math.sin(self._anim_t * 3.1)) ** 4

        f_en = st["enabled"] and st["formant"]["enabled"]
        p_en = st["enabled"] and st["pitch"]["enabled"]
        v_en = st["enabled"] and st["vocoder"]["enabled"]

        f_shift = st["formant"]["shift"] if f_en else 0.0
        throat = st["formant"]["throat"] if f_en else 0.0
        gender = st["formant"]["gender"] if f_en else 0.0
        timbre = st["formant"]["timbre"] if f_en else 0.0
        f_mix = (st["formant"]["mix"] / 100.0) if f_en else 0.0

        p_shift = st["pitch"]["semitones"] if p_en else 0.0
        f_ratio = 2.0 ** (f_shift / 12.0)
        p_ratio = 2.0 ** (p_shift / 12.0)

        in_spec, out_spec = [], []
        for f in freqs:
            # 模拟原始人声三个共振峰 F1(520Hz), F2(1550Hz), F3(2650Hz) + 清辅音高频(5600Hz)
            v1 = 18.0 * math.exp(-((math.log10(f / 520.0)) ** 2) / 0.025)
            v2 = 15.0 * math.exp(-((math.log10(f / 1550.0)) ** 2) / 0.020)
            v3 = 11.0 * math.exp(-((math.log10(f / 2650.0)) ** 2) / 0.018)
            sib = 14.0 * consonant_burst * math.exp(-((math.log10(f / 5600.0)) ** 2) / 0.03)
            harm = 2.4 * math.sin(f / 135.0 * math.pi * 2.0 + self._anim_t)
            base_db = -54.0 + (v1 + v2 + v3) * syllable + sib + harm - 8.0 * math.log10(max(100.0, f) / 100.0)
            base_db = max(-75.0, min(-4.0, base_db))
            in_spec.append(base_db)

            # Gender 非对称偏移 F1/F2，Throat 缩放低频共鸣腔
            f1_scale = f_ratio * (1.0 + gender * 0.0018) * (1.0 + throat * 0.0012)
            f2_scale = f_ratio * (1.0 + gender * 0.0026)
            f3_scale = f_ratio

            ov1 = 18.5 * math.exp(-((math.log10(max(40.0, f / max(0.35, f1_scale)) / 520.0)) ** 2) / 0.024)
            ov2 = 15.5 * math.exp(-((math.log10(max(40.0, f / max(0.35, f2_scale)) / 1550.0)) ** 2) / 0.020)
            ov3 = 11.5 * math.exp(-((math.log10(max(40.0, f / max(0.35, f3_scale)) / 2650.0)) ** 2) / 0.018)
            preserve_gain = (st["formant"]["preserve"] / 100.0) if f_en else 1.0
            osib = 14.0 * consonant_burst * preserve_gain * math.exp(-((math.log10(f / 5600.0)) ** 2) / 0.03)
            oharm = 2.4 * math.sin(f / max(50.0, 135.0 * p_ratio) * math.pi * 2.0 + self._anim_t)
            tilt_db = (timbre / 50.0) * 5.0 * math.log10(max(80.0, f) / 1000.0)

            wet_db = -54.0 + (ov1 + ov2 + ov3) * syllable + osib + oharm - 8.0 * math.log10(max(100.0, f) / 100.0) + tilt_db
            blended_db = base_db * (1.0 - f_mix) + wet_db * f_mix if f_en else wet_db
            out_spec.append(max(-75.0, min(-4.0, blended_db + st["output_gain"] * 0.4)))

        f0_in = 135.0 + 18.0 * math.sin(self._anim_t * 0.9)
        f0_out = st["vocoder"]["carrier_hz"] if v_en else f0_in * p_ratio

        # 计算可懂度参考指数
        clarity = 96.0
        if st["enabled"]:
            if f_en:
                clarity -= abs(f_shift) * 0.6 * (1.0 - st["formant"]["preserve"] / 120.0)
            if v_en:
                clarity -= (32 - st["vocoder"]["bands"]) * 0.45 * (1.0 - st["vocoder"]["unvoiced"] / 110.0)
        clarity = max(45.0, min(99.0, clarity))

        in_lvl = -22.0 + 10.0 * syllable
        out_lvl = max(-55.0, min(-1.0, in_lvl + st["output_gain"]))

        self.voice_visualizer.updateAnalysis(freqs, in_spec, out_spec, f0_in, f0_out, clarity)
        self.meter_input.setValue(in_lvl)
        self.meter_output.setValue(out_lvl)
        self.meter_fmt.setValue(min(100.0, abs(f_shift) / 12.0 * 100.0 * syllable) if f_en else 0.0)
        self.meter_pch.setValue(min(100.0, abs(p_shift) / 24.0 * 100.0 * syllable) if p_en else 0.0)
        self.meter_voc.setValue(min(100.0, 82.0 * syllable) if v_en else 0.0)
        self.meter_clr.setValue(clarity)

    # -----------------------------------------------------------------
    # 公开 API：供后端 DSP 引擎与工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_voice_changer(self):
        self._banks[self._active_bank] = default_voice_neutral()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_analysis(
        self,
        freqs_hz: list[float],
        input_spec_db: list[float],
        output_spec_db: list[float],
        f0_in_hz: float,
        f0_out_hz: float,
        clarity_pct: float = 90.0,
    ):
        """接入真实音频引擎的频谱包络、基频检测与可懂度评估结果"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.voice_visualizer.updateAnalysis(freqs_hz, input_spec_db, output_spec_db, f0_in_hz, f0_out_hz, clarity_pct)

    def update_meters(
        self,
        input_db: float,
        output_db: float,
        formant_activity: float = 0.0,
        pitch_activity: float = 0.0,
        vocoder_activity: float = 0.0,
        clarity_pct: float = 90.0,
    ):
        """接入真实音频引擎的实时电平与三模块活跃度 (0~100%)"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.meter_input.setValue(input_db)
        self.meter_output.setValue(output_db)
        self.meter_fmt.setValue(formant_activity)
        self.meter_pch.setValue(pitch_activity)
        self.meter_voc.setValue(vocoder_activity)
        self.meter_clr.setValue(clarity_pct)
