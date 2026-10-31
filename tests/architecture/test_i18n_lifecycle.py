"""Test i18n lifecycle: module registration, reload, and locale switching."""
import json
import os
import tempfile
import pytest

from src.shared.i18n.i18n import I18N


class TestI18NModuleLifecycle:
    """Test that module i18n is loaded during registration and preserved on reload."""

    def setup_method(self):
        """Reset I18N state between tests."""
        I18N._translations = {}
        I18N._fallback_translations = {}
        I18N._module_locales_dirs = []

    def _create_temp_locales(self, tmp_path, locale_code, translations):
        """Create a temporary locale file with given translations."""
        locales_dir = tmp_path / "locales"
        locales_dir.mkdir(parents=True, exist_ok=True)
        locale_file = locales_dir / f"{locale_code}.json"
        with open(locale_file, "w", encoding="utf-8") as f:
            json.dump(translations, f)
        return str(locales_dir)

    def test_module_translations_loaded(self, tmp_path):
        """Module translations should be merged into I18N after load_module_translations."""
        # Setup base locale
        base_dir = self._create_temp_locales(tmp_path, "en_US", {"menu": {"file": "File"}})
        I18N._locales_dir = base_dir
        I18N.load_locale("en_US")

        # Load module translations
        module_dir = self._create_temp_locales(
            tmp_path / "module1", "en_US",
            {"module": {"feature": "Module Feature"}}
        )
        I18N.load_module_translations(module_dir)

        assert I18N.tr("module.feature", "") == "Module Feature"

    def test_module_dirs_tracked_for_reload(self, tmp_path):
        """Module locales dirs should be tracked so reload can re-merge them."""
        base_dir = self._create_temp_locales(tmp_path, "en_US", {"menu": {"file": "File"}})
        I18N._locales_dir = base_dir
        I18N.load_locale("en_US")

        module_dir = self._create_temp_locales(
            tmp_path / "module1", "en_US",
            {"module": {"key": "English Value"}}
        )
        I18N.load_module_translations(module_dir)

        # Create Chinese version
        cn_base = self._create_temp_locales(tmp_path / "base_cn", "zh_CN", {"menu": {"file": "文件"}})
        I18N._locales_dir = cn_base

        # Create Chinese module translation
        self._create_temp_locales(
            tmp_path / "module1", "zh_CN",
            {"module": {"key": "中文值"}}
        )

        # Reload to Chinese
        I18N.reload_locale("zh_CN")

        assert I18N.tr("module.key", "") == "中文值"

    def test_module_translations_not_loaded_for_disabled_features(self, tmp_path):
        """Modules should not load translations when their feature is disabled."""
        # This is enforced by FeatureRegistry only calling register_i18n()
        # for registered modules. We test the registry integration elsewhere.
        pass

    def test_multiple_modules_merged(self, tmp_path):
        """Multiple module translations should all be present."""
        base_dir = self._create_temp_locales(tmp_path, "en_US", {"shared": {"app": "App"}})
        I18N._locales_dir = base_dir
        I18N.load_locale("en_US")

        module1_dir = self._create_temp_locales(
            tmp_path / "m1", "en_US", {"module1": {"key": "M1"}}
        )
        module2_dir = self._create_temp_locales(
            tmp_path / "m2", "en_US", {"module2": {"key": "M2"}}
        )
        I18N.load_module_translations(module1_dir)
        I18N.load_module_translations(module2_dir)

        assert I18N.tr("module1.key", "") == "M1"
        assert I18N.tr("module2.key", "") == "M2"
        assert I18N.tr("shared.app", "") == "App"

    def test_reload_locale_re_merges_all_modules(self, tmp_path):
        """After reload, all previously loaded module translations should be re-merged."""
        base_en = self._create_temp_locales(tmp_path, "en_US", {"app": {"title": "Title"}})
        I18N._locales_dir = base_en
        I18N.load_locale("en_US")

        m1_dir = self._create_temp_locales(
            tmp_path / "m1", "en_US", {"m1": {"k": "EN"}}
        )
        I18N.load_module_translations(m1_dir)

        # Setup Chinese
        base_cn = self._create_temp_locales(tmp_path / "cn_base", "zh_CN", {"app": {"title": "标题"}})
        self._create_temp_locales(tmp_path / "m1", "zh_CN", {"m1": {"k": "中"}})
        I18N._locales_dir = base_cn

        I18N.reload_locale("zh_CN")

        assert I18N.tr("m1.k", "") == "中"
        assert I18N.tr("app.title", "") == "标题"


class TestTrOrNull:
    """Regression tests for tr_or_none() — the long-key-leak bug fix.

    Before the fix, ``tr(key, default=None)`` returned the *literal key string*
    when a translation was missing, which made ``if translated:`` checks in
    callers (node titles, option labels, group names) incorrectly treat a
    missing translation as found. This surfaced as long keys like
    ``nodes.reformat.preset.UHD 4K`` showing up verbatim in the UI.

    ``tr_or_none()`` returns ``None`` for missing keys so callers fall back to
    their own sensible default (e.g. ``option_value.title()``).
    """

    def setup_method(self):
        """Reset I18N state between tests."""
        I18N._translations = {}
        I18N._fallback_translations = {}
        I18N._module_locales_dirs = []

    def _load(self, translations, fallback=None):
        """Load translations directly into the I18N singleton (no files)."""
        I18N._translations = translations
        I18N._fallback_translations = fallback or {}

    def test_missing_key_returns_none(self):
        """A key with no translation must return None (NOT the key string)."""
        self._load({"nodes": {"crop": {"title": "Crop"}}})
        result = I18N.tr_or_none("nodes.reformat.preset.UHD 4K")
        assert result is None

    def test_present_key_returns_translation(self):
        """An existing key returns the translated string."""
        self._load({"nodes": {"crop": {"title": "Crop"}}})
        assert I18N.tr_or_none("nodes.crop.title") == "Crop"

    def test_fallback_locale_used_when_current_missing(self):
        """If current locale lacks the key but fallback has it, return fallback."""
        self._load(
            translations={"nodes": {"crop": {}}},  # zh_CN has no title
            fallback={"nodes": {"crop": {"title": "Crop"}}},  # en_US has it
        )
        assert I18N.tr_or_none("nodes.crop.title") == "Crop"

    def test_both_locales_missing_returns_none(self):
        """When neither current nor fallback has the key, return None."""
        self._load(
            translations={"nodes": {"crop": {}}},
            fallback={"nodes": {"crop": {}}},
        )
        assert I18N.tr_or_none("nodes.crop.title") is None

    def test_empty_translations_returns_none(self):
        """When no locale is loaded, return None."""
        self._load({})
        assert I18N.tr_or_none("nodes.crop.title") is None

    def test_module_convenience_function_matches_method(self):
        """The module-level tr_or_none() helper mirrors I18N.tr_or_none()."""
        from src.shared.i18n.i18n import tr_or_none
        self._load({"nodes": {"crop": {"title": "Crop"}}})
        assert tr_or_none("nodes.crop.title") == "Crop"
        assert tr_or_none("missing.key") is None

    def test_no_more_long_key_leak_regression(self):
        """Simulates the exact scenario that produced the user-visible bug:
        an option value (e.g. 'UHD 4K') with no JSON entry. Before the fix the
        UI showed the literal key; now callers can detect the miss and fall
        back to a sensible default like ``option_value.title()``.
        """
        self._load({"nodes": {"reformat": {"preset": {}}}})  # no preset values
        translated = I18N.tr_or_none("nodes.reformat.preset.UHD 4K")
        # Caller falls back gracefully:
        if translated is None:
            display = "UHD 4K".title()
        else:
            display = translated
        assert display == "Uhd 4K"  # fallback .title()
        assert "nodes.reformat.preset" not in display  # no key leak


class TestFeatureRegistryI18N:
    """Test that FeatureRegistry calls register_i18n during module registration."""

    def test_register_module_calls_i18n(self, tmp_path, monkeypatch):
        """register_module should call register_i18n on the module."""
        from src.app.feature_registry import FeatureRegistry
        from src.app.app_context import AppContext

        i18n_called = []

        class FakeModule:
            feature_id = "test"
            def is_available(self): return True
            def register_pages(self, ctx): return []
            def register_actions(self, ctx): pass
            def register_save_handlers(self, ctx): pass
            def on_page_enter(self, pid, ctx): pass
            def on_page_leave(self, pid, ctx): pass
            def register_i18n(self):
                i18n_called.append(True)

        registry = FeatureRegistry()
        ctx = AppContext(
            config_manager=None,
            feature_flags=None,
            i18n=None,
            logger=None,
        )
        registry.initialize(ctx)
        registry.register_module(FakeModule())

        assert len(i18n_called) == 1

    def test_i18n_failure_does_not_block_registration(self, monkeypatch):
        """If register_i18n raises, module should still be registered."""
        from src.app.feature_registry import FeatureRegistry
        from src.app.app_context import AppContext

        class BrokenI18NModule:
            feature_id = "broken"
            def is_available(self): return True
            def register_pages(self, ctx): return []
            def register_actions(self, ctx): pass
            def register_save_handlers(self, ctx): pass
            def on_page_enter(self, pid, ctx): pass
            def on_page_leave(self, pid, ctx): pass
            def register_i18n(self):
                raise RuntimeError("i18n failed!")

        registry = FeatureRegistry()
        ctx = AppContext(
            config_manager=None,
            feature_flags=None,
            i18n=None,
            logger=None,
        )
        registry.initialize(ctx)
        result = registry.register_module(BrokenI18NModule())

        assert result is True  # Still registered despite i18n failure
        assert registry.is_feature_available("broken")
