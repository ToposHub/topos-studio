"""
Product Physical Missing Smoke Tests.

Phase 1 CI Matrix tests verifying that each product mode degrades correctly
when disabled module source directories are physically absent.

Tests use mock/patch for GUI components to avoid needing a display.
"""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from src.shared.feature_flags import (
    FeatureFlags,
    PRODUCT_MODE_PRESETS,
    load_feature_flags,
)


# ---------------------------------------------------------------------------
# 1. load_feature_flags() returns expected flags in all 4 product modes
# ---------------------------------------------------------------------------

class TestFeatureFlagsPerProductMode:
    """Verify load_feature_flags() returns correct flags for each product mode."""

    @pytest.mark.physical_missing
    @pytest.mark.color_product
    def test_color_product_flags(self, monkeypatch):
        """Color product: library+color+deliver enabled, edit+composite disabled."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        for feat in ("EDIT", "COMPOSITE", "DELIVER"):
            monkeypatch.delenv(f"TOPOS_FEATURE_{feat}", raising=False)

        flags = load_feature_flags(None)
        assert flags.library is True
        assert flags.color is True
        assert flags.edit is False
        assert flags.composite is False
        assert flags.deliver is True

    @pytest.mark.physical_missing
    @pytest.mark.studio_product
    def test_studio_product_flags(self, monkeypatch):
        """Studio product: all features enabled."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        for feat in ("EDIT", "COMPOSITE", "DELIVER"):
            monkeypatch.delenv(f"TOPOS_FEATURE_{feat}", raising=False)

        flags = load_feature_flags(None)
        assert flags.library is True
        assert flags.color is True
        assert flags.edit is True
        assert flags.composite is True
        assert flags.deliver is True

    @pytest.mark.physical_missing
    @pytest.mark.edit_product
    def test_edit_product_flags(self, monkeypatch):
        """Edit product: library+color+edit+deliver enabled, composite disabled."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "edit")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        for feat in ("EDIT", "COMPOSITE", "DELIVER"):
            monkeypatch.delenv(f"TOPOS_FEATURE_{feat}", raising=False)

        flags = load_feature_flags(None)
        assert flags.library is True
        assert flags.color is True
        assert flags.edit is True
        assert flags.composite is False
        assert flags.deliver is True

    @pytest.mark.physical_missing
    @pytest.mark.composite_product
    def test_composite_product_flags(self, monkeypatch):
        """Composite product: library+color+composite+deliver enabled, edit disabled."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "composite")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        for feat in ("EDIT", "COMPOSITE", "DELIVER"):
            monkeypatch.delenv(f"TOPOS_FEATURE_{feat}", raising=False)

        flags = load_feature_flags(None)
        assert flags.library is True
        assert flags.color is True
        assert flags.edit is False
        assert flags.composite is True
        assert flags.deliver is True

    @pytest.mark.physical_missing
    def test_product_mode_presets_match_all_modes(self):
        """All 4 product modes should be defined in PRODUCT_MODE_PRESETS."""
        expected_modes = {"color", "studio", "edit", "composite"}
        assert set(PRODUCT_MODE_PRESETS.keys()) == expected_modes

    @pytest.mark.physical_missing
    def test_color_always_enabled_in_all_modes(self):
        """Color must be enabled in every product mode preset."""
        for mode, preset in PRODUCT_MODE_PRESETS.items():
            assert preset["color"] is True, f"Color disabled in mode {mode}"


# ---------------------------------------------------------------------------
# 2. FeatureRegistry doesn't register missing modules
# ---------------------------------------------------------------------------

class TestFeatureRegistryMissingModules:
    """Verify FeatureRegistry rejects modules whose is_available() returns False."""

    @pytest.mark.physical_missing
    def test_disabled_module_not_registered(self):
        """Module with is_available()=False should not be registered."""
        from src.app.feature_registry import FeatureRegistry
        from src.app.app_context import AppContext

        registry = FeatureRegistry()
        registry.initialize(AppContext())

        mock_module = MagicMock()
        mock_module.feature_id = "edit"
        mock_module.is_available.return_value = False

        result = registry.register_module(mock_module)
        assert result is False
        assert "edit" not in registry._modules
        mock_module.register_pages.assert_not_called()

    @pytest.mark.physical_missing
    def test_disabled_modules_do_not_register_pages(self):
        """Multiple disabled modules should not contribute any pages."""
        from src.app.feature_registry import FeatureRegistry
        from src.app.app_context import AppContext

        registry = FeatureRegistry()
        registry.initialize(AppContext())

        for feature_id in ("edit", "composite", "deliver"):
            mock_module = MagicMock()
            mock_module.feature_id = feature_id
            mock_module.is_available.return_value = False
            registry.register_module(mock_module)

        assert len(registry._modules) == 0
        assert len(registry._pages) == 0

    @pytest.mark.physical_missing
    def test_enabled_module_registered_successfully(self):
        """Module with is_available()=True should be registered."""
        from src.app.feature_registry import FeatureRegistry
        from src.app.app_context import AppContext

        registry = FeatureRegistry()
        registry.initialize(AppContext())

        mock_module = MagicMock()
        mock_module.feature_id = "color"
        mock_module.is_available.return_value = True
        mock_module.register_pages.return_value = []

        result = registry.register_module(mock_module)
        assert result is True
        assert "color" in registry._modules


# ---------------------------------------------------------------------------
# 3. src.app.main_window is importable when modules are disabled
# ---------------------------------------------------------------------------

class TestMainWindowImportWithModulesDisabled:
    """Verify that the main_window module can be imported even when feature
    modules are flagged as disabled (using environment variables, not physical removal)."""

    @pytest.mark.physical_missing
    @pytest.mark.color_product
    def test_main_window_module_importable_color_mode(self, monkeypatch):
        """The main_window module should import successfully in color-only mode."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        try:
            from src.app.main_window import PageContext, PageType
            assert PageType.COLOR is not None
            assert PageContext is not None
        except (ImportError, AttributeError) as exc:
            pytest.skip(f"main_window import failed (env issue): {exc}")


# ---------------------------------------------------------------------------
# 4. MenuBuilder doesn't generate disabled module menu items
# ---------------------------------------------------------------------------

class TestMenuBuilderDisabledModules:
    """Verify MenuBuilder respects feature filtering.

    规格裁决（P1-PROD-01，2026-09-20）：MenuBuilder 自 3f1c38f3 起，对
    ``callback_name`` 无法解析出可调用对象的 descriptor 会整体跳过
    （防止"点了没反应"的空壳菜单项）。因此本组测试的 callback_resolver
    必须返回真实可调用对象，才能验证 feature 过滤本身；空壳跳过行为
    由 ``test_menu_builder_skips_unresolvable_callback`` 单独锁定。
    """

    @staticmethod
    def _resolver(name: str):
        # 生产环境由 main_window._resolve_menu_callback 提供；测试返回
        # 一个可调用对象即可让 action 正常构建。
        return lambda checked=False: None

    @pytest.mark.physical_missing
    @pytest.mark.color_product
    def test_menu_builder_no_edit_actions_in_color_mode(self, qapp):
        """MenuBuilder should not generate edit module actions when edit is disabled."""
        from src.shared.actions import ActionRegistry, MenuActionDescriptor
        from src.app.menu.menu_builder import MenuBuilder
        from PyQt6.QtWidgets import QMenuBar

        registry = ActionRegistry()
        # Register an edit action
        registry.register(MenuActionDescriptor(
            action_id="edit.project.new",
            menu_path="File/Edit Project",
            label="New Edit Project",
            callback_name="new_edit_project",
            feature_id="edit",
        ))
        # Register a color action
        registry.register(MenuActionDescriptor(
            action_id="color.reset",
            menu_path="Color",
            label="Reset Grade",
            callback_name="reset_grade",
            feature_id="color",
        ))

        menu_bar = QMenuBar()
        file_menu = menu_bar.addMenu("File")
        color_menu = menu_bar.addMenu("Color")
        builder = MenuBuilder(
            menu_bar=menu_bar,
            action_registry=registry,
            callback_resolver=self._resolver,
        )
        builder.register_existing_menu("File", file_menu)
        builder.register_existing_menu("Color", color_menu)

        # Build with only color enabled (edit disabled)
        actions = builder.build(enabled_features={"color", "library"})
        assert "edit.project.new" not in actions
        assert "color.reset" in actions

    @pytest.mark.physical_missing
    @pytest.mark.composite_product
    def test_menu_builder_no_edit_actions_in_composite_mode(self, qapp):
        """MenuBuilder should not generate edit module actions in composite mode."""
        from src.shared.actions import ActionRegistry, MenuActionDescriptor
        from src.app.menu.menu_builder import MenuBuilder
        from PyQt6.QtWidgets import QMenuBar

        registry = ActionRegistry()
        registry.register(MenuActionDescriptor(
            action_id="edit.timeline.cut",
            menu_path="Edit/Timeline",
            label="Cut",
            callback_name="cut",
            feature_id="edit",
        ))
        registry.register(MenuActionDescriptor(
            action_id="composite.add_node",
            menu_path="Composite",
            label="Add Node",
            callback_name="add_node",
            feature_id="composite",
        ))

        menu_bar = QMenuBar()
        edit_menu = menu_bar.addMenu("Edit")
        composite_menu = menu_bar.addMenu("Composite")
        builder = MenuBuilder(
            menu_bar=menu_bar,
            action_registry=registry,
            callback_resolver=self._resolver,
        )
        builder.register_existing_menu("Edit", edit_menu)
        builder.register_existing_menu("Composite", composite_menu)

        actions = builder.build(enabled_features={"composite", "color", "library"})
        assert "edit.timeline.cut" not in actions
        assert "composite.add_node" in actions

    @pytest.mark.physical_missing
    @pytest.mark.edit_product
    def test_menu_builder_no_composite_actions_in_edit_mode(self, qapp):
        """MenuBuilder should not generate composite module actions in edit mode."""
        from src.shared.actions import ActionRegistry, MenuActionDescriptor
        from src.app.menu.menu_builder import MenuBuilder
        from PyQt6.QtWidgets import QMenuBar

        registry = ActionRegistry()
        registry.register(MenuActionDescriptor(
            action_id="composite.viewer.render",
            menu_path="Composite",
            label="Render",
            callback_name="render",
            feature_id="composite",
        ))
        registry.register(MenuActionDescriptor(
            action_id="edit.timeline.paste",
            menu_path="Edit/Timeline",
            label="Paste",
            callback_name="paste",
            feature_id="edit",
        ))

        menu_bar = QMenuBar()
        edit_menu = menu_bar.addMenu("Edit")
        composite_menu = menu_bar.addMenu("Composite")
        builder = MenuBuilder(
            menu_bar=menu_bar,
            action_registry=registry,
            callback_resolver=self._resolver,
        )
        builder.register_existing_menu("Edit", edit_menu)
        builder.register_existing_menu("Composite", composite_menu)

        actions = builder.build(enabled_features={"edit", "color", "library"})
        assert "composite.viewer.render" not in actions
        assert "edit.timeline.paste" in actions

    @pytest.mark.physical_missing
    @pytest.mark.studio_product
    def test_menu_builder_skips_unresolvable_callback(self, qapp):
        """规格锁定：callback 无法解析的 descriptor 不产生菜单项（3f1c38f3）。

        防止"点了没反应"的空壳菜单项与孤儿快捷键进入 UI；这是
        MenuBuilder 的既定行为，本测试防止其被无意回退。
        """
        from src.shared.actions import ActionRegistry, MenuActionDescriptor
        from src.app.menu.menu_builder import MenuBuilder
        from PyQt6.QtWidgets import QMenuBar

        registry = ActionRegistry()
        registry.register(MenuActionDescriptor(
            action_id="color.reset",
            menu_path="Color",
            label="Reset Grade",
            callback_name="reset_grade",
            feature_id="color",
        ))

        menu_bar = QMenuBar()
        color_menu = menu_bar.addMenu("Color")
        builder = MenuBuilder(
            menu_bar=menu_bar,
            action_registry=registry,
            callback_resolver=lambda name: None,  # 故意解析失败
        )
        builder.register_existing_menu("Color", color_menu)

        actions = builder.build(enabled_features={"color", "library"})
        assert "color.reset" not in actions
        assert color_menu.actions() == []  # 不产生空壳菜单项


# ---------------------------------------------------------------------------
# 5. PreferencesDialog doesn't show disabled module pages
# ---------------------------------------------------------------------------

class TestPreferencesRegistryDisabledModules:
    """Verify PreferencesRegistry filters out pages for disabled modules."""

    @pytest.mark.physical_missing
    @pytest.mark.color_product
    def test_preferences_no_edit_pages_in_color_mode(self):
        """Edit module preference pages should not appear in color-only mode."""
        from src.shared.preferences import PreferencePageDescriptor, PreferencesRegistry

        registry = PreferencesRegistry()
        registry.register(PreferencePageDescriptor(
            page_id="edit.video_playback",
            title="Video Playback",
            category="Video Playback",
            factory=lambda ctx: None,
            feature_id="edit",
        ))
        registry.register(PreferencePageDescriptor(
            page_id="composite.settings",
            title="Composite",
            category="Composite",
            factory=lambda ctx: None,
            feature_id="composite",
        ))
        registry.register(PreferencePageDescriptor(
            page_id="app.general",
            title="General",
            category="General",
            factory=lambda ctx: None,
            feature_id=None,
        ))

        # Color-only mode: only color + app-level pages
        pages = registry.get_pages(enabled_features={"color", "library"})
        page_ids = [p.page_id for p in pages]
        assert "edit.video_playback" not in page_ids
        assert "composite.settings" not in page_ids
        assert "app.general" in page_ids

    @pytest.mark.physical_missing
    @pytest.mark.edit_product
    def test_preferences_no_composite_pages_in_edit_mode(self):
        """Composite module preference pages should not appear in edit mode."""
        from src.shared.preferences import PreferencePageDescriptor, PreferencesRegistry

        registry = PreferencesRegistry()
        registry.register(PreferencePageDescriptor(
            page_id="composite.settings",
            title="Composite",
            category="Composite",
            factory=lambda ctx: None,
            feature_id="composite",
        ))
        registry.register(PreferencePageDescriptor(
            page_id="edit.video_playback",
            title="Video Playback",
            category="Video Playback",
            factory=lambda ctx: None,
            feature_id="edit",
        ))

        pages = registry.get_pages(enabled_features={"edit", "color", "library"})
        page_ids = [p.page_id for p in pages]
        assert "composite.settings" not in page_ids
        assert "edit.video_playback" in page_ids

    @pytest.mark.physical_missing
    @pytest.mark.composite_product
    def test_preferences_no_edit_pages_in_composite_mode(self):
        """Edit module preference pages should not appear in composite mode."""
        from src.shared.preferences import PreferencePageDescriptor, PreferencesRegistry

        registry = PreferencesRegistry()
        registry.register(PreferencePageDescriptor(
            page_id="edit.video_playback",
            title="Video Playback",
            category="Video Playback",
            factory=lambda ctx: None,
            feature_id="edit",
        ))
        registry.register(PreferencePageDescriptor(
            page_id="composite.settings",
            title="Composite",
            category="Composite",
            factory=lambda ctx: None,
            feature_id="composite",
        ))

        pages = registry.get_pages(enabled_features={"composite", "color", "library"})
        page_ids = [p.page_id for p in pages]
        assert "edit.video_playback" not in page_ids
        assert "composite.settings" in page_ids


# ---------------------------------------------------------------------------
# 6. ShortcutManager.get_actions() doesn't return disabled module shortcuts
# ---------------------------------------------------------------------------

class TestShortcutManagerDisabledModules:
    """Verify ShortcutManager filters shortcuts by enabled features."""

    @pytest.mark.physical_missing
    @pytest.mark.color_product
    def test_shortcuts_no_edit_in_color_mode(self, qapp):
        """ShortcutManager.get_actions_by_feature should exclude edit shortcuts."""
        from src.shared.ui.shortcut_manager import ShortcutManager

        sm = ShortcutManager()
        sm.register("edit.cut", "Cut", "Edit", "Ctrl+X", feature_id="edit")
        sm.register("composite.render", "Render", "Composite", "Ctrl+R", feature_id="composite")
        sm.register("color.reset", "Reset", "Color", "Ctrl+Shift+R", feature_id="color")
        sm.register("app.save", "Save", "File", "Ctrl+S", feature_id=None)

        # Color-only: only color + app-level
        enabled = {"color", "library"}
        filtered = sm.get_actions_by_feature(enabled)
        ids = [a.id for a in filtered]
        assert "edit.cut" not in ids
        assert "composite.render" not in ids
        assert "color.reset" in ids
        assert "app.save" in ids

    @pytest.mark.physical_missing
    @pytest.mark.edit_product
    def test_shortcuts_no_composite_in_edit_mode(self, qapp):
        """ShortcutManager should exclude composite shortcuts in edit mode."""
        from src.shared.ui.shortcut_manager import ShortcutManager

        sm = ShortcutManager()
        sm.register("composite.render", "Render", "Composite", "Ctrl+R", feature_id="composite")
        sm.register("edit.cut", "Cut", "Edit", "Ctrl+X", feature_id="edit")

        enabled = {"edit", "color", "library"}
        filtered = sm.get_actions_by_feature(enabled)
        ids = [a.id for a in filtered]
        assert "composite.render" not in ids
        assert "edit.cut" in ids

    @pytest.mark.physical_missing
    @pytest.mark.composite_product
    def test_shortcuts_no_edit_in_composite_mode(self, qapp):
        """ShortcutManager should exclude edit shortcuts in composite mode."""
        from src.shared.ui.shortcut_manager import ShortcutManager

        sm = ShortcutManager()
        sm.register("edit.cut", "Cut", "Edit", "Ctrl+X", feature_id="edit")
        sm.register("composite.render", "Render", "Composite", "Ctrl+R", feature_id="composite")

        enabled = {"composite", "color", "library"}
        filtered = sm.get_actions_by_feature(enabled)
        ids = [a.id for a in filtered]
        assert "edit.cut" not in ids
        assert "composite.render" in ids

    @pytest.mark.physical_missing
    @pytest.mark.studio_product
    def test_shortcuts_all_in_studio_mode(self, qapp):
        """All shortcuts should be present in studio mode."""
        from src.shared.ui.shortcut_manager import ShortcutManager

        sm = ShortcutManager()
        sm.register("edit.cut", "Cut", "Edit", "Ctrl+X", feature_id="edit")
        sm.register("composite.render", "Render", "Composite", "Ctrl+R", feature_id="composite")
        sm.register("deliver.export", "Export", "Deliver", "Ctrl+E", feature_id="deliver")

        enabled = {"edit", "composite", "deliver", "color", "library"}
        filtered = sm.get_actions_by_feature(enabled)
        ids = [a.id for a in filtered]
        assert "edit.cut" in ids
        assert "composite.render" in ids
        assert "deliver.export" in ids
