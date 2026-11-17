"""Files plugin command service over the legacy catalog + HTTP transport.

First real adapter of the shared ``PluginCommandService`` contract (PL-07):
every Files entry point (toolbar, launcher, center, shortcuts) goes through
``prepare`` → ``dispatch`` with the selection frozen at the gesture.

Honesty notes kept from the review plan:
- dispatch success only means the legacy service accepted the HTTP request;
  it is not proof a tool window appeared (UI copy already says so)
- the four launchable builtin tools are the only commands; other legacy tools
  stay behind explicit compatibility entries instead of fake routes
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Protocol

from src.shared.plugins.lifecycle import InvocationContext, make_invocation
from src.shared.plugins.services import (
    CommandRejected,
    PreparedInvocation,
    ServiceResult,
    accepted,
    rejected,
)

# Legacy ids that own a real HTTP launch route (verified in this repo).
LAUNCHABLE_LEGACY_IDS = frozenset({"format_factory", "image_factory", "shutdown_timer", "decode_benchmark"})


class _TransportLike(Protocol):
    def launch_plugin(self, plugin_id: str, endpoint: str, payload: dict[str, Any]) -> None: ...


class LegacyFilesCommandService:
    """Implements ``PluginCommandService`` for the legacy Files host."""

    def __init__(self, catalog: Any, transport: _TransportLike) -> None:
        self.catalog = catalog
        self.transport = transport

    # -- introspection ----------------------------------------------------
    def command_ids(self) -> tuple[str, ...]:
        """Legacy ids that currently own a launchable command."""
        return tuple(sorted(
            info.plugin_id for info in self.catalog.snapshot()
            if info.plugin_id in LAUNCHABLE_LEGACY_IDS and info.can_launch))

    # -- PluginCommandService ---------------------------------------------
    def prepare(self, command_id: str, entry_paths: Iterable[str]) -> PreparedInvocation:
        if command_id not in LAUNCHABLE_LEGACY_IDS:
            raise CommandRejected("COMMAND_NOT_APPLICABLE", f"此插件没有受支持的原生启动接口：{command_id}")
        if isinstance(entry_paths, (str, bytes)):
            raise CommandRejected("INVALID_SELECTION", "所选路径必须是字符串列表")
        raw = list(entry_paths)
        if any(not isinstance(path, str) for path in raw):
            raise CommandRejected("INVALID_SELECTION", "所选路径必须是字符串列表")
        paths = [path for path in raw if path.strip()]
        # catalog.launch_request performs the same single/multi/none rules the
        # legacy UI applied; freezing happens here, at the user's gesture.
        try:
            endpoint, payload = self.catalog.launch_request(command_id, paths)
        except ValueError as exc:
            raise CommandRejected("COMMAND_NOT_APPLICABLE", str(exc)) from exc
        context: InvocationContext = make_invocation(
            command_id=f"files.{command_id}.launch", plugin_id=f"files.{command_id}",
            entry_paths=tuple(paths),
        )
        return PreparedInvocation(context=context, command_id=context.command_id,
                                  plugin_id=context.plugin_id, payload=payload)

    def dispatch(self, prepared: PreparedInvocation) -> ServiceResult:
        """Send the frozen request; completion arrives via the transport signal."""
        legacy_id = prepared.plugin_id.partition(".")[2] if prepared.plugin_id.startswith("files.") else ""
        if (legacy_id not in LAUNCHABLE_LEGACY_IDS or prepared.command_id != f"files.{legacy_id}.launch"
                or prepared.context.plugin_id != prepared.plugin_id or prepared.context.command_id != prepared.command_id):
            return rejected(prepared.command_id, error_code="INVALID_INVOCATION", message="插件调用身份与命令不匹配。")
        if prepared.context.cancelled:
            return rejected(prepared.command_id, error_code="INVOCATION_CANCELLED", message="插件调用已取消。")
        if prepared.context.timed_out():
            return rejected(prepared.command_id, error_code="INVOCATION_EXPIRED", message="插件调用已过期。")
        endpoint = f"/api/plugin/{legacy_id}/launch"
        self.transport.launch_plugin(legacy_id, endpoint, prepared.to_payload())
        return accepted(prepared.command_id, invocation_id=prepared.context.invocation_id,
                        stage="dispatched", message="已提交打开请求；工具窗口由插件创建。")

    def finish(self, prepared: PreparedInvocation, success: bool, message: str) -> ServiceResult:
        """Translate the transport's async acknowledgement into a ServiceResult."""
        if success:
            return accepted(prepared.command_id, invocation_id=prepared.context.invocation_id,
                            stage="completed", message=message)
        return rejected(prepared.command_id, error_code="LAUNCH_NOT_ACCEPTED", message=message)


__all__ = ["LegacyFilesCommandService", "LAUNCHABLE_LEGACY_IDS"]
