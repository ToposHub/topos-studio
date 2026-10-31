"""
Tests for LayoutProfile and LayoutStore (Phase 3B).

Covers:
- LayoutProfile creation and serialization
- LayoutStore save/load roundtrip
- Panel filtering when restoring
- Legacy page_layouts migration
- Missing panel skip
- Cross-product layout isolation
"""

from __future__ import annotations

import pytest

from src.shared.layout.profile import LayoutProfile, LAYOUT_PROFILE_SCHEMA_VERSION
from src.shared.layout.store import LayoutStore


# ---------------------------------------------------------------------------
# In-memory config stub (avoids touching the filesystem)
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
def sample_profile():
    return LayoutProfile(
        product_mode="studio",
        page_id="Color",
        visible_panels=(
            "color.color_wheel",
            "color.curves",
            "color.analysis",
        ),
        qt_state_hex="0000ff00aabb",
        floating_panels={
            "color.curves": {"x": 100, "y": 200, "w": 400, "h": 300},
        },
        schema_version=LAYOUT_PROFILE_SCHEMA_VERSION,
    )


# ---------------------------------------------------------------------------
# LayoutProfile tests
# ---------------------------------------------------------------------------


class TestLayoutProfile:
    """Tests for LayoutProfile dataclass."""

    def test_creation(self):
        p = LayoutProfile(product_mode="color", page_id="Color")
        assert p.product_mode == "color"
        assert p.page_id == "Color"
        assert p.visible_panels == ()
        assert p.qt_state_hex == ""
        assert p.floating_panels == {}
        assert p.schema_version == LAYOUT_PROFILE_SCHEMA_VERSION

    def test_creation_with_data(self, sample_profile):
        assert sample_profile.visible_panels == (
            "color.color_wheel",
            "color.curves",
            "color.analysis",
        )
        assert sample_profile.qt_state_hex == "0000ff00aabb"
        assert "color.curves" in sample_profile.floating_panels

    def test_empty_product_mode_raises(self):
        with pytest.raises(ValueError, match="product_mode"):
            LayoutProfile(product_mode="", page_id="Color")

    def test_empty_page_id_raises(self):
        with pytest.raises(ValueError, match="page_id"):
            LayoutProfile(product_mode="studio", page_id="")

    def test_to_dict(self, sample_profile):
        d = sample_profile.to_dict()
        assert d["product_mode"] == "studio"
        assert d["page_id"] == "Color"
        assert d["visible_panels"] == [
            "color.color_wheel",
            "color.curves",
            "color.analysis",
        ]
        assert d["qt_state_hex"] == "0000ff00aabb"
        assert d["schema_version"] == LAYOUT_PROFILE_SCHEMA_VERSION
        assert "color.curves" in d["floating_panels"]

    def test_from_dict(self, sample_profile):
        d = sample_profile.to_dict()
        restored = LayoutProfile.from_dict(d)
        assert restored == sample_profile

    def test_roundtrip_serialization(self, sample_profile):
        """to_dict -> from_dict must be lossless."""
        restored = LayoutProfile.from_dict(sample_profile.to_dict())
        assert restored.product_mode == sample_profile.product_mode
        assert restored.page_id == sample_profile.page_id
        assert restored.visible_panels == sample_profile.visible_panels
        assert restored.qt_state_hex == sample_profile.qt_state_hex
        assert restored.floating_panels == sample_profile.floating_panels
        assert restored.schema_version == sample_profile.schema_version

    def test_from_dict_defaults(self):
        """Missing keys should get safe defaults."""
        p = LayoutProfile.from_dict({"product_mode": "color", "page_id": "Color"})
        assert p.visible_panels == ()
        assert p.qt_state_hex == ""
        assert p.schema_version == LAYOUT_PROFILE_SCHEMA_VERSION

    def test_config_key(self, sample_profile):
        key = sample_profile.config_key
        assert key == "layout_profiles/studio/Color"

    def test_make_key(self):
        key = LayoutProfile.make_key("edit", "Edit")
        assert key == "layout_profiles/edit/Edit"

    def test_with_visible_panels(self, sample_profile):
        updated = sample_profile.with_visible_panels(["color.color_wheel"])
        assert updated.visible_panels == ("color.color_wheel",)
        # Original is unchanged (immutable)
        assert len(sample_profile.visible_panels) == 3

    def test_with_qt_state(self, sample_profile):
        updated = sample_profile.with_qt_state("abcdef")
        assert updated.qt_state_hex == "abcdef"
        assert sample_profile.qt_state_hex == "0000ff00aabb"

    def test_with_floating_panels(self, sample_profile):
        new_float = {"color.warper": {"x": 50, "y": 50, "w": 200, "h": 150}}
        updated = sample_profile.with_floating_panels(new_float)
        assert "color.warper" in updated.floating_panels
        assert "color.curves" not in updated.floating_panels

    def test_repr(self, sample_profile):
        r = repr(sample_profile)
        assert "studio" in r
        assert "Color" in r


# ---------------------------------------------------------------------------
# LayoutStore tests
# ---------------------------------------------------------------------------


class TestLayoutStore:
    """Tests for LayoutStore."""

    def test_save_and_load(self, store, sample_profile):
        store.save_profile(sample_profile)
        loaded = store.get_profile("studio", "Color")
        assert loaded is not None
        assert loaded == sample_profile

    def test_load_missing_returns_none(self, store):
        result = store.get_profile("color", "Nonexistent")
        assert result is None

    def test_delete_profile(self, store, sample_profile):
        store.save_profile(sample_profile)
        assert store.delete_profile("studio", "Color") is True
        assert store.get_profile("studio", "Color") is None

    def test_delete_nonexistent(self, store):
        assert store.delete_profile("color", "Ghost") is False

    def test_save_persists_to_config(self, config, sample_profile):
        store = LayoutStore(config=config)
        store.save_profile(sample_profile)
        # Check raw config data
        root = config.get("layout_profiles")
        assert isinstance(root, dict)
        assert "studio" in root
        assert "Color" in root["studio"]

    def test_load_reads_from_config(self, config, sample_profile):
        # Write raw data to config as if previously saved
        config.set("layout_profiles", {
            "studio": {
                "Color": sample_profile.to_dict(),
            }
        })
        store = LayoutStore(config=config)
        loaded = store.get_profile("studio", "Color")
        assert loaded is not None
        assert loaded.visible_panels == sample_profile.visible_panels

    def test_roundtrip_through_config(self, config, sample_profile):
        store1 = LayoutStore(config=config)
        store1.save_profile(sample_profile)

        store2 = LayoutStore(config=config)
        loaded = store2.get_profile("studio", "Color")
        assert loaded is not None
        assert loaded == sample_profile


# ---------------------------------------------------------------------------
# Panel filtering tests
# ---------------------------------------------------------------------------


class TestPanelFiltering:
    """Tests for panel filtering by features and registry."""

    def test_filter_by_enabled_features(self, store):
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=(
                "color.color_wheel",
                "color.curves",
                "edit.media_pool",
                "edit.timeline",
            ),
        )
        # Only color feature enabled
        filtered = store.filter_panels(
            profile, enabled_features={"color", "library"}
        )
        assert "color.color_wheel" in filtered.visible_panels
        assert "color.curves" in filtered.visible_panels
        assert "edit.media_pool" not in filtered.visible_panels
        assert "edit.timeline" not in filtered.visible_panels

    def test_filter_by_registered_panels(self, store):
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=(
                "color.color_wheel",
                "color.curves",
                "color.gone_panel",
            ),
        )
        registered = {"color.color_wheel", "color.curves"}
        filtered = store.filter_panels(
            profile, registered_panel_ids=registered
        )
        assert "color.gone_panel" not in filtered.visible_panels
        assert "color.color_wheel" in filtered.visible_panels

    def test_filter_combined(self, store):
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Edit",
            visible_panels=(
                "edit.media_pool",
                "edit.timeline",
                "edit.removed_panel",
                "color.color_wheel",
                "composite.viewer",
            ),
        )
        filtered = store.filter_panels(
            profile,
            enabled_features={"edit", "color"},
            registered_panel_ids={"edit.media_pool", "edit.timeline", "color.color_wheel"},
        )
        assert set(filtered.visible_panels) == {
            "edit.media_pool",
            "edit.timeline",
            "color.color_wheel",
        }

    def test_no_filter_returns_same_profile(self, store, sample_profile):
        filtered = store.filter_panels(sample_profile)
        assert filtered is sample_profile  # Same object, no filtering

    def test_workspace_panels_always_enabled(self, store):
        """Panels without feature prefix should pass filtering."""
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=("viewport", "toolbar"),
        )
        filtered = store.filter_panels(
            profile, enabled_features=set()
        )
        assert "viewport" in filtered.visible_panels
        assert "toolbar" in filtered.visible_panels


# ---------------------------------------------------------------------------
# Legacy migration tests
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    """Tests for legacy page_layouts migration."""

    def test_migrate_legacy_page_layouts(self, config):
        # Setup legacy data
        config._data["page_layouts"] = {
            "Color": "00aabbcc",
            "Edit": "11dd2233",
            "Composite": "44ff5566",
        }

        store = LayoutStore(config=config)
        count = store.migrate_legacy_page_layouts()

        assert count == 3

        # Verify profiles were created with current product mode
        color_profile = store.get_profile("studio", "Color")
        assert color_profile is not None
        assert color_profile.qt_state_hex == "00aabbcc"

        edit_profile = store.get_profile("studio", "Edit")
        assert edit_profile is not None
        assert edit_profile.qt_state_hex == "11dd2233"

    def test_migrate_no_legacy_data(self, config):
        store = LayoutStore(config=config)
        count = store.migrate_legacy_page_layouts()
        assert count == 0

    def test_migrate_skips_existing(self, config):
        # Setup legacy data
        config._data["page_layouts"] = {
            "Color": "00aabbcc",
        }

        store = LayoutStore(config=config)

        # Pre-create a profile for Color
        existing = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            qt_state_hex="existing_state",
        )
        store.save_profile(existing)

        count = store.migrate_legacy_page_layouts()
        assert count == 0  # Skipped because already exists

        # Verify the existing profile was NOT overwritten
        loaded = store.get_profile("studio", "Color")
        assert loaded.qt_state_hex == "existing_state"

    def test_migrate_invalid_legacy_data_ignored(self, config):
        config._data["page_layouts"] = {
            "Color": "valid_hex",
            "BadEntry": 12345,  # Not a string
        }

        store = LayoutStore(config=config)
        count = store.migrate_legacy_page_layouts()

        assert count == 1  # Only Color was migrated


# ---------------------------------------------------------------------------
# Missing panel skip tests
# ---------------------------------------------------------------------------


class TestMissingPanelSkip:
    """Tests for skipping panels not in the registry."""

    def test_skip_unregistered_panels(self, store):
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=(
                "color.color_wheel",
                "color.removed_panel",
                "color.also_gone",
                "color.curves",
            ),
        )
        registered = {"color.color_wheel", "color.curves"}
        filtered = store.filter_panels(
            profile, registered_panel_ids=registered
        )
        assert set(filtered.visible_panels) == {
            "color.color_wheel",
            "color.curves",
        }

    def test_all_panels_missing_returns_empty(self, store):
        profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=("old.panel1", "old.panel2"),
        )
        registered = set()
        filtered = store.filter_panels(
            profile, registered_panel_ids=registered
        )
        assert filtered.visible_panels == ()


# ---------------------------------------------------------------------------
# Cross-product isolation tests
# ---------------------------------------------------------------------------


class TestCrossProductIsolation:
    """Tests for layout isolation between product modes."""

    def test_profiles_isolated_across_modes(self, store):
        color_profile = LayoutProfile(
            product_mode="color",
            page_id="Color",
            visible_panels=("color.color_wheel",),
        )
        studio_profile = LayoutProfile(
            product_mode="studio",
            page_id="Color",
            visible_panels=("color.color_wheel", "color.curves"),
        )

        store.save_profile(color_profile)
        store.save_profile(studio_profile)

        loaded_color = store.get_profile("color", "Color")
        loaded_studio = store.get_profile("studio", "Color")

        assert loaded_color is not None
        assert loaded_studio is not None
        assert loaded_color.visible_panels == ("color.color_wheel",)
        assert loaded_studio.visible_panels == (
            "color.color_wheel",
            "color.curves",
        )

    def test_delete_one_mode_does_not_affect_other(self, store):
        p1 = LayoutProfile(
            product_mode="color", page_id="Color",
            visible_panels=("color.color_wheel",),
        )
        p2 = LayoutProfile(
            product_mode="studio", page_id="Color",
            visible_panels=("color.color_wheel", "color.curves"),
        )
        store.save_profile(p1)
        store.save_profile(p2)

        store.delete_profile("color", "Color")

        assert store.get_profile("color", "Color") is None
        assert store.get_profile("studio", "Color") is not None

    def test_get_profiles_for_mode(self, store):
        for mode, page in [("color", "Color"), ("studio", "Color"),
                           ("studio", "Edit"), ("edit", "Edit")]:
            store.save_profile(LayoutProfile(
                product_mode=mode, page_id=page,
                visible_panels=(f"{mode}.panel",),
            ))

        studio_profiles = store.get_profiles_for_mode("studio")
        assert "Color" in studio_profiles
        assert "Edit" in studio_profiles
        assert len(studio_profiles) == 2

        color_profiles = store.get_profiles_for_mode("color")
        assert len(color_profiles) == 1
        assert "Color" in color_profiles


# ---------------------------------------------------------------------------
# Default profiles tests
# ---------------------------------------------------------------------------


class TestDefaultProfiles:
    """Tests for default layout profiles."""

    def test_get_default_color_mode(self, store):
        profile = store.get_default_profile("color", "Color")
        assert profile.product_mode == "color"
        assert profile.page_id == "Color"
        assert "color.grading" in profile.visible_panels
        assert profile.qt_state_hex == ""

    def test_get_default_studio_mode(self, store):
        profile = store.get_default_profile("studio", "Edit")
        assert "edit.media_pool" in profile.visible_panels
        assert "edit.timeline" in profile.visible_panels

    def test_get_default_unknown_page_returns_empty(self, store):
        profile = store.get_default_profile("color", "Nonexistent")
        assert profile.visible_panels == ()

    def test_get_or_default_returns_saved(self, store, sample_profile):
        store.save_profile(sample_profile)
        result = store.get_or_default("studio", "Color")
        assert result == sample_profile

    def test_get_or_default_returns_default_when_missing(self, store):
        result = store.get_or_default("color", "Color")
        # Should be the default, not None
        assert result.product_mode == "color"
        assert len(result.visible_panels) > 0

    def test_get_or_default_filters(self, store):
        result = store.get_or_default(
            "studio",
            "Color",
            enabled_features={"color"},
            registered_panel_ids={"color.grading"},
        )
        # Only color.grading should survive both filters
        assert result.visible_panels == ("color.grading",)


# ---------------------------------------------------------------------------
# In-memory store (no config) tests
# ---------------------------------------------------------------------------


class TestInMemoryStore:
    """Tests for LayoutStore without a config backend."""

    def test_save_and_load_without_config(self):
        store = LayoutStore(config=None)
        profile = LayoutProfile(
            product_mode="color",
            page_id="Color",
            visible_panels=("color.color_wheel",),
        )
        store.save_profile(profile)
        loaded = store.get_profile("color", "Color")
        assert loaded is not None
        assert loaded == profile

    def test_migration_no_op_without_config(self):
        store = LayoutStore(config=None)
        count = store.migrate_legacy_page_layouts()
        assert count == 0

    def test_clear_cache(self, store, sample_profile):
        store.save_profile(sample_profile)
        assert store.get_profile("studio", "Color") is not None
        store.clear_cache()
        # After clearing cache, need config to reload
        # With stub config, it should still be there
        loaded = store.get_profile("studio", "Color")
        assert loaded is not None


# ---------------------------------------------------------------------------
# Manifest Phase 3B additions tests
# ---------------------------------------------------------------------------


class TestManifestPhase3B:
    """Tests for Phase 3B manifest additions."""

    def test_manifest_new_fields_default(self):
        from src.shared.project.manifest import ToposManifest

        m = ToposManifest(project_id="test-id")
        assert m.last_saved_by == ""
        assert m.required_features == ()
        assert m.optional_features == ()
        assert m.present_modules == ()

    def test_manifest_roundtrip_with_new_fields(self):
        from src.shared.project.manifest import ToposManifest

        m = ToposManifest(
            project_id="test-id",
            last_saved_by="studio",
            required_features=("color", "edit"),
            optional_features=("composite",),
            present_modules=("color", "edit", "composite", "deliver"),
        )
        d = m.to_dict()
        assert d["last_saved_by"] == "studio"
        assert d["required_features"] == ["color", "edit"]
        assert d["optional_features"] == ["composite"]
        assert d["present_modules"] == ["color", "edit", "composite", "deliver"]

        restored = ToposManifest.from_dict(d)
        assert restored.last_saved_by == "studio"
        assert restored.required_features == ("color", "edit")
        assert restored.optional_features == ("composite",)
        assert restored.present_modules == ("color", "edit", "composite", "deliver")

    def test_manifest_create_with_new_fields(self):
        from src.shared.project.manifest import ToposManifest

        m = ToposManifest.create(
            project_id="test-id",
            last_saved_by="color",
            required_features=("color",),
            present_modules=("color",),
        )
        assert m.last_saved_by == "color"
        assert m.required_features == ("color",)
        assert m.present_modules == ("color",)

    def test_manifest_with_modified_preserves_new_fields(self):
        from src.shared.project.manifest import ToposManifest

        m = ToposManifest(
            project_id="test-id",
            last_saved_by="studio",
            required_features=("color",),
            present_modules=("color", "edit"),
        )
        m2 = m.with_modified()
        assert m2.last_saved_by == "studio"
        assert m2.required_features == ("color",)
        assert m2.present_modules == ("color", "edit")

    def test_manifest_from_dict_missing_new_fields(self):
        """Old manifests without new fields should deserialize with defaults."""
        from src.shared.project.manifest import ToposManifest

        old_data = {
            "format": "topos_project_archive",
            "schema_version": 1,
            "project_id": "test-id",
            "project_mode": "single_asset",
        }
        m = ToposManifest.from_dict(old_data)
        assert m.last_saved_by == ""
        assert m.required_features == ()
        assert m.optional_features == ()
        assert m.present_modules == ()
