"""MCP Streamable HTTP transport with legacy HTTP+SSE compatibility.

Modern requests are stateless and carry protocol/client metadata in the body
and mirrored routing headers.  Older remote MCP endpoints (including the
configured API-key endpoints in this project) still use initialize plus a
session id, so the transport probes modern discovery and falls back safely.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from src.features.ai_harness.mcp_protocol import (
    LATEST_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSION,
    add_request_metadata,
    validate_tool_definition,
)

_LOG = logging.getLogger(__name__)
_CLIENT_NAME = "topos-mcp-client"
_CLIENT_VERSION = "1.1"
_DEFAULT_TIMEOUT = 60.0
_HEADER_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class McpHttpTransportError(RuntimeError):
    """HTTP 传输层错误（非 2xx / 解析失败 / 超时）。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class McpHttpAuthorizationError(McpHttpTransportError):
    """The remote server rejected the configured bearer credential."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every HTTP redirect (AUD-22017).

    CPython's default redirect handling rebuilds the request to the ``Location``
    target and only strips body-ish headers — the ``Authorization`` bearer header
    would be replayed to an arbitrary host (including an https→http downgrade that
    bypasses the constructor's localhost-only plain-HTTP constraint).  Returning
    ``None`` makes urllib surface the 30x as ``HTTPError`` instead, which ``_post``
    converts into a fail-closed :class:`McpHttpTransportError`.
    """

    def redirect_request(  # type: ignore[override]
        self, req: urllib.request.Request, fp: Any, code: int,
        msg: str, headers: Any, newurl: str,
    ) -> urllib.request.Request | None:
        return None


def _parse_sse_messages(raw: str) -> list[dict[str, Any]]:
    """Parse JSON or all request-scoped SSE events, not just the first line."""
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def flush_event() -> None:
        if not data_lines:
            return
        payload = "\n".join(data_lines).strip()
        data_lines.clear()
        if not payload:
            return
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(value, dict):
            messages.append(value)

    for line in raw.splitlines():
        if not line:
            flush_event()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    flush_event()
    if messages:
        return messages
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [value] if isinstance(value, dict) else []


def _parse_sse_or_json(raw: str) -> dict[str, Any] | None:
    """Backward-compatible helper returning the first parsed JSON-RPC object."""
    messages = _parse_sse_messages(raw)
    return messages[0] if messages else None


class HttpMcpTransport:
    """Remote MCP transport implementing modern and legacy HTTP modes."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        extra_headers: dict[str, str] | None = None,
        request_timeout: float = _DEFAULT_TIMEOUT,
        startup_timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must not be empty")
        self._url = base_url if base_url.endswith("/mcp") else base_url.rstrip("/") + "/mcp"
        parsed = urllib.parse.urlparse(self._url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("MCP HTTP endpoint must use http:// or https://")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("plain HTTP MCP endpoints are allowed only on localhost")
        self._api_key = api_key
        self._extra_headers = dict(extra_headers or {})
        self._request_timeout = max(0.1, float(request_timeout))
        self._startup_timeout = max(0.1, float(startup_timeout))
        self._session_id: str | None = None
        self._mode: str | None = None  # modern / legacy
        self._lock = threading.RLock()
        self._next_id = 100
        self._tool_schemas: dict[str, dict[str, Any]] = {}
        # AUD-22017：自带 opener 禁用重定向——绝不让 Authorization（Bearer
        # api_key）被 30x 引到构造期校验之外的任意主机（含 https→http 降级）。
        # 用实例 opener 而非 install_opener，避免污染进程级全局状态。
        self._opener = urllib.request.build_opener(_NoRedirectHandler)

    # ---------------- 协议入口 ---------------- #

    def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._mode is None:
            self.start()
        with self._lock:
            req_id = request.get("id")
            if req_id is None:
                req_id = self._next_request_id()
                request = {**request, "id": req_id}
            prepared = self._prepare_request(request)
            status, _, body = self._post(prepared, timeout=self._request_timeout, mode=self._mode)
            response = self._response_for_id(body, req_id)
            if status < 200 or status >= 300:
                self._raise_http_error(status, body, response)
            if response is None:
                raise McpHttpTransportError(
                    f"MCP HTTP 响应缺少匹配 id={req_id!r} 的 JSON-RPC 响应"
                )
            self._raise_jsonrpc_error(response)
            self._remember_tool_catalog(prepared, response)
            return response

    def start(self) -> None:
        """Probe modern discovery; use legacy initialize only when required."""
        with self._lock:
            if self._mode is not None:
                return
            probe_id = self._next_request_id()
            probe = add_request_metadata({
                "jsonrpc": "2.0", "id": probe_id,
                "method": "server/discover", "params": {},
            }, protocol_version=LATEST_PROTOCOL_VERSION,
                client_name=_CLIENT_NAME, client_version=_CLIENT_VERSION)
            status, session_id, body = self._post(
                probe, timeout=self._startup_timeout, mode="modern",
            )
            response = self._response_for_id(body, probe_id)
            if 200 <= status < 300 and isinstance(response, dict) and isinstance(response.get("result"), dict):
                self._mode = "modern"
                self._remember_tool_catalog(probe, response)
                return
            # A current server may omit optional discovery and return a
            # JSON-RPC method-not-found over 404; keep modern mode in that case.
            if status == 404 and isinstance(response, dict) and response.get("error", {}).get("code") == -32601:
                self._mode = "modern"
                return
            if status in (401, 403):
                self._raise_http_error(status, body, response)
            self._legacy_start()

    def _legacy_start(self) -> None:
        req_id = self._next_request_id()
        init = {
            "jsonrpc": "2.0", "id": req_id, "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
            },
        }
        status, session_id, body = self._post(init, timeout=self._startup_timeout, mode="legacy")
        response = self._response_for_id(body, req_id)
        if status < 200 or status >= 300 or not isinstance(response, dict) or not isinstance(response.get("result"), dict):
            self._raise_http_error(status, body, response)
            raise McpHttpTransportError(f"MCP HTTP initialize 失败：HTTP {status}")
        self._session_id = session_id
        self._mode = "legacy"
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        notification_status, _, notification_body = self._post(
            notification, timeout=self._startup_timeout, mode="legacy",
        )
        if notification_status not in (200, 202, 204):
            self._raise_http_error(notification_status, notification_body, None)

    def close(self) -> None:
        with self._lock:
            self._mode = None
            self._session_id = None
            self._tool_schemas.clear()

    # ---------------- 内部 ---------------- #

    def _prepare_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._mode != "modern":
            return request
        return add_request_metadata(
            request,
            protocol_version=LATEST_PROTOCOL_VERSION,
            client_name=_CLIENT_NAME,
            client_version=_CLIENT_VERSION,
        )

    def _post(
        self, payload: dict[str, Any], *, timeout: float, mode: str | None,
    ) -> tuple[int, str | None, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        # User headers cannot override protocol routing or authentication
        # headers; those values must be derived from the JSON-RPC body.
        for key, value in self._extra_headers.items():
            if key.lower() not in {
                "authorization", "host", "content-length", "mcp-protocol-version",
                "mcp-method", "mcp-name", "mcp-session-id",
            }:
                headers[key] = value
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if mode == "modern":
            headers["MCP-Protocol-Version"] = LATEST_PROTOCOL_VERSION
            headers["Mcp-Method"] = str(payload.get("method", ""))
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            if payload.get("method") in {"tools/call", "resources/read", "prompts/get"}:
                name = params.get("name") or params.get("uri")
                if name is not None:
                    headers["Mcp-Name"] = _encode_header_value(str(name))
            headers.update(self._custom_parameter_headers(payload))
        elif self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(self._url, data=data, headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=timeout) as resp:  # noqa: S310 — configured endpoint, redirects disabled
                return (
                    resp.status,
                    resp.headers.get("Mcp-Session-Id"),
                    resp.read().decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if 300 <= exc.code < 400:
                # AUD-22017：_NoRedirectHandler 把 30x 交还这里——fail-closed，
                # 凭据绝不被转发到 Location 目标。
                location = (
                    str(exc.headers.get("Location", "") or "")
                    if exc.headers is not None else ""
                )
                raise McpHttpTransportError(
                    f"MCP HTTP 端点返回重定向（HTTP {exc.code} → "
                    f"{location[:200]!r}）已拒绝：Authorization 凭据不允许被"
                    "转发到其他地址",
                    code=exc.code,
                ) from exc
            if exc.code in (401, 403):
                raise McpHttpAuthorizationError(
                    f"MCP HTTP authorization failed: HTTP {exc.code}",
                ) from exc
            return exc.code, exc.headers.get("Mcp-Session-Id"), body
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise McpHttpTransportError(f"MCP HTTP 请求失败：{exc}") from exc

    def _custom_parameter_headers(self, payload: dict[str, Any]) -> dict[str, str]:
        if payload.get("method") != "tools/call":
            return {}
        params = payload.get("params")
        if not isinstance(params, dict):
            return {}
        name = params.get("name")
        arguments = params.get("arguments")
        schema = self._tool_schemas.get(str(name))
        if not isinstance(arguments, dict) or not isinstance(schema, dict):
            return {}
        headers: dict[str, str] = {}

        def walk(node: dict[str, Any], value: Any, path: tuple[str, ...]) -> None:
            header_name = node.get("x-mcp-header")
            if isinstance(header_name, str) and _HEADER_TOKEN_RE.fullmatch(header_name):
                if value is not None and isinstance(value, (str, int, bool)) and not isinstance(value, float):
                    headers[f"Mcp-Param-{header_name}"] = _encode_header_value(_header_scalar(value))
            properties = node.get("properties")
            if not isinstance(properties, dict) or not isinstance(value, dict):
                return
            for key, child in properties.items():
                if key in value and isinstance(child, dict):
                    walk(child, value[key], path + (str(key),))

        walk(schema.get("inputSchema", {}), arguments, ())
        return headers

    def _remember_tool_catalog(self, request: dict[str, Any], response: dict[str, Any]) -> None:
        if request.get("method") != "tools/list":
            return
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            return
        for tool in result["tools"]:
            error = validate_tool_definition(tool)
            if error is None:
                error = _validate_header_annotations(tool.get("inputSchema", {}))
            if error:
                _LOG.warning("忽略不安全 MCP tool definition: %s", error)
                continue
            self._tool_schemas[str(tool["name"])] = dict(tool)

    @staticmethod
    def _response_for_id(raw: str, request_id: Any) -> dict[str, Any] | None:
        for message in _parse_sse_messages(raw):
            if message.get("id") == request_id:
                return message
        return None

    @staticmethod
    def _raise_jsonrpc_error(response: dict[str, Any]) -> None:
        error = response.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            raise McpHttpTransportError(
                f"MCP JSON-RPC 错误 {code}: {error.get('message')}",
                code=code if isinstance(code, int) else None,
            )

    @classmethod
    def _raise_http_error(
        cls, status: int, body: str, response: dict[str, Any] | None,
    ) -> None:
        if status in (401, 403):
            raise McpHttpAuthorizationError(f"MCP HTTP authorization failed: HTTP {status}")
        if response is not None:
            cls._raise_jsonrpc_error(response)
        raise McpHttpTransportError(f"MCP HTTP 请求失败：HTTP {status}；body={body[:300]!r}")

    def _next_request_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value


def _header_scalar(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _encode_header_value(value: str) -> str:
    """Encode unsafe HTTP header values using the MCP base64 sentinel."""
    if (
        value == value.strip()
        and all(0x20 <= ord(char) <= 0x7E for char in value)
        and not (value.startswith("=?base64?") and value.endswith("?="))
    ):
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?base64?{encoded}?="


def _validate_header_annotations(schema: dict[str, Any]) -> str | None:
    """Validate the current HTTP ``x-mcp-header`` schema extension."""
    names: set[str] = set()

    def walk(node: Any, *, through_array: bool = False) -> str | None:
        if not isinstance(node, dict):
            return None
        header = node.get("x-mcp-header")
        if header is not None:
            if through_array:
                return "x-mcp-header cannot be nested below an array"
            if not isinstance(header, str) or not _HEADER_TOKEN_RE.fullmatch(header):
                return "x-mcp-header must be a valid non-empty HTTP header token"
            if header.lower() in names:
                return "x-mcp-header values must be unique"
            names.add(header.lower())
            value_type = node.get("type")
            if value_type not in {"string", "integer", "boolean"}:
                return "x-mcp-header supports only string, integer, or boolean properties"
        properties = node.get("properties")
        if isinstance(properties, dict):
            for child in properties.values():
                error = walk(child, through_array=through_array)
                if error:
                    return error
        if "items" in node:
            error = walk(node["items"], through_array=True)
            if error:
                return error
        return None

    return walk(schema)


__all__ = [
    "HttpMcpTransport",
    "McpHttpAuthorizationError",
    "McpHttpTransportError",
    "_parse_sse_messages",
    "_parse_sse_or_json",
    "_validate_header_annotations",
]
