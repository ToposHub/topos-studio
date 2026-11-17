from __future__ import annotations

import sys
import math
import copy
import random
from collections import deque
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QFrame, QLineEdit, QSizePolicy,
    QInputDialog, QMenu
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont, QPolygonF
)


# =====================================================================
# 1. 全局色彩系统与声学常量（与 EQ-UI / Panner-UI 保持统一视觉规范）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

PALETTE = {
    "cyan":   "#28D8F0",  # 主声场宽度 / 李萨如外围包络
    "green":  "#4CD490",  # Mid (M) 通道 / 正相位相干区
    "amber":  "#F0A848",  # Side (S) 通道 / 输出修剪
    "violet": "#B67CF8",  # Mono-Maker 低频单声道化
    "coral":  "#F26D6D",  # 反相警告 / Mono Sum 检查
    "gold":   "#E8B838",  # Solo M/S 独奏监听高亮
}

MONO_MAKER_MIN = 20.0
MONO_MAKER_MAX = 1000.0


def format_freq_hz(hz: float) -> str:
    if hz >= 1000.0:
        return f"{hz / 1000.0:.2f}K"
    return f"{int(round(hz))}Hz"


def parse_freq_hz(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("HZ", "").strip()
    try:
        if s.endswith("K"):
            val = float(s[:-1]) * 1000.0
        else:
            val = float(s)
        return max(MONO_MAKER_MIN, min(MONO_MAKER_MAX, val))
    except ValueError:
        return fallback


def default_imager_state() -> dict:
    return {
        "enabled": True,
        "width": 100.0,               # 0.0% (Mono) ~ 100.0% (Original) ~ 200.0% (Super Wide)
        "mid_gain": 0.0,              # -12.0 ~ +12.0 dB
        "side_gain": 0.0,             # -12.0 ~ +12.0 dB
        "mono_maker_on": True,        # 低频单声道化开关
        "mono_maker_freq": 120.0,     # 20.0 ~ 1000.0 Hz
        "mono_maker_slope": "24dB",   # "12dB" | "24dB" | "Ellip"
        "solo_mode": "None",          # "None" | "Mid" | "Side" | "Mono"
        "output_gain": 0.0,           # -12.0 ~ +12.0 dB
        "scope_mode": "Lissajous",    # "Lissajous" | "Polar"
    }


# =====================================================================
# 2. 基础控件：现代发光电源开关（复用 EQ-UI / Panner-UI 风格）
# =====================================================================
class PowerToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True, accent: str = "#28D8F0", parent=None):
        super().__init__(parent)
        self._checked = checked
        self._accent = QColor(accent)
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
        border = self._accent if self._checked else QColor("#36363D")
        p.setPen(QPen(border, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 8.5, 8.5)

        r = 5.2
        cy = self.height() / 2.0
        cx = (self.width() - 8.8) if self._checked else 8.8
        knob_color = self._accent if self._checked else QColor("#666670")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob_color)
        p.drawEllipse(QPointF(cx, cy), r, r)


# =====================================================================
# 3. 基础控件：内嵌可编辑数字胶囊
# =====================================================================
class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "100%", accent_color: str = "#28D8F0", width: int = 62, parent=None):
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

    def setControlActive(self, active: bool):
        self._active = active
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            clean = self._text.replace("+", "").replace("%", "").replace("dB", "").replace("Hz", "").strip()
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
# 4. 基础控件：拉丝阳极铝质感旋钮（支持 linear / bipolar / log）
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
        diameter: int = 38,
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

        self.setFixedSize(diameter, diameter)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setValue(self, val: float, emit: bool = False):
        clamped = max(self._min, min(self._max, val))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def setControlActive(self, active: bool):
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

        arc_r = (self.width() * 0.5) - 3.2
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深黑轨道底槽
        p.setPen(QPen(QColor("#141417"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性模式 12 点钟基准刻度
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 2. 主题色数值弧
        arc_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(QPen(arc_col, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._mode == "bipolar":
            zero_deg = 90.0
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        # 3. 哑光浅灰旋钮帽
        knob_r = arc_r - 3.8
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
# 5. 核心示波器：李萨如 / 极坐标矢量声场仪 + 相位相关度表
# =====================================================================
class StereoVectorscopeWidget(QWidget):
    widthDragged = pyqtSignal(float)        # 直接在声场画布左右拖拽调节 Width %
    scopeModeChanged = pyqtSignal(str)      # "Lissajous" | "Polar"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(290)
        self.setMouseTracking(True)

        self._state = default_imager_state()
        self._correlation = 0.86
        self._corr_avg = 0.84
        self._trail_history: deque[list[tuple[float, float]]] = deque(maxlen=6)

        self._dragging = False
        self._drag_start_x = 0.0
        self._drag_start_width = 100.0
        self._btn_liss_rect = QRectF()
        self._btn_polar_rect = QRectF()

    def setState(self, state: dict):
        self._state = copy.deepcopy(state)
        self.update()

    def pushSamples(self, samples_l: list[float], samples_r: list[float], correlation: float):
        pairs = list(zip(samples_l, samples_r))
        self._trail_history.append(pairs)
        self._correlation = max(-1.0, min(1.0, correlation))
        self._corr_avg = self._corr_avg * 0.88 + self._correlation * 0.12
        self.update()

    # -----------------------------------------------------------------
    # 鼠标交互：画布内左右拖拽 / 滚轮直接调节声场宽度 %
    # -----------------------------------------------------------------
    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_liss_rect.contains(pos):
                self.scopeModeChanged.emit("Lissajous")
                return
            if self._btn_polar_rect.contains(pos):
                self.scopeModeChanged.emit("Polar")
                return

            self._dragging = True
            self._drag_start_x = pos.x()
            self._drag_start_width = self._state["width"]
            self.setCursor(Qt.CursorShape.SizeHorCursor)

    def mouseMoveEvent(self, event):
        if self._dragging:
            dx = event.position().x() - self._drag_start_x
            sens = 0.15 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.55
            new_w = max(0.0, min(200.0, round(self._drag_start_width + dx * sens, 1)))
            self.widthDragged.emit(new_w)
        else:
            pos = event.position()
            if self._btn_liss_rect.contains(pos) or self._btn_polar_rect.contains(pos):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeHorCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.CursorShape.SizeHorCursor)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.widthDragged.emit(100.0)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        step = 1.0 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 5.0
        new_w = max(0.0, min(200.0, round(self._state["width"] + delta * step, 1)))
        self.widthDragged.emit(new_w)
        event.accept()

    # -----------------------------------------------------------------
    # 绘制：Goniometer 坐标系 + 声场余辉轨迹 + 底部相位相关度表
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = float(self.width()), float(self.height())
        # 纯平 #1D1D20 中性深灰底（对齐 EQ-UI / Panner-UI 画布）
        p.fillRect(QRectF(0, 0, w, h), QColor("#1D1D20"))

        corr_bar_h = 44.0
        scope_area = QRectF(12.0, 10.0, w - 24.0, max(120.0, h - corr_bar_h - 16.0))
        corr_area = QRectF(16.0, scope_area.bottom() + 6.0, w - 32.0, corr_bar_h - 8.0)

        self._paint_goniometer(p, scope_area)
        self._paint_correlation_meter(p, corr_area)

    def _paint_goniometer(self, p: QPainter, area: QRectF):
        en = self._state["enabled"]
        is_polar = (self._state["scope_mode"] == "Polar")

        # 1. 示波器主体网格与安全立体声扇区
        cx = area.center().x()
        cy = area.bottom() - 18.0 if is_polar else area.center().y()
        radius = min(area.width() * 0.44, (area.height() - 26.0) if is_polar else (area.height() * 0.45))
        radius = max(40.0, radius)

        # 安全立体声扇形区（L -45° 至 R +45° 之间微弱青绿底色）
        wedge = QPainterPath()
        wedge.moveTo(cx, cy)
        wedge_rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        if is_polar:
            wedge.arcTo(wedge_rect, 45.0, 90.0)
        else:
            wedge.arcTo(wedge_rect, 45.0, 90.0)
            wedge.moveTo(cx, cy)
            wedge.arcTo(wedge_rect, 225.0, 90.0)
        wedge.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(40, 216, 240, 12 if en else 5))
        p.drawPath(wedge)

        # 同心圆刻度环 (0.33, 0.66, 1.0)
        grid_pen = QPen(QColor("#26262A"), 1.0)
        outer_pen = QPen(QColor("#34343B"), 1.1)
        for frac in (0.35, 0.70, 1.0):
            r = radius * frac
            p.setPen(outer_pen if frac == 1.0 else grid_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            if is_polar:
                p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 0, 180 * 16)
            else:
                p.drawEllipse(QPointF(cx, cy), r, r)

        # M (垂直中心) 与 S (水平) 主轴 + L / R (±45°) 对角轴
        axis_pen = QPen(QColor("#3A3A42"), 1.1)
        diag_pen = QPen(QColor("#2E343D"), 1.0, Qt.PenStyle.DashLine)

        if is_polar:
            p.setPen(axis_pen)
            p.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy - radius))
            d_off = radius * math.cos(math.radians(45.0))
            p.setPen(diag_pen)
            p.drawLine(QPointF(cx, cy), QPointF(cx - d_off, cy - d_off))
            p.drawLine(QPointF(cx, cy), QPointF(cx + d_off, cy - d_off))
        else:
            p.setPen(axis_pen)
            p.drawLine(QPointF(cx, cy - radius), QPointF(cx, cy + radius))
            p.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))
            d_off = radius * math.cos(math.radians(45.0))
            p.setPen(diag_pen)
            p.drawLine(QPointF(cx - d_off, cy - d_off), QPointF(cx + d_off, cy + d_off))
            p.drawLine(QPointF(cx - d_off, cy + d_off), QPointF(cx + d_off, cy - d_off))

        # 方位坐标字母 (+M, -S, +S, L, R)
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor("#8E8E98"))
        d_lbl = radius * math.cos(math.radians(45.0)) + 11.0
        p.drawText(QRectF(cx - 16, cy - radius - 16, 32, 14), Qt.AlignmentFlag.AlignCenter, "+M")
        p.drawText(QRectF(cx - d_lbl - 10, cy - d_lbl - 8, 20, 14), Qt.AlignmentFlag.AlignCenter, "L")
        p.drawText(QRectF(cx + d_lbl - 10, cy - d_lbl - 8, 20, 14), Qt.AlignmentFlag.AlignCenter, "R")
        p.drawText(QRectF(cx - radius - 22, cy - 7, 20, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "-S")
        p.drawText(QRectF(cx + radius + 2, cy - 7, 20, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "+S")

        # 2. 绘制实时李萨如 / 极坐标余辉点云
        solo = self._state["solo_mode"]
        width_scale = (self._state["width"] / 100.0) if en else 1.0
        m_gain_lin = (10.0 ** (self._state["mid_gain"] / 20.0)) if en else 1.0
        s_gain_lin = (10.0 ** (self._state["side_gain"] / 20.0)) if en else 1.0

        num_frames = len(self._trail_history)
        for f_idx, frame_pairs in enumerate(self._trail_history):
            if not frame_pairs:
                continue
            age = (f_idx + 1) / max(1, num_frames)  # 0..1, 最新帧为 1.0
            is_latest = (f_idx == num_frames - 1)

            poly = QPolygonF()
            for l_raw, r_raw in frame_pairs:
                # 实时预览用户调节的 Width、M/S Gain 与 Solo 状态
                m_val = 0.5 * (l_raw + r_raw) * m_gain_lin
                s_val = 0.5 * (l_raw - r_raw) * width_scale * s_gain_lin

                if solo == "Mid" or solo == "Mono":
                    s_val = 0.0
                elif solo == "Side":
                    m_val = 0.0

                # 李萨如标准坐标：X = S (左右侧差), Y = M (垂直和声)
                if is_polar:
                    # 极坐标折叠至上半平面
                    angle = math.atan2(s_val, abs(m_val) + 1e-6)
                    mag = min(1.0, math.hypot(m_val, s_val))
                    px = cx + math.sin(angle) * mag * radius
                    py = cy - math.cos(angle) * mag * radius
                else:
                    px = cx + max(-1.05, min(1.05, s_val)) * radius
                    py = cy - max(-1.05, min(1.05, m_val)) * radius

                poly.append(QPointF(px, py))

            if en:
                if solo == "Mid":
                    stroke_col = QColor(76, 212, 144, int(190 * (age ** 1.6)))
                elif solo == "Side":
                    stroke_col = QColor(240, 168, 72, int(190 * (age ** 1.6)))
                else:
                    stroke_col = QColor(40, 216, 240, int(205 * (age ** 1.7)))
            else:
                stroke_col = QColor(120, 124, 134, int(130 * (age ** 1.5)))

            p.setPen(QPen(stroke_col, 1.55 if is_latest else 1.05))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolyline(poly)

            # 最新帧绘制微光散点增强精密示波器质感
            if is_latest and en:
                dot_col = QColor("#8CF0FF") if solo not in ("Mid", "Side") else stroke_col
                p.setPen(QPen(dot_col, 2.2))
                for pt in poly[::2]:
                    p.drawPoint(pt)

        # 3. Mono-Maker 激活时，在中心 M 轴绘制低频单声道锁定指示脊线
        if en and self._state["mono_maker_on"] and self._state["solo_mode"] != "Side":
            mm_norm = math.log10(self._state["mono_maker_freq"] / MONO_MAKER_MIN) / math.log10(MONO_MAKER_MAX / MONO_MAKER_MIN)
            spine_len = radius * (0.22 + 0.45 * mm_norm)
            p.setPen(QPen(QColor(182, 124, 248, 175), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            if is_polar:
                p.drawLine(QPointF(cx, cy), QPointF(cx, cy - spine_len))
            else:
                p.drawLine(QPointF(cx, cy - spine_len * 0.7), QPointF(cx, cy + spine_len * 0.7))

        # 4. 左上角实时声场状态 HUD 胶囊（与 EQ-UI / Panner-UI 一致）
        mm_str = f"Mono<{format_freq_hz(self._state['mono_maker_freq'])}" if self._state["mono_maker_on"] else "Mono:Off"
        solo_tag = f"  •  SOLO {solo.upper()}" if solo != "None" else ""
        hud_str = f"Width {self._state['width']:.0f}%  •  {mm_str}{solo_tag}"
        hud_w = 248 if solo != "None" else 188
        hud_rect = QRectF(area.left() + 4, area.top() + 2, hud_w, 19)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor("#28D8F0") if en else QColor("#8A8A92"))
        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        # 5. 右上角示波器视图切换胶囊 (LISS / POLAR)
        self._btn_liss_rect = QRectF(area.right() - 88, area.top() + 2, 40, 18)
        self._btn_polar_rect = QRectF(area.right() - 44, area.top() + 2, 42, 18)
        for rect, label, active in (
            (self._btn_liss_rect, "LISS", self._state["scope_mode"] == "Lissajous"),
            (self._btn_polar_rect, "POLAR", self._state["scope_mode"] == "Polar"),
        ):
            p.setPen(QPen(QColor("#24788C") if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 46, 56, 210) if active else QColor(21, 21, 24, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#787882"))
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_correlation_meter(self, p: QPainter, area: QRectF):
        """绘制母带级相位相关度表 (-1 反相 ← 0 宽广 → +1 同相)"""
        # 标题与数值读数
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor("#9A9AA2"))
        p.drawText(QRectF(area.left(), area.top(), 140, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "PHASE CORRELATION")

        corr_val = self._correlation
        if corr_val < 0.0:
            val_col = QColor(PALETTE["coral"])
        elif corr_val < 0.35:
            val_col = QColor(PALETTE["amber"])
        else:
            val_col = QColor(PALETTE["green"])

        p.setPen(val_col)
        p.drawText(QRectF(area.right() - 70, area.top(), 70, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{corr_val:+.2f}")

        # 轨道凹槽
        bar_rect = QRectF(area.left(), area.top() + 16.0, area.width(), 8.0)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#111114"))
        p.drawRoundedRect(bar_rect, 2.5, 2.5)

        mid_x = bar_rect.center().x()
        norm = (corr_val + 1.0) * 0.5  # 0.0 (-1) ~ 0.5 (0) ~ 1.0 (+1)
        curr_x = bar_rect.left() + norm * bar_rect.width()

        # 填充色带（从 0 点向左右延伸）
        if abs(curr_x - mid_x) > 1.0:
            fill_left = min(mid_x, curr_x)
            fill_w = abs(curr_x - mid_x)
            fill_rect = QRectF(fill_left, bar_rect.top() + 1.0, fill_w, bar_rect.height() - 2.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(val_col)
            p.drawRoundedRect(fill_rect, 1.5, 1.5)

        # 0 中心刻度线
        p.setPen(QPen(QColor("#52525B"), 1.2))
        p.drawLine(QPointF(mid_x, bar_rect.top() - 1.5), QPointF(mid_x, bar_rect.bottom() + 1.5))

        # 平均值游标 + 瞬时白针
        avg_x = bar_rect.left() + ((self._corr_avg + 1.0) * 0.5) * bar_rect.width()
        p.setPen(QPen(QColor("#9A9AA2"), 1.2))
        p.drawLine(QPointF(avg_x, bar_rect.top()), QPointF(avg_x, bar_rect.bottom()))

        p.setPen(QPen(QColor("#FFFFFF"), 1.8))
        p.drawLine(QPointF(curr_x, bar_rect.top() - 1.5), QPointF(curr_x, bar_rect.bottom() + 1.5))

        # 底部刻度标签 (-1, 0, +1)
        p.setFont(QFont(MONO_FONT_FAM, 7))
        p.setPen(QColor("#787882"))
        tick_y = bar_rect.bottom() + 1.0
        p.drawText(QRectF(bar_rect.left(), tick_y, 24, 11), Qt.AlignmentFlag.AlignLeft, "-1")
        p.drawText(QRectF(mid_x - 12, tick_y, 24, 11), Qt.AlignmentFlag.AlignCenter, "0")
        p.drawText(QRectF(bar_rect.right() - 24, tick_y, 24, 11), Qt.AlignmentFlag.AlignRight, "+1")


# =====================================================================
# 6. 右侧监控列：Mid (M) & Side (S) 双通道电平表 + Solo M / Solo S / Mono
# =====================================================================
class MSMeterColumnWidget(QFrame):
    soloModeChanged = pyqtSignal(str)  # "None" | "Mid" | "Side" | "Mono"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MeterCol")
        self.setFixedWidth(92)
        self.setStyleSheet("QFrame#MeterCol { background-color: #222226; border-left: 1px solid #141416; }")

        self._mid_db = -18.0
        self._side_db = -24.0
        self._mid_peak = -14.0
        self._side_peak = -20.0
        self._mid_clip = False
        self._side_clip = False
        self._solo_mode = "None"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(7, 7, 7, 7)
        lay.setSpacing(5)

        # 顶部独奏监听按钮组：Solo M / Solo S / Mono Check
        hdr = QLabel("M / S MONITOR")
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr.setStyleSheet("color: #787882; font-size: 8px; font-weight: 700; letter-spacing: 0.6px;")
        lay.addWidget(hdr)

        solo_row = QHBoxLayout()
        solo_row.setSpacing(3)

        self.btn_solo_m = QPushButton("M")
        self.btn_solo_m.setCheckable(True)
        self.btn_solo_m.setFixedHeight(22)
        self.btn_solo_m.setToolTip("Solo Mid: 仅监听中心单声道和声 (L+R)")
        self.btn_solo_m.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_solo_m.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238; color: #CFCFD6; border: none;
                border-radius: 3px; font-size: 10px; font-weight: 700;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #162E24; color: {PALETTE["green"]}; border: 1px solid {PALETTE["green"]};
            }}
        """)
        self.btn_solo_m.clicked.connect(lambda: self._toggle_solo("Mid"))

        self.btn_solo_s = QPushButton("S")
        self.btn_solo_s.setCheckable(True)
        self.btn_solo_s.setFixedHeight(22)
        self.btn_solo_s.setToolTip("Solo Side: 仅监听立体声侧边差值 (L-R)")
        self.btn_solo_s.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_solo_s.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238; color: #CFCFD6; border: none;
                border-radius: 3px; font-size: 10px; font-weight: 700;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #2E2312; color: {PALETTE["amber"]}; border: 1px solid {PALETTE["amber"]};
            }}
        """)
        self.btn_solo_s.clicked.connect(lambda: self._toggle_solo("Side"))

        solo_row.addWidget(self.btn_solo_m, 1)
        solo_row.addWidget(self.btn_solo_s, 1)
        lay.addLayout(solo_row)

        self.btn_mono_chk = QPushButton("MONO CHK")
        self.btn_mono_chk.setCheckable(True)
        self.btn_mono_chk.setFixedHeight(20)
        self.btn_mono_chk.setToolTip("单声道折叠相位检查 (Mono Compatibility Check)")
        self.btn_mono_chk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mono_chk.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238; color: #CFCFD6; border: none;
                border-radius: 3px; font-size: 9px; font-weight: 700;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #2E1618; color: {PALETTE["coral"]}; border: 1px solid {PALETTE["coral"]};
            }}
        """)
        self.btn_mono_chk.clicked.connect(lambda: self._toggle_solo("Mono"))
        lay.addWidget(self.btn_mono_chk)

        # 电平表绘制区占位
        self.meter_canvas = _DualMSMeterCanvas()
        lay.addWidget(self.meter_canvas, 1)

    def _toggle_solo(self, mode: str):
        new_mode = "None" if self._solo_mode == mode else mode
        self.setSoloMode(new_mode)
        self.soloModeChanged.emit(new_mode)

    def setSoloMode(self, mode: str):
        self._solo_mode = mode
        self.btn_solo_m.setChecked(mode == "Mid")
        self.btn_solo_s.setChecked(mode == "Side")
        self.btn_mono_chk.setChecked(mode == "Mono")
        self.meter_canvas.setSoloMode(mode)

    def setLevels(self, mid_db: float, side_db: float):
        self.meter_canvas.setLevels(mid_db, side_db)


class _DualMSMeterCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._mid_db = -18.0
        self._side_db = -24.0
        self._mid_peak = -15.0
        self._side_peak = -21.0
        self._mid_clip = False
        self._side_clip = False
        self._solo_mode = "None"
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def setSoloMode(self, mode: str):
        self._solo_mode = mode
        self.update()

    def setLevels(self, mid_db: float, side_db: float):
        self._mid_db = max(-60.0, min(0.0, mid_db))
        self._side_db = max(-60.0, min(0.0, side_db))
        self._mid_peak = max(self._mid_db, self._mid_peak - 0.35)
        self._side_peak = max(self._side_db, self._side_peak - 0.35)
        if self._mid_db >= -0.1:
            self._mid_clip = True
        if self._side_db >= -0.1:
            self._side_clip = True
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._mid_clip = False
            self._side_clip = False
            self._mid_peak = self._mid_db
            self._side_peak = self._side_db
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = float(self.width()), float(self.height())
        top_y = 14.0
        bot_y = h - 22.0
        bar_h = max(40.0, bot_y - top_y)

        # 左侧 Mid 条 & 右侧 Side 条位置
        m_rect = QRectF(10.0, top_y, 12.0, bar_h)
        s_rect = QRectF(w - 22.0, top_y, 12.0, bar_h)

        # 顶部 M / S 标识
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor(PALETTE["green"]) if self._solo_mode != "Side" else QColor("#55555C"))
        p.drawText(QRectF(m_rect.left() - 4, 0, m_rect.width() + 8, 12), Qt.AlignmentFlag.AlignCenter, "M")

        p.setPen(QColor(PALETTE["amber"]) if self._solo_mode not in ("Mid", "Mono") else QColor("#55555C"))
        p.drawText(QRectF(s_rect.left() - 4, 0, s_rect.width() + 8, 12), Qt.AlignmentFlag.AlignCenter, "S")

        # 中间 dB 刻度
        p.setFont(QFont(MONO_FONT_FAM, 7))
        for db_tick in (0, -6, -12, -24, -36, -48):
            norm = (db_tick + 60.0) / 60.0
            ty = bot_y - norm * bar_h
            p.setPen(QColor("#6E6E76"))
            p.drawText(QRectF(m_rect.right() + 1, ty - 6, s_rect.left() - m_rect.right() - 2, 12), Qt.AlignmentFlag.AlignCenter, str(abs(db_tick)))

        # 绘制双通道电平柱
        channels = [
            (m_rect, self._mid_db, self._mid_peak, self._mid_clip, PALETTE["green"], self._solo_mode == "Side"),
            (s_rect, self._side_db, self._side_peak, self._side_clip, PALETTE["amber"], self._solo_mode in ("Mid", "Mono")),
        ]
        for rect, db_val, peak_val, is_clip, accent_hex, is_muted in channels:
            # 凹槽
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor("#0E0E11"))
            p.drawRoundedRect(rect, 2.0, 2.0)

            # 削波灯
            clip_r = QRectF(rect.left(), rect.top() - 4.5, rect.width(), 3.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FF4848") if is_clip else QColor("#18181C"))
            p.drawRoundedRect(clip_r, 1.0, 1.0)

            if not is_muted:
                norm = max(0.0, min(1.0, (db_val + 60.0) / 60.0))
                if norm > 0.01:
                    fh = bar_h * norm
                    f_rect = QRectF(rect.left() + 1.0, rect.bottom() - fh, rect.width() - 2.0, fh)
                    grad = QLinearGradient(rect.bottomLeft(), rect.topLeft())
                    grad.setColorAt(0.0, QColor(accent_hex))
                    grad.setColorAt(0.78, QColor("#28D8F0"))
                    grad.setColorAt(1.0, QColor("#F05050"))
                    p.setBrush(grad)
                    p.drawRoundedRect(f_rect, 1.2, 1.2)

                # 峰值保持横线
                p_norm = max(0.0, min(1.0, (peak_val + 60.0) / 60.0))
                py = rect.bottom() - bar_h * p_norm
                p.setPen(QPen(QColor("#ECECF0"), 1.2))
                p.drawLine(QPointF(rect.left() + 1.0, py), QPointF(rect.right() - 1.0, py))

            # 底部实时 dB 数值
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.setPen(QColor("#ECECF0") if not is_muted else QColor("#55555C"))
            txt = "MUTE" if is_muted else f"{db_val:.0f}"
            p.drawText(QRectF(rect.left() - 6, bot_y + 4, rect.width() + 12, 14), Qt.AlignmentFlag.AlignCenter, txt)


# =====================================================================
# 7. 底部模块单元基类（对齐 EQ-UI BandCard 网格单元格风格）
# =====================================================================
class ImagerModuleCell(QFrame):
    def __init__(self, accent_color: str, parent=None):
        super().__init__(parent)
        self._accent = accent_color
        self._enabled = True
        self._section_refs: list[QWidget] = []
        self.setObjectName("ImagerCell")
        self._apply_style()

    def setCellActive(self, active: bool):
        self._enabled = active
        self._apply_style()

    def _apply_style(self):
        # 彩色标识条移至顶边（对齐 multiband_comp_ui BandCard 彩顶边风格）：
        # 模块激活时 2px 主题色顶边，未激活时回落为 1px #141416 黑色分隔线
        top_bar = f"2px solid {self._accent}" if self._enabled else "1px solid #141416"
        self.setStyleSheet(f"""
            QFrame#ImagerCell {{
                background-color: #27272B;
                border: none;
                border-top: {top_bar};
                border-right: 1px solid #141416;
                border-radius: 0px;
            }}
            QLabel#ParamTitle {{
                color: {"#A6A6B0" if self._enabled else "#6E6E76"};
                font-size: 10px;
                font-weight: 700;
            }}
            QLabel#RangeHint {{
                color: {"#787882" if self._enabled else "#55555C"};
                font-size: 9px;
            }}
            QPushButton#SegBtn {{
                background-color: #323238;
                color: {"#CFCFD6" if self._enabled else "#8A8A92"};
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
                color: {self._accent if self._enabled else "#787D8C"};
                border: 1px solid {self._accent if self._enabled else "#3A3A40"};
            }}
        """)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#141416"), 1.0))
        for sec in self._section_refs:
            if sec.isVisible():
                y = sec.y() + 0.5
                p.drawLine(QPointF(0.0, y), QPointF(float(self.width()), y))


# =====================================================================
# 8. 主面板：母带级立体声声场处理器 (Studio Stereo Imager)
# =====================================================================
class StudioStereoImagerWidget(QWidget):
    # 对接后端音频引擎的完整信号接口
    imagerBypassChanged = pyqtSignal(bool)
    widthChanged = pyqtSignal(float)                   # 0.0 ~ 200.0 (%)
    midGainChanged = pyqtSignal(float)                 # -12.0 ~ +12.0 (dB)
    sideGainChanged = pyqtSignal(float)                # -12.0 ~ +12.0 (dB)
    monoMakerChanged = pyqtSignal(bool, float, str)    # enabled, freq_hz, slope
    soloModeChanged = pyqtSignal(str)                  # "None" | "Mid" | "Side" | "Mono"
    outputGainChanged = pyqtSignal(float)              # -12.0 ~ +12.0 (dB)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Master Bus", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Stereo Imager (M/S) — {track_name}")
        self.resize(880, 640)
        self.setMinimumSize(800, 580)

        self._updating = False
        self._active_bank = "A"
        self._banks = {
            "A": default_imager_state(),
            "B": {
                **default_imager_state(),
                "width": 135.0,
                "side_gain": 1.5,
                "mono_maker_on": True,
                "mono_maker_freq": 140.0,
            },
        }

        self._presets = {
            "Mastering Default (100%)": default_imager_state(),
            "Tight Bass + Wide Air": {
                **default_imager_state(),
                "width": 128.0, "side_gain": 1.2, "mono_maker_on": True, "mono_maker_freq": 135.0, "mono_maker_slope": "24dB",
            },
            "Club Mono Low-End (<160Hz)": {
                **default_imager_state(),
                "width": 112.0, "mid_gain": 0.5, "mono_maker_on": True, "mono_maker_freq": 160.0, "mono_maker_slope": "Ellip",
            },
            "Vocal Focus (Narrow Sides)": {
                **default_imager_state(),
                "width": 78.0, "mid_gain": 1.0, "side_gain": -1.5, "mono_maker_on": True, "mono_maker_freq": 100.0,
            },
            "Full Mono Fold-Down": {
                **default_imager_state(),
                "width": 0.0, "mono_maker_on": False,
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

        # 内置演示信号定时器（接入真实音频引擎 update_metering 后自动停止）
        self._demo_phase = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_signal)
        self._demo_timer.start(40)

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            StereoVectorscopeWidget, _DualMSMeterCanvas {{
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

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -------------------------------------------------------------
        # 1. 顶部整合工具栏（电源 + 轨道名 + 预设 + A/B 对比 + Reset）
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(38)
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb = QHBoxLayout(top_bar)
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True, PALETTE["cyan"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb.addWidget(self.power_switch)

        title_lbl = QLabel(f"STEREO IMAGER (M/S)  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb.addWidget(title_lbl)

        tb.addSpacing(10)

        # 预设管理
        add_btn = QPushButton("+")
        add_btn.setObjectName("ToolBtn")
        add_btn.setFixedSize(22, 22)
        add_btn.setToolTip("保存为新声场预设")
        add_btn.clicked.connect(self._save_new_preset)
        tb.addWidget(add_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedWidth(185)
        self.preset_combo.addItems(list(self._presets.keys()))
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        tb.addWidget(self.preset_combo)

        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setFixedSize(21, 22)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb.addWidget(prev_btn)

        next_btn = QPushButton("›")
        next_btn.setObjectName("ToolBtn")
        next_btn.setFixedSize(21, 22)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb.addWidget(next_btn)

        tb.addSpacing(10)

        # A / B 对比槽
        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(24, 22)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton("→")
        self.btn_copy_ab.setFixedSize(24, 22)
        self.btn_copy_ab.setToolTip("复制当前参数到另一对比槽位")
        self.btn_copy_ab.clicked.connect(self._copy_bank)

        self.btn_b = QPushButton("B")
        self.btn_b.setFixedSize(24, 22)
        self.btn_b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_b.clicked.connect(lambda: self._switch_bank("B"))

        tb.addWidget(self.btn_a)
        tb.addWidget(self.btn_copy_ab)
        tb.addWidget(self.btn_b)
        # 箭头按钮样式由 _update_ab_styles 统一设置（勿在入布局前绑定 objectName QSS，
        # 否则 Qt6 基线对齐分组会把它排到行底，相对 A/B 下沉 2px）

        tb.addStretch()

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_imager)
        tb.addWidget(reset_btn)

        root.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部主图形区：李萨如/极坐标示波器 + 相位相关度表 + 右侧 M/S 监控列
        # -------------------------------------------------------------
        stage_row = QHBoxLayout()
        stage_row.setContentsMargins(0, 0, 0, 0)
        stage_row.setSpacing(0)

        self.vectorscope = StereoVectorscopeWidget()
        self.vectorscope.widthDragged.connect(self._on_width_changed)
        self.vectorscope.scopeModeChanged.connect(self._on_scope_mode_changed)
        stage_row.addWidget(self.vectorscope, 1)

        self.ms_monitor = MSMeterColumnWidget()
        self.ms_monitor.soloModeChanged.connect(self._on_solo_mode_changed)
        stage_row.addWidget(self.ms_monitor)

        root.addLayout(stage_row, 5)

        # -------------------------------------------------------------
        # 3. 底部 4 模块参数控制区（网格单元贴合，1px 黑线切分）
        # -------------------------------------------------------------
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(0)

        # === 模块 1: STEREO WIDTH (M/S 宽度收放) ===
        self.cell_width = ImagerModuleCell(PALETTE["cyan"])
        w_lay = QVBoxLayout(self.cell_width)
        w_lay.setContentsMargins(10, 6, 10, 6)
        w_lay.setSpacing(4)

        w_hdr = QLabel("STEREO WIDTH")
        w_hdr.setFixedHeight(22)
        w_hdr.setStyleSheet(f"color: {PALETTE['cyan']}; font-size: 11px; font-weight: 700; letter-spacing: 0.4px;")
        w_lay.addWidget(w_hdr)

        self.knob_width = ProRotaryKnob(0.0, 200.0, 100.0, 100.0, PALETTE["cyan"], mode="bipolar", diameter=38)
        self.knob_width.valueChanged.connect(self._on_width_changed)
        self.pill_width = EditableValuePill("100%", PALETTE["cyan"], width=64)
        self.pill_width.valueEdited.connect(self._on_width_text)

        w_lay.addStretch(1)
        w_lay.addLayout(self._make_knob_cell("Width %", self.knob_width, self.pill_width, ("0%", "100%", "200%")))
        w_lay.addStretch(1)

        # 快捷宽度预设按钮
        w_presets_row = QHBoxLayout()
        w_presets_row.setSpacing(3)
        self.width_preset_btns: dict[float, QPushButton] = {}
        for w_val, label in ((0.0, "Mono"), (100.0, "100%"), (140.0, "140%")):
            btn = QPushButton(label)
            btn.setObjectName("SegBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, v=w_val: self._on_width_changed(v))
            w_presets_row.addWidget(btn)
            self.width_preset_btns[w_val] = btn
        w_lay.addLayout(w_presets_row)
        bottom_row.addWidget(self.cell_width, 1)

        # === 模块 2: MID / SIDE TRIM (独立 M/S 增益修剪) ===
        self.cell_ms = ImagerModuleCell(PALETTE["green"])
        ms_lay = QVBoxLayout(self.cell_ms)
        ms_lay.setContentsMargins(10, 6, 10, 6)
        ms_lay.setSpacing(4)

        ms_hdr = QLabel("MID / SIDE GAIN")
        ms_hdr.setFixedHeight(22)
        ms_hdr.setStyleSheet(f"color: {PALETTE['green']}; font-size: 11px; font-weight: 700; letter-spacing: 0.4px;")
        ms_lay.addWidget(ms_hdr)

        self.knob_mid = ProRotaryKnob(-12.0, 12.0, 0.0, 0.0, PALETTE["green"], mode="bipolar", diameter=38)
        self.knob_mid.valueChanged.connect(self._on_mid_gain_changed)
        self.pill_mid = EditableValuePill("0.0dB", PALETTE["green"], width=62)
        self.pill_mid.valueEdited.connect(self._on_mid_gain_text)

        self.knob_side = ProRotaryKnob(-12.0, 12.0, 0.0, 0.0, PALETTE["amber"], mode="bipolar", diameter=38)
        self.knob_side.valueChanged.connect(self._on_side_gain_changed)
        self.pill_side = EditableValuePill("0.0dB", PALETTE["amber"], width=62)
        self.pill_side.valueEdited.connect(self._on_side_gain_text)

        ms_ctrl = QHBoxLayout()
        ms_ctrl.setContentsMargins(0, 0, 0, 0)
        ms_ctrl.setSpacing(0)
        ms_ctrl.addLayout(self._make_knob_cell("Mid (M)", self.knob_mid, self.pill_mid, ("-12", "0", "+12")), 1)
        ms_ctrl.addLayout(self._make_knob_cell("Side (S)", self.knob_side, self.pill_side, ("-12", "0", "+12")), 1)
        ms_lay.addStretch(1)
        ms_lay.addLayout(ms_ctrl)
        ms_lay.addStretch(1)

        btn_unity_ms = QPushButton("Unity M/S (0 dB)")
        btn_unity_ms.setObjectName("SegBtn")
        btn_unity_ms.setFixedHeight(20)
        btn_unity_ms.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_unity_ms.clicked.connect(self._reset_ms_gains)
        ms_lay.addWidget(btn_unity_ms)
        bottom_row.addWidget(self.cell_ms, 1)

        # === 模块 3: MONO-MAKER (低频单声道化) ===
        self.cell_mm = ImagerModuleCell(PALETTE["violet"])
        mm_lay = QVBoxLayout(self.cell_mm)
        mm_lay.setContentsMargins(10, 6, 10, 6)
        mm_lay.setSpacing(4)

        self.btn_mm_toggle = QPushButton("MONO-MAKER")
        self.btn_mm_toggle.setCheckable(True)
        self.btn_mm_toggle.setFixedHeight(22)
        self.btn_mm_toggle.setToolTip("将设定截止频率以下的低频折叠为纯单声道，增强低音结像与黑胶/俱乐部兼容性")
        self.btn_mm_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mm_toggle.clicked.connect(self._on_mm_toggled)
        mm_lay.addWidget(self.btn_mm_toggle)

        self.knob_mm_freq = ProRotaryKnob(MONO_MAKER_MIN, MONO_MAKER_MAX, 120.0, 120.0, PALETTE["violet"], mode="log", diameter=38)
        self.knob_mm_freq.valueChanged.connect(self._on_mm_freq_changed)
        self.pill_mm_freq = EditableValuePill("120Hz", PALETTE["violet"], width=64)
        self.pill_mm_freq.valueEdited.connect(self._on_mm_freq_text)

        mm_lay.addStretch(1)
        mm_lay.addLayout(self._make_knob_cell("Cutoff Freq", self.knob_mm_freq, self.pill_mm_freq, ("20", "120", "1K")))
        mm_lay.addStretch(1)

        # 滤波斜率 / 椭圆滤波器切换
        mm_slope_row = QHBoxLayout()
        mm_slope_row.setSpacing(3)
        self.mm_slope_btns: dict[str, QPushButton] = {}
        for sl in ("12dB", "24dB", "Ellip"):
            btn = QPushButton(sl)
            btn.setObjectName("SegBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, s=sl: self._on_mm_slope_changed(s))
            mm_slope_row.addWidget(btn)
            self.mm_slope_btns[sl] = btn
        mm_lay.addLayout(mm_slope_row)
        bottom_row.addWidget(self.cell_mm, 1)

        # === 模块 4: SOLO M/S & OUTPUT TRIM ===
        self.cell_out = ImagerModuleCell(PALETTE["amber"])
        out_lay = QVBoxLayout(self.cell_out)
        out_lay.setContentsMargins(10, 6, 10, 6)
        out_lay.setSpacing(4)

        out_hdr = QLabel("SOLO & OUTPUT")
        out_hdr.setFixedHeight(22)
        out_hdr.setStyleSheet(f"color: {PALETTE['amber']}; font-size: 11px; font-weight: 700; letter-spacing: 0.4px;")
        out_lay.addWidget(out_hdr)

        self.knob_out = ProRotaryKnob(-12.0, 12.0, 0.0, 0.0, PALETTE["amber"], mode="bipolar", diameter=38)
        self.knob_out.valueChanged.connect(self._on_out_gain_changed)
        self.pill_out = EditableValuePill("0.0dB", PALETTE["amber"], width=62)
        self.pill_out.valueEdited.connect(self._on_out_gain_text)

        out_lay.addStretch(1)
        out_lay.addLayout(self._make_knob_cell("Output Trim", self.knob_out, self.pill_out, ("-12", "0dB", "+12")))
        out_lay.addStretch(1)

        # 底部同步 Solo M / Solo S 快捷按钮行
        solo_bot_row = QHBoxLayout()
        solo_bot_row.setSpacing(3)
        self.btn_bot_solo_m = QPushButton("Solo M")
        self.btn_bot_solo_m.setCheckable(True)
        self.btn_bot_solo_m.setFixedHeight(20)
        self.btn_bot_solo_m.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_bot_solo_m.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238; color: #CFCFD6; border: none;
                border-radius: 3px; font-size: 10px; font-weight: 700;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #162E24; color: {PALETTE["green"]}; border: 1px solid {PALETTE["green"]};
            }}
        """)
        self.btn_bot_solo_m.clicked.connect(lambda: self.ms_monitor._toggle_solo("Mid"))

        self.btn_bot_solo_s = QPushButton("Solo S")
        self.btn_bot_solo_s.setCheckable(True)
        self.btn_bot_solo_s.setFixedHeight(20)
        self.btn_bot_solo_s.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_bot_solo_s.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238; color: #CFCFD6; border: none;
                border-radius: 3px; font-size: 10px; font-weight: 700;
            }}
            QPushButton:hover {{ background-color: #3C3C44; color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #2E2312; color: {PALETTE["amber"]}; border: 1px solid {PALETTE["amber"]};
            }}
        """)
        self.btn_bot_solo_s.clicked.connect(lambda: self.ms_monitor._toggle_solo("Side"))

        solo_bot_row.addWidget(self.btn_bot_solo_m, 1)
        solo_bot_row.addWidget(self.btn_bot_solo_s, 1)
        out_lay.addLayout(solo_bot_row)
        bottom_row.addWidget(self.cell_out, 1)

        # 参数面板按内容自适应高度（消除大面积空白），富余空间全部让给示波器舞台
        root.addLayout(bottom_row, 0)

    def _make_hint_row(self, left_t: str, mid_t: str, right_t: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        for idx, t in enumerate((left_t, mid_t, right_t)):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            if idx == 1:
                row.addStretch()
            row.addWidget(lbl)
            if idx == 1:
                row.addStretch()
        return row

    def _make_knob_cell(
        self,
        title: str,
        knob: ProRotaryKnob,
        pill: EditableValuePill,
        hints: tuple[str, str, str] | None = None,
    ) -> QHBoxLayout:
        """紧凑旋钮参数列：标题 / 旋钮 / 范围提示 / 数值胶囊垂直堆叠并水平居中
        （对齐 multiband_comp_ui KnobParameterCell 的节奏，四个模块共用同一基线）"""
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        lbl = QLabel(title)
        lbl.setObjectName("ParamTitle")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(lbl)
        col.addWidget(knob, 0, Qt.AlignmentFlag.AlignCenter)
        if hints:
            col.addLayout(self._make_hint_row(*hints))
        col.addWidget(pill, 0, Qt.AlignmentFlag.AlignCenter)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addStretch(1)
        row.addLayout(col)
        row.addStretch(1)
        return row

    # -----------------------------------------------------------------
    # 状态管理与全视图同步
    # -----------------------------------------------------------------
    def _st(self) -> dict:
        return self._banks[self._active_bank]

    def _update_ab_styles(self):
        active_css = f"""
            QPushButton {{
                background-color: #182E38;
                color: {PALETTE["cyan"]};
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

    def _refresh_all_views(self):
        self._updating = True
        st = self._st()
        en = st["enabled"]

        self._update_ab_styles()
        self.power_switch.setChecked(en)

        # 1. Width
        self.cell_width.setCellActive(en)
        self.knob_width.setValue(st["width"])
        self.knob_width.setControlActive(en)
        self.pill_width.setText(f"{st['width']:.0f}%")
        self.pill_width.setControlActive(en)
        for val, btn in self.width_preset_btns.items():
            btn.setChecked(abs(st["width"] - val) < 0.5)

        # 2. Mid / Side Gain
        self.cell_ms.setCellActive(en)
        self.knob_mid.setValue(st["mid_gain"])
        self.knob_mid.setControlActive(en)
        self.pill_mid.setText(f"{st['mid_gain']:+.1f}dB" if abs(st["mid_gain"]) >= 0.05 else "0.0dB")
        self.pill_mid.setControlActive(en)

        self.knob_side.setValue(st["side_gain"])
        self.knob_side.setControlActive(en)
        self.pill_side.setText(f"{st['side_gain']:+.1f}dB" if abs(st["side_gain"]) >= 0.05 else "0.0dB")
        self.pill_side.setControlActive(en)

        # 3. Mono-Maker
        mm_on = en and st["mono_maker_on"]
        self.cell_mm.setCellActive(mm_on)
        self.btn_mm_toggle.setChecked(st["mono_maker_on"])
        if st["mono_maker_on"] and en:
            self.btn_mm_toggle.setStyleSheet(f"""
                QPushButton {{
                    background-color: #1F182E;
                    color: {PALETTE["violet"]};
                    border: 1px solid {PALETTE["violet"]};
                    border-radius: 3px;
                    font-size: 10px;
                    font-weight: 700;
                }}
            """)
        else:
            self.btn_mm_toggle.setStyleSheet("""
                QPushButton {
                    background-color: #323238;
                    color: #8A8A92;
                    border: none;
                    border-radius: 3px;
                    font-size: 10px;
                    font-weight: 600;
                }
            """)

        self.knob_mm_freq.setValue(st["mono_maker_freq"])
        self.knob_mm_freq.setControlActive(mm_on)
        self.pill_mm_freq.setText(format_freq_hz(st["mono_maker_freq"]))
        self.pill_mm_freq.setControlActive(mm_on)
        for sl, btn in self.mm_slope_btns.items():
            btn.setChecked(sl == st["mono_maker_slope"])

        # 4. Solo & Output Trim
        self.cell_out.setCellActive(en)
        self.knob_out.setValue(st["output_gain"])
        self.knob_out.setControlActive(en)
        self.pill_out.setText(f"{st['output_gain']:+.1f}dB" if abs(st["output_gain"]) >= 0.05 else "0.0dB")
        self.pill_out.setControlActive(en)

        solo = st["solo_mode"]
        self.ms_monitor.setSoloMode(solo)
        self.btn_bot_solo_m.setChecked(solo == "Mid")
        self.btn_bot_solo_s.setChecked(solo == "Side")

        # 5. 示波器状态同步
        self.vectorscope.setState(st)
        self._updating = False

    # -----------------------------------------------------------------
    # 交互回调与信号发射
    # -----------------------------------------------------------------
    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _copy_bank(self):
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._st())

    def _on_power_toggled(self, checked: bool):
        if self._updating:
            return
        self._st()["enabled"] = checked
        self._refresh_all_views()
        self.imagerBypassChanged.emit(checked)
        self.stateChanged.emit(self.get_state())

    def _on_width_changed(self, val: float):
        self._st()["width"] = round(val, 1)
        self._refresh_all_views()
        self.widthChanged.emit(self._st()["width"])
        self.stateChanged.emit(self.get_state())

    def _on_width_text(self, text: str):
        try:
            v = max(0.0, min(200.0, round(float(text.strip()), 1)))
            self._on_width_changed(v)
        except ValueError:
            pass

    def _on_mid_gain_changed(self, val: float):
        self._st()["mid_gain"] = round(val, 1)
        self._refresh_all_views()
        self.midGainChanged.emit(self._st()["mid_gain"])
        self.stateChanged.emit(self.get_state())

    def _on_mid_gain_text(self, text: str):
        try:
            v = max(-12.0, min(12.0, round(float(text.strip()), 1)))
            self._on_mid_gain_changed(v)
        except ValueError:
            pass

    def _on_side_gain_changed(self, val: float):
        self._st()["side_gain"] = round(val, 1)
        self._refresh_all_views()
        self.sideGainChanged.emit(self._st()["side_gain"])
        self.stateChanged.emit(self.get_state())

    def _on_side_gain_text(self, text: str):
        try:
            v = max(-12.0, min(12.0, round(float(text.strip()), 1)))
            self._on_side_gain_changed(v)
        except ValueError:
            pass

    def _reset_ms_gains(self):
        self._st()["mid_gain"] = 0.0
        self._st()["side_gain"] = 0.0
        self._refresh_all_views()
        self.midGainChanged.emit(0.0)
        self.sideGainChanged.emit(0.0)
        self.stateChanged.emit(self.get_state())

    def _emit_mono_maker(self):
        st = self._st()
        self.monoMakerChanged.emit(st["mono_maker_on"], st["mono_maker_freq"], st["mono_maker_slope"])
        self.stateChanged.emit(self.get_state())

    def _on_mm_toggled(self, checked: bool):
        self._st()["mono_maker_on"] = checked
        self._refresh_all_views()
        self._emit_mono_maker()

    def _on_mm_freq_changed(self, val: float):
        self._st()["mono_maker_freq"] = round(val, 1)
        self._refresh_all_views()
        self._emit_mono_maker()

    def _on_mm_freq_text(self, text: str):
        self._st()["mono_maker_freq"] = parse_freq_hz(text, self._st()["mono_maker_freq"])
        self._refresh_all_views()
        self._emit_mono_maker()

    def _on_mm_slope_changed(self, slope: str):
        self._st()["mono_maker_slope"] = slope
        self._refresh_all_views()
        self._emit_mono_maker()

    def _on_solo_mode_changed(self, mode: str):
        self._st()["solo_mode"] = mode
        self._refresh_all_views()
        self.soloModeChanged.emit(mode)
        self.stateChanged.emit(self.get_state())

    def _on_scope_mode_changed(self, mode: str):
        self._st()["scope_mode"] = mode
        self._refresh_all_views()

    def _on_out_gain_changed(self, val: float):
        self._st()["output_gain"] = round(val, 1)
        self._refresh_all_views()
        self.outputGainChanged.emit(self._st()["output_gain"])
        self.stateChanged.emit(self.get_state())

    def _on_out_gain_text(self, text: str):
        try:
            v = max(-12.0, min(12.0, round(float(text.strip()), 1)))
            self._on_out_gain_changed(v)
        except ValueError:
            pass

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
        name, ok = QInputDialog.getText(self, "保存声场预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._st())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 内置演示信号（真实音频引擎接入后自动接管）
    # -----------------------------------------------------------------
    def _tick_demo_signal(self):
        self._demo_phase += 0.16
        st = self._st()
        rng = random.Random(int(self._demo_phase * 100))

        samples_l, samples_r = [], []
        for i in range(64):
            t = self._demo_phase + i * 0.11
            mid_sig = 0.52 * math.sin(t) + 0.22 * math.sin(t * 2.3)
            side_sig = 0.28 * math.cos(t * 1.45) + rng.uniform(-0.08, 0.08)
            samples_l.append(mid_sig + side_sig)
            samples_r.append(mid_sig - side_sig)

        # 根据当前 Width % 与 Mono-Maker 估算模拟相干度与 M/S 电平
        w_factor = (st["width"] / 100.0) if st["enabled"] else 1.0
        mm_bonus = 0.12 if (st["enabled"] and st["mono_maker_on"]) else 0.0
        corr = max(-1.0, min(1.0, 0.94 - 0.34 * (w_factor ** 1.3) + mm_bonus + 0.04 * math.sin(self._demo_phase)))

        out_trim = st["output_gain"] if st["enabled"] else 0.0
        mid_db = -11.5 + (st["mid_gain"] if st["enabled"] else 0.0) + out_trim + 2.2 * math.sin(self._demo_phase * 1.3)
        side_offset = 20.0 * math.log10(max(0.01, w_factor)) if w_factor > 0.01 else -45.0
        side_db = -17.0 + side_offset + (st["side_gain"] if st["enabled"] else 0.0) + out_trim + 2.6 * math.cos(self._demo_phase * 1.1)

        self.vectorscope.pushSamples(samples_l, samples_r, corr)
        self.ms_monitor.setLevels(mid_db, side_db)

    # -----------------------------------------------------------------
    # 公开 API：供外部后端 DSP / 工程序列化调用
    # -----------------------------------------------------------------
    def reset_imager(self):
        self._banks[self._active_bank] = default_imager_state()
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._st())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()

    def update_metering(
        self,
        samples_l: list[float],
        samples_r: list[float],
        mid_db: float,
        side_db: float,
        correlation: float,
    ):
        """
        接入真实音频引擎实时遥测数据：
        - samples_l / samples_r: 当前音频缓冲区的左右声道采样切片 (-1.0 ~ +1.0)
        - mid_db / side_db: 实时 M 与 S 通道峰值/RMS 电平 (-60.0 ~ 0.0 dBFS)
        - correlation: 实时相位相关度 (-1.0 ~ +1.0)
        """
        if self._demo_timer.isActive():
            self._demo_timer.stop()
        self.vectorscope.pushSamples(samples_l, samples_r, correlation)
        self.ms_monitor.setLevels(mid_db, side_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioStereoImagerWidget("Master Bus")
    win.show()
    sys.exit(app.exec())