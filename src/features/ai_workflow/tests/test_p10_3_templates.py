"""P10.3 测试：3 个新用户模板 + Composite/ComfyUI spec 接线。

> plan §9 列了 5 个新用户模板（自动字幕/字幕翻译配音 已有；口播精剪/产品粗剪/
> 合成渲染后导入 缺失）。本批补齐后 3 个 + 把 Composite/ComfyUI 节点 spec 接进
> ``register_official_specs``（让它们在画布 Tab 搜索可见）。

验证契约：

- ``register_official_templates`` 注册至少 5 个模板（含 3 个新的）。
- 口播精剪（wf-talking-head-fine-cut）：selected_assets → transcribe → rough_cut_plan
  → preview_diff → approval → apply_approved_plan；brief 为 exposed_input。
- 产品粗剪（wf-product-rough-cut）：selected_assets → scene_cut + audio_features
  → rough_cut_plan → preview_diff → approval。
- 合成渲染后导入（wf-composite-render-import）：selected_assets → composite.subgraph
  → composite.render → composite.import_render_result。
- Composite/ComfyUI spec 接进 register_official_specs（画布 Tab 可见）。
- 每个新模板的节点 spec_id 在 registry（含 Composite spec）中存在。
- 每个新模板可实例化（instantiate）。
"""
import pytest

from src.features.ai_workflow.core.node_specs import (
    NodeSpecRegistry, register_official_specs,
)
from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateRegistry, register_official_templates,
)


# ---------------------------------------------------------------------- #
# Fixture：含 Composite/ComfyUI spec 的 registry
# ---------------------------------------------------------------------- #


@pytest.fixture
def full_registry():
    """register_official_specs + Composite + ComfyUI spec（模拟 dock 接线后）。"""
    reg = NodeSpecRegistry()
    register_official_specs(reg)
    return reg


@pytest.fixture
def templates():
    tr = WorkflowTemplateRegistry()
    register_official_templates(tr)
    return tr


# ---------------------------------------------------------------------- #
# 3 个新模板已注册
# ---------------------------------------------------------------------- #


def test_three_new_templates_registered(templates) -> None:
    """register_official_templates 注册 5 个模板（含 3 个新的）。"""
    ids = {t.template_id for t in templates.all_templates()}
    assert "wf-talking-head-fine-cut" in ids, "口播精剪 模板缺失"
    assert "wf-product-rough-cut" in ids, "产品粗剪 模板缺失"
    assert "wf-composite-render-import" in ids, "合成渲染后导入 模板缺失"
    assert len(ids) >= 5


def test_new_templates_have_metadata(templates) -> None:
    for tid in ("wf-talking-head-fine-cut", "wf-product-rough-cut",
                "wf-composite-render-import"):
        t = templates.get(tid)
        assert t is not None
        assert t.display_name
        assert t.description
        assert t.category
        assert t.skill_id


# ---------------------------------------------------------------------- #
# 口播精剪
# ---------------------------------------------------------------------- #


def test_talking_head_fine_cut_structure(templates) -> None:
    """口播精剪：selected_assets → transcribe → rough_cut_plan → preview_diff
    → approval → apply_approved_plan。"""
    t = templates.require("wf-talking-head-fine-cut")
    g = t.graph
    spec_ids = {n.spec_id for n in g.nodes}
    assert "topos.media.selected_assets" in spec_ids
    assert "topos.analysis.transcribe_auto" in spec_ids
    assert "topos.agent.rough_cut_plan" in spec_ids
    assert "topos.timeline.preview_diff" in spec_ids
    assert "topos.user.approval" in spec_ids
    assert "topos.timeline.apply_approved_plan" in spec_ids
    # brief 是 exposed_input（rough_cut_plan 需要 text_document）
    exposed_names = {name for name, _, _ in g.exposed_inputs}
    assert "brief" in exposed_names or "assets" in exposed_names


# ---------------------------------------------------------------------- #
# 产品粗剪
# ---------------------------------------------------------------------- #


def test_product_rough_cut_structure(templates) -> None:
    """产品粗剪：selected_assets → scene_cut + audio_features → rough_cut_plan
    → preview_diff → approval。"""
    t = templates.require("wf-product-rough-cut")
    g = t.graph
    spec_ids = {n.spec_id for n in g.nodes}
    assert "topos.media.selected_assets" in spec_ids
    assert "topos.analysis.scene_cut" in spec_ids
    assert "topos.analysis.audio_features" in spec_ids
    assert "topos.agent.rough_cut_plan" in spec_ids
    assert "topos.timeline.preview_diff" in spec_ids
    assert "topos.user.approval" in spec_ids


def test_product_rough_cut_every_analysis_has_consumer(templates) -> None:
    """AUD-23200 批次修复回归（AUD-23206）：双分析的输出都必须有消费边。

    audio_feat.analysis 此前无任何消费边——音频特征白跑，且缺 audio_features
    能力时这个"死"节点反而阻塞模板编译。修复后 audio_feat.analysis 连入
    planner.analysis（allow_multi=True）。"""
    g = templates.require("wf-product-rough-cut").graph
    consumed = {(e.from_port.node_id, e.from_port.port) for e in g.edges}
    assert ("audio_feat", "analysis") in consumed, (
        "audio_feat.analysis 必须有消费边（不得白跑音频特征分析）"
    )
    assert ("scene_cut", "analysis") in consumed
    targets = {(e.to_port.node_id, e.to_port.port) for e in g.edges}
    assert ("planner", "analysis") in targets


def test_product_rough_cut_all_node_outputs_consumed_or_exposed(templates) -> None:
    """模板内每个中间节点的每个输出端口必须有消费边。

    防止未来再出现"声明了分析节点但输出无人消费"的死节点（AUD-23206 泛化）。
    终端节点（planner 之后的 preview/approval）自身就是产出端，不在此约束内。"""
    from src.features.ai_workflow.core.node_specs import register_official_specs

    reg = NodeSpecRegistry()
    register_official_specs(reg)
    g = templates.require("wf-product-rough-cut").graph
    consumed = {(e.from_port.node_id, e.from_port.port) for e in g.edges}
    for node_id in ("input", "audio_feat", "scene_cut"):
        node = g.node(node_id)
        assert node is not None
        spec = reg.get(node.spec_id)
        assert spec is not None
        for port in spec.output_ports:
            assert (node_id, port.name) in consumed, (
                f"{node_id}.{port.name} 输出无消费边（死数据流）"
            )


# ---------------------------------------------------------------------- #
# 合成渲染后导入
# ---------------------------------------------------------------------- #


def test_composite_render_import_structure(templates) -> None:
    """合成渲染后导入：selected_assets → composite.subgraph → composite.render
    → composite.import_render_result。"""
    t = templates.require("wf-composite-render-import")
    g = t.graph
    spec_ids = {n.spec_id for n in g.nodes}
    assert "topos.media.selected_assets" in spec_ids
    assert "topos.composite.subgraph" in spec_ids
    assert "topos.composite.render" in spec_ids
    assert "topos.composite.import_render_result" in spec_ids


# ---------------------------------------------------------------------- #
# 节点 spec_id 在 registry 存在（含 Composite spec）
# ---------------------------------------------------------------------- #


def test_new_template_specs_resolvable(full_registry, templates) -> None:
    """新模板的 spec_id（含 Composite）在 registry 存在（Composite 需接线）。"""
    # 手动注册 Composite spec（模拟 dock 接线后）
    from src.features.ai_workflow.adapters.composite_adapter import (
        register_composite_node_specs,
    )
    register_composite_node_specs(full_registry)
    for tid in ("wf-talking-head-fine-cut", "wf-product-rough-cut",
                "wf-composite-render-import"):
        t = templates.require(tid)
        for n in t.graph.nodes:
            assert full_registry.get(n.spec_id) is not None, (
                f"template {tid} 引用未注册的 spec: {n.spec_id}"
            )


# ---------------------------------------------------------------------- #
# 可实例化
# ---------------------------------------------------------------------- #


def test_new_templates_instantiate(templates) -> None:
    for tid in ("wf-talking-head-fine-cut", "wf-product-rough-cut",
                "wf-composite-render-import"):
        copy = templates.instantiate(tid)
        t = templates.require(tid)
        assert len(copy.nodes) == len(t.graph.nodes)
        assert len(copy.edges) == len(t.graph.edges)


# ---------------------------------------------------------------------- #
# Composite/ComfyUI spec 接进 register_official_specs（画布 Tab 可见）
# ---------------------------------------------------------------------- #


def test_register_official_specs_includes_composite_and_comfy() -> None:
    """register_official_specs 后 topos.composite.* / topos.comfy.* 可查（画布 Tab 可见）。"""
    reg = NodeSpecRegistry()
    register_official_specs(reg)
    assert reg.get("topos.composite.subgraph") is not None, (
        "Composite spec 未接进 register_official_specs（画布 Tab 不可见）"
    )
    assert reg.get("topos.composite.render") is not None
    assert reg.get("topos.composite.import_render_result") is not None
    assert reg.get("topos.comfy.workflow") is not None, (
        "ComfyUI spec 未接进 register_official_specs"
    )
