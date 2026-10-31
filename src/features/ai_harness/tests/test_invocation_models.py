"""ai_harness invocation_models 单元测试（plan §10.2 P0.1）。

覆盖：

- Invocation / RunPolicy / RunRecord / RunEvent / RunStatus 构造与校验。
- source / permission / scope 必填与非法值拒绝。
- RunStatus 合法 / 非法迁移。
- sanitize_for_storage 脱敏（绝对路径 / secret / 递归 dict/list/tuple）。
- Invocation.with_skill 不可变更新。

红线对照：

- source 必须显式（plan §1.4）。
- scope.project_id 必填；analyze_all_assets 默认 False（plan §0.3 第 6 条）。
- permission 不存在"完全访问"等级（plan §10.2 P0.3）。
- 不存 secret / 绝对路径（sanitize_for_storage）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.invocation_models import (
    ArtifactRef,
    Invocation,
    InvocationScope,
    InvocationSource,
    JobRef,
    PermissionLevel,
    RunEvent,
    RunEventKind,
    RunPolicy,
    RunRecord,
    RunStatus,
    sanitize_for_storage,
)


# ================================================================
# Invocation / Scope / Policy 校验
# ================================================================


class TestInvocationScope:
    def test_project_id_required(self):
        with pytest.raises(ValueError):
            InvocationScope(project_id="")

    def test_analyze_all_defaults_false(self):
        s = InvocationScope(project_id="p1")
        assert s.analyze_all_assets is False
        # 默认只看已选素材
        assert s.effective_asset_ids == ()

    def test_effective_asset_ids_returns_selected_when_not_all(self):
        s = InvocationScope(project_id="p1", selected_asset_ids=("a1", "a2"))
        assert s.effective_asset_ids == ("a1", "a2")

    def test_effective_asset_ids_empty_when_all(self):
        s = InvocationScope(
            project_id="p1", selected_asset_ids=("a1",), analyze_all_assets=True,
        )
        # 全库时返回空占位（调用方据此要求二次确认）
        assert s.effective_asset_ids == ()

    def test_to_dict_roundtrip(self):
        s = InvocationScope(project_id="p1", timeline_id="t1", selected_asset_ids=("a",))
        d = s.to_dict()
        assert d["project_id"] == "p1"
        assert d["selected_asset_ids"] == ["a"]


class TestRunPolicy:
    def test_defaults_read_only(self):
        p = RunPolicy()
        assert p.permission == PermissionLevel.READ_ONLY
        assert p.can_write_timeline() is False

    def test_unknown_permission_rejected(self):
        with pytest.raises(ValueError):
            RunPolicy(permission="god_mode")

    def test_unknown_timeline_write_rejected(self):
        with pytest.raises(ValueError):
            RunPolicy(timeline_write="always")

    def test_apply_requires_each_approval(self):
        p = RunPolicy(
            permission=PermissionLevel.APPLY_WITH_EACH_APPROVAL,
            timeline_write="approval_required",
        )
        assert p.can_write_timeline() is True

    def test_propose_cannot_write(self):
        # propose 等级 + approval_required 仍不能直接写
        p = RunPolicy(permission=PermissionLevel.PROPOSE, timeline_write="approval_required")
        assert p.can_write_timeline() is False

    def test_forbidden_timeline_write(self):
        p = RunPolicy(
            permission=PermissionLevel.APPLY_WITH_EACH_APPROVAL,
            timeline_write="forbidden",
        )
        assert p.can_write_timeline() is False

    def test_at_least_ordering(self):
        assert PermissionLevel.at_least("read_only", "read_only")
        assert PermissionLevel.at_least("apply_with_each_approval", "read_only")
        assert PermissionLevel.at_least("apply_with_each_approval", "propose")
        assert not PermissionLevel.at_least("read_only", "propose")


class TestInvocation:
    def test_chat_source_ok_without_skill(self):
        inv = Invocation(
            source=InvocationSource.CHAT,
            scope=InvocationScope(project_id="p1"),
        )
        assert inv.skill_id == ""
        assert inv.invocation_id  # auto-generated

    def test_unknown_source_rejected(self):
        with pytest.raises(ValueError):
            Invocation(source="cli", scope=InvocationScope(project_id="p1"))

    def test_workflow_requires_skill_id(self):
        with pytest.raises(ValueError):
            Invocation(
                source=InvocationSource.WORKFLOW,
                scope=InvocationScope(project_id="p1"),
            )

    def test_developer_requires_skill_id(self):
        with pytest.raises(ValueError):
            Invocation(
                source=InvocationSource.DEVELOPER,
                scope=InvocationScope(project_id="p1"),
            )

    def test_with_skill_immutable(self):
        inv = Invocation(source=InvocationSource.CHAT, scope=InvocationScope(project_id="p1"))
        inv2 = inv.with_skill("topos.subtitle_draft")
        assert inv.skill_id == ""           # 原对象不变
        assert inv2.skill_id == "topos.subtitle_draft"
        assert inv2.invocation_id == inv.invocation_id

    def test_to_public_dict_redacts(self):
        inv = Invocation(
            source=InvocationSource.CHAT,
            scope=InvocationScope(project_id="p1"),
            user_text="my key=sk-1234567890abcdef please",
        )
        d = inv.to_public_dict()
        assert "sk-1234567890abcdef" not in str(d)


# ================================================================
# RunStatus 状态机
# ================================================================


class TestRunStatus:
    def test_legal_transitions(self):
        assert RunStatus.can_transition("draft", "queued")
        assert RunStatus.can_transition("running", "succeeded")
        assert RunStatus.can_transition("running", "cancelled")
        assert RunStatus.can_transition("waiting_for_user", "running")

    def test_illegal_transitions(self):
        assert not RunStatus.can_transition("succeeded", "running")
        assert not RunStatus.can_transition("failed", "running")
        assert not RunStatus.can_transition("cancelled", "running")

    def test_transition_raises_on_illegal(self):
        with pytest.raises(ValueError):
            RunStatus.transition("succeeded", "running")

    def test_transition_returns_target(self):
        assert RunStatus.transition("draft", "queued") == "queued"

    def test_terminal_states(self):
        assert RunStatus.is_terminal("succeeded")
        assert RunStatus.is_terminal("cancelled")
        assert not RunStatus.is_terminal("running")
        assert not RunStatus.is_terminal("waiting_for_user")

    def test_active_states(self):
        assert RunStatus.is_active("running")
        assert RunStatus.is_active("queued")
        assert RunStatus.is_active("waiting_for_user")
        assert not RunStatus.is_active("succeeded")

    def test_no_terminal_self_transition(self):
        # 终态不能再迁移
        for t in RunStatus.TERMINAL_STATES:
            for target in RunStatus.TERMINAL_STATES | RunStatus.ACTIVE_STATES:
                assert not RunStatus.can_transition(t, target), (t, target)


# ================================================================
# sanitize_for_storage
# ================================================================


class TestSanitize:
    def test_redacts_absolute_unix_user_path(self):
        s = sanitize_for_storage("open /Users/testuser/secret/file.mov")
        assert "/Users/testuser" not in s
        assert "<absolute-path>" in s

    def test_redacts_absolute_windows_path(self):
        s = sanitize_for_storage("load C:\\Users\\me\\key.json")
        assert "C:\\Users" not in s

    def test_redacts_openai_style_key(self):
        s = sanitize_for_storage("token sk-1234567890abcdefghijklmnopqrstuvwxyz")
        assert "sk-1234567890abcdefghijklmnopqrstuvwxyz" not in s

    def test_redacts_api_key_assignment(self):
        s = sanitize_for_storage("api_key=ABCDEFGHIJKLMNOP1234567890")
        assert "ABCDEFGHIJKLMNOP1234567890" not in s

    def test_redacts_authorization_header(self):
        s = sanitize_for_storage("Authorization: Bearerabcdef12345678901234")
        assert "Bearerabcdef12345678901234" not in s

    def test_recurses_dict(self):
        d = sanitize_for_storage({"a": {"b": "/Users/testuser/x"}})
        assert "/Users/testuser" not in str(d)

    def test_recurses_list_and_tuple(self):
        lst = sanitize_for_storage(["/Users/testuser/x", "ok"])
        assert "<absolute-path>" in lst[0]
        tpl = sanitize_for_storage(("/Users/testuser/x", "ok"))
        assert "<absolute-path>" in tpl[0]
        assert isinstance(tpl, tuple)

    def test_passthrough_numbers_and_none(self):
        assert sanitize_for_storage(42) == 42
        assert sanitize_for_storage(None) is None
        assert sanitize_for_storage(True) is True

    def test_does_not_mangle_relative_safe_text(self):
        s = sanitize_for_storage("分析当前素材并生成字幕")
        assert s == "分析当前素材并生成字幕"


# ================================================================
# RunEvent / RunRecord / refs
# ================================================================


class TestRunRecord:
    def test_default_record_is_draft(self):
        r = RunRecord()
        assert r.status == RunStatus.DRAFT
        assert r.is_terminal is False
        assert r.is_active is False  # draft 不是 active

    def test_to_public_dict_shape(self):
        r = RunRecord(
            run_id="r1",
            jobs=(JobRef(job_id="j1", kind="analysis", status="running"),),
            artifacts=(ArtifactRef(artifact_id="a1", artifact_type="transcript_ref"),),
        )
        d = r.to_public_dict()
        assert d["run_id"] == "r1"
        assert d["jobs"][0]["job_id"] == "j1"
        assert d["artifacts"][0]["artifact_type"] == "transcript_ref"


class TestRunEvent:
    def test_event_has_ids(self):
        e = RunEvent(run_id="r1", kind=RunEventKind.RUN_CREATED)
        assert e.event_id
        assert e.kind == "run.created"

    def test_event_public_dict_sanitizes_payload(self):
        e = RunEvent(
            run_id="r1", kind=RunEventKind.RUN_CREATED,
            payload={"path": "/Users/testuser/secret", "ok": "yes"},
        )
        d = e.to_public_dict()
        assert "/Users/testuser" not in str(d)
        assert d["payload"]["ok"] == "yes"
