"""P5.4 测试：官方模板注册表（plan §3.2 / §9.1 / §9.2）。

plan §10.7.4："先交付官方模板的查看、复制、参数编辑、运行和 debug"。

验证 :class:`WorkflowTemplateRegistry` 的核心契约：

- plan §9.1 / §9.2 的官方模板（自动字幕 / 字幕翻译配音）注册为
  :class:`WorkflowGraph`。
- 每个模板：查看（to_dict）/ 复制（instantiate 为可编辑副本）/ 参数编辑（默认参数）。
- 模板经 :class:`WorkflowCompiler` 可编译为 Invocation（可运行）。
- 模板的节点 spec 全部在 :class:`NodeSpecRegistry`（P5.2 注册的 16 个 M4）。

红线（plan §10.7.4 / §1.4）：

- 官方模板不可编辑（frozen）；用户"复制"得到 local_dev 副本才可改。
- 模板的 apply_approved_plan 节点必须连 approval_token（§6.4）。
"""
from __future__ import annotations


from src.features.ai_workflow.core.node_specs import (
    NodeSpecRegistry,
    register_official_specs,
)
from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateRegistry,
    register_official_templates,
)
from src.features.ai_workflow.core.workflow_compiler import WorkflowCompiler


def _registry_with_specs() -> NodeSpecRegistry:
    r = NodeSpecRegistry()
    register_official_specs(r)
    return r


# ---------------------------------------------------------------------- #
# 官方模板注册
# ---------------------------------------------------------------------- #


class TestOfficialTemplates:
    def test_official_templates_registered(self) -> None:
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        ids = {t.template_id for t in tr.all_templates()}
        # plan §9.1 自动字幕 + §9.2 字幕翻译配音
        assert "wf-auto-caption" in ids
        assert "wf-subtitle-localization" in ids

    def test_each_template_has_metadata(self) -> None:
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        for t in tr.all_templates():
            assert t.template_id
            assert t.display_name
            assert t.description
            assert t.category  # input/edit/generation/...
            assert t.skill_id  # 对应的 skill_id（用于编译）

    def test_get_template_by_id(self) -> None:
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        t = tr.get("wf-auto-caption")
        assert t is not None
        assert t.skill_id == "topos.auto_caption"


# ---------------------------------------------------------------------- #
# 模板节点 spec 全部在 registry
# ---------------------------------------------------------------------- #


class TestTemplateNodeSpecs:
    def test_template_nodes_use_registered_specs(self) -> None:
        reg = _registry_with_specs()
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        for t in tr.all_templates():
            for n in t.graph.nodes:
                assert reg.get(n.spec_id) is not None, (
                    f"template {t.template_id} node {n.node_id} "
                    f"uses unknown spec {n.spec_id!r}"
                )


# ---------------------------------------------------------------------- #
# 模板可编译为 Invocation
# ---------------------------------------------------------------------- #


class TestTemplateCompilable:
    def test_auto_caption_template_compiles(self) -> None:
        reg = _registry_with_specs()
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        compiler = WorkflowCompiler(reg)
        result = compiler.compile(
            t.graph,
            skill_id=t.skill_id,
            project_id="proj-1",
            timeline_id="tl-1",
            selected_asset_ids=("a1",),
            available_capabilities={"asr"},
        )
        assert result.success
        assert result.invocation.source == "workflow"


# ---------------------------------------------------------------------- #
# 复制为可编辑副本（local_dev）
# ---------------------------------------------------------------------- #


class TestInstantiateCopy:
    def test_instantiate_creates_editable_copy(self) -> None:
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        copy = tr.instantiate(t.template_id, new_workflow_id="wf-my-caption")
        # 副本是不同的 workflow_id
        assert copy.workflow_id == "wf-my-caption"
        # 副本可编辑（结构与模板一致，但内容 hash 因 workflow_id 变而不同）
        assert copy.content_hash != t.graph.content_hash
        # 节点结构一致
        assert len(copy.nodes) == len(t.graph.nodes)

    def test_instantiate_preserves_structure(self) -> None:
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        copy = tr.instantiate(t.template_id)
        assert {n.spec_id for n in copy.nodes} == {n.spec_id for n in t.graph.nodes}
        assert len(copy.edges) == len(t.graph.edges)

    def test_instantiate_with_param_overrides(self) -> None:
        """复制时可覆盖节点参数（plan §10.7.4 '参数编辑'）。"""
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        copy = tr.instantiate(
            t.template_id,
            param_overrides={"caption": {"track_name": "我的字幕"}},
        )
        cap = copy.node("caption")
        assert cap is not None
        assert cap.params.get("track_name") == "我的字幕"


# ---------------------------------------------------------------------- #
# approval_token 红线（含 apply 节点的模板）
# ---------------------------------------------------------------------- #


class TestTemplateApprovalGate:
    def test_full_pipeline_template_has_approval(self) -> None:
        """含 apply_approved_plan 的模板必须连 approval_token。"""
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        reg = _registry_with_specs()
        from src.features.ai_workflow.core.workflow_validator import (
            ValidationErrorCode, WorkflowValidator,
        )
        v = WorkflowValidator(reg)
        for t in tr.all_templates():
            has_apply = any(
                n.spec_id == "topos.timeline.apply_approved_plan" for n in t.graph.nodes
            )
            if not has_apply:
                continue
            findings = v.validate(t.graph, available_capabilities={
                "asr", "llm", "tts", "translate",
            })
            codes = [f.code for f in findings if f.severity == "error"]
            assert ValidationErrorCode.MISSING_APPROVAL_TOKEN not in codes, (
                f"template {t.template_id} apply node missing approval_token"
            )


# ---------------------------------------------------------------------- #
# 序列化（查看）
# ---------------------------------------------------------------------- #


class TestTemplateSerialization:
    def test_template_entry_to_dict(self) -> None:
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        t = tr.require("wf-auto-caption")
        d = t.to_dict()
        assert d["template_id"] == "wf-auto-caption"
        assert d["skill_id"] == "topos.auto_caption"
        assert "graph" in d
        assert d["graph"]["schema"] == "topos.workflow/1"
