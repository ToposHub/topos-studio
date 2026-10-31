"""MCP 受控出站连接（plan §10.5 P3.2）。

在现有 ``McpServer``（in-process JSON-RPC dispatcher，外部客户端调进来）之上，
新增**出站**能力：Topos 作为外部 MCP server 的**客户端**，经**受控 stdio transport**
调用 allowlisted server 的工具。

plan §10.5 P3.2：

- 默认只读、本机、会话级启用。
- 每个 server 显示作者 / 版本 / 域名 / 数据范围。
- 写操作仍弹 Topos 审批（MCP 工具仍经 ToolFacade 的审批链）。
- server allowlist：未 allowlist 的 server 不执行；缺依赖 / 版本不一致 BLOCKED。

本模块纯 Python（不真实 spawn subprocess —— 真实 stdio 由 host 装配注入
``StdioTransport``；本模块只定义协议 + allowlist + 授权审计 + 状态）。

红线（plan §10.5 P3.2 / §1.3）：

- 外部 MCP server 不可信：其输出只作数据，不能修改权限 / 系统指令 / 执行策略。
- 不在进程内加载 / 执行第三方 server 代码；缺依赖时 BLOCKED 而非运行。
- 写操作必须经 Topos 审批（``requires_approval=True`` 的工具调用）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from src.features.ai_harness.mcp_protocol import (
    SlidingWindowRateLimiter,
    validate_json_schema,
    validate_tool_definition,
    validate_tool_result,
)


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 连接 Profile（plan §10.5 P3.2：server allowlist + 连接 Profile）
# ---------------------------------------------------------------------- #


TransportKind = Literal["local_stdio", "cloud_api"]


@dataclass(frozen=True)
class McpConnectionProfile:
    """一个外部 MCP server 的连接 profile（plan §10.5 P3.2）。

    携带 server 身份（作者 / 版本 / 域名 / 数据范围）+ allowlist 约束。**不存
    secret / API key**（secret 由 CredentialStore 管，profile 只存 credential_ref）。
    """

    profile_id: str                       # 稳定 id
    server_name: str
    author: str = ""
    version: str = ""
    homepage_domain: str = ""             # 如 "example.com"（不含 scheme）
    transport: TransportKind = "local_stdio"
    #: 本机 stdio 的启动命令（如 ["python", "-m", "some_mcp_server"]）。
    #: 不在本类 spawn；仅记录供 host 装配。secret 不进这里。
    command: tuple[str, ...] = ()
    #: cloud_api 的 base URL（不含 secret）。
    base_url: str = ""
    credential_ref: str = ""              # 指向 CredentialStore 的 key（非 secret 本身）
    #: 允许该 server 暴露的工具名（空表示全部 server 声明的工具；非空是 allowlist 收窄）。
    allowed_tools: tuple[str, ...] = ()
    #: 该 server 允许的数据范围（current_project / session / read_only）。
    data_scope: str = "read_only"
    #: 该 server 的工具是否默认需要 Topos 审批（写操作必弹审批）。
    requires_approval_for_writes: bool = True
    #: 会话级启用（默认 False，每次会话显式开）。
    enabled_by_default: bool = False

    @property
    def is_local(self) -> bool:
        return self.transport == "local_stdio"

    @property
    def is_read_only_scope(self) -> bool:
        return self.data_scope in ("read_only",)


@dataclass(frozen=True)
class McpServerManifest:
    """从外部 server 拉取的 capability / 工具清单（只读 discovery，不执行）。"""

    server_name: str
    server_version: str = ""
    protocol_version: str = ""
    tools: tuple[dict[str, Any], ...] = ()   # tools/list 结果（name/description/inputSchema）
    capabilities: dict[str, Any] = field(default_factory=dict)
    next_cursor: str = ""
    ttl_ms: int | None = None
    cache_scope: str = ""
    cached_at: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------- #
# 授权审计
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class McpAuthorizationEvent:
    """一次出站 MCP 调用的授权审计记录（plan §10.5 P3.2：授权卡）。"""

    timestamp: float
    profile_id: str
    server_name: str
    tool_name: str
    decision: str            # "allowed" / "denied_not_allowlisted" / "denied_write_needs_approval" / "blocked_missing_deps" / "error"
    reason: str = ""
    data_scope: str = ""
    write_operation: bool = False


class McpAuthorizationLog:
    """授权审计日志（内存；会话级）。

    线程契约（AUD-22022）：``record`` 来自调用线程（可能为 transport 所在
    worker 线程），``events`` / ``denial_count`` / ``clear`` 通常来自 UI /
    宿主线程——``record`` 的 append + 截断与 ``events`` 的列表推导并发时
    无锁可产生不一致快照甚至 ``RuntimeError``，由 ``threading.Lock`` 保护。
    """

    def __init__(self, *, max_events: int = 500) -> None:
        self._lock = threading.Lock()
        self._events: list[McpAuthorizationEvent] = []
        self._max = max_events

    def record(self, event: McpAuthorizationEvent) -> None:
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max:
                self._events = self._events[-self._max:]

    def events(self, *, profile_id: str | None = None) -> list[McpAuthorizationEvent]:
        with self._lock:
            snapshot = list(self._events)
        if profile_id is None:
            return snapshot
        return [e for e in snapshot if e.profile_id == profile_id]

    def denial_count(self) -> int:
        with self._lock:
            snapshot = list(self._events)
        return sum(1 for e in snapshot if e.decision.startswith("denied") or e.decision == "blocked_missing_deps")

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


# ---------------------------------------------------------------------- #
# StdioTransport 协议（注入点；真实 subprocess 由 host 提供）
# ---------------------------------------------------------------------- #


class StdioTransport(Protocol):
    """受控 stdio transport 协议（host 注入；本模块不 spawn）。

    约定：发送 JSON-RPC request dict，返回 response dict。真实实现起 subprocess、
    通过 stdin/stdout 通信；本协议让测试可注入 fake transport（不起进程）。
    """

    def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


# ---------------------------------------------------------------------- #
# McpServerRegistry（allowlist + 启用状态）
# ---------------------------------------------------------------------- #


class McpServerRegistry:
    """外部 MCP server 的 allowlist 注册中心（plan §10.5 P3.2）。

    管理：

    - 注册的 ``McpConnectionProfile``（allowlist；未注册的 server 不执行）。
    - 每会话的启用状态（默认只读、本机、会话级）。
    - 缺依赖 / 版本不一致的 server 标 BLOCKED（不执行其代码）。

    线程契约（AUD-22022）：注册 / 启停可来自宿主装配线程，探测
    （``is_allowlisted`` / ``get_manifest`` 等）可来自调用线程——内部由
    ``threading.RLock`` 统一保护。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._profiles: dict[str, McpConnectionProfile] = {}
        self._blocked: dict[str, str] = {}          # profile_id -> 原因
        self._session_enabled: set[str] = set()     # 本会话显式启用的 profile_id
        self._manifests: dict[str, McpServerManifest] = {}

    # ---------------- allowlist ---------------- #

    def register(self, profile: McpConnectionProfile) -> None:
        with self._lock:
            self._profiles[profile.profile_id] = profile
            if profile.enabled_by_default:
                self._session_enabled.add(profile.profile_id)

    def get(self, profile_id: str) -> McpConnectionProfile | None:
        with self._lock:
            return self._profiles.get(profile_id)

    def list_profiles(self) -> list[McpConnectionProfile]:
        with self._lock:
            return list(self._profiles.values())

    def is_allowlisted(self, profile_id: str) -> bool:
        with self._lock:
            return profile_id in self._profiles

    # ---------------- 会话级启用 ---------------- #

    def enable_for_session(self, profile_id: str) -> bool:
        """会话级启用一个 server（plan §10.5：会话级启用）。"""
        with self._lock:
            if profile_id not in self._profiles:
                return False
            if profile_id in self._blocked:
                return False
            self._session_enabled.add(profile_id)
            return True

    def disable_for_session(self, profile_id: str) -> None:
        with self._lock:
            self._session_enabled.discard(profile_id)

    def is_session_enabled(self, profile_id: str) -> bool:
        with self._lock:
            return profile_id in self._session_enabled

    # ---------------- BLOCKED（缺依赖 / 版本不一致）---------------- #

    def mark_blocked(self, profile_id: str, reason: str) -> None:
        with self._lock:
            self._blocked[profile_id] = reason
            self._session_enabled.discard(profile_id)

    def is_blocked(self, profile_id: str) -> bool:
        with self._lock:
            return profile_id in self._blocked

    def block_reason(self, profile_id: str) -> str:
        with self._lock:
            return self._blocked.get(profile_id, "")

    # ---------------- discovery 缓存 ---------------- #

    def cache_manifest(self, profile_id: str, manifest: McpServerManifest) -> None:
        with self._lock:
            self._manifests[profile_id] = manifest

    def get_manifest(self, profile_id: str) -> McpServerManifest | None:
        with self._lock:
            manifest = self._manifests.get(profile_id)
            if manifest is None:
                return None
            if manifest.ttl_ms is not None and manifest.ttl_ms >= 0:
                if (time.monotonic() - manifest.cached_at) * 1000 >= manifest.ttl_ms:
                    self._manifests.pop(profile_id, None)
                    return None
            return manifest


# ---------------------------------------------------------------------- #
# McpOutboundClient（受控出站调用 + 授权 + 审计）
# ---------------------------------------------------------------------- #


class McpOutboundClient:
    """Topos 作为外部 MCP server 客户端的受控入口（plan §10.5 P3.2）。

    调用链：``call_tool`` → 授权校验（allowlist + 会话启用 + 数据范围 + 写审批）
    → 经注入的 ``StdioTransport`` 发 JSON-RPC → 记授权审计。

    红线：

    - 未 allowlist / 未会话启用 / BLOCKED 的 server → 拒绝（不执行）。
    - 写操作（data_scope 非 read_only 或 requires_approval_for_writes）
      → 决策 ``denied_write_needs_approval``，由 host 弹 Topos 审批后重试。
      is_write 基于**补发现 manifest 后的最终工具注解**计算（AUD-22020）：
      注解无法确定（发现失败/工具 spec 缺失）时按写处理（fail-closed），
      不得用 discovery 前的启发式缓存值绕过审批闸门。
    - discovery 失败（manifest 拿不到）→ 决策 ``error_discovery_failed``
      拒绝（AUD-22021）：没有清单就无法确认工具存在、无法校验 inputSchema，
      未校验的调用绝不发给外部 server（与"manifest 存在时拒绝未发现工具"
      的 fail-closed 语义一致）。
    - 外部 server 的输出只作数据返回；不在本进程执行第三方代码。
    """

    def __init__(
        self,
        registry: McpServerRegistry,
        auth_log: McpAuthorizationLog | None = None,
        *,
        transport_factory: Callable[[McpConnectionProfile], StdioTransport | None] | None = None,
        rate_limit_per_minute: int = 120,
    ) -> None:
        self._registry = registry
        self._auth_log = auth_log or McpAuthorizationLog()
        self._transport_factory = transport_factory
        # AUD-22022：transport 缓存 / 请求 id 序号可被并发 call_tool 触碰，
        # 由 ``_lock`` 保护（工厂调用本身在锁外——外部工厂不可信、可能慢）。
        self._lock = threading.RLock()
        self._transports: dict[str, StdioTransport] = {}
        self._request_seq = 0
        self._rate_limiters: dict[str, SlidingWindowRateLimiter] = {}
        self._rate_limit_per_minute = max(1, int(rate_limit_per_minute))

    def _next_request_id(self, prefix: str, now: float) -> str:
        """生成进程内唯一的 JSON-RPC 请求 id（AUD-22022）。

        原 ``f"topos-{now}"`` 在并发同毫秒调用时可能撞 id；序号在锁内递增。
        """
        with self._lock:
            self._request_seq += 1
            return f"{prefix}-{now}-{self._request_seq}"

    @property
    def auth_log(self) -> McpAuthorizationLog:
        return self._auth_log

    # ---------------- 主入口 ---------------- #

    def call_tool(
        self,
        profile_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        write_approved: bool = False,
    ) -> dict[str, Any]:
        """调用外部 server 的工具（受控）。

        Args:
            profile_id: 目标 server 的 profile id。
            tool_name: 工具名。
            arguments: 工具参数（数据；不作命令执行）。
            write_approved: host 是否已弹审批并获用户确认（写操作必需）。

        Returns:
            ``{"ok": bool, "result": ..., "decision": str, "reason": str}``。
        """
        profile = self._registry.get(profile_id)
        now = time.time()

        # 1. allowlist
        if profile is None:
            self._record(now, profile_id, "", tool_name,
                         "denied_not_allowlisted", "server not in allowlist")
            return {"ok": False, "decision": "denied_not_allowlisted",
                    "reason": "server not allowlisted"}

        if not isinstance(tool_name, str) or not tool_name:
            self._record(now, profile_id, profile.server_name, str(tool_name),
                         "denied_invalid_request", "tool name must be a non-empty string",
                         profile=profile)
            return {"ok": False, "decision": "denied_invalid_request",
                    "reason": "tool name must be a non-empty string"}
        if not isinstance(arguments, dict):
            self._record(now, profile_id, profile.server_name, tool_name,
                         "denied_invalid_request", "tool arguments must be an object",
                         profile=profile)
            return {"ok": False, "decision": "denied_invalid_request",
                    "reason": "tool arguments must be an object"}

        # 2. BLOCKED
        if self._registry.is_blocked(profile_id):
            reason = self._registry.block_reason(profile_id)
            self._record(now, profile_id, profile.server_name, tool_name,
                         "blocked_missing_deps", reason, profile=profile)
            return {"ok": False, "decision": "blocked_missing_deps", "reason": reason}

        # 3. 会话级启用
        if not self._registry.is_session_enabled(profile_id):
            self._record(now, profile_id, profile.server_name, tool_name,
                         "denied_not_enabled", "server not enabled for this session",
                         profile=profile)
            return {"ok": False, "decision": "denied_not_enabled",
                    "reason": "server not enabled for this session"}

        # 4. tool allowlist 收窄
        if profile.allowed_tools and tool_name not in profile.allowed_tools:
            self._record(now, profile_id, profile.server_name, tool_name,
                         "denied_tool_not_allowed",
                         f"tool {tool_name} not in profile allowed_tools",
                         profile=profile)
            return {"ok": False, "decision": "denied_tool_not_allowed",
                    "reason": f"tool {tool_name} not allowed for this server"}

        manifest = self._registry.get_manifest(profile_id)
        tool_spec = self._manifest_tool(manifest, tool_name)
        if manifest is not None and tool_spec is None:
            self._record(now, profile_id, profile.server_name, tool_name,
                         "denied_tool_not_discovered", "tool is not in the discovered manifest",
                         profile=profile)
            return {"ok": False, "decision": "denied_tool_not_discovered",
                    "reason": "tool is not in the discovered manifest"}
        if tool_spec is not None:
            schema_error = validate_json_schema(arguments, tool_spec.get("inputSchema", {}))
            if schema_error:
                self._record(now, profile_id, profile.server_name, tool_name,
                             "denied_invalid_request", schema_error, profile=profile)
                return {"ok": False, "decision": "denied_invalid_request", "reason": schema_error}

        # 5. 写操作审批（plan §10.5：写操作仍弹 Topos 审批）
        # AUD-22020：审批判定必须基于**最终**工具注解。manifest 未缓存时先按
        # 启发式做保守预闸（名字启发式命中的写在 discovery 前即拒绝，被拒的
        # 写绝不触发远程请求）；补发现后基于 manifest 里的最终注解重算
        # is_write 并重走闸门——注解拿不到（发现失败）时无法确定，按写处理
        # （fail-closed），后续审计的 write_operation 也用该最终值。
        def _deny_write() -> dict[str, Any]:
            self._record(now, profile_id, profile.server_name, tool_name,
                         "denied_write_needs_approval",
                         "write operation requires Topos approval",
                         profile=profile, write=True)
            return {"ok": False, "decision": "denied_write_needs_approval",
                    "reason": "write operation requires Topos approval"}

        is_write = self._is_write_operation(profile, tool_name, tool_spec)
        if is_write and profile.requires_approval_for_writes and not write_approved:
            return _deny_write()

        if manifest is None:
            # Discovery happens only after the local authorization gate.  A
            # heuristic-denied write must never cause a remote process or HTTP
            # request; the authoritative annotation gate below runs after
            # discovery and still denies BEFORE the tools/call itself.
            self.discover(profile_id)
            manifest = self._registry.get_manifest(profile_id)
            if manifest is None:
                # AUD-22021：discovery 失败 → fail-closed。没有清单就无法确认
                # 工具存在、无法执行 inputSchema 校验（外部数据须验证红线），
                # 与上面"manifest 存在时严格拒绝未发现工具"的语义保持一致，
                # 绝不把未校验的调用发给外部 server。
                self._record(now, profile_id, profile.server_name, tool_name,
                             "error", "tool discovery failed; unvalidated tool call refused",
                             profile=profile, write=True)
                return {"ok": False, "decision": "error_discovery_failed",
                        "reason": "tool discovery failed for server; "
                                  "cannot validate tool existence or schema"}
            tool_spec = self._manifest_tool(manifest, tool_name)
            if manifest is not None and tool_spec is None:
                self._record(now, profile_id, profile.server_name, tool_name,
                             "denied_tool_not_discovered", "tool is not in the discovered manifest",
                             profile=profile, write=is_write)
                return {"ok": False, "decision": "denied_tool_not_discovered",
                        "reason": "tool is not in the discovered manifest"}
            if tool_spec is not None:
                schema_error = validate_json_schema(arguments, tool_spec.get("inputSchema", {}))
                if schema_error:
                    self._record(now, profile_id, profile.server_name, tool_name,
                                 "denied_invalid_request", schema_error,
                                 profile=profile, write=is_write)
                    return {"ok": False, "decision": "denied_invalid_request", "reason": schema_error}
            # AUD-22020：拿到完整 manifest 后基于最终工具注解重算 is_write；
            # manifest/工具 spec 拿不到（发现失败）→ 无法确定 → 按写处理
            # （fail-closed），杜绝用 discovery 前的启发式缓存值绕过审批闸门。
            is_write = (
                self._is_write_operation(profile, tool_name, tool_spec)
                if tool_spec is not None
                else True
            )
            if is_write and profile.requires_approval_for_writes and not write_approved:
                return _deny_write()

        # 6. 经 transport 发请求
        transport = self._get_transport(profile)
        if transport is None:
            self._record(now, profile_id, profile.server_name, tool_name,
                         "error", "no transport available", profile=profile)
            return {"ok": False, "decision": "error",
                    "reason": "no transport available for server"}

        limiter = self._rate_limiters.setdefault(
            profile_id, SlidingWindowRateLimiter(self._rate_limit_per_minute),
        )
        if not limiter.allow():
            self._record(now, profile_id, profile.server_name, tool_name,
                         "denied_rate_limited", "server tool call rate limit exceeded",
                         profile=profile, write=is_write)
            return {"ok": False, "decision": "denied_rate_limited",
                    "reason": "server tool call rate limit exceeded"}

        try:
            request = {
                "jsonrpc": "2.0", "id": self._next_request_id("topos", now),
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
            response = transport.send_request(request)
        except Exception as exc:  # noqa: BLE001 — 外部 server 不可信，异常归审计
            self._record(now, profile_id, profile.server_name, tool_name,
                         "error", f"transport error: {exc}", profile=profile)
            return {"ok": False, "decision": "error",
                    "reason": f"transport error: {exc}"}

        if not isinstance(response, dict):
            self._record(now, profile_id, profile.server_name, tool_name,
                         "error", "MCP response must be an object", profile=profile, write=is_write)
            return {"ok": False, "decision": "error", "reason": "MCP response must be an object"}
        if isinstance(response.get("error"), dict):
            error = response["error"]
            reason = f"remote MCP error {error.get('code')}: {error.get('message', 'unknown error')}"
            self._record(now, profile_id, profile.server_name, tool_name,
                         "error", reason, profile=profile, write=is_write)
            return {"ok": False, "decision": "error", "reason": reason, "result": response}
        result = response.get("result")
        if not isinstance(result, dict):
            self._record(now, profile_id, profile.server_name, tool_name,
                         "error", "MCP response result must be an object", profile=profile, write=is_write)
            return {"ok": False, "decision": "error", "reason": "MCP response result must be an object"}
        result_error = validate_tool_result(result)
        if result_error:
            self._record(now, profile_id, profile.server_name, tool_name,
                         "error", result_error, profile=profile, write=is_write)
            return {"ok": False, "decision": "error", "reason": result_error}
        output_schema = tool_spec.get("outputSchema") if isinstance(tool_spec, dict) else None
        if output_schema is not None and "structuredContent" in result:
            schema_error = validate_json_schema(result["structuredContent"], output_schema)
            if schema_error:
                self._record(now, profile_id, profile.server_name, tool_name,
                             "error", schema_error, profile=profile, write=is_write)
                return {"ok": False, "decision": "error", "reason": schema_error}

        # 7. 记授权通过
        self._record(now, profile_id, profile.server_name, tool_name,
                     "allowed", "", profile=profile, write=is_write)
        # 外部输出只作数据返回（红线：不作命令执行）
        return {"ok": True, "decision": "allowed", "result": response}

    # ---------------- discovery（只读，不执行工具）---------------- #

    def discover(
        self, profile_id: str,
    ) -> McpServerManifest | None:
        """只读拉取 server 的 tools/list（不执行工具；用于授权卡展示）。"""
        profile = self._registry.get(profile_id)
        if profile is None:
            return None
        if self._registry.is_blocked(profile_id) or not self._registry.is_session_enabled(profile_id):
            return None
        cached = self._registry.get_manifest(profile_id)
        if cached is not None:
            return cached
        transport = self._get_transport(profile)
        if transport is None:
            return None
        try:
            tools_list: list[dict[str, Any]] = []
            result: dict[str, Any] = {}
            cursor = ""
            # A malformed or hostile server cannot make discovery loop
            # forever; valid catalogs are normally a single page.
            for _page in range(20):
                params = {"cursor": cursor} if cursor else {}
                resp = transport.send_request({
                    "jsonrpc": "2.0",
                    "id": self._next_request_id("topos-disc", time.time()),
                    "method": "tools/list", "params": params,
                })
                page_result = resp.get("result")
                if not isinstance(page_result, dict):
                    return None
                if "tools" not in page_result:
                    # This is a tool-call result from a legacy/fake transport,
                    # not a valid tools/list discovery response.
                    return None
                raw_tools = page_result.get("tools", [])
                if not isinstance(raw_tools, list):
                    return None
                result = page_result
                for tool in raw_tools:
                    error = validate_tool_definition(tool)
                    if error:
                        _LOG.warning("忽略不安全 MCP tool definition: %s", error)
                        continue
                    tools_list.append(dict(tool))
                next_cursor = str(page_result.get("nextCursor", "") or "")
                if not next_cursor or next_cursor == cursor:
                    cursor = ""
                    break
                cursor = next_cursor
            else:
                return None
            tools = tuple(tools_list)
            manifest = McpServerManifest(
                server_name=profile.server_name,
                server_version=str(result.get("serverInfo", {}).get("version", "")),
                protocol_version=str(result.get("protocolVersion", "")),
                tools=tools,
                capabilities=dict(result.get("capabilities", {}) or {}),
                next_cursor=cursor,
                ttl_ms=int(result["ttlMs"]) if isinstance(result.get("ttlMs"), int) else None,
                cache_scope=str(result.get("cacheScope", "") or ""),
            )
            self._registry.cache_manifest(profile_id, manifest)
            return manifest
        except Exception:  # noqa: BLE001
            return None

    # ---------------- 内部 ---------------- #

    def _is_write_operation(
        self, profile: McpConnectionProfile, tool_name: str,
        tool_spec: dict[str, Any] | None = None,
    ) -> bool:
        """判断是否写操作（data_scope 非 read_only 或工具名暗示写）。"""
        if not profile.is_read_only_scope:
            return True
        annotations = tool_spec.get("annotations") if isinstance(tool_spec, dict) else None
        if isinstance(annotations, dict) and annotations.get("readOnlyHint") is False:
            return True
        if isinstance(annotations, dict) and annotations.get("destructiveHint") is True:
            return True
        write_hints = ("write", "create", "update", "delete", "insert", "apply",
                       "commit", "set", "add", "remove", "export")
        name_lower = tool_name.lower()
        return any(h in name_lower for h in write_hints)

    def _get_transport(self, profile: McpConnectionProfile) -> StdioTransport | None:
        # AUD-22022：check-then-act 竞态修复——先锁内查缓存；工厂调用在锁外
        # （不可信外部代码，可能慢或抛错）；创建结果以 setdefault 收敛，
        # 并发首调时两个线程复用同一个 transport，不产生双连接。
        with self._lock:
            cached = self._transports.get(profile.profile_id)
        if cached is not None:
            return cached
        if self._transport_factory is None:
            return None
        try:
            t = self._transport_factory(profile)
        except Exception:  # noqa: BLE001 — 外部工厂不可信，异常归调用方语义
            return None
        if t is None:
            return None
        with self._lock:
            return self._transports.setdefault(profile.profile_id, t)

    def _record(
        self, now: float, profile_id: str, server_name: str, tool_name: str,
        decision: str, reason: str, *,
        profile: McpConnectionProfile | None = None, write: bool = False,
    ) -> None:
        self._auth_log.record(McpAuthorizationEvent(
            timestamp=now, profile_id=profile_id, server_name=server_name,
            tool_name=tool_name, decision=decision, reason=reason,
            data_scope=profile.data_scope if profile else "",
            write_operation=write,
        ))

    def close(self) -> None:
        # 锁内快照、锁外关闭（外部 transport 的 close 不可信/可能慢）。
        with self._lock:
            transports = list(self._transports.values())
            self._transports.clear()
        for t in transports:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
        self._rate_limiters.clear()

    @staticmethod
    def _manifest_tool(
        manifest: McpServerManifest | None, tool_name: str,
    ) -> dict[str, Any] | None:
        if manifest is None:
            return None
        return next((tool for tool in manifest.tools if tool.get("name") == tool_name), None)


__all__ = [
    "McpAuthorizationEvent",
    "McpAuthorizationLog",
    "McpConnectionProfile",
    "McpOutboundClient",
    "McpServerManifest",
    "McpServerRegistry",
    "StdioTransport",
    "TransportKind",
]
