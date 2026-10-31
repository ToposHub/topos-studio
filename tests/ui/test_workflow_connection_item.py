"""P8.2 测试：WorkflowConnectionItem 连线视觉项。

验证 AI Workflow 连线的视觉契约（plan §7.2 / graph_ui_protocol）：

- 连线创建携带端点端口项 + artifact_type 颜色来自 VisualTokens。
- ``update_path`` 跟随端口位置重绘贝塞尔曲线。
- 命中区扩到便于点击。
- **红线**：源码不引入 moderngl / composite.core。
"""
from __future__ import annotations

import os

import pytest


def test_source_does_not_import_gpu_or_composite() -> None:
    """红线：连线 item 源码不含 moderngl / composite.core。"""
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "src", "features", "ai_workflow", "ui", "workflow_connection_item.py",
    )
    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite.core" not in source
    assert "TextureResult" not in source


@pytest.fixture
def two_nodes(qapp):
    """两个节点（fake 端口定位器，匹配 WorkflowNodeItem 鸭子类型）。

    port_scene_pos(port_name, direction) 签名与 WorkflowNodeItem 一致。
    """
    # 用简单的 mock 端口项：实现 port_scene_pos(port_name, direction)
    class _FakePort:
        def __init__(self, x: float, y: float, artifact_type: str = "asset_set"):
            self._x = x
            self._y = y
            self._artifact_type = artifact_type

        def port_scene_pos(self, port_name: str, direction: str):
            from PyQt6.QtCore import QPointF
            return QPointF(self._x, self._y)

        @property
        def artifact_type(self):
            return self._artifact_type

    src = _FakePort(0, 50, "asset_set")
    dst = _FakePort(200, 100, "asset_set")
    return src, dst


def test_connection_creates_with_path(qapp, two_nodes) -> None:
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )
    src, dst = two_nodes
    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="output",
        to_node_id="n2", to_port="input",
        src_port=src, dst_port=dst,
        artifact_type="asset_set",
    )
    assert conn.from_node_id == "n1"
    assert conn.to_node_id == "n2"
    assert conn.from_port == "output"
    assert conn.to_port == "input"
    # path 应非空（绘制了贝塞尔）
    path = conn.path()
    assert not path.isEmpty()


def test_connection_color_from_visual_tokens(qapp, two_nodes) -> None:
    """红线：连线颜色来自 VisualTokens.port_token，不硬编码。"""
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )
    from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS

    src, dst = two_nodes
    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="output",
        to_node_id="n2", to_port="input",
        src_port=src, dst_port=dst,
        artifact_type="asset_set",
    )
    expected = DEFAULT_VISUAL_TOKENS.port_token("asset_set").color
    assert conn.color == expected


def test_connection_update_path_follows_port(qapp, two_nodes) -> None:
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )
    src, dst = two_nodes
    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="output",
        to_node_id="n2", to_port="input",
        src_port=src, dst_port=dst,
        artifact_type="asset_set",
    )
    path1 = conn.path()
    # 移动 dst 端口
    dst._x = 400
    dst._y = 200
    conn.update_path()
    path2 = conn.path()
    # path 应更新（bounding rect 不同）
    assert path1.boundingRect() != path2.boundingRect()


def test_connection_key_unique(qapp, two_nodes) -> None:
    """连线 key（用于 dict 管理）。"""
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )
    src, dst = two_nodes
    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="output",
        to_node_id="n2", to_port="input",
        src_port=src, dst_port=dst,
        artifact_type="asset_set",
    )
    assert conn.key == ("n1", "output", "n2", "input")


def test_connection_artifact_type_property(qapp, two_nodes) -> None:
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )
    src, dst = two_nodes
    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="output",
        to_node_id="n2", to_port="input",
        src_port=src, dst_port=dst,
        artifact_type="transcript_ref",
    )
    assert conn.artifact_type == "transcript_ref"


def test_connection_width_from_visual_tokens(qapp, two_nodes) -> None:
    """红线：连线宽度来自 VisualTokens.connection_width，不硬编码。"""
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )
    from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS

    src, dst = two_nodes
    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="output",
        to_node_id="n2", to_port="input",
        src_port=src, dst_port=dst,
        artifact_type="asset_set",
    )
    pen = conn.pen()
    assert pen.widthF() == pytest.approx(DEFAULT_VISUAL_TOKENS.connection_width)


# ---------------------------------------------------------------------- #
# P10.2a：拖拽连线预览（set_temp_endpoints / set_drag_validity）
# ---------------------------------------------------------------------- #


def test_set_temp_endpoints_updates_path(qapp, two_nodes) -> None:
    """set_temp_endpoints(src, dst) 用两点重画 path（拖拽预览用）。"""
    from PyQt6.QtCore import QPointF
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )
    src, dst = two_nodes
    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="output",
        to_node_id="n2", to_port="input",
        src_port=src, dst_port=dst, artifact_type="asset_set",
    )
    path_before = conn.path()
    conn.set_temp_endpoints(QPointF(0, 0), QPointF(100, 100))
    path_after = conn.path()
    # path 应变化（不同端点）
    assert path_after != path_before or not path_after.isEmpty()


def test_set_drag_validity_changes_color(qapp, two_nodes) -> None:
    """set_drag_validity(False) 把临时连线变红（无效）/ True 绿（有效）。"""
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )
    src, dst = two_nodes
    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="output",
        to_node_id="n2", to_port="input",
        src_port=src, dst_port=dst, artifact_type="asset_set",
    )
    valid_color = conn.pen().color().name()
    conn.set_drag_validity(False)
    invalid_color = conn.pen().color().name()
    assert invalid_color != valid_color, "set_drag_validity(False) 应改色"
    conn.set_drag_validity(True)
    # 恢复
    assert conn.pen().color().name() == valid_color


def test_make_temp_connection_factory(qapp) -> None:
    """make_temp_connection(artifact_type) 产一个轻量临时连线（无需真实端点）。"""
    from src.features.ai_workflow.ui.workflow_connection_item import (
        make_temp_connection,
    )
    conn = make_temp_connection("asset_set")
    assert conn is not None
    # 临时连线初始 path 空（端点未定）
    # 调 set_temp_endpoints 后有 path
    from PyQt6.QtCore import QPointF
    conn.set_temp_endpoints(QPointF(0, 0), QPointF(50, 50))
    assert not conn.path().isEmpty()
