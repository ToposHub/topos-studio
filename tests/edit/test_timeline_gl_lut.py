"""Ownership, order and color checks for asynchronous planar LUT export."""
from __future__ import annotations

import queue
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from src.features.edit.core import timeline_export
from src.features.edit.core.timeline_export import ExportCodec, TimelineExporter
from src.features.edit.core.timeline_gl_lut import PlanarLutReadback, _PixelOwner
from src.shared.media.frame import DecodedFrame


def test_hqx_host_cache_retains_pixels_until_avframe_releases_them() -> None:
    av = pytest.importorskip("av")
    cache: queue.LifoQueue[np.ndarray] = queue.LifoQueue()
    pixels = np.arange(256, dtype=np.uint16)
    leased = np.asarray(_PixelOwner(pixels, cache))
    try:
        frame = av.VideoFrame.from_dlpack(
            (leased[:128].reshape(4, 32), leased[128:192].reshape(4, 16),
             leased[192:].reshape(4, 16)), format="yuv422p10le",
        )
    except (AttributeError, NotImplementedError, ValueError):
        pytest.skip("PyAV lacks planar DLPack support")
    del leased
    assert cache.empty()
    assert frame.planes[0].buffer_ptr == pixels.ctypes.data
    del frame
    assert cache.get_nowait() is pixels


@pytest.mark.parametrize("failure", [None, "submit", "readback"])
@pytest.mark.parametrize("codec", [ExportCodec.DNXHR_HQX, ExportCodec.H264, ExportCodec.TOPOS])
def test_planar_lut_pipeline_orders_fallback_and_flushes_tail(
    monkeypatch: pytest.MonkeyPatch, failure: str | None, codec: ExportCodec,
) -> None:
    monkeypatch.setattr(timeline_export, "sys", SimpleNamespace(platform="win32"))
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._timeline_graphics_backend = "opengl"
    exporter._gpu_render_disabled = False
    exporter._opengl_planar_lut_frames = 0
    exporter._cancel_requested = False
    exporter._get_output_resolution = lambda _config: (3840, 2160)
    exporter._get_sequence_resolution = lambda: (3840, 2160)
    exporter._discard_topos_lut_prefetch = lambda: None
    exporter._wait_if_paused = lambda **_kwargs: True
    exporter._notify_progress = lambda: None
    exporter._export_telemetry = SimpleNamespace(record_stage_sample=lambda *_args, **_kwargs: None)
    recorded = []
    exporter._encode_frame = lambda frame: recorded.append((frame.frame_index, frame.route))
    exporter._render_frame = lambda index, _config: SimpleNamespace(frame_index=index, route="normal")
    released = []
    exporter._release_gpu_export_renderer = lambda: released.append(True)

    def submit(index: int, _config: object) -> object:
        if index == 2:
            return None
        if index == 5 and failure == "submit":
            raise RuntimeError("test submit failure")

        def finish() -> object:
            if index == 4 and failure == "readback":
                raise RuntimeError("test fence failure")
            return SimpleNamespace(frame_index=index, route="gpu", planes=(np.empty(1),))

        return SimpleNamespace(frame_index=index, finish=finish)

    exporter._submit_opengl_planar_lut = submit
    exporter._render_encode_pipeline(0, 7, SimpleNamespace(codec=codec))

    assert [index for index, _ in recorded] == list(range(7))
    expected = ["gpu", "gpu", "normal", "gpu", "gpu", "gpu", "gpu"]
    if failure == "submit":
        expected[3:] = ["normal"] * 4
    elif failure == "readback":
        expected[4:] = ["normal"] * 3
    assert [route for _, route in recorded] == expected
    assert len(released) == (2 if failure else 1)


@pytest.mark.parametrize("failure", ["cancel", "encode"])
def test_pipeline_returns_queued_readback_leases_on_early_exit(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    monkeypatch.setattr(timeline_export, "sys", SimpleNamespace(platform="win32"))
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._timeline_graphics_backend = "opengl"
    exporter._gpu_render_disabled = False
    exporter._opengl_planar_lut_frames = 0
    exporter._cancel_requested = False
    exporter._get_output_resolution = lambda _config: (3840, 2160)
    exporter._get_sequence_resolution = lambda: (3840, 2160)
    exporter._discard_topos_lut_prefetch = lambda: None
    exporter._wait_if_paused = lambda **_kwargs: True
    exporter._notify_progress = lambda: None
    exporter._export_telemetry = SimpleNamespace(record_stage_sample=lambda *_args, **_kwargs: None)
    borrowed: set[int] = set()
    all_returned = threading.Condition()
    queue_filled = threading.Event()

    def finish(index: int) -> DecodedFrame:
        with all_returned:
            borrowed.add(index)
            if len(borrowed) == timeline_export._RENDER_ENCODE_QUEUE_DEPTH + 1:
                queue_filled.set()

        def release() -> None:
            with all_returned:
                borrowed.remove(index)
                all_returned.notify()

        return DecodedFrame(
            index, index, float(index), planes=(np.empty((1, 1), np.uint16),),
            extra={"opengl_output_buffer_lease": SimpleNamespace(release=release)},
        )

    def release_renderer() -> None:
        with all_returned:
            assert all_returned.wait_for(lambda: not borrowed, timeout=2)

    def encode(frame: DecodedFrame) -> None:
        assert frame.frame_index == 0
        assert queue_filled.wait(timeout=2)
        if failure == "encode":
            raise RuntimeError("injected encoder failure")
        exporter._cancel_requested = True

    exporter._submit_opengl_planar_lut = lambda index, _config: SimpleNamespace(
        frame_index=index, finish=lambda: finish(index))
    exporter._release_gpu_export_renderer = release_renderer
    exporter._encode_frame = encode
    config = SimpleNamespace(codec=ExportCodec.TOPOS)
    if failure == "encode":
        with pytest.raises(RuntimeError, match="injected encoder failure"):
            exporter._render_encode_pipeline(0, 10, config)
    else:
        exporter._render_encode_pipeline(0, 10, config)
    assert not borrowed


@pytest.mark.parametrize("borrow_slots", [0, 4])
def test_topos_422_atlas_preserves_planes_across_readback_slots(borrow_slots: int) -> None:
    moderngl = pytest.importorskip("moderngl")
    pytest.importorskip("OpenGL")
    from src.features.edit.core.timeline_gpu_export_renderer import _VERT_SRC

    try:
        ctx = moderngl.create_standalone_context(require=440)
    except Exception as error:
        pytest.skip(f"OpenGL 4.4 unavailable: {error}")
    atlas = ctx.texture((32, 16), 1, dtype="u2")
    fbo = ctx.framebuffer(atlas)
    ring = None
    finished_frames = []
    try:
        try:
            ring = PlanarLutReadback(
                ctx, _VERT_SRC, None, 32, 8, "yuv422p10le",
                existing_422_atlas=True, borrow_slots=borrow_slots)
        except RuntimeError as error:
            pytest.skip(str(error))
        pixels = np.arange(512, dtype=np.uint16)
        atlas.write(pixels)
        first = ring.capture_422_atlas(fbo, 0, 0.0)
        atlas.write(pixels + 512)
        second = ring.capture_422_atlas(fbo, 1, 0.04)
        old = first.finish()
        finished_frames.append(old)
        new = second.finish()
        finished_frames.append(new)
        assert new.frame_index == 1
        if borrow_slots:
            assert new.planes[0].ctypes.data == second.slot.address
            assert not new.planes[0].flags.writeable
            assert ring._available_slot() not in (first.slot, second.slot)
        else:
            assert new.extra is None and new.planes[0].ctypes.data != second.slot.address
        for frame, offset in ((old, 0), (new, 512)):
            for plane, expected in zip(frame.planes, np.split(pixels + offset, (256, 384)), strict=True):
                np.testing.assert_array_equal(plane.ravel(), expected)
    finally:
        for frame in finished_frames:
            TimelineExporter._release_rendered_frame(frame)
        if ring is not None:
            ring.release()
        fbo.release()
        atlas.release()
        ctx.release()


@pytest.mark.parametrize("pixel_format", ["yuv422p10le", "yuv420p"])
def test_gl_reads_declared_plane_order_and_preserves_owned_frames(pixel_format: str) -> None:
    av = pytest.importorskip("av")
    moderngl = pytest.importorskip("moderngl")
    pytest.importorskip("OpenGL")
    from src.features.edit.core.timeline_gpu_export_renderer import _VERT_SRC

    try:
        ctx = moderngl.create_standalone_context(require=440)
    except Exception as error:
        pytest.skip(f"OpenGL 4.4 unavailable: {error}")
    quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype=np.float32))
    readback = None
    lut = None
    steep = None
    try:
        try:
            readback = PlanarLutReadback(ctx, _VERT_SRC, quad, 32, 8, pixel_format)
        except (RuntimeError, AttributeError, NotImplementedError, ValueError) as error:
            pytest.skip(str(error))
        # Swap red and blue, including a saturated chroma edge, on a 2x2x2 LUT.
        table = np.array([(b, g, r) for b in (0, 1) for g in (0, 1) for r in (0, 1)],
                         dtype=np.float32)
        lut = ctx.texture3d((2, 2, 2), 3, table, dtype="f4")
        lut.filter = (moderngl.LINEAR, moderngl.LINEAR)
        assert readback.supports_lut(lut)
        steep_data = np.zeros((3, 3, 3, 3), np.float32)
        steep_data[:, :, 1:, :] = 1
        steep = ctx.texture3d((3, 3, 3), 3, steep_data, dtype="f4")
        assert not readback.supports_lut(steep)
        y = np.full((8, 32), 97, dtype=np.uint8)
        u = np.full((4, 16), 80, dtype=np.uint8)
        v = np.full((4, 16), 190, dtype=np.uint8)
        u[::2, 8:] = 200
        v[1::2, :8] = 100
        first = readback.submit((y, u, v), lut, 1.0, 10, 0.4)
        second = readback.submit((np.full_like(y, 235), np.full_like(u, 128),
                                  np.full_like(v, 128)), lut, 1.0, 11, 0.44)
        graded = first.finish()
        white = second.finish()
        assert graded.frame_index == 10 and white.frame_index == 11
        # Same 2x2 source chroma replication as the packed 8-bit input.
        yf = (y.astype(float) - 16) / 219
        cb = (u.repeat(2, axis=0).repeat(2, axis=1).astype(float) - 128) / 224
        cr = (v.repeat(2, axis=0).repeat(2, axis=1).astype(float) - 128) / 224
        rgb = np.rint(np.clip(np.stack((yf + 1.8556 * cb,
                                       yf - 0.187324 * cb - 0.468124 * cr,
                                       yf + 1.5748 * cr), axis=-1), 0, 1) * 255) / 255
        reference_luma = rgb @ [0.2126, 0.7152, 0.0722]
        cb_dst = (rgb[..., 2] - reference_luma) / 1.8556
        cr_dst = (rgb[..., 0] - reference_luma) / 1.5748
        if pixel_format == "yuv420p":
            expected = (np.rint(16 + 219 * reference_luma),
                        np.rint(np.clip(128 + 224 * cb_dst.reshape(4, 2, 16, 2).mean((1, 3)), 16, 240)),
                        np.rint(np.clip(128 + 224 * cr_dst.reshape(4, 2, 16, 2).mean((1, 3)), 16, 240)))
            for plane, reference in zip(graded.planes, expected, strict=True):
                np.testing.assert_allclose(plane, reference, atol=1)
            for plane, code in zip(white.planes, (235, 128, 128), strict=True):
                np.testing.assert_array_equal(plane, code)
            buffer = graded.extra["yuv420p_buffer"]
            av_frame = av.VideoFrame.from_numpy_buffer(buffer, format=pixel_format)
            assert av_frame.planes[0].buffer_ptr == graded.planes[0].ctypes.data
            del graded, buffer
            assert readback._host.empty()
            del av_frame
            assert readback._host.qsize() == 1
            return
        expected = (np.rint(64 + 876 * reference_luma),
                    np.rint(np.clip(512 + 896 * cb_dst.reshape(8, 16, 2).mean(-1), 64, 960)),
                    np.rint(np.clip(512 + 896 * cr_dst.reshape(8, 16, 2).mean(-1), 64, 960)))
        for plane, reference in zip(graded.planes, expected, strict=True):
            np.testing.assert_allclose(plane, reference, atol=1)
        packed_source = av.VideoFrame(32, 8, "yuv420p")
        packed_source.color_range = packed_source.colorspace = 1
        for plane, values in zip(packed_source.planes, (y, u, v), strict=True):
            np.ndarray(values.shape, np.uint8, buffer=plane,
                       strides=(plane.line_size, 1))[:] = values
        # The old compositor feeds RGB8 to the LUT, then sends packed BGR16
        # to swscale for 422. Compare both paths at the saturated chroma edge.
        old_rgb = packed_source.reformat(format="rgb24").to_ndarray()
        old_bgr16 = old_rgb.astype(np.uint16) * 257
        packed = av.VideoFrame.from_ndarray(old_bgr16, format="bgr48le")
        old_output = packed.reformat(format="yuv422p10le", dst_colorspace="ITU709")
        for plane, before in zip(graded.planes, old_output.planes, strict=True):
            decoded = np.ndarray(plane.shape, np.uint16, buffer=before,
                                 strides=(before.line_size, 2))
            np.testing.assert_allclose(plane, decoded, atol=8)
        for plane, code in zip(white.planes, (940, 512, 512), strict=True):
            np.testing.assert_array_equal(plane, code)
        assert isinstance(graded.extra["yuv422p10_frame"], av.VideoFrame)
    finally:
        if readback is not None:
            readback.release()
        if lut is not None:
            lut.release()
        if steep is not None:
            steep.release()
        quad.release()
        ctx.release()


@pytest.mark.skipif(sys.platform != "win32", reason="WGL context ownership is Windows-specific")
def test_planar_lut_refuses_a_foreign_context_and_release_restores_it() -> None:
    moderngl = pytest.importorskip("moderngl")
    platform = pytest.importorskip("OpenGL.platform")
    from src.features.edit.core.timeline_gpu_export_renderer import TimelineGpuExportRenderer

    renderer = TimelineGpuExportRenderer()
    if not renderer._ensure_context():
        renderer.release()
        pytest.skip("OpenGL unavailable")
    try:
        other = moderngl.create_standalone_context(require=330)
    except Exception as error:
        renderer.release()
        pytest.skip(f"Second context unavailable: {error}")
    current = platform.PLATFORM.GetCurrentContext
    try:
        other_context = current()
        assert other_context and other_context != renderer._wgl_context_id
        with pytest.raises(RuntimeError, match="no longer current"):
            renderer.submit_planar_lut((), 32, 8, ("unused", 1.0), 0, 0.0, "yuv422p10le")
        with pytest.raises(RuntimeError, match="no longer current"):
            renderer.submit_topos_planar_lut(None, ("unused", 1.0), 32, 8, 0, 0.0)
        renderer.release()
        assert current() == other_context
    finally:
        renderer.release()
        other.release()
