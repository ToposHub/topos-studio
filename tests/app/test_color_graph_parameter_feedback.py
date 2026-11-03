"""Selected authoritative correctors refresh host panels without writeback."""
from types import SimpleNamespace
from unittest.mock import Mock

from src.app.main_window import MainWindow


def test_selected_corrector_feedback_merges_graph_grade_and_balances_guard():
    graph_host = Mock()
    # N03：面板回填读合并投影（panel_layer_payload），不再裸 legacy 载体
    graph_host.panel_layer_payload.return_value = {"id": "selected-layer", "grading_params": {"exposure": 2.0}}
    handle = SimpleNamespace(node_id="selected-node", params={}, grade_params=lambda: {"exposure": 15.0})
    host = SimpleNamespace(color_graph_host=graph_host, update_from_layer=Mock(),
                           _set_color_stack_hydration_guard=Mock(),
                           _set_color_graph_panels_enabled=Mock())
    MainWindow._on_color_graph_handle(host, handle)
    shown = host.update_from_layer.call_args.args[0]
    assert shown.id == "selected-layer"
    assert shown.grading_params["exposure"] == 15.0
    graph_host.align_engine_active_layer.assert_called_once_with("selected-node")
    assert [call.args[0] for call in host._set_color_stack_hydration_guard.call_args_list] == [True, False]


def test_read_only_selection_disables_all_node_parameter_editors(qapp, qtbot):
    from PyQt6.QtWidgets import QWidget
    # D2：plugin_panel 已删除（Effects 浏览器为资源目录，不在此禁用集）
    names = ("effects_panel", "qualifier_panel", "warper_panel", "mask_panel", "tracker_panel")
    widgets = {name: QWidget() for name in names}
    wheel = QWidget()
    for widget in [wheel, *widgets.values()]:
        qtbot.addWidget(widget)
    host = SimpleNamespace(**widgets, _collect_color_panels=lambda: {"grading_panel": wheel})
    for enabled in (False, True):
        MainWindow._set_color_graph_panels_enabled(host, enabled)
        assert all(widget.isEnabled() == enabled for widget in [wheel, *widgets.values()])
