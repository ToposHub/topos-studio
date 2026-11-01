"""Real GPU checks; readback is confined to this diagnostic test module."""
from pathlib import Path

import moderngl
import numpy as np
import pytest

from src.features.color.core.scope_types import ScopeDemand
from src.shared.gpu.gpu_scope_engine import GpuScopeEngine


@pytest.fixture(scope="module")
def gpu():
    ctx = moderngl.create_standalone_context()
    engine = GpuScopeEngine(ctx)
    assert engine.load_shaders()
    yield ctx, engine
    engine.release()
    ctx.release()


def render(gpu, rgba: np.ndarray, mask: bool = False, curve_mode: str = 'Custom'):
    ctx, engine = gpu
    h, w = rgba.shape[:2]
    tex = ctx.texture((w, h), 4, rgba.astype("f2").tobytes(), dtype="f2")
    mask_tex = ctx.texture((w, h), 1, np.ascontiguousarray(rgba[:, :, 3], dtype="f2").tobytes(), dtype="f2") if mask else None
    try:
        demand = ScopeDemand(1, 1, "histogram", (1024, 512),
                             analysis_size=(w, h), histogram_overlay=True, curves_mode=curve_mode)
        outputs = engine.render_demand(demand, tex, mask_texture=mask_tex)
        ctx.finish()
        assert outputs["curves_histogram"][0] != outputs["histogram"][0]
        pool = engine._pool
        sums = np.frombuffer(pool["hist_sum"].read(), "f2").astype("f4").reshape(512, 4)
        peak = np.frombuffer(pool["hist_peak"].read(), "f4").copy()
        image = np.frombuffer(pool["curves_hist_display"].read(), "u1").reshape(512, 1024, 4).copy()
        return np.expm1(sums), peak, image
    finally:
        tex.release()
        if mask_tex is not None:
            mask_tex.release()


def test_empty_mask_is_plain_charcoal(gpu):
    counts, peak, image = render(gpu, np.zeros((180, 320, 4), "f4"), mask=True)
    assert not counts.any()
    assert np.isfinite(peak).all() and (peak > 0).all()
    assert np.max(np.abs(image[:, :, :3].astype(int) - [35, 35, 38])) <= 1


def test_luminance_is_independent_and_full_frame_peak_stays_finite(gpu):
    rgba = np.ones((432, 768, 4), "f4")
    rgba[:, :, :3] = [0.8, 0.3, 0.1]
    counts, peak, image = render(gpu, rgba)
    assert np.isfinite(peak).all()
    assert (peak > 65504).all()  # Peak texture must support more than float16.
    bins = counts.argmax(axis=0)
    expected_y = int(np.dot([0.8, 0.3, 0.1], [0.2126, 0.7152, 0.0722]) * 512)
    assert abs(int(bins[3]) - expected_y) <= 1
    assert bins[2] < bins[1] < bins[3] < bins[0]
    assert np.allclose(counts.sum(axis=0), 432 * 768, rtol=0.015)
    assert image[:, :, :3].max() < 200  # No neon or white clipping.


def test_linear_heights_preserve_peak_ratio(gpu):
    rgba = np.ones((100, 100, 4), "f4")
    rgba[:, :, :3] = 0.25
    rgba[:20, :, :3] = 0.75
    counts, peak, image = render(gpu, rgba)
    assert counts[384, 0] / counts[128, 0] == pytest.approx(0.25, abs=0.01)
    background = np.array([35, 35, 38])
    low_peak_pixels = (np.abs(image[:, 256, :3].astype(int) - background).max(axis=1) > 8).sum()
    high_peak_pixels = (np.abs(image[:, 768, :3].astype(int) - background).max(axis=1) > 8).sum()
    assert high_peak_pixels / low_peak_pixels == pytest.approx(0.25, abs=0.04)


def test_positive_mask_weight_is_shared_by_y_and_rgb(gpu):
    rgba = np.full((180, 320, 4), 0.25, "f4")
    counts, peak, image = render(gpu, rgba, mask=True)
    assert np.allclose(counts.sum(axis=0), 180 * 320 * 0.25, rtol=0.015)


def test_repeated_static_frame_has_identical_heights_and_brightness(gpu):
    rng = np.random.default_rng(29)
    rgba = rng.random((432, 768, 4), dtype=np.float32)
    rgba[:, :, 3] = 1.0
    counts, peak, image = render(gpu, rgba)
    for _ in range(3):
        next_counts, next_peak, next_image = render(gpu, rgba)
        np.testing.assert_array_equal(next_counts, counts)
        np.testing.assert_array_equal(next_peak, peak)
        np.testing.assert_array_equal(next_image, image)


def test_contour_stroke_is_consistent_on_flat_and_steep_segments(gpu):
    from scipy.ndimage import map_coordinates
    ctx, engine = gpu
    # Feed known counts into the production display pass so only stroke
    # geometry, rather than source decoding or sampling noise, is measured.
    pool = engine.ensure_pool((768, 432), (1024, 300), (800, 800))
    values = np.zeros((512, 4), dtype=np.float32)
    values[:, 3] = np.interp(np.arange(512), [0, 210, 240, 511], [20, 20, 100, 100])
    pool['hist_sum'].write(np.log1p(values).astype('f2').tobytes())
    for width, height, dpr in ((800, 420, 1.0), (1200, 600, 1.0), (1600, 840, 2.0)):
        engine.display_curves_histogram(pool, (width, height), dpr)
        ctx.finish()
        assert pool['curves_hist_display'].size == (width, height)
        image = np.frombuffer(pool['curves_hist_display'].read(), 'u1').reshape(height, width, 4)
        peak = np.frombuffer(pool['hist_peak'].read(), 'f4')[3]
        counts = np.expm1(np.log1p(values[:, 3]).astype('f2').astype('f4'))
        smooth = np.convolve(np.pad(counts, (2, 2), mode='edge'), [1/16, 4/16, 6/16, 4/16, 1/16], 'valid')
        offsets = np.linspace(-4 * dpr, 4 * dpr, 321)
        measured = []
        for col in (100.0, 225.0):
            x = (col + 0.5) / 512 * width - 0.5
            y = smooth[int(col)] / peak * 0.98 * height - 0.5
            slope = (smooth[int(col) + 1] - smooth[int(col) - 1]) / 2 / peak * 0.98 * height * 512 / width
            normal = np.array([-slope, 1.0]) / np.hypot(slope, 1.0)
            profile = map_coordinates(image[:, :, 0].astype(float),
                                      [y + normal[1] * offsets, x + normal[0] * offsets], order=1)
            measured.append(np.count_nonzero(profile > 100) * (offsets[1] - offsets[0]) / dpr)
        assert max(measured) - min(measured) < 0.55, measured


def test_repeated_widget_size_reuses_curve_texture_and_framebuffer(gpu):
    ctx, engine = gpu
    pool = engine.ensure_pool((768, 432), (1024, 300), (800, 800))
    pool['hist_sum'].write(np.zeros((512, 4), dtype='f2').tobytes())
    engine.display_curves_histogram(pool, (900, 450), 1.0)
    tex, fbo = pool['curves_hist_display'], pool['curves_hist_display_fbo']
    for _ in range(3):
        same = engine.ensure_pool((768, 432), (1024, 300), (800, 800))
        engine.display_curves_histogram(same, (900, 450), 1.0)
        assert same['curves_hist_display'] is tex
        assert same['curves_hist_display_fbo'] is fbo


@pytest.mark.parametrize(('mode', 'bin_position', 'weight'), [
    ('HueHue', 28, 0.75), ('HueSat', 28, 0.75), ('HueLum', 28, 0.75),
    ('SatSat', 384, 1.0), ('SatLum', 384, 1.0), ('LumSat', 254, 1.0),
])
def test_hsl_histogram_uses_its_own_input_axis(gpu, mode, bin_position, weight):
    rgba = np.ones((180, 320, 4), dtype='f4')
    rgba[:, :, :3] = [0.8, 0.4, 0.2]
    counts, peak, image = render(gpu, rgba, curve_mode=mode)
    assert abs(int(counts[:, 3].argmax()) - bin_position) <= 1
    assert counts[:, 3].sum() == pytest.approx(180 * 320 * weight, rel=0.015)


def test_neutral_pixels_do_not_create_a_false_red_hue_peak(gpu):
    rgba = np.ones((180, 320, 4), dtype='f4')
    rgba[:, :, :3] = 0.5
    counts, peak, image = render(gpu, rgba, curve_mode='HueHue')
    assert counts[:, 3].sum() == 0


def test_real_frame_preview(gpu, tmp_path):
    import cv2
    video = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not video.exists():
        pytest.skip("Local diagnostic video is unavailable")
    cap = cv2.VideoCapture(str(video))
    try:
        ok, bgr = cap.read()
    finally:
        cap.release()
    assert ok
    rgb = cv2.cvtColor(cv2.resize(bgr, (768, 432)), cv2.COLOR_BGR2RGB) / 255.0
    rgba = np.concatenate([rgb, np.ones((432, 768, 1))], axis=2)
    counts, peak, image = render(gpu, rgba)
    assert np.isfinite(peak).all() and (peak > 0).all()
    assert np.allclose(counts.sum(axis=0), 432 * 768, rtol=0.015)
    path = tmp_path / "curves-histogram.png"
    cv2.imwrite(str(path), cv2.cvtColor(image[::-1], cv2.COLOR_RGBA2BGRA))
    print(f"GPU histogram preview: {path}")
    for mode in ('HueHue', 'LumSat', 'SatSat'):
        _, mode_peak, mode_image = render(gpu, rgba, curve_mode=mode)
        assert np.isfinite(mode_peak).all()
        mode_path = tmp_path / f'{mode}-histogram.png'
        cv2.imwrite(str(mode_path), cv2.cvtColor(mode_image[::-1], cv2.COLOR_RGBA2BGRA))
        print(f'GPU HSL preview: {mode_path}')
