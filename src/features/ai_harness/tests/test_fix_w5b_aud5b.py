"""AUD-22020 回归：写审批闸门基于补发现 manifest 后的最终工具注解。

旧行为（缺陷）：会话内首次调用（manifest 未缓存）时 ``is_write`` 用
``tool_spec=None`` 的名字启发式先算，补发现后 ``tool_spec`` 已更新但
``is_write`` 不重算——注解标记为写、名字却不含 write/create/update/...
启发词的工具（如 ``rename_clip``）在 ``write_approved=False`` 下被执行，
审计 ``write_operation`` 也沿用旧值。

修复后契约：

- 补发现 manifest → 基于最终工具注解重算 is_write → 重走审批闸门；
- 发现失败（注解无法确定）→ 按写处理（fail-closed）；
- 名字启发式命中的写仍在 discovery **之前**拒绝（被拒的写绝不触发远程
  请求的原设计不变式保留）；
- 审计事件的 ``write_operation`` 用最终值。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.mcp_outbound import (
    McpAuthorizationLog,
    McpConnectionProfile,
    McpOutboundClient,
    McpServerRegistry,
)


def _profile(**kw) -> McpConnectionProfile:
    defaults = dict(
        profile_id="srv", server_name="test-mcp", transport="local_stdio",
        data_scope="read_only", enabled_by_default=True,
    )
    defaults.update(kw)
    return McpConnectionProfile(**defaults)


#: rename_clip / deploy_config 名字不含 write/create/update/... 启发词，
#: 但 annotations 声明为写 —— 旧的"先算启发式缓存值"会把它们放行。
_ANNOTATION_TOOLS = [
    {"name": "rename_clip", "inputSchema": {"type": "object"},
     "annotations": {"readOnlyHint": False}},
    {"name": "deploy_config", "inputSchema": {"type": "object"},
     "annotations": {"destructiveHint": True}},
    {"name": "read_frame", "inputSchema": {"type": "object"},
     "annotations": {"readOnlyHint": True}},
]


class _ManifestTransport:
    """应答 tools/list 的 fake transport；记录全部请求。"""

    def __init__(self, tools: list) -> None:
        self.requests: list[dict] = []
        self._tools = tools

    def send_request(self, request: dict) -> dict:
        self.requests.append(request)
        if request["method"] == "tools/list":
            return {"result": {"tools": self._tools, "protocolVersion": "2024"}}
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    def close(self) -> None:
        pass


def _client(transport) -> tuple[McpOutboundClient, McpAuthorizationLog]:
    registry = McpServerRegistry()
    registry.register(_profile())
    log = McpAuthorizationLog()
    client = McpOutboundClient(registry, auth_log=log,
                               transport_factory=lambda p: transport)
    return client, log


# ================================================================
# 基于最终注解的写审批
# ================================================================


class TestWriteGateUsesFinalAnnotations:
    def test_annotation_marked_write_denied_without_approval(self) -> None:
        """readOnlyHint=False + 名字无启发词 → 补发现后判写，无审批即拒。"""
        transport = _ManifestTransport(_ANNOTATION_TOOLS)
        client, _ = _client(transport)
        result = client.call_tool("srv", "rename_clip", {})  # 首调，manifest 未缓存
        assert result["ok"] is False
        assert result["decision"] == "denied_write_needs_approval"
        # 被拒的调用绝不能发 tools/call（只允许只读 tools/list 发现）。
        assert [q["method"] for q in transport.requests] == ["tools/list"]
        client.close()

    def test_destructive_hint_denied_without_approval(self) -> None:
        transport = _ManifestTransport(_ANNOTATION_TOOLS)
        client, _ = _client(transport)
        result = client.call_tool("srv", "deploy_config", {})
        assert result["decision"] == "denied_write_needs_approval"
        client.close()

    def test_annotation_write_allowed_after_approval(self) -> None:
        transport = _ManifestTransport(_ANNOTATION_TOOLS)
        client, log = _client(transport)
        result = client.call_tool("srv", "rename_clip", {}, write_approved=True)
        assert result["ok"] is True
        assert result["decision"] == "allowed"
        # 审计 write_operation 用补发现后的最终值（不再是旧的 False）。
        allowed = [e for e in log.events() if e.decision == "allowed"]
        assert allowed and allowed[-1].write_operation is True
        client.close()

    def test_readonly_hint_tool_allowed_without_approval(self) -> None:
        """真只读注解的工具首调仍免审批（修复不误伤读路径）。"""
        transport = _ManifestTransport(_ANNOTATION_TOOLS)
        client, log = _client(transport)
        result = client.call_tool("srv", "read_frame", {})
        assert result["ok"] is True
        allowed = [e for e in log.events() if e.decision == "allowed"]
        assert allowed and allowed[-1].write_operation is False
        client.close()

    def test_denied_write_event_recorded_with_write_flag(self) -> None:
        transport = _ManifestTransport(_ANNOTATION_TOOLS)
        client, log = _client(transport)
        client.call_tool("srv", "rename_clip", {})
        denied = [e for e in log.events()
                  if e.decision == "denied_write_needs_approval"]
        assert denied and denied[-1].write_operation is True
        client.close()


# ================================================================
# fail-closed 与预闸不变式
# ================================================================


class TestFailClosedAndPreGate:
    def test_discovery_failure_treated_as_write(self) -> None:
        """发现失败 → fail-closed 拒绝（锁定旧行为翻转，AUD-22121）。

        AUD-22020 过渡语义把发现失败记为 ``denied_write_needs_approval``；
        AUD-22121 修复后改为专用 ``error_discovery_failed``——未校验的调用
        绝不发往外部 server（无论是否已获写审批），授权审计仍按写处理
        （write_operation=True，fail-closed 语义不变）。
        """

        class _Crash:
            def send_request(self, request: dict) -> dict:
                raise RuntimeError("no tools/list")

            def close(self) -> None:
                pass

        client, _ = _client(_Crash())
        result = client.call_tool("srv", "get_x", {})  # 名字无启发词
        assert result["ok"] is False
        assert result["decision"] == "error_discovery_failed"
        events = client.auth_log.events()
        assert events[-1].decision == "error"
        assert events[-1].write_operation is True
        client.close()

    def test_heuristic_pre_gate_denies_before_any_remote_request(self) -> None:
        """启发式命中的写在 discovery 前即拒——远程零请求（原不变式保留）。"""
        transport = _ManifestTransport(_ANNOTATION_TOOLS)
        client, _ = _client(transport)
        result = client.call_tool("srv", "write_file", {})
        assert result["decision"] == "denied_write_needs_approval"
        assert transport.requests == []
        client.close()
