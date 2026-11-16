"""B10 / U06：用户级 UI 配置往返（临时 ini，不碰本机 QSettings）。"""
from __future__ import annotations

from PyQt6.QtCore import QSettings

from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences


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
