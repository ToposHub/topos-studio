"""Library and dynamic settings UI with explicit target gating."""
from __future__ import annotations
from typing import Any
from PyQt6.QtCore import Qt
from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser
from src.features.color.ui.node_resource_mime import decode_resource_mime
from src.shared.ui.widgets.param_slider_row import ParamSliderRow


def resource() -> dict[str, Any]:
    return {'id': 'fx', 'name': 'Glow', 'kind': 'effect', 'category': 'Light', 'description': 'Soft bloom'}


def test_search_favorite_and_collapsible_categories(qtbot: Any) -> None:
    panel = ColorEffectsBrowser()
    qtbot.addWidget(panel)
    panel.set_resources([resource()])
    assert not panel.tabs.isTabEnabled(1)
    assert panel.resource_list.count() == 2
    panel._category_clicked(panel.resource_list.item(0))
    assert panel.resource_list.count() == 1
    panel.search_edit.setText('glow')
    assert panel.resource_list.count() == 2  # Search reveals matches in collapsed categories.
    panel.toggle_favorite('fx')
    panel.favorites_button.setChecked(True)
    assert panel.resource_list.item(1).text().endswith('Glow')
    panel.search_edit.setText('missing')
    assert panel.resource_list.count() == 0


def test_add_requires_target_but_drag_does_not(qtbot: Any) -> None:
    panel = ColorEffectsBrowser()
    qtbot.addWidget(panel)
    panel.set_resources([resource()])
    item = panel.resource_list.item(1)
    panel.resource_list.setCurrentItem(item)
    assert not panel.add_button.isEnabled()
    assert decode_resource_mime(panel.resource_list.mimeData([item]))['resource_id'] == 'fx'
    requested = []
    panel.resource_activated.connect(requested.append)
    panel.set_target('n', '01', True)
    qtbot.mouseClick(panel.add_button, Qt.MouseButton.LeftButton)
    assert requested[0]['id'] == 'fx'
    assert panel.tabs.isTabEnabled(1)
    assert panel.tabs.currentIndex() == 1
    panel.set_target('n', '01', False)
    assert not panel.settings_scroll.widget().isEnabled()


def test_parameter_hydration_silent_then_user_intent(qtbot: Any) -> None:
    panel = ColorEffectsBrowser()
    qtbot.addWidget(panel)
    panel.set_target('n', '01', True)
    changes = []
    panel.draft_edit_requested.connect(lambda *args: changes.append(args))
    panel.set_instances([{'instance_id': 'i', 'name': 'Glow', 'enabled': True, 'params': {'gain': 0.4}, 'parameters': [
        {'var_name': 'gain', 'label': 'Gain', 'type': 'slider', 'default': 0.1, 'min': 0.0, 'max': 1.0},
        {'var_name': 'color', 'label': 'Color', 'type': 'color', 'default': [1.0, 1.0, 1.0]},
        {'var_name': 'mode', 'label': 'Mode', 'type': 'select', 'default': 0, 'options': ['A', 'B']},
    ]}])
    assert changes == []
    row = panel.findChild(ParamSliderRow, 'effect_param_i_gain')
    assert row.value() == 0.4
    row.slider.setValue(round(0.6 * row.scale))
    assert changes[-1][0] == 'set_parameter'
    assert changes[-1][1]['value'] == 0.6


def test_resolve_library_toolbar_and_legacy_list_migration(qtbot: Any) -> None:
    panel = ColorEffectsBrowser()
    qtbot.addWidget(panel)
    panel.set_resources([resource()])
    assert panel.search_row.isHidden()
    assert panel.category_combo.isHidden()
    assert panel.add_button.isHidden()
    panel.search_button.setChecked(True)
    assert not panel.search_row.isHidden()
    panel.search_edit.setText('glow')
    assert panel.resource_list.count() == 2
    panel.search_button.setChecked(False)
    assert panel.search_edit.text() == ''
    panel.set_browser_state({'grid': True})
    assert panel.browser_state()['grid'] is False
