"""P8.7 测试：AI Workflow 画布集成验收（plan §7 / §8 / §10.7 / §1.4）。

> AI Workflow 作为高级入口，通过同一 :class:`HarnessRuntime` 执行（plan §10.7）。

P8（AI Workflow 主页面/Dock + 节点画布）的端到端验收：

- ``WorkflowCanvasAdapter`` 满足 :class:`GraphUiProtocol`。
- 加载 auto_caption 官方模板 → 画布渲染节点/连线。
- 画布**不引入** moderngl / composite.core（所有 ui/*.py 源码扫描）。
- 视觉 token 来自 :class:`DEFAULT_VISUAL_TOKENS`（不硬编码）。
- ``AiWorkflowModule`` 注册的 :class:`PanelDescriptor` 字段正确。
- WorkflowPanel 加载模板后画布非空。
- 画布缩放/平移/选中交互工作。
- **红线**：画布不直接写 timeline（adapter/panel/view 无 CommandStack 调用）。

本文件是 P8 的红线 gate。
"""
from __future__ import annotations

import os
import glob

import pytest


# ---------------------------------------------------------------------- #
# 全量源码红线扫描（所有 ui/*.py + module.py）
# ---------------------------------------------------------------------- #


def _ai_workflow_ui_sources() -> list:
    """收集所有 ai_workflow ui + module 源码文件路径。"""
    base = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "src", "features", "ai_workflow",
    )
    patterns = [
        os.path.join(base, "ui", "*.py"),
        os.path.join(base, "module.py"),
    ]
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    return [f for f in files if not f.endswith("__init__.py")]


def test_no_ui_file_imports_moderngl() -> None:
    """红线（plan §1.4.1 / §0）：所有 ai_workflow ui 源码不含 moderngl。"""
    for path in _ai_workflow_ui_sources():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "import moderngl" not in source, (
            f"{path} imports moderngl (forbidden in AI Workflow UI)"
        )


def test_no_ui_file_imports_composite_core() -> None:
    """红线（plan §1.4.1）：ai_workflow ui 不依赖 composite.core（GPU 渲染栈）。"""
    for path in _ai_workflow_ui_sources():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "from src.features.composite.core" not in source, (
            f"{path} imports composite.core (forbidden; AI Workflow must not "
            "reuse Composite GPU NodeGraph)"
        )


def test_no_ui_file_references_texture_result() -> None:
    """红线（plan §1.4.1）：ai_workflow ui 不传 GPU texture。"""
    for path in _ai_workflow_ui_sources():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "TextureResult" not in source, (
            f"{path} references TextureResult (forbidden; AI Workflow ports "
            "carry artifact_type strings, not GPU textures)"
        )


def test_no_ui_file_writes_timeline_directly() -> None:
    """红线（plan §1.4.3）：画布不直接写 timeline。"""
    forbidden = ("CommandStack", "commit_macro", "apply_edit_plan",
                 "timeline_write")
    for path in _ai_workflow_ui_sources():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        for token in forbidden:
            assert token not in source, (
                f"{path} references {token!r} (forbidden; AI Workflow canvas "
                "must not write timeline directly — go via WorkflowCompiler → "
                "HarnessRuntime → EditPlan → Approval)"
            )


# ---------------------------------------------------------------------- #
# GraphUiProtocol 满足
# ---------------------------------------------------------------------- #


def test_adapter_satisfies_protocol(qapp) -> None:
    """红线（plan §10.7.5）：WorkflowCanvasAdapter 满足 GraphUiProtocol。"""
    from src.shared.node_canvas.graph_ui_protocol import GraphUiProtocol
    from src.features.ai_workflow.ui.workflow_canvas_adapter import (
        WorkflowCanvasAdapter,
    )
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    reg = NodeSpecRegistry()
    register_official_specs(reg)
    adapter = WorkflowCanvasAdapter(registry=reg)
    assert isinstance(adapter, GraphUiProtocol)
    adapter.view.deleteLater()


# ---------------------------------------------------------------------- #
# 端到端：模板加载 → 画布渲染
# ---------------------------------------------------------------------- #


@pytest.fixture
def panel(qapp):
    from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    from src.features.ai_workflow.core.template_registry import (
        WorkflowTemplateRegistry, register_official_templates,
    )
    node_reg = NodeSpecRegistry()
    register_official_specs(node_reg)
    tpl_reg = WorkflowTemplateRegistry()
    register_official_templates(tpl_reg)
    p = WorkflowPanel(registry=node_reg, template_registry=tpl_reg)
    yield p
    p.deleteLater()


def test_load_auto_caption_template_renders_canvas(panel) -> None:
    """端到端：加载 auto_caption 模板 → 画布渲染节点/连线。"""
    panel.load_template("wf-auto-caption")
    # 应有多个节点
    assert panel.adapter.view.node_count > 0
    # 至少 1 条连线
    assert panel.adapter.view.connection_count > 0


def test_canvas_status_color_update(panel) -> None:
    """``set_node_status`` 改变节点状态色（运行时事件驱动）。"""
    panel.load_template("wf-auto-caption")
    # 取第一个节点
    node_id = panel.adapter.selected_node_ids()
    if not node_id:
        # 直接取画布第一个
        from src.features.ai_workflow.ui.workflow_canvas_view import (
            WorkflowCanvasView,
        )
        node_id = tuple(panel.adapter.view._node_items.keys())
    if node_id:
        nid = node_id[0]
        panel.adapter.set_node_status(nid, "running")
        item = panel.adapter.view.get_node_item(nid)
        assert item.status == "running"


def test_canvas_zoom_interaction(panel) -> None:
    """缩放交互。"""
    panel.adapter.zoom_to(2.0)
    assert panel.adapter.view.current_zoom == pytest.approx(2.0)
    panel.adapter.zoom_to(0.5)
    assert panel.adapter.view.current_zoom == pytest.approx(0.5)


def test_canvas_selection_interaction(panel) -> None:
    """选中交互。"""
    panel.load_template("wf-auto-caption")
    # 取一个节点选中
    node_ids = tuple(panel.adapter.view._node_items.keys())
    if node_ids:
        nid = node_ids[0]
        item = panel.adapter.view.get_node_item(nid)
        item.setSelected(True)
        assert nid in panel.adapter.selected_node_ids()
        panel.adapter.view.clear_selection()
        assert panel.adapter.selected_node_ids() == ()


def test_visual_tokens_single_source() -> None:
    """红线：视觉 token 来自 DEFAULT_VISUAL_TOKENS（不硬编码）。"""
    from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS
    from src.features.ai_workflow.ui.workflow_canvas_adapter import (
        WorkflowCanvasAdapter,
    )
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    reg = NodeSpecRegistry()
    register_official_specs(reg)
    # 默认 adapter 用 DEFAULT_VISUAL_TOKENS
    adapter = WorkflowCanvasAdapter(registry=reg)
    assert adapter._tokens is DEFAULT_VISUAL_TOKENS


def test_panel_descriptor_correct(qapp) -> None:
    """``AiWorkflowModule`` 注册的 PanelDescriptor 字段正确。"""
    from src.features.ai_workflow.module import AiWorkflowModule

    class _FakeReg:
        def __init__(self):
            self.registered = []
        def register(self, d):
            self.registered.append(d)

    m = AiWorkflowModule()
    fake = _FakeReg()
    m.register_panels(fake)
    # AI Workflow 改走右键弹窗，不再注册 dock descriptor
    assert len(fake.registered) == 0


def test_full_round_trip_clear_and_reload(panel) -> None:
    """清空后重新加载模板（验证 clear_all 不留残余）。"""
    panel.load_template("wf-auto-caption")
    n1 = panel.adapter.view.node_count
    assert n1 > 0
    panel.clear_canvas()
    assert panel.adapter.view.node_count == 0
    assert panel.adapter.view.connection_count == 0
    # 重新加载
    panel.load_template("wf-auto-caption")
    assert panel.adapter.view.node_count == n1
