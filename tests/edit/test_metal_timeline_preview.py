"""Compare Metal/IOSurface preview pixels with the existing Edit GL route."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pytest


def _compare_on_render_thread(
    path: Path, lut: tuple[str, float] | None = None, grade: dict[str, Any] | None = None,
) -> list[tuple[float, float]]:
    import moderngl
    from PyQt6.QtCore import QThread

    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
    from src.features.edit.core.metal_timeline_preview import MetalTimelinePreview
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.media.gpu_frame import safe_release_gpu_frame
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
    metal = source = gl_source = None
    parking_texture = context.texture((1, 1), 4)
    parking_fbo = context.framebuffer([parking_texture])
    parking_fbo.use()
    differences = []
    try:
        if not decoder.open(str(path), require_zero_copy=False, require_hardware=True):
            pytest.skip("VideoToolbox hardware decode is unavailable")
        source = decoder.read_frame_pixel_buffer(25)
        if source is None or source.pixel_format != "nv12":
            pytest.skip("VideoToolbox NV12 decode is unavailable")
        metal = MetalTimelinePreview(context)
        assert renderer._init_shaders()
        renderer._init_quad()
        gl_source = renderer._import_vt_pixel_buffer_frame(source)
        assert gl_source is not None
        assert context.error == "GL_NO_ERROR", "GL input import"
        planes = renderer._wrap_external_gpu_planes(gl_source.planes, gl_source)
        assert planes is not None
        for size in ((source.width, source.height), (832, 468)):
            width, height = size
            track_texture = context.texture(size, 4)
            track_fbo = context.framebuffer([track_texture])
            final_texture = context.texture(size, 4)
            final_fbo = context.framebuffer([final_texture])
            renderer._composite_fbo, renderer._composite_tex = final_fbo, final_texture
            assert context.error == "GL_NO_ERROR", f"{size} framebuffers initialized"
            output = metal.render(source, size, lut=lut)
            assert output is not None
            slot, lease = output
            try:
                metal.blit(slot, final_fbo)
                assert context.error == "GL_NO_ERROR", f"{size} Metal blit"
                metal_rgb = np.frombuffer(final_fbo.read(components=3), np.uint8).astype(np.int16)
                assert context.error == "GL_NO_ERROR", f"{size} Metal readback"
                renderer._render_yuv_to_track_fbo(planes, gl_source, track_fbo, track_texture)
                graded = renderer._apply_preview_grade(track_texture, 0, width, height, grade)
                renderer._composite_tracks([(graded, 1.0, 0)], width, height)
                gl_rgb = np.frombuffer(final_fbo.read(components=3), np.uint8).astype(np.int16)
                assert context.error == "GL_NO_ERROR", f"{size} GL reference"
                error = np.abs(metal_rgb - gl_rgb)
                differences.append((float(np.mean(error)), float(np.quantile(error, 0.99))))
            finally:
                context.finish()
                lease.release()
                # Keep ModernGL's current FBO valid across the size change.
                parking_fbo.use()
                final_fbo.release()
                final_texture.release()
                track_fbo.release()
                track_texture.release()
            assert context.error == "GL_NO_ERROR", f"{size} framebuffer cleanup"
    finally:
        if gl_source is not None:
            safe_release_gpu_frame(gl_source)
        elif source is not None:
            source.release()
        if metal is not None:
            metal.release()
        if renderer._vt_import_bridge is not None:
            renderer._vt_import_bridge.destroy()
        graph_renderer = getattr(renderer, "_color_graph_texture_renderer", None)
        if graph_renderer is not None:
            graph_renderer.release()
        for resource in (*renderer._graded_track_fbos, *renderer._graded_track_texes):
            resource.release()
        decoder.close()
        parking_fbo.release()
        parking_texture.release()
        context.release()
    return differences


@pytest.mark.gpu_renderer
@pytest.mark.real_media
@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Metal and CGL interop")
@pytest.mark.parametrize("with_lut", [False, True], ids=["ungraded", "single-lut"])
def test_nv12_metal_preview_matches_gl_at_native_and_proxy_sizes(
    with_lut: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not path.is_file():
        pytest.skip("H.264 test clip is unavailable")
    lut = grade = None
    if with_lut:
        from src.features.color.core.color_graph_service import ColorGraphData, ColorGraphService
        from src.features.color.core.color_graph_texture import serial_lut_only

        cube = tmp_path / "preview.cube"
        cube.write_text("LUT_3D_SIZE 2\n" + "".join(
            f"{0.1 + 0.7 * b} {0.05 + 0.8 * r} {0.2 + 0.75 * g}\n"
            for b in range(2) for g in range(2) for r in range(2)
        ), encoding="utf-8")
        reference = "topos-res://lut_" + "0" * 32
        monkeypatch.setattr("src.shared.color.lut_loader.resolve_lut_path", lambda ref: cube)
        monkeypatch.setattr("src.features.color.core.color_graph_texture.resolve_lut_path", lambda ref: cube)
        service = ColorGraphService(lut_reference_validator=lambda ref: ref == reference)
        inserted = service.apply_intent(ColorGraphData.create_serial(), "insert_corrector", {})
        assert inserted.ok
        node = next(n for n in inserted.graph.nodes if n.node_type == "corrector")
        result = service.apply_intent(inserted.graph, "set_node_lut", {
            "node_id": node.node_id, "lut_ref": reference, "strength": 0.73,
        })
        assert result.ok
        lut = serial_lut_only(result.graph)
        assert lut == (reference, 0.73)
        grade = {"color_graph": result.graph, "source_frame": 25}

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="MetalPreviewTest") as worker:
        differences = worker.submit(_compare_on_render_thread, path, lut, grade).result()
    assert all(mean < 0.8 and p99 <= 2 for mean, p99 in differences), differences
