"""Agent V2 release gate (plan §9 performance thresholds / §A7 §8).

Turns the :class:`AgentMetricsCollector` summary into a pass/fail verdict
against the performance and correctness thresholds the plan §9 table defines
as release gates — so a regression in first-token latency or cancel
responsiveness is caught before shipping, not after.

Each check is data-driven; a metric that was genuinely never observed
(e.g. no cancellations happened) is skipped rather than failed, so the gate
works on small fixture runs as well as full telemetry.  The one exception is
first-token latency: it is a hard §9 metric, so "runs > 0 but no first-token
observation" fails the gate instead of skipping it.

Hard rules (plan §9):

- **Latency gates are hard.** First-token > 200 ms or cancel > 200 ms is a
  failure, not a warning.  Missing first-token data with runs present is
  equally a failure (it proves nothing).
- **No data is a failure.** A summary with zero runs cannot prove the gate, so
  it fails with an actionable reason rather than passing vacuously.
- **The verdict is explainable.** ``ReleaseGateResult`` lists every check with
  its value / threshold so a failure points at the exact metric.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: Plan §9: "发送到首个用户可见阶段 ≤ 200ms".
DEFAULT_MAX_FIRST_TOKEN_MS: float = 200.0
#: Plan §9: "取消后的 UI 状态响应 ≤ 200ms".
DEFAULT_MAX_CANCEL_MS: float = 200.0
#: Plan §9: a soft cap beyond which the average run feels slow (warning only).
DEFAULT_WARN_DURATION_MS: float = 30_000.0


@dataclass(frozen=True)
class GateCheck:
    """One named release-gate check and its outcome."""

    name: str
    passed: bool
    severity: str = "error"  # error / warning
    value: Any = None
    threshold: Any = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "value": self.value,
            "threshold": self.threshold,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReleaseGateResult:
    """The pass/fail verdict for one metrics summary."""

    passed: bool
    checks: tuple[GateCheck, ...] = ()
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "failures": list(self.failures),
            "warnings": list(self.warnings),
        }


def evaluate_release_gate(
    summary: dict[str, Any],
    *,
    max_first_token_ms: float = DEFAULT_MAX_FIRST_TOKEN_MS,
    max_cancel_ms: float = DEFAULT_MAX_CANCEL_MS,
    warn_duration_ms: float = DEFAULT_WARN_DURATION_MS,
) -> ReleaseGateResult:
    """Evaluate *summary* against the plan §9 release-gate thresholds."""

    checks: list[GateCheck] = []
    failures: list[str] = []
    warnings: list[str] = []

    total_runs = int(summary.get("total_runs", 0) or 0)
    has_data = total_runs > 0
    checks.append(
        GateCheck(
            name="has_run_data",
            passed=has_data,
            value=total_runs,
            reason="无运行数据，无法证明发布门通过" if not has_data else "",
        )
    )
    if not has_data:
        failures.append("无运行数据：发布门需要至少一次可观测的 AgentRun")

    # First-token latency (hard gate, plan §9).
    # AUD-14208：首 token 是 §9 表中不可豁免的硬指标——"runs>0 但无一次首
    # token 观测"（全部 run 在流式开始前失败 / record_first_token 未接线）
    # 不能静默跳过，否则发布门空过，与"No data is a failure"自述矛盾。
    token = summary.get("avg_first_token_latency_ms")
    if token is not None:
        ok = float(token) <= max_first_token_ms
        checks.append(
            GateCheck(
                name="first_token_latency",
                passed=ok,
                value=token,
                threshold=max_first_token_ms,
                reason=f"首 token 延迟 {token}ms > {max_first_token_ms}ms",
            )
        )
        if not ok:
            failures.append(f"首 token 延迟 {token}ms 超过门槛 {max_first_token_ms}ms")
    elif has_data:
        checks.append(
            GateCheck(
                name="first_token_latency",
                passed=False,
                value=None,
                threshold=max_first_token_ms,
                reason="有运行记录但无一次首 token 观测，无法证明门槛",
            )
        )
        failures.append("首 token 延迟无数据：有运行记录但无一次首 token 观测")

    # Cancel responsiveness (hard gate, plan §9).
    cancel = summary.get("avg_cancel_latency_ms")
    if cancel is not None:
        ok = float(cancel) <= max_cancel_ms
        checks.append(
            GateCheck(
                name="cancel_latency",
                passed=ok,
                value=cancel,
                threshold=max_cancel_ms,
                reason=f"取消响应 {cancel}ms > {max_cancel_ms}ms",
            )
        )
        if not ok:
            failures.append(f"取消响应 {cancel}ms 超过门槛 {max_cancel_ms}ms")

    # Average duration (soft warning).
    duration = summary.get("avg_duration_ms")
    if duration is not None:
        ok = float(duration) <= warn_duration_ms
        checks.append(
            GateCheck(
                name="avg_duration",
                passed=ok,
                severity="warning",
                value=duration,
                threshold=warn_duration_ms,
            )
        )
        if not ok:
            warnings.append(
                f"平均运行时长 {duration}ms 超过建议 {warn_duration_ms}ms"
            )

    passed = not failures
    return ReleaseGateResult(
        passed=passed,
        checks=tuple(checks),
        failures=tuple(failures),
        warnings=tuple(warnings),
    )


__all__ = [
    "DEFAULT_MAX_CANCEL_MS",
    "DEFAULT_MAX_FIRST_TOKEN_MS",
    "DEFAULT_WARN_DURATION_MS",
    "GateCheck",
    "ReleaseGateResult",
    "evaluate_release_gate",
]
