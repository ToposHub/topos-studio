from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.features.color.core.color_graph_cpu import apply_curves_cpu
from src.features.color.core.color_graph_migration import stack_primary_execution_graph
from src.features.color.core.color_graph_param_source import node_layer_payload
from src.features.color.core.curves_calibration import is_calibrated_curves_lut
from src.features.color.core.layer import Layer, build_curves_lut_from_data
from src.features.color.core.shader_compiler import ShaderPipeline
from src.shared.project.color_graph import NodeType
from src.shared.project.color_stack import ColorStackData


_MIDPOINT = [(0, 255), (126.89, 75.72), (255, 0)]


def _curve_state() -> dict:
    return {"calibration_version": 1, "amounts": {"Y": 50.0, "R": 100.0}}


def test_yrgb_uses_encoded_luma_for_y_and_rebalances_rgb() -> None:
    rgb = np.array([[0.5, 0.25, 0.125], [0.125, 0.5, 0.625]], dtype=np.float32)
    linear = rgb ** 2.4
    weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

    y_curve = build_curves_lut_from_data({"M": _MIDPOINT}, calibration_version=1)
    lifted = apply_curves_cpu(linear, y_curve) ** (1.0 / 2.4)
    np.testing.assert_allclose(lifted[0], [0.653, 0.403, 0.278], atol=0.007)
    shift = lifted - rgb
    np.testing.assert_allclose(shift, np.broadcast_to(shift[:, :1], shift.shape), atol=1.0e-6)

    red_curve = build_curves_lut_from_data({"R": _MIDPOINT}, calibration_version=1)
    recolored = apply_curves_cpu(linear, red_curve) ** (1.0 / 2.4)
    assert np.all(recolored[:, 0] > rgb[:, 0])
    assert np.all(recolored[:, 1:] < rgb[:, 1:])
    np.testing.assert_allclose(recolored @ weights, rgb @ weights, atol=1.0e-6)

    y_half = build_curves_lut_from_data(
        {"M": _MIDPOINT}, {"Y": 50.0}, calibration_version=1)
    halfway = apply_curves_cpu(linear, y_half) ** (1.0 / 2.4)
    np.testing.assert_allclose(halfway, (lifted + rgb) * 0.5, atol=1.0e-6)


@pytest.mark.parametrize("version", (1, 2))
def test_calibrated_identity_preserves_negative_and_superwhite(version: int) -> None:
    neutral = [(0, 255), (128, 127), (255, 0)]
    lut = build_curves_lut_from_data(
        {"M": neutral, "R": neutral, "G": neutral, "B": neutral},
        calibration_version=version,
    )
    rgb = np.array([[-0.2, 0.4, 3.5]], dtype=np.float32)
    assert is_calibrated_curves_lut(lut)
    assert not ShaderPipeline.has_active_curves_lut(lut)
    np.testing.assert_array_equal(apply_curves_cpu(rgb, lut), rgb)


@pytest.mark.parametrize("version", (1, 2))
def test_calibrated_curve_state_rebuilds_in_layer_and_legacy_graph(version: int) -> None:
    data = {"M": _MIDPOINT, "R": _MIDPOINT}
    state = {**_curve_state(), "calibration_version": version}
    layer = Layer.from_dict({"name": "Measured", "curves_data": data, "curves_edit": state})
    restored = Layer.from_dict(layer.to_dict(None))
    reference = build_curves_lut_from_data(data, state["amounts"], calibration_version=version)
    np.testing.assert_array_equal(restored.curves_lut, reference)
    assert restored.curves_edit["calibration_version"] == version

    stack = ColorStackData.create().with_curves(data).with_curves_edit(state)
    graph = stack_primary_execution_graph(stack)
    assert graph is not None
    node = next(
        node for node in graph.nodes if node.node_type == NodeType.CORRECTOR.value)
    payload = node_layer_payload(node)
    assert payload is not None
    np.testing.assert_array_equal(payload["curves_lut"], reference)


def test_v2_luma_s_matches_held_out_resolve_ramp() -> None:
    # Resolve 20.2.1.6, 443 × 337 plot with two anchors at (109, 51)
    # and (334, 292) relative to the black point (output increases upward).
    points = [(0, 255), (255 * 109 / 443, 255 * (1 - 51 / 337)),
              (255 * 334 / 443, 255 * (1 - 292 / 337)), (255, 0)]
    signal = np.array([0, 8200, 16400, 24600, 32800, 40999, 49199, 57399, 65535],
                      dtype=np.float32) / 65535
    expected = np.array([0, 4364, 10200, 20598, 32906, 45394, 56247, 62910, 65535])
    lut = build_curves_lut_from_data({"M": points}, calibration_version=2)
    actual = apply_curves_cpu(np.repeat(signal[:, None], 3, axis=1), lut, encoded_input=True)
    np.testing.assert_allclose(np.clip(actual[:, 0], 0.0, 1.0) * 65535, expected, atol=100, rtol=0)


@pytest.mark.parametrize(("black_x", "white_x"), [(0.18, 1.0), (0.0, 0.82)])
def test_v2_linked_horizontal_endpoints_extrapolate_before_compensation(
    black_x: float, white_x: float,
) -> None:
    points = [(black_x * 255, 255), (white_x * 255, 0)]
    rgb = np.array([[0.04, 0.5, 0.92], [0.7, 0.85, 1.0]], dtype=np.float32)
    lut = build_curves_lut_from_data(dict.fromkeys("MRGB", points), calibration_version=2)
    actual = apply_curves_cpu(rgb, lut, encoded_input=True)
    expected = rgb + ((rgb * (31.0 / 32.0) - 1.0 / 2048.0 - black_x)
                      / (white_x - black_x) - rgb * (31.0 / 32.0))
    np.testing.assert_allclose(actual, expected, atol=3e-7, rtol=0)


@pytest.mark.parametrize("version", (1, 2))
def test_calibrated_curve_gl_and_texture_resize_match_cpu(version: int) -> None:
    moderngl = pytest.importorskip("moderngl")
    try:
        ctx = moderngl.create_standalone_context(require=330)
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"OpenGL context unavailable: {exc}")

    from src.features.color.core.async_renderer import AsyncRenderer
    from src.features.color.core.corrector_render_service import run_serial_passes

    pipeline = ShaderPipeline(ctx)
    owner = SimpleNamespace(ctx=ctx, layer_textures={})
    legacy = build_curves_lut_from_data({"M": _MIDPOINT})
    lut = build_curves_lut_from_data({"M": _MIDPOINT, "R": _MIDPOINT}, calibration_version=version)
    rgba = np.array([[
        [0.5, 0.15, 0.01, 0.25], [0.01, 0.6, 0.3, 0.75], [-0.05, 0.6, 1.4, 0.5],
    ]], dtype=np.float16)
    try:
        for table in (legacy, lut, legacy):
            AsyncRenderer._do_update_aux_tex(owner, {
                "type": "curves", "data": table.tobytes(), "layer_id": "existing",
            })
            assert owner.layer_textures["existing"]["curves"].size == (len(table), 1)
        actual = run_serial_passes(ctx, pipeline, rgba, [{"curves_lut": lut}])
        expected = apply_curves_cpu(rgba[..., :3].astype(np.float32), lut)
        tolerance = np.maximum(8.0e-4, np.abs(np.spacing(expected.astype(np.float16))))
        np.testing.assert_array_less(np.abs(actual[..., :3] - expected), tolerance)
        np.testing.assert_array_equal(actual[..., 3], rgba[..., 3])
    finally:
        curve_tex = owner.layer_textures.get("existing", {}).get("curves")
        if curve_tex is not None:
            curve_tex.release()
        pipeline.release()
        ctx.release()
