from __future__ import annotations
# ─────────────────────────────────────────────────────────────────────
# NF-02（2026-11-15 计划 §6/§8）：用户原型 SpectralMangler-UI.py 的生产
# 接入副本（适配说明同 voice_changer_widget.py；映射表在
# spectral_mangler_dialog.py）。
# ─────────────────────────────────────────────────────────────────────

import sys
import math
import copy
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QFrame, QLineEdit, QInputDialog, QMenu
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont, QPolygonF
)


# =====================================================================
# 1. 全局色彩系统与默认声音设计预设（延续 EQ-UI / Dynamics-UI 视觉规范）
# =====================================================================
COLORS = {
    "cyan":   "#28D8F0",  # 主控 / A-B 激活 / Ring Modulator 主题色（冰晶青）
    "grain":  "#B67CF8",  # Granular 颗粒处理器主题色（星云紫）
    "dist":   "#F26D6D",  # Saturator / Distortion 失真主题色（熔岩珊瑚红）
    "amber":  "#F0A848",  # 辅助高亮 / Freeze 冻结指示（琥珀金）
    "green":  "#4CD490",  # 电平表安全区（翡翠绿）
}

ROUTING_MODES = [
    "Ring → Grain → Dist",
    "Grain → Ring → Dist",
    "Dist → Grain → Ring",
    "Parallel Blend",
]

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def freq_to_note(hz: float) -> str:
    if hz <= 0:
        return ""
    midi = round(69 + 12 * math.log2(hz / 440.0))
    name = NOTE_NAMES[midi % 12]
    octave = (midi // 12) - 1
    return f"{name}{octave}"


def format_hz(hz: float) -> str:
    if hz >= 1000.0:
        return f"{hz / 1000.0:.2f}K" if hz < 10000.0 else f"{hz / 1000.0:.1f}K"
    if hz < 10.0:
        return f"{hz:.2f}"
    if hz < 100.0:
        return f"{hz:.1f}"
    return f"{int(round(hz))}"


def parse_hz(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("HZ", "").strip()
    try:
        if s.endswith("K"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def parse_float(text: str, fallback: float) -> float:
    s = text.strip().replace("%", "").replace("dB", "").replace("ms", "").replace("st", "").strip()
    try:
        return float(s)
    except ValueError:
        return fallback


def format_ms(val: float) -> str:
    if val < 10.0:
        return f"{val:.1f}ms"
    return f"{int(round(val))}ms"


def default_mangler_demo() -> dict:
    return {
        "enabled": True,
        "routing": "Ring → Grain → Dist",
        "global_mix": 85.0,    # 0 ~ 100 %
        "output_gain": -1.5,   # -24.0 ~ +12.0 dB
        "ring": {
            "enabled": True,
            "mode": "FM-Ring",     # "Sine" | "Dual" | "FM-Ring" | "Rectify"
            "carrier": 340.0,      # 10.0 ~ 5000.0 Hz
            "lfo_rate": 2.4,       # 0.05 ~ 40.0 Hz
            "lfo_depth": 35.0,     # 0 ~ 100 %
            "warp": 28.0,          # 0 ~ 100 % (FM / 波形折叠指数)
            "spread": 45.0,        # 0 ~ 100 % (左右声道载波相位差)
            "mix": 65.0,           # 0 ~ 100 %
        },
        "grain": {
            "enabled": True,
            "freeze": False,
            "mode": "Cloud",       # "Cloud" | "Reverse" | "Scatter"
            "size": 85.0,          # 5.0 ~ 500.0 ms
            "density": 24.0,       # 1.0 ~ 80.0 grains/s
            "pitch": 7.0,          # -24.0 ~ +24.0 st
            "pitch_jitter": 22.0,  # 0 ~ 100 %
            "position": 30.0,      # 0 ~ 100 %
            "spray": 45.0,         # 0 ~ 100 %
            "feedback": 38.0,      # 0 ~ 95 %
            "mix": 70.0,           # 0 ~ 100 %
        },
        "dist": {
            "enabled": True,
            "curve": "Fold",       # "Tape" | "Fold" | "Hard Clip" | "BitCrush"
            "drive": 14.5,         # 0.0 ~ 36.0 dB
            "fold": 42.0,          # 0 ~ 100 % (折叠度 / 降采样破碎度)
            "bias": 12.0,          # -100 ~ +100 % (直流偏置不对称染色)
            "tone": 5200.0,        # 200 ~ 16000 Hz
            "resonance": 25.0,     # 0 ~ 100 %
            "mix": 80.0,           # 0 ~ 100 %
        },
    }


def default_mangler_init() -> dict:
    st = default_mangler_demo()
    st["ring"]["enabled"] = False
    st["grain"]["enabled"] = False
    st["dist"]["enabled"] = False
    st["global_mix"] = 100.0
    st["output_gain"] = 0.0
    return st


# =====================================================================
# 2. 原创矢量波形曲线图标绘制（用于失真曲线选择器，呼应 EQ-UI 滤波器图标）
# =====================================================================
def draw_shaper_glyph(painter: QPainter, rect: QRectF, curve_type: str, color: QColor):
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    cx = rect.center().x()
    cy = rect.center().y()
    w = min(rect.width() * 0.74, 22.0)
    h = min(rect.height() * 0.58, 10.0)
    left = cx - w / 2.0
    right = cx + w / 2.0
    top = cy - h / 2.0
    bot = cy + h / 2.0

    path = QPainterPath()
    if curve_type == "Tape":
        # S 形柔和饱和曲线
        path.moveTo(left, bot)
        path.cubicTo(cx - 2.0, bot, cx + 2.0, top, right, top)
    elif curve_type == "Fold":
        # West-Coast 波形折叠锯齿折返曲线
        path.moveTo(left, cy + h * 0.25)
        path.lineTo(left + w * 0.28, top)
        path.lineTo(cx, bot)
        path.lineTo(right - w * 0.28, top)
        path.lineTo(right, cy - h * 0.25)
    elif curve_type == "Hard Clip":
        # 硬削波平顶阶跃曲线
        path.moveTo(left, bot)
        path.lineTo(cx - 3.5, bot)
        path.lineTo(cx + 3.5, top)
        path.lineTo(right, top)
    elif curve_type == "BitCrush":
        # 阶梯量化破碎曲线
        steps = 4
        dx = w / steps
        dy = h / steps
        path.moveTo(left, bot)
        for i in range(steps):
            x0 = left + i * dx
            y0 = bot - i * dy
            path.lineTo(x0 + dx, y0)
            if i < steps - 1:
                path.lineTo(x0 + dx, y0 - dy)

    painter.drawPath(path)
    painter.restore()


# =====================================================================
# 3. 通用基础控件（电源开关 / 曲线选择器 / 可编辑数值胶囊 / 拉丝阳极铝旋钮）
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


class ShaperCurveSelector(QPushButton):
    curveChanged = pyqtSignal(str)

    def __init__(self, options: list[str], current: str, accent_color: str, parent=None):
        super().__init__(parent)
        self._options = options
        self._current = current
        self._accent = QColor(accent_color)
        self._active = True
        self.setFixedSize(88, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._open_menu)

    def setCurrentCurve(self, curve: str):
        if curve in self._options and self._current != curve:
            self._current = curve
            self.update()

    def setModuleActive(self, active: bool):
        self._active = active
        self.update()

    def _open_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1B1C20;
                border: 1px solid #383A44;
                border-radius: 4px;
                padding: 4px;
                color: #E2E4EC;
                font-size: 11px;
            }
            QMenu::item { padding: 5px 16px; border-radius: 3px; }
            QMenu::item:selected { background-color: #2A2E3A; color: #F26D6D; }
        """)
        for opt in self._options:
            act = menu.addAction(opt)
            act.triggered.connect(lambda checked=False, o=opt: self._select(o))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _select(self, curve: str):
        if self._current != curve:
            self._current = curve
            self.update()
            self.curveChanged.emit(curve)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        bg = QColor("#15161A") if self._active else QColor("#202126")
        border = QColor("#363842") if self._active else QColor("#2E3038")
        p.setPen(QPen(border, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 4.0, 4.0)

        glyph_color = self._accent if self._active else QColor("#747680")
        glyph_rect = QRectF(4, 2, 24, self.height() - 4)
        draw_shaper_glyph(p, glyph_rect, self._current, glyph_color)

        p.setPen(QColor("#E4E7F0") if self._active else QColor("#747680"))
        font = QFont("SF Pro Display", 8)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.drawText(QRectF(28, 0, self.width() - 40, self.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._current)

        ax = self.width() - 8.5
        ay = self.height() / 2.0 - 1.2
        p.setPen(QPen(QColor("#8E909A"), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ax - 2.3, ay), QPointF(ax, ay + 2.5))
        p.drawLine(QPointF(ax, ay + 2.5), QPointF(ax + 2.3, ay))


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
                background-color: #0B0C0E;
                color: {accent_color};
                border: 1px solid {accent_color};
                border-radius: 3.5px;
                font-family: 'SF Pro Display', 'Segoe UI', 'Microsoft YaHei';
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
                .replace("%", "")
                .replace("ms", "")
                .replace("st", "")
                .replace("Hz", "")
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

        arc_r = 14.8
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深色凹槽底环
        p.setPen(QPen(QColor("#111216"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性模式在 12 点钟方向绘制中心零点标记
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#6E7485"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 2. 模块主题色光环
        arc_col = self._accent if self._active else QColor("#565864")
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

        # 3. 旋钮主体（拉丝阳极铝质感）
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
# 4. 控件：右侧 Global Mix & Output 推子 + 实时电平表（统一 EQ-UI / Dynamics-UI）
# =====================================================================
class ManglerMasterStrip(QWidget):
    mixChanged = pyqtSignal(float)
    gainChanged = pyqtSignal(float)
    limiterToggled = pyqtSignal(bool)  # 新增：限幅器信号（对接后端 DSP 限幅）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mix = 85.0
        self._gain = -1.5
        self._in_db = -18.0
        self._out_db = -14.0
        self._limiter = True
        self._clipped = False
        self._drag_target = ""  # "mix" | "gain" | ""
        self.setFixedWidth(104)  # 略微加宽容纳双电平表
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setMix(self, val: float, emit: bool = False):
        clamped = max(0.0, min(100.0, round(val, 0)))
        if abs(clamped - self._mix) > 1e-4:
            self._mix = clamped
            self.update()
            if emit:
                self.mixChanged.emit(self._mix)

    def setGain(self, val: float, emit: bool = False):
        clamped = max(-24.0, min(12.0, round(val, 1)))
        if abs(clamped - self._gain) > 1e-4:
            self._gain = clamped
            self.update()
            if emit:
                self.gainChanged.emit(self._gain)

    def setLevels(self, in_db: float, out_db: float):
        self._in_db = max(-60.0, min(0.0, in_db))
        self._out_db = max(-60.0, min(0.0, out_db))
        if out_db >= -0.1:
            self._clipped = True
        self.update()

    def _mix_track(self) -> QRectF:
        return QRectF(20.0, 22.0, 4.0, max(40.0, self.height() - 48.0))

    def _gain_track(self) -> QRectF:
        return QRectF(54.0, 22.0, 4.0, max(40.0, self.height() - 48.0))

    def _y_to_mix(self, y: float) -> float:
        tr = self._mix_track()
        norm = max(0.0, min(1.0, (y - tr.top()) / tr.height()))
        return (1.0 - norm) * 100.0

    def _y_to_gain(self, y: float) -> float:
        tr = self._gain_track()
        norm = max(0.0, min(1.0, (y - tr.top()) / tr.height()))
        return 12.0 - norm * 36.0

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            # 点击右上角区域：清除削波灯或切换 LIM
            if pos.y() <= 22.0 and pos.x() >= 72.0:
                if self._clipped:
                    self._clipped = False
                else:
                    self._limiter = not self._limiter
                    self.limiterToggled.emit(self._limiter)
                self.update()
                return
            if pos.x() < 38.0:
                self._drag_target = "mix"
                self.setMix(self._y_to_mix(pos.y()), emit=True)
            elif pos.x() < 72.0:
                self._drag_target = "gain"
                self.setGain(self._y_to_gain(pos.y()), emit=True)

    def mouseMoveEvent(self, event):
        if self._drag_target == "mix":
            self.setMix(self._y_to_mix(event.position().y()), emit=True)
        elif self._drag_target == "gain":
            self.setGain(self._y_to_gain(event.position().y()), emit=True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_target = ""

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if event.position().x() < 38.0:
                self.setMix(100.0, emit=True)
            elif event.position().x() < 72.0:
                self.setGain(0.0, emit=True)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        if event.position().x() < 38.0:
            self.setMix(self._mix + delta * 5.0, emit=True)
        else:
            self.setGain(self._gain + delta * 0.5, emit=True)
        event.accept()

    def _draw_fader(self, p: QPainter, tr: QRectF, norm: float, accent: QColor, label_top: str, label_bot: str):
        cx = tr.center().x()
        p.setFont(QFont("SF Pro Display", 8))
        p.setPen(QColor("#8E94A4"))
        p.drawText(QRectF(cx - 18, 2, 36, 16), Qt.AlignmentFlag.AlignCenter, label_top)

        p.setPen(QPen(QColor("#282B34"), 1.0))
        p.setBrush(QColor("#0E1014"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        cy = tr.top() + (1.0 - norm) * tr.height()
        if norm > 0.01:
            fill_r = QRectF(tr.left() + 0.5, cy, tr.width() - 1.0, tr.bottom() - cy)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 130))
            p.drawRoundedRect(fill_r, 1.5, 1.5)

        cap_w, cap_h = 16.0, 22.0
        cap_rect = QRectF(cx - cap_w / 2.0, cy - cap_h / 2.0, cap_w, cap_h)
        grad = QLinearGradient(cap_rect.topLeft(), cap_rect.bottomLeft())
        grad.setColorAt(0.0, QColor("#565A68"))
        grad.setColorAt(0.5, QColor("#3A3D48"))
        grad.setColorAt(1.0, QColor("#4A4E5C"))
        p.setPen(QPen(QColor("#111216"), 1.2))
        p.setBrush(grad)
        p.drawRoundedRect(cap_rect, 3.0, 3.0)

        p.setPen(QPen(accent, 1.6))
        p.drawLine(QPointF(cap_rect.left() + 3, cy), QPointF(cap_rect.right() - 3, cy))

        font = QFont("SF Pro Display", 8)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(accent)
        p.drawText(QRectF(cx - 18, self.height() - 21, 36, 18), Qt.AlignmentFlag.AlignCenter, label_bot)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        mix_tr = self._mix_track()
        gain_tr = self._gain_track()

        self._draw_fader(
            p, mix_tr, self._mix / 100.0,
            QColor(COLORS["grain"]), "MIX", f"{int(round(self._mix))}%"
        )
        gain_norm = (self._gain - (-24.0)) / 36.0
        g_str = f"{self._gain:+.1f}" if abs(self._gain) >= 0.05 else "0.0"
        self._draw_fader(
            p, gain_tr, gain_norm,
            QColor(COLORS["cyan"]), "OUT", g_str
        )

        # 右侧 IN (左细条) + OUT (右细条) 双通道对比电平表
        in_rect = QRectF(76.0, gain_tr.top(), 4.5, gain_tr.height())
        out_rect = QRectF(83.0, gain_tr.top(), 5.5, gain_tr.height())

        for r_box, db_val, is_out in ((in_rect, self._in_db, False), (out_rect, self._out_db, True)):
            p.setPen(QPen(QColor("#262932"), 0.8))
            p.setBrush(QColor("#0D0E12"))
            p.drawRoundedRect(r_box, 1.5, 1.5)

            norm = max(0.0, min(1.0, (db_val + 60.0) / 60.0))
            if norm > 0.01:
                fh = r_box.height() * norm
                f_rect = QRectF(r_box.left() + 0.4, r_box.bottom() - fh, r_box.width() - 0.8, fh)
                p.setPen(Qt.PenStyle.NoPen)
                if is_out:
                    grad = QLinearGradient(r_box.bottomLeft(), r_box.topLeft())
                    grad.setColorAt(0.0, QColor(COLORS["green"]))
                    grad.setColorAt(0.75, QColor(COLORS["amber"]))
                    grad.setColorAt(1.0, QColor(COLORS["dist"]))
                    p.setBrush(grad)
                else:
                    p.setBrush(QColor(110, 125, 150, 170))  # 输入电平采用冷灰蓝
                p.drawRoundedRect(f_rect, 1.0, 1.0)

        # 顶部 LIM / CLIP 状态小胶囊
        lim_rect = QRectF(74.0, 4.0, 22.0, 13.0)
        if self._clipped:
            bg, fg, txt = QColor("#FF4848"), QColor("#FFFFFF"), "CLIP"
        elif self._limiter:
            bg, fg, txt = QColor("#183228"), QColor(COLORS["green"]), "LIM"
        else:
            bg, fg, txt = QColor("#1E2028"), QColor("#666B78"), "LIM"
        p.setPen(QPen(fg, 0.8))
        p.setBrush(bg)
        p.drawRoundedRect(lim_rect, 2.5, 2.5)
        p.setFont(QFont("SF Pro Display", 6))
        p.drawText(lim_rect, Qt.AlignmentFlag.AlignCenter, txt)


# =====================================================================
# 5. 控件：核心奇幻畸变交互画布（颗粒云粒子场 + 调制折叠示波器 + 节点直拖 + 传输缩略窗）
# =====================================================================
class ProManglerVisualizerWidget(QWidget):
    # 直接在画布上拖拽三大核心控制节点，联动下方旋钮
    ringNodeDragged = pyqtSignal(float, float)      # carrier_hz, mix_pct
    ringNodeScrolled = pyqtSignal(float)            # new_warp_pct
    grainNodeDragged = pyqtSignal(float, float)     # size_ms, pitch_st
    grainNodeScrolled = pyqtSignal(float)           # new_density_hz
    distNodeDragged = pyqtSignal(float, float)      # fold_pct, drive_db
    distNodeScrolled = pyqtSignal(float)            # new_tone_hz
    moduleToggleRequested = pyqtSignal(str)         # "ring" | "grain" | "dist" (双击节点快速旁通)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(255)
        self.setMouseTracking(True)

        self._state = default_mangler_demo()
        self._phase = 0.0
        self._hover_node = ""     # "ring" | "grain" | "dist" | ""
        self._dragging_node = ""
        self._mouse_pos: QPointF | None = None

        self._show_grains = True
        self._show_shaper = True

        self._btn_grains_rect = QRectF()
        self._btn_shaper_rect = QRectF()

        # 外部真实波形缓冲 & 真实颗粒列表（接入后端后可覆盖演示模拟）
        self._external_wave: list[float] | None = None
        self._external_grains: list[dict] | None = None

        # 内部粒子系统（用于直观展现颗粒的破碎、漂浮、反转与散布）
        self._rng = random.Random(1337)
        self._particles: list[dict] = []
        for _ in range(42):
            self._particles.append(self._spawn_particle(initial=True))

        self._ml = 14.0
        self._mr = 14.0
        self._mt = 10.0
        self._mb = 22.0

    def _spawn_particle(self, initial: bool = False) -> dict:
        return {
            "x": self._rng.uniform(0.05, 0.95),
            "y": self._rng.uniform(-0.85, 0.85),
            "vx": self._rng.uniform(-0.004, 0.004),
            "vy": self._rng.uniform(-0.008, 0.008),
            "age": self._rng.uniform(0.0, 1.0) if initial else 0.0,
            "life": self._rng.uniform(0.6, 1.6),
            "rev": self._rng.random() < 0.35,
        }

    def setState(self, state: dict):
        self._state = state
        self.update()

    def setTelemetry(self, wave_samples: list[float] | None = None, grains: list[dict] | None = None):
        if wave_samples is not None:
            self._external_wave = wave_samples
        if grains is not None:
            self._external_grains = grains
        self.update()

    def stepAnimation(self, dt: float = 0.045):
        st = self._state
        if st["enabled"]:
            rate = st["ring"]["lfo_rate"] if st["ring"]["enabled"] else 1.2
            self._phase += dt * (1.8 + rate * 0.35)

            g = st["grain"]
            if g["enabled"]:
                speed_scale = 0.25 if g["freeze"] else (0.6 + g["density"] / 45.0)
                spray_scale = 0.2 + (g["spray"] / 100.0) * 1.8
                for pt in self._particles:
                    pt["age"] += dt * speed_scale / pt["life"]
                    dir_sign = -1.0 if g["mode"] == "Reverse" else 1.0
                    pt["x"] += pt["vx"] * spray_scale * dir_sign
                    pt["y"] += pt["vy"] * spray_scale
                    if pt["age"] >= 1.0 or not (0.02 <= pt["x"] <= 0.98) or not (-0.95 <= pt["y"] <= 0.95):
                        pt.update(self._spawn_particle(initial=False))
                        pt["x"] = max(0.05, min(0.95, (g["position"] / 100.0) + self._rng.uniform(-0.35, 0.35) * (g["spray"] / 100.0 + 0.15)))
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(80.0, self.width() - self._ml - self._mr),
            max(80.0, self.height() - self._mt - self._mb),
        )

    # -----------------------------------------------------------------
    # 三大交互节点的坐标映射（X/Y 双向绑定核心参数）
    # -----------------------------------------------------------------
    def _ring_node_pos(self, plot: QRectF) -> QPointF:
        r = self._state["ring"]
        norm_x = math.log10(max(10.0, r["carrier"]) / 10.0) / math.log10(5000.0 / 10.0)
        norm_y = r["mix"] / 100.0
        return QPointF(plot.left() + norm_x * plot.width(), plot.bottom() - norm_y * plot.height())

    def _grain_node_pos(self, plot: QRectF) -> QPointF:
        g = self._state["grain"]
        norm_x = math.log10(max(5.0, g["size"]) / 5.0) / math.log10(500.0 / 5.0)
        norm_y = (g["pitch"] + 24.0) / 48.0
        return QPointF(plot.left() + norm_x * plot.width(), plot.bottom() - norm_y * plot.height())

    def _dist_node_pos(self, plot: QRectF) -> QPointF:
        d = self._state["dist"]
        norm_x = d["fold"] / 100.0
        norm_y = d["drive"] / 36.0
        return QPointF(plot.left() + norm_x * plot.width(), plot.bottom() - norm_y * plot.height())

    def _hit_test_node(self, pos: QPointF) -> str:
        plot = self._plot_rect()
        st = self._state
        nodes = [
            ("dist", st["dist"]["enabled"], self._dist_node_pos(plot)),
            ("grain", st["grain"]["enabled"], self._grain_node_pos(plot)),
            ("ring", st["ring"]["enabled"], self._ring_node_pos(plot)),
        ]
        for tag, en, pt in nodes:
            if en and math.hypot(pos.x() - pt.x(), pos.y() - pt.y()) <= 13.5:
                return tag
        return ""

    # -----------------------------------------------------------------
    # 非线性失真传递函数（用于实时波形与右下角 Waveshaper 曲线预览）
    # -----------------------------------------------------------------
    def _eval_waveshaper(self, x: float) -> float:
        d = self._state["dist"]
        if not d["enabled"]:
            return max(-1.0, min(1.0, x))

        drive_lin = 10.0 ** (d["drive"] / 24.0)
        bias = (d["bias"] / 100.0) * 0.45
        fold_amt = d["fold"] / 100.0
        curve = d["curve"]

        u = x * drive_lin + bias
        if curve == "Tape":
            y = math.tanh(u)
        elif curve == "Fold":
            freq = 1.0 + fold_amt * 4.5
            y = math.sin(u * freq * (math.pi * 0.5))
        elif curve == "Hard Clip":
            thresh = max(0.18, 1.0 - fold_amt * 0.72)
            y = max(-thresh, min(thresh, u)) / thresh
        elif curve == "BitCrush":
            levels = max(2.0, 24.0 * (1.0 - fold_amt * 0.92))
            clamped = max(-1.0, min(1.0, u))
            y = round(clamped * levels) / levels
        else:
            y = math.tanh(u)

        mix = d["mix"] / 100.0
        out = (1.0 - mix) * max(-1.0, min(1.0, x)) + mix * y
        return max(-1.0, min(1.0, out))

    # -----------------------------------------------------------------
    # 鼠标交互：拖拽节点 / 滚轮调节第三维度 / 双击快速旁通
    # -----------------------------------------------------------------
    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_node = ""
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_grains_rect.contains(pos):
                self._show_grains = not self._show_grains
                self.update()
                return
            if self._btn_shaper_rect.contains(pos):
                self._show_shaper = not self._show_shaper
                self.update()
                return

            hit = self._hit_test_node(pos)
            if hit:
                self._dragging_node = hit
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._hit_test_node(event.position())
            if hit:
                self.moduleToggleRequested.emit(hit)

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()
        nx = max(0.0, min(1.0, (event.position().x() - plot.left()) / plot.width()))
        ny = max(0.0, min(1.0, (plot.bottom() - event.position().y()) / plot.height()))

        if self._dragging_node == "ring":
            carrier = round(10.0 * ((5000.0 / 10.0) ** nx), 1)
            mix = round(ny * 100.0, 0)
            self.ringNodeDragged.emit(carrier, mix)
        elif self._dragging_node == "grain":
            size = round(5.0 * ((500.0 / 5.0) ** nx), 1)
            pitch = round(-24.0 + ny * 48.0, 1)
            self.grainNodeDragged.emit(size, pitch)
        elif self._dragging_node == "dist":
            fold = round(nx * 100.0, 0)
            drive = round(ny * 36.0, 1)
            self.distNodeDragged.emit(fold, drive)
        else:
            hit = self._hit_test_node(event.position())
            self._hover_node = hit
            self.setCursor(Qt.CursorShape.OpenHandCursor if hit else Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_node:
            self._dragging_node = ""
            self.update()

    def wheelEvent(self, event):
        target = self._dragging_node or self._hover_node
        if not target:
            return
        delta = event.angleDelta().y() / 120.0
        st = self._state
        if target == "ring":
            new_warp = max(0.0, min(100.0, round(st["ring"]["warp"] + delta * 4.0, 0)))
            self.ringNodeScrolled.emit(new_warp)
        elif target == "grain":
            new_dens = max(1.0, min(80.0, round(st["grain"]["density"] + delta * 2.5, 1)))
            self.grainNodeScrolled.emit(new_dens)
        elif target == "dist":
            norm = math.log10(max(200.0, st["dist"]["tone"]) / 200.0) / math.log10(16000.0 / 200.0)
            norm = max(0.0, min(1.0, norm + delta * 0.04))
            new_tone = round(200.0 * ((16000.0 / 200.0) ** norm), 0)
            self.distNodeScrolled.emit(new_tone)
        event.accept()

    # -----------------------------------------------------------------
    # 绘制网格、颗粒云场、调制折叠波形、Waveshaper 缩略窗与节点
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 深空渐变底色（与 EQ-UI / Dynamics-UI 完全一致）
        bg_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#181A20"))
        bg_grad.setColorAt(1.0, QColor("#13151A"))
        p.fillRect(plot, bg_grad)

        # 2. 科幻坐标网格 + 底部调制标尺
        grid_pen = QPen(QColor("#232630"), 1.0)
        zero_pen = QPen(QColor("#343948"), 1.1)

        for i in range(9):
            y = plot.top() + (i / 8.0) * plot.height()
            p.setPen(zero_pen if i == 4 else grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        # 悬停/拖拽节点时，底部标尺动态切换为该模块的物理单位刻度
        target_axis = self._dragging_node or self._hover_node
        axis_labels = {
            "ring":  ["10Hz", "35Hz", "120Hz", "420Hz", "1.5K", "5KHz"],
            "grain": ["5ms", "15ms", "45ms", "130ms", "300ms", "500ms"],
            "dist":  ["0%", "20%", "40%", "60%", "80%", "100% Fold"],
        }.get(target_axis, ["0%", "20%", "40%", "60%", "80%", "100%"])

        cols = 10
        p.setFont(QFont("SF Pro Display", 7))
        for i in range(cols + 1):
            x = plot.left() + (i / cols) * plot.width()
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            if i % 2 == 0 and (i // 2) < len(axis_labels):
                p.setPen(QColor("#8E96A8") if target_axis else QColor("#6E7382"))
                p.drawText(QRectF(x - 24, plot.bottom() + 4, 48, 16), Qt.AlignmentFlag.AlignCenter, axis_labels[i // 2])

        p.setPen(QPen(QColor("#2E323D"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        st = self._state
        master_on = st["enabled"]

        # 3. 颗粒云空间粒子场 (Granular Cloud Field)
        if master_on and self._show_grains and st["grain"]["enabled"]:
            g = st["grain"]
            g_col = QColor(COLORS["grain"])
            active_count = max(6, int((g["density"] / 80.0) * len(self._particles)))
            size_norm = (g["size"] - 5.0) / 495.0
            cap_w = 6.0 + size_norm * 26.0
            cap_h = 3.0 + (g["pitch_jitter"] / 100.0) * 5.0

            # 播放头位置指示光柱
            head_x = plot.left() + (g["position"] / 100.0) * plot.width()
            head_grad = QLinearGradient(QPointF(head_x - 24, 0), QPointF(head_x + 24, 0))
            head_grad.setColorAt(0.0, QColor(182, 124, 248, 0))
            head_grad.setColorAt(0.5, QColor(182, 124, 248, 42 if g["freeze"] else 24))
            head_grad.setColorAt(1.0, QColor(182, 124, 248, 0))
            p.fillRect(QRectF(head_x - 24, plot.top(), 48, plot.height()), head_grad)

            for pt in self._particles[:active_count]:
                env = math.sin(max(0.0, min(1.0, pt["age"])) * math.pi)
                alpha = int(155 * env * (g["mix"] / 100.0))
                if alpha <= 4:
                    continue
                px = plot.left() + pt["x"] * plot.width()
                py = plot.center().y() - pt["y"] * (plot.height() * 0.44)
                p.setPen(QPen(QColor(215, 180, 255, min(220, alpha + 40)), 0.9))
                p.setBrush(QColor(g_col.red(), g_col.green(), g_col.blue(), alpha // 2))
                p.drawRoundedRect(QRectF(px - cap_w / 2.0, py - cap_h / 2.0, cap_w, cap_h), 2.0, 2.0)

        # 4. 实时环形调制 + 波形折叠示波器
        cy = plot.center().y()
        amp = plot.height() * 0.38
        steps = 360

        dry_path = QPainterPath()
        wet_path = QPainterPath()
        wet_area = QPainterPath()
        wet_area.moveTo(plot.left(), cy)

        r = st["ring"]
        g = st["grain"]
        d = st["dist"]

        lfo_mod = math.sin(self._phase * 0.7) * (r["lfo_depth"] / 100.0) * 0.45
        carrier_cycles = 2.0 + (math.log10(max(10.0, r["carrier"]) / 10.0) / 2.7) * 34.0 * (1.0 + lfo_mod)
        warp_idx = (r["warp"] / 100.0) * 3.2

        # 模块处理函数（供下方按 Routing 拓扑动态串联调用）
        def apply_ring(s_val: float, t_norm: float) -> float:
            if not r["enabled"]:
                return s_val
            c_arg = 2.0 * math.pi * carrier_cycles * t_norm + self._phase
            if r["mode"] == "FM-Ring":
                c_arg += warp_idx * math.sin(c_arg * 1.5)
            mod_w = math.cos(c_arg)
            if r["mode"] == "Dual":
                mod_w = 0.5 * (mod_w + math.cos(c_arg * 1.035 + 0.8))
            elif r["mode"] == "Rectify":
                mod_w = abs(mod_w) * 2.0 - 1.0
            rm_mix = r["mix"] / 100.0
            return (1.0 - rm_mix) * s_val + rm_mix * (s_val * mod_w)

        def apply_grain(s_val: float, t_norm: float) -> float:
            if not g["enabled"]:
                return s_val
            g_env = 0.75 + 0.25 * math.sin(2.0 * math.pi * (4.0 + g["density"] * 0.25) * t_norm - self._phase * 1.4)
            shimmer = 0.18 * (g["mix"] / 100.0) * math.sin(2.0 * math.pi * (8.0 + abs(g["pitch"]) * 0.8) * t_norm + self._phase * 1.9)
            return s_val * g_env + shimmer

        for i in range(steps + 1):
            t = i / steps
            x = plot.left() + t * plot.width()

            # 原始载波参考线
            base_sig = (
                0.65 * math.sin(2.0 * math.pi * 2.0 * t + self._phase * 0.5)
                + 0.35 * math.cos(2.0 * math.pi * 3.0 * t - self._phase * 0.35)
            )
            dy = cy - base_sig * (amp * 0.65)
            if i == 0:
                dry_path.moveTo(x, dy)
            else:
                dry_path.lineTo(x, dy)

            sig = base_sig
            if master_on:
                # 按路由拓扑执行处理链（响应顶部 Routing 切换）
                route = st.get("routing", "Ring → Grain → Dist")
                if route == "Grain → Ring → Dist":
                    sig = self._eval_waveshaper(apply_ring(apply_grain(sig, t), t))
                elif route == "Dist → Grain → Ring":
                    sig = apply_ring(apply_grain(self._eval_waveshaper(sig), t), t)
                elif route == "Parallel Blend":
                    s_r = apply_ring(sig, t)
                    s_g = apply_grain(sig, t)
                    s_d = self._eval_waveshaper(sig)
                    sig = (s_r + s_g + s_d) / 2.2
                else:  # "Ring → Grain → Dist"
                    sig = self._eval_waveshaper(apply_grain(apply_ring(sig, t), t))

            wy = cy - max(-1.05, min(1.05, sig)) * amp
            if i == 0:
                wet_path.moveTo(x, wy)
            else:
                wet_path.lineTo(x, wy)
            wet_area.lineTo(x, wy)

        wet_area.lineTo(plot.right(), cy)
        wet_area.closeSubpath()

        # 淡淡的原始信号虚线参考
        p.setPen(QPen(QColor(130, 140, 165, 55), 1.0, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(dry_path)

        if master_on:
            # 能量渐变填充（从青色过渡到珊瑚红过载光晕）
            wave_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            wave_grad.setColorAt(0.0, QColor(242, 109, 109, 44 if d["enabled"] else 22))
            wave_grad.setColorAt(0.5, QColor(40, 216, 240, 14))
            wave_grad.setColorAt(1.0, QColor(242, 109, 109, 44 if d["enabled"] else 22))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(wave_grad)
            p.drawPath(wet_area)

            # 主波形外发光 + 锐利亮线
            glow_col = QColor(COLORS["dist"]) if (d["enabled"] and d["drive"] > 18.0) else QColor(COLORS["cyan"])
            p.setPen(QPen(QColor(glow_col.red(), glow_col.green(), glow_col.blue(), 52), 4.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(wet_path)

            p.setPen(QPen(QColor("#F2F8FC"), 1.8))
            p.drawPath(wet_path)
        else:
            p.setPen(QPen(QColor("#686C78"), 1.6))
            p.drawPath(wet_path)

        # 5. 右下角迷你 Waveshaper 传输函数预览窗（呼应 Dynamics-UI 传输曲线）
        if self._show_shaper:
            inset = QRectF(plot.right() - 104, plot.bottom() - 82, 94, 72)
            p.setPen(QPen(QColor("#323644"), 1.0))
            p.setBrush(QColor(14, 16, 21, 220))
            p.drawRoundedRect(inset, 4.0, 4.0)

            # 十字基准线
            p.setPen(QPen(QColor("#262A35"), 1.0))
            p.drawLine(QPointF(inset.left() + 6, inset.center().y()), QPointF(inset.right() - 6, inset.center().y()))
            p.drawLine(QPointF(inset.center().x(), inset.top() + 6), QPointF(inset.center().x(), inset.bottom() - 6))

            shaper_path = QPainterPath()
            pad_x, pad_y = 8.0, 14.0
            sw = inset.width() - pad_x * 2
            sh = inset.height() - pad_y - 8.0
            for k in range(61):
                u = -1.0 + (k / 60.0) * 2.0
                v = self._eval_waveshaper(u) if master_on else u
                sx = inset.left() + pad_x + ((u + 1.0) * 0.5) * sw
                sy = inset.top() + pad_y + ((1.0 - v) * 0.5) * sh
                if k == 0:
                    shaper_path.moveTo(sx, sy)
                else:
                    shaper_path.lineTo(sx, sy)

            shaper_col = QColor(COLORS["dist"]) if (master_on and d["enabled"]) else QColor("#686C78")
            p.setPen(QPen(shaper_col, 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(shaper_path)

            p.setPen(QColor("#8E94A4"))
            p.setFont(QFont("SF Pro Display", 7))
            p.drawText(QRectF(inset.left() + 6, inset.top() + 2, inset.width() - 12, 12), Qt.AlignmentFlag.AlignLeft, f"SHAPER · {d['curve'].upper()}")

        # 6. 三大模块彩色交互控制节点 (R = Ring Mod / G = Granular / D = Distortion)
        node_specs = [
            ("ring", "R", COLORS["cyan"], r["enabled"], self._ring_node_pos(plot)),
            ("grain", "G", COLORS["grain"], g["enabled"], self._grain_node_pos(plot)),
            ("dist", "D", COLORS["dist"], d["enabled"], self._dist_node_pos(plot)),
        ]
        node_font = QFont("SF Pro Display", 8)
        node_font.setWeight(QFont.Weight.Bold)
        p.setFont(node_font)

        for tag, letter, hex_col, en, pt in node_specs:
            if not en:
                continue
            col = QColor(hex_col) if master_on else QColor("#565A66")
            is_hi = (self._hover_node == tag or self._dragging_node == tag)
            radius = 11.0 if is_hi else 9.0

            if is_hi and master_on:
                # 十字定位引导线
                p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 65), 1.0, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(pt.x(), plot.top()), QPointF(pt.x(), plot.bottom()))
                p.drawLine(QPointF(plot.left(), pt.y()), QPointF(plot.right(), pt.y()))

                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(col.red(), col.green(), col.blue(), 70))
                p.drawEllipse(pt, radius + 5.0, radius + 5.0)

            p.setPen(QPen(col, 1.9))
            p.setBrush(QColor("#14161C"))
            p.drawEllipse(pt, radius, radius)

            p.setPen(QColor("#F6F8FC"))
            p.drawText(QRectF(pt.x() - radius, pt.y() - radius, radius * 2, radius * 2), Qt.AlignmentFlag.AlignCenter, letter)

            # 悬停/拖拽时在节点旁绘制微型实时数值胶囊
            if is_hi and master_on:
                badge_txt = {
                    "ring":  f"{format_hz(r['carrier'])}Hz · {int(r['mix'])}%",
                    "grain": f"{format_ms(g['size'])} · {g['pitch']:+.1f}st",
                    "dist":  f"Fold {int(d['fold'])}% · +{d['drive']:.1f}dB",
                }[tag]
                bx = min(plot.right() - 96.0, max(plot.left() + 6.0, pt.x() + 14.0))
                by = max(plot.top() + 32.0, pt.y() - 10.0)
                b_rect = QRectF(bx, by, 90.0, 18.0)
                p.setPen(QPen(col, 1.0))
                p.setBrush(QColor(14, 16, 22, 230))
                p.drawRoundedRect(b_rect, 3.0, 3.0)
                p.setPen(QColor("#F4F7FC"))
                p.setFont(QFont("SF Pro Display", 7))
                p.drawText(b_rect, Qt.AlignmentFlag.AlignCenter, badge_txt)

        # 7. 右上角视觉图层切换胶囊 (GRAINS / SHAPER)
        self._btn_grains_rect = QRectF(plot.right() - 104, plot.top() + 8, 48, 18)
        self._btn_shaper_rect = QRectF(plot.right() - 52, plot.top() + 8, 44, 18)
        for rect, label, active, col_hex in (
            (self._btn_grains_rect, "GRAINS", self._show_grains, COLORS["grain"]),
            (self._btn_shaper_rect, "SHAPER", self._show_shaper, COLORS["dist"]),
        ):
            p.setPen(QPen(QColor(col_hex) if active else QColor("#282B34"), 1.0))
            p.setBrush(QColor(24, 28, 38, 210) if active else QColor(18, 20, 25, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor(col_hex) if active else QColor("#6E727E"))
            p.setFont(QFont("SF Pro Display", 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 8. 左上角实时声音设计参数 HUD
        target_hud = self._dragging_node or self._hover_node
        if target_hud == "ring":
            hud_str = f"RING MOD  •  Carrier {format_hz(r['carrier'])}Hz ({freq_to_note(r['carrier'])})  •  Mix {int(r['mix'])}%  •  Warp {int(r['warp'])}%"
            hud_col = QColor(COLORS["cyan"])
            hud_w = 325
        elif target_hud == "grain":
            hud_str = f"GRANULAR  •  Size {format_ms(g['size'])}  •  Pitch {g['pitch']:+.1f}st  •  Density {g['density']:.1f}Hz"
            hud_col = QColor(COLORS["grain"])
            hud_w = 315
        elif target_hud == "dist":
            hud_str = f"DISTORTION ({d['curve']})  •  Drive +{d['drive']:.1f}dB  •  Fold {int(d['fold'])}%  •  Tone {format_hz(d['tone'])}Hz"
            hud_col = QColor(COLORS["dist"])
            hud_w = 335
        else:
            hud_str = f"R: {format_hz(r['carrier'])}Hz   |   G: {format_ms(g['size'])} ({g['pitch']:+.0f}st)   |   D: {d['curve']} +{d['drive']:.1f}dB"
            hud_col = QColor("#B4BAC8")
            hud_w = 290

        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
        p.setPen(QPen(QColor("#323642"), 1.0))
        p.setBrush(QColor(16, 18, 23, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(hud_col)
        p.setFont(QFont("SF Pro Display", 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 6. 模块卡片 1：Ring Modulator Card（环形调制器：外星 / 金属诡异声）
# =====================================================================
class RingModulatorCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["cyan"]
        self._updating = False
        self._master_bypassed = False

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

        self.title_lbl = QLabel("Ring Mod")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        self.mode_combo = QComboBox()
        self.mode_combo.setFixedWidth(84)
        self.mode_combo.addItems(["Sine", "Dual", "FM-Ring", "Rectify"])
        self.mode_combo.currentTextChanged.connect(self._on_mode)
        h_lay.addWidget(self.mode_combo)
        root.addWidget(header)

        # Body (2x3 旋钮矩阵)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_carrier = KnobParameterCell(
            "Carrier", 10.0, 5000.0, 340.0, self._accent,
            "10", "Hz", "5K",
            format_hz, parse_hz, mode="log"
        )
        self.c_lfo_rate = KnobParameterCell(
            "LFO Rate", 0.05, 40.0, 2.4, self._accent,
            "0.05", "Hz", "40",
            format_hz, parse_hz, mode="log"
        )
        self.c_lfo_depth = KnobParameterCell(
            "LFO Depth", 0.0, 100.0, 35.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        for c in (self.c_carrier, self.c_lfo_rate, self.c_lfo_depth):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_warp = KnobParameterCell(
            "Warp / FM", 0.0, 100.0, 28.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        self.c_spread = KnobParameterCell(
            "Spread", 0.0, 100.0, 45.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 65.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        for c in (self.c_warp, self.c_spread, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_carrier.valueChanged.connect(lambda v: self._set_field("carrier", round(v, 1)))
        self.c_lfo_rate.valueChanged.connect(lambda v: self._set_field("lfo_rate", round(v, 2)))
        self.c_lfo_depth.valueChanged.connect(lambda v: self._set_field("lfo_depth", round(v, 0)))
        self.c_warp.valueChanged.connect(lambda v: self._set_field("warp", round(v, 0)))
        self.c_spread.valueChanged.connect(lambda v: self._set_field("spread", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        self._apply_view_state()
        self.mode_combo.setCurrentText(self._data["mode"])

        self.c_carrier.setValue(self._data["carrier"])
        self.c_lfo_rate.setValue(self._data["lfo_rate"])
        self.c_lfo_depth.setValue(self._data["lfo_depth"])
        self.c_warp.setValue(self._data["warp"])
        self.c_spread.setValue(self._data["spread"])
        self.c_mix.setValue(self._data["mix"])

        self._updating = False

    def setMasterBypassed(self, bypassed: bool):
        """全局旁通的纯视觉置灰：只改观感，不改 self._data["enabled"] 状态树"""
        if self._master_bypassed != bypassed:
            self._master_bypassed = bypassed
            self._apply_view_state()

    def _apply_view_state(self):
        en = bool(self._data["enabled"]) and not self._master_bypassed
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

        self.power_sw.setChecked(bool(self._data["enabled"]))
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        for cell in (self.c_carrier, self.c_lfo_rate, self.c_lfo_depth, self.c_warp, self.c_spread, self.c_mix):
            cell.setModuleActive(en)

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_mode(self, mode: str):
        self._data["mode"] = mode
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 7. 模块卡片 2：Granular Card（颗粒处理器：幽灵 / 破碎 / 漂浮 / 碎片化）
# =====================================================================
class GranularCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["grain"]
        self._updating = False
        self._master_bypassed = False

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

        self.title_lbl = QLabel("Granular")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        # FREEZE 实时缓冲区冻结开关（声音设计核心氛围按钮）
        self.freeze_btn = QPushButton("FREEZE")
        self.freeze_btn.setCheckable(True)
        self.freeze_btn.setFixedSize(54, 21)
        self.freeze_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.freeze_btn.setToolTip("冻结当前颗粒音频缓冲池，生成无限悬浮氛围音景")
        self.freeze_btn.clicked.connect(self._on_freeze)
        h_lay.addWidget(self.freeze_btn)

        self.mode_combo = QComboBox()
        self.mode_combo.setFixedWidth(82)
        self.mode_combo.addItems(["Cloud", "Reverse", "Scatter"])
        self.mode_combo.currentTextChanged.connect(self._on_mode)
        h_lay.addWidget(self.mode_combo)
        root.addWidget(header)

        # Body (2x4 旋钮矩阵)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_size = KnobParameterCell(
            "Size", 5.0, 500.0, 85.0, self._accent,
            "5", "ms", "500",
            format_ms, parse_float, mode="log"
        )
        self.c_density = KnobParameterCell(
            "Density", 1.0, 80.0, 24.0, self._accent,
            "1", "Hz", "80",
            lambda v: f"{v:.1f}Hz", parse_hz, mode="log"
        )
        self.c_pitch = KnobParameterCell(
            "Pitch", -24.0, 24.0, 7.0, self._accent,
            "-24", "st", "+24",
            lambda v: f"{v:+.1f}st" if abs(v) >= 0.05 else "0.0st", parse_float, mode="bipolar"
        )
        self.c_jitter = KnobParameterCell(
            "Jitter", 0.0, 100.0, 22.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        for c in (self.c_size, self.c_density, self.c_pitch, self.c_jitter):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_position = KnobParameterCell(
            "Position", 0.0, 100.0, 30.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        self.c_spray = KnobParameterCell(
            "Spray", 0.0, 100.0, 45.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        self.c_feedback = KnobParameterCell(
            "Feedback", 0.0, 95.0, 38.0, self._accent,
            "0", "%", "95",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 70.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        for c in (self.c_position, self.c_spray, self.c_feedback, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_size.valueChanged.connect(lambda v: self._set_field("size", round(v, 1)))
        self.c_density.valueChanged.connect(lambda v: self._set_field("density", round(v, 1)))
        self.c_pitch.valueChanged.connect(lambda v: self._set_field("pitch", round(v, 1)))
        self.c_jitter.valueChanged.connect(lambda v: self._set_field("pitch_jitter", round(v, 0)))
        self.c_position.valueChanged.connect(lambda v: self._set_field("position", round(v, 0)))
        self.c_spray.valueChanged.connect(lambda v: self._set_field("spray", round(v, 0)))
        self.c_feedback.valueChanged.connect(lambda v: self._set_field("feedback", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        self._apply_view_state()
        self.freeze_btn.setChecked(bool(self._data["freeze"]))
        self.freeze_btn.setStyleSheet("""
            QPushButton {
                background-color: #16181E;
                color: #7E8392;
                border: 1px solid #2E323E;
                border-radius: 4px;
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 0.4px;
            }
            QPushButton:hover { color: #E2E5EE; border-color: #464B5C; }
            QPushButton:checked {
                background-color: #2A1D3D;
                color: #D8B4FE;
                border: 1px solid #B67CF8;
            }
        """)
        self.mode_combo.setCurrentText(self._data["mode"])

        self.c_size.setValue(self._data["size"])
        self.c_density.setValue(self._data["density"])
        self.c_pitch.setValue(self._data["pitch"])
        self.c_jitter.setValue(self._data["pitch_jitter"])
        self.c_position.setValue(self._data["position"])
        self.c_spray.setValue(self._data["spray"])
        self.c_feedback.setValue(self._data["feedback"])
        self.c_mix.setValue(self._data["mix"])

        self._updating = False

    def setMasterBypassed(self, bypassed: bool):
        """全局旁通的纯视觉置灰：只改观感，不改 self._data["enabled"] 状态树"""
        if self._master_bypassed != bypassed:
            self._master_bypassed = bypassed
            self._apply_view_state()

    def _apply_view_state(self):
        en = bool(self._data["enabled"]) and not self._master_bypassed
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

        self.power_sw.setChecked(bool(self._data["enabled"]))
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        for cell in (
            self.c_size, self.c_density, self.c_pitch, self.c_jitter,
            self.c_position, self.c_spray, self.c_feedback, self.c_mix
        ):
            cell.setModuleActive(en)

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_freeze(self, checked: bool):
        self._data["freeze"] = checked
        self._emit()

    def _on_mode(self, mode: str):
        self._data["mode"] = mode
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 8. 模块卡片 3：Saturator / Distortion Card（饱和 / 失真：沙哑 / 破音 / 粗糙质感）
# =====================================================================
class DistortionCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["dist"]
        self._updating = False
        self._master_bypassed = False

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

        self.title_lbl = QLabel("Saturator / Dist")
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        self.curve_selector = ShaperCurveSelector(
            ["Tape", "Fold", "Hard Clip", "BitCrush"], self._data["curve"], self._accent
        )
        self.curve_selector.curveChanged.connect(self._on_curve)
        h_lay.addWidget(self.curve_selector)
        root.addWidget(header)

        # Body (2x3 旋钮矩阵)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_drive = KnobParameterCell(
            "Drive", 0.0, 36.0, 14.5, self._accent,
            "0.0", "dB", "+36",
            lambda v: f"+{v:.1f}dB" if v >= 0.05 else "0.0dB", parse_float, mode="linear"
        )
        self.c_fold = KnobParameterCell(
            "Fold / Crush", 0.0, 100.0, 42.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        self.c_bias = KnobParameterCell(
            "Bias", -100.0, 100.0, 12.0, self._accent,
            "-100", "%", "+100",
            lambda v: f"{int(round(v)):+d}%" if abs(v) >= 0.5 else "0%", parse_float, mode="bipolar"
        )
        for c in (self.c_drive, self.c_fold, self.c_bias):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_tone = KnobParameterCell(
            "Tone", 200.0, 16000.0, 5200.0, self._accent,
            "200", "Hz", "16K",
            format_hz, parse_hz, mode="log"
        )
        self.c_res = KnobParameterCell(
            "Resonance", 0.0, 100.0, 25.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 80.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", parse_float, mode="linear"
        )
        for c in (self.c_tone, self.c_res, self.c_mix):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_drive.valueChanged.connect(lambda v: self._set_field("drive", round(v, 1)))
        self.c_fold.valueChanged.connect(lambda v: self._set_field("fold", round(v, 0)))
        self.c_bias.valueChanged.connect(lambda v: self._set_field("bias", round(v, 0)))
        self.c_tone.valueChanged.connect(lambda v: self._set_field("tone", round(v, 0)))
        self.c_res.valueChanged.connect(lambda v: self._set_field("resonance", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        self._apply_view_state()
        self.curve_selector.setCurrentCurve(self._data["curve"])

        self.c_drive.setValue(self._data["drive"])
        self.c_fold.setValue(self._data["fold"])
        self.c_bias.setValue(self._data["bias"])
        self.c_tone.setValue(self._data["tone"])
        self.c_res.setValue(self._data["resonance"])
        self.c_mix.setValue(self._data["mix"])

        self._updating = False

    def setMasterBypassed(self, bypassed: bool):
        """全局旁通的纯视觉置灰：只改观感，不改 self._data["enabled"] 状态树"""
        if self._master_bypassed != bypassed:
            self._master_bypassed = bypassed
            self._apply_view_state()

    def _apply_view_state(self):
        en = bool(self._data["enabled"]) and not self._master_bypassed
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

        self.power_sw.setChecked(bool(self._data["enabled"]))
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )
        self.curve_selector.setModuleActive(en)

        for cell in (self.c_drive, self.c_fold, self.c_bias, self.c_tone, self.c_res, self.c_mix):
            cell.setModuleActive(en)

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_curve(self, curve: str):
        self._data["curve"] = curve
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 9. 主面板：奇幻畸变器 (Spectral Mangler — Creative Sci-Fi Distortion UI)
# =====================================================================
class StudioSpectralManglerWidget(QWidget):
    # 对接后端音频引擎的完整信号接口
    bypassChanged = pyqtSignal(bool)
    routingChanged = pyqtSignal(str)
    ringModChanged = pyqtSignal(dict)
    granularChanged = pyqtSignal(dict)
    distortionChanged = pyqtSignal(dict)
    globalMixChanged = pyqtSignal(float)
    outputGainChanged = pyqtSignal(float)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Audio 1", parent=None,
                 demo: bool = False):
        super().__init__(parent)
        self._demo_mode = bool(demo)
        self.setWindowTitle(f"Spectral Mangler — {track_name}")
        self.resize(1000, 690)
        self.setMinimumSize(920, 620)

        self._active_bank = "A"
        self._banks = {
            "A": default_mangler_demo(),
            "B": default_mangler_init(),
        }

        self._presets = {
            # =========================================================
            # 1. 基础初始化与日常音乐制作染色 (Init & Subtle Production)
            # =========================================================
            "Alien Transmission": copy.deepcopy(self._banks["A"]),
            "Init Clean": copy.deepcopy(self._banks["B"]),
            "Warm Analog Tape": {
                "enabled": True,
                "routing": "Grain → Ring → Dist",
                "global_mix": 65.0,
                "output_gain": -0.5,
                "ring": {"enabled": False, "mode": "Sine", "carrier": 120.0, "lfo_rate": 0.5, "lfo_depth": 10.0, "warp": 5.0, "spread": 25.0, "mix": 15.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Cloud", "size": 140.0, "density": 16.0, "pitch": 0.0, "pitch_jitter": 8.0, "position": 20.0, "spray": 12.0, "feedback": 10.0, "mix": 28.0},
                "dist": {"enabled": True, "curve": "Tape", "drive": 8.5, "fold": 15.0, "bias": 14.0, "tone": 6800.0, "resonance": 12.0, "mix": 75.0},
            },
            "Shimmer Octaves": {
                "enabled": True,
                "routing": "Parallel Blend",
                "global_mix": 72.0,
                "output_gain": -2.0,
                "ring": {"enabled": False, "mode": "Dual", "carrier": 220.0, "lfo_rate": 0.4, "lfo_depth": 15.0, "warp": 10.0, "spread": 60.0, "mix": 20.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Cloud", "size": 250.0, "density": 32.0, "pitch": 12.0, "pitch_jitter": 16.0, "position": 45.0, "spray": 55.0, "feedback": 64.0, "mix": 88.0},
                "dist": {"enabled": True, "curve": "Tape", "drive": 6.0, "fold": 12.0, "bias": 6.0, "tone": 8200.0, "resonance": 18.0, "mix": 45.0},
            },
            "Stereo Micro-Doubler": {
                "enabled": True,
                "routing": "Ring → Grain → Dist",
                "global_mix": 55.0,
                "output_gain": 0.0,
                "ring": {"enabled": True, "mode": "Sine", "carrier": 11.5, "lfo_rate": 0.8, "lfo_depth": 20.0, "warp": 8.0, "spread": 100.0, "mix": 18.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Cloud", "size": 34.0, "density": 22.0, "pitch": 0.0, "pitch_jitter": 14.0, "position": 15.0, "spray": 18.0, "feedback": 0.0, "mix": 65.0},
                "dist": {"enabled": False, "curve": "Tape", "drive": 4.0, "fold": 10.0, "bias": 5.0, "tone": 9500.0, "resonance": 10.0, "mix": 40.0},
            },
            "Lo-Fi Vinyl & Dust": {
                "enabled": True,
                "routing": "Grain → Ring → Dist",
                "global_mix": 80.0,
                "output_gain": -1.0,
                "ring": {"enabled": False, "mode": "Sine", "carrier": 60.0, "lfo_rate": 1.8, "lfo_depth": 25.0, "warp": 10.0, "spread": 30.0, "mix": 20.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Cloud", "size": 52.0, "density": 14.0, "pitch": 0.0, "pitch_jitter": 20.0, "position": 25.0, "spray": 22.0, "feedback": 15.0, "mix": 42.0},
                "dist": {"enabled": True, "curve": "BitCrush", "drive": 9.0, "fold": 24.0, "bias": 18.0, "tone": 2650.0, "resonance": 22.0, "mix": 72.0},
            },

            # =========================================================
            # 2. 科幻、机械人声与生物低吼 (Sci-Fi, Mech Vocals & Growls)
            # =========================================================
            "Cybernetic Robot Voice": {
                "enabled": True,
                "routing": "Ring → Grain → Dist",
                "global_mix": 92.0,
                "output_gain": -2.5,
                "ring": {"enabled": True, "mode": "Rectify", "carrier": 42.0, "lfo_rate": 0.25, "lfo_depth": 12.0, "warp": 26.0, "spread": 25.0, "mix": 95.0},
                "grain": {"enabled": False, "freeze": False, "mode": "Cloud", "size": 40.0, "density": 20.0, "pitch": 0.0, "pitch_jitter": 5.0, "position": 20.0, "spray": 10.0, "feedback": 10.0, "mix": 30.0},
                "dist": {"enabled": True, "curve": "Hard Clip", "drive": 16.5, "fold": 36.0, "bias": 10.0, "tone": 3100.0, "resonance": 48.0, "mix": 78.0},
            },
            "AI Neural Glitch": {
                "enabled": True,
                "routing": "Dist → Grain → Ring",
                "global_mix": 90.0,
                "output_gain": -3.5,
                "ring": {"enabled": True, "mode": "FM-Ring", "carrier": 920.0, "lfo_rate": 18.5, "lfo_depth": 72.0, "warp": 62.0, "spread": 75.0, "mix": 68.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Scatter", "size": 18.0, "density": 64.0, "pitch": 12.0, "pitch_jitter": 76.0, "position": 35.0, "spray": 85.0, "feedback": 42.0, "mix": 78.0},
                "dist": {"enabled": True, "curve": "BitCrush", "drive": 20.0, "fold": 72.0, "bias": -15.0, "tone": 5800.0, "resonance": 38.0, "mix": 86.0},
            },
            "Walkie-Talkie Interception": {
                "enabled": True,
                "routing": "Ring → Grain → Dist",
                "global_mix": 95.0,
                "output_gain": -2.0,
                "ring": {"enabled": True, "mode": "Sine", "carrier": 1850.0, "lfo_rate": 7.5, "lfo_depth": 35.0, "warp": 18.0, "spread": 15.0, "mix": 28.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Scatter", "size": 28.0, "density": 14.0, "pitch": 0.0, "pitch_jitter": 16.0, "position": 30.0, "spray": 35.0, "feedback": 12.0, "mix": 36.0},
                "dist": {"enabled": True, "curve": "Hard Clip", "drive": 22.5, "fold": 56.0, "bias": 22.0, "tone": 2150.0, "resonance": 68.0, "mix": 95.0},
            },
            "Sub-Bass Titan Growl": {
                "enabled": True,
                "routing": "Ring → Grain → Dist",
                "global_mix": 94.0,
                "output_gain": -4.0,
                "ring": {"enabled": True, "mode": "FM-Ring", "carrier": 55.0, "lfo_rate": 3.4, "lfo_depth": 48.0, "warp": 46.0, "spread": 40.0, "mix": 54.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Cloud", "size": 110.0, "density": 26.0, "pitch": -12.0, "pitch_jitter": 9.0, "position": 22.0, "spray": 24.0, "feedback": 32.0, "mix": 76.0},
                "dist": {"enabled": True, "curve": "Fold", "drive": 24.5, "fold": 68.0, "bias": -20.0, "tone": 680.0, "resonance": 58.0, "mix": 92.0},
            },

            # =========================================================
            # 3. 电影配乐、深空氛围与悬疑恐怖 (Cinematic, Ambient & Horror)
            # =========================================================
            "Deep Space Frozen Drone": {
                "enabled": True,
                "routing": "Grain → Ring → Dist",
                "global_mix": 90.0,
                "output_gain": -2.0,
                "ring": {"enabled": True, "mode": "Dual", "carrier": 62.0, "lfo_rate": 0.22, "lfo_depth": 28.0, "warp": 20.0, "spread": 90.0, "mix": 48.0},
                "grain": {"enabled": True, "freeze": True, "mode": "Cloud", "size": 360.0, "density": 20.0, "pitch": -5.0, "pitch_jitter": 14.0, "position": 48.0, "spray": 55.0, "feedback": 76.0, "mix": 92.0},
                "dist": {"enabled": True, "curve": "Tape", "drive": 12.0, "fold": 25.0, "bias": 10.0, "tone": 2100.0, "resonance": 32.0, "mix": 70.0},
            },
            "Ghostly Shattered Memory": {
                "enabled": True,
                "routing": "Grain → Ring → Dist",
                "global_mix": 78.0,
                "output_gain": -1.0,
                "ring": {"enabled": True, "mode": "Sine", "carrier": 148.0, "lfo_rate": 0.65, "lfo_depth": 45.0, "warp": 12.0, "spread": 75.0, "mix": 35.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Reverse", "size": 190.0, "density": 18.0, "pitch": 12.0, "pitch_jitter": 34.0, "position": 55.0, "spray": 72.0, "feedback": 58.0, "mix": 82.0},
                "dist": {"enabled": True, "curve": "Tape", "drive": 9.5, "fold": 18.0, "bias": 8.0, "tone": 3800.0, "resonance": 18.0, "mix": 55.0},
            },
            "Haunted Reverse Whisper": {
                "enabled": True,
                "routing": "Grain → Ring → Dist",
                "global_mix": 86.0,
                "output_gain": -1.5,
                "ring": {"enabled": True, "mode": "Dual", "carrier": 195.0, "lfo_rate": 0.35, "lfo_depth": 42.0, "warp": 22.0, "spread": 88.0, "mix": 44.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Reverse", "size": 270.0, "density": 16.0, "pitch": 5.0, "pitch_jitter": 26.0, "position": 62.0, "spray": 80.0, "feedback": 66.0, "mix": 90.0},
                "dist": {"enabled": True, "curve": "Tape", "drive": 11.5, "fold": 20.0, "bias": -12.0, "tone": 3350.0, "resonance": 28.0, "mix": 52.0},
            },
            "Cinematic Tension Riser": {
                "enabled": True,
                "routing": "Ring → Grain → Dist",
                "global_mix": 88.0,
                "output_gain": -3.0,
                "ring": {"enabled": True, "mode": "FM-Ring", "carrier": 440.0, "lfo_rate": 0.15, "lfo_depth": 90.0, "warp": 65.0, "spread": 82.0, "mix": 62.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Cloud", "size": 155.0, "density": 36.0, "pitch": 19.0, "pitch_jitter": 30.0, "position": 45.0, "spray": 60.0, "feedback": 78.0, "mix": 82.0},
                "dist": {"enabled": True, "curve": "Tape", "drive": 15.0, "fold": 35.0, "bias": 14.0, "tone": 6900.0, "resonance": 42.0, "mix": 72.0},
            },
            "Abyssal Sonar Drone": {
                "enabled": True,
                "routing": "Grain → Ring → Dist",
                "global_mix": 90.0,
                "output_gain": -2.5,
                "ring": {"enabled": True, "mode": "Sine", "carrier": 82.0, "lfo_rate": 0.4, "lfo_depth": 36.0, "warp": 18.0, "spread": 92.0, "mix": 56.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Cloud", "size": 320.0, "density": 14.0, "pitch": -12.0, "pitch_jitter": 10.0, "position": 40.0, "spray": 42.0, "feedback": 72.0, "mix": 86.0},
                "dist": {"enabled": True, "curve": "Tape", "drive": 14.0, "fold": 28.0, "bias": 8.0, "tone": 820.0, "resonance": 62.0, "mix": 80.0},
            },

            # =========================================================
            # 4. 故障、打击乐破坏与极端波形折叠 (Glitch, Industrial & Extreme)
            # =========================================================
            "Cybernetic Meltdown": {
                "enabled": True,
                "routing": "Ring → Grain → Dist",
                "global_mix": 95.0,
                "output_gain": -4.5,
                "ring": {"enabled": True, "mode": "FM-Ring", "carrier": 780.0, "lfo_rate": 11.5, "lfo_depth": 65.0, "warp": 74.0, "spread": 60.0, "mix": 85.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Scatter", "size": 24.0, "density": 56.0, "pitch": -12.0, "pitch_jitter": 48.0, "position": 18.0, "spray": 80.0, "feedback": 45.0, "mix": 65.0},
                "dist": {"enabled": True, "curve": "Fold", "drive": 26.0, "fold": 78.0, "bias": -28.0, "tone": 7400.0, "resonance": 54.0, "mix": 92.0},
            },
            "Corrupted Telemetry": {
                "enabled": True,
                "routing": "Dist → Grain → Ring",
                "global_mix": 88.0,
                "output_gain": -3.0,
                "ring": {"enabled": True, "mode": "Dual", "carrier": 1320.0, "lfo_rate": 6.2, "lfo_depth": 50.0, "warp": 40.0, "spread": 80.0, "mix": 60.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Scatter", "size": 38.0, "density": 40.0, "pitch": 5.0, "pitch_jitter": 60.0, "position": 40.0, "spray": 65.0, "feedback": 30.0, "mix": 68.0},
                "dist": {"enabled": True, "curve": "BitCrush", "drive": 18.0, "fold": 68.0, "bias": 15.0, "tone": 4400.0, "resonance": 42.0, "mix": 90.0},
            },
            "8-Bit Arcade Crusher": {
                "enabled": True,
                "routing": "Ring → Grain → Dist",
                "global_mix": 92.0,
                "output_gain": -3.5,
                "ring": {"enabled": True, "mode": "Rectify", "carrier": 260.0, "lfo_rate": 8.0, "lfo_depth": 40.0, "warp": 30.0, "spread": 50.0, "mix": 46.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Scatter", "size": 22.0, "density": 48.0, "pitch": 12.0, "pitch_jitter": 42.0, "position": 28.0, "spray": 50.0, "feedback": 24.0, "mix": 56.0},
                "dist": {"enabled": True, "curve": "BitCrush", "drive": 19.5, "fold": 84.0, "bias": 12.0, "tone": 8500.0, "resonance": 35.0, "mix": 95.0},
            },
            "Industrial Metal Clang": {
                "enabled": True,
                "routing": "Ring → Grain → Dist",
                "global_mix": 90.0,
                "output_gain": -4.0,
                "ring": {"enabled": True, "mode": "Dual", "carrier": 468.0, "lfo_rate": 3.2, "lfo_depth": 26.0, "warp": 68.0, "spread": 70.0, "mix": 90.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Cloud", "size": 35.0, "density": 42.0, "pitch": 7.0, "pitch_jitter": 20.0, "position": 20.0, "spray": 30.0, "feedback": 52.0, "mix": 60.0},
                "dist": {"enabled": True, "curve": "Hard Clip", "drive": 23.0, "fold": 60.0, "bias": -16.0, "tone": 6200.0, "resonance": 55.0, "mix": 88.0},
            },
            "West-Coast Buchla Fold": {
                "enabled": True,
                "routing": "Parallel Blend",
                "global_mix": 92.0,
                "output_gain": -4.0,
                "ring": {"enabled": True, "mode": "FM-Ring", "carrier": 220.0, "lfo_rate": 4.5, "lfo_depth": 55.0, "warp": 82.0, "spread": 65.0, "mix": 76.0},
                "grain": {"enabled": False, "freeze": False, "mode": "Cloud", "size": 80.0, "density": 24.0, "pitch": 0.0, "pitch_jitter": 10.0, "position": 30.0, "spray": 25.0, "feedback": 20.0, "mix": 40.0},
                "dist": {"enabled": True, "curve": "Fold", "drive": 25.0, "fold": 88.0, "bias": -18.0, "tone": 4800.0, "resonance": 64.0, "mix": 92.0},
            },
            "Time-Stutter Repeater": {
                "enabled": True,
                "routing": "Grain → Ring → Dist",
                "global_mix": 88.0,
                "output_gain": -2.5,
                "ring": {"enabled": False, "mode": "Sine", "carrier": 300.0, "lfo_rate": 2.0, "lfo_depth": 20.0, "warp": 15.0, "spread": 40.0, "mix": 30.0},
                "grain": {"enabled": True, "freeze": False, "mode": "Cloud", "size": 16.0, "density": 68.0, "pitch": 0.0, "pitch_jitter": 6.0, "position": 25.0, "spray": 8.0, "feedback": 82.0, "mix": 92.0},
                "dist": {"enabled": True, "curve": "Hard Clip", "drive": 10.5, "fold": 25.0, "bias": 6.0, "tone": 7200.0, "resonance": 22.0, "mix": 58.0},
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # NF-02（计划 §6.2-1/2）：demo=False 不启动模拟信号定时器；
        # 画布真实波形/颗粒由 setTelemetry 注入（NF-04 接线）。
        self._demo_phase = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_animation)
        if self._demo_mode:
            self._demo_timer.start(40)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet("""
            QWidget {
                background-color: #17191E;
                color: #E2E5EE;
                font-family: 'SF Pro Display', 'Segoe UI', 'PingFang SC', 'Microsoft YaHei';
            }
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ShaperCurveSelector, ProManglerVisualizerWidget, ManglerMasterStrip {
                background-color: transparent;
            }
            QComboBox {
                background-color: #111317;
                color: #E4E7F0;
                border: 1px solid #2E323E;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
                min-height: 21px;
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
        # 1. 顶部整合工具栏（电源 + 标题 + 预设 + A/B 对比 + 信号路由拓扑 + 重置）
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

        title_lbl = QLabel(f"SPECTRAL MANGLER  ·  {track_name}")
        title_lbl.setStyleSheet("color: #EAEDF6; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(10)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(23, 23)
        add_preset_btn.setToolTip("保存为新声音设计预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedWidth(206)
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

        tb_lay.addSpacing(8)

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

        # 处理链顺序拓扑 (Routing Chain)
        r_lbl = QLabel("Routing")
        r_lbl.setStyleSheet("color: #8E94A4; font-size: 11px; font-weight: 600;")
        tb_lay.addWidget(r_lbl)

        self.routing_combo = QComboBox()
        self.routing_combo.setFixedWidth(142)
        self.routing_combo.addItems(ROUTING_MODES)
        self.routing_combo.currentTextChanged.connect(self._on_routing_changed)
        tb_lay.addWidget(self.routing_combo)

        # 随机突变灵感按钮
        mutate_btn = QPushButton("Mutate")
        mutate_btn.setObjectName("ToolBtn")
        mutate_btn.setFixedSize(54, 23)
        mutate_btn.setToolTip("随机生成奇幻畸变参数组合（保持安全输出电平）")
        mutate_btn.clicked.connect(self.mutate_mangler)
        tb_lay.addWidget(mutate_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 23)
        reset_btn.clicked.connect(self.reset_mangler)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部主图形区：颗粒与调制折叠交互画布 + 右侧 Mix / Out 推子条
        # -------------------------------------------------------------
        graph_row = QHBoxLayout()
        graph_row.setContentsMargins(10, 10, 14, 6)
        graph_row.setSpacing(6)

        self.visualizer = ProManglerVisualizerWidget()
        self.visualizer.ringNodeDragged.connect(self._on_vis_ring_dragged)
        self.visualizer.ringNodeScrolled.connect(self._on_vis_ring_scrolled)
        self.visualizer.grainNodeDragged.connect(self._on_vis_grain_dragged)
        self.visualizer.grainNodeScrolled.connect(self._on_vis_grain_scrolled)
        self.visualizer.distNodeDragged.connect(self._on_vis_dist_dragged)
        self.visualizer.distNodeScrolled.connect(self._on_vis_dist_scrolled)
        self.visualizer.moduleToggleRequested.connect(self._on_vis_module_toggle)
        graph_row.addWidget(self.visualizer, 1)

        self.master_strip = ManglerMasterStrip()
        self.master_strip.mixChanged.connect(self._on_global_mix_changed)
        self.master_strip.gainChanged.connect(self._on_output_gain_changed)
        graph_row.addWidget(self.master_strip)

        main_lay.addLayout(graph_row, 5)

        # -------------------------------------------------------------
        # 3. 底部三核心奇幻畸变卡片矩阵 (Ring Mod | Granular | Saturator/Dist)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(14, 4, 14, 14)
        cards_row.setSpacing(9)

        st = self._current_state()
        self.ring_card = RingModulatorCard(st["ring"])
        self.ring_card.changed.connect(self._on_ring_changed)

        self.grain_card = GranularCard(st["grain"])
        self.grain_card.changed.connect(self._on_grain_changed)

        self.dist_card = DistortionCard(st["dist"])
        self.dist_card.changed.connect(self._on_dist_changed)

        # 3 : 4 : 3 黄金比例分布（10 列旋钮等宽对齐）
        cards_row.addWidget(self.ring_card, 3)
        cards_row.addWidget(self.grain_card, 4)
        cards_row.addWidget(self.dist_card, 3)

        main_lay.addLayout(cards_row, 4)

    # -----------------------------------------------------------------
    # 状态管理与双向联动逻辑
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
        master_en = bool(st["enabled"])
        self._update_ab_styles()
        self.power_switch.setChecked(master_en)
        self.routing_combo.blockSignals(True)
        self.routing_combo.setCurrentText(st["routing"])
        self.routing_combo.blockSignals(False)

        self.master_strip.setMix(st["global_mix"])
        self.master_strip.setGain(st["output_gain"])

        # 先同步真实参数，再做全局旁通纯视觉置灰（不改各自 enabled 状态树）
        self.ring_card.syncFromData(st["ring"])
        self.grain_card.syncFromData(st["grain"])
        self.dist_card.syncFromData(st["dist"])
        for card in (self.ring_card, self.grain_card, self.dist_card):
            card.setMasterBypassed(not master_en)

        self.visualizer.setState(st)

    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _copy_bank(self):
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._banks[self._active_bank])
        self.btn_copy_ab.setText("✓")
        QTimer.singleShot(650, self._update_ab_styles)

    def mutate_mangler(self):
        """声音设计灵感随机生成器：自动启用模块并生成可控的奇幻参数"""
        st = self._current_state()
        st["enabled"] = True
        st["routing"] = random.choice(ROUTING_MODES)

        st["ring"].update({
            "enabled": True,
            "mode": random.choice(["Sine", "Dual", "FM-Ring", "Rectify"]),
            "carrier": round(10.0 * (500.0 ** random.random()), 1),
            "lfo_rate": round(0.1 * (250.0 ** random.random()), 2),
            "lfo_depth": random.randint(15, 85),
            "warp": random.randint(10, 85),
            "spread": random.randint(20, 95),
            "mix": random.randint(35, 85),
        })
        st["grain"].update({
            "enabled": True,
            "freeze": False,
            "mode": random.choice(["Cloud", "Reverse", "Scatter"]),
            "size": round(12.0 * (25.0 ** random.random()), 1),
            "density": round(random.uniform(8.0, 62.0), 1),
            "pitch": random.choice([-12.0, -7.0, -5.0, 0.0, 5.0, 7.0, 12.0, 19.0]),
            "pitch_jitter": random.randint(5, 65),
            "position": random.randint(10, 90),
            "spray": random.randint(20, 85),
            "feedback": random.randint(15, 68),
            "mix": random.randint(45, 90),
        })
        st["dist"].update({
            "enabled": True,
            "curve": random.choice(["Tape", "Fold", "Hard Clip", "BitCrush"]),
            "drive": round(random.uniform(6.0, 26.0), 1),
            "fold": random.randint(15, 82),
            "bias": random.randint(-45, 45),
            "tone": round(600.0 * (20.0 ** random.random()), 0),
            "resonance": random.randint(10, 60),
            "mix": random.randint(50, 95),
        })
        st["output_gain"] = -3.0  # 自动补偿输出增益防止过载
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def _on_power_toggled(self, enabled: bool):
        st = self._current_state()
        st["enabled"] = enabled
        self._refresh_all_views()  # 全量刷新，同步更新卡片视觉态
        self.bypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_routing_changed(self, routing: str):
        st = self._current_state()
        st["routing"] = routing
        self.visualizer.setState(st)
        self.routingChanged.emit(routing)
        self.stateChanged.emit(self.get_state())

    def _on_global_mix_changed(self, mix: float):
        st = self._current_state()
        st["global_mix"] = mix
        self.globalMixChanged.emit(mix)
        self.stateChanged.emit(self.get_state())

    def _on_output_gain_changed(self, gain: float):
        st = self._current_state()
        st["output_gain"] = gain
        self.outputGainChanged.emit(gain)
        self.stateChanged.emit(self.get_state())

    def _on_ring_changed(self, ring_data: dict):
        st = self._current_state()
        st["ring"] = ring_data
        self.visualizer.setState(st)
        self.ringModChanged.emit(copy.deepcopy(ring_data))
        self.stateChanged.emit(self.get_state())

    def _on_grain_changed(self, grain_data: dict):
        st = self._current_state()
        st["grain"] = grain_data
        self.visualizer.setState(st)
        self.granularChanged.emit(copy.deepcopy(grain_data))
        self.stateChanged.emit(self.get_state())

    def _on_dist_changed(self, dist_data: dict):
        st = self._current_state()
        st["dist"] = dist_data
        self.visualizer.setState(st)
        self.distortionChanged.emit(copy.deepcopy(dist_data))
        self.stateChanged.emit(self.get_state())

    # -----------------------------------------------------------------
    # 画布节点直拖 / 滚轮 / 双击反向同步到卡片旋钮
    # -----------------------------------------------------------------
    def _on_vis_ring_dragged(self, carrier: float, mix: float):
        st = self._current_state()
        st["ring"]["carrier"] = carrier
        st["ring"]["mix"] = mix
        self.ring_card.syncFromData(st["ring"])
        self.visualizer.setState(st)
        self.ringModChanged.emit(copy.deepcopy(st["ring"]))
        self.stateChanged.emit(self.get_state())

    def _on_vis_ring_scrolled(self, warp: float):
        st = self._current_state()
        st["ring"]["warp"] = warp
        self.ring_card.syncFromData(st["ring"])
        self.visualizer.setState(st)
        self.ringModChanged.emit(copy.deepcopy(st["ring"]))
        self.stateChanged.emit(self.get_state())

    def _on_vis_grain_dragged(self, size: float, pitch: float):
        st = self._current_state()
        st["grain"]["size"] = size
        st["grain"]["pitch"] = pitch
        self.grain_card.syncFromData(st["grain"])
        self.visualizer.setState(st)
        self.granularChanged.emit(copy.deepcopy(st["grain"]))
        self.stateChanged.emit(self.get_state())

    def _on_vis_grain_scrolled(self, density: float):
        st = self._current_state()
        st["grain"]["density"] = density
        self.grain_card.syncFromData(st["grain"])
        self.visualizer.setState(st)
        self.granularChanged.emit(copy.deepcopy(st["grain"]))
        self.stateChanged.emit(self.get_state())

    def _on_vis_dist_dragged(self, fold: float, drive: float):
        st = self._current_state()
        st["dist"]["fold"] = fold
        st["dist"]["drive"] = drive
        self.dist_card.syncFromData(st["dist"])
        self.visualizer.setState(st)
        self.distortionChanged.emit(copy.deepcopy(st["dist"]))
        self.stateChanged.emit(self.get_state())

    def _on_vis_dist_scrolled(self, tone: float):
        st = self._current_state()
        st["dist"]["tone"] = tone
        self.dist_card.syncFromData(st["dist"])
        self.visualizer.setState(st)
        self.distortionChanged.emit(copy.deepcopy(st["dist"]))
        self.stateChanged.emit(self.get_state())

    def _on_vis_module_toggle(self, module_key: str):
        st = self._current_state()
        if module_key in ("ring", "grain", "dist"):
            st[module_key]["enabled"] = not st[module_key]["enabled"]
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    # -----------------------------------------------------------------
    # 预设管理与演示动画
    # -----------------------------------------------------------------
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
        name, ok = QInputDialog.getText(self, "保存声音设计预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    def _tick_demo_animation(self):
        self._demo_phase += 0.08
        st = self._current_state()
        self.visualizer.stepAnimation(0.04)

        in_db = -18.0 + 7.5 * math.sin(self._demo_phase * 1.2) + random.uniform(-1.5, 1.5)
        drive_boost = (st["dist"]["drive"] * 0.28) if (st["enabled"] and st["dist"]["enabled"]) else 0.0
        out_db = min(-0.5, in_db + drive_boost + st["output_gain"])
        self.master_strip.setLevels(in_db, out_db)

    # -----------------------------------------------------------------
    # 公开 API：供后端 DSP 引擎与工程序列化直接调用
    # -----------------------------------------------------------------
    def reset_mangler(self):
        self._banks[self._active_bank] = default_mangler_init()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_telemetry(
        self,
        input_db: float,
        output_db: float,
        wave_samples: list[float] | None = None,
        grains: list[dict] | None = None,
    ):
        """接入真实音频引擎实时电平、输出波形采样点及颗粒云坐标"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.master_strip.setLevels(input_db, output_db)
        self.visualizer.setTelemetry(wave_samples, grains)
