"""CV-B08 验证：全屏/浮动/焦点作用域声明与既有行为保持。

门槛（计划 CV-B08）：F11 作用域迁移完成前保持原行为（toggle 视口顶层
window——dock 时主窗、浮动时浮动 dock 窗）；独立 viewer-only 全屏不冒
充；focus mode 进入暂存、退出只恢复自己改变的内容。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.features.color.core.color_viewer_capabilities import (
    ColorViewerCapabilityContext,
    color_capability_by_action,
    evaluate_color_viewer_capabilities,
)
from src.features.color.ui.canvas import ViewportMenuBar


def _caps():
    return color_capability_by_action(
        evaluate_color_viewer_capabilities(
            ColorViewerCapabilityContext()))


def test_fullscreen_scope_declarations_are_honest() -> None:
    caps = _caps()
    # 既有 F11：toggle 视口顶层 window（真实行为）。
    assert caps["view.fullscreen"].supported is True
    # dock 浮动是真实能力（Qt dock 浮动，身份不变）。
    assert caps["view.dock_float"].supported is True
    # 独立 viewer-only 全屏作用域未设计——如实禁用并解释。
    viewer_only = caps["view.fullscreen.viewer_only"]
    assert viewer_only.supported is False
    assert "viewer-only fullscreen" in viewer_only.reason
    assert "F11 keeps toggling" in viewer_only.reason


def test_fullscreen_action_toggles_the_owner_top_level_window(qapp) -> None:
    """F11 原行为保持：window() 是谁就全屏谁（不预设主窗）。"""
    calls: list[str] = []
    fake_window = SimpleNamespace(
        isFullScreen=lambda: False,
        showFullScreen=lambda: calls.append("fullscreen"),
        showNormal=lambda: calls.append("normal"),
    )
    toolbar = ViewportMenuBar()
    try:
        toolbar.window = lambda: fake_window  # 顶层归属由宿主决定
        toolbar._toggle_fullscreen()
        assert calls == ["fullscreen"]

        fake_window.isFullScreen = lambda: True
        toolbar._toggle_fullscreen()
        assert calls == ["fullscreen", "normal"]
    finally:
        toolbar.deleteLater()


def test_focus_mode_stashes_and_restores_only_its_own_changes() -> None:
    from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin

    class _FakeDock:
        def __init__(self, name: str, visible: bool) -> None:
            self.name = name
            self._visible = visible
            self.requested: list[bool] = []

        def isVisible(self) -> bool:
            return self._visible

        def setVisible(self, visible: bool) -> None:
            self._visible = visible
            self.requested.append(visible)

    viewport = _FakeDock("viewport", True)
    panels = [_FakeDock("a", True), _FakeDock("b", False),
              _FakeDock("c", True)]
    host = MenuToolbarMixin.__new__(MenuToolbarMixin)
    host.all_docks = [viewport] + panels
    host.dock_viewport = viewport

    host.toggle_focus_mode()  # 进入：只隐藏 viewport 以外的可见 dock
    assert viewport._visible is True
    assert [d._visible for d in panels] == [False, False, False]

    host.toggle_focus_mode()  # 退出：恢复进入前的原状
    assert [d._visible for d in panels] == [True, False, True]
    assert viewport._visible is True
    # 恢复的正是暂存的原状：b（原本隐藏）从未被写成 True。
    assert True not in panels[1].requested
