"""本机 stdio MCP transport adapter。

该模块只负责 JSON Lines transport；所有工具语义仍由 :class:`McpServer` 路由到
``ToolFacade``。stdio 进程天然继承启动它的当前 OS 用户，不监听 socket，也不接受
公网连接。宿主应用应通过 ``serve_stdio`` 注入已连接、已授权的 ``McpServer``；命令行
``--demo`` 仅用于离线 contract test，返回的数据必须标明为 demo。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from src.features.edit.ai_editing.mcp_gateway import (
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
    McpClientPolicy,
    McpServer,
)
from src.features.edit.ai_editing.tool_call_log import ToolCallLog
from src.features.edit.ai_editing.tool_facade import ToolDefinition, ToolPermission


@dataclass(frozen=True)
class StdioConfig:
    """本机 stdio transport 的边界配置。"""

    max_line_bytes: int = 1_048_576
    client_id: str = "stdio-client"
    author: str = "local-process"
    version: str = ""
    data_scope: str = "current_project"


def serve_stdio(
    server: McpServer,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    policy: McpClientPolicy | None = None,
    feature_flags: dict[str, bool] | None = None,
    active_project_id: str = "",
    active_profile_id: str = "",
    approval_grant: Any = None,
    config: StdioConfig | None = None,
) -> int:
    """以 JSON Lines 形式服务一个本机 MCP 会话。

    返回值为进程退出码。每行最多一个 JSON-RPC 请求；空行忽略。超过大小限制、JSON
    非对象或协议版本错误均返回 JSON-RPC error，不把异常栈写入 stdout。EOF 会关闭
    会话并正常返回。
    """

    inp = input_stream or sys.stdin
    out = output_stream or sys.stdout
    cfg = config or StdioConfig()
    session_policy = policy or McpClientPolicy(
        client_id=cfg.client_id,
        author=cfg.author,
        version=cfg.version,
        data_scope=cfg.data_scope,
    )
    session = server.open_session(
        session_policy,
        active_project_id=active_project_id,
        active_profile_id=active_profile_id,
        # MCP is security-sensitive and defaults to OFF. In particular, an
        # explicitly supplied empty mapping must not be converted into an
        # enabled session by an ``or`` fallback.
        feature_flags=(
            dict(feature_flags)
            if feature_flags is not None else {"mcp_server": False}
        ),
        approval_grant=approval_grant,
    )

    try:
        for raw_line in inp:
            if not raw_line.strip():
                continue
            raw_bytes = raw_line.encode("utf-8", errors="replace")
            if len(raw_bytes) > cfg.max_line_bytes:
                _write_response(out, _error(None, JSONRPC_INVALID_REQUEST, "request too large"))
                continue
            try:
                request = json.loads(raw_line)
            except json.JSONDecodeError:
                _write_response(out, _error(None, JSONRPC_PARSE_ERROR, "invalid JSON"))
                continue
            if not isinstance(request, dict):
                _write_response(out, _error(None, JSONRPC_INVALID_REQUEST, "request must be an object"))
                continue
            response = server.handle_request(session, request)
            # v1 keeps responses one-per-line; notification support can be added without
            # changing McpServer because transport owns this framing decision.
            if "id" in request:
                _write_response(out, response)
    finally:
        server.close_session(session.session_id)
    return 0


def _write_response(output_stream: TextIO, response: dict[str, Any]) -> None:
    output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    output_stream.flush()


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class _DemoFacade:
    """显式 demo facade，仅供 ``python -m ... --demo`` contract test 使用。"""

    def __init__(self) -> None:
        self._tools = [
            ToolDefinition(
                name="topos.get_capabilities", permission=ToolPermission.READ,
                description="Read local capability manifest (demo)", input_schema={"type": "object"},
            ),
        ]

    def list_tools(self, *, max_permission: ToolPermission = ToolPermission.READ) -> list[ToolDefinition]:
        if max_permission != ToolPermission.READ:
            return list(self._tools)
        return list(self._tools)

    def invoke(self, tool_name: str, args: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "demo": True,
            "tool": tool_name,
            "capabilities": {"source": "demo", "next_step": "connect a host"},
        }


def _build_demo_server() -> McpServer:
    return McpServer(facade=_DemoFacade(), call_log=ToolCallLog())  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    """stdio entrypoint；没有 ``--demo`` 时拒绝启动未连接的假 host。"""

    parser = argparse.ArgumentParser(prog="topos-mcp-stdio")
    parser.add_argument(
        "--demo", action="store_true",
        help="run the explicit offline demo facade (contract tests only)",
    )
    parser.add_argument(
        "--socket", default="",
        help="proxy stdio MCP to the running current-user Topos desktop IPC socket",
    )
    args = parser.parse_args(argv)
    if args.socket:
        if args.demo:
            print("--demo and --socket are mutually exclusive", file=sys.stderr)
            return 2
        from src.features.edit.ai_editing.mcp_local_ipc import proxy_stdio_to_local_host
        return proxy_stdio_to_local_host(args.socket, input_stream=sys.stdin, output_stream=sys.stdout)
    if not args.demo:
        print(
            "topos-mcp-stdio requires --socket for a running application host; use --demo only for offline tests",
            file=sys.stderr,
        )
        return 2
    # ``--demo`` is an explicit opt-in for the offline contract host.
    return serve_stdio(_build_demo_server(), feature_flags={"mcp_server": True})


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess integration test
    raise SystemExit(main())


__all__ = ["StdioConfig", "serve_stdio", "main"]
