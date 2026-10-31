"""
Module isolation tests.

Verify that the application behaves correctly when optional feature modules
(edit, composite, deliver) are disabled via feature flags or physically
removed from disk.

These tests exercise import boundaries and the module_loader / feature_registry
subsystem without requiring a QApplication or OpenGL context.
"""
import importlib
import sys

import pytest

from src.shared.feature_flags import FeatureFlags, load_feature_flags
from src.app.feature_registry import FeatureRegistry
from src.app.app_context import AppContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_module(full_name: str):
    """Force-reload a module so it picks up new environment state."""
    if full_name in sys.modules:
        importlib.reload(sys.modules[full_name])


# ===================================================================
# TestColorOnlyFlags — feature flags only, directories untouched
# ===================================================================

class TestColorOnlyFlags:
    """Verify behaviour when features are disabled via env flags only."""

    def test_main_window_importable(self, color_only_env):
        """Importing main_window should not crash when features are disabled."""
        _reload_module("src.shared.feature_flags")
        _reload_module("src.app.module_loader")
        # main_window has heavy Qt imports; we only test that the *module*
        # can be found (i.e. the import machinery resolves it).  Actually
        # importing it pulls in PyQt6 which may not be headless-safe in CI,
        # so we check the import chain that *doesn't* require Qt.
        import src.app.module_loader as ml  # noqa: F401

    def test_color_module_registered(self, color_only_env):
        """Color feature module should be available even with all optional
        features disabled."""
        _reload_module("src.shared.feature_flags")
        from src.features.color.module import ColorModule

        module = ColorModule()
        assert module.is_available() is True
        assert module.feature_id == "color"

    def test_edit_feature_not_available(self, color_only_env):
        """Edit feature should report unavailable when disabled via flags."""
        _reload_module("src.shared.feature_flags")
        flags = load_feature_flags(None)
        assert flags.is_enabled("edit") is False

        # Verify that module_loader skips edit registration.
        from src.app import module_loader
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._context = MagicMock()
        registry._context.feature_flags = flags

        module_loader.register_builtin_modules(registry)
        # register_module should never be called because the flag check
        # prevents the import attempt.
        registry.register_module.assert_not_called()


# ===================================================================
# TestColorOnlyPhysicalMissing — directories physically hidden
# ===================================================================

class TestColorOnlyPhysicalMissing:
    """Verify behaviour when optional module directories are removed."""

    def test_main_window_importable(self, color_only_physical):
        """module_loader should import without error even when dirs are gone."""
        _reload_module("src.shared.feature_flags")
        _reload_module("src.app.module_loader")
        import src.app.module_loader as ml  # noqa: F401

    def test_app_controllers_importable(self, color_only_physical):
        """Controllers package should import cleanly even with missing modules.

        Controller mixins use try/except (or shared-only imports) for their
        deep feature imports, so the package and non-edit controllers must
        import cleanly with the feature directories gone.
        """
        # Purge cached controller modules so they re-import from disk.
        for key in list(sys.modules):
            if key.startswith("src.app.controllers"):
                sys.modules.pop(key, None)

        import src.app.controllers  # noqa: F401

        import src.app.controllers.deliver_controller  # noqa: F401
        import src.app.controllers.composite_controller  # noqa: F401

    @pytest.mark.xfail(
        reason="edit_controller.py 顶层存在未加防护的 src.features.edit 导入，"
               "物理 color-only 构建不可导入——app↔feature 边界存量债务，"
               "见 test_boundary_guardrails.KNOWN_BOUNDARY_BASELINES",
        strict=False,
    )
    def test_edit_controller_importable_physical(self, color_only_physical):
        """edit_controller should import even when src.features.edit is gone."""
        for key in list(sys.modules):
            if key.startswith("src.app.controllers"):
                sys.modules.pop(key, None)
        import src.app.controllers.edit_controller  # noqa: F401

    def test_loader_attempts_no_imports_when_flags_disable_all(
        self, color_only_physical
    ):
        """Flags off + dirs hidden: 注册器必须先查 flag、零导入尝试、零注册。"""
        _reload_module("src.shared.feature_flags")
        _reload_module("src.app.module_loader")

        from unittest.mock import MagicMock

        from src.app import module_loader

        flags = load_feature_flags(None)
        registry = MagicMock()
        registry._context = MagicMock()
        registry._context.feature_flags = flags

        module_loader.register_builtin_modules(registry)
        registry.register_module.assert_not_called()

    def test_edit_import_fails_gracefully(self, color_only_physical):
        """src.features.edit should not be importable when the directory is hidden."""
        # Make sure no cached import exists.
        for key in list(sys.modules):
            if key.startswith("src.features.edit"):
                sys.modules.pop(key, None)

        with pytest.raises(ModuleNotFoundError):
            import src.features.edit  # noqa: F401

    def test_color_core_still_works(self, color_only_physical):
        """The color module (core) must remain importable and functional."""
        # Reload to pick up any state changes.
        _reload_module("src.shared.feature_flags")

        from src.features.color.module import ColorModule
        from src.features.color.core.image_engine import ImageEngine  # noqa: F401

        module = ColorModule()
        assert module.is_available() is True

        flags = load_feature_flags(None)
        assert flags.is_enabled("color") is True
        assert flags.is_enabled("edit") is False
        assert flags.is_enabled("composite") is False
        assert flags.is_enabled("deliver") is False


# ===================================================================
# TestDeliverWithoutEdit — only edit hidden, deliver available
# ===================================================================

class TestDeliverWithoutEdit:
    """Verify that deliver works independently of edit."""

    def test_deliver_works_without_edit(self, hide_modules, monkeypatch):
        """Hide only the edit module; deliver should still be importable."""
        # Hide edit only.
        hide_modules(["edit"])

        # Enable deliver but disable edit.
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "edit")
        monkeypatch.delenv("TOPOS_FEATURE_EDIT", raising=False)

        _reload_module("src.shared.feature_flags")

        # deliver module should still be importable.
        _reload_module("src.features.deliver.module")
        from src.features.deliver.module import DeliverModule

        module = DeliverModule()
        assert module.feature_id == "deliver"

        # Verify flags.
        flags = load_feature_flags(None)
        assert flags.is_enabled("deliver") is True
        assert flags.is_enabled("edit") is False
        assert flags.is_enabled("color") is True

    def test_export_functionality_available(self, hide_modules, monkeypatch):
        """Export-related code paths should work without the edit module."""
        hide_modules(["edit"])
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "edit")
        monkeypatch.delenv("TOPOS_FEATURE_EDIT", raising=False)
        # 场景是"仅 edit 缺席"：把后来加入默认开启集的 teaching_animation /
        # ai_workflow 显式关掉，注册数断言（composite+deliver == 2）才成立。
        monkeypatch.setenv("TOPOS_FEATURE_TEACHING_ANIMATION", "0")
        monkeypatch.setenv("TOPOS_FEATURE_AI_WORKFLOW", "0")

        _reload_module("src.shared.feature_flags")

        # The shared export panel and file IO should be importable.
        from src.shared.io.file_io import FileIO  # noqa: F401
        from src.shared.export.export_panel import generate_export_filename  # noqa: F401

        # Deliver controller mixin should be importable.
        for key in list(sys.modules):
            if key.startswith("src.app.controllers.deliver"):
                sys.modules.pop(key, None)

        from src.app.controllers.deliver_controller import DeliverControllerMixin  # noqa: F401

        # Module loader should register deliver but skip edit.
        from src.app import module_loader
        from unittest.mock import MagicMock

        flags = load_feature_flags(None)
        registry = MagicMock()
        registry._context = MagicMock()
        registry._context.feature_flags = flags

        module_loader.register_builtin_modules(registry)

        # register_module should have been called for composite and deliver
        # (edit is disabled).
        assert registry.register_module.call_count == 2
        registered_ids = {
            call.args[0].feature_id
            for call in registry.register_module.call_args_list
        }
        assert "deliver" in registered_ids
        assert "edit" not in registered_ids
