"""Mixer's dedicated browser uses the same effect IDs and routes as the timeline."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QTreeWidgetItem
from pytestqt.qtbot import QtBot

from src.features.edit.core.effect_registry import get_effect_registry_health
from src.features.edit.ui.edit_effects_panel import MIME_TYPE_EFFECT
from src.features.mixer.ui.effects_panel import MixerEffectsPanel
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget


def _effects(browser: MixerEffectsPanel) -> dict[str, QTreeWidgetItem]:
    items = {}
    for index in range(browser.tree.topLevelItemCount()):
        group = browser.tree.topLevelItem(index)
        for row in range(group.childCount()):
            item = group.child(row)
            items[item.data(0, Qt.ItemDataRole.UserRole)] = item
    return items


@pytest.fixture
def browser(qtbot: QtBot) -> MixerEffectsPanel:
    panel = MixerEffectsPanel()
    qtbot.addWidget(panel)
    panel.resize(290, 540)
    panel.show()
    return panel


def test_lists_only_supported_audio_effects_and_uses_project_mime(browser: MixerEffectsPanel) -> None:
    items = _effects(browser)
    native = {state.effect_type for state in get_effect_registry_health().effects if state.ok}
    assert set(items) == (native - {"noise_gate"}) | {"spatial_panner"}
    assert set(items) - {"spatial_panner"} == TrackMixerWidget._INSERT_EFFECT_TYPES
    effect = items["convolution_reverb"]
    payload = browser.tree.mimeData([effect])
    assert payload.hasFormat(MIME_TYPE_EFFECT)
    assert bytes(payload.data(MIME_TYPE_EFFECT)) == b"convolution_reverb"
    assert not browser.tree.mimeData([effect.parent()]).formats()


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
