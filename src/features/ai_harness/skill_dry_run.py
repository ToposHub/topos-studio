"""P4.2 开发者 Skill Studio —— dry-run 引擎（plan §10.6）。

> 运行 dry-run：用 fake provider、固定 evidence fixture、固定 timeline snapshot
> 生成 ``RunRecord``，检查端口、DAG、预算、权限、缓存 key、plan diff。

设计要点：

- **固定 fixture**（``DryRunFixture``）：不含私人素材路径（plan §12.2 "建立不含私人
  素材的 fixture"）；``evidence_refs`` 是版本化的 :class:`SemanticSegment` 引用，
  ``timeline_snapshot`` 是 frozen revision。
- **fake provider / fake scheduler**（``_FakeDryRunExecutor``）：全程内存；不联网、
  不写盘、不下载模型、不调真实 ASR/LLM/TTS。``SkillDryRunner`` 默认装配它。
- **检查项**（``DryRunCheckResult`` × 7）：端口兼容 / DAG 无环 / 模型需求满足 /
  预算内 / 权限不越级 / cache-key 稳定 / plan-diff 非空。
- **终态 RunRecord**：成功 → SUCCEEDED + 携带 EditPlan（PREVIEW）；失败 →
  FAILED + error_code（MODEL_MISSING / EVIDENCE_INSUFFICIENT / BUDGET_EXCEEDED /
  PORTS_INCOMPATIBLE / DAG_CYCLE / PERMISSION_DENIED / PLAN_EMPTY）。

红线（plan §10.6 / §1.4）：

- dry-run **绝不**写真实时间线（EditPlan 永远 mode=PREVIEW；RunRecord 不含
  ``approval_granted`` / ``timeline_committed`` 事件）。
- dry-run **绝不**联网（无 ``network_request`` / ``cloud_upload`` 事件）。
- dry-run **绝不**执行任意代码（仅调用 :class:`SkillDraft.executor_kind` 受控
  枚举；无 ``process_spawn`` / ``shell_exec`` / ``code_exec`` / ``fs_write`` 事件）。
- local_dev 草稿即便声明 ``risk=write``，dry-run 也不真正写时间线。

与既有模块的关系：

- 复用 :class:`HarnessRuntime` / :class:`RunRecord` / :class:`RunPolicy` /
  :class:`Invocation`（P0.1/P0.2）作为统一运行记录。
- 复用 :func:`build_auto_caption` / :func:`build_rough_cut_plan`（P2.2）构造
  EditPlan（PREVIEW），不重新实现产物构造。
- 复用 :class:`SkillDraft`（P4.1）作为输入。
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.features.ai_harness.invocation_models import (
    ArtifactRef,
    Invocation,
    InvocationScope,
    InvocationSource,
    PermissionLevel,
    RunPolicy,
    RunRecord,
    RunStatus,
    sanitize_for_storage,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.skill_studio_drafts import ExecutorKind, SkillDraft
from src.features.edit.ai_editing.editorial_models import (
    FrameRange,
    SegmentKind,
    SemanticSegment,
)
from src.features.edit.ai_editing.models import EditPlan, PlanMode


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# TimelineSnapshot（固定 revision）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class TimelineSnapshot:
    """dry-run 用的固定时间线快照（plan §10.6 "固定 timeline snapshot"）。

    红线：不含绝对路径 / 私人素材；只记 revision / timeline_id / 轨道结构。
    """

    timeline_id: str
    project_id: str
    revision: int
    track_count: int = 0
    fps: float = 24.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timeline_id": self.timeline_id,
            "project_id": self.project_id,
            "revision": self.revision,
            "track_count": self.track_count,
            "fps": self.fps,
        }


# ---------------------------------------------------------------------- #
# DryRunFixture（固定证据 + snapshot）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceRef:
    """dry-run 用的版本化证据引用（不含私人素材）。

    ``payload`` 只放稳定摘要（不含绝对路径 / secret）；真实 evidence 由 host
    在真实运行时填充。dry-run 只看 kind/capability/confidence。
    """

    evidence_id: str
    capability: str           # asr / ocr / scene_cut / tts / llm / translate ...
    kind: str                 # SegmentKind value（transcript/shot/ocr/...）
    confidence: float = 1.0
    payload: str = ""         # 脱敏摘要（非原文/路径）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "capability": self.capability,
            "kind": self.kind,
            "confidence": self.confidence,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class DryRunFixture:
    """dry-run 的固定输入：证据引用 + 时间线快照 + 可用能力集。

    plan §10.6 "用 fake provider、固定 evidence fixture、固定 timeline snapshot"。
    所有字段 frozen；同一 fixture 多次运行可复现（回归对比）。
    """

    evidence_refs: Tuple[EvidenceRef, ...] = ()
    timeline_snapshot: Optional[TimelineSnapshot] = None
    #: host 当前可用的能力集（``asr``/``llm``/...）。空集合 → 模型缺失检查失败。
    available_capabilities: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        # available_capabilities 规范化为 frozenset（frozen dataclass 要求可哈希字段）
        object.__setattr__(self, "available_capabilities", frozenset(self.available_capabilities))


# ---------------------------------------------------------------------- #
# 检查项
# ---------------------------------------------------------------------- #


class DryRunCheckCode:
    """dry-run 检查项编码（plan §10.6 "检查端口/DAG/预算/权限/cache-key/plan-diff"）。"""

    PORTS_COMPATIBLE = "PORTS_COMPATIBLE"        # 节点端口连接兼容
    DAG_ACYCLIC = "DAG_ACYCLIC"                  # 任务图无环
    MODEL_REQUIREMENTS = "MODEL_REQUIREMENTS"    # 所需模型/能力可用
    BUDGET_WITHIN = "BUDGET_WITHIN"              # 预计成本在预算内
    PERMISSION_OK = "PERMISSION_OK"              # 权限不越级（local_dev 无 write）
    CACHE_KEY_STABLE = "CACHE_KEY_STABLE"        # cache-key 可稳定计算（含输入 hash）
    PLAN_DIFF_NONEMPTY = "PLAN_DIFF_NONEMPTY"    # 产 EditPlan 且 diff 非空（PREVIEW）

    ALL: Tuple[str, ...] = (
        PORTS_COMPATIBLE, DAG_ACYCLIC, MODEL_REQUIREMENTS,
        BUDGET_WITHIN, PERMISSION_OK, CACHE_KEY_STABLE, PLAN_DIFF_NONEMPTY,
    )


@dataclass(frozen=True)
class DryRunCheckResult:
    """单个检查项的结果（passed + 详细 message）。"""

    code: str
    passed: bool
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "passed": self.passed, "message": self.message}


# ---------------------------------------------------------------------- #
# DryRunOutcome（dry-run 终态）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class DryRunOutcome:
    """dry-run 一次运行的完整结果。"""

    success: bool
    run_record: RunRecord
    edit_plan: Optional[EditPlan]
    checks: Tuple[DryRunCheckResult, ...]
    cache_key: str

    @property
    def error_code(self) -> str:
        return self.run_record.error_code

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "run_record": self.run_record.to_public_dict(),
            "edit_plan": self.edit_plan.to_dict() if self.edit_plan else None,
            "checks": [c.to_dict() for c in self.checks],
            "cache_key": self.cache_key,
        }


# ---------------------------------------------------------------------- #
# 错误码
# ---------------------------------------------------------------------- #


class DryRunErrorCode:
    MODEL_MISSING = "MODEL_MISSING"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    PORTS_INCOMPATIBLE = "PORTS_INCOMPATIBLE"
    DAG_CYCLE = "DAG_CYCLE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PLAN_EMPTY = "PLAN_EMPTY"
    STALE_REVISION = "STALE_REVISION"


# ---------------------------------------------------------------------- #
# 固定 fixture 工厂（无私人素材）
# ---------------------------------------------------------------------- #


def _fixture_transcript_segments() -> Tuple[SemanticSegment, ...]:
    """生成固定 transcript segments（脱敏；不含真实素材/路径）。"""
    return (
        SemanticSegment(
            segment_id="fx-transcript-001",
            asset_id="fx-asset-001",
            source_range=FrameRange(start=0, duration=120),
            kind=SegmentKind.TRANSCRIPT,
            payload={"text": "fixture transcript line one", "speaker": "A"},
            confidence=0.95,
        ),
        SemanticSegment(
            segment_id="fx-transcript-002",
            asset_id="fx-asset-001",
            source_range=FrameRange(start=120, duration=140),
            kind=SegmentKind.TRANSCRIPT,
            payload={"text": "fixture transcript line two", "speaker": "A"},
            confidence=0.92,
        ),
    )


def make_auto_caption_fixture() -> DryRunFixture:
    """生成 auto_caption 的固定 fixture（脱敏；可用于回归对比）。

    红线：fixture 不含私人素材路径 / secret；payload 只放稳定摘要。
    """
    segs = _fixture_transcript_segments()
    evidence = tuple(
        EvidenceRef(
            evidence_id=s.segment_id,
            capability="asr",
            kind=s.kind.value,
            confidence=s.confidence,
            payload=str(s.payload.get("text", ""))[:32],  # 截断，仅摘要
        )
        for s in segs
    )
    return DryRunFixture(
        evidence_refs=evidence,
        timeline_snapshot=TimelineSnapshot(
            timeline_id="fx-timeline-001",
            project_id="fx-project-001",
            revision=42,
            track_count=2,
            fps=24.0,
        ),
        available_capabilities={"asr", "demucs"},
    )


def make_voiceover_polish_fixture() -> DryRunFixture:
    """生成 voiceover_polish 的固定 fixture（含 asr + llm 能力）。"""
    base = make_auto_caption_fixture()
    return DryRunFixture(
        evidence_refs=base.evidence_refs,
        timeline_snapshot=base.timeline_snapshot,
        available_capabilities={"asr", "llm"},
    )


# ---------------------------------------------------------------------- #
# SkillDryRunner —— 主引擎
# ---------------------------------------------------------------------- #


class SkillDryRunner:
    """开发者 Skill dry-run 引擎（plan §10.6）。

    用法::

        runner = SkillDryRunner()
        outcome = runner.run(draft, fixture)
        if outcome.success:
            assert outcome.edit_plan.mode == PlanMode.PREVIEW

    红线：

    - 全程内存（fake provider）；不联网 / 不写盘 / 不下载模型。
    - 产 EditPlan 永远 PREVIEW；不触发 approval/commit。
    - 只调用 ``draft.executor_kind`` 受控枚举对应构造器；不执行任意代码。
    """

    def __init__(
        self,
        *,
        budget_limit: Optional[float] = None,
        fake_executor: Optional[Callable[..., Any]] = None,
    ) -> None:
        #: 预算上限（None=不限；0=必超）。dry-run 用它做 BUDGET_WITHIN 检查。
        self._budget_limit = budget_limit
        #: fake executor 工厂（按 executor_kind 分派）；默认用内置 _FakeDryRunExecutor。
        self._fake_executor = fake_executor or _FakeDryRunExecutor()

    def run(
        self, draft: SkillDraft, fixture: DryRunFixture, *,
        now: float = 0.0, cancel_before_executor: bool = False,
        base_revision: Optional[int] = None,
    ) -> DryRunOutcome:
        """运行一次 dry-run，返回 :class:`DryRunOutcome`。

        Args:
            cancel_before_executor: 在调 fake executor 前请求取消（eval 的 cancel
                用例用）。取消后终态 CANCELLED + cancel_reason；已完成检查保留为
                partial artifact（plan §6.2："已完成 artifact 可保留并标注 partial"）。
            base_revision: 草稿/审批所基于的 timeline revision。提供时（宿主注入
                实际值，或取 workflow_template.base_revision）与
                ``fixture.timeline_snapshot.revision`` 不一致即 STALE（plan §6.4
                "跨 revision 审批不可复用"，AUD-22403）；缺省时保留 eval 用的
                ``revision < 0`` stale 哨兵语义。
        """
        ts = now or time.time()
        checks: List[DryRunCheckResult] = []

        # ---- 构造 Invocation + RunRecord（dry-run source=developer）----
        runtime = HarnessRuntime()
        snapshot = fixture.timeline_snapshot or TimelineSnapshot(
            timeline_id="dryrun-tl", project_id="dryrun-proj", revision=0,
        )
        policy = RunPolicy(
            permission=PermissionLevel.PROPOSE,  # dry-run 只提议，不写
            profile_id="dryrun-profile",
            timeline_write="approval_required",
            allow_model_download=False,
            allow_generation=False,
            notes="dry-run",
        )
        inv = Invocation(
            source=InvocationSource.DEVELOPER,
            skill_id=draft.skill_id,
            scope=InvocationScope(
                project_id=snapshot.project_id,
                timeline_id=snapshot.timeline_id,
            ),
            policy=policy,
            inputs={"fixture_id": _fixture_id(fixture), "dry_run": True},
            user_text="",  # 红线：dry-run 不带用户文本
            created_at=ts,
        )
        run_id = runtime.start(inv)
        runtime.set_status(run_id, RunStatus.VALIDATING)

        # ---- 检查 1: DAG 无环（workflow_template 有 edges 时检查）----
        dag_ok, dag_msg = _check_dag_acyclic(draft)
        checks.append(DryRunCheckResult(
            code=DryRunCheckCode.DAG_ACYCLIC, passed=dag_ok, message=dag_msg,
        ))

        # ---- 检查 2: 端口兼容（按 executor_kind 基本端口契约）----
        ports_ok, ports_msg = _check_ports_compatible(draft, fixture)
        checks.append(DryRunCheckResult(
            code=DryRunCheckCode.PORTS_COMPATIBLE, passed=ports_ok, message=ports_msg,
        ))

        # ---- 检查 3: revision-stale（项目级网关；先于模型/预算/证据检查）----
        # plan §6.4：跨 revision 审批不可复用。判定优先级（AUD-22403）：
        # 1) 宿主显式传入 base_revision（run 参数）；
        # 2) 草稿 workflow_template 自带 base_revision；
        # 3) 均缺失 → 保留 eval 用的 ``revision < 0`` stale 哨兵语义。
        declared_base = base_revision
        if declared_base is None:
            wf = draft.workflow_template or {}
            wf_base = wf.get("base_revision") if isinstance(wf, dict) else None
            if isinstance(wf_base, int) and not isinstance(wf_base, bool):
                declared_base = wf_base
        if declared_base is not None:
            revision_ok = snapshot.revision == declared_base
            revision_msg = (
                f"revision current ({snapshot.revision} == base {declared_base})"
                if revision_ok
                else (
                    f"stale revision: snapshot.revision={snapshot.revision} "
                    f"!= draft base_revision={declared_base}"
                )
            )
        else:
            revision_ok = snapshot.revision >= 0
            revision_msg = (
                f"revision current ({snapshot.revision})"
                if revision_ok
                else f"stale revision: snapshot.revision={snapshot.revision} < 0"
            )
        checks.append(DryRunCheckResult(
            code="REVISION_CURRENT", passed=revision_ok, message=revision_msg,
        ))

        # ---- 检查 4: 模型需求满足 ----
        model_ok, model_msg, missing_caps = _check_model_requirements(draft, fixture)
        checks.append(DryRunCheckResult(
            code=DryRunCheckCode.MODEL_REQUIREMENTS, passed=model_ok, message=model_msg,
        ))

        # ---- 检查 5: 预算内 ----
        budget_ok, budget_msg, estimated_cost = _check_budget(draft, fixture, self._budget_limit)
        checks.append(DryRunCheckResult(
            code=DryRunCheckCode.BUDGET_WITHIN, passed=budget_ok, message=budget_msg,
        ))

        # ---- 检查 6: 权限不越级（local_dev 不得真正写时间线）----
        perm_ok, perm_msg = _check_permission(draft)
        checks.append(DryRunCheckResult(
            code=DryRunCheckCode.PERMISSION_OK, passed=perm_ok, message=perm_msg,
        ))

        # ---- 失败短路（DAG/端口/revision/模型/预算/权限 任一失败 → 不进 executor）----
        blocking = [c for c in checks if not c.passed and c.code in (
            DryRunCheckCode.DAG_ACYCLIC, DryRunCheckCode.PORTS_COMPATIBLE,
            "REVISION_CURRENT",
            DryRunCheckCode.MODEL_REQUIREMENTS, DryRunCheckCode.BUDGET_WITHIN,
            DryRunCheckCode.PERMISSION_OK,
        )]
        if blocking:
            err_code = _blocking_error_code(blocking[0].code)
            runtime.record_diagnostic(run_id, sanitize_for_storage({
                "severity": "error", "code": err_code,
                "message": blocking[0].message,
                "failed_check": blocking[0].code,
            }))
            runtime.finish(
                run_id, success=False,
                error_code=err_code,
                error_message=blocking[0].message,
            )
            record = runtime.get_run(run_id)
            return DryRunOutcome(
                success=False, run_record=record, edit_plan=None,
                checks=tuple(checks),
                cache_key=_compute_cache_key(draft, fixture),
            )

        # ---- 证据充足性检查（auto_caption 需要 transcript 证据）----
        evidence_ok, evidence_msg = _check_evidence_sufficient(draft, fixture)
        if not evidence_ok:
            runtime.record_diagnostic(run_id, sanitize_for_storage({
                "severity": "error", "code": DryRunErrorCode.EVIDENCE_INSUFFICIENT,
                "message": evidence_msg,
            }))
            runtime.finish(
                run_id, success=False,
                error_code=DryRunErrorCode.EVIDENCE_INSUFFICIENT,
                error_message=evidence_msg,
            )
            checks.append(DryRunCheckResult(
                code="EVIDENCE_SUFFICIENT", passed=False, message=evidence_msg,
            ))
            record = runtime.get_run(run_id)
            return DryRunOutcome(
                success=False, run_record=record, edit_plan=None,
                checks=tuple(checks),
                cache_key=_compute_cache_key(draft, fixture),
            )
        checks.append(DryRunCheckResult(
            code="EVIDENCE_SUFFICIENT", passed=True, message=evidence_msg,
        ))

        # ---- 调 fake executor 构造 EditPlan（PREVIEW）----
        # 状态机合法路径：VALIDATING → QUEUED → RUNNING → 终态（plan §6.1）。
        runtime.set_status(run_id, RunStatus.QUEUED)
        runtime.set_status(run_id, RunStatus.RUNNING)

        # ---- 取消（eval cancel 用例：executor 前协作取消）----
        # 红线（plan §6.2）：cancel 是协作的 —— ``runtime.cancel`` 只标记
        # cancel_requested；executor 轮询后自行终态。dry-run 在此显式迁移到
        # CANCELLED（真实 provider 由 JobHandle.request_cancel 传播）。
        if cancel_before_executor:
            runtime.cancel(run_id, reason="cancelled before executor (eval cancel case)")
            runtime.set_status(run_id, RunStatus.CANCELLED)
            record = runtime.get_run(run_id)
            return DryRunOutcome(
                success=False, run_record=record, edit_plan=None,
                checks=tuple(checks), cache_key=_compute_cache_key(draft, fixture),
            )

        edit_plan = self._invoke_fake_executor(draft, fixture, snapshot)
        runtime.record_cost(run_id, estimated_cost)
        if edit_plan is not None:
            runtime.record_artifact(run_id, ArtifactRef(
                artifact_id=edit_plan.plan_id,
                artifact_type="edit_plan_ref",
                producer_node_id="dryrun-executor",
                summary=f"dry-run plan ({len(edit_plan.operations)} ops)",
            ))

        # ---- 检查 6: cache-key 稳定 ----
        cache_key = _compute_cache_key(draft, fixture)
        checks.append(DryRunCheckResult(
            code=DryRunCheckCode.CACHE_KEY_STABLE, passed=bool(cache_key),
            message=f"cache_key={cache_key[:12]}...",
        ))

        # ---- 检查 7: plan-diff 非空（PREVIEW）----
        plan_nonempty = edit_plan is not None and bool(edit_plan.operations)
        checks.append(DryRunCheckResult(
            code=DryRunCheckCode.PLAN_DIFF_NONEMPTY, passed=plan_nonempty,
            message=(
                f"{len(edit_plan.operations)} operations (mode=PREVIEW)"
                if plan_nonempty else "no plan / empty operations"
            ),
        ))

        if edit_plan is None or not edit_plan.operations:
            runtime.record_diagnostic(run_id, sanitize_for_storage({
                "severity": "error", "code": DryRunErrorCode.PLAN_EMPTY,
                "message": "dry-run produced no EditPlan",
            }))
            runtime.finish(
                run_id, success=False,
                error_code=DryRunErrorCode.PLAN_EMPTY,
                error_message="dry-run produced no EditPlan",
            )
        else:
            runtime.finish(run_id, success=True)
        record = runtime.get_run(run_id)
        return DryRunOutcome(
            success=record.error_code == "" and record.status in (
                RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS,
            ),
            run_record=record, edit_plan=edit_plan,
            checks=tuple(checks), cache_key=cache_key,
        )

    # ---------------- fake executor 分派 ---------------- #

    def _invoke_fake_executor(
        self, draft: SkillDraft, fixture: DryRunFixture, snapshot: TimelineSnapshot,
    ) -> Optional[EditPlan]:
        """按 executor_kind 调用受控 fake 构造器产 EditPlan（PREVIEW）。

        注入的 ``fake_executor``（构造参数）在此真正被调用（AUD-22402）：
        它收到 ``draft`` / ``fixture`` / ``snapshot`` / ``executor_kind``，返回
        ``EditPlan`` 时直接采用；返回 ``None``（如默认占位或统计型 fake）时回退
        到内置受控分派。注入 fake 的异常被包含并降级回内置分派，不允许把 run
        卡死在 RUNNING。
        """
        ek = draft.executor_kind
        # 红线：拒绝任意代码执行后端（不应发生，二次防御）
        if ek.is_arbitrary_code:
            return None
        injected = self._fake_executor
        if injected is not None:
            try:
                result = injected(
                    draft=draft, fixture=fixture, snapshot=snapshot,
                    executor_kind=ek.value,
                )
            except Exception:  # noqa: BLE001 - injected hook must not stall the run
                _LOG.exception(
                    "injected fake_executor raised; falling back to built-in dispatch"
                )
                result = None
            if isinstance(result, EditPlan):
                return result
        if ek == ExecutorKind.EDITORIAL_BUILDER:
            return _fake_editorial_plan(draft, fixture, snapshot)
        if ek == ExecutorKind.WORKFLOW_RUNNER:
            return _fake_workflow_plan(draft, fixture, snapshot)
        if ek == ExecutorKind.TASKGRAPH_ROLES:
            return _fake_taskgraph_plan(draft, fixture, snapshot)
        return None


# ---------------------------------------------------------------------- #
# Fake executors（受控；纯内存；产 PREVIEW EditPlan）
# ---------------------------------------------------------------------- #


def _fake_editorial_plan(
    draft: SkillDraft, fixture: DryRunFixture, snapshot: TimelineSnapshot,
) -> Optional[EditPlan]:
    """auto_caption / subtitle_localization 的 fake 构造器。

    复用 :func:`build_auto_caption`（P2.2），用 fixture transcript segments 构造
    真实结构的 EditPlan（PREVIEW）。其它 editorial skill 产最小占位 plan。
    """
    if "auto_caption" in draft.origin.template_id:
        from src.features.ai_harness.editorial_skills import build_auto_caption
        segs = _fixture_transcript_segments()
        outcome = build_auto_caption(
            project_id=snapshot.project_id,
            timeline_id=snapshot.timeline_id,
            base_revision=snapshot.revision,
            transcript_segments=segs,
            fps=snapshot.fps,
            brief_id="dryrun-brief",
        )
        return outcome.plan
    # 其它 editorial：产 1 个 placeholder operation 的 PREVIEW plan
    from src.features.edit.ai_editing.models import (
        EditOperation, OperationKind, PlanTarget,
    )
    return EditPlan.create(
        target=PlanTarget(
            project_id=snapshot.project_id, timeline_id=snapshot.timeline_id,
        ),
        base_revision=snapshot.revision,
        operations=(
            EditOperation(
                op_id="dryrun_placeholder",
                kind=OperationKind.ENSURE_SUBTITLE_TRACK,
                params={"track_name": "dryrun"},
                intent={"reason": "dry-run placeholder (editorial)"},
            ),
        ),
        mode=PlanMode.PREVIEW,
        title=f"dry-run placeholder ({draft.skill_id})",
        rationale="fake executor dry-run",
    )


def _fake_workflow_plan(
    draft: SkillDraft, fixture: DryRunFixture, snapshot: TimelineSnapshot,
) -> Optional[EditPlan]:
    """workflow_runner 的 fake 构造器：产最小 PREVIEW EditPlan。"""
    from src.features.edit.ai_editing.models import (
        EditOperation, OperationKind, PlanTarget,
    )
    return EditPlan.create(
        target=PlanTarget(
            project_id=snapshot.project_id, timeline_id=snapshot.timeline_id,
        ),
        base_revision=snapshot.revision,
        operations=(
            EditOperation(
                op_id="dryrun_workflow_op",
                kind=OperationKind.ENSURE_SUBTITLE_TRACK,
                params={"track_name": "dryrun-wf"},
                intent={"reason": "dry-run workflow placeholder"},
            ),
        ),
        mode=PlanMode.PREVIEW,
        title=f"dry-run workflow ({draft.skill_id})",
        rationale="fake executor dry-run (workflow_runner)",
    )


def _fake_taskgraph_plan(
    draft: SkillDraft, fixture: DryRunFixture, snapshot: TimelineSnapshot,
) -> Optional[EditPlan]:
    """taskgraph_roles 的 fake 构造器：产最小 PREVIEW EditPlan。"""
    from src.features.edit.ai_editing.models import (
        EditOperation, OperationKind, PlanTarget,
    )
    return EditPlan.create(
        target=PlanTarget(
            project_id=snapshot.project_id, timeline_id=snapshot.timeline_id,
        ),
        base_revision=snapshot.revision,
        operations=(
            EditOperation(
                op_id="dryrun_taskgraph_op",
                kind=OperationKind.ENSURE_SUBTITLE_TRACK,
                params={"track_name": "dryrun-tg"},
                intent={"reason": "dry-run taskgraph placeholder"},
            ),
        ),
        mode=PlanMode.PREVIEW,
        title=f"dry-run taskgraph ({draft.skill_id})",
        rationale="fake executor dry-run (taskgraph_roles)",
    )


class _FakeDryRunExecutor:
    """默认 fake executor 占位（返回 None → 内置受控分派接管）。

    保留为可注入点：测试可传入自定义 fake（如统计调用次数、返回定制
    EditPlan）。注入 fake 由 :meth:`SkillDryRunner._invoke_fake_executor`
    在每次 dry-run 中真实调用（AUD-22402）；返回 ``EditPlan`` 时优先采用，
    返回 ``None`` 时回退内置分派，仍受 executor_kind 受控枚举约束（不执行
    任意代码）。
    """

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return None  # 默认无操作；真实分派在 SkillDryRunner


# ---------------------------------------------------------------------- #
# 检查项实现
# ---------------------------------------------------------------------- #


def _check_dag_acyclic(draft: SkillDraft) -> Tuple[bool, str]:
    """检查 workflow_template 的 edges 是否无环（Kahn 拓扑）。"""
    wf = draft.workflow_template
    if not wf:
        return True, "no workflow_template (editorial/taskgraph); DAG check skipped"
    nodes = [str(n.get("node_id", "")) for n in (wf.get("nodes", []) or []) if isinstance(n, dict)]
    edges = wf.get("edges", []) or []
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    indeg: Dict[str, int] = {n: 0 for n in nodes}
    for e in edges:
        if not isinstance(e, dict):
            continue
        f = str((e.get("from") or {}).get("node_id", ""))
        t = str((e.get("to") or {}).get("node_id", ""))
        if f in adj and t in indeg:
            adj[f].append(t)
            indeg[t] += 1
    # Kahn
    queue = [n for n in nodes if indeg[n] == 0]
    visited = 0
    while queue:
        n = queue.pop(0)
        visited += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if visited == len(nodes):
        return True, f"DAG acyclic ({len(nodes)} nodes, {len(edges)} edges)"
    return False, f"DAG cycle detected ({visited}/{len(nodes)} nodes reachable)"


def _check_ports_compatible(draft: SkillDraft, fixture: DryRunFixture) -> Tuple[bool, str]:
    """检查基本端口契约（输入 evidence kind 与 skill 所需匹配）。"""
    wf = draft.workflow_template
    if wf:
        nodes = wf.get("nodes", []) or []
        has_input = any(
            str(n.get("spec_id", "")).startswith(("topos.media.", "topos.timeline."))
            for n in nodes if isinstance(n, dict)
        )
        if nodes and not has_input:
            return False, "workflow has nodes but no input node (topos.media.*/topos.timeline.*)"
        return True, f"ports ok ({len(nodes)} nodes)"
    # editorial/taskgraph：检查 evidence_refs 非空即可（细化在 evidence check）
    return True, "ports ok (no workflow_template)"


def _check_model_requirements(
    draft: SkillDraft, fixture: DryRunFixture,
) -> Tuple[bool, str, List[str]]:
    """检查 draft.model_requirements 中的 required capability 是否在 fixture.available_capabilities。"""
    available = set(fixture.available_capabilities or set())
    missing: List[str] = []
    for mr in draft.model_requirements:
        if mr.required and mr.capability not in available:
            missing.append(mr.capability)
    if missing:
        return False, f"missing required capabilities: {missing}", missing
    return True, f"all required capabilities available ({sorted(available)})", []


def _check_budget(
    draft: SkillDraft, fixture: DryRunFixture, limit: Optional[float],
) -> Tuple[bool, str, float]:
    """检查预计成本是否在预算内（dry-run 用 fixture 估算）。"""
    # 估算：每个 evidence ref ~ 0.01；每个 model requirement ~ 0.05
    estimated = len(fixture.evidence_refs) * 0.01 + len(draft.model_requirements) * 0.05
    if limit is None:
        return True, f"no budget limit (estimated={estimated:.3f})", estimated
    if estimated > limit:
        return False, f"budget exceeded: {estimated:.3f} > {limit:.3f}", estimated
    return True, f"budget ok ({estimated:.3f} <= {limit:.3f})", estimated


def _check_permission(draft: SkillDraft) -> Tuple[bool, str]:
    """红线：local_dev 草稿不得真正写时间线（dry-run 始终 PREVIEW）。"""
    # dry-run 永远不写时间线（EditPlan.mode=PREVIEW），所以权限检查总是通过；
    # 这里只确认 executor_kind 不是任意代码。
    if draft.executor_kind.is_arbitrary_code:
        return False, f"forbidden executor_kind: {draft.executor_kind.value}"
    return True, "permission ok (dry-run PREVIEW only; no arbitrary code)"


def _check_evidence_sufficient(
    draft: SkillDraft, fixture: DryRunFixture,
) -> Tuple[bool, str]:
    """检查 fixture 的证据是否满足 skill 所需的 evidence kind。"""
    required_kinds = set(draft.manifest.required_evidence_kinds or ())
    if not required_kinds:
        return True, "no required evidence kinds"
    required_kind_values = {k.value for k in required_kinds}
    available_kinds = {e.kind for e in fixture.evidence_refs}
    missing = required_kind_values - available_kinds
    if missing:
        return False, f"insufficient evidence: missing kinds {sorted(missing)}"
    return True, f"evidence sufficient ({sorted(available_kinds)})"


def _blocking_error_code(check_code: str) -> str:
    return {
        DryRunCheckCode.DAG_ACYCLIC: DryRunErrorCode.DAG_CYCLE,
        DryRunCheckCode.PORTS_COMPATIBLE: DryRunErrorCode.PORTS_INCOMPATIBLE,
        "REVISION_CURRENT": DryRunErrorCode.STALE_REVISION,
        DryRunCheckCode.MODEL_REQUIREMENTS: DryRunErrorCode.MODEL_MISSING,
        DryRunCheckCode.BUDGET_WITHIN: DryRunErrorCode.BUDGET_EXCEEDED,
        DryRunCheckCode.PERMISSION_OK: DryRunErrorCode.PERMISSION_DENIED,
    }.get(check_code, "DRY_RUN_CHECK_FAILED")


# ---------------------------------------------------------------------- #
# Cache key 计算（稳定 hash）
# ---------------------------------------------------------------------- #


def _compute_cache_key(draft: SkillDraft, fixture: DryRunFixture) -> str:
    """计算 dry-run 的稳定 cache-key（plan §6.3 cache 规则）。

    key = sha256(draft_id + manifest content_hash + executor_kind + fixture_id +
                 required_evidence_kinds + available_capabilities)
    """
    payload = {
        "draft_id": draft.draft_id,
        "manifest_hash": draft.manifest.content_hash,
        "executor_kind": draft.executor_kind.value,
        "fixture_id": _fixture_id(fixture),
        "required_evidence_kinds": sorted(
            k.value for k in (draft.manifest.required_evidence_kinds or ())
        ),
        "available_capabilities": sorted(fixture.available_capabilities or set()),
    }
    import json
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fixture_id(fixture: DryRunFixture) -> str:
    """fixture 的稳定 id（基于内容 hash）。"""
    payload = (
        tuple(e.to_dict() for e in fixture.evidence_refs),
        fixture.timeline_snapshot.to_dict() if fixture.timeline_snapshot else None,
        tuple(sorted(fixture.available_capabilities or set())),
    )
    import json
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "TimelineSnapshot",
    "EvidenceRef",
    "DryRunFixture",
    "DryRunCheckCode",
    "DryRunCheckResult",
    "DryRunOutcome",
    "DryRunErrorCode",
    "SkillDryRunner",
    "make_auto_caption_fixture",
    "make_voiceover_polish_fixture",
]
