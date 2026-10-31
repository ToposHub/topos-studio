#!/usr/bin/env python3
"""
Performance Regression Gate (P0-09 本地门禁 / P0-12 消费端)
对照预算注册表与参照报告裁决 benchmark runner 结果；同机 p95 退化
≥threshold 触发调查（性能总计划 §6 P0-09 / 决策 D-04）。

## 用法

    # 只对预算注册表裁决
    .venv/bin/python tools/perf_regression_gate.py --report bench_out/p0_runner/p0_runner_X.json

    # 预算裁决 + 与上一次报告的同机回归对比
    .venv/bin/python tools/perf_regression_gate.py \
        --report bench_out/p0_runner/new.json \
        --reference bench_out/p0_runner/old.json --threshold-pct 5.0

退出码：0 = 通过；1 = soft_breach / 回归退化（需调查，可解释后放行）；
2 = hard_breach / 缺报告指标（阻止）；3 = 用法/输入错误。

## 约束

- 纯 CPU 逻辑（计划 P0-09：CPU 机器做 schema/逻辑门；GPU 真机 nightly
  另行接入）。跨机器对比不裁决（只标 cross_machine）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.shared.video.performance_budgets import (  # noqa: E402
    compare_reports,
    evaluate_report_against_budgets,
    gate_summary,
    load_budgets,
)


def _load_report(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"report not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True,
                        help="benchmark runner 结果 JSON")
    parser.add_argument("--reference", default="",
                        help="参照 runner JSON（同机回归对比，可选）")
    parser.add_argument("--budgets", default="",
                        help="预算注册表 JSON（默认 config/performance_budgets.json）")
    parser.add_argument("--threshold-pct", type=float, default=5.0,
                        help="同机退化告警阈值百分比（默认 5.0，D-04）")
    args = parser.parse_args(argv)

    try:
        report = _load_report(args.report)
    except (OSError, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"gate input error: {e}", file=sys.stderr)
        return 3

    doc = None
    if args.budgets:
        try:
            doc = load_budgets(Path(args.budgets))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"budgets error: {e}", file=sys.stderr)
            return 3

    verdicts = evaluate_report_against_budgets(report, doc)
    regressions = []
    if args.reference:
        try:
            reference = _load_report(args.reference)
        except (OSError, json.JSONDecodeError, FileNotFoundError) as e:
            print(f"reference error: {e}", file=sys.stderr)
            return 3
        regressions = compare_reports(report, reference, args.threshold_pct)

    summary = gate_summary(verdicts, regressions)

    print("== budget verdicts ==")
    for v in verdicts:
        line = (f"[{v['verdict']:>12}] {v['budget_id']} "
                f"{v['metric']} = {v['value']}")
        if v["value"] is not None:
            line += f" (soft<={v['soft_max_ms']} hard<={v['hard_max_ms']})"
        if v.get("note") and v["verdict"] in ("known_breach", "hard_breach"):
            line += f"\n             note: {v['note']}"
        print(line)

    if regressions:
        print(f"\n== regressions (same-machine, >={args.threshold_pct}% slower) ==")
        for r in regressions:
            print(f"[{r['verdict']:>12}] {r['media']} {r['phase']}.{r['metric']}: "
                  f"{r['ref_ms']}ms -> {r['new_ms']}ms ({r['delta_pct']:+.1f}%)")

    print(f"\ngate exit = {summary['exit_code']} "
          f"(hard={len(summary['hard_breaches'])} "
          f"missing={len(summary['missing'])} "
          f"soft={len(summary['soft_breaches'])} "
          f"known={len(summary['known_breaches'])} "
          f"regressions={len(summary['regressions'])})")
    return summary["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
