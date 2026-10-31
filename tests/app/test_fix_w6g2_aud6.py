"""W6-G2 修复回归测试（批 B，Major×S：BUG-B3-02/03/04 + BUG-B4-02/03）。

对 ``docs/testcoverage/2026-09-28/BUGS-agent-B3.md`` / ``BUGS-agent-B4.md``
中五条发现的根因修复做独立回归（锁定用例已在原补测文件中翻转，本文件
从"默认键完整性 / 清单完整性"角度再加一层防线）：

* BUG-B3-02: DEFAULT_CONFIG 必须含 ``render_cache_auto_threshold``（硬约束
  "DEFAULT_CONFIG 不得缺键"），且 Restore Defaults 把 smart 阈值重置回 6。
* BUG-B3-03: 加载/恢复默认后 Timeline 分辨率预设下拉与宽高配置同步，
  非预设尺寸落到 "Custom" 且不覆盖宽高。
* BUG-B4-02: ``_enforce_page_dock_visibility`` 的 edit_only 清单必须覆盖
  dock_caption_workspace（硬约束"dock 可见性强制清单不得漏项"）。
* BUG-B4-03: initialize_page_layouts 的隐藏清单必须覆盖 dock_deliver /
  dock_node_properties；enforce 必须有 dock_deliver 的页面强制分支。
"""

from __future__ import annotations

import inspect
import os
import re
from typing import Any, Callable, Iterator, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMainWindow, QMessageBox

import src.app.ui.preferences_dialog as preferences_dialog_module
from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.app.ui.preferences_dialog import PreferencesDialog
from src.shared.config.config_manager import ConfigManager


# ── PreferencesDialog 替身（同 test_preferences_dialog_coverage.py）──


class _RecordingConfig:
    """ConfigManager 替身：仅内存读写，记录 set()/save_config() 调用。"""

    def __init__(self, initial: Optional[dict] = None) -> None:
        self.DEFAULT_CONFIG = ConfigManager.DEFAULT_CONFIG
        self.config: dict = {**self.DEFAULT_CONFIG, **(initial or {})}
        self.save_count = 0

    def get(self, key: str) -> Any:
        return self.config.get(key)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.config[key] = value

    def save_config(self) -> None:
        self.save_count += 1


@pytest.fixture
def make_dialog(qapp, monkeypatch, tmp_path) -> Iterator[
        Callable[..., tuple[PreferencesDialog, _RecordingConfig]]]:
    """PreferencesDialog 工厂：hermetic 缓存目录 + 关闭真实 FE 插件桥。"""
    import src.shared.io.file_io as file_io
    monkeypatch.setattr(file_io.FileIO, "CACHE_DIR", str(tmp_path / "images_cache"))
    monkeypatch.setattr(file_io.FileIO, "BAKED_CACHE_DIR", str(tmp_path / "baked_layers"))
    monkeypatch.setattr(
        preferences_dialog_module.QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    created: list[PreferencesDialog] = []

    def _make(initial: Optional[dict] = None) -> tuple[PreferencesDialog, _RecordingConfig]:
        cfg = _RecordingConfig(initial)
        dlg = PreferencesDialog(cfg)
        dlg._fe_plugin_bridge = lambda: None  # type: ignore[method-assign]
        created.append(dlg)
        return dlg, cfg

    yield _make
    for dlg in created:
        dlg.deleteLater()


# ── BUG-B3-02 / BUG-B3-04: DEFAULT_CONFIG 默认键完整性 + Restore 覆盖 ──


def test_default_config_has_smart_threshold_and_fusion_mode_keys() -> None:
    """[BUG-B3-02/B3-04] 偏好面板管理的键必须在 DEFAULT_CONFIG 有默认值。"""
    assert ConfigManager.DEFAULT_CONFIG["render_cache_auto_threshold"] == 6
    assert ConfigManager.DEFAULT_CONFIG["pass_fusion_mode"] == "conservative"


def test_restore_defaults_resets_smart_threshold_and_fusion_mode(
        make_dialog) -> None:
    """[BUG-B3-02/B3-04] Restore Defaults 重置 smart 阈值与 Pass Fusion Mode。"""
    dlg, _ = make_dialog({
        "render_cache_auto_threshold": 21,
        "pass_fusion_mode": "aggressive",
    })
    dlg._on_restore_defaults()
    assert dlg._widgets["render_cache_auto_threshold"].value() == 6
    assert dlg._widgets["pass_fusion_mode"].currentData() == "conservative"


def test_restore_defaults_syncs_resolution_combo_to_default(
        make_dialog) -> None:
    """[BUG-B3-03] 恢复默认（1920x1080）后预设下拉同步到 1080p 项。"""
    dlg, _ = make_dialog({
        "default_timeline_width": 1280,
        "default_timeline_height": 720,
    })
    dlg._on_restore_defaults()
    combo = dlg._widgets["_timeline_res_combo"]
    assert combo.itemData(combo.currentIndex()) == (1920, 1080)
    assert dlg._widgets["default_timeline_width"].intValue() == 1920
    assert dlg._widgets["default_timeline_height"].intValue() == 1080


@pytest.mark.parametrize("width,height,expected", [
    (3840, 2160, (3840, 2160)),   # 4K UHD（首项，此前恒停此处）
    (2560, 1440, (2560, 1440)),   # 2K QHD
    (1920, 1080, (1920, 1080)),   # 1080p
    (1280, 720, (1280, 720)),     # 720p
    (999, 888, (0, 0)),           # 非预设 → Custom，宽高不被覆盖
])
def test_timeline_res_combo_syncs_on_load(
        make_dialog, width: int, height: int, expected: tuple) -> None:
    """[BUG-B3-03] 加载配置时预设下拉按宽高反查同步；无匹配选 Custom。"""
    dlg, _ = make_dialog({
        "default_timeline_width": width,
        "default_timeline_height": height,
    })
    combo = dlg._widgets["_timeline_res_combo"]
    assert combo.itemData(combo.currentIndex()) == expected
    # 同步过程不得篡改配置宽高（Custom 不覆盖、预设项写入相同值）。
    assert dlg._widgets["default_timeline_width"].intValue() == width
    assert dlg._widgets["default_timeline_height"].intValue() == height


# ── DockLayoutMixin 清单完整性（源码级防线）─────────────────────────


class _ListSourceFixture:
    """提取 _enforce_page_dock_visibility / initialize_page_layouts 源码。"""

    enforce_src: str = inspect.getsource(DockLayoutMixin._enforce_page_dock_visibility)
    init_src: str = inspect.getsource(DockLayoutMixin.initialize_page_layouts)


def test_enforce_edit_only_list_covers_caption_workspace() -> None:
    """[BUG-B4-02] edit_only 清单必须包含 dock_caption_workspace。"""
    literal = re.search(r"edit_only\s*=\s*\[(.*?)\]", _ListSourceFixture.enforce_src,
                        re.DOTALL)
    assert literal is not None, "edit_only list literal must exist"
    members = set(re.findall(r"self\.(dock_\w+)", literal.group(1)))
    assert "dock_caption_workspace" in members


def test_enforce_has_deliver_visibility_branch() -> None:
    """[BUG-B4-03] enforce 必须对非 Deliver 页隐藏 dock_deliver。"""
    assert re.search(r'current_page\s*!=\s*"Deliver"', _ListSourceFixture.enforce_src), (
        "_enforce_page_dock_visibility must scope dock_deliver to the Deliver page")


def test_initialize_hide_lists_cover_deliver_and_node_properties() -> None:
    """[BUG-B4-03] initialize 的隐藏清单必须覆盖 dock_deliver / dock_node_properties。"""
    lists = re.findall(r"for d in \[(.*?)\]:", _ListSourceFixture.init_src, re.DOTALL)
    assert lists, "initialize_page_layouts must keep per-page hide lists"
    for body in lists:
        members = set(re.findall(r"self\.(dock_\w+)", body))
        # 清单一旦收录了 Edit/Composite 家族成员，就必须同时覆盖这两个
        # 曾靠巧合隐藏的页面专属 dock。
        if "dock_properties" in members or "dock_composite" in members:
            assert "dock_deliver" in members, (
                f"hide list missing dock_deliver: {sorted(members)}")
            assert "dock_node_properties" in members, (
                f"hide list missing dock_node_properties: {sorted(members)}")
