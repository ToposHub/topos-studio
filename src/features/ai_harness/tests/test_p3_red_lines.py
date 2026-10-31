"""P3 三条红线集成验收（plan §10.5 P3.4）。

plan §10.5 三个方向的完成定义里的红线：

1. **子智能体无独立 approval / 不写时间线**：LlmRoleExecutor 不持有 grant；
   SingleWriterPolicy 仍强制；任何角色执行器都不写 timeline。
2. **MCP 默认只读 / 本机 / 会话级启用**：未 allowlist 不执行；写操作弹审批；
   外部 server 输出只作数据。
3. **生成不自动进 timeline**：候选进 GenerationBin + RunRecord artifact；
   无 approval/commit/apply 事件；取消/失败候选不进 bin。

本文件做跨模块集成验证，证明 P3 落地后三条红线都成立。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.generation_adapter import GenerationHarnessAdapter
from src.features.ai_harness.invocation_models import (
    Invocation, InvocationScope, PermissionLevel, RunPolicy, RunStatus,
)
from src.features.ai_harness.llm_role_executor import LlmRoleExecutor
from src.features.ai_harness.mcp_outbound import (
    McpConnectionProfile, McpOutboundClient, McpServerRegistry,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.edit.ai_editing.generation_models import GenerationConsent
from src.features.edit.ai_editing.generation_provider import (
    DeterministicGenerationProvider, ProviderRegistry,
)
from src.features.edit.ai_editing.media_generation_service import GenerationService
from src.features.edit.ai_editing.task_graph import (
    ContextSlice, SingleWriterPolicy, TaskNode, TaskRole,
)


# ================================================================
# 红线 1：子智能体无独立 approval / 不写时间线
# ================================================================


class TestSubagentNoApproval:
    """plan §10.5 P3.1 完成定义：没有任何角色能直接获得 ApprovalGrant 或写时间线。"""

    def test_llm_executor_has_no_approval_fields(self):
        ex = LlmRoleExecutor(skill_id="t", skill_version="1")
        for forbidden in ("approval", "grant", "approval_service", "approval_token",
                          "write_timeline", "commit", "apply_plan"):
            assert not hasattr(ex, forbidden)

    def test_single_writer_policy_rejects_timeline_writing_subagent(self):
        """SingleWriterPolicy.assert_subagent_no_write 强制：can_write_timeline=True
        的 TaskNode 永远被拒。"""
        policy = SingleWriterPolicy()
        bad_node = TaskNode(task_id="t1", role=TaskRole.QA_REVIEWER, can_write_timeline=True)
        with pytest.raises(PermissionError):
            policy.assert_subagent_no_write(bad_node)

    def test_role_executors_never_return_timeline_writes(self):
        """无论是 LLM 还是确定性执行器，返回的都是 typed artifact，不是 timeline 写。"""
        from src.features.edit.ai_editing.skill_runtime import DeterministicRoleExecutor
        det = DeterministicRoleExecutor(skill_id="t", skill_version="1")
        llm = LlmRoleExecutor(skill_id="t", skill_version="1")  # 无 model → 降级
        ctx = ContextSlice(brief_summary="b")
        for executor in (det, llm):
            art = executor.execute(TaskRole.CREATIVE_DIRECTOR, ctx, ())
            # 返回的是 ScriptDraft，没有 timeline 写字段
            assert not hasattr(art, "operations")
            assert not hasattr(art, "timeline_id")


# ================================================================
# 红线 2：MCP 默认只读 / 本机 / 会话级
# ================================================================


class TestMcpReadOnlyLocalSession:
    """plan §10.5 P3.2 完成定义：默认只读、本机、会话级启用。"""

    def test_default_profile_is_read_only_local(self):
        p = McpConnectionProfile(
            profile_id="x", server_name="x",  # 用默认值
        )
        assert p.is_read_only_scope is True
        assert p.is_local is True
        assert p.enabled_by_default is False  # 默认会话级关闭

    def test_not_allowlisted_server_never_executes(self):
        reg = McpServerRegistry()
        client = McpOutboundClient(reg, transport_factory=lambda p: _T())
        r = client.call_tool("unknown", "get_x", {})
        assert r["ok"] is False
        assert r["decision"] == "denied_not_allowlisted"

    def test_write_operation_requires_approval(self):
        reg = McpServerRegistry()
        reg.register(McpConnectionProfile(
            profile_id="s", server_name="s", enabled_by_default=True,
        ))
        client = McpOutboundClient(reg, transport_factory=lambda p: _T())
        r = client.call_tool("s", "write_data", {})
        assert r["decision"] == "denied_write_needs_approval"
        # 审批后才能调
        r2 = client.call_tool("s", "write_data", {}, write_approved=True)
        assert r2["ok"] is True

    def test_external_output_returned_as_data_only(self):
        """外部 server 输出只作数据（不执行第三方代码）。"""
        reg = McpServerRegistry()
        reg.register(McpConnectionProfile(
            profile_id="s", server_name="s", enabled_by_default=True,
        ))
        client = McpOutboundClient(reg, transport_factory=lambda p: _T())
        r = client.call_tool("s", "get_x", {})
        assert isinstance(r["result"], dict)
        # 不返回可执行对象
        assert not callable(r["result"])

    def test_session_disabled_blocks(self):
        reg = McpServerRegistry()
        reg.register(McpConnectionProfile(
            profile_id="s", server_name="s", enabled_by_default=False,
        ))
        client = McpOutboundClient(reg, transport_factory=lambda p: _T())
        r = client.call_tool("s", "get_x", {})
        assert r["decision"] == "denied_not_enabled"

    def test_no_secret_in_profile(self):
        """profile 不存 secret（plan §1.3）。"""
        p = McpConnectionProfile(
            profile_id="s", server_name="s", credential_ref="keychain:s",
        )
        for forbidden in ("secret", "api_key", "password", "token"):
            assert not hasattr(p, forbidden)


class _T:
    """fake stdio transport。

    AUD-22020 修复后，tools/list 发现失败的 server 按写处理（fail-closed），
    纯读工具也会被拒——fake 需像真实 server 一样应答 tools/list（get_x 声明
    readOnlyHint；write_data 无注解、靠名字启发式判写）。
    """
    def send_request(self, req):
        if req["method"] == "tools/list":
            return {"result": {"tools": [
                {"name": "get_x", "inputSchema": {"type": "object"},
                 "annotations": {"readOnlyHint": True}},
                {"name": "write_data", "inputSchema": {"type": "object"}},
            ], "protocolVersion": "2024"}}
        return {"result": {"content": []}}
    def close(self):
        pass


# ================================================================
# 红线 3：生成不自动进 timeline
# ================================================================


class TestGenerationNoAutoTimeline:
    """plan §10.5 P3.3 完成定义：结果不自动进入 timeline；取消/失败不污染工程。"""

    def _adapter(self):
        reg = ProviderRegistry()
        reg.register(DeterministicGenerationProvider(), preferred=True)
        svc = GenerationService(reg)
        rt = HarnessRuntime()
        return GenerationHarnessAdapter(svc, rt), rt

    def test_no_timeline_write_events(self):
        ad, rt = self._adapter()
        inv = Invocation(
            source="chat", scope=InvocationScope(project_id="p1"),
            policy=RunPolicy(permission=PermissionLevel.PROPOSE, allow_generation=True),
            inputs={
                "modality": "image", "prompt": "cat",
                "consent": GenerationConsent(
                    confirmed=True, provider_id="deterministic-stub",
                    cost_acknowledged=True, upload_acknowledged=True,
                    copyright_acknowledged=True,
                ),
            },
            parent_session_id="sess1",
        )
        rid = ad.invoke(inv)
        rec = rt.get_run(rid)
        # 成功，但无任何 timeline 写入事件
        assert rec.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)
        write_events = [e for e in rec.events
                        if any(k in e.kind for k in ("approval", "commit", "apply"))]
        assert write_events == []

    def test_candidates_are_artifact_refs_not_clips(self):
        ad, rt = self._adapter()
        inv = Invocation(
            source="chat", scope=InvocationScope(project_id="p1"),
            policy=RunPolicy(allow_generation=True),
            inputs={
                "modality": "music", "prompt": "jazz",
                "consent": GenerationConsent(
                    confirmed=True, provider_id="deterministic-stub",
                    cost_acknowledged=True, upload_acknowledged=True,
                    copyright_acknowledged=True,
                ),
            },
        )
        rid = ad.invoke(inv)
        rec = rt.get_run(rid)
        for a in rec.artifacts:
            # artifact 引用，无 timeline 字段
            assert not hasattr(a, "timeline_id")
            assert not hasattr(a, "track_name")
            assert not hasattr(a, "clip_id")

    def test_consent_incomplete_does_not_generate(self):
        """consent 不完整 → 不生成（候选不进 bin，不污染工程）。"""
        ad, rt = self._adapter()
        inv = Invocation(
            source="chat", scope=InvocationScope(project_id="p1"),
            policy=RunPolicy(allow_generation=True),
            inputs={"modality": "image", "prompt": "cat"},  # 无 consent
        )
        rid = ad.invoke(inv)
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.WAITING_FOR_USER
        # 无候选 artifact（未生成）
        assert rec.artifacts == ()


# ================================================================
# 综合：三条红线同时成立
# ================================================================


class TestAllP3RedLines:
    def test_three_red_lines_independent(self):
        """三条红线各自独立成立（不因一个被绕过而其它失效）。"""
        # 1. 子智能体无 approval
        ex = LlmRoleExecutor(skill_id="t", skill_version="1")
        assert not hasattr(ex, "approval")
        # 2. MCP 默认只读
        p = McpConnectionProfile(profile_id="x", server_name="x")
        assert p.is_read_only_scope
        # 3. 生成不写 timeline（结构性：GenerationHarnessAdapter 无 timeline 方法）
        reg = ProviderRegistry()
        reg.register(DeterministicGenerationProvider(), preferred=True)
        ad = GenerationHarnessAdapter(GenerationService(reg), HarnessRuntime())
        for forbidden in ("write_timeline", "commit", "apply_plan", "insert_clip"):
            assert not callable(getattr(ad, forbidden, None))
