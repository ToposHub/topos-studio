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
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont, QPolygonF
)


# =====================================================================
# 1. 全局色彩系统与声学常量
# =====================================================================
FREQ_MIN = 20.0
FREQ_MAX = 20000.0

# 与 AudioClipEditor-UI 统一的达芬奇 Fairlight 视觉系统：
# #27272B 哑光中性灰基底 + 1px #141416 纯黑细线切分，字体栈一致
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

# 6 频段专属高级音频色板（低眩光、高辨识度）
BAND_PALETTE = [
    "#F26D6D",  # Band 1: 珊瑚红 (Low Cut / Sub)
    "#F0A848",  # Band 2: 琥珀金 (Low / Bass)
    "#4CD490",  # Band 3: 翡翠绿 (Low-Mid)
    "#28D8F0",  # Band 4: 冰晶青 (High-Mid)
    "#6495FA",  # Band 5: 钴蓝   (Presence)
    "#B67CF8",  # Band 6: 紫罗兰 (High Cut / Air)
]

RANGE_PRESETS = {
    "L":  (30.0, 399.0, "30", "399"),
    "ML": (100.0, 1500.0, "100", "1.5K"),
    "MH": (450.0, 8000.0, "450", "8.0K"),
    "H":  (1400.0, 22000.0, "1.4K", "22.0K"),
}

# 自主命名的调音台滤波染色特性（替代达芬奇专有命名）
VOICING_Q_SCALE = {
    "Clean":   1.00,  # 线性精准数字
    "British": 1.18,  # 经典英式调音台比例 Q
    "Warm":    0.92,  # 宽厚模拟润色
    "Punch":   0.85,  # 现代紧凑聚焦
}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def freq_to_note(hz: float) -> str:
    if hz <= 0:
        return ""
    midi = round(69 + 12 * math.log2(hz / 440.0))
    name = NOTE_NAMES[midi % 12]
    octave = (midi // 12) - 1
    return f"{name}{octave}"


def format_freq(hz: float) -> str:
    if hz >= 1000.0:
        val = hz / 1000.0
        return f"{val:.1f}K"
    return f"{int(round(hz))}"


def parse_freq(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("HZ", "").strip()
    try:
        if s.endswith("K"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def auto_range_key(freq: float) -> str:
    """根据当前频率自动匹配最合适的旋钮精细调节区间"""
    if freq < 350.0:
        return "L"
    elif freq < 1400.0:
        return "ML"
    elif freq < 7500.0:
        return "MH"
    return "H"


def default_bands_demo() -> list[dict]:
    return [
        {"index": 1, "enabled": True,  "solo": False, "filter_type": "HPF",        "range_key": "L",  "freq": 65.0,    "gain": 0.0,  "q": 1.1, "slope": 12},
        {"index": 2, "enabled": True,  "solo": False, "filter_type": "Low Shelf",  "range_key": "L",  "freq": 110.0,   "gain": -4.5, "q": 1.4, "slope": 12},
        {"index": 3, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "ML", "freq": 320.0,   "gain": -2.2, "q": 2.1, "slope": 12},
        {"index": 4, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "MH", "freq": 2400.0,  "gain": 3.4,  "q": 1.4, "slope": 12},
        {"index": 5, "enabled": True,  "solo": False, "filter_type": "High Shelf", "range_key": "H",  "freq": 8200.0,  "gain": 2.0,  "q": 1.2, "slope": 12},
        {"index": 6, "enabled": False, "solo": False, "filter_type": "LPF",        "range_key": "H",  "freq": 19500.0, "gain": 0.0,  "q": 1.1, "slope": 12},
    ]


def default_bands_flat() -> list[dict]:
    return [
        {"index": 1, "enabled": False, "solo": False, "filter_type": "HPF",        "range_key": "L",  "freq": 70.0,    "gain": 0.0, "q": 1.1, "slope": 12},
        {"index": 2, "enabled": True,  "solo": False, "filter_type": "Low Shelf",  "range_key": "L",  "freq": 80.0,    "gain": 0.0, "q": 1.1, "slope": 12},
        {"index": 3, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "ML", "freq": 250.0,   "gain": 0.0, "q": 1.1, "slope": 12},
        {"index": 4, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "MH", "freq": 1500.0,  "gain": 0.0, "q": 1.1, "slope": 12},
        {"index": 5, "enabled": True,  "solo": False, "filter_type": "High Shelf", "range_key": "H",  "freq": 6000.0,  "gain": 0.0, "q": 1.1, "slope": 12},
        {"index": 6, "enabled": False, "solo": False, "filter_type": "LPF",        "range_key": "H",  "freq": 20000.0, "gain": 0.0, "q": 1.1, "slope": 12},
    ]


# =====================================================================
# 2. 原创极简声学曲线图标绘制 (现代连续曲线风格)
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
        # 现代连续高斯钟形曲线（区别于达芬奇的菱形符号）
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
# 3. 控件：现代发光电源开关
# =====================================================================
class PowerToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(34, 17)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, val: bool):
        if self._checked != val:
            self._checked = val
            self.update()
            self.toggled.emit(self._checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        bg = QColor("#12262E") if self._checked else QColor("#121215")
        border = QColor("#28D8F0") if self._checked else QColor("#36363D")
        p.setPen(QPen(border, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 8.5, 8.5)

        r = 5.2
        cy = self.height() / 2.0
        cx = (self.width() - 8.8) if self._checked else 8.8
        knob_color = QColor("#28D8F0") if self._checked else QColor("#666670")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob_color)
        p.drawEllipse(QPointF(cx, cy), r, r)


# =====================================================================
# 4. 控件：矢量滤波器形状选择器
# =====================================================================
class FilterShapeSelector(QPushButton):
    shapeChanged = pyqtSignal(str)

    def __init__(self, options: list[str], current: str, accent_color: str, parent=None):
        super().__init__(parent)
        self._options = options
        self._current = current
        self._accent = QColor(accent_color)
        self._active = True
        self.setFixedSize(50, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._open_menu)

    def setCurrentShape(self, shape: str):
        if shape in self._options and self._current != shape:
            self._current = shape
            self.update()

    def setBandActive(self, active: bool):
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
            QMenu::item {
                padding: 5px 16px;
                border-radius: 3px;
            }
            QMenu::item:selected {
                background-color: #35353D;
                color: #FFFFFF;
            }
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
        glyph_rect = QRectF(4, 2, self.width() - 16, self.height() - 4)
        draw_filter_glyph(p, glyph_rect, self._current, glyph_color)

        ax = self.width() - 8.5
        ay = self.height() / 2.0 - 1.2
        p.setPen(QPen(QColor("#9A9AA2"), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ax - 2.3, ay), QPointF(ax, ay + 2.5))
        p.drawLine(QPointF(ax, ay + 2.5), QPointF(ax + 2.3, ay))


# =====================================================================
# 5. 控件：内嵌可编辑数字胶囊
# =====================================================================
class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._active = True
        self.setFixedSize(54, 21)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(0, 0, 54, 21)
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
        self._text = text
        self.update()

    def setBandActive(self, active: bool):
        self._active = active
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._editor.setText(self._text.replace("+", ""))
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

        # 达芬奇插件风格：纯黑内凹数据框（与编辑器数值胶囊一致）
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(QColor("#121214"), 1.0))
        p.setBrush(QColor("#151518"))
        p.drawRoundedRect(rect, 3.0, 3.0)

        p.setPen(QColor("#ECECF0") if self._active else QColor("#6E6E76"))
        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


# =====================================================================
# 6. 控件：拉丝阳极铝质感旋钮 (带频段专属主题色光环)
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

    def setRange(self, minimum: float, maximum: float):
        self._min = minimum
        self._max = maximum
        self._value = max(self._min, min(self._max, self._value))
        self.update()

    def setBandActive(self, active: bool):
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
            # 右键直接复位默认值
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

        # 1. 深黑轨道底槽
        p.setPen(QPen(QColor("#141417"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性模式（如 Gain）在正上方 12 点钟方向绘制 0dB 基准刻度点
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 2. 频段主题色数值弧（克制单层，无外发光）
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
# 7. 控件：升级版交互式频响曲线图（单频段彩色包络 + 十字准星 + 频谱控制）
# =====================================================================
class ProEQGraphWidget(QWidget):
    nodeDragged = pyqtSignal(int, float, float)       # band_idx, freq, gain
    nodeQScrolled = pyqtSignal(int, float)            # band_idx, new_q
    nodeClicked = pyqtSignal(int)                     # band_idx
    nodeShapeRequested = pyqtSignal(int, str)         # band_idx, new_shape
    nodeToggleRequested = pyqtSignal(int)             # 新增：双击节点开关频段
    bandSpawnRequested = pyqtSignal(float, float)     # 新增：双击空白处激活频段 (freq, gain)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setMouseTracking(True)

        self._bands = default_bands_demo()
        self._eq_enabled = True
        self._eq_voicing = "Clean"
        self._master_gain = 0.0

        self._show_spectrum = True
        self._show_peak_hold = True

        self._hover_node = -1
        self._dragging_node = -1
        self._drag_start_freq = 0.0
        self._drag_start_gain = 0.0
        self._mouse_pos: QPointF | None = None

        self._ml = 36.0
        self._mr = 36.0
        self._mt = 10.0
        self._mb = 24.0

        # 悬浮控制按钮区域
        self._btn_spec_rect = QRectF()
        self._btn_peak_rect = QRectF()

        # 模拟频谱数据
        self._num_bins = 260
        self._spec_freqs = [
            FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** (i / (self._num_bins - 1)))
            for i in range(self._num_bins)
        ]
        self._spec_post = []
        self._spec_peak = []
        self._init_demo_spectrum()

    def _init_demo_spectrum(self):
        rng = random.Random(42)
        post, peak = [], []
        for f in self._spec_freqs:
            if f < 100:
                base = -46.0 + 9.0 * math.exp(-((math.log10(f) - math.log10(110)) ** 2) / 0.08)
            else:
                decades = math.log10(f / 100.0)
                base = -38.0 - 16.5 * decades
            ripple = 2.6 * math.sin(f * 0.032) + 2.0 * math.cos(f * 0.011) + rng.uniform(-3.5, 3.5)
            val = max(-82.0, min(-15.0, base + ripple))
            post.append(val)
            peak.append(min(-12.0, val + rng.uniform(0.5, 4.0)))
        self._spec_post = post
        self._spec_peak = peak

    def update_spectrum(self, freqs_hz: list[float], post_db: list[float], peak_db: list[float] | None = None):
        self._spec_freqs = freqs_hz
        self._spec_post = post_db
        self._spec_peak = peak_db if peak_db is not None else post_db
        self.update()

    def setState(self, bands: list[dict], eq_enabled: bool, eq_voicing: str, master_gain: float):
        self._bands = bands
        self._eq_enabled = eq_enabled
        self._eq_voicing = eq_voicing
        self._master_gain = master_gain
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
    # Biquad 频响计算
    # -----------------------------------------------------------------
    def _band_magnitude_db(self, band: dict, freq: float) -> float:
        if not band["enabled"]:
            return 0.0

        ftype = band["filter_type"]
        f0 = max(10.0, float(band["freq"]))
        gain = float(band["gain"])
        q_scale = VOICING_Q_SCALE.get(self._eq_voicing, 1.0)
        q = max(0.1, float(band["q"]) * q_scale)

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
        A = 10.0 ** (gain / 40.0)

        if ftype == "Bell":
            if abs(gain) < 1e-4:
                return 0.0
            b0, b1, b2 = 1.0 + alpha * A, -2.0 * cos_w0, 1.0 - alpha * A
            a0, a1, a2 = 1.0 + alpha / A, -2.0 * cos_w0, 1.0 - alpha / A
        elif ftype == "Low Shelf":
            if abs(gain) < 1e-4:
                return 0.0
            beta = 2.0 * math.sqrt(A) * alpha
            b0 = A * ((A + 1.0) - (A - 1.0) * cos_w0 + beta)
            b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w0)
            b2 = A * ((A + 1.0) - (A - 1.0) * cos_w0 - beta)
            a0 = (A + 1.0) + (A - 1.0) * cos_w0 + beta
            a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cos_w0)
            a2 = (A + 1.0) + (A - 1.0) * cos_w0 - beta
        elif ftype == "High Shelf":
            if abs(gain) < 1e-4:
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

    def _total_db_at(self, freq: float) -> float:
        if not self._eq_enabled:
            return 0.0
        total = self._master_gain
        for b in self._bands:
            total += self._band_magnitude_db(b, freq)
        return total

    def _node_pos(self, band: dict, plot: QRectF) -> QPointF:
        x = self._freq_to_x(band["freq"], plot)
        if band["filter_type"] in ("HPF", "LPF"):
            y = self._gain_to_y(self._master_gain, plot)
        else:
            y = self._gain_to_y(band["gain"] + self._master_gain, plot)
        return QPointF(x, y)

    # -----------------------------------------------------------------
    # 交互事件：节点拖拽 / 滚轮 Q / 右键菜单 / 十字准星
    # -----------------------------------------------------------------
    def _hit_test_node(self, pos: QPointF) -> int:
        plot = self._plot_rect()
        for idx in reversed(range(len(self._bands))):
            b = self._bands[idx]
            if not b["enabled"]:
                continue
            np = self._node_pos(b, plot)
            if math.hypot(pos.x() - np.x(), pos.y() - np.y()) <= 12.5:
                return idx
        return -1

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_node = -1
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_spec_rect.contains(pos):
                self._show_spectrum = not self._show_spectrum
                self.update()
                return
            if self._btn_peak_rect.contains(pos):
                self._show_peak_hold = not self._show_peak_hold
                self.update()
                return

            idx = self._hit_test_node(pos)
            if idx >= 0:
                self._dragging_node = idx
                self._drag_start_freq = self._bands[idx]["freq"]
                self._drag_start_gain = self._bands[idx]["gain"]
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.nodeClicked.emit(idx)
                self.update()

        elif event.button() == Qt.MouseButton.RightButton:
            idx = self._hit_test_node(pos)
            if idx >= 0:
                self._show_node_context_menu(idx, event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        plot = self._plot_rect()
        if not plot.contains(pos):
            return

        idx = self._hit_test_node(pos)
        if idx >= 0:
            # 双击节点：快速旁通/启用该频段
            self.nodeToggleRequested.emit(idx)
        else:
            # 双击空白处：自动唤醒最近的未启用频段并定位
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
        menu.exec(global_pos)

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()
        if self._dragging_node >= 0:
            b = self._bands[self._dragging_node]
            mods = event.modifiers()

            # 解除 RANGE_PRESETS 锁定，允许在全频谱 20Hz-20kHz 自由拖动
            if mods & Qt.KeyboardModifier.ControlModifier:
                new_freq = self._drag_start_freq  # Ctrl 锁定频率
            else:
                new_freq = self._x_to_freq(event.position().x(), plot)

            if b["filter_type"] in ("HPF", "LPF") or (mods & Qt.KeyboardModifier.AltModifier):
                new_gain = self._drag_start_gain  # Alt 锁定增益
            else:
                raw_g = self._y_to_gain(event.position().y(), plot) - self._master_gain
                new_gain = max(-20.0, min(20.0, round(raw_g, 1)))

            self.nodeDragged.emit(self._dragging_node, new_freq, new_gain)
        else:
            hit = self._hit_test_node(event.position())
            self._hover_node = hit
            self.setCursor(Qt.CursorShape.OpenHandCursor if hit >= 0 else Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_node >= 0:
            self._dragging_node = -1
            self.update()

    def wheelEvent(self, event):
        target = self._dragging_node if self._dragging_node >= 0 else self._hover_node
        if target >= 0:
            b = self._bands[target]
            delta = event.angleDelta().y() / 120.0
            new_q = max(0.3, min(10.3, round(b["q"] + delta * 0.2, 1)))
            self.nodeQScrolled.emit(target, new_q)
            event.accept()

    # -----------------------------------------------------------------
    # 绘制网格、频谱、单频段包络、总曲线、节点与 HUD
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 纯平 #1D1D20 中性深灰底（与编辑器波形面板一致）
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 水平刻度线
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

        # 3. 对数频率网格线
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

        # 4. 实时频谱仪
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
            spec_grad.setColorAt(0.0, QColor(56, 128, 210, 75))
            spec_grad.setColorAt(1.0, QColor(36, 72, 130, 18))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(spec_grad)
            p.drawPolygon(fill_poly)

            if self._show_peak_hold:
                p.setPen(QPen(QColor(150, 150, 158, 120), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(peak_path)

            p.setPen(QPen(QColor(82, 154, 235, 195), 1.15))
            p.drawPath(post_path)

        # 5. 各频段独立彩色包络填充（支持 Solo 隔离高亮）
        steps = 320
        zero_y = self._gain_to_y(self._master_gain, plot)
        solo_idx = next((i for i, b in enumerate(self._bands) if b["enabled"] and b.get("solo", False)), -1)

        if self._eq_enabled:
            # 当 Master Gain 不为 0 时，绘制一条淡淡的基准水平参考线
            if abs(self._master_gain) >= 0.05:
                p.setPen(QPen(QColor(255, 255, 255, 36), 1.0, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(plot.left(), zero_y), QPointF(plot.right(), zero_y))

            for idx_b, b in enumerate(self._bands):
                if not b["enabled"]:
                    continue
                is_solo = (idx_b == solo_idx)
                is_focus = (idx_b == self._hover_node or idx_b == self._dragging_node or is_solo)
                b_col = QColor(BAND_PALETTE[idx_b])

                if solo_idx >= 0 and not is_solo:
                    alpha, stroke_alpha = 6, 18
                else:
                    alpha = 54 if is_focus else 18
                    stroke_alpha = 140 if is_focus else 42

                b_fill = QPainterPath()
                for i in range(steps + 1):
                    norm = i / steps
                    f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
                    x = plot.left() + norm * plot.width()
                    db = self._master_gain + self._band_magnitude_db(b, f)
                    y = self._gain_to_y(db, plot)
                    if i == 0:
                        b_fill.moveTo(x, zero_y)
                        b_fill.lineTo(x, y)
                    else:
                        b_fill.lineTo(x, y)
                b_fill.lineTo(plot.right(), zero_y)
                b_fill.closeSubpath()

                p.setPen(QPen(QColor(b_col.red(), b_col.green(), b_col.blue(), stroke_alpha), 1.15 if is_solo else 1.0))
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), alpha))
                p.drawPath(b_fill)

        # 6. 总 EQ 响应主曲线 + 基准线渐变填充
        eq_path = QPainterPath()
        eq_area = QPainterPath()
        for i in range(steps + 1):
            norm = i / steps
            f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
            x = plot.left() + norm * plot.width()
            y = self._gain_to_y(self._total_db_at(f), plot)
            if i == 0:
                eq_path.moveTo(x, y)
                eq_area.moveTo(x, zero_y)
                eq_area.lineTo(x, y)
            else:
                eq_path.lineTo(x, y)
                eq_area.lineTo(x, y)
        eq_area.lineTo(plot.right(), zero_y)
        eq_area.closeSubpath()

        if self._eq_enabled:
            area_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            area_grad.setColorAt(0.0, QColor(240, 248, 255, 28))
            area_grad.setColorAt(0.5, QColor(240, 248, 255, 8))
            area_grad.setColorAt(1.0, QColor(240, 248, 255, 28))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(area_grad)
            p.drawPath(eq_area)

            p.setPen(QPen(QColor(235, 248, 255, 45), 4.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(eq_path)
            p.setPen(QPen(QColor("#F2F8FC"), 1.9))
            p.drawPath(eq_path)
        else:
            p.setPen(QPen(QColor("#686C78"), 1.6))
            p.drawPath(eq_path)

        # 7. 频段彩色控制节点（含 Solo 金色监听外环）
        node_font = QFont(MONO_FONT_FAM, 8)
        node_font.setWeight(QFont.Weight.Bold)
        p.setFont(node_font)

        for idx, b in enumerate(self._bands):
            if not b["enabled"]:
                continue
            pt = self._node_pos(b, plot)
            is_solo = (idx == solo_idx)
            dimmed = (solo_idx >= 0 and not is_solo)
            b_col = QColor(BAND_PALETTE[idx]) if (self._eq_enabled and not dimmed) else QColor("#565A66")
            is_hi = (idx == self._hover_node or idx == self._dragging_node or is_solo)
            radius = 10.2 if is_hi else 8.4

            if is_solo and self._eq_enabled:
                p.setPen(QPen(QColor("#F6CE58"), 1.4))
                p.setBrush(QColor(232, 184, 56, 55))
                p.drawEllipse(pt, radius + 5.5, radius + 5.5)
            elif is_hi and self._eq_enabled:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 65))
                p.drawEllipse(pt, radius + 4.5, radius + 4.5)

            p.setPen(QPen(b_col, 1.8))
            p.setBrush(QColor("#151518"))
            p.drawEllipse(pt, radius, radius)

            p.setPen(QColor("#ECECF0") if not dimmed else QColor("#8A8A92"))
            p.drawText(
                QRectF(pt.x() - radius, pt.y() - radius, radius * 2, radius * 2),
                Qt.AlignmentFlag.AlignCenter,
                str(b["index"]),
            )

        # 8. 右上角悬浮频谱开关胶囊 (SPEC / PEAK)
        self._btn_spec_rect = QRectF(plot.right() - 88, plot.top() + 8, 38, 18)
        self._btn_peak_rect = QRectF(plot.right() - 46, plot.top() + 8, 38, 18)
        for rect, label, active in (
            (self._btn_spec_rect, "SPEC", self._show_spectrum),
            (self._btn_peak_rect, "PEAK", self._show_peak_hold and self._show_spectrum),
        ):
            p.setPen(QPen(QColor("#24788C") if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 46, 56, 210) if active else QColor(21, 21, 24, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#787882"))
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 9. 实时十字准星 & 鼠标频率/音高/增益读数 HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(mx, plot.top()), QPointF(mx, plot.bottom()))
            p.drawLine(QPointF(plot.left(), my), QPointF(plot.right(), my))

            if self._dragging_node >= 0:
                db_band = self._bands[self._dragging_node]
                hud_str = f"Band {db_band['index']}  •  {format_freq(db_band['freq'])}Hz ({freq_to_note(db_band['freq'])})  •  {db_band['gain']:+.1f} dB  •  Q {db_band['q']:.1f}"
                hud_col = QColor(BAND_PALETTE[self._dragging_node])
            else:
                cur_f = self._x_to_freq(mx, plot)
                cur_g = self._y_to_gain(my, plot)
                hud_str = f"{format_freq(cur_f)}Hz ({freq_to_note(cur_f)})   {cur_g:+.1f} dB"
                hud_col = QColor("#B4B4BE")

            hud_w = 245 if self._dragging_node >= 0 else 165
            hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor(21, 21, 24, 215))
            p.drawRoundedRect(hud_rect, 3.5, 3.5)
            p.setPen(hud_col)
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 8. 控件：右侧 Master Output 推子 + 实时峰值电平表
# =====================================================================
class MasterOutputStrip(QWidget):
    gainChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gain = 0.0
        self._meter_db = -18.0
        self._dragging = False
        self._hovered = False
        self._clipped = False
        self.setFixedWidth(64)
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
        return QRectF(20.0, 10.0, 4.0, max(40.0, self.height() - 36.0))

    def _y_to_gain(self, y: float) -> float:
        tr = self._track_rect()
        norm = max(0.0, min(1.0, (y - tr.top()) / tr.height()))
        return 20.0 - norm * 40.0

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # 点击顶部削波灯区域清除削波状态
            if event.position().y() <= 14.0 and event.position().x() >= 40.0:
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

        # 刻度线
        p.setPen(QPen(QColor("#52525B"), 1.0))
        for i in range(5):
            ty = tr.top() + (i / 4.0) * tr.height()
            p.drawLine(QPointF(cx - 14, ty), QPointF(cx - 10, ty))
            p.drawLine(QPointF(cx + 10, ty), QPointF(cx + 14, ty))

        # 推子凹槽
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        # 右侧实时输出电平表 (Output Peak Meter)
        meter_rect = QRectF(46.0, tr.top(), 5.0, tr.height())
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

        # 顶部削波指示灯 (Clip LED)
        clip_rect = QRectF(46.0, tr.top() - 7.0, 5.0, 4.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)

        # 推子帽
        norm = (20.0 - self._gain) / 40.0
        cy = tr.top() + norm * tr.height()
        cap_w, cap_h = 18.0, 24.0
        cap_rect = QRectF(cx - cap_w / 2.0, cy - cap_h / 2.0, cap_w, cap_h)

        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawRoundedRect(cap_rect, 3.0, 3.0)

        # 青色中心发光指示条
        p.setPen(QPen(QColor("#28D8F0"), 1.6))
        p.drawLine(QPointF(cap_rect.left() + 3, cy), QPointF(cap_rect.right() - 3, cy))

        # 底部数值
        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor("#28D8F0"))
        val_str = f"{self._gain:+.1f}" if abs(self._gain) >= 0.05 else "0.0"
        p.drawText(QRectF(0, self.height() - 22, self.width(), 18), Qt.AlignmentFlag.AlignCenter, val_str)


# =====================================================================
# 9. 控件：单频段控制卡片 (带频段色条 + S 独奏监听按钮)
# =====================================================================
class ProBandCard(QFrame):
    bandChanged = pyqtSignal(int, dict)
    soloToggled = pyqtSignal(int, bool)

    def __init__(self, band_data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(band_data)
        self._idx = self._data["index"] - 1
        self._accent = BAND_PALETTE[self._idx]
        self._updating = False
        self._selected = False

        self.setObjectName("BandCard")
        # 达芬奇网格单元风格：无卡片描边/圆角，选中态频段色彩顶边，卡片之间用 1px 黑线切分，
        # 全部样式统一由 _apply_card_style 管理（init 末尾 syncFromData 会触发）

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- 1. 顶部栏：频段开关 + Solo 监听 + 滤波器形状 ---
        header = QWidget()
        header.setFixedHeight(36)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(7, 6, 7, 5)
        h_lay.setSpacing(4)

        self.band_btn = QPushButton(f"Band {self._data['index']}")
        self.band_btn.setCheckable(True)
        self.band_btn.setFixedHeight(22)
        self.band_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.band_btn.clicked.connect(self._on_toggle_band)
        h_lay.addWidget(self.band_btn, 1)

        # 新增：单频段 Solo 扫频监听按钮
        self.solo_btn = QPushButton("S")
        self.solo_btn.setCheckable(True)
        self.solo_btn.setFixedSize(22, 22)
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

        # --- 2. Frequency 区块 ---
        freq_sec = QFrame()
        freq_sec.setObjectName("SubSection")
        freq_lay = QVBoxLayout(freq_sec)
        freq_lay.setContentsMargins(8, 6, 8, 7)
        freq_lay.setSpacing(6)

        f_top = QHBoxLayout()
        f_left = QVBoxLayout()
        f_left.setSpacing(4)
        f_lbl = QLabel("Frequency")
        f_lbl.setObjectName("ParamTitle")
        self.freq_pill = EditableValuePill(format_freq(self._data["freq"]), self._accent)
        self.freq_pill.valueEdited.connect(self._on_freq_text)
        f_left.addWidget(f_lbl)
        f_left.addWidget(self.freq_pill)
        f_left.addStretch()

        f_right = QVBoxLayout()
        f_right.setSpacing(1)
        r_min, r_max, r_min_s, r_max_s = RANGE_PRESETS[self._data["range_key"]]
        self.freq_knob = ProRotaryKnob(r_min, r_max, self._data["freq"], self._data["freq"], self._accent, mode="log")
        self.freq_knob.valueChanged.connect(self._on_freq_knob)

        f_minmax = QHBoxLayout()
        f_minmax.setSpacing(4)
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
        freq_lay.addLayout(f_top)

        self.seg_row = QHBoxLayout()
        self.seg_row.setSpacing(2)
        self.slope_btns: dict[int, QPushButton] = {}
        self.range_btns: dict[str, QPushButton] = {}

        for s_val in (6, 12, 18, 24):
            btn = QPushButton(f"{s_val}dB")
            btn.setObjectName("SegBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, sv=s_val: self._on_slope_clicked(sv))
            self.seg_row.addWidget(btn)
            self.slope_btns[s_val] = btn

        for r_key in ("L", "ML", "MH", "H"):
            btn = QPushButton(r_key)
            btn.setObjectName("SegBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, rk=r_key: self._on_range_clicked(rk))
            self.seg_row.addWidget(btn)
            self.range_btns[r_key] = btn

        freq_lay.addLayout(self.seg_row)
        root.addWidget(freq_sec)

        # --- 3. Gain 区块 ---
        self.gain_sec = QFrame()
        self.gain_sec.setObjectName("SubSection")
        g_lay = QHBoxLayout(self.gain_sec)
        g_lay.setContentsMargins(8, 6, 8, 6)

        g_left = QVBoxLayout()
        g_left.setSpacing(4)
        g_lbl = QLabel("Gain")
        g_lbl.setObjectName("ParamTitle")
        self.gain_pill = EditableValuePill("0.0", self._accent)
        self.gain_pill.valueEdited.connect(self._on_gain_text)
        g_left.addWidget(g_lbl)
        g_left.addWidget(self.gain_pill)
        g_left.addStretch()

        g_right = QVBoxLayout()
        g_right.setSpacing(1)
        self.gain_knob = ProRotaryKnob(-20.0, 20.0, self._data["gain"], 0.0, self._accent, mode="bipolar")
        self.gain_knob.valueChanged.connect(self._on_gain_knob)

        g_hints = QHBoxLayout()
        g_hints.setSpacing(4)
        for t in ("-20", "dB", "+20"):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            g_hints.addWidget(lbl)

        g_right.addWidget(self.gain_knob, 0, Qt.AlignmentFlag.AlignCenter)
        g_right.addLayout(g_hints)

        g_lay.addLayout(g_left)
        g_lay.addStretch()
        g_lay.addLayout(g_right)
        root.addWidget(self.gain_sec)

        # --- 4. Q Factor 区块 ---
        self.q_sec = QFrame()
        self.q_sec.setObjectName("SubSection")
        q_lay = QHBoxLayout(self.q_sec)
        q_lay.setContentsMargins(8, 6, 8, 7)

        q_left = QVBoxLayout()
        q_left.setSpacing(4)
        q_lbl = QLabel("Q Factor")
        q_lbl.setObjectName("ParamTitle")
        self.q_pill = EditableValuePill("1.1", self._accent)
        self.q_pill.valueEdited.connect(self._on_q_text)
        q_left.addWidget(q_lbl)
        q_left.addWidget(self.q_pill)
        q_left.addStretch()

        q_right = QVBoxLayout()
        q_right.setSpacing(1)
        self.q_knob = ProRotaryKnob(0.3, 10.3, self._data["q"], 1.1, self._accent, mode="linear")
        self.q_knob.valueChanged.connect(self._on_q_knob)

        q_hints = QHBoxLayout()
        q_hints.setSpacing(12)
        for t in ("0.3", "10.3"):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            q_hints.addWidget(lbl)

        q_right.addWidget(self.q_knob, 0, Qt.AlignmentFlag.AlignCenter)
        q_right.addLayout(q_hints)

        q_lay.addLayout(q_left)
        q_lay.addStretch()
        q_lay.addLayout(q_right)
        root.addWidget(self.q_sec)

        self.bottom_spacer = QFrame()
        self.bottom_spacer.setObjectName("SubSection")
        self.bottom_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.bottom_spacer)

        # 横向分割线由 paintEvent 统一绘制（贯穿整卡宽度，与竖线完全连通）；
        # 这里登记各子区块引用，绘制时按其顶边 y 取线位
        self.freq_sec = freq_sec
        self._section_refs = [self.freq_sec, self.gain_sec, self.q_sec, self.bottom_spacer]

        self.syncFromData(self._data)

    def paintEvent(self, event):
        super().paintEvent(event)  # 先画 QSS 底色与边缘竖线
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 横向分割线：取每个可见子区块的顶边 y，横跨整卡宽度，
        # 保证与左右竖向黑线在交点完全连通，不再出现 3px 细缝
        p.setPen(QPen(QColor("#141416"), 1.0))
        for sec in self._section_refs:
            if sec.isVisible():
                y = sec.y() + 0.5
                p.drawLine(QPointF(0.0, y), QPointF(float(self.width()), y))

    def setSelected(self, selected: bool):
        self._selected = selected
        self._apply_card_style()

    def _apply_card_style(self):
        """达芬奇网格单元风格：无卡片描边与圆角，频段之间由 1px 黑线切分；
        选中态用频段主题色彩条顶边标识（对齐 multiband_comp BandCard 彩顶边），禁用态整体压暗"""
        en = bool(self._data["enabled"])
        sel = getattr(self, "_selected", False)
        top_bar = f"2px solid {self._accent}" if (sel and en) else "1px solid #141416"

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
                color: {"#9A9AA2" if en else "#6E6E76"};
                font-size: 10px;
                font-weight: 600;
            }}
            QLabel#RangeHint {{
                color: {"#787882" if en else "#55555C"};
                font-size: 9px;
            }}
            QPushButton#SegBtn {{
                background-color: #323238;
                color: {"#CFCFD6" if en else "#8A8A92"};
                border: none;
                border-radius: 3px;
                font-size: 10px;
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

        self.gain_sec.setVisible(not is_cut)
        self.q_sec.setVisible(not is_cut)
        self.bottom_spacer.setVisible(is_cut)
        self.update()  # 子区块可见性变化后重画横向分割线

        g = self._data["gain"]
        self.gain_knob.setValue(g)
        self.gain_knob.setBandActive(en)
        self.gain_pill.setText(f"{g:+.1f}" if abs(g) >= 0.05 else "0.0")
        self.gain_pill.setBandActive(en)

        q = self._data["q"]
        self.q_knob.setValue(q)
        self.q_knob.setBandActive(en)
        self.q_pill.setText(f"{q:.1f}")
        self.q_pill.setBandActive(en)

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

    def _on_gain_knob(self, val: float):
        self._data["gain"] = round(val, 1)
        self._emit_change()

    def _on_gain_text(self, text: str):
        try:
            val = float(text.strip())
            self._data["gain"] = max(-20.0, min(20.0, round(val, 1)))
            self._emit_change()
        except ValueError:
            pass

    def _on_q_knob(self, val: float):
        self._data["q"] = round(val, 1)
        self._emit_change()

    def _on_q_text(self, text: str):
        try:
            val = float(text.strip())
            self._data["q"] = max(0.3, min(10.3, round(val, 1)))
            self._emit_change()
        except ValueError:
            pass


# =====================================================================
# 10. 主面板：自主旗舰参数均衡器 (Studio Parametric EQ)
# =====================================================================
class StudioParametricEQWidget(QWidget):
    # 对接后端音频引擎的完整信号接口
    eqBypassChanged = pyqtSignal(bool)
    eqVoicingChanged = pyqtSignal(str)
    masterGainChanged = pyqtSignal(float)
    bandChanged = pyqtSignal(int, dict)
    bandSoloChanged = pyqtSignal(int, bool)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Audio 1", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Parametric EQ — {track_name}")
        self.resize(980, 690)
        self.setMinimumSize(880, 610)

        self._active_bank = "A"
        self._banks = {
            "A": {"enabled": True, "voicing": "Clean", "master_gain": 0.0, "bands": default_bands_demo()},
            "B": {"enabled": True, "voicing": "British", "master_gain": 0.0, "bands": default_bands_flat()},
        }

        self._presets = {
            "Vocal Presence": copy.deepcopy(self._banks["A"]),
            "Flat Neutral": copy.deepcopy(self._banks["B"]),
            "Dialogue De-Mud": {
                "enabled": True, "voicing": "Clean", "master_gain": 0.5,
                "bands": [
                    {"index": 1, "enabled": True,  "solo": False, "filter_type": "HPF",        "range_key": "L",  "freq": 90.0,    "gain": 0.0,  "q": 1.1, "slope": 18},
                    {"index": 2, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "L",  "freq": 230.0,   "gain": -3.5, "q": 2.4, "slope": 12},
                    {"index": 3, "enabled": False, "solo": False, "filter_type": "Bell",       "range_key": "ML", "freq": 550.0,   "gain": 0.0,  "q": 1.1, "slope": 12},
                    {"index": 4, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "MH", "freq": 3400.0,  "gain": 2.6,  "q": 1.6, "slope": 12},
                    {"index": 5, "enabled": True,  "solo": False, "filter_type": "High Shelf", "range_key": "H",  "freq": 9000.0,  "gain": 1.5,  "q": 1.1, "slope": 12},
                    {"index": 6, "enabled": False, "solo": False, "filter_type": "LPF",        "range_key": "H",  "freq": 18500.0, "gain": 0.0,  "q": 1.1, "slope": 12},
                ],
            },
            "Radio / Lo-Fi": {
                "enabled": True, "voicing": "Punch", "master_gain": 2.0,
                "bands": [
                    {"index": 1, "enabled": True,  "solo": False, "filter_type": "HPF",        "range_key": "L",  "freq": 360.0,   "gain": 0.0,  "q": 1.1, "slope": 24},
                    {"index": 2, "enabled": False, "solo": False, "filter_type": "Low Shelf",  "range_key": "L",  "freq": 80.0,    "gain": 0.0,  "q": 1.1, "slope": 12},
                    {"index": 3, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "ML", "freq": 1200.0,  "gain": 5.0,  "q": 2.0, "slope": 12},
                    {"index": 4, "enabled": False, "solo": False, "filter_type": "Bell",       "range_key": "MH", "freq": 2500.0,  "gain": 0.0,  "q": 1.1, "slope": 12},
                    {"index": 5, "enabled": False, "solo": False, "filter_type": "High Shelf", "range_key": "H",  "freq": 6000.0,  "gain": 0.0,  "q": 1.1, "slope": 12},
                    {"index": 6, "enabled": True,  "solo": False, "filter_type": "LPF",        "range_key": "H",  "freq": 3500.0,  "gain": 0.0,  "q": 1.1, "slope": 24},
                ],
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        self._anim_phase = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_spectrum)
        self._demo_timer.start(45)

    def _setup_ui(self, track_name: str):
        # 达芬奇 Fairlight 视觉系统（与 AudioClipEditor-UI 完全一致）：
        # #27272B 哑光中性灰基底 + 1px #141416 黑线切分，功能区不再使用卡片容器
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            FilterShapeSelector, ProEQGraphWidget, MasterOutputStrip {{
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
        # 1. 顶部整合工具栏（电源 + 轨道名 + 预设 + A/B 对比 + 染色模式 + 重置）
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(38)
        # 样式限定在 #TopBar 自身：裸 QFrame 选择器会渗透子级容器（剪辑编辑器工具栏同款教训）
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True)
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"PARAMETRIC EQ  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(12)

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

        # 染色特性模式 (Voicing)
        v_lbl = QLabel("Voicing")
        v_lbl.setStyleSheet("color: #9A9AA2; font-size: 11px; font-weight: 600;")
        tb_lay.addWidget(v_lbl)

        self.voicing_combo = QComboBox()
        self.voicing_combo.setFixedWidth(84)
        self.voicing_combo.addItems(["Clean", "British", "Warm", "Punch"])
        self.voicing_combo.currentTextChanged.connect(self._on_voicing_changed)
        tb_lay.addWidget(self.voicing_combo)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_eq)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部主图形区：交互频响曲线图 + 右侧 Output 推子与电平表
        # -------------------------------------------------------------
        graph_row = QHBoxLayout()
        graph_row.setContentsMargins(0, 0, 0, 0)
        graph_row.setSpacing(0)

        self.eq_graph = ProEQGraphWidget()
        self.eq_graph.nodeDragged.connect(self._on_graph_node_dragged)
        self.eq_graph.nodeQScrolled.connect(self._on_graph_node_q_scrolled)
        self.eq_graph.nodeShapeRequested.connect(self._on_graph_node_shape)
        self.eq_graph.nodeClicked.connect(self._select_band_card)
        self.eq_graph.nodeToggleRequested.connect(self._on_graph_node_toggled)
        self.eq_graph.bandSpawnRequested.connect(self._on_graph_band_spawn)
        graph_row.addWidget(self.eq_graph, 1)

        # 右侧输出监控列（#222226 平底 + 左侧 1px 黑线，与剪辑编辑器电平列一致）
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
        # 3. 底部 6 频段参数区（去卡片化：单元格贴合，1px 黑线竖向切分）
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        self.band_cards: list[ProBandCard] = []
        for b in self._current_state()["bands"]:
            card = ProBandCard(b)
            card.bandChanged.connect(self._on_band_card_changed)
            card.soloToggled.connect(self._on_band_solo_toggled)
            cards_row.addWidget(card, 1)
            self.band_cards.append(card)

        main_lay.addLayout(cards_row, 4)

    # -----------------------------------------------------------------
    # 状态管理与联动逻辑
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_ab_styles(self):
        # 编辑器同款：选中态深青底 + 青字细青边，未选中态纯平无描边
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
            QPushButton:hover { background-color: #3C3C44; color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_css if self._active_bank == "A" else inactive_css)
        self.btn_b.setStyleSheet(active_css if self._active_bank == "B" else inactive_css)
        self.btn_copy_ab.setStyleSheet(inactive_css)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

    def _refresh_all_views(self):
        st = self._current_state()
        self._update_ab_styles()
        self.power_switch.setChecked(st["enabled"])
        self.voicing_combo.blockSignals(True)
        self.voicing_combo.setCurrentText(st["voicing"])
        self.voicing_combo.blockSignals(False)
        self.master_strip.setGain(st["master_gain"])

        for i, card in enumerate(self.band_cards):
            card.syncFromData(st["bands"][i])

        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])

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
        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])
        self.eqBypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_voicing_changed(self, voicing: str):
        st = self._current_state()
        st["voicing"] = voicing
        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])
        self.eqVoicingChanged.emit(voicing)
        self.stateChanged.emit(self.get_state())

    def _on_master_gain_changed(self, gain: float):
        st = self._current_state()
        st["master_gain"] = gain
        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])
        self.masterGainChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _on_band_card_changed(self, band_index: int, band_data: dict):
        st = self._current_state()
        st["bands"][band_index - 1] = band_data
        self._select_band_card(band_index - 1)
        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])
        self.bandChanged.emit(band_index, band_data)
        self.stateChanged.emit(self.get_state())

    def _on_band_solo_toggled(self, band_index: int, is_solo: bool):
        # 互斥 Solo 逻辑：开启某频段独奏时自动关闭其他频段 Solo
        st = self._current_state()
        for i, b in enumerate(st["bands"]):
            if b["index"] != band_index and b.get("solo", False):
                b["solo"] = False
                self.band_cards[i].syncFromData(b)
        self.bandSoloChanged.emit(band_index, is_solo)

    def _select_band_card(self, idx: int):
        for i, card in enumerate(self.band_cards):
            card.setSelected(i == idx)

    def _on_graph_node_toggled(self, idx: int):
        st = self._current_state()
        b = st["bands"][idx]
        b["enabled"] = not b["enabled"]
        self.band_cards[idx].syncFromData(b)
        self._select_band_card(idx)
        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_band_spawn(self, freq: float, gain: float):
        st = self._current_state()
        # 优先寻找未启用的中间频段 (Band 2~5)，其次找所有未启用频段
        disabled = [i for i, b in enumerate(st["bands"]) if not b["enabled"] and i in (1, 2, 3, 4)]
        if not disabled:
            disabled = [i for i, b in enumerate(st["bands"]) if not b["enabled"]]
        if not disabled:
            return

        # 选择默认频率最接近点击位置的未启用频段
        target_idx = min(disabled, key=lambda i: abs(math.log10(st["bands"][i]["freq"] / max(20.0, freq))))
        b = st["bands"][target_idx]
        b["enabled"] = True
        b["freq"] = freq
        b["range_key"] = auto_range_key(freq)
        if b["filter_type"] not in ("HPF", "LPF"):
            b["gain"] = gain
        self.band_cards[target_idx].syncFromData(b)
        self._select_band_card(target_idx)
        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_node_dragged(self, idx: int, freq: float, gain: float):
        st = self._current_state()
        b = st["bands"][idx]
        b["freq"] = round(freq, 1)
        b["gain"] = round(gain, 1)
        # 自动同步 L / ML / MH / H 范围档位
        b["range_key"] = auto_range_key(b["freq"])
        self.band_cards[idx].syncFromData(b)
        self._select_band_card(idx)
        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_node_q_scrolled(self, idx: int, new_q: float):
        st = self._current_state()
        b = st["bands"][idx]
        b["q"] = new_q
        self.band_cards[idx].syncFromData(b)
        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_node_shape(self, idx: int, new_shape: str):
        st = self._current_state()
        b = st["bands"][idx]
        b["filter_type"] = new_shape
        self.band_cards[idx].syncFromData(b)
        self.eq_graph.setState(st["bands"], st["enabled"], st["voicing"], st["master_gain"])
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
        name, ok = QInputDialog.getText(self, "保存 EQ 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    def _tick_demo_spectrum(self):
        self._anim_phase += 0.14
        st = self._current_state()
        post, peak = [], []
        max_lvl = -60.0
        for i, f in enumerate(self.eq_graph._spec_freqs):
            decades = math.log10(max(100.0, f) / 100.0)
            base = -46.0 + 9.0 * math.exp(-((math.log10(f) - math.log10(110)) ** 2) / 0.08) if f < 100 else (-38.0 - 16.5 * decades)
            wave = (
                2.5 * math.sin(i * 0.25 + self._anim_phase)
                + 1.8 * math.cos(i * 0.62 - self._anim_phase * 1.4)
                + 1.1 * math.sin(i * 1.3 + self._anim_phase * 0.7)
            )
            raw_db = base + wave
            eq_offset = self.eq_graph._total_db_at(f) if st["enabled"] else 0.0
            final_db = max(-82.0, min(-12.0, raw_db + eq_offset))
            post.append(final_db)
            peak.append(min(-10.0, max(raw_db + 2.5, final_db + 1.8)))
            if final_db > max_lvl:
                max_lvl = final_db

        self.eq_graph.update_spectrum(self.eq_graph._spec_freqs, post, peak)
        self.master_strip.setMeterLevel(max_lvl + 8.0)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_eq(self):
        self._banks[self._active_bank]["master_gain"] = 0.0
        self._banks[self._active_bank]["bands"] = default_bands_flat()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_spectrum(self, freqs_hz: list[float], post_db: list[float], peak_db: list[float] | None = None):
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.eq_graph.update_spectrum(freqs_hz, post_db, peak_db)

    def update_output_meter(self, level_db: float):
        """接入真实音频引擎输出峰值电平 (-60.0 ~ 0.0 dBFS)"""
        self.master_strip.setMeterLevel(level_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioParametricEQWidget("Audio 1")
    win.show()
    sys.exit(app.exec())