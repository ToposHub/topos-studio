from __future__ import annotations

import sys
import math
import bisect
import copy
import random
from functools import lru_cache

import numpy as np
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QFrame, QLineEdit, QSizePolicy, QMenu,
    QScrollArea
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont, QMouseEvent, QPolygonF, QImage, QPixmap, QKeySequence, QShortcut
)


from src.features.edit.core.fade_curve_spec import fade_curve_from_midpoint, fade_power
from src.features.edit.ui.fade_overlay import (
    FADE_HANDLE_HIT_HEIGHT, FADE_HANDLE_HIT_RADIUS, FADE_HANDLE_SIZE,
    paint_fade_curve_point, paint_fade_ramp, paint_fade_tab,
)

# =====================================================================
# 1. 全局色彩系统与声学常量（与 EQ-UI.py 保持一致的暗色工作室美学）
# =====================================================================
ACCENT_CYAN    = "#28D8F0"  # 播放头 / 主激活高亮 / 选区边框
ACCENT_GREEN   = "#4CD490"  # 波形主体 / 安全电平 / 干净度良好
ACCENT_AMBER   = "#F0A848"  # 淡入淡出曲线 / 警告电平 / 增益调节
ACCENT_RED     = "#F26D6D"  # 削波过载 (>0dBFS) / 静音与频谱修复
ACCENT_BLUE    = "#6495FA"  # 辅助网格 / 标尺
ACCENT_VIOLET  = "#B67CF8"  # 语图高能量过渡色

# 跨平台字体栈（QSS 用）。注意：macOS 的 QSS 会为字体列表中【任何一个】缺失
# 字体族触发 qt.qpa.fonts 警告并付出 ~230ms 别名填充开销，因此列表只保留
# macOS 原生字体（Segoe UI / SF Pro Display / Consolas / monospace 均会告警，
# 不得再加回列表首位）；QFont 代码路径统一走 MONO_FONT_FAM。
UI_FONT_CSS   = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"  # QFont 渲染用


def format_timecode(sec: float) -> str:
    sec = max(0.0, sec)
    mins = int(sec // 60)
    rem = sec - mins * 60
    return f"{mins:02d}:{rem:06.3f}"


def format_freq_short(hz: float) -> str:
    if hz >= 1000.0:
        return f"{hz / 1000.0:.1f}kHz"
    return f"{int(round(hz))}Hz"


def _display_fade_factor(curve: str, progress: float) -> float:
    power = fade_power(curve)
    if power is not None:
        return progress ** power
    if curve == "S-Curve":
        return progress * progress * (3.0 - 2.0 * progress)
    if curve == "Log":
        return math.sqrt(progress)
    if curve == "Equal Power":
        return math.sin(math.pi * progress / 2.0)
    return progress


def freq_to_note_name(hz: float) -> str:
    if hz <= 16.0:
        return ""
    notes = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    midi = int(round(69 + 12 * math.log2(hz / 440.0)))
    octave = (midi // 12) - 1
    return f"{notes[midi % 12]}{octave}"


def build_spectrogram_lut() -> list[tuple[int, int, int]]:
    """生成 256 级低眩光工作室热力图 LUT（深海蓝 -> 钴蓝 -> 冰晶青 -> 琥珀金 -> 珊瑚红 -> 暖白）"""
    stops = [
        (0.00, (14, 17, 26)),
        (0.22, (24, 48, 98)),
        (0.45, (36, 138, 196)),
        (0.66, (76, 212, 144)),
        (0.84, (240, 168, 72)),
        (0.95, (242, 96, 96)),
        (1.00, (255, 244, 228)),
    ]
    lut: list[tuple[int, int, int]] = []
    for i in range(256):
        t = i / 255.0
        for j in range(len(stops) - 1):
            t0, c0 = stops[j]
            t1, c1 = stops[j + 1]
            if t0 <= t <= t1:
                u = (t - t0) / max(1e-6, t1 - t0)
                r = int(c0[0] + (c1[0] - c0[0]) * u)
                g = int(c0[1] + (c1[1] - c0[1]) * u)
                b = int(c0[2] + (c1[2] - c0[2]) * u)
                lut.append((r, g, b))
                break
    return lut


SPEC_LUT = build_spectrogram_lut()
_SPEC_LUT_RGB = np.asarray(SPEC_LUT, dtype=np.uint8)
_WAVE_SHADES = 48
_WAVE_RMS, _WAVE_EDGE, _WAVE_GHOST, _WAVE_GHOST_EDGE, _WAVE_MUTED, _WAVE_MUTED_EDGE = range(
    1 + 2 * _WAVE_SHADES, 7 + 2 * _WAVE_SHADES)


@lru_cache(maxsize=1)
def _waveform_colors() -> list[int]:
    """Indexed colors for the dense waveform (peak, peak + RMS, and the edges)."""
    shades = _WAVE_SHADES
    peak = [QColor(36 + i * 40 // (shades - 1), 170 + i * 42 // (shades - 1),
                   132 + i * 12 // (shades - 1), 30 + i * 100 // (shades - 1))
            for i in range(shades)]
    # The RMS layer sits in front of the peak fill, in the brighter center of the waveform.
    rms = QColor(40, 216, 240, 105)
    combined = []
    rms_weight = rms.alphaF()
    for color in peak:
        peak_weight = color.alphaF() * (1.0 - rms_weight)
        alpha = rms_weight + peak_weight
        red = round((rms.red() * rms_weight + color.red() * peak_weight) / alpha)
        green = round((rms.green() * rms_weight + color.green() * peak_weight) / alpha)
        blue = round((rms.blue() * rms_weight + color.blue() * peak_weight) / alpha)
        combined.append(QColor(red, green, blue, round(alpha * 255.0)))
    extra = [rms, QColor(110, 238, 175, 200), QColor(255, 255, 255, 24),
             QColor(255, 255, 255, 63), QColor(90, 98, 114, 24), QColor(110, 118, 136, 105)]
    return [0] + [color.rgba() for color in peak + combined + extra]


# =====================================================================
# 2. 复用视觉基因的基础控件：开关、数字胶囊、拉丝阳极铝旋钮
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
        active = self._checked and self.isEnabled()
        bg = QColor("#12262E") if active else QColor("#121215")
        border = QColor(ACCENT_CYAN) if active else QColor("#36363D")
        p.setPen(QPen(border, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 8.5, 8.5)

        r = 5.2
        cy = self.height() / 2.0
        cx = (self.width() - 8.8) if self._checked else 8.8
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ACCENT_CYAN) if active else QColor("#666670"))
        p.drawEllipse(QPointF(cx, cy), r, r)


class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "0.0", accent_color: str = ACCENT_CYAN, width: int = 64, parent=None):
        super().__init__(parent)
        self._text = text
        self._accent = accent_color
        self._hovered = False
        self.setFixedSize(width, 21)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(0, 0, width, 21)
        self._editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._editor.setStyleSheet(f"""
            QLineEdit {{
                background-color: #090B0E;
                color: {accent_color};
                border: 1px solid {accent_color};
                border-radius: 4px;
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
        if text != self._text:
            self._text = text
            self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._editor.setText(self._text.replace("+", "").replace("dB", "").replace("s", "").strip())
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

        # 达芬奇插件风格：纯黑内凹数据框，悬停时边框点亮为主题色
        border_col = QColor(self._accent) if self._hovered else QColor("#121214")
        p.setPen(QPen(border_col, 1.0))
        p.setBrush(QColor("#151518"))
        p.drawRoundedRect(rect, 3.0, 3.0)

        p.setPen(QColor("#ECECF0" if self.isEnabled() else "#90919B"))
        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


class ProRotaryKnob(QWidget):
    valueChanged = pyqtSignal(float)
    valueCommitted = pyqtSignal(float)

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
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(34, 34)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def value(self) -> float:
        return self._value

    def setValue(self, val: float, emit: bool = False):
        clamped = max(self._min, min(self._max, val))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def _val_to_norm(self, val: float) -> float:
        if self._max <= self._min:
            return 0.0
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        n = max(0.0, min(1.0, norm))
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
            self.valueCommitted.emit(self._value)

    def mouseMoveEvent(self, event):
        if self._dragging:
            dy = self._last_y - event.position().y()
            self._last_y = event.position().y()
            sens = 0.0015 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.0065
            norm = self._val_to_norm(self._value) + dy * sens
            self.setValue(self._norm_to_val(norm), emit=True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.valueCommitted.emit(self._value)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._default, emit=True)
            self.valueCommitted.emit(self._value)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        step = 0.01 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.03
        norm = self._val_to_norm(self._value) + delta * step
        self.setValue(self._norm_to_val(norm), emit=True)
        self.valueCommitted.emit(self._value)
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        norm = max(0.0, min(1.0, self._val_to_norm(self._value)))

        start_deg = 225.0
        total_span = -270.0
        curr_deg = start_deg + norm * total_span
        arc_r = 13.8
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深黑轨道底槽
        p.setPen(QPen(QColor("#141417"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 2. 激活数值弧（克制单层，无外发光）
        accent = self._accent if self.isEnabled() else QColor("#666670")
        p.setPen(QPen(accent, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._mode == "bipolar":
            span_deg = curr_deg - 90.0
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(90.0 * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        # 3. 旋钮黑边凹槽 + 达芬奇风格哑光浅灰旋钮顶盖
        knob_r = 10.2
        p.setPen(QPen(QColor("#111114"), 1.1))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 4. 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 2.8
        iy1 = cy - math.sin(rad) * 2.8
        ix2 = cx + math.cos(rad) * (knob_r - 1.2)
        iy2 = cy - math.sin(rad) * (knob_r - 1.2)
        p.setPen(QPen(QColor("#141416"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 3. 控件：全片概览导航条 (Minimap Navigator)
# =====================================================================
class AudioOverviewMinimap(QWidget):
    viewportChanged = pyqtSignal(float, float)  # view_start_sec, view_end_sec
    seekRequested = pyqtSignal(float)
    playheadMoved = pyqtSignal(float)           # 拖拽概览条播放头时发出（sec）
    playheadDragFinished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

        self._duration = 10.0
        self._view_start = 0.0
        self._view_end = 10.0
        self._playhead = 0.0
        self._sel_start = -1.0
        self._sel_end = -1.0
        self._peaks_max: list[float] = []
        self._summary_cache: QPixmap | None = None

        self._drag_mode = ""  # "move", "left", "right"
        self._drag_offset = 0.0
        # 左右内边距：视口框与抓手永远离开窗口边缘，避免想抓抓手时误触 macOS 的窗口缩放热区
        self._edge_pad = 8.0

    def setAudioSummary(self, duration: float, peaks_max_mono: list[float]):
        self._duration = max(0.01, duration)
        self._peaks_max = peaks_max_mono
        self._summary_cache = None
        self.update()

    def setViewport(self, start_sec: float, end_sec: float):
        start = max(0.0, min(self._duration - 0.005, start_sec))
        end = max(start + 0.005, min(self._duration, end_sec))
        if (start, end) != (self._view_start, self._view_end):
            self._view_start, self._view_end = start, end
            self.update()

    def setPlayhead(self, sec: float):
        if sec != self._playhead:
            old_x = self._sec_to_x(self._playhead)
            self._playhead = sec
            for x in (old_x, self._sec_to_x(sec)):
                self.update(QRectF(x - 6, 0, 12, self.height()).toAlignedRect())

    def setSelection(self, start_sec: float, end_sec: float):
        if (start_sec, end_sec) != (self._sel_start, self._sel_end):
            self._sel_start, self._sel_end = start_sec, end_sec
            self.update()

    def _sec_to_x(self, sec: float) -> float:
        # 时间 -> 像素：映射进 [edge_pad, width - edge_pad]，两端留出抓取余量
        pad = self._edge_pad
        return pad + (sec / self._duration) * max(1.0, self.width() - pad * 2.0)

    def _x_to_sec(self, x: float) -> float:
        pad = self._edge_pad
        usable = max(1.0, self.width() - pad * 2.0)
        return max(0.0, min(self._duration, ((x - pad) / usable) * self._duration))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        vx0 = self._sec_to_x(self._view_start)
        vx1 = self._sec_to_x(self._view_end)

        # 播放头与缩放把手重叠时，顶部三角用于定位，下方把手用于缩放。
        left_distance, right_distance = abs(x - vx0), abs(x - vx1)
        at_handle = min(left_distance, right_distance) <= 6.0
        if abs(x - self._sec_to_x(self._playhead)) <= 6.0 and (
                not at_handle or event.position().y() < 7.0):
            self._drag_mode = "playhead"
            self.setPlayhead(self._x_to_sec(x))
            self.playheadMoved.emit(self._playhead)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return

        if at_handle:
            self._drag_mode = "left" if left_distance <= right_distance else "right"
        elif vx0 <= x <= vx1:
            self._drag_mode = "move"
            self._drag_offset = self._x_to_sec(x) - self._view_start
        else:
            # 点击视口外：将视口中心平移至点击位置
            span = self._view_end - self._view_start
            center = self._x_to_sec(x)
            ns = max(0.0, min(self._duration - span, center - span * 0.5))
            self.viewportChanged.emit(ns, ns + span)
            self._drag_mode = "move"
            self._drag_offset = center - ns

    def mouseMoveEvent(self, event):
        x = event.position().x()
        vx0 = self._sec_to_x(self._view_start)
        vx1 = self._sec_to_x(self._view_end)

        if not self._drag_mode:
            if any(abs(x - marker) <= 6.0 for marker in (self._sec_to_x(self._playhead), vx0, vx1)):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif vx0 <= x <= vx1:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            return

        sec = self._x_to_sec(x)
        min_span = min(self._duration, 0.02)

        if self._drag_mode == "playhead":
            self.setPlayhead(sec)
            self.playheadMoved.emit(sec)
            return

        if self._drag_mode == "left":
            ns = max(0.0, min(self._view_end - min_span, sec))
            self.viewportChanged.emit(ns, self._view_end)
        elif self._drag_mode == "right":
            ne = max(self._view_start + min_span, min(self._duration, sec))
            self.viewportChanged.emit(self._view_start, ne)
        elif self._drag_mode == "move":
            span = self._view_end - self._view_start
            ns = max(0.0, min(self._duration - span, sec - self._drag_offset))
            self.viewportChanged.emit(ns, ns + span)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            mode, self._drag_mode = self._drag_mode, ""
            if mode == "playhead":
                self.playheadDragFinished.emit()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # 双击导航条一键重置为全片视图
            self.viewportChanged.emit(0.0, self._duration)

    def _paint_summary(self, p: QPainter) -> None:
        w, h = float(self.width()), float(self.height())
        cy = h / 2.0
        # 内容内收：缩略波形/刻度/视口框两端各留 edge_pad，避免抓手贴窗口边缘
        mx0, mx1 = self._edge_pad, w - self._edge_pad

        # 达芬奇风格纯平底色（与画布容器 #1E1E22 统一）
        p.fillRect(self.rect(), QColor("#1E1E22"))

        # 顶部微时间刻度点阵
        p.setPen(QPen(QColor("#2A2A2E"), 1.0))
        tick_step = max(16.0, (mx1 - mx0) / 30.0)
        tx = mx0 + tick_step
        while tx < mx1 - 4.0:
            p.drawLine(QPointF(tx, 1.0), QPointF(tx, 4.0))
            tx += tick_step

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.drawLine(QPointF(mx0, cy), QPointF(mx1, cy))

        # 1. 平滑对称多边形缩略波形
        n = len(self._peaks_max)
        if n > 2:
            cols = max(2, int((mx1 - mx0) // 2))
            wave_poly = QPolygonF()
            bot_pts: list[QPointF] = []
            for i in range(cols + 1):
                px = mx0 + (i / cols) * (mx1 - mx0)
                idx = min(n - 1, int((i / cols) * n))
                amp = min(1.0, self._peaks_max[idx]) * (h * 0.37)
                wave_poly.append(QPointF(px, cy - amp))
                bot_pts.append(QPointF(px, cy + amp))
            for pt in reversed(bot_pts):
                wave_poly.append(pt)

            fill_grad = QLinearGradient(0, cy - h * 0.37, 0, cy + h * 0.37)
            fill_grad.setColorAt(0.0, QColor(76, 212, 144, 155))
            fill_grad.setColorAt(0.5, QColor(40, 216, 240, 50))
            fill_grad.setColorAt(1.0, QColor(76, 212, 144, 155))
            p.setPen(QPen(QColor(96, 232, 166, 190), 1.0))
            p.setBrush(fill_grad)
            p.drawPolygon(wave_poly)

    def paintEvent(self, event):
        dpr = self.devicePixelRatioF()
        w, h = self.width(), self.height()
        if (self._summary_cache is None or self._summary_cache.devicePixelRatio() != dpr
                or self._summary_cache.deviceIndependentSize().toSize() != self.size()):
            cache = QPixmap(round(w * dpr), round(h * dpr))
            cache.setDevicePixelRatio(dpr)
            with QPainter(cache) as summary:
                summary.setRenderHint(QPainter.RenderHint.Antialiasing)
                self._paint_summary(summary)
            self._summary_cache = cache
        p = QPainter(self)
        p.drawPixmap(0, 0, self._summary_cache)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 2. 选区高亮标记
        if self._sel_end > self._sel_start >= 0.0:
            sx0 = self._sec_to_x(self._sel_start)
            sx1 = self._sec_to_x(self._sel_end)
            p.fillRect(QRectF(sx0, 0, max(2.0, sx1 - sx0), h), QColor(240, 168, 72, 65))

        # 3. 视口暗化遮罩 + 玻璃态视口框
        vx0 = self._sec_to_x(self._view_start)
        vx1 = self._sec_to_x(self._view_end)
        mask_col = QColor(5, 7, 11, 195)
        if vx0 > 0:
            p.fillRect(QRectF(0, 0, vx0, h), mask_col)
        if vx1 < w:
            p.fillRect(QRectF(vx1, 0, w - vx1, h), mask_col)

        vp_rect = QRectF(vx0 + 0.5, 1.0, max(10.0, vx1 - vx0 - 1.0), h - 2.0)
        vp_grad = QLinearGradient(vp_rect.topLeft(), vp_rect.bottomLeft())
        vp_grad.setColorAt(0.0, QColor(40, 216, 240, 32))
        vp_grad.setColorAt(0.4, QColor(40, 216, 240, 10))
        vp_grad.setColorAt(1.0, QColor(40, 216, 240, 22))
        p.setPen(QPen(QColor(ACCENT_CYAN), 1.3))
        p.setBrush(vp_grad)
        p.drawRoundedRect(vp_rect, 4.0, 4.0)

        # 左右机械防滑抓手（带中心刻痕）
        for gx in (vx0, vx1 - 4.5):
            grip_r = QRectF(gx, h * 0.22, 4.5, h * 0.56)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT_CYAN))
            p.drawRoundedRect(grip_r, 2.0, 2.0)
            p.setPen(QPen(QColor("#0A1016"), 1.0))
            p.drawLine(QPointF(grip_r.center().x(), grip_r.top() + 3.5), QPointF(grip_r.center().x(), grip_r.bottom() - 3.5))

        # 4. 缩放倍率微徽章（仅在局部放大时浮现）
        span = max(1e-4, self._view_end - self._view_start)
        zoom_x = self._duration / span
        if zoom_x > 1.05:
            z_txt = f"{zoom_x:.1f}x"
            z_rect = QRectF(w - 42.0, 4.0, 36.0, 14.0)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor(20, 20, 23, 220))
            p.drawRoundedRect(z_rect, 3.0, 3.0)
            p.setPen(QColor(ACCENT_CYAN))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(z_rect, Qt.AlignmentFlag.AlignCenter, z_txt)

        # 5. 播放头指示线 + 顶部迷你三角游标（与主画布一致的红色）
        px = self._sec_to_x(self._playhead)
        p.setPen(QPen(QColor(255, 82, 82, 70), 3.5))
        p.drawLine(QPointF(px, 0), QPointF(px, h))
        p.setPen(QPen(QColor("#FF5252"), 1.2))
        p.drawLine(QPointF(px, 0), QPointF(px, h))

        mini_head = QPolygonF([
            QPointF(px - 3.5, 0.5),
            QPointF(px + 3.5, 0.5),
            QPointF(px, 5.5),
        ])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF5252"))
        p.drawPolygon(mini_head)

        # 注：概览条自身不再画四周边框，交由外部 mini_sep 与 canvas_deck 统一画 1px 黑线，避免双层叠线


# =====================================================================
# 4. 核心画布：Audition 风格同步波形 + 频谱瀑布图编辑器
# =====================================================================
class WaveformSpectralCanvas(QWidget):
    playheadMoved = pyqtSignal(float)
    playheadDragFinished = pyqtSignal()
    selectionChanged = pyqtSignal(float, float, float, float)  # t0, t1, f_low, f_high
    selectionDragFinished = pyqtSignal()
    viewportChanged = pyqtSignal(float, float)
    previewGainChanged = pyqtSignal(float)
    gainCommitRequested = pyqtSignal(float, float, float)      # t0, t1, gain_db
    fadeChanged = pyqtSignal(float, float)                     # fade_in_sec, fade_out_sec
    fadeCurveCommitted = pyqtSignal(str, str)                  # in/out, model curve
    spectralActionRequested = pyqtSignal(str, float, float, float, float)  # action, t0, t1, f_low, f_high
    spectralBrushApplied = pyqtSignal(list, float)        # [(t, freq), ...], radius_px 修复笔涂抹轨迹（已做本地视觉平滑）
    channelStateChanged = pyqtSignal(int, bool, bool)     # ch_idx, muted, phase_inverted
    markersChanged = pyqtSignal(list)                     # [{"time": float, "label": str}, ...]
    envelopeChanged = pyqtSignal(list)                    # [{"t": float, "db": float}, ...] 音量包络关键帧（按 t 升序）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(340)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

        # 音频数据缓存
        self._sample_rate = 48000
        self._duration = 10.0
        self._channels = 2
        self._peaks_min: list[list[float]] = [[], []]
        self._peaks_max: list[list[float]] = [[], []]
        self._rms: list[list[float]] = [[], []]
        self._wave_channels: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        self._cache_wave_channels()
        self._spec_matrix: list[list[float]] = []  # [time_bins][freq_bins] 归一化 0.0~1.0
        self._spec_values: np.ndarray | None = None
        self._spec_image: QImage | None = None
        self._spectrum_dirty = False

        # 视图状态
        self._view_start = 0.0
        self._view_end = 10.0
        self._playhead = 0.0
        self._show_spectral = True
        self._spectral_ratio = 0.44  # 频谱图占主画布高度比例
        self._spec_gain_boost = 0.0  # 仅视觉增强频谱底噪可见度 (dB)
        self._stretch_preview_ratio = 100.0  # 时间伸缩速率预览（画布投射目标落点虚线）

        # 选区状态 (1D 时间选区 或 2D 频谱选区)
        self._sel_t0 = -1.0
        self._sel_t1 = -1.0
        self._sel_f_low = 20.0
        self._sel_f_high = 24000.0
        self._is_2d_spectral_sel = False

        # 非破坏性实时预览参数
        self._preview_gain_db = 0.0
        self._edit_channel = -1           # 增益编辑目标声道：-1=全部(L+R)，0=左，1=右
        self._fade_in_sec = 0.0
        self._fade_out_sec = 0.0
        self._amp_zoom = 1.0               # 波形垂直显示放大倍率 (1.0 ~ 16.0, 仅视觉不改变实际音量)
        self._fade_curve_type = "S-Curve"  # "Linear" | "S-Curve" | "Log"
        self._fade_out_curve_type = self._fade_curve_type
        self.fade_controls_editable = True
        self._fade_curve_drag: tuple[str, float, float, float] | None = None
        self._fade_curve_preview: str | None = None
        self._tool_mode = "select"         # "select" (框选) | "brush" (污点修复画笔)

        # 交互状态机
        self._drag_mode = ""  # "select_wave", "select_spec", "sel_left", "sel_right", "ruler_nav", "splitter", "gain_hud", "fade_in", "fade_out", "pan", "brush_paint"
        self._drag_anchor_t = 0.0
        self._drag_anchor_f = 0.0
        self._selection_fixed_edge = 0.0
        self._drag_start_y = 0.0
        self._drag_start_gain = 0.0
        self._pan_start_v0 = 0.0
        self._pan_start_v1 = 0.0
        self._mouse_pos: QPointF | None = None

        # 布局边距（加宽左侧刻度槽以容纳可交互 CH 按钮与悬停音高指示）
        self._ml = 54.0
        self._mr = 2.0
        self._mt = 24.0   # 顶部时间标尺高度
        self._mb = 10.0

        # 声道开关 / 标记点 / 修复画笔 / 标尺导航状态
        self._ch_muted: list[bool] = [False, False]
        self._ch_phase_inv: list[bool] = [False, False]
        self._markers: list[dict] = []
        self._brush_stroke: list[tuple[float, float]] = []
        self._brush_radius_px = 14.0
        self._ruler_anchor_sec = 0.0
        self._ruler_dragged = False

        # 悬浮增益 HUD 热区
        self._gain_hud_rect = QRectF()
        self._gain_commit_rect = QRectF()
        self.selection_gain_editable = True
        self.project_edits_enabled = True
        self.channel_monitor_enabled = True
        self.waveform_includes_processing = False

        # 音量包络线（Vocal Riding）：关键帧按 t 升序，0dB 基准磁吸
        self._env_points: list[dict] = []          # [{"t": float, "db": float}, ...]
        self._env_enabled = True
        self._env_curve_mode = "smooth"            # "linear" | "smooth"
        self._env_hover_idx = -1                   # 当前悬停的节点索引
        self._env_hover_seg = -1                   # 当前悬停的线段索引
        self._env_selected_idx = -1                # 当前选中的节点索引（Del/Backspace 删除）
        self._env_drag_indices: list[int] = []     # 正在拖拽的节点索引列表（单点 / 线段两点 / 选区4点）
        self._env_drag_init_pts: list[dict] = []   # 拖拽起点快照
        self._env_drag_anchor_pos = QPointF()
        self._env_snap_zero_flash = False          # 0.0 dB 磁吸触发指示

        # 磁吸开关（由主面板"⌖ 过零"吸附开关联动控制）
        self._magnetic_snap = True

        # 静态渲染缓存与惯性/磁吸物理引擎（60Hz 物理帧率）
        self._init_kinetic_physics()

    # -----------------------------------------------------------------
    # 公共数据注入接口
    # -----------------------------------------------------------------
    def loadAudioData(
        self,
        sample_rate: int,
        duration_sec: float,
        peaks_min: list[list[float]],
        peaks_max: list[list[float]],
        rms: list[list[float]],
        spec_matrix: list[list[float]] | None = None,
        *,
        preserve_edit_view: bool = False,
    ):
        self._sample_rate = max(8000, sample_rate)
        self._duration = max(0.01, duration_sec)
        self._channels = max(1, len(peaks_max))
        self._peaks_min = peaks_min
        self._peaks_max = peaks_max
        self._rms = rms
        self._cache_wave_channels()
        if not preserve_edit_view:
            self._view_start = 0.0
            self._view_end = self._duration
            self._playhead = 0.0
            self._sel_t0 = -1.0
            self._sel_t1 = -1.0
            self._preview_gain_db = 0.0
            self._fade_in_sec = 0.0
            self._fade_out_sec = 0.0
            self._env_points.clear()
            self._env_hover_idx = -1
            self._env_hover_seg = -1
            self._env_selected_idx = -1

        if spec_matrix and spec_matrix is not self._spec_matrix:
            self._spec_matrix = spec_matrix
            self._spec_values = None
            self._rebuild_spectrogram_image()
        elif not spec_matrix:
            self._spec_matrix = []
            self._spec_values = None
            self._spec_image = None
        self._invalidate_cache()

    def _cache_wave_channels(self) -> None:
        self._wave_channels = [
            (np.asarray(high), np.asarray(low), np.asarray(self._rms[ch] if ch < len(self._rms) else high))
            for ch, (high, low) in enumerate(zip(self._peaks_max, self._peaks_min))
        ]

    def setSpectralVisible(self, visible: bool):
        if visible != self._show_spectral:
            self._show_spectral = visible
            self._invalidate_cache()

    def setSpectralContrastBoost(self, boost_db: float):
        if self._spec_gain_boost == boost_db:
            return
        self._spec_gain_boost = boost_db
        self._rebuild_spectrogram_image()
        self._invalidate_spectrogram()

    def _invalidate_spectrogram(self) -> None:
        if self._show_spectral:
            self._spectrum_dirty = True
            self.update(self._wave_and_spec_rects()[1].toAlignedRect())

    def setViewport(self, start_sec: float, end_sec: float):
        start = max(0.0, min(self._duration - 0.005, start_sec))
        end = max(start + 0.005, min(self._duration, end_sec))
        if (start, end) != (self._view_start, self._view_end):
            self._view_start, self._view_end = start, end
            self._invalidate_cache()

    def setPlayhead(self, sec: float):
        sec = max(0.0, min(self._duration, sec))
        if sec != self._playhead:
            old, self._playhead = self._playhead, sec
            body = self._body_rect()
            for time in (old, sec):
                if self._view_start <= time <= self._view_end:
                    x = self._time_to_x(time, body)
                    self.update(QRectF(x - 8, 0, 16, self.height()).toAlignedRect())

    def setPreviewGain(self, gain_db: float):
        value = max(-36.0, min(24.0, round(gain_db, 1)))
        if value != self._preview_gain_db:
            self._preview_gain_db = value
            self._invalidate_cache()

    def setEditChannel(self, ch: int):
        """设置增益编辑目标声道：-1=全部(L+R)，0=左声道，1=右声道"""
        if ch != self._edit_channel:
            self._edit_channel = ch
            self._invalidate_cache()

    def _channel_gain_db(self, ch: int) -> float:
        """返回该声道当前生效的预览增益（非目标声道恒为 0dB）"""
        if self._edit_channel == -1 or self._edit_channel == ch:
            return self._preview_gain_db
        return 0.0

    def setFadeCurveType(self, curve_type: str) -> None:
        self.setFadeCurves(curve_type, curve_type)

    def setFadeCurves(self, fade_in_curve: str, fade_out_curve: str) -> None:
        self._fade_curve_type = fade_in_curve
        self._fade_out_curve_type = fade_out_curve
        self._refresh_processing_overlay()

    def _refresh_processing_overlay(self) -> None:
        if self.waveform_includes_processing:
            self.update()
        else:
            self._invalidate_cache()

    def cancel_fade_drag(self) -> None:
        is_dragging = self._drag_mode in {"fade_in", "fade_out", "fade_curve"}
        if is_dragging or self._fade_curve_drag is not None:
            if is_dragging:
                self._drag_mode = ""
                self.unsetCursor()
            self._fade_curve_drag = None
            self._fade_curve_preview = None
            self._refresh_processing_overlay()

    def setToolMode(self, mode: str):
        self._tool_mode = mode
        self.update()

    def setStretchPreviewRatio(self, ratio_pct: float):
        """时间伸缩速率预览（拧动右侧"伸缩速率"旋钮时实时更新画布落点投射）"""
        if ratio_pct != self._stretch_preview_ratio:
            self._stretch_preview_ratio = ratio_pct
            self.update()

    def clearSelection(self):
        self._sel_t0 = -1.0
        self._sel_t1 = -1.0
        self._is_2d_spectral_sel = False
        self.setPreviewGain(0.0)
        self._env_selected_idx = -1   # Esc 同时清除包络节点选中态
        self.selectionChanged.emit(-1.0, -1.0, 20.0, self._nyquist())
        self.update()

    def selectAll(self):
        self._sel_t0 = 0.0
        self._sel_t1 = self._duration
        self._sel_f_low = 20.0
        self._sel_f_high = self._nyquist()
        self._is_2d_spectral_sel = False
        self._notify_selection_changed()

    def setSelection(self, start_sec: float, end_sec: float,
                     f_low: float = 20.0, f_high: float | None = None,
                     *, spectral: bool = False) -> None:
        """程序化设置选区（过零吸附、外部命令等）；保持时间顺序并通知监听者。"""
        start = max(0.0, min(self._duration, float(start_sec)))
        end = max(0.0, min(self._duration, float(end_sec)))
        if end < start:
            start, end = end, start
        self._sel_t0 = start
        self._sel_t1 = end
        self._sel_f_low = f_low if f_low > 0 else 20.0
        self._sel_f_high = f_high if f_high and f_high > 0 else self._nyquist()
        self._is_2d_spectral_sel = spectral
        self._notify_selection_changed()

    def is_selecting(self) -> bool:
        return self._drag_mode in ("select_wave", "select_spec", "sel_left", "sel_right")

    def _notify_selection_changed(self) -> None:
        self.selectionChanged.emit(self._sel_t0, self._sel_t1, self._sel_f_low, self._sel_f_high)
        if abs(self._preview_gain_db) >= 0.05:
            self._invalidate_cache()
        else:
            self.update()

    def getSelection(self) -> tuple[float, float, float, float]:
        if self._sel_t1 > self._sel_t0 >= 0.0:
            return (self._sel_t0, self._sel_t1, self._sel_f_low, self._sel_f_high)
        return (0.0, self._duration, 20.0, self._nyquist())

    def addMarker(self, sec: float | None = None, label: str = ""):
        t = self._playhead if sec is None else max(0.0, min(self._duration, sec))
        idx = len(self._markers) + 1
        self._markers.append({"time": t, "label": label or f"M{idx}"})
        self._markers.sort(key=lambda m: m["time"])
        self.markersChanged.emit(list(self._markers))
        self.update()

    def clearMarkers(self):
        self._markers.clear()
        self.markersChanged.emit([])
        self.update()

    # -----------------------------------------------------------------
    # 音量包络线：坐标映射、插值求值、命中检测与关键帧编辑
    # （Vocal Riding 核心工具：单条曲线完成全片段连续动态塑形，
    #   后端监听 envelopeChanged(points) 按 10**(db/20) 做分段插值增益即可）
    # -----------------------------------------------------------------
    def setEnvelopeEnabled(self, enabled: bool):
        self._env_enabled = enabled
        self._invalidate_cache()

    def setEnvelopeCurveMode(self, mode: str):
        self._env_curve_mode = mode
        self.envelopeChanged.emit(copy.deepcopy(self._env_points))
        self._invalidate_cache()

    def resetEnvelope(self):
        self._env_points.clear()
        self._env_hover_idx = -1
        self._env_hover_seg = -1
        self._env_selected_idx = -1
        self.envelopeChanged.emit([])
        self._invalidate_cache()

    def getEnvelopePoints(self) -> list[dict]:
        return copy.deepcopy(self._env_points) if self._env_enabled else []

    def _env_db_to_y(self, db: float, wave_r: QRectF) -> float:
        """将包络增益 [-36dB, +12dB] 映射到波形画布纵坐标（0dB 位于上方 28% 处，兼顾提升与衰减手感，
        纵向区间避开顶部淡入淡出角标热区与底部分割线）"""
        y_top = wave_r.top() + 14.0
        y_bot = max(y_top + 20.0, wave_r.bottom() - 10.0)
        y_zero = y_top + (y_bot - y_top) * 0.28
        db_c = max(-36.0, min(12.0, db))
        if db_c >= 0.0:
            return y_zero - (db_c / 12.0) * (y_zero - y_top)
        return y_zero + (abs(db_c) / 36.0) * (y_bot - y_zero)

    def _env_y_to_db(self, y: float, wave_r: QRectF, snap_zero: bool = True) -> float:
        y_top = wave_r.top() + 14.0
        y_bot = max(y_top + 20.0, wave_r.bottom() - 10.0)
        y_zero = y_top + (y_bot - y_top) * 0.28
        if y <= y_zero:
            db = ((y_zero - y) / max(1.0, y_zero - y_top)) * 12.0
        else:
            db = -((y - y_zero) / max(1.0, y_bot - y_zero)) * 36.0
        db = max(-36.0, min(12.0, db))
        if snap_zero and abs(db) <= 0.45:
            self._env_snap_zero_flash = True
            return 0.0
        self._env_snap_zero_flash = False
        return round(db, 1)

    def _env_node_slope(self, idx: int) -> float:
        """节点切线斜率 (dB/s)：取相邻两节点割线方向（Catmull-Rom 切线，手柄自然指向节点连线方向），
        端点用单侧差分。相比逐段 Smoothstep（节点处切线恒为水平），曲线起伏过渡更自然"""
        pts = self._env_points
        if len(pts) < 2:
            return 0.0
        if idx == 0:
            dt = pts[1]["t"] - pts[0]["t"]
            return (pts[1]["db"] - pts[0]["db"]) / dt if dt > 1e-6 else 0.0
        if idx >= len(pts) - 1:
            dt = pts[-1]["t"] - pts[-2]["t"]
            return (pts[-1]["db"] - pts[-2]["db"]) / dt if dt > 1e-6 else 0.0
        dt = pts[idx + 1]["t"] - pts[idx - 1]["t"]
        return (pts[idx + 1]["db"] - pts[idx - 1]["db"]) / dt if dt > 1e-6 else 0.0

    def _eval_envelope_db(self, t: float) -> float:
        """任意时间点 t 的包络增益 (dB)；二分查找 + 端点延续（首节点前/末节点后沿用边界增益）。
        smooth 模式为 Catmull-Rom 三次 Hermite 样条（切线方向 = 相邻节点割线方向）"""
        if not self._env_enabled or not self._env_points:
            return 0.0
        pts = self._env_points
        if len(pts) == 1 or t <= pts[0]["t"]:
            return pts[0]["db"]
        if t >= pts[-1]["t"]:
            return pts[-1]["db"]

        ts = [p["t"] for p in pts]
        idx = bisect.bisect_right(ts, t) - 1
        idx = max(0, min(len(pts) - 2, idx))
        p0, p1 = pts[idx], pts[idx + 1]
        span = p1["t"] - p0["t"]
        if span <= 1e-6:
            return p0["db"]
        u = (t - p0["t"]) / span
        if self._env_curve_mode == "smooth":
            # Catmull-Rom -> Hermite 基函数：节点两侧手柄沿切线（割线）方向，而非水平
            m0 = self._env_node_slope(idx)
            m1 = self._env_node_slope(idx + 1)
            u2 = u * u
            u3 = u2 * u
            db = ((2.0 * u3 - 3.0 * u2 + 1.0) * p0["db"]
                  + (u3 - 2.0 * u2 + u) * span * m0
                  + (-2.0 * u3 + 3.0 * u2) * p1["db"]
                  + (u3 - u2) * span * m1)
            return max(-36.0, min(12.0, db))
        return p0["db"] + (p1["db"] - p0["db"]) * u

    def _hit_test_envelope_node(self, pos: QPointF, wave_r: QRectF, radius: float = 7.5) -> int:
        if not self._env_enabled or not self._env_points:
            return -1
        best_i, best_d = -1, radius
        for i, pt in enumerate(self._env_points):
            nx = self._time_to_x(pt["t"], wave_r)
            ny = self._env_db_to_y(pt["db"], wave_r)
            d = math.hypot(pos.x() - nx, pos.y() - ny)
            if d <= best_d:
                best_d = d
                best_i = i
        return best_i

    def _hit_test_envelope_line(self, pos: QPointF, wave_r: QRectF, thresh_y: float = 6.0) -> tuple[bool, int]:
        """检测鼠标是否靠近包络线；返回 (是否命中, 所在节点区间左索引)"""
        if not self._env_enabled or not wave_r.contains(pos):
            return False, -1
        t = self._x_to_time(pos.x(), wave_r)
        line_y = self._env_db_to_y(self._eval_envelope_db(t), wave_r)
        if abs(pos.y() - line_y) > thresh_y:
            return False, -1
        if len(self._env_points) < 2:
            return True, -1
        ts = [p["t"] for p in self._env_points]
        seg_idx = bisect.bisect_right(ts, t) - 1
        if 0 <= seg_idx < len(self._env_points) - 1:
            return True, seg_idx
        return True, -1

    def _insert_envelope_node(self, t: float, db: float) -> int:
        t = max(0.0, min(self._duration, t))
        db = max(-36.0, min(12.0, round(db, 1)))
        self._env_points.append({"t": t, "db": db})
        self._env_points.sort(key=lambda item: item["t"])
        for i, p in enumerate(self._env_points):
            if abs(p["t"] - t) < 1e-7 and abs(p["db"] - db) < 1e-5:
                return i
        return 0

    def _create_range_push_nodes(self, t0: float, t1: float) -> list[int]:
        """存在时间选区时，在选区边界自动生成 4 个包围节点（含 20ms 防爆音斜坡）并整体推拉选区电平"""
        ramp = min(0.025, (t1 - t0) * 0.2)
        b0 = self._eval_envelope_db(t0)
        b1 = self._eval_envelope_db(t1)
        # 清除选区内部原有碎点，保持曲线整洁（列表重建 -> 选中态失效）
        self._env_points = [p for p in self._env_points if not (t0 - 1e-4 <= p["t"] <= t1 + 1e-4)]
        self._env_selected_idx = -1
        self._insert_envelope_node(t0, b0)
        idx_in_l = self._insert_envelope_node(t0 + ramp, b0)
        idx_in_r = self._insert_envelope_node(t1 - ramp, b1)
        self._insert_envelope_node(t1, b1)
        return [idx_in_l, idx_in_r]

    def _set_env_node_db(self, idx: int, db: float):
        if 0 <= idx < len(self._env_points):
            self._env_points[idx]["db"] = db
            self.envelopeChanged.emit(copy.deepcopy(self._env_points))
            self._invalidate_cache()

    def _delete_env_node(self, idx: int):
        if 0 <= idx < len(self._env_points):
            self._env_points.pop(idx)
            self._env_hover_idx = -1
            self._env_selected_idx = -1
            self.envelopeChanged.emit(copy.deepcopy(self._env_points))
            self._invalidate_cache()

    def deleteSelectedEnvelopeNode(self) -> bool:
        """删除当前选中的包络节点（Del / 退格键触发）；无选中节点时返回 False，走原删除逻辑"""
        if not self._env_enabled:
            return False
        idx = self._env_selected_idx
        if 0 <= idx < len(self._env_points):
            self._delete_env_node(idx)
            return True
        return False

    def _apply_local_brush_heal(self, stroke: list[tuple[float, float]]):
        """对语图矩阵执行局部视觉平滑修复，提供即时视觉反馈"""
        if not self._spec_matrix or not self._spec_matrix[0] or not stroke:
            return
        t_bins = len(self._spec_matrix)
        f_bins = len(self._spec_matrix[0])
        f_min, f_max = 20.0, max(1000.0, self._nyquist())
        log_span = math.log10(f_max / f_min)

        for t, freq in stroke:
            tx = max(1, min(t_bins - 2, int((t / self._duration) * t_bins)))
            norm_f = math.log10(max(f_min, min(f_max, freq)) / f_min) / log_span
            fy = max(2, min(f_bins - 3, int(norm_f * f_bins)))
            for dt in (-1, 0, 1):
                for df in (-2, -1, 0, 1, 2):
                    w = 0.45 if (dt == 0 and df == 0) else 0.72
                    self._spec_matrix[tx + dt][fy + df] *= w
        self._spec_values = None
        self._rebuild_spectrogram_image()
        self._invalidate_spectrogram()

    # -----------------------------------------------------------------
    # 语图 QImage 缓存构建（零外部依赖的高速像素缓冲）
    # -----------------------------------------------------------------
    def _rebuild_spectrogram_image(self):
        if not self._spec_matrix or not self._spec_matrix[0]:
            self._spec_image = None
            return
        if self._spec_values is None:
            self._spec_values = np.asarray(self._spec_matrix, dtype=np.float64).T[::-1]
        indices = np.clip((self._spec_values + self._spec_gain_boost / 60.0) * 255.0, 0, 255)
        pixels = np.ascontiguousarray(_SPEC_LUT_RGB[indices.astype(np.uint8)])
        height, width = indices.shape
        img = QImage(pixels.tobytes(), width, height, width * 3, QImage.Format.Format_RGB888)
        self._spec_image = img.convertToFormat(QImage.Format.Format_RGB32)

    # -----------------------------------------------------------------
    # 坐标映射工具
    # -----------------------------------------------------------------
    def _nyquist(self) -> float:
        return self._sample_rate * 0.5

    def _ruler_rect(self) -> QRectF:
        return QRectF(self._ml, 0.0, max(10.0, self.width() - self._ml - self._mr), self._mt)

    def _body_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(10.0, self.width() - self._ml - self._mr),
            max(40.0, self.height() - self._mt - self._mb),
        )

    def _wave_and_spec_rects(self) -> tuple[QRectF, QRectF, float]:
        body = self._body_rect()
        if not self._show_spectral:
            return body, QRectF(), -1.0
        # 强制取整，保证分割条上下两条黑线落在精确物理像素上
        split_y = float(round(body.top() + body.height() * (1.0 - self._spectral_ratio)))
        wave_r = QRectF(body.left(), body.top(), body.width(), max(40.0, split_y - body.top() - 2.0))
        spec_r = QRectF(body.left(), split_y + 2.0, body.width(), max(40.0, body.bottom() - split_y - 2.0))
        return wave_r, spec_r, split_y

    def _time_to_x(self, sec: float, rect: QRectF) -> float:
        span = max(1e-6, self._view_end - self._view_start)
        return rect.left() + ((sec - self._view_start) / span) * rect.width()

    def _x_to_time(self, x: float, rect: QRectF) -> float:
        span = max(1e-6, self._view_end - self._view_start)
        norm = max(0.0, min(1.0, (x - rect.left()) / max(1.0, rect.width())))
        return self._view_start + norm * span

    def _freq_to_spec_y(self, freq: float, spec_r: QRectF) -> float:
        f_min, f_max = 20.0, max(1000.0, self._nyquist())
        f = max(f_min, min(f_max, freq))
        norm = math.log10(f / f_min) / math.log10(f_max / f_min)
        return spec_r.bottom() - norm * spec_r.height()

    def _spec_y_to_freq(self, y: float, spec_r: QRectF) -> float:
        f_min, f_max = 20.0, max(1000.0, self._nyquist())
        norm = max(0.0, min(1.0, (spec_r.bottom() - y) / max(1.0, spec_r.height())))
        return f_min * ((f_max / f_min) ** norm)

    def brush_stroke_payload(self, stroke, radius_px: float) -> dict:
        """§5 画笔：像素域笔迹 → 数据域载荷（提交前转换，缩放/重开可重现）。

        返回 ``{"strokes": [(t 秒, hz), ...], "time_radius_s": …,
        "bandwidth_oct": …}``；频谱面板不可见时按当前几何近似换算。
        """
        import math as _math
        _, spec_r, _ = self._wave_and_spec_rects()
        width = max(1.0, spec_r.width() if not spec_r.isNull()
                    else max(1.0, self.width() - self._ml - self._mr))
        height = max(1.0, spec_r.height() if not spec_r.isNull() else 100.0)
        span = max(1e-6, self._view_end - self._view_start)
        time_radius_s = max(0.002, float(radius_px) / max(1.0, width / span))
        f_min, f_max = 20.0, max(1000.0, self._nyquist())
        decades_per_px = _math.log10(f_max / f_min) / height
        bandwidth_oct = max(0.02, float(radius_px) * decades_per_px
                            * _math.log2(10.0))
        return {
            "strokes": [(float(t), float(f)) for t, f in stroke],
            "time_radius_s": time_radius_s,
            "bandwidth_oct": bandwidth_oct,
        }

    # -----------------------------------------------------------------
    # 鼠标与滚轮交互
    # -----------------------------------------------------------------
    def leaveEvent(self, event):
        self._mouse_pos = None
        self.update()

    def mousePressEvent(self, event):
        if self._drag_mode in {"fade_in", "fade_out", "fade_curve"}:
            return
        pos = event.position()
        ruler = self._ruler_rect()
        body = self._body_rect()
        wave_r, spec_r, split_y = self._wave_and_spec_rects()

        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_vel_sec = 0.0
            self._drag_mode = "pan"
            self._drag_anchor_t = pos.x()
            self._pan_start_v0 = self._view_start
            self._pan_start_v1 = self._view_end
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() == Qt.MouseButton.LeftButton and self.fade_controls_editable:
            self._mouse_pos = pos
            if end := self._fade_tab_at(pos, wave_r):
                self._drag_mode = f"fade_{end}"
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
            if hit := self._fade_curve_point_at(pos, wave_r):
                end, band_height = hit
                midpoint = self._eval_fade_curve(0.5, fade_out=end == "out")
                self._fade_curve_drag = (end, pos.y(), midpoint, band_height)
                self._drag_mode = "fade_curve"
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                return

        # 左侧声道轨头交互：点击 CH1/CH2 静音，右键反相/独奏
        if pos.x() < self._ml and wave_r.top() <= pos.y() <= wave_r.bottom():
            if not self.channel_monitor_enabled:
                return
            ch_count = max(1, len(self._peaks_max))
            ch_h = wave_r.height() / ch_count
            ch = min(ch_count - 1, int((pos.y() - wave_r.top()) / ch_h))
            while len(self._ch_muted) <= ch:
                self._ch_muted.append(False)
                self._ch_phase_inv.append(False)
            if event.button() == Qt.MouseButton.LeftButton:
                self._ch_muted[ch] = not self._ch_muted[ch]
                self.channelStateChanged.emit(ch, self._ch_muted[ch], self._ch_phase_inv[ch])
                self._invalidate_cache()
            elif event.button() == Qt.MouseButton.RightButton:
                self._show_channel_menu(ch, event.globalPosition().toPoint())
            return

        # 右键包络节点/曲线：快捷归零 / 删除 / 整体重置
        if (event.button() == Qt.MouseButton.RightButton and self.project_edits_enabled
                and self._env_enabled and wave_r.contains(pos)):
            hit_node = self._hit_test_envelope_node(pos, wave_r)
            on_line, _ = self._hit_test_envelope_line(pos, wave_r)
            if hit_node >= 0 or (self._tool_mode == "envelope" and on_line):
                menu = QMenu(self)
                menu.setStyleSheet(
                    "QMenu{background:#1B1C20;border:1px solid #383A44;color:#E2E4EC;font-size:11px;padding:4px;}"
                    "QMenu::item{padding:5px 14px;}QMenu::item:selected{background:#2A2E3A;color:#B67CF8;}"
                )
                if hit_node >= 0:
                    pt = self._env_points[hit_node]
                    act_zero = menu.addAction(f"重置节点为 0.0 dB (当前 {pt['db']:+.1f} dB)")
                    act_zero.triggered.connect(lambda: self._set_env_node_db(hit_node, 0.0))
                    act_del = menu.addAction("删除此包络节点 (Alt+点击)")
                    act_del.triggered.connect(lambda: self._delete_env_node(hit_node))
                    menu.addSeparator()
                act_clear = menu.addAction("重置全部音量包络线")
                act_clear.triggered.connect(self.resetEnvelope)
                menu.exec(event.globalPosition().toPoint())
                return

        # 右键标尺：添加或清除标记点
        if (event.button() == Qt.MouseButton.RightButton and self.project_edits_enabled
                and ruler.contains(pos)):
            t = self._x_to_time(pos.x(), ruler)
            menu = QMenu(self)
            menu.setStyleSheet("QMenu{background:#1B1C20;border:1px solid #383A44;color:#E2E4EC;font-size:11px;padding:4px;}QMenu::item{padding:5px 14px;}QMenu::item:selected{background:#2A2E3A;color:#28D8F0;}")
            menu.addAction(f"在此处插入标记点 ({format_timecode(t)})", lambda: self.addMarker(t))
            if self._markers:
                menu.addAction("清除全部标记点", self.clearMarkers)
            menu.exec(event.globalPosition().toPoint())
            return

        if event.button() == Qt.MouseButton.RightButton:
            if self._sel_t1 > self._sel_t0 >= 0.0 and body.contains(pos):
                self._show_selection_context_menu(event.globalPosition().toPoint())
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        # 1. 悬浮增益 HUD
        if self.selection_gain_editable and self._gain_commit_rect.contains(pos):
            t0, t1, _, _ = self.getSelection()
            self.gainCommitRequested.emit(t0, t1, self._preview_gain_db)
            return
        if self.selection_gain_editable and self._gain_hud_rect.contains(pos):
            self._drag_mode = "gain_hud"
            self._drag_start_y = pos.y()
            self._drag_start_gain = self._preview_gain_db
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            return

        # 2. 拖拽分割线
        if self._show_spectral and abs(pos.y() - split_y) <= 4.5 and body.left() <= pos.x() <= body.right():
            self._drag_mode = "splitter"
            self.setCursor(Qt.CursorShape.SplitVCursor)
            return

        # 2.5 抓取标尺中的播放头小三角拖拽定位（±5px 热区，优先于视口平移导航）
        ph_x = self._time_to_x(self._playhead, body)
        if (event.button() == Qt.MouseButton.LeftButton and ruler.contains(pos)
                and abs(pos.x() - ph_x) <= 5.0):
            self._drag_mode = "playhead"
            self._playhead = max(0.0, min(self._duration, self._x_to_time(pos.x(), body)))
            self.playheadMoved.emit(self._playhead)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.update()
            return

        # 3. 顶部时间标尺：点击定位播放头，拖拽实现 Audition 式水平平移 + 垂直缩放
        if ruler.contains(pos):
            self._drag_mode = "ruler_nav"
            self._drag_anchor_t = pos.x()
            self._drag_start_y = pos.y()
            self._pan_start_v0 = self._view_start
            self._pan_start_v1 = self._view_end
            self._ruler_anchor_sec = self._x_to_time(pos.x(), ruler)
            self._ruler_dragged = False
            return

        # 4. 音量包络线交互：三层策略避免与框选手势冲突
        #    A. Alt+点击节点 -> 删除；B. 紧致热区命中节点 -> 选中并拖拽（两种模式均生效）；
        #    C. 包络模式 / Ctrl+点击线段 -> 选区推拉、整段平移或插入新节点
        #    选中节点后按 Del / 退格键可删除（右键节点菜单亦提供删除）
        if self.project_edits_enabled and wave_r.contains(pos) and self._env_enabled:
            hit_node = self._hit_test_envelope_node(pos, wave_r)
            alt_held = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
            ctrl_held = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))

            # A. Alt + 点击已有节点 -> 快捷删除节点
            if hit_node >= 0 and alt_held:
                self._env_points.pop(hit_node)
                self._env_hover_idx = -1
                self._env_selected_idx = -1
                self.envelopeChanged.emit(copy.deepcopy(self._env_points))
                self._invalidate_cache()
                return

            # B. 直接命中已有节点 -> 选中 + 进入拖拽（框选 / 包络两种模式下均可，≤7.5px 热区无误触）
            if hit_node >= 0:
                self._env_selected_idx = hit_node
                self._drag_mode = "env_drag"
                self._env_drag_indices = [hit_node]
                self._env_drag_init_pts = copy.deepcopy(self._env_points)
                self._env_drag_anchor_pos = pos
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                return

            # C. 处于"✎ 包络"专属模式，或按住 Ctrl/Cmd 点击包络线
            if self._tool_mode == "envelope" or ctrl_held:
                t_click = self._x_to_time(pos.x(), wave_r)
                # 若已有时间选区且点击在选区内包络线附近 -> 触发 4 点选区推拉
                if self._sel_t1 > self._sel_t0 >= 0.0 and self._sel_t0 <= t_click <= self._sel_t1:
                    on_line, _ = self._hit_test_envelope_line(pos, wave_r, thresh_y=12.0)
                    if on_line:
                        self._drag_mode = "env_drag"
                        self._env_drag_indices = self._create_range_push_nodes(self._sel_t0, self._sel_t1)
                        self._env_drag_init_pts = copy.deepcopy(self._env_points)
                        self._env_drag_anchor_pos = pos
                        self.setCursor(Qt.CursorShape.SizeVerCursor)
                        self._invalidate_cache()
                        return

                # 命中线段 -> 整体上下平移整段（同时拖动左右两个端点）
                on_line, seg_idx = self._hit_test_envelope_line(pos, wave_r, thresh_y=5.5)
                if on_line and seg_idx >= 0 and not alt_held:
                    self._drag_mode = "env_drag"
                    self._env_drag_indices = [seg_idx, seg_idx + 1]
                    self._env_drag_init_pts = copy.deepcopy(self._env_points)
                    self._env_drag_anchor_pos = pos
                    self.setCursor(Qt.CursorShape.SizeVerCursor)
                    self.update()
                    return

                # 其余位置点击（仅在 envelope 模式下）-> 插入新节点并立刻进入拖拽
                if self._tool_mode == "envelope":
                    snap_z = not alt_held
                    db_click = self._env_y_to_db(pos.y(), wave_r, snap_zero=snap_z)
                    new_idx = self._insert_envelope_node(t_click, db_click)
                    self._env_selected_idx = new_idx
                    self._drag_mode = "env_drag"
                    self._env_drag_indices = [new_idx]
                    self._env_drag_init_pts = copy.deepcopy(self._env_points)
                    self._env_drag_anchor_pos = pos
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                    self._invalidate_cache()
                    return

            # 未命中任何节点 -> 清除节点选中态
            if hit_node < 0:
                self._env_selected_idx = -1
                self.update()

        # 5. 微调已有选区的左右边缘
        if self._sel_t1 > self._sel_t0 >= 0.0 and body.contains(pos):
            sx0 = self._time_to_x(self._sel_t0, body)
            sx1 = self._time_to_x(self._sel_t1, body)
            if abs(pos.x() - sx0) <= 6.0:
                self._drag_mode = "sel_left"
                self._selection_fixed_edge = self._sel_t1
                return
            if abs(pos.x() - sx1) <= 6.0:
                self._drag_mode = "sel_right"
                self._selection_fixed_edge = self._sel_t0
                return

        # 5.5 抓取波形区中的红色播放头拖拽定位（±5px 热区；
        #     选区边缘与包络节点已在上序分支优先命中，避免抢走精细调节手势）
        if (event.button() == Qt.MouseButton.LeftButton and body.contains(pos)
                and abs(pos.x() - self._time_to_x(self._playhead, body)) <= 5.0):
            self._drag_mode = "playhead"
            self._playhead = max(0.0, min(self._duration, self._x_to_time(pos.x(), body)))
            self.playheadMoved.emit(self._playhead)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.update()
            return

        # 6. 在波形区拖拽 -> 建立全频段段落选区
        if wave_r.contains(pos):
            t = self._x_to_time(pos.x(), wave_r)
            self._drag_mode = "select_wave"
            self._drag_anchor_t = t
            self._sel_t0 = t
            self._sel_t1 = t
            self._sel_f_low = 20.0
            self._sel_f_high = self._nyquist()
            self._is_2d_spectral_sel = False
            self._playhead = t
            self.playheadMoved.emit(t)
            self._notify_selection_changed()
            return

        # 7. 在语图区拖拽 -> 修复画笔涂抹 OR 建立 2D 频谱矩形选区
        if self._show_spectral and spec_r.contains(pos):
            t = self._x_to_time(pos.x(), spec_r)
            f = self._spec_y_to_freq(pos.y(), spec_r)
            if self._tool_mode == "brush":
                self._drag_mode = "brush_paint"
                self._brush_stroke = [(t, f)]
                self.update()
                return
            self._drag_mode = "select_spec"
            self._drag_anchor_t = t
            self._drag_anchor_f = f
            self._sel_t0 = t
            self._sel_t1 = t
            self._sel_f_low = f
            self._sel_f_high = f
            self._is_2d_spectral_sel = True
            self._playhead = t
            self.playheadMoved.emit(t)
            self._notify_selection_changed()

    def mouseMoveEvent(self, event):
        pos = event.position()
        self._mouse_pos = pos
        ruler = self._ruler_rect()
        body = self._body_rect()
        wave_r, spec_r, split_y = self._wave_and_spec_rects()

        if not self._drag_mode:
            if self.fade_controls_editable and self._fade_tab_at(pos, wave_r):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                self.update()
                return
            if self.fade_controls_editable and self._fade_curve_point_at(pos, wave_r):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                self.update()
                return
            # 包络悬停反馈：节点开手光标 / 包络模式下的线段高亮与插入光标
            if wave_r.contains(pos) and self._env_enabled:
                h_idx = self._hit_test_envelope_node(pos, wave_r)
                on_line, s_idx = self._hit_test_envelope_line(pos, wave_r)
                if h_idx != self._env_hover_idx or s_idx != self._env_hover_seg:
                    self._env_hover_idx = h_idx
                    self._env_hover_seg = s_idx
                    self.update()
                if h_idx >= 0:
                    self.setCursor(Qt.CursorShape.OpenHandCursor)
                    self.update()
                    return
                if self._tool_mode == "envelope":
                    self.setCursor(Qt.CursorShape.SizeVerCursor if (on_line and s_idx >= 0) else Qt.CursorShape.CrossCursor)
                    self.update()
                    return
            if pos.x() < self._ml and wave_r.top() <= pos.y() <= wave_r.bottom():
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            elif ruler.contains(pos):
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            elif self._gain_hud_rect.isValid() and self._gain_hud_rect.contains(pos):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif self._show_spectral and abs(pos.y() - split_y) <= 4.5 and body.left() <= pos.x() <= body.right():
                self.setCursor(Qt.CursorShape.SplitVCursor)
            elif self._sel_t1 > self._sel_t0 >= 0.0 and body.contains(pos):
                sx0 = self._time_to_x(self._sel_t0, body)
                sx1 = self._time_to_x(self._sel_t1, body)
                if abs(pos.x() - sx0) <= 6.0 or abs(pos.x() - sx1) <= 6.0:
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                else:
                    self.setCursor(Qt.CursorShape.IBeamCursor)
            elif body.contains(pos):
                self.setCursor(Qt.CursorShape.BlankCursor if (self._tool_mode == "brush" and spec_r.contains(pos)) else Qt.CursorShape.IBeamCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
            return

        if self._drag_mode == "playhead":
            # 拖拽播放头连续定位（不吸附，避免吸到自身位置造成卡滞）
            t = max(0.0, min(self._duration, self._x_to_time(pos.x(), body)))
            self._playhead = t
            self.playheadMoved.emit(t)
            self.update()
            return

        if self._drag_mode == "ruler_nav":
            dx = pos.x() - self._drag_anchor_t
            dy = pos.y() - self._drag_start_y
            if abs(dx) > 3.0 or abs(dy) > 3.0:
                self._ruler_dragged = True
            if self._ruler_dragged:
                init_span = self._pan_start_v1 - self._pan_start_v0
                zoom_scale = max(0.02, min(20.0, 1.0 + dy * 0.012))
                new_span = max(0.02, min(self._duration, init_span * zoom_scale))
                ratio = (self._ruler_anchor_sec - self._pan_start_v0) / max(1e-6, init_span)
                pan_dt = -(dx / max(1.0, ruler.width())) * new_span
                ns = max(0.0, min(self._duration - new_span, self._ruler_anchor_sec - ratio * new_span + pan_dt))
                self._navigate_viewport(ns, ns + new_span)
            return

        if self._drag_mode == "brush_paint" and self._show_spectral:
            t = self._x_to_time(pos.x(), spec_r)
            f = self._spec_y_to_freq(pos.y(), spec_r)
            # 最小时间间距过滤，避免鼠标停顿堆积重复点
            if not self._brush_stroke or abs(t - self._brush_stroke[-1][0]) > 0.004:
                self._brush_stroke.append((t, f))
            self.update()
            return

        if self._drag_mode == "pan":
            dx = pos.x() - self._drag_anchor_t
            span = self._pan_start_v1 - self._pan_start_v0
            dt = -(dx / max(1.0, body.width())) * span
            ns = max(0.0, min(self._duration - span, self._pan_start_v0 + dt))
            # 记录帧间位移，供松开中键后的惯性滑行使用
            self._pan_vel_sec = ns - self._view_start
            self._navigate_viewport(ns, ns + span)

        elif self._drag_mode == "splitter":
            norm_y = (pos.y() - body.top()) / max(1.0, body.height())
            self._spectral_ratio = max(0.20, min(0.75, 1.0 - norm_y))
            self._invalidate_cache()

        elif self._drag_mode == "gain_hud":
            dy = self._drag_start_y - pos.y()
            new_g = max(-36.0, min(24.0, round(self._drag_start_gain + dy * 0.15, 1)))
            if new_g != self._preview_gain_db:
                self.setPreviewGain(new_g)
                self.previewGainChanged.emit(new_g)

        elif self._drag_mode == "fade_in":
            t = max(0.0, min(self._duration - self._fade_out_sec, self._x_to_time(pos.x(), wave_r)))
            fade = round(t, 3)
            if fade != self._fade_in_sec:
                self._fade_in_sec = fade
                self.fadeChanged.emit(self._fade_in_sec, self._fade_out_sec)
                self._refresh_processing_overlay()

        elif self._drag_mode == "fade_out":
            t = max(self._fade_in_sec, min(self._duration, self._x_to_time(pos.x(), wave_r)))
            fade = round(self._duration - t, 3)
            if fade != self._fade_out_sec:
                self._fade_out_sec = fade
                self.fadeChanged.emit(self._fade_in_sec, self._fade_out_sec)
                self._refresh_processing_overlay()

        elif self._drag_mode == "fade_curve" and self._fade_curve_drag is not None:
            _, initial_y, midpoint, height = self._fade_curve_drag
            offset = (pos.y() - initial_y) / height
            curve = fade_curve_from_midpoint(midpoint - offset) if abs(pos.y() - initial_y) >= 1.0 else None
            if curve != self._fade_curve_preview:
                self._fade_curve_preview = curve
                self._refresh_processing_overlay()

        elif self._drag_mode in ("sel_left", "sel_right"):
            t = self._apply_magnetic_snap(self._x_to_time(pos.x(), body), body)
            self._sel_t0, self._sel_t1 = sorted((t, self._selection_fixed_edge))
            self._notify_selection_changed()

        elif self._drag_mode == "select_wave":
            t = self._x_to_time(pos.x(), body)
            self._sel_t0 = min(self._drag_anchor_t, t)
            self._sel_t1 = max(self._drag_anchor_t, t)
            self._notify_selection_changed()

        elif self._drag_mode == "select_spec" and self._show_spectral:
            t = self._x_to_time(pos.x(), spec_r)
            f = self._spec_y_to_freq(pos.y(), spec_r)
            self._sel_t0 = min(self._drag_anchor_t, t)
            self._sel_t1 = max(self._drag_anchor_t, t)
            self._sel_f_low = min(self._drag_anchor_f, f)
            self._sel_f_high = max(self._drag_anchor_f, f)
            self._notify_selection_changed()

        elif self._drag_mode == "env_drag" and self._env_drag_indices:
            # Shift 锁单轴，Alt 0.1dB 精调；单点支持时间+电平双维调节，多点纯垂直平移
            dy = pos.y() - self._env_drag_anchor_pos.y()
            dx = pos.x() - self._env_drag_anchor_pos.x()
            mods = event.modifiers()
            lock_time = bool(mods & Qt.KeyboardModifier.ShiftModifier) and abs(dy) >= abs(dx)
            lock_db = bool(mods & Qt.KeyboardModifier.ShiftModifier) and abs(dx) > abs(dy)
            fine_mode = bool(mods & Qt.KeyboardModifier.AltModifier)

            if len(self._env_drag_indices) == 1:
                idx = self._env_drag_indices[0]
                orig = self._env_drag_init_pts[idx]
                min_t = (self._env_points[idx - 1]["t"] + 0.002) if idx > 0 else 0.0
                max_t = (self._env_points[idx + 1]["t"] - 0.002) if idx < len(self._env_points) - 1 else self._duration
                new_t = orig["t"] if lock_time else max(min_t, min(max_t, self._x_to_time(pos.x(), wave_r)))

                if lock_db:
                    new_db = orig["db"]
                elif fine_mode:
                    new_db = max(-36.0, min(12.0, round(orig["db"] - dy * 0.05, 1)))
                else:
                    new_db = self._env_y_to_db(pos.y(), wave_r, snap_zero=True)

                self._env_points[idx]["t"] = new_t
                self._env_points[idx]["db"] = new_db
            else:
                db_delta = -(dy / max(1.0, wave_r.height() * 0.65)) * (12.0 if fine_mode else 36.0)
                self._env_snap_zero_flash = False
                for idx in self._env_drag_indices:
                    orig_db = self._env_drag_init_pts[idx]["db"]
                    cand_db = max(-36.0, min(12.0, orig_db + db_delta))
                    if not fine_mode and abs(cand_db) <= 0.45:
                        cand_db = 0.0
                    if abs(cand_db) <= 1e-9:
                        self._env_snap_zero_flash = True
                    self._env_points[idx]["db"] = round(cand_db, 1)

            self._refresh_processing_overlay()
            return

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        button = Qt.MouseButton.MiddleButton if self._drag_mode == "pan" else Qt.MouseButton.LeftButton
        if event.button() != button:
            return
        if self._drag_mode == "fade_curve" and self._fade_curve_drag is not None:
            end, _, _, _ = self._fade_curve_drag
            curve = self._fade_curve_preview
            self.cancel_fade_drag()
            if curve is not None:
                if end == "in":
                    self.setFadeCurves(curve, self._fade_out_curve_type)
                else:
                    self.setFadeCurves(self._fade_curve_type, curve)
                self.fadeCurveCommitted.emit(end, curve)
            return
        selecting = self.is_selecting()
        seeking = self._drag_mode in ("playhead", "select_wave", "select_spec")
        if self._drag_mode == "ruler_nav" and not self._ruler_dragged:
            self._playhead = self._ruler_anchor_sec
            self.playheadMoved.emit(self._playhead)
            self.update()
        elif self._drag_mode == "brush_paint" and self._brush_stroke:
            stroke = list(self._brush_stroke)
            self._apply_local_brush_heal(stroke)
            self.spectralBrushApplied.emit(stroke, self._brush_radius_px)
            self._brush_stroke.clear()
            self._invalidate_cache()
        elif self._drag_mode == "pan":
            # 松开中键平移：启动惯性滑行物理（速度在 mouseMoveEvent 中逐帧记录）
            if abs(self._pan_vel_sec) > 1e-4 and not self._physics_timer.isActive():
                self._physics_timer.start()
        elif self._drag_mode == "env_drag":
            # 拖拽结束：清空拖拽态并提交最终包络关键帧
            self._env_drag_indices.clear()
            self._env_drag_init_pts.clear()
            self._env_snap_zero_flash = False
            self.envelopeChanged.emit(copy.deepcopy(self._env_points))
            self._refresh_processing_overlay()
        elif self._drag_mode in ("select_wave", "select_spec"):
            # 若只是极短点击而非拖拽选区，则清空选区仅保留播放头定位
            if abs(self._sel_t1 - self._sel_t0) < (self._view_end - self._view_start) * 0.003:
                self.clearSelection()
        self._drag_mode = ""
        if selecting:
            self.selectionDragFinished.emit()
        if seeking:
            self.playheadDragFinished.emit()

    def _show_channel_menu(self, ch: int, global_pos):
        menu = QMenu(self)
        menu.setStyleSheet("QMenu{background:#1B1C20;border:1px solid #383A44;color:#E2E4EC;font-size:11px;padding:4px;}QMenu::item{padding:5px 14px;}QMenu::item:selected{background:#2A2E3A;color:#28D8F0;}")
        inv_act = menu.addAction(f"{'✓ ' if self._ch_phase_inv[ch] else ''}相位反转 (Ø Invert Phase)")
        inv_act.triggered.connect(lambda: self._toggle_ch_phase(ch))
        inv_act.setEnabled(self.project_edits_enabled)
        if len(self._peaks_max) > 1:
            solo_act = menu.addAction(f"仅独奏 CH{ch+1} (静音其他声道)")
            solo_act.triggered.connect(lambda: self._solo_channel(ch))
        menu.exec(global_pos)

    def _toggle_ch_phase(self, ch: int):
        self._ch_phase_inv[ch] = not self._ch_phase_inv[ch]
        self.channelStateChanged.emit(ch, self._ch_muted[ch], self._ch_phase_inv[ch])
        self.update()

    def _solo_channel(self, ch: int):
        for i in range(len(self._ch_muted)):
            self._ch_muted[i] = (i != ch)
            self.channelStateChanged.emit(i, self._ch_muted[i], self._ch_phase_inv[i])
        self.update()

    def mouseDoubleClickEvent(self, event):
        # 双击包络节点 -> 电平归零 0.0 dB（优先于全选手势，不误触）
        if event.button() == Qt.MouseButton.LeftButton and self.project_edits_enabled and self._env_enabled:
            wave_r, _, _ = self._wave_and_spec_rects()
            hit_node = self._hit_test_envelope_node(event.position(), wave_r)
            if hit_node >= 0:
                self._env_points[hit_node]["db"] = 0.0
                self.envelopeChanged.emit(copy.deepcopy(self._env_points))
                self._invalidate_cache()
                return
        if event.button() == Qt.MouseButton.LeftButton and self._body_rect().contains(event.position()):
            self.selectAll()

    def wheelEvent(self, event):
        pos = event.position()
        body = self._body_rect()
        pixels = event.pixelDelta().y()
        delta = max(-12.0, min(12.0, pixels / 120.0 if pixels else event.angleDelta().y() / 120.0))
        if delta == 0.0:
            event.ignore()
            return

        # Alt + 滚轮 -> 垂直放大波形显示倍率（观察微弱底噪/呼吸声，不影响实际音量）
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            factor = 1.25 ** delta
            zoom = max(1.0, min(16.0, self._amp_zoom * factor))
            if zoom != self._amp_zoom:
                self._amp_zoom = zoom
                self._invalidate_cache()
            event.accept()
            return

        # 鼠标悬停在悬浮增益胶囊上滚动滚轮 -> 精细调节 dB
        if self.selection_gain_editable and self._gain_hud_rect.contains(pos):
            step = 0.2 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.5
            self.setPreviewGain(self._preview_gain_db + delta * step)
            self.previewGainChanged.emit(self._preview_gain_db)
            event.accept()
            return

        # Shift + 滚轮 -> 横向平移时间轴
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            span = self._view_end - self._view_start
            shift = -delta * span * 0.15
            ns = max(0.0, min(self._duration - span, self._view_start + shift))
            self._navigate_viewport(ns, ns + span)
            event.accept()
            return

        # 普通滚轮 -> 以鼠标光标所在时间点为锚点缩放时间轴
        anchor_t = self._x_to_time(pos.x(), body) if body.contains(pos) else (self._view_start + self._view_end) * 0.5
        zoom_factor = 0.80 ** delta
        old_span = self._view_end - self._view_start
        new_span = max(0.02, min(self._duration, old_span * zoom_factor))
        ratio = (anchor_t - self._view_start) / max(1e-6, old_span)
        ns = max(0.0, min(self._duration - new_span, anchor_t - ratio * new_span))
        self._navigate_viewport(ns, ns + new_span)
        event.accept()

    def _navigate_viewport(self, start: float, end: float) -> None:
        previous = (self._view_start, self._view_end)
        self.setViewport(start, end)
        if previous != (self._view_start, self._view_end):
            self.viewportChanged.emit(self._view_start, self._view_end)

    def _show_selection_context_menu(self, global_pos):
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
            QMenu::item:selected { background-color: #2A2E3A; color: #28D8F0; }
        """)
        t0, t1, fl, fh = self.getSelection()
        if self._is_2d_spectral_sel:
            act_heal = menu.addAction(f"频谱修复 / 消除选区杂音 ({format_freq_short(fl)} - {format_freq_short(fh)})")
            act_heal.triggered.connect(lambda: self.spectralActionRequested.emit("heal", t0, t1, fl, fh))
            act_heal.setEnabled(self.project_edits_enabled)
            act_att = menu.addAction("衰减选区频段 (-12 dB)")
            act_att.triggered.connect(lambda: self.spectralActionRequested.emit("attenuate", t0, t1, fl, fh))
            act_att.setEnabled(self.project_edits_enabled)
            act_solo = menu.addAction("仅试听选区频段 (Bandpass Preview)")
            act_solo.triggered.connect(lambda: self.spectralActionRequested.emit("solo_band", t0, t1, fl, fh))
            act_solo.setEnabled(self.channel_monitor_enabled)
            menu.addSeparator()

        act_sil = menu.addAction("静音当前段落 (Silence)")
        act_sil.triggered.connect(lambda: self.spectralActionRequested.emit("silence", t0, t1, fl, fh))
        act_sil.setEnabled(self.project_edits_enabled)
        act_trim = menu.addAction("裁剪保留当前段落 (Trim)")
        act_trim.triggered.connect(lambda: self.spectralActionRequested.emit("trim", t0, t1, fl, fh))
        act_trim.setEnabled(self.project_edits_enabled)
        act_del = menu.addAction("删除当前段落 (Ripple Delete)")
        act_del.triggered.connect(lambda: self.spectralActionRequested.emit("delete", t0, t1, fl, fh))
        act_del.setEnabled(self.project_edits_enabled)
        menu.exec(global_pos)

    # -----------------------------------------------------------------
    # 绘制逻辑：时间标尺、波形包络、频谱瀑布图、选区、悬浮增益 HUD
    # -----------------------------------------------------------------
    # -----------------------------------------------------------------
    # 惯性滑行动力学 + 磁吸物理引力井（60Hz 物理帧率定时器）
    # -----------------------------------------------------------------
    def _init_kinetic_physics(self):
        """在 WaveformSpectralCanvas.__init__ 末尾调用此方法"""
        self._pan_vel_sec = 0.0
        self._last_drag_ts = 0.0
        self._snap_flash_sec = -1.0
        self._snap_flash_alpha = 0

        self._physics_timer = QTimer(self)
        self._physics_timer.setInterval(16)  # 60Hz 丝滑物理帧率
        self._physics_timer.timeout.connect(self._tick_kinetic_physics)

    def _tick_kinetic_physics(self):
        need_repaint = False

        # 1. 惯性滑行衰减（指数阻尼）
        if abs(self._pan_vel_sec) > 1e-4:
            span = self._view_end - self._view_start
            ns = max(0.0, min(self._duration - span, self._view_start + self._pan_vel_sec))
            if abs(ns - self._view_start) < 1e-6:
                self._pan_vel_sec = 0.0
            else:
                self._pan_vel_sec *= 0.86  # 物理阻尼系数
                self._navigate_viewport(ns, ns + span)
        else:
            self._pan_vel_sec = 0.0

        # 2. 磁吸激光脉冲渐隐
        if self._snap_flash_alpha > 0:
            self._snap_flash_alpha = max(0, self._snap_flash_alpha - 24)
            need_repaint = True

        if need_repaint:
            self.update()
        elif self._pan_vel_sec == 0.0 and self._snap_flash_alpha == 0:
            self._physics_timer.stop()

    def _apply_magnetic_snap(self, raw_sec: float, rect: QRectF) -> float:
        """选区边缘拖拽时的 6px 磁吸引力井（吸附至播放头或用户标记点）"""
        if not getattr(self, "_magnetic_snap", True):
            return raw_sec
        span = max(1e-6, self._view_end - self._view_start)
        thresh_sec = (6.0 / max(1.0, rect.width())) * span
        candidates = [self._playhead] + [m["time"] for m in self._markers]

        best_t = raw_sec
        best_dist = thresh_sec
        snapped = False
        for c in candidates:
            d = abs(raw_sec - c)
            if d < best_dist:
                best_dist = d
                best_t = c
                snapped = True

        if snapped and abs(best_t - self._snap_flash_sec) > 1e-5:
            self._snap_flash_sec = best_t
            self._snap_flash_alpha = 230
            if not self._physics_timer.isActive():
                self._physics_timer.start()
        return best_t

    # -----------------------------------------------------------------
    # 多级 LOD 采样点重建曲线 + Catmull-Rom 三次样条插值
    # -----------------------------------------------------------------
    @staticmethod
    def _build_catmull_rom_path(pts: list[QPointF]) -> QPainterPath:
        """三次 Catmull-Rom 样条插值，将离散采样点平滑重建为模拟连续波形"""
        path = QPainterPath()
        n = len(pts)
        if n == 0:
            return path
        path.moveTo(pts[0])
        if n == 1:
            return path
        for i in range(n - 1):
            p0 = pts[max(0, i - 1)]
            p1 = pts[i]
            p2 = pts[i + 1]
            p3 = pts[min(n - 1, i + 2)]
            c1 = QPointF(p1.x() + (p2.x() - p0.x()) / 6.0, p1.y() + (p2.y() - p0.y()) / 6.0)
            c2 = QPointF(p2.x() - (p3.x() - p1.x()) / 6.0, p2.y() - (p3.y() - p1.y()) / 6.0)
            path.cubicTo(c1, c2, p2)
        return path

    def _draw_channel_waveform_lod(
        self,
        p: QPainter,
        wave_r: QRectF,
        cy: float,
        half_h: float,
        p_max: np.ndarray,
        p_min: np.ndarray,
        p_rms: np.ndarray,
        is_muted: bool,
        is_inv: bool,
        ch: int = -1,
    ) -> list[float]:
        total_bins = len(p_max)
        if total_bins < 2:
            return []

        # 按声道解析生效增益：单声道编辑目标下，非目标声道保持原样
        gain_db = self._channel_gain_db(ch)
        gain_linear = 10.0 ** (gain_db / 20.0)
        has_sel = (self._sel_t1 > self._sel_t0 >= 0.0)
        has_env = not self.waveform_includes_processing and self._env_enabled and bool(self._env_points)
        fade_in = 0.0 if self.waveform_includes_processing else self._fade_in_sec
        fade_out = 0.0 if self.waveform_includes_processing else self._fade_out_sec
        show_ghost = (not is_muted) and (
            abs(gain_db) >= 0.05 or fade_in > 0.001 or fade_out > 0.001 or has_env
        )

        # 计算当前视口下每个采样 bin 占据的像素宽度，决定 LOD 层级
        vis_span = max(1e-6, self._view_end - self._view_start)
        vis_bins = total_bins * (vis_span / self._duration)
        px_per_bin = wave_r.width() / max(1.0, vis_bins)

        clip_xs: list[float] = []

        # =============================================================
        # LOD 级别 B：微观采样点级视图 (px_per_bin >= 2.2)
        # Catmull-Rom 三次样条模拟重建曲线 + 精密采样点圆环
        # =============================================================
        if px_per_bin >= 2.2:
            i_start = max(0, int((self._view_start / self._duration) * total_bins) - 2)
            i_end = min(total_bins - 1, int((self._view_end / self._duration) * total_bins) + 2)

            sample_pts: list[QPointF] = []
            ghost_pts: list[QPointF] = []

            for i in range(i_start, i_end + 1):
                t = (i / total_bins) * self._duration
                x = self._time_to_x(t, wave_r)

                # 取包络主极性点（若接入真实 PCM 采样数组，直接用 pcm[i] 效果更佳）
                raw_v = p_max[i] if abs(p_max[i]) >= abs(p_min[i]) else p_min[i]
                if is_inv:
                    raw_v = -raw_v

                in_target = (not has_sel) or (self._sel_t0 <= t <= self._sel_t1)
                # 包络增益实时联动：波形振幅随包络插值同步缩放（LOD-B 微观视图）
                env_linear = 10.0 ** (self._eval_envelope_db(t) / 20.0) if has_env else 1.0
                clip_scale = (gain_linear if in_target else 1.0) * env_linear
                if fade_in > 0.0 and t < fade_in:
                    clip_scale *= self._eval_fade_curve(max(0.0, min(1.0, t / fade_in)))
                if fade_out > 0.0 and t > self._duration - fade_out:
                    clip_scale *= self._eval_fade_curve(max(0.0, min(1.0, (self._duration - t) / fade_out)), fade_out=True)

                if not is_muted and abs(raw_v * clip_scale) > 0.995:
                    clip_xs.append(x)

                v_disp = max(-1.0, min(1.0, raw_v * clip_scale * self._amp_zoom))
                sample_pts.append(QPointF(x, cy - v_disp * half_h))

                if show_ghost:
                    v_ghost = max(-1.0, min(1.0, raw_v * self._amp_zoom))
                    ghost_pts.append(QPointF(x, cy - v_ghost * half_h))

            if show_ghost and len(ghost_pts) >= 2:
                p.setPen(QPen(QColor(255, 255, 255, 55), 0.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPolyline(QPolygonF(ghost_pts))

            if len(sample_pts) >= 2:
                show_points = px_per_bin >= 5.5
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(110, 118, 136, 110) if is_muted else QColor("#74F5B4"), 0.0))
                if show_points:
                    p.drawPath(self._build_catmull_rom_path(sample_pts))
                else:
                    p.drawPolyline(QPolygonF(sample_pts))

                if show_points and not is_muted:
                    p.setPen(QPen(QColor("#0B0E14"), 1.0))
                    p.setBrush(QColor(ACCENT_CYAN))
                    for pt in sample_pts:
                        if wave_r.left() <= pt.x() <= wave_r.right():
                            p.drawEllipse(pt, 2.5, 2.5)
            return clip_xs

        # =============================================================
        # LOD A：逐像素列保留极值，直接生成波形包络的栅格。
        # =============================================================
        cols = int(wave_r.width())
        if cols < 2:
            return []
        columns = np.arange(cols + 1, dtype=np.float64)
        times = self._view_start + columns * (vis_span / wave_r.width())
        indices = np.clip((times * (total_bins / self._duration)).astype(np.intp), 0, total_bins - 1)
        hi = self._wave_column_extrema(p_max, indices, np.maximum)
        lo = self._wave_column_extrema(p_min, indices, np.minimum)
        rms = self._wave_column_extrema(p_rms, indices, np.maximum)
        if is_inv:
            hi, lo = -lo, -hi
        times = times[:-1]
        xs = wave_r.left() + columns[:-1]
        scale = np.ones(cols)
        if gain_linear != 1.0:
            if has_sel:
                scale[(times >= self._sel_t0) & (times <= self._sel_t1)] = gain_linear
            else:
                scale.fill(gain_linear)
        if has_env:
            scale *= [10.0 ** (self._eval_envelope_db(t) / 20.0) for t in times]
        if fade_in > 0.0:
            affected = times < fade_in
            scale[affected] *= [self._eval_fade_curve(t / fade_in) for t in times[affected]]
        if fade_out > 0.0:
            affected = times > self._duration - fade_out
            scale[affected] *= [self._eval_fade_curve((self._duration - t) / fade_out, fade_out=True)
                                for t in times[affected]]
        if not is_muted:
            clip_xs = xs[(hi * scale > 0.995) | (lo * scale < -0.995)].tolist()
        scale *= self._amp_zoom
        area = QRectF(wave_r.left(), cy - half_h, wave_r.width(), 2.0 * half_h)
        self._draw_wave_columns(p, area, hi, lo, rms, scale, is_muted, show_ghost)

        return clip_xs

    @staticmethod
    def _wave_column_extrema(samples: np.ndarray, edges: np.ndarray, reducer: np.ufunc) -> np.ndarray:
        # Adjacent columns share the bin crossing their pixel boundary so peaks
        # remain visible while panning. reduceat also handles repeated bin edges.
        return reducer(reducer.reduceat(samples, edges)[:-1], samples[edges[1:]])

    def _draw_wave_columns(self, p: QPainter, area: QRectF,
                           hi: np.ndarray, lo: np.ndarray, rms: np.ndarray,
                           scale: np.ndarray, muted: bool, ghost: bool) -> None:
        cols = len(hi)
        rows = max(2, round(area.height() * self.devicePixelRatioF()))
        stride = (cols + 3) & ~3
        center = (rows - 1) * 0.5
        indices = np.zeros((rows, stride), dtype=np.uint8)
        pixels = indices[:, :cols]
        row = np.arange(rows)[:, None]
        col = np.arange(cols)

        if ghost:
            top = np.rint(center * (1.0 - np.clip(hi * self._amp_zoom, -1.0, 1.0))).astype(np.intp)
            bottom = np.rint(center * (1.0 - np.clip(lo * self._amp_zoom, -1.0, 1.0))).astype(np.intp)
            np.copyto(pixels, _WAVE_GHOST, where=(row >= top) & (row <= bottom))
            pixels[top, col] = pixels[bottom, col] = _WAVE_GHOST_EDGE

        top = np.rint(center * (1.0 - np.clip(hi * scale, -1.0, 1.0))).astype(np.intp)
        bottom = np.rint(center * (1.0 - np.clip(lo * scale, -1.0, 1.0))).astype(np.intp)
        inside_peak = (row >= top) & (row <= bottom)
        if muted:
            np.copyto(pixels, _WAVE_MUTED, where=inside_peak)
            pixels[top, col] = pixels[bottom, col] = _WAVE_MUTED_EDGE
        else:
            shade = 1 + (abs(row - center) * ((_WAVE_SHADES - 1) / center)).astype(np.uint8)
            level = center * np.clip(rms * scale, 0.0, 1.0)
            inside_rms = abs(row - center) <= level
            if ghost:
                np.copyto(pixels, shade, where=inside_peak)
                np.copyto(pixels, _WAVE_RMS, where=inside_rms & ~inside_peak)
                np.copyto(pixels, shade + _WAVE_SHADES, where=inside_rms & inside_peak)
            else:
                pixels[:] = np.where(
                    inside_peak, shade + inside_rms * np.uint8(_WAVE_SHADES),
                    inside_rms * np.uint8(_WAVE_RMS))
            pixels[top, col] = pixels[bottom, col] = _WAVE_EDGE

        # QImage borrows this byte buffer until the immediate draw finishes.
        buffer = indices.tobytes()
        image = QImage(buffer, cols, rows, stride, QImage.Format.Format_Indexed8)
        image.setColorTable(_waveform_colors())
        p.drawImage(area, image)

    # -----------------------------------------------------------------
    # 渲染管线：视网膜级离屏双缓冲金字塔
    # 静态层（标尺/波形/幽灵轮廓/语图/分割条）按 devicePixelRatio 缓存进高分
    # QPixmap；播放头、十字准星、谐波梳、磁吸激光等轻量矢量层逐帧叠画，
    # 播放/悬停时 CPU 占用大幅下降（4K/5K 全屏播放不掉帧的关键）。
    # -----------------------------------------------------------------
    def _invalidate_cache(self):
        """凡是改变波形、语图、视口缩放、增益预览、淡入淡出的操作，均调用此方法"""
        self._cache_dirty = True
        self.update()

    def resizeEvent(self, event):
        self._cache_dirty = True
        super().resizeEvent(event)

    def _rebuild_static_cache(self):
        dpr = max(1.0, self.devicePixelRatioF())
        w_px = max(1, int(round(self.width() * dpr)))
        h_px = max(1, int(round(self.height() * dpr)))
        pix = QPixmap(w_px, h_px)
        pix.setDevicePixelRatio(dpr)
        pix.fill(QColor("#1E1E22"))

        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        ruler = self._ruler_rect()
        body = self._body_rect()
        wave_r, spec_r, split_y = self._wave_and_spec_rects()

        # 左侧刻度槽底色
        p.fillRect(QRectF(0, 0, self._ml, self.height()), QColor("#27272B"))

        # 1. 顶部时间标尺底色（分割线统一在所有背景填充之后绘制，防止被 wave_r 覆盖一半）
        self._draw_time_ruler(p, ruler)

        # 2. 波形面板底色与波形
        p.fillRect(wave_r, QColor("#1D1D20"))
        self._draw_waveform_pane(p, wave_r)

        # 3. 频谱瀑布图底色与语图
        if self._show_spectral and spec_r.isValid():
            self._draw_spectrogram_pane(p, spec_r)

        # 4. 统一在最顶层绘制所有正交 1px 黑线（对齐到 .5 物理像素中心，锐利无重影且完全连通）
        gutter_x = math.floor(self._ml) - 0.5
        ruler_y = math.floor(ruler.bottom()) - 0.5

        p.setPen(QPen(QColor("#141416"), 1.0))
        # 标尺底部分隔线（贯穿左侧槽与主标尺）
        p.drawLine(QPointF(0.0, ruler_y), QPointF(self.width(), ruler_y))
        # 左侧刻度槽右边界竖线（从顶部贯穿到底部）
        p.drawLine(QPointF(gutter_x, 0.0), QPointF(gutter_x, self.height()))

        # 波形与语图之间的 4px 分割条
        if self._show_spectral and spec_r.isValid():
            top_y = split_y - 2.5
            bot_y = split_y + 2.5
            p.fillRect(QRectF(0.0, split_y - 2.0, self.width(), 4.0), QColor("#27272B"))
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.drawLine(QPointF(0.0, top_y), QPointF(self.width(), top_y))
            p.drawLine(QPointF(0.0, bot_y), QPointF(self.width(), bot_y))

            # 分割条拖拽把手微刻点（保留可拖拽暗示，色值统一为中性灰）
            cx = body.center().x()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#52525B"))
            for dx in (-8.0, 0.0, 8.0):
                p.drawEllipse(QPointF(cx + dx, split_y), 1.1, 1.1)

        p.end()
        self._static_cache = pix
        self._cache_dirty = False
        self._spectrum_dirty = False

    def paintEvent(self, event):
        cache = getattr(self, "_static_cache", None)
        if (getattr(self, "_cache_dirty", True) or cache is None
                or cache.devicePixelRatio() != self.devicePixelRatioF()):
            self._rebuild_static_cache()
        elif self._spectrum_dirty:
            _, spec_r, split_y = self._wave_and_spec_rects()
            with QPainter(cache) as background:
                background.setRenderHint(QPainter.RenderHint.Antialiasing)
                self._paint_spectrogram_content(background, spec_r)
                background.setPen(QPen(QColor("#141416"), 1.0))
                background.drawLine(QPointF(spec_r.left(), split_y + 2.5),
                                    QPointF(spec_r.right(), split_y + 2.5))
            self._spectrum_dirty = False

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. 零开销瞬时贴上 Retina 离屏静态底图
        p.drawPixmap(0, 0, self._static_cache)

        ruler = self._ruler_rect()
        body = self._body_rect()
        wave_r, spec_r, _ = self._wave_and_spec_rects()

        # 2. 动态交互层：选区遮罩、悬浮 HUD、磁吸激光闪光、标记点、播放头、谐波游标
        if self.waveform_includes_processing:
            self._draw_fade_overlays(p, wave_r)
        self._draw_selection_overlay(p, body, wave_r, spec_r)
        self._draw_dynamic_overlays(p, ruler, body, wave_r, spec_r)
        self._draw_fade_controls(p, wave_r)

    def _draw_dynamic_overlays(self, p: QPainter, ruler: QRectF, body: QRectF, wave_r: QRectF, spec_r: QRectF):
        # 1. 磁吸对齐瞬间激光脉冲反馈（配合惯性/磁吸物理引擎）
        if getattr(self, "_snap_flash_alpha", 0) > 4:
            sx = self._time_to_x(self._snap_flash_sec, body)
            if body.left() <= sx <= body.right():
                alpha = self._snap_flash_alpha
                p.setPen(QPen(QColor(40, 216, 240, alpha // 3), 6.0))
                p.drawLine(QPointF(sx, ruler.bottom()), QPointF(sx, body.bottom()))
                p.setPen(QPen(QColor(130, 245, 255, alpha), 1.5))
                p.drawLine(QPointF(sx, ruler.bottom()), QPointF(sx, body.bottom()))

        # 2. 用户标记点 (Markers)
        for m in self._markers:
            mt = m["time"]
            if self._view_start <= mt <= self._view_end:
                mx = self._time_to_x(mt, body)
                p.setPen(QPen(QColor(240, 168, 72, 135), 1.0, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(mx, ruler.bottom()), QPointF(mx, body.bottom()))
                flag = QRectF(mx, ruler.top() + 2, 26, 13)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(ACCENT_AMBER))
                p.drawRoundedRect(flag, 2.0, 2.0)
                p.setPen(QColor("#101216"))
                p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
                p.drawText(flag, Qt.AlignmentFlag.AlignCenter, m["label"])

        # 3. 高对比度播放头（深色外描边 + 红色高亮芯，亮色语图上依然锐利）
        if self._view_start <= self._playhead <= self._view_end:
            px = self._time_to_x(self._playhead, body)
            p.setPen(QPen(QColor(6, 9, 14, 215), 2.8))
            p.drawLine(QPointF(px, ruler.top()), QPointF(px, body.bottom()))
            p.setPen(QPen(QColor("#FF5252"), 1.3))
            p.drawLine(QPointF(px, ruler.top()), QPointF(px, body.bottom()))

            head_poly = QPolygonF([
                QPointF(px - 5.5, ruler.top() + 0.5),
                QPointF(px + 5.5, ruler.top() + 0.5),
                QPointF(px + 5.5, ruler.top() + 7.5),
                QPointF(px, ruler.top() + 13.0),
                QPointF(px - 5.5, ruler.top() + 7.5),
            ])
            p.setPen(QPen(QColor("#160606"), 1.0))
            p.setBrush(QColor("#FF5252"))
            p.drawPolygon(head_poly)
            # 游标顶部高光横线
            p.setPen(QPen(QColor("#FFB8B8"), 1.0))
            p.drawLine(QPointF(px - 4.0, ruler.top() + 1.5), QPointF(px + 4.0, ruler.top() + 1.5))

        # 4. 鼠标十字准星 + iZotope RX 级“谐波泛音梳状游标”
        if self._mouse_pos is not None and body.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 26), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(mx, body.top()), QPointF(mx, body.bottom()))
            p.drawLine(QPointF(body.left(), my), QPointF(body.right(), my))

            cur_t = self._x_to_time(mx, body)
            if self._show_spectral and spec_r.contains(self._mouse_pos):
                f0 = self._spec_y_to_freq(my, spec_r)
                note = freq_to_note_name(f0)
                hud_txt = f"{format_timecode(cur_t)}  •  {format_freq_short(f0)} ({note})"

                # 向上投射 2f0 ~ 8f0 泛音梳状标尺
                p.save()
                p.setClipRect(spec_r)
                p.setFont(QFont(MONO_FONT_FAM, 7))
                last_fy = my
                for n in range(2, 9):
                    fn = f0 * n
                    if fn > self._nyquist():
                        break
                    fy = self._freq_to_spec_y(fn, spec_r)
                    if (last_fy - fy) < 9.0:
                        continue
                    last_fy = fy
                    alpha = max(28, 145 - n * 14)
                    p.setPen(QPen(QColor(40, 216, 240, alpha), 1.0, Qt.PenStyle.DashLine))
                    p.drawLine(QPointF(spec_r.left(), fy), QPointF(spec_r.right(), fy))

                    if n <= 5:
                        tag_x = min(spec_r.right() - 56.0, mx + 10.0)
                        p.setPen(QColor(130, 235, 250, min(220, alpha + 50)))
                        p.drawText(
                            QRectF(tag_x, fy - 11.0, 54.0, 11.0),
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                            f"{n}f·{format_freq_short(fn)}",
                        )
                p.restore()

                # 左侧频率轴跟踪标牌
                f_pill = QRectF(3.0, max(spec_r.top(), min(spec_r.bottom() - 15, my - 7.5)), self._ml - 6.0, 15.0)
                p.setPen(QPen(QColor(ACCENT_CYAN), 1.0))
                p.setBrush(QColor("#101D24"))
                p.drawRoundedRect(f_pill, 2.5, 2.5)
                p.setPen(QColor(ACCENT_CYAN))
                p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
                p.drawText(f_pill, Qt.AlignmentFlag.AlignCenter, format_freq_short(f0))
            else:
                hud_txt = f"{format_timecode(cur_t)}"

            # 时间码 HUD 固定在波形右下角（远离顶部淡入淡出角标与增益 HUD）；
            # 若与选区时长胶囊（底缘居中于选区中点）重叠则自动左移让位
            hud_x = body.right() - 182.0
            if self._sel_t1 > self._sel_t0 >= 0.0:
                cap_mid = (self._time_to_x(self._sel_t0, body) + self._time_to_x(self._sel_t1, body)) * 0.5
                # 仅当两个底缘胶囊的水平区间真正相交时才左移让位
                if hud_x < cap_mid + 36.0 and hud_x + 174.0 > cap_mid - 36.0:
                    hud_x = max(body.left() + 4.0, cap_mid - 36.0 - 6.0 - 174.0)
            hud_rect = QRectF(hud_x, wave_r.bottom() - 24.0, 174, 19)
            p.setPen(QPen(QColor("#323846"), 1.0))
            p.setBrush(QColor(14, 16, 22, 225))
            p.drawRoundedRect(hud_rect, 4.0, 4.0)
            p.setPen(QColor("#C8D0DF"))
            p.setFont(QFont(MONO_FONT_FAM, 8))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_txt)

        # 5. 修复画笔实时涂抹轨迹 + 光标圈
        # （置于动态层：静态缓存重建成本高，悬停移动不应触发整体重绘）
        if self._show_spectral and spec_r.isValid():
            p.save()
            p.setClipRect(spec_r)
            if self._brush_stroke:
                b_path = QPainterPath()
                for idx_pt, (bt, bf) in enumerate(self._brush_stroke):
                    bx = self._time_to_x(bt, spec_r)
                    by = self._freq_to_spec_y(bf, spec_r)
                    if idx_pt == 0:
                        b_path.moveTo(bx, by)
                    else:
                        b_path.lineTo(bx, by)
                p.setPen(QPen(QColor(240, 168, 72, 95), self._brush_radius_px * 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.drawPath(b_path)
                p.setPen(QPen(QColor(ACCENT_AMBER), 1.6, Qt.PenStyle.DashLine))
                p.drawPath(b_path)
            if self._tool_mode == "brush" and self._mouse_pos is not None and spec_r.contains(self._mouse_pos):
                p.setPen(QPen(QColor(ACCENT_AMBER), 1.4, Qt.PenStyle.DashLine))
                p.setBrush(QColor(240, 168, 72, 42))
                p.drawEllipse(self._mouse_pos, self._brush_radius_px, self._brush_radius_px)
            p.restore()

        # 6. 音量包络线（动态层：拖拽节点高亮与悬浮 dB 胶囊零延迟跟随）
        self._draw_volume_envelope(p, wave_r)

        # 7. 时间伸缩目标落点预览（拧动速率旋钮时实时投射目标结束虚线与差值胶囊）
        self._draw_stretch_preview_overlay(p, body, wave_r)

    def _draw_volume_envelope(self, p: QPainter, wave_r: QRectF):
        """音量包络线矢量渲染：0dB 基准线 / 双层发光曲线 / LED 关键帧节点 / 悬浮读数胶囊"""
        if not self._env_enabled:
            return
        has_pts = bool(self._env_points)
        in_env_mode = (self._tool_mode == "envelope")
        if not has_pts and not in_env_mode:
            return

        p.save()
        p.setClipRect(wave_r)

        y_zero = self._env_db_to_y(0.0, wave_r)

        # 1. 0.0 dB 基准参考线（进入包络模式或发生 0dB 磁吸时点亮）
        if in_env_mode or self._env_drag_indices:
            zero_alpha = 180 if self._env_snap_zero_flash else 65
            p.setPen(QPen(QColor(182, 124, 248, zero_alpha), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(wave_r.left(), y_zero), QPointF(wave_r.right(), y_zero))

            badge_r = QRectF(wave_r.left() + 6.0, y_zero - 8.0, 34.0, 15.0)
            p.setPen(QPen(QColor(182, 124, 248, 120), 1.0))
            p.setBrush(QColor(18, 14, 28, 215))
            p.drawRoundedRect(badge_r, 3.0, 3.0)
            p.setPen(QColor(ACCENT_VIOLET))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(badge_r, Qt.AlignmentFlag.AlignCenter, "0 dB")

        # 2. 构建当前视口内的包络曲线路径与相对 0dB 的面积遮罩
        curve_path = QPainterPath()
        if not has_pts or self._env_curve_mode == "linear":
            prev_t, prev_db = self._view_start, self._eval_envelope_db(self._view_start)
            curve_path.moveTo(wave_r.left(), self._env_db_to_y(prev_db, wave_r))
            knots = [(p["t"], p["db"]) for p in self._env_points
                     if self._view_start < p["t"] < self._view_end]
            knots.append((self._view_end, self._eval_envelope_db(self._view_end)))
            for time, db in knots:
                if prev_db * db < 0.0:
                    # The display scale has different slopes above and below 0 dB.
                    cross = prev_t + (time - prev_t) * prev_db / (prev_db - db)
                    curve_path.lineTo(self._time_to_x(cross, wave_r), y_zero)
                curve_path.lineTo(self._time_to_x(time, wave_r), self._env_db_to_y(db, wave_r))
                prev_t, prev_db = time, db
        else:
            samples = max(48, int(wave_r.width() // 3))
            for i in range(samples + 1):
                x = wave_r.left() + i / samples * wave_r.width()
                y = self._env_db_to_y(self._eval_envelope_db(self._x_to_time(x, wave_r)), wave_r)
                if i == 0:
                    curve_path.moveTo(x, y)
                else:
                    curve_path.lineTo(x, y)

        if has_pts:
            area_path = QPainterPath(curve_path)
            area_path.lineTo(wave_r.right(), y_zero)
            area_path.lineTo(wave_r.left(), y_zero)
            area_path.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(182, 124, 248, 22 if in_env_mode else 12))
            p.drawPath(area_path)

        # 悬停整段线段时的高亮预览
        if self._env_hover_seg >= 0 and self._env_hover_seg < len(self._env_points) - 1:
            p0, p1 = self._env_points[self._env_hover_seg], self._env_points[self._env_hover_seg + 1]
            seg_path = QPainterPath()
            for s_i in range(25):
                su = s_i / 24.0
                st = p0["t"] + su * (p1["t"] - p0["t"])
                sx = self._time_to_x(st, wave_r)
                sy = self._env_db_to_y(self._eval_envelope_db(st), wave_r)
                if s_i == 0:
                    seg_path.moveTo(sx, sy)
                else:
                    seg_path.lineTo(sx, sy)
            p.setPen(QPen(QColor(228, 200, 255, 220), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPath(seg_path)

        # 包络主曲线（包络模式下提高亮度）
        line_alpha = 235 if (in_env_mode or self._env_drag_indices) else 155
        p.setBrush(Qt.BrushStyle.NoBrush)

        core_col = QColor(182, 124, 248, line_alpha)
        pen_style = Qt.PenStyle.SolidLine if has_pts else Qt.PenStyle.DashLine
        p.setPen(QPen(core_col, 1.75 if in_env_mode else 1.35, pen_style, Qt.PenCapStyle.RoundCap))
        p.drawPath(curve_path)

        # 3. 绘制双环硬件 LED 关键帧节点
        active_hud_pt: dict | None = None
        active_hud_xy = QPointF()

        for i, pt in enumerate(self._env_points):
            nx = self._time_to_x(pt["t"], wave_r)
            if nx < wave_r.left() - 10.0 or nx > wave_r.right() + 10.0:
                continue
            ny = self._env_db_to_y(pt["db"], wave_r)
            is_dragged = i in self._env_drag_indices
            is_hovered = (i == self._env_hover_idx)
            is_selected = (i == self._env_selected_idx)
            is_active = (is_dragged or is_hovered or is_selected)

            if is_dragged or is_hovered:
                active_hud_pt = pt
                active_hud_xy = QPointF(nx, ny)
                halo = QRadialGradient(nx, ny, 10.0)
                halo.setColorAt(0.0, QColor(182, 124, 248, 120))
                halo.setColorAt(1.0, QColor(182, 124, 248, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(halo)
                p.drawEllipse(QPointF(nx, ny), 10.0, 10.0)
            elif is_selected:
                # 选中态：柔和光晕 + 白色外环（区别于悬停，持续显示便于定位后按 Del 删除）
                active_hud_pt = pt
                active_hud_xy = QPointF(nx, ny)
                halo = QRadialGradient(nx, ny, 9.0)
                halo.setColorAt(0.0, QColor(182, 124, 248, 75))
                halo.setColorAt(1.0, QColor(182, 124, 248, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(halo)
                p.drawEllipse(QPointF(nx, ny), 9.0, 9.0)
                p.setPen(QPen(QColor("#F3E8FF"), 1.3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(nx, ny), 7.2, 7.2)

            r_outer = 4.8 if (is_active or in_env_mode) else 3.6
            p.setPen(QPen(QColor("#F3E8FF") if is_active else QColor(ACCENT_VIOLET), 1.6))
            p.setBrush(QColor("#101219"))
            p.drawEllipse(QPointF(nx, ny), r_outer, r_outer)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FFFFFF") if is_active else QColor(ACCENT_VIOLET))
            p.drawEllipse(QPointF(nx, ny), 1.5, 1.5)

        # 4. 悬停或拖拽节点时，在节点旁浮现精致参数读数胶囊
        if active_hud_pt is not None:
            db_val = active_hud_pt["db"]
            db_str = "0.0 dB" if abs(db_val) < 0.05 else f"{db_val:+.1f} dB"
            pill_txt = f"{db_str} · {format_timecode(active_hud_pt['t'])}"
            pw, ph = 114.0, 20.0
            px = max(wave_r.left() + 6.0, min(wave_r.right() - pw - 6.0, active_hud_xy.x() - pw * 0.5))
            py = active_hud_xy.y() - ph - 10.0
            if py < wave_r.top() + 4.0:
                py = active_hud_xy.y() + 11.0

            pill_r = QRectF(px, py, pw, ph)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 160))
            p.drawRoundedRect(pill_r.translated(0, 1.5), 5.0, 5.0)

            p.setPen(QPen(QColor(ACCENT_VIOLET), 1.1))
            p.setBrush(QColor(19, 16, 28, 240))
            p.drawRoundedRect(pill_r, 5.0, 5.0)
            p.setPen(QColor("#F4ECFF"))
            p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
            p.drawText(pill_r, Qt.AlignmentFlag.AlignCenter, pill_txt)

        p.restore()

    def _draw_stretch_preview_overlay(self, p: QPainter, body: QRectF, wave_r: QRectF):
        """时间伸缩实时落点预览：目标结束虚线 + 差值光带 + 时长差值胶囊（应用前对照画面切点）"""
        if abs(self._stretch_preview_ratio - 100.0) < 0.05:
            return
        t0, t1, _, _ = self.getSelection()
        if t1 <= t0 or t0 < 0.0:
            return
        ratio = self._stretch_preview_ratio / 100.0
        target_t1 = t0 + (t1 - t0) / ratio
        delta_sec = target_t1 - t1

        x_orig = self._time_to_x(t1, body)
        x_targ = self._time_to_x(target_t1, body)

        p.save()
        p.setClipRect(body)

        # 1. 原结束点与预计目标结束点之间的差值区间光带（缩短=青色 / 拉长=紫色）
        x_left = min(x_orig, x_targ)
        x_right = max(x_orig, x_targ)
        if x_right - x_left >= 2.0:
            band_col = QColor(40, 216, 240, 28) if delta_sec < 0 else QColor(182, 124, 248, 28)
            p.fillRect(QRectF(x_left, wave_r.top(), x_right - x_left, wave_r.height()), band_col)

        # 2. 目标结束落点竖向虚线 + 悬浮目标落点胶囊
        if body.left() <= x_targ <= body.right():
            p.setPen(QPen(QColor(ACCENT_CYAN), 1.5, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x_targ, wave_r.top()), QPointF(x_targ, body.bottom()))

            tag_txt = f"⇢ {format_timecode(target_t1)} ({delta_sec:+.2f}s)"
            pw, ph = 148.0, 20.0
            px = max(body.left() + 6.0, min(body.right() - pw - 6.0, x_targ - pw * 0.5))
            py = wave_r.bottom() - ph - 26.0
            pill = QRectF(px, py, pw, ph)

            p.setPen(QPen(QColor(ACCENT_CYAN), 1.1))
            p.setBrush(QColor(10, 22, 30, 235))
            p.drawRoundedRect(pill, 4.0, 4.0)
            p.setPen(QColor(ACCENT_CYAN))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, tag_txt)

        p.restore()

    def _draw_time_ruler(self, p: QPainter, ruler: QRectF):
        p.fillRect(ruler, QColor("#222226"))
        # 注：底部分隔线已移至 _rebuild_static_cache 统一绘制（.5 像素对齐）

        span = max(0.01, self._view_end - self._view_start)
        raw_step = span / max(2.0, ruler.width() / 95.0)
        nice_steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0]
        step = next((s for s in nice_steps if s >= raw_step), 60.0)
        sub_step = step / 4.0

        first_tick = math.floor(self._view_start / step) * step
        p.setFont(QFont("Menlo", 8))
        t = first_tick
        while t <= self._view_end + step:
            x = math.floor(self._time_to_x(t, ruler)) + 0.5
            if ruler.left() <= x <= ruler.right():
                p.setPen(QPen(QColor("#545C6E"), 1.0))
                p.drawLine(QPointF(x, ruler.bottom() - 7.0), QPointF(x, ruler.bottom() - 1.0))
                p.setPen(QColor("#8E96A8"))
                p.drawText(
                    QRectF(x + 4.0, ruler.top() + 2.0, 74.0, 15.0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    format_timecode(t),
                )
            for s_i in range(1, 4):
                sx = math.floor(self._time_to_x(t + s_i * sub_step, ruler)) + 0.5
                if ruler.left() <= sx <= ruler.right():
                    p.setPen(QPen(QColor("#2E3340"), 1.0))
                    p.drawLine(QPointF(sx, ruler.bottom() - 3.5), QPointF(sx, ruler.bottom() - 1.0))
            t += step

    def _draw_waveform_pane(self, p: QPainter, wave_r: QRectF):
        p.save()
        p.setClipRect(QRectF(0, wave_r.top(), self.width(), wave_r.height()))

        ch_count = len(self._wave_channels)
        ch_h = wave_r.height() / max(1, ch_count)

        for ch, (p_max, p_min, p_rms) in enumerate(self._wave_channels):
            top = wave_r.top() + ch * ch_h
            cy = top + ch_h * 0.5
            half_h = ch_h * 0.45
            is_muted = ch < len(self._ch_muted) and self._ch_muted[ch]
            is_inv = ch < len(self._ch_phase_inv) and self._ch_phase_inv[ch]

            # 左侧精密 dB 刻度线与带状态灯的 CH 开关徽章
            p.setPen(QColor("#788094"))
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(QRectF(2, top + 2, self._ml - 11, 12), Qt.AlignmentFlag.AlignRight, "0dB")
            p.drawText(QRectF(2, top + ch_h - 14, self._ml - 11, 12), Qt.AlignmentFlag.AlignRight, "-∞")

            # 右边缘对齐 -6dB 虚线的短刻度针
            p.setPen(QPen(QColor("#3A4152"), 1.0))
            p.drawLine(QPointF(self._ml - 6.0, cy - half_h * 0.5), QPointF(self._ml - 1.0, cy - half_h * 0.5))
            p.drawLine(QPointF(self._ml - 6.0, cy + half_h * 0.5), QPointF(self._ml - 1.0, cy + half_h * 0.5))

            ch_pill = QRectF(6, cy - 10, self._ml - 12, 20)
            pill_border = QColor("#323746") if is_muted else (QColor(ACCENT_AMBER) if is_inv else QColor("#267A8E"))
            pill_bg = QColor("#171920") if is_muted else QColor("#10222A")
            p.setPen(QPen(pill_border, 1.0))
            p.setBrush(pill_bg)
            p.drawRoundedRect(ch_pill, 4.0, 4.0)

            # 徽章左侧微型 LED 状态点（绿=正常 / 琥珀=反相 / 灰=静音）
            led_col = QColor("#4A5060") if is_muted else (QColor(ACCENT_AMBER) if is_inv else QColor(ACCENT_GREEN))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(led_col)
            p.drawEllipse(QPointF(ch_pill.left() + 6.0, ch_pill.center().y()), 2.0, 2.0)

            p.setPen(QColor("#687082") if is_muted else QColor("#E4E9F5"))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(
                QRectF(ch_pill.left() + 8.0, ch_pill.top(), ch_pill.width() - 9.0, ch_pill.height()),
                Qt.AlignmentFlag.AlignCenter,
                f"CH{ch+1}{'Ø' if is_inv else ''}",
            )

            if ch > 0:
                p.setPen(QPen(QColor("#232732"), 1.0))
                p.drawLine(QPointF(0, top), QPointF(wave_r.right(), top))

            # 中心 0 电平线 & -6dB 参考虚线
            p.setPen(QPen(QColor("#2D3342"), 1.0))
            p.drawLine(QPointF(wave_r.left(), cy), QPointF(wave_r.right(), cy))
            p.setPen(QPen(QColor("#1F2430"), 1.0, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(wave_r.left(), cy - half_h * 0.5), QPointF(wave_r.right(), cy - half_h * 0.5))
            p.drawLine(QPointF(wave_r.left(), cy + half_h * 0.5), QPointF(wave_r.right(), cy + half_h * 0.5))

            # 多级 LOD 波形渲染：逐列峰值包络 <-> 微观 Catmull-Rom 采样点重建曲线，
            # 调节增益/淡变时自动叠加 Before/After 幽灵参考轮廓
            clip_xs = self._draw_channel_waveform_lod(
                p, wave_r, cy, half_h, p_max, p_min, p_rms, is_muted,
                is_inv and not self.waveform_includes_processing, ch
            )

            # 单声道编辑目标可视化：目标声道左缘琥珀标记条，非目标声道轻微压暗
            if self._edit_channel >= 0:
                if ch == self._edit_channel:
                    p.fillRect(QRectF(wave_r.left(), top, 3.0, ch_h), QColor(ACCENT_AMBER))
                else:
                    p.fillRect(QRectF(wave_r.left(), top, wave_r.width(), ch_h), QColor(6, 8, 12, 110))

            # 穿透波形的微弱 0dB 中心基准线（绘制在填充之上，提升对称读数精度）
            if not is_muted:
                p.setPen(QPen(QColor(255, 255, 255, 24), 1.0))
                p.drawLine(QPointF(wave_r.left(), cy), QPointF(wave_r.right(), cy))

            if is_muted:
                p.setPen(QColor("#788092"))
                p.setFont(QFont("Helvetica Neue", 8, QFont.Weight.Bold))
                p.drawText(QRectF(wave_r.left() + 10, top + 6, 80, 16), Qt.AlignmentFlag.AlignLeft, "MUTED")

            # 相邻的削波柱合为一段，避免为贴近的一组峰值重复描边。
            if clip_xs:
                start = end = clip_xs[0]
                warning = QColor(ACCENT_RED)
                for cx in clip_xs[1:]:
                    if cx - end > 1.0:
                        p.fillRect(QRectF(start, top + 2, end - start + 1, 5), warning)
                        start = cx
                    end = cx
                p.fillRect(QRectF(start, top + 2, end - start + 1, 5), warning)

        if not self.waveform_includes_processing:
            self._draw_fade_overlays(p, wave_r)
        p.restore()

    def _fade_ranges(self) -> tuple[tuple[str, float, float], ...]:
        return (("in", 0.0, self._fade_in_sec),
                ("out", self._duration, self._duration - self._fade_out_sec))

    def _fade_bands(self, wave_r: QRectF) -> tuple[QRectF, ...]:
        count = max(1, len(self._peaks_max))
        height = wave_r.height() / count
        return tuple(QRectF(wave_r.left(), wave_r.top() + channel * height + 1.0,
                            wave_r.width(), max(1.0, height - 2.0))
                     for channel in range(count))

    def _fade_tabs(self, wave_r: QRectF) -> tuple[tuple[str, float], ...]:
        inset = FADE_HANDLE_SIZE / 2.0
        tabs = []
        for end, _, time in self._fade_ranges():
            x = self._time_to_x(time, wave_r)
            if wave_r.left() <= x <= wave_r.right():
                tabs.append((end, max(wave_r.left() + inset, min(wave_r.right() - inset, x))))
        return tuple(tabs)

    def _fade_tab_at(self, pos: QPointF, wave_r: QRectF) -> str | None:
        if not wave_r.contains(pos) or pos.y() > wave_r.top() + FADE_HANDLE_HIT_HEIGHT:
            return None
        candidates = [(abs(pos.x() - x), end != ("in" if pos.x() < x else "out"), end)
                      for end, x in self._fade_tabs(wave_r)
                      if abs(pos.x() - x) <= FADE_HANDLE_HIT_RADIUS]
        return min(candidates)[2] if candidates else None

    def _fade_curve_points(self, wave_r: QRectF) -> tuple[tuple[str, QPointF, float], ...]:
        points = []
        for end, edge, time in self._fade_ranges():
            if edge == time or abs(self._time_to_x(time, wave_r) - self._time_to_x(edge, wave_r)) < 24.0:
                continue
            x = self._time_to_x((edge + time) / 2.0, wave_r)
            if not wave_r.left() + 3.0 <= x <= wave_r.right() - 3.0:
                continue
            midpoint = self._eval_fade_curve(0.5, fade_out=end == "out")
            for band in self._fade_bands(wave_r):
                if band.height() >= 18.0:
                    y = band.bottom() - band.height() * midpoint
                    points.append((end, QPointF(x, y), band.height()))
        return tuple(points)

    def _fade_curve_point_at(self, pos: QPointF, wave_r: QRectF) -> tuple[str, float] | None:
        if not wave_r.contains(pos):
            return None
        for end, point, height in self._fade_curve_points(wave_r):
            if abs(point.x() - pos.x()) <= 5.5 and abs(point.y() - pos.y()) <= 5.5:
                return end, height
        return None

    def _draw_fade_controls(self, painter: QPainter, wave_r: QRectF) -> None:
        dragging_fade = self._drag_mode in {"fade_in", "fade_out", "fade_curve"}
        if not self.fade_controls_editable or not (
            dragging_fade or (self._mouse_pos is not None and wave_r.contains(self._mouse_pos))
        ):
            return
        hovered = self._fade_tab_at(self._mouse_pos, wave_r) if self._mouse_pos is not None else None
        painter.save()
        painter.setClipRect(wave_r, Qt.ClipOperation.IntersectClip)
        for end, x in self._fade_tabs(wave_r):
            paint_fade_tab(painter, x - FADE_HANDLE_SIZE / 2.0, wave_r.top() + 0.5,
                           hover=end == hovered or self._drag_mode == f"fade_{end}")
        for _, point, _ in self._fade_curve_points(wave_r):
            paint_fade_curve_point(painter, point, QColor("#D8D9DE"))
        painter.restore()

    def _eval_fade_curve(self, u: float, *, fade_out: bool = False) -> float:
        curve = self._fade_out_curve_type if fade_out else self._fade_curve_type
        if (self._fade_curve_drag is not None and self._fade_curve_preview is not None
                and self._fade_curve_drag[0] == ("out" if fade_out else "in")):
            curve = self._fade_curve_preview
        return _display_fade_factor(curve, u)

    def _draw_fade_overlays(self, p: QPainter, wave_r: QRectF) -> None:
        bands = self._fade_bands(wave_r)
        for end, edge, time in self._fade_ranges():
            if edge == time:
                continue
            x = self._time_to_x(edge, wave_r)
            span = self._time_to_x(time, wave_r) - x
            for band in bands:
                paint_fade_ramp(p, band, x, span,
                                lambda progress: self._eval_fade_curve(progress, fade_out=end == "out"),
                                outline=QColor(181, 188, 203, 155))

        if self._amp_zoom > 1.01:
            boost_db = 20.0 * math.log10(self._amp_zoom)
            badge = QRectF(wave_r.left() + 8, wave_r.top() + 8, 92, 18)
            p.setPen(QPen(QColor(ACCENT_GREEN), 1.0))
            p.setBrush(QColor(14, 24, 21, 220))
            p.drawRoundedRect(badge, 3.5, 3.5)
            p.setPen(QColor(ACCENT_GREEN))
            p.setFont(QFont("Helvetica Neue", 7, QFont.Weight.Bold))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, f"视图放大 +{boost_db:.1f}dB")

    def _draw_spectrogram_pane(self, p: QPainter, spec_r: QRectF):
        p.save()
        p.setFont(QFont("Menlo", 7))
        for f_tick in (60, 250, 1000, 4000, 12000, 20000):
            if f_tick <= self._nyquist():
                fy = self._freq_to_spec_y(f_tick, spec_r)
                p.setPen(QColor("#737A8C"))
                lbl = f"{f_tick // 1000}k" if f_tick >= 1000 else str(f_tick)
                p.drawText(QRectF(4, fy - 7, self._ml - 10, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        p.restore()
        self._paint_spectrogram_content(p, spec_r)

    def _paint_spectrogram_content(self, p: QPainter, spec_r: QRectF) -> None:
        p.save()
        p.setClipRect(spec_r, Qt.ClipOperation.IntersectClip)
        p.fillRect(spec_r, QColor("#141417"))
        if self._spec_image is not None and not self._spec_image.isNull():
            img_w = self._spec_image.width()
            img_h = self._spec_image.height()
            sx0 = (self._view_start / self._duration) * img_w
            sx1 = (self._view_end / self._duration) * img_w
            width = min(float(img_w), max(1.0, sx1 - sx0))
            src_rect = QRectF(min(sx0, img_w - width), 0.0, width, float(img_h))
            p.drawImage(spec_r, self._spec_image, src_rect)

        p.setPen(QPen(QColor(255, 255, 255, 18), 1.0, Qt.PenStyle.DotLine))
        for f_tick in (60, 250, 1000, 4000, 12000):
            if f_tick <= self._nyquist():
                fy = self._freq_to_spec_y(f_tick, spec_r)
                p.drawLine(QPointF(spec_r.left(), fy), QPointF(spec_r.right(), fy))

        # 达芬奇风格：语图面板边界由上方 1px 黑线分割条定义，去掉旧版描边

        # 右侧悬浮 dBFS 色阶标尺（增加深色磨砂胶囊衬底，防止亮色语图遮挡文字）
        bar_w, bar_h = 7.0, min(106.0, spec_r.height() - 20.0)
        if bar_h > 30.0:
            bx = spec_r.right() - bar_w - 10.0
            by = spec_r.top() + 10.0
            backdrop = QRectF(bx - 34.0, by - 5.0, bar_w + 39.0, bar_h + 10.0)
            p.setPen(QPen(QColor("#2C3240"), 1.0))
            p.setBrush(QColor(11, 13, 18, 210))
            p.drawRoundedRect(backdrop, 4.0, 4.0)

            scale_rect = QRectF(bx, by, bar_w, bar_h)
            lut_grad = QLinearGradient(scale_rect.bottomLeft(), scale_rect.topLeft())
            for stop_i in (0, 56, 115, 168, 214, 242, 255):
                r, g, b = SPEC_LUT[stop_i]
                lut_grad.setColorAt(stop_i / 255.0, QColor(r, g, b, 240))

            p.setPen(QPen(QColor("#383E4E"), 1.0))
            p.setBrush(lut_grad)
            p.drawRoundedRect(scale_rect, 1.5, 1.5)

            p.setFont(QFont("Menlo", 7))
            p.setPen(QColor("#C6CDD9"))
            p.drawText(QRectF(bx - 32, by - 3, 28, 12), Qt.AlignmentFlag.AlignRight, "0dB")
            p.drawText(QRectF(bx - 32, by + bar_h * 0.5 - 6, 28, 12), Qt.AlignmentFlag.AlignRight, "-40")
            p.drawText(QRectF(bx - 32, by + bar_h - 9, 28, 12), Qt.AlignmentFlag.AlignRight, "-80")

        p.restore()

    def _draw_selection_overlay(self, p: QPainter, body: QRectF, wave_r: QRectF, spec_r: QRectF):
        self._gain_hud_rect = QRectF()
        self._gain_commit_rect = QRectF()
        has_sel = (self._sel_t1 > self._sel_t0 >= 0.0)
        if not has_sel:
            return

        sx0 = max(body.left(), self._time_to_x(self._sel_t0, body))
        sx1 = min(body.right(), self._time_to_x(self._sel_t1, body))
        if sx1 <= sx0:
            return

        p.save()
        p.setClipRect(body)

        # 1. 非选区微暗化遮罩，强化当前编辑区域的视觉聚焦（Spotlight）
        dim_col = QColor(6, 8, 12, 72)
        if sx0 > body.left():
            p.fillRect(QRectF(body.left(), wave_r.top(), sx0 - body.left(), wave_r.height()), dim_col)
        if sx1 < body.right():
            p.fillRect(QRectF(sx1, wave_r.top(), body.right() - sx1, wave_r.height()), dim_col)

        # 2. 波形区选区垂直光幕渐变高亮
        w_sel = QRectF(sx0, wave_r.top(), sx1 - sx0, wave_r.height())
        sel_grad = QLinearGradient(w_sel.topLeft(), w_sel.bottomLeft())
        sel_grad.setColorAt(0.0, QColor(40, 216, 240, 42))
        sel_grad.setColorAt(0.5, QColor(40, 216, 240, 16))
        sel_grad.setColorAt(1.0, QColor(40, 216, 240, 42))
        p.fillRect(w_sel, sel_grad)

        # 选区边界发光竖线
        p.setPen(QPen(QColor(40, 216, 240, 55), 3.5))
        p.drawLine(QPointF(sx0, wave_r.top()), QPointF(sx0, wave_r.bottom()))
        p.drawLine(QPointF(sx1, wave_r.top()), QPointF(sx1, wave_r.bottom()))
        p.setPen(QPen(QColor(ACCENT_CYAN), 1.3))
        p.drawLine(QPointF(sx0, wave_r.top()), QPointF(sx0, wave_r.bottom()))
        p.drawLine(QPointF(sx1, wave_r.top()), QPointF(sx1, wave_r.bottom()))

        # 左右边缘微抓手胶囊
        if (sx1 - sx0) >= 14.0:
            handle_h = min(24.0, wave_r.height() * 0.22)
            hy = wave_r.center().y() - handle_h * 0.5
            p.setPen(QPen(QColor("#0B0E14"), 1.0))
            p.setBrush(QColor(ACCENT_CYAN))
            p.drawRoundedRect(QRectF(sx0 - 2.2, hy, 4.4, handle_h), 2.2, 2.2)
            p.drawRoundedRect(QRectF(sx1 - 2.2, hy, 4.4, handle_h), 2.2, 2.2)

        # 3. 语图区选区高亮（支持 2D 频率框选）
        if self._show_spectral and spec_r.isValid():
            if self._is_2d_spectral_sel:
                fy_top = self._freq_to_spec_y(self._sel_f_high, spec_r)
                fy_bot = self._freq_to_spec_y(self._sel_f_low, spec_r)
                s_box = QRectF(sx0, fy_top, sx1 - sx0, max(4.0, fy_bot - fy_top))
                p.setPen(QPen(QColor(240, 168, 72, 65), 4.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(s_box)
                p.setPen(QPen(QColor(ACCENT_AMBER), 1.4))
                p.setBrush(QColor(240, 168, 72, 32))
                p.drawRect(s_box)
            else:
                s_sel = QRectF(sx0, spec_r.top(), sx1 - sx0, spec_r.height())
                p.fillRect(s_sel, QColor(40, 216, 240, 18))
                p.setPen(QPen(QColor(40, 216, 240, 140), 1.0))
                p.drawLine(QPointF(sx0, spec_r.top()), QPointF(sx0, spec_r.bottom()))
                p.drawLine(QPointF(sx1, spec_r.top()), QPointF(sx1, spec_r.bottom()))

        # 4. 悬浮毛玻璃药丸增益 HUD
        if self.selection_gain_editable:
            self._draw_selection_gain(p, body, wave_r, (sx0 + sx1) * 0.5)

        # 选区精确时长胶囊
        if (sx1 - sx0) >= 68.0:
            dur_txt = f"Δ {self._sel_t1 - self._sel_t0:.3f}s"
            dur_r = QRectF((sx0 + sx1) * 0.5 - 36.0, wave_r.bottom() - 21.0, 72.0, 17.0)
            p.setPen(QPen(QColor("#303C50"), 1.0))
            p.setBrush(QColor(11, 15, 22, 232))
            p.drawRoundedRect(dur_r, 8.5, 8.5)
            p.setPen(QColor(ACCENT_CYAN))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(dur_r, Qt.AlignmentFlag.AlignCenter, dur_txt)

        p.restore()

    def _draw_selection_gain(self, p: QPainter, body: QRectF, wave_r: QRectF, center_x: float) -> None:
        active_gain = abs(self._preview_gain_db) >= 0.05
        hud_w = 124.0 if active_gain else 104.0
        hud_h = 26.0
        cx = max(body.left() + hud_w * 0.5 + 8.0, min(body.right() - hud_w * 0.5 - 8.0, center_x))
        cy = wave_r.top() + 26.0
        self._gain_hud_rect = QRectF(cx - hud_w * 0.5, cy - hud_h * 0.5, hud_w, hud_h)

        # HUD 底部投影
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(self._gain_hud_rect.translated(0, 2.0), 13.0, 13.0)

        hud_bg = QLinearGradient(self._gain_hud_rect.topLeft(), self._gain_hud_rect.bottomLeft())
        hud_bg.setColorAt(0.0, QColor(32, 37, 48, 245))
        hud_bg.setColorAt(1.0, QColor(16, 19, 26, 245))
        p.setPen(QPen(QColor(ACCENT_AMBER if active_gain else "#485166"), 1.2))
        p.setBrush(hud_bg)
        p.drawRoundedRect(self._gain_hud_rect, 13.0, 13.0)

        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor(ACCENT_AMBER if active_gain else "#EAEEF7"))
        txt_w = hud_w - 25.0 if active_gain else hud_w
        ch_tag = {0: "L ", 1: "R "}.get(self._edit_channel, "")
        p.drawText(
            QRectF(self._gain_hud_rect.left() + 2.0, self._gain_hud_rect.top(), txt_w, hud_h),
            Qt.AlignmentFlag.AlignCenter,
            f"↕ {ch_tag}{self._preview_gain_db:+.1f} dB",
        )

        if active_gain:
            self._gain_commit_rect = QRectF(self._gain_hud_rect.right() - 22.5, self._gain_hud_rect.top() + 3.5, 19.0, 19.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT_GREEN))
            p.drawRoundedRect(self._gain_commit_rect, 9.5, 9.5)
            p.setPen(QColor("#090C10"))
            p.drawText(self._gain_commit_rect, Qt.AlignmentFlag.AlignCenter, "✓")


# =====================================================================
# 5. 右侧双声道实时峰值电平表 (复用 EQ-UI.py MasterOutputStrip 视觉风格)
# =====================================================================
class StereoOutputMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._left_db = -60.0
        self._right_db = -60.0
        self._hold_l = -60.0
        self._hold_r = -60.0
        self._clip_l = False
        self._clip_r = False
        self.setFixedWidth(44)
        self.setToolTip("输出峰值电平表 (点击重置峰值保持与削波灯)")

    def setLevels(self, left_db: float, right_db: float):
        self._left_db = max(-60.0, min(0.0, left_db))
        self._right_db = max(-60.0, min(0.0, right_db))
        self._hold_l = max(self._hold_l - 0.45, self._left_db)
        self._hold_r = max(self._hold_r - 0.45, self._right_db)
        if left_db >= -0.1:
            self._clip_l = True
        if right_db >= -0.1:
            self._clip_r = True
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._clip_l = False
            self._clip_r = False
            self._hold_l = self._left_db
            self._hold_r = self._right_db
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#222226"))

        max_hold = max(self._hold_l, self._hold_r)
        is_clip = self._clip_l or self._clip_r
        peak_str = "CLIP" if is_clip else (f"{max_hold:.1f}" if max_hold > -59.0 else "-∞")
        peak_col = QColor(ACCENT_RED) if (is_clip or max_hold > -3.0) else QColor("#A6AEBF")
        p.setPen(peak_col)
        p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
        p.drawText(QRectF(1.0, 4.0, self.width() - 2.0, 13.0), Qt.AlignmentFlag.AlignCenter, peak_str)

        top_y = 26.0
        bar_h = max(40.0, self.height() - 46.0)
        bars = [
            (4.0, self._left_db, self._hold_l, self._clip_l, "L"),
            (15.0, self._right_db, self._hold_r, self._clip_r, "R"),
        ]

        m_grad = QLinearGradient(0, top_y + bar_h, 0, top_y)
        m_grad.setColorAt(0.0, QColor(ACCENT_GREEN))
        m_grad.setColorAt(0.75, QColor(ACCENT_AMBER))
        m_grad.setColorAt(1.0, QColor(ACCENT_RED))

        for bx, db_val, hold_db, clipped, ch_lbl in bars:
            if clipped:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 72, 72, 70))
                p.drawRoundedRect(QRectF(bx - 1.0, 16.5, 10.0, 7.0), 2.5, 2.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FF5252") if clipped else QColor("#18181C"))
            p.drawRoundedRect(QRectF(bx, 18.0, 8.0, 4.5), 1.5, 1.5)

            track = QRectF(bx, top_y, 8.0, bar_h)
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor("#0E0E11"))
            p.drawRoundedRect(track, 2.0, 2.0)

            norm = max(0.0, min(1.0, (db_val + 60.0) / 60.0))
            if norm > 0.01:
                fh = bar_h * norm
                fill_r = QRectF(bx + 1.0, track.bottom() - fh, 6.0, fh)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(m_grad)
                p.drawRoundedRect(fill_r, 1.2, 1.2)

            p.setPen(QPen(QColor(9, 11, 15, 185), 1.0))
            step_px = 3.5
            y_grid = track.bottom() - step_px
            while y_grid > track.top() + 2.0:
                p.drawLine(QPointF(bx + 1.0, y_grid), QPointF(bx + 7.0, y_grid))
                y_grid -= step_px

            hold_norm = max(0.0, min(1.0, (hold_db + 60.0) / 60.0))
            if hold_norm > 0.02:
                hy = track.bottom() - bar_h * hold_norm
                p.setPen(QPen(QColor("#FFFFFF") if hold_db < -3.0 else QColor(ACCENT_RED), 1.5))
                p.drawLine(QPointF(bx + 1.0, hy), QPointF(bx + 7.0, hy))

            p.setPen(QColor("#8E96A8"))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(QRectF(bx - 1, track.bottom() + 3, 10.0, 14.0), Qt.AlignmentFlag.AlignCenter, ch_lbl)

        p.setFont(QFont(MONO_FONT_FAM, 6))
        for db_tick in (0, -6, -12, -24, -36, -48):
            y = math.floor(top_y + bar_h * (1.0 - (db_tick + 60.0) / 60.0)) + 0.5
            p.setPen(QPen(QColor("#3E4659"), 1.0))
            p.drawLine(QPointF(25.0, y), QPointF(28.0, y))
            p.setPen(QColor("#788092"))
            p.drawText(QRectF(29.0, y - 6.0, 14.0, 12.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(abs(db_tick)))


# =====================================================================
# 6. 主面板：Audition 风格音频片段波形与频谱编辑器
# =====================================================================
# ------------------------- 后端对接指南 -------------------------------
# 【对接方式】在剪辑时间线中右键音频片段触发"编辑"时，实例化
# StudioAudioClipEditor 并绑定以下信号即可完成集成：
#
# 1) 注入波形与频谱数据：
#    后端解码音频后，按固定时间步长（例如每 2ms 一个 bin）提取各声道的
#    peaks_min / peaks_max / rms（取值范围 [-1.0, 1.0]），以及短时傅里叶
#    变换（STFT）归一化矩阵 spectrogram_2d（[time_bins][freq_bins]，
#    取值范围 [0.0, 1.0]），调用 editor.load_audio_data(...) 即可瞬间完成渲染。
#
# 2) 接收编辑指令信号（均为非破坏性编辑指令，由后端实际改写音频缓冲）：
#    - gainApplied(t0, t1, gain_db)：用户拖动悬浮增益胶囊并点击 ✓ 或点击
#      "应用增益"后触发。后端对 [t0, t1] 区间采样乘以 10**(gain_db/20)，
#      然后回传更新后的波形（再次调用 load_audio_data）。
#    - channelGainApplied(ch, t0, t1, gain_db)：单声道增益指令（ch=0 左 / 1 右）。
#      用户在增益卡把目标声道切到 L 或 R 后，拖悬浮增益胶囊/旋钮并提交时发出；
#      全声道操作仍走 gainApplied。后端仅对对应声道区间乘以增益系数。
#    - canvas.setEditChannel(ch)：编辑目标声道切换（-1=全部/L+R），预览渲染、
#      幽灵轮廓与提交信号随之切换；非目标声道在预览中自动压暗显示。
#    - spectralRepairRequested(t0, t1, f_low, f_high, mode)：用户在语图
#      框选某段时间的特定频段（如 50~80Hz 电源嗡嗡声、5k~8kHz 齿音）并
#      触发修复时发出（右键菜单 / 顶部"修复 (H)"按钮 / 快捷键 H）。
#      后端用带阻滤波 / 频谱插值处理该区域；mode 目前为 "heal"。
#    - noisePrintCaptured(t0, t1) 与 denoiseRequested(t0, t1, reduction_db)：
#      对应 Audition 经典的"采集噪声样本 -> 谱减法降噪"两步工作流。
#    - commitAndCloseRequested()：用户点击"保存修改并返回剪辑"时触发，
#      将处理后的音频缓存替换 / 非破坏性关联回时间线上的 Clip。
#    - spectralBrushApplied([(t, f_hz), ...], radius_px)：修复笔涂抹轨迹
#      （UI 已对语图矩阵做本地视觉平滑预览），后端沿轨迹做真实频谱修复。
#    - channelStateChanged(ch_idx, muted, phase_inverted)：左侧 CH 轨头
#      点击静音 / 右键反相 / 独奏时发出，后端联动真实混音矩阵。
#    - markersChanged([{"time", "label"}, ...])：M 键 / 右键标尺打点后发出，
#      持久化到 Clip 元数据。
#    - envelopeChanged([{"t": float, "db": float}, ...])：音量包络关键帧
#      （Vocal Riding，按 t 升序）。用户在波形上添加/拖拽/删除包络节点后发出。
#      后端按关键帧分段插值（Catmull-Rom 切线样条或线性，与 UI "平滑"开关一致），
#      对每个采样乘以 10**(db/20) 即可实现连续动态塑形；首节点之前/末节点
#      之后沿用边界节点增益。节点支持点击选中 + Del/退格键删除。
#    - timePitchApplied(t0, t1, ratio_pct, pitch_semitones, preserve_pitch, algorithm)：
#      时间伸缩与音高微调指令。time_stretch_factor = 100.0 / ratio_pct（125% 表示
#      加快到 0.8 倍时长）；preserve_pitch=True 时接入 pyrubberband / WSOLA 并传
#      pitch_semitones；False 时按 ratio_pct/100.0 高质量重采样（磁带变速 Varispeed，
#      音高按 12*log2(r) 自然联动）。处理完调用 load_audio_data 回传新波形与新总时长。
#    - lufsNormalizeRequested(t0, t1, target_lufs, true_peak_dbtp)：一键匹配 ITU-R
#      BS.1770 目标响度。后端计算 Integrated LUFS 增益补偿并做真峰值限幅，处理后把
#      实测 integrated_lufs / true_peak_dbtp 放进 cleanliness_report 回传即可刷新读数。
#    - processStackChanged([{"id","type","label","detail","color","enabled","params"}, ...])：
#      非破坏性处理栈全量列表（增益/降噪/频谱修复/时间伸缩/响度等操作自动入栈）。
#      后端以原始音频缓冲为起点按顺序遍历 enabled 步骤重放 DSP 管线。
#    - processStepToggled(step_id, enabled) / processStepRemoved(step_id)：单步旁通
#      与删除，用于增量更新管线开关（也可只依赖 processStackChanged 全量重放）。
#    - LissajousGoniometer.pushStereoFrame(l_samples, r_samples)：实时立体声
#      采样帧（李萨如相位示波器 + 相位相关度表）。播放管线每帧推送真实 PCM
#      即可获得 RX 级相位可视化；当前演示模式由 _tick_demo_playback 合成。
#    - toolModeChanged("select"|"envelope"|"brush") / snapZeroCrossingToggled(bool) /
#      bypassOriginalToggled(bool) / fftSizeChanged(512|1024|2048|4096) /
#      fadeCurveChanged("Linear"|"S-Curve"|"Log")：状态类开关，供后端
#      切换处理管线与修复算法参数。
#
# 【协同建议一：非破坏性编辑栈 (Edit Command Stack)】
#    不要直接覆盖原始音频文件，而是在后端维护一个操作列表，例如
#    [Gain(1.2s, 3.4s, +4.5dB), Denoise(0.0s, 8.5s, 12dB), SpectralHeal(...)]。
#    这样顶部"原声对比 (Bypass)"与 Ctrl+Z 撤销只需切换渲染管线开关即可
#    零延迟响应：bypassOriginalToggled(True) 时直接用原始缓冲渲染。
#
# 【协同建议二：过零点吸附计算 (Snap to Zero-Crossing)】
#    当 snapZeroCrossingToggled(True) 启用时，后端在接收到
#    selectionChanged(t0, t1, ...) 后，只需在 t0 / t1 前后 ±15ms 窗口内
#    搜索采样点符号翻转（sample[i] * sample[i+1] <= 0）的位置，返回对齐
#    后的精确时间点（可调用 editor.canvas 的选区接口回写），防止剪辑爆音。
#    UI 侧兜底方案：在选区边缘自动施加 5~10ms 微淡变。
# ---------------------------------------------------------------------
# ------------------- 待开发功能路线图（对照 Adobe Audition） -------------------
# 以下功能 UI 层已就绪或部分就绪，剩余工作集中在后端 DSP 对接：
#
# 已完成 UI 闭环、待后端接管的：
# 1. 声道独立开关（CH1/CH2 轨头交互）：左键静音、右键反相(Ø)/独奏均已实现，
#    波形渲染与电平表已联动；后端需响应 channelStateChanged 更新真实混音。
# 2. 时间标尺拖拽缩放：水平拖平移、垂直拖以光标为中心缩放（ruler_nav）。
# 3. 跳过选区试听 (Ctrl+Space)：演示播放器已实现跳选区逻辑，接入真实后端
#    播放管线时需支持 skip 区间无缝衔接。
# 4. 标记点 Markers (M)：打点/清除/渲染已实现，markersChanged 信号需持久化。
# 5. 相位反转 (Invert Phase)：视觉预览已实现（波形翻转 + Ø 徽标），后端需
#    对真实采样做 -x 处理。
#
# 尚未实现、规划中的：
# 6. 时间伸缩与音高微调 (Time & Pitch Stretch)：【UI 已闭环】右侧 TIME & PITCH
#    STRETCH 卡片已实现：伸缩速率 50%~200%（旋钮/胶囊双输入）、目标时长秒数
#    双向反算（对齐画面切点）、保持原调开关（关闭时磁带变速 12*log2(r) 联动
#    半音）、±12 半音音高微调、算法内核选择（人声共振峰/通用复音/瞬态鼓点）、
#    画布实时目标落点虚线 + 差值胶囊预览；演示模式本地线性重采样。剩余工作：
#    后端监听 timePitchApplied 接入 WSOLA / Phase Vocoder 真实处理并回传波形。
# 7. 去齿音 (DeEsser)：高频刺耳齿音的快速批处理（右侧卡片加按钮 + 指令信号）。
# 8. LUFS 集成响度匹配 (Loudness Match)：【UI 已闭环】LUFS LOUDNESS MATCH 卡片
#    已实现：OLED 双读数（Integrated LUFS + True Peak dBTP，超天花板变红警示）、
#    -14/-16/-23 行业预设 + 自定义目标、真峰值天花板限幅输入、一键匹配按钮实时
#    显示补偿 dB。剩余工作：后端监听 lufsNormalizeRequested 做 BS.1770 实测匹配，
#    并把 integrated_lufs / true_peak_dbtp 放进 cleanliness_report 回填读数。
# 9. 非破坏性效果历史列表 (Effects Rack / History)：【UI 已闭环】PROCESS STACK
#    卡片已实现：增益/降噪/频谱修复/时间伸缩/响度匹配全部自动入栈（ProcessStepRow
#    机架条目 + 序号 + 参数摘要），单步旁通开关 / 删除 / 清空、激活计数徽标、
#    Ctrl+Z / Ctrl+Y 快照回溯、全局"原声对比"切换时整栈置灰。剩余工作：后端监听
#    processStackChanged 按步骤顺序重放 DSP（见对接指南"后端 DSP 重放约定"）。
#
# 本轮对照 Audition 精修工作流与 NLE 剪辑协同，补充规划的高频实用功能：
# 10. 选区时间码精确输入 (Selection Precision Input)：把底部"选区"纯文本
#     升级为 Start（入点）/ End（出点）/ Duration（时长）三个可双击输入
#     精确数值的胶囊（复用 EditableValuePill），支持按毫秒或视频帧对齐
#     画面切点；输入后回写 canvas 的选区接口（setSelection）。
# 11. 波形关键帧音量包络线 (Clip Volume Keyframe Envelope)：【UI 已闭环】
#     ✎ 包络工具 (V) + 右侧"启用/平滑/重置"控制条已实现：点击插入节点、拖拽
#     调节（Alt 精调 / Shift 锁轴 / 0dB 磁吸）、双击归零、Alt+点击或右键删除、
#     线段整段平移、选区 4 点防爆音推拉；波形振幅随包络实时缩放并显示幽灵
#     原始轮廓。剩余工作：后端监听 envelopeChanged 按关键帧插值逐段应用增益。
# 12. 语图纵轴（频率轴）缩放 + L/R/M/S 监听胶囊：左侧频率刻度槽支持滚轮
#     缩放频段（如局部放大 20Hz~500Hz 修电源嗡声），提升画笔涂抹精度；
#     语图顶部加 L / R / M / S 通道切换胶囊。需要后端提供分声道 STFT
#     与中/侧(Mid/Side)矩阵。
# 13. 一键高通低切与削波修复 (High-Pass / DeClip)：外景风噪、手持麦低频
#     闷响用一键 80Hz / 100Hz 陡峭高通；麦克风过载平顶用 DeClip 削波修复。
#     建议新增两个按钮 + 对应指令信号（如 highPassRequested(t0, t1, cutoff_hz)
#     / declipRequested(t0, t1)），比手动框选语图更干净。
# 14. 剪辑点自动微交叉淡化 (Auto-Crossfade on Delete/Cut)：后端执行
#     deleteRangeRequested 拼接时，默认在接缝处施加 5~10ms 等功率交叉淡化
#     ——Z 过零点吸附只防极性跳变，防不了前后两段底噪纹理差异导致的"咔哒"。
# 15. 相位相关性指示条 (Phase Correlation Meter) + 单声道监听 (Mono Check)：
#     在右侧电平表旁增加一条 -1~+1 实时相关度指示条与 Mono 合并监听按钮，
#     用于发现双声道微小延迟导致的相位抵消（手机单声道外放声音发空）。
#     后端需输出实时左右声道相关系数；与第 5 项相位反转配合排查。
# ----------------------------------------------------------------------
class LissajousGoniometer(QWidget):
    """立体声李萨如相位示波器：磷光余晖 QImage 缓冲 + 相位相关度指示条"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(58, 76)
        self._trail_img: QImage | None = None
        self._corr = 1.0  # 相位相关度 [-1.0, +1.0]
        self.setToolTip("立体声李萨如相位示波器 & 相关度表 (+1单声道 / 0宽立体声 / -1反相抵消)")

    def _ensure_buffer(self):
        dpr = max(1.0, self.devicePixelRatioF())
        w_px = int(round(58 * dpr))
        h_px = int(round(58 * dpr))
        if self._trail_img is None or self._trail_img.width() != w_px:
            self._trail_img = QImage(w_px, h_px, QImage.Format.Format_ARGB32_Premultiplied)
            self._trail_img.setDevicePixelRatio(dpr)
            self._trail_img.fill(Qt.GlobalColor.transparent)

    def pushStereoFrame(self, left_samples: list[float], right_samples: list[float]):
        """输入当前帧左右声道样本列表，更新磷光余晖轨迹与相关系数"""
        self._ensure_buffer()
        assert self._trail_img is not None

        # 1. 利用 DestinationIn 乘法衰减旧帧 Alpha，实现纯正示波器余晖而不发灰
        tp = QPainter(self._trail_img)
        tp.setRenderHint(QPainter.RenderHint.Antialiasing)
        tp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        tp.fillRect(QRectF(0, 0, 58, 58), QColor(255, 255, 255, 198))
        tp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        cx, cy = 29.0, 29.0
        radius = 24.0
        inv_sqrt2 = 0.70710678

        sum_lr = 0.0
        sum_l2 = 1e-9
        sum_r2 = 1e-9

        poly = QPolygonF()
        for l_v, r_v in zip(left_samples, right_samples):
            # 旋转 45° 映射到 Mid(垂直) / Side(水平) 坐标系
            side = (l_v - r_v) * inv_sqrt2
            mid = (l_v + r_v) * inv_sqrt2
            px = max(3.0, min(55.0, cx + side * radius))
            py = max(3.0, min(55.0, cy - mid * radius))
            poly.append(QPointF(px, py))

            sum_lr += l_v * r_v
            sum_l2 += l_v * l_v
            sum_r2 += r_v * r_v

        if len(poly) >= 2:
            tp.setPen(QPen(QColor(40, 216, 240, 190), 1.2))
            tp.drawPolyline(poly)
        tp.end()

        raw_corr = max(-1.0, min(1.0, sum_lr / math.sqrt(sum_l2 * sum_r2)))
        self._corr = self._corr * 0.75 + raw_corr * 0.25
        self.update()

    def paintEvent(self, event):
        self._ensure_buffer()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. 上半部：58x58 示波器雷达屏
        scope_r = QRectF(0.5, 0.5, 57.0, 57.0)
        p.setPen(QPen(QColor("#272C3A"), 1.0))
        p.setBrush(QColor("#090B0F"))
        p.drawRoundedRect(scope_r, 6.0, 6.0)

        # 45° L/R 基准对角线 + 中心十字准星
        p.setPen(QPen(QColor(255, 255, 255, 18), 1.0, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(8, 8), QPointF(50, 50))
        p.drawLine(QPointF(50, 8), QPointF(8, 50))
        p.drawLine(QPointF(29, 5), QPointF(29, 53))
        p.drawLine(QPointF(5, 29), QPointF(53, 29))

        p.setFont(QFont(MONO_FONT_FAM, 6))
        p.setPen(QColor("#5C657A"))
        p.drawText(QRectF(4, 3, 12, 10), Qt.AlignmentFlag.AlignLeft, "L")
        p.drawText(QRectF(42, 3, 12, 10), Qt.AlignmentFlag.AlignRight, "R")

        if self._trail_img is not None:
            p.drawImage(QRectF(0, 0, 58, 58), self._trail_img)

        # 2. 下半部：-1 .. 0 .. +1 相位相关度微条
        corr_r = QRectF(0.5, 61.5, 57.0, 14.0)
        p.setPen(QPen(QColor("#252A38"), 1.0))
        p.setBrush(QColor("#0B0D12"))
        p.drawRoundedRect(corr_r, 3.5, 3.5)

        mid_x = corr_r.center().x()
        p.setPen(QPen(QColor("#384052"), 1.0))
        p.drawLine(QPointF(mid_x, corr_r.top() + 2), QPointF(mid_x, corr_r.bottom() - 2))

        nx = mid_x + self._corr * (corr_r.width() * 0.44)
        bar_col = QColor(ACCENT_GREEN) if self._corr >= 0.0 else QColor(ACCENT_RED)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bar_col)
        x_left = min(mid_x, nx)
        p.drawRoundedRect(QRectF(x_left, corr_r.top() + 4.0, max(2.5, abs(nx - mid_x)), 6.0), 1.5, 1.5)


class FadeCurveMiniPreview(QWidget):
    """淡入/淡出卡片中央的迷你实时曲线形态显示器（线性 / S 曲线 / 对数）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(64, 34)
        self._curve = "S-Curve"
        self._out_curve = self._curve
        self._has_in = False
        self._has_out = False

    def setState(self, curve: str, fade_in: float, fade_out: float, *, out_curve: str | None = None) -> None:
        self._curve = curve
        self._out_curve = out_curve or curve
        self._has_in = fade_in > 0.001
        self._has_out = fade_out > 0.001
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(QColor("#252B3A"), 1.0))
        p.setBrush(QColor("#0B0D12"))
        p.drawRoundedRect(r, 4.0, 4.0)

        pad_x, pad_y = 5.0, 5.0
        w = r.width() - pad_x * 2
        h = r.height() - pad_y * 2
        y_bot = r.bottom() - pad_y
        y_top = r.top() + pad_y

        path = QPainterPath(QPointF(r.left() + pad_x, y_bot))
        steps = 28
        for i in range(steps + 1):
            t = i / steps
            if t < 0.38:
                gain = _display_fade_factor(self._curve, t / 0.38)
            elif t > 0.62:
                gain = _display_fade_factor(self._out_curve, (1.0 - t) / 0.38)
            else:
                gain = 1.0
            px = r.left() + pad_x + t * w
            py = y_bot - gain * h
            path.lineTo(px, py)

        fill_path = QPainterPath(path)
        fill_path.lineTo(r.right() - pad_x, y_bot)
        fill_path.closeSubpath()

        grad = QLinearGradient(0, y_top, 0, y_bot)
        grad.setColorAt(0.0, QColor(100, 149, 250, 95 if (self._has_in or self._has_out) else 45))
        grad.setColorAt(1.0, QColor(100, 149, 250, 8))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawPath(fill_path)

        p.setPen(QPen(QColor(ACCENT_BLUE if (self._has_in or self._has_out) else "#6B7C9E"), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPath(path)


class ProcessStepRow(QFrame):
    """非破坏性处理栈中的单条 DSP 处理步骤（暗色硬件机架条目：状态色点 + 参数摘要 + 旁通开关 + 删除）"""
    toggled = pyqtSignal(str, bool)
    removed = pyqtSignal(str)

    def __init__(self, step_data: dict, global_bypassed: bool = False, parent=None):
        super().__init__(parent)
        self._step_id = step_data["id"]
        self._enabled = step_data.get("enabled", True)
        self._global_bypassed = global_bypassed
        accent = step_data.get("color", ACCENT_CYAN)

        active = self._enabled and not self._global_bypassed
        border_col = "#18181B" if active else "#101012"
        bg_col = "#1D1D20" if active else "#151518"

        self.setFixedHeight(32)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {bg_col};
                border: 1px solid {border_col};
                border-left: 3px solid {accent if active else '#3A3A40'};
                border-radius: 3px;
            }}
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 5, 2)
        lay.setSpacing(5)

        # 1. 步骤序号 + 名称 + 参数摘要
        info_col = QVBoxLayout()
        info_col.setSpacing(0)

        title_lbl = QLabel(f"{step_data['index']}. {step_data['label']}")
        title_lbl.setStyleSheet(
            f"color: {'#E4E4E8' if active else '#6E6E76'}; "
            f"font-size: 9px; font-weight: 700; border: none;"
        )

        sub_txt = "[旁通] " + step_data["detail"] if not self._enabled else step_data["detail"]
        detail_lbl = QLabel(sub_txt)
        detail_lbl.setStyleSheet(
            f"color: {'#9A9AA2' if active else '#55555C'}; "
            f"font-family: {MONO_FONT_CSS}; font-size: 8px; border: none;"
        )

        info_col.addWidget(title_lbl)
        info_col.addWidget(detail_lbl)
        lay.addLayout(info_col, 1)

        # 2. 独立旁通开关（PowerToggleSwitch）
        self.sw = PowerToggleSwitch(checked=self._enabled)
        self.sw.setToolTip("启用 / 旁通此处理步骤")
        self.sw.toggled.connect(lambda on: self.toggled.emit(self._step_id, on))
        lay.addWidget(self.sw)

        # 3. 移除步骤按钮
        del_btn = QPushButton("×")
        del_btn.setFixedSize(16, 16)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setToolTip("从此处理栈移除该步骤")
        del_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #6B7387;
                border: none;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton:hover {
                background-color: #2D1A1E;
                color: #F26D6D;
                border-radius: 3px;
            }
        """)
        del_btn.clicked.connect(lambda: self.removed.emit(self._step_id))
        lay.addWidget(del_btn)


class StudioAudioClipEditor(QWidget):
    # 对接剪辑软件后端音频引擎的完整信号接口
    playStateChanged = pyqtSignal(bool)                        # True=播放, False=暂停
    stopRequested = pyqtSignal()
    loopToggled = pyqtSignal(bool)
    seekRequested = pyqtSignal(float)                          # time_sec
    selectionChanged = pyqtSignal(float, float, float, float)  # t0, t1, f_low, f_high
    gainApplied = pyqtSignal(float, float, float)              # t0, t1, gain_db（作用于全部声道）
    channelGainApplied = pyqtSignal(int, float, float, float)  # ch, t0, t1, gain_db（仅作用于单声道：0=左 1=右）
    fadeApplied = pyqtSignal(float, float)                     # fade_in_sec, fade_out_sec
    silenceRequested = pyqtSignal(float, float)                # t0, t1
    trimRequested = pyqtSignal(float, float)                   # t0, t1
    deleteRangeRequested = pyqtSignal(float, float)            # t0, t1
    normalizeRequested = pyqtSignal(float, float, float)       # t0, t1, target_db
    noisePrintCaptured = pyqtSignal(float, float)              # t0, t1
    denoiseRequested = pyqtSignal(float, float, float)         # t0, t1, reduction_db
    spectralRepairRequested = pyqtSignal(float, float, float, float, str)  # t0, t1, f_low, f_high, mode
    undoRequested = pyqtSignal()
    redoRequested = pyqtSignal()
    commitAndCloseRequested = pyqtSignal()

    # 工具状态与视图开关信号（详见上方"后端对接指南"）
    toolModeChanged = pyqtSignal(str)          # "select" (框选) | "envelope" (✎ 包络) | "brush" (污点修复画笔)
    snapZeroCrossingToggled = pyqtSignal(bool) # 过零点吸附开关（防剪辑爆音）
    bypassOriginalToggled = pyqtSignal(bool)   # True=试听未处理原声, False=试听处理后
    fftSizeChanged = pyqtSignal(int)           # 512 | 1024 | 2048 | 4096
    fadeCurveChanged = pyqtSignal(str)         # "Linear" | "S-Curve" | "Log"
    spectralBrushApplied = pyqtSignal(list, float)  # [(t, f_hz), ...], radius_px 修复笔涂抹轨迹（已含本地视觉平滑），后端沿轨迹精修
    channelStateChanged = pyqtSignal(int, bool, bool)  # ch_idx, muted, phase_inverted（后端联动真实混音）
    markersChanged = pyqtSignal(list)          # [{"time": float, "label": str}, ...] 标记点持久化到 Clip 元数据
    envelopeChanged = pyqtSignal(list)         # [{"t": float, "db": float}, ...] 音量包络关键帧，后端按 10**(db/20) 分段插值增益
    timePitchApplied = pyqtSignal(float, float, float, float, bool, str)
    # (t0, t1, ratio_pct, pitch_semitones, preserve_pitch, algorithm_mode) 时间伸缩与音高微调指令
    lufsNormalizeRequested = pyqtSignal(float, float, float, float)  # (t0, t1, target_lufs, true_peak_dbtp)
    processStackChanged = pyqtSignal(list)   # 完整非破坏性处理栈列表（供后端按顺序重算 DSP 管线）
    processStepToggled = pyqtSignal(str, bool)  # (step_id, enabled) 单个步骤旁通切换
    processStepRemoved = pyqtSignal(str)     # (step_id) 删除单个步骤

    def __init__(
        self, clip_name: str = "Dialogue_Take04.wav", parent: QWidget | None = None, *,
        show_commit_button: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Audio Clip Editor — {clip_name}")
        self.resize(1180, 680)
        self.setMinimumSize(1080, 600)

        self._clip_name = clip_name
        self._is_playing = False
        self._is_looping = False
        self._skip_selection_mode = False
        self._duration = 8.5
        self._sample_rate = 48000
        self._playhead = 0.0
        self._has_noise_print = False
        self._updating_time_pitch = False   # 时间伸缩控件双向联动防回环标志

        # 非破坏性处理栈（PROCESS STACK）与 LUFS 响度读数状态
        self._process_steps: list[dict] = []
        self._stack_undo_history: list[list[dict]] = []
        self._stack_redo_history: list[list[dict]] = []
        self._step_seq = 0

        self._current_lufs = -19.4
        self._current_true_peak = -1.8
        self._target_lufs = -14.0
        self._true_peak_ceil = -1.0

        # 本地演示数据缓存（接入真实后端时通过 load_audio_data 覆盖）
        self._demo_peaks_min: list[list[float]] = [[], []]
        self._demo_peaks_max: list[list[float]] = [[], []]
        self._demo_rms: list[list[float]] = [[], []]
        self._demo_spec: list[list[float]] = []

        self._setup_ui(show_commit_button=show_commit_button)
        self._setup_shortcuts()
        self._init_demo_audio()

        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._tick_demo_playback)
        self._play_timer.start(30)

    def _setup_ui(self, *, show_commit_button: bool) -> None:
        # 达芬奇 Fairlight 风格：统一 #27272B 哑光中性灰基底 + 1px #141416 纯黑细线切分边界
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            AudioOverviewMinimap, WaveformSpectralCanvas, StereoOutputMeter {{
                background-color: transparent;
            }}
            /* 画布区域去圆角卡片化，仅保留右侧 1px 黑线分割 */
            QFrame#CanvasDeck {{
                background-color: #1E1E22;
                border: none;
                border-right: 1px solid #141416;
                border-radius: 0px;
            }}
            /* 右侧功能区：去掉独立卡片圆角与外边距，改为 1px 黑线上下分割 */
            QFrame#GridRow {{
                background-color: #27272B;
                border: none;
                border-bottom: 1px solid #141416;
                border-radius: 0px;
            }}
            QFrame#SideCard {{
                background-color: #27272B;
                border: none;
                border-bottom: 1px solid #141416;
                border-radius: 0px;
            }}
            QFrame#GridCell {{
                background-color: #27272B;
                border: none;
                border-radius: 0px;
            }}
            QLabel#CardHeader {{
                color: #D0D0D8;
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 0.6px;
            }}
            QFrame#ToolGroup {{
                background-color: transparent;
                border: none;
            }}
            QPushButton#ToolBtn, QPushButton#GroupBtn {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 600;
                padding: 0px 7px;
                height: 22px;
            }}
            QPushButton#ToolBtn:hover, QPushButton#GroupBtn:hover {{
                background-color: #3C3C44;
                color: #FFFFFF;
            }}
            QPushButton#ToolBtn:pressed, QPushButton#GroupBtn:pressed {{
                background-color: #1D1D21;
                padding-top: 1px;
            }}
            QPushButton#ToolBtn:checked, QPushButton#GroupBtn:checked {{
                background-color: #182E38;
                color: {ACCENT_CYAN};
                border: 1px solid #24788C;
            }}
            QPushButton#GroupBtn[captured="true"] {{
                background-color: #193026;
                color: {ACCENT_GREEN};
                border: 1px solid #2A7554;
            }}
            QPushButton#PrimaryBtn {{
                background-color: #2E8B68;
                color: #F4F6F8;
                border: 1px solid #141416;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 700;
                padding: 0px 12px;
                height: 26px;
            }}
            QPushButton#PrimaryBtn:hover {{
                background-color: #36A178;
                color: #FFFFFF;
            }}
            QPushButton#PrimaryBtn:pressed {{
                background-color: #26755A;
                padding-top: 1px;
            }}
            QComboBox {{
                background-color: #18181B;
                color: #DCDCE2;
                border: 1px solid #121214;
                border-radius: 3px;
                padding: 0px 5px;
                font-size: 9px;
                font-weight: 600;
                height: 19px;
            }}
            QComboBox:hover {{ border-color: #484852; }}
            QComboBox::drop-down {{ border: none; width: 12px; }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 3px solid transparent;
                border-right: 3px solid transparent;
                border-top: 4px solid #9A9AA4;
                margin-right: 4px;
            }}
            QComboBox QAbstractItemView {{
                background-color: #222226;
                color: #E4E4EA;
                border: 1px solid #141416;
                selection-background-color: #35353D;
                selection-color: #FFFFFF;
                outline: none;
            }}
            QToolTip {{
                background-color: #1D1D21;
                color: #DCDCE2;
                border: 1px solid #141416;
                padding: 6px 9px;
                font-size: 11px;
            }}
        """)

        def _make_group(*widgets: QWidget, trailing_sep: bool = True) -> QFrame:
            # 达芬奇风格：按钮直接平铺在工具栏上，组与组之间仅用 1px 竖向黑线隔开；
            # 工具栏末尾一组不画分隔线，避免右缘出现多余垂线
            box = QFrame()
            box.setObjectName("ToolGroup")
            box.setFixedHeight(26)
            gl = QHBoxLayout(box)
            gl.setContentsMargins(0, 1, 4 if trailing_sep else 0, 1)
            gl.setSpacing(3)
            for w in widgets:
                gl.addWidget(w)
            if trailing_sep:
                v_line = QFrame()
                v_line.setFixedWidth(1)
                v_line.setFixedHeight(16)
                v_line.setStyleSheet("background-color: #141416; border: none;")
                gl.addSpacing(3)
                gl.addWidget(v_line)
            return box

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -------------------------------------------------------------
        # 1. 顶部工具栏（紧凑 36px，组间 1px 黑线统一，末尾无多余垂线）
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(36)
        # 样式必须限定在 #TopBar 自身：裸 QFrame 选择器会“渗”给子级 ToolGroup 容器，
        # 在每组按钮下方画出分段底线；底部区域分割线只画在工具栏自身边界上，
        # 与概览条/参数面板之间保持通长的 1px 黑线分隔
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb = QHBoxLayout(top_bar)
        tb.setContentsMargins(8, 5, 8, 5)
        tb.setSpacing(5)

        # [组1] 走带与数码时间码舱
        self.btn_play = QPushButton("▶ 播放")
        self.btn_play.setObjectName("GroupBtn")
        self.btn_play.setCheckable(True)
        self.btn_play.setFixedWidth(56)
        self.btn_play.setToolTip("播放/暂停 (Space)")
        self.btn_play.setStyleSheet("""
            QPushButton#GroupBtn:checked {
                background-color: #19382C;
                color: #4CD490;
                border: 1px solid #2A7554;
            }
        """)
        self.btn_play.clicked.connect(self._toggle_play)

        btn_stop = QPushButton("■")
        btn_stop.setObjectName("GroupBtn")
        btn_stop.setFixedWidth(24)
        btn_stop.setToolTip("停止并返回起点")
        btn_stop.clicked.connect(self._stop_playback)

        self.btn_loop = QPushButton("↻")
        self.btn_loop.setObjectName("GroupBtn")
        self.btn_loop.setCheckable(True)
        self.btn_loop.setFixedWidth(24)
        self.btn_loop.setToolTip("循环播放当前选区")
        self.btn_loop.clicked.connect(self._toggle_loop)
        transport = _make_group(self.btn_play, btn_stop, self.btn_loop)
        self._transport_layout: QHBoxLayout = transport.layout()
        tb.addWidget(transport)

        # 达芬奇风格深黑内嵌时间码框
        tc_box = QFrame()
        tc_box.setFixedHeight(22)
        tc_box.setStyleSheet("""
            QFrame {
                background-color: #151518;
                border: 1px solid #101012;
                border-radius: 3px;
            }
        """)
        tc_lay = QHBoxLayout(tc_box)
        tc_lay.setContentsMargins(7, 0, 7, 0)
        self.tc_lbl = QLabel("00:00.000")
        self.tc_lbl.setStyleSheet(f"""
            color: #ECECF0;
            font-family: {MONO_FONT_CSS};
            font-size: 11px;
            font-weight: 700;
            border: none;
        """)
        tc_lay.addWidget(self.tc_lbl)
        tb.addWidget(tc_box)

        # 采样率/位深徽章：独立于时间码框放置，绝不与其边框重叠
        self.sr_badge = QLabel("48k·32f")
        self.sr_badge.setFixedHeight(22)
        self.sr_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sr_badge.setStyleSheet("""
            background-color: #1E1E22;
            color: #8E8E98;
            border: 1px solid #141416;
            border-radius: 3px;
            padding: 0px 6px;
            font-size: 9px;
            font-weight: 700;
        """)
        tb.addWidget(self.sr_badge)

        # [组2] 选择与包络 / 修复笔工具组
        self.btn_tool_select = QPushButton("I 框选")
        self.btn_tool_select.setObjectName("GroupBtn")
        self.btn_tool_select.setCheckable(True)
        self.btn_tool_select.setChecked(True)
        self.btn_tool_select.setToolTip("框选工具 (A)")
        self.btn_tool_select.clicked.connect(lambda: self._set_tool_mode("select"))

        self.btn_tool_envelope = QPushButton("✎ 包络")
        self.btn_tool_envelope.setObjectName("GroupBtn")
        self.btn_tool_envelope.setCheckable(True)
        self.btn_tool_envelope.setToolTip("音量包络线工具：点击添加节点，拖拽调节各区域音量 (V)")
        self.btn_tool_envelope.clicked.connect(lambda: self._set_tool_mode("envelope"))

        self.btn_tool_brush = QPushButton("✦ 修复笔")
        self.btn_tool_brush.setObjectName("GroupBtn")
        self.btn_tool_brush.setCheckable(True)
        self.btn_tool_brush.setToolTip("污点修复画笔：在语图上涂抹消除口水音/爆音 (B)")
        self.btn_tool_brush.clicked.connect(lambda: self._set_tool_mode("brush"))

        self.btn_snap_zero = QPushButton("⌖ 过零")
        self.btn_snap_zero.setObjectName("GroupBtn")
        self.btn_snap_zero.setCheckable(True)
        self.btn_snap_zero.setChecked(True)
        self.btn_snap_zero.setToolTip("选区边缘自动吸附至过零点 (Z)")
        self.btn_snap_zero.toggled.connect(self.snapZeroCrossingToggled.emit)
        self.btn_snap_zero.toggled.connect(lambda on: setattr(self.canvas, "_magnetic_snap", on))
        tb.addWidget(_make_group(self.btn_tool_select, self.btn_tool_envelope, self.btn_tool_brush, self.btn_snap_zero))

        # [组3] 视图与缩放
        self.btn_spectral = QPushButton("语图")
        self.btn_spectral.setObjectName("GroupBtn")
        self.btn_spectral.setCheckable(True)
        self.btn_spectral.setChecked(True)
        self.btn_spectral.setToolTip("显示/隐藏频谱频率视图")
        self.btn_spectral.toggled.connect(self._on_spectral_toggled)

        btn_zoom_sel = QPushButton("缩放选区")
        btn_zoom_sel.setObjectName("GroupBtn")
        btn_zoom_sel.setToolTip("将选区铺满画布 (E)")
        btn_zoom_sel.clicked.connect(self._zoom_to_selection)

        btn_zoom_fit = QPushButton("全览")
        btn_zoom_fit.setObjectName("GroupBtn")
        btn_zoom_fit.clicked.connect(lambda: self._sync_viewport(0.0, self._duration))
        tb.addWidget(_make_group(self.btn_spectral, btn_zoom_sel, btn_zoom_fit))

        # [组4] 段落剪辑
        clip_btns = []
        for lbl_txt, tip_txt, slot_fn in (
            ("静音", "静音当前选区", self._emit_silence),
            ("裁剪", "裁剪保留当前选区", self._emit_trim),
            ("删除", "删除当前选区 (Delete)", self._emit_delete),
            ("标准化", "峰值标准化至 -1dBFS", self._emit_normalize),
        ):
            b = QPushButton(lbl_txt)
            b.setObjectName("GroupBtn")
            b.setToolTip(tip_txt)
            b.clicked.connect(slot_fn)
            clip_btns.append(b)
        tb.addWidget(_make_group(*clip_btns))

        # [组5] 降噪与频谱修复
        self.btn_noise_print = QPushButton("采集噪声")
        self.btn_noise_print.setObjectName("GroupBtn")
        self.btn_noise_print.setToolTip("框选纯底噪段落后点击采集特征")
        self.btn_noise_print.clicked.connect(self._emit_capture_noise)

        self.btn_denoise = QPushButton("降噪")
        self.btn_denoise.setObjectName("GroupBtn")
        self.btn_denoise.setToolTip("按右侧降噪衰减量执行谱减法降噪")
        self.btn_denoise.clicked.connect(self._emit_denoise)

        btn_heal = QPushButton("修复")
        btn_heal.setObjectName("GroupBtn")
        btn_heal.setToolTip("频谱插值修复选区 (H)")
        btn_heal.clicked.connect(self._trigger_quick_heal)
        tb.addWidget(_make_group(self.btn_noise_print, self.btn_denoise, btn_heal))

        tb.addStretch()

        # [组6] 监听对比与撤销栈
        self.btn_bypass = QPushButton("⇄ 原声对比")
        self.btn_bypass.setObjectName("ToolBtn")
        self.btn_bypass.setCheckable(True)
        self.btn_bypass.setFixedHeight(22)
        self.btn_bypass.setToolTip("切换对比处理前后差异 (Tab)")
        self.btn_bypass.setStyleSheet("""
            QPushButton#ToolBtn:checked {
                background-color: #3A2A14;
                color: #F0A848;
                border: 1px solid #8A5D20;
            }
        """)
        self.btn_bypass.toggled.connect(self.bypassOriginalToggled.emit)
        self.btn_bypass.toggled.connect(lambda _: self._rebuild_process_stack_ui())
        tb.addWidget(self.btn_bypass)

        btn_undo = QPushButton("↶")
        btn_undo.setObjectName("GroupBtn")
        btn_undo.setFixedWidth(26)
        btn_undo.setToolTip("撤销 (Ctrl+Z)")
        btn_undo.clicked.connect(self._undo_process_stack)

        btn_redo = QPushButton("↷")
        btn_redo.setObjectName("GroupBtn")
        btn_redo.setFixedWidth(26)
        btn_redo.setToolTip("重做 (Ctrl+Y)")
        btn_redo.clicked.connect(self._redo_process_stack)
        tb.addWidget(_make_group(btn_undo, btn_redo, trailing_sep=False))

        root.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部主工作区：零外边距，面板间直接用 1px 黑线贴合分割
        # -------------------------------------------------------------
        center_lay = QHBoxLayout()
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(0)

        # 左侧画布列（去圆角卡片化，仅保留右侧 1px 黑线分割）
        canvas_deck = QFrame()
        canvas_deck.setObjectName("CanvasDeck")
        left_col = QVBoxLayout(canvas_deck)
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(0)

        self.minimap = AudioOverviewMinimap()
        self.minimap.viewportChanged.connect(self._sync_viewport)
        self.minimap.playheadMoved.connect(self._on_minimap_playhead_moved)
        left_col.addWidget(self.minimap)

        # 概览条与主画布之间的 1px 黑线
        mini_sep = QFrame()
        mini_sep.setFixedHeight(1)
        mini_sep.setStyleSheet("background-color: #141416; border: none;")
        left_col.addWidget(mini_sep)

        self.canvas = WaveformSpectralCanvas()
        self.canvas.playheadMoved.connect(self._on_canvas_playhead_moved)
        self.canvas.selectionChanged.connect(self._on_canvas_selection_changed)
        self.canvas.viewportChanged.connect(self._sync_viewport)
        self.canvas.previewGainChanged.connect(self._on_preview_gain_changed)
        self.canvas.gainCommitRequested.connect(self._on_gain_committed)
        self.canvas.fadeChanged.connect(self._on_fade_changed)
        self.canvas.fadeCurveCommitted.connect(self._on_canvas_fade_curve_committed)
        self.canvas.spectralActionRequested.connect(self._on_spectral_action)
        self.canvas.spectralBrushApplied.connect(self.spectralBrushApplied.emit)
        self.canvas.channelStateChanged.connect(self.channelStateChanged.emit)
        self.canvas.markersChanged.connect(self.markersChanged.emit)
        self.canvas.envelopeChanged.connect(self.envelopeChanged.emit)
        left_col.addWidget(self.canvas, 1)

        center_lay.addWidget(canvas_deck, 1)

        # 右侧控制面板 + 电平表（参数网格 297px + 1px 竖线 + 电平表 44px = 342px，黑线贴合分割）
        right_panel = QWidget()
        right_panel.setFixedWidth(342)
        right_panel.setStyleSheet("""
            QWidget { background-color: #27272B; }
            QFrame#GridCell { background-color: #27272B; border: none; }
            QFrame#SideCard {
                background-color: #27272B;
                border: none;
                border-bottom: 1px solid #141416;
                border-radius: 0px;
            }
            QPushButton#MiniCellBtn {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
                padding: 0px 2px;
                height: 20px;
            }
            QPushButton#MiniCellBtn:hover {
                background-color: #3C3C44;
                color: #FFFFFF;
            }
            QPushButton#MiniCellBtn:pressed {
                background-color: #1D1D21;
            }
            QPushButton#MiniCellBtn:checked {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
            }
            /* 面板内 QWidget 底色规则会与全局 QComboBox 同级冲突，这里显式保住深黑内凹下拉框 */
            QComboBox {
                background-color: #18181B;
                color: #DCDCE2;
                border: 1px solid #121214;
                border-radius: 3px;
                padding: 0px 5px;
                font-size: 9px;
                font-weight: 600;
                height: 19px;
            }
        """)
        rp_lay = QHBoxLayout(right_panel)
        rp_lay.setContentsMargins(0, 0, 0, 0)
        rp_lay.setSpacing(0)

        right_container = QVBoxLayout()
        right_container.setContentsMargins(0, 0, 0, 0)
        right_container.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("""
            QScrollArea { background: #27272B; border: none; }
            QScrollBar:vertical { background: #222226; width: 4px; margin: 0px; }
            QScrollBar::handle:vertical { background: #42424A; border-radius: 2px; min-height: 24px; }
            QScrollBar::handle:vertical:hover { background: #55555E; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)

        def _make_grid_row(left_w: QWidget, right_w: QWidget) -> QFrame:
            """达芬奇插件式双列网格行：左右等宽并排，中间 1px 竖向黑线，底部 1px 横向黑线"""
            row_frame = QFrame()
            row_frame.setObjectName("GridRow")
            hl = QHBoxLayout(row_frame)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(0)
            hl.addWidget(left_w, 1)

            v_div = QFrame()
            v_div.setFixedWidth(1)
            v_div.setStyleSheet("background-color: #141416; border: none;")
            hl.addWidget(v_div)

            hl.addWidget(right_w, 1)
            return row_frame

        cards_host = QWidget()
        cards_host.setStyleSheet("background-color: #27272B;")
        cards_col = QVBoxLayout(cards_host)
        cards_col.setContentsMargins(0, 0, 0, 0)
        cards_col.setSpacing(0)  # 关键：间距设为 0，让黑线无缝衔接

        # 达芬奇 2×2 双列网格布局（按使用频率优先排布）：
        # 第 1 行 [选区增益 | 淡入淡出与包络]，第 2 行 [干净度降噪 | 时间与音高]，
        # 第 3 行全宽 LUFS 响度匹配，第 4 行全宽非破坏性处理栈
        cards_col.addWidget(_make_grid_row(self._build_gain_card(), self._build_fade_card()))

        cards_col.addWidget(_make_grid_row(self._build_cleanliness_card(), self._build_time_pitch_card()))

        cards_col.addWidget(self._build_loudness_card())

        cards_col.addWidget(self._build_process_stack_card())

        cards_col.addStretch()

        scroll.setWidget(cards_host)
        right_container.addWidget(scroll, 1)

        # 底部提交到时间线按钮（固定可见，顶部 1px 黑线隔开，不随卡片滚动）
        if show_commit_button:
            commit_wrap = QFrame()
            commit_wrap.setStyleSheet("background-color: #27272B; border-top: 1px solid #141416;")
            cw_lay = QVBoxLayout(commit_wrap)
            cw_lay.setContentsMargins(8, 7, 8, 7)
            commit_btn = QPushButton("✓ 保存修改并返回剪辑")
            commit_btn.setObjectName("PrimaryBtn")
            commit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            commit_btn.clicked.connect(self.commitAndCloseRequested.emit)
            cw_lay.addWidget(commit_btn)
            right_container.addWidget(commit_wrap)

        rp_lay.addLayout(right_container, 1)

        # 右侧监控列：双声道电平表贯穿全高（44px，与上下栏 1px 黑线完全对齐），李萨如示波器已移除
        meter_sep = QFrame()
        meter_sep.setFixedWidth(1)
        meter_sep.setStyleSheet("background-color: #141416; border: none;")
        self.output_meter = StereoOutputMeter()
        rp_lay.addWidget(meter_sep)
        rp_lay.addWidget(self.output_meter)

        center_lay.addWidget(right_panel)
        root.addLayout(center_lay, 1)

        # -------------------------------------------------------------
        # 3. 底部状态栏（#222226 + 顶部 1px 黑线）
        # -------------------------------------------------------------
        status_bar = QFrame()
        status_bar.setFixedHeight(24)
        status_bar.setStyleSheet("background-color: #222226; border-top: 1px solid #141416;")
        sb = QHBoxLayout(status_bar)
        self._status_layout = sb
        sb.setContentsMargins(10, 0, 10, 0)
        sb.setSpacing(10)

        engine_dot = QLabel("● DSP READY")
        engine_dot.setStyleSheet(f"color: {ACCENT_GREEN}; font-family: {MONO_FONT_CSS}; font-size: 9px; font-weight: 700; border: none;")
        sb.addWidget(engine_dot)

        self.sel_info_lbl = QLabel("选区: 全片段 (00:00.000 - 00:08.500)")
        self.sel_info_lbl.setStyleSheet(f"color: #A0A0AA; font-family: {MONO_FONT_CSS}; font-size: 10px; border: none;")
        sb.addWidget(self.sel_info_lbl)
        sb.addStretch()

        quick_tip = QLabel("滚轮缩放 · Alt+滚轮放大波形 · 右键声道独奏/反相")
        quick_tip.setStyleSheet("color: #787882; font-size: 10px; border: none;")
        sb.addWidget(quick_tip)

        kbd_badge = QLabel("⌨ 快捷键")
        kbd_badge.setStyleSheet("""
            background-color: #2E2E34;
            color: #B4B4BE;
            border: 1px solid #141416;
            border-radius: 3px;
            padding: 1px 6px;
            font-size: 9px;
        """)
        kbd_badge.setToolTip(
            "Space: 播放/暂停  |  Ctrl+Space: 跳过选区试听\n"
            "A: 框选工具  |  V: 音量包络  |  B: 污点修复笔  |  H: 频谱修复\n"
            "Z: 过零点吸附  |  E: 缩放至选区  |  M: 添加标记点\n"
            "Del: 删除选区/包络节点  |  Tab: 原声/处理对比  |  Alt+滚轮: 波形垂直放大"
        )
        sb.addWidget(kbd_badge)

        root.addWidget(status_bar)

    def _build_gain_card(self) -> QFrame:
        # 达芬奇网格单元版：无彩色边框/状态点，直接继承 QFrame#GridCell 纯平样式
        cell = QFrame()
        cell.setObjectName("GridCell")
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(8, 7, 8, 8)
        lay.setSpacing(6)

        hdr = QLabel("CLIP / SELECTION GAIN")
        hdr.setObjectName("CardHeader")
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hdr)

        # 增益作用声道切换：L+R（全部）/ L（仅左）/ R（仅右）
        tgt_row = QHBoxLayout()
        tgt_row.setSpacing(3)
        self._edit_ch_btns: dict[int, QPushButton] = {}
        for ch_val, ch_lbl, ch_tip in ((-1, "L+R", "增益作用于全部声道"), (0, "L", "仅作用于左声道"), (1, "R", "仅作用于右声道")):
            b = QPushButton(ch_lbl)
            b.setObjectName("MiniCellBtn")
            b.setCheckable(True)
            b.setChecked(ch_val == -1)
            b.setToolTip(ch_tip)
            b.clicked.connect(lambda checked=False, cv_=ch_val: self._on_edit_channel_selected(cv_))
            tgt_row.addWidget(b, 1)
            self._edit_ch_btns[ch_val] = b
        lay.addLayout(tgt_row)

        row = QHBoxLayout()
        row.setSpacing(6)
        left_v = QVBoxLayout()
        left_v.setSpacing(3)
        lbl = QLabel("音量增益")
        lbl.setStyleSheet("color: #B4B4BE; font-size: 10px;")
        self.gain_pill = EditableValuePill("0.0 dB", ACCENT_AMBER, width=62)
        self.gain_pill.setToolTip("拖拽左侧旋钮预览，双击胶囊输入精确增益 (-36.0 ~ +24.0 dB)")
        self.gain_pill.valueEdited.connect(self._on_gain_pill_edited)
        left_v.addWidget(lbl)
        left_v.addWidget(self.gain_pill)

        self.gain_knob = ProRotaryKnob(-36.0, 24.0, 0.0, 0.0, ACCENT_AMBER, mode="bipolar")
        self.gain_knob.valueChanged.connect(self._on_gain_knob_changed)

        row.addLayout(left_v)
        row.addStretch()
        row.addWidget(self.gain_knob)
        lay.addLayout(row)

        apply_gain_btn = QPushButton("应用增益到选区")
        apply_gain_btn.setObjectName("MiniCellBtn")
        apply_gain_btn.setToolTip("把预览增益写入选区（非破坏性，自动入处理栈）")
        apply_gain_btn.setStyleSheet(f"""
            QPushButton#MiniCellBtn {{
                background-color: #342B1E;
                color: {ACCENT_AMBER};
                border: none;
                height: 22px;
            }}
            QPushButton#MiniCellBtn:hover {{
                background-color: #423625;
                color: #FFFFFF;
            }}
        """)
        apply_gain_btn.clicked.connect(lambda: self._on_gain_committed(*self.canvas.getSelection()[:2], self.gain_knob.value()))
        lay.addWidget(apply_gain_btn)
        return cell

    def _build_cleanliness_card(self) -> QFrame:
        # 达芬奇网格单元版：PEAK/RMS/FLOOR 读数框改为 #151518 深黑内凹框
        cell = QFrame()
        cell.setObjectName("GridCell")
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(8, 7, 8, 8)
        lay.setSpacing(6)

        hdr = QLabel("CLEANLINESS & NOISE")
        hdr.setObjectName("CardHeader")
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hdr)

        # 三栏深黑内凹读数框
        metrics_box = QFrame()
        metrics_box.setStyleSheet("""
            QFrame {
                background-color: #151518;
                border: 1px solid #101012;
                border-radius: 3px;
            }
        """)
        m_lay = QHBoxLayout(metrics_box)
        m_lay.setContentsMargins(2, 4, 2, 4)
        m_lay.setSpacing(0)

        def _make_stat(title: str, init_val: str, color: str) -> QLabel:
            col = QVBoxLayout()
            col.setSpacing(2)
            t = QLabel(title)
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setStyleSheet("color: #787882; font-size: 8px; font-weight: 700; border: none;")
            v = QLabel(init_val)
            v.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.setStyleSheet(f"color: {color}; font-family: {MONO_FONT_CSS}; font-size: 9px; font-weight: 700; border: none;")
            col.addWidget(t)
            col.addWidget(v)
            m_lay.addLayout(col, 1)
            return v

        def _v_sep() -> QFrame:
            s = QFrame()
            s.setFixedWidth(1)
            s.setStyleSheet("background-color: #242429; border: none;")
            return s

        self.peak_val_lbl = _make_stat("PEAK", "-2.4dB", ACCENT_GREEN)
        m_lay.addWidget(_v_sep())
        self.rms_val_lbl = _make_stat("RMS", "-18.6dB", ACCENT_CYAN)
        m_lay.addWidget(_v_sep())
        self.noise_val_lbl = _make_stat("FLOOR", "-58.2dB", ACCENT_AMBER)
        lay.addWidget(metrics_box)

        # 语图底噪视觉增强旋钮（方便看清极微弱底噪）
        r1 = QHBoxLayout()
        l1 = QVBoxLayout()
        l1.setSpacing(2)
        t1 = QLabel("底噪显影")
        t1.setStyleSheet("color: #B4B4BE; font-size: 10px;")
        self.spec_boost_pill = EditableValuePill("+0 dB", ACCENT_CYAN, width=58)
        self.spec_boost_pill.setToolTip("提升语图暗部对比度，显影极微弱底噪痕迹 (0 ~ 36 dB)")
        self.spec_boost_pill.valueEdited.connect(self._on_spec_boost_text)
        l1.addWidget(t1)
        l1.addWidget(self.spec_boost_pill)

        self.spec_boost_knob = ProRotaryKnob(0.0, 36.0, 0.0, 0.0, ACCENT_CYAN, mode="linear")
        self.spec_boost_knob.valueChanged.connect(self._on_spec_boost_knob)
        r1.addLayout(l1)
        r1.addStretch()
        r1.addWidget(self.spec_boost_knob)
        lay.addLayout(r1)

        # 降噪衰减量旋钮
        r2 = QHBoxLayout()
        l2 = QVBoxLayout()
        l2.setSpacing(2)
        t2 = QLabel("降噪衰减")
        t2.setStyleSheet("color: #B4B4BE; font-size: 10px;")
        self.denoise_pill = EditableValuePill("12.0 dB", ACCENT_GREEN, width=58)
        self.denoise_pill.setToolTip("谱减法降噪强度 (0 ~ 40 dB，过大易产生水声伪影)")
        self.denoise_pill.valueEdited.connect(self._on_denoise_text)
        l2.addWidget(t2)
        l2.addWidget(self.denoise_pill)

        self.denoise_knob = ProRotaryKnob(0.0, 40.0, 12.0, 12.0, ACCENT_GREEN, mode="linear")
        self.denoise_knob.valueChanged.connect(lambda v: self.denoise_pill.setText(f"{v:.1f} dB"))
        r2.addLayout(l2)
        r2.addStretch()
        r2.addWidget(self.denoise_knob)
        lay.addLayout(r2)

        # 底行：噪声采集状态 + FFT 窗口精度合并（低 FFT 高时间精度抓口水音，高 FFT 高频率精度抓交流嗡声）
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(4)
        self.noise_status_lbl = QLabel("● 未采集")
        self.noise_status_lbl.setStyleSheet("color: #888892; font-size: 9px; font-weight: 600;")
        self.noise_status_lbl.setToolTip("噪声特征未采集，降噪按自适应模式估计底噪；框选纯底噪段落点工具栏「采集噪声」可锁定特征")
        bottom_row.addWidget(self.noise_status_lbl)

        fft_lbl = QLabel("FFT")
        fft_lbl.setStyleSheet("color: #9A9AA4; font-size: 10px; font-weight: 600;")
        bottom_row.addWidget(fft_lbl)

        self.fft_combo = QComboBox()
        self.fft_combo.addItems(["512 瞬态", "1024 均衡", "2048 人声", "4096 低频"])
        self.fft_combo.setCurrentIndex(2)
        self.fft_combo.setToolTip("低窗口抓瞬态口水音，高窗口抓低频交流嗡声")
        self.fft_combo.currentTextChanged.connect(
            lambda txt: self.fftSizeChanged.emit(int(txt.split()[0]))
        )
        bottom_row.addWidget(self.fft_combo, 1)
        lay.addLayout(bottom_row)
        return cell

    def _build_fade_card(self) -> QFrame:
        # 达芬奇网格单元版：与增益卡并排，标题居中
        cell = QFrame()
        cell.setObjectName("GridCell")
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(8, 7, 8, 8)
        lay.setSpacing(6)

        hdr = QLabel("FADE & ENVELOPE")
        hdr.setObjectName("CardHeader")
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hdr)

        row = QHBoxLayout()
        row.setSpacing(3)
        c1 = QVBoxLayout()
        c1.setSpacing(2)
        l1 = QLabel("淡入(s)")
        l1.setStyleSheet("color: #9A9AA4; font-size: 9px;")
        self.fi_pill = EditableValuePill("0.00 s", ACCENT_BLUE, width=42)
        self.fi_pill.setToolTip("双击输入淡入时长 (秒)，画布顶部蓝色手柄亦可拖拽")
        self.fi_pill.valueEdited.connect(self._on_fi_edited)
        c1.addWidget(l1)
        c1.addWidget(self.fi_pill)

        self.fade_preview = FadeCurveMiniPreview()
        self.fade_preview.setFixedSize(34, 26)

        c2 = QVBoxLayout()
        c2.setSpacing(2)
        l2 = QLabel("淡出(s)")
        l2.setAlignment(Qt.AlignmentFlag.AlignRight)
        l2.setStyleSheet("color: #9A9AA4; font-size: 9px;")
        self.fo_pill = EditableValuePill("0.00 s", ACCENT_BLUE, width=42)
        self.fo_pill.setToolTip("双击输入淡出时长 (秒)，画布顶部蓝色手柄亦可拖拽")
        self.fo_pill.valueEdited.connect(self._on_fo_edited)
        c2.addWidget(l2)
        c2.addWidget(self.fo_pill)

        row.addLayout(c1)
        row.addStretch()
        row.addWidget(self.fade_preview, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch()
        row.addLayout(c2)
        lay.addLayout(row)

        # 一键对称防爆音微淡入/淡出预设（保留归零与最常用防爆 20ms）
        preset_row = QHBoxLayout()
        preset_row.setSpacing(3)
        self.fade_preset_buttons: dict[float, QPushButton] = {}
        for sec_val, p_lbl in ((0.0, "归零"), (0.02, "防爆20m")):
            pb = QPushButton(p_lbl)
            pb.setObjectName("MiniCellBtn")
            pb.setToolTip("同时为淡入与淡出设置该时长")
            pb.clicked.connect(lambda checked=False, sv=sec_val: self._apply_symmetric_fade(sv))
            preset_row.addWidget(pb, 1)
            self.fade_preset_buttons[sec_val] = pb
        lay.addLayout(preset_row)

        # 淡入淡出曲线形状切换组（线性 / S 曲线 / 对数）
        curve_row = QHBoxLayout()
        curve_row.setSpacing(3)
        self._fade_curve_btns: dict[str, QPushButton] = {}
        for c_name, c_label in (("Linear", "线性"), ("S-Curve", "S曲线"), ("Log", "对数")):
            b = QPushButton(c_label)
            b.setObjectName("MiniCellBtn")
            b.setCheckable(True)
            b.setChecked(c_name == "S-Curve")
            b.clicked.connect(lambda checked=False, cn=c_name: self._on_fade_curve_selected(cn))
            curve_row.addWidget(b, 1)
            self._fade_curve_btns[c_name] = b
        lay.addLayout(curve_row)

        # 音量包络快捷控制条（顶部 1px 细黑线隔开）：启用 / 平滑曲线 / 重置
        env_sep = QFrame()
        env_sep.setFixedHeight(1)
        env_sep.setStyleSheet("background-color: #141416; border: none;")
        lay.addWidget(env_sep)

        env_row = QHBoxLayout()
        env_row.setSpacing(3)
        env_tag = QLabel("包络")
        env_tag.setStyleSheet(f"color: {ACCENT_VIOLET}; font-size: 10px; font-weight: 700;")
        env_tag.setToolTip("包络常驻启用；顶部工具栏「✎ 包络」(V) 切换编辑模式")

        self.btn_env_smooth = QPushButton("平滑")
        self.btn_env_smooth.setObjectName("MiniCellBtn")
        self.btn_env_smooth.setCheckable(True)
        self.btn_env_smooth.setChecked(True)
        self.btn_env_smooth.setToolTip("切换线性折线 / 自然样条过渡（Catmull-Rom，切线方向连续）")
        self.btn_env_smooth.toggled.connect(
            lambda on: self.canvas.setEnvelopeCurveMode("smooth" if on else "linear")
        )

        self.btn_env_reset = QPushButton("重置")
        self.btn_env_reset.setObjectName("MiniCellBtn")
        self.btn_env_reset.setToolTip("清除全部包络节点，恢复原始电平")
        self.btn_env_reset.clicked.connect(self.canvas.resetEnvelope)

        env_row.addWidget(env_tag)
        env_row.addWidget(self.btn_env_smooth, 1)
        env_row.addWidget(self.btn_env_reset, 1)
        lay.addLayout(env_row)
        return cell

    def _build_time_pitch_card(self) -> QFrame:
        """时间伸缩与音高微调网格单元：速率% ↔ 目标时长s 双向换算 + 保持原调 / 磁带变速双模式"""
        cell = QFrame()
        cell.setObjectName("GridCell")
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(8, 7, 8, 8)
        lay.setSpacing(6)

        # 1. 居中标题 + 保持原调开关
        hdr_row = QHBoxLayout()
        hdr = QLabel("TIME & PITCH")
        hdr.setObjectName("CardHeader")

        keep_lbl = QLabel("保调")
        keep_lbl.setStyleSheet("color: #9A9AA4; font-size: 9px; font-weight: 700;")
        keep_lbl.setToolTip("开启：变长/变速不改变音高；关闭：物理磁带变速联动变调")

        self.preserve_pitch_sw = PowerToggleSwitch(checked=True)
        self.preserve_pitch_sw.setToolTip("保持原调 (Preserve Pitch)")
        self.preserve_pitch_sw.toggled.connect(self._on_preserve_pitch_toggled)

        hdr_row.addStretch()
        hdr_row.addWidget(hdr)
        hdr_row.addStretch()
        hdr_row.addWidget(keep_lbl)
        hdr_row.addWidget(self.preserve_pitch_sw)
        lay.addLayout(hdr_row)

        # 2. 伸缩速率（% 旋钮 + 胶囊） & 目标时长（s 可编辑胶囊，对齐画面切点）
        r_time = QHBoxLayout()
        r_time.setSpacing(3)

        c_ratio = QVBoxLayout()
        c_ratio.setSpacing(2)
        l_ratio = QLabel("速率")
        l_ratio.setStyleSheet("color: #B4B4BE; font-size: 9px;")
        self.stretch_ratio_pill = EditableValuePill("100.0%", ACCENT_CYAN, width=42)
        self.stretch_ratio_pill.setToolTip("双击输入百分比 (50% ~ 200%，>100% 加快缩短)")
        self.stretch_ratio_pill.valueEdited.connect(self._on_stretch_ratio_edited)
        c_ratio.addWidget(l_ratio)
        c_ratio.addWidget(self.stretch_ratio_pill)

        c_dur = QVBoxLayout()
        c_dur.setSpacing(2)
        l_dur = QLabel("目标时长")
        l_dur.setStyleSheet("color: #9A9AA4; font-size: 9px;")
        self.target_dur_pill = EditableValuePill(f"{self._duration:.2f} s", ACCENT_CYAN, width=46)
        self.target_dur_pill.setToolTip("直接输入目标秒数以对齐画面切点，自动反算速率")
        self.target_dur_pill.valueEdited.connect(self._on_target_dur_edited)
        c_dur.addWidget(l_dur)
        c_dur.addWidget(self.target_dur_pill)

        self.stretch_knob = ProRotaryKnob(50.0, 200.0, 100.0, 100.0, ACCENT_CYAN, mode="linear")
        self.stretch_knob.valueChanged.connect(self._on_stretch_knob_changed)

        r_time.addLayout(c_ratio)
        r_time.addLayout(c_dur)
        r_time.addStretch()
        r_time.addWidget(self.stretch_knob)
        lay.addLayout(r_time)

        # 3. 音高微调（-12 ~ +12 半音双极旋钮）
        r_pitch = QHBoxLayout()
        c_pitch = QVBoxLayout()
        c_pitch.setSpacing(2)
        l_pitch = QLabel("音高 (半音)")
        l_pitch.setStyleSheet("color: #B4B4BE; font-size: 10px;")
        self.pitch_pill = EditableValuePill("0.0 st", ACCENT_VIOLET, width=58)
        self.pitch_pill.setToolTip("双击输入半音偏移 (-12.0 ~ +12.0 st，支持小数如 +0.5)")
        self.pitch_pill.valueEdited.connect(self._on_pitch_pill_edited)
        c_pitch.addWidget(l_pitch)
        c_pitch.addWidget(self.pitch_pill)

        self.pitch_knob = ProRotaryKnob(-12.0, 12.0, 0.0, 0.0, ACCENT_VIOLET, mode="bipolar")
        self.pitch_knob.valueChanged.connect(self._on_pitch_knob_changed)

        r_pitch.addLayout(c_pitch)
        r_pitch.addStretch()
        r_pitch.addWidget(self.pitch_knob)
        lay.addLayout(r_pitch)

        # 4. 算法模式 + 重置（同排并列）
        algo_row = QHBoxLayout()
        algo_row.setSpacing(2)
        self.stretch_algo_combo = QComboBox()
        self.stretch_algo_combo.addItems(["人声", "复音", "瞬态"])
        self.stretch_algo_combo.setToolTip("选择时间伸缩 DSP 算法内核")
        algo_row.addWidget(self.stretch_algo_combo, 1)

        self.time_pitch_reset_button = QPushButton("重置")
        self.time_pitch_reset_button.setObjectName("MiniCellBtn")
        self.time_pitch_reset_button.setToolTip("恢复 100% 速率与 0.0 st 音高")
        self.time_pitch_reset_button.clicked.connect(self._reset_time_pitch)
        algo_row.addWidget(self.time_pitch_reset_button)
        lay.addLayout(algo_row)

        # 5. 提交应用按钮
        self.time_pitch_action_button = apply_tp_btn = QPushButton("应用时间与音高")
        apply_tp_btn.setObjectName("MiniCellBtn")
        apply_tp_btn.setToolTip("将当前速率与音高设置写入选区（非破坏性，自动入处理栈）")
        apply_tp_btn.setStyleSheet(f"""
            QPushButton#MiniCellBtn {{
                background-color: #1A3038;
                color: {ACCENT_CYAN};
                border: none;
                height: 22px;
            }}
            QPushButton#MiniCellBtn:hover {{
                background-color: #23404A;
                color: #FFFFFF;
            }}
        """)
        apply_tp_btn.clicked.connect(self._commit_time_pitch)
        lay.addWidget(apply_tp_btn)
        return cell

    def _build_loudness_card(self) -> QFrame:
        """全宽响度匹配栏（两块式）：标题行右侧行业标准预设 + 下行 OLED 读数与目标/限幅/一键匹配"""
        card = QFrame()
        card.setObjectName("SideCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 7, 8, 8)
        lay.setSpacing(6)

        # 块 1：标题 + 行业标准预设右置
        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(4)
        hdr = QLabel("LUFS LOUDNESS MATCH")
        hdr.setObjectName("CardHeader")
        hdr_row.addWidget(hdr)
        hdr_row.addStretch()

        self._lufs_preset_btns: dict[float, QPushButton] = {}
        for lufs_val, lbl_txt, tip_txt in (
            (-14.0, "-14网络", "B站 / YouTube / 抖音流媒体标准 (-14 LUFS)"),
            (-16.0, "-16播客", "Apple Podcasts / 移动端标准 (-16 LUFS)"),
            (-23.0, "-23广电", "EBU R128 / 电视台广播标准 (-23 LUFS)"),
        ):
            b = QPushButton(lbl_txt)
            b.setObjectName("MiniCellBtn")
            b.setCheckable(True)
            b.setChecked(abs(lufs_val - self._target_lufs) < 0.1)
            b.setToolTip(tip_txt)
            b.clicked.connect(lambda checked=False, lv=lufs_val: self._set_target_lufs(lv))
            hdr_row.addWidget(b)
            self._lufs_preset_btns[lufs_val] = b
        lay.addLayout(hdr_row)

        # 块 2：OLED 双读数 + 目标/限幅胶囊（单位 LU / dB 区分，悬停看说明）
        cfg_row = QHBoxLayout()
        cfg_row.setSpacing(5)

        oled = QFrame()
        oled.setStyleSheet("QFrame { background-color: #151518; border: 1px solid #101012; border-radius: 3px; }")
        o_lay = QHBoxLayout(oled)
        o_lay.setContentsMargins(6, 2, 6, 2)
        o_lay.setSpacing(6)

        def _stat_col(title: str, init_txt: str, col: str) -> QLabel:
            c = QVBoxLayout()
            c.setSpacing(1)
            t = QLabel(title)
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setStyleSheet("color: #787882; font-size: 8px; font-weight: 700; border: none;")
            v = QLabel(init_txt)
            v.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.setStyleSheet(f"color: {col}; font-family: {MONO_FONT_CSS}; font-size: 9px; font-weight: 700; border: none;")
            c.addWidget(t)
            c.addWidget(v)
            o_lay.addLayout(c, 1)
            return v

        self.lufs_val_lbl = _stat_col("INTEGRATED", f"{self._current_lufs:.1f} LUFS", ACCENT_CYAN)
        v_div = QFrame()
        v_div.setFixedWidth(1)
        v_div.setStyleSheet("background-color: #242429; border: none;")
        o_lay.addWidget(v_div)
        self.true_peak_lbl = _stat_col("TRUE PEAK", f"{self._current_true_peak:.1f} dBTP", ACCENT_GREEN)
        cfg_row.addWidget(oled, 3)

        self.target_lufs_pill = EditableValuePill(f"{self._target_lufs:.1f} LU", ACCENT_RED, width=54)
        self.target_lufs_pill.setToolTip("目标响度 (LUFS)：双击输入自定义值 (-36.0 ~ -6.0)")
        self.target_lufs_pill.valueEdited.connect(self._on_target_lufs_edited)
        cfg_row.addWidget(self.target_lufs_pill)

        self.tp_ceil_pill = EditableValuePill(f"{self._true_peak_ceil:.1f} dB", ACCENT_AMBER, width=50)
        self.tp_ceil_pill.setToolTip("真峰值保护上限 (True Peak Ceiling，防削波失真)")
        self.tp_ceil_pill.valueEdited.connect(self._on_tp_ceil_edited)
        cfg_row.addWidget(self.tp_ceil_pill)
        lay.addLayout(cfg_row)

        # 一键匹配按钮独占一行（实时显示所需增益补偿值）
        self.btn_apply_lufs = QPushButton("匹配目标响度 (+5.4 dB)")
        self.btn_apply_lufs.setToolTip("一键计算并应用增益补偿，使选区/整段达到目标响度")
        self.btn_apply_lufs.setStyleSheet(f"""
            QPushButton {{
                background-color: #362226;
                color: {ACCENT_RED};
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 600;
                height: 21px;
                padding: 0px 6px;
            }}
            QPushButton:hover {{
                background-color: #452B30;
                color: #FFFFFF;
            }}
        """)
        self.btn_apply_lufs.clicked.connect(self._emit_lufs_normalize)
        lay.addWidget(self.btn_apply_lufs)

        self._refresh_lufs_ui()
        return card

    def _build_process_stack_card(self) -> QFrame:
        """全宽非破坏性处理栈机架列表：所有操作自动入栈，单步可旁通/删除，支持撤销/重做快照"""
        card = QFrame()
        card.setObjectName("SideCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 7, 8, 8)
        lay.setSpacing(6)

        # 1. 标题栏 + 激活计数徽标 + 清空按钮
        hdr_row = QHBoxLayout()
        hdr = QLabel("PROCESS STACK")
        hdr.setObjectName("CardHeader")

        self.stack_count_lbl = QLabel("0 步")
        self.stack_count_lbl.setStyleSheet(f"""
            background-color: #151518;
            color: {ACCENT_VIOLET};
            border: 1px solid #101012;
            border-radius: 3px;
            padding: 0px 5px;
            font-family: {MONO_FONT_CSS};
            font-size: 9px;
            font-weight: 700;
        """)

        self.btn_clear_stack = QPushButton("清空")
        self.btn_clear_stack.setObjectName("MiniCellBtn")
        self.btn_clear_stack.setToolTip("清除全部处理步骤并恢复原始音频")
        self.btn_clear_stack.clicked.connect(self._clear_process_stack)

        hdr_row.addWidget(hdr)
        hdr_row.addWidget(self.stack_count_lbl)
        hdr_row.addStretch()
        hdr_row.addWidget(self.btn_clear_stack)
        lay.addLayout(hdr_row)

        # 2. 动态步骤列表容器
        self.stack_list_layout = QVBoxLayout()
        self.stack_list_layout.setContentsMargins(0, 0, 0, 0)
        self.stack_list_layout.setSpacing(4)
        lay.addLayout(self.stack_list_layout)

        self._rebuild_process_stack_ui()
        return card

    def _apply_symmetric_fade(self, sec: float):
        v = min(self._duration * 0.45, sec)
        self.canvas._fade_in_sec = v
        self.canvas._fade_out_sec = v
        self.canvas.update()
        self._on_fade_changed(v, v)

    def _clear_selection_or_cancel_fade_curve(self) -> None:
        if self.canvas._drag_mode == "fade_curve":
            self.canvas.cancel_fade_drag()
        else:
            self.canvas.clearSelection()

    def _on_canvas_fade_curve_committed(self, _end: str, _curve: str) -> None:
        self.fade_preview.setState(self.canvas._fade_curve_type, self.canvas._fade_in_sec,
                                   self.canvas._fade_out_sec, out_curve=self.canvas._fade_out_curve_type)

    def _on_fade_curve_selected(self, curve_name: str):
        for k, btn in self._fade_curve_btns.items():
            btn.setChecked(k == curve_name)
        self.canvas.setFadeCurveType(curve_name)
        if hasattr(self, "fade_preview"):
            self.fade_preview.setState(curve_name, self.canvas._fade_in_sec, self.canvas._fade_out_sec)
        self.fadeCurveChanged.emit(curve_name)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self._toggle_play)
        QShortcut(QKeySequence("Ctrl+Space"), self, self._toggle_skip_play)
        QShortcut(QKeySequence("Ctrl+A"), self, self.canvas.selectAll)
        QShortcut(QKeySequence("Ctrl+Z"), self, self._undo_process_stack)
        QShortcut(QKeySequence("Ctrl+Y"), self, self._redo_process_stack)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._clear_selection_or_cancel_fade_curve)
        # Del / 退格键：有选中的包络节点时优先删除该节点，否则删除选区音频（原行为）
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, self._delete_selection_or_node)
        QShortcut(QKeySequence(Qt.Key.Key_Backspace), self, self._delete_selection_or_node)
        # 标记点 / 专业精修快捷键：一键修复 / 过零点吸附 / 缩放到选区 / 画笔与框选切换 / 原声对比
        QShortcut(QKeySequence(Qt.Key.Key_M), self, self.canvas.addMarker)
        QShortcut(QKeySequence(Qt.Key.Key_H), self, self._trigger_quick_heal)
        QShortcut(QKeySequence(Qt.Key.Key_Z), self, lambda: self.btn_snap_zero.toggle())
        QShortcut(QKeySequence(Qt.Key.Key_E), self, self._zoom_to_selection)
        QShortcut(QKeySequence(Qt.Key.Key_B), self, self._toggle_brush_tool)
        QShortcut(QKeySequence(Qt.Key.Key_A), self, lambda: self._set_tool_mode("select"))
        QShortcut(QKeySequence(Qt.Key.Key_V), self, lambda: self._set_tool_mode(
            "select" if self.canvas._tool_mode == "envelope" else "envelope"
        ))
        QShortcut(QKeySequence(Qt.Key.Key_Tab), self, lambda: self.btn_bypass.toggle())

    def _toggle_skip_play(self):
        """跳过选区试听 (Ctrl+Space)：预览删除选区后的上下文衔接效果"""
        t0, t1, _, _ = self.canvas.getSelection()
        if t1 - t0 <= 0.01:
            self._toggle_play()
            return
        self._skip_selection_mode = True
        self.update_playhead(max(0.0, t0 - 1.2))
        if not self._is_playing:
            self._toggle_play()

    # -----------------------------------------------------------------
    # 交互联动与信号分发
    # -----------------------------------------------------------------
    def _sync_viewport(self, start_sec: float, end_sec: float):
        self.canvas.setViewport(start_sec, end_sec)
        self.minimap.setViewport(self.canvas._view_start, self.canvas._view_end)

    def _toggle_play(self):
        self._is_playing = not self._is_playing
        self.btn_play.setChecked(self._is_playing)
        self.btn_play.setText("❚❚ 暂停" if self._is_playing else "▶ 播放")
        self.playStateChanged.emit(self._is_playing)

    def _stop_playback(self):
        self._is_playing = False
        self._skip_selection_mode = False
        self.btn_play.setChecked(False)
        self.btn_play.setText("▶ 播放")
        t0, _, _, _ = self.canvas.getSelection()
        self.update_playhead(t0)
        self.stopRequested.emit()

    def _toggle_loop(self, checked: bool):
        self._is_looping = checked
        self.loopToggled.emit(checked)

    def _on_spectral_toggled(self, checked: bool):
        self.canvas.setSpectralVisible(checked)

    def _set_tool_mode(self, mode: str):
        self.btn_tool_select.setChecked(mode == "select")
        self.btn_tool_envelope.setChecked(mode == "envelope")
        self.btn_tool_brush.setChecked(mode == "brush")
        self.canvas.setToolMode(mode)
        self.toolModeChanged.emit(mode)

    def _toggle_brush_tool(self):
        self._set_tool_mode("brush" if self.canvas._tool_mode != "brush" else "select")

    def _zoom_to_selection(self):
        t0, t1, _, _ = self.canvas.getSelection()
        if t1 - t0 > 0.005:
            pad = (t1 - t0) * 0.05
            self._sync_viewport(max(0.0, t0 - pad), min(self._duration, t1 + pad))

    def _trigger_quick_heal(self):
        t0, t1, fl, fh = self.canvas.getSelection()
        self.spectralRepairRequested.emit(t0, t1, fl, fh, "heal")
        self._push_process_step(
            "spectral", "频谱修复", f"{format_freq_short(fl)}-{format_freq_short(fh)} · {t0:.2f}s-{t1:.2f}s",
            ACCENT_RED, {"t0": t0, "t1": t1, "f_low": fl, "f_high": fh, "mode": "heal"}
        )

    def _on_canvas_playhead_moved(self, sec: float):
        self._playhead = sec
        self.minimap.setPlayhead(sec)
        self.tc_lbl.setText(format_timecode(sec))
        self.seekRequested.emit(sec)

    def _on_minimap_playhead_moved(self, sec: float):
        """概览条拖拽播放头：同步主画布播放头 + 时间码 + seek 指令"""
        self._playhead = sec
        self.canvas.setPlayhead(sec)
        self.tc_lbl.setText(format_timecode(sec))
        self.seekRequested.emit(sec)

    def _on_canvas_selection_changed(self, t0: float, t1: float, fl: float, fh: float):
        self.minimap.setSelection(t0, t1)
        if t1 > t0 >= 0.0:
            dur = t1 - t0
            self.sel_info_lbl.setText(
                f"选区: {format_timecode(t0)} - {format_timecode(t1)}  (时长 {dur:.3f}s | {format_freq_short(fl)} ~ {format_freq_short(fh)})"
            )
        else:
            self.sel_info_lbl.setText(f"选区: 全片段 (00:00.000 - {format_timecode(self._duration)})")
        self.selectionChanged.emit(t0, t1, fl, fh)
        self._refresh_target_duration_display()   # 框选区间变化 -> 目标时长与落点预览跟随刷新

    def _on_preview_gain_changed(self, gain_db: float):
        self.gain_knob.setValue(gain_db, emit=False)
        self.gain_pill.setText(f"{gain_db:+.1f} dB" if abs(gain_db) >= 0.05 else "0.0 dB")

    def _on_gain_knob_changed(self, gain_db: float):
        val = round(gain_db, 1)
        self.gain_pill.setText(f"{val:+.1f} dB" if abs(val) >= 0.05 else "0.0 dB")
        self.canvas.setPreviewGain(val)

    def _on_gain_pill_edited(self, text: str):
        try:
            val = max(-36.0, min(24.0, round(float(text), 1)))
            self.gain_knob.setValue(val, emit=True)
        except ValueError:
            pass

    def _on_edit_channel_selected(self, ch: int):
        for k, btn in self._edit_ch_btns.items():
            btn.setChecked(k == ch)
        self.canvas.setEditChannel(ch)

    def _on_gain_committed(self, t0: float, t1: float, gain_db: float):
        if abs(gain_db) < 0.05:
            return
        ch = self.canvas._edit_channel
        # 本地演示波形同步更新（接入真实后端后可由后端重新推送波形）
        self._apply_demo_gain_locally(t0, t1, gain_db, ch)
        self.gain_knob.setValue(0.0, emit=False)
        self.gain_pill.setText("0.0 dB")
        self.canvas.setPreviewGain(0.0)
        # 按目标声道路由指令：全部声道走 gainApplied，单声道走 channelGainApplied
        if ch == -1:
            self.gainApplied.emit(t0, t1, gain_db)
        else:
            self.channelGainApplied.emit(ch, t0, t1, gain_db)
        ch_tag = {-1: "L+R", 0: "L", 1: "R"}.get(ch, "L+R")
        self._push_process_step(
            "gain", f"选区增益 ({ch_tag})", f"{gain_db:+.1f}dB · {t0:.2f}s-{t1:.2f}s",
            ACCENT_AMBER, {"ch": ch, "t0": t0, "t1": t1, "gain_db": gain_db}
        )

    def _on_spec_boost_knob(self, val: float):
        v = round(val, 1)
        self.spec_boost_pill.setText(f"+{v:.0f} dB")
        self.canvas.setSpectralContrastBoost(v)

    def _on_spec_boost_text(self, text: str):
        try:
            v = max(0.0, min(36.0, float(text)))
            self.spec_boost_knob.setValue(v, emit=True)
        except ValueError:
            pass

    def _on_denoise_text(self, text: str):
        try:
            v = max(0.0, min(40.0, round(float(text), 1)))
            self.denoise_knob.setValue(v, emit=True)
        except ValueError:
            pass

    def _on_fade_changed(self, fi: float, fo: float):
        self.fi_pill.setText(f"{fi:.2f} s")
        self.fo_pill.setText(f"{fo:.2f} s")
        if hasattr(self, "fade_preview"):
            self.fade_preview.setState(self.canvas._fade_curve_type, fi, fo)
        self.fadeApplied.emit(fi, fo)

    def _on_fi_edited(self, text: str):
        try:
            fi = max(0.0, min(self._duration - self.canvas._fade_out_sec, float(text)))
            self.canvas._fade_in_sec = fi
            self.canvas.update()
            self._on_fade_changed(fi, self.canvas._fade_out_sec)
        except ValueError:
            pass

    def _on_fo_edited(self, text: str):
        try:
            fo = max(0.0, min(self._duration - self.canvas._fade_in_sec, float(text)))
            self.canvas._fade_out_sec = fo
            self.canvas.update()
            self._on_fade_changed(self.canvas._fade_in_sec, fo)
        except ValueError:
            pass

    # -----------------------------------------------------------------
    # 时间伸缩与音高微调交互逻辑（速率% ↔ 目标时长s 双向换算 + 磁带变速联动）
    # -----------------------------------------------------------------
    def _active_range_duration(self) -> float:
        t0, t1, _, _ = self.canvas.getSelection()
        if t1 > t0 >= 0.0:
            return max(0.01, t1 - t0)
        return max(0.01, self._duration)   # 无选区时按整段计算

    def _refresh_target_duration_display(self):
        speed = self.stretch_knob.value()
        ratio = max(10.0, speed) / 100.0
        target_sec = self._active_range_duration() / ratio
        self.target_dur_pill.setText(f"{target_sec:.2f} s")
        self.canvas.setStretchPreviewRatio(speed)

    def _on_preserve_pitch_toggled(self, preserve: bool):
        if not preserve:
            # 切换为物理磁带变速模式时，立即按当前速率同步音高
            ratio = self.stretch_knob.value() / 100.0
            coupled_st = max(-12.0, min(12.0, round(12.0 * math.log2(max(0.01, ratio)), 1)))
            self._updating_time_pitch = True
            self.pitch_knob.setValue(coupled_st, emit=False)
            self.pitch_pill.setText(f"{coupled_st:+.1f} st" if abs(coupled_st) >= 0.05 else "0.0 st")
            self._updating_time_pitch = False

    def _on_stretch_knob_changed(self, ratio_pct: float):
        if self._updating_time_pitch:
            return
        self._updating_time_pitch = True
        val = round(ratio_pct, 1)
        self.stretch_ratio_pill.setText(f"{val:.1f}%")
        self._refresh_target_duration_display()

        # 未勾选"保持原调"时，按物理变速比联动半音偏移
        if not self.preserve_pitch_sw.isChecked():
            coupled_st = max(-12.0, min(12.0, round(12.0 * math.log2(max(0.01, val / 100.0)), 1)))
            self.pitch_knob.setValue(coupled_st, emit=False)
            self.pitch_pill.setText(f"{coupled_st:+.1f} st" if abs(coupled_st) >= 0.05 else "0.0 st")
        self._updating_time_pitch = False

    def _on_stretch_ratio_edited(self, text: str):
        try:
            clean = text.replace("%", "").strip()
            val = max(50.0, min(200.0, round(float(clean), 1)))
            self.stretch_knob.setValue(val, emit=True)
        except ValueError:
            pass

    def _on_target_dur_edited(self, text: str):
        try:
            clean = text.replace("s", "").strip()
            target_sec = max(0.02, float(clean))
            src_dur = self._active_range_duration()
            new_ratio = max(50.0, min(200.0, round((src_dur / target_sec) * 100.0, 1)))
            self.stretch_knob.setValue(new_ratio, emit=True)
        except ValueError:
            self._refresh_target_duration_display()

    def _on_pitch_knob_changed(self, semitones: float):
        if self._updating_time_pitch:
            return
        self._updating_time_pitch = True
        st = round(semitones, 1)
        self.pitch_pill.setText(f"{st:+.1f} st" if abs(st) >= 0.05 else "0.0 st")

        # 磁带变速模式下，反向联动伸缩速率（音高升 -> 速率升 -> 片段缩短）
        if not self.preserve_pitch_sw.isChecked():
            coupled_ratio = max(50.0, min(200.0, round(100.0 * (2.0 ** (st / 12.0)), 1)))
            self.stretch_knob.setValue(coupled_ratio, emit=False)
            self.stretch_ratio_pill.setText(f"{coupled_ratio:.1f}%")
            self._refresh_target_duration_display()
        self._updating_time_pitch = False

    def _on_pitch_pill_edited(self, text: str):
        try:
            clean = text.replace("st", "").strip()
            val = max(-12.0, min(12.0, round(float(clean), 1)))
            self.pitch_knob.setValue(val, emit=True)
        except ValueError:
            pass

    def _reset_time_pitch(self):
        self._updating_time_pitch = True
        self.stretch_knob.setValue(100.0, emit=False)
        self.stretch_ratio_pill.setText("100.0%")
        self.pitch_knob.setValue(0.0, emit=False)
        self.pitch_pill.setText("0.0 st")
        self._updating_time_pitch = False
        self._refresh_target_duration_display()

    def _commit_time_pitch(self):
        ratio_pct = round(self.stretch_knob.value(), 1)
        pitch_st = round(self.pitch_knob.value(), 1)
        if abs(ratio_pct - 100.0) < 0.05 and abs(pitch_st) < 0.05:
            return
        t0, t1, _, _ = self.canvas.getSelection()
        if t1 <= t0 or t0 < 0.0:
            t0, t1 = 0.0, self._duration   # 无选区时作用于整段
        preserve = self.preserve_pitch_sw.isChecked()
        algo_map = {0: "formant", 1: "polyphonic", 2: "transient"}
        algo = algo_map.get(self.stretch_algo_combo.currentIndex(), "formant")

        # 本地演示波形重采样（接入真实后端后由后端返回重采样后的波形与新总时长）
        self._apply_demo_time_stretch_locally(t0, t1, ratio_pct)
        self.timePitchApplied.emit(t0, t1, ratio_pct, pitch_st, preserve, algo)
        self._push_process_step(
            "time_pitch", "时间/音高伸缩", f"{ratio_pct:.1f}% / {pitch_st:+.1f}st",
            ACCENT_CYAN, {"t0": t0, "t1": t1, "ratio_pct": ratio_pct, "pitch_st": pitch_st, "preserve": preserve, "algo": algo}
        )
        self._reset_time_pitch()

    def _apply_demo_time_stretch_locally(self, t0: float, t1: float, ratio_pct: float):
        """本地演示：对 [t0, t1] 区间波形做线性重采样并更新总时长"""
        if not self._demo_peaks_max or not self._demo_peaks_max[0] or abs(ratio_pct - 100.0) < 0.05:
            return
        scale = ratio_pct / 100.0
        old_bins = len(self._demo_peaks_max[0])
        i0 = max(0, min(old_bins - 1, int((t0 / self._duration) * old_bins)))
        i1 = max(i0 + 1, min(old_bins, int((t1 / self._duration) * old_bins)))
        seg_len = i1 - i0
        new_seg_len = max(4, int(round(seg_len / scale)))

        def _resample_seg(arr: list[float]) -> list[float]:
            seg = arr[i0:i1]
            res = []
            for k in range(new_seg_len):
                pos = (k / max(1, new_seg_len - 1)) * (len(seg) - 1)
                idx_a = int(pos)
                idx_b = min(len(seg) - 1, idx_a + 1)
                frac = pos - idx_a
                res.append(seg[idx_a] * (1.0 - frac) + seg[idx_b] * frac)
            return arr[:i0] + res + arr[i1:]

        for ch in range(len(self._demo_peaks_max)):
            self._demo_peaks_max[ch] = _resample_seg(self._demo_peaks_max[ch])
            self._demo_peaks_min[ch] = _resample_seg(self._demo_peaks_min[ch])
            self._demo_rms[ch] = _resample_seg(self._demo_rms[ch])

        new_bins = len(self._demo_peaks_max[0])
        self._duration = max(0.05, self._duration * (new_bins / old_bins))
        self.minimap.setAudioSummary(self._duration, self._demo_peaks_max[0])
        self.minimap.setViewport(0.0, self._duration)
        self.canvas.loadAudioData(
            self._sample_rate, self._duration,
            self._demo_peaks_min, self._demo_peaks_max, self._demo_rms, self._demo_spec
        )
        self.sel_info_lbl.setText(f"选区: 全片段 (00:00.000 - {format_timecode(self._duration)})  |  {self._sample_rate} Hz")
        # loadAudioData 会重置淡入淡出与包络，同步右侧控件显示避免数值失真
        self.fi_pill.setText("0.00 s")
        self.fo_pill.setText("0.00 s")
        self.fade_preview.setState(self.canvas._fade_curve_type, 0.0, 0.0)

    # -----------------------------------------------------------------
    # LUFS 集成响度匹配逻辑（演示读数；接入后端后由 BS.1770 实测回填）
    # -----------------------------------------------------------------
    def _set_target_lufs(self, target: float):
        self._target_lufs = max(-36.0, min(-6.0, round(target, 1)))
        for k, btn in self._lufs_preset_btns.items():
            btn.setChecked(abs(k - self._target_lufs) < 0.1)
        self.target_lufs_pill.setText(f"{self._target_lufs:.1f} LU")
        self._refresh_lufs_ui()

    def _on_target_lufs_edited(self, text: str):
        try:
            val = float(text.replace("LUFS", "").replace("LU", "").strip())
            self._set_target_lufs(val)
        except ValueError:
            pass

    def _on_tp_ceil_edited(self, text: str):
        try:
            val = max(-6.0, min(0.0, round(float(text.replace("dBTP", "").replace("dB", "").strip()), 1)))
            self._true_peak_ceil = val
            self.tp_ceil_pill.setText(f"{self._true_peak_ceil:.1f} dB")
            self._refresh_lufs_ui()
        except ValueError:
            pass

    def _refresh_lufs_ui(self):
        self.lufs_val_lbl.setText(f"{self._current_lufs:.1f} LUFS")
        self.true_peak_lbl.setText(f"{self._current_true_peak:.1f} dBTP")
        tp_col = ACCENT_RED if self._current_true_peak > self._true_peak_ceil else ACCENT_GREEN
        self.true_peak_lbl.setStyleSheet(f"color: {tp_col}; font-family: {MONO_FONT_CSS}; font-size: 11px; font-weight: 700; border: none;")

        delta_db = round(self._target_lufs - self._current_lufs, 1)
        if hasattr(self, "btn_apply_lufs"):
            self.btn_apply_lufs.setText(f"匹配目标响度 ({delta_db:+.1f} dB)")

    def _emit_lufs_normalize(self):
        t0, t1, _, _ = self.canvas.getSelection()
        if t1 <= t0 or t0 < 0.0:
            t0, t1 = 0.0, self._duration   # 无选区时作用于整段
        delta_db = round(self._target_lufs - self._current_lufs, 1)
        # 演示模式：按响度差值更新波形与当前 LUFS / True Peak 读数（真峰值受天花板限幅保护）
        self._apply_demo_gain_locally(t0, t1, delta_db)
        self._current_lufs = self._target_lufs
        self._current_true_peak = min(self._true_peak_ceil, round(self._current_true_peak + delta_db, 1))
        self._refresh_lufs_ui()

        self._push_process_step(
            step_type="loudness",
            label="LUFS 响度匹配",
            detail=f"{self._target_lufs:.1f} LUFS ({delta_db:+.1f}dB)",
            color=ACCENT_RED,
            params={"t0": t0, "t1": t1, "target_lufs": self._target_lufs, "true_peak_ceil": self._true_peak_ceil},
        )
        self.lufsNormalizeRequested.emit(t0, t1, self._target_lufs, self._true_peak_ceil)

    # -----------------------------------------------------------------
    # 非破坏性处理栈：增删改、旁通、撤销/重做快照同步
    # -----------------------------------------------------------------
    def _push_process_step(self, step_type: str, label: str, detail: str, color: str, params: dict):
        self._stack_undo_history.append(copy.deepcopy(self._process_steps))
        self._stack_redo_history.clear()

        self._step_seq += 1
        self._process_steps.append({
            "id": f"proc_{self._step_seq}",
            "type": step_type,
            "label": label,
            "detail": detail,
            "color": color,
            "enabled": True,
            "params": params,
        })
        self._rebuild_process_stack_ui()
        self.processStackChanged.emit(copy.deepcopy(self._process_steps))

    def _on_process_step_toggled(self, step_id: str, enabled: bool):
        self._stack_undo_history.append(copy.deepcopy(self._process_steps))
        self._stack_redo_history.clear()
        for s in self._process_steps:
            if s["id"] == step_id:
                s["enabled"] = enabled
                break
        self._rebuild_process_stack_ui()
        self.processStepToggled.emit(step_id, enabled)
        self.processStackChanged.emit(copy.deepcopy(self._process_steps))

    def _on_process_step_removed(self, step_id: str):
        self._stack_undo_history.append(copy.deepcopy(self._process_steps))
        self._stack_redo_history.clear()
        self._process_steps = [s for s in self._process_steps if s["id"] != step_id]
        self._rebuild_process_stack_ui()
        self.processStepRemoved.emit(step_id)
        self.processStackChanged.emit(copy.deepcopy(self._process_steps))

    def _clear_process_stack(self):
        if not self._process_steps:
            return
        self._stack_undo_history.append(copy.deepcopy(self._process_steps))
        self._stack_redo_history.clear()
        self._process_steps.clear()
        self._rebuild_process_stack_ui()
        self.processStackChanged.emit([])

    def _undo_process_stack(self):
        if self._stack_undo_history:
            self._stack_redo_history.append(copy.deepcopy(self._process_steps))
            self._process_steps = self._stack_undo_history.pop()
            self._rebuild_process_stack_ui()
            self.processStackChanged.emit(copy.deepcopy(self._process_steps))
        self.undoRequested.emit()

    def _redo_process_stack(self):
        if self._stack_redo_history:
            self._stack_undo_history.append(copy.deepcopy(self._process_steps))
            self._process_steps = self._stack_redo_history.pop()
            self._rebuild_process_stack_ui()
            self.processStackChanged.emit(copy.deepcopy(self._process_steps))
        self.redoRequested.emit()

    def _rebuild_process_stack_ui(self):
        while self.stack_list_layout.count():
            item = self.stack_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        total = len(self._process_steps)
        active_cnt = sum(1 for s in self._process_steps if s.get("enabled", True))
        self.stack_count_lbl.setText(f"{active_cnt}/{total} 步" if total else "0 步")
        self.btn_clear_stack.setEnabled(total > 0)

        if not self._process_steps:
            empty = QLabel("暂无非破坏性处理步骤\n执行增益、降噪、响度或修复后自动入栈")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("""
                background-color: #151518;
                color: #6E6E76;
                border: 1px dashed #2A2A2E;
                border-radius: 3px;
                padding: 10px 6px;
                font-size: 9px;
            """)
            self.stack_list_layout.addWidget(empty)
            return

        global_bypass = self.btn_bypass.isChecked()
        for idx, step in enumerate(self._process_steps, start=1):
            step_view = dict(step, index=idx)
            row = ProcessStepRow(step_view, global_bypassed=global_bypass)
            row.toggled.connect(self._on_process_step_toggled)
            row.removed.connect(self._on_process_step_removed)
            self.stack_list_layout.addWidget(row)

    def _emit_silence(self):
        t0, t1, _, _ = self.canvas.getSelection()
        self._apply_demo_gain_locally(t0, t1, -96.0)
        self.silenceRequested.emit(t0, t1)

    def _emit_trim(self):
        t0, t1, _, _ = self.canvas.getSelection()
        self.trimRequested.emit(t0, t1)

    def _delete_selection_or_node(self):
        """Del / 退格键路由：优先删除选中的包络节点，无选中时删除选区音频"""
        if self.canvas.deleteSelectedEnvelopeNode():
            return
        self._emit_delete()

    def _emit_delete(self):
        t0, t1, _, _ = self.canvas.getSelection()
        self.deleteRangeRequested.emit(t0, t1)

    def _emit_normalize(self):
        t0, t1, _, _ = self.canvas.getSelection()
        self.normalizeRequested.emit(t0, t1, -1.0)

    def _emit_capture_noise(self):
        t0, t1, _, _ = self.canvas.getSelection()
        self._has_noise_print = True
        # 用动态属性切换样式（配合 QSS 的 [captured="true"] 规则），
        # 避免硬编码 setStyleSheet 覆盖掉按钮原有的渐变与 hover 效果
        self.btn_noise_print.setProperty("captured", True)
        self.btn_noise_print.style().unpolish(self.btn_noise_print)
        self.btn_noise_print.style().polish(self.btn_noise_print)
        self.noise_status_lbl.setText("● 已锁定")
        self.noise_status_lbl.setToolTip(f"已锁定噪声特征 ({t1 - t0:.2f}s)，降噪将按此特征谱减")
        self.noise_status_lbl.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 9px; font-weight: 700;")
        self.noisePrintCaptured.emit(t0, t1)

    def _emit_denoise(self):
        t0, t1, _, _ = self.canvas.getSelection()
        red_db = round(self.denoise_knob.value(), 1)
        self._push_process_step(
            "denoise", "谱减法降噪", f"-{red_db:.1f}dB · {t0:.2f}s-{t1:.2f}s",
            ACCENT_GREEN, {"t0": t0, "t1": t1, "reduction_db": red_db}
        )
        self.denoiseRequested.emit(t0, t1, self.denoise_knob.value())

    def _on_spectral_action(self, action: str, t0: float, t1: float, fl: float, fh: float):
        if action == "silence":
            self._emit_silence()
        elif action == "trim":
            self._emit_trim()
        elif action == "delete":
            self._emit_delete()
        else:
            self.spectralRepairRequested.emit(t0, t1, fl, fh, action)
            self._push_process_step(
                "spectral", "频谱修复", f"{format_freq_short(fl)}-{format_freq_short(fh)} · {t0:.2f}s-{t1:.2f}s",
                ACCENT_RED, {"t0": t0, "t1": t1, "f_low": fl, "f_high": fh, "mode": action}
            )

    # -----------------------------------------------------------------
    # 内置仿真音频波形与频谱生成（便于独立运行调试交互）
    # -----------------------------------------------------------------
    def _init_demo_audio(self):
        rng = random.Random(108)
        bins = 1600
        dur = 8.5
        p_max_l, p_min_l, rms_l = [], [], []
        p_max_r, p_min_r, rms_r = [], [], []

        # 模拟 5 段人声台词 + 停顿呼吸 + 轻微低频底噪
        syllables = [
            (0.6, 1.8, 0.72),
            (2.2, 3.5, 0.85),
            (4.1, 4.9, 0.62),
            (5.3, 6.9, 0.90),
            (7.2, 8.0, 0.55),
        ]

        for i in range(bins):
            t = (i / bins) * dur
            env = 0.025 + rng.uniform(0.0, 0.012)  # 轻微底噪本底
            for s0, s1, amp in syllables:
                if s0 <= t <= s1:
                    u = (t - s0) / (s1 - s0)
                    mod = math.sin(math.pi * u) ** 0.65
                    syllable_pulse = 0.55 + 0.45 * abs(math.sin(t * 19.0))
                    env = max(env, amp * mod * syllable_pulse * rng.uniform(0.75, 1.05))

            p_max_l.append(min(0.98, env))
            p_min_l.append(-min(0.98, env * rng.uniform(0.88, 1.0)))
            rms_l.append(env * 0.56)

            env_r = env * rng.uniform(0.88, 1.04)
            p_max_r.append(min(0.98, env_r))
            p_min_r.append(-min(0.98, env_r * rng.uniform(0.88, 1.0)))
            rms_r.append(env_r * 0.56)

        # 生成 240 x 96 语图矩阵：包含 60Hz 低频交流声横线 + 人声谐波峰 + 气音宽频带
        t_bins, f_bins = 240, 96
        spec: list[list[float]] = []
        for tx in range(t_bins):
            t = (tx / t_bins) * dur
            speech_energy = 0.0
            for s0, s1, amp in syllables:
                if s0 <= t <= s1:
                    u = (t - s0) / (s1 - s0)
                    speech_energy = amp * (math.sin(math.pi * u) ** 0.6)
            col: list[float] = []
            for fy in range(f_bins):
                f_norm = fy / f_bins
                # 60Hz 低频持续电源嗡嗡声（在 f_norm ≈ 0.08 处形成一条连续细亮线）
                hum = 0.42 * math.exp(-((f_norm - 0.08) ** 2) / 0.0004)
                # 人声基频与共振峰
                formant = speech_energy * (
                    0.82 * math.exp(-((f_norm - 0.24) ** 2) / 0.012)
                    + 0.65 * math.exp(-((f_norm - 0.48) ** 2) / 0.018)
                    + 0.35 * math.exp(-((f_norm - 0.72) ** 2) / 0.025)
                )
                noise = rng.uniform(0.04, 0.14) + speech_energy * rng.uniform(0.0, 0.12)
                col.append(max(0.0, min(1.0, hum + formant + noise)))
            spec.append(col)

        self._demo_peaks_min = [p_min_l, p_min_r]
        self._demo_peaks_max = [p_max_l, p_max_r]
        self._demo_rms = [rms_l, rms_r]
        self._demo_spec = spec

        self.load_audio_data(
            sample_rate=48000,
            duration_sec=dur,
            peaks_min=self._demo_peaks_min,
            peaks_max=self._demo_peaks_max,
            rms=self._demo_rms,
            spectrogram_2d=self._demo_spec,
            clip_name=self._clip_name,
        )

    def _apply_demo_gain_locally(self, t0: float, t1: float, gain_db: float, ch: int = -1):
        """ch=-1 作用于全部声道，否则仅目标声道（本地演示；接入后端后由后端处理）"""
        if not self._demo_peaks_max or not self._demo_peaks_max[0]:
            return
        scale = 10.0 ** (gain_db / 20.0)
        bins = len(self._demo_peaks_max[0])
        target_chs = range(len(self._demo_peaks_max)) if ch == -1 else [min(ch, len(self._demo_peaks_max) - 1)]
        for i in range(bins):
            t = (i / bins) * self._duration
            if t0 <= t <= t1:
                for ch_i in target_chs:
                    self._demo_peaks_max[ch_i][i] = min(1.0, self._demo_peaks_max[ch_i][i] * scale)
                    self._demo_peaks_min[ch_i][i] = max(-1.0, self._demo_peaks_min[ch_i][i] * scale)
                    self._demo_rms[ch_i][i] = min(1.0, self._demo_rms[ch_i][i] * scale)
        self.canvas._peaks_max = self._demo_peaks_max
        self.canvas._peaks_min = self._demo_peaks_min
        self.canvas._rms = self._demo_rms
        self.canvas._cache_wave_channels()
        self.canvas._invalidate_cache()
        self.minimap.setAudioSummary(self._duration, self._demo_peaks_max[0])

    def _tick_demo_playback(self):
        if not self._is_playing:
            self._skip_selection_mode = False
            self.output_meter.setLevels(-60.0, -60.0)
            return

        t0, t1, _, _ = self.canvas.getSelection()
        self._playhead += 0.03

        if self._skip_selection_mode:
            if t0 <= self._playhead < t1:
                self._playhead = t1
            elif self._playhead >= min(self._duration, t1 + 1.2):
                self._skip_selection_mode = False
                self._toggle_play()
                return
        elif self._playhead >= t1:
            if self._is_looping:
                self._playhead = t0
            else:
                self._playhead = t0
                self._toggle_play()
                return

        # 播放头超出右视口时自动平滑翻页跟随
        span = self.canvas._view_end - self.canvas._view_start
        if span < self._duration and self._playhead > self.canvas._view_end:
            ns = min(self._duration - span, self._playhead - span * 0.15)
            self._sync_viewport(ns, ns + span)

        self.update_playhead(self._playhead)
        # 根据当前播放头处的波形幅度驱动右侧输出电平表（静音声道置 -60dB）
        if self._demo_peaks_max and self._demo_peaks_max[0]:
            idx = min(len(self._demo_peaks_max[0]) - 1, int((self._playhead / self._duration) * len(self._demo_peaks_max[0])))
            amp_l = 0.0 if self.canvas._ch_muted[0] else max(1e-4, self._demo_peaks_max[0][idx])
            amp_r = 0.0 if self.canvas._ch_muted[-1] else max(1e-4, self._demo_peaks_max[-1][idx])
            self.output_meter.setLevels(
                -60.0 if amp_l <= 0 else 20.0 * math.log10(amp_l),
                -60.0 if amp_r <= 0 else 20.0 * math.log10(amp_r),
            )

    # -----------------------------------------------------------------
    # 外部后端对接公共 API
    # -----------------------------------------------------------------
    def load_audio_data(
        self,
        sample_rate: int,
        duration_sec: float,
        peaks_min: list[list[float]],
        peaks_max: list[list[float]],
        rms: list[list[float]],
        spectrogram_2d: list[list[float]] | None = None,
        clip_name: str | None = None,
        cleanliness_report: dict | None = None,
        *,
        preserve_edit_view: bool = False,
    ):
        """
        供后端 DSP 引擎加载真实音频可视化缓存：
        - peaks_min / peaks_max / rms: shape 为 [channels][num_bins]，取值范围 [-1.0, 1.0]
        - spectrogram_2d: shape 为 [time_bins][freq_bins]，归一化能量 [0.0, 1.0]
        """
        if clip_name:
            self._clip_name = clip_name
            self.setWindowTitle(f"Audio Clip Editor — {clip_name}")
        self._sample_rate = sample_rate
        self._duration = max(0.01, duration_sec)
        self.minimap.setAudioSummary(self._duration, peaks_max[0] if peaks_max else [])
        self.canvas.loadAudioData(
            sample_rate, self._duration, peaks_min, peaks_max, rms, spectrogram_2d,
            preserve_edit_view=preserve_edit_view)
        if not preserve_edit_view:
            self._playhead = 0.0
            self.minimap.setViewport(0.0, self._duration)
            self.sel_info_lbl.setText(f"选区: 全片段 (00:00.000 - {format_timecode(self._duration)})  |  {sample_rate} Hz")
        self._refresh_target_duration_display()   # 新片段时长 -> 目标时长胶囊跟随刷新

        if cleanliness_report:
            peak_db = cleanliness_report.get("peak_db", -2.4)
            rms_db = cleanliness_report.get("rms_db", -18.6)
            noise_db = cleanliness_report.get("noise_floor_db", -58.2)
            self.peak_val_lbl.setText(f"{peak_db:+.1f}dB")
            self.rms_val_lbl.setText(f"{rms_db:+.1f}dB")
            self.noise_val_lbl.setText(f"{noise_db:+.1f}dB")
            # 后端实测 BS.1770 响度回填（可选键）：刷新 LUFS OLED 读数与匹配按钮补偿值
            if "integrated_lufs" in cleanliness_report:
                self._current_lufs = float(cleanliness_report["integrated_lufs"])
            if "true_peak_dbtp" in cleanliness_report:
                self._current_true_peak = float(cleanliness_report["true_peak_dbtp"])
            if "integrated_lufs" in cleanliness_report or "true_peak_dbtp" in cleanliness_report:
                self._refresh_lufs_ui()

    def update_playhead(self, time_sec: float):
        self._playhead = max(0.0, min(self._duration, time_sec))
        self.tc_lbl.setText(format_timecode(self._playhead))
        self.minimap.setPlayhead(self._playhead)
        self.canvas.setPlayhead(self._playhead)

    def update_output_meter(self, left_db: float, right_db: float):
        self.output_meter.setLevels(left_db, right_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    editor = StudioAudioClipEditor("Interview_Main_Track_01.wav")
    editor.show()
    sys.exit(app.exec())
