#!/usr/bin/env python3
"""M8 V1 总验收矩阵基准（计划 §13/§14 M8，2026-08-31）。

公平对照原则（§13.1/§13.2）：
  - 同源：1080p/4K 均取自 tests/test_videos 的 ProRes422HQ 真实素材解码帧
    （yuv422p10le，PyAV threads=8 解码，值确定性与线程无关）；
  - 同线程：Topos（set_slice_threads/产品默认 8）vs prores_ks / dnxhr
    （PyAV thread_count=8）；
  - 同码率锚点：prores_ks profile=hq 的 120 帧编码码率；Topos plain 以
    qp 校准至 ±5%；DNxHR HQX 为固定码率 profile，按其实测码率报告；
  - 预解码进内存口径（§4.2 精确法）：编码计时不含源解码，12 帧 warmup
    丢弃；解码计时含 demux/packet 读取（完整产品路径）；
  - "产品路径"：Topos 解码 = ToposMediaSource.read_next_frame/read_frame，
    Topos 编码 = ToposVideoEncoder.encode_frame（planar 直通）。

子命令（各自独立落盘 bench_out/m8/<phase>_<res>.json）：
  gen     生成三份同源资产 + 编码计时矩阵（prores/dnxhr/topos plain 校准
          + sized 同码率 + 1/2/4/8 线程 parity 断言）
  decode  顺序/随机 × avg/p95 矩阵 + RSS 稳态 + CPU 核均利用
  quality PSNR/SSIM 分通道 vs 源 + Topos/ProRes 多代退化
  cold    子进程隔离的进程级 cold 首帧（3 轮 median）

用法：
    .venv/bin/python tools/bench_m8_matrix.py gen --res 1080p
    .venv/bin/python tools/bench_m8_matrix.py decode --res 4k
    .venv/bin/python tools/bench_m8_matrix.py quality --res 4k
    .venv/bin/python tools/bench_m8_matrix.py cold --res 1080p
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

OUT_DIR = PROJECT_ROOT / "bench_out" / "m8"
THREADS = 8
FPS = 25
WARMUP = 12
N_FRAMES = 120
MULTIGEN_FRAMES = 60
SSIM_FRAMES = 60

SOURCES = {
    "1080p": ("tests/test_videos/2K_prores422HQ.mov", 1920, 1080),
    "4k": ("tests/test_videos/4K_prores422HQ.mov", 3840, 2160),
}


# ----------------------------------------------------------------------------
# 源帧（同源基准）：每次调用重新解码（内存只保留单帧；值与线程无关）
# ----------------------------------------------------------------------------

def source_frames(res: str, n: int = N_FRAMES):
    """生成器：yield (y, u, v) uint16 LE 平面（tight copy）。"""
    import av

    path, w, h = SOURCES[res]
    path = str(PROJECT_ROOT / path)
    c = av.open(path)
    st = next(s for s in c.streams if s.type == "video")
    got = 0
    for frame in c.decode(st):
        if got >= n:
            break
        yield planes_from_frame(frame, w, h)
        got += 1
    c.close()


def planes_from_frame(frame, w: int, h: int):
    """PyAV VideoFrame → (y, u, v) uint16 数组（按 line_size 步长取前 w 列）。"""
    cw = w // 2
    out = []
    for i, (pw, ph) in enumerate(((w, h), (cw, h), (cw, h))):
        plane = frame.planes[i]
        stride = plane.line_size // 2
        buf = np.frombuffer(plane, dtype="<u2", count=stride * ph)
        arr = buf.reshape(ph, stride)[:, :pw]
        out.append(np.ascontiguousarray(arr))
    return tuple(out)


def topos_frame_obj(idx: int, planes, w: int, h: int):
    from src.shared.media.frame import DecodedFrame

    return DecodedFrame(
        frame_index=idx, pts=idx, time_seconds=idx / FPS,
        planes=planes, pixel_format="yuv422p10le",
        width=w, height=h,
    )


def write_json(name: str, data: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"[write] {path}")


def stats_of(times_ms: list[float]) -> dict:
    s = sorted(times_ms)
    n = len(s)
    return {
        "n": n,
        "avg_ms": round(statistics.fmean(s), 3),
        "p50_ms": round(statistics.median(s), 3),
        "p95_ms": round(s[int(0.95 * (n - 1))], 3),
        "p99_ms": round(s[int(0.99 * (n - 1))], 3),
        "max_ms": round(s[-1], 3),
    }


# ----------------------------------------------------------------------------
# gen：资产 + 编码矩阵
# ----------------------------------------------------------------------------

def encode_prores(out: str, frames_iter, w: int, h: int) -> dict:
    import av

    times, total = [], 0
    oc = av.open(out, "w")
    st = oc.add_stream("prores_ks", rate=FPS)
    cc = st.codec_context
    cc.width, cc.height, cc.pix_fmt = w, h, "yuv422p10le"
    cc.time_base = Fraction(1, FPS)
    cc.thread_count = THREADS
    cc.profile = "hq"
    for i, (y, u, v) in enumerate(frames_iter):
        f = av.VideoFrame(w, h, "yuv422p10le")
        f.planes[0].update(y.tobytes())
        f.planes[1].update(u.tobytes())
        f.planes[2].update(v.tobytes())
        t0 = time.perf_counter()
        pkts = list(cc.encode(f))
        dt = (time.perf_counter() - t0) * 1000.0
        for p in pkts:
            oc.mux(p)
        if i >= WARMUP:
            times.append(dt)
            total += sum(p.size for p in pkts)
    for p in cc.encode(None):
        oc.mux(p)
    oc.close()
    # mux 包装后取真实文件码率（计时只含 encode，码率取整文件更公平）
    fsize = os.path.getsize(out)
    n_meas = N_FRAMES - WARMUP
    return {
        "path": out,
        "encode": stats_of(times),
        "avg_frame_bytes(measured)": round(total / n_meas, 1),
        "file_bytes": fsize,
        "bitrate_mbps(file,120f)": round(fsize * 8 * FPS / N_FRAMES / 1e6, 2),
    }


def encode_dnxhr(out: str, frames_iter, w: int, h: int) -> dict:
    import av

    times, total = [], 0
    oc = av.open(out, "w")
    st = oc.add_stream("dnxhd", rate=FPS)
    cc = st.codec_context
    cc.width, cc.height, cc.pix_fmt = w, h, "yuv422p10le"
    cc.time_base = Fraction(1, FPS)
    cc.thread_count = THREADS
    cc.options = {"profile": "dnxhr_hqx"}
    for i, (y, u, v) in enumerate(frames_iter):
        f = av.VideoFrame(w, h, "yuv422p10le")
        f.planes[0].update(y.tobytes())
        f.planes[1].update(u.tobytes())
        f.planes[2].update(v.tobytes())
        t0 = time.perf_counter()
        pkts = list(cc.encode(f))
        dt = (time.perf_counter() - t0) * 1000.0
        for p in pkts:
            oc.mux(p)
        if i >= WARMUP:
            times.append(dt)
            total += sum(p.size for p in pkts)
    for p in cc.encode(None):
        oc.mux(p)
    oc.close()
    fsize = os.path.getsize(out)
    n_meas = N_FRAMES - WARMUP
    return {
        "path": out,
        "encode": stats_of(times),
        "avg_frame_bytes(measured)": round(total / n_meas, 1),
        "file_bytes": fsize,
        "bitrate_mbps(file,120f)": round(fsize * 8 * FPS / N_FRAMES / 1e6, 2),
    }


def _topos_encoder(out: str, w: int, h: int, qp: int):
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.metadata import Rational

    cfg = FFmpegEncoderConfig(
        codec="topos", width=w, height=h, fps=Rational(FPS, 1),
        pix_fmt="yuv422p10le", profile="standard", crf=qp,
    )
    return ToposVideoEncoder(out, cfg, slice_threads=THREADS)


def _native_frame_cfg(w: int, h: int, qp: int):
    """native 编码配置（与产品 standard 档同 profile/qmatrix/色彩域）。"""
    from src.shared.codec.topos_binding import (
        TOPOS_CODEC_ABI_VERSION, _CFrameConfig,
    )
    import ctypes

    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.qp_base = qp
    fc.qmatrix_id = 1
    fc.profile = 3          # standard
    fc.pixel_format = 0     # 4:2:2
    fc.bit_depth = 10
    fc.alpha_mode = 0
    fc.alpha_bit_depth = 0
    fc.alpha_premultiplied = 0
    fc.color_range = 1      # limited（产品 SDR 默认）
    fc.color_primaries = 1  # bt709
    fc.color_transfer = 1   # bt709
    fc.color_matrix = 1     # bt709
    fc.sar_num, fc.sar_den = 1, 1
    return fc


def topos_qp_calibration(w: int, h: int, target_bytes: float) -> tuple[int, list]:
    """24 帧 native plain 编码 packet 尺寸 → 括号搜索 qp（尺寸对 qp 单调不增）。

    口径与 prores 锚点一致（packet 字节，不含容器开销）。
    """
    from src.shared.codec.topos_binding import ToposCodec

    codec = ToposCodec()
    prev = codec.slice_threads()
    codec.set_slice_threads(THREADS)
    res = _res_of(w, h)
    results = []

    def avg_size(qp: int) -> float:
        fc = _native_frame_cfg(w, h, qp)
        sizes = []
        # 等距采样全 120 帧（前 24 帧复杂度不代表性——曾致整文件偏差 +12%）
        for i, (y, u, v) in enumerate(source_frames(res, N_FRAMES)):
            if i % 5 != 0:
                continue
            pkt, _st = codec.encode_frame(
                fc, [y.tobytes(), u.tobytes(), v.tobytes()])
            sizes.append(len(pkt))
        s = float(np.mean(sizes[1:]))  # 丢首帧预热
        results.append({"qp": qp, "avg_bytes": round(s, 1)})
        return s

    probe_qp = 20
    s = avg_size(probe_qp)
    for _ in range(12):
        if abs(s - target_bytes) / target_bytes <= 0.05:
            break
        if s > target_bytes:
            probe_qp = min(63, probe_qp + 4)
        else:
            probe_qp = max(0, probe_qp - 4)
        s = avg_size(probe_qp)
    # 邻域细化 ±3
    center = min(
        (abs(r["avg_bytes"] - target_bytes), r["qp"]) for r in results
    )[1]
    for q in range(max(0, center - 3), min(63, center + 3) + 1):
        if all(r["qp"] != q for r in results):
            avg_size(q)
    best = min(
        (abs(r["avg_bytes"] - target_bytes) / target_bytes, r["qp"])
        for r in results
    )[1]
    codec.set_slice_threads(prev)
    return best, results


def _res_of(w: int, h: int) -> str:
    return "4k" if w >= 3840 else "1080p"


def encode_topos(out: str, w: int, h: int, qp: int, frames_iter) -> dict:
    enc = _topos_encoder(out, w, h, qp)
    enc.open()
    times = []
    for i, planes in enumerate(frames_iter):
        frame = topos_frame_obj(i, planes, w, h)
        t0 = time.perf_counter()
        enc.encode_frame(frame)
        times.append((time.perf_counter() - t0) * 1000.0)
    enc.close()
    fsize = os.path.getsize(out)
    return {
        "path": out,
        "qp": qp,
        "encode": stats_of(times[WARMUP:]),
        "file_bytes": fsize,
        "bitrate_mbps(file,120f)": round(fsize * 8 * FPS / N_FRAMES / 1e6, 2),
    }


def bench_topos_sized(w: int, h: int, target_frame_bytes: int) -> dict:
    """native encode_sized 同码率口径（目标 = prores 实测平均帧字节数）。"""
    from src.shared.codec.topos_binding import ToposCodec

    codec = ToposCodec()
    prev = codec.slice_threads()
    codec.set_slice_threads(THREADS)
    fc = _native_frame_cfg(w, h, 20)
    times, sizes, qps = [], [], []
    for i, (y, u, v) in enumerate(source_frames(_res_of(w, h), N_FRAMES)):
        pl = [y.tobytes(), u.tobytes(), v.tobytes()]
        t0 = time.perf_counter()
        pkt, st, qp_used = codec.encode_sized(fc, pl, target_frame_bytes)
        times.append((time.perf_counter() - t0) * 1000.0)
        sizes.append(len(pkt))
        qps.append(qp_used)
    codec.set_slice_threads(prev)
    return {
        "target_frame_bytes": target_frame_bytes,
        "encode": stats_of(times[WARMUP:]),
        "avg_frame_bytes": round(float(np.mean(sizes[WARMUP:])), 1),
        "rate_error_pct": round(
            (float(np.mean(sizes[WARMUP:])) - target_frame_bytes)
            / target_frame_bytes * 100.0, 2),
        "qp_histogram": {
            str(q): qps[WARMUP:].count(q) for q in sorted(set(qps[WARMUP:]))
        },
    }


def thread_parity(w: int, h: int) -> dict:
    """1/2/4/8 线程：编码 packet 逐字节一致 + 解码像素一致（§16 门）。"""
    from src.shared.codec.topos_binding import ToposCodec

    codec = ToposCodec()
    frames = []
    for i, planes in enumerate(source_frames(_res_of(w, h), WARMUP + 1)):
        frames.append(planes)
    y, u, v = frames[WARMUP]
    pl = [y.tobytes(), u.tobytes(), v.tobytes()]
    fc = _native_frame_cfg(w, h, 20)

    ref_pkt = None
    ref_frame = None
    result = {}
    for nth in (1, 2, 4, 8):
        codec.set_slice_threads(nth)
        pkt, _ = codec.encode_frame(fc, pl)
        entry = {"packet_bytes": len(pkt), "encode_match": None,
                 "decode_match": None}
        if ref_pkt is None:
            ref_pkt, entry["encode_match"] = pkt, True
        else:
            entry["encode_match"] = (pkt == ref_pkt)
        frame = codec.decode(pkt)
        key = f"threads_{nth}"
        if ref_frame is None:
            ref_frame = frame
            entry["decode_match"] = True
        else:
            entry["decode_match"] = all(
                np.array_equal(a, b) for a, b in zip(frame.planes, ref_frame.planes)
            )
        result[key] = entry
    codec.set_slice_threads(THREADS)
    return result


def cmd_gen(res: str) -> None:
    src_path, w, h = SOURCES[res]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_prores = str(OUT_DIR / f"{res}_proreshq.mov")
    out_dnxhr = str(OUT_DIR / f"{res}_dnxhrhqx.mov")

    print(f"[gen {res}] prores_ks hq ...")
    prores = encode_prores(out_prores, source_frames(res), w, h)
    print(json.dumps(prores, indent=2))

    print(f"[gen {res}] dnxhr hqx ...")
    dnxhr = encode_dnxhr(out_dnxhr, source_frames(res), w, h)
    print(json.dumps(dnxhr, indent=2))

    print(f"[gen {res}] topos qp calibration ...")
    target = prores["avg_frame_bytes(measured)"]
    qp, calib = topos_qp_calibration(w, h, target)
    out_topos = str(OUT_DIR / f"{res}_topos_qp{qp}.mov")
    print(f"[gen {res}] topos plain qp={qp} ...")
    topos = encode_topos(out_topos, w, h, qp, source_frames(res))
    dev = (topos["bitrate_mbps(file,120f)"]
           - prores["bitrate_mbps(file,120f)"]) / prores["bitrate_mbps(file,120f)"]
    topos["bitrate_dev_vs_prores_pct"] = round(dev * 100.0, 2)
    # 整文件偏差超 ±5% → 按局部斜率换 qp 重编（最多 2 次）
    for _retry in range(2):
        if abs(topos["bitrate_dev_vs_prores_pct"]) <= 5.0:
            break
        direction = 1 if dev > 0 else -1
        nxt = max(0, min(63, qp + direction))
        if nxt == qp:
            break
        print(f"[gen {res}] dev={topos['bitrate_dev_vs_prores_pct']}% "
              f"→ retry qp={nxt}")
        qp = nxt
        out_topos = str(OUT_DIR / f"{res}_topos_qp{qp}.mov")
        Path(str(OUT_DIR / f"{res}_topos_qp{topos['qp']}.mov")).unlink(missing_ok=True)
        topos = encode_topos(out_topos, w, h, qp, source_frames(res))
        dev = (topos["bitrate_mbps(file,120f)"]
               - prores["bitrate_mbps(file,120f)"]) / prores["bitrate_mbps(file,120f)"]
        topos["bitrate_dev_vs_prores_pct"] = round(dev * 100.0, 2)

    print(f"[gen {res}] topos sized @ target={target}B ...")
    sized = bench_topos_sized(w, h, int(target))

    print(f"[gen {res}] thread parity 1/2/4/8 ...")
    parity = thread_parity(w, h)

    write_json(f"gen_{res}.json", {
        "res": res, "source": src_path, "frames": N_FRAMES,
        "threads": THREADS, "warmup_discarded": WARMUP,
        "prores": prores, "dnxhr": dnxhr, "topos": topos,
        "topos_calibration": calib, "topos_sized": sized,
        "thread_parity": parity,
    })


# ----------------------------------------------------------------------------
# decode：顺序/随机矩阵 + RSS + CPU
# ----------------------------------------------------------------------------

def _decode_topos(path: str, n: int) -> dict:
    import psutil

    from src.shared.media.topos_source import ToposMediaSource

    proc = psutil.Process(os.getpid())
    src = ToposMediaSource(path)
    src.open()
    # 顺序
    for _ in range(WARMUP):
        src.read_next_frame()
    times, rss_seq = [], []
    cpu0, wall0 = proc.cpu_times(), time.perf_counter()
    for _ in range(100):
        t0 = time.perf_counter()
        src.read_next_frame()
        times.append((time.perf_counter() - t0) * 1000.0)
        rss_seq.append(proc.memory_info().rss)
    wall_seq = time.perf_counter() - wall0
    cpu_seq = proc.cpu_times()
    # 随机（确定性伪随机索引；Topos seek = 游标移动，O(1)）
    rng = np.random.RandomState(20260831)
    idxs = rng.choice(100 + WARMUP + min(8, max(0, n - 100 - WARMUP)),
                      size=100, replace=False)
    rtimes = []
    for idx in idxs:
        t0 = time.perf_counter()
        src.read_frame(int(idx))
        rtimes.append((time.perf_counter() - t0) * 1000.0)
    src.close()
    rss_warm = rss_seq[20:]
    mid = len(rss_warm) // 2
    cores = ((cpu_seq.user - cpu0.user) + (cpu_seq.system - cpu0.system)) / wall_seq
    return {
        "sequential": stats_of(times),
        "random": stats_of(rtimes),
        "rss_after_warm_mb": round(rss_warm[0] / 1e6, 1),
        "rss_p50_mb": round(float(np.median(rss_warm)) / 1e6, 1),
        "rss_max_mb": round(max(rss_warm) / 1e6, 1),
        "rss_drift_last80_mb": round((rss_warm[-1] - rss_warm[0]) / 1e6, 2),
        "rss_drift_late_half_mb": round((rss_warm[-1] - rss_warm[mid]) / 1e6, 2),
        "cpu_cores_avg": round(cores, 2),
    }


def _decode_pyav(path: str, w: int, h: int) -> dict:
    import av

    import psutil

    proc = psutil.Process(os.getpid())
    c = av.open(path)
    st = next(s for s in c.streams if s.type == "video")
    st.codec_context.thread_count = THREADS
    st.codec_context.thread_type = "AUTO"

    seq_iter = c.decode(st)  # PyAV 17：流级生成器（demux+decode 一体）

    def one_decode():
        return planes_from_frame(next(seq_iter), w, h)

    for _ in range(WARMUP):
        one_decode()
    times, rss_seq = [], []
    cpu0, wall0 = proc.cpu_times(), time.perf_counter()
    for _ in range(100):
        t0 = time.perf_counter()
        one_decode()
        times.append((time.perf_counter() - t0) * 1000.0)
        rss_seq.append(proc.memory_info().rss)
    wall_seq = time.perf_counter() - wall0
    cpu_seq = proc.cpu_times()

    # 随机 seek（intra：seek 到目标 pts，any_frame 精确取帧）
    rng = np.random.RandomState(20260831)
    idxs = rng.choice(100 + WARMUP + 8, size=100, replace=False)
    rtimes, pts_mismatch = [], 0
    tb = st.time_base  # Fraction；帧 pts = idx/fps 换算到流时基
    for idx in idxs:
        target = int(round(idx * Fraction(1, FPS) / tb))
        t0 = time.perf_counter()
        c.seek(target, stream=st, any_frame=True)
        got = None
        for frame in c.decode(st):
            got = frame
            break
        planes_from_frame(got, w, h)
        rtimes.append((time.perf_counter() - t0) * 1000.0)
        if int(got.pts) != target:
            pts_mismatch += 1
    c.close()
    rss_warm = rss_seq[20:]
    mid = len(rss_warm) // 2
    cores = ((cpu_seq.user - cpu0.user) + (cpu_seq.system - cpu0.system)) / wall_seq
    return {
        "sequential": stats_of(times),
        "random": stats_of(rtimes),
        "rss_after_warm_mb": round(rss_warm[0] / 1e6, 1),
        "rss_p50_mb": round(float(np.median(rss_warm)) / 1e6, 1),
        "rss_max_mb": round(max(rss_warm) / 1e6, 1),
        "rss_drift_last80_mb": round((rss_warm[-1] - rss_warm[0]) / 1e6, 2),
        "rss_drift_late_half_mb": round((rss_warm[-1] - rss_warm[mid]) / 1e6, 2),
        "cpu_cores_avg": round(cores, 2),
        "random_seek_pts_mismatch": pts_mismatch,
    }


def cmd_decode(res: str) -> None:
    src_path, w, h = SOURCES[res]
    gen = json.loads((OUT_DIR / f"gen_{res}.json").read_text())
    topos_path = gen["topos"]["path"]

    print(f"[decode {res}] topos ...")
    topos = _decode_topos(topos_path, N_FRAMES)
    print(json.dumps(topos, indent=2))
    print(f"[decode {res}] prores ...")
    prores = _decode_pyav(gen["prores"]["path"], w, h)
    print(json.dumps(prores, indent=2))
    print(f"[decode {res}] dnxhr ...")
    dnxhr = _decode_pyav(gen["dnxhr"]["path"], w, h)
    print(json.dumps(dnxhr, indent=2))

    write_json(f"decode_{res}.json", {
        "res": res, "threads": THREADS, "frames_seq": 100,
        "topos": topos, "prores": prores, "dnxhr": dnxhr,
    })


# ----------------------------------------------------------------------------
# quality：PSNR/SSIM vs 源 + 多代
# ----------------------------------------------------------------------------

def _sse(a: np.ndarray, b: np.ndarray) -> float:
    d = a.astype(np.int64) - b.astype(np.int64)
    return float(np.sum(d * d))


def psnr_from_sse(sse: float, n: int, peak: int = 1023) -> float:
    if sse <= 0:
        return float("inf")
    mse = sse / n
    return 10.0 * np.log10((peak + 1) ** 2 / mse)


def ssim_plane(a: np.ndarray, b: np.ndarray, peak: float = 1023.0) -> float:
    """标准 SSIM（gaussian 11×11 σ=1.5，Wang et al.），float64。"""
    from scipy.ndimage import gaussian_filter

    a = a.astype(np.float64)
    b = b.astype(np.float64)
    radius = 5  # truncate=3.0, σ=1.5 → 11-tap
    mode = "reflect"
    c1, c2 = (0.01 * peak) ** 2, (0.03 * peak) ** 2
    mu_a = gaussian_filter(a, 1.5, radius=radius, mode=mode)
    mu_b = gaussian_filter(b, 1.5, radius=radius, mode=mode)
    mu_aa, mu_bb, mu_ab = mu_a * mu_a, mu_b * mu_b, mu_a * mu_b
    var_a = gaussian_filter(a * a, 1.5, radius=radius, mode=mode) - mu_aa
    var_b = gaussian_filter(b * b, 1.5, radius=radius, mode=mode) - mu_bb
    cov = gaussian_filter(a * b, 1.5, radius=radius, mode=mode) - mu_ab
    num = (2 * mu_ab + c1) * (2 * cov + c2)
    den = (mu_aa + mu_bb + c1) * (var_a + var_b + c2)
    return float(np.mean(num / den))


def _decode_topos_frames(path: str, n: int):
    from src.shared.media.topos_source import ToposMediaSource

    src = ToposMediaSource(path)
    src.open()
    got = 0
    while got < n:
        try:
            f = src.read_next_frame()
        except StopIteration:
            break
        yield f.planes[0], f.planes[1], f.planes[2]
        got += 1
    src.close()


def _decode_pyav_frames(path: str, w: int, h: int, n: int):
    import av

    c = av.open(path)
    st = next(s for s in c.streams if s.type == "video")
    st.codec_context.thread_count = THREADS
    got = 0
    for frame in c.decode(st):
        if got >= n:
            c.close()
            return
        yield planes_from_frame(frame, w, h)
        got += 1
    c.close()


def _psnr_ssim_vs_source(decoder, res: str, n_psnr: int, n_ssim: int) -> dict:
    """decoder(i) → planes；与源逐帧配对累计 SSE / SSIM。"""
    src_iter = source_frames(res, n_psnr)
    dec_iter = decoder(n_psnr)
    sse = [0.0, 0.0, 0.0]
    cnt = [0, 0, 0]
    ssim_sum = [0.0, 0.0, 0.0]
    ssim_n = 0
    maxerr = [0, 0, 0]
    for i, (sp, dp) in enumerate(zip(src_iter, dec_iter)):
        for ch in range(3):
            sse[ch] += _sse(sp[ch], dp[ch])
            cnt[ch] += sp[ch].size
            d = np.abs(sp[ch].astype(np.int32) - dp[ch].astype(np.int32))
            m = int(d.max()) if d.size else 0
            if m > maxerr[ch]:
                maxerr[ch] = m
            if i < n_ssim:
                ssim_sum[ch] += ssim_plane(sp[ch], dp[ch])
        ssim_n = min(i + 1, n_ssim)
    return {
        "psnr_y_db": round(psnr_from_sse(sse[0], cnt[0]), 3),
        "psnr_u_db": round(psnr_from_sse(sse[1], cnt[1]), 3),
        "psnr_v_db": round(psnr_from_sse(sse[2], cnt[2]), 3),
        "max_abs_err_yuv": maxerr,
        "ssim_frames": ssim_n,
        "ssim_y": round(ssim_sum[0] / ssim_n, 5),
        "ssim_u": round(ssim_sum[1] / ssim_n, 5),
        "ssim_v": round(ssim_sum[2] / ssim_n, 5),
    }


def _multigen_topos(res: str, w: int, h: int, qp: int, gens=(1, 3, 5, 10)) -> list:
    """代链：源→T1→T2→…；每代 PSNR(Tn, 源)。n=60。"""
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.metadata import Rational
    from src.shared.media.topos_source import ToposMediaSource

    n = MULTIGEN_FRAMES
    cur = list(source_frames(res, n))  # 60 帧 1080p≈373MB / 4K≈1.5GB，可承受
    results = []
    g = 0
    while g < max(gens):
        g += 1
        out = str(OUT_DIR / f".mg_topos_g{g}.mov")
        cfg = FFmpegEncoderConfig(
            codec="topos", width=w, height=h, fps=Rational(FPS, 1),
            pix_fmt="yuv422p10le", profile="standard", crf=qp,
        )
        enc = ToposVideoEncoder(out, cfg, slice_threads=THREADS)
        enc.open()
        for i, planes in enumerate(cur):
            enc.encode_frame(topos_frame_obj(i, planes, w, h))
        enc.close()
        nxt = []
        src2 = ToposMediaSource(out)
        src2.open()
        for _ in range(n):
            try:
                f = src2.read_next_frame()
            except StopIteration:
                break
            nxt.append((f.planes[0], f.planes[1], f.planes[2]))
        src2.close()
        cur = nxt
        if g in gens:
            sse = [0.0, 0.0, 0.0]
            cnt = [0, 0, 0]
            for sp, dp in zip(source_frames(res, n), cur):
                for ch in range(3):
                    sse[ch] += _sse(sp[ch], dp[ch])
                    cnt[ch] += sp[ch].size
            results.append({
                "gen": g,
                "psnr_y_db": round(psnr_from_sse(sse[0], cnt[0]), 3),
                "psnr_u_db": round(psnr_from_sse(sse[1], cnt[1]), 3),
                "psnr_v_db": round(psnr_from_sse(sse[2], cnt[2]), 3),
            })
    for gfile in OUT_DIR.glob(".mg_topos_g*.mov"):
        gfile.unlink()
    return results


def _multigen_prores(res: str, w: int, h: int, gens=(1, 3, 5, 10)) -> list:
    import av

    n = MULTIGEN_FRAMES
    cur = list(source_frames(res, n))
    results = []
    g = 0
    while g < max(gens):
        g += 1
        out = str(OUT_DIR / f".mg_prores_g{g}.mov")
        oc = av.open(out, "w")
        st = oc.add_stream("prores_ks", rate=FPS)
        cc = st.codec_context
        cc.width, cc.height, cc.pix_fmt = w, h, "yuv422p10le"
        cc.time_base = Fraction(1, FPS)
        cc.thread_count = THREADS
        cc.profile = "hq"
        for y, u, v in cur:
            f = av.VideoFrame(w, h, "yuv422p10le")
            f.planes[0].update(y.tobytes())
            f.planes[1].update(u.tobytes())
            f.planes[2].update(v.tobytes())
            for p in cc.encode(f):
                oc.mux(p)
        for p in cc.encode(None):
            oc.mux(p)
        oc.close()
        nxt = list(_decode_pyav_frames(out, w, h, n))
        cur = nxt
        if g in gens:
            sse = [0.0, 0.0, 0.0]
            cnt = [0, 0, 0]
            for sp, dp in zip(source_frames(res, n), cur):
                for ch in range(3):
                    sse[ch] += _sse(sp[ch], dp[ch])
                    cnt[ch] += sp[ch].size
            results.append({
                "gen": g,
                "psnr_y_db": round(psnr_from_sse(sse[0], cnt[0]), 3),
                "psnr_u_db": round(psnr_from_sse(sse[1], cnt[1]), 3),
                "psnr_v_db": round(psnr_from_sse(sse[2], cnt[2]), 3),
            })
    for gfile in OUT_DIR.glob(".mg_prores_g*.mov"):
        gfile.unlink()
    return results


def cmd_quality(res: str) -> None:
    src_path, w, h = SOURCES[res]
    gen = json.loads((OUT_DIR / f"gen_{res}.json").read_text())
    qp = gen["topos"]["qp"]

    print(f"[quality {res}] topos vs source ...")
    topos_q = _psnr_ssim_vs_source(
        lambda n: _decode_topos_frames(gen["topos"]["path"], n), res,
        N_FRAMES, SSIM_FRAMES)
    print(json.dumps(topos_q, indent=2))
    print(f"[quality {res}] prores vs source ...")
    prores_q = _psnr_ssim_vs_source(
        lambda n: _decode_pyav_frames(gen["prores"]["path"], w, h, n), res,
        N_FRAMES, SSIM_FRAMES)
    print(json.dumps(prores_q, indent=2))
    print(f"[quality {res}] dnxhr vs source ...")
    dnxhr_q = _psnr_ssim_vs_source(
        lambda n: _decode_pyav_frames(gen["dnxhr"]["path"], w, h, n), res,
        N_FRAMES, SSIM_FRAMES)
    print(json.dumps(dnxhr_q, indent=2))

    print(f"[quality {res}] topos multigen ...")
    mg_topos = _multigen_topos(res, w, h, qp)
    print(f"[quality {res}] prores multigen ...")
    mg_prores = _multigen_prores(res, w, h)

    write_json(f"quality_{res}.json", {
        "res": res, "frames_psnr": N_FRAMES, "frames_ssim": SSIM_FRAMES,
        "topos": topos_q, "prores": prores_q, "dnxhr": dnxhr_q,
        "multigen_topos": mg_topos, "multigen_prores": mg_prores,
        "bitrate_mbps": {
            "topos": gen["topos"]["bitrate_mbps(file,120f)"],
            "prores": gen["prores"]["bitrate_mbps(file,120f)"],
            "dnxhr": gen["dnxhr"]["bitrate_mbps(file,120f)"],
        },
    })


# ----------------------------------------------------------------------------
# cold：子进程隔离首帧
# ----------------------------------------------------------------------------

_COLD_SNIPPET = r"""
import sys, time
sys.path.insert(0, {root!r})
kind, path, w, h = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
t0 = time.perf_counter()
if kind == "topos":
    from src.shared.media.topos_source import ToposMediaSource
    src = ToposMediaSource(path)
    src.open()
    f = src.read_next_frame()
    assert f.planes[0].shape[0] == h
    src.close()
else:
    import av
    c = av.open(path)
    st = next(s for s in c.streams if s.type == "video")
    st.codec_context.thread_count = 8
    for frame in c.decode(st):
        assert frame.height == h
        break
    c.close()
print((time.perf_counter() - t0) * 1000.0)
"""


def cmd_cold(res: str) -> None:
    src_path, w, h = SOURCES[res]
    gen = json.loads((OUT_DIR / f"gen_{res}.json").read_text())
    snippet_path = OUT_DIR / "_cold_snippet.py"
    snippet_path.write_text(_COLD_SNIPPET.format(root=str(PROJECT_ROOT)))
    out = {}
    for kind, path in (
        ("topos", gen["topos"]["path"]),
        ("prores", gen["prores"]["path"]),
        ("dnxhr", gen["dnxhr"]["path"]),
    ):
        vals = []
        for run in range(3):
            r = subprocess.run(
                [sys.executable, str(snippet_path), kind, path, str(w), str(h)],
                capture_output=True, text=True, check=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            vals.append(float(r.stdout.strip().splitlines()[-1]))
        out[kind] = {
            "runs_ms": [round(v, 1) for v in vals],
            "median_ms": round(statistics.median(vals), 1),
        }
        print(kind, out[kind])
    write_json(f"cold_{res}.json", {"res": res, **out})


# ----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["gen", "decode", "quality", "cold"])
    ap.add_argument("--res", choices=["1080p", "4k"], required=True)
    args = ap.parse_args()
    {"gen": cmd_gen, "decode": cmd_decode,
     "quality": cmd_quality, "cold": cmd_cold}[args.phase](args.res)


if __name__ == "__main__":
    main()
