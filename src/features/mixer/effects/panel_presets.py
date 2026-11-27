"""Connect the supplied preset controls to the existing user preset library."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QObject, QSignalBlocker
from PyQt6.QtWidgets import QComboBox, QInputDialog, QMessageBox, QPushButton, QWidget

from src.features.edit.core.effect_preset import load_user_presets, remove_user_preset, save_user_preset
from src.shared.i18n.i18n import I18N


_LIBRARY_KEYS = {
    "delay": "delay",
    "dynamics": "dynamics",
    "imager": "imager",
    "reverb": "reverb",
    "spatial_panner": "panner",
    "spectral_mangler": "spectral_mangler",
    "strip_eq": "strip_eq",
    "dynamic_eq": "dynamic_eq",
    "de_esser": "de_esser",
    "compressor": "compressor",
    "limiter": "limiter",
    "expander": "expander",
    "noise_gate": "noise_gate",
    "bus_comp": "bus_comp",
    "multiband_comp": "multiband_comp",
    "transient_designer": "transient_designer",
    "chorus_flanger": "chorus_flanger",
    "phaser": "phaser",
    "tremolo_auto_pan": "tremolo_auto_pan",
    "true_peak_limiter": "true_peak_limiter",
    "voice_changer": "voice_changer",
}
_PRESERVE_WHEN_LOADING = {
    "delay": ("enabled", "link_lr", "input_format"),
    "dynamics": ("enabled",),
    "imager": ("enabled",),
    "reverb": ("enabled",),
    "spatial_panner": ("enabled", "boom_on"),
    "strip_eq": ("enabled",),
    "dynamic_eq": ("enabled",),
    "de_esser": ("enabled", "listen"),
    "compressor": ("enabled", "sc_listen"),
    "limiter": ("enabled",),
    "expander": ("enabled",),
    "noise_gate": ("enabled",),
    "bus_comp": ("enabled",),
    "multiband_comp": ("enabled",),
    "transient_designer": ("enabled",),
    "chorus_flanger": ("enabled",),
    "phaser": ("enabled",),
    "tremolo_auto_pan": ("enabled",),
    "true_peak_limiter": ("enabled",),
}


def install_effect_presets(
    effect_type: str,
    panel: QWidget,
    params_reader: Callable[[], dict[str, Any]],
    apply_params: Callable[[dict[str, Any]], None],
) -> None:
    """Keep factory presets native and route user presets through the editor."""
    if effect_type in _LIBRARY_KEYS:
        _EffectPresets(effect_type, panel, params_reader, apply_params)


class _EffectPresets(QObject):
    def __init__(
        self,
        effect_type: str,
        panel: QWidget,
        params_reader: Callable[[], dict[str, Any]],
        apply_params: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__(panel)
        self._panel = panel
        self._effect_type = effect_type
        self._library_key = _LIBRARY_KEYS[effect_type]
        self._params_reader = params_reader
        self._apply_params = apply_params
        self._combo: QComboBox = getattr(panel, "preset_combo")
        self._load_factory: Callable[[str], None] = getattr(panel, "_load_preset")
        self._factory_names = {
            self._combo.itemText(index) for index in range(self._combo.count())
        }
        self._combo.currentTextChanged.disconnect(self._load_factory)
        self._combo.currentIndexChanged.connect(self._select)

        for button in panel.findChildren(QPushButton, "ToolBtn"):
            if button.text() == "+":
                button.clicked.disconnect(getattr(panel, "_save_new_preset"))
                button.clicked.connect(self._save)
            elif effect_type in ("voice_changer", "chorus_flanger", "phaser") and button.text() == "−":
                button.clicked.disconnect(getattr(panel, "_delete_current_preset"))
                button.clicked.connect(self._delete)

        for entry in load_user_presets(self._library_key):
            self._insert(entry)

    def _insert(self, entry: dict[str, Any]) -> int:
        name = entry["name"]
        if self._effect_type == "voice_changer":
            name = name.removeprefix("vc:")
        if name in self._factory_names:
            name += I18N.tr("audio.fx.user_preset_suffix", " (User)")
        self._combo.addItem(name, entry)
        return self._combo.count() - 1

    def _select(self, index: int) -> None:
        if index < 0:
            return
        entry = self._combo.itemData(index)
        if entry is None:
            self._load_factory(self._combo.itemText(index))
            return
        values = deepcopy(entry["params"])
        if self._effect_type == "spatial_panner":
            lr = values.pop("lr", None)
            if isinstance(lr, (int, float)):
                values["pan"] = lr / 100.0
        elif self._effect_type == "dynamics" and "makeup" not in values:
            compressor = values.get("comp")
            if isinstance(compressor, dict) and "makeup_gain" in compressor:
                values["makeup"] = compressor["makeup_gain"]

        current = self._params_reader()
        for key in _PRESERVE_WHEN_LOADING.get(self._effect_type, ()):
            if key in current:
                values[key] = current[key]
        self._apply_params(values)

    def _save(self) -> None:
        selected = self._combo.currentData()
        name, confirmed = QInputDialog.getText(
            self._panel,
            I18N.tr("audio.fx.save_preset_title", "Save preset"),
            I18N.tr("audio.fx.preset_name_prompt", "Preset name"),
            text=str(selected["name"]) if selected is not None else "",
        )
        name = name.strip()
        if not confirmed or not name:
            return
        if name in self._factory_names:
            self._warn(I18N.tr("audio.fx.preset_factory_name", "Choose a name distinct from the factory presets."))
            return
        params = deepcopy(self._params_reader())
        if self._effect_type == "transient_designer":
            params.pop("sc_listen", None)
        if self._effect_type == "spatial_panner" and "pan" in params:
            params["lr"] = float(params.pop("pan")) * 100.0
        # A freeze's asset identity belongs to its source project.
        params.pop("freeze_capture_id", None)
        stored = load_user_presets(self._library_key)
        previous = next((item for item in stored if item["name"] == name), None)
        preset_id = (previous["preset_id"] if previous is not None else "") or str(uuid4())
        if not save_user_preset(self._library_key, name, params, preset_id=preset_id):
            self._warn(I18N.tr("audio.fx.preset_save_failed", "The preset could not be saved."))
            return
        entry = {"preset_id": preset_id, "name": name, "params": params}
        with QSignalBlocker(self._combo):
            index = next((index for index in range(self._combo.count())
                          if (self._combo.itemData(index) or {}).get("preset_id") == preset_id), -1)
            if index < 0:
                index = self._insert(entry)
            else:
                self._combo.setItemData(index, entry)
            self._combo.setCurrentIndex(index)

    def _delete(self) -> None:
        entry = self._combo.currentData()
        if entry is None:
            return
        if not remove_user_preset(self._library_key, entry["preset_id"]):
            self._warn(I18N.tr("audio.fx.preset_delete_failed", "The preset could not be deleted."))
            return
        with QSignalBlocker(self._combo):
            self._combo.removeItem(self._combo.currentIndex())
            self._combo.setCurrentIndex(-1)

    def _warn(self, message: str) -> None:
        QMessageBox.warning(self._panel, I18N.tr("audio.fx.preset", "Preset"), message)
