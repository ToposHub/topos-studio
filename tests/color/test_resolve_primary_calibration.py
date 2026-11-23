from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.shared.color.lut_loader import load_lut, sample_lut_trilinear
from tools import resolve_primary_calibration as cal


def test_chart_round_trip_and_independent_samples(tmp_path: Path) -> None:
    chart = tmp_path / cal.CHART_NAME
    cal.write_chart(chart)
    codes = cal.chart_codes()
    np.testing.assert_array_equal(cal.read_patches(chart), codes)
    grid, holdout, gray = np.split(codes, (cal.GRID**3, cal.GRID**3 + cal.HOLDOUT))
    assert len(set(map(tuple, holdout))) == cal.HOLDOUT
    assert not set(map(tuple, grid)).intersection(map(tuple, holdout))
    np.testing.assert_array_equal(gray[:, 0], gray[:, 1])
    np.testing.assert_array_equal(gray[:, 0], gray[:, 2])


def test_edit_validation_rejects_duplicate_offset_across_modes() -> None:
    with pytest.raises(ValueError, match="Repeat edit"):
        cal.parse_edits([["bars", "offset", "r", 26], ["log", "log_offset", "r", 27]])
    with pytest.raises(ValueError, match="Invalid edit"):
        cal.parse_edits([["log", "log_shadow", "y", 0.1]])


def test_independent_fit_validates_combinations_and_bakes_linear_lut(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    version = "test-build"
    (tmp_path / "session.json").write_text(json.dumps({
        "settings": cal.PROJECT_SETTINGS, "resolve_version": version,
    }), encoding="utf-8")
    codes = cal.chart_codes()
    points = codes.astype(np.float32) / cal.MAX_CODE
    edits_r = [["bars", "offset", "r", 27.0]]
    edits_g = [["log", "log_midtone", "g", 0.10]]
    red, green = np.zeros_like(points), np.zeros_like(points)
    red[:, 0] = 0.10 * (1.0 - points[:, 0])
    green[:, 1] = -0.16 * points[:, 1]
    mix_edits = [["log", "log_offset", "r", 26.0], ["log", "log_midtone", "g", 0.05]]
    frames = {}
    for case, edits, sample in (
        ("neutral", [], points), ("offset", edits_r, points + red),
        ("mid", edits_g, points + green), ("mix", mix_edits, points + (red + green) * 0.5),
    ):
        target = tmp_path / "captures" / case
        target.mkdir(parents=True)
        frames[target / "case.tif"] = np.rint(np.clip(sample, 0.0, 1.0) * cal.MAX_CODE).astype(np.uint16)
        (target / "capture.json").write_text(json.dumps({
            "case": case, "frame": "case.tif", "resolve_edits": edits, "resolve_version": version,
        }), encoding="utf-8")
    monkeypatch.setattr(cal, "read_patches", lambda path, **_options: frames[path])
    calibration = cal.Calibration(tmp_path)
    assert calibration.neutral_code_max == 0
    assert len(calibration.responses[("offset", "r")]) == 1
    stats = cal.report(calibration, include_topos=False)["cases"]["mix"]["interpolated_edits"]
    assert stats["rgb"]["relative_rmse_reduction"] > 0.96
    assert stats["gray"]["relative_rmse_reduction"] > 0.96

    path = tmp_path / "linear.cube"
    calibration.bake(path, case=None, edits=cal.parse_edits(mix_edits), size=33, domain="linear")
    lut = load_lut(str(path))
    rgb = np.array([[0.15, 0.55, 0.80]], dtype=np.float32)
    encoded = rgb ** (1.0 / cal.TONE_CURVE)
    expected = (encoded * [0.95, 0.92, 1.0] + [0.05, 0.0, 0.0]) ** cal.TONE_CURVE
    np.testing.assert_allclose(sample_lut_trilinear(lut, rgb), expected, atol=5e-5)
    with pytest.raises(ValueError, match="No measured interval"):
        calibration.apply(rgb, cal.parse_edits([["wheels", "offset", "r", 24.0]]))

    frames[tmp_path / "captures" / "neutral" / "case.tif"] = np.zeros_like(codes)
    with pytest.raises(ValueError, match="Neutral TIFF failed"):
        cal.Calibration(tmp_path)
