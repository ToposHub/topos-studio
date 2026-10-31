"""Regression test: EditEffectsPanel must inject custom startDrag.

The panel defines four ``_inject_*_drag_support`` module-level helpers
that replace each tree's ``startDrag`` so drags carry the application
MIME types (``application/x-topos-effect`` etc.) the timeline routes on.
These helpers used to be dead code — defined but never called — which
meant the trees fell back to QTreeWidget's default ``startDrag`` and
emitted a generic model-data MIME the timeline couldn't recognise.
Effect drags were then misrouted into the external-file-import branch
and rejected with "no project set".

This test verifies the injection happens at panel construction time.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QMovie

from src.features.edit.ui.edit_effects_panel import (
    EditEffectsPanel,
    _MotionGraphicPreviewButton,
    _ResponsiveEffectSection,
)
from src.features.teaching_animation.core.motion_graphic_generator_registry import (
    get_motion_graphic_generator_registry,
)
from src.features.teaching_animation.core.motion_graphic_template_library import (
    discover_motion_graphic_template_packages,
)
from src.shared.ui.style_sheet import C


def test_video_tree_startdrag_is_replaced(qapp):
    """The video effects tree's startDrag must be a custom callable, not
    QTreeWidget's bound default. The default does not set our MIME type.
    """
    panel = EditEffectsPanel()
    # QTreeWidget.startDrag is a bound method from C++; after injection it
    # becomes a Python function. We can't compare types directly across
    # PyQt versions, but the injected one is a plain function whose
    # ``__name__`` is ``custom_start_drag`` (set at def time in the helper).
    sd = panel.video_effects_tree.startDrag
    assert hasattr(sd, "__name__")
    assert sd.__name__ == "custom_start_drag", (
        f"video_effects_tree.startDrag should be the injected "
        f"custom_start_drag, got {sd.__name__!r} — drag injection broken"
    )


def test_audio_tree_startdrag_is_replaced(qapp):
    panel = EditEffectsPanel()
    sd = panel.audio_effects_tree.startDrag
    assert sd.__name__ == "custom_start_drag"


def test_favorites_tree_startdrag_is_replaced(qapp):
    panel = EditEffectsPanel()
    sd = panel.favorites_tree.startDrag
    assert sd.__name__ == "custom_start_drag"


def test_transitions_tree_startdrag_is_replaced(qapp):
    panel = EditEffectsPanel()
    sd = panel.transitions_tree.startDrag
    assert sd.__name__ == "custom_start_drag"


def test_drag_enabled_on_all_trees(qapp):
    """Sanity: each tree must have drag enabled or startDrag never fires."""
    panel = EditEffectsPanel()
    assert panel.video_effects_tree.dragEnabled() is True
    assert panel.audio_effects_tree.dragEnabled() is True
    assert panel.favorites_tree.dragEnabled() is True
    assert panel.transitions_tree.dragEnabled() is True


def test_transition_tree_uses_svg_preview_icons(qapp):
    """Every draggable transition entry has a loaded SVG preview icon."""
    panel = EditEffectsPanel()
    tree = panel.transitions_tree
    transition_items = []
    for index in range(tree.topLevelItemCount()):
        category = tree.topLevelItem(index)
        transition_items.extend(
            category.child(child_index)
            for child_index in range(category.childCount())
            if category.child(child_index).data(0, Qt.ItemDataRole.UserRole) == "transition"
        )

    assert len(transition_items) >= 17
    assert all(not item.icon(0).isNull() for item in transition_items)


def test_transitions_use_full_label_and_responsive_grid(qapp, monkeypatch, tmp_path):
    """Transitions share the modern grid and no longer use the short tab label."""
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
    panel = EditEffectsPanel()
    panel.resize(560, 780)
    panel.show()
    panel.tabs.setCurrentIndex(1)
    qapp.processEvents()

    assert panel.tabs.tabText(1) == "Transitions"
    assert all(
        panel.transitions_tree.itemWidget(
            panel.transitions_tree.topLevelItem(index), 0
        ).column_count == 2
        for index in range(panel.transitions_tree.topLevelItemCount())
    )


def test_video_effect_tree_uses_svg_preview_icons(qapp):
    """Every visible video effect entry has a loaded SVG preview icon."""
    panel = EditEffectsPanel()
    tree = panel.video_effects_tree
    effect_items = []
    for index in range(tree.topLevelItemCount()):
        category = tree.topLevelItem(index)
        effect_items.extend(
            category.child(child_index)
            for child_index in range(category.childCount())
            if category.child(child_index).data(0, Qt.ItemDataRole.UserRole) == "effect"
        )

    assert len(effect_items) >= 13
    assert all(not item.icon(0).isNull() for item in effect_items)


def test_audio_effect_tree_uses_svg_preview_icons(qapp):
    """Every mixer effect entry has a loaded SVG preview icon."""
    panel = EditEffectsPanel()
    category = panel.audio_effects_tree.topLevelItem(0)
    assert category is not None
    effect_items = [
        category.child(index)
        for index in range(category.childCount())
        if category.child(index).data(0, Qt.ItemDataRole.UserRole) == "effect"
    ]
    assert len(effect_items) >= 8
    assert all(not item.icon(0).isNull() for item in effect_items)


def test_effect_section_adapts_between_one_two_and_three_columns(qapp):
    """The responsive grid uses the expected Resolve-style breakpoints."""
    section = _ResponsiveEffectSection("Audio")
    section.show()
    section.resize(320, 100)
    qapp.processEvents()
    assert section.column_count == 1
    section.resize(520, 100)
    qapp.processEvents()
    assert section.column_count == 2
    section.resize(720, 100)
    qapp.processEvents()
    assert section.column_count == 3


def test_effect_panel_uses_the_global_edit_panel_background(qapp):
    """All visible effect surfaces must use the shared Edit panel token."""
    panel = EditEffectsPanel()
    panel_color = C.BG_PANEL
    section = panel.video_effects_tree.itemWidget(
        panel.video_effects_tree.topLevelItem(0), 0
    )
    card = section._cards[0]

    assert panel_color in panel.styleSheet()
    assert panel_color in panel.video_effects_tree.styleSheet()
    assert panel_color in panel.tabs.styleSheet()
    assert panel_color in section.styleSheet()
    assert panel_color in card.styleSheet()


def test_mg_generators_are_video_cards_with_hover_gif_preview(qapp):
    """MG is a Video section, and a packaged GIF starts only on hover."""
    registry = get_motion_graphic_generator_registry()
    registry.clear()
    package = next(
        item for item in discover_motion_graphic_template_packages()
        if item.template.template_id == "particle-flow-background"
    )
    registry.register(
        package.template,
        preview_path=package.preview_path,
        packaged=True,
    )
    panel = EditEffectsPanel()
    panel.show()
    qapp.processEvents()

    assert not hasattr(panel, "motion_graphic_parameter_form_host")
    assert not hasattr(panel, "motion_graphic_parameter_label")
    assert panel._motion_graphic_parameter_values(package.template) == (
        package.template.default_params()
    )
    # C1: Titles tab 加入效果面板
    assert [panel.tabs.tabText(index) for index in range(panel.tabs.count())] == [
        "Video", "Transitions", "Titles", "Audio", "Favorites",
    ]
    sections = [
        panel.video_effects_tree.itemWidget(
            panel.video_effects_tree.topLevelItem(index), 0,
        )
        for index in range(panel.video_effects_tree.topLevelItemCount())
    ]
    from src.shared.i18n.i18n import I18N
    mg_title = I18N.tr("edit.effects.mg_section", "MG Generators")
    mg_section = next(section for section in sections if section.title == mg_title)
    card = mg_section._cards[0]
    assert isinstance(card, _MotionGraphicPreviewButton)

    card._show_preview()
    qapp.processEvents()
    assert card._preview_popup is not None
    assert card._preview_movie is not None
    assert card._preview_movie.state() == QMovie.MovieState.Running

    card._hide_preview()
    qapp.processEvents()
    assert card._preview_popup is None
    assert card._preview_movie is None
    registry.clear()


def test_effect_search_is_hidden_until_toolbar_icon_is_clicked(qapp):
    """Search stays out of the compact header until explicitly requested."""
    panel = EditEffectsPanel()
    assert panel._search_visible is False
    assert panel.search_container.isHidden() is True

    panel._toggle_search_visibility()
    assert panel._search_visible is True
    assert panel.search_container.isHidden() is False

    panel._toggle_search_visibility()
    assert panel._search_visible is False
    assert panel.search_container.isHidden() is True
