"""Regression tests for registration and persistence of Edit docks (BUG-B4-01)."""

from __future__ import annotations

import os
import re
from typing import Any, Iterator

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMainWindow, QWidget

from src.app.controllers.dock_layout_controller import (
    DockLayoutMixin,
    FloatingDockTitleBar,
)
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
