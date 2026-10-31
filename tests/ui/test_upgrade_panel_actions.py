"""菜单挂接注册表回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.shared.ui.upgrade_panel_actions import (
    open_panel,
    panel_menu_actions,
    register_default_actions,
    register_panel_action,
)


class TestRegistry:
    def test_default_actions_registered(self):
        actions = panel_menu_actions()
        for aid in ("color.shot_match", "color.version_tools",
                    "edit.filler_removal", "project.snapshot_compare",
                    "project.proxy_cleanup",
                    "composite.tracker_corrections"):
            assert aid in actions, f"missing {aid}"
            assert actions[aid][0]  # 非空标签

    def test_idempotent_register(self):
        register_default_actions()
        register_default_actions()
        assert len(panel_menu_actions()) >= 6

    def test_open_unknown_raises(self):
        with pytest.raises(KeyError, match="未登记"):
            open_panel("no.such.action")

    def test_open_version_tools_constructs(self, qtbot):
        from src.features.edit.core.project import (
            EditProject, MediaAsset, ProjectBin,
        )
        from src.shared.project.color_stack import ColorStackData
        project = EditProject(
            project_id="p", name="P",
            assets={"a": MediaAsset("a", "/m.mov", "video", "a", {})},
            bins={"root": ProjectBin(bin_id="root", name="Root",
                                     parent_id=None, asset_ids=("a",))},
            root_bin_id="root",
            color_stacks={"s1": ColorStackData(
                stack_id="s1", asset_id="a", name="S")})
        panel = open_panel("color.version_tools",
                           project=project, stack_id="s1")
        qtbot.addWidget(panel)
        assert panel.windowTitle().startswith("调色版本")
