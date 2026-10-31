"""Regression tests for ColorStack visibility in the Edit preview path."""

from __future__ import annotations

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
