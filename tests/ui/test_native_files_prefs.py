"""B10 / U06：用户级 UI 配置往返（临时 ini，不碰本机 QSettings）。"""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QCheckBox, QComboBox, QGroupBox, QSpinBox, QWidget

from src.app.ui.files_browser_preferences import FilesBrowserPreferencePage
from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.shared.i18n.i18n import I18N


def _prefs(tmp_path) -> NativeFilesPreferences:
    ini = tmp_path / "prefs.ini"
    return NativeFilesPreferences(QSettings(str(ini), QSettings.Format.IniFormat))


def test_defaults_preserve_responsive_behaviour(tmp_path):
    prefs = _prefs(tmp_path)
    # 未存过值：入口默认原生；侧栏/信息栏交给宽度自适应（None）
    assert prefs.native_default is True
    assert prefs.sidebar_preference is None
    assert prefs.inspector_preference is None
    assert prefs.view_index == 0
    assert prefs.density == 180


def test_view_density_and_sort_roundtrip_between_panels(qtbot, tmp_path):
    prefs = _prefs(tmp_path)
    first = NativeFilesPanel(preferences=prefs)
    qtbot.addWidget(first)
    first.view_combo.setCurrentIndex(first.view_combo.findData("list"))
    first.density_slider.setValue(210)
    first.sort_combo.setCurrentIndex(1)

    assert prefs.view_index == 1
    assert prefs.density == 210
    assert prefs.sort_index == 1

    second = NativeFilesPanel(preferences=prefs)
    qtbot.addWidget(second)
    assert second.view_combo.currentData() == "list"
    assert second.pages.currentIndex() == 1
    assert second.density_slider.value() == 210
    assert second.sort_combo.currentIndex() == 1
    assert second.card_delegate.card_width == 210


def test_manual_sidebar_and_inspector_choice_is_sticky(qtbot, tmp_path):
    prefs = _prefs(tmp_path)
    panel = NativeFilesPanel(preferences=prefs)
    qtbot.addWidget(panel)
    panel.resize(1200, 700)
    panel.show()
    panel.sidebar_button.click()  # 真实点击路径（toggled 不是连接信号）
    assert prefs.sidebar_preference is False
    panel.inspector_button.click()
    assert prefs.inspector_preference is True

    restored = NativeFilesPanel(preferences=prefs)
    qtbot.addWidget(restored)
    restored.resize(1200, 700)
    restored.show()
    assert not restored.sidebar.isVisible()  # 用户关掉的侧栏不会被宽度自适应重新打开
    assert restored._inspector_preference is True


def test_preferences_do_not_store_paths_or_secrets(tmp_path):
    prefs = _prefs(tmp_path)
    prefs.view_index = 1
    settings = prefs._settings
    settings.sync()
    keys = settings.allKeys()
    assert keys and all(key.startswith("files/native/") for key in keys)
    # 只允许 UI 状态键：任何路径/搜索历史键都视为越界
    allowed = {"files/native/native_default", "files/native/view_index",
               "files/native/density", "files/native/sort_index",
               "files/native/sidebar_preference", "files/native/inspector_preference"}
    assert set(keys) <= allowed


def test_favorites_and_history_are_bounded_and_deduped(tmp_path):
    from PyQt6.QtCore import QSettings

    from src.app.ui.native_files_prefs import NativeFilesPreferences

    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
    prefs = NativeFilesPreferences(settings)
    prefs.remember_location("/a")
    prefs.remember_location("/b")
    prefs.remember_location("/a")
    assert prefs.history[:2] == ["/a", "/b"]
    prefs.favorites = [f"/lib/{i}" for i in range(20)]
    assert len(prefs.favorites) == prefs.MAX_FAVORITES
    prefs.remember_search("clip")
    prefs.remember_search("Clip")
    assert prefs.search_history == ["Clip"]


def _set_option(widget: QWidget, name: str, value: object) -> None:
    combo = widget.findChild(QComboBox, name)
    index = combo.findData(value)
    assert index >= 0
    combo.setCurrentIndex(index)


def test_browser_preferences_stage_edits_and_reload_both_views(qtbot, tmp_path) -> None:
    prefs = _prefs(tmp_path)
    files = NativeFilesPanel(preferences=prefs)
    library = NativeFilesPanel(preferences=_prefs(tmp_path))
    for panel in (files, library):
        qtbot.addWidget(panel)
        panel.resize(1200, 700)
        panel.show()
    files.sort_combo.setCurrentIndex(1)
    library.sort_combo.setCurrentIndex(2)

    parent = QWidget()
    qtbot.addWidget(parent)
    page = FilesBrowserPreferencePage(prefs)
    widget = page.create_widget(parent)
    page.load({})
    assert not any(check.isChecked() for check in widget.findChildren(QCheckBox))
    _set_option(widget, "filesBrowserLayout", "grid")
    _set_option(widget, "filesBrowserGridFill", True)
    _set_option(widget, "filesBrowserSidebar", False)
    _set_option(widget, "filesBrowserInspector", True)
    widget.findChild(QSpinBox, "filesBrowserThumbnailWidth").setValue(232)
    widget.findChild(QCheckBox, "filesBrowserCard_rating").setChecked(True)
    assert prefs.layout_mode == "adaptive"
    assert prefs.sidebar_preference is None
    assert prefs.card_metadata == []

    page.apply(None)
    assert prefs.layout_mode == "grid"
    assert prefs.grid_fill is True
    assert prefs.sidebar_preference is False
    assert prefs.inspector_preference is True
    assert prefs.density == 232
    assert prefs.card_metadata == ["rating"]

    stored = (tmp_path / "prefs.ini").read_bytes()
    for panel in (files, library):
        panel.reload_preferences()
        assert panel.view_combo.currentData() == "grid"
        assert panel.card_delegate.grid_fill is True
        assert panel.card_delegate.caption_fields == {"rating"}
        assert panel.density_slider.value() == 232
        assert panel.sidebar.isHidden()
        assert not panel.inspector.isHidden()
    assert files.sort_combo.currentData() == "date"
    assert library.sort_combo.currentData() == "size"
    assert (tmp_path / "prefs.ini").read_bytes() == stored


def test_restore_browser_defaults_and_rollback_only_changed_fields(qtbot, tmp_path) -> None:
    prefs = _prefs(tmp_path)
    prefs.layout_mode = "masonry"
    prefs.sidebar_preference = False
    prefs.inspector_preference = True
    prefs.density = 250
    prefs.card_metadata = ["tags"]
    prefs.search_history = ["clip"]
    panel = NativeFilesPanel(preferences=prefs)
    qtbot.addWidget(panel)
    panel.resize(1200, 700)
    panel.show()

    parent = QWidget()
    qtbot.addWidget(parent)
    page = FilesBrowserPreferencePage(prefs)
    page.create_widget(parent)
    page.load({})
    page.restore_defaults({})
    assert prefs.layout_mode == "masonry"
    assert prefs.sidebar_preference is False
    assert prefs.density == 250

    page.apply(None)
    assert prefs.layout_mode == "adaptive"
    assert prefs.sidebar_preference is None
    assert prefs.inspector_preference is None
    assert prefs.density == 180
    assert prefs.card_metadata == []
    panel.reload_preferences()
    assert not panel.sidebar.isHidden()
    assert panel.inspector.isHidden()

    # A different writer can update a setting between saving and rollback.
    prefs.density = 215
    prefs.grid_fill = True
    page.rollback()
    assert prefs.layout_mode == "masonry"
    assert prefs.sidebar_preference is False
    assert prefs.inspector_preference is True
    assert prefs.density == 215
    assert prefs.grid_fill is True
    assert prefs.card_metadata == ["tags"]
    assert prefs.search_history == ["clip"]


@pytest.mark.parametrize(("locale", "title"), [("zh_CN", "浏览视图"), ("fr_FR", "Browser View")])
def test_browser_preferences_fit_narrow_panel(qtbot, tmp_path, monkeypatch, locale: str, title: str) -> None:
    monkeypatch.setattr(I18N, "_current_locale", locale)
    parent = QWidget()
    qtbot.addWidget(parent)
    page = FilesBrowserPreferencePage(_prefs(tmp_path))
    widget = page.create_widget(parent)
    page.load({})
    parent.resize(532, 420)
    widget.resize(parent.size())
    parent.show()
    qtbot.waitUntil(widget.isVisible)

    assert widget.findChildren(QGroupBox)[0].title() == title
    assert widget.horizontalScrollBar().maximum() == 0


def test_reloading_unchanged_preferences_keeps_the_gallery_layout(qtbot, tmp_path, monkeypatch) -> None:
    panel = NativeFilesPanel(preferences=_prefs(tmp_path))
    qtbot.addWidget(panel)
    changed = []
    monkeypatch.setattr(panel, "_change_layout", lambda *args, **kwargs: changed.append("view"))
    monkeypatch.setattr(panel.icon_view, "doItemsLayout", lambda: changed.append("gallery"))

    panel.reload_preferences()

    assert changed == []
