"""P5.5 发布 workflow 为 Skill（plan §10.7.4）。

> "发布为 Skill"只是把当前 workflow 的 graph template 和声明打包到
> ``SkillDefinition``；聊天自动出现该 Skill 卡片，输入 schema 相同。

核心职责：

- 把 :class:`WorkflowGraph` → :class:`SkillDraft`（复用 P4.1
  ``new_draft_from_workflow``，graph 成为 draft 的 ``workflow_template``）。
- bump draft version 到用户指定的发布版本（≥ 1.0.0）。
- 跑 P4 发布门禁（eval + 版本 + executor；复用 :class:`SkillReleaseGate`）。
- 通过 → :class:`SkillRelease`（携带 graph_template + workflow_hash）。
- 注册进 :class:`SkillRegistry` → 聊天可见。

红线（plan §10.7.4 / §1.4 / §5.3）：

- 发布走 P4 同一门禁（不绕过 eval / executor 安全检查）。
- workflow graph 的 executor_kind 受控（``new_draft_from_workflow`` 已保证）。
- local_dev workflow 发布后仍 unverified。
- 发布产物 skill_id 加 ``draft.`` 前缀（不覆盖 official）。

与既有模块的关系：

- 复用 :func:`new_draft_from_workflow`（P4.1）+ :class:`SkillReleaseGate`（P4.4）。
- 复用 :class:`WorkflowCompiler`（P5.3）做 eval 的 dry-run 路径（eval 套件内部用）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from src.features.edit.ai_editing.editorial_models import SegmentKind

from src.features.ai_harness.skill_release import (
    PublishResult,
    SkillRelease,
    SkillReleaseGate,
)
from src.features.ai_harness.skill_studio_drafts import (
    ModelRequirement,
    SkillDraft,
    SkillDraftVersion,
    new_draft_from_workflow,
)
from src.features.ai_workflow.core.node_specs import NodeSpecRegistry
from src.features.ai_workflow.core.workflow_models import WorkflowGraph


# ---------------------------------------------------------------------- #
# PublishFromWorkflowResult
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class PublishFromWorkflowResult:
    """从 workflow 发布 Skill 的结果（封装 P4 PublishResult + workflow 上下文）。"""

    success: bool
    release: Optional[SkillRelease] = None
    deny_code: str = ""
    deny_message: str = ""
    draft: Optional[SkillDraft] = None
    inner: Optional[PublishResult] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "release": self.release.to_dict() if self.release else None,
            "deny_code": self.deny_code,
            "deny_message": self.deny_message,
            "draft_id": self.draft.draft_id if self.draft else None,
        }


# ---------------------------------------------------------------------- #
# WorkflowPublisher
# ---------------------------------------------------------------------- #


class WorkflowPublisher:
    """把 :class:`WorkflowGraph` 发布为 Skill（plan §10.7.4）。

    用法::

        publisher = WorkflowPublisher(node_registry)
        result = publisher.publish(graph, author="dev", skill_title="T", version="1.0.0")
        if result.success:
            publisher.register_into(skill_registry, result.release)
    """

    def __init__(
        self,
        *,
        node_registry: NodeSpecRegistry,
        gate: Optional[SkillReleaseGate] = None,
    ) -> None:
        self._node_registry = node_registry
        self._gate = gate or SkillReleaseGate()

    def publish(
        self,
        *,
        graph: WorkflowGraph,
        author: str,
        skill_title: str,
        version: str = "1.0.0",
        description: str = "",
        model_requirements: Optional[List[ModelRequirement]] = None,
    ) -> PublishFromWorkflowResult:
        """发布 workflow graph 为 Skill。

        Args:
            graph: 源 workflow graph（成为发布产物的 graph_template）。
            author: 发布者。
            skill_title: 发布 Skill 的用户可见名。
            version: 发布版本（必须 ≥ 1.0.0；dev 0.x 会被门禁拒绝）。
            description: Skill 描述（空则用 graph.name）。
            model_requirements: 显式模型需求（空则从节点 spec 推断）。
        """
        # 1. graph → SkillDraft（复用 P4.1）
        draft = new_draft_from_workflow(
            author=author,
            workflow_definition=graph.to_dict(),
            title=skill_title,
            description=description or graph.name,
            model_requirements=tuple(model_requirements or ()),
        )

        # 2. 从节点 spec 推断模型需求（若用户未显式提供）
        if not model_requirements:
            inferred = self._infer_model_requirements(graph)
            if inferred:
                from dataclasses import replace
                draft = replace(draft, model_requirements=inferred)

        # 2b. 从节点 spec 推断 required_evidence_kinds（使 eval 的 evidence
        # 用例可触发；new_draft_from_workflow 不自动推断）
        inferred_kinds = self._infer_evidence_kinds(graph)
        if inferred_kinds:
            from dataclasses import replace
            draft = replace(
                draft,
                manifest=replace(
                    draft.manifest,
                    required_evidence_kinds=tuple(inferred_kinds),
                ),
            )

        # 3. bump draft version 到发布版本
        draft = self._bump_to_version(draft, version)

        # 4. 跑 P4 发布门禁
        inner = self._gate.publish(draft, released_by=author)
        if not inner.success:
            return PublishFromWorkflowResult(
                success=False,
                deny_code=inner.deny_code,
                deny_message=inner.deny_message,
                draft=draft,
                inner=inner,
            )

        return PublishFromWorkflowResult(
            success=True,
            release=inner.release,
            draft=draft,
            inner=inner,
        )

    def register_into(self, registry: Any, release: SkillRelease) -> List[Any]:
        """注册发布产物进 SkillRegistry（聊天可见）。返回诊断列表。"""
        return self._gate.register_into(registry, release)

    # ---------------- 内部 ---------------- #

    def _infer_model_requirements(
        self, graph: WorkflowGraph,
    ) -> tuple[ModelRequirement, ...]:
        """从 graph 节点的 spec_id 推断模型需求。"""
        reqs: list[ModelRequirement] = []
        seen: set[str] = set()
        for n in graph.nodes:
            spec = self._node_registry.get(n.spec_id)
            if spec is None:
                continue
            for cap in spec.required_capabilities:
                if cap in seen:
                    continue
                seen.add(cap)
                reqs.append(ModelRequirement(
                    capability=cap,
                    required=True,
                    estimated_vram_mb=0,
                ))
        return tuple(reqs)

    def _infer_evidence_kinds(self, graph: WorkflowGraph) -> list["SegmentKind"]:
        """从 graph 节点 spec 的端口 artifact type 推断所需 evidence kind。

        规则（plan §3.1 artifact type → SegmentKind）：
        - transcript_ref / subtitle_track_ref 出现 → TRANSCRIPT
        - analysis_ref 出现 → 视具体 capability（这里保守只加 TRANSCRIPT，
          因为 OCR/shot 等是可选增强）
        """
        from src.features.edit.ai_editing.editorial_models import SegmentKind
        kinds: list[SegmentKind] = []
        has_transcript = False
        for n in graph.nodes:
            spec = self._node_registry.get(n.spec_id)
            if spec is None:
                continue
            for p in spec.inputs + spec.outputs:
                if p.artifact_type in ("transcript_ref", "subtitle_track_ref"):
                    has_transcript = True
        if has_transcript:
            kinds.append(SegmentKind.TRANSCRIPT)
        return kinds

    def _bump_to_version(self, draft: SkillDraft, target_version: str) -> SkillDraft:
        """把 draft version bump 到目标发布版本。"""
        target = SkillDraftVersion.from_string(target_version)
        # 直接构造目标版本的 draft（frozen → replace）
        from dataclasses import replace
        # 重算 manifest version
        from src.features.edit.ai_editing.skill_manifest import with_signature
        new_manifest = replace(draft.manifest, version=str(target))
        new_manifest = with_signature(new_manifest, signed_by=f"local_dev:{draft.author}")
        return replace(draft, version=target, manifest=new_manifest)


__all__ = [
    "PublishFromWorkflowResult",
    "WorkflowPublisher",
]
