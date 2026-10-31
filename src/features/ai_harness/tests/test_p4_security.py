"""P4.5 测试：第三方 Skill 安全红线（plan §10.6 / §1.4 / §5.3）。

> 第三方不得执行任意 Python、shell、文件系统或 UI 自动化。

本测试做"深度防御"验证：跨 P4.1–P4.4 多层防御，确保第三方 / local_dev
Skill 在任何路径下都无法取得任意代码执行能力。覆盖的攻击向量：

1. **ExecutorKind 构造期**：直接构造 ``python_exec`` / ``shell_exec`` 等被拒；
2. **草稿期**：SkillDraft.executor_kind 不得为任意代码；
3. **dry-run 期**：dry-run 不执行任意代码（事件流无 process_spawn/shell_exec）；
4. **发布期**：发布门禁拒绝 executor_kind=任意代码的草稿；
5. **registry 期**：SkillRegistry.register 拒绝声明 FORBIDDEN_CAPABILITIES 的
   tool allowlist（exec_python / call_shell / click_ui 等）；
6. **manifest 期**：local_dev manifest 永远 unverified；
7. **graph template 期**：WorkflowDefinition 中的 spec_id 不得引用任意代码节点。

红线：每一层都独立拒绝；任何单层失守不能让第三方拿到任意代码执行。
"""
from __future__ import annotations

import pytest

from src.features.ai_harness.skill_dry_run import SkillDryRunner, make_voiceover_polish_fixture
from src.features.ai_harness.skill_release import (
    PublishDenyCode,
    SkillRelease,
    SkillReleaseGate,
)
from src.features.ai_harness.skill_studio_drafts import (
    ExecutorKind,
    SkillDraft,
    new_draft_from_template,
    new_draft_from_workflow,
)
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_VOICEOVER_POLISH,
    SkillManifest,
    SkillSource,
    ToolPolicy,
)
from src.features.edit.ai_editing.skill_registry import SkillRegistry


# 受禁止的 executor kind 候选（攻击向量）
FORBIDDEN_EXEC_KINDS = (
    "python_exec", "shell_exec", "code_exec",
    "fs_write", "fs_read_arbitrary", "ui_automation",
    "eval", "exec", "subprocess", "os_system",
)

# ToolFacade.FORBIDDEN_CAPABILITIES（镜像）
FORBIDDEN_TOOLS = (
    "exec_python", "execute_code", "read_arbitrary_path",
    "call_shell", "click_ui", "inject_glsl", "download_url",
)


def _is_error_diag(d: object) -> bool:
    """判断一个 Diagnostic 是否 ERROR 级。

    Diagnostic.severity 是 :class:`DiagnosticSeverity` 枚举（``str`` Enum 子类）；
    其 ``.value`` 为 ``"error"``。本辅助兼容枚举与字符串两种形态。
    """
    sev = getattr(d, "severity", None)
    if sev is None:
        return False
    if hasattr(sev, "value"):
        return str(sev.value).lower() == "error"
    return str(sev).lower() == "error"


def _ready_draft() -> SkillDraft:
    d = new_draft_from_template(
        author="third-party", template=OFFICIAL_VOICEOVER_POLISH, title="第三方 Skill",
    )
    return d.update_field("title", "第三方 v1", kind="major")


# ---------------------------------------------------------------------- #
# 层 1：ExecutorKind 构造期防御
# ---------------------------------------------------------------------- #


class TestExecutorKindConstructionDefense:
    @pytest.mark.parametrize("forbidden", FORBIDDEN_EXEC_KINDS)
    def test_forbidden_kind_rejected_at_construction(self, forbidden: str) -> None:
        """红线：所有已知攻击向量在 ExecutorKind 构造期被拒。"""
        with pytest.raises(ValueError, match="forbidden|executor|arbitrary"):
            ExecutorKind(forbidden)

    def test_known_kinds_all_non_arbitrary(self) -> None:
        for kind in (ExecutorKind.WORKFLOW_RUNNER, ExecutorKind.TASKGRAPH_ROLES, ExecutorKind.EDITORIAL_BUILDER):
            assert not kind.is_arbitrary_code

    def test_kind_for_workflow_never_returns_arbitrary(self) -> None:
        """即便 WorkflowDefinition 含恶意节点 spec，kind_for_workflow 仍返回受控值。"""
        malicious_wf = {
            "workflow_id": "evil",
            "nodes": [{"node_id": "hack", "spec_id": "exec.python"}],
        }
        kind = ExecutorKind.kind_for_workflow(malicious_wf)
        assert not kind.is_arbitrary_code


# ---------------------------------------------------------------------- #
# 层 2：草稿期防御
# ---------------------------------------------------------------------- #


class TestDraftDefense:
    def test_draft_executor_kind_never_arbitrary(self) -> None:
        d = _ready_draft()
        assert not d.executor_kind.is_arbitrary_code

    def test_draft_cannot_set_arbitrary_executor(self) -> None:
        d = _ready_draft()
        # 无法直接构造 ExecutorKind(forbidden) → 无法 with_executor_kind
        for forbidden in FORBIDDEN_EXEC_KINDS:
            with pytest.raises(ValueError):
                d.with_executor_kind(ExecutorKind(forbidden))

    def test_local_dev_draft_always_unverified(self) -> None:
        d = _ready_draft()
        assert d.manifest.source == SkillSource.LOCAL_DEV
        assert d.unverified is True


# ---------------------------------------------------------------------- #
# 层 3：dry-run 期防御
# ---------------------------------------------------------------------- #


class TestDryRunDefense:
    def test_dry_run_no_arbitrary_code_events(self) -> None:
        d = _ready_draft()
        outcome = SkillDryRunner().run(d, make_voiceover_polish_fixture())
        kinds = {e.kind for e in outcome.run_record.events}
        for forbidden in ("process_spawn", "shell_exec", "code_exec", "fs_write", "ui_automation"):
            assert forbidden not in kinds

    def test_dry_run_no_network_events(self) -> None:
        d = _ready_draft()
        outcome = SkillDryRunner().run(d, make_voiceover_polish_fixture())
        kinds = {e.kind for e in outcome.run_record.events}
        assert "cloud_upload" not in kinds
        assert "network_request" not in kinds

    def test_dry_run_no_timeline_commit(self) -> None:
        """红线：dry-run 不写时间线。"""
        d = _ready_draft()
        outcome = SkillDryRunner().run(d, make_voiceover_polish_fixture())
        kinds = {e.kind for e in outcome.run_record.events}
        assert "approval.granted" not in kinds
        assert "timeline_committed" not in kinds


# ---------------------------------------------------------------------- #
# 层 4：发布门禁期防御
# ---------------------------------------------------------------------- #


class TestPublishGateDefense:
    def test_publish_gate_rejects_arbitrary_executor(self) -> None:
        """红线：发布门禁二次防御（即便绕过 ExecutorKind 构造，仍拒绝）。"""
        from dataclasses import replace

        class _ForgedArbitraryKind:
            """绕过 ExecutorKind 构造期防御的伪造执行后端（攻击模拟）。

            正常途径无法构造（ExecutorKind("python_exec") 抛 ValueError）；
            攻击者可能经反序列化 / replace 注入伪造对象。门禁只依赖
            ``is_arbitrary_code`` / ``value`` 两个属性，必须独立拒绝。
            """

            value = "python_exec"
            is_arbitrary_code = True

        gate = SkillReleaseGate()
        d = _ready_draft()  # version ≥ 1.0.0：先过版本门，逼出 executor 门
        malicious = replace(d, executor_kind=_ForgedArbitraryKind())
        result = gate.publish(malicious, released_by="third-party")
        # 无条件断言：门禁必须拒绝（success=False 时空跑通过是旧缺陷）
        assert result.success is False
        assert result.deny_code == PublishDenyCode.ARBITRARY_CODE_EXECUTOR
        assert result.release is None
        # 对照：同一草稿未篡改 executor → 门禁通过且产物受控
        ok = gate.publish(d, released_by="third-party")
        assert ok.success
        assert not ok.release.executor_kind.is_arbitrary_code

    def test_release_executor_kind_property_safe(self) -> None:
        """SkillRelease.executor_kind 对合法值受控（见下方未知值回退用例）。"""
        release = SkillRelease.from_draft(
            draft=_ready_draft(), eval_suite_version="v1", released_by="x",
        )
        # 正常 release → 受控
        assert not release.executor_kind.is_arbitrary_code

    def test_release_executor_kind_unknown_value_treated_as_dangerous(self) -> None:
        """docstring 承诺的回退路径（AUD-22706）：未知 executor_kind 值必须
        保守视为任意代码（is_arbitrary_code=True），删除该回退分支时本测试失败。"""
        from dataclasses import replace

        release = SkillRelease.from_draft(
            draft=_ready_draft(), eval_suite_version="v1", released_by="x",
        )
        # 模拟篡改/反序列化注入一个非受控枚举值（ExecutorKind 构造会拒绝它）。
        tampered = replace(release, executor_kind_value="python_exec")
        assert tampered.executor_kind.is_arbitrary_code is True


# ---------------------------------------------------------------------- #
# 层 5：SkillRegistry 注册期防御（FORBIDDEN_CAPABILITIES）
# ---------------------------------------------------------------------- #


class TestRegistryDefense:
    @pytest.mark.parametrize("forbidden_tool", FORBIDDEN_TOOLS)
    def test_registry_rejects_forbidden_tool(self, forbidden_tool: str) -> None:
        """红线：声明 FORBIDDEN_CAPABILITIES 的 Skill 不得注册。"""
        registry = SkillRegistry(register_official=False)
        malicious = SkillManifest(
            skill_id=f"evil.skill.{forbidden_tool}",
            version="1.0.0",
            title="恶意 Skill",
            source=SkillSource.LOCAL_DEV,
            tool_policy=ToolPolicy(allowed_tools=(forbidden_tool,)),
        )
        diags = registry.register(malicious)
        # 必须有 ERROR 级诊断（Diagnostic.severity 是 DiagnosticSeverity 枚举）
        assert any(_is_error_diag(d) for d in diags)
        # 确实没注册进去
        assert registry.get(malicious.skill_id) is None

    def test_registry_validates_tool_call_against_policy(self) -> None:
        """validate_tool_call 在调用前校验（红线：双重过滤）。"""
        registry = SkillRegistry(register_official=False)
        benign = SkillManifest(
            skill_id="benign.skill", version="1.0.0", title="良性",
            source=SkillSource.LOCAL_DEV,
            tool_policy=ToolPolicy(allowed_tools=("topos.search_assets",)),
        )
        registry.register(benign)
        # 允许的工具 → 通过
        diags_ok = registry.validate_tool_call("benign.skill", "topos.search_assets", "read")
        assert not any(_is_error_diag(d) for d in diags_ok)
        # 不在 allowlist 的工具 → 拒
        diags_no = registry.validate_tool_call("benign.skill", "exec_python", "read")
        assert any(_is_error_diag(d) for d in diags_no)


# ---------------------------------------------------------------------- #
# 层 6：local_dev 永远 unverified（source 不可改）
# ---------------------------------------------------------------------- #


class TestLocalDevUnverifiedInvariant:
    def test_release_keeps_local_dev_unverified(self) -> None:
        gate = SkillReleaseGate()
        result = gate.publish(_ready_draft(), released_by="third-party")
        assert result.success
        assert result.release.unverified is True
        assert result.release.manifest.source == SkillSource.LOCAL_DEV

    def test_registry_keeps_local_dev_unverified(self) -> None:
        gate = SkillReleaseGate()
        result = gate.publish(_ready_draft(), released_by="third-party")
        registry = SkillRegistry(register_official=False)
        gate.register_into(registry, result.release)
        got = registry.get(result.release.skill_id)
        assert got is not None
        assert got.unverified is True
        assert got.source == SkillSource.LOCAL_DEV

    def test_release_cannot_escalate_source_to_official(self) -> None:
        """红线：草稿 source 不可在发布过程中升级为 official。"""
        gate = SkillReleaseGate()
        result = gate.publish(_ready_draft(), released_by="third-party")
        # 草稿 local_dev → 发布后仍 local_dev（不是 official）
        assert result.release.manifest.source != SkillSource.OFFICIAL


# ---------------------------------------------------------------------- #
# 层 7：WorkflowDefinition 恶意节点 spec
# ---------------------------------------------------------------------- #


class TestWorkflowNodeSpecDefense:
    def test_dry_run_does_not_execute_malicious_node_spec(self) -> None:
        """WorkflowDefinition 含恶意 spec_id（exec.python）→ dry-run 不执行它。

        红线：spec_id 只是声明；dry-run 只跑受控 fake executor，不按 spec_id
        调用真实代码。恶意 spec_id 被 DAG/ports 检查识别但不会被执行。
        """
        malicious_wf = {
            "workflow_id": "evil-wf",
            "name": "恶意 workflow",
            "nodes": [
                {"node_id": "input", "spec_id": "topos.media.selected_assets"},
                {"node_id": "hack", "spec_id": "exec.python"},  # 恶意
            ],
            "edges": [
                {"from": {"node_id": "input", "port": "out"},
                 "to": {"node_id": "hack", "port": "in"}},
            ],
            "policy": {"network": "ask", "timeline_write": "approval_required"},
        }
        draft = new_draft_from_workflow(
            author="attacker", workflow_definition=malicious_wf, title="恶意",
        )
        draft = draft.update_field("title", "恶意 v1", kind="major")
        outcome = SkillDryRunner().run(draft, make_voiceover_polish_fixture())
        # 不会执行 exec.python —— 事件流无 process_spawn / code_exec
        kinds = {e.kind for e in outcome.run_record.events}
        for forbidden in ("process_spawn", "code_exec", "shell_exec"):
            assert forbidden not in kinds
        # executor_kind 仍是受控（workflow_runner），不是 exec
        assert not draft.executor_kind.is_arbitrary_code
