"""TAEP Decision Service（Phase 2）。

管理 ``EditDecision`` 的接受/拒绝/锁定/用户 override，并把人类反馈回灌为下一轮
规划的硬约束（TAEP §5.5 / §6）。

关键规则（TAEP §5.5 关键规则）：

- 用户拒绝“删口误”后，该拒绝成为下一轮规划的约束（``rejected_decisions``）。
- 锁定的 decision（``DecisionState.LOCKED``）在重规划时必须保留。
- 人工在时间线上的修改映射为 ``UserOverride``，并标出原 AI decision 是否仍有效。
- 重规划默认保留锁定/已接受的 override，不被抹掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.features.edit.ai_editing.editorial_models import (
    DecisionState,
    EditDecision,
    UserOverride,
)


#: accept/reject/lock 的合法状态迁移表（TAEP §5.5 状态机，AUD-13308）。
#: 同态幂等迁移（如对 LOCKED 再 lock）总是允许；未列出的源→目标迁移
#: 抛 ``ValueError``。关键保护：
#: - ``LOCKED`` 只能保持锁定（重规划必须保留，不可被 reject/accept 改写）；
#: - ``REJECTED`` 不可被 accept 复活（拒绝是下一轮规划的约束）。
_ALLOWED_TRANSITIONS: dict[DecisionState, frozenset[DecisionState]] = {
    DecisionState.PROPOSED: frozenset({
        DecisionState.ACCEPTED, DecisionState.REJECTED, DecisionState.LOCKED,
    }),
    DecisionState.ACCEPTED: frozenset({
        DecisionState.REJECTED, DecisionState.LOCKED,
    }),
    DecisionState.REJECTED: frozenset(),
    DecisionState.LOCKED: frozenset(),
    DecisionState.USER_OVERRIDE: frozenset({
        DecisionState.ACCEPTED, DecisionState.REJECTED, DecisionState.LOCKED,
    }),
    DecisionState.SUPERSEDED: frozenset(),
}


@dataclass
class DecisionStore:
    """variant 内 decisions 的可变工作集（重规划前后的状态载体）。

    P2 为内存态；P5+ 接 ``assistant_session_store`` 持久化到工程可选 payload。
    """

    decisions: dict[str, EditDecision] = field(default_factory=dict)
    overrides: dict[str, UserOverride] = field(default_factory=dict)

    def add(self, decision: EditDecision) -> None:
        self.decisions[decision.decision_id] = decision

    def add_override(self, override: UserOverride) -> None:
        self.overrides[override.override_id] = override

    def get(self, decision_id: str) -> EditDecision | None:
        return self.decisions.get(decision_id)

    # ---------------- 状态迁移 ---------------- #

    def accept(self, decision_id: str) -> EditDecision:
        """接受一条 decision。"""
        return self._transition_to(decision_id, DecisionState.ACCEPTED)

    def reject(self, decision_id: str) -> EditDecision:
        """拒绝一条 decision —— 拒绝成为下一轮规划的约束。"""
        return self._transition_to(decision_id, DecisionState.REJECTED)

    def lock(self, decision_id: str) -> EditDecision:
        """锁定一条 decision —— 重规划必须保留。"""
        return self._transition_to(decision_id, DecisionState.LOCKED)

    def record_user_override(self, override: UserOverride) -> None:
        """记录一次用户手工修改。

        若 override 关联了某 decision，把该 decision 标记为 ``USER_OVERRIDE``，
        表示原 AI 决策已被人工取代（TAEP §5.5）。
        """
        self.overrides[override.override_id] = override
        if override.related_decision_id is not None:
            d = self.decisions.get(override.related_decision_id)
            if d is not None and d.state not in (DecisionState.LOCKED,):
                self.decisions[override.related_decision_id] = d.with_state(
                    DecisionState.USER_OVERRIDE
                )

    def _require(self, decision_id: str) -> EditDecision:
        d = self.decisions.get(decision_id)
        if d is None:
            raise KeyError(f"decision {decision_id!r} not found")
        return d

    def _transition_to(self, decision_id: str, target: DecisionState) -> EditDecision:
        """按 ``_ALLOWED_TRANSITIONS`` 校验后迁移状态（TAEP §5.5，AUD-13308）。

        非法迁移（如 LOCKED 被 reject 覆盖、REJECTED 被 accept 复活）抛
        ``ValueError`` 并保留原状态——锁定/拒绝保护不被一次误调失效。
        """
        d = self._require(decision_id)
        allowed = _ALLOWED_TRANSITIONS.get(d.state, frozenset())
        if target != d.state and target not in allowed:
            raise ValueError(
                f"decision {decision_id!r} 状态 {d.state.value!r} 不允许迁移到 "
                f"{target.value!r}（TAEP §5.5：锁定/已拒绝的 decision 不可被"
                " accept/reject/lock 直接改写）"
            )
        self.decisions[decision_id] = d.with_state(target)
        return self.decisions[decision_id]

    # ---------------- 查询（供 planner 读约束）---------------- #

    @property
    def rejected(self) -> list[EditDecision]:
        """被拒绝的 decisions —— 下一轮规划必须避开这些选择。"""
        return [d for d in self.decisions.values() if d.state == DecisionState.REJECTED]

    @property
    def locked(self) -> list[EditDecision]:
        """锁定的 decisions —— 下一轮规划必须保留。"""
        return [d for d in self.decisions.values() if d.state == DecisionState.LOCKED]

    @property
    def accepted(self) -> list[EditDecision]:
        return [d for d in self.decisions.values() if d.state == DecisionState.ACCEPTED]

    def rejected_subjects(self) -> list[str]:
        """被拒绝的素材范围签名（``asset_id:start-dur``），供 planner 排除。"""
        out: list[str] = []
        for d in self.rejected:
            if d.subject is not None:
                out.append(f"{d.subject.asset_id}:{d.subject.source.start}-{d.subject.source.duration}")
        return out

    def planning_constraints(self) -> dict[str, Any]:
        """汇总下一轮规划必须遵守的约束（TAEP §5.5 关键规则）。"""
        return {
            "must_preserve": [d.decision_id for d in self.locked + self.accepted],
            "must_avoid": self.rejected_subjects(),
            "user_overrides": [o.override_id for o in self.overrides.values()],
        }
