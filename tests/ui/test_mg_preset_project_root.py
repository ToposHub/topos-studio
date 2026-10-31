"""Project-local mg_presets/ root: scan precedence, write target, refresh.

Covers the third library root introduced for project-portable presets:
``<project dir>/mg_presets/`` joins bundled + user roots while a project is
open, wins preset_id clashes, and receives new saves (falling back to the
user root when no project is open).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.features.teaching_animation.presets import paths
from src.features.teaching_animation.presets.builders import (
    key, scene_document, text_element, track,
)
from src.features.teaching_animation.presets.library import MgPresetLibrary

BUNDLED_ROOT = paths.bundled_preset_root()


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path, monkeypatch):
    """Keep tests away from the real user library and project state."""
    monkeypatch.setenv("TOPOS_MG_PRESET_ROOT", str(tmp_path / "user_lib"))
    paths.set_active_project_dir(None)
    yield
    paths.set_active_project_dir(None)


def _write_preset(root: Path, preset_id: str, *, category: str = "general") -> Path:
    folder = root / category / preset_id
    folder.mkdir(parents=True)
    manifest = {"preset_id": preset_id, "name": preset_id, "category": category}
    (folder / "preset.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "scene.json").write_text("{}", encoding="utf-8")
    return folder


def _scene_data() -> dict:
    return scene_document([
        text_element("t1", 100, 100, "Project Root", size=48,
                     tracks=(track("opacity", key(0, 0.0), key(12, 1.0)),)),
    ], name="Project Root", duration_frames=24).to_dict()


# -- roots ----------------------------------------------------------------


def test_project_root_appended_last_and_cleared(tmp_path):
    assert paths.default_preset_roots() == [
        paths.bundled_preset_root(), paths.user_preset_root()]

    project_file = tmp_path / "video_work" / "demo.topos"
    project_file.parent.mkdir()
    assert paths.set_active_project_file(project_file) == project_file.parent.resolve()
    roots = paths.default_preset_roots()
    assert roots[-1] == project_file.parent.resolve() / "mg_presets"
    assert paths.project_preset_root() == roots[-1]

    paths.set_active_project_file(None)
    assert paths.project_preset_root() is None
    assert len(paths.default_preset_roots()) == 2


def test_project_root_wins_id_clash_and_receives_saves(qapp, tmp_path):
    user_root = paths.user_preset_root()
    _write_preset(user_root, "clash_id")
    project_dir = (tmp_path / "proj").resolve()
    project_root = project_dir / "mg_presets"
    _write_preset(project_root, "clash_id")

    paths.set_active_project_dir(project_dir)
    library = MgPresetLibrary()
    assert library.roots[-1] == project_root
    winner = library.get("clash_id")
    assert winner is not None and winner.origin == "project"
    assert winner.folder == project_root / "general" / "clash_id"

    # New saves go to the project root (auto-created by the write probe).
    record = library.save_preset(name="Fresh In Project", category="titles",
                                 scene_data=_scene_data())
    assert record.folder.is_relative_to(project_root)
    assert not (user_root / "titles").exists()


def test_saves_fall_back_to_user_root_without_project(qapp, tmp_path):
    library = MgPresetLibrary()
    record = library.save_preset(name="No Project", category="general",
                                 scene_data=_scene_data())
    assert record.folder.is_relative_to(paths.user_preset_root().resolve())


def test_refresh_roots_follows_project_switch(qapp, tmp_path):
    library = MgPresetLibrary()
    assert library.refresh_roots() is False  # nothing changed

    paths.set_active_project_dir(tmp_path / "proj_a")
    assert library.refresh_roots() is True
    record = library.save_preset(name="In A", category="general", scene_data=_scene_data())
    assert record.folder.is_relative_to((tmp_path / "proj_a").resolve() / "mg_presets")

    paths.set_active_project_dir(tmp_path / "proj_b")
    assert library.refresh_roots() is True
    assert library.get(record.preset_id) is None  # preset stayed in project A

    # Explicit roots are static by contract: project switches must not
    # silently alter an injected library (tests, custom folders).
    static_library = MgPresetLibrary([BUNDLED_ROOT])
    paths.set_active_project_dir(tmp_path / "proj_c")
    assert static_library.refresh_roots() is False
    assert len(static_library.roots) == 1


def test_panel_show_event_follows_project_switch(qapp, tmp_path):
    from src.features.teaching_animation.ui.asset_library_panel import AssetLibraryPanel

    panel = AssetLibraryPanel()  # default (dynamic) library
    try:
        paths.set_active_project_dir(tmp_path / "opened_late")
        panel.show()  # showEvent → refresh_roots → rebuild
        assert panel.library.roots[-1] == (tmp_path / "opened_late").resolve() / "mg_presets"
    finally:
        panel.deleteLater()
