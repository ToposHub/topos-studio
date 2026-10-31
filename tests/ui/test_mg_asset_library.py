"""Asset library (MG presets): core library, fragment grafting, panel and window wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.features.teaching_animation.core.commands import InsertSceneFragmentCommand
from src.features.teaching_animation.core.hierarchy import (
    read_hierarchy,
    write_hierarchy_into,
    Hierarchy,
)
from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.core.validator import validate_scene
from src.features.teaching_animation.presets import (
    MG_PRESET_MIME,
    MgPresetLibrary,
    PresetError,
    builders,
    bundled_preset_root,
    extract_fragment,
    fragment_max_keyframe_frame,
    load_preset_scene,
    prepare_insert,
)
from src.shared.keyframe import Keyframe, KeyframeTrack

BUNDLED_ROOT = bundled_preset_root()
EXPECTED_CATEGORIES = {"backgrounds", "charts", "text", "particles", "light", "shapes3d"}


def _bundled_library() -> MgPresetLibrary:
    return MgPresetLibrary([BUNDLED_ROOT])


def _host_scene(*, elements: tuple[SceneElement, ...] = ()) -> TeachingSceneDocument:
    return TeachingSceneDocument.from_dict({
        "scene_id": "host_scene", "name": "Host", "width": 1920, "height": 1080,
        "fps_num": 24, "fps_den": 1, "duration_frames": 60,
        "background": {"color": "#111827"},
        "elements": [element.to_dict() for element in elements],
        "animations": [],
    })


def _element(element_id: str, x: float, y: float, z: int = 0) -> SceneElement:
    return SceneElement(
        element_id=element_id, type="shape", z_index=z,
        transform=SceneTransform(x=x, y=y, width=100, height=80),
        style={"fill": "#ff8800"}, content={"kind": "rect"})


# -- library discovery & loading -----------------------------------------

def test_bundled_library_discovers_categorized_presets():
    library = _bundled_library()
    assert library.errors == []
    records = library.records()
    assert len(records) >= 12
    assert set(library.categories()) == EXPECTED_CATEGORIES
    ids = {record.preset_id for record in records}
    assert len(ids) == len(records), "preset ids must be unique"
    folders = {record.folder.parent.name for record in records}
    assert folders <= EXPECTED_CATEGORIES, "every preset must live in its category folder"


def test_every_bundled_preset_loads_validates_and_has_gif():
    library = _bundled_library()
    for record in library.records():
        scene = load_preset_scene(record.manifest, record.folder)
        assert scene.elements, record.preset_id
        assert validate_scene(scene).is_valid, record.preset_id
        assert record.preview_path is not None, f"{record.preset_id} has no preview.gif"


def test_load_preset_scene_folds_scene_animations_into_local_tracks():
    """A scene.json carrying scene-level animations loads as pure elements."""
    library = _bundled_library()
    record = next(r for r in library.records() if r.manifest.entry == "build.py")
    scene = load_preset_scene(record.manifest, record.folder)
    assert scene.animations == ()
    assert any(element.local_animation_tracks for element in scene.elements)


def test_loader_rejects_broken_package(tmp_path: Path):
    library = _bundled_library()
    record = library.records()[0]
    from src.features.teaching_animation.presets.manifest import PresetManifest
    with pytest.raises(PresetError):
        load_preset_scene(PresetManifest(preset_id="ghost", name="Ghost",
                                         category=record.manifest.category),
                          tmp_path)


# -- fragment preparation --------------------------------------------------

def test_prepare_insert_remaps_ids_recenters_and_stacks_z():
    preset = load_preset_scene(*_first_record("charts", "pie_donut_chart"))
    host = _host_scene(elements=(_element("host_a", 0, 0, z=5),))
    elements, parents = prepare_insert(preset, host, target_x=400.0, target_y=300.0)
    new_ids = [element.element_id for element in elements]
    assert len(set(new_ids)) == len(new_ids)
    assert not (set(new_ids) & {"host_a", *{e.element_id for e in preset.elements}})
    # Fragment bbox centred on the drop point.
    min_x = min(e.transform.x for e in elements)
    min_y = min(e.transform.y for e in elements)
    max_x = max(e.transform.x + e.transform.width for e in elements)
    max_y = max(e.transform.y + e.transform.height for e in elements)
    assert (min_x + max_x) / 2 == pytest.approx(400.0, abs=2.0)
    assert (min_y + max_y) / 2 == pytest.approx(300.0, abs=2.0)
    # Stacked above the host's z=5 layer, keeping internal order.
    assert min(e.z_index for e in elements) == 6
    z_original = [e.z_index for e in preset.elements]
    z_inserted = [e.z_index for e in elements]
    assert sorted(z_inserted) == [value - min(z_original) + 6 for value in sorted(z_original)]


def _first_record(category: str, preset_id: str):
    library = _bundled_library()
    record = library.get(preset_id)
    assert record is not None and record.manifest.category == category
    return record.manifest, record.folder


def test_prepare_insert_remaps_inner_hierarchy():
    preset = TeachingSceneDocument.from_dict({
        "scene_id": "frag", "name": "Frag", "width": 1920, "height": 1080,
        "fps_num": 24, "fps_den": 1, "duration_frames": 30,
        "background": {"color": "#000000"},
        "elements": [
            {"element_id": "plate", "type": "shape", "z_index": 0,
             "transform": {"x": 100, "y": 100, "width": 400, "height": 300},
             "style": {"fill": "#112233"}, "content": {"kind": "rect"}},
            {"element_id": "child", "type": "text", "z_index": 1,
             "transform": {"x": 120, "y": 140, "width": 200, "height": 60},
             "style": {}, "content": "hi"},
        ],
        "animations": [],
        "metadata": write_hierarchy_into({}, Hierarchy(parents={"child": "plate"})),
    })
    host = _host_scene()
    elements, parents = prepare_insert(preset, host, target_x=960.0, target_y=540.0)
    assert len(elements) == 2
    assert len(parents) == 1
    child_id, parent_id = next(iter(parents.items()))
    assert {element.element_id for element in elements} == {child_id, parent_id}


def test_insert_scene_fragment_command_roundtrip_with_hierarchy():
    host = _host_scene()
    preset = TeachingSceneDocument.from_dict({
        "scene_id": "frag", "name": "Frag", "width": 1920, "height": 1080,
        "fps_num": 24, "fps_den": 1, "duration_frames": 30,
        "background": {"color": "#000000"},
        "elements": [
            {"element_id": "plate", "type": "shape", "z_index": 0,
             "transform": {"x": 0, "y": 0, "width": 50, "height": 50},
             "style": {"fill": "#112233"}, "content": {"kind": "rect"}},
            {"element_id": "child", "type": "text", "z_index": 1,
             "transform": {"x": 10, "y": 10, "width": 30, "height": 20},
             "style": {}, "content": "x"},
        ],
        "animations": [],
        "metadata": write_hierarchy_into({}, Hierarchy(parents={"child": "plate"})),
    })
    elements, parents = prepare_insert(preset, host, target_x=100.0, target_y=100.0)
    command = InsertSceneFragmentCommand(elements, parents)
    inserted = command.apply(host)
    assert len(inserted.elements) == 2
    assert read_hierarchy(inserted.metadata).parents == parents
    undone = command.undo(inserted)
    assert undone.elements == ()
    assert "hierarchy" not in undone.metadata
    redone = command.apply(undone)
    assert len(redone.elements) == 2
    assert read_hierarchy(redone.metadata).parents == parents


def test_extract_fragment_includes_descendants_and_folds_animations():
    parent = _element("plate", 200, 100, z=1)
    child = SceneElement(
        element_id="child", type="text", z_index=2,
        transform=SceneTransform(x=220, y=140, width=120, height=40),
        style={}, content="label")
    outsider = _element("outside", 900, 900, z=3)
    host = TeachingSceneDocument.from_dict({
        "scene_id": "host", "name": "Host", "width": 1920, "height": 1080,
        "fps_num": 24, "fps_den": 1, "duration_frames": 60,
        "background": {"color": "#111827"},
        "elements": [parent.to_dict(), child.to_dict(), outsider.to_dict()],
        "animations": [{
            "animation_id": "anim_1", "element_id": "child", "property": "x",
            "param_name": "x",
            "keyframes": [
                {"frame": 0, "value": 220.0},
                {"frame": 30, "value": 320.0, "interpolation": "ease_out"},
            ],
            "min_val": 0.0, "max_val": 1000.0,
        }],
        "metadata": write_hierarchy_into({}, Hierarchy(parents={"child": "plate"})),
    })
    data = extract_fragment(host, ["plate"])
    fragment = TeachingSceneDocument.from_dict(data)
    assert validate_scene(fragment).is_valid
    assert {e.element_id for e in fragment.elements} == {"plate", "child"}
    assert all(e.transform.x >= 0 and e.transform.y >= 0 for e in fragment.elements)
    assert min(e.transform.x for e in fragment.elements) == pytest.approx(0.0)
    child_element = fragment.element_by_id("child")
    assert child_element is not None
    assert any(track.param_name == "x" for track in child_element.local_animation_tracks)
    assert fragment.animations == ()
    assert read_hierarchy(fragment.metadata).parents == {"child": "plate"}


def test_fragment_max_keyframe_frame():
    track = KeyframeTrack(param_name="opacity", keyframes=[Keyframe(0, 0.0), Keyframe(41, 1.0)],
                          min_val=0.0, max_val=1.0)
    element = SceneElement(
        element_id="e", type="shape", transform=SceneTransform(),
        style={}, content={"kind": "rect"}, local_animation_tracks=(track,))
    assert fragment_max_keyframe_frame((element,)) == 41
    assert fragment_max_keyframe_frame(()) == -1


# -- GIF preview ------------------------------------------------------------

def test_render_preset_gif_produces_animation(qapp, tmp_path: Path):
    from src.features.teaching_animation.presets import render_preset_gif
    scene = builders.scene_document([
        builders.shape_element("bg", 0, 0, 1920, 1080, kind="rect", fill="#102040"),
        builders.shape_element(
            "pulse", 800, 400, 320, 320, kind="circle", fill="#22d3ee",
            tracks=(builders.track("opacity",
                                   builders.key(0, 0.2), builders.key(20, 1.0),
                                   builders.key(40, 0.2)),)),
    ], name="GIF Probe", duration_frames=41)
    out = render_preset_gif(scene, tmp_path / "probe.gif", width=320, max_frames=8)
    from PIL import Image
    with Image.open(out) as gif:
        assert gif.n_frames >= 6
        assert gif.size == (320, 180)
        # Sample the circle centre in scaled coordinates (scene 960,560 -> /6).
        # Sampled frames 6 (opacity 0.44) and 17 (opacity 0.88) must differ.
        gif.seek(1)
        first = gif.convert("RGB").getpixel((160, 93))
        gif.seek(3)
        peak = gif.convert("RGB").getpixel((160, 93))
    assert first != peak, "sampled frames should differ across the animation"


# -- save / delete / import / export ---------------------------------------

def _sample_scene_data() -> dict:
    return builders.scene_document([
        builders.text_element("t1", 100, 100, "Saved Preset", size=64),
        builders.shape_element("s1", 100, 220, 400, 20, kind="rect", fill="#ff8800",
                               tracks=(builders.track("opacity", builders.key(0, 0.0),
                                                      builders.key(20, 1.0)),)),
    ], name="Saved Preset", duration_frames=30).to_dict()


def test_save_delete_and_bundled_protection(tmp_path: Path, qapp):
    library = MgPresetLibrary([BUNDLED_ROOT, tmp_path / "user"])
    record = library.save_preset(name="测试预设 Save Me", category="general",
                                 scene_data=_sample_scene_data(),
                                 description="demo", tags=["demo", "测试"])
    assert record.folder.is_dir()
    assert (record.folder / "preset.json").is_file()
    assert (record.folder / "scene.json").is_file()
    assert (record.folder / "preview.gif").is_file()
    assert record.manifest.category == "general"
    assert library.get(record.preset_id) is not None
    assert library.search("测试")[0].preset_id == record.preset_id
    # Bundled presets are protected from in-app deletion.
    bundled = _bundled_library().records()[0]
    with pytest.raises(PresetError):
        library.delete(bundled.preset_id)
    assert library.delete(record.preset_id) is True
    assert library.get(record.preset_id) is None
    assert not record.folder.exists()


def test_export_import_zip_roundtrip(tmp_path: Path):
    source = _bundled_library()
    record = source.get("pie_donut_chart")
    assert record is not None
    archive = source.export_to(tmp_path / "share.zip", [record.preset_id])
    assert archive.exists()
    user_root = tmp_path / "imported"
    target = MgPresetLibrary([BUNDLED_ROOT, user_root])
    imported = target.import_from(archive)
    assert imported.manifest.name == record.manifest.name
    assert imported.manifest.category == record.manifest.category
    assert imported.preview_path is not None
    assert load_preset_scene(imported.manifest, imported.folder).elements


def test_import_rejects_unsafe_zip_entries(tmp_path: Path):
    import zipfile
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as bundle:
        bundle.writestr("../escape.txt", "bad")
    library = MgPresetLibrary([BUNDLED_ROOT, tmp_path / "user"])
    with pytest.raises(PresetError):
        library.import_from(evil)


def test_import_folder_directly(tmp_path: Path):
    source = _bundled_library()
    record = source.get("neon_glow_title")
    assert record is not None
    target = MgPresetLibrary([BUNDLED_ROOT, tmp_path / "user"])
    imported = target.import_from(record.folder)
    assert imported.manifest.name == record.manifest.name


# -- panel & window integration --------------------------------------------

@pytest.fixture()
def library(qapp) -> MgPresetLibrary:
    return MgPresetLibrary([BUNDLED_ROOT])


def test_panel_lists_categories_and_emits_activation(qapp, library):
    from PyQt6.QtCore import Qt
    from src.features.teaching_animation.ui.asset_library_panel import AssetLibraryPanel
    panel = AssetLibraryPanel(library=library)
    assert panel.grid.count() >= 12
    # R4: the unified catalog adds one "HTML Templates" section on top of
    # the preset categories.
    assert panel._category_combo.count() == 1 + len(EXPECTED_CATEGORIES) + 1
    panel._search_edit.setText("饼")
    assert panel.grid.count() == 1
    activated: list[str] = []
    panel.presetActivated.connect(lambda preset_id: activated.append(preset_id))
    panel.grid.itemDoubleClicked.emit(panel.grid.item(0))
    assert activated and activated[0] == "pie_donut_chart"
    # Drag payload carries the preset mime.
    item = panel.grid.item(0)
    mime = panel.grid.mimeData([item])
    assert mime.hasFormat(MG_PRESET_MIME)
    payload = json.loads(bytes(mime.data(MG_PRESET_MIME)).decode("utf-8"))
    assert payload == {"preset_id": "pie_donut_chart", "kind": "scene_preset"}
    assert mime.text() == item.text()
    del Qt


def test_scene_view_accepts_preset_drop(qapp):
    from PyQt6.QtCore import QMimeData, QPointF, Qt
    from src.features.teaching_animation.ui.scene_view import SceneView
    view = SceneView()
    assert view.acceptDrops()
    view.set_scene(_host_scene())
    mime = QMimeData()
    mime.setData(MG_PRESET_MIME, json.dumps({"preset_id": "neon_glow_title"}).encode("utf-8"))
    dropped: list[tuple[str, float, float]] = []
    view.presetDropped.connect(lambda pid, x, y: dropped.append((pid, x, y)))
    from PyQt6.QtGui import QDropEvent
    event = QDropEvent(QPointF(50.0, 30.0), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(event)
    assert dropped and dropped[0][0] == "neon_glow_title"


def _make_window(qapp, tmp_path: Path, scene: TeachingSceneDocument | None = None):
    from src.features.teaching_animation.ui.motion_graphic_editor_window import MotionGraphicEditorWindow
    import os
    old = os.environ.get("TOPOS_MG_PRESET_ROOT")
    os.environ["TOPOS_MG_PRESET_ROOT"] = str(tmp_path / "user_lib")
    try:
        window = MotionGraphicEditorWindow(scene if scene is not None else _host_scene())
    finally:
        if old is None:
            os.environ.pop("TOPOS_MG_PRESET_ROOT", None)
        else:
            os.environ["TOPOS_MG_PRESET_ROOT"] = old
    return window


def test_window_inserts_preset_as_one_undo_step(qapp, tmp_path: Path):
    window = _make_window(qapp, tmp_path)
    try:
        assert window._panel_tabs.tabText(4) == "Library"
        assert window.asset_library.grid.count() >= 12
        window._instantiate_preset_at("animated_histogram", 960.0, 540.0)
        scene = window.editor_controller.scene
        inserted = [e for e in scene.elements if e.element_id.startswith("bar_")]
        assert len(inserted) == 7
        assert window._selected_elements == {e.element_id for e in scene.elements}
        assert len(window.editor_controller._stack._undo_stack) == 1
        window.editor_controller.undo()
        assert window.editor_controller.scene.elements == ()
        assert window.editor_controller.scene.metadata == {}
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_window_insert_extends_short_scene_duration(qapp, tmp_path: Path):
    short = TeachingSceneDocument.from_dict({
        "scene_id": "short", "name": "Short", "width": 1920, "height": 1080,
        "fps_num": 24, "fps_den": 1, "duration_frames": 10,
        "background": {"color": "#111827"}, "elements": [], "animations": []})
    window = _make_window(qapp, tmp_path, scene=short)
    try:
        window._instantiate_preset_at("floating_orbs", None, None)
        scene = window.editor_controller.scene
        assert scene.duration_frames >= 97
        assert len(scene.elements) == 18
        window.editor_controller.undo()
        assert window.editor_controller.scene.duration_frames == 10
        assert window.editor_controller.scene.elements == ()
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_window_saves_selection_as_preset(qapp, tmp_path: Path, monkeypatch):
    window = _make_window(qapp, tmp_path)
    try:
        window._instantiate_preset_at("neon_glow_title", 960.0, 540.0)
        window.editor_controller.mark_clean()
        window.editor_controller.undo()

        class _Dialog:
            def __init__(self, *args, **kwargs):
                pass

            def exec(self):
                return 1  # QDialog.Accepted

            payload = lambda self: {  # noqa: E731
                "name": "Journey Preset", "category": "text",
                "tags": ["journey"], "description": "from test"}

        monkeypatch.setattr(
            "src.features.teaching_animation.ui.motion_graphic_editor_window.PresetSaveDialog",
            _Dialog)
        # No selection -> whole scene (empty after undo) must refuse politely.
        assert window.editor_controller.scene.elements == ()
        window._save_preset_triggered()
        user_root = tmp_path / "user_lib"
        assert not any(user_root.glob("text/*/preset.json"))
        # With layers present, saving writes a complete package.
        window._instantiate_preset_at("neon_glow_title", 960.0, 540.0)
        window._save_preset_triggered()
        packages = list(user_root.glob("text/*/preset.json"))
        assert len(packages) == 1
        folder = packages[0].parent
        assert (folder / "scene.json").is_file()
        assert (folder / "preview.gif").is_file()
        record = window.asset_library.library.get(packages[0].parent.name)
        assert record is not None
        scene = load_preset_scene(record.manifest, record.folder)
        assert scene.elements
    finally:
        window.editor_controller.mark_clean()
        window.close()


# -- R-01: HTML template cards behave like presets (minus disk writes) ------

def _select_first_template(panel) -> None:
    from PyQt6.QtCore import Qt
    for row in range(panel.grid.count()):
        item = panel.grid.item(row)
        if item.data(Qt.ItemDataRole.UserRole + 1) == "html_template":
            panel.grid.setCurrentItem(item)
            return item
    raise AssertionError("no html template item listed")


def test_template_selection_disables_disk_actions_and_keeps_preview(qapp, library):
    from src.features.teaching_animation.ui.asset_library_panel import AssetLibraryPanel
    panel = AssetLibraryPanel(library=library)
    item = _select_first_template(panel)
    # The detail pane is gone; selection only governs the preset-only disk
    # actions, which stay visibly disabled for template cards.
    assert not panel._delete_button.isEnabled()
    assert not panel._export_button.isEnabled()
    assert not panel._regenerate_button.isEnabled()
    # The bundled template ships a preview.gif the live thumbnail can play
    # (UserRole + 2 carries the preview path for the viewport-culled QMovies).
    assert item.data(0x0102)


def test_template_double_click_emits_templateActivated(qapp, library):
    from src.features.teaching_animation.ui.asset_library_panel import AssetLibraryPanel
    panel = AssetLibraryPanel(library=library)
    item = _select_first_template(panel)
    template_ids: list[str] = []
    panel.templateActivated.connect(template_ids.append)
    panel._item_activated(item)
    assert template_ids == [item.data(0x0100)]


def test_template_tooltip_leaks_no_absolute_path(qapp, library):
    from src.features.teaching_animation.ui.asset_library_panel import AssetLibraryPanel
    panel = AssetLibraryPanel(library=library)
    item = _select_first_template(panel)
    tooltip = item.toolTip()
    assert str(panel.catalog.get(item.data(0x0100)).preview_path) not in tooltip
    assert "/Users/" not in tooltip and "/home/" not in tooltip


def test_all_categories_default_selection_is_actionable(qapp, library):
    from src.features.teaching_animation.ui.asset_library_panel import AssetLibraryPanel
    panel = AssetLibraryPanel(library=library)
    # Row 0 is the first listed card (templates come first); every card kind
    # is insertable via drag/double-click, so the default selection exists
    # and preset-only disk actions turn on once a preset card is selected.
    assert panel.grid.currentRow() == 0
    assert not panel._delete_button.isEnabled()
    panel.select_preset("neon_glow_title")
    assert panel._delete_button.isEnabled()
    assert panel._export_button.isEnabled()
    assert panel._regenerate_button.isEnabled()


# -- live thumbnails, zoom slider, collapsed search ---------------------------

@pytest.fixture()
def clean_thumb_settings():
    from PyQt6.QtCore import QSettings
    settings = QSettings("ToposColor", "MGLibraryPanel")
    settings.remove("thumbnail_columns")
    settings.remove("thumbnail_width")  # legacy key from the pixel-size era
    yield
    settings.remove("thumbnail_columns")


def _process_ui(qapp, rounds: int = 6) -> None:
    import time
    for _ in range(rounds):
        qapp.processEvents()
        time.sleep(0.01)


def test_panel_thumbnail_movies_play_only_in_viewport(qapp, library):
    from PyQt6.QtGui import QMovie
    from src.features.teaching_animation.ui.asset_library_panel import AssetLibraryPanel
    panel = AssetLibraryPanel(library=library)
    panel.resize(420, 260)
    panel.show()
    _process_ui(qapp)
    try:
        # Small viewport over a 15-item grid: only on-screen cards animate.
        assert 0 < len(panel._thumb_movies) < panel.grid.count()
        assert all(movie.state() == QMovie.MovieState.Running
                   for movie in panel._thumb_movies.values())
        panel.grid.scrollToBottom()
        _process_ui(qapp)
        assert 0 < len(panel._thumb_movies) < panel.grid.count()
        panel.hide()
        _process_ui(qapp)
        assert panel._thumb_movies == {}
    finally:
        panel.hide()
        panel.deleteLater()


def test_panel_zoom_tiles_columns_without_side_margins(qapp, library, clean_thumb_settings):
    from src.features.teaching_animation.ui.asset_library_panel import (
        _CARD_GAP, AssetLibraryPanel)
    panel = AssetLibraryPanel(library=library)
    panel.resize(500, 420)
    panel.show()
    _process_ui(qapp)
    try:
        def row_zero_cell(columns: int) -> int:
            panel._zoom_slider.setValue(columns)
            viewport_width = panel.grid.viewport().width()
            cell = panel.grid.gridSize().width()
            # Cards tile the full width: the row really holds `columns`
            # cards and the right edge is flush to within the 2px wrap
            # slack plus the flooring remainder — no blank strip.
            assert viewport_width - cell * columns < 2 + columns
            in_row = 0
            for row in range(panel.grid.count()):
                rect = panel.grid.visualItemRect(panel.grid.item(row))
                if rect.y() != 0:
                    break
                in_row += 1
            assert in_row == columns
            return cell

        one, two, eight = row_zero_cell(1), row_zero_cell(2), row_zero_cell(8)
        # Every position is a visibly different packing — no dead zone.
        assert one > two > eight
        # 16:9 cards with a visible gap: the icon is inset inside its pitch
        # so adjacent cards never touch, while rows still end flush.
        icon = panel.grid.iconSize()
        assert icon.height() == round(icon.width() * 9 / 16)
        assert panel.grid.gridSize().width() - icon.width() == _CARD_GAP
        # Caption font 10px with a metrics-derived caption strip.
        assert panel.grid.font().pixelSize() == 10
        assert panel.grid.gridSize().height() - icon.height() >= 18
        # The column preference survives into the next panel via QSettings.
        panel._zoom_slider.setValue(4)
        panel._persist_thumbnail_zoom()
        again = AssetLibraryPanel(library=library)
        assert again._zoom_slider.value() == 4
    finally:
        panel.hide()
        panel.deleteLater()


def test_panel_search_collapsed_by_default_and_expands(qapp, library):
    from src.features.teaching_animation.ui.asset_library_panel import AssetLibraryPanel
    panel = AssetLibraryPanel(library=library)
    total = panel.grid.count()
    assert panel._search_row.isHidden()
    panel._search_toggle.setChecked(True)
    assert not panel._search_row.isHidden()
    panel._search_edit.setText("饼")
    assert panel.grid.count() == 1
    panel._search_toggle.setChecked(False)
    assert panel._search_row.isHidden()
    # Collapsing drops the filter — results are never silently hidden.
    assert panel._search_edit.text() == ""
    assert panel.grid.count() == total


# -- timeline adaptation: fps conversion + page alignment ---------------------

def test_prepare_insert_converts_preset_fps():
    from src.features.teaching_animation.presets.builders import (
        key, scene_document, shape_element, track)
    preset = scene_document(
        (shape_element("dot", 100.0, 100.0, 50.0, 50.0,
                       tracks=(track("opacity", key(6, 0.0), key(18, 1.0)),)),),
        name="fps probe", fps=12)
    host = _host_scene()
    elements, _parents = prepare_insert(preset, host, target_x=400.0, target_y=300.0)
    frames = [keyframe.frame
              for track_item in elements[0].local_animation_tracks
              for keyframe in track_item.keyframes]
    # 0.5 s / 1.5 s at 24 fps — wall-clock timing survives the graft.
    assert frames == [12, 36]


def test_window_insert_aligns_keyframes_to_active_page(qapp, tmp_path):
    two_pages = TeachingSceneDocument.from_dict({
        "scene_id": "pages", "name": "Pages", "width": 1920, "height": 1080,
        "fps_num": 24, "fps_den": 1, "duration_frames": 96,
        "background": {"color": "#111827"}, "elements": [], "animations": [],
        "pages": [
            {"page_id": "page_1", "duration_frames": 48},
            {"page_id": "page_2", "duration_frames": 48},
        ]})
    window = _make_window(qapp, tmp_path, scene=two_pages)
    try:
        # Playhead on page 2 → the fragment must animate inside page 2's
        # span instead of playing during page 1 (the pre-fix behaviour).
        window.editor_controller.set_frame(60)
        window._instantiate_preset_at("neon_glow_title", 960.0, 540.0)
        scene = window.editor_controller.scene
        assert scene.elements
        for element in scene.elements:
            assert element.page_id == "page_2"
            for track_item in element.local_animation_tracks:
                for keyframe in track_item.keyframes:
                    assert 48 <= keyframe.frame < scene.duration_frames
        # neon_glow_title's longest track ends at frame 72 → page 2 grows to
        # 73 frames (48 + 73 = 121 total) inside the same single undo step.
        assert scene.page_by_id("page_2").duration_frames == 73
        assert scene.duration_frames == 121
        assert len(window.editor_controller._stack._undo_stack) == 1
        window.editor_controller.undo()
        assert window.editor_controller.scene.duration_frames == 96
        assert window.editor_controller.scene.elements == ()
    finally:
        window.editor_controller.mark_clean()
        window.close()
