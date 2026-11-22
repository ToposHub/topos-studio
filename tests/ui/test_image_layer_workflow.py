"""Still-image mode exposes real layers and accepts desktop files at the viewer."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QMainWindow

from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.app.controllers.layer_controller import LayerOpsMixin
from src.features.color.core.layer import Layer
from src.features.color.ui.canvas import Canvas
from src.shared.feature_flags import uses_image_layer_workflow


@pytest.mark.parametrize("product,mode,expected", [
    ("studio", "image", True), ("studio", "video", False),
    ("color", "video", True), ("edit", "image", False),
])
def test_layer_workflow_is_specific_to_image_or_color_product(monkeypatch, product, mode, expected):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", product)
    assert uses_image_layer_workflow(SimpleNamespace(get=lambda key: mode)) is expected


class _LayerMenuHost(QMainWindow, MenuToolbarMixin):
    def __init__(self) -> None:
        super().__init__()
        self.config_manager = SimpleNamespace(get=lambda key: "image")
        self.engine = SimpleNamespace(
            img_full_original=np.zeros((2, 2, 3), dtype=np.float32),
            layers=[Layer("Base"), Layer("Grade")], active_layer_index=1)
        self.engine.get_active_layer = lambda: self.engine.layers[self.engine.active_layer_index]
        self.calls = []
        for name in ("on_layer_added", "on_layer_removed", "on_layer_reset", "on_layer_copy",
                     "on_layer_paste", "on_layer_duplicate", "rename_current_layer",
                     "toggle_current_layer_visibility", "disable_all_layers"):
            setattr(self, name, lambda *args, name=name: self.calls.append((name, args)))


def test_image_menu_edits_layers_without_a_node_panel(qtbot, monkeypatch):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    host = _LayerMenuHost()
    qtbot.addWidget(host)
    menu = host.menuBar().addMenu("Edit")
    actions = host._create_color_node_menu(menu)
    assert host.menu_layer.objectName() == "ColorLayersMenu"
    assert host._color_node_menu_actions == {}
    actions["add_layer_act"].trigger()
    actions["del_layer_act"].trigger()
    actions["duplicate_layer_act"].trigger()
    assert host.calls == [("on_layer_added", ()), ("on_layer_removed", (1,)), ("on_layer_duplicate", ())]
    host.engine.layers.pop()
    host.engine.active_layer_index = 0
    host._refresh_image_layer_menu()
    assert not actions["del_layer_act"].isEnabled()
    host.engine.img_full_original = None
    host._refresh_image_layer_menu()
    assert not any(action.isEnabled() for action in actions.values())
    assert all(action.shortcut().isEmpty() for action in actions.values())


def test_viewer_accepts_local_image_and_project_drops_once(qtbot, tmp_path):
    canvas = Canvas()
    qtbot.addWidget(canvas)
    opened = []
    canvas.file_open_requested.connect(opened.append)
    for name in ("中文 photo.png", "raw.dng", "document.topos"):
        path = tmp_path / name
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        canvas.dragEnterEvent(enter)
        assert enter.isAccepted()
        drop = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        canvas.dropEvent(drop)
        assert drop.isAccepted()
    assert opened == [str(tmp_path / name) for name in ("中文 photo.png", "raw.dng", "document.topos")]


@pytest.mark.parametrize("action", [Qt.DropAction.CopyAction, Qt.DropAction.MoveAction])
def test_image_mode_canvas_imports_photo_batches_as_copy_only(qtbot, tmp_path, action):
    canvas = Canvas()
    qtbot.addWidget(canvas)
    canvas.grade_set_import_enabled = True
    images = [str(tmp_path / name) for name in ("portrait.jpg", "raw.dng")]
    opened, imported = [], []
    canvas.file_open_requested.connect(opened.append)
    canvas.photos_dropped.connect(imported.append)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path) for path in images])
    event = QDropEvent(QPointF(5, 5), action, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    canvas.dropEvent(event)
    if action == Qt.DropAction.CopyAction:
        assert event.isAccepted()
        assert event.dropAction() == Qt.DropAction.CopyAction
        assert imported == [images]
    else:
        assert not event.isAccepted()
        assert imported == []
    assert opened == []


@pytest.mark.parametrize("urls", [
    ["https://example.com/image.png"], ["file:///tmp/lut.cube"],
    ["file:///tmp/a.png", "file:///tmp/b.png"], [],
])
def test_viewer_does_not_turn_resource_or_remote_drops_into_media(qtbot, urls):
    canvas = Canvas()
    qtbot.addWidget(canvas)
    opened = []
    canvas.file_open_requested.connect(opened.append)
    mime = QMimeData()
    mime.setUrls([QUrl(value) for value in urls])
    event = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    canvas.dropEvent(event)
    assert not event.isAccepted() and not opened


def test_layer_properties_use_history_and_autosave():
    layer = Layer("Grade")
    host = SimpleNamespace(
        engine=SimpleNamespace(get_active_layer=lambda: layer,
                               set_layer_opacity=Mock(), set_layer_blend_mode=Mock()),
        layer_panel=SimpleNamespace(opacity_slider=SimpleNamespace(isSliderDown=lambda: False)),
        _push_layer_history=Mock(), _trigger_autosave=Mock())
    LayerOpsMixin.on_layer_opacity_changed(host, .5)
    LayerOpsMixin.on_layer_blend_mode_changed(host, "Multiply")
    assert host._push_layer_history.call_count == 2
    assert host._trigger_autosave.call_count == 2
    host.layer_panel.opacity_slider.isSliderDown = lambda: True
    LayerOpsMixin.on_layer_opacity_changed(host, .3)
    assert host._push_layer_history.call_count == 2


def test_duplicate_and_paste_restore_the_immediately_previous_layers():
    from src.features.color.core.history import HistoryManager
    from src.features.color.core.image_engine import ImageEngine

    class Engine:
        copy_layer = ImageEngine.copy_layer
        can_paste_layer = ImageEngine.can_paste_layer
        paste_layer = ImageEngine.paste_layer
        duplicate_layer = ImageEngine.duplicate_layer

        def __init__(self) -> None:
            self.layers = [Layer("Grade")]
            self.layers[0].grading_params["exposure"] = 1.25
            self.active_layer_index = 0
            self.img_full_original = np.zeros((2, 2, 3), dtype=np.float32)
            self.history = HistoryManager()
            self._clipboard_layer = None
            self.render_pipeline = Mock()

        def get_active_layer(self) -> Layer:
            return self.layers[self.active_layer_index]

    engine = Engine()
    host = SimpleNamespace(engine=engine, status_bar=SimpleNamespace(showMessage=Mock()),
                           layer_panel=SimpleNamespace(update_layers=Mock()),
                           on_layer_selected=Mock(), update_from_layer=Mock(),
                           _trigger_autosave=Mock())
    host._push_layer_history = lambda: LayerOpsMixin._push_layer_history(host)
    LayerOpsMixin.on_layer_duplicate(host)
    assert len(engine.layers) == 2
    previous = engine.history.undo(engine.img_full_original, engine.layers, 1)
    assert len(previous["layers"]) == 1
    assert previous["layers"][0]["grading_params"]["exposure"] == 1.25
    engine.copy_layer(0)
    engine.layers[1].grading_params["exposure"] = 2.5
    LayerOpsMixin.on_layer_paste(host)
    assert engine.layers[1].grading_params["exposure"] == 1.25
    previous = engine.history.undo(engine.img_full_original, engine.layers, 1)
    assert previous["layers"][1]["grading_params"]["exposure"] == 2.5
    engine._clipboard_layer = None
    LayerOpsMixin.on_layer_paste(host)
    assert not engine.history.undo_stack
    assert host._trigger_autosave.call_count == 2


@pytest.mark.parametrize("operation", ["opacity", "blend"])
def test_saved_image_becomes_dirty_before_autosave_debounce(monkeypatch, operation):
    from src.app.controllers.file_controller import FileOpsMixin
    from src.features.color.core.image_engine import ImageEngine

    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    layer = Layer("Grade")
    engine = SimpleNamespace(img_full_original=np.zeros((2, 2, 3)),
                             get_active_layer=lambda: layer, render_pipeline=Mock())
    engine.set_layer_opacity = lambda value: ImageEngine.set_layer_opacity(engine, value)
    engine.set_layer_blend_mode = lambda value: ImageEngine.set_layer_blend_mode(engine, value)
    timer = Mock()
    host = SimpleNamespace(
        config_manager=SimpleNamespace(get=lambda key: "image"), engine=engine,
        _current_edit_project=object(), _current_edit_project_filepath="/tmp/graded.topos",
        current_file_path="/tmp/source.png", edit_project_dirty=False,
        layer_panel=SimpleNamespace(opacity_slider=SimpleNamespace(isSliderDown=lambda: False)),
        _push_layer_history=Mock(), _autosave_router_timer=timer)
    host._trigger_autosave = lambda: FileOpsMixin._trigger_autosave(host)
    if operation == "opacity":
        LayerOpsMixin.on_layer_opacity_changed(host, .5)
    else:
        LayerOpsMixin.on_layer_blend_mode_changed(host, "Multiply")
    assert FileOpsMixin._has_unsaved_changes(host)
    timer.start.assert_called_once_with(2000)


@pytest.mark.parametrize("mode,loaded", [("video", True), ("image", False)])
def test_autosave_does_not_mark_unloaded_or_video_image_layers_dirty(monkeypatch, mode, loaded):
    from src.app.controllers.file_controller import FileOpsMixin

    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    host = SimpleNamespace(config_manager=SimpleNamespace(get=lambda key: mode),
                           _current_edit_project=object(), _current_edit_project_filepath="/tmp/doc.topos",
                           current_file_path="/tmp/source.png", edit_project_dirty=False,
                           engine=SimpleNamespace(img_full_original=np.zeros((2, 2, 3)) if loaded else None),
                           _autosave_router_timer=Mock())
    FileOpsMixin._trigger_autosave(host)
    assert not host.edit_project_dirty


@pytest.mark.parametrize("mode", ["image", "video"])
@pytest.mark.parametrize("operation", ["layer_reset", "adjustments_reset", "reset_all", "undo", "redo"])
def test_reset_and_history_changes_mark_saved_image_dirty(monkeypatch, mode, operation):
    from src.app.controllers.file_controller import FileOpsMixin
    from src.features.color.core.history import HistoryManager

    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    layer = Layer("Grade")
    layer.grading_params["exposure"] = 30.0
    pixels = np.zeros((2, 2, 3), dtype=np.float32)
    engine = SimpleNamespace(img_full_original=pixels, layers=[layer], active_layer_index=0,
                             history=HistoryManager(), get_current_image=lambda: pixels,
                             get_active_layer=lambda: layer, discard_changes=layer.reset_params,
                             reset_all_layers=layer.reset_params, reset_flip=Mock(),
                             undo=Mock(return_value=True), redo=Mock(return_value=True))
    canvas = Mock()
    canvas.active_shape_id = None
    canvas.sam_mode_active = False
    host = SimpleNamespace(
        engine=engine, canvas=canvas,
        config_manager=SimpleNamespace(get=lambda key: mode if key == "work_mode" else False),
        _current_edit_project=object(), _current_edit_project_filepath="/tmp/grade.topos",
        current_file_path="/tmp/source.png", edit_project_dirty=False,
        _autosave_router_timer=Mock(), layer_panel=Mock(), status_bar=Mock(), raw_panel=Mock(),
        mask_panel=Mock(), update_canvas=Mock(), update_from_layer=Mock(), request_render=Mock(),
        _on_tracker_shape_selected=Mock(), _cancel_warper_gestures=Mock(),
        _edit_undo_redo_target=lambda **kwargs: None, _composite_available=False)
    host._trigger_autosave = lambda: FileOpsMixin._trigger_autosave(host)
    host._push_layer_history = lambda: LayerOpsMixin._push_layer_history(host)
    host._sync_ui_after_undo_redo = lambda: LayerOpsMixin._sync_ui_after_undo_redo(host)
    if operation == "layer_reset":
        LayerOpsMixin.on_layer_reset(host, 0)
    elif operation == "adjustments_reset":
        LayerOpsMixin.on_adjustments_reset(host)
    elif operation == "reset_all":
        LayerOpsMixin.on_reset_all_click(host)
    else:
        getattr(LayerOpsMixin, operation)(host)
    assert FileOpsMixin._has_unsaved_changes(host) is (mode == "image")
    assert host._autosave_router_timer.start.call_count == (1 if mode == "image" else 0)
