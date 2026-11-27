"""Pro Widgets — 新一代音频效果器共享自绘控件库（CW-A1）。

从 5 个重绘的效果器原型（EQ / Dynamics / Reverb / Delay / Panner）中合并的
统一控件超集，供各效果器对话框复用：

  - PowerToggleSwitch  现代发光电源开关（模块主题色）
  - EditableValuePill  内嵌可编辑数字胶囊
  - ProRotaryKnob      拉丝阳极铝质感旋钮（linear/log/power/bipolar/circular）
  - KnobParameterCell  标签 + 旋钮 + 刻度范围 + 胶囊 的统一参数单元
  - ProVerticalMeter   垂直高精电平表（level / GR 衰减双模式）
  - ProStereoOutputMeter 双声道 L/R 输出电平表
  - ProModuleCard      底部参数模块卡片基类
  - MakeUpFaderStrip   Make Up 补偿增益推子

交互约定（对接项目三段节流）：
  - KnobParameterCell 在连续拖动 / 滚轮 / 胶囊编辑期间持续发射 valueChanged
    （→ 对话框 preview 路径），松手/提交时发射 interactionEnded（→ committed）。

数值格式化/解析助手：format_freq_hz / parse_freq_hz / parse_delay_ms /
parse_float_clean / format_ms / format_ratio_comp / format_ratio_eg /
parse_ratio_value。

线程安全：纯 UI 控件，仅 UI 线程访问。全部自绘（QPainter），无图片资源。
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient,
)
from PyQt6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget


# ═════════════════════════════════════════════════════════════════════════════
# 跨平台字体 / 置顶浮窗 / 字形无关图标
# ═════════════════════════════════════════════════════════════════════════════

# macOS 无 'Segoe UI'（Windows 字体），缺字回退会让 ‹ › → + 等字形变成
# 空心方块——统一用 macOS 优先的字体栈，Qt 取第一个已安装族。
PRO_FONT_FAMILIES = [
    "SF Pro Display", "Helvetica Neue", "Segoe UI", "PingFang SC",
    "Microsoft YaHei",
]


def pro_font(size: int, weight: int = -1) -> QFont:
    """对话框统一 UI 字体（按平台可用性依次回退）。"""
    f = QFont()
    f.setFamilies(PRO_FONT_FAMILIES)
    f.setPointSize(size)
    if weight >= 0:
        f.setWeight(QFont.Weight(weight))
    return f


def apply_float_panel(dialog: QDialog) -> None:
    """效果器对话框置顶：始终保持在主窗口（含全屏视频窗口）最前面。

    Da Vinci Fairlight 风格——效果器是浮动面板，播放/编辑期间不会被
    主窗口或全屏监视器压底。
    """
    dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)


def present_effect_window(dialog: QDialog) -> None:
    if dialog.isMinimized():
        dialog.showNormal()
    else:
        dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def glyph_icon(kind: str, color: str = "#B9BFCE", size: int = 14) -> QIcon:
    """字体无关的矢量小图标（chevron / 箭头 / 加号等）。

    用 QPainter 直接画，避免 ‹ › → ← 等字形在缺字字体下渲染成方块。
    """
    pm = QPixmap(QSize(size, size))
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.4, size / 10.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    c = size / 2.0
    path = QPainterPath()
    if kind == "plus":
        path.moveTo(c, size * 0.2)
        path.lineTo(c, size * 0.8)
        path.moveTo(size * 0.2, c)
        path.lineTo(size * 0.8, c)
        p.drawPath(path)
    elif kind == "chevron-left":
        path.moveTo(size * 0.62, size * 0.22)
        path.lineTo(size * 0.36, c)
        path.lineTo(size * 0.62, size * 0.78)
        p.drawPath(path)
    elif kind == "chevron-right":
        path.moveTo(size * 0.38, size * 0.22)
        path.lineTo(size * 0.64, c)
        path.lineTo(size * 0.38, size * 0.78)
        p.drawPath(path)
    elif kind == "arrow-right":
        path.moveTo(size * 0.18, c)
        path.lineTo(size * 0.80, c)
        path.moveTo(size * 0.58, size * 0.36)
        path.lineTo(size * 0.80, c)
        path.lineTo(size * 0.58, size * 0.64)
        p.drawPath(path)
    elif kind == "arrow-left":
        path.moveTo(size * 0.82, c)
        path.lineTo(size * 0.20, c)
        path.moveTo(size * 0.42, size * 0.36)
        path.lineTo(size * 0.20, c)
        path.lineTo(size * 0.42, size * 0.64)
        p.drawPath(path)
    elif kind == "panel-right":
        p.drawRoundedRect(QRectF(size * 0.12, size * 0.19, size * 0.76, size * 0.62), 1.2, 1.2)
        p.drawLine(QPointF(size * 0.61, size * 0.20), QPointF(size * 0.61, size * 0.80))
    elif kind == "power":
        p.drawArc(QRectF(size * 0.18, size * 0.23, size * 0.64, size * 0.64), 135 * 16, 270 * 16)
        p.drawLine(QPointF(c, size * 0.10), QPointF(c, size * 0.49))
    else:
        # 未知 kind：画一个圆点占位，避免静默空白
        p.drawEllipse(QPointF(c, c), size * 0.22, size * 0.22)
    p.end()
    return QIcon(pm)


# ═════════════════════════════════════════════════════════════════════════════
# 数值格式化 / 自然语言解析
# ═════════════════════════════════════════════════════════════════════════════

def format_freq_hz(hz: float) -> str:
    """频率 → "1.23k" / "440" 形式。"""
    if hz >= 1000.0:
        return f"{hz / 1000.0:.2f}k"
    return f"{int(round(hz))}"


def parse_freq_hz(text: str, fallback: float) -> float:
    """解析 "1.2k" / "440hz" 等频率文本。"""
    s = text.strip().upper().replace("HZ", "").strip()
    try:
        if s.endswith("K"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def parse_delay_ms(text: str, fallback: float) -> float:
    """解析 "123ms" / "1.2s" 等延迟文本。"""
    s = text.strip().lower().replace("ms", "").strip()
    try:
        if s.endswith("s"):
            return float(s[:-1]) * 1000.0
        return float(s)
    except ValueError:
        return fallback


def parse_float_clean(text: str, fallback: float) -> float:
    """解析裸数字文本（剥离 %/dB/ms 等单位残余）。"""
    s = text.strip().replace("%", "").replace("dB", "").replace("db", "").strip()
    try:
        return float(s)
    except ValueError:
        return fallback


def format_ms(val: float) -> str:
    """时间值自适应精度。"""
    if val < 1.0:
        return f"{val:.2f}"
    if val < 10.0:
        return f"{val:.1f}"
    return f"{int(round(val))}"


def format_ratio_comp(val: float) -> str:
    return f"{val:.0f}:1" if val >= 10.0 else f"{val:.1f}:1"


def format_ratio_eg(val: float) -> str:
    return f"1:{val:.1f}"


def parse_ratio_value(text: str, is_eg: bool, fallback: float) -> float:
    """解析 "4:1"（压缩）/ "1:2.5"（扩展）比值文本。"""
    s = text.strip().replace(" ", "")
    try:
        if ":" in s:
            left, right = s.split(":", 1)
            return float(right) if is_eg else float(left)
        return float(s)
    except ValueError:
        return fallback


# ═════════════════════════════════════════════════════════════════════════════
# 电源开关
# ═════════════════════════════════════════════════════════════════════════════

class PowerToggleSwitch(QWidget):
    """现代发光电源开关（模块主题色，setChecked 默认不发信号防回环）。"""

    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True, accent_color: str = "#28D8F0", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._checked = checked
        self._accent = QColor(accent_color)
        self.setFixedSize(34, 17)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, val: bool, emit: bool = False) -> None:
        if self._checked != val:
            self._checked = val
            self.update()
            if emit:
                self.toggled.emit(self._checked)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked, emit=True)

    def paintEvent(self, event) -> None:  # noqa: N802
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


# ═════════════════════════════════════════════════════════════════════════════
# 可编辑数字胶囊
# ═════════════════════════════════════════════════════════════════════════════

class EditableValuePill(QWidget):
    """内嵌可编辑数字胶囊：单击原地编辑，提交时发射 valueEdited(str)。

    活跃态由 setModuleActive 控制（非活跃灰显且不可编辑）。
    """

    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = "#28D8F0",
                 width: int = 72, parent: Optional[QWidget] = None) -> None:
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
                background-color: #0B0C0E;
                color: {accent_color};
                border: 1px solid {accent_color};
                border-radius: 5px;
                font-family: 'SF Pro Display', 'Helvetica Neue', 'Segoe UI', 'Microsoft YaHei';
                font-size: 11px;
                font-weight: 600;
                padding: 0px 2px;
            }}
        """)
        self._editor.hide()
        self._editor.returnPressed.connect(self._commit_edit)
        self._editor.editingFinished.connect(self._commit_edit)

    def setText(self, text: str) -> None:
        if self._text != text:
            self._text = text
            self.update()

    def text(self) -> str:
        return self._text

    def setModuleActive(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._active and event.button() == Qt.MouseButton.LeftButton:
            clean = self._text.replace("dB", "").replace("%", "").replace("ms", "").strip()
            self._editor.setText(clean)
            self._editor.show()
            self._editor.setFocus()
            self._editor.selectAll()

    def _commit_edit(self) -> None:
        if self._editor.isVisible():
            val = self._editor.text()
            self._editor.hide()
            self.valueEdited.emit(val)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(QColor("#282A32"), 1.0))
        p.setBrush(QColor("#101114"))
        p.drawRoundedRect(rect, 5.0, 5.0)

        p.setPen(QColor("#F0F2F6") if self._active else QColor("#6E727E"))
        font = pro_font(10)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


# ═════════════════════════════════════════════════════════════════════════════
# 旋钮
# ═════════════════════════════════════════════════════════════════════════════

class ProRotaryKnob(QWidget):
    """拉丝阳极铝质感旋钮。

    支持映射模式：linear / log / power（指数幂）/ bipolar（双极零点居中）/
    circular（连续旋转，如 Rotate 0..360°）。invert 用于 F/B 旋钮的直觉方向。
    零刻度点：bipolar 正上方；zero_marker_val 支持非对称零位（如 Level 0dB）。

    交互：垂直拖动调值（Shift 细调）、滚轮步进、右键/双击复位默认值。
    交互起止经 interactionStarted/interactionEnded 透出（三段节流用）。
    """

    valueChanged = pyqtSignal(float)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value: float,
        default_value: float,
        accent_color: str = "#28D8F0",
        mode: str = "linear",
        zero_marker_val: Optional[float] = None,
        invert: bool = False,
        size: int = 54,
        power_exp: float = 2.2,
        bipolar_origin: Optional[float] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._mode = mode
        self._zero_marker_val = zero_marker_val
        self._invert = invert
        self._power_exp = power_exp
        # bipolar 模式的起弧零点（None = 区间中点；如 -18..6 dB 增益的 0dB 位）
        self._bipolar_origin = bipolar_origin
        self._active = True
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def value(self) -> float:
        return self._value

    def setValue(self, val: float, emit: bool = False) -> None:
        if self._mode == "circular":
            clamped = val % (self._max - self._min) if self._max > self._min else val
        else:
            clamped = max(self._min, min(self._max, val))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def setModuleActive(self, active: bool) -> None:
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
            return ratio ** (1.0 / self._power_exp)
        n = (val - self._min) / (self._max - self._min)
        return (1.0 - n) if self._invert else n

    def _norm_to_val(self, norm: float) -> float:
        if self._mode == "circular":
            n = norm % 1.0
        else:
            n = max(0.0, min(1.0, norm))
        if self._invert:
            n = 1.0 - n
        if self._mode == "log":
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return mn * ((mx / mn) ** n)
        if self._mode == "power":
            return self._min + (n ** self._power_exp) * (self._max - self._min)
        return self._min + n * (self._max - self._min)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self._active:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_y = event.position().y()
            self.interactionStarted.emit()
        elif event.button() == Qt.MouseButton.RightButton:
            self.setValue(self._default, emit=True)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging and self._active:
            dy = self._last_y - event.position().y()
            self._last_y = event.position().y()
            sens = 0.0015 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.0065
            norm = self._val_to_norm(self._value) + dy * sens
            self.setValue(self._norm_to_val(norm), emit=True)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self._dragging:
                self._dragging = False
                self.interactionEnded.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._active and event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._default, emit=True)
            self.interactionEnded.emit()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self._active:
            return
        delta = event.angleDelta().y() / 120.0
        step = 0.01 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.03
        norm = self._val_to_norm(self._value) + delta * step
        self.setValue(self._norm_to_val(norm), emit=True)
        self.interactionEnded.emit()
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        norm = max(0.0, min(1.0, self._val_to_norm(self._value)))

        start_deg = 225.0
        total_span = -270.0
        curr_deg = start_deg + norm * total_span

        size = float(self.width())
        arc_r = size * 0.43
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深色凹槽底环
        ring_w = max(2.5, size * 0.056)
        p.setPen(QPen(QColor("#111216"), ring_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性正上方 0 基准刻度
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#6E7485"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 非对称零刻度点（如 Level 的 0.0 dB 位置）
        if self._zero_marker_val is not None:
            z_norm = self._val_to_norm(self._zero_marker_val)
            z_rad = math.radians(start_deg + z_norm * total_span)
            p.setPen(QPen(QColor("#787E90"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(
                QPointF(cx + math.cos(z_rad) * (arc_r - 1.2), cy - math.sin(z_rad) * (arc_r - 1.2)),
                QPointF(cx + math.cos(z_rad) * (arc_r + 2.2), cy - math.sin(z_rad) * (arc_r + 2.2)),
            )

        # 2. 模块主题色光环
        arc_col = self._accent if self._active else QColor("#4A4D58")
        p.setPen(QPen(arc_col, ring_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._mode == "bipolar":
            if self._bipolar_origin is not None:
                zero_norm = self._val_to_norm(self._bipolar_origin)
            else:
                zero_norm = 0.5
            zero_deg = start_deg + zero_norm * total_span
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        # 3. 旋钮主体（拉丝阳极铝径向渐变）
        knob_r = arc_r * 0.71
        grad = QRadialGradient(cx - knob_r * 0.35, cy - knob_r * 0.35, knob_r * 1.45)
        if (self._hovered or self._dragging) and self._active:
            grad.setColorAt(0.0, QColor("#555966"))
            grad.setColorAt(0.65, QColor("#343742"))
            grad.setColorAt(1.0, QColor("#202229"))
        else:
            grad.setColorAt(0.0, QColor("#464954" if self._active else "#343740"))
            grad.setColorAt(0.65, QColor("#2B2D36" if self._active else "#23252C"))
            grad.setColorAt(1.0, QColor("#1B1D23"))

        p.setPen(QPen(QColor("#101115"), 1.4))
        p.setBrush(grad)
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        p.setPen(QPen(QColor(255, 255, 255, 26 if self._active else 12), 0.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), knob_r - 1.5, knob_r - 1.5)

        # 4. 发光指针刻度线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * knob_r * 0.26
        iy1 = cy - math.sin(rad) * knob_r * 0.26
        ix2 = cx + math.cos(rad) * (knob_r - 2.5)
        iy2 = cy - math.sin(rad) * (knob_r - 2.5)
        ptr_col = self._accent if self._active else QColor("#6E727E")
        p.setPen(QPen(ptr_col, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# ═════════════════════════════════════════════════════════════════════════════
# 统一参数单元（标签 + 旋钮 + 刻度范围 + 胶囊）
# ═════════════════════════════════════════════════════════════════════════════

class KnobParameterCell(QWidget):
    """标签 + 旋钮 + 上下限提示 + 可编辑胶囊的统一参数单元。

    valueChanged 持续发射（拖动/滚轮/胶囊提交），interactionStarted/
    interactionEnded 标记一次连续编辑的起止（三段节流：preview ↔ committed）。
    """

    valueChanged = pyqtSignal(float)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

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
        formatter: Callable[[float], str],
        parser: Callable[[str, float], float],
        mode: str = "linear",
        zero_marker_val: Optional[float] = None,
        pill_width: int = 72,
        knob_size: int = 54,
        power_exp: float = 2.2,
        invert: bool = False,
        bipolar_origin: Optional[float] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
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
            accent_color, mode=mode, zero_marker_val=zero_marker_val,
            invert=invert, size=knob_size, power_exp=power_exp,
            bipolar_origin=bipolar_origin,
        )
        self.knob.valueChanged.connect(self._on_knob_changed)
        self.knob.interactionStarted.connect(self.interactionStarted.emit)
        self.knob.interactionEnded.connect(self.interactionEnded.emit)
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
            hl.setStyleSheet("color: #666B78; font-size: 8px;")
            hints_row.addWidget(hl, 1)
        lay.addLayout(hints_row)

        self.pill = EditableValuePill(formatter(default_val), accent_color, width=pill_width)
        self.pill.valueEdited.connect(self._on_pill_edited)
        lay.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignCenter)

    def value(self) -> float:
        """当前参数值。"""
        return self._val

    def setValue(self, val: float) -> None:
        """程序化回填（blockSignals 语义：不发 valueChanged）。"""
        self._updating = True
        self._val = max(self._min, min(self._max, val))
        self.knob.setValue(self._val)
        self.pill.setText(self._formatter(self._val))
        self._updating = False

    def setModuleActive(self, active: bool) -> None:
        self.title_lbl.setStyleSheet(
            f"color: {'#9AA0B0' if active else '#565A66'}; font-size: 11px; font-weight: 600;")
        self.knob.setModuleActive(active)
        self.pill.setModuleActive(active)

    def _on_knob_changed(self, val: float) -> None:
        if self._updating:
            return
        self._val = val
        self.pill.setText(self._formatter(val))
        self.valueChanged.emit(val)

    def _on_pill_edited(self, text: str) -> None:
        parsed = self._parser(text, self._val)
        clamped = max(self._min, min(self._max, parsed))
        self.setValue(clamped)
        self.valueChanged.emit(clamped)
        self.interactionEnded.emit()


# ═════════════════════════════════════════════════════════════════════════════
# 垂直高精电平表（level / GR 双模式）
# ═════════════════════════════════════════════════════════════════════════════

class ProVerticalMeter(QWidget):
    """垂直高精电平表。

    mode="level"：自下而上电平（峰值保持 + 削波灯 + 三色梯度）。
    mode="gr"：自上而下增益衰减（0 dB 顶部 → min_db 底部，主题色填充）。
    """

    def __init__(
        self,
        title: str = "",
        mode: str = "level",
        min_db: float = -55.0,
        max_db: float = 0.0,
        ticks: Optional[list] = None,
        show_ticks: bool = True,
        accent_color: str = "#F0A848",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._mode = mode
        self._min_db = min_db
        self._max_db = max_db
        self._ticks = ticks or (
            [0, -5, -10, -15, -20, -30, -40, -50] if mode == "level"
            else [0, -3, -6, -10, -14, -20, -30, -50])
        self._show_ticks = show_ticks
        self._accent = QColor(accent_color)

        self._value_db = min_db if mode == "level" else 0.0
        self._peak_db = self._value_db
        self._clipped = False
        self._active = True

        self.setFixedWidth(44 if show_ticks else 18)
        self.setMinimumHeight(180)

    def setValue(self, db: float) -> None:
        if self._mode == "level":
            self._value_db = max(self._min_db, min(self._max_db, db))
            if self._value_db >= self._peak_db:
                self._peak_db = self._value_db
            else:
                self._peak_db = max(self._value_db, self._peak_db - 0.35)
            if db >= -0.1:
                self._clipped = True
        else:
            # GR：输入正衰减值，内部转为 0..-min_db 向下填充
            val = -abs(db)
            self._value_db = max(self._min_db, min(0.0, val))
            if self._value_db <= self._peak_db:
                self._peak_db = self._value_db
            else:
                self._peak_db = min(self._value_db, self._peak_db + 0.35)
        self.update()

    def setActive(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._clipped = False
            self._peak_db = self._value_db
            self.update()

    def _db_to_y(self, db: float, bar: QRectF) -> float:
        # 对 -20..0 dB（level）/ -14..0（GR）区间给予更高视觉分辨率
        clamped = max(self._min_db, min(self._max_db, db))
        hi_bound = -20.0 if self._mode == "level" else -14.0
        hi_norm = 0.52 if self._mode == "level" else 0.55
        if clamped >= hi_bound:
            norm = hi_norm + ((clamped - hi_bound) / (0.0 - hi_bound)) * (1.0 - hi_norm)
        else:
            norm = ((clamped - self._min_db) / (hi_bound - self._min_db)) * hi_norm
        return bar.bottom() - norm * bar.height()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 10.0
        bot_pad = 18.0 if self._title else 6.0
        bar_w = 10.0
        bar_x = (self.width() - bar_w - 3.0) if self._show_ticks else (self.width() - bar_w) / 2.0
        bar = QRectF(bar_x, top_pad, bar_w, max(40.0, self.height() - top_pad - bot_pad))

        # 1. 刻度文字
        if self._show_ticks:
            p.setFont(pro_font(7))
            for t in self._ticks:
                ty = self._db_to_y(float(t), bar)
                p.setPen(QColor("#787D8C") if self._active else QColor("#4A4D58"))
                p.drawText(
                    QRectF(0, ty - 7, bar.left() - 3, 14),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{t} -",
                )

        # 2. 表槽底色
        p.setPen(QPen(QColor("#282B35"), 1.0))
        p.setBrush(QColor("#0D0E12"))
        p.drawRoundedRect(bar, 2.0, 2.0)

        # 3. 填充
        if self._active:
            if self._mode == "level":
                y_val = self._db_to_y(self._value_db, bar)
                fill_h = bar.bottom() - y_val
                if fill_h > 1.0:
                    fill_rect = QRectF(bar.left() + 1.0, y_val, bar.width() - 2.0, fill_h - 0.5)
                    grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                    grad.setColorAt(0.0, QColor("#26C281"))
                    grad.setColorAt(0.50, QColor("#4CD490"))
                    grad.setColorAt(0.53, QColor("#E8C538"))
                    grad.setColorAt(0.75, QColor("#F0A848"))
                    grad.setColorAt(0.78, QColor("#F26D6D"))
                    grad.setColorAt(1.0, QColor("#FF4848"))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(grad)
                    p.drawRoundedRect(fill_rect, 1.2, 1.2)

                if self._peak_db > self._min_db + 0.5:
                    py = self._db_to_y(self._peak_db, bar)
                    p.setPen(QPen(QColor("#F26D6D") if self._peak_db > -10.0 else QColor("#F0F2F6"), 1.4))
                    p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

                clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#FF4848") if self._clipped else QColor("#22252E"))
                p.drawRoundedRect(clip_rect, 1.0, 1.0)
            else:
                # GR 自上而下
                y_val = self._db_to_y(self._value_db, bar)
                fill_h = y_val - bar.top()
                if fill_h > 1.0:
                    fill_rect = QRectF(bar.left() + 1.0, bar.top() + 0.5, bar.width() - 2.0, fill_h)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(self._accent)
                    p.drawRoundedRect(fill_rect, 1.2, 1.2)

                if self._peak_db < -0.3:
                    py = self._db_to_y(self._peak_db, bar)
                    p.setPen(QPen(QColor("#FFFFFF"), 1.3))
                    p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

        # 4. 横向刻度栅格遮罩
        p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
        gy = bar.top() + 3.0
        while gy < bar.bottom():
            p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
            gy += 3.0

        # 5. 底部标识
        if self._title:
            p.setFont(pro_font(8))
            p.setPen(QColor("#8E94A4") if self._active else QColor("#545864"))
            p.drawText(QRectF(0, self.height() - 16, self.width(), 15),
                       Qt.AlignmentFlag.AlignCenter, self._title)


# ═════════════════════════════════════════════════════════════════════════════
# 双声道输出电平表
# ═════════════════════════════════════════════════════════════════════════════

class ProStereoOutputMeter(QWidget):
    """双声道高精输出电平表（L/R 双条 + 峰值保持 + 削波灯）。"""

    TICKS = [0, -5, -10, -15, -20, -30, -40, -50]
    MIN_DB = -55.0
    MAX_DB = 0.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
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

    def setLevels(self, left_db: float, right_db: float) -> None:
        self._l_db = max(self.MIN_DB, min(self.MAX_DB, left_db))
        self._r_db = max(self.MIN_DB, min(self.MAX_DB, right_db))
        self._l_peak = max(self._l_db, self._l_peak - 0.35)
        self._r_peak = max(self._r_db, self._r_peak - 0.35)
        if left_db >= -0.1:
            self._l_clip = True
        if right_db >= -0.1:
            self._r_clip = True
        self.update()

    def setActive(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
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

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_pad = 12.0
        bot_pad = 18.0
        bar_w = 8.5
        l_x = 32.0
        r_x = 43.0
        bar_h = max(40.0, self.height() - top_pad - bot_pad)
        ref_bar = QRectF(l_x, top_pad, bar_w, bar_h)

        p.setFont(pro_font(7))
        for t in self.TICKS:
            ty = self._db_to_y(float(t), ref_bar)
            p.setPen(QColor("#787D8C") if self._active else QColor("#4A4D58"))
            p.drawText(
                QRectF(0, ty - 7, l_x - 4, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{t} -",
            )

        channels = [
            ("L", l_x, self._l_db, self._l_peak, self._l_clip),
            ("R", r_x, self._r_db, self._r_peak, self._r_clip),
        ]
        for ch_name, bx, cur_db, peak_db, clipped in channels:
            bar = QRectF(bx, top_pad, bar_w, bar_h)

            p.setPen(QPen(QColor("#282B35"), 1.0))
            p.setBrush(QColor("#0D0E12"))
            p.drawRoundedRect(bar, 2.0, 2.0)

            if self._active:
                y_val = self._db_to_y(cur_db, bar)
                fill_h = bar.bottom() - y_val
                if fill_h > 1.0:
                    fill_rect = QRectF(bar.left() + 1.0, y_val, bar.width() - 2.0, fill_h - 0.5)
                    grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                    grad.setColorAt(0.0, QColor("#26C281"))
                    grad.setColorAt(0.50, QColor("#4CD490"))
                    grad.setColorAt(0.53, QColor("#E8C538"))
                    grad.setColorAt(0.75, QColor("#F0A848"))
                    grad.setColorAt(0.78, QColor("#F26D6D"))
                    grad.setColorAt(1.0, QColor("#FF4848"))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(grad)
                    p.drawRoundedRect(fill_rect, 1.2, 1.2)

                if peak_db > self.MIN_DB + 0.5:
                    py = self._db_to_y(peak_db, bar)
                    p.setPen(QPen(QColor("#F26D6D") if peak_db > -10.0 else QColor("#F0F2F6"), 1.4))
                    p.drawLine(QPointF(bar.left() + 1.0, py), QPointF(bar.right() - 1.0, py))

            clip_rect = QRectF(bar.left(), bar.top() - 6.5, bar.width(), 4.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FF4848") if (clipped and self._active) else QColor("#22252E"))
            p.drawRoundedRect(clip_rect, 1.0, 1.0)

            p.setPen(QPen(QColor(13, 14, 18, 165), 1.0))
            gy = bar.top() + 3.0
            while gy < bar.bottom():
                p.drawLine(QPointF(bar.left() + 0.5, gy), QPointF(bar.right() - 0.5, gy))
                gy += 3.0

            p.setFont(pro_font(8))
            p.setPen(QColor("#8E94A4") if self._active else QColor("#545864"))
            p.drawText(QRectF(bx - 2, self.height() - 16, bar_w + 4, 15),
                       Qt.AlignmentFlag.AlignCenter, ch_name)


# ═════════════════════════════════════════════════════════════════════════════
# Make Up 补偿增益推子
# ═════════════════════════════════════════════════════════════════════════════

class MakeUpFaderStrip(QWidget):
    """Make Up 增益推子（0..+20 dB，双击归零，滚轮步进）。"""

    gainChanged = pyqtSignal(float)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._gain = 0.0
        self._dragging = False
        self.setFixedWidth(56)
        self.setMinimumHeight(180)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setGain(self, val: float, emit: bool = False) -> None:
        clamped = max(0.0, min(20.0, round(val, 1)))
        if abs(clamped - self._gain) > 1e-4:
            self._gain = clamped
            self.update()
            if emit:
                self.gainChanged.emit(self._gain)

    def _track_rect(self) -> QRectF:
        return QRectF(34.0, 10.0, 4.0, max(40.0, self.height() - 36.0))

    def _y_to_gain(self, y: float) -> float:
        tr = self._track_rect()
        norm = max(0.0, min(1.0, (y - tr.top()) / tr.height()))
        return 20.0 * (1.0 - norm)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.interactionStarted.emit()
            self.setGain(self._y_to_gain(event.position().y()), emit=True)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self.setGain(self._y_to_gain(event.position().y()), emit=True)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self._dragging:
                self._dragging = False
                self.interactionEnded.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.setGain(0.0, emit=True)
            self.interactionEnded.emit()

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y() / 120.0
        self.setGain(self._gain + delta * 0.5, emit=True)
        self.interactionEnded.emit()
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        tr = self._track_rect()
        cx = tr.center().x()

        p.setFont(pro_font(7))
        for val in (20, 15, 10, 5, 0):
            norm = (20.0 - val) / 20.0
            ty = tr.top() + norm * tr.height()
            p.setPen(QColor("#787D8C"))
            lbl = f"+{val} -" if val > 0 else "0 -"
            p.drawText(QRectF(0, ty - 7, tr.left() - 9, 14),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        p.setPen(QPen(QColor("#282B34"), 1.0))
        p.setBrush(QColor("#0E1014"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        norm = (20.0 - self._gain) / 20.0
        cy = tr.top() + norm * tr.height()
        if self._gain > 0.05:
            active_rect = QRectF(tr.left() + 0.5, cy, tr.width() - 1.0, tr.bottom() - cy)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(40, 216, 240, 150))
            p.drawRoundedRect(active_rect, 1.5, 1.5)

        cap_w, cap_h = 18.0, 22.0
        cap_rect = QRectF(cx - cap_w / 2.0, cy - cap_h / 2.0, cap_w, cap_h)
        grad = QLinearGradient(cap_rect.topLeft(), cap_rect.bottomLeft())
        grad.setColorAt(0.0, QColor("#565A68"))
        grad.setColorAt(0.5, QColor("#3A3D48"))
        grad.setColorAt(1.0, QColor("#4A4E5C"))
        p.setPen(QPen(QColor("#111216"), 1.2))
        p.setBrush(grad)
        p.drawRoundedRect(cap_rect, 3.0, 3.0)

        p.setPen(QPen(QColor("#28D8F0"), 1.6))
        p.drawLine(QPointF(cap_rect.left() + 3, cy), QPointF(cap_rect.right() - 3, cy))

        font = pro_font(9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor("#28D8F0"))
        val_str = f"+{self._gain:.1f}" if self._gain >= 0.05 else "0.0"
        p.drawText(QRectF(4, self.height() - 21, self.width() - 4, 18),
                   Qt.AlignmentFlag.AlignCenter, val_str)


# ═════════════════════════════════════════════════════════════════════════════
# 模块卡片基类
# ═════════════════════════════════════════════════════════════════════════════

class ProModuleCard(QFrame):
    """底部参数模块卡片（主题色顶边 + 标题 + Body 区）。

    子类通过 add_section() 追加水平分区；setModuleActive 切换激活视觉。
    """

    def __init__(self, title: str, badge_text: str, accent_color: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._accent = accent_color
        self._active = True
        self._cells: list[QWidget] = []

        self.setObjectName("ModuleCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(36)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 6, 12, 5)
        h_lay.setSpacing(6)

        self.title_lbl = QLabel(title)
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        self.badge_lbl = QLabel(badge_text)
        self.badge_lbl.setStyleSheet(
            "color: #6E7382; font-size: 8.5px; font-weight: 700; letter-spacing: 0.6px;")
        h_lay.addWidget(self.badge_lbl)
        root.addWidget(header)

        self._body_hosts: list[QFrame] = []
        self.setModuleActive(True)

    def add_section(self) -> QHBoxLayout:
        """追加一个水平 Body 分区，返回其布局。"""
        body = QFrame()
        body.setObjectName("CardBody")
        self._body_hosts.append(body)
        lay = QHBoxLayout(body)
        lay.setContentsMargins(8, 10, 8, 12)
        lay.setSpacing(12)
        # 插到 header 之后
        self.layout().addWidget(body, 1)
        return lay

    def setBadgeText(self, text: str) -> None:
        self.badge_lbl.setText(text)

    def setModuleActive(self, active: bool) -> None:
        self._active = active
        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: {'#1D1F25' if active else '#181A20'};
                border: 1px solid #2A2D37;
                border-top: 2px solid {self._accent if active else '#343844'};
                border-radius: 5px;
            }}
            QFrame#CardBody {{
                background-color: transparent;
                border-top: 1px solid #262932;
            }}
        """)
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if active else '#6E727E'}; "
            "font-size: 11px; font-weight: 700; letter-spacing: 0.3px;")


# ═════════════════════════════════════════════════════════════════════════════
# 对话框级统一样式表
# ═════════════════════════════════════════════════════════════════════════════

PRO_DIALOG_QSS = """
QWidget {
    background-color: #17191E;
    color: #E2E5EE;
    font-family: 'SF Pro Display', 'Helvetica Neue', 'Segoe UI', 'PingFang SC', 'Microsoft YaHei';
}
QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch, ProVerticalMeter,
ProStereoOutputMeter, MakeUpFaderStrip {
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
/* 关键：主窗口 DARK_THEME 的 QPushButton { padding: 5px 15px; ... } 会级联
   进对话框（对话框 parent=主窗口）——24px 宽的 A/B、22px 的 S 等小按钮
   内容区被 30px 水平内边距吃成负值，文字整体被裁空（只剩色块）。
   这里显式声明全部按钮基础属性，就近样式表优先生效，阻断级联污染。 */
QPushButton {
    background-color: #22252E;
    color: #E2E5EE;
    border: 1px solid #303440;
    border-radius: 4px;
    padding: 0px 4px;
    font-size: 11px;
}
QPushButton:hover {
    color: #FFFFFF;
    border-color: #424858;
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
"""
