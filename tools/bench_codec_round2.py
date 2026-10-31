#!/usr/bin/env python3
"""第二轮性能复审基准（M10-0.4/0.5）：Topos 解码三层口径分离。

背景（复审 P0-1/P0-8/P0-9）：旧随机访问口径把 Topos 的 O(1) 索引读取与
PyAV 的 seek+解码器重置混在一起，且单次单进程运行易受调度/温度影响。
本工具只测 Topos 自身，把「解码内核 / 容器顺序 / 索引随机」拆成三层，
各自独立报告，不混写结论：

  层 A  内存 packet 纯 native —— packet 预取进内存 + 平面预分配 +
        tc_frame_decode 直调。隔离 demux/pread/Python 缓冲物化，
        代表解码内核上限。
  层 B  容器顺序 —— ToposMovieFile.packet_into(i) 顺序读
        （索引解析 + pread + memmove）+ 内核解码。产品顺序播放下界。
  层 C  预建索引随机 —— 同 B，但索引为确定性伪随机序（含正反跳）。
        代表随机访问产品路径（O(1) 索引 + pread + 解码）。

口径（M10-0.5）：每层预热 ≥20 帧、有效 ≥200 帧、重复 5 次取中位数；
记录 CPU 时间/有效核数/RSS 漂移；重复间 avg CV>3% 时自动追加重跑并标注。
输出目录绑定 commit+时间戳，已存在即拒绝（禁止覆盖，P0-8）。

用法：
    TOPOS_SLICE_THREADS=8 .venv/bin/python tools/bench_codec_round2.py \
        [--res 1080p 4k] [--repeats 5] [--frames 200]
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
try:
    import resource  # POSIX：getrusage（Windows 无此模块，回退 psutil）
except ImportError:
    resource = None
import statistics
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

SOURCES = {
    "1080p": ("tests/test_videos/2K_topos.mov", 1920, 1080),
    "4k": ("tests/test_videos/4K_topos.mov", 3840, 2160),
}
FPS = 25.0
WARMUP = 20          # M10-0.5：预热 ≥20 帧
DEFAULT_FRAMES = 200  # 有效 ≥200 帧
DEFAULT_REPEATS = 5   # 5 次取中位数
CV_GATE = 0.03        # 重复间 avg CV ≤ 3%，超出自动追加重跑
MAX_RERUN = 2


def _git_commit() -> tuple[str, bool]:
    try:
        c = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip() != ""
        return c, dirty
    except Exception:
        return "unknown", True


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _cpu_seconds() -> float:
    if resource is not None:
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_utime + r.ru_stime
    import psutil

    t = psutil.Process().cpu_times()
    return t.user + t.system


def _rss_mb() -> float:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / 1e6
    except Exception:
        if resource is not None:
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
        return 0.0


def _stats(times_ms: list[float], wall_s: float, cpu_s: float) -> dict:
    xs = sorted(times_ms)
    n = len(xs)

    def pct(p: float) -> float:
        i = min(n - 1, max(0, int(round(p * (n - 1)))))
        return xs[i]

    return {
        "frames": n,
        "avg_ms": round(statistics.fmean(xs), 3),
        "p50_ms": round(pct(0.50), 3),
        "p95_ms": round(pct(0.95), 3),
        "p99_ms": round(pct(0.99), 3),
        "min_ms": round(xs[0], 3),
        "max_ms": round(xs[-1], 3),
        "throughput_fps": round(n / wall_s, 1),
        "wall_s": round(wall_s, 3),
        "cpu_s": round(cpu_s, 3),
        "effective_cores": round(cpu_s / wall_s, 2) if wall_s > 0 else None,
    }


class _Kernel:
    """tc_frame_decode 直调（层 A）：内存 packet + 预分配平面。"""

    def __init__(self, lib, packets: list[ctypes.Array], sizes: list[int]):
        self.lib = lib
        self.packets = packets
        self.sizes = sizes
        self.ptrs = None
        self.info = None
        # P0-01（UAF 修复）：平面数组必须由本实例持有——此前只保留
        # ctypes.cast 出来的裸指针，局部 arr 在循环重绑/函数返回后被
        # GC 回收，native 解码向悬垂地址写入（吞吐/p95/RSS 全部失效）。
        self._plane_keep: list = []

    def _ensure_planes(self) -> None:
        if self.ptrs is not None:
            return
        from src.shared.codec import topos_binding as tb

        info = tb._CFrameOutput()
        buf0 = self.packets[0]
        rc = int(self.lib.tc_frame_decode(
            buf0, self.sizes[0], None, None, ctypes.byref(info)))
        if rc not in (0, 1):
            raise RuntimeError(f"query rc={rc}")
        self.info = info
        ptrs = (ctypes.POINTER(ctypes.c_uint16) * tb.TC_FRAME_MAX_PLANES)()
        codec = tb.ToposCodec()
        for p in range(info.plane_count):
            w, h = codec.plane_geometry(info, p)
            arr = (ctypes.c_uint16 * (w * h))()
            self._plane_keep.append(arr)  # 保活到解码器生命周期结束
            ptrs[p] = ctypes.cast(arr, ctypes.POINTER(ctypes.c_uint16))
        self.ptrs = ptrs

    def run(self, idxs: list[int]) -> None:
        self._ensure_planes()
        lib = self.lib
        info = self.info
        ptrs = self.ptrs
        out = ctypes.byref(info)
        for i in idxs:
            rc = int(lib.tc_frame_decode(
                self.packets[i], self.sizes[i], ptrs, None, out))
            if rc not in (0, 1):
                raise RuntimeError(f"decode rc={rc}")


def _timed_layer(run_once, idx_seq: list[int]) -> dict:
    """单次重复计时：返回该次重复的统计（含 CPU/RSS）。"""
    rss0 = _rss_mb()
    cpu0, w0 = _cpu_seconds(), time.perf_counter()
    times = []
    for i in idx_seq:
        t0 = time.perf_counter()
        run_once(i)
        times.append((time.perf_counter() - t0) * 1000.0)
    wall = time.perf_counter() - w0
    cpu = _cpu_seconds() - cpu0
    st = _stats(times, wall, cpu)
    st["rss_before_mb"] = round(rss0, 1)
    st["rss_after_mb"] = round(_rss_mb(), 1)
    st["rss_drift_mb"] = round(st["rss_after_mb"] - st["rss_before_mb"], 2)
    return st


def _aggregate(repeats: list[dict]) -> dict:
    """多次重复取中位数聚合。"""
    out: dict = {"repeats": len(repeats), "repeat_avgs_ms": []}
    for key in ("avg_ms", "p50_ms", "p95_ms", "p99_ms", "throughput_fps",
                "effective_cores", "cpu_s", "rss_drift_mb"):
        vals = [r[key] for r in repeats if r.get(key) is not None]
        out[key] = round(statistics.median(vals), 3) if vals else None
    out["repeat_avgs_ms"] = [round(r["avg_ms"], 3) for r in repeats]
    out["frames"] = repeats[0]["frames"]
    avgs = out["repeat_avgs_ms"]
    if len(avgs) >= 2 and statistics.fmean(avgs) > 0:
        out["avg_cv"] = round(statistics.pstdev(avgs) / statistics.fmean(avgs), 4)
    return out


def bench_layer(name: str, run_once, idx_seqs: list[list[int]]) -> dict:
    repeats = [_timed_layer(run_once, seq) for seq in idx_seqs]
    agg = _aggregate(repeats)
    reruns = 0
    while agg.get("avg_cv") is not None and agg["avg_cv"] > CV_GATE \
            and reruns < MAX_RERUN and len(idx_seqs) > 0:
        repeats.append(_timed_layer(run_once, idx_seqs[0]))
        agg = _aggregate(repeats)
        reruns += 1
    agg["cv_gate"] = CV_GATE
    agg["auto_reruns"] = reruns
    agg["cv_ok"] = bool(agg.get("avg_cv") is not None and agg["avg_cv"] <= CV_GATE)
    agg["all_repeats"] = repeats
    return {name: agg}


def cmd_decode(res: str, repeats: int, frames: int, out_dir: Path,
               manifest: dict) -> None:
    import numpy as np

    from src.shared.codec import topos_binding as tb

    path, w, h = SOURCES[res]
    codec = tb.ToposCodec()
    lib = codec._lib
    movie = tb.ToposMovieFile(codec, path)
    total = movie.info().sample_count
    n_avail = min(frames, total)

    # 层 A 输入：全部 packet 预取进内存（复用缓冲物化一次）
    print(f"[{res}] 预取 {total} packet 进内存（一次性，不计入层 A）…")
    packets, sizes = [], []
    for i in range(total):
        buf, n = movie.packet_into(i)
        packets.append(ctypes.create_string_buffer(bytes(buf.raw[:n]), n))
        sizes.append(n)
    kernel = _Kernel(lib, packets, sizes)
    kernel._ensure_planes()
    info = kernel.info
    bitrate_mbps = round(
        sum(sizes) * 8.0 / (total / FPS) / 1e6, 2) if total else None

    rng = np.random.RandomState(20260901)
    seq_idxs = [[i % total for i in range(n_avail)] for _ in range(repeats)]
    rnd_base = rng.choice(total, size=min(n_avail, total), replace=False)
    rnd_idxs = []
    for r in range(repeats):
        perm = rng.permutation(rnd_base)
        rnd_idxs.append([int(x) for x in perm])

    print(f"[{res}] 层 A：内存 packet 纯 native（{n_avail} 帧 × {repeats} 次）…")
    res_a = bench_layer("A_memory_packet_native",
                        lambda i: kernel.run([i]), seq_idxs)

    print(f"[{res}] 层 B：容器顺序（{n_avail} 帧 × {repeats} 次）…")
    def container_once(i: int) -> None:
        buf, n = movie.packet_into(i % total)
        rc = int(lib.tc_frame_decode(
            buf, n, kernel.ptrs, None, ctypes.byref(kernel.info)))
        if rc not in (0, 1):
            raise RuntimeError(f"decode rc={rc}")
    res_b = bench_layer("B_container_sequential", container_once, seq_idxs)

    print(f"[{res}] 层 C：预建索引随机（{len(rnd_idxs[0])} 帧 × {repeats} 次）…")
    res_c = bench_layer("C_index_random", container_once, rnd_idxs)

    result = {
        "res": res,
        "source": path,
        "width": w,
        "height": h,
        "sample_count": total,
        "bit_depth": int(info.bit_depth),
        "pixel_format": int(info.pixel_format),
        "plane_count": int(info.plane_count),
        "alpha_mode": int(info.alpha_mode),
        "bitrate_mbps": bitrate_mbps,
        "input_memory_copy": "A=无（packet 常驻内存直调）；B/C=os.pread bytes→ctypes.memmove",
        "layers": {**res_a, **res_b, **res_c},
    }
    out_file = out_dir / f"decode_{res}.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"[{res}] → {out_file}")
    for layer, agg in result["layers"].items():
        cv = agg.get("avg_cv")
        print(f"  {layer}: avg {agg['avg_ms']} ms  p95 {agg['p95_ms']} ms  "
              f"吞吐 {agg['throughput_fps']} fps  核 {agg['effective_cores']}  "
              f"CV {cv}{'（超门，自动重跑 ' + str(agg['auto_reruns']) + ' 次）' if not agg['cv_ok'] else ''}")
    movie.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--res", nargs="+", default=["1080p", "4k"],
                    choices=sorted(SOURCES.keys()))
    ap.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    ap.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    ap.add_argument("--force", action="store_true",
                    help="允许写入已存在的本 commit 输出目录（追加新文件）")
    args = ap.parse_args()

    commit, dirty = _git_commit()
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = PROJECT_ROOT / "bench_out" / "round2" / f"{commit}_{ts}"
    if out_dir.exists():
        if not args.force:
            raise SystemExit(f"输出目录已存在，拒绝覆盖：{out_dir}")
    else:
        out_dir.mkdir(parents=True)

    lib_path = Path(os.environ.get("TOPOS_CODEC_LIB", ""))
    if not lib_path.is_file():
        from src.shared.codec import topos_binding as tb

        for cand in tb.candidate_library_paths():
            if cand.is_file():
                lib_path = cand
                break
    manifest = {
        "git_commit": commit,
        "git_dirty": dirty,
        "timestamp": ts,
        "threads_env": os.environ.get("TOPOS_SLICE_THREADS", "default"),
        "library": str(lib_path),
        "library_sha256": _sha256(lib_path) if lib_path.is_file() else None,
        "sources": {},
        "warmup_frames": WARMUP,
        "effective_frames": args.frames,
        "repeats": args.repeats,
        "cv_gate": CV_GATE,
    }
    for res in args.res:
        src = SOURCES[res][0]
        manifest["sources"][res] = {
            "path": src,
            "sha256": _sha256(PROJECT_ROOT / src),
        }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1))

    # 预热全局（LUT/SIMD/线程池）不计入任何层
    print(f"输出目录：{out_dir}")
    for res in args.res:
        cmd_decode(res, args.repeats, args.frames, out_dir, manifest)


if __name__ == "__main__":
    main()
