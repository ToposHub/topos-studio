"""
Frame Path 模块测试（Phase A1）

测试目标：
1. DecodePath 枚举值正确
2. RenderFramePath 枚举值正确
3. is_zero_copy / is_cpu / is_hwaccel_download / needs_cpu_download 属性正确
4. classify_decode_path 分类逻辑正确
5. classify_render_frame_path 分类逻辑正确
6. is_zero_copy_combination 组合判断正确
7. fallback_reason_for_decode_path / fallback_reason_for_render_path 正确
8. PerformanceMonitor 零拷贝追踪字段和统计正确
"""

from __future__ import annotations

import pytest
import time
import threading

from src.shared.media.frame_path import (
    DecodePath,
    RenderFramePath,
    classify_decode_path,
    classify_render_frame_path,
    is_zero_copy_combination,
    fallback_reason_for_decode_path,
    fallback_reason_for_render_path,
)
from src.shared.media.performance_monitor import (
    VideoPerformanceMonitor,
    get_performance_monitor,
    reset_global_monitor,
)


# =========================================================================
# DecodePath 枚举测试
# =========================================================================

class TestDecodePath:
    """DecodePath 枚举测试"""

    def test_all_decode_paths_defined(self) -> None:
        assert DecodePath.CPU_PLANAR_YUV == "cpu_planar_yuv"
        assert DecodePath.CPU_PACKED_RGB == "cpu_packed_rgb"
        assert DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB == "hwaccel_download_packed_rgb"
        assert DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV == "hwaccel_download_planar_yuv"
        assert DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY == "macos_vt_iosurface_zero_copy"
        assert DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY == "macos_topos_iosurface_zero_copy"
        assert DecodePath.WINDOWS_D3D11_GL_ZERO_COPY == "windows_d3d11_gl_zero_copy"

    def test_decode_path_is_zero_copy(self) -> None:
        assert DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.is_zero_copy is True
        assert DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY.is_zero_copy is True
        assert DecodePath.WINDOWS_D3D11_GL_ZERO_COPY.is_zero_copy is True
        assert DecodePath.CPU_PLANAR_YUV.is_zero_copy is False
        assert DecodePath.CPU_PACKED_RGB.is_zero_copy is False
        assert DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB.is_zero_copy is False
        assert DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV.is_zero_copy is False

    def test_decode_path_is_cpu(self) -> None:
        assert DecodePath.CPU_PLANAR_YUV.is_cpu is True
        assert DecodePath.CPU_PACKED_RGB.is_cpu is True
        assert DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB.is_cpu is False
        assert DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.is_cpu is False
        assert DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY.is_cpu is False

    def test_decode_path_is_hwaccel_download(self) -> None:
        assert DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB.is_hwaccel_download is True
        assert DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV.is_hwaccel_download is True
        assert DecodePath.CPU_PLANAR_YUV.is_hwaccel_download is False
        assert DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.is_hwaccel_download is False
        assert DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY.is_hwaccel_download is False

    def test_decode_path_needs_cpu_download(self) -> None:
        assert DecodePath.CPU_PLANAR_YUV.needs_cpu_download is True
        assert DecodePath.CPU_PACKED_RGB.needs_cpu_download is True
        assert DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB.needs_cpu_download is True
        assert DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV.needs_cpu_download is True
        assert DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.needs_cpu_download is False
        assert DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY.needs_cpu_download is False
        assert DecodePath.WINDOWS_D3D11_GL_ZERO_COPY.needs_cpu_download is False

    def test_decode_path_str_enum(self) -> None:
        assert isinstance(DecodePath.CPU_PLANAR_YUV, str)
        assert DecodePath.CPU_PLANAR_YUV == "cpu_planar_yuv"
        assert DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.value == "macos_vt_iosurface_zero_copy"
        assert DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY.value == "macos_topos_iosurface_zero_copy"


# =========================================================================
# RenderFramePath 枚举测试
# =========================================================================

class TestRenderFramePath:
    """RenderFramePath 枚举测试"""

    def test_all_render_paths_defined(self) -> None:
        assert RenderFramePath.PACKED_RGB_CPU_UPLOAD == "packed_rgb_cpu_upload"
        assert RenderFramePath.PLANAR_YUV_R8_UPLOAD == "planar_yuv_r8_upload"
        assert RenderFramePath.PLANAR_YUV_R16UI_UPLOAD == "planar_yuv_r16ui_upload"
        assert RenderFramePath.NV12_R8_RG8_UPLOAD == "nv12_r8_rg8_upload"
        assert RenderFramePath.P010_R16UI_UPLOAD == "p010_r16ui_upload"
        assert RenderFramePath.GPU_DECODED_EXTERNAL_YUV == "gpu_decoded_external_yuv"
        assert RenderFramePath.GPU_DECODED_EXTERNAL_RGB == "gpu_decoded_external_rgb"

    def test_render_path_is_zero_copy(self) -> None:
        assert RenderFramePath.GPU_DECODED_EXTERNAL_YUV.is_zero_copy is True
        assert RenderFramePath.GPU_DECODED_EXTERNAL_RGB.is_zero_copy is True
        assert RenderFramePath.PACKED_RGB_CPU_UPLOAD.is_zero_copy is False
        assert RenderFramePath.PLANAR_YUV_R8_UPLOAD.is_zero_copy is False
        assert RenderFramePath.PLANAR_YUV_R16UI_UPLOAD.is_zero_copy is False
        assert RenderFramePath.NV12_R8_RG8_UPLOAD.is_zero_copy is False
        assert RenderFramePath.P010_R16UI_UPLOAD.is_zero_copy is False

    def test_render_path_needs_cpu_upload(self) -> None:
        assert RenderFramePath.PACKED_RGB_CPU_UPLOAD.needs_cpu_upload is True
        assert RenderFramePath.PLANAR_YUV_R8_UPLOAD.needs_cpu_upload is True
        assert RenderFramePath.PLANAR_YUV_R16UI_UPLOAD.needs_cpu_upload is True
        assert RenderFramePath.NV12_R8_RG8_UPLOAD.needs_cpu_upload is True
        assert RenderFramePath.P010_R16UI_UPLOAD.needs_cpu_upload is True
        assert RenderFramePath.GPU_DECODED_EXTERNAL_YUV.needs_cpu_upload is False
        assert RenderFramePath.GPU_DECODED_EXTERNAL_RGB.needs_cpu_upload is False

    def test_render_path_needs_texture_write(self) -> None:
        assert RenderFramePath.PACKED_RGB_CPU_UPLOAD.needs_texture_write is True
        assert RenderFramePath.GPU_DECODED_EXTERNAL_YUV.needs_texture_write is False


# =========================================================================
# 分类函数测试
# =========================================================================

class TestClassifyDecodePath:
    """classify_decode_path 测试"""

    def test_cpu_planar_yuv(self) -> None:
        path = classify_decode_path(
            hwaccel_enabled=False, is_planar=True, pixel_format="yuv420p"
        )
        assert path == DecodePath.CPU_PLANAR_YUV

    def test_cpu_packed_rgb(self) -> None:
        path = classify_decode_path(
            hwaccel_enabled=False, is_planar=False, pixel_format="rgba"
        )
        assert path == DecodePath.CPU_PACKED_RGB

    def test_hwaccel_download_packed_rgb(self) -> None:
        path = classify_decode_path(
            hwaccel_enabled=True, decoder_type="videotoolbox",
            pixel_format="rgba", zero_copy_available=False,
        )
        assert path == DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB

    def test_hwaccel_download_planar_yuv(self) -> None:
        path = classify_decode_path(
            hwaccel_enabled=True, decoder_type="videotoolbox",
            pixel_format="yuv420p", is_planar=True, zero_copy_available=False,
        )
        assert path == DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV

    def test_macos_vt_zero_copy(self) -> None:
        path = classify_decode_path(
            hwaccel_enabled=True, decoder_type="videotoolbox",
            zero_copy_available=True,
        )
        assert path == DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY

    def test_macos_topos_zero_copy(self) -> None:
        path = classify_decode_path(
            hwaccel_enabled=False, decoder_type="topos",
            pixel_format="yuv422p10le", is_planar=True,
            zero_copy_available=True,
        )
        assert path == DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY

    def test_windows_d3d11_zero_copy(self) -> None:
        path = classify_decode_path(
            hwaccel_enabled=True, decoder_type="d3d11va",
            zero_copy_available=True,
        )
        assert path == DecodePath.WINDOWS_D3D11_GL_ZERO_COPY

    def test_nvdec_zero_copy(self) -> None:
        path = classify_decode_path(
            hwaccel_enabled=True, decoder_type="nvdec",
            zero_copy_available=True,
        )
        assert path == DecodePath.WINDOWS_D3D11_GL_ZERO_COPY


class TestClassifyRenderFramePath:
    """classify_render_frame_path 测试"""

    def test_zero_copy_yuv(self) -> None:
        path = classify_render_frame_path(
            decode_path=DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY,
            pixel_format="nv12",
        )
        # P3-04：classify 现在产出 graphics 限定的规范值（旧值仅作读别名）
        assert path == RenderFramePath.OPENGL_EXTERNAL_YUV

    def test_zero_copy_rgb(self) -> None:
        path = classify_render_frame_path(
            decode_path=DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY,
            pixel_format="rgba",
        )
        assert path == RenderFramePath.OPENGL_EXTERNAL_RGB

    def test_planar_yuv_r8(self) -> None:
        path = classify_render_frame_path(
            decode_path=DecodePath.CPU_PLANAR_YUV,
            pixel_format="yuv420p", bit_depth=8, is_planar=True,
        )
        assert path == RenderFramePath.PLANAR_YUV_R8_UPLOAD

    def test_planar_yuv_r16ui(self) -> None:
        path = classify_render_frame_path(
            decode_path=DecodePath.CPU_PLANAR_YUV,
            pixel_format="yuv422p10le", bit_depth=10, is_planar=True,
        )
        assert path == RenderFramePath.PLANAR_YUV_R16UI_UPLOAD

    def test_nv12_r8_rg8(self) -> None:
        path = classify_render_frame_path(
            decode_path=DecodePath.CPU_PLANAR_YUV,
            pixel_format="nv12", bit_depth=8, is_semi_planar=True,
        )
        assert path == RenderFramePath.NV12_R8_RG8_UPLOAD

    def test_p010_r16ui(self) -> None:
        path = classify_render_frame_path(
            decode_path=DecodePath.CPU_PLANAR_YUV,
            pixel_format="p010", bit_depth=10, is_semi_planar=True,
        )
        assert path == RenderFramePath.P010_R16UI_UPLOAD

    def test_packed_rgb(self) -> None:
        path = classify_render_frame_path(
            decode_path=DecodePath.CPU_PACKED_RGB,
            pixel_format="rgba",
        )
        assert path == RenderFramePath.PACKED_RGB_CPU_UPLOAD


class TestIsZeroCopyCombination:
    """is_zero_copy_combination 测试"""

    def test_true_zero_copy(self) -> None:
        assert is_zero_copy_combination(
            DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY,
            RenderFramePath.GPU_DECODED_EXTERNAL_YUV,
        ) is True

    def test_false_cpu_path(self) -> None:
        assert is_zero_copy_combination(
            DecodePath.CPU_PLANAR_YUV,
            RenderFramePath.PLANAR_YUV_R8_UPLOAD,
        ) is False

    def test_false_hwaccel_download(self) -> None:
        assert is_zero_copy_combination(
            DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB,
            RenderFramePath.PACKED_RGB_CPU_UPLOAD,
        ) is False

    def test_false_mixed_path(self) -> None:
        assert is_zero_copy_combination(
            DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY,
            RenderFramePath.PACKED_RGB_CPU_UPLOAD,
        ) is False


class TestFallbackReason:
    """fallback_reason 测试"""

    def test_zero_copy_has_no_fallback(self) -> None:
        assert fallback_reason_for_decode_path(DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY) == ""
        assert fallback_reason_for_render_path(RenderFramePath.GPU_DECODED_EXTERNAL_YUV) == ""

    def test_cpu_fallback_reason(self) -> None:
        assert fallback_reason_for_decode_path(DecodePath.CPU_PLANAR_YUV) == "cpu_decode_no_hwaccel"
        assert fallback_reason_for_decode_path(DecodePath.CPU_PACKED_RGB) == "cpu_decode_no_hwaccel"

    def test_hwaccel_download_fallback_reason(self) -> None:
        assert fallback_reason_for_decode_path(DecodePath.HWACCEL_DOWNLOAD_PACKED_RGB) == "hwaccel_cpu_download_packed_rgb"
        assert fallback_reason_for_decode_path(DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV) == "hwaccel_cpu_download_planar_yuv"

    def test_render_fallback_reason(self) -> None:
        assert fallback_reason_for_render_path(RenderFramePath.PACKED_RGB_CPU_UPLOAD) == "cpu_upload_packed_rgb"
        assert fallback_reason_for_render_path(RenderFramePath.PLANAR_YUV_R8_UPLOAD) == "cpu_upload_planar_yuv_r8"
        assert fallback_reason_for_render_path(RenderFramePath.PLANAR_YUV_R16UI_UPLOAD) == "cpu_upload_planar_yuv_r16ui"
        assert fallback_reason_for_render_path(RenderFramePath.NV12_R8_RG8_UPLOAD) == "cpu_upload_nv12_r8_rg8"
        assert fallback_reason_for_render_path(RenderFramePath.P010_R16UI_UPLOAD) == "cpu_upload_p010_r16ui"


# =========================================================================
# PerformanceMonitor 零拷贝追踪测试 (A2)
# =========================================================================

class TestZeroCopyTracking:
    """零拷贝追踪统计测试"""

    def test_track_zero_copy_frame(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_zero_copy_frame(
            decode_path="macos_vt_iosurface_zero_copy",
            render_frame_path="gpu_decoded_external_yuv",
            is_zero_copy=True,
            external_texture=True,
        )

        stats = monitor.get_aggregate_stats()
        assert stats['zero_copy_frames'] == 1
        assert stats['cpu_download_frames'] == 0
        assert stats['cpu_upload_frames'] == 0
        assert stats['external_texture_frames'] == 1
        assert stats['texture_write_video_frames'] == 0
        assert stats['decode_path_counts'].get('macos_vt_iosurface_zero_copy') == 1
        assert stats['render_frame_path_counts'].get('gpu_decoded_external_yuv') == 1
        assert stats['last_fallback_reason'] is None

    def test_track_cpu_upload_frame(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_zero_copy_frame(
            decode_path="cpu_planar_yuv",
            render_frame_path="planar_yuv_r8_upload",
            is_zero_copy=False,
            cpu_upload=True,
            texture_write=True,
            fallback_reason="cpu_decode_no_hwaccel",
        )

        stats = monitor.get_aggregate_stats()
        assert stats['zero_copy_frames'] == 0
        assert stats['cpu_upload_frames'] == 1
        assert stats['texture_write_video_frames'] == 1
        assert stats['external_texture_frames'] == 0
        assert stats['last_fallback_reason'] == "cpu_decode_no_hwaccel"

    def test_track_hwaccel_download_frame(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_zero_copy_frame(
            decode_path="hwaccel_download_packed_rgb",
            render_frame_path="packed_rgb_cpu_upload",
            is_zero_copy=False,
            cpu_download=True,
            cpu_upload=True,
            texture_write=True,
            fallback_reason="hwaccel_cpu_download_packed_rgb",
        )

        stats = monitor.get_aggregate_stats()
        assert stats['zero_copy_frames'] == 0
        assert stats['cpu_download_frames'] == 1
        assert stats['cpu_upload_frames'] == 1
        assert stats['texture_write_video_frames'] == 1

    def test_mixed_frames(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        for i in range(10):
            monitor.track_zero_copy_frame(
                decode_path="macos_vt_iosurface_zero_copy",
                render_frame_path="gpu_decoded_external_yuv",
                is_zero_copy=True,
                external_texture=True,
            )

        for i in range(3):
            monitor.track_zero_copy_frame(
                decode_path="cpu_planar_yuv",
                render_frame_path="planar_yuv_r8_upload",
                is_zero_copy=False,
                cpu_upload=True,
                texture_write=True,
                fallback_reason="cpu_decode_no_hwaccel",
            )

        stats = monitor.get_aggregate_stats()
        assert stats['zero_copy_frames'] == 10
        assert stats['cpu_upload_frames'] == 3
        assert stats['external_texture_frames'] == 10
        assert stats['texture_write_video_frames'] == 3
        assert stats['decode_path_counts']['macos_vt_iosurface_zero_copy'] == 10
        assert stats['decode_path_counts']['cpu_planar_yuv'] == 3
        assert stats['render_frame_path_counts']['gpu_decoded_external_yuv'] == 10
        assert stats['render_frame_path_counts']['planar_yuv_r8_upload'] == 3
        assert stats['last_fallback_reason'] == "cpu_decode_no_hwaccel"

    def test_track_cpu_download_frame(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_cpu_download_frame()
        monitor.track_cpu_download_frame()

        stats = monitor.get_aggregate_stats()
        assert stats['cpu_download_frames'] == 2

    def test_track_cpu_upload_frame_direct(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_cpu_upload_frame()
        monitor.track_cpu_upload_frame()
        monitor.track_cpu_upload_frame()

        stats = monitor.get_aggregate_stats()
        assert stats['cpu_upload_frames'] == 3

    def test_track_external_texture_frame_direct(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_external_texture_frame()
        monitor.track_external_texture_frame()

        stats = monitor.get_aggregate_stats()
        assert stats['external_texture_frames'] == 2

    def test_zero_copy_counts_reset_on_session(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_zero_copy_frame(
            decode_path="macos_vt_iosurface_zero_copy",
            render_frame_path="gpu_decoded_external_yuv",
            is_zero_copy=True,
            external_texture=True,
        )

        stats = monitor.get_aggregate_stats()
        assert stats['zero_copy_frames'] == 1

        monitor.start_session()
        stats = monitor.get_aggregate_stats()
        assert stats['zero_copy_frames'] == 0
        assert stats['cpu_download_frames'] == 0
        assert stats['external_texture_frames'] == 0
        assert stats['decode_path_counts'] == {}
        assert stats['render_frame_path_counts'] == {}
        assert stats['last_fallback_reason'] is None

    def test_non_zero_copy_sets_fallback_reason_auto(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_zero_copy_frame(
            decode_path="cpu_planar_yuv",
            render_frame_path="planar_yuv_r8_upload",
            is_zero_copy=False,
        )

        stats = monitor.get_aggregate_stats()
        assert stats['last_fallback_reason'] == "non_zero_copy_path"

    def test_aggregate_log_includes_zero_copy_info(self) -> None:
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        monitor.track_zero_copy_frame(
            decode_path="macos_vt_iosurface_zero_copy",
            render_frame_path="gpu_decoded_external_yuv",
            is_zero_copy=True,
            external_texture=True,
        )
        monitor.track_tick_time(5.0)

        log_output = monitor._output_aggregate_log()
        assert log_output is None

# ---------------------------------------------------------------------------
# P3-04: graphics-backend-qualified external paths + legacy aliases
# ---------------------------------------------------------------------------

class TestQualifiedExternalPaths:
    def test_qualify_external_opengl(self):
        assert (RenderFramePath.qualify_external("opengl", is_rgb=False)
                is RenderFramePath.OPENGL_EXTERNAL_YUV)
        assert (RenderFramePath.qualify_external("opengl", is_rgb=True)
                is RenderFramePath.OPENGL_EXTERNAL_RGB)

    def test_qualify_external_metal(self):
        assert (RenderFramePath.qualify_external("metal", is_rgb=False)
                is RenderFramePath.METAL_EXTERNAL_YUV)

    def test_qualify_external_rejects_unknown_backend(self):
        with pytest.raises(ValueError, match="unsupported graphics backend"):
            RenderFramePath.qualify_external("cuda", is_rgb=False)

    def test_new_qualified_values_are_zero_copy(self):
        for member in (RenderFramePath.OPENGL_EXTERNAL_YUV,
                       RenderFramePath.OPENGL_EXTERNAL_RGB,
                       RenderFramePath.METAL_EXTERNAL_YUV,
                       RenderFramePath.METAL_EXTERNAL_RGB):
            assert member.is_zero_copy is True

    def test_legacy_values_still_parse_and_stay_zero_copy(self):
        # 旧值仍是合法枚举成员（兼容既有比较/序列化）
        assert RenderFramePath("gpu_decoded_external_yuv").is_zero_copy is True

    def test_from_serialized_maps_legacy_to_qualified(self):
        assert (RenderFramePath.from_serialized("gpu_decoded_external_yuv")
                is RenderFramePath.OPENGL_EXTERNAL_YUV)
        assert (RenderFramePath.from_serialized("gpu_decoded_external_rgb")
                is RenderFramePath.OPENGL_EXTERNAL_RGB)

    def test_from_serialized_passes_new_values_through(self):
        assert (RenderFramePath.from_serialized("metal_external_yuv")
                is RenderFramePath.METAL_EXTERNAL_YUV)
        assert (RenderFramePath.from_serialized("planar_yuv_r8_upload")
                is RenderFramePath.PLANAR_YUV_R8_UPLOAD)

    def test_from_serialized_rejects_unknown_explicitly(self):
        with pytest.raises(ValueError, match="unknown render frame path"):
            RenderFramePath.from_serialized("does_not_exist")

    def test_try_from_serialized_returns_none_for_freeform_diagnostics(self):
        # _debug_frame_path 是自由格式诊断串：遥测侧必须拿 None 而非异常，
        # 否则 track_frame_path / track_zero_copy_frame 整段被吞（回归：
        # 'unknown' / '*_gpu_failed' 曾使性能统计静默丢帧）。
        for raw in ("unknown", "planar_yuv_r8_upload_gpu_failed", "", None):
            assert RenderFramePath.try_from_serialized(raw) is None

    def test_try_from_serialized_parses_valid_and_legacy_values(self):
        assert (RenderFramePath.try_from_serialized("opengl_external_yuv")
                is RenderFramePath.OPENGL_EXTERNAL_YUV)
        assert (RenderFramePath.try_from_serialized("gpu_decoded_external_rgb")
                is RenderFramePath.OPENGL_EXTERNAL_RGB)
        assert (RenderFramePath.try_from_serialized("planar_yuv_r16ui_upload")
                is RenderFramePath.PLANAR_YUV_R16UI_UPLOAD)

    def test_classify_uses_graphics_api_for_zero_copy(self):
        path = classify_render_frame_path(
            DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY,
            pixel_format="nv12",
            graphics_api="metal",
        )
        assert path is RenderFramePath.METAL_EXTERNAL_YUV

    def test_classify_default_remains_opengl(self):
        path = classify_render_frame_path(
            DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY, pixel_format="nv12")
        assert path is RenderFramePath.OPENGL_EXTERNAL_YUV
