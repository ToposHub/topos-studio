"""Library and dynamic settings UI with explicit target gating."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtSvg import QSvgRenderer
from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser, _effect_icon
from src.features.color.ui.node_resource_mime import decode_resource_mime
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.style_sheet import C


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
    assert not panel.tabs.isTabEnabled(1)
    assert panel.tabs.currentIndex() == 0
    panel.set_instances([{'instance_id': 'i', 'name': 'Glow'}])
    panel.show_settings()
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


def test_header_divider_spans_actions_and_search_opens_library(qtbot: Any) -> None:
    panel = ColorEffectsBrowser()
    qtbot.addWidget(panel)
    panel.set_resources([resource()])
    panel.set_instances([{'instance_id': 'i', 'name': 'Glow'}])
    panel.show()
    for width in (220, 360, 610):
        panel.resize(width, 200)
        image = panel.tabs.grab().toImage()
        y = panel.tabs.HEADER_HEIGHT - 1
        assert all(image.pixelColor(x, y) == QColor(C.PANEL_DIVIDER)
                   for x in range(image.width()))
    panel.show_settings()
    assert panel.tabs.currentIndex() == 1
    qtbot.mouseClick(panel.search_button, Qt.MouseButton.LeftButton)
    assert panel.tabs.currentIndex() == 0
    assert panel.search_edit.isVisible()
    panel.search_edit.setText('glow')
    panel.show_settings()
    qtbot.mouseClick(panel.search_button, Qt.MouseButton.LeftButton)
    assert panel.tabs.currentIndex() == 0
    assert panel.search_button.isChecked()
    assert panel.search_edit.text() == 'glow'
    qtbot.mouseClick(panel.search_button, Qt.MouseButton.LeftButton)
    assert panel.search_edit.text() == ''


def test_drag_enabled_after_switching_list_view_mode(qtbot: Any) -> None:
    panel = ColorEffectsBrowser()
    qtbot.addWidget(panel)
    panel.set_resources([resource()])
    for grid in (True, False):
        panel.view_button.setChecked(grid)
        assert panel.resource_list.dragEnabled()
        assert not panel.resource_list.viewport().acceptDrops()
        item = panel.resource_list.item(0 if grid else 1)
        assert item.flags() & Qt.ItemFlag.ItemIsDragEnabled
        assert decode_resource_mime(panel.resource_list.mimeData([item]))['resource_id'] == 'fx'


def test_production_library_omits_other_page_effects_and_double_click_is_inert(qtbot: Any) -> None:
    panel = ColorEffectsBrowser()
    qtbot.addWidget(panel)
    panel.set_production_mode(True)
    panel.set_resources([resource(), dict(resource(), id='effect:blur',
                                          available=False, disabled_reason='color_backend_not_wired')])
    assert panel.resource_list.count() == 2
    panel.set_target('n', '01', True)
    item = panel.resource_list.item(1)
    panel.resource_list.setCurrentItem(item)
    with qtbot.assertNotEmitted(panel.resource_activated):
        panel.resource_list.itemDoubleClicked.emit(item)
    assert panel.tabs.currentIndex() == 0


def test_bundled_plugins_have_distinct_renderable_svg_icons(qtbot: Any) -> None:
    from src.features.color.ui import effects_browser_panel

    root = Path(__file__).resolve().parents[2]
    icon_dir = Path(effects_browser_panel.__file__).with_name('effect_icons')
    fallback = _effect_icon('unknown_plugin').pixmap(24, 24).toImage()
    rendered = set()
    for plugin in (root / 'plugins').iterdir():
        if plugin.suffix not in {'.tglsl', '.tai'}:
            continue
        assert QSvgRenderer(str(icon_dir / f'{plugin.stem}.svg')).isValid(), plugin.stem
        image = _effect_icon(plugin.stem).pixmap(24, 24).toImage()
        assert not image.isNull()
        assert image != fallback
        pixels = image.constBits().asstring(image.sizeInBytes())
        assert pixels not in rendered, plugin.stem
        rendered.add(pixels)

    panel = ColorEffectsBrowser()
    qtbot.addWidget(panel)
    panel.set_resources([dict(resource(), id='Film_Grain@abcd1234', name='胶片颗粒')])
    assert panel.resource_list.item(1).icon().pixmap(24, 24).toImage() == (
        _effect_icon('Film_Grain').pixmap(24, 24).toImage())
