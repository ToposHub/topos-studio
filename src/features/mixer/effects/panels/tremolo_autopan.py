from __future__ import annotations

import sys
import math
import copy
import time
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QFrame, QLineEdit, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QLinearGradient, QRadialGradient,
    QPainterPath, QFont
)


# =====================================================================
# 1. 全局色彩系统与默认参数配置（与 Dynamics-UI / Panner-UI 完全统一）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

PALETTE = {
    "cyan":   "#28D8F0",  # 主控 / Auto-Pan 轨迹 / A-B 激活
    "green":  "#4CD490",  # 左声道 (L) / Rate 频率模块
    "amber":  "#F0A848",  # 右声道 (R) / Shape 波形模块
    "violet": "#B67CF8",  # Stereo Phase 相位偏移 / Dual 模式
    "coral":  "#F26D6D",  # Depth 调制深度 / Output 电平
    "blue":   "#6495FA",  # BPM Sync / Smooth 平滑
}

SYNC_DIVISIONS = [
    ("1/1",   0.25),
    ("1/2D",  1.0 / 3.0),
    ("1/2",   0.5),
    ("1/2T",  0.75),
    ("1/4D",  2.0 / 3.0),
    ("1/4",   1.0),
    ("1/4T",  1.5),
    ("1/8D",  4.0 / 3.0),
    ("1/8",   2.0),
    ("1/8T",  3.0),
    ("1/16D", 8.0 / 3.0),
    ("1/16",  4.0),
    ("1/16T", 6.0),
    ("1/32",  8.0),
]
SYNC_MULT_MAP = dict(SYNC_DIVISIONS)

WAVEFORMS = ["Sine", "Triangle", "Square", "Saw Up", "Saw Down", "Random"]


def default_tremolo_autopan_state() -> dict:
    return {
        "enabled": True,
        "mode": "Auto-Pan",       # "Tremolo" | "Auto-Pan" | "Dual"
        "sync": False,            # False = Hz 自由速率 | True = BPM 节拍同步
        "bpm": 120.0,             # 40.0 ~ 240.0 BPM
        "rate_hz": 2.40,          # 0.05 ~ 30.0 Hz
        "sync_div": "1/8",        # SYNC_DIVISIONS key
        "waveform": "Sine",       # "Sine" | "Triangle" | "Square" | "Saw Up" | "Saw Down" | "Random"
        "shape": 50.0,            # 0.0 ~ 100.0 % (50% = 标准对称，偏左/右改变占空比/斜率)
        "smooth": 15.0,           # 0.0 ~ 100.0 % (方波/锯齿边沿抗锯齿平滑度)
        "depth": 80.0,            # 0.0 ~ 100.0 % (调制深度)
        "phase_offset": 180.0,    # 0.0 ~ 360.0 deg (立体声左右声道 LFO 相位差)
        "pan_center": 0.0,        # -100.0 (100L) ~ +100.0 (100R) 声像中心偏移
        "pan_width": 100.0,       # 0.0 ~ 100.0 % 摆动幅度宽度
        "output_gain": 0.0,       # -12.0 ~ +12.0 dB 补偿输出增益
        "retrigger": True,        # 播放头重触发相位对齐
    }


def format_hz(val: float) -> str:
    if val < 1.0:
        return f"{val:.2f}Hz"
    if val < 10.0:
        return f"{val:.2f}Hz"
    return f"{val:.1f}Hz"


def format_lr(val: float) -> str:
    v = int(round(val))
    if v == 0:
        return "C"
    return f"{abs(v)}L" if v < 0 else f"{v}R"


def parse_lr(text: str, fallback: float) -> float:
    s = text.strip().upper()
    if s in ("C", "CENTER", "0"):
        return 0.0
    try:
        if s.endswith("L"):
            return -max(0.0, min(100.0, float(s[:-1])))
        if s.endswith("R"):
            return max(0.0, min(100.0, float(s[:-1])))
        return max(-100.0, min(100.0, float(s)))
    except ValueError:
        return fallback


# =====================================================================
# 2. LFO 核心波形数学函数（供 UI 实时示波器 & 轨迹预览使用，亦可直接复用于后端）
# =====================================================================
def evaluate_lfo_unipolar(phase_0_1: float, waveform: str, shape: float = 50.0, smooth: float = 0.0) -> float:
    """
    计算归一化 LFO 振幅 [0.0 ~ 1.0]
    :param phase_0_1: 周期相位 [0.0, 1.0)
    :param waveform: 波形类型
    :param shape: 对称/占空比塑形 [0.0 ~ 100.0], 50.0 为标准对称
    :param smooth: 边沿平滑度 [0.0 ~ 100.0]
    """
    p = phase_0_1 % 1.0
    skew = max(0.05, min(0.95, shape / 100.0))

    if waveform == "Sine":
        # 通过相位扭曲实现正弦波不对称塑形
        if p < skew:
            warped = 0.5 * (p / skew)
        else:
            warped = 0.5 + 0.5 * ((p - skew) / (1.0 - skew))
        val = 0.5 - 0.5 * math.cos(2.0 * math.pi * warped)

    elif waveform == "Triangle":
        if p < skew:
            val = p / skew
        else:
            val = 1.0 - (p - skew) / (1.0 - skew)

    elif waveform == "Square":
        edge = max(0.002, (smooth / 100.0) * 0.22)
        if p < skew - edge * 0.5:
            val = 1.0
        elif p < skew + edge * 0.5:
            t = (p - (skew - edge * 0.5)) / edge
            val = 1.0 - (0.5 - 0.5 * math.cos(math.pi * t))
        elif p > 1.0 - edge * 0.5:
            t = (p - (1.0 - edge * 0.5)) / edge
            val = 0.5 - 0.5 * math.cos(math.pi * t)
        else:
            val = 0.0

    elif waveform == "Saw Up":
        # 占空比控制上升沿比例，末尾回落受 smooth 柔化
        fall = max(0.005, (smooth / 100.0) * 0.25)
        up_end = max(0.05, min(1.0 - fall, 0.5 + skew * 0.5))
        if p < up_end:
            val = (p / up_end) ** (1.35 - skew * 0.7)
        else:
            t = (p - up_end) / max(1e-4, 1.0 - up_end)
            val = 1.0 - (0.5 - 0.5 * math.cos(math.pi * t))

    elif waveform == "Saw Down":
        rise = max(0.005, (smooth / 100.0) * 0.25)
        if p < rise:
            t = p / rise
            val = 0.5 - 0.5 * math.cos(math.pi * t)
        else:
            t = (p - rise) / max(1e-4, 1.0 - rise)
            val = (1.0 - t) ** (0.65 + skew * 0.7)

    else:  # Random (Sample & Hold 伪随机确定性阶梯)
        steps = 8
        idx = int(p * steps)
        frac = (p * steps) - idx
        # 确定性哈希避免重绘闪烁
        h_curr = (math.sin(idx * 12.9898 + 78.233) * 43758.5453) % 1.0
        h_next = (math.sin(((idx + 1) % steps) * 12.9898 + 78.233) * 43758.5453) % 1.0
        blend_w = max(0.01, smooth / 100.0)
        if frac > 1.0 - blend_w:
            t = (frac - (1.0 - blend_w)) / blend_w
            s = 0.5 - 0.5 * math.cos(math.pi * t)
            val = h_curr * (1.0 - s) + h_next * s
        else:
            val = h_curr

    return max(0.0, min(1.0, val))


# =====================================================================
# 3. 基础控件：现代发光电源开关（复用 Dynamics-UI 风格）
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
# 4. 基础控件：内嵌可编辑数字胶囊（复用 Dynamics-UI / Panner-UI 风格）
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
                self._text.replace("Hz", "")
                .replace("dB", "")
                .replace("%", "")
                .replace("°", "")
                .replace("BPM", "")
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
# 5. 基础控件：拉丝阳极铝质感旋钮（支持 linear / log / bipolar / circular）
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

        self.setFixedSize(38, 38)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setValue(self, val: float, emit: bool = False):
        if self._mode == "circular":
            clamped = val % (self._max - self._min)
        else:
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
            v = max(1e-4, val)
            mn = max(1e-4, self._min)
            mx = max(mn * 1.001, self._max)
            return math.log(v / mn) / math.log(mx / mn)
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        if self._mode == "circular":
            n = norm % 1.0
        else:
            n = max(0.0, min(1.0, norm))
        if self._mode == "log":
            mn = max(1e-4, self._min)
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

        arc_r = 15.6
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深色凹槽底环
        p.setPen(QPen(QColor("#141417"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 2. 模块主题色发光弧
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

        # 3. 哑光浅灰旋钮顶盖
        knob_r = 11.6
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 4. 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.4
        iy1 = cy - math.sin(rad) * 3.4
        ix2 = cx + math.cos(rad) * (knob_r - 1.4)
        iy2 = cy - math.sin(rad) * (knob_r - 1.4)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 6. 组合旋钮参数单元（标签 + 旋钮 + 上下限提示 + 可编辑胶囊）
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
        lay.setSpacing(3)

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
# 7. 控件：双声道实时调制电平/声像动态指示条
# =====================================================================
class StereoModulationMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(54)
        self.setMinimumHeight(180)
        self._l_val = 1.0   # 0.0 ~ 1.0 (gain or pan weight)
        self._r_val = 1.0
        self._mode = "Auto-Pan"
        self._active = True

    def setModulationValues(self, l_val: float, r_val: float, mode: str, active: bool):
        self._l_val = max(0.0, min(1.0, l_val))
        self._r_val = max(0.0, min(1.0, r_val))
        self._mode = mode
        self._active = active
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 12.0
        bot_pad = 22.0
        h = max(40.0, self.height() - top_pad - bot_pad)
        bar_w = 11.0
        gap = 8.0
        x_l = (self.width() - (bar_w * 2 + gap)) / 2.0
        x_r = x_l + bar_w + gap

        rect_l = QRectF(x_l, top_pad, bar_w, h)
        rect_r = QRectF(x_r, top_pad, bar_w, h)

        # 槽底
        for r in (rect_l, rect_r):
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor("#0E0E11"))
            p.drawRoundedRect(r, 2.0, 2.0)

        if self._active:
            for r, val, col_hex in (
                (rect_l, self._l_val, PALETTE["green"]),
                (rect_r, self._r_val, PALETTE["amber"]),
            ):
                fh = val * h
                if fh > 1.0:
                    fill_r = QRectF(r.left() + 1.0, r.bottom() - fh, r.width() - 2.0, fh - 0.5)
                    c = QColor(col_hex)
                    grad = QLinearGradient(r.bottomLeft(), r.topLeft())
                    grad.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 110))
                    grad.setColorAt(1.0, c)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(grad)
                    p.drawRoundedRect(fill_r, 1.2, 1.2)

        # 栅格线
        p.setPen(QPen(QColor(13, 14, 18, 175), 1.0))
        gy = top_pad + 3.0
        while gy < top_pad + h:
            p.drawLine(QPointF(x_l, gy), QPointF(x_r + bar_w, gy))
            gy += 3.0

        # 底部 L / R 标识
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor(PALETTE["green"]) if self._active else QColor("#55555C"))
        p.drawText(QRectF(x_l - 3, self.height() - 18, bar_w + 6, 15), Qt.AlignmentFlag.AlignCenter, "L")
        p.setPen(QColor(PALETTE["amber"]) if self._active else QColor("#55555C"))
        p.drawText(QRectF(x_r - 3, self.height() - 18, bar_w + 6, 15), Qt.AlignmentFlag.AlignCenter, "R")


# =====================================================================
# 8. 核心视觉舞台：双通道 LFO 示波器 + 实时声像摆动轨道 (Dual Scope & Pan Stage)
# =====================================================================
class LfoModulationVisualizer(QWidget):
    depthDragged = pyqtSignal(float)
    phaseOffsetDragged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(460, 225)
        self.setMouseTracking(True)

        self._state = default_tremolo_autopan_state()
        self._phase = 0.0  # [0.0, 1.0) 当前 LFO 播放相位
        self._dragging_mode = ""  # "depth" | "phase" | ""
        self._drag_start = QPointF()
        self._drag_start_val = 0.0

    def setState(self, state: dict):
        self._state = copy.deepcopy(state)
        self.update()

    def setPhase(self, phase_0_1: float):
        self._phase = phase_0_1 % 1.0
        self.update()

    def _scope_rect(self) -> QRectF:
        # 左侧 68% 为双通道 LFO 示波器，右侧 32% 为实时声像/振幅雷达舞台
        total_w = self.width()
        stage_w = max(175.0, min(220.0, total_w * 0.30))
        scope_w = max(220.0, total_w - stage_w - 38.0)
        return QRectF(14.0, 12.0, scope_w, max(120.0, self.height() - 24.0))

    def _pan_stage_rect(self) -> QRectF:
        sc = self._scope_rect()
        return QRectF(sc.right() + 12.0, 12.0, max(150.0, self.width() - sc.right() - 26.0), sc.height())

    # -----------------------------------------------------------------
    # 交互：在示波器上垂直拖拽调节 Depth，水平拖拽调节 Stereo Phase Offset
    # -----------------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            sc = self._scope_rect()
            if sc.contains(event.position()):
                self._drag_start = event.position()
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._dragging_mode = "phase"
                    self._drag_start_val = self._state["phase_offset"]
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                else:
                    self._dragging_mode = "depth"
                    self._drag_start_val = self._state["depth"]
                    self.setCursor(Qt.CursorShape.SizeVerCursor)

    def mouseMoveEvent(self, event):
        if self._dragging_mode == "depth":
            dy = self._drag_start.y() - event.position().y()
            new_d = max(0.0, min(100.0, round(self._drag_start_val + dy * 0.45, 1)))
            self.depthDragged.emit(new_d)
        elif self._dragging_mode == "phase":
            dx = event.position().x() - self._drag_start.x()
            new_p = round((self._drag_start_val + dx * 1.2) % 360.0, 1)
            self.phaseOffsetDragged.emit(new_p)
        else:
            sc = self._scope_rect()
            if sc.contains(event.position()):
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_mode:
            self._dragging_mode = ""
            self.setCursor(Qt.CursorShape.CrossCursor)

    # -----------------------------------------------------------------
    # 绘制：左侧双通道 LFO 示波器 + 右侧立体声摆动声场舞台
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        sc = self._scope_rect()
        st_rect = self._pan_stage_rect()
        st = self._state
        en = st["enabled"]
        mode = st["mode"]

        # -------------------------------------------------------------
        # A. 左侧：L/R 双通道 LFO 调制示波器
        # -------------------------------------------------------------
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#1D1D20"))
        p.drawRoundedRect(sc, 3.0, 3.0)

        # 网格线 (2 完整周期, 8 拍细分)
        p.setPen(QPen(QColor("#26262B"), 1.0))
        for i in range(1, 8):
            gx = sc.left() + (i / 8.0) * sc.width()
            p.drawLine(QPointF(gx, sc.top()), QPointF(gx, sc.bottom()))
        for j in range(1, 4):
            gy = sc.top() + (j / 4.0) * sc.height()
            p.drawLine(QPointF(sc.left(), gy), QPointF(sc.right(), gy))

        # 中心基准线 (0.5 / Center)
        mid_y = sc.center().y()
        p.setPen(QPen(QColor("#383842"), 1.1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(sc.left(), mid_y), QPointF(sc.right(), mid_y))

        p.save()
        p.setClipRect(sc.adjusted(1, 1, -1, -1))

        depth = (st["depth"] / 100.0) if en else 0.0
        phase_off_norm = (st["phase_offset"] % 360.0) / 360.0
        wf = st["waveform"]
        shape = st["shape"]
        smooth = st["smooth"]

        # 绘制 2 个完整周期的 L(绿) & R(琥珀) 调制包络线
        cycles = 2.0
        steps = 240
        path_l = QPainterPath()
        path_r = QPainterPath()
        fill_l = QPainterPath()

        margin_y = 26.0
        usable_h = max(40.0, sc.height() - margin_y * 2.0)

        for i in range(steps + 1):
            t = i / steps
            ph_l = (t * cycles) % 1.0
            ph_r = (t * cycles + phase_off_norm) % 1.0

            raw_l = evaluate_lfo_unipolar(ph_l, wf, shape, smooth)
            raw_r = evaluate_lfo_unipolar(ph_r, wf, shape, smooth)

            if mode == "Tremolo":
                # Tremolo: 1.0 为 0dB 满幅，向下衰减 depth
                val_l = 1.0 - depth * (1.0 - raw_l)
                val_r = 1.0 - depth * (1.0 - raw_r)
            else:
                # Auto-Pan / Dual: 围绕 0.5 (Center) 上下摆动
                val_l = 0.5 + (raw_l - 0.5) * depth
                val_r = 0.5 + (raw_r - 0.5) * depth

            px = sc.left() + t * sc.width()
            py_l = sc.bottom() - margin_y - val_l * usable_h
            py_r = sc.bottom() - margin_y - val_r * usable_h

            if i == 0:
                path_l.moveTo(px, py_l)
                path_r.moveTo(px, py_r)
                fill_l.moveTo(px, sc.bottom() - margin_y)
                fill_l.lineTo(px, py_l)
            else:
                path_l.lineTo(px, py_l)
                path_r.lineTo(px, py_r)
                fill_l.lineTo(px, py_l)

        fill_l.lineTo(sc.right(), sc.bottom() - margin_y)
        fill_l.closeSubpath()

        if en:
            grad_l = QLinearGradient(sc.topLeft(), sc.bottomLeft())
            grad_l.setColorAt(0.0, QColor(76, 212, 144, 34))
            grad_l.setColorAt(1.0, QColor(76, 212, 144, 3))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad_l)
            p.drawPath(fill_l)

            # R 通道波形（虚线/实线区分相位偏移）
            r_col = QColor(PALETTE["amber"])
            p.setPen(QPen(QColor(r_col.red(), r_col.green(), r_col.blue(), 215), 1.7,
                          Qt.PenStyle.DashLine if phase_off_norm < 0.01 else Qt.PenStyle.SolidLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path_r)

            # L 通道主波形
            l_col = QColor(PALETTE["green"])
            p.setPen(QPen(QColor(l_col.red(), l_col.green(), l_col.blue(), 60), 4.0))
            p.drawPath(path_l)
            p.setPen(QPen(l_col, 2.0))
            p.drawPath(path_l)
        else:
            p.setPen(QPen(QColor("#787882"), 1.5))
            p.drawPath(path_l)

        # 实时扫描播放指针 (Playhead Cursor)
        play_x = sc.left() + (self._phase / cycles) * sc.width()
        p.setPen(QPen(QColor(40, 216, 240, 170) if en else QColor("#55555C"), 1.2))
        p.drawLine(QPointF(play_x, sc.top()), QPointF(play_x, sc.bottom()))

        # 当前瞬时采样点光珠
        cur_raw_l = evaluate_lfo_unipolar(self._phase, wf, shape, smooth)
        cur_raw_r = evaluate_lfo_unipolar((self._phase + phase_off_norm) % 1.0, wf, shape, smooth)
        if mode == "Tremolo":
            cur_vl = 1.0 - depth * (1.0 - cur_raw_l)
            cur_vr = 1.0 - depth * (1.0 - cur_raw_r)
        else:
            cur_vl = 0.5 + (cur_raw_l - 0.5) * depth
            cur_vr = 0.5 + (cur_raw_r - 0.5) * depth

        dot_ly = sc.bottom() - margin_y - cur_vl * usable_h
        dot_ry = sc.bottom() - margin_y - cur_vr * usable_h
        if en:
            p.setPen(QPen(QColor("#111318"), 1.2))
            p.setBrush(QColor(PALETTE["amber"]))
            p.drawEllipse(QPointF(play_x, dot_ry), 4.2, 4.2)
            p.setBrush(QColor(PALETTE["green"]))
            p.drawEllipse(QPointF(play_x, dot_ly), 4.5, 4.5)

        p.restore()

        # 左上角 HUD 信息胶囊
        rate_str = f"{st['sync_div']} ({st['bpm']:.0f} BPM)" if st["sync"] else format_hz(st["rate_hz"])
        hud_text = f"{mode.upper()}  •  {wf}  •  {rate_str}  •  ΔΦ {int(round(st['phase_offset']))}°"
        hud_rect = QRectF(sc.left() + 8, sc.top() + 6, min(sc.width() - 16, 310), 19)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 220))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor(PALETTE["cyan"]) if en else QColor("#8A8A92"))
        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_text)

        # 底部操作提示
        p.setPen(QColor("#787882"))
        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.drawText(
            QRectF(sc.left() + 8, sc.bottom() - 18, sc.width() - 16, 15),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "拖拽: 深度 Depth  •  Shift+拖拽: 相位偏移 Phase",
        )

        # -------------------------------------------------------------
        # B. 右侧：实时声像摆动 & 振幅能量雷达舞台 (Panner-UI 基因延续)
        # -------------------------------------------------------------
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#1D1D20"))
        p.drawRoundedRect(st_rect, 3.0, 3.0)

        cx = st_rect.center().x()
        cy = st_rect.center().y() + 4.0
        radius = min(st_rect.width(), st_rect.height()) * 0.37

        # 声场弧形刻度环 (半圆舞台 L - C - R)
        arc_box = QRectF(cx - radius, cy - radius * 0.85, radius * 2.0, radius * 1.7)
        p.setPen(QPen(QColor("#2E2E36"), 1.2))
        p.drawArc(arc_box, 0 * 16, 180 * 16)
        p.setPen(QPen(QColor("#26262C"), 1.0, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(cx, cy - radius * 0.85), QPointF(cx, cy + radius * 0.55))
        p.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))

        # L / C / R 标记
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor("#9A9AA2"))
        p.drawText(QRectF(cx - radius - 14, cy - 8, 16, 16), Qt.AlignmentFlag.AlignCenter, "L")
        p.drawText(QRectF(cx + radius - 2, cy - 8, 16, 16), Qt.AlignmentFlag.AlignCenter, "R")
        p.drawText(QRectF(cx - 12, cy - radius * 0.85 - 16, 24, 14), Qt.AlignmentFlag.AlignCenter, "C")

        # 计算当前瞬时声像位置 pan_l, pan_r [-1.0 .. +1.0] 与音量缩放 gain_l, gain_r [0.0 .. 1.0]
        center_norm = st["pan_center"] / 100.0
        width_norm = st["pan_width"] / 100.0
        bipolar_l = (cur_raw_l * 2.0 - 1.0) * depth * width_norm
        bipolar_r = (cur_raw_r * 2.0 - 1.0) * depth * width_norm

        if mode == "Tremolo":
            pan_l = max(-1.0, min(1.0, center_norm - 0.35 * width_norm))
            pan_r = max(-1.0, min(1.0, center_norm + 0.35 * width_norm))
            gain_l = 1.0 - depth * (1.0 - cur_raw_l)
            gain_r = 1.0 - depth * (1.0 - cur_raw_r)
        elif mode == "Auto-Pan":
            pan_l = max(-1.0, min(1.0, center_norm + bipolar_l))
            pan_r = max(-1.0, min(1.0, center_norm + bipolar_r))
            gain_l = 0.85
            gain_r = 0.85
        else:  # Dual
            pan_l = max(-1.0, min(1.0, center_norm + bipolar_l))
            pan_r = max(-1.0, min(1.0, center_norm + bipolar_r))
            gain_l = 1.0 - (depth * 0.75) * (1.0 - cur_raw_l)
            gain_r = 1.0 - (depth * 0.75) * (1.0 - cur_raw_r)

        def _pan_to_pt(pan_norm: float, r_scale: float = 1.0) -> QPointF:
            ang = math.radians(90.0 - pan_norm * 82.0)
            rr = radius * 0.78 * r_scale
            return QPointF(cx + math.cos(ang) * rr, cy - math.sin(ang) * rr * 0.85 + radius * 0.22)

        pt_l = _pan_to_pt(pan_l, 0.72 + 0.28 * gain_l)
        pt_r = _pan_to_pt(pan_r, 0.72 + 0.28 * gain_r)
        pt_mid = QPointF((pt_l.x() + pt_r.x()) * 0.5, (pt_l.y() + pt_r.y()) * 0.5)

        # 绘制摆动轨迹高亮弧
        if en and depth > 0.01:
            span_deg = depth * width_norm * 82.0
            c_deg = 90.0 - center_norm * 82.0
            p.setPen(QPen(QColor(40, 216, 240, 95), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(
                QRectF(cx - radius * 0.78, cy - radius * 0.66 + radius * 0.22, radius * 1.56, radius * 1.32),
                int((c_deg - span_deg) * 16),
                int((span_deg * 2.0) * 16),
            )

        # 声场耦合连线与光晕
        if en:
            halo_r = 14.0 + 18.0 * ((gain_l + gain_r) * 0.5)
            rg = QRadialGradient(pt_mid, halo_r)
            rg.setColorAt(0.0, QColor(40, 216, 240, 75))
            rg.setColorAt(1.0, QColor(40, 216, 240, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rg)
            p.drawEllipse(pt_mid, halo_r, halo_r)

        p.setPen(QPen(QColor(182, 124, 248, 150) if en else QColor("#55555C"), 1.4))
        p.drawLine(pt_l, pt_r)

        # L / R 声像节点（半径随 Tremolo 振幅脉动）
        rl_size = 4.0 + 3.2 * gain_l
        rr_size = 4.0 + 3.2 * gain_r
        p.setPen(QPen(QColor("#111318"), 1.2))
        p.setBrush(QColor(PALETTE["green"]) if en else QColor("#6E6E76"))
        p.drawEllipse(pt_l, rl_size, rl_size)

        p.setBrush(QColor(PALETTE["amber"]) if en else QColor("#6E6E76"))
        p.drawEllipse(pt_r, rr_size, rr_size)

        # 虚拟声像中心方锚点 (Panner-UI 风格)
        p.setBrush(QColor(PALETTE["cyan"]) if en else QColor("#8A8A92"))
        p.drawRoundedRect(QRectF(pt_mid.x() - 4.0, pt_mid.y() - 4.0, 8.0, 8.0), 1.5, 1.5)

        # 舞台标题与实时坐标读数
        p.setPen(QColor("#9A9AA2"))
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.drawText(QRectF(st_rect.left() + 8, st_rect.top() + 6, st_rect.width() - 16, 16),
                   Qt.AlignmentFlag.AlignLeft, "STEREO STAGE")

        avg_pan = ((pan_l + pan_r) * 0.5) * 100.0
        p.setPen(QColor(PALETTE["cyan"]) if en else QColor("#787882"))
        p.drawText(QRectF(st_rect.left() + 8, st_rect.bottom() - 20, st_rect.width() - 16, 16),
                   Qt.AlignmentFlag.AlignCenter, f"Pos: {format_lr(avg_pan)}  •  Center: {format_lr(st['pan_center'])}")


# =====================================================================
# 9. 底部模块卡片 1：Rate & Tempo Sync (速率与 BPM 节拍同步)
# =====================================================================
class RateSyncCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, state: dict, parent=None):
        super().__init__(parent)
        self._state = state
        self._accent = PALETTE["green"]
        self._updating = False
        self._tap_times: list[float] = []

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

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {self._accent}; font-size: 9px;")
        h_lay.addWidget(dot)

        title = QLabel("RATE & SYNC")
        title.setStyleSheet(f"color: {self._accent}; font-size: 11px; font-weight: 700; letter-spacing: 0.4px;")
        h_lay.addWidget(title)
        h_lay.addStretch()

        # Hz / Sync 分段切换
        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.btn_hz = QPushButton("Hz")
        self.btn_sync = QPushButton("Sync")
        for b in (self.btn_hz, self.btn_sync):
            b.setCheckable(True)
            b.setFixedSize(38, 20)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_lay.addWidget(b)

        self.btn_hz.clicked.connect(lambda: self._on_sync_mode(False))
        self.btn_sync.clicked.connect(lambda: self._on_sync_mode(True))
        h_lay.addWidget(seg_wrap)
        root.addWidget(header)

        # Body
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(10, 10, 10, 10)
        b_lay.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        # 自由速率旋钮
        self.c_rate = KnobParameterCell(
            "Rate (Hz)", 0.05, 30.0, 2.40, self._accent,
            "0.05", "Hz", "30.0",
            format_hz, lambda s, fb: float(s), mode="log", pill_width=64
        )
        self.c_rate.valueChanged.connect(self._on_rate_changed)
        top_row.addWidget(self.c_rate, 1)

        # 节拍时值 & BPM 控制列
        sync_col = QVBoxLayout()
        sync_col.setSpacing(6)

        div_lbl = QLabel("Note Division")
        div_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        div_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        sync_col.addWidget(div_lbl)

        self.div_combo = QComboBox()
        self.div_combo.setFixedHeight(23)
        self.div_combo.addItems([d[0] for d in SYNC_DIVISIONS])
        self.div_combo.currentTextChanged.connect(self._on_div_changed)
        sync_col.addWidget(self.div_combo)

        bpm_lbl = QLabel("Tempo (BPM)")
        bpm_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bpm_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        sync_col.addWidget(bpm_lbl)

        bpm_row = QHBoxLayout()
        bpm_row.setSpacing(4)
        self.bpm_pill = EditableValuePill("120.0", PALETTE["blue"], width=56)
        self.bpm_pill.valueEdited.connect(self._on_bpm_edited)
        bpm_row.addWidget(self.bpm_pill)

        self.btn_tap = QPushButton("TAP")
        self.btn_tap.setObjectName("ToolBtn")
        self.btn_tap.setFixedSize(36, 21)
        self.btn_tap.setToolTip("连续点击测定 BPM 速度")
        self.btn_tap.clicked.connect(self._on_tap_tempo)
        bpm_row.addWidget(self.btn_tap)
        sync_col.addLayout(bpm_row)

        sync_col.addStretch()
        top_row.addLayout(sync_col, 1)
        b_lay.addLayout(top_row)

        # 底部 Retrigger 相位重触发开关
        bot_row = QHBoxLayout()
        self.btn_retrig = QPushButton("Phase Retrigger: ON")
        self.btn_retrig.setCheckable(True)
        self.btn_retrig.setFixedHeight(22)
        self.btn_retrig.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_retrig.clicked.connect(self._on_retrig_toggled)
        bot_row.addWidget(self.btn_retrig)
        b_lay.addLayout(bot_row)

        root.addWidget(body, 1)
        self.syncFromState(state)

    def syncFromState(self, state: dict):
        self._updating = True
        self._state = state
        en = bool(state["enabled"])
        is_sync = bool(state["sync"])

        self.setStyleSheet("""
            QFrame#ModuleCard {
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
            }
            QFrame#CardBody {
                background-color: transparent;
                border-top: 1px solid #141416;
            }
        """)

        self.btn_hz.setChecked(not is_sync)
        self.btn_sync.setChecked(is_sync)

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
        self.btn_hz.setStyleSheet(active_seg if not is_sync else inactive_seg)
        self.btn_sync.setStyleSheet(active_seg if is_sync else inactive_seg)

        self.c_rate.setValue(state["rate_hz"])
        self.c_rate.setModuleActive(en and not is_sync)

        self.div_combo.setCurrentText(state["sync_div"])
        self.div_combo.setEnabled(en and is_sync)

        self.bpm_pill.setText(f"{state['bpm']:.1f}")
        self.bpm_pill.setModuleActive(en)

        retrig = bool(state["retrigger"])
        self.btn_retrig.setChecked(retrig)
        self.btn_retrig.setText("Phase Retrigger: ON" if retrig else "Phase Retrigger: FREE")
        self.btn_retrig.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238;
                color: #8A8A94;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:checked {{
                background-color: #182E38;
                color: {PALETTE['cyan'] if en else '#8A8A94'};
                border: 1px solid {PALETTE['cyan'] if en else '#3A3A40'};
            }}
        """)
        self._updating = False

    def _emit_change(self):
        if not self._updating:
            self.changed.emit(self._state)

    def _on_sync_mode(self, is_sync: bool):
        self._state["sync"] = is_sync
        self.syncFromState(self._state)
        self._emit_change()

    def _on_rate_changed(self, val: float):
        self._state["rate_hz"] = round(val, 3)
        if self._state["sync"]:
            self._state["sync"] = False
            self.syncFromState(self._state)
        self._emit_change()

    def _on_div_changed(self, div_str: str):
        if self._updating:
            return
        self._state["sync_div"] = div_str
        self._emit_change()

    def _on_bpm_edited(self, text: str):
        try:
            val = max(40.0, min(240.0, round(float(text), 1)))
            self._state["bpm"] = val
            self.bpm_pill.setText(f"{val:.1f}")
            self._emit_change()
        except ValueError:
            pass

    def _on_tap_tempo(self):
        now = time.monotonic()
        if self._tap_times and (now - self._tap_times[-1]) > 2.0:
            self._tap_times.clear()
        self._tap_times.append(now)
        if len(self._tap_times) > 4:
            self._tap_times.pop(0)
        if len(self._tap_times) >= 2:
            intervals = [self._tap_times[i] - self._tap_times[i - 1] for i in range(1, len(self._tap_times))]
            avg_dt = sum(intervals) / len(intervals)
            if avg_dt > 0.1:
                bpm = max(40.0, min(240.0, round(60.0 / avg_dt, 1)))
                self._state["bpm"] = bpm
                self._state["sync"] = True
                self.syncFromState(self._state)
                self._emit_change()

    def _on_retrig_toggled(self, checked: bool):
        self._state["retrigger"] = checked
        self.syncFromState(self._state)
        self._emit_change()


# =====================================================================
# 10. 底部模块卡片 2：LFO Waveform & Shaping (波形选择与塑形)
# =====================================================================
class WaveformShapingCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, state: dict, parent=None):
        super().__init__(parent)
        self._state = state
        self._accent = PALETTE["amber"]
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

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {self._accent}; font-size: 9px;")
        h_lay.addWidget(dot)

        title = QLabel("WAVEFORM & SHAPE")
        title.setStyleSheet(f"color: {self._accent}; font-size: 11px; font-weight: 700; letter-spacing: 0.4px;")
        h_lay.addWidget(title)
        h_lay.addStretch()
        root.addWidget(header)

        # Body
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(10, 10, 10, 10)
        b_lay.setSpacing(10)

        # 6 种波形快捷选择网格 (2x3)
        wf_grid_top = QHBoxLayout()
        wf_grid_top.setSpacing(4)
        wf_grid_bot = QHBoxLayout()
        wf_grid_bot.setSpacing(4)

        self._wf_buttons: dict[str, QPushButton] = {}
        wf_labels = [
            ("Sine", "∿ Sine"),
            ("Triangle", "⋀ Tri"),
            ("Square", "⊓ Sqr"),
            ("Saw Up", "⟋ Saw+"),
            ("Saw Down", "⟍ Saw-"),
            ("Random", "⚄ Rand"),
        ]
        for idx, (wf_key, wf_txt) in enumerate(wf_labels):
            btn = QPushButton(wf_txt)
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, k=wf_key: self._on_wf_selected(k))
            self._wf_buttons[wf_key] = btn
            if idx < 3:
                wf_grid_top.addWidget(btn, 1)
            else:
                wf_grid_bot.addWidget(btn, 1)

        b_lay.addLayout(wf_grid_top)
        b_lay.addLayout(wf_grid_bot)

        # Shape + Smooth 双旋钮
        knobs_row = QHBoxLayout()
        knobs_row.setSpacing(10)

        self.c_shape = KnobParameterCell(
            "Symmetry / Shape", 0.0, 100.0, 50.0, self._accent,
            "0%", "50%", "100%",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear", pill_width=60
        )
        self.c_shape.valueChanged.connect(self._on_shape_changed)

        self.c_smooth = KnobParameterCell(
            "Edge Smooth", 0.0, 100.0, 15.0, PALETTE["blue"],
            "Sharp", "", "Soft",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear", pill_width=60
        )
        self.c_smooth.valueChanged.connect(self._on_smooth_changed)

        knobs_row.addWidget(self.c_shape, 1)
        knobs_row.addWidget(self.c_smooth, 1)
        b_lay.addLayout(knobs_row)

        root.addWidget(body, 1)
        self.syncFromState(state)

    def syncFromState(self, state: dict):
        self._updating = True
        self._state = state
        en = bool(state["enabled"])
        cur_wf = state["waveform"]

        self.setStyleSheet("""
            QFrame#ModuleCard {
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
            }
            QFrame#CardBody {
                background-color: transparent;
                border-top: 1px solid #141416;
            }
        """)

        btn_css = f"""
            QPushButton {{
                background-color: #323238;
                color: #B4B4BE;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #2B2418;
                color: {self._accent if en else '#8E8E98'};
                border: 1px solid {self._accent if en else '#3A3A40'};
                font-weight: 700;
            }}
        """
        for k, b in self._wf_buttons.items():
            b.setChecked(k == cur_wf)
            b.setStyleSheet(btn_css)

        self.c_shape.setValue(state["shape"])
        self.c_shape.setModuleActive(en)

        self.c_smooth.setValue(state["smooth"])
        self.c_smooth.setModuleActive(en)
        self._updating = False

    def _emit_change(self):
        if not self._updating:
            self.changed.emit(self._state)

    def _on_wf_selected(self, wf_key: str):
        self._state["waveform"] = wf_key
        self.syncFromState(self._state)
        self._emit_change()

    def _on_shape_changed(self, val: float):
        self._state["shape"] = round(val, 1)
        self._emit_change()

    def _on_smooth_changed(self, val: float):
        self._state["smooth"] = round(val, 1)
        self._emit_change()


# =====================================================================
# 11. 底部模块卡片 3：Depth, Stereo Phase & Pan Field (深度 / 相位偏移 / 声场宽度)
# =====================================================================
class DepthStereoPhaseCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, state: dict, parent=None):
        super().__init__(parent)
        self._state = state
        self._accent = PALETTE["violet"]
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

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {self._accent}; font-size: 9px;")
        h_lay.addWidget(dot)

        title = QLabel("DEPTH & STEREO PHASE")
        title.setStyleSheet(f"color: {self._accent}; font-size: 11px; font-weight: 700; letter-spacing: 0.4px;")
        h_lay.addWidget(title)
        h_lay.addStretch()

        # 相位快捷按钮: 0° (Mono) / 90° / 180° (Ping-Pong)
        for deg, tip in ((0.0, "0° 同相 (Mono Tremolo)"), (90.0, "90° 正交旋转"), (180.0, "180° 反相乒乓 (Ping-Pong)")):
            pb = QPushButton(f"{int(deg)}°")
            pb.setObjectName("ToolBtn")
            pb.setFixedSize(32, 20)
            pb.setToolTip(tip)
            pb.clicked.connect(lambda checked=False, d=deg: self._set_phase_preset(d))
            h_lay.addWidget(pb)

        root.addWidget(header)

        # Body
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(10, 10, 10, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(6)

        self.c_depth = KnobParameterCell(
            "Depth", 0.0, 100.0, 80.0, PALETTE["coral"],
            "0%", "50%", "100%",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear", pill_width=58
        )
        self.c_depth.valueChanged.connect(self._on_depth_changed)

        self.c_phase = KnobParameterCell(
            "Stereo Phase", 0.0, 360.0, 180.0, PALETTE["violet"],
            "0°", "180°", "360°",
            lambda v: f"{int(round(v)) % 360}°", lambda s, fb: float(s) % 360.0, mode="circular", pill_width=58
        )
        self.c_phase.valueChanged.connect(self._on_phase_changed)

        self.c_center = KnobParameterCell(
            "Pan Center", -100.0, 100.0, 0.0, PALETTE["cyan"],
            "100L", "C", "100R",
            format_lr, parse_lr, mode="bipolar", pill_width=58
        )
        self.c_center.valueChanged.connect(self._on_center_changed)

        self.c_width = KnobParameterCell(
            "Pan Spread", 0.0, 100.0, 100.0, PALETTE["green"],
            "0%", "50%", "100%",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear", pill_width=58
        )
        self.c_width.valueChanged.connect(self._on_width_changed)

        for cell in (self.c_depth, self.c_phase, self.c_center, self.c_width):
            row1.addWidget(cell, 1)

        b_lay.addLayout(row1)
        root.addWidget(body, 1)
        self.syncFromState(state)

    def syncFromState(self, state: dict):
        self._updating = True
        self._state = state
        en = bool(state["enabled"])
        is_pan_active = en and (state["mode"] in ("Auto-Pan", "Dual"))

        self.setStyleSheet("""
            QFrame#ModuleCard {
                background-color: #27272B;
                border: none;
                border-top: 1px solid #141416;
            }
            QFrame#CardBody {
                background-color: transparent;
                border-top: 1px solid #141416;
            }
        """)

        self.c_depth.setValue(state["depth"])
        self.c_depth.setModuleActive(en)

        self.c_phase.setValue(state["phase_offset"])
        self.c_phase.setModuleActive(en)

        self.c_center.setValue(state["pan_center"])
        self.c_center.setModuleActive(is_pan_active)

        self.c_width.setValue(state["pan_width"])
        self.c_width.setModuleActive(is_pan_active)
        self._updating = False

    def _emit_change(self):
        if not self._updating:
            self.changed.emit(self._state)

    def _set_phase_preset(self, deg: float):
        self._state["phase_offset"] = deg
        self.syncFromState(self._state)
        self._emit_change()

    def _on_depth_changed(self, val: float):
        self._state["depth"] = round(val, 1)
        self._emit_change()

    def _on_phase_changed(self, val: float):
        self._state["phase_offset"] = round(val, 1) % 360.0
        self._emit_change()

    def _on_center_changed(self, val: float):
        self._state["pan_center"] = round(val, 1)
        self._emit_change()

    def _on_width_changed(self, val: float):
        self._state["pan_width"] = round(val, 1)
        self._emit_change()


# =====================================================================
# 12. 主面板：Studio Tremolo / Auto-Pan Processor
# =====================================================================
class StudioTremoloAutoPanWidget(QWidget):
    # 对接后端 DSP 引擎的完整信号接口
    bypassChanged = pyqtSignal(bool)
    modeChanged = pyqtSignal(str)               # "Tremolo" | "Auto-Pan" | "Dual"
    rateSyncChanged = pyqtSignal(dict)          # sync, bpm, rate_hz, sync_div, retrigger
    waveformChanged = pyqtSignal(dict)          # waveform, shape, smooth
    modulationChanged = pyqtSignal(dict)        # depth, phase_offset, pan_center, pan_width
    stateChanged = pyqtSignal(dict)             # 全状态字典快照

    def __init__(self, track_name: str = "Audio 1", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Tremolo / Auto-Pan — {track_name}")
        self.resize(940, 560)
        self.setMinimumSize(860, 510)

        self._active_bank = "A"
        self._banks = {
            "A": default_tremolo_autopan_state(),
            "B": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo",
                "sync": True,
                "sync_div": "1/16",
                "waveform": "Square",
                "shape": 50.0,
                "smooth": 12.0,
                "depth": 90.0,
                "phase_offset": 0.0,
            },
        }

        self._presets = {
            # --- 1. 经典 Auto-Pan 立体声声像移动 ---
            "Wide Ping-Pong Auto-Pan": {
                **default_tremolo_autopan_state(),
                "mode": "Auto-Pan", "sync": False, "rate_hz": 2.40,
                "waveform": "Sine", "shape": 50.0, "smooth": 15.0,
                "depth": 85.0, "phase_offset": 180.0, "pan_center": 0.0, "pan_width": 100.0,
            },
            "Slow Ambient Stereo Drift": {
                **default_tremolo_autopan_state(),
                "mode": "Auto-Pan", "sync": False, "rate_hz": 0.18,
                "waveform": "Sine", "shape": 50.0, "smooth": 45.0,
                "depth": 75.0, "phase_offset": 90.0, "pan_center": 0.0, "pan_width": 90.0,
            },
            "1/4 Bar Ping-Pong Bounce": {
                **default_tremolo_autopan_state(),
                "mode": "Auto-Pan", "sync": True, "bpm": 120.0, "sync_div": "1/4",
                "waveform": "Square", "shape": 50.0, "smooth": 28.0,
                "depth": 95.0, "phase_offset": 180.0, "pan_center": 0.0, "pan_width": 100.0,
            },
            "1/8 Triplet Circular Swirl": {
                **default_tremolo_autopan_state(),
                "mode": "Auto-Pan", "sync": True, "bpm": 120.0, "sync_div": "1/8T",
                "waveform": "Triangle", "shape": 50.0, "smooth": 20.0,
                "depth": 80.0, "phase_offset": 120.0, "pan_center": 0.0, "pan_width": 95.0,
            },
            "Left-Side Subtle Orbit": {
                **default_tremolo_autopan_state(),
                "mode": "Auto-Pan", "sync": False, "rate_hz": 0.85,
                "waveform": "Sine", "shape": 55.0, "smooth": 30.0,
                "depth": 50.0, "phase_offset": 60.0, "pan_center": -45.0, "pan_width": 45.0,
            },
            "Right-Side Subtle Orbit": {
                **default_tremolo_autopan_state(),
                "mode": "Auto-Pan", "sync": False, "rate_hz": 0.85,
                "waveform": "Sine", "shape": 45.0, "smooth": 30.0,
                "depth": 50.0, "phase_offset": 60.0, "pan_center": 45.0, "pan_width": 45.0,
            },

            # --- 2. 经典复古与现代 Tremolo 音量震音 ---
            "Classic 60s Amp Tremolo": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo", "sync": False, "rate_hz": 4.80,
                "waveform": "Sine", "shape": 46.0, "smooth": 35.0,
                "depth": 65.0, "phase_offset": 0.0,
            },
            "Vintage Bias Tube Pulse": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo", "sync": False, "rate_hz": 3.50,
                "waveform": "Triangle", "shape": 38.0, "smooth": 40.0,
                "depth": 55.0, "phase_offset": 0.0,
            },
            "Fender Opto Deep Throb": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo", "sync": False, "rate_hz": 5.60,
                "waveform": "Sine", "shape": 62.0, "smooth": 25.0,
                "depth": 88.0, "phase_offset": 0.0,
            },
            "Electric Piano Rhodes Shimmer": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo", "sync": False, "rate_hz": 3.80,
                "waveform": "Sine", "shape": 50.0, "smooth": 30.0,
                "depth": 60.0, "phase_offset": 180.0,
            },

            # --- 3. 电子节奏切片与 Sidechain 抽吸模拟 ---
            "1/16 Trance Gate Chopper": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo", "sync": True, "bpm": 128.0, "sync_div": "1/16",
                "waveform": "Square", "shape": 50.0, "smooth": 6.0,
                "depth": 100.0, "phase_offset": 0.0,
            },
            "1/8D Dotted Rhythmic Pulse": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo", "sync": True, "bpm": 124.0, "sync_div": "1/8D",
                "waveform": "Square", "shape": 42.0, "smooth": 10.0,
                "depth": 95.0, "phase_offset": 180.0,
            },
            "1/4 Sidechain Pump Ducking": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo", "sync": True, "bpm": 126.0, "sync_div": "1/4",
                "waveform": "Saw Up", "shape": 65.0, "smooth": 24.0,
                "depth": 82.0, "phase_offset": 0.0,
            },
            "1/8 Reverse Swell Gate": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo", "sync": True, "bpm": 120.0, "sync_div": "1/8",
                "waveform": "Saw Down", "shape": 35.0, "smooth": 18.0,
                "depth": 90.0, "phase_offset": 0.0,
            },

            # --- 4. Dual 双模联动与音效设计 ---
            "Dual Psychedelic Spiral": {
                **default_tremolo_autopan_state(),
                "mode": "Dual", "sync": True, "bpm": 120.0, "sync_div": "1/8",
                "waveform": "Triangle", "shape": 35.0, "smooth": 20.0,
                "depth": 85.0, "phase_offset": 180.0, "pan_center": 0.0, "pan_width": 100.0,
            },
            "Helicopter Rotor Flutter": {
                **default_tremolo_autopan_state(),
                "mode": "Dual", "sync": False, "rate_hz": 14.50,
                "waveform": "Saw Down", "shape": 28.0, "smooth": 12.0,
                "depth": 92.0, "phase_offset": 90.0, "pan_center": 0.0, "pan_width": 85.0,
            },
            "Sci-Fi Random S&H Scatter": {
                **default_tremolo_autopan_state(),
                "mode": "Dual", "sync": True, "bpm": 130.0, "sync_div": "1/16",
                "waveform": "Random", "shape": 50.0, "smooth": 8.0,
                "depth": 90.0, "phase_offset": 180.0, "pan_center": 0.0, "pan_width": 100.0,
            },
            "Broken Radio Glitch Stutter": {
                **default_tremolo_autopan_state(),
                "mode": "Tremolo", "sync": False, "rate_hz": 22.0,
                "waveform": "Random", "shape": 30.0, "smooth": 2.0,
                "depth": 98.0, "phase_offset": 45.0,
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 60fps LFO 相位动画定时器（接入外部宿主时钟后可调用 update_phase() 替代）
        self._lfo_phase = 0.0
        self._last_tick = time.monotonic()
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_lfo_animation)
        self._anim_timer.start(16)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            LfoModulationVisualizer, StereoModulationMeter {{
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
                min-height: 21px;
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
        # 1. 顶部统一工具栏（电源 + 标题 + 模式切换 + 预设 + A/B 对比 + Reset）
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setFixedHeight(38)
        top_bar.setObjectName("TopBar")
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb = QHBoxLayout(top_bar)
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True, PALETTE["cyan"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb.addWidget(self.power_switch)

        title_lbl = QLabel(f"TREMOLO / AUTO-PAN  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb.addWidget(title_lbl)

        tb.addSpacing(8)

        # 核心引擎模式切换分段胶囊 (Tremolo | Auto-Pan | Dual)
        mode_wrap = QFrame()
        mode_wrap.setStyleSheet("background-color: #18181B; border: 1px solid #121214; border-radius: 4px;")
        mode_lay = QHBoxLayout(mode_wrap)
        mode_lay.setContentsMargins(2, 2, 2, 2)
        mode_lay.setSpacing(2)

        self._mode_btns: dict[str, QPushButton] = {}
        for m_name in ("Tremolo", "Auto-Pan", "Dual"):
            mb = QPushButton(m_name)
            mb.setCheckable(True)
            mb.setFixedHeight(20)
            mb.setMinimumWidth(64)
            mb.setCursor(Qt.CursorShape.PointingHandCursor)
            mb.clicked.connect(lambda checked=False, m=m_name: self._on_mode_selected(m))
            self._mode_btns[m_name] = mb
            mode_lay.addWidget(mb)
        tb.addWidget(mode_wrap)

        tb.addSpacing(8)

        # 预设管理
        add_btn = QPushButton("+")
        add_btn.setObjectName("ToolBtn")
        add_btn.setFixedSize(22, 22)
        add_btn.setToolTip("保存为新预设")
        add_btn.clicked.connect(self._save_new_preset)
        tb.addWidget(add_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedWidth(185)
        self.preset_combo.addItems(list(self._presets.keys()))
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        tb.addWidget(self.preset_combo)

        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setFixedSize(21, 22)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb.addWidget(prev_btn)

        next_btn = QPushButton("›")
        next_btn.setObjectName("ToolBtn")
        next_btn.setFixedSize(21, 22)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb.addWidget(next_btn)

        tb.addSpacing(8)

        # A / B 对比槽
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

        tb.addWidget(self.btn_a)
        tb.addWidget(self.btn_copy_ab)
        tb.addWidget(self.btn_b)

        tb.addStretch()

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_processor)
        tb.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监视区：双通道 LFO 示波器 + 立体声摆动舞台 + L/R 调制表
        # -------------------------------------------------------------
        upper_panel = QFrame()
        up_lay = QHBoxLayout(upper_panel)
        up_lay.setContentsMargins(0, 0, 0, 0)
        up_lay.setSpacing(0)

        self.visualizer = LfoModulationVisualizer()
        self.visualizer.depthDragged.connect(self._on_vis_depth_dragged)
        self.visualizer.phaseOffsetDragged.connect(self._on_vis_phase_dragged)
        up_lay.addWidget(self.visualizer, 1)

        vsep = QFrame()
        vsep.setFixedWidth(1)
        vsep.setStyleSheet("background-color: #141416; border: none;")
        up_lay.addWidget(vsep)

        meter_col = QVBoxLayout()
        meter_col.setContentsMargins(2, 6, 2, 4)
        meter_col.setSpacing(2)
        m_lbl = QLabel("MOD")
        m_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        m_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.4px;")
        meter_col.addWidget(m_lbl)

        self.mod_meter = StereoModulationMeter()
        meter_col.addWidget(self.mod_meter, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(meter_col)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部三模块控制矩阵 (Rate & Sync | Waveform & Shape | Depth & Stereo Phase)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        st = self._st()
        self.rate_card = RateSyncCard(st)
        self.rate_card.changed.connect(self._on_rate_card_changed)

        self.wave_card = WaveformShapingCard(st)
        self.wave_card.changed.connect(self._on_wave_card_changed)

        self.depth_card = DepthStereoPhaseCard(st)
        self.depth_card.changed.connect(self._on_depth_card_changed)

        cards_row.addWidget(self.rate_card, 3)
        cards_row.addWidget(self.wave_card, 3)
        cards_row.addWidget(self.depth_card, 4)

        main_lay.addLayout(cards_row, 4)

    # -----------------------------------------------------------------
    # 状态管理与全视图同步
    # -----------------------------------------------------------------
    def _st(self) -> dict:
        return self._banks[self._active_bank]

    def _effective_freq_hz(self) -> float:
        st = self._st()
        if st["sync"]:
            mult = SYNC_MULT_MAP.get(st["sync_div"], 1.0)
            return (st["bpm"] / 60.0) * mult
        return st["rate_hz"]

    def _update_mode_and_ab_styles(self):
        st = self._st()
        en = st["enabled"]
        cur_mode = st["mode"]

        mode_colors = {
            "Tremolo": PALETTE["green"],
            "Auto-Pan": PALETTE["cyan"],
            "Dual": PALETTE["violet"],
        }
        for m_name, btn in self._mode_btns.items():
            is_sel = (m_name == cur_mode)
            btn.setChecked(is_sel)
            ac = mode_colors[m_name]
            if is_sel:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: #182E38;
                        color: {ac if en else '#8E8E98'};
                        border: 1px solid {ac if en else '#3A3A40'};
                        border-radius: 3px;
                        font-size: 10px;
                        font-weight: 700;
                    }}
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: transparent;
                        color: #9A9AA2;
                        border: none;
                        border-radius: 3px;
                        font-size: 10px;
                        font-weight: 600;
                    }
                    QPushButton:hover { color: #FFFFFF; }
                """)

        active_ab = f"""
            QPushButton {{
                background-color: #182E38;
                color: {PALETTE["cyan"]};
                border: 1px solid #24788C;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
            }}
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
        st = self._st()
        self._update_mode_and_ab_styles()
        self.power_switch.setChecked(st["enabled"])
        self.rate_card.syncFromState(st)
        self.wave_card.syncFromState(st)
        self.depth_card.syncFromState(st)
        self.visualizer.setState(st)

    # -----------------------------------------------------------------
    # 交互回调与信号分发
    # -----------------------------------------------------------------
    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _copy_bank(self):
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._st())

    def _on_power_toggled(self, enabled: bool):
        self._st()["enabled"] = enabled
        self._refresh_all_views()
        self.bypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_mode_selected(self, mode: str):
        if self._st()["mode"] != mode:
            self._st()["mode"] = mode
            self._refresh_all_views()
            self.modeChanged.emit(mode)
            self.stateChanged.emit(self.get_state())

    def _on_rate_card_changed(self, st: dict):
        self.visualizer.setState(st)
        self.rateSyncChanged.emit({
            "sync": st["sync"],
            "bpm": st["bpm"],
            "rate_hz": st["rate_hz"],
            "sync_div": st["sync_div"],
            "effective_hz": self._effective_freq_hz(),
            "retrigger": st["retrigger"],
        })
        self.stateChanged.emit(self.get_state())

    def _on_wave_card_changed(self, st: dict):
        self.visualizer.setState(st)
        self.waveformChanged.emit({
            "waveform": st["waveform"],
            "shape": st["shape"],
            "smooth": st["smooth"],
        })
        self.stateChanged.emit(self.get_state())

    def _on_depth_card_changed(self, st: dict):
        self.visualizer.setState(st)
        self.modulationChanged.emit({
            "depth": st["depth"],
            "phase_offset": st["phase_offset"],
            "pan_center": st["pan_center"],
            "pan_width": st["pan_width"],
        })
        self.stateChanged.emit(self.get_state())

    def _on_vis_depth_dragged(self, new_depth: float):
        st = self._st()
        st["depth"] = new_depth
        self.depth_card.syncFromState(st)
        self.visualizer.setState(st)
        self.modulationChanged.emit({
            "depth": st["depth"],
            "phase_offset": st["phase_offset"],
            "pan_center": st["pan_center"],
            "pan_width": st["pan_width"],
        })
        self.stateChanged.emit(self.get_state())

    def _on_vis_phase_dragged(self, new_phase: float):
        st = self._st()
        st["phase_offset"] = new_phase
        self.depth_card.syncFromState(st)
        self.visualizer.setState(st)
        self.modulationChanged.emit({
            "depth": st["depth"],
            "phase_offset": st["phase_offset"],
            "pan_center": st["pan_center"],
            "pan_width": st["pan_width"],
        })
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
        name, ok = QInputDialog.getText(self, "保存 Tremolo / Auto-Pan 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._st())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 实时动画驱动（默认内部时钟，接入真实后端后调用 update_phase 即可接管）
    # -----------------------------------------------------------------
    def _tick_lfo_animation(self):
        now = time.monotonic()
        dt = min(0.05, now - self._last_tick)
        self._last_tick = now

        st = self._st()
        if st["enabled"]:
            freq = self._effective_freq_hz()
            # 2.0 对应示波器双周期 [0.0, 2.0)
            self._lfo_phase = (self._lfo_phase + freq * dt) % 2.0

        self._update_visual_meters(self._lfo_phase)

    def _update_visual_meters(self, phase_0_2: float):
        st = self._st()
        en = st["enabled"]
        depth = (st["depth"] / 100.0) if en else 0.0
        phase_off_norm = (st["phase_offset"] % 360.0) / 360.0

        raw_l = evaluate_lfo_unipolar(phase_0_2 % 1.0, st["waveform"], st["shape"], st["smooth"])
        raw_r = evaluate_lfo_unipolar((phase_0_2 + phase_off_norm) % 1.0, st["waveform"], st["shape"], st["smooth"])

        if st["mode"] == "Tremolo":
            val_l = 1.0 - depth * (1.0 - raw_l)
            val_r = 1.0 - depth * (1.0 - raw_r)
        else:
            val_l = 0.5 + (raw_l - 0.5) * depth
            val_r = 0.5 + (raw_r - 0.5) * depth

        self.visualizer.setPhase(phase_0_2)
        self.mod_meter.setModulationValues(val_l, val_r, st["mode"], en)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_processor(self):
        self._banks[self._active_bank] = default_tremolo_autopan_state()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        st = copy.deepcopy(self._st())
        st["effective_hz"] = self._effective_freq_hz()
        return st

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_phase(self, lfo_phase_cycles: float):
        """接入后端 DSP 音频线程的精确 LFO 累计周期相位，自动接管示波器刷新"""
        if self._anim_timer.isActive():
            self._anim_timer.stop()
        self._update_visual_meters(lfo_phase_cycles % 2.0)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioTremoloAutoPanWidget("Audio 1")
    win.show()
    sys.exit(app.exec())