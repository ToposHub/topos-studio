"""Batch A 修复回归测试：AUD-03900（= BUG-B3-01）与 BUG-B4-01。

1. AUD-03900 [Critical]：PreferencesDialog 在插件列表未被 "Refresh Plugins"
   填充（列表为空或只有占位行）时，Apply/Save 不得把 file_explorer 自己的
   ``disabled_plugins`` 覆写为空——那会静默重新启用用户此前停用的全部 FE
   插件（用户配置数据丢失）。刷新且 FE 返回真实插件条目后，Apply 仍应把
   勾选状态写回 FE config。
2. BUG-B4-01 [Critical]：``DockLayoutMixin.setup_docks`` 创建的 5 个 Edit 页
   dock（Properties / Edit Effects / Captions / ADR Take Sheet / Multicam）
   必须注册进 ``all_docks``，否则浮动后拿不到可拖拽的 FloatingDockTitleBar、
   停靠尺寸也不持久化（与 Keyframes/Spline 当年的缺口同机制）。

详见 docs/audit/2026-09-28/batches/batch-039-app-ui.md、
docs/testcoverage/2026-09-28/BUGS-agent-B3.md、BUGS-agent-B4.md。
"""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMainWindow, QWidget

from src.app.controllers.dock_layout_controller import (
    DockLayoutMixin,
    FloatingDockTitleBar,
)
from src.shared.config.config_manager import ConfigManager
from src.app.ui.preferences_dialog import PreferencesDialog


# ── AUD-03900：file_explorer 停用插件列表 ───────────────────────


class _RecordingConfig:
    """ConfigManager 替身：仅内存读写（与补测文件同款最小接口）。"""

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


@pytest.fixture
def make_dialog(qapp, monkeypatch, tmp_path) -> Iterator[
        Callable[..., tuple[PreferencesDialog, _RecordingConfig]]]:
    """PreferencesDialog 工厂：hermetic 缓存目录；FE 桥默认切断。"""
    import src.shared.io.file_io as file_io
    monkeypatch.setattr(file_io.FileIO, "CACHE_DIR", str(tmp_path / "images_cache"))
    monkeypatch.setattr(file_io.FileIO, "BAKED_CACHE_DIR", str(tmp_path / "baked_layers"))

    created: list[PreferencesDialog] = []

    def _make(initial: Optional[dict] = None
              ) -> tuple[PreferencesDialog, _RecordingConfig]:
        cfg = _RecordingConfig(initial)
        dlg = PreferencesDialog(cfg)
        dlg._fe_plugin_bridge = lambda: None  # type: ignore[method-assign]
        created.append(dlg)
        return dlg, cfg

    yield _make
    for dlg in created:
        dlg.deleteLater()


class TestAud03900FeDisabledPluginsPreserved:

    def test_apply_without_refresh_skips_fe_write(self, make_dialog) -> None:
        """复现路径：打开偏好 → 直接 Apply。FE config 必须保持原样。"""
        dlg, _cfg = make_dialog()
        fe = _FakeFeModule(disabled=["fe.alpha", "fe.beta"])
        dlg._fe_plugin_bridge = lambda: fe  # type: ignore[method-assign]
        assert dlg._plugin_list.count() == 0  # 构造时不刷新

        dlg._on_apply()

        assert fe.saved is None, "未刷新插件列表时不得写回 FE config"
        assert fe.conf["disabled_plugins"] == ["fe.alpha", "fe.beta"]

    def test_save_without_refresh_skips_fe_write(self, make_dialog) -> None:
        """复现路径：打开偏好 → 直接 Save（确定关闭）同样不得清空。"""
        dlg, _cfg = make_dialog()
        fe = _FakeFeModule(disabled=["fe.alpha"])
        dlg._fe_plugin_bridge = lambda: fe  # type: ignore[method-assign]
        dlg._on_save()  # _on_save 会调 accept()；offscreen 下安全

        assert fe.saved is None
        assert fe.conf["disabled_plugins"] == ["fe.alpha"]

    def test_apply_after_placeholder_refresh_skips_fe_write(self, make_dialog) -> None:
        """刷新过但 FE 无插件（或 FE 运行时未起）→ 只有占位行，同样不写回。

        对应审计报告的加重因素：ensure_runtime() 之前 plugin_manager.plugins
        为空，若照写会把停用列表清空。"""
        dlg, _cfg = make_dialog()
        fe = _FakeFeModule(disabled=["fe.alpha"])  # plugins=[]
        dlg._fe_plugin_bridge = lambda: fe  # type: ignore[method-assign]
        dlg._refresh_plugins()
        assert dlg._plugin_list.count() == 1  # "No plugins found" 占位行

        dlg._on_apply()

        assert fe.saved is None
        assert fe.conf["disabled_plugins"] == ["fe.alpha"]

    def test_apply_after_real_refresh_still_writes(self, make_dialog) -> None:
        """正常路径不回退：刷新出真实插件后，Apply 写回勾选结果。"""
        dlg, _cfg = make_dialog()
        fe = _FakeFeModule(
            disabled=["fe.alpha"],
            plugins=[
                {"name": "fe.alpha", "info": {"version": "1.0"}},
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
        assert dlg._disabled_plugins == ["fe.alpha", "fe.gamma"]

    def test_apply_after_real_refresh_all_enabled_writes_empty(self, make_dialog) -> None:
        """列表有真实条目时，全勾选 → 写回空列表是明确的用户意图，允许。"""
        dlg, _cfg = make_dialog()
        fe = _FakeFeModule(
            disabled=["fe.alpha"],
            plugins=[{"name": "fe.alpha", "info": {}}],
        )
        dlg._fe_plugin_bridge = lambda: fe  # type: ignore[method-assign]
        dlg._refresh_plugins()
        for i in range(dlg._plugin_list.count()):
            dlg._plugin_list.item(i).setCheckState(Qt.CheckState.Checked)

        dlg._on_apply()

        assert fe.saved is not None
        assert fe.saved["disabled_plugins"] == []


# ── BUG-B4-01：5 个 Edit dock 注册进 all_docks ──────────────────


_EDIT_DOCK_FIXES: list[tuple[str, str]] = [
    ("dock_properties", "dockProperties"),
    ("dock_edit_effects", "dockEditEffects"),
    ("dock_caption_workspace", "dockCaptionWorkspace"),
    ("dock_adr_take_sheet", "dockAdrTakeSheet"),
    ("dock_multicam", "dockMulticam"),
]


def test_setup_docks_registers_the_five_edit_docks() -> None:
    """源码契约：5 个 dock 必须以条件 append 的形式进入 all_docks。"""
    import inspect
    src = inspect.getsource(DockLayoutMixin.setup_docks)
    literal = re.search(r"self\.all_docks\s*=\s*\[(.*?)\]", src, re.DOTALL)
    assert literal is not None, "setup_docks must construct the all_docks list"
    base = set(re.findall(r"self\.(dock_\w+)", literal.group(1)))
    appended = set(re.findall(r"self\.all_docks\.append\(self\.(dock_\w+)\)", src))
    for attr, _obj_name in _EDIT_DOCK_FIXES:
        assert attr in appended or attr in base, (
            f"{attr} must be registered into all_docks (BUG-B4-01)")
        assert f"if self.{attr}:" in src, (
            f"{attr} append must be guarded (conditionally created dock)")


class _MinimalDockHost(QMainWindow, DockLayoutMixin):
    """最小宿主：模拟修复后 setup_docks 的注册形态并保留一个反证 dock。"""

    def __init__(self) -> None:
        QMainWindow.__init__(self)
        self.setWindowTitle("AudFixDockHost")
        self._updating_titlebars = False
        self.all_docks: list = []
        # 修复后：5 个 Edit dock 全部进入 all_docks。
        for attr, obj_name in _EDIT_DOCK_FIXES:
            dock = _make_dock(self, obj_name)
            setattr(self, attr, dock)
            self.all_docks.append(dock)
        # 反证：模拟修复前未注册的 dock（不进 all_docks）。
        self.dock_untracked = _make_dock(self, "dockUntracked")


def _make_dock(parent: QMainWindow, obj_name: str):
    from PyQt6.QtWidgets import QDockWidget
    dock = QDockWidget(obj_name, parent)
    dock.setObjectName(obj_name)
    dock.setWidget(QWidget())
    parent.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    return dock


@pytest.fixture
def dock_host(qapp) -> Iterator[_MinimalDockHost]:
    h = _MinimalDockHost()
    yield h
    h.close()
    h.deleteLater()


def test_registered_edit_docks_get_floating_titlebar(dock_host) -> None:
    """行为侧：注册后浮动 dock 获得 FloatingDockTitleBar（可拖拽把手）。"""
    for attr, _obj_name in _EDIT_DOCK_FIXES:
        dock = getattr(dock_host, attr)
        dock.setFloating(True)
    dock_host._perform_update_all_titlebars()
    for attr, _obj_name in _EDIT_DOCK_FIXES:
        dock = getattr(dock_host, attr)
        assert isinstance(dock.titleBarWidget(), FloatingDockTitleBar), (
            f"{attr} 浮动后必须安装 FloatingDockTitleBar（BUG-B4-01）")


def test_unregistered_dock_keeps_legacy_invisible_titlebar(dock_host) -> None:
    """反证修复机制：不在 all_docks 的 dock 不会被升级（原 bug 机制）。"""
    dock_host.dock_untracked.setFloating(True)
    dock_host._perform_update_all_titlebars()
    assert not isinstance(dock_host.dock_untracked.titleBarWidget(),
                          FloatingDockTitleBar)


def test_registered_edit_docks_get_docked_size_persistence(dock_host) -> None:
    """行为侧：注册后 _save_all_docked_sizes 为它们写入尺寸键。"""

    class _SizeConfig:
        def __init__(self) -> None:
            self.data: dict = {}
            self.save_count = 0

        def get(self, key: str, default: Any = None) -> Any:
            return self.data.get(key, default)

        def set(self, key: str, value: Any, auto_save: bool = True) -> None:
            self.data[key] = value

        def save_config(self) -> None:
            self.save_count += 1

    dock_host.config_manager = _SizeConfig()
    dock_host._save_all_docked_sizes()
    for _attr, obj_name in _EDIT_DOCK_FIXES:
        assert f"dock_{obj_name}_docked_size" in dock_host.config_manager.data, (
            f"{obj_name} 的停靠尺寸必须被持久化（BUG-B4-01）")
