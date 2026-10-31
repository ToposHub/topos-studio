"""受控自主剪辑执行器。

This module is the write-side of the AI editing agent.  Model/skill code may
produce an :class:`EditPlan`, but it never receives an approval grant and never
touches the timeline.  ``AutonomousAgent`` turns an explicitly enabled,
project-scoped automation policy into one auditable transaction:

    current state -> fresh preview -> compile consistency gate -> policy gate ->
    one-use grant -> CommandStack commit -> revision verification (or guarded rollback)

The class is deliberately Qt/GPU/provider agnostic so it can be tested with
small fakes and reused by the desktop controller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from src.features.edit.ai_editing.approval_service import ApprovalGrant
from src.features.edit.ai_editing.models import EditPlan


logger = logging.getLogger(__name__)


DEFAULT_FORBIDDEN_OPERATIONS: frozenset[str] = frozenset({
    "create_timeline",
    "delete_clip",
    "replace_clip",
    "set_track_state",
})


@dataclass(frozen=True)
class AutonomousAgentConfig:
    """Hard limits for one autonomous timeline transaction."""

    enabled: bool = False
    max_operations: int = 64
    forbidden_operations: frozenset[str] = DEFAULT_FORBIDDEN_OPERATIONS
    require_post_commit_verification: bool = True

    def __post_init__(self) -> None:
        if self.max_operations <= 0:
            raise ValueError("max_operations must be positive")


@dataclass(frozen=True)
class AutonomousApplyResult:
    """Sanitized outcome suitable for runtime diagnostics and UI notices."""

    applied: bool
    code: str
    message: str
    plan_id: str = ""
    new_revision: int | None = None
    rolled_back: bool = False
    rollback_failures: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutonomousStageEvent:
    """A non-sensitive progress signal for one guarded write stage."""

    stage: str
    passed: bool
    code: str = ""
    message: str = ""


class AutonomousAgentHost(Protocol):
    """Trusted host boundary supplied by the desktop controller."""

    def get_project_id(self) -> str: ...

    def get_project_sequences(self) -> Any: ...

    def check_ai_action(self, action: Any, **kwargs: Any) -> Any: ...

    def issue_automatic_plan_approval(
        self, plan: EditPlan, preview: Any, *, asset_ids: tuple[str, ...] = (),
    ) -> ApprovalGrant | None: ...

    def is_autonomous_mode_enabled(self) -> bool: ...

    def detect_autonomous_replay(self, plan: EditPlan, state: Any) -> Any: ...

    def get_selected_asset_ids(self) -> tuple[str, ...]: ...

    def approval_service(self) -> Any: ...

    def commit_ai_command(self, timeline_id: str, macro: Any, *, undo_label: str) -> Any: ...

    def rollback_ai_command(
        self, timeline_id: str, *, expected_undo_label: str,
        expected_revision: int | None,
    ) -> Any: ...

    def refresh_after_ai_commit(self, result: Any) -> None: ...


class AutonomousAgent:
    """Apply one plan autonomously after all host-side gates pass.

    ``preview_plan`` and ``compile_plan`` are injected to keep this class out of
    the session/compiler implementation details.  The injected preview always
    runs against the current host state, never against a cached preview from
    the model run.
    """

    def __init__(
        self,
        host: AutonomousAgentHost,
        *,
        preview_plan: Callable[[EditPlan, Any], Any],
        compile_plan: Callable[[EditPlan], Any],
        config: AutonomousAgentConfig | None = None,
        on_stage: Callable[[AutonomousStageEvent], None] | None = None,
    ) -> None:
        self._host = host
        self._preview_plan = preview_plan
        self._compile_plan = compile_plan
        self._config = config or AutonomousAgentConfig()
        self._on_stage = on_stage

    @property
    def config(self) -> AutonomousAgentConfig:
        return self._config

    def apply(self, plan: EditPlan) -> AutonomousApplyResult:
        """Attempt one bounded, current-revision-bound autonomous commit."""
        plan_id = str(getattr(plan, "plan_id", "") or "")
        if not self._config.enabled:
            return self._blocked(plan_id, "AUTO_MODE_DISABLED", "受限自动模式未启用")

        operations = tuple(getattr(plan, "operations", ()) or ())
        if not operations:
            return self._blocked(plan_id, "EMPTY_PLAN", "编辑计划为空")
        if len(operations) > self._config.max_operations:
            return self._blocked(
                plan_id, "AUTO_OPERATION_LIMIT", "编辑操作数量超过自动执行上限",
            )
        forbidden = self._forbidden_operations(operations)
        if forbidden:
            return self._blocked(
                plan_id,
                "AUTO_RISK_OPERATION",
                f"计划包含需人工确认的高风险操作：{', '.join(forbidden)}",
            )
        plan_asset_ids = self._asset_ids(operations)
        selected_asset_ids = tuple(self._host.get_selected_asset_ids() or ())
        if selected_asset_ids and not set(plan_asset_ids).issubset(set(selected_asset_ids)):
            return self._blocked(
                plan_id,
                "AUTO_ASSET_SCOPE_MISMATCH",
                "计划引用了当前选择范围之外的素材，必须重新规划",
            )

        state = self._host.get_project_sequences()
        if state is None:
            return self._blocked(plan_id, "NO_PROJECT_STATE", "当前工程状态不可用")
        sequences = getattr(state, "sequences", {}) or {}
        target = getattr(plan, "target", None)
        project_id = str(getattr(target, "project_id", "") or "")
        timeline_id = str(getattr(target, "timeline_id", "") or "")
        if not project_id or project_id != str(self._host.get_project_id() or ""):
            return self._blocked(plan_id, "PROJECT_SCOPE_MISMATCH", "计划不属于当前工程")
        sequence = sequences.get(timeline_id)
        if sequence is None:
            return self._blocked(plan_id, "TIMELINE_SCOPE_MISMATCH", "计划目标时间线不可用")
        current_revision = int(getattr(sequence, "revision", -1))
        base_revision = int(getattr(plan, "base_revision", -1))
        replay_checker = getattr(self._host, "detect_autonomous_replay", None)
        if callable(replay_checker):
            try:
                replay = replay_checker(plan, state)
            except Exception:
                replay = None
            if isinstance(replay, dict):
                if bool(replay.get("ambiguous", False)):
                    return self._blocked(
                        plan_id,
                        "AUTO_REPLAY_AMBIGUOUS",
                        str(replay.get("reason", "检测到部分计划可能已提交，请先人工检查")),
                    )
                if bool(replay.get("already_applied", False)):
                    return AutonomousApplyResult(
                        applied=True,
                        code="AUTO_ALREADY_APPLIED",
                        message="检测到该计划已提交，已跳过重复写入",
                        plan_id=plan_id,
                        new_revision=current_revision,
                    )
        if base_revision != current_revision:
            return self._blocked(
                plan_id, "STALE_PLAN", "时间线 revision 已变化，必须重新规划",
            )

        try:
            preview = self._preview_plan(plan, state)
        except Exception as exc:
            return self._blocked(plan_id, "AUTO_PREVIEW_EXCEPTION", str(exc))
        if preview is None:
            return self._blocked(plan_id, "AUTO_PREVIEW_FAILED", "计划预览未生成")
        if bool(getattr(preview, "has_errors", False)):
            return self._blocked(plan_id, "AUTO_PREVIEW_INVALID", "计划未通过当前工程校验")
        compiled_preview = getattr(preview, "compiled", None)
        if compiled_preview is None:
            return self._blocked(plan_id, "AUTO_PREVIEW_INCOMPLETE", "预览缺少可提交编译结果")

        # Compilation is pure, so do it before any approval is issued or
        # consumed.  The preview and commit paths must describe the same plan;
        # otherwise a provider/compiler race could authorize one diff and write
        # another.
        try:
            compiled = self._compile_plan(plan)
        except Exception as exc:
            return self._blocked(plan_id, "AUTO_COMPILE_FAILED", str(exc))
        if not self._compiled_results_match(compiled_preview, compiled):
            return self._blocked(
                plan_id,
                "AUTO_PREVIEW_COMPILE_MISMATCH",
                "预览与提交编译结果不一致，必须重新规划",
            )
        self._emit_stage(AutonomousStageEvent(
            stage="preview", passed=True, code="AUTO_PREVIEW_PASSED",
            message="当前 revision 预览与提交编译结果一致",
        ))

        policy = self._host.check_ai_action(
            "timeline_write",
            timeline_id=timeline_id,
            base_revision=current_revision,
            asset_ids=plan_asset_ids,
            approval_granted=True,
        )
        if not bool(getattr(policy, "allowed", False)):
            return self._blocked(
                plan_id, "AUTO_POLICY_BLOCKED", str(getattr(policy, "reason", "策略拒绝")),
            )

        grant = self._host.issue_automatic_plan_approval(
            plan, preview, asset_ids=plan_asset_ids,
        )
        if grant is None:
            return self._blocked(plan_id, "AUTO_APPROVAL_UNAVAILABLE", "自动授权未签发")
        approval_service = self._host.approval_service()
        # Re-read the live safety boundary after preview/approval.  This closes
        # the race where an emergency kill switch is activated while the plan is
        # being compiled or waiting on the host boundary.
        live_checker = getattr(self._host, "is_autonomous_mode_enabled", None)
        if callable(live_checker):
            try:
                if not live_checker():
                    self._revoke_safely(approval_service, grant)
                    return self._blocked(
                        plan_id, "AUTO_MODE_DISABLED", "自主剪辑已被紧急停用",
                    )
            except Exception:
                # AUD-13104：紧急停用分支中 revoke 自身抛异常时不得从 apply()
                # 逃逸（apply 的契约是"总返回结构化结果"），且一次性 grant 必须
                # 尽力撤销——撤销失败仅记诊断日志，等 TTL 过期兜底。
                self._revoke_safely(approval_service, grant)
                return self._blocked(
                    plan_id, "AUTO_MODE_DISABLED", "无法确认自主剪辑安全状态",
                )
        if approval_service is None:
            return self._blocked(plan_id, "APPROVAL_SERVICE_UNAVAILABLE", "授权服务不可用")

        try:
            from src.features.edit.ai_editing.transaction_service import _expected_commit_scope

            approval_service.consume(
                grant.grant_id,
                _expected_commit_scope(plan, diff_hash=getattr(preview, "diff_hash", None)),
            )
            macro = getattr(compiled, "macro", None)
            undo_label = str(getattr(compiled, "undo_label", "") or plan.title or "AI 自动剪辑")
            if macro is None:
                return self._blocked(plan_id, "AUTO_COMPILE_FAILED", "计划编译结果为空")
            new_timeline = self._host.commit_ai_command(
                timeline_id, macro, undo_label=undo_label,
            )
        except Exception as exc:
            return self._blocked(plan_id, "AUTO_COMMIT_EXCEPTION", str(exc))

        if new_timeline is None:
            return self._blocked(plan_id, "AUTO_COMMIT_FAILED", "CommandStack 未接受计划")
        new_revision = int(getattr(new_timeline, "revision", -1))
        if new_revision <= current_revision:
            # AUD-13102：回滚调用本身也要防异常——apply() 的契约是"总返回结构化
            # 结果"；host 回滚抛异常时不得让异常逃出并丢失 AUTO_VERIFY_FAILED 诊断。
            rollback_failure = self._rollback_safely(
                timeline_id, undo_label, new_revision,
            )
            return AutonomousApplyResult(
                applied=False,
                code="AUTO_VERIFY_FAILED",
                message="提交后 revision 未按预期变化，已尝试恢复",
                plan_id=plan_id,
                new_revision=new_revision,
                rolled_back=rollback_failure is None,
                rollback_failures=(
                    () if rollback_failure is None
                    else (rollback_failure, "revision verification failed")
                ),
            )
        self._emit_stage(AutonomousStageEvent(
            stage="commit", passed=True, code="AUTO_COMMIT_PASSED",
            message="命令栈提交成功且 revision 前进",
        ))

        post_verifier = getattr(self._host, "verify_autonomous_commit", None)
        if self._config.require_post_commit_verification and callable(post_verifier):
            try:
                verification = post_verifier(
                    plan, state, new_timeline, preview,
                )
                if isinstance(verification, dict):
                    passed = bool(verification.get("passed", False))
                    reason = str(verification.get("reason", ""))
                else:
                    passed = bool(getattr(verification, "passed", verification is True))
                    reason = str(getattr(verification, "reason", ""))
            except Exception as exc:
                passed = False
                reason = f"post-commit verification failed: {exc}"
            if not passed:
                # AUD-13102：后置校验失败的回滚同样防异常，保持结构化结果契约。
                rollback_failure = self._rollback_safely(
                    timeline_id, undo_label, new_revision,
                )
                detail = reason[:240] if reason else "未满足计划后置质量条件"
                return AutonomousApplyResult(
                    applied=False,
                    code="AUTO_POST_VERIFY_FAILED",
                    message=f"自动剪辑后置校验失败：{detail}",
                    plan_id=plan_id,
                    new_revision=new_revision,
                    rolled_back=rollback_failure is None,
                    rollback_failures=(
                        () if rollback_failure is None
                        else (
                            rollback_failure,
                            "post-commit verification rollback failed",
                        )
                    ),
                )

        self._emit_stage(AutonomousStageEvent(
            stage="verify", passed=True, code="AUTO_VERIFY_PASSED",
            message="提交后质量校验通过",
        ))

        self._host.refresh_after_ai_commit(new_timeline)
        return AutonomousApplyResult(
            applied=True,
            code="AUTO_APPLIED",
            message="AI 已在当前范围内完成一次可撤销的自动剪辑",
            plan_id=plan_id,
            new_revision=new_revision,
        )

    def _forbidden_operations(self, operations: tuple[Any, ...]) -> tuple[str, ...]:
        found: list[str] = []
        for operation in operations:
            kind = getattr(getattr(operation, "kind", None), "value", None)
            kind = str(kind or getattr(operation, "kind", ""))
            if kind in self._config.forbidden_operations and kind not in found:
                found.append(kind)
        return tuple(found)

    def _emit_stage(self, event: AutonomousStageEvent) -> None:
        """Notify the mission ledger without making it part of the write path."""
        if self._on_stage is None:
            return
        try:
            self._on_stage(event)
        except Exception:
            # Telemetry/UI bookkeeping must never turn a committed command into
            # an application failure or bypass the host's safety gates.
            return

    @staticmethod
    def _revoke_safely(approval_service: Any, grant: ApprovalGrant) -> None:
        """尽力撤销一次性授权 grant，撤销失败不传播异常（AUD-13104）。

        紧急停用分支中 revoke 若再抛异常，会突破 ``apply()`` "总返回结构化
        结果"的契约，并让已签发的 grant 既未消费也未撤销（只能等 TTL 过期）。
        这里包一层受保护的撤销：失败降级为 warning 日志留痕。
        """
        if approval_service is None:
            return
        try:
            approval_service.revoke(grant.grant_id)
        except Exception as exc:  # noqa: BLE001 — 撤销失败仅留痕，不阻塞阻断路径
            logger.warning(
                "failed to revoke auto-approval grant %r: %s: %s",
                getattr(grant, "grant_id", ""), type(exc).__name__, exc,
            )

    def _rollback_safely(
        self, timeline_id: str, undo_label: str, expected_revision: int,
    ) -> str | None:
        """执行一次受保护的回滚（AUD-13102）。

        Returns:
            ``None`` 表示回滚成功（host 返回非 None 结果）；否则返回可记入
            ``rollback_failures`` 的失败描述——host 抛出的异常转为结构化
            诊断，绝不让异常逃出 :meth:`apply` 的"总返回结构化结果"契约。
        """
        try:
            rollback = self._host.rollback_ai_command(
                timeline_id,
                expected_undo_label=undo_label,
                expected_revision=expected_revision,
            )
        except Exception as exc:  # noqa: BLE001 — host 回滚异常转为 rollback_failures
            return f"{type(exc).__name__}: {exc}"
        if rollback is None:
            return "rollback returned no result"
        return None

    @staticmethod
    def _compiled_results_match(preview_compiled: Any, commit_compiled: Any) -> bool:
        """Compare stable compile metadata without comparing command objects.

        AUD-13103：除 ``plan_id`` / ``operation_count`` 外还比较
        ``operation_summaries``（op_id → 编译摘要，plan_compiler 的稳定元数据）。
        仅比较"双侧都可用"的字段（旧编译结果缺该元数据时跳过，向后兼容）；
        两侧摘要不一致说明同 plan 两次编译产出了内容不同的命令（如插件注册表
        /效果目录在预览与提交之间刷新）——grant 绑定的是 preview 的 diff_hash，
        实际提交的却是另一份 macro，必须拦截。
        """
        if preview_compiled is None or commit_compiled is None:
            return False
        for attribute in ("plan_id", "operation_count", "operation_summaries"):
            preview_value = getattr(preview_compiled, attribute, None)
            commit_value = getattr(commit_compiled, attribute, None)
            if preview_value is None or commit_value is None:
                continue
            if preview_value != commit_value:
                return False
        return getattr(commit_compiled, "macro", None) is not None

    @staticmethod
    def _asset_ids(operations: tuple[Any, ...]) -> tuple[str, ...]:
        values: list[str] = []
        for operation in operations:
            params = getattr(operation, "params", {}) or {}
            asset_id = str(params.get("asset_id", "") or "")
            if asset_id and asset_id not in values:
                values.append(asset_id)
        return tuple(values)

    @staticmethod
    def _blocked(plan_id: str, code: str, message: str) -> AutonomousApplyResult:
        return AutonomousApplyResult(
            applied=False, code=code, message=message[:500], plan_id=plan_id,
        )


__all__ = [
    "AutonomousAgent",
    "AutonomousAgentConfig",
    "AutonomousAgentHost",
    "AutonomousApplyResult",
    "AutonomousStageEvent",
    "DEFAULT_FORBIDDEN_OPERATIONS",
]
