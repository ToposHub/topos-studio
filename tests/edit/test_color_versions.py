"""F4.2 · 命名调色版本与共享 look 锁定——验收测试。

验收锚点：
- 同镜头 A/B/C 一键比较/回退（激活=内容互换，undo 单步恢复）；
- 跨镜头共享变化的影响范围可预览（stack_dependent_clips）；
- 复制/删除版本、Undo、保存恢复一致；
- 共享 look 默认防误改（锁定拒改，显式解锁/本地副本可改）；
- 切换不覆盖其他镜头私有修改（F4.1 微调栈独立存活）；
- 插件缺失不丢版本数据（载荷保留 + missing_plugins 明示）。
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.color_versions import (
    ActivateColorVersionCommand,
    DeleteColorVersionCommand,
    SaveColorVersionCommand,
    SetStackLockCommand,
    is_stack_locked,
    lock_shared_stacks,
    promote_default_version,
    shared_stack_ids,
    stack_dependent_clips,
    versions_state,
)
from src.features.edit.core.commands import UpdateColorStackCommand
from src.features.edit.core.edit_project_io import (
    convert_edit_project_to_topos_project,
    convert_topos_project_to_edit_project,
)
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence
from src.shared.project.color_stack import ColorStackData

pytestmark = pytest.mark.edit_core


def _timeline(clip_ids, stack_id="s1") -> TimelineSequence:
    clips = tuple(
        ClipInstance(cid, "asset", "v1", i * 10, 10, 0, 10,
                     color_stack_id=stack_id,
                     color_trim_stack_id=f"trim-{cid}")
        for i, cid in enumerate(clip_ids))
    return TimelineSequence(
        timeline_id="tl1", name="TL", fps=24.0,
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=clips),))


def _project(exposure=5.0, clip_ids=("c1", "c2")) -> EditProject:
    stack = ColorStackData(
        stack_id="s1", name="Look",
        grading_params={"exposure": exposure})
    trims = {
        f"trim-{cid}": ColorStackData(
            stack_id=f"trim-{cid}",
            grading_params={"saturation": 5.0})
        for cid in clip_ids}
    stacks = {"s1": stack, **trims}
    return EditProject(
        project_id="p", name="P", color_stacks=stacks,
        timelines={"tl1": _timeline(clip_ids)})


def _set_exposure(project, value):
    from dataclasses import replace
    stack = replace(
        project.color_stacks["s1"],
        grading_params={**project.color_stacks["s1"].grading_params,
                        "exposure": value})
    return project.with_color_stacks({**project.color_stacks, "s1": stack})


class TestNamedVersions:
    def test_save_activate_abc_compare_and_revert(self):
        """A/B/C 保存 + 激活比较 + undo 单步回退。"""
        p = _project(exposure=5.0)
        p = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        p = _set_exposure(p, 20.0)
        p = SaveColorVersionCommand.create(p, "s1", "B").apply(p)
        p = _set_exposure(p, 40.0)
        p = SaveColorVersionCommand.create(p, "s1", "C").apply(p)

        for name, exposure in (("A", 5.0), ("B", 20.0), ("C", 40.0)):
            result = ActivateColorVersionCommand.create("s1", name).apply(p)
            p_next, applied = result.project, result.updated_command
            assert p_next.color_stacks["s1"].grading_params[
                "exposure"] == exposure
            reverted = applied.undo(p_next)  # 单步回退
            assert reverted.color_stacks["s1"].grading_params[
                "exposure"] == p.color_stacks["s1"].grading_params["exposure"]
            assert versions_state(reverted, "s1")["active"] == \
                versions_state(p, "s1")["active"]

    def test_activation_preserves_stack_identity(self):
        """激活 = 内容互换：身份字段不变（引用/共享/组链稳定）。"""
        p = _project()
        p = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        p = _set_exposure(p, 30.0)
        p_next = ActivateColorVersionCommand.create(
            "s1", "A").apply(p).project
        stack = p_next.color_stacks["s1"]
        assert stack.stack_id == "s1"
        assert stack.scope == "clip_local"
        assert stack.name == "Look"

    def test_duplicate_name_refused_and_replace(self):
        p = _project()
        p = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        with pytest.raises(ValueError, match="已存在"):
            SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        p2 = _set_exposure(p, 12.0)
        p3 = SaveColorVersionCommand.create(
            p2, "s1", "A", replace=True).apply(p2)
        assert versions_state(p3, "s1")["versions"]["A"][
            "grading_params"]["exposure"] == 12.0

    def test_delete_version_and_active_pointer(self):
        p = _project()
        p = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        p = DeleteColorVersionCommand.create(p, "s1", "A").apply(p)
        assert versions_state(p, "s1")["versions"] == {}
        assert versions_state(p, "s1")["active"] is None
        assert p.color_stacks["s1"].grading_params["exposure"] == 5.0  # 内容不动

    def test_conflicting_transaction_refused(self):
        p = _project()
        p2 = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        stale = SaveColorVersionCommand.create(p, "s1", "X")
        with pytest.raises(ValueError, match="已被其他操作修改"):
            stale.apply(p2)

    def test_io_roundtrip(self):
        p = _project()
        p = SaveColorVersionCommand.create(p, "s1", "Warm").apply(p)
        restored = convert_topos_project_to_edit_project(
            convert_edit_project_to_topos_project(p), full_data={})
        assert "Warm" in versions_state(restored, "s1")["versions"]
        assert versions_state(restored, "s1")["active"] == "Warm"
        # 旧工程（无键）→ 空表
        blank = convert_topos_project_to_edit_project(
            convert_edit_project_to_topos_project(
                EditProject(project_id="q", name="Q")), full_data={})
        assert blank.color_stack_versions == {}


class TestSharedLookLock:
    def test_shared_stacks_detected_and_locked_by_default(self):
        p = _project(clip_ids=("c1", "c2"))  # 两镜头引用同一栈 = 共享
        assert shared_stack_ids(p) == ("s1",)
        locked, ids = lock_shared_stacks(p)
        assert "s1" in ids and is_stack_locked(locked, "s1")

    def test_locked_stack_refuses_direct_edit(self):
        p, _ = lock_shared_stacks(_project())
        with pytest.raises(ValueError, match="已锁定"):
            UpdateColorStackCommand(
                command_id="x", label="L", stack_id="s1",
                grading_params={"exposure": 9.0}).apply(p)

    def test_unlock_then_edit(self):
        p, _ = lock_shared_stacks(_project())
        p2 = SetStackLockCommand.create(p, "s1", False).apply(p)
        p3 = UpdateColorStackCommand(
            command_id="x", label="L", stack_id="s1",
            grading_params={"exposure": 9.0}).apply(p2)
        assert p3.color_stacks["s1"].grading_params["exposure"] == 9.0

    def test_lock_does_not_block_version_management(self):
        """锁防误改 look，不防版本管理（保存/激活/删除版本可用）。"""
        p, _ = lock_shared_stacks(_project())
        p2 = SaveColorVersionCommand.create(p, "s1", "V1").apply(p)
        p3 = ActivateColorVersionCommand.create(
            "s1", "V1").apply(p2).project
        assert versions_state(p3, "s1")["active"] == "V1"

    def test_impact_preview_dependent_clips(self):
        """跨镜头共享变化的影响范围可预览。"""
        p = _project(clip_ids=("c1", "c2", "c3"))
        assert stack_dependent_clips(p, "s1") == [
            ("tl1", "c1"), ("tl1", "c2"), ("tl1", "c3")]
        assert stack_dependent_clips(p, "trim-c1") == [("tl1", "c1")]


class TestInteractionWithF41:
    def test_version_switch_preserves_private_trims(self):
        """切换共享版本不覆盖其他镜头私有修改（F4.1 微调栈独立）。"""
        p = _project()
        p = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        p = _set_exposure(p, 25.0)
        p = SaveColorVersionCommand.create(p, "s1", "B").apply(p)
        p2 = ActivateColorVersionCommand.create(
            "s1", "A").apply(p).project
        for clip in p2.timelines["tl1"].video_tracks[0].clips:
            assert clip.color_trim_stack_id == f"trim-{clip.clip_id}"
        assert p2.color_stacks["trim-c1"].grading_params["saturation"] == 5.0

    def test_active_version_content_reaches_export_resolution(self):
        """非当前序列导出取正确版本：激活=内容互换，导出解析
        （_get_clip_color_grade 读 color_stacks）自动取激活版本。"""
        from src.features.edit.core.timeline_export import TimelineExporter

        p = _project()
        p = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        p = _set_exposure(p, 33.0)
        p2 = ActivateColorVersionCommand.create(
            "s1", "A").apply(p).project

        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._project = SimpleNamespace(color_stacks=p2.color_stacks)
        exporter._color_grade_lut_cache = {}
        layer = SimpleNamespace(
            clip=SimpleNamespace(color_stack_id="s1"),
            clip_id="c1", source_frame=0)
        grade = exporter._get_clip_color_grade(layer)
        assert grade["grading_params"]["exposure"] == 5.0  # 版本 A 内容


class TestVersionTableIOFidelity:
    """IO-01 · 版本表（含 BE-04 default_version 键）保存/加载保真。"""

    def _roundtrip(self, project):
        return convert_topos_project_to_edit_project(
            convert_edit_project_to_topos_project(project), full_data={})

    def test_full_table_roundtrip_preserves_every_field(self):
        """版本内容/active/locked/default_version/未知 payload 字段保真。"""
        from src.features.edit.core.color_versions import (
            DeleteAllVersionsCommand, SaveColorVersionCommand,
        )
        p = _project()
        p = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        p = SaveColorVersionCommand.create(p, "s1", "B").apply(p)
        # 未知 payload 字段（未来 schema 前向兼容）——注入将被保留的 B
        table = dict(p.color_stack_versions)
        table["s1"]["versions"]["B"]["future_field"] = {"nested": [1, 2]}
        p = p.with_color_stack_versions(table)
        p, _migrate = promote_default_version(p, "s1")
        p = DeleteAllVersionsCommand.create(p, "s1").apply(p).project

        restored = self._roundtrip(p)
        state = versions_state(restored, "s1")
        assert set(state["versions"]) == {state["default_version"]}
        assert state["active"] == state["default_version"] == "B"
        assert state["versions"]["B"]["future_field"] == {"nested": [1, 2]}
        assert state["versions"]["B"]["grading_params"] == \
            versions_state(p, "s1")["versions"]["B"]["grading_params"]

    def test_locked_flag_roundtrip(self):
        from src.features.edit.core.color_versions import SetStackLockCommand
        p = _project()
        p = SetStackLockCommand.create(p, "s1", True).apply(p)
        assert self._roundtrip(p).color_stack_versions["s1"]["locked"] is True

    def test_old_project_without_keys_loads_empty(self):
        blank = self._roundtrip(
            EditProject(project_id="q", name="Q"))
        assert blank.color_stack_versions == {}
        assert versions_state(blank, "s1") == {
            "active": None, "locked": False, "versions": {},
            "default_version": None}


class TestPluginManifest:
    def test_version_payload_carries_plugin_manifest(self):
        """插件清单随版本保存；数据不因缺失而丢。"""
        stack = ColorStackData(
            stack_id="s1",
            layers=({"plugin_id": "fx.denoise.v2",
                     "plugin_version": "2.3"},))
        p = EditProject(project_id="p", name="P",
                        color_stacks={"s1": stack})
        p2 = SaveColorVersionCommand.create(p, "s1", "V").apply(p)
        payload = versions_state(p2, "s1")["versions"]["V"]
        assert payload["plugin_versions"] == {"fx.denoise.v2": "2.3"}
        # 未知插件不丢载荷（missing_plugins 计算失败时保守为空）
        assert "layers" in payload


class TestPluginDiagnostics:
    """DIAG-01 · 统一诊断字段：missing/unverified 区分 + 持久化。"""

    def test_diagnostics_persisted_and_distinguished(self):
        from src.features.edit.core.color_versions import (
            version_plugin_diagnostics,
        )
        stack = ColorStackData(
            stack_id="s1",
            layers=({"plugin_id": "fx.notinstalled.v1",
                     "plugin_version": "1.0"},))
        p = EditProject(project_id="p", name="P",
                        color_stacks={"s1": stack})
        p2 = SaveColorVersionCommand.create(p, "s1", "V").apply(p)
        payload = versions_state(p2, "s1")["versions"]["V"]
        # 保存时持久化统一字段
        assert "plugin_diagnostics" in payload
        diagnostics = version_plugin_diagnostics(payload)
        assert isinstance(diagnostics["unverified"], bool)
        # 注册表可用时给出真实缺失清单（fx.notinstalled 未注册）
        assert "fx.notinstalled.v1" in diagnostics["missing"]
        assert diagnostics["unverified"] is False

    def test_diagnostics_registry_unavailable_marks_unverified(self):
        """注册表不可用：unverified=True，不当"无缺失"。"""
        import src.features.edit.core.color_versions as cv
        from src.features.edit.core.project import EditProject as EP
        from src.shared.project.color_stack import ColorStackData as CS

        p = EP(project_id="p", name="P",
               color_stacks={"s1": CS(
                   stack_id="s1", grading_params={"exposure": 1.0},
                   layers=({"plugins": (
                       {"plugin_id": "fx.any", "plugin_version": "1.0"},)},))})

        class _BrokenManager:
            def get_all_plugins(self):
                raise RuntimeError("registry down")

        import src.features.color.core.plugin_manager as pm
        original = pm.get_plugin_manager
        pm.get_plugin_manager = lambda: _BrokenManager()
        try:
            p2 = cv.SaveColorVersionCommand.create(p, "s1", "V").apply(p)
            saved = cv.versions_state(p2, "s1")["versions"]["V"]
            diagnostics = cv.version_plugin_diagnostics(saved)
            assert diagnostics["unverified"] is True
            assert diagnostics["missing"] == []  # 保守为空，不冒充已核对
        finally:
            pm.get_plugin_manager = original

    def test_lock_rejects_param_write_but_allows_version_mgmt(self):
        """锁语义：普通参数写锁定栈零写入；版本管理照常（DIAG-01）。"""
        p, _ = lock_shared_stacks(_project())
        from src.features.edit.core.color_versions import (
            RenameColorVersionCommand, SaveColorVersionCommand,
        )
        with pytest.raises(ValueError, match="已锁定"):
            UpdateColorStackCommand(
                command_id="x", label="L", stack_id="s1",
                grading_params={"exposure": 9.0}).apply(p)
        p2 = SaveColorVersionCommand.create(p, "s1", "V1").apply(p)
        p3 = RenameColorVersionCommand.create(
            p2, "s1", "V1", "Renamed").apply(p2)
        assert "Renamed" in versions_state(p3, "s1")["versions"]
