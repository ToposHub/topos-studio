"""TAEP M0.1 — ApprovalService 安全边界契约测试。

验收（审查 M0.1 / 总体不变量 §1.3 / §3.3）：

- grant 由宿主签发，不透明、一次性、绑定作用域（project/timeline/revision/plan hash）。
- 伪造 token、重复 token、过期 token、跨 project/timeline/revision/plan hash 均被拒，
  且 project 完全不变。
- 用户批准一次只能产生一次 commit。
- consume 原子：失败时不标 used。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.approval_service import (
    ApprovalError,
    ApprovalScope,
    ApprovalService,
    GrantExpired,
    GrantNotFound,
    GrantScopeMismatch,
    compute_plan_hash,
)


def _scope(
    *, project_id: str = "p1", timeline_id: str = "t1",
    base_revision: int = 0, plan_hash: str = "h1",
    diff_hash: str | None = None,
    action: str = "commit_plan",
) -> ApprovalScope:
    return ApprovalScope(
        action=action,  # type: ignore[arg-type]
        project_id=project_id,
        timeline_id=timeline_id,
        base_revision=base_revision,
        plan_hash=plan_hash,
        diff_hash=diff_hash,
    )


# ================================================================
# 签发 + 消费（正常路径）
# ================================================================


def test_issue_and_consume_single_use_grant():
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope())
    assert grant.grant_id  # 不透明
    assert grant.single_use is True
    consumed = svc.consume(grant.grant_id, _scope())
    assert consumed.grant_id == grant.grant_id


def test_single_use_grant_cannot_be_consumed_twice():
    """M0.1：一次性 grant 消费后不可重放。"""
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope())
    svc.consume(grant.grant_id, _scope())
    with pytest.raises(GrantNotFound):
        svc.consume(grant.grant_id, _scope())


def test_multi_use_grant_can_be_consumed_multiple_times_within_ttl():
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(), single_use=False)
    svc.consume(grant.grant_id, _scope())
    svc.consume(grant.grant_id, _scope())  # 第二次仍有效
    svc.consume(grant.grant_id, _scope())  # 第三次


# ================================================================
# 伪造 / 未知 token
# ================================================================


def test_unknown_grant_id_rejected():
    svc = ApprovalService()
    with pytest.raises(GrantNotFound):
        svc.consume("totally-fake-id", _scope())


def test_empty_string_grant_id_rejected():
    svc = ApprovalService()
    with pytest.raises(GrantNotFound):
        svc.consume("", _scope())


# ================================================================
# 过期
# ================================================================


def test_expired_grant_rejected_and_cleaned():
    clock = {"t": 0.0}
    svc = ApprovalService(time_fn=lambda: clock["t"])
    grant = svc.issue_after_user_confirmation(_scope(), ttl_seconds=10.0)
    clock["t"] = 100.0  # 推进时间，grant 过期
    with pytest.raises(GrantExpired):
        svc.consume(grant.grant_id, _scope())
    # 过期 grant 被清理，再 consume 报 NotFound
    with pytest.raises(GrantNotFound):
        svc.consume(grant.grant_id, _scope())


def test_injected_clock_controls_validation_and_active_count():
    """All service checks use the injected clock, not the host wall clock."""
    clock = {"t": 100.0}
    svc = ApprovalService(time_fn=lambda: clock["t"])
    grant = svc.issue_after_user_confirmation(_scope(), ttl_seconds=10.0)

    assert svc.validate(grant.grant_id, _scope()) is True
    assert svc.active_count() == 1

    clock["t"] = 110.0
    assert svc.validate(grant.grant_id, _scope()) is False
    assert svc.active_count() == 0
    with pytest.raises(GrantExpired):
        svc.consume(grant.grant_id, _scope())


# ================================================================
# 作用域不匹配（跨 project/timeline/revision/plan hash）
# ================================================================


def test_scope_mismatch_wrong_project_rejected():
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(project_id="p1"))
    with pytest.raises(GrantScopeMismatch):
        svc.consume(grant.grant_id, _scope(project_id="other"))


def test_scope_mismatch_wrong_timeline_rejected():
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(timeline_id="t1"))
    with pytest.raises(GrantScopeMismatch):
        svc.consume(grant.grant_id, _scope(timeline_id="other"))


def test_scope_mismatch_wrong_revision_rejected():
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(base_revision=0))
    with pytest.raises(GrantScopeMismatch):
        svc.consume(grant.grant_id, _scope(base_revision=5))


def test_scope_mismatch_wrong_plan_hash_rejected():
    """M0.1：grant 绑定 plan hash；换一份 plan 即作废（预览即提交）。"""
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(plan_hash="hash-a"))
    with pytest.raises(GrantScopeMismatch):
        svc.consume(grant.grant_id, _scope(plan_hash="hash-b"))


def test_scope_mismatch_wrong_diff_hash_rejected():
    """M1 收尾：grant 绑定 diff hash；换一份 diff 即作废（预览即提交 §1.4）。

    diff_hash 把"用户看到的那一份 diff"与"将要提交的那一份 diff"绑定。timeline
    在预览与提交之间被改动会导致 diff_hash 变化，grant 必须失效。
    """
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(plan_hash="h1", diff_hash="diff-a"))
    with pytest.raises(GrantScopeMismatch):
        svc.consume(grant.grant_id, _scope(plan_hash="h1", diff_hash="diff-b"))


def test_scope_diff_hash_none_in_grant_is_unconstrained():
    """M1 收尾：grant 未绑定 diff_hash（None）时不强制——向后兼容旧 grant。

    这样 expected 携带 diff_hash 而签发侧省略时仍能 consume（旧测试路径）。
    """
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(plan_hash="h1"))  # diff_hash 默认 None
    consumed = svc.consume(grant.grant_id, _scope(plan_hash="h1", diff_hash="anything"))
    assert consumed.grant_id == grant.grant_id


def test_scope_diff_hash_match_succeeds():
    """M1 收尾：diff_hash 相同时 consume 成功。"""
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(plan_hash="h1", diff_hash="same"))
    consumed = svc.consume(grant.grant_id, _scope(plan_hash="h1", diff_hash="same"))
    assert consumed.grant_id == grant.grant_id


def test_scope_mismatch_wrong_action_rejected():
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(action="commit_plan"))
    with pytest.raises(GrantScopeMismatch):
        svc.consume(
            grant.grant_id,
            ApprovalScope(
                action="export_file",  # type: ignore[arg-type]
                project_id="p1", timeline_id="t1",
                base_revision=0, plan_hash="h1",
            ),
        )


def test_scope_unconstrained_field_allows_none_in_expected():
    """expected scope 的 None 字段不强制匹配（签发侧已强约束）。"""
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(
        ApprovalScope(action="commit_plan", project_id="p1")  # type: ignore[arg-type]
    )
    # expected 省略 timeline/revision/hash 也能 consume
    consumed = svc.consume(
        grant.grant_id,
        ApprovalScope(action="commit_plan", project_id="p1"),  # type: ignore[arg-type]
    )
    assert consumed.grant_id == grant.grant_id


# ================================================================
# consume 原子性：失败不标 used
# ================================================================


def test_failed_consume_does_not_mark_used():
    """scope 不匹配的 consume 失败后，grant 仍可用（用正确 scope 再 consume 成功）。"""
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope(plan_hash="h1"))
    # 错误 scope → 失败
    with pytest.raises(GrantScopeMismatch):
        svc.consume(grant.grant_id, _scope(plan_hash="h2"))
    # 正确 scope → 仍能消费（未被错误 consume 标 used）
    consumed = svc.consume(grant.grant_id, _scope(plan_hash="h1"))
    assert consumed.grant_id == grant.grant_id


# ================================================================
# validate（非消费式预检）
# ================================================================


def test_validate_does_not_consume():
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope())
    assert svc.validate(grant.grant_id, _scope()) is True
    # validate 不消费 → 仍可 consume
    svc.consume(grant.grant_id, _scope())


def test_validate_expired_returns_false():
    clock = {"t": 0.0}
    svc = ApprovalService(time_fn=lambda: clock["t"])
    grant = svc.issue_after_user_confirmation(_scope(), ttl_seconds=5.0)
    clock["t"] = 100.0
    assert svc.validate(grant.grant_id, _scope()) is False


# ================================================================
# 撤销 / 生命周期
# ================================================================


def test_revoke_single_grant():
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_scope())
    assert svc.revoke(grant.grant_id) is True
    with pytest.raises(GrantNotFound):
        svc.consume(grant.grant_id, _scope())


def test_revoke_for_project_clears_only_that_project():
    svc = ApprovalService()
    g1 = svc.issue_after_user_confirmation(_scope(project_id="p1"))
    g2 = svc.issue_after_user_confirmation(_scope(project_id="p2"))
    n = svc.revoke_for_project("p1")
    assert n == 1
    # p1 的 grant 失效
    with pytest.raises(GrantNotFound):
        svc.consume(g1.grant_id, _scope(project_id="p1"))
    # p2 的 grant 仍有效
    svc.consume(g2.grant_id, _scope(project_id="p2"))


def test_revoke_all_clears_everything():
    svc = ApprovalService()
    svc.issue_after_user_confirmation(_scope(project_id="p1"))
    svc.issue_after_user_confirmation(_scope(project_id="p2"))
    assert svc.revoke_all() == 2
    assert svc.active_count() == 0


# ================================================================
# 签发前置条件
# ================================================================


def test_issue_requires_project_id():
    svc = ApprovalService()
    with pytest.raises(ValueError):
        svc.issue_after_user_confirmation(
            ApprovalScope(action="commit_plan", project_id="")  # type: ignore[arg-type]
        )


# ================================================================
# compute_plan_hash
# ================================================================


def test_compute_plan_hash_stable_and_distinct():
    h1 = compute_plan_hash(b'{"a":1}')
    h2 = compute_plan_hash(b'{"a":1}')
    h3 = compute_plan_hash(b'{"a":2}')
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # sha256 hex


def test_compute_plan_hash_accepts_str_or_bytes():
    assert compute_plan_hash("hello") == compute_plan_hash(b"hello")
