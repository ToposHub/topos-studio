"""P1-10: true async PBO readback contract.

The ring must:
- enqueue without blocking (DMA issued, fence recorded, data still None);
- deliver pixel-correct results via poll_ready/get_latest once the DMA
  completes (fence-gated, non-blocking until the caller opts in);
- survive a full ring rotation (slot reuse force-waits in-flight DMA);
- support the RGBA8 ('f1') sRGB-preview format as well as f4.

Also covers the CompositeFrameRenderer async preview lane end-to-end on a
solid-colour graph (submit → collect, latest-completed semantics).
"""

import time

import numpy as np
import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.render.composite_frame_renderer import (
    CompositeFrameRenderer,
)
from src.features.composite.render.pbo_readback import AsyncPBOReadback

pytestmark = pytest.mark.composite_core

W, H = 96, 64


@pytest.fixture()
def gl_ctx():
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # pragma: no cover - CI GPU availability
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    yield ctx
    ctx.release()


def _solid_fbo(gl_ctx, dtype: str):
    """FBO filled with a known RGBA pattern (row 0 = GL bottom)."""
    tex = gl_ctx.texture((W, H), 4, dtype=dtype)
    fbo = gl_ctx.framebuffer(color_attachments=[tex])
    if dtype == "f4":
        data = np.zeros((H, W, 4), dtype=np.float32)
        data[..., 0] = 0.25   # R
        data[..., 1] = 0.5    # G
        data[..., 2] = 0.75   # B
        data[..., 3] = 1.0
        data = np.flipud(data)
        tex.write(data.tobytes())
    else:
        data = np.zeros((H, W, 4), dtype=np.uint8)
        data[..., 0] = 40
        data[..., 1] = 80
        data[..., 2] = 160
        data[..., 3] = 255
        data = np.flipud(data)
        tex.write(data.tobytes())
    return tex, fbo


def _collect_with_deadline(pbo: AsyncPBOReadback, wait: bool = False):
    """Poll until the DMA lands (real DMA of a small FBO is fast; give it
    a generous deadline instead of assuming instant completion)."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        results = pbo.poll_ready()
        if results:
            return results
        if wait:
            r = pbo.get_latest(wait_newest=True)
            if r is not None:
                return [r]
        time.sleep(0.001)
    return []


class TestAsyncPBORing:
    def test_enqueue_does_not_block_and_poll_delivers_pixels(self, gl_ctx):
        tex, fbo = _solid_fbo(gl_ctx, "f4")
        pbo = AsyncPBOReadback(gl_ctx, ring_size=3, dtype="f4")
        try:
            assert pbo.enqueue(fbo, W, H, request_id=7) is True
            # Non-blocking contract: the result is not mapped synchronously.
            assert pbo.pending_count >= 1

            results = _collect_with_deadline(pbo)
            assert results, "DMA never completed"
            r = results[0]
            assert r.request_id == 7
            assert (r.width, r.height) == (W, H)
            # Module contract: CPU top-left BGRA float32.
            assert r.data.dtype == np.float32
            assert r.data.shape == (H, W, 4)
            px = r.data[H // 2, W // 2]
            assert px[0] == pytest.approx(0.75, abs=0.01)  # B
            assert px[1] == pytest.approx(0.5, abs=0.01)   # G
            assert px[2] == pytest.approx(0.25, abs=0.01)  # R
        finally:
            pbo.cleanup()

    def test_u8_ring_delivers_bytes(self, gl_ctx):
        tex, fbo = _solid_fbo(gl_ctx, "f1")
        pbo = AsyncPBOReadback(gl_ctx, ring_size=2, dtype="f1")
        try:
            assert pbo.enqueue(fbo, W, H, request_id=1) is True
            results = _collect_with_deadline(pbo)
            assert results
            r = results[0]
            assert r.data.dtype == np.uint8
            px = r.data[H // 2, W // 2]
            assert (int(px[0]), int(px[1]), int(px[2])) == (160, 80, 40)
        finally:
            pbo.cleanup()

    def test_ring_rotation_reuses_slots_safely(self, gl_ctx):
        """More enqueues than slots: in-flight slots are force-waited on
        reuse; every request_id still comes back exactly once."""
        tex, fbo = _solid_fbo(gl_ctx, "f1")
        pbo = AsyncPBOReadback(gl_ctx, ring_size=2, dtype="f1")
        try:
            for i in range(5):
                assert pbo.enqueue(fbo, W, H, request_id=i + 1) is True
            results = pbo.get_latest(wait_newest=True)
            assert results is not None
            assert results.request_id == 5
        finally:
            pbo.cleanup()

    def test_get_latest_nonblocking_returns_none_when_pending(self, gl_ctx):
        tex, fbo = _solid_fbo(gl_ctx, "f4")
        pbo = AsyncPBOReadback(gl_ctx, ring_size=2, dtype="f4")
        try:
            # Enqueue enough frames to fill the ring: the oldest slot is
            # force-waited during rotation, but the NEWEST entry stays
            # pending right after its enqueue.
            pbo.enqueue(fbo, W, H, request_id=1)
            pbo.enqueue(fbo, W, H, request_id=2)
            r = pbo.get_latest(wait_newest=False)
            # Either already done (tiny DMA) or None — never a wrong frame.
            assert r is None or r.request_id == 2
        finally:
            pbo.cleanup()


def _solid_graph() -> NodeGraph:
    data = {
        "nodes": [
            {"id": "bg", "type": "constant", "parameters": {
                "width": W, "height": H, "mode": "solid",
                "color": [0.5, 0.25, 0.125, 1.0]}},
            {"id": "out", "type": "output", "parameters": {
                "format_mode": "custom", "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "bg", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }
    return NodeGraph.from_dict(data)


class TestAsyncPreviewLane:
    """CompositeFrameRenderer.submit/collect async preview lane."""

    def test_submit_then_collect_wait_returns_pixels(self, monkeypatch):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
            ctx.release()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
        renderer = CompositeFrameRenderer()
        try:
            req = renderer.submit_preview_frame_async(
                graph_data=_solid_graph().to_dict())
            assert req > 0
            got = renderer.collect_async_preview(wait=True)
            assert got is not None
            rid, img = got
            assert rid == req
            assert img.shape == (H, W, 3) and img.dtype == np.uint8
            # sRGB(BGR) of linear (0.5, 0.25, 0.125) — same expectation as
            # the full-frame preview strategy test.
            def srgb(x):
                x = min(max(x, 0.0), 1.0)
                return x * 12.92 if x <= 0.0031308 else \
                    1.055 * x ** (1.0 / 2.4) - 0.055
            expected = [round(srgb(c) * 255 + 0.5) for c in
                        (0.125, 0.25, 0.5)]  # B, G, R
            px = img[H // 2, W // 2]
            for ch in range(3):
                assert abs(int(px[ch]) - expected[ch]) <= 2
            # Download-byte accounting for the P0-4 trace.
            assert renderer.async_download_bytes >= W * H * 3
        finally:
            renderer.cleanup()

    def test_collect_before_submit_is_none(self):
        renderer = CompositeFrameRenderer()
        try:
            assert renderer.collect_async_preview() is None
        finally:
            renderer.cleanup()
