"""Dynamics Dialog — 动态处理器面板（Expander/Gate + Compressor + Limiter，CW-A1 重构）。

自主旗舰动态处理器：I/O 传输特性曲线图（三阈值可拖拽 + 实时信号轨迹点）+
Input / GR×3 / Make Up / Output / Sidechain 六表计 + 三张模块卡片：

  - ExpanderGateCard：Expander/Gate 双模式（mode/range/ratio/attack/hold/release）
  - CompressorCard：压缩（threshold/ratio/knee%/mix/attack/hold/release）+
    Sidechain（On/Listen/源选择，源可为任意轨）
  - LimiterCard：限制（threshold/attack/hold/release）

面板在轨道 effect_chain 中管理三个单例实例（panel="dynamics"，与 FX 列表
多实例互不干扰）。映射约定：
  - 面板总电源 → 三个实例的 enabled 同步（开启时补建缺失实例）
  - Make Up 全局推子 → compressor 实例的 makeup_gain（引擎链序
    expander → compressor(含 makeup) → limiter，与传输曲线一致）

对接项目契约：
  - 构造：``(track_id, track_name, chain: Optional[EffectChainParams], parent)``
  - 信号：``dynamics_preview`` / ``dynamics_committed`` (track_id, EffectChainParams)
  - ``update_chain(chain)`` 外部静默回填

遥测：``update_meters(input_db, gr_eg, gr_comp, gr_lim, output_db, sidechain_db)``。
侧链源列表：``set_sidechain_sources([(track_id, name), ...])``。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
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

from src.features.edit.core.timeline_v2 import (
    CompressorParams,
    DynamicsParams,
    EffectChainParams,
    ExpanderParams,
    LimiterParams,
    TrackEffectInstance,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import (
    KnobParameterCell,
    MakeUpFaderStrip,
    apply_float_panel,
    glyph_icon,
    PowerToggleSwitch,
    ProModuleCard,
    ProVerticalMeter,
    PRO_DIALOG_QSS,
    format_ms,
    parse_float_clean,
    parse_ratio_value,
)
from src.shared.ui.audio_mixer.pro_widgets import (
    format_ratio_comp as _fmt_ratio_comp,
    format_ratio_eg as _fmt_ratio_eg,
)
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

COLORS = {
    "cyan": "#28D8F0",  # 主控 / A-B 激活 / Make Up 指示
    "eg":   "#4CD490",  # Expander / Gate 模块主题色（翡翠绿）
    "comp": "#F0A848",  # Compressor 模块主题色（琥珀金）
    "lim":  "#F26D6D",  # Limiter 模块主题色（珊瑚红）
    "sc":   "#6495FA",  # Sidechain 侧链主题色（钴蓝）
}

_DYN_EXPANDER = "expander"
_DYN_COMPRESSOR = "compressor"
_DYN_LIMITER = "limiter"

# 面板预设（完整三模块状态；makeup 归入 comp）
DYNAMICS_PRESETS: list[dict[str, Any]] = [
    {"name": "Dialogue Control", "params": {
        "eg": {"mode": "Expander", "threshold_db": -35.0, "range_db": 18.0,
               "ratio": 1.8, "attack_ms": 1.4, "hold_ms": 0.0, "release_ms": 93.0, "enabled": True},
        "comp": {"threshold_db": -15.0, "ratio": 2.5, "attack_ms": 1.4, "hold_ms": 0.0,
                 "release_ms": 93.0, "knee": 15.0, "mix": 100.0, "makeup_gain": 0.0,
                 "sc_on": False, "sc_listen": False, "sc_source": "internal", "enabled": True},
        "lim": {"threshold_db": -6.0, "attack_ms": 0.71, "hold_ms": 0.0,
                "release_ms": 93.0, "enabled": True},
    }},
    {"name": "Flat Bypass", "params": {
        "eg": {"mode": "Expander", "threshold_db": -35.0, "range_db": 18.0,
               "ratio": 1.8, "attack_ms": 1.4, "hold_ms": 0.0, "release_ms": 93.0, "enabled": False},
        "comp": {"threshold_db": -15.0, "ratio": 2.5, "attack_ms": 1.4, "hold_ms": 0.0,
                 "release_ms": 93.0, "knee": 15.0, "mix": 100.0, "makeup_gain": 0.0,
                 "sc_on": False, "sc_listen": False, "sc_source": "internal", "enabled": False},
        "lim": {"threshold_db": -6.0, "attack_ms": 0.71, "hold_ms": 0.0,
                "release_ms": 93.0, "enabled": False},
    }},
    {"name": "Broadcast Voice Leveler", "params": {
        "eg": {"mode": "Gate", "threshold_db": -42.0, "range_db": 24.0,
               "ratio": 2.5, "attack_ms": 1.0, "hold_ms": 45.0, "release_ms": 140.0, "enabled": True},
        "comp": {"threshold_db": -18.0, "ratio": 3.2, "attack_ms": 2.5, "hold_ms": 0.0,
                 "release_ms": 110.0, "knee": 25.0, "mix": 100.0, "makeup_gain": 3.5,
                 "sc_on": False, "sc_listen": False, "sc_source": "internal", "enabled": True},
        "lim": {"threshold_db": -1.5, "attack_ms": 0.70, "hold_ms": 0.0,
                "release_ms": 60.0, "enabled": True},
    }},
    {"name": "Music Ducking (Sidechain)", "params": {
        "eg": {"mode": "Expander", "threshold_db": -35.0, "range_db": 18.0,
               "ratio": 1.5, "attack_ms": 1.4, "hold_ms": 0.0, "release_ms": 93.0, "enabled": False},
        "comp": {"threshold_db": -24.0, "ratio": 4.5, "attack_ms": 5.0, "hold_ms": 20.0,
                 "release_ms": 220.0, "knee": 35.0, "mix": 100.0, "makeup_gain": 0.0,
                 "sc_on": True, "sc_listen": False, "sc_source": "internal", "enabled": True},
        "lim": {"threshold_db": -3.0, "attack_ms": 0.80, "hold_ms": 0.0,
                "release_ms": 80.0, "enabled": True},
    }},
]


# ═════════════════════════════════════════════════════════════════════════════
# 交互式动态传输特性曲线图
# ═════════════════════════════════════════════════════════════════════════════

class ProDynamicsGraphWidget(QWidget):
    """I/O 传输特性曲线图（三阈值拖拽 + 实时样本轨迹）。"""

    egThresholdDragged = pyqtSignal(float)
    compThresholdDragged = pyqtSignal(float)
    limThresholdDragged = pyqtSignal(float)
    thresholdDragEnded = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(250, 220)
        self.setMouseTracking(True)

        # 面板状态快照 {eg: dict, comp: dict, lim: dict, makeup: float, enabled: bool}
        self._state: dict = copy.deepcopy(DYNAMICS_PRESETS[0]["params"])
        self._state["makeup"] = 0.0
        self._state["enabled"] = True
        self._dragging_target = ""
        self._hover_target = ""
        self._mouse_pos: Optional[QPointF] = None

        # 实时样本历史轨迹 [(in_db, out_db), ...]
        self._trail_points: list = []

        self._ml = 32.0
        self._mr = 12.0
        self._mt = 10.0
        self._mb = 22.0

    def setState(self, state: dict) -> None:
        self._state = state
        self.update()

    def pushSample(self, input_db: float) -> None:
        in_clamped = max(-80.0, min(0.0, input_db))
        out_db = self.evaluate_transfer(in_clamped)
        self._trail_points.append((in_clamped, out_db))
        if len(self._trail_points) > 26:
            self._trail_points.pop(0)
        self.update()

    def _plot_rect(self) -> QRectF:
        avail_w = max(80.0, self.width() - self._ml - self._mr)
        avail_h = max(80.0, self.height() - self._mt - self._mb)
        side = min(avail_w, avail_h * 1.15)
        left = self._ml + (avail_w - side) / 2.0
        return QRectF(left, self._mt, side, avail_h)

    def _db_to_x(self, db: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (db + 80.0) / 80.0))
        return plot.left() + norm * plot.width()

    def _x_to_db(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return -80.0 + norm * 80.0

    def _db_to_y(self, db: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (0.0 - db) / 80.0))
        return plot.top() + norm * plot.height()

    def _y_to_db(self, y: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (y - plot.top()) / plot.height()))
        return -norm * 80.0

    # ── 传输函数：Expander → Compressor(含 makeup) → Limiter ──

    def evaluate_transfer(self, in_db: float) -> float:
        st = self._state
        if not st.get("enabled", True):
            return in_db

        y = in_db

        eg = st.get("eg", {})
        if eg.get("enabled", True):
            t_eg = eg.get("threshold_db", -35.0)
            r_eg = 8.0 if eg.get("mode") == "Gate" else max(1.01, eg.get("ratio", 1.8))
            if y < t_eg:
                atten = (t_eg - y) * (1.0 - 1.0 / r_eg)
                y -= min(eg.get("range_db", 18.0), atten)

        comp = st.get("comp", {})
        if comp.get("enabled", True):
            t_c = comp.get("threshold_db", -15.0)
            r_c = max(1.01, comp.get("ratio", 2.5))
            knee_db = (comp.get("knee", 15.0) / 100.0) * 24.0
            mix = comp.get("mix", 100.0) / 100.0
            d = y - t_c

            if knee_db <= 0.1:
                y_comp = (t_c + d / r_c) if d > 0.0 else y
            else:
                if 2.0 * d < -knee_db:
                    y_comp = y
                elif 2.0 * abs(d) <= knee_db:
                    y_comp = y + (1.0 / r_c - 1.0) * ((d + knee_db / 2.0) ** 2) / (2.0 * knee_db)
                else:
                    y_comp = t_c + d / r_c

            y = (1.0 - mix) * y + mix * y_comp

        y += st.get("makeup", 0.0)

        lim = st.get("lim", {})
        if lim.get("enabled", True):
            t_l = lim.get("threshold_db", -6.0)
            if y > t_l:
                y = t_l

        return max(-80.0, min(0.0, y))

    # ── 阈值命中与拖拽 ────────────────────────────────────────

    def _hit_test_threshold(self, pos: QPointF, plot: QRectF) -> str:
        st = self._state
        if not st.get("enabled", True):
            return ""
        tol = 7.0
        if st.get("lim", {}).get("enabled", True):
            ly = self._db_to_y(st["lim"].get("threshold_db", -6.0), plot)
            if abs(pos.y() - ly) <= tol and plot.left() <= pos.x() <= plot.right():
                return "lim"
        if st.get("comp", {}).get("enabled", True):
            cx = self._db_to_x(st["comp"].get("threshold_db", -15.0), plot)
            if abs(pos.x() - cx) <= tol and plot.top() <= pos.y() <= plot.bottom():
                return "comp"
        if st.get("eg", {}).get("enabled", True):
            ex = self._db_to_x(st["eg"].get("threshold_db", -35.0), plot)
            if abs(pos.x() - ex) <= tol and plot.top() <= pos.y() <= plot.bottom():
                return "eg"
        return ""

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = None
        self._hover_target = ""
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            plot = self._plot_rect()
            hit = self._hit_test_threshold(event.position(), plot)
            if hit:
                self._dragging_target = hit

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._dragging_target == "eg":
            val = max(-50.0, min(0.0, round(self._x_to_db(event.position().x(), plot), 1)))
            self.egThresholdDragged.emit(val)
        elif self._dragging_target == "comp":
            val = max(-60.0, min(0.0, round(self._x_to_db(event.position().x(), plot), 1)))
            self.compThresholdDragged.emit(val)
        elif self._dragging_target == "lim":
            val = max(-30.0, min(0.0, round(self._y_to_db(event.position().y(), plot), 1)))
            self.limThresholdDragged.emit(val)
        else:
            hit = self._hit_test_threshold(event.position(), plot)
            self._hover_target = hit
            if hit in ("eg", "comp"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif hit == "lim":
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_target:
            self._dragging_target = ""
            self.thresholdDragEnded.emit()
            self.update()

    # ── 绘制 ─────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        bg_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#181A20"))
        bg_grad.setColorAt(1.0, QColor("#13151A"))
        p.fillRect(plot, bg_grad)

        grid_pen = QPen(QColor("#252832"), 1.0)
        p.setFont(QFont("Segoe UI", 8))

        for db in (0, -20, -40, -60):
            y = self._db_to_y(float(db), plot)
            x = self._db_to_x(float(db), plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

            p.setPen(QColor("#7E828E"))
            p.drawText(QRectF(0, y - 8, plot.left() - 6, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(db))
            p.drawText(QRectF(x - 16, plot.bottom() + 4, 32, 16),
                       Qt.AlignmentFlag.AlignCenter, str(db))

        p.setPen(QColor("#7E828E"))
        p.drawText(QRectF(plot.left() - 22, plot.bottom() + 4, 20, 16),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "dB")
        p.drawText(QRectF(plot.right() + 6, plot.top() - 2, 24, 16),
                   Qt.AlignmentFlag.AlignLeft, "1:1")

        p.setPen(QPen(QColor("#2E323D"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        # 1:1 对角基准线
        p.setPen(QPen(QColor("#343947"), 1.1, Qt.PenStyle.DashLine))
        p.drawLine(plot.bottomLeft(), plot.topRight())

        st = self._state
        enabled = st.get("enabled", True)

        # 2. 模块阈值参考线（可拖拽交互）
        if enabled:
            if st.get("eg", {}).get("enabled", True):
                ex = self._db_to_x(st["eg"].get("threshold_db", -35.0), plot)
                hi = (self._hover_target == "eg" or self._dragging_target == "eg")
                col = QColor(COLORS["eg"])
                col.setAlpha(230 if hi else 135)
                p.setPen(QPen(col, 1.8 if hi else 1.1))
                p.drawLine(QPointF(ex, plot.top()), QPointF(ex, plot.bottom()))

            if st.get("comp", {}).get("enabled", True):
                cx = self._db_to_x(st["comp"].get("threshold_db", -15.0), plot)
                hi = (self._hover_target == "comp" or self._dragging_target == "comp")
                col = QColor(COLORS["comp"])
                col.setAlpha(230 if hi else 135)
                p.setPen(QPen(col, 1.8 if hi else 1.1))
                p.drawLine(QPointF(cx, plot.top()), QPointF(cx, plot.bottom()))

            if st.get("lim", {}).get("enabled", True):
                ly = self._db_to_y(st["lim"].get("threshold_db", -6.0), plot)
                hi = (self._hover_target == "lim" or self._dragging_target == "lim")
                col = QColor(COLORS["lim"])
                col.setAlpha(230 if hi else 135)
                p.setPen(QPen(col, 1.8 if hi else 1.1))
                p.drawLine(QPointF(plot.left(), ly), QPointF(plot.right(), ly))

        # 3. 动态传输主曲线 + 渐变填充
        curve_path = QPainterPath()
        fill_path = QPainterPath()
        steps = 200
        for i in range(steps + 1):
            in_db = -80.0 + (i / steps) * 80.0
            out_db = self.evaluate_transfer(in_db)
            px = self._db_to_x(in_db, plot)
            py = self._db_to_y(out_db, plot)
            if i == 0:
                curve_path.moveTo(px, py)
                fill_path.moveTo(px, plot.bottom())
                fill_path.lineTo(px, py)
            else:
                curve_path.lineTo(px, py)
                fill_path.lineTo(px, py)
        fill_path.lineTo(plot.right(), plot.bottom())
        fill_path.closeSubpath()

        if enabled:
            area_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            area_grad.setColorAt(0.0, QColor(40, 216, 240, 32))
            area_grad.setColorAt(1.0, QColor(40, 216, 240, 4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(area_grad)
            p.drawPath(fill_path)

            p.setPen(QPen(QColor(40, 216, 240, 55), 4.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(curve_path)
            p.setPen(QPen(QColor("#28D8F0"), 2.0))
            p.drawPath(curve_path)
        else:
            p.setPen(QPen(QColor("#686C78"), 1.6))
            p.drawPath(curve_path)

        # 4. 阈值交点控制手柄
        if enabled:
            handles = []
            if st.get("eg", {}).get("enabled", True):
                tx = st["eg"].get("threshold_db", -35.0)
                handles.append(("eg", COLORS["eg"],
                                QPointF(self._db_to_x(tx, plot),
                                        self._db_to_y(self.evaluate_transfer(tx), plot))))
            if st.get("comp", {}).get("enabled", True):
                tx = st["comp"].get("threshold_db", -15.0)
                handles.append(("comp", COLORS["comp"],
                                QPointF(self._db_to_x(tx, plot),
                                        self._db_to_y(self.evaluate_transfer(tx), plot))))
            if st.get("lim", {}).get("enabled", True):
                ty = st["lim"].get("threshold_db", -6.0)
                handles.append(("lim", COLORS["lim"],
                                QPointF(self._db_to_x(ty, plot), self._db_to_y(ty, plot))))

            for tag, hex_col, pt in handles:
                is_hi = (self._hover_target == tag or self._dragging_target == tag)
                r = 5.5 if is_hi else 4.2
                c = QColor(hex_col)
                if is_hi:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(c.red(), c.green(), c.blue(), 70))
                    p.drawEllipse(pt, r + 4.0, r + 4.0)
                p.setPen(QPen(c, 1.6))
                p.setBrush(QColor("#14161C"))
                p.drawEllipse(pt, r, r)

        # 5. 实时信号动态轨迹点 + 轴分布刻度
        if enabled and self._trail_points:
            n = len(self._trail_points)
            for idx, (s_in, s_out) in enumerate(self._trail_points):
                age = (idx + 1) / n
                sx = self._db_to_x(s_in, plot)
                sy = self._db_to_y(s_out, plot)

                tick_col = QColor(40, 216, 240, int(180 * age))
                p.setPen(QPen(tick_col, 1.2))
                p.drawLine(QPointF(sx, plot.bottom() - 5.0), QPointF(sx, plot.bottom()))
                p.drawLine(QPointF(plot.left(), sy), QPointF(plot.left() + 5.0, sy))

                dot_r = 1.6 + 2.4 * age
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(175, 245, 255, int(220 * age)))
                p.drawEllipse(QPointF(sx, sy), dot_r, dot_r)

        # 6. HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx = self._mouse_pos.x()
            if self._dragging_target == "eg":
                hud_str = f"Exp/Gate Thresh: {st['eg'].get('threshold_db', -35.0):.1f} dB"
                hud_col = QColor(COLORS["eg"])
            elif self._dragging_target == "comp":
                hud_str = f"Comp Thresh: {st['comp'].get('threshold_db', -15.0):.1f} dB"
                hud_col = QColor(COLORS["comp"])
            elif self._dragging_target == "lim":
                hud_str = f"Limiter Ceiling: {st['lim'].get('threshold_db', -6.0):.1f} dB"
                hud_col = QColor(COLORS["lim"])
            else:
                in_v = self._x_to_db(mx, plot)
                out_v = self.evaluate_transfer(in_v)
                hud_str = f"In {in_v:.1f} dB  →  Out {out_v:.1f} dB"
                hud_col = QColor("#B4BAC8")

            hud_rect = QRectF(plot.left() + 8, plot.top() + 8, 178, 19)
            p.setPen(QPen(QColor("#323642"), 1.0))
            p.setBrush(QColor(16, 18, 23, 215))
            p.drawRoundedRect(hud_rect, 3.5, 3.5)
            p.setPen(hud_col)
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# ═════════════════════════════════════════════════════════════════════════════
# 模块卡片 1：Expander / Gate
# ═════════════════════════════════════════════════════════════════════════════

class ExpanderGateCard(ProModuleCard):
    """Expander/Gate 模块卡片（双模式切换 + 6 旋钮）。"""

    changed = pyqtSignal(dict)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(self, data: dict, parent: Optional[QWidget] = None) -> None:
        accent = COLORS["eg"]
        super().__init__("Expander / Gate", "E/G", accent, parent)
        self._data = copy.deepcopy(data)
        self._updating = False

        # Header 左侧：电源 + 双态切换
        hdr = self.layout().itemAt(0).widget()
        h_lay = hdr.layout()
        h_lay.takeAt(1)  # 移除 stretch，改为开关 + 分段按钮 + stretch

        self.power_sw = PowerToggleSwitch(self._data.get("enabled", True), accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.insertWidget(0, self.power_sw)

        seg_wrap = QFrame()
        seg_wrap.setStyleSheet(
            "background-color: #121318; border: 1px solid #2A2D37; border-radius: 4px;")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)

        self.btn_exp = QPushButton(I18N.tr("audio.fx.expander", "Expander"))
        self.btn_gate = QPushButton(I18N.tr("audio.fx.gate", "Gate"))
        for b in (self.btn_exp, self.btn_gate):
            b.setCheckable(True)
            b.setFixedHeight(20)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_lay.addWidget(b, 1)

        self.btn_exp.clicked.connect(lambda: self._on_mode("Expander"))
        self.btn_gate.clicked.connect(lambda: self._on_mode("Gate"))
        h_lay.insertWidget(1, seg_wrap, 1)
        h_lay.insertStretch(2)

        # Body（2×3 旋钮矩阵）
        row1 = self.add_section()
        self.c_thresh = KnobParameterCell(
            I18N.tr("audio.fx.threshold", "Threshold"), -50.0, 0.0, -35.0, accent,
            "-50.0", "dB", "0.0", lambda v: f"{v:.1f}", parse_float_clean,
            knob_size=42)
        self.c_range = KnobParameterCell(
            I18N.tr("audio.fx.range", "Range"), 0.0, 60.0, 18.0, accent,
            "0.0", "dB", "60.0", lambda v: f"{v:.1f}", parse_float_clean,
            knob_size=42)
        self.c_ratio = KnobParameterCell(
            I18N.tr("audio.fx.ratio", "Ratio"), 1.1, 3.0, 1.8, accent,
            "1:1.1", "", "1:3.0", _fmt_ratio_eg,
            lambda s, fb: parse_ratio_value(s, True, fb), knob_size=42)
        for c in (self.c_thresh, self.c_range, self.c_ratio):
            row1.addWidget(c, 1)

        row2 = self.add_section()
        self.c_attack = KnobParameterCell(
            I18N.tr("audio.fx.attack", "Attack"), 0.5, 100.0, 1.4, accent,
            "0.50", "ms", "100", format_ms, parse_float_clean, mode="log",
            knob_size=42)
        self.c_hold = KnobParameterCell(
            I18N.tr("audio.fx.hold", "Hold"), 0.0, 4000.0, 0.0, accent,
            "0.00", "ms", "4000", format_ms, parse_float_clean, mode="power",
            knob_size=42)
        self.c_release = KnobParameterCell(
            I18N.tr("audio.fx.release", "Release"), 50.0, 4000.0, 93.0, accent,
            "50", "ms", "4000", format_ms, parse_float_clean, mode="log",
            knob_size=42)
        for c in (self.c_attack, self.c_hold, self.c_release):
            row2.addWidget(c, 1)

        for cell in (self.c_thresh, self.c_range, self.c_ratio,
                     self.c_attack, self.c_hold, self.c_release):
            cell.valueChanged.connect(self._on_cell_changed)
            cell.interactionStarted.connect(self.interactionStarted.emit)
            cell.interactionEnded.connect(self.interactionEnded.emit)

        self.syncFromData(self._data)

    def syncFromData(self, data: dict) -> None:
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data.get("enabled", True))
        is_exp = (self._data.get("mode") == "Expander")

        self.setModuleActive(en)
        self.power_sw.setChecked(en)
        self.btn_exp.setChecked(is_exp)
        self.btn_gate.setChecked(not is_exp)

        active_seg = f"""
            QPushButton {{
                background-color: #1F242C;
                color: {COLORS['eg'] if en else '#9AA0B0'};
                border: 1px solid {COLORS['eg'] if en else '#3A3E4A'};
                border-radius: 3px;
                font-size: 10.5px;
                font-weight: 700;
            }}
        """
        inactive_seg = """
            QPushButton {
                background-color: transparent;
                color: #787D8C;
                border: none;
                border-radius: 3px;
                font-size: 10.5px;
                font-weight: 600;
            }
            QPushButton:hover { color: #D0D4E0; }
        """
        self.btn_exp.setStyleSheet(active_seg if is_exp else inactive_seg)
        self.btn_gate.setStyleSheet(active_seg if not is_exp else inactive_seg)

        self.c_thresh.setValue(self._data.get("threshold_db", -35.0))
        self.c_range.setValue(self._data.get("range_db", 18.0))
        self.c_ratio.setValue(self._data.get("ratio", 1.8))
        self.c_attack.setValue(self._data.get("attack_ms", 1.4))
        self.c_hold.setValue(self._data.get("hold_ms", 0.0))
        self.c_release.setValue(self._data.get("release_ms", 93.0))

        for cell in (self.c_thresh, self.c_range, self.c_ratio,
                     self.c_attack, self.c_hold, self.c_release):
            cell.setModuleActive(en)
        if not is_exp:
            self.c_ratio.setModuleActive(False)  # Gate 模式比率锁定

        self._updating = False

    def _on_power(self, checked: bool) -> None:
        self._data["enabled"] = checked
        self._emit()

    def _on_mode(self, mode: str) -> None:
        if self._data.get("mode") != mode:
            self._data["mode"] = mode
            self._emit()

    def _on_cell_changed(self, _val: float) -> None:
        if self._updating:
            return
        self._data["threshold_db"] = round(self.c_thresh.value(), 1)
        self._data["range_db"] = round(self.c_range.value(), 1)
        self._data["ratio"] = round(self.c_ratio.value(), 1)
        self._data["attack_ms"] = round(self.c_attack.value(), 2)
        self._data["hold_ms"] = round(self.c_hold.value(), 2)
        self._data["release_ms"] = round(self.c_release.value(), 1)
        self.changed.emit(copy.deepcopy(self._data))

    def _emit(self) -> None:
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))


# ═════════════════════════════════════════════════════════════════════════════
# 模块卡片 2：Compressor（含 Sidechain）
# ═════════════════════════════════════════════════════════════════════════════

class CompressorCard(ProModuleCard):
    """Compressor 模块卡片（7 旋钮 + Sidechain 控制列）。"""

    changed = pyqtSignal(dict)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(self, data: dict, parent: Optional[QWidget] = None) -> None:
        accent = COLORS["comp"]
        super().__init__("Compressor", "COMP", accent, parent)
        self._data = copy.deepcopy(data)
        self._updating = False
        # 侧链源列表 [(track_id, name), ...]
        self._sc_sources: list = []

        hdr = self.layout().itemAt(0).widget()
        h_lay = hdr.layout()
        self.power_sw = PowerToggleSwitch(self._data.get("enabled", True), accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.insertWidget(0, self.power_sw)

        # Body 2×4 网格 + Sidechain 列
        row1 = self.add_section()
        self.c_thresh = KnobParameterCell(
            I18N.tr("audio.fx.threshold", "Threshold"), -60.0, 0.0, -15.0, accent,
            "-60.0", "dB", "0.0", lambda v: f"{v:.1f}", parse_float_clean,
            knob_size=42)
        self.c_ratio = KnobParameterCell(
            I18N.tr("audio.fx.ratio", "Ratio"), 1.2, 20.0, 2.5, accent,
            "1.2:1", "", "20:1", _fmt_ratio_comp,
            lambda s, fb: parse_ratio_value(s, False, fb), mode="log", knob_size=42)
        self.c_knee = KnobParameterCell(
            I18N.tr("audio.fx.knee", "Knee"), 0.0, 100.0, 15.0, accent,
            "0", "", "100", lambda v: f"{int(round(v))}", parse_float_clean,
            knob_size=42)
        self.c_mix = KnobParameterCell(
            I18N.tr("audio.fx.mix", "Mix"), 0.0, 100.0, 100.0, accent,
            "0", "%", "100", lambda v: f"{int(round(v))}", parse_float_clean,
            knob_size=42)
        for c in (self.c_thresh, self.c_ratio, self.c_knee, self.c_mix):
            row1.addWidget(c, 1)

        row2 = self.add_section()
        self.c_attack = KnobParameterCell(
            I18N.tr("audio.fx.attack", "Attack"), 0.7, 100.0, 1.4, accent,
            "0.70", "ms", "100", format_ms, parse_float_clean, mode="log",
            knob_size=42)
        self.c_hold = KnobParameterCell(
            I18N.tr("audio.fx.hold", "Hold"), 0.0, 4000.0, 0.0, accent,
            "0.00", "ms", "4000", format_ms, parse_float_clean, mode="power",
            knob_size=42)
        self.c_release = KnobParameterCell(
            I18N.tr("audio.fx.release", "Release"), 50.0, 5000.0, 93.0, accent,
            "50", "ms", "5000", format_ms, parse_float_clean, mode="log",
            knob_size=42)
        for c in (self.c_attack, self.c_hold, self.c_release):
            row2.addWidget(c, 1)

        # Sidechain 控制列
        sc_col = QVBoxLayout()
        sc_col.setContentsMargins(4, 2, 2, 2)
        sc_col.setSpacing(5)

        sc_lbl = QLabel(I18N.tr("audio.fx.sidechain", "Sidechain"))
        sc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sc_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        sc_col.addWidget(sc_lbl)

        self.btn_sc_on = QPushButton(I18N.tr("audio.fx.sc_on", "On"))
        self.btn_sc_on.setCheckable(True)
        self.btn_sc_on.setFixedHeight(20)
        self.btn_sc_on.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sc_on.clicked.connect(self._on_sc_on)
        sc_col.addWidget(self.btn_sc_on)

        self.btn_sc_listen = QPushButton(I18N.tr("audio.fx.sc_listen", "Listen"))
        self.btn_sc_listen.setCheckable(True)
        self.btn_sc_listen.setFixedHeight(20)
        self.btn_sc_listen.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sc_listen.clicked.connect(self._on_sc_listen)
        sc_col.addWidget(self.btn_sc_listen)

        self.sc_source_combo = ChevronComboBox()
        self.sc_source_combo.setFixedHeight(21)
        self._rebuild_sc_combo()
        self.sc_source_combo.activated.connect(self._on_sc_source)
        sc_col.addWidget(self.sc_source_combo)

        row2.addLayout(sc_col, 1)

        for cell in (self.c_thresh, self.c_ratio, self.c_knee, self.c_mix,
                     self.c_attack, self.c_hold, self.c_release):
            cell.valueChanged.connect(self._on_cell_changed)
            cell.interactionStarted.connect(self.interactionStarted.emit)
            cell.interactionEnded.connect(self.interactionEnded.emit)

        self.syncFromData(self._data)

    def set_sidechain_sources(self, sources: list) -> None:
        """更新侧链源列表 [(track_id, name), ...]。"""
        self._sc_sources = list(sources)
        self._rebuild_sc_combo()

    def _rebuild_sc_combo(self) -> None:
        self.sc_source_combo.blockSignals(True)
        self.sc_source_combo.clear()
        self.sc_source_combo.addItem(I18N.tr("audio.fx.sc_internal", "Internal"), "internal")
        for tid, name in self._sc_sources:
            self.sc_source_combo.addItem(name, tid)
        cur = self._data.get("sc_source", "internal")
        idx = next((i for i in range(self.sc_source_combo.count())
                    if self.sc_source_combo.itemData(i) == cur), 0)
        self.sc_source_combo.setCurrentIndex(idx)
        self.sc_source_combo.blockSignals(False)

    def syncFromData(self, data: dict) -> None:
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data.get("enabled", True))

        self.setModuleActive(en)
        self.power_sw.setChecked(en)

        self.c_thresh.setValue(self._data.get("threshold_db", -15.0))
        self.c_ratio.setValue(self._data.get("ratio", 2.5))
        self.c_knee.setValue(self._data.get("knee", 15.0))
        self.c_mix.setValue(self._data.get("mix", 100.0))
        self.c_attack.setValue(self._data.get("attack_ms", 1.4))
        self.c_hold.setValue(self._data.get("hold_ms", 0.0))
        self.c_release.setValue(self._data.get("release_ms", 93.0))

        for cell in (self.c_thresh, self.c_ratio, self.c_knee, self.c_mix,
                     self.c_attack, self.c_hold, self.c_release):
            cell.setModuleActive(en)

        sc_on = bool(self._data.get("sc_on", False))
        sc_listen = bool(self._data.get("sc_listen", False))
        self.btn_sc_on.setChecked(sc_on)
        self.btn_sc_listen.setChecked(sc_listen)
        cur = self._data.get("sc_source", "internal")
        idx = next((i for i in range(self.sc_source_combo.count())
                    if self.sc_source_combo.itemData(i) == cur), 0)
        self.sc_source_combo.setCurrentIndex(idx)

        pill_btn_css = f"""
            QPushButton {{
                background-color: #16181E;
                color: #7E8392;
                border: 1px solid #2E323E;
                border-radius: 10px;
                font-size: 10px;
                font-weight: 600;
            }}
            QPushButton:hover {{ color: #E2E5EE; border-color: #464B5C; }}
            QPushButton:checked {{
                background-color: #162338;
                color: {COLORS['sc']};
                border: 1px solid {COLORS['sc']};
            }}
        """
        self.btn_sc_on.setStyleSheet(pill_btn_css)
        self.btn_sc_listen.setStyleSheet(pill_btn_css.replace(COLORS["sc"], "#E8B838"))

        self._updating = False

    def _on_power(self, checked: bool) -> None:
        self._data["enabled"] = checked
        self._emit()

    def _on_sc_on(self, checked: bool) -> None:
        self._data["sc_on"] = checked
        self._emit()

    def _on_sc_listen(self, checked: bool) -> None:
        self._data["sc_listen"] = checked
        self._emit()

    def _on_sc_source(self, index: int) -> None:
        tid = self.sc_source_combo.itemData(index)
        self._data["sc_source"] = str(tid) if tid else "internal"
        self._emit()

    def _on_cell_changed(self, _val: float) -> None:
        if self._updating:
            return
        self._data["threshold_db"] = round(self.c_thresh.value(), 1)
        self._data["ratio"] = round(self.c_ratio.value(), 1)
        self._data["knee"] = round(self.c_knee.value(), 0)
        self._data["mix"] = round(self.c_mix.value(), 0)
        self._data["attack_ms"] = round(self.c_attack.value(), 2)
        self._data["hold_ms"] = round(self.c_hold.value(), 2)
        self._data["release_ms"] = round(self.c_release.value(), 1)
        self.changed.emit(copy.deepcopy(self._data))

    def _emit(self) -> None:
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))


# ═════════════════════════════════════════════════════════════════════════════
# 模块卡片 3：Limiter
# ═════════════════════════════════════════════════════════════════════════════

class LimiterCard(ProModuleCard):
    """Limiter 模块卡片（4 旋钮）。"""

    changed = pyqtSignal(dict)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(self, data: dict, parent: Optional[QWidget] = None) -> None:
        accent = COLORS["lim"]
        super().__init__("Limiter", "BRICKWALL", accent, parent)
        self._data = copy.deepcopy(data)
        self._updating = False

        hdr = self.layout().itemAt(0).widget()
        h_lay = hdr.layout()
        self.power_sw = PowerToggleSwitch(self._data.get("enabled", True), accent)
        self.power_sw.toggled.connect(self._on_power)
        h_lay.insertWidget(0, self.power_sw)

        row1 = self.add_section()
        row1.addStretch(1)
        self.c_thresh = KnobParameterCell(
            I18N.tr("audio.fx.threshold", "Threshold"), -30.0, 0.0, -6.0, accent,
            "-30.0", "dB", "0.0", lambda v: f"{v:.1f}", parse_float_clean,
            knob_size=42)
        row1.addWidget(self.c_thresh, 1)
        row1.addStretch(1)

        row2 = self.add_section()
        self.c_attack = KnobParameterCell(
            I18N.tr("audio.fx.attack", "Attack"), 0.7, 30.0, 0.71, accent,
            "0.70", "ms", "30", format_ms, parse_float_clean, mode="log",
            knob_size=42)
        self.c_hold = KnobParameterCell(
            I18N.tr("audio.fx.hold", "Hold"), 0.0, 4000.0, 0.0, accent,
            "0.00", "ms", "4000", format_ms, parse_float_clean, mode="power",
            knob_size=42)
        self.c_release = KnobParameterCell(
            I18N.tr("audio.fx.release", "Release"), 50.0, 4000.0, 93.0, accent,
            "50", "ms", "4000", format_ms, parse_float_clean, mode="log",
            knob_size=42)
        for c in (self.c_attack, self.c_hold, self.c_release):
            row2.addWidget(c, 1)

        for cell in (self.c_thresh, self.c_attack, self.c_hold, self.c_release):
            cell.valueChanged.connect(self._on_cell_changed)
            cell.interactionStarted.connect(self.interactionStarted.emit)
            cell.interactionEnded.connect(self.interactionEnded.emit)

        self.syncFromData(self._data)

    def syncFromData(self, data: dict) -> None:
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data.get("enabled", True))

        self.setModuleActive(en)
        self.power_sw.setChecked(en)

        self.c_thresh.setValue(self._data.get("threshold_db", -6.0))
        self.c_attack.setValue(self._data.get("attack_ms", 0.71))
        self.c_hold.setValue(self._data.get("hold_ms", 0.0))
        self.c_release.setValue(self._data.get("release_ms", 93.0))

        for cell in (self.c_thresh, self.c_attack, self.c_hold, self.c_release):
            cell.setModuleActive(en)

        self._updating = False

    def _on_power(self, checked: bool) -> None:
        self._data["enabled"] = checked
        self._emit()

    def _on_cell_changed(self, _val: float) -> None:
        if self._updating:
            return
        self._data["threshold_db"] = round(self.c_thresh.value(), 1)
        self._data["attack_ms"] = round(self.c_attack.value(), 2)
        self._data["hold_ms"] = round(self.c_hold.value(), 2)
        self._data["release_ms"] = round(self.c_release.value(), 1)
        self.changed.emit(copy.deepcopy(self._data))

    def _emit(self) -> None:
        if not self._updating:
            self.syncFromData(self._data)
            self.changed.emit(copy.deepcopy(self._data))


# ═════════════════════════════════════════════════════════════════════════════
# DynamicsDialog 主面板
# ═════════════════════════════════════════════════════════════════════════════

class DynamicsDialog(QDialog):
    """动态处理器面板（Expander/Gate + Compressor + Limiter 单例管理）。

    信号：
        dynamics_preview(str, EffectChainParams)   — 拖动期实时听感
        dynamics_committed(str, EffectChainParams) — 松手/预设/关闭时提交（命令栈）
    """

    dynamics_preview = pyqtSignal(str, object)
    dynamics_committed = pyqtSignal(str, object)

    def __init__(
        self,
        track_id: str,
        track_name: str,
        chain: Optional[EffectChainParams],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._track_name = track_name
        self._chain = chain  # 当前 effect_chain（只读快照，编辑时由父组件回填）
        self._dragging = False
        self._updating = False

        self.setWindowTitle(
            I18N.tr("audio.fx.dynamics", "Dynamics") + f" - {track_name or track_id}")
        self.setModal(False)
        apply_float_panel(self)
        self.resize(980, 690)
        self.setMinimumSize(900, 620)

        self._presets: list[dict[str, Any]] = copy.deepcopy(DYNAMICS_PRESETS)
        # ST-01: 用户预设跨会话持久化
        self._effect_type = "dynamics"
        try:
            from src.features.edit.core.effect_preset import load_user_presets
            self._presets.extend(load_user_presets(self._effect_type))
        except Exception:  # noqa: BLE001
            pass
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

        title_lbl = QLabel(f"DYNAMICS PROCESSOR  ·  {track_name}")
        title_lbl.setStyleSheet(
            "color: #EAEDF6; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(12)

        add_preset_btn = QPushButton()
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setIcon(glyph_icon("plus"))
        add_preset_btn.setIconSize(QSize(12, 12))
        add_preset_btn.setFixedSize(23, 23)
        add_preset_btn.setToolTip(I18N.tr("audio.fx.save_preset", "保存为新预设"))
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(195)
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

        tb_lay.addStretch()

        reset_btn = QPushButton(I18N.tr("audio.fx.reset", "Reset"))
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedHeight(23)
        reset_btn.setMinimumWidth(max(48, reset_btn.sizeHint().width() + 6))
        reset_btn.clicked.connect(self.reset_dynamics)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # 2. 中部：Input | 传输曲线 | GR×3 | MakeUp | Output | Sidechain
        upper_panel = QFrame()
        upper_panel.setStyleSheet("background-color: #17191E;")
        up_lay = QHBoxLayout(upper_panel)
        up_lay.setContentsMargins(18, 12, 18, 10)
        up_lay.setSpacing(14)

        in_col = self._build_meter_column(I18N.tr("audio.fx.input", "Input"))
        self.meter_input = ProVerticalMeter(mode="level", show_ticks=True)
        in_col.addWidget(self.meter_input, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(in_col)

        self.dyn_graph = ProDynamicsGraphWidget()
        self.dyn_graph.egThresholdDragged.connect(self._on_graph_eg_thresh)
        self.dyn_graph.compThresholdDragged.connect(self._on_graph_comp_thresh)
        self.dyn_graph.limThresholdDragged.connect(self._on_graph_lim_thresh)
        self.dyn_graph.thresholdDragEnded.connect(self._on_drag_released)
        up_lay.addWidget(self.dyn_graph, 4)

        gr_col = self._build_meter_column(I18N.tr("audio.fx.gain_reduction", "Gain Reduction"))
        gr_meters_row = QHBoxLayout()
        gr_meters_row.setSpacing(6)
        self.meter_gr_eg = ProVerticalMeter(title="E/G", mode="gr", show_ticks=True,
                                            accent_color=COLORS["eg"])
        self.meter_gr_comp = ProVerticalMeter(title="C", mode="gr", show_ticks=True,
                                              accent_color=COLORS["comp"])
        self.meter_gr_lim = ProVerticalMeter(title="L", mode="gr", show_ticks=True,
                                             accent_color=COLORS["lim"])
        gr_meters_row.addWidget(self.meter_gr_eg)
        gr_meters_row.addWidget(self.meter_gr_comp)
        gr_meters_row.addWidget(self.meter_gr_lim)
        gr_col.addLayout(gr_meters_row, 1)
        up_lay.addLayout(gr_col)

        mu_col = self._build_meter_column(I18N.tr("audio.fx.makeup", "Make Up"))
        self.makeup_fader = MakeUpFaderStrip()
        self.makeup_fader.gainChanged.connect(self._on_makeup_changed)
        self.makeup_fader.interactionStarted.connect(lambda: setattr(self, "_dragging", True))
        self.makeup_fader.interactionEnded.connect(self._on_drag_released)
        mu_col.addWidget(self.makeup_fader, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(mu_col)

        out_col = self._build_meter_column(I18N.tr("audio.fx.output", "Output"))
        self.meter_output = ProVerticalMeter(mode="level", show_ticks=True)
        out_col.addWidget(self.meter_output, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(out_col)

        sc_col = self._build_meter_column(I18N.tr("audio.fx.sidechain", "Sidechain"))
        self.meter_sidechain = ProVerticalMeter(mode="level", show_ticks=True)
        sc_col.addWidget(self.meter_sidechain, 1, Qt.AlignmentFlag.AlignHCenter)
        up_lay.addLayout(sc_col)

        main_lay.addWidget(upper_panel, 5)

        # 3. 底部三卡片
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(14, 4, 14, 14)
        cards_row.setSpacing(9)

        self.eg_card = ExpanderGateCard(self._panel_state()["eg"])
        self.eg_card.changed.connect(self._on_eg_changed)
        self.eg_card.interactionStarted.connect(lambda: setattr(self, "_dragging", True))
        self.eg_card.interactionEnded.connect(self._on_drag_released)

        self.comp_card = CompressorCard(self._panel_state()["comp"])
        self.comp_card.changed.connect(self._on_comp_changed)
        self.comp_card.interactionStarted.connect(lambda: setattr(self, "_dragging", True))
        self.comp_card.interactionEnded.connect(self._on_drag_released)

        self.lim_card = LimiterCard(self._panel_state()["lim"])
        self.lim_card.changed.connect(self._on_lim_changed)
        self.lim_card.interactionStarted.connect(lambda: setattr(self, "_dragging", True))
        self.lim_card.interactionEnded.connect(self._on_drag_released)

        cards_row.addWidget(self.eg_card, 3)
        cards_row.addWidget(self.comp_card, 4)
        cards_row.addWidget(self.lim_card, 3)

        main_lay.addLayout(cards_row, 4)

    def _build_meter_column(self, header_text: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        lbl = QLabel(header_text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            "color: #A4AAB8; font-size: 10.5px; font-weight: 700; letter-spacing: 0.3px;")
        col.addWidget(lbl)
        return col

    # ── 面板状态 ↔ chain 实例 ─────────────────────────────────

    def _find_effect(self, effect_type: str) -> Optional[TrackEffectInstance]:
        """从当前 chain 找 DY 管理的实例（panel="dynamics"，取第一个）。"""
        if self._chain is None:
            return None
        return next(
            (e for e in self._chain.effects
             if e.effect_type == effect_type and getattr(e, "panel", "fx") == "dynamics"),
            None,
        )

    def set_chain(self, chain: Any) -> None:
        """外部静默回填整条链（clip 模式参数同步，不回发信号）。"""
        self._chain = chain
        self._refresh_all_views()

    def _panel_state(self) -> dict:
        """从 chain 实例提取面板状态（缺失实例用默认参数）。"""
        eg_inst = self._find_effect(_DYN_EXPANDER)
        # 旧 noise_gate 实例（panel="dynamics"）无损承接为 Gate
        if eg_inst is None:
            eg_inst = self._find_effect("noise_gate")
        eg_data = ExpanderParams.from_dict(eg_inst.params).to_dict() if eg_inst \
            else ExpanderParams().to_dict()

        comp_inst = self._find_effect(_DYN_COMPRESSOR)
        comp_data = CompressorParams.from_dict(comp_inst.params).to_dict() if comp_inst \
            else CompressorParams().to_dict()

        lim_inst = self._find_effect(_DYN_LIMITER)
        lim_data = LimiterParams.from_dict(lim_inst.params).to_dict() if lim_inst \
            else LimiterParams().to_dict()

        # DY-01：总电源 = 实例级旁路（TrackEffectInstance.enabled），
        # 与三模块 params.enabled 的独立旁路组合分离。旧实现从模块开关
        # OR 推导总电源——关→开会把 [False,True,False] 改写成全 True，
        # 摧毁模块旁路组合。
        existing = [inst for inst in (
            self._find_effect(_DYN_EXPANDER),
            self._find_effect(_DYN_COMPRESSOR),
            self._find_effect(_DYN_LIMITER),
        ) if inst is not None]
        master_on = any(bool(inst.enabled) for inst in existing) if existing else False

        return {"eg": eg_data, "comp": comp_data, "lim": lim_data,
                "makeup": comp_data.get("makeup_gain", 0.0),
                "enabled": master_on}

    def _write_instance(self, effect_type: str, params_dict: dict,
                        ensure_exists: bool = True,
                        instance_enabled: Optional[bool] = None) -> None:
        """更新 chain 中 DY 管理的实例（按 panel="dynamics" 匹配）。

        Args:
            instance_enabled: None=保留原实例级旁路；True/False=总电源切换
                （DY-01：只动实例级，模块 params.enabled 不受影响）。
        """
        effects = list(self._chain.effects) if self._chain is not None else []
        idx = next(
            (i for i, e in enumerate(effects)
             if e.effect_type == effect_type and getattr(e, "panel", "fx") == "dynamics"),
            -1,
        )
        if idx >= 0:
            old = effects[idx]
            effects[idx] = TrackEffectInstance(
                effect_type=effect_type, params=dict(params_dict),
                enabled=bool(old.enabled) if instance_enabled is None else bool(instance_enabled),
                order=old.order, instance_id=old.instance_id,
                panel="dynamics",
            )
        elif ensure_exists:
            new_order = max((e.order for e in effects), default=-1) + 1
            effects.append(TrackEffectInstance(
                effect_type=effect_type, params=dict(params_dict),
                # 新建实例：实例级旁路默认开（总电源语义），模块级由 params 承载
                enabled=True if instance_enabled is None else bool(instance_enabled),
                order=new_order, panel="dynamics",
            ))
        self._chain = EffectChainParams(effects=tuple(effects)) if effects else None

    # ── 状态与联动 ───────────────────────────────────────────

    def _refresh_all_views(self) -> None:
        state = self._panel_state()
        self._updating = True
        self.power_switch.setChecked(state["enabled"])
        self.makeup_fader.setGain(state["makeup"])
        self.eg_card.syncFromData(state["eg"])
        self.comp_card.syncFromData(state["comp"])
        self.lim_card.syncFromData(state["lim"])
        self.meter_sidechain.setActive(bool(state["comp"].get("sc_on", False)))
        self.dyn_graph.setState(copy.deepcopy(state))
        self._updating = False

    def _emit(self, commit: Optional[bool] = None) -> None:
        if self._updating:
            return
        use_commit = (not self._dragging) if commit is None else commit
        if use_commit:
            self.dynamics_committed.emit(self._track_id, self._chain)
        else:
            self.dynamics_preview.emit(self._track_id, self._chain)

    def _on_drag_released(self) -> None:
        if self._updating:
            return
        self._dragging = False
        self.dynamics_committed.emit(self._track_id, self._chain)

    # ── 事件处理 ─────────────────────────────────────────────

    def _on_power_toggled(self, enabled: bool) -> None:
        # DY-01：总电源只翻转实例级旁路（TrackEffectInstance.enabled），
        # 每模块 params.enabled 的独立旁路组合原样保留。
        # 关→开后模块组合不变：[False,True,False] 仍是 [False,True,False]。
        state = self._panel_state()
        for key, etype in (("eg", _DYN_EXPANDER), ("comp", _DYN_COMPRESSOR),
                           ("lim", _DYN_LIMITER)):
            self._write_instance(etype, state[key], ensure_exists=True,
                                 instance_enabled=enabled)
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_eg_changed(self, eg_data: dict) -> None:
        self._write_instance(_DYN_EXPANDER, eg_data)
        state = self._panel_state()
        self.dyn_graph.setState(state)
        self._emit()

    def _on_comp_changed(self, comp_data: dict) -> None:
        self._write_instance(_DYN_COMPRESSOR, comp_data)
        state = self._panel_state()
        self.meter_sidechain.setActive(bool(comp_data.get("sc_on", False)))
        self.dyn_graph.setState(state)
        self._emit()

    def _on_lim_changed(self, lim_data: dict) -> None:
        self._write_instance(_DYN_LIMITER, lim_data)
        state = self._panel_state()
        self.dyn_graph.setState(state)
        self._emit()

    def _on_makeup_changed(self, gain: float) -> None:
        state = self._panel_state()
        state["comp"]["makeup_gain"] = round(gain, 1)
        self._write_instance(_DYN_COMPRESSOR, state["comp"])
        self.dyn_graph.setState(state)
        self._emit()

    def _on_graph_eg_thresh(self, val: float) -> None:
        state = self._panel_state()
        state["eg"]["threshold_db"] = val
        self._write_instance(_DYN_EXPANDER, state["eg"])
        self.eg_card.syncFromData(state["eg"])
        self.dyn_graph.setState(state)
        self._emit()

    def _on_graph_comp_thresh(self, val: float) -> None:
        state = self._panel_state()
        state["comp"]["threshold_db"] = val
        self._write_instance(_DYN_COMPRESSOR, state["comp"])
        self.comp_card.syncFromData(state["comp"])
        self.dyn_graph.setState(state)
        self._emit()

    def _on_graph_lim_thresh(self, val: float) -> None:
        state = self._panel_state()
        state["lim"]["threshold_db"] = val
        self._write_instance(_DYN_LIMITER, state["lim"])
        self.lim_card.syncFromData(state["lim"])
        self.dyn_graph.setState(state)
        self._emit()

    # ── 预设 ─────────────────────────────────────────────────

    def _on_preset_selected(self, combo_idx: int) -> None:
        if 0 <= combo_idx < len(self._presets):
            preset = self._presets[combo_idx]["params"]
            # DY-01：预设保留自己的模块旁路状态（Flat Bypass 应用后三模块
            # 保持 params.enabled=False，不因总电源开启被翻转）。
            # 实例级旁路（总电源）不变：缺失实例按当前总电源创建。
            panel_on = self._panel_state()["enabled"]
            for key, etype in (("eg", _DYN_EXPANDER), ("comp", _DYN_COMPRESSOR),
                               ("lim", _DYN_LIMITER)):
                data = dict(preset[key])
                self._write_instance(etype, data, ensure_exists=True,
                                     instance_enabled=panel_on)
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
            self, I18N.tr("audio.fx.save_preset_title", "保存 Dynamics 预设"),
            I18N.tr("audio.fx.preset_name_prompt", "输入预设名称:"))
        if ok and name.strip():
            state = self._panel_state()
            clean = name.strip()
            params = {
                "eg": state["eg"], "comp": state["comp"], "lim": state["lim"]}
            self._presets.append({"name": clean, "params": params})
            self.preset_combo.addItem(clean)
            # ST-01: 原子持久化
            try:
                from src.features.edit.core.effect_preset import save_user_preset
                save_user_preset(self._effect_type, clean, params)
            except Exception:  # noqa: BLE001
                pass

    # ── 公开 API ─────────────────────────────────────────────

    def reset_dynamics(self) -> None:
        """DY-01：Reset 依产品默认值——三模块参数回 Flat Bypass 默认且模块
        开关关闭（params.enabled=False）；总电源（实例级旁路）不变；
        已存在的实例保留（不删除重建）。"""
        panel_on = self._panel_state()["enabled"]
        had = {etype: self._find_effect(etype) is not None
               for etype in (_DYN_EXPANDER, _DYN_COMPRESSOR, _DYN_LIMITER)}
        flat = DYNAMICS_PRESETS[1]["params"]
        for key, etype in (("eg", _DYN_EXPANDER), ("comp", _DYN_COMPRESSOR),
                           ("lim", _DYN_LIMITER)):
            data = dict(flat[key])
            data["enabled"] = False  # 模块级默认旁路（产品默认值）
            self._write_instance(etype, data, ensure_exists=had[etype],
                                 instance_enabled=panel_on if had[etype] else None)
        self._refresh_all_views()
        self._emit(commit=True)

    def get_chain(self) -> Optional[EffectChainParams]:
        return self._chain

    def update_chain(self, chain: Optional[EffectChainParams]) -> None:
        """外部回填 chain（timeline 变化后同步显示，不回发信号）。"""
        self._chain = chain
        self._refresh_all_views()

    def set_sidechain_sources(self, sources: list) -> None:
        """更新侧链源列表 [(track_id, name), ...]（宿主从面板下发）。"""
        self.comp_card.set_sidechain_sources(sources)

    def update_meters(
        self,
        input_db: float,
        gr_eg_db: float,
        gr_comp_db: float,
        gr_lim_db: float,
        output_db: float,
        sidechain_db: float = -60.0,
    ) -> None:
        """接入真实音频引擎实时电平与增益衰减值 (dBFS / dB)。"""
        self.meter_input.setValue(input_db)
        self.meter_gr_eg.setValue(gr_eg_db)
        self.meter_gr_comp.setValue(gr_comp_db)
        self.meter_gr_lim.setValue(gr_lim_db)
        self.meter_output.setValue(output_db)
        self.meter_sidechain.setValue(sidechain_db)
        self.dyn_graph.pushSample(input_db)


if __name__ == "__main__":  # pragma: no cover
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    win = DynamicsDialog("t1", "Audio 1", None, None)
    win.show()
    sys.exit(app.exec())


# ─────────────────────────────────────────────────────────────────────────────
# ClipDynamicsDialog — clip 级 Dynamics 完整单元对话框（CW-A1）
# ─────────────────────────────────────────────────────────────────────────────

def _dynamics_params_to_chain(params: DynamicsParams) -> EffectChainParams:
    """DynamicsParams → 内部 chain（panel="dynamics" 三实例）。

    供 ClipDynamicsDialog 复用完整的 DynamicsDialog 面板 UI：总电源写进
    三实例的 params.enabled（与 _on_power_toggled 语义一致），
    TrackEffectInstance.enabled 保持 True（面板自身管理旁路）。
    """
    d = params.to_dict()
    effects = []
    for order, (et, sub) in enumerate(
            (("expander", d["eg"]), ("compressor", d["comp"]), ("limiter", d["lim"]))):
        effects.append(TrackEffectInstance(
            effect_type=et,
            params={**sub, "enabled": bool(d["enabled"])},
            enabled=True, order=order, instance_id=f"clipdyn-{et}",
            panel="dynamics",
        ))
    return EffectChainParams(effects=tuple(effects))


def _chain_to_dynamics_params(chain: Any) -> DynamicsParams:
    """chain（panel="dynamics" 三实例）→ DynamicsParams（clip 提交形态）。"""
    d = DynamicsParams.default().to_dict()
    if chain is not None and getattr(chain, "effects", None):
        for e in chain.effects:
            if getattr(e, "panel", "fx") != "dynamics":
                continue
            if e.effect_type == "expander":
                d["eg"] = dict(e.params or {})
            elif e.effect_type == "compressor":
                d["comp"] = dict(e.params or {})
            elif e.effect_type == "limiter":
                d["lim"] = dict(e.params or {})
    comp = d.get("comp") or {}
    makeup = comp.get("makeup_gain", 0.0)
    enabled = bool(
        d["eg"].get("enabled", False)
        or comp.get("enabled", False)
        or d["lim"].get("enabled", False)
    )
    return DynamicsParams(
        enabled=enabled,
        makeup=float(makeup),
        eg=d["eg"], comp=comp, lim=d["lim"],
    )


class ClipDynamicsDialog(QDialog):
    """clip 级 Dynamics 编辑对话框——完整 Dynamics 面板的参数适配壳。

    DynamicsDialog 面板 UI（EG→Comp→Lim 单一整体，绝不拆分）直接内嵌复用；
    本壳只做 DynamicsParams（clip EffectInstance 单实例存储形态）与内部
    chain（面板管理形态）的互转，并把信号以 clip 契约转发：

      - 构造 ``(track_id, track_name, instance_id, initial_params, parent)``
      - 信号 ``dynamics_preview`` / ``dynamics_committed`` (track_id, DynamicsParams)
    """

    dynamics_preview = pyqtSignal(str, object)
    dynamics_committed = pyqtSignal(str, object)

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[DynamicsParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._clip_id = track_id
        self._instance_id = instance_id
        self._current: DynamicsParams = initial_params or DynamicsParams.default()
        self._updating = False

        self.setWindowTitle(
            I18N.tr("audio.fx.dynamics", "Dynamics") + f" - {track_name or track_id}")
        apply_float_panel(self)
        self.setModal(False)
        self.setMinimumSize(920, 640)
        self.setStyleSheet(PRO_DIALOG_QSS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        # 完整 Dynamics 面板作为内嵌控件复用（QDialog → Widget 旗标）
        self._panel = DynamicsDialog(
            track_id=track_id,
            track_name=track_name,
            chain=_dynamics_params_to_chain(self._current),
            parent=None,
        )
        self._panel.setWindowFlags(Qt.WindowType.Widget)
        lay.addWidget(self._panel, 1)

        self._panel.dynamics_preview.connect(self._on_panel_preview)
        self._panel.dynamics_committed.connect(self._on_panel_committed)

    def _on_panel_preview(self, _tid: str, chain: Any) -> None:
        if self._updating:
            return
        self._current = _chain_to_dynamics_params(chain)
        self.dynamics_preview.emit(self._clip_id, self._current)

    def _on_panel_committed(self, _tid: str, chain: Any) -> None:
        if self._updating:
            return
        self._current = _chain_to_dynamics_params(chain)
        self.dynamics_committed.emit(self._clip_id, self._current)

    def set_current_params(self, params: DynamicsParams) -> None:
        """外部静默回填（timeline 变化后同步，不回发信号）。"""
        self._updating = True
        self._current = params or DynamicsParams.default()
        self._panel.set_chain(_dynamics_params_to_chain(self._current))
        self._updating = False
