"""The Metal playback renderer shows what the GL playback worker shows (plan 7.1).

The shared players (media pool, file page, shared player) hand ``YuvFrame``
planes to ``gl_render_worker``, which uploads them with ``YUVGPUUploader``
and converts them in a letterboxed viewport.  ``PlaybackYuvRenderer`` is the
Metal counterpart.  Each case here draws one frame both ways on the render
thread of a standalone GL context and compares the pictures:

* 8-bit planes and every layout go through the GL path as it is; the two
  pictures may differ by rounding only.
* 10/12/16-bit planes at 1:1 likewise.
* Scaled 10/12-bit planes interpolate at the output pixel centres, compared
  with a separate CPU reference for both renderers.
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PyQt6.QtWidgets")


def _planes(rng: np.random.Generator, w: int, h: int, sub: str, depth: int) -> tuple:
    """Legal-range studio noise: every pixel different, so a shifted sample shows."""
    cw, ch = {"420": (w // 2, h // 2), "422": (w // 2, h), "444": (w, h)}[sub]
    shift = depth - 8
    if depth == 8:
        y = rng.integers(16, 236, (h, w), np.uint8)
        u = rng.integers(16, 241, (ch, cw), np.uint8)
        v = rng.integers(16, 241, (ch, cw), np.uint8)
        return y, u, v
    y = rng.integers(16 << shift, (235 << shift) + 1, (h, w), np.uint16)
    u = rng.integers(16 << shift, (240 << shift) + 1, (ch, cw), np.uint16)
    v = rng.integers(16 << shift, (240 << shift) + 1, (ch, cw), np.uint16)
    return y, u, v


def _linear_reference(frame: Any, out: tuple[int, int]) -> np.ndarray:
    """Bilinear YUV -> RGB at the centres of an output-sized picture."""
    from src.shared.playback.metal_playback import chroma_sampling, range_constants, uploader_matrix

    ow, oh = out
    ux = (np.arange(ow) + 0.5) / ow
    uy = (np.arange(oh) + 0.5) / oh
    scale, offset = chroma_sampling(frame)

    def sample(plane: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        h, w = plane.shape
        x, y = x * w - 0.5, y * h - 0.5
        ix, iy = np.floor(x).astype(int), np.floor(y).astype(int)
        fx, fy = (x - ix)[None, :], (y - iy)[:, None]
        x0, x1 = ix.clip(0, w - 1), (ix + 1).clip(0, w - 1)
        y0, y1 = iy.clip(0, h - 1), (iy + 1).clip(0, h - 1)
        a = plane[np.ix_(y0, x0)] * (1 - fx) + plane[np.ix_(y0, x1)] * fx
        b = plane[np.ix_(y1, x0)] * (1 - fx) + plane[np.ix_(y1, x1)] * fx
        return a * (1 - fy) + b * fy

    max_val, y_black, y_range_inv, c_neutral, c_range_inv = range_constants(frame.bit_depth)
    cx, cy = ux * scale[0] + offset[0], uy * scale[1] + offset[1]
    y = sample(frame.y, ux, uy) / max_val
    u = sample(frame.u, cx, cy) / max_val
    v = sample(frame.v, cx, cy) / max_val
    if frame.color_range:
        u, v = u - 0.5, v - 0.5
    else:
        y = (y - y_black) * y_range_inv
        u, v = (u - c_neutral) * c_range_inv, (v - c_neutral) * c_range_inv
    cr_r, cb_g, cr_g, cb_b = {0: (1.5748, -0.1873, -0.4681, 1.8556), 1: (1.4746, -0.1646, -0.5714, 1.8814),
                              2: (1.4020, -0.3441, -0.7141, 1.7720)}[uploader_matrix(frame.matrix)]
    rgb = np.stack([y + cr_r * v, y + cb_g * u + cr_g * v, y + cb_b * u], axis=-1)
    return np.clip(np.round(rgb * 255.0), 0, 255)


def _frame(rng, w, h, sub, depth, **fields):
    from src.shared.playback.yuv_frame import YuvFrame

    y, u, v = _planes(rng, w, h, sub, depth)
    base = dict(bit_depth=depth, subsampling=sub, matrix=1, color_range=0, chroma_siting=1,
                pixel_format=sub, source_width=w, source_height=h)
    base.update(fields)
    return YuvFrame(y, u, v, **base)


class _Both:
    """The GL path of the playback worker and the Metal renderer, on one thread."""

    def __init__(self) -> None:
        import moderngl

        from src.shared.gpu.metal.context import MetalComputeContext
        from src.shared.playback.metal_yuv_renderer import PlaybackYuvRenderer
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader

        if not MetalComputeContext.is_supported():
            pytest.skip(f"no Metal device: {MetalComputeContext.availability_reason()[1]}")
        self.gl = moderngl.create_standalone_context(require=410)
        self.metal = MetalComputeContext()
        self.renderer = PlaybackYuvRenderer(self.metal, pool_size=3, upload_slots=2)
        self.uploader = YUVGPUUploader(self.gl, pool_size=4, dtype="f2")
        self.index = 0

    def gl_picture(self, frame, out) -> np.ndarray:
        """What ``gl_render_worker._render_frame`` leaves in its framebuffer."""
        from src.shared.playback.gl_video_canvas import _flip_rows, _h273_matrix_to_uploader
        from src.shared.playback.metal_playback import chroma_sampling
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader

        width, height = out
        texture = self.gl.texture(out, 4)
        fbo = self.gl.framebuffer([texture])
        fbo.use()
        self.gl.viewport = (0, 0, width, height)
        fbo.clear(0.0, 0.0, 0.0, 1.0)
        scale = min(width / frame.width, height / frame.height)
        view_w, view_h = max(1, int(frame.width * scale)), max(1, int(frame.height * scale))
        fbo.viewport = ((width - view_w) // 2, (height - view_h) // 2, view_w, view_h)
        self.index += 1
        planes = self.uploader.upload_yuv_planar(
            _flip_rows(frame.y), _flip_rows(frame.u), _flip_rows(frame.v),
            frame_index=self.index, bit_depth=frame.bit_depth, subsampling=frame.subsampling)
        chroma_scale, chroma_offset = chroma_sampling(frame)
        self.uploader.set_yuv_matrix(_h273_matrix_to_uploader(frame.matrix))
        self.uploader.set_color_range(
            YUVGPUUploader.RANGE_FULL if frame.color_range else YUVGPUUploader.RANGE_LIMITED)
        self.uploader.render_yuv_to_rgb(
            *planes[:3], fbo, chroma_uv_scale=chroma_scale, chroma_uv_offset=chroma_offset,
            bit_depth=frame.bit_depth, gbr_mode="gbr" in str(frame.pixel_format),
            linear_sampling=(view_w, view_h) != (frame.width, frame.height))
        top_down = np.frombuffer(fbo.read(components=3), np.uint8).reshape(height, width, 3)[::-1]
        picture = top_down.copy()
        texture.release()
        fbo.release()
        return picture

    def metal_picture(self, frame, out) -> np.ndarray:
        self.renderer.ensure_size(out)
        lease = self.renderer.acquire_slot(generation_id=1, frame_index=self.index)
        assert lease is not None
        free_uploads = self.renderer.encode(frame, lease.resource.native_handle, out)
        self.metal.submit(wait=True)               # the frame transaction's completion
        free_uploads()
        assert self.renderer.uploads_busy == 0
        picture = self.renderer.read_slot(lease.slot_id).copy()
        lease.mark_ready(0)
        present = self.renderer.make_present(lease, frame=self.index, generation_id=1, frame_meta=frame)
        self.renderer.ack(present, "compared")
        return picture

    def close(self) -> None:
        self.metal.finish()
        self.renderer.release_all()
        self.metal.finish()
        self.gl.release()


def _compare(cases: list) -> list:
    """Run on a worker thread: GL and Metal contexts belong to one thread."""
    both = _Both()
    rng = np.random.default_rng(3)
    report = []
    try:
        for label, make, out, against in cases:
            frame = make(rng)
            got = both.metal_picture(frame, out)
            gl = both.gl_picture(frame, out).astype(np.int16)
            expected = gl if against == "gl" else _linear_reference(frame, out).astype(np.int16)
            difference = np.abs(got.astype(np.int16) - expected)
            if against != "gl":
                difference = np.maximum(difference, np.abs(gl - expected))
            report.append((label, int(difference.max()), float(difference.mean())))
    finally:
        both.close()
    return report


W, H = 64, 36


def _case(label, sub="420", depth=8, out=(W, H), against="gl", **fields):
    size = (W, H) if out in ((W, H),) or out == "1:1" else out
    return (label, lambda rng: _frame(rng, W, H, sub, depth, **fields), size, against)


VS_GL = [
    _case("8-bit 4:2:0 1:1"),
    _case("8-bit 4:2:0 -> 96x54", out=(96, 54)),
    _case("8-bit 4:2:0 -> 43x24", out=(43, 24)),
    _case("8-bit 4:2:0 -> 32x18", out=(32, 18)),
    _case("8-bit 4:2:0 full range", color_range=1),
    _case("8-bit 4:2:0 BT.601", matrix=5),
    _case("8-bit 4:2:0 BT.2020", matrix=9),
    _case("8-bit 4:2:2 siting 0", sub="422", chroma_siting=0),
    _case("8-bit 4:2:2 siting 2 -> 96x54", sub="422", chroma_siting=2, out=(96, 54)),
    _case("8-bit 4:4:4", sub="444"),
    _case("8-bit GBR 4:4:4 full range", sub="444", pixel_format="gbrp", color_range=1),
    _case("letterbox 64x36 in 80x36", out=(80, 36)),
    _case("letterbox 64x36 in 64x48", out=(64, 48)),
    _case("10-bit 4:2:2 siting 1 1:1", sub="422", depth=10),
    _case("10-bit 4:2:2 siting 0 1:1", sub="422", depth=10, chroma_siting=0),
    _case("10-bit 4:2:2 siting 2 1:1", sub="422", depth=10, chroma_siting=2),
    _case("12-bit 4:4:4 BT.2020 1:1", sub="444", depth=12, matrix=9),
    _case("16-bit 4:2:2 full range 1:1", sub="422", depth=16, color_range=1),
]

VS_REFERENCE = [
    _case("10-bit 4:2:2 -> 96x54", sub="422", depth=10, out=(96, 54), against="reference"),
    _case("10-bit 4:2:2 -> 32x18", sub="422", depth=10, out=(32, 18), against="reference"),
    _case("10-bit 4:2:0 -> 96x54", sub="420", depth=10, out=(96, 54), against="reference"),
    _case("12-bit 4:4:4 BT.2020 -> 96x54", sub="444", depth=12, matrix=9, out=(96, 54), against="reference"),
]


def _needs_gl() -> None:
    try:
        import moderngl  # noqa: F401
    except ImportError as error:      # pragma: no cover
        pytest.skip(f"moderngl is unavailable: {error}")


@pytest.mark.gpu_renderer
@pytest.mark.skipif(sys.platform != "darwin", reason="needs a Metal device next to a CGL context")
def test_the_metal_picture_is_the_gl_picture() -> None:
    _needs_gl()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="PlaybackParity") as worker:
        report = worker.submit(_compare, VS_GL).result()
    worst = max(report, key=lambda item: item[1])
    for label, worst_level, mean in report:
        assert worst_level <= 2, (label, worst_level, mean)
    assert worst[1] >= 1                     # the comparison is not vacuous


@pytest.mark.gpu_renderer
@pytest.mark.skipif(sys.platform != "darwin", reason="needs a Metal device next to a CGL context")
def test_scaled_high_bit_depth_planes_sample_at_the_pixel_centres() -> None:
    _needs_gl()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="PlaybackParity") as worker:
        report = worker.submit(_compare, VS_REFERENCE).result()
    for label, worst_level, mean in report:
        # Integer presentation only rounds when writing the 8-bit drawable.
        assert worst_level <= 1 and mean < 0.10, (label, worst_level, mean)


# ---------------------------------------------------------------------------
# Slots and uploads, on the device
# ---------------------------------------------------------------------------


def _lifecycle() -> dict:
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.playback.metal_yuv_renderer import BackPressure, PlaybackYuvRenderer
    from src.shared.gpu.present_frame import SlotState

    metal = MetalComputeContext()
    renderer = PlaybackYuvRenderer(metal, pool_size=2, upload_slots=1)
    rng = np.random.default_rng(5)
    facts: dict = {}
    try:
        frame = _frame(rng, W, H, "420", 8)
        renderer.ensure_size((W, H))
        first = renderer.acquire_slot(generation_id=7, frame_index=1)
        second = renderer.acquire_slot(generation_id=7, frame_index=2)
        assert first is not None and second is not None
        assert renderer.acquire_slot(generation_id=7, frame_index=3) is None    # the viewport holds both
        free = renderer.encode(frame, first.resource.native_handle, (W, H))
        assert renderer.uploads_busy == 1
        try:
            renderer.encode(frame, second.resource.native_handle, (W, H))
            facts["back_pressure"] = False
        except BackPressure:
            facts["back_pressure"] = True
        metal.submit(wait=True)
        free()
        assert renderer.uploads_busy == 0
        facts["first_state_after_gpu"] = first.state.name
        first.mark_ready(0)
        present = renderer.make_present(first, frame=1, generation_id=7, frame_meta=frame)
        descriptor = present.native
        facts["present"] = dict(
            is_native=present.is_native, api=present.api, width=present.width, height=present.height,
            resource_is_slot=descriptor.resource is first.resource.native_handle,
            resource_format=descriptor.resource_format, origin=descriptor.origin,
            color_range=descriptor.color.color_range, color_matrix=descriptor.color.color_matrix,
            slot_id=present.slot_id, frame=present.frame, generation=present.generation_id)
        assert first.state is SlotState.READY             # shown to the viewer, not yet taken by it
        renderer.pool.mark_in_viewer(present.slot_id, present.lease_key)
        assert first.state is SlotState.IN_VIEWER
        renderer.ack(present, "replaced")
        facts["slot_after_ack"] = renderer.slot_state(present.slot_id)
        facts["stale_acks"] = renderer.pool.stats().get("stale_acks")
        renderer.ack(present, "again")                                  # a duplicate ack: harmless
        assert renderer.slot_state(present.slot_id) == facts["slot_after_ack"]
        assert renderer.pool.stats()["reuse_violations"] == 0
        # Another size retires the old pool; its free textures go once nothing is out.
        renderer.ensure_size((96, 54))
        facts["retired_after_resize"] = renderer.stats_snapshot().get("retired_pools")
        second.abort_writing()
        renderer.drain_retired()
        facts["retired_after_drain"] = renderer.stats_snapshot().get("retired_pools")
        facts["states"] = renderer.pool.stats()["states"]
    finally:
        metal.finish()
        renderer.release_all()
        metal.finish()
    return facts


@pytest.mark.gpu_renderer
@pytest.mark.skipif(sys.platform != "darwin", reason="needs a Metal device")
def test_slots_uploads_and_retired_pools() -> None:
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="PlaybackParity") as worker:
        facts = worker.submit(_lifecycle).result()
    assert facts["back_pressure"] is True                     # one upload set, still being read
    assert facts["present"] == dict(
        is_native=True, api="metal", width=W, height=H, resource_is_slot=True,
        # The slot already holds display RGB: full range, the source's matrix.
        resource_format="bgra8unorm", origin="top_left", color_range="full",
        color_matrix="bt709", slot_id=0, frame=1, generation=7)
    assert facts["slot_after_ack"] in {"RELEASABLE", "FREE"}
    assert facts["stale_acks"] in (0, None)
    assert facts["retired_after_resize"] == 1 and facts["retired_after_drain"] == 0
    # Every slot back to reusable: nothing is still being written or waiting to be shown.
    assert set(facts["states"]) <= {"RELEASABLE", "FREE"}
