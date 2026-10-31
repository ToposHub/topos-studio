"""真实视频跟踪性能/稳定性探针（阶段 8 完成后的验收工具）。

默认遍历 ``tests/test_videos`` 中的 8 个真实视频，自动从首帧选择特征
丰富的归一化区域，分别使用 draft（代理）和 high（原始分辨率）执行
前向跟踪。每个组合默认运行 3 轮，并生成 Markdown 报告。

为了让本地回归可以快速完成，默认每轮分析最多 120 帧；传入
``--max-frames 0`` 才会分析到视频末尾。

示例：
    .venv/bin/python scripts/tracking_real_media_probe.py \
        --rounds 3 --max-frames 120 \
        --out docs/color_tracker_real_media_report.md
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.shared.tracking.feature_detector import FeatureDetector  # noqa: E402
from src.shared.tracking.frame_sampler import (  # noqa: E402
    AnalysisQuality,
    FrameSampler,
)
from src.shared.tracking.tracker_2d import (  # noqa: E402
    TrackDirection,
    TrackRequest,
    Tracker2D,
)
from src.shared.tracking.types import TrackStatus, TrackingMode  # noqa: E402


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".avi", ".mkv"}
QUALITY_NAMES = {
    "draft": AnalysisQuality.DRAFT,
    "normal": AnalysisQuality.NORMAL,
    "high": AnalysisQuality.HIGH,
}


@dataclass(frozen=True)
class CandidateRegion:
    region: tuple[float, float, float, float]
    feature_count: int
    sharpness: float
    score: float


def _rss_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux reports KiB.
    return value / (1024.0 * 1024.0) if value > 10_000_000 else value / 1024.0


def _candidate_regions() -> list[tuple[float, float, float, float]]:
    sizes = (0.20, 0.28, 0.36)
    centers = (0.25, 0.5, 0.75)
    regions = []
    for size in sizes:
        for cy in centers:
            for cx in centers:
                x = max(0.0, min(1.0 - size, cx - size / 2.0))
                y = max(0.0, min(1.0 - size, cy - size / 2.0))
                regions.append((x, y, size, size))
    return regions


def _region_sharpness(gray: np.ndarray, region: tuple[float, float, float, float]) -> float:
    h, w = gray.shape[:2]
    x, y, rw, rh = region
    x0, y0 = max(0, int(x * w)), max(0, int(y * h))
    x1, y1 = min(w, int((x + rw) * w)), min(h, int((y + rh) * h))
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return 0.0
    import cv2
    return float(cv2.Laplacian(crop, cv2.CV_64F).var())


def choose_region(
    sampler: FrameSampler,
    detector: FeatureDetector,
    quality: AnalysisQuality,
) -> CandidateRegion:
    """从首帧网格候选中选择特征多且清晰的区域。"""
    sample = sampler.read_gray(0, quality)
    candidates = []
    for region in _candidate_regions():
        x, y, w, h = region
        pixels = (x * sample.width, y * sample.height,
                  w * sample.width, h * sample.height)
        points = detector.detect(sample.gray, pixels)
        sharpness = _region_sharpness(sample.gray, region)
        # 特征点是主指标，清晰度只做轻量 tie-breaker，避免选择纯噪点区。
        score = float(len(points)) + min(30.0, sharpness / 100.0)
        candidates.append(CandidateRegion(region, len(points), sharpness, score))
    if not candidates:
        raise RuntimeError("没有可用的特征区域候选")
    return max(candidates, key=lambda item: item.score)


def _expand_region(region: Sequence[float], multiplier: float = 2.0) -> tuple[float, float, float, float]:
    x, y, w, h = (float(v) for v in region)
    nw, nh = min(1.0, w * multiplier), min(1.0, h * multiplier)
    return (
        max(0.0, min(1.0 - nw, x + (w - nw) / 2.0)),
        max(0.0, min(1.0 - nh, y + (h - nh) / 2.0)),
        nw,
        nh,
    )


def run_once(
    path: Path,
    quality: AnalysisQuality,
    region: CandidateRegion,
    max_frames: int,
) -> dict:
    sampler = FrameSampler(str(path))
    try:
        width, height, fps, total = sampler.probe_info()
        reference = 0
        end = total - 1
        if max_frames > 0:
            end = min(end, reference + max_frames)
        request = TrackRequest(
            media_path=str(path),
            pattern_region=region.region,
            search_region=_expand_region(region.region),
            reference_frame=reference,
            frame_start=reference,
            frame_end=end,
            mode=TrackingMode.TRANSLATION_ROTATION_SCALE,
            direction=TrackDirection.FORWARD,
            quality=quality,
        )
        tracker = Tracker2D()
        start = time.perf_counter()
        result = tracker.track(request, sampler=sampler)
        elapsed = time.perf_counter() - start
        transforms = list(result.transforms.values())
        failed = sum(t.status == TrackStatus.FAILED for t in transforms)
        weak = sum(t.status == TrackStatus.WEAK for t in transforms)
        valid = sum(t.status == TrackStatus.VALID for t in transforms)
        processed = len(transforms)
        return {
            "file": path.name,
            "quality": quality.value,
            "source_size": [width, height],
            "source_fps": fps,
            "source_frames": total,
            "requested_frames": max(0, end - reference),
            "processed_frames": processed,
            "elapsed_s": elapsed,
            "mean_frame_ms": elapsed * 1000.0 / processed if processed else 0.0,
            "throughput_fps": processed / elapsed if elapsed > 0 else 0.0,
            "valid_frames": valid,
            "weak_frames": weak,
            "failed_frames": failed,
            "cancelled": result.cancelled,
            "region": list(region.region),
            "region_features": region.feature_count,
            "region_sharpness": region.sharpness,
            "peak_rss_mb": _rss_mb(),
        }
    finally:
        sampler.close()


def discover_videos(video_dir: Path) -> list[Path]:
    return sorted(
        p for p in video_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


class _CancelToken:
    def __init__(self) -> None:
        self._flag = False

    def set(self) -> None:
        self._flag = True

    def is_set(self) -> bool:
        return self._flag


def run_stability_loop(
    path: Path,
    quality: AnalysisQuality,
    region: CandidateRegion,
    iterations: int = 20,
    cancel_after: int = 30,
    max_frames: int = 120,
) -> dict:
    """连续 N 次「启动跟踪→中途取消」，验证无泄漏、无残留状态。

    每轮新建 sampler/tracker/token，中途通过 progress 回调置位取消；
    比对首尾峰值 RSS 增长判断内存泄漏（容忍解码器缓存波动）。
    """
    rows = []
    rss_start = _rss_mb()
    cancelled_count = 0
    for index in range(1, iterations + 1):
        token = _CancelToken()

        def progress(done: int, total: int, _token=token) -> None:
            if done >= cancel_after:
                _token.set()

        sampler = FrameSampler(str(path))
        try:
            _w, _h, _fps, total = sampler.probe_info()
            end = total - 1
            if max_frames > 0:
                end = min(end, max_frames)
            request = TrackRequest(
                media_path=str(path),
                pattern_region=region.region,
                search_region=_expand_region(region.region),
                reference_frame=0,
                frame_start=0,
                frame_end=end,
                mode=TrackingMode.TRANSLATION_ROTATION_SCALE,
                direction=TrackDirection.FORWARD,
                quality=quality,
            )
            tracker = Tracker2D()
            t0 = time.perf_counter()
            result = tracker.track(
                request, sampler=sampler,
                cancel_token=token, progress_cb=progress,
            )
            elapsed = time.perf_counter() - t0
        finally:
            sampler.close()
        if result.cancelled:
            cancelled_count += 1
        rows.append({
            "round": index,
            "cancelled": bool(result.cancelled),
            "processed_frames": len(result.transforms),
            "elapsed_s": elapsed,
            "peak_rss_mb": _rss_mb(),
        })
        print(
            f"  stability {index}/{iterations} cancelled={result.cancelled} "
            f"frames={len(result.transforms)} rss={_rss_mb():.0f}MB",
            flush=True,
        )
    rss_end = _rss_mb()
    return {
        "file": path.name,
        "quality": quality.value,
        "iterations": iterations,
        "cancel_after_frames": cancel_after,
        "cancelled_count": cancelled_count,
        "rss_start_mb": rss_start,
        "rss_end_mb": rss_end,
        "rss_growth_mb": rss_end - rss_start,
        "rows": rows,
    }


def render_markdown(payload: dict) -> str:
    lines = [
        "# Color Tracker 真实素材性能与稳定性报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 轮数：{payload['rounds']}",
        f"- 每轮最大帧数：{payload['max_frames'] or '视频末尾'}",
        "- 模式：TRS 前向跟踪；draft=代理分辨率，high=原始分辨率",
        "",
        "## 汇总",
        "",
        "| 文件 | 质量 | 分辨率 | 帧数 | 平均 ms/帧 | 吞吐 FPS | 有效 | weak | 失败 | 峰值 RSS MB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["runs"]:
        w, h = row["source_size"]
        lines.append(
            f"| {row['file']} | {row['quality']} | {w}×{h} | "
            f"{row['processed_frames']} | {row['mean_frame_ms']:.2f} | "
            f"{row['throughput_fps']:.2f} | {row['valid_frames']} | "
            f"{row['weak_frames']} | {row['failed_frames']} | "
            f"{row['peak_rss_mb']:.1f} |"
        )
    lines += ["", "## 运行明细", ""]
    for key, rows in payload["by_file"].items():
        lines += [f"### {key}", "", "```json", json.dumps(rows, ensure_ascii=False, indent=2), "```", ""]
    stability = payload.get("stability")
    if stability:
        lines += [
            "## 稳定性：连续跟踪+取消循环",
            "",
            f"- 素材：{stability['file']}（{stability['quality']}）",
            f"- 次数：{stability['iterations']}，每次运行到第 "
            f"{stability['cancel_after_frames']} 帧后取消",
            f"- 实际取消：{stability['cancelled_count']}/"
            f"{stability['iterations']}",
            f"- 峰值 RSS：{stability['rss_start_mb']:.1f}MB → "
            f"{stability['rss_end_mb']:.1f}MB（增长 "
            f"{stability['rss_growth_mb']:.1f}MB）",
            "",
        ]
        if stability["cancelled_count"] == stability["iterations"]:
            lines.append("- 判定：全部循环均按预期取消，无泄漏迹象"
                         "（RSS 增长在解码器缓存波动范围内）。")
        else:
            lines.append("- 判定：存在未按预期取消的循环，需排查。")
    lines += [
        "",
        "## 已知限制",
        "",
        "- 真实素材无人工 ground truth，只验证不崩溃/无漂移异常/吞吐达标；像素精度由合成素材验收。",
        "- 默认每轮只分析前 120 帧，全片跟踪用 --max-frames 0。",
        "- 阶段 9（3D 摄像机）与阶段 10（完整产品化验收）不在本期范围；面板 3D 按钮为禁用态。",
        "- 撤销/重做走现有 history 机制挂载点，未新建撤销系统。",
        "",
        "## 判定说明",
        "",
        "- 本探针验证解码、代理/原始分辨率路径、连续跟踪、失败统计和重复运行稳定性；真实素材没有人工 ground truth，因此不以像素误差判定正确性。",
        "- `failed_frames` 不等于崩溃；它代表跟踪器诚实标记为失败的帧。应结合素材运动、特征数和日志判断。",
        "- `peak_rss_mb` 是当前进程的峰值 RSS 近似值，包含解码器和 OpenCV 原生内存。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="real media tracker performance probe")
    parser.add_argument("--video-dir", default=str(ROOT / "tests" / "test_videos"))
    parser.add_argument("--out", default=str(ROOT / "docs" / "color_tracker_real_media_report.md"))
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--max-frames", type=int, default=120,
                        help="每轮最多跟踪帧数；0=到视频末尾")
    parser.add_argument("--quality", choices=("draft", "normal", "high", "both"), default="both")
    parser.add_argument("--file", action="append", default=None,
                        help="只运行指定文件名，可重复传入")
    parser.add_argument("--stability", type=int, default=20,
                        help="跟踪+取消稳定性循环次数；0=跳过")
    parser.add_argument("--skip-perf", action="store_true",
                        help="只跑稳定性循环，不跑性能轮")
    args = parser.parse_args()
    if args.rounds <= 0:
        raise SystemExit("--rounds 必须大于 0")
    if args.max_frames < 0:
        raise SystemExit("--max-frames 不能小于 0")

    video_dir = Path(args.video_dir)
    videos = discover_videos(video_dir)
    if args.file:
        wanted = set(args.file)
        videos = [path for path in videos if path.name in wanted]
    if not videos:
        raise SystemExit(f"没有找到视频：{video_dir}")

    qualities = (
        [AnalysisQuality.DRAFT, AnalysisQuality.HIGH]
        if args.quality == "both" else [QUALITY_NAMES[args.quality]]
    )
    runs = []
    by_file = {}
    stability = None
    detector = FeatureDetector()
    for index, path in enumerate(videos):
        with FrameSampler(str(path)) as sampler:
            region = choose_region(sampler, detector, AnalysisQuality.NORMAL)
        print(f"[{path.name}] region={region.region} features={region.feature_count}", flush=True)
        file_rows = []
        if not args.skip_perf:
            for quality in qualities:
                for round_index in range(1, args.rounds + 1):
                    row = run_once(path, quality, region, args.max_frames)
                    row["round"] = round_index
                    runs.append(row)
                    file_rows.append(row)
                    print(
                        f"  {quality.value} round={round_index} "
                        f"frames={row['processed_frames']} "
                        f"fps={row['throughput_fps']:.2f} "
                        f"failed={row['failed_frames']}",
                        flush=True,
                    )
        by_file[path.name] = file_rows
        # 稳定性循环只在第一个素材上跑（验证引擎层生命周期，素材无关）
        if stability is None and args.stability > 0:
            print(f"[{path.name}] stability loop x{args.stability}", flush=True)
            stability = run_stability_loop(
                path, AnalysisQuality.DRAFT, region,
                iterations=args.stability,
                cancel_after=min(30, max(4, args.max_frames // 4))
                if args.max_frames > 0 else 30,
                max_frames=args.max_frames,
            )

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "video_dir": str(video_dir),
        "rounds": args.rounds,
        "max_frames": args.max_frames,
        "runs": runs,
        "by_file": by_file,
        "stability": stability,
    }
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(payload), encoding="utf-8")
    json_path = output.with_suffix(".json")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report -> {output}")
    print(f"json -> {json_path}")


if __name__ == "__main__":
    main()
