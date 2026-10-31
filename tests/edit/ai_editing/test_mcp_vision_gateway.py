"""MCP 视觉网关 + stdio 传输测试。

覆盖：
* SubprocessStdioTransport 帧处理（JSON 行解析 / PATH 前置 / 错误归一）。
* 真实子进程往返：用 Topos 自带的 demo MCP server（``mcp_stdio --demo``）验证
  initialize 握手 + tools/list（不需要 node/npx）。
* McpVisionGateway：不可用降级、4 个工具定义（READ）、引用解析（latest/asset/显式）、
  参数映射（image_ref→image_source 等）、经 fake transport 的调用链。
* ToolFacade 合并：topos.mcp.* 进目录；invoke 路由 + 输出 sanitize。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.mcp_stdio_transport import (
    McpTransportError,
    SubprocessStdioTransport,
)
from src.features.edit.ai_editing.mcp_vision_gateway import (
    McpVisionGateway,
    build_zhipu_vision_profile,
    resolve_npx_path,
)


class _StubOpener:
    """AUD-22017 后 HttpMcpTransport 用实例 opener（禁重定向）发请求。

    测试用本桩替换 ``transport._opener``，沿用原 ``urlopen(request, timeout)``
    的响应契约。
    """

    def __init__(self, handler):
        self._handler = handler

    def open(self, request, timeout=None):
        return self._handler(request, timeout)


# ================================================================
# 传输层：帧处理（无子进程）
# ================================================================


class TestTransportFraming:
    def test_parse_valid_json_line(self):
        line = b'{"jsonrpc":"2.0","id":1,"result":{"a":1}}\n'
        msg = SubprocessStdioTransport._parse(line)
        assert msg == {"jsonrpc": "2.0", "id": 1, "result": {"a": 1}}

    def test_parse_invalid_and_empty(self):
        assert SubprocessStdioTransport._parse(b"not json\n") is None
        assert SubprocessStdioTransport._parse(b"\n") is None
        assert SubprocessStdioTransport._parse(b"") is None
        assert SubprocessStdioTransport._parse(b"[1,2]") is None  # 非 dict

    def test_build_env_prepends_command_dir_to_path(self, tmp_path):
        # W07：Py3.13 ntpath.isabs 对无盘符的 "/" 根路径判 False——
        # POSIX 风格常量在 Windows 上不是合法绝对命令。按平台构造真实
        # 绝对路径（意图不变：绝对命令目录前置到 PATH）。
        npx = tmp_path / "bin" / "npx.exe"
        t = SubprocessStdioTransport(
            (str(npx), "-y", "pkg"), env={"Z_AI_API_KEY": "k"},
        )
        env = t._build_env()
        assert env["Z_AI_API_KEY"] == "k"
        assert env["PATH"].startswith(str(tmp_path / "bin") + os.pathsep)

    def test_build_env_relative_command_leaves_path(self):
        t = SubprocessStdioTransport(("npx", "-y", "pkg"))
        env = t._build_env()
        assert not env.get("PATH", "").startswith("npx")

    def test_empty_command_raises(self):
        with pytest.raises(ValueError):
            SubprocessStdioTransport(())

    def test_raise_if_error(self):
        with pytest.raises(McpTransportError):
            SubprocessStdioTransport._raise_if_error(
                {"error": {"code": -32000, "message": "boom"}}
            )
        SubprocessStdioTransport._raise_if_error({"result": {}})  # 不抛


# ================================================================
# 传输层：真实子进程往返（Topos 自带 demo MCP server，无需 node）
# ================================================================


@pytest.mark.integration
def test_transport_roundtrip_against_demo_mcp_server():
    """与真实 stdio MCP server（demo）完成握手 + tools/list（客户端方向冒烟）。"""
    cmd = (sys.executable, "-m", "src.features.edit.ai_editing.mcp_stdio", "--demo")
    t = SubprocessStdioTransport(cmd, startup_timeout=30, request_timeout=30)
    try:
        t.start()  # spawn + initialize 握手（失败抛 McpTransportError）
        resp = t.send_request({"jsonrpc": "2.0", "method": "tools/list", "params": {}})
        tools = (resp.get("result") or {}).get("tools", [])
        assert isinstance(tools, list)
        names = {t_.get("name") for t_ in tools if isinstance(t_, dict)}
        assert "topos.get_capabilities" in names
    finally:
        t.close()


# ================================================================
# Gateway：不可用降级 + 工具定义 + 引用解析
# ================================================================


class TestGatewayBasics:
    def test_unavailable_without_npx(self, monkeypatch):
        monkeypatch.setattr(
            "src.features.edit.ai_editing.mcp_vision_gateway.resolve_npx_path",
            lambda: "",
        )
        gw = McpVisionGateway(
            api_key_provider=lambda: "k", npx_path="", transport_factory=lambda p: None,
        )
        assert gw.available is False
        # 视觉工具不暴露；但 HTTP 工具（search/reader）不依赖 npx，仍可用。
        names = [d.name for d in gw.tool_definitions()]
        assert "topos.mcp.analyze_image" not in names
        assert "topos.mcp.web_search" in names  # key 在 → 联网工具可用
        assert gw.web_available is True
        r = gw.invoke("topos.mcp.analyze_image", {"image_ref": "latest"})
        assert r["ok"] is False and "npx" in r["error"]

    def test_tool_definitions_four_read_tools(self):
        # 无 key → 只剩视觉 4 工具（隔离验证视觉目录）。
        gw = McpVisionGateway(
            api_key_provider=lambda: None, npx_path="/fake/npx",
        )
        assert gw.web_available is False
        defs = gw.tool_definitions()
        names = [d.name for d in defs]
        assert names == [
            "topos.mcp.analyze_image", "topos.mcp.analyze_video",
            "topos.mcp.extract_text", "topos.mcp.ui_diff_check",
        ]
        for d in defs:
            assert d.permission.value == "read"

    def test_web_tools_when_key_available(self):
        gw = McpVisionGateway(
            api_key_provider=lambda: "k", npx_path="/fake/npx",
        )
        names = [d.name for d in gw.tool_definitions()]
        assert "topos.mcp.web_search" in names
        assert "topos.mcp.web_read" in names

    def test_profile_is_read_only_local(self):
        p = build_zhipu_vision_profile("/fake/npx")
        assert p.is_local is True
        assert p.is_read_only_scope is True
        assert p.enabled_by_default is True
        assert "analyze_image" in p.allowed_tools
        # 红线：command 不含 secret。
        assert all("Z_AI_API_KEY" not in part for part in p.command)

    def test_ref_resolution(self, tmp_path):
        gw = McpVisionGateway(api_key_provider=lambda: "k", npx_path="/fake/npx")
        f = tmp_path / "frame.png"
        f.write_bytes(b"png")
        gw.register_session_image(str(f))
        # latest → 最近注册
        assert gw.resolve_image_ref("latest") == os.path.abspath(str(f))
        # asset:<id> 经 resolver
        assert gw.resolve_image_ref("asset:a1", lambda i: f"/proj/{i}.mp4") == "/proj/a1.mp4"
        # 显式路径
        assert gw.resolve_image_ref("/tmp/x.png") == "/tmp/x.png"
        # 空 / 无注册 → 空串
        gw2 = McpVisionGateway(api_key_provider=lambda: "k", npx_path="/fake/npx")
        assert gw2.resolve_image_ref("latest") == ""

    def test_register_only_existing_files(self, tmp_path):
        gw = McpVisionGateway(api_key_provider=lambda: "k", npx_path="/fake/npx")
        gw.register_session_image(str(tmp_path / "missing.png"))
        assert gw.resolve_image_ref("latest") == ""


# ================================================================
# Gateway：调用链（fake transport）
# ================================================================


class _FakeTransport:
    """记录请求并返回预设响应的假 transport（满足 StdioTransport 协议）。

    AUD-22020 修复后 McpOutboundClient 对 tools/list 发现失败的 server 按
    写处理（fail-closed），只读网关工具也会被拒。真实 MCP server 都应答
    tools/list（网关工具 schema 即来自实测），fake 同样应答：返回覆盖网关
    全部 MCP 工具、声明 readOnlyHint 的清单。
    """

    #: 网关会调用的全部 MCP 工具名（vision / search / reader 三个 server）。
    GATEWAY_MCP_TOOLS = (
        "analyze_image", "analyze_video", "extract_text_from_screenshot",
        "ui_diff_check", "web_search_prime", "webReader",
    )

    def __init__(self, response: dict | None = None) -> None:
        self.requests: list[dict] = []
        self._response = response or {
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": "红色圆形与蓝色矩形"}]},
        }

    def send_request(self, request: dict) -> dict:
        self.requests.append(request)
        if request.get("method") == "tools/list":
            return {"result": {
                "tools": [
                    {
                        "name": name,
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": True},
                    }
                    for name in self.GATEWAY_MCP_TOOLS
                ],
                "protocolVersion": "2024",
            }}
        return self._response

    def close(self) -> None:
        pass


def _last_tool_call(fake: _FakeTransport) -> dict:
    return next(request for request in reversed(fake.requests) if request["method"] == "tools/call")


def _make_gateway_with_fake(tmp_path, tool="analyze_image"):
    img = tmp_path / "img.png"
    img.write_bytes(b"png")
    fake = _FakeTransport()
    gw = McpVisionGateway(
        api_key_provider=lambda: "test-key",
        npx_path="/fake/npx",
        transport_factory=lambda profile: fake,
    )
    gw.register_session_image(str(img))
    return gw, fake, str(img)


class TestGatewayInvoke:
    def test_analyze_image_maps_ref_to_image_source(self, tmp_path):
        gw, fake, img = _make_gateway_with_fake(tmp_path)
        r = gw.invoke(
            "topos.mcp.analyze_image",
            {"image_ref": "latest", "prompt": "描述颜色"},
        )
        assert r["ok"] is True
        assert "红色圆形" in r["analysis"]
        call = _last_tool_call(fake)
        assert call["method"] == "tools/call"
        params = call["params"]
        assert params["name"] == "analyze_image"
        assert params["arguments"]["image_source"] == img
        assert params["arguments"]["prompt"] == "描述颜色"

    def test_extract_text_maps_to_ocr_tool(self, tmp_path):
        gw, fake, img = _make_gateway_with_fake(tmp_path)
        r = gw.invoke("topos.mcp.extract_text", {"image_ref": "latest"})
        assert r["ok"] is True
        assert _last_tool_call(fake)["params"]["name"] == "extract_text_from_screenshot"

    def test_ui_diff_check_two_refs(self, tmp_path):
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        a.write_bytes(b"1"); b.write_bytes(b"2")
        fake = _FakeTransport()
        gw = McpVisionGateway(
            api_key_provider=lambda: "k", npx_path="/fake/npx",
            transport_factory=lambda p: fake,
        )
        gw.register_session_image(str(a))
        r = gw.invoke("topos.mcp.ui_diff_check", {
            "expected_image_ref": str(a), "actual_image_ref": str(b),
        })
        assert r["ok"] is True
        args = _last_tool_call(fake)["params"]["arguments"]
        assert args["expected_image_source"] == os.path.abspath(str(a))
        assert args["actual_image_source"] == os.path.abspath(str(b))

    def test_missing_image_degrades_gracefully(self, tmp_path):
        gw, fake, _ = _make_gateway_with_fake(tmp_path)
        r = gw.invoke("topos.mcp.analyze_image", {"image_ref": "/no/such/file.png"})
        assert r["ok"] is False
        assert "图片不可用" in r["error"]
        assert fake.requests == []  # 未发起 MCP 调用

    def test_unknown_tool_rejected(self, tmp_path):
        gw, _, _ = _make_gateway_with_fake(tmp_path)
        r = gw.invoke("topos.mcp.nope", {})
        assert r["ok"] is False and "unknown" in r["error"]


# ================================================================
# HTTP 传输（远程 MCP：search/reader）+ web 工具路由
# ================================================================


class TestHttpTransportParsing:
    def test_parse_sse_data_line(self):
        from src.features.ai_harness.mcp_http_transport import _parse_sse_or_json
        raw = "id:1\nevent:message\ndata:{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\n\n"
        msg = _parse_sse_or_json(raw)
        assert msg == {"jsonrpc": "2.0", "id": 1, "result": {}}

    def test_parse_plain_json(self):
        from src.features.ai_harness.mcp_http_transport import _parse_sse_or_json
        assert _parse_sse_or_json('{"ok":1}') == {"ok": 1}

    def test_parse_garbage_returns_none(self):
        from src.features.ai_harness.mcp_http_transport import _parse_sse_or_json
        assert _parse_sse_or_json("not json") is None
        assert _parse_sse_or_json("data: broken") is None

    def test_url_appends_mcp_suffix(self):
        from src.features.ai_harness.mcp_http_transport import HttpMcpTransport
        t = HttpMcpTransport("https://x.example/api/mcp")
        assert t._url == "https://x.example/api/mcp"
        t2 = HttpMcpTransport("https://x.example/api")
        assert t2._url == "https://x.example/api/mcp"
        with pytest.raises(ValueError):
            HttpMcpTransport("")

    def test_modern_transport_sends_metadata_and_routing_headers(self, monkeypatch):
        import json as _json
        from src.features.ai_harness.mcp_http_transport import HttpMcpTransport

        calls: list = []

        class _Response:
            status = 200

            def __init__(self, body):
                self.headers = {}
                self._body = body.encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def _urlopen(request, timeout):
            payload = _json.loads(request.data)
            calls.append((request, payload, timeout))
            if payload["method"] == "server/discover":
                body = {"jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": "2026-07-28"}}
            else:
                body = {"jsonrpc": "2.0", "id": payload["id"], "result": {"content": []}}
            return _Response(_json.dumps(body))

        transport = HttpMcpTransport("https://example.com/mcp", api_key="secret")
        # AUD-22017：transport 改用自带 _NoRedirectHandler 的实例 opener，不再
        # 走模块级 urlopen——测试改为替换实例 opener（同一响应契约）。
        monkeypatch.setattr(transport, "_opener", _StubOpener(_urlopen))
        response = transport.send_request({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "get_weather", "arguments": {}},
        })
        assert response["id"] == 7
        request, payload, _ = calls[-1]
        assert request.headers["Mcp-method"] == "tools/call"
        assert request.headers["Mcp-name"] == "get_weather"
        assert request.headers["Mcp-protocol-version"] == "2026-07-28"
        assert payload["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        assert request.headers["Authorization"] == "Bearer secret"

    def test_legacy_transport_falls_back_to_initialize(self, monkeypatch):
        import json as _json
        from src.features.ai_harness.mcp_http_transport import HttpMcpTransport

        calls: list = []

        class _Response:
            def __init__(self, status, body, headers=None):
                self.status = status
                self.headers = headers or {}
                self._body = body.encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def _urlopen(request, timeout):
            payload = _json.loads(request.data)
            calls.append((request, payload))
            if payload["method"] == "server/discover":
                return _Response(404, "not found")
            if payload["method"] == "initialize":
                return _Response(
                    200,
                    _json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": "2024-11-05"}}),
                    {"Mcp-Session-Id": "legacy-session"},
                )
            if payload["method"] == "notifications/initialized":
                return _Response(202, "")
            return _Response(200, _json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"content": []}}))

        transport = HttpMcpTransport("https://example.com/mcp")
        # AUD-22017：同上——patch 实例 opener 而非模块级 urlopen。
        monkeypatch.setattr(transport, "_opener", _StubOpener(_urlopen))
        transport.send_request({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "x", "arguments": {}}})
        call_request = next(request for request, payload in calls if payload["method"] == "tools/call")
        assert call_request.headers["Mcp-session-id"] == "legacy-session"
        assert "Mcp-protocol-version" not in call_request.headers


def _seed_web_client(gw, profile_id, fake_transport):
    """把带 fake transport 的 McpOutboundClient 直接种进网关（绕过真实 HTTP）。"""
    from src.features.ai_harness.mcp_outbound import (
        McpOutboundClient, McpServerRegistry,
    )
    from src.features.edit.ai_editing.mcp_vision_gateway import (
        ZHIPU_SEARCH_PROFILE_ID,
        build_zhipu_reader_profile,
        build_zhipu_search_profile,
    )
    profile = (
        build_zhipu_search_profile() if profile_id == ZHIPU_SEARCH_PROFILE_ID
        else build_zhipu_reader_profile()
    )
    registry = McpServerRegistry()
    registry.register(profile)
    client = McpOutboundClient(registry, transport_factory=lambda p: fake_transport)
    gw._http_clients[profile_id] = client


class TestWebToolRouting:
    def _gw(self):
        return McpVisionGateway(api_key_provider=lambda: "k", npx_path="/fake/npx")

    def test_web_search_maps_query(self):
        gw = self._gw()
        fake = _FakeTransport()
        _seed_web_client(gw, "zhipu-web-search-mcp", fake)
        r = gw.invoke("topos.mcp.web_search", {"query": "2026 电影票房"})
        assert r["ok"] is True
        call = _last_tool_call(fake)
        assert call["method"] == "tools/call"
        assert call["params"]["name"] == "web_search_prime"
        assert call["params"]["arguments"]["search_query"] == "2026 电影票房"

    def test_web_search_domain_filter(self):
        gw = self._gw()
        fake = _FakeTransport()
        _seed_web_client(gw, "zhipu-web-search-mcp", fake)
        gw.invoke("topos.mcp.web_search", {"query": "x", "domain_filter": "www.example.com"})
        args = _last_tool_call(fake)["params"]["arguments"]
        assert args["search_domain_filter"] == "www.example.com"

    def test_web_search_empty_query_rejected(self):
        gw = self._gw()
        r = gw.invoke("topos.mcp.web_search", {"query": ""})
        assert r["ok"] is False and "query" in r["error"]

    def test_web_read_maps_url(self):
        gw = self._gw()
        fake = _FakeTransport()
        _seed_web_client(gw, "zhipu-web-reader-mcp", fake)
        r = gw.invoke("topos.mcp.web_read", {"url": "https://example.com/a", "return_format": "markdown"})
        assert r["ok"] is True
        args = _last_tool_call(fake)["params"]["arguments"]
        assert args["url"] == "https://example.com/a"
        assert args["return_format"] == "markdown"

    def test_web_read_rejects_non_http(self):
        gw = self._gw()
        r = gw.invoke("topos.mcp.web_read", {"url": "ftp://x"})
        assert r["ok"] is False and "http" in r["error"]

    def test_web_unavailable_without_key(self):
        gw = McpVisionGateway(api_key_provider=lambda: None, npx_path="/fake/npx")
        r = gw.invoke("topos.mcp.web_search", {"query": "x"})
        assert r["ok"] is False and "不可用" in r["error"]


# ================================================================
# 管理 API（偏好页「MCP 服务」分区）
# ================================================================


class TestServerManagement:
    def _gw(self):
        return McpVisionGateway(
            api_key_provider=lambda: "k", npx_path="/fake/npx",
            transport_factory=lambda p: None,
        )

    def test_server_statuses_lists_three_servers(self):
        statuses = self._gw().server_statuses()
        ids = [s["profile_id"] for s in statuses]
        assert ids == [
            "zhipu-vision-mcp", "zhipu-web-search-mcp", "zhipu-web-reader-mcp",
        ]
        for s in statuses:
            assert s["available"] is True
            assert s["enabled"] is True
            assert s["author"]
            assert s["transport"]
            assert s["tools"]
        # 展示名与工具归属
        vision = statuses[0]
        assert "Vision" in vision["name"]
        assert "topos.mcp.analyze_image" in vision["tools"]
        assert "topos.mcp.web_search" in statuses[1]["tools"]

    def test_status_unavailable_reasons(self, monkeypatch):
        # 无 npx + 无 key → 视觉不可用且给出原因；HTTP 服务只依赖 key。
        monkeypatch.setattr(
            "src.features.edit.ai_editing.mcp_vision_gateway.resolve_npx_path",
            lambda: "",
        )
        gw = McpVisionGateway(api_key_provider=lambda: None, npx_path="")
        statuses = {s["profile_id"]: s for s in gw.server_statuses()}
        assert statuses["zhipu-vision-mcp"]["available"] is False
        assert "npx" in statuses["zhipu-vision-mcp"]["reason"]
        assert statuses["zhipu-web-search-mcp"]["available"] is False
        assert "key" in statuses["zhipu-web-search-mcp"]["reason"]

    def test_disable_server_hides_its_tools(self):
        gw = self._gw()
        assert gw.set_server_enabled("zhipu-vision-mcp", False) is True
        names = [d.name for d in gw.tool_definitions()]
        assert "topos.mcp.analyze_image" not in names
        assert "topos.mcp.web_search" in names  # 其它 server 不受影响
        # 调用也直接降级（不发起 MCP 请求）
        r = gw.invoke("topos.mcp.analyze_image", {"image_ref": "latest"})
        assert r["ok"] is False and "禁用" in r["error"]

    def test_disable_web_server_only(self):
        gw = self._gw()
        gw.set_server_enabled("zhipu-web-search-mcp", False)
        names = [d.name for d in gw.tool_definitions()]
        assert "topos.mcp.web_search" not in names
        assert "topos.mcp.web_read" in names
        assert "topos.mcp.analyze_image" in names

    def test_reenable_restores_tools(self):
        gw = self._gw()
        gw.set_server_enabled("zhipu-vision-mcp", False)
        gw.set_server_enabled("zhipu-vision-mcp", True)
        names = [d.name for d in gw.tool_definitions()]
        assert "topos.mcp.analyze_image" in names

    def test_unknown_profile_rejected(self):
        gw = self._gw()
        assert gw.set_server_enabled("no-such-server", True) is False


# ================================================================
# ToolFacade 合并 + 路由
# ================================================================


class TestFacadeMerge:
    def _facade_with_gateway(self, tmp_path):
        from src.features.edit.ai_editing.edit_api import EditSession
        from src.features.edit.ai_editing.tool_facade import ToolFacade

        gw, fake, img = _make_gateway_with_fake(tmp_path)
        EditSession.unbind()
        facade = ToolFacade(None, mcp_gateway=gw)  # session 未用于 MCP 路径
        return facade, gw, fake, img

    def test_list_tools_includes_mcp_tools(self, tmp_path):
        facade, *_ = self._facade_with_gateway(tmp_path)
        names = [t.name for t in facade.list_tools()]
        assert "topos.mcp.analyze_image" in names
        assert "topos.mcp.analyze_video" in names
        # 本地工具不受影响
        assert "topos.get_project_snapshot" in names

    def test_invoke_routes_to_gateway_and_sanitizes(self, tmp_path):
        import json as _json
        facade, gw, fake, img = self._facade_with_gateway(tmp_path)
        r = facade.invoke("topos.mcp.analyze_image", {"image_ref": "latest"})
        assert r["ok"] is True
        # sanitize：analysis 文本被标为不可信（transport 表示）
        assert r["analysis"]["trust"] == "untrusted"
        _json.dumps(r)  # JSON-safe 契约

    def test_facade_without_gateway_unknown_tool(self):
        from src.features.edit.ai_editing.tool_facade import ToolFacade

        facade = ToolFacade.__new__(ToolFacade)
        facade._mcp_gateway = None
        r = facade._invoke_mcp_tool("topos.mcp.analyze_image", {})
        assert r["ok"] is False
