"""Mixer's dedicated browser uses the same effect IDs and routes as the timeline."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QDropEvent, QPalette
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QTreeWidgetItem
from pytestqt.qtbot import QtBot

from src.features.edit.core.effect_registry import get_effect_registry_health
from src.features.edit.ui.edit_effects_panel import MIME_TYPE_EFFECT
from src.features.mixer.effects import effect_catalog
from src.features.mixer.effects.production_dialog import PRODUCTION_BINDINGS
from src.features.mixer.ui.effects_panel import MixerEffectsPanel, _PANEL_ROLE
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
from src.shared.ui.effect_icons import AUDIO_EFFECT_ICON_DIR
from src.shared.ui.style_sheet import C


def _effects(browser: MixerEffectsPanel) -> dict[str, QTreeWidgetItem]:
    items = {}
    for index in range(browser.tree.topLevelItemCount()):
        group = browser.tree.topLevelItem(index)
        for row in range(group.childCount()):
            item = group.child(row)
            if effect_id := item.data(0, Qt.ItemDataRole.UserRole):
                items[effect_id] = item
    return items


@pytest.fixture
def browser(qtbot: QtBot) -> MixerEffectsPanel:
    panel = MixerEffectsPanel()
    qtbot.addWidget(panel)
    panel.resize(290, 540)
    panel.show()
    return panel


def test_lists_supported_audio_inserts_and_uses_project_mime(browser: MixerEffectsPanel) -> None:
    items = _effects(browser)
    native = {state.effect_type for state in get_effect_registry_health().effects if state.ok}
    assert set(items) == (native - {"noise_gate"}) | {"spatial_panner"}
    assert set(items) - {"spatial_panner"} == TrackMixerWidget._INSERT_EFFECT_TYPES
    effect = items["convolution_reverb"]
    payload = browser.tree.mimeData([effect])
    assert payload.hasFormat(MIME_TYPE_EFFECT)
    assert bytes(payload.data(MIME_TYPE_EFFECT)) == b"convolution_reverb"
    assert not browser.tree.mimeData([effect.parent()]).formats()


def test_effects_and_panel_tools_have_distinct_svg_icons(browser: MixerEffectsPanel) -> None:
    items = _effects(browser)
    for group_index in range(browser.tree.topLevelItemCount()):
        group = browser.tree.topLevelItem(group_index)
        for row in range(group.childCount()):
            item = group.child(row)
            if panel_id := item.data(0, _PANEL_ROLE):
                items[panel_id] = item

    rendered = set()
    for effect_id, item in items.items():
        assert QSvgRenderer(str(AUDIO_EFFECT_ICON_DIR / f"{effect_id}.svg")).isValid(), effect_id
        image = item.icon(0).pixmap(browser.tree.iconSize()).toImage()
        assert not image.isNull(), effect_id
        pixels = image.constBits().asstring(image.sizeInBytes())
        assert pixels not in rendered, effect_id
        rendered.add(pixels)


def test_effect_group_titles_are_brighter_than_effect_names(browser: MixerEffectsPanel) -> None:
    group = _effects(browser)["strip_eq"].parent()
    assert group.foreground(0).color() == QColor(C.TEXT_PRIMARY)
    assert browser.tree.palette().color(QPalette.ColorRole.Text) == QColor(C.TEXT_LABEL)


def test_all_clip_tools_route_to_the_project_without_insert_payloads(
    browser: MixerEffectsPanel, qtbot: QtBot,
) -> None:
    panels = {
        item.data(0, _PANEL_ROLE): item
        for index in range(browser.tree.topLevelItemCount())
        for group in [browser.tree.topLevelItem(index)]
        for row in range(group.childCount())
        if (item := group.child(row)).data(0, _PANEL_ROLE)
    }
    assert set(panels) == {"clip_editor", "declick_dehum", "pitch_correction", "doubler"}
    live = {panel_id for panel_id, _fields in PRODUCTION_BINDINGS.values()}
    assert live | panels.keys() == {entry.effect_id for entry in effect_catalog()}
    for effect_id, item in panels.items():
        assert not browser.tree.mimeData([item]).hasFormat(MIME_TYPE_EFFECT)
        with qtbot.waitSignal(browser.effect_activated) as tool:
            browser._activate(item, 0)
        assert tool.args == [effect_id]


def test_search_hides_unmatched_effects_and_restores_collapsed_groups(browser: MixerEffectsPanel) -> None:
    items = _effects(browser)
    group = items["dynamic_eq"].parent()
    group.setExpanded(False)

    browser.search.setText("dynamic_eq")
    assert [effect for effect, item in items.items() if not item.isHidden()] == ["dynamic_eq"]
    assert group.isExpanded()
    assert not browser.tree.isHidden()

    browser.search.setText("unmatched")
    assert browser.tree.isHidden()
    browser.search.clear()
    assert not group.isExpanded()
    assert not browser.tree.isHidden()
    assert all(not item.isHidden() for item in items.values())


def test_selection_can_activate_and_drag_to_an_existing_mixer_channel(
    browser: MixerEffectsPanel, qtbot: QtBot,
) -> None:
    effect = _effects(browser)["strip_eq"]
    browser.tree.setCurrentItem(effect)
    browser.tree.setFocus()
    with qtbot.waitSignal(browser.effect_activated) as selection:
        qtbot.keyClick(browser.tree, Qt.Key.Key_Return)
    assert selection.args == ["strip_eq"]

    strip = TrackMixerWidget("audio", "Dialogue", "A1")
    qtbot.addWidget(strip)
    payload = browser.tree.mimeData([effect])
    event = QDropEvent(
        QPointF(), Qt.DropAction.CopyAction, payload,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    with qtbot.waitSignal(strip.track_effects_committed) as insertion:
        strip.dropEvent(event)
    assert event.isAccepted()
    track_id, chain = insertion.args
    assert track_id == "audio"
    assert [instance.effect_type for instance in chain.effects] == ["strip_eq"]
