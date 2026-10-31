"""模板库 + 参数表单入口测试（批次 2）。

验收：

- 弹窗默认落模板库页（gallery）；模板卡选择 → 表单页（不进画布）。
- 表单页按节点分组渲染参数控件（复用 WorkflowInspector 工厂）；改动经
  ``parameter_changed`` → ``WorkflowGraphEdit.set_node_param`` 入图。
- 「高级编辑」/工具栏按钮切换画布视图；Run 按钮两种模式共用。
- 清空 → 回模板库；``load_graph``（持久化还原）落表单页。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.ai_workflow.core.node_specs import (
    NodeSpecRegistry,
    register_official_specs,
)
from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateRegistry,
    register_official_templates,
)
from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel


@pytest.fixture()
def node_reg():
    reg = NodeSpecRegistry()
    register_official_specs(reg)
    return reg


@pytest.fixture()
def tpl_reg():
    treg = WorkflowTemplateRegistry()
    register_official_templates(treg)
    return treg


@pytest.fixture()
def panel(qapp, node_reg, tpl_reg):
    p = WorkflowPanel(registry=node_reg, template_registry=tpl_reg)
    yield p
    p.deleteLater()


class TestGalleryEntry:
    def test_panel_defaults_to_gallery(self, panel) -> None:
        assert panel.view_mode == "gallery"
        assert panel._stack.currentWidget() is panel.gallery_page

    def test_gallery_lists_all_templates(self, panel, tpl_reg) -> None:
        for entry in tpl_reg.all_templates():
            assert panel.gallery_page.findChild(
                object, f"template-card-{entry.template_id}",
            ) is not None

    def test_gallery_selection_lands_on_form(self, panel) -> None:
        panel.gallery_page.template_selected.emit("wf-auto-caption")
        assert panel.view_mode == "form"
        assert panel._stack.currentWidget() is panel.form_page
        assert panel.current_graph is not None


class TestFormPage:
    def test_load_template_lands_on_form_not_canvas(self, panel) -> None:
        panel.load_template("wf-talking-head-fine-cut")
        assert panel.view_mode == "form"
        assert panel._stack.currentWidget() is panel.form_page
        # 画布也已同步渲染（可随时切高级编辑）
        assert panel.adapter.view.node_count > 0
        assert panel.run_button.isEnabled()

    def test_form_renders_per_node_sections(self, panel) -> None:
        panel.load_template("wf-talking-head-fine-cut")
        # 口播模板 asr 节点有 params_schema（language/enable_diarization）
        assert len(panel.form_page._inspectors) >= 1
        inspector_ids = {i.current_node_id for i in panel.form_page._inspectors}
        assert "asr" in inspector_ids

    def test_form_param_change_updates_graph(self, panel) -> None:
        panel.load_template("wf-talking-head-fine-cut")
        before = dict(
            next(n.params for n in panel.current_graph.nodes if n.node_id == "asr")
        )
        # 模拟表单控件改动（与画布 Inspector 同一信号路径）
        panel.form_page.parameter_changed.emit(
            "asr", "language", "zh",
        )
        after = next(
            n.params for n in panel.current_graph.nodes if n.node_id == "asr"
        )
        assert after.get("language") == "zh"
        assert before.get("language") != "zh" or after != before

    def test_no_param_template_shows_hint(self, panel) -> None:
        panel.load_template("wf-composite-render-import")
        assert panel.view_mode == "form"


class TestViewSwitching:
    def test_advanced_edit_switches_to_canvas(self, panel) -> None:
        panel.load_template("wf-auto-caption")
        panel.form_page.advanced_edit_requested.emit()
        assert panel.view_mode == "canvas"
        assert panel._stack.currentWidget() is panel.adapter.view

    def test_toolbar_buttons_switch_views(self, panel) -> None:
        panel.load_template("wf-auto-caption")
        panel.show_canvas_view()
        assert panel.view_mode == "canvas"
        panel.show_form_view()
        assert panel.view_mode == "form"
        assert panel._stack.currentWidget() is panel.form_page

    def test_canvas_graph_change_keeps_form_mode(self, panel) -> None:
        """表单模式下 adapter 静默渲染不被打断（不会跳画布）。"""
        panel.load_template("wf-auto-caption")
        assert panel.view_mode == "form"
        panel.adapter.view.graph_changed.emit()
        assert panel._stack.currentWidget() is panel.form_page

    def test_clear_returns_to_gallery(self, panel) -> None:
        panel.load_template("wf-auto-caption")
        panel.clear_canvas()
        assert panel.view_mode == "gallery"
        assert panel._stack.currentWidget() is panel.gallery_page
        assert panel.current_graph is None
        assert not panel.run_button.isEnabled()


class TestBackNavigation:
    """表单页「返回模板库」：仅切视图，保留当前图（清空才丢弃）。"""

    def test_back_keeps_graph_and_switches_view(self, panel) -> None:
        panel.load_template("wf-auto-caption")
        panel.form_page.gallery_requested.emit()
        assert panel.view_mode == "gallery"
        assert panel._stack.currentWidget() is panel.gallery_page
        # 图保留：表单按钮仍可用，切回表单还在
        assert panel.current_graph is not None
        panel.show_form_view()
        assert panel.view_mode == "form"

    def test_back_then_new_template_replaces_graph(self, panel) -> None:
        panel.load_template("wf-auto-caption")
        panel.form_page.gallery_requested.emit()
        panel.gallery_page.template_selected.emit("wf-talking-head-fine-cut")
        assert panel.view_mode == "form"
        assert panel.current_template_id == "wf-talking-head-fine-cut"

    def test_back_from_canvas_via_form(self, panel) -> None:
        """画布 → 表单 → 返回模板库的两跳路径可达。"""
        panel.load_template("wf-auto-caption")
        panel.show_canvas_view()
        panel.show_form_view()
        panel.form_page.gallery_requested.emit()
        assert panel.view_mode == "gallery"


class TestLoadGraphRestore:
    def test_load_graph_lands_on_form(self, panel, tpl_reg) -> None:
        graph = tpl_reg.instantiate("wf-auto-caption")
        panel.load_graph(graph, skill_id="topos.auto_caption")
        assert panel.view_mode == "form"
        assert panel.current_graph is not None
        assert panel.current_skill_id == "topos.auto_caption"
        assert panel.current_template_id == ""
