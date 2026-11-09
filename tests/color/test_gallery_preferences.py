"""G11 Gallery 显示偏好与外部变化同步测试。

门槛映射：版本化偏好原子落盘、损坏/未来 schema 回退不清文件；旧
presets_columns 迁移不产生 2px 缩略图；重启/往返后视图状态恢复；外部
删除后重扫选择自然清理；可见性触发同步且索引未变零磁盘。
"""
import json
import os

import pytest

from src.features.color.core.gallery_preferences import (
    GalleryPreferences,
    load_preferences,
    migrate_legacy_columns,
    preferences_path,
    save_preferences,
)


class TestPreferencesService:
    def test_roundtrip_and_clamps(self, tmp_path):
        prefs = GalleryPreferences(thumbnail_width=999, view_mode="bogus")
        assert prefs.thumbnail_width == 256          # 钳制
        assert prefs.view_mode == "grid"
        assert save_preferences(str(tmp_path), prefs)
        loaded = load_preferences(str(tmp_path))
        assert loaded.thumbnail_width == 256
        assert loaded.view_mode == "grid"

    def test_corrupt_json_falls_back_without_touching_file(self, tmp_path):
        path = preferences_path(str(tmp_path))
        with open(path, "w") as fh:
            fh.write("{ broken")
        prefs = load_preferences(str(tmp_path))
        assert prefs.thumbnail_width == 108
        # 用户文件保持原样（回退不等于清除）
        assert "broken" in open(path).read()

    def test_future_schema_defaults_without_overwrite(self, tmp_path):
        path = preferences_path(str(tmp_path))
        with open(path, "w") as fh:
            json.dump({"schema": 99, "thumbnail_width": 120}, fh)
        prefs = load_preferences(str(tmp_path))
        assert prefs.schema == 1 and prefs.thumbnail_width == 108
        assert json.load(open(path))["schema"] == 99   # 未被覆盖

    def test_legacy_columns_migration_never_2px(self):
        assert migrate_legacy_columns(2) == 192
        assert migrate_legacy_columns(6) == 112
        assert migrate_legacy_columns("garbage") == 108
        assert migrate_legacy_columns(None) == 108
        for columns in range(2, 7):
            width = migrate_legacy_columns(columns)
            assert 108 <= width <= 256

    def test_save_failure_returns_false(self, tmp_path):
        assert save_preferences(str(tmp_path / "missing" / "deep"),
                                GalleryPreferences()) is False


class TestPanelPreferencesAndSync:
    def _make_panel(self, tmp_path, monkeypatch, qtbot):
        monkeypatch.chdir(tmp_path)
        from src.features.color.ui import presets_panel
        monkeypatch.setattr(
            presets_panel, "_resolve_presets_root",
            lambda: str(tmp_path / "Presets"))
        widget = presets_panel.PresetsPanel()
        qtbot.addWidget(widget)
        return widget

    def test_view_state_survives_restart(self, tmp_path, monkeypatch, qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        panel.zoom_slider.setValue(200)
        panel._gallery.set_view_mode("list")
        panel._flush_preferences()
        # “重启”：新面板从同一根目录恢复
        from src.features.color.ui import presets_panel as module
        second = module.PresetsPanel()
        qtbot.addWidget(second)
        assert second.zoom_slider.value() == 200
        assert second._gallery.grid.list_mode is True

    def test_external_delete_clears_stale_selection(self, tmp_path,
                                                    monkeypatch, qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        folder = panel.get_current_folder()
        for name in ("A", "B"):
            with open(os.path.join(folder, f"{name}.topos"), "wb") as fh:
                fh.write(b"zip")
        panel.scan_presets()
        panel.list_widget.item(0).setSelected(True)
        panel.list_widget.item(1).setSelected(True)
        # 外部删除 A（不经面板）
        os.remove(os.path.join(folder, "A.topos"))
        panel.scan_presets()
        selected = {item.text() for item in panel.list_widget.selectedItems()}
        assert selected == {"B"}                # 外删条目自然清出选择

    def test_show_event_resyncs_external_addition_via_index(self, tmp_path,
                                                            monkeypatch,
                                                            qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        folder = panel.get_current_folder()
        with open(os.path.join(folder, "A.topos"), "wb") as fh:
            fh.write(b"zip")
        panel.scan_presets()
        assert panel.list_widget.count() == 1
        with open(os.path.join(folder, "B.topos"), "wb") as fh:
            fh.write(b"zip")
        from PyQt6.QtGui import QShowEvent
        panel.showEvent(QShowEvent())
        assert panel.list_widget.count() == 2    # 可见性驱动同步生效

    def test_prefs_file_is_json_not_project_data(self, tmp_path, monkeypatch,
                                                 qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        panel._flush_preferences()
        path = preferences_path(panel.presets_root)
        assert os.path.isfile(path)
        data = json.load(open(path))
        assert set(data) == {"schema", "thumbnail_width", "view_mode",
                             "sort_desc", "sidebar_visible"}
        # 无绝对路径/无像素缓存数据
        assert panel.presets_root not in json.dumps(data)
