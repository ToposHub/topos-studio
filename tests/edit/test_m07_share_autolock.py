"""M07 · 分享后自动锁回归（Use Shared Grade → 共享栈默认锁定）。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import EditProject, ProjectBin
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)
from src.shared.project.color_stack import ColorStackData


def _project_with_shared_stack() -> EditProject:
    """一 clip 引用 asset_default 主栈（shared_stack_ids 应包含它）。"""
    stack = ColorStackData(
        stack_id="s-main", asset_id="a1", name="Asset",
        grading_params={}, scope="asset_default")
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=24,
        source_start=0, source_duration=24, color_stack_id="s-main")
    track = EditTrack(track_id="v1", name="V1", track_type="video",
                      index=0, clips=(clip,))
    seq = TimelineSequence(
        timeline_id="tl1", name="TL", fps=Rational(24, 1),
        video_tracks=(track,))
    from dataclasses import replace as _dc_replace
    project = EditProject(
        project_id="p", name="P", assets={},
        bins={"root": ProjectBin(bin_id="root", name="Root",
                                 parent_id=None, asset_ids=())},
        root_bin_id="root")
    return _dc_replace(
        project, color_stacks={"s-main": stack},
        timelines={"tl1": seq})


class _FakeCommandStack:
    """执行即应用并记录（undo 链可回放的命令栈替身）。"""

    def __init__(self):
        self.executed = []

    def execute(self, command, project):
        self.executed.append(command)
        return command.apply(project)


class TestShareAutoLock:
    def test_use_shared_grade_locks_shared_stack(self):
        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.color_versions import versions_state

        project = _project_with_shared_stack()
        assert versions_state(project, "s-main")["locked"] is False

        cmd_stack = _FakeCommandStack()
        host = NS(
            _current_edit_project=project,
            edit_page=NS(_current_timeline=NS(timeline_id='tl1')),
            media_pool_panel=NS(get_command_stack=lambda: cmd_stack),
            timeline_preview_renderer=None,
            # 分享切换由时间线命令路径负责——替身只回报成功
            _apply_clip_color_stack_change=(
                lambda track_id, clip_id, stack_id, *, shared: True),
            _find_timeline_clip=lambda cid: NS(
                clip_id=cid, asset_id="a1", color_stack_id=""),
        )
        EditControllerMixin._on_use_shared_grade_requested(host, "c1", "v1")

        # 共享栈被自动锁定（asset_default scope ∈ shared_stack_ids）
        assert versions_state(host._current_edit_project,
                              "s-main")["locked"] is True
        # 经命令栈（可撤销），非裸 replace
        assert len(cmd_stack.executed) == 1
        assert cmd_stack.executed[0].after["locked"] is True

    def test_share_failure_skips_autolock(self):
        """分享切换失败 → 不触发自动锁（锁跟随分享动作）。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.color_versions import versions_state

        project = _project_with_shared_stack()
        cmd_stack = _FakeCommandStack()
        host = NS(
            _current_edit_project=project,
            edit_page=NS(_current_timeline=NS(timeline_id='tl1')),
            media_pool_panel=NS(get_command_stack=lambda: cmd_stack),
            timeline_preview_renderer=None,
            _apply_clip_color_stack_change=(
                lambda track_id, clip_id, stack_id, *, shared: False),
            _find_timeline_clip=lambda cid: NS(
                clip_id=cid, asset_id="a1", color_stack_id=""),
        )
        EditControllerMixin._on_use_shared_grade_requested(host, "c1", "v1")
        assert versions_state(host._current_edit_project,
                              "s-main")["locked"] is False
        assert cmd_stack.executed == []


# ================================================================
# M07 · 版本比较（compare_versions + 面板报告文本）
# ================================================================

class TestCompareVersions:
    def _project(self):
        from dataclasses import replace as _dc_replace
        base = _project_with_shared_stack()
        versions = {"s-main": {
            "active": "v2",
            "locked": False,
            "versions": {
                "v1": {"grading_params": {"exposure": 10.0, "contrast": 0.0},
                       "plugin_versions": {"p1": "1.0", "p2": "1.0"}},
                "v2": {"grading_params": {"exposure": 20.0, "contrast": 5.0},
                       "plugin_versions": {"p2": "1.0", "p3": "2.0"}},
            }}}
        return _dc_replace(base, color_stack_versions=versions)

    def test_diff_and_plugin_sets(self):
        from src.features.edit.core.color_versions import compare_versions
        report = compare_versions(self._project(), "s-main", "v1", "v2")
        assert report["same"] is False
        assert report["diffs"]["exposure"] == [10.0, 20.0]
        assert report["diffs"]["contrast"] == [0.0, 5.0]
        assert report["plugin_only_a"] == ["p1"]
        assert report["plugin_only_b"] == ["p3"]

    def test_same_versions_report_identical(self):
        from src.features.edit.core.color_versions import compare_versions
        project = self._project()
        table = project.color_stack_versions
        table["s-main"]["versions"]["v3"] = dict(
            table["s-main"]["versions"]["v1"])
        report = compare_versions(project, "s-main", "v1", "v3")
        assert report["same"] is True and not report["diffs"]

    def test_unknown_version_rejected(self):
        from src.features.edit.core.color_versions import compare_versions
        with pytest.raises(ValueError):
            compare_versions(self._project(), "s-main", "v1", "gone")

    def test_panel_compare_text(self):
        from src.features.edit.ui.m07_version_tools_panel import (
            ColorVersionToolsPanel,
        )
        text = ColorVersionToolsPanel._compare_text({
            "a": "v1", "b": "v2", "same": False,
            "diffs": {"exposure": [10.0, 20.0]},
            "plugin_only_a": ["p1"], "plugin_only_b": []})
        assert "v1 ↔ v2" in text
        assert "exposure: 10.0 → 20.0" in text
        assert "仅 v1 有 ['p1']" in text
