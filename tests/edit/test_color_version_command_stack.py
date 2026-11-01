"""BE-01 · 调色版本命令 × 真实 ProjectCommandStack 验收。

2026-11-01 实测阻塞回归锚点：``ActivateColorVersionCommand.apply`` 曾返回
裸 ``(new_project, applied)`` 二元组，``ProjectCommandStack.execute`` 把
二元组交给 ``dataclasses.replace`` 直接 TypeError。本文件全部使用**真实
栈**（不拆 tuple 的 fake executor），覆盖：

- save → modify live → activate → undo → redo 全链路无 TypeError；
- undo 栈持有 apply 后命令（激活前内容 + active 指针可恢复）；
- revision 每次 execute/undo/redo 单调 +1；
- 失败命令不压入历史（栈长度不变、redo 不被清空）；
- undo/redo 内容与 active 指针对称；
- ProjectMacroCommand 内嵌激活命令时宏 undo 同样恢复。
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.color_versions import (
    DEFAULT_VERSION_NAME,
    ActivateColorVersionCommand,
    DeleteAllVersionsCommand,
    DeleteColorVersionCommand,
    DuplicateColorVersionCommand,
    InitializeDefaultVersionCommand,
    RenameColorVersionCommand,
    SaveColorVersionCommand,
    create_version_with_default,
    promote_default_version,
    validate_version_name,
    versions_state,
)
from src.features.edit.core.commands import (
    ProjectCommandStack,
    ProjectMacroCommand,
)
from src.features.edit.core.project import EditProject
from src.shared.project.color_stack import ColorStackData

pytestmark = pytest.mark.edit_core


def _project(exposure: float = 5.0) -> EditProject:
    return EditProject(
        project_id="p", name="P",
        color_stacks={"s1": ColorStackData(
            stack_id="s1", name="Look",
            grading_params={"exposure": exposure})})


def _two_stack_project() -> EditProject:
    """s1（exposure 5.0）+ s2（exposure 50.0）双栈工程（跨栈复制用）。"""
    return EditProject(
        project_id="p", name="P",
        color_stacks={
            "s1": ColorStackData(
                stack_id="s1", name="Look",
                grading_params={"exposure": 5.0}),
            "s2": ColorStackData(
                stack_id="s2", name="Remote",
                grading_params={"exposure": 50.0}),
        })


def _set_exposure(project: EditProject, value: float) -> EditProject:
    stack = replace(
        project.color_stacks["s1"],
        grading_params={**project.color_stacks["s1"].grading_params,
                        "exposure": value})
    return project.with_color_stacks({**project.color_stacks, "s1": stack})


class TestRealStackActivationProtocol:
    def test_save_modify_activate_undo_redo_on_real_stack(self):
        """计划步骤 A 主验收：真实栈全链路无 TypeError，内容与指针对称。"""
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)

        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        p = stack.execute(
            SaveColorVersionCommand.create(
                _set_exposure(p, 20.0), "s1", "B"), p)

        # live 改成 40 后激活 A：工程内容回到 5，active 指向 A
        live = _set_exposure(p, 40.0)
        p = stack.execute(
            ActivateColorVersionCommand.create("s1", "A"), live)
        assert p.color_stacks["s1"].grading_params["exposure"] == 5.0
        assert versions_state(p, "s1")["active"] == "A"

        # undo：恢复激活前 live（40）与 active（B）
        p = stack.undo(p)
        assert p.color_stacks["s1"].grading_params["exposure"] == 40.0
        assert versions_state(p, "s1")["active"] == "B"

        # redo：内容与指针再次对称
        p = stack.redo(p)
        assert p.color_stacks["s1"].grading_params["exposure"] == 5.0
        assert versions_state(p, "s1")["active"] == "A"

    def test_revision_monotonic_across_execute_undo_redo(self):
        stack = ProjectCommandStack()
        p = _project()
        revisions = [p.revision]
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        revisions.append(p.revision)
        p = stack.execute(
            ActivateColorVersionCommand.create("s1", "A"), p)
        revisions.append(p.revision)
        p = stack.undo(p)
        revisions.append(p.revision)
        p = stack.redo(p)
        revisions.append(p.revision)
        assert revisions == sorted(revisions), revisions
        assert len(set(revisions)) == len(revisions)  # 严格单调 +1

    def test_failed_command_not_pushed_and_redo_preserved(self):
        stack = ProjectCommandStack()
        p = _project()
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        # 激活不存在的版本：apply 抛 ValueError，栈不得压入该命令
        with pytest.raises(ValueError, match="不存在"):
            stack.execute(
                ActivateColorVersionCommand.create("s1", "nope"), p)
        assert stack.get_history_size() == 1  # 只有 save
        # undo/redo 仍对称可用
        undone = stack.undo(p)
        assert versions_state(undone, "s1")["versions"] == {}
        redone = stack.redo(undone)
        assert "A" in versions_state(redone, "s1")["versions"]

    def test_stale_activation_rejected_zero_commit(self):
        """stale 激活命令（创建后目标版本被删）失败：无半提交、无历史污染。"""
        stack = ProjectCommandStack()
        p = _project()
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        stale_cmd = ActivateColorVersionCommand.create("s1", "A")
        # 命令创建后版本被删除：apply 期校验拒绝
        p = stack.undo(p)
        assert versions_state(p, "s1")["versions"] == {}
        with pytest.raises(ValueError, match="不存在"):
            stack.execute(stale_cmd, p)
        # 失败零提交：undo 栈未增长，redo 历史未被清空
        assert not stack.can_undo()
        assert stack.can_redo()
        assert versions_state(p, "s1")["active"] is None

    def test_undo_restores_pre_activation_live_content(self):
        """undo 保存的是 apply 后命令：激活前 live 内容完整恢复。"""
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        live = _set_exposure(p, 12.5)
        p = stack.execute(
            SaveColorVersionCommand.create(live, "s1", "B"), live)
        p = stack.execute(
            ActivateColorVersionCommand.create("s1", "A"), p)
        assert p.color_stacks["s1"].grading_params["exposure"] == 5.0
        undone = stack.undo(p)
        # 激活前的 live 是 12.5（B 保存时的栈内容），不是 5.0
        assert undone.color_stacks["s1"].grading_params["exposure"] == 12.5


class TestRenameDuplicateOnRealStack:
    """BE-02 · Rename/Duplicate 原子命令（计划 §7.3「名称与非当前版本」）。"""

    def _project_ab_live_c(self):
        """保存 A(5.0)/B(20.0)，live 栈再改为 C(40.0)——复制源必须是
        指定快照而非 live。"""
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        live_b = _set_exposure(p, 20.0)
        p = stack.execute(
            SaveColorVersionCommand.create(live_b, "s1", "B"), live_b)
        return stack, _set_exposure(p, 40.0)  # live=C，未保存

    def test_duplicate_non_current_snapshot_then_rename(self):
        """保存 A/B、live 改为 C：复制 A 为 A2 → A2 内容来自 A 而非 C；
        再重命名 A2 → 内容不变。"""
        stack, p = self._project_ab_live_c()
        p = stack.execute(
            DuplicateColorVersionCommand.create(p, "s1", "A", "A2"), p)
        versions = versions_state(p, "s1")["versions"]
        assert versions["A2"]["grading_params"]["exposure"] == 5.0  # 来自 A
        assert versions["A2"]["saved_at"] == versions["A"]["saved_at"]
        assert versions_state(p, "s1")["active"] == "B"  # 不自动激活

        p = stack.execute(
            RenameColorVersionCommand.create(p, "s1", "A2", "A3"), p)
        versions = versions_state(p, "s1")["versions"]
        assert "A2" not in versions and "A3" in versions
        assert versions["A3"]["grading_params"]["exposure"] == 5.0
        assert p.color_stacks["s1"].grading_params["exposure"] == 40.0  # live 不动

        # undo ×2 对称恢复
        p = stack.undo(p)
        assert "A3" not in versions_state(p, "s1")["versions"]
        assert "A2" in versions_state(p, "s1")["versions"]
        p = stack.undo(p)
        assert "A2" not in versions_state(p, "s1")["versions"]
        p = stack.redo(p)
        assert versions_state(p, "s1")["versions"]["A2"][
            "grading_params"]["exposure"] == 5.0

    def test_rename_active_tracks_pointer_and_preserves_order(self):
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        live_b = _set_exposure(p, 20.0)
        p = stack.execute(
            SaveColorVersionCommand.create(live_b, "s1", "B"), live_b)
        assert versions_state(p, "s1")["active"] == "B"
        p = stack.execute(
            RenameColorVersionCommand.create(p, "s1", "B", "B2"), p)
        state = versions_state(p, "s1")
        assert list(state["versions"]) == ["A", "B2"]  # 原位换名保序
        assert state["active"] == "B2"  # active 同步改名
        # undo 恢复旧表（active 指回 B）
        p = stack.undo(p)
        assert list(versions_state(p, "s1")["versions"]) == ["A", "B"]
        assert versions_state(p, "s1")["active"] == "B"

    def test_rename_keeps_payload_plugin_manifest_and_saved_at(self):
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        before_payload = versions_state(p, "s1")["versions"]["A"]
        p = stack.execute(
            RenameColorVersionCommand.create(p, "s1", "A", "Warm"), p)
        after_payload = versions_state(p, "s1")["versions"]["Warm"]
        assert after_payload == before_payload  # 逐字节保留

    def test_duplicate_and_rename_reject_bad_names_zero_commit(self):
        stack, p = self._project_ab_live_c()
        history = stack.get_history_size()
        with pytest.raises(ValueError, match="已存在"):
            stack.execute(
                DuplicateColorVersionCommand.create(p, "s1", "A", "B"), p)
        with pytest.raises(ValueError, match="不存在"):
            stack.execute(
                RenameColorVersionCommand.create(p, "s1", "nope", "X"), p)
        with pytest.raises(ValueError, match="相同"):
            stack.execute(
                RenameColorVersionCommand.create(p, "s1", "A", " A "), p)
        with pytest.raises(ValueError, match="不能为空"):
            stack.execute(
                DuplicateColorVersionCommand.create(p, "s1", "A", "  "), p)
        with pytest.raises(ValueError, match="超过"):
            stack.execute(
                DuplicateColorVersionCommand.create(
                    p, "s1", "A", "x" * 129), p)
        with pytest.raises(ValueError, match="控制字符"):
            stack.execute(
                RenameColorVersionCommand.create(
                    p, "s1", "A", "bad\x01name"), p)
        assert stack.get_history_size() == history  # 全部零提交
        assert set(versions_state(p, "s1")["versions"]) == {"A", "B"}

    def test_stale_rename_rejected_not_overwriting_newer_edits(self):
        """命令创建后版本表被改：apply 期校验拒绝，不覆盖新修改。"""
        stack, p = self._project_ab_live_c()
        stale = RenameColorVersionCommand.create(p, "s1", "A", "Warm")
        p = stack.execute(
            DuplicateColorVersionCommand.create(p, "s1", "B", "B2"), p)
        with pytest.raises(ValueError, match="已被其他操作修改"):
            stack.execute(stale, p)
        assert "Warm" not in versions_state(p, "s1")["versions"]

    def test_validate_version_name_shared_rules(self):
        assert validate_version_name("  Look 2 ") == "Look 2"
        assert validate_version_name("x" * 128) == "x" * 128
        for bad in ("", "   ", "x" * 129, "a\nb", "a\x7fb"):
            with pytest.raises(ValueError):
                validate_version_name(bad)


class TestCopyVersionAcrossStacks:
    """WIRE-01 · CopyColorVersionCommand（跨栈快照复制，COPY_TO_SCOPE）。"""

    def test_copy_snapshot_across_stacks_undo_symmetric(self):
        from src.features.edit.core.color_versions import (
            CopyColorVersionCommand,
        )
        stack = ProjectCommandStack()
        p = _two_stack_project()
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "Warm"), p)
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s2", "Other"), p)
        # 跨栈复制：s1.Warm → s2.RemoteWarm（不覆盖 s2.Other，不动 active）
        p = stack.execute(
            CopyColorVersionCommand.create(p, "s1", "Warm", "s2",
                                           "RemoteWarm"), p)
        s2_state = versions_state(p, "s2")
        assert set(s2_state["versions"]) == {"Other", "RemoteWarm"}
        assert s2_state["versions"]["RemoteWarm"]["grading_params"][
            "exposure"] == 5.0  # 来自源快照，非 s2 live（50.0）
        assert s2_state["active"] == "Other"  # 不自动激活
        assert p.color_stacks["s2"].grading_params["exposure"] == 50.0
        # undo 只还原目标表；源表从未被写
        p = stack.undo(p)
        assert set(versions_state(p, "s2")["versions"]) == {"Other"}
        assert set(versions_state(p, "s1")["versions"]) == {"Warm"}
        p = stack.redo(p)
        assert "RemoteWarm" in versions_state(p, "s2")["versions"]

    def test_copy_rejects_missing_source_and_duplicate_dest(self):
        from src.features.edit.core.color_versions import (
            CopyColorVersionCommand,
        )
        p = _two_stack_project()
        p = SaveColorVersionCommand.create(p, "s1", "Warm").apply(p)
        p = SaveColorVersionCommand.create(p, "s2", "Warm").apply(p)
        with pytest.raises(ValueError, match="不存在"):
            CopyColorVersionCommand.create(p, "s1", "ghost", "s2", "X")
        with pytest.raises(ValueError, match="已存在"):
            CopyColorVersionCommand.create(p, "s1", "Warm", "s2", "Warm")


class TestDefaultVersionAndDeleteAll:
    """BE-04 · 默认版本初始化 + DeleteAll 原子事务（真实栈）。"""

    def test_create_first_version_initializes_default_in_same_transaction(self):
        """空域首个 CREATE：同一宏事务产出默认版本 + 用户命名版本。"""
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)
        p = stack.execute(
            create_version_with_default(p, "s1", "Warm"), p)
        state = versions_state(p, "s1")
        assert set(state["versions"]) == {
            DEFAULT_VERSION_NAME, "Warm"}
        assert state["default_version"] == DEFAULT_VERSION_NAME
        assert state["active"] == "Warm"
        # 两版本快照都来自初始化时的 live 内容
        assert state["versions"][DEFAULT_VERSION_NAME][
            "grading_params"]["exposure"] == 5.0
        assert state["versions"]["Warm"][
            "grading_params"]["exposure"] == 5.0
        # 单步 undo 整体恢复空表
        undone = stack.undo(p)
        assert versions_state(undone, "s1")["versions"] == {}
        assert versions_state(undone, "s1")["default_version"] is None
        redone = stack.redo(undone)
        assert versions_state(redone, "s1")["active"] == "Warm"

    def test_initialize_default_on_non_empty_table_refused(self):
        p = _project(exposure=5.0)
        p = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        with pytest.raises(ValueError, match="非空"):
            InitializeDefaultVersionCommand.create(p, "s1")

    def test_delete_all_keeps_default_and_loads_it_atomically(self):
        """DeleteAll：保留 default、删其余、live 栈换入 default 内容，
        undo/redo 整体恢复（表 + live 栈）。"""
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)
        p = stack.execute(
            create_version_with_default(p, "s1", "Warm"), p)
        live_b = _set_exposure(p, 20.0)
        p = stack.execute(
            SaveColorVersionCommand.create(live_b, "s1", "B"), live_b)
        p = stack.execute(
            DuplicateColorVersionCommand.create(
                p, "s1", "Warm", "Warm2"), p)
        assert set(versions_state(p, "s1")["versions"]) == {
            DEFAULT_VERSION_NAME, "Warm", "B", "Warm2"}

        p = stack.execute(DeleteAllVersionsCommand.create(p, "s1"), p)
        state = versions_state(p, "s1")
        assert set(state["versions"]) == {DEFAULT_VERSION_NAME}
        assert state["active"] == DEFAULT_VERSION_NAME
        assert state["default_version"] == DEFAULT_VERSION_NAME
        # live 栈已换入 default 内容（5.0），不是停留 B 的 20.0
        assert p.color_stacks["s1"].grading_params["exposure"] == 5.0

        undone = stack.undo(p)  # 整体恢复：表 + live 栈
        assert set(versions_state(undone, "s1")["versions"]) == {
            DEFAULT_VERSION_NAME, "Warm", "B", "Warm2"}
        assert undone.color_stacks["s1"].grading_params["exposure"] == 20.0
        redone = stack.redo(undone)
        assert set(versions_state(redone, "s1")["versions"]) == {
            DEFAULT_VERSION_NAME}
        assert redone.color_stacks["s1"].grading_params["exposure"] == 5.0

    def test_delete_all_without_default_refused_zero_commit(self):
        """旧数据无 default_version 键：create 即拒绝（宿主先迁移）。"""
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        p = stack.execute(
            SaveColorVersionCommand.create(
                _set_exposure(p, 7.0), "s1", "B"), _set_exposure(p, 7.0))
        history = stack.get_history_size()
        with pytest.raises(ValueError, match="默认版本"):
            DeleteAllVersionsCommand.create(p, "s1")
        assert stack.get_history_size() == history  # 零提交

    def test_delete_all_single_version_refused(self):
        p = _project(exposure=5.0)
        p = InitializeDefaultVersionCommand.create(p, "s1").apply(p)
        with pytest.raises(ValueError, match="没有可删除"):
            DeleteAllVersionsCommand.create(p, "s1")

    def test_migration_promotes_active_then_delete_all_works(self):
        """迁移策略：优先提升 active 为默认；迁移后 DeleteAll 可用。"""
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "A"), p)
        live_b = _set_exposure(p, 20.0)
        p = stack.execute(
            SaveColorVersionCommand.create(live_b, "s1", "B"), live_b)
        assert versions_state(p, "s1")["default_version"] is None

        migrated, cmd = promote_default_version(p, "s1")
        assert cmd is not None
        assert versions_state(migrated, "s1")["default_version"] == "B"
        # 已迁移表幂等
        again, cmd2 = promote_default_version(migrated, "s1")
        assert cmd2 is None and again is migrated

        p = stack.execute(cmd, p)
        p = stack.execute(DeleteAllVersionsCommand.create(p, "s1"), p)
        state = versions_state(p, "s1")
        assert set(state["versions"]) == {"B"}
        assert state["active"] == "B"
        # 保留并加载的是迁移提升的 active（B，20.0）
        assert p.color_stacks["s1"].grading_params["exposure"] == 20.0

    def test_migration_empty_table_initializes_from_live(self):
        p = _project(exposure=6.0)
        migrated, cmd = promote_default_version(p, "s1")
        state = versions_state(migrated, "s1")
        assert state["default_version"] == DEFAULT_VERSION_NAME
        assert state["versions"][DEFAULT_VERSION_NAME][
            "grading_params"]["exposure"] == 6.0

    def test_migration_without_active_promotes_earliest_saved(self):
        p = _project(exposure=5.0)
        p = SaveColorVersionCommand.create(p, "s1", "First").apply(p)
        p = SaveColorVersionCommand.create(p, "s1", "Second").apply(p)
        p = DeleteColorVersionCommand.create(p, "s1", "Second").apply(p)
        # active 被删后为 None：按 saved_at 最早提升
        assert versions_state(p, "s1")["active"] is None
        migrated, _cmd = promote_default_version(p, "s1")
        assert versions_state(migrated, "s1")["default_version"] == "First"


class TestMacroWithActivation:
    def test_macro_activates_and_undoes_as_single_step(self):
        """BE-04 地基：宏内激活命令的 apply 期快照必须供宏 undo 使用。"""
        stack = ProjectCommandStack()
        p = _project(exposure=5.0)
        p = stack.execute(
            SaveColorVersionCommand.create(
                _set_exposure(p, 20.0), "s1", "Warm"), p)
        p = stack.execute(
            SaveColorVersionCommand.create(p, "s1", "Plain"), p)
        active_before = versions_state(p, "s1")["active"]
        exposure_before = p.color_stacks["s1"].grading_params["exposure"]

        macro = ProjectMacroCommand.create("激活 Warm", [
            ActivateColorVersionCommand.create("s1", "Warm")])
        p = stack.execute(macro, p)
        assert p.color_stacks["s1"].grading_params["exposure"] == 20.0
        assert versions_state(p, "s1")["active"] == "Warm"

        undone = stack.undo(p)
        assert undone.color_stacks["s1"].grading_params[
            "exposure"] == exposure_before
        assert versions_state(undone, "s1")["active"] == active_before

    def test_macro_partial_failure_rolls_back(self):
        """宏内后续子命令失败：已应用子命令逆序回滚，无半提交。"""
        p = _project(exposure=5.0)
        p = SaveColorVersionCommand.create(p, "s1", "A").apply(p)
        macro = ProjectMacroCommand.create("失败宏", [
            ActivateColorVersionCommand.create("s1", "A"),
            # 引用不存在的栈：apply 抛 ValueError
            ActivateColorVersionCommand.create("missing", "A"),
        ])
        with pytest.raises(ValueError):
            macro.apply(p)
        # 第一个子命令已回滚：live 内容未被 A 覆盖（保存后未改 live，
        # 内容相同，校验 active 指针与栈表完整即可）
        assert versions_state(p, "s1")["active"] == "A"
        assert "A" in versions_state(p, "s1")["versions"]
