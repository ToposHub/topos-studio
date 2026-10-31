"""CVMetalVideoBridge 测试（计划 Phase 5，P5-01/02/04/05）。

两层：
- 语义层（任意环境可跑）：plane 格式矩阵、转换参数构建、numpy 参考
  数学、MetalVideoFrameRing 生命周期（command completion 前不释放）。
- 真机层（有 Metal 设备 + Quartz 时跑）：合成 NV12/P010 CVPixelBuffer
  → 导入 MTLTexture → MSL 转换 → 读回，与 numpy 参考做逐像素误差门禁。
  合成数据直接写进 CVPixelBuffer 的 plane 内存，若导入路径存在 CPU
  拷贝/伪造，读回结果与参考的比对会失败——这是 P5-04 的零拷贝正确性
  门禁。
"""

from __future__ import annotations

import ctypes
import sys

import numpy as np
import pytest

from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.submission_scheduler import MetalSubmissionScheduler
from src.shared.gpu.metal.kernels_video import (
    YUV_CONVERT_PARAMS_DTYPE,
    build_yuv_convert_params,
    yuv_to_rgba_reference,
)
from src.shared.media.frame_path import DecodePath
from src.shared.media.gpu_frame import GpuFrameRelease
from src.shared.media.macos_vt.cv_metal_bridge import (
    _PLANE_LAYOUTS,
    MetalVideoFrameRing,
    cv_metal_bridge_available,
    is_cv_metal_bridge_implemented,
    lock_pixel_buffer_planes,
    mtl_pixel_format,
    unlock_pixel_buffer_base_address,
)


# ---------------------------------------------------------------------------
# 语义层
# ---------------------------------------------------------------------------


_darwin_only_w01 = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS 专属逻辑（VT/Metal）；W01 复跑确认在 Windows 为测试缺平台门"
)

@_darwin_only_w01
class TestPlaneLayouts :
    def test_nv12_layout(self):
        fourccs, planes = _PLANE_LAYOUTS["nv12"]
        assert [p[0] for p in planes] == ["Y", "UV"]
        assert [p[1] for p in planes] == ["r8unorm", "rg8unorm"]
        assert planes[1][3:] == (2, 2)  # 4:2:0 半分辨率

    def test_p010_layout(self):
        _, planes = _PLANE_LAYOUTS["p010"]
        assert [p[1] for p in planes] == ["r16unorm", "rg16unorm"]

    def test_p010_fourccs_match_corevideo_constants(self):
        try:
            import Quartz
        except ImportError:
            pytest.skip("CoreVideo FourCC constants are available only on macOS")

        fourccs, _ = _PLANE_LAYOUTS["p010"]
        expected = {
            int(Quartz.kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange),
            int(Quartz.kCVPixelFormatType_420YpCbCr10BiPlanarFullRange),
        }
        assert fourccs == expected
        assert all(len(value.to_bytes(4, "big")) == 4 for value in fourccs)

    def test_mtl_format_values_match_metal_module(self):
        # 防回归：格式值必须来自 PyObjC Metal 模块（手写魔数曾把
        # RG8Unorm 误写为 70——实为 RGBA8Unorm 的值——门禁抓出）
        import Metal

        assert mtl_pixel_format("r8unorm") == Metal.MTLPixelFormatR8Unorm
        assert mtl_pixel_format("rg8unorm") == Metal.MTLPixelFormatRG8Unorm
        assert mtl_pixel_format("r16unorm") == Metal.MTLPixelFormatR16Unorm
        assert mtl_pixel_format("rg16unorm") == Metal.MTLPixelFormatRG16Unorm
        assert mtl_pixel_format("rgba16float") == Metal.MTLPixelFormatRGBA16Float

    def test_unknown_format_name_raises(self):
        with pytest.raises(ValueError, match="MTLPixelFormat"):
            mtl_pixel_format("bgra10unorm")

    def test_unsupported_formats_absent(self):
        # v210/422 无 10-bit 直通（ADR 0003）——不得伪装支持
        assert "v210" not in _PLANE_LAYOUTS
        assert "2vuy" not in _PLANE_LAYOUTS


class TestDecodePathCvMetal:
    def test_new_path_is_zero_copy(self):
        path = DecodePath.MACOS_VT_CVMETAL_ZERO_COPY
        assert path.value == "macos_vt_cvmetal_zero_copy"
        assert path.is_zero_copy


class TestConvertParams:
    def test_dtype_matches_msl_struct(self):
        # 全 float32、9 字段——与 MSL YuvConvertParams 逐字段对齐
        assert YUV_CONVERT_PARAMS_DTYPE.itemsize == 9 * 4

    def test_limited_range_8bit(self):
        p = build_yuv_convert_params(8, "limited", "bt709")
        assert p["max_code"][0] == 255.0
        assert p["y_offset"][0] == 16.0
        assert p["y_scale"][0] == pytest.approx(1.0 / 219.0)
        assert p["c_neutral"][0] == 128.0
        assert p["c_scale"][0] == pytest.approx(1.0 / 224.0)

    def test_limited_range_10bit_p010(self):
        p = build_yuv_convert_params(10, "limited", "bt709")
        assert p["max_code"][0] == 1023.0
        assert p["y_offset"][0] == 64.0
        assert p["y_scale"][0] == pytest.approx(1.0 / 876.0)
        assert p["c_neutral"][0] == 512.0
        assert p["c_scale"][0] == pytest.approx(1.0 / 896.0)

    def test_full_range(self):
        p = build_yuv_convert_params(8, "full", "bt709")
        assert p["y_offset"][0] == 0.0
        assert p["y_scale"][0] == pytest.approx(1.0 / 255.0)
        assert p["c_scale"][0] == pytest.approx(1.0 / 256.0)

    def test_unknown_matrix_raises(self):
        with pytest.raises(ValueError, match="色彩矩阵"):
            build_yuv_convert_params(8, "limited", "gbr")

    def test_bad_bit_depth_raises(self):
        with pytest.raises(ValueError, match="位深"):
            build_yuv_convert_params(6, "limited", "bt709")

    def test_reference_white_black_and_neutral(self):
        # BT.709 limited：Y=235/255, 中性色度 → 白；Y=16/255 → 黑
        white = yuv_to_rgba_reference(
            np.array([[235.0 / 255.0]]),
            np.full((1, 1, 2), 128.0 / 255.0), 8, "limited", "bt709",
        )
        assert white[0, 0] == pytest.approx([1.0, 1.0, 1.0], abs=1e-3)
        black = yuv_to_rgba_reference(
            np.array([[16.0 / 255.0]]),
            np.full((1, 1, 2), 128.0 / 255.0), 8, "limited", "bt709",
        )
        assert black[0, 0] == pytest.approx([0.0, 0.0, 0.0], abs=1e-3)

    def test_reference_red_biased_sample(self):
        # 正 Cr、负偏 Cb → 偏红（回归 vt_zero_copy_probe 的绿项系数坑）
        rgb = yuv_to_rgba_reference(
            np.array([[0.55]]), np.array([[[0.42, 0.62]]]), 8, "limited", "bt709",
        )
        assert rgb[0, 0, 0] > rgb[0, 0, 1]
        assert rgb[0, 0, 0] > rgb[0, 0, 2]


class TestMetalVideoFrameRing:
    def _entry(self, counter: dict) -> GpuFrameRelease:
        def _cb() -> None:
            counter["released"] += 1

        return GpuFrameRelease(release_callback=_cb)

    def test_capacity(self):
        ring = MetalVideoFrameRing(max_in_flight=2)
        counter = {"released": 0}
        assert ring.track(0, self._entry(counter))
        assert ring.track(1, self._entry(counter))
        assert ring.track(2, self._entry(counter)) is False  # 满员拒绝
        assert ring.in_flight == 2

    def test_release_requires_gpu_complete(self):
        ring = MetalVideoFrameRing(max_in_flight=4)
        counter = {"released": 0}
        ring.track(0, self._entry(counter))
        ring.mark_submitted()
        # 已提交但未确认完成——不释放（宁可延迟回收也不放使用中的资源）
        assert ring.release_completed() == 0
        ring.on_gpu_complete()  # finish() 之后调用
        assert ring.release_completed() == 1
        assert ring.in_flight == 0

    def test_unsubmitted_never_released_by_complete(self):
        ring = MetalVideoFrameRing(max_in_flight=4)
        counter = {"released": 0}
        ring.track(0, self._entry(counter))
        ring.on_gpu_complete()
        assert ring.release_completed() == 0
        ring.mark_submitted()
        ring.on_gpu_complete()
        assert ring.release_completed() == 1

    def test_exactly_once(self):
        ring = MetalVideoFrameRing(max_in_flight=4)
        counter = {"released": 0}
        ring.track(0, self._entry(counter))
        ring.mark_submitted()
        ring.on_gpu_complete()
        assert ring.release_completed() == 1
        assert ring.release_completed() == 0
        assert counter["released"] == 1

    def test_release_all_for_destroy_or_seek(self):
        ring = MetalVideoFrameRing(max_in_flight=4)
        counter = {"released": 0}
        ring.track(0, self._entry(counter))
        ring.track(1, self._entry(counter))
        assert ring.release_all() == 2
        assert ring.in_flight == 0

    def test_bad_capacity(self):
        with pytest.raises(ValueError):
            MetalVideoFrameRing(max_in_flight=0)

    def test_completion_handler_releases_only_frames_in_that_submission(self):
        class Context:
            def __init__(self):
                self.callbacks = []

            def submit(self, *, completion, wait):
                command_buffer = type("CommandBuffer", (), {"error": lambda _self: None})()
                self.callbacks.append((completion, command_buffer))
                return command_buffer

        context = Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
        ring = MetalVideoFrameRing(max_in_flight=4)
        released = {0: 0, 1: 0}
        for index in (0, 1):
            ring.track(
                index,
                GpuFrameRelease(
                    release_callback=lambda index=index: released.__setitem__(
                        index, released[index] + 1
                    )
                ),
            )

        assert ring.submit_pending(scheduler, (0,)) is not None
        assert ring.submit_pending(scheduler, (1,)) is not None
        context.callbacks[0][0](context.callbacks[0][1])
        assert ring.release_completed() == 1
        assert released == {0: 1, 1: 0}
        context.callbacks[1][0](context.callbacks[1][1])
        assert ring.release_completed() == 1
        assert released == {0: 1, 1: 1}


class TestAvailability:
    def test_available_returns_tuple(self):
        ok, reason = cv_metal_bridge_available()
        assert isinstance(ok, bool)
        assert isinstance(reason, str)

    def test_implemented_flag_on_macos(self):
        if sys.platform == "darwin":
            assert is_cv_metal_bridge_implemented()


# ---------------------------------------------------------------------------
# 真机层
# ---------------------------------------------------------------------------

_QUARTZ_OK = False
try:
    if sys.platform == "darwin":
        import Quartz  # noqa: F401

        _QUARTZ_OK = True
except ImportError:
    pass

_METAL_OK, _METAL_REASON = MetalComputeContext.availability_reason()

requires_mac_metal = pytest.mark.skipif(
    not (_QUARTZ_OK and _METAL_OK),
    reason=f"需要 macOS + Metal + Quartz（metal={_METAL_OK}: {_METAL_REASON}, "
           f"quartz={_QUARTZ_OK}）",
)

W, H = 64, 32  # 4:2:0 要求偶数维；小尺寸足以覆盖半分辨率寻址


def _make_biplanar_buffer(pixel_format_const: int) -> "object":
    status, pb = Quartz.CVPixelBufferCreate(
        None, W, H, pixel_format_const,
        {Quartz.kCVPixelBufferIOSurfacePropertiesKey: {}}, None,
    )
    if status != 0 or pb is None:  # status 显式 ==0 判断在 cv 里，这里直接拒绝
        pytest.fail(f"CVPixelBufferCreate 失败 status={status}")
    return pb


def _fill_nv12(pb) -> tuple[np.ndarray, np.ndarray]:
    """写入确定性 NV12 图案，返回 (y_norm(32,64), uv_norm(16,32,2))。"""
    y10 = (np.arange(H * W, dtype=np.uint8).reshape(H, W) * 7 + 16).astype(np.uint8)
    uv = np.empty((H // 2, W // 2, 2), dtype=np.uint8)
    uv[..., 0] = (np.arange(W // 2)[None, :] * 5 + np.arange(H // 2)[:, None] * 3 + 64) % 256
    uv[..., 1] = (np.arange(W // 2)[None, :] * 2 + np.arange(H // 2)[:, None] * 11 + 32) % 256
    planes = lock_pixel_buffer_planes(pb)
    assert len(planes) == 2
    (y_base, y_bpr), (uv_base, uv_bpr) = planes
    assert y_bpr >= W and uv_bpr >= W
    # Y plane
    y_rows = np.zeros((H, y_bpr), dtype=np.uint8)
    y_rows[:, :W] = y10
    ctypes.memmove(y_base, y_rows.tobytes(), y_rows.nbytes)
    # UV plane（交织 U/V）
    uv_rows = np.zeros((H // 2, uv_bpr), dtype=np.uint8)
    packed = np.empty((H // 2, W // 2, 2), dtype=np.uint8)
    packed[..., 0], packed[..., 1] = uv[..., 0], uv[..., 1]
    uv_rows[:, :W] = packed.reshape(H // 2, W)
    ctypes.memmove(uv_base, uv_rows.tobytes(), uv_rows.nbytes)
    unlock_pixel_buffer_base_address(pb)
    return y10.astype(np.float64) / 255.0, uv.astype(np.float64) / 255.0


def _fill_p010(pb) -> tuple[np.ndarray, np.ndarray]:
    """写入确定性 P010 图案（10-bit 左对齐进 16-bit），返回归一化平面。"""
    y10 = ((np.arange(H * W, dtype=np.uint32).reshape(H, W) * 31 + 64) % 1024).astype(np.uint16)
    u10 = ((np.arange(W // 2)[None, :] * 17 + np.arange(H // 2)[:, None] * 13 + 256) % 1024).astype(np.uint16)
    v10 = ((np.arange(W // 2)[None, :] * 7 + np.arange(H // 2)[:, None] * 29 + 128) % 1024).astype(np.uint16)
    planes = lock_pixel_buffer_planes(pb)
    assert len(planes) == 2
    (y_base, y_bpr), (uv_base, uv_bpr) = planes
    assert y_bpr >= W * 2 and uv_bpr >= W * 2
    y_rows = np.zeros((H, y_bpr // 2), dtype="<u2")
    y_rows[:, :W] = y10 << 6  # P010：10-bit 左对齐
    ctypes.memmove(y_base, y_rows.tobytes(), y_rows.nbytes)
    uv_rows = np.zeros((H // 2, uv_bpr // 2), dtype="<u2")
    uv_rows[:, 0::2] = u10 << 6
    uv_rows[:, 1::2] = v10 << 6
    ctypes.memmove(uv_base, uv_rows.tobytes(), uv_rows.nbytes)
    unlock_pixel_buffer_base_address(pb)
    # uint16 先移位再转 float（float 数组不支持左移位）
    y_norm = (y10 << 6).astype(np.float64) / 65535.0
    uv16 = np.stack([u10, v10], axis=-1)
    uv_norm = (uv16 << 6).astype(np.float64) / 65535.0
    return y_norm, uv_norm


@requires_mac_metal
class TestRealDeviceImport:
    def setup_method(self):
        assert MetalComputeContext.is_supported()

    def test_import_nv12_structure_and_release(self):
        from src.shared.media.macos_vt.cv_metal_bridge import CVMetalVideoBridge

        bridge = CVMetalVideoBridge(ring_size=2)
        assert bridge.initialize()
        pb = _make_biplanar_buffer(Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)
        try:
            result = bridge.import_pixel_buffer(pb, 0, "nv12", 8)
            assert result is not None, bridge.last_fallback_reason()
            planes, gpu_release = result
            assert len(planes) == 2
            for i, plane in enumerate(planes):
                assert plane.api == "metal"
                assert plane.resource is not None  # P2-03：非 GL 必须带 resource
                tex = plane.resource.native_handle
                exp = (W, H) if i == 0 else (W // 2, H // 2)
                assert (tex.width(), tex.height()) == exp
            assert planes[0].resolved_handle() is not None
            assert not gpu_release.is_released
            gpu_release.release()
            assert gpu_release.is_released
            gpu_release.release()  # exactly-once：重复无害
            assert bridge.bind_count() == 1
        finally:
            bridge.destroy()

    def test_format_mismatch_fails_explicitly(self):
        from src.shared.media.macos_vt.cv_metal_bridge import CVMetalVideoBridge

        bridge = CVMetalVideoBridge()
        assert bridge.initialize()
        # 声明 nv12 但实际是 BGRA packed buffer → 显式诊断，不得硬闯 Metal 校验
        status, pb = Quartz.CVPixelBufferCreate(None, W, H, Quartz.kCVPixelFormatType_32BGRA, None, None)
        assert status == 0
        try:
            assert bridge.import_pixel_buffer(pb, 0, "nv12", 8) is None
            assert "pixel_format_mismatch" in bridge.last_fallback_reason()
            assert bridge.fail_count() == 1
        finally:
            bridge.destroy()

    def test_unsupported_format_rejected(self):
        from src.shared.media.macos_vt.cv_metal_bridge import CVMetalVideoBridge

        bridge = CVMetalVideoBridge()
        assert bridge.initialize()
        # ProRes 无硬解（本机）→ 422 v210 路径整体不支持，接口层显式拒绝
        assert bridge.import_pixel_buffer(None, 0, "v210", 10) is None
        assert "unsupported_pixel_format" in bridge.last_fallback_reason()
        bridge.destroy()


@requires_mac_metal
class TestRealDeviceConversionParity:
    """P5-04 画质门禁：CVPixelBuffer 内存图案 → MTLTexture → RGBA16F 读回
    与 numpy 参考逐像素比对。"""

    GATE_8BIT = 2.5 / 255.0
    GATE_10BIT = 10.0 / 1023.0

    def _run(self, pb, fmt: str, bd: int, y_norm, uv_norm, color_range="limited"):
        from src.shared.media.macos_vt.cv_metal_bridge import (
            CVMetalVideoBridge,
            convert_planes_to_rgba16f,
            read_texture_rgba16f,
        )

        metal_ctx = MetalComputeContext()
        bridge = CVMetalVideoBridge(metal_ctx=metal_ctx)
        assert bridge.initialize()
        try:
            result = bridge.import_pixel_buffer(pb, 0, fmt, bd)
            assert result is not None, bridge.last_fallback_reason()
            planes, gpu_release = result
            y_tex = planes[0].resource.native_handle
            uv_tex = planes[1].resource.native_handle
            params = build_yuv_convert_params(bd, color_range, "bt709")
            out_tex = convert_planes_to_rgba16f(
                metal_ctx, y_tex, uv_tex, params, W, H
            )
            got = read_texture_rgba16f(metal_ctx, out_tex, W, H)
            expected = yuv_to_rgba_reference(y_norm, uv_norm, bd, color_range, "bt709")
            # 释放时机契约：读回（finish 之后）才允许 release
            bridge.ring.track(0, gpu_release)
            bridge.ring.mark_submitted()
            metal_ctx.finish()
            bridge.ring.on_gpu_complete()
            assert bridge.ring.release_completed() == 1
            return got, expected
        finally:
            bridge.destroy()

    def test_nv12_parity(self):
        pb = _make_biplanar_buffer(Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)
        try:
            y_norm, uv_norm = _fill_nv12(pb)
            got, expected = self._run(pb, "nv12", 8, y_norm, uv_norm)
            assert got.shape == (H, W, 4)
            diff = np.abs(got[..., :3].astype(np.float64) - expected.astype(np.float64))
            assert diff.max() <= self.GATE_8BIT, (
                f"NV12 画质门禁失败: max_abs={diff.max():.5f} "
                f"(gate={self.GATE_8BIT:.5f})"
            )
        finally:
            pass  # pb 的 +1 由 Quartz PyObjC 代理持有（GC 时 release），不要手动释放

    def test_p010_parity(self):
        pb = _make_biplanar_buffer(Quartz.kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange)
        try:
            y_norm, uv_norm = _fill_p010(pb)
            got, expected = self._run(pb, "p010", 10, y_norm, uv_norm)
            diff = np.abs(got[..., :3].astype(np.float64) - expected.astype(np.float64))
            assert diff.max() <= self.GATE_10BIT, (
                f"P010 画质门禁失败: max_abs={diff.max():.5f} "
                f"(gate={self.GATE_10BIT:.5f})"
            )
        finally:
            pass  # pb 的 +1 由 Quartz PyObjC 代理持有（GC 时 release），不要手动释放

    def test_p010_full_range_parity(self):
        pb = _make_biplanar_buffer(Quartz.kCVPixelFormatType_420YpCbCr10BiPlanarFullRange)
        try:
            y_norm, uv_norm = _fill_p010(pb)
            got, expected = self._run(pb, "p010", 10, y_norm, uv_norm, "full")
            diff = np.abs(got[..., :3].astype(np.float64) - expected.astype(np.float64))
            assert diff.max() <= self.GATE_10BIT, (
                f"Full-range P010 画质门禁失败: max_abs={diff.max():.5f} "
                f"(gate={self.GATE_10BIT:.5f})"
            )
        finally:
            pass  # pb 的 +1 由 Quartz PyObjC 代理持有（GC 时 release），不要手动释放

    def test_nv12_full_range_parity(self):
        pb = _make_biplanar_buffer(Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarFullRange)
        try:
            y_norm, uv_norm = _fill_nv12(pb)
            got, expected = self._run(pb, "nv12", 8, y_norm, uv_norm, "full")
            diff = np.abs(got[..., :3].astype(np.float64) - expected.astype(np.float64))
            assert diff.max() <= self.GATE_8BIT
        finally:
            pass  # pb 的 +1 由 Quartz PyObjC 代理持有（GC 时 release），不要手动释放


# ---------------------------------------------------------------------------
# source 集成（P5-07 / P5-08）
# ---------------------------------------------------------------------------


@_darwin_only_w01
class TestMetalZeroCopyGate :
    def test_disabled_by_default(self, monkeypatch):
        from src.shared.media.macos_vt.source import metal_zero_copy_gate

        monkeypatch.delenv("TOPOS_METAL_VIDEO_PREVIEW", raising=False)
        ok, reason = metal_zero_copy_gate("h264", 8, 1920, 1080)
        assert ok is False
        assert "disabled" in reason

    def test_enabled_but_prores_rejected(self, monkeypatch):
        """本机实测：ProRes 无 VT 硬解（§0.1）——无零拷贝源头，显式拒绝。"""
        from src.shared.media.macos_vt.source import metal_zero_copy_gate

        monkeypatch.setenv("TOPOS_METAL_VIDEO_PREVIEW", "1")
        ok, reason = metal_zero_copy_gate("prores", 10, 3840, 2160)
        assert ok is False
        # probe 不可用（无 path → verdict None）也必须拒绝而非放行
        assert "no_vt_hardware_decode" in reason

    def test_gate_honest_when_probe_unavailable(self, monkeypatch):
        """h264 有硬解（§0.1），但无 path 时 probe 返回 None——门禁诚实拒绝。"""
        from src.shared.media.macos_vt.source import metal_zero_copy_gate

        monkeypatch.setenv("TOPOS_METAL_VIDEO_PREVIEW", "1")
        ok, reason = metal_zero_copy_gate("h264", 8, 1920, 1080, path="")
        assert ok is False or ("no_vt_hardware_decode" in reason and ok is False)


@requires_mac_metal
class TestSourceMetalImport:
    def _make_frame(self, source, pb, frame_index=0):
        from src.shared.media.macos_vt.source import VtPixelBufferFrame
        from unittest.mock import MagicMock

        decoded = MagicMock()
        decoded.pixel_buffer = pb
        return VtPixelBufferFrame(
            source=source, decoded=decoded, frame_index=frame_index,
            pts=0, time_seconds=0.0, width=W, height=H,
            pixel_format="nv12", bit_depth=8,
        )

    def test_import_and_release_chain(self):
        from src.shared.media.frame_path import DecodePath, RenderFramePath
        from src.shared.media.gpu_frame import (
            get_frame_decode_path,
            get_frame_render_path,
        )
        from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

        source = MacOSVtZeroCopySource()
        pb = _make_biplanar_buffer(Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)
        frame = self._make_frame(source, pb)
        try:
            gpu_frame = source.import_pixel_buffer_frame_metal(frame)
            assert gpu_frame is not None, source.fallback_reason
            assert gpu_frame.decode_path == DecodePath.MACOS_VT_CVMETAL_ZERO_COPY.value
            assert gpu_frame.zero_copy is True
            assert all(p.api == "metal" for p in gpu_frame.planes)
            # P3-04 × P5-07 契约：路径枚举按 metal 限定
            assert (
                get_frame_render_path(gpu_frame)
                == RenderFramePath.METAL_EXTERNAL_YUV.value
            )
            assert get_frame_decode_path(gpu_frame) == (
                DecodePath.MACOS_VT_CVMETAL_ZERO_COPY.value
            )
            gpu_frame.release.release()
            assert gpu_frame.release.is_released
            gpu_frame.release.release()  # exactly-once
        finally:
            bridge = source._metal_bridge
            if bridge is not None:
                bridge.destroy()

    def test_missing_pixel_buffer_fails_with_reason(self):
        from unittest.mock import MagicMock

        from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

        source = MacOSVtZeroCopySource()
        decoded = MagicMock()
        decoded.pixel_buffer = None
        frame = self._make_frame(source, None)
        frame.decoded = decoded
        assert source.import_pixel_buffer_frame_metal(frame) is None
        assert source.fallback_reason == "missing_pixel_buffer"
