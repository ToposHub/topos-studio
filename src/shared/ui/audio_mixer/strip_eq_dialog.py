"""Parametric EQ Dialog — 6 频段全参量均衡器对话框（CW-A1 重构）。

自主旗舰参数化 EQ：交互式频响曲线（节点拖频率/增益、滚轮 Q、右键换滤波器
形状、双击开关/唤醒频段）+ 实时频谱（SPEC/PEAK）+ Voicing 染色 + Master
输出推子/电平表 + 6 张频段卡片（频率/增益/Q/斜率，L/ML/MH/H 精调区间）。

对接项目契约：
  - 构造：``(track_id, track_name, instance_id, initial_params, parent)``
    （EQ 徽章与 FX 列表/clip 适配器共用；instance_id 可为空串，由宿主补建）
  - 信号：``strip_eq_preview(track_id, StripEQParams)``（拖动期实时听感）与
    ``strip_eq_committed(track_id, StripEQParams)``（松手/预设/开关 → 命令栈）
  - 三段节流：连续交互 → preview，交互结束 → committed
  - A/B 对比槽与 "+" 存预设为对话框内存行为

遥测（由 TrackMixerWidget 转发引擎 33Hz 数据）：
  - ``update_spectrum(freqs_hz, post_db, peak_db)`` 频谱仪
  - ``update_output_meter(level_db)`` Master 电平表
  - ``update_sample_rate(sample_rate)`` 曲线求值采样率
"""

from __future__ import annotations

import copy
import logging
import math
from typing import Any, Callable, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.core import audio_dsp as _audio_dsp_common
from src.features.edit.core.audio_dsp import (
    VOICING_Q_SCALE,
    strip_eq_band_response_db,
)
from src.features.edit.core.timeline_v2 import (
    StripEQParams,
    auto_range_key,
    default_eq_bands_flat,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.eq_presets import PARAMETRIC_EQ_PRESETS
from src.shared.ui.audio_mixer.pro_widgets import (
    EditableValuePill,
    PowerToggleSwitch,
    apply_float_panel,
    glyph_icon,
    ProRotaryKnob,
    PRO_DIALOG_QSS,
    format_freq_hz,
    parse_float_clean,
    parse_freq_hz,
)
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# 声学常量
# ═════════════════════════════════════════════════════════════════════════════

FREQ_MIN = 20.0
FREQ_MAX = 20000.0

# 6 频段专属高级音频色板（低眩光、高辨识度）
BAND_PALETTE = [
    "#F26D6D",  # Band 1: 珊瑚红 (HPF / Sub)
    "#F0A848",  # Band 2: 琥珀金 (Low Shelf / Bass)
    "#4CD490",  # Band 3: 翡翠绿 (Low-Mid)
    "#28D8F0",  # Band 4: 冰晶青 (High-Mid)
    "#6495FA",  # Band 5: 钴蓝   (High Shelf / Presence)
    "#B67CF8",  # Band 6: 紫罗兰 (LPF / Air)
]

RANGE_PRESETS = {
    "L":  (30.0, 399.0, "30", "399"),
    "ML": (100.0, 1500.0, "100", "1.5K"),
    "MH": (450.0, 8000.0, "450", "8.0K"),
    "H":  (1400.0, 22000.0, "1.4K", "22.0K"),
}

# 调音台滤波染色特性（EQ-01：唯一真源在 audio_dsp，此处仅别名引用，
# 不得在 UI 侧复制系数表——响应曲线与 DSP 传输必须同源）
VOICING_Q_SCALE = _audio_dsp_common.VOICING_Q_SCALE

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def freq_to_note(hz: float) -> str:
    """频率 → 最近音名（如 A4）。"""
    if hz <= 0:
        return ""
    midi = round(69 + 12 * math.log2(hz / 440.0))
    name = NOTE_NAMES[midi % 12]
    octave = (midi // 12) - 1
    return f"{name}{octave}"


# ═════════════════════════════════════════════════════════════════════════════
# 极简声学曲线图标
# ═════════════════════════════════════════════════════════════════════════════

def draw_filter_glyph(painter: QPainter, rect: QRectF, ftype: str, color: QColor) -> None:
    """现代连续曲线风格的滤波器形状图标。"""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    cx = rect.center().x()
    cy = rect.center().y()
    w = min(rect.width() * 0.72, 22.0)
    h = min(rect.height() * 0.54, 9.5)
    left = cx - w / 2.0
    right = cx + w / 2.0
    top = cy - h / 2.0
    bot = cy + h / 2.0

    path = QPainterPath()
    if ftype == "HPF":
        path.moveTo(left + 1, bot)
        path.cubicTo(cx - 1.5, bot, cx - 2.5, top, cx + 2.5, top)
        path.lineTo(right - 1, top)
    elif ftype == "LPF":
        path.moveTo(left + 1, top)
        path.lineTo(cx - 2.5, top)
        path.cubicTo(cx + 2.5, top, cx + 1.5, bot, right - 1, bot)
    elif ftype == "Low Shelf":
        path.moveTo(left, top)
        path.lineTo(cx - 3.0, top)
        path.cubicTo(cx, top, cx, bot, cx + 3.0, bot)
        path.lineTo(right, bot)
    elif ftype == "High Shelf":
        path.moveTo(left, bot)
        path.lineTo(cx - 3.0, bot)
        path.cubicTo(cx, bot, cx, top, cx + 3.0, top)
        path.lineTo(right, top)
    elif ftype == "Bell":
        path.moveTo(left, bot - 1.0)
        path.cubicTo(cx - 4.5, bot - 1.0, cx - 3.5, top, cx, top)
        path.cubicTo(cx + 3.5, top, cx + 4.5, bot - 1.0, right, bot - 1.0)
    elif ftype == "Notch":
        path.moveTo(left, top + 0.5)
        path.lineTo(cx - 4.0, top + 0.5)
        path.cubicTo(cx - 1.5, top + 0.5, cx - 1.2, bot, cx, bot)
        path.cubicTo(cx + 1.2, bot, cx + 1.5, top + 0.5, cx + 4.0, top + 0.5)
        path.lineTo(right, top + 0.5)

    painter.drawPath(path)
    painter.restore()


# ═════════════════════════════════════════════════════════════════════════════
# 矢量滤波器形状选择器
# ═════════════════════════════════════════════════════════════════════════════

class FilterShapeSelector(QPushButton):
    """矢量滤波器形状选择按钮（下拉菜单）。"""

    shapeChanged = pyqtSignal(str)

    def __init__(self, options: list[str], current: str, accent_color: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._options = options
        self._current = current
        self._accent = QColor(accent_color)
        self._active = True
        self.setFixedSize(50, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._open_menu)

    def setCurrentShape(self, shape: str) -> None:
        if shape in self._options and self._current != shape:
            self._current = shape
            self.update()

    def setBandActive(self, active: bool) -> None:
        self._active = active
        self.update()

    def _open_menu(self) -> None:
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
        for opt in self._options:
            act = menu.addAction(opt)
            act.triggered.connect(lambda checked=False, o=opt: self._select(o))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _select(self, shape: str) -> None:
        if self._current != shape:
            self._current = shape
            self.update()
            self.shapeChanged.emit(shape)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        bg = QColor("#15161A") if self._active else QColor("#202126")
        border = QColor("#363842") if self._active else QColor("#2E3038")
        p.setPen(QPen(border, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 4.0, 4.0)

        glyph_color = self._accent if self._active else QColor("#747680")
        glyph_rect = QRectF(4, 2, self.width() - 16, self.height() - 4)
        draw_filter_glyph(p, glyph_rect, self._current, glyph_color)

        ax = self.width() - 8.5
        ay = self.height() / 2.0 - 1.2
        p.setPen(QPen(QColor("#8E909A"), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ax - 2.3, ay), QPointF(ax, ay + 2.5))
        p.drawLine(QPointF(ax, ay + 2.5), QPointF(ax + 2.3, ay))


# ═════════════════════════════════════════════════════════════════════════════
# 交互式频响曲线图
# ═════════════════════════════════════════════════════════════════════════════

class ProEQGraphWidget(QWidget):
    """频响曲线图：节点拖拽 / 滚轮 Q / 右键换形 / 双击开关与唤醒 / 频谱。"""

    nodeDragged = pyqtSignal(int, float, float)       # band_idx, freq, gain
    nodeDragEnded = pyqtSignal()                      # 拖拽结束（commit 时机）
    nodeQScrolled = pyqtSignal(int, float)            # band_idx, new_q
    nodeClicked = pyqtSignal(int)                     # band_idx
    nodeShapeRequested = pyqtSignal(int, str)         # band_idx, new_shape
    nodeToggleRequested = pyqtSignal(int)             # 双击节点开关频段
    bandSpawnRequested = pyqtSignal(float, float)     # 双击空白唤醒频段

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setMouseTracking(True)

        self._bands: list[dict] = [dict(b) for b in default_eq_bands_flat()]
        self._eq_enabled = True
        self._eq_voicing = "Clean"
        self._master_gain = 0.0
        self._sample_rate = 48000.0

        self._show_spectrum = True
        self._show_peak_hold = True

        self._hover_node = -1
        self._dragging_node = -1
        self._drag_start_freq = 0.0
        self._drag_start_gain = 0.0
        self._mouse_pos: Optional[QPointF] = None

        self._ml = 36.0
        self._mr = 36.0
        self._mt = 10.0
        self._mb = 24.0

        self._btn_spec_rect = QRectF()
        self._btn_peak_rect = QRectF()

        self._num_bins = 260
        self._spec_freqs = [
            FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** (i / (self._num_bins - 1)))
            for i in range(self._num_bins)
        ]
        self._spec_post: list[float] = []
        self._spec_peak: list[float] = []

        # AUD-37819（CW-A1 迁移）：总响应曲线缓存。键 = 归一化状态摘要；
        # 状态未变的连续重绘（如频谱刷新触发）不重算 320×bands 次 biquad。
        self._resp_cache_key: Optional[tuple] = None
        self._resp_cache_points: Optional[list] = None
        self._resp_cache_steps = 320

    def _state_cache_key(self) -> tuple:
        """当前影响总曲线的状态摘要（哈希键）。"""
        return (
            self._eq_enabled,
            self._eq_voicing,
            round(self._master_gain, 3),
            round(self._sample_rate, 1),
            tuple(
                (b["enabled"], b.get("solo", False), b["filter_type"],
                 round(b["freq"], 2), round(b["gain"], 2), round(b["q"], 2),
                 b.get("slope", 12))
                for b in self._bands
            ),
        )

    def _total_curve_points(self) -> list:
        """总响应曲线采样点（带缓存）。返回 [(px_norm 0..1, gain_db), ...]。"""
        key = self._state_cache_key()
        if self._resp_cache_key == key and self._resp_cache_points is not None:
            return self._resp_cache_points
        steps = self._resp_cache_steps
        points = []
        for i in range(steps + 1):
            norm = i / steps
            f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
            points.append((norm, self._total_db_at(f)))
        self._resp_cache_key = key
        self._resp_cache_points = points
        return points

    # ── 状态 ──────────────────────────────────────────────────

    def setState(self, bands: list[dict], eq_enabled: bool, eq_voicing: str,
                 master_gain: float) -> None:
        self._bands = bands
        self._eq_enabled = eq_enabled
        self._eq_voicing = eq_voicing
        self._master_gain = master_gain
        self.update()

    def update_spectrum(self, freqs_hz: list[float], post_db: list[float],
                        peak_db: Optional[list[float]] = None) -> None:
        self._spec_freqs = freqs_hz
        self._spec_post = post_db
        self._spec_peak = peak_db if peak_db is not None else post_db
        self._num_bins = len(freqs_hz)
        self.update()

    def has_spectrum(self) -> bool:
        return bool(self._spec_post)

    def reset_peak_hold(self) -> None:
        """MX-01/EQ-03：清空频谱 peak hold 历史轨迹（停止/seek/手动重置）。"""
        self._spec_peak = []
        self.update()

    # ── 坐标映射 ─────────────────────────────────────────────

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml, self._mt,
            max(10.0, self.width() - self._ml - self._mr),
            max(10.0, self.height() - self._mt - self._mb),
        )

    def _freq_to_x(self, freq: float, plot: QRectF) -> float:
        f = max(FREQ_MIN, min(FREQ_MAX, freq))
        norm = math.log10(f / FREQ_MIN) / math.log10(FREQ_MAX / FREQ_MIN)
        return plot.left() + norm * plot.width()

    def _x_to_freq(self, x: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)

    def _gain_to_y(self, gain_db: float, plot: QRectF) -> float:
        g = max(-25.0, min(25.0, gain_db))
        norm = (20.0 - g) / 40.0
        return plot.top() + norm * plot.height()

    def _y_to_gain(self, y: float, plot: QRectF) -> float:
        norm = max(0.0, min(1.0, (y - plot.top()) / plot.height()))
        return 20.0 - norm * 40.0

    def _spec_db_to_y(self, spec_db: float, plot: QRectF) -> float:
        norm = (0.0 - spec_db) / 80.0
        return plot.top() + max(0.0, min(1.0, norm)) * plot.height()

    # ── Biquad 频响（客户端曲线求值）────────────────────────

    def _band_magnitude_db(self, band: dict, freq: float) -> float:
        """EQ-01：曲线求值委托 audio_dsp 共享服务（与 DSP 传输严格同源）。

        旧实现在此重复了一套系数公式（Shelf Q 与 DSP 不同源、HPF/LPF 用
        解析式而 DSP 是错误级联），导致"图上画的"与"耳朵听到的"不一致。
        """
        return strip_eq_band_response_db(
            band, float(freq), self._sample_rate, self._eq_voicing)

    def _total_db_at(self, freq: float) -> float:
        if not self._eq_enabled:
            return 0.0
        total = self._master_gain
        # EQ-02 审查修正：与 DSP/共享服务一致——Solo 时累加**全部** solo
        # 频段（UI 正常互斥，但手工编辑的工程文件可能带多个 solo 标志）
        solo_active = any(b["enabled"] and b.get("solo", False)
                          for b in self._bands)
        for b in self._bands:
            if solo_active and not (b["enabled"] and b.get("solo", False)):
                continue
            total += self._band_magnitude_db(b, freq)
        return total

    def _node_pos(self, band: dict, plot: QRectF) -> QPointF:
        x = self._freq_to_x(band["freq"], plot)
        if band["filter_type"] in ("HPF", "LPF"):
            y = self._gain_to_y(self._master_gain, plot)
        else:
            y = self._gain_to_y(band["gain"] + self._master_gain, plot)
        return QPointF(x, y)

    # ── 交互 ─────────────────────────────────────────────────

    def _hit_test_node(self, pos: QPointF) -> int:
        plot = self._plot_rect()
        for idx in reversed(range(len(self._bands))):
            b = self._bands[idx]
            if not b["enabled"]:
                continue
            np = self._node_pos(b, plot)
            if math.hypot(pos.x() - np.x(), pos.y() - np.y()) <= 12.5:
                return idx
        return -1

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = None
        self._hover_node = -1
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._btn_spec_rect.contains(pos):
                self._show_spectrum = not self._show_spectrum
                self.update()
                return
            if self._btn_peak_rect.contains(pos):
                self._show_peak_hold = not self._show_peak_hold
                self.update()
                return

            idx = self._hit_test_node(pos)
            if idx >= 0:
                self._dragging_node = idx
                self._drag_start_freq = self._bands[idx]["freq"]
                self._drag_start_gain = self._bands[idx]["gain"]
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.nodeClicked.emit(idx)
                self.update()

        elif event.button() == Qt.MouseButton.RightButton:
            idx = self._hit_test_node(pos)
            if idx >= 0:
                self._show_node_context_menu(idx, event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        plot = self._plot_rect()
        if not plot.contains(pos):
            return

        idx = self._hit_test_node(pos)
        if idx >= 0:
            self.nodeToggleRequested.emit(idx)
        else:
            f = self._x_to_freq(pos.x(), plot)
            g = max(-20.0, min(20.0, round(self._y_to_gain(pos.y(), plot) - self._master_gain, 1)))
            self.bandSpawnRequested.emit(round(f, 1), g)

    def _show_node_context_menu(self, idx: int, global_pos) -> None:
        if idx == 0:
            opts = ["HPF", "Low Shelf", "Bell"]
        elif idx == 5:
            opts = ["LPF", "High Shelf", "Bell"]
        else:
            opts = ["Low Shelf", "Bell", "High Shelf", "Notch"]

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #1B1C20; border: 1px solid #383A44; color: #E2E4EC; font-size: 11px; }
            QMenu::item { padding: 5px 16px; }
            QMenu::item:selected { background-color: #2A2E3A; color: #28D8F0; }
        """)
        for opt in opts:
            act = menu.addAction(f"Filter: {opt}")
            act.triggered.connect(lambda checked=False, o=opt: self.nodeShapeRequested.emit(idx, o))
        menu.exec(global_pos)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = event.position()
        plot = self._plot_rect()
        if self._dragging_node >= 0:
            b = self._bands[self._dragging_node]
            mods = event.modifiers()

            if mods & Qt.KeyboardModifier.ControlModifier:
                new_freq = self._drag_start_freq  # Ctrl 锁定频率
            else:
                new_freq = self._x_to_freq(event.position().x(), plot)

            if b["filter_type"] in ("HPF", "LPF") or (mods & Qt.KeyboardModifier.AltModifier):
                new_gain = self._drag_start_gain  # Alt 锁定增益
            else:
                raw_g = self._y_to_gain(event.position().y(), plot) - self._master_gain
                new_gain = max(-20.0, min(20.0, round(raw_g, 1)))

            self.nodeDragged.emit(self._dragging_node, new_freq, new_gain)
        else:
            hit = self._hit_test_node(event.position())
            self._hover_node = hit
            self.setCursor(Qt.CursorShape.OpenHandCursor if hit >= 0 else Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_node >= 0:
            self._dragging_node = -1
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._hover_node >= 0
                           else Qt.CursorShape.CrossCursor)
            self.nodeDragEnded.emit()
            self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802
        target = self._dragging_node if self._dragging_node >= 0 else self._hover_node
        if target >= 0:
            b = self._bands[target]
            delta = event.angleDelta().y() / 120.0
            new_q = max(0.3, min(10.3, round(b["q"] + delta * 0.2, 1)))
            self.nodeQScrolled.emit(target, new_q)
            event.accept()

    # ── 绘制 ─────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 深邃夜空渐变底色
        bg_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#181A20"))
        bg_grad.setColorAt(1.0, QColor("#13151A"))
        p.fillRect(plot, bg_grad)

        # 2. 水平刻度线
        grid_pen = QPen(QColor("#242730"), 1.0)
        zero_pen = QPen(QColor("#383D4B"), 1.1)
        p.setFont(QFont("Segoe UI", 8))

        left_ticks = [0, -10, -20, -30, -40, -50, -60, -70, -80]
        right_ticks = ["+20", "", "+10", "", "0", "", "-10", "", "-20"]

        for i, l_val in enumerate(left_ticks):
            y = plot.top() + (i / (len(left_ticks) - 1)) * plot.height()
            p.setPen(zero_pen if l_val == -40 else grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

            p.setPen(QColor("#7E828E"))
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

        # 3. 对数频率网格线
        freq_ticks = [
            (31.25, "31"), (62.5, "62"), (125.0, "125"), (250.0, "250"),
            (500.0, "500"), (1000.0, "1K"), (2000.0, "2K"), (4000.0, "4K"),
            (8000.0, "8K"), (16000.0, "16K"),
        ]
        p.setPen(QColor("#7E828E"))
        p.drawText(QRectF(plot.left(), plot.bottom() + 5, 24, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Hz")
        for f_val, f_label in freq_ticks:
            x = self._freq_to_x(f_val, plot)
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor("#7E828E"))
            p.drawText(QRectF(x - 18, plot.bottom() + 5, 36, 16),
                       Qt.AlignmentFlag.AlignCenter, f_label)

        p.setPen(QPen(QColor("#2E323D"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        # 4. 实时频谱仪（引擎遥测到达前不绘制）
        if self._show_spectrum and len(self._spec_freqs) > 1 \
                and len(self._spec_post) == len(self._spec_freqs):
            peak_path = QPainterPath()
            post_path = QPainterPath()
            fill_poly = QPolygonF()
            first_x = self._freq_to_x(self._spec_freqs[0], plot)
            fill_poly.append(QPointF(first_x, plot.bottom()))

            for idx_s, f_s in enumerate(self._spec_freqs):
                sx = self._freq_to_x(f_s, plot)
                py = self._spec_db_to_y(self._spec_peak[idx_s], plot)
                sy = self._spec_db_to_y(self._spec_post[idx_s], plot)
                if idx_s == 0:
                    peak_path.moveTo(sx, py)
                    post_path.moveTo(sx, sy)
                else:
                    peak_path.lineTo(sx, py)
                    post_path.lineTo(sx, sy)
                fill_poly.append(QPointF(sx, sy))

            last_x = self._freq_to_x(self._spec_freqs[-1], plot)
            fill_poly.append(QPointF(last_x, plot.bottom()))

            spec_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            spec_grad.setColorAt(0.0, QColor(56, 128, 210, 75))
            spec_grad.setColorAt(1.0, QColor(36, 72, 130, 18))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(spec_grad)
            p.drawPolygon(fill_poly)

            if self._show_peak_hold:
                p.setPen(QPen(QColor(135, 142, 160, 120), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(peak_path)

            p.setPen(QPen(QColor(82, 154, 235, 195), 1.15))
            p.drawPath(post_path)

        # 5. 各频段独立彩色包络填充（Solo 隔离，其余暗化省略）
        steps = 320
        zero_y = self._gain_to_y(self._master_gain, plot)
        solo_idx = next((i for i, b in enumerate(self._bands)
                         if b["enabled"] and b.get("solo", False)), -1)

        if self._eq_enabled:
            if abs(self._master_gain) >= 0.05:
                p.setPen(QPen(QColor(255, 255, 255, 36), 1.0, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(plot.left(), zero_y), QPointF(plot.right(), zero_y))

            for idx_b, b in enumerate(self._bands):
                if not b["enabled"]:
                    continue
                if solo_idx >= 0 and idx_b != solo_idx:
                    continue
                is_solo = (idx_b == solo_idx)
                is_focus = (idx_b == self._hover_node or idx_b == self._dragging_node)
                b_col = QColor(BAND_PALETTE[idx_b])

                alpha = 54 if (is_focus or is_solo) else 18
                stroke_alpha = 140 if (is_focus or is_solo) else 42

                b_fill = QPainterPath()
                for i in range(steps + 1):
                    norm = i / steps
                    f = FREQ_MIN * ((FREQ_MAX / FREQ_MIN) ** norm)
                    x = plot.left() + norm * plot.width()
                    db = self._master_gain + self._band_magnitude_db(b, f)
                    y = self._gain_to_y(db, plot)
                    if i == 0:
                        b_fill.moveTo(x, zero_y)
                        b_fill.lineTo(x, y)
                    else:
                        b_fill.lineTo(x, y)
                b_fill.lineTo(plot.right(), zero_y)
                b_fill.closeSubpath()

                p.setPen(QPen(QColor(b_col.red(), b_col.green(), b_col.blue(), stroke_alpha),
                              1.15 if is_solo else 1.0))
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), alpha))
                p.drawPath(b_fill)

        # 6. 总 EQ 响应主曲线（AUD-37819: 状态未变的重绘复用缓存点）
        eq_path = QPainterPath()
        eq_area = QPainterPath()
        for norm, out_db in self._total_curve_points():
            x = plot.left() + norm * plot.width()
            y = self._gain_to_y(out_db, plot)
            if not eq_path.elementCount():
                eq_path.moveTo(x, y)
                eq_area.moveTo(x, zero_y)
                eq_area.lineTo(x, y)
            else:
                eq_path.lineTo(x, y)
                eq_area.lineTo(x, y)
        eq_area.lineTo(plot.right(), zero_y)
        eq_area.closeSubpath()

        if self._eq_enabled:
            area_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            area_grad.setColorAt(0.0, QColor(240, 248, 255, 28))
            area_grad.setColorAt(0.5, QColor(240, 248, 255, 8))
            area_grad.setColorAt(1.0, QColor(240, 248, 255, 28))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(area_grad)
            p.drawPath(eq_area)

            p.setPen(QPen(QColor(235, 248, 255, 45), 4.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(eq_path)
            p.setPen(QPen(QColor("#F2F8FC"), 1.9))
            p.drawPath(eq_path)
        else:
            p.setPen(QPen(QColor("#686C78"), 1.6))
            p.drawPath(eq_path)

        # 7. 频段彩色控制节点（Solo 金色外环）
        node_font = QFont("Segoe UI", 8)
        node_font.setWeight(QFont.Weight.Bold)
        p.setFont(node_font)

        for idx, b in enumerate(self._bands):
            if not b["enabled"]:
                continue
            pt = self._node_pos(b, plot)
            is_solo = (idx == solo_idx)
            dimmed = (solo_idx >= 0 and not is_solo)
            b_col = QColor(BAND_PALETTE[idx]) if (self._eq_enabled and not dimmed) else QColor("#565A66")
            is_hi = (idx == self._hover_node or idx == self._dragging_node or is_solo)
            radius = 10.2 if is_hi else 8.4

            if is_solo and self._eq_enabled:
                p.setPen(QPen(QColor("#F6CE58"), 1.4))
                p.setBrush(QColor(232, 184, 56, 55))
                p.drawEllipse(pt, radius + 5.5, radius + 5.5)
            elif is_hi and self._eq_enabled:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 65))
                p.drawEllipse(pt, radius + 4.5, radius + 4.5)

            p.setPen(QPen(b_col, 1.8))
            p.setBrush(QColor("#14161C"))
            p.drawEllipse(pt, radius, radius)

            p.setPen(QColor("#F6F8FC") if not dimmed else QColor("#8A8E9A"))
            p.drawText(
                QRectF(pt.x() - radius, pt.y() - radius, radius * 2, radius * 2),
                Qt.AlignmentFlag.AlignCenter,
                str(b["index"]),
            )

        # 8. 右上角悬浮频谱开关胶囊 (SPEC / PEAK)
        self._btn_spec_rect = QRectF(plot.right() - 88, plot.top() + 8, 38, 18)
        self._btn_peak_rect = QRectF(plot.right() - 46, plot.top() + 8, 38, 18)
        for rect, label, active in (
            (self._btn_spec_rect, "SPEC", self._show_spectrum),
            (self._btn_peak_rect, "PEAK", self._show_peak_hold and self._show_spectrum),
        ):
            p.setPen(QPen(QColor("#3A4252") if active else QColor("#282B34"), 1.0))
            p.setBrush(QColor(24, 34, 46, 210) if active else QColor(18, 20, 25, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#6E727E"))
            p.setFont(QFont("Segoe UI", 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 9. 实时十字准星 & HUD
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(mx, plot.top()), QPointF(mx, plot.bottom()))
            p.drawLine(QPointF(plot.left(), my), QPointF(plot.right(), my))

            if self._dragging_node >= 0:
                db_band = self._bands[self._dragging_node]
                hud_str = (f"Band {db_band['index']}  •  {format_freq_hz(db_band['freq'])}Hz "
                           f"({freq_to_note(db_band['freq'])})  •  {db_band['gain']:+.1f} dB"
                           f"  •  Q {db_band['q']:.1f}")
                hud_col = QColor(BAND_PALETTE[self._dragging_node])
            else:
                cur_f = self._x_to_freq(mx, plot)
                cur_g = self._y_to_gain(my, plot)
                hud_str = f"{format_freq_hz(cur_f)}Hz ({freq_to_note(cur_f)})   {cur_g:+.1f} dB"
                hud_col = QColor("#B4BAC8")

            hud_w = 245 if self._dragging_node >= 0 else 165
            hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
            p.setPen(QPen(QColor("#323642"), 1.0))
            p.setBrush(QColor(16, 18, 23, 215))
            p.drawRoundedRect(hud_rect, 3.5, 3.5)
            p.setPen(hud_col)
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# ═════════════════════════════════════════════════════════════════════════════
# Master Output 推子 + 电平表
# ═════════════════════════════════════════════════════════════════════════════

class MasterOutputStrip(QWidget):
    """EQ Master 增益推子 + 实时输出峰值电平表（-60..0 dB）。"""

    gainChanged = pyqtSignal(float)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._gain = 0.0
        self._meter_db = -60.0
        self._dragging = False
        self._clipped = False
        self.setFixedWidth(64)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def setGain(self, val: float, emit: bool = False) -> None:
        clamped = max(-20.0, min(20.0, round(val, 1)))
        if abs(clamped - self._gain) > 1e-4:
            self._gain = clamped
            self.update()
            if emit:
                self.gainChanged.emit(self._gain)

    def setMeterLevel(self, level_db: float) -> None:
        self._meter_db = max(-60.0, min(0.0, level_db))
        if level_db >= -0.1:
            self._clipped = True
        self.update()

    def _track_rect(self) -> QRectF:
        return QRectF(20.0, 10.0, 4.0, max(40.0, self.height() - 36.0))

    def _y_to_gain(self, y: float) -> float:
        tr = self._track_rect()
        norm = max(0.0, min(1.0, (y - tr.top()) / tr.height()))
        return 20.0 - norm * 40.0

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if event.position().y() <= 14.0 and event.position().x() >= 40.0:
                self._clipped = False
                self.update()
                return
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

        p.setPen(QPen(QColor("#525664"), 1.0))
        for i in range(5):
            ty = tr.top() + (i / 4.0) * tr.height()
            p.drawLine(QPointF(cx - 14, ty), QPointF(cx - 10, ty))
            p.drawLine(QPointF(cx + 10, ty), QPointF(cx + 14, ty))

        p.setPen(QPen(QColor("#282B34"), 1.0))
        p.setBrush(QColor("#0E1014"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        meter_rect = QRectF(46.0, tr.top(), 5.0, tr.height())
        p.setPen(QPen(QColor("#262932"), 1.0))
        p.setBrush(QColor("#0D0E12"))
        p.drawRoundedRect(meter_rect, 2.0, 2.0)

        m_norm = max(0.0, min(1.0, (self._meter_db + 60.0) / 60.0))
        if m_norm > 0.01:
            fill_h = meter_rect.height() * m_norm
            fill_rect = QRectF(meter_rect.left() + 0.5, meter_rect.bottom() - fill_h,
                               meter_rect.width() - 1.0, fill_h)
            m_grad = QLinearGradient(meter_rect.bottomLeft(), meter_rect.topLeft())
            m_grad.setColorAt(0.0, QColor("#28C882"))
            m_grad.setColorAt(0.75, QColor("#E8B838"))
            m_grad.setColorAt(1.0, QColor("#F05050"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(m_grad)
            p.drawRoundedRect(fill_rect, 1.5, 1.5)

        clip_rect = QRectF(46.0, tr.top() - 7.0, 5.0, 4.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#252832"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)

        norm = (20.0 - self._gain) / 40.0
        cy = tr.top() + norm * tr.height()
        cap_w, cap_h = 18.0, 24.0
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

        font = QFont("Segoe UI", 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor("#28D8F0"))
        val_str = f"{self._gain:+.1f}" if abs(self._gain) >= 0.05 else "0.0"
        p.drawText(QRectF(0, self.height() - 22, self.width(), 18),
                   Qt.AlignmentFlag.AlignCenter, val_str)


# ═════════════════════════════════════════════════════════════════════════════
# 单频段控制卡片
# ═════════════════════════════════════════════════════════════════════════════

class ProBandCard(QFrame):
    """单频段控制卡片（频段色条 + Solo + 滤波器形状 + Freq/Gain/Q）。"""

    bandChanged = pyqtSignal(int, dict)
    soloToggled = pyqtSignal(int, bool)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(self, band_data: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._data = copy.deepcopy(band_data)
        self._idx = self._data["index"] - 1
        self._accent = BAND_PALETTE[self._idx]
        self._updating = False
        self._selected = False

        self.setObjectName("BandCard")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 1. 顶部栏：频段开关 + Solo + 滤波器形状
        header = QWidget()
        header.setFixedHeight(36)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(7, 6, 7, 5)
        h_lay.setSpacing(4)

        self.band_btn = QPushButton(f"Band {self._data['index']}")
        self.band_btn.setCheckable(True)
        self.band_btn.setFixedHeight(22)
        self.band_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.band_btn.clicked.connect(self._on_toggle_band)
        h_lay.addWidget(self.band_btn, 1)

        self.solo_btn = QPushButton("S")
        self.solo_btn.setCheckable(True)
        self.solo_btn.setFixedSize(22, 22)
        self.solo_btn.setToolTip(I18N.tr("audio.fx.solo_band", "独奏监听当前频段 (Solo Band)"))
        self.solo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.solo_btn.setStyleSheet("""
            QPushButton {
                background-color: #252830;
                color: #848996;
                border: 1px solid #323642;
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #E8B838;
                color: #121316;
                border: 1px solid #F6CE58;
            }
        """)
        self.solo_btn.clicked.connect(self._on_toggle_solo)
        h_lay.addWidget(self.solo_btn)

        idx = self._data["index"]
        if idx == 1:
            shapes = ["HPF", "Low Shelf", "Bell"]
        elif idx == 6:
            shapes = ["LPF", "High Shelf", "Bell"]
        else:
            shapes = ["Low Shelf", "Bell", "High Shelf", "Notch"]

        self.shape_selector = FilterShapeSelector(shapes, self._data["filter_type"], self._accent)
        self.shape_selector.shapeChanged.connect(self._on_shape_changed)
        h_lay.addWidget(self.shape_selector)
        root.addWidget(header)

        # 2. Frequency 区块
        freq_sec = QFrame()
        freq_sec.setObjectName("SubSection")
        freq_lay = QVBoxLayout(freq_sec)
        freq_lay.setContentsMargins(8, 6, 8, 7)
        freq_lay.setSpacing(6)

        f_top = QHBoxLayout()
        f_left = QVBoxLayout()
        f_left.setSpacing(4)
        f_lbl = QLabel(I18N.tr("audio.fx.frequency", "Frequency"))
        f_lbl.setObjectName("ParamTitle")
        self.freq_pill = EditableValuePill(format_freq_hz(self._data["freq"]), self._accent, width=54)
        self.freq_pill.valueEdited.connect(self._on_freq_text)
        f_left.addWidget(f_lbl)
        f_left.addWidget(self.freq_pill)
        f_left.addStretch()

        f_right = QVBoxLayout()
        f_right.setSpacing(1)
        r_min, r_max, r_min_s, r_max_s = RANGE_PRESETS[self._data["range_key"]]
        self.freq_knob = ProRotaryKnob(
            r_min, r_max, self._data["freq"], self._data["freq"], self._accent,
            mode="log", size=36)
        self.freq_knob.valueChanged.connect(self._on_freq_knob)
        self.freq_knob.interactionStarted.connect(self.interactionStarted.emit)
        self.freq_knob.interactionEnded.connect(self.interactionEnded.emit)

        f_minmax = QHBoxLayout()
        f_minmax.setSpacing(4)
        self.freq_min_lbl = QLabel(r_min_s)
        self.freq_min_lbl.setObjectName("RangeHint")
        self.freq_max_lbl = QLabel(r_max_s)
        self.freq_max_lbl.setObjectName("RangeHint")
        f_minmax.addWidget(self.freq_min_lbl)
        f_minmax.addStretch()
        f_minmax.addWidget(self.freq_max_lbl)

        f_right.addWidget(self.freq_knob, 0, Qt.AlignmentFlag.AlignCenter)
        f_right.addLayout(f_minmax)

        f_top.addLayout(f_left)
        f_top.addStretch()
        f_top.addLayout(f_right)
        freq_lay.addLayout(f_top)

        self.seg_row = QHBoxLayout()
        self.seg_row.setSpacing(2)
        self.slope_btns: dict[int, QPushButton] = {}
        self.range_btns: dict[str, QPushButton] = {}

        for s_val in (6, 12, 18, 24):
            btn = QPushButton(f"{s_val}dB")
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, sv=s_val: self._on_slope_clicked(sv))
            self.seg_row.addWidget(btn)
            self.slope_btns[s_val] = btn

        for r_key in ("L", "ML", "MH", "H"):
            btn = QPushButton(r_key)
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, rk=r_key: self._on_range_clicked(rk))
            self.seg_row.addWidget(btn)
            self.range_btns[r_key] = btn

        freq_lay.addLayout(self.seg_row)
        root.addWidget(freq_sec)

        # 3. Gain 区块
        self.gain_sec = QFrame()
        self.gain_sec.setObjectName("SubSection")
        g_lay = QHBoxLayout(self.gain_sec)
        g_lay.setContentsMargins(8, 6, 8, 6)

        g_left = QVBoxLayout()
        g_left.setSpacing(4)
        g_lbl = QLabel(I18N.tr("audio.fx.gain", "Gain"))
        g_lbl.setObjectName("ParamTitle")
        self.gain_pill = EditableValuePill("0.0", self._accent, width=54)
        self.gain_pill.valueEdited.connect(self._on_gain_text)
        g_left.addWidget(g_lbl)
        g_left.addWidget(self.gain_pill)
        g_left.addStretch()

        g_right = QVBoxLayout()
        g_right.setSpacing(1)
        self.gain_knob = ProRotaryKnob(-20.0, 20.0, self._data["gain"], 0.0,
                                       self._accent, mode="bipolar", size=36)
        self.gain_knob.valueChanged.connect(self._on_gain_knob)
        self.gain_knob.interactionStarted.connect(self.interactionStarted.emit)
        self.gain_knob.interactionEnded.connect(self.interactionEnded.emit)

        g_hints = QHBoxLayout()
        g_hints.setSpacing(4)
        for t in ("-20", "dB", "+20"):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            g_hints.addWidget(lbl)

        g_right.addWidget(self.gain_knob, 0, Qt.AlignmentFlag.AlignCenter)
        g_right.addLayout(g_hints)

        g_lay.addLayout(g_left)
        g_lay.addStretch()
        g_lay.addLayout(g_right)
        root.addWidget(self.gain_sec)

        # 4. Q Factor 区块
        self.q_sec = QFrame()
        self.q_sec.setObjectName("SubSection")
        q_lay = QHBoxLayout(self.q_sec)
        q_lay.setContentsMargins(8, 6, 8, 7)

        q_left = QVBoxLayout()
        q_left.setSpacing(4)
        q_lbl = QLabel(I18N.tr("audio.fx.q_factor", "Q Factor"))
        q_lbl.setObjectName("ParamTitle")
        self.q_pill = EditableValuePill("1.1", self._accent, width=54)
        self.q_pill.valueEdited.connect(self._on_q_text)
        q_left.addWidget(q_lbl)
        q_left.addWidget(self.q_pill)
        q_left.addStretch()

        q_right = QVBoxLayout()
        q_right.setSpacing(1)
        self.q_knob = ProRotaryKnob(0.3, 10.3, self._data["q"], 1.1, self._accent,
                                    mode="linear", size=36)
        self.q_knob.valueChanged.connect(self._on_q_knob)
        self.q_knob.interactionStarted.connect(self.interactionStarted.emit)
        self.q_knob.interactionEnded.connect(self.interactionEnded.emit)

        q_hints = QHBoxLayout()
        q_hints.setSpacing(12)
        for t in ("0.3", "10.3"):
            lbl = QLabel(t)
            lbl.setObjectName("RangeHint")
            q_hints.addWidget(lbl)

        q_right.addWidget(self.q_knob, 0, Qt.AlignmentFlag.AlignCenter)
        q_right.addLayout(q_hints)

        q_lay.addLayout(q_left)
        q_lay.addStretch()
        q_lay.addLayout(q_right)
        root.addWidget(self.q_sec)

        self.bottom_spacer = QFrame()
        self.bottom_spacer.setObjectName("SubSection")
        self.bottom_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.bottom_spacer)

        self.syncFromData(self._data)

    def setSelected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_card_style()

    def _apply_card_style(self) -> None:
        en = bool(self._data["enabled"])
        sel = getattr(self, "_selected", False)
        top_col = self._accent if en else "#343844"
        border_col = self._accent if (sel and en) else ("#484D5E" if sel else "#2A2D37")
        bg_col = "#21242C" if sel else ("#1D1F25" if en else "#181A20")

        self.setStyleSheet(f"""
            QFrame#BandCard {{
                background-color: {bg_col};
                border: 1px solid {border_col};
                border-top: 2px solid {top_col};
                border-radius: 5px;
            }}
            QFrame#SubSection {{
                background-color: transparent;
                border-top: 1px solid #262932;
            }}
            QLabel#ParamTitle {{
                color: {"#888D9C" if en else "#585C68"};
                font-size: 10px;
                font-weight: 600;
            }}
            QLabel#RangeHint {{ color: #666B78; font-size: 9px; }}
            QPushButton {{
                background-color: #252830;
                color: #828796;
                border: 1px solid #1A1C22;
                border-radius: 2.5px;
                font-size: 10px;
                font-weight: 600;
                padding: 0px;
            }}
            QPushButton:hover {{ background-color: #2E323D; color: #D8DCE6; }}
            QPushButton:checked {{
                background-color: #111318;
                color: {self._accent if en else "#787D8C"};
                border: 1px solid {self._accent if en else "#424654"};
            }}
        """)

    def syncFromData(self, data: dict) -> None:
        self._updating = True
        self._data = copy.deepcopy(data)
        en = bool(self._data["enabled"])
        ftype = self._data["filter_type"]
        is_cut = ftype in ("HPF", "LPF")

        self.band_btn.setChecked(en)
        self.solo_btn.setChecked(bool(self._data.get("solo", False)))

        if en:
            self.band_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #13151B;
                    color: {self._accent};
                    border: 1px solid {self._accent};
                    border-radius: 4px;
                    font-size: 11px;
                    font-weight: 700;
                }}
            """)
        else:
            self.band_btn.setStyleSheet("""
                QPushButton {
                    background-color: #252830;
                    color: #7E8392;
                    border: 1px solid #323642;
                    border-radius: 4px;
                    font-size: 11px;
                    font-weight: 600;
                }
            """)

        self.shape_selector.setCurrentShape(ftype)
        self.shape_selector.setBandActive(en)

        r_min, r_max, r_min_s, r_max_s = RANGE_PRESETS[self._data["range_key"]]
        self.freq_min_lbl.setText(r_min_s)
        self.freq_max_lbl.setText(r_max_s)
        self.freq_knob._min = r_min
        self.freq_knob._max = r_max
        self.freq_knob.setValue(self._data["freq"])
        self.freq_knob.setModuleActive(en)
        self.freq_pill.setText(format_freq_hz(self._data["freq"]))
        self.freq_pill.setModuleActive(en)

        for sv, btn in self.slope_btns.items():
            btn.setVisible(is_cut)
            btn.setChecked(sv == self._data["slope"])
        for rk, btn in self.range_btns.items():
            btn.setVisible(not is_cut)
            btn.setChecked(rk == self._data["range_key"])

        self.gain_sec.setVisible(not is_cut)
        self.q_sec.setVisible(not is_cut)
        self.bottom_spacer.setVisible(is_cut)

        if not is_cut:
            g = self._data["gain"]
            self.gain_knob.setValue(g)
            self.gain_knob.setModuleActive(en)
            self.gain_pill.setText(f"{g:+.1f}" if abs(g) >= 0.05 else "0.0")
            self.gain_pill.setModuleActive(en)

            q = self._data["q"]
            self.q_knob.setValue(q)
            self.q_knob.setModuleActive(en)
            self.q_pill.setText(f"{q:.1f}")
            self.q_pill.setModuleActive(en)

        self._apply_card_style()
        self._updating = False

    def _emit_change(self) -> None:
        if not self._updating:
            self.syncFromData(self._data)
            self.bandChanged.emit(self._data["index"], copy.deepcopy(self._data))

    def _on_toggle_band(self, checked: bool) -> None:
        self._data["enabled"] = checked
        self._emit_change()

    def _on_toggle_solo(self, checked: bool) -> None:
        self._data["solo"] = checked
        if not self._updating:
            self.soloToggled.emit(self._data["index"], checked)
            self.bandChanged.emit(self._data["index"], copy.deepcopy(self._data))

    def _on_shape_changed(self, shape: str) -> None:
        self._data["filter_type"] = shape
        self._emit_change()

    def _on_freq_knob(self, val: float) -> None:
        self._data["freq"] = round(val, 1)
        self._emit_change()

    def _on_freq_text(self, text: str) -> None:
        r_min, r_max, _, _ = RANGE_PRESETS[self._data["range_key"]]
        val = parse_freq_hz(text, self._data["freq"])
        self._data["freq"] = max(r_min, min(r_max, val))
        self._emit_change()

    def _on_slope_clicked(self, slope: int) -> None:
        self._data["slope"] = slope
        self._emit_change()

    def _on_range_clicked(self, r_key: str) -> None:
        self._data["range_key"] = r_key
        r_min, r_max, _, _ = RANGE_PRESETS[r_key]
        self._data["freq"] = max(r_min, min(r_max, self._data["freq"]))
        self._emit_change()

    def _on_gain_knob(self, val: float) -> None:
        self._data["gain"] = round(val, 1)
        self._emit_change()

    def _on_gain_text(self, text: str) -> None:
        val = parse_float_clean(text, self._data["gain"])
        self._data["gain"] = max(-20.0, min(20.0, round(val, 1)))
        self._emit_change()

    def _on_q_knob(self, val: float) -> None:
        self._data["q"] = round(val, 1)
        self._emit_change()

    def _on_q_text(self, text: str) -> None:
        try:
            val = float(text.strip())
            self._data["q"] = max(0.3, min(10.3, round(val, 1)))
            self._emit_change()
        except ValueError:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# 主对话框
# ═════════════════════════════════════════════════════════════════════════════

class ParametricEQDialog(QDialog):
    """参数化 EQ 对话框（6 频段全参量 + 频谱 + Voicing + Master 输出）。"""

    # track_id, StripEQParams（保持 strip_eq 信号名，宿主接线零改动）
    strip_eq_preview = pyqtSignal(str, object)    # 拖动期实时听感
    strip_eq_committed = pyqtSignal(str, object)  # 松手/预设/开关，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str = "",
        initial_params: Optional[StripEQParams] = None,
        parent: Optional[QWidget] = None,
        sample_rate: float = 48000.0,
    ) -> None:
        super().__init__(parent)
        # MX-09: 采样率由引擎下发（构造注入 + update_sample_rate 运行更新）
        self._sample_rate = float(sample_rate)
        self._track_id = track_id
        self._instance_id = instance_id
        self._current: StripEQParams = initial_params or StripEQParams.default()
        self._dragging = {"active": False}
        self._updating = False

        self.setWindowTitle(
            I18N.tr("audio.fx.parametric_eq", "Parametric EQ") + f" - {track_name or track_id}")
        self.setModal(False)
        apply_float_panel(self)
        self.resize(980, 690)
        self.setMinimumSize(880, 610)

        # A/B 对比槽（对话框内存行为）
        self._active_bank = "A"
        self._banks = {
            "A": self._current,
            "B": StripEQParams(bands=tuple(default_eq_bands_flat())),
        }
        # 预设（内置 + 用户库；ST-01 跨会话持久化）
        self._presets: list[dict[str, Any]] = copy.deepcopy(PARAMETRIC_EQ_PRESETS)
        self._effect_type = "strip_eq"
        try:
            from src.features.edit.core.effect_preset import load_user_presets
            self._presets.extend(load_user_presets(self._effect_type))
        except Exception as exc:  # noqa: BLE001 — 预设库不可读不阻断对话框
            logger.warning("strip_eq dialog: user presets unavailable: %r", exc)

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

        self.power_switch = PowerToggleSwitch(True, BAND_PALETTE[3])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"PARAMETRIC EQ  ·  {track_name}")
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
        self.preset_combo.setFixedWidth(185)
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

        tb_lay.addSpacing(10)

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

        v_lbl = QLabel(I18N.tr("audio.fx.voicing", "Voicing"))
        v_lbl.setStyleSheet("color: #8E94A4; font-size: 11px; font-weight: 600;")
        tb_lay.addWidget(v_lbl)

        self.voicing_combo = ChevronComboBox()
        self.voicing_combo.setFixedWidth(84)
        self.voicing_combo.addItems(["Clean", "British", "Warm", "Punch"])
        self.voicing_combo.activated.connect(self._on_voicing_changed)
        tb_lay.addWidget(self.voicing_combo)

        reset_btn = QPushButton(I18N.tr("audio.fx.reset", "Reset"))
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedHeight(23)
        reset_btn.setMinimumWidth(max(48, reset_btn.sizeHint().width() + 6))
        reset_btn.clicked.connect(self.reset_eq)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # 2. 中部：频响曲线 + Master 输出
        graph_row = QHBoxLayout()
        graph_row.setContentsMargins(8, 10, 12, 6)
        graph_row.setSpacing(6)

        self.eq_graph = ProEQGraphWidget()
        self.eq_graph._sample_rate = self._sample_rate
        self.eq_graph.nodeDragged.connect(self._on_graph_node_dragged)
        self.eq_graph.nodeDragEnded.connect(self._on_drag_released)
        self.eq_graph.nodeQScrolled.connect(self._on_graph_node_q_scrolled)
        self.eq_graph.nodeShapeRequested.connect(self._on_graph_node_shape)
        self.eq_graph.nodeClicked.connect(self._select_band_card)
        self.eq_graph.nodeToggleRequested.connect(self._on_graph_node_toggled)
        self.eq_graph.bandSpawnRequested.connect(self._on_graph_band_spawn)
        graph_row.addWidget(self.eq_graph, 1)

        out_col = QVBoxLayout()
        out_col.setSpacing(2)
        out_lbl = QLabel("OUT")
        out_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        out_lbl.setStyleSheet(
            "color: #8E94A4; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;")
        out_col.addWidget(out_lbl)

        self.master_strip = MasterOutputStrip()
        self.master_strip.gainChanged.connect(self._on_master_gain_changed)
        self.master_strip.interactionStarted.connect(
            lambda: self._dragging.update(active=True))
        self.master_strip.interactionEnded.connect(self._on_drag_released)
        out_col.addWidget(self.master_strip, 1)
        graph_row.addLayout(out_col)

        main_lay.addLayout(graph_row, 5)

        # 3. 底部 6 频段卡片
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(14, 4, 14, 14)
        cards_row.setSpacing(7)

        self.band_cards: list[ProBandCard] = []
        for b in self._current.bands:
            card = ProBandCard(dict(b))
            card.bandChanged.connect(self._on_band_card_changed)
            card.soloToggled.connect(self._on_band_solo_toggled)
            card.interactionStarted.connect(lambda: self._dragging.update(active=True))
            card.interactionEnded.connect(self._on_drag_released)
            cards_row.addWidget(card, 1)
            self.band_cards.append(card)

        main_lay.addLayout(cards_row, 4)

    # ── 状态与联动 ───────────────────────────────────────────

    def _st(self) -> StripEQParams:
        return self._banks[self._active_bank]

    def _update_ab_styles(self) -> None:
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

    def _refresh_all_views(self) -> None:
        st = self._st()
        self._updating = True
        self._update_ab_styles()
        self.power_switch.setChecked(st.enabled)
        idx = self.voicing_combo.findText(st.voicing)
        if idx >= 0:
            self.voicing_combo.setCurrentIndex(idx)
        self.master_strip.setGain(st.master_gain)

        bands = [dict(b) for b in st.bands]
        for i, card in enumerate(self.band_cards):
            if i < len(bands):
                card.syncFromData(bands[i])
        self.eq_graph.setState(bands, st.enabled, st.voicing, st.master_gain)
        self._updating = False

    def _emit(self, commit: Optional[bool] = None) -> None:
        """按当前拖拽状态发射 preview / committed。"""
        if self._updating:
            return
        use_commit = (not self._dragging["active"]) if commit is None else commit
        if use_commit:
            self.strip_eq_committed.emit(self._track_id, self._st())
        else:
            self.strip_eq_preview.emit(self._track_id, self._st())

    def _replace_state(self, **kwargs: Any) -> None:
        st = self._st()
        merged = {
            "enabled": st.enabled, "voicing": st.voicing,
            "master_gain": st.master_gain, "bands": st.bands,
        }
        merged.update(kwargs)
        self._banks[self._active_bank] = StripEQParams(**merged)

    def _replace_bands(self, bands: list[dict]) -> None:
        self._replace_state(bands=tuple(bands))

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
        self._replace_state(enabled=enabled)
        st = self._st()
        self.eq_graph.setState([dict(b) for b in st.bands], enabled,
                               st.voicing, st.master_gain)
        self._emit(commit=True)

    def _on_voicing_changed(self, index: int) -> None:
        voicing = self.voicing_combo.itemText(index)
        self._replace_state(voicing=voicing)
        st = self._st()
        self.eq_graph.setState([dict(b) for b in st.bands], st.enabled,
                               voicing, st.master_gain)
        self._emit(commit=True)

    def _on_master_gain_changed(self, gain: float) -> None:
        self._replace_state(master_gain=gain)
        st = self._st()
        self.eq_graph.setState([dict(b) for b in st.bands], st.enabled,
                               st.voicing, gain)
        self._emit()

    def _on_band_card_changed(self, band_index: int, band_data: dict) -> None:
        st = self._st()
        bands = [dict(b) for b in st.bands]
        bands[band_index - 1] = band_data
        self._replace_bands(bands)
        self._select_band_card(band_index - 1)
        self.eq_graph.setState(bands, st.enabled, st.voicing, st.master_gain)
        self._emit()

    def _on_band_solo_toggled(self, band_index: int, is_solo: bool) -> None:
        # 互斥 Solo：开启某频段独奏时关闭其他频段 Solo
        st = self._st()
        bands = [dict(b) for b in st.bands]
        for i, b in enumerate(bands):
            if b["index"] != band_index and b.get("solo", False):
                bands[i]["solo"] = False
                self.band_cards[i].syncFromData(bands[i])
        self._replace_bands(bands)
        self.eq_graph.setState(bands, st.enabled, st.voicing, st.master_gain)
        self._emit()

    def _select_band_card(self, idx: int) -> None:
        for i, card in enumerate(self.band_cards):
            card.setSelected(i == idx)

    def _on_graph_node_toggled(self, idx: int) -> None:
        st = self._st()
        bands = [dict(b) for b in st.bands]
        bands[idx]["enabled"] = not bands[idx]["enabled"]
        self._replace_bands(bands)
        self.band_cards[idx].syncFromData(bands[idx])
        self._select_band_card(idx)
        self.eq_graph.setState(bands, st.enabled, st.voicing, st.master_gain)
        self._emit(commit=True)

    def _on_graph_band_spawn(self, freq: float, gain: float) -> None:
        st = self._st()
        bands = [dict(b) for b in st.bands]
        disabled = [i for i, b in enumerate(bands) if not b["enabled"] and i in (1, 2, 3, 4)]
        if not disabled:
            disabled = [i for i, b in enumerate(bands) if not b["enabled"]]
        if not disabled:
            return

        target_idx = min(disabled, key=lambda i: abs(math.log10(bands[i]["freq"] / max(20.0, freq))))
        b = bands[target_idx]
        b["enabled"] = True
        b["freq"] = freq
        b["range_key"] = auto_range_key(freq)
        if b["filter_type"] not in ("HPF", "LPF"):
            b["gain"] = gain
        self._replace_bands(bands)
        self.band_cards[target_idx].syncFromData(b)
        self._select_band_card(target_idx)
        self.eq_graph.setState(bands, st.enabled, st.voicing, st.master_gain)
        self._emit(commit=True)

    def _on_graph_node_dragged(self, idx: int, freq: float, gain: float) -> None:
        st = self._st()
        bands = [dict(b) for b in st.bands]
        bands[idx]["freq"] = round(freq, 1)
        bands[idx]["gain"] = round(gain, 1)
        bands[idx]["range_key"] = auto_range_key(bands[idx]["freq"])
        self._replace_bands(bands)
        self.band_cards[idx].syncFromData(bands[idx])
        self._select_band_card(idx)
        self.eq_graph.setState(bands, st.enabled, st.voicing, st.master_gain)
        self._emit()

    def _on_graph_node_q_scrolled(self, idx: int, new_q: float) -> None:
        st = self._st()
        bands = [dict(b) for b in st.bands]
        bands[idx]["q"] = new_q
        self._replace_bands(bands)
        self.band_cards[idx].syncFromData(bands[idx])
        self.eq_graph.setState(bands, st.enabled, st.voicing, st.master_gain)
        self._emit(commit=True)

    def _on_graph_node_shape(self, idx: int, new_shape: str) -> None:
        st = self._st()
        bands = [dict(b) for b in st.bands]
        bands[idx]["filter_type"] = new_shape
        self._replace_bands(bands)
        self.band_cards[idx].syncFromData(bands[idx])
        self.eq_graph.setState(bands, st.enabled, st.voicing, st.master_gain)
        self._emit(commit=True)

    def _on_drag_released(self) -> None:
        if self._updating:
            return
        self._dragging["active"] = False
        self.strip_eq_committed.emit(self._track_id, self._st())

    # ── 预设 ─────────────────────────────────────────────────

    def _on_preset_selected(self, combo_idx: int) -> None:
        if 0 <= combo_idx < len(self._presets):
            params_data = dict(self._presets[combo_idx]["params"])
            params_data["enabled"] = self._st().enabled  # 保留当前开关状态
            self._banks[self._active_bank] = StripEQParams.from_dict(params_data)
            self._refresh_all_views()
            # 保留选中项显示（旧逻辑置 -1 会让预设名消失，误以为未选中）
            self._emit(commit=True)

    def _step_preset(self, step: int) -> None:
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex(
                (max(self.preset_combo.currentIndex(), 0) + step) % cnt)
            self._on_preset_selected(self.preset_combo.currentIndex())

    def _save_new_preset(self) -> None:
        name, ok = QInputDialog.getText(
            self, I18N.tr("audio.fx.save_preset_title", "保存 EQ 预设"),
            I18N.tr("audio.fx.preset_name_prompt", "输入预设名称:"))
        if ok and name.strip():
            clean = name.strip()
            data = self._st().to_dict()
            self._presets.append({"name": clean, "params": data})
            self.preset_combo.addItem(clean)
            # ST-01: 原子落盘到用户库（关窗口/重启/新工程均可加载）
            try:
                from src.features.edit.core.effect_preset import save_user_preset
                save_user_preset(self._effect_type, clean, data)
            except Exception as exc:  # noqa: BLE001 — 落盘失败不阻断内存视图
                logger.warning("strip_eq dialog: persist preset failed: %r", exc)

    # ── 公开 API ─────────────────────────────────────────────

    def reset_eq(self) -> None:
        """重置为平直响应（保留当前开关状态）。"""
        was_enabled = self._st().enabled
        fresh = StripEQParams.default()
        self._banks[self._active_bank] = StripEQParams(
            enabled=was_enabled, voicing=fresh.voicing,
            master_gain=fresh.master_gain, bands=fresh.bands)
        self._refresh_all_views()
        self._emit(commit=True)

    def get_params(self) -> StripEQParams:
        return copy.deepcopy(self._st())

    def set_current_params(self, params: StripEQParams) -> None:
        """外部静默回填（timeline 变化后同步显示，不回发信号）。"""
        self._banks[self._active_bank] = copy.deepcopy(params)
        self._refresh_all_views()

    # 遥测入口（TrackMixerWidget 转发引擎数据）
    def update_spectrum(self, freqs_hz: list, post_db: list,
                        peak_db: Optional[list] = None) -> None:
        self.eq_graph.update_spectrum(freqs_hz, post_db, peak_db)

    def reset_peak_hold(self) -> None:
        """MX-01/EQ-03：清空频谱 peak hold（停止/seek 清表事件）。"""
        self.eq_graph.reset_peak_hold()

    def update_output_meter(self, level_db: float) -> None:
        self.master_strip.setMeterLevel(level_db)

    def update_sample_rate(self, sample_rate: float) -> None:
        """MX-09: 引擎采样率下发（曲线求值与频谱轴同一真源）。"""
        self._sample_rate = float(sample_rate)
        self.eq_graph._sample_rate = float(sample_rate)
        self.eq_graph.update()


if __name__ == "__main__":  # pragma: no cover
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    win = ParametricEQDialog("Audio 1", "Audio 1", "", None)
    win.show()
    sys.exit(app.exec())
