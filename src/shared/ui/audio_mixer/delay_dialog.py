"""Stereo Delay Dialog — 立体声双线延迟器对话框（CW-A1 重构）。

自主旗舰立体声延迟器：交互式延迟脉冲示波器（拖 Tap 调时间/反馈、
Ctrl/Alt 锁轴、滚轮调 High Ratio、ENV/GHOST 视图开关、Mono 单泳道模式）
+ 双声道输出电平表 + 5 张参数卡片（Filter / Left / Right / Feedback / Output）
+ Link L/R 联动 + 输入格式选择。

对接项目契约：
  - 构造：``(track_id, track_name, instance_id, initial_params: DelayParams, parent)``
  - 信号：``delay_preview`` / ``delay_committed`` (track_id, DelayParams)
  - 三段节流 + A/B 对比槽 + 预设

遥测：``update_output_meters(left_db, right_db)`` 双声道输出电平表。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.core.timeline_v2 import DelayParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import (
    KnobParameterCell,
    PowerToggleSwitch,
    apply_float_panel,
    glyph_icon,
    ProModuleCard,
    PRO_DIALOG_QSS,
    ProStereoOutputMeter,
    format_freq_hz,
    parse_delay_ms,
    parse_float_clean,
    parse_freq_hz,
)
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

COLORS = {
    "cyan":   "#28D8F0",  # 主控 / A-B 激活 / Left 声道 / Output
    "filter": "#4CD490",  # Filter 模块主题色（翡翠绿）
    "right":  "#6495FA",  # Right 声道主题色（钴蓝）
    "fb":     "#F0A848",  # Feedback 矩阵模块主题色（琥珀金）
    "clip":   "#F26D6D",  # 削波警示（珊瑚红）
}

DELAY_PRESETS: list[dict[str, Any]] = [
    {"name": "Default", "params": {
        "high_cut": 12500.0, "low_cut": 108.0,
        "left_delay": 121.0, "left_feedback": 16.0,
        "right_delay": 135.0, "right_feedback": -7.0,
        "high_ratio": 60.0, "stereo_blend": 15.0,
        "dry_wet": 30.0, "level_db": 0.0}},
    {"name": "Clean Quarter", "params": {
        "high_cut": 12500.0, "low_cut": 108.0,
        "left_delay": 250.0, "left_feedback": 25.0,
        "right_delay": 250.0, "right_feedback": 25.0,
        "high_ratio": 30.0, "stereo_blend": 0.0,
        "dry_wet": 25.0, "level_db": 0.0}},
    {"name": "Slapback Vocal", "params": {
        "high_cut": 6800.0, "low_cut": 180.0,
        "left_delay": 88.0, "left_feedback": 8.0,
        "right_delay": 112.0, "right_feedback": -10.0,
        "high_ratio": 45.0, "stereo_blend": 10.0,
        "dry_wet": 24.0, "level_db": 0.0}},
    {"name": "Ping-Pong Ambient", "params": {
        "high_cut": 8200.0, "low_cut": 150.0,
        "left_delay": 240.0, "left_feedback": 46.0,
        "right_delay": 360.0, "right_feedback": 42.0,
        "high_ratio": 72.0, "stereo_blend": 85.0,
        "dry_wet": 38.0, "level_db": -0.5}},
    {"name": "Wide Haas Doubler", "params": {
        "high_cut": 16000.0, "low_cut": 90.0,
        "left_delay": 14.0, "left_feedback": 0.0,
        "right_delay": 26.0, "right_feedback": 0.0,
        "high_ratio": 20.0, "stereo_blend": 0.0,
        "dry_wet": 45.0, "level_db": 0.0}},
    {"name": "Dub Phase Echo", "params": {
        "high_cut": 4800.0, "low_cut": 220.0,
        "left_delay": 310.0, "left_feedback": -58.0,
        "right_delay": 465.0, "right_feedback": 54.0,
        "high_ratio": 80.0, "stereo_blend": 65.0,
        "dry_wet": 42.0, "level_db": -1.0}},
]


# ═════════════════════════════════════════════════════════════════════════════
# 交互式延迟脉冲示波器
# ═════════════════════════════════════════════════════════════════════════════

class ProDelayTapGraphWidget(QWidget):
    """延迟脉冲示波器（包络曲线 + 相位反转 + 直接拖拽交互）。

    双泳道（L/R）显示脉冲序列（反馈符号控制上下相位）、指数衰减包络
    （ENV）与对侧交叉串扰幽灵脉冲（GHOST）；拖拽 Tap 1 节点同时调节
    延迟时间（X 轴）与反馈量（Y 轴）。
    """

    tapDragged = pyqtSignal(str, float, float)   # channel ("L"|"R"), delay_ms, feedback_pct
    tapDragEnded = pyqtSignal()                  # 拖拽结束（commit 时机）
    highRatioScrolled = pyqtSignal(float)        # delta

    MAX_TIME_MS = 2000.0
    TIME_TICKS = [0, 250, 500, 750, 1000, 1250, 1500, 1750, 2000]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(230)
        self.setMouseTracking(True)

        self._state: dict = DelayParams.default().to_dict()
        self._show_envelope = True
        self._show_ghost = True

        self._hover_target = ""
        self._dragging_target = ""
        self._drag_start_delay = 0.0
        self._drag_start_fb = 0.0
        self._mouse_pos: Optional[QPointF] = None

        self._btn_env_rect = QRectF()
        self._btn_ghost_rect = QRectF()

    def setState(self, state: dict) -> None:
        self._state = state
        self.update()

    def _lanes_geometry(self) -> tuple:
        ml, mr, mt, mb = 32.0, 14.0, 10.0, 10.0
        w = max(120.0, self.width() - ml - mr)
        h = max(120.0, self.height() - mt - mb)
        axis_h = 20.0
        lane_h = (h - axis_h) / 2.0

        l_rect = QRectF(ml, mt, w, lane_h)
        axis_rect = QRectF(ml, mt + lane_h, w, axis_h)
        r_rect = QRectF(ml, mt + lane_h + axis_h, w, lane_h)
        return l_rect, axis_rect, r_rect

    def _hit_test_tap(self, pos: QPointF) -> str:
        if not self._state.get("enabled", True):
            return ""
        l_rect, _, r_rect = self._lanes_geometry()
        is_mono = (self._state.get("input_format") == "Mono")

        if is_mono:
            full_rect = QRectF(l_rect.left(), l_rect.top(), l_rect.width(),
                               r_rect.bottom() - l_rect.top())
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

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = None
        self._hover_target = ""
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
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

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = event.position()
        l_rect, _, r_rect = self._lanes_geometry()

        if self._dragging_target:
            mods = event.modifiers()
            target_rect = l_rect if self._dragging_target == "L" else r_rect
            if self._state.get("input_format") == "Mono":
                target_rect = QRectF(l_rect.left(), l_rect.top(), l_rect.width(),
                                     r_rect.bottom() - l_rect.top())

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

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_target:
            self._dragging_target = ""
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._hover_target
                           else Qt.CursorShape.CrossCursor)
            self.tapDragEnded.emit()
            self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._hover_target or self._dragging_target:
            delta = (event.angleDelta().y() / 120.0) * 4.0
            self.highRatioScrolled.emit(delta)
            event.accept()

    # ── 绘制 ─────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        l_rect, axis_rect, r_rect = self._lanes_geometry()
        is_mono = (self._state.get("input_format") == "Mono")
        enabled = self._state.get("enabled", True)

        if is_mono:
            mono_rect = QRectF(l_rect.left(), l_rect.top(), l_rect.width(),
                               r_rect.bottom() - l_rect.top() - 18.0)
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

        # 右上角视图开关胶囊 (ENV / GHOST)
        self._btn_env_rect = QRectF(top_overlay_rect.right() - 88, top_overlay_rect.top() + 7, 38, 18)
        self._btn_ghost_rect = QRectF(top_overlay_rect.right() - 46, top_overlay_rect.top() + 7, 40, 18)
        for rect, label, active in (
            (self._btn_env_rect, "ENV", self._show_envelope),
            (self._btn_ghost_rect, "GHOST", self._show_ghost and not is_mono),
        ):
            p.setPen(QPen(QColor("#3A4252") if active else QColor("#282B34"), 1.0))
            p.setBrush(QColor(24, 34, 46, 210) if active else QColor(18, 20, 25, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#6E727E"))
            p.setFont(QFont("Segoe UI", 7))
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
                    hud_col = QColor("#B4BAC8")
                    hud_w = 132

                hud_rect = QRectF(top_overlay_rect.left() + 8, top_overlay_rect.top() + 7, hud_w, 18)
                p.setPen(QPen(QColor("#323642"), 1.0))
                p.setBrush(QColor(16, 18, 23, 215))
                p.drawRoundedRect(hud_rect, 3.5, 3.5)
                p.setPen(hud_col)
                p.setFont(QFont("Segoe UI", 8))
                p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

    def _draw_lane_canvas(self, p: QPainter, rect: QRectF, ch_label: str, accent_hex: str) -> None:
        bg_grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#181A20"))
        bg_grad.setColorAt(1.0, QColor("#13151A"))
        p.fillRect(rect, bg_grad)

        grid_pen = QPen(QColor("#242730"), 1.0)
        sec_pen = QPen(QColor("#2F3442"), 1.0)
        for t in self.TIME_TICKS[1:-1]:
            x = rect.left() + (t / self.MAX_TIME_MS) * rect.width()
            p.setPen(sec_pen if t == 1000 else grid_pen)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))

        mid_y = rect.center().y()
        p.setPen(QPen(QColor("#383D4B"), 1.1))
        p.drawLine(QPointF(rect.left(), mid_y), QPointF(rect.right(), mid_y))

        p.setPen(QPen(QColor("#2E323D"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)

        badge_rect = QRectF(4, mid_y - 10, 22, 20)
        col = QColor(accent_hex if self._state.get("enabled", True) else "#686C78")
        p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 90), 1.0))
        p.setBrush(QColor("#14161C"))
        p.drawRoundedRect(badge_rect, 3.5, 3.5)
        font = QFont("Segoe UI")
        font.setPointSizeF(8.5)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(col)
        p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, ch_label)

    def _draw_time_axis(self, p: QPainter, rect: QRectF) -> None:
        p.setPen(QColor("#7E828E"))
        p.setFont(QFont("Segoe UI", 8))
        labels = ["0ms", "250", "500", "750", "1s", "1.25s", "1.50s", "1.75s", "2s"]
        for t, txt in zip(self.TIME_TICKS, labels):
            x = rect.left() + (t / self.MAX_TIME_MS) * rect.width()
            if t == 0:
                p.drawText(QRectF(x, rect.top(), 36, rect.height()),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)
            elif t == 2000:
                p.drawText(QRectF(x - 36, rect.top(), 36, rect.height()),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, txt)
            else:
                p.drawText(QRectF(x - 20, rect.top(), 40, rect.height()),
                           Qt.AlignmentFlag.AlignCenter, txt)

    def _draw_tap_cluster(
        self, p: QPainter, rect: QRectF,
        delay_ms: float, fb_pct: float,
        cross_delay_ms: Optional[float], cross_fb_pct: float,
        accent_hex: str, is_focus: bool,
    ) -> None:
        from PyQt6.QtGui import QPainterPath

        p.save()
        p.setClipRect(rect.adjusted(1, 1, -1, -1))

        mid_y = rect.center().y()
        max_h = rect.height() * 0.42
        enabled = self._state.get("enabled", True)
        base_col = QColor(accent_hex if enabled else "#585C68")

        fb = fb_pct / 100.0
        blend = self._state.get("stereo_blend", 15.0) / 100.0
        damping = 1.0 - (self._state.get("high_ratio", 60.0) / 100.0) * 0.16

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

        # 2. 指数衰减包络曲线
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

        # 3. 主脉冲序列（正负 Feedback 相位上下翻转）
        tap1_pt: Optional[QPointF] = None
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

            if enabled and k <= 6:
                glow = QColor(base_col)
                glow.setAlpha(int(48 * (damping ** (k - 1))))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(glow)
                p.drawRoundedRect(bar_rect.adjusted(-2.2, -1.5, 2.2, 1.5), 2.5, 2.5)

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

        # 4. Tap 1 交互控制手柄
        if tap1_pt is not None and enabled:
            r = 5.2 if is_focus else 4.0
            if is_focus:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(base_col.red(), base_col.green(), base_col.blue(), 75))
                p.drawEllipse(tap1_pt, r + 4.2, r + 4.2)
            p.setPen(QPen(base_col, 1.7))
            p.setBrush(QColor("#14161C"))
            p.drawEllipse(tap1_pt, r, r)

        p.restore()


# ═════════════════════════════════════════════════════════════════════════════
# 主对话框
# ═════════════════════════════════════════════════════════════════════════════

class DelayDialog(QDialog):
    """立体声双线延迟器对话框（脉冲示波器 + 5 参数卡片 + Link/格式）。"""

    delay_preview = pyqtSignal(str, object)    # 拖动期实时听感
    delay_committed = pyqtSignal(str, object)  # 松手/预设/开关，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[DelayParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id
        self._current: DelayParams = initial_params or DelayParams.default()
        self._dragging = False
        self._updating = False

        self.setWindowTitle(
            I18N.tr("audio.fx.stereo_delay", "Stereo Delay") + f" - {track_name or track_id}")
        self.setModal(False)
        apply_float_panel(self)
        self.resize(880, 660)
        self.setMinimumSize(760, 620)

        # A/B 对比槽（对话框内存行为）
        self._active_bank = "A"
        clean = DelayParams.from_dict(DELAY_PRESETS[1]["params"])
        self._banks = {"A": self._current, "B": clean}
        self._presets: list[dict[str, Any]] = copy.deepcopy(DELAY_PRESETS)
        self._cells: dict[str, KnobParameterCell] = {}

        self._setup_ui(track_name)
        self._refresh_all_views()

    # ── UI 构建 ───────────────────────────────────────────────

    def _setup_ui(self, track_name: str) -> None:
        self.setStyleSheet(PRO_DIALOG_QSS)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # 1. 顶部工具栏
        top_bar = QFrame()
        top_bar.setFixedHeight(44)
        top_bar.setStyleSheet("background-color: #13151A; border-bottom: 1px solid #232630;")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(14, 6, 14, 6)
        tb_lay.setSpacing(8)

        self.power_switch = PowerToggleSwitch(True, COLORS["cyan"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"STEREO DELAY  ·  {track_name}")
        title_lbl.setStyleSheet(
            "color: #EAEDF6; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(10)

        add_preset_btn = QPushButton()
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setIcon(glyph_icon("plus"))
        add_preset_btn.setIconSize(QSize(12, 12))
        add_preset_btn.setFixedSize(23, 23)
        add_preset_btn.setToolTip(I18N.tr("audio.fx.save_preset", "保存为新预设"))
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(165)
        for p in self._presets:
            self.preset_combo.addItem(p["name"])
        self.preset_combo.activated.connect(self._on_preset_selected)
        tb_lay.addWidget(self.preset_combo)

        prev_btn = QPushButton()
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setIcon(glyph_icon("chevron-left"))
        prev_btn.setIconSize(QSize(12, 12))
        prev_btn.setFixedSize(21, 23)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb_lay.addWidget(prev_btn)

        next_btn = QPushButton()
        next_btn.setObjectName("ToolBtn")
        next_btn.setIcon(glyph_icon("chevron-right"))
        next_btn.setIconSize(QSize(12, 12))
        next_btn.setFixedSize(21, 23)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb_lay.addWidget(next_btn)

        tb_lay.addSpacing(6)

        ab_lay = QHBoxLayout()
        ab_lay.setSpacing(2)
        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(24, 23)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton()
        self.btn_copy_ab.setObjectName("ToolBtn")
        self.btn_copy_ab.setIcon(glyph_icon("arrow-right"))
        self.btn_copy_ab.setIconSize(QSize(12, 12))
        self.btn_copy_ab.setFixedSize(24, 23)
        self.btn_copy_ab.setToolTip(I18N.tr("audio.fx.copy_bank", "复制当前参数到另一槽位"))
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

        # L/R 联动开关
        self.btn_link = QPushButton(I18N.tr("audio.fx.link_lr", "Link L/R"))
        self.btn_link.setCheckable(True)
        self.btn_link.setFixedSize(72, 23)
        self.btn_link.setToolTip(I18N.tr(
            "audio.fx.link_lr_tip", "锁定左右声道延迟时间与反馈比例"))
        self.btn_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_link.clicked.connect(self._on_link_toggled)
        tb_lay.addWidget(self.btn_link)

        fmt_lbl = QLabel(I18N.tr("audio.fx.input", "Input"))
        fmt_lbl.setStyleSheet("color: #8E94A4; font-size: 11px; font-weight: 600;")
        tb_lay.addWidget(fmt_lbl)

        # DL-01：输入选择矩阵与 DSP 同源（Stereo/Mono/Left/Right）；
        # 旧 LCR/Quad/5.x 菜单项从未有真实多声道实现，已迁移为 Stereo
        self.format_combo = ChevronComboBox()
        self.format_combo.setFixedWidth(78)
        self.format_combo.addItems(["Stereo", "Mono", "Left", "Right"])
        self.format_combo.activated.connect(self._on_format_changed)
        tb_lay.addWidget(self.format_combo)

        reset_btn = QPushButton(I18N.tr("audio.fx.reset", "Reset"))
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(46, 23)
        reset_btn.clicked.connect(self.reset_delay)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # 2. 中部：脉冲示波器 + 双声道输出电平表
        upper_panel = QFrame()
        upper_panel.setStyleSheet("background-color: #17191E;")
        up_lay = QHBoxLayout(upper_panel)
        up_lay.setContentsMargins(12, 12, 16, 8)
        up_lay.setSpacing(10)

        self.tap_graph = ProDelayTapGraphWidget()
        self.tap_graph.tapDragged.connect(self._on_graph_tap_dragged)
        self.tap_graph.tapDragEnded.connect(self._on_drag_released)
        self.tap_graph.highRatioScrolled.connect(self._on_graph_hr_scrolled)
        up_lay.addWidget(self.tap_graph, 1)

        out_col = QVBoxLayout()
        out_col.setSpacing(3)
        out_lbl = QLabel(I18N.tr("audio.fx.output", "Output"))
        out_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        out_lbl.setStyleSheet(
            "color: #A4AAB8; font-size: 10.5px; font-weight: 700; letter-spacing: 0.3px;")
        out_col.addWidget(out_lbl)

        self.output_meter = ProStereoOutputMeter()
        out_col.addWidget(self.output_meter, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)

        main_lay.addWidget(upper_panel, 5)

        # 3. 底部 5 张卡片
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(14, 4, 14, 14)
        cards_row.setSpacing(8)

        # 3.1 Filter Card
        c_hcut = self._add_ctrl(
            "high_cut", I18N.tr("audio.fx.high_cut", "High Cut"),
            100.0, 20000.0, 12500.0, COLORS["filter"], "100", "Hz", "20k",
            format_freq_hz, parse_freq_hz, mode="log")
        c_lcut = self._add_ctrl(
            "low_cut", I18N.tr("audio.fx.low_cut", "Low Cut"),
            20.0, 5000.0, 108.0, COLORS["filter"], "20", "Hz", "5k",
            format_freq_hz, parse_freq_hz, mode="log")
        self.card_filter = ProModuleCard("Filter", "2-POLE", COLORS["filter"])
        lay = self.card_filter.add_section()
        lay.addWidget(c_hcut, 1)
        lay = self.card_filter.add_section()
        lay.addWidget(c_lcut, 1)

        # 3.2 Left Card
        c_ldel = self._add_ctrl(
            "left_delay", I18N.tr("audio.fx.delay_time", "Delay Time"),
            0.0, 1200.0, 121.0, COLORS["cyan"], "0", "ms", "1.2s",
            lambda v: f"{v:.2f}", parse_delay_ms, mode="power")
        c_lfb = self._add_ctrl(
            "left_feedback", I18N.tr("audio.fx.feedback", "Feedback"),
            -99.0, 99.0, 16.0, COLORS["cyan"], "-99", "%", "99",
            lambda v: f"{v:.2f}", parse_float_clean, mode="bipolar", bipolar_origin=0.0)
        self.card_left = ProModuleCard("Left", "CH L", COLORS["cyan"])
        lay = self.card_left.add_section()
        lay.addWidget(c_ldel, 1)
        lay = self.card_left.add_section()
        lay.addWidget(c_lfb, 1)

        # 3.3 Right Card
        c_rdel = self._add_ctrl(
            "right_delay", I18N.tr("audio.fx.delay_time", "Delay Time"),
            0.0, 1200.0, 135.0, COLORS["right"], "0", "ms", "1.2s",
            lambda v: f"{v:.2f}", parse_delay_ms, mode="power")
        c_rfb = self._add_ctrl(
            "right_feedback", I18N.tr("audio.fx.feedback", "Feedback"),
            -99.0, 99.0, -7.0, COLORS["right"], "-99", "%", "99",
            lambda v: f"{v:.2f}", parse_float_clean, mode="bipolar", bipolar_origin=0.0)
        self.card_right = ProModuleCard("Right", "CH R", COLORS["right"])
        lay = self.card_right.add_section()
        lay.addWidget(c_rdel, 1)
        lay = self.card_right.add_section()
        lay.addWidget(c_rfb, 1)

        # 3.4 Feedback Matrix Card
        c_hr = self._add_ctrl(
            "high_ratio", I18N.tr("audio.fx.high_ratio", "High Ratio"),
            0.0, 100.0, 60.0, COLORS["fb"], "0", "%", "100",
            lambda v: f"{int(round(v))}", parse_float_clean)
        c_sb = self._add_ctrl(
            "stereo_blend", I18N.tr("audio.fx.stereo_blend", "Stereo Blend"),
            0.0, 100.0, 15.0, COLORS["fb"], "None", "%", "Full",
            lambda v: f"{v:.1f}", parse_float_clean)
        self.card_fb = ProModuleCard("Feedback", "MATRIX", COLORS["fb"])
        lay = self.card_fb.add_section()
        lay.addWidget(c_hr, 1)
        lay = self.card_fb.add_section()
        lay.addWidget(c_sb, 1)

        # 3.5 Output Card
        c_dw = self._add_ctrl(
            "dry_wet", I18N.tr("audio.fx.dry_wet", "Dry/Wet"),
            0.0, 100.0, 30.0, COLORS["cyan"], "Dry", "%", "Wet",
            lambda v: f"{v:.1f}", parse_float_clean)
        c_lvl = self._add_ctrl(
            "level_db", I18N.tr("audio.fx.level", "Level"),
            -24.0, 6.0, 0.0, COLORS["cyan"], "-24", "dB", "+6",
            lambda v: f"{v:.1f}", parse_float_clean, zero_marker_val=0.0)
        self.card_out = ProModuleCard("Output", "MASTER", COLORS["cyan"])
        lay = self.card_out.add_section()
        lay.addWidget(c_dw, 1)
        lay = self.card_out.add_section()
        lay.addWidget(c_lvl, 1)

        for card in (self.card_filter, self.card_left, self.card_right, self.card_fb, self.card_out):
            cards_row.addWidget(card, 1)

        main_lay.addLayout(cards_row, 4)

    def _add_ctrl(self, key: str, title: str, min_v: float, max_v: float,
                  default_v: float, accent: str, min_hint: str, mid_hint: str,
                  max_hint: str, formatter, parser, **kwargs) -> KnobParameterCell:
        cell = KnobParameterCell(
            title, min_v, max_v, default_v, accent,
            min_hint, mid_hint, max_hint, formatter, parser, **kwargs)
        cell.valueChanged.connect(lambda val, k=key: self._on_param_changed(k, val))
        cell.interactionStarted.connect(lambda: setattr(self, "_dragging", True))
        cell.interactionEnded.connect(self._on_drag_released)
        self._cells[key] = cell
        return cell

    # ── 状态与联动 ───────────────────────────────────────────

    def _st(self) -> DelayParams:
        return self._banks[self._active_bank]

    def _update_top_button_styles(self) -> None:
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
        self.btn_copy_ab.setIcon(glyph_icon(
            "arrow-right" if self._active_bank == "A" else "arrow-left"))

        st = self._st()
        self.btn_link.setChecked(st.link_lr)
        self.btn_link.setStyleSheet(active_css if st.link_lr else inactive_css)

    def _refresh_all_views(self) -> None:
        st = self._st()
        self._updating = True
        self._update_top_button_styles()

        self.power_switch.setChecked(st.enabled)
        idx = self.format_combo.findText(st.input_format)
        if idx >= 0:
            self.format_combo.setCurrentIndex(idx)

        for pid, cell in self._cells.items():
            if pid in st.to_dict():
                cell.setValue(getattr(st, pid))
            cell.setModuleActive(st.enabled)

        en = bool(st.enabled)
        is_mono = (st.input_format == "Mono")

        self.card_filter.setModuleActive(en)
        self.card_left.setModuleActive(en)
        self.card_left.setBadgeText("MONO" if is_mono else "CH L")
        self.card_right.setModuleActive(en and not is_mono)
        self.card_fb.setModuleActive(en)
        self._cells["stereo_blend"].setModuleActive(en and not is_mono)
        self.card_out.setModuleActive(en)

        self.output_meter.setActive(en)
        self.tap_graph.setState(st.to_dict())
        self._updating = False

    def _emit(self, commit: Optional[bool] = None) -> None:
        if self._updating:
            return
        use_commit = (not self._dragging) if commit is None else commit
        if use_commit:
            self.delay_committed.emit(self._track_id, self._st())
        else:
            self.delay_preview.emit(self._track_id, self._st())

    def _on_drag_released(self) -> None:
        if self._updating:
            return
        self._dragging = False
        self.delay_committed.emit(self._track_id, self._st())

    # ── 事件处理 ─────────────────────────────────────────────

    def _switch_bank(self, bank: str) -> None:
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self._emit(commit=True)

    def _copy_bank(self) -> None:
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._st())

    def _on_power_toggled(self, enabled: bool) -> None:
        st = self._st()
        self._banks[self._active_bank] = DelayParams(**{**st.to_dict(), "enabled": enabled})
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_link_toggled(self, linked: bool) -> None:
        st = self._st()
        data = st.to_dict()
        data["link_lr"] = linked
        if linked:
            data["right_delay"] = st.left_delay
            data["right_feedback"] = st.left_feedback
        self._banks[self._active_bank] = DelayParams.from_dict(data)
        # DL-01：一个离散手势最多一次工程提交（旧实现 linked 分支先 emit
        # 一次 committed，随后的 _emit(commit=True) 再发一次 = 双提交）。
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_format_changed(self, index: int) -> None:
        fmt = self.format_combo.itemText(index)
        st = self._st()
        self._banks[self._active_bank] = DelayParams(**{**st.to_dict(), "input_format": fmt})
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_param_changed(self, key: str, val: float) -> None:
        st = self._st()
        data = st.to_dict()
        data[key] = val
        if data.get("link_lr") and key in (
                "left_delay", "right_delay", "left_feedback", "right_feedback"):
            if key.startswith("left_"):
                data["right" + key[4:]] = val
            else:
                data["left" + key[5:]] = val
        try:
            self._banks[self._active_bank] = DelayParams.from_dict(data)
        except ValueError:
            return
        self.tap_graph.setState(self._st().to_dict())
        self._refresh_link_views()
        self._emit()

    def _refresh_link_views(self) -> None:
        """Link 开启时同步另一声道控件显示。"""
        st = self._st()
        if st.link_lr:
            self._cells["right_delay"].setValue(st.left_delay)
            self._cells["right_feedback"].setValue(st.left_feedback)

    def _on_graph_tap_dragged(self, channel: str, delay_ms: float, fb_pct: float) -> None:
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

    def _on_graph_hr_scrolled(self, delta: float) -> None:
        st = self._st()
        new_hr = max(0.0, min(100.0, round(st.high_ratio + delta, 0)))
        self._cells["high_ratio"].setValue(new_hr)
        self._on_param_changed("high_ratio", new_hr)
        self._on_drag_released()

    # ── 预设 ─────────────────────────────────────────────────

    def _on_preset_selected(self, combo_idx: int) -> None:
        if 0 <= combo_idx < len(self._presets):
            data = dict(self._presets[combo_idx]["params"])
            data["enabled"] = self._st().enabled  # 保留当前开关状态
            data["link_lr"] = self._st().link_lr
            data["input_format"] = self._st().input_format
            self._banks[self._active_bank] = DelayParams.from_dict(data)
            self._refresh_all_views()
            # 保留选中项显示（旧逻辑置 -1 会让预设名消失）
            self._emit(commit=True)

    def _step_preset(self, step: int) -> None:
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex(
                (max(self.preset_combo.currentIndex(), 0) + step) % cnt)
            self._on_preset_selected(self.preset_combo.currentIndex())

    def _save_new_preset(self) -> None:
        name, ok = QInputDialog.getText(
            self, I18N.tr("audio.fx.save_preset_title", "保存 Delay 预设"),
            I18N.tr("audio.fx.preset_name_prompt", "输入预设名称:"))
        if ok and name.strip():
            clean = name.strip()
            data = self._st().to_dict()
            data.pop("enabled", None)
            self._presets.append({"name": clean, "params": data})
            self.preset_combo.addItem(clean)

    # ── 公开 API ─────────────────────────────────────────────

    def reset_delay(self) -> None:
        """重置为默认延迟（保留开关/Link/格式状态）。"""
        st = self._st()
        self._banks[self._active_bank] = DelayParams(
            enabled=st.enabled, link_lr=st.link_lr, input_format=st.input_format)
        self._refresh_all_views()
        self._emit(commit=True)

    def get_params(self) -> DelayParams:
        return copy.deepcopy(self._st())

    def set_current_params(self, params: DelayParams) -> None:
        """外部静默回填（timeline 变化后同步显示，不回发信号）。"""
        self._banks[self._active_bank] = copy.deepcopy(params)
        self._refresh_all_views()

    def update_output_meters(self, left_db: float, right_db: float) -> None:
        """接入真实音频引擎双声道输出峰值电平 (-55.0 ~ 0.0 dBFS)。"""
        self.output_meter.setLevels(left_db, right_db)


if __name__ == "__main__":  # pragma: no cover
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    win = DelayDialog("t1", "Audio 2", "inst-1", None, None)
    win.show()
    sys.exit(app.exec())
