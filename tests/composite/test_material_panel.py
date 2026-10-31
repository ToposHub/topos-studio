"""UI handoff tests for the P1-23 Model3D material panel."""

from __future__ import annotations

import os
import json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.composite.ui.material_panel import MaterialPanel


def _material_summary() -> dict:
    return {
        "material_id": "body",
        "name": "Body",
        "mesh_count": 2,
        "base_color": [0.2, 0.3, 0.4],
        "metallic": 0.1,
        "roughness": 0.8,
        "opacity": 1.0,
        "slots": {
            "base_color": {
                "path": "body.png", "color_space": "srgb", "missing": True,
            },
            "normal": {"path": "", "color_space": "linear", "missing": False},
        },
    }


def test_material_panel_edits_and_resets_one_material(qapp) -> None:
    panel = MaterialPanel()
    emitted: list[str] = []
    panel.overrides_changed.connect(emitted.append)
    panel.set_materials([_material_summary()])

    assert panel.current_material_id() == "body"
    assert "body.png" in panel._slot_labels["base_color"].text()
    assert "⚠" in panel._slot_labels["base_color"].text()

    panel._metallic.setValue(0.9)
    assert emitted
    assert '"metallic":0.9' in emitted[-1]

    panel._reset_button.click()
    assert emitted[-1] == "{}"
    assert panel._metallic.value() == 0.1


def test_material_panel_restores_serialized_override(qapp) -> None:
    panel = MaterialPanel()
    panel.set_materials(
        [_material_summary()],
        '{"body":{"target_material_id":"body","overrides":{"roughness":0.2}}}',
    )
    assert panel._roughness.value() == 0.2


def test_material_panel_replaces_texture_slot_and_color_space(qapp, tmp_path) -> None:
    panel = MaterialPanel()
    emitted: list[str] = []
    panel.overrides_changed.connect(emitted.append)
    panel.set_materials([_material_summary()])

    replacement = tmp_path / "replacement.exr"
    panel._slot_edits["base_color"].setText(str(replacement))
    panel._slot_color_spaces["base_color"].setCurrentIndex(2)
    panel._slot_edits["base_color"].editingFinished.emit()

    assert emitted
    document = json.loads(emitted[-1])
    slot = document["body"]["slot_overrides"]["base_color_slot"]
    assert slot["path"] == str(replacement)
    assert slot["color_space"] == "raw"
    assert panel._slot_edits["base_color"].text() == str(replacement)

    panel._reset_button.click()
    assert emitted[-1] == "{}"
    assert panel._slot_edits["base_color"].text() == "body.png"
