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
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont, QPolygonF
)


# =====================================================================
# 1. 全局色彩系统与声学常量（统一 Fairlight 深色哑光视觉规范）
# =====================================================================
SPEC_FREQ_MIN = 15.0
SPEC_FREQ_MAX = 1200.0

UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

COLORS = {
    "cyan":   "#28D8F0",  # 主控 / A-B 激活 / Output
    "src":    "#F0A848",  # Source Freq / -1 Oct 主题色（琥珀金）
    "oct2":   "#B67CF8",  # -2 Oct / Harmonics 主题色（紫罗兰）
    "drive":  "#F26D6D",  # Drive / Character / Sub Synth 主题色（珊瑚红）
    "filter": "#4CD490",  # Sub LPF / Low Cut 主题色（翡翠绿）
}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def freq_to_note(hz: float) -> str:
    if hz <= 0:
        return ""
    midi = round(69 + 12 * math.log2(hz / 440.0))
    return f"{NOTE_NAMES[midi % 12]}{(midi // 12) - 1}"


def format_freq(hz: float) -> str:
    if hz >= 1000.0:
        return f"{hz / 1000.0:.2f}K"
    if hz < 40.0:
        return f"{hz:.1f}"
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


def default_subharmonic_demo() -> dict:
    """默认演示预设：底鼓/贝斯低八度胸腔重击 (Waves RBass + LoAir 混合架构)"""
    return {
        "enabled": True,
        "sub_solo": False,
        "source": {
            "enabled": True,
            "octave_mode": "Dual",     # "-1 Oct" | "-2 Oct" | "Dual"
            "freq": 64.0,              # 30.0 ~ 180.0 Hz (谐波源中心频点)
            "bandwidth": 1.6,          # 0.5 ~ 6.0 Q (源检测带通带宽)
            "sub_lpf": 115.0,          # 40.0 ~ 250.0 Hz (生成低频低通截止)
            "oct1_level": 85.0,        # 0 ~ 100 % (-1 Octave, f/2)
            "oct2_level": 42.0,        # 0 ~ 100 % (-2 Octave, f/4)
            "harmonics": 30.0,         # 0 ~ 100 % (RBass 上行心理声学泛音)
        },
        "drive": {
            "enabled": True,
            "engine_mode": "RBass",    # "RBass" | "LoAir" | "Subsynth"
            "drive_db": 7.5,           # 0.0 ~ 24.0 dB (谐波激励驱动)
            "saturation": 35.0,        # 0 ~ 100 % (偶次/奇次非线性饱和度)
            "punch": 45.0,             # 0 ~ 100 % (瞬态包络收紧度)
            "attack": 4.5,             # 0.5 ~ 80.0 ms
            "release": 115.0,          # 20.0 ~ 1000.0 ms
            "phase": 0.0,              # 0 ~ 180 deg (相位对齐补偿)
        },
        "output": {
            "direct_level": 100.0,     # 0 ~ 100 % (直达干声比例)
            "sub_level": 55.0,         # 0 ~ 100 % (低八度湿声比例)
            "mix": 65.0,               # 0 ~ 100 % (全局干湿混合 Mix)
            "low_cut": 22.0,           # 15.0 ~ 45.0 Hz (次声波保护高通)
            "width": 0.0,              # 0 ~ 100 % (0% = 超低频单声道收束)
            "trim_db": 0.0,            # -12.0 ~ +12.0 dB (输出电平微调)
        },
    }


def default_subharmonic_flat() -> dict:
    st = default_subharmonic_demo()
    st["source"]["oct1_level"] = 50.0
    st["source"]["oct2_level"] = 0.0
    st["source"]["harmonics"] = 0.0
    st["source"]["octave_mode"] = "-1 Oct"
    st["drive"]["drive_db"] = 0.0
    st["drive"]["saturation"] = 0.0
    st["drive"]["punch"] = 0.0
    st["drive"]["phase"] = 0.0
    st["output"]["mix"] = 0.0
    st["output"]["trim_db"] = 0.0
    return st


# =====================================================================
# 2. 控件：现代发光电源开关（支持模块专属主题色）
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
# 3. 控件：内嵌可编辑数字胶囊
# =====================================================================
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
                font-size: 10.5px;
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
                self._text.replace("+", "")
                .replace("dB", "")
                .replace("%", "")
                .replace("ms", "")
                .replace("Hz", "")
                .replace("°", "")
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
# 4. 控件：拉丝阳极铝质感旋钮（支持 linear / log / bipolar 映射）
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
        if self._mode == "bipolar" and self._min < 0.0 < self._max:
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

        arc_r = 14.8
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深色凹槽底环
        p.setPen(QPen(QColor("#141417"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 2. 双向零点刻度线 (12点钟方向)
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.0), QPointF(cx, cy - arc_r + 0.7))

        # 3. 模块主题色光环
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

        # 4. 旋钮主体
        knob_r = 11.0
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 5. 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.2
        iy1 = cy - math.sin(rad) * 3.2
        ix2 = cx + math.cos(rad) * (knob_r - 1.4)
        iy2 = cy - math.sin(rad) * (knob_r - 1.4)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 5. 控件：垂直高精电平表（支持 Input/Output 三色梯度 & Sub Synth 合成能量表）
# =====================================================================
class ProVerticalMeter(QWidget):
    def __init__(
        self,
        title: str = "",
        mode: str = "level",      # "level" (标准三色电平) | "sub" (次低频合成能量表)
        min_db: float = -55.0,
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
        self._ticks = ticks or [0, -5, -10, -15, -20, -30, -40, -50]
        self._show_ticks = show_ticks
        self._accent = QColor(accent_color)

        self._value_db = min_db
        self._peak_db = min_db
        self._clipped = False
        self._active = True

        self.setFixedWidth(46 if show_ticks else 18)
        self.setMinimumHeight(180)

    def setValue(self, db: float):
        self._value_db = max(self._min_db, min(self._max_db, db))
        if self._value_db >= self._peak_db:
            self._peak_db = self._value_db
        else:
            self._peak_db = max(self._value_db, self._peak_db - 0.35)
        if self._mode == "level" and db >= -0.1:
            self._clipped = True
        self.update()

    def setActive(self, active: bool):
        if self._active != active:
            self._active = active
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._clipped = False
            self._peak_db = self._value_db
            self.update()

    def _db_to_y(self, db: float, bar: QRectF) -> float:
        clamped = max(self._min_db, min(self._max_db, db))
        if clamped >= -20.0:
            norm = 0.52 + ((clamped + 20.0) / 20.0) * 0.48
        else:
            norm = ((clamped - self._min_db) / (-20.0 - self._min_db)) * 0.52
        return bar.bottom() - norm * bar.height()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 10.0
        bot_pad = 18.0 if self._title else 6.0
        bar_w = 10.0
        bar_x = (self.width() - bar_w - 3.0) if self._show_ticks else (self.width() - bar_w) / 2.0
        bar = QRectF(bar_x, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))

        # 1. 刻度文字
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

        # 2. 表槽底色
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        # 3. 电平填充
        if self._active:
            y_val = self._db_to_y(self._value_db, bar)
            fill_h = bar.bottom() - y_val
            if fill_h > 1.0:
                fill_rect = QRectF(bar.left() + 1.0, y_val, bar.width() - 2.0, fill_h - 0.5)
                grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                if self._mode == "level":
                    grad.setColorAt(0.0,  QColor("#26C281"))
                    grad.setColorAt(0.50, QColor("#4CD490"))
                    grad.setColorAt(0.53, QColor("#E8C538"))
                    grad.setColorAt(0.75, QColor("#F0A848"))
                    grad.setColorAt(0.78, QColor("#F26D6D"))
                    grad.setColorAt(1.0,  QColor("#FF4848"))
                else:
                    # Sub Synth 专属能量梯度（紫罗兰 -> 冰晶青 -> 珊瑚红）
                    grad.setColorAt(0.0,  QColor("#6E48AA"))
                    grad.setColorAt(0.45, QColor("#28D8F0"))
                    grad.setColorAt(0.80, QColor("#F0A848"))
                    grad.setColorAt(1.0,  QColor("#F26D6D"))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(grad)
                p.drawRoundedRect(fill_rect, 1.2, 1.2)

            if self._peak_db > self._min_db + 0.5:
                py = self._db_to_y(self._peak_db, bar)
                p.setPen(QPen(QColor("#F26D6D") if self._peak_db > -10.0 else QColor("#F0F2F6"), 1.4))
                p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

            if self._mode == "level":
                clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
                p.drawRoundedRect(clip_rect, 1.0, 1.0)

        # 4. 精细横向刻度栅格遮罩
        p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
        step_y = 3.0
        gy = bar.top() + step_y
        while gy < bar.bottom():
            p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
            gy += step_y

        # 5. 底部标识
        if self._title:
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.setPen(QColor("#9A9AA2") if self._active else QColor("#55555C"))
            p.drawText(QRectF(0, self.height() - 16, self.width(), 15), Qt.AlignmentFlag.AlignCenter, self._title)


# =====================================================================
# 6. 控件：交互式低频频谱与谐波合成可视化图 (ProSubharmonicGraphWidget)
# =====================================================================
class ProSubharmonicGraphWidget(QWidget):
    """
    专为次低频增强设计的交互式频谱与谐波向量图 (15 Hz ~ 1200 Hz)：
      - 拖拽 SRC 节点：水平调节谐波源频点 (30~180 Hz)，垂直调节驱动 (0~24 dB)，滚轮调节检测带宽 (BW)
      - 拖拽 LPF 节点：水平调节低通滤波器截止频率 (40~250 Hz)
      - 拖拽 CUT 节点：水平调节次声波保护高通截止频率 (15~45 Hz)
      - 实时展示 -1 Oct (f/2) 与 -2 Oct (f/4) 生成谐波落点与向量弧
    """
    sourceFreqDragged = pyqtSignal(float)
    driveDragged = pyqtSignal(float)
    bandwidthScrolled = pyqtSignal(float)
    subLpfDragged = pyqtSignal(float)
    lowCutDragged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 240)
        self.setMouseTracking(True)

        self._state = default_subharmonic_demo()
        self._show_dry_spec = True
        self._show_wet_spec = True
        self._show_peak = True
        self._show_vectors = True

        self._dragging_target = ""   # "src" | "lpf" | "cut" | ""
        self._hover_target = ""
        self._mouse_pos: QPointF | None = None

        self._ml = 36.0
        self._mr = 36.0
        self._mt = 10.0
        self._mb = 22.0

        self._btn_dry_rect = QRectF()
        self._btn_wet_rect = QRectF()
        self._btn_peak_rect = QRectF()
        self._btn_vect_rect = QRectF()

        # 实时频谱缓冲 (15 Hz ~ 1200 Hz 对数分布)
        self._num_bins = 220
        self._spec_freqs = [
            SPEC_FREQ_MIN * ((SPEC_FREQ_MAX / SPEC_FREQ_MIN) ** (i / (self._num_bins - 1)))
            for i in range(self._num_bins)
        ]
        self._spec_dry = [-55.0] * self._num_bins
        self._spec_wet = [-55.0] * self._num_bins
        self._spec_peak = [-50.0] * self._num_bins
        self._sub_energy_norm = 0.0  # 0.0 ~ 1.0 实时低八度合成脉冲强度

    def setState(self, state: dict):
        self._state = state
        self.update()

    def update_spectrum(
        self,
        freqs_hz: list[float],
        dry_db: list[float],
        wet_db: list[float],
        peak_db: list[float] | None = None,
        sub_energy_norm: float | None = None,
    ):
        self._spec_freqs = freqs_hz
        self._spec_dry = dry_db
        self._spec_wet = wet_db
        self._spec_peak = peak_db if peak_db is not None else wet_db
        if sub_energy_norm is not None:
            self._sub_energy_norm = max(0.0, min(1.0, sub_energy_norm))
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(40.0, self.width() - self._ml - self._mr),
            max(40.0, self.height() - self._mt - self._mb),
        )

    def _freq_to_x(self, freq: float, plot: QRectF) -> float:
        f = max(SPEC_FREQ_MIN, min(SPEC_FREQ_MAX, freq))
        norm = math.log10(f / SPEC_FREQ_MIN) / math.log10(SPEC_FREQ_MAX / SPEC_FREQ_MIN)
        return plot.left() + norm * plot.width()

    def _x_to_freq(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return SPEC_FREQ_MIN * ((SPEC_FREQ_MAX / SPEC_FREQ_MIN) ** norm)

    def _drive_to_y(self, drive_db: float, plot: QRectF) -> float:
        # 0 dB -> 偏下基准线 (0.68), 24 dB -> 顶部 (0.14)
        norm = max(0.0, min(1.0, drive_db / 24.0))
        return plot.top() + (0.68 - norm * 0.54) * plot.height()

    def _y_to_drive(self, y: float, plot: QRectF) -> float:
        rel = (y - plot.top()) / plot.height()
        norm = max(0.0, min(1.0, (0.68 - rel) / 0.54))
        return norm * 24.0

    def _spec_db_to_y(self, db: float, plot: QRectF) -> float:
        norm = (0.0 - db) / 80.0
        return plot.top() + max(0.0, min(1.0, norm)) * plot.height()

    def _zero_db_y(self, plot: QRectF) -> float:
        return plot.top() + 0.68 * plot.height()

    def _src_node_pos(self, plot: QRectF) -> QPointF:
        src = self._state["source"]
        drv = self._state["drive"]
        return QPointF(self._freq_to_x(src["freq"], plot), self._drive_to_y(drv["drive_db"], plot))

    def _lpf_node_pos(self, plot: QRectF) -> QPointF:
        src = self._state["source"]
        return QPointF(self._freq_to_x(src["sub_lpf"], plot), self._zero_db_y(plot))

    def _cut_node_pos(self, plot: QRectF) -> QPointF:
        out = self._state["output"]
        return QPointF(self._freq_to_x(out["low_cut"], plot), self._zero_db_y(plot))

    # -----------------------------------------------------------------
    # 交互事件处理
    # -----------------------------------------------------------------
    def _hit_test(self, pos: QPointF, plot: QRectF) -> str:
        if not self._state["enabled"]:
            return ""
        src_pt = self._src_node_pos(plot)
        if math.hypot(pos.x() - src_pt.x(), pos.y() - src_pt.y()) <= 13.0:
            return "src"
        lpf_pt = self._lpf_node_pos(plot)
        if math.hypot(pos.x() - lpf_pt.x(), pos.y() - lpf_pt.y()) <= 11.0:
            return "lpf"
        cut_pt = self._cut_node_pos(plot)
        if math.hypot(pos.x() - cut_pt.x(), pos.y() - cut_pt.y()) <= 11.0:
            return "cut"
        return ""

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_target = ""
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_dry_rect.contains(pos):
                self._show_dry_spec = not self._show_dry_spec
                self.update()
                return
            if self._btn_wet_rect.contains(pos):
                self._show_wet_spec = not self._show_wet_spec
                self.update()
                return
            if self._btn_peak_rect.contains(pos):
                self._show_peak = not self._show_peak
                self.update()
                return
            if self._btn_vect_rect.contains(pos):
                self._show_vectors = not self._show_vectors
                self.update()
                return

            plot = self._plot_rect()
            hit = self._hit_test(pos, plot)
            if hit:
                self._dragging_target = hit
                self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._dragging_target == "src":
            mods = event.modifiers()
            if not (mods & Qt.KeyboardModifier.ControlModifier):
                new_f = max(30.0, min(180.0, round(self._x_to_freq(event.position().x(), plot), 1)))
                self.sourceFreqDragged.emit(new_f)
            if not (mods & Qt.KeyboardModifier.AltModifier):
                new_d = max(0.0, min(24.0, round(self._y_to_drive(event.position().y(), plot), 1)))
                self.driveDragged.emit(new_d)
            return

        if self._dragging_target == "lpf":
            new_lpf = max(40.0, min(250.0, round(self._x_to_freq(event.position().x(), plot), 1)))
            self.subLpfDragged.emit(new_lpf)
            return

        if self._dragging_target == "cut":
            new_cut = max(15.0, min(45.0, round(self._x_to_freq(event.position().x(), plot), 1)))
            self.lowCutDragged.emit(new_cut)
            return

        hit = self._hit_test(event.position(), plot)
        self._hover_target = hit
        if hit == "src":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif hit in ("lpf", "cut"):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_target:
            self._dragging_target = ""
            self.update()

    def wheelEvent(self, event):
        if self._hover_target == "src" or self._dragging_target == "src":
            delta = event.angleDelta().y() / 120.0
            cur_bw = self._state["source"]["bandwidth"]
            new_bw = max(0.5, min(6.0, round(cur_bw + delta * 0.2, 1)))
            self.bandwidthScrolled.emit(new_bw)
            event.accept()

    # -----------------------------------------------------------------
    # 绘制频谱、滤波器包络、八度谐波映射弧与交互控制点
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 画布纯平底色
        p.fillRect(plot, QColor("#1D1D20"))

        # 2. 水平 dB 网格线
        grid_pen = QPen(QColor("#26262A"), 1.0)
        zero_pen = QPen(QColor("#383840"), 1.1)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        left_ticks = [0, -10, -20, -30, -40, -50, -60, -70, -80]
        right_ticks = ["+24", "", "+12", "", "0", "", "-12", "", "-24"]
        zero_y = self._zero_db_y(plot)

        for i, l_val in enumerate(left_ticks):
            y = plot.top() + (i / (len(left_ticks) - 1)) * plot.height()
            p.setPen(grid_pen)
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

        # 滤波器 0 dB 基准线
        p.setPen(zero_pen)
        p.drawLine(QPointF(plot.left(), zero_y), QPointF(plot.right(), zero_y))

        # 3. 垂直对数频率刻度线（专注次低频与低中频解析度）
        freq_ticks = [
            (20.0, "20"), (30.0, "30"), (40.0, "40"), (50.0, "50"),
            (65.0, "65"), (80.0, "80"), (100.0, "100"), (150.0, "150"),
            (250.0, "250"), (400.0, "400"), (650.0, "650"), (1000.0, "1K"),
        ]
        p.setPen(QColor("#9A9AA2"))
        p.drawText(QRectF(plot.left(), plot.bottom() + 4, 24, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Hz")
        for f_val, f_label in freq_ticks:
            x = self._freq_to_x(f_val, plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#9A9AA2"))
            p.drawText(QRectF(x - 18, plot.bottom() + 4, 36, 16), Qt.AlignmentFlag.AlignCenter, f_label)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        st = self._state
        enabled = st["enabled"]
        src = st["source"]
        drv = st["drive"]
        out = st["output"]

        # 4. 实时频谱层（Dry 原声频谱 + Wet 谐波增强后频谱）
        if len(self._spec_freqs) > 1:
            # 4.1 增强后输出频谱 (Wet Sub-Enhanced)
            if self._show_wet_spec:
                wet_path = QPainterPath()
                peak_path = QPainterPath()
                wet_poly = QPolygonF()
                wet_poly.append(QPointF(self._freq_to_x(self._spec_freqs[0], plot), plot.bottom()))

                for i, f_s in enumerate(self._spec_freqs):
                    sx = self._freq_to_x(f_s, plot)
                    wy = self._spec_db_to_y(self._spec_wet[i], plot)
                    py = self._spec_db_to_y(self._spec_peak[i], plot)
                    if i == 0:
                        wet_path.moveTo(sx, wy)
                        peak_path.moveTo(sx, py)
                    else:
                        wet_path.lineTo(sx, wy)
                        peak_path.lineTo(sx, py)
                    wet_poly.append(QPointF(sx, wy))

                wet_poly.append(QPointF(self._freq_to_x(self._spec_freqs[-1], plot), plot.bottom()))

                wet_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
                if enabled:
                    wet_grad.setColorAt(0.0, QColor(40, 216, 240, 75))
                    wet_grad.setColorAt(0.55, QColor(242, 109, 109, 45))
                    wet_grad.setColorAt(1.0, QColor(182, 124, 248, 12))
                else:
                    wet_grad.setColorAt(0.0, QColor(120, 120, 130, 35))
                    wet_grad.setColorAt(1.0, QColor(80, 80, 90, 8))

                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(wet_grad)
                p.drawPolygon(wet_poly)

                if self._show_peak:
                    p.setPen(QPen(QColor(165, 170, 182, 95), 1.0))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawPath(peak_path)

                p.setPen(QPen(QColor("#28D8F0") if enabled else QColor("#7A7A84"), 1.45))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(wet_path)

            # 4.2 原始输入干声频谱对照线 (Dry Input)
            if self._show_dry_spec:
                dry_path = QPainterPath()
                for i, f_s in enumerate(self._spec_freqs):
                    sx = self._freq_to_x(f_s, plot)
                    dy = self._spec_db_to_y(self._spec_dry[i], plot)
                    if i == 0:
                        dry_path.moveTo(sx, dy)
                    else:
                        dry_path.lineTo(sx, dy)
                p.setPen(QPen(QColor(240, 168, 72, 135), 1.15, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(dry_path)

        # 5. 绘制 Sub 通带响应包络 (Low Cut Guard -> Source Bell -> Sub LPF)
        f0 = float(src["freq"])
        bw = float(src["bandwidth"])
        f_lpf = float(src["sub_lpf"])
        f_cut = float(out["low_cut"])
        drv_db = float(drv["drive_db"])

        env_path = QPainterPath()
        fill_env = QPainterPath()
        steps = 260
        for i in range(steps + 1):
            norm = i / steps
            f = SPEC_FREQ_MIN * ((SPEC_FREQ_MAX / SPEC_FREQ_MIN) ** norm)
            x = plot.left() + norm * plot.width()

            # 次声波高通衰减 (4阶 24dB/oct)
            hp_mag = 1.0 / (1.0 + (f_cut / max(1.0, f)) ** 8)
            # Sub 低通衰减 (3阶 18dB/oct)
            lp_mag = 1.0 / (1.0 + (f / max(1.0, f_lpf)) ** 6)
            # 源频点激励钟形轮廓
            oct_dist = math.log2(max(1.0, f) / f0) * bw
            bell_boost = (drv_db / 24.0) * math.exp(-0.5 * oct_dist * oct_dist)

            atten_db = 10.0 * math.log10(max(1e-6, hp_mag * lp_mag))
            y_offset_px = (bell_boost * 0.54 * plot.height()) + (atten_db / 36.0) * (0.30 * plot.height())
            y = max(plot.top() + 6.0, min(plot.bottom() - 2.0, zero_y - y_offset_px))

            if i == 0:
                env_path.moveTo(x, y)
                fill_env.moveTo(x, zero_y)
                fill_env.lineTo(x, y)
            else:
                env_path.lineTo(x, y)
                fill_env.lineTo(x, y)
        fill_env.lineTo(plot.right(), zero_y)
        fill_env.closeSubpath()

        if enabled:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(240, 168, 72, 26))
            p.drawPath(fill_env)
            p.setPen(QPen(QColor("#F0A848"), 1.65))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(env_path)
        else:
            p.setPen(QPen(QColor("#6E6E78"), 1.3))
            p.drawPath(env_path)

        # 6. 绘制低八度谐波映射向量弧 (-1 Oct = f0/2, -2 Oct = f0/4)
        src_pt = self._src_node_pos(plot)
        oct_mode = src["octave_mode"]
        has_oct1 = oct_mode in ("-1 Oct", "Dual") and src["oct1_level"] > 0.5
        has_oct2 = oct_mode in ("-2 Oct", "Dual") and src["oct2_level"] > 0.5

        if enabled and self._show_vectors and src["enabled"]:
            targets = []
            if has_oct1:
                targets.append(("-1 OCT", f0 * 0.5, src["oct1_level"] / 100.0, COLORS["src"]))
            if has_oct2:
                targets.append(("-2 OCT", f0 * 0.25, src["oct2_level"] / 100.0, COLORS["oct2"]))

            for tag, sub_f, lvl_norm, hex_col in targets:
                if sub_f < SPEC_FREQ_MIN:
                    continue
                tx = self._freq_to_x(sub_f, plot)
                ty = zero_y - (lvl_norm * (0.18 + 0.25 * self._sub_energy_norm) * plot.height())
                t_pt = QPointF(tx, ty)

                # 拱形贝塞尔映射路径 (Source -> Sub Octave)
                arc_path = QPainterPath()
                arc_path.moveTo(src_pt)
                ctrl_y = min(src_pt.y(), t_pt.y()) - 28.0
                arc_path.quadTo(QPointF((src_pt.x() + tx) * 0.5, max(plot.top() + 10.0, ctrl_y)), t_pt)

                c = QColor(hex_col)
                p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 175), 1.35, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(arc_path)

                # 目标低八度垂直指示线与能量光环
                p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 110), 1.0, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(tx, ty), QPointF(tx, plot.bottom()))

                pulse_r = 4.5 + 5.0 * lvl_norm * self._sub_energy_norm
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(c.red(), c.green(), c.blue(), 65))
                p.drawEllipse(t_pt, pulse_r + 4.0, pulse_r + 4.0)

                p.setPen(QPen(c, 1.5))
                p.setBrush(QColor("#151518"))
                p.drawEllipse(t_pt, 4.2, 4.2)

                # 胶囊标签 (-1 OCT · 32Hz)
                lbl_str = f"{tag} · {format_freq(sub_f)}Hz"
                lbl_rect = QRectF(tx - 40, ty - 20, 80, 14)
                p.setPen(QPen(QColor("#141416"), 1.0))
                p.setBrush(QColor(21, 21, 24, 215))
                p.drawRoundedRect(lbl_rect, 3.0, 3.0)
                p.setPen(c)
                p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
                p.drawText(lbl_rect, Qt.AlignmentFlag.AlignCenter, lbl_str)

        # 7. 三个可拖拽控制节点 (SRC / LPF / CUT)
        nodes = [
            ("cut", "CUT", COLORS["filter"], self._cut_node_pos(plot), 8.2),
            ("lpf", "LPF", COLORS["filter"], self._lpf_node_pos(plot), 8.2),
            ("src", "SRC", COLORS["src"],    src_pt,                    10.5),
        ]
        for tag, label, hex_col, pt, base_r in nodes:
            is_hi = (self._hover_target == tag or self._dragging_target == tag)
            r = base_r + (1.5 if is_hi else 0.0)
            col = QColor(hex_col) if enabled else QColor("#64646E")

            if is_hi and enabled:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(col.red(), col.green(), col.blue(), 60))
                p.drawEllipse(pt, r + 5.0, r + 5.0)

            p.setPen(QPen(col, 1.8))
            p.setBrush(QColor("#151518"))
            p.drawEllipse(pt, r, r)

            p.setPen(QColor("#ECECF0") if enabled else QColor("#8A8A92"))
            p.setFont(QFont(MONO_FONT_FAM, 6 if tag != "src" else 7, QFont.Weight.Bold))
            p.drawText(QRectF(pt.x() - r, pt.y() - r, r * 2, r * 2), Qt.AlignmentFlag.AlignCenter, label)

        # 8. 右上角悬浮频谱开关胶囊 (WET / DRY / PEAK / VECT)
        self._btn_wet_rect = QRectF(plot.right() - 162, plot.top() + 8, 36, 18)
        self._btn_dry_rect = QRectF(plot.right() - 122, plot.top() + 8, 36, 18)
        self._btn_peak_rect = QRectF(plot.right() - 82, plot.top() + 8, 38, 18)
        self._btn_vect_rect = QRectF(plot.right() - 40, plot.top() + 8, 34, 18)

        pills = [
            (self._btn_wet_rect,  "WET",  self._show_wet_spec, "#28D8F0", "#24788C"),
            (self._btn_dry_rect,  "DRY",  self._show_dry_spec, "#F0A848", "#8C6224"),
            (self._btn_peak_rect, "PEAK", self._show_peak,     "#28D8F0", "#24788C"),
            (self._btn_vect_rect, "OCT",  self._show_vectors,  "#B67CF8", "#6C4896"),
        ]
        for rect, label, active, fg_hex, bd_hex in pills:
            p.setPen(QPen(QColor(bd_hex) if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 34, 42, 215) if active else QColor(21, 21, 24, 185))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor(fg_hex) if active else QColor("#787882"))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 9. 左上角十字准星 & 实时声学参数 HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(mx, plot.top()), QPointF(mx, plot.bottom()))
            p.drawLine(QPointF(plot.left(), my), QPointF(plot.right(), my))

            if self._dragging_target == "src":
                hud_str = (
                    f"SRC {format_freq(f0)}Hz ({freq_to_note(f0)})  →  "
                    f"-1 Oct: {format_freq(f0 * 0.5)}Hz  •  Drive: +{drv_db:.1f} dB"
                )
                hud_col = QColor(COLORS["src"])
                hud_w = 335
            elif self._dragging_target == "lpf":
                hud_str = f"Sub LPF Cutoff: {format_freq(f_lpf)} Hz ({freq_to_note(f_lpf)})"
                hud_col = QColor(COLORS["filter"])
                hud_w = 215
            elif self._dragging_target == "cut":
                hud_str = f"Subsonic Guard Cut: {format_freq(f_cut)} Hz"
                hud_col = QColor(COLORS["filter"])
                hud_w = 205
            else:
                cur_f = self._x_to_freq(mx, plot)
                cur_db = -((my - plot.top()) / plot.height()) * 80.0
                hud_str = f"{format_freq(cur_f)}Hz ({freq_to_note(cur_f)})   {cur_db:.1f} dBFS"
                hud_col = QColor("#B4B4BE")
                hud_w = 185

            hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor(21, 21, 24, 215))
            p.drawRoundedRect(hud_rect, 3.5, 3.5)
            p.setPen(hud_col)
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 7. 控件：统一风格旋钮参数单元 (KnobParameterCell)
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
# 8. 模块卡片 1：Source & Octave Card (谐波源频点与八度选择)
# =====================================================================
class SourceOctaveCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["src"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header: 电源开关 + 八度三态分段按钮 (-1 Oct | -2 Oct | Dual)
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 7, 10, 6)
        h_lay.setSpacing(8)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.oct_btns: dict[str, QPushButton] = {}
        for mode_name in ("-1 Oct", "-2 Oct", "Dual"):
            btn = QPushButton(mode_name)
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, m=mode_name: self._on_octave_mode(m))
            seg_lay.addWidget(btn, 1)
            self.oct_btns[mode_name] = btn

        h_lay.addWidget(seg_wrap, 1)
        root.addWidget(header)

        # Body (2x3 旋钮矩阵)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_freq = KnobParameterCell(
            "Source Freq", 30.0, 180.0, 64.0, self._accent,
            "30", "Hz", "180",
            lambda v: f"{format_freq(v)}Hz", parse_freq, mode="log"
        )
        self.c_bw = KnobParameterCell(
            "BW (Q)", 0.5, 6.0, 1.6, self._accent,
            "0.5", "Q", "6.0",
            lambda v: f"{v:.1f}", lambda s, fb: float(s), mode="log"
        )
        self.c_lpf = KnobParameterCell(
            "Sub LPF", 40.0, 250.0, 115.0, COLORS["filter"],
            "40", "Hz", "250",
            lambda v: f"{int(round(v))}Hz", parse_freq, mode="log"
        )
        for c in (self.c_freq, self.c_bw, self.c_lpf):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_oct1 = KnobParameterCell(
            "-1 Oct (f/2)", 0.0, 100.0, 85.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        self.c_oct2 = KnobParameterCell(
            "-2 Oct (f/4)", 0.0, 100.0, 42.0, COLORS["oct2"],
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        self.c_harm = KnobParameterCell(
            "Harmonics", 0.0, 100.0, 30.0, COLORS["oct2"],
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        for c in (self.c_oct1, self.c_oct2, self.c_harm):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_freq.valueChanged.connect(lambda v: self._set_field("freq", round(v, 1)))
        self.c_bw.valueChanged.connect(lambda v: self._set_field("bandwidth", round(v, 1)))
        self.c_lpf.valueChanged.connect(lambda v: self._set_field("sub_lpf", round(v, 1)))
        self.c_oct1.valueChanged.connect(lambda v: self._set_field("oct1_level", round(v, 0)))
        self.c_oct2.valueChanged.connect(lambda v: self._set_field("oct2_level", round(v, 0)))
        self.c_harm.valueChanged.connect(lambda v: self._set_field("harmonics", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        mode = self._data["octave_mode"]

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

        active_seg = f"""
            QPushButton {{
                background-color: #2E2618;
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
        for m_name, btn in self.oct_btns.items():
            is_cur = (m_name == mode)
            btn.setChecked(is_cur)
            btn.setStyleSheet(active_seg if is_cur else inactive_seg)

        self.c_freq.setValue(self._data["freq"])
        self.c_bw.setValue(self._data["bandwidth"])
        self.c_lpf.setValue(self._data["sub_lpf"])
        self.c_oct1.setValue(self._data["oct1_level"])
        self.c_oct2.setValue(self._data["oct2_level"])
        self.c_harm.setValue(self._data["harmonics"])

        self.c_freq.setModuleActive(en)
        self.c_bw.setModuleActive(en)
        self.c_lpf.setModuleActive(en)
        self.c_harm.setModuleActive(en)
        self.c_oct1.setModuleActive(en and mode in ("-1 Oct", "Dual"))
        self.c_oct2.setModuleActive(en and mode in ("-2 Oct", "Dual"))

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_octave_mode(self, mode: str):
        if self._data["octave_mode"] != mode:
            self._data["octave_mode"] = mode
            self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 9. 模块卡片 2：Drive & Character Card (谐波驱动与动态塑形)
# =====================================================================
class DriveCharacterCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
        self._accent = COLORS["drive"]
        self._updating = False

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header: 电源开关 + 谐波引擎模式切换 (RBass | LoAir | Subsynth)
        header = QWidget()
        header.setFixedHeight(38)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 7, 10, 6)
        h_lay.setSpacing(8)

        self.power_sw = PowerToggleSwitch(self._data["enabled"], self._accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.addWidget(self.power_sw)

        seg_wrap = QFrame()
        seg_wrap.setStyleSheet("background-color: #1D1D21; border: 1px solid #141416; border-radius: 3px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.eng_btns: dict[str, QPushButton] = {}
        for eng_name in ("RBass", "LoAir", "Subsynth"):
            btn = QPushButton(eng_name)
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, e=eng_name: self._on_engine_mode(e))
            seg_lay.addWidget(btn, 1)
            self.eng_btns[eng_name] = btn

        h_lay.addWidget(seg_wrap, 1)
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
            "Drive", 0.0, 24.0, 7.5, self._accent,
            "0.0", "dB", "24.0",
            lambda v: f"+{v:.1f}" if v > 0.05 else "0.0", lambda s, fb: float(s), mode="linear"
        )
        self.c_sat = KnobParameterCell(
            "Saturation", 0.0, 100.0, 35.0, self._accent,
            "Soft", "%", "Hard",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        self.c_punch = KnobParameterCell(
            "Punch", 0.0, 100.0, 45.0, self._accent,
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        for c in (self.c_drive, self.c_sat, self.c_punch):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_attack = KnobParameterCell(
            "Attack", 0.5, 80.0, 4.5, self._accent,
            "0.5", "ms", "80",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_release = KnobParameterCell(
            "Release", 20.0, 1000.0, 115.0, self._accent,
            "20", "ms", "1000",
            format_ms, lambda s, fb: float(s), mode="log"
        )
        self.c_phase = KnobParameterCell(
            "Phase", 0.0, 180.0, 0.0, COLORS["cyan"],
            "0°", "deg", "180°",
            lambda v: f"{int(round(v))}°", lambda s, fb: float(s), mode="linear"
        )
        for c in (self.c_attack, self.c_release, self.c_phase):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_drive.valueChanged.connect(lambda v: self._set_field("drive_db", round(v, 1)))
        self.c_sat.valueChanged.connect(lambda v: self._set_field("saturation", round(v, 0)))
        self.c_punch.valueChanged.connect(lambda v: self._set_field("punch", round(v, 0)))
        self.c_attack.valueChanged.connect(lambda v: self._set_field("attack", round(v, 2)))
        self.c_release.valueChanged.connect(lambda v: self._set_field("release", round(v, 1)))
        self.c_phase.valueChanged.connect(lambda v: self._set_field("phase", round(v, 0)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        eng = self._data["engine_mode"]

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

        active_seg = f"""
            QPushButton {{
                background-color: #341E22;
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
        for e_name, btn in self.eng_btns.items():
            is_cur = (e_name == eng)
            btn.setChecked(is_cur)
            btn.setStyleSheet(active_seg if is_cur else inactive_seg)

        self.c_drive.setValue(self._data["drive_db"])
        self.c_sat.setValue(self._data["saturation"])
        self.c_punch.setValue(self._data["punch"])
        self.c_attack.setValue(self._data["attack"])
        self.c_release.setValue(self._data["release"])
        self.c_phase.setValue(self._data["phase"])

        for cell in (self.c_drive, self.c_sat, self.c_punch, self.c_attack, self.c_release, self.c_phase):
            cell.setModuleActive(en)

        self._updating = False

    def _emit(self):
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))

    def _on_power(self, checked: bool):
        self._data["enabled"] = checked
        self._emit()

    def _on_engine_mode(self, mode: str):
        if self._data["engine_mode"] != mode:
            self._data["engine_mode"] = mode
            self._emit()

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 10. 模块卡片 3：Output & Mix Card (干湿混合、次声波保护与输出微调)
# =====================================================================
class OutputMixCard(QFrame):
    changed = pyqtSignal(dict)

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = copy.deepcopy(data)
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

        self.title_lbl = QLabel("OUTPUT & MIX")
        self.title_lbl.setStyleSheet(
            f"color: {self._accent}; font-size: 11px; font-weight: 700; letter-spacing: 0.4px;"
        )
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        badge = QLabel("SUBSONIC GUARD")
        badge.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.6px;")
        h_lay.addWidget(badge)
        root.addWidget(header)

        # Body (2x3 旋钮矩阵)
        body = QFrame()
        body.setObjectName("CardBody")
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(8, 8, 8, 10)
        b_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.c_direct = KnobParameterCell(
            "Direct", 0.0, 100.0, 100.0, "#A0A4B0",
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        self.c_sub = KnobParameterCell(
            "Sub Level", 0.0, 100.0, 55.0, COLORS["drive"],
            "0", "%", "100",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        self.c_mix = KnobParameterCell(
            "Mix", 0.0, 100.0, 65.0, self._accent,
            "Dry", "%", "Wet",
            lambda v: f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        for c in (self.c_direct, self.c_sub, self.c_mix):
            row1.addWidget(c, 1)
        b_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.c_lowcut = KnobParameterCell(
            "Low Cut", 15.0, 45.0, 22.0, COLORS["filter"],
            "15", "Hz", "45",
            lambda v: f"{int(round(v))}Hz", parse_freq, mode="linear"
        )
        self.c_width = KnobParameterCell(
            "Sub Width", 0.0, 100.0, 0.0, self._accent,
            "Mono", "%", "Wide",
            lambda v: "Mono" if v < 1.0 else f"{int(round(v))}%", lambda s, fb: float(s), mode="linear"
        )
        self.c_trim = KnobParameterCell(
            "Output", -12.0, 12.0, 0.0, self._accent,
            "-12", "dB", "+12",
            format_db_signed, lambda s, fb: float(s), mode="bipolar"
        )
        for c in (self.c_lowcut, self.c_width, self.c_trim):
            row2.addWidget(c, 1)
        b_lay.addLayout(row2)
        root.addWidget(body, 1)

        self.c_direct.valueChanged.connect(lambda v: self._set_field("direct_level", round(v, 0)))
        self.c_sub.valueChanged.connect(lambda v: self._set_field("sub_level", round(v, 0)))
        self.c_mix.valueChanged.connect(lambda v: self._set_field("mix", round(v, 0)))
        self.c_lowcut.valueChanged.connect(lambda v: self._set_field("low_cut", round(v, 1)))
        self.c_width.valueChanged.connect(lambda v: self._set_field("width", round(v, 0)))
        self.c_trim.valueChanged.connect(lambda v: self._set_field("trim_db", round(v, 1)))

        self.syncFromData(self._data)

    def syncFromData(self, data: dict):
        self._updating = True
        self._data = copy.deepcopy(data)

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

        self.c_direct.setValue(self._data["direct_level"])
        self.c_sub.setValue(self._data["sub_level"])
        self.c_mix.setValue(self._data["mix"])
        self.c_lowcut.setValue(self._data["low_cut"])
        self.c_width.setValue(self._data["width"])
        self.c_trim.setValue(self._data["trim_db"])

        self._updating = False

    def setDirectMutedBySolo(self, is_solo: bool):
        self.c_direct.setModuleActive(not is_solo)

    def _set_field(self, key: str, val: float):
        self._data[key] = val
        if not self._updating:
            self.changed.emit(copy.deepcopy(self._data))


# =====================================================================
# 11. 主面板：自主旗舰低频谐波增强器 (Studio Subharmonic Enhancer)
# =====================================================================
class StudioSubharmonicWidget(QWidget):
    """
    低频谐波增强器 UI 主面板，提供完整信号接口供后端 DSP 引擎订阅：
      - bypassChanged(bool)
      - subSoloChanged(bool)
      - sourceOctaveChanged(dict)
      - driveChanged(dict)
      - outputMixChanged(dict)
      - stateChanged(dict)
    """
    bypassChanged = pyqtSignal(bool)
    subSoloChanged = pyqtSignal(bool)
    sourceOctaveChanged = pyqtSignal(dict)
    driveChanged = pyqtSignal(dict)
    outputMixChanged = pyqtSignal(dict)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Kick / Sub Bus", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Subharmonic Enhancer — {track_name}")
        self.resize(1040, 710)
        self.setMinimumSize(940, 630)

        self._active_bank = "A"
        self._banks = {
            "A": default_subharmonic_demo(),
            "B": default_subharmonic_flat(),
        }

        self._presets = {
            "Kick Gut-Punch (RBass)": copy.deepcopy(self._banks["A"]),
            "Flat / Bypass": copy.deepcopy(self._banks["B"]),
            "Cinematic LFE Rumble (LoAir)": {
                "enabled": True,
                "sub_solo": False,
                "source": {
                    "enabled": True,
                    "octave_mode": "Dual",
                    "freq": 56.0,
                    "bandwidth": 1.2,
                    "sub_lpf": 85.0,
                    "oct1_level": 75.0,
                    "oct2_level": 90.0,
                    "harmonics": 10.0,
                },
                "drive": {
                    "enabled": True,
                    "engine_mode": "LoAir",
                    "drive_db": 10.5,
                    "saturation": 25.0,
                    "punch": 20.0,
                    "attack": 12.0,
                    "release": 280.0,
                    "phase": 0.0,
                },
                "output": {
                    "direct_level": 100.0,
                    "sub_level": 72.0,
                    "mix": 75.0,
                    "low_cut": 18.0,
                    "width": 15.0,
                    "trim_db": -1.0,
                },
            },
            "808 & Sub Bass Weight": {
                "enabled": True,
                "sub_solo": False,
                "source": {
                    "enabled": True,
                    "octave_mode": "-1 Oct",
                    "freq": 72.0,
                    "bandwidth": 2.0,
                    "sub_lpf": 140.0,
                    "oct1_level": 92.0,
                    "oct2_level": 0.0,
                    "harmonics": 55.0,
                },
                "drive": {
                    "enabled": True,
                    "engine_mode": "Subsynth",
                    "drive_db": 12.0,
                    "saturation": 60.0,
                    "punch": 65.0,
                    "attack": 2.5,
                    "release": 95.0,
                    "phase": 15.0,
                },
                "output": {
                    "direct_level": 95.0,
                    "sub_level": 60.0,
                    "mix": 70.0,
                    "low_cut": 25.0,
                    "width": 0.0,
                    "trim_db": -0.5,
                },
            },
            "Thin Bass Restoration": {
                "enabled": True,
                "sub_solo": False,
                "source": {
                    "enabled": True,
                    "octave_mode": "-1 Oct",
                    "freq": 95.0,
                    "bandwidth": 1.4,
                    "sub_lpf": 155.0,
                    "oct1_level": 80.0,
                    "oct2_level": 0.0,
                    "harmonics": 45.0,
                },
                "drive": {
                    "enabled": True,
                    "engine_mode": "RBass",
                    "drive_db": 8.5,
                    "saturation": 40.0,
                    "punch": 35.0,
                    "attack": 6.0,
                    "release": 130.0,
                    "phase": 0.0,
                },
                "output": {
                    "direct_level": 100.0,
                    "sub_level": 50.0,
                    "mix": 60.0,
                    "low_cut": 28.0,
                    "width": 0.0,
                    "trim_db": 0.0,
                },
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 演示低频脉冲模拟定时器（接入真实后端 update_meters / update_spectrum 后自动停止）
        self._anim_t = 0.0
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
            ProVerticalMeter, ProSubharmonicGraphWidget {{
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
        # 1. 顶部整合工具栏
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

        title_lbl = QLabel(f"SUBHARMONIC ENHANCER  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(12)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存为新预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedWidth(205)
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

        tb_lay.addSpacing(10)

        # Sub Solo 独奏监听开关
        self.sub_solo_btn = QPushButton("Sub Solo")
        self.sub_solo_btn.setCheckable(True)
        self.sub_solo_btn.setFixedSize(64, 22)
        self.sub_solo_btn.setToolTip("独奏监听新生成的低八度谐波信号 (Audition Subharmonics Only)")
        self.sub_solo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sub_solo_btn.clicked.connect(self._on_sub_solo_toggled)
        tb_lay.addWidget(self.sub_solo_btn)

        tb_lay.addStretch()

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_subharmonic)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心监控区：Input 表 | 低频谐波谱与八度向量画布 | Sub Synth 表 | Output 表
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

        # 2.1 左侧 Input 电平表
        in_col = self._build_meter_column("Input")
        self.meter_input = ProVerticalMeter(mode="level", show_ticks=True)
        in_col.addWidget(self.meter_input, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(in_col)
        up_lay.addWidget(_vsep())

        # 2.2 中央低频频谱与谐波向量图
        self.sub_graph = ProSubharmonicGraphWidget()
        self.sub_graph.sourceFreqDragged.connect(self._on_graph_src_freq)
        self.sub_graph.driveDragged.connect(self._on_graph_drive)
        self.sub_graph.bandwidthScrolled.connect(self._on_graph_bw)
        self.sub_graph.subLpfDragged.connect(self._on_graph_lpf)
        self.sub_graph.lowCutDragged.connect(self._on_graph_lowcut)
        up_lay.addWidget(self.sub_graph, 1)
        up_lay.addWidget(_vsep())

        # 2.3 右侧 Sub Synth 合成能量表
        sub_col = self._build_meter_column("Sub Gen")
        self.meter_sub_gen = ProVerticalMeter(title="-1/-2", mode="sub", show_ticks=True)
        sub_col.addWidget(self.meter_sub_gen, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(sub_col)
        up_lay.addWidget(_vsep())

        # 2.4 最右侧 Output 电平表
        out_col = self._build_meter_column("Output")
        self.meter_output = ProVerticalMeter(mode="level", show_ticks=True)
        out_col.addWidget(self.meter_output, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)

        main_lay.addWidget(upper_panel, 5)

        # -------------------------------------------------------------
        # 3. 底部三模块处理卡片矩阵 (Source & Octave | Drive & Character | Output & Mix)
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        st = self._current_state()
        self.src_card = SourceOctaveCard(st["source"])
        self.src_card.changed.connect(self._on_src_changed)

        self.drv_card = DriveCharacterCard(st["drive"])
        self.drv_card.changed.connect(self._on_drv_changed)

        self.out_card = OutputMixCard(st["output"])
        self.out_card.changed.connect(self._on_out_changed)

        cards_row.addWidget(self.src_card, 1)
        cards_row.addWidget(self.drv_card, 1)
        cards_row.addWidget(self.out_card, 1)

        main_lay.addLayout(cards_row, 4)

    def _build_meter_column(self, header_text: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(3, 5, 3, 4)
        col.setSpacing(3)
        lbl = QLabel(header_text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #787882; font-size: 9.5px; font-weight: 700; letter-spacing: 0.3px;")
        col.addWidget(lbl)
        return col

    # -----------------------------------------------------------------
    # 状态同步与双向交互逻辑
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_top_bar_styles(self):
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

        solo = bool(st["sub_solo"])
        self.sub_solo_btn.setChecked(solo)
        self.sub_solo_btn.setStyleSheet("""
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
                background-color: #3A2E18;
                color: #F0A848;
                border: 1px solid #F0A848;
            }
        """)

    def _refresh_all_views(self):
        st = self._current_state()
        self._update_top_bar_styles()
        self.power_switch.setChecked(st["enabled"])
        self.src_card.syncFromData(st["source"])
        self.drv_card.syncFromData(st["drive"])
        self.out_card.syncFromData(st["output"])
        self.out_card.setDirectMutedBySolo(bool(st["sub_solo"]))
        self.meter_sub_gen.setActive(bool(st["enabled"]) and bool(st["source"]["enabled"]))
        self.sub_graph.setState(st)

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
        self.meter_sub_gen.setActive(enabled and bool(st["source"]["enabled"]))
        self.sub_graph.setState(st)
        self.bypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_sub_solo_toggled(self, checked: bool):
        st = self._current_state()
        st["sub_solo"] = checked
        self._update_top_bar_styles()
        self.out_card.setDirectMutedBySolo(checked)
        self.subSoloChanged.emit(checked)
        self.stateChanged.emit(self.get_state())

    def _on_src_changed(self, src_data: dict):
        st = self._current_state()
        st["source"] = src_data
        self.meter_sub_gen.setActive(bool(st["enabled"]) and bool(src_data["enabled"]))
        self.sub_graph.setState(st)
        self.sourceOctaveChanged.emit(copy.deepcopy(src_data))
        self.stateChanged.emit(self.get_state())

    def _on_drv_changed(self, drv_data: dict):
        st = self._current_state()
        st["drive"] = drv_data
        self.sub_graph.setState(st)
        self.driveChanged.emit(copy.deepcopy(drv_data))
        self.stateChanged.emit(self.get_state())

    def _on_out_changed(self, out_data: dict):
        st = self._current_state()
        st["output"] = out_data
        self.sub_graph.setState(st)
        self.outputMixChanged.emit(copy.deepcopy(out_data))
        self.stateChanged.emit(self.get_state())

    # 画布拖拽反向同步到底部卡片
    def _on_graph_src_freq(self, freq: float):
        st = self._current_state()
        st["source"]["freq"] = freq
        self.src_card.syncFromData(st["source"])
        self.sub_graph.setState(st)
        self.sourceOctaveChanged.emit(copy.deepcopy(st["source"]))
        self.stateChanged.emit(self.get_state())

    def _on_graph_drive(self, drive_db: float):
        st = self._current_state()
        st["drive"]["drive_db"] = drive_db
        self.drv_card.syncFromData(st["drive"])
        self.sub_graph.setState(st)
        self.driveChanged.emit(copy.deepcopy(st["drive"]))
        self.stateChanged.emit(self.get_state())

    def _on_graph_bw(self, bw: float):
        st = self._current_state()
        st["source"]["bandwidth"] = bw
        self.src_card.syncFromData(st["source"])
        self.sub_graph.setState(st)
        self.sourceOctaveChanged.emit(copy.deepcopy(st["source"]))
        self.stateChanged.emit(self.get_state())

    def _on_graph_lpf(self, lpf: float):
        st = self._current_state()
        st["source"]["sub_lpf"] = lpf
        self.src_card.syncFromData(st["source"])
        self.sub_graph.setState(st)
        self.sourceOctaveChanged.emit(copy.deepcopy(st["source"]))
        self.stateChanged.emit(self.get_state())

    def _on_graph_lowcut(self, cut: float):
        st = self._current_state()
        st["output"]["low_cut"] = cut
        self.out_card.syncFromData(st["output"])
        self.sub_graph.setState(st)
        self.outputMixChanged.emit(copy.deepcopy(st["output"]))
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
        name, ok = QInputDialog.getText(self, "保存 Subharmonic 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 演示低频动态模拟（真实呈现底鼓触发低八度谐波峰值的效果）
    # -----------------------------------------------------------------
    def _tick_demo_simulation(self):
        self._anim_t += 0.14
        st = self._current_state()
        src = st["source"]
        drv = st["drive"]
        out = st["output"]

        # 模拟底鼓每拍重击包络 (0.0 ~ 1.0)
        beat_phase = (self._anim_t % (2.0 * math.pi)) / (2.0 * math.pi)
        kick_env = math.exp(-beat_phase * 5.2)
        bass_groove = 0.45 + 0.35 * math.sin(self._anim_t * 0.5)
        excitation = min(1.0, kick_env * 0.85 + bass_groove * 0.35)

        f0 = float(src["freq"])
        f_oct1 = f0 * 0.5
        f_oct2 = f0 * 0.25
        f_lpf = float(src["sub_lpf"])
        f_cut = float(out["low_cut"])

        active_synth = st["enabled"] and src["enabled"]
        oct_mode = src["octave_mode"]
        w1 = (src["oct1_level"] / 100.0) if (active_synth and oct_mode in ("-1 Oct", "Dual")) else 0.0
        w2 = (src["oct2_level"] / 100.0) if (active_synth and oct_mode in ("-2 Oct", "Dual")) else 0.0
        wh = (src["harmonics"] / 100.0) if active_synth else 0.0

        drv_boost = (1.0 + drv["drive_db"] / 14.0) if (active_synth and drv["enabled"]) else 0.65
        mix_wet = (out["mix"] / 100.0) * (out["sub_level"] / 100.0) if st["enabled"] else 0.0
        dry_gain = 0.0 if st["sub_solo"] else (out["direct_level"] / 100.0)
        trim = out["trim_db"]

        dry_spec, wet_spec, peak_spec = [], [], []
        in_peak = -55.0
        wet_peak = -55.0
        sub_peak = -55.0

        for i, f in enumerate(self.sub_graph._spec_freqs):
            # 模拟原始底鼓/贝斯频谱 (基频在 ~65Hz 附近，缺少超低频下潜)
            fund = 34.0 * excitation * math.exp(-((math.log2(f / 66.0)) ** 2) / 0.18)
            mid_click = 14.0 * kick_env * math.exp(-((math.log2(f / 320.0)) ** 2) / 0.45)
            noise = 1.6 * math.sin(i * 0.35 + self._anim_t * 2.1)
            raw_db = -58.0 + fund + mid_click + noise
            in_peak = max(in_peak, raw_db)
            dry_spec.append(max(-80.0, min(-4.0, raw_db)))

            # 计算合成的低八度谐波能量分布
            sub1_bell = 32.0 * w1 * drv_boost * excitation * math.exp(-((math.log2(f / max(15.0, f_oct1))) ** 2) / 0.14)
            sub2_bell = 30.0 * w2 * drv_boost * excitation * math.exp(-((math.log2(f / max(15.0, f_oct2))) ** 2) / 0.14)
            harm_bell = 18.0 * wh * drv_boost * excitation * math.exp(-((math.log2(f / (f0 * 2.0))) ** 2) / 0.22)

            # 受 Sub LPF 与 Low Cut Guard 滤波器边界约束
            hp_atten = 1.0 / (1.0 + (f_cut / max(1.0, f)) ** 6)
            lp_atten = 1.0 / (1.0 + (f / max(1.0, f_lpf)) ** 4)
            synth_db = (-62.0 + sub1_bell + sub2_bell + harm_bell) + 12.0 * math.log10(max(1e-4, hp_atten * lp_atten))
            sub_peak = max(sub_peak, synth_db + 20.0 * math.log10(max(0.05, mix_wet)))

            dry_lin = (10.0 ** (raw_db / 20.0)) * dry_gain
            wet_lin = (10.0 ** (synth_db / 20.0)) * mix_wet * 1.8
            out_db = 20.0 * math.log10(max(1e-5, dry_lin + wet_lin)) + trim
            out_clamped = max(-80.0, min(-2.0, out_db))

            wet_spec.append(out_clamped)
            peak_spec.append(min(-1.0, out_clamped + 2.2))
            wet_peak = max(wet_peak, out_clamped)

        sub_norm = max(0.0, min(1.0, (sub_peak + 45.0) / 38.0)) if active_synth else 0.0
        self.sub_graph.update_spectrum(self.sub_graph._spec_freqs, dry_spec, wet_spec, peak_spec, sub_norm)
        self.meter_input.setValue(in_peak + 6.0)
        self.meter_sub_gen.setValue(sub_peak if active_synth else -55.0)
        self.meter_output.setValue(wet_peak + 6.0)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程文件序列化调用
    # -----------------------------------------------------------------
    def reset_subharmonic(self):
        self._banks[self._active_bank] = default_subharmonic_flat()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_meters(self, input_db: float, sub_gen_db: float, output_db: float):
        """接入真实音频引擎实时电平 (dBFS)"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.meter_input.setValue(input_db)
        self.meter_sub_gen.setValue(sub_gen_db)
        self.meter_output.setValue(output_db)

    def update_spectrum(
        self,
        freqs_hz: list[float],
        dry_db: list[float],
        wet_db: list[float],
        peak_db: list[float] | None = None,
        sub_energy_norm: float | None = None,
    ):
        """接入真实音频引擎低频 FFT 频谱数据 (15 Hz ~ 1200 Hz)"""
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.sub_graph.update_spectrum(freqs_hz, dry_db, wet_db, peak_db, sub_energy_norm)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioSubharmonicWidget("Kick / Sub Bus")
    win.show()
    sys.exit(app.exec())