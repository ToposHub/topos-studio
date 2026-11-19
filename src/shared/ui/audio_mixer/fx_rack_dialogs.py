"""FX 机架通用参数对话框（2026-11-19 计划第三阶段 #1-#7 新效果器）。

与 *_dialog.py 同一对接契约：
  - 构造：``(track_id, track_name, instance_id, effect_type, initial_params, parent)``
  - 信号：``fx_preview`` / ``fx_committed`` (track_id, params dict)
  - ``set_current_params(dict)``：R06 外部链变化回填（不发信号）

控件表驱动（``_FX_FIELD_SPECS``）：float 滑条 / 整数 / 枚举 / 开关 /
三段网格（multiband）/ IR 文件选择（convolution_reverb）。字段默认值
来自 DSP DEFAULTS；滑条实时预览、松手提交，枚举/开关直接提交。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import PRO_DIALOG_QSS


@dataclass(frozen=True)
class FieldSpec:
    """单个参数控件规格。"""
    key: str
    label: str            # I18N key
    label_default: str
    kind: str             # "float" | "int" | "combo" | "bool" | "bands" | "ir_file"
    lo: float = 0.0
    hi: float = 1.0
    default: Any = None
    options: tuple = ()   # combo 选项（value, label）


def _f(key: str, label: str, lo: float, hi: float, default: float) -> FieldSpec:
    return FieldSpec(key, f"audio.fx.{key}", label, "float", lo, hi, default)


def _c(key: str, label: str, options: tuple, default: Any) -> FieldSpec:
    return FieldSpec(key, f"audio.fx.{key}", label, "combo", 0.0, 0.0,
                     default, options)


def _b(key: str, label: str, default: bool) -> FieldSpec:
    return FieldSpec(key, f"audio.fx.{key}", label, "bool", 0.0, 0.0, default)


_MODE_WIDE = (("wide", "Wide (broadband)"), ("split", "Split (subtractive)"))
_MODE_TREMOLO = (("tremolo", "Tremolo"), ("auto_pan", "Auto Pan"))
_MODE_CF = (("chorus", "Chorus"), ("flanger", "Flanger"))

_FX_FIELD_SPECS: dict[str, tuple[FieldSpec, ...]] = {
    "de_esser": (
        _f("threshold_dbfs", "Threshold (dBFS)", -60.0, 0.0, -32.0),
        _f("ratio", "Ratio", 1.0, 12.0, 4.0),
        _f("frequency", "Split frequency (Hz)", 1000.0, 16000.0, 6500.0),
        _f("q", "Resonance (Q)", 0.3, 12.0, 2.0),
        _f("attack_ms", "Attack (ms)", 0.05, 50.0, 5.0),
        _f("release_ms", "Release (ms)", 1.0, 500.0, 60.0),
        _f("max_reduction_db", "Max reduction (dB)", 0.0, 24.0, 12.0),
        _c("mode", "Mode", _MODE_WIDE, "split"),
        _b("listen", "Listen isolate", False),
    ),
    "true_peak_limiter": (
        _f("ceiling_dbtp", "Ceiling (dBTP)", -12.0, 0.0, -1.0),
        _f("lookahead_ms", "Lookahead (ms)", 1.0, 20.0, 5.0),
        _f("release_ms", "Release (ms)", 20.0, 500.0, 80.0),
        _b("linked", "Link channels", True),
    ),
    "dynamic_eq": (
        _f("frequency", "Frequency (Hz)", 20.0, 20000.0, 3000.0),
        _f("q", "Resonance (Q)", 0.3, 12.0, 1.4),
        _f("gain_db", "Base gain (dB)", -18.0, 18.0, 0.0),
        _f("threshold_dbfs", "Threshold (dBFS)", -60.0, 0.0, -30.0),
        _f("max_cut_db", "Max cut (dB)", 0.0, 24.0, 9.0),
        _f("max_boost_db", "Max boost (dB)", 0.0, 12.0, 0.0),
        _f("attack_ms", "Attack (ms)", 0.5, 100.0, 8.0),
        _f("release_ms", "Release (ms)", 10.0, 1000.0, 120.0),
        _b("dynamic_enabled", "Dynamic on", True),
        _f("slope", "Threshold slope", 0.0, 1.0, 0.5),
    ),
    "bus_comp": (
        _c("template", "Glue template",
           (("dx_glue", "DX Glue"), ("mx_glue", "MX Glue")), "dx_glue"),
        _f("sc_hpf_hz", "Sidechain HPF (Hz)", 0.0, 500.0, 120.0),
    ),
    "multiband_comp": (
        _f("crossover_low", "Crossover 1 (Hz)", 60.0, 800.0, 180.0),
        _f("crossover_high", "Crossover 2 (Hz)", 800.0, 12000.0, 4200.0),
        _f("attack_ms", "Attack (ms)", 1.0, 100.0, 10.0),
        FieldSpec("bands", "audio.fx.bands", "Bands (low / mid / high)",
                  "bands", 0.0, 0.0, None),
    ),
    "convolution_reverb": (
        FieldSpec("ir_path", "audio.fx.ir_file", "Impulse response (WAV)",
                  "ir_file", 0.0, 0.0, ""),
        _f("wet", "Wet", 0.0, 1.0, 0.35),
        _f("dry", "Dry", 0.0, 1.0, 1.0),
        _b("true_stereo", "True stereo (4-path)", False),
    ),
    "transient_designer": (
        _f("attack_db", "Attack (dB)", -12.0, 24.0, 6.0),
        _f("sustain_db", "Sustain (dB)", -24.0, 12.0, 0.0),
        _f("attack_ms", "Attack time (ms)", 0.5, 50.0, 8.0),
        _f("sustain_ms", "Sustain release (ms)", 10.0, 300.0, 80.0),
    ),
    "tremolo_auto_pan": (
        _c("mode", "Mode", _MODE_TREMOLO, "tremolo"),
        _f("rate_hz", "Rate (Hz)", 0.05, 20.0, 4.0),
        _f("depth", "Depth", 0.0, 1.0, 0.7),
    ),
    "chorus_flanger": (
        _c("mode", "Mode", _MODE_CF, "chorus"),
        _f("rate_hz", "Rate (Hz)", 0.01, 20.0, 0.6),
        _f("depth_ms", "Depth (ms)", 0.0, 10.0, 3.0),
        _f("base_ms", "Base delay (ms)", 0.5, 40.0, 18.0),
        _f("feedback", "Feedback", 0.0, 0.95, 0.0),
        FieldSpec("voices", "audio.fx.voices", "Voices", "int", 1.0, 4.0, 2),
        _f("mix", "Mix", 0.0, 1.0, 0.5),
    ),
    "phaser": (
        _f("rate_hz", "Rate (Hz)", 0.01, 20.0, 0.5),
        FieldSpec("stages", "audio.fx.stages", "Stages", "int", 1.0, 12.0, 4),
        _f("base_hz", "Base frequency (Hz)", 100.0, 4000.0, 600.0),
        _f("sweep_octaves", "Sweep (octaves)", 0.5, 5.0, 2.5),
        _f("mix", "Mix", 0.0, 1.0, 0.5),
        _f("feedback", "Feedback", 0.0, 0.9, 0.3),
    ),
}

# multiband band 网格列（DSP bands dict 的键）
_BAND_COLUMNS = (
    ("threshold_db", "Thresh (dB)", -60.0, 0.0),
    ("ratio", "Ratio", 1.0, 10.0),
    ("release_ms", "Release (ms)", 20.0, 1000.0),
    ("makeup_db", "Makeup (dB)", 0.0, 24.0),
)
_BAND_NAMES = ("Low", "Mid", "High")


def field_specs_for(effect_type: str) -> tuple[FieldSpec, ...]:
    """该效果器的控件规格（未知类型返回空 → 不启用通用对话框）。"""
    return _FX_FIELD_SPECS.get(effect_type, ())


def _params_to_ui(effect_type: str, params: dict) -> dict:
    """DSP 键 → UI 扁平键（multiband 的 crossovers 列表拆成两个滑条键）。"""
    params = dict(params or {})
    if effect_type == "multiband_comp":
        crossovers = params.pop("crossovers", None)
        if isinstance(crossovers, (list, tuple)) and len(crossovers) == 2:
            params.setdefault("crossover_low", float(crossovers[0]))
            params.setdefault("crossover_high", float(crossovers[1]))
    return params


def _params_from_ui(effect_type: str, params: dict) -> dict:
    """UI 扁平键 → DSP 键（multiband 合回 crossovers 列表）。"""
    params = dict(params or {})
    if effect_type == "multiband_comp":
        low = params.pop("crossover_low", None)
        high = params.pop("crossover_high", None)
        if low is not None or high is not None:
            previous = params.get("crossovers")
            previous = previous if isinstance(previous, (list, tuple)) and                 len(previous) == 2 else (180.0, 4200.0)
            params["crossovers"] = [
                float(low) if low is not None else float(previous[0]),
                float(high) if high is not None else float(previous[1]),
            ]
    return params


class FxParamDialog(QDialog):
    """通用 FX 参数对话框（表驱动）。"""

    fx_preview = pyqtSignal(str, dict)
    fx_committed = pyqtSignal(str, dict)

    def __init__(self, track_id: str, track_name: str, instance_id: str,
                 effect_type: str, initial_params: dict,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._track_id = str(track_id)
        self._instance_id = str(instance_id)
        self.fx_effect_type = str(effect_type)
        self._specs = field_specs_for(effect_type)
        self._params = _params_to_ui(effect_type, initial_params)
        title = I18N.tr(f"audio.fx.name.{effect_type}", effect_type)
        self.setWindowTitle(f"{title} · {track_name}")
        self.setStyleSheet(PRO_DIALOG_QSS)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        root.addLayout(form)
        self._widgets: dict[str, Any] = {}

        for spec in self._specs:
            if spec.kind == "float":
                form.addRow(self._make_float_row(spec))
            elif spec.kind == "int":
                form.addRow(self._make_int_row(spec))
            elif spec.kind == "combo":
                form.addRow(self._make_combo_row(spec))
            elif spec.kind == "bool":
                form.addRow(self._make_bool_row(spec))
            elif spec.kind == "bands":
                form.addRow(self._make_bands_box(spec))
            elif spec.kind == "ir_file":
                form.addRow(self._make_ir_row(spec))

    # ── 控件构造 ─────────────────────────────────────────────────────

    def _label(self, spec: FieldSpec) -> QLabel:
        return QLabel(I18N.tr(spec.label, spec.label_default))

    def _make_float_row(self, spec: FieldSpec) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal, row)
        steps = max(1, int(round((spec.hi - spec.lo) * 100)))
        slider.setRange(0, steps)
        value = float(self._params.get(spec.key, spec.default if spec.default is not None else spec.lo))
        value = min(spec.hi, max(spec.lo, value))
        slider.setValue(int(round((value - spec.lo) / (spec.hi - spec.lo) * steps)))
        readout = QLabel(self)
        readout.setMinimumWidth(72)
        self._widgets[spec.key] = (slider, readout, spec)
        self._update_float_readout(spec)
        slider.sliderMoved.connect(lambda _v: self._on_float_changed(spec, commit=False))
        slider.valueChanged.connect(lambda _v: self._on_float_changed(spec, commit=False))
        slider.sliderReleased.connect(lambda: self._on_float_changed(spec, commit=True))
        layout.addWidget(slider, 1)
        layout.addWidget(readout)
        return row

    def _make_int_row(self, spec: FieldSpec) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        spin = QSpinBox(row)
        spin.setRange(int(spec.lo), int(spec.hi))
        spin.setValue(int(self._params.get(spec.key, spec.default) or spec.lo))
        spin.valueChanged.connect(
            lambda v, s=spec: self._on_param_value(s.key, int(v), commit=True))
        self._widgets[spec.key] = spin
        layout.addWidget(self._label(spec))
        layout.addWidget(spin)
        return row

    def _make_combo_row(self, spec: FieldSpec) -> QWidget:
        combo = QComboBox(self)
        for value, label in spec.options:
            combo.addItem(label, value)
        current = self._params.get(spec.key, spec.default)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.currentIndexChanged.connect(
            lambda _i, s=spec: self._on_param_value(s.key, combo.currentData(), commit=True))
        self._widgets[spec.key] = combo
        return self._wrap_labeled(spec, combo)

    def _make_bool_row(self, spec: FieldSpec) -> QWidget:
        check = QCheckBox(self)
        check.setChecked(bool(self._params.get(spec.key, spec.default)))
        check.toggled.connect(
            lambda on, s=spec: self._on_param_value(s.key, bool(on), commit=True))
        self._widgets[spec.key] = check
        check.setText(I18N.tr(spec.label, spec.label_default))
        return check

    def _make_ir_row(self, spec: FieldSpec) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        readout = QLabel(self)
        readout.setMinimumWidth(120)
        path = str(self._params.get(spec.key, "") or "")
        readout.setText(path or I18N.tr("audio.fx.no_ir", "No impulse response"))
        button = QPushButton(I18N.tr("audio.fx.choose_ir", "Choose WAV…"), self)
        button.clicked.connect(lambda: self._choose_ir(spec, readout))
        self._widgets[spec.key] = readout
        layout.addWidget(readout, 1)
        layout.addWidget(button)
        return self._wrap_labeled(spec, row)

    def _make_bands_box(self, spec: FieldSpec) -> QWidget:
        box = QGroupBox(I18N.tr(spec.label, spec.label_default), self)
        grid = QGridLayout(box)
        bands = self._params.get(spec.key)
        if not isinstance(bands, (list, tuple)) or len(bands) != 3:
            bands = self._default_bands()
        spins: list[dict[str, QDoubleSpinBox]] = []
        for row_index, (band, name) in enumerate(zip(bands, _BAND_NAMES)):
            name_label = QLabel(I18N.tr("audio.fx.band", name), box)
            grid.addWidget(name_label, row_index, 0)
            row_spins: dict[str, QDoubleSpinBox] = {}
            for col, (key, label, lo, hi) in enumerate(_BAND_COLUMNS, start=1):
                spin = QDoubleSpinBox(box)
                spin.setRange(lo, hi)
                spin.setDecimals(1)
                spin.setSingleStep((hi - lo) / 100.0)
                spin.setValue(float(band.get(key, lo)) if isinstance(band, dict) else lo)
                spin.editingFinished.connect(
                    lambda s=spin: self._on_bands_changed(commit=True))
                grid.addWidget(spin, row_index, col)
                row_spins[key] = spin
            spins.append(row_spins)
        self._widgets["bands"] = spins
        return box

    def _wrap_labeled(self, spec: FieldSpec, widget: QWidget) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label(spec))
        layout.addWidget(widget, 1)
        return row

    @staticmethod
    def _default_bands() -> list[dict[str, float]]:
        return [
            {"threshold_db": -20.0, "ratio": 2.0, "release_ms": 220.0, "makeup_db": 0.0},
            {"threshold_db": -24.0, "ratio": 2.5, "release_ms": 150.0, "makeup_db": 0.0},
            {"threshold_db": -22.0, "ratio": 2.0, "release_ms": 180.0, "makeup_db": 0.0},
        ]

    # ── 状态读写 ─────────────────────────────────────────────────────

    def _update_float_readout(self, spec: FieldSpec) -> None:
        slider, readout, _spec = self._widgets[spec.key]
        steps = slider.maximum()
        value = spec.lo + (spec.hi - spec.lo) * slider.value() / steps
        readout.setText(f"{value:.2f}")

    def _on_float_changed(self, spec: FieldSpec, commit: bool) -> None:
        slider, _readout, _spec = self._widgets[spec.key]
        steps = slider.maximum()
        value = spec.lo + (spec.hi - spec.lo) * slider.value() / steps
        self._params[spec.key] = float(value)
        self._update_float_readout(spec)
        self._emit(commit)

    def _on_param_value(self, key: str, value: Any, commit: bool) -> None:
        self._params[key] = value
        self._emit(commit)

    def _on_bands_changed(self, commit: bool) -> None:
        spins = self._widgets.get("bands") or []
        bands = []
        for row_spins in spins:
            bands.append({key: float(spin.value()) for key, spin in row_spins.items()})
        self._params["bands"] = bands
        self._emit(commit)

    def _choose_ir(self, spec: FieldSpec, readout: QLabel) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, I18N.tr("audio.fx.choose_ir", "Choose WAV…"), "",
            I18N.tr("audio.fx.wav_filter", "WAV audio (*.wav)"))
        if path:
            self._params[spec.key] = path
            readout.setText(path)
            self._emit(True)

    def _emit(self, commit: bool) -> None:
        payload = dict(self.current_params())
        if commit:
            self.fx_committed.emit(self._track_id, payload)
        else:
            self.fx_preview.emit(self._track_id, payload)

    def current_params(self) -> dict:
        """当前控件状态 → DSP 参数 dict（未展示的键原样保留）。"""
        return _params_from_ui(self.fx_effect_type, self._params)

    def get_state(self) -> dict:
        """兼容 EffectWindowManager 的 capture_state 契约。"""
        return self.current_params()

    def set_current_params(self, params: dict) -> None:
        """R06：外部链变化回填（不发信号）。"""
        self._params = dict(params or {})
        for spec in self._specs:
            widgets = self._widgets.get(spec.key)
            if widgets is None:
                continue
            value = self._params.get(spec.key)
            if spec.kind == "float":
                slider, readout, _s = widgets
                value = float(value if value is not None else spec.lo)
                value = min(spec.hi, max(spec.lo, value))
                steps = slider.maximum()
                slider.blockSignals(True)
                slider.setValue(int(round((value - spec.lo) / (spec.hi - spec.lo) * steps)))
                slider.blockSignals(False)
                self._update_float_readout(spec)
            elif spec.kind == "int":
                widgets.blockSignals(True)
                widgets.setValue(int(value or spec.lo))
                widgets.blockSignals(False)
            elif spec.kind == "combo":
                index = widgets.findData(value)
                if index >= 0:
                    widgets.blockSignals(True)
                    widgets.setCurrentIndex(index)
                    widgets.blockSignals(False)
            elif spec.kind == "bool":
                widgets.blockSignals(True)
                widgets.setChecked(bool(value))
                widgets.blockSignals(False)
            elif spec.kind == "bands":
                bands = value if isinstance(value, (list, tuple)) and len(value) == 3 \
                    else self._default_bands()
                for band, row_spins in zip(bands, widgets):
                    for key, spin in row_spins.items():
                        spin.blockSignals(True)
                        spin.setValue(float(band.get(key, spin.minimum()))
                                      if isinstance(band, dict) else spin.minimum())
                        spin.blockSignals(False)
            elif spec.kind == "ir_file":
                widgets.setText(str(value or "") or
                                I18N.tr("audio.fx.no_ir", "No impulse response"))
