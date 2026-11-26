"""Tests for the optional animated transition-library presentation."""

from __future__ import annotations

from unittest.mock import MagicMock

from PyQt6.QtCore import QCoreApplication, QPoint, QSize, Qt
from PyQt6.QtGui import QColor, QMovie
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDockWidget

from src.shared.i18n.i18n import I18N
from src.features.edit.ui.edit_effects_panel import (
    EditEffectsPanel,
    _TransitionPreviewButton,
    _transition_preview_icon,
)
from src.features.edit.core.transition_catalog import TRANSITION_DEFINITIONS


def test_transition_library_defaults_to_static_icons(qapp, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
    panel = EditEffectsPanel()

    assert panel._transition_preview_mode == "icon"
    assert panel._transition_preview_size == "small"
    assert len(panel._transition_preview_cards) >= 19
    assert all(
        card._preview_mode == "icon"
        for card in panel._transition_preview_cards
        if isinstance(card, _TransitionPreviewButton)
    )
    panel.close()


def test_built_in_transitions_have_distinct_static_icons(qapp: QApplication) -> None:
    size = QSize(24, 24)
    fallback = _transition_preview_icon("").pixmap(size).toImage()
    rendered = set()
    for definition in TRANSITION_DEFINITIONS:
        effect_type = definition.transition_type
        image = _transition_preview_icon(effect_type).pixmap(size).toImage()
        assert not image.isNull() and image != fallback, effect_type
        assert image.pixelColor(0, 0).alpha() == 0, effect_type
        pixels = image.constBits().asstring(image.sizeInBytes())
        assert pixels not in rendered, effect_type
        rendered.add(pixels)


def test_effects_header_and_body_have_distinct_backgrounds(qapp: QApplication) -> None:
    panel = EditEffectsPanel()
    panel.resize(660, 360)
    panel.show()
    qapp.processEvents()
    snapshot = panel.grab()
    pixels = snapshot.toImage()
    dpr = snapshot.devicePixelRatio()
    bar = panel.tabs.tabBar()
    header_y = bar.mapTo(panel, QPoint(0, bar.height() // 2)).y()
    gap_x = round((panel.width() - 55) * dpr)
    assert pixels.pixelColor(gap_x, round(header_y * dpr)) == QColor("#202024")
    assert pixels.pixelColor(gap_x, round((panel.height() - 8) * dpr)) == QColor("#27272b")
    for index, color in ((0, "#27272b"), (1, "#202024")):
        sample = bar.mapTo(panel, bar.tabRect(index).topLeft() + QPoint(3, 3))
        assert pixels.pixelColor(round(sample.x() * dpr), round(sample.y() * dpr)) == QColor(color)

    pane_y = panel.tabs.currentWidget().mapTo(panel, QPoint()).y()
    divider_y = round((pane_y - 1) * dpr)
    assert all(
        pixels.pixelColor(round(x * dpr), divider_y) == QColor("#0d0d0f")
        for x in (10, panel.width() // 2, panel.width() - 10)
    )
    panel.close()


def test_transition_library_only_plays_visible_gifs_and_releases_on_hide(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = EditEffectsPanel()
    panel._config_manager.set = MagicMock()
    panel.resize(660, 280)
    panel.show()
    panel.tabs.setCurrentIndex(1)
    panel._set_transition_preview_mode("animated")
    panel._set_transition_preview_size("medium")
    QCoreApplication.processEvents()

    all_cards = [
        card for card in panel._transition_preview_cards
        if isinstance(card, _TransitionPreviewButton)
    ]
    gif_cards = [card for card in all_cards if card.has_preview]
    active_cards = [card for card in gif_cards if card._preview_movie is not None]
    # Animated previews are optional. New transitions still have static SVG
    # icons when an animated preview has not been authored yet.
    available_previews = sum(
        panel._transition_preview_path(definition.transition_type) is not None
        for definition in TRANSITION_DEFINITIONS
    )
    assert len(gif_cards) == available_previews + 2
    assert 0 < len(active_cards) <= 24
    assert all(
        card.height() == panel._transition_preview_geometry()[2]
        for card in active_cards
    )
    assert all(card.is_preview_playing for card in active_cards)
    tree = panel.transitions_tree
    section = tree.itemWidget(tree.topLevelItem(0), 0)
    assert section.column_count == 4
    first = section.grid.itemAtPosition(0, 0).widget()
    second = section.grid.itemAtPosition(0, 1).widget()
    below = section.grid.itemAtPosition(1, 0).widget()
    assert first.width() == second.width() == 128
    assert second.x() - (first.x() + first.width()) == 3
    assert below.y() - (first.y() + first.height()) == 3
    assert first._poster_rect().size() == QSize(128, 72)
    snapshot = panel.grab()
    pixels = snapshot.toImage()
    dpr = snapshot.devicePixelRatio()
    origin = first.mapTo(panel, QPoint())
    assert pixels.pixelColor(round(origin.x() * dpr), round(origin.y() * dpr)) == QColor("#27272b")
    assert pixels.pixelColor(round((origin.x() + 8) * dpr), round(origin.y() * dpr)) != QColor("#27272b")

    panel.resize(290, 280)
    QCoreApplication.processEvents()
    assert section.column_count == 2

    panel.hide()
    QCoreApplication.processEvents()
    assert all(card._preview_movie is None for card in gif_cards)

    panel._set_transition_preview_mode("icon")
    QCoreApplication.processEvents()
    assert all(card._preview_movie is None for card in panel._transition_preview_cards)
    panel.close()


def test_preview_size_slider_follows_the_animated_tab_and_existing_presets(qapp: QApplication) -> None:
    panel = EditEffectsPanel()
    panel._config_manager.set = MagicMock()
    panel.resize(470, 350)
    panel.show()
    panel.tabs.setCurrentIndex(1)
    assert panel.transition_size_slider.isHidden()
    panel._set_transition_preview_mode("animated")
    panel._set_transition_preview_size("small")
    qapp.processEvents()

    slider = panel.transition_size_slider
    assert slider.isVisible()
    assert slider.mapTo(panel, slider.rect().topRight()).x() < (
        panel.search_toggle_button.mapTo(panel, QPoint()).x()
    )
    snapshot = panel.grab()
    actions = panel.tabs.cornerWidget(Qt.Corner.TopRightCorner)
    sample = actions.mapTo(panel, QPoint(1, 1))
    dpr = snapshot.devicePixelRatio()
    assert snapshot.toImage().pixelColor(round(sample.x() * dpr), round(sample.y() * dpr)) == QColor("#202024")
    QTest.keyClick(slider, Qt.Key.Key_Right)
    assert panel._transition_preview_size == "medium"
    assert panel._transition_preview_geometry()[:2] == (128, 72)
    panel._config_manager.set.assert_called_with("transition_preview_size", "medium")
    panel._set_transition_preview_size("large")
    assert slider.value() == slider.maximum()
    panel.tabs.setCurrentIndex(0)
    assert slider.isHidden()
    panel.close()


def test_transition_preview_size_presets_update_cards(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = EditEffectsPanel()
    panel._config_manager.set = MagicMock()
    panel._set_transition_preview_mode("animated")

    panel._set_transition_preview_size("large")
    large_cards = [
        card for card in panel._transition_preview_cards
        if isinstance(card, _TransitionPreviewButton) and card.has_preview
    ]
    assert large_cards
    assert all(card.preview_size == "large" for card in large_cards)
    assert all(card.iconSize().width() == 192 for card in large_cards)
    assert all(card.iconSize().height() == 108 for card in large_cards)
    assert all(card.size() == QSize(192, 126) for card in large_cards)

    panel._set_transition_preview_size("small")
    small_cards = [
        card for card in panel._transition_preview_cards
        if isinstance(card, _TransitionPreviewButton) and card.has_preview
    ]
    assert all(card.preview_size == "small" for card in small_cards)
    assert all(card.iconSize().width() == 96 for card in small_cards)
    assert all(card.iconSize().height() == 54 for card in small_cards)
    assert all(card.size() == QSize(96, 72) for card in small_cards)
    panel.close()


def test_effects_context_menu_unifies_preview_size_and_dock_actions(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = EditEffectsPanel()
    dock = QDockWidget("Effects")
    dock.setWidget(panel)

    effect_menu = panel._build_library_context_menu_for_type("gaussian_blur")
    effect_labels = [
        action.text() for action in effect_menu.actions()
        if not action.isSeparator()
    ]
    assert I18N.tr("dock.close", "Close") in effect_labels
    dock_action_label = (
        I18N.tr("dock.dock", "Dock")
        if dock.isFloating()
        else I18N.tr("dock.float", "Float")
    )
    assert dock_action_label in effect_labels
    assert set(effect_labels) == {I18N.tr("dock.close", "Close"), dock_action_label}

    transition_menu = panel._build_library_context_menu_for_type(
        "cross_dissolve",
        is_transition=True,
        preview_stem="cross_dissolve",
    )
    transition_actions = transition_menu.actions()
    transition_labels = [
        action.text() for action in transition_actions
        if not action.isSeparator()
    ]
    assert I18N.tr(
        "edit.effects.transition_icon_preview", "Icon Preview"
    ) in transition_labels
    assert I18N.tr(
        "edit.effects.transition_animated_preview", "Animated Preview"
    ) in transition_labels
    size_action = next(
        action for action in transition_actions
        if action.text() == I18N.tr(
            "edit.effects.transition_preview_size", "GIF Preview Size"
        )
    )
    assert size_action.menu() is not None
    assert {
        I18N.tr("edit.effects.transition_preview_small", "Small"),
        I18N.tr("edit.effects.transition_preview_medium", "Medium"),
        I18N.tr("edit.effects.transition_preview_large", "Large"),
    }.issubset({action.text() for action in size_action.menu().actions()})
    assert I18N.tr("dock.close", "Close") in transition_labels
    assert dock_action_label in transition_labels

    missing_preview_menu = panel._build_library_context_menu_for_type(
        "custom_missing_preview",
        is_transition=True,
        preview_stem="custom_missing_preview",
    )
    missing_preview_actions = getattr(missing_preview_menu, "_preview_actions")
    assert missing_preview_actions[0].isEnabled()
    assert not missing_preview_actions[1].isEnabled()

    panel.close()
    dock.deleteLater()
