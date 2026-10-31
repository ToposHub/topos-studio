"""B2.1 Windows cuda 驻留零拷贝契约与接线测试（计划表 B）。

分两层：

- **L1（无硬件，全平台可跑）**：契约（CudaResidentFrame 幂等释放/字段、
  GpuFramePlane 裸纹理名规则）、opt-in 门禁（env 解析/默认关闭）、
  能力门 fail-closed、interop 会话未初始化/跨线程/已释放帧拒绝、
  源类软解帧 fail-closed（不冒充驻留）、SLICE seek 会话禁用。
- **L2（本机真机，需 nvcuda + cuda-bindings + 真实素材，否则 skip）**：
  驻留源端到端——open → read_frame_resident → GL 会话 bind（双平面
  readback md5 对拍同解码器下载参考）→ 契约字段 → slot 复用与
  已释放帧拒绝。

依据：docs/poc_interop_b12_windows_2026-10.md（B1.2 L2 报告）。
"""
from __future__ import annotations

import ctypes
import hashlib
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.shared.media.gpu_frame import GpuFramePlane  # noqa: E402
from src.shared.media.frame_path import DecodePath  # noqa: E402
from src.shared.media.windows_hw.cuda_resident import (  # noqa: E402
    CudaGlInteropSession,
    CudaResidentFrame,
    CudaResidentPlane,
    WindowsCudaResidentSource,
    import_failure_latch_tripped,
    is_cuda_resident_frame,
    plane_pitch_bytes,
    probe_cuda_resident_capability,
    register_import_failure,
    register_import_success,
    resident_decode_enabled,
    resident_route_available,
    reset_import_failure_latch,
)

TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"


def _make_frame(**overrides) -> CudaResidentFrame:
    planes = (
        CudaResidentPlane("Y", 0x10000, 4096, 3840, 2160),
        CudaResidentPlane("UV", 0x200000, 4096, 3840, 1080),
    )
    kwargs = dict(
        frame_index=3, pts=42, time_seconds=0.05, width=3840, height=2160,
        pixel_format="nv12", bit_depth=8, planes=planes, av_frame=object(),
        color_range="limited", color_matrix="bt709",
    )
    kwargs.update(overrides)
    return CudaResidentFrame(**kwargs)


# ===========================================================================
# L1：契约
# ===========================================================================

class TestCudaResidentFrameContract:
    def test_is_instance_and_fields(self):
        frame = _make_frame()
        assert is_cuda_resident_frame(frame)
        assert frame.frame_index == 3
        assert frame.pixel_format == "nv12"
        assert frame.planes[0].pitch == 4096
        assert frame.planes[0].row_bytes == 3840

    def test_release_idempotent(self):
        frame = _make_frame()
        assert not frame.released
        frame.release()
        frame.release()
        assert frame.released

    def test_frozen_plane(self):
        plane = CudaResidentPlane("Y", 1, 2, 3, 4)
        with pytest.raises(Exception):
            plane.pitch = 99


class TestPitchGeometry:
    def test_strides_in_element_units(self):
        info = {"strides": [4096, 1], "itemsize": 1}
        assert plane_pitch_bytes(info, 3840) == 4096

    def test_no_strides_falls_back_to_row_bytes(self):
        assert plane_pitch_bytes({"strides": None, "itemsize": 1}, 3840) == 3840


class TestOptInGate:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("TOPOS_WIN_HW_RESIDENT_DECODE", raising=False)
        assert resident_decode_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "YES", " True "])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", value)
        assert resident_decode_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "off", "nope"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", value)
        assert resident_decode_enabled() is False


class TestCapabilityGate:
    def test_fail_closed_on_non_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        available, reason = probe_cuda_resident_capability()
        assert available is False
        assert reason == "not_windows"

    def test_fail_closed_without_bindings(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        # 非 Windows 宿主没有 ctypes.WinDLL（AttributeError 不在 probe 的
        # OSError 捕获域）。stub 成功加载，使探针走到被测的
        # cuda.bindings 导入分支——L1 用例须全平台可跑（文件头契约）。
        monkeypatch.setattr(
            ctypes, "WinDLL", lambda *_a, **_k: object(), raising=False)

        import builtins

        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name.startswith("cuda.bindings"):
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        available, reason = probe_cuda_resident_capability()
        assert available is False
        assert "cuda_bindings_unavailable" in reason


# ===========================================================================
# L1：interop 会话门禁（不触真实 CUDA/GL——前置条件校验分支）
# ===========================================================================

class TestInteropSessionGates:
    def _armed_session(self, owner_thread=None) -> CudaGlInteropSession:
        session = CudaGlInteropSession()
        session._initialized = True
        session._owner_thread_id = (
            owner_thread if owner_thread is not None
            else threading.get_ident())
        return session

    def test_bind_before_initialize_fail_closed(self):
        session = CudaGlInteropSession()
        result = session.bind_resident_frame(_make_frame())
        assert result is None
        assert session.last_fallback_reason == "session_not_initialized"

    def test_bind_released_frame_rejected(self):
        session = self._armed_session()
        frame = _make_frame()
        frame.release()
        assert session.bind_resident_frame(frame) is None
        assert session.last_fallback_reason == "invalid_or_released_frame"

    def test_bind_non_resident_rejected(self):
        session = self._armed_session()
        assert session.bind_resident_frame(object()) is None
        assert session.last_fallback_reason == "invalid_or_released_frame"

    def test_cross_thread_bind_rejected(self):
        session = self._armed_session(owner_thread=threading.get_ident() + 1)
        assert session.bind_resident_frame(_make_frame()) is None
        assert session.last_fallback_reason == "session_thread_mismatch"

    def test_destroy_idempotent_without_init(self):
        session = CudaGlInteropSession()
        session.destroy()
        session.destroy()

    def test_stats_shape(self):
        session = self._armed_session()
        stats = session.stats
        assert stats["initialized"] is True
        assert stats["slots"] == 0
        assert stats["bind_count"] == 0


# ===========================================================================
# L1：源类 fail-closed 语义（不触硬件）
# ===========================================================================

class TestSourceFailClosed:
    def test_convert_non_cuda_frame_raises(self, tmp_path):
        """非 cuda 帧（硬解中途失效/会话污染）必须显式失败，不冒充驻留。"""
        source = WindowsCudaResidentSource(str(tmp_path / "nope.mov"))

        class _FakeFormat:
            name = "nv12"

        class _FakeFrame:
            format = _FakeFormat()
            width = 3840
            height = 2160

        from src.shared.media.source import MediaDecodeError

        with pytest.raises(MediaDecodeError):
            source._convert_frame_planar(_FakeFrame())

    def test_slice_seek_session_disabled(self, tmp_path):
        """SLICE seek 会话产出软件帧，与驻留契约冲突——子类显式禁用。"""
        source = WindowsCudaResidentSource(str(tmp_path / "nope.mov"))
        with pytest.raises(RuntimeError):
            source._seek_gop_session(10, None)

    def test_non_nvdec_decoder_rejected(self, tmp_path):
        from src.shared.media.source import MediaOpenError

        source = WindowsCudaResidentSource(str(tmp_path / "nope.mov"))

        class _D3D11Decoder:
            value = "d3d11va"

        with pytest.raises(MediaOpenError):
            source._reopen_with_hwaccel(_D3D11Decoder())


class TestDecodePathMember:
    def test_zero_copy_family(self):
        assert DecodePath.WINDOWS_CUDA_GL_ZERO_COPY.is_zero_copy


class TestGpuPlaneContract:
    def test_bare_texture_id_requires_opengl(self):
        """P2-03 契约：非 GL 后端必须携带 GpuPlaneResource。"""
        with pytest.raises(ValueError):
            GpuFramePlane(
                component="Y", api="cuda", texture_id=1, target="2d",
                width=4, height=4)


def test_routing_env_off_by_default(monkeypatch):
    """opt-in 关闭时解码管理器不进入驻留分支（3.4b 门禁的第一道闸）。"""
    monkeypatch.delenv("TOPOS_WIN_HW_RESIDENT_DECODE", raising=False)
    assert resident_decode_enabled() is False


class TestImportFailureLatch:
    """B2.1 运行中降级：连续导入失败门闩（新源路由停止）。"""

    def setup_method(self):
        reset_import_failure_latch()

    def teardown_method(self):
        reset_import_failure_latch()

    def test_latch_trips_at_threshold(self, monkeypatch):
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
        assert resident_route_available() is True
        register_import_failure()
        register_import_failure()
        assert resident_route_available() is True  # 未达阈值
        register_import_failure()
        assert import_failure_latch_tripped() is True
        assert resident_route_available() is False

    def test_success_resets_consecutive_count(self, monkeypatch):
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
        register_import_failure()
        register_import_failure()
        register_import_success()  # 复位连续计数
        register_import_failure()
        register_import_failure()
        assert import_failure_latch_tripped() is False  # 2 次 < 阈值 3

    def test_success_does_not_untrip_latch(self, monkeypatch):
        """门闩进程内不自动解除（设备级故障不自愈）。"""
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
        for _ in range(3):
            register_import_failure()
        assert import_failure_latch_tripped()
        register_import_success()
        assert import_failure_latch_tripped() is True
        assert resident_route_available() is False

    def test_env_off_gates_route_regardless_of_latch(self, monkeypatch):
        monkeypatch.delenv("TOPOS_WIN_HW_RESIDENT_DECODE", raising=False)
        for _ in range(3):
            register_import_failure()
        assert resident_route_available() is False  # env 门优先

    def test_reset_untrips(self, monkeypatch):
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
        for _ in range(3):
            register_import_failure()
        reset_import_failure_latch()
        assert resident_route_available() is True


# ===========================================================================
# L2：本机真机端到端（无 nvcuda / cuda-bindings / 素材时诚实 skip）
# ===========================================================================

def _l2_ready() -> tuple[bool, str]:
    available, reason = probe_cuda_resident_capability()
    if not available:
        return False, reason
    if not (TEST_VIDEOS / "4K_H264.mov").exists():
        return False, "test_video_missing"
    return True, ""


_L2_OK, _L2_REASON = _l2_ready()


@pytest.mark.skipif(not _L2_OK, reason=f"cuda resident L2 unavailable: {_L2_REASON}")
class TestCudaResidentEndToEndMachineB:
    """机器 B（RTX 4090）真机：驻留源 → GL 导入 md5 对拍。

    时序遵循 B1.2 结论：解码会话先开（主上下文由 FFmpeg 初始化），
    interop 私有上下文后建；对拍参考用 is_hw_owned=False 下载会话
    （FFmpeg 私有 ctx 路径，免疫 -129 毒化）。
    """

    def test_resident_import_md5_matches_download_reference(self):
        import av
        import moderngl
        from av.codec.hwaccel import HWAccel, HWDeviceType

        source = WindowsCudaResidentSource(str(TEST_VIDEOS / "4K_H264.mov"))
        source.open()
        assert source.resident_contract_format in ("nv12", "p016le")
        frame0 = source.read_frame_resident(0)
        frame1 = source.read_frame_resident(1)
        assert is_cuda_resident_frame(frame0)
        assert frame0.frame_index == 0

        # 参考：同解码器下载会话（行归一化紧凑平面）
        ref_container = av.open(
            str(TEST_VIDEOS / "4K_H264.mov"),
            hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=False))
        ref_iter = ref_container.decode(ref_container.streams.video[0])
        ref_frame = next(ref_iter)
        ref_container.close()
        itemsize = 2 if frame0.pixel_format == "p016le" else 1
        w = ref_frame.width
        ref_md5 = []
        for idx, rows in ((0, ref_frame.height), (1, ref_frame.height // 2)):
            plane = ref_frame.planes[idx]
            pitch = plane.line_size
            row_bytes = w * itemsize
            mv = memoryview(plane)
            compact = b"".join(
                bytes(mv[r * pitch:r * pitch + row_bytes]) for r in range(rows))
            ref_md5.append(hashlib.md5(compact).hexdigest())

        # GL 导入（私有上下文；会话在解码会话之后创建——B1.2 时序约束）
        ctx = moderngl.create_standalone_context()
        session = CudaGlInteropSession(ring_size=2, max_ring_size=4)
        try:
            assert session.initialize()
            result = session.bind_resident_frame(
                frame0, decode_path=DecodePath.WINDOWS_CUDA_GL_ZERO_COPY.value)
            assert result is not None, session.last_fallback_reason
            planes, lease = result

            assert len(planes) == 2
            assert planes[0].api == "opengl"
            assert planes[0].target == "2d"
            if frame0.pixel_format == "nv12":
                assert planes[0].internal_format == "GL_R8"
                assert planes[1].internal_format == "GL_RG8"
            else:
                assert planes[0].internal_format == "GL_R16"
                assert planes[1].internal_format == "GL_RG16"

            # readback md5 对拍：glGetTexImage 直读会话纹理
            from OpenGL.GL import (
                glBindTexture, glGetTexImage, GL_TEXTURE_2D,
                GL_RED, GL_RG, GL_UNSIGNED_BYTE, GL_UNSIGNED_SHORT,
            )
            got_md5 = []
            for pi, (gl_fmt, ncomp) in enumerate(((GL_RED, 1), (GL_RG, 2))):
                gl_type = (GL_UNSIGNED_BYTE if itemsize == 1
                           else GL_UNSIGNED_SHORT)
                w_tex = frame0.width if pi == 0 else frame0.width // 2
                h_tex = frame0.height if pi == 0 else frame0.height // 2
                nbytes = w_tex * h_tex * ncomp * itemsize
                buf = ctypes.create_string_buffer(nbytes)
                glBindTexture(GL_TEXTURE_2D, planes[pi].texture_id)
                glGetTexImage(GL_TEXTURE_2D, 0, gl_fmt, gl_type, buf)
                got_md5.append(hashlib.md5(buf.raw).hexdigest())
            assert got_md5 == ref_md5, f"{got_md5} vs {ref_md5}"

            # slot 租约 exactly-once + 池复用：第二帧应复用同一 slot
            lease.release()
            lease.release()
            result1 = session.bind_resident_frame(
                frame1, decode_path=DecodePath.WINDOWS_CUDA_GL_ZERO_COPY.value)
            assert result1 is not None, session.last_fallback_reason
            assert session.stats["slots"] == 1
            planes1, lease1 = result1
            assert planes1[0].texture_id == planes[0].texture_id
            lease1.release()

            # 已释放源帧必须被拒绝（设备缓冲可能已归还解码器池）
            frame0.release()
            assert session.bind_resident_frame(frame0) is None
            assert session.last_fallback_reason == "invalid_or_released_frame"
            frame1.release()
        finally:
            session.destroy()
            ctx.release()
            source.close()

    def test_p010_resident_import(self):
        """P010 家族（H265 10bit）经 GL_R16/RG16 UNORM 路线。"""
        import moderngl

        path = TEST_VIDEOS / "4K_H265.mov"
        if not path.exists():
            pytest.skip("4K_H265.mov missing")
        source = WindowsCudaResidentSource(str(path))
        source.open()
        assert source.resident_contract_format == "p016le"
        frame = source.read_frame_resident(0)
        ctx = moderngl.create_standalone_context()
        session = CudaGlInteropSession(ring_size=1, max_ring_size=2)
        try:
            assert session.initialize()
            result = session.bind_resident_frame(frame)
            assert result is not None, session.last_fallback_reason
            planes, lease = result
            assert planes[0].internal_format == "GL_R16"
            assert planes[1].internal_format == "GL_RG16"
            # 采样语义验证：UNORM 纹理以 sampler2D 归一化读出（≠0 且 ≤1）
            from OpenGL.GL import (
                glBindTexture, glGetTexImage, GL_TEXTURE_2D,
                GL_RED, GL_UNSIGNED_SHORT,
            )
            import numpy as np

            w_tex, h_tex = frame.width, frame.height
            buf = ctypes.create_string_buffer(w_tex * h_tex * 2)
            glBindTexture(GL_TEXTURE_2D, planes[0].texture_id)
            glGetTexImage(GL_TEXTURE_2D, 0, GL_RED, GL_UNSIGNED_SHORT, buf)
            raw = np.frombuffer(buf.raw, dtype=np.uint16)
            assert raw.size == w_tex * h_tex
            assert int(raw.max()) > 0
            # MSB 对齐 10bit 码值：有效高位应大量非零（非 0..1023 低对齐）
            assert int(np.count_nonzero(raw)) > raw.size // 2
            lease.release()
            frame.release()
        finally:
            session.destroy()
            ctx.release()
            source.close()
