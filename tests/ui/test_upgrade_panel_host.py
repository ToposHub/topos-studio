"""N24 · 升级面板共用宿主测试。

覆盖审计验收句：真实菜单打开后窗口持续存在（引用保持+单实例）；
批准改变主工程（executor 链）、取消零变更；多次开关无泄漏（关闭清理）；
换工程拒绝旧对话框写入（guard/reconcile）；构造失败用户可见。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

from src.app.controllers.upgrade_panel_host import UpgradePanelHost  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeProject:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id


class _FakeStack:
    def __init__(self) -> None:
        self.executed = []

    def execute(self, command, project):
        self.executed.append(command)
        return command.apply(project)


class _FakeMediaPoolPanel(QWidget):
    from PyQt6.QtCore import pyqtSignal

    project_changed = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.stack = _FakeStack()
        self.set_projects = []

    def get_command_stack(self):
        return self.stack

    def set_project(self, project):
        self.set_projects.append(project)


class _FakeOwner(QWidget):
    """主窗口替身：工程/媒体池/不可用提示通道。"""

    def __init__(self) -> None:
        super().__init__()
        self._current_edit_project = None
        self.edit_project_dirty = False
        self.media_pool_panel = _FakeMediaPoolPanel()
        self.unavailable_messages = []

    def _upgrade_panel_unavailable(self, message: str) -> None:
        self.unavailable_messages.append(message)


class _FakeDialog(QDialog):
    accepted_payloads = []

    def __init__(self, project=None, **kwargs) -> None:
        super().__init__()
        self.project = project
        self.applied_command = None


@pytest.fixture()
def registry_factory(monkeypatch: pytest.MonkeyPatch):
    """替换注册表 open_panel：可控面板工厂（不污染全局注册表）。"""
    created = []

    def factory(action_id, **context):
        panel = _FakeDialog(**context)
        created.append(panel)
        return panel

    import src.shared.ui.upgrade_panel_actions as actions
    monkeypatch.setattr(actions, "open_panel", factory)
    return created


def test_open_tracks_and_owns_panel(
        qapp, registry_factory) -> None:
    owner = _FakeOwner()
    project = _FakeProject("p1")
    owner._current_edit_project = project
    host = UpgradePanelHost(owner)
    report = host.open_panel("test.panel", {"project": project})
    assert report.opened and not report.activated_existing
    panel = host.active_panel("test.panel")
    assert panel is registry_factory[0]
    # 主窗口拥有 = 宿主强引用（对话框保持顶层窗口：setParent 会把它
    # 变成内嵌子部件，父隐藏时 close 不走 finished——见 host 注释）
    assert panel.isWindow()
    assert "test.panel" in host.open_action_ids


def test_single_instance_activates_existing(
        qapp, registry_factory) -> None:
    owner = _FakeOwner()
    host = UpgradePanelHost(owner)
    host.open_panel("test.panel", {})
    report2 = host.open_panel("test.panel", {})
    assert report2.activated_existing and not report2.opened
    assert len(registry_factory) == 1  # 不叠加第二实例


def test_finished_cleans_record(qapp, registry_factory) -> None:
    owner = _FakeOwner()
    host = UpgradePanelHost(owner)
    host.open_panel("test.panel", {})
    panel = host.active_panel("test.panel")
    panel.close()  # WA_DeleteOnClose + finished
    assert host.active_panel("test.panel") is None
    # 关闭后 accepted（迟到信号）不再触发处理器
    called = []
    host._records["test.panel"].result_handler = lambda p: called.append(p) \
        or True
    host._on_accepted("test.panel")
    assert not called


def test_accepted_result_handler_and_rejection_notice(
        qapp, registry_factory) -> None:
    owner = _FakeOwner()
    host = UpgradePanelHost(owner)
    seen = []
    host.open_panel(
        "test.panel", {"project": _FakeProject("p1")},
        result_handler=lambda panel: seen.append(panel) or True)
    panel = host.active_panel("test.panel")
    panel.accept()
    assert seen == [panel]

    # 处理器返回 False → 用户可见的拒绝提示
    host.open_panel(
        "test.panel2", {},
        result_handler=lambda panel: False)
    host.active_panel("test.panel2").accept()
    assert any("提交被拒绝" in m for m in owner.unavailable_messages)


def test_guard_rejects_after_project_switch(
        qapp, registry_factory) -> None:
    owner = _FakeOwner()
    p1, p2 = _FakeProject("p1"), _FakeProject("p2")
    owner._current_edit_project = p1
    host = UpgradePanelHost(owner)
    host.open_panel("test.panel", {"project": p1})
    panel = host.active_panel("test.panel")
    assert host.guard(panel) is True
    owner._current_edit_project = p2  # 换工程
    assert host.guard(panel) is False


def test_reconcile_closes_stale_panels(qapp, registry_factory) -> None:
    owner = _FakeOwner()
    p1, p2 = _FakeProject("p1"), _FakeProject("p2")
    owner._current_edit_project = p1
    host = UpgradePanelHost(owner)
    host.open_panel("domain.panel", {"project": p1})
    host.open_panel("global.panel", {})  # 非工程域面板不受影响
    closed = host.reconcile(p2)
    assert closed == ["domain.panel"]
    assert host.active_panel("domain.panel") is None
    assert host.active_panel("global.panel") is not None


def test_construction_failure_is_user_visible(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _FakeOwner()

    def boom(action_id, **context):
        raise RuntimeError("no engine")

    import src.shared.ui.upgrade_panel_actions as actions
    monkeypatch.setattr(actions, "open_panel", boom)
    host = UpgradePanelHost(owner)
    report = host.open_panel("test.panel", {})
    assert not report.opened and report.failed_reason
    assert any("no engine" in m for m in owner.unavailable_messages)


def test_project_command_executor_canonical_chain(qapp) -> None:
    owner = _FakeOwner()
    project = _FakeProject("p1")
    owner._current_edit_project = project
    host = UpgradePanelHost(owner)
    executor = host.project_command_executor()
    marker = SimpleNamespace(applied=None)

    def apply(prj):
        marker.applied = prj
        return _FakeProject("p1")  # 新工程对象

    marker.apply = apply
    command = SimpleNamespace(apply=apply)
    result = executor(command)
    # N04 契约：成功返回**新工程对象**（truthy，面板回读刷新）
    assert result is not None and result is not False
    assert result is not project
    assert owner._current_edit_project is result  # 已回写新工程
    assert owner.edit_project_dirty is True
    assert owner.media_pool_panel.stack.executed == [command]
    assert owner.media_pool_panel.set_projects  # set_project 已广播


def test_project_command_executor_fails_closed(qapp) -> None:
    owner = _FakeOwner()  # 无工程
    host = UpgradePanelHost(owner)
    executor = host.project_command_executor()
    assert executor(SimpleNamespace()) is False  # 无栈/无工程拒绝

    owner2 = _FakeOwner()
    owner2._current_edit_project = _FakeProject("p1")

    class _Boom:
        def apply(self, prj):
            raise ValueError("bad")

    host2 = UpgradePanelHost(owner2)
    assert host2.project_command_executor()(_Boom()) is False
    assert owner2._current_edit_project.project_id == "p1"  # 零半提交


def test_open_reconciles_stale_first(qapp, registry_factory) -> None:
    owner = _FakeOwner()
    p1, p2 = _FakeProject("p1"), _FakeProject("p2")
    owner._current_edit_project = p1
    host = UpgradePanelHost(owner)
    host.open_panel("old.panel", {"project": p1})
    owner._current_edit_project = p2
    report = host.open_panel("new.panel", {"project": p2})
    assert report.stale_closed == ["old.panel"]
    assert report.opened
