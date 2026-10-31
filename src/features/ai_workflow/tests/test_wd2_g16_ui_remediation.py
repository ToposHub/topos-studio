"""WD2-G16 回归测试：workflow UI 批 D 修复（REMEDIATION_PLAN 902-911 行）。

覆盖条目：

- AUD-23901：框选 overlay 视口坐标绘制（drawForeground 场景变换重置）。
- AUD-23902：空地点击（无 Ctrl）清除选中；Ctrl 保留追加语义。
- AUD-23903：首次点选未选中节点直接拖动 → nodes_moved 提交。
- AUD-23904：重复 key 加连线/节点先移除旧视觉（不孤儿残留）。
- AUD-23905：dragEnter/dragMove/drop 落地 assets_dropped（不再是死通道）。
- AUD-23906：程序化 add_connection_item 不发 node_connected（防 host 再入）。
- AUD-23912：build_from_graph 坐标容错（None/非法值不崩，回退默认）。
- AUD-23920：Inspector 参数值 int()/float() 强转防护。
- AUD-24004：加节点 / 加边 / 改参数均产生 undo 动作且可往返。
- AUD-24005：跨视图参数同步（画布 Inspector ↔ 表单页）+ 加节点重建表单。

本文件自带 QApplication fixture（本目录无共享 conftest 提供者）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from PyQt6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRectF, QUrl, Qt
from PyQt6.QtGui import (
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QMouseEvent,
    QPainter,
)
from PyQt6.QtWidgets import QApplication

from src.features.ai_workflow.core.node_specs import (
    NodeCategory,
    NodeExecutorId,
    NodePortSpec,
    WorkflowNodeSpec,
    register_official_specs,
)
from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateRegistry,
    register_official_templates,
)
from src.features.ai_workflow.core.node_specs import NodeSpecRegistry


# 本目录的 test_workflow_run_controller.py 在模块导入（collect）阶段创建
# QCoreApplication；QWidget 测试必须持 QApplication，且 Qt 只允许一个应用
# 实例。按字母序本文件先于该文件导入，故在 import 期即确保 QApplication
# 存在（该文件的 instance 守卫随后会复用它，不会另建 QCoreApplication）。
_APP = QApplication.instance()
if not isinstance(_APP, QApplication):
    _APP = QApplication(["test-wd2-g16"])


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if isinstance(app, QApplication):
        return app
    pytest.skip("会话已被 QCoreApplication 占用（无法构造 QWidget）")
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------- #
# 公共构造
# ---------------------------------------------------------------------- #


def _spec() -> WorkflowNodeSpec:
    return WorkflowNodeSpec(
        spec_id="topos.test.node",
        display_name="T",
        category=NodeCategory.ANALYSIS,
        description="",
        inputs=(NodePortSpec(name="in1", artifact_type="asset_set", direction="input"),),
        outputs=(NodePortSpec(name="out1", artifact_type="asset_set", direction="output"),),
        executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
    )


def _make_view(qapp) -> "object":
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
    view = WorkflowCanvasView()
    view.resize(400, 300)
    return view


def _make_node_item(node_id: str = "n1"):
    from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
    return WorkflowNodeItem(node_id=node_id, spec_id="topos.test.node", node_spec=_spec())


def _make_conn(view, n1, n2):
    from src.features.ai_workflow.ui.workflow_connection_item import (
        WorkflowConnectionItem,
    )
    return WorkflowConnectionItem(
        from_node_id=n1.node_id, from_port="out1",
        to_node_id=n2.node_id, to_port="in1",
        src_port=n1, dst_port=n2, artifact_type="asset_set",
    )


def _mouse(event_type, view_pos: QPointF, scene_pos: QPointF, button, buttons, modifiers):
    return QMouseEvent(event_type, view_pos, scene_pos, button, buttons, modifiers)


@pytest.fixture
def registries():
    node_reg = NodeSpecRegistry()
    register_official_specs(node_reg)
    tpl_reg = WorkflowTemplateRegistry()
    register_official_templates(tpl_reg)
    return node_reg, tpl_reg


@pytest.fixture
def panel(qapp, registries):
    from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel
    node_reg, tpl_reg = registries
    p = WorkflowPanel(registry=node_reg, template_registry=tpl_reg)
    yield p
    p.deleteLater()


# ---------------------------------------------------------------------- #
# AUD-23901：drawForeground 视口坐标
# ---------------------------------------------------------------------- #


def test_rubber_overlay_drawn_in_viewport_coords(qapp) -> None:
    """缩放后框选矩形仍画在视口坐标（painter 场景变换须被重置）。"""
    view = _make_view(qapp)
    view.zoom_to(0.5)
    view._rubber_active = True
    view._rubber_rect = QRectF(10, 10, 50, 40)

    img = QImage(400, 300, QImage.Format.Format_ARGB32)
    img.fill(0xFF000000)
    painter = QPainter(img)
    # 模拟 Qt：drawForeground 收到的 painter 处于场景坐标系
    painter.setTransform(view.transform())
    view.drawForeground(painter, QRectF())
    painter.end()

    inside = img.pixelColor(50, 40)   # 框内（视口坐标）
    outside = img.pixelColor(200, 200)  # 框外
    assert (inside.red(), inside.green(), inside.blue()) != (0, 0, 0), (
        "框选矩形应按视口坐标绘制在 (10..60, 10..50)"
    )
    assert (outside.red(), outside.green(), outside.blue()) == (0, 0, 0)
    view.deleteLater()


# ---------------------------------------------------------------------- #
# AUD-23902：空地点击清除选中
# ---------------------------------------------------------------------- #


def _press_empty(view, modifiers) -> None:
    press = _mouse(
        QEvent.Type.MouseButtonPress, QPointF(350, 280), QPointF(350, 280),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, modifiers,
    )
    view.mousePressEvent(press)


def test_empty_press_clears_selection_without_ctrl(qapp) -> None:
    view = _make_view(qapp)
    item = _make_node_item("n1")
    view.add_node_item(item)
    item.setSelected(True)
    cleared = []
    view.selection_cleared.connect(lambda: cleared.append(1))

    _press_empty(view, Qt.KeyboardModifier.NoModifier)

    assert view.selected_node_ids() == (), "空地点击（无 Ctrl）应清除选中"
    assert cleared, "清除选中应发 selection_cleared"
    view.deleteLater()


def test_empty_press_with_ctrl_keeps_selection(qapp) -> None:
    view = _make_view(qapp)
    item = _make_node_item("n1")
    view.add_node_item(item)
    item.setSelected(True)

    _press_empty(view, Qt.KeyboardModifier.ControlModifier)

    assert "n1" in view.selected_node_ids(), "Ctrl+空地点击保留既有选中（追加框选）"
    view.deleteLater()


# ---------------------------------------------------------------------- #
# AUD-23903：首次点选未选中节点直接拖动 → nodes_moved 提交
# ---------------------------------------------------------------------- #


def test_first_click_drag_on_unselected_node_commits_move(qapp) -> None:
    view = _make_view(qapp)
    item = _make_node_item("n1")
    view.add_node_item(item)
    item.setPos(0.0, 0.0)
    assert not item.isSelected()

    received = []
    view.nodes_moved.connect(lambda ids, pos: received.append((list(ids), list(pos))))

    # press 在未选中的节点上（super() 阶段 Qt 才把它置选中）
    press = _mouse(
        QEvent.Type.MouseButtonPress, QPointF(view.mapFromScene(QPointF(5, 5))),
        QPointF(5, 5), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(press)
    assert item.isSelected(), "press 后节点应被 Qt 场景分发选中"

    # 模拟拖拽 + release
    item.setPos(30.0, 40.0)
    release = _mouse(
        QEvent.Type.MouseButtonRelease, QPointF(view.mapFromScene(QPointF(35, 45))),
        QPointF(35, 45), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mouseReleaseEvent(release)

    assert received, "首次点选未选中节点直接拖动也应 emit nodes_moved"
    ids, positions = received[0]
    assert "n1" in ids
    assert (0.0, 0.0) in positions
    view.deleteLater()


# ---------------------------------------------------------------------- #
# AUD-23904：重复 key 不孤儿残留
# ---------------------------------------------------------------------- #


def test_duplicate_connection_key_replaces_old_item(qapp) -> None:
    view = _make_view(qapp)
    n1, n2 = _make_node_item("n1"), _make_node_item("n2")
    n2.setPos(200.0, 0.0)
    view.add_node_item(n1)
    view.add_node_item(n2)
    conn1 = _make_conn(view, n1, n2)
    conn2 = _make_conn(view, n1, n2)

    view.add_connection_item(conn1)
    scene_count = len(view.scene().items())
    view.add_connection_item(conn2)  # 同 key 重加

    assert view.connection_count == 1
    assert view.connection_items[conn2.key] is conn2
    assert conn1.scene() is None, "旧连线应从 scene 移除（不孤儿残留）"
    assert len(view.scene().items()) == scene_count, "一删一加，scene 总数不变"
    view.deleteLater()


def test_duplicate_node_id_replaces_old_item(qapp) -> None:
    view = _make_view(qapp)
    old_item = _make_node_item("n1")
    new_item = _make_node_item("n1")
    view.add_node_item(old_item)
    scene_count = len(view.scene().items())

    view.add_node_item(new_item)  # 同 node_id 重加

    assert view.node_count == 1
    assert view.get_node_item("n1") is new_item
    assert old_item.scene() is None, "旧节点应从 scene 移除"
    assert len(view.scene().items()) == scene_count  # 一删一加，总数不变
    view.deleteLater()


# ---------------------------------------------------------------------- #
# AUD-23905：拖放落地 assets_dropped
# ---------------------------------------------------------------------- #


def _mime_with_urls() -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl("file:///tmp/a.mp4"), QUrl("file:///tmp/b.mp4")])
    return mime


def test_drop_emits_assets_dropped(qapp) -> None:
    view = _make_view(qapp)
    received = []
    view.assets_dropped.connect(lambda urls, pos: received.append((list(urls), tuple(pos))))

    # 注意：QDragEnterEvent/QDropEvent 不持有 QMimeData 所有权，须保持
    # Python 引用存活到事件处理完（否则悬空指针段错误）。
    mime_enter = _mime_with_urls()
    enter = QDragEnterEvent(
        QPoint(100, 100), Qt.DropAction.CopyAction, mime_enter,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    view.dragEnterEvent(enter)
    assert enter.isAccepted(), "携带 URL 的拖入应被接受"

    mime_drop = _mime_with_urls()
    drop = QDropEvent(
        QPointF(120, 130), Qt.DropAction.CopyAction, mime_drop,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        QEvent.Type.Drop,
    )
    view.dropEvent(drop)

    assert received == [
        (["file:///tmp/a.mp4", "file:///tmp/b.mp4"], (120.0, 130.0)),
    ], "drop 应 emit assets_dropped(urls, scene_pos)"
    del drop, enter, mime_drop, mime_enter
    view.deleteLater()


def test_drag_enter_rejects_non_asset_mime(qapp) -> None:
    view = _make_view(qapp)
    mime = QMimeData()
    mime.setImageData(QImage(8, 8, QImage.Format.Format_ARGB32))  # 无 urls/text
    enter = QDragEnterEvent(
        QPoint(50, 50), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    view.dragEnterEvent(enter)
    assert not enter.isAccepted(), "非素材 mime 不应被接受"
    del enter, mime
    view.deleteLater()


# ---------------------------------------------------------------------- #
# AUD-23906：程序化加连线不发 node_connected
# ---------------------------------------------------------------------- #


def test_programmatic_add_connection_does_not_emit_node_connected(qapp) -> None:
    """add_connection_item 是程序化路径，emit 会造成 host 编辑层再入。"""
    view = _make_view(qapp)
    n1, n2 = _make_node_item("n1"), _make_node_item("n2")
    n2.setPos(200.0, 0.0)
    view.add_node_item(n1)
    view.add_node_item(n2)
    received = []
    view.node_connected.connect(lambda *a: received.append(a))

    view.add_connection_item(_make_conn(view, n1, n2))

    assert received == [], "程序化加连线不得 emit node_connected"
    assert view.connection_count == 1
    view.deleteLater()


# ---------------------------------------------------------------------- #
# AUD-23912：build_from_graph 坐标容错
# ---------------------------------------------------------------------- #


class _FakeNode:
    def __init__(self, node_id: str, spec_id: str, ui: object) -> None:
        self.node_id = node_id
        self.spec_id = spec_id
        self.ui = ui


class _FakeGraph:
    def __init__(self, nodes, edges=()) -> None:
        self.nodes = nodes
        self.edges = edges


class _FakeRegistry:
    def get(self, spec_id: str):  # noqa: ANN204 — 测试桩
        return None


def test_build_from_graph_survives_bad_coords(qapp) -> None:
    from src.features.ai_workflow.ui.workflow_canvas_adapter import (
        WorkflowCanvasAdapter,
    )
    adapter = WorkflowCanvasAdapter(registry=_FakeRegistry())
    graph = _FakeGraph(nodes=[
        _FakeNode("ok", "topos.x.a", {"x": 10.0, "y": 20.0}),
        _FakeNode("null_xy", "topos.x.a", {"x": None, "y": None}),
        _FakeNode("bad_str", "topos.x.a", {"x": "abc", "y": [1, 2]}),
        _FakeNode("num_str", "topos.x.a", {"x": "12.5", "y": "-7"}),
        _FakeNode("no_ui", "topos.x.a", None),
    ])

    adapter.build_from_graph(graph)  # 不得抛 TypeError/ValueError

    assert adapter.view.node_count == 5
    ok_item = adapter.view.get_node_item("ok")
    assert (ok_item.x(), ok_item.y()) == (10.0, 20.0)
    null_item = adapter.view.get_node_item("null_xy")
    assert (null_item.x(), null_item.y()) == (0.0, 0.0), "None 坐标回退默认"
    bad_item = adapter.view.get_node_item("bad_str")
    assert (bad_item.x(), bad_item.y()) == (0.0, 0.0), "非法串回退默认"
    num_item = adapter.view.get_node_item("num_str")
    assert (num_item.x(), num_item.y()) == (12.5, -7.0), "数字串正常解析"
    adapter.view.deleteLater()


# ---------------------------------------------------------------------- #
# AUD-23920：Inspector 参数强转防护
# ---------------------------------------------------------------------- #


def test_inspector_set_node_with_hostile_params_no_crash(qapp) -> None:
    from PyQt6.QtWidgets import QDoubleSpinBox, QSpinBox
    from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector

    spec = SimpleNamespace(
        display_name="Hostile",
        params_schema={
            "properties": {
                "ratio": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "bad_range": {"type": "number", "minimum": "x", "maximum": None},
                "count": {"type": "integer", "minimum": 0, "maximum": 10},
                "name": {"type": "string"},
            }
        },
    )
    params = {
        "ratio": "not-a-number",
        "bad_range": {"nested": True},
        "count": None,
        "name": "ok",
    }
    inspector = WorkflowInspector()
    inspector.set_node("n1", spec, params)  # 不得抛 ValueError/TypeError

    ratio = inspector.param_widgets["ratio"]
    assert isinstance(ratio, QDoubleSpinBox)
    assert ratio.value() == pytest.approx(0.0), "坏值回退 minimum"
    bad_range = inspector.param_widgets["bad_range"]
    assert isinstance(bad_range, QDoubleSpinBox), "坏 schema 边界回退默认不崩"
    count = inspector.param_widgets["count"]
    assert isinstance(count, QSpinBox)
    assert count.value() == 0, "None 回退 minimum"
    inspector.deleteLater()


def test_inspector_refresh_param_no_reemit(qapp) -> None:
    """refresh_param 更新控件且不回发 parameter_changed（防递归状态锁）。"""
    from PyQt6.QtWidgets import QLineEdit, QSpinBox
    from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector

    spec = SimpleNamespace(
        display_name="Sync",
        params_schema={
            "properties": {
                "count": {"type": "integer", "minimum": 0, "maximum": 100},
                "name": {"type": "string"},
            }
        },
    )
    inspector = WorkflowInspector()
    inspector.set_node("n1", spec, {"count": 3, "name": "old"})
    emitted = []
    inspector.parameter_changed.connect(lambda *a: emitted.append(a))

    inspector.refresh_param("n1", "count", 42)
    inspector.refresh_param("n1", "name", "synced")

    assert isinstance(inspector.param_widgets["count"], QSpinBox)
    assert inspector.param_widgets["count"].value() == 42
    assert isinstance(inspector.param_widgets["name"], QLineEdit)
    assert inspector.param_widgets["name"].text() == "synced"
    assert emitted == [], "程序化刷新不得回发 parameter_changed（防递归）"
    # 其它节点 id 不影响本 Inspector
    inspector.refresh_param("other", "count", 99)
    assert inspector.param_widgets["count"].value() == 42
    inspector.deleteLater()


# ---------------------------------------------------------------------- #
# AUD-24004：撤销栈覆盖（加边 / 改参 / 加节点）
# ---------------------------------------------------------------------- #


def _find_fresh_edge_pair(panel) -> tuple:
    """在当前图中找一对尚未连接的真实端口（from→to）。"""
    graph = panel.current_graph
    existing = {
        (e.from_port.node_id, e.from_port.port, e.to_port.node_id, e.to_port.port)
        for e in graph.edges
    }
    nodes = list(graph.nodes)
    for src in nodes:
        src_spec = panel._registry.get(src.spec_id)
        outs = [p.name for p in getattr(src_spec, "outputs", ())] if src_spec else []
        for dst in nodes:
            if dst.node_id == src.node_id:
                continue
            dst_spec = panel._registry.get(dst.spec_id)
            ins = [p.name for p in getattr(dst_spec, "inputs", ())] if dst_spec else []
            for out_p in outs:
                for in_p in ins:
                    if (src.node_id, out_p, dst.node_id, in_p) not in existing:
                        return (src.node_id, out_p, dst.node_id, in_p)
    raise AssertionError("模板图中找不到未连接的端口对")


def test_add_edge_pushes_undo_and_roundtrips(panel) -> None:
    panel.load_template("wf-auto-caption")
    fn, fp, tn, tp = _find_fresh_edge_pair(panel)
    edges_before = len(panel.current_graph.edges)
    conns_before = panel.adapter.view.connection_count
    depth_before = panel.undo_stack.depth()

    panel.adapter.view.node_connected.emit(fn, fp, tn, tp)

    assert len(panel.current_graph.edges) == edges_before + 1
    assert panel.adapter.view.connection_count == conns_before + 1
    assert panel.undo_stack.depth() == depth_before + 1, "加边应 push undo 动作"

    panel.undo_stack.undo()
    assert len(panel.current_graph.edges) == edges_before, "undo 移除边"
    assert panel.adapter.view.connection_count == conns_before, "undo 移除连线视觉"

    panel.undo_stack.redo()
    assert len(panel.current_graph.edges) == edges_before + 1
    assert panel.adapter.view.connection_count == conns_before + 1


def test_param_change_pushes_undo_and_roundtrips(panel) -> None:
    panel.load_template("wf-auto-caption")
    node, props = _node_with_props(panel)
    key = next(iter(props))
    old_value = node.params.get(key)
    new_value = _different_value(props[key], old_value)
    assert new_value != old_value, "测试前提：新旧值必须可区分"
    depth_before = panel.undo_stack.depth()

    panel.inspector.parameter_changed.emit(node.node_id, key, new_value)

    assert panel.undo_stack.depth() == depth_before + 1, "改参数应 push undo 动作"
    assert panel.current_graph.require_node(node.node_id).params[key] == new_value

    panel.undo_stack.undo()
    assert panel.current_graph.require_node(node.node_id).params[key] == old_value

    panel.undo_stack.redo()
    assert panel.current_graph.require_node(node.node_id).params[key] == new_value


def test_add_node_pushes_undo_and_roundtrips(panel) -> None:
    panel.load_template("wf-auto-caption")
    nodes_before = len(panel.current_graph.nodes)
    depth_before = panel.undo_stack.depth()

    panel._add_node_from_search("topos.analysis.transcribe_auto")

    assert len(panel.current_graph.nodes) == nodes_before + 1
    assert panel.adapter.view.node_count == nodes_before + 1
    assert panel.undo_stack.depth() == depth_before + 1, "加节点应 push undo 动作"

    panel.undo_stack.undo()
    assert len(panel.current_graph.nodes) == nodes_before, "undo 移除节点"
    assert panel.adapter.view.node_count == nodes_before

    panel.undo_stack.redo()
    assert len(panel.current_graph.nodes) == nodes_before + 1
    assert panel.adapter.view.node_count == nodes_before + 1


# ---------------------------------------------------------------------- #
# AUD-24005：跨视图参数同步 + 加节点重建表单
# ---------------------------------------------------------------------- #


def _node_with_props(panel) -> tuple:
    for node in panel.current_graph.nodes:
        spec = panel._registry.get(node.spec_id)
        if spec is None:
            continue
        schema = getattr(spec, "params_schema", {}) or {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if props:
            return node, props
    raise AssertionError("模板图中没有带 params_schema 的节点")


def _different_value(definition: dict, current: object) -> object:
    """产一个与当前值不同的 JSON-safe 新值（保证断言可区分新旧）。"""
    ptype = definition.get("type", "string")
    if ptype == "boolean":
        return not bool(current)
    if ptype == "number":
        mn = float(definition.get("minimum", 0.0) or 0.0)
        mx = float(definition.get("maximum", 1.0) or 1.0)
        if mx <= mn:
            mx = mn + 1.0
        if current is None or float(current) != mn:
            return mn
        return min(mn + (mx - mn) / 2.0, mx)
    if ptype == "integer":
        mn = int(definition.get("minimum", 0) or 0)
        mx = int(definition.get("maximum", 100) or 100)
        if mx <= mn:
            mx = mn + 1
        if current is None or int(current) != mn:
            return mn
        return min(mn + max((mx - mn) // 2, 1), mx)
    if "enum" in definition:
        for opt in definition["enum"]:
            if str(opt) != str(current):
                return str(opt)
        return str(definition["enum"][0])
    return "synced" if current != "synced" else "synced-v2"


def _widget_value(widget) -> object:
    from PyQt6.QtWidgets import (
        QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox,
    )
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QDoubleSpinBox):
        return widget.value()
    if isinstance(widget, QSpinBox):
        return widget.value()
    if isinstance(widget, QLineEdit):
        return widget.text()
    return None


def test_canvas_param_change_updates_form_page(panel) -> None:
    """画布 Inspector 改参数 → 表单页同节点控件刷新。"""
    panel.load_template("wf-auto-caption")
    node, props = _node_with_props(panel)
    key = next(iter(props))
    # 表单页持该节点的 Inspector
    form_inspector = next(
        insp for insp in panel.form_page._inspectors
        if insp.current_node_id == node.node_id
    )
    widget = form_inspector.param_widgets[key]
    new_value = _different_value(props[key], _widget_value(widget))
    assert _widget_value(widget) != new_value, "测试前提：控件旧值 ≠ 新值"

    panel.inspector.parameter_changed.emit(node.node_id, key, new_value)

    assert _widget_value(widget) == new_value, "表单页控件应显示最新值"


def test_form_param_change_updates_canvas_inspector(panel) -> None:
    """表单页改参数 → 画布 Inspector（已选中该节点）控件刷新。"""
    panel.load_template("wf-auto-caption")
    node, props = _node_with_props(panel)
    key = next(iter(props))
    # 画布 Inspector 载入该节点
    panel.adapter.view.node_selected.emit(node.node_id)
    assert panel.inspector.current_node_id == node.node_id
    canvas_widget = panel.inspector.param_widgets[key]
    new_value = _different_value(props[key], _widget_value(canvas_widget))
    assert _widget_value(canvas_widget) != new_value, "测试前提：控件旧值 ≠ 新值"

    form_inspector = next(
        insp for insp in panel.form_page._inspectors
        if insp.current_node_id == node.node_id
    )
    form_inspector.parameter_changed.emit(node.node_id, key, new_value)

    assert _widget_value(canvas_widget) == new_value, "画布 Inspector 应显示最新值"


def test_add_node_rebuilds_form_page(panel) -> None:
    """画布加节点后表单页重建，新节点出现在参数表单中。"""
    panel.load_template("wf-auto-caption")
    ids_before = {insp.current_node_id for insp in panel.form_page._inspectors}

    panel._add_node_from_search("topos.analysis.transcribe_auto")

    new_ids = {insp.current_node_id for insp in panel.form_page._inspectors}
    added = new_ids - ids_before
    assert len(added) == 1, "新节点应出现在参数表单页"
    new_node_id = added.pop()
    assert any(
        n.node_id == new_node_id and n.spec_id == "topos.analysis.transcribe_auto"
        for n in panel.current_graph.nodes
    )
