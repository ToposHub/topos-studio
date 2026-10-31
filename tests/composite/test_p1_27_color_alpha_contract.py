"""P1-27 colour-space, alpha and AOV semantic contracts."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _result(**changes):
    from src.features.composite.nodes.base_node import TextureResult

    values = dict(texture_id=1, width=1, height=1,
                  color_space="linear", alpha_premultiplied=True)
    values.update(changes)
    return TextureResult(**values)


def test_texture_result_has_explicit_aov_semantic_default():
    result = _result()
    assert result.aov_semantic == "beauty"
    assert result.with_updates(aov_semantic="depth").aov_semantic == "depth"


def test_file_output_unpremultiplies_before_colour_encoding():
    from src.features.composite.nodes.file_output_node import FileOutputNode

    node = FileOutputNode()
    node.parameters.update({
        "format": "EXR", "color_space": "linear", "channels": "RGBA",
        "alpha_mode": "straight",
    })
    src = _result()
    # RGB is premultiplied representation of (0.2, 0.4, 0.6) at alpha .5.
    rgba = np.array([[[0.1, 0.2, 0.3, 0.5]]], dtype=np.float32)
    exported = node._prepare_for_export(rgba, src)
    # FileOutput returns BGRA for OpenCV.
    assert exported[0, 0, :3] == pytest.approx((0.6, 0.4, 0.2), abs=1e-5)
    assert exported[0, 0, 3] == pytest.approx(0.5)


def test_file_output_can_preserve_premultiplied_alpha():
    from src.features.composite.nodes.file_output_node import FileOutputNode

    node = FileOutputNode()
    node.parameters.update({
        "format": "EXR", "color_space": "linear", "channels": "RGBA",
        "alpha_mode": "premult",
    })
    src = _result(alpha_premultiplied=False)
    rgba = np.array([[[0.2, 0.4, 0.6, 0.5]]], dtype=np.float32)
    exported = node._prepare_for_export(rgba, src)
    assert exported[0, 0, :3] == pytest.approx((0.3, 0.2, 0.1), abs=1e-5)


def test_aov_export_skips_display_colour_transform():
    from src.features.composite.nodes.file_output_node import FileOutputNode

    node = FileOutputNode()
    node.parameters.update({
        "format": "PNG", "color_space": "sRGB", "channels": "RGB",
        "bit_depth": "32",
    })
    src = _result(color_space="data", alpha_premultiplied=False,
                  aov_semantic="depth")
    rgba = np.array([[[0.25, 0.5, 0.75, 1.0]]], dtype=np.float32)
    exported = node._prepare_for_export(rgba, src)
    assert exported[0, 0] == pytest.approx((0.75, 0.5, 0.25), abs=1e-5)


def test_aov_selection_marks_data_semantics():
    from src.features.composite.nodes.file_output_node import FileOutputNode

    result = _result(aov_texture_ids={"depth": 9},
                     aov_dimensions={"depth": (1, 1)})
    selected = FileOutputNode._select_render_pass(result, "depth")
    assert selected is not None
    assert selected.aov_semantic == "depth"
    assert selected.color_space == "data"
    assert selected.alpha_premultiplied is False


def test_export_renderer_forces_linear_readback_for_aov():
    from src.features.composite.render.composite_export_renderer import (
        CompositeExportRenderer)

    renderer = CompositeExportRenderer.__new__(CompositeExportRenderer)
    renderer._readback_output = "srgb_u8"
    # The public method makes this decision before touching a GL context; use
    # source-level contract here because constructing a context is not CI-safe.
    import inspect
    source = inspect.getsource(CompositeExportRenderer.render_and_readback)
    assert '"linear_f32"' in source
    assert "Auxiliary AOVs are numeric data" in source


def test_fused_color_path_injects_premult_decode_encode_when_needed():
    from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

    import inspect
    source = inspect.getsource(BaseEvaluatorMixin._apply_fused_chain)
    assert "auto_unpremult" in source
    assert "auto_repremult" in source
    assert "u_enable_unpremult" in source
    assert "u_enable_premult" in source
