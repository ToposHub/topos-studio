"""P8.3 测试：WorkflowCanvasView 画布视图（QGraphicsView）。

验证 AI Workflow 画布视图的契约（plan §7.2 / §6.1 / graph_ui_protocol GraphUiSignals）：

- 视图创建 + scene 存在 + 默认 zoom。
- 节点/连线增删管理（``add_node_item`` / ``remove_node_item`` / ``add_connection_item``）。
- ``selected_node_ids`` 反映选中态。
- ``zoom_to`` / ``current_zoom`` 缩放控制。
- 信号定义对齐 ``GraphUiSignals.ALL``（关键信号存在）。
- **红线**：源码不引入 moderngl / composite.core。
"""
from __future__ import annotations

import os

import pytest


def test_source_does_not_import_gpu_or_composite() -> None:
    """红线：画布视图源码不含 moderngl / composite.core。"""
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "src", "features", "ai_workflow", "ui", "workflow_canvas_view.py",
    )
    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite.core" not in source
    assert "TextureResult" not in source


@pytest.fixture
def view(qapp):
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
    v = WorkflowCanvasView()
    yield v
    v.deleteLater()


@pytest.fixture
def node_item(qapp):
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, NodePortSpec, WorkflowNodeSpec,
    )
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    spec = WorkflowNodeSpec(
        spec_id="topos.test.n",
        display_name="N", category=NodeCategory.INPUT,
        inputs=(),
        outputs=(NodePortSpec(name="out", artifact_type="asset_ref", direction="output"),),
        executor_id=NodeExecutorId.READ_ONLY,
    )
    return WorkflowNodeItem(node_id="n1", spec_id="topos.test.n", node_spec=spec)


# ---------------------------------------------------------------------- #
# 创建 + scene
# ---------------------------------------------------------------------- #


def test_view_creates_with_scene(view) -> None:
    assert view.scene() is not None
    # 默认 zoom = 1.0
    assert view.current_zoom == pytest.approx(1.0)


def test_view_has_signal_attributes(view) -> None:
    """画布视图定义了 GraphUiSignals.ALL 中的关键信号。"""
    expected_signals = [
        "node_added", "node_removed", "node_connected", "node_disconnected",
        "node_selected", "nodes_selected", "selection_cleared",
        "node_double_clicked", "nodes_moved", "graph_changed",
        "zoom_changed", "undo_requested", "redo_requested",
        "run_requested", "cancel_requested", "assets_dropped",
    ]
    for sig_name in expected_signals:
        assert hasattr(view, sig_name), f"missing signal: {sig_name}"


# ---------------------------------------------------------------------- #
# 节点 / 连线管理
# ---------------------------------------------------------------------- #


def test_add_node_item(view, node_item) -> None:
    view.add_node_item(node_item)
    assert view.get_node_item("n1") is node_item
    assert view.node_count == 1


def test_remove_node_item(view, node_item) -> None:
    view.add_node_item(node_item)
    view.remove_node_item("n1")
    assert view.get_node_item("n1") is None
    assert view.node_count == 0


def test_remove_unknown_node_is_noop(view) -> None:
    view.remove_node_item("nonexistent")  # 不抛
    assert view.node_count == 0


def test_add_connection_item(view, node_item) -> None:
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, NodePortSpec, WorkflowNodeSpec,
    )
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )

    spec2 = WorkflowNodeSpec(
        spec_id="topos.test.n2",
        display_name="N2", category=NodeCategory.ANALYSIS,
        inputs=(NodePortSpec(name="in", artifact_type="asset_ref", direction="input"),),
        outputs=(),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )
    n2 = WorkflowNodeItem(node_id="n2", spec_id="topos.test.n2", node_spec=spec2)
    view.add_node_item(node_item)
    view.add_node_item(n2)

    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="out",
        to_node_id="n2", to_port="in",
        src_port=node_item, dst_port=n2,
        artifact_type="asset_ref",
    )
    # 给连线提供 port_scene_pos 的鸭子类型：node_item 已实现
    # WorkflowConnectionItem 调 src_port.port_scene_pos() —— 但 NodeItem 没这方法
    # 改用直接传端口定位器
    view.add_connection_item(conn)
    assert view.connection_count == 1


def test_remove_connection_item(view, node_item) -> None:
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, NodePortSpec, WorkflowNodeSpec,
    )
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )

    spec2 = WorkflowNodeSpec(
        spec_id="topos.test.n2",
        display_name="N2", category=NodeCategory.ANALYSIS,
        inputs=(NodePortSpec(name="in", artifact_type="asset_ref", direction="input"),),
        outputs=(),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )
    n2 = WorkflowNodeItem(node_id="n2", spec_id="topos.test.n2", node_spec=spec2)
    view.add_node_item(node_item)
    view.add_node_item(n2)
    conn = WorkflowConnectionItem(
        from_node_id="n1", from_port="out",
        to_node_id="n2", to_port="in",
        src_port=node_item, dst_port=n2,
        artifact_type="asset_ref",
    )
    view.add_connection_item(conn)
    view.remove_connection_item(conn.key)
    assert view.connection_count == 0


def test_clear_all_removes_nodes_and_connections(view, node_item) -> None:
    view.add_node_item(node_item)
    view.clear_all()
    assert view.node_count == 0
    assert view.connection_count == 0


# ---------------------------------------------------------------------- #
# 选中
# ---------------------------------------------------------------------- #


def test_selected_node_ids_empty_initially(view) -> None:
    assert view.selected_node_ids() == ()


def test_selected_node_ids_after_select(view, node_item) -> None:
    view.add_node_item(node_item)
    node_item.setSelected(True)
    # 触发 scene 的 selectionChanged（scene 自动处理）
    ids = view.selected_node_ids()
    assert "n1" in ids


def test_clear_selection(view, node_item) -> None:
    view.add_node_item(node_item)
    node_item.setSelected(True)
    view.clear_selection()
    assert view.selected_node_ids() == ()


# ---------------------------------------------------------------------- #
# 缩放
# ---------------------------------------------------------------------- #


def test_zoom_to_changes_current_zoom(view) -> None:
    view.zoom_to(1.5)
    assert view.current_zoom == pytest.approx(1.5)


def test_zoom_clamped_to_min(view) -> None:
    view.zoom_to(0.01)
    # 不低于 visual_tokens.zoom_min
    from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS
    assert view.current_zoom >= DEFAULT_VISUAL_TOKENS.zoom_min


def test_zoom_clamped_to_max(view) -> None:
    view.zoom_to(100.0)
    from src.shared.node_canvas.graph_ui_protocol import DEFAULT_VISUAL_TOKENS
    assert view.current_zoom <= DEFAULT_VISUAL_TOKENS.zoom_max


def test_zoom_changed_signal_emitted(view) -> None:
    """``zoom_to`` 应 emit ``zoom_changed``。"""
    received = []
    view.zoom_changed.connect(lambda z: received.append(z))
    view.zoom_to(2.0)
    assert len(received) >= 1
    assert received[-1] == pytest.approx(2.0)


def test_node_count_property(view) -> None:
    assert view.node_count == 0


# ---------------------------------------------------------------------- #
# P10.1c：节点移动 → nodes_moved 信号
# ---------------------------------------------------------------------- #


def test_nodes_moved_signal_declared(view) -> None:
    """view 暴露 nodes_moved 信号（list, list）。"""
    assert hasattr(view, "nodes_moved")


def test_nodes_moved_emitted_on_release_after_drag(qapp) -> None:
    """拖动节点（press → move → release）后 emit nodes_moved。

    Qt 原生拖拽期间 item.pos 变；release 时 view 比对 start 快照 → emit。
    """
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, WorkflowNodeSpec,
    )
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
    from PyQt6.QtCore import QPointF, QEvent, Qt
    from PyQt6.QtGui import QMouseEvent

    spec = WorkflowNodeSpec(
        spec_id="topos.test.node", display_name="T", category=NodeCategory.ANALYSIS,
        description="", inputs=(), outputs=(),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )
    view = WorkflowCanvasView()
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=spec)
    view.add_node_item(item)
    item.setPos(0.0, 0.0)
    item.setSelected(True)

    received = []
    view.nodes_moved.connect(lambda ids, pos: received.append((list(ids), list(pos))))

    # press（在节点上）→ 记 start
    scene_pt = QPointF(5, 5)
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(view.mapFromScene(scene_pt)), scene_pt,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(press)

    # 模拟 Qt 原生拖拽：item 被移动
    item.setPos(30.0, 40.0)

    # release
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(view.mapFromScene(QPointF(35, 45))),
        QPointF(35, 45),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mouseReleaseEvent(release)

    assert len(received) >= 1, "拖动后应 emit nodes_moved"
    ids, positions = received[0]
    assert "n1" in ids
    assert (0.0, 0.0) in positions  # old position 快照


def test_nodes_moved_not_emitted_when_no_drag(qapp) -> None:
    """点节点但不拖动（pos 未变）→ release 不 emit nodes_moved。"""
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, WorkflowNodeSpec,
    )
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
    from PyQt6.QtCore import QPointF, QEvent, Qt
    from PyQt6.QtGui import QMouseEvent

    spec = WorkflowNodeSpec(
        spec_id="topos.test.node", display_name="T", category=NodeCategory.ANALYSIS,
        description="", inputs=(), outputs=(),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )
    view = WorkflowCanvasView()
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=spec)
    view.add_node_item(item)
    item.setSelected(True)

    received = []
    view.nodes_moved.connect(lambda ids, pos: received.append(1))

    scene_pt = QPointF(5, 5)
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(view.mapFromScene(scene_pt)), scene_pt,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(press)
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(view.mapFromScene(scene_pt)), scene_pt,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mouseReleaseEvent(release)
    assert received == [], "未拖动不应 emit nodes_moved"


# ---------------------------------------------------------------------- #
# P10.2a：端口命中检测 + 连线创建
# ---------------------------------------------------------------------- #


def _port_spec():
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, NodePortSpec, WorkflowNodeSpec,
    )
    return WorkflowNodeSpec(
        spec_id="topos.test.node", display_name="T", category=NodeCategory.ANALYSIS,
        description="",
        inputs=(NodePortSpec(name="in1", artifact_type="asset_set", direction="input"),),
        outputs=(NodePortSpec(name="out1", artifact_type="asset_set", direction="output"),),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )


def test_find_port_at_output_returns_node_and_port(qapp) -> None:
    """_find_port_at 在 output 端口位置命中 → 返回 (node_item, port_name, 'output')。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView

    view = WorkflowCanvasView()
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=_port_spec())
    view.add_node_item(item)
    item.setPos(0.0, 0.0)
    out_pos = item.port_scene_pos("out1", "output")
    assert out_pos is not None
    found = view._find_port_at(out_pos)
    assert found is not None
    found_item, port_name, direction = found
    assert found_item is item
    assert port_name == "out1"
    assert direction == "output"


def test_find_port_at_input_returns_input(qapp) -> None:
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView

    view = WorkflowCanvasView()
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=_port_spec())
    view.add_node_item(item)
    item.setPos(0.0, 0.0)
    in_pos = item.port_scene_pos("in1", "input")
    assert in_pos is not None
    found = view._find_port_at(in_pos)
    assert found is not None
    _, port_name, direction = found
    assert port_name == "in1"
    assert direction == "input"


def test_find_port_at_miss_returns_none(qapp) -> None:
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
    from PyQt6.QtCore import QPointF

    view = WorkflowCanvasView()
    item = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=_port_spec())
    view.add_node_item(item)
    item.setPos(0.0, 0.0)
    center = QPointF(item.boundingRect().width() / 2, item.boundingRect().height() / 2)
    assert view._find_port_at(center) is None


def test_connection_drag_creates_node_connected(qapp) -> None:
    """从 n1.out1 拖到 n2.in1 → emit node_connected(n1, out1, n2, in1)。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
    from PyQt6.QtCore import QPointF, QEvent, Qt
    from PyQt6.QtGui import QMouseEvent

    view = WorkflowCanvasView()
    n1 = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=_port_spec())
    n2 = WorkflowNodeItem(node_id="n2", spec_id="topos.test.node", node_spec=_port_spec())
    view.add_node_item(n1)
    view.add_node_item(n2)
    n1.setPos(0.0, 0.0)
    n2.setPos(200.0, 0.0)

    received = []
    view.node_connected.connect(
        lambda fn, fp, tn, tp: received.append((fn, fp, tn, tp))
    )

    src_port = n1.port_scene_pos("out1", "output")
    dst_port = n2.port_scene_pos("in1", "input")
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(view.mapFromScene(src_port)), src_port,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(press)
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(view.mapFromScene(dst_port)), dst_port,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mouseMoveEvent(move)
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(view.mapFromScene(dst_port)), dst_port,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mouseReleaseEvent(release)
    assert received == [("n1", "out1", "n2", "in1")], (
        f"应 emit node_connected(n1,out1,n2,in1)；实际：{received}"
    )


def test_connection_drag_no_target_no_emit(qapp) -> None:
    """拖到空地 → 不 emit node_connected。"""
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
    from PyQt6.QtCore import QPointF, QEvent, Qt
    from PyQt6.QtGui import QMouseEvent

    view = WorkflowCanvasView()
    n1 = WorkflowNodeItem(node_id="n1", spec_id="topos.test.node", node_spec=_port_spec())
    view.add_node_item(n1)
    n1.setPos(0.0, 0.0)
    received = []
    view.node_connected.connect(lambda *a: received.append(a))

    src_port = n1.port_scene_pos("out1", "output")
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(view.mapFromScene(src_port)), src_port,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(press)
    nowhere = QPointF(500.0, 500.0)
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(view.mapFromScene(nowhere)), nowhere,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mouseMoveEvent(move)
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(view.mapFromScene(nowhere)), nowhere,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mouseReleaseEvent(release)
    assert received == [], "拖到空地不应 emit node_connected"


def test_connection_drag_type_mismatch_no_emit(qapp) -> None:
    """artifact_type 不匹配 → 不 emit node_connected。"""
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory, NodeExecutorId, NodePortSpec, WorkflowNodeSpec,
    )
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
    from PyQt6.QtCore import QPointF, QEvent, Qt
    from PyQt6.QtGui import QMouseEvent

    spec_out = WorkflowNodeSpec(
        spec_id="topos.test.out", display_name="O", category=NodeCategory.ANALYSIS,
        description="",
        inputs=(),
        outputs=(NodePortSpec(name="out1", artifact_type="transcript_ref", direction="output"),),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )
    spec_in = WorkflowNodeSpec(
        spec_id="topos.test.in", display_name="I", category=NodeCategory.ANALYSIS,
        description="",
        inputs=(NodePortSpec(name="in1", artifact_type="asset_set", direction="input"),),
        outputs=(),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )
    view = WorkflowCanvasView()
    n1 = WorkflowNodeItem(node_id="n1", spec_id="topos.test.out", node_spec=spec_out)
    n2 = WorkflowNodeItem(node_id="n2", spec_id="topos.test.in", node_spec=spec_in)
    view.add_node_item(n1)
    view.add_node_item(n2)
    n1.setPos(0.0, 0.0)
    n2.setPos(200.0, 0.0)
    received = []
    view.node_connected.connect(lambda *a: received.append(a))

    src_port = n1.port_scene_pos("out1", "output")
    dst_port = n2.port_scene_pos("in1", "input")
    for et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease):
        pos = src_port if et == QEvent.Type.MouseButtonPress else dst_port
        evt = QMouseEvent(
            et, QPointF(view.mapFromScene(pos)), pos,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton if et != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        if et == QEvent.Type.MouseButtonPress:
            view.mousePressEvent(evt)
        elif et == QEvent.Type.MouseMove:
            view.mouseMoveEvent(evt)
        else:
            view.mouseReleaseEvent(evt)
    assert received == [], "类型不匹配不应 emit node_connected"
