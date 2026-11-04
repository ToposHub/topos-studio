"""Tool markers reflect real editor changes without changing project state."""
from types import SimpleNamespace

from PyQt6.QtCore import QObject

from src.app.controllers.color_node_tool_feedback import ColorNodeToolFeedback
from src.features.color.core.layer import Layer
from src.features.color.ui.node_editor_panel import ColorNodePanel


def test_live_primary_and_curve_markers_clear_when_reset(qapp, qtbot):
    panel = ColorNodePanel()
    qtbot.addWidget(panel)
    node_id = panel.view.state.graph.active_id
    layer = Layer('Selected')
    host = QObject()
    host.engine = SimpleNamespace(get_active_layer=lambda: layer)
    host.color_node_panel = panel
    host.color_graph_host = SimpleNamespace(
        panel_host=SimpleNamespace(active_handle=SimpleNamespace(node_id=node_id, is_corrector=True)),
        panel_layer_payload=lambda _: {'id': layer.id},
    )
    feedback = ColorNodeToolFeedback(host)
    feedback.refresh()
    assert not panel.view.cards[node_id].node.tool_indicators
    layer.grading_params['temp'] = 300.0
    feedback.refresh()
    assert panel.view.cards[node_id].node.tool_indicators == ('primary',)
    layer.curves_data = {'RGB': [(0.0, 255.0), (128.0, 90.0), (255.0, 0.0)]}
    feedback.refresh()
    assert 'curves' in panel.view.cards[node_id].node.tool_indicators
    assert 'Applied tools' in panel.view.cards[node_id].toolTip()
    layer.reset_params()
    feedback.refresh()
    assert not panel.view.cards[node_id].node.tool_indicators
    assert 'Applied tools' not in panel.view.cards[node_id].toolTip()


def test_wrong_layer_cannot_mark_selected_node(qapp, qtbot):
    panel = ColorNodePanel()
    qtbot.addWidget(panel)
    node_id = panel.view.state.graph.active_id
    layer = Layer('Other')
    layer.grading_params['temp'] = 300.0
    host = QObject()
    host.engine = SimpleNamespace(get_active_layer=lambda: layer)
    host.color_node_panel = panel
    host.color_graph_host = SimpleNamespace(
        panel_host=SimpleNamespace(active_handle=SimpleNamespace(node_id=node_id, is_corrector=True)),
        panel_layer_payload=lambda _: {'id': 'different'},
    )
    ColorNodeToolFeedback(host).refresh()
    assert not panel.view.cards[node_id].node.tool_indicators
