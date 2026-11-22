"""Image documents keep their canonical save lifecycle without Edit widgets."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
import zipfile

import pytest
from PyQt6.QtCore import QTimer

from src.app.controllers import edit_controller, file_controller
from src.app.controllers.edit_controller import EditControllerMixin
from src.app.controllers.file_controller import FileOpsMixin
from src.app.controllers.page_controller import PageManagementMixin
from src.features.color.core.color_session import ColorSessionManager
from src.features.color.core.layer import Layer
from src.features.edit.core.edit_project_io import EditProjectIO
from src.features.edit.core.project import EditProject
from src.shared.project.document_mode import infer_project_mode
from src.shared.project.save_router import SaveRouter


class _Config:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {"work_mode": "image"}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.values[key] = value

    def save_config(self) -> None:
        pass


class _ImageHost(FileOpsMixin, EditControllerMixin):
    _create_standalone_image_color_session = PageManagementMixin._create_standalone_image_color_session

    def __init__(self) -> None:
        self._current_edit_project = None
        self._current_edit_project_filepath = None
        self._current_timeline_sequences = {}
        self.media_pool_panel = None
        self.timeline_panel = None
        self.edit_page = None
        self.current_page = "Color"
        self.current_file_path = None
        self.current_file_is_video = False
        self._edit_available = False
        self._color_available = True
        self.config_manager = _Config()
        self._color_session_manager = ColorSessionManager()
        self.save_router = SaveRouter()
        self.edit_project_io = EditProjectIO()
        self.status_bar = SimpleNamespace(showMessage=Mock())
        self.engine = SimpleNamespace(layers=[Layer("Base")], active_layer_index=0,
                                      current_raw_params={})
        self.titles: list[str] = []
        self.recent: list[str] = []
        self.edit_project_dirty = False
        self._setup_ai_editing = Mock()
        self._teardown_ai_editing = Mock()
        self._sync_mg_preset_project_root = Mock()
        self.save_router.set_callbacks({
            "get_current_page": lambda: self.current_page,
            "get_current_file_path": lambda: self.current_file_path,
            "get_current_edit_project": lambda: self._current_edit_project,
            "get_edit_project_filepath": lambda: self._current_edit_project_filepath,
            "get_current_timeline_sequences": self._get_timeline_sequences,
            "get_engine": lambda: self.engine,
            "get_edit_project_io": lambda: self.edit_project_io,
            "get_current_color_session": self._color_session_manager.get_current_session,
            "get_work_mode": self._get_work_mode,
            "get_ui_state": self._get_edit_ui_state,
            "on_edit_project_updated": self._on_edit_project_updated_from_save_router,
        })

    def setWindowTitle(self, title: str) -> None:
        self.titles.append(title)

    def _add_to_recent_projects(self, path: str) -> None:
        self.recent.append(path)

    def unload_video(self, *, save_before_unload: bool = True) -> None:
        self.current_file_path = None
        self._color_session_manager.clear_current_session()

    def load_image(self, path: str, *, project_path: str | None = None) -> None:
        """Keep rendering outside this lifecycle test; read real saved layers."""
        self.current_file_path = path
        assert self._ensure_image_project(path, project_path)
        self._create_standalone_image_color_session(path, project_path=project_path)
        if project_path:
            data = self.save_router.load_single_asset_data(project_path)
            assert data is not None
            self.engine.layers = data["layers"]


@pytest.fixture()
def image_host(qapp, monkeypatch):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
    host = _ImageHost()
    yield host
    host.edit_project_io.cleanup()


def test_create_canonical_image_document_without_edit_widgets(image_host, tmp_path: Path) -> None:
    source = str(tmp_path / "照片.png")
    assert image_host._ensure_image_project(source)
    project = image_host._current_edit_project
    assert not project.timelines and len(project.assets) == 1
    asset = project.get_asset_by_path(source)
    stack = project.get_color_stack_for_asset(asset.asset_id)
    assert stack.source_kind == "standalone_image"
    image_host._create_standalone_image_color_session(source)
    session = image_host._color_session_manager.get_current_session()
    assert session.project_id == project.project_id and session.color_stack_id == stack.stack_id
    ui = image_host._get_edit_ui_state()
    assert ui.playhead_frame == 0 and ui.selected_clip_ids == [] and ui.expanded_bins == []


def test_image_sidecars_keep_same_stem_photos_and_their_grades_separate(image_host, tmp_path: Path) -> None:
    first = str(tmp_path / "photo.jpg")
    second = str(tmp_path / "photo.dng")
    for path in (first, second):
        Path(path).write_bytes(b"test source")

    image_host.load_image(first)
    image_host.engine.layers[0].grading_params["exposure"] = 4.5
    assert image_host.save_router.save()
    first_package = str(tmp_path / "photo.topos")
    assert image_host.save_router._get_project_path_for_file(first) == first_package
    assert image_host.save_router._get_project_path_for_file(second) == second + ".topos"

    image_host._current_edit_project = None
    image_host.save_router.clear_cache()
    image_host.load_image(second)
    image_host.engine.layers[0].grading_params["exposure"] = -3.0
    assert image_host.save_router.save()
    assert image_host.save_router._get_project_path_for_file(second) == second + ".topos"
    first_data = image_host.save_router.load_single_asset_data(first_package)
    second_data = image_host.save_router.load_single_asset_data(second + ".topos")
    assert first_data["layers"][0].grading_params["exposure"] == 4.5
    assert second_data["layers"][0].grading_params["exposure"] == -3.0


def test_optional_panels_still_receive_image_project(image_host, tmp_path: Path) -> None:
    image_host.media_pool_panel = SimpleNamespace(set_project=Mock())
    image_host.timeline_panel = SimpleNamespace(set_project=Mock())
    assert image_host._ensure_image_project(str(tmp_path / "photo.png"))
    image_host.media_pool_panel.set_project.assert_called_once_with(image_host._current_edit_project)
    image_host.timeline_panel.set_project.assert_called_once_with(image_host._current_edit_project)


def test_replacing_image_cancel_preserves_current_document(image_host, tmp_path: Path) -> None:
    first = str(tmp_path / "first.png")
    assert image_host._ensure_image_project(first)
    original = image_host._current_edit_project
    image_host._close_current_project = Mock(return_value=False)
    assert image_host._ensure_image_project(str(tmp_path / "second.png")) is False
    assert image_host._current_edit_project is original
    image_host._close_current_project.assert_called_once_with()


def test_cancelling_grade_set_switch_leaves_live_photo_untouched(
    image_host, tmp_path: Path, monkeypatch,
) -> None:
    first = str(tmp_path / "first.png")
    image_host.load_image(first)
    image_host.grade_set_panel = Mock()
    image_host.unload_video = Mock()
    image_host.save_router.save = Mock()
    image_host._autosave_router_timer = QTimer()
    image_host._autosave_router_timer.setSingleShot(True)
    image_host._autosave_router_timer.start(1000)
    original = image_host._current_edit_project

    def question(*args: object) -> edit_controller.QMessageBox.StandardButton:
        assert not image_host._autosave_router_timer.isActive()
        return edit_controller.QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(edit_controller.QMessageBox, "question", question)
    FileOpsMixin.load_image(image_host, str(tmp_path / "second.png"))
    assert image_host._autosave_router_timer.isActive()
    image_host._autosave_router_timer.stop()
    assert image_host._current_edit_project is original
    assert image_host.current_file_path == first
    image_host.save_router.save.assert_not_called()
    image_host.grade_set_panel.invalidate_graded_thumbnail.assert_not_called()
    image_host.unload_video.assert_not_called()


def test_discarding_grade_set_photo_expires_preview_without_saving(
    image_host, tmp_path: Path, monkeypatch,
) -> None:
    first = str(tmp_path / "first.png")
    image_host.load_image(first)
    image_host.grade_set_panel = Mock()
    image_host.save_router.save = Mock()
    image_host._autosave_router_timer = QTimer()
    image_host._autosave_router_timer.setSingleShot(True)
    image_host._autosave_router_timer.start(1000)
    monkeypatch.setattr(edit_controller.QMessageBox, "question",
                        lambda *args: edit_controller.QMessageBox.StandardButton.Discard)

    assert image_host._close_current_project()
    assert not image_host._autosave_router_timer.isActive()
    image_host.save_router.save.assert_not_called()
    image_host.grade_set_panel.invalidate_graded_thumbnail.assert_called_once_with(first)
    image_host.grade_set_panel.set_active_path.assert_called_once_with(None)


def test_standalone_photo_retains_history_and_resources_until_archive_worker_finishes(
    image_host, tmp_path: Path, monkeypatch,
) -> None:
    from src.shared.color import lut_resource_registry
    from src.shared.project import resource_resolver

    image_host.load_image(str(tmp_path / "photo.png"))
    image_host.engine.history = Mock()
    resolver = Mock()
    resources = Mock()
    monkeypatch.setattr(resource_resolver, "set_active_project_resolver", resolver)
    monkeypatch.setattr(lut_resource_registry, "get_lut_resource_session", lambda: resources)
    question = Mock(return_value=edit_controller.QMessageBox.StandardButton.Discard)
    monkeypatch.setattr(edit_controller.QMessageBox, "question", question)
    worker = object()
    image_host.edit_project_io._manual_save_workers.add(worker)
    assert not image_host._close_current_project()
    image_host.edit_project_io._manual_save_workers.remove(worker)
    question.assert_not_called()
    image_host.engine.history.clear.assert_not_called()
    resolver.assert_not_called()
    resources.reset.assert_not_called()

    assert image_host._close_current_project()
    image_host.engine.history.clear.assert_called_once_with()
    resolver.assert_called_once_with(None)
    resources.reset.assert_called_once_with()


def test_color_product_accepts_images_but_rejects_timeline_projects(image_host, monkeypatch) -> None:
    notices = []
    monkeypatch.setattr(file_controller.QMessageBox, "information", lambda *args: notices.append(args))
    assert image_host._get_work_mode() == "video"  # Non-studio preset default.
    image_host._read_project_mode_from_manifest = lambda _path: "standalone_image"
    assert image_host._check_project_matches_work_mode("photo.topos")
    image_host._read_project_mode_from_manifest = lambda _path: "edit_project"
    assert not image_host._check_project_matches_work_mode("movie.topos")
    assert len(notices) == 1


@pytest.mark.parametrize("work_mode, project_mode, allowed", [
    ("image", "standalone_image", True), ("image", "edit_project", False),
    ("video", "standalone_image", False), ("video", "edit_project", True),
])
def test_studio_keeps_workflow_gate(image_host, monkeypatch, work_mode: str,
                                  project_mode: str, allowed: bool) -> None:
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    monkeypatch.setattr(file_controller.QMessageBox, "information", lambda *args: None)
    image_host.config_manager.set("work_mode", work_mode)
    image_host._read_project_mode_from_manifest = lambda _path: project_mode
    assert image_host._check_project_matches_work_mode("project.topos") is allowed


def test_regular_save_reopen_preserves_image_type_layers_and_unknown_fields(
    image_host, tmp_path: Path,
) -> None:
    source = tmp_path / "photo.png"
    source.write_bytes(b"source placeholder")
    target = str(tmp_path / "grade.topos")
    image_host.load_image(str(source))
    project = image_host._current_edit_project
    stack_id = image_host._color_session_manager.get_current_color_stack_id()
    stack = replace(project.color_stacks[stack_id], _extra_fields={"future_stack": {"keep": 1}})
    asset_id, asset = next(iter(project.assets.items()))
    asset = replace(asset, _extra_fields={"future_asset": {"keep": 2}})
    image_host._current_edit_project = replace(
        project, color_stacks={stack_id: stack}, assets={asset_id: asset})
    layer = image_host.engine.layers[0]
    layer.grading_params["exposure"] = 1.25
    image_host.engine.layers.append(Layer("Second"))
    image_host.engine.layers[1].grading_params["contrast"] = 0.2
    image_host._current_edit_project_filepath = target
    image_host.save_router.set_project_path(target)

    assert image_host.save_router.save()
    with zipfile.ZipFile(target) as archive:
        assert json.loads(archive.read("manifest.json"))["project_mode"] == "standalone_image"
    image_host._clear_all_dirty_states()
    assert image_host._close_current_project()
    assert image_host._current_edit_project is None
    image_host._load_standalone_project(target)
    restored = image_host._current_edit_project
    assert restored.assets[asset_id]._extra_fields["future_asset"] == {"keep": 2}
    assert restored.color_stacks[stack_id]._extra_fields["future_stack"] == {"keep": 1}
    assert len(image_host.engine.layers) == 2
    assert image_host.engine.layers[0].grading_params["exposure"] == 1.25
    assert image_host.engine.layers[1].grading_params["contrast"] == 0.2
    assert image_host._color_session_manager.get_current_color_stack_id() == stack_id


def test_save_as_uses_image_document_type_without_media_pool(image_host, tmp_path: Path,
                                                          monkeypatch, qtbot) -> None:
    source = str(tmp_path / "photo.png")
    image_host.load_image(source)
    stack_id = image_host._color_session_manager.get_current_color_stack_id()
    assert image_host._current_edit_project.color_stacks[stack_id].grading_params["exposure"] == 0
    image_host.engine.layers[0].grading_params["exposure"] = 1.75
    image_host.engine.layers.append(Layer("Extra"))
    target = str(tmp_path / "copy.topos")
    monkeypatch.setattr(edit_controller.QFileDialog, "getSaveFileName", lambda *args: (target, ""))
    image_host._save_current_project_as()
    assert image_host._current_edit_project_filepath is None
    assert image_host.edit_project_dirty
    qtbot.waitUntil(lambda: image_host._current_edit_project_filepath == target, timeout=10000)
    with zipfile.ZipFile(target) as archive:
        assert json.loads(archive.read("manifest.json"))["project_mode"] == "standalone_image"
    loaded, _ui, _sequences = image_host.edit_project_io.load_project(target)
    assert loaded.color_stacks[stack_id].grading_params["exposure"] == 1.75
    assert len(loaded.color_stacks[stack_id].layers) == 2
    assert not image_host.edit_project_dirty


def test_idle_grade_save_adopts_sidecar_only_after_archive_commit(image_host, tmp_path: Path,
                                                          qtbot) -> None:
    source = tmp_path / "photo.png"
    source.write_bytes(b"original photo")
    image_host.grade_set_model = object()
    image_host.library_panel = SimpleNamespace(invalidate_image_grade_badge=Mock())
    image_host.load_image(str(source))
    stack_id = image_host._color_session_manager.get_current_color_stack_id()
    image_host.engine.layers[0].grading_params["exposure"] = 1.35
    target = source.with_suffix(".topos")

    image_host._perform_debounced_save()
    assert image_host._current_edit_project_filepath is None
    assert image_host.edit_project_dirty
    qtbot.waitUntil(lambda: image_host._current_edit_project_filepath == str(target), timeout=10000)

    assert not image_host.edit_project_dirty
    assert source.read_bytes() == b"original photo"
    image_host.library_panel.invalidate_image_grade_badge.assert_called_once_with(str(source))
    loaded, _, _ = image_host.edit_project_io.load_project(str(target))
    assert loaded.color_stacks[stack_id].grading_params["exposure"] == 1.35


@pytest.mark.parametrize("discard", [True, False])
def test_app_quit_prompt_pauses_idle_photo_save(image_host, tmp_path: Path,
                                               monkeypatch, discard: bool) -> None:
    source = tmp_path / "photo.png"
    image_host.grade_set_model = object()
    image_host.load_image(str(source))
    image_host._autosave_router_timer = QTimer()
    image_host._autosave_router_timer.setSingleShot(True)
    image_host._autosave_router_timer.start(1000)
    image_host._save_image_project_as = Mock()

    def answer(*args: object) -> file_controller.QMessageBox.StandardButton:
        assert not image_host._autosave_router_timer.isActive()
        image_host._perform_debounced_save()
        if discard:
            return file_controller.QMessageBox.StandardButton.Discard
        return file_controller.QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(file_controller.QMessageBox, "question", answer)
    assert image_host._confirm_save_before_close() is discard
    image_host._save_image_project_as.assert_not_called()
    assert image_host._autosave_router_timer.isActive() is not discard
    image_host._autosave_router_timer.stop()


def test_failed_image_save_as_keeps_path_dirty_and_model(image_host, tmp_path: Path,
                                                       monkeypatch, qtbot) -> None:
    image_host.load_image(str(tmp_path / "photo.png"))
    original = image_host._current_edit_project
    image_host.engine.layers[0].grading_params["exposure"] = 2.0
    target = str(tmp_path / "failed.topos")
    monkeypatch.setattr(edit_controller.QFileDialog, "getSaveFileName", lambda *args: (target, ""))
    monkeypatch.setattr(image_host.edit_project_io, "save_project", lambda *args, **kwargs: False)
    image_host._save_current_project_as()
    qtbot.waitUntil(lambda: not image_host.edit_project_io._manual_save_workers, timeout=10000)
    assert image_host._current_edit_project_filepath is None
    assert image_host._current_edit_project is original
    assert image_host.edit_project_dirty
    assert not Path(target).exists()


def test_cancelled_image_save_as_does_not_capture_or_write(image_host, tmp_path: Path,
                                                        monkeypatch) -> None:
    image_host.load_image(str(tmp_path / "photo.png"))
    original = image_host._current_edit_project
    monkeypatch.setattr(edit_controller.QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    image_host._save_current_project_as()
    assert image_host._current_edit_project_filepath is None
    assert image_host._current_edit_project is original
    assert image_host.edit_project_dirty
    assert not image_host.edit_project_io._manual_save_workers


@pytest.mark.parametrize("change_kind", ["grade", "project"])
def test_editing_during_save_as_stays_live_and_dirty(image_host, tmp_path: Path,
                                                   monkeypatch, qtbot, change_kind: str) -> None:
    image_host.load_image(str(tmp_path / "photo.png"))
    image_host.engine.layers[0].grading_params["exposure"] = 1.0
    target = str(tmp_path / "snapshot.topos")
    original_save = image_host.edit_project_io.save_project
    entered = threading.Event()
    gate = threading.Event()

    def slow_save(*args: Any, **kwargs: Any) -> bool:
        entered.set()
        assert gate.wait(5)
        return original_save(*args, **kwargs)

    monkeypatch.setattr(image_host.edit_project_io, "save_project", slow_save)
    monkeypatch.setattr(edit_controller.QFileDialog, "getSaveFileName", lambda *args: (target, ""))
    try:
        image_host._save_current_project_as()
        qtbot.waitUntil(entered.is_set, timeout=5000)
        if change_kind == "grade":
            image_host.engine.layers[0].grading_params["exposure"] = 3.0
        else:
            image_host._current_edit_project = replace(image_host._current_edit_project, name="Renamed")
        gate.set()
        qtbot.waitUntil(lambda: image_host._current_edit_project_filepath == target, timeout=10000)
        expected_exposure = 3.0 if change_kind == "grade" else 1.0
        assert image_host.engine.layers[0].grading_params["exposure"] == expected_exposure
        assert image_host.edit_project_dirty
        stack_id = image_host._color_session_manager.get_current_color_stack_id()
        assert image_host._current_edit_project.color_stacks[stack_id].grading_params["exposure"] == expected_exposure
        if change_kind == "project":
            assert image_host._current_edit_project.name == "Renamed"
        loaded, _ui, _sequences = image_host.edit_project_io.load_project(target)
        assert loaded.color_stacks[stack_id].grading_params["exposure"] == 1.0
        assert loaded.name != "Renamed"
    finally:
        gate.set()


def test_close_cancel_and_save_failure_keep_image_project(image_host, tmp_path: Path,
                                                       monkeypatch) -> None:
    image_host.load_image(str(tmp_path / "photo.png"))
    original = image_host._current_edit_project
    monkeypatch.setattr(edit_controller.QMessageBox, "question",
                        lambda *args: edit_controller.QMessageBox.StandardButton.Cancel)
    assert image_host._close_current_project() is False
    assert image_host._current_edit_project is original
    monkeypatch.setattr(edit_controller.QMessageBox, "question",
                        lambda *args: edit_controller.QMessageBox.StandardButton.Save)
    image_host._save_current_project = Mock()  # A failed/cancelled save leaves dirty state.
    assert image_host._close_current_project() is False
    assert image_host._current_edit_project is original


@pytest.mark.parametrize("operation", ["save", "close", "replace", "deliver"])
def test_existing_image_save_captures_live_grade_before_leaving(
    image_host, tmp_path: Path, monkeypatch, operation: str,
) -> None:
    source = str(tmp_path / "photo.png")
    target = str(tmp_path / "existing.topos")
    image_host.load_image(source)
    stack_id = image_host._color_session_manager.get_current_color_stack_id()
    image_host._current_edit_project_filepath = target
    image_host.save_router.set_project_path(target)
    assert image_host.save_router.save()
    image_host._clear_all_dirty_states()
    assert image_host._current_edit_project.color_stacks[stack_id].grading_params["exposure"] == 0
    image_host.engine.layers[0].grading_params["exposure"] = 4.5
    image_host.edit_project_dirty = True
    monkeypatch.setattr(edit_controller.QMessageBox, "question",
                        lambda *args: edit_controller.QMessageBox.StandardButton.Save)
    if operation == "save":
        image_host._save_current_project()
        assert image_host.current_page == "Color"
        assert not image_host.edit_project_dirty
    elif operation == "deliver":
        image_host.current_page = "Deliver"
        assert image_host.save_router.save()
    elif operation == "close":
        assert image_host._close_current_project()
        assert image_host._current_edit_project is None
    else:
        next_source = str(tmp_path / "next.png")
        assert image_host._ensure_image_project(next_source)
        assert image_host._current_edit_project.get_asset_by_path(next_source) is not None
    loaded, _ui, _sequences = image_host.edit_project_io.load_project(target)
    assert loaded.color_stacks[stack_id].grading_params["exposure"] == 4.5
    assert loaded.color_stacks[stack_id].layers[0]["grading_params"]["exposure"] == 4.5


def test_existing_image_close_save_failure_keeps_dirty_and_archive(
    image_host, tmp_path: Path, monkeypatch,
) -> None:
    source = str(tmp_path / "photo.png")
    target = str(tmp_path / "existing.topos")
    image_host.load_image(source)
    project_id = image_host._current_edit_project.project_id
    stack_id = image_host._color_session_manager.get_current_color_stack_id()
    image_host._current_edit_project_filepath = target
    image_host.save_router.set_project_path(target)
    assert image_host.save_router.save()
    image_host._clear_all_dirty_states()
    image_host.engine.layers[0].grading_params["exposure"] = 4.5
    image_host.edit_project_dirty = True
    monkeypatch.setattr(edit_controller.QMessageBox, "question",
                        lambda *args: edit_controller.QMessageBox.StandardButton.Save)
    monkeypatch.setattr(edit_controller.QMessageBox, "critical", lambda *args: None)
    monkeypatch.setattr(image_host.edit_project_io, "save_project", lambda *args, **kwargs: False)
    assert image_host._close_current_project() is False
    assert image_host._current_edit_project.project_id == project_id
    assert image_host._has_unsaved_changes()
    assert image_host.current_page == "Color"
    loaded, _ui, _sequences = image_host.edit_project_io.load_project(target)
    assert loaded.color_stacks[stack_id].grading_params["exposure"] == 0


@pytest.mark.parametrize("cancel_dialog", [False, True])
def test_close_unsaved_image_waits_for_async_save_and_never_closes_on_failure(
    image_host, tmp_path: Path, monkeypatch, qtbot, cancel_dialog: bool,
) -> None:
    image_host.load_image(str(tmp_path / "photo.png"))
    project_id = image_host._current_edit_project.project_id
    target = str(tmp_path / "failed.topos")
    monkeypatch.setattr(edit_controller.QMessageBox, "question",
                        lambda *args: edit_controller.QMessageBox.StandardButton.Save)
    monkeypatch.setattr(edit_controller.QFileDialog, "getSaveFileName",
                        lambda *args: ("" if cancel_dialog else target, ""))
    monkeypatch.setattr(image_host.edit_project_io, "save_project", lambda *args, **kwargs: False)
    assert image_host._close_current_project() is False
    qtbot.waitUntil(lambda: not image_host.edit_project_io._manual_save_workers, timeout=10000)
    assert image_host._current_edit_project.project_id == project_id
    assert image_host._current_edit_project_filepath is None
    assert image_host._has_unsaved_changes()
    assert not Path(target).exists()


def test_failed_reopen_keeps_existing_image_document(image_host, tmp_path: Path,
                                                 monkeypatch) -> None:
    image_host.load_image(str(tmp_path / "photo.png"))
    original = image_host._current_edit_project
    monkeypatch.setattr(file_controller.QMessageBox, "critical", lambda *args: None)
    image_host._load_standalone_project(str(tmp_path / "missing.topos"))
    assert image_host._current_edit_project is original


def test_document_mode_keeps_empty_and_import_only_video_documents(tmp_path: Path) -> None:
    empty = EditProject.create("Video")
    imported = empty.import_asset(str(tmp_path / "still.png"), "image", {})
    assert infer_project_mode(empty, work_mode="video") == "edit_project"
    assert infer_project_mode(imported, work_mode="video") == "edit_project"
    canonical = EditProject.create_image_project("Image", str(tmp_path / "still.png"))
    assert infer_project_mode(canonical, work_mode="video") == "standalone_image"
    stack_id, stack = next(iter(canonical.color_stacks.items()))
    legacy = replace(canonical, color_stacks={stack_id: replace(stack, source_kind="timeline_clip")})
    assert infer_project_mode(legacy, work_mode="video") == "standalone_image"
    with_timeline = replace(canonical, timelines={"timeline": object()})
    assert infer_project_mode(with_timeline, work_mode="image") == "edit_project"


@pytest.mark.parametrize("product, work_mode, expect_image", [
    ("studio", "image", True), ("color", "video", True), ("studio", "video", False),
])
def test_dng_prefers_still_only_in_image_workflow(tmp_path: Path, monkeypatch, product: str,
                                               work_mode: str, expect_image: bool) -> None:
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", product)
    path = str(tmp_path / "raw.dng")
    host = SimpleNamespace(
        current_page="Color", _get_work_mode=lambda: work_mode,
        save_router=SaveRouter(), load_image=Mock(), load_video=Mock(),
        _reject_standalone_video_for_color=Mock(),
    )
    FileOpsMixin._open_media_path(host, path)
    if expect_image:
        host.load_image.assert_called_once_with(path)
        host._reject_standalone_video_for_color.assert_not_called()
    else:
        host.load_image.assert_not_called()
        host._reject_standalone_video_for_color.assert_called_once_with(path)


def test_media_entry_routes_project_before_media_preview(tmp_path: Path) -> None:
    path = str(tmp_path / "grade.TOPOS")
    host = SimpleNamespace(_open_project_path=Mock(), load_image=Mock(), load_video=Mock())
    FileOpsMixin._open_media_path(host, path)
    host._open_project_path.assert_called_once_with(path)
    host.load_image.assert_not_called()
    host.load_video.assert_not_called()


def test_missing_project_from_media_entry_does_not_load_as_image(image_host, tmp_path: Path) -> None:
    image_host.load_image = Mock()
    image_host._open_media_path(str(tmp_path / "missing.topos"))
    assert image_host._current_edit_project is None
    image_host.load_image.assert_not_called()
