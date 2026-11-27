from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.inspector_layout import InspectorComboBox, PARAM_LABEL_WIDTH
from src.features.composite.ui.node_properties_panel import NodePropertiesPanel


def _node_data(node):
    return {
        "node_id": node.id,
        "node_type": node.node_type,
        "node_title": node.node_title,
        "parameters": dict(node.parameters),
        "param_options": node.get_param_options(),
        "param_defs": node.get_param_defs(),
    }


def test_composite_sections_share_control_columns(qtbot):
    panel = NodePropertiesPanel()
    qtbot.addWidget(panel)
    nodes = [ConstantNode("constant"), OutputNode("output"), BlurNode("blur"),
             TransformNode("transform")]
    panel.set_nodes([_node_data(node) for node in nodes])
    panel.show_project_settings(NodeGraph())
    panel.resize(374, 1000)
    panel.show()
    qtbot.waitUntil(lambda: panel._sections["constant"].width() > 300)

    constant, output, blur, transform = (panel._sections[node.id] for node in nodes)
    settings = panel._project_settings_section._settings_widget
    controls = [
        constant._param_widgets["mode"]["widget"],
        constant._param_widgets["width"]["widget"],
        output._param_widgets["format_mode"]["widget"],
        output._param_widgets["motion_blur_enabled"]["widget"],
        output._param_widgets["output_name"]["widget"],
        output._param_widgets["width"]["slider"],
        blur._param_widgets["radius_x"]["widget"],
        blur._param_widgets["mix"]["slider"],
        transform._param_widgets["motion_blur"]["widget"],
        settings._fps_combo,
    ]
    assert len({w.mapTo(panel, QPoint()).x() for w in controls}) == 1
    assert len({s._reset_btn.mapTo(panel, QPoint()).x() for s in (constant, output, blur)}) == 1
    for section, key in ((constant, "mode"), (output, "format_mode"), (blur, "blur_type")):
        combo = section._param_widgets[key]["widget"]
        assert combo.mapTo(section, QPoint()).x() + combo.width() == (
            output._param_widgets["width"]["widget"].mapTo(output, QPoint()).x()
            + output._param_widgets["width"]["widget"].width()
        )
    assert not constant._param_widgets["width"]["slider"].isVisible()
    assert not blur._param_widgets["radius_x"]["slider"].isVisible()
    assert output._param_widgets["width"]["slider"].styleSheet() == ""


def test_long_combo_reclaims_only_the_space_it_needs(qtbot):
    row = QWidget()
    qtbot.addWidget(row)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    label = QLabel("Mode")
    label.setFixedWidth(PARAM_LABEL_WIDTH)
    combo = InspectorComboBox(label)
    combo.addItems(["Short", "High-quality compositing mode"])
    layout.addWidget(label)
    layout.addWidget(combo, 1)
    text_width = combo.fontMetrics().horizontalAdvance(combo.itemText(1))
    row.resize(PARAM_LABEL_WIDTH + layout.spacing() + text_width + 24, 32)
    row.show()

    qtbot.waitUntil(lambda: label.width() == PARAM_LABEL_WIDTH)
    combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: label.width() < PARAM_LABEL_WIDTH)
    qtbot.waitUntil(lambda: combo.width() >= text_width + 44)

    row.resize(row.width() + 80, 32)
    qtbot.waitUntil(lambda: label.width() == PARAM_LABEL_WIDTH)
