"""P0 三条红线集成测试（plan §14 step 6 通过准则）。

plan §14 明确：测试三条红线，**通过后才启动 P1**：

1. **取消会传播**：run.cancel → cancel token 真实传到 provider/executor，
   下游不再启动、已完成 artifact 标 partial、UI 有原因。
2. **旧 approval 不可复用**：grant 是一次性、绑定 revision/plan_hash；
   revision 变化后旧 grant 被拒。
3. **工程关闭后 run 不回写**：detach_session 冻结活跃 run，已关闭的工程
   不被回写。

本文件做**跨模块**集成（ai_harness + ai_editing.approval + scheduler_adapter），
验证 P0 落地后三条红线在数据层都成立。
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    PermissionLevel,
    RunPolicy,
    RunStatus,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.scheduler_adapter import (
    JobRunBridge,
    poll_cancel_to_job,
    sync_job_to_run,
)
from src.features.edit.ai_editing.approval_service import (
    ApprovalScope,
    ApprovalService,
    GrantExpired,
    GrantNotFound,
    GrantScopeMismatch,
    compute_plan_hash,
)
from src.features.edit.ai_editing.jobs import JobHandle, JobKind, JobStatus
from src.features.edit.ai_editing.task_graph import (
    ContextSlice,
    SingleWriterPolicy,
    TaskGraph,
    TaskGraphExecutor,
    TaskNode,
    TaskRole,
)
from src.features.edit.ai_editing.task_artifacts import ScriptDraft


# ================================================================
# 红线 1：取消会传播
# ================================================================


class TestCancelPropagation:
    """plan §10.2 P0.6：取消是真实 cancel token 传播，不是仅变更状态文案。"""

    def test_cancel_token_reaches_task_graph_executor(self):
        """run.cancel → runtime.is_cancelled → TaskGraphExecutor 在检查点退出。

        构造一个 2 节点 TaskGraph，run 在第一个节点完成后被 cancel；验证第二个
        节点不会执行（CANCELLED），且已完成 artifact 保留（标 partial 由 run
        的 finish(cancelled) 体现）。
        """
        rt = HarnessRuntime()
        run_id = rt.start(Invocation(
            source="chat", skill_id="topos.test",
            scope=InvocationScope(project_id="p1", selected_asset_ids=("a1",)),
            policy=RunPolicy(permission=PermissionLevel.PROPOSE),
            parent_session_id="sess1",
        ))

        executed: list[str] = []
        class _Executor:
            def execute(self, role, context, upstream):
                executed.append(role.value)
                # 第一个节点完成后，模拟外部 cancel（检查点）
                if role == TaskRole.CREATIVE_DIRECTOR:
                    rt.cancel(run_id, reason="user_aborted")
                return ScriptDraft(
                    draft_id="d1", audience="a", tone="t",
                    hooks=(), call_to_action="c",
                    produced_by="creative_director", skill_id="s", skill_version="1",
                    evidence_refs=(),
                )

        graph = TaskGraph(
            skill_id="topos.test",
            nodes=(
                TaskNode(task_id="cd", role=TaskRole.CREATIVE_DIRECTOR),
                TaskNode(task_id="qa", role=TaskRole.QA_REVIEWER, depends_on=("cd",)),
            ),
        )
        executor = TaskGraphExecutor(
            role_executor=_Executor(), policy=SingleWriterPolicy(),
        )
        rt.mark_running(run_id)
        results = executor.execute(
            graph, cancel_requested=lambda: rt.is_cancelled(run_id),
        )
        # 第一个节点执行了，第二个被 cancel
        assert TaskRole.CREATIVE_DIRECTOR.value in executed
        states = {r.task_id: r.state.value for r in results}
        assert states["cd"] == "succeeded"
        assert states["qa"] == "cancelled"
        # run 终态为 cancelled，已完成 artifact 保留（partial）
        rt.finish(run_id, success=False)
        assert rt.get_run(run_id).status == RunStatus.CANCELLED
        assert rt.get_run(run_id).cancel_reason == "user_aborted"

    def test_cancel_propagates_to_job_via_scheduler_adapter(self):
        """run.cancel → poll_cancel_to_job → JobHandle.request_cancel。"""
        rt = HarnessRuntime()
        run_id = rt.start(Invocation(
            source="chat",
            scope=InvocationScope(project_id="p1", selected_asset_ids=("a1",)),
            parent_session_id="sess1",
        ))
        job = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.RUNNING)
        sync_job_to_run(rt, run_id, job)
        rt.cancel(run_id)
        # provider 检查点
        assert poll_cancel_to_job(rt, run_id, job) is True
        assert job.status == JobStatus.CANCELLING  # 真实标记，不只是 UI 文案

    def test_cancel_reason_visible_in_run_record(self):
        """UI 有原因（plan §0.3 第 3 条）。"""
        rt = HarnessRuntime()
        run_id = rt.start(Invocation(
            source="chat",
            scope=InvocationScope(project_id="p1", selected_asset_ids=("a1",)),
            parent_session_id="sess1",
        ))
        rt.cancel(run_id, reason="用户在 50% 时取消")
        rec = rt.get_run(run_id)
        assert rec.cancel_reason == "用户在 50% 时取消"
        # 事件流里有 cancel_requested，UI drawer 据此显示
        kinds = [e.kind for e in rec.events]
        assert "run.cancel_requested" in kinds


# ================================================================
# 红线 2：旧 approval 不可复用
# ================================================================


class TestApprovalNotReusable:
    """plan §1.4 红线 4 / §6.4：ApprovalGrant 一次性、绑定 revision/plan_hash。"""

    def _scope(self, *, base_revision=10, plan_hash="h1", timeline_id="t1"):
        return ApprovalScope(
            action="commit_plan",
            project_id="p1",
            timeline_id=timeline_id,
            base_revision=base_revision,
            plan_hash=plan_hash,
        )

    def test_single_use_grant_consumed_once(self):
        svc = ApprovalService()
        scope = self._scope()
        grant = svc.issue_after_user_confirmation(scope)
        # 第一次 consume 成功
        svc.consume(grant.grant_id, scope)
        # 第二次 → GrantNotFound（已 used）
        with pytest.raises(GrantNotFound):
            svc.consume(grant.grant_id, scope)

    def test_stale_revision_grant_rejected(self):
        """revision 变化后旧 grant 被拒（红线：旧 approval 永不可恢复）。"""
        svc = ApprovalService()
        grant = svc.issue_after_user_confirmation(self._scope(base_revision=10))
        # revision 变成 11（stale）
        new_scope = self._scope(base_revision=11)
        with pytest.raises(GrantScopeMismatch):
            svc.consume(grant.grant_id, new_scope)

    def test_changed_plan_hash_rejected(self):
        """plan 内容变了（hash 变）→ 旧 grant 失效。"""
        svc = ApprovalService()
        grant = svc.issue_after_user_confirmation(
            self._scope(plan_hash=compute_plan_hash('{"v":1}')),
        )
        # 用户改了 plan
        new_scope = self._scope(plan_hash=compute_plan_hash('{"v":2}'))
        with pytest.raises(GrantScopeMismatch):
            svc.consume(grant.grant_id, new_scope)

    def test_expired_grant_rejected(self):
        """超过 TTL 的 grant 被拒。"""
        clock = [1000.0]
        svc = ApprovalService(time_fn=lambda: clock[0])
        grant = svc.issue_after_user_confirmation(
            self._scope(), ttl_seconds=300.0,
        )
        # 推进时间超过 TTL
        clock[0] = 1000.0 + 301.0
        with pytest.raises(GrantExpired):
            svc.consume(grant.grant_id, self._scope())

    def test_cross_project_grant_rejected(self):
        """跨工程 grant 无效。"""
        svc = ApprovalService()
        grant = svc.issue_after_user_confirmation(self._scope())  # project p1
        other_scope = ApprovalScope(
            action="commit_plan", project_id="p2", timeline_id="t1",
            base_revision=10, plan_hash="h1",
        )
        with pytest.raises(GrantScopeMismatch):
            svc.consume(grant.grant_id, other_scope)

    def test_resume_old_session_does_not_restore_grant(self):
        """plan §10.2 P0.4：恢复旧会话后旧 plan 必须以当前 revision 重做 preview，
        旧 grant 永不可恢复。

        这里证明：会话目录（session_models）的数据层**不存 grant**——grant 只在
        ApprovalService 内存里，重启即失效。
        """
        from src.features.edit.ai_editing.session_models import (
            CompressionCheckpoint, MessageSummary, SessionCatalog,
        )
        catalog = SessionCatalog()
        entry = catalog.create_session("old")
        entry.add_message(MessageSummary(text="已批准方案", run_id="r1"))
        # 序列化目录 → 检查不含任何 grant 信息
        d = catalog.to_dict()
        dumped = repr(d)
        assert "grant" not in dumped.lower()
        assert "approval" not in dumped.lower()


# ================================================================
# 红线 3：工程关闭后 run 不回写
# ================================================================


class TestClosedProjectNoWriteback:
    """plan §1.4 红线 + §10.2 P0.6：工程关闭（detach_session）后 run 不回写。"""

    def test_detach_freezes_active_run(self):
        rt = HarnessRuntime()
        run_id = rt.start(Invocation(
            source="chat",
            scope=InvocationScope(project_id="p1", selected_asset_ids=("a1",)),
            parent_session_id="proj-A",
        ))
        rt.mark_running(run_id)
        # 工程关闭
        n = rt.detach_session("proj-A")
        assert n == 1
        # 之后任何回写都被拒
        assert rt.finish(run_id) is False
        assert rt.set_status(run_id, RunStatus.SUCCEEDED) is False
        assert rt.record_progress(run_id, 1.0) is False
        assert rt.record_cost(run_id, 0.5) is False
        # 状态保持 RUNNING（未被改写）
        assert rt.get_run(run_id).status == RunStatus.RUNNING

    def test_cancel_after_detach_ignored(self):
        """工程关闭后取消请求也不回写（避免在已关闭工程触发副作用）。"""
        rt = HarnessRuntime()
        run_id = rt.start(Invocation(
            source="chat",
            scope=InvocationScope(project_id="p1", selected_asset_ids=("a1",)),
            parent_session_id="proj-A",
        ))
        rt.detach_session("proj-A")
        assert rt.cancel(run_id) is False
        assert rt.is_cancelled(run_id) is False

    def test_job_sync_after_detach_rejected(self):
        """工程关闭后 Job 进度也不再同步进 run。"""
        rt = HarnessRuntime()
        run_id = rt.start(Invocation(
            source="chat",
            scope=InvocationScope(project_id="p1", selected_asset_ids=("a1",)),
            parent_session_id="proj-A",
        ))
        job = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.RUNNING, progress=0.5)
        sync_job_to_run(rt, run_id, job)
        rt.detach_session("proj-A")
        job.update_progress(0.9)
        assert sync_job_to_run(rt, run_id, job) is False
        # run 里的 job 进度仍是 0.5（未改写）
        rec = rt.get_run(run_id)
        assert rec.jobs[0].progress == 0.5

    def test_other_projects_unaffected_by_detach(self):
        """关闭 proj-A 不影响 proj-B 的 run。"""
        rt = HarnessRuntime()
        ridA = rt.start(Invocation(
            source="chat",
            scope=InvocationScope(project_id="pA", selected_asset_ids=("a1",)),
            parent_session_id="proj-A",
        ))
        ridB = rt.start(Invocation(
            source="chat",
            scope=InvocationScope(project_id="pB", selected_asset_ids=("a1",)),
            parent_session_id="proj-B",
        ))
        rt.mark_running(ridA)
        rt.mark_running(ridB)
        rt.detach_session("proj-A")
        # proj-B 仍可正常 finish
        assert rt.finish(ridB) is True
        assert rt.get_run(ridB).status == RunStatus.SUCCEEDED


# ================================================================
# 综合验收：三条红线同时成立
# ================================================================


class TestAllThreeRedLinesTogether:
    """一个场景串起三条红线（plan §14 step 6 综合验收）。"""

    def test_cancel_then_close_then_stale_approval_all_blocked(self):
        rt = HarnessRuntime()
        approval = ApprovalService()

        # 1. 启动一个 run，用户中途取消
        run_id = rt.start(Invocation(
            source="chat", skill_id="topos.subtitle_draft",
            scope=InvocationScope(project_id="p1", timeline_id="t1",
                                  selected_asset_ids=("a1",)),
            policy=RunPolicy(permission=PermissionLevel.APPLY_WITH_EACH_APPROVAL),
            parent_session_id="proj-A",
        ))
        rt.mark_running(run_id)
        rt.cancel(run_id, reason="用户取消")
        assert rt.is_cancelled(run_id)
        # 红线 1：取消真实传播
        job = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.RUNNING)
        sync_job_to_run(rt, run_id, job)
        assert poll_cancel_to_job(rt, run_id, job) is True
        assert job.status == JobStatus.CANCELLING

        # 2. 关闭工程
        rt.detach_session("proj-A")
        # 红线 3：之后 run 不再回写
        assert rt.finish(run_id) is False
        assert rt.get_run(run_id).status == RunStatus.RUNNING

        # 3. 用户之前批准过一个 plan（revision=10），现在 revision 变了
        plan_hash = compute_plan_hash('{"op":"add_subtitle"}')
        grant = approval.issue_after_user_confirmation(ApprovalScope(
            action="commit_plan", project_id="p1", timeline_id="t1",
            base_revision=10, plan_hash=plan_hash,
        ))
        # 消费一次（成功）
        approval.consume(grant.grant_id, ApprovalScope(
            action="commit_plan", project_id="p1", timeline_id="t1",
            base_revision=10, plan_hash=plan_hash,
        ))
        # 红线 2：再消费（即使 revision 没变，single_use 也拒绝）
        with pytest.raises(GrantNotFound):
            approval.consume(grant.grant_id, ApprovalScope(
                action="commit_plan", project_id="p1", timeline_id="t1",
                base_revision=10, plan_hash=plan_hash,
            ))
        # revision 变了 → stale
        grant2 = approval.issue_after_user_confirmation(ApprovalScope(
            action="commit_plan", project_id="p1", timeline_id="t1",
            base_revision=10, plan_hash=plan_hash,
        ))
        with pytest.raises(GrantScopeMismatch):
            approval.consume(grant2.grant_id, ApprovalScope(
                action="commit_plan", project_id="p1", timeline_id="t1",
                base_revision=11, plan_hash=plan_hash,  # revision 变了
            ))
