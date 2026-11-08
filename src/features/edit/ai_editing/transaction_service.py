"""TAEP Transaction Service（Phase 2）。

编排 ``validate → compile → simulate(diff) → commit`` 的完整事务（TAEP §6.1）。
它是 ``EditProjectService``（P0 单 command 提交）之上的 plan 级封装。

职责：

1. **validate**：调 ``plan_validator.validate_plan``（schema + domain）。
2. **compile**：调 ``plan_compiler.compile_plan``（operation → MacroCommand）。
3. **simulate**：在副本上 MacroCommand.apply，得到 proposed sequence + diff（不 bump）。
4. **commit**：乐观锁检查 + ``EditProjectService.commit_command``（bump + sync 两层）。
5. **audit**：可选记录 plan/revision/diagnostics 摘要（不保存 secret/对话/路径）。

不变量：

- preview 与 commit 复用同一 compiler（保证 proposed 与 commit 后 byte-equal）。
- 任一中间步骤失败都不产生半套状态（commit_command 已保证原子性）。
- revision 冲突不自动 last-write-wins；返回 STALE 供调用方刷新。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING, Iterable

from src.features.edit.ai_editing.approval_service import (
    ApprovalError,
    ApprovalGrant,
    ApprovalScope,
    ApprovalService,
    compute_diff_hash,
    compute_plan_hash,
)
from src.features.edit.ai_editing.edit_project_service import (
    CommitResult,
    EditProjectService,
    ProjectSequences,
    RevisionConflict,
)
from src.features.edit.ai_editing.models import (
    CapabilityManifest,
    EditPlan,
    PlanStatus,
    ValidationReport,
)
from src.features.edit.ai_editing.plan_compiler import CompileError, CompiledPlan, compile_plan
from src.features.edit.ai_editing.plan_validator import validate_plan

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import TimelineSequence


# ---------------------------------------------------------------------- #
# TimelineDiff（TAEP §6.1 步骤 6 / P2 验收）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClipDiff:
    """单个 clip 的变更摘要。"""

    change: str  # "added" / "removed" / "moved" / "trimmed" / "replaced"
    clip_id: str
    track_id: str
    before: dict[str, Any] | None = None  # timeline/source range
    after: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "change": self.change,
            "clip_id": self.clip_id,
            "track_id": self.track_id,
        }
        if self.before is not None:
            d["before"] = dict(self.before)
        if self.after is not None:
            d["after"] = dict(self.after)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClipDiff":
        return cls(
            change=str(data.get("change", "")),
            clip_id=str(data.get("clip_id", "")),
            track_id=str(data.get("track_id", "")),
            before=dict(data["before"]) if data.get("before") is not None else None,
            after=dict(data["after"]) if data.get("after") is not None else None,
        )


@dataclass(frozen=True)
class TimelineDiff:
    """proposed timeline 相对 base 的结构化 diff。

    供 UI 展示新增/删除/移动、时长变化、受影响轨、风险与导出预估（TAEP §11.6 步骤 10）。
    """

    added_clips: tuple[ClipDiff, ...] = ()
    removed_clips: tuple[ClipDiff, ...] = ()
    moved_clips: tuple[ClipDiff, ...] = ()
    trimmed_clips: tuple[ClipDiff, ...] = ()
    duration_before: int = 0
    duration_after: int = 0
    affected_track_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def total_changes(self) -> int:
        return (
            len(self.added_clips) + len(self.removed_clips)
            + len(self.moved_clips) + len(self.trimmed_clips)
        )

    def to_dict(self) -> dict[str, Any]:
        def _clip(c: ClipDiff) -> dict[str, Any]:
            d: dict[str, Any] = {"change": c.change, "clip_id": c.clip_id, "track_id": c.track_id}
            if c.before is not None:
                d["before"] = dict(c.before)
            if c.after is not None:
                d["after"] = dict(c.after)
            return d

        return {
            "added": [_clip(c) for c in self.added_clips],
            "removed": [_clip(c) for c in self.removed_clips],
            "moved": [_clip(c) for c in self.moved_clips],
            "trimmed": [_clip(c) for c in self.trimmed_clips],
            "duration_before": self.duration_before,
            "duration_after": self.duration_after,
            "affected_track_ids": list(self.affected_track_ids),
            "warnings": list(self.warnings),
            "total_changes": self.total_changes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimelineDiff":
        """从 ``to_dict`` 的输出重建（用于工程重开后的 diff 回看）。

        ``total_changes`` 是派生字段，不参与重建。
        """
        def _clips(key: str) -> tuple[ClipDiff, ...]:
            return tuple(ClipDiff.from_dict(c) for c in data.get(key, []))
        return cls(
            added_clips=_clips("added"),
            removed_clips=_clips("removed"),
            moved_clips=_clips("moved"),
            trimmed_clips=_clips("trimmed"),
            duration_before=int(data.get("duration_before", 0)),
            duration_after=int(data.get("duration_after", 0)),
            affected_track_ids=tuple(data.get("affected_track_ids", [])),
            warnings=tuple(data.get("warnings", [])),
        )


def compute_diff(base: TimelineSequence, proposed: TimelineSequence, compiled: CompiledPlan | None = None) -> TimelineDiff:
    """计算 base → proposed 的 clip 级 diff。

    Args:
        base: 原始 TimelineSequence。
        proposed: MacroCommand.apply 后的 sequence。
        compiled: 保留参数（向后兼容）。变更类型现完全由 base/proposed 的
            状态差决定——operation_summaries 只保留每个 clip 最后一个 op 的
            kind，不足以区分"移动+裁切"这类复合变更（AUD-14813）。
    """
    def _timeline_clips(sequence: Any) -> list[Any]:
        """Return media and subtitle clips in one diff namespace."""
        clips: list[Any] = []
        for track in sequence.all_tracks:
            clips.extend(getattr(track, "clips", ()) or ())
            clips.extend(getattr(track, "subtitle_clips", ()) or ())
        return clips

    base_clips = {c.clip_id: c for c in _timeline_clips(base)}
    proposed_clips = {c.clip_id: c for c in _timeline_clips(proposed)}

    added: list[ClipDiff] = []
    removed: list[ClipDiff] = []
    moved: list[ClipDiff] = []
    trimmed: list[ClipDiff] = []
    affected: set[str] = set()

    def _range_payload(clip: Any) -> dict[str, Any]:
        timeline = {
            "start": int(clip.timeline_start),
            "duration": int(clip.timeline_duration),
        }
        # AUD-14813：before/after 载荷必须含 track，批准 UI 才能看到轨道变更。
        payload: dict[str, Any] = {"track": str(clip.track_id), "timeline": timeline}
        if hasattr(clip, "source_start") and hasattr(clip, "source_duration"):
            payload["source"] = {
                "start": int(clip.source_start),
                "duration": int(clip.source_duration),
            }
        if hasattr(clip, "text"):
            payload["text"] = str(clip.text)
        return payload

    for cid, clip in proposed_clips.items():
        if cid not in base_clips:
            added.append(ClipDiff(
                change="added", clip_id=cid, track_id=clip.track_id,
                after=_range_payload(clip),
            ))
            affected.add(clip.track_id)
        else:
            old = base_clips[cid]
            before = _range_payload(old)
            after = _range_payload(clip)
            track_changed = old.track_id != clip.track_id
            start_changed = old.timeline_start != clip.timeline_start
            duration_changed = old.timeline_duration != clip.timeline_duration
            if track_changed or start_changed:
                # AUD-14813：位置/轨道变化归 "moved"。若时长同时变化则双记
                # "trimmed"——旧实现的单标签分类会把"跨轨移动+裁切"折叠成
                # "trimmed"（op_kinds_by_clip 只保留最后一个 op 的 kind），
                # 轨道变更既不在 change 标签里也不在载荷里，批准 UI 看不到
                # 轨道变更且 total_changes 少计。
                moved.append(ClipDiff(change="moved", clip_id=cid, track_id=clip.track_id, before=before, after=after))
                if duration_changed:
                    trimmed.append(ClipDiff(change="trimmed", clip_id=cid, track_id=clip.track_id, before=before, after=after))
                affected.add(clip.track_id)
                if track_changed:
                    affected.add(old.track_id)
            elif duration_changed:
                trimmed.append(ClipDiff(change="trimmed", clip_id=cid, track_id=clip.track_id, before=before, after=after))
                affected.add(clip.track_id)

    for cid, clip in base_clips.items():
        if cid not in proposed_clips:
            removed.append(ClipDiff(
                change="removed", clip_id=cid, track_id=clip.track_id,
                before=_range_payload(clip),
            ))
            affected.add(clip.track_id)

    warnings: list[str] = []
    if removed:
        warnings.append(f"将删除 {len(removed)} 个片段，提交前请确认")

    return TimelineDiff(
        added_clips=tuple(added),
        removed_clips=tuple(removed),
        moved_clips=tuple(moved),
        trimmed_clips=tuple(trimmed),
        duration_before=base.duration_frames,
        duration_after=proposed.duration_frames,
        affected_track_ids=tuple(sorted(affected)),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------- #
# TransactionService
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class PreviewResult:
    """validate + compile + simulate 的结果（不提交）。

    Attributes:
        diff_hash: ``diff`` 内容的 sha256（``compute_diff_hash``）。绑定"用户看到
            的 diff == 将要提交的 diff"（总体不变量 §1.4）。``diff`` 为 None（校验/
            编译失败）时 diff_hash 也为 None。宿主签发 commit grant 时应把此值写入
            ``ApprovalScope.diff_hash``，使 commit 复用同一份 diff。
    """

    report: ValidationReport
    compiled: CompiledPlan | None
    diff: TimelineDiff | None
    proposed_sequence: Any | None = None
    diff_hash: str | None = None

    @property
    def has_errors(self) -> bool:
        return self.report.has_errors

    def to_dict(self) -> dict[str, Any]:
        """可安全序列化的子集（供工程持久化/历史回看）。

        **不**含 ``compiled`` / ``proposed_sequence``——它们持有活对象/编译产物，
        不可 JSON 化，且恢复后必须以当前 revision 重新 preview
        （``PlanRepository`` 红线：恢复后 ``is_preview_stale`` 应为 True）。
        只保留 ``report``、``diff``、``diff_hash`` 供 UI 回看历史。
        """
        return {
            "report": self.report.to_dict(),
            "diff": self.diff.to_dict() if self.diff is not None else None,
            "diff_hash": self.diff_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PreviewResult":
        """从持久化的子集重建（``compiled``/``proposed_sequence`` 留空）。

        重建后的 ``PreviewResult`` 只用于回看历史 diff，**不可**直接用于 commit
        ——commit 前必须重新 preview 拿到新鲜的 ``compiled`` + ``proposed_sequence``。
        """
        from src.features.edit.ai_editing.models import ValidationReport
        diff_data = data.get("diff")
        return cls(
            report=ValidationReport.from_dict(data.get("report", {})),
            compiled=None,  # 活对象不持久化
            diff=TimelineDiff.from_dict(diff_data) if diff_data else None,
            proposed_sequence=None,  # 活对象不持久化
            diff_hash=data.get("diff_hash"),
        )


@dataclass(frozen=True)
class AuditRecord:
    """可选审计记录（TAEP §6.3）。不保存 secret/对话/绝对路径。"""

    plan_id: str
    protocol: str
    base_revision: int
    new_revision: int | None
    undo_label: str
    operation_count: int
    status: str
    diagnostics_summary: dict[str, int] = field(default_factory=dict)
    timestamp: float = 0.0
    client: str = ""  # 模型/客户端标识（不含 secret）

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "protocol": self.protocol,
            "base_revision": self.base_revision,
            "new_revision": self.new_revision,
            "undo_label": self.undo_label,
            "operation_count": self.operation_count,
            "status": self.status,
            "diagnostics_summary": dict(self.diagnostics_summary),
            "timestamp": self.timestamp,
            "client": self.client,
        }


class TransactionService:
    """plan 级事务编排（validate → compile → simulate → commit → audit）。"""

    def __init__(self, audit_sink: Any | None = None) -> None:
        """audit_sink: 可选 callable(AuditRecord)；None 时不审计。"""
        self._audit_sink = audit_sink

    # ---------------- validate + compile + simulate ---------------- #

    def preview(
        self,
        plan: EditPlan,
        state: ProjectSequences,
        *,
        manifest: CapabilityManifest | None = None,
    ) -> PreviewResult:
        """validate → compile → simulate（不提交，不 bump）。

        返回 ``PreviewResult``，含 ValidationReport / CompiledPlan / TimelineDiff /
        proposed_sequence。调用方据此向用户展示 diff 并请求批准。
        """
        report = validate_plan(plan, state=state, manifest=manifest)
        if report.has_errors:
            return PreviewResult(report=report, compiled=None, diff=None)

        try:
            compiled = compile_plan(plan)
        except CompileError as exc:
            from src.features.edit.ai_editing.diagnostics import (
                DiagnosticSeverity,
                TaepDiagnosticCode,
                make_diagnostic,
            )
            err_diag = make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                op_id=exc.op_id, path=f"/operations/{exc.op_id}",
                detail=f"compile failed: {exc.reason}",
            )
            from src.features.edit.ai_editing.models import ValidationReport as VR

            failed_report = VR(
                plan_id=plan.plan_id,
                status=PlanStatus.REJECTED,
                base_revision=plan.base_revision,
                current_revision=report.current_revision,
                diagnostics=report.diagnostics + (err_diag,),
            )
            return PreviewResult(report=failed_report, compiled=None, diff=None)

        # simulate：在副本上 apply（不 bump）
        try:
            sequence = EditProjectService.get_sequence(state, plan.target.timeline_id)
            proposed = compiled.macro.apply(sequence)
            if isinstance(proposed, tuple):
                proposed = proposed[0]
        except Exception as exc:
            from src.features.edit.ai_editing.diagnostics import (
                Diagnostic,
                DiagnosticSeverity,
                TaepDiagnosticCode,
            )
            from src.features.edit.ai_editing.models import ValidationReport as VR

            # AUD-14812：走到 simulate 前 plan 已通过 schema+domain 校验，
            # 此处异常来自 MacroCommand.apply 的运行期缺陷（AttributeError/
            # KeyError 等内部 bug 或数据问题），不是用户可修复的 plan 格式
            # 问题——不得标 SCHEMA_INVALID（会误导模型/用户反复改 plan）。
            # 用 INTERNAL_ERROR 并保留异常类型，便于归因。INTERNAL_ERROR 无
            # 消息模板（make_diagnostic 会丢弃 detail），直接构造 Diagnostic
            # 保证失败原因进入 message 而非只剩裸码。
            err_diag = Diagnostic(
                severity=DiagnosticSeverity.ERROR,
                code=TaepDiagnosticCode.INTERNAL_ERROR,
                message=f"simulation failed: {type(exc).__name__}: {exc}",
            )
            failed_report = VR(
                plan_id=plan.plan_id, status=PlanStatus.REJECTED,
                base_revision=plan.base_revision, current_revision=report.current_revision,
                diagnostics=report.diagnostics + (err_diag,),
            )
            return PreviewResult(report=failed_report, compiled=compiled, diff=None)

        diff = compute_diff(sequence, proposed, compiled)
        return PreviewResult(
            report=report, compiled=compiled, diff=diff, proposed_sequence=proposed,
            diff_hash=compute_diff_hash(diff),
        )

    # ---------------- commit ---------------- #

    def commit(
        self,
        plan: EditPlan,
        state: ProjectSequences,
        *,
        manifest: CapabilityManifest | None = None,
        approval: "ApprovalGrant | ApprovalService | str | None" = None,
        approval_service: "ApprovalService | None" = None,
        client: str = "",
        position_locked_clip_ids: "Iterable[str] | None" = None,
    ) -> CommitResult:
        """validate → compile → 乐观锁 → 原子提交 → audit。

        授权契约（M0.1）：commit 不再接受任意"非空字符串 token"。调用方必须提供：

        - 一个真实的 ``ApprovalGrant``（由受信任宿主签发，见 ``ApprovalService``）**并**
          同时提供 ``approval_service``——内部经 ``consume`` 校验作用域并把
          ``single_use`` grant 标记为已用（防重放，红线 §0 第二条）；或
        - （兼容/测试）一个字符串——**仅当**同时提供 ``approval_service`` 时被解释为
          grant_id 走 ``consume``；否则字符串被视为未授权并拒绝。

        注意（AUD-14811）：直接传 ``ApprovalService`` 本身**不是**有效授权。
        grant_id 是不透明的，``consume`` 必须指名消费哪个 grant；"传入 service
        就自动构造 scope 并 consume"的路径无法确定用户究竟批准了哪一个
        grant，因此恒被拒绝（返回 ``TAEP_PERMISSION_DENIED`` 语义的 CommitResult）。

        成功后两层状态更新，产生单个 undo group。授权失败返回稳定
        ``TAEP_PERMISSION_DENIED``，project 完全不变。
        """
        # 先 preview（validate → compile → simulate），拿到 diff_hash 后再授权。
        # 这样 ``ApprovalScope`` 同时绑定 plan_hash 与 diff_hash：用户批准的那一份
        # diff 必须与将要提交的那一份 diff 完全一致（总体不变量 §1.4：预览即提交）。
        # preview/commit 复用同一 compiler，proposed 与 commit 后 byte-equal
        # （已有不变量），不重复执行模型推理。
        preview = self.preview(plan, state, manifest=manifest)
        if preview.has_errors or preview.compiled is None:
            return CommitResult(
                applied=False,
                new_project=state.project,
                new_sequences=dict(state.sequences),
                error="plan did not pass validation; cannot commit",
                errors=tuple(d.message for d in preview.report.errors),
            )

        # AUD-14817：授权（consume 会把 single_use grant 标记为已用）之前，
        # 先把"可预先失败"的检查做掉——乐观锁比较是纯状态读取，这里预检与
        # commit_command 内部的正式乐观锁在同一（未变更的）state 上执行，结果
        # 一致。revision 冲突时直接返回冲突结果，grant 不再被白白烧掉。
        try:
            current_sequence = EditProjectService.get_sequence(
                state, plan.target.timeline_id
            )
        except ValueError as exc:
            # 与 commit_command 的 ValueError→CommitResult 折叠行为一致，
            # 只是把失败提前到授权之前。
            return CommitResult(
                applied=False,
                new_project=state.project,
                new_sequences=dict(state.sequences),
                error=str(exc),
            )
        if (
            plan.base_revision is not None
            and plan.base_revision != current_sequence.revision
        ):
            conflict = RevisionConflict(
                plan.base_revision, current_sequence.revision,
                timeline_id=plan.target.timeline_id,
            )
            return CommitResult(
                applied=False,
                new_project=state.project,
                new_sequences=dict(state.sequences),
                error=str(conflict),
                errors=(str(conflict),),
            )

        grant = self._authorize(
            plan, state, approval, approval_service, diff_hash=preview.diff_hash,
        )
        if grant is None:
            return CommitResult(
                applied=False,
                new_project=state.project,
                new_sequences=dict(state.sequences),
                error=(
                    "commit requires a valid ApprovalGrant issued by the host "
                    "(TAEP §5.6); a bare string token is not accepted without an "
                    "ApprovalService to consume it"
                ),
            )

        # 原子提交（commit_command 内部做乐观锁 + bump + sync；
        # position_locked_clip_ids 为 W06 显式锁策略，None = 不锁）
        try:
            result = EditProjectService.commit_command(
                state, plan.target.timeline_id, preview.compiled.macro,
                base_revision=plan.base_revision,
                undo_label=preview.compiled.undo_label,
                position_locked_clip_ids=position_locked_clip_ids,
            )
        except RevisionConflict:
            # AUD-14817 残余窗口（预检与提交之间 state 被并发改写）：
            # grant 已消耗且提交未发生，错误文案必须明确告知调用方需要重新批准，
            # 不要让用户以为重试同一个 grant 即可。
            conflict_error = (
                "revision conflict; refresh snapshot and regenerate plan "
                "(the approval grant was already consumed and cannot be reused; "
                "re-approve after refreshing)"
            )
            return CommitResult(
                applied=False,
                new_project=state.project,
                new_sequences=dict(state.sequences),
                error=conflict_error,
                errors=(conflict_error,),
            )

        # audit（不保存 secret/对话/路径）
        if self._audit_sink is not None:
            self._record_audit(plan, result, client, preview.compiled.operation_count)

        return result

    @staticmethod
    def _authorize(
        plan: EditPlan,
        state: ProjectSequences,
        approval: "ApprovalGrant | ApprovalService | str | None",
        approval_service: "ApprovalService | None",
        *,
        diff_hash: str | None = None,
    ) -> "ApprovalGrant | None":
        """解析并消费授权，返回 ``ApprovalGrant`` 或 None（未授权）。

        绑定作用域：commit_plan + 当前 project_id + plan.target.timeline_id +
        plan.base_revision + plan 的 canonical hash + preview 的 diff hash。

        ``diff_hash`` 来自当前 commit 内部刚算出的 preview（总体不变量 §1.4：
        预览即提交——用户看到的那一份 diff 必须与将要提交的一致）。

        授权契约（红线 §0 第二条"一次性"）：

        - ``ApprovalGrant`` 对象**必须配合** ``approval_service``——``consume`` 既校验
          作用域又把 ``single_use`` grant 标记为已用。无 ``approval_service`` 时返回 None
          （grant 自身不可变，无法就地标记 used，放行会让同一 grant 被反复 commit）。
        - 字符串 grant_id 同样**必须配合** ``approval_service`` 才能 consume。
        """
        # 1) 直接给 Grant：必须配合 ApprovalService 才能使用——``consume`` 同时校验
        #    作用域并把 ``single_use`` grant 标记为已用（防重放，红线 §0 第二条）。
        #    不提供 ApprovalService 时拒绝：grant 自身不可变，无法就地标记 used，
        #    若放行则同一 grant 可被反复 commit（破坏"一次性"语义）。
        if isinstance(approval, ApprovalGrant):
            svc = approval_service
            if svc is not None:
                try:
                    return svc.consume(
                        approval.grant_id,
                        _expected_commit_scope(plan, diff_hash),
                    )
                except ApprovalError:
                    return None
            return None  # 无 service → 无法 consume/标 used → 视为未授权

        # 2) 给 ApprovalService：内部构造 expected scope 并 consume；无 grant_id
        #    时无法 consume，拒绝。
        if isinstance(approval, ApprovalService):
            return None  # ApprovalService 本身不是 grant；需要 grant_id
        if isinstance(approval_service, ApprovalService) and isinstance(approval, str) and approval:
            try:
                return approval_service.consume(
                    approval, _expected_commit_scope(plan, diff_hash),
                )
            except ApprovalError:
                return None
        return None

    # ---------------- audit ---------------- #

    def _record_audit(
        self, plan: EditPlan, result: CommitResult, client: str, op_count: int
    ) -> None:
        import time as _time


        diag_summary: dict[str, int] = {"errors": 0, "warnings": 0, "info": 0}
        # commit 阶段诊断较少；统计 errors tuple
        if result.errors:
            diag_summary["errors"] = len(result.errors)
        record = AuditRecord(
            plan_id=plan.plan_id,
            protocol=plan.protocol,
            base_revision=plan.base_revision,
            new_revision=result.new_revision if result.applied else None,
            undo_label=result.undo_label,
            operation_count=op_count,
            status="applied" if result.applied else "rejected",
            diagnostics_summary=diag_summary,
            timestamp=_time.time(),
            client=client,
        )
        try:
            self._audit_sink(record)  # type: ignore[misc]
        except Exception:
            # 审计失败不影响提交结果
            pass


def _expected_commit_scope(
    plan: EditPlan, diff_hash: str | None = None,
) -> ApprovalScope:
    """构造 commit 动作的 expected scope（绑定 project/timeline/revision/plan/diff hash）。

    plan_hash 用 plan 的 canonical JSON（``to_dict`` 稳定序列化）sha256；这样
    "用户看到的那一份 plan"与"将要提交的那一份"通过 hash 绑定（总体不变量 §1.4）。

    diff_hash 用 preview 的 TimelineDiff sha256；绑定"用户看到的那一份 diff"与
    "将要提交的那一份"。``diff_hash`` 为 None 时不约束（向后兼容旧路径；正常
    commit 入口一定带 preview.diff_hash）。
    """
    import json

    canonical = json.dumps(plan.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return ApprovalScope(
        action="commit_plan",
        project_id=plan.target.project_id,
        timeline_id=plan.target.timeline_id,
        base_revision=plan.base_revision,
        plan_hash=compute_plan_hash(canonical),
        diff_hash=diff_hash,
    )
