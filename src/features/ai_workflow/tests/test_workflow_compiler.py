"""P5.3 测试：WorkflowCompiler（plan §10.7.2）。

> ``WorkflowDefinition`` 只保存节点、端口、参数、布局和暴露输入；点击 Run 时
> 由 ``WorkflowCompiler`` 生成 ``Invocation`` 并调用 ``HarnessRuntime``。

验证 :class:`WorkflowCompiler` 的核心契约：

- 把 :class:`WorkflowGraph` 编译为 :class:`Invocation`（source=workflow）。
- 复用 :class:`SkillCompiler` 路径：产出 :class:`CompiledSkill`，host 用
  :meth:`LegacyHarnessAdapter.invoke_compiled` 执行（同一 RunRecord / plan repo /
  approval / undo；plan §10.7.2）。
- 编译前先跑 :class:`WorkflowValidator`（P5.2）；不合法 → 拒绝编译。
- 从 graph 的暴露输入（exposed_inputs）+ 节点参数推断 invocation.inputs。
- graph 的 policy 映射为 :class:`RunPolicy`。

红线（plan §10.7.2 / §10.9）：

- 编译产物走同一 ``invoke_compiled``；**不**新建第二个 WorkflowExecutor。
- ``Invocation.source = "workflow"``（与 chat 的 "chat" 区分审计）。
- 编译期不执行任务（纯 Python；plan §10.1）。
- workflow graph 不直接写时间线；timeline 写仍经 EditPlan → Approval → CommandStack。
"""
from __future__ import annotations


from src.features.ai_workflow.core.node_specs import (
    NodeSpecRegistry,
    register_official_specs,
)
from src.features.ai_workflow.core.workflow_compiler import (
    WorkflowCompiler,
)
from src.features.ai_workflow.core.workflow_models import (
    PortRef,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowPolicy,
)


def _registry() -> NodeSpecRegistry:
    r = NodeSpecRegistry()
    register_official_specs(r)
    return r


def _voiceover_graph() -> WorkflowGraph:
    """一个映射到 voiceover_polish workflow 的简化 graph。

    用 voiceover_polish 因为它是 BUILTIN_SPECS 之一，invocation_to_workflow_input
    能把 skill_id='topos.voiceover_polish' 反解回 WorkflowId.VOICEOVER_POLISH。
    """
    return WorkflowGraph(
        workflow_id="wf-voiceover",
        name="口播精剪",
        nodes=(
            WorkflowNode(
                node_id="input",
                spec_id="topos.media.selected_assets",
                params={"media_kind": "video"},
            ),
            WorkflowNode(
                node_id="rough_cut",
                spec_id="topos.agent.rough_cut_plan",
            ),
        ),
        edges=(
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="rough_cut", port="assets"),
            ),
        ),
        exposed_inputs=(
            ("assets", "input", "assets"),
        ),
        policy=WorkflowPolicy(),
    )


# ---------------------------------------------------------------------- #
# 编译成功
# ---------------------------------------------------------------------- #


class TestCompileSuccess:
    def test_compile_produces_invocation(self) -> None:
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="proj-1",
            timeline_id="tl-1",
            selected_asset_ids=("a1", "a2"),
            available_capabilities={"asr", "llm"},
        )
        assert result.success
        assert result.invocation is not None
        assert result.invocation.source == "workflow"  # 红线：source=workflow

    def test_compile_invocation_carries_scope(self) -> None:
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="proj-1", timeline_id="tl-1",
            selected_asset_ids=("a1",),
            available_capabilities={"asr", "llm"},
        )
        inv = result.invocation
        assert inv.scope.project_id == "proj-1"
        assert inv.scope.timeline_id == "tl-1"
        assert inv.scope.selected_asset_ids == ("a1",)

    def test_compile_invocation_skill_id_set(self) -> None:
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities={"asr", "llm"},
        )
        assert result.invocation.skill_id == "topos.voiceover_polish"

    def test_compile_policy_mapped_from_graph(self) -> None:
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities={"asr", "llm"},
        )
        # graph.policy.timeline_write=approval_required → policy.timeline_write 一致
        assert result.invocation.policy.timeline_write == "approval_required"


# ---------------------------------------------------------------------- #
# 编译失败（validator 拒绝）
# ---------------------------------------------------------------------- #


class TestCompileValidationFailure:
    def test_compile_cycle_rejected(self) -> None:
        g = WorkflowGraph(
            workflow_id="wf-cycle", name="x",
            nodes=(
                WorkflowNode(node_id="a", spec_id="topos.report.run_summary"),
                WorkflowNode(node_id="b", spec_id="topos.report.run_summary"),
            ),
            edges=(
                WorkflowEdge(
                    from_port=PortRef(node_id="a", port="summary"),
                    to_port=PortRef(node_id="b", port="any"),
                ),
                WorkflowEdge(
                    from_port=PortRef(node_id="b", port="summary"),
                    to_port=PortRef(node_id="a", port="any"),
                ),
            ),
        )
        c = WorkflowCompiler(_registry())
        result = c.compile(
            g, skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities=set(),
        )
        assert not result.success
        assert result.invocation is None
        # diagnostics 含 error
        assert any(d.get("severity") == "error" for d in result.diagnostics)

    def test_compile_missing_capability_rejected(self) -> None:
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities=set(),  # 缺 asr/llm
        )
        assert not result.success


# ---------------------------------------------------------------------- #
# 复用 SkillCompiler 路径
# ---------------------------------------------------------------------- #


class TestReusesSkillCompilerPath:
    def test_compilation_carries_compiled_skill(self) -> None:
        """编译产物含 CompiledSkill，供 invoke_compiled 执行（同一底层）。"""
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities={"asr", "llm"},
        )
        assert result.compiled is not None
        # CompiledSkill.target 应映射到可执行目标
        assert result.compiled.target in ("workflow", "taskgraph", "editorial")

    def test_compilation_runnable_when_valid(self) -> None:
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities={"asr", "llm"},
        )
        assert result.compiled.runnable


# ---------------------------------------------------------------------- #
# 红线：source=workflow（审计分流）
# ---------------------------------------------------------------------- #


class TestSourceWorkflow:
    def test_invocation_source_is_workflow(self) -> None:
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities={"asr", "llm"},
        )
        assert result.invocation.source == "workflow"

    def test_invocation_user_text_empty(self) -> None:
        """红线：workflow 入口不带用户文本（避免 prompt 注入）。"""
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities={"asr", "llm"},
        )
        assert result.invocation.user_text == ""


# ---------------------------------------------------------------------- #
# graph_template 透传（发布为 Skill 时用）
# ---------------------------------------------------------------------- #


class TestGraphTemplatePassthrough:
    def test_compilation_carries_graph(self) -> None:
        g = _voiceover_graph()
        c = WorkflowCompiler(_registry())
        result = c.compile(
            g, skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities={"asr", "llm"},
        )
        assert result.graph is g
        assert result.graph.content_hash == g.content_hash


# ---------------------------------------------------------------------- #
# 编译产物可序列化（供发布 / 审计）
# ---------------------------------------------------------------------- #


class TestCompilationSerializable:
    def test_to_dict(self) -> None:
        c = WorkflowCompiler(_registry())
        result = c.compile(
            _voiceover_graph(),
            skill_id="topos.voiceover_polish",
            project_id="p", timeline_id="t",
            available_capabilities={"asr", "llm"},
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["invocation"]["source"] == "workflow"
        assert "graph" in d
        assert "diagnostics" in d


# ---------------------------------------------------------------------- #
# target_duration 流转（AUD-23210）
# ---------------------------------------------------------------------- #


def _planner_graph(target_duration: int | None) -> WorkflowGraph:
    """带（或不带）planner target_duration 声明的最小可编译 graph。"""
    planner_params = (
        {"target_duration": target_duration} if target_duration is not None else {}
    )
    return WorkflowGraph(
        workflow_id="wf-td",
        name="target duration",
        nodes=(
            WorkflowNode(
                node_id="input",
                spec_id="topos.media.selected_assets",
                params={"media_kind": "video"},
            ),
            WorkflowNode(
                node_id="rough_cut",
                spec_id="topos.agent.rough_cut_plan",
                params=planner_params,
            ),
        ),
        edges=(
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="rough_cut", port="assets"),
            ),
        ),
        policy=WorkflowPolicy(),
    )


def _compile_td(graph: WorkflowGraph, **extra: object):
    compiler = WorkflowCompiler(_registry())
    return compiler.compile(
        graph,
        skill_id="topos.voiceover_polish",
        project_id="p", timeline_id="t",
        selected_asset_ids=("a1",),
        available_capabilities={"llm"},
        **extra,
    )


class TestTargetDurationFlow:
    def test_node_declared_duration_flows_to_invocation(self) -> None:
        """AUD-23210：planner 节点声明的 target_duration 必须流入运行时。"""
        result = _compile_td(_planner_graph(60))
        assert result.success
        assert result.invocation.inputs["target_duration"] == 60

    def test_different_template_declarations_differ(self) -> None:
        """口播精剪（60）与产品粗剪（90）的声明不得被同一个硬编码值覆盖。"""
        r60 = _compile_td(_planner_graph(60))
        r90 = _compile_td(_planner_graph(90))
        assert r60.invocation.inputs["target_duration"] == 60
        assert r90.invocation.inputs["target_duration"] == 90

    def test_undeclared_falls_back_to_legacy_default(self) -> None:
        """graph 未声明时退回 720（与 legacy_adapters 默认一致）。"""
        result = _compile_td(_planner_graph(None))
        assert result.invocation.inputs["target_duration"] == 720

    def test_explicit_kwarg_takes_precedence(self) -> None:
        """显式 compile 参数优先于 graph 节点声明。"""
        result = _compile_td(_planner_graph(60), target_duration=45)
        assert result.invocation.inputs["target_duration"] == 45

    def test_official_template_durations_flow(self) -> None:
        """官方模板（口播精剪 60 / 产品粗剪 90）编译值与声明一致。"""
        from src.features.ai_workflow.core.template_registry import (
            WorkflowTemplateRegistry,
            register_official_templates,
        )
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        compiler = WorkflowCompiler(_registry())
        for template_id, expected in (
            ("wf-talking-head-fine-cut", 60),
            ("wf-product-rough-cut", 90),
        ):
            entry = tr.require(template_id)
            graph = tr.instantiate(template_id)
            result = compiler.compile(
                graph, skill_id=entry.skill_id,
                project_id="p", timeline_id="t",
                selected_asset_ids=("a1",),
                available_capabilities={"ffmpeg", "asr", "scene_cut",
                                        "audio_features", "llm"},
            )
            assert result.success, template_id
            assert result.invocation.inputs["target_duration"] == expected, (
                f"{template_id}: 模板声明的 target_duration 未流入运行时"
            )
