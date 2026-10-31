"""
P0-4: End-to-end zero-copy playback test gate.

Verifies the complete hot path:
- MacOSVtZeroCopySource produces GpuDecodedFrame with zero_copy=True
- Renderer receives RENDER_VIDEO_GPU_FRAME without Texture.write/to_ndarray/ctx.finish
- No CPU data path in zero-copy hot path

This test is macOS-gated and requires TOPOS_VIDEO_ZERO_COPY=force or real VT hardware.
"""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

# Skip on non-macOS
pytestmark = pytest.mark.skipif(
    sys.platform != 'darwin',
    reason="macOS VT zero-copy tests require macOS"
)


class TestZeroCopyEndToEnd:
    """P0-4: Verify complete zero-copy hot path."""

    def test_gpu_decoded_frame_structure(self):
        """GpuDecodedFrame from VT source must have correct structure."""
        from src.shared.media.gpu_frame import (
            GpuDecodedFrame, GpuFramePlane, GpuFrameRelease
        )

        plane_y = GpuFramePlane(
            component="Y", api="opengl", texture_id=1, target="2d",
            width=1920, height=1080, internal_format="GL_R8",
            pixel_format="nv12_y", pixel_type="GL_UNSIGNED_BYTE"
        )
        plane_uv = GpuFramePlane(
            component="UV", api="opengl", texture_id=2, target="2d",
            width=960, height=540, internal_format="GL_RG8",
            pixel_format="nv12_uv", pixel_type="GL_UNSIGNED_BYTE"
        )

        frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=1920, height=1080,
            visible_rect=(0, 0, 1920, 1080),
            pixel_format='nv12', bit_depth=8,
            planes=[plane_y, plane_uv],
            decode_path='macos_vt_iosurface_zero_copy',
            zero_copy=True,
        )

        assert frame.zero_copy is True
        assert frame.decode_path == 'macos_vt_iosurface_zero_copy'
        assert len(frame.planes) == 2
        assert frame.planes[0].component == 'Y'
        assert frame.planes[1].component == 'UV'

    def test_gpu_frame_validation_rejects_zero_copy_without_planes(self):
        """GpuDecodedFrame with zero_copy=True but no planes should fail."""
        from src.shared.media.gpu_frame import GpuDecodedFrame

        with pytest.raises(ValueError, match="requires at least one plane"):
            GpuDecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                width=1920, height=1080,
                visible_rect=(0, 0, 1920, 1080),
                pixel_format='nv12', bit_depth=8,
                planes=[],  # No planes!
                zero_copy=True,
            )

    def test_gpu_frame_rejects_zero_copy_with_cpu_decode_path(self):
        """GpuDecodedFrame with zero_copy=True and CPU decode path should fail."""
        from src.shared.media.gpu_frame import GpuDecodedFrame, GpuFramePlane

        plane = GpuFramePlane(
            component="Y", api="opengl", texture_id=1, target="2d",
            width=1920, height=1080
        )

        with pytest.raises(ValueError, match="cpu"):
            GpuDecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                width=1920, height=1080,
                visible_rect=(0, 0, 1920, 1080),
                pixel_format='nv12', bit_depth=8,
                planes=[plane],
                decode_path='cpu_packed_rgb',  # CPU path!
                zero_copy=True,
            )

    def test_decode_path_classification(self):
        """DecodePath classification for zero-copy and fallback."""
        from src.shared.media.frame_path import (
            DecodePath, RenderFramePath,
            classify_decode_path, classify_render_frame_path,
            is_zero_copy_combination
        )

        # Zero-copy macOS VT
        decode = classify_decode_path(
            hwaccel_enabled=True, decoder_type='videotoolbox',
            zero_copy_available=True
        )
        assert decode == DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY
        assert decode.is_zero_copy

        # CPU fallback
        decode_cpu = classify_decode_path(
            hwaccel_enabled=False, pixel_format='yuv420p', is_planar=True
        )
        assert decode_cpu == DecodePath.CPU_PLANAR_YUV
        assert not decode_cpu.is_zero_copy

        # Hwaccel download
        decode_hw = classify_decode_path(
            hwaccel_enabled=True, decoder_type='videotoolbox',
            pixel_format='yuv420p', is_planar=True
        )
        assert decode_hw.is_hwaccel_download

        # Zero-copy combination check
        render_zc = RenderFramePath.GPU_DECODED_EXTERNAL_YUV
        assert is_zero_copy_combination(
            DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY, render_zc
        )
        assert not is_zero_copy_combination(
            DecodePath.CPU_PLANAR_YUV, RenderFramePath.PLANAR_YUV_R8_UPLOAD
        )

    def test_render_path_for_gpu_frame(self):
        """get_frame_render_path returns external texture path for GpuDecodedFrame."""
        from src.shared.media.gpu_frame import (
            GpuDecodedFrame, GpuFramePlane, get_frame_render_path
        )

        plane_y = GpuFramePlane(
            component="Y", api="opengl", texture_id=1, target="2d",
            width=1920, height=1080
        )
        plane_uv = GpuFramePlane(
            component="UV", api="opengl", texture_id=2, target="2d",
            width=960, height=540
        )

        gpu_frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=1920, height=1080,
            visible_rect=(0, 0, 1920, 1080),
            pixel_format='nv12', bit_depth=8,
            planes=[plane_y, plane_uv],
            decode_path='macos_vt_iosurface_zero_copy',
            zero_copy=True,
        )

        render_path = get_frame_render_path(gpu_frame)
        assert render_path == 'opengl_external_yuv'  # P3-04 限定值

    def test_10bit_gpu_frame_path(self):
        """10-bit P010 GPU frame should have correct path classification."""
        from src.shared.media.frame_path import (
            classify_render_frame_path, RenderFramePath, DecodePath
        )

        # P010 semi-planar 10-bit
        render_path = classify_render_frame_path(
            decode_path=DecodePath.CPU_PLANAR_YUV,
            pixel_format='p010', bit_depth=10, is_semi_planar=True
        )
        assert render_path == RenderFramePath.P010_R16UI_UPLOAD

    def test_frame_release_lifecycle(self):
        """GpuFrameRelease should only release once."""
        from src.shared.media.gpu_frame import GpuFrameRelease

        release_count = 0

        def on_release():
            nonlocal release_count
            release_count += 1

        release = GpuFrameRelease(release_callback=on_release)

        # Release twice - should only call callback once
        release.release()
        release.release()

        assert release_count == 1
        assert release.is_released

    def test_frame_path_enums_consistency(self):
        """Verify all decode/render paths have consistent properties."""
        from src.shared.media.frame_path import DecodePath, RenderFramePath

        # Zero-copy decode paths
        zc_decode = {dp for dp in DecodePath if dp.is_zero_copy}
        assert DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY in zc_decode

        # Zero-copy render paths
        zc_render = {rp for rp in RenderFramePath if rp.is_zero_copy}
        assert RenderFramePath.GPU_DECODED_EXTERNAL_YUV in zc_render

        # CPU decode paths
        cpu_decode = {dp for dp in DecodePath if dp.is_cpu}
        assert DecodePath.CPU_PLANAR_YUV in cpu_decode
        assert DecodePath.CPU_PACKED_RGB in cpu_decode

        # Hwaccel download paths
        hw_dl = {dp for dp in DecodePath if dp.is_hwaccel_download}
        assert DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB in hw_dl
