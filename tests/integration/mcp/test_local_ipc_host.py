"""Current-user local MCP IPC integration: CLI/stdio use the real gateway."""

from __future__ import annotations

import io
import stat

import pytest

from src.features.edit.ai_editing.mcp_gateway import McpServer
from src.features.edit.ai_editing.mcp_local_ipc import (
    LocalMcpIpcClient,
    LocalMcpIpcHost,
    proxy_stdio_to_local_host,
)
from src.features.edit.ai_editing.tool_call_log import ToolCallLog
from src.features.edit.ai_editing.tool_facade import ToolDefinition, ToolPermission

pytestmark = pytest.mark.edit_core


class _Facade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self, *, max_permission=ToolPermission.READ):
        return [
            ToolDefinition("topos.search_assets", ToolPermission.READ, "search", {"type": "object"}),
            ToolDefinition("topos.validate_edit_plan", ToolPermission.PREVIEW, "preview", {"type": "object"}),
            ToolDefinition("topos.apply_edit_plan", ToolPermission.WRITE, "write", {"type": "object"}),
        ]

    def invoke(self, name, args=None, **_kw):
        self.calls.append((name, dict(args or {})))
        return {"ok": True, "name": name, "args": dict(args or {})}


def _host(tmp_path):
    facade = _Facade()
    server = McpServer(facade=facade, call_log=ToolCallLog())  # type: ignore[arg-type]
    host = LocalMcpIpcHost(
        server,
        socket_path=tmp_path / "ipc" / "topos.sock",
        context_provider=lambda: ("project-1", "profile-1", {"mcp_server": True}),
    )
    host.start()
    return host, facade


def test_local_ipc_client_routes_to_same_gateway_and_is_owner_only(tmp_path) -> None:
    host, facade = _host(tmp_path)
    try:
        assert stat.S_IMODE(host.socket_path.stat().st_mode) == 0o600
        result = LocalMcpIpcClient(host.socket_path).invoke(
            "topos.search_assets", {"text": "产品"},
        )
        assert result["ok"] is True
        assert facade.calls == [("topos.search_assets", {"text": "产品"})]
    finally:
        host.stop()
    assert not host.socket_path.exists()


def test_local_ipc_keeps_write_tool_hidden_and_stdio_can_proxy(tmp_path) -> None:
    host, facade = _host(tmp_path)
    try:
        incoming = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
            '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"topos.apply_edit_plan","arguments":{}}}\n'
        )
        outgoing = io.StringIO()
        assert proxy_stdio_to_local_host(host.socket_path, input_stream=incoming, output_stream=outgoing) == 0
        lines = [__import__("json").loads(line) for line in outgoing.getvalue().splitlines()]
        names = {tool["name"] for tool in lines[1]["result"]["tools"]}
        assert "topos.search_assets" in names
        assert "topos.validate_edit_plan" in names
        assert "topos.apply_edit_plan" not in names
        assert lines[2]["result"]["isError"] is True
        assert facade.calls == []
    finally:
        host.stop()
