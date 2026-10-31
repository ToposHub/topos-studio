"""MCP connector lifecycle bridge for the plugin kernel.

``McpServer`` remains a thin JSON-RPC transport adapter.  This module only
adds the plugin boundary around it: identity, endpoint metadata, source,
TTL, data policy, health and activation.  It deliberately does not copy
tool permissions or approval logic; calls still go through ``McpServer`` and
then ``ToolFacade``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.features.ai_harness.plugin_runtime import (
    PLUGIN_API_VERSION,
    PluginCapability,
    PluginContext,
    PluginExecutionPolicy,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    PluginSource,
)
from src.features.edit.ai_editing.mcp_gateway import McpClientPolicy, McpServer, McpSession


_DATA_POLICIES = frozenset({"local", "cloud", "hybrid"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpPluginDescriptor:
    """Non-secret metadata required to expose one MCP connector."""

    plugin_id: str
    display_name: str
    endpoint: str = ""
    tool_names: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    ttl_seconds: int = 30
    source: PluginSource = PluginSource.LOCAL_DEV
    data_policy: str = "local"
    network_domains: tuple[str, ...] = ()
    version: str = "1.0.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        plugin_id = str(self.plugin_id or "").strip()
        display_name = str(self.display_name or "").strip()
        if not plugin_id or not display_name:
            raise ValueError("MCP plugin_id and display_name are required")
        if isinstance(self.ttl_seconds, bool) or not isinstance(self.ttl_seconds, int) or self.ttl_seconds <= 0:
            raise ValueError("MCP ttl_seconds must be a positive integer")
        data_policy = str(self.data_policy or "").strip().lower()
        if data_policy not in _DATA_POLICIES:
            raise ValueError(f"unsupported MCP data policy: {data_policy!r}")
        source = self.source if isinstance(self.source, PluginSource) else PluginSource(str(self.source))
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "data_policy", data_policy)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "tool_names", tuple(dict.fromkeys(
            str(name).strip() for name in self.tool_names if str(name).strip()
        )))
        object.__setattr__(self, "capabilities", tuple(dict.fromkeys(
            str(name).strip() for name in self.capabilities if str(name).strip()
        )))
        object.__setattr__(self, "network_domains", tuple(dict.fromkeys(
            str(domain).strip().lower() for domain in self.network_domains if str(domain).strip()
        )))

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "display_name": self.display_name[:200],
            "endpoint": self.endpoint[:500],
            "tool_names": list(self.tool_names),
            "capabilities": list(self.capabilities),
            "ttl_seconds": self.ttl_seconds,
            "source": self.source.value,
            "data_policy": self.data_policy,
            "network_domains": list(self.network_domains),
            "version": self.version,
            "metadata": dict(self.metadata),
        }


class _McpPluginAdapter:
    def __init__(self, server: McpServer) -> None:
        self.server = server
        self._active = False

    def activate(self, context: PluginContext) -> None:
        self._active = True

    def deactivate(self) -> None:
        self._active = False

    def health(self) -> PluginHealth:
        return PluginHealth(
            status="healthy" if self._active else "inactive",
            message="MCP transport ready" if self._active else "MCP plugin is inactive",
        )


class McpPluginHost:
    """Lifecycle-gate MCP servers without changing their transport semantics."""

    def __init__(self, *, plugin_registry: PluginRegistry | None = None) -> None:
        self.plugin_registry = plugin_registry or PluginRegistry()
        self._servers: dict[str, tuple[McpPluginDescriptor, McpServer]] = {}
        # AUD-13830：_servers 写入（register）与迭代读（snapshot）并发时可能
        # 抛 "dictionary changed size during iteration"；一把锁覆盖读写，
        # 并提供 remove() 清理路径（此前只增不删）。
        self._lock = threading.Lock()

    def register(
        self,
        server: McpServer,
        descriptor: McpPluginDescriptor,
        *,
        policy: PluginExecutionPolicy | None = None,
        enabled: bool = True,
    ) -> bool:
        capability = PluginCapability(
            capability_id=f"mcp.{descriptor.plugin_id}",
            description=descriptor.display_name,
            tool_names=descriptor.tool_names,
            permission="read",
            metadata={
                "mcp": descriptor.to_dict(),
                "requires_consent": descriptor.data_policy in {"cloud", "hybrid"},
            },
        )
        manifest = PluginManifest(
            plugin_id=descriptor.plugin_id,
            display_name=descriptor.display_name,
            version=descriptor.version,
            api_version=PLUGIN_API_VERSION,
            source=descriptor.source,
            description="MCP connector transport adapter",
            capabilities=(capability,),
            metadata={"mcp": descriptor.to_dict()},
        )
        accepted = self.plugin_registry.register(
            manifest,
            _McpPluginAdapter(server),
            enabled=enabled,
            policy=policy,
        )
        if accepted:
            with self._lock:
                self._servers[descriptor.plugin_id] = (descriptor, server)
        return accepted

    def remove(self, plugin_id: str) -> bool:
        """下线并移除一个 MCP server 绑定（AUD-13830 清理路径）。

        先从 ``_servers`` 摘除绑定，再尝试 registry 停用（失败不影响移除结果）。
        返回是否确实移除了一个绑定。
        """
        plugin_id = str(plugin_id)
        with self._lock:
            binding = self._servers.pop(plugin_id, None)
        if binding is None:
            return False
        try:
            self.plugin_registry.deactivate(plugin_id)
        except Exception as exc:  # noqa: BLE001 — registry 停用失败不阻止绑定移除
            logger.warning(
                "mcp plugin registry deactivate failed for %r: %s: %s",
                plugin_id, type(exc).__name__, exc,
            )
        return True

    def activate(self, plugin_id: str) -> bool:
        return self.plugin_registry.activate(str(plugin_id))

    def deactivate(self, plugin_id: str) -> bool:
        return self.plugin_registry.deactivate(str(plugin_id))

    def server(self, plugin_id: str) -> McpServer | None:
        plugin_id = str(plugin_id)
        record = self.plugin_registry.get(plugin_id)
        if record is None or record.state.value != "active":
            return None
        with self._lock:
            binding = self._servers.get(plugin_id)
        return binding[1] if binding is not None else None

    def open_session(
        self,
        plugin_id: str,
        policy: McpClientPolicy,
        *,
        active_project_id: str = "",
        active_profile_id: str = "",
        feature_flags: dict[str, bool] | None = None,
        approval_grant: Any = None,
    ) -> McpSession | None:
        server = self.server(plugin_id)
        if server is None:
            return None
        return server.open_session(
            policy,
            active_project_id=active_project_id,
            active_profile_id=active_profile_id,
            feature_flags=feature_flags,
            approval_grant=approval_grant,
        )

    def handle_request(
        self,
        plugin_id: str,
        session: McpSession,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        server = self.server(plugin_id)
        if server is None:
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {
                    "code": -32001,
                    "message": "MCP plugin is not active",
                },
            }
        return server.handle_request(session, request)

    def descriptor(self, plugin_id: str) -> McpPluginDescriptor | None:
        with self._lock:
            binding = self._servers.get(str(plugin_id))
        return binding[0] if binding is not None else None

    def snapshot(self) -> dict[str, Any]:
        # AUD-13830：锁内取快照、锁外序列化，迭代不受并发 register 影响。
        with self._lock:
            bindings = list(self._servers.values())
        return {
            "schema_version": "topos-mcp-plugin-host/1.0",
            "connectors": [descriptor.to_dict() for descriptor, _ in bindings],
            "plugin_registry": self.plugin_registry.snapshot(),
        }


__all__ = ["McpPluginDescriptor", "McpPluginHost"]
