from __future__ import annotations

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
    QPainter, QColor, QPen, QLinearGradient,
    QPainterPath, QFont, QPolygonF
)


# =====================================================================
# 1. 全局色彩系统与默认状态配置（与 Delay-UI / DeEsser_UI 完全统一）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":   "#28D8F0",  # 主控 / A-B 激活 / Output
    "click":  "#F26D6D",  # De-Click 瞬态爆点检测主题色（珊瑚红）
    "hum":    "#F0A848",  # De-Hum 工频谐波陷波主题色（琥珀金）
    "green":  "#4CD490",  # 自适应 / 修复安全电平色（翡翠绿）
}

FREQ_MIN = 20.0
FREQ_MAX = 4000.0  # De-Hum 聚焦 20Hz ~ 4kHz 工频及高次谐波区


def default_repair_state() -> dict:
    return {
        "enabled": True,
        "output_diff": False,         # True = 仅输出被剔除的咔哒/哼声残差 (Output Clicks/Hum Only)
        "view_focus": "split",        # "split" (双视图分屏) | "click" | "hum"
        "output_gain": 0.0,           # -12.0 ~ +12.0 dB

        # --- De-Click 模块参数 (参考 iZotope RX De-click) ---
        "click_enabled": True,
        "click_algo": "Multi-band (Random)",  # "Single-band" | "Multi-band (Periodic)" | "Multi-band (Random)" | "Low Latency"
        "click_sensitivity": 3.5,     # 0.0 ~ 10.0 (灵敏度)
        "click_widening": 1.2,        # 0.0 ~ 5.0 ms (咔哒宽度 / 瞬态修补扩展)
        "click_skew": 0.0,            # -1.0 ~ +1.0 (高/低频瞬态偏斜)

        # --- De-Hum 模块参数 (参考 iZotope RX De-hum) ---
        "hum_enabled": True,
        "hum_base_freq": 50.0,        # 45.0 ~ 65.0 Hz (50Hz 欧标/国标 或 60Hz 美标微调)
        "hum_harmonics": 8,           # 1 ~ 16 阶谐波数
        "hum_gain": -24.0,            # -60.0 ~ 0.0 dB (谐波陷波衰减深度)
        "hum_q": 18.0,                # 4.0 ~ 60.0 (陷波器品质因数 / 窄度)
        "hum_slope": "4-Pole",        # "2-Pole" | "4-Pole"
        "hum_adaptive": True,         # 自适应工频漂移追踪模式 (Adaptive Mode)
    }


def default_repair_vinyl_preset() -> dict:
    st = default_repair_state()
    st["click_algo"] = "Multi-band (Periodic)"
    st["click_sensitivity"] = 5.2
    st["click_widening"] = 2.0
    st["hum_base_freq"] = 50.0
    st["hum_harmonics"] = 10
    st["hum_gain"] = -30.0
    st["hum_adaptive"] = True
    return st


def parse_float_clean(text: str, fallback: float) -> float:
    s = text.strip().replace("Hz", "").replace("hz", "").replace("ms", "").replace("dB", "").replace("db", "").strip()
    try:
        return float(s)
    except ValueError:
        return fallback


# =====================================================================
# 2. 基础控件：现代发光电源开关（对齐 Delay-UI / DeEsser_UI）
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


# =====================================================================
# 3. 控件：内嵌可编辑数字胶囊
# =====================================================================
class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 64, parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._active = True
        self.setFixedSize(width, 21)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(1, 1, width - 2, 19)
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
                padding: 0px 2px;
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
        if self._active and event.button() == Qt.MouseButton.LeftButton:
            clean = self._text.replace("Hz", "").replace("ms", "").replace("dB", "").replace("+", "").strip()
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
        p.drawRoundedRect(rect, 4.0, 4.0)

        p.setPen(QColor("#ECECF0") if self._active else QColor("#6E6E76"))
        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


# =====================================================================
# 4. 控件：拉丝阳极铝质感旋钮
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
        mode: str = "linear",         # "linear" | "log" | "bipolar"
        step_int: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._mode = mode
        self._step_int = step_int
        self._active = True
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(44, 44)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setValue(self, val: float, emit: bool = False):
        clamped = max(self._min, min(self._max, val))
        if self._step_int:
            clamped = float(round(clamped))
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
        if not self._active:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_y = event.position().y()
        elif event.button() == Qt.MouseButton.RightButton:
            self.setValue(self._default, emit=True)

    def mouseMoveEvent(self, event):
        if self._dragging and self._active:
            dy = self._last_y - event.position().y()
            self._last_y = event.position().y()
            sens = 0.0015 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.0065
            norm = self._val_to_norm(self._value) + dy * sens
            self.setValue(self._norm_to_val(norm), emit=True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def mouseDoubleClickEvent(self, event):
        if self._active and event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._default, emit=True)

    def wheelEvent(self, event):
        if not self._active:
            return
        delta = event.angleDelta().y() / 120.0
        if self._step_int:
            self.setValue(self._value + (1 if delta > 0 else -1), emit=True)
        else:
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

        arc_r = 18.2
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 深色凹槽底环
        p.setPen(QPen(QColor("#141417"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        arc_col = self._accent if self._active else QColor("#55555C")
        p.setPen(QPen(arc_col, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._mode == "bipolar":
            zero_deg = 90.0
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        knob_r = 14.2
        p.setPen(QPen(QColor("#111114"), 1.3))
        cap_col = "#8E9096" if ((self._hovered or self._dragging) and self._active) else ("#7A7C82" if self._active else "#5A5A60")
        p.setBrush(QColor(cap_col))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 4.0
        iy1 = cy - math.sin(rad) * 4.0
        ix2 = cx + math.cos(rad) * (knob_r - 2.0)
        iy2 = cy - math.sin(rad) * (knob_r - 2.0)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 5. 控件：统一旋钮参数单元
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
        step_int: bool = False,
        pill_width: int = 64,
        parent=None,
    ):
        super().__init__(parent)
        self._formatter = formatter
        self._parser = parser
        self._min = min_val
        self._max = max_val
        self._val = default_val
        self._step_int = step_int
        self._updating = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)

        self.title_lbl = QLabel(label)
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet("color: #A4A4AE; font-size: 10px; font-weight: 600;")
        lay.addWidget(self.title_lbl)

        self.knob = ProRotaryKnob(
            min_val, max_val, default_val, default_val,
            accent_color, mode=mode, step_int=step_int
        )
        self.knob.valueChanged.connect(self._on_knob_changed)
        lay.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(4, 0, 4, 0)
        hints_row.setSpacing(2)
        for h_text, align in (
            (min_hint, Qt.AlignmentFlag.AlignLeft),
            (mid_hint, Qt.AlignmentFlag.AlignCenter),
            (max_hint, Qt.AlignmentFlag.AlignRight),
        ):
            hl = QLabel(h_text)
            hl.setAlignment(align)
            hl.setStyleSheet("color: #787882; font-size: 8px;")
            hints_row.addWidget(hl, 1)
        lay.addLayout(hints_row)

        self.pill = EditableValuePill(formatter(default_val), accent_color, width=pill_width)
        self.pill.valueEdited.connect(self._on_pill_edited)
        lay.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignCenter)

    def setValue(self, val: float):
        self._updating = True
        clamped = max(self._min, min(self._max, val))
        if self._step_int:
            clamped = float(round(clamped))
        self._val = clamped
        self.knob.setValue(self._val)
        self.pill.setText(self._formatter(self._val))
        self._updating = False

    def setModuleActive(self, active: bool):
        self.title_lbl.setStyleSheet(f"color: {'#A4A4AE' if active else '#6E6E76'}; font-size: 10px; font-weight: 600;")
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
        if self._step_int:
            clamped = float(round(clamped))
        self.setValue(clamped)
        self.valueChanged.emit(clamped)


# =====================================================================
# 6. 控件：双模复合诊断视图（左/上：De-Click 瞬态检测示波器；右/下：De-Hum 谐波梳状频谱仪）
# =====================================================================
class RepairDiagnosticCanvas(QWidget):
    # 直接在图表上的交互信号
    clickSensitivityDragged = pyqtSignal(float)      # 0.0 ~ 10.0
    clickWideningScrolled = pyqtSignal(float)        # delta ms
    humNodeDragged = pyqtSignal(float, float)        # base_freq_hz, gain_db
    humHarmonicsScrolled = pyqtSignal(int)           # delta count
    viewFocusChanged = pyqtSignal(str)               # "split" | "click" | "hum"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(255)
        self.setMouseTracking(True)

        self._state = default_repair_state()
        self._dragging_target = ""   # "click_thresh" | "hum_node" | ""
        self._hover_target = ""
        self._mouse_pos: QPointF | None = None

        # 视图切换按钮热区
        self._btn_split_rect = QRectF()
        self._btn_click_rect = QRectF()
        self._btn_hum_rect = QRectF()

        # 实时示波器波形与检测爆点缓存 (供后端 push_click_waveform 更新)
        self._wave_samples = 220
        self._raw_wave = [0.0] * self._wave_samples
        self._clean_wave = [0.0] * self._wave_samples
        # detected_clicks: list of (norm_pos 0~1, severity 0~1)
        self._detected_clicks: list[tuple[float, float]] = []

        # 实时低频工频频谱缓存 (供后端 push_hum_spectrum 更新)
        self._spec_bins = 220
        self._spec_freqs = [
            FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** (i / (self._spec_bins - 1)))
            for i in range(self._spec_bins)
        ]
        self._spec_in = [-65.0] * self._spec_bins
        self._spec_out = [-65.0] * self._spec_bins

    def setState(self, state: dict):
        self._state = copy.deepcopy(state)
        self.update()

    def pushClickTelemetry(self, raw_wave: list[float], clean_wave: list[float], clicks: list[tuple[float, float]]):
        self._raw_wave = raw_wave
        self._clean_wave = clean_wave
        self._detected_clicks = clicks
        self.update()

    def pushHumSpectrum(self, freqs_hz: list[float], input_db: list[float], output_db: list[float]):
        self._spec_freqs = freqs_hz
        self._spec_in = input_db
        self._spec_out = output_db
        self.update()

    def _sub_rects(self) -> tuple[QRectF | None, QRectF | None]:
        focus = self._state.get("view_focus", "split")
        w, h = float(self.width()), float(self.height())
        if focus == "click":
            return QRectF(0, 0, w, h), None
        if focus == "hum":
            return None, QRectF(0, 0, w, h)
        # Split 左右双屏：左 46% De-Click 时域瞬态检测，右 54% De-Hum 谐波频谱
        split_x = math.floor(w * 0.46)
        return QRectF(0, 0, split_x, h), QRectF(split_x + 1, 0, w - split_x - 1, h)

    def _click_plot_rect(self, area: QRectF) -> QRectF:
        return QRectF(area.left() + 34, area.top() + 30, max(40.0, area.width() - 46), max(40.0, area.height() - 48))

    def _hum_plot_rect(self, area: QRectF) -> QRectF:
        return QRectF(area.left() + 34, area.top() + 30, max(40.0, area.width() - 48), max(40.0, area.height() - 48))

    def _freq_to_x(self, freq: float, plot: QRectF) -> float:
        f = max(FREQ_MIN, min(FREQ_MAX, freq))
        norm = math.log10(f / FREQ_MIN) / math.log10(FREQ_MAX / FREQ_MIN)
        return plot.left() + norm * plot.width()

    def _x_to_freq(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)

    def _db_to_y(self, db: float, plot: QRectF) -> float:
        # 0 dB (顶) -> -80 dB (底)
        norm = (0.0 - db) / 80.0
        return plot.top() + max(0.0, min(1.0, norm)) * plot.height()

    def _y_to_db(self, y: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (y - plot.top()) / plot.height()))
        return 0.0 - norm * 80.0

    def _sens_to_y(self, sens: float, plot: QRectF) -> float:
        # Sensitivity 0.0 (高阈值/少检测，靠近顶部) -> 10.0 (极高灵敏度/低阈值，靠近中心线)
        mid_y = plot.center().y()
        half_h = plot.height() * 0.44
        ratio = 1.0 - (max(0.0, min(10.0, sens)) / 10.0) * 0.85
        return mid_y - ratio * half_h

    def _y_to_sens(self, y: float, plot: QRectF) -> float:
        mid_y = plot.center().y()
        half_h = max(10.0, plot.height() * 0.44)
        ratio = max(0.15, min(1.0, abs(mid_y - y) / half_h))
        sens = ((1.0 - ratio) / 0.85) * 10.0
        return max(0.0, min(10.0, round(sens, 1)))

    def _hum_node_pos(self, plot: QRectF) -> QPointF:
        x = self._freq_to_x(self._state["hum_base_freq"], plot)
        # 节点纵坐标对应衰减深度 (-60 ~ 0 dB 映射到绘图区)
        y = self._db_to_y(self._state["hum_gain"], plot)
        return QPointF(x, y)

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_target = ""
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_split_rect.contains(pos):
                self.viewFocusChanged.emit("split")
                return
            if self._btn_click_rect.contains(pos):
                self.viewFocusChanged.emit("click")
                return
            if self._btn_hum_rect.contains(pos):
                self.viewFocusChanged.emit("hum")
                return

            c_area, h_area = self._sub_rects()
            if c_area is not None and self._state["enabled"] and self._state["click_enabled"]:
                c_plot = self._click_plot_rect(c_area)
                ty = self._sens_to_y(self._state["click_sensitivity"], c_plot)
                ty_bot = c_plot.center().y() + (c_plot.center().y() - ty)
                if c_plot.contains(pos) and (abs(pos.y() - ty) <= 11.0 or abs(pos.y() - ty_bot) <= 11.0):
                    self._dragging_target = "click_thresh"
                    self.setCursor(Qt.CursorShape.SizeVerCursor)
                    self.update()
                    return

            if h_area is not None and self._state["enabled"] and self._state["hum_enabled"]:
                h_plot = self._hum_plot_rect(h_area)
                np = self._hum_node_pos(h_plot)
                if math.hypot(pos.x() - np.x(), pos.y() - np.y()) <= 14.0:
                    self._dragging_target = "hum_node"
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                    self.update()
                    return

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        c_area, h_area = self._sub_rects()

        if self._dragging_target == "click_thresh" and c_area is not None:
            c_plot = self._click_plot_rect(c_area)
            new_sens = self._y_to_sens(event.position().y(), c_plot)
            self.clickSensitivityDragged.emit(new_sens)
            return

        if self._dragging_target == "hum_node" and h_area is not None:
            h_plot = self._hum_plot_rect(h_area)
            raw_f = self._x_to_freq(event.position().x(), h_plot)
            new_f = max(45.0, min(65.0, round(raw_f, 2)))
            raw_g = self._y_to_db(event.position().y(), h_plot)
            new_g = max(-60.0, min(0.0, round(raw_g, 1)))
            self.humNodeDragged.emit(new_f, new_g)
            return

        # 悬停命中检测
        hit = ""
        if c_area is not None and self._state["enabled"] and self._state["click_enabled"]:
            c_plot = self._click_plot_rect(c_area)
            ty = self._sens_to_y(self._state["click_sensitivity"], c_plot)
            ty_bot = c_plot.center().y() + (c_plot.center().y() - ty)
            if c_plot.contains(event.position()) and (abs(event.position().y() - ty) <= 10.0 or abs(event.position().y() - ty_bot) <= 10.0):
                hit = "click_thresh"

        if not hit and h_area is not None and self._state["enabled"] and self._state["hum_enabled"]:
            h_plot = self._hum_plot_rect(h_area)
            np = self._hum_node_pos(h_plot)
            if math.hypot(event.position().x() - np.x(), event.position().y() - np.y()) <= 14.0:
                hit = "hum_node"

        self._hover_target = hit
        if hit == "click_thresh":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif hit == "hum_node":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_target:
            self._dragging_target = ""
            self.update()

    def wheelEvent(self, event):
        c_area, h_area = self._sub_rects()
        pos = event.position()
        delta = event.angleDelta().y() / 120.0
        if c_area is not None and c_area.contains(pos):
            self.clickWideningScrolled.emit(0.2 if delta > 0 else -0.2)
            event.accept()
        elif h_area is not None and h_area.contains(pos):
            self.humHarmonicsScrolled.emit(1 if delta > 0 else -1)
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        p.fillRect(self.rect(), QColor("#222226"))
        c_area, h_area = self._sub_rects()

        if c_area is not None:
            self._paint_declick_pane(p, c_area)

        if h_area is not None:
            if c_area is not None:
                # 中间 1px 黑色垂直切分线
                p.setPen(QPen(QColor("#141416"), 1.0))
                p.drawLine(QPointF(c_area.right() + 0.5, 0.0), QPointF(c_area.right() + 0.5, float(self.height())))
            self._paint_dehum_pane(p, h_area)

        # 右上角视图聚焦胶囊切换器 [SPLIT | CLICK | HUM]
        focus = self._state.get("view_focus", "split")
        rx = float(self.width()) - 144.0
        self._btn_split_rect = QRectF(rx, 6.0, 42.0, 18.0)
        self._btn_click_rect = QRectF(rx + 46.0, 6.0, 44.0, 18.0)
        self._btn_hum_rect = QRectF(rx + 94.0, 6.0, 38.0, 18.0)

        for rect, label, key, col_hex in (
            (self._btn_split_rect, "SPLIT", "split", COLORS["cyan"]),
            (self._btn_click_rect, "CLICK", "click", COLORS["click"]),
            (self._btn_hum_rect,   "HUM",   "hum",   COLORS["hum"]),
        ):
            active = (focus == key)
            col = QColor(col_hex)
            p.setPen(QPen(col if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(col.red(), col.green(), col.blue(), 42) if active else QColor(21, 21, 24, 210))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(col if active else QColor("#787882"))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    # -----------------------------------------------------------------
    # 子画布 A：De-Click 瞬态爆点检测示波器
    # -----------------------------------------------------------------
    def _paint_declick_pane(self, p: QPainter, area: QRectF):
        en = bool(self._state["enabled"] and self._state["click_enabled"])
        plot = self._click_plot_rect(area)

        # 顶部标签
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor(COLORS["click"]) if en else QColor("#6E6E76"))
        p.drawText(QRectF(area.left() + 10, 5, 190, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"DE-CLICK TRANSIENTS  •  {len(self._detected_clicks)} HITS")

        # 绘图区底色
        p.fillRect(plot, QColor("#1D1D20"))

        # 网格线
        grid_pen = QPen(QColor("#26262A"), 1.0)
        mid_y = plot.center().y()
        for frac in (0.15, 0.32, 0.68, 0.85):
            gy = plot.top() + frac * plot.height()
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), gy), QPointF(plot.right(), gy))
        for i in range(1, 6):
            gx = plot.left() + (i / 6.0) * plot.width()
            p.setPen(grid_pen)
            p.drawLine(QPointF(gx, plot.top()), QPointF(gx, plot.bottom()))

        # 中心零电平轴
        p.setPen(QPen(QColor("#3A3A40"), 1.1))
        p.drawLine(QPointF(plot.left(), mid_y), QPointF(plot.right(), mid_y))

        # 左侧 dB 刻度
        p.setFont(QFont(MONO_FONT_FAM, 7))
        p.setPen(QColor("#8E8E98"))
        for lbl, y_pos in (("0dB", plot.top() + 6), ("-12", plot.top() + plot.height() * 0.25),
                           ("-∞", mid_y), ("-12", plot.bottom() - plot.height() * 0.25), ("0dB", plot.bottom() - 6)):
            p.drawText(QRectF(area.left() + 2, y_pos - 7, 28, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        half_h = plot.height() * 0.44
        n = len(self._raw_wave)

        # 1. 高亮检测到的爆点垂直脉冲柱 (Click Markers + Widening 宽度带)
        if en and self._detected_clicks:
            widen_px = max(2.5, (self._state["click_widening"] / 5.0) * 14.0)
            for norm_x, sev in self._detected_clicks:
                cx = plot.left() + norm_x * plot.width()
                band = QRectF(cx - widen_px / 2.0, plot.top(), widen_px, plot.height())
                p.fillRect(band, QColor(242, 109, 109, int(28 + sev * 32)))
                p.setPen(QPen(QColor(242, 109, 109, 185), 1.2))
                p.drawLine(QPointF(cx, plot.top() + 3), QPointF(cx, plot.bottom() - 3))

        # 2. 原始带爆点信号包络 (珊瑚红半透明残影)
        if n > 1 and en:
            raw_path = QPainterPath()
            for i, v in enumerate(self._raw_wave):
                x = plot.left() + (i / (n - 1)) * plot.width()
                y = mid_y - v * half_h
                if i == 0:
                    raw_path.moveTo(x, y)
                else:
                    raw_path.lineTo(x, y)
            p.setPen(QPen(QColor(242, 109, 109, 155), 1.15))
            p.drawPath(raw_path)

        # 3. 修复后的干净时域波形 (冰晶青主曲线)
        if n > 1:
            clean_path = QPainterPath()
            src = self._clean_wave if en else self._raw_wave
            for i, v in enumerate(src):
                x = plot.left() + (i / (n - 1)) * plot.width()
                y = mid_y - v * half_h
                if i == 0:
                    clean_path.moveTo(x, y)
                else:
                    clean_path.lineTo(x, y)
            p.setPen(QPen(QColor(COLORS["cyan"] if en else "#686C78"), 1.45))
            p.drawPath(clean_path)

        # 4. 可拖拽的 Sensitivity 双向触发阈值线
        ty_top = self._sens_to_y(self._state["click_sensitivity"], plot)
        ty_bot = mid_y + (mid_y - ty_top)
        is_hi = (self._hover_target == "click_thresh" or self._dragging_target == "click_thresh")
        t_col = QColor(COLORS["click"]) if en else QColor("#585C68")
        t_col.setAlpha(235 if is_hi else 160)

        p.setPen(QPen(t_col, 1.4 if is_hi else 1.1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(plot.left(), ty_top), QPointF(plot.right(), ty_top))
        p.drawLine(QPointF(plot.left(), ty_bot), QPointF(plot.right(), ty_bot))

        # 左侧阈值手柄标签
        handle_rect = QRectF(plot.left() + 6, ty_top - 8, 62, 16)
        p.setPen(QPen(t_col, 1.0))
        p.setBrush(QColor(21, 21, 24, 225))
        p.drawRoundedRect(handle_rect, 3.0, 3.0)
        p.setPen(t_col)
        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        p.drawText(handle_rect, Qt.AlignmentFlag.AlignCenter, f"SENS {self._state['click_sensitivity']:.1f}")

        p.restore()

        # 底部时间轴标识
        p.setFont(QFont(MONO_FONT_FAM, 7))
        p.setPen(QColor("#8E8E98"))
        for idx_t, txt in enumerate(("-50ms", "-40", "-30", "-20", "-10", "NOW")):
            tx = plot.left() + (idx_t / 5.0) * plot.width()
            p.drawText(QRectF(tx - 18, plot.bottom() + 3, 36, 14), Qt.AlignmentFlag.AlignCenter, txt)

    # -----------------------------------------------------------------
    # 子画布 B：De-Hum 50/60Hz 工频及多阶谐波梳状陷波频谱仪
    # -----------------------------------------------------------------
    def _paint_dehum_pane(self, p: QPainter, area: QRectF):
        en = bool(self._state["enabled"] and self._state["hum_enabled"])
        plot = self._hum_plot_rect(area)

        f0 = float(self._state["hum_base_freq"])
        n_harm = int(self._state["hum_harmonics"])
        adapt_tag = "ADAPTIVE ON" if self._state["hum_adaptive"] else "STATIC LOCK"

        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor(COLORS["hum"]) if en else QColor("#6E6E76"))
        p.drawText(
            QRectF(area.left() + 10, 5, area.width() - 160, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"DE-HUM COMB  •  {f0:.1f}Hz × {n_harm}H  •  {adapt_tag}",
        )

        # 绘图区底色
        p.fillRect(plot, QColor("#1D1D20"))

        # 水平 dB 刻度网格
        grid_pen = QPen(QColor("#26262A"), 1.0)
        p.setFont(QFont(MONO_FONT_FAM, 7))
        for db_val in (0, -20, -40, -60, -80):
            y = self._db_to_y(float(db_val), plot)
            p.setPen(QPen(QColor("#34343B"), 1.0) if db_val == -40 else grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor("#8E8E98"))
            p.drawText(QRectF(area.left() + 2, y - 7, 28, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(db_val))

        # 对数频率刻度线 (20Hz ~ 4kHz)
        freq_ticks = [
            (25.0, "25"), (50.0, "50"), (60.0, "60"), (100.0, "100"),
            (200.0, "200"), (500.0, "500"), (1000.0, "1K"), (2000.0, "2K"), (4000.0, "4K"),
        ]
        for f_val, f_lbl in freq_ticks:
            x = self._freq_to_x(f_val, plot)
            is_mains = f_val in (50.0, 60.0)
            p.setPen(QPen(QColor("#38342A"), 1.0) if is_mains else grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#C8A868") if is_mains else QColor("#8E8E98"))
            p.drawText(QRectF(x - 16, plot.bottom() + 3, 32, 14), Qt.AlignmentFlag.AlignCenter, f_lbl)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        # 1. 输入（含工频毛刺）与输出（净化后）实时频谱对比
        if len(self._spec_freqs) > 1:
            in_path = QPainterPath()
            out_path = QPainterPath()
            out_poly = QPolygonF()
            out_poly.append(QPointF(self._freq_to_x(self._spec_freqs[0], plot), plot.bottom()))

            for idx, f in enumerate(self._spec_freqs):
                sx = self._freq_to_x(f, plot)
                iy = self._db_to_y(self._spec_in[idx], plot)
                oy = self._db_to_y(self._spec_out[idx] if en else self._spec_in[idx], plot)
                if idx == 0:
                    in_path.moveTo(sx, iy)
                    out_path.moveTo(sx, oy)
                else:
                    in_path.lineTo(sx, iy)
                    out_path.lineTo(sx, oy)
                out_poly.append(QPointF(sx, oy))

            out_poly.append(QPointF(self._freq_to_x(self._spec_freqs[-1], plot), plot.bottom()))

            grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            grad.setColorAt(0.0, QColor(40, 216, 240, 65))
            grad.setColorAt(1.0, QColor(40, 216, 240, 12))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawPolygon(out_poly)

            # 原始哼声尖峰轮廓（琥珀虚线）
            if en:
                p.setPen(QPen(QColor(240, 168, 72, 120), 1.0, Qt.PenStyle.DotLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(in_path)

            p.setPen(QPen(QColor(82, 184, 235, 200), 1.2))
            p.drawPath(out_path)

        # 2. 多阶谐波垂直指示线与梳状陷波响应曲线
        gain_db = float(self._state["hum_gain"])
        q_val = max(2.0, float(self._state["hum_q"]))
        order = 2 if self._state["hum_slope"] == "4-Pole" else 1
        hum_col = QColor(COLORS["hum"]) if en else QColor("#565A66")

        # 谐波垂直标注线 (H1 ~ Hn)
        for h in range(1, n_harm + 1):
            hf = f0 * h
            if hf > FREQ_MAX:
                break
            hx = self._freq_to_x(hf, plot)
            alpha = 110 if h == 1 else max(28, 75 - h * 4)
            p.setPen(QPen(QColor(hum_col.red(), hum_col.green(), hum_col.blue(), alpha), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(hx, plot.top()), QPointF(hx, plot.bottom()))

        # 绘制顶部 0dB 基准线向下凹陷的梳状滤波曲线
        comb_path = QPainterPath()
        comb_fill = QPainterPath()
        steps = 360
        for i in range(steps + 1):
            norm = i / steps
            f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
            x = plot.left() + norm * plot.width()

            # 叠加所有激活谐波的陷波衰减
            total_cut_db = 0.0
            for h in range(1, n_harm + 1):
                hf = f0 * h
                if hf > FREQ_MAX * 1.1:
                    break
                # 高次谐波自然轻微滚降权重
                harm_weight = 1.0 - (h - 1) * 0.035
                bw = hf / q_val
                dist = abs(f - hf) / max(0.5, bw)
                notch = 1.0 / (1.0 + (dist ** (2 * order)))
                total_cut_db += gain_db * max(0.25, harm_weight) * notch

            total_cut_db = max(-65.0, min(0.0, total_cut_db))
            y = self._db_to_y(total_cut_db, plot)
            if i == 0:
                comb_path.moveTo(x, y)
                comb_fill.moveTo(x, plot.top())
                comb_fill.lineTo(x, y)
            else:
                comb_path.lineTo(x, y)
                comb_fill.lineTo(x, y)

        comb_fill.lineTo(plot.right(), plot.top())
        comb_fill.closeSubpath()

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(hum_col.red(), hum_col.green(), hum_col.blue(), 34 if en else 12))
        p.drawPath(comb_fill)

        p.setPen(QPen(hum_col, 1.7))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(comb_path)

        # 3. 基频 H1 可拖拽节点 (Base Freq × Notch Gain)
        np = self._hum_node_pos(plot)
        is_hi = (self._hover_target == "hum_node" or self._dragging_target == "hum_node")
        rad = 10.0 if is_hi else 8.4

        if is_hi and en:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(hum_col.red(), hum_col.green(), hum_col.blue(), 68))
            p.drawEllipse(np, rad + 4.5, rad + 4.5)

        p.setPen(QPen(hum_col, 1.8))
        p.setBrush(QColor("#151518"))
        p.drawEllipse(np, rad, rad)

        p.setPen(QColor("#ECECF0") if en else QColor("#8A8A92"))
        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        p.drawText(QRectF(np.x() - rad, np.y() - rad, rad * 2, rad * 2), Qt.AlignmentFlag.AlignCenter, "H1")

        p.restore()


# =====================================================================
# 7. 控件：右侧双联修复成效监测表 (CLICK 瞬态修复活动度 / HUM 抑制量 / OUT)
# =====================================================================
class RepairMeterStrip(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._click_activity = 0.0   # 0.0 ~ 1.0 (瞬态修补触发强度)
        self._hum_reduction_db = 0.0 # 0.0 ~ 24.0 dB (工频哼声抑制量)
        self._out_db = -18.0
        self._clipped = False
        self.setFixedWidth(82)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击重置削波指示灯")

    def setTelemetry(self, click_act: float, hum_red_db: float, out_db: float):
        self._click_activity = max(0.0, min(1.0, click_act))
        self._hum_reduction_db = max(0.0, min(24.0, hum_red_db))
        self._out_db = max(-60.0, min(0.0, out_db))
        if out_db >= -0.1:
            self._clipped = True
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
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
            ("CLK", 14.0),
            ("HUM", 37.0),
            ("OUT", 60.0),
        ]

        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        for name, x in cols:
            p.setPen(QColor("#8A8A94"))
            p.drawText(QRectF(x - 7, 4, bar_w + 14, 14), Qt.AlignmentFlag.AlignCenter, name)

            bg_rect = QRectF(x, top_y, bar_w, bar_h)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor("#0E0E11"))
            p.drawRoundedRect(bg_rect, 2.0, 2.0)

        # 1. CLK 瞬态脉冲剔除指示条 (自顶向下，珊瑚红)
        if self._click_activity > 0.01:
            h = bar_h * self._click_activity
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(COLORS["click"]))
            p.drawRoundedRect(QRectF(14.5, top_y, bar_w - 1.0, h), 1.5, 1.5)

        # 2. HUM 谐波抑制深度指示条 (自顶向下，琥珀金)
        hum_norm = self._hum_reduction_db / 24.0
        if hum_norm > 0.01:
            h = bar_h * hum_norm
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(COLORS["hum"]))
            p.drawRoundedRect(QRectF(37.5, top_y, bar_w - 1.0, h), 1.5, 1.5)

        # 3. OUT 输出峰值电平表 (自底向上，翡翠绿->黄->红)
        out_norm = (self._out_db + 60.0) / 60.0
        if out_norm > 0.01:
            h = bar_h * out_norm
            r = QRectF(60.5, bot_y - h, bar_w - 1.0, h)
            grad = QLinearGradient(QPointF(0, bot_y), QPointF(0, top_y))
            grad.setColorAt(0.0, QColor("#28C882"))
            grad.setColorAt(0.75, QColor("#E8B838"))
            grad.setColorAt(1.0, QColor("#F05050"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(r, 1.5, 1.5)

        # 削波灯
        clip_rect = QRectF(60.0, top_y - 6.0, bar_w, 3.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)

        # 底部输出 dBFS 实时数值
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor("#9A9AA2"))
        p.drawText(QRectF(0, self.height() - 22, self.width(), 18), Qt.AlignmentFlag.AlignCenter, f"{self._out_db:.1f}dB")


# =====================================================================
# 8. 控件：底部处理模块卡片（带独立模块电源开关）
# =====================================================================
class RepairModuleCard(QFrame):
    moduleToggled = pyqtSignal(bool)

    def __init__(
        self,
        title: str,
        badge_text: str,
        accent_color: str,
        has_power: bool = True,
        is_last: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._accent = accent_color
        self._active = True

        self.setObjectName("RepairCard")
        right_border = "none" if is_last else "1px solid #141416"
        self.setStyleSheet(f"""
            QFrame#RepairCard {{
                background-color: #27272B;
                border: none;
                border-top: 1px solid #141416;
                border-right: {right_border};
                border-radius: 0px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
            QPushButton#SegBtn {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
                padding: 0px 6px;
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

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(32)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 5, 10, 4)
        h_lay.setSpacing(6)

        self.power_btn: PowerToggleSwitch | None = None
        if has_power:
            self.power_btn = PowerToggleSwitch(True, accent_color)
            self.power_btn.toggled.connect(self.moduleToggled.emit)
            h_lay.addWidget(self.power_btn)

        self.title_lbl = QLabel(title)
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        self.badge_lbl = QLabel(badge_text)
        self.badge_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.6px;")
        h_lay.addWidget(self.badge_lbl)
        root.addWidget(header)

        # Body
        self.body = QFrame()
        self.body.setObjectName("CardBody")
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(10, 8, 10, 10)
        self.body_lay.setSpacing(8)
        root.addWidget(self.body, 1)

        self.setModuleActive(True)

    def setModuleActive(self, active: bool):
        self._active = active
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if active else '#6E6E76'}; font-size: 11px; font-weight: 700; letter-spacing: 0.4px;"
        )


# =====================================================================
# 9. 主面板：自主旗舰音频修复套件 (Studio De-Click / De-Hum)
# =====================================================================
class StudioDeClickDeHumWidget(QWidget):
    # 对接后端 DSP 引擎的完整信号接口
    bypassChanged = pyqtSignal(bool)
    outputDiffChanged = pyqtSignal(bool)
    parameterChanged = pyqtSignal(str, object)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Dialogue 1", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"De-Click / De-Hum Repair — {track_name}")
        self.resize(980, 620)
        self.setMinimumSize(900, 560)

        self._active_bank = "A"
        self._banks = {
            "A": default_repair_state(),
            "B": default_repair_vinyl_preset(),
        }

        self._presets = {
            # --- 1. 通用与人声口播场景 (Dialogue & Voiceover) ---
            "Dialogue Clean (Default)": default_repair_state(),
            "Podcast / Dry Mouth Clicks (口腔粘连/唾液音)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_algo": "Multi-band (Random)",
                "click_sensitivity": 4.8,
                "click_widening": 1.6,
                "click_skew": 0.35,      # 偏向高频唾液细碎声
                "hum_enabled": False,
            },
            "Close-Mic Plosive & Lip Smack (近讲唇齿爆音)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_algo": "Multi-band (Random)",
                "click_sensitivity": 5.5,
                "click_widening": 2.2,
                "click_skew": -0.25,     # 兼顾低频唇部闭合瞬态
                "hum_enabled": False,
            },
            "Lav Mic Clothing Rustle & Static (领夹麦静电/摩擦)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_algo": "Multi-band (Random)",
                "click_sensitivity": 4.0,
                "click_widening": 1.8,
                "click_skew": 0.45,
                "hum_enabled": True,
                "hum_base_freq": 50.0,
                "hum_harmonics": 4,
                "hum_gain": -18.0,
                "hum_q": 14.0,
                "hum_adaptive": True,
            },
            "Low-Latency Live Stream / Broadcast (直播低延迟)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_algo": "Low Latency",
                "click_sensitivity": 3.8,
                "click_widening": 0.8,
                "click_skew": 0.1,
                "hum_enabled": True,
                "hum_base_freq": 50.0,
                "hum_harmonics": 6,
                "hum_gain": -22.0,
                "hum_q": 20.0,
                "hum_adaptive": False,
            },

            # --- 2. 电源工频干扰场景 (50Hz / 60Hz Mains & Ground Loop) ---
            "50Hz Mains Hum + Mouth Clicks (国标/欧标全套)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_algo": "Multi-band (Random)",
                "click_sensitivity": 4.2,
                "click_widening": 1.4,
                "hum_enabled": True,
                "hum_base_freq": 50.0,
                "hum_harmonics": 10,
                "hum_gain": -28.0,
                "hum_q": 20.0,
                "hum_slope": "4-Pole",
                "hum_adaptive": True,
            },
            "60Hz US Power + Buzz (美标/日标全套)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_sensitivity": 3.2,
                "hum_enabled": True,
                "hum_base_freq": 60.0,
                "hum_harmonics": 12,
                "hum_gain": -32.0,
                "hum_q": 22.0,
                "hum_slope": "4-Pole",
                "hum_adaptive": True,
            },
            "Heavy Ground Loop Buzz (严重接地环路嗡嗡声)": {
                **default_repair_state(),
                "click_enabled": False,
                "hum_enabled": True,
                "hum_base_freq": 50.0,
                "hum_harmonics": 16,     # 满阶谐波切除高频蜂鸣泛音
                "hum_gain": -42.0,
                "hum_q": 24.0,
                "hum_slope": "4-Pole",
                "hum_adaptive": True,
            },
            "Surgical Pure 50Hz Hum (外科手术级仅去基频)": {
                **default_repair_state(),
                "click_enabled": False,
                "hum_enabled": True,
                "hum_base_freq": 50.0,
                "hum_harmonics": 3,
                "hum_gain": -36.0,
                "hum_q": 48.0,           # 超高 Q 值几乎不伤及男声低频基音
                "hum_slope": "4-Pole",
                "hum_adaptive": True,
            },
            "Unstable Generator / Inverter Hum (变频/发电机漂移)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_sensitivity": 3.0,
                "hum_enabled": True,
                "hum_base_freq": 50.0,
                "hum_harmonics": 8,
                "hum_gain": -26.0,
                "hum_q": 10.0,           # 较宽带宽包容频率剧烈波动
                "hum_slope": "2-Pole",
                "hum_adaptive": True,
            },

            # --- 3. 乐器与老旧介质修复 (Instruments & Archive Media) ---
            "Electric Guitar Single-Coil Buzz (单线圈电吉他电流声)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_algo": "Single-band",
                "click_sensitivity": 2.2,
                "click_widening": 0.6,
                "hum_enabled": True,
                "hum_base_freq": 50.0,
                "hum_harmonics": 14,
                "hum_gain": -30.0,
                "hum_q": 26.0,
                "hum_slope": "4-Pole",
                "hum_adaptive": False,   # 静态锁定避免误吞琴弦持续泛音
            },
            "Old Vinyl / Archive Crackle (老黑胶/档案炒豆声)": default_repair_vinyl_preset(),
            "78 RPM Shellac Heavy Pop (老唱片重度爆豆/划痕)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_algo": "Multi-band (Periodic)",
                "click_sensitivity": 7.2,
                "click_widening": 3.2,
                "click_skew": -0.15,
                "hum_enabled": True,
                "hum_base_freq": 50.0,
                "hum_harmonics": 6,
                "hum_gain": -24.0,
                "hum_q": 16.0,
                "hum_adaptive": True,
            },
            "Digital Clipping / Buffer Glitch (数字爆音/采样丢帧)": {
                **default_repair_state(),
                "click_enabled": True,
                "click_algo": "Single-band",
                "click_sensitivity": 6.0,
                "click_widening": 1.0,
                "click_skew": 0.5,       # 锁定极陡峭的数字方波边沿
                "hum_enabled": False,
            },
        }

        self._cells: dict[str, KnobParameterCell] = {}
        self._updating = False
        self._setup_ui(track_name)
        self._refresh_all_views()

        # 独立运行时的拟真口播咔哒 + 50/60Hz工频谐波演示定时器（接入真实后端后自动停止）
        self._anim_t = 0.0
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
            RepairDiagnosticCanvas, RepairMeterStrip {{
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
        # 1. 顶部整合工具栏 (总旁通 + 预设 + A/B 对比 + DIFF 残差监听 + Reset)
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

        title_lbl = QLabel(f"DE-CLICK / DE-HUM  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(10)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存为新修复预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedWidth(215)
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

        tb_lay.addStretch()

        # 输出残差独奏监听 (Output Clicks/Hum Only)
        self.btn_diff = QPushButton("🎧 Output Diff (仅听剔除杂音)")
        self.btn_diff.setCheckable(True)
        self.btn_diff.setFixedHeight(22)
        self.btn_diff.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_diff.setToolTip("仅监听被 De-Click / De-Hum 剥离出的咔哒声与工频哼声残差，确保不误伤正常语音")
        self.btn_diff.clicked.connect(self._on_diff_toggled)
        tb_lay.addWidget(self.btn_diff)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_repair)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心图形监控区：双模诊断画布 + 右侧三联修复监控表
        # -------------------------------------------------------------
        mid_row = QHBoxLayout()
        mid_row.setContentsMargins(0, 0, 0, 0)
        mid_row.setSpacing(0)

        self.canvas = RepairDiagnosticCanvas()
        self.canvas.clickSensitivityDragged.connect(self._on_canvas_sens_dragged)
        self.canvas.clickWideningScrolled.connect(self._on_canvas_widen_scrolled)
        self.canvas.humNodeDragged.connect(self._on_canvas_hum_dragged)
        self.canvas.humHarmonicsScrolled.connect(self._on_canvas_harm_scrolled)
        self.canvas.viewFocusChanged.connect(self._on_view_focus_changed)
        mid_row.addWidget(self.canvas, 1)

        meter_frame = QFrame()
        meter_frame.setObjectName("MeterFrame")
        meter_frame.setStyleSheet("QFrame#MeterFrame { background-color: #222226; border-left: 1px solid #141416; }")
        m_lay = QVBoxLayout(meter_frame)
        m_lay.setContentsMargins(4, 6, 4, 6)
        self.meter_strip = RepairMeterStrip()
        m_lay.addWidget(self.meter_strip, 1)
        mid_row.addWidget(meter_frame)

        main_lay.addLayout(mid_row, 5)

        # -------------------------------------------------------------
        # 3. 底部三大处理模块矩阵 (De-Click | De-Hum | Master Output)
        # -------------------------------------------------------------
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(0)

        # === 模块 1：DE-CLICK 去咔哒 / 爆点 ===
        self.card_click = RepairModuleCard("DE-CLICK  去咔哒/爆点", "TRANSIENT REPAIR", COLORS["click"], has_power=True, is_last=False)
        self.card_click.moduleToggled.connect(self._on_click_power_toggled)

        algo_row = QHBoxLayout()
        algo_lbl = QLabel("Algorithm")
        algo_lbl.setStyleSheet("color: #A4A4AE; font-size: 10px; font-weight: 600;")
        self.algo_combo = QComboBox()
        self.algo_combo.addItems([
            "Single-band",
            "Multi-band (Periodic)",
            "Multi-band (Random)",
            "Low Latency",
        ])
        self.algo_combo.currentTextChanged.connect(self._on_algo_changed)
        algo_row.addWidget(algo_lbl)
        algo_row.addSpacing(6)
        algo_row.addWidget(self.algo_combo, 1)
        self.card_click.body_lay.addLayout(algo_row)

        click_knobs_row = QHBoxLayout()
        click_knobs_row.setSpacing(6)

        c_sens = KnobParameterCell(
            "Sensitivity", 0.0, 10.0, 3.5, COLORS["click"],
            "0.0", "SENS", "10.0",
            lambda v: f"{v:.1f}", parse_float_clean, mode="linear"
        )
        c_wide = KnobParameterCell(
            "Click Widening", 0.0, 5.0, 1.2, COLORS["click"],
            "0.0", "ms", "5.0",
            lambda v: f"{v:.1f}ms", parse_float_clean, mode="linear"
        )
        c_skew = KnobParameterCell(
            "Freq Skew", -1.0, 1.0, 0.0, COLORS["click"],
            "Low", "BAL", "High",
            lambda v: f"{v:+.2f}" if abs(v) >= 0.01 else "0.00", parse_float_clean, mode="bipolar"
        )
        self._cells["click_sensitivity"] = c_sens
        self._cells["click_widening"] = c_wide
        self._cells["click_skew"] = c_skew

        for cell in (c_sens, c_wide, c_skew):
            click_knobs_row.addWidget(cell, 1)
        self.card_click.body_lay.addLayout(click_knobs_row, 1)

        bottom_row.addWidget(self.card_click, 3)

        # === 模块 2：DE-HUM 去工频哼声及谐波 ===
        self.card_hum = RepairModuleCard("DE-HUM  去工频哼声", "50/60Hz HARMONIC NOTCH", COLORS["hum"], has_power=True, is_last=False)
        self.card_hum.moduleToggled.connect(self._on_hum_power_toggled)

        hum_top_row = QHBoxLayout()
        hum_top_row.setSpacing(4)

        self.btn_50hz = QPushButton("50Hz")
        self.btn_50hz.setObjectName("SegBtn")
        self.btn_50hz.setCheckable(True)
        self.btn_50hz.setFixedSize(44, 21)
        self.btn_50hz.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_50hz.clicked.connect(lambda: self._on_base_freq_preset(50.0))

        self.btn_60hz = QPushButton("60Hz")
        self.btn_60hz.setObjectName("SegBtn")
        self.btn_60hz.setCheckable(True)
        self.btn_60hz.setFixedSize(44, 21)
        self.btn_60hz.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_60hz.clicked.connect(lambda: self._on_base_freq_preset(60.0))

        self.btn_slope = QPushButton("4-Pole")
        self.btn_slope.setObjectName("SegBtn")
        self.btn_slope.setCheckable(True)
        self.btn_slope.setFixedSize(50, 21)
        self.btn_slope.setToolTip("切换 2-Pole (12dB/oct) 或 4-Pole (24dB/oct) 陷波陡度")
        self.btn_slope.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_slope.clicked.connect(self._on_slope_toggled)

        hum_top_row.addWidget(self.btn_50hz)
        hum_top_row.addWidget(self.btn_60hz)
        hum_top_row.addWidget(self.btn_slope)
        hum_top_row.addStretch()

        self.btn_adaptive = QPushButton("⚡ Adaptive 自适应")
        self.btn_adaptive.setCheckable(True)
        self.btn_adaptive.setFixedHeight(21)
        self.btn_adaptive.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_adaptive.setToolTip("开启后自动动态追踪电网频率轻微漂移与谐波能量变化，减少音乐音染")
        self.btn_adaptive.clicked.connect(self._on_adaptive_toggled)
        hum_top_row.addWidget(self.btn_adaptive)

        self.card_hum.body_lay.addLayout(hum_top_row)

        hum_knobs_row = QHBoxLayout()
        hum_knobs_row.setSpacing(6)

        c_hfreq = KnobParameterCell(
            "Base Freq", 45.0, 65.0, 50.0, COLORS["hum"],
            "45", "Hz", "65",
            lambda v: f"{v:.2f}Hz", parse_float_clean, mode="linear"
        )
        c_harm = KnobParameterCell(
            "Harmonics", 1.0, 16.0, 8.0, COLORS["hum"],
            "1", "CNT", "16",
            lambda v: f"{int(round(v))}", parse_float_clean, mode="linear", step_int=True
        )
        c_hgain = KnobParameterCell(
            "Notch Gain", -60.0, 0.0, -24.0, COLORS["hum"],
            "-60", "dB", "0",
            lambda v: f"{v:.1f}dB", parse_float_clean, mode="linear"
        )
        c_hq = KnobParameterCell(
            "Filter Q", 4.0, 60.0, 18.0, COLORS["hum"],
            "Wide", "Q", "Surgical",
            lambda v: f"{v:.1f}", parse_float_clean, mode="log"
        )
        self._cells["hum_base_freq"] = c_hfreq
        self._cells["hum_harmonics"] = c_harm
        self._cells["hum_gain"] = c_hgain
        self._cells["hum_q"] = c_hq

        for cell in (c_hfreq, c_harm, c_hgain, c_hq):
            hum_knobs_row.addWidget(cell, 1)
        self.card_hum.body_lay.addLayout(hum_knobs_row, 1)

        bottom_row.addWidget(self.card_hum, 4)

        # === 模块 3：OUTPUT 输出补偿 ===
        self.card_out = RepairModuleCard("OUTPUT", "TRIM", COLORS["cyan"], has_power=False, is_last=True)

        c_out = KnobParameterCell(
            "Output Gain", -12.0, 12.0, 0.0, COLORS["cyan"],
            "-12", "dB", "+12",
            lambda v: f"{v:+.1f}dB" if abs(v) >= 0.05 else "0.0dB", parse_float_clean, mode="bipolar"
        )
        self._cells["output_gain"] = c_out
        self.card_out.body_lay.addStretch()
        self.card_out.body_lay.addWidget(c_out, 0, Qt.AlignmentFlag.AlignCenter)
        self.card_out.body_lay.addStretch()

        bottom_row.addWidget(self.card_out, 1)

        main_lay.addLayout(bottom_row, 4)

        # 绑定所有旋钮信号
        for pid, cell in self._cells.items():
            cell.valueChanged.connect(lambda val, key=pid: self._on_param_changed(key, val))

    # -----------------------------------------------------------------
    # 状态刷新与联动交互
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_top_styles(self):
        st = self._current_state()
        active_css = f"""
            QPushButton {{
                background-color: #182E38;
                color: {COLORS["cyan"]};
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

        self.btn_diff.setChecked(bool(st["output_diff"]))
        self.btn_diff.setStyleSheet(f"""
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
                background-color: #381C1E;
                color: {COLORS["click"]};
                border: 1px solid {COLORS["click"]};
            }}
        """)

        self.btn_adaptive.setChecked(bool(st["hum_adaptive"]))
        self.btn_adaptive.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238;
                color: #A4A4AE;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
                padding: 0px 7px;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #163226;
                color: {COLORS["green"]};
                border: 1px solid {COLORS["green"]};
            }}
        """)

    def _refresh_all_views(self):
        self._updating = True
        st = self._current_state()
        en = bool(st["enabled"])
        c_en = en and bool(st["click_enabled"])
        h_en = en and bool(st["hum_enabled"])

        self._update_top_styles()
        self.power_switch.setChecked(en)

        # De-Click 模块状态
        self.card_click.setModuleActive(c_en)
        if self.card_click.power_btn is not None:
            self.card_click.power_btn.setChecked(bool(st["click_enabled"]))
        self.algo_combo.blockSignals(True)
        self.algo_combo.setCurrentText(st["click_algo"])
        self.algo_combo.setEnabled(c_en)
        self.algo_combo.blockSignals(False)

        for k in ("click_sensitivity", "click_widening", "click_skew"):
            self._cells[k].setValue(st[k])
            self._cells[k].setModuleActive(c_en)

        # De-Hum 模块状态
        self.card_hum.setModuleActive(h_en)
        if self.card_hum.power_btn is not None:
            self.card_hum.power_btn.setChecked(bool(st["hum_enabled"]))
        self.btn_50hz.setChecked(abs(st["hum_base_freq"] - 50.0) < 0.3)
        self.btn_60hz.setChecked(abs(st["hum_base_freq"] - 60.0) < 0.3)
        self.btn_slope.setChecked(st["hum_slope"] == "4-Pole")
        self.btn_slope.setText(st["hum_slope"])

        for k in ("hum_base_freq", "hum_harmonics", "hum_gain", "hum_q"):
            self._cells[k].setValue(st[k])
            self._cells[k].setModuleActive(h_en)

        # Output 模块状态
        self.card_out.setModuleActive(en)
        self._cells["output_gain"].setValue(st["output_gain"])
        self._cells["output_gain"].setModuleActive(en)

        self.canvas.setState(st)
        self._updating = False

    def _emit_state(self):
        if not self._updating:
            self.stateChanged.emit(self.get_state())

    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self._emit_state()

    def _copy_bank(self):
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._banks[self._active_bank])

    def _on_power_toggled(self, enabled: bool):
        self._current_state()["enabled"] = enabled
        self._refresh_all_views()
        self.bypassChanged.emit(enabled)
        self._emit_state()

    def _on_diff_toggled(self, checked: bool):
        self._current_state()["output_diff"] = bool(checked)
        self._refresh_all_views()
        self.outputDiffChanged.emit(bool(checked))
        self._emit_state()

    def _on_click_power_toggled(self, enabled: bool):
        self._current_state()["click_enabled"] = enabled
        self._refresh_all_views()
        self.parameterChanged.emit("click_enabled", enabled)
        self._emit_state()

    def _on_hum_power_toggled(self, enabled: bool):
        self._current_state()["hum_enabled"] = enabled
        self._refresh_all_views()
        self.parameterChanged.emit("hum_enabled", enabled)
        self._emit_state()

    def _on_algo_changed(self, algo: str):
        self._current_state()["click_algo"] = algo
        self._refresh_all_views()
        self.parameterChanged.emit("click_algo", algo)
        self._emit_state()

    def _on_base_freq_preset(self, freq: float):
        self._current_state()["hum_base_freq"] = freq
        self._refresh_all_views()
        self.parameterChanged.emit("hum_base_freq", freq)
        self._emit_state()

    def _on_slope_toggled(self):
        st = self._current_state()
        st["hum_slope"] = "2-Pole" if st["hum_slope"] == "4-Pole" else "4-Pole"
        self._refresh_all_views()
        self.parameterChanged.emit("hum_slope", st["hum_slope"])
        self._emit_state()

    def _on_adaptive_toggled(self, checked: bool):
        self._current_state()["hum_adaptive"] = bool(checked)
        self._refresh_all_views()
        self.parameterChanged.emit("hum_adaptive", bool(checked))
        self._emit_state()

    def _on_view_focus_changed(self, focus: str):
        self._current_state()["view_focus"] = focus
        self._refresh_all_views()

    def _on_param_changed(self, key: str, val: float):
        st = self._current_state()
        st[key] = int(round(val)) if key == "hum_harmonics" else val
        self._refresh_all_views()
        self.parameterChanged.emit(key, st[key])
        self._emit_state()

    def _on_canvas_sens_dragged(self, new_sens: float):
        self._on_param_changed("click_sensitivity", new_sens)

    def _on_canvas_widen_scrolled(self, delta: float):
        st = self._current_state()
        new_w = max(0.0, min(5.0, round(st["click_widening"] + delta, 1)))
        self._on_param_changed("click_widening", new_w)

    def _on_canvas_hum_dragged(self, new_f: float, new_g: float):
        st = self._current_state()
        st["hum_base_freq"] = new_f
        st["hum_gain"] = new_g
        self._refresh_all_views()
        self.parameterChanged.emit("hum_base_freq", new_f)
        self.parameterChanged.emit("hum_gain", new_g)
        self._emit_state()

    def _on_canvas_harm_scrolled(self, delta: int):
        st = self._current_state()
        new_h = max(1, min(16, int(st["hum_harmonics"]) + delta))
        self._on_param_changed("hum_harmonics", float(new_h))

    def _load_preset(self, name: str):
        if name in self._presets:
            cur_focus = self._current_state()["view_focus"]
            self._banks[self._active_bank] = copy.deepcopy(self._presets[name])
            self._banks[self._active_bank]["view_focus"] = cur_focus
            self._refresh_all_views()
            self._emit_state()

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存修复预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 独立演示动画（模拟口播瞬态咔哒爆点 + 50Hz 工频及其高次谐波尖峰）
    # -----------------------------------------------------------------
    def _tick_demo_animation(self):
        self._anim_t += 0.12
        st = self._current_state()
        en = bool(st["enabled"])

        # 1. 生成模拟口播波形 + 周期性口腔咔哒爆点
        n_w = self.canvas._wave_samples
        raw_w, clean_w = [], []
        clicks: list[tuple[float, float]] = []
        sens_thresh = 1.0 - (st["click_sensitivity"] / 10.0) * 0.85

        for i in range(n_w):
            norm = i / (n_w - 1)
            phase = norm * 16.0 + self._anim_t * 1.4
            speech = 0.28 * math.sin(phase) * (0.5 + 0.5 * math.sin(phase * 0.23)) + 0.08 * math.cos(phase * 3.1)

            # 模拟 3 处不同强度的瞬态爆点
            spike = 0.0
            for c_pos, c_amp in ((0.24, 0.82), (0.58, 0.56), (0.83, 0.91)):
                # 随时间缓缓向左滚动
                moving_pos = (c_pos - (self._anim_t * 0.035)) % 1.0
                if abs(norm - moving_pos) < 0.009:
                    spike = c_amp if ((i % 2) == 0) else -c_amp * 0.85
                    if abs(spike) > sens_thresh and i % 2 == 0:
                        clicks.append((moving_pos, min(1.0, abs(spike))))

            raw_val = max(-0.98, min(0.98, speech + spike))
            raw_w.append(raw_val)

            if en and st["click_enabled"] and abs(spike) > sens_thresh:
                clean_w.append(speech)
            else:
                clean_w.append(raw_val)

        # 2. 生成模拟 50/60Hz 工频及谐波频谱
        f0 = float(st["hum_base_freq"])
        n_harm = int(st["hum_harmonics"])
        in_spec, out_spec = [], []
        for idx, f in enumerate(self.canvas._spec_freqs):
            base = -46.0 - 11.0 * math.log10(max(20.0, f) / 20.0) + 1.8 * math.sin(idx * 0.35 + self._anim_t)
            hum_spike = 0.0
            for h in range(1, 13):
                hf = f0 * h
                if abs(f - hf) < (hf * 0.035):
                    peak_h = max(6.0, 32.0 - (h - 1) * 2.1)
                    hum_spike = max(hum_spike, peak_h * math.exp(-0.5 * (((f - hf) / (hf * 0.012)) ** 2)))

            raw_db = max(-80.0, min(-3.0, base + hum_spike))
            in_spec.append(raw_db)

            # 计算 De-Hum 实际削减后的频谱
            cut = 0.0
            if en and st["hum_enabled"] and hum_spike > 0.5:
                for h in range(1, n_harm + 1):
                    hf = f0 * h
                    if abs(f - hf) < (hf * 0.04):
                        cut = min(hum_spike, abs(st["hum_gain"]) * 0.85)
                        break
            out_spec.append(max(-80.0, min(0.0, raw_db - cut + st["output_gain"])))

        self.canvas.pushClickTelemetry(raw_w, clean_w, clicks)
        self.canvas.pushHumSpectrum(self.canvas._spec_freqs, in_spec, out_spec)

        click_act = min(1.0, len(clicks) * 0.32) if (en and st["click_enabled"]) else 0.0
        hum_red = min(24.0, abs(st["hum_gain"]) * 0.55) if (en and st["hum_enabled"]) else 0.0
        out_peak = max(out_spec) + 6.0
        self.meter_strip.setTelemetry(click_act, hum_red, out_peak)

    # -----------------------------------------------------------------
    # 公开 API：供后端 DSP 引擎与工程序列化调用
    # -----------------------------------------------------------------
    def reset_repair(self):
        self._banks[self._active_bank] = default_repair_state()
        self._refresh_all_views()
        self._emit_state()

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = {**default_repair_state(), **copy.deepcopy(state)}
        self._refresh_all_views()

    def update_realtime_telemetry(
        self,
        raw_wave: list[float],
        clean_wave: list[float],
        detected_clicks: list[tuple[float, float]],
        freqs_hz: list[float],
        input_db: list[float],
        output_db: list[float],
        click_activity: float,
        hum_reduction_db: float,
        out_peak_db: float,
    ):
        """接入真实音频修复引擎的实时波形、频谱与电平监控数据"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.canvas.pushClickTelemetry(raw_wave, clean_wave, detected_clicks)
        self.canvas.pushHumSpectrum(freqs_hz, input_db, output_db)
        self.meter_strip.setTelemetry(click_activity, hum_reduction_db, out_peak_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioDeClickDeHumWidget("Dialogue 1")
    win.show()
    sys.exit(app.exec())