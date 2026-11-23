"""A03 路径身份 + A10 窄布局：纯逻辑与控件可达性（DPI 由 platform_check 子进程覆盖）。"""
from __future__ import annotations

import unicodedata
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSettings, QSize, Qt, QUrl
from PyQt6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from src.features.files.contracts import identity_key, normalize_path


def test_unicode_forms_share_identity():
    nfc = unicodedata.normalize("NFC", "素材 café #1 100%.mov")
    nfd = unicodedata.normalize("NFD", nfc)
    assert identity_key(nfc) == identity_key(nfd)
    assert identity_key("/A/B.MOV") == identity_key("/a/b.mov")


def test_windows_and_unc_paths_are_normalized_not_url_guessed():
    assert normalize_path("C:\\Media\\clip.mov") == "C:/Media/clip.mov"
    assert normalize_path("E:\\") == "E:/"
    assert normalize_path("\\\\server\\share\\folder") == "//server/share/folder"


def test_local_url_roundtrip_keeps_special_characters():
    for path in ("/Users/creator/中文 空格/引号'\"#%&.mov",
                 unicodedata.normalize("NFD", "/Users/creator/café.mov")):
        url = QUrl.fromLocalFile(path)
        assert url.toLocalFile() == path
        assert identity_key(url.toLocalFile()) == identity_key(path)


def test_narrow_panel_keeps_primary_controls_reachable(qtbot):
    from src.app.ui.native_files_panel import NativeFilesPanel

    panel = NativeFilesPanel(home_path="/Users/creator", preferences=None)
    qtbot.addWidget(panel)
    panel.resize(760, 520)
    panel.show()
    qtbot.wait(50)
    # 760px 下搜索框按设计收起；窄布局验收要求其余主控件仍可达
    for widget in (panel.back_button, panel.up_button, panel.file_filter_button,
                   panel.display_button, panel.view_combo, panel.inspector_button):
        assert widget.isVisible()
        assert widget.width() > 0
        assert widget.mapTo(panel, widget.rect().center()).x() >= 0
    for button in (panel.back_button, panel.forward_button, panel.up_button):
        assert button.text() == ""
        assert button.iconSize() == QSize(20, 20)
        assert not button.icon().isNull()
    panel.splitter.moveSplitter(1, 1)
    assert panel.sidebar.width() == 210
    assert panel.inspector.width() >= 200


def test_gallery_toolbar_fits_when_inspector_is_open(qtbot: QtBot, tmp_path: Path) -> None:
    from src.app.ui.native_files_panel import NativeFilesPanel
    from src.app.ui.native_files_prefs import NativeFilesPreferences

    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    panel = NativeFilesPanel(preferences=prefs)
    qtbot.addWidget(panel)
    panel.resize(1246, 650)
    panel.show()
    rule = panel.sidebar.findChild(QWidget, "filesRule")
    assert rule.height() == 1
    assert rule.width() == panel.sidebar.width()
    assert rule.mapTo(panel, QPoint()).y() == panel.gallery_toolbar.mapTo(
        panel, QPoint(0, panel.gallery_toolbar.height())
    ).y()
    assert panel.sidebar.width() == 240
    panel.inspector_button.click()
    assert panel.inspector.width() == 210
    assert panel.sidebar.width() == 240
    inspector_rule = panel.inspector.findChild(QWidget, "filesRule", Qt.FindChildOption.FindDirectChildrenOnly)
    assert inspector_rule.height() == 1
    assert inspector_rule.width() == panel.inspector.width()
    assert inspector_rule.mapTo(panel, QPoint()).y() == rule.mapTo(panel, QPoint()).y()
    panel.inspector_button.click()
    assert panel.gallery_toolbar.isAncestorOf(panel.density_slider)
    assert panel.gallery_toolbar.geometry().bottom() < panel.pages.geometry().top()

    panel.resize(380, 520)
    panel.inspector_button.click()
    toolbar = panel.gallery_toolbar
    qtbot.waitUntil(lambda: panel.inspector.isVisible() and panel.sort_combo.isHidden())
    qtbot.waitUntil(lambda: inspector_rule.mapTo(panel, QPoint()).y() == toolbar.mapTo(
        panel, QPoint(0, toolbar.height())
    ).y())
    for widget in (panel.file_filter_button, panel.density_slider):
        assert widget.isVisible()
        qtbot.waitUntil(lambda: toolbar.rect().contains(QRect(widget.mapTo(toolbar, QPoint()), widget.size())))
    assert panel.more_button.geometry().right() < panel.width()


def test_library_share_actions_use_location_context_menus(
    qtbot: QtBot, monkeypatch, tmp_path: Path
) -> None:
    from PyQt6.QtWidgets import QFileDialog, QMenu, QPushButton

    from src.app.ui.native_files_panel import NativeFilesPanel, _tr

    panel = NativeFilesPanel(home_path=str(tmp_path), preferences=None)
    qtbot.addWidget(panel)
    panel.enable_grade_set_locations(lambda: None)
    panel.resize(900, 520)
    panel.show()
    model = panel.locations.model()
    heading = model.index(0, 0)
    import_text = _tr("library_import", "导入素材库…")
    export_text = _tr("library_export", "导出素材库…")
    package = str(tmp_path / "Example.toposlibrary")
    receiver = str(tmp_path / "receiver")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (package, ""))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: receiver)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (package, ""))

    def open_import(menu: QMenu, position: QPoint) -> None:
        actions = {action.text(): action for action in menu.actions()}
        assert export_text not in actions
        actions[import_text].trigger()

    monkeypatch.setattr(QMenu, "exec", open_import)
    with qtbot.waitSignal(panel.library_import_requested, timeout=500) as imported:
        panel._show_library_menu(panel.locations.visualRect(heading).center())
    assert imported.args == [package, receiver]
    panel.show_library_transfer_result(True, "Imported")
    panel.show_libraries([{"library_id": "library", "name": "Example", "path": str(tmp_path / "Example")}])
    library = model.index(0, 0, heading)

    def open_export(menu: QMenu, position: QPoint) -> None:
        actions = {action.text(): action for action in menu.actions()}
        assert import_text in actions
        actions[export_text].trigger()

    monkeypatch.setattr(QMenu, "exec", open_export)
    with qtbot.waitSignal(panel.library_export_requested, timeout=500) as exported:
        panel._show_library_menu(panel.locations.visualRect(library).center())
    assert exported.args == ["library", package]
    for name in ("openAILibrary", "addMediaLibrary", "newGradeSet"):
        button = panel.findChild(QPushButton, name)
        assert not button.icon().isNull()
        assert button.iconSize() == QSize(20, 20)
        assert button.size() == QSize(28, 20)
    with qtbot.waitSignal(panel.ai_library_requested, timeout=500):
        panel.findChild(QPushButton, "openAILibrary").click()
