"""P5.3 WorkflowCompiler（plan §10.7.2）。

> ``WorkflowDefinition`` 只保存节点、端口、参数、布局和暴露输入；点击 Run 时
> 由 ``WorkflowCompiler`` 生成 ``Invocation`` 并调用 ``HarnessRuntime``。

核心职责（plan §10.7.2）：

1. 先跑 :class:`WorkflowValidator`（P5.2）；不合法 → 拒绝编译（含诊断）。
2. 从 graph 的 ``exposed_inputs`` + 节点参数推断 :class:`Invocation.inputs`。
3. graph.policy 映射为 :class:`RunPolicy`（timeline_write / network / budget）。
4. 构造 :class:`Invocation`（``source="workflow"``，红线：审计分流）。
5. 复用 :class:`SkillCompiler` 产出 :class:`CompiledSkill`，host 用
   :meth:`LegacyHarnessAdapter.invoke_compiled` 执行 —— **不**新建第二个
   WorkflowExecutor（plan §10.9 验收红线）。

红线（plan §10.7.2 / §10.9 / §1.4）：

- 编译产物走同一 ``invoke_compiled``；chat / 快捷卡 / workflow 三入口同底层。
- ``Invocation.source = "workflow"``；``user_text = ""``（避免 prompt 注入）。
- 编译期不执行任务（纯 Python；plan §10.1）。
- workflow graph 不直接写时间线；timeline 写仍经 EditPlan → Approval → CommandStack。

与既有模块的关系：

- 用 :class:`WorkflowValidator`（本包 P5.2）做合法性检查。
- 用 :class:`SkillCompiler`（ai_harness P1.3）+ :class:`SkillDefinition` 产出
  :class:`CompiledSkill`。
- host 装配侧（M5+ UI）拿到 :class:`WorkflowCompilation` 后调
  ``adapter.invoke_compiled(result.compiled, result.invocation)``。
"""
from __future__ import annotations

import time
import uuid
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set, Tuple

from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
    PermissionLevel,
    RunPolicy,
    sanitize_for_storage,
)
from src.features.ai_harness.skill_compiler import (
    CompiledSkill,
    SkillCompiler,
    SkillDefinition,
)
from src.features.edit.ai_editing.skill_manifest import SkillManifest, SkillSource
from src.features.ai_workflow.core.node_specs import NodeSpecRegistry
from src.features.ai_workflow.core.workflow_models import (
    WorkflowGraph,
    WorkflowPolicy,
)
from src.features.ai_workflow.core.workflow_validator import (
    WorkflowValidator,
)


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- #
# WorkflowCompilation —— 编译产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkflowCompilation:
    """WorkflowCompiler 的编译产物（plan §10.7.2）。

    success=True 时携带 invocation + compiled + graph；success=False 时携带
    diagnostics（validator 错误）。host 用 ``invoke_compiled(compiled, invocation)``
    执行（同一 RunRecord / approval / undo）。
    """

    success: bool
    graph: WorkflowGraph
    invocation: Optional[Invocation] = None
    compiled: Optional[CompiledSkill] = None
    diagnostics: Tuple[Dict[str, Any], ...] = ()
    compiled_at: float = 0.0
    plan_hash: str = ""
    plan_preview: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "graph": self.graph.to_dict(),
            "invocation": (
                sanitize_for_storage(self.invocation.to_public_dict())
                if self.invocation else None
            ),
            "compiled": (
                {
                    "target": self.compiled.target,
                    "runnable": self.compiled.runnable,
                    "skill_id": self.compiled.skill_id,
                }
                if self.compiled else None
            ),
            "diagnostics": list(self.diagnostics),
            "compiled_at": self.compiled_at,
            "plan_hash": self.plan_hash,
            "plan_preview": sanitize_for_storage(self.plan_preview),
        }


# ---------------------------------------------------------------------- #
# WorkflowCompiler
# ---------------------------------------------------------------------- #


class WorkflowCompiler:
    """把 :class:`WorkflowGraph` 编译为 :class:`Invocation` + :class:`CompiledSkill`。

    用法（host 装配侧）::

        compiler = WorkflowCompiler(node_registry)
        result = compiler.compile(graph, skill_id="topos.voiceover_polish", ...)
        if result.success:
            run_id = adapter.invoke_compiled(result.compiled, result.invocation)
    """

    def __init__(
        self,
        node_registry: NodeSpecRegistry,
        *,
        skill_compiler: Optional[SkillCompiler] = None,
        validator: Optional[WorkflowValidator] = None,
    ) -> None:
        self._registry = node_registry
        self._validator = validator or WorkflowValidator(node_registry)
        self._skill_compiler = skill_compiler or SkillCompiler()

    def compile(
        self,
        graph: WorkflowGraph,
        *,
        skill_id: str,
        project_id: str,
        timeline_id: str = "",
        selected_asset_ids: Tuple[str, ...] = (),
        brief_id: str = "",
        brief_revision: int = 0,
        base_revision: int | None = None,
        analysis_snapshot_hash: str = "",
        profile_id: str = "",
        network_allowed: bool = False,
        available_capabilities: Optional[Set[str]] = None,
        permission: str = PermissionLevel.PROPOSE,
        target_duration: int | None = None,
        now: float = 0.0,
    ) -> WorkflowCompilation:
        """编译 workflow graph。返回 :class:`WorkflowCompilation`。

        Args:
            skill_id: 目标 Skill / workflow ID（如 ``topos.voiceover_polish``）。
                host 决定 graph 编译为哪个 skill（官方模板 → 对应 skill_id；
                自定义 graph → 用户命名）。
            project_id / timeline_id / selected_asset_ids: 运行时作用范围。
            available_capabilities: host 能力集（validator 据此检查缺模型）。
            permission: 权限等级（默认 propose；红线：不含"完全访问"）。
            target_duration: 目标时长（秒）。``None`` 时从 graph 节点 params 的
                ``target_duration`` 推导（AUD-23210：模板 planner 节点声明的
                60/90 必须流入运行时，而不是被硬编码 720 覆盖）；graph 也没有
                时退回 720（与 legacy 默认一致）。
        """
        ts = now or time.time()
        caps = available_capabilities or set()

        # 1. 校验
        findings = self._validator.validate(graph, available_capabilities=caps)
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            diags = tuple(
                sanitize_for_storage({
                    "severity": f.severity, "code": f.code,
                    "node_id": f.node_id, "port": f.port, "message": f.message,
                })
                for f in findings
            )
            return WorkflowCompilation(
                success=False, graph=graph, diagnostics=diags, compiled_at=ts,
            )

        # 2. graph.policy → RunPolicy
        policy = self._build_policy(graph.policy, permission)

        # 4. 构造 Invocation（source=workflow，user_text=""）
        invocation = Invocation(
            invocation_id=uuid.uuid4().hex,
            source=InvocationSource.WORKFLOW,
            skill_id=skill_id,
            scope=InvocationScope(
                project_id=project_id,
                timeline_id=timeline_id,
                selected_asset_ids=tuple(selected_asset_ids),
                brief_id=brief_id or f"wf-{graph.workflow_id}",
                brief_revision=max(0, int(brief_revision)),
                base_revision=base_revision,
                analysis_snapshot_hash=analysis_snapshot_hash,
                network_allowed=network_allowed,
            ),
            policy=RunPolicy(
                permission=policy.permission,
                profile_id=profile_id,
                model_id=policy.model_id,
                cost_budget=policy.cost_budget,
                timeout_ms=policy.timeout_ms,
                timeline_write=policy.timeline_write,
                allow_model_download=policy.allow_model_download,
                allow_generation=policy.allow_generation,
                notes=policy.notes,
            ),
            inputs=self._build_inputs(
                graph,
                selected_asset_ids,
                timeline_id,
                brief_id=brief_id or f"wf-{graph.workflow_id}",
                base_revision=base_revision,
                analysis_snapshot_hash=analysis_snapshot_hash,
                target_duration=target_duration,
            ),
            user_text="",  # 红线：workflow 入口不带用户文本
            created_at=ts,
        )

        # 5. SkillCompiler 产出 CompiledSkill（复用同一编译路径）
        definition = self._build_definition(skill_id, graph)
        compiled = self._skill_compiler.compile(definition, invocation)
        plan_preview = self._build_plan_preview(
            graph, invocation, available_capabilities=caps,
        )
        plan_hash = _stable_hash({
            "graph_hash": graph.content_hash,
            # Exclude invocation_id/created_at: the same live input must produce
            # the same preview hash even when it is compiled twice.
            "scope": invocation.scope.to_dict(),
            "policy": invocation.policy.to_dict(),
            "inputs": invocation.inputs,
            "plan_preview": plan_preview,
        })

        # 把 validator 的 warning/info 也带进 diagnostics
        warn_diags = tuple(
            sanitize_for_storage({
                "severity": f.severity, "code": f.code,
                "node_id": f.node_id, "message": f.message,
            })
            for f in findings if f.severity != "error"
        )

        return WorkflowCompilation(
            success=compiled.runnable,
            graph=graph,
            invocation=invocation,
            compiled=compiled,
            diagnostics=warn_diags + compiled.diagnostics,
            compiled_at=ts,
            plan_hash=plan_hash,
            plan_preview={**plan_preview, "plan_hash": plan_hash},
        )

    # ---------------- 内部 ---------------- #

    def _build_inputs(
        self,
        graph: WorkflowGraph,
        selected_asset_ids: Tuple[str, ...],
        timeline_id: str,
        *,
        brief_id: str,
        base_revision: int | None,
        analysis_snapshot_hash: str,
        target_duration: int | None = None,
    ) -> Dict[str, Any]:
        """构造 invocation.inputs。

        包含：

        - **workflow 输入契约字段**（``invocation_to_workflow_input`` 需要）：
          ``asset_ids`` / ``brief_id`` / ``base_revision`` /
          ``analysis_snapshot_hash`` / ``target_duration``。
        - **exposed_inputs** 占位（host 在 invoke 前填实际 artifact 引用）。
        - **每个节点的 params**（key 前缀 ``node_<node_id>_``）。
        """
        inputs: Dict[str, Any] = {
            # workflow 输入契约（invocation_to_workflow_input 读取这些）
            "asset_ids": list(selected_asset_ids),
            "brief_id": brief_id,
            "base_revision": 0 if base_revision is None else int(base_revision),
            "analysis_snapshot_hash": analysis_snapshot_hash,
            # AUD-23210：目标时长优先级 = 显式参数 > graph 节点声明 > 720
            # （legacy 默认）。模板 planner 节点声明的 60/90 由此流入运行时。
            "target_duration": self._resolve_target_duration(
                graph, target_duration,
            ),
            # 节点 id 快照（有序全量）：invocation_to_workflow_input 回填进
            # WorkflowInput.node_ids，供逐节点事件（P9 画布着色）。不能用
            # ``node_<id>_params`` 键反推——无参节点会被跳过且无序。
            "workflow_node_ids": [n.node_id for n in graph.nodes],
        }
        # exposed_inputs 占位（host 装配侧负责填实际值）
        for exposed_name, node_id, port in graph.exposed_inputs:
            inputs[exposed_name] = {
                "_kind": "exposed_input",
                "node_id": node_id,
                "port": port,
            }
        # 节点参数（host / executor 可读取）
        for n in graph.nodes:
            if n.params:
                inputs[f"node_{n.node_id}_params"] = dict(n.params)
        return inputs

    #: graph 节点均未声明 target_duration 时的兜底（与 legacy_adapters 默认一致）。
    _DEFAULT_TARGET_DURATION = 720

    @classmethod
    def _resolve_target_duration(
        cls, graph: WorkflowGraph, explicit: int | None,
    ) -> int:
        """解析运行时 target_duration（AUD-23210）。

        优先级：显式 compile 参数 > graph 中首个声明 ``target_duration`` param
        的节点（按节点顺序，planner 类节点声明）> 默认 720。
        """
        if explicit is not None:
            return int(explicit)
        for node in graph.nodes:
            declared = node.params.get("target_duration") if node.params else None
            if declared is None:
                continue
            try:
                return int(declared)
            except (TypeError, ValueError):
                # 非法声明（如字符串）不流入运行时；validator 的
                # INVALID_PARAM_TYPE 会给出可定位诊断。
                continue
        return cls._DEFAULT_TARGET_DURATION

    def _build_plan_preview(
        self,
        graph: WorkflowGraph,
        invocation: Invocation,
        *,
        available_capabilities: Set[str],
    ) -> Dict[str, Any]:
        """Build a deterministic, non-mutating preview for the Run gate."""
        nodes: list[dict[str, Any]] = []
        total_cost = 0.0
        for node in graph.nodes:
            spec = self._registry.get(node.spec_id)
            estimated_cost = float(getattr(spec, "estimated_cost", 0.0) or 0.0)
            total_cost += estimated_cost
            nodes.append({
                "node_id": node.node_id,
                "spec_id": node.spec_id,
                "action": getattr(spec, "executor_id", "unknown"),
                "required_capabilities": list(getattr(spec, "required_capabilities", ()) or ()),
                "timeline_write": bool(getattr(spec, "timeline_write_required", False)),
                "network": bool(getattr(spec, "network_required", False)),
                "estimated_cost": estimated_cost,
                "risk": "timeline_write" if getattr(spec, "timeline_write_required", False) else "read_only",
            })
        return {
            "workflow_id": graph.workflow_id,
            "graph_hash": graph.content_hash,
            "scope": invocation.scope.to_dict(),
            "permission": invocation.policy.permission,
            "profile_id": invocation.policy.profile_id,
            "available_capabilities": sorted(available_capabilities),
            "nodes": nodes,
            "edge_count": graph.edge_count,
            "estimated_cost": total_cost,
            "timeline_write_requires_approval": any(n["timeline_write"] for n in nodes),
            "execution": "preview_only_until_approval",
        }

    def _build_policy(self, gp: WorkflowPolicy, permission: str) -> RunPolicy:
        """graph.policy → RunPolicy。"""
        return RunPolicy(
            permission=permission,
            timeline_write=gp.timeline_write,
            cost_budget=gp.cost_budget,
            allow_model_download=gp.allow_model_download,
            allow_generation=gp.allow_generation,
        )

    def _build_definition(self, skill_id: str, graph: WorkflowGraph) -> SkillDefinition:
        """构造 SkillDefinition（编译时用；workflow 来源不持久化为 manifest）。

        host 决定 skill_id；本方法产最小 SkillDefinition 供 SkillCompiler 路由。
        若 skill_id 对应已注册的官方 manifest，复用之；否则用 local_dev 占位。
        execution_hint 按 skill 家族决定：editorial builder skill → "editorial"；
        其它 → "workflow"（SkillCompiler 据此路由 target）。
        """
        from src.features.ai_harness.skill_compiler import EDITORIAL_BUILDER_SKILL_IDS
        from src.features.edit.ai_editing.skill_manifest import OFFICIAL_SKILLS

        # editorial builder skill（auto_caption / subtitle_localization）→ editorial
        is_editorial = skill_id in EDITORIAL_BUILDER_SKILL_IDS
        hint = "editorial" if is_editorial else "workflow"

        # 尝试用已注册的官方 manifest（如果 skill_id 命中）
        for m in OFFICIAL_SKILLS:
            if m.skill_id == skill_id:
                return SkillDefinition(
                    manifest=m, execution_hint=hint, is_official=True,
                )
        # P2 editorial manifest（不在 OFFICIAL_SKILLS，在 p2_official_manifests）
        if is_editorial:
            from src.features.ai_harness.editorial_skills import p2_official_manifests
            for m in p2_official_manifests():
                if m.skill_id == skill_id:
                    return SkillDefinition(
                        manifest=m, execution_hint=hint, is_official=True,
                    )
        # 自定义 graph：构造占位 manifest（local_dev；roles=() → workflow target）
        placeholder = SkillManifest(
            skill_id=skill_id,
            version="1.0.0",
            title=graph.name or skill_id,
            description=f"workflow graph: {graph.workflow_id}",
            source=SkillSource.LOCAL_DEV,
            produces_plan=True,
        )
        return SkillDefinition(
            manifest=placeholder, execution_hint=hint, is_official=False,
        )


__all__ = [
    "WorkflowCompilation",
    "WorkflowCompiler",
]
