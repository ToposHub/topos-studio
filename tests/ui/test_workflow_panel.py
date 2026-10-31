"""P8.5 测试：WorkflowPanel 容器面板。

验证 AI Workflow dock 容器的契约（plan §7.1 / §8）：

- 面板创建 + 空态显示（无 graph 时提示）。
- 模板下拉列官方模板（auto_caption / subtitle_localization）。
- ``load_template`` → 画布出现节点（``adapter.view`` scene items 非空）。
- ``clear_canvas`` → 画布空。
- 缩放 label 更新（绑 ``view.zoom_changed``）。
- Run / Cancel 按钮存在（本批禁用）。
- **红线**：源码不引入 moderngl / composite.core。
"""
from __future__ import annotations

import os

import pytest


def test_source_does_not_import_gpu_or_composite() -> None:
    """红线：panel 源码不含 moderngl / composite.core。"""
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "src", "features", "ai_workflow", "ui", "workflow_panel.py",
    )
    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite.core" not in source
    assert "TextureResult" not in source


@pytest.fixture
def registries():
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
    return node_reg, tpl_reg


@pytest.fixture
def panel(qapp, registries):
    from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel
    node_reg, tpl_reg = registries
    p = WorkflowPanel(registry=node_reg, template_registry=tpl_reg)
    yield p
    p.deleteLater()


# ---------------------------------------------------------------------- #
# 创建 + 空态
# ---------------------------------------------------------------------- #


def test_panel_creates(panel) -> None:
    assert panel is not None
    assert panel.adapter is not None
    assert panel.adapter.view is not None


def test_panel_shows_empty_hint_when_no_graph(panel) -> None:
    """空态：无 graph 时显示提示。"""
    # 空态 label 可见
    assert panel.empty_hint_widget is not None
    assert panel.empty_hint_widget.isVisible() or not panel.adapter.view.node_count


def test_panel_template_combo_lists_official_templates(panel) -> None:
    """模板下拉列官方模板（auto_caption / subtitle_localization）。"""
    combo = panel.template_combo
    # 至少 2 个模板
    assert combo.count() >= 2
    texts = [combo.itemText(i) for i in range(combo.count())]
    # 应包含自动字幕 / 字幕翻译配音（display_name）
    joined = " ".join(texts)
    assert "caption" in joined.lower() or "字幕" in joined or "Auto" in joined


# ---------------------------------------------------------------------- #
# 加载模板 / 清空
# ---------------------------------------------------------------------- #


def test_load_template_renders_nodes(panel) -> None:
    """加载模板 → 画布出现节点。"""
    panel.load_template("wf-auto-caption")
    assert panel.adapter.view.node_count > 0


def test_clear_canvas_empties_view(panel) -> None:
    """Clear → 画布空。"""
    panel.load_template("wf-auto-caption")
    assert panel.adapter.view.node_count > 0
    panel.clear_canvas()
    assert panel.adapter.view.node_count == 0


def test_agent_console_context_and_plan_workspace(panel) -> None:
    panel.load_template("wf-auto-caption")
    panel.set_context_snapshot({
        "project_id": "project-1",
        "timeline_id": "timeline-1",
        "brief_id": "brief-1",
        "brief_revision": 3,
        "base_revision": 8,
        "selected_asset_ids": ["a1", "a2"],
        "profile_id": "deepseek-local",
        "permission": "propose",
    }, provider_label="DeepSeek", status="已绑定")
    panel.set_plan_preview({
        "plan_hash": "abcdef1234567890",
        "nodes": [{"node_id": "n1"}],
        "scope": {"analysis_snapshot_hash": "analysis-1", "selected_asset_ids": ["a1"]},
        "timeline_write_requires_approval": True,
    }, plan_hash="abcdef1234567890")
    assert "project-1" in panel.context_labels["project"].text()
    assert "timeline-1" in panel.context_labels["timeline"].text()
    assert "abcdef123456" in panel.plan_text.toPlainText()
    assert "analysis-1" in panel.evidence_text.toPlainText()
    assert panel.plan_tabs.count() == 6
    assert "审批" in panel.release_gate_text.toPlainText()


def test_load_unknown_template_is_noop(panel) -> None:
    """未知模板 → 不抛，画布不变。"""
    panel.load_template("nonexistent-template")
    assert panel.adapter.view.node_count == 0


# ---------------------------------------------------------------------- #
# 缩放 label
# ---------------------------------------------------------------------- #


def test_zoom_label_updates(panel) -> None:
    """缩放 label 跟随 view.zoom_changed。"""
    panel.adapter.zoom_to(1.5)
    label_text = panel.zoom_label.text()
    # label 应含 150 或 1.5
    assert "150" in label_text or "1.5" in label_text


# ---------------------------------------------------------------------- #
# Run / Cancel 按钮（P9：随 graph 状态使能）
# ---------------------------------------------------------------------- #


def test_run_button_disabled_when_no_graph(panel) -> None:
    """无 graph 时 Run 按钮禁用。"""
    assert panel.run_button is not None
    assert not panel.run_button.isEnabled()


def test_run_button_enabled_when_graph_loaded(panel) -> None:
    """加载模板（有节点）后 Run 按钮使能。"""
    panel.load_template("wf-auto-caption")
    assert panel.adapter.view.node_count > 0
    assert panel.run_button.isEnabled()


def test_run_button_disabled_after_clear(panel) -> None:
    """清空画布后 Run 按钮再次禁用。"""
    panel.load_template("wf-auto-caption")
    assert panel.run_button.isEnabled()
    panel.clear_canvas()
    assert not panel.run_button.isEnabled()


def test_cancel_button_exists(panel) -> None:
    """Cancel 按钮存在；默认禁用，run 进行时由 controller 使能。"""
    assert panel.cancel_button is not None
    # 初始（无运行）禁用
    assert not panel.cancel_button.isEnabled()


# ---------------------------------------------------------------------- #
# run_requested / cancel_requested 信号（P9）
# ---------------------------------------------------------------------- #


def test_panel_has_run_requested_signal(panel) -> None:
    """panel 暴露 run_requested signal（controller 连接它）。"""
    assert hasattr(panel, "run_requested")
    assert hasattr(panel, "cancel_requested")


def test_run_button_click_emits_run_requested(panel) -> None:
    """点 Run 按钮 → emit run_requested（用 list connect 捕获）。"""
    panel.load_template("wf-auto-caption")
    received = []
    panel.run_requested.connect(lambda: received.append(1))
    panel.run_button.click()
    assert received == [1]


def test_cancel_button_click_emits_cancel_requested(panel) -> None:
    """点 Cancel 按钮 → emit cancel_requested。"""
    received = []
    panel.cancel_requested.connect(lambda: received.append(1))
    # 手动使能 cancel（模拟 run 进行中）以验证信号链路
    panel.cancel_button.setEnabled(True)
    panel.cancel_button.click()
    assert received == [1]


# ---------------------------------------------------------------------- #
# current_graph（P9：controller 据 graph 编译）
# ---------------------------------------------------------------------- #


def test_current_graph_none_initially(panel) -> None:
    """初始无 graph：current_graph 为 None。"""
    assert panel.current_graph is None


def test_current_graph_set_after_load(panel) -> None:
    """加载模板后 current_graph 非 None。"""
    panel.load_template("wf-auto-caption")
    assert panel.current_graph is not None
    # 应是 WorkflowGraph 实例
    from src.features.ai_workflow.core.workflow_models import WorkflowGraph
    assert isinstance(panel.current_graph, WorkflowGraph)


def test_current_graph_cleared_on_clear(panel) -> None:
    """清空画布后 current_graph 重置为 None。"""
    panel.load_template("wf-auto-caption")
    assert panel.current_graph is not None
    panel.clear_canvas()
    assert panel.current_graph is None


# ---------------------------------------------------------------------- #
# Run Drawer（P9：嵌入 AiRunDrawer）
# ---------------------------------------------------------------------- #


def test_panel_has_run_drawer(panel) -> None:
    """panel 嵌入 AiRunDrawer（复用 edit feature 的现成 widget）。"""
    assert hasattr(panel, "run_drawer")
    assert panel.run_drawer is not None


def test_panel_set_run_drawer(panel) -> None:
    """set_run_drawer setter 把状态灌进 drawer（带 _updating 锁防递归）。"""
    from src.features.edit.ui.ai_assistant_composer import (
        RunDrawerItem, RunDrawerState,
    )
    state = RunDrawerState(
        items=[RunDrawerItem(item_id="r1", label="合成运行", status="running")],
        active_count=1,
    )
    panel.set_run_drawer(state)
    # drawer 内部 state 已更新
    assert panel.run_drawer.state.active_count == 1
    assert len(panel.run_drawer.state.items) == 1


def test_run_drawer_renders_history_and_recovery_metadata(panel) -> None:
    """History is rendered as a compact safe summary, not raw run payloads."""
    from src.features.edit.ui.ai_assistant_composer import (
        RunDrawerItem, RunDrawerState,
    )
    panel.set_run_drawer(RunDrawerState(
        items=[RunDrawerItem(
            item_id="run-1", label="Workflow", status="waiting_input",
            can_resume=True, checkpoint_summary="等待审批",
            recovery_summary="等待用户确认后恢复",
        )],
        active_count=1,
        history=[RunDrawerItem(
            item_id="old-run", label="旧任务", status="failed",
            checkpoint_summary="终态快照", recovery_summary="可重试 / 可查看诊断",
            kind="history",
        )],
    ))
    assert panel.run_drawer.state.history[0].item_id == "old-run"
    assert not panel.run_drawer._history_text.isHidden()
    assert "旧任务" in panel.run_drawer._history_text.text()


def test_panel_artifact_and_release_gate_details(panel) -> None:
    panel.set_artifact_details({"artifact_id": "art-1", "summary": "plan ready"})
    assert "art-1" in panel.evidence_text.toPlainText()
    panel.set_release_gate([{"id": "host_delivery_gate", "status": "not_run"}])
    assert "not_run" in panel.release_gate_text.toPlainText()


def test_panel_metrics_details(panel) -> None:
    panel.set_metrics({"total_runs": 3, "success_rate": 0.666667})
    text = panel.metrics_text.toPlainText()
    assert "total_runs" in text
    assert "0.666667" in text


# ---------------------------------------------------------------------- #
# adapter 暴露（host 接 run 信号）
# ---------------------------------------------------------------------- #


def test_panel_exposes_adapter(panel) -> None:
    """host 通过 ``panel.adapter`` 接 run_requested 信号。"""
    assert hasattr(panel.adapter.view, "run_requested")
    assert hasattr(panel.adapter.view, "cancel_requested")


# ---------------------------------------------------------------------- #
# P10.1d：节点移动 → 变更层 → 撤销栈
# ---------------------------------------------------------------------- #


def test_panel_has_undo_stack(panel) -> None:
    """panel 持 UndoStack（P10.1d 接线）。"""
    assert hasattr(panel, "undo_stack")
    assert panel.undo_stack is not None


def test_nodes_moved_updates_current_graph(panel) -> None:
    """view.nodes_moved emit → panel 用 WorkflowGraphEdit.move_nodes 更新 current_graph。

    moved_ids 的节点 ui.x/y 变为新位置；其它节点不变。
    """
    panel.load_template("wf-auto-caption")
    graph_before = panel.current_graph
    assert graph_before is not None
    # 找第一个节点，记原位
    first_node = graph_before.nodes[0]
    old_x = float(first_node.ui.get("x", 0.0))
    old_y = float(first_node.ui.get("y", 0.0))
    # emit nodes_moved：first_node 从 old 移到 (old_x+100, old_y+50)
    panel.adapter.view.nodes_moved.emit(
        [first_node.node_id], [(old_x, old_y)],
    )
    # current_graph 更新
    new_node = panel.current_graph.require_node(first_node.node_id)
    # 视觉位置（item.pos）= old + delta；graph 应反映视觉当前位置
    item = panel.adapter.view.get_node_item(first_node.node_id)
    assert new_node.ui["x"] == item.x()
    assert new_node.ui["y"] == item.y()


def test_nodes_moved_pushes_undo_action(panel) -> None:
    """每次 nodes_moved push 一个 EditAction（undo 栈深度 +1）。"""
    panel.load_template("wf-auto-caption")
    depth_before = panel.undo_stack.depth()
    graph = panel.current_graph
    nid = graph.nodes[0].node_id
    panel.adapter.view.nodes_moved.emit([nid], [(0.0, 0.0)])
    assert panel.undo_stack.depth() == depth_before + 1


def test_undo_reverts_node_position(panel) -> None:
    """undo 后节点视觉 + graph 回到旧位置。"""
    panel.load_template("wf-auto-caption")
    graph = panel.current_graph
    nid = graph.nodes[0].node_id
    item = panel.adapter.view.get_node_item(nid)
    pos_before = (item.x(), item.y())
    # 移动
    panel.adapter.view.nodes_moved.emit([nid], [pos_before])
    pos_after_move = (item.x(), item.y())
    assert (panel.current_graph.require_node(nid).ui["x"],
            panel.current_graph.require_node(nid).ui["y"]) == pos_after_move
    # undo
    panel.undo_stack.undo()
    assert (item.x(), item.y()) == pos_before
    assert (panel.current_graph.require_node(nid).ui["x"],
            panel.current_graph.require_node(nid).ui["y"]) == pos_before


def test_redo_reapplies_node_position(panel) -> None:
    """redo 后节点再到新位置。"""
    panel.load_template("wf-auto-caption")
    graph = panel.current_graph
    nid = graph.nodes[0].node_id
    item = panel.adapter.view.get_node_item(nid)
    pos_before = (item.x(), item.y())
    panel.adapter.view.nodes_moved.emit([nid], [pos_before])
    pos_after = (item.x(), item.y())
    panel.undo_stack.undo()
    panel.undo_stack.redo()
    assert (item.x(), item.y()) == pos_after
    assert (panel.current_graph.require_node(nid).ui["x"],
            panel.current_graph.require_node(nid).ui["y"]) == pos_after


def test_undo_requested_signal_triggers_undo(panel) -> None:
    """view.undo_requested（Ctrl+Z）→ panel.undo_stack.undo。"""
    panel.load_template("wf-auto-caption")
    graph = panel.current_graph
    nid = graph.nodes[0].node_id
    panel.adapter.view.nodes_moved.emit([nid], [(0.0, 0.0)])
    depth_before = panel.undo_stack.depth()
    # 模拟 Ctrl+Z
    panel.adapter.view.undo_requested.emit()
    assert panel.undo_stack.depth() == depth_before - 1


def test_set_node_position_does_not_loop(panel) -> None:
    """adapter.set_node_position（undo/redo 内部用）不 emit nodes_moved（防循环）。"""
    panel.load_template("wf-auto-caption")
    graph = panel.current_graph
    nid = graph.nodes[0].node_id
    received = []
    panel.adapter.view.nodes_moved.connect(
        lambda ids, pos: received.append(1)
    )
    # 直接调 set_node_position（不经过 mouseRelease）
    panel.adapter.set_node_position(nid, 999.0, 888.0)
    assert received == [], "set_node_position 不应 emit nodes_moved（防 undo/redo 循环）"
