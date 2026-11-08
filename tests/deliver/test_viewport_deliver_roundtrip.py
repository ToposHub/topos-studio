"""VP-B10：Deliver／Edit 往返——全屏父级、占用 dock 与借还信号。

缺口（计划 VP-B10）：

- attach 直接 reparent Program viewer——若借出时它正处于全屏（位于独立
  顶层 ``_fullscreen_window``），会留下空的全屏壳且 ``_original_parent``
  失配，Esc 返回时父级悬空；
- detach 无条件 ``dock.setWidget(widget)``——原 dock 被其他面板占用时
  会**顶掉别人**；也不上报"未完全归还"；
- 借出/归还没有对宿主的信号钩子（进入 Deliver 暂停 Source、Deliver
  强制 Timeline 监看等策略无处接线）。

契约：

- 借出前先退出 Program 全屏（从干净的内联状态借出）；
- 原 dock 被占用 → 不覆盖，widget 留在存活宿主（Deliver splitter），
  ``edit_panels_returned(False)`` 诊断；完全归还发 ``returned(True)``；
- ``edit_panels_borrowed``/``edit_panels_returned`` 供宿主接 B01 仲裁
  （进入 Deliver 暂停 Source）与 Viewport 模式切换；
- 30 次往返：对象身份不变、``timeline_changed`` 恰好一条连接、显示模
  式还原、占位符不累积；重复 attach/detach 幂等。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDockWidget, QWidget

from src.features.deliver.ui.deliver_workspace import DeliverWorkspace

pytestmark = pytest.mark.deliver_core


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class _ProgramStub(QWidget):
    """ProgramViewerWidget 鸭子件：全屏状态 + 退出计数。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._is_fullscreen = False
        self.exit_fullscreen_calls = 0

    def enter_fullscreen(self) -> None:
        self._is_fullscreen = True

    def exit_fullscreen(self) -> None:
        self.exit_fullscreen_calls += 1
        self._is_fullscreen = False


class _TimelineStub(QWidget):
    """TimelinePanel 鸭子件（deliver 布局测试同款 + timeline_changed）。"""

    timeline_changed_signal_fired = 0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PyQt6.QtCore import pyqtSignal
        # 类级信号需要在类体定义；此处用动态绑定替代（仅测试用）
        self._signals = _TimelineSignals()
        self.timeline_changed = self._signals.timeline_changed
        self._toolbar_display_mode = "edit"
        self._deliver_range_type = None

    def set_deliver_timeline_controls(self, controls: Any) -> None:
        if controls is not None:
            controls.setParent(self)

    def set_display_mode(self, mode: str) -> None:
        self._toolbar_display_mode = mode

    def set_deliver_range_type(self, range_type: Any) -> None:
        self._deliver_range_type = range_type


class _TimelineSignals(QWidget):
    from PyQt6.QtCore import pyqtSignal

    timeline_changed = pyqtSignal(object)


def _dock(widget: QWidget | None) -> QDockWidget:
    dock = QDockWidget()
    dock.setWidget(widget)
    return dock


# ── 借出/归还信号 ──────────────────────────────────────────────────


def test_b10_borrowed_and_returned_signals(qapp):
    workspace = DeliverWorkspace()
    program = _ProgramStub()
    borrowed: list[bool] = []
    returned: list[bool] = []
    workspace.edit_panels_borrowed.connect(lambda: borrowed.append(True))
    workspace.edit_panels_returned.connect(returned.append)

    workspace.attach_edit_panels(program, None)
    workspace.detach_edit_panels()

    assert borrowed == [True]
    assert returned == [True]


# ── 全屏父级：借出前退出 ───────────────────────────────────────────


def test_b10_borrow_exits_fullscreen_first(qapp):
    workspace = DeliverWorkspace()
    program = _ProgramStub()
    program.enter_fullscreen()

    workspace.attach_edit_panels(program, None)

    assert program.exit_fullscreen_calls == 1
    assert program._is_fullscreen is False
    # 从干净内联状态借出：viewer 进入 Deliver splitter
    assert program.parent() is workspace._edit_preview_splitter
    workspace.detach_edit_panels()


# ── 占用 dock：不覆盖 + 诊断 ───────────────────────────────────────


def test_b10_detach_does_not_overwrite_occupied_dock(qapp):
    workspace = DeliverWorkspace()
    program = _ProgramStub()
    dock = _dock(program)
    returned: list[bool] = []
    workspace.edit_panels_returned.connect(returned.append)

    workspace.attach_edit_panels(program, None, program_viewer_dock=dock)
    other = QWidget()
    dock.setWidget(other)  # 原宿主在借出期间被其他面板占用

    workspace.detach_edit_panels()

    assert dock.widget() is other          # 不顶掉别人
    assert program.parent() is not None    # 存活宿主（Deliver splitter）
    assert program.parent() is workspace._edit_preview_splitter
    assert returned == [False]             # 未完全归还的诊断回填
    assert workspace._edit_panels_attached is False


def test_b10_detach_restores_into_dock_when_free(qapp):
    workspace = DeliverWorkspace()
    program = _ProgramStub()
    dock = _dock(program)
    returned: list[bool] = []
    workspace.edit_panels_returned.connect(returned.append)

    workspace.attach_edit_panels(program, None, program_viewer_dock=dock)
    workspace.detach_edit_panels()

    assert dock.widget() is program
    assert returned == [True]


def test_b10_occupied_timeline_dock_kept_alive(qapp):
    workspace = DeliverWorkspace()
    timeline = _TimelineStub()
    dock = _dock(timeline)

    workspace.attach_edit_panels(None, timeline, timeline_dock=dock)
    other = QWidget()
    dock.setWidget(other)

    workspace.detach_edit_panels()

    assert dock.widget() is other
    assert timeline.parent() is workspace._right_splitter
    assert timeline._toolbar_display_mode == "edit"  # 显示模式仍还原


# ── 幂等与 30 次往返 ───────────────────────────────────────────────


def test_b10_attach_idempotent_keeps_first_widgets(qapp):
    workspace = DeliverWorkspace()
    first = _ProgramStub()
    second = _ProgramStub()
    borrowed: list[bool] = []
    workspace.edit_panels_borrowed.connect(lambda: borrowed.append(True))

    workspace.attach_edit_panels(first, None)
    workspace.attach_edit_panels(second, None)  # 重复借出：no-op

    assert first.parent() is workspace._edit_preview_splitter
    assert second.parent() is None
    assert borrowed == [True]
    workspace.detach_edit_panels()


def test_b10_roundtrip_30_cycles_identity_and_connections(qapp):
    workspace = DeliverWorkspace()
    program = _ProgramStub()
    timeline = _TimelineStub()
    program_dock = _dock(program)
    timeline_dock = _dock(timeline)

    for _ in range(30):
        workspace.attach_edit_panels(
            program, timeline,
            program_viewer_dock=program_dock,
            timeline_dock=timeline_dock)
        assert program.parent() is workspace._edit_preview_splitter
        assert timeline._toolbar_display_mode == "deliver"
        workspace.detach_edit_panels()
        assert program_dock.widget() is program
        assert timeline_dock.widget() is timeline
        assert timeline._toolbar_display_mode == "edit"

    # 对象身份稳定（移动而非复制）
    assert workspace._shared_program_viewer is program
    # timeline_changed 恰好一条连接（重复 attach 不重复连接）：再借出一次，
    # 断开 workspace 的槽恰好成功一次，第二次抛 TypeError（无多余连接）
    workspace.attach_edit_panels(
        program, timeline,
        program_viewer_dock=program_dock, timeline_dock=timeline_dock)
    timeline.timeline_changed.disconnect(workspace._on_shared_timeline_changed)
    with pytest.raises(TypeError):
        timeline.timeline_changed.disconnect(
            workspace._on_shared_timeline_changed)
    workspace.detach_edit_panels()
    # 占位符不累积
    assert workspace._edit_preview_placeholder is not None
    assert workspace._timeline_placeholder is not None


def test_b10_double_detach_is_noop(qapp):
    workspace = DeliverWorkspace()
    program = _ProgramStub()

    workspace.attach_edit_panels(program, None)
    workspace.detach_edit_panels()
    dock_widget_after_first = program.parent()
    workspace.detach_edit_panels()  # 未借出状态下再还：no-op

    assert program.parent() is dock_widget_after_first
