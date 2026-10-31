"""B1.1 设备驻留与资源访问 PoC（计划表 B · Windows 线）。

目的（计划 §B1.1 候选 2）：在本机（RTX 4090 / PyAV 18.1.0 / FFmpeg 8 系）真机验证

1. `HWAccel(device, is_hw_owned=True)` 保留硬件帧（对照 `is_hw_owned=False`
   的自动下载基线）；
2. 保留帧的真实 surface 格式（cuda / d3d11 家族）、尺寸、device 绑定；
3. Python 侧消费接口实查：`frame.planes` 访问、`__dlpack__` / `__dlpack_device__`
   导出、`to_ndarray()` 行为——记录能力边界，不猜测；
4. 生命周期：EOF / close / 二次 open 循环后资源归还（无崩溃）；
5. 字节口径：下载会话显式计经 CPU 字节；驻留会话预期 0（以帧格式保持
   硬件面为证——PyAV 层无逐调用 hook，方法学如实声明）。

覆盖：4K H264（NV12 面）+ 4K H265 10bit（P010 面）× cuda；d3d11va 保留
（H264/H265）作为 GL interop 路线的 surface 形态证据。

fail-closed：任何阶段失败记录结构化 reason 后继续下一阶段；退出码 0=全部
阶段完成（无论能力结论如何），2=有阶段异常。

用法：
    .venv/Scripts/python.exe scripts/poc_nvdec_zerocopy.py --tag B1_20261024_run01
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import av  # noqa: E402
from av.codec.hwaccel import HWAccel, HWDeviceType  # noqa: E402

TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"
N_FRAMES = 24  # PoC 帧数：覆盖格式/生命周期即可，不做吞吐结论

RESULT: dict = {
    "schema": "poc_nvdec_zerocopy/1",
    "date": datetime.now().isoformat(timespec="seconds"),
    "pyav": av.__version__,
    "phases": [],
}
ISSUES: list[str] = []


def phase(name: str, fn) -> dict:
    entry: dict = {"phase": name, "status": "ok"}
    try:
        entry.update(fn() or {})
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "error"
        entry["error"] = f"{type(exc).__name__}: {exc}"
        ISSUES.append(f"{name}: {exc}")
        if "--debug" in sys.argv:
            traceback.print_exc()
    RESULT["phases"].append(entry)
    brief = {k: v for k, v in entry.items() if k not in ("phase", "status")}
    print(f"[{entry['status']:>5}] {name}: "
          f"{json.dumps(brief, ensure_ascii=False)[:200]}")
    return entry


def probe_codec_configs() -> dict:
    from av.codec.codec import Codec

    out = {}
    for codec_name in ("h264", "hevc"):
        cfgs = []
        for c in Codec(codec_name).hardware_configs:
            if c.is_supported:  # HW_DEVICE_CTX 方法（PyAV 唯一支持路径）
                cfgs.append({
                    "device_type": c.device_type.name,
                    "sw_format": c.format.name if c.format else None,
                })
        out[codec_name] = cfgs
    from av.codec.hwaccel import hwdevices_available

    return {"hwdevices_available": hwdevices_available(), "codec_configs": out}


def _decode_session(path: Path, device: HWDeviceType, hw_owned: bool,
                    n_frames: int) -> dict:
    accel = HWAccel(device, is_hw_owned=hw_owned)
    container = av.open(str(path), hwaccel=accel)
    stream = container.streams.video[0]
    it = container.decode(stream)

    frames = []
    downloaded_bytes = 0
    counted = 0
    fmt_names: set[str] = set()
    for frame in it:
        if counted >= n_frames:
            break
        counted += 1
        fmt_names.add(frame.format.name if frame.format else "?")
        if hw_owned:
            frames.append(frame)  # 持引用验证生命周期
        else:
            for p in frame.planes:
                downloaded_bytes += len(bytes(p))  # 基线：显式经 CPU 的字节
    meta: dict = {
        "frames": counted,
        "frame_formats": sorted(fmt_names),
        "cpu_bytes": downloaded_bytes,
        "hw_owned": hw_owned,
    }
    if hw_owned and frames:
        f0 = frames[0]
        meta["first_frame"] = {
            "format": f0.format.name if f0.format else None,
            "width": f0.width, "height": f0.height,
            "device_id": getattr(f0, "_device_id", None),
        }
        # —— 消费接口实查（逐项记录，不猜测）——
        probe: dict[str, str] = {}
        try:
            planes = f0.planes
            probe["planes"] = f"ok n={len(planes)}"
            try:
                dev = planes[0].__dlpack_device__()
                probe["dlpack_device"] = f"ok {dev}"
            except Exception as exc:  # noqa: BLE001
                probe["dlpack_device"] = f"unavailable: {type(exc).__name__}: {exc}"
            try:
                capsule = planes[0].__dlpack__()
                probe["dlpack"] = f"ok capsule={type(capsule).__name__}"
            except Exception as exc:  # noqa: BLE001
                probe["dlpack"] = f"unavailable: {type(exc).__name__}: {str(exc)[:80]}"
        except Exception as exc:  # noqa: BLE001
            probe["planes"] = f"unavailable: {type(exc).__name__}: {str(exc)[:80]}"
        try:
            f0.to_ndarray()
            probe["to_ndarray"] = "ok（若为设备内存则值得怀疑，逐帧核对）"
        except Exception as exc:  # noqa: BLE001
            probe["to_ndarray"] = f"unavailable: {type(exc).__name__}: {str(exc)[:80]}"
        meta["consumption_probe"] = probe
    container.close()
    meta["close_ok"] = True
    return meta


def run_matrix() -> dict:
    out: dict = {}
    cases = [
        ("h264_nv12_cuda_download", TEST_VIDEOS / "4K_H264.mov",
         HWDeviceType.cuda, False),
        ("h264_nv12_cuda_resident", TEST_VIDEOS / "4K_H264.mov",
         HWDeviceType.cuda, True),
        ("h264_nv12_d3d11va_resident", TEST_VIDEOS / "4K_H264.mov",
         HWDeviceType.d3d11va, True),
        ("h265_p010_cuda_resident", TEST_VIDEOS / "4K_H265.mov",
         HWDeviceType.cuda, True),
        ("h265_p010_d3d11va_resident", TEST_VIDEOS / "4K_H265.mov",
         HWDeviceType.d3d11va, True),
    ]
    for name, path, device, owned in cases:
        if not path.exists():
            out[name] = {"status": "missing_asset"}
            continue
        try:
            out[name] = _decode_session(path, device, owned, N_FRAMES)
        except Exception as exc:  # noqa: BLE001
            out[name] = {"status": "error",
                         "error": f"{type(exc).__name__}: {exc}"}
    return out


def verify_lifetime() -> dict:
    """EOF→close→二次 open 循环：资源归还与重入（计划验收项）。"""
    path = TEST_VIDEOS / "4K_H264.mov"
    rounds = []
    for r in range(2):
        accel = HWAccel(HWDeviceType.cuda, is_hw_owned=True)
        c = av.open(str(path), hwaccel=accel)
        n = sum(1 for _ in c.decode(c.streams.video[0]))
        c.close()
        rounds.append({"round": r, "decoded_all_frames": n, "eof_close_ok": True})
    return {"rounds": rounds}


# ---------------------------------------------------------------------------
# correctness：同一解码器（cuda）下载参考 vs 驻留帧经 DLPack+nvcuda 回读对拍。
# 计划 B1.1 验收："输出与同一解码器下载参考验证……性能阶段与 correctness
# 下载阶段分开"——本阶段的回读字节不计入性能口径。
# ---------------------------------------------------------------------------

def _parse_dlpack_capsule(capsule) -> dict:
    """解析 PyAV VideoPlane DLPack 胶囊。

    字段偏移为本机实证（4090/驱动 621.01/PyAV 18.1.0）：标准 DLManagedTensor
    布局读不到合法数据指针，实测布局为 data@+0（首 qword 即设备指针，内容
    回读验证为真实 luma）、device@+8、ndim@+16、dtype@+20、shape@+24、
    strides@+32、byte_offset@+40。最终以"与同解码器下载参考 md5 逐帧一致"
    为正确性判据（见 correctness_cuda）。
    """
    import ctypes

    pycapsule = ctypes.pythonapi
    pycapsule.PyCapsule_GetName.restype = ctypes.c_char_p
    pycapsule.PyCapsule_GetName.argtypes = [ctypes.py_object]
    pycapsule.PyCapsule_GetPointer.restype = ctypes.c_void_p
    pycapsule.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    name = pycapsule.PyCapsule_GetName(capsule)
    base = pycapsule.PyCapsule_GetPointer(capsule, name)

    device_type = ctypes.c_int32.from_address(base + 8).value
    device_id = ctypes.c_int32.from_address(base + 12).value
    ndim = ctypes.c_int32.from_address(base + 16).value
    dtype_bits = ctypes.c_uint8.from_address(base + 21).value
    itemsize = max(1, dtype_bits // 8)
    shape_ptr = ctypes.c_void_p.from_address(base + 24).value or 0
    strides_ptr = ctypes.c_void_p.from_address(base + 32).value
    byte_offset = ctypes.c_uint64.from_address(base + 40).value
    data = ctypes.c_void_p.from_address(base + 0).value
    shape = [ctypes.c_int64.from_address(shape_ptr + 8 * i).value
             for i in range(ndim)]
    strides = ([ctypes.c_int64.from_address(strides_ptr + 8 * i).value
                for i in range(ndim)] if strides_ptr else None)
    return {
        "capsule_name": name.decode(), "device_type": device_type,
        "device_id": device_id, "ndim": ndim, "itemsize": itemsize,
        "shape": shape, "strides": strides,
        "data_ptr": data, "byte_offset": byte_offset,
    }


class _CudaRt:
    """nvcuda 最小绑定：主上下文 + DtoH 拷贝（仅 correctness 回读用）。

    必须用带 _v2 后缀的入口：驱动 621.01 上未版本化符号（cuMemAlloc/
    cuMemcpyDtoH）签名已变（实测 rc=201 假象，见 PoC 报告）。
    """

    def __init__(self) -> None:
        import ctypes

        self.ct = ctypes
        self.cuda = ctypes.WinDLL("nvcuda.dll")
        self.cuda.cuInit.argtypes = [ctypes.c_uint]
        self.cuda.cuDevicePrimaryCtxRetain.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
        self.cuda.cuDevicePrimaryCtxRetain.restype = ctypes.c_int
        self.cuda.cuCtxSetCurrent.argtypes = [ctypes.c_void_p]
        self.cuda.cuCtxSetCurrent.restype = ctypes.c_int
        self.cuda.cuCtxSynchronize.restype = ctypes.c_int
        self.cuda.cuMemcpyDtoH_v2.argtypes = [
            ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_size_t]
        self.cuda.cuMemcpyDtoH_v2.restype = ctypes.c_int
        rc = self.cuda.cuInit(0)
        if rc != 0:
            raise RuntimeError(f"cuInit rc={rc}")
        self.ctx = ctypes.c_void_p(0)
        rc = self.cuda.cuDevicePrimaryCtxRetain(ctypes.byref(self.ctx), 0)
        if rc != 0:
            raise RuntimeError(f"cuDevicePrimaryCtxRetain rc={rc}")
        rc = self.cuda.cuCtxSetCurrent(self.ctx)
        if rc != 0:
            raise RuntimeError(f"cuCtxSetCurrent rc={rc}")

    def sync(self) -> None:
        rc = self.cuda.cuCtxSynchronize()
        if rc != 0:
            raise RuntimeError(f"cuCtxSynchronize rc={rc}")

    def dtoh(self, addr: int, nbytes: int) -> bytes:
        buf = self.ct.create_string_buffer(nbytes)
        rc = self.cuda.cuMemcpyDtoH_v2(buf, self.ct.c_ulonglong(addr), nbytes)
        if rc != 0:
            raise RuntimeError(f"cuMemcpyDtoH_v2 rc={rc}")
        return buf.raw


def _row_normalized(blob: bytes, pitch: int, row_bytes: int, rows: int) -> bytes:
    return b"".join(blob[r * pitch:r * pitch + row_bytes] for r in range(rows))


def correctness_cuda(compare_frames: int = 6) -> dict:
    """cuda 驻留帧 vs 同解码器下载参考：Y 平面行归一化 md5 对拍。

    时序约束（本机实测）：进程内把主上下文设为 current 后，PyAV 的
    `av_hwdevice_ctx_create(cuda)` 会以 -129 失败——因此解码会话与
    nvcuda 回读在时间上完全分离：先解帧+解析胶囊（纯 Python），容器
    close 后才触碰 CUDA 上下文。
    """
    import hashlib

    path = TEST_VIDEOS / "4K_H264.mov"

    # 参考会话：下载（is_hw_owned=False）
    ref_md5: list[str] = []
    ref_meta: dict = {}
    c = av.open(str(path), hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=False))
    for i, frame in enumerate(c.decode(c.streams.video[0])):
        if i >= compare_frames:
            break
        p0 = frame.planes[0]
        pitch, rb = p0.line_size, frame.width
        ref_md5.append(hashlib.md5(
            _row_normalized(bytes(p0), pitch, rb, frame.height)).hexdigest())
        ref_meta = {"pitch": pitch, "row_bytes": rb, "height": frame.height}
    c.close()

    # 驻留会话：解帧 → 解析 DLPack 胶囊（不触碰 CUDA API）→ close 容器
    resident: list[dict] = []
    c = av.open(str(path), hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=True))
    for i, frame in enumerate(c.decode(c.streams.video[0])):
        if i >= compare_frames:
            break
        cap = frame.planes[0].__dlpack__()
        info = _parse_dlpack_capsule(cap)
        resident.append({"frame": frame, "capsule": cap, "info": info})
    c.close()

    # 回读阶段：容器已关，nvcuda 上下文操作与 PyAV 解码会话时间分离
    rt = _CudaRt()
    res_md5: list[str] = []
    for item in resident:
        info = item["info"]
        rt.sync()
        pitch = info["strides"][0] * info["itemsize"]
        row_bytes = info["shape"][1] * info["itemsize"]
        rows = info["shape"][0]
        blob = rt.dtoh(info["data_ptr"] + info["byte_offset"],
                       pitch * (rows - 1) + row_bytes)
        res_md5.append(hashlib.md5(
            _row_normalized(blob, pitch, row_bytes, rows)).hexdigest())
    first_capsule = resident[0]["info"] if resident else {}

    matches = sum(1 for a, b in zip(ref_md5, res_md5) if a == b)
    return {
        "frames_compared": len(ref_md5),
        "y_plane_md5_matches": matches,
        "reference_meta": ref_meta,
        "resident_first_capsule": {
            k: first_capsule.get(k) for k in
            ("capsule_name", "device_type", "device_id", "ndim", "itemsize",
             "shape", "strides")
        },
        "byte_equal_verdict": matches == len(ref_md5),
    }


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="")
    parser.add_argument("--frames", type=int, default=N_FRAMES)
    parser.add_argument("--debug", action="store_true",
                        help="失败阶段打印完整 traceback")
    args = parser.parse_args()

    phase("capability", probe_codec_configs)
    phase("matrix", run_matrix)
    phase("lifetime", verify_lifetime)
    phase("correctness_cuda", correctness_cuda)

    RESULT["issues"] = ISSUES
    out = PROJECT_ROOT / "scripts" / (
        f"poc_nvdec_zerocopy_{args.tag}.json" if args.tag
        else "poc_nvdec_zerocopy.json")
    out.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")
    print(f"issues={len(ISSUES)}")
    return 0 if not ISSUES else 2


if __name__ == "__main__":
    raise SystemExit(main())
