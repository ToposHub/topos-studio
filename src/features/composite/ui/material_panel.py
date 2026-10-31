"""Model3D material editor panel.

The panel is deliberately a UI-only adapter.  It receives immutable material
summaries from the render metadata path and emits a serialized override
document back to ``CompositePage``.  It never touches OpenGL objects or the
imported ``MaterialData`` instances.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.features.composite.render3d.material_model import (
    COLOR_SPACE_LINEAR,
    COLOR_SPACE_RAW,
    COLOR_SPACE_SRGB,
    MaterialOverride,
    TextureSlot,
    decode_material_overrides,
    default_material_presets,
    encode_material_overrides,
)
from src.shared.i18n.i18n import I18N


class MaterialPanel(QWidget):
    """Edit the supported per-material PBR fields and texture-slot metadata."""

    overrides_changed = pyqtSignal(str)
    _SLOT_NAMES = ("base_color", "normal", "orm", "ao", "emissive")
    _SLOT_OVERRIDE_NAMES = {
        name: f"{name}_slot" for name in _SLOT_NAMES
    }
    _COLOR_SPACES = (
        ("sRGB", COLOR_SPACE_SRGB),
        ("Linear", COLOR_SPACE_LINEAR),
        ("Raw", COLOR_SPACE_RAW),
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._guard = False
        self._materials: List[Dict[str, Any]] = []
        self._overrides: Dict[str, MaterialOverride] = {}
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)

        title = QLabel(I18N.tr("materials.title", "材质 / Materials"))
        title.setStyleSheet("color: #ddd; font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        self._material_combo = QComboBox()
        self._material_combo.setStyleSheet(self._combo_style())
        self._material_combo.currentIndexChanged.connect(self._on_material_changed)
        layout.addWidget(self._material_combo)

        self._status_label = QLabel(I18N.tr("materials.no_selection", "未选择导入材质"))
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #999; font-size: 10px;")
        layout.addWidget(self._status_label)

        form = QFormLayout()
        form.setContentsMargins(0, 2, 0, 0)
        form.setSpacing(4)
        self._rgb = [self._make_spin(0.0, 1.0, 0.01) for _ in range(3)]
        self._metallic = self._make_spin(0.0, 1.0, 0.01)
        self._roughness = self._make_spin(0.01, 1.0, 0.01)
        self._opacity = self._make_spin(0.0, 1.0, 0.01)
        rgb_row = QWidget()
        rgb_layout = QHBoxLayout(rgb_row)
        rgb_layout.setContentsMargins(0, 0, 0, 0)
        rgb_layout.setSpacing(3)
        for label, spin in zip(("R", "G", "B"), self._rgb):
            rgb_layout.addWidget(QLabel(label))
            rgb_layout.addWidget(spin, 1)
        form.addRow("基色", rgb_row)
        form.addRow("金属度", self._metallic)
        form.addRow("粗糙度", self._roughness)
        form.addRow("不透明度", self._opacity)
        layout.addLayout(form)

        for spin in (*self._rgb, self._metallic, self._roughness, self._opacity):
            spin.valueChanged.connect(self._on_value_changed)

        layout.addWidget(QLabel(I18N.tr("materials.texture_slots", "贴图槽 / Texture Slots")))
        self._slot_labels: Dict[str, QLabel] = {}
        self._slot_edits: Dict[str, QLineEdit] = {}
        self._slot_color_spaces: Dict[str, QComboBox] = {}
        self._slot_browse_buttons: Dict[str, QPushButton] = {}
        for slot_name in self._SLOT_NAMES:
            row = QHBoxLayout()
            key = QLabel(slot_name)
            key.setMinimumWidth(72)
            key.setStyleSheet("color: #888; font-size: 10px;")
            row.addWidget(key)
            edit = QLineEdit()
            edit.setPlaceholderText(I18N.tr("materials.slot_placeholder", "无贴图或输入替换路径"))
            edit.setStyleSheet(
                "QLineEdit { background: #2a2a2a; color: #bbb;"
                " border: 1px solid #444; padding: 1px; }"
            )
            edit.editingFinished.connect(
                lambda name=slot_name: self._on_slot_changed(name))
            row.addWidget(edit, 1)
            browse = QPushButton(I18N.tr("materials.browse", "选择"))
            browse.setFixedWidth(42)
            browse.clicked.connect(
                lambda _checked=False, name=slot_name: self._browse_slot(name))
            row.addWidget(browse)
            color_space = QComboBox()
            color_space.setFixedWidth(72)
            color_space.setStyleSheet(self._combo_style())
            for label, value in self._COLOR_SPACES:
                color_space.addItem(label, value)
            color_space.currentIndexChanged.connect(
                lambda _index, name=slot_name: self._on_slot_changed(name))
            row.addWidget(color_space)
            layout.addLayout(row)
            # Keep a compact status label API for existing callers/tests while
            # the line edit and color-space combo provide actual replacement.
            status = QLabel("—")
            status.setVisible(False)
            self._slot_labels[slot_name] = status
            self._slot_edits[slot_name] = edit
            self._slot_color_spaces[slot_name] = color_space
            self._slot_browse_buttons[slot_name] = browse

        buttons = QHBoxLayout()
        self._preset_combo = QComboBox()
        self._preset_combo.setStyleSheet(self._combo_style())
        self._preset_combo.addItem("预设…", None)
        for preset in default_material_presets():
            self._preset_combo.addItem(preset.name, preset)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        buttons.addWidget(self._preset_combo, 1)
        self._reset_button = QPushButton(I18N.tr("materials.reset", "还原材质"))
        self._reset_button.clicked.connect(self._reset_current)
        buttons.addWidget(self._reset_button)
        layout.addLayout(buttons)
        layout.addStretch()
        self._set_enabled(False)

    @staticmethod
    def _combo_style() -> str:
        return (
            "QComboBox { background: #2a2a2a; color: #ccc; border: 1px solid #444;"
            " padding: 2px; }"
        )

    @staticmethod
    def _make_spin(minimum: float, maximum: float, step: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setDecimals(3)
        spin.setStyleSheet(
            "QDoubleSpinBox { background: #2a2a2a; color: #ccc;"
            " border: 1px solid #444; padding: 1px; }"
        )
        return spin

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (*self._rgb, self._metallic, self._roughness, self._opacity,
                       self._reset_button, self._preset_combo):
            widget.setEnabled(enabled)
        for slot_name in self._SLOT_NAMES:
            self._slot_edits[slot_name].setEnabled(enabled)
            self._slot_color_spaces[slot_name].setEnabled(enabled)
            self._slot_browse_buttons[slot_name].setEnabled(enabled)

    def set_materials(self, materials: object, overrides: object = None) -> None:
        """Replace the render metadata snapshot and refresh the selected slot."""
        self._materials = [dict(item) for item in materials
                           if isinstance(item, Mapping)] \
            if isinstance(materials, (list, tuple)) else []
        self._overrides, _ = decode_material_overrides(overrides)
        previous = self.current_material_id()
        self._guard = True
        try:
            self._material_combo.clear()
            for item in self._materials:
                material_id = str(item.get("material_id", ""))
                name = str(item.get("name", "") or material_id)
                count = int(item.get("mesh_count", 1) or 1)
                self._material_combo.addItem(f"{name}  ({count} mesh)", material_id)
            if previous:
                index = self._material_combo.findData(previous)
                if index >= 0:
                    self._material_combo.setCurrentIndex(index)
        finally:
            self._guard = False
        self._set_enabled(bool(self._materials))
        self._refresh_current()

    def clear(self) -> None:
        """Clear material metadata when selection leaves Model3D."""
        self.set_materials([], "")

    def current_material_id(self) -> str:
        value = self._material_combo.currentData()
        return str(value or "")

    def _current_material(self) -> Optional[Dict[str, Any]]:
        current_id = self.current_material_id()
        return next((item for item in self._materials
                     if str(item.get("material_id", "")) == current_id), None)

    def _current_values(self) -> Dict[str, Any]:
        item = self._current_material() or {}
        values = dict(item)
        override = self._overrides.get(self.current_material_id())
        if override is not None:
            values.update(override.overrides)
        return values

    def _refresh_current(self) -> None:
        item = self._current_material()
        if item is None:
            self._status_label.setText(I18N.tr("materials.no_selection", "未选择导入材质"))
            for label in self._slot_labels.values():
                label.setText("—")
            for slot_name in self._SLOT_NAMES:
                self._slot_edits[slot_name].clear()
                self._slot_color_spaces[slot_name].setCurrentIndex(0)
            return
        values = self._current_values()
        # AUD-10309: the guard reset must live in ``finally`` — an exception
        # on any of the widget-update paths below used to leave ``_guard``
        # stuck at True, permanently ignoring all later user edits.
        self._guard = True
        try:
            try:
                color = values.get("base_color", [0.8, 0.8, 0.8])
                if not isinstance(color, (list, tuple)) or len(color) < 3:
                    color = [0.8, 0.8, 0.8]
                for spin, value in zip(self._rgb, color[:3]):
                    spin.setValue(float(value))
                self._metallic.setValue(float(values.get("metallic", 0.0)))
                self._roughness.setValue(
                    max(0.01, float(values.get("roughness", 0.5))))
                self._opacity.setValue(float(values.get("opacity", 1.0)))
            except (TypeError, ValueError):
                pass

            missing = []
            slots = item.get("slots", {})
            if not isinstance(slots, Mapping):
                slots = {}
            for slot_name, label in self._slot_labels.items():
                slot = slots.get(slot_name, {})
                if not isinstance(slot, Mapping):
                    slot = {}
                path = str(slot.get("path", "") or "")
                color_space = str(slot.get("color_space", "linear"))
                is_missing = bool(slot.get("missing", False))
                override = self._overrides.get(self.current_material_id())
                override_slot = (override.slot_overrides.get(
                    self._SLOT_OVERRIDE_NAMES[slot_name])
                    if override is not None else None)
                if override_slot is not None:
                    path = str(override_slot.path or "")
                    color_space = str(override_slot.color_space or color_space)
                    is_missing = bool(
                        path and os.path.isabs(path) and not os.path.isfile(path))
                self._slot_edits[slot_name].setText(path)
                combo = self._slot_color_spaces[slot_name]
                color_index = combo.findData(color_space)
                combo.setCurrentIndex(color_index if color_index >= 0 else 1)
                if is_missing:
                    missing.append(slot_name)
                label.setText(
                    ("⚠ " if is_missing else "")
                    + (path or "无") + f"  [{color_space}]"
                )
        finally:
            self._guard = False
        suffix = "；缺失: " + ", ".join(missing) if missing else ""
        override_suffix = "；已覆盖" if self.current_material_id() in self._overrides else ""
        self._status_label.setText(
            f"ID: {self.current_material_id()}{override_suffix}{suffix}"
        )

    def _on_material_changed(self, _index: int) -> None:
        if not self._guard:
            self._refresh_current()

    def _browse_slot(self, slot_name: str) -> None:
        """Choose a replacement texture without touching imported material data."""
        if slot_name not in self._slot_edits or not self.current_material_id():
            return
        current = self._slot_edits[slot_name].text().strip()
        path, _filter = QFileDialog.getOpenFileName(
            self, f"选择 {slot_name} 贴图", current,
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.exr *.hdr);;All files (*)",
        )
        if path:
            self._slot_edits[slot_name].setText(path)
            self._on_slot_changed(slot_name)

    def _on_slot_changed(self, slot_name: str) -> None:
        if (self._guard or slot_name not in self._slot_edits
                or not self.current_material_id()):
            return
        material = self._current_material() or {}
        material_id = self.current_material_id()
        override = self._overrides.setdefault(
            material_id,
            MaterialOverride(
                target_material_id=material_id,
                target_material_name=str(material.get("name", "") or ""),
            ),
        )
        color_space = self._slot_color_spaces[slot_name].currentData()
        override.set_slot(
            self._SLOT_OVERRIDE_NAMES[slot_name],
            TextureSlot(
                path=self._slot_edits[slot_name].text().strip(),
                color_space=str(color_space or COLOR_SPACE_LINEAR),
            ),
        )
        self._emit_overrides()
        self._refresh_current()

    def _on_value_changed(self, _value: float) -> None:
        if self._guard or not self.current_material_id():
            return
        material = self._current_material() or {}
        material_id = self.current_material_id()
        override = self._overrides.setdefault(
            material_id,
            MaterialOverride(
                target_material_id=material_id,
                target_material_name=str(material.get("name", "") or ""),
            ),
        )
        override.set_scalar("base_color", [spin.value() for spin in self._rgb])
        override.set_scalar("metallic", self._metallic.value())
        override.set_scalar("roughness", self._roughness.value())
        override.set_scalar("opacity", self._opacity.value())
        self._emit_overrides()
        self._refresh_current()

    def _on_preset_changed(self, _index: int) -> None:
        if self._guard:
            return
        preset = self._preset_combo.currentData()
        # AUD-10311: resetting the combo back to the "预设…" placeholder fires
        # currentIndexChanged again; hold the state lock so the re-entrant
        # signal is swallowed instead of re-entering this handler.
        self._guard = True
        try:
            self._preset_combo.setCurrentIndex(0)
        finally:
            self._guard = False
        if preset is None or not self.current_material_id():
            return
        material_id = self.current_material_id()
        material = self._current_material() or {}
        override = self._overrides.setdefault(
            material_id,
            MaterialOverride(
                target_material_id=material_id,
                target_material_name=str(material.get("name", "") or ""),
            ),
        )
        override.set_scalar("base_color", preset.model.base_color.tolist())
        override.set_scalar("metallic", float(preset.model.metallic))
        override.set_scalar("roughness", float(preset.model.roughness))
        override.set_scalar("opacity", float(preset.model.opacity))
        self._emit_overrides()
        self._refresh_current()

    def _reset_current(self) -> None:
        material_id = self.current_material_id()
        if not material_id:
            return
        self._overrides.pop(material_id, None)
        self._emit_overrides()
        self._refresh_current()

    def _emit_overrides(self) -> None:
        self.overrides_changed.emit(encode_material_overrides(self._overrides))
