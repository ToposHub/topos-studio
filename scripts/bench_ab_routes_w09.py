"""W09（Windows 计划项）：固定解码策略候选 A/B 对比基准（机器 B）。

按 codec/位深/尺寸交错 **成对** 比较固定候选路由（cpu_planar /
hw_download_planar / cuda_resident），每对 ≥5 次重复给出噪声带，
只有**超噪声且正确稳定**（md5 与同解码器下载参考一致）的子矩阵才可
作为固定白名单候选。本脚本只产出原始数据与判定，**不是**运行时自适应
路由（UW01 另行立项）；显式 opt-in 与 fail-closed 回退语义保持不变。

路由环境（子进程隔离，杜绝 env 串扰）：
- cpu_planar        TOPOS_DISABLE_WIN_HW_DECODE=1
- hw_download       无驻留 env、无 disable
- cuda_resident     TOPOS_WIN_HW_RESIDENT_DECODE=1

覆盖样本（含失败格）：4K H264 8bit / 2K H265 10bit / 非对齐 322×202 /
ProRes422（生产路由=cpu，验证无退化）/ H264 4:2:2（驻留须 fail-closed
回退——覆盖率证据，不冒充支持）。

用法：
    .venv/Scripts/python.exe scripts/bench_ab_routes_w09.py \
        --out docs/validation/upgrade_2026-10-24/W09/_work/ab_results.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"

#: 成对比较矩阵：每项 = (asset, route_a, route_b)
PAIRS = [
    ("4K_H264.mov", "cpu_planar", "hw_download"),
    ("4K_H264.mov", "hw_download", "cuda_resident"),
    ("2K_H265.mov", "hw_download", "cuda_resident"),
    ("unaligned_322x202_h264.mov", "hw_download", "cuda_resident"),
    ("2K_prores422.mov", "cpu_planar", "hw_download"),
]

#: 覆盖率/失败样本：4:2:2 在驻留路由必须 fail-closed 回退
COVERAGE = [
    ("yuv422_h264.mov", "cuda_resident"),
    ("yuv422_h264.mov", "hw_download"),
]

REPS = 5
WARMUP = 6
TIMED = 42

ROUTE_ENV = {
    "cpu_planar": {"TOPOS_DISABLE_WIN_HW_DECODE": "1"},
    "hw_download": {},
    "cuda_resident": {"TOPOS_WIN_HW_RESIDENT_DECODE": "1"},
}

_CHILD = r'''
import hashlib
import json, os, sys, time, statistics
sys.path.insert(0, r"{root}")

asset, route, warmup, timed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
for k, v in {env}.items():
    os.environ[k] = v
os.environ.pop("TOPOS_WIN_HW_RESIDENT_DECODE", None)
os.environ.pop("TOPOS_DISABLE_WIN_HW_DECODE", None)
for k, v in {env}.items():
    if v:
        os.environ[k] = v

from pathlib import Path
videos = Path(r"{videos}")

from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

manager = EditGpuDecodeManager()
out = {{"asset": asset, "route": route}}
try:
    t0 = time.monotonic()
    frame, ftype = manager.get_frame(str(videos / asset), 0)
    first_ms = (time.monotonic() - t0) * 1000.0
    if frame is None:
        out.update(status="no_frame", frame_type=ftype)
        print(json.dumps(out)); sys.exit(0)
    out["frame_type"] = ftype
    out["first_frame_ms"] = round(first_ms, 1)
    # 吞吐：warmup + timed 环形窗口（i%24——**非**全序列连续吞吐，
    # 指标名 fps_cyclic24 如实声明；窗口外全序列吞吐另行基准）
    cur = 0
    for i in range(warmup):
        f2, _t = manager.get_frame(str(videos / asset), i % 24)
        if f2 is None:
            out.update(status="decode_none_at", frame=i); print(json.dumps(out)); sys.exit(0)
    t1 = time.monotonic()
    n = 0
    for i in range(timed):
        f2, _t = manager.get_frame(str(videos / asset), i % 24)
        if f2 is None:
            out.update(status="decode_none_at", frame=i); print(json.dumps(out)); sys.exit(0)
        n += 1
    elapsed = time.monotonic() - t1
    out["fps_cyclic24"] = round(n / elapsed, 1)
    # 反向 seek 时延（中位 5 次）——N23：seek 返回 None 是失败不是时延
    lats = []
    for target in (180, 40, 160, 60, 140):
        ts = time.monotonic()
        f3, _t3 = manager.get_frame(str(videos / asset), target)
        if f3 is None:
            out.update(status="seek_none_at", seek_target=target)
            print(json.dumps(out)); sys.exit(0)
        lats.append((time.monotonic() - ts) * 1000.0)
    out["seek_med_ms"] = round(statistics.median(lats), 1)
    # 正确性：帧 0 内容 md5（Y 平面或首平面字节）。cuda_resident 帧是
    # 设备指针描述（0 CPU 字节，by design）——CPU 侧 md5 不适用，
    # 正确性由 W04 L2 的 interop 纹理 md5 对拍证据持有，此处显式标记。
    f0, _t0 = manager.get_frame(str(videos / asset), 0)
    if f0 is None:
        out.update(status="redecode_none"); print(json.dumps(out)); sys.exit(0)
    from src.shared.media.windows_hw.cuda_resident import (
        is_cuda_resident_frame,
    )
    if is_cuda_resident_frame(f0):
        out["frame0_md5"] = None
        out["md5_note"] = "device_resident:CPU md5 n/a (W04 L2 interop md5 为准)"
    else:
        import numpy as np
        planes = getattr(f0, "planes", None)
        if planes is not None:
            arr = np.asarray(planes[0])
        else:
            data = getattr(f0, "data", b"")
            arr = np.frombuffer(data, dtype=np.uint8)
        out["frame0_md5"] = hashlib.md5(np.ascontiguousarray(arr).tobytes()).hexdigest()
    out["status"] = "ok"
finally:
    manager.close_all()
print(json.dumps(out))
'''


def _run_child(asset: str, route: str) -> dict:
    import tempfile

    code = _CHILD.format(
        root=str(PROJECT_ROOT), env=repr(ROUTE_ENV[route]),
        videos=str(TEST_VIDEOS))
    with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = f.name
    try:
        res = subprocess.run(
            [sys.executable, path, asset, route, str(WARMUP), str(TIMED)],
            capture_output=True, text=True, timeout=300,
            cwd=str(PROJECT_ROOT))
        line = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
        try:
            return json.loads(line)
        except Exception:
            return {"asset": asset, "route": route, "status": "child_error",
                    "stderr": res.stderr[-400:]}
    finally:
        os.unlink(path)


def _correctness_verdict(samples: list, ref_md5: str | None) -> tuple[str, list]:
    """N23 · 单路由正确性三态判定（不再空列表恒真）。

    - mismatch：任一有效 md5 与参考不一致（硬失败）；
    - unverified_cpu：无参考指纹（参照系运行失败）；
    - unverified_external：全部为设备驻留帧（CPU md5 不适用——外部
      W04 L2 interop 证据持有，本基准不据此判 stable）；
    - unverified_empty：ok 样本为零（无任何证据）；
    - verified：≥1 有效 CPU md5 且全部与参考一致。
    """
    oks = [s for s in samples if s.get("status") == "ok"]
    md5s = [s.get("frame0_md5") for s in oks]
    if not oks:
        return "unverified_empty", []
    if ref_md5 is None:
        return "unverified_cpu", md5s
    cpu_md5s = [m for m in md5s if m is not None]
    if not cpu_md5s:
        return "unverified_external", md5s
    matches = [m == ref_md5 for m in cpu_md5s]
    if all(matches):
        return "verified", md5s
    return "mismatch", md5s


#: N23 · 路由命中校验：样本 frame_type 必须落在该路由的预期族内，
#: 否则样本被错误路由（env 未生效/路由退化）——标 route_miss 非零退出。
ROUTE_EXPECTED_FRAME_TYPES = {
    "cpu_planar": {"cpu", "planar", ""},
    "hw_download": {"gpu", "planar", ""},
    "cuda_resident": {"cuda_resident"},
}


def _route_hit(route: str, samples: list) -> tuple[bool, list]:
    expected = ROUTE_EXPECTED_FRAME_TYPES[route]
    seen = sorted({s.get("frame_type", "?") for s in samples
                   if s.get("status") == "ok"})
    return (bool(seen) and set(seen) <= expected), seen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--reps", type=int, default=REPS)
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    import av

    result = {
        "schema": "bench_ab_routes_w09/2",
        "date": datetime.now().isoformat(timespec="seconds"),
        "pyav": av.__version__,
        "reps": args.reps,
        "timed_frames": TIMED,
        "machine": "B(RTX 4090)/Windows",
        "metric_note": "fps_cyclic24 = i%24 环形窗口吞吐（非全序列连续吞吐）",
        "disclaimer": "固定候选对比原始数据；非运行时自适应路由(UW01)。"
                      "仅超噪声且正确性 verified 的子矩阵可作固定白名单候选；"
                      "mismatch/unverified 均不得通过。",
        "pairs": [],
        "coverage": [],
    }
    failures: list[str] = []

    # 参考指纹（hw_download 一次，作为 md5 正确性参照）
    ref_by_asset: dict[str, str] = {}
    for asset, _a, _b in PAIRS:
        r = _run_child(asset, "hw_download")
        if r.get("status") == "ok" and r.get("frame0_md5"):
            ref_by_asset[asset] = r["frame0_md5"]

    for asset, ra, rb in PAIRS:
        samples = {ra: [], rb: []}
        for _ in range(args.reps):
            for route in (ra, rb):
                samples[route].append(_run_child(asset, route))
        entry = {"asset": asset, "routes": [ra, rb], "reps": {}}
        for route in (ra, rb):
            oks = [s for s in samples[route] if s.get("status") == "ok"]
            verdict, md5s = _correctness_verdict(
                samples[route], ref_by_asset.get(asset))
            hit, seen_types = _route_hit(route, samples[route])
            entry["reps"][route] = {
                "ok_count": len(oks),
                "total": len(samples[route]),
                "statuses": sorted({s.get("status") for s in samples[route]}),
                "frame_types": sorted({s.get("frame_type", "?")
                                       for s in samples[route]}),
                "fps_values": [s.get("fps_cyclic24") for s in oks],
                "fps_mean": round(statistics.fmean(
                    [s["fps_cyclic24"] for s in oks]), 1) if oks else None,
                "seek_med_ms_values": [s.get("seek_med_ms") for s in oks],
                "frame0_md5_values": md5s,
                "correctness": verdict,
                "route_hit": hit,
                "route_hit_seen": seen_types,
            }
            if not oks:
                failures.append(f"{asset}/{route}: zero ok samples")
            if verdict == "mismatch":
                failures.append(f"{asset}/{route}: md5 mismatch")
            if not hit:
                failures.append(
                    f"{asset}/{route}: route_miss seen={seen_types}")
        fa = entry["reps"][ra]
        fb = entry["reps"][rb]
        # 超噪声判定：均值差 > 两倍各自极差和（保守噪声带）
        if fa["fps_values"] and fb["fps_values"]:
            spread_a = max(fa["fps_values"]) - min(fa["fps_values"])
            spread_b = max(fb["fps_values"]) - min(fb["fps_values"])
            delta = fb["fps_mean"] - fa["fps_mean"]
            noise_band = 2 * (spread_a + spread_b) + 1.0
            entry["delta_fps"] = round(delta, 1)
            entry["beyond_noise"] = abs(delta) > noise_band
        # N23：correctness_stable 仅在两路由均 verified（旧版空列表
        # all(... or [True]) 恒真的假绿已移除）
        entry["correctness_stable"] = bool(
            fa["correctness"] == "verified"
            and fb["correctness"] == "verified")
        entry["correctness_note"] = (
            "cuda_resident 为设备驻留帧(0 CPU 字节)，CPU md5 不适用——"
            "correctness=unverified_external（非 stable）；其正确性证据 "
            "= W04 L2/W02 interop 纹理 md5 与同解码器下载参考逐字节一致"
            "(test_windows_cuda_resident/test_cuda_resident_format_matrix_w04)")
        result["pairs"].append(entry)
        print(f"[w09] {asset}: {ra}({fa['fps_mean']}) vs "
              f"{rb}({fb['fps_mean']}) beyond_noise="
              f"{entry.get('beyond_noise')} correct="
              f"{fa['correctness']}/{fb['correctness']} "
              f"hit={fa['route_hit']}/{fb['route_hit']}")

    for asset, route in COVERAGE:
        r = _run_child(asset, route)
        result["coverage"].append({
            "asset": asset, "route": route,
            "status": r.get("status"), "frame_type": r.get("frame_type", ""),
            "note": "fail-closed 回退即预期（4:2:2 无 NVDEC cuda 驻留输出）",
        })
        print(f"[w09] coverage {asset}/{route}: {r.get('status')} "
              f"-> {r.get('frame_type', r.get('stderr', '')[:60])}")

    result["failures"] = failures
    result["passed"] = not failures
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[w09] written {out_path}")
    if failures:
        print(f"[w09] FAIL ({len(failures)}):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
