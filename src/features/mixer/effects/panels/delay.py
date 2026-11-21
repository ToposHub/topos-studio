from __future__ import annotations

import sys
import math
import copy
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont
)

from src.shared.ui.widgets.chevron_combo import ChevronComboBox


# =====================================================================
# 1. 全局色彩系统与默认参数配置（与 EQ-UI.py / Dyamics-UI.py 完全统一）
# =====================================================================
# 与 AudioClipEditor-UI / EQ-UI 统一的达芬奇 Fairlight 视觉系统：
# #27272B 哑光中性灰基底 + 1px #141416 纯黑细线切分，字体栈一致
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":   "#28D8F0",  # 主控 / A-B 激活 / Left 声道 / Output
    "filter": "#4CD490",  # Filter 模块主题色（翡翠绿）
    "right":  "#6495FA",  # Right 声道主题色（钴蓝）
    "fb":     "#F0A848",  # Feedback 矩阵模块主题色（琥珀金）
    "clip":   "#F26D6D",  # 削波警示（珊瑚红）
}


def default_delay_demo() -> dict:
    return {
        "enabled": True,
        "link_lr": False,
        "input_format": "Stereo",
        "high_cut": 12500.0,     # 100.0 ~ 20000.0 Hz (log)
        "low_cut": 108.0,        # 20.0 ~ 5000.0 Hz (log)
        "left_delay": 121.0,     # 0.0 ~ 1200.0 ms (power)
        "left_feedback": 16.0,   # -99.0 ~ +99.0 % (bipolar)
        "right_delay": 135.0,    # 0.0 ~ 1200.0 ms (power)
        "right_feedback": -7.0,  # -99.0 ~ +99.0 % (bipolar)
        "high_ratio": 60.0,      # 0.0 ~ 100.0 % (linear)
        "stereo_blend": 15.0,    # 0.0 ~ 100.0 % (linear)
        "dry_wet": 30.0,         # 0.0 ~ 100.0 % (linear)
        "level": 0.0,            # -24.0 ~ +6.0 dB (linear)
    }


def default_delay_clean() -> dict:
    st = default_delay_demo()
    st["left_delay"] = 250.0
    st["left_feedback"] = 25.0
    st["right_delay"] = 250.0
    st["right_feedback"] = 25.0
    st["high_ratio"] = 30.0
    st["stereo_blend"] = 0.0
    st["dry_wet"] = 25.0
    st["level"] = 0.0
    return st


def format_freq_hz(hz: float) -> str:
    if hz >= 1000.0:
        return f"{hz / 1000.0:.2f}k"
    return f"{int(round(hz))}"


def parse_freq_hz(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("HZ", "").strip()
    try:
        if s.endswith("K"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def parse_delay_ms(text: str, fallback: float) -> float:
    s = text.strip().lower().replace("ms", "").strip()
    try:
        if s.endswith("s"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def parse_float_clean(text: str, fallback: float) -> float:
    s = text.strip().replace("%", "").replace("dB", "").replace("db", "").strip()
    try:
        return float(s)
    except ValueError:
        return fallback


# =====================================================================
# 2. 控件：现代发光电源开关（与 EQ-UI.py / Dyamics-UI.py 完全一致）
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
# 3. 控件：内嵌可编辑数字胶囊（单击原地编辑，对齐 EQ / Dynamics 风格）
# =====================================================================
class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 72, parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._active = True
        self.setFixedSize(width, 22)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(1, 1, width - 2, 20)
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
            clean = self._text.replace("dB", "").replace("%", "").replace("ms", "").strip()
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
        p.drawRoundedRect(rect, 5.0, 5.0)

        p.setPen(QColor("#ECECF0") if self._active else QColor("#6E6E76"))
        font = QFont(MONO_FONT_FAM, 10)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


# =====================================================================
# 4. 控件：拉丝阳极铝质感旋钮（严格保持 36x36px 尺寸与交互手感）
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
        mode: str = "linear",         # "linear" | "log" | "power" | "bipolar"
        zero_marker_val: float | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._mode = mode
        self._zero_marker_val = zero_marker_val
        self._active = True
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(54, 54)
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

        arc_r = 23.0
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深色凹槽底环
        p.setPen(QPen(QColor("#141417"), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性模式（Feedback -99~+99）正上方 0% 基准刻度点
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 可选非对称零刻度点（如 Level 旋钮的 0.0 dB 位置）
        if self._zero_marker_val is not None:
            z_norm = self._val_to_norm(self._zero_marker_val)
            z_rad = math.radians(start_deg + z_norm * total_span)
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(
                QPointF(cx + math.cos(z_rad) * (arc_r - 1.2), cy - math.sin(z_rad) * (arc_r - 1.2)),
                QPointF(cx + math.cos(z_rad) * (arc_r + 2.2), cy - math.sin(z_rad) * (arc_r + 2.2)),
            )

        # 2. 模块主题色光环
        arc_col = self._accent if self._active else QColor("#55555C")
        p.setPen(QPen(arc_col, 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
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
        knob_r = 19.0
        p.setPen(QPen(QColor("#111114"), 1.4))
        cap_col = "#8E9096" if ((self._hovered or self._dragging) and self._active) else ("#7A7C82" if self._active else "#5A5A60")
        p.setBrush(QColor(cap_col))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 4. 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 5.0
        iy1 = cy - math.sin(rad) * 5.0
        ix2 = cx + math.cos(rad) * (knob_r - 2.5)
        iy2 = cy - math.sin(rad) * (knob_r - 2.5)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 5. 控件：统一旋钮参数单元（标签 + 36x36旋钮 + 刻度范围 + 可编辑胶囊）
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
        zero_marker_val: float | None = None,
        pill_width: int = 72,
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
        self.title_lbl.setStyleSheet("color: #9AA0B0; font-size: 11px; font-weight: 600;")
        lay.addWidget(self.title_lbl)

        self.knob = ProRotaryKnob(
            min_val, max_val, default_val, default_val,
            accent_color, mode=mode, zero_marker_val=zero_marker_val
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
        self._val = max(self._min, min(self._max, val))
        self.knob.setValue(self._val)
        self.pill.setText(self._formatter(self._val))
        self._updating = False

    def setModuleActive(self, active: bool):
        self.title_lbl.setStyleSheet(f"color: {'#9A9AA2' if active else '#6E6E76'}; font-size: 11px; font-weight: 600;")
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
# 6. 控件：交互式延迟脉冲示波器（包络曲线 + 相位反转 + 直接拖拽交互）
# =====================================================================
class ProDelayTapGraphWidget(QWidget):
    tapDragged = pyqtSignal(str, float, float)   # channel ("L"|"R"), delay_ms, feedback_pct
    highRatioScrolled = pyqtSignal(float)        # delta

    MAX_TIME_MS = 2000.0
    TIME_TICKS = [0, 250, 500, 750, 1000, 1250, 1500, 1750, 2000]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(230)
        self.setMouseTracking(True)

        self._state = default_delay_demo()
        self._show_envelope = True
        self._show_ghost = True

        self._hover_target = ""      # "L" | "R" | ""
        self._dragging_target = ""   # "L" | "R" | ""
        self._drag_start_delay = 0.0
        self._drag_start_fb = 0.0
        self._mouse_pos: QPointF | None = None

        self._btn_env_rect = QRectF()
        self._btn_ghost_rect = QRectF()

    def setState(self, state: dict):
        self._state = state
        self.update()

    def _lanes_geometry(self):
        ml, mr, mt, mb = 32.0, 14.0, 10.0, 10.0
        w = max(120.0, self.width() - ml - mr)
        h = max(120.0, self.height() - mt - mb)
        axis_h = 20.0
        lane_h = (h - axis_h) / 2.0

        l_rect = QRectF(ml, mt, w, lane_h)
        axis_rect = QRectF(ml, mt + lane_h, w, axis_h)
        r_rect = QRectF(ml, mt + lane_h + axis_h, w, lane_h)
        return l_rect, axis_rect, r_rect

    def _tap1_handle_pos(self, lane_rect: QRectF, delay_ms: float, feedback_pct: float) -> QPointF:
        x = lane_rect.left() + (max(0.0, min(self.MAX_TIME_MS, delay_ms)) / self.MAX_TIME_MS) * lane_rect.width()
        mid_y = lane_rect.center().y()
        # 句柄位于第一脉冲柱顶端
        top_y = mid_y - lane_rect.height() * 0.42
        return QPointF(x, top_y)

    def _hit_test_tap(self, pos: QPointF) -> str:
        if not self._state["enabled"]:
            return ""
        l_rect, _, r_rect = self._lanes_geometry()
        is_mono = (self._state["input_format"] == "Mono")

        if is_mono:
            full_rect = QRectF(l_rect.left(), l_rect.top(), l_rect.width(), r_rect.bottom() - l_rect.top())
            lx = full_rect.left() + (self._state["left_delay"] / self.MAX_TIME_MS) * full_rect.width()
            if full_rect.contains(pos) and abs(pos.x() - lx) <= 13.0:
                return "L"
            return ""

        lx = l_rect.left() + (self._state["left_delay"] / self.MAX_TIME_MS) * l_rect.width()
        if l_rect.contains(pos) and abs(pos.x() - lx) <= 13.0:
            return "L"

        rx = r_rect.left() + (self._state["right_delay"] / self.MAX_TIME_MS) * r_rect.width()
        if r_rect.contains(pos) and abs(pos.x() - rx) <= 13.0:
            return "R"

        return ""

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_target = ""
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_env_rect.contains(pos):
                self._show_envelope = not self._show_envelope
                self.update()
                return
            if self._btn_ghost_rect.contains(pos):
                self._show_ghost = not self._show_ghost
                self.update()
                return

            hit = self._hit_test_tap(pos)
            if hit:
                self._dragging_target = hit
                if hit == "L":
                    self._drag_start_delay = self._state["left_delay"]
                    self._drag_start_fb = self._state["left_feedback"]
                else:
                    self._drag_start_delay = self._state["right_delay"]
                    self._drag_start_fb = self._state["right_feedback"]
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        l_rect, _, r_rect = self._lanes_geometry()

        if self._dragging_target:
            mods = event.modifiers()
            target_rect = l_rect if self._dragging_target == "L" else r_rect
            if self._state["input_format"] == "Mono":
                target_rect = QRectF(l_rect.left(), l_rect.top(), l_rect.width(), r_rect.bottom() - l_rect.top())

            if mods & Qt.KeyboardModifier.ControlModifier:
                new_delay = self._drag_start_delay
            else:
                norm_x = max(0.0, min(1.0, (event.position().x() - target_rect.left()) / target_rect.width()))
                new_delay = round(min(1200.0, norm_x * self.MAX_TIME_MS), 1)

            if mods & Qt.KeyboardModifier.AltModifier:
                new_fb = self._drag_start_fb
            else:
                mid_y = target_rect.center().y()
                half_h = target_rect.height() * 0.42
                norm_y = max(-1.0, min(1.0, (mid_y - event.position().y()) / max(10.0, half_h)))
                new_fb = round(norm_y * 99.0, 1)

            self.tapDragged.emit(self._dragging_target, new_delay, new_fb)
            return

        hit = self._hit_test_tap(event.position())
        self._hover_target = hit
        self.setCursor(Qt.CursorShape.OpenHandCursor if hit else Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_target:
            self._dragging_target = ""
            self.update()

    def wheelEvent(self, event):
        if self._hover_target or self._dragging_target:
            delta = (event.angleDelta().y() / 120.0) * 4.0
            self.highRatioScrolled.emit(delta)
            event.accept()

    # -----------------------------------------------------------------
    # 绘制双泳道网格、包络曲线、正负相位脉冲、交叉串扰幽灵脉冲与 HUD
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        l_rect, axis_rect, r_rect = self._lanes_geometry()
        is_mono = (self._state["input_format"] == "Mono")
        enabled = self._state["enabled"]

        if is_mono:
            mono_rect = QRectF(l_rect.left(), l_rect.top(), l_rect.width(), r_rect.bottom() - l_rect.top() - 18.0)
            mono_axis = QRectF(l_rect.left(), mono_rect.bottom() + 2.0, l_rect.width(), 18.0)
            self._draw_lane_canvas(p, mono_rect, "M", COLORS["cyan"])
            self._draw_time_axis(p, mono_axis)
            self._draw_tap_cluster(
                p, mono_rect,
                delay_ms=self._state["left_delay"],
                fb_pct=self._state["left_feedback"],
                cross_delay_ms=None,
                cross_fb_pct=0.0,
                accent_hex=COLORS["cyan"],
                is_focus=(self._hover_target == "L" or self._dragging_target == "L"),
            )
            top_overlay_rect = mono_rect
        else:
            self._draw_lane_canvas(p, l_rect, "L", COLORS["cyan"])
            self._draw_time_axis(p, axis_rect)
            self._draw_lane_canvas(p, r_rect, "R", COLORS["right"])

            self._draw_tap_cluster(
                p, l_rect,
                delay_ms=self._state["left_delay"],
                fb_pct=self._state["left_feedback"],
                cross_delay_ms=self._state["right_delay"],
                cross_fb_pct=self._state["right_feedback"],
                accent_hex=COLORS["cyan"],
                is_focus=(self._hover_target == "L" or self._dragging_target == "L"),
            )
            self._draw_tap_cluster(
                p, r_rect,
                delay_ms=self._state["right_delay"],
                fb_pct=self._state["right_feedback"],
                cross_delay_ms=self._state["left_delay"],
                cross_fb_pct=self._state["left_feedback"],
                accent_hex=COLORS["right"],
                is_focus=(self._hover_target == "R" or self._dragging_target == "R"),
            )
            top_overlay_rect = l_rect

        # 右上角悬浮视图开关胶囊 (ENV / GHOST，对齐 EQ-UI.py 的 SPEC / PEAK)
        self._btn_env_rect = QRectF(top_overlay_rect.right() - 88, top_overlay_rect.top() + 7, 38, 18)
        self._btn_ghost_rect = QRectF(top_overlay_rect.right() - 46, top_overlay_rect.top() + 7, 40, 18)
        for rect, label, active in (
            (self._btn_env_rect, "ENV", self._show_envelope),
            (self._btn_ghost_rect, "GHOST", self._show_ghost and not is_mono),
        ):
            p.setPen(QPen(QColor("#3A4252") if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 46, 56, 210) if active else QColor(21, 21, 24, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#6E6E76"))
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 实时准星与 HUD 信息浮窗
        if self._mouse_pos is not None:
            active_lane = None
            if is_mono and top_overlay_rect.contains(self._mouse_pos):
                active_lane = top_overlay_rect
            elif l_rect.contains(self._mouse_pos):
                active_lane = l_rect
            elif r_rect.contains(self._mouse_pos):
                active_lane = r_rect

            if active_lane is not None:
                mx = self._mouse_pos.x()
                p.setPen(QPen(QColor(255, 255, 255, 26), 1.0, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(mx, active_lane.top()), QPointF(mx, active_lane.bottom()))

                if self._dragging_target:
                    ch = "Mono" if is_mono else ("Left" if self._dragging_target == "L" else "Right")
                    d_val = self._state["left_delay"] if self._dragging_target == "L" else self._state["right_delay"]
                    f_val = self._state["left_feedback"] if self._dragging_target == "L" else self._state["right_feedback"]
                    hud_str = f"{ch} Tap  •  {d_val:.1f} ms  •  Feedback {f_val:+.1f}%"
                    hud_col = QColor(COLORS["cyan"] if self._dragging_target == "L" else COLORS["right"])
                    hud_w = 232
                else:
                    t_ms = ((mx - active_lane.left()) / active_lane.width()) * self.MAX_TIME_MS
                    mid_y = active_lane.center().y()
                    amp_pct = max(-100.0, min(100.0, ((mid_y - self._mouse_pos.y()) / (active_lane.height() * 0.42)) * 100.0))
                    hud_str = f"{t_ms:.0f} ms   ({amp_pct:+.0f}%)"
                    hud_col = QColor("#B4B4BE")
                    hud_w = 132

                hud_rect = QRectF(top_overlay_rect.left() + 8, top_overlay_rect.top() + 7, hud_w, 18)
                p.setPen(QPen(QColor("#141416"), 1.0))
                p.setBrush(QColor(21, 21, 24, 215))
                p.drawRoundedRect(hud_rect, 3.5, 3.5)
                p.setPen(hud_col)
                p.setFont(QFont(MONO_FONT_FAM, 8))
                p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

    def _draw_lane_canvas(self, p: QPainter, rect: QRectF, ch_label: str, accent_hex: str):
        # 深邃渐变底色（与 EQ-UI.py / Dyamics-UI.py 完全一致）
        bg_grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#1D1D20"))
        bg_grad.setColorAt(1.0, QColor("#1D1D20"))
        p.fillRect(rect, bg_grad)

        # 垂直时间刻度网格
        grid_pen = QPen(QColor("#26262A"), 1.0)
        sec_pen = QPen(QColor("#333338"), 1.0)
        for t in self.TIME_TICKS[1:-1]:
            x = rect.left() + (t / self.MAX_TIME_MS) * rect.width()
            p.setPen(sec_pen if t == 1000 else grid_pen)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))

        # 中心零相位参考线
        mid_y = rect.center().y()
        p.setPen(QPen(QColor("#3A3A40"), 1.1))
        p.drawLine(QPointF(rect.left(), mid_y), QPointF(rect.right(), mid_y))

        # 外框描边
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)

        # 左侧声道标识徽章
        badge_rect = QRectF(4, mid_y - 10, 22, 20)
        col = QColor(accent_hex if self._state["enabled"] else "#686C78")
        p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 90), 1.0))
        p.setBrush(QColor("#151518"))
        p.drawRoundedRect(badge_rect, 3.5, 3.5)
        font = QFont(MONO_FONT_FAM)
        font.setPointSizeF(8.5)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(col)
        p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, ch_label)

    def _draw_time_axis(self, p: QPainter, rect: QRectF):
        p.setPen(QColor("#9A9AA2"))
        p.setFont(QFont(MONO_FONT_FAM, 8))
        labels = ["0ms", "250", "500", "750", "1s", "1.25s", "1.50s", "1.75s", "2s"]
        for t, txt in zip(self.TIME_TICKS, labels):
            x = rect.left() + (t / self.MAX_TIME_MS) * rect.width()
            if t == 0:
                p.drawText(QRectF(x, rect.top(), 36, rect.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)
            elif t == 2000:
                p.drawText(QRectF(x - 36, rect.top(), 36, rect.height()), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, txt)
            else:
                p.drawText(QRectF(x - 20, rect.top(), 40, rect.height()), Qt.AlignmentFlag.AlignCenter, txt)

    def _draw_tap_cluster(
        self, p: QPainter, rect: QRectF,
        delay_ms: float, fb_pct: float,
        cross_delay_ms: float | None, cross_fb_pct: float,
        accent_hex: str, is_focus: bool
    ):
        p.save()
        p.setClipRect(rect.adjusted(1, 1, -1, -1))

        mid_y = rect.center().y()
        max_h = rect.height() * 0.42
        enabled = self._state["enabled"]
        base_col = QColor(accent_hex if enabled else "#585C68")

        fb = fb_pct / 100.0
        blend = self._state["stereo_blend"] / 100.0
        damping = 1.0 - (self._state["high_ratio"] / 100.0) * 0.16

        # 1. 交叉串扰幽灵脉冲 (Stereo Blend > 0)
        if self._show_ghost and enabled and cross_delay_ms is not None and cross_delay_ms > 2.0 and blend > 0.01:
            ghost_col = QColor(base_col)
            ghost_col.setAlpha(58)
            p.setBrush(ghost_col)
            p.setPen(Qt.PenStyle.NoPen)
            c_fb = cross_fb_pct / 100.0
            for k in range(1, 24):
                t_ms = k * cross_delay_ms
                if t_ms > self.MAX_TIME_MS:
                    break
                amp = blend * 0.62 * (c_fb ** (k - 1)) * (damping ** (k - 1))
                if abs(amp) < 0.015:
                    break
                x = rect.left() + (t_ms / self.MAX_TIME_MS) * rect.width()
                h = amp * max_h
                p.drawRoundedRect(QRectF(x - 1.4, mid_y - h if h >= 0 else mid_y, 2.8, abs(h)), 1.2, 1.2)

        # 2. 指数衰减包络曲线（增强视觉科技感）
        eff_delay = max(4.0, delay_ms)
        if self._show_envelope and abs(fb) > 0.02:
            env_path = QPainterPath()
            first_x = rect.left() + (eff_delay / self.MAX_TIME_MS) * rect.width()
            env_path.moveTo(first_x, mid_y - max_h)
            steps = 90
            for i in range(1, steps + 1):
                t_ms = eff_delay + (i / steps) * (self.MAX_TIME_MS - eff_delay)
                k_cont = (t_ms - eff_delay) / eff_delay
                env_amp = (abs(fb) * damping) ** k_cont
                if env_amp < 0.005:
                    break
                px = rect.left() + (t_ms / self.MAX_TIME_MS) * rect.width()
                py = mid_y - env_amp * max_h
                env_path.lineTo(px, py)

            env_col = QColor(base_col.red(), base_col.green(), base_col.blue(), 85 if enabled else 35)
            p.setPen(QPen(env_col, 1.2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(env_path)

        # 3. 主脉冲序列（支持正负 Feedback 相位上下翻转）
        tap1_pt: QPointF | None = None
        for k in range(1, 48):
            t_ms = k * eff_delay
            if t_ms > self.MAX_TIME_MS:
                break
            amp = (fb ** (k - 1)) * (damping ** (k - 1))
            if abs(amp) < 0.012:
                break

            x = rect.left() + (t_ms / self.MAX_TIME_MS) * rect.width()
            h = amp * max_h
            bar_w = 4.2 if k == 1 else 3.0
            bar_rect = QRectF(x - bar_w / 2.0, mid_y - h if h >= 0 else mid_y, bar_w, abs(h))

            # 发光外晕
            if enabled and k <= 6:
                glow = QColor(base_col)
                glow.setAlpha(int(48 * (damping ** (k - 1))))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(glow)
                p.drawRoundedRect(bar_rect.adjusted(-2.2, -1.5, 2.2, 1.5), 2.5, 2.5)

            # 脉冲柱主体渐变
            bar_grad = QLinearGradient(bar_rect.topLeft(), bar_rect.bottomLeft())
            c_top = QColor(base_col)
            c_bot = QColor(base_col)
            alpha_main = int(max(95, 245 * (damping ** (k - 1))))
            c_top.setAlpha(alpha_main)
            c_bot.setAlpha(int(alpha_main * 0.55))
            if h >= 0:
                bar_grad.setColorAt(0.0, c_top)
                bar_grad.setColorAt(1.0, c_bot)
            else:
                bar_grad.setColorAt(0.0, c_bot)
                bar_grad.setColorAt(1.0, c_top)

            p.setBrush(bar_grad)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(bar_rect, 1.4, 1.4)

            if k == 1:
                tap1_pt = QPointF(x, mid_y - h)

        # 4. Tap 1 交互控制手柄节点（与 EQ-UI.py / Dyamics-UI.py 节点语言完全一致）
        if tap1_pt is not None and enabled:
            r = 5.2 if is_focus else 4.0
            if is_focus:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(base_col.red(), base_col.green(), base_col.blue(), 75))
                p.drawEllipse(tap1_pt, r + 4.2, r + 4.2)
            p.setPen(QPen(base_col, 1.7))
            p.setBrush(QColor("#151518"))
            p.drawEllipse(tap1_pt, r, r)

        p.restore()


# =====================================================================
# 7. 控件：双声道高精输出电平表（对齐 Dyamics-UI.py ProVerticalMeter）
# =====================================================================
class StereoOutputMeter(QWidget):
    TICKS = [0, -5, -10, -15, -20, -30, -40, -50]
    MIN_DB = -55.0
    MAX_DB = 0.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(58)
        self.setMinimumHeight(210)
        self._l_db = self.MIN_DB
        self._r_db = self.MIN_DB
        self._l_peak = self.MIN_DB
        self._r_peak = self.MIN_DB
        self._l_clip = False
        self._r_clip = False
        self._active = True

    def setLevels(self, left_db: float, right_db: float):
        self._l_db = max(self.MIN_DB, min(self.MAX_DB, left_db))
        self._r_db = max(self.MIN_DB, min(self.MAX_DB, right_db))
        self._l_peak = max(self._l_db, self._l_peak - 0.35)
        self._r_peak = max(self._r_db, self._r_peak - 0.35)
        if left_db >= -0.1:
            self._l_clip = True
        if right_db >= -0.1:
            self._r_clip = True
        self.update()

    def setActive(self, active: bool):
        self._active = active
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._l_clip = False
            self._r_clip = False
            self._l_peak = self._l_db
            self._r_peak = self._r_db
            self.update()

    def _db_to_y(self, db: float, bar: QRectF) -> float:
        clamped = max(self.MIN_DB, min(self.MAX_DB, db))
        if clamped >= -20.0:
            norm = 0.52 + ((clamped + 20.0) / 20.0) * 0.48
        else:
            norm = ((clamped - self.MIN_DB) / (-20.0 - self.MIN_DB)) * 0.52
        return bar.bottom() - norm * bar.height()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 12.0
        bot_pad = 18.0
        bar_w = 8.5
        l_x = 32.0
        r_x = 43.0
        bar_h = max(40.0, self.height() - top_pad - bot_pad)
        ref_bar = QRectF(l_x, top_pad, bar_w, bar_h)

        # 1. 刻度文字
        p.setFont(QFont(MONO_FONT_FAM, 7))
        for t in self.TICKS:
            ty = self._db_to_y(float(t), ref_bar)
            p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
            p.drawText(
                QRectF(0, ty - 7, l_x - 4, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{t} -",
            )

        # 2. 绘制 L / R 双声道表条
        channels = [
            ("L", l_x, self._l_db, self._l_peak, self._l_clip),
            ("R", r_x, self._r_db, self._r_peak, self._r_clip),
        ]
        for ch_name, bx, cur_db, peak_db, clipped in channels:
            bar = QRectF(bx, top_pad, bar_w, bar_h)

            # 槽底色
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor("#0E0E11"))
            p.drawRoundedRect(bar, 2.0, 2.0)

            if self._active:
                y_val = self._db_to_y(cur_db, bar)
                fill_h = bar.bottom() - y_val
                if fill_h > 1.0:
                    fill_rect = QRectF(bar.left() + 1.0, y_val, bar.width() - 2.0, fill_h - 0.5)
                    grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                    grad.setColorAt(0.0,  QColor("#26C281"))
                    grad.setColorAt(0.50, QColor("#4CD490"))
                    grad.setColorAt(0.53, QColor("#E8C538"))
                    grad.setColorAt(0.75, QColor("#F0A848"))
                    grad.setColorAt(0.78, QColor("#F26D6D"))
                    grad.setColorAt(1.0,  QColor("#FF4848"))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(grad)
                    p.drawRoundedRect(fill_rect, 1.2, 1.2)

                # 峰值保持线
                if peak_db > self.MIN_DB + 0.5:
                    py = self._db_to_y(peak_db, bar)
                    p.setPen(QPen(QColor("#F26D6D") if peak_db > -10.0 else QColor("#F0F2F6"), 1.4))
                    p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

            # 顶部削波指示灯
            clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FF4848") if (clipped and self._active) else QColor("#18181C"))
            p.drawRoundedRect(clip_rect, 1.0, 1.0)

            # 横向刻度栅格遮罩（与 Dyamics-UI.py 完全一致）
            p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
            gy = bar.top() + 3.0
            while gy < bar.bottom():
                p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
                gy += 3.0

            # 底部 L / R 标识
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
            p.drawText(QRectF(bx - 2, self.height() - 16, bar_w + 4, 15), Qt.AlignmentFlag.AlignCenter, ch_name)


# =====================================================================
# 8. 控件：底部参数模块卡片（对齐 Dyamics-UI.py ModuleCard 规范）
# =====================================================================
class DelayModuleCard(QFrame):
    def __init__(self, title: str, badge_text: str, accent_color: str, cells: list[KnobParameterCell], parent=None):
        super().__init__(parent)
        self._accent = accent_color
        self._cells = cells
        self._active = True

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(36)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 6, 12, 5)
        h_lay.setSpacing(6)

        self.title_lbl = QLabel(title)
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        self.badge_lbl = QLabel(badge_text)
        self.badge_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.6px;")
        h_lay.addWidget(self.badge_lbl)
        root.addWidget(header)

        # Body
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 7, 8, 8)
        b_lay.setSpacing(8)
        for cell in self._cells:
            b_lay.addWidget(cell, 1, Qt.AlignmentFlag.AlignCenter)
        root.addWidget(body, 1)

        self.setModuleActive(True)

    def setBadgeText(self, text: str):
        self.badge_lbl.setText(text)

    def setModuleActive(self, active: bool):
        self._active = active
        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
                border-radius: 0px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if active else '#6E6E76'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )
        for cell in self._cells:
            cell.setModuleActive(active)


# =====================================================================
# 9. 主面板：自主旗舰立体声延迟器 (Studio Stereo Delay)
# =====================================================================
class StudioDelayWidget(QWidget):
    # 对接后端音频引擎的完整信号接口
    bypassChanged = pyqtSignal(bool)
    inputFormatChanged = pyqtSignal(str)
    parameterChanged = pyqtSignal(str, float)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Audio 2", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Stereo Delay — {track_name}")
        self.resize(840, 680)
        self.setMinimumSize(760, 620)

        self._active_bank = "A"
        self._banks = {
            "A": default_delay_demo(),
            "B": default_delay_clean(),
        }

        self._presets = {
            "Default": copy.deepcopy(self._banks["A"]),
            "Clean Quarter": copy.deepcopy(self._banks["B"]),
            "Slapback Vocal": {
                **default_delay_demo(),
                "high_cut": 6800.0, "low_cut": 180.0,
                "left_delay": 88.0, "left_feedback": 8.0,
                "right_delay": 112.0, "right_feedback": -10.0,
                "high_ratio": 45.0, "stereo_blend": 10.0,
                "dry_wet": 24.0, "level": 0.0,
            },
            "Ping-Pong Ambient": {
                **default_delay_demo(),
                "high_cut": 8200.0, "low_cut": 150.0,
                "left_delay": 240.0, "left_feedback": 46.0,
                "right_delay": 360.0, "right_feedback": 42.0,
                "high_ratio": 72.0, "stereo_blend": 85.0,
                "dry_wet": 38.0, "level": -0.5,
            },
            "Wide Haas Doubler": {
                **default_delay_demo(),
                "high_cut": 16000.0, "low_cut": 90.0,
                "left_delay": 14.0, "left_feedback": 0.0,
                "right_delay": 26.0, "right_feedback": 0.0,
                "high_ratio": 20.0, "stereo_blend": 0.0,
                "dry_wet": 45.0, "level": 0.0,
            },
            "Dub Phase Echo": {
                **default_delay_demo(),
                "high_cut": 4800.0, "low_cut": 220.0,
                "left_delay": 310.0, "left_feedback": -58.0,
                "right_delay": 465.0, "right_feedback": 54.0,
                "high_ratio": 80.0, "stereo_blend": 65.0,
                "dry_wet": 42.0, "level": -1.0,
            },
        }

        self._cells: dict[str, KnobParameterCell] = {}
        self._setup_ui(track_name)
        self._refresh_all_views()

        # 演示动态模拟定时器（接入真实后端 update_output_meters 后自动停止）
        self._anim_t = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_meters)
        self._demo_timer.start(40)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: 'Helvetica Neue', 'PingFang SC';
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ProDelayTapGraphWidget, StereoOutputMeter {{
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
        # 1. 顶部整合工具栏（与 EQ-UI.py / Dyamics-UI.py 完全统一）
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

        title_lbl = QLabel(f"STEREO DELAY  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(10)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存为新预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(165)
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

        tb_lay.addSpacing(6)

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
        # 箭头按钮样式由 _update_top_button_styles 统一设置（勿在入布局前绑定 objectName QSS，
        # 否则 Qt6 基线对齐分组会把它排到行底，相对 A/B 下沉 2px）

        tb_lay.addStretch()

        # L/R 联动开关
        self.btn_link = QPushButton("Link L/R")
        self.btn_link.setCheckable(True)
        self.btn_link.setFixedSize(62, 22)
        self.btn_link.setToolTip("锁定左右声道延迟时间与反馈比例")
        self.btn_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_link.clicked.connect(self._on_link_toggled)
        tb_lay.addWidget(self.btn_link)

        fmt_lbl = QLabel("Input")
        fmt_lbl.setStyleSheet("color: #8E94A4; font-size: 11px; font-weight: 600;")
        tb_lay.addWidget(fmt_lbl)

        self.format_combo = ChevronComboBox()
        self.format_combo.addItems(["Mono", "Stereo", "LCR", "Quad", "5.0", "5.1", "7.1"])
        self.format_combo.setFixedWidth(self.format_combo.sizeHint().width())
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        tb_lay.addWidget(self.format_combo)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(46, 22)
        reset_btn.clicked.connect(self.reset_delay)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：交互式脉冲示波器 + 右侧 Output 双声道电平表
        # -------------------------------------------------------------
        upper_panel = QFrame()
        up_lay = QHBoxLayout(upper_panel)
        up_lay.setContentsMargins(0, 0, 0, 0)
        up_lay.setSpacing(0)

        self.tap_graph = ProDelayTapGraphWidget()
        self.tap_graph.tapDragged.connect(self._on_graph_tap_dragged)
        self.tap_graph.highRatioScrolled.connect(self._on_graph_hr_scrolled)
        up_lay.addWidget(self.tap_graph, 1)

        # 频响图与输出电平列之间的 1px 竖向黑线
        v_sep = QFrame()
        v_sep.setFixedWidth(1)
        v_sep.setStyleSheet("background-color: #141416; border: none;")
        up_lay.addWidget(v_sep)

        out_col = QVBoxLayout()
        out_col.setSpacing(3)
        out_lbl = QLabel("Output")
        out_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        out_lbl.setStyleSheet("color: #787882; font-size: 10px; font-weight: 700; letter-spacing: 0.3px;")
        out_col.addWidget(out_lbl)

        self.output_meter = StereoOutputMeter()
        out_col.addWidget(self.output_meter, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部 5 组独立处理卡片矩阵 (Filter | Left | Right | Feedback | Output)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        # 3.1 Filter Card
        c_hcut = KnobParameterCell(
            "High Cut", 100.0, 20000.0, 12500.0, COLORS["filter"],
            "100", "Hz", "20k",
            format_freq_hz, parse_freq_hz, mode="log"
        )
        c_lcut = KnobParameterCell(
            "Low Cut", 20.0, 5000.0, 108.0, COLORS["filter"],
            "20", "Hz", "5k",
            format_freq_hz, parse_freq_hz, mode="log"
        )
        self._cells["high_cut"] = c_hcut
        self._cells["low_cut"] = c_lcut
        self.card_filter = DelayModuleCard("Filter", "2-POLE", COLORS["filter"], [c_hcut, c_lcut])

        # 3.2 Left Card
        c_ldel = KnobParameterCell(
            "Delay Time", 0.0, 1200.0, 121.0, COLORS["cyan"],
            "0", "ms", "1.2s",
            lambda v: f"{v:.2f}", parse_delay_ms, mode="power"
        )
        c_lfb = KnobParameterCell(
            "Feedback", -99.0, 99.0, 16.0, COLORS["cyan"],
            "-99", "%", "99",
            lambda v: f"{v:.2f}", parse_float_clean, mode="bipolar"
        )
        self._cells["left_delay"] = c_ldel
        self._cells["left_feedback"] = c_lfb
        self.card_left = DelayModuleCard("Left", "CH L", COLORS["cyan"], [c_ldel, c_lfb])

        # 3.3 Right Card
        c_rdel = KnobParameterCell(
            "Delay Time", 0.0, 1200.0, 135.0, COLORS["right"],
            "0", "ms", "1.2s",
            lambda v: f"{v:.2f}", parse_delay_ms, mode="power"
        )
        c_rfb = KnobParameterCell(
            "Feedback", -99.0, 99.0, -7.0, COLORS["right"],
            "-99", "%", "99",
            lambda v: f"{v:.2f}", parse_float_clean, mode="bipolar"
        )
        self._cells["right_delay"] = c_rdel
        self._cells["right_feedback"] = c_rfb
        self.card_right = DelayModuleCard("Right", "CH R", COLORS["right"], [c_rdel, c_rfb])

        # 3.4 Feedback Matrix Card
        c_hr = KnobParameterCell(
            "High Ratio", 0.0, 100.0, 60.0, COLORS["fb"],
            "0", "%", "100",
            lambda v: f"{int(round(v))}", parse_float_clean, mode="linear"
        )
        c_sb = KnobParameterCell(
            "Stereo Blend", 0.0, 100.0, 15.0, COLORS["fb"],
            "None", "%", "Full",
            lambda v: f"{v:.1f}", parse_float_clean, mode="linear"
        )
        self._cells["high_ratio"] = c_hr
        self._cells["stereo_blend"] = c_sb
        self.card_fb = DelayModuleCard("Feedback", "MATRIX", COLORS["fb"], [c_hr, c_sb])

        # 3.5 Output Card
        c_dw = KnobParameterCell(
            "Dry/Wet", 0.0, 100.0, 30.0, COLORS["cyan"],
            "Dry", "%", "Wet",
            lambda v: f"{v:.1f}", parse_float_clean, mode="linear"
        )
        c_lvl = KnobParameterCell(
            "Level", -24.0, 6.0, 0.0, COLORS["cyan"],
            "-24", "dB", "+6",
            lambda v: f"{v:.1f}", parse_float_clean, mode="linear",
            zero_marker_val=0.0
        )
        self._cells["dry_wet"] = c_dw
        self._cells["level"] = c_lvl
        self.card_out = DelayModuleCard("Output", "MASTER", COLORS["cyan"], [c_dw, c_lvl])

        for card in (self.card_filter, self.card_left, self.card_right, self.card_fb, self.card_out):
            cards_row.addWidget(card, 1)

        main_lay.addLayout(cards_row)

        # 连接所有旋钮变更信号
        for pid, cell in self._cells.items():
            cell.valueChanged.connect(lambda val, key=pid: self._on_param_changed(key, val))

    # -----------------------------------------------------------------
    # 状态管理与双向交互联动
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_top_button_styles(self):
        st = self._current_state()
        active_css = """
            QPushButton {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
            }
        """
        inactive_css = """
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
        self.btn_a.setStyleSheet(active_css if self._active_bank == "A" else inactive_css)
        self.btn_b.setStyleSheet(active_css if self._active_bank == "B" else inactive_css)
        self.btn_copy_ab.setStyleSheet(inactive_css)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

        self.btn_link.setChecked(st["link_lr"])
        self.btn_link.setStyleSheet(active_css if st["link_lr"] else inactive_css)

    def _refresh_all_views(self):
        st = self._current_state()
        self._update_top_button_styles()

        self.power_switch.setChecked(st["enabled"])
        self.format_combo.blockSignals(True)
        self.format_combo.setCurrentText(st["input_format"])
        self.format_combo.blockSignals(False)

        for pid, cell in self._cells.items():
            if pid in st:
                cell.setValue(st[pid])

        en = bool(st["enabled"])
        is_mono = (st["input_format"] == "Mono")

        self.card_filter.setModuleActive(en)
        self.card_left.setModuleActive(en)
        self.card_left.setBadgeText("MONO" if is_mono else "CH L")
        self.card_right.setModuleActive(en and not is_mono)
        self.card_fb.setModuleActive(en)
        self._cells["stereo_blend"].setModuleActive(en and not is_mono)
        self.card_out.setModuleActive(en)

        self.output_meter.setActive(en)
        self.tap_graph.setState(st)

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

    def _on_link_toggled(self, linked: bool):
        st = self._current_state()
        st["link_lr"] = linked
        if linked:
            st["right_delay"] = st["left_delay"]
            st["right_feedback"] = st["left_feedback"]
            self.parameterChanged.emit("right_delay", st["right_delay"])
            self.parameterChanged.emit("right_feedback", st["right_feedback"])
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def _on_format_changed(self, fmt: str):
        st = self._current_state()
        st["input_format"] = fmt
        self._refresh_all_views()
        self.inputFormatChanged.emit(fmt)
        self.stateChanged.emit(self.get_state())

    def _on_param_changed(self, key: str, val: float):
        st = self._current_state()
        st[key] = val

        if st["link_lr"]:
            if key == "left_delay":
                st["right_delay"] = val
                self._cells["right_delay"].setValue(val)
                self.parameterChanged.emit("right_delay", val)
            elif key == "right_delay":
                st["left_delay"] = val
                self._cells["left_delay"].setValue(val)
                self.parameterChanged.emit("left_delay", val)
            elif key == "left_feedback":
                st["right_feedback"] = val
                self._cells["right_feedback"].setValue(val)
                self.parameterChanged.emit("right_feedback", val)
            elif key == "right_feedback":
                st["left_feedback"] = val
                self._cells["left_feedback"].setValue(val)
                self.parameterChanged.emit("left_feedback", val)

        self.tap_graph.setState(st)
        self.parameterChanged.emit(key, val)
        self.stateChanged.emit(self.get_state())

    def _on_graph_tap_dragged(self, channel: str, delay_ms: float, fb_pct: float):
        if channel == "L":
            self._cells["left_delay"].setValue(delay_ms)
            self._cells["left_feedback"].setValue(fb_pct)
            self._on_param_changed("left_delay", delay_ms)
            self._on_param_changed("left_feedback", fb_pct)
        else:
            self._cells["right_delay"].setValue(delay_ms)
            self._cells["right_feedback"].setValue(fb_pct)
            self._on_param_changed("right_delay", delay_ms)
            self._on_param_changed("right_feedback", fb_pct)

    def _on_graph_hr_scrolled(self, delta: float):
        st = self._current_state()
        new_hr = max(0.0, min(100.0, round(st["high_ratio"] + delta, 0)))
        self._cells["high_ratio"].setValue(new_hr)
        self._on_param_changed("high_ratio", new_hr)

    def _load_preset(self, name: str):
        if name in self._presets:
            cur_fmt = self._current_state()["input_format"]
            self._banks[self._active_bank] = copy.deepcopy(self._presets[name])
            self._banks[self._active_bank]["input_format"] = cur_fmt
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存 Delay 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 演示动态模拟（接入真实音频引擎后自动停止）
    # -----------------------------------------------------------------
    def _tick_demo_meters(self):
        self._anim_t += 0.11
        st = self._current_state()
        if not st["enabled"]:
            self.output_meter.setLevels(-55.0, -55.0)
            return
        lvl = st["level"]
        l_db = -16.0 + 9.0 * math.sin(self._anim_t * 1.1) + 4.5 * math.cos(self._anim_t * 2.5) + random.uniform(-1.5, 1.5) + lvl
        r_db = -17.0 + 8.5 * math.cos(self._anim_t * 0.95) + 4.0 * math.sin(self._anim_t * 2.2) + random.uniform(-1.5, 1.5) + lvl
        self.output_meter.setLevels(l_db, r_db)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_delay(self):
        cur_fmt = self._current_state()["input_format"]
        self._banks[self._active_bank] = default_delay_demo()
        self._banks[self._active_bank]["input_format"] = cur_fmt
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_output_meters(self, left_db: float, right_db: float):
        """接入真实音频引擎双声道输出峰值电平 (-55.0 ~ 0.0 dBFS)"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.output_meter.setLevels(left_db, right_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioDelayWidget("Audio 2")
    win.show()
    sys.exit(app.exec())
