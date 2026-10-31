"""本机 MCP stdio transport contract / subprocess integration tests。"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.mcp_gateway import McpClientPolicy, McpServer
from src.features.edit.ai_editing.mcp_stdio import StdioConfig, serve_stdio
from src.features.edit.ai_editing.tool_call_log import ToolCallLog
from src.features.edit.ai_editing.tool_facade import ToolDefinition, ToolPermission


class _Facade:
    def __init__(self) -> None:
        self._tools = [ToolDefinition(
            name="topos.search_assets", permission=ToolPermission.READ,
            description="search", input_schema={"type": "object"},
        )]
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self, *, max_permission=ToolPermission.READ):
        return list(self._tools)

    def invoke(self, name, args=None, **kwargs):
        self.calls.append((name, args or {}))
        return {"ok": True, "segments": [{"id": "s1"}], "untrusted": True}


def _server(facade: _Facade | None = None) -> tuple[McpServer, _Facade]:
    f = facade or _Facade()
    return McpServer(facade=f, call_log=ToolCallLog()), f  # type: ignore[arg-type]


def test_stdio_initialize_list_and_call_use_same_gateway() -> None:
    server, facade = _server()
    inp = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "topos.search_assets", "arguments": {"text": "产品"}},
        }),
    ]) + "\n")
    out = io.StringIO()

    assert serve_stdio(
        server,
        input_stream=inp,
        output_stream=out,
        feature_flags={"mcp_server": True},
    ) == 0
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "topos-mcp"
    assert responses[1]["result"]["tools"][0]["name"] == "topos.search_assets"
    assert responses[2]["result"]["structured"]["untrusted"] is True
    assert facade.calls == [("topos.search_assets", {"text": "产品"})]
    assert server.get_session("missing") is None


def test_stdio_rejects_malformed_and_oversized_without_crashing() -> None:
    server, _ = _server()
    inp = io.StringIO("not-json\n" + json.dumps({"jsonrpc": "2.0", "id": 1}) + "\n")
    out = io.StringIO()
    serve_stdio(
        server, input_stream=inp, output_stream=out,
        config=StdioConfig(max_line_bytes=16),
    )
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32700  # malformed JSON
    assert responses[1]["error"]["code"] == -32600  # oversized JSON-RPC request


def test_stdio_policy_stays_read_only() -> None:
    server, facade = _server()
    inp = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
    out = io.StringIO()
    serve_stdio(
        server, input_stream=inp, output_stream=out,
        policy=McpClientPolicy(max_permission=ToolPermission.READ),
    )
    assert facade.calls == []


def test_stdio_requires_explicit_feature_flag() -> None:
    """Missing or empty flags must preserve the safe MCP-off default."""
    for flags in (None, {}):
        server, _ = _server()
        inp = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
        out = io.StringIO()
        serve_stdio(server, input_stream=inp, output_stream=out, feature_flags=flags)
        response = json.loads(out.getvalue())
        assert response["result"]["mcp_enabled"] is False


@pytest.mark.integration
def test_subprocess_demo_stdio_protocol() -> None:
    repo = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "src.features.edit.ai_editing.mcp_stdio", "--demo"],
        input=(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        ),
        text=True, capture_output=True, cwd=repo, env=env, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert responses[0]["result"]["mcp_enabled"] is True
    assert responses[1]["result"]["tools"][0]["name"] == "topos.get_capabilities"
