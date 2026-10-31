from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.gpu.metal.context import MetalComputeContext


@pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="requires an available Metal device"
)
def test_videotoolbox_surface_provider_allocates_iosurface_planes() -> None:
    from src.shared.media.macos_vt.gpu_encode_surface import (
        VideoToolboxGpuSurfaceProvider,
    )

    submitted = []
    context = MetalComputeContext()
    provider = VideoToolboxGpuSurfaceProvider(
        context, submitter=lambda surface: submitted.append(surface.native_handle)
    )
    surface = provider.acquire(width=64, height=64, frame_index=7, pts=11)
    try:
        assert surface.descriptor.backend == "videotoolbox"
        assert surface.descriptor.pixel_format == "nv12"
        assert surface.descriptor.native_handle is not None
        assert {plane.component for plane in surface.descriptor.planes} == {"Y", "UV"}
        assert surface.validate(
            backend="videotoolbox", device_identity=provider.device_identity
        ) == ""
        surface.submit()
        assert submitted == [surface.native_handle]
    finally:
        surface.release()
        provider.close()


@pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="requires an available Metal device"
)
def test_nv12_surface_render_has_no_cpu_download() -> None:
    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer,
    )
    from src.shared.export.gpu_trail import ExportGpuTrail
    from src.shared.media.gpu_frame import safe_release_gpu_frame
    from src.shared.media.macos_vt.gpu_encode_surface import (
        VideoToolboxGpuSurfaceProvider,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    source_path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not source_path.is_file():
        pytest.skip("real H.264 test material is unavailable")

    frame_provider = VideoFrameProviderService()
    clip_id = frame_provider.open_clip(str(source_path), share_by_path=False)
    frame = frame_provider.request_metal_frame(clip_id, 0, quality="master")
    if frame is None or getattr(frame, "pixel_format", "") != "nv12":
        frame_provider.close_clip(clip_id)
        pytest.skip("VideoToolbox NV12 Metal input is unavailable")

    context = MetalComputeContext()
    surface_provider = VideoToolboxGpuSurfaceProvider(context)
    surface = surface_provider.acquire(width=64, height=64)
    trail = ExportGpuTrail("vt-surface-test")
    renderer = TimelineMetalExportRenderer(trail)
    try:
        result = renderer.render_metal_texture_frame(
            frame,
            None,
            64,
            64,
            1.0,
            output_yuv420p=True,
            output_surface=surface,
        )
        assert result is surface, renderer.last_fallback_reason
        assert renderer.last_fallback_reason is None
        assert trail.snapshot().stage("yuv_conversion").cpu_download_bytes == 0
    finally:
        surface.release()
        renderer.release()
        surface_provider.close()
        safe_release_gpu_frame(frame)
        frame_provider.close_clip(clip_id)


@pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="requires an available Metal device"
)
def test_p010_surface_render_has_no_cpu_download() -> None:
    """Prove a real VT P010 frame is written to a P010 IOSurface on Metal."""
    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer,
    )
    from src.shared.export.gpu_trail import ExportGpuTrail
    from src.shared.media.gpu_frame import safe_release_gpu_frame
    from src.shared.media.macos_vt.gpu_encode_surface import (
        VideoToolboxGpuSurfaceProvider,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    source_path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_H265.mov"
    if not source_path.is_file():
        pytest.skip("real HEVC/P010 test material is unavailable")

    frame_provider = VideoFrameProviderService()
    clip_id = frame_provider.open_clip(str(source_path), share_by_path=False)
    frame = frame_provider.request_metal_frame(clip_id, 0, quality="master")
    if frame is None or getattr(frame, "pixel_format", "") != "p010":
        frame_provider.close_clip(clip_id)
        pytest.skip("VideoToolbox P010 Metal input is unavailable")

    context = MetalComputeContext()
    surface_provider = VideoToolboxGpuSurfaceProvider(context)
    surface = surface_provider.acquire(
        width=64, height=64, pixel_format="p010", frame_index=0, pts=0,
    )
    trail = ExportGpuTrail("vt-p010-surface-test")
    renderer = TimelineMetalExportRenderer(trail)
    try:
        result = renderer.render_metal_texture_frame(
            frame,
            None,
            64,
            64,
            1.0,
            output_yuv420p=True,
            output_surface=surface,
        )
        assert result is surface, renderer.last_fallback_reason
        assert renderer.last_fallback_reason is None
        assert trail.snapshot().stage("yuv_conversion").cpu_download_bytes == 0
    finally:
        surface.release()
        renderer.release()
        surface_provider.close()
        safe_release_gpu_frame(frame)
        frame_provider.close_clip(clip_id)
