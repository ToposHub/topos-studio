"""Shared MCP protocol helpers.

The project supports both the current stateless MCP revision and older servers
which still require the ``initialize`` handshake.  Keeping the wire helpers in
one module prevents the stdio and HTTP clients from drifting apart.
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any


LATEST_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = (
    LATEST_PROTOCOL_VERSION,
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    LEGACY_PROTOCOL_VERSION,
)

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# AUD-22101：schema 来自外部 MCP server（不可信数据）。校验在调用线程上
# 同步执行，必须给组合爆炸（allOf/anyOf/oneOf 嵌套全量求值）与超长回溯型
# pattern 设硬上限：总递归节点数、单组合关键字分支数、pattern 字符数。
_MAX_SCHEMA_NODES = 2048
_MAX_SCHEMA_VARIANTS = 64
_MAX_SCHEMA_PATTERN_CHARS = 256


class _SchemaBudget:
    """Bounded work counter threaded through one schema validation run."""

    def __init__(self, max_nodes: int = _MAX_SCHEMA_NODES) -> None:
        self.remaining_nodes = max_nodes

    def spend_node(self, path: str) -> str | None:
        self.remaining_nodes -= 1
        if self.remaining_nodes < 0:
            return f"{path}: schema complexity budget exceeded"
        return None


def add_request_metadata(
    request: dict[str, Any],
    *,
    protocol_version: str = LATEST_PROTOCOL_VERSION,
    client_name: str = "topos-mcp-client",
    client_version: str = "1.0",
    client_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a request carrying the current per-request MCP metadata."""

    params = request.get("params")
    params = dict(params) if isinstance(params, dict) else {}
    meta = params.get("_meta")
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta.update({
        META_PROTOCOL_VERSION: protocol_version,
        META_CLIENT_INFO: {"name": client_name, "version": client_version},
        META_CLIENT_CAPABILITIES: dict(client_capabilities or {}),
    })
    params["_meta"] = meta
    return {**request, "params": params}


def request_metadata(request: dict[str, Any]) -> dict[str, Any]:
    params = request.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return dict(meta) if isinstance(meta, dict) else {}


def request_protocol_version(request: dict[str, Any]) -> str:
    value = request_metadata(request).get(META_PROTOCOL_VERSION)
    return str(value) if value else ""


def validate_tool_definition(tool: Any) -> str | None:
    """Validate the safety-critical shape of a ``tools/list`` entry."""

    if not isinstance(tool, dict):
        return "tool definition must be an object"
    name = tool.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
        return "tool name must contain 1-128 ASCII letters, digits, '.', '_' or '-'"
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return f"tool {name!r} inputSchema must be an object"
    if "outputSchema" in tool and not isinstance(tool["outputSchema"], dict):
        return f"tool {name!r} outputSchema must be an object"
    return None


def validate_tool_result(result: Any) -> str | None:
    """Validate the untrusted structural part of a ``tools/call`` result."""
    if not isinstance(result, dict):
        return "tool result must be an object"
    content = result.get("content")
    if content is None:
        return None
    if not isinstance(content, list):
        return "tool result content must be an array"
    allowed_types = {"text", "image", "audio", "resource_link", "resource"}
    for index, item in enumerate(content):
        if not isinstance(item, dict):
            return f"tool result content[{index}] must be an object"
        item_type = item.get("type")
        if item_type not in allowed_types:
            return f"tool result content[{index}] has unsupported type {item_type!r}"
        if item_type == "text" and not isinstance(item.get("text"), str):
            return f"tool result content[{index}] text must be a string"
        if item_type in {"image", "audio"} and not isinstance(item.get("data"), str):
            return f"tool result content[{index}] data must be base64 text"
        if item_type == "resource_link" and not isinstance(item.get("uri"), str):
            return f"tool result content[{index}] uri must be a string"
    return None


def validate_json_schema(
    value: Any, schema: Any, *, path: str = "$", max_depth: int = 32,
    _root_schema: dict[str, Any] | None = None,
    _budget: "_SchemaBudget | None" = None,
) -> str | None:
    """Small dependency-free JSON Schema validator for MCP tool boundaries.

    It intentionally covers the keywords used by the project's tool schemas
    and common MCP servers.  Unsupported annotation keywords are ignored; a
    malformed structural keyword is rejected rather than silently widening
    the trust boundary.

    AUD-22101：schema 是不可信输入，校验线程必须有界——单次校验总共访问
    的 schema 节点数（``_MAX_SCHEMA_NODES``）、单个组合关键字分支数
    （``_MAX_SCHEMA_VARIANTS``）与 pattern 长度（``_MAX_SCHEMA_PATTERN_CHARS``）
    超限即拒绝，阻断组合爆炸与超长回溯正则。
    """

    budget = _budget if _budget is not None else _SchemaBudget()
    exhausted = budget.spend_node(path)
    if exhausted is not None:
        return exhausted
    if max_depth <= 0:
        return f"{path}: schema nesting is too deep"
    if schema is True or schema is None:
        return None
    if schema is False:
        return f"{path}: value is not allowed"
    if not isinstance(schema, dict):
        return f"{path}: schema must be an object"
    root_schema = _root_schema if _root_schema is not None else schema
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return f"{path}: only local JSON Schema $ref values are supported"
        target: Any = root_schema
        try:
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return f"{path}: unresolved JSON Schema reference {reference!r}"
        return validate_json_schema(
            value, target, path=path, max_depth=max_depth - 1,
            _root_schema=root_schema, _budget=budget,
        )

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not any(value == candidate for candidate in enum):
            return f"{path}: value is not one of the allowed values"

    schema_type = schema.get("type")
    if schema_type is not None:
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        if not all(isinstance(item, str) for item in types):
            return f"{path}: invalid schema type"
        if not any(_json_type_matches(value, item) for item in types):
            return f"{path}: expected type {schema_type!r}"

    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        variants = schema[keyword]
        if not isinstance(variants, list) or not variants:
            return f"{path}: invalid {keyword} schema"
        if len(variants) > _MAX_SCHEMA_VARIANTS:
            # AUD-22101：分支数超限——嵌套组合求值是分支数^深度级的工作量。
            return (
                f"{path}: {keyword} has {len(variants)} variants "
                f"(max {_MAX_SCHEMA_VARIANTS})"
            )
        errors = [validate_json_schema(
            value, item, path=path, max_depth=max_depth - 1,
            _root_schema=root_schema, _budget=budget,
        ) for item in variants]
        if keyword == "allOf" and any(error for error in errors):
            return next(error for error in errors if error)
        if keyword == "anyOf" and all(error for error in errors):
            return f"{path}: value does not match any allowed schema"
        if keyword == "oneOf" and sum(error is None for error in errors) != 1:
            return f"{path}: value must match exactly one schema"

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            return f"{path}: invalid required schema"
        for key in required:
            if key not in value:
                return f"{path}: missing required property {key!r}"

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return f"{path}: invalid properties schema"
        for key, child_schema in properties.items():
            if key in value:
                error = validate_json_schema(
                    value[key], child_schema, path=f"{path}.{key}",
                    max_depth=max_depth - 1, _root_schema=root_schema,
                    _budget=budget,
                )
                if error:
                    return error
        additional = schema.get("additionalProperties", True)
        if additional is False:
            unknown = [key for key in value if key not in properties]
            if unknown:
                return f"{path}: unknown properties {unknown!r}"
        elif not isinstance(additional, (bool, dict)):
            return f"{path}: invalid additionalProperties schema"
        elif isinstance(additional, dict):
            for key in value:
                if key not in properties:
                    error = validate_json_schema(
                        value[key], additional, path=f"{path}.{key}",
                        max_depth=max_depth - 1, _root_schema=root_schema,
                        _budget=budget,
                    )
                    if error:
                        return error

    if isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        if not isinstance(item_schema, (bool, dict)):
            return f"{path}: invalid items schema"
        for index, item in enumerate(value):
            error = validate_json_schema(
                item, item_schema, path=f"{path}[{index}]",
                max_depth=max_depth - 1, _root_schema=root_schema,
                _budget=budget,
            )
            if error:
                return error

    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            return f"{path}: string is shorter than minLength"
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            return f"{path}: string is longer than maxLength"
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            # AUD-22101：server 提供的 pattern 交给无超时的 re 引擎前先限长，
            # 超长（潜在灾难性回溯）pattern 直接拒绝。
            if len(pattern) > _MAX_SCHEMA_PATTERN_CHARS:
                return (
                    f"{path}: pattern is too long "
                    f"({len(pattern)} > {_MAX_SCHEMA_PATTERN_CHARS} chars)"
                )
            try:
                if re.search(pattern, value) is None:
                    return f"{path}: string does not match pattern"
            except re.error:
                return f"{path}: invalid pattern in schema"

    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            return f"{path}: array is shorter than minItems"
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            return f"{path}: array is longer than maxItems"

    return None


def _json_type_matches(value: Any, schema_type: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(schema_type, False)


class SlidingWindowRateLimiter:
    """Bounded in-memory rate limiter for a single MCP process/session."""

    def __init__(self, max_calls: int = 120, window_seconds: float = 60.0) -> None:
        self.max_calls = max(1, int(max_calls))
        self.window_seconds = max(1.0, float(window_seconds))
        self._calls: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        cutoff = current - self.window_seconds
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            return False
        self._calls.append(current)
        return True


__all__ = [
    "LATEST_PROTOCOL_VERSION",
    "LEGACY_PROTOCOL_VERSION",
    "META_CLIENT_CAPABILITIES",
    "META_CLIENT_INFO",
    "META_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "SlidingWindowRateLimiter",
    "add_request_metadata",
    "request_metadata",
    "request_protocol_version",
    "validate_json_schema",
    "validate_tool_definition",
    "validate_tool_result",
]
