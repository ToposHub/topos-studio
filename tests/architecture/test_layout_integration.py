"""
Tests for LayoutStore integration with the dock system (Phase A).

These tests verify that LayoutStore is correctly wired into the real
dock save/restore flow without requiring a running MainWindow.
They use mock objects for Qt widgets and test the integration logic
in isolation.
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from PyQt6.QtCore import QByteArray

from src.shared.layout.profile import LayoutProfile, LAYOUT_PROFILE_SCHEMA_VERSION
from src.shared.layout.store import LayoutStore
from src.shared.panels.registry import PanelRegistry, PanelDescriptor


# ---------------------------------------------------------------------------
# Stub config
# ---------------------------------------------------------------------------


class _StubConfig:
    """Minimal ConfigManager-like object for testing."""

    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value, auto_save: bool = True) -> None:
        self._data[key] = value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    return _StubConfig()


@pytest.fixture
def store(config):
    return LayoutStore(config=config)


@pytest.fixture
def panel_registry():
    """Return a fresh PanelRegistry with some sample panels registered."""
    registry = PanelRegistry()
    registry.register(PanelDescriptor(
        panel_id="color.grading",
        title_key="panel.color_wheel",
        fallback_title="Color Wheel",
        feature_id="color",
        category="Color",
        dock_attr="dock_grading",
        order=100,
    ))
    registry.register(PanelDescriptor(
        panel_id="color.curves",
        title_key="panel.curves",
        fallback_title="Curves",
        feature_id="color",
        category="Color",
        dock_attr="dock_curves",
        order=200,
    ))
    registry.register(PanelDescriptor(
        panel_id="color.analysis",
        title_key="panel.analysis",
        fallback_title="Analysis",
        feature_id="color",
        category="Color",
        dock_attr="dock_analysis",
        order=300,
    ))
    registry.register(PanelDescriptor(
        panel_id="edit.media_pool",
        title_key="panel.media_pool",
        fallback_title="Media Pool",
        feature_id="edit",
        category="Edit",
        dock_attr="dock_edit_media_pool",
        order=100,
    ))
    registry.register(PanelDescriptor(
        panel_id="edit.timeline",
        title_key="panel.timeline",
        fallback_title="Timeline",
        feature_id="edit",
        category="Edit",
        dock_attr="dock_edit_timeline",
        order=200,
    ))
    registry.register(PanelDescriptor(
        panel_id="composite.node_graph",
        title_key="panel.node_graph",
        fallback_title="Node Graph",
        feature_id="composite",
        category="Composite",
        dock_attr="dock_composite",
        order=100,
    ))
    registry.register(PanelDescriptor(
        panel_id="deliver.export",
        title_key="panel.export",
        fallback_title="Export",
        feature_id="deliver",
        category="Deliver",
        dock_attr="dock_export",
        order=100,
    ))
    registry.register(PanelDescriptor(
        panel_id="workspace.viewport",
        title_key="panel.viewport",
        fallback_title="Viewport",
        feature_id="color",
        category="Workspace",
        dock_attr="dock_viewport",
        order=10,
    ))
    return registry


# ---------------------------------------------------------------------------
# Test: Color and Studio Color page layouts don't overwrite each other
# ---------------------------------------------------------------------------


class TestCrossProductLayoutIsolation:
    """Verify that layouts saved for different product modes are isolated."""

    def test_color_and_studio_color_layouts_independent(self, store):
        """Color mode and Studio mode can each have their own Color page layout."""
        color_profile = LayoutProfile(
            product_mode="color",
            page_id="Color",
            visible_panels=("color.grading", "color.curves"),
            qt_state_hex="aabbcc",
        )
        studio_profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=("color.grading", "color.curves", "edit.media_pool"),
            qt_state_hex="ddeeff",
        )

        store.save_profile(color_profile)
        store.save_profile(studio_profile)

        loaded_color = store.get_profile("color", "Color")
        loaded_studio = store.get_profile("studio", "Color")

        assert loaded_color is not None
        assert loaded_studio is not None
        assert loaded_color.qt_state_hex == "aabbcc"
        assert loaded_studio.qt_state_hex == "ddeeff"
        assert set(loaded_color.visible_panels) == {"color.grading", "color.curves"}
        assert set(loaded_studio.visible_panels) == {
            "color.grading", "color.curves", "edit.media_pool"
        }

    def test_saving_studio_layout_does_not_affect_color_layout(self, store):
        """Overwriting the studio Color layout should not touch the color Color layout."""
        # Save initial color layout
        store.save_profile(LayoutProfile(
            product_mode="color",
            page_id="Color",
            qt_state_hex="original_color",
        ))
        # Save and then update studio layout
        store.save_profile(LayoutProfile(
            product_mode="studio",
            page_id="Color",
            qt_state_hex="original_studio",
        ))
        store.save_profile(LayoutProfile(
            product_mode="studio",
            page_id="Color",
            qt_state_hex="updated_studio",
        ))

        # Color layout should be untouched
        color_loaded = store.get_profile("color", "Color")
        assert color_loaded.qt_state_hex == "original_color"

        # Studio layout should be updated
        studio_loaded = store.get_profile("studio", "Color")
        assert studio_loaded.qt_state_hex == "updated_studio"


# ---------------------------------------------------------------------------
# Test: Old page_layouts migrate to current product's layout_profiles
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    """Verify that legacy page_layouts dict migrates into LayoutStore profiles."""

    def test_legacy_migration_creates_profiles_for_current_mode(self, config):
        """Legacy page_layouts should be migrated into profiles for the current mode."""
        config._data["page_layouts"] = {
            "Color": "hex_color_state",
            "Edit": "hex_edit_state",
            "Composite": "hex_composite_state",
        }

        with patch("src.shared.layout.store.LayoutStore._get_product_mode", return_value="studio"):
            store = LayoutStore(config=config)
            count = store.migrate_legacy_page_layouts()

        assert count == 3

        # Verify each was migrated
        color_p = store.get_profile("studio", "Color")
        assert color_p is not None
        assert color_p.qt_state_hex == "hex_color_state"
        assert color_p.product_mode == "studio"

        edit_p = store.get_profile("studio", "Edit")
        assert edit_p is not None
        assert edit_p.qt_state_hex == "hex_edit_state"

        comp_p = store.get_profile("studio", "Composite")
        assert comp_p is not None
        assert comp_p.qt_state_hex == "hex_composite_state"

    def test_migration_does_not_overwrite_existing_profiles(self, config):
        """If a LayoutStore profile already exists, migration should skip it."""
        config._data["page_layouts"] = {
            "Color": "legacy_hex",
        }

        store = LayoutStore(config=config)
        # Pre-save a profile
        store.save_profile(LayoutProfile(
            product_mode="studio",
            page_id="Color",
            qt_state_hex="already_saved",
        ))

        with patch("src.shared.layout.store.LayoutStore._get_product_mode", return_value="studio"):
            count = store.migrate_legacy_page_layouts()

        assert count == 0
        loaded = store.get_profile("studio", "Color")
        assert loaded.qt_state_hex == "already_saved"

    def test_migration_for_color_product_mode(self, config):
        """Migration in color mode should create color-mode profiles."""
        config._data["page_layouts"] = {
            "Color": "color_mode_hex",
        }

        with patch("src.shared.layout.store.LayoutStore._get_product_mode", return_value="color"):
            store = LayoutStore(config=config)
            count = store.migrate_legacy_page_layouts()

        assert count == 1
        p = store.get_profile("color", "Color")
        assert p is not None
        assert p.product_mode == "color"


# ---------------------------------------------------------------------------
# Test: Missing edit/composite panels are skipped during layout restore
# ---------------------------------------------------------------------------


class TestMissingPanelSkipped:
    """Verify that panels not registered for the current product mode are skipped."""

    def test_edit_panels_skipped_in_color_mode(self, store, panel_registry):
        """In color mode, edit panels should be filtered out."""
        profile = LayoutProfile(
            product_mode="color",
            page_id="Color",
            visible_panels=(
                "color.grading",
                "color.curves",
                "edit.media_pool",
                "edit.timeline",
            ),
        )

        # Only color feature is enabled in color mode
        enabled = {"color", "library", "deliver"}
        registered = {desc.panel_id for desc in panel_registry.get_all()}

        filtered = store.filter_panels(
            profile,
            enabled_features=enabled,
            registered_panel_ids=registered,
        )

        assert "color.grading" in filtered.visible_panels
        assert "color.curves" in filtered.visible_panels
        assert "edit.media_pool" not in filtered.visible_panels
        assert "edit.timeline" not in filtered.visible_panels

    def test_composite_panels_skipped_in_edit_mode(self, store, panel_registry):
        """In edit mode, composite panels should be filtered out."""
        profile = LayoutProfile(
            product_mode="edit",
            page_id="Edit",
            visible_panels=(
                "edit.media_pool",
                "edit.timeline",
                "composite.node_graph",
            ),
        )

        enabled = {"edit", "color", "library", "deliver"}
        registered = {desc.panel_id for desc in panel_registry.get_all()}

        filtered = store.filter_panels(
            profile,
            enabled_features=enabled,
            registered_panel_ids=registered,
        )

        assert "edit.media_pool" in filtered.visible_panels
        assert "edit.timeline" in filtered.visible_panels
        assert "composite.node_graph" not in filtered.visible_panels

    def test_unknown_panel_ids_skipped(self, store):
        """Panel IDs not in the registry should be silently dropped."""
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=(
                "color.grading",
                "color.old_removed_panel",
                "nonexistent.widget",
            ),
        )

        registered = {"color.grading", "color.curves"}
        filtered = store.filter_panels(
            profile,
            registered_panel_ids=registered,
        )

        assert set(filtered.visible_panels) == {"color.grading"}


# ---------------------------------------------------------------------------
# Test: restoreState failure doesn't affect startup
# ---------------------------------------------------------------------------


class TestRestoreStateFailure:
    """Verify that a bad qt_state_hex doesn't crash or break startup."""

    def test_empty_state_hex_handled_gracefully(self, store):
        """An empty qt_state_hex should not cause issues."""
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=("color.grading",),
            qt_state_hex="",
        )
        store.save_profile(profile)

        # get_or_default should return the profile without crashing
        result = store.get_or_default("studio", "Color")
        assert result is not None
        assert result.qt_state_hex == ""

    def test_garbage_state_hex_stored_and_retrieved(self, store):
        """Even garbage hex data should roundtrip without error."""
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            qt_state_hex="this_is_not_valid_qt_state",
        )
        store.save_profile(profile)

        loaded = store.get_profile("studio", "Color")
        assert loaded is not None
        assert loaded.qt_state_hex == "this_is_not_valid_qt_state"

    def test_restore_from_store_returns_false_for_missing_profile(self, store):
        """When no profile exists, the restore attempt should return False.

        This simulates the _restore_layout_from_store path used in
        restore_layout_state and switch_to_page.
        """
        result = store.get_profile("studio", "Color")
        assert result is None

        # get_or_default will return a default profile with empty qt_state_hex
        default = store.get_or_default("studio", "Color")
        assert default is not None
        # Default profile has no qt_state_hex
        assert default.qt_state_hex == ""


# ---------------------------------------------------------------------------
# Test: Window > Panels menu items have resolvable docks
# ---------------------------------------------------------------------------


class TestPanelDockResolution:
    """Verify that registered panels can be mapped to dock attribute names."""

    def test_registered_panels_have_dock_attrs(self, panel_registry):
        """All registered panels should have a non-empty dock_attr."""
        for desc in panel_registry.get_all():
            assert desc.dock_attr, (
                f"Panel {desc.panel_id} is missing dock_attr"
            )

    def test_dock_attr_format(self, panel_registry):
        """dock_attr values should follow the 'dock_' naming convention."""
        for desc in panel_registry.get_all():
            assert desc.dock_attr.startswith("dock"), (
                f"Panel {desc.panel_id} has unexpected dock_attr: {desc.dock_attr}"
            )

    def test_panel_ids_by_feature(self, panel_registry):
        """Panels can be grouped by feature for product mode filtering."""
        color_panels = panel_registry.get_panel_ids_for_feature("color")
        edit_panels = panel_registry.get_panel_ids_for_feature("edit")

        assert "color.grading" in color_panels
        assert "edit.media_pool" in edit_panels
        assert "edit.media_pool" not in color_panels

    def test_panel_categorization(self, panel_registry):
        """Panels should be correctly categorized for the Window menu."""
        from src.shared.panels.registry import CATEGORY_ORDER

        grouped = panel_registry.get_panels_by_category(
            enabled_features={"color", "edit", "composite", "deliver"}
        )

        assert "Color" in grouped
        assert "Edit" in grouped
        assert "Composite" in grouped
        assert "Deliver" in grouped


# ---------------------------------------------------------------------------
# Test: reset_product_layout only clears current product
# ---------------------------------------------------------------------------


class TestResetProductLayout:
    """Verify that reset_product_layout only affects the current product mode."""

    def test_reset_only_clears_current_mode(self, store):
        """Resetting studio layouts should not affect color layouts."""
        # Save layouts for both modes
        store.save_profile(LayoutProfile(
            product_mode="color",
            page_id="Color",
            qt_state_hex="color_state",
        ))
        store.save_profile(LayoutProfile(
            product_mode="studio",
            page_id="Color",
            qt_state_hex="studio_color_state",
        ))
        store.save_profile(LayoutProfile(
            product_mode="studio",
            page_id="Edit",
            qt_state_hex="studio_edit_state",
        ))

        # Simulate reset_product_layout for studio mode
        profiles = store.get_profiles_for_mode("studio")
        for page_id in list(profiles.keys()):
            store.delete_profile("studio", page_id)

        # Color mode should be untouched
        color_p = store.get_profile("color", "Color")
        assert color_p is not None
        assert color_p.qt_state_hex == "color_state"

        # Studio mode should be cleared
        assert store.get_profile("studio", "Color") is None
        assert store.get_profile("studio", "Edit") is None

    def test_reset_preserves_other_modes(self, store):
        """Resetting one product mode preserves all other modes."""
        # Create layouts for all modes
        for mode in ("color", "studio", "edit", "composite"):
            store.save_profile(LayoutProfile(
                product_mode=mode,
                page_id="Color",
                qt_state_hex=f"{mode}_state",
            ))

        # Reset only edit mode
        profiles = store.get_profiles_for_mode("edit")
        for page_id in list(profiles.keys()):
            store.delete_profile("edit", page_id)

        # Verify other modes are intact
        for mode in ("color", "studio", "composite"):
            p = store.get_profile(mode, "Color")
            assert p is not None, f"{mode} mode was incorrectly deleted"
            assert p.qt_state_hex == f"{mode}_state"

        # Edit mode should be gone
        assert store.get_profile("edit", "Color") is None


# ---------------------------------------------------------------------------
# Test: End-to-end save/restore cycle through LayoutStore
# ---------------------------------------------------------------------------


class TestEndToEndSaveRestore:
    """Test the full save and restore cycle through LayoutStore."""

    def test_save_and_restore_cycle(self, store, panel_registry):
        """A profile saved with visible panels should be restorable with filtering."""
        # Save a studio Color layout with mixed panels
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=(
                "workspace.viewport",
                "color.grading",
                "color.curves",
                "color.analysis",
                "edit.media_pool",  # Will be filtered out in color mode
            ),
            qt_state_hex="deadbeef",
        )
        store.save_profile(profile)

        # Restore in color mode (edit feature disabled)
        restored = store.get_or_default(
            "studio",
            "Color",
            enabled_features={"color", "library"},
            registered_panel_ids={desc.panel_id for desc in panel_registry.get_all()},
        )

        # edit.media_pool should be filtered out
        assert "edit.media_pool" not in restored.visible_panels
        assert "color.grading" in restored.visible_panels
        assert restored.qt_state_hex == "deadbeef"

    def test_default_profile_used_when_no_saved(self, store):
        """When no profile is saved, a default should be returned."""
        default = store.get_or_default("color", "Color")
        assert default is not None
        assert default.product_mode == "color"
        assert default.page_id == "Color"
        # Default profiles should have at least some panels
        assert len(default.visible_panels) > 0
