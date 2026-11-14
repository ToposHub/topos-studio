"""Spatial Panner Dialog — 空间声像器对话框（CW-A1 新增）。

自主旗舰空间声像器：2D 声场俯视矩阵 / 3D 透视声学空间舞台（拖拽定位、
Shift/滚轮调高度、Ctrl/Alt 锁轴、双击复位、右键预设菜单）+ 7 张控制卡片
（L/R、F/B、Height(Z)、Rotate、Spread(+Auto)、Divergence、Boom/LFE 发送）
+ 扬声器制式（2.0/5.1/7.1/7.1.4）与 2D/3D 视图切换。

立体声域实现说明（与 DSP 一致）：L/R、Spread、Rotate、Divergence、Boom
在立体声输出上真实可听；F/B、Elevation、制式先完整持久化并参与可视化，
待多声道引擎后启用。

对接项目契约：
  - 构造：``(track_id, track_name, initial_pan: float, initial_panner: Optional[dict], parent)``
  - 信号：``panner_preview`` / ``panner_committed`` (track_id, {"pan": -1..1, "panner": dict})
    —— pan 部分由宿主走 SetTrackPanCommand，panner 部分走 SetTrackPannerCommand
  - 三段节流：舞台/旋钮拖动期 preview，松手/开关/预设 committed
"""

from __future__ import annotations

import copy
import logging
import math
from typing import Any, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.core.timeline_v2 import SpatialPannerParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import (
    EditableValuePill,
    PowerToggleSwitch,
    apply_float_panel,
    glyph_icon,
    ProRotaryKnob,
    PRO_DIALOG_QSS,
    parse_float_clean,
)
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

logger = logging.getLogger(__name__)

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


# ── 数值格式化 / 自然语言解析 ────────────────────────────────────────────────

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
    return f"{int(round(val)) % 360}°"


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


# ═════════════════════════════════════════════════════════════════════════════
# 参数控制卡片
# ═════════════════════════════════════════════════════════════════════════════

class PannerControlCard(QFrame):
    """单项声像模块卡片（左侧标题+辅助按钮+胶囊，右侧旋钮+刻度）。"""

    def __init__(self, title: str, accent_color: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._accent = accent_color
        self._enabled = True

        self.setObjectName("PanCard")
        self._apply_style()

        main_lay = QHBoxLayout(self)
        main_lay.setContentsMargins(10, 7, 10, 7)
        main_lay.setSpacing(8)

        self.left_col = QVBoxLayout()
        self.left_col.setSpacing(5)

        self.title_lbl = QLabel(title)
        self.title_lbl.setObjectName("CardTitle")
        self.left_col.addWidget(self.title_lbl)

        self.extra_slot = QVBoxLayout()
        self.extra_slot.setSpacing(4)
        self.left_col.addLayout(self.extra_slot)
        self.left_col.addStretch()

        self.right_col = QVBoxLayout()
        self.right_col.setSpacing(2)
        self.right_col.setAlignment(Qt.AlignmentFlag.AlignCenter)

        main_lay.addLayout(self.left_col, 1)
        main_lay.addLayout(self.right_col)

    def setCardEnabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._apply_style()

    def _apply_style(self) -> None:
        top_col = self._accent if self._enabled else "#343844"
        title_col = "#B6BCCB" if self._enabled else "#5E6370"
        self.setStyleSheet(f"""
            QFrame#PanCard {{
                background-color: #1D1F25;
                border: 1px solid #2A2D37;
                border-left: 2.5px solid {top_col};
                border-radius: 5px;
            }}
            QLabel#CardTitle {{
                color: {title_col};
                font-size: 11px;
                font-weight: 700;
            }}
            QLabel#HintText {{
                color: #6E7382;
                font-size: 9px;
                font-weight: 600;
            }}
        """)

    def build_pill_btn(self, text: str, checkable: bool = True,
                       accent: Optional[str] = None) -> QPushButton:
        ac = accent or self._accent
        btn = QPushButton(text)
        btn.setCheckable(checkable)
        btn.setFixedSize(58, 20)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #252830;
                color: #8E94A4;
                border: 1px solid #323642;
                border-radius: 10px;
                font-size: 10px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: #2E323D;
                color: #E6E9F2;
            }}
            QPushButton:checked {{
                background-color: #121820;
                color: {ac};
                border: 1px solid {ac};
            }}
        """)
        return btn


# ═════════════════════════════════════════════════════════════════════════════
# 2D/3D 声场舞台
# ═════════════════════════════════════════════════════════════════════════════

class SpatialSoundStageWidget(QWidget):
    """2D 声场俯视矩阵 / 3D 透视声学空间舞台。"""

    xyDragged = pyqtSignal(float, float)          # lr, fb
    xyDragEnded = pyqtSignal()
    elevationDragged = pyqtSignal(float)          # elevation
    elevationDragEnded = pyqtSignal()
    resetPositionRequested = pyqtSignal()
    toggle3DRequested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 420)
        self.setMouseTracking(True)

        # 状态 = SpatialPannerParams dict + "lr"（pan 真源以 UI 单位 -100..100 呈现）
        self._state: dict = {**SpatialPannerParams.default().to_dict(), "lr": 0.0}
        self._dragging = False
        self._drag_mode = "xy"
        self._drag_start_pos = QPointF()
        self._drag_start_lr = 0.0
        self._drag_start_fb = 100.0
        self._drag_start_elev = 0.0
        self._hover_anchor = False

        self._stage_rect = QRectF()

    def setState(self, state: dict) -> None:
        self._state = copy.deepcopy(state)
        self.update()

    # ── 声学运动学 ────────────────────────────────────────────

    def _effective_spread(self) -> float:
        x_norm = abs(self._state["lr"]) / 100.0
        y_norm = abs(self._state["fb"]) / 100.0
        if self._state.get("spread_auto"):
            auto_s = max(0.0, (1.0 - x_norm) * (1.0 - 0.35 * y_norm))
            return auto_s
        return self._state["spread"] / 100.0

    def _compute_virtual_nodes(self) -> tuple:
        """返回 (anchor_norm, left_sat_norm, right_sat_norm)，坐标范围 [-1, 1]。"""
        ax = self._state["lr"] / 100.0
        ay = self._state["fb"] / 100.0
        s = self._effective_spread()
        theta = math.radians(-self._state["rotate"])

        dx = s * math.cos(theta)
        dy = s * math.sin(theta)

        lx = max(-1.0, min(1.0, ax - dx))
        ly = max(-1.0, min(1.0, ay - dy))
        rx = max(-1.0, min(1.0, ax + dx))
        ry = max(-1.0, min(1.0, ay + dy))

        return QPointF(ax, ay), QPointF(lx, ly), QPointF(rx, ry)

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

    def _2d_to_norm(self, pt: QPointF, rect: QRectF) -> tuple:
        nx = (pt.x() - rect.center().x()) / max(1.0, rect.width() * 0.5)
        ny = (rect.center().y() - pt.y()) / max(1.0, rect.height() * 0.5)
        return max(-1.0, min(1.0, nx)), max(-1.0, min(1.0, ny))

    def _project_3d(self, nx: float, ny: float, nz: float, rect: QRectF) -> QPointF:
        """空间归一化坐标 → 2.5D 透视声学房间视角。"""
        cx = rect.center().x()
        cy = rect.center().y() + rect.height() * 0.14
        w_half = rect.width() * 0.44
        depth_scale = 1.0 - 0.24 * ((ny + 1.0) * 0.5)
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

    # ── 交互 ─────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802
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
                anchor_pt = self._screen_anchor_pos(rect)
                if math.hypot(pos.x() - anchor_pt.x(), pos.y() - anchor_pt.y()) > 16.0:
                    self._update_xy_from_pos(pos, rect, event.modifiers())

        elif event.button() == Qt.MouseButton.RightButton:
            self._open_context_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
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

    def _update_xy_from_pos(self, pos: QPointF, rect: QRectF, mods) -> None:
        if self._state["space_mode"] == "3D":
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

        if mods & Qt.KeyboardModifier.ControlModifier:
            new_lr = self._drag_start_lr
        if mods & Qt.KeyboardModifier.AltModifier:
            new_fb = self._drag_start_fb

        self.xyDragged.emit(new_lr, new_fb)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._hover_anchor
                           else Qt.CursorShape.CrossCursor)
            if self._drag_mode == "z":
                self.elevationDragEnded.emit()
            else:
                self.xyDragEnded.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.resetPositionRequested.emit()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._state["space_mode"] == "3D":
            delta = event.angleDelta().y() / 120.0
            new_elev = max(0.0, min(100.0, round(self._state["elevation"] + delta * 4.0, 1)))
            self.elevationDragged.emit(new_elev)
            event.accept()

    def _open_context_menu(self, global_pos) -> None:
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
            (I18N.tr("audio.fx.pan_front_center", "前置正中 (C, 100F)"), 0.0, 100.0),
            (I18N.tr("audio.fx.pan_center", "声场正心 (C, C)"), 0.0, 0.0),
            (I18N.tr("audio.fx.pan_front_left", "左前音箱 (100L, 100F)"), -100.0, 100.0),
            (I18N.tr("audio.fx.pan_front_right", "右前音箱 (100R, 100F)"), 100.0, 100.0),
            (I18N.tr("audio.fx.pan_rear_center", "后环绕正中 (C, 100B)"), 0.0, -100.0),
        ]
        for label, lr, fb in presets:
            act = menu.addAction(label)
            act.triggered.connect(lambda checked=False, x=lr, y=fb: self.xyDragged.emit(x, y))
        menu.addSeparator()
        act_3d = menu.addAction(I18N.tr("audio.fx.toggle_2d3d", "切换 2D / 3D 空间视图"))
        act_3d.triggered.connect(self.toggle3DRequested.emit)
        menu.exec(global_pos)

    # ── 绘制 ─────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self._compute_stage_rect()
        self._stage_rect = rect
        en = self._state.get("enabled", True)
        is_3d = (self._state["space_mode"] == "3D")

        panel_rect = QRectF(4, 4, self.width() - 8, self.height() - 8)
        bg_grad = QLinearGradient(panel_rect.topLeft(), panel_rect.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#181A20"))
        bg_grad.setColorAt(1.0, QColor("#121419"))
        p.setPen(QPen(QColor("#262934"), 1.0))
        p.setBrush(bg_grad)
        p.drawRoundedRect(panel_rect, 6.0, 6.0)

        if is_3d:
            self._paint_3d_stage(p, rect, en)
        else:
            self._paint_2d_stage(p, rect, en)

        # 左上角实时声像坐标 HUD
        lr_s = fmt_lr(self._state["lr"])
        fb_s = fmt_fb(self._state["fb"])
        rot_s = fmt_rotate(self._state["rotate"])
        if is_3d:
            z_s = fmt_elev(self._state["elevation"])
            hud_text = f"L/R: {lr_s}  •  F/B: {fb_s}  •  Z: {z_s}  •  Rot: {rot_s}"
            hud_w = 248
        else:
            hud_text = f"L/R: {lr_s}   •   F/B: {fb_s}   •   Rot: {rot_s}"
            hud_w = 205

        hud_rect = QRectF(14, 12, hud_w, 20)
        p.setPen(QPen(QColor("#323642"), 1.0))
        p.setBrush(QColor(16, 18, 23, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor("#28D8F0") if en else QColor("#747988"))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_text)

        hint_str = (I18N.tr("audio.fx.stage_hint_3d", "Shift+拖拽 / 滚轮: 调节高度") if is_3d
                    else I18N.tr("audio.fx.stage_hint_2d", "双击复位  •  Ctrl/Alt 锁定单轴"))
        p.setPen(QColor("#5E6372"))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(self.width() - 195, 12, 182, 20),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, hint_str)

    def _paint_2d_stage(self, p: QPainter, rect: QRectF, en: bool) -> None:
        grid_pen = QPen(QColor("#232630"), 1.0)
        axis_pen = QPen(QColor("#363B4A"), 1.15)
        cells = 6
        for i in range(cells + 1):
            t = i / cells
            x = rect.left() + t * rect.width()
            y = rect.top() + t * rect.height()
            p.setPen(axis_pen if i == 3 else grid_pen)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

        inner = QRectF(
            rect.left() + rect.width() * 0.25,
            rect.top() + rect.height() * 0.25,
            rect.width() * 0.5,
            rect.height() * 0.5,
        )
        p.setPen(QPen(QColor("#343948"), 1.0, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(inner)

        p.setPen(QPen(QColor("#323746"), 1.1))
        p.drawRect(rect)

        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.setPen(QColor("#787E8E"))
        cx, cy = rect.center().x(), rect.center().y()
        p.drawText(QRectF(cx - 14, rect.top() - 18, 28, 16), Qt.AlignmentFlag.AlignCenter, "F")
        p.drawText(QRectF(cx - 14, rect.bottom() + 2, 28, 16), Qt.AlignmentFlag.AlignCenter, "B")
        p.drawText(QRectF(rect.left() - 20, cy - 8, 16, 16), Qt.AlignmentFlag.AlignCenter, "L")
        p.drawText(QRectF(rect.right() + 4, cy - 8, 16, 16), Qt.AlignmentFlag.AlignCenter, "R")

        speakers = SPEAKER_LAYOUTS.get(self._state["format"], [])
        for spk_name, sx, sy, sz in speakers:
            if sz > 0.5:
                continue  # 2D 视图仅绘制平面层音箱
            spt = self._norm_to_2d(sx, sy, rect)
            p.setPen(QPen(QColor("#484E60"), 1.0))
            p.setBrush(QColor("#1A1D24"))
            p.drawRoundedRect(QRectF(spt.x() - 11, spt.y() - 7, 22, 14), 2.5, 2.5)
            p.setPen(QColor("#6E7588"))
            p.setFont(QFont("Segoe UI", 7))
            p.drawText(QRectF(spt.x() - 11, spt.y() - 7, 22, 14),
                       Qt.AlignmentFlag.AlignCenter, spk_name)

        a_norm, l_norm, r_norm = self._compute_virtual_nodes()
        a_pt = self._norm_to_2d(a_norm.x(), a_norm.y(), rect)
        l_pt = self._norm_to_2d(l_norm.x(), l_norm.y(), rect)
        r_pt = self._norm_to_2d(r_norm.x(), r_norm.y(), rect)

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

        line_col = QColor(76, 212, 144, 120) if en else QColor(90, 95, 110, 80)
        p.setPen(QPen(line_col, 1.35))
        p.drawLine(l_pt, a_pt)
        p.drawLine(a_pt, r_pt)

        l_col = QColor(PALETTE["green"]) if en else QColor("#5A5F6E")
        r_col = QColor(PALETTE["amber"]) if en else QColor("#5A5F6E")
        for spt, scol in ((l_pt, l_col), (r_pt, r_col)):
            p.setPen(QPen(QColor("#111318"), 1.2))
            p.setBrush(scol)
            p.drawEllipse(spt, 5.2, 5.2)

        c_col = QColor(PALETTE["cyan"]) if en else QColor("#686D7C")
        if self._hover_anchor or self._dragging:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(40, 216, 240, 55))
            p.drawEllipse(a_pt, 14.0, 14.0)

        p.setPen(QPen(QColor("#0F1218"), 1.4))
        p.setBrush(c_col)
        p.drawRoundedRect(QRectF(a_pt.x() - 5.5, a_pt.y() - 5.5, 11.0, 11.0), 2.0, 2.0)

    def _paint_3d_stage(self, p: QPainter, rect: QRectF, en: bool) -> None:
        """3D 透视声学房间立方体与高度投影线。"""
        f_fl = self._project_3d(-1,  1, 0, rect)
        f_fr = self._project_3d( 1,  1, 0, rect)
        f_br = self._project_3d( 1, -1, 0, rect)
        f_bl = self._project_3d(-1, -1, 0, rect)

        c_fl = self._project_3d(-1,  1, 1, rect)
        c_fr = self._project_3d( 1,  1, 1, rect)
        c_br = self._project_3d( 1, -1, 1, rect)
        c_bl = self._project_3d(-1, -1, 1, rect)

        floor_poly = QPolygonF([f_fl, f_fr, f_br, f_bl])
        p.setPen(QPen(QColor("#363C4D"), 1.1))
        p.setBrush(QColor(26, 30, 40, 140))
        p.drawPolygon(floor_poly)

        p.setPen(QPen(QColor("#252936"), 1.0))
        for i in range(1, 4):
            t = -1.0 + i * 0.5
            p.drawLine(self._project_3d(t, -1, 0, rect), self._project_3d(t, 1, 0, rect))
            p.drawLine(self._project_3d(-1, t, 0, rect), self._project_3d(1, t, 0, rect))

        p.setPen(QPen(QColor("#2E3342"), 1.0, Qt.PenStyle.DashLine))
        for fp, cp in ((f_fl, c_fl), (f_fr, c_fr), (f_br, c_br), (f_bl, c_bl)):
            p.drawLine(fp, cp)

        ceil_poly = QPolygonF([c_fl, c_fr, c_br, c_bl])
        p.setPen(QPen(QColor("#3A4154"), 1.0))
        p.setBrush(QColor(40, 216, 240, 10))
        p.drawPolygon(ceil_poly)

        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.setPen(QColor("#787E8E"))
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
        p.drawText(QRectF(top_mid.x() - 24, top_mid.y() - 8, 48, 16),
                   Qt.AlignmentFlag.AlignCenter, "TOP (Z)")

        nz = self._state["elevation"] / 100.0
        a_norm, l_norm, r_norm = self._compute_virtual_nodes()

        floor_pt = self._project_3d(a_norm.x(), a_norm.y(), 0.0, rect)
        a_pt = self._project_3d(a_norm.x(), a_norm.y(), nz, rect)
        l_pt = self._project_3d(l_norm.x(), l_norm.y(), nz, rect)
        r_pt = self._project_3d(r_norm.x(), r_norm.y(), nz, rect)

        p.setPen(QPen(QColor(182, 124, 248, 160), 1.2, Qt.PenStyle.DashLine))
        p.drawLine(floor_pt, a_pt)
        p.setPen(QPen(QColor("#5C6275"), 1.0))
        p.drawEllipse(floor_pt, 4.0, 2.5)

        div_norm = self._state["divergence"] / 100.0
        if div_norm > 0.01 and en:
            halo_r = 14.0 + div_norm * (rect.width() * 0.28)
            rad_grad = QRadialGradient(a_pt, halo_r)
            rad_grad.setColorAt(0.0, QColor(182, 124, 248, int(75 * div_norm)))
            rad_grad.setColorAt(1.0, QColor(40, 216, 240, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rad_grad)
            p.drawEllipse(a_pt, halo_r, halo_r)

        p.setPen(QPen(QColor(76, 212, 144, 140) if en else QColor("#555A68"), 1.4))
        p.drawLine(l_pt, a_pt)
        p.drawLine(a_pt, r_pt)

        for spt, col_hex in ((l_pt, PALETTE["green"]), (r_pt, PALETTE["amber"])):
            p.setPen(QPen(QColor("#111318"), 1.2))
            p.setBrush(QColor(col_hex) if en else QColor("#5A5F6E"))
            p.drawEllipse(spt, 5.0, 5.0)

        c_col = QColor(PALETTE["cyan"]) if en else QColor("#686D7C")
        sphere_grad = QRadialGradient(a_pt.x() - 2.5, a_pt.y() - 2.5, 10.0)
        sphere_grad.setColorAt(0.0, QColor("#FFFFFF"))
        sphere_grad.setColorAt(0.35, c_col)
        sphere_grad.setColorAt(1.0, QColor("#0E4A56"))
        p.setPen(QPen(QColor("#0D1016"), 1.2))
        p.setBrush(sphere_grad)
        p.drawEllipse(a_pt, 6.8, 6.8)


# ═════════════════════════════════════════════════════════════════════════════
# 主对话框
# ═════════════════════════════════════════════════════════════════════════════

class SpatialPannerDialog(QDialog):
    """空间声像器对话框（2D/3D 声场舞台 + 7 参数卡片）。"""

    panner_preview = pyqtSignal(str, object)    # 拖动期实时听感
    panner_committed = pyqtSignal(str, object)  # 松手/开关/预设，进命令栈
    # payload: {"pan": -1..1, "panner": SpatialPannerParams dict}

    def __init__(
        self,
        track_id: str,
        track_name: str,
        initial_pan: float = 0.0,
        initial_panner: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._dragging = False
        self._updating = False
        # ST-01: 用户预设跨会话持久化（关窗口/重启/新工程均可加载）
        self._effect_type = "panner"
        self._custom_presets: list = []
        try:
            from src.features.edit.core.effect_preset import load_user_presets
            for entry in load_user_presets(self._effect_type):
                state = dict(entry["params"])
                if isinstance(state, dict) and state:
                    self._custom_presets.append(
                        {"name": str(entry["name"]), "state": state})
        except Exception as exc:  # noqa: BLE001 — 预设库不可读不阻断对话框
            logger.warning("panner dialog: user presets unavailable: %r", exc)

        panner = initial_panner if isinstance(initial_panner, dict) and initial_panner \
            else SpatialPannerParams.default().to_dict()
        # lr（pan 真源）以 UI 单位 -100..100 存于内部状态
        self._state: dict = {**SpatialPannerParams.from_dict(panner).to_dict(),
                             "lr": max(-100.0, min(100.0, initial_pan * 100.0))}

        self.setWindowTitle(
            I18N.tr("audio.fx.spatial_panner", "Spatial Panner") + f" - {track_name or track_id}")
        self.setModal(False)
        apply_float_panel(self)
        self.resize(780, 620)
        self.setMinimumSize(700, 560)

        self._setup_ui(track_name)
        self._refresh_all_views()

    # ── UI 构建 ───────────────────────────────────────────────

    def _setup_ui(self, track_name: str) -> None:
        self.setStyleSheet(PRO_DIALOG_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 1. 顶部工具栏
        top_bar = QFrame()
        top_bar.setFixedHeight(44)
        top_bar.setStyleSheet("background-color: #13151A; border-bottom: 1px solid #232630;")
        tb = QHBoxLayout(top_bar)
        tb.setContentsMargins(14, 6, 14, 6)
        tb.setSpacing(7)

        self.power_switch = PowerToggleSwitch(True, PALETTE["cyan"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb.addWidget(self.power_switch)

        title_lbl = QLabel(f"SPATIAL PANNER  ·  {track_name}")
        title_lbl.setStyleSheet(
            "color: #EAEDF6; font-size: 11px; font-weight: 700; letter-spacing: 0.5px;")
        tb.addWidget(title_lbl)

        tb.addSpacing(6)

        add_btn = QPushButton()
        add_btn.setObjectName("ToolBtn")
        add_btn.setIcon(glyph_icon("plus"))
        add_btn.setIconSize(QSize(12, 12))
        add_btn.setFixedSize(22, 23)
        add_btn.setToolTip(I18N.tr("audio.fx.save_preset", "保存当前声像为预设"))
        add_btn.clicked.connect(self._save_new_preset)
        tb.addWidget(add_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(175)
        self.preset_combo.activated.connect(self._on_preset_selected)
        self._rebuild_preset_combo()
        tb.addWidget(self.preset_combo)

        # A/B 对比槽
        self._banks = {"A": copy.deepcopy(self._state), "B": copy.deepcopy(self._state)}
        self._active_bank = "A"

        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(23, 23)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton()
        self.btn_copy_ab.setObjectName("ToolBtn")
        self.btn_copy_ab.setIcon(glyph_icon("arrow-right"))
        self.btn_copy_ab.setIconSize(QSize(12, 12))
        self.btn_copy_ab.setFixedSize(22, 23)
        self.btn_copy_ab.setToolTip(I18N.tr("audio.fx.copy_bank", "复制当前参数到另一槽位"))
        self.btn_copy_ab.clicked.connect(self._copy_bank)

        self.btn_b = QPushButton("B")
        self.btn_b.setFixedSize(23, 23)
        self.btn_b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_b.clicked.connect(lambda: self._switch_bank("B"))

        tb.addWidget(self.btn_a)
        tb.addWidget(self.btn_copy_ab)
        tb.addWidget(self.btn_b)

        tb.addStretch()

        self.format_combo = ChevronComboBox()
        self.format_combo.setFixedWidth(64)
        self.format_combo.addItems(["2.0", "5.1", "7.1", "7.1.4"])
        self.format_combo.activated.connect(self._on_format_changed)
        tb.addWidget(self.format_combo)

        self.btn_3d = QPushButton("3D")
        self.btn_3d.setCheckable(True)
        self.btn_3d.setFixedSize(46, 23)
        self.btn_3d.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_3d.setToolTip(I18N.tr("audio.fx.toggle_2d3d_tip", "切换 2D 平面 / 3D 立体声学空间模式"))
        self.btn_3d.setStyleSheet(f"""
            QPushButton {{
                background-color: #22252E;
                color: #9096A6;
                border: 1px solid #303440;
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

        reset_btn = QPushButton(I18N.tr("audio.fx.reset", "Reset"))
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedHeight(23)
        reset_btn.setMinimumWidth(max(46, reset_btn.sizeHint().width() + 6))
        reset_btn.clicked.connect(self.reset_panner)
        tb.addWidget(reset_btn)

        root.addWidget(top_bar)

        # 2. 主体：左侧参数卡片列（可滚动）+ 右侧声场舞台
        body = QHBoxLayout()
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(12)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFixedWidth(196)
        left_scroll.setStyleSheet("""
            QScrollArea { background-color: transparent; border: none; }
            QScrollArea > QWidget > QWidget { background-color: transparent; }
        """)
        left_panel = QWidget()
        left_lay = QVBoxLayout(left_panel)
        left_lay.setContentsMargins(0, 0, 6, 0)
        left_lay.setSpacing(6)

        self._build_left_cards(left_lay)
        left_lay.addStretch()
        left_scroll.setWidget(left_panel)
        body.addWidget(left_scroll)

        self.stage = SpatialSoundStageWidget()
        self.stage.xyDragged.connect(self._on_stage_xy_dragged)
        self.stage.xyDragEnded.connect(self._on_drag_released)
        self.stage.elevationDragged.connect(self._on_stage_elev_dragged)
        self.stage.elevationDragEnded.connect(self._on_drag_released)
        self.stage.resetPositionRequested.connect(self._reset_stage_position)
        self.stage.toggle3DRequested.connect(self._toggle_3d_from_stage)
        body.addWidget(self.stage, 1)

        root.addLayout(body, 1)

    def _make_hint_row(self, parent_lay: QVBoxLayout, left_t: str, right_t: str) -> None:
        row = QHBoxLayout()
        row.setSpacing(6)
        l1 = QLabel(left_t)
        l1.setObjectName("HintText")
        l2 = QLabel(right_t)
        l2.setObjectName("HintText")
        row.addWidget(l1)
        row.addStretch()
        row.addWidget(l2)
        parent_lay.addLayout(row)

    def _add_knob(self, card: PannerControlCard, minimum: float, maximum: float,
                  default: float, mode: str = "linear", invert: bool = False) -> ProRotaryKnob:
        knob = ProRotaryKnob(minimum, maximum, default, default, card._accent,
                             mode=mode, invert=invert, size=38)
        knob.valueChanged.connect(lambda _v, c=card: self._on_knob_changed(c))
        knob.interactionStarted.connect(lambda: setattr(self, "_dragging", True))
        knob.interactionEnded.connect(self._on_drag_released)
        card.right_col.addWidget(knob, 0, Qt.AlignmentFlag.AlignCenter)
        return knob

    def _build_left_cards(self, left_lay: QVBoxLayout) -> None:
        # 卡片 1: Left / Right
        self.card_lr = PannerControlCard(I18N.tr("audio.fx.left_right", "Left / Right"), PALETTE["blue"])
        self.pill_lr = EditableValuePill("C", PALETTE["blue"], width=58)
        self.pill_lr.valueEdited.connect(self._on_lr_text)
        self.card_lr.extra_slot.addWidget(self.pill_lr)
        self.knob_lr = self._add_knob(self.card_lr, -100.0, 100.0, 0.0, mode="bipolar")
        self._make_hint_row(self.card_lr.right_col, "L", "R")
        left_lay.addWidget(self.card_lr)

        # 卡片 2: Front / Back
        self.card_fb = PannerControlCard(I18N.tr("audio.fx.front_back", "Front / Back"), PALETTE["cyan"])
        self.pill_fb = EditableValuePill("100F", PALETTE["cyan"], width=58)
        self.pill_fb.valueEdited.connect(self._on_fb_text)
        self.card_fb.extra_slot.addWidget(self.pill_fb)
        # invert: 向左/逆时针为 F(+100)
        self.knob_fb = self._add_knob(self.card_fb, -100.0, 100.0, 100.0,
                                      mode="bipolar", invert=True)
        self._make_hint_row(self.card_fb.right_col, "F", "B")
        left_lay.addWidget(self.card_fb)

        # 卡片 3: Height (Z)
        self.card_elev = PannerControlCard(I18N.tr("audio.fx.height_z", "Height (Z)"), PALETTE["violet"])
        self.pill_elev = EditableValuePill("0", PALETTE["violet"], width=58)
        self.pill_elev.valueEdited.connect(self._on_elev_text)
        self.card_elev.extra_slot.addWidget(self.pill_elev)
        self.knob_elev = self._add_knob(self.card_elev, 0.0, 100.0, 0.0)
        self._make_hint_row(self.card_elev.right_col, "0", "100T")
        left_lay.addWidget(self.card_elev)

        # 卡片 4: Rotate
        self.card_rot = PannerControlCard(I18N.tr("audio.fx.rotate", "Rotate"), PALETTE["amber"])
        self.pill_rot = EditableValuePill("0°", PALETTE["amber"], width=58)
        self.pill_rot.valueEdited.connect(self._on_rot_text)
        self.card_rot.extra_slot.addWidget(self.pill_rot)
        self.knob_rot = self._add_knob(self.card_rot, 0.0, 360.0, 0.0, mode="circular")
        self._make_hint_row(self.card_rot.right_col, "0", "360")
        left_lay.addWidget(self.card_rot)

        # 卡片 5: Spread
        self.card_spread = PannerControlCard(I18N.tr("audio.fx.spread", "Spread"), PALETTE["green"])
        self.btn_spread_auto = self.card_spread.build_pill_btn("Auto", True, PALETTE["green"])
        self.btn_spread_auto.clicked.connect(self._on_spread_auto_clicked)
        self.card_spread.extra_slot.addWidget(self.btn_spread_auto)
        self.pill_spread = EditableValuePill("FULL", PALETTE["green"], width=58)
        self.pill_spread.valueEdited.connect(self._on_spread_text)
        self.card_spread.extra_slot.addWidget(self.pill_spread)
        self.knob_spread = self._add_knob(self.card_spread, 0.0, 100.0, 100.0)
        self._make_hint_row(self.card_spread.right_col, "PNT", "FULL")
        left_lay.addWidget(self.card_spread)

        # 卡片 6: Divergence
        self.card_div = PannerControlCard(I18N.tr("audio.fx.divergence", "Divergence"), PALETTE["violet"])
        self.btn_div_mode = self.card_div.build_pill_btn("2D", False, PALETTE["violet"])
        self.btn_div_mode.clicked.connect(self._on_div_mode_clicked)
        self.card_div.extra_slot.addWidget(self.btn_div_mode)
        self.pill_div = EditableValuePill("0", PALETTE["violet"], width=58)
        self.pill_div.valueEdited.connect(self._on_div_text)
        self.card_div.extra_slot.addWidget(self.pill_div)
        self.knob_div = self._add_knob(self.card_div, 0.0, 100.0, 0.0)
        self._make_hint_row(self.card_div.right_col, "0", "100")
        left_lay.addWidget(self.card_div)

        # 卡片 7: Boom (LFE Send)
        self.card_boom = PannerControlCard("Boom", PALETTE["coral"])
        self.btn_boom_on = self.card_boom.build_pill_btn(I18N.tr("audio.fx.on", "On"), True, PALETTE["coral"])
        self.btn_boom_on.clicked.connect(self._on_boom_on_clicked)
        self.btn_boom_route = self.card_boom.build_pill_btn("Pre", False, PALETTE["amber"])
        self.btn_boom_route.setToolTip(I18N.tr(
            "audio.fx.boom_route_tip", "点击切换 LFE 发送路由：Pre (推子前) / Post (推子后) / Only (仅 LFE)"))
        self.btn_boom_route.clicked.connect(self._on_boom_route_clicked)
        self.card_boom.extra_slot.addWidget(self.btn_boom_on)
        self.card_boom.extra_slot.addWidget(self.btn_boom_route)
        self.knob_boom = self._add_knob(self.card_boom, -60.0, 10.0, -60.0)
        self._make_hint_row(self.card_boom.right_col, "-∞", "+10")
        self.pill_boom = EditableValuePill("-∞", PALETTE["coral"], width=58)
        self.pill_boom.valueEdited.connect(self._on_boom_text)
        self.card_boom.right_col.addWidget(self.pill_boom, 0, Qt.AlignmentFlag.AlignHCenter)
        left_lay.addWidget(self.card_boom)

    # ── 预设 ─────────────────────────────────────────────────

    def _default_presets(self) -> list:
        d = SpatialPannerParams.default().to_dict()
        return [
            {"name": I18N.tr("audio.fx.p_front_center", "Front Center (Default)"), "state": {**d, "lr": 0.0, "fb": 100.0}},
            {"name": I18N.tr("audio.fx.p_wide_stage", "Wide Stereo Stage"),
             "state": {**d, "lr": 0.0, "fb": 85.0, "spread": 100.0, "divergence": 15.0}},
            {"name": I18N.tr("audio.fx.p_mono_point", "Intimate Mono Point"),
             "state": {**d, "lr": 0.0, "fb": 90.0, "spread": 0.0, "divergence": 0.0}},
            {"name": I18N.tr("audio.fx.p_surround", "Surround Envelopment"),
             "state": {**d, "lr": 0.0, "fb": -15.0, "spread": 100.0, "divergence": 55.0}},
            {"name": I18N.tr("audio.fx.p_flyby", "3D Overhead Flyby"),
             "state": {**d, "space_mode": "3D", "format": "7.1.4",
                       "lr": -25.0, "fb": 30.0, "elevation": 85.0, "spread": 40.0, "divergence": 30.0}},
            {"name": I18N.tr("audio.fx.p_lfe", "Subwoofer LFE Send"),
             "state": {**d, "boom_on": True, "boom_route": "Pre", "boom_gain": 0.0}},
        ]

    def _rebuild_preset_combo(self) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for p in self._default_presets():
            self.preset_combo.addItem(p["name"], p["state"])
        self._custom_presets: list = getattr(self, "_custom_presets", [])
        for p in self._custom_presets:
            self.preset_combo.addItem(p["name"], p["state"])
        self.preset_combo.blockSignals(False)

    # ── 状态与联动 ───────────────────────────────────────────

    def _refresh_all_views(self) -> None:
        st = dict(self._state)
        self._updating = True
        en = bool(st.get("enabled", True))
        is_3d = (st.get("space_mode") == "3D")

        self._update_ab_styles()
        self.power_switch.setChecked(en)

        idx = self.format_combo.findText(st.get("format", "2.0"))
        if idx >= 0:
            self.format_combo.setCurrentIndex(idx)
        self.btn_3d.setChecked(is_3d)

        self.card_lr.setCardEnabled(en)
        self.knob_lr.setValue(st["lr"])
        self.knob_lr.setModuleActive(en)
        self.pill_lr.setText(fmt_lr(st["lr"]))
        self.pill_lr.setModuleActive(en)

        self.card_fb.setCardEnabled(en)
        self.knob_fb.setValue(st["fb"])
        self.knob_fb.setModuleActive(en)
        self.pill_fb.setText(fmt_fb(st["fb"]))
        self.pill_fb.setModuleActive(en)

        elev_en = en and is_3d
        self.card_elev.setCardEnabled(elev_en)
        self.knob_elev.setValue(st["elevation"])
        self.knob_elev.setModuleActive(elev_en)
        self.pill_elev.setText(fmt_elev(st["elevation"]))
        self.pill_elev.setModuleActive(elev_en)

        self.card_rot.setCardEnabled(en)
        self.knob_rot.setValue(st["rotate"])
        self.knob_rot.setModuleActive(en)
        self.pill_rot.setText(fmt_rotate(st["rotate"]))
        self.pill_rot.setModuleActive(en)

        self.card_spread.setCardEnabled(en)
        is_auto = st.get("spread_auto", False)
        self.btn_spread_auto.setChecked(is_auto)
        eff_spread = (self.stage._effective_spread() * 100.0) if is_auto else st["spread"]
        self.knob_spread.setValue(eff_spread)
        self.knob_spread.setModuleActive(en and not is_auto)
        self.pill_spread.setText(fmt_spread(eff_spread, is_auto))
        self.pill_spread.setModuleActive(en)

        self.card_div.setCardEnabled(en)
        self.btn_div_mode.setText(st.get("div_mode", "2D"))
        self.knob_div.setValue(st["divergence"])
        self.knob_div.setModuleActive(en)
        self.pill_div.setText(f"{int(round(st['divergence']))}")
        self.pill_div.setModuleActive(en)

        boom_active = en and st.get("boom_on", False)
        self.card_boom.setCardEnabled(boom_active)
        self.btn_boom_on.setChecked(st.get("boom_on", False))
        self.btn_boom_route.setText(st.get("boom_route", "Pre"))
        self.knob_boom.setValue(st["boom_gain"])
        self.knob_boom.setModuleActive(boom_active)
        self.pill_boom.setText(fmt_boom(st["boom_gain"]))
        self.pill_boom.setModuleActive(boom_active)

        self.stage.setState(copy.deepcopy(st))
        self._updating = False

    def _update_ab_styles(self) -> None:
        active_css = f"""
            QPushButton {{
                background-color: #13242C;
                color: {PALETTE["cyan"]};
                border: 1px solid {PALETTE["cyan"]};
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
            }}
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
        self.btn_copy_ab.setIcon(glyph_icon(
            "arrow-right" if self._active_bank == "A" else "arrow-left"))

    def _payload(self) -> dict:
        panner = SpatialPannerParams.from_dict(self._state).to_dict()
        return {"pan": max(-1.0, min(1.0, self._state["lr"] / 100.0)), "panner": panner}

    def _emit(self, commit: Optional[bool] = None) -> None:
        if self._updating:
            return
        use_commit = (not self._dragging) if commit is None else commit
        if use_commit:
            self.panner_committed.emit(self._track_id, self._payload())
        else:
            self.panner_preview.emit(self._track_id, self._payload())

    def _on_drag_released(self) -> None:
        if self._updating:
            return
        self._dragging = False
        self.panner_committed.emit(self._track_id, self._payload())

    # ── 事件处理 ─────────────────────────────────────────────

    def _switch_bank(self, bank: str) -> None:
        if bank != self._active_bank:
            self._active_bank = bank
            self._state = copy.deepcopy(self._banks[bank])
            self._refresh_all_views()
            self._emit(commit=True)

    def _copy_bank(self) -> None:
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._state)

    def _on_power_toggled(self, checked: bool) -> None:
        self._state["enabled"] = checked
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_format_changed(self, index: int) -> None:
        fmt = self.format_combo.itemText(index)
        self._state["format"] = fmt
        if fmt == "7.1.4" and self._state["space_mode"] != "3D":
            self._state["space_mode"] = "3D"
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_3d_toggled(self, checked: bool) -> None:
        self._state["space_mode"] = "3D" if checked else "2D"
        self._refresh_all_views()
        self._emit(commit=True)

    def _toggle_3d_from_stage(self) -> None:
        self._on_3d_toggled(self._state["space_mode"] != "3D")

    def _emit_pos(self) -> None:
        self._emit()

    def _on_knob_changed(self, card: PannerControlCard) -> None:
        if self._updating:
            return
        self._state["lr"] = round(self.knob_lr.value(), 1)
        self._state["fb"] = round(self.knob_fb.value(), 1)
        self._state["elevation"] = round(self.knob_elev.value(), 1)
        self._state["rotate"] = round(self.knob_rot.value(), 1) % 360.0
        self._state["spread"] = round(self.knob_spread.value(), 1)
        self._state["divergence"] = round(self.knob_div.value(), 1)
        self._state["boom_gain"] = round(self.knob_boom.value(), 1)
        self._refresh_all_views()
        self._emit()

    def _on_lr_text(self, text: str) -> None:
        self._state["lr"] = parse_lr(text, self._state["lr"])
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_fb_text(self, text: str) -> None:
        self._state["fb"] = parse_fb(text, self._state["fb"])
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_elev_text(self, text: str) -> None:
        self._state["elevation"] = parse_elev(text, self._state["elevation"])
        if self._state["space_mode"] != "3D":
            self._state["space_mode"] = "3D"
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_rot_text(self, text: str) -> None:
        self._state["rotate"] = parse_rotate(text, self._state["rotate"])
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_stage_xy_dragged(self, lr: float, fb: float) -> None:
        self._state["lr"] = lr
        self._state["fb"] = fb
        self._refresh_all_views()
        self._emit_pos()

    def _on_stage_elev_dragged(self, elev: float) -> None:
        self._state["elevation"] = elev
        self._refresh_all_views()
        self._emit_pos()

    def _reset_stage_position(self) -> None:
        self._state["lr"] = 0.0
        self._state["fb"] = 100.0
        self._state["elevation"] = 0.0
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_spread_auto_clicked(self, checked: bool) -> None:
        self._state["spread_auto"] = checked
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_spread_text(self, text: str) -> None:
        self._state["spread_auto"] = False
        self._state["spread"] = parse_spread(text, self._state["spread"])
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_div_mode_clicked(self) -> None:
        self._state["div_mode"] = "3D" if self._state["div_mode"] == "2D" else "2D"
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_div_text(self, text: str) -> None:
        try:
            v = float(text.strip().replace("%", ""))
            self._state["divergence"] = max(0.0, min(100.0, round(v, 1)))
            self._refresh_all_views()
            self._emit(commit=True)
        except ValueError:
            pass

    def _on_boom_on_clicked(self, checked: bool) -> None:
        self._state["boom_on"] = checked
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_boom_route_clicked(self) -> None:
        order = ["Pre", "Post", "Only"]
        nxt = order[(order.index(self._state["boom_route"]) + 1) % len(order)]
        self._state["boom_route"] = nxt
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_boom_text(self, text: str) -> None:
        self._state["boom_gain"] = parse_boom(text, self._state["boom_gain"])
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_preset_selected(self, combo_idx: int) -> None:
        state = self.preset_combo.itemData(combo_idx)
        if isinstance(state, dict):
            keep = {
                "enabled": self._state["enabled"],
                "boom_on": self._state["boom_on"],
            }
            self._state = {**copy.deepcopy(state), **keep}
            self._refresh_all_views()
            # 保留选中项显示（旧逻辑置 -1 会让预设名消失）
            self._emit(commit=True)

    def _save_new_preset(self) -> None:
        name, ok = QInputDialog.getText(
            self, I18N.tr("audio.fx.save_preset_title", "保存声像预设"),
            I18N.tr("audio.fx.preset_name_prompt", "输入预设名称:"))
        if ok and name.strip():
            clean = name.strip()
            state = copy.deepcopy(self._state)
            self._custom_presets.append({"name": clean, "state": state})
            self.preset_combo.addItem(clean, copy.deepcopy(state))
            # ST-01: 原子落盘到用户库
            try:
                from src.features.edit.core.effect_preset import save_user_preset
                save_user_preset(self._effect_type, clean, state)
            except Exception as exc:  # noqa: BLE001 — 落盘失败不阻断内存视图
                logger.warning("panner dialog: persist preset failed: %r", exc)

    # ── 公开 API ─────────────────────────────────────────────

    def reset_panner(self) -> None:
        """复位声像位置（保留制式/2D3D/Boom 路由等）。"""
        keep = {
            "enabled": self._state["enabled"],
            "format": self._state["format"],
            "space_mode": self._state["space_mode"],
            "boom_on": self._state["boom_on"],
            "boom_route": self._state["boom_route"],
        }
        self._state = {**SpatialPannerParams.default().to_dict(),
                       "lr": 0.0, "fb": 100.0, **keep}
        self._refresh_all_views()
        self._emit(commit=True)

    def get_state(self) -> dict:
        return self._payload()

    def set_state(self, pan: float, panner: Optional[dict]) -> None:
        """外部静默回填（timeline 变化后同步显示，不回发信号）。"""
        data = panner if isinstance(panner, dict) and panner \
            else SpatialPannerParams.default().to_dict()
        self._state = {**SpatialPannerParams.from_dict(data).to_dict(),
                       "lr": max(-100.0, min(100.0, float(pan) * 100.0))}
        self._banks[self._active_bank] = copy.deepcopy(self._state)
        self._refresh_all_views()


if __name__ == "__main__":  # pragma: no cover
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    win = SpatialPannerDialog("t1", "Audio 1", 0.0, None, None)
    win.show()
    sys.exit(app.exec())
