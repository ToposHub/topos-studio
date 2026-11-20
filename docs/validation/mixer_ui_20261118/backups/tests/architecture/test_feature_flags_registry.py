"""
Tests for FeatureFlags and FeatureRegistry functionality.

These tests verify that feature flags work correctly without requiring
GUI components or OpenGL context.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.shared.feature_flags import FeatureFlags, DEFAULT_FEATURE_FLAGS, load_feature_flags
from src.app.feature_registry import FeatureRegistry
from src.app.app_context import AppContext
from src.app.page_contract import PageDescriptor


class TestFeatureFlagsBasic:
    """F-001: Basic FeatureFlags functionality tests."""

    def test_default_flags_all_enabled(self):
        """Default feature flags should have all features enabled."""
        flags = FeatureFlags()
        assert flags.library == True
        assert flags.color == True
        assert flags.edit == True
        assert flags.composite == True
        assert flags.deliver == True

    def test_custom_flags_respected(self):
        """Custom feature flags should be respected."""
        flags = FeatureFlags(edit=False, composite=False, deliver=False)
        assert flags.is_enabled("edit") == False
        assert flags.is_enabled("composite") == False
        assert flags.is_enabled("deliver") == False
        assert flags.is_enabled("library") == True  # Not specified, default True

    def test_color_always_enabled(self):
        """F-003: Color feature should always be enabled regardless of flag value."""
        flags = FeatureFlags(color=False)
        assert flags.is_enabled("color") == True  # Color is always True

    def test_is_enabled_for_unknown_feature_returns_true(self):
        """Unknown features should return True by default (permissive)."""
        flags = FeatureFlags()
        assert flags.is_enabled("unknown_feature") == True

    def test_get_enabled_features(self):
        """get_enabled_features should return list of enabled features."""
        flags = FeatureFlags(edit=False, composite=False)
        enabled = flags.get_enabled_features()
        assert "library" in enabled
        assert "color" in enabled
        assert "edit" not in enabled
        assert "composite" not in enabled
        assert "deliver" in enabled

    def test_get_disabled_features(self):
        """get_disabled_features should return list of disabled features."""
        flags = FeatureFlags(edit=False, deliver=False)
        disabled = flags.get_disabled_features()
        assert "edit" in disabled
        assert "deliver" in disabled
        assert "color" not in disabled  # Color can't be disabled
        assert "library" not in disabled
        assert "composite" not in disabled

    def test_is_available_alias(self):
        """is_available should be an alias for is_enabled."""
        flags = FeatureFlags(edit=False)
        assert flags.is_available("edit") == False
        assert flags.is_available("color") == True

    def test_frozen_dataclass(self):
        """FeatureFlags should be immutable (frozen dataclass)."""
        flags = FeatureFlags()
        with pytest.raises(AttributeError):
            flags.edit = False


class TestLoadFeatureFlags:
    """Test load_feature_flags function."""

    def test_load_from_config_dict(self):
        """Should load flags from config dict."""
        config = {"features": {"edit": False, "composite": False}}
        flags = load_feature_flags(config)
        assert flags.edit == False
        assert flags.composite == False
        assert flags.color == True  # Always True

    def test_load_from_config_with_get_method(self):
        """Should load flags from config object with get method."""
        mock_config = MagicMock()
        mock_config.get.return_value = {"edit": False}
        flags = load_feature_flags(mock_config)
        assert flags.edit == False

    def test_load_from_environment_variables(self, monkeypatch):
        """Should load flags from environment variables."""
        monkeypatch.setenv("TOPOS_FEATURE_EDIT", "false")
        monkeypatch.setenv("TOPOS_FEATURE_DELIVER", "0")
        monkeypatch.setenv("TOPOS_FEATURE_LIBRARY", "disabled")

        flags = load_feature_flags(None)
        assert flags.edit == False
        assert flags.deliver == False
        assert flags.library == False

    def test_env_overrides_config(self, monkeypatch):
        """Environment variables should override config values."""
        config = {"features": {"edit": True}}
        monkeypatch.setenv("TOPOS_FEATURE_EDIT", "false")

        flags = load_feature_flags(config)
        assert flags.edit == False

    def test_color_cannot_be_disabled_via_config(self):
        """Color feature should not be disabled even if config says so."""
        config = {"features": {"color": False}}
        flags = load_feature_flags(config)
        assert flags.color == True

    def test_color_cannot_be_disabled_via_env(self, monkeypatch):
        """Color feature should not be disabled even if env says so."""
        monkeypatch.setenv("TOPOS_FEATURE_COLOR", "false")
        flags = load_feature_flags(None)
        assert flags.color == True


class TestFeatureFlagPrecedence:
    """Test that feature flag precedence is correct:
    TOPOS_FEATURE_<NAME> > TOPOS_DISABLE_FEATURES > config > default.
    """

    def test_explicit_env_overrides_bulk_disable_edit(self, monkeypatch):
        """TOPOS_FEATURE_EDIT=true should override TOPOS_DISABLE_FEATURES=edit."""
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "edit,composite")
        monkeypatch.setenv("TOPOS_FEATURE_EDIT", "true")
        monkeypatch.delenv("TOPOS_FEATURE_COMPOSITE", raising=False)

        flags = load_feature_flags(None)
        assert flags.edit is True   # Explicit env overrides bulk disable
        assert flags.composite is False  # No explicit env, stays disabled

    def test_bulk_disable_overrides_config(self, monkeypatch):
        """TOPOS_DISABLE_FEATURES should override config values."""
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "edit")
        config = {"features": {"edit": True}}

        flags = load_feature_flags(config)
        assert flags.edit is False  # Bulk disable overrides config

    def test_config_overrides_default(self):
        """Config values should override defaults."""
        config = {"features": {"edit": False}}
        flags = load_feature_flags(config)
        assert flags.edit is False

    def test_full_precedence_chain(self, monkeypatch):
        """Full precedence: explicit env > bulk disable > config > default."""
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "edit,deliver")
        monkeypatch.setenv("TOPOS_FEATURE_EDIT", "true")  # Re-enable edit
        config = {"features": {"edit": False, "deliver": True}}

        flags = load_feature_flags(config)
        assert flags.edit is True    # Explicit env wins
        assert flags.deliver is False  # Bulk disable wins (no explicit env)
        assert flags.color is True   # Always enabled

    def test_color_always_enabled_despite_all_disable_attempts(self, monkeypatch):
        """Color should remain enabled even with all disable mechanisms."""
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "color")
        monkeypatch.setenv("TOPOS_FEATURE_COLOR", "false")
        config = {"features": {"color": False}}

        flags = load_feature_flags(config)
        assert flags.color is True


class TestFeatureRegistryBasic:
    """F-002: Test FeatureRegistry functionality."""

    def test_registry_initialization(self):
        """Registry should initialize with empty state."""
        registry = FeatureRegistry()
        assert registry._modules == {}
        assert registry._pages == {}
        assert registry._context is None

    def test_registry_initialize_with_context(self):
        """Registry should accept AppContext on initialize."""
        registry = FeatureRegistry()
        context = AppContext()
        registry.initialize(context)
        assert registry._context is context

    def test_register_module_calls_is_available(self):
        """Registry should check is_available before registering module."""
        registry = FeatureRegistry()
        registry.initialize(AppContext())
        
        mock_module = MagicMock()
        mock_module.feature_id = "test_feature"
        mock_module.is_available.return_value = False
        
        result = registry.register_module(mock_module)
        assert result == False
        assert "test_feature" not in registry._modules

    def test_register_module_registers_when_available(self):
        """Registry should register module when is_available returns True."""
        registry = FeatureRegistry()
        registry.initialize(AppContext())
        
        mock_module = MagicMock()
        mock_module.feature_id = "test_feature"
        mock_module.is_available.return_value = True
        mock_module.register_pages.return_value = []
        
        result = registry.register_module(mock_module)
        assert result == True
        assert "test_feature" in registry._modules

    def test_register_page(self):
        """Registry should register page descriptors."""
        registry = FeatureRegistry()
        
        page = PageDescriptor(
            page_id="test_page",
            title="Test Page",
            factory=lambda ctx: None,
        )
        registry.register_page(page)
        
        assert "test_page" in registry._pages
        assert registry.get_page("test_page") == page

    def test_get_all_pages_sorted(self):
        """get_all_pages should return pages sorted by page_id."""
        registry = FeatureRegistry()
        
        page_a = PageDescriptor(page_id="b_page", title="B", factory=lambda ctx: None)
        page_b = PageDescriptor(page_id="a_page", title="A", factory=lambda ctx: None)
        
        registry.register_page(page_a)
        registry.register_page(page_b)
        
        pages = registry.get_all_pages()
        assert pages[0].page_id == "a_page"
        assert pages[1].page_id == "b_page"

    def test_is_feature_available(self):
        """is_feature_available should check module availability."""
        registry = FeatureRegistry()
        registry.initialize(AppContext())
        
        mock_module = MagicMock()
        mock_module.feature_id = "test"
        mock_module.is_available.return_value = True
        mock_module.register_pages.return_value = []
        
        registry.register_module(mock_module)
        assert registry.is_feature_available("test") == True
        assert registry.is_feature_available("nonexistent") == False

    def test_get_available_features(self):
        """get_available_features should return list of available feature IDs."""
        registry = FeatureRegistry()
        registry.initialize(AppContext())
        
        mock_module1 = MagicMock()
        mock_module1.feature_id = "feature_a"
        mock_module1.is_available.return_value = True
        mock_module1.register_pages.return_value = []
        
        mock_module2 = MagicMock()
        mock_module2.feature_id = "feature_b"
        mock_module2.is_available.return_value = False
        
        registry.register_module(mock_module1)
        registry.register_module(mock_module2)
        
        available = registry.get_available_features()
        assert "feature_a" in available
        assert "feature_b" not in available


class TestDisabledModulesDoNotRegisterPages:
    """F-002: Disabled optional modules should not register pages."""

    def test_disabled_module_not_registered(self):
        """When module is_available returns False, it should not register pages."""
        registry = FeatureRegistry()
        registry.initialize(AppContext())
        
        mock_module = MagicMock()
        mock_module.feature_id = "disabled_feature"
        mock_module.is_available.return_value = False
        
        result = registry.register_module(mock_module)
        
        assert result == False
        mock_module.register_pages.assert_not_called()
        assert "disabled_feature" not in registry._modules

    def test_enabled_module_registers_pages(self):
        """When module is_available returns True, it should register pages."""
        registry = FeatureRegistry()
        registry.initialize(AppContext())
        
        mock_page = MagicMock(spec=PageDescriptor)
        mock_page.page_id = "test_page"
        mock_page.title = "Test Page"
        
        mock_module = MagicMock()
        mock_module.feature_id = "enabled_feature"
        mock_module.is_available.return_value = True
        mock_module.register_pages.return_value = [mock_page]
        
        result = registry.register_module(mock_module)
        
        assert result == True
        mock_module.register_pages.assert_called_once()
        assert registry.get_page("test_page") == mock_page

    def test_disabled_feature_via_flags_skips_module_import(self):
        """
        When feature flag is disabled, module_loader should not attempt import.
        
        This is tested by verifying module_loader checks flags before importing.
        """
        from src.app import module_loader
        
        mock_registry = MagicMock()
        mock_registry._context = MagicMock()
        
        disabled_flags = FeatureFlags(
            edit=False, composite=False, deliver=False, ai_workflow=False,
        )
        mock_registry._context.feature_flags = disabled_flags
        
        module_loader.register_builtin_modules(mock_registry)
        
        mock_registry.register_module.assert_not_called()


    def test_bulk_disable_still_works_without_explicit_override(self, monkeypatch):
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "composite")
        monkeypatch.delenv("TOPOS_FEATURE_COMPOSITE", raising=False)
        flags = load_feature_flags(None)
        assert flags.composite is False


class TestColorAlwaysRegistered:
    """F-003: Color feature should always be available and registered."""

    def test_color_feature_cannot_be_disabled(self):
        """Color feature should return True regardless of flag setting."""
        flags_with_color_false = FeatureFlags(color=False)
        assert flags_with_color_false.is_enabled("color") == True

    def test_color_module_is_available_always_true(self):
        """ColorModule.is_available should always return True."""
        from src.features.color.module import ColorModule
        
        module = ColorModule()
        assert module.is_available() == True

    def test_color_in_default_flags(self):
        """Color should be in DEFAULT_FEATURE_FLAGS with True value."""
        assert "color" in DEFAULT_FEATURE_FLAGS
        assert DEFAULT_FEATURE_FLAGS["color"] == True

    def test_load_feature_flags_preserves_color(self, monkeypatch):
        """load_feature_flags should preserve color=True even when disabled."""
        config = {"features": {"color": False}}
        monkeypatch.setenv("TOPOS_FEATURE_COLOR", "false")
        
        flags = load_feature_flags(config)
        assert flags.color == True


class TestModuleImportNoGuiTrigger:
    """F-004: Module import should not trigger deep GUI imports."""

    def test_feature_flags_import_no_gui(self):
        """Importing FeatureFlags should not import PyQt6 or GUI modules."""
        import sys
        
        modules_before = set(sys.modules.keys())
        
        from src.shared.feature_flags import FeatureFlags
        
        modules_after = set(sys.modules.keys())
        new_modules = modules_after - modules_before
        
        gui_modules = [m for m in new_modules if 'PyQt6' in m or 'PySide' in m]
        assert gui_modules == [], f"GUI modules imported: {gui_modules}"

    def test_feature_registry_import_no_gui(self):
        """Importing FeatureRegistry should not import PyQt6 or GUI modules."""
        import sys
        
        modules_before = set(sys.modules.keys())
        
        from src.app.feature_registry import FeatureRegistry
        
        modules_after = set(sys.modules.keys())
        new_modules = modules_after - modules_before
        
        gui_modules = [m for m in new_modules if 'PyQt6' in m or 'PySide' in m]
        assert gui_modules == [], f"GUI modules imported: {gui_modules}"

    def test_app_context_import_no_gui(self):
        """Importing AppContext should not import PyQt6 or GUI modules."""
        import sys
        
        modules_before = set(sys.modules.keys())
        
        from src.app.app_context import AppContext
        
        modules_after = set(sys.modules.keys())
        new_modules = modules_after - modules_before
        
        gui_modules = [m for m in new_modules if 'PyQt6' in m or 'PySide' in m]
        assert gui_modules == [], f"GUI modules imported: {gui_modules}"

    def test_page_contract_import_no_gui(self):
        """Importing PageContract should not import PyQt6 or GUI modules."""
        import sys
        
        modules_before = set(sys.modules.keys())
        
        from src.app.page_contract import PageDescriptor, FeatureModule
        
        modules_after = set(sys.modules.keys())
        new_modules = modules_after - modules_before
        
        gui_modules = [m for m in new_modules if 'PyQt6' in m or 'PySide' in m]
        assert gui_modules == [], f"GUI modules imported: {gui_modules}"

    def test_module_loader_import_no_gui(self):
        """Importing module_loader should not import PyQt6 or GUI modules."""
        import sys
        
        modules_before = set(sys.modules.keys())
        
        from src.app import module_loader
        
        modules_after = set(sys.modules.keys())
        new_modules = modules_after - modules_before
        
        gui_modules = [m for m in new_modules if 'PyQt6' in m or 'PySide' in m]
        assert gui_modules == [], f"GUI modules imported: {gui_modules}"


class TestFeatureModuleIntegration:
    """Integration tests for FeatureModule and FeatureRegistry."""

    def test_module_with_pages_registers_correctly(self):
        """A module with pages should register all pages."""
        registry = FeatureRegistry()
        registry.initialize(AppContext())
        
        mock_page1 = MagicMock(spec=PageDescriptor)
        mock_page1.page_id = "page1"
        mock_page1.title = "Page 1"
        
        mock_page2 = MagicMock(spec=PageDescriptor)
        mock_page2.page_id = "page2"
        mock_page2.title = "Page 2"
        
        mock_module = MagicMock()
        mock_module.feature_id = "multi_page_module"
        mock_module.is_available.return_value = True
        mock_module.register_pages.return_value = [mock_page1, mock_page2]
        
        registry.register_module(mock_module)
        
        assert registry.get_page("page1") == mock_page1
        assert registry.get_page("page2") == mock_page2

    def test_module_actions_registered_with_context(self):
        """Module actions should be registered with the context."""
        context = AppContext()
        registry = FeatureRegistry()
        registry.initialize(context)
        
        mock_module = MagicMock()
        mock_module.feature_id = "test"
        mock_module.is_available.return_value = True
        mock_module.register_pages.return_value = []
        
        registry.register_module(mock_module)
        
        mock_module.register_actions.assert_called_once_with(context)

    def test_save_handlers_not_registered_without_router(self):
        """Save handlers should not be registered if save_router is None."""
        context = AppContext(save_router=None)
        registry = FeatureRegistry()
        registry.initialize(context)
        
        mock_module = MagicMock()
        mock_module.feature_id = "test"
        mock_module.is_available.return_value = True
        mock_module.register_pages.return_value = []
        
        registry.register_module(mock_module)
        
        mock_module.register_save_handlers.assert_not_called()

    def test_save_handlers_registered_with_router(self):
        """Save handlers should be registered if save_router exists."""
        mock_router = MagicMock()
        context = AppContext(save_router=mock_router)
        registry = FeatureRegistry()
        registry.initialize(context)
        
        mock_module = MagicMock()
        mock_module.feature_id = "test"
        mock_module.is_available.return_value = True
        mock_module.register_pages.return_value = []
        
        registry.register_module(mock_module)
        
        mock_module.register_save_handlers.assert_called_once_with(context)

    def test_duplicate_module_registration_rejected(self):
        """Registering same module twice should be rejected."""
        registry = FeatureRegistry()
        registry.initialize(AppContext())
        
        mock_module = MagicMock()
        mock_module.feature_id = "duplicate"
        mock_module.is_available.return_value = True
        mock_module.register_pages.return_value = []
        
        result1 = registry.register_module(mock_module)
        result2 = registry.register_module(mock_module)
        
        assert result1 == True
        assert result2 == False

    def test_duplicate_page_registration_rejected(self):
        """Registering same page twice should be rejected."""
        registry = FeatureRegistry()
        
        page = PageDescriptor(
            page_id="duplicate",
            title="Duplicate",
            factory=lambda ctx: None,
        )
        
        registry.register_page(page)
        registry.register_page(page)  # Should not raise, just ignore
        
        assert registry.get_page("duplicate") == page


class TestProductMode:
    """Test TOPOS_PRODUCT_MODE support for color-only vs studio modes."""

    def test_color_mode_disables_edit_and_composite(self, monkeypatch):
        """Color product mode should disable edit and composite."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        monkeypatch.delenv("TOPOS_FEATURE_EDIT", raising=False)
        monkeypatch.delenv("TOPOS_FEATURE_COMPOSITE", raising=False)

        flags = load_feature_flags(None)
        assert flags.color is True
        assert flags.library is True
        assert flags.deliver is True
        assert flags.edit is False
        assert flags.composite is False

    def test_full_product_name_color_mode_disables_edit_and_composite(self, monkeypatch):
        """Packaged product names should resolve to the same presets as short names."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "topos-color")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        monkeypatch.delenv("TOPOS_FEATURE_EDIT", raising=False)
        monkeypatch.delenv("TOPOS_FEATURE_COMPOSITE", raising=False)

        flags = load_feature_flags(None)
        assert flags.color is True
        assert flags.deliver is True
        assert flags.edit is False
        assert flags.composite is False

    def test_studio_mode_enables_all(self, monkeypatch):
        """Studio product mode should enable all features."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)

        flags = load_feature_flags(None)
        assert flags.color is True
        assert flags.edit is True
        assert flags.composite is True
        assert flags.deliver is True
        assert flags.library is True

    def test_studio_is_default(self, monkeypatch):
        """Default (no TOPOS_PRODUCT_MODE) should be studio-equivalent (all enabled)."""
        monkeypatch.delenv("TOPOS_PRODUCT_MODE", raising=False)
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)

        flags = load_feature_flags(None)
        assert flags.edit is True
        assert flags.composite is True

    def test_explicit_env_overrides_color_mode(self, monkeypatch):
        """Explicit TOPOS_FEATURE_* should override color mode defaults."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        monkeypatch.setenv("TOPOS_FEATURE_EDIT", "true")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)

        flags = load_feature_flags(None)
        assert flags.edit is True  # Explicit env overrides color mode
        assert flags.composite is False  # Not overridden, stays disabled

    def test_bulk_disable_overrides_color_mode(self, monkeypatch):
        """TOPOS_DISABLE_FEATURES should override color mode (additive disable)."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "deliver")
        monkeypatch.delenv("TOPOS_FEATURE_DELIVER", raising=False)

        flags = load_feature_flags(None)
        assert flags.edit is False  # Color mode default
        assert flags.composite is False  # Color mode default
        assert flags.deliver is False  # Bulk disable overrides color mode

    def test_color_mode_color_always_enabled(self, monkeypatch):
        """Even in color mode, color cannot be disabled."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        monkeypatch.setenv("TOPOS_FEATURE_COLOR", "false")

        flags = load_feature_flags(None)
        assert flags.color is True

    def test_get_product_mode(self, monkeypatch):
        """get_product_mode should return the correct mode."""
        from src.shared.feature_flags import get_product_mode

        monkeypatch.delenv("TOPOS_PRODUCT_MODE", raising=False)
        assert get_product_mode() == "studio"

        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        assert get_product_mode() == "color"

        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "topos-composite")
        assert get_product_mode() == "composite"

    def test_unknown_product_mode_uses_defaults(self, monkeypatch):
        """Unknown product mode should fall through to defaults (all enabled)."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "unknown_mode")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)

        flags = load_feature_flags(None)
        # Unknown mode doesn't apply any preset, so defaults are used
        assert flags.color is True
        assert flags.edit is True
        assert flags.composite is True


class TestAiCapabilityFlags:
    """M0 freeze: AI/automation capability flags must default to off and only
    turn on via an explicit env var or explicit config entry.

    These flags gate every AI/automation surface (analysis, cloud upload,
    generation, MCP). The whole point of the M0 baseline (see
    docs/ai_assistant_agent_harness_product_plan_2026-08-01.md §11.1) is that
    importing the app or switching product mode must NEVER silently enable an
    AI capability. If any of these defaults to True, the safety boundary is
    broken.
    """

    def test_ai_capability_flags_constant_lists_all_five(self):
        """AI_CAPABILITY_FLAGS must enumerate exactly the five M0 flags."""
        from src.shared.feature_flags import AI_CAPABILITY_FLAGS
        assert set(AI_CAPABILITY_FLAGS) == {
            "ai_assistant", "media_analysis", "cloud_analysis",
            "generation", "mcp_server",
        }

    def test_default_flags_all_ai_off(self):
        """Default FeatureFlags must have every AI capability flag off."""
        from src.shared.feature_flags import AI_CAPABILITY_FLAGS
        flags = FeatureFlags()
        for name in AI_CAPABILITY_FLAGS:
            assert getattr(flags, name) is False, f"{name} should default to False"
            assert flags.is_enabled(name) is False, f"is_enabled({name!r}) should be False"

    def test_ai_flags_in_default_flags_registry(self):
        """Each AI capability must be registered in DEFAULT_FEATURE_FLAGS as False."""
        for name in ("ai_assistant", "media_analysis", "cloud_analysis",
                     "generation", "mcp_server"):
            assert name in DEFAULT_FEATURE_FLAGS
            assert DEFAULT_FEATURE_FLAGS[name] is False

    def test_ai_flags_off_in_every_product_mode(self, monkeypatch):
        """No product mode preset may enable an AI capability."""
        from src.shared.feature_flags import AI_CAPABILITY_FLAGS, PRODUCT_MODE_PRESETS
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        for mode in PRODUCT_MODE_PRESETS:
            for name in AI_CAPABILITY_FLAGS:
                # presets are static dicts; assert directly so a future edit
                # cannot silently flip an AI flag on for any mode.
                assert PRODUCT_MODE_PRESETS[mode].get(name) is False, (
                    f"{name} must be False in product mode {mode!r}"
                )
            # and the loader must agree with the preset
            monkeypatch.setenv("TOPOS_PRODUCT_MODE", mode)
            flags = load_feature_flags(None)
            for name in AI_CAPABILITY_FLAGS:
                assert getattr(flags, name) is False, (
                    f"{name} leaked on in mode {mode!r}"
                )

    def test_unknown_mode_does_not_enable_ai(self, monkeypatch):
        """An unrecognized TOPOS_PRODUCT_MODE must keep all AI flags off."""
        from src.shared.feature_flags import AI_CAPABILITY_FLAGS
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "totally_unknown")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        flags = load_feature_flags(None)
        for name in AI_CAPABILITY_FLAGS:
            assert getattr(flags, name) is False

    def test_env_var_can_enable_ai_flag(self, monkeypatch):
        """TOPOS_FEATURE_<NAME>=true must be able to opt into a single AI flag."""
        from src.shared.feature_flags import AI_CAPABILITY_FLAGS
        # Clear every AI env var so only the one we set takes effect.
        for name in AI_CAPABILITY_FLAGS:
            monkeypatch.delenv(f"TOPOS_FEATURE_{name.upper()}", raising=False)
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        monkeypatch.setenv("TOPOS_FEATURE_AI_ASSISTANT", "true")

        flags = load_feature_flags(None)
        assert flags.ai_assistant is True
        # Other AI flags stay off — opt-in is per-flag, not all-or-nothing.
        assert flags.media_analysis is False
        assert flags.generation is False

    def test_bulk_disable_keeps_ai_off(self, monkeypatch):
        """TOPOS_DISABLE_FEATURES must not be needed to keep AI off, and listing
        an AI flag in it must keep it off."""
        from src.shared.feature_flags import AI_CAPABILITY_FLAGS
        for name in AI_CAPABILITY_FLAGS:
            monkeypatch.delenv(f"TOPOS_FEATURE_{name.upper()}", raising=False)
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "media_analysis,generation")
        flags = load_feature_flags(None)
        assert flags.media_analysis is False
        assert flags.generation is False
        assert flags.ai_assistant is False

    def test_config_can_enable_ai_flag(self):
        """An explicit config entry must be able to turn an AI flag on."""
        config = {"features": {"generation": True}}
        flags = load_feature_flags(config)
        assert flags.generation is True
        # Other AI flags remain off.
        assert flags.ai_assistant is False
        assert flags.cloud_analysis is False

    def test_ai_assistant_config_block_does_not_enable_flags(self):
        """The config.json ``ai_assistant`` product-config block (release scope,
        capability mirror, policies) must NOT enable AI flags — only the
        ``features`` block drives load_feature_flags. This guards against a
        future regression where someone flips the mirror in the wrong place."""
        config = {
            "ai_assistant": {
                "capability_flags": {
                    "ai_assistant": True,
                    "media_analysis": True,
                    "generation": True,
                },
            },
        }
        flags = load_feature_flags(config)
        from src.shared.feature_flags import AI_CAPABILITY_FLAGS
        for name in AI_CAPABILITY_FLAGS:
            assert getattr(flags, name) is False, (
                f"{name} must stay off: ai_assistant config block is not a flag source"
            )

    def test_explicit_env_overrides_bulk_disable_for_ai(self, monkeypatch):
        """Precedence chain applies to AI flags too:
        TOPOS_FEATURE_<NAME> > TOPOS_DISABLE_FEATURES > config > default."""
        from src.shared.feature_flags import AI_CAPABILITY_FLAGS
        for name in AI_CAPABILITY_FLAGS:
            monkeypatch.delenv(f"TOPOS_FEATURE_{name.upper()}", raising=False)
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "ai_assistant,generation")
        monkeypatch.setenv("TOPOS_FEATURE_AI_ASSISTANT", "true")
        flags = load_feature_flags(None)
        assert flags.ai_assistant is True      # explicit env wins
        assert flags.generation is False       # bulk disable holds

    def test_full_precedence_chain_for_ai_flags(self, monkeypatch):
        """Full precedence: explicit env > bulk disable > config > default, for AI."""
        from src.shared.feature_flags import AI_CAPABILITY_FLAGS
        for name in AI_CAPABILITY_FLAGS:
            monkeypatch.delenv(f"TOPOS_FEATURE_{name.upper()}", raising=False)
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "ai_assistant,generation")
        monkeypatch.setenv("TOPOS_FEATURE_AI_ASSISTANT", "true")  # re-enable
        config = {"features": {"ai_assistant": False, "generation": True}}
        flags = load_feature_flags(config)
        assert flags.ai_assistant is True    # explicit env wins
        assert flags.generation is False     # bulk disable wins (no explicit env)
