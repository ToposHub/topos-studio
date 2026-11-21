from __future__ import annotations

import sys
import math
import copy
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QInputDialog, QMenu, QDoubleSpinBox
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer, QSignalBlocker
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QLinearGradient,
    QPainterPath, QFont, QPolygonF
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

from ..panel_glyphs import GlyphComboBox

# =====================================================================
# 1. 全局色彩系统与声学常量 (与 EQ-UI 保持一致)
# =====================================================================
FREQ_MIN = 20.0
FREQ_MAX = 20000.0

DETECTOR_FREQ_MIN = 1000.0
DETECTOR_FREQ_MAX = 16000.0

UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

# 语义功能色板（低眩光、高辨识度）
ACCENT_CYAN = "#28D8F0"    # 频率 / 激活主色
ACCENT_AMBER = "#F0A848"   # 侧链检测 / 阈值色
ACCENT_CORAL = "#F26D6D"   # 增益衰减 (GR) / Max Range 色
ACCENT_GREEN = "#4CD490"   # 包络时间 / 安全电平色

# 人声常见齿音快捷定位预设
SIBILANCE_TARGETS = [
    ("5.5K", 5500.0, "sh / ch 宽齿音"),
    ("7.0K", 7000.0, "s 尖锐齿音"),
    ("8.5K", 8500.0, "高频气声嘶响"),
]

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
        return f"{hz / 1000.0:.1f}K"
    return f"{int(round(hz))}"


def parse_freq(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("HZ", "").strip()
    try:
        if s.endswith("K"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def default_deesser_state() -> dict:
    return {
        "enabled": True,
        "mode": "split",           # "split" (分频式) | "sidechain" (侧链式)
        "filter_shape": "Bandpass",  # "Bandpass" | "High Shelf"
        "listen": False,           # 侧链齿音独奏监听
        "lookahead": 2.0,          # 0.0 / 2.0 / 5.0 ms
        "freq": 6800.0,            # 2000 ~ 16000 Hz (专治 5-9kHz)
        "q": 2.2,                  # 0.5 ~ 8.0
        "threshold": -24.0,        # -60.0 ~ 0.0 dBFS
        "range": 12.0,             # 0.0 ~ 24.0 dB (最大衰减深度)
        "attack": 1.5,             # 0.1 ~ 20.0 ms
        "release": 65.0,           # 10.0 ~ 300.0 ms
        "output_gain": 0.0,        # -12.0 ~ +12.0 dB
    }


# =====================================================================
# 2. 原创极简检测滤波器矢量图标绘制
# =====================================================================
def draw_detector_glyph(painter: QPainter, rect: QRectF, shape: str, color: QColor):
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
    if shape == "High Shelf":
        path.moveTo(left, bot)
        path.lineTo(cx - 3.0, bot)
        path.cubicTo(cx, bot, cx, top, cx + 3.0, top)
        path.lineTo(right, top)
    else:  # "Bandpass"
        path.moveTo(left, bot - 1.0)
        path.cubicTo(cx - 4.5, bot - 1.0, cx - 3.5, top, cx, top)
        path.cubicTo(cx + 3.5, top, cx + 4.5, bot - 1.0, right, bot - 1.0)

    painter.drawPath(path)
    painter.restore()


# =====================================================================
# 3. 基础控件：电源开关 / 形状选择器 / 可编辑数值胶囊 / 旋钮
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
        border = QColor(ACCENT_CYAN) if self._checked else QColor("#36363D")
        p.setPen(QPen(border, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 8.5, 8.5)

        r = 5.2
        cy = self.height() / 2.0
        cx = (self.width() - 8.8) if self._checked else 8.8
        knob_color = QColor(ACCENT_CYAN) if self._checked else QColor("#666670")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob_color)
        p.drawEllipse(QPointF(cx, cy), r, r)


class DetectorShapeSelector(GlyphComboBox):
    shapeChanged = pyqtSignal(str)

    def __init__(self, options: list[str], current: str, accent_color: str = ACCENT_AMBER, parent: QWidget | None = None) -> None:
        super().__init__(options, current, accent_color, draw_detector_glyph, parent)
        self.setAccessibleName("Detector shape")
        self.setMinimumWidth(124)
        self.currentTextChanged.connect(self.shapeChanged)

    def setCurrentShape(self, shape: str) -> None:
        self.set_choice(shape)

    def setActive(self, active: bool) -> None:
        self.setEnabled(active)


class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = ACCENT_CYAN, width: int = 58, parent=None):
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

    def setActive(self, active: bool):
        self._active = active
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._active:
            raw = self._text.replace("+", "").replace("dB", "").replace("ms", "").strip()
            self._editor.setText(raw)
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
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value: float,
        default_value: float,
        accent_color: str = ACCENT_CYAN,
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
        clamped = max(self._min, min(self._max, val))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def setActive(self, active: bool):
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

        arc_r = 15.6
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 深黑轨道底槽
        p.setPen(QPen(QColor("#141417"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

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

        knob_r = 11.6
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.4
        iy1 = cy - math.sin(rad) * 3.4
        ix2 = cx + math.cos(rad) * (knob_r - 1.5)
        iy2 = cy - math.sin(rad) * (knob_r - 1.5)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 4. 控件：交互式齿音检测与动态衰减频谱图
# =====================================================================
class DeEsserGraphWidget(QWidget):
    nodeDragged = pyqtSignal(float, float)         # freq_hz, threshold_db
    nodeQScrolled = pyqtSignal(float)              # new_q
    modeToggleRequested = pyqtSignal(str)          # "split" | "sidechain"
    shapeToggleRequested = pyqtSignal(str)         # "Bandpass" | "High Shelf"
    listenToggleRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(250)
        self.setMouseTracking(True)

        self._state = default_deesser_state()
        self._processor_controls = False
        self._current_gr_db = 0.0   # 实时衰减量 (负数，例如 -5.4 dB)
        self._current_sc_db = -36.0 # 实时侧链检测电平

        self._show_spectrum = True
        self._show_peak_hold = True

        self._hover_node = False
        self._dragging_node = False
        self._drag_start_freq = 6800.0
        self._drag_start_thresh = -24.0
        self._mouse_pos: QPointF | None = None

        self._ml = 36.0
        self._mr = 38.0
        self._mt = 10.0
        self._mb = 24.0

        self._btn_spec_rect = QRectF()
        self._btn_peak_rect = QRectF()

        self._num_bins = 260
        self._spec_freqs = [
            FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** (i / (self._num_bins - 1)))
            for i in range(self._num_bins)
        ]
        self._spec_in = [-65.0] * self._num_bins
        self._spec_out = [-65.0] * self._num_bins
        self._spec_peak = [-60.0] * self._num_bins

    def setState(self, state: dict):
        self._state = copy.deepcopy(state)
        self.update()

    def setDynamicsTelemetry(self, sc_db: float, gr_db: float):
        self._current_sc_db = sc_db
        self._current_gr_db = min(0.0, gr_db)
        self.update()

    def update_spectrum(
        self,
        freqs_hz: list[float],
        input_db: list[float],
        output_db: list[float] | None = None,
        peak_db: list[float] | None = None,
    ):
        self._spec_freqs = freqs_hz
        self._spec_in = input_db
        self._spec_out = output_db if output_db is not None else input_db
        self._spec_peak = peak_db if peak_db is not None else input_db
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

    def _spec_db_to_y(self, db: float, plot: QRectF) -> float:
        # 左侧刻度：0 dBFS (顶) -> -80 dBFS (底)
        norm = (0.0 - db) / 80.0
        return plot.top() + max(0.0, min(1.0, norm)) * plot.height()

    def _y_to_spec_db(self, y: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (y - plot.top()) / plot.height()))
        return 0.0 - norm * 80.0

    def _gr_db_to_y(self, gr_db: float, plot: QRectF) -> float:
        # 右侧刻度：0 dB GR (顶) -> -24 dB GR (底)
        g = max(-24.0, min(0.0, gr_db))
        norm = abs(g) / 24.0
        return plot.top() + norm * plot.height()

    def _detector_weight(self, freq: float) -> float:
        """计算检测滤波器在指定频率的归一化权重 (0.0 ~ 1.0)"""
        f0 = max(100.0, float(self._state["freq"]))
        q = max(0.2, float(self._state["q"]))
        shape = self._state["filter_shape"]

        if shape == "High Shelf":
            # 平滑高架过渡权重
            ratio = math.log2(max(1.0, freq) / f0)
            steepness = 1.6 + q * 0.55
            return 1.0 / (1.0 + math.exp(-steepness * ratio * 3.0))
        else:
            # 钟形带通权重 (对称对数高斯响应)
            octaves = math.log2(max(1.0, freq) / f0)
            bw = 0.85 / q
            return math.exp(-0.5 * ((octaves / max(0.08, bw)) ** 2))

    def _node_pos(self, plot: QRectF) -> QPointF:
        x = self._freq_to_x(self._state["freq"], plot)
        y = self._spec_db_to_y(self._state["threshold"], plot)
        return QPointF(x, y)

    def _hit_test_node(self, pos: QPointF) -> bool:
        np = self._node_pos(self._plot_rect())
        return math.hypot(pos.x() - np.x(), pos.y() - np.y()) <= 13.0

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_node = False
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

            if self._hit_test_node(pos):
                self._dragging_node = True
                self._drag_start_freq = self._state["freq"]
                self._drag_start_thresh = self._state["threshold"]
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                return

            # 点击绘图区空白处也可快速定位检测频率与阈值
            plot = self._plot_rect()
            if plot.contains(pos):
                new_f = max(DETECTOR_FREQ_MIN, min(DETECTOR_FREQ_MAX, self._x_to_freq(pos.x(), plot)))
                new_t = max(-60.0, min(0.0, round(self._y_to_spec_db(pos.y(), plot), 1)))
                self._dragging_node = True
                self._drag_start_freq = new_f
                self._drag_start_thresh = new_t
                self.nodeDragged.emit(round(new_f, 1), new_t)

        elif event.button() == Qt.MouseButton.RightButton:
            if self._plot_rect().contains(pos):
                self._show_context_menu(event.globalPosition().toPoint())

    def _show_context_menu(self, global_pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #1D1D21; border: 1px solid #141416; color: #DCDCE2; font-size: 11px; }
            QMenu::item { padding: 5px 16px; }
            QMenu::item:selected { background-color: #35353D; color: #FFFFFF; }
        """)
        act_split = menu.addAction("Mode: Split-Band (分频式)")
        act_split.triggered.connect(lambda: self.modeToggleRequested.emit("split"))

        act_sc = menu.addAction("Mode: Wide-Band" if self._processor_controls else "Mode: Sidechain (侧链式)")
        act_sc.triggered.connect(lambda: self.modeToggleRequested.emit("sidechain"))

        if not self._processor_controls:
            menu.addSeparator()
            act_bp = menu.addAction("Detector: Bandpass (钟形带通)")
            act_bp.triggered.connect(lambda: self.shapeToggleRequested.emit("Bandpass"))
            act_hs = menu.addAction("Detector: High Shelf (高架式)")
            act_hs.triggered.connect(lambda: self.shapeToggleRequested.emit("High Shelf"))

        menu.addSeparator()
        act_listen = menu.addAction("Toggle Listen Sidechain (监听齿音)")
        act_listen.triggered.connect(self.listenToggleRequested.emit)
        menu.exec(global_pos)

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()
        if self._dragging_node:
            mods = event.modifiers()
            if mods & Qt.KeyboardModifier.ControlModifier:
                new_freq = self._drag_start_freq
            else:
                raw_f = self._x_to_freq(event.position().x(), plot)
                new_freq = max(DETECTOR_FREQ_MIN, min(DETECTOR_FREQ_MAX, raw_f))

            if mods & Qt.KeyboardModifier.AltModifier:
                new_thresh = self._drag_start_thresh
            else:
                raw_t = self._y_to_spec_db(event.position().y(), plot)
                new_thresh = max(-60.0, min(0.0, round(raw_t, 1)))

            self.nodeDragged.emit(round(new_freq, 1), new_thresh)
        else:
            hit = self._hit_test_node(event.position())
            self._hover_node = hit
            self.setCursor(Qt.CursorShape.OpenHandCursor if hit else Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_node:
            self._dragging_node = False
            self.update()

    def wheelEvent(self, event):
        if self._plot_rect().contains(event.position()):
            delta = event.angleDelta().y() / 120.0
            new_q = max(0.5, min(8.0, round(self._state["q"] + delta * 0.2, 1)))
            self.nodeQScrolled.emit(new_q)
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()
        en = bool(self._state["enabled"])

        # 1. 绘图区纯平深灰底 (#1D1D20)
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 水平刻度线（左侧：频谱/阈值 dBFS；右侧：Gain Reduction dB）
        grid_pen = QPen(QColor("#26262A"), 1.0)
        zero_pen = QPen(QColor("#3A3A40"), 1.1)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        left_ticks = [0, -10, -20, -30, -40, -50, -60, -70, -80]
        right_ticks = ["0", "-3", "-6", "-9", "-12", "-15", "-18", "-21", "-24"]

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
            p.setPen(QColor("#9A6A6A") if i > 0 else QColor("#9A9AA2"))
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

        # 4. 5–9 kHz 人声齿音重点治理区微光背景带
        x_5k = self._freq_to_x(5000.0, plot)
        x_9k = self._freq_to_x(9000.0, plot)
        sib_rect = QRectF(x_5k, plot.top(), x_9k - x_5k, plot.height())
        p.fillRect(sib_rect, QColor(240, 168, 72, 11))
        p.setPen(QPen(QColor(240, 168, 72, 34), 1.0, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(x_5k, plot.top()), QPointF(x_5k, plot.bottom()))
        p.drawLine(QPointF(x_9k, plot.top()), QPointF(x_9k, plot.bottom()))

        p.setFont(QFont(MONO_FONT_FAM, 7))
        p.setPen(QColor(240, 168, 72, 110))
        p.drawText(
            QRectF(x_5k, plot.bottom() - 18, x_9k - x_5k, 14),
            Qt.AlignmentFlag.AlignCenter,
            "5–9k SIBILANCE",
        )

        # 5. 实时频谱仪（输入与处理后对比）
        if self._show_spectrum and len(self._spec_freqs) > 1:
            peak_path = QPainterPath()
            in_path = QPainterPath()
            out_path = QPainterPath()
            fill_poly = QPolygonF()
            first_x = self._freq_to_x(self._spec_freqs[0], plot)
            fill_poly.append(QPointF(first_x, plot.bottom()))

            for idx_s, f_s in enumerate(self._spec_freqs):
                sx = self._freq_to_x(f_s, plot)
                py = self._spec_db_to_y(self._spec_peak[idx_s], plot)
                iy = self._spec_db_to_y(self._spec_in[idx_s], plot)
                oy = self._spec_db_to_y(self._spec_out[idx_s], plot)
                if idx_s == 0:
                    peak_path.moveTo(sx, py)
                    in_path.moveTo(sx, iy)
                    out_path.moveTo(sx, oy)
                else:
                    peak_path.lineTo(sx, py)
                    in_path.lineTo(sx, iy)
                    out_path.lineTo(sx, oy)
                fill_poly.append(QPointF(sx, oy))

            last_x = self._freq_to_x(self._spec_freqs[-1], plot)
            fill_poly.append(QPointF(last_x, plot.bottom()))

            spec_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            spec_grad.setColorAt(0.0, QColor(56, 128, 210, 75))
            spec_grad.setColorAt(1.0, QColor(36, 72, 130, 18))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(spec_grad)
            p.drawPolygon(fill_poly)

            # 当存在增益衰减时，用浅灰虚线保留原始齿音峰值轮廓对比
            if en and self._current_gr_db < -0.2:
                p.setPen(QPen(QColor(242, 109, 109, 115), 1.0, Qt.PenStyle.DotLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(in_path)

            if self._show_peak_hold:
                p.setPen(QPen(QColor(150, 150, 158, 110), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(peak_path)

            p.setPen(QPen(QColor(82, 154, 235, 200), 1.15))
            p.drawPath(out_path)

        # 6. 侧链检测滤波器包络（以 Threshold 为顶点的检测权重曲线）
        steps = 280
        thresh_y = self._spec_db_to_y(self._state["threshold"], plot)
        det_col = QColor(ACCENT_AMBER) if en else QColor("#565A66")

        det_fill = QPainterPath()
        det_stroke = QPainterPath()
        for i in range(steps + 1):
            norm = i / steps
            f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
            x = plot.left() + norm * plot.width()
            w = self._detector_weight(f)
            # 权重=1 时贴合 Threshold 线，权重=0 时落回底端
            y = plot.bottom() - w * (plot.bottom() - thresh_y)
            if i == 0:
                det_fill.moveTo(x, plot.bottom())
                det_fill.lineTo(x, y)
                det_stroke.moveTo(x, y)
            else:
                det_fill.lineTo(x, y)
                det_stroke.lineTo(x, y)
        det_fill.lineTo(plot.right(), plot.bottom())
        det_fill.closeSubpath()

        fill_alpha = 42 if (self._hover_node or self._dragging_node) else 22
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(det_col.red(), det_col.green(), det_col.blue(), fill_alpha if en else 10))
        p.drawPath(det_fill)

        p.setPen(QPen(QColor(det_col.red(), det_col.green(), det_col.blue(), 175 if en else 70), 1.35))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(det_stroke)

        # 7. Threshold 水平参考线
        p.setPen(QPen(QColor(det_col.red(), det_col.green(), det_col.blue(), 130 if en else 50), 1.0, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(plot.left(), thresh_y), QPointF(plot.right(), thresh_y))

        # 8. 动态增益衰减曲线 (Split-Band 分频局部下凹 vs Sidechain 宽带整体下压)
        if en:
            gr_path = QPainterPath()
            gr_area = QPainterPath()
            is_split = (self._state["mode"] == "split")

            for i in range(steps + 1):
                norm = i / steps
                f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
                x = plot.left() + norm * plot.width()
                local_gr = self._current_gr_db * (self._detector_weight(f) if is_split else 1.0)
                y = self._gr_db_to_y(local_gr, plot)
                if i == 0:
                    gr_area.moveTo(x, plot.top())
                    gr_area.lineTo(x, y)
                    gr_path.moveTo(x, y)
                else:
                    gr_area.lineTo(x, y)
                    gr_path.lineTo(x, y)
            gr_area.lineTo(plot.right(), plot.top())
            gr_area.closeSubpath()

            if self._current_gr_db < -0.05:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(242, 109, 109, 44))
                p.drawPath(gr_area)

            p.setPen(QPen(QColor("#F26D6D"), 1.8))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(gr_path)

        # 9. 核心控制节点 (Freq × Threshold)
        pt = self._node_pos(plot)
        is_hi = (self._hover_node or self._dragging_node)
        radius = 10.5 if is_hi else 8.8
        node_col = QColor(ACCENT_AMBER) if en else QColor("#565A66")

        if is_hi and en:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(node_col.red(), node_col.green(), node_col.blue(), 65))
            p.drawEllipse(pt, radius + 4.8, radius + 4.8)

        p.setPen(QPen(node_col, 1.9))
        p.setBrush(QColor("#151518"))
        p.drawEllipse(pt, radius, radius)

        p.setPen(QColor("#ECECF0") if en else QColor("#8A8A92"))
        nf = QFont(MONO_FONT_FAM, 8)
        nf.setWeight(QFont.Weight.Bold)
        p.setFont(nf)
        p.drawText(QRectF(pt.x() - radius, pt.y() - radius, radius * 2, radius * 2), Qt.AlignmentFlag.AlignCenter, "DS")

        # 10. 右上角 SPEC / PEAK 悬浮按钮 + 当前模式状态标签
        self._btn_spec_rect = QRectF(plot.right() - 88, plot.top() + 8, 38, 18)
        self._btn_peak_rect = QRectF(plot.right() - 46, plot.top() + 8, 38, 18)
        for rect, label, active in (
            (self._btn_spec_rect, "SPEC", self._show_spectrum),
            (self._btn_peak_rect, "PEAK", self._show_peak_hold and self._show_spectrum),
        ):
            p.setPen(QPen(QColor("#24788C") if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 46, 56, 210) if active else QColor(21, 21, 24, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor(ACCENT_CYAN) if active else QColor("#787882"))
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 11. 左上角实时 HUD 读数胶囊
        mode_tag = "SPLIT-BAND" if self._state["mode"] == "split" else "SIDECHAIN"
        if self._dragging_node:
            hud_str = f"{mode_tag}  •  {format_freq(self._state['freq'])}Hz ({freq_to_note(self._state['freq'])})  •  Thresh {self._state['threshold']:.1f} dB  •  Q {self._state['q']:.1f}"
            hud_col = QColor(ACCENT_AMBER)
            hud_w = 325
        elif self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(mx, plot.top()), QPointF(mx, plot.bottom()))
            p.drawLine(QPointF(plot.left(), my), QPointF(plot.right(), my))
            cur_f = self._x_to_freq(mx, plot)
            cur_db = self._y_to_spec_db(my, plot)
            hud_str = f"{format_freq(cur_f)}Hz ({freq_to_note(cur_f)})   {cur_db:.1f} dBFS"
            hud_col = QColor("#B4B4BE")
            hud_w = 185
        else:
            gr_txt = f"GR {self._current_gr_db:.1f} dB" if self._current_gr_db < -0.05 else "GR 0.0 dB"
            hud_str = f"{mode_tag}  •  {format_freq(self._state['freq'])}Hz  •  {gr_txt}"
            hud_col = QColor(ACCENT_CORAL) if self._current_gr_db < -0.5 else QColor("#B4B4BE")
            hud_w = 235

        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(hud_col)
        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 5. 控件：右侧三联动态监测电平表 (SC 侧链 / GR 齿音衰减 / OUT 输出)
# =====================================================================
class DeEsserMeterStrip(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._sc_db = -38.0
        self._thresh_db = -24.0
        self._gr_db = 0.0
        self._gr_peak_db = 0.0
        self._out_db = -18.0
        self._clipped = False
        self.setFixedWidth(78)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击重置 GR 峰值与削波指示灯")

    def setTelemetry(self, sc_db: float, thresh_db: float, gr_db: float, out_db: float):
        self._sc_db = max(-60.0, min(0.0, sc_db))
        self._thresh_db = max(-60.0, min(0.0, thresh_db))
        self._gr_db = max(-24.0, min(0.0, gr_db))
        if self._gr_db < self._gr_peak_db:
            self._gr_peak_db = self._gr_db
        self._out_db = max(-60.0, min(0.0, out_db))
        if out_db >= -0.1:
            self._clipped = True
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._gr_peak_db = 0.0
            self._clipped = False
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_y = 22.0
        bot_y = max(60.0, self.height() - 28.0)
        bar_h = bot_y - top_y
        bar_w = 8.0

        cols = [
            ("SC", 14.0),
            ("GR", 35.0),
            ("OUT", 56.0),
        ]

        lbl_font = QFont(MONO_FONT_FAM, 7)
        lbl_font.setWeight(QFont.Weight.Bold)
        p.setFont(lbl_font)

        for name, x in cols:
            p.setPen(QColor("#8A8A94"))
            p.drawText(QRectF(x - 6, 4, bar_w + 12, 14), Qt.AlignmentFlag.AlignCenter, name)

            bg_rect = QRectF(x, top_y, bar_w, bar_h)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor("#0E0E11"))
            p.drawRoundedRect(bg_rect, 2.0, 2.0)

        # 1. SC 侧链检测表 (自底向上，琥珀金)
        sc_norm = (self._sc_db + 60.0) / 60.0
        if sc_norm > 0.01:
            h = bar_h * sc_norm
            r = QRectF(14.5, bot_y - h, bar_w - 1.0, h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT_AMBER))
            p.drawRoundedRect(r, 1.5, 1.5)

        # SC 表上的 Threshold 触发刻度线
        th_norm = (self._thresh_db + 60.0) / 60.0
        th_y = bot_y - bar_h * th_norm
        p.setPen(QPen(QColor("#FFFFFF"), 1.5))
        p.drawLine(QPointF(12.0, th_y), QPointF(24.0, th_y))

        # 2. GR 增益衰减表 (自顶向下，珊瑚红)
        gr_norm = abs(self._gr_db) / 24.0
        if gr_norm > 0.005:
            h = bar_h * gr_norm
            r = QRectF(35.5, top_y, bar_w - 1.0, h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT_CORAL))
            p.drawRoundedRect(r, 1.5, 1.5)

        # GR 峰值保持横线
        if self._gr_peak_db < -0.1:
            pk_y = top_y + bar_h * (abs(self._gr_peak_db) / 24.0)
            p.setPen(QPen(QColor("#FF9E9E"), 1.4))
            p.drawLine(QPointF(35.0, pk_y), QPointF(43.0, pk_y))

        # 3. OUT 输出电平表 (自底向上，翡翠绿->黄->红)
        out_norm = (self._out_db + 60.0) / 60.0
        if out_norm > 0.01:
            h = bar_h * out_norm
            r = QRectF(56.5, bot_y - h, bar_w - 1.0, h)
            grad = QLinearGradient(QPointF(0, bot_y), QPointF(0, top_y))
            grad.setColorAt(0.0, QColor("#28C882"))
            grad.setColorAt(0.75, QColor("#E8B838"))
            grad.setColorAt(1.0, QColor("#F05050"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(r, 1.5, 1.5)

        # 削波灯
        clip_rect = QRectF(56.0, top_y - 6.0, bar_w, 3.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)

        # 底部 GR 峰值数值显示
        val_font = QFont(MONO_FONT_FAM, 8)
        val_font.setWeight(QFont.Weight.Bold)
        p.setFont(val_font)
        p.setPen(QColor(ACCENT_CORAL) if self._gr_peak_db < -0.2 else QColor("#9A9AA2"))
        gr_str = f"{self._gr_peak_db:.1f}dB" if self._gr_peak_db < -0.05 else "0.0dB"
        p.drawText(QRectF(0, self.height() - 22, self.width(), 18), Qt.AlignmentFlag.AlignCenter, gr_str)


# =====================================================================
# 6. 控件：底部参数单元基类 (1px #141416 黑线切分，无冗余外框)
# =====================================================================
class ParameterCellFrame(QFrame):
    def __init__(self, title: str, accent_color: str, is_last: bool = False, parent=None):
        super().__init__(parent)
        self._accent = accent_color
        self._is_last = is_last
        self._section_refs: list[QWidget] = []

        self.setObjectName("ParamCell")
        right_border = "none" if is_last else "1px solid #141416"
        self.setStyleSheet(f"""
            QFrame#ParamCell {{
                background-color: #27272B;
                border: none;
                border-top: 1px solid #141416;
                border-right: {right_border};
                border-radius: 0px;
            }}
            QLabel#CellHeader {{
                color: {accent_color};
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 0.7px;
            }}
            QLabel#ParamTitle {{
                color: #A8A8B2;
                font-size: 10px;
                font-weight: 600;
            }}
            QLabel#RangeHint {{
                color: #787882;
                font-size: 9px;
            }}
            QPushButton#SegBtn {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
                padding: 0px 4px;
            }}
            QPushButton#SegBtn:hover {{
                background-color: #3C3C44;
                color: #FFFFFF;
            }}
            QPushButton#SegBtn:checked {{
                background-color: #182E38;
                color: {accent_color};
                border: 1px solid {accent_color};
            }}
        """)

        self.root_lay = QVBoxLayout(self)
        self.root_lay.setContentsMargins(0, 0, 0, 0)
        self.root_lay.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(30)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 4, 10, 2)

        dot = QLabel("▪")
        dot.setStyleSheet(f"color: {accent_color}; font-size: 11px;")
        h_lay.addWidget(dot)

        lbl = QLabel(title.upper())
        lbl.setObjectName("CellHeader")
        h_lay.addWidget(lbl)
        h_lay.addStretch()

        self.root_lay.addWidget(header)

    def register_divider_above(self, widget: QWidget):
        self._section_refs.append(widget)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setPen(QPen(QColor("#141416"), 1.0))
        for sec in self._section_refs:
            if sec.isVisible():
                y = sec.y() + 0.5
                p.drawLine(QPointF(0.0, y), QPointF(float(self.width()), y))


# =====================================================================
# 7. 主面板：自主旗舰齿音消除器 (Studio De-Esser)
# =====================================================================
class StudioDeEsserWidget(QWidget):
    # 对接后端音频引擎的完整信号接口
    bypassChanged = pyqtSignal(bool)
    modeChanged = pyqtSignal(str)          # "split" | "sidechain"
    filterShapeChanged = pyqtSignal(str)   # "Bandpass" | "High Shelf"
    listenChanged = pyqtSignal(bool)       # 侧链独奏监听
    lookaheadChanged = pyqtSignal(float)   # 0.0 / 2.0 / 5.0 ms
    freqChanged = pyqtSignal(float)        # 2000.0 ~ 16000.0 Hz
    qChanged = pyqtSignal(float)           # 0.5 ~ 8.0
    thresholdChanged = pyqtSignal(float)   # -60.0 ~ 0.0 dBFS
    rangeChanged = pyqtSignal(float)       # 0.0 ~ 24.0 dB
    attackChanged = pyqtSignal(float)      # 0.1 ~ 20.0 ms
    releaseChanged = pyqtSignal(float)     # 10.0 ~ 300.0 ms
    outputGainChanged = pyqtSignal(float)  # -12.0 ~ +12.0 dB
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Vocal 1", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"De-Esser — {track_name}")
        self.resize(940, 630)
        self.setMinimumSize(860, 570)

        self._active_bank = "A"
        self._banks = {
            "A": default_deesser_state(),
            "B": {
                **default_deesser_state(),
                "mode": "sidechain",
                "freq": 6400.0,
                "threshold": -25.0,
                "range": 10.0,
            },
        }

        self._presets = {
            "Vocal Sibilance (5–9kHz)": default_deesser_state(),
            "Female Bright Vocal": {
                **default_deesser_state(),
                "mode": "split", "filter_shape": "Bandpass",
                "freq": 7800.0, "q": 2.8, "threshold": -26.0, "range": 14.0,
                "attack": 1.0, "release": 55.0,
            },
            "Male Voiceover / Podcast": {
                **default_deesser_state(),
                "mode": "split", "filter_shape": "Bandpass",
                "freq": 5600.0, "q": 1.9, "threshold": -22.5, "range": 10.0,
                "attack": 2.0, "release": 75.0,
            },
            "Sidechain Transparent": {
                **default_deesser_state(),
                "mode": "sidechain", "filter_shape": "Bandpass",
                "freq": 6500.0, "q": 2.0, "threshold": -25.0, "range": 9.0,
                "attack": 1.2, "release": 80.0,
            },
            "Harsh High-End Tamer": {
                **default_deesser_state(),
                "mode": "split", "filter_shape": "High Shelf",
                "freq": 6000.0, "q": 1.2, "threshold": -23.0, "range": 11.0,
                "attack": 1.5, "release": 70.0,
            },
        }

        self._updating = False
        self._setup_ui(track_name)
        self._refresh_all_views()

        # 独立运行时的拟真语音齿音脉冲演示定时器（接入真实后端后自动停止）
        self._anim_phase = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_animation)
        self._demo_timer.start(45)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            DetectorShapeSelector, DeEsserGraphWidget, DeEsserMeterStrip {{
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
        # 1. 顶部整合工具栏 (电源 + 标题 + 预设 + A/B 对比 + Listen + Reset)
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(38)
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True)
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"DE-ESSER  ·  {track_name}")
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
        self.btn_copy_ab.setToolTip("复制当前参数到另一对比槽")
        self.btn_copy_ab.clicked.connect(self._copy_bank)

        self.btn_b = QPushButton("B")
        self.btn_b.setFixedSize(24, 22)
        self.btn_b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_b.clicked.connect(lambda: self._switch_bank("B"))

        ab_lay.addWidget(self.btn_a)
        ab_lay.addWidget(self.btn_copy_ab)
        ab_lay.addWidget(self.btn_b)
        tb_lay.addLayout(ab_lay)
        # 箭头按钮样式由 _update_ab_and_listen_styles 统一设置（勿在入布局前绑定 objectName QSS，
        # 否则 Qt6 基线对齐分组会把它排到行底，相对 A/B 下沉 2px）

        tb_lay.addStretch()

        # 侧链监听快捷按钮 (Listen SC)
        self.top_listen_btn = QPushButton("🎧 Listen SC")
        self.top_listen_btn.setCheckable(True)
        self.top_listen_btn.setFixedHeight(22)
        self.top_listen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.top_listen_btn.setToolTip("监听孤立后的 5–9kHz 齿音检测信号 (Sidechain Audition)")
        self.top_listen_btn.clicked.connect(self._on_listen_toggled)
        tb_lay.addWidget(self.top_listen_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_deesser)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部主图形区：交互频谱/检测曲线图 + 右侧 SC/GR/OUT 电平监控列
        # -------------------------------------------------------------
        graph_row = QHBoxLayout()
        graph_row.setContentsMargins(0, 0, 0, 0)
        graph_row.setSpacing(0)

        self.graph = DeEsserGraphWidget()
        self.graph.nodeDragged.connect(self._on_graph_node_dragged)
        self.graph.nodeQScrolled.connect(self._on_q_changed)
        self.graph.modeToggleRequested.connect(self._on_mode_selected)
        self.graph.shapeToggleRequested.connect(self._on_shape_selected)
        self.graph.listenToggleRequested.connect(lambda: self._on_listen_toggled(not self._current_state()["listen"]))
        graph_row.addWidget(self.graph, 1)

        meter_col_frame = QFrame()
        meter_col_frame.setObjectName("MeterCol")
        meter_col_frame.setStyleSheet("QFrame#MeterCol { background-color: #222226; border-left: 1px solid #141416; }")
        m_lay = QVBoxLayout(meter_col_frame)
        m_lay.setContentsMargins(4, 6, 4, 6)
        m_lay.setSpacing(0)

        self.meter_strip = DeEsserMeterStrip()
        m_lay.addWidget(self.meter_strip, 1)
        graph_row.addWidget(meter_col_frame)

        main_lay.addLayout(graph_row, 5)

        # -------------------------------------------------------------
        # 3. 底部四栏参数控制区 (去卡片化，1px #141416 黑线切分)
        # -------------------------------------------------------------
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(0)

        # === 栏 1：模式与检测器 (MODE & DETECTOR) ===
        cell_mode = ParameterCellFrame("Mode & Detector", ACCENT_CYAN, is_last=False)

        mode_sec = QWidget()
        mode_lay = QVBoxLayout(mode_sec)
        mode_lay.setContentsMargins(10, 6, 10, 8)
        mode_lay.setSpacing(6)

        m_lbl = QLabel("Processing Mode (处理模式)")
        m_lbl.setObjectName("ParamTitle")
        mode_lay.addWidget(m_lbl)

        mode_btn_row = QHBoxLayout()
        mode_btn_row.setSpacing(4)
        self.btn_split = QPushButton("Split-Band 分频")
        self.btn_split.setObjectName("SegBtn")
        self.btn_split.setCheckable(True)
        self.btn_split.setFixedHeight(24)
        self.btn_split.setToolTip("分频式：仅对触发的齿音高频段进行动态衰减，不损失人声低频厚度")
        self.btn_split.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_split.clicked.connect(lambda: self._on_mode_selected("split"))

        self.btn_sidechain = QPushButton("Sidechain 侧链")
        self.btn_sidechain.setObjectName("SegBtn")
        self.btn_sidechain.setCheckable(True)
        self.btn_sidechain.setFixedHeight(24)
        self.btn_sidechain.setToolTip("侧链式：由高频检测触发全频段宽带平滑衰减，无分频相位染色")
        self.btn_sidechain.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sidechain.clicked.connect(lambda: self._on_mode_selected("sidechain"))

        mode_btn_row.addWidget(self.btn_split, 1)
        mode_btn_row.addWidget(self.btn_sidechain, 1)
        mode_lay.addLayout(mode_btn_row)
        cell_mode.root_lay.addWidget(mode_sec)
        self._mode_column = cell_mode

        det_sec = QWidget()
        cell_mode.register_divider_above(det_sec)
        det_lay = QVBoxLayout(det_sec)
        det_lay.setContentsMargins(10, 8, 10, 8)
        det_lay.setSpacing(8)

        shape_row = QHBoxLayout()
        s_lbl = QLabel("Detector Filter")
        s_lbl.setObjectName("ParamTitle")
        self.shape_selector = DetectorShapeSelector(["Bandpass", "High Shelf"], "Bandpass", ACCENT_AMBER)
        self.shape_selector.shapeChanged.connect(self._on_shape_selected)
        shape_row.addWidget(s_lbl)
        shape_row.addStretch()
        shape_row.addWidget(self.shape_selector)
        det_lay.addLayout(shape_row)

        la_row = QHBoxLayout()
        la_row.setSpacing(4)
        la_lbl = QLabel("Lookahead")
        la_lbl.setObjectName("ParamTitle")
        la_row.addWidget(la_lbl)
        la_row.addStretch()

        self.lookahead_btns: dict[float, QPushButton] = {}
        for la_ms, la_txt in ((0.0, "Off"), (2.0, "2ms"), (5.0, "5ms")):
            b = QPushButton(la_txt)
            b.setObjectName("SegBtn")
            b.setCheckable(True)
            b.setFixedSize(36, 20)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda checked=False, val=la_ms: self._on_lookahead_selected(val))
            la_row.addWidget(b)
            self.lookahead_btns[la_ms] = b
        det_lay.addLayout(la_row)

        det_lay.addStretch()
        cell_mode.root_lay.addWidget(det_sec, 1)
        self._demo_detector_section = det_sec
        bottom_row.addWidget(cell_mode, 1)

        # === 栏 2：目标频段与带宽 (SIBILANCE BAND) ===
        cell_band = ParameterCellFrame("Sibilance Band (5–9k)", ACCENT_AMBER, is_last=False)

        freq_sec = QWidget()
        f_lay = QVBoxLayout(freq_sec)
        f_lay.setContentsMargins(10, 6, 10, 7)
        f_lay.setSpacing(6)

        f_top = QHBoxLayout()
        f_left = QVBoxLayout()
        f_left.setSpacing(4)
        f_title = QLabel("Center Freq")
        f_title.setObjectName("ParamTitle")
        self.freq_pill = EditableValuePill("6.8K", ACCENT_AMBER, width=60)
        self.freq_pill.valueEdited.connect(self._on_freq_text)
        f_left.addWidget(f_title)
        f_left.addWidget(self.freq_pill)
        f_left.addStretch()

        f_right = QVBoxLayout()
        f_right.setSpacing(1)
        self.freq_knob = ProRotaryKnob(DETECTOR_FREQ_MIN, DETECTOR_FREQ_MAX, 6800.0, 6800.0, ACCENT_AMBER, mode="log")
        self.freq_knob.valueChanged.connect(self._on_freq_changed)
        f_hints = QHBoxLayout()
        for t in ("1.0K", "16.0K"):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            f_hints.addWidget(lbl)
        f_right.addWidget(self.freq_knob, 0, Qt.AlignmentFlag.AlignCenter)
        f_right.addLayout(f_hints)

        f_top.addLayout(f_left)
        f_top.addStretch()
        f_top.addLayout(f_right)
        f_lay.addLayout(f_top)

        # 快捷齿音靶向按钮 (5.5K / 7.0K / 8.5K)
        target_row = QHBoxLayout()
        target_row.setSpacing(3)
        self.target_btns: list[tuple[float, QPushButton]] = []
        for label, hz, tip in SIBILANCE_TARGETS:
            btn = QPushButton(label)
            btn.setObjectName("SegBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setToolTip(f"{label}Hz — {tip}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, val=hz: self._on_freq_changed(val))
            target_row.addWidget(btn)
            self.target_btns.append((hz, btn))
        f_lay.addLayout(target_row)
        cell_band.root_lay.addWidget(freq_sec)

        q_sec = QWidget()
        cell_band.register_divider_above(q_sec)
        q_lay = QHBoxLayout(q_sec)
        q_lay.setContentsMargins(10, 7, 10, 7)

        q_left = QVBoxLayout()
        q_left.setSpacing(4)
        self.q_title = QLabel("Bandwidth (Q)")
        self.q_title.setObjectName("ParamTitle")
        self.q_pill = EditableValuePill("2.2", ACCENT_AMBER, width=60)
        self.q_pill.valueEdited.connect(self._on_q_text)
        q_left.addWidget(self.q_title)
        q_left.addWidget(self.q_pill)
        q_left.addStretch()

        q_right = QVBoxLayout()
        q_right.setSpacing(1)
        self.q_knob = ProRotaryKnob(0.3, 12.0, 2.2, 2.2, ACCENT_AMBER, mode="linear")
        self.q_knob.valueChanged.connect(self._on_q_changed)
        q_hints = QHBoxLayout()
        for t in ("0.3", "12.0"):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            q_hints.addWidget(lbl)
        q_right.addWidget(self.q_knob, 0, Qt.AlignmentFlag.AlignCenter)
        q_right.addLayout(q_hints)

        q_lay.addLayout(q_left)
        q_lay.addStretch()
        q_lay.addLayout(q_right)
        cell_band.root_lay.addWidget(q_sec, 1)

        bottom_row.addWidget(cell_band, 1)

        # === 栏 3：动态阈值与衰减深度 (DYNAMICS) ===
        cell_dyn = ParameterCellFrame("Dynamics & Depth", ACCENT_CORAL, is_last=False)

        th_sec = QWidget()
        th_lay = QHBoxLayout(th_sec)
        th_lay.setContentsMargins(10, 8, 10, 8)

        th_left = QVBoxLayout()
        th_left.setSpacing(4)
        th_title = QLabel("Threshold")
        th_title.setObjectName("ParamTitle")
        self.thresh_pill = EditableValuePill("-24.0", ACCENT_AMBER, width=60)
        self.thresh_pill.valueEdited.connect(self._on_thresh_text)
        th_left.addWidget(th_title)
        th_left.addWidget(self.thresh_pill)
        th_left.addStretch()

        th_right = QVBoxLayout()
        th_right.setSpacing(1)
        self.thresh_knob = ProRotaryKnob(-60.0, 0.0, -24.0, -24.0, ACCENT_AMBER, mode="linear")
        self.thresh_knob.valueChanged.connect(self._on_thresh_changed)
        th_hints = QHBoxLayout()
        for t in ("-60", "dB", "0"):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            th_hints.addWidget(lbl)
        th_right.addWidget(self.thresh_knob, 0, Qt.AlignmentFlag.AlignCenter)
        th_right.addLayout(th_hints)

        th_lay.addLayout(th_left)
        th_lay.addStretch()
        th_lay.addLayout(th_right)
        cell_dyn.root_lay.addWidget(th_sec, 1)

        rng_sec = QWidget()
        cell_dyn.register_divider_above(rng_sec)
        rng_lay = QHBoxLayout(rng_sec)
        rng_lay.setContentsMargins(10, 8, 10, 8)

        rng_left = QVBoxLayout()
        rng_left.setSpacing(4)
        rng_title = QLabel("Max Range")
        rng_title.setObjectName("ParamTitle")
        self.range_pill = EditableValuePill("12.0", ACCENT_CORAL, width=60)
        self.range_pill.valueEdited.connect(self._on_range_text)
        rng_left.addWidget(rng_title)
        rng_left.addWidget(self.range_pill)
        rng_left.addStretch()
        self._depth_controls = rng_left

        rng_right = QVBoxLayout()
        rng_right.setSpacing(1)
        self.range_knob = ProRotaryKnob(0.0, 24.0, 12.0, 12.0, ACCENT_CORAL, mode="linear")
        self.range_knob.valueChanged.connect(self._on_range_changed)
        rng_hints = QHBoxLayout()
        for t in ("0", "dB", "24"):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            rng_hints.addWidget(lbl)
        rng_right.addWidget(self.range_knob, 0, Qt.AlignmentFlag.AlignCenter)
        rng_right.addLayout(rng_hints)

        rng_lay.addLayout(rng_left)
        rng_lay.addStretch()
        rng_lay.addLayout(rng_right)
        cell_dyn.root_lay.addWidget(rng_sec, 1)

        bottom_row.addWidget(cell_dyn, 1)

        # === 栏 4：包络时间与输出补偿 (TIMING & OUTPUT) ===
        cell_time = ParameterCellFrame("Timing & Output", ACCENT_GREEN, is_last=True)

        env_sec = QWidget()
        env_lay = QHBoxLayout(env_sec)
        env_lay.setContentsMargins(10, 6, 10, 6)
        env_lay.setSpacing(10)

        # Attack 列
        atk_col = QVBoxLayout()
        atk_col.setSpacing(3)
        atk_lbl = QLabel("Attack (ms)")
        atk_lbl.setObjectName("ParamTitle")
        atk_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.attack_knob = ProRotaryKnob(0.05, 50.0, 1.5, 1.5, ACCENT_GREEN, mode="log")
        self.attack_knob.valueChanged.connect(self._on_attack_changed)
        self.attack_pill = EditableValuePill("1.5", ACCENT_GREEN, width=54)
        self.attack_pill.valueEdited.connect(self._on_attack_text)
        atk_col.addWidget(atk_lbl)
        atk_col.addWidget(self.attack_knob, 0, Qt.AlignmentFlag.AlignCenter)
        atk_col.addWidget(self.attack_pill, 0, Qt.AlignmentFlag.AlignCenter)

        # Release 列
        rel_col = QVBoxLayout()
        rel_col.setSpacing(3)
        rel_lbl = QLabel("Release (ms)")
        rel_lbl.setObjectName("ParamTitle")
        rel_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.release_knob = ProRotaryKnob(1.0, 500.0, 65.0, 65.0, ACCENT_GREEN, mode="log")
        self.release_knob.valueChanged.connect(self._on_release_changed)
        self.release_pill = EditableValuePill("65", ACCENT_GREEN, width=54)
        self.release_pill.valueEdited.connect(self._on_release_text)
        rel_col.addWidget(rel_lbl)
        rel_col.addWidget(self.release_knob, 0, Qt.AlignmentFlag.AlignCenter)
        rel_col.addWidget(self.release_pill, 0, Qt.AlignmentFlag.AlignCenter)

        env_lay.addLayout(atk_col, 1)
        env_lay.addLayout(rel_col, 1)
        cell_time.root_lay.addWidget(env_sec, 1)

        out_sec = QWidget()
        cell_time.register_divider_above(out_sec)
        out_lay = QHBoxLayout(out_sec)
        out_lay.setContentsMargins(10, 7, 10, 7)

        out_left = QVBoxLayout()
        out_left.setSpacing(4)
        out_title = QLabel("Output Trim")
        out_title.setObjectName("ParamTitle")
        self.out_pill = EditableValuePill("0.0", ACCENT_CYAN, width=60)
        self.out_pill.valueEdited.connect(self._on_out_text)
        out_left.addWidget(out_title)
        out_left.addWidget(self.out_pill)
        out_left.addStretch()

        out_right = QVBoxLayout()
        out_right.setSpacing(1)
        self.out_knob = ProRotaryKnob(-12.0, 12.0, 0.0, 0.0, ACCENT_CYAN, mode="bipolar")
        self.out_knob.valueChanged.connect(self._on_out_changed)
        out_hints = QHBoxLayout()
        for t in ("-12", "dB", "+12"):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            out_hints.addWidget(lbl)
        out_right.addWidget(self.out_knob, 0, Qt.AlignmentFlag.AlignCenter)
        out_right.addLayout(out_hints)

        out_lay.addLayout(out_left)
        out_lay.addStretch()
        out_lay.addLayout(out_right)
        cell_time.root_lay.addWidget(out_sec, 1)
        self._demo_output_section = out_sec

        bottom_row.addWidget(cell_time, 1)
        main_lay.addLayout(bottom_row, 4)

    # -----------------------------------------------------------------
    # 状态刷新与联动逻辑
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_ab_and_listen_styles(self):
        active_css = f"""
            QPushButton {{
                background-color: #182E38;
                color: {ACCENT_CYAN};
                border: 1px solid #24788C;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 700;
            }}
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

        self.top_listen_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
                padding: 0px 8px;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #382812;
                color: {ACCENT_AMBER};
                border: 1px solid {ACCENT_AMBER};
            }}
        """)

    def use_processor_controls(self) -> None:
        self.graph._processor_controls = True

        self._mode_column.root_lay.addStretch(1)
        self.btn_sidechain.setText("Wide-Band")
        self.ratio_input = QDoubleSpinBox(self)
        self.ratio_input.setRange(1.0, 12.0)
        self.ratio_input.setDecimals(2)
        self.ratio_input.setSingleStep(0.1)
        self.ratio_input.setSuffix(":1")
        self.ratio_input.setKeyboardTracking(False)
        ratio_label = QLabel(I18N.tr("audio.fx.ratio", "Ratio"), self)
        ratio_label.setObjectName("ParamTitle")
        self._depth_controls.insertWidget(2, ratio_label)
        self._depth_controls.insertWidget(3, self.ratio_input)
        self.ratio_input.valueChanged.connect(self._on_ratio_changed)
        self._refresh_all_views()

    def _on_ratio_changed(self, ratio: float) -> None:
        if self._updating:
            return
        self._current_state()["ratio"] = ratio
        self.stateChanged.emit(self.get_state())

    def _refresh_all_views(self):
        self._updating = True
        st = self._current_state()
        en = bool(st["enabled"])

        self._update_ab_and_listen_styles()
        self.power_switch.setChecked(en)
        self.top_listen_btn.setChecked(bool(st["listen"]))

        self.btn_split.setChecked(st["mode"] == "split")
        self.btn_sidechain.setChecked(st["mode"] == "sidechain")

        self.shape_selector.setCurrentShape(st["filter_shape"])
        self.shape_selector.setActive(en)

        for ms, btn in self.lookahead_btns.items():
            btn.setChecked(abs(st["lookahead"] - ms) < 0.01)

        # Frequency & Targets
        self.freq_knob.setValue(st["freq"])
        self.freq_knob.setActive(en)
        self.freq_pill.setText(format_freq(st["freq"]))
        self.freq_pill.setActive(en)
        for target_hz, btn in self.target_btns:
            btn.setChecked(abs(st["freq"] - target_hz) < 120.0)

        # Q / Slope
        self.q_title.setText("Slope / Q" if st["filter_shape"] == "High Shelf" else "Bandwidth (Q)")
        self.q_knob.setValue(st["q"])
        self.q_knob.setActive(en)
        self.q_pill.setText(f"{st['q']:.1f}")
        self.q_pill.setActive(en)

        # Threshold & Max Range
        self.thresh_knob.setValue(st["threshold"])
        self.thresh_knob.setActive(en)
        self.thresh_pill.setText(f"{st['threshold']:.1f}")
        self.thresh_pill.setActive(en)

        self.range_knob.setValue(st["range"])
        self.range_knob.setActive(en)
        self.range_pill.setText(f"{st['range']:.1f}")
        self.range_pill.setActive(en)
        ratio_input = getattr(self, "ratio_input", None)
        if ratio_input is not None:
            with QSignalBlocker(ratio_input):
                ratio_input.setValue(st.get("ratio", 4.0))

        # Timing & Output
        self.attack_knob.setValue(st["attack"])
        self.attack_knob.setActive(en)
        self.attack_pill.setText(f"{st['attack']:.2f}")
        self.attack_pill.setActive(en)

        self.release_knob.setValue(st["release"])
        self.release_knob.setActive(en)
        self.release_pill.setText(f"{int(round(st['release']))}")
        self.release_pill.setActive(en)

        og = st["output_gain"]
        self.out_knob.setValue(og)
        self.out_knob.setActive(en)
        self.out_pill.setText(f"{og:+.1f}" if abs(og) >= 0.05 else "0.0")
        self.out_pill.setActive(en)

        self.graph.setState(st)
        self._updating = False

    def _commit_change(self, specific_signal=None, val=None):
        if self._updating:
            return
        self._refresh_all_views()
        if specific_signal is not None:
            specific_signal.emit(val)
        self.stateChanged.emit(self.get_state())

    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _copy_bank(self):
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._banks[self._active_bank])

    def _on_power_toggled(self, enabled: bool):
        self._current_state()["enabled"] = enabled
        self._commit_change(self.bypassChanged, enabled)

    def _on_mode_selected(self, mode: str):
        self._current_state()["mode"] = mode
        self._commit_change(self.modeChanged, mode)

    def _on_shape_selected(self, shape: str):
        self._current_state()["filter_shape"] = shape
        self._commit_change(self.filterShapeChanged, shape)

    def _on_listen_toggled(self, checked: bool):
        self._current_state()["listen"] = bool(checked)
        self._commit_change(self.listenChanged, bool(checked))

    def _on_lookahead_selected(self, ms: float):
        self._current_state()["lookahead"] = ms
        self._commit_change(self.lookaheadChanged, ms)

    def _on_freq_changed(self, freq: float):
        val = max(DETECTOR_FREQ_MIN, min(DETECTOR_FREQ_MAX, round(freq, 1)))
        self._current_state()["freq"] = val
        self._commit_change(self.freqChanged, val)

    def _on_freq_text(self, text: str):
        val = parse_freq(text, self._current_state()["freq"])
        self._on_freq_changed(val)

    def _on_q_changed(self, q: float):
        val = max(0.3, min(12.0, round(q, 1)))
        self._current_state()["q"] = val
        self._commit_change(self.qChanged, val)

    def _on_q_text(self, text: str):
        try:
            self._on_q_changed(float(text))
        except ValueError:
            pass

    def _on_thresh_changed(self, thresh: float):
        val = max(-60.0, min(0.0, round(thresh, 1)))
        self._current_state()["threshold"] = val
        self._commit_change(self.thresholdChanged, val)

    def _on_thresh_text(self, text: str):
        try:
            self._on_thresh_changed(float(text))
        except ValueError:
            pass

    def _on_range_changed(self, rng: float):
        val = max(0.0, min(24.0, round(rng, 1)))
        self._current_state()["range"] = val
        self._commit_change(self.rangeChanged, val)

    def _on_range_text(self, text: str):
        try:
            self._on_range_changed(float(text))
        except ValueError:
            pass

    def _on_attack_changed(self, atk: float):
        val = max(0.05, min(50.0, round(atk, 2)))
        self._current_state()["attack"] = val
        self._commit_change(self.attackChanged, val)

    def _on_attack_text(self, text: str):
        try:
            self._on_attack_changed(float(text))
        except ValueError:
            pass

    def _on_release_changed(self, rel: float):
        val = max(1.0, min(500.0, round(rel, 1)))
        self._current_state()["release"] = val
        self._commit_change(self.releaseChanged, val)

    def _on_release_text(self, text: str):
        try:
            self._on_release_changed(float(text))
        except ValueError:
            pass

    def _on_out_changed(self, gain: float):
        val = max(-12.0, min(12.0, round(gain, 1)))
        self._current_state()["output_gain"] = val
        self._commit_change(self.outputGainChanged, val)

    def _on_out_text(self, text: str):
        try:
            self._on_out_changed(float(text))
        except ValueError:
            pass

    def _on_graph_node_dragged(self, freq: float, thresh: float):
        st = self._current_state()
        st["freq"] = max(DETECTOR_FREQ_MIN, min(DETECTOR_FREQ_MAX, round(freq, 1)))
        st["threshold"] = max(-60.0, min(0.0, round(thresh, 1)))
        self._refresh_all_views()
        self.freqChanged.emit(st["freq"])
        self.thresholdChanged.emit(st["threshold"])
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
        name, ok = QInputDialog.getText(self, "保存 De-Esser 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 独立演示动画 (模拟人声周期性 s/sh 齿音尖峰)
    # -----------------------------------------------------------------
    def _tick_demo_animation(self):
        self._anim_phase += 0.14
        st = self._current_state()

        # 周期性模拟人声词尾 "s / sh" 齿音能量爆发
        sibilance_burst = max(0.0, math.sin(self._anim_phase * 0.55)) ** 6
        sibilance_db_boost = 22.0 * sibilance_burst

        in_bins, out_bins, peak_bins = [], [], []
        sc_energy_peak = -60.0

        for i, f in enumerate(self.graph._spec_freqs):
            # 人声基频与泛音自然衰减基底
            if f < 120.0:
                base = -54.0 + 14.0 * math.exp(-((math.log10(f) - math.log10(130.0)) ** 2) / 0.05)
            else:
                base = -32.0 - 15.0 * math.log10(f / 120.0)

            # 5.5k ~ 8.5kHz 齿音共鸣峰
            sib_bell = math.exp(-0.5 * ((math.log2(f / 6900.0) / 0.38) ** 2))
            ripple = 2.2 * math.sin(i * 0.28 + self._anim_phase) + 1.4 * math.cos(i * 0.7 - self._anim_phase)
            raw_db = max(-80.0, min(-4.0, base + ripple + sibilance_db_boost * sib_bell))
            in_bins.append(raw_db)

            # 统计侧链检测器加权峰值
            w = self.graph._detector_weight(f)
            weighted_db = raw_db - (1.0 - w) * 35.0
            if weighted_db > sc_energy_peak:
                sc_energy_peak = weighted_db

        # 计算触发后的增益衰减量
        overshoot = sc_energy_peak - st["threshold"]
        if st["enabled"] and overshoot > 0.0:
            gr_db = -min(st["range"], overshoot * 0.85)
        else:
            gr_db = 0.0

        for i, f in enumerate(self.graph._spec_freqs):
            raw_db = in_bins[i]
            w = self.graph._detector_weight(f) if st["mode"] == "split" else 1.0
            att = gr_db * w if st["enabled"] else 0.0
            final_db = max(-80.0, min(0.0, raw_db + att + st["output_gain"]))
            out_bins.append(final_db)
            peak_bins.append(min(-3.0, max(raw_db + 1.5, final_db + 2.0)))

        out_peak = max(out_bins) + 10.0
        self.graph.update_spectrum(self.graph._spec_freqs, in_bins, out_bins, peak_bins)
        self.graph.setDynamicsTelemetry(sc_energy_peak, gr_db)
        self.meter_strip.setTelemetry(sc_energy_peak, st["threshold"], gr_db, out_peak)

    # -----------------------------------------------------------------
    # 公开 API：供后端 DSP 引擎与工程序列化调用
    # -----------------------------------------------------------------
    def reset_deesser(self):
        self._banks[self._active_bank] = default_deesser_state()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return {"ratio": 4.0, **copy.deepcopy(self._current_state())}

    def set_state(self, state: dict):
        self._banks[self._active_bank] = {**default_deesser_state(), **copy.deepcopy(state)}
        self._refresh_all_views()

    def update_spectrum(
        self,
        freqs_hz: list[float],
        input_db: list[float],
        output_db: list[float] | None = None,
        peak_db: list[float] | None = None,
    ):
        """接入真实音频 FFT 频谱数据 (-80.0 ~ 0.0 dBFS)"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.graph.update_spectrum(freqs_hz, input_db, output_db, peak_db)

    def update_meters(self, sc_db: float, gr_db: float, out_db: float):
        """
        接入真实音频引擎动态监测数值：
        - sc_db:  侧链检测器当前电平 (-60.0 ~ 0.0 dBFS)
        - gr_db:  当前齿音衰减量 (负数，例如 -6.5 表示衰减 6.5 dB)
        - out_db: 输出峰值电平 (-60.0 ~ 0.0 dBFS)
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        st = self._current_state()
        self.graph.setDynamicsTelemetry(sc_db, gr_db)
        self.meter_strip.setTelemetry(sc_db, st["threshold"], gr_db, out_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioDeEsserWidget("Vocal 1")
    win.show()
    sys.exit(app.exec())
