"""P8.4 测试：WorkflowCanvasAdapter（NodeCanvasBase 子类，实现 GraphUiProtocol）。

> AI Workflow 的 ``WorkflowCanvasAdapter`` 与 Composite 的 ``NodeEditorWidget``
> 都应满足本 Protocol（graph_ui_protocol.py:193-199）。

验证 :class:`WorkflowCanvasAdapter` 的核心契约：

- 满足 :class:`GraphUiProtocol`（``isinstance`` runtime_checkable）。
- ``build_from_graph`` 把 :class:`WorkflowGraph` 渲染为画布节点/连线。
- ``add_node_visual`` / ``remove_node_visual`` 增删。
- ``add_connection_visual`` / ``remove_connection_visual`` 增删。
- ``set_node_status`` 改变节点状态色。
- ``selected_node_ids`` / ``zoom_to`` 委托 view。
- ``clear_all`` 清空。
- **红线**：源码不引入 moderngl / composite.core。
"""
from __future__ import annotations

import os

import pytest


def test_source_does_not_import_gpu_or_composite() -> None:
    """红线：adapter 源码不含 moderngl / composite.core。"""
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src", "features", "ai_workflow", "ui", "workflow_canvas_adapter.py",
    )
    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite.core" not in source
    assert "TextureResult" not in source


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


@pytest.fixture
def registry():
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    reg = NodeSpecRegistry()
    register_official_specs(reg)
    return reg


@pytest.fixture
def adapter(qapp, registry):
    from src.features.ai_workflow.ui.workflow_canvas_adapter import (
        WorkflowCanvasAdapter,
    )
    a = WorkflowCanvasAdapter(registry=registry)
    yield a
    a.view.deleteLater()


def _simple_graph():
    """一个最小 WorkflowGraph：2 节点 + 1 连线。"""
    from src.features.ai_workflow.core.workflow_models import (
        PortRef, WorkflowEdge, WorkflowGraph, WorkflowNode,
    )
    return WorkflowGraph(
        workflow_id="test-wf",
        name="Test",
        nodes=(
            WorkflowNode(
                node_id="n1",
                spec_id="topos.media.selected_assets",
                ui={"x": 0, "y": 0},
            ),
            WorkflowNode(
                node_id="n2",
                spec_id="topos.analysis.transcribe_auto",
                ui={"x": 300, "y": 0},
            ),
        ),
        edges=(
            WorkflowEdge(
                from_port=PortRef(node_id="n1", port="assets"),
                to_port=PortRef(node_id="n2", port="assets"),
            ),
        ),
    )


# ---------------------------------------------------------------------- #
# GraphUiProtocol 满足
# ---------------------------------------------------------------------- #


def test_adapter_satisfies_graph_ui_protocol(adapter) -> None:
    """红线（plan §10.7.5）：WorkflowCanvasAdapter 满足 GraphUiProtocol。"""
    from src.shared.node_canvas.graph_ui_protocol import GraphUiProtocol
    assert isinstance(adapter, GraphUiProtocol)


def test_adapter_inherits_node_canvas_base(adapter) -> None:
    from src.shared.node_canvas.graph_ui_protocol import NodeCanvasBase
    assert isinstance(adapter, NodeCanvasBase)


# ---------------------------------------------------------------------- #
# build_from_graph
# ---------------------------------------------------------------------- #


def test_build_from_graph_renders_nodes(adapter) -> None:
    graph = _simple_graph()
    adapter.build_from_graph(graph)
    assert adapter.view.node_count == 2
    assert adapter.view.get_node_item("n1") is not None
    assert adapter.view.get_node_item("n2") is not None


def test_build_from_graph_renders_connections(adapter) -> None:
    graph = _simple_graph()
    adapter.build_from_graph(graph)
    assert adapter.view.connection_count == 1


def test_build_from_graph_clears_previous(adapter) -> None:
    graph = _simple_graph()
    adapter.build_from_graph(graph)
    assert adapter.view.node_count == 2
    # 再次 build（空 graph）应清空
    from src.features.ai_workflow.core.workflow_models import WorkflowGraph
    adapter.build_from_graph(WorkflowGraph(workflow_id="empty", name="E"))
    assert adapter.view.node_count == 0
    assert adapter.view.connection_count == 0


def test_build_from_graph_uses_node_ui_position(adapter) -> None:
    graph = _simple_graph()
    adapter.build_from_graph(graph)
    n1 = adapter.view.get_node_item("n1")
    assert n1.pos().x() == pytest.approx(0.0)
    n2 = adapter.view.get_node_item("n2")
    assert n2.pos().x() == pytest.approx(300.0)


# ---------------------------------------------------------------------- #
# 增删
# ---------------------------------------------------------------------- #


def test_add_node_visual(adapter) -> None:
    adapter.add_node_visual("n1", "topos.media.selected_assets", x=100, y=50)
    assert adapter.view.node_count == 1
    item = adapter.view.get_node_item("n1")
    assert item is not None
    assert item.pos().x() == pytest.approx(100.0)


def test_remove_node_visual(adapter) -> None:
    adapter.add_node_visual("n1", "topos.media.selected_assets")
    adapter.remove_node_visual("n1")
    assert adapter.view.node_count == 0


def test_add_connection_visual(adapter) -> None:
    adapter.add_node_visual("n1", "topos.media.selected_assets", x=0, y=0)
    adapter.add_node_visual("n2", "topos.analysis.transcribe_auto", x=300, y=0)
    adapter.add_connection_visual("n1", "assets", "n2", "assets")
    assert adapter.view.connection_count == 1


def test_remove_connection_visual(adapter) -> None:
    adapter.add_node_visual("n1", "topos.media.selected_assets", x=0, y=0)
    adapter.add_node_visual("n2", "topos.analysis.transcribe_auto", x=300, y=0)
    adapter.add_connection_visual("n1", "assets", "n2", "assets")
    adapter.remove_connection_visual("n1", "assets", "n2", "assets")
    assert adapter.view.connection_count == 0


# ---------------------------------------------------------------------- #
# 状态 / 选中 / 缩放
# ---------------------------------------------------------------------- #


def test_set_node_status(adapter) -> None:
    adapter.add_node_visual("n1", "topos.media.selected_assets")
    adapter.set_node_status("n1", "running")
    item = adapter.view.get_node_item("n1")
    assert item.status == "running"


def test_set_node_status_unknown_node_noop(adapter) -> None:
    adapter.set_node_status("nonexistent", "running")  # 不抛


def test_selected_node_ids_delegates_to_view(adapter) -> None:
    adapter.add_node_visual("n1", "topos.media.selected_assets")
    item = adapter.view.get_node_item("n1")
    item.setSelected(True)
    ids = adapter.selected_node_ids()
    assert "n1" in ids


def test_zoom_to_delegates_to_view(adapter) -> None:
    adapter.zoom_to(1.5)
    assert adapter.view.current_zoom == pytest.approx(1.5)


def test_clear_all(adapter) -> None:
    adapter.add_node_visual("n1", "topos.media.selected_assets")
    adapter.add_node_visual("n2", "topos.analysis.transcribe_auto")
    adapter.clear_all()
    assert adapter.view.node_count == 0


# ---------------------------------------------------------------------- #
# Adapter 持有 view（host 可访问信号）
# ---------------------------------------------------------------------- #


def test_adapter_exposes_view(adapter) -> None:
    """host 通过 ``adapter.view`` 访问 pyqtSignal（run_requested 等）。"""
    assert adapter.view is not None
    assert hasattr(adapter.view, "run_requested")


def test_adapter_unknown_spec_id_still_renders(adapter) -> None:
    """未知 spec_id（不在 registry）→ 用默认 token 渲染（不崩溃）。"""
    adapter.add_node_visual("n1", "topos.nonexistent.spec", x=0, y=0)
    assert adapter.view.node_count == 1
    item = adapter.view.get_node_item("n1")
    # category 退化为 control
    assert item.category == "control"


# ---------------------------------------------------------------------- #
# 红线：不直接写 timeline
# ---------------------------------------------------------------------- #


def test_adapter_does_not_write_timeline(adapter) -> None:
    """红线（plan §1.4.3）：adapter 不直接写 timeline。"""
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src", "features", "ai_workflow", "ui", "workflow_canvas_adapter.py",
    )
    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()
    # 不应调用 timeline_write / CommandStack / commit
    assert "CommandStack" not in source
    assert "timeline_write" not in source
    assert "commit_macro" not in source
