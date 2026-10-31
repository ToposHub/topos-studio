"""Whole primary-grade paths: HDR intermediates, saturation and GPU preview."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.color.core.the_decompiler import GradingMath
from src.features.edit.core.project import ColorStackData
from src.features.edit.core.timeline_export import TimelineExporter
from src.features.edit.core.timeline_preview_renderer import AssetResolver, PreviewRenderWorker


def _exporter(params: dict) -> tuple[TimelineExporter, SimpleNamespace]:
    stack = ColorStackData(stack_id="grade", grading_params=params)
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = SimpleNamespace(color_stacks={"grade": stack})
    exporter._color_grade_lut_cache = {}
    layer = SimpleNamespace(clip=SimpleNamespace(color_stack_id="grade"),
                            clip_id="clip", source_frame=0)
    return exporter, layer


def _reference(frame: np.ndarray, params: dict) -> np.ndarray:
    integer_input = np.issubdtype(frame.dtype, np.integer)
    scale = float(np.iinfo(frame.dtype).max) if integer_input else 1.0
    rgb = frame[..., :3][..., ::-1].astype(np.float32) / scale
    graded = GradingMath.apply_grading(rgb.reshape(-1, 3), params).reshape(rgb.shape)
    result = frame.copy()
    result[..., :3] = (np.rint(np.clip(graded[..., ::-1], 0, 1) * scale)
                       if integer_input else graded[..., ::-1])
    return result


@pytest.mark.parametrize("params", [
    {"exposure": 60.0, "saturation": -70.0},
    {"contrast": 50.0, "saturation": -100.0},
    {"gain": (30.0, 0.6, 0.5), "saturation": -50.0},
    {"temp": 30.0, "saturation": 50.0},
    {"saturation": 100.0},
])
def test_saturation_consumes_unclipped_prefix_on_consecutive_frames(params: dict) -> None:
    exporter, layer = _exporter(params)
    rng = np.random.default_rng(415)
    for _ in range(3):
        frame = rng.integers(0, 256, (32, 64, 3), dtype=np.uint8)
        actual = exporter._apply_color_grade(frame, layer)
        error = np.abs(actual.astype(np.int32) - _reference(frame, params).astype(np.int32))
        assert actual.dtype == np.uint8
        assert error.max() <= 1  # final float32 matrix rounding only


@pytest.mark.parametrize("dtype", [np.float32, np.uint16])
def test_cpu_preview_and_export_preserve_working_depth_and_alpha(dtype: type) -> None:
    params = {"exposure": 25.0, "hue_shift": 37.0, "saturation": -30.0}
    values = np.array([[[0.4, 0.2, 0.8, 0.37], [1.8, -0.3, 0.6, 0.73]]], np.float32)
    frame = values if dtype == np.float32 else np.rint(np.clip(values, 0, 1) * 65535).astype(dtype)
    exporter, layer = _exporter(params)
    stack = exporter._project.color_stacks["grade"]
    worker = PreviewRenderWorker(
        frame_provider=MagicMock(),
        asset_resolver=AssetResolver(SimpleNamespace(color_stacks={"grade": stack})),
        video_renderer=None)
    try:
        expected = _reference(frame, params)
        for actual in (exporter._apply_color_grade(frame, layer),
                       worker._apply_preview_color_grade(frame, layer)):
            assert actual.dtype == frame.dtype
            np.testing.assert_array_equal(actual[..., 3], frame[..., 3])
            np.testing.assert_allclose(actual, expected, atol=1e-6, rtol=0)
    finally:
        worker._decode_executor.shutdown(wait=True)


def _render_primary_in_worker(backend: str, frame: np.ndarray, params: dict) -> np.ndarray | None:
    from src.features.edit.core.timeline_gpu_export_renderer import (
        TimelineGpuExportRenderer, grade_params_to_uniforms)
    if backend == "metal":
        from src.features.edit.core.timeline_metal_export_renderer import TimelineMetalExportRenderer
        renderer = TimelineMetalExportRenderer()
    elif backend == "preview":
        import moderngl
        from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
        ctx = moderngl.create_standalone_context()
        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer.ctx = ctx
        try:
            assert renderer._init_shaders()
            renderer._init_quad()
            renderer._graded_track_fbos = []
            renderer._graded_track_texes = []
            rgba = np.concatenate((frame[..., ::-1], np.full((*frame.shape[:2], 1), 255, np.uint8)), axis=-1)
            source = ctx.texture((frame.shape[1], frame.shape[0]), 4, rgba.tobytes())
            result = renderer._apply_preview_grade(
                source, 0, frame.shape[1], frame.shape[0], {"grading_params": params})
            return np.frombuffer(result.read(), np.uint8).reshape(rgba.shape)[..., 2::-1].copy()
        finally:
            ctx.release()
    else:
        renderer = TimelineGpuExportRenderer()
    try:
        return renderer.render_packed_bgr(
            frame, grade_params_to_uniforms(params), frame.shape[1], frame.shape[0], 1.0)
    finally:
        renderer.release()


@pytest.mark.parametrize("backend", ["preview", "opengl", "metal"])
@pytest.mark.parametrize("params", [
    {"exposure": 60.0, "hue_shift": 47.0, "saturation": -70.0},
    {"lift": (271.0, 0.8, -0.9), "gamma": (72.0, 0.9, -0.7),
     "gain": (276.0, 0.9, 0.5), "hue_shift": 31.0, "saturation": -20.0},
])
def test_real_gpu_preview_and_delivery_match_cpu_authority(backend: str, params: dict) -> None:
    frame = np.random.default_rng(91).integers(0, 256, (16, 24, 3), dtype=np.uint8)
    # GPU resources are created, consumed and released on the same worker.
    with ThreadPoolExecutor(max_workers=1) as pool:
        actual = pool.submit(_render_primary_in_worker, backend, frame, params).result()
    if actual is None:
        pytest.skip(f"{backend} backend unavailable")
    expected = _reference(frame, params)
    error = np.abs(actual.astype(np.int32) - expected.astype(np.int32))
    assert error.max() <= 1
