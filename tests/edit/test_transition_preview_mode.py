"""Tests for the optional animated transition-library presentation."""

from __future__ import annotations

from unittest.mock import MagicMock

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtGui import QMovie
from PyQt6.QtWidgets import QDockWidget

from src.shared.i18n.i18n import I18N
from src.features.edit.ui.edit_effects_panel import (
    EditEffectsPanel,
    _TransitionPreviewButton,
)
from src.features.edit.core.transition_catalog import TRANSITION_DEFINITIONS


def test_transition_library_defaults_to_static_icons(qapp, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
    panel = EditEffectsPanel()

    assert panel._transition_preview_mode == "icon"
    assert panel._transition_preview_size == "medium"
    assert len(panel._transition_preview_cards) >= 19
    assert all(
        card._preview_mode == "icon"
        for card in panel._transition_preview_cards
        if isinstance(card, _TransitionPreviewButton)
    )
    panel.close()


def test_transition_library_only_plays_visible_gifs_and_releases_on_hide(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = EditEffectsPanel()
    panel._config_manager.set = MagicMock()
    panel.resize(660, 280)
    panel.show()
    panel.tabs.setCurrentIndex(1)
    panel._set_transition_preview_mode("animated")
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

    panel.hide()
    QCoreApplication.processEvents()
    assert all(card._preview_movie is None for card in gif_cards)

    panel._set_transition_preview_mode("icon")
    QCoreApplication.processEvents()
    assert all(card._preview_movie is None for card in panel._transition_preview_cards)
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
    assert all(card.height() == 136 for card in large_cards)

    panel._set_transition_preview_size("small")
    small_cards = [
        card for card in panel._transition_preview_cards
        if isinstance(card, _TransitionPreviewButton) and card.has_preview
    ]
    assert all(card.preview_size == "small" for card in small_cards)
    assert all(card.iconSize().width() == 96 for card in small_cards)
    assert all(card.iconSize().height() == 54 for card in small_cards)
    assert all(card.height() == 78 for card in small_cards)
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
    favorite_key = (
        "edit.effects.remove_favorite"
        if panel._is_favorite("gaussian_blur")
        else "edit.effects.add_favorite"
    )
    favorite_fallback = (
        "Remove from Favorites"
        if panel._is_favorite("gaussian_blur")
        else "Add to Favorites"
    )
    assert I18N.tr(favorite_key, favorite_fallback) in effect_labels
    assert I18N.tr("dock.close", "Close") in effect_labels
    dock_action_label = (
        I18N.tr("dock.dock", "Dock")
        if dock.isFloating()
        else I18N.tr("dock.float", "Float")
    )
    assert dock_action_label in effect_labels

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
