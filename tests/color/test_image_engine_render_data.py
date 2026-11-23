"""Render-thread DTO coverage for ImageEngine layer state."""

from src.features.color.core.curves_soft_clip import set_soft_clip_control
from src.features.color.core.image_engine import ImageEngine, _layer_render_data
from src.features.color.core.layer import Layer


def test_layer_render_data_keeps_bake_and_ai_plugin_identity():
    layer = Layer("Frozen AI")
    layer.is_baked = True
    layer.plugins = [{
        "instance_id": "ai-instance",
        "plugin_id": "upscale",
        "type": "ai",
        "enabled": True,
        "params": {"scale": 2},
    }]

    data = _layer_render_data(layer)

    assert data["is_baked"] is True
    assert data["plugins"] == [{
        "instance_id": "ai-instance",
        "plugin_id": "upscale",
        "type": "ai",
        "enabled": True,
        "params": {"scale": 2},
    }]
    assert data["plugins"][0]["params"] is not layer.plugins[0]["params"]


def test_calibrated_soft_clip_is_frozen_for_preview_and_bake(image_engine: ImageEngine) -> None:
    layer = Layer()
    assert "curves_soft_clip" not in _layer_render_data(layer)
    layer.curves_soft_clip = set_soft_clip_control(
        {"calibration_version": 1}, "R", "low_soft", 0.1)

    preview = _layer_render_data(layer)
    bake = image_engine._create_layer_snapshot(layer)
    assert preview["curves_soft_clip"] == bake["curves_soft_clip"] == layer.curves_soft_clip

    layer.curves_soft_clip["channels"]["R"]["low_soft"] = 0.2
    assert preview["curves_soft_clip"]["channels"]["R"]["low_soft"] == 0.1
    assert bake["curves_soft_clip"]["channels"]["R"]["low_soft"] == 0.1
