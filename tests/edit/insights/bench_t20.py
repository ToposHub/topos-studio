"""T20 · 性能基准脚本（独立运行，非 pytest）。

记录计划 T20 验收要求的量：100 / 1万（可选10万）实体下的
首屏（capture + 索引构建）、搜索 p50/p95、RSS。纯数据路径，无 Qt/GPU。

用法：
    .venv/bin/python tests/edit/insights/bench_t20.py [out_json]
    INSIGHTS_SCALE_100K=1 .venv/bin/python tests/edit/insights/bench_t20.py  # 追加10万
"""

from __future__ import annotations

import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.edit.insights.scale_fixture import build_scale_state  # noqa: E402

from src.features.edit.insights.capture import capture_readonly_state  # noqa: E402
from src.features.edit.insights.index import SearchIndexCache  # noqa: E402
from src.features.edit.insights.search import SearchQuery, unified_search  # noqa: E402
from src.features.edit.insights.stats import compute_usage_rhythm  # noqa: E402


def _rss_mb() -> float:
    # macOS: ru_maxrss 单位是字节；Linux 是 KB。取 max 更保守。
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def bench_scale(n_clips: int, search_iterations: int = 50) -> dict:
    t0 = time.perf_counter()
    sc = build_scale_state(n_clips)
    fixture_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    snapshot = capture_readonly_state(sc.state, capture_id=f"bench-{n_clips}")
    capture_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    cache = SearchIndexCache()
    cache.rows_for(snapshot)
    index_s = time.perf_counter() - t0
    cache.rows_for(snapshot)  # 二次命中

    # 搜索：一半命中 cue（common-word-3），一半空结果词（miss-all）
    hit_times: list[float] = []
    miss_times: list[float] = []
    for _ in range(search_iterations // 2):
        t0 = time.perf_counter()
        unified_search(snapshot, SearchQuery(text="common-word-3"))
        hit_times.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        unified_search(snapshot, SearchQuery(text="不存在的词xyz"))
        miss_times.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    compute_usage_rhythm(snapshot)
    stats_s = time.perf_counter() - t0

    return {
        "n_clips": n_clips,
        "n_cues": sc.n_cues,
        "fixture_build_s": round(fixture_s, 4),
        "capture_s": round(capture_s, 4),
        "index_build_s": round(index_s, 4),
        "index_cache": cache.stats,
        "search_hit_p50_ms": round(_percentile(hit_times, 0.50) * 1000, 2),
        "search_hit_p95_ms": round(_percentile(hit_times, 0.95) * 1000, 2),
        "search_miss_p50_ms": round(_percentile(miss_times, 0.50) * 1000, 2),
        "search_miss_p95_ms": round(_percentile(miss_times, 0.95) * 1000, 2),
        "stats_s": round(stats_s, 4),
        "rss_max_mb": round(_rss_mb(), 1),
    }


def main() -> int:
    scales = [100, 10_000]
    if os.environ.get("INSIGHTS_SCALE_100K") == "1":
        scales.append(100_000)
    results = []
    for n in scales:
        print(f"benchmarking n={n} ...", flush=True)
        results.append(bench_scale(n))
    report = {
        "machine": "Mac Intel x86_64, macOS 15.3.1, 64GB, Python 3.12.14",
        "note": "纯数据路径（无 Qt/GPU/媒体解码）；fixture 构建时间仅参考",
        "results": results,
    }
    out = (
        sys.argv[1]
        if len(sys.argv) > 1
        else str(Path(__file__).resolve().parents[3]
                 / "docs/validation/upgrade_T/T20/bench.json")
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
