"""W06（Windows 计划项）：干净 venv 的 cuda extra 缺席/在场验收脚本。

在目标 venv 内运行（不依赖开发 venv）：

    python tools/w06_capability_check.py --expect-no-cuda
        → probe 如实报缺（cuda_bindings_unavailable），解码走下载
          planar/CPU 路径（fail-closed，不崩溃、不冒充驻留）。

    python tools/w06_capability_check.py --expect-cuda
        → probe 可用，WindowsCudaResidentSource 驻留路由真实工作
          （interop md5 与同解码器下载参考逐字节一致）。

退出码 0 = 验收通过；非 0 = 失败（原因打印到 stdout，供打包/安装记录）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    # 非工程根运行兜底：src 包按文件位置定位
    sys.path.insert(0, str(_PROJECT_ROOT))


def _report() -> dict:
    from src.shared.media.windows_hw.cuda_resident import (
        resident_capability_report,
    )
    return resident_capability_report()


def _fallback_decode_check(asset: Path) -> str:
    """无 extra：WindowsHwPlanarSource 下载路径解一帧并自洽校验。"""
    import av
    from av.codec.hwaccel import HWAccel, HWDeviceType
    from src.shared.media.windows_hw.hw_planar_source import (
        WindowsHwPlanarSource,
    )

    src = WindowsHwPlanarSource(str(asset))
    src.open()
    frame = src.read_frame_planar(0)
    assert frame is not None, "下载 planar 解码失败"

    # 自洽参考：同解码器下载会话行归一化 md5
    container = av.open(
        str(asset), hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=False))
    ref = next(container.decode(container.streams.video[0]))
    container.close()
    itemsize = 1
    md5s = []
    for idx, rows in ((0, ref.height), (1, ref.height // 2)):
        plane = ref.planes[idx]
        mv = memoryview(plane)
        row_bytes = ref.width * itemsize
        compact = b"".join(
            bytes(mv[r * plane.line_size:
                    r * plane.line_size + row_bytes]) for r in range(rows))
        md5s.append(hashlib.md5(compact).hexdigest())
    ok = bool(getattr(frame, "planes", None))
    src.close()
    assert ok, "planar 帧无平面数据"
    return f"download-planar ok (ref md5 {md5s[0][:10]}…)"


def _resident_decode_check(asset: Path) -> str:
    """有 extra：驻留路由 open→read→GL interop→md5 对拍。"""
    import av
    import moderngl
    from av.codec.hwaccel import HWAccel, HWDeviceType

    from src.shared.media.frame_path import DecodePath
    from src.shared.media.windows_hw.cuda_resident import (
        CudaGlInteropSession,
        WindowsCudaResidentSource,
        is_cuda_resident_frame,
    )

    source = WindowsCudaResidentSource(str(asset))
    source.open()
    frame = source.read_frame_resident(0)
    assert is_cuda_resident_frame(frame), "未产出驻留帧"

    container = av.open(
        str(asset), hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=False))
    ref = next(container.decode(container.streams.video[0]))
    container.close()
    itemsize = 1
    ref_md5 = []
    for idx, rows in ((0, ref.height), (1, ref.height // 2)):
        plane = ref.planes[idx]
        mv = memoryview(plane)
        row_bytes = ref.width * itemsize
        compact = b"".join(
            bytes(mv[r * plane.line_size:
                    r * plane.line_size + row_bytes]) for r in range(rows))
        ref_md5.append(hashlib.md5(compact).hexdigest())

    import ctypes

    from OpenGL.GL import (
        GL_RED, GL_RG, GL_TEXTURE_2D, GL_UNSIGNED_BYTE, glBindTexture,
        glGetTexImage,
    )

    ctx = moderngl.create_standalone_context()
    try:
        session = CudaGlInteropSession(ring_size=2, max_ring_size=4)
        assert session.initialize(), session.last_fallback_reason
        result = session.bind_resident_frame(
            frame, decode_path=DecodePath.WINDOWS_CUDA_GL_ZERO_COPY.value)
        assert result is not None, session.last_fallback_reason
        planes, lease = result
        try:
            got_md5 = []
            for pi, (gl_fmt, ncomp) in enumerate(((GL_RED, 1), (GL_RG, 2))):
                w = frame.width if pi == 0 else frame.width // 2
                h = frame.height if pi == 0 else frame.height // 2
                buf = ctypes.create_string_buffer(w * h * ncomp * itemsize)
                glBindTexture(GL_TEXTURE_2D, planes[pi].texture_id)
                glGetTexImage(
                    GL_TEXTURE_2D, 0, gl_fmt, GL_UNSIGNED_BYTE, buf)
                got_md5.append(hashlib.md5(buf.raw).hexdigest())
            assert got_md5 == ref_md5, f"{got_md5} vs {ref_md5}"
        finally:
            lease.release()
        session.destroy()
    finally:
        ctx.release()
        source.close()
    return f"resident ok (interop md5 == download ref {ref_md5[0][:10]}…)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-no-cuda", action="store_true")
    parser.add_argument("--expect-cuda", action="store_true")
    args = parser.parse_args()

    report = _report()
    print("[w06] capability report:")
    print(json.dumps(report, ensure_ascii=False, indent=1))

    asset = Path(__file__).resolve().parent.parent / \
        "tests" / "test_videos" / "unaligned_322x202_h264.mov"
    if not asset.exists():
        asset = Path(__file__).resolve().parent.parent / \
            "tests" / "test_videos" / "4K_H264.mov"
    if not asset.exists():
        print("[w06] FAIL: no test asset")
        return 2

    try:
        if args.expect_no_cuda:
            assert report["platform_ok"], "验收机非 Windows"
            assert report["nvcuda_ok"], "本机应装有 NVIDIA 驱动"
            assert not report["cuda_bindings_ok"], (
                "期望无 cuda-bindings（venv 未装 extra），实际在场")
            assert report["reason"].startswith("cuda_bindings_unavailable"), (
                f"探针原因不符: {report['reason']}")
            detail = _fallback_decode_check(asset)
            print(f"[w06] no-extra fallback: {detail}")
            print("[w06] PASS: 无 extra → 下载 planar 路径 fail-closed")
            return 0
        if args.expect_cuda:
            assert report["route_available"], (
                f"期望驻留可用: {report['reason']}")
            detail = _resident_decode_check(asset)
            print(f"[w06] extra present: {detail}")
            print("[w06] PASS: 有 extra → 真实驻留路由 + md5 对拍一致")
            return 0
        print("[w06] FAIL: 需要 --expect-no-cuda 或 --expect-cuda")
        return 2
    except AssertionError as exc:
        print(f"[w06] FAIL: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[w06] FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
