"""Regression coverage for Transform connections to Merge and Output."""

from unittest.mock import Mock

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


def test_custom_output_resizes_a_decoded_proxy_to_fill_the_canvas() -> None:
    output = OutputNode()
    output.parameters.update(format_mode="custom", width=3840, height=2160)
    proxy = TextureResult(texture_id=5, width=1920, height=1080, revision=4)
    output._resize = Mock(return_value={"display": TextureResult(
        texture_id=6, width=3840, height=2160, revision=proxy.revision,
    )})
    output._place_on_canvas = Mock()
    ctx, fb_manager, pool = object(), object(), object()

    display = output._apply_format(proxy, ctx, fb_manager, pool)["display"]

    output._resize.assert_called_once_with(proxy, 3840, 2160, ctx, fb_manager, pool)
    output._place_on_canvas.assert_not_called()
    assert display.bbox == BBox.full_frame(3840, 2160)
    assert display.revision == proxy.revision


def test_custom_output_keeps_geometry_of_a_different_aspect_ratio() -> None:
    portrait = TextureResult(texture_id=5, width=1080, height=1920)
    landscape = ImageFormat(width=3840, height=2160, name="UHD")

    assert OutputNode._needs_canvas_placement(portrait, landscape)


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
