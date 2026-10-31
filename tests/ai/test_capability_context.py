"""TM-06: AI capability context provider — registry-derived manifest.

The point of TM-06 is that the prompt must NOT hand-maintain capability lists.
These tests pin the contract: the manifest is derived from the live
registries, so retiring a capability removes it from the manifest and the
prompt text; and the manifest is deterministic for a given build.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.ai.capability_context import (
    CAPABILITY_CONTEXT_SCHEMA_VERSION, DEFAULT_COMMANDS, CommandDescriptor,
    CommandParam, build_capability_manifest, default_capability_manifest,
)
from src.features.teaching_animation.core import (
    ExecutableTemplate, ExecutableTemplateRegistry, ParamSpec, TemplateBuildContext,
    create_builtin_template_registry,
)
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.motion.presets import (
    MotionPreset, MotionPresetRegistry, create_default_preset_registry,
)


@pytest.fixture(scope="module")
def manifest():
    return default_capability_manifest()


class TestManifestStructure:
    def test_schema_version_pinned(self, manifest):
        assert manifest.schema_version == CAPABILITY_CONTEXT_SCHEMA_VERSION == 1

    def test_lists_all_fifteen_templates(self, manifest):
        assert len(manifest.templates) == 15
        for entry in manifest.templates:
            assert {"template_id", "version", "params", "capabilities"} <= entry.keys()

    def test_template_ids_match_registry(self, manifest):
        reg = create_builtin_template_registry()
        assert set(manifest.template_ids()) == {t.template_id for t in reg.list_templates()}

    def test_lists_default_presets(self, manifest):
        reg = create_default_preset_registry()
        assert set(manifest.preset_ids()) == set(reg.ids())
        # Each preset carries its default params for the model.
        pop = next(p for p in manifest.presets if p["preset_id"] == "pop")
        assert "start" in pop["default_params"]

    def test_commands_have_unique_names_and_categories(self, manifest):
        names = manifest.command_names()
        assert len(names) == len(set(names))
        assert "apply_template" in names and "set_brand_theme" in names

    def test_error_codes_include_render_and_quality(self, manifest):
        codes = {e["code"] for e in manifest.error_codes}
        # RenderErrorCode members:
        assert {"cancelled", "invalid_scene", "missing_asset", "unsupported_backend"} <= codes
        # Quality-gate supplements:
        assert {"font_fallback", "cache_mismatch", "css_budget_exceeded"} <= codes


class TestDeterminism:
    def test_prompt_text_is_stable(self, manifest):
        assert manifest.to_prompt_text() == manifest.to_prompt_text()
        # The prompt section mentions templates and commands by name.
        text = manifest.to_prompt_text()
        assert "apply_template" in text and "fade" in text

    def test_to_dict_roundtrips(self, manifest):
        data = manifest.to_dict()
        assert data["schema_version"] == manifest.schema_version
        assert len(data["templates"]) == len(manifest.templates)


class TestRetiringACapability:
    """The core TM-06 guarantee: removing a capability from a registry removes
    it from the manifest, so the model can no longer be told it is available."""

    def test_retired_template_disappears(self):
        custom = ExecutableTemplateRegistry()
        # Register only one template -> manifest should expose only that one.
        def _build(params, ctx):
            return TeachingSceneDocument(
                scene_id="x", schema_version=2, name="x", width=10, height=10,
                fps_num=24, fps_den=1, duration_frames=1, background={"color": "#000"},
                elements=(), animations=(),
            )
        custom.register(ExecutableTemplate(
            template_id="lone", version=1, display_name="Lone",
            params_schema=(ParamSpec("a", "string", default="b"),), build=_build))
        m = build_capability_manifest(custom, create_default_preset_registry())
        assert m.template_ids() == ("lone",)
        assert "lone" in m.to_prompt_text()
        assert "title_card" not in m.to_prompt_text()

    def test_custom_presets_surface(self):
        preset_reg = MotionPresetRegistry()
        preset_reg.register(MotionPreset("custom", 1, "desc", default_params={"start": 0},
                                         build=lambda **kw: (lambda f, fps: 0.0)))
        m = build_capability_manifest(create_builtin_template_registry(), preset_reg)
        assert "custom" in m.preset_ids()
        assert "fade" not in m.preset_ids()


class TestCommandDescriptors:
    def test_every_default_command_is_reversible(self):
        # Every high-level command maps onto the undoable SceneCommandStack.
        assert all(c.reversible for c in DEFAULT_COMMANDS)

    def test_command_param_to_dict_includes_enum(self):
        cp = CommandParam("kind", "enum", enum=("cut", "dissolve"))
        data = cp.to_dict()
        assert data["enum"] == ["cut", "dissolve"]

    def test_custom_command_appears_in_manifest(self):
        extra = CommandDescriptor("custom_cmd", "scene", (CommandParam("x", "string"),), "test")
        m = build_capability_manifest(create_builtin_template_registry(),
                                      create_default_preset_registry(),
                                      commands=DEFAULT_COMMANDS + (extra,))
        assert "custom_cmd" in m.command_names()
