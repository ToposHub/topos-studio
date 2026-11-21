"""A03 路径身份 + A10 窄布局：纯逻辑与控件可达性（DPI 由 platform_check 子进程覆盖）。"""
from __future__ import annotations

import unicodedata
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSettings, QUrl
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
    for widget in (panel.back_button, panel.up_button, panel.type_combo,
                   panel.flag_combo, panel.view_combo, panel.inspector_button):
        assert widget.isVisible()
        assert widget.width() > 0
        assert widget.mapTo(panel, widget.rect().center()).x() >= 0
    assert panel.sidebar.width() >= 112
    assert panel.inspector.width() >= 200


def test_gallery_toolbar_fits_when_inspector_is_open(qtbot: QtBot, tmp_path: Path) -> None:
    from src.app.ui.native_files_panel import NativeFilesPanel
    from src.app.ui.native_files_prefs import NativeFilesPreferences

    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    panel = NativeFilesPanel(preferences=prefs)
    qtbot.addWidget(panel)
    panel.resize(1246, 650)
    panel.show()
    assert panel.gallery_toolbar.isAncestorOf(panel.density_slider)
    assert panel.gallery_toolbar.geometry().bottom() < panel.pages.geometry().top()

    panel.resize(380, 520)
    panel.inspector_button.click()
    toolbar = panel.gallery_toolbar
    qtbot.waitUntil(lambda: panel.inspector.isVisible() and panel.flag_combo.isHidden())
    for widget in (panel.type_combo, panel.density_slider):
        assert widget.isVisible()
        qtbot.waitUntil(lambda: toolbar.rect().contains(QRect(widget.mapTo(toolbar, QPoint()), widget.size())))
    assert panel.more_button.geometry().right() < panel.width()
