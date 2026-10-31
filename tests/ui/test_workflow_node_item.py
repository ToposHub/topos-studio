"""P8.1 测试：WorkflowNodeItem 节点视觉项。

验证 AI Workflow 节点的视觉契约（plan §7.2 / graph_ui_protocol.py VisualTokens）：

- 节点创建携带 node_id / spec_id / category 色来自 VisualTokens（不硬编码）。
- 状态色条（``set_status``）反映 StatusColor 映射。
- 端口位置（``port_scene_pos``）返回合理 QPointF（给连线定位用）。
- 选中 glow 切换。
- **红线**：源码不 import moderngl / composite.core。

不依赖真实 GPU / OpenGL（纯 QGraphicsItem 自绘）。
"""
from __future__ import annotations

import os

import pytest


# ---------------------------------------------------------------------- #
# 源码红线扫描（在 import 前做，避免 import 副作用）
# ----------------------------------------------------------------------


def test_source_does_not_import_gpu_or_composite() -> None:
    """红线：节点 item 源码不含 moderngl / composite.core import。"""
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "src", "features", "ai_workflow", "ui", "workflow_node_item.py",
    )
    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite.core" not in source
    # 不应依赖 TextureResult / OpenGL context
    assert "TextureResult" not in source
    assert "QOpenGL" not in source


# ---------------------------------------------------------------------- #
# 节点视觉项测试（需 qapp）
# ----------------------------------------------------------------------


@pytest.fixture
def node_spec():
    """一个简单的 NodeSpec 测试用例。"""
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, NodePortSpec, WorkflowNodeSpec,
    )
    return WorkflowNodeSpec(
        spec_id="topos.test.node",
        display_name="Test Node",
        category=NodeCategory.ANALYSIS,
        description="test",
        inputs=(
            NodePortSpec(name="assets", artifact_type="asset_set", direction="input"),
            NodePortSpec(name="text", artifact_type="text_document", direction="input",
                         required=False),
        ),
        outputs=(
            NodePortSpec(name="result", artifact_type="analysis_ref", direction="output"),
        ),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )


def test_node_item_creates_with_correct_metadata(qapp, node_spec) -> None:
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    assert item.node_id == "n1"
    assert item.spec_id == "topos.test.node"
    assert item.category == "analysis"


def test_node_item_category_color_from_visual_tokens(qapp, node_spec) -> None:
    """红线：节点 header 色来自 VisualTokens，不硬编码。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    expected = DEFAULT_VISUAL_TOKENS.node_token("analysis").color
    assert item.header_color == expected


def test_node_item_set_status_changes_status_field(qapp, node_spec) -> None:
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    assert item.status == "idle"
    item.set_status("running")
    assert item.status == "running"
    item.set_status("succeeded")
    assert item.status == "succeeded"


def test_node_item_status_color_from_visual_tokens(qapp, node_spec) -> None:
    """红线：状态色来自 StatusColor，不硬编码。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    item.set_status("running")
    expected = DEFAULT_VISUAL_TOKENS.status_color("running")
    assert item.status_color == expected


def test_node_item_set_position(qapp, node_spec) -> None:
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    item.set_position(100.0, 200.0)
    assert item.pos().x() == pytest.approx(100.0)
    assert item.pos().y() == pytest.approx(200.0)


def test_node_item_port_scene_pos_input(qapp, node_spec) -> None:
    """端口位置返回合理 QPointF（给连线定位用）。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    item.set_position(0.0, 0.0)
    # input 端口在节点左侧
    pos = item.port_scene_pos("assets", "input")
    assert pos is not None
    # x 应在节点左边界附近（<= 节点宽度）
    assert pos.x() <= item.boundingRect().width()
    # output 端口在节点右侧
    pos_out = item.port_scene_pos("result", "output")
    assert pos_out is not None
    assert pos_out.x() >= 0


def test_node_item_port_scene_pos_unknown_returns_none(qapp, node_spec) -> None:
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    assert item.port_scene_pos("nonexistent", "input") is None


def test_node_item_selected_glow_toggle(qapp, node_spec) -> None:
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    assert item.has_glow is False
    item.set_selected_glow(True)
    assert item.has_glow is True
    item.set_selected_glow(False)
    assert item.has_glow is False


def test_node_item_respects_visual_tokens_layout(qapp, node_spec) -> None:
    """节点布局尺寸从 VisualTokens 读，不硬编码。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    # 宽度 >= visual_tokens.node_min_width
    assert item.boundingRect().width() >= DEFAULT_VISUAL_TOKENS.node_min_width
    # 高度 >= header + 至少一行端口
    assert item.boundingRect().height() >= DEFAULT_VISUAL_TOKENS.node_header_height


def test_node_item_port_artifact_types_accessible(qapp, node_spec) -> None:
    """节点暴露其端口 artifact_type（给连线颜色用）。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem

    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec,
    )
    input_types = item.input_artifact_types
    assert "assets" in input_types
    assert input_types["assets"] == "asset_set"
    output_types = item.output_artifact_types
    assert output_types["result"] == "analysis_ref"


def test_node_item_custom_visual_tokens(qapp, node_spec) -> None:
    """可注入自定义 VisualTokens（不强制用默认）。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.shared.node_canvas.graph_ui_protocol import (
        NodeVisualToken, VisualTokens,
    )

    custom_tokens = VisualTokens(
        node_tokens=(NodeVisualToken(category="analysis", color="#ff00ff", icon="x"),),
    )
    item = WorkflowNodeItem(
        node_id="n1", spec_id="topos.test.node",
        node_spec=node_spec, visual_tokens=custom_tokens,
    )
    assert item.header_color == "#ff00ff"


# ---------------------------------------------------------------------- #
# P10.1c：连线跟随（register_connection / unregister_connection / itemChange）
# ---------------------------------------------------------------------- #


def test_node_item_register_connection(qapp, node_spec) -> None:
    """register_connection 把连线加入节点的关联列表。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=node_spec)
    mock_conn = object()  # 鸭子类型；register 只存引用
    item.register_connection(mock_conn)
    assert mock_conn in item.connections


def test_node_item_unregister_connection(qapp, node_spec) -> None:
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=node_spec)
    mock_conn = object()
    item.register_connection(mock_conn)
    item.unregister_connection(mock_conn)
    assert mock_conn not in item.connections


def test_node_item_unregister_nonexistent_is_noop(qapp, node_spec) -> None:
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=node_spec)
    item.unregister_connection(object())  # 不抛


def test_node_item_position_change_calls_connection_update(qapp, node_spec) -> None:
    """itemChange：节点 setPos 后，注册的连线的 update_path 被调（连线跟随）。

    需把 item 加进 scene（itemChange 的 ItemPositionHasChanged 才会触发）。
    """
    from PyQt6.QtWidgets import QGraphicsScene
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem

    scene = QGraphicsScene()
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=node_spec)
    scene.addItem(item)

    # mock 连线：记录 update_path 调用次数
    class _MockConn:
        def __init__(self):
            self.update_count = 0
        def update_path(self):
            self.update_count += 1
    conn = _MockConn()
    item.register_connection(conn)
    assert conn.update_count == 0

    item.setPos(50.0, 60.0)  # 触发 ItemPositionHasChanged → itemChange
    assert conn.update_count >= 1, (
        f"setPos 后连线的 update_path 应被调；实际：{conn.update_count}"
    )
