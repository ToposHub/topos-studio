"""Legacy adapters —— 把现有 AssistantService.run_workflow / run_skill 包装成
同一 RunRecord（plan §14 step 3 / §10.2 P0.2）。

目标：不一次性大重构。保留旧 public API（``AssistantService.run_workflow`` /
``run_skill``），新增一层 adapter 让它们的产出归入 :class:`HarnessRuntime` 的
统一 ``RunRecord``。这样：

- 同一"自动字幕"从 chat 和 workflow 发起，都能拿到**同结构**的 RunRecord。
- 不允许两边各自创建 JobRegistry（adapter 通过 :class:`HarnessRuntime` 单点）。
- 取消贯通：adapter 把 runtime 的 cancel token 透传给 ``run_skill`` 的
  ``cancel_requested``（plan §10.2 P0.6）。``run_workflow`` 当前无 cancel 钩子，
  adapter 记录此限制（不伪造取消）。

映射规则（plan §0.4 / 现有数据合同）：

- ``Invocation`` -> ``WorkflowInput`` / ``SkillInput``：字段对齐。
- ``WorkflowResult`` / ``SkillResult`` -> ``RunRecord`` 终态 + diagnostics +
  artifact。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.features.ai_harness.invocation_models import ArtifactRef, Invocation, InvocationSource, RunStatus
from src.features.ai_harness.runtime import HarnessRuntime


_LOG = logging.getLogger(__name__)


@dataclass
class _EditorialRunContext:
    """单次 editorial run 的局部证据上下文（AUD-22008）。

    以前转录追踪用的 ``_editorial_analyses`` 挂在 adapter 实例上：adapter 是
    长生命周期对象，跨 run 残留会污染下一次 run 的 TRANSCRIPT_ROUTE_TRACE，
    且并发 run 之间存在数据竞争。本上下文随 run 创建、局部传递，run 结束
    即不可达——adapter 自身不再持有任何跨 run 可变状态。
    """

    analyses: dict[str, Any] = field(default_factory=dict)


def invocation_to_workflow_input(inv: Invocation) -> Any:
    """``Invocation`` -> 现有 ``WorkflowInput``。

    ``WorkflowInput`` 需要 ``workflow_id``；从 ``inv.skill_id`` 解析（约定 skill_id
    形如 ``topos.subtitle_draft`` 或 workflow_id 直接是 ``subtitle_draft``）。
    """
    from src.features.edit.ai_editing.workflow import (
        BUILTIN_SPECS, WorkflowId, WorkflowInput,
    )

    skill_id = inv.skill_id
    # 把 "topos.subtitle_draft" / "subtitle_draft" 都对齐到 WorkflowId。
    raw = skill_id.split(".")[-1] if skill_id else ""
    try:
        wf_id = WorkflowId(raw)
    except ValueError:
        # 找不到对应 workflow：返回 None 由调用方拒绝（不让任意 skill_id 进 workflow）。
        raise ValueError(
            f"skill_id {skill_id!r} does not map to a known WorkflowId; "
            f"valid: {[w.value for w in WorkflowId]}"
        )
    if wf_id not in BUILTIN_SPECS:
        raise ValueError(f"workflow {wf_id!r} not in BUILTIN_SPECS")
    return WorkflowInput(
        workflow_id=wf_id,
        asset_ids=inv.scope.selected_asset_ids,
        timeline_id=inv.scope.timeline_id,
        project_id=inv.scope.project_id,
        base_revision=int(inv.inputs.get("base_revision", 0)),
        brief_id=str(inv.inputs.get("brief_id", "default")),
        analysis_snapshot_hash=str(inv.inputs.get("analysis_snapshot_hash", "")),
        target_duration=int(inv.inputs.get("target_duration", 720)),
        node_ids=tuple(inv.inputs.get("workflow_node_ids", ()) or ()),
    )


def invocation_to_skill_input(inv: Invocation) -> Any:
    """``Invocation`` -> 现有 ``SkillInput``。"""
    from src.features.edit.ai_editing.skill_runtime import SkillInput

    return SkillInput(
        skill_id=inv.skill_id,
        asset_ids=inv.scope.selected_asset_ids,
        timeline_id=inv.scope.timeline_id,
        project_id=inv.scope.project_id,
        base_revision=int(inv.inputs.get("base_revision", 0)),
        brief_id=str(inv.inputs.get("brief_id", "default")),
        brief_summary=str(inv.inputs.get("brief_summary", "")),
        evidence_refs=tuple(inv.inputs.get("evidence_refs", ())),
        target_duration=int(inv.inputs.get("target_duration", 720)),
    )


class LegacyHarnessAdapter:
    """把 :class:`AssistantService` 的 run_workflow / run_skill 包装进
    :class:`HarnessRuntime`。

    用法（宿主装配侧 / 测试）::

        adapter = LegacyHarnessAdapter(service, runtime)
        run_id = adapter.invoke(invocation)

    ``invoke`` 根据 ``invocation.skill_id`` 路由到 workflow 或 skill：

    - skill_id 能映射到 ``WorkflowId`` 且 source 不是 developer → 走
      ``run_workflow``（向后兼容 M7）。
    - 否则走 ``run_skill``（含官方与多角色 Skill）。

    取消：adapter 把 ``runtime.is_cancelled`` 作为 ``cancel_requested`` 透传给
    ``run_skill``；``run_workflow`` 无钩子，adapter 在调用前检查一次 cancel（已
    cancel 则不调，记 cancelled）。
    """

    def __init__(self, service: Any, runtime: HarnessRuntime) -> None:
        self._service = service
        self._runtime = runtime
        # P2 editorial 构造器需要的 evidence provider（host 注入）。
        # 签名：(asset_ids, kind) -> Sequence[SemanticSegment]。
        # 为 None 时 editorial 路径降级（无证据）。
        self._editorial_provider: Any = None
        # 字幕本地化的 TTS 结果必须已经 materialize 并导入 Media Pool。该
        # provider 只返回可安全放置的 SkillArtifact，绝不在 adapter/UI 线程内
        # 发起合成、下载或直接写时间线。
        self._localization_media_provider: Any = None
        # 合成渲染后导入（plan §9.5）的渲染 provider（host 注入）。
        # 签名：(invocation, cancel_requested) -> SkillArtifact | None。
        # provider 在宿主侧同步执行 headless 渲染（editorial 路径本身在
        # worker 线程），产物为生成素材箱候选；只返回引用，不写时间线。
        self._composite_render_provider: Any = None
        # host-owned plan handoff: workflow actions are previewed/applied by a
        # separate service, never by this adapter.
        self._plan_observer: Any = None
        # P9：workflow 路径的节点事件回调（host 注入；签名 (kind, node_id) -> None）。
        # 为 None 时 run_workflow 按旧约调用（不发 node 事件），兼容既有 chat 路径。
        self._workflow_node_emitter: Any = None

    def set_editorial_provider(self, provider: Any) -> None:
        """注入 P2 editorial 构造器的 evidence provider（plan §10.4 P2.3）。

        provider 签名：``(asset_ids: tuple[str,...], kind: str) -> Sequence``，
        返回对应类型的 SemanticSegment（transcript / 字幕行等）。
        """
        self._editorial_provider = provider

    def set_localization_media_provider(self, provider: Any) -> None:
        """Inject a resolver for an already-imported localization TTS asset.

        The callable receives ``(invocation, transcript_segments,
        target_language)`` and returns a ``SkillArtifact`` with a
        ``media_asset_id`` (or ``None`` when no materialized candidate is
        available).  Keeping this boundary explicit prevents a remote URL or a
        GenerationBin id from ever becoming an ``ADD_CLIP`` proposal.
        """
        self._localization_media_provider = provider

    def set_composite_render_provider(self, provider: Any) -> None:
        """Inject the composite render executor for plan §9.5 runs.

        The callable receives ``(invocation, cancel_requested)`` and returns a
        ``SkillArtifact`` describing a rendered candidate already registered in
        the Generation Bin (or ``None`` when there is nothing to render, e.g.
        no composite graph / cancelled).  The provider runs the headless
        composite renderer synchronously — the editorial path executes on a
        worker thread — and must never write the timeline or import to the
        Media Pool itself; those are user actions behind the approval boundary.
        """
        self._composite_render_provider = provider

    def set_workflow_node_emitter(self, emitter: Any) -> None:
        """注入 workflow 路径的节点事件回调（plan §10.7 P9）。

        emitter 签名：``(kind: str, node_id: str) -> None``（与
        :class:`FakeWorkflowService.run_workflow` 的 ``emit_node_event`` 一致）。
        为 None 时按旧约调用 ``run_workflow(wf_input)``（不发 node 事件，兼容
        既有 chat/skill 路径零影响）。
        """
        self._workflow_node_emitter = emitter

    def set_plan_observer(self, observer: Any) -> None:
        """Observe produced EditPlans for the host approval/action boundary.

        ``observer`` receives ``(run_id, plan)``.  It must not mutate the
        timeline; the observer is only a registry handoff for later preview and
        approval.
        """
        self._plan_observer = observer

    # ------------------------------------------------------------------ #

    def invoke(self, inv: Invocation) -> str:
        """执行 invocation，返回 run_id。状态全部写进 runtime 的 RunRecord。"""
        run_id = self._runtime.start(inv)
        # 路由：先尝试 workflow（向后兼容），失败回退 skill。
        if self._should_route_workflow(inv):
            self._run_workflow_path(run_id, inv)
        else:
            self._run_skill_path(run_id, inv)
        return run_id

    def invoke_compiled(self, compiled: Any, inv: Invocation) -> str:
        """执行已编译的 :class:`CompiledSkill`（plan §10.3 P1.3 统一路径）。

        chat / 快捷卡 / workflow 模板三入口都先经 :class:`SkillCompiler.compile`
        产出 CompiledSkill，再调本方法 —— 保证它们走同一 RunRecord / plan repo /
        approval / undo（plan §10.3 P1.3 验收）。

        target=workflow 用 compiled.workflow_input；target=taskgraph 用
        compiled.skill_input；未 runnable 直接终态 FAILED。
        """
        run_id = self._runtime.start(inv)
        if not getattr(compiled, "runnable", False):
            # 编译失败：DRAFT → VALIDATING → FAILED（合法迁移路径）
            self._runtime.set_status(run_id, "validating")
            for diag in getattr(compiled, "diagnostics", ()) or ():
                self._runtime.record_diagnostic(run_id, diag)
            self._runtime.finish(
                run_id, success=False,
                error_code="COMPILE_FAILED",
                error_message="skill 编译失败（见 diagnostics）",
            )
            return run_id
        target = getattr(compiled, "target", "")
        if target == "workflow":
            self._run_workflow_path_with_input(run_id, inv, compiled.workflow_input)
        elif target == "taskgraph":
            self._run_skill_path_with_input(run_id, inv, compiled.skill_input)
        elif target == "editorial":
            self._run_editorial_path(run_id, inv)
        else:
            self._runtime.finish(
                run_id, success=False,
                error_code="UNSUPPORTED_TARGET",
                error_message=f"无法路由的执行目标：{target}",
            )
        return run_id

    # ------------------------------------------------------------------ #
    # workflow 路径
    # ------------------------------------------------------------------ #

    def _should_route_workflow(self, inv: Invocation) -> bool:
        """skill_id 能映射到 WorkflowId 且 source 不是 developer 时走 workflow。

        与类文档（invoke 路由契约）一致：developer 入口即使 skill_id 恰好映射
        到 WorkflowId，也必须走 ``run_skill``（多角色 Skill 路径），不被旧
        M7 workflow 兼容路径截走（AUD-22006）。
        """
        if not inv.skill_id:
            return False
        if inv.source == InvocationSource.DEVELOPER:
            return False
        try:
            invocation_to_workflow_input(inv)
            return True
        except ValueError:
            return False

    def _run_workflow_path(self, run_id: str, inv: Invocation) -> None:
        try:
            wf_input = invocation_to_workflow_input(inv)
        except ValueError as exc:
            # AUD-22005：run 刚 start、状态是 DRAFT；DRAFT→FAILED 是非法迁移
            # （finish 会直接 return False，run 永远停在 draft）。先经
            # VALIDATING 中转再 finish，与 invoke_compiled 的正确做法一致。
            self._runtime.set_status(run_id, RunStatus.VALIDATING)
            self._runtime.record_diagnostic(
                run_id, {"code": "WORKFLOW_MAPPING_FAILED", "message": str(exc)}
            )
            self._runtime.finish(
                run_id, success=False,
                error_code="WORKFLOW_MAPPING_FAILED", error_message=str(exc),
            )
            return
        self._run_workflow_path_with_input(run_id, inv, wf_input)

    def _run_workflow_path_with_input(
        self, run_id: str, inv: Invocation, wf_input: Any,
    ) -> None:
        # run_workflow 当前无 cancel 钩子；若已 cancel，直接终态。
        if self._runtime.is_cancelled(run_id):
            self._runtime.finish(run_id, success=False)
            return
        self._runtime.mark_running(run_id)
        # P9：若 host 注入了 node 事件 emitter（AI Workflow 路径），把 emitter 与
        # cancel token 作为 kwargs 传给 run_workflow（仅 FakeWorkflowService 接收）。
        # 既有 AssistantService.run_workflow 只接受位置参 wf_input → 用 inspect 探测
        # 是否接受这些 kwarg，不接受则按旧约调用（零影响）。
        emitter = self._workflow_node_emitter
        extra_kwargs: dict[str, Any] = {}
        if emitter is not None:
            extra_kwargs["emit_node_event"] = emitter
            extra_kwargs["cancel_requested"] = (
                lambda rid=run_id: self._runtime.is_cancelled(rid)
            )
        try:
            if extra_kwargs and self._service_accepts_workflow_kwargs():
                result = self._service.run_workflow(wf_input, **extra_kwargs)
            else:
                result = self._service.run_workflow(wf_input)
        except Exception as exc:  # noqa: BLE001 — adapter 必须把异常归入 RunRecord
            self._runtime.record_diagnostic(
                run_id, {"code": "WORKFLOW_EXCEPTION", "message": str(exc)}
            )
            self._runtime.finish(
                run_id, success=False,
                error_code="WORKFLOW_EXCEPTION", error_message=str(exc),
            )
            _LOG.exception("workflow path failed for run %s", run_id)
            return
        # AUD-22007：结果归档（repo.get_plan 等 host 注入的鸭子类型对象）在
        # provider try/except 之外——归档自身抛错也必须有终态，不能让 run
        # 卡死在 RUNNING。
        try:
            self._record_workflow_result(run_id, result)
        except Exception as exc:  # noqa: BLE001 — 归档失败兜底收尾
            self._fail_run_recording(run_id, exc)

    def _service_accepts_workflow_kwargs(self) -> bool:
        """探测 ``run_workflow`` 是否接受 emit_node_event / cancel_requested kwargs。

        既有 ``AssistantService.run_workflow(wf_input)`` 不接受 → False（按旧约调用）。
        ``FakeWorkflowService.run_workflow`` 接受 → True。
        """
        import inspect
        try:
            sig = inspect.signature(self._service.run_workflow)
        except (TypeError, ValueError):
            return False
        params = sig.parameters
        return (
            "emit_node_event" in params and "cancel_requested" in params
        )

    def _record_workflow_result(self, run_id: str, result: Any) -> None:
        # diagnostics（现有 WorkflowResult.diagnostics 是 tuple[dict]）
        for diag in getattr(result, "diagnostics", ()) or ():
            self._runtime.record_diagnostic(run_id, diag)
        # degradation -> with_warnings
        degraded = bool(getattr(result, "degraded", False))
        # artifact_id -> ArtifactRef
        art_id = getattr(result, "artifact_id", None)
        if art_id:
            self._runtime.record_artifact(run_id, ArtifactRef(
                artifact_id=str(art_id),
                artifact_type="script",  # workflow 字幕初稿产 ScriptArtifact
                producer_node_id="workflow",
                summary=f"workflow artifact {art_id}",
            ))
        variants = getattr(result, "variants", ()) or ()
        if variants:
            repo = getattr(self._service, "plan_repository", None)
            for variant in variants:
                plan_id = str(getattr(variant, "edit_plan_ref", "") or "")
                if not plan_id or repo is None:
                    continue
                plan = repo.get_plan(plan_id)
                if plan is not None:
                    self._observe_plan(run_id, plan)
        missing = getattr(result, "missing_evidence", ()) or ()
        if missing:
            self._runtime.record_diagnostic(run_id, {
                "code": "MISSING_EVIDENCE",
                "missing": list(missing),
            })
        # missing_evidence / degradation 视为 SUCCEEDED_WITH_WARNINGS（非失败），
        # 与现有"降级而非报错"语义一致。
        success = True
        err_code = ""
        err_msg = ""
        # 只有没有任何产出（无 variant / 无 artifact）且非 degraded 才算失败。
        has_variants = bool(getattr(result, "variants", ()))
        has_artifact = art_id is not None
        if not has_variants and not has_artifact and not degraded and not missing:
            # 既无产出也未降级 → 真正的失败（如校验错误）
            success = False
            err_code = "WORKFLOW_NO_OUTPUT"
            err_msg = getattr(result, "degradation", "") or "workflow produced no output"
        self._runtime.finish(
            run_id, success=success, with_warnings=degraded or bool(missing),
            error_code=err_code, error_message=err_msg,
        )

    # ------------------------------------------------------------------ #
    # skill 路径
    # ------------------------------------------------------------------ #

    def _run_skill_path(self, run_id: str, inv: Invocation) -> None:
        try:
            sk_input = invocation_to_skill_input(inv)
        except Exception as exc:  # noqa: BLE001
            # AUD-22005：同 workflow 路径——DRAFT 先经 VALIDATING 中转再终态化。
            self._runtime.set_status(run_id, RunStatus.VALIDATING)
            self._runtime.record_diagnostic(
                run_id, {"code": "SKILL_INPUT_FAILED", "message": str(exc)}
            )
            self._runtime.finish(
                run_id, success=False,
                error_code="SKILL_INPUT_FAILED", error_message=str(exc),
            )
            return
        self._run_skill_path_with_input(run_id, inv, sk_input)

    def _run_skill_path_with_input(
        self, run_id: str, inv: Invocation, sk_input: Any,
    ) -> None:
        self._runtime.mark_running(run_id)
        # 取消 token 透传（plan §10.2 P0.6）。
        def _cancel_requested() -> bool:
            return self._runtime.is_cancelled(run_id)
        try:
            result = self._service.run_skill(
                sk_input, cancel_requested=_cancel_requested,
            )
        except Exception as exc:  # noqa: BLE001
            self._runtime.record_diagnostic(
                run_id, {"code": "SKILL_EXCEPTION", "message": str(exc)}
            )
            self._runtime.finish(
                run_id, success=False,
                error_code="SKILL_EXCEPTION", error_message=str(exc),
            )
            _LOG.exception("skill path failed for run %s", run_id)
            return
        # AUD-22007：skill 结果归档同样需要异常边界（诊断/artifact 写入链）。
        try:
            self._record_skill_result(run_id, result)
        except Exception as exc:  # noqa: BLE001 — 归档失败兜底收尾
            self._fail_run_recording(run_id, exc)

    # ------------------------------------------------------------------ #
    # editorial 路径（P2：topos.auto_caption / topos.subtitle_localization）
    # ------------------------------------------------------------------ #

    def _run_editorial_path(self, run_id: str, inv: Invocation) -> None:
        """P2 editorial builder 路径（plan §10.4 P2.2/P2.3）。

        调 ``editorial_skills.build_*`` 构造 EditPlan 提议 + typed artifact；
        不走 M7 WorkflowRunner（这些 skill 无 WorkflowId）。evidence 由注入的
        ``_editorial_provider`` 提供。转录追踪所需的 analyses 放在 run 级局部
        上下文（AUD-22008），不经实例属性。
        """
        self._runtime.mark_running(run_id)
        run_ctx = _EditorialRunContext()
        try:
            # composite 渲染 provider 需要协作取消；其余 builder 忽略该参数。
            cancel_requested = (lambda: self._runtime.is_cancelled(run_id))
            outcome = self._build_editorial_outcome(inv, cancel_requested, run_ctx)
        except Exception as exc:  # noqa: BLE001
            self._runtime.record_diagnostic(
                run_id, {"code": "EDITORIAL_EXCEPTION", "message": str(exc)}
            )
            self._runtime.finish(
                run_id, success=False,
                error_code="EDITORIAL_EXCEPTION", error_message=str(exc),
            )
            _LOG.exception("editorial path failed for run %s", run_id)
            return
        # AUD-22007：结果归档（repo.store / observer 交接）需要异常边界。
        try:
            self._record_editorial_outcome(run_id, inv, outcome, run_ctx)
        except Exception as exc:  # noqa: BLE001 — 归档失败兜底收尾
            self._fail_run_recording(run_id, exc)

    def _fail_run_recording(self, run_id: str, exc: Exception) -> None:
        """结果归档抛错时的兜底收尾（AUD-22007）：run 必须收敛到终态。"""
        self._runtime.record_diagnostic(run_id, {
            "severity": "error",
            "code": "RECORD_FAILED",
            "message": f"{type(exc).__name__}: {exc}",
        })
        self._runtime.finish(
            run_id, success=False,
            error_code="RECORD_FAILED",
            error_message=f"结果归档失败：{type(exc).__name__}: {exc}",
        )
        _LOG.exception("run %s result recording failed", run_id)

    def _build_editorial_outcome(
        self, inv: Invocation, cancel_requested: Any = None,
        run_ctx: "_EditorialRunContext | None" = None,
    ) -> Any:
        """按 skill_id 分发到对应的 editorial_skills 构造器。"""
        from src.features.ai_harness import editorial_skills as es

        ctx = run_ctx if run_ctx is not None else _EditorialRunContext()
        asset_ids = inv.scope.selected_asset_ids
        base_revision = int(inv.inputs.get("base_revision", 0))
        brief_id = inv.inputs.get("brief_id")

        if inv.skill_id == "topos.auto_caption":
            segs = self._fetch_evidence(asset_ids, "transcript", ctx)
            track_name = str(inv.inputs.get("track_name", "AI 字幕"))
            return es.build_auto_caption(
                project_id=inv.scope.project_id,
                timeline_id=inv.scope.timeline_id,
                base_revision=base_revision,
                transcript_segments=segs,
                track_name=track_name,
                brief_id=brief_id,
            )

        if inv.skill_id == "topos.subtitle_localization":
            segs = self._fetch_evidence(asset_ids, "transcript", ctx)
            target_language = str(inv.inputs.get("target_language", ""))
            # Translation and TTS are host-provided.  TTS may only be placed
            # after the candidate has been materialized and imported to the
            # Media Pool; an absent artifact is an honest, actionable degrade.
            translation = None
            if target_language:
                translation = es.TranslationResult(
                    text_document_id=f"text_doc:{inv.invocation_id[:8]}",
                    target_language=target_language,
                    line_count=len(segs),
                )
            tts_ref = self._resolve_localization_media(inv, segs, target_language)
            return es.build_subtitle_localization(
                project_id=inv.scope.project_id,
                timeline_id=inv.scope.timeline_id,
                base_revision=base_revision,
                subtitle_lines=segs,
                translation=translation, tts_media_ref=tts_ref,
                target_language=target_language, brief_id=brief_id,
            )

        if inv.skill_id == "topos.composite_render_import":
            render_ref = self._resolve_composite_render_media(inv, cancel_requested)
            composite_graph_id = str(inv.inputs.get("composite_graph_id", "") or "")
            return es.build_composite_render_import(
                project_id=inv.scope.project_id,
                timeline_id=inv.scope.timeline_id,
                base_revision=base_revision,
                render_media_ref=render_ref,
                composite_graph_id=composite_graph_id,
                brief_id=brief_id,
            )

        # 未知 editorial skill → 降级
        raise ValueError(f"unknown editorial skill: {inv.skill_id}")

    def _resolve_composite_render_media(
        self, inv: Invocation, cancel_requested: Any = None,
    ) -> Any:
        """Resolve a rendered composite candidate for plan §9.5 runs.

        The host provider executes the headless render and registers the output
        in the Generation Bin; it returns a reference only.  The explicit input
        form (``rendered_media_asset_id``) covers MCP/CLI re-runs after the user
        has already imported a candidate.
        """
        provider = self._composite_render_provider
        if callable(provider):
            try:
                artifact = provider(inv, cancel_requested)
            except Exception:  # noqa: BLE001 -- provider failures degrade, never fake renders
                _LOG.warning("composite render provider failed", exc_info=True)
                return None
            if artifact is not None:
                return artifact

        media_asset_id = str(inv.inputs.get("rendered_media_asset_id", "") or "").strip()
        if not media_asset_id:
            return None
        from src.features.ai_harness.editorial_skills import SkillArtifact
        return SkillArtifact(
            artifact_id=f"media_asset:{media_asset_id}",
            artifact_type="media_generation_ref",
            summary="已渲染并导入媒体池的合成产物",
            trust="unverified",
            payload_ref=media_asset_id,
            media_asset_id=media_asset_id,
        )

    def _resolve_localization_media(
        self, inv: Invocation, transcript_segments: tuple[Any, ...], target_language: str,
    ) -> Any:
        """Resolve only an imported TTS media reference for a localization run.

        A host can inject the resolver when it owns generation/materialization.
        The explicit input form is retained for MCP/CLI and re-plan flows after
        a user has imported a GenerationBin candidate.  It is deliberately not
        a remote handle: PlanValidator still verifies the MediaAsset id before
        preview/approval.
        """
        provider = self._localization_media_provider
        if callable(provider):
            try:
                artifact = provider(inv, transcript_segments, target_language)
            except Exception:  # noqa: BLE001 -- provider failures degrade, never fake audio
                _LOG.warning("localization media resolver failed", exc_info=True)
                return None
            if artifact is not None:
                return artifact

        media_asset_id = str(inv.inputs.get("tts_media_asset_id", "") or "").strip()
        if not media_asset_id:
            return None
        try:
            duration_frames = int(inv.inputs.get("tts_duration_frames", 0) or 0)
            start_frame = int(inv.inputs.get("tts_start_frame", 0) or 0)
        except (TypeError, ValueError):
            return None
        from src.features.ai_harness.editorial_skills import SkillArtifact
        return SkillArtifact(
            artifact_id=f"media_asset:{media_asset_id}",
            artifact_type="media_generation_ref",
            summary="已物化并导入媒体池的 TTS 配音",
            trust="unverified",
            payload_ref=media_asset_id,
            media_asset_id=media_asset_id,
            duration_frames=max(0, duration_frames),
            start_frame=max(0, start_frame),
        )

    def _fetch_evidence(
        self, asset_ids: tuple[str, ...], kind: str,
        run_ctx: "_EditorialRunContext | None" = None,
    ) -> tuple:
        """从注入的 editorial_provider 取 evidence；无 provider 返回空。

        provider 返回二元组 ``(segments, analysis_by_asset)`` 时，analyses 写进
        **run 级局部上下文**（AUD-22008）——不再落实例属性，杜绝跨 run 污染与
        并发数据竞争。
        """
        if self._editorial_provider is None:
            return ()
        try:
            result = self._editorial_provider(asset_ids, kind)
            # provider 可返回 (segments,) 或 (segments, analysis_by_asset)
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
                if run_ctx is not None:
                    run_ctx.analyses = result[1]  # 供 trace 记录用
                return tuple(result[0])
            return tuple(result)
        except Exception:  # noqa: BLE001
            return ()

    def _record_transcript_traces(
        self, run_id: str, asset_ids: tuple[str, ...],
        run_ctx: "_EditorialRunContext | None" = None,
    ) -> None:
        """把每个 asset 的转录路由追踪记进 RunRecord 诊断（plan §10.4 验收）。

        只读**本次 run** 的局部上下文（AUD-22008）；本次 provider 未返回
        analyses 时如实跳过，绝不复用上一 run 的数据。
        """
        analyses = run_ctx.analyses if run_ctx is not None else {}
        if not analyses:
            return
        from src.features.ai_harness.analysis_skills import collect_transcript_trace
        for asset_id in asset_ids:
            analysis = analyses.get(asset_id)
            if analysis is None:
                continue
            try:
                trace = collect_transcript_trace(asset_id, analysis)
                self._runtime.record_diagnostic(run_id, {
                    "severity": "info",
                    "code": "TRANSCRIPT_ROUTE_TRACE",
                    "asset_id": asset_id,
                    "trace": trace.to_public_dict(),
                })
            except Exception:  # noqa: BLE001 — trace 不应让 run 崩
                pass

    def _record_editorial_outcome(
        self, run_id: str, inv: Invocation, outcome: Any,
        run_ctx: "_EditorialRunContext | None" = None,
    ) -> None:
        """把 editorial SkillOutcome 归入 RunRecord（plan §10.4 P2.3）。"""
        # diagnostics
        for diag in getattr(outcome, "diagnostics", ()) or ():
            self._runtime.record_diagnostic(run_id, diag)
        # 转录路由追踪（plan §10.4 验收：Qwen/Demucs/WhisperX 路径可见）
        if inv.skill_id in ("topos.auto_caption", "topos.subtitle_localization"):
            self._record_transcript_traces(run_id, inv.scope.selected_asset_ids, run_ctx)
        # artifacts
        for art in getattr(outcome, "artifacts", ()) or ():
            self._runtime.record_artifact(run_id, ArtifactRef(
                artifact_id=art.artifact_id,
                artifact_type=art.artifact_type,
                producer_node_id=f"editorial:{inv.skill_id}",
                summary=art.summary,
            ))
        degraded = getattr(outcome, "degraded", False)
        plan = getattr(outcome, "plan", None)
        artifacts = getattr(outcome, "artifacts", ()) or ()
        success = True
        with_warnings = degraded
        err_code = ""
        err_msg = ""
        if plan is None:
            # 无 plan：若产了 artifact 或显式降级 → with_warnings（部分产出）；
            # 否则才算失败。
            if artifacts or degraded:
                with_warnings = True
            else:
                success = False
                err_code = "EDITORIAL_NO_PLAN"
                err_msg = "editorial skill 未产出 EditPlan 或 artifact"
        if plan is not None:
            repo = getattr(self._service, "plan_repository", None)
            if repo is not None and hasattr(repo, "store"):
                repo.store(plan)
            # Editorial SkillOutcome already exposes its typed artifact (for
            # auto-caption this is subtitle_draft). The plan is handed to the
            # host observer for preview/approval, but must not be registered a
            # second time as an edit_plan_ref artifact in the same RunRecord.
            self._observe_plan(run_id, plan, record_artifact=False)
        self._runtime.finish(
            run_id, success=success, with_warnings=with_warnings,
            error_code=err_code, error_message=err_msg,
        )

    def _observe_plan(self, run_id: str, plan: Any, *, record_artifact: bool = True) -> None:
        """Record a plan reference without making it executable here."""
        plan_id = str(getattr(plan, "plan_id", "") or "")
        if not plan_id:
            return
        if record_artifact:
            self._runtime.record_artifact(run_id, ArtifactRef(
                artifact_id=plan_id,
                artifact_type="edit_plan_ref",
                producer_node_id="workflow.plan",
                summary=f"EditPlan {plan_id[:12]} ready for preview/approval",
            ))
        if self._plan_observer is not None:
            try:
                self._plan_observer(run_id, plan)
            except Exception:  # noqa: BLE001 — observation must not alter run result
                _LOG.exception("workflow plan observer failed for run %s", run_id)

    def _record_skill_result(self, run_id: str, result: Any) -> None:
        diagnostics = list(getattr(result, "diagnostics", ()) or ())
        for diag in diagnostics:
            self._runtime.record_diagnostic(run_id, diag)
        # typed artifacts（多角色 Skill）
        for art in getattr(result, "artifacts", ()) or ():
            art_id = getattr(art, "id", None) or getattr(art, "draft_id", None) \
                or getattr(art, "set_id", None) or getattr(art, "outline_id", None) \
                or getattr(art, "report_id", None) or ""
            kind = type(art).__name__.lower()
            self._runtime.record_artifact(run_id, ArtifactRef(
                artifact_id=str(art_id),
                artifact_type=kind,
                producer_node_id="skill_subagent",
                summary=f"skill artifact {art_id}",
            ))
        # task_results（取消 / 失败可见）
        cancelled_tasks = getattr(result, "cancelled_tasks", ())
        if cancelled_tasks and self._runtime.is_cancelled(run_id):
            # 取消传播：result 有 cancelled_tasks 且 run 被 cancel → 终态 cancelled
            self._runtime.finish(run_id, success=False)
            return
        degraded = bool(getattr(result, "degraded", False))
        failed_tasks = getattr(result, "failed_tasks", ())
        art_id = getattr(result, "artifact_id", None)
        if art_id:
            self._runtime.record_artifact(run_id, ArtifactRef(
                artifact_id=str(art_id), artifact_type="script",
                producer_node_id="skill",
            ))
        has_variants = bool(getattr(result, "variants", ()))
        has_artifacts = bool(getattr(result, "artifacts", ()))
        has_error_diag = any(
            str(d.get("severity", "")).lower() == "error" for d in diagnostics
        )
        # error 级诊断（UNSUPPORTED_OPERATION / PERMISSION_DENIED 等）→ 失败
        if has_error_diag and not has_variants and not has_artifacts and not art_id:
            err_code = "SKILL_DIAGNOSTIC_ERROR"
            err_msg = next(
                (str(d.get("details", {}).get("reason") or d.get("reason")
                      or d.get("message", ""))
                 for d in diagnostics
                 if str(d.get("severity", "")).lower() == "error"),
                "skill reported error diagnostics",
            )
            self._runtime.finish(
                run_id, success=False, error_code=err_code, error_message=err_msg,
            )
            return
        success = True
        err_code = ""
        err_msg = ""
        if failed_tasks and not has_variants and not has_artifacts and not art_id:
            success = False
            err_code = "SKILL_TASKS_FAILED"
            err_msg = getattr(result, "degradation", "") or "skill tasks failed"
        self._runtime.finish(
            run_id, success=success, with_warnings=degraded or bool(failed_tasks),
            error_code=err_code, error_message=err_msg,
        )


__all__ = [
    "LegacyHarnessAdapter",
    "invocation_to_skill_input",
    "invocation_to_workflow_input",
]
