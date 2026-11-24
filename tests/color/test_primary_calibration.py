from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.features.color.core.primary_calibration import PrimaryCalibration, calibrated_grade_inputs
from src.features.color.core.color_graph_cpu import apply_layer_grading_cpu
from src.shared.color.lut_loader import LoadedLut, sample_lut_trilinear


def _write_profile(path: Path, **overrides: np.ndarray) -> dict[str, np.ndarray]:
    b, g, r = np.indices((3, 3, 3), dtype=np.float32) / 2.0
    tables = np.zeros((3, 3, 3, 3, 3), dtype=np.float16)
    tables[0, ..., 0] = (1.0 - r) / 4.0
    tables[1, ..., 0] = -(1.0 - r) / 8.0
    tables[2, ..., 1] = (b - g) / 8.0
    records = {
        "schema_version": np.asarray(1),
        "control_names": np.array(["lift", "lift", "vibrance"]),
        "channel_names": np.array(["r", "r", "value"]),
        "knots": np.array([2.0, -1.0, 20.0], dtype=np.float64),
        "deltas": tables,
    }
    records.update(overrides)
    np.savez_compressed(path, **records)
    return records


def test_combined_grid_matches_independent_samples_and_caches(tmp_path: Path) -> None:
    path = tmp_path / "profile.npz"
    records = _write_profile(path)
    profile = PrimaryCalibration.from_npz(path)
    controls = {("lift", "r"): 1.0, ("vibrance", "value"): 5.0}
    assert profile.supported_controls == frozenset(controls)
    assert profile.measured_interval(("lift", "r")) == (-1.0, 2.0)

    grid = profile.build_grid(controls, extrapolation="error")
    assert grid.dtype == np.float32
    assert grid is profile.build_grid(dict(reversed(tuple(controls.items()))), extrapolation="error")
    with pytest.raises(ValueError, match="WRITEABLE"):
        grid.setflags(write=True)
    np.testing.assert_array_equal(grid, records["deltas"][0] * 0.5 + records["deltas"][2] * 0.25)

    rgb = np.array([[0.16, 0.29, 0.81], [0.72, 0.85, 0.41]], dtype=np.float32)
    expected = rgb.copy()
    for table, weight in zip(records["deltas"][[0, 2]], (0.5, 0.25)):
        expected += weight * sample_lut_trilinear(LoadedLut(table, 3, "cube"), rgb)
    np.testing.assert_allclose(
        profile.apply_encoded(rgb, controls, extrapolation="error"), expected, atol=1e-7,
    )
    np.testing.assert_allclose(
        expected, rgb + np.stack(((1.0 - rgb[:, 0]) / 8.0,
                                  (rgb[:, 2] - rgb[:, 1]) / 32.0,
                                  np.zeros(len(rgb))), axis=-1), atol=1e-7,
    )


def test_parameter_extrapolation_and_unavailable_controls(tmp_path: Path) -> None:
    path = tmp_path / "profile.npz"
    _write_profile(path)
    profile = PrimaryCalibration.from_npz(path)
    sampled = profile.build_grid({("lift", "r"): 2.0})
    np.testing.assert_array_equal(profile.build_grid({("lift", "r"): 3.0}), sampled)
    np.testing.assert_array_equal(
        profile.build_grid({("lift", "r"): 3.0}, extrapolation="linear"), sampled * 1.5,
    )
    np.testing.assert_array_equal(profile.build_grid({("vibrance", "value"): -10.0}), 0.0)
    np.testing.assert_array_equal(
        profile.build_grid({("vibrance", "value"): -10.0}, extrapolation="linear"),
        -profile.build_grid({("vibrance", "value"): 10.0}),
    )
    with pytest.raises(ValueError, match="outside the measured interval"):
        profile.build_grid({("lift", "r"): 3.0}, extrapolation="error")
    with pytest.raises(ValueError, match="Unsupported primary calibration control"):
        profile.build_grid({("contrast", "value"): 1.0})
    with pytest.raises(ValueError, match="finite numeric delta"):
        profile.build_grid({("lift", "r"): np.nan})


def test_linear_rec709_neutral_is_exact_and_signed_hdr_extends_boundary(tmp_path: Path) -> None:
    path = tmp_path / "profile.npz"
    _write_profile(path)
    profile = PrimaryCalibration.from_npz(path)
    rgb = np.array([[-0.2, 0.25, 1.4], [0.12, 0.73, 0.91]], dtype=np.float32)
    np.testing.assert_array_equal(profile.apply_linear(rgb, {}), rgb)
    np.testing.assert_array_equal(profile.apply_linear(rgb, {("missing", "value"): 0.0}), rgb)
    sdr = np.clip(rgb, 0.0, 1.0)
    expected_code = sdr ** (1.0 / 2.4)
    expected_code[:, 0] += (1.0 - expected_code[:, 0]) * 0.25
    expected_code[:, 1] += (expected_code[:, 2] - expected_code[:, 1]) * 0.125
    calibrated = profile.apply_linear(rgb, {("lift", "r"): 2.0, ("vibrance", "value"): 20.0})
    np.testing.assert_allclose(calibrated, np.clip(expected_code, 0.0, 1.0) ** 2.4 + rgb - sdr, atol=1e-7)
    assert calibrated[0, 0] < 0.0
    assert calibrated[0, 2] > 1.0


def test_npz_duplicate_measurements_and_wrong_schema_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "profile.npz"
    _write_profile(path, knots=np.array([2.0, 2.0, 20.0]))
    with pytest.raises(ValueError, match="Duplicate primary calibration knot"):
        PrimaryCalibration.from_npz(path)
    _write_profile(path, schema_version=np.asarray(5))
    with pytest.raises(ValueError, match="Unsupported primary calibration schema"):
        PrimaryCalibration.from_npz(path)


def test_lum_mix_preserves_input_luma_after_measured_gain() -> None:
    src = np.array([[0.23, 0.54, 0.43], [0.17, 0.24, 0.32]], dtype=np.float32)
    brightness = np.sqrt(np.log2(1.1) / 6.0)
    grade = {"primary_calibration_version": 1, "gain": (0, 0, brightness)}
    boosted, _ = apply_layer_grading_cpu(src, {"grading_params": grade}, width=2, height=1)
    restored, _ = apply_layer_grading_cpu(src, {"grading_params": {**grade, "lum_mix": 0.0}}, width=2, height=1)
    luma = np.array([0.299, 0.587, 0.114], dtype=np.float32)
    assert (boosted @ luma > src @ luma).all()
    np.testing.assert_allclose(restored @ luma, src @ luma, atol=1e-6)


@pytest.mark.parametrize(("params", "codes", "tolerance"), [
    ({"hue_shift": -86.4}, [[41598, 45142, 65535], [26342, 36838, 51601], [52531, 14053, 0]], 2),
    ({"contrast": 11.0, "pivot": 0.245},
     [[12907, 60470, 46608], [17810, 43132, 26879], [46476, 6847, 50637]], 70),
])
def test_refined_primary_matches_independent_resolve_patches(
    params: dict[str, float], codes: list[list[int]], tolerance: int,
) -> None:
    src = np.asarray([[13312, 58367, 44031], [19456, 41983, 27648], [46079, 9216, 50175]],
                     dtype=np.float32) / 65535.0
    grade = {"grading_params": {"primary_calibration_version": 2, **params}}
    result, _ = apply_layer_grading_cpu(src**2.4, grade, width=3, height=1)
    np.testing.assert_allclose(result**(1.0 / 2.4) * 65535.0, codes, rtol=0.0, atol=tolerance)
    grid, _ = calibrated_grade_inputs(grade)
    assert grid is not None and not grid.flags.writeable
    assert calibrated_grade_inputs(grade)[0] is grid


@pytest.mark.parametrize("params", [
    {"temp": 1e308}, {"hue_shift": float("nan")}, {"mid_detail": float("inf")},
    {"contrast": 15.0, "pivot": float("inf")},
])
def test_refined_primary_rejects_invalid_lattice_inputs(params: dict[str, float]) -> None:
    grade = {"grading_params": {"primary_calibration_version": 2, **params}}
    with pytest.raises(ValueError, match="Primary calibration"):
        calibrated_grade_inputs(grade)
