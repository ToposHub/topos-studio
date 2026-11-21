"""Exercise the native catalog, search dialog and media-pool import together."""
from pathlib import Path
from threading import Event
import wave

import pytest
from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QInputDialog, QMenu, QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from src.app.services import ai_library_analysis
from src.app.ui import files_panel
from src.app.ui.ai_library_window import RegisteredAILibraryWindow
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.native_files_transport import NativeFilesTransport
from src.features.edit.core.asset_source_range import get_effective_source_range
from src.features.edit.core.project import EditProject
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.features.files.repository.catalog import FilesCatalog
from src.features.files.repository.locator import FilesDataLocator
from src.features.files.services.ai_library import LibraryAnalysisPolicy, MediaEvidence
from src.features.files.services.facade import FilesFacade
from src.features.files.transactions import CancelToken
from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, DataPolicy, ProfileRegistry
from src.shared.i18n.i18n import I18N


def test_media_library_opens_from_menus_and_sidebar(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    monkeypatch.setattr("src.app.ui.native_files_prefs.NativeFilesPreferences", lambda: prefs)
    parent = QWidget()
    qtbot.addWidget(parent)
    host = files_panel.FilesPanel(parent, include_plugins=False)
    panel = host._native_panel
    transport = host._native_transport
    peer = files_panel.FilesPanel(include_plugins=False)
    qtbot.addWidget(peer)
    host.library_registry_changed.connect(peer.refresh_library_registry)
    peer.library_registry_changed.connect(host.refresh_library_registry)
    parent.resize(1000, 660)
    host.resize(parent.size())
    parent.show()
    pool = MediaPoolPanel()
    qtbot.addWidget(pool)
    pool.open_ai_library_requested.connect(host.open_ai_library)

    def select_ai_action() -> None:
        popup = QApplication.activePopupWidget()
        assert isinstance(popup, QMenu)
        action = next(action for action in popup.actions()
                      if action.text() == I18N.tr("files.ai_library.open", "AI 素材库…"))
        qtbot.mouseClick(popup, Qt.MouseButton.LeftButton, pos=popup.actionGeometry(action).center())

    def click_ai_menu(button: QWidget) -> None:
        QTimer.singleShot(0, select_ai_action)
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

    def stale_window(*args: object) -> RegisteredAILibraryWindow:
        raise AttributeError("old transport has no ai_library_settings_loaded")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(files_panel, "RegisteredAILibraryWindow", stale_window)
            click_ai_menu(panel.more_button)
        notice = parent.findChild(QMessageBox)
        assert notice is not None and "ai_library_settings_loaded" in notice.detailedText()
        assert host._registered_ai_library_window is None
        notice.accept()

        with qtbot.waitSignal(transport.ai_library_results):
            click_ai_menu(panel.more_button)
        window = host._registered_ai_library_window
        assert window is not None and window.isVisible()
        window.close()
        pool.show()
        with qtbot.waitSignal(transport.ai_library_results):
            click_ai_menu(pool.btn_more)
        assert host._registered_ai_library_window is window and window.isVisible()
        window.close()

        media = tmp_path / "library"
        with qtbot.waitSignal(transport.library_changed):
            transport.register_library(str(tmp_path), media.name)
        tree = panel.locations
        peer_tree = peer._native_panel.locations
        qtbot.waitUntil(lambda: tree.library_root(tree.item_model.index(0, 0)))
        qtbot.waitUntil(lambda: peer_tree.library_root(peer_tree.item_model.index(0, 0)))
        index = tree.item_model.index(0, 0)
        with qtbot.waitSignal(panel.directory_requested) as directory:
            qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=tree.visualRect(index).center())
        assert directory.args == [str(media.resolve())]

        def choose_new_folder(menu: QMenu, *_: object) -> object:
            return menu.actions()[0]

        with monkeypatch.context() as patch:
            patch.setattr(QMenu, "exec", choose_new_folder)
            patch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("日素材", True))
            with qtbot.waitSignal(transport.task_finished):
                panel._show_library_menu(tree.visualRect(index).center())
        assert (media / "日素材").is_dir()
        qtbot.waitUntil(lambda: tree.item_model.index(0, 0, index).data() == "日素材")

        def choose_scoped_search(menu: QMenu, *_: object) -> object:
            label = I18N.tr("files.native.library_analyze", "AI 搜索与分析…")
            return next(action for action in menu.actions() if action.text() == label)

        with monkeypatch.context() as patch:
            patch.setattr(QMenu, "exec", choose_scoped_search)
            with qtbot.waitSignal(transport.ai_library_settings_loaded):
                panel._show_library_menu(tree.visualRect(index).center())
        assert window.isVisible()
        assert window.analysis_directory.currentText() == media.name

        library_id = window.analysis_directory.currentData()
        with qtbot.waitSignal(peer._native_transport.library_changed):
            peer._native_transport.rename_library(library_id, "Renamed library")
        qtbot.waitUntil(lambda: window.analysis_directory.currentText() == "Renamed library")
        assert tree.currentIndex().data() == "Renamed library"

        with qtbot.waitSignal(peer._native_transport.library_changed):
            peer._native_transport.remove_library(library_id)
        qtbot.waitUntil(lambda: not tree.library_root(tree.item_model.index(0, 0))
                        and window.analysis_directory.count() == 1)
        assert window.analysis_directory.currentData() is None
        assert media.is_dir()
    finally:
        peer.shutdown()
        host.shutdown()
        assert peer.wait_for_shutdown(3)
        assert host.wait_for_shutdown(3)


@pytest.mark.parametrize(("kind", "suffix"), [("video", ".mov"), ("audio", ".wav")])
def test_search_registered_library_and_import_a_timed_excerpt(
    qtbot: QtBot, tmp_path: Path, kind: str, suffix: str,
) -> None:
    directory = tmp_path / "media"
    directory.mkdir()
    sample = directory / f"store{suffix}"
    sample.write_bytes(b"fixture source bytes")
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    facade = FilesFacade(catalog)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    pool = MediaPoolPanel()
    qtbot.addWidget(pool)
    pool.set_project(EditProject.create("Library Excerpts").import_asset(
        path=str(sample), media_type=kind, metadata={"duration_seconds": 5.0, "fps": 24.0, "total_frames": 120},
    ))
    window = RegisteredAILibraryWindow(transport)
    qtbot.addWidget(window)
    window.import_requested.connect(pool.import_ai_library_candidate)
    try:
        facade.libraries.register(directory)
        state = facade.build_ai_library(
            lambda path, media_kind: (MediaEvidence("咖啡店门铃", 1.0, 2.5, ("门铃",)),),
            revision="fixture", cancel=CancelToken(), progress=lambda done, total: None,
        )
        assert state["indexed"] == 1
        window.search_edit.setText("门铃")
        with qtbot.waitSignal(transport.ai_library_results, timeout=3000):
            window.search()
        assert window.results.count() == 1
        assert window.import_button.isEnabled()
        with qtbot.waitSignal(pool.project_changed) as changed:
            window.import_button.click()
        project = changed.args[0]
        excerpts = [asset for asset in project.assets.values() if asset.metadata.get("is_subclip")]
        assert len(excerpts) == 1
        assert excerpts[0].media_type == kind
        assert get_effective_source_range(excerpts[0]) == (24, 59)
        assert excerpts[0].path == str(sample)
        window.import_button.click()
        assert len(pool._project.assets) == 2
    finally:
        transport.shutdown()
        stopped = Event()
        facade.when_idle(stopped.set)
        assert stopped.wait(3)
        catalog.close()


def test_copying_into_library_leaves_source_and_registers_destination(tmp_path: Path) -> None:
    source = (tmp_path / "original.wav").resolve()
    source.write_bytes(b"audio fixture")
    folder = (tmp_path / "library").resolve()
    folder.mkdir()
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    facade = FilesFacade(catalog)
    try:
        result = facade.copy_to_ai_library([str(source)], str(folder))
        assert result.ok
        assert (folder / source.name).read_bytes() == source.read_bytes()
        assert [library.root for library in facade.libraries.list_libraries()] == [str(folder)]
        child = folder / "shots"
        child.mkdir()
        assert facade.copy_to_ai_library([str(source)], str(child)).ok
        assert (child / source.name).is_file()
        assert [library.root for library in facade.libraries.list_libraries()] == [str(folder)]
    finally:
        facade.shutdown(wait=True)
        catalog.close()


def test_directory_model_consent_is_explicit_and_local_to_selected_directory(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    registry = ProfileRegistry()
    profile = AiProviderProfile(
        "visual", "Studio Vision", endpoint="https://vision.test/v1", model_id="vision-model",
        capabilities=("vision",), modalities=("image", "media_upload"),
        data_policy=DataPolicy(execution="cloud", allow_media_upload=True),
    )
    registry.add(profile)
    registry.set_active_for_capability("vision", profile.profile_id)
    monkeypatch.setattr(ai_library_analysis, "_load_profiles", lambda: registry)

    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    facade = FilesFacade(catalog)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    selected, other = facade.libraries.register(first), facade.libraries.register(second)
    window = RegisteredAILibraryWindow(transport)
    qtbot.addWidget(window)
    response = QMessageBox.StandardButton.Cancel
    prompts: list[str] = []

    def confirm(dialog: QMessageBox) -> int:
        assert dialog.defaultButton() == dialog.button(QMessageBox.StandardButton.Cancel)
        prompts.append(dialog.text())
        return int(response)

    monkeypatch.setattr(QMessageBox, "exec", confirm)
    try:
        with qtbot.waitSignal(transport.ai_library_settings_loaded, timeout=3000):
            transport.load_ai_library_settings()
        with qtbot.waitSignal(transport.ai_library_settings_loaded, timeout=3000):
            window.analysis_directory.setCurrentIndex(window.analysis_directory.findData(selected.library_id))
        shots = first / "shots"
        shots.mkdir()
        window.set_library_scope(selected.library_id, str(shots.resolve()))
        with qtbot.waitSignal(transport.ai_library_settings_loaded, timeout=3000):
            window.refresh_library_registry()
        window.analysis_mode.setCurrentIndex(window.analysis_mode.findData("profiles"))
        window.type_checks["video"].setChecked(False)
        window.type_checks["audio"].setChecked(False)
        window.recursive_check.setChecked(False)
        window.save_policy_button.click()
        assert facade.ai_library_analysis_policy(selected.library_id, folder=str(shots.resolve())) == LibraryAnalysisPolicy()
        assert len(prompts) == 1
        assert all(value in prompts[0] for value in (str(shots.resolve()), "https://vision.test", "vision-model"))

        response = QMessageBox.StandardButton.Yes
        with qtbot.waitSignal(transport.ai_library_settings_saved, timeout=3000) as saved:
            window.save_policy_button.click()
        assert saved.args == [""]
        saved_policy = facade.ai_library_analysis_policy(selected.library_id, folder=str(shots.resolve()))
        assert saved_policy.approved_routes == (ai_library_analysis.library_media_route("image", "visual", registry).approval,)
        assert saved_policy.kinds == ("image",) and not saved_policy.recursive
        assert dict(saved_policy.models or ())["image"] == "visual"
        assert facade.ai_library_analysis_policy(selected.library_id) == LibraryAnalysisPolicy()
        assert facade.ai_library_analysis_policy(other.library_id) == LibraryAnalysisPolicy()
    finally:
        transport.shutdown()
        stopped = Event()
        facade.when_idle(stopped.set)
        assert stopped.wait(3)
        catalog.close()


def test_analysis_without_audio_model_opens_speech_preferences(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(ai_library_analysis, "_load_profiles", ProfileRegistry)
    media = tmp_path / "media"
    media.mkdir()
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    facade = FilesFacade(catalog)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    window = RegisteredAILibraryWindow(transport)
    qtbot.addWidget(window)
    try:
        record = facade.libraries.register(media)
        child = media / "registered-child"
        child.mkdir()
        child_library = facade.libraries.register(child)
        window.set_library_scope(record.library_id, str(child.resolve()))
        with qtbot.waitSignal(transport.ai_library_settings_loaded, timeout=3000):
            window.refresh_library_registry()
        assert window.analysis_directory.currentData() == child_library.library_id
        assert window._folder_scope is None
        window.type_checks["image"].setChecked(False)
        window.type_checks["video"].setChecked(False)
        with qtbot.waitSignal(window.configure_models_requested) as requested:
            window.index_button.click()
        assert requested.args == ["speech"]
        assert not transport.ai_library_index_state().get("running")
    finally:
        transport.shutdown()
        stopped = Event()
        facade.when_idle(stopped.set)
        assert stopped.wait(3)
        catalog.close()


def test_changing_scope_clears_previous_consents_and_search_results(
    qtbot: QtBot, tmp_path: Path,
) -> None:
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    facade = FilesFacade(catalog)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    window = RegisteredAILibraryWindow(transport)
    qtbot.addWidget(window)
    try:
        window.set_library_scope("first", "/example/first")
        window._request_id = "first-request"
        window._pending_policy_scope = ("first", "/example/first")
        window._pending_policy = {"approved_routes": ("a" * 64,)}
        window._saving_policy = True
        window.set_library_scope("second", "/example/second")
        window._directory_policy_saved("")
        window._show_results("first-request", [{"name": "wrong.jpg", "online": True}], "")
        assert window.results.count() == 0 and not window.import_button.isEnabled()
        assert not window._scope_policy.get("approved_routes")
    finally:
        transport.shutdown()
        facade.shutdown(wait=True)
        catalog.close()


def test_pending_filter_and_gallery_badge_use_saved_analysis(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from src.app.ui.native_files_panel import NativeFilesPanel

    monkeypatch.setattr(ai_library_analysis, "_load_profiles", ProfileRegistry)
    root = tmp_path / "media"
    root.mkdir()
    previous = root / "finished.png"
    pending = root / "pending.png"
    previous.write_bytes(b"finished")
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    facade = FilesFacade(catalog)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    window = RegisteredAILibraryWindow(transport)
    qtbot.addWidget(window)
    gallery = NativeFilesPanel(
        home_path=str(tmp_path), transport=transport,
        preferences=NativeFilesPreferences(QSettings(str(tmp_path / "gallery.ini"), QSettings.Format.IniFormat)),
    )
    qtbot.addWidget(gallery)
    transport.ai_analysis_marks_loaded.connect(gallery.apply_ai_marks)
    try:
        record = facade.libraries.register(root)
        gallery.show_libraries([{"library_id": record.library_id, "path": record.root, "name": record.name}])
        report = facade.build_ai_library(
            lambda _path, _kind: (MediaEvidence("画面描述"),), revision="test",
            cancel=CancelToken(), progress=lambda *_: None, library_id=record.library_id,
        )
        assert report["indexed"] == 1
        pending.write_bytes(b"pending")
        window.set_library_scope(record.library_id)
        window.show()
        with qtbot.waitSignal(transport.ai_library_settings_loaded, timeout=3000):
            window.refresh_library_registry()
        with qtbot.waitSignal(transport.ai_library_results, timeout=3000):
            window.pending_only_check.setChecked(True)
        assert [window.results.item(row).text() for row in range(window.results.count())] == [pending.name]
        transport.loaded.connect(gallery.show_directory_page)
        with qtbot.waitSignal(transport.ai_analysis_marks_loaded, timeout=3000):
            transport.load_directory(record.root)
        assert gallery.card_delegate.ai_analyzed(str(previous.resolve()))
        assert not gallery.card_delegate.ai_analyzed(str(pending.resolve()))
    finally:
        transport.shutdown()
        stopped = Event()
        facade.when_idle(stopped.set)
        assert stopped.wait(3)
        catalog.close()


def test_new_candidate_imports_audio_and_excerpt_into_selected_bin(qtbot: QtBot, tmp_path: Path) -> None:
    source = tmp_path / "bell.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 48_000)

    pool = MediaPoolPanel()
    qtbot.addWidget(pool)
    project = EditProject.create("Library Import").create_bin("Effects")
    selected_bin = next(bin_ for bin_ in project.bins.values() if bin_.name == "Effects")
    pool.set_project(project)
    pool._on_bin_selected(selected_bin.bin_id, False)
    file_stat = source.stat()
    hit = {"asset_id": "global-bell", "path": str(source), "online": True,
           "start_seconds": 1.0, "end_seconds": 2.0,
           "source_size": file_stat.st_size, "source_mtime_ns": file_stat.st_mtime_ns}
    with qtbot.waitSignal(pool.project_changed, timeout=5000) as changed:
        pool.import_ai_library_candidate(hit)
    committed = changed.args[0]
    in_bin = [committed.assets[asset_id] for asset_id in committed.bins[selected_bin.bin_id].asset_ids]
    assert len(in_bin) == 2
    excerpt = next(asset for asset in in_bin if asset.metadata.get("is_subclip"))
    assert excerpt.metadata["global_library_asset_id"] == "global-bell"
    assert get_effective_source_range(excerpt) == (24, 47)
