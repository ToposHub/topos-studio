"""Regression tests for ColorStack visibility in the Edit preview path."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.edit.core.project import ColorStackData
from src.features.edit.core.timeline_preview_renderer import (
    AssetResolver,
    PreviewRenderWorker,
)


def _worker_for_stack(stack: ColorStackData) -> PreviewRenderWorker:
    project = SimpleNamespace(color_stacks={stack.stack_id: stack})
    return PreviewRenderWorker(
        frame_provider=MagicMock(),
        asset_resolver=AssetResolver(project),
        video_renderer=None,
    )


def _layer(stack_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        clip=SimpleNamespace(color_stack_id=stack_id),
        clip_id="clip-1",
        asset_id="asset-1",
        composite_id=None,
        opacity=1.0,
        transform=(0.0, 0.0, 1.0, 1.0, 0.0, 0.5, 0.5),
        crop=(0.0, 0.0, 0.0, 0.0, 0.0),
        is_in_transition=False,
    )


def test_graph_packet_binds_task_source_frame_without_mutating_cached_grade():
    graph = object()
    cached = {"color_graph": graph, "source_frame": 10}
    first = PreviewRenderWorker._evaluate_preview_grade_at(cached, 11)
    second = PreviewRenderWorker._evaluate_preview_grade_at(cached, 12)
    assert first["source_frame"] == 11
    assert second["source_frame"] == 12
    assert cached["source_frame"] == 10
    assert first["color_graph"] is graph


def test_active_color_stack_disables_raw_direct_preview() -> None:
    stack = ColorStackData(
        stack_id="stack-1",
        grading_params={"exposure": 25.0},
    )
    worker = _worker_for_stack(stack)
    try:
        layer = _layer(stack.stack_id)
        grade = worker._get_preview_color_grade(layer)

        assert grade is not None
        assert grade["stack_id"] == stack.stack_id
        assert worker._is_identity_layer(layer) is False
    finally:
        worker._decode_executor.shutdown(wait=True)


def test_cpu_fallback_applies_the_same_primary_grade() -> None:
    stack = ColorStackData(
        stack_id="stack-1",
        grading_params={"exposure": 50.0},
    )
    worker = _worker_for_stack(stack)
    try:
        frame_bgr = np.full((2, 2, 3), 32, dtype=np.uint8)
        graded = worker._apply_preview_color_grade(frame_bgr, _layer(stack.stack_id))

        assert np.all(graded > frame_bgr)
        assert graded.dtype == np.uint8
    finally:
        worker._decode_executor.shutdown(wait=True)


def test_gpu_color_stack_shader_compiles() -> None:
    """The Edit preview must have a working GPU ColorStack grade pass."""
    moderngl = pytest.importorskip("moderngl")
    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer

    try:
        ctx = moderngl.create_standalone_context()
    except Exception as error:
        pytest.skip(f"Failed to create OpenGL context: {error}")

    renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
    renderer.ctx = ctx
    try:
        assert renderer._init_shaders() is True
        assert renderer._grade_program is not None
    finally:
        ctx.release()


def test_gpu_program_composites_independent_per_track_luts(tmp_path: Path) -> None:
    from PyQt6.QtCore import QThread
    from src.features.color.core.color_graph_migration import migrate_legacy_stack
    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
    from tests.color.fixtures.color_graph import _neutral_layer
    from tests.color.test_color_graph_migration import _stack_with_layers

    moderngl = pytest.importorskip("moderngl")
    colors = [(0.8, 0.2, 0.1), (0.1, 0.8, 0.2), (0.2, 0.1, 0.8), (0.7, 0.5, 0.2)]
    graphs = []
    for index, (r, g, b) in enumerate(colors):
        cube = tmp_path / f"track-{index}.cube"
        cube.write_text("LUT_3D_SIZE 2\n" + f"{r} {g} {b}\n" * 8)
        layer = _neutral_layer(f"Track {index}")
        layer.active_lut_path = str(cube)
        graphs.append(migrate_legacy_stack(_stack_with_layers([layer])))

    def render() -> np.ndarray:
        try:
            ctx = moderngl.create_standalone_context(require=330)
        except Exception as error:
            pytest.skip(f"No offscreen GL context: {error}")
        preview = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        QThread.__init__(preview)
        preview.ctx = ctx
        try:
            assert preview._init_shaders()
            preview._init_quad()
            preview._graded_track_fbos = []
            preview._graded_track_texes = []
            preview._working_dtype = "f4"
            size = (8, 4)
            preview._composite_tex = ctx.texture(size, 4, dtype="f4")
            preview._composite_fbo = ctx.framebuffer([preview._composite_tex])
            pixels = np.full((4, 8, 4), 0.4, "f4")
            pixels[..., 3] = 1.0
            source = ctx.texture(size, 4, pixels, dtype="f4")
            tracks = []
            for index, graph in enumerate(graphs):
                tex = preview._apply_preview_grade(
                    source, index, *size, {"color_graph": graph, "source_frame": 7})
                tracks.append((tex, 1.0 if index == 0 else 0.5))
            out = preview._composite_tracks(tracks, *size)
            return np.frombuffer(out.read(), "f4").reshape(4, 8, 4)[2, 4, :3].copy()
        finally:
            graph_renderer = getattr(preview, "_color_graph_texture_renderer", None)
            if graph_renderer is not None:
                graph_renderer.release()
            ctx.release()

    with ThreadPoolExecutor(max_workers=1) as pool:
        actual = pool.submit(render).result()
    expected = np.array(colors[0])
    for overlay in colors[1:]:
        expected = 0.5 * expected + 0.5 * np.array(overlay)
    np.testing.assert_allclose(actual, expected, atol=0.004)
