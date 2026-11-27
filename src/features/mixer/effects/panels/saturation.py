from __future__ import annotations

import sys
import math
import copy
import random
import numpy as np
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QSizePolicy, QInputDialog, QMenu
)
from PyQt6.QtCore import Qt, QRectF, QPointF, QSignalBlocker, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QLinearGradient,
    QPainterPath, QFont, QPolygonF
)

from src.shared.ui.widgets.chevron_combo import ChevronComboBox

from src.features.mixer.effects.panel_glyphs import GlyphComboBox
from src.features.edit.core.audio_effects.saturation import (
    PANEL_DRIVE_TO_DB, shape_tape, shape_transistor, shape_tube,
)
from src.shared.i18n.i18n import I18N


# =====================================================================
# 1. 全局色彩系统与声学常量 (与 EQ-UI 统一视觉语言)
# =====================================================================
FREQ_MIN = 20.0
FREQ_MAX = 20000.0

UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC', 'Microsoft YaHei'"
MONO_FONT_CSS = "'Menlo', 'Consolas'"
MONO_FONT_FAM = "Menlo"

# 三频段专属低眩光高级色板 (LOW / MID / HIGH)
BAND_PALETTE = [
    "#F0A848",  # Band 1 (LOW):  琥珀金 — 底鼓/贝斯厚度与磁带低频凝聚
    "#28D8F0",  # Band 2 (MID):  冰晶青 — 人声/军鼓谐波密度与临场感
    "#B67CF8",  # Band 3 (HIGH): 紫罗兰 — 空气感丝滑延展与高频染色
]

SAT_MODELS = ["Tape", "Tube", "Transistor"]

SAT_MODEL_DESC = {
    "Tape":       "磁带: 平滑奇偶谐波 + 高频自然压缩粘合 (Glue)",
    "Tube":       "电子管: 丰满偶次谐波 (2nd) + 非对称暖度",
    "Transistor": "晶体管: 紧致奇次谐波 (3rd) + 瞬态冲击力",
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
        return f"{hz / 1000.0:.2f}K" if hz < 10000.0 else f"{hz / 1000.0:.1f}K"
    return f"{int(round(hz))}"


def parse_freq(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("HZ", "").strip()
    try:
        if s.endswith("K"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def parse_number(text: str, fallback: float) -> float:
    s = (
        text.strip()
        .upper()
        .replace("DB", "")
        .replace("%", "")
        .replace("HZ", "")
        .replace("MS", "")
        .replace("+", "")
        .strip()
    )
    try:
        return float(s)
    except ValueError:
        return fallback


def default_bands_warm_glue() -> list[dict]:
    return [
        {
            "index": 1, "name": "LOW", "enabled": True, "solo": False,
            "model": "Tape", "drive": 6.5, "tone": -12.0, "bias": 15.0, "level": 0.5,
        },
        {
            "index": 2, "name": "MID", "enabled": True, "solo": False,
            "model": "Tube", "drive": 8.0, "tone": 10.0, "bias": 25.0, "level": 0.0,
        },
        {
            "index": 3, "name": "HIGH", "enabled": True, "solo": False,
            "model": "Tape", "drive": 4.2, "tone": 18.0, "bias": -10.0, "level": 0.8,
        },
    ]


def default_bands_clean() -> list[dict]:
    return [
        {
            "index": 1, "name": "LOW", "enabled": True, "solo": False,
            "model": "Tape", "drive": 0.0, "tone": 0.0, "bias": 0.0, "level": 0.0,
        },
        {
            "index": 2, "name": "MID", "enabled": True, "solo": False,
            "model": "Tube", "drive": 0.0, "tone": 0.0, "bias": 0.0, "level": 0.0,
        },
        {
            "index": 3, "name": "HIGH", "enabled": True, "solo": False,
            "model": "Transistor", "drive": 0.0, "tone": 0.0, "bias": 0.0, "level": 0.0,
        },
    ]


# =====================================================================
# 2. 原创非线性传输特性曲线图标绘制 (Tape / Tube / Transistor)
# =====================================================================
def draw_sat_model_glyph(painter: QPainter, rect: QRectF, model: str, color: QColor):
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    cx = rect.center().x()
    cy = rect.center().y()
    w = min(rect.width() * 0.76, 22.0)
    h = min(rect.height() * 0.68, 12.0)

    # 十字微弱坐标轴参考
    painter.setPen(QPen(QColor(255, 255, 255, 28), 0.8))
    painter.drawLine(QPointF(cx - w / 2.0, cy), QPointF(cx + w / 2.0, cy))
    painter.drawLine(QPointF(cx, cy - h / 2.0), QPointF(cx, cy + h / 2.0))

    pen = QPen(color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    path = QPainterPath()
    steps = 24
    for i in range(steps + 1):
        u = -1.0 + 2.0 * (i / steps)  # -1 .. +1
        if model == "Tape":
            # 磁带: 经典对称平滑 S 曲线 (tanh)
            y_val = math.tanh(1.85 * u) / math.tanh(1.85)
        elif model == "Tube":
            # 电子管: 非对称偶次谐波曲线 (正半周柔和压缩，负半周更舒展)
            if u >= 0:
                y_val = (1.0 - math.exp(-2.4 * u)) / (1.0 - math.exp(-2.4))
            else:
                y_val = -math.tanh(1.25 * abs(u)) / math.tanh(1.25)
        else:
            # 晶体管: 高线性度 + 较硬削波拐点 (奇次谐波特征)
            v = u * 1.55
            y_val = max(-1.0, min(1.0, v - (v ** 3) / 4.2))

        px = cx + u * (w / 2.0)
        py = cy - y_val * (h / 2.0)
        if i == 0:
            path.moveTo(px, py)
        else:
            path.lineTo(px, py)

    painter.drawPath(path)
    painter.restore()


# =====================================================================
# 3. 控件：现代发光电源开关 (复用 EQ-UI 风格)
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
# 4. 控件：饱和染色模型选择器 (带传输曲线图标 + 型号文字)
# =====================================================================
class SatModelSelector(GlyphComboBox):
    modelChanged = pyqtSignal(str)

    def __init__(self, current: str, accent_color: str, parent: QWidget | None = None) -> None:
        super().__init__(SAT_MODELS, current, accent_color, draw_sat_model_glyph, parent)
        self.setAccessibleName("Saturation model")
        self.setFixedWidth(self.sizeHint().width())
        self.currentTextChanged.connect(self.modelChanged.emit)

    def setCurrentModel(self, model: str) -> None:
        self.set_choice(model)

    def setBandActive(self, active: bool) -> None:
        self.set_glyph_active(active)

    def _refresh_tooltip(self, index: int) -> None:
        self.setToolTip(SAT_MODEL_DESC.get(self.itemData(index), ""))



# =====================================================================
# 5. 控件：内嵌可编辑数字胶囊 (纯黑内凹框)
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
            clean = (
                self._text.replace("+", "")
                .replace("dB", "")
                .replace("%", "")
                .replace("ms", "")
                .replace("Hz", "")
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
        p.drawRoundedRect(rect, 3.0, 3.0)

        p.setPen(QColor("#ECECF0") if self._active else QColor("#6E6E76"))
        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


# =====================================================================
# 6. 控件：拉丝哑光灰精密旋钮 (复用 EQ-UI ProRotaryKnob)
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
        size: int = 36,
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

        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setValue(self, val: float, emit: bool = False):
        clamped = max(self._min, min(self._max, val))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def setScale(self, minimum: float, maximum: float, mode: str) -> None:
        self._min = minimum
        self._max = maximum
        self._mode = mode
        self.setValue(self._value)
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

        arc_r = (min(self.width(), self.height()) / 2.0) - 3.2
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深黑轨道底槽
        p.setPen(QPen(QColor("#141417"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性模式 12 点钟方向基准刻度点
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 2. 主题色数值弧
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

        # 3. 旋钮帽
        knob_r = arc_r - 3.8
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 4. 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.0
        iy1 = cy - math.sin(rad) * 3.0
        ix2 = cx + math.cos(rad) * (knob_r - 1.4)
        iy2 = cy - math.sin(rad) * (knob_r - 1.4)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 7. 控件：交互式多段饱和频谱 + 分频点 + 非线性传输曲线视窗
# =====================================================================
class ProSaturationGraphWidget(QWidget):
    crossoverDragged = pyqtSignal(float, float)      # xover_low, xover_high
    bandNodeDragged = pyqtSignal(int, float, float)  # band_idx, new_drive, new_tone
    bandToneScrolled = pyqtSignal(int, float)        # band_idx, new_tone
    bandNodeClicked = pyqtSignal(int)                # band_idx
    bandToggleRequested = pyqtSignal(int)            # band_idx
    bandModelRequested = pyqtSignal(int, str)        # band_idx, new_model
    spectrumVisibilityChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(250)
        self.setMouseTracking(True)

        self._bands = default_bands_warm_glue()
        self._enabled = True
        self._global_drive = 0.0
        self._wave_shaper_drive: float | None = None
        self._processor_controls = False
        self._max_band_drive = 24.0
        self._mix = 100.0
        self._xover_low = 210.0
        self._xover_high = 3200.0
        self._selected_band = 1

        # 磁带停机与颤音状态 (用于可视化)
        self._tape_speed = 1.0  # 1.0 = 正常, 0.0 = 完全停机
        self._tape_stop_active = False
        self._flutter_depth = 0.0

        self._show_spectrum = True
        self._show_peak_hold = True
        self._show_transfer_hud = True

        self._hover_target: tuple[str, int] | None = None  # ("xover", 0/1) or ("node", 0/1/2)
        self._dragging_target: tuple[str, int] | None = None
        self._drag_start_tone = 0.0
        self._mouse_pos: QPointF | None = None

        self._ml = 36.0
        self._mr = 36.0
        self._mt = 10.0
        self._mb = 24.0

        self._btn_spec_rect = QRectF()
        self._btn_peak_rect = QRectF()
        self._btn_curve_rect = QRectF()

        self._num_bins = 260
        self._spec_freqs = [
            FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** (i / (self._num_bins - 1)))
            for i in range(self._num_bins)
        ]
        self._spec_post = []
        self._spec_peak = []
        self._init_demo_spectrum()

    def _init_demo_spectrum(self):
        rng = random.Random(77)
        post, peak = [], []
        for f in self._spec_freqs:
            if f < 100:
                base = -44.0 + 10.0 * math.exp(-((math.log10(f) - math.log10(95)) ** 2) / 0.09)
            else:
                decades = math.log10(f / 100.0)
                base = -36.0 - 15.5 * decades
            ripple = 2.4 * math.sin(f * 0.028) + 1.8 * math.cos(f * 0.013) + rng.uniform(-3.0, 3.0)
            val = max(-82.0, min(-14.0, base + ripple))
            post.append(val)
            peak.append(min(-11.0, val + rng.uniform(0.5, 3.8)))
        self._spec_post = post
        self._spec_peak = peak

    def update_spectrum(self, freqs_hz: list[float], post_db: list[float], peak_db: list[float] | None = None):
        if peak_db is None:
            if self._show_peak_hold and self._spec_freqs == freqs_hz and len(self._spec_peak) == len(post_db):
                peak_db = [max(current, previous) for current, previous in zip(post_db, self._spec_peak)]
            else:
                peak_db = list(post_db)
        self._spec_freqs = freqs_hz
        self._spec_post = post_db
        self._spec_peak = peak_db
        self.update()

    def setState(
        self,
        bands: list[dict],
        enabled: bool,
        global_drive: float,
        mix: float,
        xover_low: float,
        xover_high: float,
        selected_band: int,
    ):
        self._bands = bands
        self._enabled = enabled
        self._global_drive = global_drive
        self._mix = mix
        self._xover_low = xover_low
        self._xover_high = xover_high
        self._selected_band = selected_band
        self.update()

    def setTapeVisualState(self, speed: float, stop_active: bool, flutter_depth: float):
        self._tape_speed = speed
        self._tape_stop_active = stop_active
        self._flutter_depth = flutter_depth
        self.update()

    def setWaveShaperDrive(self, drive: float | None) -> None:
        self._wave_shaper_drive = drive
        self.update()

    def spectrum_visible(self) -> bool:
        return self._show_spectrum

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

    def _drive_to_y(self, drive_db: float, plot: QRectF) -> float:
        # 0 dB 在底部 82% 处，24 dB 在顶部 14% 处
        d = max(0.0, min(self._max_band_drive, drive_db))
        norm = d / self._max_band_drive
        bottom_y = plot.top() + plot.height() * 0.84
        top_y = plot.top() + plot.height() * 0.14
        return bottom_y - norm * (bottom_y - top_y)

    def _y_to_drive(self, y: float, plot: QRectF) -> float:
        bottom_y = plot.top() + plot.height() * 0.84
        top_y = plot.top() + plot.height() * 0.14
        norm = max(0.0, min(1.0, (bottom_y - y) / max(1.0, bottom_y - top_y)))
        return norm * self._max_band_drive

    def _spec_db_to_y(self, spec_db: float, plot: QRectF) -> float:
        norm = (0.0 - spec_db) / 80.0
        return plot.top() + max(0.0, min(1.0, norm)) * plot.height()

    def _band_center_freq(self, idx: int) -> float:
        if idx == 0:
            return math.sqrt(FREQ_MIN * self._xover_low)
        elif idx == 1:
            return math.sqrt(self._xover_low * self._xover_high)
        return math.sqrt(self._xover_high * FREQ_MAX)

    def _node_pos(self, idx: int, plot: QRectF) -> QPointF:
        b = self._bands[idx]
        x = self._freq_to_x(self._band_center_freq(idx), plot)
        y = self._drive_to_y(b["drive"], plot)
        return QPointF(x, y)

    # -----------------------------------------------------------------
    # 交互命中检测
    # -----------------------------------------------------------------
    def _hit_test(self, pos: QPointF) -> tuple[str, int] | None:
        if self._wave_shaper_drive is not None:
            return None
        plot = self._plot_rect()
        # 1. 优先检测频段 Drive 节点
        for idx in range(3):
            np = self._node_pos(idx, plot)
            if math.hypot(pos.x() - np.x(), pos.y() - np.y()) <= 14.0:
                return ("node", idx)

        # 2. 检测两条分频线 (X-Over Low/Mid, X-Over Mid/High)
        if plot.top() <= pos.y() <= plot.bottom():
            x1 = self._freq_to_x(self._xover_low, plot)
            x2 = self._freq_to_x(self._xover_high, plot)
            if abs(pos.x() - x1) <= 7.0:
                return ("xover", 0)
            if abs(pos.x() - x2) <= 7.0:
                return ("xover", 1)

        return None

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_target = None
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_spec_rect.contains(pos):
                self._show_spectrum = not self._show_spectrum
                self.update_spectrum([], [])
                self.spectrumVisibilityChanged.emit(self._show_spectrum)
                self.update()
                return
            if self._btn_peak_rect.contains(pos):
                self._show_peak_hold = not self._show_peak_hold
                self._spec_peak = list(self._spec_post)
                self.update()
                return
            if self._btn_curve_rect.contains(pos):
                self._show_transfer_hud = not self._show_transfer_hud
                self.update()
                return

            hit = self._hit_test(pos)
            if hit is not None:
                self._dragging_target = hit
                if hit[0] == "node":
                    idx = hit[1]
                    self._drag_start_tone = self._bands[idx]["tone"]
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                    self.bandNodeClicked.emit(idx)
                else:
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                self.update()
                return

            # 点击频段区域内部也自动选中对应频段卡片
            plot = self._plot_rect()
            if plot.contains(pos) and self._wave_shaper_drive is None:
                f = self._x_to_freq(pos.x(), plot)
                idx = 0 if f < self._xover_low else (1 if f < self._xover_high else 2)
                self.bandNodeClicked.emit(idx)

        elif event.button() == Qt.MouseButton.RightButton:
            hit = self._hit_test(pos)
            if hit is not None and hit[0] == "node":
                self._show_node_context_menu(hit[1], event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit_test(event.position())
        if hit is not None and hit[0] == "node":
            self.bandToggleRequested.emit(hit[1])

    def _show_node_context_menu(self, idx: int, global_pos):
        b = self._bands[idx]
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #1D1D21; border: 1px solid #141416; color: #DCDCE2; font-size: 11px; }
            QMenu::item { padding: 5px 16px; }
            QMenu::item:selected { background-color: #35353D; color: #FFFFFF; }
        """)
        for m in SAT_MODELS:
            act = menu.addAction(f"{b['name']} Circuit: {m}")
            act.triggered.connect(lambda checked=False, model=m: self.bandModelRequested.emit(idx, model))
        menu.exec(global_pos)

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._dragging_target is not None:
            kind, idx = self._dragging_target
            if kind == "xover":
                f = self._x_to_freq(event.position().x(), plot)
                if idx == 0:
                    new_low = max(40.0, min(self._xover_high * 0.65, round(f, 1)))
                    self.crossoverDragged.emit(new_low, self._xover_high)
                else:
                    new_high = max(self._xover_low * 1.5, min(14000.0, round(f, 1)))
                    self.crossoverDragged.emit(self._xover_low, new_high)
            elif kind == "node":
                b = self._bands[idx]
                new_drive = round(self._y_to_drive(event.position().y(), plot), 1)
                # Shift + 左右拖动可微调 Tone 倾斜度
                new_tone = b["tone"]
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    center_x = self._freq_to_x(self._band_center_freq(idx), plot)
                    dx = event.position().x() - center_x
                    new_tone = max(-100.0, min(100.0, round(dx * 1.2, 0)))
                self.bandNodeDragged.emit(idx, new_drive, new_tone)
        else:
            hit = self._hit_test(event.position())
            self._hover_target = hit
            if hit is None:
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif hit[0] == "xover":
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_target is not None:
            self._dragging_target = None
            self.update()

    def wheelEvent(self, event):
        target = self._dragging_target or self._hover_target
        if target is not None and target[0] == "node":
            idx = target[1]
            b = self._bands[idx]
            delta = event.angleDelta().y() / 120.0
            new_tone = max(-100.0, min(100.0, round(b["tone"] + delta * 5.0, 0)))
            self.bandToneScrolled.emit(idx, new_tone)
            event.accept()

    # -----------------------------------------------------------------
    # 绘制网格、多段分区背景、频谱、饱和响应包络、传输特性示波器
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 纯平 #1D1D20 中性深灰底
        p.fillRect(self.rect(), QColor("#27272B"))
        p.fillRect(plot, QColor("#1D1D20"))

        x_low = self._freq_to_x(self._xover_low, plot)
        x_high = self._freq_to_x(self._xover_high, plot)
        band_bounds = [
            (plot.left(), x_low),
            (x_low, x_high),
            (x_high, plot.right()),
        ]

        solo_idx = next((i for i, b in enumerate(self._bands) if b["enabled"] and b.get("solo", False)), -1)

        # 2. 三频段背景染色分区 (根据 Drive 强度与 Solo 态呈现克制氛围填充)
        p.save()
        p.setClipRect(plot)
        for idx, (bx1, bx2) in enumerate(band_bounds if self._wave_shaper_drive is None else ()):
            b = self._bands[idx]
            b_col = QColor(BAND_PALETTE[idx])
            is_solo = (idx == solo_idx)
            dimmed = (solo_idx >= 0 and not is_solo) or (not b["enabled"]) or (not self._enabled)

            drive_norm = min(1.0, max(0.0, (b["drive"] + max(0.0, self._global_drive) * 0.5) / 24.0))
            alpha = 6 if dimmed else int(10 + drive_norm * 28 + (12 if is_solo else 0))

            zone_rect = QRectF(bx1, plot.top(), max(1.0, bx2 - bx1), plot.height())
            grad = QLinearGradient(zone_rect.bottomLeft(), zone_rect.topLeft())
            grad.setColorAt(0.0, QColor(b_col.red(), b_col.green(), b_col.blue(), alpha))
            grad.setColorAt(1.0, QColor(b_col.red(), b_col.green(), b_col.blue(), max(2, alpha // 3)))
            p.fillRect(zone_rect, grad)

            # 顶部频段水印标识 (LOW · TAPE / MID · TUBE / HIGH · TAPE)
            if (bx2 - bx1) > 74:
                p.setPen(QColor(b_col.red(), b_col.green(), b_col.blue(), 75 if dimmed else 155))
                lbl_font = QFont(MONO_FONT_FAM, 8)
                lbl_font.setWeight(QFont.Weight.Bold)
                p.setFont(lbl_font)
                p.drawText(
                    QRectF(bx1 + 8, plot.bottom() - 22, bx2 - bx1 - 16, 16),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    f"{b['name']} · {b['model'].upper()}",
                )
        p.restore()

        # 3. 水平与垂直对数刻度线
        grid_pen = QPen(QColor("#26262A"), 1.0)
        zero_pen = QPen(QColor("#3A3A40"), 1.1)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        left_ticks = [0, -10, -20, -30, -40, -50, -60, -70, -80]
        right_ticks = ([""] * len(left_ticks) if self._processor_controls else
                       ["24dB", "", "18", "", "12", "", "6", "", "0dB"])

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
        if self._processor_controls and self._wave_shaper_drive is None:
            for drive in (0, 9, 18, 27, 36):
                y = self._drive_to_y(drive / PANEL_DRIVE_TO_DB, plot)
                p.drawText(QRectF(plot.right() + 7, y - 8, self._mr - 7, 16),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(drive))

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
                    peak_path.lineTo(sx, sy)
                    post_path.lineTo(sx, sy)
                fill_poly.append(QPointF(sx, sy))

            last_x = self._freq_to_x(self._spec_freqs[-1], plot)
            fill_poly.append(QPointF(last_x, plot.bottom()))

            spec_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            spec_grad.setColorAt(0.0, QColor(56, 128, 210, 72))
            spec_grad.setColorAt(1.0, QColor(36, 72, 130, 16))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(spec_grad)
            p.drawPolygon(fill_poly)

            if self._show_peak_hold:
                p.setPen(QPen(QColor(150, 150, 158, 115), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(peak_path)

            p.setPen(QPen(QColor(82, 154, 235, 190), 1.15))
            p.drawPath(post_path)

        if not self._processor_controls:
            # 5. 多段饱和能量包络曲线 (平滑插值展示各频段 Drive + Tone 分布)
            steps = 280
            sat_path = QPainterPath()
            for i in range(steps + 1):
                norm = i / steps
                f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
                x = plot.left() + norm * plot.width()

                # 平滑分频权重 (Linkwitz-Riley 过渡近似)
                w_low = 1.0 / (1.0 + (f / max(20.0, self._xover_low)) ** 4)
                w_high = 1.0 / (1.0 + (max(20.0, self._xover_high) / f) ** 4)
                w_mid = max(0.0, 1.0 - w_low - w_high)

                eff_drives = []
                for idx_b, b in enumerate(self._bands):
                    if not self._enabled or not b["enabled"]:
                        eff_drives.append(0.0)
                    else:
                        # Tone 倾斜影响该频段内部高低端谐波能量
                        c_f = self._band_center_freq(idx_b)
                        tilt = (b["tone"] / 100.0) * 3.2 * math.tanh(math.log2(max(1e-3, f / c_f)))
                        eff_drives.append(max(0.0, b["drive"] + tilt))

                blended_drive = w_low * eff_drives[0] + w_mid * eff_drives[1] + w_high * eff_drives[2]
                y = self._drive_to_y(blended_drive, plot)
                if i == 0:
                    sat_path.moveTo(x, y)
                else:
                    sat_path.lineTo(x, y)

            if self._enabled:
                p.setPen(QPen(QColor(235, 248, 255, 42), 4.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(sat_path)
                p.setPen(QPen(QColor("#F2F8FC"), 1.85))
                p.drawPath(sat_path)
            else:
                p.setPen(QPen(QColor("#686C78"), 1.5))
                p.drawPath(sat_path)

        if self._wave_shaper_drive is None:
            # 6. 可拖拽分频点垂直分割线 (X-Over Low/Mid & Mid/High)
            for xo_idx, (xo_x, xo_hz) in enumerate(((x_low, self._xover_low), (x_high, self._xover_high))):
                is_xo_hi = (
                    (self._hover_target == ("xover", xo_idx))
                    or (self._dragging_target == ("xover", xo_idx))
                )
                line_col = QColor("#28D8F0") if is_xo_hi else QColor("#565864")
                p.setPen(QPen(line_col, 1.6 if is_xo_hi else 1.1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(xo_x, plot.top()), QPointF(xo_x, plot.bottom()))

                # 分频点顶部小胶囊手柄
                pill_r = QRectF(xo_x - 24, plot.bottom() - 44, 48, 16)
                p.setPen(QPen(line_col, 1.0))
                p.setBrush(QColor("#151518"))
                p.drawRoundedRect(pill_r, 3.0, 3.0)
                p.setPen(QColor("#ECECF0") if is_xo_hi else QColor("#9A9AA2"))
                p.setFont(QFont(MONO_FONT_FAM, 7))
                p.drawText(pill_r, Qt.AlignmentFlag.AlignCenter, f"{format_freq(xo_hz)}")

            # 7. 三频段 Drive 控制节点
            node_font = QFont(MONO_FONT_FAM, 8)
            node_font.setWeight(QFont.Weight.Bold)
            p.setFont(node_font)
            short_names = ["L", "M", "H"]

            for idx, b in enumerate(self._bands):
                pt = self._node_pos(idx, plot)
                is_solo = (idx == solo_idx)
                dimmed = (solo_idx >= 0 and not is_solo) or (not b["enabled"]) or (not self._enabled)
                b_col = QColor(BAND_PALETTE[idx]) if not dimmed else QColor("#565A66")
                is_hi = (
                    self._hover_target == ("node", idx)
                    or self._dragging_target == ("node", idx)
                    or is_solo
                    or idx == self._selected_band
                )
                radius = 10.5 if is_hi else 8.8

                if is_solo and self._enabled:
                    p.setPen(QPen(QColor("#F6CE58"), 1.4))
                    p.setBrush(QColor(232, 184, 56, 55))
                    p.drawEllipse(pt, radius + 5.5, radius + 5.5)
                elif is_hi and not dimmed:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 60))
                    p.drawEllipse(pt, radius + 4.5, radius + 4.5)

                p.setPen(QPen(b_col, 1.8))
                p.setBrush(QColor("#151518"))
                p.drawEllipse(pt, radius, radius)

                p.setPen(QColor("#ECECF0") if not dimmed else QColor("#8A8A92"))
                p.drawText(
                    QRectF(pt.x() - radius, pt.y() - radius, radius * 2, radius * 2),
                    Qt.AlignmentFlag.AlignCenter,
                    short_names[idx],
                )

        # 8. 右上角悬浮视图开关胶囊 (CURVE / SPEC / PEAK)
        self._btn_curve_rect = QRectF(plot.right() - 134, plot.top() + 8, 42, 18)
        self._btn_spec_rect = QRectF(plot.right() - 88, plot.top() + 8, 38, 18)
        self._btn_peak_rect = QRectF(plot.right() - 46, plot.top() + 8, 38, 18)
        for rect, label, active in (
            (self._btn_curve_rect, "CURVE", self._show_transfer_hud),
            (self._btn_spec_rect, "SPEC", self._show_spectrum),
            (self._btn_peak_rect, "PEAK", self._show_peak_hold and self._show_spectrum),
        ):
            p.setPen(QPen(QColor("#24788C") if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 46, 56, 210) if active else QColor(21, 21, 24, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#787882"))
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 9. 非线性传输特性 & 谐波柱状 Mini-HUD (显示当前选中频段的饱和波形失真形状)
        if self._show_transfer_hud:
            sel_b = self._bands[self._selected_band]
            sel_col = QColor(BAND_PALETTE[self._selected_band])
            wave_shaper_drive = self._wave_shaper_drive
            curve_title = f"{sel_b['name']} · {sel_b['model'].upper()}"
            curve_active = self._enabled and sel_b["enabled"]
            if wave_shaper_drive is not None:
                sel_col = QColor("#28D8F0")
                curve_title = "DRIVE"
                curve_active = self._enabled
            shape = (wave_shaper_drive or 0.0) / 2.0 if curve_active else 0.0
            hud_box = QRectF(plot.right() - 148, plot.top() + 32, 140, 76)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor(21, 21, 24, 215))
            p.drawRoundedRect(hud_box, 3.5, 3.5)

            # 左侧: 实时传输曲线示波框
            scope_r = QRectF(hud_box.left() + 8, hud_box.top() + 18, 52, 50)
            p.setPen(QPen(QColor("#2C2C32"), 1.0))
            p.drawRect(scope_r)
            p.drawLine(QPointF(scope_r.left(), scope_r.center().y()), QPointF(scope_r.right(), scope_r.center().y()))
            p.drawLine(QPointF(scope_r.center().x(), scope_r.top()), QPointF(scope_r.center().x(), scope_r.bottom()))

            gain_factor = 1.0 + (sel_b["drive"] / 24.0) * 4.5
            bias_shift = (sel_b["bias"] / 100.0) * 0.35
            measured_shape = None
            scale_y = 1.0
            if self._processor_controls and wave_shaper_drive is None:
                u_values = np.linspace(-1.0, 1.0, 33)
                drive_db = sel_b["drive"] * PANEL_DRIVE_TO_DB
                x_values = u_values * 10.0 ** (drive_db / 20.0)
                offset = sel_b["bias"] / 100.0
                model = sel_b["model"]
                if not curve_active:
                    measured_shape = u_values
                elif model == "Tube":
                    measured_shape = shape_tube(x_values, offset)
                elif model == "Transistor":
                    measured_shape = shape_transistor(x_values + offset)
                else:
                    measured_shape = shape_tape(x_values + offset)
                scale_y = max(1.0, float(np.max(np.abs(measured_shape))))
            t_path = QPainterPath()
            for s_i in range(33):
                u = -1.0 + 2.0 * (s_i / 32.0)
                ub = u + bias_shift
                if wave_shaper_drive is not None:
                    yv = (1.0 + shape) * u / (1.0 + shape * abs(u))
                elif measured_shape is not None:
                    yv = measured_shape[s_i] / scale_y
                elif sel_b["model"] == "Tape":
                    yv = math.tanh(gain_factor * ub)
                elif sel_b["model"] == "Tube":
                    yv = (1.0 - math.exp(-gain_factor * 1.2 * ub)) if ub >= 0 else -math.tanh(gain_factor * 0.75 * abs(ub))
                else:
                    v = ub * gain_factor * 0.85
                    yv = max(-1.0, min(1.0, v - (v ** 3) / 3.5))
                yv = max(-1.0, min(1.0, yv))
                px = scope_r.center().x() + u * (scope_r.width() * 0.46)
                py = scope_r.center().y() - yv * (scope_r.height() * 0.46)
                if s_i == 0:
                    t_path.moveTo(px, py)
                else:
                    t_path.lineTo(px, py)

            p.setPen(QPen(sel_col if curve_active else QColor("#666670"), 1.5))
            p.drawPath(t_path)

            # 标题与谐波分量指示条 (H2 偶次 / H3 奇次 / H4 / H5)
            p.setPen(sel_col)
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(
                QRectF(hud_box.left() + 8, hud_box.top() + 3, hud_box.width() - 16, 13),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                curve_title,
            )

            if wave_shaper_drive is not None:
                p.drawText(
                    QRectF(scope_r.right() + 6, scope_r.top(), 68, scope_r.height()),
                    Qt.AlignmentFlag.AlignCenter,
                    f"{wave_shaper_drive:.1f}%",
                )
            elif self._processor_controls:
                drive_db = sel_b["drive"] * PANEL_DRIVE_TO_DB
                p.drawText(QRectF(scope_r.right() + 6, scope_r.top(), 68, scope_r.height()),
                           Qt.AlignmentFlag.AlignCenter, f"{drive_db:.1f} dB")
            else:
                drv_n = sel_b["drive"] / 24.0
                if sel_b["model"] == "Tape":
                    harms = [("H2", 0.35 * drv_n), ("H3", 0.65 * drv_n), ("H4", 0.15 * drv_n), ("H5", 0.25 * drv_n)]
                elif sel_b["model"] == "Tube":
                    harms = [("H2", 0.88 * drv_n), ("H3", 0.32 * drv_n), ("H4", 0.42 * drv_n), ("H5", 0.12 * drv_n)]
                else:
                    harms = [("H2", 0.22 * drv_n), ("H3", 0.92 * drv_n), ("H4", 0.18 * drv_n), ("H5", 0.58 * drv_n)]

                bar_area_x = scope_r.right() + 10
                for h_i, (h_lbl, h_val) in enumerate(harms):
                    bx = bar_area_x + h_i * 16
                    bar_max_h = 36.0
                    bh = max(2.0, min(bar_max_h, h_val * bar_max_h))
                    by = scope_r.bottom() - 10 - bh
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(sel_col if h_val > 0.02 else QColor("#36363E"))
                    p.drawRoundedRect(QRectF(bx, by, 9, bh), 1.5, 1.5)
                    p.setPen(QColor("#8A8A94"))
                    p.drawText(QRectF(bx - 2, scope_r.bottom() - 9, 13, 10), Qt.AlignmentFlag.AlignCenter, h_lbl)

        # 10. 磁带停机状态提示条 (当 Tape Stop 触发或减速中)
        if self._tape_stop_active or self._tape_speed < 0.99:
            brake_rect = QRectF(plot.center().x() - 92, plot.top() + 30, 184, 22)
            p.setPen(QPen(QColor("#F26D6D"), 1.2))
            p.setBrush(QColor(38, 16, 18, 225))
            p.drawRoundedRect(brake_rect, 3.5, 3.5)
            p.setPen(QColor("#F26D6D"))
            bf = QFont(MONO_FONT_FAM, 8)
            bf.setWeight(QFont.Weight.Bold)
            p.setFont(bf)
            p.drawText(
                brake_rect,
                Qt.AlignmentFlag.AlignCenter,
                f"TAPE BRAKE  •  SPEED {int(round(self._tape_speed * 100))}%",
            )

        # 11. 实时十字准星 & 鼠标 HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(mx, plot.top()), QPointF(mx, plot.bottom()))
            p.drawLine(QPointF(plot.left(), my), QPointF(plot.right(), my))

            if self._dragging_target is not None:
                kind, idx = self._dragging_target
                if kind == "xover":
                    hz = self._xover_low if idx == 0 else self._xover_high
                    hud_str = f"X-Over {idx + 1}  •  {format_freq(hz)}Hz ({freq_to_note(hz)})"
                    hud_col = QColor("#28D8F0")
                else:
                    b = self._bands[idx]
                    drive = b["drive"] * (PANEL_DRIVE_TO_DB if self._processor_controls else 1.0)
                    tone = f"{b['tone'] * 0.12:+.1f} dB" if self._processor_controls else f"{b['tone']:+.0f}%"
                    hud_str = f"{b['name']} ({b['model']})  •  Drive {drive:.1f} dB  •  Tone {tone}"
                    hud_col = QColor(BAND_PALETTE[idx])
            else:
                cur_f = self._x_to_freq(mx, plot)
                cur_d = self._y_to_drive(my, plot) * (PANEL_DRIVE_TO_DB if self._processor_controls else 1.0)
                hud_str = f"{format_freq(cur_f)}Hz ({freq_to_note(cur_f)})"
                if self._wave_shaper_drive is None:
                    hud_str += f"   Drive {cur_d:.1f} dB"
                hud_col = QColor("#B4B4BE")

            hud_w = 248 if self._dragging_target is not None else 178
            hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor(21, 21, 24, 215))
            p.drawRoundedRect(hud_rect, 3.5, 3.5)
            p.setPen(hud_col)
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 8. 控件：右侧 Master Output 推子 + 实时峰值电平表 (复用 EQ-UI)
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
        self.setFixedWidth(60)
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
        return QRectF(18.0, 10.0, 4.0, max(40.0, self.height() - 36.0))

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

        meter_rect = QRectF(43.0, tr.top(), 5.0, tr.height())
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

        clip_rect = QRectF(43.0, tr.top() - 7.0, 5.0, 4.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)

        norm = (20.0 - self._gain) / 40.0
        cy = tr.top() + norm * tr.height()
        cap_w, cap_h = 18.0, 24.0
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
        p.drawText(QRectF(0, self.height() - 22, self.width(), 18), Qt.AlignmentFlag.AlignCenter, val_str)


# =====================================================================
# 9. 控件：中右部全局并行混合 & 磁带停机颤音控制面板 (Global & Tape FX)
# =====================================================================
class GlobalAndTapePanel(QFrame):
    globalDriveChanged = pyqtSignal(float)
    mixChanged = pyqtSignal(float)
    flutterChanged = pyqtSignal(float, float)       # depth_pct, rate_hz
    tapeStopTriggered = pyqtSignal(bool, float)     # active, stop_time_ms

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("GlobalTapePanel")
        self.setFixedWidth(214)
        self.setStyleSheet("""
            QFrame#GlobalTapePanel {
                background-color: #222226;
                border-left: 1px solid #141416;
            }
            QLabel#SecHeader {
                color: #787882;
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 0.8px;
            }
            QLabel#MiniTitle {
                color: #9A9AA2;
                font-size: 10px;
                font-weight: 600;
            }
        """)

        self._updating = False
        self._global_drive = 0.0
        self._drive_in_percent = False
        self._mix = 100.0
        self._flutter_depth = 12.0
        self._flutter_rate = 3.2
        self._stop_time_ms = 420.0
        self._stop_latched = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- 区块 1: MASTER DRIVE & PARALLEL MIX ---
        self.master_sec = QWidget()
        m_lay = QVBoxLayout(self.master_sec)
        m_lay.setContentsMargins(10, 8, 10, 10)
        m_lay.setSpacing(8)

        m_hdr = QLabel("MASTER & PARALLEL MIX")
        m_hdr.setObjectName("SecHeader")
        m_lay.addWidget(m_hdr)

        m_knobs_row = QHBoxLayout()
        m_knobs_row.setSpacing(8)

        # Global Input Drive
        drv_col = QVBoxLayout()
        drv_col.setSpacing(4)
        self.drive_label = QLabel("Input Drive")
        self.drive_label.setObjectName("MiniTitle")
        self.drive_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.g_drive_knob = ProRotaryKnob(-12.0, 24.0, 0.0, 0.0, "#28D8F0", mode="bipolar", size=38)
        self.g_drive_knob.valueChanged.connect(self._on_drive_knob)
        self.g_drive_pill = EditableValuePill("0.0 dB", "#28D8F0", width=62)
        self.g_drive_pill.valueEdited.connect(self._on_drive_text)
        drv_col.addWidget(self.drive_label)
        drv_col.addWidget(self.g_drive_knob, 0, Qt.AlignmentFlag.AlignCenter)
        drv_col.addWidget(self.g_drive_pill, 0, Qt.AlignmentFlag.AlignCenter)

        # Parallel Mix (Dry/Wet)
        mix_col = QVBoxLayout()
        mix_col.setSpacing(4)
        self.mix_label = QLabel("Parallel Mix")
        self.mix_label.setObjectName("MiniTitle")
        self.mix_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mix_knob = ProRotaryKnob(0.0, 100.0, 100.0, 100.0, "#4CD490", mode="linear", size=38)
        self.mix_knob.valueChanged.connect(self._on_mix_knob)
        self.mix_pill = EditableValuePill("100%", "#4CD490", width=62)
        self.mix_pill.valueEdited.connect(self._on_mix_text)
        mix_col.addWidget(self.mix_label)
        mix_col.addWidget(self.mix_knob, 0, Qt.AlignmentFlag.AlignCenter)
        mix_col.addWidget(self.mix_pill, 0, Qt.AlignmentFlag.AlignCenter)

        m_knobs_row.addLayout(drv_col)
        m_knobs_row.addLayout(mix_col)
        m_lay.addLayout(m_knobs_row)
        root.addWidget(self.master_sec)

        # --- 区块 2: TAPE FLUTTER & TAPE STOP (磁带停机颤音) ---
        self.tape_sec = QWidget()
        t_lay = QVBoxLayout(self.tape_sec)
        t_lay.setContentsMargins(10, 8, 10, 10)
        t_lay.setSpacing(8)

        t_hdr = QLabel("TAPE FLUTTER & BRAKE")
        t_hdr.setObjectName("SecHeader")
        t_lay.addWidget(t_hdr)

        t_knobs_row = QHBoxLayout()
        t_knobs_row.setSpacing(6)

        # Wow & Flutter Depth
        fl_col = QVBoxLayout()
        fl_col.setSpacing(4)
        fl_lbl = QLabel("Flutter")
        fl_lbl.setObjectName("MiniTitle")
        fl_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.flutter_knob = ProRotaryKnob(0.0, 100.0, 12.0, 0.0, "#F0A848", mode="linear", size=34)
        self.flutter_knob.valueChanged.connect(self._on_flutter_knob)
        self.flutter_pill = EditableValuePill("12%", "#F0A848", width=56)
        self.flutter_pill.valueEdited.connect(self._on_flutter_text)
        fl_col.addWidget(fl_lbl)
        fl_col.addWidget(self.flutter_knob, 0, Qt.AlignmentFlag.AlignCenter)
        fl_col.addWidget(self.flutter_pill, 0, Qt.AlignmentFlag.AlignCenter)

        # Flutter Rate
        rt_col = QVBoxLayout()
        rt_col.setSpacing(4)
        rt_lbl = QLabel("Rate")
        rt_lbl.setObjectName("MiniTitle")
        rt_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rate_knob = ProRotaryKnob(0.2, 12.0, 3.2, 3.0, "#F0A848", mode="log", size=34)
        self.rate_knob.valueChanged.connect(self._on_rate_knob)
        self.rate_pill = EditableValuePill("3.2Hz", "#F0A848", width=56)
        self.rate_pill.valueEdited.connect(self._on_rate_text)
        rt_col.addWidget(rt_lbl)
        rt_col.addWidget(self.rate_knob, 0, Qt.AlignmentFlag.AlignCenter)
        rt_col.addWidget(self.rate_pill, 0, Qt.AlignmentFlag.AlignCenter)

        # Tape Stop Time
        st_col = QVBoxLayout()
        st_col.setSpacing(4)
        st_lbl = QLabel("Brake")
        st_lbl.setObjectName("MiniTitle")
        st_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stop_time_knob = ProRotaryKnob(50.0, 2000.0, 420.0, 420.0, "#F26D6D", mode="log", size=34)
        self.stop_time_knob.valueChanged.connect(self._on_stop_time_knob)
        self.stop_time_pill = EditableValuePill("420ms", "#F26D6D", width=56)
        self.stop_time_pill.valueEdited.connect(self._on_stop_time_text)
        st_col.addWidget(st_lbl)
        st_col.addWidget(self.stop_time_knob, 0, Qt.AlignmentFlag.AlignCenter)
        st_col.addWidget(self.stop_time_pill, 0, Qt.AlignmentFlag.AlignCenter)

        t_knobs_row.addLayout(fl_col)
        t_knobs_row.addLayout(rt_col)
        t_knobs_row.addLayout(st_col)
        t_lay.addLayout(t_knobs_row)

        # 磁带停机触发按钮 (按住瞬间停机 / 右键锁定停机)
        self.stop_btn = QPushButton("TAPE STOP  (按住停机 / 右键锁定)")
        self.stop_btn.setFixedHeight(26)
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.setToolTip("左键按住执行磁带减速停机，松开恢复；右键切换锁定停机状态")
        self.stop_btn.pressed.connect(self._on_stop_pressed)
        self.stop_btn.released.connect(self._on_stop_released)
        self.stop_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.stop_btn.customContextMenuRequested.connect(self._on_stop_right_click)
        self._update_stop_btn_style(False)
        t_lay.addWidget(self.stop_btn)

        t_lay.addStretch()
        root.addWidget(self.tape_sec, 1)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setPen(QPen(QColor("#141416"), 1.0))
        y = self.tape_sec.y() + 0.5
        p.drawLine(QPointF(0.0, y), QPointF(float(self.width()), y))

    def _update_stop_btn_style(self, active: bool):
        if active:
            self.stop_btn.setStyleSheet("""
                QPushButton {
                    background-color: #3A181B;
                    color: #F26D6D;
                    border: 1px solid #F26D6D;
                    border-radius: 3px;
                    font-size: 10px;
                    font-weight: 700;
                }
            """)
        else:
            self.stop_btn.setStyleSheet("""
                QPushButton {
                    background-color: #323238;
                    color: #CFCFD6;
                    border: none;
                    border-radius: 3px;
                    font-size: 10px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background-color: #3C3C44;
                    color: #FFFFFF;
                }
            """)

    def use_wave_shaper_drive(self) -> None:
        self._drive_in_percent = True
        self.drive_label.setText(I18N.tr("audio.fx.drive", "Drive (%)"))
        self.g_drive_knob.setScale(0.0, 100.0, "linear")

    def set_processor_profile(self, multiband: bool) -> None:
        self.tape_sec.hide()
        for widget in (self.drive_label, self.g_drive_knob, self.g_drive_pill):
            widget.setVisible(not multiband)
        for widget in (self.mix_label, self.mix_knob, self.mix_pill):
            widget.setVisible(multiband)

    def _drive_text(self, value: float) -> str:
        if self._drive_in_percent:
            return f"{value:.1f}%"
        return f"{value:+.1f} dB" if abs(value) >= 0.05 else "0.0 dB"

    def syncState(self, g_drive: float, mix: float, fl_depth: float, fl_rate: float, stop_ms: float):
        self._updating = True
        self._global_drive = g_drive
        self._mix = mix
        self._flutter_depth = fl_depth
        self._flutter_rate = fl_rate
        self._stop_time_ms = stop_ms

        self.g_drive_knob.setValue(g_drive)
        self.g_drive_pill.setText(self._drive_text(g_drive))

        self.mix_knob.setValue(mix)
        self.mix_pill.setText(f"{int(round(mix))}%")

        self.flutter_knob.setValue(fl_depth)
        self.flutter_pill.setText(f"{int(round(fl_depth))}%")

        self.rate_knob.setValue(fl_rate)
        self.rate_pill.setText(f"{fl_rate:.1f}Hz")

        self.stop_time_knob.setValue(stop_ms)
        self.stop_time_pill.setText(f"{int(round(stop_ms))}ms")
        self._updating = False

    def _on_drive_knob(self, val: float):
        self._global_drive = round(val, 1)
        self.g_drive_pill.setText(self._drive_text(self._global_drive))
        if not self._updating:
            self.globalDriveChanged.emit(self._global_drive)

    def _on_drive_text(self, text: str):
        value = parse_number(text, self._global_drive)
        if math.isfinite(value):
            self.g_drive_knob.setValue(round(value, 1), emit=True)

    def _on_mix_knob(self, val: float):
        self._mix = round(val, 0)
        self.mix_pill.setText(f"{int(self._mix)}%")
        if not self._updating:
            self.mixChanged.emit(self._mix)

    def _on_mix_text(self, text: str):
        val = max(0.0, min(100.0, round(parse_number(text, self._mix), 0)))
        self.mix_knob.setValue(val, emit=True)

    def _on_flutter_knob(self, val: float):
        self._flutter_depth = round(val, 0)
        self.flutter_pill.setText(f"{int(self._flutter_depth)}%")
        if not self._updating:
            self.flutterChanged.emit(self._flutter_depth, self._flutter_rate)

    def _on_flutter_text(self, text: str):
        val = max(0.0, min(100.0, round(parse_number(text, self._flutter_depth), 0)))
        self.flutter_knob.setValue(val, emit=True)

    def _on_rate_knob(self, val: float):
        self._flutter_rate = round(val, 1)
        self.rate_pill.setText(f"{self._flutter_rate:.1f}Hz")
        if not self._updating:
            self.flutterChanged.emit(self._flutter_depth, self._flutter_rate)

    def _on_rate_text(self, text: str):
        val = max(0.2, min(12.0, round(parse_number(text, self._flutter_rate), 1)))
        self.rate_knob.setValue(val, emit=True)

    def _on_stop_time_knob(self, val: float):
        self._stop_time_ms = round(val, 0)
        self.stop_time_pill.setText(f"{int(self._stop_time_ms)}ms")

    def _on_stop_time_text(self, text: str):
        val = max(50.0, min(2000.0, round(parse_number(text, self._stop_time_ms), 0)))
        self.stop_time_knob.setValue(val, emit=True)

    def _on_stop_pressed(self):
        if not self._stop_latched:
            self._update_stop_btn_style(True)
            self.tapeStopTriggered.emit(True, self._stop_time_ms)

    def _on_stop_released(self):
        if not self._stop_latched:
            self._update_stop_btn_style(False)
            self.tapeStopTriggered.emit(False, self._stop_time_ms)

    def _on_stop_right_click(self, pos):
        self._stop_latched = not self._stop_latched
        self._update_stop_btn_style(self._stop_latched)
        self.stop_btn.setText(
            "TAPE STOP  [已锁定]" if self._stop_latched else "TAPE STOP  (按住停机 / 右键锁定)"
        )
        self.tapeStopTriggered.emit(self._stop_latched, self._stop_time_ms)


# =====================================================================
# 10. 控件：底部三频段独立饱和控制卡片 (LOW / MID / HIGH)
# =====================================================================
def _make_knob_cell(
    title: str,
    knob: ProRotaryKnob,
    pill: EditableValuePill,
    min_hint: str,
    max_hint: str,
) -> QWidget:
    """紧凑旋钮参数单元：标题 / 旋钮 / 范围提示 / 数值胶囊垂直堆叠并整体居中
    （对齐 multiband_comp KnobParameterCell 与 Stereo Imager 的参数列节奏）"""
    cell = QWidget()
    lay = QVBoxLayout(cell)
    lay.setContentsMargins(2, 0, 2, 0)
    lay.setSpacing(3)

    lay.addStretch(1)
    lbl = QLabel(title)
    lbl.setObjectName("ParamTitle")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(lbl)
    lay.addWidget(knob, 0, Qt.AlignmentFlag.AlignCenter)

    hints = QHBoxLayout()
    hints.setContentsMargins(1, 0, 1, 0)
    lo_lbl = QLabel(min_hint)
    lo_lbl.setObjectName("RangeHint")
    hi_lbl = QLabel(max_hint)
    hi_lbl.setObjectName("RangeHint")
    hi_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
    hints.addWidget(lo_lbl)
    hints.addStretch()
    hints.addWidget(hi_lbl)
    lay.addLayout(hints)

    lay.addWidget(pill, 0, Qt.AlignmentFlag.AlignCenter)
    lay.addStretch(1)
    return cell


class ProSatBandCard(QFrame):
    bandChanged = pyqtSignal(int, dict)
    soloToggled = pyqtSignal(int, bool)
    cardSelected = pyqtSignal(int)

    def __init__(self, band_data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(band_data)
        self._idx = self._data["index"] - 1
        self._accent = BAND_PALETTE[self._idx]
        self._updating = False
        self._selected = False
        self._processor_controls = False

        self.setObjectName("BandCard")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- 1. 顶部栏: 频段开关 + Solo 独奏 + 染色电路选择器 ---
        header = QWidget()
        header.setFixedHeight(36)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 6, 10, 5)
        h_lay.setSpacing(6)

        self.band_btn = QPushButton(f"BAND {self._data['index']}  ·  {self._data['name']}")
        self.band_btn.setCheckable(True)
        self.band_btn.setFixedHeight(22)
        self.band_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.band_btn.clicked.connect(self._on_toggle_band)
        h_lay.addWidget(self.band_btn, 1)

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

        self.model_selector = SatModelSelector(self._data["model"], self._accent)
        self.model_selector.modelChanged.connect(self._on_model_changed)
        h_lay.addWidget(self.model_selector)
        root.addWidget(header)

        # --- 2. 参数区: Drive | Tone | Bias | Level 四列紧凑参数单元 ---
        self.param_sec = QFrame()
        self.param_sec.setObjectName("SubSection")
        ps_lay = QHBoxLayout(self.param_sec)
        ps_lay.setContentsMargins(7, 6, 7, 7)
        ps_lay.setSpacing(5)

        self.drive_knob = ProRotaryKnob(0.0, 24.0, self._data["drive"], 0.0, self._accent, mode="linear")
        self.drive_knob.valueChanged.connect(self._on_drive_knob)
        self.drive_pill = EditableValuePill("0.0 dB", self._accent, width=58)
        self.drive_pill.valueEdited.connect(self._on_drive_text)

        self.tone_knob = ProRotaryKnob(-100.0, 100.0, self._data["tone"], 0.0, self._accent, mode="bipolar")
        self.tone_knob.valueChanged.connect(self._on_tone_knob)
        self.tone_pill = EditableValuePill("0%", self._accent, width=58)
        self.tone_pill.valueEdited.connect(self._on_tone_text)

        self.bias_knob = ProRotaryKnob(-100.0, 100.0, self._data["bias"], 0.0, self._accent, mode="bipolar")
        self.bias_knob.valueChanged.connect(self._on_bias_knob)
        self.bias_pill = EditableValuePill("0%", self._accent, width=58)
        self.bias_pill.valueEdited.connect(self._on_bias_text)

        self.level_knob = ProRotaryKnob(-12.0, 12.0, self._data["level"], 0.0, self._accent, mode="bipolar")
        self.level_knob.valueChanged.connect(self._on_level_knob)
        self.level_pill = EditableValuePill("0.0 dB", self._accent, width=58)
        self.level_pill.valueEdited.connect(self._on_level_text)

        for title, knob, pill, lo, hi in (
            ("Drive", self.drive_knob, self.drive_pill, "0", "24"),
            ("Tone", self.tone_knob, self.tone_pill, "Dark", "Bright"),
            ("Bias / Glue", self.bias_knob, self.bias_pill, "Soft", "Hard"),
            ("Band Level", self.level_knob, self.level_pill, "-12", "+12"),
        ):
            ps_lay.addWidget(_make_knob_cell(title, knob, pill, lo, hi), 1)
        root.addWidget(self.param_sec, 1)

        self.syncFromData(self._data)

    def use_processor_controls(self) -> None:
        self._processor_controls = True
        self.solo_btn.hide()
        self.drive_knob.setScale(0.0, 36.0, "linear")
        self.tone_knob.setScale(-12.0, 12.0, "bipolar")
        self.level_knob.setScale(-24.0, 12.0, "linear")
        self.drive_knob.parentWidget().findChildren(QLabel, "RangeHint")[-1].setText("36")
        hints = self.tone_knob.parentWidget().findChildren(QLabel, "RangeHint")
        hints[0].setText("-12")
        hints[-1].setText("+12")
        self.level_knob.parentWidget().findChildren(QLabel, "RangeHint")[0].setText("-24")

    def mousePressEvent(self, event):
        self.cardSelected.emit(self._idx)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setPen(QPen(QColor("#141416"), 1.0))
        # 标题栏与参数区之间的通长横向 1px 黑线
        y = self.param_sec.y() + 0.5
        p.drawLine(QPointF(0.0, y), QPointF(float(self.width()), y))

    def setSelected(self, selected: bool):
        self._selected = selected
        self._apply_card_style()

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
                color: {"#9A9AA2" if en else "#6E6E76"};
                font-size: 10px;
                font-weight: 600;
            }}
            QLabel#RangeHint {{
                color: {"#787882" if en else "#55555C"};
                font-size: 9px;
            }}
        """)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])

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

        self.model_selector.setCurrentModel(self._data["model"])
        self.model_selector.setBandActive(en)

        d = self._data["drive"] * (PANEL_DRIVE_TO_DB if self._processor_controls else 1.0)
        self.drive_knob.setValue(d)
        self.drive_knob.setBandActive(en)
        self.drive_pill.setText(f"{d:.1f} dB")
        self.drive_pill.setBandActive(en)

        t = self._data["tone"] * (0.12 if self._processor_controls else 1.0)
        self.tone_knob.setValue(t)
        self.tone_knob.setBandActive(en)
        self.tone_pill.setText(f"{t:+.1f} dB" if self._processor_controls else
                               (f"{t:+.0f}%" if abs(t) >= 0.5 else "0%"))
        self.tone_pill.setBandActive(en)

        b_val = self._data["bias"]
        self.bias_knob.setValue(b_val)
        self.bias_knob.setBandActive(en)
        self.bias_pill.setText(f"{b_val:+.0f}%" if abs(b_val) >= 0.5 else "0%")
        self.bias_pill.setBandActive(en)

        lvl = self._data["level"]
        self.level_knob.setValue(lvl)
        self.level_knob.setBandActive(en)
        self.level_pill.setText(f"{lvl:+.1f} dB" if abs(lvl) >= 0.05 else "0.0 dB")
        self.level_pill.setBandActive(en)

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

    def _on_model_changed(self, model: str):
        self._data["model"] = model
        self._emit_change()

    def _on_drive_knob(self, val: float):
        self._data["drive"] = (round(val, 1) / PANEL_DRIVE_TO_DB
                               if self._processor_controls else round(val, 1))
        self._emit_change()

    def _on_drive_text(self, text: str):
        scale = PANEL_DRIVE_TO_DB if self._processor_controls else 1.0
        limit = 36.0 if self._processor_controls else 24.0
        val = parse_number(text, self._data["drive"] * scale)
        self._on_drive_knob(max(0.0, min(limit, val)))

    def _on_tone_knob(self, val: float):
        self._data["tone"] = round(val, 1) / 0.12 if self._processor_controls else round(val, 0)
        self._emit_change()

    def _on_tone_text(self, text: str):
        scale = 0.12 if self._processor_controls else 1.0
        limit = 12.0 if self._processor_controls else 100.0
        val = parse_number(text, self._data["tone"] * scale)
        self._on_tone_knob(max(-limit, min(limit, val)))

    def _on_bias_knob(self, val: float):
        self._data["bias"] = round(val, 0)
        self._emit_change()

    def _on_bias_text(self, text: str):
        val = max(-100.0, min(100.0, round(parse_number(text, self._data["bias"]), 0)))
        self._data["bias"] = val
        self._emit_change()

    def _on_level_knob(self, val: float):
        self._data["level"] = round(val, 1)
        self._emit_change()

    def _on_level_text(self, text: str):
        lower = -24.0 if self._processor_controls else -12.0
        val = max(lower, min(12.0, round(parse_number(text, self._data["level"]), 1)))
        self._data["level"] = val
        self._emit_change()


# =====================================================================
# 11. 主面板：多段模拟饱和染色器 (Studio Harmonic Saturator)
# =====================================================================
class StudioSaturationWidget(QWidget):
    # 对接后端音频 DSP 引擎的完整信号接口
    bypassChanged = pyqtSignal(bool)
    globalDriveChanged = pyqtSignal(float)
    mixChanged = pyqtSignal(float)
    outputGainChanged = pyqtSignal(float)
    autoGainChanged = pyqtSignal(bool)
    oversamplingChanged = pyqtSignal(str)
    crossoversChanged = pyqtSignal(float, float)
    bandChanged = pyqtSignal(int, dict)
    bandSoloChanged = pyqtSignal(int, bool)
    tapeFlutterChanged = pyqtSignal(float, float)
    tapeStopTriggered = pyqtSignal(bool, float)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Audio 1", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Harmonic Saturator — {track_name}")
        self.resize(1020, 700)
        self.setMinimumSize(900, 620)

        self._active_bank = "A"
        self._selected_band = 1
        self._drive_key = "global_drive"

        # 磁带停机视觉惯性仿真状态
        self._tape_speed = 1.0
        self._tape_braking = False
        self._tape_stop_ms = 420.0

        self._banks = {
            "A": {
                "enabled": True,
                "global_drive": 1.5,
                "mix": 100.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "oversampling": "2x",
                "xover_low": 210.0,
                "xover_high": 3200.0,
                "flutter_depth": 12.0,
                "flutter_rate": 3.2,
                "stop_time_ms": 420.0,
                "bands": default_bands_warm_glue(),
            },
            "B": {
                "enabled": True,
                "global_drive": 0.0,
                "mix": 100.0,
                "output_gain": 0.0,
                "auto_gain": True,
                "oversampling": "2x",
                "xover_low": 200.0,
                "xover_high": 3000.0,
                "flutter_depth": 0.0,
                "flutter_rate": 3.0,
                "stop_time_ms": 420.0,
                "bands": default_bands_clean(),
            },
        }

        self._presets = {
            "Warm Vocal & Bus Glue": copy.deepcopy(self._banks["A"]),
            "Clean Init": copy.deepcopy(self._banks["B"]),
            "Punchy Kick & Sub": {
                "enabled": True, "global_drive": 2.0, "mix": 85.0, "output_gain": -0.5,
                "auto_gain": True, "oversampling": "4x", "xover_low": 160.0, "xover_high": 2800.0,
                "flutter_depth": 0.0, "flutter_rate": 2.5, "stop_time_ms": 350.0,
                "bands": [
                    {"index": 1, "name": "LOW",  "enabled": True, "solo": False, "model": "Transistor", "drive": 11.5, "tone": -15.0, "bias": 35.0, "level": 1.2},
                    {"index": 2, "name": "MID",  "enabled": True, "solo": False, "model": "Tape",       "drive": 5.0,  "tone": 8.0,   "bias": 10.0, "level": -0.5},
                    {"index": 3, "name": "HIGH", "enabled": True, "solo": False, "model": "Tube",       "drive": 3.5,  "tone": 22.0,  "bias": 0.0,  "level": 0.5},
                ],
            },
            "Parallel Drum Smash": {
                "enabled": True, "global_drive": 6.0, "mix": 42.0, "output_gain": 0.0,
                "auto_gain": False, "oversampling": "4x", "xover_low": 240.0, "xover_high": 4000.0,
                "flutter_depth": 5.0, "flutter_rate": 4.0, "stop_time_ms": 500.0,
                "bands": [
                    {"index": 1, "name": "LOW",  "enabled": True, "solo": False, "model": "Tube",       "drive": 14.0, "tone": 10.0,  "bias": 45.0, "level": 1.0},
                    {"index": 2, "name": "MID",  "enabled": True, "solo": False, "model": "Transistor", "drive": 16.5, "tone": 18.0,  "bias": 55.0, "level": 0.5},
                    {"index": 3, "name": "HIGH", "enabled": True, "solo": False, "model": "Tape",       "drive": 10.0, "tone": -10.0, "bias": 20.0, "level": -1.0},
                ],
            },
            "Vintage Lo-Fi Reel": {
                "enabled": True, "global_drive": 4.5, "mix": 100.0, "output_gain": 0.5,
                "auto_gain": True, "oversampling": "2x", "xover_low": 260.0, "xover_high": 2400.0,
                "flutter_depth": 48.0, "flutter_rate": 5.8, "stop_time_ms": 680.0,
                "bands": [
                    {"index": 1, "name": "LOW",  "enabled": True, "solo": False, "model": "Tape", "drive": 9.5,  "tone": -28.0, "bias": 20.0,  "level": 1.0},
                    {"index": 2, "name": "MID",  "enabled": True, "solo": False, "model": "Tube", "drive": 11.0, "tone": -15.0, "bias": 30.0,  "level": 0.5},
                    {"index": 3, "name": "HIGH", "enabled": True, "solo": False, "model": "Tape", "drive": 8.0,  "tone": -55.0, "bias": -25.0, "level": -3.0},
                ],
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        self._anim_phase = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_animation)
        self._demo_timer.start(40)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            SatModelSelector, ProSaturationGraphWidget, MasterOutputStrip {{
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
            QPushButton#TogglePillBtn {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
            }}
            QPushButton#TogglePillBtn:hover {{
                background-color: #3C3C44;
                color: #FFFFFF;
            }}
            QPushButton#TogglePillBtn:checked {{
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
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
        # 1. 顶部整合工具栏 (电源 + 标题 + 预设 + A/B + 全局电路 + 过采样 + 自动增益)
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(38)
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb_lay = QHBoxLayout(top_bar)
        self._top_controls = tb_lay
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True)
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"SATURATION  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(10)

        # 预设管理
        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存为新预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
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

        tb_lay.addStretch()

        # 全局快速切换三模式 (Multi / All Tape / All Tube / All Transistor)
        m_lbl = QLabel("Circuit")
        m_lbl.setStyleSheet("color: #9A9AA2; font-size: 11px; font-weight: 600;")
        tb_lay.addWidget(m_lbl)

        self.global_model_combo = ChevronComboBox()
        self.global_model_combo.addItems(["Multi-Band", "All Tape", "All Tube", "All Transistor"])
        self.global_model_combo.setFixedWidth(self.global_model_combo.sizeHint().width())
        self.global_model_combo.setToolTip("一键统一切换三频段饱和电路，或保持 Multi-Band 独立混合")
        self.global_model_combo.currentTextChanged.connect(self._on_global_model_changed)
        tb_lay.addWidget(self.global_model_combo)

        # 过采样倍率 (Oversampling)
        self.os_combo = ChevronComboBox()
        self.os_combo.addItems(["1x", "2x", "4x"])
        self.os_combo.setFixedWidth(self.os_combo.sizeHint().width())
        self.os_combo.setToolTip("抗混叠过采样倍率 (Oversampling)")
        self.os_combo.currentTextChanged.connect(self._on_oversampling_changed)
        tb_lay.addWidget(self.os_combo)

        # 自动增益补偿开关 (Auto-Gain)
        self.auto_gain_btn = QPushButton("AUTO")
        self.auto_gain_btn.setObjectName("TogglePillBtn")
        self.auto_gain_btn.setCheckable(True)
        self.auto_gain_btn.setFixedSize(44, 22)
        self.auto_gain_btn.setToolTip("自动输出响度补偿 (Auto-Gain Compensation)")
        self.auto_gain_btn.clicked.connect(self._on_auto_gain_toggled)
        tb_lay.addWidget(self.auto_gain_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(46, 22)
        reset_btn.clicked.connect(self.reset_saturation)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部主区: 多段频谱/传输视窗 + 全局并行与磁带停机颤音面板 + 输出表
        # -------------------------------------------------------------
        mid_row = QHBoxLayout()
        mid_row.setContentsMargins(0, 0, 0, 0)
        mid_row.setSpacing(0)

        self.sat_graph = ProSaturationGraphWidget()
        self.sat_graph.crossoverDragged.connect(self._on_graph_xover_dragged)
        self.sat_graph.bandNodeDragged.connect(self._on_graph_node_dragged)
        self.sat_graph.bandToneScrolled.connect(self._on_graph_tone_scrolled)
        self.sat_graph.bandNodeClicked.connect(self._select_band_card)
        self.sat_graph.bandToggleRequested.connect(self._on_graph_band_toggled)
        self.sat_graph.bandModelRequested.connect(self._on_graph_model_requested)
        mid_row.addWidget(self.sat_graph, 1)

        # 全局并行 Mix + 磁带停机颤音控制面板
        self.global_tape_panel = GlobalAndTapePanel()
        self.global_tape_panel.globalDriveChanged.connect(self._on_global_drive_changed)
        self.global_tape_panel.mixChanged.connect(self._on_mix_changed)
        self.global_tape_panel.flutterChanged.connect(self._on_flutter_changed)
        self.global_tape_panel.tapeStopTriggered.connect(self._on_tape_stop_triggered)
        mid_row.addWidget(self.global_tape_panel)

        # 右侧输出监控列
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
        mid_row.addWidget(meter_col_frame)

        main_lay.addLayout(mid_row, 5)

        # -------------------------------------------------------------
        # 3. 底部三频段独立饱和参数区 (LOW / MID / HIGH 贴合单元格)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        self.band_cards: list[ProSatBandCard] = []
        for b in self._current_state()["bands"]:
            card = ProSatBandCard(b)
            card.bandChanged.connect(self._on_band_card_changed)
            card.soloToggled.connect(self._on_band_solo_toggled)
            card.cardSelected.connect(self._select_band_card)
            cards_row.addWidget(card, 1)
            self.band_cards.append(card)

        # 参数面板按内容自适应高度（消除大面积空白），富余空间全部让给频谱视窗
        main_lay.addLayout(cards_row, 0)

    # -----------------------------------------------------------------
    # 状态管理与联动逻辑
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def use_wave_shaper_drive(self) -> None:
        """Expose the existing full-band DSP drive in the native input-drive control."""
        self._drive_key = "drive"
        for state in self._banks.values():
            state.update(drive=0.0, model_profile="legacy", crossover_mode="legacy")
        for name, state in self._presets.items():
            legacy = name == "Clean Init"
            state.update(drive=0.0, model_profile="legacy" if legacy else "multiband",
                         crossover_mode="legacy" if legacy else "dual")
        self.profile_combo = ChevronComboBox(self)
        self.profile_combo.addItem(I18N.tr("mixer.effect_panel.saturation_wave", "WaveShaper"), "legacy")
        self.profile_combo.addItem(I18N.tr("mixer.effect_panel.saturation_multi", "Multiband"), "multiband")
        self.profile_combo.currentIndexChanged.connect(self._change_profile)
        self._top_controls.insertWidget(self._top_controls.indexOf(self.os_combo), self.profile_combo)
        self.crossover_upgrade = QPushButton(
            I18N.tr("mixer.effect_panel.saturation_upgrade", "Separate crossovers"), self)
        self.crossover_upgrade.setObjectName("ToolBtn")
        self.crossover_upgrade.setToolTip(I18N.tr(
            "mixer.effect_panel.saturation_upgrade_hint",
            "Enable independent low and high crossovers for this saved effect"))
        self.crossover_upgrade.clicked.connect(self._enable_dual_crossovers)
        self._top_controls.insertWidget(self._top_controls.indexOf(self.os_combo), self.crossover_upgrade)
        self.global_tape_panel.use_wave_shaper_drive()
        self.auto_gain_btn.hide()
        self.sat_graph._processor_controls = True
        self.sat_graph._max_band_drive = 36.0 / PANEL_DRIVE_TO_DB
        for card in self.band_cards:
            card.use_processor_controls()
        self._refresh_all_views()

    def _change_profile(self, index: int) -> None:
        profile = self.profile_combo.itemData(index)
        state = self._current_state()
        if not profile or state["model_profile"] == profile:
            return
        state["model_profile"] = profile
        if profile == "multiband" and not state.get("_configured_multiband"):
            state["crossover_mode"] = "dual"
            state["_configured_multiband"] = True
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def _enable_dual_crossovers(self) -> None:
        self._current_state()["crossover_mode"] = "dual"
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

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
            QPushButton:hover { background-color: #3C3C44; color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_css if self._active_bank == "A" else inactive_css)
        self.btn_b.setStyleSheet(active_css if self._active_bank == "B" else inactive_css)
        # 箭头按钮与 A/B 完全相同的样式设置路径（QSS 需在布局后由此处统一刷新）
        self.btn_copy_ab.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: #3C3C44; color: #FFFFFF; }
        """)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

    def _sync_global_model_combo(self):
        st = self._current_state()
        models = {b["model"] for b in st["bands"]}
        self.global_model_combo.blockSignals(True)
        if len(models) == 1:
            only = next(iter(models))
            self.global_model_combo.setCurrentText(f"All {only}")
        else:
            self.global_model_combo.setCurrentText("Multi-Band")
        self.global_model_combo.blockSignals(False)

    def _refresh_all_views(self):
        st = self._current_state()
        self._update_ab_styles()
        self.power_switch.setChecked(st["enabled"])
        self._sync_global_model_combo()

        self.os_combo.blockSignals(True)
        self.os_combo.setCurrentText(st["oversampling"])
        self.os_combo.blockSignals(False)

        self.auto_gain_btn.setChecked(st["auto_gain"])
        self.master_strip.setGain(st["output_gain"])

        self.global_tape_panel.syncState(
            st.get(self._drive_key, 0.0),
            st["mix"],
            st["flutter_depth"],
            st["flutter_rate"],
            st["stop_time_ms"],
        )
        if self._drive_key == "drive":
            multiband = st.get("model_profile") == "multiband"
            with QSignalBlocker(self.profile_combo):
                self.profile_combo.setCurrentIndex(1 if multiband else 0)
            self._production_multiband = multiband
            self.crossover_upgrade.setVisible(multiband and st.get("crossover_mode") != "dual")
            self.os_combo.setVisible(multiband)
            self.global_model_combo.setVisible(multiband)
            self.master_strip.setEnabled(multiband)
            self.global_tape_panel.set_processor_profile(multiband)
            self.sat_graph.setWaveShaperDrive(None if multiband else st.get("drive", 0.0))

        for i, card in enumerate(self.band_cards):
            if self._drive_key == "drive":
                card.setVisible(self._production_multiband)
            card.syncFromData(st["bands"][i])
            card.setSelected(i == self._selected_band)

        self.sat_graph.setState(
            st["bands"],
            st["enabled"],
            st["global_drive"],
            st["mix"],
            st["xover_low"],
            st["xover_high"],
            self._selected_band,
        )

    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _copy_bank(self):
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._banks[self._active_bank])

    def _select_band_card(self, idx: int):
        self._selected_band = idx
        for i, card in enumerate(self.band_cards):
            card.setSelected(i == idx)
        st = self._current_state()
        self.sat_graph.setState(
            st["bands"], st["enabled"], st["global_drive"], st["mix"],
            st["xover_low"], st["xover_high"], self._selected_band
        )

    def _on_power_toggled(self, enabled: bool):
        st = self._current_state()
        st["enabled"] = enabled
        self.sat_graph.setState(
            st["bands"], st["enabled"], st["global_drive"], st["mix"],
            st["xover_low"], st["xover_high"], self._selected_band
        )
        self.bypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_global_model_changed(self, text: str):
        if text.startswith("All "):
            target_model = text.replace("All ", "").strip()
            st = self._current_state()
            for i, b in enumerate(st["bands"]):
                b["model"] = target_model
                self.band_cards[i].syncFromData(b)
                self.bandChanged.emit(b["index"], copy.deepcopy(b))
            self.sat_graph.setState(
                st["bands"], st["enabled"], st["global_drive"], st["mix"],
                st["xover_low"], st["xover_high"], self._selected_band
            )
            self.stateChanged.emit(self.get_state())

    def _on_oversampling_changed(self, os_str: str):
        self._current_state()["oversampling"] = os_str
        self.oversamplingChanged.emit(os_str)
        self.stateChanged.emit(self.get_state())

    def _on_auto_gain_toggled(self, checked: bool):
        self._current_state()["auto_gain"] = checked
        self.autoGainChanged.emit(checked)
        self.stateChanged.emit(self.get_state())

    def _on_global_drive_changed(self, drive: float) -> None:
        st = self._current_state()
        st[self._drive_key] = drive
        if self._drive_key == "drive":
            self.sat_graph.setWaveShaperDrive(drive)
        self.sat_graph.setState(
            st["bands"], st["enabled"], st["global_drive"], st["mix"],
            st["xover_low"], st["xover_high"], self._selected_band
        )
        self.globalDriveChanged.emit(drive)
        self.stateChanged.emit(self.get_state())

    def _on_mix_changed(self, mix_pct: float):
        st = self._current_state()
        st["mix"] = mix_pct
        self.sat_graph.setState(
            st["bands"], st["enabled"], st["global_drive"], st["mix"],
            st["xover_low"], st["xover_high"], self._selected_band
        )
        self.mixChanged.emit(mix_pct)
        self.stateChanged.emit(self.get_state())

    def _on_flutter_changed(self, depth_pct: float, rate_hz: float):
        st = self._current_state()
        st["flutter_depth"] = depth_pct
        st["flutter_rate"] = rate_hz
        self.sat_graph.setTapeVisualState(self._tape_speed, self._tape_braking, depth_pct)
        self.tapeFlutterChanged.emit(depth_pct, rate_hz)
        self.stateChanged.emit(self.get_state())

    def _on_tape_stop_triggered(self, active: bool, stop_time_ms: float):
        self._tape_braking = active
        self._tape_stop_ms = stop_time_ms
        self._current_state()["stop_time_ms"] = stop_time_ms
        self.tapeStopTriggered.emit(active, stop_time_ms)

    def _on_output_gain_changed(self, gain_db: float):
        self._current_state()["output_gain"] = gain_db
        self.outputGainChanged.emit(gain_db)
        self.stateChanged.emit(self.get_state())

    def _on_band_card_changed(self, band_index: int, band_data: dict):
        st = self._current_state()
        st["bands"][band_index - 1] = band_data
        self._sync_global_model_combo()
        self._select_band_card(band_index - 1)
        self.bandChanged.emit(band_index, band_data)
        self.stateChanged.emit(self.get_state())

    def _on_band_solo_toggled(self, band_index: int, is_solo: bool):
        st = self._current_state()
        for i, b in enumerate(st["bands"]):
            if b["index"] != band_index and b.get("solo", False):
                b["solo"] = False
                self.band_cards[i].syncFromData(b)
        self.bandSoloChanged.emit(band_index, is_solo)

    def _on_graph_xover_dragged(self, low_hz: float, high_hz: float):
        st = self._current_state()
        st["xover_low"] = low_hz
        st["xover_high"] = high_hz
        if self._drive_key == "drive":
            st["crossover_mode"] = "dual"
            self.crossover_upgrade.hide()
        self.sat_graph.setState(
            st["bands"], st["enabled"], st["global_drive"], st["mix"],
            st["xover_low"], st["xover_high"], self._selected_band
        )
        self.crossoversChanged.emit(low_hz, high_hz)
        self.stateChanged.emit(self.get_state())

    def _on_graph_node_dragged(self, idx: int, new_drive: float, new_tone: float):
        st = self._current_state()
        b = st["bands"][idx]
        b["drive"] = new_drive
        b["tone"] = new_tone
        self.band_cards[idx].syncFromData(b)
        self._select_band_card(idx)
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_tone_scrolled(self, idx: int, new_tone: float):
        st = self._current_state()
        b = st["bands"][idx]
        b["tone"] = new_tone
        self.band_cards[idx].syncFromData(b)
        self.sat_graph.setState(
            st["bands"], st["enabled"], st["global_drive"], st["mix"],
            st["xover_low"], st["xover_high"], self._selected_band
        )
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_band_toggled(self, idx: int):
        st = self._current_state()
        b = st["bands"][idx]
        b["enabled"] = not b["enabled"]
        self.band_cards[idx].syncFromData(b)
        self._select_band_card(idx)
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _on_graph_model_requested(self, idx: int, new_model: str):
        st = self._current_state()
        b = st["bands"][idx]
        b["model"] = new_model
        self.band_cards[idx].syncFromData(b)
        self._sync_global_model_combo()
        self.sat_graph.setState(
            st["bands"], st["enabled"], st["global_drive"], st["mix"],
            st["xover_low"], st["xover_high"], self._selected_band
        )
        self.bandChanged.emit(b["index"], copy.deepcopy(b))
        self.stateChanged.emit(self.get_state())

    def _load_preset(self, name: str):
        if name in self._presets:
            state = copy.deepcopy(self._presets[name])
            if self._drive_key == "drive":
                drive = 0.0 if name == "Clean Init" else self._current_state().get("drive", 0.0)
                state.setdefault("drive", drive)
            self._banks[self._active_bank] = state
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存饱和器预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    def _tick_demo_animation(self):
        st = self._current_state()
        # 1. 磁带停机减速/回弹物理曲线模拟
        dt_ms = 40.0
        step = dt_ms / max(50.0, self._tape_stop_ms)
        if self._tape_braking:
            self._tape_speed = max(0.0, self._tape_speed - step)
        else:
            self._tape_speed = min(1.0, self._tape_speed + step * 1.6)

        self.sat_graph.setTapeVisualState(self._tape_speed, self._tape_braking, st["flutter_depth"])

        # 2. 演示实时频谱 (随磁带停机与颤音产生音高/频谱漂移)
        flutter_mod = (st["flutter_depth"] / 100.0) * 0.04 * math.sin(self._anim_phase * st["flutter_rate"] * 0.5)
        speed_factor = max(0.08, self._tape_speed * (1.0 + flutter_mod))
        self._anim_phase += 0.14 * speed_factor

        post, peak = [], []
        max_lvl = -60.0
        wet_ratio = (st["mix"] / 100.0) if st["enabled"] else 0.0

        for i, f in enumerate(self.sat_graph._spec_freqs):
            eff_f = f / speed_factor
            decades = math.log10(max(100.0, eff_f) / 100.0)
            base = (
                -44.0 + 10.0 * math.exp(-((math.log10(max(20.0, eff_f)) - math.log10(95)) ** 2) / 0.09)
                if eff_f < 100
                else (-36.0 - 15.5 * decades)
            )
            wave = (
                2.4 * math.sin(i * 0.24 + self._anim_phase)
                + 1.7 * math.cos(i * 0.58 - self._anim_phase * 1.3)
            )
            raw_db = base + wave

            # 模拟各频段谐波染色抬升
            b_idx = 0 if f < st["xover_low"] else (1 if f < st["xover_high"] else 2)
            band = st["bands"][b_idx]
            sat_boost = (band["drive"] * 0.22 + (band["tone"] / 100.0) * 2.5) if band["enabled"] else 0.0
            final_db = max(-82.0, min(-10.0, raw_db + wet_ratio * sat_boost + st["output_gain"] * 0.4))
            if self._tape_speed < 0.05:
                final_db = -82.0

            post.append(final_db)
            peak.append(min(-9.0, final_db + 2.2))
            if final_db > max_lvl:
                max_lvl = final_db

        self.sat_graph.update_spectrum(self.sat_graph._spec_freqs, post, peak)
        self.master_strip.setMeterLevel(max_lvl + 6.0)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_saturation(self):
        st = self._current_state()
        st["global_drive"] = 0.0
        st[self._drive_key] = 0.0
        st["mix"] = 100.0
        st["output_gain"] = 0.0
        st["flutter_depth"] = 0.0
        st["bands"] = default_bands_clean()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        if state.get("model_profile") == "multiband":
            self._current_state()["_configured_multiband"] = True
        self._refresh_all_views()

    def update_spectrum(self, freqs_hz: list[float], post_db: list[float], peak_db: list[float] | None = None):
        """接入真实音频引擎 FFT 频谱数据 (调用后自动停止内部演示频谱动画)"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.sat_graph.update_spectrum(freqs_hz, post_db, peak_db)

    def update_output_meter(self, level_db: float):
        """接入真实音频引擎输出峰值电平 (-60.0 ~ 0.0 dBFS)"""
        self.master_strip.setMeterLevel(level_db)

    def update_tape_speed(self, speed_ratio: float):
        """供后端实时回传磁带停机当前转速 (1.0 = 正常, 0.0 = 完全停机)"""
        self._tape_speed = max(0.0, min(1.0, speed_ratio))
        self.sat_graph.setTapeVisualState(
            self._tape_speed, self._tape_braking, self._current_state()["flutter_depth"]
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioSaturationWidget("Audio 1")
    win.show()
    sys.exit(app.exec())
