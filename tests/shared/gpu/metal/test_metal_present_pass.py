"""The native present pass on a real Metal device (Metal migration plan 2.1).

The pass draws one frame texture into a color attachment with a letterbox
rectangle.  Verified here offscreen — no window, no display — by reading the
attachment back: row order for both texture conventions, letterbox bars in
the background colour, and the picture scaled into its rectangle.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.shared.gpu.metal.context import MetalComputeContext, MetalComputeError
from src.shared.gpu.metal.present_port import (
    DEFAULT_BACKGROUND,
    HeldFrame,
    MetalPresentPass,
    ORIGIN_BOTTOM_LEFT,
    ORIGIN_TOP_LEFT,
    normalized_rect,
)

_METAL_OK, _METAL_REASON = MetalComputeContext.availability_reason()
requires_mac_metal = pytest.mark.skipif(
    not _METAL_OK, reason=f"needs a Metal device ({_METAL_REASON})")

W, H = 64, 32
RED, GREEN, BLUE = ((0, 0, 255, 255), (0, 255, 0, 255), (255, 0, 0, 255))  # BGRA bytes


class _OffscreenSink:
    """A single reusable color attachment standing in for a drawable."""

    pixel_format = 80  # MTLPixelFormatBGRA8Unorm

    def __init__(self, target) -> None:
        self.target = target
        self.command_buffer = None
        self.presented = 0

    def next_drawable(self):
        return self.target

    @staticmethod
    def drawable_texture(drawable):
        return drawable

    def present(self, command_buffer, drawable) -> None:
        self.command_buffer = command_buffer

    def on_presented(self, drawable, callback) -> None:
        callback()


def _render_target(device, metal):
    descriptor = metal.MTLTextureDescriptor.texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
        metal.MTLPixelFormatBGRA8Unorm, W, H, False)
    descriptor.setUsage_(metal.MTLTextureUsageRenderTarget | metal.MTLTextureUsageShaderRead)
    descriptor.setStorageMode_(
        metal.MTLStorageModeShared if device.hasUnifiedMemory()
        else metal.MTLStorageModeManaged)
    return device.newTextureWithDescriptor_(descriptor)


def _source(context, rows) -> "object":
    """A W×H BGRA8 texture; ``rows[r]`` is the BGRA colour of row ``r``."""
    image = np.zeros((H, W, 4), dtype=np.uint8)
    for index, colour in enumerate(rows):
        image[index::len(rows)] = colour
    return context.create_texture_from_numpy(image, 80, W, H)


def _read(context, target) -> np.ndarray:
    raw = context.read_texture(target, W * 4, H, dtype=np.uint8, region_width=W)
    return raw.reshape(H, W, 4)


def _near(actual, expected, tolerance=3) -> bool:
    return all(abs(int(a) - int(e)) <= tolerance for a, e in zip(actual, expected))


@pytest.fixture(scope="module")
def metal_context():
    try:
        context = MetalComputeContext()
    except MetalComputeError as exc:
        pytest.skip(str(exc))
    import Metal

    return context, Metal


def _draw(context, present_pass, source, *, origin, rect=(0.0, 0.0, 1.0, 1.0)):
    target = _render_target(context.device, present_pass._metal)
    sink = _OffscreenSink(target)
    frame = SimpleNamespace(native=SimpleNamespace(
        resource=source, origin=origin, background=None))
    present_pass.draw(
        HeldFrame(frame=frame, rect=rect, display_rect_px=(0, 0, W, H)),
        target, sink,
        on_complete=lambda command_buffer: None,
        on_actual_present=lambda: None,
    )
    sink.command_buffer.waitUntilCompleted()
    assert sink.command_buffer.error() is None
    return _read(context, target)


@requires_mac_metal
class TestPresentPass:
    def test_top_left_origin_texture_is_shown_upright(self, metal_context) -> None:
        context, metal = metal_context
        present_pass = MetalPresentPass(context, metal.MTLPixelFormatBGRA8Unorm)
        # Row 0 red, last row blue: a compute output whose row 0 is the top.
        image = np.zeros((H, W, 4), dtype=np.uint8)
        image[0, :, :] = RED
        image[-1, :, :] = BLUE
        source = context.create_texture_from_numpy(image, metal.MTLPixelFormatBGRA8Unorm, W, H)
        result = _draw(context, present_pass, source, origin=ORIGIN_TOP_LEFT)

        assert _near(result[0, W // 2], RED), result[0, W // 2]
        assert _near(result[-1, W // 2], BLUE), result[-1, W // 2]
        # Left/right are not mirrored.
        image[:, 0, :] = GREEN
        image[:, -1, :] = RED
        image[0, :, :] = RED
        image[-1, :, :] = BLUE
        image[1:-1, 1:-1, :] = 0
        source = context.create_texture_from_numpy(image, metal.MTLPixelFormatBGRA8Unorm, W, H)
        result = _draw(context, present_pass, source, origin=ORIGIN_TOP_LEFT)
        assert _near(result[H // 2, 0], GREEN) and _near(result[H // 2, -1], RED)

    def test_bottom_left_origin_texture_is_flipped(self, metal_context) -> None:
        context, metal = metal_context
        present_pass = MetalPresentPass(context, metal.MTLPixelFormatBGRA8Unorm)
        # An FBO-style attachment: row 0 is the bottom of the picture.
        image = np.zeros((H, W, 4), dtype=np.uint8)
        image[0, :, :] = BLUE       # bottom row of the picture
        image[-1, :, :] = RED       # top row of the picture
        source = context.create_texture_from_numpy(image, metal.MTLPixelFormatBGRA8Unorm, W, H)
        result = _draw(context, present_pass, source, origin=ORIGIN_BOTTOM_LEFT)
        assert _near(result[0, W // 2], RED), result[0, W // 2]
        assert _near(result[-1, W // 2], BLUE), result[-1, W // 2]

    def test_letterbox_bars_keep_the_background(self, metal_context) -> None:
        context, metal = metal_context
        present_pass = MetalPresentPass(context, metal.MTLPixelFormatBGRA8Unorm)
        image = np.zeros((H, W, 4), dtype=np.uint8)
        image[:, :, :3] = (40, 80, 200)
        source = context.create_texture_from_numpy(image, metal.MTLPixelFormatBGRA8Unorm, W, H)
        # The picture occupies the middle half of the height: 8 px bars.
        rect = normalized_rect((0, 8, W, 16), (W, H))
        result = _draw(context, present_pass, source, origin=ORIGIN_TOP_LEFT, rect=rect)

        background = tuple(round(value * 255) for value in DEFAULT_BACKGROUND)
        assert _near(result[0, W // 2], background), result[0, W // 2]
        assert _near(result[-1, W // 2], background), result[-1, W // 2]
        # Inside the bars the picture colour is scaled up, not stretched.
        inside = result[H // 2, W // 2]
        assert _near(inside[:3], (40, 80, 200), tolerance=8), inside
        # Side bars when the picture is pillarboxed.
        rect = normalized_rect((16, 0, 32, H), (W, H))
        result = _draw(context, present_pass, source, origin=ORIGIN_TOP_LEFT, rect=rect)
        assert _near(result[H // 2, 0], background) and _near(result[H // 2, -1], background)
        assert _near(result[H // 2, W // 2][:3], (40, 80, 200), tolerance=8)

    def test_alpha_is_opaque_everywhere(self, metal_context) -> None:
        context, metal = metal_context
        present_pass = MetalPresentPass(context, metal.MTLPixelFormatBGRA8Unorm)
        image = np.zeros((H, W, 4), dtype=np.uint8)     # alpha 0 in the source
        image[:, :, 2] = 90
        source = context.create_texture_from_numpy(image, metal.MTLPixelFormatBGRA8Unorm, W, H)
        result = _draw(context, present_pass, source, origin=ORIGIN_TOP_LEFT,
                       rect=normalized_rect((8, 8, 48, 16), (W, H)))
        assert (result[:, :, 3] == 255).all()
