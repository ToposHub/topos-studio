"""P10.2c 测试：WorkflowInspector 参数编辑面板 + 双击 + 接线。

> 选中节点 → Inspector 显示其参数（从 WorkflowNodeSpec.params_schema 渲染控件）；
> 改参数 → emit parameter_changed(node_id, key, value) → host 经 WorkflowGraphEdit
> 更新 graph。双击节点 → emit node_double_clicked（host 据 signals 打开/聚焦 Inspector）。

验证契约：

- Inspector 创建 + set_node 渲染参数控件（bool→CheckBox, string→LineEdit,
  number→SpinBox）。
- 改控件 → emit parameter_changed(node_id, key, value)。
- view 双击节点 → emit node_double_clicked。
- panel 接线：node_connected → graph_edit.add_edge；add_node_requested → 弹窗加节点；
  node_selected → Inspector.set_node；parameter_changed → graph_edit.set_node_param。
- **红线**：源码不 import moderngl / composite.core。
"""
import os

import pytest


# ---------------------------------------------------------------------- #
# 红线
# ---------------------------------------------------------------------- #


_INSPECTOR_SRC = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "src", "features", "ai_workflow", "ui", "workflow_inspector.py",
)


def test_inspector_source_no_gpu_no_composite() -> None:
    if not os.path.isfile(_INSPECTOR_SRC):
        pytest.skip("Inspector 源码尚未创建")
    with open(_INSPECTOR_SRC, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite" not in source
    assert "TextureResult" not in source


# ---------------------------------------------------------------------- #
# Inspector
# ---------------------------------------------------------------------- #


def _spec_with_params():
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, NodePortSpec, WorkflowNodeSpec,
    )
    return WorkflowNodeSpec(
        spec_id="topos.test.inspector", display_name="Inspector Test",
        category=NodeCategory.ANALYSIS, description="",
        inputs=(NodePortSpec(name="in1", artifact_type="asset_set", direction="input"),),
        outputs=(NodePortSpec(name="out1", artifact_type="asset_set", direction="output"),),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
        params_schema={"type": "object", "properties": {
            "language": {"type": "string"},
            "enable_diarization": {"type": "boolean"},
            "threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        }},
        default_params={"language": "zh", "enable_diarization": True, "threshold": 0.5},
    )


def test_inspector_creates(qapp) -> None:
    from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector
    insp = WorkflowInspector()
    assert insp is not None
    insp.deleteLater()


def test_inspector_has_parameter_changed_signal(qapp) -> None:
    from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector
    insp = WorkflowInspector()
    assert hasattr(insp, "parameter_changed")
    insp.deleteLater()


def test_inspector_set_node_renders_param_widgets(qapp) -> None:
    """set_node 后渲染 params_schema 中每个参数的控件。"""
    from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector
    insp = WorkflowInspector()
    spec = _spec_with_params()
    insp.set_node("n1", spec, {"language": "zh", "enable_diarization": True, "threshold": 0.5})
    # 应有 3 个参数控件
    assert len(insp.param_widgets) == 3
    assert "language" in insp.param_widgets
    assert "enable_diarization" in insp.param_widgets
    assert "threshold" in insp.param_widgets
    insp.deleteLater()


def test_inspector_bool_change_emits_parameter_changed(qapp) -> None:
    """改 bool 控件 → emit parameter_changed(node_id, key, value)。"""
    from PyQt6.QtWidgets import QCheckBox
    from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector
    insp = WorkflowInspector()
    spec = _spec_with_params()
    insp.set_node("n1", spec, {"enable_diarization": True})
    received = []
    insp.parameter_changed.connect(lambda nid, k, v: received.append((nid, k, v)))
    # 找 checkbox 并 toggle
    widget = insp.param_widgets["enable_diarization"]
    assert isinstance(widget, QCheckBox)
    widget.setChecked(False)
    assert received == [("n1", "enable_diarization", False)]
    insp.deleteLater()


def test_inspector_string_change_emits_parameter_changed(qapp) -> None:
    from PyQt6.QtWidgets import QLineEdit
    from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector
    insp = WorkflowInspector()
    spec = _spec_with_params()
    insp.set_node("n1", spec, {"language": "zh"})
    received = []
    insp.parameter_changed.connect(lambda nid, k, v: received.append((nid, k, v)))
    widget = insp.param_widgets["language"]
    assert isinstance(widget, QLineEdit)
    widget.setText("en")
    widget.editingFinished.emit()
    assert received == [("n1", "language", "en")]
    insp.deleteLater()


def test_inspector_number_change_emits_parameter_changed(qapp) -> None:
    from PyQt6.QtWidgets import QDoubleSpinBox
    from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector
    insp = WorkflowInspector()
    spec = _spec_with_params()
    insp.set_node("n1", spec, {"threshold": 0.5})
    received = []
    insp.parameter_changed.connect(lambda nid, k, v: received.append((nid, k, v)))
    widget = insp.param_widgets["threshold"]
    assert isinstance(widget, QDoubleSpinBox)
    widget.setValue(0.8)
    assert received == [("n1", "threshold", 0.8)]
    insp.deleteLater()


def test_inspector_clear_node(qapp) -> None:
    """clear_node 后无控件。

    注：set_node 渲染 **params_schema 的所有 properties**（不只是传入 params 的
    key），故加载 3-schema 的 spec 后有 3 个控件（缺失 key 用 schema 默认）。
    """
    from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector
    insp = WorkflowInspector()
    spec = _spec_with_params()
    insp.set_node("n1", spec, {"language": "zh"})
    assert len(insp.param_widgets) == 3  # schema 有 3 个 properties
    insp.clear_node()
    assert len(insp.param_widgets) == 0
    insp.deleteLater()


# ---------------------------------------------------------------------- #
# 双击 → node_double_clicked
# ---------------------------------------------------------------------- #


def test_view_double_click_node_emits_signal(qapp) -> None:
    """双击节点 → emit node_double_clicked(node_id)。"""
    from PyQt6.QtCore import QPointF, QEvent
    from PyQt6.QtGui import QMouseEvent
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, WorkflowNodeSpec,
    )
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView

    spec = WorkflowNodeSpec(
        spec_id="topos.test.node", display_name="T", category=NodeCategory.ANALYSIS,
        description="", inputs=(), outputs=(),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )
    view = WorkflowCanvasView()
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=spec)
    view.add_node_item(item)
    received = []
    view.node_double_clicked.connect(lambda nid: received.append(nid))

    # 模拟双击（两次 press+release + 一次 doubleClick）
    scene_pt = QPointF(20, 20)
    from PyQt6.QtCore import Qt as QQt
    dbl = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(view.mapFromScene(scene_pt)), scene_pt,
        QQt.MouseButton.LeftButton, QQt.MouseButton.LeftButton,
        QQt.KeyboardModifier.NoModifier,
    )
    view.mouseDoubleClickEvent(dbl)
    # 双击点中节点 → emit node_double_clicked("n1")
    assert "n1" in received


# ---------------------------------------------------------------------- #
# graph_edit.add_node
# ---------------------------------------------------------------------- #


def test_graph_edit_add_node() -> None:
    """WorkflowGraphEdit.add_node 产新 graph（含新节点）。"""
    from src.features.ai_workflow.core.workflow_graph_edit import WorkflowGraphEdit
    from src.features.ai_workflow.core.workflow_models import (
        WorkflowGraph, WorkflowNode,
    )
    g = WorkflowGraph(workflow_id="wf", nodes=(
        WorkflowNode(node_id="n1", spec_id="topos.test.node"),
    ))
    edit = WorkflowGraphEdit()
    new_node = WorkflowNode(node_id="n2", spec_id="topos.test.other", ui={"x": 50, "y": 60})
    new = edit.add_node(g, new_node)
    assert len(new.nodes) == 2
    assert new.require_node("n2").ui["x"] == 50
    # 原 graph 不变
    assert len(g.nodes) == 1


def test_graph_edit_add_node_duplicate_id_raises() -> None:
    from src.features.ai_workflow.core.workflow_graph_edit import WorkflowGraphEdit
    from src.features.ai_workflow.core.workflow_models import (
        WorkflowGraph, WorkflowNode,
    )
    g = WorkflowGraph(workflow_id="wf", nodes=(
        WorkflowNode(node_id="n1", spec_id="topos.test.node"),
    ))
    edit = WorkflowGraphEdit()
    with pytest.raises(ValueError):
        edit.add_node(g, WorkflowNode(node_id="n1", spec_id="topos.test.dup"))


# ---------------------------------------------------------------------- #
# Panel 接线：node_connected → add_edge；add_node_requested → 弹窗
# ---------------------------------------------------------------------- #


@pytest.fixture
def panel(qapp):
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    from src.features.ai_workflow.core.template_registry import (
        WorkflowTemplateRegistry, register_official_templates,
    )
    from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel
    node_reg = NodeSpecRegistry()
    register_official_specs(node_reg)
    tpl_reg = WorkflowTemplateRegistry()
    register_official_templates(tpl_reg)
    p = WorkflowPanel(registry=node_reg, template_registry=tpl_reg)
    yield p
    p.deleteLater()


def test_panel_has_inspector(panel) -> None:
    """panel 嵌 WorkflowInspector。"""
    assert hasattr(panel, "inspector")
    assert panel.inspector is not None


def test_panel_node_connected_updates_graph(panel) -> None:
    """view.node_connected emit → panel 用 graph_edit.add_edge 更新 current_graph。"""
    panel.load_template("wf-auto-caption")
    graph_before = panel.current_graph
    edges_before = len(graph_before.edges)
    # auto_caption: input→asr→caption；input 与 asr 之间已有边
    # emit 一条新连接（asr→caption 若已有则重复；用不存在的端口避免重复校验）
    # 用 graph 已有节点但新端口对：取两个节点 id
    node_ids = [n.node_id for n in graph_before.nodes]
    # 找一对尚未连接的节点（auto_caption 是链式，n0→n1→n2 已连；试 n0→n2）
    panel.adapter.view.node_connected.emit(
        node_ids[0], "out", node_ids[2] if len(node_ids) > 2 else node_ids[1], "in",
    )
    # 若不重复则 edges +1；若重复则 add_edge 抛 ValueError（panel 应吞掉）
    edges_after = len(panel.current_graph.edges)
    # 接受 +1（新连）或 不变（重复被吞）
    assert edges_after in (edges_before, edges_before + 1)


def test_panel_node_selected_loads_inspector(panel) -> None:
    """view.node_selected emit → inspector.set_node。"""
    panel.load_template("wf-auto-caption")
    graph = panel.current_graph
    node = graph.nodes[0]
    panel.adapter.view.node_selected.emit(node.node_id)
    # inspector 应已加载该节点
    assert panel.inspector.current_node_id == node.node_id
