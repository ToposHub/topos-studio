"""M07 UI · 调色版本工具面板回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.ui.m07_version_tools_panel import ColorVersionToolsPanel
from src.features.edit.core.commands import ProjectTransactionResult
from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin
from src.shared.project.color_stack import ColorStackData


def _project():
    return EditProject(
        project_id="p", name="P",
        assets={"a": MediaAsset("a", "/m.mov", "video", "a", {})},
        bins={"root": ProjectBin(bin_id="root", name="Root",
                                 parent_id=None, asset_ids=("a",))},
        root_bin_id="root",
        color_stacks={"s1": ColorStackData(
            stack_id="s1", asset_id="a", name="S",
            grading_params={"exposure": 10.0})},
        revision=1)


class TestVersionToolsPanel:
    def test_save_lists_version(self, qtbot, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("v1", True)))
        panel = ColorVersionToolsPanel(_project(), "s1")
        qtbot.addWidget(panel)
        panel._on_save()
        names = [panel._list.item(i).text()
                 for i in range(panel._list.count())]
        assert any("v1" in n for n in names)
        assert "未锁定" in panel._status.text()

    def test_lock_toggle_reflected(self, qtbot):
        panel = ColorVersionToolsPanel(_project(), "s1")
        qtbot.addWidget(panel)
        panel._on_toggle_lock()
        assert "已锁定" in panel._status.text()
        panel._on_toggle_lock()
        assert "未锁定" in panel._status.text()


class TestVersionToolsPanelExecutor:
    """N04 · 面板命令必须经 executor 入主工程栈并回读新工程。"""

    def _panel_with_executor(self, qtbot):
        submitted = []
        state = {"project": _project()}

        def executor(command):
            submitted.append(command)
            result = command.apply(state["project"])
            # BE-01：Activate 返回 ProjectTransactionResult（兼容旧二元组）
            if isinstance(result, tuple):
                result = result[0]
            if isinstance(result, ProjectTransactionResult):
                result = result.project
            state["project"] = result
            return state["project"]  # 正典链返回新工程

        panel = ColorVersionToolsPanel(
            state["project"], "s1", command_executor=executor)
        qtbot.addWidget(panel)
        return panel, submitted

    def test_save_routes_through_executor_and_reads_back(
            self, qtbot, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("v1", True)))
        panel, submitted = self._panel_with_executor(qtbot)
        before = panel._project
        panel._on_save()
        assert len(submitted) == 1  # 命令经 executor（主工程栈路径）
        assert panel._project is not before  # 回读新工程（非旧私有快照）
        names = [panel._list.item(i).text()
                 for i in range(panel._list.count())]
        assert any("v1" in n for n in names)

    def test_activate_routes_through_executor(self, qtbot, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("v1", True)))
        monkeypatch.setattr(
            QMessageBox, "information", staticmethod(lambda *a, **k: None))
        panel, submitted = self._panel_with_executor(qtbot)
        panel._on_save()
        assert len(submitted) == 1
        # 选中已保存版本后激活
        panel._list.setCurrentRow(0)
        panel._on_activate()
        assert len(submitted) == 2  # 激活同走命令出口（旧路径绕栈直改）
        state_after = panel._state()
        assert state_after.get("active") == "v1"

    def test_executor_rejection_leaves_panel_unwritten(self, qtbot):
        panel = ColorVersionToolsPanel(
            _project(), "s1",
            command_executor=lambda command: False)  # 工程已切换/被拒
        qtbot.addWidget(panel)
        before = panel._project
        from src.features.edit.core.color_versions import SetStackLockCommand
        panel._execute(SetStackLockCommand.create(
            panel._project, "s1", True))
        assert panel._project is before  # 拒绝：不回读不写入
        assert "提交被拒" in panel._status.text()

    def test_registry_factory_forwards_executor(self):
        from src.shared.ui.upgrade_panel_actions import open_panel
        captured = {}

        class _Probe:
            def __init__(self, *a, **kw):
                captured.update(kw)

        import src.features.edit.ui.m07_version_tools_panel as m07
        original = m07.ColorVersionToolsPanel
        m07.ColorVersionToolsPanel = _Probe
        try:
            sentinel = object()
            open_panel("color.version_tools", project=_project(),
                       stack_id="s1", command_executor=sentinel)
        finally:
            m07.ColorVersionToolsPanel = original
        assert captured.get("command_executor") is sentinel
