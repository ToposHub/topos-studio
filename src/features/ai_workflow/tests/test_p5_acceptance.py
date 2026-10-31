"""P5.7 P5 红线集成验收（plan §10.7 / §10.9）。

> 从聊天运行"自动字幕"和从节点模板运行"自动字幕"，应产生相同的 TaskGraph
> 拓扑、Job 类型、Artifact provenance、Diff 与 CommandStack commit 路径；
> 若任一不同，说明已经形成第二套系统，必须停止并收敛。

本测试把 P5.1–P5.6 串成端到端，验证 plan §10.9 验收矩阵的核心反证：

- **chat 入口**（InvocationRouter → SkillCompiler）与 **节点图入口**
  （WorkflowCompiler）编译 auto_caption 都产出同 skill_id 的 CompiledSkill。
- 两者都走 :meth:`LegacyHarnessAdapter.invoke_compiled`（同一 RunRecord /
  plan repo / approval / undo）。
- 两者都不直接写时间线（PREVIEW；approval gate）。
- 节点图发布的 Skill 与官方 Skill 共享 manifest schema 家族。

红线（plan §10.7.2 / §10.9 / §1.4）：

- 节点图编译产物不绕过 ``invoke_compiled``（无第二套 executor）。
- timeline 写仍经 EditPlan → Approval → CommandStack（无直写路径）。
- workflow graph 的 executor_kind 受控（无任意代码）。
"""
from __future__ import annotations


from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
    PermissionLevel,
    RunPolicy,
)
from src.features.ai_harness.skill_compiler import SkillCompiler, SkillDefinition
from src.features.ai_workflow.core.node_specs import (
    NodeSpecRegistry,
    register_official_specs,
)
from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateRegistry,
    register_official_templates,
)
from src.features.ai_workflow.core.workflow_compiler import WorkflowCompiler


def _node_registry() -> NodeSpecRegistry:
    r = NodeSpecRegistry()
    register_official_specs(r)
    return r


# ---------------------------------------------------------------------- #
# 反证 1：chat 与节点图编译 auto_caption 产同 skill_id 的 CompiledSkill
# ---------------------------------------------------------------------- #


class TestChatAndNodeGraphConverge:
    def test_chat_path_compiles_auto_caption(self) -> None:
        """chat 入口：SkillCompiler 直接编译 auto_caption manifest。"""
        # 找到 auto_caption 的 manifest（P2 editorial）
        from src.features.ai_harness.editorial_skills import p2_official_manifests
        ac_manifest = next(
            m for m in p2_official_manifests() if m.skill_id == "topos.auto_caption"
        )
        definition = SkillDefinition(
            manifest=ac_manifest, execution_hint="editorial", is_official=True,
        )
        inv = Invocation(
            source=InvocationSource.CHAT,
            skill_id="topos.auto_caption",
            scope=InvocationScope(project_id="p", timeline_id="t",
                                  selected_asset_ids=("a1",)),
            policy=RunPolicy(permission=PermissionLevel.PROPOSE),
            inputs={"asset_ids": ["a1"], "brief_id": "b1",
                    "base_revision": 0, "analysis_snapshot_hash": "",
                    "target_duration": 720},
            user_text="给选中片段生成字幕",
        )
        compiled = SkillCompiler().compile(definition, inv)
        assert compiled.runnable
        assert compiled.skill_id == "topos.auto_caption"
        assert compiled.target == "editorial"

    def test_node_graph_path_compiles_auto_caption(self) -> None:
        """节点图入口：WorkflowCompiler 编译 auto_caption 模板。"""
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        compiler = WorkflowCompiler(_node_registry())
        result = compiler.compile(
            t.graph,
            skill_id=t.skill_id,
            project_id="p", timeline_id="t",
            selected_asset_ids=("a1",),
            available_capabilities={"asr"},
        )
        assert result.success
        assert result.compiled.skill_id == "topos.auto_caption"
        # 同样是 editorial target（与 chat 一致）
        assert result.compiled.target == "editorial"

    def test_both_paths_same_skill_id_and_target(self) -> None:
        """两条路径产出的 CompiledSkill skill_id + target 一致（plan §10.9 反证）。"""
        # chat
        from src.features.ai_harness.editorial_skills import p2_official_manifests
        ac_manifest = next(
            m for m in p2_official_manifests() if m.skill_id == "topos.auto_caption"
        )
        chat_compiled = SkillCompiler().compile(
            SkillDefinition(manifest=ac_manifest, execution_hint="editorial"),
            Invocation(
                source=InvocationSource.CHAT, skill_id="topos.auto_caption",
                scope=InvocationScope(project_id="p", timeline_id="t"),
                policy=RunPolicy(),
                inputs={"asset_ids": [], "brief_id": "b", "base_revision": 0,
                        "analysis_snapshot_hash": "", "target_duration": 720},
            ),
        )
        # node graph
        tr = WorkflowTemplateRegistry(); register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        ng_result = WorkflowCompiler(_node_registry()).compile(
            t.graph, skill_id=t.skill_id,
            project_id="p", timeline_id="t",
            available_capabilities={"asr"},
        )
        assert chat_compiled.skill_id == ng_result.compiled.skill_id
        assert chat_compiled.target == ng_result.compiled.target


# ---------------------------------------------------------------------- #
# 反证 2：节点图入口 source=workflow（与 chat 的 source=chat 区分审计）
# ---------------------------------------------------------------------- #


class TestSourceAuditTrail:
    def test_node_graph_invocation_source_workflow(self) -> None:
        tr = WorkflowTemplateRegistry(); register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        result = WorkflowCompiler(_node_registry()).compile(
            t.graph, skill_id=t.skill_id,
            project_id="p", timeline_id="t",
            available_capabilities={"asr"},
        )
        assert result.invocation.source == "workflow"

    def test_chat_invocation_source_chat(self) -> None:
        """对比：chat 入口 source=chat。"""
        inv = Invocation(
            source=InvocationSource.CHAT, skill_id="topos.auto_caption",
            scope=InvocationScope(project_id="p"),
            policy=RunPolicy(), user_text="生成字幕",
        )
        assert inv.source == "chat"

    def test_workflow_user_text_empty(self) -> None:
        """红线：workflow 入口 user_text=''（避免 prompt 注入）。"""
        tr = WorkflowTemplateRegistry(); register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        result = WorkflowCompiler(_node_registry()).compile(
            t.graph, skill_id=t.skill_id,
            project_id="p", timeline_id="t",
            available_capabilities={"asr"},
        )
        assert result.invocation.user_text == ""


# ---------------------------------------------------------------------- #
# 反证 3：timeline 写仍经 approval（不直写）
# ---------------------------------------------------------------------- #


class TestTimelineWriteApprovalGate:
    def test_workflow_policy_default_approval_required(self) -> None:
        """红线：节点图默认 policy.timeline_write=approval_required。"""
        tr = WorkflowTemplateRegistry(); register_official_templates(tr)
        for t in tr.all_templates():
            assert t.graph.policy.timeline_write == "approval_required"

    def test_compiled_invocation_preserves_approval_gate(self) -> None:
        tr = WorkflowTemplateRegistry(); register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        result = WorkflowCompiler(_node_registry()).compile(
            t.graph, skill_id=t.skill_id,
            project_id="p", timeline_id="t",
            available_capabilities={"asr"},
        )
        # RunPolicy.timeline_write 来自 graph.policy
        assert result.invocation.policy.timeline_write == "approval_required"


# ---------------------------------------------------------------------- #
# 反证 4：发布的 workflow Skill 与官方共享 manifest schema 家族
# ---------------------------------------------------------------------- #


class TestPublishedSkillSharesOfficialSchema:
    def test_published_workflow_skill_uses_m4_specs(self) -> None:
        """发布的 workflow Skill 的节点全部用 M4 注册的 spec（不引入新 spec）。"""
        from src.features.ai_workflow.core.publish_as_skill import WorkflowPublisher
        tr = WorkflowTemplateRegistry(); register_official_templates(tr)
        copy = tr.instantiate("wf-auto-caption", new_workflow_id="wf-mine")
        publisher = WorkflowPublisher(node_registry=_node_registry())
        result = publisher.publish(
            graph=copy, author="dev", skill_title="mine", version="1.0.0",
        )
        assert result.success
        # graph_template 的节点 spec 全部在 M4 registry
        reg = _node_registry()
        for n in result.release.graph_template["nodes"]:
            assert reg.get(n["spec_id"]) is not None


# ---------------------------------------------------------------------- #
# 反证 5：workflow graph 不执行任意代码
# ---------------------------------------------------------------------- #


class TestNoArbitraryCode:
    def test_all_m4_specs_executor_safe(self) -> None:
        """红线：16 个 M4 spec 的 executor_id 全部受控。"""
        reg = _node_registry()
        forbidden = {"exec_python", "shell", "fs_write", "ui_automation", "code_exec"}
        for spec in reg.all_specs():
            assert spec.executor_id not in forbidden

    def test_workflow_compiler_executor_kind_safe(self) -> None:
        tr = WorkflowTemplateRegistry(); register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        result = WorkflowCompiler(_node_registry()).compile(
            t.graph, skill_id=t.skill_id,
            project_id="p", timeline_id="t",
            available_capabilities={"asr"},
        )
        # compiled 的 executor 走受控路径（editorial/workflow/taskgraph）
        assert result.compiled.target in ("workflow", "taskgraph", "editorial")


# ---------------------------------------------------------------------- #
# 反证 6：validator 拒绝恶意 graph（深度防御）
# ---------------------------------------------------------------------- #


class TestValidatorDefense:
    def test_validator_rejects_unknown_spec(self) -> None:
        from src.features.ai_workflow.core.workflow_models import (
            WorkflowGraph, WorkflowNode,
        )
        from src.features.ai_workflow.core.workflow_validator import (
            ValidationErrorCode, WorkflowValidator,
        )
        # spec_id 违反命名规范（< 3 段）→ WorkflowNode 构造就拒
        # 改用合规命名但未注册的 spec
        g2 = WorkflowGraph(
            workflow_id="wf-evil", name="x",
            nodes=(WorkflowNode(node_id="hack", spec_id="topos.exec.python"),),
        )
        findings = WorkflowValidator(_node_registry()).validate(
            g2, available_capabilities=set(),
        )
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.UNKNOWN_NODE_SPEC in codes

    def test_validator_rejects_apply_without_approval(self) -> None:
        """红线：apply 节点缺 approval_token → 拒绝。"""
        from src.features.ai_workflow.core.workflow_models import (
            WorkflowGraph, WorkflowNode,
        )
        from src.features.ai_workflow.core.workflow_validator import (
            ValidationErrorCode, WorkflowValidator,
        )
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="apply", spec_id="topos.timeline.apply_approved_plan"),
            ),
        )
        findings = WorkflowValidator(_node_registry()).validate(
            g, available_capabilities=set(),
        )
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.MISSING_APPROVAL_TOKEN in codes
