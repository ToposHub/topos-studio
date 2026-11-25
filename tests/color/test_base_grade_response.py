"""Invariants of Base Grade's exposure and tone zones on linear input."""

import numpy as np
import pytest

from src.features.color.core.base_grade import (
    BASE_GRADE_DEFAULTS,
    apply_base_grade_linear,
    base_grade_active,
    base_grade_curve,
)


def test_base_grade_neutral_and_pivot_only_bypass_exactly() -> None:
    rgb = np.array([[-0.001, 0.1, 4.0], [0.0, 0.0, 0.0]], dtype=np.float32)
    assert apply_base_grade_linear(rgb, BASE_GRADE_DEFAULTS) is rgb
    shape = {"base_grade_dim_pivot": -3.0, "base_grade_dim_falloff": 0.0}
    assert not base_grade_active(shape)
    assert apply_base_grade_linear(rgb, shape) is rgb


def test_base_grade_balance_uses_stops_and_keeps_hdr_and_signed_channels() -> None:
    rgb = np.array([[-0.03, 0.2, 1.45], [0.0, 0.0, 0.0]], dtype=np.float32)
    output = apply_base_grade_linear(rgb, {"base_grade_balance_exposure": 2.0})
    np.testing.assert_array_equal(output, 4.0 * rgb)
    assert output.dtype == rgb.dtype


def test_response_trace_uses_the_same_global_and_upstream_zone_transforms() -> None:
    ramp = np.geomspace(0.003, 3.0, 64, dtype=np.float32)
    global_params = {"base_grade_contrast": 1.4, "base_grade_contrast_pivot": 0.3,
                     "base_grade_balance_exposure": 0.4, "base_grade_flare": 0.01}
    prior_zones = {**global_params, "base_grade_bright_exposure": 0.7, "base_grade_light_exposure": -0.4}
    params = {**prior_zones, "base_grade_dim_exposure": 0.6}
    stages: dict[str, np.ndarray] = {}
    output = base_grade_curve(ramp, params, stage_levels=stages)

    np.testing.assert_array_equal(output, base_grade_curve(ramp, params))
    np.testing.assert_allclose(stages["global"], base_grade_curve(ramp, global_params), rtol=3e-7)
    np.testing.assert_allclose(stages["bright"], stages["global"], rtol=3e-7)
    np.testing.assert_allclose(stages["dim"], base_grade_curve(ramp, prior_zones), rtol=3e-7)


@pytest.mark.parametrize("zone,pivot", [("dark", -1.0), ("dim", 1.0), ("light", -1.0), ("bright", 2.0)])
def test_base_grade_zone_stays_on_its_side_of_the_pivot(zone: str, pivot: float) -> None:
    split = 0.18 * 2**pivot
    ramp = np.array([split * 0.125, split * 0.5, split * 2, split * 8], dtype=np.float32)
    output = base_grade_curve(ramp, {f"base_grade_{zone}_exposure": 1.0})
    changed = slice(0, 2) if zone in ("dark", "dim") else slice(2, 4)
    bypassed = slice(2, 4) if zone in ("dark", "dim") else slice(0, 2)
    np.testing.assert_allclose(output[bypassed], ramp[bypassed], rtol=1e-6)
    assert np.all(output[changed] > ramp[changed])


@pytest.mark.parametrize("zone", ["dim", "light"])
@pytest.mark.parametrize("falloff", [0.0, 0.5, 1.0])
def test_base_grade_zone_exposure_keeps_neutral_ramp_monotonic(zone: str, falloff: float) -> None:
    ramp = np.geomspace(0.000001, 20.0, 8192)
    for stops in (-3.0, -1.0, 1.0, 2.0, 3.0):
        params = {f"base_grade_{zone}_falloff": falloff, f"base_grade_{zone}_exposure": stops}
        assert np.all(np.diff(base_grade_curve(ramp, params)) > 0)
