"""Check the working encoding, custom LUT domain, and viewer shader parity."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.shared.color.aces_monitor_lut import (
    ACES_MONITOR_LUT_SPACE, apply_aces_monitor_lut, validate_aces_monitor_lut,
)
from src.shared.color.aces_video_gl import AcesVideoGLPass
from src.shared.color.color_space_descriptor import ACESCG_SCENE
from src.shared.color.color_transform_provider import BuiltinColorProvider


def _write_cube(
    path: Path, table: np.ndarray, lower: tuple[float, float, float], upper: tuple[float, float, float],
    *, shaper: bool = False,
) -> None:
    prefix = "LUT_1D_SIZE 2\n0 0 0\n1 1 1\n" if shaper else ""
    header = [f"LUT_3D_SIZE {len(table)}", "DOMAIN_MIN " + " ".join(map(str, lower)),
              "DOMAIN_MAX " + " ".join(map(str, upper))]
    rows = [" ".join(map(str, row)) for row in table.reshape(-1, 3)]
    path.write_text(prefix + "\n".join(header + rows) + "\n", encoding="utf-8")


def _identity_grid(size: int) -> np.ndarray:
    levels = np.linspace(0.0, 1.0, size, dtype=np.float32)
    blue, green, red = np.meshgrid(levels, levels, levels, indexing="ij")
    return np.stack((red, green, blue), axis=-1)


def test_aces_monitor_cube_encodes_acescct_and_preserves_alpha(tmp_path: Path) -> None:
    path = tmp_path / "display.cube"
    lower, upper = (-0.1, 0.1, -0.2), (0.7, 0.8, 0.5)
    grid = _identity_grid(3)[..., [2, 0, 1]] * 1.25 - 0.05
    _write_cube(path, grid, lower, upper)
    validate_aces_monitor_lut(str(path))

    scene = np.array([[-0.002, 0.18, 0.0078125, 0.19], [1.7, 10.0, 4.0, 0.77]], dtype=np.float32)
    encoded = np.where(scene[..., :3] <= 0.0078125, scene[..., :3] * 10.5402377416545 + 0.0729055341958355,
                       (np.log2(np.maximum(scene[..., :3], 0.0078125)) + 9.72) / 17.52)
    normalized = np.clip((encoded - lower) / (np.asarray(upper) - lower), 0.0, 1.0)
    result = apply_aces_monitor_lut(scene, str(path))
    np.testing.assert_allclose(result[..., :3], normalized[..., [2, 0, 1]] * 1.25 - 0.05, atol=1e-6)
    np.testing.assert_array_equal(result[..., 3], scene[..., 3])
    assert result.max() > 1.0


def test_aces_monitor_cube_rejects_shapers_and_invalid_domains(tmp_path: Path) -> None:
    path = tmp_path / "bad.cube"
    grid = _identity_grid(2)
    _write_cube(path, grid, (0, 0, 0), (1, 1, 1), shaper=True)
    with pytest.raises(ValueError, match="without a 1D shaper"):
        validate_aces_monitor_lut(str(path))
    _write_cube(path, grid, (0, 1, 0), (1, 1, 1))
    with pytest.raises(ValueError, match="maximum must exceed"):
        validate_aces_monitor_lut(str(path))


def test_aces_monitor_cube_gpu_matches_cpu_from_multiple_working_encodings(tmp_path: Path) -> None:
    moderngl = pytest.importorskip("moderngl")
    try:
        ctx = moderngl.create_standalone_context(require=330)
    except Exception as exc:
        pytest.skip(f"OpenGL 3.3 unavailable: {exc}")

    path = tmp_path / "nonlinear.cube"
    rng = np.random.default_rng(20261126)
    grid = rng.uniform(-0.12, 1.2, (5, 5, 5, 3)).astype(np.float32)
    _write_cube(path, grid, (-0.25, 0.0, -0.08), (0.8, 0.9, 0.67))
    scene = rng.uniform(-0.018, 8.0, (8, 16, 4)).astype(np.float32)
    scene[..., 3] = rng.random(scene.shape[:2])
    scene[:, 0, :3] = [0.0, 0.007, 0.18]
    output = ctx.texture((16, 8), 4, dtype="f4")
    fbo = ctx.framebuffer(color_attachments=[output])
    view = AcesVideoGLPass(ctx, SimpleNamespace(version="1.3", target="rec709-g24", monitor_lut=str(path)))
    try:
        expected = apply_aces_monitor_lut(scene, str(path))
        for working in (ACESCG_SCENE, ACES_MONITOR_LUT_SPACE):
            encoded = BuiltinColorProvider().transform_signal(scene, ACESCG_SCENE, working)
            source = ctx.texture((16, 8), 4, encoded.tobytes(), dtype="f4")
            try:
                fbo.use()
                view.draw(source, working)
                result = np.frombuffer(output.read(), dtype=np.float32).reshape(scene.shape)
                np.testing.assert_allclose(result, expected, atol=2e-5, rtol=0.0)
            finally:
                source.release()
    finally:
        view.release()
        fbo.release()
        output.release()
        ctx.release()
