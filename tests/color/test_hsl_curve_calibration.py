"""Calibrated HSL model, persistence, and rendering parity."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.features.color.core.advanced_grade_cpu import apply_hsl_curves_image
from src.features.color.core.hsl_curve_calibration import (
    calibrated_hsl_curve_samples,
    calibrated_hsl_input,
)
from src.features.color.core.layer import Layer, build_hsl_curve_lut_from_data
from src.features.color.core.shader_compiler import ShaderPipeline


def test_measured_hue_saturation_preserves_encoded_luma_and_hdr() -> None:
    encoded = np.array([[0.62, 0.32, 0.41], [-0.2, 1.4, 0.35]], dtype=np.float32)
    linear = np.copysign(np.abs(encoded) ** 2.4, encoded)
    data = {"HueSat": [(127.5, 255.0 - 1.3 * 127.5)]}
    lut = build_hsl_curve_lut_from_data(data, calibration_version=1)
    luma = encoded @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    expected = luma[:, None] + 1.3 * (encoded - luma[:, None])
    actual = apply_hsl_curves_image(linear, lut)
    np.testing.assert_allclose(
        np.copysign(np.abs(actual) ** (1.0 / 2.4), actual), expected, atol=2e-6,
    )


def test_calibrated_hsl_curve_round_trip_keeps_legacy_shape() -> None:
    curve = {"HueSat": [(0.0, 127.5), (82.0, 84.0), (255.0, 127.5)]}
    original = Layer("HSL")
    original.hsl_curve_data = curve
    legacy = Layer.from_dict(original.to_dict(None))
    assert legacy.hsl_curve_lut.shape == (6, 256)

    original.curves_edit["calibration_version"] = 1
    restored = Layer.from_dict(original.to_dict(None))
    expected = build_hsl_curve_lut_from_data(curve, calibration_version=1)
    assert expected.shape == (6, 512)
    np.testing.assert_array_equal(restored.hsl_curve_lut, expected)


def test_calibrated_picker_axes_and_inactive_curves() -> None:
    assert calibrated_hsl_input([0.6, 0.6, 0.6], "lum") == pytest.approx(0.6)
    assert calibrated_hsl_input([0.6, 0.6, 0.6], "sat") == pytest.approx(0.0, abs=1e-6)
    assert calibrated_hsl_input([0.1, 0.6, 0.1], "hue") == pytest.approx(1.0 / 3.0)
    lut = build_hsl_curve_lut_from_data({}, calibration_version=1)
    assert not ShaderPipeline.has_active_hsl_curve_lut(lut)


def test_calibrated_hue_curves_wrap_between_interior_points() -> None:
    samples = calibrated_hsl_curve_samples([(32.0, 90.0), (192.0, 127.5)], "HueSat")
    assert samples[0] == samples[-1]
    assert 1.0 < samples[0] < samples.max()


@pytest.mark.parametrize(("mode", "expected_codes"), [
    ("HueHue", [[60259, 19013, 5874], [38708, 13397, 63449], [49127, 33345, 9085]]),
    ("HueSat", [[62657, 17161, 17161], [31136, 15817, 61774], [28602, 40973, 0]]),
    ("HueLum", [[63539, 14388, 14388], [30289, 13905, 63056], [36974, 47213, 8302]]),
    ("LumSat", [[65535, 14637, 14637], [34457, 13320, 65535], [30336, 39327, 5159]]),
    ("SatSat", [[64982, 16533, 16533], [30532, 16271, 59054], [30332, 39331, 5134]]),
    ("SatLum", [[65151, 16000, 16000], [28843, 12459, 61610], [25462, 35701, 0]]),
])
def test_v3_hsl_matches_resolve_holdout(mode: str, expected_codes: list[list[int]]) -> None:
    source = np.array([[65535, 16384, 16384], [31744, 15360, 64511], [29696, 39935, 1024]],
                      dtype=np.float32) / 65535.0
    # Recorded normalized anchors from the Resolve SDR TIFF capture.
    controls = [(41, 187), (197, 107), (413, 180), (641, 133)]
    if not mode.startswith("Hue"):
        controls = [(0, 156.5), *controls, (697, 156.5)]
    lut = build_hsl_curve_lut_from_data(
        {mode: [(x * 255 / 697, y * 255 / 313) for x, y in controls]}, calibration_version=3)
    result = np.clip(apply_hsl_curves_image(source, lut, encoded_input=True), 0.0, 1.0)
    np.testing.assert_allclose(result * 65535, expected_codes, atol=42, rtol=0)


def test_script_uses_the_active_hsl_calibration_version() -> None:
    from src.features.scripting.api import ToposAPI

    layer = Layer("Recorded curves")
    layer.curves_edit["calibration_version"] = 3
    layer.hsl_curve_data = {"SatLum": [(10.0, 130.0)]}
    layer.hsl_curve_lut = build_hsl_curve_lut_from_data(layer.hsl_curve_data, calibration_version=3)
    api = ToposAPI.__new__(ToposAPI)
    api._engine = SimpleNamespace(
        get_active_layer=lambda: layer,
        apply_hsl_curve=lambda lut: setattr(layer, "hsl_curve_lut", lut),
    )
    api._bridge = SimpleNamespace(call_sync=lambda fn: fn())
    api._invalidate_video_render_params_if_loaded = lambda: None
    api.set_hsl_curve("HueSat", [(90.0, 100.0)])
    restored = Layer.from_dict(layer.to_dict())
    np.testing.assert_array_equal(layer.hsl_curve_lut, restored.hsl_curve_lut)
    assert layer.hsl_curve_lut.shape == (6, 1024)


def test_calibrated_hsl_graph_rebuilds_and_bakes_saved_controls() -> None:
    from src.features.color.core.advanced_grade_cpu import apply_curves_soft_clip_image
    from src.features.color.core.color_graph_cpu import apply_curves_cpu
    from src.features.color.core.color_graph_migration import migrate_legacy_stack
    from src.features.color.core.color_graph_service import ColorGraphService
    from src.features.color.core.curves_soft_clip import normalize_curves_soft_clip
    from src.features.color.core.graph_lut_export import bake_graph_lut
    from src.features.edit.core.project import EditProject
    from src.shared.color.lut_loader import parse_cube, sample_lut_trilinear

    layer = Layer("Calibrated HSL")
    layer.hsl_curve_data = {"HueSat": [(127.5, 100.0)]}
    layer.curves_edit["calibration_version"] = 1
    layer.curves_soft_clip = normalize_curves_soft_clip({"calibration_version": 1})
    layer.curves_data = {"M": [(0.0, 255.0), (127.5, 102.0), (255.0, 0.0)]}
    project, stack_id = EditProject.create(name="Curves").create_color_stack(
        asset_id="chart", name="HSL",
    )
    stack = project.color_stacks[stack_id].with_layers([layer.to_dict(None)])
    graph = migrate_legacy_stack(stack)
    node = next(item for item in graph.nodes if item.node_type == "corrector")
    soft_clip = normalize_curves_soft_clip({
        "calibration_version": 1,
        "channels": {"G": {"low_soft": 0.15, "high_clip": 0.8}},
    })
    hdr_curve = {"HueSat": [(110.0, 110.0)]}
    update = ColorGraphService().op_paste_selected_domains(
        graph, [node.node_id],
        {"hsl": hdr_curve, "curves_soft_clip": soft_clip}, ["hsl", "curves_soft_clip"],
    )
    assert update.ok
    cube = parse_cube(bake_graph_lut(update.graph, size=9))
    probe = np.array([[0.25, 0.5, 0.375]], dtype=np.float32)
    from src.features.color.core.layer import build_curves_lut_from_data

    mapped = apply_curves_cpu(probe, build_curves_lut_from_data(
        layer.curves_data, calibration_version=1))
    lut = build_hsl_curve_lut_from_data(hdr_curve, calibration_version=1)
    expected = apply_curves_soft_clip_image(apply_hsl_curves_image(mapped, lut), soft_clip)
    np.testing.assert_allclose(sample_lut_trilinear(cube, probe), expected, atol=2e-6)


@pytest.mark.parametrize("version", [1, 3])
def test_calibrated_hsl_curves_render_with_cpu_gl_parity(version: int) -> None:
    moderngl = pytest.importorskip("moderngl")
    from src.features.color.core.async_renderer import AsyncRenderer
    from src.features.color.core.corrector_render_service import run_serial_passes

    try:
        ctx = moderngl.create_standalone_context(require=330)
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"OpenGL context unavailable: {exc}")
    pipeline = ShaderPipeline(ctx)
    owner = SimpleNamespace(ctx=ctx, layer_textures={})
    try:
        data = {
            "LumSat": [(80.0, 116.0)],
            "HueHue": [(80.0, 114.0)],
            "HueSat": [(0.0, 127.5), (82.0, 84.0), (255.0, 127.5)],
            "HueLum": [(80.0, 107.0)],
            "SatSat": [(70.0, 140.0)],
            "SatLum": [(150.0, 110.0)],
        }
        lut = build_hsl_curve_lut_from_data(data, calibration_version=version)
        legacy = build_hsl_curve_lut_from_data(data)
        for table in (legacy, lut, legacy):
            AsyncRenderer._do_update_aux_tex(owner, {
                "type": "hsl_curve", "data": table.tobytes(), "layer_id": "single",
            })
            texture = owner.layer_textures["single"]["hsl_curve"]
            assert texture.size == table.shape
            np.testing.assert_array_equal(np.frombuffer(texture.read(), dtype=np.float32),
                                          table.T.ravel())
        source = np.random.default_rng(25).uniform(-0.1, 1.4, (32, 32, 4)).astype(np.float32)
        source[..., 3] = 1.0
        rgb_fp16 = source[..., :3].astype(np.float16).astype(np.float32)
        expected = apply_hsl_curves_image(rgb_fp16, lut)
        rendered = run_serial_passes(ctx, pipeline, source, [{"hsl_curve_lut": lut}])
        tolerance = np.maximum(2e-3, np.abs(np.spacing(expected.astype(np.float16))))
        np.testing.assert_array_less(np.abs(rendered[..., :3] - expected), tolerance)
        np.testing.assert_array_equal(rendered[..., 3], 1.0)
    finally:
        for textures in owner.layer_textures.values():
            textures["hsl_curve"].release()
        pipeline.release()
        ctx.release()
