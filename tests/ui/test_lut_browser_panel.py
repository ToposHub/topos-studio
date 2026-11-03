"""LUT browser presentation and drag intents do not execute LUTs."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt

from src.features.color.ui.lut_browser_panel import ColorLutBrowser
from src.features.color.ui.node_resource_mime import decode_resource_mime, resource_mime_data


def entries() -> list[dict[str, Any]]:
    return [
        {'id': 'warm', 'kind': 'lut', 'name': 'Warm Print', 'category': 'Film/Print'},
        {'id': 'cool', 'kind': 'lut', 'name': 'Cool Night', 'category': 'Film/Night'},
        {'id': 'log', 'kind': 'lut', 'name': 'Log Conversion', 'category': 'Utility'},
    ]


def test_search_folders_and_favorites_filter_catalog(qtbot: Any) -> None:
    panel = ColorLutBrowser()
    qtbot.addWidget(panel)
    panel.set_resources(entries())
    assert panel.list_widget.count() == 3
    panel.search_edit.setText('print')
    assert panel.list_widget.count() == 1
    assert panel.list_widget.item(0).text() == 'Warm Print'
    panel.search_edit.clear()
    panel._folder = 'Film'
    panel._populate()
    assert panel.list_widget.count() == 2
    panel._toggle_favorite('cool')
    panel.favorite_button.setChecked(True)
    assert panel.list_widget.count() == 1
    assert panel.list_widget.item(0).text() == '★ Cool Night'


def test_activation_requires_explicit_editable_target(qtbot: Any) -> None:
    panel = ColorLutBrowser()
    qtbot.addWidget(panel)
    panel.set_resources(entries())
    requested = []
    panel.resource_activated.connect(requested.append)
    item = panel.list_widget.item(0)
    panel._activate(item)
    assert requested == []
    panel.set_target('n1', '01 · Balance', True)
    panel._activate(item)
    assert len(requested) == 1 and requested[0]['kind'] == 'lut'
    panel.set_target('m1', 'Mixer', False, 'Select a corrector')
    panel._activate(item)
    assert len(requested) == 1


def test_strength_hydration_is_silent_and_user_edit_emits_intent(qtbot: Any) -> None:
    panel = ColorLutBrowser()
    qtbot.addWidget(panel)
    panel.set_resources(entries())
    panel.set_target('n1', '01', True)
    edits = []
    panel.draft_edit_requested.connect(lambda action, payload: edits.append((action, payload)))
    panel.set_current_lut('warm', 0.42)
    assert panel.strength_slider.value() == 420
    assert panel.strength_spin.value() == 0.42
    assert edits == []
    panel.strength_slider.setValue(600)
    assert edits == [('set_lut_strength', {'strength': 0.6})]
    panel._remove_lut()
    assert edits[-1] == ('remove_lut', {})
    assert not panel.strength_slider.isEnabled()


def test_mime_contains_identity_only_and_roundtrips() -> None:
    entry = {**entries()[0], 'path': '/private/local/secret-name.cube'}
    data = resource_mime_data(entry)
    decoded = decode_resource_mime(data)
    assert decoded == {'version': 1, 'kind': 'lut', 'resource_id': 'warm'}
    assert b'/private' not in bytes(data.data('application/x-topos-color-resource'))


def test_list_mode_and_thumbnail_zoom_preserve_selection(qtbot: Any) -> None:
    panel = ColorLutBrowser()
    qtbot.addWidget(panel)
    panel.resize(360, 520)
    panel.set_resources(entries())
    panel._select_resource('warm')
    panel._set_view_mode(False)
    assert panel.selected_resource_id() == 'warm'
    assert not panel.zoom_slider.isEnabled()
    panel._set_view_mode(True)
    panel.zoom_slider.setValue(140)
    assert panel.list_widget.iconSize().width() == 140
    assert panel.selected_resource_id() == 'warm'
