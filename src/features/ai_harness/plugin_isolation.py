"""Optional process boundary for untrusted or resource-heavy plugins.

The normal plugin path remains in-process for low latency.  This adapter is an
explicit opt-in boundary for handlers that need a hard timeout/cancel stop:
the child owns the plugin instance, the host exchanges bounded RPC messages,
and a non-cooperative call is terminated instead of being described as safely
cancelled while its Python code keeps running.

The factory must be importable/picklable.  A process-isolated plugin receives a
default-deny resource broker in the child; host-side grants and callbacks are
never implicitly serialized into the child address space.
"""

from __future__ import annotations

import inspect
import multiprocessing as mp
import pickle
import threading
import time
from collections.abc import Mapping
from multiprocessing.connection import Connection
from typing import Any, Callable

from src.features.ai_harness.plugin_broker import (
    BrokerDecision,
    DefaultDenyResourceBroker,
    HostIOResult,
    PluginHostIO,
)
from src.features.ai_harness.plugin_runtime import (
    PluginCallContext,
    PluginContext,
    PluginExecutionPolicy,
    PluginHealth,
    PluginInvocationCancelled,
    PluginInvocationProcessCrashed,
    PluginInvocationTimedOut,
)
from src.features.ai_harness.plugin_sandbox import (
    PluginSandboxLauncher,
    PluginSandboxReport,
    validate_sandbox_launcher,
)


PluginFactory = Callable[[], Any]
_PROTOCOL_VERSION = 1
# AUD-22105：invoke 等待循环的进程级默认看门狗上限。外部 deadline
# （context.deadline）与 policy.timeout_seconds 都可能为 None（默认策略），
# 且 context 可为 None（无取消源）——子进程活着但不回包时，等待循环若没有
# 本地看门狗就会永久自旋并占住 self._lock。宿主可经构造参数覆盖/关闭。
_DEFAULT_INVOKE_TIMEOUT_SECONDS = 600.0


def _send(connection: Connection, message: Mapping[str, Any]) -> None:
    connection.send(dict(message))


def _accepts_context(handler: Any) -> bool:
    try:
        parameters = inspect.signature(handler).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "context"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


class _ProcessRPC:
    """Small synchronous child-to-host RPC helper used by host capabilities."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._remaining_seconds: float | None = None

    def set_remaining_seconds(self, value: float | None) -> None:
        self._remaining_seconds = value

    def request(self, operation: str, **payload: Any) -> dict[str, Any]:
        _send(self._connection, {
            "version": _PROTOCOL_VERSION,
            "type": "io_request",
            "operation": str(operation),
            "remaining_seconds": self._remaining_seconds,
            **payload,
        })
        while True:
            if not self._connection.poll(0.02):
                continue
            message = self._connection.recv()
            if not isinstance(message, Mapping):
                raise RuntimeError("host capability returned an invalid response")
            if int(message.get("version", -1)) != _PROTOCOL_VERSION:
                raise RuntimeError("host capability protocol version mismatch")
            if str(message.get("type", "")) == "event":
                # Lifecycle events are best-effort; the host will see them
                # when it continues polling the main invocation stream.
                continue
            if str(message.get("type", "")) != "io_response":
                raise RuntimeError("host capability returned an unexpected response")
            return dict(message)


class _ProcessResourceBroker:
    """Child proxy for host-side allowlist decisions."""

    def __init__(self, rpc: _ProcessRPC, plugin_id: str) -> None:
        self._rpc = rpc
        self._plugin_id = str(plugin_id)

    def check_path(self, plugin_id: str, path: str, *, mode: str = "read", policy: Any = None) -> BrokerDecision:
        response = self._rpc.request(
            "check_path", plugin_id=self._plugin_id, path=str(path), mode=str(mode),
        )
        raw = response.get("decision", {})
        return BrokerDecision(
            bool(raw.get("allowed", False)),
            str(raw.get("code", "PLUGIN_PATH_DENIED")),
            str(raw.get("message", "path access denied"))[:500],
            str(raw.get("target", path)),
        )

    def check_network(self, plugin_id: str, url: str, *, policy: Any = None) -> BrokerDecision:
        response = self._rpc.request(
            "check_network", plugin_id=self._plugin_id, url=str(url),
        )
        raw = response.get("decision", {})
        return BrokerDecision(
            bool(raw.get("allowed", False)),
            str(raw.get("code", "PLUGIN_NETWORK_DENIED")),
            str(raw.get("message", "network access denied"))[:500],
            str(raw.get("target", url)),
        )


class _ProcessHostIO:
    """Child proxy for the explicit host I/O capability."""

    def __init__(self, connection: Connection, plugin_id: str) -> None:
        self._rpc = _ProcessRPC(connection)
        self._resource_broker = _ProcessResourceBroker(self._rpc, plugin_id)

    def set_remaining_seconds(self, value: float | None) -> None:
        self._rpc.set_remaining_seconds(value)

    def check_path(self, path: str, *, mode: str = "read") -> BrokerDecision:
        return self._resource_broker.check_path("", path, mode=mode)

    def check_network(self, url: str) -> BrokerDecision:
        return self._resource_broker.check_network("", url)

    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> HostIOResult:
        response = self._rpc.request("read_bytes", path=str(path), max_bytes=max_bytes)
        return _host_io_result(response)

    def write_bytes(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool = False,
        max_bytes: int | None = None,
    ) -> HostIOResult:
        response = self._rpc.request(
            "write_bytes", path=str(path), data=bytes(data),
            overwrite=bool(overwrite), max_bytes=max_bytes,
        )
        return _host_io_result(response)

    def request_bytes(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        timeout_seconds: float | None = None,
    ) -> HostIOResult:
        response = self._rpc.request(
            "request_bytes", url=str(url), method=str(method), body=bytes(body),
            headers=dict(headers or {}), max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
        return _host_io_result(response)


def _host_io_result(response: Mapping[str, Any]) -> HostIOResult:
    raw_data = response.get("data", b"")
    return HostIOResult(
        bool(response.get("ok", False)),
        str(response.get("code", "PLUGIN_IO_FAILED")),
        str(response.get("message", "host I/O failed"))[:500],
        bytes(raw_data) if isinstance(raw_data, (bytes, bytearray)) else b"",
        int(response.get("bytes_count", 0) or 0),
        int(response.get("status_code", 0) or 0),
        str(response.get("content_type", ""))[:200],
    )


def _child_emit(connection: Connection, kind: str, payload: Mapping[str, Any]) -> None:
    try:
        _send(connection, {
            "version": _PROTOCOL_VERSION,
            "type": "event",
            "kind": str(kind),
            "payload": dict(payload or {}),
        })
    except (BrokenPipeError, EOFError, OSError):
        # The host may have terminated the process while a plugin was
        # emitting diagnostics.  There is no safe recovery path in the child.
        return


def _plugin_worker(
    connection: Connection,
    factory: PluginFactory,
    plugin_id: str,
    policy: PluginExecutionPolicy,
) -> None:
    """Spawn target; deliberately defined at module scope for ``spawn``."""

    try:
        plugin = factory()
        activate = getattr(plugin, "activate", None)
        invoke = getattr(plugin, "invoke", None)
        health = getattr(plugin, "health", None)
        if not callable(activate) or not callable(invoke) or not callable(health):
            raise TypeError("isolated plugin must implement activate/invoke/health")
        host_io = _ProcessHostIO(connection, plugin_id)
        activate(PluginContext(
            plugin_id=plugin_id,
            emit=lambda kind, payload: _child_emit(connection, kind, payload),
            policy=policy,
            resource_broker=_ProcessResourceBroker(host_io._rpc, plugin_id),
            host_io=host_io,
        ))
        plugin_health = health()
        if not isinstance(plugin_health, PluginHealth):
            raise TypeError("plugin.health() must return PluginHealth")
        _send(connection, {
            "version": _PROTOCOL_VERSION,
            "type": "ready",
            "health": plugin_health.to_dict(),
        })

        while True:
            message = connection.recv()
            if not isinstance(message, Mapping):
                raise TypeError("isolated plugin request must be an object")
            operation = str(message.get("operation", ""))
            if operation == "deactivate":
                getattr(plugin, "deactivate")()
                _send(connection, {
                    "version": _PROTOCOL_VERSION,
                    "type": "deactivated",
                })
                return
            if operation != "invoke":
                raise ValueError("unknown isolated plugin operation")

            invocation_id = str(message.get("invocation_id", ""))
            started_at = time.monotonic()
            remaining = message.get("remaining_seconds")
            try:
                remaining_seconds = (
                    None if remaining is None else max(0.0, float(remaining))
                )
            except (TypeError, ValueError):
                remaining_seconds = None
            call_context = PluginCallContext(
                invocation_id=invocation_id,
                plugin_id=plugin_id,
                tool_name=str(message.get("tool_name", "")),
                run_id=str(message.get("run_id", "")),
                started_at=started_at,
                deadline=(
                    None
                    if remaining_seconds is None
                    else time.monotonic() + remaining_seconds
                ),
                host_io=host_io,
            )
            host_io.set_remaining_seconds(remaining_seconds)
            try:
                args = message.get("args", {})
                approval = message.get("approval")
                if _accepts_context(invoke):
                    result = invoke(
                        call_context.tool_name,
                        args,
                        approval=approval,
                        context=call_context,
                    )
                else:
                    result = invoke(
                        call_context.tool_name,
                        args,
                        approval=approval,
                    )
                if not isinstance(result, Mapping):
                    raise TypeError("plugin invoke() must return a mapping")
                _send(connection, {
                    "version": _PROTOCOL_VERSION,
                    "type": "response",
                    "ok": True,
                    "result": dict(result),
                })
            except PluginInvocationCancelled as exc:
                _send(connection, {
                    "version": _PROTOCOL_VERSION,
                    "type": "response",
                    "ok": False,
                    "code": "PLUGIN_CANCELLED",
                    "error": str(exc)[:500],
                })
            except PluginInvocationTimedOut as exc:
                _send(connection, {
                    "version": _PROTOCOL_VERSION,
                    "type": "response",
                    "ok": False,
                    "code": "PLUGIN_TIMEOUT",
                    "error": str(exc)[:500],
                })
            except BaseException as exc:  # noqa: BLE001 - contain child failures
                try:
                    _send(connection, {
                        "version": _PROTOCOL_VERSION,
                        "type": "response",
                        "ok": False,
                        "code": "PLUGIN_PROCESS_HANDLER_FAILED",
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    })
                except (BrokenPipeError, EOFError, OSError):
                    return
    except BaseException as exc:  # noqa: BLE001 - report boot/protocol failure
        try:
            _send(connection, {
                "version": _PROTOCOL_VERSION,
                "type": "boot_error",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            })
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        try:
            connection.close()
        except (OSError, EOFError):
            pass


class ProcessIsolatedPlugin:
    """Lifecycle adapter that runs a picklable plugin factory in a child.

    ``PluginRegistry`` recognizes this adapter through
    ``is_process_isolated=True`` when the execution policy explicitly selects
    ``isolation_mode="process"``.  The adapter is intentionally single-flight;
    the registry's semaphore remains the authoritative concurrency gate.

    AUD-22105：``invoke`` 的等待循环带本地看门狗——外部 deadline / 取消源 /
    policy 超时全部缺失时按 ``invoke_timeout_seconds``（默认 600s）到期即
    终止子进程并抛 :class:`PluginInvocationTimedOut`，绝不永久占住实例锁。
    """

    is_process_isolated = True

    def __init__(
        self,
        factory: PluginFactory,
        *,
        start_method: str = "spawn",
        startup_timeout_seconds: float = 5.0,
        terminate_grace_seconds: float = 0.25,
        sandbox_launcher: PluginSandboxLauncher | None = None,
        invoke_timeout_seconds: float | None = _DEFAULT_INVOKE_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(factory):
            raise TypeError("isolated plugin factory must be callable")
        if startup_timeout_seconds <= 0 or terminate_grace_seconds <= 0:
            raise ValueError("process timeouts must be positive")
        if invoke_timeout_seconds is not None and (
            isinstance(invoke_timeout_seconds, bool)
            or not isinstance(invoke_timeout_seconds, (int, float))
            or invoke_timeout_seconds <= 0
        ):
            raise ValueError("invoke_timeout_seconds must be a positive number or None")
        try:
            self._mp_context = mp.get_context(start_method)
        except ValueError as exc:
            raise ValueError(f"unsupported multiprocessing start method: {start_method}") from exc
        self._factory = factory
        self._startup_timeout = float(startup_timeout_seconds)
        self._terminate_grace = float(terminate_grace_seconds)
        # AUD-22105：invoke 本地看门狗（context/policy 都未给 deadline 时的
        # 兜底上限；None = 宿主显式声明不限时）。
        self._invoke_timeout_seconds = (
            None if invoke_timeout_seconds is None else float(invoke_timeout_seconds)
        )
        self._sandbox_launcher = sandbox_launcher
        self._process: mp.Process | None = None
        self._connection: Connection | None = None
        self._event_sink: Callable[[str, Mapping[str, Any]], None] | None = None
        self._plugin_id = ""
        self._host_io: PluginHostIO | None = None
        # AUD-22105：activate 时捕获 policy，供 invoke 在外部 deadline 缺失时
        # 采用 policy.timeout_seconds 作为看门狗预算。
        self._policy: PluginExecutionPolicy | None = None
        self._sandbox_report = PluginSandboxReport(
            platform="unknown",
            requested_mode="report",
            available=False,
            enforced=False,
            reason="not activated",
        )
        self._health = PluginHealth(status="inactive", message="not activated")
        self._lock = threading.RLock()

    def activate(self, context: PluginContext) -> None:
        with self._lock:
            if self._is_alive_locked():
                return
            try:
                pickle.dumps(self._factory)
            except (pickle.PickleError, TypeError, AttributeError) as exc:
                raise TypeError(
                    "isolated plugin factory must be importable and picklable"
                ) from exc
            self._plugin_id = str(context.plugin_id)
            # AUD-22105：捕获 policy 供 invoke 看门狗使用（外部 deadline 缺失
            # 时采用 policy.timeout_seconds）。
            self._policy = context.policy
            self._sandbox_report = validate_sandbox_launcher(
                self._sandbox_launcher,
                requested_mode=context.policy.sandbox_mode,
            )
            if context.policy.sandbox_mode == "required" and not (
                self._sandbox_report.available and self._sandbox_report.enforced
            ):
                raise RuntimeError(
                    "PLUGIN_SANDBOX_REQUIRED: injected launcher did not prove an enforced OS sandbox"
                )
            parent, child = self._mp_context.Pipe(duplex=True)
            if self._sandbox_launcher is None:
                process = self._mp_context.Process(
                    target=_plugin_worker,
                    args=(child, self._factory, str(context.plugin_id), context.policy),
                    name=f"topos-plugin-{context.plugin_id}",
                    daemon=True,
                )
            else:
                process = self._sandbox_launcher.launch(
                    mp_context=self._mp_context,
                    target=_plugin_worker,
                    args=(child, self._factory, str(context.plugin_id), context.policy),
                    name=f"topos-plugin-{context.plugin_id}",
                )
                if process is None or not callable(getattr(process, "start", None)):
                    raise TypeError("sandbox launcher must return a startable process")
            self._connection = parent
            self._process = process
            self._event_sink = context.emit
            if context.emit is not None:
                try:
                    context.emit(
                        "plugin.sandbox_enabled"
                        if self._sandbox_report.enforced
                        else "plugin.sandbox_degraded",
                        self._sandbox_report.to_dict(),
                    )
                except Exception:
                    pass
            self._host_io = PluginHostIO(
                context.resource_broker or DefaultDenyResourceBroker(),
                context.plugin_id,
                context.policy,
            )
            try:
                process.start()
            except Exception:
                self._close_connection_locked()
                self._process = None
                raise
            finally:
                try:
                    child.close()
                except (OSError, EOFError):
                    pass
            try:
                message = self._wait_for_type_locked("ready", self._startup_timeout)
                health = message.get("health", {})
                self._health = PluginHealth(
                    status=str(health.get("status", "unknown")),
                    message=str(health.get("message", ""))[:500],
                    checked_at=float(health.get("checked_at", time.time())),
                )
                if self._health.status == "failed":
                    raise RuntimeError(self._health.message or "isolated plugin health failed")
            except Exception:
                self._terminate_locked()
                raise

    def deactivate(self) -> None:
        with self._lock:
            if not self._is_alive_locked():
                self._terminate_locked()
                self._health = PluginHealth(status="inactive", message="deactivated")
                return
            try:
                self._send_locked({"operation": "deactivate"})
                self._wait_for_type_locked("deactivated", self._startup_timeout)
            except Exception:
                # A stuck teardown must not keep the host process alive.
                self._terminate_locked()
                raise
            finally:
                self._terminate_locked()
            self._health = PluginHealth(status="inactive", message="deactivated")

    def health(self) -> PluginHealth:
        with self._lock:
            if self._process is not None and not self._process.is_alive():
                return PluginHealth(status="failed", message="isolated plugin process exited")
            return self._health

    def sandbox_report(self) -> PluginSandboxReport:
        """Return the last honest OS sandbox capability report."""

        with self._lock:
            return self._sandbox_report

    def invoke(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        approval: Any = None,
        context: PluginCallContext | None = None,
    ) -> Mapping[str, Any]:
        with self._lock:
            if not self._is_alive_locked() or self._connection is None:
                raise PluginInvocationProcessCrashed(
                    "isolated plugin process is not running"
                )
            if context is not None:
                context.raise_if_cancelled()
            remaining = None
            if context is not None and context.deadline is not None:
                remaining = max(0.0, context.deadline - time.monotonic())
            # AUD-22105：本地看门狗。默认策略 timeout_seconds=None 且
            # context=None/无 deadline 时，等待循环此前没有任何退出条件（子
            # 进程活着但不回包 → 永久自旋并占住 self._lock，health/deactivate/
            # activate 全部陪死）。优先级：外部 deadline > policy.timeout_seconds
            # > 构造参数 invoke_timeout_seconds（默认 600s）。看门狗预算同时
            # 发给子进程（remaining_seconds），协作插件可先自行超时退出。
            if remaining is None:
                remaining = self._watchdog_budget_seconds()
            deadline = (
                None if remaining is None else time.monotonic() + remaining
            )
            self._send_locked({
                "operation": "invoke",
                "tool_name": str(tool_name),
                "args": dict(args or {}),
                "approval": approval,
                "invocation_id": context.invocation_id if context else "",
                "run_id": context.run_id if context else "",
                "remaining_seconds": remaining,
            })
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    self._terminate_locked()
                    raise PluginInvocationTimedOut(
                        "plugin invocation exceeded its watchdog deadline "
                        f"({remaining:.3f}s without external deadline/cancel source)"
                    )
                if context is not None:
                    if context.cancelled:
                        self._terminate_locked()
                        raise PluginInvocationCancelled("plugin invocation cancelled")
                    if context.timed_out:
                        self._terminate_locked()
                        raise PluginInvocationTimedOut("plugin invocation deadline exceeded")
                message = self._poll_message_locked(0.02)
                if message is None:
                    if not self._is_alive_locked():
                        self._terminate_locked()
                        raise PluginInvocationProcessCrashed(
                            "isolated plugin process exited without a result"
                        )
                    continue
                message_type = str(message.get("type", ""))
                if message_type == "event":
                    self._forward_event_locked(message)
                    continue
                if message_type == "io_request":
                    self._handle_io_request_locked(message)
                    continue
                if message_type != "response":
                    self._terminate_locked()
                    raise PluginInvocationProcessCrashed(
                        "isolated plugin returned an invalid response"
                    )
                if bool(message.get("ok")):
                    result = message.get("result", {})
                    if not isinstance(result, Mapping):
                        raise PluginInvocationProcessCrashed(
                            "isolated plugin response result must be an object"
                        )
                    return dict(result)
                code = str(message.get("code", "PLUGIN_PROCESS_HANDLER_FAILED"))
                error = str(message.get("error", "isolated plugin call failed"))[:500]
                if code == "PLUGIN_CANCELLED":
                    raise PluginInvocationCancelled(error)
                if code == "PLUGIN_TIMEOUT":
                    raise PluginInvocationTimedOut(error)
                raise PluginInvocationProcessCrashed(error)

    def _is_alive_locked(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _watchdog_budget_seconds(self) -> float | None:
        """Resolve the local invoke watchdog when no external deadline exists.

        AUD-22105：``policy.timeout_seconds``（默认 None）优先于构造参数的
        进程级默认上限；两者都为 None 时返回 None（宿主显式不限时）。
        """
        policy_timeout = (
            getattr(self._policy, "timeout_seconds", None)
            if self._policy is not None else None
        )
        if (
            isinstance(policy_timeout, (int, float))
            and not isinstance(policy_timeout, bool)
            and policy_timeout > 0
        ):
            return float(policy_timeout)
        return self._invoke_timeout_seconds

    def _send_locked(self, message: Mapping[str, Any]) -> None:
        if self._connection is None:
            raise PluginInvocationProcessCrashed("isolated plugin IPC is unavailable")
        try:
            _send(self._connection, message)
        except (BrokenPipeError, EOFError, OSError, pickle.PickleError, TypeError) as exc:
            self._terminate_locked()
            raise PluginInvocationProcessCrashed(
                "isolated plugin request could not cross the process boundary"
            ) from exc

    def _poll_message_locked(self, timeout: float) -> dict[str, Any] | None:
        if self._connection is None:
            return None
        try:
            if not self._connection.poll(timeout):
                return None
            message = self._connection.recv()
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise PluginInvocationProcessCrashed(
                "isolated plugin IPC closed unexpectedly"
            ) from exc
        if not isinstance(message, dict) or int(message.get("version", -1)) != _PROTOCOL_VERSION:
            raise PluginInvocationProcessCrashed("isolated plugin protocol version mismatch")
        return message

    def _wait_for_type_locked(self, expected: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._poll_message_locked(min(0.02, deadline - time.monotonic()))
            if message is None:
                if not self._is_alive_locked():
                    raise PluginInvocationProcessCrashed(
                        "isolated plugin process exited during lifecycle transition"
                    )
                continue
            message_type = str(message.get("type", ""))
            if message_type == "event":
                self._forward_event_locked(message)
                continue
            if message_type == "io_request":
                self._handle_io_request_locked(message)
                continue
            if message_type == "boot_error":
                raise PluginInvocationProcessCrashed(str(message.get("error", "plugin boot failed")))
            if message_type == expected:
                return message
            raise PluginInvocationProcessCrashed(
                f"isolated plugin expected {expected}, received {message_type or 'empty'}"
            )
        raise TimeoutError(f"isolated plugin {expected} timed out")

    def _forward_event_locked(self, message: Mapping[str, Any]) -> None:
        sink = self._event_sink
        if sink is None:
            return
        try:
            payload = message.get("payload", {})
            sink(
                str(message.get("kind", "plugin.child_event")),
                dict(payload) if isinstance(payload, Mapping) else {},
            )
        except Exception:
            return

    def _handle_io_request_locked(self, message: Mapping[str, Any]) -> None:
        """Service one child capability request in the host process."""

        host_io = self._host_io
        operation = str(message.get("operation", ""))
        try:
            if host_io is None:
                result = HostIOResult(False, "PLUGIN_IO_UNAVAILABLE", "host I/O capability is unavailable")
            elif operation == "check_path":
                decision = host_io.check_path(
                    str(message.get("path", "")),
                    mode=str(message.get("mode", "read")),
                )
                self._send_locked({
                    "version": _PROTOCOL_VERSION,
                    "type": "io_response",
                    "ok": True,
                    "decision": decision.to_dict(),
                })
                return
            elif operation == "check_network":
                decision = host_io.check_network(str(message.get("url", "")))
                self._send_locked({
                    "version": _PROTOCOL_VERSION,
                    "type": "io_response",
                    "ok": True,
                    "decision": decision.to_dict(),
                })
                return
            elif operation == "read_bytes":
                result = host_io.read_bytes(
                    str(message.get("path", "")),
                    max_bytes=message.get("max_bytes"),
                )
            elif operation == "write_bytes":
                result = host_io.write_bytes(
                    str(message.get("path", "")),
                    message.get("data", b""),
                    overwrite=bool(message.get("overwrite", False)),
                    max_bytes=message.get("max_bytes"),
                )
            elif operation == "request_bytes":
                remaining = message.get("remaining_seconds")
                requested_timeout = message.get("timeout_seconds")
                timeout = requested_timeout
                if remaining is not None:
                    remaining_value = max(0.0, float(remaining))
                    timeout = (
                        remaining_value
                        if timeout is None
                        else min(float(timeout), remaining_value)
                    )
                    if remaining_value <= 0:
                        result = HostIOResult(False, "PLUGIN_TIMEOUT", "plugin invocation deadline exceeded")
                    else:
                        result = host_io.request_bytes(
                            str(message.get("url", "")),
                            method=str(message.get("method", "GET")),
                            body=message.get("body", b""),
                            headers=message.get("headers", {}),
                            max_bytes=message.get("max_bytes"),
                            timeout_seconds=timeout,
                        )
                else:
                    result = host_io.request_bytes(
                        str(message.get("url", "")),
                        method=str(message.get("method", "GET")),
                        body=message.get("body", b""),
                        headers=message.get("headers", {}),
                        max_bytes=message.get("max_bytes"),
                        timeout_seconds=timeout,
                    )
            else:
                result = HostIOResult(False, "PLUGIN_IO_OPERATION_DENIED", "host I/O operation is not allowed")
        except (TypeError, ValueError, OSError) as exc:
            result = HostIOResult(False, "PLUGIN_IO_REQUEST_INVALID", str(exc)[:500])
        self._send_locked({
            "version": _PROTOCOL_VERSION,
            "type": "io_response",
            "ok": result.ok,
            "code": result.code,
            "message": result.message,
            "data": result.data,
            "bytes_count": result.bytes_count,
            "status_code": result.status_code,
            "content_type": result.content_type,
        })

    def _terminate_locked(self) -> None:
        process = self._process
        self._close_connection_locked()
        self._process = None
        if process is None:
            return
        try:
            if process.is_alive():
                process.terminate()
                process.join(self._terminate_grace)
            if process.is_alive():
                process.kill()
                process.join(self._terminate_grace)
            else:
                process.join(0)
        except (OSError, ValueError):
            return

    def _close_connection_locked(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except (OSError, EOFError):
                pass


__all__ = ["PluginFactory", "ProcessIsolatedPlugin"]
