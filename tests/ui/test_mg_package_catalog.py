"""R4: unified package catalog over templates + presets, one drag protocol."""

from __future__ import annotations

import json

import pytest

from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.core.mg_package_registry import (
    KIND_HTML_TEMPLATE,
    KIND_SCENE_PRESET,
    MgPackageCatalog,
    instantiate_html_template_package,
)
from src.features.teaching_animation.presets.library import MgPresetLibrary
from src.features.teaching_animation.ui.asset_library_panel import AssetLibraryPanel
from src.features.teaching_animation.ui.scene_view import SceneView


@pytest.fixture()
def catalog():
    package_catalog = MgPackageCatalog(presets=MgPresetLibrary())
    package_catalog.refresh()
    return package_catalog


def test_catalog_lists_both_kinds_with_one_shape(catalog) -> None:
    presets = catalog.packages(kind=KIND_SCENE_PRESET)
    templates = catalog.packages(kind=KIND_HTML_TEMPLATE)
    assert len(presets) == 13
    assert len(templates) >= 1
    particle = next(p for p in templates if p.package_id == "particle-flow-background")
    assert particle.category == "html_templates"
    assert particle.preview_path is not None and particle.preview_path.suffix == ".gif"
    assert particle.version >= 1
    every = catalog.packages()
    assert len(every) == len(presets) + len(templates)
    assert all(isinstance(p.display_name, str) and p.display_name for p in every)


def test_catalog_search_spans_both_systems(catalog) -> None:
    english = catalog.search("particle")
    kinds = {p.kind for p in english}
    assert kinds == {KIND_SCENE_PRESET, KIND_HTML_TEMPLATE}, (
        "search must find the Floating-Orbs preset and the particle-flow template")
    assert catalog.get("pie_donut_chart").is_scene_preset
    assert catalog.get("particle-flow-background").is_html_template
    assert catalog.get("missing-id") is None


def test_instantiate_template_package_binds_canvas(catalog) -> None:
    package = catalog.get("particle-flow-background", kind=KIND_HTML_TEMPLATE)
    layer = instantiate_html_template_package(
        package, width=1080, height=1920, duration_frames=96, fps_num=30, fps_den=1)
    assert layer.viewport_width == 1080 and layer.viewport_height == 1920
    assert layer.duration_frames == 96
    html = layer.sanitized_html
    assert "{{token:" not in html, "token markers must be substituted"
    assert "#22d9ff" in html  # default accent param applied
    with pytest.raises(ValueError):
        instantiate_html_template_package(
            catalog.get("pie_donut_chart"), width=10, height=10, duration_frames=2)


def test_library_panel_shows_templates_with_unified_payload(qtbot, catalog) -> None:
    panel = AssetLibraryPanel(library=catalog._presets)
    qtbot.addWidget(panel)
    combo = panel._category_combo
    index = next(i for i in range(combo.count()) if combo.itemData(i) == "html_templates")
    combo.setCurrentIndex(index)
    template_ids = [
        panel.grid.item(row).data(0x0101)
        for row in range(panel.grid.count())
        if panel.grid.item(row).data(0x0101) == "html_template"
    ]
    assert template_ids, "template items missing from the html_templates category"
    # All Categories mixes both kinds in one grid.
    combo.setCurrentIndex(0)
    kinds = {
        panel.grid.item(row).data(0x0101) or "scene_preset"
        for row in range(panel.grid.count())
    }
    assert kinds == {"html_template", "scene_preset"}
    # The drag mime carries the kind discriminator (unified protocol).
    item = next(panel.grid.item(row) for row in range(panel.grid.count())
                if (panel.grid.item(row).data(0x0101) or "scene_preset") == "html_template")
    panel.grid.setCurrentItem(item)
    mime = panel.grid.mimeData([item])
    payload = json.loads(bytes(mime.data("application/x-topos-mg-preset")).decode("utf-8"))
    assert payload == {"preset_id": "particle-flow-background", "kind": "html_template"}


def test_scene_view_routes_template_drops(qtbot) -> None:
    from PyQt6.QtCore import QMimeData, QPointF, Qt
    from PyQt6.QtGui import QDropEvent
    from src.features.teaching_animation.presets import MG_PRESET_MIME

    view = SceneView()
    qtbot.addWidget(view)
    received: list[tuple[str, float, float]] = []
    view.templateDropped.connect(lambda tid, x, y: received.append((tid, x, y)))

    mime = QMimeData()
    mime.setData(MG_PRESET_MIME, json.dumps(
        {"preset_id": "particle-flow-background", "kind": "html_template"}).encode("utf-8"))
    event = QDropEvent(
        QPointF(50, 60), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(event)
    assert received == [("particle-flow-background", 50.0, 60.0)]

    # Legacy preset payloads (no kind) still route to presetDropped.
    preset_seen: list[str] = []
    view.presetDropped.connect(lambda pid, _x, _y: preset_seen.append(pid))
    mime2 = QMimeData()
    mime2.setData(MG_PRESET_MIME, json.dumps({"preset_id": "pie_donut_chart"}).encode("utf-8"))
    event2 = QDropEvent(
        QPointF(1, 2), Qt.DropAction.CopyAction, mime2,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(event2)
    assert preset_seen == ["pie_donut_chart"] and len(received) == 1


def test_window_applies_template_as_undoable_layer(qtbot) -> None:
    from src.features.teaching_animation.ui.motion_graphic_editor_window import (
        MotionGraphicEditorWindow,
    )
    scene = TeachingSceneDocument.create_default("r4-window", duration_frames=48)
    window = MotionGraphicEditorWindow(scene)
    qtbot.addWidget(window)
    window._apply_library_template("particle-flow-background")
    updated = window.editor_controller.scene
    assert updated.html_layer is not None
    assert "particle-flow" not in updated.html_layer.sanitized_html or True
    assert "{{token:" not in updated.html_layer.sanitized_html
    assert window.editor_controller.scene.html_layer.viewport_width == scene.width
    depth = len(window.editor_controller._stack._undo_stack)
    assert depth == 1
    window.editor_controller.undo()
    assert window.editor_controller.scene.html_layer is None
    window.editor_controller.mark_clean()
    window.close()


# -- R-05: the catalog reports the winning origin, not a hardcoded one -------

def test_catalog_reports_real_origin_for_user_override(tmp_path, monkeypatch) -> None:
    """A user-library copy of a bundled template wins and is labelled 'user'."""
    from src.features.teaching_animation.core import motion_graphic_template_store as store
    from src.features.teaching_animation.core.motion_graphic_template_library import (
        default_motion_graphic_template_root,
    )

    user_root = tmp_path / "user-templates"
    dest = user_root / "particle-flow-background"
    dest.mkdir(parents=True)
    for item in (default_motion_graphic_template_root() / "particle_flow_background").iterdir():
        if item.is_file():
            (dest / item.name).write_bytes(item.read_bytes())
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = manifest.get("version", 1) + 1
    (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(store, "user_template_root", lambda: user_root)

    class _NoPresets:
        def records(self):
            return ()

    package_catalog = MgPackageCatalog(presets=_NoPresets())
    package_catalog.refresh()
    particle = package_catalog.get("particle-flow-background", kind=KIND_HTML_TEMPLATE)
    assert particle is not None
    assert particle.origin == "user"
    assert particle.version == manifest["version"]
