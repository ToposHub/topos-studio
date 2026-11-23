"""End to end: a real video frame through the native preview path (plan 2.2).

The existing Metal preview writes a frame into an IOSurface, which the GL
path then blits into a present slot; ``test_metal_timeline_preview.py`` shows
that picture matches the GL composite.  The native path writes the same
kernel into a Metal present-slot texture through a frame transaction.  This
test decodes one frame twice, runs it through both routes on the render
thread and compares the two textures pixel by pixel, so the native slot holds
what the GL viewer would have shown.  Under the Cocoa platform the published
frame is then pushed through the real present port into a real CAMetalLayer.
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


def _compare_on_render_thread(path: Path, layer: Any, publisher: Any) -> dict:
    import moderngl
    from PyQt6.QtCore import QThread

    from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask, EditGpuPreviewRenderer
    from src.features.edit.core.metal_timeline_preview import MetalTimelinePreview
    from src.features.edit.core.native_metal_preview import SUBMITTED
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.gpu.metal.present_port import normalized_rect
    from src.shared.gpu.present_frame import SlotState
    from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

    if not MetalComputeContext.is_supported():
        pytest.skip("Metal is unavailable")
    try:
        context = moderngl.create_standalone_context(require=410)
    except (RuntimeError, OSError) as error:
        pytest.skip(f"A CGL render context is unavailable: {error}")
    decoder = MacOSVtZeroCopySource()
    renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
    QThread.__init__(renderer)
    renderer.ctx = context
    renderer._vt_import_bridge = None
    renderer._active_transform_coordinate_size = (1920, 1080)
    renderer._graded_track_texes = []
    renderer._graded_track_fbos = []
    renderer._ensure_native_viewport_state()
    renderer._metal_preview_mode = "all"
    renderer._native_viewport_opt_in = True
    refusals: list = []
    renderer.native_viewport_failed.connect(refusals.append)
    renderer._task_used_io_surface_source = False
    # Telemetry and error reporting are not under test here.
    renderer._note_zero_copy_import = lambda *args, **kwargs: None
    errors: list = []
    renderer._emit_task_render_error = lambda task, message: errors.append((task.frame, message))
    published: list = []
    presented: list = []
    renderer.present_frame_ready.connect(published.append)
    renderer.native_frame_presented.connect(presented.append)

    metal = source_a = source_b = None
    result: dict = {}
    try:
        if not decoder.open(str(path), require_zero_copy=False, require_hardware=True):
            pytest.skip("VideoToolbox hardware decode is unavailable")
        source_a = decoder.read_frame_pixel_buffer(25)
        source_b = decoder.read_frame_pixel_buffer(25)
        if source_a is None or source_b is None or source_a.pixel_format != "nv12":
            pytest.skip("VideoToolbox NV12 decode is unavailable")
        # Count how often the real frame's idempotent release is invoked.
        vt_releases: list = []
        real_release = source_b.release
        source_b.release = lambda: (vt_releases.append(1), real_release())  # type: ignore[method-assign]
        metal = MetalTimelinePreview(context)
        renderer._metal_preview = metal          # as the render thread would hold it
        size = (source_a.width, source_a.height)
        metal_context = metal.metal_context

        # Reference: the IOSurface route the GL viewer shows today.
        output = metal.render(source_a, size)
        assert output is not None
        surface, lease = output
        reference = metal_context.read_texture(
            surface.metal_texture, size[0] * 4, size[1], np.uint8, region_width=size[0],
        ).reshape(size[1], size[0], 4).copy()
        lease.release()

        if layer is not None:
            renderer._apply_native_viewport_request(("attach", layer, publisher))
            assert renderer._native_port is not None, f"native viewport refused: {refusals}"
        else:
            from src.features.edit.core.native_metal_preview import NativePreviewProducer
            renderer._native_producer = NativePreviewProducer(metal)

        producer = renderer._native_producer
        task = EditRenderTask(
            frame=25, generation_id=1,
            layers=[{"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": source_b,
                     "track_info": {}}],
            output_fbo_size=size,
        )
        assert renderer._try_native_metal_preview(task, size[0], size[1]) is True
        assert task.layers[0]["vt_pixel_buffer"] is None
        assert published == []                    # nothing is published before the GPU is done

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not published:
            renderer._poll_native_viewport()
            time.sleep(0.002)
        (present,) = published
        assert errors == []
        assert present.is_native and present.frame == 25
        assert (present.width, present.height) == size
        slot_texture = present.native.resource
        metal_context.finish()
        native = metal_context.read_texture(
            slot_texture, size[0] * 4, size[1], np.uint8, region_width=size[0],
        ).reshape(size[1], size[0], 4).copy()
        difference = np.abs(native.astype(np.int16) - reference.astype(np.int16))
        result["mae"] = float(np.mean(difference))
        result["p99"] = float(np.quantile(difference, 0.99))
        result["max"] = int(difference.max())

        # The VT frame and its import were returned exactly once.
        assert len(vt_releases) == 1
        # The slot stays with the viewport until it is done with the frame.
        lease_ = producer._pool.get_slot(present.slot_id)
        assert lease_.state is SlotState.READY

        if layer is not None:
            result["port"] = _exercise_port(renderer, present, size)
        else:
            producer.ack(present, "replaced")
            result["port"] = None
            assert producer.shutdown() is True
    finally:
        if source_a is not None:
            source_a.release()
        if layer is not None:
            done = threading.Event()
            renderer._apply_native_viewport_request(("detach", done))
            assert done.is_set()
        if metal is not None:
            metal.release()
        if renderer._vt_import_bridge is not None:
            renderer._vt_import_bridge.destroy()
        decoder.close()
        context.release()
    return result


def _exercise_port(renderer: Any, present: Any, size: tuple) -> dict:
    """Push the published frame through the real port; report what happened."""
    stats: dict = {}
    acked: list = []

    def on_released(frame, reason):
        # The render thread frees the slot itself (producer.ack); the
        # viewer-side callback only learns that the frame was let go.
        acked.append(reason)

    assert renderer.submit_native_present(
        present, rect=(0.0, 0.0, 1.0, 1.0),
        display_rect_px=(0, 0, size[0], size[1]), on_released=on_released,
    ) is True
    port = renderer._native_port
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and port.in_flight == 0 and port.pending:
        renderer._poll_native_viewport()
        time.sleep(0.002)
    renderer._poll_native_viewport()
    stats["presented"] = port.stats["presented"]
    stats["no_drawable"] = port.stats["no_drawable"]
    stats["stale"] = port.stats["stale"]
    stats["in_flight"] = port.in_flight
    stats["released"] = list(acked)
    return stats


def _needs_video() -> Path:
    path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not path.is_file():
        pytest.skip("H.264 test clip is unavailable")
    return path


@pytest.mark.gpu_renderer
@pytest.mark.real_media
@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Metal and CGL interop")
def test_native_slot_matches_the_iosurface_route(tmp_path: Path) -> None:
    path = _needs_video()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="NativePreviewTest") as worker:
        result = worker.submit(_compare_on_render_thread, path, None, None).result()
    assert result["mae"] < 0.5 and result["p99"] <= 2, result


@pytest.mark.gpu_renderer
@pytest.mark.real_media
@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Metal and CGL interop")
def test_native_frame_reaches_a_real_layer(qapp) -> None:
    path = _needs_video()
    if qapp.platformName() != "cocoa":
        pytest.skip("a CAMetalLayer needs the cocoa Qt platform")
    from src.shared.ui.widgets.metal_viewport_host import MetalViewportHost

    host = MetalViewportHost()
    host.resize(640, 360)
    host.show()
    qapp.processEvents()
    try:
        if not host.attach():
            pytest.skip(f"no Metal layer on this machine: {host.failure}")
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="NativePreviewTest") as worker:
            result = worker.submit(
                _compare_on_render_thread, path, host.layer, host.publisher).result()
        assert result["mae"] < 0.5 and result["p99"] <= 2, result
        port = result["port"]
        # A drawable is only handed out while the layer is on screen; with the
        # display asleep the port keeps the last picture and reports it.
        assert port["stale"] == 0, port
        assert port["presented"] + port["no_drawable"] >= 1, port
    finally:
        host.detach()
        host.close()
        qapp.processEvents()
