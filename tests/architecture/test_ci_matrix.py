"""
Parametrized CI matrix tests.

Runs the same import and availability checks across multiple feature
configurations to ensure the application handles each product mode
correctly without requiring GUI components.
"""
import importlib
import sys
from typing import Dict, FrozenSet

import pytest

from src.shared.feature_flags import FeatureFlags, load_feature_flags
from src.app.feature_registry import FeatureRegistry
from src.app.app_context import AppContext


# ---------------------------------------------------------------------------
# Configuration definitions
# ---------------------------------------------------------------------------

class _Config:
    """Describes a product-mode configuration for parametrized tests."""

    def __init__(
        self,
        name: str,
        env_vars: Dict[str, str],
        expected_enabled: FrozenSet[str],
        expected_disabled: FrozenSet[str],
    ):
        self.name = name
        self.env_vars = env_vars
        self.expected_enabled = expected_enabled
        self.expected_disabled = expected_disabled


CONFIGURATIONS = [
    _Config(
        name="color_only",
        env_vars={
            "TOPOS_DISABLE_FEATURES": "edit,composite,deliver",
        },
        expected_enabled=frozenset({"color", "library"}),
        expected_disabled=frozenset({"edit", "composite", "deliver"}),
    ),
    _Config(
        name="studio_full",
        env_vars={
            "TOPOS_PRODUCT_MODE": "studio",
        },
        expected_enabled=frozenset({"color", "library", "edit", "composite", "deliver"}),
        expected_disabled=frozenset(),
    ),
    _Config(
        name="deliver_only",
        env_vars={
            "TOPOS_DISABLE_FEATURES": "edit,composite",
        },
        expected_enabled=frozenset({"color", "library", "deliver"}),
        expected_disabled=frozenset({"edit", "composite"}),
    ),
]


def _apply_config(config: _Config, monkeypatch):
    """Apply environment variables for a configuration and clean up
    per-feature overrides that might interfere."""
    # Clear all relevant env vars first.
    for var in (
        "TOPOS_DISABLE_FEATURES",
        "TOPOS_PRODUCT_MODE",
    ):
        monkeypatch.delenv(var, raising=False)
    for feat in ("LIBRARY", "COLOR", "EDIT", "COMPOSITE", "DELIVER"):
        monkeypatch.delenv(f"TOPOS_FEATURE_{feat}", raising=False)

    # Apply configuration env vars.
    for key, value in config.env_vars.items():
        monkeypatch.setenv(key, value)

    # Reload feature flags module to pick up new env.
    if "src.shared.feature_flags" in sys.modules:
        importlib.reload(sys.modules["src.shared.feature_flags"])


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------

@pytest.fixture(params=CONFIGURATIONS, ids=[c.name for c in CONFIGURATIONS])
def config(request):
    """Yield each configuration for parametrized testing."""
    return request.param


class TestCIMatrix:
    """Parametrized tests across product configurations."""

    def test_main_window_module_resolvable(self, config, monkeypatch):
        """src.app.module_loader must be importable for every configuration."""
        _apply_config(config, monkeypatch)
        import src.app.module_loader as ml  # noqa: F401

    def test_feature_flags_correct(self, config, monkeypatch):
        """FeatureFlags must reflect the expected enabled/disabled sets."""
        _apply_config(config, monkeypatch)
        flags = load_feature_flags(None)

        for feat in config.expected_enabled:
            assert flags.is_enabled(feat) is True, (
                f"[{config.name}] Expected '{feat}' to be enabled"
            )

        for feat in config.expected_disabled:
            assert flags.is_enabled(feat) is False, (
                f"[{config.name}] Expected '{feat}' to be disabled"
            )

    def test_color_always_enabled(self, config, monkeypatch):
        """Color must be enabled regardless of configuration."""
        _apply_config(config, monkeypatch)
        flags = load_feature_flags(None)
        assert flags.is_enabled("color") is True

    def test_module_loader_skips_disabled(self, config, monkeypatch):
        """module_loader.register_builtin_modules should skip modules whose
        flags are disabled."""
        _apply_config(config, monkeypatch)
        from src.app import module_loader
        from unittest.mock import MagicMock

        flags = load_feature_flags(None)
        registry = MagicMock()
        registry._context = MagicMock()
        registry._context.feature_flags = flags

        module_loader.register_builtin_modules(registry)

        if registry.register_module.called:
            registered_ids = {
                call.args[0].feature_id
                for call in registry.register_module.call_args_list
            }
            for feat in config.expected_disabled:
                assert feat not in registered_ids, (
                    f"[{config.name}] Disabled feature '{feat}' was registered"
                )
        # If nothing was called (all disabled), that is also fine.

    def test_feature_registry_color_available(self, config, monkeypatch):
        """FeatureRegistry must always be able to host the color module."""
        _apply_config(config, monkeypatch)
        from src.features.color.module import ColorModule

        registry = FeatureRegistry()
        registry.initialize(AppContext())
        assert registry.register_module(ColorModule()) is True
        assert registry.is_feature_available("color") is True
