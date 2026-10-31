"""官方模板可运行性回归测试。

背景（2026-08 修复）：P10.3 的 3 个模板（口播精剪 / 产品粗剪 / 合成渲染导入）
曾因「必填端口缺边」+「skill_id 无后端映射」编译即失败（MISSING_REQUIRED_INPUT /
WORKFLOW_MAPPING_FAILED），而既有测试只断言模板结构，不断言可运行性。本文件把
「所有注册模板在能力齐备时必须编译 runnable=True」固化为回归红线。

覆盖：

1. 全部官方模板编译成功且 target 可路由（workflow / editorial）。
2. 模板图结构关键边存在（planner.assets 连入）。
3. node_ids 管道：编译产物 → invocation_to_workflow_input → WorkflowInput.node_ids
   逐节点着色的数据源（P9）。
4. exposed_inputs 满足必填端口（composite_graph 只能由 exposed input 提供）。
"""
from __future__ import annotations

import pytest

from src.features.ai_workflow.core.node_specs import (
    NodeSpecRegistry,
    register_official_specs,
)
from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateRegistry,
    register_official_templates,
)
from src.features.ai_workflow.core.workflow_compiler import WorkflowCompiler
from src.features.ai_workflow.core.workflow_validator import (
    ValidationErrorCode,
    WorkflowValidator,
)


#: 覆盖全部官方 spec 能力需求的集合（宿主 provider 齐备时的形态）。
_ALL_CAPABILITIES = {
    "ffmpeg", "asr", "scene_cut", "ocr", "audio_features",
    "llm", "translate", "tts", "composite", "image_gen", "video_gen",
}


@pytest.fixture()
def registry() -> NodeSpecRegistry:
    reg = NodeSpecRegistry()
    register_official_specs(reg)
    return reg


@pytest.fixture()
def templates() -> WorkflowTemplateRegistry:
    treg = WorkflowTemplateRegistry()
    register_official_templates(treg)
    return treg


@pytest.fixture()
def compiler(registry: NodeSpecRegistry) -> WorkflowCompiler:
    return WorkflowCompiler(registry)


def _compile_template(
    compiler: WorkflowCompiler,
    templates: WorkflowTemplateRegistry,
    template_id: str,
):
    entry = templates.require(template_id)
    graph = templates.instantiate(template_id)
    return compiler.compile(
        graph,
        skill_id=entry.skill_id,
        project_id="proj-1",
        timeline_id="timeline-1",
        selected_asset_ids=("asset-1",),
        available_capabilities=set(_ALL_CAPABILITIES),
    )


class TestAllTemplatesRunnable:
    """所有注册模板在能力齐备时必须编译 runnable=True。"""

    def test_every_template_compiles_runnable(
        self, compiler, templates,
    ) -> None:
        assert templates.all_templates(), "no official templates registered"
        failures = []
        for entry in templates.all_templates():
            result = _compile_template(compiler, templates, entry.template_id)
            if not result.success:
                errors = [
                    d.get("code") for d in result.diagnostics
                    if d.get("severity") == "error"
                ]
                failures.append((entry.template_id, errors))
        assert not failures, f"templates failed to compile: {failures}"

    def test_targets_routable(self, compiler, templates) -> None:
        """skill_id 必须路由到真实执行目标（不许 legacy_unsupported）。"""
        for entry in templates.all_templates():
            result = _compile_template(compiler, templates, entry.template_id)
            assert result.compiled is not None
            assert result.compiled.target in ("workflow", "editorial"), (
                f"{entry.template_id}: unexpected target "
                f"{result.compiled.target!r}"
            )


class TestTalkingHeadTemplate:
    def test_planner_assets_edge_exists(self, templates) -> None:
        graph = templates.require("wf-talking-head-fine-cut").graph
        targets = {(e.to_port.node_id, e.to_port.port) for e in graph.edges}
        assert ("planner", "assets") in targets

    def test_routes_to_voiceover_polish_backend(
        self, compiler, templates,
    ) -> None:
        entry = templates.require("wf-talking-head-fine-cut")
        assert entry.skill_id == "topos.voiceover_polish"
        result = _compile_template(compiler, templates, entry.template_id)
        assert result.success
        assert result.compiled.target == "workflow"
        assert result.compiled.workflow_input.workflow_id.value == "voiceover_polish"


class TestProductRoughCutTemplate:
    def test_planner_assets_edge_exists(self, templates) -> None:
        graph = templates.require("wf-product-rough-cut").graph
        targets = {(e.to_port.node_id, e.to_port.port) for e in graph.edges}
        assert ("planner", "assets") in targets

    def test_routes_to_product_ad_backend(self, compiler, templates) -> None:
        entry = templates.require("wf-product-rough-cut")
        assert entry.skill_id == "topos.product_ad_30s"
        result = _compile_template(compiler, templates, entry.template_id)
        assert result.success
        assert result.compiled.target == "workflow"
        assert result.compiled.workflow_input.workflow_id.value == "product_ad_30s"


class TestCompositeTemplate:
    def test_routes_to_editorial(self, compiler, templates) -> None:
        entry = templates.require("wf-composite-render-import")
        result = _compile_template(compiler, templates, entry.template_id)
        assert result.success
        assert result.compiled.target == "editorial"


class TestValidatorExposedInputs:
    """exposed_inputs 绑定满足必填端口（composite_graph 场景）。"""

    def test_exposed_input_satisfies_required_port(self, registry, templates) -> None:
        validator = WorkflowValidator(registry)
        graph = templates.require("wf-composite-render-import").graph
        findings = validator.validate(
            graph, available_capabilities=set(_ALL_CAPABILITIES),
        )
        errors = [f for f in findings if f.severity == "error"]
        assert not errors, [f.message for f in errors]

    def test_unconnected_required_port_still_errors(self, registry) -> None:
        """无边且无 exposed 绑定的必填端口仍然必须报 MISSING_REQUIRED_INPUT。"""
        validator = WorkflowValidator(registry)
        from src.features.ai_workflow.core.workflow_models import (
            WorkflowGraph,
            WorkflowNode,
        )
        graph = WorkflowGraph(
            workflow_id="wf-broken",
            name="broken",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),
                # transcribe_auto 的 assets 端口必填且无任何连入/暴露。
                WorkflowNode(node_id="asr", spec_id="topos.analysis.transcribe_auto"),
            ),
        )
        findings = validator.validate(graph, available_capabilities={"ffmpeg", "asr"})
        codes = {f.code for f in findings if f.severity == "error"}
        assert ValidationErrorCode.MISSING_REQUIRED_INPUT in codes


class TestNodeIdsPipeline:
    """编译 → WorkflowInput.node_ids 管道（fake 后端逐节点着色数据源）。"""

    def test_compile_writes_ordered_node_ids(self, compiler, templates) -> None:
        result = _compile_template(
            compiler, templates, "wf-talking-head-fine-cut",
        )
        assert result.success
        ids = result.invocation.inputs.get("workflow_node_ids")
        assert tuple(ids) == (
            "input", "asr", "planner", "preview", "timeline_in",
            "approval", "apply",
        )

    def test_invocation_to_workflow_input_carries_node_ids(
        self, compiler, templates,
    ) -> None:
        from src.features.ai_harness.legacy_adapters import (
            invocation_to_workflow_input,
        )

        result = _compile_template(
            compiler, templates, "wf-talking-head-fine-cut",
        )
        wf_input = invocation_to_workflow_input(result.invocation)
        assert wf_input.node_ids == (
            "input", "asr", "planner", "preview", "timeline_in",
            "approval", "apply",
        )

    def test_chat_entry_has_empty_node_ids(self, compiler, templates) -> None:
        """无 graph 的调用方（聊天入口）node_ids 为空 → 退化单节点。"""
        from src.features.ai_harness.invocation_models import Invocation
        from src.features.ai_harness.legacy_adapters import (
            invocation_to_workflow_input,
        )

        inv = Invocation(
            invocation_id="inv-chat",
            skill_id="topos.voiceover_polish",
            user_text="帮我精剪",
        )
        wf_input = invocation_to_workflow_input(inv)
        assert wf_input.node_ids == ()
