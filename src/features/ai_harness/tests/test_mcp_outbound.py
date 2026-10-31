"""MCP outbound 单元测试（plan §10.5 P3.2）。

覆盖：

- McpConnectionProfile：只读 / 本机 / 会话级；不存 secret（credential_ref）。
- McpServerRegistry：allowlist / 会话启用 / BLOCKED / discovery 缓存。
- McpOutboundClient：授权链（allowlist → BLOCKED → 会话启用 → tool allowlist
  → 写审批 → transport）；外部 server 输出只作数据；transport 异常归审计。
- McpAuthorizationLog：记录所有决策；denial_count。

红线（plan §10.5 P3.2 / §1.3）：

- 默认只读 / 本机 / 会话级启用。
- 未 allowlist / BLOCKED 的 server 不执行。
- 写操作弹 Topos 审批（未审批 denied）。
- 外部 server 输出不可信（只作数据）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.mcp_outbound import (
    McpAuthorizationLog,
    McpConnectionProfile,
    McpOutboundClient,
    McpServerManifest,
    McpServerRegistry,
)


# ================================================================
# fixtures
# ================================================================


def _profile(profile_id="srv", **kw):
    defaults = dict(
        profile_id=profile_id, server_name="test-mcp", author="acme",
        version="1.0", homepage_domain="acme.com", transport="local_stdio",
        data_scope="read_only", enabled_by_default=True,
    )
    defaults.update(kw)
    return McpConnectionProfile(**defaults)


class _OkTransport:
    """fake transport：tools/list 返回工具清单；tools/call 返回数据。"""
    def send_request(self, req):
        if req["method"] == "tools/list":
            return {"result": {"tools": [
                               {"name": "get_x", "inputSchema": {"type": "object"}},
                               {"name": "write_y", "inputSchema": {"type": "object"}},
                               {"name": "write_file", "inputSchema": {"type": "object"}},
                           ],
                               "protocolVersion": "2024"}}
        return {"result": {"content": [{"type": "text", "text": "data"}]}}
    def close(self):
        pass


class _CrashTransport:
    def send_request(self, req):
        raise RuntimeError("server crashed")
    def close(self):
        pass


class _DiscoverThenCrashTransport:
    """tools/list 正常应答（discovery 成功）、tools/call 崩溃的 fake transport。"""
    def send_request(self, req):
        if req["method"] == "tools/list":
            return {"result": {"tools": [
                               {"name": "get_x", "inputSchema": {"type": "object"},
                                "annotations": {"readOnlyHint": True}},
                           ], "protocolVersion": "2024"}}
        raise RuntimeError("server crashed")
    def close(self):
        pass


@pytest.fixture
def client_with_server():
    reg = McpServerRegistry()
    reg.register(_profile())
    client = McpOutboundClient(reg, transport_factory=lambda p: _OkTransport())
    yield reg, client
    client.close()


# ================================================================
# McpConnectionProfile
# ================================================================


class TestConnectionProfile:
    def test_defaults_read_only_local(self):
        p = _profile()
        assert p.is_local is True
        assert p.is_read_only_scope is True
        assert p.enabled_by_default is True

    def test_no_secret_stored(self):
        """红线：profile 不存 secret（只有 credential_ref）。"""
        p = _profile(credential_ref="keychain:weather")
        # profile 字段里没有 secret / api_key / password 明文
        assert not hasattr(p, "secret")
        assert not hasattr(p, "api_key")
        assert p.credential_ref == "keychain:weather"  # 只是引用

    def test_cloud_profile(self):
        p = _profile(transport="cloud_api", base_url="https://api.acme.com")
        assert p.is_local is False


# ================================================================
# McpServerRegistry
# ================================================================


class TestServerRegistry:
    def test_register_and_get(self):
        reg = McpServerRegistry()
        reg.register(_profile("a"))
        assert reg.get("a") is not None
        assert reg.is_allowlisted("a") is True

    def test_not_allowlisted(self):
        reg = McpServerRegistry()
        assert reg.is_allowlisted("nope") is False

    def test_session_enable_disable(self):
        reg = McpServerRegistry()
        reg.register(_profile("a", enabled_by_default=False))
        assert reg.is_session_enabled("a") is False
        assert reg.enable_for_session("a") is True
        assert reg.is_session_enabled("a") is True
        reg.disable_for_session("a")
        assert reg.is_session_enabled("a") is False

    def test_enable_blocked_rejected(self):
        reg = McpServerRegistry()
        reg.register(_profile("a"))
        reg.mark_blocked("a", "missing dep")
        assert reg.enable_for_session("a") is False

    def test_blocked(self):
        reg = McpServerRegistry()
        reg.register(_profile("a"))
        reg.mark_blocked("a", "missing numpy>=2")
        assert reg.is_blocked("a") is True
        assert "numpy" in reg.block_reason("a")

    def test_discovery_cache(self):
        reg = McpServerRegistry()
        reg.register(_profile("a"))
        m = McpServerManifest(server_name="a", tools=({"name": "x"},))
        reg.cache_manifest("a", m)
        assert reg.get_manifest("a") is m


# ================================================================
# McpOutboundClient 授权链
# ================================================================


class TestAuthorizationChain:
    def test_allowed_read_call(self, client_with_server):
        _, client = client_with_server
        r = client.call_tool("srv", "get_x", {})
        assert r["ok"] is True
        assert r["decision"] == "allowed"

    def test_not_allowlisted_denied(self, client_with_server):
        _, client = client_with_server
        r = client.call_tool("unknown", "x", {})
        assert r["ok"] is False
        assert r["decision"] == "denied_not_allowlisted"

    def test_blocked_denied(self, client_with_server):
        reg, client = client_with_server
        reg.mark_blocked("srv", "missing dep")
        r = client.call_tool("srv", "get_x", {})
        assert r["decision"] == "blocked_missing_deps"

    def test_not_enabled_denied(self, client_with_server):
        reg, client = client_with_server
        reg.register(_profile("srv2", enabled_by_default=False))
        r = client.call_tool("srv2", "get_x", {})
        assert r["decision"] == "denied_not_enabled"

    def test_write_needs_approval(self, client_with_server):
        """红线：写操作未审批 → denied。"""
        _, client = client_with_server
        r = client.call_tool("srv", "write_file", {})
        assert r["decision"] == "denied_write_needs_approval"

    def test_write_approved_succeeds(self, client_with_server):
        _, client = client_with_server
        r = client.call_tool("srv", "write_file", {}, write_approved=True)
        assert r["ok"] is True

    def test_tool_not_in_profile_allowlist(self):
        reg = McpServerRegistry()
        reg.register(_profile(allowed_tools=("get_x",)))
        client = McpOutboundClient(reg, transport_factory=lambda p: _OkTransport())
        r = client.call_tool("srv", "other_tool", {})
        assert r["decision"] == "denied_tool_not_allowed"
        client.close()

    def test_transport_error_recorded(self):
        reg = McpServerRegistry()
        reg.register(_profile())
        client = McpOutboundClient(reg, transport_factory=lambda p: _CrashTransport())
        # AUD-22121（锁定旧行为翻转，原 AUD-22020 过渡语义）：crash transport
        # 无法应答 tools/list → discovery 失败 → 无论是否已获写审批，未校验的
        # 调用都 fail-closed 拒绝（不再放行到 transport）。
        r = client.call_tool("srv", "get_x", {})
        assert r["ok"] is False
        assert r["decision"] == "error_discovery_failed"
        # 已获写审批同样拒绝：缺清单就无法验证工具存在性/入参 schema。
        r2 = client.call_tool("srv", "get_x", {}, write_approved=True)
        assert r2["ok"] is False
        assert r2["decision"] == "error_discovery_failed"
        # 真正抵达 transport 的异常（发现成功后的 tools/call 崩溃）仍归审计
        # 为 error（原语义保留，用可发现的 transport 验证）。
        reg2 = McpServerRegistry()
        reg2.register(_profile())
        client2 = McpOutboundClient(reg2, transport_factory=lambda p: _DiscoverThenCrashTransport())
        r3 = client2.call_tool("srv", "get_x", {})
        assert r3["ok"] is False
        assert r3["decision"] == "error"
        client.close()
        client2.close()

    def test_no_transport_factory(self):
        reg = McpServerRegistry()
        reg.register(_profile())
        client = McpOutboundClient(reg)  # 无 transport_factory
        # 无 transport → 发现失败 → 未校验调用拒绝（AUD-22121 fail-closed）。
        r = client.call_tool("srv", "get_x", {})
        assert r["decision"] == "error_discovery_failed"
        r2 = client.call_tool("srv", "get_x", {}, write_approved=True)
        assert r2["decision"] == "error_discovery_failed"
        client.close()


# ================================================================
# 红线：外部 server 输出只作数据（不执行第三方代码）
# ================================================================


class TestExternalOutputUntrusted:
    def test_result_returned_as_data(self, client_with_server):
        """外部 server 的返回只作数据；不在本进程执行。"""
        _, client = client_with_server
        r = client.call_tool("srv", "get_x", {})
        assert r["ok"] is True
        # result 是 dict 数据（含 content），不是可执行对象
        assert isinstance(r["result"], dict)

    def test_write_in_non_readonly_scope_always_needs_approval(self):
        """data_scope 非 read_only → 即使 read_named 工具也需审批。"""
        reg = McpServerRegistry()
        reg.register(_profile(data_scope="current_project"))
        client = McpOutboundClient(reg, transport_factory=lambda p: _OkTransport())
        r = client.call_tool("srv", "get_x", {})  # 看似 read，但 scope 非只读
        assert r["decision"] == "denied_write_needs_approval"
        client.close()


# ================================================================
# discovery（只读，不执行工具）
# ================================================================


class TestDiscovery:
    def test_discover_returns_manifest(self, client_with_server):
        _, client = client_with_server
        m = client.discover("srv")
        assert m is not None
        assert any(t["name"] == "get_x" for t in m.tools)

    def test_discover_caches(self, client_with_server):
        _, client = client_with_server
        client.discover("srv")
        # 第二次从缓存（不再调 transport）
        m2 = client.discover("srv")
        assert m2 is not None

    def test_discover_unknown_profile(self, client_with_server):
        _, client = client_with_server
        assert client.discover("nope") is None

    def test_discover_disabled_server_does_not_contact_transport(self):
        registry = McpServerRegistry()
        registry.register(_profile("disabled", enabled_by_default=False))
        calls = []

        class _NoContact:
            def send_request(self, request):
                calls.append(request)
                return {"result": {"tools": []}}
            def close(self):
                pass

        client = McpOutboundClient(registry, transport_factory=lambda p: _NoContact())
        assert client.discover("disabled") is None
        assert calls == []
        client.close()

    def test_discovered_input_schema_is_enforced(self, client_with_server):
        registry, client = client_with_server
        registry.cache_manifest("srv", McpServerManifest(
            server_name="test-mcp",
            tools=({
                "name": "get_x",
                "inputSchema": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
            },),
        ))
        result = client.call_tool("srv", "get_x", {})
        assert result["decision"] == "denied_invalid_request"


# ================================================================
# 授权审计日志
# ================================================================


class TestAuthorizationLog:
    def test_records_all_decisions(self, client_with_server):
        _, client = client_with_server
        client.call_tool("srv", "get_x", {})         # allowed
        client.call_tool("srv", "write_file", {})    # denied
        client.call_tool("nope", "x", {})            # denied
        events = client.auth_log.events()
        assert len(events) == 3
        decisions = [e.decision for e in events]
        assert "allowed" in decisions

    def test_denial_count(self, client_with_server):
        _, client = client_with_server
        client.call_tool("srv", "write_file", {})    # denied
        client.call_tool("nope", "x", {})            # denied
        assert client.auth_log.denial_count() == 2

    def test_filter_by_profile(self, client_with_server):
        reg, client = client_with_server
        reg.register(_profile("other", enabled_by_default=True))
        client.call_tool("srv", "get_x", {})
        client.call_tool("other", "get_x", {})
        srv_events = client.auth_log.events(profile_id="srv")
        assert all(e.profile_id == "srv" for e in srv_events)

    def test_write_flag_recorded(self, client_with_server):
        _, client = client_with_server
        client.call_tool("srv", "write_file", {}, write_approved=True)
        events = client.auth_log.events()
        write_events = [e for e in events if e.write_operation]
        assert len(write_events) >= 1

    def test_max_events_cap(self):
        log = McpAuthorizationLog(max_events=3)
        from src.features.ai_harness.mcp_outbound import McpAuthorizationEvent
        for i in range(10):
            log.record(McpAuthorizationEvent(
                timestamp=float(i), profile_id="p", server_name="s",
                tool_name="t", decision="allowed",
            ))
        assert len(log.events()) == 3  # 截断到最近 3
