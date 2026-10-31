"""Render-thread DTO coverage for ImageEngine layer state."""

from src.features.color.core.image_engine import _layer_render_data
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
