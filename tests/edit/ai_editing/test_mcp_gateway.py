"""M10 MCP Gateway 单元测试（§3 / §4.2 / §11.1 M10 / §16）。

覆盖：
- McpClientPolicy allowlist + permission ceiling
- McpSession feature flag 检查（mcp_server off → 全拒）
- McpServer JSON-RPC：initialize / tools/list / tools/call
- 动态 manifest：按 client policy 过滤
- tools/call 路由到 ToolFacade.invoke + 审计
- 注入隔离：未授权工具不可调；未 initialize 不可调
- 相同 diagnostics：MCP 走同一 ToolFacade，结果与直接 invoke 一致
- JSON-RPC 错误码（method-not-found / invalid-params）

红线对照：
- MCP 只是 transport，不复制业务（§4.2 step 2）
- 不监听公网（§11.2 step 4，in-process dispatcher）
- 动态暴露工具（§4.2 step 3）
- MCP 输出是非可信数据（§1.2 不变量 #2）
- 相同请求相同 diagnostics（§11.2 完成检查）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.mcp_gateway import (
    McpClientPolicy,
    McpServer,
    McpSession,
    PROTOCOL_VERSION,
    SERVER_NAME,
)
from src.features.edit.ai_editing.tool_call_log import ToolCallLog
from src.features.edit.ai_editing.tool_facade import ToolPermission


# ================================================================
# McpClientPolicy
# ================================================================


class TestClientPolicy:
    def test_default_read_only(self):
        p = McpClientPolicy()
        assert p.max_permission == ToolPermission.READ
        # READ 工具可见，PREVIEW 不可见
        assert p.permits_tool("topos.search_assets", ToolPermission.READ) is True
        assert p.permits_tool("topos.validate_edit_plan", ToolPermission.PREVIEW) is False

    def test_allowlist_filter(self):
        p = McpClientPolicy(
            allowed_tools=("topos.search_assets",),
            max_permission=ToolPermission.READ,
        )
        assert p.permits_tool("topos.search_assets", ToolPermission.READ) is True
        # 在 ceiling 但不在 allowlist
        assert p.permits_tool("topos.get_capabilities", ToolPermission.READ) is False

    def test_permission_ceiling_blocks_write(self):
        p = McpClientPolicy(max_permission=ToolPermission.PREVIEW)
        assert p.permits_tool("topos.apply_edit_plan", ToolPermission.WRITE) is False


# ================================================================
# McpServer 会话管理
# ================================================================


def _make_server(facade=None) -> McpServer:
    """构造一个带 stub facade 的 McpServer（不依赖真实 EditSession）。"""
    facade = facade or _StubFacade()
    return McpServer(facade=facade, call_log=ToolCallLog())


class _StubFacade:
    """duck-typed ToolFacade（避免依赖 EditSession/ApprovalService）。"""

    def __init__(self, tools=None, invoke_result=None) -> None:
        self._tools = tools or _default_tools()
        self._invoke_result = invoke_result or {"ok": True, "segments": [{"id": "s1"}]}
        self.invoke_calls: list[tuple] = []

    def list_tools(self, *, max_permission=ToolPermission.READ):
        order = list(ToolPermission)
        idx = order.index(max_permission)
        allowed = set(order[: idx + 1])
        return [t for t in self._tools if t.permission in allowed]

    def invoke(self, tool_name, args=None, **kw):
        self.invoke_calls.append((tool_name, args, kw))
        return dict(self._invoke_result)


def _default_tools():
    from src.features.edit.ai_editing.tool_facade import ToolDefinition
    return [
        ToolDefinition(name="topos.get_capabilities", permission=ToolPermission.READ,
                       description="caps", input_schema={}),
        ToolDefinition(name="topos.search_assets", permission=ToolPermission.READ,
                       description="search", input_schema={}),
        ToolDefinition(name="topos.validate_edit_plan", permission=ToolPermission.PREVIEW,
                       description="validate", input_schema={}),
        ToolDefinition(name="topos.apply_edit_plan", permission=ToolPermission.WRITE,
                       description="apply", input_schema={}),
    ]


def _open_session(server, *, mcp_enabled=True, approval_grant=None, **policy_kw):
    policy = McpClientPolicy(client_id="test-client", **policy_kw)
    return server.open_session(
        policy, feature_flags={"mcp_server": mcp_enabled}, approval_grant=approval_grant,
    )


class TestSessionManagement:
    def test_open_close_session(self):
        server = _make_server()
        session = _open_session(server)
        assert server.get_session(session.session_id) is session
        assert server.close_session(session.session_id) is True
        assert server.get_session(session.session_id) is None

    def test_close_unknown_returns_false(self):
        server = _make_server()
        assert server.close_session("bogus") is False


# ================================================================
# feature flag（mcp_server off → 全拒）
# ================================================================


class TestFeatureFlag:
    def test_disabled_flag_blocks_tools_list(self):
        """红线：mcp_server off → 工具不暴露（§4.2 step 3 / feature_flags 默认 off）。"""
        server = _make_server()
        session = _open_session(server, mcp_enabled=False)
        assert session.mcp_enabled is False
        assert server.list_tools_for(session) == []

    def test_disabled_flag_blocks_tools_call(self):
        server = _make_server()
        session = _open_session(server, mcp_enabled=False)
        resp = server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "topos.search_assets", "arguments": {}},
        })
        assert "result" in resp
        result = resp["result"]
        assert result["isError"] is True
        assert any(d["code"] == "TAEP_PERMISSION_DENIED" for d in result.get("diagnostics", []))


# ================================================================
# JSON-RPC initialize / tools/list
# ================================================================


class TestJsonRpc:
    def test_initialize_returns_server_info(self):
        server = _make_server()
        session = _open_session(server)
        resp = server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
        })
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        info = resp["result"]
        assert info["serverInfo"]["name"] == SERVER_NAME
        assert info["protocolVersion"] == PROTOCOL_VERSION
        assert info["mcp_enabled"] is True
        assert session.initialized is True

    def test_tools_list_filtered_by_policy(self):
        """§4.2 step 3：动态 manifest 按 client policy 过滤。"""
        server = _make_server()
        # client policy 只读 → 只见 READ 工具，不见 PREVIEW/WRITE
        session = _open_session(server, max_permission=ToolPermission.READ)
        resp = server.handle_request(session, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        })
        names = {t["name"] for t in resp["result"]["tools"]}
        assert "topos.search_assets" in names
        assert "topos.validate_edit_plan" not in names  # PREVIEW，被 ceiling 过滤
        assert "topos.apply_edit_plan" not in names  # WRITE

    def test_tools_list_respects_allowlist(self):
        server = _make_server()
        session = _open_session(
            server, allowed_tools=("topos.search_assets",),
        )
        resp = server.handle_request(session, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        assert names == {"topos.search_assets"}

    def test_method_not_found(self):
        server = _make_server()
        session = _open_session(server)
        resp = server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "bogus/method",
        })
        assert resp["error"]["code"] == -32601  # METHOD_NOT_FOUND

    def test_invalid_jsonrpc_version(self):
        server = _make_server()
        session = _open_session(server)
        resp = server.handle_request(session, {"id": 1, "method": "tools/list"})
        assert resp["error"]["code"] == -32600  # INVALID_REQUEST

    def test_current_protocol_is_stateless(self):
        """2026-07-28 requests can call tools without initialize."""
        server = _make_server()
        resp = server.handle_request(_open_session(server), {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "topos.search_assets", "arguments": {},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        })
        assert resp["result"]["isError"] is False

    def test_current_protocol_unknown_tool_is_jsonrpc_error(self):
        server = _make_server()
        resp = server.handle_request(_open_session(server), {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "not_a_tool", "arguments": {},
                "_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION},
            },
        })
        assert resp["error"]["code"] == -32602
        assert "Unknown tool" in resp["error"]["message"]

    def test_tool_call_rate_limit_is_enforced(self):
        server = McpServer(facade=_StubFacade(), call_log=ToolCallLog(), rate_limit_per_minute=1)
        session = _open_session(server)
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "topos.search_assets", "arguments": {},
                "_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION},
            },
        }
        assert "result" in server.handle_request(session, request)
        request["id"] = 2
        assert server.handle_request(session, request)["error"]["code"] == -32029


# ================================================================
# tools/call 路由 + 审计 + 相同 diagnostics
# ================================================================


class TestToolsCall:
    def test_tools_call_routes_to_facade(self):
        """红线：tools/call 经 ToolFacade.invoke（§4.2 step 2）。"""
        facade = _StubFacade()
        server = _make_server(facade=facade)
        session = _open_session(server)
        server.handle_request(session, {"jsonrpc": "2.0", "id": 0, "method": "initialize"})
        resp = server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "topos.search_assets", "arguments": {"text": "产品"}},
        })
        assert resp["jsonrpc"] == "2.0"
        # facade.invoke 被调用了
        assert len(facade.invoke_calls) == 1
        tool_name, args, _kw = facade.invoke_calls[0]
        assert tool_name == "topos.search_assets"
        assert args == {"text": "产品"}
        # 结果包成 MCP content
        assert resp["result"]["isError"] is False

    def test_tools_call_audited(self):
        """§4.2 step 1：每次 tools/call 经 ToolCallLog 审计。"""
        facade = _StubFacade()
        server = _make_server(facade=facade)
        session = _open_session(server)
        server.handle_request(session, {"jsonrpc": "2.0", "id": 0, "method": "initialize"})
        server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "topos.search_assets", "arguments": {}},
        })
        log = server.call_log
        assert log.count == 1
        rec = log.records()[0]
        assert rec.client == "mcp"
        assert rec.client_session_id == session.session_id

    def test_unauthorized_tool_rejected(self):
        """注入隔离：客户端调未授权工具 → PERMISSION_DENIED。"""
        facade = _StubFacade()
        server = _make_server(facade=facade)
        # client 只读 → apply_edit_plan (WRITE) 不可见
        session = _open_session(server, max_permission=ToolPermission.READ)
        server.handle_request(session, {"jsonrpc": "2.0", "id": 0, "method": "initialize"})
        resp = server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "topos.apply_edit_plan", "arguments": {}},
        })
        result = resp["result"]
        assert result["isError"] is True
        assert any(d["code"] == "TAEP_PERMISSION_DENIED" for d in result["diagnostics"])
        # facade.invoke 没被调用（在路由前就拒了）
        assert len(facade.invoke_calls) == 0

    def test_call_before_initialize_rejected(self):
        server = _make_server()
        session = _open_session(server)
        # 未 initialize 直接 tools/call
        resp = server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "topos.search_assets", "arguments": {}},
        })
        result = resp["result"]
        assert result["isError"] is True
        assert "not initialized" in result["diagnostics"][0]["message"]

    def test_same_diagnostics_as_direct_invoke(self):
        """§11.2 完成检查：MCP 与直接 invoke 结果一致。"""
        invoke_result = {"ok": True, "segments": [{"id": "s1"}, {"id": "s2"}]}
        facade = _StubFacade(invoke_result=invoke_result)
        server = _make_server(facade=facade)
        session = _open_session(server)
        server.handle_request(session, {"jsonrpc": "2.0", "id": 0, "method": "initialize"})
        mcp_resp = server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "topos.search_assets", "arguments": {}},
        })
        # MCP 的 structured 副本与直接 invoke 结果一致
        assert mcp_resp["result"]["structured"] == invoke_result

    def test_missing_name_param(self):
        server = _make_server()
        session = _open_session(server)
        server.handle_request(session, {"jsonrpc": "2.0", "id": 0, "method": "initialize"})
        resp = server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"arguments": {}},  # 缺 name
        })
        assert resp["error"]["code"] == -32602  # INVALID_PARAMS

    def test_write_tool_forwards_session_approval(self):
        """红线修复：WRITE 工具必须收到会话级 approval（§4.2 step 2）。

        宿主在 open_session 注入 approval_grant，_tools_call 透传给
        ToolFacade.invoke 的 ``approval`` 关键字（镜像 tool_calling_client.run_tool_loop）。
        无 approval_grant 时 write 工具不收到 approval kwarg（下游 transaction_service 会拒）。
        """
        facade = _StubFacade()
        server = _make_server(facade=facade)
        grant = "grant-mcp-xyz"
        session = _open_session(
            server, max_permission=ToolPermission.WRITE, approval_grant=grant,
        )
        server.handle_request(session, {"jsonrpc": "2.0", "id": 0, "method": "initialize"})
        server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "topos.apply_edit_plan", "arguments": {"plan": {}}},
        })
        # facade.invoke 收到 approval kwarg
        assert len(facade.invoke_calls) == 1
        _name, _args, kw = facade.invoke_calls[0]
        assert kw.get("approval") == grant
        # 审计记录也带 grant_id
        rec = server.call_log.records()[0]
        assert rec.approval_grant_id == grant
        assert rec.client == "mcp"

    def test_write_tool_without_approval_grant_passes_none(self):
        """无 session approval_grant 时，invoke 不收到 approval kwarg（下游自行拒绝）。"""
        facade = _StubFacade()
        server = _make_server(facade=facade)
        session = _open_session(server, max_permission=ToolPermission.WRITE)
        server.handle_request(session, {"jsonrpc": "2.0", "id": 0, "method": "initialize"})
        server.handle_request(session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "topos.apply_edit_plan", "arguments": {}},
        })
        _name, _args, kw = facade.invoke_calls[0]
        assert "approval" not in kw  # grant=None → 不传
        assert server.call_log.records()[0].approval_grant_id == ""

    def test_approval_grant_id_property_from_object(self):
        """session.approval_grant_id 从 ApprovalGrant 对象取 grant_id。"""
        from src.features.edit.ai_editing.approval_service import (
            ApprovalGrant, ApprovalScope,
        )
        scope = ApprovalScope(action="commit_plan", project_id="p1")
        grant = ApprovalGrant(grant_id="g-001", scope=scope, expires_at=99999.0)
        session = McpSession(
            session_id="s1", client_policy=McpClientPolicy(), approval_grant=grant,
        )
        assert session.approval_grant_id == "g-001"
