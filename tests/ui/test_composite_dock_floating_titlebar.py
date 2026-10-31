"""回归测试：浮动 dock 必须获得可拖拽的 FloatingDockTitleBar。

背景：Keyframes/Spline dock 曾不在 ``all_docks`` 里，导致浮动时
``_perform_update_all_titlebars`` / ``_sync_floating_docks`` 跳过它们，
浮动窗口顶部只剩 1px 隐形标题栏——既无法拖动整个面板，也无法拖回停靠。

本测试不启动完整应用，而是把 DockLayoutMixin 的两个标题栏维护方法
挂在一个最小 QMainWindow 上，验证机制本身正确。
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import inspect
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget

from src.app.controllers.dock_layout_controller import (
    DockLayoutMixin, FloatingDockTitleBar)
from src.shared.i18n.i18n import I18N


class _Host(QMainWindow, DockLayoutMixin):
    """最小宿主窗口：仅提供标题栏维护方法所需的状态。"""

    def __init__(self) -> None:
        super().__init__()
        self.all_docks: list[QDockWidget] = []
        self._floating_docks: set[QDockWidget] = set()
        self._updating_titlebars = False


def _make_dock(host: QMainWindow, name: str) -> QDockWidget:
    dock = QDockWidget(name, host)
    dock.setObjectName(name)
    dock.setWidget(QWidget())
    host.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    return dock


# ── 浮动时安装可拖拽标题栏 ──────────────────────────────────────────────

def test_floating_dock_gets_draggable_titlebar(qapp):
    host = _Host()
    kf = _make_dock(host, "dockCompositeKeyframes")
    host.all_docks.append(kf)

    kf.setFloating(True)
    host._perform_update_all_titlebars()

    tw = kf.titleBarWidget()
    assert isinstance(tw, FloatingDockTitleBar), (
        "浮动 dock 必须安装 FloatingDockTitleBar 才能拖拽移动/重新停靠"
    )
    assert tw.height() >= 20, "标题栏必须有真实高度作为拖拽把手"


def test_sync_floating_docks_covers_registered_dock(qapp):
    host = _Host()
    spline = _make_dock(host, "dockCompositeSpline")
    host.all_docks.append(spline)

    spline.setFloating(True)
    host._sync_floating_docks()

    assert spline in host._floating_docks
    assert isinstance(spline.titleBarWidget(), FloatingDockTitleBar)


def test_docked_dock_gets_1px_titlebar(qapp):
    host = _Host()
    kf = _make_dock(host, "dockCompositeKeyframes")
    host.all_docks.append(kf)

    # 先浮动再拖回停靠：标题栏必须从 FloatingDockTitleBar 换回 1px
    kf.setFloating(True)
    host._perform_update_all_titlebars()
    kf.setFloating(False)
    host._perform_update_all_titlebars()

    tw = kf.titleBarWidget()
    assert not isinstance(tw, FloatingDockTitleBar)
    assert tw is not None and tw.maximumHeight() == 1


def test_dock_missing_from_all_docks_is_skipped(qapp):
    """反证：不在 all_docks 的 dock 不会被升级——这正是原 bug 的机制。"""
    host = _Host()
    tracked = _make_dock(host, "dockTracked")
    untracked = _make_dock(host, "dockUntracked")
    host.all_docks.append(tracked)  # untracked 故意不注册

    tracked.setFloating(True)
    untracked.setFloating(True)
    host._perform_update_all_titlebars()

    assert isinstance(tracked.titleBarWidget(), FloatingDockTitleBar)
    assert not isinstance(untracked.titleBarWidget(), FloatingDockTitleBar)


def test_asset_dependency_panel_is_registered_as_a_dock():
    """The asset dependency widget must never remain a raw main-window child."""
    setup_source = inspect.getsource(DockLayoutMixin.setup_docks)
    layout_source = inspect.getsource(DockLayoutMixin.initialize_page_layouts)
    visibility_source = inspect.getsource(
        DockLayoutMixin._enforce_page_dock_visibility)

    assert 'docks.get("asset_dependency")' in setup_source
    assert '"asset_dependency"]' in setup_source
    assert "self.dock_composite_asset_dependency" in setup_source
    assert "self.all_docks.append(self.dock_composite_asset_dependency)" in setup_source
    assert 'docks.get("scope")' in setup_source
    assert "self.dock_composite_scope" in setup_source
    assert "self.all_docks.append(self.dock_composite_scope)" in setup_source
    assert "self.dock_composite_scope.setVisible(True)" in layout_source
    assert "self.dock_composite_asset_dependency.setVisible(False)" in layout_source
    assert "self.dock_composite_asset_dependency.setVisible(False)" in visibility_source


def test_composite_workflow_groups_keep_timeline_docks_independent():
    """Timeline views stay separate until Spline is folded into Keyframes."""
    setup_source = inspect.getsource(DockLayoutMixin.setup_docks)
    layout_source = inspect.getsource(DockLayoutMixin.initialize_page_layouts)

    assert "self.dock_composite_keyframes" in setup_source
    assert "self.dock_composite_spline" in setup_source
    assert "self.dock_composite_animation" not in setup_source
    assert "self.dock_composite_scene_outliner" in setup_source
    assert "self.dock_composite_material" in setup_source
    assert "self.dock_composite_import_report" in setup_source
    assert "self.dock_composite_asset_dependency" in setup_source
    assert "self.tabifyDockWidget(self.dock_composite_keyframes, self.dock_composite_spline)" not in setup_source
    assert "self.tabifyDockWidget(self.dock_composite_keyframes, self.dock_composite_spline)" not in layout_source
    assert "self.tabifyDockWidget(\n                        self.dock_composite_asset_dependency" in layout_source
    assert "self.tabifyDockWidget(\n                        self.dock_node_properties" in layout_source


def test_composite_panel_titles_are_localized():
    """Composite dock titles resolve in both supported application locales."""
    locale_dir = Path(__file__).resolve().parents[2] / "src/features/composite/locales"
    panel_titles = {
        "node_properties": ("Node Properties", "节点属性"),
        "performance": ("Performance", "性能"),
        "composite_scopes": ("Scopes", "示波器"),
        "keyframes": ("Keyframes", "关键帧"),
        "spline": ("Spline", "曲线"),
        "animation": ("Animation", "动画"),
        "import_report": ("Import Report", "导入报告"),
        "asset_dependency": ("Asset Dependencies", "资产依赖"),
        "scene_outliner": ("Scene Outliner", "场景大纲"),
        "material": ("Material", "材质"),
    }
    previous = I18N.get_current_locale()
    try:
        for locale, index in (("en_US", 0), ("zh_CN", 1)):
            assert I18N.load_locale(locale)
            I18N.load_module_translations(str(locale_dir))
            for key, expected in panel_titles.items():
                assert I18N.tr(f"panel.{key}") == expected[index]
    finally:
        I18N.load_locale(previous)
        I18N.load_module_translations(str(locale_dir))
