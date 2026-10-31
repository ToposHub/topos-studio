"""Phase 0 acceptance tests for the optional Teaching Animation module."""

from __future__ import annotations

import json
import importlib
import sys
import zipfile

import pytest

from src.app.app_context import AppContext
from src.app.feature_registry import FeatureRegistry
from src.features.composite.facade import (
    get_node_providers,
    register_node_provider,
    unregister_node_provider,
)
from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.facade import preview_frame
from src.features.teaching_animation.module import TeachingAnimationModule
from src.features.teaching_animation.ui.preview_bridge import PreviewBridge
from src.shared.feature_flags import FeatureFlags, load_feature_flags
from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_io import ToposProjectIO


def _project() -> ToposProject:
    return ToposProject.create_single_asset("Phase 0", "/tmp/phase0.jpg")


def test_product_presets_and_explicit_flag(monkeypatch):
    monkeypatch.delenv("TOPOS_PRODUCT_MODE", raising=False)
    monkeypatch.delenv("TOPOS_FEATURE_TEACHING_ANIMATION", raising=False)
    assert load_feature_flags().teaching_animation is True

    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    assert load_feature_flags().teaching_animation is True

    monkeypatch.setenv("TOPOS_FEATURE_TEACHING_ANIMATION", "0")
    assert load_feature_flags().teaching_animation is False

    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
    monkeypatch.delenv("TOPOS_FEATURE_TEACHING_ANIMATION")
    assert load_feature_flags().teaching_animation is False


def test_module_discovery_is_lazy_and_disabled_registry_stays_clean():
    disabled = FeatureFlags(teaching_animation=False)
    registry = FeatureRegistry()
    registry.initialize(AppContext(feature_flags=disabled))
    if disabled.is_enabled("teaching_animation"):
        registry.register_module(TeachingAnimationModule())
    assert registry.get_module("teaching_animation") is None

    enabled = FeatureFlags(teaching_animation=True, composite=False)
    enabled_registry = FeatureRegistry()
    enabled_registry.initialize(AppContext(feature_flags=enabled))
    assert enabled_registry.register_module(TeachingAnimationModule()) is True
    assert enabled_registry.get_module("teaching_animation") is not None
    assert enabled_registry.get_all_pages() == []


def test_builtin_loader_discovers_optional_module_only_when_flagged():
    from src.app import module_loader

    flags = FeatureFlags(
        composite=False,
        edit=False,
        deliver=False,
        teaching_animation=True,
        ai_workflow=False,
    )
    registry = FeatureRegistry()
    registry.initialize(AppContext(feature_flags=flags))
    module_loader.register_builtin_modules(registry)
    assert registry.get_module("teaching_animation") is not None


def test_composite_provider_hook_is_boundary_only():
    provider = object()
    assert register_node_provider("phase0-test", provider) is True
    assert get_node_providers()["phase0-test"] is provider
    assert register_node_provider("phase0-test", provider) is True
    assert unregister_node_provider("phase0-test") is True
    assert "phase0-test" not in get_node_providers()


def test_preview_and_bridge_use_identical_frame_bytes():
    scene = {"title": "速度", "arrow_color": "#2563eb"}
    context = RenderContext(fps=24.0, width=320, height=180)
    first = preview_frame(scene, 12, context)
    second = preview_frame(scene, 12, context)
    assert first.ok and second.ok
    assert first.value.data == second.value.data

    html = PreviewBridge().update_frame(first.value)
    assert first.value.data.decode("utf-8") in html


def test_teaching_payload_roundtrip_and_missing_module_fallback(tmp_path, monkeypatch):
    path = str(tmp_path / "phase0.topos")
    scenes = {"schema_version": 1, "scenes": [{"scene_id": "scene-1"}]}
    monkeypatch.setenv("TOPOS_FEATURE_TEACHING_ANIMATION", "1")
    assert ToposProjectIO.save(_project(), path, teaching_animation_data=scenes)

    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read("teaching_animation/scenes.json")) == scenes
        manifest = json.loads(archive.read("manifest.json"))
        assert "teaching_animation" in manifest["present_modules"]

    monkeypatch.setenv("TOPOS_FEATURE_TEACHING_ANIMATION", "0")
    loaded, full_data = ToposProjectIO.load(path)
    assert loaded is not None
    assert full_data is not None
    assert full_data["teaching_animation"] == scenes
    assert full_data["module_fallbacks"]["teaching_animation"]["payload_preserved"] is True

    # A product that does not know the module can still save the project and
    # the payload remains in the archive unchanged.
    assert ToposProjectIO.save(_project(), path)
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read("teaching_animation/scenes.json")) == scenes


def test_physical_module_removal_preserves_core_project_io(
    hide_modules, monkeypatch, tmp_path
):
    """The ordinary project path works when the optional package is absent."""
    path = str(tmp_path / "physical-removal.topos")
    project = _project()

    # Create a project containing optional data before simulating a product
    # build that physically omits the module directory.
    monkeypatch.setenv("TOPOS_FEATURE_TEACHING_ANIMATION", "1")
    scenes = {"schema_version": 1, "scenes": [{"scene_id": "scene-removed"}]}
    assert ToposProjectIO.save(project, path, teaching_animation_data=scenes)

    hide_modules(["teaching_animation"])
    monkeypatch.setenv("TOPOS_FEATURE_TEACHING_ANIMATION", "0")
    for name in list(sys.modules):
        if name.startswith("src.features.teaching_animation"):
            sys.modules.pop(name, None)

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.features.teaching_animation")

    # No optional import is needed to open, save, and preserve the project.
    loaded, full_data = ToposProjectIO.load(path)
    assert loaded is not None
    assert full_data is not None
    assert full_data["module_fallbacks"]["teaching_animation"]["state"] == "missing_module"
    assert full_data["teaching_animation"] == scenes
    assert ToposProjectIO.save(project, path)

    from src.shared.export.export_panel import generate_export_filename

    assert generate_export_filename("Original Filename", "lesson", ".mp4") == "lesson.mp4"

    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read("teaching_animation/scenes.json")) == scenes
        manifest = json.loads(archive.read("manifest.json"))
        assert "teaching_animation" in manifest["present_modules"]
