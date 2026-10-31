#!/usr/bin/env python3
"""D1 播放解码 p99 帧预算门禁（BENCH-0002）。

读取 ``bench_out/d1_playback/**/result.json``（bench_topos_playback_path.py
产物），对每轮 sequential run 的 ``p99_ms`` 按 30fps 帧预算判定：
预算默认 **33.3 ms**（docs/4k-playback-optimization.md §3.1；可用
``--budget-ms`` 覆盖）。任何一轮 p99 超预算即打印明细并以 **exit 1**
失败——已知案例：4K 4444xq 第二轮 p99=36.638 ms（REGRESSION_BASELINE.md
ANC-04 / BENCH-0002）。

口径说明：
- 只判定 ``sequential_runs[*].p99_ms``（顺序播放口径）；``random_seek``
  段是 seek+解码口径、量级数百 ms，不在播放帧预算内，仅计数汇总。
- 缺 ``p99_ms`` 字段的 run 计入 ``missing_p99``，不判失败（旧产物兼容）。

用法：
    .venv/bin/python tools/check_decode_budget.py                 # 全部
    .venv/bin/python tools/check_decode_budget.py --budget-ms 30   # 收紧
    .venv/bin/python tools/check_decode_budget.py --min-height 2160  # 仅 4K

退出码：0=全部在预算内；1=存在超支；2=root 下没找到任何 result.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "bench_out" / "d1_playback"
DEFAULT_BUDGET_MS = 33.3  # 30fps 帧预算（1000/30 ≈ 33.33，取 33.3）


def iter_result_files(root: Path):
    """root 下全部 result.json（按路径排序，保证输出稳定）。"""
    return sorted(root.rglob("result.json"))


def load_record(path: Path) -> dict | None:
    """读取并解析 result.json；坏文件返回 None（调用方计入 parse_errors）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def record_label(path: Path, record: dict) -> str:
    """结果的人类可读标识（P0-04 树的 <head>/<dylib8>/<manifest8>/<utc>）。

    路径深度与标准树不一致（如测试 tmp 目录）时回退为相对路径全文。
    """
    try:
        rel = path.relative_to(PROJECT_ROOT)
    except ValueError:
        rel = path
    if len(rel.parts) >= 5:
        return "/".join(rel.parts[-5:-1])
    return str(rel)


def extract_p99_checks(record: dict) -> list[dict]:
    """从一条 result 记录抽取待判定的 p99 条目（sequential 口径）。"""
    checks: list[dict] = []
    runs = record.get("sequential_runs")
    if not isinstance(runs, list):
        return checks
    for index, run in enumerate(runs):
        if not isinstance(run, dict) or "p99_ms" not in run:
            continue
        p99 = run["p99_ms"]
        if not isinstance(p99, (int, float)) or isinstance(p99, bool):
            continue
        checks.append({"phase": "sequential", "run_index": index,
                       "p99_ms": float(p99)})
    return checks


def record_geometry(record: dict) -> dict:
    """结果的输入几何（用于 --min-height 过滤与明细打印）。"""
    info = record.get("input", {})
    if not isinstance(info, dict):
        info = {}
    return {
        "width": info.get("width"),
        "height": info.get("height"),
        "pixel_format": info.get("pixel_format"),
        "bit_depth": info.get("bit_depth"),
        "measured_frames": info.get("measured_frames"),
    }


def collect(root: Path, budget_ms: float, min_height: int = 0) -> dict:
    """扫描 root 下全部 result.json，返回判定汇总（纯函数，供单测）。

    返回结构::

        {
          "budget_ms": float,
          "files": int,              # 找到的 result.json 数
          "parse_errors": int,       # 无法解析的文件数
          "checked": int,            # 实际判定的 p99 条目数
          "missing_p99": int,        # 无 p99_ms 字段被跳过的 run 数
          "skipped_by_height": int,  # 被 --min-height 过滤的文件数
          "seek_runs": int,          # random_seek 段条目数（只计数不判定）
          "violations": [ {path, label, run_index, p99_ms, over_ms, ...} ]
        }
    """
    summary = {
        "budget_ms": float(budget_ms),
        "files": 0,
        "parse_errors": 0,
        "checked": 0,
        "missing_p99": 0,
        "skipped_by_height": 0,
        "seek_runs": 0,
        "violations": [],
    }
    for path in iter_result_files(root):
        summary["files"] += 1
        record = load_record(path)
        if record is None:
            summary["parse_errors"] += 1
            continue
        geom = record_geometry(record)
        height = geom["height"]
        if min_height and (not isinstance(height, (int, float))
                           or height < min_height):
            summary["skipped_by_height"] += 1
            continue
        runs = record.get("sequential_runs")
        if isinstance(runs, list):
            summary["missing_p99"] += sum(
                1 for run in runs
                if not isinstance(run, dict) or "p99_ms" not in run)
        seek = record.get("random_seek")
        if isinstance(seek, dict) and "p99_ms" in seek:
            summary["seek_runs"] += 1
        for check in extract_p99_checks(record):
            summary["checked"] += 1
            if check["p99_ms"] > budget_ms:
                config = record.get("config", {})
                if not isinstance(config, dict):
                    config = {}
                summary["violations"].append({
                    "path": str(path),
                    "label": record_label(path, record),
                    "run_index": check["run_index"],
                    "p99_ms": check["p99_ms"],
                    "over_ms": check["p99_ms"] - budget_ms,
                    "budget_ms": float(budget_ms),
                    "geometry": geom,
                    "config": {
                        "threads": config.get("threads"),
                        "read_ahead_requested": config.get(
                            "read_ahead_requested"),
                        "repeats": config.get("repeats"),
                    },
                })
    return summary


def format_report(summary: dict) -> str:
    """把 collect() 汇总渲染为报告文本（纯函数，供单测）。"""
    budget = summary["budget_ms"]
    lines = [
        f"[budget] p99 帧预算 = {budget:.3f} ms（sequential 口径）",
        f"[scan] result.json 文件 {summary['files']} 个，"
        f"判定 p99 条目 {summary['checked']} 条"
        f"（跳过缺 p99 字段 {summary['missing_p99']} 条、"
        f"高度过滤 {summary['skipped_by_height']} 文件、"
        f"解析失败 {summary['parse_errors']} 文件；"
        f"random_seek 段 {summary['seek_runs']} 条仅计数不判定）",
    ]
    violations = summary["violations"]
    if not violations:
        lines.append("[pass] 全部 p99 在预算内")
        return "\n".join(lines)
    lines.append(f"[fail] 超支 {len(violations)} 条：")
    for v in violations:
        g = v["geometry"]
        c = v["config"]
        lines.append(
            f"  OVER {v['label']} run#{v['run_index']}: "
            f"p99={v['p99_ms']:.3f} ms > {v['budget_ms']:.3f} ms "
            f"(+{v['over_ms']:.3f}) | {g['width']}x{g['height']} "
            f"{g['pixel_format']} bd={g['bit_depth']} "
            f"frames={g['measured_frames']} "
            f"threads={c['threads']} read_ahead={c['read_ahead_requested']}"
            f" | {v['path']}")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="D1 播放解码 p99 帧预算门禁（读 bench_out/d1_playback/"
                    "并按 33.3ms 预算判定；BENCH-0002）")
    ap.add_argument("--root", default=str(DEFAULT_ROOT),
                    help="扫描根目录（默认 bench_out/d1_playback；"
                         "任意目录树均可，只要含 result.json）")
    ap.add_argument("--budget-ms", type=float, default=DEFAULT_BUDGET_MS,
                    help=f"p99 预算（ms；默认 {DEFAULT_BUDGET_MS} = "
                         "30fps 帧预算）")
    ap.add_argument("--min-height", type=int, default=0,
                    help="只判定输入高度 >= 该值的结果（0=不过滤；"
                         "如 2160 = 仅 4K）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.budget_ms <= 0:
        raise SystemExit("budget-ms 必须为正数")
    if args.min_height < 0:
        raise SystemExit("min-height 必须 >= 0")
    root = Path(args.root)
    if not root.is_dir():
        print(f"[error] 扫描根不存在：{root}", file=sys.stderr)
        return 2
    summary = collect(root, args.budget_ms, args.min_height)
    if summary["files"] == 0:
        print(f"[error] root 下未找到任何 result.json：{root}", file=sys.stderr)
        return 2
    print(format_report(summary))
    if summary["violations"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
