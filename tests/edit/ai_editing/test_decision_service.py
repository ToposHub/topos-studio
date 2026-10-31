"""TAEP Phase 2 — Decision service 反馈回灌测试。

验证（TAEP §5.5 关键规则）：
- 拒绝 decision 后，其素材范围进入 must_avoid（下一轮规划约束）。
- 锁定 decision 进入 must_preserve。
- 用户 override 取代关联的 AI decision（USER_OVERRIDE 状态）。
- 锁定的 decision 不被 override 覆盖。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.decision_service import DecisionStore
from src.features.edit.ai_editing.editorial_models import (
    DecisionState,
    EditDecision,
    FrameRange,
    SourceRef,
    UserOverride,
)


def _decision(did: str, asset: str = "a1", start: int = 0, dur: int = 100) -> EditDecision:
    return EditDecision(
        decision_id=did, action="select_source_range",
        subject=SourceRef(asset, FrameRange(start, dur)), confidence=0.8,
    )


def test_reject_feeds_back_as_must_avoid():
    store = DecisionStore()
    store.add(_decision("d1"))
    store.add(_decision("d2", start=200, dur=50))
    store.reject("d1")
    constraints = store.planning_constraints()
    assert "a1:0-100" in constraints["must_avoid"]
    assert "a1:200-50" not in constraints["must_avoid"]


def test_lock_feeds_back_as_must_preserve():
    store = DecisionStore()
    store.add(_decision("d1"))
    store.lock("d1")
    constraints = store.planning_constraints()
    assert "d1" in constraints["must_preserve"]
    assert store.get("d1").state == DecisionState.LOCKED


def test_user_override_supersedes_decision():
    store = DecisionStore()
    store.add(_decision("d1"))
    store.record_user_override(UserOverride(
        override_id="o1", target_clip_id="c1", related_decision_id="d1",
        kind="trim", detail={"to": 80},
    ))
    assert store.get("d1").state == DecisionState.USER_OVERRIDE
    assert "o1" in store.planning_constraints()["user_overrides"]


def test_lock_not_overridden_by_user_override():
    """锁定的 decision 不被 override 覆盖（保留锁定语义）。"""
    store = DecisionStore()
    store.add(_decision("d1"))
    store.lock("d1")
    store.record_user_override(UserOverride(
        override_id="o1", related_decision_id="d1", kind="trim",
    ))
    # locked stays locked
    assert store.get("d1").state == DecisionState.LOCKED


def test_accept_then_reject_idempotent_state():
    store = DecisionStore()
    store.add(_decision("d1"))
    store.accept("d1")
    assert store.get("d1").state == DecisionState.ACCEPTED
    store.reject("d1")
    assert store.get("d1").state == DecisionState.REJECTED
