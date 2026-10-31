"""E11: 全局 Undo/Redo 路由。

审计实测：全局 Edit 菜单的 Undo/Redo 只路由图像引擎/Composite；时间线
编辑后菜单 Undo 仍 disabled，焦点移走后也没有全局撤销可用。

回归锁定：
- Edit 页激活且时间线栈有历史 → undo/redo 驱动 TimelineView；
- 焦点在媒体池内 → 驱动媒体池命令栈；
- 非 Edit 页 → 维持既有引擎路由（不回归）；
- update_undo_redo_actions 按 Edit 页命令栈更新菜单 enabled 与 label。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.app.controllers.edit_controller import EditControllerMixin
from src.app.controllers.file_controller import FileOpsMixin
from src.app.controllers.layer_controller import LayerOpsMixin

_qapp = QApplication.instance() or QApplication([])


class _FocusHost(QWidget):
    """可持有焦点的替身容器（模拟媒体池 dock / 时间线面板）。"""


class _Host(EditControllerMixin, LayerOpsMixin, FileOpsMixin):
    """MainWindow 的最小混入替身（undo/redo 路由在 LayerOpsMixin、
    菜单状态在 FileOpsMixin）。"""


def _make_controller(page: str, edit_available: bool = True):
    ctrl = _Host.__new__(_Host)
    ctrl.current_page = page
    ctrl._edit_available = edit_available
    ctrl.media_pool_panel = None
    ctrl.timeline_panel = None
    return ctrl


@pytest.fixture
def focus_host():
    host = _FocusHost()
    host.show()
    _qapp.processEvents()
    yield host
    host.close()
    _qapp.processEvents()


class TestUndoRouting:
    def test_timeline_stack_driven_on_edit_page(self, focus_host):
        ctrl = _make_controller("Edit")
        view = SimpleNamespace(
            can_undo=lambda: True, can_redo=lambda: False,
            undo=lambda: None, redo=lambda: None)
        ctrl.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
        assert ctrl._edit_undo_redo_target() is view

    def test_media_pool_stack_driven_when_focused_inside(
        self, focus_host, monkeypatch,
    ):
        ctrl = _make_controller("Edit")
        # pool 必须是真 QWidget：焦点判定走 parentWidget() 链
        pool = QWidget(focus_host)
        pool.can_undo = lambda: True
        pool.can_redo = lambda: False
        pool.undo = lambda: None
        pool.redo = lambda: None
        child = QWidget(pool)
        ctrl.media_pool_panel = pool
        ctrl.timeline_panel = SimpleNamespace(get_timeline_view=lambda: None)
        # offscreen 下窗口不会真正激活，直接钉住 focusWidget 返回子控件
        from PyQt6.QtWidgets import QApplication
        monkeypatch.setattr(
            QApplication, "focusWidget", staticmethod(lambda: child))
        assert ctrl._edit_undo_redo_target() is pool

    def test_engine_route_on_color_page(self):
        ctrl = _make_controller("Color")
        view = SimpleNamespace(can_undo=lambda: True, can_redo=lambda: False)
        ctrl.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
        assert ctrl._edit_undo_redo_target() is None

    def test_no_route_when_stack_empty(self):
        ctrl = _make_controller("Edit")
        view = SimpleNamespace(can_undo=lambda: False, can_redo=lambda: False)
        ctrl.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
        assert ctrl._edit_undo_redo_target() is None

    def test_undo_and_redo_invoke_target(self):
        ctrl = _make_controller("Edit")
        ctrl.engine = SimpleNamespace(can_undo=lambda: False, can_redo=lambda: False)
        calls: list[str] = []
        view = SimpleNamespace(
            can_undo=lambda: True, can_redo=lambda: True,
            undo=lambda: calls.append("undo"),
            redo=lambda: calls.append("redo"))
        ctrl.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
        ctrl.update_undo_redo_actions = lambda: None
        ctrl.undo()
        ctrl.redo()
        assert calls == ["undo", "redo"]


class TestMenuStateUpdate:
    def test_menu_enabled_follows_timeline_stack(self):
        ctrl = _Host.__new__(_Host)
        ctrl.current_page = "Edit"
        ctrl._edit_available = True
        view = SimpleNamespace(
            can_undo=lambda: True, can_redo=lambda: False,
            _command_stack=SimpleNamespace(
                get_undo_label=lambda: "Split Clip",
                get_redo_label=lambda: None))
        ctrl.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
        ctrl.media_pool_panel = None
        ctrl.engine = SimpleNamespace(
            can_undo=lambda: False, can_redo=lambda: False)
        states: dict[str, bool] = {}
        texts: dict[str, str] = {}
        ctrl.undo_action = SimpleNamespace(setEnabled=lambda v: states.update(toolbar_undo=v))
        ctrl.redo_action = SimpleNamespace(setEnabled=lambda v: states.update(toolbar_redo=v))
        ctrl.undo_action_menu = SimpleNamespace(
            setEnabled=lambda v: states.update(menu_undo=v),
            setText=lambda t: texts.update(undo=t))
        ctrl.redo_action_menu = SimpleNamespace(
            setEnabled=lambda v: states.update(menu_redo=v),
            setText=lambda t: texts.update(redo=t))

        ctrl.update_undo_redo_actions()
        assert states["menu_undo"] is True
        assert states["menu_redo"] is False
        assert texts["undo"] == "Undo Split Clip"
