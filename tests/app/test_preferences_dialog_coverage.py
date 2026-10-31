"""PreferencesDialog headless coverage tests（计划02 补测，模块 B3）。

覆盖 ``src/app/ui/preferences_dialog.py`` 中可离屏（offscreen Qt）测试的单元：
- 各偏好组的 load/save 往返（General/Appearance/Performance/File & Storage/
  Timeline Defaults/Advanced）
- Restore Defaults（确认弹窗 Yes/No 两条路径 + 预览信号防抖）
- Apply / Cancel / Save 语义（含 Apply 后 Cancel 的配置快照回滚）
- 重启标记（locale / work_mode / developer_mode 变更）
- 非法值加载回退（未知枚举回退首项、数值 clamp、None 阈值兜底）
- 外观即时预览信号（含 _preview_blocked 抑制与批量回滚）
- 分类导航 / select_page / 图片模式隐藏视频页 / feature 过滤
- 模块偏好页（ModularPreferencePage）load/apply/restore_defaults 委托
- file_explorer 插件列表刷新与停用状态写回

锁定现状的用例（断言引用 BUG-B3-xx，修复 src 时需同步更新）：
- ``test_apply_without_refresh_clears_fe_disabled_plugins`` → BUG-B3-01
  （已随 AUD-03900 修复翻转为 ``test_apply_without_refresh_preserves_fe_disabled_plugins``）
- ``test_restore_defaults_skips_smart_threshold_lock_in`` → BUG-B3-02
  （已修复，翻转为 ``test_restore_defaults_resets_smart_threshold``）
- ``test_timeline_resolution_preset_combo_not_synced_with_config`` → BUG-B3-03
  （已修复，翻转为 ``test_timeline_resolution_preset_combo_syncs_with_config``）
- ``test_restore_defaults_skips_pass_fusion_mode_lock_in`` → BUG-B3-04
  （已修复，翻转为 ``test_restore_defaults_resets_pass_fusion_mode``）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional

import time

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog, QLabel,
                             QMessageBox, QWidget)

import src.app.ui.preferences_dialog as preferences_dialog_module
from src.app.ui.preferences_dialog import PreferencesDialog
from src.shared.config.config_manager import ConfigManager


def _wait_for_cache_label(label: QLabel, suffix: str,
                          timeout: float = 5.0) -> None:
    """等待缓存大小标签异步回填到期望后缀。

    ef-19/AUD-03908：缓存目录扫盘移入后台线程，经 ``cache_size_ready``
    信号回 UI 线程刷新标签——断言前需泵事件循环等待扫描完成。
    """
    deadline = time.monotonic() + timeout
    while not label.text().endswith(suffix):
        if time.monotonic() > deadline:
            break
        QApplication.processEvents()
        time.sleep(0.02)
    assert label.text().endswith(suffix)


def _wait_cache_label_settled(label: QLabel, timeout: float = 5.0) -> None:
    """等待构造期扫描落定（标签从 '--' 变为实际值）；超时则视为无扫描。"""
    deadline = time.monotonic() + timeout
    while label.text().endswith("--"):
        if time.monotonic() > deadline:
            return
        QApplication.processEvents()
        time.sleep(0.02)


# ── 测试替身 ────────────────────────────────────────────────────


class _RecordingConfig:
    """ConfigManager 替身：仅内存读写，记录 set()/save_config() 调用。"""

    def __init__(self, initial: Optional[dict] = None) -> None:
        self.DEFAULT_CONFIG = ConfigManager.DEFAULT_CONFIG
        self.config: dict = {**self.DEFAULT_CONFIG, **(initial or {})}
        self.set_calls: list[tuple[str, Any, bool]] = []
        self.save_count = 0

    def get(self, key: str) -> Any:
        return self.config.get(key)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.set_calls.append((key, value, auto_save))
        self.config[key] = value

    def save_config(self) -> None:
        self.save_count += 1


class _FakeFeModule:
    """file_explorer.main 替身：内存 config + 可控 plugin_manager。"""

    def __init__(self, disabled: Optional[list[str]] = None,
                 plugins: Optional[list[dict]] = None) -> None:
        self.conf: dict = {"disabled_plugins": list(disabled or [])}
        self.saved: Optional[dict] = None
        self.plugin_manager = SimpleNamespace(plugins=list(plugins or []))

    def load_config(self) -> dict:
        return dict(self.conf)

    def save_config(self, conf: dict) -> None:
        self.saved = dict(conf)


class _FakeModulePage:
    """ModularPreferencePage 最小实现，记录生命周期回调。"""

    def __init__(self, page_id: str) -> None:
        self.page_id = page_id
        self.created = False
        self.loads: list[dict] = []
        self.applies = 0
        self.default_restores: list[dict] = []

    def create_widget(self, parent: Optional[QWidget] = None) -> QWidget:
        self.created = True
        return QLabel(f"fake page: {self.page_id}")

    def load(self, config: dict) -> None:
        self.loads.append(config)

    def apply(self, config_manager: Any) -> None:
        self.applies += 1

    def restore_defaults(self, defaults: dict) -> None:
        self.default_restores.append(defaults)


# ── 工具函数 ────────────────────────────────────────────────────


def _combo_index_for_data(combo: QComboBox, data: Any) -> int:
    for i in range(combo.count()):
        if combo.itemData(i) == data:
            return i
    return -1


def _set_combo_by_data(combo: QComboBox, data: Any) -> None:
    index = _combo_index_for_data(combo, data)
    assert index >= 0, f"combo has no item with data {data!r}"
    combo.setCurrentIndex(index)


def _combo_index_for_text(combo: QComboBox, text: str) -> int:
    for i in range(combo.count()):
        if combo.itemText(i) == text:
            return i
    return -1


def _confirm_with(monkeypatch: pytest.MonkeyPatch,
                  button: QMessageBox.StandardButton) -> None:
    """把 QMessageBox.question 替换为固定回答，避免模态弹窗。"""
    monkeypatch.setattr(
        preferences_dialog_module.QMessageBox, "question",
        lambda *args, **kwargs: button,
    )


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def make_dialog(qapp, monkeypatch, tmp_path) -> Iterator[
        Callable[..., tuple[PreferencesDialog, _RecordingConfig]]]:
    """PreferencesDialog 工厂：hermetic 缓存目录 + 关闭真实 FE 插件桥。"""
    import src.shared.io.file_io as file_io
    monkeypatch.setattr(file_io.FileIO, "CACHE_DIR", str(tmp_path / "images_cache"))
    monkeypatch.setattr(file_io.FileIO, "BAKED_CACHE_DIR", str(tmp_path / "baked_layers"))

    created: list[PreferencesDialog] = []

    def _make(initial: Optional[dict] = None,
              module_pages: Optional[list[Any]] = None,
              feature_flags: Any = None) -> tuple[PreferencesDialog, _RecordingConfig]:
        cfg = _RecordingConfig(initial)
        dlg = PreferencesDialog(
            cfg, module_pages=list(module_pages or []), feature_flags=feature_flags)
        # 默认切断真实 file_explorer 桥，防止 _on_apply 写开发者本机 FE config。
        # 需要 FE 行为的用例在测试体内重新覆盖该实例属性。
        dlg._fe_plugin_bridge = lambda: None  # type: ignore[method-assign]
        created.append(dlg)
        return dlg, cfg

    yield _make
    for dlg in created:
        dlg.deleteLater()


def _page_ids(dlg: PreferencesDialog) -> list[str]:
    return [d.page_id for d in dlg._page_registry.get_pages(dlg._enabled_features)]


# ── General 页 ──────────────────────────────────────────────────


class TestGeneralPageRoundTrip:

    def test_general_widgets_load_non_default_values(self, make_dialog) -> None:
        dlg, _ = make_dialog({
            "locale": "zh_CN",
            "auto_save_interval": 30,
            "max_recent_files": 15,
            "undo_history_limit": 120,
            "show_welcome_on_startup": False,
            "confirm_before_reset": False,
            "auto_load_last_project": False,
            "check_updates_on_startup": False,
        })
        w = dlg._widgets
        assert w["locale"].currentData() == "zh_CN"
        assert w["auto_save_interval"].intValue() == 30
        assert w["max_recent_files"].intValue() == 15
        assert w["undo_history_limit"].intValue() == 120
        assert w["show_welcome_on_startup"].isChecked() is False
        assert w["confirm_before_reset"].isChecked() is False
        assert w["auto_load_last_project"].isChecked() is False
        assert w["check_updates_on_startup"].isChecked() is False

    def test_general_round_trip(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        w = dlg._widgets
        _set_combo_by_data(w["locale"], "zh_CN")
        w["auto_save_interval"].setValue(45)
        w["max_recent_files"].setValue(20)
        w["undo_history_limit"].setValue(200)
        w["show_welcome_on_startup"].setChecked(False)
        w["confirm_before_reset"].setChecked(False)
        w["auto_load_last_project"].setChecked(False)
        w["check_updates_on_startup"].setChecked(False)
        dlg._on_apply()
        assert cfg.config["locale"] == "zh_CN"
        assert cfg.config["auto_save_interval"] == 45
        assert cfg.config["max_recent_files"] == 20
        assert cfg.config["undo_history_limit"] == 200
        assert cfg.config["show_welcome_on_startup"] is False
        assert cfg.config["confirm_before_reset"] is False
        assert cfg.config["auto_load_last_project"] is False
        assert cfg.config["check_updates_on_startup"] is False

    def test_work_mode_round_trip_in_studio(self, make_dialog, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
        dlg, cfg = make_dialog({"work_mode": "video"})
        assert dlg._widgets["work_mode"] is not None
        assert dlg._widgets["work_mode"].currentData() == "video"
        _set_combo_by_data(dlg._widgets["work_mode"], "image")
        dlg._on_apply()
        assert cfg.config["work_mode"] == "image"

    def test_work_mode_widget_absent_outside_studio(self, make_dialog, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "topos-color")
        dlg, cfg = make_dialog()
        assert dlg._widgets["work_mode"] is None
        dlg._on_apply()
        assert all(key != "work_mode" for key, _value, _auto in cfg.set_calls)

    def test_apply_writes_without_autosave_then_saves_once(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        dlg._on_apply()
        assert cfg.set_calls, "Apply 必须写回全部设置"
        assert all(auto_save is False for _k, _v, auto_save in cfg.set_calls)
        assert cfg.save_count == 1
        written = {key for key, _v, _a in cfg.set_calls}
        for expected in (
            "locale", "ui_scale", "panel_opacity", "backend",
            "video_playback_resolution", "video_macos_vt_zero_copy",
            "pass_fusion_mode", "render_cache_auto_mode", "proxy_format",
            "log_level", "plugin_directory", "default_timeline_fps",
            "default_subtitle_clip_duration_frames",
        ):
            assert expected in written, f"Apply 未写回 {expected}"


# ── Appearance / Performance 页 ─────────────────────────────────


class TestAppearanceAndPerformanceRoundTrip:

    def test_appearance_round_trip(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        w = dlg._widgets
        _set_combo_by_data(w["ui_scale"], 120)
        w["panel_opacity"].setValue(88)
        w["show_tooltips"].setChecked(False)
        _set_combo_by_data(w["accent_color"], "#4488ff")
        w["show_panel_titles"].setChecked(False)
        w["zoom_speed"].setValue(150)
        dlg._on_apply()
        assert cfg.config["ui_scale"] == 120
        assert cfg.config["panel_opacity"] == 88
        assert cfg.config["show_tooltips"] is False
        assert cfg.config["accent_color"] == "#4488ff"
        assert cfg.config["show_panel_titles"] is False
        assert cfg.config["zoom_speed"] == 150

    def test_backend_graphics_write_preserves_sibling_dimensions(self, make_dialog) -> None:
        dlg, cfg = make_dialog({"backend": {
            "graphics": "auto", "compute": "cuda",
            "decode": "nvdec", "interop": "egl",
        }})
        assert dlg._widgets["timeline_graphics_backend"].currentData() == "auto"
        _set_combo_by_data(dlg._widgets["timeline_graphics_backend"], "opengl")
        dlg._on_apply()
        assert cfg.config["backend"] == {
            "graphics": "opengl", "compute": "cuda",
            "decode": "nvdec", "interop": "egl",
        }

    def test_non_dict_backend_falls_back_to_auto_then_normalizes(self, make_dialog) -> None:
        dlg, cfg = make_dialog({"backend": "corrupt"})
        assert dlg._widgets["timeline_graphics_backend"].currentData() == "auto"
        _set_combo_by_data(dlg._widgets["timeline_graphics_backend"], "opengl")
        dlg._on_apply()
        assert isinstance(cfg.config["backend"], dict)
        assert cfg.config["backend"]["graphics"] == "opengl"

    def test_performance_round_trip(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        w = dlg._widgets
        w["gpu_acceleration"].setChecked(False)
        _set_combo_by_data(w["video_macos_vt_zero_copy"], "force")
        w["processing_threads"].setValue(8)
        _set_combo_by_data(w["pass_fusion_mode"], "aggressive")
        dlg._on_apply()
        assert cfg.config["gpu_acceleration"] is False
        assert cfg.config["video_macos_vt_zero_copy"] == "force"
        assert cfg.config["processing_threads"] == 8
        assert cfg.config["pass_fusion_mode"] == "aggressive"

    def test_video_playback_resolution_maps_labels_to_long_edge(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        res_combo = dlg._widgets["video_playback_resolution"]
        idx_720 = _combo_index_for_text(res_combo, "720p")
        assert idx_720 >= 0
        res_combo.setCurrentIndex(idx_720)
        dlg._on_apply()
        assert cfg.config["video_playback_resolution"] == 1280

        idx_original = _combo_index_for_text(res_combo, "Original")
        assert idx_original >= 0
        res_combo.setCurrentIndex(idx_original)
        dlg._on_apply()
        assert cfg.config["video_playback_resolution"] == 0


# ── File & Storage 页 ───────────────────────────────────────────


class TestFileStorageRoundTrip:

    def test_file_storage_round_trip(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        w = dlg._widgets
        w["default_open_directory"].setText("/open")
        w["auto_backup_interval"].setValue(30)
        w["backup_directory"].setText("/backup")
        w["max_backup_count"].setValue(20)
        w["cache_directory"].setText("/cache")
        w["cache_size_limit_gb"].setValue(40)
        w["cleanup_temp_on_exit"].setChecked(False)
        w["temp_file_max_age_days"].setValue(30)
        dlg._on_apply()
        assert cfg.config["default_open_directory"] == "/open"
        assert cfg.config["auto_backup_interval"] == 30
        assert cfg.config["backup_directory"] == "/backup"
        assert cfg.config["max_backup_count"] == 20
        assert cfg.config["cache_directory"] == "/cache"
        assert cfg.config["cache_size_limit_gb"] == 40
        assert cfg.config["cleanup_temp_on_exit"] is False
        assert cfg.config["temp_file_max_age_days"] == 30

    def test_render_cache_round_trip(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        w = dlg._widgets
        _set_combo_by_data(w["render_cache_auto_mode"], "smart")
        w["render_cache_auto_threshold"].setValue(12)
        _set_combo_by_data(w["render_cache_codec"], "dnxhr_sq")
        _set_combo_by_data(w["proxy_format"], "h264")
        w["render_cache_directory"].setText("/render_cache")
        _set_combo_by_data(w["render_cache_resolution_mode"], "half")
        w["render_cache_max_size_gb"].setValue(120)
        dlg._on_apply()
        assert cfg.config["render_cache_auto_mode"] == "smart"
        assert cfg.config["render_cache_auto_threshold"] == 12
        assert cfg.config["render_cache_codec"] == "dnxhr_sq"
        assert cfg.config["proxy_format"] == "h264"
        assert cfg.config["render_cache_directory"] == "/render_cache"
        assert cfg.config["render_cache_resolution_mode"] == "half"
        assert cfg.config["render_cache_max_size_gb"] == 120

    def test_cache_size_label_formats_total_of_both_caches(
            self, make_dialog, tmp_path) -> None:
        baked = tmp_path / "baked_layers"
        baked.mkdir(parents=True, exist_ok=True)
        (baked / "a.bin").write_bytes(b"\0" * 2048)
        images = tmp_path / "images_cache"
        images.mkdir(parents=True, exist_ok=True)
        (images / "b.bin").write_bytes(b"\0" * 512)
        dlg, _ = make_dialog()
        label = dlg._widgets["cache_size_label"]
        _wait_for_cache_label(label, "2.5 KB")


# ── Timeline Defaults 页 ────────────────────────────────────────


class TestTimelineDefaultsRoundTrip:

    INITIAL: dict = {
        "default_timeline_fps": "24",
        "default_timeline_width": 2560,
        "default_timeline_height": 1440,
        "default_timeline_sample_rate": 44100,
        "default_timeline_pixel_aspect_ratio": "1.0926",
        "default_timeline_field_order": "upper",
        "default_timeline_audio_track_type": "mono",
        "default_timeline_start_timecode": "01:00:00:00",
        "default_subtitle_clip_duration_frames": 144,
    }

    def test_timeline_defaults_load(self, make_dialog) -> None:
        dlg, _ = make_dialog(self.INITIAL)
        w = dlg._widgets
        assert w["default_timeline_fps"].currentData() == "24"
        assert w["default_timeline_width"].intValue() == 2560
        assert w["default_timeline_height"].intValue() == 1440
        assert w["default_timeline_sample_rate"].currentData() == 44100
        assert w["default_timeline_pixel_aspect_ratio"].currentData() == "1.0926"
        assert w["default_timeline_field_order"].currentData() == "upper"
        assert w["default_timeline_audio_track_type"].currentData() == "mono"
        assert w["default_timeline_start_timecode"].text() == "01:00:00:00"
        assert w["default_subtitle_clip_duration_frames"].intValue() == 144

    def test_timeline_defaults_round_trip(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        w = dlg._widgets
        _set_combo_by_data(w["default_timeline_fps"], "59.94")
        w["default_timeline_width"].setValue(3840)
        w["default_timeline_height"].setValue(2160)
        _set_combo_by_data(w["default_timeline_sample_rate"], 96000)
        _set_combo_by_data(w["default_timeline_pixel_aspect_ratio"], "1.3333")
        _set_combo_by_data(w["default_timeline_field_order"], "lower")
        _set_combo_by_data(w["default_timeline_audio_track_type"], "51")
        w["default_timeline_start_timecode"].setText("00:59:59:23")
        w["default_subtitle_clip_duration_frames"].setValue(600)
        dlg._on_apply()
        assert cfg.config["default_timeline_fps"] == "59.94"
        assert cfg.config["default_timeline_width"] == 3840
        assert cfg.config["default_timeline_height"] == 2160
        assert cfg.config["default_timeline_sample_rate"] == 96000
        assert cfg.config["default_timeline_pixel_aspect_ratio"] == "1.3333"
        assert cfg.config["default_timeline_field_order"] == "lower"
        assert cfg.config["default_timeline_audio_track_type"] == "51"
        assert cfg.config["default_timeline_start_timecode"] == "00:59:59:23"
        assert cfg.config["default_subtitle_clip_duration_frames"] == 600

    def test_resolution_preset_updates_dimensions(self, make_dialog) -> None:
        dlg, _ = make_dialog(self.INITIAL)
        res_combo = dlg._widgets["_timeline_res_combo"]
        # BUG-B3-03 修复后：加载 2560x1440 配置时下拉同步到匹配预设（2K QHD）
        assert res_combo.currentIndex() == 1
        res_combo.setCurrentIndex(3)  # 720p
        assert dlg._widgets["default_timeline_width"].intValue() == 1280
        assert dlg._widgets["default_timeline_height"].intValue() == 720
        res_combo.setCurrentIndex(0)  # 4K UHD (3840x2160)
        assert dlg._widgets["default_timeline_width"].intValue() == 3840
        assert dlg._widgets["default_timeline_height"].intValue() == 2160

    def test_custom_preset_leaves_dimensions_untouched(self, make_dialog) -> None:
        dlg, _ = make_dialog(self.INITIAL)
        dlg._widgets["default_timeline_width"].setValue(1234)
        res_combo = dlg._widgets["_timeline_res_combo"]
        res_combo.setCurrentIndex(4)  # Custom (0, 0) -> 不覆盖
        assert dlg._widgets["default_timeline_width"].intValue() == 1234

    def test_timeline_resolution_preset_combo_syncs_with_config(
            self, make_dialog) -> None:
        # BUG-B3-03 已修复：加载 1080p 配置时预设下拉按宽高反查同步到
        # "1080p (1920x1080)"，与 spinbox 一致。
        dlg, _ = make_dialog({
            "default_timeline_width": 1920,
            "default_timeline_height": 1080,
        })
        res_combo = dlg._widgets["_timeline_res_combo"]
        assert dlg._widgets["default_timeline_width"].intValue() == 1920
        assert res_combo.currentIndex() == 2
        assert res_combo.currentText().startswith("1080p")

    def test_timeline_resolution_preset_combo_custom_when_no_match(
            self, make_dialog) -> None:
        # BUG-B3-03 已修复：非预设尺寸落到末项 "Custom"，且不覆盖宽高。
        dlg, _ = make_dialog({
            "default_timeline_width": 1234,
            "default_timeline_height": 567,
        })
        res_combo = dlg._widgets["_timeline_res_combo"]
        assert res_combo.currentIndex() == res_combo.count() - 1
        assert res_combo.itemData(res_combo.currentIndex()) == (0, 0)
        assert dlg._widgets["default_timeline_width"].intValue() == 1234
        assert dlg._widgets["default_timeline_height"].intValue() == 567


# ── Advanced 页 ─────────────────────────────────────────────────


class TestAdvancedPageRoundTrip:

    def test_advanced_round_trip(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        w = dlg._widgets
        _set_combo_by_data(w["log_level"], "error")
        w["log_file_path"].setText("/tmp/topos.log")
        w["enable_file_logging"].setChecked(True)
        w["max_log_file_size_mb"].setValue(42)
        w["show_version_in_title"].setChecked(False)
        w["show_gpu_info"].setChecked(True)
        w["developer_mode"].setChecked(True)
        w["plugin_directory"].setText("/plug")
        w["auto_scan_plugins"].setChecked(False)
        dlg._on_apply()
        assert cfg.config["log_level"] == "error"
        assert cfg.config["log_file_path"] == "/tmp/topos.log"
        assert cfg.config["enable_file_logging"] is True
        assert cfg.config["max_log_file_size_mb"] == 42
        assert cfg.config["show_version_in_title"] is False
        assert cfg.config["show_gpu_info"] is True
        assert cfg.config["developer_mode"] is True
        assert cfg.config["plugin_directory"] == "/plug"
        assert cfg.config["auto_scan_plugins"] is False

    def test_shortcuts_round_trip(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        dlg._widgets["global_shortcuts"].setChecked(True)
        dlg._widgets["show_shortcut_conflicts"].setChecked(False)
        dlg._on_apply()
        assert cfg.config["global_shortcuts"] is True
        assert cfg.config["show_shortcut_conflicts"] is False


# ── Restore Defaults ────────────────────────────────────────────


class TestRestoreDefaults:

    NON_DEFAULT_INITIAL: dict = {
        "locale": "zh_CN",
        "auto_save_interval": 45,
        "max_recent_files": 20,
        "undo_history_limit": 200,
        "show_welcome_on_startup": False,
        "confirm_before_reset": False,
        "auto_load_last_project": False,
        "check_updates_on_startup": False,
        "ui_scale": 150,
        "panel_opacity": 80,
        "accent_color": "#9944ff",
        "show_tooltips": False,
        "show_panel_titles": False,
        "zoom_speed": 200,
        "gpu_acceleration": False,
        "video_macos_vt_zero_copy": "force",
        "pass_fusion_mode": "off",
        "video_playback_resolution": 3840,
        "global_shortcuts": True,
        "show_shortcut_conflicts": False,
        "default_open_directory": "/x",
        "auto_backup_interval": 90,
        "backup_directory": "/b",
        "max_backup_count": 40,
        "cache_directory": "/c",
        "cache_size_limit_gb": 90,
        "cleanup_temp_on_exit": False,
        "temp_file_max_age_days": 300,
        "render_cache_auto_mode": "smart",
        "render_cache_codec": "dnxhr_sq",
        "proxy_format": "h264",
        "render_cache_resolution_mode": "quarter",
        "render_cache_max_size_gb": 400,
        "log_level": "debug",
        "log_file_path": "/l",
        "enable_file_logging": True,
        "max_log_file_size_mb": 90,
        "show_version_in_title": False,
        "show_gpu_info": True,
        "developer_mode": True,
        "plugin_directory": "/p",
        "auto_scan_plugins": False,
        "default_timeline_fps": "60",
        "default_timeline_width": 3840,
        "default_timeline_height": 2160,
        "default_timeline_sample_rate": 96000,
        "default_timeline_pixel_aspect_ratio": "1.3333",
        "default_timeline_field_order": "upper",
        "default_timeline_audio_track_type": "mono",
        "default_timeline_start_timecode": "01:02:03:04",
        "default_subtitle_clip_duration_frames": 600,
    }

    def test_restore_defaults_resets_widgets_to_factory_values(
            self, make_dialog, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
        _confirm_with(monkeypatch, QMessageBox.StandardButton.Yes)
        dlg, _ = make_dialog(self.NON_DEFAULT_INITIAL)
        dlg._on_restore_defaults()
        d = ConfigManager.DEFAULT_CONFIG
        w = dlg._widgets

        combo_expected = {
            "locale": d["locale"],
            "ui_scale": d["ui_scale"],
            "accent_color": d["accent_color"],
            "video_macos_vt_zero_copy": d["video_macos_vt_zero_copy"],
            "render_cache_auto_mode": d["render_cache_auto_mode"],
            "render_cache_codec": d["render_cache_codec"],
            "proxy_format": d["proxy_format"],
            "render_cache_resolution_mode": d["render_cache_resolution_mode"],
            "log_level": d["log_level"],
            "default_timeline_fps": d["default_timeline_fps"],
            "default_timeline_sample_rate": d["default_timeline_sample_rate"],
            "default_timeline_pixel_aspect_ratio": d["default_timeline_pixel_aspect_ratio"],
            "default_timeline_field_order": d["default_timeline_field_order"],
            "default_timeline_audio_track_type": d["default_timeline_audio_track_type"],
            "timeline_graphics_backend": d["backend"]["graphics"],
            "work_mode": d["work_mode"],
        }
        for key, expected in combo_expected.items():
            assert w[key].currentData() == expected, key

        value_expected = {
            "auto_save_interval": d["auto_save_interval"],
            "max_recent_files": d["max_recent_files"],
            "undo_history_limit": d["undo_history_limit"],
            "panel_opacity": d["panel_opacity"],
            "zoom_speed": d["zoom_speed"],
            "processing_threads": d["processing_threads"],
            "auto_backup_interval": d["auto_backup_interval"],
            "max_backup_count": d["max_backup_count"],
            "cache_size_limit_gb": d["cache_size_limit_gb"],
            "temp_file_max_age_days": d["temp_file_max_age_days"],
            "render_cache_max_size_gb": d["render_cache_max_size_gb"],
            "max_log_file_size_mb": d["max_log_file_size_mb"],
            "default_timeline_width": d["default_timeline_width"],
            "default_timeline_height": d["default_timeline_height"],
            "default_subtitle_clip_duration_frames": d["default_subtitle_clip_duration_frames"],
        }
        for key, expected in value_expected.items():
            widget = w[key]
            value = widget.intValue() if hasattr(widget, "intValue") else widget.value()
            assert value == expected, key

        checked_expected = {
            "show_welcome_on_startup": d["show_welcome_on_startup"],
            "confirm_before_reset": d["confirm_before_reset"],
            "auto_load_last_project": d["auto_load_last_project"],
            "check_updates_on_startup": d["check_updates_on_startup"],
            "show_tooltips": d["show_tooltips"],
            "show_panel_titles": d["show_panel_titles"],
            "gpu_acceleration": d["gpu_acceleration"],
            "global_shortcuts": d["global_shortcuts"],
            "show_shortcut_conflicts": d["show_shortcut_conflicts"],
            "cleanup_temp_on_exit": d["cleanup_temp_on_exit"],
            "enable_file_logging": d["enable_file_logging"],
            "show_version_in_title": d["show_version_in_title"],
            "show_gpu_info": d["show_gpu_info"],
            "developer_mode": d["developer_mode"],
            "auto_scan_plugins": d["auto_scan_plugins"],
        }
        for key, expected in checked_expected.items():
            assert w[key].isChecked() is expected, key

        text_expected = {
            "default_open_directory": d["default_open_directory"],
            "backup_directory": d["backup_directory"],
            "cache_directory": d["cache_directory"],
            "render_cache_directory": d["render_cache_directory"],
            "log_file_path": d["log_file_path"],
            "plugin_directory": d["plugin_directory"],
            "default_timeline_start_timecode": d["default_timeline_start_timecode"],
        }
        for key, expected in text_expected.items():
            assert w[key].text() == expected, key

        res_combo = w["video_playback_resolution"]
        assert PreferencesDialog.VIDEO_RESOLUTIONS[res_combo.currentText()] == \
            d["video_playback_resolution"]

    def test_restore_defaults_declined_keeps_user_values(
            self, make_dialog, monkeypatch) -> None:
        _confirm_with(monkeypatch, QMessageBox.StandardButton.No)
        dlg, _ = make_dialog(self.NON_DEFAULT_INITIAL)
        dlg._on_restore_defaults()
        w = dlg._widgets
        assert w["locale"].currentData() == "zh_CN"
        assert w["ui_scale"].currentData() == 150
        assert w["panel_opacity"].value() == 80
        assert w["log_level"].currentData() == "debug"
        assert w["developer_mode"].isChecked() is True

    def test_restore_defaults_blocks_preview_then_emits_single_batch(
            self, make_dialog, monkeypatch) -> None:
        _confirm_with(monkeypatch, QMessageBox.StandardButton.Yes)
        dlg, _ = make_dialog(self.NON_DEFAULT_INITIAL)
        events: list[tuple[str, Any]] = []
        dlg.appearance_preview_changed.connect(
            lambda key, value: events.append((key, value)))
        dlg._on_restore_defaults()
        assert len(events) == 3
        assert {key for key, _v in events} == set(PreferencesDialog.PREVIEWABLE_KEYS)
        assert ("ui_scale", ConfigManager.DEFAULT_CONFIG["ui_scale"]) in events
        assert ("panel_opacity", ConfigManager.DEFAULT_CONFIG["panel_opacity"]) in events

    def test_restore_defaults_resets_smart_threshold(
            self, make_dialog, monkeypatch) -> None:
        # BUG-B3-02 已修复：DEFAULT_CONFIG 补了 render_cache_auto_threshold=6，
        # _on_restore_defaults 也重置该阈值。
        _confirm_with(monkeypatch, QMessageBox.StandardButton.Yes)
        dlg, _ = make_dialog({"render_cache_auto_threshold": 21})
        dlg._on_restore_defaults()
        assert dlg._widgets["render_cache_auto_threshold"].value() == 6

    def test_restore_defaults_resets_pass_fusion_mode(
            self, make_dialog, monkeypatch) -> None:
        # BUG-B3-04 已修复：_on_restore_defaults 补了 pass_fusion_mode 重置，
        # DEFAULT_CONFIG 默认键为 "conservative"。
        _confirm_with(monkeypatch, QMessageBox.StandardButton.Yes)
        dlg, _ = make_dialog({"pass_fusion_mode": "off"})
        dlg._on_restore_defaults()
        assert dlg._widgets["pass_fusion_mode"].currentData() == "conservative"


# ── Apply / Cancel / Save 语义 ──────────────────────────────────


class TestApplyCancelSemantics:

    def test_apply_keeps_dialog_open_and_marks_applied(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        outcomes: list[str] = []
        dlg.accept = lambda: outcomes.append("accept")  # type: ignore[method-assign]
        dlg.reject = lambda: outcomes.append("reject")  # type: ignore[method-assign]
        dlg._on_apply()
        assert outcomes == []
        assert dlg._applied is True

    def test_save_applies_and_accepts(self, make_dialog, monkeypatch) -> None:
        monkeypatch.setattr(PreferencesDialog, "_show_restart_prompt",
                            lambda self: None)
        dlg, cfg = make_dialog()
        outcomes: list[str] = []
        dlg.accept = lambda: outcomes.append("accept")  # type: ignore[method-assign]
        dlg._on_save()
        assert outcomes == ["accept"]
        assert cfg.save_count == 1

    def test_cancel_without_apply_rejects_and_keeps_config_clean(
            self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        dlg._widgets["auto_save_interval"].setValue(44)
        dlg._on_cancel()
        assert dlg.result() == QDialog.DialogCode.Rejected
        assert cfg.set_calls == []
        assert cfg.save_count == 0

    def test_cancel_after_apply_restores_config_snapshot(self, make_dialog) -> None:
        dlg, cfg = make_dialog({"panel_opacity": 90})
        dlg._widgets["panel_opacity"].setValue(100)
        _set_combo_by_data(dlg._widgets["timeline_graphics_backend"], "opengl")
        dlg._on_apply()
        assert cfg.config["panel_opacity"] == 100
        assert cfg.config["backend"]["graphics"] == "opengl"
        dlg._on_cancel()
        assert dlg.result() == QDialog.DialogCode.Rejected
        assert cfg.config["panel_opacity"] == 90
        assert cfg.config["backend"]["graphics"] == "auto"
        assert cfg.save_count == 2  # Apply 落盘一次 + Cancel 回滚一次

    def test_cancel_rolls_back_appearance_preview_widgets(self, make_dialog) -> None:
        dlg, _ = make_dialog({
            "ui_scale": 100, "panel_opacity": 90, "accent_color": "#4488ff",
        })
        _set_combo_by_data(dlg._widgets["ui_scale"], 130)
        dlg._widgets["panel_opacity"].setValue(100)
        _set_combo_by_data(dlg._widgets["accent_color"], "#00aa99")
        dlg._on_cancel()
        assert dlg._widgets["ui_scale"].currentData() == 100
        assert dlg._widgets["panel_opacity"].value() == 90
        assert dlg._widgets["accent_color"].currentData() == "#4488ff"

    def test_escape_key_triggers_cancel(self, make_dialog) -> None:
        dlg, cfg = make_dialog()
        dlg.keyPressEvent(SimpleNamespace(key=lambda: Qt.Key.Key_Escape))
        assert dlg.result() == QDialog.DialogCode.Rejected
        assert cfg.save_count == 0


# ── 重启标记 ────────────────────────────────────────────────────


class TestRestartFlags:

    def test_save_without_changes_needs_no_restart(self, make_dialog, monkeypatch) -> None:
        monkeypatch.setattr(PreferencesDialog, "_show_restart_prompt",
                            lambda self: None)
        dlg, _ = make_dialog()
        dlg._on_save()
        assert dlg.needs_restart() is False

    def test_locale_change_requests_restart(self, make_dialog, monkeypatch) -> None:
        prompted: list[bool] = []

        def _fake_prompt(self: PreferencesDialog) -> None:
            prompted.append(True)

        monkeypatch.setattr(PreferencesDialog, "_show_restart_prompt", _fake_prompt)
        dlg, cfg = make_dialog({"locale": "en_US"})
        _set_combo_by_data(dlg._widgets["locale"], "zh_CN")
        dlg._on_save()
        assert dlg.needs_restart() is True
        assert prompted == [True]
        assert cfg.config["locale"] == "zh_CN"

    def test_work_mode_change_requests_restart(self, make_dialog, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
        monkeypatch.setattr(PreferencesDialog, "_show_restart_prompt",
                            lambda self: None)
        dlg, _ = make_dialog({"work_mode": "video"})
        _set_combo_by_data(dlg._widgets["work_mode"], "image")
        dlg._on_save()
        assert dlg.needs_restart() is True


# ── 非法值校验与回退 ────────────────────────────────────────────


class TestInvalidValueFallbacks:

    GARBAGE: dict = {
        "locale": "xx_XX",
        "ui_scale": 137,
        "accent_color": "#nope",
        "log_level": "verbose",
        "video_macos_vt_zero_copy": "sometimes",
        "pass_fusion_mode": "warp",
        "render_cache_auto_mode": "always",
        "render_cache_codec": "prores_4444",
        "proxy_format": "av1",
        "render_cache_resolution_mode": "tenth",
        "default_timeline_fps": "17",
        "default_timeline_sample_rate": 8000,
        "default_timeline_pixel_aspect_ratio": "2.0",
        "default_timeline_field_order": "mixed",
        "default_timeline_audio_track_type": "quad",
    }

    def test_unknown_enum_values_fall_back_to_first_choice(self, make_dialog) -> None:
        dlg, _ = make_dialog(self.GARBAGE)
        w = dlg._widgets
        assert w["locale"].currentData() == "en_US"
        assert w["ui_scale"].currentData() == 90
        assert w["accent_color"].currentData() == "#ff8800"
        assert w["log_level"].currentData() == "debug"
        assert w["video_macos_vt_zero_copy"].currentData() == "auto"
        assert w["pass_fusion_mode"].currentData() == "conservative"
        assert w["render_cache_auto_mode"].currentData() == "off"
        assert w["render_cache_codec"].currentData() == "topos_proxy"
        assert w["proxy_format"].currentData() == "topos_proxy"
        assert w["render_cache_resolution_mode"].currentData() == "timeline"
        assert w["default_timeline_fps"].currentData() == "23.976"
        assert w["default_timeline_sample_rate"].currentData() == 44100
        assert w["default_timeline_pixel_aspect_ratio"].currentData() == "1.0"
        assert w["default_timeline_field_order"].currentData() == "progressive"
        assert w["default_timeline_audio_track_type"].currentData() == "mono"

    def test_apply_normalizes_garbage_enums_into_valid_domain(self, make_dialog) -> None:
        dlg, cfg = make_dialog(self.GARBAGE)
        dlg._on_apply()
        assert cfg.config["locale"] == "en_US"
        assert cfg.config["render_cache_auto_mode"] == "off"
        assert cfg.config["proxy_format"] == "topos_proxy"
        assert cfg.config["log_level"] == "debug"

    def test_out_of_range_numbers_are_clamped(self, make_dialog) -> None:
        dlg, _ = make_dialog({
            "auto_save_interval": 999,
            "max_recent_files": 1,
            "undo_history_limit": 9999,
            "panel_opacity": 10,
            "zoom_speed": 999,
            "processing_threads": -5,
        })
        w = dlg._widgets
        assert w["auto_save_interval"].intValue() == 60
        assert w["max_recent_files"].intValue() == 5
        assert w["undo_history_limit"].intValue() == 200
        assert w["panel_opacity"].value() == 80
        assert w["zoom_speed"].value() == 200
        assert w["processing_threads"].intValue() == 0

    def test_none_smart_threshold_falls_back_to_six(self, make_dialog) -> None:
        dlg, _ = make_dialog({"render_cache_auto_threshold": None})
        assert dlg._widgets["render_cache_auto_threshold"].value() == 6


# ── 外观即时预览 ────────────────────────────────────────────────


class TestPreviewMechanism:

    def test_ui_scale_change_emits_preview_signal(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        events: list[tuple[str, Any]] = []
        dlg.appearance_preview_changed.connect(
            lambda key, value: events.append((key, value)))
        _set_combo_by_data(dlg._widgets["ui_scale"], 120)
        assert events == [("ui_scale", 120)]

    def test_opacity_and_accent_changes_emit_preview_signals(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        events: list[tuple[str, Any]] = []
        dlg.appearance_preview_changed.connect(
            lambda key, value: events.append((key, value)))
        dlg._widgets["panel_opacity"].setValue(88)
        _set_combo_by_data(dlg._widgets["accent_color"], "#00aa99")
        assert ("panel_opacity", 88) in events
        assert ("accent_color", "#00aa99") in events

    def test_preview_block_suppresses_emissions(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        events: list[tuple[str, Any]] = []
        dlg.appearance_preview_changed.connect(
            lambda key, value: events.append((key, value)))
        dlg._preview_blocked = True
        dlg._widgets["panel_opacity"].setValue(87)
        assert events == []
        dlg._preview_blocked = False
        dlg._widgets["panel_opacity"].setValue(86)
        assert events == [("panel_opacity", 86)]

    def test_batch_preview_emits_all_previewable_keys(self, make_dialog) -> None:
        dlg, _ = make_dialog({"ui_scale": 110, "panel_opacity": 95,
                              "accent_color": "#4488ff"})
        events: list[tuple[str, Any]] = []
        dlg.appearance_preview_changed.connect(
            lambda key, value: events.append((key, value)))
        dlg._emit_batch_preview()
        assert {key for key, _v in events} == set(PreferencesDialog.PREVIEWABLE_KEYS)
        assert dict(events) == {
            "ui_scale": 110, "panel_opacity": 95, "accent_color": "#4488ff",
        }

    def test_cancel_emits_batch_rollback_preview(self, make_dialog) -> None:
        dlg, _ = make_dialog({"ui_scale": 100, "panel_opacity": 90,
                              "accent_color": "#ff8800"})
        events: list[tuple[str, Any]] = []
        dlg.appearance_preview_changed.connect(
            lambda key, value: events.append((key, value)))
        _set_combo_by_data(dlg._widgets["ui_scale"], 140)
        dlg._on_cancel()
        rollback = dict(events[-3:])
        assert rollback == {
            "ui_scale": 100, "panel_opacity": 90, "accent_color": "#ff8800",
        }


# ── 导航与页面注册 ──────────────────────────────────────────────


class TestNavigationAndRegistry:

    def test_registry_orders_general_first_with_app_pages(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        assert _page_ids(dlg) == [
            "app.general", "app.appearance", "app.performance",
            "app.timeline_defaults", "app.shortcuts", "app.file_storage",
            "app.advanced",
        ]
        assert dlg._category_list.count() == 7
        assert dlg._pages.count() == 7

    def test_category_switch_updates_stacked_pages(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        assert dlg._pages.currentIndex() == 0
        target = _page_ids(dlg).index("app.appearance")
        dlg._category_list.setCurrentRow(target)
        assert dlg._pages.currentIndex() == target

    def test_switching_to_file_storage_refreshes_cache_label(
            self, make_dialog, tmp_path) -> None:
        dlg, _ = make_dialog()
        # 先等构造期扫描（页面装载后触发）落定：进行中的扫描会让
        # _cache_scan_in_progress 去重吞掉切页触发的新扫描
        label = dlg._widgets["cache_size_label"]
        _wait_cache_label_settled(label)
        baked = tmp_path / "baked_layers"
        baked.mkdir(parents=True, exist_ok=True)
        (baked / "late.bin").write_bytes(b"\0" * 1024)
        assert not label.text().endswith("1.0 KB")
        row = _page_ids(dlg).index("app.file_storage")
        dlg._category_list.setCurrentRow(row)
        _wait_for_cache_label(label, "1.0 KB")

    def test_select_page_selects_matching_row(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        dlg.select_page("app.appearance")
        row = _page_ids(dlg).index("app.appearance")
        assert dlg._category_list.currentRow() == row
        assert dlg._pages.currentIndex() == row

    def test_select_page_unknown_id_keeps_selection(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        dlg.select_page("app.appearance")
        before = dlg._category_list.currentRow()
        dlg.select_page("does.not.exist")
        assert dlg._category_list.currentRow() == before

    def test_image_mode_hides_video_only_pages(self, make_dialog, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
        video_page = _FakeModulePage("edit.video_playback")
        dlg, _ = make_dialog({"work_mode": "image"}, module_pages=[video_page])
        ids = _page_ids(dlg)
        assert "app.timeline_defaults" not in ids
        assert "edit.video_playback" not in ids
        assert dlg._pages.count() == len(ids)
        # 页面对象仍被跟踪并参与 load/apply 委托
        assert "edit.video_playback" in dlg._module_pages
        # Timeline 页控件在图片模式下不存在
        assert dlg._widgets.get("default_timeline_width") is None


# ── 模块偏好页委托 ──────────────────────────────────────────────


class TestModulePageDelegation:

    def test_module_page_receives_load_apply_and_default_hooks(
            self, make_dialog, monkeypatch) -> None:
        _confirm_with(monkeypatch, QMessageBox.StandardButton.Yes)
        page = _FakeModulePage("color.processing")
        dlg, cfg = make_dialog(module_pages=[page])
        assert page.created is True
        assert page.loads == [cfg.config]
        dlg._on_apply()
        assert page.applies == 1
        dlg._on_restore_defaults()
        assert page.default_restores == [ConfigManager.DEFAULT_CONFIG]

    def test_unknown_module_page_delegated_but_not_listed(self, make_dialog) -> None:
        page = _FakeModulePage("mystery.page")
        dlg, _ = make_dialog(module_pages=[page])
        assert "mystery.page" not in _page_ids(dlg)
        assert "mystery.page" in dlg._module_pages
        assert page.loads, "未注册的模块页仍应收到 load 委托"

    def test_module_pages_filtered_by_enabled_features(self, make_dialog) -> None:
        flags = SimpleNamespace(get_enabled_features=lambda: {"color"})
        color_page = _FakeModulePage("color.processing")
        deliver_page = _FakeModulePage("deliver.export")
        dlg, _ = make_dialog(feature_flags=flags,
                             module_pages=[color_page, deliver_page])
        ids = _page_ids(dlg)
        assert "color.processing" in ids
        assert "deliver.export" not in ids
        assert dlg._pages.count() == len(ids)
        # 过滤只影响展示，不影响委托
        assert set(dlg._module_pages) == {"color.processing", "deliver.export"}


# ── file_explorer 插件列表 ──────────────────────────────────────


class TestFileExplorerPluginSync:

    def test_apply_without_refresh_preserves_fe_disabled_plugins(
            self, make_dialog) -> None:
        # BUG-B3-01 / AUD-03900 已修复：对话框构造时不刷新插件列表，用户
        # 未点 Advanced > Refresh 就按 Apply/Save 时，_apply_settings 必须
        # 跳过对 file_explorer config 的 disabled_plugins 写回（列表无真实
        # 条目 = 无数据可写），FE 侧停用状态保持原值，不被清空。
        dlg, _cfg = make_dialog()
        fe = _FakeFeModule(disabled=["fe.alpha", "fe.beta"])
        dlg._fe_plugin_bridge = lambda: fe  # type: ignore[method-assign]
        assert dlg._plugin_list.count() == 0
        dlg._on_apply()
        assert fe.saved is None  # 未刷新：不得写回 FE config
        assert fe.conf["disabled_plugins"] == ["fe.alpha", "fe.beta"]
        assert dlg._disabled_plugins == []

    def test_apply_after_refresh_writes_unchecked_entries(self, make_dialog) -> None:
        dlg, _cfg = make_dialog()
        fe = _FakeFeModule(
            disabled=["fe.alpha"],
            plugins=[
                {"name": "fe.alpha", "info": {"version": "1.0", "description": "a"}},
                {"name": "fe.gamma", "info": {}},
            ],
        )
        dlg._fe_plugin_bridge = lambda: fe  # type: ignore[method-assign]
        dlg._refresh_plugins()
        assert dlg._plugin_list.count() == 2
        for i in range(dlg._plugin_list.count()):
            item = dlg._plugin_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == "fe.gamma":
                item.setCheckState(Qt.CheckState.Unchecked)
        dlg._on_apply()
        assert fe.saved is not None
        assert fe.saved["disabled_plugins"] == ["fe.alpha", "fe.gamma"]

    def test_refresh_without_bridge_shows_disabled_placeholder(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        dlg._fe_plugin_bridge = lambda: None  # type: ignore[method-assign]
        dlg._refresh_plugins()
        assert dlg._plugin_list.count() == 1
        item = dlg._plugin_list.item(0)
        assert not (item.flags() & Qt.ItemFlag.ItemIsEnabled)

    def test_refresh_populates_check_states_from_fe_config(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        fe = _FakeFeModule(
            disabled=["fe.alpha"],
            plugins=[{"name": "fe.alpha", "info": {"version": "2.0"}},
                     {"name": "fe.gamma", "info": {}}],
        )
        dlg._fe_plugin_bridge = lambda: fe  # type: ignore[method-assign]
        dlg._refresh_plugins()
        states = {
            dlg._plugin_list.item(i).data(Qt.ItemDataRole.UserRole):
                dlg._plugin_list.item(i).checkState()
            for i in range(dlg._plugin_list.count())
        }
        assert states == {
            "fe.alpha": Qt.CheckState.Unchecked,
            "fe.gamma": Qt.CheckState.Checked,
        }

    def test_fe_disabled_plugins_empty_without_bridge(self, make_dialog) -> None:
        dlg, _ = make_dialog()
        assert dlg._fe_disabled_plugins(None) == []
