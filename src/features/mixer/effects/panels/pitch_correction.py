from __future__ import annotations

import sys
import math
import copy
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QFrame, QLineEdit, QInputDialog, QMenu
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont
)


# =====================================================================
# 1. 全局色彩系统与音高声学常量（统一 Fairlight 深色哑光视觉规范）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":     "#28D8F0",  # 主控 / A-B 激活 / 修正后音高线
    "retune":   "#4CD490",  # Retune & Flex-Tune 修正引擎模块主题色（翡翠绿）
    "scale":    "#F0A848",  # 调性音阶与琴键锁定模块主题色（琥珀金）
    "formant":  "#B67CF8",  # 喉腔共振峰保护与颤音模块主题色（紫罗兰）
    "coral":    "#F26D6D",  # 音高偏差警示 / 削波提示
    "orig_line": "#8A92A6", # 原始检测音高虚线
}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
BLACK_KEY_INDICES = {1, 3, 6, 8, 10}

SCALE_PATTERNS = {
    "Chromatic (半音阶)":   [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
    "Major (自然大调)":     [0, 2, 4, 5, 7, 9, 11],
    "Minor (自然小调)":     [0, 2, 3, 5, 7, 8, 10],
    "Harmonic Minor":       [0, 2, 3, 5, 7, 8, 11],
    "Melodic Minor":        [0, 2, 3, 5, 7, 9, 11],
    "Major Pentatonic":     [0, 2, 4, 7, 9],
    "Minor Pentatonic":     [0, 3, 5, 7, 10],
    "Blues":                [0, 3, 5, 6, 7, 10],
    "Dorian":               [0, 2, 3, 5, 7, 9, 10],
    "Mixolydian":           [0, 2, 4, 5, 7, 9, 10],
    "Custom (手动点选)":    [0, 2, 4, 5, 7, 9, 11],
}

VOICE_TYPES = {
    "Soprano (女高音)":    {"min_midi": 60, "max_midi": 88, "ref_hz": 440.0},  # C4 - E6
    "Alto / Tenor (中音)": {"min_midi": 48, "max_midi": 76, "ref_hz": 440.0},  # C3 - E5
    "Low Male (男低音)":   {"min_midi": 36, "max_midi": 64, "ref_hz": 440.0},  # C2 - E4
    "Instrument / Wide":   {"min_midi": 36, "max_midi": 96, "ref_hz": 440.0},  # C2 - C7
}


def midi_to_note_name(midi_val: float, include_octave: bool = True) -> str:
    m_int = int(round(midi_val))
    name = NOTE_NAMES[m_int % 12]
    if not include_octave:
        return name
    octave = (m_int // 12) - 1
    return f"{name}{octave}"


def midi_to_hz(midi_val: float, ref_a4: float = 440.0) -> float:
    return ref_a4 * (2.0 ** ((midi_val - 69.0) / 12.0))


def hz_to_midi(hz: float, ref_a4: float = 440.0) -> float:
    if hz <= 0.0:
        return 60.0
    return 69.0 + 12.0 * math.log2(hz / ref_a4)


def format_ms(val: float) -> str:
    if val < 1.0:
        return f"{val:.1f} ms"
    return f"{int(round(val))} ms"


def format_pct(val: float) -> str:
    return f"{int(round(val))}%"


def format_st(val: float) -> str:
    if abs(val) < 0.05:
        return "0.0 st"
    return f"{val:+.1f} st"


def format_cents(val: float) -> str:
    iv = int(round(val))
    if iv == 0:
        return "0 ct"
    return f"{iv:+d} ct"


def parse_numeric(text: str, fallback: float) -> float:
    s = (
        text.strip()
        .lower()
        .replace("ms", "")
        .replace("st", "")
        .replace("cents", "")
        .replace("ct", "")
        .replace("hz", "")
        .replace("%", "")
        .strip()
    )
    try:
        return float(s)
    except ValueError:
        return fallback


def default_pitch_demo_transparent() -> dict:
    """默认配置 1：自然隐形修音（保留颤音与滑音表情，仅修正走音尾音）"""
    return {
        "enabled": True,
        "mode": "Transparent",        # "Transparent" (隐形自然) | "Hard Tune" (电音阶梯) | "Graph Edit" (曲线手修)
        "voice_type": "Alto / Tenor (中音)",
        "ref_a4": 440.0,
        "output_gain": 0.0,
        "correction": {
            "enabled": True,
            "retune_speed": 38.0,     # 0.0 ~ 400.0 ms (0=极致电音阶梯, 30~60=自然隐形修正)
            "flex_tune": 65.0,        # 0 ~ 100 % (允许滑音/装饰音自由表达，仅在稳定持音时介入)
            "humanize": 45.0,         # 0 ~ 100 % (长音尾音保持自然微波动，避免机械死板)
            "natural_vibrato": 0.0,   # -100 ~ +100 % (原声颤音幅度的缩放/增强)
            "correction_amt": 88.0,   # 0 ~ 100 % (整体音准吸附强度)
            "mix": 100.0,             # 0 ~ 100 %
        },
        "scale": {
            "enabled": True,
            "root": "C",              # C, C#, D ... B
            "scale_type": "Major (自然大调)",
            "active_notes": [True, False, True, False, True, True, False, True, False, True, False, True],
            "bypass_notes": [False] * 12,  # 蓝标：穿过该音但不强行吸附
            "detune_cents": 0.0,      # -50 ~ +50 ct 全局音高微调基准
            "transpose": 0.0,         # -12 ~ +12 st 音阶内移调
        },
        "formant": {
            "enabled": True,
            "preserve": 95.0,         # 0 ~ 100 % 喉腔共振峰保护（防止高音变花栗鼠）
            "throat_shift": 0.0,      # -12.0 ~ +12.0 st 独立共振峰微调
            "breath_pass": 85.0,      # 0 ~ 100 % 清辅音/气音直通旁通
            "sibilance_guard": 70.0,  # 0 ~ 100 % 齿音防失真保护
            "tilt": 0.0,              # -50 ~ +50 % 明亮度倾斜
            "mix": 100.0,             # 0 ~ 100 %
        },
        # 曲线编辑器中的人工修正锚点/音块列表 (Melodyne 风格手修节点)
        "graph_anchors": [
            {"id": 1, "beat": 0.45, "duration": 0.55, "midi": 64.0, "offset_cents": 0.0, "drift": 15.0},
            {"id": 2, "beat": 1.25, "duration": 0.65, "midi": 67.0, "offset_cents": 0.0, "drift": 20.0},
            {"id": 3, "beat": 2.15, "duration": 0.70, "midi": 69.0, "offset_cents": 0.0, "drift": 10.0},
            {"id": 4, "beat": 3.05, "duration": 0.85, "midi": 67.0, "offset_cents": 0.0, "drift": 25.0},
        ],
    }


def default_pitch_hard_autotune() -> dict:
    """默认配置 2：经典 Auto-Tune 零延迟硬核电音阶梯效果"""
    st = default_pitch_demo_transparent()
    st["mode"] = "Hard Tune"
    st["correction"]["retune_speed"] = 0.0
    st["correction"]["flex_tune"] = 0.0
    st["correction"]["humanize"] = 0.0
    st["correction"]["correction_amt"] = 100.0
    st["scale"]["root"] = "A"
    st["scale"]["scale_type"] = "Minor (自然小调)"
    st["scale"]["active_notes"] = [True, False, True, True, False, True, False, True, True, False, True, False]
    return st


# =====================================================================
# 2. 基础控件：电源开关 / 可编辑数字胶囊 / 阳极铝旋钮 / 垂直电平与偏差表
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
                self._text.replace("ms", "")
                .replace("st", "")
                .replace("ct", "")
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

        p.setPen(QPen(QColor("#141417"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        arc_col = self._accent if self._active else QColor("#4A4A52")
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
# 3. 控件：音准偏差双向指示表 (Cent Deviation Meter: -50 ~ +50 cents)
# =====================================================================
class PitchCentsMeter(QWidget):
    """实时音准偏差表：0 cent 居中，向上/向下显示检测输入或修正量偏移"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._detected_cents = 0.0   # -50 ~ +50 cents
        self._correction_cents = 0.0 # -50 ~ +50 cents
        self._detected_note = "A4"
        self._active = True
        self.setFixedWidth(54)
        self.setMinimumHeight(180)

    def setPitchReadout(self, note_name: str, detected_cents: float, correction_cents: float):
        self._detected_note = note_name
        self._detected_cents = max(-50.0, min(50.0, detected_cents))
        self._correction_cents = max(-50.0, min(50.0, correction_cents))
        self.update()

    def setActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 顶部检测音符胶囊
        note_rect = QRectF(4.0, 4.0, self.width() - 8.0, 22.0)
        p.setPen(QPen(QColor("#24788C") if self._active else QColor("#26262A"), 1.0))
        p.setBrush(QColor("#141C22") if self._active else QColor("#18181B"))
        p.drawRoundedRect(note_rect, 3.0, 3.0)
        p.setPen(QColor("#28D8F0") if self._active else QColor("#6E6E76"))
        p.setFont(QFont(MONO_FONT_FAM, 10, QFont.Weight.Bold))
        p.drawText(note_rect, Qt.AlignmentFlag.AlignCenter, self._detected_note if self._active else "--")

        top_pad = 32.0
        bot_pad = 22.0
        bar_w = 10.0
        bar = QRectF(34.0, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))
        mid_y = bar.center().y()

        # 刻度数字 (+50, +25, 0, -25, -50)
        p.setFont(QFont(MONO_FONT_FAM, 7))
        for c_val in (50, 25, 0, -25, -50):
            norm = (50.0 - c_val) / 100.0
            ty = bar.top() + norm * bar.height()
            p.setPen(QColor("#4CD490") if c_val == 0 else QColor("#8A8A94"))
            lbl = "0" if c_val == 0 else f"{c_val:+d}"
            p.drawText(QRectF(0, ty - 6, 30, 12), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        # 表槽背景
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        # 0 cent 中心准星线
        p.setPen(QPen(QColor("#4CD490"), 1.3))
        p.drawLine(QPointF(bar.left() - 2, mid_y), QPointF(bar.right() + 2, mid_y))

        if self._active:
            # 1. 修正量填充条（翡翠绿）
            if abs(self._correction_cents) > 0.5:
                h = (abs(self._correction_cents) / 50.0) * (bar.height() * 0.48)
                if self._correction_cents > 0:
                    fill = QRectF(bar.left() + 1.5, mid_y - h, bar.width() - 3.0, h)
                else:
                    fill = QRectF(bar.left() + 1.5, mid_y, bar.width() - 3.0, h)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#4CD490"))
                p.drawRoundedRect(fill, 1.2, 1.2)

            # 2. 原始输入音高游标线（琥珀金/珊瑚红）
            det_y = mid_y - (self._detected_cents / 50.0) * (bar.height() * 0.48)
            ind_col = QColor("#F26D6D") if abs(self._detected_cents) > 25.0 else QColor("#F0A848")
            p.setPen(QPen(ind_col, 2.0))
            p.drawLine(QPointF(bar.left() + 0.5, det_y), QPointF(bar.right() - 0.5, det_y))

        # 底部实时偏差数字
        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
        c_str = f"{int(round(self._detected_cents)):+d}c" if self._active else "0c"
        p.drawText(QRectF(0, self.height() - 18, self.width(), 15), Qt.AlignmentFlag.AlignCenter, c_str)


# =====================================================================
# 4. 控件：左侧 Input 电平表 & 右侧 Output 推子+电平表
# =====================================================================
class CompactLevelMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._level_db = -24.0
        self._peak_db = -24.0
        self._clipped = False
        self.setFixedWidth(34)

    def setLevel(self, db: float):
        self._level_db = max(-60.0, min(0.0, db))
        if self._level_db >= self._peak_db:
            self._peak_db = self._level_db
        else:
            self._peak_db = max(self._level_db, self._peak_db - 0.35)
        if db >= -0.1:
            self._clipped = True
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._clipped = False
            self._peak_db = self._level_db
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        bar = QRectF(21.0, 10.0, 6.0, max(40.0, self.height() - 36.0))
        p.setFont(QFont(MONO_FONT_FAM, 7))
        for t in (0, -10, -20, -30, -40, -50):
            norm = (t + 60.0) / 60.0
            ty = bar.bottom() - norm * bar.height()
            p.setPen(QColor("#8A8A94"))
            p.drawText(QRectF(0, ty - 6, 18, 12), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(t))

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        m_norm = max(0.0, min(1.0, (self._level_db + 60.0) / 60.0))
        if m_norm > 0.01:
            fh = bar.height() * m_norm
            fill = QRectF(bar.left() + 0.5, bar.bottom() - fh, bar.width() - 1.0, fh)
            grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
            grad.setColorAt(0.0, QColor("#28C882"))
            grad.setColorAt(0.75, QColor("#E8B838"))
            grad.setColorAt(1.0, QColor("#F05050"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(fill, 1.5, 1.5)

        if self._peak_db > -59.5:
            py = bar.bottom() - ((self._peak_db + 60.0) / 60.0) * bar.height()
            p.setPen(QPen(QColor("#ECECF0"), 1.2))
            p.drawLine(QPointF(bar.left() + 0.5, py), QPointF(bar.right() - 0.5, py))

        clip_rect = QRectF(bar.left(), bar.top() - 7.0, bar.width(), 4.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)


class MasterOutputStrip(QWidget):
    gainChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gain = 0.0
        self._meter_db = -18.0
        self._dragging = False
        self._hovered = False
        self._clipped = False
        self.setFixedWidth(62)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def setGain(self, val: float, emit: bool = False):
        clamped = max(-20.0, min(20.0, round(val, 1)))
        if abs(clamped - self._gain) > 1e-4:
            self._gain = clamped
            self.update()
            if emit:
                self.gainChanged.emit(self._gain)

    def setMeterLevel(self, level_db: float):
        self._meter_db = max(-60.0, min(0.0, level_db))
        if level_db >= -0.1:
            self._clipped = True
        self.update()

    def _track_rect(self) -> QRectF:
        return QRectF(19.0, 10.0, 4.0, max(40.0, self.height() - 36.0))

    def _y_to_gain(self, y: float) -> float:
        tr = self._track_rect()
        norm = max(0.0, min(1.0, (y - tr.top()) / tr.height()))
        return 20.0 - norm * 40.0

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if event.position().y() <= 14.0 and event.position().x() >= 38.0:
                self._clipped = False
                self.update()
                return
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

        meter_rect = QRectF(44.0, tr.top(), 5.0, tr.height())
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(meter_rect, 2.0, 2.0)

        m_norm = max(0.0, min(1.0, (self._meter_db + 60.0) / 60.0))
        if m_norm > 0.01:
            fill_h = meter_rect.height() * m_norm
            fill_rect = QRectF(meter_rect.left() + 0.5, meter_rect.bottom() - fill_h, meter_rect.width() - 1.0, fill_h)
            m_grad = QLinearGradient(meter_rect.bottomLeft(), meter_rect.topLeft())
            m_grad.setColorAt(0.0, QColor("#28C882"))
            m_grad.setColorAt(0.75, QColor("#E8B838"))
            m_grad.setColorAt(1.0, QColor("#F05050"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(m_grad)
            p.drawRoundedRect(fill_rect, 1.5, 1.5)

        clip_rect = QRectF(44.0, tr.top() - 7.0, 5.0, 4.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)

        norm = (20.0 - self._gain) / 40.0
        cy = tr.top() + norm * tr.height()
        cap_w, cap_h = 18.0, 22.0
        cap_rect = QRectF(cx - cap_w / 2.0, cy - cap_h / 2.0, cap_w, cap_h)

        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawRoundedRect(cap_rect, 3.0, 3.0)

        p.setPen(QPen(QColor("#28D8F0"), 1.6))
        p.drawLine(QPointF(cap_rect.left() + 3, cy), QPointF(cap_rect.right() - 3, cy))

        font = QFont(MONO_FONT_FAM, 9, QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor("#28D8F0"))
        val_str = f"{self._gain:+.1f}" if abs(self._gain) >= 0.05 else "0.0"
        p.drawText(QRectF(0, self.height() - 22, self.width(), 18), Qt.AlignmentFlag.AlignCenter, val_str)


# =====================================================================
# 5. 核心交互画布：琴键音阶尺 + 逐音音高轨迹 + 修正曲线编辑器 (PitchGraphEditorWidget)
# =====================================================================
class PitchGraphEditorWidget(QWidget):
    """
    结合 Antares Auto-Tune 实时音高跟踪线 与 Melodyne 音块/锚点微调曲线编辑器：
      - 左侧 12 音高钢琴卷帘尺（支持点击切换音阶内激活/Bypass/禁用音符）
      - 实时滚动/静态音高曲线：灰色虚线为输入原声曲线，青绿实线为修音后曲线
      - 修正曲线锚点 (Note Blobs / Correction Anchors)：
        * 左键上下拖动：微调目标音高 (Semitones / Shift 精细 Cents 音分)
        * 左键左右拖动：移动时间位置
        * 滚轮上下：调节单音块 Pitch Drift / 颤音平稳度
        * 双击空白处：新增人工修正锚点；右键锚点：吸附到最近音阶音 / 删除
    """
    noteScaleStateToggled = pyqtSignal(int)         # 点击左侧钢琴键切换音级状态 (0~11)
    anchorsChanged = pyqtSignal(list)               # 手修曲线锚点列表更新
    retuneScrolled = pyqtSignal(float)              # 画布内 Ctrl+滚轮 快捷调节 Retune Speed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 250)
        self.setMouseTracking(True)

        self._state = default_pitch_demo_transparent()
        self._show_orig_curve = True
        self._show_blobs = True
        self._snap_semitone = True

        self._piano_w = 54.0
        self._mt = 10.0
        self._mb = 22.0
        self._mr = 12.0

        self._hover_piano_midi = -1
        self._hover_anchor_idx = -1
        self._selected_anchor_idx = -1
        self._dragging_anchor_idx = -1
        self._drag_start_pos = QPointF()
        self._drag_start_midi = 60.0
        self._drag_start_cents = 0.0
        self._drag_start_beat = 0.0
        self._mouse_pos: QPointF | None = None

        self._btn_orig_rect = QRectF()
        self._btn_blob_rect = QRectF()
        self._btn_snap_rect = QRectF()

        # 4 拍时间窗口内的实时音高历史轨迹 [(beat_0_to_4, raw_midi, corrected_midi, voiced_bool), ...]
        self._num_pts = 180
        self._pitch_trace: list[tuple[float, float, float, bool]] = []
        self._playhead_beat = 0.0

    def setState(self, state: dict):
        self._state = state
        self.update()

    def updatePitchTrace(self, trace: list[tuple[float, float, float, bool]], playhead_beat: float = 0.0):
        self._pitch_trace = trace
        self._playhead_beat = playhead_beat % 4.0
        self.update()

    def _midi_range(self) -> tuple[float, float]:
        vt = VOICE_TYPES.get(self._state["voice_type"], VOICE_TYPES["Alto / Tenor (中音)"])
        return float(vt["min_midi"]), float(vt["max_midi"])

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._piano_w,
            self._mt,
            max(120.0, self.width() - self._piano_w - self._mr),
            max(120.0, self.height() - self._mt - self._mb),
        )

    def _midi_to_y(self, midi_val: float, plot: QRectF) -> float:
        m_min, m_max = self._midi_range()
        span = max(12.0, m_max - m_min)
        norm = (m_max - midi_val) / span
        return plot.top() + norm * plot.height()

    def _y_to_midi(self, y: float, plot: QRectF) -> float:
        m_min, m_max = self._midi_range()
        span = max(12.0, m_max - m_min)
        norm = max(0.0, min(1.0, (y - plot.top()) / plot.height()))
        return m_max - norm * span

    def _beat_to_x(self, beat: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, beat / 4.0))
        return plot.left() + norm * plot.width()

    def _x_to_beat(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return norm * 4.0

    def _anchor_rect(self, anchor: dict, plot: QRectF) -> QRectF:
        eff_midi = anchor["midi"] + anchor.get("offset_cents", 0.0) / 100.0
        x1 = self._beat_to_x(anchor["beat"], plot)
        x2 = self._beat_to_x(min(4.0, anchor["beat"] + anchor["duration"]), plot)
        cy = self._midi_to_y(eff_midi, plot)
        m_min, m_max = self._midi_range()
        row_h = max(9.0, min(16.0, plot.height() / max(12.0, (m_max - m_min)) * 0.78))
        return QRectF(x1, cy - row_h / 2.0, max(16.0, x2 - x1), row_h)

    def _is_note_in_scale(self, midi_int: int) -> tuple[bool, bool]:
        """返回 (is_active_in_scale, is_bypassed)"""
        root_idx = NOTE_NAMES.index(self._state["scale"]["root"])
        rel = (midi_int - root_idx) % 12
        active = bool(self._state["scale"]["active_notes"][rel])
        bypass = bool(self._state["scale"]["bypass_notes"][rel])
        return active, bypass

    def _snap_to_nearest_scale_midi(self, raw_midi: float) -> float:
        base = round(raw_midi)
        best = base
        best_dist = 999.0
        for cand in range(base - 6, base + 7):
            act, byp = self._is_note_in_scale(cand)
            if act and not byp:
                d = abs(cand - raw_midi)
                if d < best_dist:
                    best_dist = d
                    best = cand
        return float(best)

    # -----------------------------------------------------------------
    # 鼠标交互：左侧钢琴键切换音阶 / 画布内音块拖拽 / 滚轮 / 右键菜单
    # -----------------------------------------------------------------
    def _hit_test_anchor(self, pos: QPointF, plot: QRectF) -> int:
        if not self._show_blobs:
            return -1
        anchors = self._state.get("graph_anchors", [])
        for idx in reversed(range(len(anchors))):
            r = self._anchor_rect(anchors[idx], plot).adjusted(-3, -3, 3, 3)
            if r.contains(pos):
                return idx
        return -1

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_piano_midi = -1
        self._hover_anchor_idx = -1
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        plot = self._plot_rect()

        if event.button() == Qt.MouseButton.LeftButton:
            # 1. 顶部浮动胶囊按钮
            if self._btn_orig_rect.contains(pos):
                self._show_orig_curve = not self._show_orig_curve
                self.update()
                return
            if self._btn_blob_rect.contains(pos):
                self._show_blobs = not self._show_blobs
                self.update()
                return
            if self._btn_snap_rect.contains(pos):
                self._snap_semitone = not self._snap_semitone
                self.update()
                return

            # 2. 左侧钢琴卷帘尺点击 -> 切换音阶内音级激活状态
            if 0.0 <= pos.x() <= self._piano_w and plot.top() <= pos.y() <= plot.bottom():
                midi_clicked = int(round(self._y_to_midi(pos.y(), plot)))
                note_idx = midi_clicked % 12
                self.noteScaleStateToggled.emit(note_idx)
                return

            # 3. 修正曲线音块锚点拖拽
            a_idx = self._hit_test_anchor(pos, plot)
            if a_idx >= 0:
                anc = self._state["graph_anchors"][a_idx]
                self._dragging_anchor_idx = a_idx
                self._selected_anchor_idx = a_idx
                self._drag_start_pos = pos
                self._drag_start_midi = anc["midi"]
                self._drag_start_cents = anc.get("offset_cents", 0.0)
                self._drag_start_beat = anc["beat"]
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                return
            else:
                self._selected_anchor_idx = -1
                self.update()

        elif event.button() == Qt.MouseButton.RightButton:
            a_idx = self._hit_test_anchor(pos, plot)
            if a_idx >= 0:
                self._show_anchor_menu(a_idx, event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        plot = self._plot_rect()
        if not plot.contains(pos):
            return

        a_idx = self._hit_test_anchor(pos, plot)
        anchors = copy.deepcopy(self._state.get("graph_anchors", []))
        if a_idx >= 0:
            # 双击已有锚点：音分偏置归零并吸附到音阶
            anc = anchors[a_idx]
            anc["midi"] = self._snap_to_nearest_scale_midi(anc["midi"] + anc.get("offset_cents", 0.0) / 100.0)
            anc["offset_cents"] = 0.0
            self.anchorsChanged.emit(anchors)
        else:
            # 双击空白网格：新增一个 Melodyne 修正锚点
            beat = round(self._x_to_beat(pos.x(), plot), 2)
            raw_m = self._y_to_midi(pos.y(), plot)
            snapped_m = self._snap_to_nearest_scale_midi(raw_m) if self._snap_semitone else round(raw_m, 1)
            new_id = max([a["id"] for a in anchors], default=0) + 1
            anchors.append({
                "id": new_id,
                "beat": max(0.0, min(3.6, beat)),
                "duration": 0.45,
                "midi": snapped_m,
                "offset_cents": 0.0,
                "drift": 15.0,
            })
            self._selected_anchor_idx = len(anchors) - 1
            self.anchorsChanged.emit(anchors)

    def _show_anchor_menu(self, idx: int, global_pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #1D1D21; border: 1px solid #141416; color: #DCDCE2; font-size: 11px; }
            QMenu::item { padding: 5px 16px; }
            QMenu::item:selected { background-color: #35353D; color: #FFFFFF; }
        """)
        act_snap = menu.addAction("Snap to Scale Note (音阶吸附)")
        act_reset_ct = menu.addAction("Zero Fine Cents (音分归零)")
        menu.addSeparator()
        act_del = menu.addAction("Delete Anchor (删除锚点)")

        chosen = menu.exec(global_pos)
        if not chosen:
            return
        anchors = copy.deepcopy(self._state.get("graph_anchors", []))
        if idx >= len(anchors):
            return
        if chosen == act_snap:
            cur = anchors[idx]["midi"] + anchors[idx].get("offset_cents", 0.0) / 100.0
            anchors[idx]["midi"] = self._snap_to_nearest_scale_midi(cur)
            anchors[idx]["offset_cents"] = 0.0
        elif chosen == act_reset_ct:
            anchors[idx]["offset_cents"] = 0.0
        elif chosen == act_del:
            anchors.pop(idx)
            self._selected_anchor_idx = -1
        self.anchorsChanged.emit(anchors)

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._dragging_anchor_idx >= 0:
            anchors = copy.deepcopy(self._state.get("graph_anchors", []))
            if self._dragging_anchor_idx < len(anchors):
                anc = anchors[self._dragging_anchor_idx]
                mods = event.modifiers()

                # 时间轴位置调整 (Ctrl 锁定时间仅调音高)
                if not (mods & Qt.KeyboardModifier.ControlModifier):
                    dx = event.position().x() - self._drag_start_pos.x()
                    d_beat = (dx / plot.width()) * 4.0
                    anc["beat"] = round(max(0.0, min(4.0 - anc["duration"], self._drag_start_beat + d_beat)), 2)

                # 音高调整：Shift 进入音分 (Cents) 精细微调模式，否则半音/音阶步进
                dy = self._drag_start_pos.y() - event.position().y()
                m_min, m_max = self._midi_range()
                d_midi = (dy / plot.height()) * max(12.0, m_max - m_min)

                if mods & Qt.KeyboardModifier.ShiftModifier:
                    new_cents = max(-100.0, min(100.0, round(self._drag_start_cents + d_midi * 40.0, 0)))
                    anc["offset_cents"] = new_cents
                else:
                    target_m = max(m_min, min(m_max, self._drag_start_midi + d_midi))
                    if self._snap_semitone:
                        anc["midi"] = self._snap_to_nearest_scale_midi(target_m)
                        anc["offset_cents"] = 0.0
                    else:
                        anc["midi"] = round(target_m, 1)
                        anc["offset_cents"] = 0.0

                self.anchorsChanged.emit(anchors)
            return

        # 悬停状态检测
        if 0.0 <= event.position().x() <= self._piano_w and plot.top() <= event.position().y() <= plot.bottom():
            self._hover_piano_midi = int(round(self._y_to_midi(event.position().y(), plot)))
            self._hover_anchor_idx = -1
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self._hover_piano_midi = -1
            a_idx = self._hit_test_anchor(event.position(), plot)
            self._hover_anchor_idx = a_idx
            self.setCursor(Qt.CursorShape.OpenHandCursor if a_idx >= 0 else Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_anchor_idx >= 0:
            self._dragging_anchor_idx = -1
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._hover_anchor_idx >= 0 else Qt.CursorShape.CrossCursor)
            self.update()

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        target_a = self._hover_anchor_idx if self._hover_anchor_idx >= 0 else self._selected_anchor_idx
        if target_a >= 0:
            # 在音块上滚动滚轮：调节 Pitch Drift 音高平稳度 (0~100%)
            anchors = copy.deepcopy(self._state.get("graph_anchors", []))
            if target_a < len(anchors):
                cur_d = anchors[target_a].get("drift", 15.0)
                anchors[target_a]["drift"] = max(0.0, min(100.0, round(cur_d + delta * 5.0, 0)))
                self.anchorsChanged.emit(anchors)
                event.accept()
                return

        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            cur_r = self._state["correction"]["retune_speed"]
            step = 1.0 if cur_r < 20.0 else 5.0
            new_r = max(0.0, min(400.0, round(cur_r - delta * step, 1)))
            self.retuneScrolled.emit(new_r)
            event.accept()

    # -----------------------------------------------------------------
    # 绘制钢琴卷帘尺、音阶高亮行、双层实时音高线、手修音块锚点
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()
        st = self._state
        enabled = st["enabled"]
        m_min, m_max = self._midi_range()
        m_start = int(math.floor(m_min))
        m_end = int(math.ceil(m_max))

        # 1. 画布深色底色
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 按半音绘制水平音阶泳道 + 左侧钢琴卷帘琴键
        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        for m in range(m_start, m_end + 1):
            y_top = self._midi_to_y(m + 0.5, plot)
            y_bot = self._midi_to_y(m - 0.5, plot)
            row_rect = QRectF(plot.left(), y_top, plot.width(), y_bot - y_top)
            key_rect = QRectF(1.0, y_top, self._piano_w - 2.0, y_bot - y_top)

            note_idx = m % 12
            is_black = note_idx in BLACK_KEY_INDICES
            in_scale, is_bypass = self._is_note_in_scale(m)

            # 泳道底色（音阶内激活音符微亮，音阶外禁用音符更暗）
            if row_rect.bottom() >= plot.top() and row_rect.top() <= plot.bottom():
                clipped_row = row_rect.intersected(plot)
                if in_scale and not is_bypass and st["scale"]["enabled"]:
                    p.fillRect(clipped_row, QColor(40, 216, 240, 9) if note_idx == NOTE_NAMES.index(st["scale"]["root"]) else QColor("#212126"))
                elif is_black:
                    p.fillRect(clipped_row, QColor("#19191C"))

                # 半音分隔线 (C 音使用略亮分隔线)
                p.setPen(QPen(QColor("#36363E") if note_idx == 0 else QColor("#25252A"), 1.0))
                p.drawLine(QPointF(plot.left(), y_bot), QPointF(plot.right(), y_bot))

            # 左侧琴键绘制
            if key_rect.bottom() >= plot.top() and key_rect.top() <= plot.bottom():
                k_clip = key_rect.intersected(QRectF(1.0, plot.top(), self._piano_w - 2.0, plot.height()))
                is_hover_k = (self._hover_piano_midi == m)

                if is_hover_k:
                    k_bg = QColor("#3E424E")
                elif is_black:
                    k_bg = QColor("#141417")
                else:
                    k_bg = QColor("#2B2B31")

                p.setPen(QPen(QColor("#121214"), 0.8))
                p.setBrush(k_bg)
                p.drawRect(k_clip)

                # 音阶激活指示色点（翡翠绿=吸附目标音，琥珀金=根音，灰=屏蔽）
                if st["scale"]["enabled"]:
                    dot_col = None
                    if note_idx == NOTE_NAMES.index(st["scale"]["root"]):
                        dot_col = QColor(COLORS["scale"])
                    elif is_bypass:
                        dot_col = QColor(COLORS["cyan"])
                    elif in_scale:
                        dot_col = QColor(COLORS["retune"])

                    if dot_col is not None:
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(dot_col)
                        p.drawEllipse(QPointF(self._piano_w - 8.5, key_rect.center().y()), 2.6, 2.6)

                # C 音或悬停时显示音名标签 (如 C3, C4, A4)
                if note_idx == 0 or is_hover_k or (k_clip.height() >= 11.0 and not is_black):
                    p.setPen(QColor("#ECECF0") if (note_idx == 0 or is_hover_k) else QColor("#8A8A94"))
                    p.drawText(
                        QRectF(4.0, key_rect.top(), self._piano_w - 16.0, key_rect.height()),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        midi_to_note_name(m, True),
                    )

        # 3. 垂直节拍网格线 (Beat 1 ~ Beat 4 + 1/4 细分)
        p.setFont(QFont(MONO_FONT_FAM, 8))
        for b_sub in range(17):
            beat = b_sub * 0.25
            x = self._beat_to_x(beat, plot)
            is_major = (b_sub % 4 == 0)
            p.setPen(QPen(QColor("#33333A") if is_major else QColor("#242429"), 1.0))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            if is_major and b_sub < 16:
                p.setPen(QColor("#9A9AA2"))
                p.drawText(QRectF(x + 4, plot.bottom() + 4, 48, 15), Qt.AlignmentFlag.AlignLeft, f"Beat {b_sub // 4 + 1}")

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        # 4. 绘制实时/历史音高轨迹：原始输入虚线 + 修音后实线
        if len(self._pitch_trace) > 1:
            raw_path = QPainterPath()
            corr_path = QPainterPath()
            started_raw = False
            started_corr = False

            for beat_v, raw_m, corr_m, voiced in self._pitch_trace:
                if not voiced:
                    started_raw = False
                    started_corr = False
                    continue
                px = self._beat_to_x(beat_v, plot)
                py_raw = self._midi_to_y(raw_m, plot)
                py_corr = self._midi_to_y(corr_m if enabled else raw_m, plot)

                if not started_raw:
                    raw_path.moveTo(px, py_raw)
                    started_raw = True
                else:
                    raw_path.lineTo(px, py_raw)

                if not started_corr:
                    corr_path.moveTo(px, py_corr)
                    started_corr = True
                else:
                    corr_path.lineTo(px, py_corr)

            if self._show_orig_curve:
                p.setPen(QPen(QColor(155, 162, 182, 130), 1.3, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(raw_path)

            line_col = QColor(COLORS["cyan"]) if enabled else QColor("#6E6E76")
            if enabled and st["mode"] == "Hard Tune":
                line_col = QColor(COLORS["retune"])

            p.setPen(QPen(QColor(line_col.red(), line_col.green(), line_col.blue(), 52), 4.5))
            p.drawPath(corr_path)
            p.setPen(QPen(line_col, 2.0))
            p.drawPath(corr_path)

        # 5. Melodyne 风格可拖拽手修曲线锚点 (Note Blobs)
        if self._show_blobs:
            anchors = st.get("graph_anchors", [])
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            for idx, anc in enumerate(anchors):
                arect = self._anchor_rect(anc, plot)
                is_sel = (idx == self._selected_anchor_idx)
                is_hov = (idx in (self._hover_anchor_idx, self._dragging_anchor_idx))
                is_graph_mode = (st["mode"] == "Graph Edit")

                b_col = QColor(COLORS["scale"] if is_graph_mode else COLORS["retune"])
                if not enabled:
                    b_col = QColor("#6E6E76")

                fill_alpha = 115 if (is_sel or is_hov) else (78 if is_graph_mode else 46)
                p.setPen(QPen(QColor("#FFFFFF") if (is_sel or is_hov) else b_col, 1.4 if (is_sel or is_hov) else 1.1))
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), fill_alpha))
                p.drawRoundedRect(arect, 3.5, 3.5)

                # 内部音准中心基准细线
                cy = arect.center().y()
                p.setPen(QPen(QColor(255, 255, 255, 110), 1.0))
                p.drawLine(QPointF(arect.left() + 3, cy), QPointF(arect.right() - 3, cy))

                # 音名与音分微调标签
                eff_m = anc["midi"] + anc.get("offset_cents", 0.0) / 100.0
                ct_off = int(round(anc.get("offset_cents", 0.0)))
                lbl = f"{midi_to_note_name(eff_m, True)}" + (f" {ct_off:+d}c" if ct_off != 0 else "")
                if arect.width() >= 28.0:
                    p.setPen(QColor("#F4F6FA"))
                    p.drawText(arect.adjusted(4, 0, -2, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)

        # 6. 播放头扫描指针
        ph_x = self._beat_to_x(self._playhead_beat, plot)
        p.setPen(QPen(QColor(40, 216, 240, 140), 1.2))
        p.drawLine(QPointF(ph_x, plot.top()), QPointF(ph_x, plot.bottom()))

        # 7. 右上角开关胶囊 (ORIG / BLOBS / SNAP)
        self._btn_orig_rect = QRectF(plot.right() - 138, plot.top() + 8, 40, 18)
        self._btn_blob_rect = QRectF(plot.right() - 94, plot.top() + 8, 44, 18)
        self._btn_snap_rect = QRectF(plot.right() - 46, plot.top() + 8, 38, 18)

        for rect, label, active in (
            (self._btn_orig_rect, "ORIG", self._show_orig_curve),
            (self._btn_blob_rect, "BLOBS", self._show_blobs),
            (self._btn_snap_rect, "SNAP", self._snap_semitone),
        ):
            p.setPen(QPen(QColor("#24788C") if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 46, 56, 215) if active else QColor(21, 21, 24, 185))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#6E6E76"))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 8. 左上角实时音准状态 & 十字准星 HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 24), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(mx, plot.top()), QPointF(mx, plot.bottom()))
            p.drawLine(QPointF(plot.left(), my), QPointF(plot.right(), my))

            cur_m = self._y_to_midi(my, plot)
            cur_hz = midi_to_hz(cur_m, st["ref_a4"])
            cur_beat = self._x_to_beat(mx, plot)
            if self._dragging_anchor_idx >= 0 and self._dragging_anchor_idx < len(st["graph_anchors"]):
                anc = st["graph_anchors"][self._dragging_anchor_idx]
                eff = anc["midi"] + anc.get("offset_cents", 0.0) / 100.0
                hud_str = (
                    f"Anchor #{anc['id']}  •  {midi_to_note_name(eff, True)} ({int(round(anc.get('offset_cents', 0.0))):+d} ct)  •  "
                    f"Drift {int(anc.get('drift', 15))}%  •  Beat {anc['beat']:.2f}"
                )
            else:
                hud_str = (
                    f"{midi_to_note_name(cur_m, True)}  ({int(round(cur_hz))} Hz)  •  "
                    f"Beat {cur_beat:.2f}  •  Retune {format_ms(st['correction']['retune_speed'])}"
                )
        else:
            sc_str = f"{st['scale']['root']} {st['scale']['scale_type'].split(' ')[0]}"
            hud_str = (
                f"Key: {sc_str}  •  Retune: {format_ms(st['correction']['retune_speed'])}  •  "
                f"Flex-Tune: {int(st['correction']['flex_tune'])}%  •  Mode: {st['mode']}"
            )

        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, 355, 19)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor("#28D8F0") if self._dragging_anchor_idx >= 0 else QColor("#C6CCD8"))
        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 6. 交互式钢琴键盘音阶选择器 (InteractiveOctaveKeyboard)
# =====================================================================
class InteractiveOctaveKeyboard(QWidget):
    """一个八度 12 音琴键选择器：
       - 左键点击：激活 / 禁用该音级作为修音目标音 (翡翠绿高亮)
       - 右键点击：切换为 Bypass 穿过模式 (冰晶青边框，允许经过但不强行吸附)
    """
    notesChanged = pyqtSignal(list, list)  # active_notes[12], bypass_notes[12]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = [True, False, True, False, True, True, False, True, False, True, False, True]
        self._bypass = [False] * 12
        self._root_idx = 0
        self._module_active = True
        self._hover_idx = -1
        self.setFixedHeight(54)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def setScaleState(self, root_name: str, active_notes: list[bool], bypass_notes: list[bool]):
        self._root_idx = NOTE_NAMES.index(root_name) if root_name in NOTE_NAMES else 0
        self._active = list(active_notes)
        self._bypass = list(bypass_notes)
        self.update()

    def setModuleActive(self, active: bool):
        if self._module_active != active:
            self._module_active = active
            self.update()

    def _key_rects(self) -> tuple[dict[int, QRectF], dict[int, QRectF]]:
        w = float(self.width())
        h = float(self.height())
        white_indices = [0, 2, 4, 5, 7, 9, 11]
        white_w = w / 7.0
        whites: dict[int, QRectF] = {}
        for i, n_idx in enumerate(white_indices):
            whites[n_idx] = QRectF(i * white_w, 0.0, white_w, h)

        black_w = white_w * 0.62
        black_h = h * 0.60
        black_offsets = {
            1: 1 * white_w - black_w * 0.5,
            3: 2 * white_w - black_w * 0.5,
            6: 4 * white_w - black_w * 0.5,
            8: 5 * white_w - black_w * 0.5,
            10: 6 * white_w - black_w * 0.5,
        }
        blacks: dict[int, QRectF] = {
            n_idx: QRectF(x, 0.0, black_w, black_h) for n_idx, x in black_offsets.items()
        }
        return whites, blacks

    def _hit_note(self, pos: QPointF) -> int:
        whites, blacks = self._key_rects()
        for n_idx, rect in blacks.items():
            if rect.contains(pos):
                return n_idx
        for n_idx, rect in whites.items():
            if rect.contains(pos):
                return n_idx
        return -1

    def leaveEvent(self, event):
        self._hover_idx = -1
        self.update()

    def mouseMoveEvent(self, event):
        idx = self._hit_note(event.position())
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.update()

    def mousePressEvent(self, event):
        idx = self._hit_note(event.position())
        if idx < 0:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._active[idx] = not self._active[idx]
            if not self._active[idx]:
                self._bypass[idx] = False
            self.update()
            self.notesChanged.emit(list(self._active), list(self._bypass))
        elif event.button() == Qt.MouseButton.RightButton:
            self._bypass[idx] = not self._bypass[idx]
            if self._bypass[idx]:
                self._active[idx] = True
            self.update()
            self.notesChanged.emit(list(self._active), list(self._bypass))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        whites, blacks = self._key_rects()

        # 1. 绘制白键
        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        for n_idx, rect in whites.items():
            is_act = self._active[n_idx]
            is_byp = self._bypass[n_idx]
            is_root = (n_idx == self._root_idx)
            is_hov = (n_idx == self._hover_idx)

            if not self._module_active:
                bg = QColor("#242429")
            elif is_byp:
                bg = QColor("#18323D")
            elif is_act:
                bg = QColor("#3A3522") if is_root else QColor("#22382E")
            else:
                bg = QColor("#323238" if is_hov else "#27272C")

            border = QColor("#F0A848") if (is_root and is_act and self._module_active) else QColor("#141416")
            p.setPen(QPen(border, 1.1))
            p.setBrush(bg)
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 2.5, 2.5)

            if is_act and self._module_active:
                ind_col = QColor("#F0A848") if is_root else (QColor("#28D8F0") if is_byp else QColor("#4CD490"))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(ind_col)
                p.drawEllipse(QPointF(rect.center().x(), rect.bottom() - 15.0), 2.5, 2.5)

            p.setPen(QColor("#ECECF0") if (is_act and self._module_active) else QColor("#787882"))
            p.drawText(
                QRectF(rect.left(), rect.bottom() - 12.0, rect.width(), 11.0),
                Qt.AlignmentFlag.AlignCenter,
                NOTE_NAMES[n_idx],
            )

        # 2. 绘制黑键
        for n_idx, rect in blacks.items():
            is_act = self._active[n_idx]
            is_byp = self._bypass[n_idx]
            is_root = (n_idx == self._root_idx)
            is_hov = (n_idx == self._hover_idx)

            if not self._module_active:
                bg = QColor("#161619")
            elif is_byp:
                bg = QColor("#183846")
            elif is_act:
                bg = QColor("#463B1E") if is_root else QColor("#1E3E30")
            else:
                bg = QColor("#222227" if is_hov else "#131316")

            border = QColor("#F0A848") if (is_root and is_act and self._module_active) else (
                QColor("#4CD490") if (is_act and self._module_active) else QColor("#101012")
            )
            p.setPen(QPen(border, 1.1))
            p.setBrush(bg)
            p.drawRoundedRect(rect, 2.5, 2.5)

            if is_act and self._module_active:
                ind_col = QColor("#F0A848") if is_root else (QColor("#28D8F0") if is_byp else QColor("#4CD490"))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(ind_col)
                p.drawEllipse(QPointF(rect.center().x(), rect.bottom() - 6.5), 2.2, 2.2)


# =====================================================================
# 7. 底部三模块卡片：
#    Card 1: Retune & Expression (Retune Speed / Flex-Tune / Humanize / Natural Vibrato / Amount / Mix)
#    Card 2: Key & Scale Lock (Root / Scale / 12-Key Piano / Detune / Transpose)
#    Card 3: Formant & Vocal Care (Preserve / Throat / Breath Pass / Sibilance / Tilt / Mix)
# =====================================================================
class RetuneCorrectionCard(QFrame):
    changed = pyqtSignal(dict)
    presetModeClicked = pyqtSignal(str)  # "Transparent" | "Hard Tune"

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["retune"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 7, 10, 6)
        h_lay.setSpacing(7)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Retune & Flex-Tune")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # 快捷一键切换：隐形修正 (Natural) / 硬核电音 (Hard)
        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.btn_natural = QPushButton("Natural")
        self.btn_hard = QPushButton("Hard")
        for b in (self.btn_natural, self.btn_hard):
            b.setCheckable(True)
            b.setFixedSize(48, 19)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_lay.addWidget(b)

        self.btn_natural.clicked.connect(lambda: self.presetModeClicked.emit("Transparent"))
        self.btn_hard.clicked.connect(lambda: self.presetModeClicked.emit("Hard Tune"))
        h_lay.addWidget(seg_wrap)
        root.addWidget(header)

        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_retune = KnobParameterCell(
            "Retune Speed", 0.0, 400.0, 38.0, self._accent,
            "0 Fast", "ms", "400 Slow",
            format_ms, parse_numeric, mode="power"
        )
        self.c_flex = KnobParameterCell(
            "Flex-Tune", 0.0, 100.0, 65.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_humanize = KnobParameterCell(
            "Humanize", 0.0, 100.0, 45.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_retune, self.c_flex, self.c_humanize):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_vibrato = KnobParameterCell(
            "Nat Vibrato", -100.0, 100.0, 0.0, self._accent,
            "-100", "%", "+100",
            lambda v: f"{int(round(v)):+d}%" if abs(v) >= 0.5 else "0%",
            parse_numeric, mode="bipolar"
        )
        self.c_amt = KnobParameterCell(
            "Correction", 0.0, 100.0, 88.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 100.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_vibrato, self.c_amt, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_retune.valueChanged.connect(lambda v: self._set_field("retune_speed", round(v, 1)))
        self.c_flex.valueChanged.connect(lambda v: self._set_field("flex_tune", round(v, 0)))
        self.c_humanize.valueChanged.connect(lambda v: self._set_field("humanize", round(v, 0)))
        self.c_vibrato.valueChanged.connect(lambda v: self._set_field("natural_vibrato", round(v, 0)))
        self.c_amt.valueChanged.connect(lambda v: self._set_field("correction_amt", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        is_hard = (self._data["retune_speed"] <= 3.0 and self._data["flex_tune"] <= 5.0)

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

        self.btn_natural.setChecked(not is_hard)
        self.btn_hard.setChecked(is_hard)
        active_seg = f"""
            QPushButton {{
                background-color: #182E26;
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
        self.btn_natural.setStyleSheet(active_seg if not is_hard else inactive_seg)
        self.btn_hard.setStyleSheet(active_seg if is_hard else inactive_seg)

        self.c_retune.setValue(self._data["retune_speed"])
        self.c_flex.setValue(self._data["flex_tune"])
        self.c_humanize.setValue(self._data["humanize"])
        self.c_vibrato.setValue(self._data["natural_vibrato"])
        self.c_amt.setValue(self._data["correction_amt"])
        self.c_mix.setValue(self._data["mix"])

        for cell in (self.c_retune, self.c_flex, self.c_humanize, self.c_vibrato, self.c_amt, self.c_mix):
            cell.setModuleActive(en)

        self._updating = False

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))


class KeyScaleCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["scale"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 7, 10, 6)
        h_lay.setSpacing(7)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Key & Scale Lock")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        self.root_combo = QComboBox()
        self.root_combo.setFixedSize(46, 21)
        self.root_combo.addItems(NOTE_NAMES)
        self.root_combo.currentTextChanged.connect(self._on_root_changed)
        h_lay.addWidget(self.root_combo)

        self.scale_combo = QComboBox()
        self.scale_combo.setFixedSize(118, 21)
        self.scale_combo.addItems(list(SCALE_PATTERNS.keys()))
        self.scale_combo.currentTextChanged.connect(self._on_scale_changed)
        h_lay.addWidget(self.scale_combo)
        root.addWidget(header)

        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(10, 8, 10, 10)
        b_lay.setSpacing(8)

        # 12 音级交互式琴键区
        kb_hdr = QHBoxLayout()
        kb_lbl = QLabel("Scale Notes (Click: Lock  •  Right-Click: Bypass)")
        kb_lbl.setStyleSheet("color: #8A8A94; font-size: 9px; font-weight: 600;")
        kb_hdr.addWidget(kb_lbl)
        kb_hdr.addStretch()
        b_lay.addLayout(kb_hdr)

        self.keyboard = InteractiveOctaveKeyboard()
        self.keyboard.notesChanged.connect(self._on_notes_changed)
        b_lay.addWidget(self.keyboard)

        # 底部两旋钮：Detune (基准微调) & Scale Transpose (音阶内移调)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.c_detune = KnobParameterCell(
            "Detune", -50.0, 50.0, 0.0, self._accent,
            "-50", "ct", "+50",
            format_cents, parse_numeric, mode="bipolar"
        )
        self.c_transpose = KnobParameterCell(
            "Transpose", -12.0, 12.0, 0.0, self._accent,
            "-12", "st", "+12",
            format_st, parse_numeric, mode="bipolar"
        )
        row2.addWidget(self.c_detune, 1)
        row2.addWidget(self.c_transpose, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_detune.valueChanged.connect(lambda v: self._set_field("detune_cents", round(v, 0)))
        self.c_transpose.valueChanged.connect(lambda v: self._set_field("transpose", round(v, 0)))

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

        self.root_combo.setEnabled(en)
        self.scale_combo.setEnabled(en)
        self.root_combo.setCurrentText(self._data["root"])
        self.scale_combo.setCurrentText(self._data["scale_type"])

        self.keyboard.setScaleState(self._data["root"], self._data["active_notes"], self._data["bypass_notes"])
        self.keyboard.setModuleActive(en)

        self.c_detune.setValue(self._data["detune_cents"])
        self.c_transpose.setValue(self._data["transpose"])
        self.c_detune.setModuleActive(en)
        self.c_transpose.setModuleActive(en)

        self._updating = False

    def _rebuild_notes_from_scale(self):
        root_idx = NOTE_NAMES.index(self._data["root"])
        st_name = self._data["scale_type"]
        if st_name == "Custom (手动点选)":
            return
        intervals = set(SCALE_PATTERNS.get(st_name, [0, 2, 4, 5, 7, 9, 11]))
        self._data["active_notes"] = [((i - root_idx) % 12) in intervals for i in range(12)]
        self._data["bypass_notes"] = [False] * 12

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_root_changed(self, root_name: str):
        if self._data["root"] != root_name:
            self._data["root"] = root_name
            self._rebuild_notes_from_scale()
            self._emit()

    def _on_scale_changed(self, scale_name: str):
        if self._data["scale_type"] != scale_name:
            self._data["scale_type"] = scale_name
            self._rebuild_notes_from_scale()
            self._emit()

    def _on_notes_changed(self, active: list[bool], bypass: list[bool]):
        self._data["active_notes"] = active
        self._data["bypass_notes"] = bypass
        self._data["scale_type"] = "Custom (手动点选)"
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


class FormantVocalCareCard(QFrame):
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

        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 7, 10, 6)
        h_lay.setSpacing(7)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("Formant & Vocal Care")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        badge = QLabel("THROAT GUARD")
        badge.setStyleSheet("color: #8A8A94; font-size: 9px; font-weight: 700; letter-spacing: 0.5px;")
        h_lay.addWidget(badge)
        root.addWidget(header)

        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_preserve = KnobParameterCell(
            "Formant Guard", 0.0, 100.0, 95.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_throat = KnobParameterCell(
            "Throat Shift", -12.0, 12.0, 0.0, self._accent,
            "-12", "st", "+12",
            format_st, parse_numeric, mode="bipolar"
        )
        self.c_breath = KnobParameterCell(
            "Breath Pass", 0.0, 100.0, 85.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_preserve, self.c_throat, self.c_breath):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_sib = KnobParameterCell(
            "De-Ess Guard", 0.0, 100.0, 70.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        self.c_tilt = KnobParameterCell(
            "Air Tilt", -50.0, 50.0, 0.0, self._accent,
            "Warm", "", "Air",
            lambda v: f"{int(round(v)):+d}%" if abs(v) >= 0.5 else "0%",
            parse_numeric, mode="bipolar"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 100.0, self._accent,
            "0", "%", "100",
            format_pct, parse_numeric, mode="linear"
        )
        for c in (self.c_sib, self.c_tilt, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_preserve.valueChanged.connect(lambda v: self._set_field("preserve", round(v, 0)))
        self.c_throat.valueChanged.connect(lambda v: self._set_field("throat_shift", round(v, 1)))
        self.c_breath.valueChanged.connect(lambda v: self._set_field("breath_pass", round(v, 0)))
        self.c_sib.valueChanged.connect(lambda v: self._set_field("sibilance_guard", round(v, 0)))
        self.c_tilt.valueChanged.connect(lambda v: self._set_field("tilt", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])

        self.setStyleSheet("""
            QFrame#ModuleCard {
                background-color: #27272B;
                border: none;
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

        self.c_preserve.setValue(self._data["preserve"])
        self.c_throat.setValue(self._data["throat_shift"])
        self.c_breath.setValue(self._data["breath_pass"])
        self.c_sib.setValue(self._data["sibilance_guard"])
        self.c_tilt.setValue(self._data["tilt"])
        self.c_mix.setValue(self._data["mix"])

        for cell in (self.c_preserve, self.c_throat, self.c_breath, self.c_sib, self.c_tilt, self.c_mix):
            cell.setModuleActive(en)

        self._updating = False

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 8. 主面板：自主旗舰自动修音器 (Studio Pitch Correction)
# =====================================================================
class StudioPitchCorrectionWidget(QWidget):
    """
    人声自动修音主界面（支持 Auto-Tune 实时追踪与 Melodyne 曲线手修），提供完整信号对接后端 DSP：
      - bypassChanged(bool)
      - modeChanged(str)                   # "Transparent" | "Hard Tune" | "Graph Edit"
      - voiceTypeChanged(str)              # "Soprano (女高音)" | "Alto / Tenor (中音)" | ...
      - correctionChanged(dict)            # retune_speed, flex_tune, humanize, natural_vibrato, correction_amt, mix
      - scaleChanged(dict)                 # root, scale_type, active_notes, bypass_notes, detune_cents, transpose
      - formantChanged(dict)               # preserve, throat_shift, breath_pass, sibilance_guard, tilt, mix
      - graphAnchorsChanged(list)          # 修正曲线编辑器中的人工音块锚点数据
      - outputGainChanged(float)
      - learnKeyToggled(bool)              # 自动侦测输入人声调性按钮
      - stateChanged(dict)                 # 完整参数快照
    """
    bypassChanged = pyqtSignal(bool)
    modeChanged = pyqtSignal(str)
    voiceTypeChanged = pyqtSignal(str)
    correctionChanged = pyqtSignal(dict)
    scaleChanged = pyqtSignal(dict)
    formantChanged = pyqtSignal(dict)
    graphAnchorsChanged = pyqtSignal(list)
    outputGainChanged = pyqtSignal(float)
    learnKeyToggled = pyqtSignal(bool)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Lead Vocal", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Pitch Correction — {track_name}")
        self.resize(1100, 720)
        self.setMinimumSize(980, 650)

        self._active_bank = "A"
        self._banks = {
            "A": default_pitch_demo_transparent(),
            "B": default_pitch_hard_autotune(),
        }

        # 辅助构造函数，使预设列表结构清晰、可直接替换 StudioPitchCorrectionWidget.__init__ 中的 self._presets
        def _p(
            mode="Transparent",
            voice="Alto / Tenor (中音)",
            gain=0.0,
            retune=35.0,
            flex=60.0,
            humanize=45.0,
            vib=0.0,
            amt=90.0,
            c_mix=100.0,
            root="C",
            scale="Major (自然大调)",
            detune=0.0,
            trans=0.0,
            f_guard=95.0,
            throat=0.0,
            breath=85.0,
            deess=70.0,
            tilt=0.0,
            f_mix=100.0,
        ) -> dict:
            r_idx = NOTE_NAMES.index(root)
            intervals = set(SCALE_PATTERNS.get(scale, [0, 2, 4, 5, 7, 9, 11]))
            active = [((i - r_idx) % 12) in intervals for i in range(12)]
            st = copy.deepcopy(self._banks["A"])
            st.update({
                "enabled": True,
                "mode": mode,
                "voice_type": voice,
                "output_gain": gain,
                "correction": {
                    "enabled": True,
                    "retune_speed": retune,
                    "flex_tune": flex,
                    "humanize": humanize,
                    "natural_vibrato": vib,
                    "correction_amt": amt,
                    "mix": c_mix,
                },
                "scale": {
                    "enabled": True,
                    "root": root,
                    "scale_type": scale,
                    "active_notes": active,
                    "bypass_notes": [False] * 12,
                    "detune_cents": detune,
                    "transpose": trans,
                },
                "formant": {
                    "enabled": True,
                    "preserve": f_guard,
                    "throat_shift": throat,
                    "breath_pass": breath,
                    "sibilance_guard": deess,
                    "tilt": tilt,
                    "mix": f_mix,
                },
            })
            return st

        self._presets = {
            # ================= 1. 隐形自然修音（广播级母带/录音棚人声） =================
            "01 · Transparent Vocal Polish (自然隐形修音)":
                _p(retune=38.0, flex=65.0, humanize=45.0, amt=88.0),

            "02 · Invisible Acoustic / Folk (民谣原声无痕微修)":
                _p(retune=65.0, flex=80.0, humanize=70.0, amt=75.0, breath=95.0, deess=75.0),

            "03 · Intimate Ballad / Emotional (抒情慢歌情感保留)":
                _p(retune=48.0, flex=75.0, humanize=65.0, vib=8.0, amt=84.0, tilt=-6.0),

            "04 · Musical Theater / Belting (音乐剧高音张力保护)":
                _p(voice="Soprano (女高音)", retune=32.0, flex=68.0, humanize=50.0, amt=90.0, f_guard=100.0, deess=82.0),

            "05 · Classical / Opera Vibrato Keep (美声大颤音保留)":
                _p(voice="Soprano (女高音)", retune=85.0, flex=92.0, humanize=85.0, vib=15.0, amt=72.0, f_guard=100.0),

            "06 · Pitch Drift Tamer (长音走音渐飘稳音器)":
                _p(retune=42.0, flex=55.0, humanize=18.0, vib=-20.0, amt=92.0),

            # ================= 2. 现代流行 / 商业单曲主唱 =================
            "07 · Modern Pop Lead (现代流行主唱标准)":
                _p(retune=18.0, flex=42.0, humanize=28.0, amt=95.0, tilt=8.0),

            "08 · K-Pop / J-Pop Bright Idol (日韩流行清亮贴耳)":
                _p(voice="Soprano (女高音)", retune=12.0, flex=30.0, humanize=18.0, amt=98.0, throat=0.4, tilt=16.0, deess=85.0),

            "09 · Mandopop Lyric Vocal (华语流行细腻人声)":
                _p(retune=24.0, flex=52.0, humanize=36.0, amt=92.0, breath=92.0, tilt=6.0),

            "10 · Rock / Punk Grit Vocal (摇滚撕裂音防破保护)":
                _p(retune=28.0, flex=58.0, humanize=40.0, amt=85.0, f_guard=98.0, breath=90.0, deess=60.0),

            "11 · Country Twang Natural (乡村音乐滑音质感)":
                _p(root="G", scale="Major (自然大调)", retune=34.0, flex=72.0, humanize=55.0, amt=86.0),

            # ================= 3. R&B / 灵魂乐 / 爵士转音 =================
            "12 · R&B Melisma Smooth (R&B 复杂转音顺滑保留)":
                _p(root="C", scale="Minor Pentatonic", retune=26.0, flex=82.0, humanize=60.0, vib=10.0, amt=85.0),

            "13 · Neo-Soul Laid-Back (新灵魂乐慵懒微醺)":
                _p(root="F", scale="Dorian", retune=44.0, flex=78.0, humanize=68.0, vib=12.0, amt=80.0, tilt=-8.0),

            "14 · Jazz & Blues Blue-Note (爵士蓝调微分音宽容)":
                _p(root="A", scale="Blues", retune=55.0, flex=86.0, humanize=75.0, amt=76.0),

            # ================= 4. 经典 Auto-Tune 电音 / 说唱 / 电子 =================
            "15 · Zero-Latency Hard Tune (经典零延迟硬核电音)":
                _p(mode="Hard Tune", retune=0.0, flex=0.0, humanize=0.0, amt=100.0, root="A", scale="Minor (自然小调)"),

            "16 · Melodic Trap / Rap Tune (旋律说唱流行电音)":
                _p(mode="Hard Tune", retune=4.0, flex=14.0, humanize=8.0, amt=100.0, root="C#", scale="Minor (自然小调)", tilt=10.0),

            "17 · Hyperpop / Glitch Vocal (超流行高音共振峰电音)":
                _p(mode="Hard Tune", retune=0.0, flex=0.0, humanize=0.0, amt=100.0, root="E", scale="Major (自然大调)", f_guard=60.0, throat=2.4, tilt=22.0),

            "18 · EDM / Future Bass Chop (电音切片强吸附)":
                _p(mode="Hard Tune", retune=0.0, flex=0.0, humanize=0.0, vib=-60.0, amt=100.0, root="F", scale="Major Pentatonic", throat=1.2),

            "19 · Cher / T-Pain Classic Step (复古阶梯电音标志)":
                _p(mode="Hard Tune", retune=0.0, flex=0.0, humanize=0.0, vib=-100.0, amt=100.0, root="G", scale="Major (自然大调)"),

            # ================= 5. 和声叠录 / 合唱团 / 音色微塑 =================
            "20 · Tight Backing Doubles (副歌叠录和声紧致对齐)":
                _p(retune=10.0, flex=18.0, humanize=12.0, vib=-35.0, amt=98.0, deess=90.0, tilt=-12.0),

            "21 · Choir / Ensemble Unison (大合唱音准统一去颤)":
                _p(retune=16.0, flex=25.0, humanize=15.0, vib=-55.0, amt=95.0, breath=60.0, deess=85.0),

            "22 · +1 Octave Formant-Safe High (高八度伪和声生成)":
                _p(retune=14.0, flex=35.0, humanize=25.0, amt=96.0, trans=12.0, f_guard=100.0, throat=-0.8, tilt=10.0),

            "23 · -1 Octave Deep Sub Vocal (低八度厚重男声垫底)":
                _p(voice="Low Male (男低音)", retune=18.0, flex=30.0, humanize=20.0, amt=95.0, trans=-12.0, f_guard=100.0, throat=1.0, tilt=-18.0),

            "24 · 432Hz Healing / Meditation (432Hz 自然律调音基准)":
                _p(retune=40.0, flex=65.0, humanize=50.0, amt=88.0, detune=-32.0, tilt=-6.0),

            # ================= 6. 特殊音域与 Melodyne 曲线手修 =================
            "25 · Deep Baritone / Bass Vocal (男低音防八度跳变)":
                _p(voice="Low Male (男低音)", retune=36.0, flex=62.0, humanize=48.0, amt=88.0, f_guard=96.0),

            "26 · Kids / Youth Choir (童声清澈音准修正)":
                _p(voice="Soprano (女高音)", retune=22.0, flex=48.0, humanize=35.0, amt=92.0, f_guard=100.0, tilt=8.0),

            "27 · Melodyne Graph Precision (曲线锚点手工精修)":
                _p(mode="Graph Edit", retune=12.0, flex=20.0, humanize=15.0, amt=100.0),
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 演示实时音高曲线动画（接入真实后端 update_pitch_frame 后自动停止）
        self._anim_beat = 0.0
        self._trace_buffer: list[tuple[float, float, float, bool]] = []
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_pitch)
        self._demo_timer.start(35)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            PitchGraphEditorWidget, PitchCentsMeter, MasterOutputStrip,
            CompactLevelMeter, InteractiveOctaveKeyboard {{
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
                color: #E4E4EA;
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
                font-size: 10.5px;
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
        # 1. 顶部主控栏（电源 + 预设 + A/B 对比 + 引擎模式 + 人声范围 + Learn Key + Reset）
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(38)
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True, COLORS["cyan"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"PITCH CORRECTION  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(8)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存为新预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedWidth(210)
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

        tb_lay.addSpacing(10)

        # 修音引擎模式切换 (Transparent / Hard Tune / Graph Edit)
        self.mode_combo = QComboBox()
        self.mode_combo.setFixedWidth(102)
        self.mode_combo.addItems(["Transparent", "Hard Tune", "Graph Edit"])
        self.mode_combo.setToolTip("选择修音引擎算法：自然隐形 / 零延迟电音 / 曲线锚点手修")
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        tb_lay.addWidget(self.mode_combo)

        # 人声音域范围 (Voice Type)
        self.voice_combo = QComboBox()
        self.voice_combo.setFixedWidth(128)
        self.voice_combo.addItems(list(VOICE_TYPES.keys()))
        self.voice_combo.setToolTip("设置人声音域检测范围，防止八度误判")
        self.voice_combo.currentTextChanged.connect(self._on_voice_type_changed)
        tb_lay.addWidget(self.voice_combo)

        # 自动侦测调性按钮
        self.learn_key_btn = QPushButton("Learn Key")
        self.learn_key_btn.setCheckable(True)
        self.learn_key_btn.setFixedSize(64, 22)
        self.learn_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.learn_key_btn.setToolTip("实时监听输入人声并自动匹配调性音阶")
        self.learn_key_btn.toggled.connect(self._on_learn_key_toggled)
        tb_lay.addWidget(self.learn_key_btn)

        tb_lay.addStretch()

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(46, 22)
        reset_btn.clicked.connect(self.reset_pitch_correction)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心交互区：Input 电平 | 琴键音阶尺 & 修正曲线编辑器 | Cents 音准表 | Output 推子
        # -------------------------------------------------------------
        graph_row = QHBoxLayout()
        graph_row.setContentsMargins(0, 0, 0, 0)
        graph_row.setSpacing(0)

        # 左侧 Input 电平表列
        in_col_frame = QFrame()
        in_col_frame.setObjectName("InMeterCol")
        in_col_frame.setStyleSheet("QFrame#InMeterCol { background-color: #222226; border-right: 1px solid #141416; }")
        in_col = QVBoxLayout(in_col_frame)
        in_col.setContentsMargins(3, 6, 3, 6)
        in_col.setSpacing(2)
        in_lbl = QLabel("IN")
        in_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        in_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;")
        in_col.addWidget(in_lbl)

        self.in_meter = CompactLevelMeter()
        in_col.addWidget(self.in_meter, 1)
        graph_row.addWidget(in_col_frame)

        # 中央交互式音高曲线与修正锚点编辑器
        self.pitch_graph = PitchGraphEditorWidget()
        self.pitch_graph.noteScaleStateToggled.connect(self._on_graph_note_toggled)
        self.pitch_graph.anchorsChanged.connect(self._on_graph_anchors_changed)
        self.pitch_graph.retuneScrolled.connect(self._on_graph_retune_scrolled)
        graph_row.addWidget(self.pitch_graph, 1)

        # 右侧音准偏差表列 (CENTS DEVIATION)
        cents_col_frame = QFrame()
        cents_col_frame.setObjectName("CentsCol")
        cents_col_frame.setStyleSheet("QFrame#CentsCol { background-color: #222226; border-left: 1px solid #141416; }")
        cents_col = QVBoxLayout(cents_col_frame)
        cents_col.setContentsMargins(3, 6, 3, 6)
        cents_col.setSpacing(2)
        cents_lbl = QLabel("CENTS")
        cents_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cents_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;")
        cents_col.addWidget(cents_lbl)

        self.cents_meter = PitchCentsMeter()
        cents_col.addWidget(self.cents_meter, 1)
        graph_row.addWidget(cents_col_frame)

        # 最右侧 Output 推子与电平表列
        meter_col_frame = QFrame()
        meter_col_frame.setObjectName("MeterCol")
        meter_col_frame.setStyleSheet("QFrame#MeterCol { background-color: #222226; border-left: 1px solid #141416; }")
        out_col = QVBoxLayout(meter_col_frame)
        out_col.setContentsMargins(4, 6, 4, 6)
        out_col.setSpacing(2)
        out_lbl = QLabel("OUT")
        out_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        out_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;")
        out_col.addWidget(out_lbl)

        self.master_strip = MasterOutputStrip()
        self.master_strip.gainChanged.connect(self._on_output_gain_changed)
        out_col.addWidget(self.master_strip, 1)
        graph_row.addWidget(meter_col_frame)

        main_lay.addLayout(graph_row, 5)

        # -------------------------------------------------------------
        # 3. 底部三模块控制卡片 (Retune & Flex-Tune | Key & Scale | Formant & Vocal Care)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        st = self._current_state()
        self.retune_card = RetuneCorrectionCard(st["correction"])
        self.retune_card.changed.connect(self._on_correction_changed)
        self.retune_card.presetModeClicked.connect(self._apply_quick_mode)

        self.scale_card = KeyScaleCard(st["scale"])
        self.scale_card.changed.connect(self._on_scale_changed)

        self.formant_card = FormantVocalCareCard(st["formant"])
        self.formant_card.changed.connect(self._on_formant_changed)

        cards_row.addWidget(self.retune_card, 1)
        cards_row.addWidget(self.scale_card, 1)
        cards_row.addWidget(self.formant_card, 1)

        main_lay.addLayout(cards_row, 4)

    # -----------------------------------------------------------------
    # 状态管理与双向信号联动
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_top_bar_styles(self):
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
            QPushButton:hover { background-color: #3C3C44; color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_ab if self._active_bank == "A" else inactive_ab)
        self.btn_b.setStyleSheet(active_ab if self._active_bank == "B" else inactive_ab)
        self.btn_copy_ab.setStyleSheet(inactive_ab)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

        self.learn_key_btn.setStyleSheet("""
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
                background-color: #3A2E18;
                color: #F0A848;
                border: 1px solid #F0A848;
                font-weight: 700;
            }
        """)

    def _refresh_all_views(self):
        st = self._current_state()
        self._update_top_bar_styles()
        self.power_switch.setChecked(st["enabled"])

        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentText(st["mode"])
        self.mode_combo.blockSignals(False)

        self.voice_combo.blockSignals(True)
        self.voice_combo.setCurrentText(st["voice_type"])
        self.voice_combo.blockSignals(False)

        self.master_strip.setGain(st["output_gain"])
        self.cents_meter.setActive(st["enabled"])

        self.retune_card.syncFromData(st["correction"])
        self.scale_card.syncFromData(st["scale"])
        self.formant_card.syncFromData(st["formant"])
        self.pitch_graph.setState(st)

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
        self.cents_meter.setActive(enabled)
        self.pitch_graph.setState(st)
        self.bypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_mode_changed(self, mode: str):
        st = self._current_state()
        st["mode"] = mode
        if mode == "Hard Tune":
            st["correction"]["retune_speed"] = 0.0
            st["correction"]["flex_tune"] = 0.0
            st["correction"]["humanize"] = 0.0
            self.retune_card.syncFromData(st["correction"])
            self.correctionChanged.emit(copy.deepcopy(st["correction"]))
        elif mode == "Transparent" and st["correction"]["retune_speed"] < 5.0:
            st["correction"]["retune_speed"] = 38.0
            st["correction"]["flex_tune"] = 65.0
            st["correction"]["humanize"] = 45.0
            self.retune_card.syncFromData(st["correction"])
            self.correctionChanged.emit(copy.deepcopy(st["correction"]))
        self.pitch_graph.setState(st)
        self.modeChanged.emit(mode)
        self.stateChanged.emit(self.get_state())

    def _apply_quick_mode(self, mode: str):
        self.mode_combo.setCurrentText(mode)

    def _on_voice_type_changed(self, voice_type: str):
        st = self._current_state()
        st["voice_type"] = voice_type
        self.pitch_graph.setState(st)
        self.voiceTypeChanged.emit(voice_type)
        self.stateChanged.emit(self.get_state())

    def _on_learn_key_toggled(self, checked: bool):
        self.learnKeyToggled.emit(checked)

    def _on_output_gain_changed(self, gain: float):
        st = self._current_state()
        st["output_gain"] = gain
        self.outputGainChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _on_correction_changed(self, data: dict):
        st = self._current_state()
        st["correction"] = data
        self.pitch_graph.setState(st)
        self.correctionChanged.emit(copy.deepcopy(data))
        self.stateChanged.emit(self.get_state())

    def _on_scale_changed(self, data: dict):
        st = self._current_state()
        st["scale"] = data
        self.pitch_graph.setState(st)
        self.scaleChanged.emit(copy.deepcopy(data))
        self.stateChanged.emit(self.get_state())

    def _on_formant_changed(self, data: dict):
        st = self._current_state()
        st["formant"] = data
        self.pitch_graph.setState(st)
        self.formantChanged.emit(copy.deepcopy(data))
        self.stateChanged.emit(self.get_state())

    def _on_graph_note_toggled(self, note_idx: int):
        st = self._current_state()
        sc = st["scale"]
        sc["enabled"] = True
        sc["active_notes"][note_idx] = not sc["active_notes"][note_idx]
        if not sc["active_notes"][note_idx]:
            sc["bypass_notes"][note_idx] = False
        sc["scale_type"] = "Custom (手动点选)"
        self.scale_card.syncFromData(sc)
        self.pitch_graph.setState(st)
        self.scaleChanged.emit(copy.deepcopy(sc))
        self.stateChanged.emit(self.get_state())

    def _on_graph_anchors_changed(self, anchors: list):
        st = self._current_state()
        st["graph_anchors"] = anchors
        self.pitch_graph.setState(st)
        self.graphAnchorsChanged.emit(copy.deepcopy(anchors))
        self.stateChanged.emit(self.get_state())

    def _on_graph_retune_scrolled(self, new_retune: float):
        st = self._current_state()
        st["correction"]["retune_speed"] = new_retune
        self.retune_card.syncFromData(st["correction"])
        self.pitch_graph.setState(st)
        self.correctionChanged.emit(copy.deepcopy(st["correction"]))
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
        name, ok = QInputDialog.getText(self, "保存修音预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 演示实时人声逐音修音动画（接入真实后端 DSP 数据后自动停止）
    # -----------------------------------------------------------------
    def _tick_demo_pitch(self):
        st = self._current_state()
        self._anim_beat = (self._anim_beat + 0.032) % 4.0
        if self._anim_beat < 0.035:
            self._trace_buffer.clear()

        b = self._anim_beat
        # 模拟 4 拍人声旋律句：滑音进入 + 自然颤音 + 轻微音准偏低(-22 cents)
        voiced = True
        if b < 0.30:
            voiced = False
            raw_midi = 64.0
        elif b < 1.15:
            glide = -0.65 * math.exp(-((b - 0.30) * 9.0))
            vib = 0.24 * math.sin((b - 0.30) * 28.0)
            raw_midi = 64.0 - 0.22 + glide + vib
        elif b < 2.00:
            glide = -0.85 * math.exp(-((b - 1.15) * 8.0))
            vib = 0.28 * math.sin((b - 1.15) * 30.0)
            raw_midi = 67.0 + 0.18 + glide + vib
        elif b < 2.90:
            glide = -0.55 * math.exp(-((b - 2.00) * 10.0))
            vib = 0.32 * math.sin((b - 2.00) * 29.0)
            raw_midi = 69.0 - 0.26 + glide + vib
        elif b < 3.85:
            glide = 0.45 * math.exp(-((b - 2.90) * 9.0))
            vib = 0.25 * math.sin((b - 2.90) * 27.0)
            raw_midi = 67.0 - 0.16 + glide + vib
        else:
            voiced = False
            raw_midi = 67.0

        # 计算目标吸附音高（受音阶锁、Retune Speed 与 Flex-Tune 影响）
        target_midi = self.pitch_graph._snap_to_nearest_scale_midi(raw_midi) if st["scale"]["enabled"] else round(raw_midi)
        if st["mode"] == "Graph Edit":
            for anc in st.get("graph_anchors", []):
                if anc["beat"] <= b <= anc["beat"] + anc["duration"]:
                    target_midi = anc["midi"] + anc.get("offset_cents", 0.0) / 100.0
                    break

        target_midi += st["scale"]["transpose"] + st["scale"]["detune_cents"] / 100.0

        corr = st["correction"]
        if st["enabled"] and corr["enabled"] and voiced:
            speed_factor = max(0.0, min(1.0, 1.0 - (corr["retune_speed"] / 260.0)))
            flex_allow = (corr["flex_tune"] / 100.0) * 0.45
            amt = (corr["correction_amt"] / 100.0) * (corr["mix"] / 100.0)
            diff = target_midi - raw_midi
            if abs(diff) < flex_allow:
                corrected_midi = raw_midi + diff * amt * speed_factor * 0.35
            else:
                corrected_midi = raw_midi + diff * amt * speed_factor
        else:
            corrected_midi = raw_midi

        self._trace_buffer.append((b, raw_midi, corrected_midi, voiced))
        self.pitch_graph.updatePitchTrace(self._trace_buffer, b)

        det_cents = (raw_midi - round(raw_midi)) * 100.0 if voiced else 0.0
        cor_cents = (corrected_midi - raw_midi) * 100.0 if voiced else 0.0
        self.cents_meter.setPitchReadout(midi_to_note_name(raw_midi, True), det_cents, cor_cents)

        lvl = (-19.0 + 4.0 * math.sin(b * 7.0)) if voiced else -54.0
        self.in_meter.setLevel(lvl)
        self.master_strip.setMeterLevel(min(-0.5, lvl + st["output_gain"]))

    # -----------------------------------------------------------------
    # 公开 API：供后端音频引擎实时喂入数据与工程序列化
    # -----------------------------------------------------------------
    def reset_pitch_correction(self):
        self._banks[self._active_bank] = default_pitch_demo_transparent()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_pitch_frame(
        self,
        trace_points: list[tuple[float, float, float, bool]],
        playhead_beat: float,
        current_note: str,
        detected_cents: float,
        correction_cents: float,
        input_db: float,
        output_db: float,
    ):
        """
        接入真实音频引擎逐音检测与修音结果 (建议 30~60Hz 调用)：
          - trace_points: [(beat_0_to_4, raw_midi, corrected_midi, is_voiced), ...]
          - playhead_beat: 当前播放头在 4 拍循环内的位置 (0.0 ~ 4.0)
          - current_note: 当前检测到的音名 (如 "A4")
          - detected_cents: 输入音准偏差 (-50.0 ~ +50.0 cents)
          - correction_cents: 引擎施加的音高修正量 (-50.0 ~ +50.0 cents)
          - input_db / output_db: 输入输出峰值电平 (-60.0 ~ 0.0 dBFS)
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.pitch_graph.updatePitchTrace(trace_points, playhead_beat)
        self.cents_meter.setPitchReadout(current_note, detected_cents, correction_cents)
        self.in_meter.setLevel(input_db)
        self.master_strip.setMeterLevel(output_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioPitchCorrectionWidget("Lead Vocal")
    win.show()
    sys.exit(app.exec())