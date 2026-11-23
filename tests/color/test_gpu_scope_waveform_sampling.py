"""Real GPU checks for horizontal waveform coverage, including idle quality."""
from collections.abc import Iterator

import moderngl
import numpy as np
import pytest

from src.features.color.core.scope_types import ScopeDemand
from src.shared.gpu.gpu_scope_engine import GpuScopeEngine


@pytest.fixture(scope="module")
def scope_engine() -> Iterator[GpuScopeEngine]:
    try:
        ctx = moderngl.create_standalone_context()
    except Exception as exc:
        # glcontext native backends have no shared exception type. Only
        # context availability is optional; shader/render failures must fail.
        pytest.skip(f"Standalone OpenGL unavailable: {exc}")
    engine = GpuScopeEngine(ctx)
    try:
        assert engine.load_shaders(), "Scope shaders must compile on the real GPU"
        yield engine
    finally:
        engine.release()
        ctx.release()


def _density(engine: GpuScopeEngine, analysis: tuple[int, int], mode: str,
             values: np.ndarray, mask_weight: float | None = None) -> np.ndarray:
    """Feed a horizontally uniform signal through the production GPU passes."""
    height = values.shape[0]
    rgba = np.ones((height, 64, 4), dtype=np.float16)
    rgba[:, :, :3] = values[:, None, :]
    source = engine.ctx.texture((64, height), 4, rgba.tobytes(), dtype="f2")
    source.filter = (moderngl.LINEAR, moderngl.LINEAR)
    mask = None
    try:
        if mask_weight is not None:
            mask = engine.ctx.texture((1, 1), 1, np.array([mask_weight], dtype=np.float16).tobytes(), dtype="f2")
        demand = ScopeDemand(1, 1, mode, (250, 300), analysis_size=analysis)
        engine.render_demand(demand, source, mask_texture=mask)
        engine.ctx.finish()
        texture = engine._pool["wf_density"]
        return np.frombuffer(texture.read(), dtype=np.float16).reshape(300, 1024, 4).astype(np.float32)
    finally:
        if mask is not None:
            mask.release()
        source.release()


@pytest.mark.parametrize("analysis", [(320, 180), (512, 288), (768, 432)])
@pytest.mark.parametrize("mode", ["waveform", "luma", "parade"])
def test_uniform_signal_has_no_vertical_bands(scope_engine: GpuScopeEngine,
                                             analysis: tuple[int, int], mode: str) -> None:
    ramp = np.repeat(np.linspace(0.08, 0.92, 864)[:, None], 3, axis=1)
    density = _density(scope_engine, analysis, mode, ramp)
    regions = [(0, 341, 0), (341, 682, 1), (682, 1023, 2)] if mode == "parade" else [(0, 1024, 0)]
    for start, end, channel in regions:
        columns = density[90:210, start:end, channel].mean(axis=0)
        assert columns.mean() > 0.01, "A blank waveform must not pass the uniformity check"
        assert columns.std() / columns.mean() < 0.015
    if mode == "parade":
        for channel, (start, end, _) in enumerate(regions):
            other_channels = [index for index in range(3) if index != channel]
            assert np.count_nonzero(density[:, start:end, other_channels]) == 0
        assert np.count_nonzero(density[:, 1023, :]) == 0


def test_waveform_preserves_mask_weights_and_range_diagnostics(scope_engine: GpuScopeEngine) -> None:
    ramp = np.repeat(np.linspace(-0.05, 1.1, 864)[:, None], 3, axis=1)
    unmasked = _density(scope_engine, (768, 432), "waveform", ramp)
    weighted = _density(scope_engine, (768, 432), "waveform", ramp, mask_weight=0.25)
    excluded = _density(scope_engine, (768, 432), "waveform", ramp, mask_weight=0.0)
    np.testing.assert_allclose(weighted, unmasked * 0.25, rtol=0.002, atol=0.001)
    assert not np.any(excluded)
    assert unmasked[:, :, 3].min() < 0.0
    assert unmasked[:, :, 3].max() > 0.0
