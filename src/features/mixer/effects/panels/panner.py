from __future__ import annotations

import sys
import math
import copy
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QSizePolicy,
    QInputDialog, QMenu, QScrollArea
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont, QPolygonF
)

from src.shared.ui.widgets.chevron_combo import ChevronComboBox


# =====================================================================
# 1. 全局色彩系统与空间声学常量（与 EQ-UI.py 保持统一视觉规范）
# =====================================================================
# 与 AudioClipEditor-UI / EQ-UI 统一的达芬奇 Fairlight 视觉系统：
# #27272B 哑光中性灰基底 + 1px #141416 纯黑细线切分，字体栈一致
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

PALETTE = {
    "cyan":   "#28D8F0",  # 主声像锚点 / 纵深
    "green":  "#4CD490",  # 左声道 / 声场宽度
    "amber":  "#F0A848",  # 右声道 / 声场旋转
    "blue":   "#6495FA",  # 横向定位
    "violet": "#B67CF8",  # 3D 高度 / 弥散度
    "coral":  "#F26D6D",  # Boom / LFE 低频发送
}

# 不同制式下的物理扬声器参考点位 (label, x[-1..1], y[-1..1], z[0..1])
SPEAKER_LAYOUTS = {
    "2.0": [
        ("L", -1.0, 1.0, 0.0),
        ("R",  1.0, 1.0, 0.0),
    ],
    "5.1": [
        ("L",  -1.0,  1.0, 0.0),
        ("C",   0.0,  1.0, 0.0),
        ("R",   1.0,  1.0, 0.0),
        ("Ls", -1.0, -0.55, 0.0),
        ("Rs",  1.0, -0.55, 0.0),
    ],
    "7.1": [
        ("L",   -1.0,  1.0, 0.0),
        ("C",    0.0,  1.0, 0.0),
        ("R",    1.0,  1.0, 0.0),
        ("Lss", -1.0,  0.0, 0.0),
        ("Rss",  1.0,  0.0, 0.0),
        ("Lrs", -0.75, -1.0, 0.0),
        ("Rrs",  0.75, -1.0, 0.0),
    ],
    "7.1.4": [
        ("L",   -1.0,  1.0, 0.0),
        ("C",    0.0,  1.0, 0.0),
        ("R",    1.0,  1.0, 0.0),
        ("Lss", -1.0,  0.0, 0.0),
        ("Rss",  1.0,  0.0, 0.0),
        ("Lrs", -0.75, -1.0, 0.0),
        ("Rrs",  0.75, -1.0, 0.0),
        ("Ltf", -0.85,  0.65, 1.0),
        ("Rtf",  0.85,  0.65, 1.0),
        ("Ltr", -0.85, -0.65, 1.0),
        ("Rtr",  0.85, -0.65, 1.0),
    ],
}


def default_panner_state() -> dict:
    return {
        "enabled": True,
        "space_mode": "2D",       # "2D" | "3D"
        "format": "5.1",          # "2.0" | "5.1" | "7.1" | "7.1.4"
        "lr": 0.0,                # -100.0 (100L) ~ +100.0 (100R), 0 = C
        "fb": 100.0,              # -100.0 (100B) ~ +100.0 (100F), 0 = C
        "elevation": 0.0,         # 0.0 (Floor) ~ 100.0 (100T Ceiling)
        "rotate": 0.0,            # 0.0 ~ 360.0 deg
        "spread": 100.0,          # 0.0 (PNT) ~ 100.0 (FULL)
        "spread_auto": False,     # Auto spread convergence
        "divergence": 0.0,        # 0.0 ~ 100.0 %
        "div_mode": "2D",         # "2D" | "3D"
        "boom_on": False,         # LFE Send On/Off
        "boom_route": "Pre",      # "Pre" | "Post" | "Only"
        "boom_gain": -60.0,       # -60.0 (-inf) ~ +10.0 dB
    }


# =====================================================================
# 2. 数值格式化与自然文本解析工具
# =====================================================================
def fmt_lr(val: float) -> str:
    v = int(round(val))
    if v == 0:
        return "C"
    return f"{abs(v)}L" if v < 0 else f"{v}R"


def parse_lr(text: str, fallback: float) -> float:
    s = text.strip().upper()
    if s in ("C", "CENTER", "0"):
        return 0.0
    try:
        if s.endswith("L"):
            return -max(0.0, min(100.0, float(s[:-1])))
        if s.startswith("L"):
            return -max(0.0, min(100.0, float(s[1:])))
        if s.endswith("R"):
            return max(0.0, min(100.0, float(s[:-1])))
        if s.startswith("R"):
            return max(0.0, min(100.0, float(s[1:])))
        return max(-100.0, min(100.0, float(s)))
    except ValueError:
        return fallback


def fmt_fb(val: float) -> str:
    v = int(round(val))
    if v == 0:
        return "C"
    return f"{v}F" if v > 0 else f"{abs(v)}B"


def parse_fb(text: str, fallback: float) -> float:
    s = text.strip().upper()
    if s in ("C", "CENTER", "0"):
        return 0.0
    try:
        if s.endswith("F"):
            return max(0.0, min(100.0, float(s[:-1])))
        if s.startswith("F"):
            return max(0.0, min(100.0, float(s[1:])))
        if s.endswith("B"):
            return -max(0.0, min(100.0, float(s[:-1])))
        if s.startswith("B"):
            return -max(0.0, min(100.0, float(s[1:])))
        return max(-100.0, min(100.0, float(s)))
    except ValueError:
        return fallback


def fmt_elev(val: float) -> str:
    v = int(round(val))
    return "0" if v == 0 else f"{v}T"


def parse_elev(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("T", "").replace("%", "")
    try:
        return max(0.0, min(100.0, float(s)))
    except ValueError:
        return fallback


def fmt_rotate(val: float) -> str:
    v = int(round(val)) % 360
    return f"{v}°"


def parse_rotate(text: str, fallback: float) -> float:
    s = text.strip().replace("°", "")
    try:
        return float(s) % 360.0
    except ValueError:
        return fallback


def fmt_spread(val: float, is_auto: bool) -> str:
    v = int(round(val))
    if is_auto:
        return f"A·{v}%"
    if v <= 0:
        return "PNT"
    if v >= 100:
        return "FULL"
    return f"{v}%"


def parse_spread(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("%", "").replace("A·", "")
    if s in ("PNT", "POINT"):
        return 0.0
    if s in ("FULL", "MAX"):
        return 100.0
    try:
        return max(0.0, min(100.0, float(s)))
    except ValueError:
        return fallback


def fmt_boom(val: float) -> str:
    if val <= -59.5:
        return "-∞"
    if abs(val) < 0.05:
        return "0.0"
    return f"{val:+.1f}"


def parse_boom(text: str, fallback: float) -> float:
    s = text.strip().upper().replace("DB", "").strip()
    if s in ("-INF", "-∞", "INF", "OFF"):
        return -60.0
    try:
        return max(-60.0, min(10.0, float(s)))
    except ValueError:
        return fallback


# =====================================================================
# 3. 基础控件：现代发光电源开关（复用 EQ-UI 风格）
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
# 4. 基础控件：内嵌可编辑数字胶囊（复用 EQ-UI 风格）
# =====================================================================
class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(str)

    def __init__(self, text: str = "C", accent_color: str = "#28D8F0", width: int = 58, parent=None):
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
                font-size: 11px;
                font-weight: 600;
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
            self._editor.setText(self._text.replace("°", "").replace("A·", ""))
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
# 5. 基础控件：拉丝阳极铝质感旋钮（支持单极 / 双极 / 连续旋转）
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
        mode: str = "linear",   # "linear" | "bipolar" | "circular"
        invert: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._mode = mode
        self._invert = invert
        self._active = True
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(38, 38)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setValue(self, val: float, emit: bool = False):
        if self._mode == "circular":
            clamped = val % (self._max - self._min)
        else:
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
        n = (val - self._min) / (self._max - self._min)
        return (1.0 - n) if self._invert else n

    def _norm_to_val(self, norm: float) -> float:
        if self._mode == "circular":
            n = norm % 1.0
        else:
            n = max(0.0, min(1.0, norm))
        if self._invert:
            n = 1.0 - n
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

        # 1. 深色凹槽底环
        p.setPen(QPen(QColor("#141417"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性模式下绘制 12 点钟中点基准刻度
        if self._mode == "bipolar":
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - arc_r - 2.2), QPointF(cx, cy - arc_r + 0.8))

        # 2. 参数主题色发光弧
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

        # 3. 旋钮黑边凹槽 + 达芬奇风格哑光浅灰旋钮顶盖
        knob_r = 11.6
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 4. 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * 3.4
        iy1 = cy - math.sin(rad) * 3.4
        ix2 = cx + math.cos(rad) * (knob_r - 1.4)
        iy2 = cy - math.sin(rad) * (knob_r - 1.4)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 6. 左侧参数控制卡片：单项声像模块（带色条 + 独立功能开关/标签）
# =====================================================================
class PannerControlCard(QFrame):
    def __init__(self, title: str, accent_color: str, parent=None):
        super().__init__(parent)
        self._accent = accent_color
        self._enabled = True

        self.setObjectName("PanCard")
        self._apply_style()

        self.main_lay = QHBoxLayout(self)
        self.main_lay.setContentsMargins(9, 5, 9, 5)
        self.main_lay.setSpacing(8)

        # 左侧：标题 + 辅助按钮 + 数值胶囊
        self.left_col = QVBoxLayout()
        self.left_col.setSpacing(5)

        self.title_lbl = QLabel(title)
        self.title_lbl.setObjectName("CardTitle")
        self.left_col.addWidget(self.title_lbl)

        self.extra_slot = QVBoxLayout()
        self.extra_slot.setSpacing(4)
        self.left_col.addLayout(self.extra_slot)
        self.left_col.addStretch()

        # 右侧：旋钮 + 刻度范围提示
        self.right_col = QVBoxLayout()
        self.right_col.setSpacing(2)
        self.right_col.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.main_lay.addLayout(self.left_col, 1)
        self.main_lay.addLayout(self.right_col)

    def setCardEnabled(self, enabled: bool):
        self._enabled = enabled
        self._apply_style()

    def _apply_style(self):
        top_col = self._accent
        title_col = "#B6BCCB" if self._enabled else "#5E6370"
        self.setStyleSheet(f"""
            QFrame#PanCard {{
                background-color: #27272B;
                border: none;
                border-bottom: 1px solid #141416;
                border-left: 2.5px solid {top_col};
                border-radius: 0px;
            }}
            QLabel#CardTitle {{
                color: {title_col};
                font-size: 11px;
                font-weight: 700;
            }}
            QLabel#HintText {{
                color: #787882;
                font-size: 9px;
                font-weight: 600;
            }}
        """)

    def build_pill_btn(self, text: str, checkable: bool = True, accent: str | None = None) -> QPushButton:
        ac = accent or self._accent
        btn = QPushButton(text)
        btn.setCheckable(checkable)
        btn.setFixedSize(58, 20)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: #3C3C44;
                color: #FFFFFF;
            }}
            QPushButton:checked {{
                background-color: #182E38;
                color: {ac};
                border: 1px solid {ac};
            }}
        """)
        return btn


# =====================================================================
# 7. 核心画布：2D 声场俯视矩阵 / 3D 透视声学空间舞台
# =====================================================================
class SpatialSoundStageWidget(QWidget):
    # (lr [-100..100], fb [-100..100])
    xyDragged = pyqtSignal(float, float)
    # elevation [0..100]
    elevationDragged = pyqtSignal(float)
    # quick actions
    resetPositionRequested = pyqtSignal()
    toggle3DRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 420)
        self.setMouseTracking(True)

        self._state = default_panner_state()
        self._dragging = False
        self._drag_mode = "xy"   # "xy" | "z"
        self._drag_start_pos = QPointF()
        self._drag_start_lr = 0.0
        self._drag_start_fb = 100.0
        self._drag_start_elev = 0.0
        self._hover_anchor = False

        self._stage_rect = QRectF()

    def setState(self, state: dict):
        self._state = copy.deepcopy(state)
        self.update()

    # -----------------------------------------------------------------
    # 声学运动学计算：根据 LR / FB / Rotate / Spread 计算 L/R 虚拟卫星节点
    # -----------------------------------------------------------------
    def _effective_spread(self) -> float:
        x_norm = abs(self._state["lr"]) / 100.0
        y_norm = abs(self._state["fb"]) / 100.0
        if self._state["spread_auto"]:
            # 模拟 Fairlight Auto Spread：越靠边界/角落越向点声源收敛
            auto_s = max(0.0, (1.0 - x_norm) * (1.0 - 0.35 * y_norm))
            return auto_s
        return self._state["spread"] / 100.0

    def _compute_virtual_nodes(self) -> tuple[QPointF, QPointF, QPointF]:
        """返回 (anchor_norm, left_sat_norm, right_sat_norm)，坐标范围 [-1, 1]"""
        ax = self._state["lr"] / 100.0
        ay = self._state["fb"] / 100.0
        s = self._effective_spread()
        theta = math.radians(-self._state["rotate"])

        # 左右半宽向量旋转
        dx = s * math.cos(theta)
        dy = s * math.sin(theta)

        lx = max(-1.0, min(1.0, ax - dx))
        ly = max(-1.0, min(1.0, ay - dy))
        rx = max(-1.0, min(1.0, ax + dx))
        ry = max(-1.0, min(1.0, ay + dy))

        return QPointF(ax, ay), QPointF(lx, ly), QPointF(rx, ry)

    # -----------------------------------------------------------------
    # 2D 平面坐标映射 & 3D 透视房间投影映射
    # -----------------------------------------------------------------
    def _compute_stage_rect(self) -> QRectF:
        pad_x = 34.0
        pad_top = 38.0
        pad_bot = 28.0
        avail_w = max(120.0, self.width() - pad_x * 2)
        avail_h = max(120.0, self.height() - pad_top - pad_bot)
        side = min(avail_w, avail_h)
        cx = self.width() / 2.0
        cy = pad_top + avail_h / 2.0
        return QRectF(cx - side / 2.0, cy - side / 2.0, side, side)

    def _norm_to_2d(self, nx: float, ny: float, rect: QRectF) -> QPointF:
        px = rect.center().x() + nx * (rect.width() * 0.5)
        py = rect.center().y() - ny * (rect.height() * 0.5)
        return QPointF(px, py)

    def _2d_to_norm(self, pt: QPointF, rect: QRectF) -> tuple[float, float]:
        nx = (pt.x() - rect.center().x()) / max(1.0, rect.width() * 0.5)
        ny = (rect.center().y() - pt.y()) / max(1.0, rect.height() * 0.5)
        return max(-1.0, min(1.0, nx)), max(-1.0, min(1.0, ny))

    def _project_3d(self, nx: float, ny: float, nz: float, rect: QRectF) -> QPointF:
        """
        将空间归一化坐标 nx[-1..1], ny[-1..1(Front)], nz[0..1(Top)] 投影为 2.5D/3D 声学房间视角
        Front(ny=+1) 在后上方，Back(ny=-1) 在前下方，nz 向上抬升
        """
        cx = rect.center().x()
        cy = rect.center().y() + rect.height() * 0.14
        w_half = rect.width() * 0.44
        depth_scale = 1.0 - 0.24 * ((ny + 1.0) * 0.5)  # 远端微缩透视
        px = cx + nx * w_half * depth_scale
        py = cy - ny * (rect.height() * 0.25) - nz * (rect.height() * 0.34)
        return QPointF(px, py)

    def _screen_anchor_pos(self, rect: QRectF) -> QPointF:
        nx = self._state["lr"] / 100.0
        ny = self._state["fb"] / 100.0
        nz = self._state["elevation"] / 100.0
        if self._state["space_mode"] == "3D":
            return self._project_3d(nx, ny, nz, rect)
        return self._norm_to_2d(nx, ny, rect)

    # -----------------------------------------------------------------
    # 鼠标交互：拖拽定位 / Shift 调节 3D 高度 / Ctrl·Alt 轴锁定 / 右键菜单
    # -----------------------------------------------------------------
    def mousePressEvent(self, event):
        rect = self._compute_stage_rect()
        pos = event.position()

        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start_pos = pos
            self._drag_start_lr = self._state["lr"]
            self._drag_start_fb = self._state["fb"]
            self._drag_start_elev = self._state["elevation"]

            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._drag_mode = "z"
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self._drag_mode = "xy"
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                # 点击空白处直接吸附过去（若未按住微调修饰键）
                anchor_pt = self._screen_anchor_pos(rect)
                if math.hypot(pos.x() - anchor_pt.x(), pos.y() - anchor_pt.y()) > 16.0:
                    self._update_xy_from_pos(pos, rect, event.modifiers())

        elif event.button() == Qt.MouseButton.RightButton:
            self._open_context_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event):
        rect = self._compute_stage_rect()
        pos = event.position()

        if self._dragging:
            if self._drag_mode == "z" or (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                dy = self._drag_start_pos.y() - pos.y()
                new_elev = max(0.0, min(100.0, round(self._drag_start_elev + dy * 0.45, 1)))
                self.elevationDragged.emit(new_elev)
            else:
                self._update_xy_from_pos(pos, rect, event.modifiers())
        else:
            anchor_pt = self._screen_anchor_pos(rect)
            hit = math.hypot(pos.x() - anchor_pt.x(), pos.y() - anchor_pt.y()) <= 14.0
            if hit != self._hover_anchor:
                self._hover_anchor = hit
                self.setCursor(Qt.CursorShape.OpenHandCursor if hit else Qt.CursorShape.CrossCursor)
                self.update()

    def _update_xy_from_pos(self, pos: QPointF, rect: QRectF, mods):
        if self._state["space_mode"] == "3D":
            # 3D 模式下基于当前仰角平面反算 XY
            nz = self._state["elevation"] / 100.0
            cy = rect.center().y() + rect.height() * 0.14 - nz * (rect.height() * 0.34)
            ny = max(-1.0, min(1.0, (cy - pos.y()) / max(1.0, rect.height() * 0.25)))
            depth_scale = 1.0 - 0.24 * ((ny + 1.0) * 0.5)
            w_half = rect.width() * 0.44 * depth_scale
            nx = max(-1.0, min(1.0, (pos.x() - rect.center().x()) / max(1.0, w_half)))
        else:
            nx, ny = self._2d_to_norm(pos, rect)

        new_lr = round(nx * 100.0, 1)
        new_fb = round(ny * 100.0, 1)

        # Ctrl 锁定左右，Alt 锁定前后
        if mods & Qt.KeyboardModifier.ControlModifier:
            new_lr = self._drag_start_lr
        if mods & Qt.KeyboardModifier.AltModifier:
            new_fb = self._drag_start_fb

        self.xyDragged.emit(new_lr, new_fb)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._hover_anchor else Qt.CursorShape.CrossCursor)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.resetPositionRequested.emit()

    def wheelEvent(self, event):
        # 滚轮在 3D 模式下直接微调仰角高度 Elevation
        if self._state["space_mode"] == "3D":
            delta = event.angleDelta().y() / 120.0
            new_elev = max(0.0, min(100.0, round(self._state["elevation"] + delta * 4.0, 1)))
            self.elevationDragged.emit(new_elev)
            event.accept()

    def _open_context_menu(self, global_pos):
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
        presets = [
            ("前置正中  (C, 100F)", 0.0, 100.0),
            ("声场正心  (C, C)",    0.0, 0.0),
            ("左前音箱  (100L, 100F)", -100.0, 100.0),
            ("右前音箱  (100R, 100F)",  100.0, 100.0),
            ("后环绕正中 (C, 100B)", 0.0, -100.0),
        ]
        for label, lr, fb in presets:
            act = menu.addAction(label)
            act.triggered.connect(lambda checked=False, x=lr, y=fb: self.xyDragged.emit(x, y))
        menu.addSeparator()
        act_3d = menu.addAction("切换 2D / 3D 空间视图")
        act_3d.triggered.connect(self.toggle3DRequested.emit)
        menu.exec(global_pos)

    # -----------------------------------------------------------------
    # 绘制 2D 声学网格 / 3D 透视房间 / 扬声器 / 弥散光晕 / 声像节点
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self._compute_stage_rect()
        self._stage_rect = rect
        en = self._state["enabled"]
        is_3d = (self._state["space_mode"] == "3D")

        # 1. 画布外框与夜空微渐变背景
        panel_rect = QRectF(0, 0, self.width(), self.height())
        bg_grad = QLinearGradient(panel_rect.topLeft(), panel_rect.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#1D1D20"))
        bg_grad.setColorAt(1.0, QColor("#1D1D20"))
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(bg_grad)
        p.drawRect(panel_rect)

        if is_3d:
            self._paint_3d_stage(p, rect, en)
        else:
            self._paint_2d_stage(p, rect, en)

        # 2. 左上角实时声像坐标 HUD（复用 EQ-UI 悬浮读数胶囊风格）
        lr_s = fmt_lr(self._state["lr"])
        fb_s = fmt_fb(self._state["fb"])
        rot_s = fmt_rotate(self._state["rotate"])
        if is_3d:
            z_s = fmt_elev(self._state["elevation"])
            hud_text = f"L/R {lr_s}  ·  F/B {fb_s}  ·  Z {z_s}  ·  Rot {rot_s}"
        else:
            hud_text = f"L/R {lr_s}  ·  F/B {fb_s}  ·  Rot {rot_s}"

        p.setFont(QFont(MONO_FONT_FAM, 8))
        hud_w = p.fontMetrics().horizontalAdvance(hud_text) + 16
        hud_rect = QRectF(14, 12, hud_w, 20)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor("#28D8F0") if en else QColor("#8A8A92"))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_text)

        # 右上角交互提示标签
        hint_str = "Shift+拖拽/滚轮调高度" if is_3d else "双击复位 · Ctrl/Alt 锁轴"
        p.setPen(QColor("#787882"))
        hint_start = hud_rect.right() + 8
        hint_width = self.width() - hint_start - 13
        if p.fontMetrics().horizontalAdvance(hint_str) <= hint_width:
            p.drawText(QRectF(hint_start, 12, hint_width, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, hint_str)

    def _paint_2d_stage(self, p: QPainter, rect: QRectF, en: bool):
        # 6x6 主网格
        grid_pen = QPen(QColor("#26262A"), 1.0)
        axis_pen = QPen(QColor("#3A3A40"), 1.15)
        cells = 6
        for i in range(cells + 1):
            t = i / cells
            x = rect.left() + t * rect.width()
            y = rect.top() + t * rect.height()
            p.setPen(axis_pen if i == 3 else grid_pen)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

        # 内部甜点听音区参考框 (50% 边界)
        inner = QRectF(
            rect.left() + rect.width() * 0.25,
            rect.top() + rect.height() * 0.25,
            rect.width() * 0.5,
            rect.height() * 0.5,
        )
        p.setPen(QPen(QColor("#343948"), 1.0, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(inner)

        # 外边框
        p.setPen(QPen(QColor("#323746"), 1.1))
        p.drawRect(rect)

        # 方位标识 F / B / L / R
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor("#9A9AA2"))
        cx, cy = rect.center().x(), rect.center().y()
        p.drawText(QRectF(cx - 14, rect.top() - 18, 28, 16), Qt.AlignmentFlag.AlignCenter, "F")
        p.drawText(QRectF(cx - 14, rect.bottom() + 2, 28, 16), Qt.AlignmentFlag.AlignCenter, "B")
        p.drawText(QRectF(rect.left() - 20, cy - 8, 16, 16), Qt.AlignmentFlag.AlignCenter, "L")
        p.drawText(QRectF(rect.right() + 4, cy - 8, 16, 16), Qt.AlignmentFlag.AlignCenter, "R")

        # 物理扬声器参考标记
        speakers = SPEAKER_LAYOUTS.get(self._state["format"], [])
        for spk_name, sx, sy, sz in speakers:
            if sz > 0.5:
                continue  # 2D 视图仅绘制平面层音箱
            spt = self._norm_to_2d(sx, sy, rect)
            p.setPen(QPen(QColor("#4A4A52"), 1.0))
            p.setBrush(QColor("#151518"))
            p.drawRoundedRect(QRectF(spt.x() - 11, spt.y() - 7, 22, 14), 2.5, 2.5)
            p.setPen(QColor("#9A9AA2"))
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(QRectF(spt.x() - 11, spt.y() - 7, 22, 14), Qt.AlignmentFlag.AlignCenter, spk_name)

        # 计算主锚点与 L/R 卫星节点
        a_norm, l_norm, r_norm = self._compute_virtual_nodes()
        a_pt = self._norm_to_2d(a_norm.x(), a_norm.y(), rect)
        l_pt = self._norm_to_2d(l_norm.x(), l_norm.y(), rect)
        r_pt = self._norm_to_2d(r_norm.x(), r_norm.y(), rect)

        # Divergence 声学弥散光晕
        div_norm = self._state["divergence"] / 100.0
        if div_norm > 0.01 and en:
            halo_r = 16.0 + div_norm * (rect.width() * 0.36)
            rad_grad = QRadialGradient(a_pt, halo_r)
            rad_grad.setColorAt(0.0, QColor(182, 124, 248, int(65 * div_norm)))
            rad_grad.setColorAt(0.7, QColor(40, 216, 240, int(32 * div_norm)))
            rad_grad.setColorAt(1.0, QColor(40, 216, 240, 0))
            p.setPen(QPen(QColor(182, 124, 248, int(90 * div_norm)), 1.0, Qt.PenStyle.DotLine))
            p.setBrush(rad_grad)
            p.drawEllipse(a_pt, halo_r, halo_r)

        # L - Anchor - R 声场耦合连线
        line_col = QColor(76, 212, 144, 120) if en else QColor(90, 95, 110, 80)
        p.setPen(QPen(line_col, 1.35))
        p.drawLine(l_pt, a_pt)
        p.drawLine(a_pt, r_pt)

        # 绘制 L / R 卫星声像节点
        l_col = QColor(PALETTE["green"]) if en else QColor("#6E6E76")
        r_col = QColor(PALETTE["amber"]) if en else QColor("#6E6E76")
        for spt, scol, tag in ((l_pt, l_col, "L"), (r_pt, r_col, "R")):
            p.setPen(QPen(QColor("#111318"), 1.2))
            p.setBrush(scol)
            p.drawEllipse(spt, 5.2, 5.2)

        # 绘制主声像方形控制锚点（与截图呼应，融入青色发光外环）
        c_col = QColor(PALETTE["cyan"]) if en else QColor("#8A8A92")
        if self._hover_anchor or self._dragging:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(40, 216, 240, 55))
            p.drawEllipse(a_pt, 14.0, 14.0)

        p.setPen(QPen(QColor("#0F1218"), 1.4))
        p.setBrush(c_col)
        p.drawRoundedRect(QRectF(a_pt.x() - 5.5, a_pt.y() - 5.5, 11.0, 11.0), 2.0, 2.0)

    def _paint_3d_stage(self, p: QPainter, rect: QRectF, en: bool):
        """绘制 3D 透视声学房间立方体与高度投影线"""
        # 地板四角 (z=0) 与 天花板四角 (z=1)
        f_fl = self._project_3d(-1,  1, 0, rect)
        f_fr = self._project_3d( 1,  1, 0, rect)
        f_br = self._project_3d( 1, -1, 0, rect)
        f_bl = self._project_3d(-1, -1, 0, rect)

        c_fl = self._project_3d(-1,  1, 1, rect)
        c_fr = self._project_3d( 1,  1, 1, rect)
        c_br = self._project_3d( 1, -1, 1, rect)
        c_bl = self._project_3d(-1, -1, 1, rect)

        # 地板半透明填充
        floor_poly = QPolygonF([f_fl, f_fr, f_br, f_bl])
        p.setPen(QPen(QColor("#3A3A40"), 1.1))
        p.setBrush(QColor(30, 30, 34, 140))
        p.drawPolygon(floor_poly)

        # 地板 4x4 透视网格
        p.setPen(QPen(QColor("#26262A"), 1.0))
        for i in range(1, 4):
            t = -1.0 + i * 0.5
            p.drawLine(self._project_3d(t, -1, 0, rect), self._project_3d(t, 1, 0, rect))
            p.drawLine(self._project_3d(-1, t, 0, rect), self._project_3d(1, t, 0, rect))

        # 4 根垂直立柱
        p.setPen(QPen(QColor("#333338"), 1.0, Qt.PenStyle.DashLine))
        for fp, cp in ((f_fl, c_fl), (f_fr, c_fr), (f_br, c_br), (f_bl, c_bl)):
            p.drawLine(fp, cp)

        # 天花板线框
        ceil_poly = QPolygonF([c_fl, c_fr, c_br, c_bl])
        p.setPen(QPen(QColor("#3A3A40"), 1.0))
        p.setBrush(QColor(40, 216, 240, 10))
        p.drawPolygon(ceil_poly)

        # 方位标签
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.setPen(QColor("#9A9AA2"))
        f_mid = self._project_3d(0, 1.14, 0, rect)
        b_mid = self._project_3d(0, -1.16, 0, rect)
        l_mid = self._project_3d(-1.14, 0, 0, rect)
        r_mid = self._project_3d(1.14, 0, 0, rect)
        top_mid = self._project_3d(0, 1.0, 1.12, rect)
        p.drawText(QRectF(f_mid.x() - 16, f_mid.y() - 8, 32, 16), Qt.AlignmentFlag.AlignCenter, "F")
        p.drawText(QRectF(b_mid.x() - 16, b_mid.y() - 8, 32, 16), Qt.AlignmentFlag.AlignCenter, "B")
        p.drawText(QRectF(l_mid.x() - 16, l_mid.y() - 8, 32, 16), Qt.AlignmentFlag.AlignCenter, "L")
        p.drawText(QRectF(r_mid.x() - 16, r_mid.y() - 8, 32, 16), Qt.AlignmentFlag.AlignCenter, "R")
        p.setPen(QColor(PALETTE["violet"]))
        p.drawText(QRectF(top_mid.x() - 24, top_mid.y() - 8, 48, 16), Qt.AlignmentFlag.AlignCenter, "TOP (Z)")

        # 计算 3D 空间节点
        nz = self._state["elevation"] / 100.0
        a_norm, l_norm, r_norm = self._compute_virtual_nodes()

        floor_pt = self._project_3d(a_norm.x(), a_norm.y(), 0.0, rect)
        a_pt = self._project_3d(a_norm.x(), a_norm.y(), nz, rect)
        l_pt = self._project_3d(l_norm.x(), l_norm.y(), nz, rect)
        r_pt = self._project_3d(r_norm.x(), r_norm.y(), nz, rect)

        # 地板投影十字准星与高度垂直虚线
        p.setPen(QPen(QColor(182, 124, 248, 160), 1.2, Qt.PenStyle.DashLine))
        p.drawLine(floor_pt, a_pt)
        p.setPen(QPen(QColor("#55555C"), 1.0))
        p.drawEllipse(floor_pt, 4.0, 2.5)

        # Divergence 3D 弥散球光晕
        div_norm = self._state["divergence"] / 100.0
        if div_norm > 0.01 and en:
            halo_r = 14.0 + div_norm * (rect.width() * 0.28)
            rad_grad = QRadialGradient(a_pt, halo_r)
            rad_grad.setColorAt(0.0, QColor(182, 124, 248, int(75 * div_norm)))
            rad_grad.setColorAt(1.0, QColor(40, 216, 240, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rad_grad)
            p.drawEllipse(a_pt, halo_r, halo_r)

        # 3D 卫星连线与节点
        p.setPen(QPen(QColor(76, 212, 144, 140) if en else QColor("#555A68"), 1.4))
        p.drawLine(l_pt, a_pt)
        p.drawLine(a_pt, r_pt)

        for spt, col_hex in ((l_pt, PALETTE["green"]), (r_pt, PALETTE["amber"])):
            p.setPen(QPen(QColor("#111318"), 1.2))
            p.setBrush(QColor(col_hex) if en else QColor("#6E6E76"))
            p.drawEllipse(spt, 5.0, 5.0)

        # 3D 主声像立体球体锚点
        c_col = QColor(PALETTE["cyan"]) if en else QColor("#8A8A92")
        sphere_grad = QRadialGradient(a_pt.x() - 2.5, a_pt.y() - 2.5, 10.0)
        sphere_grad.setColorAt(0.0, QColor("#FFFFFF"))
        sphere_grad.setColorAt(0.35, c_col)
        sphere_grad.setColorAt(1.0, QColor("#0E4A56"))
        p.setPen(QPen(QColor("#0D1016"), 1.2))
        p.setBrush(sphere_grad)
        p.drawEllipse(a_pt, 6.8, 6.8)


# =====================================================================
# 8. 主面板：自主旗舰空间声像器 (Studio Spatial Panner)
# =====================================================================
class StudioSpatialPannerWidget(QWidget):
    # 对接后端音频引擎的完整信号接口
    panBypassChanged = pyqtSignal(bool)
    spaceModeChanged = pyqtSignal(str)
    channelFormatChanged = pyqtSignal(str)
    panPositionChanged = pyqtSignal(float, float, float)   # lr, fb, elevation
    rotateChanged = pyqtSignal(float)
    spreadChanged = pyqtSignal(float, bool)                # spread, is_auto
    divergenceChanged = pyqtSignal(float, str)             # divergence, mode
    boomChanged = pyqtSignal(bool, str, float)             # on, route, gain_db
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Audio 1", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Spatial Panner — {track_name}")
        self.resize(780, 620)
        self.setMinimumSize(700, 560)

        self._updating = False
        self._active_bank = "A"
        self._banks = {
            "A": default_panner_state(),
            "B": {
                **default_panner_state(),
                "lr": -35.0, "fb": 40.0, "rotate": 25.0, "spread": 65.0
            },
        }

        self._presets = {
            "Front Center (Default)": default_panner_state(),
            "Wide Stereo Stage": {
                **default_panner_state(), "lr": 0.0, "fb": 85.0, "spread": 100.0, "divergence": 15.0
            },
            "Intimate Mono Point": {
                **default_panner_state(), "lr": 0.0, "fb": 90.0, "spread": 0.0, "divergence": 0.0
            },
            "Surround Envelopment": {
                **default_panner_state(), "lr": 0.0, "fb": -15.0, "spread": 100.0, "divergence": 55.0
            },
            "3D Overhead Flyby": {
                **default_panner_state(), "space_mode": "3D", "format": "7.1.4",
                "lr": -25.0, "fb": 30.0, "elevation": 85.0, "spread": 40.0, "divergence": 30.0
            },
            "Subwoofer LFE Send": {
                **default_panner_state(), "boom_on": True, "boom_route": "Pre", "boom_gain": 0.0
            },
        }

        self._setup_ui(track_name)
        self._refresh_all_views()

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: 'Helvetica Neue', 'PingFang SC';
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            SpatialSoundStageWidget {{
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

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -------------------------------------------------------------
        # 1. 顶部统一工具栏（电源 + 标题 + 预设 + A/B 对比 + 制式 + 2D/3D + Reset）
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setFixedHeight(38)
        top_bar.setObjectName("TopBar")
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb = QHBoxLayout(top_bar)
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True, PALETTE["cyan"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb.addWidget(self.power_switch)

        title_lbl = QLabel(f"SPATIAL PANNER  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.5px;")
        tb.addWidget(title_lbl)

        tb.addSpacing(6)

        # 预设管理
        add_btn = QPushButton("+")
        add_btn.setObjectName("ToolBtn")
        add_btn.setFixedSize(22, 22)
        add_btn.setToolTip("保存当前声像为预设")
        add_btn.clicked.connect(self._save_new_preset)
        tb.addWidget(add_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(155)
        self.preset_combo.addItems(list(self._presets.keys()))
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        tb.addWidget(self.preset_combo)

        # A / B 对比槽
        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(22, 22)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton("→")
        self.btn_copy_ab.setFixedSize(22, 22)
        self.btn_copy_ab.setToolTip("复制当前参数到另一槽位")
        self.btn_copy_ab.clicked.connect(self._copy_bank)

        self.btn_b = QPushButton("B")
        self.btn_b.setFixedSize(22, 22)
        self.btn_b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_b.clicked.connect(lambda: self._switch_bank("B"))

        tb.addWidget(self.btn_a)
        tb.addWidget(self.btn_copy_ab)
        tb.addWidget(self.btn_b)
        # 箭头按钮样式由 _update_ab_styles 统一设置（勿在入布局前绑定 objectName QSS，
        # 否则 Qt6 基线对齐分组会把它排到行底，相对 A/B 下沉 2px）

        tb.addStretch()

        # 扬声器制式选择
        self.format_combo = ChevronComboBox()
        self.format_combo.addItems(["2.0", "5.1", "7.1", "7.1.4"])
        self.format_combo.setFixedWidth(self.format_combo.sizeHint().width())
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        tb.addWidget(self.format_combo)

        # 2D / 3D 空间模式切换胶囊
        self.btn_3d = QPushButton("3D")
        self.btn_3d.setCheckable(True)
        self.btn_3d.setFixedSize(46, 22)
        self.btn_3d.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_3d.setToolTip("切换 2D 平面 / 3D 立体声学空间模式")
        self.btn_3d.setStyleSheet(f"""
            QPushButton {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 11px;
                font-size: 11px;
                font-weight: 700;
            }}
            QPushButton:hover {{ color: #FFFFFF; }}
            QPushButton:checked {{
                background-color: #1A1429;
                color: {PALETTE["violet"]};
                border: 1px solid {PALETTE["violet"]};
            }}
        """)
        self.btn_3d.clicked.connect(self._on_3d_toggled)
        tb.addWidget(self.btn_3d)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(46, 22)
        reset_btn.clicked.connect(self.reset_panner)
        tb.addWidget(reset_btn)

        root.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 主体工作区：左侧控制卡片列 + 右侧空间声场画布
        # -------------------------------------------------------------
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        def _vsep() -> QFrame:
            f = QFrame()
            f.setFixedWidth(1)
            f.setStyleSheet("background-color: #141416; border: none;")
            return f

        # 左侧固定宽度参数列
        left_panel = QWidget()
        left_panel.setFixedWidth(182)
        left_lay = QVBoxLayout(left_panel)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)

        # --- 卡片 1: Left / Right ---
        self.card_lr = PannerControlCard("Left / Right", PALETTE["blue"])
        self.pill_lr = EditableValuePill("C", PALETTE["blue"])
        self.pill_lr.valueEdited.connect(self._on_lr_text)
        self.card_lr.extra_slot.addWidget(self.pill_lr)

        self.knob_lr = ProRotaryKnob(-100.0, 100.0, 0.0, 0.0, PALETTE["blue"], mode="bipolar")
        self.knob_lr.valueChanged.connect(self._on_lr_knob)
        self.card_lr.right_col.addWidget(self.knob_lr)
        self.card_lr.right_col.addLayout(self._make_hint_row("L", "R"))
        left_lay.addWidget(self.card_lr)

        # --- 卡片 2: Front / Back ---
        self.card_fb = PannerControlCard("Front / Back", PALETTE["cyan"])
        self.pill_fb = EditableValuePill("100F", PALETTE["cyan"])
        self.pill_fb.valueEdited.connect(self._on_fb_text)
        self.card_fb.extra_slot.addWidget(self.pill_fb)

        # invert=True 使得向左/逆时针为 F(+100)，向右/顺时针为 B(-100)，与达芬奇操作直觉完全一致
        self.knob_fb = ProRotaryKnob(-100.0, 100.0, 100.0, 100.0, PALETTE["cyan"], mode="bipolar", invert=True)
        self.knob_fb.valueChanged.connect(self._on_fb_knob)
        self.card_fb.right_col.addWidget(self.knob_fb)
        self.card_fb.right_col.addLayout(self._make_hint_row("F", "B"))
        left_lay.addWidget(self.card_fb)

        # --- 卡片 3: Elevation / Height (3D 模式激活高亮) ---
        self.card_elev = PannerControlCard("Height (Z)", PALETTE["violet"])
        self.pill_elev = EditableValuePill("0", PALETTE["violet"])
        self.pill_elev.valueEdited.connect(self._on_elev_text)
        self.card_elev.extra_slot.addWidget(self.pill_elev)

        self.knob_elev = ProRotaryKnob(0.0, 100.0, 0.0, 0.0, PALETTE["violet"], mode="linear")
        self.knob_elev.valueChanged.connect(self._on_elev_knob)
        self.card_elev.right_col.addWidget(self.knob_elev)
        self.card_elev.right_col.addLayout(self._make_hint_row("0", "100T"))
        left_lay.addWidget(self.card_elev)

        # --- 卡片 4: Rotate ---
        self.card_rot = PannerControlCard("Rotate", PALETTE["amber"])
        self.pill_rot = EditableValuePill("0°", PALETTE["amber"])
        self.pill_rot.valueEdited.connect(self._on_rot_text)
        self.card_rot.extra_slot.addWidget(self.pill_rot)

        self.knob_rot = ProRotaryKnob(0.0, 360.0, 0.0, 0.0, PALETTE["amber"], mode="circular")
        self.knob_rot.valueChanged.connect(self._on_rot_knob)
        self.card_rot.right_col.addWidget(self.knob_rot)
        self.card_rot.right_col.addLayout(self._make_hint_row("0", "360"))
        left_lay.addWidget(self.card_rot)

        # --- 卡片 5: Spread ---
        self.card_spread = PannerControlCard("Spread", PALETTE["green"])
        self.btn_spread_auto = self.card_spread.build_pill_btn("Auto", True, PALETTE["green"])
        self.btn_spread_auto.clicked.connect(self._on_spread_auto_clicked)
        self.pill_spread = EditableValuePill("FULL", PALETTE["green"])
        self.pill_spread.valueEdited.connect(self._on_spread_text)
        self.card_spread.extra_slot.addWidget(self.btn_spread_auto)
        self.card_spread.extra_slot.addWidget(self.pill_spread)

        self.knob_spread = ProRotaryKnob(0.0, 100.0, 100.0, 100.0, PALETTE["green"], mode="linear")
        self.knob_spread.valueChanged.connect(self._on_spread_knob)
        self.card_spread.right_col.addWidget(self.knob_spread)
        self.card_spread.right_col.addLayout(self._make_hint_row("PNT", "FULL"))
        left_lay.addWidget(self.card_spread)

        # --- 卡片 6: Divergence ---
        self.card_div = PannerControlCard("Divergence", PALETTE["violet"])
        self.btn_div_mode = self.card_div.build_pill_btn("2D", False, PALETTE["violet"])
        self.btn_div_mode.clicked.connect(self._on_div_mode_clicked)
        self.pill_div = EditableValuePill("0", PALETTE["violet"])
        self.pill_div.valueEdited.connect(self._on_div_text)
        self.card_div.extra_slot.addWidget(self.btn_div_mode)
        self.card_div.extra_slot.addWidget(self.pill_div)

        self.knob_div = ProRotaryKnob(0.0, 100.0, 0.0, 0.0, PALETTE["violet"], mode="linear")
        self.knob_div.valueChanged.connect(self._on_div_knob)
        self.card_div.right_col.addWidget(self.knob_div)
        self.card_div.right_col.addLayout(self._make_hint_row("0", "100"))
        left_lay.addWidget(self.card_div)

        # --- 卡片 7: Boom (LFE Send) ---
        self.card_boom = PannerControlCard("Boom", PALETTE["coral"])
        self.btn_boom_on = self.card_boom.build_pill_btn("On", True, PALETTE["coral"])
        self.btn_boom_on.clicked.connect(self._on_boom_on_clicked)
        self.btn_boom_route = self.card_boom.build_pill_btn("Pre", False, PALETTE["amber"])
        self.btn_boom_route.setToolTip("点击切换 LFE 发送路由：Pre (推子前) / Post (推子后) / Only (仅 LFE)")
        self.btn_boom_route.clicked.connect(self._on_boom_route_clicked)
        self.card_boom.extra_slot.addWidget(self.btn_boom_on)
        self.card_boom.extra_slot.addWidget(self.btn_boom_route)

        self.knob_boom = ProRotaryKnob(-60.0, 10.0, -60.0, -60.0, PALETTE["coral"], mode="linear")
        self.knob_boom.valueChanged.connect(self._on_boom_knob)
        self.pill_boom = EditableValuePill("-∞", PALETTE["coral"])
        self.pill_boom.valueEdited.connect(self._on_boom_text)
        self.card_boom.right_col.addWidget(self.knob_boom)
        self.card_boom.right_col.addLayout(self._make_hint_row("-∞", "+10"))
        self.card_boom.right_col.addWidget(self.pill_boom)
        left_lay.addWidget(self.card_boom)

        left_lay.addStretch()
        body.addWidget(left_panel)

        body.addWidget(_vsep())

        # 右侧：2D / 3D 交互式空间声场舞台
        self.stage = SpatialSoundStageWidget()
        self.stage.xyDragged.connect(self._on_stage_xy_dragged)
        self.stage.elevationDragged.connect(self._on_stage_elev_dragged)
        self.stage.resetPositionRequested.connect(self._reset_stage_position)
        self.stage.toggle3DRequested.connect(self._toggle_3d_from_stage)
        body.addWidget(self.stage, 1)

        root.addLayout(body, 1)

    def _make_hint_row(self, left_t: str, right_t: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        l1 = QLabel(left_t)
        l1.setObjectName("HintText")
        l2 = QLabel(right_t)
        l2.setObjectName("HintText")
        row.addWidget(l1)
        row.addStretch()
        row.addWidget(l2)
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

    def _refresh_all_views(self):
        self._updating = True
        st = self._st()
        en = st["enabled"]
        is_3d = (st["space_mode"] == "3D")

        self._update_ab_styles()
        self.power_switch.setChecked(en)

        self.format_combo.blockSignals(True)
        self.format_combo.setCurrentText(st["format"])
        self.format_combo.blockSignals(False)

        self.btn_3d.setChecked(is_3d)

        # Left / Right
        self.card_lr.setCardEnabled(en)
        self.knob_lr.setValue(st["lr"])
        self.knob_lr.setControlActive(en)
        self.pill_lr.setText(fmt_lr(st["lr"]))
        self.pill_lr.setControlActive(en)

        # Front / Back
        self.card_fb.setCardEnabled(en)
        self.knob_fb.setValue(st["fb"])
        self.knob_fb.setControlActive(en)
        self.pill_fb.setText(fmt_fb(st["fb"]))
        self.pill_fb.setControlActive(en)

        # Height (Z) — 仅在 3D 模式下激活高亮
        elev_en = en and is_3d
        self.card_elev.setCardEnabled(elev_en)
        self.knob_elev.setValue(st["elevation"])
        self.knob_elev.setControlActive(elev_en)
        self.pill_elev.setText(fmt_elev(st["elevation"]))
        self.pill_elev.setControlActive(elev_en)

        # Rotate
        self.card_rot.setCardEnabled(en)
        self.knob_rot.setValue(st["rotate"])
        self.knob_rot.setControlActive(en)
        self.pill_rot.setText(fmt_rotate(st["rotate"]))
        self.pill_rot.setControlActive(en)

        # Spread
        self.card_spread.setCardEnabled(en)
        is_auto = st["spread_auto"]
        self.btn_spread_auto.setChecked(is_auto)
        eff_spread = (self.stage._effective_spread() * 100.0) if is_auto else st["spread"]
        self.knob_spread.setValue(eff_spread)
        self.knob_spread.setControlActive(en and not is_auto)
        self.pill_spread.setText(fmt_spread(eff_spread, is_auto))
        self.pill_spread.setControlActive(en)

        # Divergence
        self.card_div.setCardEnabled(en)
        self.btn_div_mode.setText(st["div_mode"])
        self.knob_div.setValue(st["divergence"])
        self.knob_div.setControlActive(en)
        self.pill_div.setText(f"{int(round(st['divergence']))}")
        self.pill_div.setControlActive(en)

        # Boom
        boom_active = en and st["boom_on"]
        self.card_boom.setCardEnabled(boom_active)
        self.btn_boom_on.setChecked(st["boom_on"])
        self.btn_boom_route.setText(st["boom_route"])
        self.knob_boom.setValue(st["boom_gain"])
        self.knob_boom.setControlActive(boom_active)
        self.pill_boom.setText(fmt_boom(st["boom_gain"]))
        self.pill_boom.setControlActive(boom_active)

        # 同步声场画布
        self.stage.setState(st)
        self._updating = False

    # -----------------------------------------------------------------
    # 交互回调与信号分发
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
        self.panBypassChanged.emit(checked)
        self.stateChanged.emit(self.get_state())

    def _on_format_changed(self, fmt: str):
        if self._updating:
            return
        self._st()["format"] = fmt
        if fmt == "7.1.4" and self._st()["space_mode"] != "3D":
            self._st()["space_mode"] = "3D"
            self.spaceModeChanged.emit("3D")
        self._refresh_all_views()
        self.channelFormatChanged.emit(fmt)
        self.stateChanged.emit(self.get_state())

    def _on_3d_toggled(self, checked: bool):
        if self._updating:
            return
        mode = "3D" if checked else "2D"
        self._st()["space_mode"] = mode
        self._refresh_all_views()
        self.spaceModeChanged.emit(mode)
        self.stateChanged.emit(self.get_state())

    def _toggle_3d_from_stage(self):
        self._on_3d_toggled(self._st()["space_mode"] != "3D")

    def _emit_pos(self):
        st = self._st()
        self.panPositionChanged.emit(st["lr"], st["fb"], st["elevation"])
        self.stateChanged.emit(self.get_state())

    def _on_lr_knob(self, val: float):
        self._st()["lr"] = round(val, 1)
        self._refresh_all_views()
        self._emit_pos()

    def _on_lr_text(self, text: str):
        self._st()["lr"] = parse_lr(text, self._st()["lr"])
        self._refresh_all_views()
        self._emit_pos()

    def _on_fb_knob(self, val: float):
        self._st()["fb"] = round(val, 1)
        self._refresh_all_views()
        self._emit_pos()

    def _on_fb_text(self, text: str):
        self._st()["fb"] = parse_fb(text, self._st()["fb"])
        self._refresh_all_views()
        self._emit_pos()

    def _on_elev_knob(self, val: float):
        self._st()["elevation"] = round(val, 1)
        if self._st()["space_mode"] != "3D":
            self._st()["space_mode"] = "3D"
            self.spaceModeChanged.emit("3D")
        self._refresh_all_views()
        self._emit_pos()

    def _on_elev_text(self, text: str):
        self._st()["elevation"] = parse_elev(text, self._st()["elevation"])
        if self._st()["space_mode"] != "3D":
            self._st()["space_mode"] = "3D"
            self.spaceModeChanged.emit("3D")
        self._refresh_all_views()
        self._emit_pos()

    def _on_stage_xy_dragged(self, lr: float, fb: float):
        self._st()["lr"] = lr
        self._st()["fb"] = fb
        self._refresh_all_views()
        self._emit_pos()

    def _on_stage_elev_dragged(self, elev: float):
        self._st()["elevation"] = elev
        self._refresh_all_views()
        self._emit_pos()

    def _reset_stage_position(self):
        self._st()["lr"] = 0.0
        self._st()["fb"] = 100.0
        self._st()["elevation"] = 0.0
        self._refresh_all_views()
        self._emit_pos()

    def _on_rot_knob(self, val: float):
        self._st()["rotate"] = round(val, 1) % 360.0
        self._refresh_all_views()
        self.rotateChanged.emit(self._st()["rotate"])
        self.stateChanged.emit(self.get_state())

    def _on_rot_text(self, text: str):
        self._st()["rotate"] = parse_rotate(text, self._st()["rotate"])
        self._refresh_all_views()
        self.rotateChanged.emit(self._st()["rotate"])
        self.stateChanged.emit(self.get_state())

    def _on_spread_auto_clicked(self, checked: bool):
        self._st()["spread_auto"] = checked
        self._refresh_all_views()
        self.spreadChanged.emit(self._st()["spread"], checked)
        self.stateChanged.emit(self.get_state())

    def _on_spread_knob(self, val: float):
        # 手动调节旋钮时自动解除 Auto 锁定
        self._st()["spread_auto"] = False
        self._st()["spread"] = round(val, 1)
        self._refresh_all_views()
        self.spreadChanged.emit(self._st()["spread"], False)
        self.stateChanged.emit(self.get_state())

    def _on_spread_text(self, text: str):
        self._st()["spread_auto"] = False
        self._st()["spread"] = parse_spread(text, self._st()["spread"])
        self._refresh_all_views()
        self.spreadChanged.emit(self._st()["spread"], False)
        self.stateChanged.emit(self.get_state())

    def _on_div_mode_clicked(self):
        curr = self._st()["div_mode"]
        nxt = "3D" if curr == "2D" else "2D"
        self._st()["div_mode"] = nxt
        self._refresh_all_views()
        self.divergenceChanged.emit(self._st()["divergence"], nxt)
        self.stateChanged.emit(self.get_state())

    def _on_div_knob(self, val: float):
        self._st()["divergence"] = round(val, 1)
        self._refresh_all_views()
        self.divergenceChanged.emit(self._st()["divergence"], self._st()["div_mode"])
        self.stateChanged.emit(self.get_state())

    def _on_div_text(self, text: str):
        try:
            v = float(text.strip().replace("%", ""))
            self._st()["divergence"] = max(0.0, min(100.0, round(v, 1)))
            self._refresh_all_views()
            self.divergenceChanged.emit(self._st()["divergence"], self._st()["div_mode"])
            self.stateChanged.emit(self.get_state())
        except ValueError:
            pass

    def _on_boom_on_clicked(self, checked: bool):
        self._st()["boom_on"] = checked
        self._refresh_all_views()
        st = self._st()
        self.boomChanged.emit(st["boom_on"], st["boom_route"], st["boom_gain"])
        self.stateChanged.emit(self.get_state())

    def _on_boom_route_clicked(self):
        order = ["Pre", "Post", "Only"]
        curr = self._st()["boom_route"]
        nxt = order[(order.index(curr) + 1) % len(order)]
        self._st()["boom_route"] = nxt
        self._refresh_all_views()
        st = self._st()
        self.boomChanged.emit(st["boom_on"], st["boom_route"], st["boom_gain"])
        self.stateChanged.emit(self.get_state())

    def _on_boom_knob(self, val: float):
        self._st()["boom_gain"] = round(val, 1)
        self._refresh_all_views()
        st = self._st()
        self.boomChanged.emit(st["boom_on"], st["boom_route"], st["boom_gain"])
        self.stateChanged.emit(self.get_state())

    def _on_boom_text(self, text: str):
        self._st()["boom_gain"] = parse_boom(text, self._st()["boom_gain"])
        self._refresh_all_views()
        st = self._st()
        self.boomChanged.emit(st["boom_on"], st["boom_route"], st["boom_gain"])
        self.stateChanged.emit(self.get_state())

    def _load_preset(self, name: str):
        if name in self._presets:
            self._banks[self._active_bank] = copy.deepcopy(self._presets[name])
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存声像预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._st())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 公开 API：供外部音频引擎 / 工程序列化调用
    # -----------------------------------------------------------------
    def reset_panner(self):
        keep_fmt = self._st()["format"]
        keep_mode = self._st()["space_mode"]
        self._banks[self._active_bank] = default_panner_state()
        self._banks[self._active_bank]["format"] = keep_fmt
        self._banks[self._active_bank]["space_mode"] = keep_mode
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._st())

    def set_state(self, state: dict):
        self._banks[self._active_bank] = copy.deepcopy(state)
        self._refresh_all_views()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioSpatialPannerWidget("Audio 1")
    win.show()
    sys.exit(app.exec())
