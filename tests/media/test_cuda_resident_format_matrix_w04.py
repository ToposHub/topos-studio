"""W04（计划 W-I04）：设备匹配与平面格式能力门 —— L2 真机格式矩阵（机器 B）。

覆盖计划验收矩阵的 Windows 侧子集：

- 非对齐尺寸（322×202，非 2^n 对齐）驻留导入 vs 下载参考逐字节 md5 对拍
  （pitch padding 几何正确性）；
- 4:2:2（High 4:2:2 H.264）→ NVDEC 无 422 cuda 驻留输出，能力门必须
  fail-closed 拒绝驻留路由（回退 CPU planar，不冒充驻留/不错读 UV）；
- 会话能力身份（stats 携带设备/驱动/来源——能力缓存的失效条件）；
- 强制错设备：GL 关联设备与上下文创建设备的对齐由 L1 替身覆盖
  （cuGLGetDevices 路径），真机单 4090 环境无第二台 CUDA 设备可强制
  （如实登记；AMD/Intel 另机取证）。

前置：win32 + nvcuda + cuda-bindings + 真实素材（否则诚实 skip）。
VT→GL 行属 M19/Mac（Windows 记"不适用"）。
"""
from __future__ import annotations

import ctypes
import hashlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.shared.media.frame_path import DecodePath  # noqa: E402
from src.shared.media.windows_hw.cuda_resident import (  # noqa: E402
    CudaGlInteropSession,
    WindowsCudaResidentSource,
    is_cuda_resident_frame,
    probe_cuda_resident_capability,
)

TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"
UNALIGNED_322X202 = TEST_VIDEOS / "unaligned_322x202_h264.mov"
YUV422_H264 = TEST_VIDEOS / "yuv422_h264.mov"


def _l2_ready() -> tuple[bool, str]:
    available, reason = probe_cuda_resident_capability()
    if not available:
        return False, reason
    if not UNALIGNED_322X202.exists():
        return False, "unaligned_test_video_missing"
    return True, ""


_L2_OK, _L2_REASON = _l2_ready()

pytestmark = pytest.mark.skipif(
    not _L2_OK, reason=f"cuda resident L2 unavailable: {_L2_REASON}")


def _download_reference_md5(path: Path, itemsize: int):
    """同解码器下载会话（is_hw_owned=False）行归一化紧凑平面 md5。"""
    import av
    from av.codec.hwaccel import HWAccel, HWDeviceType

    container = av.open(
        str(path), hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=False))
    frame = next(container.decode(container.streams.video[0]))
    container.close()
    w = frame.width
    md5s = []
    for idx, rows in ((0, frame.height), (1, frame.height // 2)):
        plane = frame.planes[idx]
        pitch = plane.line_size
        row_bytes = w * itemsize
        mv = memoryview(plane)
        compact = b"".join(
            bytes(mv[r * pitch:r * pitch + row_bytes]) for r in range(rows))
        md5s.append(hashlib.md5(compact).hexdigest())
    return md5s


def _texture_md5(planes, itemsize: int, width: int, height: int):
    from OpenGL.GL import (
        GL_RED, GL_RG, GL_TEXTURE_2D, GL_UNSIGNED_BYTE, GL_UNSIGNED_SHORT,
        glBindTexture, glGetTexImage,
    )

    md5s = []
    for pi, (gl_fmt, ncomp) in enumerate(((GL_RED, 1), (GL_RG, 2))):
        w = width if pi == 0 else width // 2
        h = height if pi == 0 else height // 2
        nbytes = w * h * ncomp * itemsize
        buf = ctypes.create_string_buffer(nbytes)
        glBindTexture(GL_TEXTURE_2D, planes[pi].texture_id)
        glGetTexImage(
            GL_TEXTURE_2D, 0, gl_fmt,
            GL_UNSIGNED_BYTE if itemsize == 1 else GL_UNSIGNED_SHORT, buf)
        md5s.append(hashlib.md5(buf.raw).hexdigest())
    return md5s


class TestUnalignedSizeResident:
    """322×202 非对齐尺寸：pitch padding 几何 + 逐字节 md5 对拍。"""

    def test_unaligned_322x202_resident_md5_matches_download(self):
        import moderngl

        source = WindowsCudaResidentSource(str(UNALIGNED_322X202))
        source.open()
        assert source.resident_contract_format == "nv12"
        frame = source.read_frame_resident(0)
        assert is_cuda_resident_frame(frame)
        assert (frame.width, frame.height) == (322, 202)
        # W04 平面几何：非对齐尺寸下 pitch 必然带 padding 且 ≥ row_bytes
        y, uv = frame.planes
        assert y.pitch >= y.row_bytes == 322
        assert uv.pitch >= uv.row_bytes == 322
        assert uv.rows == 101  # 202/2：半高恰为奇数行数

        ref_md5 = _download_reference_md5(UNALIGNED_322X202, itemsize=1)

        ctx = moderngl.create_standalone_context()
        try:
            session = CudaGlInteropSession(ring_size=2, max_ring_size=4)
            assert session.initialize(), session.last_fallback_reason
            result = session.bind_resident_frame(
                frame, decode_path=DecodePath.WINDOWS_CUDA_GL_ZERO_COPY.value)
            assert result is not None, session.last_fallback_reason
            planes, lease = result
            try:
                got_md5 = _texture_md5(planes, 1, 322, 202)
                assert got_md5 == ref_md5, f"{got_md5} vs {ref_md5}"
            finally:
                lease.release()
            session.destroy()
        finally:
            ctx.release()
            source.close()


class TestYuv422Rejected:
    """4:2:2 H.264：驻留路由必须 fail-closed（NVDEC 无 422 cuda 输出）。"""

    def test_422_open_fails_closed_not_fake_resident(self):
        from src.shared.media.source import MediaOpenError

        source = WindowsCudaResidentSource(str(YUV422_H264))
        with pytest.raises(MediaOpenError):
            source.open()

    def test_422_manager_falls_back_to_cpu_planar(self, monkeypatch):
        """经真实管理器路由：422 素材不得走 cuda_resident（能力门拒绝）。"""
        if not YUV422_H264.exists():
            pytest.skip("yuv422 test video missing")
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
        monkeypatch.setenv("TOPOS_FORCE_WIN_HW_DECODE", "1")
        monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)

        from src.features.edit.core.edit_gpu_decode_manager import (
            EditGpuDecodeManager,
        )

        manager = EditGpuDecodeManager()
        try:
            frame, frame_type = manager.get_frame(str(YUV422_H264), 0)
            assert frame is not None, "422 应可解码（CPU 回退）"
            assert frame_type != "cuda_resident", (
                f"4:2:2 不得走驻留路由，实际 {frame_type!r}")
            assert frame_type in ("vt_planar", "planar", "cpu"), frame_type
        finally:
            manager.close_all()


class TestSessionCapabilityIdentity:
    """W04：会话能力身份随 stats 可见（设备/驱动/来源=缓存失效条件）。"""

    def test_stats_carry_device_identity(self):
        import moderngl

        ctx = moderngl.create_standalone_context()
        try:
            session = CudaGlInteropSession(ring_size=2, max_ring_size=4)
            assert session.initialize()
            stats = session.stats
            assert stats["device_name"], "设备名不可为空"
            assert stats["driver_version"] and stats["driver_version"] > 0
            # standalone GL 上下文与 CUDA 的关联可查 → gl_context 路径；
            # 无关联（无 current GL）时为 single_cuda_device（L1 已覆盖）
            assert stats["device_source"] in (
                'gl_context', 'single_cuda_device')
            assert isinstance(stats["device_ordinal"], int)
            session.destroy()
        finally:
            ctx.release()
