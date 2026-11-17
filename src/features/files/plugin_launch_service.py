"""Files plugin command service over the legacy catalog + HTTP transport.

First real adapter of the shared ``PluginCommandService`` contract (PL-07):
every Files entry point (toolbar, launcher, center, shortcuts) goes through
``prepare`` → ``revalidate`` → ``dispatch`` with the selection frozen at the
gesture.

Honesty notes kept from the review plan (PR-05):
- dispatch success means the transport queued an HTTP request; only ``finish``
  records its HTTP acknowledgement. Neither phase proves a window appeared
  or a job completed
- ``revalidate`` is the authoritative pre-dispatch recheck (plugin still
  launchable for the frozen selection). The controller runs it in its worker
  so the GUI thread never compensates with a synchronous scan; calling
  ``dispatch`` directly without ``revalidate`` re-runs the check inside the
  service — no entry point bypasses the authority
- the four launchable builtin tools are the only commands; other legacy tools
  stay behind explicit compatibility entries instead of fake routes
"""
from __future__ import annotations

from dataclasses import replace
from threading import Lock
from typing import Any, Iterable, Protocol
from weakref import WeakValueDictionary

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
    def launch_plugin(self, plugin_id: str, endpoint: str,
                      payload: dict[str, Any]) -> bool: ...


class LegacyFilesCommandService:
    """Implements ``PluginCommandService`` for the legacy Files host."""

    def __init__(self, catalog: Any, transport: _TransportLike) -> None:
        self.catalog = catalog
        self.transport = transport
        # A caller-controlled DTO flag is not proof that this service checked it.
        # Weak ownership also releases checks abandoned when a window closes.
        self._checked: WeakValueDictionary[str, PreparedInvocation] = WeakValueDictionary()
        self._checked_lock = Lock()

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

    def revalidate(self, prepared: PreparedInvocation) -> PreparedInvocation:
        """执行前权威复核（PR-05）：插件仍可启动且接口未变才放行提交。

        与手势使用同一权威（catalog.launch_request）；prepare 后停用、
        接口变化在这里以 CommandRejected 拒绝。复核应在 worker 线程调用，
        dispatch 只剩 Qt 线程安全的提交，宿主不在 GUI 线程补同步扫描。
        """
        legacy_id = prepared.plugin_id.partition(".")[2] if prepared.plugin_id.startswith("files.") else ""
        if (legacy_id not in LAUNCHABLE_LEGACY_IDS
                or prepared.command_id != f"files.{legacy_id}.launch"
                or prepared.context.plugin_id != prepared.plugin_id
                or prepared.context.command_id != prepared.command_id):
            raise CommandRejected("INVALID_INVOCATION", "插件调用身份与命令不匹配。")
        if prepared.context.cancelled:
            raise CommandRejected("INVOCATION_CANCELLED", "插件调用已取消。")
        if prepared.context.timed_out():
            raise CommandRejected("INVOCATION_EXPIRED", "插件调用已过期。")
        paths = list(prepared.context.entry_paths)
        try:
            endpoint, payload = self.catalog.launch_request(legacy_id, paths)
        except ValueError as exc:
            raise CommandRejected("COMMAND_NOT_APPLICABLE", str(exc)) from exc
        if endpoint != f"/api/plugin/{legacy_id}/launch" or payload != prepared.to_payload():
            raise CommandRejected("INVOCATION_STALE", "插件状态或启动接口已变化，请重新发起。")
        checked = replace(prepared, revalidated=True)
        with self._checked_lock:
            self._checked[checked.context.invocation_id] = checked
        return checked

    def dispatch(self, prepared: PreparedInvocation) -> ServiceResult:
        """Send the frozen request; completion arrives via the transport signal."""
        legacy_id = prepared.plugin_id.partition(".")[2] if prepared.plugin_id.startswith("files.") else ""
        if (legacy_id not in LAUNCHABLE_LEGACY_IDS or prepared.command_id != f"files.{legacy_id}.launch"
                or prepared.context.plugin_id != prepared.plugin_id or prepared.context.command_id != prepared.command_id):
            return rejected(prepared.command_id, error_code="INVALID_INVOCATION", message="插件调用身份与命令不匹配。",
                            invocation_id=prepared.context.invocation_id)
        if prepared.context.cancelled:
            return rejected(prepared.command_id, error_code="INVOCATION_CANCELLED", message="插件调用已取消。",
                            invocation_id=prepared.context.invocation_id)
        if prepared.context.timed_out():
            return rejected(prepared.command_id, error_code="INVOCATION_EXPIRED", message="插件调用已过期。",
                            invocation_id=prepared.context.invocation_id)
        with self._checked_lock:
            checked_here = self._checked.get(prepared.context.invocation_id) is prepared
        if not checked_here:
            # 直接调用 dispatch 的入口不能绕过权威复核：服务内补做
            try:
                prepared = self.revalidate(prepared)
            except CommandRejected as exc:
                return rejected(prepared.command_id, error_code=exc.error_code, message=exc.message,
                                retriable=exc.retriable, invocation_id=prepared.context.invocation_id)
        with self._checked_lock:
            self._checked.pop(prepared.context.invocation_id, None)
        endpoint = f"/api/plugin/{legacy_id}/launch"
        try:
            submitted = self.transport.launch_plugin(legacy_id, endpoint, prepared.to_payload())
        except (OSError, ValueError, RuntimeError) as exc:
            return rejected(prepared.command_id, error_code="LAUNCH_REJECTED",
                            message=f"插件启动请求未能提交：{exc}", retriable=True,
                            invocation_id=prepared.context.invocation_id)
        if submitted is False:
            # transport 同步拒绝（服务未就绪/插件不受支持/通道已关闭）不算受理
            return rejected(prepared.command_id, error_code="LAUNCH_REJECTED",
                            message="插件启动请求被同步拒绝，请检查插件状态或稍后重试。",
                            retriable=True, invocation_id=prepared.context.invocation_id)
        return accepted(prepared.command_id, invocation_id=prepared.context.invocation_id,
                        stage="queued", message="已发出打开请求，等待插件服务确认；工具窗口由插件创建。")

    def finish(self, prepared: PreparedInvocation, success: bool, message: str) -> ServiceResult:
        """Translate the transport's async acknowledgement into a ServiceResult."""
        if success:
            return accepted(prepared.command_id, invocation_id=prepared.context.invocation_id,
                            stage="dispatched", message=message)
        return rejected(prepared.command_id, error_code="LAUNCH_NOT_ACCEPTED", message=message,
                        invocation_id=prepared.context.invocation_id)


__all__ = ["LegacyFilesCommandService", "LAUNCHABLE_LEGACY_IDS"]
