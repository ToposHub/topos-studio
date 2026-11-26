"""A calibration cube operates on display code values."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from src.shared.color.monitor_calibration import load_monitor_calibration


def test_calibration_reloads_a_replaced_cube(tmp_path: Path) -> None:
    pytest.importorskip("PyOpenColorIO")
    path = tmp_path / "calibration.cube"
    path.write_text("LUT_3D_SIZE 2\n" + "\n".join(
        f"{1 - r} {g / 2} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)
    ) + "\n")
    rgb = np.array([[[0.25, 0.8, 0.2]]], dtype=np.float32)
    original = rgb.copy()
    calibration = load_monitor_calibration(str(path))
    assert load_monitor_calibration(str(path)) is calibration
    np.testing.assert_allclose(calibration.apply_rgb(rgb), [[[0.75, 0.4, 0.2]]], atol=1e-6)
    np.testing.assert_array_equal(rgb, original)

    original_stat = path.stat()
    replacement = tmp_path / "replacement.cube"
    black = "LUT_3D_SIZE 2\n" + "0 0 0\n" * 8
    replacement.write_text(black.ljust(original_stat.st_size))
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    replacement.replace(path)
    updated = load_monitor_calibration(str(path))
    np.testing.assert_array_equal(updated.apply_rgb(rgb), np.zeros_like(rgb))
    np.testing.assert_allclose(calibration.apply_rgb(rgb), [[[0.75, 0.4, 0.2]]], atol=1e-6)


def test_calibration_rejects_an_invalid_cube(tmp_path: Path) -> None:
    pytest.importorskip("PyOpenColorIO")
    path = tmp_path / "failed.cube"
    path.write_text("LUT_3D_SIZE 2\n0 0 0\n")
    with pytest.raises(ValueError, match="Invalid monitor calibration LUT"):
        load_monitor_calibration(str(path))


def test_calibration_supports_3dl_and_1d_cube(tmp_path: Path) -> None:
    pytest.importorskip("PyOpenColorIO")
    path = tmp_path / "identity.3dl"
    points = (0, 2048, 4095)
    path.write_text("\n".join(f"{r} {g} {b}" for r in points for g in points for b in points) + "\n")
    rgb = np.array([[0.2, 0.4, 0.9]], dtype=np.float32)
    np.testing.assert_allclose(load_monitor_calibration(str(path)).apply_rgb(rgb), rgb, atol=1e-4)

    curves = tmp_path / "curves.cube"
    curves.write_text("LUT_1D_SIZE 2\n0 0 0\n0.5 0.75 1\n")
    np.testing.assert_allclose(load_monitor_calibration(str(curves)).apply_rgb(rgb), [[0.1, 0.3, 0.9]], atol=1e-6)
