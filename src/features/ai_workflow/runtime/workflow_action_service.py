"""Host-owned Workflow EditPlan preview/apply boundary (B4).

This service deliberately sits after the provider/adapter and before the host
command stack.  Providers can propose plans, but only this boundary can preview
against the current project, consume a scoped approval grant, and submit one
compiled macro.  It is pure Python and has no Qt/OpenGL dependency.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from src.features.edit.ai_editing.approval_service import (
    ApprovalError,
    ApprovalService,
    compute_diff_hash,
)
from src.features.edit.ai_editing.plan_compiler import compile_plan
from src.features.edit.ai_editing.transaction_service import PreviewResult, _expected_commit_scope


@dataclass(frozen=True)
class WorkflowActionResult:
    ok: bool
    run_id: str
    plan_id: str = ""
    plan_hash: str = ""
    diff_hash: str = ""
    grant_id: str = ""
    status: str = ""
    diagnostic_code: str = ""
    message: str = ""
    new_timeline: Any = None


class WorkflowActionService:
    """Preview and apply plans observed from a workflow run."""

    def __init__(
        self,
        *,
        service_provider: Callable[[], Any],
        bridge_provider: Callable[[], Any],
        approval_provider: Callable[[], ApprovalService | None],
    ) -> None:
        self._service_provider = service_provider
        self._bridge_provider = bridge_provider
        self._approval_provider = approval_provider
        self._plans: dict[str, Any] = {}
        self._previews: dict[str, PreviewResult] = {}
        self._applied: dict[tuple[str, str, str], Any] = {}
        self._lock = threading.RLock()

    def register_plan(self, run_id: str, plan: Any) -> None:
        """Register only the immutable plan reference; do not execute it."""
        plan_id = str(getattr(plan, "plan_id", "") or "")
        if not run_id or not plan_id:
            return
        with self._lock:
            self._plans[run_id] = plan

    def get_plan(self, run_id: str) -> Any | None:
        with self._lock:
            return self._plans.get(run_id)

    def get_preview(self, run_id: str) -> PreviewResult | None:
        with self._lock:
            return self._previews.get(run_id)

    def can_rollback(self, run_id: str) -> bool:
        with self._lock:
            applied = any(rid == run_id for rid, _plan_hash, _diff_hash in self._applied)
        bridge = self._bridge_provider()
        return applied and callable(getattr(bridge, "rollback_ai_command", None))

    def preview(self, run_id: str) -> WorkflowActionResult:
        plan = self.get_plan(run_id)
        if plan is None:
            return self._failure(run_id, "NO_EDIT_PLAN", "Workflow 尚未产出 EditPlan")
        service = self._service_provider()
        bridge = self._bridge_provider()
        if service is None or bridge is None:
            return self._failure(run_id, "WORKFLOW_CONTEXT_INVALID", "AI 工程上下文尚未装配")
        state = bridge.get_project_sequences()
        if state is None:
            return self._failure(run_id, "NO_PROJECT", "没有活动工程，无法预览 EditPlan")
        sequence = state.sequences.get(plan.target.timeline_id)
        if sequence is None:
            return self._failure(run_id, "TIMELINE_NOT_FOUND", "目标时间线不存在")
        current_revision = int(getattr(sequence, "revision", 0))
        if current_revision != int(plan.base_revision):
            return self._failure(
                run_id, "STALE_REVISION",
                f"计划基于 revision {plan.base_revision}，当前为 {current_revision}；请重新规划",
                plan=plan,
            )
        preview = service.preview_plan(plan, state)
        if preview is None or preview.has_errors or preview.compiled is None or preview.diff is None:
            return self._failure(run_id, "PLAN_PREVIEW_FAILED", "EditPlan 预览未通过", plan=plan)
        diff_hash = str(preview.diff_hash or compute_diff_hash(preview.diff))
        with self._lock:
            self._previews[run_id] = preview
        return WorkflowActionResult(
            ok=True,
            run_id=run_id,
            plan_id=str(plan.plan_id),
            plan_hash=self._plan_hash(plan),
            diff_hash=diff_hash,
            status="preview_ready",
            message="EditPlan 预览已生成，等待宿主审批",
        )

    def apply(self, run_id: str, grant_id: str) -> WorkflowActionResult:
        """Consume a scoped grant and submit exactly one macro to the host stack."""
        plan = self.get_plan(run_id)
        if plan is None:
            return self._failure(run_id, "NO_EDIT_PLAN", "Workflow 尚未产出 EditPlan")
        # Check the successful application cache before re-reading the timeline:
        # a successful command necessarily bumps revision, so replay detection
        # must happen before the normal stale-plan guard.
        with self._lock:
            cached_preview = self._previews.get(run_id)
            cached_hash = self._plan_hash(plan)
            if cached_preview is not None:
                cached_diff_hash = str(cached_preview.diff_hash or compute_diff_hash(cached_preview.diff))
                cached_key = (run_id, cached_hash, cached_diff_hash)
                if cached_key in self._applied:
                    return WorkflowActionResult(
                        ok=True, run_id=run_id, plan_id=str(plan.plan_id),
                        plan_hash=cached_hash, diff_hash=cached_diff_hash,
                        status="already_applied", message="该 EditPlan 已应用，未重复写入",
                        new_timeline=self._applied[cached_key],
                    )
        preview_result = self.preview(run_id)
        if not preview_result.ok:
            return preview_result
        key = (run_id, preview_result.plan_hash, preview_result.diff_hash)
        approval = self._approval_provider()
        if approval is None or not grant_id:
            return self._failure(run_id, "APPROVAL_REQUIRED", "缺少宿主审批授权，未修改时间线", plan=plan)
        try:
            approval.consume(
                grant_id,
                _expected_commit_scope(plan, diff_hash=preview_result.diff_hash),
            )
        except ApprovalError as exc:
            return self._failure(run_id, exc.code, str(exc), plan=plan)
        bridge = self._bridge_provider()
        if bridge is None:
            return self._failure(run_id, "WORKFLOW_CONTEXT_INVALID", "AI 工程上下文已失效", plan=plan)
        try:
            compiled = compile_plan(plan)
            new_timeline = bridge.commit_ai_command(
                plan.target.timeline_id, compiled.macro, undo_label=compiled.undo_label,
            )
        except Exception as exc:  # noqa: BLE001 — return explicit failed action
            return self._failure(run_id, "COMMAND_COMMIT_FAILED", str(exc), plan=plan)
        if new_timeline is None:
            return self._failure(run_id, "COMMAND_COMMIT_FAILED", "宿主命令栈未接受 EditPlan", plan=plan)
        refresh = getattr(bridge, "refresh_after_ai_commit", None)
        if callable(refresh):
            refresh(new_timeline)
        with self._lock:
            self._applied[key] = new_timeline
        return WorkflowActionResult(
            ok=True, run_id=run_id, plan_id=str(plan.plan_id),
            plan_hash=preview_result.plan_hash, diff_hash=preview_result.diff_hash,
            status="applied", message="EditPlan 已通过审批并提交到宿主命令栈",
            new_timeline=new_timeline,
        )

    def rollback(self, run_id: str) -> WorkflowActionResult:
        """Undo this run only when the host can prove it is still the latest edit.

        The host owns the undo stack.  This service therefore refuses to guess
        which edit to undo: it passes the original undo label and applied
        revision to an explicit host capability, which may reject the request
        when another edit has happened since the AI commit.
        """
        plan = self.get_plan(run_id)
        if plan is None:
            return self._failure(run_id, "NO_EDIT_PLAN", "Workflow 尚未产出 EditPlan")
        with self._lock:
            applied = [
                value for (rid, _plan_hash, _diff_hash), value in self._applied.items()
                if rid == run_id
            ]
        if not applied:
            return self._failure(run_id, "NO_APPLIED_PLAN", "当前 run 没有可回滚的已应用 EditPlan", plan=plan)
        bridge = self._bridge_provider()
        rollback = getattr(bridge, "rollback_ai_command", None) if bridge is not None else None
        if not callable(rollback):
            return self._failure(
                run_id, "ROLLBACK_UNAVAILABLE",
                "宿主未提供可验证的回滚入口，未修改时间线", plan=plan,
            )
        try:
            compiled = compile_plan(plan)
            applied_timeline = applied[-1]
            expected_revision = getattr(applied_timeline, "revision", None)
            restored = rollback(
                plan.target.timeline_id,
                expected_undo_label=compiled.undo_label,
                expected_revision=expected_revision,
            )
        except Exception as exc:  # noqa: BLE001 — rollback is an explicit diagnostic
            return self._failure(run_id, "ROLLBACK_FAILED", str(exc), plan=plan)
        if restored is None:
            return self._failure(
                run_id, "ROLLBACK_NOT_SAFE",
                "宿主未确认最近一次命令属于当前 AI EditPlan，未执行回滚", plan=plan,
            )
        refresh = getattr(bridge, "refresh_after_ai_commit", None)
        if callable(refresh):
            refresh(restored)
        with self._lock:
            self._applied = {
                key: value for key, value in self._applied.items()
                if key[0] != run_id
            }
        return WorkflowActionResult(
            ok=True, run_id=run_id, plan_id=str(plan.plan_id),
            status="rolled_back", message="当前 AI EditPlan 已通过宿主命令栈回滚",
            new_timeline=restored,
        )

    def request_approval(self, run_id: str) -> WorkflowActionResult:
        """Ask the host approval boundary to issue a grant for the fresh preview."""
        plan = self.get_plan(run_id)
        if plan is None:
            return self._failure(run_id, "NO_EDIT_PLAN", "Workflow 尚未产出 EditPlan")
        preview_result = self.preview(run_id)
        if not preview_result.ok:
            return preview_result
        bridge = self._bridge_provider()
        request = getattr(bridge, "request_plan_approval", None) if bridge is not None else None
        if not callable(request):
            return self._failure(run_id, "APPROVAL_HANDLER_UNAVAILABLE", "宿主未提供审批入口", plan=plan)
        with self._lock:
            preview = self._previews.get(run_id)
        if preview is None:
            return self._failure(run_id, "PLAN_PREVIEW_FAILED", "审批前未找到当前预览", plan=plan)
        try:
            grant = request(plan, preview)
        except Exception as exc:  # noqa: BLE001 — approval boundary returns a diagnostic
            return self._failure(run_id, "APPROVAL_REQUEST_FAILED", str(exc), plan=plan)
        grant_id = str(getattr(grant, "grant_id", "") or "") if grant is not None else ""
        if not grant_id:
            return self._failure(run_id, "APPROVAL_DECLINED", "用户未批准当前 EditPlan", plan=plan)
        return WorkflowActionResult(
            ok=True, run_id=run_id, plan_id=str(plan.plan_id),
            plan_hash=preview_result.plan_hash, diff_hash=preview_result.diff_hash,
            grant_id=grant_id, status="approved", message="宿主已签发当前 EditPlan 授权",
        )

    @staticmethod
    def _plan_hash(plan: Any) -> str:
        import hashlib
        import json

        canonical = json.dumps(plan.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _failure(self, run_id: str, code: str, message: str, *, plan: Any = None) -> WorkflowActionResult:
        return WorkflowActionResult(
            ok=False, run_id=run_id, plan_id=str(getattr(plan, "plan_id", "") or ""),
            plan_hash=self._plan_hash(plan) if plan is not None else "",
            status="failed", diagnostic_code=code, message=message,
        )


__all__ = ["WorkflowActionResult", "WorkflowActionService"]
