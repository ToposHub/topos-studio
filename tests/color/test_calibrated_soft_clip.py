"""Rec.709 Gamma 2.4 Soft Clip against sampled Resolve TIFF ramps."""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.advanced_grade_cpu import apply_curves_soft_clip_image
from src.features.color.core.curves_soft_clip import (
    curves_soft_clip_is_identity,
    normalize_curves_soft_clip,
    set_soft_clip_control,
)
from src.features.color.core.layer import Layer


_INPUT_CODES = (0, 1025, 2050, 4100, 6150, 8200, 16400, 32800, 57399,
                59449, 61499, 63549, 65535)
_RAMP_CASES = [
    ("low", "RGB", {"low_clip": 0.075},
     (4915, 4915, 4915, 4915, 6119, 8167, 16359, 32743, 57318, 59366, 61414, 63462, 65446), 1),
    ("high", "RGB", {"high_clip": 0.925},
     (0, 994, 2018, 4066, 6114, 8162, 16354, 32738, 57313, 59361, 60561, 60561, 60561), 1),
    ("low-knee", "RGB", {"low_soft": 0.125},
     (2026, 2567, 3173, 4575, 6234, 8148, 16340, 32724, 57299, 59347, 61395, 63443, 65427), 1),
    ("high-knee", "RGB", {"high_soft": 0.125},
     (0, 988, 2012, 4060, 6108, 8156, 16348, 32732, 57307, 59231, 60900, 62312, 63437), 1),
    ("combined-low", "RGB", {"low_clip": 0.0305, "low_soft": 0.06},
     (2203, 2521, 2973, 4276, 6109, 8157, 16349, 32733, 57308, 59356, 61404, 63452, 65436), 26),
    ("four-controls", "RGB", {"low_clip": 0.05, "low_soft": 0.09, "high_clip": 0.94, "high_soft": 0.06},
     (3560, 3829, 4187, 5169, 6507, 8200, 16348, 32732, 57307, 59175, 60517, 61327, 61602), 1),
    ("red-only", "R", {"low_soft": 0.1},
     (1618, 2166, 2795, 4293, 6110, 8151, 16342, 32726, 57301, 59349, 61397, 63445, 65429), 1),
]


@pytest.mark.parametrize("case,channels,controls,reference,tolerance", _RAMP_CASES,
                         ids=[case[0] for case in _RAMP_CASES])
def test_soft_clip_calibrated_resolve_ramp(
    case: str, channels: str, controls: dict[str, float], reference: tuple[int, ...],
    tolerance: int,
) -> None:
    state = normalize_curves_soft_clip({
        "calibration_version": 1, "channels": {ch: controls for ch in channels},
    })
    encoded = np.asarray(_INPUT_CODES, dtype=np.float32) / 65535.0
    source = np.tile(encoded[:, None], (1, 3)) ** 2.4
    linear = apply_curves_soft_clip_image(source, state)
    actual_codes = np.clip(linear, 0.0, 1.0) ** (1.0 / 2.4) * 65535.0
    for ch_index, channel in enumerate("RGB"):
        if channel in channels:
            np.testing.assert_allclose(actual_codes[:, ch_index], reference,
                                       atol=tolerance, rtol=0.0, err_msg=case)
        else:
            np.testing.assert_array_equal(linear[:, ch_index], source[:, ch_index])


def test_calibrated_soft_clip_state_survives_project_round_trip() -> None:
    identity = normalize_curves_soft_clip({"calibration_version": 1})
    source = np.asarray([[-0.25, 2.0, -0.05]], dtype=np.float32)
    assert curves_soft_clip_is_identity(identity)
    np.testing.assert_array_equal(apply_curves_soft_clip_image(source, identity), source)

    layer = Layer()
    layer.curves_soft_clip = set_soft_clip_control(identity, "R", "low_soft", 0.125)
    restored = Layer.from_dict(layer.to_dict(None))
    assert restored.curves_soft_clip == layer.curves_soft_clip
    assert restored.curves_soft_clip["calibration_version"] == 1
    graded = apply_curves_soft_clip_image(source, restored.curves_soft_clip)
    assert graded[0, 0] == 0.0
    np.testing.assert_array_equal(graded[:, 1:], source[:, 1:])


@pytest.mark.parametrize("channels,controls,expected", [
    ("RGB", (0.25, 0.5, 0.75, 0.5),
     (16983, 17104, 17239, 17558, 17948, 18416, 21210, 32704, 47071, 47547, 47944, 48269, 48530)),
    ("RGB", (0.1875, 0.5, 0.8125, 0.4375),
     (14183, 14417, 14667, 15221, 15849, 16556, 20253, 32698, 49101, 49832, 50482, 51052, 51549)),
    ("RGB", (-0.125, 0.4375, 0.8125, 0.0),
     (3606, 4272, 4956, 6379, 7876, 9446, 16456, 32688, 53159, 53159, 53159, 53159, 53159)),
    ("G", (0.1875, 0.5, 0.875, 0.4375),
     (14183, 14416, 14666, 15220, 15848, 16555, 20252, 32696, 50700, 51651, 52523, 53317, 54035)),
], ids=["overlap", "asymmetric-overlap", "hard-high-with-low-soft", "green-only"])
def test_independent_soft_clip_matches_resolve_ramp(
    channels: str, controls: tuple[float, ...], expected: tuple[int, ...],
) -> None:
    keys = ("low_clip", "low_soft", "high_clip", "high_soft")
    raw = dict(zip(keys, controls))
    state = normalize_curves_soft_clip({
        "calibration_version": 2, "channels": {ch: raw for ch in channels},
    })
    assert all(state["channels"][ch] == raw for ch in channels)
    source = np.repeat(np.array((0, 1025, 2050, 4100, 6150, 8200, 16400,
                                 32800, 57335, 59385, 61435, 63485, 65535),
                                dtype=np.float32)[:, None] / 65535.0, 3, axis=1)
    result = apply_curves_soft_clip_image(source, state, encoded_input=True)
    for idx, channel in enumerate("RGB"):
        if channel in channels:
            np.testing.assert_allclose(result[:, idx] * 65535.0, expected, atol=1, rtol=0)
        else:
            np.testing.assert_array_equal(result[:, idx], source[:, idx])


def test_calibrated_gl_soft_clip_matches_cpu_and_refreshes_version() -> None:
    moderngl = pytest.importorskip("moderngl")

    from src.features.color.core.corrector_render_service import run_serial_passes
    from src.features.color.core.shader_compiler import ShaderPipeline

    controls = {
        "R": {"low_clip": 0.05, "low_soft": 0.15},
        "B": {"high_clip": 0.92, "high_soft": 0.075},
    }
    legacy = normalize_curves_soft_clip({"channels": controls})
    calibrated = {**legacy, "calibration_version": 1}
    independent = normalize_curves_soft_clip({"calibration_version": 2, "channels": {
        "R": {"low_clip": 0.25, "low_soft": 0.5, "high_clip": 0.75, "high_soft": 0.5},
        "B": {"high_clip": 0.875, "low_soft": 0.4},
    }})
    source = np.asarray([[[-0.05, 1.3, 0.90, 0.25],
                          [0.00, -0.05, 1.15, 0.50],
                          [0.10, 0.50, 0.75, 0.75]]], dtype=np.float16)
    try:
        ctx = moderngl.create_standalone_context(require=330)
    except RuntimeError as exc:
        pytest.skip(f"No standalone OpenGL context: {exc}")
    pipeline = ShaderPipeline(ctx)
    try:
        for state in (independent, calibrated, legacy, independent):
            expected = apply_curves_soft_clip_image(source[..., :3], state)
            actual = run_serial_passes(ctx, pipeline, source, [{"curves_soft_clip": state}])
            np.testing.assert_allclose(actual[..., :3], expected, atol=5e-4, rtol=0.0)
            np.testing.assert_array_equal(actual[..., 3], source[..., 3])
    finally:
        pipeline.release()
        ctx.release()
