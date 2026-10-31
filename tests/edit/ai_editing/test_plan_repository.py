"""TAEP M2.1 — PlanRepository 测试。

验收（审查 M2.1 / 总体不变量 §1.4）：

- planner 编译的 plan 存入仓库；variant 只引用 plan_id/hash。
- get/get_plan 取回 immutable plan；find_by_hash 按 hash 取。
- preview 与状态指纹绑定；状态变化 → stale。
- 不存在的 plan → None（apply 路径据此中止）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanTarget,
)
from src.features.edit.ai_editing.plan_repository import (
    PlanRepository,
    StoredPlan,
    state_fingerprint,
)


def _plan(plan_id: str = "plan-1", dur: int = 100) -> EditPlan:
    return EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        plan_id=plan_id,
        operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={
            "track_id": "v1", "asset_id": "a1",
            "source": {"start": 0, "duration": dur},
            "timeline": {"start": 0, "duration": dur},
        }),),
    )


# ================================================================
# store / get
# ================================================================


def test_store_returns_stored_plan_with_hash():
    repo = PlanRepository()
    plan = _plan()
    stored = repo.store(plan, source_variant_id="v1")
    assert isinstance(stored, StoredPlan)
    assert stored.plan_id == "plan-1"
    assert stored.content_hash  # sha256 hex
    assert len(stored.content_hash) == 64
    assert stored.source_variant_id == "v1"


def test_get_returns_immutable_plan():
    repo = PlanRepository()
    plan = _plan()
    repo.store(plan)
    assert repo.get_plan("plan-1") is plan
    assert repo.get("plan-1").plan is plan


def test_get_nonexistent_returns_none():
    repo = PlanRepository()
    assert repo.get("nope") is None
    assert repo.get_plan("nope") is None


def test_content_hash_distinct_for_different_plans():
    repo = PlanRepository()
    a = repo.store(_plan("a", dur=100))
    b = repo.store(_plan("b", dur=200))
    assert a.content_hash != b.content_hash


def test_find_by_hash():
    repo = PlanRepository()
    stored = repo.store(_plan("a"))
    found = repo.find_by_hash(stored.content_hash)
    assert found is not None
    assert found.plan_id == "a"
    assert repo.find_by_hash("nonexistent") is None


# ================================================================
# preview 绑定 + stale 判定
# ================================================================


def test_attach_preview_and_stale_detection():
    repo = PlanRepository()
    repo.store(_plan())
    fp1 = state_fingerprint(project_revision=0, manifest_hash="m1", timeline_revision=0)
    assert repo.attach_preview("plan-1", {"diff": "x"}, state_fingerprint_value=fp1)
    # 同指纹 → 不 stale
    assert repo.is_preview_stale("plan-1", current_fingerprint=fp1) is False
    # 状态变化 → stale
    fp2 = state_fingerprint(project_revision=1, manifest_hash="m1", timeline_revision=0)
    assert repo.is_preview_stale("plan-1", current_fingerprint=fp2) is True


def test_is_preview_stale_when_no_preview():
    """未绑定 preview → stale（必须先 preview）。"""
    repo = PlanRepository()
    repo.store(_plan())
    assert repo.is_preview_stale(
        "plan-1",
        current_fingerprint=state_fingerprint(
            project_revision=None, manifest_hash=None, timeline_revision=None
        ),
    ) is True


def test_is_preview_stale_when_plan_missing():
    repo = PlanRepository()
    assert repo.is_preview_stale(
        "ghost",
        current_fingerprint=state_fingerprint(
            project_revision=None, manifest_hash=None, timeline_revision=None
        ),
    ) is True


def test_attach_preview_preserved_on_restore_same_content():
    """同内容 plan 重新 store 时保留已绑定的 preview。"""
    repo = PlanRepository()
    repo.store(_plan())
    fp = state_fingerprint(project_revision=0, manifest_hash="m", timeline_revision=0)
    repo.attach_preview("plan-1", {"diff": "x"}, state_fingerprint_value=fp)
    # 重新 store 同一 plan（同 hash）
    repo.store(_plan())
    stored = repo.get("plan-1")
    assert stored.preview == {"diff": "x"}
    assert stored.preview_state_fingerprint == fp


def test_remove_and_clear():
    repo = PlanRepository()
    repo.store(_plan("a"))
    repo.store(_plan("b"))
    assert repo.count() == 2
    assert repo.remove("a") is True
    assert repo.count() == 1
    assert repo.remove("a") is False
    repo.clear()
    assert repo.count() == 0
