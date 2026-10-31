"""TL-P0-02 / P0-5: CompositeGpuFrameHandle producer/consumer contract.

The comp-clip GPU direct path must:
- produce a presentable handle (metadata complete, fence set) without any
  CPU readback;
- expose an idempotent lease whose release returns the texture to the
  producer pool exactly once;
- invalidate stale generations (never presentable);
- degrade explicitly (available=False + fallback_reason) instead of
  handing out dead texture ids.
"""

import threading

import numpy as np
import pytest

from src.shared.gpu.composite_gpu_frame_handle import (
    CompositeGpuFrameHandle,
    make_unavailable_handle,
)

pytestmark = pytest.mark.composite_core

W, H = 256, 192
SOLID = (0.25, 0.5, 0.75, 1.0)


def _solid_graph() -> "NodeGraph":
    from src.features.composite.core.node_engine import NodeGraph
    data = {
        "nodes": [
            {"id": "bg", "type": "constant", "parameters": {
                "width": W, "height": H, "mode": "solid",
                "color": list(SOLID)}},
            {"id": "out", "type": "output", "parameters": {
                "format_mode": "custom", "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "bg", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }
    return NodeGraph.from_dict(data)


@pytest.fixture()
def headless_renderer(monkeypatch):
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
        ctx.release()
    except Exception as exc:  # pragma: no cover - CI GPU availability
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
    from src.features.composite.render.composite_export_renderer import (
        CompositeExportRenderer,
    )
    renderer = CompositeExportRenderer()
    renderer.set_persistent_mode(True)
    yield renderer
    renderer.cleanup()


class TestHandleDataclass:
    """Pure-python contract — no GL required."""

    def test_stale_generation_never_presentable(self):
        handle = CompositeGpuFrameHandle(texture_id=42, generation=7)
        assert handle.is_presentable(7)
        assert not handle.is_presentable(8)
        assert not handle.is_presentable(None) is False  # None = no check

    def test_unavailable_handle_never_presentable(self):
        handle = make_unavailable_handle("device lost", generation=3)
        assert not handle.available
        assert handle.fallback_reason == "device lost"
        assert not handle.is_presentable(3)

    def test_release_is_idempotent_and_thread_safe(self):
        calls = []

        def _cb():
            calls.append(1)

        handle = CompositeGpuFrameHandle(texture_id=1, release_callback=_cb)
        threads = [threading.Thread(target=handle.release) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        handle.release()
        assert len(calls) == 1
        assert handle.released

    def test_as_dict_hides_fence_and_callbacks(self):
        handle = CompositeGpuFrameHandle(
            texture_id=5, width=W, height=H, fence_ns=12345,
            release_callback=lambda: None)
        d = handle.as_dict()
        assert d["has_fence"] is True
        assert "fence_ns" not in d and "release_callback" not in d
        assert d["pixel_format"] == "f2"


class TestGpuHandleProducer:
    """Producer path through a real standalone GL context."""

    def test_produces_presentable_handle_without_readback(
            self, headless_renderer):
        snapshot = _solid_graph().snapshot(
            color_texture_id=0, color_width=0, color_height=0,
            color_revision=0)
        handle = headless_renderer.render_gpu_handle(
            snapshot, frame_number=11, generation=4)
        try:
            assert handle.available, handle.fallback_reason
            assert handle.is_presentable(4)
            assert handle.texture_id > 0
            assert (handle.width, handle.height) == (W, H)
            assert handle.pixel_format == "f2"
            assert handle.color_space == "linear"
            # The handle carries the producer's alpha semantics verbatim.
            assert isinstance(handle.alpha_premultiplied, bool)
            assert handle.is_gl_oriented is True
            assert handle.backend == "opengl"
            assert handle.device_identity  # GL renderer string
            assert handle.frame == 11 and handle.generation == 4
        finally:
            handle.release()
        assert handle.released

    def test_lease_release_returns_texture_to_pool(self, headless_renderer):
        snapshot = _solid_graph().snapshot(
            color_texture_id=0, color_width=0, color_height=0,
            color_revision=0)
        handle = headless_renderer.render_gpu_handle(snapshot)
        texture_id = handle.texture_id
        handle.release()
        # The released texture must be back in the pool's free set, and a
        # subsequent acquire must reuse the same GL texture (no leak).
        pooled = []
        for lst in headless_renderer._resource_pool._pooled_textures.values():
            pooled.extend(t.glo for t in lst)
        assert texture_id in pooled
        tex = headless_renderer._resource_pool.acquire_texture(
            W, H, components=4, dtype="f2")
        assert tex.glo == texture_id
        headless_renderer._resource_pool.release_texture(tex)

    def test_handle_content_matches_source(self, headless_renderer):
        snapshot = _solid_graph().snapshot(
            color_texture_id=0, color_width=0, color_height=0,
            color_revision=0)
        handle = headless_renderer.render_gpu_handle(snapshot)
        try:
            assert handle.available
            assert handle.has_fence(), (
                "standalone contexts support sync objects; the producer "
                "must not silently fall back to finish()")
            tex = next(
                (t for t in
                 headless_renderer.fb_manager._active_textures
                 if t.glo == handle.texture_id), None)
            assert tex is not None
            fbo = headless_renderer.fb_manager.get_fbo(tex)
            fbo.use()
            headless_renderer.ctx.viewport = (0, 0, W, H)
            raw = fbo.read(components=4, dtype="f4")
            arr = np.flipud(np.frombuffer(
                raw, dtype=np.float32).reshape(H, W, 4))
            # Solid constant: sampled colour matches on every channel
            # (half-float storage ⇒ loose tolerance).
            assert arr[..., 0].mean() == pytest.approx(SOLID[0], abs=0.02)
            assert arr[..., 1].mean() == pytest.approx(SOLID[1], abs=0.02)
            assert arr[..., 2].mean() == pytest.approx(SOLID[2], abs=0.02)
        finally:
            handle.release()

    def test_failure_degrades_explicitly(self, headless_renderer):
        from src.features.composite.core.node_engine import NodeGraph
        # An Output node with nothing upstream — evaluation cannot produce
        # a beauty texture, so the producer must return an explicit
        # fallback (never a dead texture id, never an exception).
        data = {
            "nodes": [
                {"id": "out", "type": "output", "parameters": {
                    "format_mode": "custom", "width": W, "height": H}},
            ],
            "connections": [],
        }
        g = NodeGraph.from_dict(data)
        snapshot = g.snapshot(color_texture_id=0, color_width=0,
                              color_height=0, color_revision=0)
        handle = headless_renderer.render_gpu_handle(snapshot)
        assert not handle.available
        assert handle.fallback_reason
        assert not handle.is_presentable(0)


class TestDeferredFenceDeletion:
    """P0-5 audit fix: consumers release handles on their own thread where
    no GL context is current — glDeleteSync there would fail silently and
    leak one sync object per frame. The release callback must QUEUE the
    fence instead; the producer drains the queue with its context current.
    """

    def test_drain_deletes_queued_fences_in_order(self):
        from unittest.mock import patch

        from src.features.composite.render.composite_export_renderer import (
            CompositeExportRenderer,
        )
        renderer = CompositeExportRenderer()
        renderer._deferred_fence_deletes.append(111)
        renderer._deferred_fence_deletes.append(222)
        with patch(
            "src.shared.gpu.composite_gpu_frame_handle.delete_fence"
        ) as df:
            renderer._drain_deferred_fence_deletes()
        assert [c.args[0] for c in df.call_args_list] == [111, 222]
        # Queue fully drained — no sync object left behind.
        assert not renderer._deferred_fence_deletes

    def test_drain_noop_on_empty_queue(self):
        from unittest.mock import patch

        from src.features.composite.render.composite_export_renderer import (
            CompositeExportRenderer,
        )
        renderer = CompositeExportRenderer()
        with patch(
            "src.shared.gpu.composite_gpu_frame_handle.delete_fence"
        ) as df:
            renderer._drain_deferred_fence_deletes()
        df.assert_not_called()
