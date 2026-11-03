"""Usage markers reflect effective parameters, never merely nonempty dictionaries."""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.layer import Layer
from src.features.color.ui.node_tool_indicators import detect_tool_indicators


def test_fresh_layer_and_serialized_neutral_defaults_have_no_markers() -> None:
    layer = Layer()
    assert detect_tool_indicators(layer.to_dict(None)) == ()
    assert detect_tool_indicators({}, {"grade": layer.grading_params}) == ()
    assert detect_tool_indicators(None) == ()


def test_grade_override_reset_and_identity_ndarrays() -> None:
    payload = Layer().to_dict(None)
    payload["grading_params"]["exposure"] = 3.0
    assert detect_tool_indicators(payload) == ("primary",)
    assert detect_tool_indicators(payload, {"grade": {"exposure": 0.0}}) == ()
    payload["grading_params"] = {"lift": np.zeros(3)}
    payload["warper_params"] = np.zeros((8, 8, 2))
    payload["mixer_params"] = {"red": np.zeros(3)}
    assert detect_tool_indicators(payload) == ()
    payload["warper_params"][1, 1, 0] = 0.1
    assert detect_tool_indicators(payload) == ("color_mapping",)


def test_curves_use_screen_coordinates_and_hsl_neutral_center() -> None:
    payload = {"curves_data": {"M": [(0, 255), (127.5, 127.5), (255, 0)]},
               "hsl_curve_data": {"HueSat": [(0, 127.5), (255, 127.5)]}}
    assert detect_tool_indicators(payload) == ()
    payload["curves_data"]["M"][1] = (127.5, 90)
    assert detect_tool_indicators(payload) == ("curves",)


@pytest.mark.parametrize(("payload", "expected"), [
    ({"qualifier_params": {"enabled": True}}, "qualifier"),
    ({"mask_shapes": [{"type": "circle", "visible": True}]}, "window"),
    ({"effects_params": {"enabled": True, "sharpen": 2}}, "blur"),
    ({"effects_params": {"blur_enabled": True, "blur_intensity": 20}}, "blur"),
    ({"active_lut_path": "test.cube", "lut_intensity": 1}, "lut"),
    ({"plugins": [{"plugin_id": "test", "enabled": True}]}, "ofx"),
    ({"opacity": 0.5}, "key"),
    ({"grade_keyframes": {"exposure": {"keys": []}}}, "keyframes"),
])
def test_supported_layer_tools_have_distinct_markers(payload: dict, expected: str) -> None:
    assert detect_tool_indicators(payload) == (expected,)


def test_disabled_or_empty_tools_do_not_report_usage() -> None:
    assert detect_tool_indicators({
        "qualifier_params": {"enabled": False, "hue_center": 0.5},
        "effects_params": {"enabled": True, "mode": 3, "iterations": 4},
        "plugins": [{"plugin_id": "test", "enabled": False}],
        "active_lut_path": "test.cube", "lut_intensity": 0,
        "mask_shapes": [{"type": "circle", "visible": False}],
        "id": "has-metadata", "name": "Renamed", "is_dirty": True,
    }) == ()


def test_footer_tooltip_keeps_complete_tool_list_when_source_preview_replaces_text(qapp) -> None:
    from src.features.color.ui.node_editor_items import NodeCardItem
    from src.features.color.ui.node_editor_state import NodeDraft, NodeKind, node_ports
    node = NodeDraft("test", NodeKind.CORRECTOR, tool_indicators=("primary", "curves", "qualifier", "window", "lut"))
    card = NodeCardItem(node, node_ports(node))
    card.setToolTip("Source preview")
    assert "Primary correction" in card.toolTip()
    assert "Power Window" in card.toolTip()
    assert "LUT" in card.toolTip()
    card.setToolTip(card.toolTip())
    assert card.toolTip().count("Applied tools") == 1
