"""
Performance Budget Registry (P0-12)
性能预算注册表：source-controlled 的场景 SLA 硬门/软门与免责条件
（性能总计划 §6 P0-12 / §3.2 / 决策 D-04）。

## 职责

- 加载 `config/performance_budgets.json`（预算 ID、场景、指标、硬门/软门、
  目标机类别、免责状态 known_breach）。
- evaluate_report_against_budgets(): 把 benchmark runner 的结果 JSON 对照
  预算逐项裁决（pass / soft_breach / hard_breach / known_breach / missing）。
- compare_reports(): 同机两次 runner 报告的逐指标回归对比（D-04：
  p95 退化 ≥5% 触发调查）。

## 口径

- hard_max：硬门，超了必须调查/阻止合入；known_breach 状态的预算是
  已登记的免责缺口（有 note 说明责任工作项），降级为警告。
- soft_max：软门（更紧的目标值），超了告警不阻止。
- 回归对比只做同机（platform 一致）对比；跨机器对比无意义，只报错不裁决。

## 用法

```python
from src.shared.video.performance_budgets import (
    evaluate_report_against_budgets, compare_reports, load_budgets,
)

doc = load_budgets()
verdicts = evaluate_report_against_budgets(report_json, doc)
regressions = compare_reports(new_report, old_report, threshold_pct=5.0)
```
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 预算文档 schema 版本（字段变更必须递增）。
BUDGETS_SCHEMA_VERSION = 1

#: 默认注册表路径（env 可覆盖，便于 CI 注入不同目标机的门槛集）。
DEFAULT_BUDGETS_PATH = Path(
    os.environ.get(
        "TOPOS_PERF_BUDGETS",
        str(REPO_ROOT / "config" / "performance_budgets.json"),
    )
)

#: 参与回归对比的 runner 指标（phase → metric）。
REGRESSION_METRICS = (
    ("cold_first_frame", "p95_ms"),
    ("steady_sequential", "p50_ms"),
    ("steady_sequential", "p95_ms"),
    ("seek_random", "p95_ms"),
)


@dataclass(frozen=True)
class Budget:
    """单条预算（一个场景 × 一个指标的门槛）。"""

    budget_id: str
    scenario: str
    metric: str            # runner phase.metric，如 "steady_sequential.p95_ms"
    media_glob: str        # 对 runner report 的 media file 名做 fnmatch
    hard_max_ms: float
    soft_max_ms: float
    machine_class: str = ""
    state: str = "active"  # active | known_breach
    note: str = ""
    source: str = ""


def load_budgets(path: Optional[Path] = None) -> Dict[str, Any]:
    """加载预算注册表 JSON（不存在时抛 FileNotFoundError）。"""
    p = Path(path) if path else DEFAULT_BUDGETS_PATH
    doc = json.loads(p.read_text(encoding="utf-8"))
    if int(doc.get("$schema_version", 0)) != BUDGETS_SCHEMA_VERSION:
        raise ValueError(
            f"budgets schema version mismatch in {p}: "
            f"{doc.get('$schema_version')} != {BUDGETS_SCHEMA_VERSION}"
        )
    return doc


def iter_budgets(doc: Dict[str, Any], scenario: Optional[str] = None) -> List[Budget]:
    """遍历预算条目（可按场景过滤）；坏条目直接抛错（注册表必须干净）。"""
    out: List[Budget] = []
    for raw in doc.get("budgets", []):
        b = Budget(
            budget_id=str(raw["budget_id"]),
            scenario=str(raw["scenario"]),
            metric=str(raw["metric"]),
            media_glob=str(raw["media_glob"]),
            hard_max_ms=float(raw["hard_max_ms"]),
            soft_max_ms=float(raw["soft_max_ms"]),
            machine_class=str(raw.get("machine_class", "")),
            state=str(raw.get("state", "active")),
            note=str(raw.get("note", "")),
            source=str(raw.get("source", "")),
        )
        if b.state not in ("active", "known_breach"):
            raise ValueError(f"bad budget state: {b.budget_id} -> {b.state!r}")
        if scenario is None or b.scenario == scenario:
            out.append(b)
    return out


def _resolve_metric(media_entry: Dict[str, Any], metric: str) -> Optional[float]:
    """从 media entry 按稳定 phase.metric 取数值（缺失返回 None）。"""
    phases = media_entry.get("phases") or {}
    phase_name, _, key = metric.partition(".")
    phase = phases.get(phase_name)
    if not isinstance(phase, dict):
        return None
    value = phase.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def evaluate_report_against_budgets(
    report: Dict[str, Any],
    doc: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """把 runner 报告对照注册表逐条裁决。

    返回每条预算一个 verdict dict：
    - pass: 达标；soft_breach: 超 soft 未超 hard（active）；
    - hard_breach: 超 hard（active）→ 门禁失败；
    - known_breach: 超 hard 但预算已登记免责（附带 note）→ 警告不阻止；
    - missing: 场景适用但报告里没有对应 media/指标 → 门禁失败（防静默漏跑）；
    - not_applicable: 报告未声明覆盖该预算的场景（runner 每次只跑部分
      场景，不适用 ≠ 失败），不计入门禁结论。

    AUD-39207: media_glob 命中多个 media 文件时，取所有命中样本的
    最差值（延迟类指标取最大值）裁决——任何文件劣化都不得逃过门禁。
    """
    if doc is None:
        doc = load_budgets()
    media_entries = report.get("media", [])
    # runner 报告声明本批覆盖的场景；老报告没有该字段时视为全部适用。
    report_scenarios = {str(s) for s in (report.get("scenarios") or [])}
    verdicts: List[Dict[str, Any]] = []
    for b in iter_budgets(doc):
        if report_scenarios and b.scenario not in report_scenarios:
            verdicts.append({
                "budget_id": b.budget_id, "scenario": b.scenario,
                "metric": b.metric, "verdict": "not_applicable",
                "value": None, "hard_max_ms": b.hard_max_ms,
                "soft_max_ms": b.soft_max_ms, "note": b.note,
            })
            continue
        matched = [m for m in media_entries
                   if fnmatch.fnmatch(Path(str(m.get("file", ""))).name,
                                      b.media_glob)]
        # AUD-39207: 同一 glob 命中多个 media 文件时必须全部裁决。
        # 此前只取第一个有值的样本，其余文件即使劣化到 hard_breach
        # 也逃过门禁。延迟类指标取所有命中样本的最差值（最大值），
        # verdict 跟随最差样本，media 记录触发该值的文件。
        value: Optional[float] = None
        sample_file = ""
        for m in matched:
            v = _resolve_metric(m, b.metric)
            if v is not None and (value is None or v > value):
                value = v
                sample_file = str(m.get("file", ""))
        if value is None:
            verdicts.append({
                "budget_id": b.budget_id, "scenario": b.scenario,
                "metric": b.metric, "verdict": "missing",
                "value": None, "hard_max_ms": b.hard_max_ms,
                "soft_max_ms": b.soft_max_ms, "note": b.note,
            })
            continue
        if value > b.hard_max_ms:
            verdict = ("known_breach" if b.state == "known_breach"
                       else "hard_breach")
        elif value > b.soft_max_ms:
            verdict = "soft_breach"
        else:
            verdict = "pass"
        verdicts.append({
            "budget_id": b.budget_id, "scenario": b.scenario,
            "metric": b.metric, "verdict": verdict,
            "value": value, "hard_max_ms": b.hard_max_ms,
            "soft_max_ms": b.soft_max_ms, "media": sample_file,
            "note": b.note,
        })
    return verdicts


def compare_reports(
    report: Dict[str, Any],
    reference: Dict[str, Any],
    threshold_pct: float = 5.0,
) -> List[Dict[str, Any]]:
    """同机两次 runner 报告的逐指标回归对比（D-04）。

    延迟类指标只关心恶化（new > ref）：退化超过 threshold_pct 记
    regression。platform 不一致的指标对直接跳过并记 cross_machine
    （不做跨机裁决）。reference 值 <= 0 的跳过（无基线意义）。
    """
    ref_by_file = {
        Path(str(m.get("file", ""))).name: m for m in reference.get("media", [])
    }
    out: List[Dict[str, Any]] = []
    for m in report.get("media", []):
        name = Path(str(m.get("file", ""))).name
        ref_m = ref_by_file.get(name)
        if ref_m is None:
            continue
        same_machine = (
            str((report.get("machine") or {}).get("platform", "")) ==
            str((reference.get("machine") or {}).get("platform", ""))
        )
        for phase, key in REGRESSION_METRICS:
            new_v = _resolve_metric(m, f"{phase}.{key}")
            ref_v = _resolve_metric(ref_m, f"{phase}.{key}")
            if new_v is None or ref_v is None or ref_v <= 0:
                continue
            delta_pct = (new_v - ref_v) / ref_v * 100.0
            if not same_machine:
                out.append({
                    "media": name, "phase": phase, "metric": key,
                    "new_ms": new_v, "ref_ms": ref_v,
                    "delta_pct": round(delta_pct, 2),
                    "verdict": "cross_machine",
                })
            elif delta_pct >= threshold_pct:
                out.append({
                    "media": name, "phase": phase, "metric": key,
                    "new_ms": new_v, "ref_ms": ref_v,
                    "delta_pct": round(delta_pct, 2),
                    "verdict": "regression",
                })
    return out


def gate_summary(verdicts: List[Dict[str, Any]],
                 regressions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """聚合成门禁结论：0=pass / 1=soft 或 regression 需调查 / 2=hard 失败。"""
    hard = [v for v in verdicts if v["verdict"] == "hard_breach"]
    missing = [v for v in verdicts if v["verdict"] == "missing"]
    soft = [v for v in verdicts if v["verdict"] == "soft_breach"]
    known = [v for v in verdicts if v["verdict"] == "known_breach"]
    code = 0
    if hard or missing:
        code = 2
    elif soft or regressions:
        code = 1
    return {
        "exit_code": code,
        "hard_breaches": hard,
        "missing": missing,
        "soft_breaches": soft,
        "known_breaches": known,
        "regressions": regressions,
    }
