"""
Benchmark Runner (P0-08)
冷/热/seek/长跑统一基准协议 runner（性能总计划 §6 P0-08 / §3.2 验收口径）。

## 协议

对每个媒体条目（来自 P0-06 冻结矩阵）跑四个阶段：

- cold     ：全新 source 的首次 open + 首帧延迟（冷缓存首帧 p50/p95）。
- steady   ：固定 warmup 后顺序读 N 帧，逐帧计时 p50/p95/max（稳态吞吐）。
- seek     ：随机 seek + 读帧延迟 p50/p95（随机访问）。
- longrun  ：顺序读 M 帧并跟踪 RSS 前/后差（长跑内存趋势指标）。

固定参数（默认）：warmup=10、steady_frames=120、seek_samples=30、
longrun_frames=600（clamp 到素材剩余长度，避免回绕）、cold 固定 3 次。
所有结果带 p50/p95/max、机器信息、git commit、媒体指纹引用，写 JSON artifact。

## 用法

    # 跑 B-01..B-04 全部场景（默认输出 bench_out/p0_runner/p0_runner_<ts>.json）
    .venv/bin/python -m tests.benchmark.benchmark_runner --scenario B-01 B-02 B-03 B-04

    # 只跑单个文件，快速冒烟
    .venv/bin/python -m tests.benchmark.benchmark_runner --file tests/test_videos/2K_h264.mp4 --quick

## 约束

- 纯 headless（FFmpegMediaSource），不依赖 GUI/GL。
- 历史报告只做对照：结果 JSON 带 schema_version/commit，不覆盖旧文件。
- "实时"结论必须来自 p95 而非平均值（计划 §3.2）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 结果 JSON schema 版本（字段变更必须递增）。
RUNNER_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# 协议参数（固定默认；--quick 缩样冒烟用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtocolConfig:
    warmup_frames: int = 10
    steady_frames: int = 120
    seek_samples: int = 30
    longrun_frames: int = 600

    @classmethod
    def quick(cls) -> "ProtocolConfig":
        return cls(warmup_frames=5, steady_frames=30, seek_samples=8,
                   longrun_frames=60)


# ---------------------------------------------------------------------------
# 场景 → 媒体映射（与 P0-06 冻结清单的 B-xx 对应）
# ---------------------------------------------------------------------------

SCENARIO_MEDIA: Dict[str, List[str]] = {
    "B-01": ["tests/test_videos/2K_h264.mp4"],
    "B-02": ["tests/test_videos/4K_H265.mov"],
    "B-03": ["tests/test_videos/4K_prores422HQ.mov",
             "tests/test_videos/4K_prores422LT.mov",
             "tests/test_videos/4K_DNxHR HQX 10bit.mov"],
    "B-04": ["tests/test_videos/4K_topos_422_HQ.mov",
             "tests/test_videos/4K_topos_422_Proxy.mov"],
}

DEFAULT_OUTPUT_DIR = REPO_ROOT / "bench_out" / "p0_runner"


# ---------------------------------------------------------------------------
# 统计辅助
# ---------------------------------------------------------------------------


def _percentile(sorted_values: List[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac


def _stats(values_ms: List[float]) -> Dict[str, Any]:
    if not values_ms:
        return {"n": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0,
                "mean_ms": 0.0}
    s = sorted(values_ms)
    return {
        "n": len(s),
        "p50_ms": round(_percentile(s, 50), 3),
        "p95_ms": round(_percentile(s, 95), 3),
        "max_ms": round(s[-1], 3),
        "mean_ms": round(statistics.fmean(s), 3),
    }


def _rss_bytes() -> Optional[int]:
    """当前进程 RSS。psutil 不可用时返回 None——报告必须显式
    rss_available=False，不能用 0 冒充"无泄漏"。"""
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 单文件四阶段
# ---------------------------------------------------------------------------


def benchmark_media_file(
    media_path: Path,
    config: ProtocolConfig,
) -> Dict[str, Any]:
    """对一个媒体文件跑 cold/steady/seek/longrun 四阶段。

    统一走 MediaSourceFactory.create（应用真实解码入口）：Topos 媒体由
    工厂路由到 libtopos_codec 的 ToposMediaSource，其余走 FFmpeg——
    这样 T-01/T-10 的结论反映产品实际路径而非绕行通道。
    """
    from src.shared.media.factory import MediaSourceFactory

    def _open_source():
        return MediaSourceFactory.create(str(media_path))

    def _display(p: Path) -> str:
        try:
            return str(p.relative_to(REPO_ROOT))
        except ValueError:
            return str(p)  # 仓库外绝对路径（--file 支持）原样记录

    result: Dict[str, Any] = {
        "file": _display(media_path),
        "phases": {},
    }

    # ---- cold：全新 source 首帧延迟（含工厂路由 + open + 首帧解码）----
    cold_ms: List[float] = []
    backend_name = ""
    for _ in range(3):
        source = _open_source()
        try:
            t0 = time.perf_counter()
            source.open()
            source.read_frame(0)
            cold_ms.append((time.perf_counter() - t0) * 1000.0)
            backend_name = type(source).__name__
        finally:
            source.close()
    result["phases"]["cold_first_frame"] = _stats(cold_ms)
    result["backend"] = backend_name

    # ---- steady：warmup 后顺序读 ----
    source = _open_source()
    try:
        source.open()
        meta = source.probe()
        total_frames = int(getattr(meta, "total_frames", 0) or 0)
        total_frames_estimated = total_frames <= 0
        if total_frames_estimated:
            total_frames = 300  # metadata 缺失时的保守上限（报告标记 estimated）
        result["meta"] = {
            "codec": getattr(meta, "codec_name", ""),
            "width": getattr(meta, "width", 0),
            "height": getattr(meta, "height", 0),
            "fps": round(float(getattr(meta, "fps", 0.0) or 0.0), 3),
            "total_frames": total_frames,
            "total_frames_estimated": total_frames_estimated,
        }

        for i in range(config.warmup_frames):
            source.read_frame(i % total_frames)
        steady_ms: List[float] = []
        base = config.warmup_frames % total_frames
        for i in range(config.steady_frames):
            t0 = time.perf_counter()
            source.read_frame((base + i) % total_frames)
            steady_ms.append((time.perf_counter() - t0) * 1000.0)
        steady_stats = _stats(steady_ms)
        steady_stats["p50_fps"] = (
            round(1000.0 / steady_stats["p50_ms"], 2)
            if steady_stats["p50_ms"] > 0 else 0.0
        )
        result["phases"]["steady_sequential"] = steady_stats

        # ---- seek：随机跳转 + 读帧 ----
        # 单样本越界/解码失败只跳过该样本（记 skipped），不拖垮整阶段。
        import random

        rng = random.Random(42)  # 固定种子保证跨机器可比
        seek_ms: List[float] = []
        seek_skipped = 0
        for _ in range(config.seek_samples):
            target = rng.randrange(0, total_frames)
            try:
                t0 = time.perf_counter()
                source.seek_frame(target)
                source.read_frame(target)
                seek_ms.append((time.perf_counter() - t0) * 1000.0)
            except Exception:  # noqa: BLE001
                seek_skipped += 1
        seek_stats = _stats(seek_ms)
        if seek_skipped:
            seek_stats["skipped"] = seek_skipped
        result["phases"]["seek_random"] = seek_stats

        # ---- longrun：顺序读 M 帧 + RSS 趋势 ----
        # 从 steady 结束位置继续顺序读；帧数 clamp 到素材剩余长度：
        # modulo 回绕会把一次大步向后 seek（长 GOP 从最近 keyframe 重解）
        # 混进顺序读统计并污染 RSS 差值。
        base = (config.warmup_frames + config.steady_frames) % total_frames
        effective_longrun = (
            max(1, min(config.longrun_frames, total_frames - base))
            if not total_frames_estimated else config.longrun_frames
        )
        rss_before = _rss_bytes()
        longrun_ms: List[float] = []
        for i in range(effective_longrun):
            t0 = time.perf_counter()
            source.read_frame((base + i) % total_frames)
            longrun_ms.append((time.perf_counter() - t0) * 1000.0)
        rss_after = _rss_bytes()
        longrun_stats = _stats(longrun_ms)
        longrun_stats["longrun_frames_effective"] = effective_longrun
        rss_available = rss_before is not None and rss_after is not None
        longrun_stats["rss_available"] = rss_available
        longrun_stats["rss_before_bytes"] = rss_before if rss_available else 0
        longrun_stats["rss_after_bytes"] = rss_after if rss_available else 0
        longrun_stats["rss_delta_bytes"] = (
            rss_after - rss_before if rss_available else None
        )
        result["phases"]["longrun"] = longrun_stats
    finally:
        source.close()
    return result


# ---------------------------------------------------------------------------
# 整批 runner
# ---------------------------------------------------------------------------


def run_benchmark(
    media_files: List[Path],
    config: ProtocolConfig,
    scenarios: List[str],
) -> Dict[str, Any]:
    """跑一批媒体文件，返回完整结果文档（含机器信息与 commit）。"""
    from tools.freeze_benchmark_media_manifest import machine_info

    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    media_results = []
    for path in media_files:
        if not path.exists():
            media_results.append({
                "file": str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path),
                "error": "file_not_found",
            })
            continue
        print(f"benchmarking {path.name} ...")
        t0 = time.perf_counter()
        try:
            entry = benchmark_media_file(path, config)
        except Exception as e:  # noqa: BLE001 单文件失败不拖垮整批
            entry = {"file": str(path), "error": f"{type(e).__name__}: {e}"}
        entry["wall_s"] = round(time.perf_counter() - t0, 2)
        media_results.append(entry)

    return {
        "$schema_version": RUNNER_SCHEMA_VERSION,
        "runner": "p0_benchmark_runner",
        "protocol": {
            "warmup_frames": config.warmup_frames,
            "steady_frames": config.steady_frames,
            "seek_samples": config.seek_samples,
            "longrun_frames": config.longrun_frames,
            "cold_repeats": 3,
        },
        "scenarios": scenarios,
        "started_at": started,
        "machine": machine_info(),
        "media": media_results,
    }


def resolve_scenario_media(scenarios: List[str]) -> List[Path]:
    files: List[Path] = []
    seen = set()
    for sid in scenarios:
        for rel in SCENARIO_MEDIA.get(sid, []):
            p = REPO_ROOT / rel
            if str(p) not in seen:
                seen.add(str(p))
                files.append(p)
    return files


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", nargs="*", default=["B-01"],
                        help="B-01..B-04 场景（默认 B-01）")
    parser.add_argument("--file", nargs="*", default=[],
                        help="显式媒体文件（覆盖场景映射）")
    parser.add_argument("--quick", action="store_true", help="缩样冒烟")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--stdout", action="store_true",
                        help="只打印不落盘")
    args = parser.parse_args(argv)

    config = ProtocolConfig.quick() if args.quick else ProtocolConfig()
    if args.file:
        media_files = [REPO_ROOT / f if not Path(f).is_absolute() else Path(f)
                       for f in args.file]
    else:
        media_files = resolve_scenario_media(args.scenario)
    if not media_files:
        parser.error("no media files resolved")

    report = run_benchmark(media_files, config, scenarios=args.scenario)

    summary_lines = []
    for entry in report["media"]:
        if "error" in entry:
            summary_lines.append(f"{entry['file']}: ERROR {entry['error']}")
            continue
        ph = entry["phases"]
        name = Path(entry["file"]).name
        rss_delta = ph["longrun"].get("rss_delta_bytes")
        rss_text = f"{rss_delta}B" if rss_delta is not None else "n/a"
        summary_lines.append(
            f"{name}: cold_p95={ph['cold_first_frame']['p95_ms']}ms "
            f"steady_p50={ph['steady_sequential']['p50_ms']}ms"
            f" ({ph['steady_sequential'].get('p50_fps', 0)}fps) "
            f"steady_p95={ph['steady_sequential']['p95_ms']}ms "
            f"seek_p95={ph['seek_random']['p95_ms']}ms "
            f"longrun_rss_delta={rss_text}"
        )
    print("\n".join(summary_lines))

    if not args.stdout:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = args.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"p0_runner_{ts}.json"
        out_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nreport -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
