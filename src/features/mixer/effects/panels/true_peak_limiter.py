from __future__ import annotations

import sys
import math
import copy
import random
from collections import deque
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer, QSignalBlocker
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


# =====================================================================
# 1. 全局色彩系统与默认母带交付规范（与 Dyamics-UI / multiband_comp_ui 统一）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":   "#28D8F0",  # 主控 / A-B 激活 / 输出包络主色
    "green":  "#4CD490",  # 交付合规 / Safe / DC Filter
    "amber":  "#F0A848",  # LUFS 响度联动 / Target 参考线
    "red":    "#F26D6D",  # Ceiling dBTP / Gain Reduction / Clip 警示
    "blue":   "#6495FA",  # True-Peak 过采样 / ISP 捕获标记
}

DELIVERY_STANDARDS = {
    "Streaming (-14 LUFS / -1.0 dBTP)": {
        "target_lufs": -14.0, "ceiling_dbtp": -1.0, "oversampling": 4, "true_peak": True,
    },
    "Apple Digital (-16 LUFS / -1.0 dBTP)": {
        "target_lufs": -16.0, "ceiling_dbtp": -1.0, "oversampling": 4, "true_peak": True,
    },
    "EBU R128 (-23 LUFS / -1.0 dBTP)": {
        "target_lufs": -23.0, "ceiling_dbtp": -1.0, "oversampling": 4, "true_peak": True,
    },
    "ATSC A/85 (-24 LUFS / -2.0 dBTP)": {
        "target_lufs": -24.0, "ceiling_dbtp": -2.0, "oversampling": 4, "true_peak": True,
    },
    "CD / Club Master (-9 LUFS / -0.3 dBTP)": {
        "target_lufs": -9.0, "ceiling_dbtp": -0.3, "oversampling": 8, "true_peak": True,
    },
    "Custom Delivery Target": None,
}

OS_LATENCY_MS = {
    1: 0.0,
    2: 0.8,
    4: 1.5,
    8: 3.1,
}


def default_limiter_demo() -> dict:
    """默认母带流媒体发行配置 (-14.0 LUFS / -1.0 dBTP, 4x True-Peak)"""
    return {
        "enabled": True,
        "standard": "Streaming (-14 LUFS / -1.0 dBTP)",
        "gain": 4.5,                 # 0.0 ~ +24.0 dB
        "ceiling": -1.0,             # -12.0 ~ 0.0 dBTP
        "target_lufs": -14.0,        # -30.0 ~ -5.0 LUFS
        "trim": 0.0,                 # -6.0 ~ +6.0 dB
        "lufs_auto_link": False,     # 实时自动跟踪 Target LUFS
        "ceil_link": False,          # 降低 Ceiling 时联动补偿 Gain
        "audition_mode": "Normal",   # "Normal" | "Delta" | "Unity"
        "dc_filter": True,           # 5 Hz 高通直流偏移滤波
        "engine": {
            "enabled": True,
            "style": "Transparent",  # Transparent | Punchy | Dynamic | Allround | Aggressive | Modern
            "lookahead": 1.50,       # 0.00 ~ 10.00 ms
            "attack": 1.00,          # 0.05 ~ 100.0 ms
            "release": 80.0,         # 1.0 ~ 1000.0 ms
            "knee": 25.0,            # 0 ~ 100 %
            "link_trans": 100.0,     # 0 ~ 100 % 瞬态立体声链接
            "link_rel": 75.0,        # 0 ~ 100 % 释放立体声链接
            "oversampling": 4,       # 1 | 2 | 4 | 8
            "true_peak": True,       # ISP 间采样真峰检测
            "auto_release": True,    # 节目自适应释放
            "anti_clip": True,       # 末级硬防削波保护
        },
        "dither": {
            "enabled": True,
            "bit_depth": "24-bit",       # "16-bit" | "24-bit" | "32-bit Float"
            "shaping": "POW-r 2",        # "None" | "Triangular" | "POW-r 1" | "POW-r 2" | "POW-r 3"
            "auto_blank": True,          # 数字静音自动静默抖动
        },
    }


def default_limiter_flat() -> dict:
    st = default_limiter_demo()
    st["standard"] = "Custom Delivery Target"
    st["gain"] = 0.0
    st["ceiling"] = -0.3
    st["trim"] = 0.0
    st["lufs_auto_link"] = False
    st["ceil_link"] = False
    st["audition_mode"] = "Normal"
    st["engine"]["oversampling"] = 4
    st["engine"]["true_peak"] = True
    st["dither"]["enabled"] = False
    return st


def format_ms(val: float) -> str:
    if val < 1.0:
        return f"{val:.2f}"
    if val < 10.0:
        return f"{val:.2f}"
    if val < 100.0:
        return f"{val:.1f}"
    return f"{int(round(val))}"


def format_db_signed(val: float) -> str:
    if abs(val) < 0.05:
        return "0.0"
    return f"{val:+.1f}"


# =====================================================================
# 2. 基础定制控件：电源开关 / 可编辑数字胶囊 / 阳极铝旋钮
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

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0", width: int = 58, parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._active = True
        self.setFixedSize(width, 20)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(0, 0, width, 20)
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
                self._text.replace("dBTP", "")
                .replace("LUFS", "")
                .replace("dB", "")
                .replace("%", "")
                .replace("ms", "")
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

        arc_r = 14.5
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        p.setPen(QPen(QColor("#141417"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        arc_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(QPen(arc_col, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        span_deg = curr_deg - start_deg
        if abs(span_deg) > 0.5:
            p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        knob_r = 10.6
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.0
        iy1 = cy - math.sin(rad) * 3.0
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
        lay.setContentsMargins(2, 1, 2, 1)
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
        hints_row.setSpacing(1)
        for h_text, align in (
            (min_hint, Qt.AlignmentFlag.AlignLeft),
            (mid_hint, Qt.AlignmentFlag.AlignCenter),
            (max_hint, Qt.AlignmentFlag.AlignRight),
        ):
            hl = QLabel(h_text)
            hl.setAlignment(align)
            hl.setStyleSheet("color: #787882; font-size: 8.5px;")
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
# 3. 专业母带表头：Input / Gain Reduction / True-Peak (dBTP) / LUFS
# =====================================================================
class ProVerticalMeter(QWidget):
    """通用输入电平与 Gain Reduction 衰减表"""
    def __init__(
        self,
        title: str = "",
        mode: str = "level",
        min_db: float = -48.0,
        max_db: float = 0.0,
        ticks: list[int] | None = None,
        show_ticks: bool = True,
        accent_color: str = "#F26D6D",
        parent=None,
    ):
        super().__init__(parent)
        self._title = title
        self._mode = mode
        self._min_db = min_db
        self._max_db = max_db
        self._ticks = ticks or (
            [0, -3, -6, -12, -18, -24, -36, -48]
            if mode == "level"
            else [0, -2, -4, -6, -10, -15, -24]
        )
        self._show_ticks = show_ticks
        self._accent = QColor(accent_color)

        self._value_db = min_db if mode == "level" else 0.0
        self._peak_db = self._value_db
        self._clipped = False
        self._active = True

        self.setFixedWidth(44 if show_ticks else 18)
        self.setMinimumHeight(180)

    def setValue(self, db: float):
        if self._mode == "level":
            self._value_db = max(self._min_db, min(self._max_db, db))
            if self._value_db >= self._peak_db:
                self._peak_db = self._value_db
            else:
                self._peak_db = max(self._value_db, self._peak_db - 0.35)
            if db >= -0.05:
                self._clipped = True
        else:
            val = -abs(db)
            self._value_db = max(self._min_db, min(0.0, val))
            if self._value_db <= self._peak_db:
                self._peak_db = self._value_db
            else:
                self._peak_db = min(self._value_db, self._peak_db + 0.35)
        self.update()

    def resetPeaks(self):
        self._clipped = False
        self._peak_db = self._value_db
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.resetPeaks()

    def _db_to_y(self, db: float, bar: QRectF) -> float:
        clamped = max(self._min_db, min(self._max_db, db))
        if self._mode == "level":
            if clamped >= -18.0:
                norm = 0.48 + ((clamped + 18.0) / 18.0) * 0.52
            else:
                norm = ((clamped - self._min_db) / (-18.0 - self._min_db)) * 0.48
        else:
            if clamped >= -10.0:
                norm = 0.45 + ((clamped + 10.0) / 10.0) * 0.55
            else:
                norm = ((clamped - self._min_db) / (-10.0 - self._min_db)) * 0.45
        return bar.bottom() - norm * bar.height()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 10.0
        bot_pad = 18.0
        bar_w = 10.0
        bar_x = (self.width() - bar_w - 3.0) if self._show_ticks else (self.width() - bar_w) / 2.0
        bar = QRectF(bar_x, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))

        if self._show_ticks:
            p.setFont(QFont(MONO_FONT_FAM, 7))
            for t in self._ticks:
                ty = self._db_to_y(float(t), bar)
                p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
                p.drawText(
                    QRectF(0, ty - 7, bar.left() - 3, 14),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{t} -",
                )

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        if self._active:
            if self._mode == "level":
                y_val = self._db_to_y(self._value_db, bar)
                fill_h = bar.bottom() - y_val
                if fill_h > 1.0:
                    fill_rect = QRectF(bar.left() + 1.0, y_val, bar.width() - 2.0, fill_h - 0.5)
                    grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                    grad.setColorAt(0.0,  QColor("#26C281"))
                    grad.setColorAt(0.50, QColor("#4CD490"))
                    grad.setColorAt(0.72, QColor("#F0A848"))
                    grad.setColorAt(0.92, QColor("#F26D6D"))
                    grad.setColorAt(1.0,  QColor("#FF4848"))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(grad)
                    p.drawRoundedRect(fill_rect, 1.2, 1.2)

                if self._peak_db > self._min_db + 0.5:
                    py = self._db_to_y(self._peak_db, bar)
                    p.setPen(QPen(QColor("#F26D6D") if self._peak_db > -6.0 else QColor("#F0F2F6"), 1.4))
                    p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

                clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
                p.drawRoundedRect(clip_rect, 1.0, 1.0)
            else:
                y_val = self._db_to_y(self._value_db, bar)
                fill_h = y_val - bar.top()
                if fill_h > 1.0:
                    fill_rect = QRectF(bar.left() + 1.0, bar.top() + 0.5, bar.width() - 2.0, fill_h)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(self._accent)
                    p.drawRoundedRect(fill_rect, 1.2, 1.2)

                if self._peak_db < -0.2:
                    py = self._db_to_y(self._peak_db, bar)
                    p.setPen(QPen(QColor("#FFFFFF"), 1.3))
                    p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

        p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
        gy = bar.top() + 3.0
        while gy < bar.bottom():
            p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
            gy += 3.0

        # 底部数字峰值读数
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        if self._mode == "level":
            txt = f"{self._peak_db:.1f}" if self._peak_db > self._min_db + 0.5 else "-inf"
            p.setPen(QColor("#D0D0D8"))
        else:
            txt = f"{self._peak_db:.1f}" if self._peak_db < -0.05 else "0.0"
            p.setPen(self._accent)
        p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, txt)


class TruePeakOutputMeter(QWidget):
    """母带专用 True-Peak (dBTP) 与 Sample Peak 双层电平表"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._min_db = -48.0
        self._max_db = 0.0
        self._sample_db = -48.0
        self._true_peak_db = -48.0
        self._max_tp_hold = -48.0
        self._ceiling_db = -1.0
        self._tp_clipped = False

        self.setFixedWidth(52)
        self.setMinimumHeight(180)
        self.setToolTip("双层输出表：主柱体为 Sample Peak，高亮蓝白线为过采样 True-Peak (dBTP)，点击重置峰值")

    def setCeiling(self, ceiling_dbtp: float):
        self._ceiling_db = ceiling_dbtp
        self.update()

    def setValues(self, sample_db: float, true_peak_dbtp: float):
        self._sample_db = max(self._min_db, min(self._max_db, sample_db))
        self._true_peak_db = max(self._min_db, min(0.5, true_peak_dbtp))
        if self._true_peak_db >= self._max_tp_hold:
            self._max_tp_hold = self._true_peak_db
        if self._true_peak_db > self._ceiling_db + 0.05 or self._true_peak_db >= -0.02:
            self._tp_clipped = True
        self.update()

    def resetPeaks(self):
        self._tp_clipped = False
        self._max_tp_hold = self._true_peak_db
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.resetPeaks()

    def _db_to_y(self, db: float, bar: QRectF) -> float:
        clamped = max(self._min_db, min(self._max_db, db))
        if clamped >= -18.0:
            norm = 0.48 + ((clamped + 18.0) / 18.0) * 0.52
        else:
            norm = ((clamped - self._min_db) / (-18.0 - self._min_db)) * 0.48
        return bar.bottom() - norm * bar.height()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 10.0
        bot_pad = 18.0
        bar_w = 11.0
        bar_x = self.width() - bar_w - 5.0
        bar = QRectF(bar_x, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))

        # 刻度
        p.setFont(QFont(MONO_FONT_FAM, 7))
        for t in (0, -1, -3, -6, -12, -18, -24, -36, -48):
            ty = self._db_to_y(float(t), bar)
            p.setPen(QColor("#F26D6D") if t == -1 else QColor("#9A9AA2"))
            p.drawText(
                QRectF(0, ty - 7, bar.left() - 3, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{t} -",
            )

        # 底槽
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        # Sample Peak 填充
        y_sp = self._db_to_y(self._sample_db, bar)
        fill_h = bar.bottom() - y_sp
        if fill_h > 1.0:
            fill_rect = QRectF(bar.left() + 1.0, y_sp, bar.width() - 2.0, fill_h - 0.5)
            grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
            grad.setColorAt(0.0,  QColor("#26C281"))
            grad.setColorAt(0.55, QColor("#4CD490"))
            grad.setColorAt(0.82, QColor("#28D8F0"))
            grad.setColorAt(0.95, QColor("#F0A848"))
            grad.setColorAt(1.0,  QColor("#F26D6D"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(fill_rect, 1.2, 1.2)

        # 栅格遮罩
        p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
        gy = bar.top() + 3.0
        while gy < bar.bottom():
            p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
            gy += 3.0

        # Ceiling (dBTP) 红色警戒标尺
        cy = self._db_to_y(self._ceiling_db, bar)
        p.setPen(QPen(QColor("#F26D6D"), 1.5))
        p.drawLine(QPointF(bar.left() - 2.0, cy), QPointF(bar.right() + 2.0, cy))

        # 瞬时 True-Peak (dBTP) 亮蓝指针
        if self._true_peak_db > self._min_db + 0.5:
            tpy = self._db_to_y(self._true_peak_db, bar)
            p.setPen(QPen(QColor("#6495FA"), 1.8))
            p.drawLine(QPointF(bar.left() + 0.5, tpy), QPointF(bar.right() - 0.5, tpy))

        # Max True-Peak 保持线
        if self._max_tp_hold > self._min_db + 0.5:
            mpy = self._db_to_y(self._max_tp_hold, bar)
            p.setPen(QPen(QColor("#FF5858") if self._tp_clipped else QColor("#FFFFFF"), 1.5))
            p.drawLine(QPointF(bar.left() + 0.5, mpy), QPointF(bar.right() - 0.5, mpy))

        # 顶部 TP OVER 警示灯
        clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._tp_clipped else QColor("#18181C"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)

        # 底部 Max dBTP 读数
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        txt = f"{self._max_tp_hold:.1f}" if self._max_tp_hold > self._min_db + 0.5 else "-inf"
        p.setPen(QColor("#F26D6D") if self._tp_clipped else QColor("#6495FA"))
        p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, txt)


class ProLufsMeter(QWidget):
    """ITU-R BS.1770-4 垂直响度表 (-36 ~ 0 LUFS, Short-Term + Integrated + Target)"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._min_lu = -36.0
        self._max_lu = 0.0
        self._short_term = -36.0
        self._integrated = -36.0
        self._target = -14.0

        self.setFixedWidth(52)
        self.setMinimumHeight(180)
        self.setToolTip("ITU-R BS.1770 响度表：柱体为 Short-Term (3s)，白线为 Integrated (I)，琥珀三角为 Target LUFS")

    def setTarget(self, target_lufs: float):
        self._target = target_lufs
        self.update()

    def setValues(self, short_term_lufs: float, integrated_lufs: float):
        self._short_term = max(self._min_lu, min(self._max_lu, short_term_lufs))
        self._integrated = max(self._min_lu, min(self._max_lu, integrated_lufs))
        self.update()

    def _lu_to_y(self, lu: float, bar: QRectF) -> float:
        clamped = max(self._min_lu, min(self._max_lu, lu))
        norm = (clamped - self._min_lu) / (self._max_lu - self._min_lu)
        return bar.bottom() - norm * bar.height()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 10.0
        bot_pad = 18.0
        bar_w = 11.0
        bar_x = self.width() - bar_w - 5.0
        bar = QRectF(bar_x, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))

        p.setFont(QFont(MONO_FONT_FAM, 7))
        for t in (0, -6, -9, -14, -18, -23, -30, -36):
            ty = self._lu_to_y(float(t), bar)
            p.setPen(QColor("#F0A848") if abs(t - self._target) < 0.6 else QColor("#9A9AA2"))
            p.drawText(
                QRectF(0, ty - 7, bar.left() - 3, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{t} -",
            )

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        # Short-Term 响度柱
        y_st = self._lu_to_y(self._short_term, bar)
        fill_h = bar.bottom() - y_st
        if fill_h > 1.0:
            fill_rect = QRectF(bar.left() + 1.0, y_st, bar.width() - 2.0, fill_h - 0.5)
            grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
            grad.setColorAt(0.0,  QColor("#2E8B6E"))
            grad.setColorAt(0.55, QColor("#4CD490"))
            grad.setColorAt(0.75, QColor("#F0A848"))
            grad.setColorAt(1.0,  QColor("#F26D6D"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(fill_rect, 1.2, 1.2)

        p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
        gy = bar.top() + 3.0
        while gy < bar.bottom():
            p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
            gy += 3.0

        # Target LUFS 琥珀金三角标尺
        ty_tar = self._lu_to_y(self._target, bar)
        p.setPen(QPen(QColor("#F0A848"), 1.5))
        p.drawLine(QPointF(bar.left() - 2.0, ty_tar), QPointF(bar.right() + 2.0, ty_tar))

        # Integrated LUFS 白色精确定位线
        if self._integrated > self._min_lu + 0.5:
            iy = self._lu_to_y(self._integrated, bar)
            p.setPen(QPen(QColor("#FFFFFF"), 1.8))
            p.drawLine(QPointF(bar.left() + 0.5, iy), QPointF(bar.right() - 0.5, iy))

        # 底部 Integrated LUFS 读数
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        txt = f"{self._integrated:.1f}" if self._integrated > self._min_lu + 0.5 else "-inf"
        diff = abs(self._integrated - self._target)
        col = QColor("#4CD490") if diff <= 0.8 else QColor("#F0A848")
        p.setPen(col)
        p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, txt)


# =====================================================================
# 4. 核心监控画布：实时滚动波形 + Gain Reduction 幕帘 + dBTP/LUFS 交互线
# =====================================================================
class ProTruePeakDisplayWidget(QWidget):
    ceilingDragged = pyqtSignal(float)
    targetLufsDragged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 230)
        self.setMouseTracking(True)

        self._state = default_limiter_demo()
        self.processor_controls = False
        self._integrated_lufs = -14.2
        self._max_tp_dbtp = -1.0

        # 滚动历史环形队列 (140 帧，约 5.6 秒窗口)
        max_len = 140
        self._hist_boosted: deque[float] = deque([-48.0] * max_len, maxlen=max_len)
        self._hist_output: deque[float] = deque([-48.0] * max_len, maxlen=max_len)
        self._hist_gr: deque[float] = deque([0.0] * max_len, maxlen=max_len)
        self._hist_isp: deque[bool] = deque([False] * max_len, maxlen=max_len)

        self._drag_target = ""   # "ceiling" | "target_lufs" | ""
        self._hover_target = ""
        self._mouse_pos: QPointF | None = None

        self._ml = 36.0
        self._mr = 14.0
        self._mt = 12.0
        self._mb = 22.0

    def setState(self, state: dict):
        self._state = state
        self.update()

    def pushFrame(
        self,
        boosted_db: float,
        output_dbtp: float,
        gr_db: float,
        isp_caught: bool,
        integrated_lufs: float,
        max_tp_dbtp: float,
    ):
        self._hist_boosted.append(max(-48.0, min(6.0, boosted_db)))
        self._hist_output.append(max(-48.0, min(0.5, output_dbtp)))
        self._hist_gr.append(max(0.0, min(24.0, gr_db)))
        self._hist_isp.append(isp_caught)
        self._integrated_lufs = integrated_lufs
        self._max_tp_dbtp = max_tp_dbtp
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(120.0, self.width() - self._ml - self._mr),
            max(100.0, self.height() - self._mt - self._mb),
        )

    def _db_to_y(self, db: float, plot: QRectF) -> float:
        # 纵轴：0 dB (顶) ~ -48 dB (底)，上方留 0 dBTP 边界清晰度
        clamped = max(-48.0, min(0.0, db))
        if clamped >= -18.0:
            norm = 0.45 + ((clamped + 18.0) / 18.0) * 0.55
        else:
            norm = ((clamped + 48.0) / 30.0) * 0.45
        return plot.bottom() - norm * plot.height()

    def _y_to_db(self, y: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (plot.bottom() - y) / plot.height()))
        if norm >= 0.45:
            return -18.0 + ((norm - 0.45) / 0.55) * 18.0
        return -48.0 + (norm / 0.45) * 30.0

    def _gr_to_y(self, gr_db: float, plot: QRectF) -> float:
        # Gain Reduction 自顶向下展开 (0 ~ 18 dB 占上半视觉区)
        clamped = max(0.0, min(24.0, gr_db))
        norm = min(1.0, clamped / 18.0)
        return plot.top() + norm * (plot.height() * 0.72)

    def _hit_test(self, pos: QPointF, plot: QRectF) -> str:
        if not plot.contains(pos):
            return ""
        ceil_y = self._db_to_y(self._state["ceiling"], plot)
        if abs(pos.y() - ceil_y) <= 7.0:
            return "ceiling"
        tar_y = self._db_to_y(self._state["target_lufs"], plot)
        if not self.processor_controls and abs(pos.y() - tar_y) <= 7.0:
            return "target_lufs"
        return ""

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_target = ""
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            plot = self._plot_rect()
            hit = self._hit_test(event.position(), plot)
            if hit:
                self._drag_target = hit

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._drag_target == "ceiling":
            raw = self._y_to_db(event.position().y(), plot)
            clamped = max(-12.0, min(0.0, round(raw, 1)))
            self.ceilingDragged.emit(clamped)
            return

        if self._drag_target == "target_lufs":
            raw = self._y_to_db(event.position().y(), plot)
            clamped = max(-30.0, min(-5.0, round(raw, 1)))
            self.targetLufsDragged.emit(clamped)
            return

        hit = self._hit_test(event.position(), plot)
        self._hover_target = hit
        if hit in ("ceiling", "target_lufs"):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_target = ""
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 画布底色
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 网格线与 dB / 时间刻度
        grid_pen = QPen(QColor("#18181C"), 1.0)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        for db in (0, -3, -6, -12, -18, -24, -36, -48):
            y = self._db_to_y(float(db), plot)
            p.setPen(QPen(QColor("#2A2A32"), 1.0) if db == 0 else grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

            p.setPen(QColor("#9A9AA2"))
            p.drawText(QRectF(0, y - 8, plot.left() - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(db))

        time_marks = [(0.0, "-5s"), (0.2, "-4s"), (0.4, "-3s"), (0.6, "-2s"), (0.8, "-1s"), (1.0, "NOW")]
        for ratio, lbl in time_marks:
            x = plot.left() + ratio * plot.width()
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#28D8F0") if lbl == "NOW" else QColor("#9A9AA2"))
            p.drawText(QRectF(x - 16, plot.bottom() + 4, 32, 16), Qt.AlignmentFlag.AlignCenter, lbl)

        p.save()
        p.setClipRect(plot)

        st = self._state
        enabled = st["enabled"]
        is_delta = (st["audition_mode"] == "Delta")
        n = len(self._hist_output)

        if n >= 2:
            # 3. 增益后原始峰值幽灵波形（展示被削减前的能量）
            ghost_path = QPainterPath()
            ghost_path.moveTo(plot.left(), plot.bottom())
            for i, b_db in enumerate(self._hist_boosted):
                x = plot.left() + (i / (n - 1)) * plot.width()
                y = self._db_to_y(min(0.0, b_db), plot)
                ghost_path.lineTo(x, y)
            ghost_path.lineTo(plot.right(), plot.bottom())
            ghost_path.closeSubpath()

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(130, 140, 160, 26 if enabled else 14))
            p.drawPath(ghost_path)

            # 4. 限制后输出主波形（被 Ceiling 削平后的干净包络）
            out_line = QPainterPath()
            out_fill = QPainterPath()
            out_fill.moveTo(plot.left(), plot.bottom())
            for i, o_db in enumerate(self._hist_output):
                x = plot.left() + (i / (n - 1)) * plot.width()
                y = self._db_to_y(o_db, plot)
                if i == 0:
                    out_line.moveTo(x, y)
                else:
                    out_line.lineTo(x, y)
                out_fill.lineTo(x, y)
            out_fill.lineTo(plot.right(), plot.bottom())
            out_fill.closeSubpath()

            wave_alpha = 20 if is_delta else (52 if enabled else 22)
            wave_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            wave_grad.setColorAt(0.0, QColor(40, 216, 240, wave_alpha))
            wave_grad.setColorAt(1.0, QColor(40, 216, 240, 4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(wave_grad)
            p.drawPath(out_fill)

            p.setPen(QPen(QColor(40, 216, 240, 80 if is_delta else (210 if enabled else 90)), 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(out_line)

            # 5. Gain Reduction 自顶向下动态衰减幕帘 (Pro-L 2 核心视觉)
            if enabled:
                gr_line = QPainterPath()
                gr_fill = QPainterPath()
                gr_fill.moveTo(plot.left(), plot.top())
                for i, gr_v in enumerate(self._hist_gr):
                    x = plot.left() + (i / (n - 1)) * plot.width()
                    y = self._gr_to_y(gr_v, plot)
                    if i == 0:
                        gr_line.moveTo(x, y)
                    else:
                        gr_line.lineTo(x, y)
                    gr_fill.lineTo(x, y)
                gr_fill.lineTo(plot.right(), plot.top())
                gr_fill.closeSubpath()

                gr_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
                gr_grad.setColorAt(0.0, QColor(242, 109, 109, 95 if is_delta else 68))
                gr_grad.setColorAt(1.0, QColor(242, 109, 109, 12))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(gr_grad)
                p.drawPath(gr_fill)

                p.setPen(QPen(QColor("#F26D6D"), 2.1 if is_delta else 1.7))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(gr_line)

                # 6. Inter-Sample Peak (ISP) 真峰过采样捕获菱形标记
                if st["engine"]["true_peak"] and st["engine"]["oversampling"] > 1:
                    ceil_y = self._db_to_y(st["ceiling"], plot)
                    p.setPen(QPen(QColor("#6495FA"), 1.2))
                    p.setBrush(QColor("#151518"))
                    for i, caught in enumerate(self._hist_isp):
                        if caught:
                            x = plot.left() + (i / (n - 1)) * plot.width()
                            diamond = QPainterPath()
                            diamond.moveTo(x, ceil_y - 4.0)
                            diamond.lineTo(x + 3.2, ceil_y)
                            diamond.lineTo(x, ceil_y + 4.0)
                            diamond.lineTo(x - 3.2, ceil_y)
                            diamond.closeSubpath()
                            p.drawPath(diamond)

        # 7. Target LUFS 琥珀金参考线（可上下拖拽）
        if not self.processor_controls:
            tar_y = self._db_to_y(st["target_lufs"], plot)
            tar_hi = (self._hover_target == "target_lufs" or self._drag_target == "target_lufs")
            tar_col = QColor(COLORS["amber"])
            tar_col.setAlpha(245 if tar_hi else 165)
            p.setPen(QPen(tar_col, 1.8 if tar_hi else 1.2, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(plot.left(), tar_y), QPointF(plot.right(), tar_y))

            tar_pill = QRectF(plot.left() + 8.0, tar_y - 9.0, 118.0, 18.0)
            p.setPen(QPen(tar_col, 1.0))
            p.setBrush(QColor(21, 21, 24, 225))
            p.drawRoundedRect(tar_pill, 3.5, 3.5)
            p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
            p.setPen(tar_col)
            p.drawText(tar_pill, Qt.AlignmentFlag.AlignCenter, f"LUFS {st['target_lufs']:.1f}")

        # 8. Ceiling (dBTP) 珊瑚红砖墙天花板线（可上下拖拽）
        ceil_y = self._db_to_y(st["ceiling"], plot)
        ceil_hi = (self._hover_target == "ceiling" or self._drag_target == "ceiling")
        ceil_col = QColor(COLORS["red"])
        ceil_col.setAlpha(250 if ceil_hi else 200)
        p.setPen(QPen(ceil_col, 2.0 if ceil_hi else 1.4))
        p.drawLine(QPointF(plot.left(), ceil_y), QPointF(plot.right(), ceil_y))

        ceil_pill = QRectF(plot.right() - 122.0, ceil_y - 9.0, 114.0, 18.0)
        p.setPen(QPen(ceil_col, 1.1))
        p.setBrush(QColor(21, 21, 24, 230))
        p.drawRoundedRect(ceil_pill, 3.5, 3.5)
        p.setPen(ceil_col)
        p.drawText(ceil_pill, Qt.AlignmentFlag.AlignCenter, f"CEIL {st['ceiling']:.1f} dBTP")

        # 9. 左上角模式角标 & 悬停/拖拽 HUD
        if is_delta:
            mode_pill = QRectF(plot.left() + 8.0, plot.top() + 8.0, 146.0, 19.0)
            p.setPen(QPen(QColor("#F26D6D"), 1.0))
            p.setBrush(QColor(56, 28, 30, 225))
            p.drawRoundedRect(mode_pill, 3.5, 3.5)
            p.setPen(QColor("#F26D6D"))
            p.drawText(mode_pill, Qt.AlignmentFlag.AlignCenter, "AUDITION: DELTA (Δ)")
        elif st["audition_mode"] == "Unity":
            mode_pill = QRectF(plot.left() + 8.0, plot.top() + 8.0, 152.0, 19.0)
            p.setPen(QPen(QColor("#28D8F0"), 1.0))
            p.setBrush(QColor(24, 46, 56, 225))
            p.drawRoundedRect(mode_pill, 3.5, 3.5)
            p.setPen(QColor("#28D8F0"))
            p.drawText(mode_pill, Qt.AlignmentFlag.AlignCenter, "AUDITION: 1:1 UNITY")

        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            hud_text = ""
            hud_col = QColor("#B4B4BE")
            if self._drag_target == "ceiling":
                hud_text = f"True-Peak Ceiling: {st['ceiling']:.1f} dBTP"
                hud_col = QColor(COLORS["red"])
            elif self._drag_target == "target_lufs":
                hud_text = f"Target Loudness: {st['target_lufs']:.1f} LUFS"
                hud_col = QColor(COLORS["amber"])
            else:
                db_at = self._y_to_db(self._mouse_pos.y(), plot)
                gr_at = max(0.0, (self._mouse_pos.y() - plot.top()) / (plot.height() * 0.72) * 18.0)
                hud_text = f"Cursor {db_at:.1f} dBTP  |  GR -{gr_at:.1f} dB"

            hud_rect = QRectF(plot.left() + 8.0, plot.bottom() - 26.0, 225.0, 19.0)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor(21, 21, 24, 225))
            p.drawRoundedRect(hud_rect, 3.5, 3.5)
            p.setPen(hud_col)
            p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_text)

        p.restore()

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)


# =====================================================================
# 5. 底部卡片 1：Gain & LUFS 响度联动模块 (GainLufsCard)
# =====================================================================
class GainLufsCard(QFrame):
    gainChanged = pyqtSignal(float)
    ceilingChanged = pyqtSignal(float)
    targetLufsChanged = pyqtSignal(float)
    trimChanged = pyqtSignal(float)
    flagsChanged = pyqtSignal(dict)
    matchLufsRequested = pyqtSignal()

    def __init__(self, state: dict, parent=None):
        super().__init__(parent)
        self._accent = COLORS["cyan"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 7, 12, 6)
        h_lay.setSpacing(8)

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {self._accent}; font-size: 9px;")
        h_lay.addWidget(dot)

        title = QLabel("GAIN & LUFS LINK")
        title.setStyleSheet(f"color: {self._accent}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;")
        h_lay.addWidget(title)
        h_lay.addStretch()

        badge = QLabel("ITU-R BS.1770-4")
        badge.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.4px;")
        h_lay.addWidget(badge)
        root.addWidget(header)

        # Body (3 列 x 2 行)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        # Row 1: Gain | Ceiling (dBTP) | Target LUFS
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_gain = KnobParameterCell(
            "Gain", 0.0, 24.0, 4.5, COLORS["cyan"],
            "0.0", "dB", "+24.0",
            format_db_signed, lambda s, fb: float(s), mode="linear"
        )
        self.c_ceiling = KnobParameterCell(
            "Ceiling", -12.0, 0.0, -1.0, COLORS["red"],
            "-12.0", "dBTP", "0.0",
            lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="linear"
        )
        self.c_target = KnobParameterCell(
            "Target LUFS", -30.0, -5.0, -14.0, COLORS["amber"],
            "-30", "LUFS", "-5",
            lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="linear"
        )
        for c in (self.c_gain, self.c_ceiling, self.c_target):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        # Row 2: Output Trim | LUFS Linkage 开关列 | Audition 监听列
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_trim = KnobParameterCell(
            "Out Trim", -6.0, 6.0, 0.0, COLORS["cyan"],
            "-6.0", "dB", "+6.0",
            format_db_signed, lambda s, fb: float(s), mode="linear"
        )
        row2.addWidget(self.c_trim, 1)

        # Column 2: LUFS Linkage
        link_col = QVBoxLayout()
        link_col.setContentsMargins(4, 1, 4, 1)
        link_col.setSpacing(5)
        l_lbl = QLabel("LUFS Link")
        l_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        link_col.addWidget(l_lbl)

        self.btn_auto_lufs = QPushButton("Auto Link")
        self.btn_auto_lufs.setCheckable(True)
        self.btn_auto_lufs.setFixedHeight(20)
        self.btn_auto_lufs.setToolTip("开启后自动微调 Gain 使实时响度锁定 Target LUFS")
        self.btn_auto_lufs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_lufs.clicked.connect(self._emit_flags)
        link_col.addWidget(self.btn_auto_lufs)

        self.btn_ceil_link = QPushButton("Ceil Link")
        self.btn_ceil_link.setCheckable(True)
        self.btn_ceil_link.setFixedHeight(20)
        self.btn_ceil_link.setToolTip("天花板联动：调整 Ceiling 时同步等量补偿 Gain 以保持响度恒定")
        self.btn_ceil_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ceil_link.clicked.connect(self._emit_flags)
        link_col.addWidget(self.btn_ceil_link)

        self.btn_match_now = QPushButton("Match Now")
        self.btn_match_now.setFixedHeight(21)
        self.btn_match_now.setToolTip("根据当前 Integrated LUFS 一键计算并匹配至 Target LUFS")
        self.btn_match_now.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_match_now.clicked.connect(self.matchLufsRequested.emit)
        link_col.addWidget(self.btn_match_now)
        row2.addLayout(link_col, 1)

        # Column 3: Monitoring & DC Filter
        mon_col = QVBoxLayout()
        mon_col.setContentsMargins(4, 1, 4, 1)
        mon_col.setSpacing(5)
        m_lbl = QLabel("Audition")
        m_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        m_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        mon_col.addWidget(m_lbl)

        self.btn_delta = QPushButton("Delta (Δ)")
        self.btn_delta.setCheckable(True)
        self.btn_delta.setFixedHeight(20)
        self.btn_delta.setToolTip("仅监听被限制器削减掉的峰值差值信号 (Pro-L 2 Delta 模式)")
        self.btn_delta.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delta.clicked.connect(lambda checked: self._on_audition_btn("Delta", checked))
        mon_col.addWidget(self.btn_delta)

        self.btn_unity = QPushButton("1:1 Unity")
        self.btn_unity.setCheckable(True)
        self.btn_unity.setFixedHeight(20)
        self.btn_unity.setToolTip("等响度输出衰减监听，消除响度偏差以客观对比动态压缩音质")
        self.btn_unity.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_unity.clicked.connect(lambda checked: self._on_audition_btn("Unity", checked))
        mon_col.addWidget(self.btn_unity)

        self.btn_dc = QPushButton("DC Cut")
        self.btn_dc.setCheckable(True)
        self.btn_dc.setFixedHeight(21)
        self.btn_dc.setToolTip("启用 5Hz 直流偏移高通滤波，防止低频非对称偏移吞噬 True-Peak 余量")
        self.btn_dc.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dc.clicked.connect(self._emit_flags)
        mon_col.addWidget(self.btn_dc)
        row2.addLayout(mon_col, 1)

        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_gain.valueChanged.connect(lambda v: self.gainChanged.emit(round(v, 1)))
        self.c_ceiling.valueChanged.connect(lambda v: self.ceilingChanged.emit(round(v, 1)))
        self.c_target.valueChanged.connect(lambda v: self.targetLufsChanged.emit(round(v, 1)))
        self.c_trim.valueChanged.connect(lambda v: self.trimChanged.emit(round(v, 1)))

        self.syncFromState(state)

    def syncFromState(self, st: dict):
        self._updating = True
        en = bool(st["enabled"])

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

        self.c_gain.setValue(st["gain"])
        self.c_ceiling.setValue(st["ceiling"])
        self.c_target.setValue(st["target_lufs"])
        self.c_trim.setValue(st["trim"])

        for cell in (self.c_gain, self.c_ceiling, self.c_target, self.c_trim):
            cell.setModuleActive(en)

        self.btn_auto_lufs.setChecked(bool(st["lufs_auto_link"]))
        self.btn_ceil_link.setChecked(bool(st["ceil_link"]))
        self.btn_delta.setChecked(st["audition_mode"] == "Delta")
        self.btn_unity.setChecked(st["audition_mode"] == "Unity")
        self.btn_dc.setChecked(bool(st["dc_filter"]))

        def _pill_css(hex_col: str) -> str:
            return f"""
                QPushButton {{
                    background-color: #323238;
                    color: #9A9AA2;
                    border: none;
                    border-radius: 3px;
                    font-size: 10px;
                    font-weight: 600;
                }}
                QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
                QPushButton:checked {{
                    background-color: #182E38;
                    color: {hex_col};
                    border: 1px solid {hex_col};
                }}
            """

        self.btn_auto_lufs.setStyleSheet(_pill_css(COLORS["amber"]))
        self.btn_ceil_link.setStyleSheet(_pill_css(COLORS["cyan"]))
        self.btn_delta.setStyleSheet(_pill_css(COLORS["red"]))
        self.btn_unity.setStyleSheet(_pill_css(COLORS["cyan"]))
        self.btn_dc.setStyleSheet(_pill_css(COLORS["green"]))
        self.btn_match_now.setStyleSheet("""
            QPushButton {
                background-color: #18181B;
                color: #F0A848;
                border: 1px solid #4A3818;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #2A2215; border-color: #F0A848; }
            QPushButton:pressed { background-color: #121215; }
        """)

        self._updating = False

    def _on_audition_btn(self, target_mode: str, checked: bool):
        if self._updating:
            return
        if target_mode == "Delta" and checked:
            self.btn_unity.setChecked(False)
        elif target_mode == "Unity" and checked:
            self.btn_delta.setChecked(False)
        self._emit_flags()

    def _emit_flags(self):
        if self._updating:
            return
        mode = "Normal"
        if self.btn_delta.isChecked():
            mode = "Delta"
        elif self.btn_unity.isChecked():
            mode = "Unity"
        self.flagsChanged.emit({
            "lufs_auto_link": self.btn_auto_lufs.isChecked(),
            "ceil_link": self.btn_ceil_link.isChecked(),
            "audition_mode": mode,
            "dc_filter": self.btn_dc.isChecked(),
        })


# =====================================================================
# 6. 底部卡片 2：True-Peak 限制器引擎与立体声链接 (TruePeakEngineCard)
# =====================================================================
class TruePeakEngineCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["red"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 7, 12, 6)
        h_lay.setSpacing(8)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("TRUE-PEAK ENGINE")
        h_lay.addWidget(self.title_lbl)

        h_lay.addStretch()

        style_lbl = QLabel("Style:")
        self._style_label = style_lbl
        style_lbl.setStyleSheet("color: #8A8A94; font-size: 10px; font-weight: 600;")
        h_lay.addWidget(style_lbl)

        self.style_combo = ChevronComboBox()
        self.style_combo.setFixedWidth(104)
        self.style_combo.addItems(["Transparent", "Punchy", "Dynamic", "Allround", "Aggressive", "Modern"])
        self.style_combo.setToolTip("母带限制算法性格：Transparent 透明无染 / Punchy 保留瞬态 / Modern 现代高响度")
        self.style_combo.currentTextChanged.connect(self._on_style)
        h_lay.addWidget(self.style_combo)

        root.addWidget(header)

        # Body (4 列 x 2 行)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        # Row 1: Lookahead | Attack | Release | Knee
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_lookahead = KnobParameterCell(
            "Lookahead", 0.0, 20.0, 1.50, self._accent,
            "0.0", "ms", "20.0",
            format_ms, lambda s, fb: float(s), mode="power"
        )
        self.c_attack = KnobParameterCell(
            "Attack", 0.05, 100.0, 1.00, self._accent,
            "0.05", "ms", "100",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_release = KnobParameterCell(
            "Release", 1.0, 1000.0, 80.0, self._accent,
            "1.0", "ms", "1000",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_knee = KnobParameterCell(
            "Knee", 0.0, 100.0, 25.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        for c in (self.c_lookahead, self.c_attack, self.c_release, self.c_knee):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        # Row 2: Link Trans | Link Rel | Oversample & ISP | Anti-Clip & ISP Margin
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_link_tr = KnobParameterCell(
            "Link Trans", 0.0, 100.0, 100.0, self._accent,
            "0%", "Stereo", "100%",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        self.c_link_rel = KnobParameterCell(
            "Link Rel", 0.0, 100.0, 75.0, self._accent,
            "0%", "Stereo", "100%",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        row2.addWidget(self.c_link_tr, 1)
        row2.addWidget(self.c_link_rel, 1)
        self._engine_body = b_lay
        self._parameter_row = row1
        self._link_row = row2

        # Col 3: True-Peak & Oversampling
        os_col = QVBoxLayout()
        os_col.setContentsMargins(4, 1, 4, 1)
        os_col.setSpacing(5)
        os_lbl = QLabel("True-Peak")
        os_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        os_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        os_col.addWidget(os_lbl)

        self.btn_isp = QPushButton("ISP On")
        self.btn_isp.setCheckable(True)
        self.btn_isp.setFixedHeight(20)
        self.btn_isp.setToolTip("启用 ITU-R BS.1770 间采样真峰 (True-Peak) 检测与防削波")
        self.btn_isp.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_isp.clicked.connect(self._on_isp)
        os_col.addWidget(self.btn_isp)

        self.os_combo = ChevronComboBox()
        self.os_combo.setFixedHeight(21)
        self.os_combo.addItems(["1x (Off)", "2x FIR", "4x FIR", "8x FIR"])
        self.os_combo.setToolTip("过采样倍率：4x/8x 可精确重建 D/A 转换器间采样峰值")
        self.os_combo.currentIndexChanged.connect(self._on_os_combo)
        os_col.addWidget(self.os_combo)

        self.isp_margin_pill = QLabel("ISP +0.0 dB")
        self.isp_margin_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.isp_margin_pill.setFixedHeight(20)
        self.isp_margin_pill.setStyleSheet(f"""
            background-color: #151518;
            color: #6495FA;
            border: 1px solid #121214;
            border-radius: 3px;
            font-family: {MONO_FONT_CSS};
            font-size: 9px;
            font-weight: 700;
        """)
        os_col.addWidget(self.isp_margin_pill)
        row2.addLayout(os_col, 1)

        # Col 4: Ballistics & Brickwall Guard
        gd_col = QVBoxLayout()
        gd_col.setContentsMargins(4, 1, 4, 1)
        gd_col.setSpacing(5)
        gd_lbl = QLabel("Ballistics")
        gd_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gd_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        gd_col.addWidget(gd_lbl)

        self.btn_auto_rel = QPushButton("Auto Rel")
        self.btn_auto_rel.setCheckable(True)
        self.btn_auto_rel.setFixedHeight(20)
        self.btn_auto_rel.setToolTip("双时间常数程序自适应释放，兼顾瞬态通透度与低频无抽吸")
        self.btn_auto_rel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_rel.clicked.connect(self._on_auto_rel)
        gd_col.addWidget(self.btn_auto_rel)

        self.btn_anti_clip = QPushButton("Anti-Clip")
        self.btn_anti_clip.setCheckable(True)
        self.btn_anti_clip.setFixedHeight(20)
        self.btn_anti_clip.setToolTip("末级安全软削波保护，杜绝极端瞬态突破 Ceiling dBTP")
        self.btn_anti_clip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_anti_clip.clicked.connect(self._on_anti_clip)
        gd_col.addWidget(self.btn_anti_clip)

        self.gr_peak_pill = QLabel("GR 0.0 dB")
        self.gr_peak_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.gr_peak_pill.setFixedHeight(20)
        self.gr_peak_pill.setStyleSheet(f"""
            background-color: #151518;
            color: #F26D6D;
            border: 1px solid #121214;
            border-radius: 3px;
            font-family: {MONO_FONT_CSS};
            font-size: 9px;
            font-weight: 700;
        """)
        gd_col.addWidget(self.gr_peak_pill)
        row2.addLayout(gd_col, 1)

        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_lookahead.valueChanged.connect(lambda v: self._set_field("lookahead", round(v, 2)))
        self.c_attack.valueChanged.connect(lambda v: self._set_field("attack", round(v, 2)))
        self.c_release.valueChanged.connect(lambda v: self._set_field("release", round(v, 1)))
        self.c_knee.valueChanged.connect(lambda v: self._set_field("knee", round(v, 0)))
        self.c_link_tr.valueChanged.connect(lambda v: self._set_field("link_trans", round(v, 0)))
        self.c_link_rel.valueChanged.connect(lambda v: self._set_field("link_rel", round(v, 0)))

        self.syncFromData(self._data)

    def use_processor_controls(self, ceiling: KnobParameterCell) -> None:
        link = QPushButton(I18N.tr("audio.fx.linked", "Stereo Link"), self)
        link.setCheckable(True)
        link.setFixedSize(106, 22)
        link.setToolTip("Apply the same gain reduction to both channels")
        link.clicked.connect(self._on_stereo_link)
        self.stereo_link_button = link
        self._style_label.hide()
        self.style_combo.hide()
        self.power_sw.hide()
        for control in (self.c_attack, self.c_knee, self.c_link_tr, self.c_link_rel,
                        self.btn_isp, self.os_combo, self.btn_auto_rel, self.btn_anti_clip):
            control.hide()
        row = self._parameter_row
        for control in (self.c_lookahead, self.c_release):
            row.removeWidget(control)
        row.setSpacing(10)
        row.addStretch()
        for control in (ceiling, self.c_lookahead, self.c_release):
            control.setMaximumWidth(112)
            row.addWidget(control, 1)
        row.addWidget(link, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch()
        self._engine_body.removeItem(self._link_row)
        self._hide_layout_widgets(self._link_row)
        self.syncFromData(self._data)

    def _hide_layout_widgets(self, layout: QHBoxLayout | QVBoxLayout) -> None:
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()
            if widget is not None:
                widget.hide()
            elif item.layout() is not None:
                self._hide_layout_widgets(item.layout())

    def _on_stereo_link(self, linked: bool) -> None:
        self._data["linked"] = linked
        amount = 100.0 if linked else 0.0
        self._data["link_trans"] = amount
        self._data["link_rel"] = amount
        self._emit()

    def updateRealtimeBadges(self, isp_overshoot_db: float, current_gr_db: float):
        self.isp_margin_pill.setText(f"ISP +{isp_overshoot_db:.2f} dB")
        self.gr_peak_pill.setText(f"GR -{current_gr_db:.1f} dB" if current_gr_db > 0.05 else "GR 0.0 dB")

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
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )
        self.style_combo.setCurrentText(self._data["style"])

        self.c_lookahead.setValue(self._data["lookahead"])
        self.c_attack.setValue(self._data["attack"])
        self.c_release.setValue(self._data["release"])
        self.c_knee.setValue(self._data["knee"])
        self.c_link_tr.setValue(self._data["link_trans"])
        self.c_link_rel.setValue(self._data["link_rel"])
        link = getattr(self, "stereo_link_button", None)
        if link is not None:
            with QSignalBlocker(link):
                link.setChecked(self._data.get("linked", True))

        for cell in (self.c_lookahead, self.c_attack, self.c_release, self.c_knee, self.c_link_tr, self.c_link_rel):
            cell.setModuleActive(en)

        self.btn_isp.setChecked(bool(self._data["true_peak"]))
        self.btn_auto_rel.setChecked(bool(self._data["auto_release"]))
        self.btn_anti_clip.setChecked(bool(self._data["anti_clip"]))

        os_map = {1: 0, 2: 1, 4: 2, 8: 3}
        self.os_combo.setCurrentIndex(os_map.get(self._data["oversampling"], 2))

        def _pill_css(hex_col: str) -> str:
            return f"""
                QPushButton {{
                    background-color: #323238;
                    color: #9A9AA2;
                    border: none;
                    border-radius: 3px;
                    font-size: 10px;
                    font-weight: 600;
                }}
                QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
                QPushButton:checked {{
                    background-color: #182E38;
                    color: {hex_col};
                    border: 1px solid {hex_col};
                }}
            """

        self.btn_isp.setStyleSheet(_pill_css(COLORS["blue"]))
        self.btn_auto_rel.setStyleSheet(_pill_css(COLORS["red"]))
        self.btn_anti_clip.setStyleSheet(_pill_css(COLORS["green"]))
        if link is not None:
            link.setStyleSheet(_pill_css(COLORS["red"]))

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_style(self, style: str):
        self._data["style"] = style
        self._emit()

    def _on_isp(self, checked: bool):
        self._data["true_peak"] = checked
        self._emit()

    def _on_os_combo(self, idx: int):
        factors = [1, 2, 4, 8]
        self._data["oversampling"] = factors[max(0, min(3, idx))]
        self._emit()

    def _on_auto_rel(self, checked: bool):
        self._data["auto_release"] = checked
        self._emit()

    def _on_anti_clip(self, checked: bool):
        self._data["anti_clip"] = checked
        self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 7. 底部卡片 3：Delivery & Dither 交付校验与广播响度矩阵 (DeliveryDitherCard)
# =====================================================================
class DeliveryDitherCard(QFrame):
    ditherChanged = pyqtSignal(dict)
    resetLoudnessRequested = pyqtSignal()

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["green"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 7, 12, 6)
        h_lay.setSpacing(8)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        self.title_lbl = QLabel("DELIVERY & DITHER")
        h_lay.addWidget(self.title_lbl)

        h_lay.addStretch()

        self.compliance_badge = QLabel("PASS · -1.0 dBTP")
        self.compliance_badge.setStyleSheet(f"""
            background-color: #152B22;
            color: #4CD490;
            border: 1px solid #2E8B63;
            border-radius: 3px;
            padding: 1px 6px;
            font-family: {MONO_FONT_CSS};
            font-size: 9px;
            font-weight: 700;
        """)
        h_lay.addWidget(self.compliance_badge)
        root.addWidget(header)

        # Body: 左侧 Dither 量化控制列 + 右侧 2x3 出版交付响度仪表盘
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QHBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(8)

        # Left column: Dither & Reset
        dither_col = QVBoxLayout()
        dither_col.setContentsMargins(2, 1, 2, 1)
        dither_col.setSpacing(5)

        d_lbl = QLabel("Quantize / Dither")
        d_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        dither_col.addWidget(d_lbl)

        self.bit_combo = ChevronComboBox()
        self.bit_combo.setFixedHeight(21)
        self.bit_combo.addItems(["24-bit", "16-bit", "32-bit Float"])
        self.bit_combo.setToolTip("母带导出目标量化位深（CD 交付选 16-bit，流媒体/母带归档选 24-bit）")
        self.bit_combo.currentTextChanged.connect(self._on_bit_depth)
        dither_col.addWidget(self.bit_combo)

        self.shape_combo = ChevronComboBox()
        self.shape_combo.setFixedHeight(21)
        self.shape_combo.addItems(["None", "Triangular", "POW-r 1", "POW-r 2", "POW-r 3"])
        self.shape_combo.setToolTip("心理声学噪声整形曲线，降低低比特量化基底噪声")
        self.shape_combo.currentTextChanged.connect(self._on_shaping)
        dither_col.addWidget(self.shape_combo)

        self.btn_auto_blank = QPushButton("Auto-Blank")
        self.btn_auto_blank.setCheckable(True)
        self.btn_auto_blank.setFixedHeight(20)
        self.btn_auto_blank.setToolTip("检测到数字静音段落时自动关闭抖动底噪")
        self.btn_auto_blank.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_blank.clicked.connect(self._on_auto_blank)
        dither_col.addWidget(self.btn_auto_blank)

        dither_col.addStretch()

        self.btn_reset_lufs = QPushButton("↺ Reset Stats")
        self.btn_reset_lufs.setFixedHeight(22)
        self.btn_reset_lufs.setToolTip("重置 Integrated LUFS、Max True-Peak 与 LRA 统计累计")
        self.btn_reset_lufs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reset_lufs.clicked.connect(self.resetLoudnessRequested.emit)
        self.btn_reset_lufs.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #DCDCE2;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: #3C3C44; color: #FFFFFF; }
            QPushButton:pressed { background-color: #1D1D21; }
        """)
        dither_col.addWidget(self.btn_reset_lufs)

        b_lay.addLayout(dither_col, 1)

        # Right 2 columns: 2x3 Hardware Loudness Telemetry Grid
        grid = QGridLayout()
        grid.setSpacing(5)

        self._cells: dict[str, tuple[QLabel, QLabel]] = {}
        specs = [
            ("integrated", "INTEGRATED (I)", "-14.0 LUFS", "#F0A848"),
            ("max_tp",     "MAX TRUE PEAK",  "-1.00 dBTP", "#6495FA"),
            ("short_term", "SHORT-TERM (S)", "-13.2 LUFS", "#ECECF0"),
            ("lra",        "LOUDNESS RANGE", "5.4 LU",     "#4CD490"),
            ("momentary",  "MOMENTARY (M)",  "-11.8 LUFS", "#ECECF0"),
            ("delta_tar",  "TARGET DELTA",   "0.0 LU",     "#28D8F0"),
        ]

        for idx, (key, cap, init_val, col) in enumerate(specs):
            box = QFrame()
            box.setStyleSheet("""
                QFrame {
                    background-color: #151518;
                    border: 1px solid #121214;
                    border-radius: 3.5px;
                }
            """)
            c_lay = QVBoxLayout(box)
            c_lay.setContentsMargins(6, 4, 6, 4)
            c_lay.setSpacing(1)

            cap_lbl = QLabel(cap)
            cap_lbl.setStyleSheet("color: #787882; font-size: 8.5px; font-weight: 700; border: none; background: transparent;")
            val_lbl = QLabel(init_val)
            val_lbl.setStyleSheet(f"color: {col}; font-family: {MONO_FONT_CSS}; font-size: 10.5px; font-weight: 700; border: none; background: transparent;")

            c_lay.addWidget(cap_lbl)
            c_lay.addWidget(val_lbl)
            self._cells[key] = (cap_lbl, val_lbl)
            grid.addWidget(box, idx // 2, idx % 2)

        b_lay.addLayout(grid, 2)
        root.addWidget(body, 1)

        self.syncFromData(self._data)

    def updateTelemetry(
        self,
        momentary_lufs: float,
        short_term_lufs: float,
        integrated_lufs: float,
        lra_lu: float,
        max_tp_dbtp: float,
        target_lufs: float,
        ceiling_dbtp: float,
    ):
        self._cells["integrated"][1].setText(f"{integrated_lufs:.1f} LUFS")
        self._cells["short_term"][1].setText(f"{short_term_lufs:.1f} LUFS")
        self._cells["momentary"][1].setText(f"{momentary_lufs:.1f} LUFS")
        self._cells["lra"][1].setText(f"{lra_lu:.1f} LU")
        self._cells["max_tp"][1].setText(f"{max_tp_dbtp:+.2f} dBTP")

        delta = integrated_lufs - target_lufs
        self._cells["delta_tar"][1].setText(f"{delta:+.1f} LU")

        tp_ok = (max_tp_dbtp <= ceiling_dbtp + 0.05)
        lufs_ok = (abs(delta) <= 1.0)

        self._cells["max_tp"][1].setStyleSheet(
            f"color: {'#6495FA' if tp_ok else '#FF4848'}; font-family: {MONO_FONT_CSS}; font-size: 10.5px; font-weight: 700; border: none; background: transparent;"
        )
        self._cells["delta_tar"][1].setStyleSheet(
            f"color: {'#4CD490' if lufs_ok else '#F0A848'}; font-family: {MONO_FONT_CSS}; font-size: 10.5px; font-weight: 700; border: none; background: transparent;"
        )

        if not tp_ok:
            self.compliance_badge.setText(f"TP OVER · {max_tp_dbtp:+.2f}")
            self.compliance_badge.setStyleSheet(f"""
                background-color: #381C1E;
                color: #F26D6D;
                border: 1px solid #F26D6D;
                border-radius: 3px;
                padding: 1px 6px;
                font-family: {MONO_FONT_CSS};
                font-size: 9px;
                font-weight: 700;
            """)
        elif lufs_ok:
            self.compliance_badge.setText(f"PASS · {ceiling_dbtp:.1f} dBTP")
            self.compliance_badge.setStyleSheet(f"""
                background-color: #152B22;
                color: #4CD490;
                border: 1px solid #2E8B63;
                border-radius: 3px;
                padding: 1px 6px;
                font-family: {MONO_FONT_CSS};
                font-size: 9px;
                font-weight: 700;
            """)
        else:
            self.compliance_badge.setText(f"LUFS {delta:+.1f} LU")
            self.compliance_badge.setStyleSheet(f"""
                background-color: #3A2E18;
                color: #F0A848;
                border: 1px solid #9A7228;
                border-radius: 3px;
                padding: 1px 6px;
                font-family: {MONO_FONT_CSS};
                font-size: 9px;
                font-weight: 700;
            """)

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
            f"color: {self._accent if en else '#787D8C'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        self.bit_combo.setCurrentText(self._data["bit_depth"])
        self.shape_combo.setCurrentText(self._data["shaping"])
        self.btn_auto_blank.setChecked(bool(self._data["auto_blank"]))

        self.bit_combo.setEnabled(en)
        self.shape_combo.setEnabled(en)
        self.btn_auto_blank.setEnabled(en)

        self.btn_auto_blank.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #182E38;
                color: {COLORS['green']};
                border: 1px solid {COLORS['green']};
            }}
        """)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.ditherChanged.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_bit_depth(self, txt: str):
        self._data["bit_depth"] = txt
        self._emit()

    def _on_shaping(self, txt: str):
        self._data["shaping"] = txt
        self._emit()

    def _on_auto_blank(self, checked: bool):
        self._data["auto_blank"] = checked
        self._emit()


# =====================================================================
# 8. 主面板：母带级 True-Peak 限制器 (Studio True-Peak Limiter)
# =====================================================================
class StudioTruePeakLimiterWidget(QWidget):
    """
    母带级 True-Peak 限制器主界面，提供完整 PyQt6 信号接口供后端 DSP 引擎订阅：
      - bypassChanged(bool)
      - gainChanged(float)
      - ceilingChanged(float)          # 天花板 dBTP
      - targetLufsChanged(float)       # 目标响度 LUFS
      - trimChanged(float)
      - oversamplingChanged(int, bool) # (oversampling_factor, true_peak_isp_enabled)
      - engineChanged(dict)            # 限制器核心算法与包络参数
      - ditherChanged(dict)            # 导出位深与抖动参数
      - auditionChanged(str)           # "Normal" | "Delta" | "Unity"
      - lufsResetRequested()           # 用户请求重置响度/真峰统计
      - stateChanged(dict)             # 完整参数树快照
    """
    bypassChanged = pyqtSignal(bool)
    gainChanged = pyqtSignal(float)
    ceilingChanged = pyqtSignal(float)
    targetLufsChanged = pyqtSignal(float)
    trimChanged = pyqtSignal(float)
    oversamplingChanged = pyqtSignal(int, bool)
    engineChanged = pyqtSignal(dict)
    ditherChanged = pyqtSignal(dict)
    auditionChanged = pyqtSignal(str)
    lufsResetRequested = pyqtSignal()
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Master Bus", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"True-Peak Limiter — {track_name}")
        self.resize(1160, 720)
        self.setMinimumSize(1020, 640)

        self._active_bank = "A"
        self._banks = {
            "A": default_limiter_demo(),
            "B": default_limiter_flat(),
        }

        self._presets = {
            "Streaming Master (-14 LUFS / -1 dBTP)": copy.deepcopy(self._banks["A"]),
            "Flat Safe Bypass": copy.deepcopy(self._banks["B"]),
            "EBU R128 Broadcast (-23 LUFS / -1 dBTP)": {
                **default_limiter_demo(),
                "standard": "EBU R128 (-23 LUFS / -1.0 dBTP)",
                "gain": 1.5,
                "ceiling": -1.0,
                "target_lufs": -23.0,
                "lufs_auto_link": True,
                "engine": {
                    **default_limiter_demo()["engine"],
                    "style": "Transparent",
                    "lookahead": 2.0,
                    "release": 120.0,
                    "oversampling": 4,
                    "true_peak": True,
                },
            },
            "Apple Digital Masters (-16 LUFS / -1 dBTP)": {
                **default_limiter_demo(),
                "standard": "Apple Digital (-16 LUFS / -1.0 dBTP)",
                "gain": 3.0,
                "ceiling": -1.0,
                "target_lufs": -16.0,
                "engine": {
                    **default_limiter_demo()["engine"],
                    "style": "Dynamic",
                    "lookahead": 1.8,
                    "release": 95.0,
                    "oversampling": 8,
                    "true_peak": True,
                },
            },
            "Loud Club / CD (-9 LUFS / -0.3 dBTP)": {
                **default_limiter_demo(),
                "standard": "CD / Club Master (-9 LUFS / -0.3 dBTP)",
                "gain": 8.5,
                "ceiling": -0.3,
                "target_lufs": -9.0,
                "engine": {
                    **default_limiter_demo()["engine"],
                    "style": "Modern",
                    "lookahead": 1.0,
                    "attack": 0.6,
                    "release": 45.0,
                    "oversampling": 8,
                    "true_peak": True,
                },
                "dither": {
                    "enabled": True,
                    "bit_depth": "16-bit",
                    "shaping": "POW-r 3",
                    "auto_blank": True,
                },
            },
        }

        # 演示模拟状态缓存
        self._anim_t = 0.0
        self._sim_integrated = -14.2
        self._sim_max_tp = -1.02

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 演示动态模拟定时器（接入真实后端 update_meters 后自动停止）
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_meters)
        self._demo_timer.start(40)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ProVerticalMeter, TruePeakOutputMeter, ProLufsMeter, ProTruePeakDisplayWidget {{
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
        # 1. 顶部主控与出版交付配置栏
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

        title_lbl = QLabel(f"TRUE-PEAK LIMITER  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(8)

        # 预设管理
        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存当前母带限制器配置为预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
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

        # 出版交付标准快速切换
        std_lbl = QLabel("Delivery:")
        self._delivery_label = std_lbl
        std_lbl.setStyleSheet("color: #8A8A94; font-size: 10px; font-weight: 600;")
        tb_lay.addWidget(std_lbl)

        self.std_combo = ChevronComboBox()
        self.std_combo.setFixedWidth(190)
        self.std_combo.addItems(list(DELIVERY_STANDARDS.keys()))
        self.std_combo.setToolTip("选择出版与流媒体交付硬标准，自动配置 Target LUFS、Ceiling dBTP 与过采样")
        self.std_combo.currentTextChanged.connect(self._on_standard_changed)
        tb_lay.addWidget(self.std_combo)

        tb_lay.addSpacing(8)

        # 真峰过采样分段选择器 (1x / 2x / 4x / 8x)
        os_lbl = QLabel("OS:")
        os_lbl.setStyleSheet("color: #8A8A94; font-size: 10px; font-weight: 600;")
        tb_lay.addWidget(os_lbl)

        self._os_btns: dict[int, QPushButton] = {}
        os_wrap = QFrame()
        os_wrap.setStyleSheet("background-color: #18181B; border: 1px solid #121214; border-radius: 3px;")
        os_lay = QHBoxLayout(os_wrap)
        os_lay.setContentsMargins(2, 2, 2, 2)
        os_lay.setSpacing(2)
        for factor in (1, 2, 4, 8):
            btn = QPushButton(f"{factor}x")
            btn.setFixedSize(26, 18)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, f=factor: self._on_os_factor_changed(f))
            os_lay.addWidget(btn)
            self._os_btns[factor] = btn
        tb_lay.addWidget(os_wrap)

        self.latency_badge = QLabel("4x FIR · 1.5 ms")
        self.latency_badge.setStyleSheet(f"color: #6495FA; font-family: {MONO_FONT_CSS}; font-size: 9.5px; font-weight: 600;")
        tb_lay.addWidget(self.latency_badge)

        tb_lay.addStretch()

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

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_limiter)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：Input | True-Peak 滚动波形与 GR 画布 | GR | dBTP | LUFS
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
        in_col = self._build_meter_column("Input")
        self.meter_input = ProVerticalMeter(mode="level", show_ticks=True)
        in_col.addWidget(self.meter_input, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(in_col)
        up_lay.addWidget(_vsep())

        # 2.2 实时滚动波形 & Gain Reduction 画布
        self.tp_display = ProTruePeakDisplayWidget()
        self.tp_display.ceilingDragged.connect(self._on_ceiling_changed)
        self.tp_display.targetLufsDragged.connect(self._on_target_lufs_changed)
        up_lay.addWidget(self.tp_display, 1)
        up_lay.addWidget(_vsep())

        # 2.3 Gain Reduction 衰减表
        gr_col = self._build_meter_column("GR")
        self.meter_gr = ProVerticalMeter(mode="gr", show_ticks=True, accent_color=COLORS["red"])
        gr_col.addWidget(self.meter_gr, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(gr_col)
        up_lay.addWidget(_vsep())

        # 2.4 True-Peak (dBTP) 输出表
        tp_col = self._build_meter_column("True Peak")
        self.meter_tp = TruePeakOutputMeter()
        tp_col.addWidget(self.meter_tp, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(tp_col)
        self._lufs_separator = _vsep()
        up_lay.addWidget(self._lufs_separator)

        # 2.5 LUFS 响度表
        lufs_col = self._build_meter_column("LUFS")
        self._lufs_label = lufs_col.itemAt(0).widget()
        self.meter_lufs = ProLufsMeter()
        lufs_col.addWidget(self.meter_lufs, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(lufs_col)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部三联母带处理卡片矩阵 (3 : 4 : 3 视觉重心比例)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        st = self._current_state()
        self.gain_lufs_card = GainLufsCard(st)
        self.gain_lufs_card.gainChanged.connect(self._on_gain_changed)
        self.gain_lufs_card.ceilingChanged.connect(self._on_ceiling_changed)
        self.gain_lufs_card.targetLufsChanged.connect(self._on_target_lufs_changed)
        self.gain_lufs_card.trimChanged.connect(self._on_trim_changed)
        self.gain_lufs_card.flagsChanged.connect(self._on_gain_flags_changed)
        self.gain_lufs_card.matchLufsRequested.connect(self._on_match_lufs_now)

        self.engine_card = TruePeakEngineCard(st["engine"])
        self.engine_card.changed.connect(self._on_engine_changed)

        self.delivery_card = DeliveryDitherCard(st["dither"])
        self.delivery_card.ditherChanged.connect(self._on_dither_changed)
        self.delivery_card.resetLoudnessRequested.connect(self._on_reset_loudness)

        cards_row.addWidget(self.gain_lufs_card, 3)
        cards_row.addWidget(self.engine_card, 4)
        cards_row.addWidget(self.delivery_card, 3)

        main_lay.addLayout(cards_row, 4)

    def _build_meter_column(self, header_text: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(2, 4, 2, 2)
        col.setSpacing(4)
        lbl = QLabel(header_text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #787882; font-size: 10px; font-weight: 700; letter-spacing: 0.3px;")
        col.addWidget(lbl)
        return col

    # -----------------------------------------------------------------
    # 状态同步与双向交互逻辑
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_top_bar_styles(self):
        st = self._current_state()
        os_factor = st["engine"]["oversampling"]
        tp_on = st["engine"]["true_peak"]

        for f, btn in self._os_btns.items():
            if f == os_factor:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #18263C;
                        color: #6495FA;
                        border: 1px solid #3D64B8;
                        border-radius: 2.5px;
                        font-size: 10px;
                        font-weight: 700;
                    }
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: transparent;
                        color: #9A9AA2;
                        border: none;
                        font-size: 10px;
                        font-weight: 600;
                    }
                    QPushButton:hover { color: #FFFFFF; }
                """)

        lat = OS_LATENCY_MS.get(os_factor, 1.5)
        if os_factor == 1:
            self.latency_badge.setText("1x · 0.0 ms")
            self.latency_badge.setStyleSheet(f"color: #787882; font-family: {MONO_FONT_CSS}; font-size: 9.5px; font-weight: 600;")
        else:
            isp_tag = "ISP" if tp_on else "SMP"
            self.latency_badge.setText(f"{os_factor}x {isp_tag} · {lat:.1f} ms")
            self.latency_badge.setStyleSheet(f"color: #6495FA; font-family: {MONO_FONT_CSS}; font-size: 9.5px; font-weight: 600;")

        active_ab = """
            QPushButton {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
            }
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
        st = self._current_state()
        if hasattr(self.engine_card, "stereo_link_button"):
            st.update(gain=0.0, trim=0.0, ceil_link=False, audition_mode="Normal")
            st["engine"].update(oversampling=4, true_peak=True, enabled=True)
        self.power_switch.setChecked(st["enabled"])

        self.std_combo.blockSignals(True)
        self.std_combo.setCurrentText(st["standard"])
        self.std_combo.blockSignals(False)

        self._update_top_bar_styles()
        self.gain_lufs_card.syncFromState(st)
        self.engine_card.syncFromData(st["engine"])
        self.delivery_card.syncFromData(st["dither"])

        self.meter_tp.setCeiling(st["ceiling"])
        self.meter_lufs.setTarget(st["target_lufs"])
        self.tp_display.setState(st)

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

    def _on_standard_changed(self, std_name: str):
        st = self._current_state()
        st["standard"] = std_name
        spec = DELIVERY_STANDARDS.get(std_name)
        if spec is not None:
            st["target_lufs"] = spec["target_lufs"]
            st["ceiling"] = spec["ceiling_dbtp"]
            st["engine"]["oversampling"] = spec["oversampling"]
            st["engine"]["true_peak"] = spec["true_peak"]
            self.targetLufsChanged.emit(st["target_lufs"])
            self.ceilingChanged.emit(st["ceiling"])
            self.oversamplingChanged.emit(spec["oversampling"], spec["true_peak"])
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def _on_os_factor_changed(self, factor: int):
        st = self._current_state()
        if st["engine"]["oversampling"] != factor:
            st["engine"]["oversampling"] = factor
            self._refresh_all_views()
            self.oversamplingChanged.emit(factor, bool(st["engine"]["true_peak"]))
            self.engineChanged.emit(copy.deepcopy(st["engine"]))
            self.stateChanged.emit(self.get_state())

    def _on_gain_changed(self, val: float):
        st = self._current_state()
        st["gain"] = val
        self.gainChanged.emit(val)
        self.stateChanged.emit(self.get_state())

    def _on_ceiling_changed(self, val: float):
        st = self._current_state()
        old_ceil = st["ceiling"]
        st["ceiling"] = val
        st["standard"] = "Custom Delivery Target"

        # Ceiling Link：降低天花板时等量联动补偿 Gain
        if st["ceil_link"]:
            diff = val - old_ceil
            st["gain"] = max(0.0, min(24.0, round(st["gain"] + diff, 1)))
            self.gainChanged.emit(st["gain"])

        self._refresh_all_views()
        self.ceilingChanged.emit(val)
        self.stateChanged.emit(self.get_state())

    def _on_target_lufs_changed(self, val: float):
        st = self._current_state()
        st["target_lufs"] = val
        st["standard"] = "Custom Delivery Target"
        self._refresh_all_views()
        self.targetLufsChanged.emit(val)
        self.stateChanged.emit(self.get_state())

    def _on_trim_changed(self, val: float):
        st = self._current_state()
        st["trim"] = val
        self.trimChanged.emit(val)
        self.stateChanged.emit(self.get_state())

    def _on_gain_flags_changed(self, flags: dict):
        st = self._current_state()
        old_aud = st["audition_mode"]
        st.update(flags)
        self._refresh_all_views()
        if st["audition_mode"] != old_aud:
            self.auditionChanged.emit(st["audition_mode"])
        self.stateChanged.emit(self.get_state())

    def _on_match_lufs_now(self):
        st = self._current_state()
        delta = st["target_lufs"] - self._sim_integrated
        new_gain = max(0.0, min(24.0, round(st["gain"] + delta, 1)))
        st["gain"] = new_gain
        self._sim_integrated = st["target_lufs"]
        self._refresh_all_views()
        self.gainChanged.emit(new_gain)
        self.stateChanged.emit(self.get_state())

    def _on_engine_changed(self, engine_data: dict):
        st = self._current_state()
        old_os = st["engine"]["oversampling"]
        old_tp = st["engine"]["true_peak"]
        st["engine"] = engine_data
        self._refresh_all_views()
        if engine_data["oversampling"] != old_os or engine_data["true_peak"] != old_tp:
            self.oversamplingChanged.emit(engine_data["oversampling"], bool(engine_data["true_peak"]))
        self.engineChanged.emit(copy.deepcopy(engine_data))
        self.stateChanged.emit(self.get_state())

    def _on_dither_changed(self, dither_data: dict):
        st = self._current_state()
        st["dither"] = dither_data
        self.ditherChanged.emit(copy.deepcopy(dither_data))
        self.stateChanged.emit(self.get_state())

    def _on_reset_loudness(self):
        self._sim_integrated = -36.0
        self._sim_max_tp = -48.0
        self.meter_input.resetPeaks()
        self.meter_gr.resetPeaks()
        self.meter_tp.resetPeaks()
        self.lufsResetRequested.emit()

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
        name, ok = QInputDialog.getText(self, "保存 True-Peak Limiter 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 演示动态模拟（接入真实音频引擎 update_meters 后自动停止）
    # -----------------------------------------------------------------
    def _tick_demo_meters(self):
        self._anim_t += 0.09
        st = self._current_state()
        en = st["enabled"] and st["engine"]["enabled"]

        # 模拟具有底鼓/军鼓瞬态的母带总线信号 (-22 ~ -5 dBFS)
        transient = 5.5 if (int(self._anim_t * 4.2) % 6 == 0) else 0.0
        in_db = (
            -12.5
            + 4.5 * math.sin(self._anim_t * 1.1)
            + 2.2 * math.cos(self._anim_t * 2.9)
            + transient
            + random.uniform(-0.9, 0.9)
        )
        in_db = max(-45.0, min(-1.5, in_db))

        # 若开启 LUFS Auto-Link，模拟缓慢闭环增益微调
        if en and st["lufs_auto_link"]:
            err = st["target_lufs"] - self._sim_integrated
            if abs(err) > 0.15:
                st["gain"] = max(0.0, min(24.0, round(st["gain"] + err * 0.04, 1)))
                self.gain_lufs_card.c_gain.setValue(st["gain"])

        boosted_db = in_db + (st["gain"] if st["enabled"] else 0.0)
        isp_excursion = random.uniform(0.15, 0.65) if transient > 0 else random.uniform(0.02, 0.18)
        raw_tp = boosted_db + isp_excursion

        ceiling = st["ceiling"]
        trim = st["trim"] if st["enabled"] else 0.0

        if en:
            detect_peak = raw_tp if st["engine"]["true_peak"] else boosted_db
            gr_db = max(0.0, detect_peak - ceiling)
            out_sp = min(ceiling, boosted_db) + trim
            if st["engine"]["true_peak"] and st["engine"]["oversampling"] > 1:
                out_tp = min(ceiling, raw_tp - gr_db) + trim
                isp_caught = (boosted_db <= ceiling) and (raw_tp > ceiling)
            else:
                out_tp = out_sp + isp_excursion
                isp_caught = False
        else:
            gr_db = 0.0
            out_sp = boosted_db + trim
            out_tp = raw_tp + trim
            isp_caught = False

        # 模拟 ITU-R BS.1770 响度演变
        momentary = out_sp - 4.2 + random.uniform(-0.4, 0.4)
        short_term = out_sp - 4.8
        if self._sim_integrated < -35.0:
            self._sim_integrated = short_term
        else:
            self._sim_integrated = self._sim_integrated * 0.96 + short_term * 0.04
        self._sim_max_tp = max(self._sim_max_tp, out_tp)
        lra_lu = 4.8 + 0.6 * math.sin(self._anim_t * 0.25)

        self._push_visual_frame(
            input_db=in_db,
            boosted_db=boosted_db,
            output_dbtp=out_tp,
            sample_peak_db=out_sp,
            gr_db=gr_db,
            momentary_lufs=momentary,
            short_term_lufs=short_term,
            integrated_lufs=self._sim_integrated,
            lra_lu=lra_lu,
            max_true_peak_dbtp=self._sim_max_tp,
            isp_overshoot_db=isp_excursion,
            isp_caught=isp_caught,
        )

    def _push_visual_frame(
        self,
        input_db: float,
        boosted_db: float,
        output_dbtp: float,
        sample_peak_db: float,
        gr_db: float,
        momentary_lufs: float,
        short_term_lufs: float,
        integrated_lufs: float,
        lra_lu: float,
        max_true_peak_dbtp: float,
        isp_overshoot_db: float,
        isp_caught: bool,
    ):
        st = self._current_state()
        self.meter_input.setValue(input_db)
        self.meter_gr.setValue(gr_db)
        self.meter_tp.setValues(sample_peak_db, output_dbtp)
        self.meter_lufs.setValues(short_term_lufs, integrated_lufs)

        self.tp_display.pushFrame(
            boosted_db=boosted_db,
            output_dbtp=output_dbtp,
            gr_db=gr_db,
            isp_caught=isp_caught,
            integrated_lufs=integrated_lufs,
            max_tp_dbtp=max_true_peak_dbtp,
        )
        self.engine_card.updateRealtimeBadges(isp_overshoot_db, gr_db)
        self.delivery_card.updateTelemetry(
            momentary_lufs=momentary_lufs,
            short_term_lufs=short_term_lufs,
            integrated_lufs=integrated_lufs,
            lra_lu=lra_lu,
            max_tp_dbtp=max_true_peak_dbtp,
            target_lufs=st["target_lufs"],
            ceiling_dbtp=st["ceiling"],
        )

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_limiter(self):
        self._banks[self._active_bank] = default_limiter_flat()
        self._on_reset_loudness()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def use_processor_controls(self) -> None:
        self.engine_card.use_processor_controls(self.gain_lufs_card.c_ceiling)
        self.tp_display.processor_controls = True
        self._lufs_separator.hide()
        self._lufs_label.hide()
        self.meter_lufs.hide()
        self.gain_lufs_card.hide()
        self.delivery_card.hide()
        self._delivery_label.hide()
        self.std_combo.hide()
        self.latency_badge.hide()
        for factor, button in self._os_btns.items():
            button.setVisible(factor == 4)
            button.setEnabled(False)

    def update_processor_meters(
        self, input_db: float, input_true_peak: float | None, output_db: float,
        output_true_peak: float | None, reduction_db: float,
    ) -> None:
        self.meter_input.setValue(input_db)
        self.meter_gr.setValue(reduction_db)
        self.meter_tp.setValues(output_db, -120.0 if output_true_peak is None else output_true_peak)
        overshoot = max(0.0, input_true_peak - input_db) if input_true_peak is not None else 0.0
        self.engine_card.updateRealtimeBadges(overshoot, reduction_db)
        self.tp_display.pushFrame(
            boosted_db=input_db,
            output_dbtp=output_db if output_true_peak is None else output_true_peak,
            gr_db=reduction_db, isp_caught=overshoot > 0.05 and reduction_db > 0.05,
            integrated_lufs=-70.0, max_tp_dbtp=self.meter_tp._max_tp_hold,
        )

    def get_state(self) -> dict:
        state = copy.deepcopy(self._current_state())
        state["engine"].setdefault("linked", True)
        return state

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_meters(
        self,
        input_db: float,
        output_dbtp: float,
        sample_peak_db: float,
        gr_db: float,
        momentary_lufs: float,
        short_term_lufs: float,
        integrated_lufs: float,
        lra_lu: float,
        max_true_peak_dbtp: float,
        isp_overshoot_db: float = 0.0,
    ):
        """
        接入真实音频引擎实时遥测数据（建议每帧 30~60Hz 调用，调用后自动停止内置演示动画）：
          - input_db: 输入峰值电平 (dBFS)
          - output_dbtp: 过采样重建后的瞬时真峰输出 (dBTP)
          - sample_peak_db: 常规采样峰值输出 (dBFS)
          - gr_db: 当前增益衰减量 (正值 dB)
          - momentary_lufs: 400ms 瞬时响度 (LUFS)
          - short_term_lufs: 3s 短期响度 (LUFS)
          - integrated_lufs: 全程积分响度 (LUFS)
          - lra_lu: 响度范围 Loudness Range (LU)
          - max_true_peak_dbtp: 历史最大真峰 (dBTP)
          - isp_overshoot_db: 间采样峰值超出常规采样峰值的差值 (dB)
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()

        st = self._current_state()
        boosted_db = input_db + (st["gain"] if st["enabled"] else 0.0)
        isp_caught = (isp_overshoot_db > 0.05) and (gr_db > 0.05)

        self._sim_integrated = integrated_lufs
        self._sim_max_tp = max_true_peak_dbtp

        self._push_visual_frame(
            input_db=input_db,
            boosted_db=boosted_db,
            output_dbtp=output_dbtp,
            sample_peak_db=sample_peak_db,
            gr_db=gr_db,
            momentary_lufs=momentary_lufs,
            short_term_lufs=short_term_lufs,
            integrated_lufs=integrated_lufs,
            lra_lu=lra_lu,
            max_true_peak_dbtp=max_true_peak_dbtp,
            isp_overshoot_db=isp_overshoot_db,
            isp_caught=isp_caught,
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioTruePeakLimiterWidget("Master Bus")
    win.show()
    sys.exit(app.exec())
