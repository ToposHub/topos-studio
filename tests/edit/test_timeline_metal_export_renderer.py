"""Host-side Metal timeline parameter layout and input validation."""

from __future__ import annotations

import logging
import math
from types import SimpleNamespace

import numpy as np
import pytest

from src.features.edit.core.timeline_metal_export_renderer import (
    TimelineMetalExportRenderer,
    _PARAM_DTYPE,
)
from src.shared.export.gpu_encode_surface import (
    GpuEncodeSurface,
    GpuEncodeSurfaceDescriptor,
)
from src.shared.export.gpu_trail import ExportGpuTrail
from src.shared.media.gpu_frame import GpuFramePlane, GpuFrameRelease, GpuPlaneResource


def test_renderer_records_measured_render_and_readback_evidence():
    trail = ExportGpuTrail("renderer-evidence")
    renderer = TimelineMetalExportRenderer(trail)
    context = SimpleNamespace(last_gpu_time_ms=2.5)

    renderer._record_render_completion(
        context,
        1920,
        yuv_output=True,
        cpu_upload_bytes=512,
    )
    renderer._fallback("encoder_surface_unavailable")

    snapshot = trail.snapshot()
    assert snapshot.stage("render").gpu_submit_ms == pytest.approx(2.5)
    assert snapshot.stage("yuv_conversion").cpu_download_bytes == 1920
    assert snapshot.cpu_upload_bytes == 512
    assert snapshot.full_gpu_claim_reason() == "trail_stage_missing:decode"
    assert snapshot.fallback_reason == "encoder_surface_unavailable"


def test_renderer_resolves_owned_linear_metal_output_surface():
    class FakeBuffer:
        def length(self):
            return 4096

    buffer = FakeBuffer()
    plane = GpuFramePlane(
        "Y", "metal", 0, "2d", 64, 64,
        resource=GpuPlaneResource(
            api="metal", native_handle=buffer, resource_format="linear_yuv",
            width=64, height=64, device_identity="gpu-0",
        ),
    )
    surface = GpuEncodeSurface(
        GpuEncodeSurfaceDescriptor(
            backend="metal", pixel_format="nv12-linear", width=64, height=64,
            bit_depth=8, device_identity="gpu-0", planes=(plane,),
        ),
        GpuFrameRelease(),
        lambda _surface: None,
    )
    renderer = TimelineMetalExportRenderer()
    renderer._context = SimpleNamespace(device_name="gpu-0")
    assert renderer._resolve_surface_buffer(surface, 1024) is buffer


def test_metal_renderer_compiles_only_the_pipeline_on_first_use(monkeypatch):
    class FakeContext:
        def __init__(self):
            self.compiled = []

        def compile_kernel(self, name, _source):
            self.compiled.append(name)
            return name

        def finish(self):
            return None

    context = FakeContext()
    monkeypatch.setattr(
        "src.features.edit.core.timeline_metal_export_renderer.MetalComputeContext",
        lambda: context,
    )
    renderer = TimelineMetalExportRenderer()
    assert renderer._ensure_ready()
    assert context.compiled == []

    assert renderer._kernel("timeline_fused_export_texture") == (
        "timeline_fused_export_texture")
    assert renderer._kernel("timeline_fused_export_texture") == (
        "timeline_fused_export_texture")
    assert context.compiled == ["timeline_fused_export_texture"]
    renderer.release()


def test_metal_timeline_parameter_buffer_matches_msl_struct_layout():
    grade = {
        "u_lift": (0.9, 1.0, 1.1),
        "u_gamma": (1.0, 1.0, 1.0),
        "u_gain": (1.1, 1.0, 0.9),
        "u_exposure": 12.0,
        "u_temp": -5.0,
        "u_tint": 2.0,
        "u_contrast": 8.0,
        "u_shadow": 3.0,
        "u_highlights": -4.0,
        "u_saturation": 10.0,
        "u_vibrance": 6.0,
        "u_hue_shift": 15.0,
    }
    params = TimelineMetalExportRenderer._make_params(
        1280, 720, 1920, 1080,
        640, 360, 640, 360,
        10, 2, 2, True, False,
        0.75, True, True,
        0.2126, 0.0722, grade,
    )

    assert params.dtype == _PARAM_DTYPE
    assert params.dtype.itemsize == 22 * 16
    assert params["source_output_dims"][0].tolist() == [1280, 720, 1920, 1080]
    assert params["source_flags"][0].tolist() == [10, 2, 2, 1]
    assert params["control"][0].tolist() == [0.75, 1.0, 1.0, 0.0]
    assert params["grade_a"][0].tolist() == [12.0, -5.0, 2.0, 8.0]
    assert params["grade_b"][0].tolist() == [3.0, -4.0, 10.0, 6.0]
    assert params["grade_c"][0, 0] == 15.0

    effect = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 2, "half_x": 3, "half_y": 2, "strength": 0.75,
         "sigma_x": 1.4, "sigma_y": 1.1},
    )
    assert effect["effect"][0].tolist() == [2.0, 3.0, 2.0, 0.75]
    assert effect["effect_sigma"][0, :2].tolist() == pytest.approx([1.4, 1.1])
    glow = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 4, "half_x": 5, "half_y": 5, "strength": 0.8,
         "sigma_x": 2.5, "sigma_y": 2.5,
         "threshold": 0.7, "intensity": 0.5},
    )
    assert glow["effect_extra"][0, :2].tolist() == pytest.approx([0.7, 0.5])
    morphology = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 5, "half_x": 2, "half_y": 2, "strength": 1.0,
         "sigma_x": 1.0, "sigma_y": 1.0, "operation": 1.0},
    )
    assert morphology["effect_extra"][0].tolist() == [1.0, 0.0, 0.0, 0.0]
    morphology_reflect = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 5, "half_x": 2, "half_y": 2, "strength": 1.0,
         "sigma_x": 1.0, "sigma_y": 1.0,
         "operation": 0.0, "edge_policy": 2.0},
    )
    assert morphology_reflect["effect_sigma"][0].tolist() == [1.0, 1.0, 2.0, 0.0]
    edge = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 6, "half_x": 1, "half_y": 1, "strength": 1.5,
         "sigma_x": 0.2, "sigma_y": 0.3,
         "direction": 2, "output_mode": 1, "invert": 1,
         "preblur": 1.25},
    )
    assert edge["effect_sigma"][0].tolist() == pytest.approx(
        [0.2, 0.3, 2.0, 1.0])
    assert edge["effect_extra"][0].tolist() == [1.0, 0.0, 0.0, 1.25]
    edge_alpha = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 6, "half_x": 1, "half_y": 1, "strength": 1.0,
         "sigma_x": 0.1, "sigma_y": 0.3, "direction": 0,
         "output_mode": 0, "invert": 0, "channel": 1},
    )
    assert edge_alpha["effect_extra"][0].tolist() == [0.0, 1.0, 0.0, 0.0]
    canny = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 6, "half_x": 1, "half_y": 1, "strength": 1.0,
         "sigma_x": 0.2, "sigma_y": 0.4, "direction": 0,
         "output_mode": 0, "invert": 0, "channel": 0,
         "canny": 1},
    )
    assert canny["effect_extra"][0].tolist() == [0.0, 0.0, 1.0, 0.0]
    denoise = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 7, "half_x": 3, "half_y": 3, "strength": 0.5,
         "sigma_x": 1.2, "sigma_y": 0.1125,
         "spatial_sigma": 1.2, "range_sigma": 0.1125,
         "luminance_only": 1, "chroma_only": 0},
    )
    assert denoise["effect_sigma"][0].tolist() == pytest.approx(
        [1.2, 0.1125, 0.0, 0.0])
    assert denoise["effect_extra"][0].tolist() == [1.0, 0.0, 0.0, 0.0]
    bilateral = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 9, "half_x": 5, "half_y": 5, "strength": 0.75,
         "sigma_x": 5.0, "sigma_y": 48.0 / 255.0,
         "spatial_sigma": 5.0, "range_sigma": 48.0 / 255.0,
         "channel_r": 1, "channel_g": 0, "channel_b": 1},
    )
    assert bilateral["effect_sigma"][0].tolist() == pytest.approx(
        [5.0, 48.0 / 255.0, 0.0, 0.0])
    assert bilateral["effect_extra"][0].tolist() == [1.0, 0.0, 1.0, 0.0]
    motion = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 10, "half_x": 8, "half_y": 1.25, "strength": 0.75,
         "sigma_x": 0.6, "sigma_y": 1.25, "edge_policy": 1.0,
         "sample_count": 32, "channel_r": 1, "channel_g": 0,
         "channel_b": 1},
    )
    assert motion["effect_sigma"][0].tolist() == pytest.approx(
        [math.cos(0.6), math.sin(0.6), 1.0, 32.0])
    assert motion["effect_extra"][0].tolist() == [1.0, 0.0, 1.0, 0.0]
    bokeh = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 11, "half_x": 8, "half_y": 6, "strength": 0.75,
         "anamorphic": 0.4, "highlight_gain": 1.2,
         "edge_policy": 2.0, "sample_count": 16,
         "channel_r": 1, "channel_g": 1, "channel_b": 0,
         "aperture_blades": 6},
    )
    assert bokeh["effect"].tolist()[0] == pytest.approx([11.0, 8.0, 6.0, 0.75])
    assert bokeh["effect_sigma"][0].tolist() == pytest.approx(
        [0.4, 1.2, 2.0, 16.0])
    assert bokeh["effect_extra"][0].tolist() == [1.0, 1.0, 0.0, 6.0]
    defocus = TimelineMetalExportRenderer._make_params(
        64, 48, 64, 48, 1, 1, 1, 1, 8, 1, 1, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
        {"kind": 8, "half_x": 15, "half_y": 0, "strength": 1.0,
         "sigma_x": 0.4, "sigma_y": 0.6,
         "focal_x": 0.4, "focal_y": 0.6, "focal_size": 0.25,
         "max_radius": 24.0, "edge_policy": 2.0,
         "mode": 0.0, "process_alpha": 0.0},
    )
    assert defocus["effect_sigma"][0].tolist() == pytest.approx(
        [0.4, 0.6, 0.25, 24.0])
    assert defocus["effect_extra"][0].tolist() == [2.0, 0.0, 0.0, 0.0]


def test_metal_timeline_parameter_buffer_is_contiguous():
    params = TimelineMetalExportRenderer._make_params(
        2, 2, 2, 2, 1, 1, 1, 1, 8, 2, 2, False, False,
        1.0, False, False, 0.2126, 0.0722, None,
    )
    assert np.ascontiguousarray(params).tobytes() == params.tobytes()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_high_precision_export_quantizes_bgr16_on_metal():
    renderer = TimelineMetalExportRenderer()
    source = np.zeros((4, 4, 3), dtype=np.uint8)

    result = renderer.render_packed_bgr(
        source, None, 4, 4, 1.0, output_u16=True)
    try:
        assert result is not None, renderer.last_fallback_reason
        assert result.dtype == np.uint16
        assert result.shape == (4, 4, 3)
        assert renderer.metal_errors == 0
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_topos_yuv422p10_output_stays_planar_on_metal():
    """Topos delivery is emitted as uint16 Y/U/V planes by both Metal paths.

    AUD-18601（锁定旧行为协议更新）：Metal 合成链的工作集是 8-bit 码值域，
    >8-bit 源的 yuv422p10 交付此前经该链静默降为 8-bit 有效精度。新契约：
    8-bit 源仍在两条 Metal 路径上直出 uint16 Y/U/V 平面（原意图保留）；
    10-bit 源 fail-closed（fallback reason
    ``metal_8bit_chain_high_depth_source``），由调用方回落 GL RGB16UI /
    CPU BGR16 全精度路径。
    """
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuv422P10Output, GpuYuvSource,
    )

    rng = np.random.default_rng(90210)
    planes = (
        rng.integers(16, 235, (48, 64), dtype=np.uint8),
        rng.integers(16, 240, (48, 32), dtype=np.uint8),
        rng.integers(16, 240, (48, 32), dtype=np.uint8),
    )
    source = GpuYuvSource(
        planes=planes, luma_bits=8, chroma_x_div=2, chroma_y_div=1,
        full_range=False, kr=0.2126, kb=0.0722,
    )
    renderer = TimelineMetalExportRenderer()
    try:
        fused = renderer.render_planar(
            source, None, 64, 48, 1.0, output_yuv422p10=True)
        stacked = renderer.render_layer_stack(
            [(source, None, 1.0, 0)], 64, 48,
            output_yuv422p10=True)
        assert isinstance(fused, GpuYuv422P10Output)
        assert isinstance(stacked, GpuYuv422P10Output)
        for plane in fused.planes:
            assert plane.dtype == np.uint16
            assert plane.max() <= 1023
        for actual, expected in zip(fused.planes, stacked.planes):
            assert int(np.abs(actual.astype(np.int32)
                              - expected.astype(np.int32)).max()) <= 2

        # AUD-18601：10-bit 源 + 高精度交付 fail-closed（两条路径一致）。
        deep_planes = (
            rng.integers(64, 940, (48, 64), dtype=np.uint16),
            rng.integers(64, 960, (48, 32), dtype=np.uint16),
            rng.integers(64, 960, (48, 32), dtype=np.uint16),
        )
        deep_source = GpuYuvSource(
            planes=deep_planes, luma_bits=10, chroma_x_div=2, chroma_y_div=1,
            full_range=False, kr=0.2126, kb=0.0722,
        )
        assert renderer.render_planar(
            deep_source, None, 64, 48, 1.0, output_yuv422p10=True) is None
        assert renderer.last_fallback_reason == \
            "metal_8bit_chain_high_depth_source"
        assert renderer.render_layer_stack(
            [(deep_source, None, 1.0, 0)], 64, 48,
            output_yuv422p10=True) is None
        assert renderer.last_fallback_reason == \
            "metal_8bit_chain_high_depth_source"
    finally:
        renderer.release()


def test_unsupported_odd_yuv420_dimensions_report_cpu_fallback(caplog):
    caplog.set_level(logging.INFO)
    renderer = TimelineMetalExportRenderer()
    source = np.zeros((4, 4, 3), dtype=np.uint8)

    assert renderer.render_packed_bgr(
        source, None, 3, 4, 1.0, output_yuv420p=True) is None

    assert renderer.last_fallback_reason == "odd_yuv420p_output_dimensions"
    assert "odd_yuv420p_output_dimensions; using CPU fallback" in caplog.text


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize("feather", (0.0, 2.5))
def test_real_metal_layer_stack_applies_transform_and_crop_matte(feather):
    from src.features.edit.core.adjustment_composite import apply_transform_cpu

    def _metal_reference_crop(frame: np.ndarray, crop) -> np.ndarray:
        """AUD-16315：Metal MSL crop shader 的本地参考实现。

        timeline_metal_export_renderer.py 的 MSL ``crop_matte_pixel`` 仍是
        旧的“内侧羽化”语义（边界 alpha=0、向矩形内渐显，硬边用
        ceil/floor）。crop_cpu.apply_crop_cpu 已改为委托 crop_render 单一
        几何源（边界 alpha=1、向外渐隐），两者方向相反；Metal 侧对齐属
        其自身条目（文件不在 AUD-16315 批次白名单）。本平价测试的意图
        是“Metal 输出 == 其声明的 matte 语义”，故在此内联镜像 MSL 的
        ramp，而不是让测试静默跟随 crop_cpu 的新方向。
        """
        h, w = frame.shape[:2]
        left, right, top, bottom, feather_px = (float(v) for v in crop[:5])
        low = np.array([left * w, top * h], dtype=np.float32)
        high = np.array([(1.0 - right) * w, (1.0 - bottom) * h],
                        dtype=np.float32)
        ys = (np.arange(h, dtype=np.float32) + 0.5)[:, None]
        xs = (np.arange(w, dtype=np.float32) + 0.5)[None, :]
        centers = np.stack(np.broadcast_arrays(xs, ys), axis=-1)

        def _smooth(distance: np.ndarray) -> np.ndarray:
            t = np.clip(distance / feather_px, 0.0, 1.0)
            return t * t * (3.0 - 2.0 * t)

        if feather_px > 0.5:
            matte = (
                _smooth(centers[..., 0] - low[0])
                * _smooth(high[0] - centers[..., 0])
                * _smooth(centers[..., 1] - low[1])
                * _smooth(high[1] - centers[..., 1])
            )
        else:
            first = np.ceil(low).astype(int)
            end = np.floor(high).astype(int)
            xx, yy = np.meshgrid(np.arange(w), np.arange(h))
            matte = (
                (xx >= first[0]) & (xx < end[0])
                & (yy >= first[1]) & (yy < end[1])
            ).astype(np.float32)

        if frame.shape[2] == 3:
            alpha_ch = np.full((h, w, 1), 255, dtype=frame.dtype)
            frame = np.concatenate([frame, alpha_ch], axis=2)
        else:
            frame = frame.copy()
        existing = frame[:, :, 3].astype(np.float32) / 255.0
        frame[:, :, 3] = np.rint(
            np.clip(existing * matte, 0.0, 1.0) * 255.0
        ).astype(frame.dtype)
        return frame

    height, width = 24, 32
    yy, xx = np.mgrid[:height, :width]
    source = np.stack((xx * 7 % 256, yy * 9 % 256,
                       (xx * 3 + yy * 5) * 2 % 256), axis=-1).astype(np.uint8)
    transform = (2.25, -1.5, 1.08, 0.92, 11.0,
                 "center", width / 2.0, height / 2.0)
    crop = (0.12, 0.08, 0.10, 0.14, feather)

    source_with_matte = _metal_reference_crop(source, crop)
    transformed = apply_transform_cpu(
        source_with_matte, transform, width, height, width, height)
    reference = _metal_reference_crop(transformed, crop)
    expected = np.rint(
        reference[..., :3].astype(np.float32)
        * (reference[..., 3:4].astype(np.float32) / 255.0)
    ).astype(np.uint8)

    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, None, transform,
              (width, height), crop)], width, height)
        assert actual is not None, renderer.last_fallback_reason
        delta = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        # OpenCV quantizes affine interpolation to 1/32 pixel; Metal samples
        # with full precision, so allow a small boundary difference.
        assert float(delta.mean()) <= 1.0
        assert int(delta.max()) <= 12
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_h264_frame_planes_match_opengl_with_primary_grade():
    """Exercise MSL compilation/dispatch and compare encoder planes to GL."""
    from src.features.edit.core.timeline_gpu_export_renderer import (
        TimelineGpuExportRenderer,
        grade_params_to_uniforms,
    )

    rng = np.random.default_rng(13)
    source = rng.integers(0, 256, (64, 96, 3), dtype=np.uint8)
    grade = grade_params_to_uniforms({
        "exposure": 12.0,
        "temp": -5.0,
        "tint": 2.0,
        "contrast": 8.0,
        "shadow": 3.0,
        "highlights": -4.0,
        "saturation": 10.0,
        "vibrance": 6.0,
        "hue_shift": 15.0,
    })
    metal = TimelineMetalExportRenderer()
    opengl = TimelineGpuExportRenderer()
    try:
        metal_result = metal.render_packed_bgr(
            source, grade, 80, 48, 0.83, output_yuv420p=True)
        assert metal_result is not None, "Metal kernel failed to produce encoder planes"
        gl_result = opengl.render_packed_bgr(
            source, grade, 80, 48, 0.83, output_yuv420p=True)
        if gl_result is None:
            pytest.skip("OpenGL standalone render context unavailable on this host")
        for metal_plane, gl_plane in zip(metal_result.planes, gl_result.planes):
            difference = np.abs(
                metal_plane.astype(np.int16) - gl_plane.astype(np.int16))
            assert int(difference.max()) <= 1
    finally:
        metal.release()
        opengl.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_two_layer_stack_matches_opengl_and_reuses_inputs():
    from src.features.edit.core.timeline_gpu_export_renderer import (
        TimelineGpuExportRenderer,
    )

    rng = np.random.default_rng(31)
    bottom = rng.integers(0, 256, (64, 96, 3), dtype=np.uint8)
    top = rng.integers(0, 256, (48, 80, 3), dtype=np.uint8)
    layers = [(bottom, None, 0.37, 0), (top, None, 0.63, 0)]
    metal = TimelineMetalExportRenderer()
    opengl = TimelineGpuExportRenderer()
    try:
        metal_result = metal.render_layer_stack(layers, 80, 48)
        gl_result = opengl.render_layer_stack(layers, 80, 48)
        assert metal_result is not None
        if gl_result is None:
            pytest.skip("OpenGL standalone render context unavailable on this host")
        difference = np.abs(metal_result.astype(np.int16) - gl_result.astype(np.int16))
        assert int(difference.max()) <= 1

        # Re-upload different content through the same per-layer buffers.
        replacement = np.full_like(top, (17, 131, 239))
        updated = metal.render_layer_stack(
            [(bottom, None, 0.37, 0), (replacement, None, 0.63, 0)], 80, 48)
        expected = opengl.render_layer_stack(
            [(bottom, None, 0.37, 0), (replacement, None, 0.63, 0)], 80, 48)
        assert updated is not None and expected is not None
        assert int(np.abs(updated.astype(np.int16) - expected.astype(np.int16)).max()) <= 1
    finally:
        metal.release()
        opengl.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_spatial_wipe_matches_opengl_for_first_and_later_layers():
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuWipeElement,
        TimelineGpuExportRenderer,
    )

    rng = np.random.default_rng(307)
    bottom = rng.integers(0, 256, (32, 48, 3), dtype=np.uint8)
    left = rng.integers(0, 256, (24, 40, 3), dtype=np.uint8)
    right = rng.integers(0, 256, (40, 56, 3), dtype=np.uint8)
    mask = rng.random((32, 48), dtype=np.float32)
    wipe = GpuWipeElement(
        left=left, left_grade=None,
        right=right, right_grade=None,
        mask=mask, left_opacity=0.63, right_opacity=0.84,
        opacity=0.47,
    )
    metal = TimelineMetalExportRenderer()
    opengl = TimelineGpuExportRenderer()
    try:
        cases = ([wipe], [(bottom, None, 0.72, 0), wipe])
        for layers in cases:
            expected = opengl.render_layer_stack(layers, 48, 32)
            if expected is None:
                pytest.skip("OpenGL standalone render context unavailable")
            actual = metal.render_layer_stack(layers, 48, 32)
            assert actual is not None, metal.last_fallback_reason
            difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
            assert int(difference.max()) <= 1
    finally:
        metal.release()
        opengl.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_procedural_transition_mask_matches_cpu_mask():
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuWipeElement, TimelineGpuExportRenderer,
    )
    from src.features.edit.core.transition_mask import compute_transition_mask
    from src.features.edit.core.transition_mask_gpu import (
        build_gpu_transition_mask_spec,
    )
    width, height = 64, 40
    rng = np.random.default_rng(811)
    left = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    right = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    transition_params = {"direction": "angle", "angle": 32.0, "softness": 0.04}
    progress = 0.47
    cpu_mask = compute_transition_mask(
        width, height, progress, "wipe", transition_params)
    spec = build_gpu_transition_mask_spec(
        "wipe", progress, transition_params)
    assert spec is not None
    wipe_cpu = GpuWipeElement(
        left=left, left_grade=None, right=right, right_grade=None,
        mask=cpu_mask, left_opacity=0.63, right_opacity=0.84, opacity=0.47,
    )
    wipe_gpu = GpuWipeElement(
        left=left, left_grade=None, right=right, right_grade=None,
        mask=None, left_opacity=0.63, right_opacity=0.84, opacity=0.47,
        mask_spec=spec,
    )
    metal = TimelineMetalExportRenderer()
    opengl = TimelineGpuExportRenderer()
    try:
        expected = opengl.render_layer_stack([wipe_cpu], width, height)
        actual = metal.render_layer_stack([wipe_gpu], width, height)
        if expected is None:
            pytest.skip("OpenGL standalone render context unavailable")
        assert actual is not None, metal.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert int(difference.max()) <= 1
    finally:
        metal.release()
        opengl.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_stack_reuses_scratch_between_separable_effect_layers():
    from src.features.edit.core.timeline_export import _cpu_blur

    rng = np.random.default_rng(41)
    bottom = rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)
    top = rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)
    radius = 8.0
    sigma = 0.3 * (radius - 1.0) + 0.8
    effect_params = {
        "kind": 2.0, "half_x": radius, "half_y": radius,
        "strength": 1.0, "sigma_x": sigma, "sigma_y": sigma,
    }
    cpu_params = {
        "blur_type": "gaussian", "radius_x": radius,
        "radius_y": radius, "mix": 1.0,
    }
    bottom_blur = _cpu_blur(bottom, cpu_params).astype(np.float32)
    top_blur = _cpu_blur(top, cpu_params).astype(np.float32)
    expected = np.rint(bottom_blur * 0.5 + top_blur * 0.5).astype(np.uint8)

    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [
                (bottom, None, 1.0, 0, effect_params),
                (top, None, 0.5, 0, effect_params),
            ],
            64,
            48,
        )
        assert actual is not None, renderer.last_fallback_reason
        assert int(np.abs(actual.astype(np.int16) - expected.astype(np.int16)).max()) <= 2
        # T01-c（2026-10-14 复查）：radius>1 走物化链路径，scratch 键为
        # stack_effect_chain_horizontal（旧断言要求的 stack_effect_horizontal
        # 已被分离链实现取代）。同时验证跨 layer 真实复用：同一对象在
        # 两层间不重复分配（对象身份一致）。
        assert "stack_effect_chain_horizontal" in renderer._buffers
        chain_buf = renderer._buffers["stack_effect_chain_horizontal"]
        # 渲染第二层时 scratch 仍是同一对象（层间复用而非重建）
        actual2 = renderer.render_layer_stack(
            [(top, None, 1.0, 0, effect_params)], 64, 48)
        assert actual2 is not None
        assert renderer._buffers.get("stack_effect_chain_horizontal") is chain_buf
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_layer_stack_keeps_zero_opacity_and_yuv_output_semantics():
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuv420Output,
        TimelineGpuExportRenderer,
    )

    bottom = np.full((48, 64, 3), (20, 90, 180), dtype=np.uint8)
    top = np.full((48, 64, 3), (230, 40, 5), dtype=np.uint8)
    metal = TimelineMetalExportRenderer()
    opengl = TimelineGpuExportRenderer()
    try:
        layers = [(bottom, None, 0.5, 0), (top, None, 0.0, 0)]
        metal_rgb = metal.render_layer_stack(layers, 64, 48)
        gl_rgb = opengl.render_layer_stack(layers, 64, 48)
        assert metal_rgb is not None and gl_rgb is not None
        assert int(np.abs(metal_rgb.astype(np.int16) - gl_rgb.astype(np.int16)).max()) <= 1

        metal_yuv = metal.render_layer_stack(
            layers, 64, 48, output_yuv420p=True)
        gl_yuv = opengl.render_layer_stack(
            layers, 64, 48, output_yuv420p=True)
        assert isinstance(metal_yuv, GpuYuv420Output)
        assert isinstance(gl_yuv, GpuYuv420Output)
        for actual, expected in zip(metal_yuv.planes, gl_yuv.planes):
            assert int(np.abs(actual.astype(np.int16) - expected.astype(np.int16)).max()) <= 1
    finally:
        metal.release()
        opengl.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize("blend_mode", range(1, 16))
def test_real_metal_layer_blend_modes_match_cpu(blend_mode):
    from src.features.edit.core.blend_modes import blend_rgb

    rng = np.random.default_rng(2500 + blend_mode)
    bottom = rng.integers(8, 248, (17, 21, 3), dtype=np.uint8)
    top = rng.integers(8, 248, (17, 21, 3), dtype=np.uint8)
    opacity = 0.63
    base_rgb = bottom[..., ::-1].astype(np.float32) / 255.0
    source_rgb = top[..., ::-1].astype(np.float32) / 255.0
    blended_bgr = blend_rgb(base_rgb, source_rgb, blend_mode)[..., ::-1]
    expected = np.rint(
        (bottom.astype(np.float32) / 255.0 * (1.0 - opacity)
         + blended_bgr * opacity) * 255.0
    ).astype(np.uint8)

    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(bottom, None, 1.0, 0), (top, None, opacity, blend_mode)],
            21, 17)
        assert actual is not None, renderer.last_fallback_reason
        delta = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(delta.mean()) <= 0.12
        assert int(delta.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("effect_params", "cpu_params"),
    (
        ({"kind": 1.0, "half_x": 3.0, "half_y": 2.0,
          "strength": 0.75, "sigma_x": 1.0, "sigma_y": 1.0},
         {"blur_type": "box", "radius_x": 3.0, "radius_y": 2.0,
          "mix": 0.75}),
        ({"kind": 2.0, "half_x": 3.0, "half_y": 2.0,
          "strength": 1.0, "sigma_x": 1.4, "sigma_y": 1.1},
         {"blur_type": "gaussian", "radius_x": 3.0, "radius_y": 2.0,
          "mix": 1.0}),
        ({"kind": 2.0, "half_x": 16.0, "half_y": 16.0,
          "strength": 1.0, "sigma_x": 5.3, "sigma_y": 5.3},
         {"blur_type": "gaussian", "radius_x": 16.0,
          "radius_y": 16.0, "mix": 1.0}),
        ({"kind": 3.0, "half_x": 1.0, "half_y": 1.0,
          "strength": 1.0, "sigma_x": 0.5, "sigma_y": 0.5},
         {"amount": 1.0, "radius": 1.0}),
        ({"kind": 3.0, "half_x": 2.0, "half_y": 2.0,
          "strength": 1.7, "sigma_x": 1.0, "sigma_y": 1.0,
          "threshold": 0.2, "channels": 1.0},
         {"amount": 1.7, "radius": 2.0, "threshold": 0.2,
          "channels": "rgb"}),
        ({"kind": 3.0, "half_x": 4.0, "half_y": 4.0,
          "strength": 1.2, "sigma_x": 2.0, "sigma_y": 2.0,
          "threshold": 0.08, "channels": 2.0},
         {"amount": 1.2, "radius": 4.0, "threshold": 0.08,
          "channels": "luminance"}),
        ({"kind": 3.0, "half_x": 16.0, "half_y": 16.0,
          "strength": 1.0, "sigma_x": 8.0, "sigma_y": 8.0},
         {"amount": 1.0, "radius": 16.0}),
        ({"kind": 4.0, "half_x": 16.0, "half_y": 16.0,
          "strength": 0.8, "sigma_x": 1.5, "sigma_y": 1.5,
          "threshold": 0.7, "intensity": 0.6,
          "tint_r": 1.0, "tint_g": 0.45, "tint_b": 0.8},
         {"threshold": 0.7, "radius": 3.0,
          "intensity": 0.6, "mix": 0.8,
          "tint_r": 1.0, "tint_g": 0.45, "tint_b": 0.8}),
        ({"kind": 4.0, "half_x": 16.0, "half_y": 16.0,
          "strength": 1.0, "sigma_x": 8.0, "sigma_y": 8.0,
          "threshold": 0.65, "intensity": 0.8,
          "tint_r": 0.8, "tint_g": 1.2, "tint_b": 0.6},
         {"threshold": 0.65, "radius": 16.0,
          "intensity": 0.8, "mix": 1.0,
          "tint_r": 0.8, "tint_g": 1.2, "tint_b": 0.6}),
        ({"kind": 4.0, "half_x": 32.0, "half_y": 32.0,
          "strength": 0.9, "sigma_x": 9.0, "sigma_y": 9.0,
          "threshold": 0.6, "intensity": 1.2,
          "tint_r": 1.1, "tint_g": 0.9, "tint_b": 0.5},
         {"threshold": 0.6, "radius": 18.0,
          "intensity": 1.2, "mix": 0.9,
          "tint_r": 1.1, "tint_g": 0.9, "tint_b": 0.5,
          "quality": "high"}),
    ),
)
def test_real_metal_blur_and_sharpen_match_cpu_export(
    effect_params, cpu_params,
):
    from src.features.edit.core.timeline_export import (
        _cpu_glow,
        _cpu_blur,
        _cpu_sharpen,
    )

    rng = np.random.default_rng(79)
    source = rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)
    effect_kind = int(effect_params["kind"])
    if effect_kind == 3:
        cpu_reference = _cpu_sharpen(source, cpu_params)
    elif effect_kind == 4:
        cpu_reference = _cpu_glow(source, cpu_params)
    else:
        cpu_reference = _cpu_blur(source, cpu_params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect_params)], 64, 48)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - cpu_reference.astype(np.int16))
        assert float(difference.mean()) <= 1.25
        assert int(difference.max()) <= 5
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("operation", "size", "iterations"),
    (("erode", 3, 1), ("dilate", 5, 2), ("erode", 35, 1),
     ("dilate", 50, 10)),
)
def test_real_metal_erode_dilate_matches_cpu_export(
    operation, size, iterations,
):
    from src.features.edit.core.timeline_export import _cpu_erode_dilate

    rng = np.random.default_rng(1807 + size + iterations)
    source = rng.integers(0, 256, (37, 53, 3), dtype=np.uint8)
    params = {
        "operation": operation, "size": size,
        "iterations": iterations, "channels": "all",
        "edge_mode": "extend",
    }
    half_size = (size // 2) * iterations
    effect = {
        "kind": 5.0, "half_x": float(half_size),
        "half_y": float(half_size), "strength": 1.0,
        "sigma_x": 1.0, "sigma_y": 1.0,
        "operation": float(operation == "dilate"),
    }
    expected = _cpu_erode_dilate(source, params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 53, 37)
        assert actual is not None, renderer.last_fallback_reason
        assert np.array_equal(actual, expected)
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("direction", "output_mode", "invert", "strength"),
    (("both", "edges", False, 1.0),
     ("horizontal", "gradient", False, 1.5),
     ("vertical", "direction", True, 0.75)),
)
def test_real_metal_sobel_edge_detect_matches_cpu_export(
    direction, output_mode, invert, strength,
):
    from src.features.edit.core.timeline_export import _cpu_edge_detect

    rng = np.random.default_rng(961 + len(direction) + len(output_mode))
    source = rng.integers(0, 256, (39, 57, 3), dtype=np.uint8)
    params = {
        "method": "sobel", "threshold": 0.17,
        "threshold_high": 0.3, "direction": direction,
        "output_mode": output_mode, "invert": invert,
        "output_strength": strength, "kernel_size": 3,
        "preblur": 0.0, "channel": "luminance",
    }
    effect = {
        "kind": 6.0, "half_x": 1.0, "half_y": 1.0,
        "strength": strength, "sigma_x": params["threshold"],
        "sigma_y": params["threshold_high"],
        "direction": float(("both", "horizontal", "vertical").index(direction)),
        "output_mode": float(("edges", "gradient", "direction").index(output_mode)),
        "invert": float(invert),
    }
    expected = _cpu_edge_detect(source, params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 57, 39)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.1
        assert int(difference.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("method", "kernel_size", "output_mode"),
    (("sobel", 5, "gradient"), ("canny", 5, "edges"),
     ("laplacian", 5, "gradient"), ("prewitt", 7, "direction")),
)
def test_real_metal_edge_detect_all_kernel_sizes_match_cpu(
    method, kernel_size, output_mode,
):
    from src.features.edit.core.timeline_export import _cpu_edge_detect

    rng = np.random.default_rng(5150 + kernel_size + len(method))
    source = rng.integers(0, 256, (31, 43, 3), dtype=np.uint8)
    params = {
        "method": method, "threshold": 0.17, "threshold_high": 0.33,
        "direction": "both", "output_mode": output_mode,
        "channel": "luminance", "kernel_size": kernel_size,
        "preblur": 0.0, "output_strength": 1.25,
    }
    expected = _cpu_edge_detect(source, params)
    effect = {
        "kind": 6.0, "half_x": float(kernel_size), "half_y": 1.0,
        "strength": 1.25, "sigma_x": 0.17, "sigma_y": 0.33,
        "direction": 0.0,
        "output_mode": float(("edges", "gradient", "direction").index(
            output_mode)),
        "invert": 0.0,
        "channel": 0.0,
        "method": float(("sobel", "canny", "prewitt", "laplacian").index(
            method)),
        "preblur": 0.0,
    }
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 43, 31)
        assert actual is not None, renderer.last_fallback_reason
        delta = np.abs(actual.astype(np.int16) - expected[..., :3].astype(np.int16))
        assert float(delta.mean()) <= 0.1
        assert int(delta.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("output_mode", "threshold", "invert"),
    (("edges", 0.2, False), ("edges", 0.0, False),
     ("gradient", 0.2, True), ("direction", 0.2, False)),
)
def test_real_metal_sobel_alpha_channel_matches_opaque_cpu_reference(
    output_mode, threshold, invert,
):
    from src.features.edit.core.timeline_export import _cpu_edge_detect

    rng = np.random.default_rng(11261)
    source = rng.integers(0, 256, (17, 25, 3), dtype=np.uint8)
    params = {
        "method": "sobel", "threshold": threshold,
        "threshold_high": 0.3, "direction": "both",
        "output_mode": output_mode, "invert": invert,
        "output_strength": 1.0, "kernel_size": 3,
        "preblur": 0.0, "channel": "a",
    }
    effect = {
        "kind": 6.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 1.0, "sigma_x": threshold, "sigma_y": 0.3,
        "direction": 0.0,
        "output_mode": float(("edges", "gradient", "direction").index(
            output_mode)),
        "invert": float(invert), "channel": 1.0,
    }
    expected = _cpu_edge_detect(source, params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 25, 17)
        assert actual is not None, renderer.last_fallback_reason
        assert np.array_equal(actual, expected)
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(("channel", "channel_code"),
                         (("r", 2.0), ("g", 3.0), ("b", 4.0)))
def test_real_metal_sobel_color_channel_matches_cpu_export(channel, channel_code):
    from src.features.edit.core.timeline_export import _cpu_edge_detect

    rng = np.random.default_rng(11479)
    source = rng.integers(0, 256, (21, 29, 3), dtype=np.uint8)
    params = {
        "method": "sobel", "threshold": 0.13,
        "threshold_high": 0.3, "direction": "both",
        "output_mode": "edges", "invert": False,
        "output_strength": 1.0, "kernel_size": 3,
        "preblur": 0.0, "channel": channel,
    }
    effect = {
        "kind": 6.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 1.0, "sigma_x": 0.13, "sigma_y": 0.3,
        "direction": 0.0, "output_mode": 0.0,
        "invert": 0.0, "channel": channel_code, "canny": 0.0,
    }
    expected = _cpu_edge_detect(source, params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 29, 21)
        assert actual is not None, renderer.last_fallback_reason
        assert np.array_equal(actual, expected)
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("output_mode", "threshold", "threshold_high", "invert"),
    (("edges", 0.17, 0.36, False), ("edges", 0.0, 0.4, True),
     ("gradient", 0.17, 0.36, False), ("direction", 0.17, 0.36, True)),
)
def test_real_metal_canny_double_threshold_matches_cpu_export(
    output_mode, threshold, threshold_high, invert,
):
    from src.features.edit.core.timeline_export import _cpu_edge_detect

    rng = np.random.default_rng(12389)
    source = rng.integers(0, 256, (23, 35, 3), dtype=np.uint8)
    params = {
        "method": "canny", "threshold": threshold,
        "threshold_high": threshold_high, "direction": "both",
        "output_mode": output_mode, "invert": invert,
        "output_strength": 1.0, "kernel_size": 3,
        "preblur": 0.0, "channel": "luminance",
    }
    effect = {
        "kind": 6.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 1.0, "sigma_x": threshold,
        "sigma_y": threshold_high, "direction": 0.0,
        "output_mode": float(("edges", "gradient", "direction").index(
            output_mode)),
        "invert": float(invert), "channel": 0.0, "canny": 1.0,
    }
    expected = _cpu_edge_detect(source, params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 35, 23)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.1
        assert int(difference.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("method", "method_code", "output_mode"),
    (("prewitt", 2.0, "edges"), ("prewitt", 2.0, "gradient"),
     ("laplacian", 3.0, "edges"), ("laplacian", 3.0, "gradient"),
     ("laplacian", 3.0, "direction")),
)
def test_real_metal_prewitt_and_laplacian_match_cpu_export(
    method, method_code, output_mode,
):
    from src.features.edit.core.timeline_export import _cpu_edge_detect

    rng = np.random.default_rng(12631)
    source = rng.integers(0, 256, (23, 35, 3), dtype=np.uint8)
    params = {
        "method": method, "threshold": 0.14,
        "threshold_high": 0.3, "direction": "vertical",
        "output_mode": output_mode, "invert": False,
        "output_strength": 1.0, "kernel_size": 3,
        "preblur": 0.0, "channel": "luminance",
    }
    effect = {
        "kind": 6.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 1.0, "sigma_x": 0.14, "sigma_y": 0.3,
        "direction": 2.0,
        "output_mode": float(("edges", "gradient", "direction").index(
            output_mode)),
        "invert": 0.0, "channel": 0.0, "method": method_code,
        "preblur": 0.0,
    }
    expected = _cpu_edge_detect(source, params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 35, 23)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.1
        assert int(difference.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
# Fractional preblur stays float32 through the Metal Sobel pass. Metal and
# NumPy/OpenCV may round a final code point differently; bound the mean error
# below 0.4 code points and every pixel to at most two 8-bit code points.
@pytest.mark.parametrize(
    ("channel", "channel_code"),
    (
        pytest.param("luminance", 0.0),
        pytest.param("g", 3.0),
        pytest.param("a", 1.0),
    ))
def test_real_metal_sobel_fractional_preblur_matches_cpu_export(
    channel, channel_code,
):
    from src.features.edit.core.timeline_export import _cpu_edge_detect

    rng = np.random.default_rng(13441)
    source = rng.integers(0, 256, (23, 37, 3), dtype=np.uint8)
    params = {
        "method": "sobel", "threshold": 0.2,
        "threshold_high": 0.35, "direction": "both",
        "output_mode": "gradient", "invert": False,
        "output_strength": 1.0, "kernel_size": 3,
        "preblur": 1.3, "channel": channel,
    }
    effect = {
        "kind": 6.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 1.0, "sigma_x": 0.2, "sigma_y": 0.35,
        "direction": 0.0, "output_mode": 1.0,
        "invert": 0.0, "channel": channel_code, "canny": 0.0,
        "preblur": 1.3,
    }
    expected = _cpu_edge_detect(source, params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 37, 23)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.4
        assert int(difference.max()) <= 2
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("luminance_only", "chroma_only", "radius"),
    ((False, False, 2), (True, False, 3),
     (False, True, 2), (True, True, 1), (False, False, 10)),
)
def test_real_metal_denoise_matches_cpu_export(
    luminance_only, chroma_only, radius,
):
    from src.features.edit.core.timeline_export import _cpu_denoise

    rng = np.random.default_rng(10103 + radius)
    source = rng.integers(0, 256, (17, 23, 3), dtype=np.uint8)
    cpu_params = {
        "strength": 0.63, "radius": radius,
        "luminance_only": luminance_only,
        "chroma_only": chroma_only,
    }
    spatial_sigma = max(radius * 0.4, 1.0)
    effect = {
        "kind": 7.0, "half_x": float(radius), "half_y": float(radius),
        "strength": 0.63, "sigma_x": spatial_sigma,
        "sigma_y": max(0.1, 0.15 * (1.0 - 0.63 * 0.5)),
        "spatial_sigma": spatial_sigma,
        "range_sigma": max(0.1, 0.15 * (1.0 - 0.63 * 0.5)),
        "luminance_only": float(luminance_only),
        "chroma_only": float(chroma_only),
    }
    expected = _cpu_denoise(source, cpu_params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 23, 17)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.25
        assert int(difference.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("edge_preserve", "mix", "channel_mask"),
    ((0.4, 0.75, (True, True, True)),
     (0.9, 0.5, (False, True, True))),
)
def test_real_metal_bilateral_blur_matches_cpu_export(
    edge_preserve, mix, channel_mask,
):
    from src.features.edit.core.timeline_export import _cpu_blur

    rng = np.random.default_rng(16081)
    source = rng.integers(0, 256, (19, 27, 3), dtype=np.uint8)
    sigma_color = max(5.0, 80.0 * (1.0 - edge_preserve))
    cpu_params = {
        "blur_type": "bilateral", "radius": 5.0,
        "edge_preserve": edge_preserve, "mix": mix,
        "channel_r": channel_mask[0], "channel_g": channel_mask[1],
        "channel_b": channel_mask[2],
    }
    effect = {
        "kind": 9.0, "half_x": 5.0, "half_y": 5.0,
        "strength": mix, "sigma_x": 5.0,
        "sigma_y": sigma_color / 255.0,
        "spatial_sigma": 5.0, "range_sigma": sigma_color / 255.0,
        "channel_r": float(channel_mask[0]),
        "channel_g": float(channel_mask[1]),
        "channel_b": float(channel_mask[2]),
    }
    expected = _cpu_blur(source, cpu_params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 27, 19)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.5
        assert int(difference.max()) <= 3
    finally:
        renderer.release()


def _blur_shader_reference(source_bgr, effect):
    """Small OpenCV reference for the shared GLSL/Metal single-pass Blur math."""
    import cv2

    height, width = source_bgr.shape[:2]
    source = source_bgr[:, :, ::-1].astype(np.float32)
    grid_y, grid_x = np.mgrid[0:height, 0:width].astype(np.float32)
    edge_policy = int(effect["edge_policy"])
    border = {
        0: cv2.BORDER_REPLICATE,
        1: cv2.BORDER_WRAP,
        2: cv2.BORDER_REFLECT,
    }[edge_policy]

    def sample(dx, dy):
        return cv2.remap(
            source, grid_x + np.float32(dx), grid_y + np.float32(dy),
            cv2.INTER_LINEAR, borderMode=border,
        )

    count = int(effect["sample_count"])
    radius = float(effect["half_x"])
    total = np.zeros_like(source)
    weight_sum = np.zeros((height, width), dtype=np.float32)
    if int(effect["kind"]) == 10:
        direction_x = np.cos(float(effect["sigma_x"]))
        direction_y = np.sin(float(effect["sigma_x"]))
        scale = np.array((width, height), dtype=np.float32) / max(width, height)
        for index in range(count):
            t = (index / (count - 1)) * 2.0 - 1.0
            distance = t * radius * float(effect["half_y"])
            total += sample(direction_x * scale[0] * distance,
                            direction_y * scale[1] * distance)
            weight_sum += 1.0
    else:
        blades = int(effect["half_y"])
        anamorphic = float(effect["anamorphic"])
        highlight_gain = float(effect["highlight_gain"])
        aspect = (1.0 + anamorphic * 1.5,
                  1.0 / (1.0 + anamorphic * 1.5))
        golden = 2.39996323
        segment = 2.0 * np.pi / max(blades, 1)
        for index in range(count):
            fi = index + 0.5
            r = np.sqrt(fi / count)
            theta = fi * golden
            dx, dy = np.cos(theta) * r, np.sin(theta) * r
            if blades > 0:
                angle = np.fmod(np.arctan2(dy, dx), segment)
                if angle < 0.0:
                    angle += segment
                poly_r = np.cos(segment * 0.5) / max(
                    np.cos(angle - segment * 0.5), 0.0001)
                dx *= np.clip(poly_r, 0.0, 1.0)
                dy *= np.clip(poly_r, 0.0, 1.0)
            current = sample(dx * aspect[0] * radius,
                             dy * aspect[1] * radius)
            luma = (current[..., 0] * 0.2126
                    + current[..., 1] * 0.7152
                    + current[..., 2] * 0.0722) / 255.0
            weight = (np.power(np.maximum(luma, 0.0), highlight_gain) + 1.0
                      if highlight_gain > 0.001 else 1.0)
            total += current * weight[..., None]
            weight_sum += weight
    blurred = total / weight_sum[..., None]
    mask = np.array((effect["channel_r"], effect["channel_g"],
                     effect["channel_b"]), dtype=np.float32)
    result_rgb = source + (blurred - source) * (
        mask * float(effect["strength"]))
    return np.rint(np.clip(result_rgb[..., ::-1], 0.0, 255.0)).astype(np.uint8)


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize("effect", (
    {
        "kind": 10.0, "half_x": 5.0, "half_y": 1.25,
        "strength": 0.7, "sigma_x": 0.71, "sigma_y": 1.25,
        "edge_policy": 0.0, "sample_count": 32.0,
        "channel_r": 1.0, "channel_g": 0.0, "channel_b": 1.0,
    },
    {
        "kind": 11.0, "half_x": 5.0, "half_y": 6.0,
        "strength": 0.8, "anamorphic": 0.35,
        "highlight_gain": 1.1, "edge_policy": 2.0,
        "sample_count": 16.0, "aperture_blades": 6.0,
        "channel_r": 1.0, "channel_g": 1.0, "channel_b": 1.0,
    },
))
def test_real_metal_motion_and_bokeh_blur_match_shader_reference(effect):
    rng = np.random.default_rng(36127)
    source = rng.integers(0, 256, (27, 41, 3), dtype=np.uint8)
    expected = _blur_shader_reference(source, effect)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 41, 27)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.35
        assert int(difference.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_bilateral_blur_runs_inside_ordered_effect_chain():
    from src.features.edit.core.timeline_export import _cpu_blur, _cpu_sharpen

    rng = np.random.default_rng(17131)
    source = rng.integers(0, 256, (23, 31, 3), dtype=np.uint8)
    blur_cpu = {
        "blur_type": "bilateral", "radius": 4.0,
        "edge_preserve": 0.65, "mix": 0.8,
    }
    blur = {
        "kind": 9.0, "half_x": 4.0, "half_y": 4.0,
        "strength": 0.8, "sigma_x": 4.0,
        "sigma_y": (80.0 * (1.0 - 0.65)) / 255.0,
        "spatial_sigma": 4.0,
        "range_sigma": (80.0 * (1.0 - 0.65)) / 255.0,
        "channel_r": 1.0, "channel_g": 1.0, "channel_b": 1.0,
    }
    sharpen_cpu = {"amount": 0.55, "radius": 1.0,
                   "threshold": 0.0, "channels": "rgb"}
    sharpen = {
        "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 0.55, "sigma_x": 0.5, "sigma_y": 0.5,
        "threshold": 0.0, "channels": 1.0,
    }
    expected = _cpu_sharpen(_cpu_blur(source, blur_cpu), sharpen_cpu)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, (blur, sharpen))], 31, 23)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.75
        assert int(difference.max()) <= 4
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize("edge_policy", ("clamp", "wrap", "mirror"))
def test_real_metal_defocus_matches_cpu_export(edge_policy):
    from src.features.edit.core.timeline_export import _cpu_defocus

    rng = np.random.default_rng(10927)
    source = rng.integers(0, 256, (23, 37, 3), dtype=np.uint8)
    params = {
        "mode": "separable", "focal_x": 0.42, "focal_y": 0.57,
        "focal_size": 0.24, "max_radius": 18.0,
        "edge_policy": edge_policy, "process_alpha": True,
    }
    effect = {
        "kind": 8.0, "half_x": 15.0, "half_y": 0.0,
        "strength": 1.0, "sigma_x": params["focal_x"],
        "sigma_y": params["focal_y"],
        "focal_x": params["focal_x"], "focal_y": params["focal_y"],
        "focal_size": params["focal_size"],
        "max_radius": params["max_radius"],
        "edge_policy": float(("clamp", "wrap", "mirror").index(edge_policy)),
        "mode": 0.0, "process_alpha": 1.0,
    }
    expected = _cpu_defocus(source, params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 37, 23)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.2
        assert int(difference.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_defocus_mipmap_matches_cpu_export():
    from src.features.edit.core.timeline_export import _cpu_defocus

    rng = np.random.default_rng(10931)
    source = rng.integers(0, 256, (27, 39, 3), dtype=np.uint8)
    params = {
        "mode": "mipmap", "focal_x": 0.43, "focal_y": 0.55,
        "focal_size": 0.18, "max_radius": 40.0,
        "edge_policy": "mirror", "process_alpha": True,
    }
    effect = {
        "kind": 8.0, "half_x": 15.0, "half_y": 0.0,
        "strength": 1.0, "sigma_x": params["focal_x"],
        "sigma_y": params["focal_y"],
        "focal_x": params["focal_x"], "focal_y": params["focal_y"],
        "focal_size": params["focal_size"],
        "max_radius": params["max_radius"],
        "edge_policy": 2.0, "mode": 1.0, "process_alpha": 1.0,
    }
    expected = _cpu_defocus(source, params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 39, 27)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.5
        assert int(difference.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_defocus_runs_in_ordered_effect_chain():
    from src.features.edit.core.timeline_export import _cpu_defocus, _cpu_sharpen

    rng = np.random.default_rng(10929)
    source = rng.integers(0, 256, (19, 29, 3), dtype=np.uint8)
    defocus_params = {
        "mode": "separable", "focal_x": 0.5, "focal_y": 0.5,
        "focal_size": 0.15, "max_radius": 15.0,
        "edge_policy": "clamp", "process_alpha": True,
    }
    defocus = {
        "kind": 8.0, "half_x": 15.0, "half_y": 0.0,
        "strength": 1.0, "sigma_x": 0.5, "sigma_y": 0.5,
        "focal_x": 0.5, "focal_y": 0.5,
        "focal_size": 0.15, "max_radius": 15.0,
        "edge_policy": 0.0, "mode": 0.0, "process_alpha": 1.0,
    }
    sharpen = {
        "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 0.4, "sigma_x": 0.5, "sigma_y": 0.5,
        "threshold": 0.0, "channels": 1.0,
    }
    expected = _cpu_defocus(source, defocus_params)
    expected = _cpu_sharpen(expected, {
        "amount": 0.4, "radius": 1.0,
        "threshold": 0.0, "channels": "rgb",
    })
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, (defocus, sharpen))], 29, 19)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.3
        assert int(difference.max()) <= 1
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_morphology_runs_in_ordered_effect_chain():
    from src.features.edit.core.timeline_export import (
        _cpu_erode_dilate, _cpu_sharpen,
    )

    rng = np.random.default_rng(7419)
    source = rng.integers(0, 256, (31, 47, 3), dtype=np.uint8)
    morphology = {
        "kind": 5.0, "half_x": 2.0, "half_y": 2.0,
        "strength": 1.0, "sigma_x": 1.0, "sigma_y": 1.0,
        "operation": 1.0,
    }
    sharpen = {
        "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 0.65, "sigma_x": 0.5, "sigma_y": 0.5,
        "threshold": 0.0, "channels": 1.0,
    }
    cpu = _cpu_erode_dilate(source, {
        "operation": "dilate", "size": 5, "iterations": 1,
        "channels": "all", "edge_mode": "extend",
    })
    expected = _cpu_sharpen(cpu, {
        "amount": 0.65, "radius": 1.0,
        "threshold": 0.0, "channels": "rgb",
    })
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, (morphology, sharpen))], 47, 31)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.5
        assert int(difference.max()) <= 2
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(("edge_mode", "edge_policy"), (
    ("extend", 0.0), ("wrap", 1.0), ("reflect", 2.0),
))
@pytest.mark.parametrize("operation", ("erode", "dilate"))
def test_real_metal_morphology_border_modes_match_cpu(edge_mode, edge_policy, operation):
    from src.features.edit.core.timeline_export import _cpu_erode_dilate

    rng = np.random.default_rng(7211)
    source = rng.integers(0, 256, (19, 31, 3), dtype=np.uint8)
    cpu_params = {
        "operation": operation, "size": 5, "iterations": 1,
        "channels": "all", "edge_mode": edge_mode,
    }
    effect = {
        "kind": 5.0, "half_x": 2.0, "half_y": 2.0,
        "strength": 1.0, "sigma_x": 1.0, "sigma_y": 1.0,
        "operation": float(operation == "dilate"),
        "edge_policy": edge_policy,
    }
    expected = _cpu_erode_dilate(source, cpu_params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 31, 19)
        assert actual is not None, renderer.last_fallback_reason
        assert np.array_equal(actual, expected)
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_edge_detect_runs_in_ordered_effect_chain():
    from src.features.edit.core.timeline_export import _cpu_edge_detect

    rng = np.random.default_rng(12631)
    source = rng.integers(0, 256, (29, 43, 3), dtype=np.uint8)
    first_params = {
        "method": "sobel", "threshold": 0.2,
        "direction": "both", "output_mode": "edges",
        "output_strength": 1.0, "kernel_size": 3,
        "preblur": 0.0, "channel": "luminance",
    }
    second_params = {
        "method": "sobel", "threshold": 0.2,
        "direction": "both", "output_mode": "gradient",
        "output_strength": 1.0, "kernel_size": 3,
        "preblur": 0.0, "channel": "luminance",
    }
    first = {
        "kind": 6.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 1.0, "sigma_x": 0.2, "sigma_y": 0.3,
        "direction": 0.0, "output_mode": 0.0, "invert": 0.0,
    }
    second = {**first, "output_mode": 1.0}
    expected = _cpu_edge_detect(source, first_params)
    expected = _cpu_edge_detect(expected, second_params)
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, (first, second))], 43, 29)
        assert actual is not None, renderer.last_fallback_reason
        assert np.array_equal(actual, expected)
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_denoise_runs_in_ordered_effect_chain():
    from src.features.edit.core.timeline_export import (
        _cpu_denoise, _cpu_sharpen,
    )

    rng = np.random.default_rng(17819)
    source = rng.integers(0, 256, (27, 41, 3), dtype=np.uint8)
    denoise_params = {
        "strength": 0.55, "radius": 2,
        "luminance_only": False, "chroma_only": False,
    }
    denoise = {
        "kind": 7.0, "half_x": 2.0, "half_y": 2.0,
        "strength": 0.55, "sigma_x": 1.0,
        "sigma_y": max(0.1, 0.15 * (1.0 - 0.55 * 0.5)),
        "spatial_sigma": 1.0,
        "range_sigma": max(0.1, 0.15 * (1.0 - 0.55 * 0.5)),
        "luminance_only": 0.0, "chroma_only": 0.0,
    }
    sharpen = {
        "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 0.6, "sigma_x": 0.5, "sigma_y": 0.5,
        "threshold": 0.0, "channels": 1.0,
    }
    expected = _cpu_denoise(source, denoise_params)
    expected = _cpu_sharpen(expected, {
        "amount": 0.6, "radius": 1.0,
        "threshold": 0.0, "channels": "rgb",
    })
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, (denoise, sharpen))], 41, 27)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 0.5
        assert int(difference.max()) <= 2
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(("edge_policy", "edge_index"), (
    ("clamp", 0.0), ("wrap", 1.0), ("mirror", 2.0),
))
@pytest.mark.parametrize(("blur_type", "kind"), (
    ("box", 1.0), ("gaussian", 2.0),
))
def test_real_metal_blur_edge_policies_match_cpu_at_frame_boundaries(
    edge_policy, edge_index, blur_type, kind,
):
    from src.features.edit.core.timeline_export import _cpu_blur

    source = np.zeros((11, 15, 3), dtype=np.uint8)
    source[0, 0] = (255, 32, 4)
    source[0, -1] = (3, 180, 241)
    source[-1, 0] = (29, 220, 12)
    source[-1, -1] = (170, 5, 255)
    source[0, 4:11] = (80, 230, 40)
    source[5:9, 0] = (240, 50, 100)
    effect = {
        "kind": kind, "half_x": 8.0, "half_y": 7.0,
        "strength": 1.0, "sigma_x": 2.9, "sigma_y": 2.6,
        "edge_policy": edge_index,
    }
    cpu = _cpu_blur(source, {
        "blur_type": blur_type, "radius_x": 8.0, "radius_y": 7.0,
        "edge_policy": edge_policy, "mix": 1.0,
    })
    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, effect)], 15, 11)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - cpu.astype(np.int16))
        assert float(difference.mean()) <= 0.75
        assert int(difference.max()) <= 3
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(("edge_policy", "edge_index"), (
    ("clamp", 0.0), ("wrap", 1.0), ("mirror", 2.0),
))
def test_real_metal_packed_effect_chain_uses_precomputed_blur_edges(
    edge_policy, edge_index,
):
    from src.features.edit.core.timeline_export import (
        _cpu_blur, _cpu_sharpen,
    )

    rng = np.random.default_rng(257)
    source = rng.integers(0, 256, (17, 23, 3), dtype=np.uint8)
    blur = {
        "kind": 2.0, "half_x": 2.0, "half_y": 2.0,
        "strength": 0.8, "sigma_x": 1.1, "sigma_y": 1.1,
        "edge_policy": edge_index,
    }
    sharpen = {
        "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 0.35, "sigma_x": 0.5, "sigma_y": 0.5,
        "threshold": 0.0, "channels": 1.0,
    }
    expected = _cpu_blur(source, {
        "blur_type": "gaussian", "radius_x": 2.0, "radius_y": 2.0,
        "mix": 0.8, "edge_policy": edge_policy,
    })
    expected = _cpu_sharpen(expected, {
        "amount": 0.35, "radius": 1.0, "threshold": 0.0,
        "channels": "rgb",
    })

    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0, (blur, sharpen))], 23, 17)
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        assert float(difference.mean()) <= 1.25
        worst = np.unravel_index(int(np.argmax(difference)), difference.shape)
        assert int(difference.max()) <= 5, (
            f"max error {int(difference.max())} at {worst}: "
            f"Metal={actual[worst[0], worst[1]].tolist()}, "
            f"CPU={expected[worst[0], worst[1]].tolist()}"
        )
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_effect_chain_matches_ordered_cpu_export_without_readback():
    from src.features.edit.core.timeline_export import (
        _cpu_blur,
        _cpu_glow,
        _cpu_sharpen,
    )

    rng = np.random.default_rng(163)
    source = rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)
    blur_params = {
        "kind": 2.0, "half_x": 2.0, "half_y": 2.0,
        "strength": 0.75, "sigma_x": 1.1, "sigma_y": 1.1,
    }
    sharpen_params = {
        "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 0.8, "sigma_x": 0.5, "sigma_y": 0.5,
        "threshold": 0.01, "channels": 1.0,
    }
    glow_params = {
        "kind": 4.0, "half_x": 32.0, "half_y": 32.0,
        "strength": 0.65, "sigma_x": 1.5, "sigma_y": 1.5,
        "threshold": 0.55, "intensity": 0.6,
        "tint_r": 0.9, "tint_g": 0.7, "tint_b": 0.8,
        "quality": "high",
    }
    cpu = _cpu_blur(source, {
        "blur_type": "gaussian", "radius_x": 2.0,
        "radius_y": 2.0, "mix": 0.75,
    })
    cpu = _cpu_sharpen(cpu, {
        "amount": 0.8, "radius": 1.0,
        "threshold": 0.01, "channels": "rgb",
    })
    cpu = _cpu_glow(cpu, {
        "threshold": 0.55, "radius": 3.0,
        "intensity": 0.6, "mix": 0.65,
        "tint_r": 0.9, "tint_g": 0.7, "tint_b": 0.8,
        "quality": "high",
    })

    renderer = TimelineMetalExportRenderer()
    try:
        actual = renderer.render_layer_stack(
            [(source, None, 1.0, 0,
              (blur_params, sharpen_params, glow_params))],
            64,
            48,
        )
        assert actual is not None, renderer.last_fallback_reason
        difference = np.abs(actual.astype(np.int16) - cpu.astype(np.int16))
        assert float(difference.mean()) <= 1.25
        assert int(difference.max()) <= 5
        assert renderer.frames_rendered == 1
        assert "stack_effect_chain_a" in renderer._buffers
        assert "stack_effect_chain_b" in renderer._buffers
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
def test_real_metal_even_effect_chain_reuses_scratch_across_layers_and_frames():
    from src.features.edit.core.timeline_export import _cpu_blur, _cpu_sharpen

    rng = np.random.default_rng(211)
    sources = [
        rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)
        for _ in range(4)
    ]
    blur_uniforms = {
        "kind": 2.0, "half_x": 3.0, "half_y": 3.0,
        "strength": 1.0, "sigma_x": 1.4, "sigma_y": 1.4,
    }
    sharpen_uniforms = {
        "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
        "strength": 0.7, "sigma_x": 0.5, "sigma_y": 0.5,
    }
    chain = (blur_uniforms, sharpen_uniforms)

    def cpu_chain(source: np.ndarray) -> np.ndarray:
        blurred = _cpu_blur(source, {
            "blur_type": "gaussian", "radius_x": 3.0,
            "radius_y": 3.0, "mix": 1.0,
        })
        return _cpu_sharpen(blurred, {
            "amount": 0.7, "radius": 1.0,
        })

    renderer = TimelineMetalExportRenderer()
    try:
        first = renderer.render_layer_stack(
            [(sources[0], None, 1.0, 0, chain),
             (sources[1], None, 0.4, 0, chain)],
            64,
            48,
        )
        assert first is not None, renderer.last_fallback_reason
        expected_first = np.rint(
            cpu_chain(sources[0]).astype(np.float32) * 0.6
            + cpu_chain(sources[1]).astype(np.float32) * 0.4
        ).astype(np.uint8)
        assert int(np.abs(first.astype(np.int16)
                          - expected_first.astype(np.int16)).max()) <= 3

        scratch_ids = {
            name: id(renderer._buffers[name])
            for name in (
                "stack_effect_chain_a",
                "stack_effect_chain_b",
                "stack_effect_chain_horizontal",
            )
        }
        second = renderer.render_layer_stack(
            [(sources[2], None, 1.0, 0, chain),
             (sources[3], None, 0.4, 0, chain)],
            64,
            48,
        )
        assert second is not None, renderer.last_fallback_reason
        expected_second = np.rint(
            cpu_chain(sources[2]).astype(np.float32) * 0.6
            + cpu_chain(sources[3]).astype(np.float32) * 0.4
        ).astype(np.uint8)
        assert int(np.abs(second.astype(np.int16)
                          - expected_second.astype(np.int16)).max()) <= 3
        assert scratch_ids == {
            name: id(renderer._buffers[name]) for name in scratch_ids
        }
        assert not any("chain_a" in name and name.startswith("stack_layer_")
                       for name in renderer._buffers)
        assert renderer.frames_rendered == 2
    finally:
        renderer.release()


@pytest.mark.skipif(
    not TimelineMetalExportRenderer.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("video_name", "pixel_format"),
    (("2K_h264.mp4", "nv12"), ("2K_H265.mov", "p010")),
)
def test_videotoolbox_yuv_textures_render_without_cpu_frame_upload(
    video_name: str, pixel_format: str,
):
    """Exercise real VT→CVMetalTexture→timeline MSL for NV12 and P010."""
    import cv2
    from pathlib import Path
    import shutil
    import subprocess

    from src.shared.media.gpu_frame import GpuDecodedFrame, safe_release_gpu_frame
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuv420Output,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    video_path = Path(__file__).resolve().parents[1] / "test_videos" / video_name
    if not video_path.is_file():
        pytest.skip("real H.264 test material is unavailable")
    provider = VideoFrameProviderService()
    clip_id = provider.open_clip(str(video_path), share_by_path=False)
    gpu_frame = None
    renderer = TimelineMetalExportRenderer()
    try:
        gpu_frame = provider.request_metal_frame(clip_id, 0, quality="master")
        if not isinstance(gpu_frame, GpuDecodedFrame):
            pytest.skip("VideoToolbox/CoreVideo Metal texture import is unavailable")
        assert gpu_frame.pixel_format == pixel_format
        rendered = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0)], 1280, 720)
        assert rendered is not None, renderer.last_fallback_reason
        fused_yuv = renderer.render_metal_texture_frame(
            gpu_frame, None, 1280, 720, 1.0, output_yuv420p=True)
        stacked_yuv = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0)], 1280, 720,
            output_yuv420p=True)
        assert isinstance(fused_yuv, GpuYuv420Output)
        assert isinstance(stacked_yuv, GpuYuv420Output)
        for fused_plane, stacked_plane in zip(
                fused_yuv.planes, stacked_yuv.planes):
            plane_difference = np.abs(
                fused_plane.astype(np.int16)
                - stacked_plane.astype(np.int16))
            assert int(plane_difference.max()) <= 1

        # A wide Gaussian over VT YUV textures is materialized once on Metal
        # before its separable taps; this guards the optimized no-readback path
        # for both NV12 and P010 sources.
        wide_blur = {
            "kind": 2.0, "half_x": 8.0, "half_y": 8.0,
            "strength": 1.0, "sigma_x": 2.9, "sigma_y": 2.9,
        }
        blurred = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0, wide_blur)], 1280, 720)
        assert blurred is not None, renderer.last_fallback_reason
        assert blurred.shape == (720, 1280, 3)
        assert renderer.last_fallback_reason is None
        assert "stack_effect_chain_a" in renderer._buffers
        assert "stack_effect_chain_b" in renderer._buffers
        wide_glow = {
            "kind": 4.0, "half_x": 8.0, "half_y": 8.0,
            "strength": 0.5, "sigma_x": 2.9, "sigma_y": 2.9,
            "threshold": 0.7, "intensity": 0.5,
            "tint_r": 1.0, "tint_g": 0.85, "tint_b": 0.7,
        }
        glowed = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0, wide_glow)], 1280, 720)
        assert glowed is not None, renderer.last_fallback_reason
        assert glowed.shape == (720, 1280, 3)
        assert renderer.last_fallback_reason is None
        sharpen = {
            "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
            "strength": 1.0, "sigma_x": 0.5, "sigma_y": 0.5,
            "threshold": 0.0, "channels": 0.0,
        }
        sharpened = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0, sharpen)], 1280, 720)
        assert sharpened is not None, renderer.last_fallback_reason
        assert sharpened.shape == (720, 1280, 3)
        assert renderer.last_fallback_reason is None

        if pixel_format == "nv12":
            cpu_frame = provider.request_frame(clip_id, 0, quality="master")
            if cpu_frame is None or getattr(cpu_frame, "data", None) is None:
                pytest.skip("CPU reference frame could not be decoded")
            reference_bgr = np.ascontiguousarray(cpu_frame.data[..., ::-1])
        else:
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg is None:
                pytest.skip("ffmpeg is required to build the P010 reference")
            width, height = gpu_frame.width, gpu_frame.height
            decoded = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i",
                 str(video_path), "-frames:v", "1", "-pix_fmt",
                 "yuv420p10le", "-f", "rawvideo", "pipe:1"],
                check=True, capture_output=True,
            ).stdout
            luma_size = width * height
            chroma_size = luma_size // 4
            y_plane = np.frombuffer(
                decoded[:luma_size * 2], dtype="<u2").reshape(height, width)
            u_plane = np.frombuffer(
                decoded[luma_size * 2:(luma_size + chroma_size) * 2],
                dtype="<u2").reshape(height // 2, width // 2)
            v_plane = np.frombuffer(
                decoded[(luma_size + chroma_size) * 2:], dtype="<u2"
            ).reshape(height // 2, width // 2)
            u_full = cv2.resize(
                u_plane.astype(np.float32), (width, height),
                interpolation=cv2.INTER_LINEAR)
            v_full = cv2.resize(
                v_plane.astype(np.float32), (width, height),
                interpolation=cv2.INTER_LINEAR)
            y = (y_plane.astype(np.float32) - 64.0) / 876.0
            cb = (u_full - 512.0) / 896.0
            cr = (v_full - 512.0) / 896.0
            kr, kb = 0.2126, 0.0722
            kg = 1.0 - kr - kb
            red = y + 2.0 * (1.0 - kr) * cr
            blue = y + 2.0 * (1.0 - kb) * cb
            green = (y - 2.0 * kb * (1.0 - kb) / kg * cb
                     - 2.0 * kr * (1.0 - kr) / kg * cr)
            reference_bgr = np.rint(np.stack(
                (blue, green, red), axis=-1).clip(0.0, 1.0) * 255.0
            ).astype(np.uint8)
        source_reference_bgr = reference_bgr
        reference_bgr = cv2.resize(
            reference_bgr, (1280, 720), interpolation=cv2.INTER_LINEAR)
        difference = np.abs(rendered.astype(np.int16) - reference_bgr.astype(np.int16))
        assert float(difference.mean()) < 2.0
        assert float(np.percentile(difference, 99)) <= 8.0

        from src.features.edit.core.timeline_export import (
            _cpu_blur, _cpu_glow, _cpu_sharpen,
        )
        for radius in (5.0, 16.0):
            sigma = 0.3 * (radius - 1.0) + 0.8
            effect_params = {
                "kind": 2.0, "half_x": radius, "half_y": radius,
                "strength": 1.0, "sigma_x": sigma, "sigma_y": sigma,
            }
            effected = renderer.render_layer_stack(
                [(gpu_frame, None, 1.0, 0, effect_params)], 1280, 720)
            assert effected is not None, renderer.last_fallback_reason
            cpu_effected = _cpu_blur(source_reference_bgr, {
                "blur_type": "gaussian", "radius_x": radius,
                "radius_y": radius, "mix": 1.0,
            })
            cpu_effected = cv2.resize(
                cpu_effected, (1280, 720), interpolation=cv2.INTER_LINEAR)
            effect_difference = np.abs(
                effected.astype(np.int16) - cpu_effected.astype(np.int16))
            assert float(effect_difference.mean()) < 2.0
            assert float(np.percentile(effect_difference, 99)) <= 8.0

        glow_params = {
            "kind": 4.0, "half_x": 16.0, "half_y": 16.0,
            "strength": 1.0, "sigma_x": 8.0, "sigma_y": 8.0,
            "threshold": 0.7, "intensity": 0.5,
        }
        glow_output = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0, glow_params)], 1280, 720)
        assert glow_output is not None, renderer.last_fallback_reason
        glow_source = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0)], gpu_frame.width, gpu_frame.height)
        assert glow_source is not None
        from src.features.edit.core.timeline_export import _cpu_erode_dilate
        for operation, size, iterations, edge_mode in (
            ("erode", 3, 1, "extend"),
            ("dilate", 4, 2, "extend"),
            ("erode", 5, 1, "reflect"),
            ("dilate", 5, 1, "wrap"),
        ):
            morphology_params = {
                "operation": operation, "size": size,
                "iterations": iterations, "channels": "all",
                "edge_mode": edge_mode,
            }
            half_size = (size // 2) * iterations
            morphology_effect = {
                "kind": 5.0, "half_x": float(half_size),
                "half_y": float(half_size), "strength": 1.0,
                "sigma_x": 1.0, "sigma_y": 1.0,
                "operation": float(operation == "dilate"),
                "edge_policy": float(("extend", "wrap", "reflect").index(edge_mode)),
            }
            metal_morphology_native = renderer.render_layer_stack(
                [(gpu_frame, None, 1.0, 0, morphology_effect)],
                gpu_frame.width, gpu_frame.height)
            assert metal_morphology_native is not None, renderer.last_fallback_reason
            cpu_morphology_native = _cpu_erode_dilate(
                glow_source, morphology_params)
            native_difference = np.abs(
                metal_morphology_native.astype(np.int16)
                - cpu_morphology_native.astype(np.int16))
            assert float(native_difference.mean()) < 0.5, (
                f"native {pixel_format} {operation} size={size} "
                f"iterations={iterations}: "
                f"mean={float(native_difference.mean()):.3f}, "
                f"p99={float(np.percentile(native_difference, 99)):.3f}"
            )
            metal_morphology = renderer.render_layer_stack(
                [(gpu_frame, None, 1.0, 0, morphology_effect)], 1280, 720)
            assert metal_morphology is not None, renderer.last_fallback_reason
            cpu_morphology = cv2.resize(
                _cpu_erode_dilate(glow_source, morphology_params),
                (1280, 720), interpolation=cv2.INTER_LINEAR)
            morphology_difference = np.abs(
                metal_morphology.astype(np.int16)
                - cpu_morphology.astype(np.int16))
            assert float(morphology_difference.mean()) < 0.5, (
                f"{pixel_format} {operation} size={size} iterations={iterations}: "
                f"mean={float(morphology_difference.mean()):.3f}, "
                f"p99={float(np.percentile(morphology_difference, 99)):.3f}"
            )
            assert int(morphology_difference.max()) <= 2

        from src.features.edit.core.timeline_export import _cpu_edge_detect
        edge_params = {
            "method": "sobel", "threshold": 0.13,
            "threshold_high": 0.3, "direction": "both",
            "output_mode": "edges", "invert": False,
            "output_strength": 1.0, "kernel_size": 3,
            "preblur": 0.0, "channel": "luminance",
        }
        edge_effect = {
            "kind": 6.0, "half_x": 1.0, "half_y": 1.0,
            "strength": 1.0, "sigma_x": 0.13, "sigma_y": 0.3,
            "direction": 0.0, "output_mode": 0.0, "invert": 0.0,
        }
        metal_edge = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0, edge_effect)],
            gpu_frame.width, gpu_frame.height)
        assert metal_edge is not None, renderer.last_fallback_reason
        cpu_edge = _cpu_edge_detect(glow_source, edge_params)
        edge_difference = np.abs(metal_edge.astype(np.int16) - cpu_edge.astype(np.int16))
        assert float(edge_difference.mean()) <= 0.1, (
            f"native {pixel_format} edge detect mean difference "
            f"{float(edge_difference.mean()):.3f}"
        )
        assert int(edge_difference.max()) <= 1, (
            f"native {pixel_format} edge detect max difference "
            f"{int(edge_difference.max())}, mismatched channels="
            f"{int(np.count_nonzero(edge_difference))}, "
            f"samples={np.argwhere(edge_difference > 1)[:8].tolist()}"
        )

        from src.features.edit.core.timeline_export import _cpu_denoise
        denoise_params = {
            "strength": 0.5, "radius": 3,
            "luminance_only": False, "chroma_only": False,
        }
        denoise_effect = {
            "kind": 7.0, "half_x": 3.0, "half_y": 3.0,
            "strength": 0.5, "sigma_x": 1.2, "sigma_y": 0.1125,
            "spatial_sigma": 1.2, "range_sigma": 0.1125,
            "luminance_only": 0.0, "chroma_only": 0.0,
        }
        metal_denoise = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0, denoise_effect)],
            gpu_frame.width, gpu_frame.height)
        assert metal_denoise is not None, renderer.last_fallback_reason
        cpu_denoise = _cpu_denoise(glow_source, denoise_params)
        denoise_difference = np.abs(
            metal_denoise.astype(np.int16) - cpu_denoise.astype(np.int16))
        assert float(denoise_difference.mean()) <= 0.5, (
            f"native {pixel_format} denoise mean difference "
            f"{float(denoise_difference.mean()):.3f}"
        )
        assert int(denoise_difference.max()) <= 2

        from src.features.edit.core.timeline_export import _cpu_blur
        bilateral_params = {
            "blur_type": "bilateral", "radius": 5.0,
            "edge_preserve": 0.4, "mix": 0.75,
        }
        bilateral_effect = {
            "kind": 9.0, "half_x": 5.0, "half_y": 5.0,
            "strength": 0.75, "sigma_x": 5.0,
            "sigma_y": 48.0 / 255.0,
            "spatial_sigma": 5.0, "range_sigma": 48.0 / 255.0,
            "channel_r": 1.0, "channel_g": 1.0, "channel_b": 1.0,
        }
        metal_bilateral = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0, bilateral_effect)],
            gpu_frame.width, gpu_frame.height)
        assert metal_bilateral is not None, renderer.last_fallback_reason
        cpu_bilateral = _cpu_blur(glow_source, bilateral_params)
        bilateral_difference = np.abs(
            metal_bilateral.astype(np.int16)
            - cpu_bilateral.astype(np.int16))
        assert float(bilateral_difference.mean()) <= 0.5, (
            f"native {pixel_format} bilateral mean difference "
            f"{float(bilateral_difference.mean()):.3f}"
        )
        assert int(bilateral_difference.max()) <= 2

        from src.features.edit.core.timeline_export import _cpu_defocus
        defocus_params = {
            "mode": "separable", "focal_x": 0.46, "focal_y": 0.52,
            "focal_size": 0.2, "max_radius": 18.0,
            "edge_policy": "mirror", "process_alpha": True,
        }
        defocus_effect = {
            "kind": 8.0, "half_x": 15.0, "half_y": 0.0,
            "strength": 1.0, "sigma_x": 0.46, "sigma_y": 0.52,
            "focal_x": 0.46, "focal_y": 0.52,
            "focal_size": 0.2, "max_radius": 18.0,
            "edge_policy": 2.0, "mode": 0.0, "process_alpha": 1.0,
        }
        metal_defocus = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0, defocus_effect)], 1280, 720)
        assert metal_defocus is not None, renderer.last_fallback_reason
        cpu_defocus = _cpu_defocus(glow_source, defocus_params)
        cpu_defocus = cv2.resize(
            cpu_defocus, (1280, 720), interpolation=cv2.INTER_LINEAR)
        defocus_difference = np.abs(
            metal_defocus.astype(np.int16) - cpu_defocus.astype(np.int16))
        assert float(defocus_difference.mean()) <= 1.0, (
            f"native {pixel_format} defocus mean difference "
            f"{float(defocus_difference.mean()):.3f}"
        )
        assert int(np.percentile(defocus_difference, 99)) <= 4

        mipmap_params = {**defocus_params, "mode": "mipmap"}
        mipmap_effect = {**defocus_effect, "mode": 1.0}
        metal_defocus_mipmap = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0, mipmap_effect)], 1280, 720)
        assert metal_defocus_mipmap is not None, renderer.last_fallback_reason
        cpu_defocus_mipmap = _cpu_defocus(glow_source, mipmap_params)
        cpu_defocus_mipmap = cv2.resize(
            cpu_defocus_mipmap, (1280, 720), interpolation=cv2.INTER_LINEAR)
        mipmap_difference = np.abs(
            metal_defocus_mipmap.astype(np.int16)
            - cpu_defocus_mipmap.astype(np.int16))
        assert float(mipmap_difference.mean()) <= 0.5, (
            f"native {pixel_format} defocus mipmap mean difference "
            f"{float(mipmap_difference.mean()):.3f}"
        )
        assert int(np.percentile(mipmap_difference, 99)) <= 3

        morphology_chain_blur = {
            "kind": 2.0, "half_x": 3.0, "half_y": 3.0,
            "strength": 0.8, "sigma_x": 1.4, "sigma_y": 1.4,
        }
        chain_morphology = {
            "kind": 5.0, "half_x": 2.0, "half_y": 2.0,
            "strength": 1.0, "sigma_x": 1.0, "sigma_y": 1.0,
            "operation": 0.0,
        }
        morphology_chain_sharpen = {
            "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
            "strength": 0.8, "sigma_x": 0.5, "sigma_y": 0.5,
            "threshold": 0.01, "channels": 1.0,
        }
        chain_morphology_output = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0,
              (morphology_chain_blur, chain_morphology,
               morphology_chain_sharpen))],
            1280, 720,
        )
        assert chain_morphology_output is not None, renderer.last_fallback_reason
        morphology_reference = _cpu_blur(glow_source, {
            "blur_type": "gaussian", "radius_x": 3.0,
            "radius_y": 3.0, "mix": 0.8,
        })
        morphology_reference = _cpu_erode_dilate(morphology_reference, {
            "operation": "erode", "size": 5, "iterations": 1,
            "channels": "all", "edge_mode": "extend",
        })
        morphology_reference = _cpu_sharpen(morphology_reference, {
            "amount": 0.8, "radius": 1.0,
            "threshold": 0.01, "channels": "rgb",
        })
        morphology_reference = cv2.resize(
            morphology_reference, (1280, 720), interpolation=cv2.INTER_LINEAR)
        morphology_chain_difference = np.abs(
            chain_morphology_output.astype(np.int16)
            - morphology_reference.astype(np.int16))
        assert float(morphology_chain_difference.mean()) < 0.75
        assert int(morphology_chain_difference.max()) <= 3
        cpu_glow = cv2.resize(
            _cpu_glow(glow_source, {
                "threshold": 0.7, "radius": 16.0,
                "intensity": 0.5, "mix": 1.0,
            }),
            (1280, 720), interpolation=cv2.INTER_LINEAR)
        glow_difference = np.abs(glow_output.astype(np.int16) - cpu_glow.astype(np.int16))
        assert float(glow_difference.mean()) < 2.0
        assert float(np.percentile(glow_difference, 99)) <= 8.0

        chain_blur = {
            "kind": 2.0, "half_x": 3.0, "half_y": 3.0,
            "strength": 0.8, "sigma_x": 1.4, "sigma_y": 1.4,
        }
        chain_sharpen = {
            "kind": 3.0, "half_x": 1.0, "half_y": 1.0,
            "strength": 0.8, "sigma_x": 0.5, "sigma_y": 0.5,
            "threshold": 0.01, "channels": 1.0,
        }
        chain_glow = {
            "kind": 4.0, "half_x": 8.0, "half_y": 8.0,
            "strength": 0.7, "sigma_x": 1.5, "sigma_y": 1.5,
            "threshold": 0.7, "intensity": 0.4,
        }
        chain_output = renderer.render_layer_stack(
            [(gpu_frame, None, 1.0, 0,
              (chain_blur, chain_sharpen, chain_glow))],
            1280,
            720,
        )
        assert chain_output is not None, renderer.last_fallback_reason
        chain_reference = _cpu_blur(glow_source, {
            "blur_type": "gaussian", "radius_x": 3.0,
            "radius_y": 3.0, "mix": 0.8,
        })
        chain_reference = _cpu_sharpen(chain_reference, {
            "amount": 0.8, "radius": 1.0,
            "threshold": 0.01, "channels": "rgb",
        })
        chain_reference = cv2.resize(
            _cpu_glow(chain_reference, {
                "threshold": 0.7, "radius": 3.0,
                "intensity": 0.4, "mix": 0.7,
                "quality": "fast",
            }),
            (1280, 720), interpolation=cv2.INTER_LINEAR)
        chain_difference = np.abs(
            chain_output.astype(np.int16) - chain_reference.astype(np.int16))
        assert float(chain_difference.mean()) < 2.0
        assert float(np.percentile(chain_difference, 99)) <= 9.0

        for radius in (1.0, 16.0):
            sharpen_params = {
                "kind": 3.0, "half_x": radius, "half_y": radius,
                "strength": 1.0,
                "sigma_x": radius * 0.5, "sigma_y": radius * 0.5,
            }
            sharpened = renderer.render_layer_stack(
                [(gpu_frame, None, 1.0, 0, sharpen_params)], 1280, 720)
            assert sharpened is not None, renderer.last_fallback_reason
            sharpen_source = source_reference_bgr
            if radius > 1.0:
                # Compare the large-radius filter on identical source RGB
                # samples so decoder YUV conversion rounding is not amplified.
                sharpen_source = renderer.render_layer_stack(
                    [(gpu_frame, None, 1.0, 0)],
                    gpu_frame.width, gpu_frame.height,
                )
                assert sharpen_source is not None
            cpu_sharpened = cv2.resize(
                _cpu_sharpen(sharpen_source, {
                    "amount": 1.0, "radius": radius,
                }),
                (1280, 720), interpolation=cv2.INTER_LINEAR)
            sharpen_difference = np.abs(
                sharpened.astype(np.int16) - cpu_sharpened.astype(np.int16))
            assert float(sharpen_difference.mean()) < 2.0
            assert float(np.percentile(sharpen_difference, 99)) <= 8.0
        assert gpu_frame.decode_path == "macos_vt_cvmetal_zero_copy"
    finally:
        renderer.release()
        if isinstance(gpu_frame, GpuDecodedFrame):
            safe_release_gpu_frame(gpu_frame)
        provider.close_clip(clip_id)
