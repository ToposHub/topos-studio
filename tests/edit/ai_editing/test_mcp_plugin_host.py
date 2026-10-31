"""P3 MCP connector plugin lifecycle tests."""

from __future__ import annotations

from typing import Any

from src.features.ai_harness.plugin_runtime import PluginExecutionPolicy
from src.features.edit.ai_editing.mcp_gateway import McpClientPolicy, McpServer
from src.features.edit.ai_editing.mcp_plugin import McpPluginDescriptor, McpPluginHost


class _Facade:
    def list_tools(self, **_: Any) -> list[Any]:
        return []

    def invoke(self, *_: Any, **__: Any) -> tuple[dict[str, Any], None]:
        return {"ok": True}, None


def test_mcp_connector_is_gated_by_plugin_activation() -> None:
    host = McpPluginHost()
    server = McpServer(_Facade())
    descriptor = McpPluginDescriptor(
        plugin_id="mcp.editorial",
        display_name="Editorial MCP",
        endpoint="stdio://editorial",
        tool_names=("topos.get_capabilities", "topos.get_capabilities"),
        capabilities=("editorial",),
        data_policy="cloud",
        network_domains=("api.example.com", "API.EXAMPLE.COM"),
    )
    assert host.register(
        server,
        descriptor,
        policy=PluginExecutionPolicy(max_concurrent_calls=2),
    )
    assert host.open_session(descriptor.plugin_id, McpClientPolicy()) is None
    assert host.activate(descriptor.plugin_id)

    session = host.open_session(
        descriptor.plugin_id,
        McpClientPolicy(client_id="test"),
        feature_flags={"mcp_server": True},
    )
    assert session is not None
    response = host.handle_request(
        descriptor.plugin_id,
        session,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert response["result"]["mcp_enabled"] is True

    snapshot = host.snapshot()
    assert snapshot["connectors"][0]["data_policy"] == "cloud"
    assert snapshot["connectors"][0]["network_domains"] == ["api.example.com"]
    assert host.deactivate(descriptor.plugin_id)
    blocked = host.handle_request(
        descriptor.plugin_id,
        session,
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
    )
    assert blocked["error"]["code"] == -32001


def test_mcp_descriptor_rejects_invalid_ttl_and_policy() -> None:
    try:
        McpPluginDescriptor(plugin_id="mcp.bad", display_name="Bad", ttl_seconds=0)
    except ValueError as exc:
        assert "ttl_seconds" in str(exc)
    else:
        raise AssertionError("non-positive TTL must fail closed")

    try:
        McpPluginDescriptor(
            plugin_id="mcp.bad-policy",
            display_name="Bad",
            data_policy="unknown",
        )
    except ValueError as exc:
        assert "data policy" in str(exc)
    else:
        raise AssertionError("unknown data policy must fail closed")
