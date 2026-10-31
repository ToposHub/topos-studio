"""TAEP M12 — 评估 trace（§3 架构图 "evaluation traces" / §12.2 离线评测集 / §10.1）。

持久化每次评估 run 的轨迹：run_id / 时间戳 / 用例结果 / 指标 / 质量门判定。支持按 run_id
查询 + 与上次对比回归（某指标是否退化）。

设计（遵循 .clinerules / §0 / §3 / §12.2 / §15.3）：

- **第三粒度审计**（§3 "evaluation traces"）：与既有 ``AuditStore``（per-plan-apply）、
  ``ToolCallLog``（per-tool-call）互补；本模块是 per-eval-run 的轨迹。
- **离线 eval**（§12.2："建立不含私人素材的 fixture"）：``EvalCase`` 是单个测试用例的结果
  （name/passed/duration/error），不含素材原文（§0 第四条 / §12.3 第三条）。
- **回归对比**（§12.2 量化趋势）：``compare_regression`` 对比本次与上次的指标，标出退化项。
- **可导出/删除**（§15.3："用户能导出/删除会话、偏好、审计和生成资产元数据"）：
  ``EvalTraceStore`` 支持 ``to_dict``（导出）+ ``clear``（删除）。
- **不采集 prompt/素材**（§11.1 M12 / §12.3 第三条）：trace 只记计数/比率/duration/error_code，
  不记 prompt/素材/路径原文。

与 ``QualityGateResult`` 的关系：``EvalTrace`` 内嵌一次 ``QualityGateResult``（gate 判定）+
``EvalCase`` 列表（用例明细）+ ``EvalMetricSnapshot``（指标快照，供回归对比）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.features.edit.ai_editing.quality_gate import QualityGateResult
from src.features.edit.ai_editing.release_test_matrix import MatrixResult
from src.features.edit.ai_editing.commercial_acceptance import CommercialAcceptanceReport


# ---------------------------------------------------------------------- #
# EvalCase —— 单个测试用例结果
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalCase:
    """单个评估用例的结果（§12.2："每个 Skill/子任务有输入、期望 artifact、禁止行为和可测指标"）。

    红线（§0 第四条 / §12.3 第三条）：``error_message`` 只记诊断码/简短原因，不含 prompt/
    素材/路径原文。
    """

    name: str                         # 用例名（如 "voiceover_golden_ab_variants"）
    passed: bool = True
    duration_ms: int = 0
    error_code: str = ""              # 失败时的诊断码（如 TAEP_SCHEMA_INVALID）
    error_message: str = ""           # 简短失败原因（脱敏）
    category: str = ""                # 用例类别（§12.1：安全/编辑语义/Job/上下文/UX/Skill-MCP/生成）

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "passed": self.passed,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code, "error_message": self.error_message,
            "category": self.category,
        }


# ---------------------------------------------------------------------- #
# EvalMetricSnapshot —— 指标快照（供回归对比）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalMetricSnapshot:
    """一次 eval run 的指标快照（§12.2 量化趋势）。

    从 ``QualityGateResult.metrics`` 提取的 ``name → value`` 快照，供 ``compare_regression``
    对比两次 run 的指标变化。
    """

    values: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_gate_result(cls, result: QualityGateResult) -> "EvalMetricSnapshot":
        """从 QualityGateResult 提取指标快照。"""
        return cls(values={m.name: m.value for m in result.metrics})

    def get(self, name: str) -> float | None:
        return self.values.get(name)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.values)


# ---------------------------------------------------------------------- #
# RegressionFinding —— 回归对比发现
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class RegressionFinding:
    """单个指标的回归对比发现。"""

    metric_name: str
    previous: float
    current: float
    delta: float                    # current - previous
    regressed: bool                 # 是否退化（退化方向取决于指标）
    higher_is_better: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "previous": self.previous, "current": self.current,
            "delta": self.delta, "regressed": self.regressed,
            "higher_is_better": self.higher_is_better,
        }


# ---------------------------------------------------------------------- #
# EvalTrace —— 单次评估 run 的完整轨迹
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalTrace:
    """一次评估 run 的完整轨迹（§3 "evaluation traces" / §12.2）。

    内嵌 ``QualityGateResult``（gate 判定）+ ``EvalCase`` 列表（用例明细）+
    ``EvalMetricSnapshot``（指标快照）。``run_id`` 单调，便于按时间排序。
    """

    run_id: str
    timestamp: float
    suite_name: str = ""             # eval 套件名（如 "ai_editing_regression_v1"）
    cases: tuple[EvalCase, ...] = ()
    gate_result: QualityGateResult | None = None
    metric_snapshot: EvalMetricSnapshot = field(default_factory=lambda: EvalMetricSnapshot())
    matrix_result: MatrixResult | None = None
    commercial_audit_passed: bool = False
    commercial_audit_report_id: str = ""
    commercial_acceptance: CommercialAcceptanceReport | None = None

    @property
    def passed(self) -> bool:
        """本次 run 是否通过（gate + 所有 case）。"""
        all_cases_passed = all(c.passed for c in self.cases)
        gate_passed = self.gate_result.passed if self.gate_result else True
        matrix_passed = self.matrix_result.passed if self.matrix_result is not None else True
        return all_cases_passed and gate_passed and matrix_passed

    @property
    def case_count(self) -> int:
        return len(self.cases)

    @property
    def failure_count(self) -> int:
        return sum(1 for c in self.cases if not c.passed)

    @property
    def total_duration_ms(self) -> int:
        return sum(c.duration_ms for c in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "suite_name": self.suite_name,
            "cases": [c.to_dict() for c in self.cases],
            "gate_result": self.gate_result.to_dict() if self.gate_result else None,
            "metric_snapshot": self.metric_snapshot.to_dict(),
            "matrix_result": self.matrix_result.to_dict() if self.matrix_result else None,
            "commercial_audit_passed": self.commercial_audit_passed,
            "commercial_audit_report_id": self.commercial_audit_report_id,
            "commercial_acceptance": (
                self.commercial_acceptance.to_dict()
                if self.commercial_acceptance is not None else None
            ),
            "passed": self.passed,
            "case_count": self.case_count,
            "failure_count": self.failure_count,
            "total_duration_ms": self.total_duration_ms,
        }


# ---------------------------------------------------------------------- #
# EvalTraceStore —— append-only 轨迹存储 + 回归对比
# ---------------------------------------------------------------------- #


# 指标的"好方向"映射（用于回归判定：哪些指标 higher_is_better）
_DEFAULT_HIGHER_IS_BETTER: dict[str, bool] = {
    "unconfirmed_writes": False,     # 越低越好
    "secret_leaks": False,
    "path_leaks": False,
    "cancel_residual_rate": False,
    # 其余默认 higher_is_better=True（成功率类）
}


class EvalTraceStore:
    """append-only 评估轨迹存储（§3 "evaluation traces" / §15.3 可导出/删除）。

    用法::

        store = EvalTraceStore()
        trace = EvalTrace(run_id="run-1", timestamp=time.time(),
                          cases=(...), gate_result=result,
                          metric_snapshot=EvalMetricSnapshot.from_gate_result(result))
        store.record(trace)
        findings = store.compare_regression("run-1")  # 对比上次

    线程安全由调用方在 eval-runner 层保证（单线程模型）。
    """

    def __init__(self, *, max_traces: int = 100) -> None:
        self._traces: list[EvalTrace] = []
        self._max = max_traces
        self._counter = 0

    def record(self, trace: EvalTrace) -> EvalTrace:
        """追加一条轨迹。超 ``max_traces`` 时 FIFO 丢弃最旧。"""
        self._traces.append(trace)
        if len(self._traces) > self._max:
            self._traces.pop(0)
        return trace

    def next_run_id(self) -> str:
        """生成下一个 run_id（单调递增）。"""
        self._counter += 1
        return f"run-{self._counter:06d}"

    # ---------------- 查询 ---------------- #

    @property
    def count(self) -> int:
        return len(self._traces)

    def get(self, run_id: str) -> EvalTrace | None:
        for t in self._traces:
            if t.run_id == run_id:
                return t
        return None

    def latest(self) -> EvalTrace | None:
        """最近一次 run（None = 空）。"""
        return self._traces[-1] if self._traces else None

    def previous(self, run_id: str) -> EvalTrace | None:
        """指定 run 的上一次 run（按插入序；None = 无更早的）。"""
        idx = None
        for i, t in enumerate(self._traces):
            if t.run_id == run_id:
                idx = i
                break
        if idx is None or idx == 0:
            return None
        return self._traces[idx - 1]

    def all_traces(self) -> tuple[EvalTrace, ...]:
        return tuple(self._traces)

    # ---------------- 回归对比 ---------------- #

    def compare_regression(
        self, run_id: str, *,
        higher_is_better: dict[str, bool] | None = None,
    ) -> tuple[RegressionFinding, ...]:
        """对比指定 run 与其上一次 run 的指标，返回退化项（§12.2 量化趋势）。

        ``higher_is_better`` 覆盖默认的方向映射；未覆盖的用 ``_DEFAULT_HIGHER_IS_BETTER``。
        只对比两次 run 都有的指标。
        """
        current = self.get(run_id)
        if current is None:
            return ()
        prev = self.previous(run_id)
        if prev is None:
            return ()  # 无更早的 run，无法对比

        direction = dict(_DEFAULT_HIGHER_IS_BETTER)
        if higher_is_better:
            direction.update(higher_is_better)

        findings: list[RegressionFinding] = []
        for name, cur_val in current.metric_snapshot.values.items():
            prev_val = prev.metric_snapshot.get(name)
            if prev_val is None:
                continue  # 上次没这个指标
            delta = cur_val - prev_val
            hib = direction.get(name, True)
            # 退化判定：higher_is_better 时 delta<0 退化；lower_is_better 时 delta>0 退化
            regressed = (delta < 0) if hib else (delta > 0)
            findings.append(RegressionFinding(
                metric_name=name, previous=prev_val, current=cur_val,
                delta=delta, regressed=regressed, higher_is_better=hib,
            ))
        return tuple(findings)

    def regressions(self, run_id: str) -> tuple[RegressionFinding, ...]:
        """``compare_regression`` 的便捷别名：只返回退化项（regressed=True）。"""
        return tuple(f for f in self.compare_regression(run_id) if f.regressed)

    # ---------------- 导出/删除（§15.3）---------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "traces": [t.to_dict() for t in self._traces],
        }

    def clear(self) -> None:
        """清空（§15.3 数据删除）。"""
        self._traces.clear()
        self._counter = 0


__all__ = [
    "EvalCase",
    "EvalMetricSnapshot",
    "RegressionFinding",
    "EvalTrace",
    "EvalTraceStore",
]
