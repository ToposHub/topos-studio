"""
Graphic EQ Dialog — 多频段图形均衡器编辑对话框（3/7/10/15 band）。

点击 mixer 内嵌效果器列表中的 EQ 条目时弹出。包含：
- 频段模式选择（3 / 7 / 10 / 15 band）
- 每频段垂直推子（-12..+12 dB）+ 频率标签 + 实时增益数值
- Low-Cut（高通）开关 + 频率
- High-Cut（低通）开关 + 频率
- 整体增益（-12..+12 dB）
- 实时频响曲线预览（对数频率轴 20Hz-20kHz，增益轴 -15..+15 dB）
- 预设下拉（按 mode 分组：平坦/低音增强/人声/电子/摇滚/爵士/原声 等）
- 三段式节流：拖动期 emit strip_eq_preview（实时听感），松手 emit strip_eq_committed（命令）

信号链：
  对话框 → TrackMixerWidget._on_eq_edited_from_list → track_effects_committed
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QPen, QColor, QPainterPath, QPalette, QFont,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QSlider,
    QCheckBox, QPushButton, QSizePolicy, QWidget,
    QGroupBox, QFrame,
)

from src.features.edit.core.timeline_v2 import StripEQParams
from src.features.edit.core.audio_dsp import (
    compute_eq_response, EQ_BAND_CONFIGS, get_band_count,
)
from src.shared.ui.audio_mixer.eq_presets import get_presets_for_mode
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.toggle_switch import ToggleSwitch


# 频响曲线范围
FREQ_MIN_HZ = 20.0
FREQ_MAX_HZ = 20000.0
GAIN_MIN_DB = -15.0
GAIN_MAX_DB = 15.0

# AUD-37819: 频响曲线的采样频率轴是固定常量，预计算一次，
# 避免 paintEvent 每次重绘都重建 logspace 数组。
_RESP_FREQS = np.logspace(np.log10(FREQ_MIN_HZ), np.log10(FREQ_MAX_HZ), 256)

# 推子范围（dB）与精度（0.1 dB）
SLIDER_GAIN_MIN = -12.0
SLIDER_GAIN_MAX = 12.0
SLIDER_STEP = 0.1
SLIDER_RANGE = int(round((SLIDER_GAIN_MAX - SLIDER_GAIN_MIN) / SLIDER_STEP))  # 240

# 频段模式
EQ_MODES = ("3-band", "7-band", "10-band", "15-band")


class FrequencyResponseWidget(QWidget):
    """频响曲线预览控件（Bode 图）。

    对数频率轴（20Hz-20kHz），增益轴 -15..+15 dB。
    实时绘制多频段 EQ 的总频响曲线（含 Low/High-Cut + 频段链 + 整体增益）。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._params: Optional[StripEQParams] = None
        self._sample_rate: float = 48000.0
        # AUD-37819: 频响曲线按 (EQ 参数, 采样率) 缓存，参数未变时不重算 scipy 频响
        self._resp_cache_key: Optional[tuple] = None
        self._resp_cache_vals: Optional[np.ndarray] = None
        self.setMinimumSize(400, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_params(self, params: Optional[StripEQParams], sample_rate: float = 48000.0) -> None:
        self._params = params
        self._sample_rate = float(sample_rate)
        # AUD-37819: 参数变化即作废缓存
        self._resp_cache_key = None
        self._resp_cache_vals = None
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        margin_l, margin_r, margin_t, margin_b = 40, 10, 10, 24
        plot_w = w - margin_l - margin_r
        plot_h = h - margin_t - margin_b

        palette = self.palette()
        bg = palette.color(QPalette.ColorRole.Window)
        painter.fillRect(self.rect(), bg)

        # 网格
        grid_pen = QPen(QColor(120, 120, 120, 60), 1)
        painter.setPen(grid_pen)

        # 频率刻度（对数）
        freq_ticks = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
        for f in freq_ticks:
            x = margin_l + self._freq_to_x(f, plot_w)
            painter.drawLine(int(x), margin_t, int(x), margin_t + plot_h)
            label = f"{f//1000}k" if f >= 1000 else str(f)
            painter.setPen(palette.color(QPalette.ColorRole.WindowText))
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(int(x) - 12, margin_t + plot_h + 14, label)
            painter.setPen(grid_pen)

        # 增益刻度
        gain_ticks = [-12, -6, 0, 6, 12]
        for g in gain_ticks:
            y = margin_t + self._gain_to_y(g, plot_h)
            pen = QPen(QColor(120, 120, 120, 80 if g != 0 else 200), 1 if g != 0 else 2)
            painter.setPen(pen)
            painter.drawLine(margin_l, int(y), margin_l + plot_w, int(y))
            painter.setPen(palette.color(QPalette.ColorRole.WindowText))
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(4, int(y) + 4, f"{g:+d}")

        # 频响曲线
        if self._params is not None and self._params.enabled:
            # AUD-37819: 复用固定采样轴；仅当 (EQ 参数, 采样率) 变化时重算
            # compute_eq_response（内部走 scipy，重绘制下无谓重复）。
            freqs = _RESP_FREQS
            cache_key = (
                self._params.mode,
                tuple(self._params.bands),
                self._params.gain,
                self._params.low_cut,
                self._params.high_cut,
                self._params.low_cut_enable,
                self._params.high_cut_enable,
                self._sample_rate,
            )
            if cache_key != self._resp_cache_key or self._resp_cache_vals is None:
                self._resp_cache_vals = compute_eq_response(
                    self._params, self._sample_rate, freqs)
                self._resp_cache_key = cache_key
            resp = self._resp_cache_vals

            path = QPainterPath()
            for i, (f, r) in enumerate(zip(freqs, resp)):
                x = margin_l + self._freq_to_x(f, plot_w)
                y = margin_t + self._gain_to_y(r, plot_h)
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

            curve_pen = QPen(QColor(80, 160, 240, 220), 2)
            painter.setPen(curve_pen)
            painter.drawPath(path)

        # 边框
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Dark), 1))
        painter.drawRect(margin_l, margin_t, plot_w, plot_h)

    def _freq_to_x(self, freq_hz: float, plot_w: int) -> float:
        """对数频率 → x 像素。"""
        log_min = np.log10(FREQ_MIN_HZ)
        log_max = np.log10(FREQ_MAX_HZ)
        log_f = np.log10(max(FREQ_MIN_HZ, min(FREQ_MAX_HZ, freq_hz)))
        return (log_f - log_min) / (log_max - log_min) * plot_w

    def _gain_to_y(self, gain_db: float, plot_h: int) -> float:
        """增益 dB → y 像素（上正下负）。"""
        clamped = max(GAIN_MIN_DB, min(GAIN_MAX_DB, gain_db))
        return (0.5 - clamped / (2 * GAIN_MAX_DB)) * plot_h


class BandFaderWidget(QWidget):
    """单个频段的垂直推子控件（slider + 频率标签 + 增益数值）。

    拖动期发出 drag_state 信号（active/inactive），由父对话框统一处理节流。
    """

    def __init__(
        self,
        band_cfg: dict,
        initial_gain: float,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._band_cfg = band_cfg
        self._value_label: Optional[QLabel] = None
        self._slider: Optional[QSlider] = None
        self._on_changed = None  # callback(gain_db)
        self._on_drag_pressed = None
        self._on_drag_released = None
        self._init_ui(initial_gain)

    def _init_ui(self, initial_gain: float) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(2)

        # 增益数值（顶部）
        self._value_label = QLabel(f"{initial_gain:+.1f}")
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._value_label.setStyleSheet("font-size: 10px; color: #b0b0b0; min-width: 32px;")
        self._value_label.setFixedHeight(14)
        lay.addWidget(self._value_label)

        # 垂直推子（int 0..SLIDER_RANGE，0.1 dB 步进）
        self._slider = QSlider(Qt.Orientation.Vertical)
        self._slider.setMinimum(0)
        self._slider.setMaximum(SLIDER_RANGE)
        self._slider.setValue(self._gain_to_slider(initial_gain))
        self._slider.setTickPosition(QSlider.TickPosition.TicksBothSides)
        self._slider.setTickInterval(int(SLIDER_RANGE / 8))
        self._slider.setFixedHeight(140)
        self._slider.setMinimumWidth(28)
        lay.addWidget(self._slider, 0, Qt.AlignmentFlag.AlignCenter)

        # 频率标签（底部）
        freq_lbl = QLabel(self._band_cfg["name"])
        freq_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        freq_lbl.setStyleSheet("font-size: 10px; color: #888890;")
        freq_lbl.setFixedHeight(14)
        lay.addWidget(freq_lbl)

    def _gain_to_slider(self, gain_db: float) -> int:
        return int(round((gain_db - SLIDER_GAIN_MIN) / SLIDER_STEP))

    def _slider_to_gain(self, sv: int) -> float:
        return SLIDER_GAIN_MIN + sv * SLIDER_STEP

    def set_callbacks(self, on_changed, on_drag_pressed, on_drag_released) -> None:
        """注册回调：on_changed(gain_db), on_drag_pressed(), on_drag_released()。"""
        self._on_changed = on_changed
        self._on_drag_pressed = on_drag_pressed
        self._on_drag_released = on_drag_released

        def _slider_changed(sv: int) -> None:
            gain = self._slider_to_gain(sv)
            self._value_label.setText(f"{gain:+.1f}")
            if self._on_changed is not None:
                self._on_changed(gain)

        self._slider.valueChanged.connect(_slider_changed)
        self._slider.sliderPressed.connect(lambda: self._on_drag_pressed() if self._on_drag_pressed else None)
        self._slider.sliderReleased.connect(lambda: self._on_drag_released() if self._on_drag_released else None)

    def set_gain(self, gain_db: float) -> None:
        """程序化设置增益（blockSignals 防回环）。"""
        self._slider.blockSignals(True)
        self._slider.setValue(self._gain_to_slider(gain_db))
        self._slider.blockSignals(False)
        self._value_label.setText(f"{gain_db:+.1f}")


class StripEQDialog(QDialog):
    """多频段 Graphic EQ 编辑对话框。

    信号：
        strip_eq_preview(str, StripEQParams|None) — 拖动期实时预览（不进命令栈）
        strip_eq_committed(str, StripEQParams|None) — 松手/关闭时提交（进命令栈）
    """

    strip_eq_preview = pyqtSignal(str, object)   # track_id, StripEQParams|None
    strip_eq_committed = pyqtSignal(str, object)  # track_id, StripEQParams|None

    def __init__(
        self,
        track_id: str,
        track_name: str,
        initial_params: Optional[StripEQParams] = None,
        sample_rate: float = 48000.0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._track_name = track_name
        self._sample_rate = float(sample_rate)
        # 当前编辑中的参数
        self._current: StripEQParams = initial_params if initial_params is not None else StripEQParams.default()
        # 拖动状态（三段式节流）
        self._dragging = False
        # 频段推子控件列表（动态：随 mode 重建）
        self._band_faders: list[BandFaderWidget] = []
        # 控件引用
        self._response_widget: Optional[FrequencyResponseWidget] = None
        self._mode_combo: Optional[ChevronComboBox] = None
        self._preset_combo: Optional[ChevronComboBox] = None
        self._enable_check: Optional[ToggleSwitch] = None
        self._gain_spin: Optional[DragValue] = None
        self._lowcut_check: Optional[QCheckBox] = None
        self._lowcut_spin: Optional[DragValue] = None
        self._highcut_check: Optional[QCheckBox] = None
        self._highcut_spin: Optional[DragValue] = None
        self._bands_container: Optional[QWidget] = None
        self._bands_layout: Optional[QHBoxLayout] = None

        self.setWindowTitle(I18N.tr("audio.mixer.strip_eq.title", "Graphic EQ — {name}").format(name=track_name))
        self.setModal(False)
        self.setMinimumSize(640, 480)

        self._init_ui()
        self._load_params_to_ui()

    # ────────────────────────── UI 构建 ──────────────────────────

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ── 顶部：总开关 + 模式选择 + 预设 ──
        top_layout = QHBoxLayout()
        self._enable_check = ToggleSwitch(checked=self._current.enabled)
        self._enable_check.toggled.connect(self._on_enabled_toggled)
        top_layout.addWidget(self._enable_check)
        enable_lbl = QLabel(I18N.tr("audio.mixer.strip_eq.enable", "EQ Enabled"))
        enable_lbl.setStyleSheet("color: #bbbbbb; font-size: 11px;")
        top_layout.addWidget(enable_lbl)

        top_layout.addWidget(QLabel(I18N.tr("audio.mixer.strip_eq.mode", "Bands")))
        self._mode_combo = ChevronComboBox()
        for m in EQ_MODES:
            self._mode_combo.addItem(m.replace("-band", ""), m)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        top_layout.addWidget(self._mode_combo)

        top_layout.addWidget(QLabel(I18N.tr("audio.mixer.strip_eq.preset", "Preset")))
        self._preset_combo = ChevronComboBox()
        self._preset_combo.view().setMinimumWidth(200)  # 预设名较长，弹出列表加宽
        # 用 activated 而非 currentIndexChanged：仅响应用户从下拉选择，
        # 不受 _on_preset_selected 末尾 setCurrentIndex(0) 复位的程序性设置干扰
        self._preset_combo.activated.connect(self._on_preset_selected)
        top_layout.addWidget(self._preset_combo, 1)

        layout.addLayout(top_layout)

        # AM-01: 后端不可用降级横幅——scipy 缺失时 EQ 静默旁路，
        # 必须在对话框显式告知，否则用户调参听不到变化。
        self._backend_warning: Optional[QLabel] = None
        try:
            from src.features.edit.core.audio_capabilities import (
                is_scipy_available_for_dsp,
            )
            if not is_scipy_available_for_dsp():
                warn = QLabel(I18N.tr(
                    "audio.mixer.strip_eq.backend_degraded",
                    "⚠ DSP 后端（SciPy）不可用，EQ 当前为静默旁路，"
                    "调整参数不会改变声音。请安装 scipy 后重启。",
                ))
                warn.setWordWrap(True)
                warn.setStyleSheet(
                    "color: #ffcc66; background-color: #3a2a00;"
                    " border: 1px solid #664400; border-radius: 4px;"
                    " padding: 6px; font-size: 11px;"
                )
                layout.addWidget(warn)
                self._backend_warning = warn
        except Exception:
            pass

        # ── 频响曲线 ──
        self._response_widget = FrequencyResponseWidget()
        self._response_widget.set_params(self._current, self._sample_rate)
        layout.addWidget(self._response_widget, 1)

        # ── 频段推子区（动态）──
        self._bands_container = QFrame()
        self._bands_layout = QHBoxLayout(self._bands_container)
        self._bands_layout.setContentsMargins(4, 4, 4, 4)
        self._bands_layout.setSpacing(2)
        layout.addWidget(self._bands_container)

        # ── 滤波器选项（Low-Cut / High-Cut / 整体增益）──
        filters_group = QGroupBox(I18N.tr("audio.mixer.strip_eq.filters", "Filters & Gain"))
        filters_layout = QGridLayout(filters_group)
        filters_layout.setSpacing(6)

        # Low-Cut
        self._lowcut_check = QCheckBox(I18N.tr("audio.mixer.strip_eq.low_cut", "Low Cut"))
        self._lowcut_check.toggled.connect(self._on_filter_changed)
        filters_layout.addWidget(self._lowcut_check, 0, 0)
        self._lowcut_spin = DragValue(
            "", min_v=20.0, max_v=1000.0, default=100.0,
            decimals=0, suffix=" Hz",
        )
        self._lowcut_spin.setSingleStep(10.0)
        self._lowcut_spin.setFixedValueWidth(90)
        self._lowcut_spin.valueChanged.connect(self._on_filter_changed)
        self._lowcut_spin.interaction_started.connect(self._on_drag_pressed)
        self._lowcut_spin.interaction_ended.connect(self._on_drag_released)
        filters_layout.addWidget(self._lowcut_spin, 0, 1)

        # High-Cut
        self._highcut_check = QCheckBox(I18N.tr("audio.mixer.strip_eq.high_cut", "High Cut"))
        self._highcut_check.toggled.connect(self._on_filter_changed)
        filters_layout.addWidget(self._highcut_check, 0, 2)
        self._highcut_spin = DragValue(
            "", min_v=1000.0, max_v=20000.0, default=8000.0,
            decimals=0, suffix=" Hz",
        )
        self._highcut_spin.setSingleStep(100.0)
        self._highcut_spin.setFixedValueWidth(90)
        self._highcut_spin.valueChanged.connect(self._on_filter_changed)
        self._highcut_spin.interaction_started.connect(self._on_drag_pressed)
        self._highcut_spin.interaction_ended.connect(self._on_drag_released)
        filters_layout.addWidget(self._highcut_spin, 0, 3)

        # 整体增益
        gain_lbl = QLabel(I18N.tr("audio.mixer.strip_eq.gain", "Output Gain"))
        filters_layout.addWidget(gain_lbl, 1, 0)
        self._gain_spin = DragValue(
            "", min_v=-12.0, max_v=12.0, default=0.0,
            decimals=1, suffix=" dB",
        )
        self._gain_spin.setSingleStep(0.5)
        self._gain_spin.setFixedValueWidth(90)
        self._gain_spin.valueChanged.connect(self._on_filter_changed)
        self._gain_spin.interaction_started.connect(self._on_drag_pressed)
        self._gain_spin.interaction_ended.connect(self._on_drag_released)
        filters_layout.addWidget(self._gain_spin, 1, 1)

        layout.addWidget(filters_group)

        # ── 底部按钮 ──
        btn_layout = QHBoxLayout()
        self._reset_btn = QPushButton(I18N.tr("audio.mixer.strip_eq.reset", "Reset to Flat"))
        self._reset_btn.clicked.connect(self._on_reset)
        btn_layout.addWidget(self._reset_btn)
        btn_layout.addStretch()
        self._close_btn = QPushButton(I18N.tr("common.close", "Close"))
        self._close_btn.clicked.connect(self._on_close)
        btn_layout.addWidget(self._close_btn)
        layout.addLayout(btn_layout)

        # 初始化频段推子 + 预设列表
        self._rebuild_band_faders()
        self._refresh_preset_list()

    def _rebuild_band_faders(self) -> None:
        """根据当前 mode 重建频段推子控件。"""
        # 清空旧控件
        while self._bands_layout.count():
            item = self._bands_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._band_faders = []

        band_cfgs = EQ_BAND_CONFIGS.get(self._current.mode, EQ_BAND_CONFIGS["3-band"])
        for i, cfg in enumerate(band_cfgs):
            gain = float(self._current.bands[i]) if i < len(self._current.bands) else 0.0
            fader = BandFaderWidget(cfg, gain)
            idx = i  # 闭包捕获
            fader.set_callbacks(
                on_changed=lambda g, _i=idx: self._on_band_changed(_i, g),
                on_drag_pressed=self._on_drag_pressed,
                on_drag_released=self._on_drag_released,
            )
            self._band_faders.append(fader)
            self._bands_layout.addWidget(fader)
        self._bands_layout.addStretch()

    def _refresh_preset_list(self) -> None:
        """根据当前 mode 刷新预设下拉。"""
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        self._preset_combo.addItem("—", None)
        for p in get_presets_for_mode(self._current.mode):
            self._preset_combo.addItem(p["name"], p["bands"])
        self._preset_combo.blockSignals(False)

    # ────────────────────────── 参数同步 ──────────────────────────

    def _load_params_to_ui(self) -> None:
        """把 self._current 加载到所有控件（blockSignals 防回环）。"""
        p = self._current
        widgets = [
            self._enable_check, self._mode_combo, self._preset_combo,
            self._gain_spin, self._lowcut_check, self._lowcut_spin,
            self._highcut_check, self._highcut_spin,
        ]
        for w in widgets:
            if w is not None:
                w.blockSignals(True)
        try:
            self._enable_check.setChecked(p.enabled)
            mode_idx = EQ_MODES.index(p.mode) if p.mode in EQ_MODES else 0
            self._mode_combo.setCurrentIndex(mode_idx)
            self._preset_combo.setCurrentIndex(0)
            self._gain_spin.setValue(p.gain)
            self._lowcut_check.setChecked(p.low_cut_enable)
            self._lowcut_spin.setValue(p.low_cut)
            self._lowcut_spin.setEnabled(p.low_cut_enable)
            self._highcut_check.setChecked(p.high_cut_enable)
            self._highcut_spin.setValue(p.high_cut)
            self._highcut_spin.setEnabled(p.high_cut_enable)
        finally:
            for w in widgets:
                if w is not None:
                    w.blockSignals(False)
        # 频段推子（已按 mode 重建）
        for i, fader in enumerate(self._band_faders):
            gain = float(p.bands[i]) if i < len(p.bands) else 0.0
            fader.set_gain(gain)
        self._response_widget.set_params(p, self._sample_rate)

    def _collect_params(self) -> StripEQParams:
        """从控件收集当前 StripEQParams。"""
        bands = tuple(f._slider_to_gain(f._slider.value()) for f in self._band_faders)
        return StripEQParams(
            mode=self._current.mode,
            bands=bands,
            gain=float(self._gain_spin.value()),
            low_cut=float(self._lowcut_spin.value()),
            high_cut=float(self._highcut_spin.value()),
            low_cut_enable=self._lowcut_check.isChecked(),
            high_cut_enable=self._highcut_check.isChecked(),
            enabled=self._enable_check.isChecked(),
        )

    def _emit_change(self) -> None:
        """根据拖动状态发 preview 或 committed。"""
        self._current = self._collect_params()
        self._response_widget.set_params(self._current, self._sample_rate)
        if self._dragging:
            self.strip_eq_preview.emit(self._track_id, self._current)
        else:
            self.strip_eq_committed.emit(self._track_id, self._current)

    # ────────────────────────── 事件处理 ──────────────────────────

    def _on_enabled_toggled(self, checked: bool) -> None:
        self._current = self._collect_params()
        self._response_widget.set_params(self._current, self._sample_rate)
        # 总开关切换立即提交
        self.strip_eq_committed.emit(self._track_id, self._current)

    def _on_mode_changed(self, combo_idx: int) -> None:
        """频段模式切换：重建推子 + 刷新预设。"""
        if combo_idx < 0 or combo_idx >= len(EQ_MODES):
            return
        new_mode = EQ_MODES[combo_idx]
        if new_mode == self._current.mode:
            return
        # 切换 mode：bands 补零到新长度
        n = get_band_count(new_mode)
        old_bands = self._current.bands
        new_bands = tuple(float(old_bands[i]) if i < len(old_bands) else 0.0 for i in range(n))
        self._current = StripEQParams(
            mode=new_mode, bands=new_bands, gain=self._current.gain,
            low_cut=self._current.low_cut, high_cut=self._current.high_cut,
            low_cut_enable=self._current.low_cut_enable,
            high_cut_enable=self._current.high_cut_enable,
            enabled=self._current.enabled,
        )
        self._rebuild_band_faders()
        self._refresh_preset_list()
        self._load_params_to_ui()
        # mode 切换立即提交
        self.strip_eq_committed.emit(self._track_id, self._current)

    def _on_preset_selected(self, combo_idx: int) -> None:
        """应用预设到 bands。"""
        if combo_idx <= 0:
            return
        bands_data = self._preset_combo.itemData(combo_idx)
        if bands_data is None:
            return
        # 应用预设增益
        self._current = StripEQParams(
            mode=self._current.mode,
            bands=tuple(float(b) for b in bands_data),
            gain=self._current.gain,
            low_cut=self._current.low_cut, high_cut=self._current.high_cut,
            low_cut_enable=self._current.low_cut_enable,
            high_cut_enable=self._current.high_cut_enable,
            enabled=self._current.enabled,
        )
        self._load_params_to_ui()
        self._preset_combo.setCurrentIndex(0)  # 复位到 "—"
        self.strip_eq_committed.emit(self._track_id, self._current)

    def _on_band_changed(self, band_idx: int, gain_db: float) -> None:
        """某个频段推子变化。"""
        bands = list(self._current.bands)
        while len(bands) <= band_idx:
            bands.append(0.0)
        bands[band_idx] = gain_db
        self._current = StripEQParams(
            mode=self._current.mode, bands=tuple(bands),
            gain=self._current.gain,
            low_cut=self._current.low_cut, high_cut=self._current.high_cut,
            low_cut_enable=self._current.low_cut_enable,
            high_cut_enable=self._current.high_cut_enable,
            enabled=self._current.enabled,
        )
        self._response_widget.set_params(self._current, self._sample_rate)
        if self._dragging:
            self.strip_eq_preview.emit(self._track_id, self._current)
        else:
            self.strip_eq_committed.emit(self._track_id, self._current)

    def _on_filter_changed(self) -> None:
        """Low-Cut / High-Cut / 整体增益变化。"""
        self._lowcut_spin.setEnabled(self._lowcut_check.isChecked())
        self._highcut_spin.setEnabled(self._highcut_check.isChecked())
        self._emit_change()

    def _on_drag_pressed(self) -> None:
        self._dragging = True

    def _on_drag_released(self) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._current = self._collect_params()
        self.strip_eq_committed.emit(self._track_id, self._current)

    def _on_reset(self) -> None:
        """重置为平直响应（保留当前 mode）。"""
        self._current = StripEQParams.default(self._current.mode)
        self._rebuild_band_faders()
        self._load_params_to_ui()
        self.strip_eq_committed.emit(self._track_id, self._current)

    def _on_close(self) -> None:
        self.accept()

    def update_sample_rate(self, sample_rate: float) -> None:
        """外部通知采样率变化。"""
        self._sample_rate = float(sample_rate)
        self._response_widget.set_params(self._current, self._sample_rate)
