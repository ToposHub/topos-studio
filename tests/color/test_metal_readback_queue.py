"""Metal texture readback on request (plan 4.6): explicit and playback apart.

Real device: a texture made from known values must come back as the same
values, the two purposes must be counted apart, a full queue must displace
playback readbacks but never a screenshot, a cancel must stop late pixels
from reaching the new state, and the queue must not touch the command
buffer its owner is encoding into.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from src.features.color.core.metal_readback_queue import (
    PURPOSE_EXPLICIT,
    PURPOSE_PLAYBACK,
    MetalReadbackQueue,
)
from src.shared.gpu.metal.context import MetalComputeContext

pytest.importorskip("Metal")

SIZE = (19, 13)          # not a multiple of any threadgroup


@pytest.fixture(scope="module")
def ctx():
    if not MetalComputeContext.is_supported():
        pytest.skip(f"no Metal device: {MetalComputeContext.availability_reason()[1]}")
    return MetalComputeContext()


def _texture(ctx, values=(0.0, 0.5, 2.0, -0.25), size=SIZE):
    """An RGBA16F texture: the same texel everywhere, values incl. HDR and negative."""
    import Metal

    texel = np.asarray(values, np.float16)
    return ctx.create_texture_from_numpy(
        np.tile(texel, (size[1], size[0], 1)), Metal.MTLPixelFormatRGBA16Float, *size)


@pytest.fixture()
def queue(ctx):
    return MetalReadbackQueue(ctx)


def _tokens(delivered) -> list:
    return [token for token, _pixels in delivered]


class TestPixels:
    def test_float_texels_come_back_bit_for_bit(self, ctx, queue) -> None:
        texture = _texture(ctx)
        assert queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT, token="frame-7") is True
        delivered = queue.wait_all(5.0)
        assert delivered is not None and _tokens(delivered) == ["frame-7"]
        (token, pixels), = delivered
        assert pixels.dtype == np.float16 and pixels.shape == (*SIZE[::-1], 4)
        assert np.all(pixels == np.asarray([0.0, 0.5, 2.0, -0.25], np.float16))
        assert queue.stats_snapshot()["explicit_readbacks"] == 1

    def test_every_pixel_is_in_its_place(self, ctx, queue) -> None:
        """A different texel per pixel: row order and the odd edges must survive."""
        import Metal

        width, height = 37, 23
        source = np.arange(width * height * 4, dtype=np.float32).reshape(height, width, 4) / 97.0
        texture = ctx.create_texture_from_numpy(source.astype(np.float16), Metal.MTLPixelFormatRGBA16Float, width, height)
        queue.request(texture, (width, height), purpose=PURPOSE_PLAYBACK)
        (_token, pixels), = queue.wait_all(5.0)
        assert pixels.astype(np.float32).tobytes() == source.astype(np.float16).astype(np.float32).tobytes()

    def test_uint8_is_the_display_value_saturated(self, ctx, queue) -> None:
        texture = _texture(ctx)
        queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT, dtype=np.uint8)
        (_token, pixels), = queue.wait_all(5.0)
        assert pixels.dtype == np.uint8
        assert np.all(pixels == np.asarray([0, 128, 255, 0], np.uint8))   # -0.25 and 2.0 clip

    def test_an_8bit_texture_reads_as_its_own_values(self, ctx, queue) -> None:
        import Metal

        source = np.arange(16 * 9 * 4, dtype=np.uint8).reshape(9, 16, 4)
        texture = ctx.create_texture_from_numpy(source, Metal.MTLPixelFormatRGBA8Unorm, 16, 9)
        queue.request(texture, (16, 9), purpose=PURPOSE_PLAYBACK, dtype=np.uint8)
        (_token, pixels), = queue.wait_all(5.0)
        assert np.array_equal(pixels, source)


class TestAdmission:
    def test_a_full_queue_drops_the_oldest_playback_readback(self, ctx, queue) -> None:
        queue._max_queued = 2
        texture = _texture(ctx)
        assert queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK, token="old") is True
        assert queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK, token="newer") is True
        assert queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK, token="newest") is True
        stats = queue.stats_snapshot()
        assert (stats["requested_playback"], stats["dropped_playback"]) == (3, 1)
        assert _tokens(queue.wait_all(5.0)) == ["newer", "newest"]

    def test_a_screenshot_is_refused_when_the_queue_is_full_never_displaced(self, ctx, queue) -> None:
        queue._max_queued = 2
        texture = _texture(ctx)
        assert queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT, token="shot") is True
        assert queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT, token="grab") is True
        # A third explicit request is refused: the caller retries, nothing is lost.
        assert queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT, token="third") is False
        # Nor does a playback request push the screenshots out.
        assert queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK, token="scope") is False
        assert queue.pending == 2
        assert _tokens(queue.wait_all(5.0)) == ["shot", "grab"]

    def test_bad_requests_are_refused_before_they_are_queued(self, ctx, queue) -> None:
        texture = _texture(ctx)
        with pytest.raises(ValueError, match="purpose"):
            queue.request(texture, SIZE, purpose="whatever")
        with pytest.raises(ValueError, match="dtype"):
            queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT, dtype=np.float64)
        with pytest.raises(ValueError, match="size"):
            queue.request(texture, (0, 4), purpose=PURPOSE_EXPLICIT)
        assert queue.pending == 0

    def test_something_that_is_not_a_texture_is_refused_at_the_door(self, ctx, queue) -> None:
        """Metal crashes the process on an object that is not a texture, so
        the queue checks before anything is encoded."""
        buffer = ctx.create_buffer(16)
        for not_a_texture in (object(), np.zeros((4, 4, 4), np.float16), buffer):
            with pytest.raises(TypeError, match="not a Metal texture"):
                queue.request(not_a_texture, SIZE, purpose=PURPOSE_EXPLICIT)
        assert queue.pending == 0
        assert queue.stats_snapshot()["requested_explicit"] == 0


class TestCounting:
    def test_explicit_and_playback_readbacks_are_counted_apart(self, ctx, queue) -> None:
        texture = _texture(ctx)
        for token in ("scope-1", "scope-2", "scope-3"):
            queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK, token=token)
        queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT, token="shot")
        assert len(queue.wait_all(5.0)) == 4
        assert queue.stats_snapshot() == {
            "requested_explicit": 1, "requested_playback": 3,
            "explicit_readbacks": 1, "playback_readbacks": 3,
            "dropped_playback": 0, "dropped_explicit": 0, "failed": 0}


class TestCancel:
    def test_cancelled_requests_are_not_copied(self, ctx, queue) -> None:
        texture = _texture(ctx)
        queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK, token="stale")
        queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT, token="old-shot")
        assert queue.cancel_all("page switch") == {PURPOSE_EXPLICIT: 1, PURPOSE_PLAYBACK: 1}
        assert queue.drain() == 0                 # nothing left to encode
        assert queue.wait_all(5.0) == []
        stats = queue.stats_snapshot()
        assert (stats["dropped_explicit"], stats["dropped_playback"]) == (1, 1)
        assert stats["explicit_readbacks"] == stats["playback_readbacks"] == 0

    def test_pixels_of_a_cancelled_state_never_arrive(self, ctx, queue) -> None:
        """Cancelled after the GPU was already copying: dropped when it lands."""
        texture = _texture(ctx)
        seen: list = []
        queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT, token="before-undo",
                      on_ready=lambda token, pixels: seen.append(token))
        assert queue.drain() == 1
        assert queue.cancel_all("undo") == {PURPOSE_EXPLICIT: 1, PURPOSE_PLAYBACK: 0}
        deadline = threading.Event()
        for _attempt in range(2000):              # let the completion land, then poll
            assert queue.poll() == []
            if queue.pending == 0:
                deadline.set()
                break
            threading.Event().wait(0.002)
        assert deadline.is_set() and seen == []
        assert queue.stats_snapshot()["explicit_readbacks"] == 0

    def test_cancelling_twice_counts_each_request_once(self, ctx, queue) -> None:
        texture = _texture(ctx)
        queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT)
        queue.drain()
        first = queue.cancel_all("switch")
        second = queue.cancel_all("switch again")
        assert first == {PURPOSE_EXPLICIT: 1, PURPOSE_PLAYBACK: 0}
        assert second == {PURPOSE_EXPLICIT: 0, PURPOSE_PLAYBACK: 0}
        queue.wait_all(5.0)
        assert queue.stats_snapshot()["dropped_explicit"] == 1


class TestDelivery:
    def test_the_callback_gets_the_token_and_the_pixels(self, ctx, queue) -> None:
        texture = _texture(ctx)
        got: list = []
        queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK, token=("frame", 12),
                      on_ready=lambda token, pixels: got.append((token, pixels.shape)))
        queue.wait_all(5.0)
        assert got == [(("frame", 12), (SIZE[1], SIZE[0], 4))]

    def test_a_raising_callback_does_not_stop_the_others(self, ctx, queue) -> None:
        texture = _texture(ctx)

        def explode(token, pixels) -> None:
            raise RuntimeError("callback failed")

        queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK, token="first", on_ready=explode)
        queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK, token="second")
        assert _tokens(queue.wait_all(5.0)) == ["first", "second"]
        assert queue.stats_snapshot()["playback_readbacks"] == 2

    def test_requests_from_other_threads_are_queued_and_served(self, ctx, queue) -> None:
        """Four threads ask while nobody drains.  Playback readbacks only ever
        keep the newest ones: 80 asked, the 8 the queue holds are delivered,
        the other 72 are counted as displaced -- nothing lost or doubled."""
        texture = _texture(ctx)

        def ask(thread_index: int) -> None:
            for index in range(20):
                assert queue.request(texture, SIZE, purpose=PURPOSE_PLAYBACK,
                                     token=(thread_index, index))

        threads = [threading.Thread(target=ask, args=(number,)) for number in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
        delivered = queue.wait_all(5.0)
        tokens = _tokens(delivered)
        stats = queue.stats_snapshot()
        assert len(delivered) == len(set(tokens)) == stats["playback_readbacks"] == 8
        assert (stats["requested_playback"], stats["dropped_playback"]) == (80, 72)
        assert queue.pending == 0

    def test_wait_all_gives_up_after_the_timeout(self, ctx, queue, monkeypatch) -> None:
        texture = _texture(ctx)
        queue.request(texture, SIZE, purpose=PURPOSE_EXPLICIT)
        monkeypatch.setattr(queue, "drain", lambda: 0)   # nothing ever gets encoded
        assert queue.wait_all(0.05) is None
        assert queue.pending == 1

    def test_the_queue_leaves_the_owners_command_buffer_alone(self, ctx, queue) -> None:
        """A readback must not commit the work its owner is still encoding."""
        import Metal

        scratch = ctx.create_texture_2d(Metal.MTLPixelFormatRGBA16Float, 4, 4, shader_write=True)
        copy = ctx.compile_kernel("owner_copy", """
#include <metal_stdlib>
using namespace metal;
kernel void owner_copy(texture2d<float> s [[texture(0)]], texture2d<float, access::write> d [[texture(1)]],
                        uint2 p [[thread_position_in_grid]]) {
    if (p.x >= d.get_width()) return;
    d.write(float4(0.25, 0.5, 0.75, 1.0), p);
}""")
        ctx.dispatch(copy, textures={0: _texture(ctx, size=(4, 4)), 1: scratch}, threads=(4, 4, 1))
        assert ctx.has_pending_work                     # encoded, not committed
        queue.request(_texture(ctx), SIZE, purpose=PURPOSE_EXPLICIT)
        assert len(queue.wait_all(5.0)) == 1
        assert ctx.has_pending_work                     # still the owner's to commit
        assert ctx.submit(wait=True) is not None        # and it commits as usual


def test_nothing_is_delivered_before_drain(ctx, queue) -> None:
    queue.request(_texture(ctx), SIZE, purpose=PURPOSE_PLAYBACK)
    assert queue.poll() == []
    assert queue.pending == 1
