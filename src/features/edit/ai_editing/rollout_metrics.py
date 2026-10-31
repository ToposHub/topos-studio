"""Alpha/Beta/Stable rollout metrics and promotion gates (P11)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RolloutChannel(str, Enum):
    ALPHA = "alpha"
    BETA = "beta"
    STABLE = "stable"


@dataclass(frozen=True)
class RolloutMetrics:
    tasks: int = 0
    successful_tasks: int = 0
    crashes: int = 0
    provider_failures: int = 0
    repeated_charges: int = 0
    copyright_incidents: int = 0
    user_edits: int = 0
    satisfied_users: int = 0
    total_cost_units: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successful_tasks / self.tasks if self.tasks else 0.0

    @property
    def user_edit_rate(self) -> float:
        return self.user_edits / self.tasks if self.tasks else 0.0

    @property
    def satisfaction_rate(self) -> float:
        return self.satisfied_users / self.tasks if self.tasks else 0.0

    @property
    def cost_per_task(self) -> float:
        return self.total_cost_units / self.tasks if self.tasks else 0.0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "tasks": self.tasks,
            "successful_tasks": self.successful_tasks,
            "crashes": self.crashes,
            "provider_failures": self.provider_failures,
            "repeated_charges": self.repeated_charges,
            "copyright_incidents": self.copyright_incidents,
            "user_edits": self.user_edits,
            "satisfied_users": self.satisfied_users,
            "total_cost_units": self.total_cost_units,
            "success_rate": self.success_rate,
            "user_edit_rate": self.user_edit_rate,
            "satisfaction_rate": self.satisfaction_rate,
            "cost_per_task": self.cost_per_task,
        }


@dataclass(frozen=True)
class RolloutGate:
    allowed: bool
    reasons: tuple[str, ...] = ()
    metrics: RolloutMetrics = RolloutMetrics()


def evaluate_rollout_gate(
    channel: RolloutChannel,
    metrics: RolloutMetrics,
) -> RolloutGate:
    """Apply conservative promotion gates without collecting raw user content."""
    reasons: list[str] = []
    if metrics.tasks <= 0:
        reasons.append("no rollout tasks observed")
    if metrics.crashes:
        reasons.append("crashes observed")
    if metrics.repeated_charges:
        reasons.append("repeated charges observed")
    if metrics.copyright_incidents:
        reasons.append("copyright incidents observed")
    if channel in {RolloutChannel.BETA, RolloutChannel.STABLE}:
        if metrics.success_rate < 0.85:
            reasons.append("task success rate below 0.85")
        if metrics.satisfaction_rate < 0.60:
            reasons.append("satisfaction rate below 0.60")
    if channel == RolloutChannel.STABLE and metrics.provider_failures:
        reasons.append("provider failures observed")
    return RolloutGate(not reasons, tuple(reasons), metrics)


__all__ = ["RolloutChannel", "RolloutGate", "RolloutMetrics", "evaluate_rollout_gate"]
