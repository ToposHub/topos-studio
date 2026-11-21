"""Host the supplied audio-effect panels on the existing project model."""
from __future__ import annotations

from copy import deepcopy
import logging
from math import isfinite
from typing import Any

from PyQt6.QtCore import QSignalBlocker, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from src.features.edit.core.effect_registry import get_effect_definition
from src.features.edit.core.timeline_v2 import (
    CompressorParams, SpatialPannerParams, _ensure_effect_param_registry, _params_f,
)
from src.shared.i18n.i18n import I18N

from .bindings_core import collect_fields, project_fields, with_fallbacks
from .catalog import create_panel
from .classic_bindings import CLASSIC_BINDINGS
from .panel_presets import install_effect_presets
from .panel_style import fit_effect_window, style_panel_inputs
from .rack_bindings import RACK_BINDINGS
from .windows import stop_panel_timers

logger = logging.getLogger(__name__)

PRODUCTION_BINDINGS = {**CLASSIC_BINDINGS, **RACK_BINDINGS}

_DYNAMICS_CARD = {"expander": "eg", "noise_gate": "eg", "compressor": "comp", "limiter": "lim"}

_TOOLBAR_STYLE = """
QFrame#EffectWindowToolbar {
    background: #27272B;
    border-bottom: 1px solid #141416;
}
QFrame#EffectWindowToolbar QLabel {
    color: #AEB0B9;
    font-size: 11px;
}
QFrame#EffectWindowToolbar QPushButton {
    border-radius: 3px;
    font-size: 11px;
    font-weight: 600;
    min-width: 44px;
    padding: 3px 8px;
}
QPushButton#EffectWindowAction {
    background: #323238;
    color: #D0D0D8;
    border: 1px solid transparent;
}
QPushButton#EffectWindowAction:hover { background: #3C3C44; color: white; }
QPushButton#EffectWindowAction:pressed { background: #2A2A2F; }
QFrame#EffectWindowToolbar QPushButton:focus { border: 1px solid #888890; }
QFrame#EffectWindowToolbar QPushButton:disabled {
    background: #323238;
    color: #85858E;
}
"""


def _tr(key: str, fallback: str) -> str:
    return I18N.tr(f"mixer.effect_panel.{key}", fallback)


def _normalized(effect_type: str, params: dict[str, Any]) -> dict[str, Any]:
    if effect_type == "spatial_panner":
        return with_fallbacks(SpatialPannerParams.from_dict(params).to_dict(), params)
    cls = _ensure_effect_param_registry()[effect_type]
    source = params
    if effect_type == "noise_gate" and "threshold_db" not in params:
        source = {"mode": "Gate", **params}
    if effect_type == "multiband_comp":
        crossovers = params.get("crossovers")
        if isinstance(crossovers, (list, tuple)) and len(crossovers) == 2:
            # Playback reads the canonical array. Old inspector aliases can
            # coexist with it in a saved project and should not mask it.
            source = {key: value for key, value in params.items() if key not in {"crossover_low", "crossover_high"}}
    try:
        parsed = cls.from_dict(source).to_dict()
    except (TypeError, ValueError, KeyError, OverflowError):
        logger.warning("Audio effect %s has invalid panel parameters", effect_type, exc_info=True)
        parsed = cls.default().to_dict()
    return with_fallbacks(parsed, params)


def _multiband_display_params(params: dict[str, Any]) -> dict[str, Any]:
    """Show the compressor's effective defaults without saving untouched band fields."""
    compressor = CompressorParams.default()
    defaults = compressor.to_dict() | {"attack_ms": 10.0}
    bands = []
    for band in params["bands"]:
        display = defaults | band
        if "knee_db" in band and "knee" not in band:
            knee_db = _params_f("knee_db", band, compressor.knee)
            display["knee"] = min(40.0, max(0.0, knee_db if isfinite(knee_db) else compressor.knee)) * 2.5
        bands.append(display)
    return params | {"bands": bands}


class ProductionEffectDialog(QDialog):
    """Preview mapped parameters and commit a single edit on Apply or OK."""

    preview = pyqtSignal(dict)
    committed = pyqtSignal(dict)

    def __init__(
        self,
        effect_type: str,
        owner_name: str,
        params: dict[str, Any],
        parent: QWidget | None = None,
        *,
        enabled: bool | None = None,
        sidechain_sources: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.fx_effect_type = effect_type
        panel_id, fields = PRODUCTION_BINDINGS[effect_type]
        self.fields = tuple(field for field in fields if field.ui != "enabled")
        self._params: dict[str, Any] = {}
        self._projection: dict[str, Any] = {}
        self._staged: dict[str, Any] = {}
        self._external_enabled: bool | None = None
        self._sidechain_sources = sidechain_sources
        self._syncing = False
        self._last_preview: tuple[dict[str, Any], bool | None] | None = None

        definition = get_effect_definition(effect_type)
        name = I18N.tr(f"audio.fx.name.{effect_type}", definition.name if definition else effect_type)
        self.setWindowTitle(f"{name} · {owner_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.panel = create_panel(panel_id, owner_name)
        self._configure_sections()
        self._default_state = self.panel.get_state()
        self._baseline: dict[str, Any] = {}
        self._make_spectral_limiter_state()
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(25)
        self._preview_timer.timeout.connect(self._emit_preview)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_toolbar())
        layout.addWidget(self.panel, 1)
        style_panel_inputs(self.panel)
        stop_panel_timers(self.panel)
        self._clear_demo_spectrum(panel_id)
        self.set_current_params(params, enabled=enabled)
        self.panel.stateChanged.connect(self._on_edited)
        install_effect_presets(effect_type, self.panel, self._preset_params, self._apply_user_preset)
        preset_combo = getattr(self.panel, "preset_combo", None)
        if preset_combo is not None:
            with QSignalBlocker(preset_combo):
                preset_combo.setPlaceholderText(_tr("current_settings", "Current settings"))
                preset_combo.setCurrentIndex(-1)
        fit_effect_window(self, self.panel)

    def _build_toolbar(self) -> QFrame:
        toolbar = QFrame(self)
        toolbar.setObjectName("EffectWindowToolbar")
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(10, 5, 10, 5)
        row.setSpacing(10)
        summary = _tr(
            "mapped_parameters",
            "Some design controls are pending audio support.",
        )
        if self.fx_effect_type == "dynamic_eq":
            summary += "\n" + _tr("dynamic_eq_scope", "The current processor uses band 4 as a bell filter.")
        elif self.fx_effect_type == "multiband_comp":
            summary += "\n" + _tr("multiband_comp_scope", "The current processor uses three bands and two crossovers.")
        status = QLabel(summary, toolbar)
        row.addWidget(status, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply, parent=toolbar,
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self._apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        ok.setText(_tr("ok", "OK"))
        cancel.setText(_tr("cancel", "Cancel"))
        ok.setObjectName("EffectWindowAction")
        cancel.setObjectName("EffectWindowAction")
        self._apply_button.setText(_tr("apply", "Apply"))
        self._apply_button.setObjectName("EffectWindowApply")
        for button in buttons.buttons():
            button.setAutoDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._apply_button.clicked.connect(self.apply)
        self._apply_button.setEnabled(False)
        row.addWidget(buttons)
        toolbar.setStyleSheet(_TOOLBAR_STYLE)
        return toolbar

    def _clear_demo_spectrum(self, panel_id: str) -> None:
        if panel_id in {"eq", "dynamic_eq", "saturation", "deesser"}:
            self.panel.update_spectrum([], [])
        if panel_id in {"eq", "reverb", "saturation"}:
            self.panel.update_output_meter(-100.0)

    def _configure_sections(self) -> None:
        configure = getattr(self.panel, "use_processor_controls", None)
        if self.fx_effect_type in {"chorus_flanger", "phaser"}:
            configure(self.fx_effect_type)
            if self.fx_effect_type == "chorus_flanger":
                self.panel.processorModeChanged.connect(self._change_modulation_mode)
        elif configure is not None:
            configure()
        if self.fx_effect_type == "saturation":
            self.panel.use_wave_shaper_drive()
        card = _DYNAMICS_CARD.get(self.fx_effect_type)
        if card:
            self.panel.show_stage(card)
        if self.fx_effect_type == "delay":
            combo = self.panel.format_combo
            with QSignalBlocker(combo):
                combo.clear()
                combo.addItems(["Mono", "Stereo", "Left", "Right"])

    def _make_spectral_limiter_state(self) -> None:
        if self.fx_effect_type == "spectral_mangler":
            self.panel.master_strip.limiterToggled.connect(self._set_limiter)

    def _set_limiter(self, enabled: bool) -> None:
        state = self.panel.get_state()
        state["limiter"] = enabled
        self.panel.set_state(state)
        self._on_edited(state)

    def set_sidechain_sources(self, sources: tuple[tuple[str, str], ...]) -> None:
        if self._sidechain_sources == sources:
            return
        self._sidechain_sources = sources
        if self.fx_effect_type in {"compressor", "dynamics"}:
            self._populate_sidechain(self.panel.get_state()["comp"]["sc_source"])

    def _populate_sidechain(self, current: str) -> None:
        combo = self.panel.comp_card.sc_source_combo
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItem("Internal", "Internal")
            for source_id, name in self._sidechain_sources:
                combo.addItem(name, source_id)
            if combo.findData(current) < 0:
                combo.addItem(current, current)
            combo.setCurrentIndex(combo.findData(current))

    def _change_modulation_mode(self, mode: str) -> None:
        if self._syncing or mode == self._projection["mode"]:
            return
        pending = self.current_params()
        pending["mode"] = mode
        self._stage(pending)
        self._on_edited({})

    def _stage(self, pending: dict[str, Any]) -> None:
        power = self.power_override
        self._staged = {key: value for key, value in pending.items() if self._params.get(key) != value}
        self._render(pending, pending_power=power)

    def _apply_user_preset(self, preset: dict[str, Any]) -> None:
        if self.fx_effect_type == "spatial_panner":
            panner = {key: value for key, value in preset.items() if key != "pan"}
            preset = {"pan": preset.get("pan", self._params.get("pan", 0.0)), "panner": panner}
        self._stage(with_fallbacks(preset, self.current_params()))
        self._on_edited({})

    def _preset_params(self) -> dict[str, Any]:
        return self._flat(self.current_params())

    def _flat(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.fx_effect_type != "spatial_panner":
            return params
        return {**(params.get("panner") or {}), "pan": params.get("pan", 0.0)}

    def _render(self, params: dict[str, Any], *, pending_power: bool | None = None) -> None:
        previous = self._syncing
        self._syncing = True
        try:
            self._projection = _normalized(self.fx_effect_type, self._flat(params))
            state = self.panel.get_state() if self._baseline else deepcopy(self._default_state)
            projection = (_multiband_display_params(self._projection) if self.fx_effect_type == "multiband_comp"
                          else self._projection)
            project_fields(state, projection, self.fields)
            engine_power = bool(self._flat(self._params).get("enabled", True))
            state["enabled"] = self._external_enabled is not False and engine_power
            card = _DYNAMICS_CARD.get(self.fx_effect_type)
            if card:
                for key in ("eg", "comp", "lim"):
                    state[key]["enabled"] = key == card
            if self.fx_effect_type in {"compressor", "dynamics"}:
                self._populate_sidechain(state["comp"]["sc_source"])
            if self.fx_effect_type in {"chorus_flanger", "phaser"}:
                self._configure_modulation(state)
            self.panel.set_state(state)
            if self.fx_effect_type == "spectral_mangler":
                self.panel.master_strip._limiter = bool(state.get("limiter", True))
                self.panel.master_strip.update()
            self._baseline = self.panel.get_state()
            if pending_power is not None:
                state["enabled"] = pending_power
                self.panel.set_state(state)
        finally:
            self._syncing = previous

    def _configure_modulation(self, state: dict[str, Any]) -> None:
        active = "phaser" if self.fx_effect_type == "phaser" else self._projection["mode"]
        for key in ("chorus", "flanger", "phaser"):
            state[key]["enabled"] = key == active
            card = getattr(self.panel, f"{key}_card")
            card.setVisible(key == active)
            card.power_sw.hide()

    def set_current_params(self, params: dict[str, Any], *, enabled: bool | None = None) -> None:
        """Apply a replacement model snapshot without sending edits back to it."""
        if self._baseline and self._params == params and self._external_enabled == enabled:
            return
        self._preview_timer.stop()
        self._params = deepcopy(params)
        self._external_enabled = enabled
        self._staged.clear()
        self._render(params)
        self._last_preview = None
        self._apply_button.setEnabled(False)

    @property
    def power_override(self) -> bool | None:
        current = bool(self.panel.get_state().get("enabled", True))
        return current if current != self._baseline.get("enabled", current) else None

    def current_params(self) -> dict[str, Any]:
        """Merge the changed controls into the original production parameters."""
        values = deepcopy(self._projection)
        changed = collect_fields(self.panel.get_state(), self._baseline, values, self.fields)
        self._validate_sidechain(values, changed)
        power = self.power_override
        if power is not None:
            changed.add("enabled")
            values["enabled"] = power
        updates = {key: values[key] for key in changed}
        if self.fx_effect_type == "spatial_panner":
            base = deepcopy(self._params) | deepcopy(self._staged)
            if updates:
                base["pan"] = updates.pop("pan", base.get("pan", 0.0))
                base["panner"] = (base.get("panner") or {}) | updates
            return base
        if self.fx_effect_type == "dynamics" and "makeup" in updates:
            updates.setdefault("comp", deepcopy(values["comp"]))["makeup_gain"] = updates["makeup"]
        return deepcopy(self._params) | deepcopy(self._staged) | updates

    def _validate_sidechain(self, values: dict[str, Any], changed: set[str]) -> None:
        if self.fx_effect_type == "compressor" and "sc_source" in changed:
            target, baseline = values, self._projection
        elif self.fx_effect_type == "dynamics" and "comp" in changed:
            target, baseline = values["comp"], self._projection["comp"]
        else:
            return
        source = target["sc_source"]
        if source in {"internal", baseline["sc_source"]}:
            return
        if any(source == track_id for track_id, _name in self._sidechain_sources):
            return
        matches = [track_id for track_id, name in self._sidechain_sources if source == name]
        target["sc_source"] = matches[0] if len(matches) == 1 else baseline["sc_source"]

    def _on_edited(self, _state: dict[str, Any]) -> None:
        if not self._syncing:
            self._apply_button.setEnabled(self.current_params() != self._params or self.power_override is not None)
            if not self._preview_timer.isActive():
                self._preview_timer.start()

    def _emit_preview(self) -> None:
        params, power = self.current_params(), self.power_override
        payload = (params, power)
        if payload != self._last_preview and (params != self._params or power is not None or self._last_preview is not None):
            self._last_preview = deepcopy(payload)
            self.preview.emit(params)

    @property
    def active_preview(self) -> tuple[dict[str, Any], bool | None] | None:
        return self._last_preview

    def apply(self) -> None:
        self._preview_timer.stop()
        params, power = self.current_params(), self.power_override
        if params == self._params and power is None:
            return
        self.committed.emit(params)
        self.set_current_params(params, enabled=power if power is not None else self._external_enabled)

    def accept(self) -> None:
        self.apply()
        super().accept()

    def done(self, result: int) -> None:
        self._preview_timer.stop()
        super().done(result)
