"""Tests for product presets, entry points, and packaging configuration."""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.products import (
    ProductPreset,
    PRODUCT_PRESETS,
    ALWAYS_ENABLED,
    ALL_FEATURES,
    get_current_product,
    get_enabled_features,
    FEATURE_LIBRARY,
    FEATURE_COLOR,
    FEATURE_EDIT,
    FEATURE_COMPOSITE,
    FEATURE_DELIVER,
)
from src.products.packaging import get_packaging_config


# ---------------------------------------------------------------------------
# ProductPreset data structure
# ---------------------------------------------------------------------------

class TestProductPreset:
    """Test ProductPreset dataclass and resolved properties."""

    def test_resolved_enabled_always_includes_color_and_library(self):
        preset = ProductPreset(
            name="test",
            display_name="Test",
            enabled_features=frozenset(),
            disabled_features=frozenset(),
        )
        assert FEATURE_COLOR in preset.resolved_enabled
        assert FEATURE_LIBRARY in preset.resolved_enabled

    def test_resolved_enabled_subtracts_disabled(self):
        preset = ProductPreset(
            name="test",
            display_name="Test",
            enabled_features=frozenset(ALL_FEATURES),
            disabled_features=frozenset({FEATURE_EDIT}),
        )
        assert FEATURE_EDIT not in preset.resolved_enabled
        assert FEATURE_COLOR in preset.resolved_enabled

    def test_resolved_disabled_excludes_always_enabled(self):
        """Even if someone lists color in disabled_features, resolved_disabled
        should not contain it."""
        preset = ProductPreset(
            name="test",
            display_name="Test",
            disabled_features=frozenset({FEATURE_COLOR, FEATURE_EDIT}),
        )
        assert FEATURE_COLOR not in preset.resolved_disabled
        assert FEATURE_EDIT in preset.resolved_disabled


# ---------------------------------------------------------------------------
# PRODUCT_PRESETS dictionary
# ---------------------------------------------------------------------------

class TestProductPresetsDict:
    """Test the built-in product preset definitions."""

    def test_four_products_defined(self):
        assert set(PRODUCT_PRESETS.keys()) == {
            "topos-studio",
            "topos-color",
            "topos-edit",
            "topos-composite",
        }

    def test_all_presets_have_color_enabled(self):
        for name, preset in PRODUCT_PRESETS.items():
            assert FEATURE_COLOR in preset.resolved_enabled, (
                f"{name} does not have color enabled"
            )

    def test_all_presets_have_library_enabled(self):
        for name, preset in PRODUCT_PRESETS.items():
            assert FEATURE_LIBRARY in preset.resolved_enabled, (
                f"{name} does not have library enabled"
            )

    # -- topos-studio --
    def test_studio_all_features_enabled(self):
        preset = PRODUCT_PRESETS["topos-studio"]
        assert preset.resolved_enabled == frozenset(ALL_FEATURES)
        assert preset.resolved_disabled == frozenset()

    # -- topos-color --
    def test_color_preset_enabled(self):
        preset = PRODUCT_PRESETS["topos-color"]
        enabled = preset.resolved_enabled
        assert FEATURE_COLOR in enabled
        assert FEATURE_LIBRARY in enabled
        assert FEATURE_DELIVER in enabled

    def test_color_preset_disabled(self):
        preset = PRODUCT_PRESETS["topos-color"]
        disabled = preset.resolved_disabled
        assert FEATURE_EDIT in disabled
        assert FEATURE_COMPOSITE in disabled

    # -- topos-edit --
    def test_edit_preset_enabled(self):
        preset = PRODUCT_PRESETS["topos-edit"]
        enabled = preset.resolved_enabled
        assert FEATURE_EDIT in enabled
        assert FEATURE_COLOR in enabled
        assert FEATURE_DELIVER in enabled
        assert FEATURE_LIBRARY in enabled

    def test_edit_preset_disabled(self):
        preset = PRODUCT_PRESETS["topos-edit"]
        assert FEATURE_COMPOSITE in preset.resolved_disabled

    # -- topos-composite --
    def test_composite_preset_enabled(self):
        preset = PRODUCT_PRESETS["topos-composite"]
        enabled = preset.resolved_enabled
        assert FEATURE_COMPOSITE in enabled
        assert FEATURE_COLOR in enabled
        assert FEATURE_DELIVER in enabled
        assert FEATURE_LIBRARY in enabled

    def test_composite_preset_disabled(self):
        preset = PRODUCT_PRESETS["topos-composite"]
        assert FEATURE_EDIT in preset.resolved_disabled


# ---------------------------------------------------------------------------
# get_current_product()
# ---------------------------------------------------------------------------

class TestGetCurrentProduct:
    """Test get_current_product() environment variable handling."""

    def test_defaults_to_studio(self, monkeypatch):
        monkeypatch.delenv("TOPOS_PRODUCT_MODE", raising=False)
        product = get_current_product()
        assert product.name == "topos-studio"

    def test_full_name_resolved(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "topos-color")
        product = get_current_product()
        assert product.name == "topos-color"

    def test_legacy_short_name_studio(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
        product = get_current_product()
        assert product.name == "topos-studio"

    def test_legacy_short_name_color(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        product = get_current_product()
        assert product.name == "topos-color"

    def test_legacy_short_name_edit(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "edit")
        product = get_current_product()
        assert product.name == "topos-edit"

    def test_legacy_short_name_composite(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "composite")
        product = get_current_product()
        assert product.name == "topos-composite"

    def test_unknown_mode_falls_back_to_studio(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "nonexistent")
        product = get_current_product()
        assert product.name == "topos-studio"

    def test_empty_string_falls_back_to_studio(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "  ")
        product = get_current_product()
        assert product.name == "topos-studio"


# ---------------------------------------------------------------------------
# get_enabled_features()
# ---------------------------------------------------------------------------

class TestGetEnabledFeatures:
    """Test get_enabled_features() merging with TOPOS_DISABLE_FEATURES."""

    def test_studio_all_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("TOPOS_PRODUCT_MODE", raising=False)
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        features = get_enabled_features()
        assert features == frozenset(ALL_FEATURES)

    def test_color_mode_enabled(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
        features = get_enabled_features()
        assert FEATURE_COLOR in features
        assert FEATURE_DELIVER in features
        assert FEATURE_EDIT not in features
        assert FEATURE_COMPOSITE not in features

    def test_disable_overrides_product_mode(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "topos-studio")
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "edit,composite")
        features = get_enabled_features()
        assert FEATURE_EDIT not in features
        assert FEATURE_COMPOSITE not in features
        assert FEATURE_COLOR in features

    def test_color_cannot_be_disabled_library_can(self, monkeypatch):
        """[AUD-21601] get_enabled_features() now delegates to
        load_feature_flags(): color stays always-on, while library follows the
        runtime semantics and CAN be disabled via TOPOS_DISABLE_FEATURES (the
        products-side ALWAYS_ENABLED pin for library was the drifted
        dual-track behavior this convergence removed)."""
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "topos-studio")
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "color,library")
        features = get_enabled_features()
        assert FEATURE_COLOR in features
        assert FEATURE_LIBRARY not in features

    def test_disable_deliver_in_edit_product(self, monkeypatch):
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "edit")
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "deliver")
        features = get_enabled_features()
        assert FEATURE_DELIVER not in features
        assert FEATURE_EDIT in features


# ---------------------------------------------------------------------------
# Product entry point register() functions
# ---------------------------------------------------------------------------

class TestProductEntryPoints:
    """Test that each product entry point's register() function works with a
    mocked FeatureRegistry (no GUI dependencies)."""

    def _make_mock_registry(self):
        """Create a mock FeatureRegistry that simulates successful registration."""
        registry = MagicMock()
        registry._context = MagicMock()
        # register_module returns True to simulate success
        registry.register_module.return_value = True
        return registry

    def test_studio_register_does_not_crash(self):
        from src.products.topos_studio import register
        registry = self._make_mock_registry()
        register(registry)
        # Should have attempted to register modules
        assert registry.register_module.call_count > 0

    def test_color_register_does_not_crash(self):
        from src.products.topos_color import register
        registry = self._make_mock_registry()
        register(registry)
        assert registry.register_module.call_count > 0

    def test_edit_register_does_not_crash(self):
        from src.products.topos_edit import register
        registry = self._make_mock_registry()
        register(registry)
        assert registry.register_module.call_count > 0

    def test_composite_register_does_not_crash(self):
        from src.products.topos_composite import register
        registry = self._make_mock_registry()
        register(registry)
        assert registry.register_module.call_count > 0

    def test_studio_get_preset(self):
        from src.products.topos_studio import get_preset
        assert get_preset().name == "topos-studio"

    def test_color_get_preset(self):
        from src.products.topos_color import get_preset
        assert get_preset().name == "topos-color"

    def test_edit_get_preset(self):
        from src.products.topos_edit import get_preset
        assert get_preset().name == "topos-edit"

    def test_composite_get_preset(self):
        from src.products.topos_composite import get_preset
        assert get_preset().name == "topos-composite"


# ---------------------------------------------------------------------------
# Packaging configuration
# ---------------------------------------------------------------------------

class TestPackagingConfig:
    """Test get_packaging_config() for each product."""

    def test_unknown_product_raises(self):
        with pytest.raises(ValueError, match="Unknown product"):
            get_packaging_config("nonexistent")

    def test_studio_packaging(self):
        config = get_packaging_config("topos-studio")
        assert "color" in config["include_dirs"]
        assert "edit" in config["include_dirs"]
        assert "composite" in config["include_dirs"]
        assert "deliver" in config["include_dirs"]
        assert config["exclude_dirs"] == []
        assert config["include_features"] == sorted(ALL_FEATURES)
        assert config["exclude_features"] == []
        assert config["include_plugins"] == ["*"]

    def test_color_packaging(self):
        config = get_packaging_config("topos-color")
        assert "color" in config["include_dirs"]
        assert "deliver" in config["include_dirs"]
        assert "edit" in config["exclude_dirs"]
        assert "composite" in config["exclude_dirs"]
        assert FEATURE_EDIT in config["exclude_features"]
        assert FEATURE_COMPOSITE in config["exclude_features"]
        assert config["include_plugins"] == []

    def test_edit_packaging(self):
        config = get_packaging_config("topos-edit")
        assert "edit" in config["include_dirs"]
        assert "composite" in config["exclude_dirs"]
        assert FEATURE_COMPOSITE in config["exclude_features"]
        assert config["include_plugins"] == []

    def test_composite_packaging(self):
        config = get_packaging_config("topos-composite")
        assert "composite" in config["include_dirs"]
        assert "edit" in config["exclude_dirs"]
        assert FEATURE_EDIT in config["exclude_features"]
        assert config["include_plugins"] == ["*"]

    def test_all_configs_have_required_keys(self):
        required_keys = {
            "include_features", "exclude_features",
            "include_dirs", "exclude_dirs",
            "include_locales", "include_plugins",
        }
        for name in PRODUCT_PRESETS:
            config = get_packaging_config(name)
            assert required_keys.issubset(config.keys()), (
                f"{name} missing keys: {required_keys - config.keys()}"
            )

    def test_include_and_exclude_dirs_are_disjoint(self):
        for name in PRODUCT_PRESETS:
            config = get_packaging_config(name)
            include = set(config["include_dirs"])
            exclude = set(config["exclude_dirs"])
            assert include.isdisjoint(exclude), (
                f"{name}: include_dirs and exclude_dirs overlap: {include & exclude}"
            )

    def test_include_and_exclude_features_are_disjoint(self):
        for name in PRODUCT_PRESETS:
            config = get_packaging_config(name)
            include = set(config["include_features"])
            exclude = set(config["exclude_features"])
            assert include.isdisjoint(exclude), (
                f"{name}: features overlap: {include & exclude}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
