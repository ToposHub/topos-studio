"""G1a · 真实产品入口测试：EditModule 菜单注册 + 宿主回调存在性。

这是真实入口层（module.register_menu_actions / EditControllerMixin）的
证据，不是手造 MainWindow 属性的替代——菜单描述符经真实 ActionRegistry
过滤逻辑验证；回调存在性经 mixin 类属性验证（menu_builder 用
getattr(self, callback_name) 解析，缺失时菜单不出现）。
"""

from __future__ import annotations

import pytest


pytestmark = [pytest.mark.edit_core]


def _edit_module():
    from src.features.edit.module import EditModule

    module = EditModule()
    if not getattr(module, "_available", True):
        pytest.skip("edit module unavailable in this environment")
    return module


def test_insights_menu_registered_with_edit_feature():
    from src.shared.actions import ActionRegistry

    registry = ActionRegistry()
    _edit_module().register_menu_actions(registry)

    by_id = {a.action_id: a for a in registry.get_actions()}
    action = by_id.get("edit.insights.open")
    assert action is not None, "edit.insights.open missing from Edit menu"
    assert action.feature_id == "edit"
    assert action.menu_path == "Edit/Insights"
    assert action.callback_name == "on_insights_open"

    # Edit 禁用时入口隐藏（真实过滤逻辑）
    filtered = {
        a.action_id for a in registry.get_actions(enabled_features={"color"})
    }
    assert "edit.insights.open" not in filtered
    enabled = {
        a.action_id for a in registry.get_actions(enabled_features={"edit"})
    }
    assert "edit.insights.open" in enabled


def test_host_callback_exists_on_edit_controller_mixin():
    """menu_builder 按 getattr(main_window, callback_name) 解析——mixin
    类上必须真的有 on_insights_open，否则菜单不会出现（空壳防护）。"""
    from src.app.controllers.edit_controller import EditControllerMixin

    assert hasattr(EditControllerMixin, "on_insights_open")
    assert hasattr(EditControllerMixin, "_insights_capture_state")
    assert hasattr(EditControllerMixin, "_insights_navigate")


def test_navigation_semantics_on_minimal_host(qtbot):
    """_insights_navigate 的拒绝/接受语义（不建空序列；身份不匹配拒绝）。"""
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.features.edit.insights.host_protocol import NavigateRequest
    from tests.edit.insights import synthetic as syn

    class Host(EditControllerMixin):
        def __init__(self, project, sequences):
            self._current_edit_project = project
            self._current_timeline_sequences = sequences
            self.switched = []

        def _on_timeline_switched(self, timeline_id):
            self.switched.append(timeline_id)

    class EditPageStub:
        def __init__(self):
            self.frames = []

        def seek_to_frame(self, frame):
            self.frames.append(frame)

    st = syn.build_synthetic_state()
    host = Host(st.project, dict(st.sequences))
    host.edit_page = EditPageStub()

    # 无工程 → no_project
    host._current_edit_project = None
    no_project = NavigateRequest(
        capture_id="cap", project_id="p",
        sequence_id=syn.SEQ_MAIN, target_frame=0,
    )
    assert host._insights_navigate(no_project).reason == "no_project"
    host._current_edit_project = st.project

    # 正常导航 → 既有切换路径 + 既有播放头路径
    req = NavigateRequest(
        capture_id="cap", project_id=st.project.project_id,
        sequence_id=syn.SEQ_MAIN, target_frame=42,
    )
    result = host._insights_navigate(req)
    assert result.ok and result.status == "navigated"
    assert result.actual_frame == 42
    assert host.switched == [syn.SEQ_MAIN]
    assert host.edit_page.frames == [42]

    # 换工程 → project_changed
    other = NavigateRequest(
        capture_id="cap", project_id="another-project",
        sequence_id=syn.SEQ_MAIN, target_frame=0,
    )
    assert host._insights_navigate(other).reason == "project_changed"

    # 未物化序列（unloaded ref）→ target_missing，不建空序列
    missing = NavigateRequest(
        capture_id="cap", project_id=st.project.project_id,
        sequence_id=syn.SEQ_UNLOADED, target_frame=0,
    )
    assert host._insights_navigate(missing).reason == "target_missing"
    assert syn.SEQ_UNLOADED not in host._current_timeline_sequences

    # revision 漂移 → sequence_changed
    drifted = NavigateRequest(
        capture_id="cap", project_id=st.project.project_id,
        sequence_id=syn.SEQ_MAIN, target_frame=0,
        expected_sequence_revision=st.main.revision + 5,
    )
    assert host._insights_navigate(drifted).reason == "sequence_changed"
