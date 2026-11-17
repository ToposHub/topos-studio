from __future__ import annotations

import sys
import math
import copy
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QFrame, QLineEdit, QSizePolicy, QInputDialog, QMenu
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont, QPolygonF
)


# =====================================================================
# 1. 全局色彩系统与声学常量（统一 Fairlight 深色哑光视觉规范）
# =====================================================================
FREQ_MIN = 20.0
FREQ_MAX = 20000.0

UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

# 6 频段专属低眩光音频色板
BAND_PALETTE = [
    "#F26D6D",  # Band 1: 珊瑚红 (Low Cut / Sub)
    "#F0A848",  # Band 2: 琥珀金 (Low / Mud)
    "#4CD490",  # Band 3: 翡翠绿 (Low-Mid)
    "#28D8F0",  # Band 4: 冰晶青 (High-Mid / Vocal Duck)
    "#6495FA",  # Band 5: 钴蓝   (Presence / De-Ess)
    "#B67CF8",  # Band 6: 紫罗兰 (High Cut / Air)
]

RANGE_PRESETS = {
    "L":  (20.0, 400.0, "20", "400"),
    "ML": (100.0, 1500.0, "100", "1.5K"),
    "MH": (450.0, 8000.0, "450", "8.0K"),
    "H":  (1400.0, 20000.0, "1.4K", "20K"),
}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def freq_to_note(hz: float) -> str:
    if hz <= 0:
        return ""
    midi = round(69 + 12 * math.log2(hz / 440.0))
    return f"{NOTE_NAMES[midi % 12]}{(midi // 12) - 1}"


def format_freq(hz: float) -> str:
    if hz >= 10000.0:
        return f"{hz / 1000.0:.1f}K"
    if hz >= 1000.0:
        return f"{hz / 1000.0:.2f}K"
    return f"{int(round(hz))}"


def parse_freq(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("HZ", "").strip()
    try:
        if s.endswith("K"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def format_ms(val: float) -> str:
    if val < 1.0:
        return f"{val:.2f}"
    if val < 10.0:
        return f"{val:.1f}"
    return f"{int(round(val))}"


def format_db_signed(val: float) -> str:
    if abs(val) < 0.05:
        return "0.0"
    return f"{val:+.1f}"


def auto_range_key(freq: float) -> str:
    if freq < 350.0:
        return "L"
    elif freq < 1400.0:
        return "ML"
    elif freq < 7500.0:
        return "MH"
    return "H"


def _make_dyn_band(
    index: int,
    enabled: bool = True,
    filter_type: str = "Bell",
    freq: float = 1000.0,
    gain: float = 0.0,
    q: float = 1.4,
    slope: int = 12,
    dyn_enabled: bool = False,
    dyn_range: float = -4.0,     # -24.0 ~ +18.0 dB (负值=超阈值压缩衰减, 正值=超阈值向上扩展)
    threshold: float = -22.0,    # -60.0 ~ 0.0 dBFS
    attack: float = 12.0,        # 0.1 ~ 200.0 ms
    release: float = 110.0,      # 10.0 ~ 2000.0 ms
    auto_timing: bool = False,   # 自动程序相关 Attack/Release
    trigger_mode: str = "INT",   # "INT" (本频段内触发) | "EXT" (外部侧链触发)
) -> dict:
    return {
        "index": index,
        "enabled": enabled,
        "solo": False,
        "filter_type": filter_type,
        "range_key": auto_range_key(freq),
        "freq": freq,
        "gain": gain,
        "q": q,
        "slope": slope,
        "dyn_enabled": dyn_enabled,
        "dyn_range": dyn_range,
        "threshold": threshold,
        "attack": attack,
        "release": release,
        "auto_timing": auto_timing,
        "trigger_mode": trigger_mode,
    }


def default_dynamic_eq_demo() -> dict:
    """默认演示配置：伴奏低频防浑浊(内触发) + 人声频段侧链避让(外触发) + 齿音动态抑制"""
    return {
        "enabled": True,
        "phase_mode": "Zero Latency",     # "Zero Latency" | "Natural Phase" | "Linear Phase"
        "ext_sc_enabled": True,           # 全局外部侧链总线使能
        "ext_sc_source": "Bus 1-2 (Vocal)",
        "sc_audition": False,             # 侧链信号独奏监听
        "master_gain": 0.0,
        "bands": [
            _make_dyn_band(1, True,  "HPF",        75.0,   0.0,  1.0, 18, False,  0.0, -24.0, 15.0, 120.0, False, "INT"),
            _make_dyn_band(2, True,  "Bell",       165.0,  0.0,  2.2, 12, True,  -5.5, -20.0, 18.0, 140.0, False, "INT"),
            _make_dyn_band(3, True,  "Bell",       420.0, -1.5,  1.8, 12, True,  -3.5, -24.0, 12.0, 110.0, True,  "INT"),
            _make_dyn_band(4, True,  "Bell",       2650.0, 0.0,  1.6, 12, True,  -6.5, -26.0, 4.0,  85.0,  False, "EXT"),
            _make_dyn_band(5, True,  "High Shelf", 6800.0, 1.5,  1.2, 12, True,  -4.5, -22.0, 1.5,  60.0,  False, "INT"),
            _make_dyn_band(6, False, "LPF",        19000.0,0.0,  1.0, 12, False,  0.0, -24.0, 10.0, 100.0, False, "INT"),
        ],
    }


def default_dynamic_eq_flat() -> dict:
    return {
        "enabled": True,
        "phase_mode": "Zero Latency",
        "ext_sc_enabled": False,
        "ext_sc_source": "Bus 1-2 (Vocal)",
        "sc_audition": False,
        "master_gain": 0.0,
        "bands": [
            _make_dyn_band(1, False, "HPF",        60.0,    0.0, 1.0, 12, False, -4.0, -20.0, 15.0, 120.0, False, "INT"),
            _make_dyn_band(2, True,  "Low Shelf",  110.0,   0.0, 1.1, 12, False, -4.0, -20.0, 15.0, 120.0, False, "INT"),
            _make_dyn_band(3, True,  "Bell",       380.0,   0.0, 1.4, 12, False, -4.0, -20.0, 10.0, 100.0, False, "INT"),
            _make_dyn_band(4, True,  "Bell",       1800.0,  0.0, 1.4, 12, False, -4.0, -20.0, 8.0,  90.0,  False, "INT"),
            _make_dyn_band(5, True,  "High Shelf", 6500.0,  0.0, 1.1, 12, False, -4.0, -20.0, 5.0,  80.0,  False, "INT"),
            _make_dyn_band(6, False, "LPF",        19500.0, 0.0, 1.0, 12, False, -4.0, -20.0, 10.0, 100.0, False, "INT"),
        ],
    }


# =====================================================================
# 2. 原创极简声学曲线图标绘制
# =====================================================================
def draw_filter_glyph(painter: QPainter, rect: QRectF, ftype: str, color: QColor):
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    cx = rect.center().x()
    cy = rect.center().y()
    w = min(rect.width() * 0.72, 22.0)
    h = min(rect.height() * 0.54, 9.5)
    left = cx - w / 2.0
    right = cx + w / 2.0
    top = cy - h / 2.0
    bot = cy + h / 2.0

    path = QPainterPath()
    if ftype == "HPF":
        path.moveTo(left + 1, bot)
        path.cubicTo(cx - 1.5, bot, cx - 2.5, top, cx + 2.5, top)
        path.lineTo(right - 1, top)
    elif ftype == "LPF":
        path.moveTo(left + 1, top)
        path.lineTo(cx - 2.5, top)
        path.cubicTo(cx + 2.5, top, cx + 1.5, bot, right - 1, bot)
    elif ftype == "Low Shelf":
        path.moveTo(left, top)
        path.lineTo(cx - 3.0, top)
        path.cubicTo(cx, top, cx, bot, cx + 3.0, bot)
        path.lineTo(right, bot)
    elif ftype == "High Shelf":
        path.moveTo(left, bot)
        path.lineTo(cx - 3.0, bot)
        path.cubicTo(cx, bot, cx, top, cx + 3.0, top)
        path.lineTo(right, top)
    elif ftype == "Bell":
        path.moveTo(left, bot - 1.0)
        path.cubicTo(cx - 4.5, bot - 1.0, cx - 3.5, top, cx, top)
        path.cubicTo(cx + 3.5, top, cx + 4.5, bot - 1.0, right, bot - 1.0)
    elif ftype == "Notch":
        path.moveTo(left, top + 0.5)
        path.lineTo(cx - 4.0, top + 0.5)
        path.cubicTo(cx - 1.5, top + 0.5, cx - 1.2, bot, cx, bot)
        path.cubicTo(cx + 1.2, bot, cx + 1.5, top + 0.5, cx + 4.0, top + 0.5)
        path.lineTo(right, top + 0.5)

    painter.drawPath(path)
    painter.restore()


# =====================================================================
# 3. 基础定制控件：电源开关 / 滤波器选择器 / 可编辑数字胶囊 / 阳极铝旋钮
# =====================================================================
class PowerToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True, accent_color: str = "#28D8F0", parent=None):
        super().__init__(parent)
        self._checked = checked
        self._accent = QColor(accent_color)
        self.setFixedSize(32, 16)
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
        p.drawRoundedRect(rect, 8.0, 8.0)

        r = 4.8
        cy = self.height() / 2.0
        cx = (self.width() - 8.2) if self._checked else 8.2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob_color)
        p.drawEllipse(QPointF(cx, cy), r, r)


class FilterShapeSelector(QPushButton):
    shapeChanged = pyqtSignal(str)

    def __init__(self, options: list[str], current: str, accent_color: str, parent=None):
        super().__init__(parent)
        self._options = options
        self._current = current
        self._accent = QColor(accent_color)
        self._active = True
        self.setFixedSize(48, 21)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._open_menu)

    def setCurrentShape(self, shape: str):
        if shape in self._options and self._current != shape:
            self._current = shape
            self.update()

    def setBandActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def _open_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1D1D21;
                border: 1px solid #141416;
                border-radius: 4px;
                padding: 4px;
                color: #DCDCE2;
                font-size: 11px;
            }
            QMenu::item { padding: 5px 16px; border-radius: 3px; }
            QMenu::item:selected { background-color: #35353D; color: #FFFFFF; }
        """)
        for opt in self._options:
            act = menu.addAction(opt)
            act.triggered.connect(lambda checked=False, o=opt: self._select(o))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _select(self, shape: str):
        if self._current != shape:
            self._current = shape
            self.update()
            self.shapeChanged.emit(shape)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        bg = QColor("#151518") if self._active else QColor("#1B1B1E")
        p.setPen(QPen(QColor("#101012"), 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 3.0, 3.0)

        glyph_color = self._accent if self._active else QColor("#6E6E76")
        glyph_rect = QRectF(3, 2, self.width() - 15, self.height() - 4)
        draw_filter_glyph(p, glyph_rect, self._current, glyph_color)

        ax = self.width() - 8.0
        ay = self.height() / 2.0 - 1.2
        p.setPen(QPen(QColor("#9A9AA2"), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ax - 2.2, ay), QPointF(ax, ay + 2.4))
        p.drawLine(QPointF(ax, ay + 2.4), QPointF(ax + 2.2, ay))


class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 52, parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._active = True
        self.setFixedSize(width, 19)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(0, 0, width, 19)
        self._editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._editor.setStyleSheet(f"""
            QLineEdit {{
                background-color: #151518;
                color: {accent_color};
                border: 1px solid {accent_color};
                border-radius: 3px;
                font-family: {MONO_FONT_CSS};
                font-size: 10px;
                font-weight: 700;
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

    def setBandActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            clean = self._text.replace("+", "").replace("dB", "").replace("ms", "").replace("Hz", "").strip()
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
        p.drawRoundedRect(rect, 3.0, 3.0)

        p.setPen(QColor("#ECECF0") if self._active else QColor("#6E6E76"))
        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


class ProRotaryKnob(QWidget):
    """支持 linear / log / asymmetric-bipolar (0点精准锁定12点钟方向) 的拉丝阳极铝旋钮"""
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

        self.setFixedSize(32, 32)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setValue(self, val: float, emit: bool = False):
        clamped = max(self._min, min(self._max, val))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def setRange(self, minimum: float, maximum: float):
        self._min = minimum
        self._max = maximum
        self._value = max(self._min, min(self._max, self._value))
        self.update()

    def setBandActive(self, active: bool):
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
        if self._mode == "bipolar" and self._min < 0.0 < self._max:
            # 0.0 始终严格映射到 0.5 (12点钟正上方)，即使正负量程不对称 (-24 ~ +18)
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

        arc_r = 13.2
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        p.setPen(QPen(QColor("#141417"), 2.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.0), QPointF(cx, cy - arc_r + 0.7))

        arc_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(QPen(arc_col, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._mode == "bipolar":
            zero_deg = 90.0
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        knob_r = 9.6
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 2.8
        iy1 = cy - math.sin(rad) * 2.8
        ix2 = cx + math.cos(rad) * (knob_r - 1.3)
        iy2 = cy - math.sin(rad) * (knob_r - 1.3)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 4. 紧凑旋钮单元 (CompactKnobCell: 适配单频段双列参数矩阵)
# =====================================================================
class CompactKnobCell(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        label: str,
        min_val: float,
        max_val: float,
        default_val: float,
        accent_color: str,
        min_hint: str,
        max_hint: str,
        formatter,
        parser,
        mode: str = "linear",
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
        lay.setContentsMargins(1, 0, 1, 0)
        lay.setSpacing(1)

        self.title_lbl = QLabel(label)
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet("color: #9A9AA2; font-size: 9.5px; font-weight: 600;")
        lay.addWidget(self.title_lbl)

        self.knob = ProRotaryKnob(min_val, max_val, default_val, default_val, accent_color, mode=mode)
        self.knob.valueChanged.connect(self._on_knob_changed)
        lay.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(3, 0, 3, 0)
        hints_row.setSpacing(0)
        self.min_lbl = QLabel(min_hint)
        self.min_lbl.setStyleSheet("color: #70707A; font-size: 8px;")
        self.max_lbl = QLabel(max_hint)
        self.max_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.max_lbl.setStyleSheet("color: #70707A; font-size: 8px;")
        hints_row.addWidget(self.min_lbl)
        hints_row.addStretch()
        hints_row.addWidget(self.max_lbl)
        lay.addLayout(hints_row)

        self.pill = EditableValuePill(formatter(default_val), accent_color, width=54)
        self.pill.valueEdited.connect(self._on_pill_edited)
        lay.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignCenter)

    def setValue(self, val: float):
        self._updating = True
        self._val = max(self._min, min(self._max, val))
        self.knob.setValue(self._val)
        self.pill.setText(self._formatter(self._val))
        self._updating = False

    def setBandActive(self, active: bool):
        self.title_lbl.setStyleSheet(f"color: {'#9A9AA2' if active else '#64646C'}; font-size: 9.5px; font-weight: 600;")
        self.min_lbl.setStyleSheet(f"color: {'#70707A' if active else '#505058'}; font-size: 8px;")
        self.max_lbl.setStyleSheet(f"color: {'#70707A' if active else '#505058'}; font-size: 8px;")
        self.knob.setBandActive(active)
        self.pill.setBandActive(active)

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
# 5. 控件：交互式动态均衡器频响图 (静态曲线 + 动态包络扇区 + 实时调制曲线 + 侧链频谱)
# =====================================================================
class ProDynamicEQGraphWidget(QWidget):
    nodeDragged = pyqtSignal(int, float, float)          # band_idx, freq, static_gain
    nodeDynRangeDragged = pyqtSignal(int, float)         # band_idx, dyn_range_db
    nodeQScrolled = pyqtSignal(int, float)               # band_idx, new_q
    nodeClicked = pyqtSignal(int)                        # band_idx
    nodeShapeRequested = pyqtSignal(int, str)            # band_idx, new_shape
    nodeDynToggled = pyqtSignal(int)                     # band_idx
    nodeTriggerToggled = pyqtSignal(int)                 # band_idx (INT <-> EXT)
    nodeToggleRequested = pyqtSignal(int)                # double-click node bypass
    bandSpawnRequested = pyqtSignal(float, float)        # double-click empty plot (freq, gain)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(270)
        self.setMouseTracking(True)

        self._bands = default_dynamic_eq_demo()["bands"]
        self._eq_enabled = True
        self._ext_sc_enabled = True
        self._master_gain = 0.0

        # 每个频段当前的实时动态增益偏移量 (dB: 负值=衰减, 正值=扩展)
        self._band_dyn_offsets = [0.0] * 6

        self._show_spectrum = True
        self._show_sc_spectrum = True
        self._show_peak_hold = True

        self._selected_node = 3
        self._hover_node = -1
        self._hover_dyn_handle = -1
        self._drag_mode = ""          # "node" | "dyn_range" | ""
        self._dragging_node = -1
        self._drag_start_freq = 0.0
        self._drag_start_gain = 0.0
        self._mouse_pos: QPointF | None = None

        self._ml = 36.0
        self._mr = 36.0
        self._mt = 10.0
        self._mb = 24.0

        self._btn_spec_rect = QRectF()
        self._btn_sc_rect = QRectF()
        self._btn_peak_rect = QRectF()

        # 实时频谱缓冲 (主信号 + 外部侧链信号)
        self._num_bins = 240
        self._spec_freqs = [
            FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** (i / (self._num_bins - 1)))
            for i in range(self._num_bins)
        ]
        self._spec_post = [-48.0] * self._num_bins
        self._spec_peak = [-44.0] * self._num_bins
        self._spec_sc = [-65.0] * self._num_bins

    def setState(self, bands: list[dict], eq_enabled: bool, ext_sc_enabled: bool, master_gain: float):
        self._bands = bands
        self._eq_enabled = eq_enabled
        self._ext_sc_enabled = ext_sc_enabled
        self._master_gain = master_gain
        self.update()

    def setSelectedNode(self, idx: int):
        if self._selected_node != idx:
            self._selected_node = idx
            self.update()

    def update_spectrum(
        self,
        freqs_hz: list[float],
        post_db: list[float],
        peak_db: list[float] | None = None,
        sc_db: list[float] | None = None,
    ):
        self._spec_freqs = freqs_hz
        self._spec_post = post_db
        self._spec_peak = peak_db if peak_db is not None else post_db
        if sc_db is not None:
            self._spec_sc = sc_db
        self.update()

    def update_dynamic_offsets(self, offsets_db: list[float]):
        for i in range(min(6, len(offsets_db))):
            self._band_dyn_offsets[i] = offsets_db[i]
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(10.0, self.width() - self._ml - self._mr),
            max(10.0, self.height() - self._mt - self._mb),
        )

    def _freq_to_x(self, freq: float, plot: QRectF) -> float:
        f = max(FREQ_MIN, min(FREQ_MAX, freq))
        norm = math.log10(f / FREQ_MIN) / math.log10(FREQ_MAX / FREQ_MIN)
        return plot.left() + norm * plot.width()

    def _x_to_freq(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)

    def _gain_to_y(self, gain_db: float, plot: QRectF) -> float:
        g = max(-25.0, min(25.0, gain_db))
        norm = (20.0 - g) / 40.0
        return plot.top() + norm * plot.height()

    def _y_to_gain(self, y: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (y - plot.top()) / plot.height()))
        return 20.0 - norm * 40.0

    def _spec_db_to_y(self, spec_db: float, plot: QRectF) -> float:
        norm = (0.0 - spec_db) / 80.0
        return plot.top() + max(0.0, min(1.0, norm)) * plot.height()

    # -----------------------------------------------------------------
    # Biquad 频响计算 (支持静态 Gain + 实时动态偏移 Gain Offset)
    # -----------------------------------------------------------------
    def _band_magnitude_db(self, band: dict, freq: float, dyn_offset: float = 0.0) -> float:
        if not band["enabled"]:
            return 0.0

        ftype = band["filter_type"]
        f0 = max(10.0, float(band["freq"]))
        eff_gain = float(band["gain"]) + (dyn_offset if band.get("dyn_enabled", False) else 0.0)
        q = max(0.1, float(band["q"]))

        if ftype == "HPF":
            order = max(1, int(band.get("slope", 12)) // 6)
            ratio = f0 / max(1.0, freq)
            mag_sq = 1.0 / (1.0 + (ratio ** (2 * order)))
            return 10.0 * math.log10(max(1e-12, mag_sq))

        if ftype == "LPF":
            order = max(1, int(band.get("slope", 12)) // 6)
            ratio = freq / f0
            mag_sq = 1.0 / (1.0 + (ratio ** (2 * order)))
            return 10.0 * math.log10(max(1e-12, mag_sq))

        fs = 48000.0
        w0 = 2.0 * math.pi * f0 / fs
        cos_w0 = math.cos(w0)
        sin_w0 = math.sin(w0)
        alpha = sin_w0 / (2.0 * q)
        A = 10.0 ** (eff_gain / 40.0)

        if ftype == "Bell":
            if abs(eff_gain) < 1e-4:
                return 0.0
            b0, b1, b2 = 1.0 + alpha * A, -2.0 * cos_w0, 1.0 - alpha * A
            a0, a1, a2 = 1.0 + alpha / A, -2.0 * cos_w0, 1.0 - alpha / A
        elif ftype == "Low Shelf":
            if abs(eff_gain) < 1e-4:
                return 0.0
            beta = 2.0 * math.sqrt(A) * alpha
            b0 = A * ((A + 1.0) - (A - 1.0) * cos_w0 + beta)
            b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w0)
            b2 = A * ((A + 1.0) - (A - 1.0) * cos_w0 - beta)
            a0 = (A + 1.0) + (A - 1.0) * cos_w0 + beta
            a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cos_w0)
            a2 = (A + 1.0) + (A - 1.0) * cos_w0 - beta
        elif ftype == "High Shelf":
            if abs(eff_gain) < 1e-4:
                return 0.0
            beta = 2.0 * math.sqrt(A) * alpha
            b0 = A * ((A + 1.0) + (A - 1.0) * cos_w0 + beta)
            b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cos_w0)
            b2 = A * ((A + 1.0) + (A - 1.0) * cos_w0 - beta)
            a0 = (A + 1.0) - (A - 1.0) * cos_w0 + beta
            a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cos_w0)
            a2 = (A + 1.0) - (A - 1.0) * cos_w0 - beta
        elif ftype == "Notch":
            b0, b1, b2 = 1.0, -2.0 * cos_w0, 1.0
            a0, a1, a2 = 1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha
        else:
            return 0.0

        w = 2.0 * math.pi * min(freq, fs * 0.495) / fs
        cos_w, sin_w = math.cos(w), math.sin(w)
        cos_2w, sin_2w = math.cos(2.0 * w), math.sin(2.0 * w)

        num_re = b0 + b1 * cos_w + b2 * cos_2w
        num_im = -b1 * sin_w - b2 * sin_2w
        den_re = a0 + a1 * cos_w + a2 * cos_2w
        den_im = -a1 * sin_w - a2 * sin_2w

        num_sq = num_re * num_re + num_im * num_im
        den_sq = max(1e-24, den_re * den_re + den_im * den_im)
        return 10.0 * math.log10(max(1e-12, num_sq / den_sq))

    def _total_static_db_at(self, freq: float) -> float:
        if not self._eq_enabled:
            return 0.0
        total = self._master_gain
        for b in self._bands:
            total += self._band_magnitude_db(b, freq, 0.0)
        return total

    def _total_dynamic_db_at(self, freq: float) -> float:
        if not self._eq_enabled:
            return 0.0
        total = self._master_gain
        for i, b in enumerate(self._bands):
            total += self._band_magnitude_db(b, freq, self._band_dyn_offsets[i])
        return total

    def _node_pos(self, band: dict, plot: QRectF) -> QPointF:
        x = self._freq_to_x(band["freq"], plot)
        if band["filter_type"] in ("HPF", "LPF"):
            y = self._gain_to_y(self._master_gain, plot)
        else:
            y = self._gain_to_y(band["gain"] + self._master_gain, plot)
        return QPointF(x, y)

    def _dyn_handle_pos(self, band: dict, plot: QRectF) -> QPointF:
        """计算动态范围上限/下限拖拽手柄的位置"""
        np = self._node_pos(band, plot)
        target_db = band["gain"] + band["dyn_range"] + self._master_gain
        hy = self._gain_to_y(target_db, plot)
        # 若 dyn_range 接近 0，手柄默认放在节点下方 18px 方便直接下拉
        if abs(hy - np.y()) < 14.0:
            hy = min(plot.bottom() - 8.0, np.y() + 18.0)
        return QPointF(np.x(), hy)

    # -----------------------------------------------------------------
    # 交互事件：节点拖拽 / 动态范围手柄拖拽 / 滚轮 Q & Range / 右键菜单
    # -----------------------------------------------------------------
    def _hit_test(self, pos: QPointF) -> tuple[str, int]:
        plot = self._plot_rect()
        # 1. 优先命中已启用动态模式的频段的 Dynamic Range 手柄
        for idx in reversed(range(len(self._bands))):
            b = self._bands[idx]
            if not b["enabled"] or not b.get("dyn_enabled", False):
                continue
            if b["filter_type"] not in ("Bell", "Low Shelf", "High Shelf"):
                continue
            if idx in (self._selected_node, self._hover_node):
                hp = self._dyn_handle_pos(b, plot)
                if abs(pos.x() - hp.x()) <= 11.0 and abs(pos.y() - hp.y()) <= 7.0:
                    return "dyn_range", idx

        # 2. 命中频段节点圆心
        for idx in reversed(range(len(self._bands))):
            b = self._bands[idx]
            if not b["enabled"]:
                continue
            np = self._node_pos(b, plot)
            if math.hypot(pos.x() - np.x(), pos.y() - np.y()) <= 12.0:
                return "node", idx

        return "", -1

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_node = -1
        self._hover_dyn_handle = -1
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_spec_rect.contains(pos):
                self._show_spectrum = not self._show_spectrum
                self.update()
                return
            if self._btn_sc_rect.contains(pos):
                self._show_sc_spectrum = not self._show_sc_spectrum
                self.update()
                return
            if self._btn_peak_rect.contains(pos):
                self._show_peak_hold = not self._show_peak_hold
                self.update()
                return

            hit_type, idx = self._hit_test(pos)
            if idx >= 0:
                self._drag_mode = hit_type
                self._dragging_node = idx
                self._selected_node = idx
                self._drag_start_freq = self._bands[idx]["freq"]
                self._drag_start_gain = self._bands[idx]["gain"]
                self.setCursor(
                    Qt.CursorShape.SizeVerCursor if hit_type == "dyn_range" else Qt.CursorShape.ClosedHandCursor
                )
                self.nodeClicked.emit(idx)
                self.update()

        elif event.button() == Qt.MouseButton.RightButton:
            _, idx = self._hit_test(pos)
            if idx >= 0:
                self._show_node_context_menu(idx, event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        plot = self._plot_rect()
        if not plot.contains(pos):
            return

        _, idx = self._hit_test(pos)
        if idx >= 0:
            self.nodeToggleRequested.emit(idx)
        else:
            f = self._x_to_freq(pos.x(), plot)
            g = max(-20.0, min(20.0, round(self._y_to_gain(pos.y(), plot) - self._master_gain, 1)))
            self.bandSpawnRequested.emit(round(f, 1), g)

    def _show_node_context_menu(self, idx: int, global_pos):
        b = self._bands[idx]
        if idx == 0:
            opts = ["HPF", "Low Shelf", "Bell"]
        elif idx == 5:
            opts = ["LPF", "High Shelf", "Bell"]
        else:
            opts = ["Low Shelf", "Bell", "High Shelf", "Notch"]

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #1D1D21; border: 1px solid #141416; color: #DCDCE2; font-size: 11px; }
            QMenu::item { padding: 5px 16px; }
            QMenu::item:selected { background-color: #35353D; color: #FFFFFF; }
        """)
        for opt in opts:
            act = menu.addAction(f"Filter: {opt}")
            act.triggered.connect(lambda checked=False, o=opt: self.nodeShapeRequested.emit(idx, o))

        if b["filter_type"] in ("Bell", "Low Shelf", "High Shelf"):
            menu.addSeparator()
            dyn_lbl = "Disable Dynamic Mode" if b["dyn_enabled"] else "Enable Dynamic Mode"
            act_dyn = menu.addAction(dyn_lbl)
            act_dyn.triggered.connect(lambda checked=False: self.nodeDynToggled.emit(idx))

            trig_lbl = "Trigger: Switch to Internal (INT)" if b["trigger_mode"] == "EXT" else "Trigger: Switch to External SC (EXT)"
            act_trig = menu.addAction(trig_lbl)
            act_trig.triggered.connect(lambda checked=False: self.nodeTriggerToggled.emit(idx))

        menu.exec(global_pos)

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._dragging_node >= 0:
            b = self._bands[self._dragging_node]
            mods = event.modifiers()

            if self._drag_mode == "dyn_range" or (mods & Qt.KeyboardModifier.ShiftModifier):
                # 拖拽动态范围手柄或按住 Shift 垂直拖动节点 -> 调节 Dynamic Range
                target_g = self._y_to_gain(event.position().y(), plot) - self._master_gain
                raw_range = round(target_g - b["gain"], 1)
                clamped_range = max(-24.0, min(18.0, raw_range))
                self.nodeDynRangeDragged.emit(self._dragging_node, clamped_range)
                return

            new_freq = (
                self._drag_start_freq
                if (mods & Qt.KeyboardModifier.ControlModifier)
                else self._x_to_freq(event.position().x(), plot)
            )
            if b["filter_type"] in ("HPF", "LPF") or (mods & Qt.KeyboardModifier.AltModifier):
                new_gain = self._drag_start_gain
            else:
                raw_g = self._y_to_gain(event.position().y(), plot) - self._master_gain
                new_gain = max(-20.0, min(20.0, round(raw_g, 1)))

            self.nodeDragged.emit(self._dragging_node, new_freq, new_gain)
            return

        hit_type, idx = self._hit_test(event.position())
        self._hover_dyn_handle = idx if hit_type == "dyn_range" else -1
        self._hover_node = idx if hit_type in ("node", "dyn_range") else -1

        if hit_type == "dyn_range":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif hit_type == "node":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_node >= 0:
            self._dragging_node = -1
            self._drag_mode = ""
            self.update()

    def wheelEvent(self, event):
        target = self._dragging_node if self._dragging_node >= 0 else self._hover_node
        if target >= 0:
            b = self._bands[target]
            delta = event.angleDelta().y() / 120.0
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Shift + 滚轮：精细调节 Dynamic Range
                if b["filter_type"] in ("Bell", "Low Shelf", "High Shelf"):
                    new_r = max(-24.0, min(18.0, round(b["dyn_range"] + delta * 0.5, 1)))
                    self.nodeDynRangeDragged.emit(target, new_r)
            else:
                new_q = max(0.3, min(12.0, round(b["q"] + delta * 0.2, 1)))
                self.nodeQScrolled.emit(target, new_q)
            event.accept()

    # -----------------------------------------------------------------
    # 绘制网格、主/侧链频谱、动态范围扇区、静态与实时调制曲线、控制节点
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 画布纯平底色
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 水平 dB 网格线
        grid_pen = QPen(QColor("#26262A"), 1.0)
        zero_pen = QPen(QColor("#3A3A40"), 1.1)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        left_ticks = [0, -10, -20, -30, -40, -50, -60, -70, -80]
        right_ticks = ["+20", "", "+10", "", "0", "", "-10", "", "-20"]

        for i, l_val in enumerate(left_ticks):
            y = plot.top() + (i / (len(left_ticks) - 1)) * plot.height()
            p.setPen(zero_pen if l_val == -40 else grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

            p.setPen(QColor("#9A9AA2"))
            p.drawText(
                QRectF(0, y - 8, self._ml - 7, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                str(l_val),
            )
            if right_ticks[i]:
                p.drawText(
                    QRectF(plot.right() + 7, y - 8, self._mr - 7, 16),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    right_ticks[i],
                )

        # 3. 垂直对数频率刻度线
        freq_ticks = [
            (31.25, "31"), (62.5, "62"), (125.0, "125"), (250.0, "250"),
            (500.0, "500"), (1000.0, "1K"), (2000.0, "2K"), (4000.0, "4K"),
            (8000.0, "8K"), (16000.0, "16K"),
        ]
        p.setPen(QColor("#9A9AA2"))
        p.drawText(QRectF(plot.left(), plot.bottom() + 5, 24, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Hz")
        for f_val, f_label in freq_ticks:
            x = self._freq_to_x(f_val, plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#9A9AA2"))
            p.drawText(QRectF(x - 18, plot.bottom() + 5, 36, 16), Qt.AlignmentFlag.AlignCenter, f_label)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        # 4. 实时频谱仪（主信号频谱 + 外部侧链频谱冲突透视图）
        if self._show_spectrum and len(self._spec_freqs) > 1:
            peak_path = QPainterPath()
            post_path = QPainterPath()
            fill_poly = QPolygonF()
            first_x = self._freq_to_x(self._spec_freqs[0], plot)
            fill_poly.append(QPointF(first_x, plot.bottom()))

            for idx_s, f_s in enumerate(self._spec_freqs):
                sx = self._freq_to_x(f_s, plot)
                py = self._spec_db_to_y(self._spec_peak[idx_s], plot)
                sy = self._spec_db_to_y(self._spec_post[idx_s], plot)
                if idx_s == 0:
                    peak_path.moveTo(sx, py)
                    post_path.moveTo(sx, sy)
                else:
                    peak_path.lineTo(sx, py)
                    post_path.lineTo(sx, sy)
                fill_poly.append(QPointF(sx, sy))

            last_x = self._freq_to_x(self._spec_freqs[-1], plot)
            fill_poly.append(QPointF(last_x, plot.bottom()))

            spec_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            spec_grad.setColorAt(0.0, QColor(56, 128, 210, 68))
            spec_grad.setColorAt(1.0, QColor(36, 72, 130, 14))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(spec_grad)
            p.drawPolygon(fill_poly)

            if self._show_peak_hold:
                p.setPen(QPen(QColor(150, 150, 158, 110), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(peak_path)

            p.setPen(QPen(QColor(82, 154, 235, 185), 1.15))
            p.drawPath(post_path)

        # 外部侧链叠加频谱（琥珀橙轮廓线，直观标识人声/鼓组等外部触发源频率冲突）
        if self._ext_sc_enabled and self._show_sc_spectrum and len(self._spec_sc) == len(self._spec_freqs):
            sc_path = QPainterPath()
            for idx_s, f_s in enumerate(self._spec_freqs):
                sx = self._freq_to_x(f_s, plot)
                scy = self._spec_db_to_y(self._spec_sc[idx_s], plot)
                if idx_s == 0:
                    sc_path.moveTo(sx, scy)
                else:
                    sc_path.lineTo(sx, scy)
            p.setPen(QPen(QColor(240, 168, 72, 155), 1.25))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(sc_path)

        # 5. 各频段静态包络 + 动态范围极限包络扇区 (Dynamic Range Envelope)
        steps = 300
        zero_y = self._gain_to_y(self._master_gain, plot)
        solo_idx = next((i for i, b in enumerate(self._bands) if b["enabled"] and b.get("solo", False)), -1)

        if self._eq_enabled:
            for idx_b, b in enumerate(self._bands):
                if not b["enabled"]:
                    continue
                is_solo = (idx_b == solo_idx)
                is_focus = (idx_b in (self._hover_node, self._dragging_node, self._selected_node) or is_solo)
                b_col = QColor(BAND_PALETTE[idx_b])
                dimmed = (solo_idx >= 0 and not is_solo)

                # 5.1 静态频响微填充
                stat_fill = QPainterPath()
                for i in range(steps + 1):
                    norm = i / steps
                    f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
                    x = plot.left() + norm * plot.width()
                    y = self._gain_to_y(self._master_gain + self._band_magnitude_db(b, f, 0.0), plot)
                    if i == 0:
                        stat_fill.moveTo(x, zero_y)
                        stat_fill.lineTo(x, y)
                    else:
                        stat_fill.lineTo(x, y)
                stat_fill.lineTo(plot.right(), zero_y)
                stat_fill.closeSubpath()

                alpha = 6 if dimmed else (42 if is_focus else 16)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), alpha))
                p.drawPath(stat_fill)

                # 5.2 动态范围扇区 (从 Static Gain 到 Static Gain + Dyn Range 的最大允许动态调制区间)
                if b.get("dyn_enabled", False) and b["filter_type"] in ("Bell", "Low Shelf", "High Shelf") and abs(b["dyn_range"]) > 0.1:
                    dyn_ribbon = QPainterPath()
                    max_limit_path = QPainterPath()
                    for i in range(steps + 1):
                        norm = i / steps
                        f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
                        x = plot.left() + norm * plot.width()
                        y_stat = self._gain_to_y(self._master_gain + self._band_magnitude_db(b, f, 0.0), plot)
                        if i == 0:
                            dyn_ribbon.moveTo(x, y_stat)
                        else:
                            dyn_ribbon.lineTo(x, y_stat)

                    for i in range(steps, -1, -1):
                        norm = i / steps
                        f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
                        x = plot.left() + norm * plot.width()
                        y_lim = self._gain_to_y(self._master_gain + self._band_magnitude_db(b, f, b["dyn_range"]), plot)
                        dyn_ribbon.lineTo(x, y_lim)
                        if i == steps:
                            max_limit_path.moveTo(x, y_lim)
                        else:
                            max_limit_path.lineTo(x, y_lim)
                    dyn_ribbon.closeSubpath()

                    rib_alpha = 10 if dimmed else (52 if is_focus else 26)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), rib_alpha))
                    p.drawPath(dyn_ribbon)

                    # 动态极限虚线轮廓
                    lim_col = QColor(b_col.red(), b_col.green(), b_col.blue(), 40 if dimmed else (165 if is_focus else 85))
                    p.setPen(QPen(lim_col, 1.0, Qt.PenStyle.DotLine))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawPath(max_limit_path)

        # 6. 静态总曲线 (半透明参考线) + 实时动态调制后总曲线 (亮白主曲线)
        stat_path = QPainterPath()
        dyn_path = QPainterPath()
        has_any_dyn = any(
            b["enabled"] and b.get("dyn_enabled", False) and abs(self._band_dyn_offsets[i]) > 0.05
            for i, b in enumerate(self._bands)
        )

        for i in range(steps + 1):
            norm = i / steps
            f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
            x = plot.left() + norm * plot.width()
            ys = self._gain_to_y(self._total_static_db_at(f), plot)
            yd = self._gain_to_y(self._total_dynamic_db_at(f), plot)
            if i == 0:
                stat_path.moveTo(x, ys)
                dyn_path.moveTo(x, yd)
            else:
                stat_path.lineTo(x, ys)
                dyn_path.lineTo(x, yd)

        if self._eq_enabled:
            if has_any_dyn:
                p.setPen(QPen(QColor(210, 216, 228, 95), 1.2, Qt.PenStyle.DashLine))
                p.drawPath(stat_path)

            p.setPen(QPen(QColor(235, 248, 255, 42), 4.5))
            p.drawPath(dyn_path)
            p.setPen(QPen(QColor("#F2F8FC"), 1.95))
            p.drawPath(dyn_path)
        else:
            p.setPen(QPen(QColor("#686C78"), 1.6))
            p.drawPath(stat_path)

        # 7. 频段控制节点 + 动态范围调节手柄 + 实时动态偏移指示点
        node_font = QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold)
        p.setFont(node_font)

        for idx, b in enumerate(self._bands):
            if not b["enabled"]:
                continue
            pt = self._node_pos(b, plot)
            is_solo = (idx == solo_idx)
            dimmed = (solo_idx >= 0 and not is_solo)
            b_col = QColor(BAND_PALETTE[idx]) if (self._eq_enabled and not dimmed) else QColor("#565A66")
            is_hi = (idx in (self._hover_node, self._dragging_node, self._selected_node) or is_solo)
            radius = 10.0 if is_hi else 8.4

            is_dyn_band = b.get("dyn_enabled", False) and b["filter_type"] in ("Bell", "Low Shelf", "High Shelf")

            # 7.1 动态模式下绘制垂直动态范围导轨与可拖拽手柄
            if is_dyn_band and self._eq_enabled:
                hp = self._dyn_handle_pos(b, plot)
                # 节点到动态范围上限之间的垂直指示线
                p.setPen(QPen(QColor(b_col.red(), b_col.green(), b_col.blue(), 170), 1.4))
                p.drawLine(pt, hp)

                # 实时动态增益位置发光指示点
                cur_offset = self._band_dyn_offsets[idx]
                if abs(cur_offset) > 0.08:
                    cur_y = self._gain_to_y(b["gain"] + cur_offset + self._master_gain, plot)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(b_col)
                    p.drawEllipse(QPointF(pt.x(), cur_y), 3.4, 3.4)

                # 选中或悬停时显示动态范围胶囊手柄 (可直接上下拖动改变 Dyn Range)
                if is_hi:
                    h_hi = (self._hover_dyn_handle == idx) or (self._drag_mode == "dyn_range" and self._dragging_node == idx)
                    hw = 20.0 if h_hi else 16.0
                    h_rect = QRectF(hp.x() - hw / 2.0, hp.y() - 3.5, hw, 7.0)
                    p.setPen(QPen(QColor("#FFFFFF") if h_hi else b_col, 1.3))
                    p.setBrush(b_col if h_hi else QColor("#151518"))
                    p.drawRoundedRect(h_rect, 3.0, 3.0)

            # 7.2 节点外光环 (Solo 金环 / 动态模式双环 / 选中光晕)
            if is_solo and self._eq_enabled:
                p.setPen(QPen(QColor("#F6CE58"), 1.4))
                p.setBrush(QColor(232, 184, 56, 55))
                p.drawEllipse(pt, radius + 5.2, radius + 5.2)
            elif is_dyn_band and self._eq_enabled:
                p.setPen(QPen(QColor(b_col.red(), b_col.green(), b_col.blue(), 145), 1.2, Qt.PenStyle.DotLine))
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 42 if is_hi else 16))
                p.drawEllipse(pt, radius + 3.8, radius + 3.8)
            elif is_hi and self._eq_enabled:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 55))
                p.drawEllipse(pt, radius + 4.0, radius + 4.0)

            # 7.3 节点主体
            p.setPen(QPen(b_col, 1.8))
            p.setBrush(QColor("#151518"))
            p.drawEllipse(pt, radius, radius)

            p.setPen(QColor("#ECECF0") if not dimmed else QColor("#8A8A92"))
            p.drawText(
                QRectF(pt.x() - radius, pt.y() - radius, radius * 2, radius * 2),
                Qt.AlignmentFlag.AlignCenter,
                str(b["index"]),
            )

            # 若启用了外部侧链触发，在节点右上角绘制极简 "SC" 微标
            if is_dyn_band and b.get("trigger_mode") == "EXT" and self._eq_enabled:
                sc_badge = QRectF(pt.x() + radius - 2, pt.y() - radius - 6, 16, 10)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#F0A848"))
                p.drawRoundedRect(sc_badge, 2.0, 2.0)
                p.setPen(QColor("#121214"))
                p.setFont(QFont(MONO_FONT_FAM, 6, QFont.Weight.Bold))
                p.drawText(sc_badge, Qt.AlignmentFlag.AlignCenter, "SC")
                p.setFont(node_font)

        # 8. 右上角悬浮频谱控制胶囊 (SPEC / SC / PEAK)
        self._btn_spec_rect = QRectF(plot.right() - 122, plot.top() + 8, 36, 18)
        self._btn_sc_rect = QRectF(plot.right() - 82, plot.top() + 8, 32, 18)
        self._btn_peak_rect = QRectF(plot.right() - 46, plot.top() + 8, 38, 18)

        pills = [
            (self._btn_spec_rect, "SPEC", self._show_spectrum, "#28D8F0", "#24788C"),
            (self._btn_sc_rect,   "SC",   self._show_sc_spectrum and self._ext_sc_enabled, "#F0A848", "#8C6224"),
            (self._btn_peak_rect, "PEAK", self._show_peak_hold and self._show_spectrum, "#28D8F0", "#24788C"),
        ]
        for rect, label, active, fg_hex, bd_hex in pills:
            p.setPen(QPen(QColor(bd_hex) if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 34, 42, 215) if active else QColor(21, 21, 24, 185))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor(fg_hex) if active else QColor("#787882"))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 9. 左上角实时十字准星 & 频率/动态参数 HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(mx, plot.top()), QPointF(mx, plot.bottom()))
            p.drawLine(QPointF(plot.left(), my), QPointF(plot.right(), my))

            if self._dragging_node >= 0:
                db_band = self._bands[self._dragging_node]
                dyn_part = (
                    f"  •  Dyn {db_band['dyn_range']:+.1f} dB (Th {db_band['threshold']:.0f}dB)"
                    if db_band.get("dyn_enabled")
                    else ""
                )
                hud_str = (
                    f"Band {db_band['index']}  •  {format_freq(db_band['freq'])}Hz "
                    f"({freq_to_note(db_band['freq'])})  •  {db_band['gain']:+.1f} dB  •  Q {db_band['q']:.1f}{dyn_part}"
                )
                hud_col = QColor(BAND_PALETTE[self._dragging_node])
                hud_w = 365 if db_band.get("dyn_enabled") else 245
            else:
                cur_f = self._x_to_freq(mx, plot)
                cur_g = self._y_to_gain(my, plot)
                hud_str = f"{format_freq(cur_f)}Hz ({freq_to_note(cur_f)})   {cur_g:+.1f} dB"
                hud_col = QColor("#B4B4BE")
                hud_w = 165

            hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor(21, 21, 24, 215))
            p.drawRoundedRect(hud_rect, 3.5, 3.5)
            p.setPen(hud_col)
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 6. 控件：左侧 Input 电平表 & 右侧 Master Output 推子+电平表
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
# 7. 控件：单频段实时动态增益偏移迷你条 (BandDynActivityBar)
# =====================================================================
class BandDynActivityBar(QWidget):
    """双向动态增益指示条：0点居中，向左显示动态压缩衰减(-dB)，向右显示动态向上扩展(+dB)"""

    def __init__(self, accent_color: str, parent=None):
        super().__init__(parent)
        self._accent = QColor(accent_color)
        self._offset_db = 0.0
        self._active = False
        self.setFixedHeight(5)

    def setActivity(self, offset_db: float, active: bool):
        self._offset_db = max(-24.0, min(18.0, offset_db))
        self._active = active
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(QColor("#121214"), 1.0))
        p.setBrush(QColor("#141417"))
        p.drawRoundedRect(rect, 2.0, 2.0)

        mid_x = rect.center().x()
        p.setPen(QPen(QColor("#34343C"), 1.0))
        p.drawLine(QPointF(mid_x, rect.top() + 1), QPointF(mid_x, rect.bottom() - 1))

        if self._active and abs(self._offset_db) > 0.08:
            if self._offset_db < 0:
                ratio = min(1.0, abs(self._offset_db) / 18.0)
                w = (rect.width() * 0.48) * ratio
                bar = QRectF(mid_x - w, rect.top() + 0.8, w, rect.height() - 1.6)
            else:
                ratio = min(1.0, self._offset_db / 18.0)
                w = (rect.width() * 0.48) * ratio
                bar = QRectF(mid_x, rect.top() + 0.8, w, rect.height() - 1.6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._accent)
            p.drawRoundedRect(bar, 1.5, 1.5)


# =====================================================================
# 8. 控件：单频段静态 + 动态联合控制卡片 (DynamicBandCard)
# =====================================================================
class DynamicBandCard(QFrame):
    bandChanged = pyqtSignal(int, dict)
    soloToggled = pyqtSignal(int, bool)
    selected = pyqtSignal(int)

    def __init__(self, band_data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(band_data)
        self._idx = self._data["index"] - 1
        self._accent = BAND_PALETTE[self._idx]
        self._updating = False
        self._selected = False

        self.setObjectName("BandCard")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- 1. 顶部栏：频段电源 + Solo 独奏 + 滤波器形状 ---
        header = QWidget()
        header.setFixedHeight(33)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(7, 5, 7, 4)
        h_lay.setSpacing(4)

        self.band_btn = QPushButton(f"Band {self._data['index']}")
        self.band_btn.setCheckable(True)
        self.band_btn.setFixedHeight(21)
        self.band_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.band_btn.clicked.connect(self._on_toggle_band)
        h_lay.addWidget(self.band_btn, 1)

        self.solo_btn = QPushButton("S")
        self.solo_btn.setCheckable(True)
        self.solo_btn.setFixedSize(21, 21)
        self.solo_btn.setToolTip("独奏监听当前频段 (Solo Band)")
        self.solo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.solo_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #3C3C44; color: #FFFFFF; }
            QPushButton:checked {
                background-color: #E8B838;
                color: #121316;
                border: 1px solid #F6CE58;
            }
        """)
        self.solo_btn.clicked.connect(self._on_toggle_solo)
        h_lay.addWidget(self.solo_btn)

        idx = self._data["index"]
        if idx == 1:
            shapes = ["HPF", "Low Shelf", "Bell"]
        elif idx == 6:
            shapes = ["LPF", "High Shelf", "Bell"]
        else:
            shapes = ["Low Shelf", "Bell", "High Shelf", "Notch"]

        self.shape_selector = FilterShapeSelector(shapes, self._data["filter_type"], self._accent)
        self.shape_selector.shapeChanged.connect(self._on_shape_changed)
        h_lay.addWidget(self.shape_selector)
        root.addWidget(header)

        # --- 2. 静态 EQ 参数区 (Frequency + Gain & Q 双列) ---
        self.static_sec = QFrame()
        self.static_sec.setObjectName("SubSection")
        s_lay = QVBoxLayout(self.static_sec)
        s_lay.setContentsMargins(7, 5, 7, 5)
        s_lay.setSpacing(5)

        # 2.1 Frequency 行
        f_top = QHBoxLayout()
        f_left = QVBoxLayout()
        f_left.setSpacing(3)
        self.f_lbl = QLabel("Frequency")
        self.f_lbl.setObjectName("ParamTitle")
        self.freq_pill = EditableValuePill(format_freq(self._data["freq"]), self._accent, width=56)
        self.freq_pill.valueEdited.connect(self._on_freq_text)
        f_left.addWidget(self.f_lbl)
        f_left.addWidget(self.freq_pill)
        f_left.addStretch()

        f_right = QVBoxLayout()
        f_right.setSpacing(1)
        r_min, r_max, r_min_s, r_max_s = RANGE_PRESETS[self._data["range_key"]]
        self.freq_knob = ProRotaryKnob(r_min, r_max, self._data["freq"], self._data["freq"], self._accent, mode="log")
        self.freq_knob.valueChanged.connect(self._on_freq_knob)

        f_minmax = QHBoxLayout()
        f_minmax.setSpacing(2)
        self.freq_min_lbl = QLabel(r_min_s)
        self.freq_min_lbl.setObjectName("RangeHint")
        self.freq_max_lbl = QLabel(r_max_s)
        self.freq_max_lbl.setObjectName("RangeHint")
        f_minmax.addWidget(self.freq_min_lbl)
        f_minmax.addStretch()
        f_minmax.addWidget(self.freq_max_lbl)

        f_right.addWidget(self.freq_knob, 0, Qt.AlignmentFlag.AlignCenter)
        f_right.addLayout(f_minmax)

        f_top.addLayout(f_left)
        f_top.addStretch()
        f_top.addLayout(f_right)
        s_lay.addLayout(f_top)

        # 斜率 / 频段范围档位按钮行
        seg_row = QHBoxLayout()
        seg_row.setSpacing(2)
        self.slope_btns: dict[int, QPushButton] = {}
        self.range_btns: dict[str, QPushButton] = {}

        for s_val in (6, 12, 18, 24):
            btn = QPushButton(f"{s_val}dB")
            btn.setObjectName("SegBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(18)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, sv=s_val: self._on_slope_clicked(sv))
            seg_row.addWidget(btn)
            self.slope_btns[s_val] = btn

        for r_key in ("L", "ML", "MH", "H"):
            btn = QPushButton(r_key)
            btn.setObjectName("SegBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(18)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, rk=r_key: self._on_range_clicked(rk))
            seg_row.addWidget(btn)
            self.range_btns[r_key] = btn

        s_lay.addLayout(seg_row)

        # 2.2 Static Gain & Q Factor 双列并排
        self.gq_wrap = QWidget()
        gq_row = QHBoxLayout(self.gq_wrap)
        gq_row.setContentsMargins(0, 1, 0, 0)
        gq_row.setSpacing(2)

        self.c_gain = CompactKnobCell(
            "Gain", -20.0, 20.0, 0.0, self._accent,
            "-20", "+20", format_db_signed, lambda s, fb: float(s), mode="bipolar"
        )
        self.c_gain.valueChanged.connect(self._on_gain_changed)

        self.c_q = CompactKnobCell(
            "Q Factor", 0.3, 12.0, 1.4, self._accent,
            "0.3", "12.0", lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="log"
        )
        self.c_q.valueChanged.connect(self._on_q_changed)

        gq_row.addWidget(self.c_gain, 1)
        gq_row.addWidget(self.c_q, 1)
        s_lay.addWidget(self.gq_wrap)

        root.addWidget(self.static_sec)

        # --- 3. 动态均衡核心区 (Dynamics Section: DYN开关 + 触发源INT/EXT + 4旋钮) ---
        self.dyn_sec = QFrame()
        self.dyn_sec.setObjectName("SubSection")
        d_lay = QVBoxLayout(self.dyn_sec)
        d_lay.setContentsMargins(7, 5, 7, 6)
        d_lay.setSpacing(4)

        # 3.1 动态控制条：[DYN 开关] + [INT / EXT 触发切换] + [AUTO 时值]
        dyn_hdr = QHBoxLayout()
        dyn_hdr.setSpacing(3)

        self.dyn_btn = QPushButton("DYN")
        self.dyn_btn.setCheckable(True)
        self.dyn_btn.setFixedHeight(19)
        self.dyn_btn.setToolTip("启用/旁通当前频段的动态响应")
        self.dyn_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dyn_btn.clicked.connect(self._on_toggle_dyn)
        dyn_hdr.addWidget(self.dyn_btn, 1)

        self.trig_btn = QPushButton("INT")
        self.trig_btn.setCheckable(True)
        self.trig_btn.setFixedSize(34, 19)
        self.trig_btn.setToolTip("切换触发检测源：INT (本频段信号内触发) / EXT (外部侧链外触发)")
        self.trig_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.trig_btn.clicked.connect(self._on_toggle_trigger)
        dyn_hdr.addWidget(self.trig_btn)

        self.auto_btn = QPushButton("A")
        self.auto_btn.setCheckable(True)
        self.auto_btn.setFixedSize(20, 19)
        self.auto_btn.setToolTip("Auto Timing: 音频自适应 Attack / Release")
        self.auto_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_btn.clicked.connect(self._on_toggle_auto)
        dyn_hdr.addWidget(self.auto_btn)

        d_lay.addLayout(dyn_hdr)

        # 实时动态增益偏移迷你活动条
        self.dyn_bar = BandDynActivityBar(self._accent)
        d_lay.addWidget(self.dyn_bar)

        # 3.2 Dyn Row 1: Dynamic Range & Threshold
        dyn_r1 = QHBoxLayout()
        dyn_r1.setSpacing(2)
        self.c_range = CompactKnobCell(
            "Range", -24.0, 18.0, -4.0, self._accent,
            "-24", "+18", format_db_signed, lambda s, fb: float(s), mode="bipolar"
        )
        self.c_range.valueChanged.connect(self._on_dyn_range_changed)

        self.c_thresh = CompactKnobCell(
            "Threshold", -60.0, 0.0, -22.0, self._accent,
            "-60", "0.0", lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="linear"
        )
        self.c_thresh.valueChanged.connect(self._on_thresh_changed)

        dyn_r1.addWidget(self.c_range, 1)
        dyn_r1.addWidget(self.c_thresh, 1)
        d_lay.addLayout(dyn_r1)

        # 3.3 Dyn Row 2: Attack & Release
        dyn_r2 = QHBoxLayout()
        dyn_r2.setSpacing(2)
        self.c_attack = CompactKnobCell(
            "Attack", 0.1, 200.0, 12.0, self._accent,
            "0.1", "200", format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_attack.valueChanged.connect(self._on_attack_changed)

        self.c_release = CompactKnobCell(
            "Release", 10.0, 2000.0, 110.0, self._accent,
            "10", "2000", format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_release.valueChanged.connect(self._on_release_changed)

        dyn_r2.addWidget(self.c_attack, 1)
        dyn_r2.addWidget(self.c_release, 1)
        d_lay.addLayout(dyn_r2)

        root.addWidget(self.dyn_sec)

        # Cut 滤波器状态下的底部留白填充
        self.bottom_spacer = QFrame()
        self.bottom_spacer.setObjectName("SubSection")
        self.bottom_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.bottom_spacer)

        self._section_refs = [self.static_sec, self.dyn_sec, self.bottom_spacer]
        self.syncFromData(self._data)

    def mousePressEvent(self, event):
        self.selected.emit(self._idx)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#141416"), 1.0))
        for sec in self._section_refs:
            if sec.isVisible():
                y = sec.y() + 0.5
                p.drawLine(QPointF(0.0, y), QPointF(float(self.width()), y))

    def setSelected(self, selected: bool):
        if self._selected != selected:
            self._selected = selected
            self._apply_card_style()

    def setLiveDynOffset(self, offset_db: float):
        en = bool(self._data["enabled"]) and bool(self._data.get("dyn_enabled", False))
        self.dyn_bar.setActivity(offset_db, en)

    def _apply_card_style(self):
        # 选中态频段主题色彩条移至顶边（对齐 multiband_comp BandCard 彩顶边风格）
        en = bool(self._data["enabled"])
        top_bar = f"2px solid {self._accent}" if (self._selected and en) else "1px solid #141416"

        self.setStyleSheet(f"""
            QFrame#BandCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: {top_bar};
                border-radius: 0px;
            }}
            QFrame#SubSection {{
                background-color: transparent;
            }}
            QLabel#ParamTitle {{
                color: {"#9A9AA2" if en else "#64646C"};
                font-size: 9.5px;
                font-weight: 600;
            }}
            QLabel#RangeHint {{
                color: {"#70707A" if en else "#505058"};
                font-size: 8px;
            }}
            QPushButton#SegBtn {{
                background-color: #323238;
                color: {"#CFCFD6" if en else "#787882"};
                border: none;
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 600;
                padding: 0px;
            }}
            QPushButton#SegBtn:hover {{
                background-color: #3C3C44;
                color: #FFFFFF;
            }}
            QPushButton#SegBtn:checked {{
                background-color: #182E38;
                color: {self._accent if en else "#787D8C"};
                border: 1px solid {self._accent if en else "#3A3A40"};
            }}
        """)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        ftype = self._data["filter_type"]
        is_cut = ftype in ("HPF", "LPF")
        is_dyn_capable = ftype in ("Bell", "Low Shelf", "High Shelf")
        dyn_on = en and is_dyn_capable and bool(self._data.get("dyn_enabled", False))

        self.band_btn.setChecked(en)
        self.solo_btn.setChecked(bool(self._data.get("solo", False)))

        if en:
            self.band_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    color: {self._accent};
                    border: 1px solid {self._accent};
                    border-radius: 3px;
                    font-size: 11px;
                    font-weight: 700;
                }}
            """)
        else:
            self.band_btn.setStyleSheet("""
                QPushButton {
                    background-color: #323238;
                    color: #8A8A92;
                    border: none;
                    border-radius: 3px;
                    font-size: 11px;
                    font-weight: 600;
                }
            """)

        self.shape_selector.setCurrentShape(ftype)
        self.shape_selector.setBandActive(en)

        r_min, r_max, r_min_s, r_max_s = RANGE_PRESETS[self._data["range_key"]]
        self.freq_min_lbl.setText(r_min_s)
        self.freq_max_lbl.setText(r_max_s)
        self.freq_knob.setRange(r_min, r_max)
        self.freq_knob.setValue(self._data["freq"])
        self.freq_knob.setBandActive(en)
        self.freq_pill.setText(format_freq(self._data["freq"]))
        self.freq_pill.setBandActive(en)

        for sv, btn in self.slope_btns.items():
            btn.setVisible(is_cut)
            btn.setChecked(sv == self._data["slope"])
        for rk, btn in self.range_btns.items():
            btn.setVisible(not is_cut)
            btn.setChecked(rk == self._data["range_key"])

        self.gq_wrap.setVisible(not is_cut)
        self.dyn_sec.setVisible(is_dyn_capable)
        self.bottom_spacer.setVisible(not is_dyn_capable)
        self.update()

        # Static Gain & Q
        self.c_gain.setValue(self._data["gain"])
        self.c_gain.setBandActive(en)
        self.c_q.setValue(self._data["q"])
        self.c_q.setBandActive(en)

        # Dynamics Controls
        self.dyn_btn.setChecked(bool(self._data.get("dyn_enabled", False)))
        if dyn_on:
            self.dyn_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #182E38;
                    color: {self._accent};
                    border: 1px solid {self._accent};
                    border-radius: 3px;
                    font-size: 10px;
                    font-weight: 700;
                }}
            """)
        else:
            self.dyn_btn.setStyleSheet("""
                QPushButton {
                    background-color: #323238;
                    color: #8A8A92;
                    border: none;
                    border-radius: 3px;
                    font-size: 10px;
                    font-weight: 600;
                }
                QPushButton:hover { color: #FFFFFF; }
            """)

        is_ext = (self._data.get("trigger_mode", "INT") == "EXT")
        self.trig_btn.setChecked(is_ext)
        self.trig_btn.setText("EXT" if is_ext else "INT")
        self.trig_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 700;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #3A2E18;
                color: #F0A848;
                border: 1px solid #F0A848;
            }
        """)

        auto_t = bool(self._data.get("auto_timing", False))
        self.auto_btn.setChecked(auto_t)
        self.auto_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 9.5px;
                font-weight: 700;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
            }
        """)

        self.c_range.setValue(self._data["dyn_range"])
        self.c_thresh.setValue(self._data["threshold"])
        self.c_attack.setValue(self._data["attack"])
        self.c_release.setValue(self._data["release"])

        self.c_range.setBandActive(dyn_on)
        self.c_thresh.setBandActive(dyn_on)
        self.c_attack.setBandActive(dyn_on and not auto_t)
        self.c_release.setBandActive(dyn_on and not auto_t)

        self._apply_card_style()
        self._updating = False

    def _emit_change(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.bandChanged.emit(self._data["index"], copy.deepcopy(self._data))

    def _on_toggle_band(self, checked: bool):
        self._data["enabled"] = checked
        self._emit_change()

    def _on_toggle_solo(self, checked: bool):
        self._data["solo"] = checked
        if not self._updating:
            self.soloToggled.emit(self._data["index"], checked)
            self.bandChanged.emit(self._data["index"], copy.deepcopy(self._data))

    def _on_shape_changed(self, shape: str):
        self._data["filter_type"] = shape
        self._emit_change()

    def _on_freq_knob(self, val: float):
        self._data["freq"] = round(val, 1)
        self._emit_change()

    def _on_freq_text(self, text: str):
        r_min, r_max, _, _ = RANGE_PRESETS[self._data["range_key"]]
        val = parse_freq(text, self._data["freq"])
        self._data["freq"] = max(r_min, min(r_max, val))
        self._emit_change()

    def _on_slope_clicked(self, slope: int):
        self._data["slope"] = slope
        self._emit_change()

    def _on_range_clicked(self, r_key: str):
        self._data["range_key"] = r_key
        r_min, r_max, _, _ = RANGE_PRESETS[r_key]
        self._data["freq"] = max(r_min, min(r_max, self._data["freq"]))
        self._emit_change()

    def _on_gain_changed(self, val: float):
        self._data["gain"] = round(val, 1)
        self._emit_change()

    def _on_q_changed(self, val: float):
        self._data["q"] = round(val, 1)
        self._emit_change()

    def _on_toggle_dyn(self, checked: bool):
        self._data["dyn_enabled"] = checked
        if checked and abs(self._data["dyn_range"]) < 0.1:
            self._data["dyn_range"] = -4.0
        self._emit_change()

    def _on_toggle_trigger(self, checked: bool):
        self._data["trigger_mode"] = "EXT" if checked else "INT"
        # 切换为外触发时自动激活 DYN
        if checked and not self._data.get("dyn_enabled", False):
            self._data["dyn_enabled"] = True
        self._emit_change()

    def _on_toggle_auto(self, checked: bool):
        self._data["auto_timing"] = checked
        self._emit_change()

    def _on_dyn_range_changed(self, val: float):
        self._data["dyn_range"] = round(val, 1)
        self._emit_change()

    def _on_thresh_changed(self, val: float):
        self._data["threshold"] = round(val, 1)
        self._emit_change()

    def _on_attack_changed(self, val: float):
        self._data["attack"] = round(val, 2)
        self._emit_change()

    def _on_release_changed(self, val: float):
        self._data["release"] = round(val, 1)
        self._emit_change()


# =====================================================================
# 9. 主面板：旗舰动态均衡器 (Studio Dynamic EQ)
# =====================================================================
class StudioDynamicEQWidget(QWidget):
    """
    动态均衡器主界面，提供完整信号接口供后端 DSP 引擎订阅：
      - eqBypassChanged(bool)
      - phaseModeChanged(str)                      # "Zero Latency" | "Natural Phase" | "Linear Phase"
      - sidechainConfigChanged(bool, str, bool)    # (ext_sc_enabled, source_name, sc_audition)
      - masterGainChanged(float)
      - bandChanged(int, dict)                     # (band_index 1~6, band_params)
      - bandSoloChanged(int, bool)
      - stateChanged(dict)                         # 完整参数快照
    """
    eqBypassChanged = pyqtSignal(bool)
    phaseModeChanged = pyqtSignal(str)
    sidechainConfigChanged = pyqtSignal(bool, str, bool)
    masterGainChanged = pyqtSignal(float)
    bandChanged = pyqtSignal(int, dict)
    bandSoloChanged = pyqtSignal(int, bool)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Backing Bus", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Dynamic EQ — {track_name}")
        self.resize(1140, 740)
        self.setMinimumSize(1020, 660)

        self._active_bank = "A"
        self._selected_band = 3
        self._banks = {
            "A": default_dynamic_eq_demo(),
            "B": default_dynamic_eq_flat(),
        }

        self._presets = {
            "Vocal Pocket Sidechain Duck": copy.deepcopy(self._banks["A"]),
            "Flat Neutral": copy.deepcopy(self._banks["B"]),
            "Vocal De-Harsh & Mud Tamer": {
                "enabled": True,
                "phase_mode": "Natural Phase",
                "ext_sc_enabled": False,
                "ext_sc_source": "Bus 1-2 (Vocal)",
                "sc_audition": False,
                "master_gain": 0.5,
                "bands": [
                    _make_dyn_band(1, True,  "HPF",        85.0,    0.0,  1.0, 18, False,  0.0, -24.0, 15.0, 120.0, False, "INT"),
                    _make_dyn_band(2, True,  "Bell",       210.0,  -1.0,  2.5, 12, True,  -5.0, -21.0, 16.0, 130.0, False, "INT"),
                    _make_dyn_band(3, True,  "Bell",       2900.0,  1.2,  3.2, 12, True,  -6.0, -23.0, 3.0,  75.0,  True,  "INT"),
                    _make_dyn_band(4, True,  "Bell",       6200.0,  0.0,  4.0, 12, True,  -7.5, -24.0, 0.8,  45.0,  False, "INT"),
                    _make_dyn_band(5, True,  "High Shelf", 10500.0, 1.8,  1.1, 12, True,   2.5, -28.0, 12.0, 120.0, False, "INT"),
                    _make_dyn_band(6, False, "LPF",        19500.0, 0.0,  1.0, 12, False,  0.0, -24.0, 10.0, 100.0, False, "INT"),
                ],
            },
            "Bass & Kick Groove Ducking": {
                "enabled": True,
                "phase_mode": "Zero Latency",
                "ext_sc_enabled": True,
                "ext_sc_source": "Bus 3-4 (Kick)",
                "sc_audition": False,
                "master_gain": 0.0,
                "bands": [
                    _make_dyn_band(1, True,  "HPF",        32.0,    0.0,  1.0, 24, False,  0.0, -24.0, 15.0, 120.0, False, "INT"),
                    _make_dyn_band(2, True,  "Bell",       62.0,    1.5,  2.0, 12, True,  -8.0, -22.0, 2.0,  95.0,  False, "EXT"),
                    _make_dyn_band(3, True,  "Bell",       135.0,   0.0,  2.4, 12, True,  -4.5, -20.0, 12.0, 115.0, False, "INT"),
                    _make_dyn_band(4, True,  "Bell",       850.0,   1.0,  1.5, 12, True,   3.0, -24.0, 8.0,  80.0,  False, "INT"),
                    _make_dyn_band(5, False, "High Shelf", 5000.0,  0.0,  1.1, 12, False,  0.0, -20.0, 10.0, 100.0, False, "INT"),
                    _make_dyn_band(6, False, "LPF",        18000.0, 0.0,  1.0, 12, False,  0.0, -24.0, 10.0, 100.0, False, "INT"),
                ],
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 演示动态模拟定时器（接入真实后端 DSP 数据后自动停止）
        self._anim_phase = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_simulation)
        self._demo_timer.start(40)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            FilterShapeSelector, ProDynamicEQGraphWidget, MasterOutputStrip,
            CompactLevelMeter, BandDynActivityBar {{
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
        # 1. 顶部主控栏（电源 + 预设 + A/B 对比 + 外部侧链总线路由 + 相位模式 + Reset）
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(38)
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True, "#28D8F0")
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"DYNAMIC EQ  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(8)

        # 预设管理
        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存为新预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = QComboBox()
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
        # 箭头按钮样式由 _update_top_bar_styles 统一设置（勿在入布局前绑定 objectName QSS，
        # 否则 Qt6 基线对齐分组会把它排到行底，相对 A/B 下沉 2px）

        tb_lay.addSpacing(10)

        # 外部侧链总线路由区 (External Sidechain Routing)
        self.ext_sc_btn = QPushButton("Ext SC")
        self.ext_sc_btn.setCheckable(True)
        self.ext_sc_btn.setFixedSize(50, 22)
        self.ext_sc_btn.setToolTip("启用外部侧链总线输入（用于人声闪避伴奏、底鼓避让贝斯等）")
        self.ext_sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ext_sc_btn.clicked.connect(self._on_ext_sc_toggled)
        tb_lay.addWidget(self.ext_sc_btn)

        self.sc_source_combo = QComboBox()
        self.sc_source_combo.setFixedWidth(122)
        self.sc_source_combo.addItems([
            "Bus 1-2 (Vocal)",
            "Bus 3-4 (Kick)",
            "Bus 5-6 (Snare)",
            "Sidechain In 1/2",
        ])
        self.sc_source_combo.setToolTip("选择外部侧链触发输入总线")
        self.sc_source_combo.currentTextChanged.connect(self._on_sc_source_changed)
        tb_lay.addWidget(self.sc_source_combo)

        self.sc_listen_btn = QPushButton("SC Listen")
        self.sc_listen_btn.setCheckable(True)
        self.sc_listen_btn.setFixedSize(62, 22)
        self.sc_listen_btn.setToolTip("监听侧链触发信号 (Sidechain Audition)")
        self.sc_listen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sc_listen_btn.clicked.connect(self._on_sc_audition_toggled)
        tb_lay.addWidget(self.sc_listen_btn)

        tb_lay.addStretch()

        # 相位与延迟模式
        self.phase_combo = QComboBox()
        self.phase_combo.setFixedWidth(104)
        self.phase_combo.addItems(["Zero Latency", "Natural Phase", "Linear Phase"])
        self.phase_combo.currentTextChanged.connect(self._on_phase_changed)
        tb_lay.addWidget(self.phase_combo)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(46, 22)
        reset_btn.clicked.connect(self.reset_eq)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心交互区：Input 电平表 | 动态频响与侧链图谱 | Output 推子与电平表
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

        # 中央动态 EQ 交互曲线画布
        self.eq_graph = ProDynamicEQGraphWidget()
        self.eq_graph.nodeDragged.connect(self._on_graph_node_dragged)
        self.eq_graph.nodeDynRangeDragged.connect(self._on_graph_dyn_range_dragged)
        self.eq_graph.nodeQScrolled.connect(self._on_graph_node_q_scrolled)
        self.eq_graph.nodeShapeRequested.connect(self._on_graph_node_shape)
        self.eq_graph.nodeDynToggled.connect(self._on_graph_node_dyn_toggled)
        self.eq_graph.nodeTriggerToggled.connect(self._on_graph_node_trigger_toggled)
        self.eq_graph.nodeClicked.connect(self._select_band_card)
        self.eq_graph.nodeToggleRequested.connect(self._on_graph_node_toggled)
        self.eq_graph.bandSpawnRequested.connect(self._on_graph_band_spawn)
        graph_row.addWidget(self.eq_graph, 1)

        # 右侧 Output 推子与电平表列
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
        self.master_strip.gainChanged.connect(self._on_master_gain_changed)
        out_col.addWidget(self.master_strip, 1)
        graph_row.addWidget(meter_col_frame)

        main_lay.addLayout(graph_row, 5)

        # -------------------------------------------------------------
        # 3. 底部 6 频段联合控制矩阵 (静态 Freq/Gain/Q + 动态 Range/Thresh/Attack/Release/Trigger)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        self.band_cards: list[DynamicBandCard] = []
        for b in self._current_state()["bands"]:
            card = DynamicBandCard(b)
            card.bandChanged.connect(self._on_band_card_changed)
            card.soloToggled.connect(self._on_band_solo_toggled)
            card.selected.connect(self._select_band_card)
            cards_row.addWidget(card, 1)
            self.band_cards.append(card)

        main_lay.addLayout(cards_row, 5)

    # -----------------------------------------------------------------
    # 状态管理与双向联动逻辑
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

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
            QPushButton:hover { background-color: #3C3C44; color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_ab if self._active_bank == "A" else inactive_ab)
        self.btn_b.setStyleSheet(active_ab if self._active_bank == "B" else inactive_ab)
        self.btn_copy_ab.setStyleSheet(inactive_ab)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

        ext_on = bool(st["ext_sc_enabled"])
        self.ext_sc_btn.setChecked(ext_on)
        self.ext_sc_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10.5px;
                font-weight: 700;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #3A2E18;
                color: #F0A848;
                border: 1px solid #F0A848;
            }
        """)
        self.sc_source_combo.setEnabled(ext_on)

        sc_aud = bool(st["sc_audition"])
        self.sc_listen_btn.setChecked(sc_aud)
        self.sc_listen_btn.setStyleSheet("""
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
        """)

    def _refresh_all_views(self):
        st = self._current_state()
        self._update_top_bar_styles()
        self.power_switch.setChecked(st["enabled"])

        self.sc_source_combo.blockSignals(True)
        self.sc_source_combo.setCurrentText(st["ext_sc_source"])
        self.sc_source_combo.blockSignals(False)

        self.phase_combo.blockSignals(True)
        self.phase_combo.setCurrentText(st["phase_mode"])
        self.phase_combo.blockSignals(False)

        self.master_strip.setGain(st["master_gain"])

        for i, card in enumerate(self.band_cards):
            card.syncFromData(st["bands"][i])
            card.setSelected(i == self._selected_band)

        self.eq_graph.setSelectedNode(self._selected_band)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])

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
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.eqBypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_ext_sc_toggled(self, checked: bool):
        st = self._current_state()
        st["ext_sc_enabled"] = checked
        self._update_top_bar_styles()
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.sidechainConfigChanged.emit(checked, st["ext_sc_source"], st["sc_audition"])
        self.stateChanged.emit(self.get_state())

    def _on_sc_source_changed(self, source: str):
        st = self._current_state()
        st["ext_sc_source"] = source
        self.sidechainConfigChanged.emit(st["ext_sc_enabled"], source, st["sc_audition"])
        self.stateChanged.emit(self.get_state())

    def _on_sc_audition_toggled(self, checked: bool):
        st = self._current_state()
        st["sc_audition"] = checked
        self._update_top_bar_styles()
        self.sidechainConfigChanged.emit(st["ext_sc_enabled"], st["ext_sc_source"], checked)
        self.stateChanged.emit(self.get_state())

    def _on_phase_changed(self, mode: str):
        st = self._current_state()
        st["phase_mode"] = mode
        self.phaseModeChanged.emit(mode)
        self.stateChanged.emit(self.get_state())

    def _on_master_gain_changed(self, gain: float):
        st = self._current_state()
        st["master_gain"] = gain
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.masterGainChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _select_band_card(self, idx: int):
        self._selected_band = idx
        for i, card in enumerate(self.band_cards):
            card.setSelected(i == idx)
        self.eq_graph.setSelectedNode(idx)

    def _on_band_card_changed(self, band_index: int, band_data: dict):
        st = self._current_state()
        st["bands"][band_index - 1] = band_data
        self._select_band_card(band_index - 1)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.bandChanged.emit(band_index, band_data)
        self.stateChanged.emit(self.get_state())

    def _on_band_solo_toggled(self, band_index: int, is_solo: bool):
        st = self._current_state()
        for i, b in enumerate(st["bands"]):
            if b["index"] != band_index and b.get("solo", False):
                b["solo"] = False
                self.band_cards[i].syncFromData(b)
        self.bandSoloChanged.emit(band_index, is_solo)

    def _on_graph_node_toggled(self, idx: int):
        st = self._current_state()
        b = st["bands"][idx]
        b["enabled"] = not b["enabled"]
        self.band_cards[idx].syncFromData(b)
        self._select_band_card(idx)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_band_spawn(self, freq: float, gain: float):
        st = self._current_state()
        disabled = [i for i, b in enumerate(st["bands"]) if not b["enabled"] and i in (1, 2, 3, 4)]
        if not disabled:
            disabled = [i for i, b in enumerate(st["bands"]) if not b["enabled"]]
        if not disabled:
            return

        target_idx = min(disabled, key=lambda i: abs(math.log10(st["bands"][i]["freq"] / max(20.0, freq))))
        b = st["bands"][target_idx]
        b["enabled"] = True
        b["freq"] = freq
        b["range_key"] = auto_range_key(freq)
        if b["filter_type"] not in ("HPF", "LPF"):
            b["gain"] = gain
        if b["filter_type"] in ("Bell", "Low Shelf", "High Shelf"):
            b["dyn_enabled"] = True
            if abs(b["dyn_range"]) < 0.1:
                b["dyn_range"] = -4.5
        self.band_cards[target_idx].syncFromData(b)
        self._select_band_card(target_idx)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_node_dragged(self, idx: int, freq: float, gain: float):
        st = self._current_state()
        b = st["bands"][idx]
        b["freq"] = round(freq, 1)
        b["gain"] = round(gain, 1)
        b["range_key"] = auto_range_key(b["freq"])
        self.band_cards[idx].syncFromData(b)
        self._select_band_card(idx)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_dyn_range_dragged(self, idx: int, dyn_range: float):
        st = self._current_state()
        b = st["bands"][idx]
        b["dyn_enabled"] = True
        b["dyn_range"] = round(dyn_range, 1)
        self.band_cards[idx].syncFromData(b)
        self._select_band_card(idx)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_node_q_scrolled(self, idx: int, new_q: float):
        st = self._current_state()
        b = st["bands"][idx]
        b["q"] = new_q
        self.band_cards[idx].syncFromData(b)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_node_shape(self, idx: int, new_shape: str):
        st = self._current_state()
        b = st["bands"][idx]
        b["filter_type"] = new_shape
        self.band_cards[idx].syncFromData(b)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_node_dyn_toggled(self, idx: int):
        st = self._current_state()
        b = st["bands"][idx]
        b["dyn_enabled"] = not b["dyn_enabled"]
        if b["dyn_enabled"] and abs(b["dyn_range"]) < 0.1:
            b["dyn_range"] = -4.0
        self.band_cards[idx].syncFromData(b)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_node_trigger_toggled(self, idx: int):
        st = self._current_state()
        b = st["bands"][idx]
        b["trigger_mode"] = "INT" if b["trigger_mode"] == "EXT" else "EXT"
        if b["trigger_mode"] == "EXT":
            b["dyn_enabled"] = True
        self.band_cards[idx].syncFromData(b)
        self.eq_graph.setState(st["bands"], st["enabled"], st["ext_sc_enabled"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
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
        name, ok = QInputDialog.getText(self, "保存 Dynamic EQ 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 演示动态模拟（展示超阈值动态压缩/侧链避让，接入真实引擎后自动停止）
    # -----------------------------------------------------------------
    def _tick_demo_simulation(self):
        self._anim_phase += 0.12
        st = self._current_state()

        # 模拟间歇性外部侧链人声能量包络 (0.0 ~ 1.0)
        sc_gate = max(0.0, math.sin(self._anim_phase * 0.55) * 0.7 + 0.3 * math.sin(self._anim_phase * 1.45))

        # 1. 计算各频段基于能量与 Threshold 的实时动态偏移量 (dB)
        dyn_offsets = [0.0] * 6
        for i, b in enumerate(st["bands"]):
            if not (st["enabled"] and b["enabled"] and b.get("dyn_enabled", False)):
                self.band_cards[i].setLiveDynOffset(0.0)
                continue

            if b.get("trigger_mode") == "EXT":
                if not st["ext_sc_enabled"]:
                    self.band_cards[i].setLiveDynOffset(0.0)
                    continue
                detector_db = -38.0 + 26.0 * sc_gate
            else:
                # 模拟各频段音乐能量波动
                band_pulse = 0.5 + 0.35 * math.sin(self._anim_phase * (0.9 + i * 0.25)) + 0.15 * math.cos(self._anim_phase * 1.9)
                detector_db = -34.0 + 24.0 * band_pulse

            over_db = max(0.0, detector_db - b["threshold"])
            ratio_factor = min(1.0, over_db / 14.0)
            offset = b["dyn_range"] * ratio_factor
            dyn_offsets[i] = offset
            self.band_cards[i].setLiveDynOffset(offset)

        self.eq_graph.update_dynamic_offsets(dyn_offsets)

        # 2. 更新主频谱 & 外部侧链频谱
        post, peak, sc_spec = [], [], []
        in_lvl = -60.0
        out_lvl = -60.0

        for i, f in enumerate(self.eq_graph._spec_freqs):
            decades = math.log10(max(100.0, f) / 100.0)
            base = -44.0 + 9.5 * math.exp(-((math.log10(f) - math.log10(110)) ** 2) / 0.08) if f < 100 else (-36.0 - 16.0 * decades)
            wave = (
                2.4 * math.sin(i * 0.24 + self._anim_phase)
                + 1.7 * math.cos(i * 0.61 - self._anim_phase * 1.3)
            )
            raw_db = base + wave
            in_lvl = max(in_lvl, raw_db)

            eq_offset = self.eq_graph._total_dynamic_db_at(f) if st["enabled"] else 0.0
            final_db = max(-82.0, min(-10.0, raw_db + eq_offset))
            post.append(final_db)
            peak.append(min(-8.0, max(raw_db + 2.0, final_db + 1.5)))
            out_lvl = max(out_lvl, final_db)

            # 外部侧链人声频谱 (集中在 300Hz ~ 5kHz)
            vocal_formant = math.exp(-((math.log10(max(40.0, f)) - math.log10(2400.0)) ** 2) / 0.22)
            sc_val = -76.0 + (48.0 * vocal_formant * sc_gate) + 1.8 * math.sin(i * 0.4 + self._anim_phase * 1.8)
            sc_spec.append(max(-82.0, min(-12.0, sc_val)))

        self.eq_graph.update_spectrum(self.eq_graph._spec_freqs, post, peak, sc_spec)
        self.in_meter.setLevel(in_lvl + 8.0)
        self.master_strip.setMeterLevel(out_lvl + 8.0)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_eq(self):
        self._banks[self._active_bank] = default_dynamic_eq_flat()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_spectrum(
        self,
        freqs_hz: list[float],
        post_db: list[float],
        peak_db: list[float] | None = None,
        sc_db: list[float] | None = None,
    ):
        """接入真实音频引擎 FFT 频谱数据 (调用后自动停止内置演示动画)"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.eq_graph.update_spectrum(freqs_hz, post_db, peak_db, sc_db)

    def update_dynamic_meters(
        self,
        input_db: float,
        output_db: float,
        band_dyn_offsets_db: list[float],
    ):
        """
        接入真实音频引擎实时电平与各频段动态增益变化量 (每帧 30~60Hz 调用)：
          - input_db: 输入峰值电平 (-60.0 ~ 0.0 dBFS)
          - output_db: 输出峰值电平 (-60.0 ~ 0.0 dBFS)
          - band_dyn_offsets_db: 长度 6 的列表，表示各频段当前的实时动态增益偏移量 (dB，压缩为负、扩展为正)
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.in_meter.setLevel(input_db)
        self.master_strip.setMeterLevel(output_db)
        self.eq_graph.update_dynamic_offsets(band_dyn_offsets_db)
        for i in range(min(6, len(band_dyn_offsets_db))):
            self.band_cards[i].setLiveDynOffset(band_dyn_offsets_db[i])


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioDynamicEQWidget("Backing Bus")
    win.show()
    sys.exit(app.exec())