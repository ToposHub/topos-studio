"""The worker's compatibility output and cancellation use the native graph."""

from __future__ import annotations

import numpy as np
import moderngl
import pytest

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_cpu import run_plan_cpu
from src.features.color.core.color_graph_metal import MetalGraphCancelled
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_param_source import graph_node_layer_provider
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.metal_color_preview import ColorMetalPreview, ColorMetalSurface
from src.features.color.core.metal_scope_engine import SCOPE_RANGE_MAX, SCOPE_RANGE_MIN
from src.features.color.core.scope_types import (
    DEFAULT_HISTOGRAM_DISPLAY_SIZE,
    DEFAULT_WAVEFORM_DENSITY_SIZE,
    ScopeDemand,
)
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.present_port import SurfacePublisher
from tests.color.fixtures.color_graph import _neutral_layer
from tests.color.test_color_graph_migration import _stack_with_layers


@pytest.mark.skipif(not MetalComputeContext.is_supported(),
                    reason="requires a real Metal device")
def test_worker_grades_fp16_texture_with_gl_row_order() -> None:
    gl = moderngl.create_standalone_context(require=330)
    layer = _neutral_layer("Exposure")
    layer.grading_params["exposure"] = 25.0
    graph = migrate_legacy_stack(_stack_with_layers([layer]))
    snapshot = freeze_graph(graph, session_id="color-preview-test")
    rgba = np.array([[[0.1, 0.2, 0.3, 1.0], [0.2, 0.6, 0.4, 1.0]],
                     [[0.6, 0.3, 0.2, 1.0], [0.05, 0.2, 0.1, 1.0]]], np.float16)
    size = (rgba.shape[1], rgba.shape[0])
    source = gl.texture(size, 4, rgba[::-1].tobytes(), dtype="f2")
    output = gl.texture(size, 4, dtype="f2")
    source_fbo = gl.framebuffer([source])
    output_fbo = gl.framebuffer([output])
    preview = ColorMetalPreview(None, ColorMetalSurface(None, SurfacePublisher()))
    try:
        assert preview.grade(snapshot, 0, source_fbo, output_fbo,
                             cancelled=lambda: False, display=None)
        provider = graph_node_layer_provider(snapshot.graph, frame=0)
        expected = run_plan_cpu(
            compile_graph(snapshot, size=source.size), rgba.astype(np.float32),
            lambda render_pass: provider(render_pass.node_id)).output
        actual = np.frombuffer(output.read(), dtype=np.float16).reshape(rgba.shape)[::-1]
        np.testing.assert_allclose(actual, expected, atol=1.0 / 255.0)

        original_bytes = output.read()
        with pytest.raises(MetalGraphCancelled):
            preview.grade(snapshot, 0, source_fbo, output_fbo,
                          cancelled=lambda: True, display=None)
        assert output.read() == original_bytes
    finally:
        preview.release()
        output_fbo.release()
        source_fbo.release()
        output.release()
        source.release()
        gl.release()


@pytest.mark.skipif(not MetalComputeContext.is_supported(),
                    reason="requires a real Metal device")
@pytest.mark.parametrize("mode", ["waveform", "histogram"])
def test_metal_scope_reaches_gl_panel_with_display_orientation(mode: str) -> None:
    gl = moderngl.create_standalone_context(require=330)
    source_pixels = np.ones((32, 64, 4), dtype=np.float16)
    source_pixels[:, :, :3] = 0.785
    source = gl.texture((64, 32), 4, source_pixels.tobytes(), dtype="f2")
    size = DEFAULT_WAVEFORM_DENSITY_SIZE if mode == "waveform" else DEFAULT_HISTOGRAM_DISPLAY_SIZE
    output = gl.texture(size, 4, dtype="f1")
    source_fbo = gl.framebuffer([source])
    output_fbo = gl.framebuffer([output])
    preview = ColorMetalPreview(None, ColorMetalSurface(None, SurfacePublisher()))
    demand = ScopeDemand(1, 0, mode, size, analysis_size=(64, 32))
    try:
        assert preview.render_scope(demand, source_fbo, output_fbo)
        pixels = np.frombuffer(output.read(), dtype=np.uint8).reshape(size[1], size[0], 4)
        if mode == "waveform":
            expected = int((0.785 - SCOPE_RANGE_MIN) / (SCOPE_RANGE_MAX - SCOPE_RANGE_MIN) * size[1])
            mirror = size[1] - 1 - expected
            assert pixels[expected - 2:expected + 3, :, :3].max() > 70
            assert pixels[mirror - 2:mirror + 3, :, :3].max() < 70
        else:
            # GL carries the lowest point of the B band in row zero.
            assert pixels[0, 123, 0] > pixels[99, 123, 0]
    finally:
        preview.release()
        output_fbo.release()
        source_fbo.release()
        output.release()
        source.release()
        gl.release()
