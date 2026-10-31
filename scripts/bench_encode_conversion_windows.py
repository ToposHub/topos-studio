#!/usr/bin/env python3
"""W6 导出转换层分环节基准（#2 优化前置测量，本机 AMD RX 6800）。

对 4K 10bit 生产形态帧（bgr48le packed uint16，来源 4K ProRes 422 HQ）
逐环节测量，回答「AMF 10bit 端到端 22.6fps 的 44ms/帧里，转换占多少、
编码占多少、哪些环节值得并行化」：

- A. 直填快路径本体：plane.update(memoryview)（BGR numpy → bgr48le 帧）
- B. swscale reformat bgr48le→yuv420p10le（CPU x265 路径的目标格式）
- C. swscale reformat bgr48le→p010le（AMF 10bit 路径的目标格式，单线程）
- D. swscale reformat bgr48le→p010le（K 线程分带并行，含拼装开销）
- E. 端到端 hevc_amf 10bit（pipelined_conversion，对照基线 22.6fps）
- F. 端到端 libx265 10bit（kill switch 对照）

分带并行正确性：4:2:0 色度 box 2x2 只依赖行对 (2i, 2i+1)，偶数行高分带
内自洽 → 与单线程输出逐平面 bit 级一致（脚本内断言）。

用法：.venv/Scripts/python.exe scripts/bench_encode_conversion_windows.py
产物：tests/media/results/encode_conversion_breakdown_amd.{md,json}（P9C-02 配对）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SRC = PROJECT_ROOT / "tests" / "test_videos" / "4K_prores422HQ.mov"
RESULTS_DIR = PROJECT_ROOT / "tests" / "media" / "results"
N_WARMUP = 12
N_TIMED = 48
BAND_COUNTS = (2, 4, 8)


def predecode(n: int) -> list:
    """预解码为生产形态的 bgr48le packed DecodedFrame（与导出渲染输出同构）。"""
    import numpy as np

    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    src = FFmpegMediaSource(str(SRC))
    src.open()
    frames = []
    while len(frames) < n:
        f = src.read_next_frame()
        if f is None:
            break
        label = 'bgr48le' if f.data.dtype.itemsize > 1 else 'bgr24'
        frames.append(type(f)(
            frame_index=f.frame_index, pts=f.pts, time_seconds=f.time_seconds,
            data=np.ascontiguousarray(np.flip(f.data, axis=-1)),
            pixel_format=label, width=f.width, height=f.height,
        ))
    src.close()
    return frames


def stage_direct_fill(frame) -> None:
    """A：BGR 直填快路径本体（绕过 from_ndarray 跨距拷贝）。"""
    import av

    cand = av.VideoFrame(frame.width, frame.height, 'bgr48le')
    plane = cand.planes[0]
    view = memoryview(frame.data)
    if plane.line_size == frame.data.shape[1] * 6 and view.nbytes == plane.buffer_size:
        plane.update(view)
    else:
        raise RuntimeError("direct fill fast path precondition failed")


def stage_reformat(frame, dst: str) -> None:
    """B/C：直填 + swscale reformat 到目标格式（单线程）。"""
    import av

    cand = av.VideoFrame(frame.width, frame.height, 'bgr48le')
    plane = cand.planes[0]
    plane.update(memoryview(frame.data))
    cand.reformat(format=dst)


def stage_reformat_banded(frame, dst: str, k: int) -> None:
    """D：K 线程分带并行 reformat + 拼装。

    每带独立「直填子帧 → reformat」，主线程按平面字节拼装目标帧。
    """
    import av
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor

    h, w = frame.height, frame.width
    band_h = (h + k - 1) // k
    if band_h % 2:
        band_h += 1  # 4:2:0 行对自洽 + 2K 整除
    bands = []
    for b in range(k):
        r0 = b * band_h
        if r0 >= h:
            break
        r1 = min(r0 + band_h, h)
        sub = frame.data[r0:r1]
        bands.append((r0, r1 - r0, sub))

    def convert_band(sub) -> "av.VideoFrame":
        cand = av.VideoFrame(w, sub.shape[0], 'bgr48le')
        cand.planes[0].update(memoryview(np.ascontiguousarray(sub)))
        return cand.reformat(format=dst)

    with ThreadPoolExecutor(max_workers=k) as pool:
        converted = list(pool.map(convert_band, [b[2] for b in bands]))

    # 拼装：目标帧平面按行段字节拷贝
    out = av.VideoFrame(w, h, dst)
    itemsize = 2 if ('10' in dst or '16' in dst or 'p0' in dst[:2]) else 1
    for pi in range(len(out.planes)):
        out_plane = out.planes[pi]
        row_bytes = out_plane.line_size
        buf = np.zeros((out_plane.buffer_size // row_bytes, row_bytes),
                       dtype=np.uint8)
        row_scale = 1 if pi == 0 else 2  # 4:2:0 色度平面行号减半
        offset_rows = 0
        for (r0, rows, _), conv in zip(bands, converted):
            cplane = conv.planes[pi]
            src_rows = rows // row_scale
            src_row_bytes = cplane.line_size
            src = np.frombuffer(memoryview(cplane), dtype=np.uint8)
            src = src[: src_rows * src_row_bytes].reshape(src_rows, src_row_bytes)
            dst_rows = r0 // row_scale
            copy_bytes = min(src_row_bytes, row_bytes)
            buf[dst_rows: dst_rows + src_rows, :copy_bytes] = \
                src[:, :copy_bytes]
            offset_rows += src_rows
        out_plane.update(buf.tobytes())


def time_stage(name: str, fn, frames, n: int) -> dict:
    for f in frames[:N_WARMUP]:
        fn(f)
    t0 = time.perf_counter()
    for f in frames[N_WARMUP:N_WARMUP + n]:
        fn(f)
    dt = (time.perf_counter() - t0) / n * 1000
    print(f"  {name:46s} {dt:7.2f} ms/帧")
    return {"stage": name, "ms_per_frame": round(dt, 3)}


def e2e_arm(label: str, frames, codec: str, pix_fmt: str, force_cpu: bool,
            convert_workers: int = 1):
    import numpy as np

    from src.shared.export.video_encoder import FFmpegEncoderConfig, FFmpegVideoEncoder
    from src.shared.media.metadata import Rational

    if force_cpu:
        os.environ["TOPOS_DISABLE_WIN_HW_ENCODE"] = "1"
    out = tempfile.mktemp(suffix=".mp4")
    config = FFmpegEncoderConfig(
        codec=codec, width=3840, height=2160, fps=Rational(25, 1),
        pix_fmt=pix_fmt, crf=20, preset="medium", gop_size=50,
        threads=0, pipelined_conversion=True,
        convert_workers=convert_workers,
    )
    enc = FFmpegVideoEncoder(out, config)
    enc.open()
    total = 0
    t0 = time.perf_counter()
    for i, frame in enumerate(frames):
        enc.encode_frame(frame)
        total += 1
        if i == N_WARMUP - 1:
            t0 = time.perf_counter()
    enc.close()
    elapsed = time.perf_counter() - t0
    if force_cpu:
        os.environ.pop("TOPOS_DISABLE_WIN_HW_ENCODE", None)
    fps = (total - N_WARMUP) / elapsed
    used_amf = enc._using_amf
    reason = enc.hw_fallback_reason
    print(f"  {label:46s} {'AMF' if used_amf else 'CPU':3s} {fps:7.1f} fps")
    os.remove(out)
    return {"arm": label, "fps": round(fps, 1), "amf": used_amf,
            "fallback_reason": reason}


def verify_banded_equal(frames) -> bool:
    """分带并行与单线程输出逐平面 bit 级一致（4:2:0 行对带内自洽）。"""
    import numpy as np

    f = frames[0]
    single = av_frame_of(f, 'p010le')
    for k in BAND_COUNTS:
        banded = banded_frame_of(f, 'p010le', k)
        for pi in range(len(single.planes)):
            a = np.frombuffer(memoryview(single.planes[pi]), dtype=np.uint8)
            b = np.frombuffer(memoryview(banded.planes[pi]), dtype=np.uint8)
            if not np.array_equal(a, b):
                print(f"  [FAIL] k={k} 平面 {pi} 不一致")
                return False
    print(f"  [PASS] 分带拼装与单线程逐平面 bit 级一致（k={BAND_COUNTS}）")
    return True


def av_frame_of(frame, dst: str):
    import av

    cand = av.VideoFrame(frame.width, frame.height, 'bgr48le')
    cand.planes[0].update(memoryview(frame.data))
    return cand.reformat(format=dst)


def banded_frame_of(frame, dst: str, k: int):
    import av
    import numpy as np

    h, w = frame.height, frame.width
    band_h = ((h + k - 1) // k + 1) // 2 * 2
    bands = []
    for b in range(k):
        r0 = b * band_h
        if r0 >= h:
            break
        r1 = min(r0 + band_h, h)
        bands.append((r0, r1 - r0, frame.data[r0:r1]))

    def convert_band(sub):
        cand = av.VideoFrame(w, sub.shape[0], 'bgr48le')
        cand.planes[0].update(memoryview(np.ascontiguousarray(sub)))
        return cand.reformat(format=dst)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=k) as pool:
        converted = list(pool.map(convert_band, [b[2] for b in bands]))

    out = av.VideoFrame(w, h, dst)
    for pi in range(len(out.planes)):
        out_plane = out.planes[pi]
        row_bytes = out_plane.line_size
        buf = np.zeros((out_plane.buffer_size // row_bytes, row_bytes),
                       dtype=np.uint8)
        row_scale = 1 if pi == 0 else 2
        for (r0, rows, _), conv in zip(bands, converted):
            cplane = conv.planes[pi]
            src_rows = rows // row_scale
            src_row_bytes = cplane.line_size
            src = np.frombuffer(memoryview(cplane), dtype=np.uint8)
            src = src[: src_rows * src_row_bytes].reshape(src_rows, src_row_bytes)
            dst_rows = r0 // row_scale
            copy_bytes = min(src_row_bytes, row_bytes)
            buf[dst_rows: dst_rows + src_rows, :copy_bytes] = src[:, :copy_bytes]
        out_plane.update(buf.tobytes())
    return out


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    import av

    print(f"预解码 {N_WARMUP + N_TIMED} 帧 4K（bgr48le 生产形态）...")
    frames = predecode(N_WARMUP + N_TIMED)
    if len(frames) < N_WARMUP + N_TIMED:
        print(f"[FAIL] 素材可解码帧不足：{len(frames)}")
        return 1
    print(f"预解码完成 {len(frames)} 帧；分环节测量（稳态 {N_TIMED} 帧）：\n")

    results = {"date": datetime.now().isoformat(timespec="seconds"),
               "machine": "AMD RX 6800 + Win11 26200",
               "source": SRC.name, "stages": [], "e2e": []}

    n = min(N_TIMED, len(frames) - N_WARMUP)
    results["stages"].append(time_stage(
        "A 直填 plane.update（bgr48le 本体）", stage_direct_fill, frames, n))
    results["stages"].append(time_stage(
        "B reformat bgr48le→yuv420p10le（x265 路径）",
        lambda f: stage_reformat(f, 'yuv420p10le'), frames, n))
    results["stages"].append(time_stage(
        "C reformat bgr48le→p010le（AMF 10bit，单线程）",
        lambda f: stage_reformat(f, 'p010le'), frames, n))
    for k in BAND_COUNTS:
        results["stages"].append(time_stage(
            f"D reformat→p010le 分带并行 k={k}（含拼装）",
            lambda f, k=k: stage_reformat_banded(f, 'p010le', k), frames, n))

    print("\n分带正确性：")
    ok_bands = verify_banded_equal(frames)
    results["banded_bitwise_equal"] = ok_bands

    print("\n端到端对照（pipelined_conversion，稳态口径）：")
    results["e2e"].append(e2e_arm(
        "E hevc_amf 10bit（单线程转换）", frames, "hevc", "yuv420p10le", False))
    results["e2e"].append(e2e_arm(
        "G hevc_amf 10bit（3 路帧级并行转换）", frames, "hevc", "yuv420p10le",
        False, convert_workers=3))
    results["e2e"].append(e2e_arm(
        "H h264_amf 8bit（3 路帧级并行转换）", frames, "h264", "yuv420p",
        False, convert_workers=3))
    results["e2e"].append(e2e_arm(
        "F libx265 10bit（kill switch 对照）", frames, "hevc", "yuv420p10le", True))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / "encode_conversion_breakdown_amd.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    print(f"\nwritten: {json_path}")
    return 0 if ok_bands else 1


if __name__ == "__main__":
    raise SystemExit(main())
