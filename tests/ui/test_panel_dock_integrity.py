"""Tests for PanelRegistry dock integrity validation.

These tests verify that:
- Every registered panel_id is unique
- Every dock_attr is a non-empty string
- Every feature_id references a valid feature
- PanelDescriptor is frozen/immutable
- Filtering by product mode returns correct panels for each product
- Product mode isolation is correct (color has no Edit/Composite, etc.)
- validate_dock_integrity correctly reports issues for broken mappings
"""

import pytest
from unittest.mock import MagicMock

from src.shared.panels.registry import (
    PanelDescriptor,
    PanelRegistry,
    validate_dock_integrity,
)
from src.shared.feature_flags import (
    PRODUCT_MODE_PRESETS,
    DEFAULT_FEATURE_FLAGS,
    FeatureFlags,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make(
    panel_id: str,
    *,
    feature_id: str = "color",
    category: str = "Color",
    dock_attr: str = "",
    order: int = 100,
) -> PanelDescriptor:
    return PanelDescriptor(
        panel_id=panel_id,
        title_key=f"panel.{panel_id}",
        fallback_title=panel_id,
        feature_id=feature_id,
        category=category,
        dock_attr=dock_attr,
        order=order,
    )


def _mock_dock(has_toggle: bool = True):
    """Create a mock object that behaves like a QDockWidget."""
    dock = MagicMock()
    if has_toggle:
        dock.toggleViewAction.return_value = MagicMock()
    else:
        del dock.toggleViewAction
    return dock


def _mock_window(docks: dict[str, object]) -> MagicMock:
    """Create a mock window with named dock attributes.

    Args:
        docks: Mapping of attribute_name -> dock object (or None).
    """
    window = MagicMock()
    for name, dock_obj in docks.items():
        setattr(window, name, dock_obj)
    return window


def _register_all_available() -> PanelRegistry:
    """Register panels from all available feature modules."""
    registry = PanelRegistry()
    from src.features.color.module import ColorModule
    ColorModule().register_panels(registry)
    try:
        from src.features.edit.module import EditModule
        mod = EditModule()
        if mod.is_available():
            mod.register_panels(registry)
    except (ImportError, AttributeError):
        pass
    try:
        from src.features.composite.module import CompositeModule
        mod = CompositeModule()
        if mod.is_available():
            mod.register_panels(registry)
    except (ImportError, AttributeError):
        pass
    try:
        from src.features.deliver.module import DeliverModule
        mod = DeliverModule()
        if mod.is_available():
            mod.register_panels(registry)
    except (ImportError, AttributeError):
        pass
    return registry


# ---------------------------------------------------------------------------
# PanelDescriptor immutability
# ---------------------------------------------------------------------------

class TestPanelDescriptorFrozen:
    def test_cannot_set_panel_id(self):
        desc = _make("test", dock_attr="dock_test")
        with pytest.raises(AttributeError):
            desc.panel_id = "other"

    def test_cannot_set_dock_attr(self):
        desc = _make("test", dock_attr="dock_test")
        with pytest.raises(AttributeError):
            desc.dock_attr = "dock_other"

    def test_cannot_set_feature_id(self):
        desc = _make("test", feature_id="color")
        with pytest.raises(AttributeError):
            desc.feature_id = "edit"

    def test_cannot_set_category(self):
        desc = _make("test", category="Color")
        with pytest.raises(AttributeError):
            desc.category = "Edit"


# ---------------------------------------------------------------------------
# Unique panel_ids
# ---------------------------------------------------------------------------

class TestUniquePanelIds:
    def test_all_panel_ids_unique_across_modules(self):
        """No two modules register the same panel_id."""
        registry = _register_all_available()
        seen: set[str] = set()
        for desc in registry.get_all():
            assert desc.panel_id not in seen, (
                f"Duplicate panel_id: {desc.panel_id}"
            )
            seen.add(desc.panel_id)


# ---------------------------------------------------------------------------
# Non-empty dock_attrs
# ---------------------------------------------------------------------------

class TestDockAttrsNonEmpty:
    def test_all_dock_attrs_are_non_empty(self):
        """Every registered panel must have a non-empty dock_attr."""
        registry = _register_all_available()
        for desc in registry.get_all():
            assert desc.dock_attr, (
                f"Panel {desc.panel_id!r} has empty dock_attr"
            )

    def test_all_dock_attrs_are_strings(self):
        """Every dock_attr must be a string."""
        registry = _register_all_available()
        for desc in registry.get_all():
            assert isinstance(desc.dock_attr, str), (
                f"Panel {desc.panel_id!r} dock_attr is {type(desc.dock_attr)}, expected str"
            )


# ---------------------------------------------------------------------------
# Valid feature_ids
# ---------------------------------------------------------------------------

class TestValidFeatureIds:
    def test_all_feature_ids_reference_valid_features(self):
        """Every feature_id must be a key in DEFAULT_FEATURE_FLAGS."""
        valid = set(DEFAULT_FEATURE_FLAGS.keys())
        registry = _register_all_available()
        for desc in registry.get_all():
            assert desc.feature_id in valid, (
                f"Panel {desc.panel_id!r} references unknown feature_id "
                f"{desc.feature_id!r}"
            )


# ---------------------------------------------------------------------------
# Product mode filtering
# ---------------------------------------------------------------------------

class TestProductModeFiltering:
    def test_color_mode_no_edit_or_composite_panels(self):
        """Color product mode should not show Edit or Composite category panels."""
        registry = _register_all_available()
        enabled = set(
            f for f, v in PRODUCT_MODE_PRESETS["color"].items() if v
        )
        grouped = registry.get_panels_by_category(enabled)
        assert "Edit" not in grouped
        assert "Composite" not in grouped
        assert "Color" in grouped or "Workspace" in grouped

    def test_color_mode_no_edit_category_panels(self):
        """Strict check: no individual Edit-category panels appear."""
        registry = _register_all_available()
        enabled = set(
            f for f, v in PRODUCT_MODE_PRESETS["color"].items() if v
        )
        for desc in registry.get_all():
            if desc.feature_id in enabled:
                assert desc.category != "Edit", (
                    f"Panel {desc.panel_id!r} in Edit category appeared "
                    f"in color mode filtering"
                )

    def test_edit_mode_no_composite_panels(self):
        """Edit mode should not show Composite category panels."""
        registry = _register_all_available()
        enabled = set(
            f for f, v in PRODUCT_MODE_PRESETS["edit"].items() if v
        )
        grouped = registry.get_panels_by_category(enabled)
        assert "Composite" not in grouped
        # Edit panels should be present if edit module is available
        try:
            from src.features.edit.module import EditModule
            if EditModule().is_available():
                assert "Edit" in grouped
        except (ImportError, AttributeError):
            pass

    def test_composite_mode_no_edit_panels(self):
        """Composite mode should not show Edit category panels."""
        registry = _register_all_available()
        enabled = set(
            f for f, v in PRODUCT_MODE_PRESETS["composite"].items() if v
        )
        grouped = registry.get_panels_by_category(enabled)
        assert "Edit" not in grouped

    def test_studio_mode_has_all_categories(self):
        """Studio mode should include Color, Edit, and Composite panels."""
        registry = _register_all_available()
        enabled = set(
            f for f, v in PRODUCT_MODE_PRESETS["studio"].items() if v
        )
        grouped = registry.get_panels_by_category(enabled)
        assert "Color" in grouped or "Workspace" in grouped
        # Edit and Composite may not register if modules are unavailable
        try:
            from src.features.edit.module import EditModule
            if EditModule().is_available():
                assert "Edit" in grouped
        except (ImportError, AttributeError):
            pass
        try:
            from src.features.composite.module import CompositeModule
            if CompositeModule().is_available():
                assert "Composite" in grouped
        except (ImportError, AttributeError):
            pass

    def test_edit_mode_includes_deliver(self):
        """Edit mode should include deliver panels."""
        registry = _register_all_available()
        enabled = set(
            f for f, v in PRODUCT_MODE_PRESETS["edit"].items() if v
        )
        grouped = registry.get_panels_by_category(enabled)
        try:
            from src.features.deliver.module import DeliverModule
            if DeliverModule().is_available():
                assert "Deliver" in grouped
        except (ImportError, AttributeError):
            pass


# ---------------------------------------------------------------------------
# Work-mode filtering (Window > Panels menu data source)
# ---------------------------------------------------------------------------

class TestWorkModeFiltering:
    IMAGE_ONLY_TOOL_PANELS = {"color.crop", "color.deblur", "workspace.info"}

    @staticmethod
    def _panel_ids(registry: PanelRegistry, work_mode: str) -> set:
        grouped = registry.get_panels_by_category(work_mode=work_mode)
        return {d.panel_id for descs in grouped.values() for d in descs}

    def test_video_mode_hides_image_only_tool_panels(self):
        """剪辑（视频）模式下 Panels 菜单不出现图片专属的 Crop/Deblur/Info。"""
        ids = self._panel_ids(_register_all_available(), "video")
        assert self.IMAGE_ONLY_TOOL_PANELS.isdisjoint(ids), (
            f"image-only tool panels leaked into video mode: "
            f"{sorted(self.IMAGE_ONLY_TOOL_PANELS & ids)}")
        # 既有 image-only 面板同样被过滤（回归保护）。
        assert "workspace.library" not in ids
        assert "workspace.export" not in ids
        # video-only 面板仍在。
        assert "color.timeline_clips" in ids

    def test_image_mode_keeps_image_only_tool_panels(self):
        """图片模式保留图片专属的 Crop/Deblur/Info，隐藏 video 专属面板。"""
        ids = self._panel_ids(_register_all_available(), "image")
        assert self.IMAGE_ONLY_TOOL_PANELS <= ids
        assert "color.timeline_clips" not in ids
        assert "workspace.library" in ids


# ---------------------------------------------------------------------------
# validate_dock_integrity with mocks
# ---------------------------------------------------------------------------

class TestValidateDockIntegrity:
    def test_no_issues_when_all_docks_exist(self):
        """Happy path: all panels map to existing docks."""
        registry = PanelRegistry()
        registry.register(_make("a", dock_attr="dock_a"))
        registry.register(_make("b", dock_attr="dock_b"))

        window = _mock_window({
            "dock_a": _mock_dock(),
            "dock_b": _mock_dock(),
        })
        issues = validate_dock_integrity(registry, window)
        assert issues == []

    def test_reports_missing_dock_attr(self):
        """Panel with empty dock_attr is reported."""
        registry = PanelRegistry()
        registry.register(_make("a", dock_attr=""))
        issues = validate_dock_integrity(registry, _mock_window({}))
        assert any("empty dock_attr" in i for i in issues)

    def test_reports_missing_window_attribute(self):
        """Panel referencing a non-existent window attribute is reported."""
        registry = PanelRegistry()
        registry.register(_make("a", dock_attr="dock_missing"))
        # Use a plain object instead of MagicMock, since MagicMock returns
        # True for hasattr on any attribute name.
        window = type("Window", (), {})()
        issues = validate_dock_integrity(registry, window)
        assert any("not found on window" in i for i in issues)

    def test_reports_none_dock_is_not_error(self):
        """A None dock (optional feature disabled) is not reported as error."""
        registry = PanelRegistry()
        registry.register(_make("a", dock_attr="dock_optional"))

        window = _mock_window({"dock_optional": None})
        issues = validate_dock_integrity(registry, window)
        assert issues == []

    def test_reports_missing_toggle_view_action(self):
        """Dock without toggleViewAction is reported."""
        registry = PanelRegistry()
        registry.register(_make("a", dock_attr="dock_bad"))

        window = _mock_window({
            "dock_bad": _mock_dock(has_toggle=False),
        })
        issues = validate_dock_integrity(registry, window)
        assert any("toggleViewAction" in i for i in issues)

    def test_reports_unknown_feature_id(self):
        """Panel with invalid feature_id is reported."""
        registry = PanelRegistry()
        registry.register(_make("a", dock_attr="dock_a", feature_id="nonexistent"))
        window = _mock_window({"dock_a": _mock_dock()})
        issues = validate_dock_integrity(
            registry, window, valid_features={"color", "edit"}
        )
        assert any("unknown feature_id" in i for i in issues)

    def test_reports_shared_dock_attr(self):
        """Two panels sharing the same dock_attr is reported."""
        registry = PanelRegistry()
        registry.register(_make("a", dock_attr="dock_shared"))
        registry.register(_make("b", dock_attr="dock_shared"))
        window = _mock_window({"dock_shared": _mock_dock()})
        issues = validate_dock_integrity(registry, window)
        assert any("shares dock_attr" in i for i in issues)

    def test_valid_feature_id_not_reported(self):
        """Valid feature_ids are not reported as issues."""
        registry = PanelRegistry()
        registry.register(
            _make("a", dock_attr="dock_a", feature_id="color")
        )
        window = _mock_window({"dock_a": _mock_dock()})
        issues = validate_dock_integrity(
            registry, window, valid_features={"color", "edit"}
        )
        assert not any("unknown feature_id" in i for i in issues)

    def test_with_real_module_panels_and_mock_window(self):
        """Validate that real module panels pass integrity with a mock window
        that has all expected dock attributes."""
        registry = _register_all_available()
        all_dock_attrs = [desc.dock_attr for desc in registry.get_all()]

        # Build mock window with all dock_attrs as mock docks
        docks = {attr: _mock_dock() for attr in all_dock_attrs}
        window = _mock_window(docks)

        issues = validate_dock_integrity(
            registry, window,
            valid_features=set(DEFAULT_FEATURE_FLAGS.keys()),
        )

        # Filter out "shares dock_attr" since workspace.export and
        # deliver.export intentionally share dock_export
        real_issues = [
            i for i in issues if "shares dock_attr" not in i
        ]
        assert real_issues == [], (
            f"Unexpected integrity issues: {real_issues}"
        )

    def test_shared_dock_export_is_expected(self):
        """workspace.export and deliver.export share dock_export, which is
        intentional and should be the only shared dock."""
        registry = _register_all_available()
        all_dock_attrs = [desc.dock_attr for desc in registry.get_all()]
        docks = {attr: _mock_dock() for attr in all_dock_attrs}
        window = _mock_window(docks)

        issues = validate_dock_integrity(
            registry, window,
            valid_features=set(DEFAULT_FEATURE_FLAGS.keys()),
        )
        shared = [i for i in issues if "shares dock_attr" in i]
        # The shared dock should be dock_export at most
        shared_attrs = set()
        for issue in shared:
            assert "dock_export" in issue, (
                f"Unexpected shared dock_attr in: {issue}"
            )
            shared_attrs.add("dock_export")
        assert len(shared_attrs) <= 1


# ---------------------------------------------------------------------------
# Feature flag consistency
# ---------------------------------------------------------------------------

class TestFeatureFlagConsistency:
    def test_product_modes_reference_valid_features(self):
        """Every feature in PRODUCT_MODE_PRESETS should be in DEFAULT_FEATURE_FLAGS."""
        for mode, features in PRODUCT_MODE_PRESETS.items():
            for feature in features:
                assert feature in DEFAULT_FEATURE_FLAGS, (
                    f"Product mode {mode!r} references unknown feature {feature!r}"
                )

    def test_feature_flags_fields_match_defaults(self):
        """FeatureFlags dataclass fields should match DEFAULT_FEATURE_FLAGS keys."""
        valid = set(DEFAULT_FEATURE_FLAGS.keys())
        flags = FeatureFlags()
        for feature in valid:
            assert hasattr(flags, feature), (
                f"FeatureFlags missing attribute for {feature!r}"
            )

    def test_color_always_enabled_in_all_modes(self):
        """Color feature should be enabled in every product mode."""
        for mode, features in PRODUCT_MODE_PRESETS.items():
            assert features.get("color") is True, (
                f"Color is disabled in product mode {mode!r}"
            )


class TestSingleton:
    """PanelRegistry singleton tests."""

    def test_get_panel_registry_returns_same(self):
        from src.shared.panels.registry import get_panel_registry
        r1 = get_panel_registry()
        r2 = get_panel_registry()
        assert r1 is r2
