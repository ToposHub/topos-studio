from __future__ import annotations

import sys
import math
import copy
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QFrame, QLineEdit, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont, QPolygonF
)


# =====================================================================
# 1. 全局色彩系统与声学常量（统一 Fairlight 深色哑光视觉规范）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":     "#28D8F0",  # 主控 / A-B 激活 / 输出主曲线
    "attack":   "#F0A848",  # Attack 起振主题色（琥珀金）
    "sustain":  "#4CD490",  # Sustain 延音主题色（翡翠绿）
    "sc":       "#6495FA",  # Sidechain Filter 主题色（钴蓝）
    "output":   "#B67CF8",  # Output / Soft Clip 主题色（紫罗兰）
    "coral":    "#F26D6D",  # 削波 / 峰值警戒色
}

SC_FREQ_MIN = 20.0
SC_FREQ_MAX = 20000.0


def format_signed_pct(val: float) -> str:
    iv = int(round(val))
    if iv == 0:
        return "0%"
    return f"{iv:+d}%"


def format_pct(val: float) -> str:
    return f"{int(round(val))}%"


def format_db_signed(val: float) -> str:
    if abs(val) < 0.05:
        return "0.0 dB"
    return f"{val:+.1f} dB"


def format_freq(hz: float) -> str:
    if hz >= 10000.0:
        return f"{hz / 1000.0:.1f}K"
    if hz >= 1000.0:
        return f"{hz / 1000.0:.2f}K"
    return f"{int(round(hz))} Hz"


def format_ms(val: float) -> str:
    if val < 10.0:
        return f"{val:.1f} ms"
    return f"{int(round(val))} ms"


def parse_numeric(text: str, fallback: float) -> float:
    s = (
        text.strip()
        .upper()
        .replace("DB", "")
        .replace("HZ", "")
        .replace("MS", "")
        .replace("%", "")
        .strip()
    )
    try:
        if s.endswith("K"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def default_transient_demo() -> dict:
    """默认演示配置：现代鼓组 Snare/Kick 强力起振增强 + 干脆收尾控制"""
    return {
        "enabled": True,
        "output_gain": 0.0,
        "auto_gain": False,
        "transient": {
            "enabled": True,
            "mode": "Punch",       # "Punch" (标准快速起振) | "Smooth" (宽包络自然瞬态)
            "attack": 42.0,        # -100 ~ +100 % (起振增益增减)
            "sustain": -28.0,      # -100 ~ +100 % (收尾延音拉长/缩短)
            "sensitivity": 55.0,   # 0 ~ 100 % (瞬态包络检测灵敏度)
            "punch": 35.0,         # 0 ~ 100 % (瞬态冲击谐波聚焦)
            "release": 85.0,       # 15 ~ 500 ms (延音包络释放时间)
            "mix": 100.0,          # 0 ~ 100 % (干湿并行混合)
        },
        "sidechain": {
            "enabled": True,
            "listen": False,       # 侧链滤波单独监听 (SC Audition)
            "hpf_freq": 95.0,      # 20 ~ 2000 Hz (过滤低频底鼓串音触发)
            "lpf_freq": 9500.0,    # 500 ~ 20000 Hz (过滤高频踩镲毛刺触发)
            "slope": 12,           # 12 | 24 dB/oct
            "focus_q": 0.7,        # 0.5 ~ 3.0 (检测器谐振聚焦)
        },
        "output_stage": {
            "enabled": True,
            "clip_mode": "Soft",   # "Soft" (模拟磁带/SPL软削波) | "Hard" (砖墙限制)
            "ceiling": -0.3,       # -12.0 ~ 0.0 dBFS (输出峰值上限)
            "drive": 18.0,         # 0 ~ 100 % (模拟电路饱和染色)
            "stereo_link": 100.0,  # 0 ~ 100 % (左右声道包络检测联动)
        },
    }


def default_transient_neutral() -> dict:
    st = default_transient_demo()
    st["output_gain"] = 0.0
    st["auto_gain"] = False
    st["transient"]["enabled"] = True
    st["transient"]["attack"] = 0.0
    st["transient"]["sustain"] = 0.0
    st["transient"]["punch"] = 0.0
    st["transient"]["mix"] = 100.0
    st["sidechain"]["enabled"] = False
    st["sidechain"]["listen"] = False
    st["sidechain"]["hpf_freq"] = 20.0
    st["sidechain"]["lpf_freq"] = 20000.0
    st["output_stage"]["drive"] = 0.0
    st["output_stage"]["ceiling"] = 0.0
    return st


# =====================================================================
# 2. 控件：现代发光电源开关（对齐 VoiceChanger / DynamicEQ）
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

    def __init__(self, text: str = "0%", accent_color: str = "#28D8F0", width: int = 62, parent=None):
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
                self._text.replace("dB", "")
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
            return ratio ** (1.0 / 2.2)
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
            return self._min + (n ** 2.2) * (self._max - self._min)
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

        p.setPen(QPen(QColor("#141417"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

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

        knob_r = 11.0
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.2
        iy1 = cy - math.sin(rad) * 3.2
        ix2 = cx + math.cos(rad) * (knob_r - 1.4)
        iy2 = cy - math.sin(rad) * (knob_r - 1.4)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 5. 控件：垂直高精电平表 & 双向增益缩减/增强表 (GR / Boost Meter)
# =====================================================================
class ProVerticalMeter(QWidget):
    """
    支持两种模式：
      - mode="level": 传统峰值电平表 (-55 ~ 0 dBFS)，含峰值保持线与削波灯
      - mode="bipolar_gr": 瞬态双向增益变化表 (-15 ~ +15 dB)，0dB 居中，
                           向上增长表示瞬态增强 (+dB)，向下增长表示增益缩减 (-dB)
    """

    def __init__(
        self,
        title: str = "",
        mode: str = "level",
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

        self.setFixedWidth(40 if show_ticks else 26)
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
            self._value = max(-15.0, min(15.0, val))
            if abs(self._value) >= abs(self._peak):
                self._peak = self._value
            else:
                self._peak *= 0.88
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
        return (v + 15.0) / 30.0

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
            ticks = [0, -10, -20, -35, -50] if self._mode == "level" else [15, 8, 0, -8, -15]
            for t in ticks:
                norm = self._val_to_norm(float(t))
                ty = bar.bottom() - norm * bar.height()
                p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
                lbl_t = f"+{t}" if (self._mode == "bipolar_gr" and t > 0) else f"{t}"
                p.drawText(
                    QRectF(0, ty - 7, bar.left() - 3, 14),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{lbl_t} -",
                )

        # 表槽底色
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        if self._mode == "bipolar_gr":
            mid_y = bar.center().y()
            p.setPen(QPen(QColor("#484852"), 1.0))
            p.drawLine(QPointF(bar.left() + 0.5, mid_y), QPointF(bar.right() - 0.5, mid_y))

            if self._active and abs(self._value) > 0.15:
                half_h = bar.height() * 0.5
                ratio = min(1.0, abs(self._value) / 15.0)
                fh = half_h * ratio
                if self._value > 0:
                    fill_rect = QRectF(bar.left() + 1.0, mid_y - fh, bar.width() - 2.0, fh)
                    col = QColor(COLORS["attack"])
                else:
                    fill_rect = QRectF(bar.left() + 1.0, mid_y, bar.width() - 2.0, fh)
                    col = QColor(COLORS["sustain"])
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(fill_rect, 1.2, 1.2)

            if self._active and abs(self._peak) > 0.25:
                py = bar.center().y() - (self._peak / 15.0) * (bar.height() * 0.5)
                p.setPen(QPen(QColor("#ECECF0"), 1.3))
                p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))
        else:
            if self._active:
                norm = max(0.0, min(1.0, self._val_to_norm(self._value)))
                fill_h = norm * bar.height()
                if fill_h > 1.0:
                    fill_rect = QRectF(bar.left() + 1.0, bar.bottom() - fill_h, bar.width() - 2.0, fill_h - 0.5)
                    grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                    grad.setColorAt(0.0,  QColor("#26C281"))
                    grad.setColorAt(0.52, QColor("#4CD490"))
                    grad.setColorAt(0.75, QColor("#F0A848"))
                    grad.setColorAt(1.0,  QColor("#FF4848"))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(grad)
                    p.drawRoundedRect(fill_rect, 1.2, 1.2)

                p_norm = max(0.0, min(1.0, self._val_to_norm(self._peak)))
                if p_norm > 0.02:
                    py = bar.bottom() - p_norm * bar.height()
                    p.setPen(QPen(QColor("#ECECF0"), 1.3))
                    p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

                clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
                p.drawRoundedRect(clip_rect, 1.0, 1.0)

        # 精细刻度栅格遮罩
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
# 7. 控件：核心交互式“瞬态波形包络 + 动态增益曲线 + 2D 瞬态锚点画布”
# =====================================================================
class ProTransientVisualizerWidget(QWidget):
    """
    核心时域包络与瞬态动态增益画布：
      - 灰蓝虚线：输入干声包络 (Input Envelope)
      - 冰晶青实线：瞬态整形后输出包络 (Processed Output Envelope)
      - 琥珀金/翡翠绿双向增益轨迹：实时起振增强 (+dB) 与延音衰减 (-dB) 包络
      - 2D 瞬态控制锚点：X 轴 = Sustain (-100~+100%), Y 轴 = Attack (-100~+100%)
    """
    transientDragged = pyqtSignal(float, float, bool, bool)  # sustain_pct, attack_pct, s_changed, a_changed
    sensitivityScrolled = pyqtSignal(float)                  # sensitivity_pct
    transientResetRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(460, 230)
        self.setMouseTracking(True)

        self._state = default_transient_demo()
        self._show_input_ghost = True
        self._show_gain_trace = True
        self._show_xy_puck = True

        self._hover_puck = False
        self._dragging_puck = False
        self._last_drag_pos = QPointF()
        self._accum_sustain = 0.0
        self._accum_attack = 0.0

        self._btn_in_rect = QRectF()
        self._btn_gr_rect = QRectF()
        self._btn_xy_rect = QRectF()

        self._ml = 36.0
        self._mr = 36.0
        self._mt = 10.0
        self._mb = 24.0

        # 时域包络采样缓冲区 (240帧滚动历史窗口，约 2.4 秒鼓组打击乐窗口)
        self._num_pts = 240
        self._in_env = [0.02] * self._num_pts       # 0.0 ~ 1.0 线性幅度
        self._out_env = [0.02] * self._num_pts      # 0.0 ~ 1.0 线性幅度
        self._gain_env_db = [0.0] * self._num_pts   # -15.0 ~ +15.0 dB 动态增益变化
        self._att_peak_db = 0.0
        self._sus_peak_db = 0.0

    def setState(self, state: dict):
        self._state = state
        self.update()

    def pushEnvelopeFrame(
        self,
        in_amp: float,
        out_amp: float,
        gain_delta_db: float,
        att_db: float = 0.0,
        sus_db: float = 0.0,
    ):
        """流式推入单帧时域包络采样 (供真实音频引擎或演示模拟器调用)"""
        self._in_env.pop(0)
        self._in_env.append(max(0.0, min(1.0, in_amp)))

        self._out_env.pop(0)
        self._out_env.append(max(0.0, min(1.0, out_amp)))

        self._gain_env_db.pop(0)
        self._gain_env_db.append(max(-15.0, min(15.0, gain_delta_db)))

        self._att_peak_db = att_db
        self._sus_peak_db = sus_db
        self.update()

    def setEnvelopeBuffer(
        self,
        in_amps: list[float],
        out_amps: list[float],
        gain_deltas_db: list[float],
        att_db: float = 0.0,
        sus_db: float = 0.0,
    ):
        """整帧刷新时域包络波形"""
        if len(in_amps) > 1:
            self._in_env = in_amps
            self._out_env = out_amps
            self._gain_env_db = gain_deltas_db
            self._att_peak_db = att_db
            self._sus_peak_db = sus_db
            self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(100.0, self.width() - self._ml - self._mr),
            max(100.0, self.height() - self._mt - self._mb),
        )

    def _amp_to_y(self, amp: float, plot: QRectF) -> float:
        a = max(0.0, min(1.0, amp))
        return plot.bottom() - a * (plot.height() * 0.88)

    def _gain_db_to_y(self, db: float, plot: QRectF) -> float:
        clamped = max(-15.0, min(15.0, db))
        mid_y = plot.top() + plot.height() * 0.48
        return mid_y - (clamped / 15.0) * (plot.height() * 0.42)

    # 2D 瞬态锚点坐标映射：
    # X 轴：Sustain (-100% ~ +100%)
    # Y 轴：Attack  (-100% ~ +100%)
    def _puck_pos(self, plot: QRectF) -> QPointF:
        tr = self._state["transient"]
        s_val = tr["sustain"] if tr["enabled"] else 0.0
        a_val = tr["attack"] if tr["enabled"] else 0.0
        nx = (s_val + 100.0) / 200.0
        ny = (100.0 - a_val) / 200.0
        px = plot.left() + plot.width() * (0.14 + nx * 0.72)
        py = plot.top() + plot.height() * (0.16 + ny * 0.68)
        return QPointF(px, py)

    # -----------------------------------------------------------------
    # 交互事件：拖拽 2D 瞬态锚点 / 滚轮调检测灵敏度 / 双击复位
    # -----------------------------------------------------------------
    def leaveEvent(self, event):
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
            if self._btn_in_rect.contains(pos):
                self._show_input_ghost = not self._show_input_ghost
                self.update()
                return
            if self._btn_gr_rect.contains(pos):
                self._show_gain_trace = not self._show_gain_trace
                self.update()
                return
            if self._btn_xy_rect.contains(pos):
                self._show_xy_puck = not self._show_xy_puck
                self.update()
                return

            if on_puck:
                self._dragging_puck = True
                self._last_drag_pos = pos
                tr = self._state["transient"]
                self._accum_sustain = float(tr["sustain"] if tr["enabled"] else 0.0)
                self._accum_attack = float(tr["attack"] if tr["enabled"] else 0.0)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()

        elif event.button() == Qt.MouseButton.RightButton and on_puck:
            self.transientResetRequested.emit()

    def mouseMoveEvent(self, event):
        plot = self._plot_rect()
        if self._dragging_puck:
            dx = event.position().x() - self._last_drag_pos.x()
            dy = event.position().y() - self._last_drag_pos.y()
            self._last_drag_pos = event.position()

            mods = event.modifiers()
            scale = 0.2 if (mods & Qt.KeyboardModifier.ShiftModifier) else 1.0
            span_x = max(1.0, plot.width() * 0.72)
            span_y = max(1.0, plot.height() * 0.68)

            lock_a = bool(mods & Qt.KeyboardModifier.ControlModifier)
            lock_s = bool(mods & Qt.KeyboardModifier.AltModifier)

            if not lock_s:
                self._accum_sustain = max(-100.0, min(100.0, self._accum_sustain + (dx / span_x) * 200.0 * scale))
            if not lock_a:
                self._accum_attack = max(-100.0, min(100.0, self._accum_attack - (dy / span_y) * 200.0 * scale))

            s_pct = round(self._accum_sustain, 0)
            a_pct = round(self._accum_attack, 0)
            self.transientDragged.emit(s_pct, a_pct, not lock_s, not lock_a)
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
            self.transientResetRequested.emit()

    def wheelEvent(self, event):
        if self._plot_rect().contains(event.position()):
            delta = event.angleDelta().y() / 120.0
            step = 1.0 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 4.0
            cur_s = self._state["transient"]["sensitivity"]
            new_s = max(0.0, min(100.0, round(cur_s + delta * step, 0)))
            self.sensitivityScrolled.emit(new_s)
            event.accept()

    # -----------------------------------------------------------------
    # 绘制网格、时域波形包络、双向动态增益曲线、侧链状态与 2D 锚点
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 画布哑光底色
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 水平网格与左右双刻度（左：包络电平 dBFS | 右：瞬态动态增益变化 ±dB）
        grid_pen = QPen(QColor("#26262A"), 1.0)
        zero_gain_y = self._gain_db_to_y(0.0, plot)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        left_labels = ["0", "-6", "-12", "-24", "-48"]
        right_labels = ["+15", "+7.5", "0 dB", "-7.5", "-15"]
        for i in range(5):
            y = plot.top() + (i / 4.0) * plot.height()
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

            p.setPen(QColor("#9A9AA2"))
            p.drawText(QRectF(0, y - 8, self._ml - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, left_labels[i])
            p.drawText(QRectF(plot.right() + 6, y - 8, self._mr - 6, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, right_labels[i])

        # 动态增益 0dB 基准虚线
        p.setPen(QPen(QColor("#3A3A44"), 1.0, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(plot.left(), zero_gain_y), QPointF(plot.right(), zero_gain_y))

        # 3. 垂直时间轴网格 (-2.0s ~ NOW)
        time_ticks = [
            (0.0, "-2.4s"), (0.25, "-1.8s"), (0.50, "-1.2s"), (0.75, "-0.6s"), (1.0, "LIVE")
        ]
        for norm_t, t_lbl in time_ticks:
            x = plot.left() + norm_t * plot.width()
            if 0.0 < norm_t < 1.0:
                p.setPen(grid_pen)
                p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#28D8F0") if norm_t == 1.0 else QColor("#9A9AA2"))
            align = Qt.AlignmentFlag.AlignLeft if norm_t == 0.0 else (Qt.AlignmentFlag.AlignRight if norm_t == 1.0 else Qt.AlignmentFlag.AlignCenter)
            tx = x if norm_t == 0.0 else (x - 34 if norm_t == 1.0 else x - 18)
            p.drawText(QRectF(tx, plot.bottom() + 5, 34, 16), align, t_lbl)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        st = self._state
        enabled = st["enabled"]
        n_pts = len(self._in_env)

        # 4. 绘制输入原始包络 (Input Dry Envelope - 虚线参考)
        if self._show_input_ghost and n_pts > 1:
            in_path = QPainterPath()
            for i, amp in enumerate(self._in_env):
                x = plot.left() + (i / (n_pts - 1)) * plot.width()
                y = self._amp_to_y(amp, plot)
                if i == 0:
                    in_path.moveTo(x, y)
                else:
                    in_path.lineTo(x, y)
            p.setPen(QPen(QColor(145, 155, 175, 120), 1.25, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(in_path)

        # 5. 绘制输出整形后包络 (Processed Output Envelope - 主曲线与填充)
        if n_pts > 1:
            out_path = QPainterPath()
            out_poly = QPolygonF()
            out_poly.append(QPointF(plot.left(), plot.bottom()))

            for i in range(n_pts):
                x = plot.left() + (i / (n_pts - 1)) * plot.width()
                amp = self._out_env[i] if enabled else self._in_env[i]
                y = self._amp_to_y(amp, plot)
                if i == 0:
                    out_path.moveTo(x, y)
                else:
                    out_path.lineTo(x, y)
                out_poly.append(QPointF(x, y))

            out_poly.append(QPointF(plot.right(), plot.bottom()))

            wave_col = QColor(COLORS["cyan"]) if enabled else QColor("#7E828E")
            fill_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            fill_grad.setColorAt(0.0, QColor(wave_col.red(), wave_col.green(), wave_col.blue(), 46))
            fill_grad.setColorAt(1.0, QColor(wave_col.red(), wave_col.green(), wave_col.blue(), 4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill_grad)
            p.drawPolygon(out_poly)

            p.setPen(QPen(wave_col, 1.75))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(out_path)

        # 6. 绘制双向瞬态动态增益轨迹 (Attack Boost 琥珀金向上 / Sustain Cut 翡翠绿向下)
        if enabled and self._show_gain_trace and len(self._gain_env_db) == n_pts and n_pts > 1:
            gain_path = QPainterPath()
            pos_poly = QPolygonF()
            neg_poly = QPolygonF()
            pos_poly.append(QPointF(plot.left(), zero_gain_y))
            neg_poly.append(QPointF(plot.left(), zero_gain_y))

            for i, g_db in enumerate(self._gain_env_db):
                x = plot.left() + (i / (n_pts - 1)) * plot.width()
                gy = self._gain_db_to_y(g_db, plot)
                if i == 0:
                    gain_path.moveTo(x, gy)
                else:
                    gain_path.lineTo(x, gy)

                pos_poly.append(QPointF(x, min(zero_gain_y, gy)))
                neg_poly.append(QPointF(x, max(zero_gain_y, gy)))

            pos_poly.append(QPointF(plot.right(), zero_gain_y))
            neg_poly.append(QPointF(plot.right(), zero_gain_y))

            # Attack 正向增益填充（琥珀金）
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(240, 168, 72, 52))
            p.drawPolygon(pos_poly)

            # Sustain 负向衰减填充（翡翠绿）
            p.setBrush(QColor(76, 212, 144, 48))
            p.drawPolygon(neg_poly)

            # 实时动态增益曲线描边
            p.setPen(QPen(QColor("#F0A848"), 1.65))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(gain_path)

        # 7. 2D 瞬态控制锚点 (X: Sustain / Y: Attack)
        if enabled and self._show_xy_puck:
            pp = self._puck_pos(plot)
            is_hi = self._hover_puck or self._dragging_puck
            r = 12.0 if is_hi else 9.8

            if is_hi:
                cx0 = plot.left() + plot.width() * 0.50
                cy0 = plot.top() + plot.height() * 0.50
                p.setPen(QPen(QColor(255, 255, 255, 30), 1.0, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(cx0, plot.top() + 30), QPointF(cx0, plot.bottom() - 18))
                p.drawLine(QPointF(plot.left() + 8, cy0), QPointF(plot.right() - 8, cy0))

                p.setFont(QFont(MONO_FONT_FAM, 7))
                p.setPen(QColor(170, 180, 198, 135))
                p.drawText(QRectF(plot.left() + 10, cy0 - 15, 110, 14), Qt.AlignmentFlag.AlignLeft, "← TIGHT / GATE")
                p.drawText(QRectF(plot.right() - 115, cy0 - 15, 105, 14), Qt.AlignmentFlag.AlignRight, "ROOM / BLOOM →")
                p.drawText(QRectF(cx0 + 6, plot.top() + 32, 95, 14), Qt.AlignmentFlag.AlignLeft, "↑ PUNCH ATTACK")
                p.drawText(QRectF(cx0 + 6, plot.bottom() - 28, 95, 14), Qt.AlignmentFlag.AlignLeft, "↓ SOFT ATTACK")

            p.setPen(QPen(QColor(240, 168, 72, 90 if is_hi else 34), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(pp.x(), plot.top()), QPointF(pp.x(), plot.bottom()))
            p.drawLine(QPointF(plot.left(), pp.y()), QPointF(plot.right(), pp.y()))

            if is_hi:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(240, 168, 72, 58))
                p.drawEllipse(pp, r + 6.5, r + 6.5)

            p.setPen(QPen(QColor("#F0A848"), 2.0))
            p.setBrush(QColor(21, 21, 24, 228))
            p.drawEllipse(pp, r, r)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#4CD490"))
            p.drawEllipse(pp, 3.6, 3.6)

            if self._dragging_puck:
                tr = st["transient"]
                tip_txt = f"ATT {tr['attack']:+.0f}%  |  SUS {tr['sustain']:+.0f}%"
                tip_w, tip_h = 136.0, 18.0
                tx = min(plot.right() - tip_w - 6, max(plot.left() + 6, pp.x() + 16))
                ty = max(plot.top() + 32, pp.y() - 24)
                tip_rect = QRectF(tx, ty, tip_w, tip_h)
                p.setPen(QPen(QColor("#F0A848"), 1.0))
                p.setBrush(QColor(14, 16, 20, 238))
                p.drawRoundedRect(tip_rect, 3.5, 3.5)
                p.setPen(QColor("#ECECF0"))
                p.setFont(QFont(MONO_FONT_FAM, 7))
                p.drawText(tip_rect, Qt.AlignmentFlag.AlignCenter, tip_txt)

        # 8. 右上角视觉层切换胶囊 (DRY / GAIN / 2D XY)
        self._btn_in_rect = QRectF(plot.right() - 134, plot.top() + 8, 38, 18)
        self._btn_gr_rect = QRectF(plot.right() - 92, plot.top() + 8, 40, 18)
        self._btn_xy_rect = QRectF(plot.right() - 48, plot.top() + 8, 40, 18)

        pills = [
            (self._btn_in_rect, "DRY",   self._show_input_ghost, "#28D8F0", "#24788C"),
            (self._btn_gr_rect, "ENV",   self._show_gain_trace,  "#F0A848", "#8C6224"),
            (self._btn_xy_rect, "2D XY", self._show_xy_puck,     "#4CD490", "#2A7A54"),
        ]
        for rect, label, active, fg_hex, bd_hex in pills:
            p.setPen(QPen(QColor(bd_hex) if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 34, 42, 215) if active else QColor(21, 21, 24, 185))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor(fg_hex) if active else QColor("#787882"))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 9. 左上角实时声学信息 HUD
        tr = st["transient"]
        sc = st["sidechain"]
        a_val = tr["attack"] if (enabled and tr["enabled"]) else 0.0
        s_val = tr["sustain"] if (enabled and tr["enabled"]) else 0.0
        sc_str = f"SC {format_freq(sc['hpf_freq'])}–{format_freq(sc['lpf_freq'])}" if (enabled and sc["enabled"]) else "SC Full"
        hud_str = (
            f"Attack {a_val:+.0f}%  •  Sustain {s_val:+.0f}%  •  "
            f"Sens {int(tr['sensitivity'])}%  •  {sc_str}"
        )
        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, 345, 19)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor("#F0A848") if self._dragging_puck else QColor("#C6CCD8"))
        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 8. 通用旋钮参数单元（标签 + 旋钮 + 刻度提示 + 可编辑数值胶囊）
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
        self.title_lbl.setStyleSheet("color: #9A9AA2; font-size: 10px; font-weight: 600;")
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
            hl.setStyleSheet("color: #787882; font-size: 9px;")
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
# 9. 迷你侧链带通频响预览小窗 (嵌于 Sidechain Filter 卡片内)
# =====================================================================
class SidechainMiniResponseWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._hpf = 95.0
        self._lpf = 9500.0
        self._slope = 12
        self._active = True
        self.setFixedHeight(44)

    def setParams(self, hpf: float, lpf: float, slope: int, active: bool):
        self._hpf = hpf
        self._lpf = lpf
        self._slope = slope
        self._active = active
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#18181B"))
        p.drawRoundedRect(rect, 3.0, 3.0)

        p.save()
        p.setClipRect(rect.adjusted(1, 1, -1, -1))

        # 频率刻度线
        p.setPen(QPen(QColor("#25252B"), 1.0))
        for f in (100.0, 1000.0, 10000.0):
            nx = math.log10(f / SC_FREQ_MIN) / math.log10(SC_FREQ_MAX / SC_FREQ_MIN)
            x = rect.left() + nx * rect.width()
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))

        order = max(1, self._slope // 6)
        steps = 90
        path = QPainterPath()
        poly = QPolygonF()
        poly.append(QPointF(rect.left(), rect.bottom()))

        for i in range(steps + 1):
            norm = i / steps
            f = SC_FREQ_MIN * ((SC_FREQ_MAX / SC_FREQ_MIN) ** norm)
            x = rect.left() + norm * rect.width()

            hp_sq = 1.0 / (1.0 + ((self._hpf / max(1.0, f)) ** (2 * order)))
            lp_sq = 1.0 / (1.0 + ((f / max(1.0, self._lpf)) ** (2 * order)))
            db = max(-36.0, 10.0 * math.log10(max(1e-9, hp_sq * lp_sq)))

            ny = (-db) / 36.0
            y = rect.top() + 5.0 + ny * (rect.height() - 8.0)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
            poly.append(QPointF(x, y))

        poly.append(QPointF(rect.right(), rect.bottom()))

        col = QColor(COLORS["sc"]) if self._active else QColor("#565A66")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(col.red(), col.green(), col.blue(), 42 if self._active else 16))
        p.drawPolygon(poly)

        p.setPen(QPen(col, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        p.restore()


# =====================================================================
# 10. 模块卡片 1：Transient Shaper Card（Attack ± & Sustain ± 核心整形）
# =====================================================================
class TransientShaperCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["attack"]
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

        self.title_lbl = QLabel("Transient Shaper")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # Punch / Smooth 包络检测响应模式
        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.btn_punch = QPushButton("Punch")
        self.btn_smooth = QPushButton("Smooth")
        for b in (self.btn_punch, self.btn_smooth):
            b.setCheckable(True)
            b.setFixedSize(48, 19)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_lay.addWidget(b)

        self.btn_punch.clicked.connect(lambda: self._on_mode("Punch"))
        self.btn_smooth.clicked.connect(lambda: self._on_mode("Smooth"))
        h_lay.addWidget(seg_wrap)
        root.addWidget(header)

        # Body (2x3 旋钮网格：Attack 琥珀金 + Sustain 翡翠绿 双核心高亮)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_attack = KnobParameterCell(
            "Attack", -100.0, 100.0, 0.0, COLORS["attack"],
            "-100", "%", "+100",
            format_signed_pct, parse_numeric, mode="bipolar"
        )
        self.c_sustain = KnobParameterCell(
            "Sustain", -100.0, 100.0, 0.0, COLORS["sustain"],
            "-100", "%", "+100",
            format_signed_pct, parse_numeric, mode="bipolar"
        )
        self.c_sens = KnobParameterCell(
            "Sensitivity", 0.0, 100.0, 55.0, COLORS["cyan"],
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_attack, self.c_sustain, self.c_sens):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_punch = KnobParameterCell(
            "Punch", 0.0, 100.0, 35.0, COLORS["attack"],
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_release = KnobParameterCell(
            "Release", 15.0, 500.0, 85.0, COLORS["sustain"],
            "15", "ms", "500",
            format_ms, parse_numeric, mode="log"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 100.0, COLORS["cyan"],
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_punch, self.c_release, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_attack.valueChanged.connect(lambda v: self._set_field("attack", round(v, 0)))
        self.c_sustain.valueChanged.connect(lambda v: self._set_field("sustain", round(v, 0)))
        self.c_sens.valueChanged.connect(lambda v: self._set_field("sensitivity", round(v, 0)))
        self.c_punch.valueChanged.connect(lambda v: self._set_field("punch", round(v, 0)))
        self.c_release.valueChanged.connect(lambda v: self._set_field("release", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        is_punch = (self._data["mode"] == "Punch")

        self.setStyleSheet("""
            QFrame#ModuleCard {
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
                border-radius: 0px;
            }
            QFrame#CardBody {
                background-color: transparent;
                border-top: 1px solid #141416;
            }
        """)

        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#6E6E76'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        self.btn_punch.setChecked(is_punch)
        self.btn_smooth.setChecked(not is_punch)
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
        self.btn_punch.setStyleSheet(active_seg if is_punch else inactive_seg)
        self.btn_smooth.setStyleSheet(active_seg if not is_punch else inactive_seg)

        self.c_attack.setValue(self._data["attack"])
        self.c_sustain.setValue(self._data["sustain"])
        self.c_sens.setValue(self._data["sensitivity"])
        self.c_punch.setValue(self._data["punch"])
        self.c_release.setValue(self._data["release"])
        self.c_mix.setValue(self._data["mix"])

        for cell in (self.c_attack, self.c_sustain, self.c_sens, self.c_punch, self.c_release, self.c_mix):
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
# 11. 模块卡片 2：Sidechain Filter Card（侧链检测滤波 · 防止串音误触发）
# =====================================================================
class SidechainFilterCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["sc"]
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
        h_lay.setSpacing(6)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Sidechain Filter")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # 侧链滤波单独监听按钮 (Listen)
        self.listen_btn = QPushButton("Listen")
        self.listen_btn.setCheckable(True)
        self.listen_btn.setFixedSize(46, 20)
        self.listen_btn.setToolTip("单独监听侧链滤波器输出的触发检测信号（过滤底鼓/踩镲串音）")
        self.listen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.listen_btn.clicked.connect(self._on_listen)
        h_lay.addWidget(self.listen_btn)

        # 12 / 24 dB/oct 斜率切换
        self.slope_btn = QPushButton("12dB")
        self.slope_btn.setFixedSize(38, 20)
        self.slope_btn.setToolTip("切换侧链滤波器滚降斜率 (12dB / 24dB)")
        self.slope_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slope_btn.clicked.connect(self._on_toggle_slope)
        h_lay.addWidget(self.slope_btn)

        root.addWidget(header)

        # Body：顶部迷你侧链频响图 + 底部 3 旋钮 (HPF / LPF / Focus Q)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(8)

        self.mini_resp = SidechainMiniResponseWidget()
        b_lay.addWidget(self.mini_resp)

        knobs_row = QHBoxLayout()
        knobs_row.setSpacing(4)
        self.c_hpf = KnobParameterCell(
            "SC HPF", 20.0, 2000.0, 95.0, self._accent,
            "20", "Hz", "2K",
            format_freq, parse_numeric, mode="log"
        )
        self.c_lpf = KnobParameterCell(
            "SC LPF", 500.0, 20000.0, 9500.0, self._accent,
            "500", "Hz", "20K",
            format_freq, parse_numeric, mode="log"
        )
        self.c_q = KnobParameterCell(
            "Focus Q", 0.5, 3.0, 0.7, self._accent,
            "0.5", "Q", "3.0",
            lambda v: f"{v:.1f}", parse_numeric, mode="linear"
        )
        for c in (self.c_hpf, self.c_lpf, self.c_q):
            knobs_row.addWidget(c, 1)
        b_lay.addLayout(knobs_row, 1)

        root.addWidget(body, 1)

        self.c_hpf.valueChanged.connect(lambda v: self._set_field("hpf_freq", round(v, 1)))
        self.c_lpf.valueChanged.connect(lambda v: self._set_field("lpf_freq", round(v, 1)))
        self.c_q.valueChanged.connect(lambda v: self._set_field("focus_q", round(v, 2)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])

        self.setStyleSheet("""
            QFrame#ModuleCard {
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
                border-radius: 0px;
            }
            QFrame#CardBody {
                background-color: transparent;
                border-top: 1px solid #141416;
            }
        """)

        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#6E6E76'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        self.listen_btn.setChecked(bool(self._data["listen"]))
        self.listen_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #3A2E18;
                color: #F0A848;
                border: 1px solid #F0A848;
                font-weight: 700;
            }
        """)

        self.slope_btn.setText(f"{self._data['slope']}dB")
        self.slope_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238;
                color: {self._accent if en else '#787882'};
                border: none;
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 700;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
        """)

        self.mini_resp.setParams(self._data["hpf_freq"], self._data["lpf_freq"], self._data["slope"], en)

        self.c_hpf.setValue(self._data["hpf_freq"])
        self.c_lpf.setValue(self._data["lpf_freq"])
        self.c_q.setValue(self._data["focus_q"])

        for cell in (self.c_hpf, self.c_lpf, self.c_q):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_listen(self, checked: bool):
        self._data["listen"] = checked
        self._emit()

    def _on_toggle_slope(self):
        self._data["slope"] = 24 if self._data["slope"] == 12 else 12
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        self.mini_resp.setParams(self._data["hpf_freq"], self._data["lpf_freq"], self._data["slope"], self._data["enabled"])
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 12. 模块卡片 3：Output & Soft Clip Card（模拟饱和染色与峰值削波保护）
# =====================================================================
class OutputLimiterCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["output"]
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

        self.title_lbl = QLabel("Output & Clip")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # Soft / Hard 削波曲线双态切换
        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.btn_soft = QPushButton("Soft")
        self.btn_hard = QPushButton("Hard")
        for b in (self.btn_soft, self.btn_hard):
            b.setCheckable(True)
            b.setFixedSize(42, 19)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_lay.addWidget(b)

        self.btn_soft.clicked.connect(lambda: self._on_clip_mode("Soft"))
        self.btn_hard.clicked.connect(lambda: self._on_clip_mode("Hard"))
        h_lay.addWidget(seg_wrap)
        root.addWidget(header)

        # Body
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        # 提示标签条（说明防瞬态过载保护）
        info_banner = QLabel("SPL Analog Saturation & Transient Peak Clipper")
        info_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_banner.setFixedHeight(22)
        info_banner.setStyleSheet("""
            background-color: #18181B;
            color: #8A8A94;
            border: 1px solid #141416;
            border-radius: 3px;
            font-size: 9.5px;
            font-weight: 600;
        """)
        b_lay.addWidget(info_banner)

        knobs_row = QHBoxLayout()
        knobs_row.setSpacing(4)
        self.c_drive = KnobParameterCell(
            "Analog Drive", 0.0, 100.0, 18.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_ceiling = KnobParameterCell(
            "Ceiling", -12.0, 0.0, -0.3, COLORS["coral"],
            "-12", "dBFS", "0.0",
            format_db_signed, parse_numeric, mode="linear"
        )
        self.c_link = KnobParameterCell(
            "Stereo Link", 0.0, 100.0, 100.0, COLORS["cyan"],
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_drive, self.c_ceiling, self.c_link):
            knobs_row.addWidget(c, 1)
        b_lay.addLayout(knobs_row, 1)

        root.addWidget(body, 1)

        self.c_drive.valueChanged.connect(lambda v: self._set_field("drive", round(v, 0)))
        self.c_ceiling.valueChanged.connect(lambda v: self._set_field("ceiling", round(v, 1)))
        self.c_link.valueChanged.connect(lambda v: self._set_field("stereo_link", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        is_soft = (self._data["clip_mode"] == "Soft")

        self.setStyleSheet("""
            QFrame#ModuleCard {
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
                border-radius: 0px;
            }
            QFrame#CardBody {
                background-color: transparent;
                border-top: 1px solid #141416;
            }
        """)

        self.power_sw.setChecked(en)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#6E6E76'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        self.btn_soft.setChecked(is_soft)
        self.btn_hard.setChecked(not is_soft)
        active_seg = f"""
            QPushButton {{
                background-color: #281E38;
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
        self.btn_soft.setStyleSheet(active_seg if is_soft else inactive_seg)
        self.btn_hard.setStyleSheet(active_seg if not is_soft else inactive_seg)

        self.c_drive.setValue(self._data["drive"])
        self.c_ceiling.setValue(self._data["ceiling"])
        self.c_link.setValue(self._data["stereo_link"])

        for cell in (self.c_drive, self.c_ceiling, self.c_link):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_clip_mode(self, mode: str):
        if self._data["clip_mode"] != mode:
            self._data["clip_mode"] = mode
            self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 13. 主面板：旗舰瞬态塑形器 (Studio Transient Designer)
# =====================================================================
class StudioTransientDesignerWidget(QWidget):
    """
    瞬态设计器主界面，提供完整信号接口供后端 DSP 引擎订阅：
      - bypassChanged(bool)
      - transientChanged(dict)         # Attack, Sustain, Sensitivity, Punch, Release, Mix, Mode
      - sidechainChanged(dict)         # Enabled, Listen, HPF, LPF, Slope, Focus Q
      - outputStageChanged(dict)       # Enabled, Clip Mode, Ceiling, Drive, Stereo Link
      - outputGainChanged(float)
      - autoGainToggled(bool)
      - stateChanged(dict)             # 完整参数快照
    """
    bypassChanged = pyqtSignal(bool)
    transientChanged = pyqtSignal(dict)
    sidechainChanged = pyqtSignal(dict)
    outputStageChanged = pyqtSignal(dict)
    outputGainChanged = pyqtSignal(float)
    autoGainToggled = pyqtSignal(bool)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Drum Bus / Snare", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Transient Designer — {track_name}")
        self.resize(980, 680)
        self.setMinimumSize(900, 600)

        self._active_bank = "A"
        self._banks = {
            "A": default_transient_demo(),
            "B": default_transient_neutral(),
        }

        self._presets = {
            # ================= 1. 鼓组核心单轨 (Kick / Snare / Toms) =================
            "Snare Crack & Tight Gate (军鼓炸裂收尾)": copy.deepcopy(self._banks["A"]),
            "Snare Fat Body & Ring (厚实军鼓腔体共鸣)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": True,
                "transient": {"enabled": True, "mode": "Smooth", "attack": 24.0, "sustain": 45.0, "sensitivity": 52.0, "punch": 28.0, "release": 180.0, "mix": 90.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 120.0, "lpf_freq": 6500.0, "slope": 12, "focus_q": 0.9},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 26.0, "stereo_link": 100.0},
            },
            "Snare Bleed Remover (军鼓串音强力门限)": {
                "enabled": True,
                "output_gain": 0.5,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 32.0, "sustain": -78.0, "sensitivity": 66.0, "punch": 30.0, "release": 45.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 160.0, "lpf_freq": 7500.0, "slope": 24, "focus_q": 1.1},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 15.0, "stereo_link": 100.0},
            },
            "Kick Sub Punch (底鼓强力冲击)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 54.0, "sustain": -35.0, "sensitivity": 60.0, "punch": 48.0, "release": 75.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 35.0, "lpf_freq": 2800.0, "slope": 24, "focus_q": 1.1},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 24.0, "stereo_link": 100.0},
            },
            "Kick Click & Beater Attack (金属/摇滚底鼓锤头点音)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 72.0, "sustain": -50.0, "sensitivity": 68.0, "punch": 65.0, "release": 55.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 45.0, "lpf_freq": 6000.0, "slope": 12, "focus_q": 1.2},
                "output_stage": {"enabled": True, "clip_mode": "Hard", "ceiling": -0.2, "drive": 30.0, "stereo_link": 100.0},
            },
            "808 / Sub Boom Tail Extend (808底鼓超长低频延音)": {
                "enabled": True,
                "output_gain": -1.0,
                "auto_gain": True,
                "transient": {"enabled": True, "mode": "Smooth", "attack": -15.0, "sustain": 64.0, "sensitivity": 45.0, "punch": 20.0, "release": 360.0, "mix": 95.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 25.0, "lpf_freq": 850.0, "slope": 24, "focus_q": 0.8},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 38.0, "stereo_link": 100.0},
            },
            "Toms Punch & Clean Decay (通鼓饱满起振与干净衰减)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 44.0, "sustain": -48.0, "sensitivity": 58.0, "punch": 36.0, "release": 140.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 65.0, "lpf_freq": 4500.0, "slope": 24, "focus_q": 0.9},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 20.0, "stereo_link": 100.0},
            },

            # ================= 2. 镲片、房间麦与鼓组总线 (OH / Room / Drum Bus) =================
            "Hi-Hat & Percussion Tightener (踩镲颗粒感收紧)": {
                "enabled": True,
                "output_gain": 0.5,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 28.0, "sustain": -62.0, "sensitivity": 68.0, "punch": 22.0, "release": 40.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 1800.0, "lpf_freq": 18000.0, "slope": 24, "focus_q": 0.8},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 10.0, "stereo_link": 100.0},
            },
            "Overheads Cymbal Tamer (吊顶麦刺耳镲片柔化)": {
                "enabled": True,
                "output_gain": 1.0,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Smooth", "attack": -45.0, "sustain": 16.0, "sensitivity": 62.0, "punch": 0.0, "release": 160.0, "mix": 85.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 900.0, "lpf_freq": 16000.0, "slope": 12, "focus_q": 0.7},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.5, "drive": 14.0, "stereo_link": 85.0},
            },
            "Drum Room Smash & Bloom (房间麦延音轰鸣)": {
                "enabled": True,
                "output_gain": -1.5,
                "auto_gain": True,
                "transient": {"enabled": True, "mode": "Smooth", "attack": -32.0, "sustain": 68.0, "sensitivity": 50.0, "punch": 15.0, "release": 240.0, "mix": 85.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 110.0, "lpf_freq": 12000.0, "slope": 12, "focus_q": 0.7},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.5, "drive": 32.0, "stereo_link": 100.0},
            },
            "Drum Bus Glue & Snap (鼓组总线粘合与弹跳感)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": True,
                "transient": {"enabled": True, "mode": "Punch", "attack": 22.0, "sustain": -18.0, "sensitivity": 50.0, "punch": 25.0, "release": 95.0, "mix": 75.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 55.0, "lpf_freq": 11000.0, "slope": 12, "focus_q": 0.7},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.2, "drive": 22.0, "stereo_link": 100.0},
            },
            "Dry Vintage 70s Funk Drums (复古70年代干瘪放克鼓)": {
                "enabled": True,
                "output_gain": 1.0,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 18.0, "sustain": -82.0, "sensitivity": 64.0, "punch": 20.0, "release": 35.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 80.0, "lpf_freq": 9000.0, "slope": 12, "focus_q": 0.8},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.5, "drive": 42.0, "stereo_link": 100.0},
            },
            "Parallel Drum Crush (鼓组并行暴力瞬态激励)": {
                "enabled": True,
                "output_gain": -2.0,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 85.0, "sustain": 55.0, "sensitivity": 72.0, "punch": 75.0, "release": 130.0, "mix": 42.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 70.0, "lpf_freq": 10000.0, "slope": 12, "focus_q": 0.9},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 48.0, "stereo_link": 100.0},
            },

            # ================= 3. 贝斯与弹拨乐器 (Bass / Guitar / Piano) =================
            "Bass Pick & Finger Articulation (贝斯拨片与指弹清晰度)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": True,
                "transient": {"enabled": True, "mode": "Punch", "attack": 38.0, "sustain": 12.0, "sensitivity": 56.0, "punch": 32.0, "release": 110.0, "mix": 90.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 180.0, "lpf_freq": 5500.0, "slope": 12, "focus_q": 1.0},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 24.0, "stereo_link": 100.0},
            },
            "Slap Bass Pop & Tightness (Slap贝斯勾弦炸裂收紧)": {
                "enabled": True,
                "output_gain": -0.5,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 48.0, "sustain": -34.0, "sensitivity": 65.0, "punch": 40.0, "release": 65.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 120.0, "lpf_freq": 8000.0, "slope": 12, "focus_q": 0.9},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 20.0, "stereo_link": 100.0},
            },
            "Acoustic Guitar Pick Attack (原声吉他拨片颗粒)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Smooth", "attack": 36.0, "sustain": 18.0, "sensitivity": 48.0, "punch": 20.0, "release": 130.0, "mix": 80.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 140.0, "lpf_freq": 14000.0, "slope": 12, "focus_q": 0.7},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.5, "drive": 12.0, "stereo_link": 100.0},
            },
            "Tame Harsh Pluck / Slap (削弱刺耳拨弦瞬态)": {
                "enabled": True,
                "output_gain": 1.0,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": -58.0, "sustain": 24.0, "sensitivity": 62.0, "punch": 0.0, "release": 110.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 400.0, "lpf_freq": 10000.0, "slope": 12, "focus_q": 0.9},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 8.0, "stereo_link": 100.0},
            },
            "Electric Guitar Palm Mute Chug (电吉他闷音节奏颗粒)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 42.0, "sustain": -40.0, "sensitivity": 60.0, "punch": 35.0, "release": 60.0, "mix": 95.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 110.0, "lpf_freq": 4800.0, "slope": 12, "focus_q": 1.0},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 18.0, "stereo_link": 100.0},
            },
            "Piano Hammer & Sustain Richness (钢琴琴槌力度与泛音延展)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": True,
                "transient": {"enabled": True, "mode": "Smooth", "attack": 26.0, "sustain": 34.0, "sensitivity": 46.0, "punch": 15.0, "release": 260.0, "mix": 75.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 90.0, "lpf_freq": 12000.0, "slope": 12, "focus_q": 0.7},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.5, "drive": 10.0, "stereo_link": 90.0},
            },

            # ================= 4. 人声、配音与影视音效修复 (Vocal / SFX / Restoration) =================
            "Vocal Consonant Clarity (人声辅音咬字清晰增强)": {
                "enabled": True,
                "output_gain": 0.0,
                "auto_gain": True,
                "transient": {"enabled": True, "mode": "Smooth", "attack": 25.0, "sustain": -12.0, "sensitivity": 54.0, "punch": 18.0, "release": 75.0, "mix": 70.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 1200.0, "lpf_freq": 8500.0, "slope": 12, "focus_q": 0.8},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.5, "drive": 8.0, "stereo_link": 100.0},
            },
            "De-Reverb / Room Tail Cleaner (去混响与同期声房间残响消除)": {
                "enabled": True,
                "output_gain": 1.5,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Smooth", "attack": 12.0, "sustain": -72.0, "sensitivity": 60.0, "punch": 10.0, "release": 90.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 180.0, "lpf_freq": 9000.0, "slope": 12, "focus_q": 0.7},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.3, "drive": 6.0, "stereo_link": 100.0},
            },
            "SFX Impact & Foley Sharpener (拟音与动作撞击音效锐化)": {
                "enabled": True,
                "output_gain": -0.5,
                "auto_gain": False,
                "transient": {"enabled": True, "mode": "Punch", "attack": 68.0, "sustain": -24.0, "sensitivity": 65.0, "punch": 58.0, "release": 80.0, "mix": 100.0},
                "sidechain": {"enabled": True, "listen": False, "hpf_freq": 60.0, "lpf_freq": 14000.0, "slope": 12, "focus_q": 0.8},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.2, "drive": 28.0, "stereo_link": 100.0},
            },
            "Synth Pad Swell & Soft Entry (合成器铺底柔化起振)": {
                "enabled": True,
                "output_gain": 0.5,
                "auto_gain": True,
                "transient": {"enabled": True, "mode": "Smooth", "attack": -75.0, "sustain": 52.0, "sensitivity": 45.0, "punch": 0.0, "release": 320.0, "mix": 100.0},
                "sidechain": {"enabled": False, "listen": False, "hpf_freq": 80.0, "lpf_freq": 16000.0, "slope": 12, "focus_q": 0.7},
                "output_stage": {"enabled": True, "clip_mode": "Soft", "ceiling": -0.5, "drive": 16.0, "stereo_link": 100.0},
            },
            "Clean Neutral (直通初始化)": copy.deepcopy(self._banks["B"]),
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 演示鼓组敲击包络模拟器（接入真实音频引擎后自动停止）
        self._sim_phase = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_envelope)
        self._demo_timer.start(35)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ProTransientVisualizerWidget, ProVerticalMeter, MasterOutputStrip,
            SidechainMiniResponseWidget {{
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
        # 1. 顶部主控工具栏
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

        title_lbl = QLabel(f"TRANSIENT DESIGNER  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(12)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存或覆盖瞬态预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        del_preset_btn = QPushButton("−")
        del_preset_btn.setObjectName("ToolBtn")
        del_preset_btn.setFixedSize(22, 22)
        del_preset_btn.setToolTip("删除当前自定义预设")
        del_preset_btn.clicked.connect(self._delete_current_preset)
        tb_lay.addWidget(del_preset_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedWidth(235)
        self._builtin_preset_names = set(self._presets.keys())
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

        tb_lay.addStretch()

        # Auto Gain 自动响度匹配开关
        self.auto_gain_btn = QPushButton("Auto Gain")
        self.auto_gain_btn.setCheckable(True)
        self.auto_gain_btn.setFixedSize(72, 22)
        self.auto_gain_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_gain_btn.setToolTip("自动响度补偿（防止加大 Attack/Sustain 造成主观音量欺骗）")
        self.auto_gain_btn.setStyleSheet("""
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
        self.auto_gain_btn.toggled.connect(self._on_auto_gain_toggled)
        tb_lay.addWidget(self.auto_gain_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_designer)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：IN | 瞬态包络与增益轨迹画布 | ATT/SUS 双向增益表 | GAIN推子 | OUT
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

        # 2.2 中央瞬态波形包络与 2D 锚点画布
        self.visualizer = ProTransientVisualizerWidget()
        self.visualizer.transientDragged.connect(self._on_canvas_transient_dragged)
        self.visualizer.sensitivityScrolled.connect(self._on_canvas_sens_scrolled)
        self.visualizer.transientResetRequested.connect(self._on_canvas_transient_reset)
        up_lay.addWidget(self.visualizer, 5)
        up_lay.addWidget(_vsep())

        # 2.3 双通道瞬态动态增益表 (ATT 起振增减 / SUS 延音增减)
        gr_col = self._build_meter_column("GAIN Δ dB")
        gr_row = QHBoxLayout()
        gr_row.setSpacing(2)
        self.meter_att = ProVerticalMeter(title="ATT", mode="bipolar_gr", show_ticks=True)
        self.meter_sus = ProVerticalMeter(title="SUS", mode="bipolar_gr", show_ticks=False)
        gr_row.addWidget(self.meter_att)
        gr_row.addWidget(self.meter_sus)
        gr_col.addLayout(gr_row, 1)
        up_lay.addLayout(gr_col)
        up_lay.addWidget(_vsep())

        # 2.4 Output 推子 + 电平表
        fader_col = self._build_meter_column("OUT GAIN")
        self.out_fader = MasterOutputStrip()
        self.out_fader.gainChanged.connect(self._on_output_gain_changed)
        fader_col.addWidget(self.out_fader, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(fader_col)
        up_lay.addWidget(_vsep())

        out_col = self._build_meter_column("OUT")
        self.meter_output = ProVerticalMeter(mode="level", show_ticks=True)
        out_col.addWidget(self.meter_output, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部三模块控制矩阵 (Transient Shaper | Sidechain Filter | Output & Clip)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        st = self._current_state()
        self.transient_card = TransientShaperCard(st["transient"])
        self.transient_card.changed.connect(self._on_transient_changed)

        self.sidechain_card = SidechainFilterCard(st["sidechain"])
        self.sidechain_card.changed.connect(self._on_sidechain_changed)

        self.output_card = OutputLimiterCard(st["output_stage"])
        self.output_card.changed.connect(self._on_output_stage_changed)

        cards_row.addWidget(self.transient_card, 4)
        cards_row.addWidget(self.sidechain_card, 3)
        cards_row.addWidget(self.output_card, 3)

        main_lay.addLayout(cards_row, 4)

    def _build_meter_column(self, header_text: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(3, 5, 3, 4)
        col.setSpacing(3)
        lbl = QLabel(header_text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #9A9AA2; font-size: 9px; font-weight: 700; letter-spacing: 0.7px;")
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
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 700;
            }
        """
        inactive_css = """
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
        self.btn_a.setStyleSheet(active_css if self._active_bank == "A" else inactive_css)
        self.btn_b.setStyleSheet(active_css if self._active_bank == "B" else inactive_css)
        self.btn_copy_ab.setStyleSheet(inactive_css)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

    def _refresh_all_views(self):
        st = self._current_state()
        self._update_ab_styles()
        self.power_switch.setChecked(st["enabled"])
        self.out_fader.setGain(st["output_gain"])

        self.auto_gain_btn.blockSignals(True)
        self.auto_gain_btn.setChecked(bool(st.get("auto_gain", False)))
        self.auto_gain_btn.blockSignals(False)

        self.transient_card.syncFromData(st["transient"])
        self.sidechain_card.syncFromData(st["sidechain"])
        self.output_card.syncFromData(st["output_stage"])

        tr_active = st["enabled"] and st["transient"]["enabled"]
        self.meter_att.setActive(tr_active)
        self.meter_sus.setActive(tr_active)
        self.visualizer.setState(st)

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

    def _on_auto_gain_toggled(self, checked: bool):
        st = self._current_state()
        st["auto_gain"] = checked
        self.autoGainToggled.emit(checked)
        self.stateChanged.emit(self.get_state())

    def _on_transient_changed(self, data: dict):
        st = self._current_state()
        st["transient"] = data
        tr_active = st["enabled"] and data["enabled"]
        self.meter_att.setActive(tr_active)
        self.meter_sus.setActive(tr_active)
        self.visualizer.setState(st)
        self.transientChanged.emit(copy.deepcopy(data))
        self.stateChanged.emit(self.get_state())

    def _on_sidechain_changed(self, data: dict):
        st = self._current_state()
        st["sidechain"] = data
        self.visualizer.setState(st)
        self.sidechainChanged.emit(copy.deepcopy(data))
        self.stateChanged.emit(self.get_state())

    def _on_output_stage_changed(self, data: dict):
        st = self._current_state()
        st["output_stage"] = data
        self.outputStageChanged.emit(copy.deepcopy(data))
        self.stateChanged.emit(self.get_state())

    def _on_output_gain_changed(self, gain: float):
        st = self._current_state()
        st["output_gain"] = gain
        self.outputGainChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _on_canvas_transient_dragged(self, sustain_pct: float, attack_pct: float, s_changed: bool, a_changed: bool):
        st = self._current_state()
        st["transient"]["enabled"] = True
        if s_changed:
            st["transient"]["sustain"] = sustain_pct
        if a_changed:
            st["transient"]["attack"] = attack_pct

        self.transient_card.syncFromData(st["transient"])
        self.meter_att.setActive(st["enabled"])
        self.meter_sus.setActive(st["enabled"])
        self.visualizer.setState(st)
        self.transientChanged.emit(copy.deepcopy(st["transient"]))
        self.stateChanged.emit(self.get_state())

    def _on_canvas_sens_scrolled(self, sens_pct: float):
        st = self._current_state()
        st["transient"]["sensitivity"] = sens_pct
        self.transient_card.syncFromData(st["transient"])
        self.visualizer.setState(st)
        self.transientChanged.emit(copy.deepcopy(st["transient"]))
        self.stateChanged.emit(self.get_state())

    def _on_canvas_transient_reset(self):
        st = self._current_state()
        st["transient"]["attack"] = 0.0
        st["transient"]["sustain"] = 0.0
        self.transient_card.syncFromData(st["transient"])
        self.visualizer.setState(st)
        self.transientChanged.emit(copy.deepcopy(st["transient"]))
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
        name, ok = QInputDialog.getText(self, "保存 Transient Designer 预设", "输入预设名称:", text=default_name)
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
        self.preset_combo.removeItem(self.preset_combo.currentIndex())

    # -----------------------------------------------------------------
    # 演示鼓组瞬态包络模拟（接入真实音频引擎后自动停止）
    # -----------------------------------------------------------------
    def _tick_demo_envelope(self):
        self._sim_phase = (self._sim_phase + 0.045) % 1.0
        st = self._current_state()
        tr = st["transient"]

        # 模拟周期性鼓组敲击包络 (0.0~0.14 为快速起振阶段，0.14~0.85 为自然腔体延音衰减阶段)
        t = self._sim_phase
        if t < 0.04:
            in_amp = 0.04 + (t / 0.04) * 0.72
            is_attack_phase = True
        elif t < 0.14:
            in_amp = 0.76 * math.exp(-(t - 0.04) * 9.0) + 0.28
            is_attack_phase = True
        else:
            in_amp = 0.36 * math.exp(-(t - 0.14) * 4.2) + 0.02
            is_attack_phase = False

        if st["enabled"] and tr["enabled"]:
            sens_scale = 0.4 + 0.6 * (tr["sensitivity"] / 100.0)
            mix_ratio = tr["mix"] / 100.0

            if is_attack_phase:
                att_db = (tr["attack"] / 100.0) * 12.0 * sens_scale * math.sin(min(1.0, t / 0.14) * math.pi)
                sus_db = 0.0
                gain_delta_db = att_db * mix_ratio
            else:
                att_db = 0.0
                decay_weight = min(1.0, (t - 0.14) / 0.25) * math.exp(-(t - 0.14) * 2.2)
                sus_db = (tr["sustain"] / 100.0) * 14.0 * sens_scale * decay_weight
                gain_delta_db = sus_db * mix_ratio
        else:
            att_db = 0.0
            sus_db = 0.0
            gain_delta_db = 0.0

        gain_lin = 10.0 ** ((gain_delta_db + st["output_gain"]) / 20.0)
        out_amp = min(0.98, in_amp * gain_lin)

        # 软削波限幅保护模拟
        if st["enabled"] and st["output_stage"]["enabled"]:
            ceil_lin = 10.0 ** (st["output_stage"]["ceiling"] / 20.0)
            if out_amp > ceil_lin:
                out_amp = ceil_lin

        self.visualizer.pushEnvelopeFrame(in_amp, out_amp, gain_delta_db, att_db, sus_db)

        in_db = 20.0 * math.log10(max(1e-3, in_amp))
        out_db = 20.0 * math.log10(max(1e-3, out_amp))
        self.meter_input.setValue(in_db)
        self.meter_output.setValue(out_db)
        self.meter_att.setValue(att_db)
        self.meter_sus.setValue(sus_db)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_designer(self):
        self._banks[self._active_bank] = default_transient_neutral()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def push_realtime_frame(
        self,
        input_db: float,
        output_db: float,
        attack_gain_db: float,
        sustain_gain_db: float,
    ):
        """
        接入真实音频引擎实时遥测数据 (每帧 30~60Hz 调用，调用后自动停止内置演示动画)：
          - input_db: 输入峰值电平 (-55.0 ~ 0.0 dBFS)
          - output_db: 输出峰值电平 (-55.0 ~ 0.0 dBFS)
          - attack_gain_db: 当前起振阶段动态增益变化 (-15.0 ~ +15.0 dB)
          - sustain_gain_db: 当前延音阶段动态增益变化 (-15.0 ~ +15.0 dB)
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()

        in_amp = 10.0 ** (max(-55.0, min(0.0, input_db)) / 20.0)
        out_amp = 10.0 ** (max(-55.0, min(0.0, output_db)) / 20.0)
        total_delta = attack_gain_db + sustain_gain_db

        self.visualizer.pushEnvelopeFrame(in_amp, out_amp, total_delta, attack_gain_db, sustain_gain_db)
        self.meter_input.setValue(input_db)
        self.meter_output.setValue(output_db)
        self.meter_att.setValue(attack_gain_db)
        self.meter_sus.setValue(sustain_gain_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioTransientDesignerWidget("Snare Top")
    win.show()
    sys.exit(app.exec())