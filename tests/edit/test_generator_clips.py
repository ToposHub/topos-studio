"""Tests for generator clips (Adjustment Clip / Solid Color).

Covers:
- EffectRegistry exposes the Generators category with both clip templates.
- MediaAsset accepts the new media_types and the generator properties work.
- EditProject factories create valid virtual assets.
- EditRenderTask carries adjustment_layers for the GPU post-composite pass.
- timeline_view wires the generator MIME and drop handler.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.effect_registry import (
    get_effect_registry,
    GENERATOR_EFFECT_TYPES,
)
from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask


# ──────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────

class TestGeneratorRegistry:
    def test_generator_category_exists(self):
        reg = get_effect_registry()
        gens = reg.get_effects_by_category("generator")
        types = {g.effect_type for g in gens}
        assert "adjustment_clip" in types
        assert "solid_color" in types

    def test_generator_display_name(self):
        reg = get_effect_registry()
        assert reg.get_category_display_name("generator") == "Generators"

    def test_generator_effect_types_frozenset(self):
        assert "adjustment_clip" in GENERATOR_EFFECT_TYPES
        assert "solid_color" in GENERATOR_EFFECT_TYPES
        # Generators are NOT audio mixer effects
        from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES
        assert GENERATOR_EFFECT_TYPES.isdisjoint(AUDIO_MIXER_EFFECT_TYPES)

    def test_generator_category_in_order(self):
        """Generators appear in the panel category order (between stylize & plugin)."""
        reg = get_effect_registry()
        cats = reg.get_categories()
        assert "generator" in cats
        # stylize before generator before plugin
        assert cats.index("stylize") < cats.index("generator")
        assert cats.index("generator") < cats.index("plugin")


# ──────────────────────────────────────────────────────────────────────────
# MediaAsset generator types
# ──────────────────────────────────────────────────────────────────────────

class TestMediaAssetGenerators:
    def test_adjustment_asset_allows_empty_path(self):
        from src.features.edit.core.project import MediaAsset
        a = MediaAsset(
            asset_id="adj-1", path="", media_type="adjustment",
            display_name="Adj", metadata={},
        )
        assert a.is_adjustment
        assert a.is_generator
        assert a.media_type == "adjustment"

    def test_solid_asset_carries_color(self):
        from src.features.edit.core.project import MediaAsset
        s = MediaAsset(
            asset_id="sol-1", path="", media_type="solid",
            display_name="Sol", metadata={"color": [0.2, 0.4, 0.6]},
        )
        assert s.is_generator
        assert not s.is_adjustment
        assert s.solid_color == (0.2, 0.4, 0.6)

    def test_solid_color_defaults_to_black_when_missing(self):
        from src.features.edit.core.project import MediaAsset
        s = MediaAsset(
            asset_id="sol-2", path="", media_type="solid",
            display_name="Sol", metadata={},
        )
        assert s.solid_color == (0.0, 0.0, 0.0)

    def test_non_generator_solid_color_is_none(self):
        from src.features.edit.core.project import MediaAsset
        v = MediaAsset(
            asset_id="vid-1", path="/x.mp4", media_type="video",
            display_name="V", metadata={},
        )
        assert v.solid_color is None
        assert not v.is_generator

    def test_invalid_media_type_rejected(self):
        from src.features.edit.core.project import MediaAsset
        with pytest.raises(ValueError):
            MediaAsset(
                asset_id="x", path="", media_type="bogus",
                display_name="X", metadata={},
            )


# ──────────────────────────────────────────────────────────────────────────
# EditProject factories
# ──────────────────────────────────────────────────────────────────────────

class TestProjectFactories:
    def _make_empty_project(self):
        from src.features.edit.core.project import EditProject, ProjectBin
        import uuid
        root_bin_id = str(uuid.uuid4())
        root_bin = ProjectBin(bin_id=root_bin_id, name="Root", parent_id=None)
        project = EditProject(
            project_id=str(uuid.uuid4()),
            name="t",
            assets={},
            bins={root_bin_id: root_bin},
            timelines={},
            compositions={},
            color_stacks={},
            comp_clips={},
            render_cache_manifests={},
            render_cache_settings=None,
            root_bin_id=root_bin_id,
            active_timeline_id=None,
            active_comp_clip_id=None,
            created_at=0.0,
            modified_at=0.0,
        )
        return project, root_bin_id

    def test_create_adjustment_clip(self):
        project, root_bin_id = self._make_empty_project()
        new_project, asset_id = project.create_adjustment_clip(name="My Adj")
        asset = new_project.assets[asset_id]
        assert asset.is_adjustment
        assert asset.media_type == "adjustment"
        assert asset.display_name == "My Adj"
        # Added to root bin
        assert asset_id in new_project.bins[root_bin_id].asset_ids

    def test_create_solid_color_asset(self):
        project, root_bin_id = self._make_empty_project()
        new_project, asset_id = project.create_solid_color_asset(
            name="Red", color_rgb=(1.0, 0.0, 0.0)
        )
        asset = new_project.assets[asset_id]
        assert asset.media_type == "solid"
        assert asset.solid_color == (1.0, 0.0, 0.0)
        assert asset.display_name == "Red"


# ──────────────────────────────────────────────────────────────────────────
# EditRenderTask adjustment_layers field
# ──────────────────────────────────────────────────────────────────────────

class TestEditRenderTaskAdjustment:
    def test_default_adjustment_layers_empty(self):
        t = EditRenderTask(frame=0, generation_id=1, layers=[])
        assert t.adjustment_layers == []

    def test_adjustment_layers_carried(self):
        t = EditRenderTask(
            frame=5, generation_id=2, layers=[],
            adjustment_layers=[("fake_layer", ("effect1",))],
        )
        assert len(t.adjustment_layers) == 1
        assert t.adjustment_layers[0][1] == ("effect1",)


# ──────────────────────────────────────────────────────────────────────────
# Effects panel MIME
# ──────────────────────────────────────────────────────────────────────────

class TestGeneratorMIME:
    def test_generator_mime_constant(self):
        from src.features.edit.ui import edit_effects_panel as ep
        assert ep.MIME_TYPE_GENERATOR == "application/x-topos-generator"

    def test_generator_mime_helpers_exist(self):
        # QMimeData.hasData needs a real GUI session; here we just verify the
        # helper functions exist and reference the correct MIME constant.
        from src.features.edit.ui import edit_effects_panel as ep
        assert callable(ep.is_generator_mime_data)
        assert callable(ep.get_generator_type_from_mime_data)

    def test_generator_mime_distinct_from_effect_constant(self):
        from src.features.edit.ui import edit_effects_panel as ep
        # The two MIME constants must differ so the timeline drop router can
        # tell generator drags apart from effect drags.
        assert ep.MIME_TYPE_GENERATOR != ep.MIME_TYPE_EFFECT


# ──────────────────────────────────────────────────────────────────────────
# timeline_view wiring
# ──────────────────────────────────────────────────────────────────────────

class TestTimelineViewGeneratorWiring:
    def test_generator_mime_constant_in_view(self):
        from src.features.edit.ui import timeline_view as tv
        assert tv.MIME_TYPE_GENERATOR == "application/x-topos-generator"

    def test_drop_handler_methods_exist(self):
        from src.features.edit.ui import timeline_view as tv
        assert hasattr(tv.TimelineView, "_handle_generator_drop")
        assert hasattr(tv.TimelineView, "_handle_generator_drag_move")
