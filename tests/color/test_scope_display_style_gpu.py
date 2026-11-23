"""GPU display regressions; pixel readback is confined to these tests."""
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
        # Native context backends do not expose a common exception type.
        # Shader compilation and rendering remain outside this optional guard.
        pytest.skip(f"Standalone OpenGL unavailable: {exc}")
    engine = GpuScopeEngine(ctx)
    try:
        assert engine.load_shaders(), "Scope shaders must compile on the GPU"
        yield engine
    finally:
        engine.release()
        ctx.release()


def _render_histogram(engine: GpuScopeEngine, rgba: np.ndarray,
                      mask_weight: float | None = None) -> np.ndarray:
    height, width = rgba.shape[:2]
    source = engine.ctx.texture(
        (width, height), 4, np.ascontiguousarray(rgba, dtype="f2").tobytes(), dtype="f2",
    )
    source.filter = (moderngl.LINEAR, moderngl.LINEAR)
    mask = None
    try:
        if mask_weight is not None:
            mask = engine.ctx.texture(
                (1, 1), 1, np.array([mask_weight], dtype="f2").tobytes(), dtype="f2",
            )
        demand = ScopeDemand(1, 1, "histogram", (512, 300), analysis_size=(width, height))
        engine.render_demand(demand, source, mask_texture=mask)
        engine.ctx.finish()
        output = engine._pool["hist_display"]
        out_width, out_height = output.size
        return np.frombuffer(output.read(), dtype=np.uint8).reshape(out_height, out_width, 4).copy()
    finally:
        if mask is not None:
            mask.release()
        source.release()


def _signal_pixels(rgb: np.ndarray, channel: int) -> np.ndarray:
    """Exclude neutral background and amber guides while retaining RGB fill."""
    values = rgb.astype(np.float32)
    others = np.delete(values, channel, axis=-1).max(axis=-1)
    return (values[..., channel] > 12.0) & (values[..., channel] > others * 2.0)


def _two_peak_frame() -> np.ndarray:
    rgba = np.ones((100, 100, 4), dtype=np.float32)
    # Exact binary code values avoid sampling ambiguity. Both peaks also sit
    # between the vertical guides, so their filled height remains measurable.
    rgba[:, :, :3] = 0.28125
    rgba[:20, :, :3] = 0.71875
    return rgba


def test_histogram_display_preserves_four_to_one_peak_heights(scope_engine: GpuScopeEngine) -> None:
    image = _render_histogram(scope_engine, _two_peak_frame())
    for row, channel in zip(np.array_split(image[:, :, :3], 3), (2, 1, 0)):
        measured = []
        for code in (0.28125, 0.71875):
            bin_index = int(code * 512)
            x = round((bin_index + 0.5) / 512 * image.shape[1] - 0.5)
            measured.append(np.count_nonzero(_signal_pixels(row[:, x, :], channel)))
        strong_height, weak_height = measured
        assert strong_height > row.shape[0] * 0.75, "A blank or clipped trace cannot pass"
        assert weak_height / strong_height == pytest.approx(0.25, abs=0.05)


def test_histogram_empty_mask_removes_rgb_signal(scope_engine: GpuScopeEngine) -> None:
    frame = _two_peak_frame()
    populated = _render_histogram(scope_engine, frame)
    assert any(np.any(_signal_pixels(populated[:, :, :3], channel)) for channel in range(3))
    empty = _render_histogram(scope_engine, frame, mask_weight=0.0)
    assert not any(np.any(_signal_pixels(empty[:, :, :3], channel)) for channel in range(3))
    # The opaque ruler/grid may remain; it must not conceal stale histogram data.
    assert np.all(empty[:, :, 3] == 255)


def test_histogram_static_frame_has_identical_display_on_repeat(scope_engine: GpuScopeEngine) -> None:
    rng = np.random.default_rng(51)
    frame = rng.random((96, 160, 4), dtype=np.float32)
    frame[:, :, 3] = 1.0
    first = _render_histogram(scope_engine, frame)
    assert any(np.any(_signal_pixels(first[:, :, :3], channel)) for channel in range(3))
    for _ in range(3):
        np.testing.assert_array_equal(_render_histogram(scope_engine, frame), first)


def test_vectorscope_trace_is_coloured_and_retains_density_levels(scope_engine: GpuScopeEngine) -> None:
    pool = scope_engine.ensure_pool((320, 180), (1024, 300), (800, 800))
    density = np.zeros((800, 800), dtype=np.float16)
    # Both patches sit inside the rim and away from crosshairs and targets.
    density[232:249, 232:249] = 1.0
    density[552:569, 552:569] = 10.0
    pool["vs_density"].write(density.tobytes())
    scope_engine.display_vectorscope(pool)
    scope_engine.ctx.finish()
    image = np.frombuffer(pool["vs_display"].read(), dtype=np.uint8).reshape(800, 800, 4)
    samples = [image[y - 1:y + 2, x - 1:x + 2, :3] for x, y in ((240, 240), (560, 560))]
    for patch in samples:
        assert np.min(np.ptp(patch.astype(int), axis=-1)) > 25, "Chromatic traces must retain colour"
    assert samples[0][1, 1, 1] > samples[0][1, 1, 0]  # Negative Cb/Cr: green.
    assert samples[1][1, 1, 0] > samples[1][1, 1, 1]  # Positive Cb/Cr: magenta.
    sparse, dense = [float(patch.max(axis=-1).mean()) for patch in samples]
    assert 20.0 < sparse < 200.0, "Sparse signal should remain visible without saturating"
    assert dense > sparse + 30.0, "Density must remain distinguishable in the display"


def test_vectorscope_colour_tracks_chroma_independently_of_zoom(scope_engine: GpuScopeEngine) -> None:
    pool = scope_engine.ensure_pool((320, 180), (1024, 300), (800, 800))
    colours = []
    for zoom, x, y in ((1.0, 500, 300), (2.0, 600, 200)):
        density = np.zeros((800, 800), dtype=np.float16)
        density[y - 4:y + 5, x - 4:x + 5] = 10.0
        density[396:405, 396:405] = 10.0
        pool['vs_density'].write(density.tobytes())
        scope_engine.display_vectorscope(pool, zoom)
        scope_engine.ctx.finish()
        image = np.frombuffer(pool['vs_display'].read(), dtype=np.uint8).reshape(800, 800, 4)
        colours.append(image[y, x, :3].copy())
        neutral = image[400, 400, :3].astype(int)
        assert neutral.min() > 200 and np.ptp(neutral) <= 3
        assert colours[-1][2] > colours[-1][0] + 30
    np.testing.assert_allclose(colours[0], colours[1], atol=2.0)


def test_parade_dense_traces_have_white_cores_and_sparse_traces_keep_rgb(scope_engine: GpuScopeEngine) -> None:
    pool = scope_engine.ensure_pool((320, 180), (1024, 300), (800, 800))
    density = np.zeros((300, 1024, 4), dtype=np.float16)
    for channel in range(3):
        start, end = channel * 341, (channel + 1) * 341
        density[92:109, start:end, channel] = 0.5
        density[192:209, start:end, channel] = 128.0
    pool["wf_density"].write(density.tobytes())
    scope_engine.display_parade(pool)
    scope_engine.ctx.finish()
    image = np.frombuffer(pool["wf_display"].read(), dtype=np.uint8).reshape(300, 1024, 4)
    for channel, x in enumerate((170, 511, 852)):
        sparse = image[100, x, :3].astype(int)
        dense = image[200, x, :3].astype(int)
        assert sparse[channel] > 30
        secondary = np.delete(sparse, channel)
        assert secondary.min() >= sparse[channel] * 0.04, "Phosphor tints must not revert to pure RGB"
        assert secondary.max() < sparse[channel] * 0.25, "Sparse trails must retain vivid channel colour"
        assert dense.min() >= 210, "Dense channel traces should develop a pale core"
        assert np.ptp(dense) <= 35


def test_waveform_width_change_preserves_trace_brightness(scope_engine: GpuScopeEngine) -> None:
    # Exact horizontal footprints already normalize each density column.
    # Increasing only the plot width must not add a second gain correction.
    rgba = np.zeros((432, 64, 4), dtype=np.float16)
    rgba[:, :, 0] = np.linspace(0.1, 0.9, 432)[:, None]
    rgba[:, :, 3] = 1.0
    source = scope_engine.ctx.texture((64, 432), 4, rgba.tobytes(), dtype="f2")
    source.filter = (moderngl.LINEAR, moderngl.LINEAR)
    columns = []
    try:
        for width in (1024, 2048):
            pool = scope_engine.ensure_pool((768, 432), (width, 600), (800, 800))
            scope_engine.sample(source, pool)
            scope_engine.accumulate_waveform(pool)
            scope_engine.display_waveform(pool)
            scope_engine.ctx.finish()
            image = np.frombuffer(pool["wf_display"].read(), dtype=np.uint8).reshape(600, width, 4)
            columns.append(image[150:400, width // 2, 0].copy())
        assert columns[0].mean() > 10.0, "The comparison must contain an actual trace"
        np.testing.assert_allclose(columns[1], columns[0], rtol=0.03, atol=1.0)
    finally:
        source.release()


@pytest.mark.parametrize(('dx', 'dy'), [(1, 0), (0, 1), (1, 1), (1, -1)])
def test_parade_reconstructs_supported_single_pixel_gaps_only(
    scope_engine: GpuScopeEngine, dx: int, dy: int,
) -> None:
    pool = scope_engine.ensure_pool((320, 180), (1024, 300), (800, 800))
    density = np.zeros((300, 1024, 4), dtype=np.float16)
    for channel in range(3):
        x, y = 170 + channel * 341, 150
        density[y - dy, x - dx, channel] = 0.8
        density[y + dy, x + dx, channel] = 0.8
    pool['wf_density'].write(density.tobytes())
    before = pool['wf_density'].read()
    scope_engine.display_parade(pool)
    scope_engine.ctx.finish()
    image = np.frombuffer(pool['wf_display'].read(), dtype=np.uint8).reshape(300, 1024, 4)
    for channel in range(3):
        x = 170 + channel * 341
        assert image[150, x, channel] > 40, 'Two-sided support must reconstruct the missing display point'
        assert image[150, x, channel] < image[150 + dy, x + dx, channel]
    assert pool['wf_density'].read() == before, 'Display reconstruction must not modify measured density'


def test_parade_does_not_dilate_isolated_points_or_bridge_different_channels(
    scope_engine: GpuScopeEngine,
) -> None:
    pool = scope_engine.ensure_pool((320, 180), (1024, 300), (800, 800))
    density = np.zeros((300, 1024, 4), dtype=np.float16)
    density[150, 120, 0] = 0.8  # Retain only the existing narrow vertical AA.
    density[150, 220, 0] = density[150, 223, 0] = 0.8  # Two-pixel gap remains open.
    density[150, 269, 0] = density[150, 271, 1] = 0.8  # Different channels provide no shared support.
    pool['wf_density'].write(density.tobytes())
    scope_engine.display_parade(pool)
    scope_engine.ctx.finish()
    image = np.frombuffer(pool['wf_display'].read(), dtype=np.uint8).reshape(300, 1024, 4)
    assert image[150, 120, 0] > 40
    for y in (149, 151):
        assert image[y, 120, 0] < image[150, 120, 0] * 0.5
    for x, y in ((119, 150), (121, 150), (120, 148), (120, 152),
                 (221, 150), (222, 150), (270, 150)):
        assert image[y, x, :3].max() < 5, 'Unsupported areas must stay empty'
