#!/usr/bin/env python3
"""预览关滤波（解码请求 TC_DECODE_FLAG_NO_DEBLOCK）A/B：画质 + 吞吐 + 放大面板。

用户偏好 `video_preview_no_deblock` 走的就是这条路径：预览/播放解码请求带
NO_DEBLOCK，跳过解码端输出去块。本工具用**产品预览路径**
（`ToposMediaSource(target_size=…, no_deblock=…)` → `read_frame`）做同源 A/B：

- 画质：对照母版 reformat 到**同一目标尺寸**（与画质文档同口径）出块界台阶 /
  PSNR / 锐度；
- 吞吐：逐帧 read_frame 的中位 fps（含容器读取与平面池，即预览真实口径）；
- 面板：母版 / 滤波 / 关滤波 三方放大（主观验收"关掉后能看到什么"）。

用法::

    .venv/bin/python tools/bench_topos_preview_filter.py \
      --files tests/test_videos/4K_topos_422_Proxy.mov tests/test_videos/4K_topos_422.mov \
      --target 1920x1080 --frames 0 100 240 300 486 --reps 3
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from bench_topos_qgain import iter_master  # noqa: E402
from compare_topos_deblock import metrics  # noqa: E402
from diagnose_edge_jaggies import auto_crops, panel  # noqa: E402
from panel_topos_blockiness import pick_worst_blocking  # noqa: E402
from src.shared.media.topos_source import ToposMediaSource  # noqa: E402


def run_file(path: Path, target: tuple[int, int] | None, frames: list[int],
             reps: int, time_count: int) -> dict:
    """同一文件、同一目标尺寸：滤波 on/off 各解一轮，返回指标 + 时间。

    计时用**稠密帧序列**（0..time_count-1）摊平平面池/首帧预热——只解指标用的
    5 帧会把 read_frame 的固定开销算进每帧成本，滤掉滤波本身的差别。
    """
    out: dict = {"file": path.name, "variants": {}}
    got_y: dict[str, np.ndarray] = {}
    time_frames = list(range(max(1, time_count)))
    for tag, no_deblock in (("filtered", False), ("no_deblock", True)):
        src = ToposMediaSource(str(path), target_size=target, no_deblock=no_deblock)
        src.open()
        try:
            diag = dict(src.diagnostics)
            times: list[float] = []
            for _ in range(reps):
                t0 = time.perf_counter()
                for i in time_frames:
                    src.read_frame(i)
                times.append((time.perf_counter() - t0) / max(1, len(time_frames)))
            frame = src.read_frame(frames[-1])
            got_y[tag] = np.asarray(frame.planes[0], dtype=np.uint16)
            out["variants"][tag] = {
                "no_deblock": no_deblock,
                "native_no_deblock": bool(diag.get("native_no_deblock")),
                "decode_path": diag.get("decode_path"),
                "ms_per_frame": round(float(np.median(times)) * 1e3, 3),
                "fps": round(1.0 / float(np.median(times)), 1),
                "plane": list(got_y[tag].shape),
            }
        finally:
            src.close()
    out["frames"] = frames
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="+", required=True)
    ap.add_argument("--target", default="1920x1080",
                    help="预览目标尺寸 WxH；none = 全尺寸解码")
    ap.add_argument("--frames", nargs="+", type=int,
                    default=[0, 100, 240, 300, 486])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--time-count", type=int, default=24,
                    help="计时帧数（稠密 0..N-1，摊平预热；默认 24）")
    ap.add_argument("--crops", type=int, default=2)
    ap.add_argument("--pick", default="blockiness",
                    choices=("blockiness", "edges"),
                    help="裁片选择：blockiness = 平坦区块界台阶最大处（默认，"
                         "块效应最显眼）；edges = 边缘最密处")
    ap.add_argument("--out", type=Path, default=REPO / "bench_out" / "qgain")
    args = ap.parse_args()

    target = None if args.target.lower() in ("none", "full") else tuple(
        int(x) for x in args.target.lower().split("x"))
    want = set(args.frames)
    report: dict = {"target": args.target, "frames": args.frames,
                    "reps": args.reps, "time_count": args.time_count,
                    "files": []}

    print(f"\n{'文件':<26}{'滤波 ms/帧':>11}{'关滤波 ms/帧':>13}{'提速':>8}"
          f"{'block_h':>18}{'block_v':>18}{'Y PSNR':>15}")
    for f in args.files:
        path = Path(f)
        row = run_file(path, target, args.frames, args.reps, args.time_count)
        ref = iter_master("yuv422p10le", *target, want) if target else None
        for tag in ("filtered", "no_deblock"):
            y = None
            src = ToposMediaSource(str(path), target_size=target,
                                   no_deblock=(tag == "no_deblock"))
            src.open()
            try:
                acc: dict[str, list] = {}
                for i in args.frames:
                    y = np.asarray(src.read_frame(i).planes[0], dtype=np.uint16)
                    if ref is not None and i in ref:
                        for k, v in metrics(ref, {i: y}).items():
                            acc.setdefault(k, []).append(v)
            finally:
                src.close()
            row["variants"][tag].update(
                {k: round(float(np.mean(v)), 4) for k, v in acc.items()})
            row["variants"][tag]["_y"] = y
        a = row["variants"]["filtered"]
        b = row["variants"]["no_deblock"]
        print(f"{row['file']:<26}{a['ms_per_frame']:>11.3f}"
              f"{b['ms_per_frame']:>13.3f}{b['fps'] / a['fps']:>7.2f}×"
              f"{a.get('block_h', float('nan')):>9.4f}→{b.get('block_h', float('nan')):<8.4f}"
              f"{a.get('block_v', float('nan')):>9.4f}→{b.get('block_v', float('nan')):<8.4f}"
              f"{a.get('y_psnr', float('nan')):>7.2f}→{b.get('y_psnr', float('nan')):<7.2f}")
        if ref is not None and args.crops > 0:
            f0 = args.frames[-1] if args.frames[-1] in ref else sorted(ref)[-1]
            args.out.mkdir(parents=True, exist_ok=True)
            if args.pick == "blockiness":
                crops = [(y, x) for _s, y, x in pick_worst_blocking(
                    ref[f0][0], b["_y"], args.crops, 192,
                    mean_range=(0.0, 1023.0))]
            else:
                crops = []
            if not crops:
                crops = auto_crops(ref[f0][0], k=args.crops, win=192)
            for j, (cy, cx) in enumerate(crops):
                panel(ref[f0][0], a["_y"], b["_y"], cy, cx, 160, 6,
                      args.out / f"nodeblock_{path.stem}_f{f0}_c{j}.png",
                      f"{path.stem} f{f0}",
                      labels=("Master", "Preview: filtered",
                              "Preview: no deblock"))
            print(f"   面板 → {args.out}/nodeblock_{path.stem}_f{f0}_c*.png")
        for tag in ("filtered", "no_deblock"):
            row["variants"][tag].pop("_y", None)
        report["files"].append(row)

    args.out.mkdir(parents=True, exist_ok=True)
    key = args.target.replace("x", "x")
    out = args.out / f"preview_nodeblock_ab_{key}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(f"\nreport → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
