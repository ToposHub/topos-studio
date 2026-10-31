"""Regression coverage for Transform connections to Merge and Output."""

import pytest

from src.features.composite.core.format import BBox, ImageFormat
from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.render.composite_renderer import CompositeRenderer


pytestmark = pytest.mark.composite_core


def test_transform_as_merge_b_keeps_the_format_canvas() -> None:
    """A packed Transform B input must not shrink the Merge output canvas."""
    canvas = ImageFormat(width=1920, height=1080, name="HD")
    background = TextureResult(
        texture_id=1,
        width=1920,
        height=1080,
        format=canvas,
        bbox=BBox.full_frame(1920, 1080),
    )
    transformed = TextureResult(
        texture_id=2,
        width=720,
        height=405,
        format=canvas,
        bbox=BBox(x=600, y=300, width=720, height=405),
    )

    geometry = BaseEvaluatorMixin._compute_merge_geometry(
        background, transformed, None, "from_b", "union"
    )

    assert (geometry.output_width, geometry.output_height) == (1920, 1080)
    assert geometry.resolution_b == (720, 405)
    assert geometry.bbox_b == BBox(x=600, y=300, width=720, height=405)


def test_direct_output_places_a_packed_transform_into_its_input_canvas() -> None:
    """Transform -> Output must retain translation rather than crop to bbox."""
    output = OutputNode()
    canvas = ImageFormat(width=1920, height=1080, name="HD")
    transformed = TextureResult(
        texture_id=5,
        width=960,
        height=540,
        format=canvas,
        bbox=BBox(x=480, y=270, width=960, height=540),
    )
    calls = []

    def place(src, target_fmt, ctx, fb_manager, resource_pool):
        calls.append((src, target_fmt))
        return {"display": src}

    output._place_on_canvas = place
    result = output._apply_format(transformed, object(), object(), object())

    assert result["display"] is transformed
    assert calls == [(transformed, canvas)]


def test_output_ring_releases_framebuffer_before_texture() -> None:
    """Resize/cleanup cannot leave FBOs attached to released textures."""
    release_log = []

    class FakeFbo:
        def release(self):
            release_log.append("fbo")

    class FakeTexture:
        def release(self):
            release_log.append("tex")

    renderer = CompositeRenderer.__new__(CompositeRenderer)
    renderer.fbo_ring = [FakeFbo(), FakeFbo(), FakeFbo()]
    renderer.tex_ring = [FakeTexture(), FakeTexture(), FakeTexture()]
    renderer.write_idx = 2

    renderer._release_output_buffers()

    assert release_log == ["fbo", "tex"] * 3
    assert renderer.fbo_ring == [None, None, None]
    assert renderer.tex_ring == [None, None, None]
    assert renderer.write_idx == 0
