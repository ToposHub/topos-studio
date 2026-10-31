"""Safe plugin manifest and lifecycle registry for the AI editing harness.

This module deliberately stays Qt/provider-free.  A plugin can declare
capabilities and be activated by the host, but it never receives implicit
approval, filesystem access, credentials, or timeline internals.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import re
import threading
import time
import types
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from src.features.ai_harness.plugin_broker import (
    DefaultDenyResourceBroker,
    PluginHostIO,
    PolicyBoundResourceBroker,
)


PLUGIN_API_VERSION = "1.0"
_LOG = logging.getLogger(__name__)
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_PERMISSIONS = frozenset({
    "read", "preview", "proposal", "write", "expensive", "file_write",
})
_FORBIDDEN_CAPABILITIES = frozenset({
    "exec_python", "execute_code", "read_arbitrary_path",
    "call_shell", "click_ui", "inject_glsl", "download_url",
})
_PLUGIN_ISOLATION_MODES = frozenset({"in_process", "process"})
_PLUGIN_SANDBOX_MODES = frozenset({"report", "required"})


@dataclass(frozen=True)
class PluginExecutionPolicy:
    """Host-enforced limits for a plugin tool call.

    ``in_process`` is the backwards-compatible low-overhead mode.  The
    optional ``process`` mode is only valid with a
    :class:`ProcessIsolatedPlugin`-compatible adapter; the registry rejects a
    plain handler paired with that policy instead of pretending that Python
    code is isolated.
    """

    max_concurrent_calls: int = 1
    max_input_chars: int = 64_000
    max_output_chars: int = 256_000
    allow_network: bool = False
    allow_filesystem: bool = False
    timeout_seconds: float | None = None
    isolation_mode: str = "in_process"
    sandbox_mode: str = "report"

    def __post_init__(self) -> None:
        for name in ("max_concurrent_calls", "max_input_chars", "max_output_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive number or None")
        if str(self.isolation_mode) not in _PLUGIN_ISOLATION_MODES:
            raise ValueError(
                "isolation_mode must be one of: in_process, process"
            )
        if str(self.sandbox_mode) not in _PLUGIN_SANDBOX_MODES:
            raise ValueError("sandbox_mode must be one of: report, required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_concurrent_calls": self.max_concurrent_calls,
            "max_input_chars": self.max_input_chars,
            "max_output_chars": self.max_output_chars,
            "allow_network": bool(self.allow_network),
            "allow_filesystem": bool(self.allow_filesystem),
            "timeout_seconds": self.timeout_seconds,
            "isolation_mode": str(self.isolation_mode),
            "sandbox_mode": str(self.sandbox_mode),
        }


class PluginInvocationCancelled(Exception):
    """Raised cooperatively by a plugin when its host call is cancelled."""


class PluginInvocationTimedOut(Exception):
    """Raised cooperatively by a plugin when its deadline expires."""


class PluginInvocationProcessCrashed(Exception):
    """Raised when an isolated plugin process exits without a result."""


@dataclass(frozen=True)
class PluginCallContext:
    """Bounded control context exposed to cooperative plugin handlers."""

    invocation_id: str
    plugin_id: str
    tool_name: str
    run_id: str
    started_at: float
    deadline: float | None
    cancel_event: threading.Event | None = None
    cancel_check: Callable[[], bool] | None = None
    host_io: Any = None

    @property
    def cancelled(self) -> bool:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        if self.cancel_check is None:
            return False
        try:
            return bool(self.cancel_check())
        except Exception:
            # A broken cancellation source must fail closed.
            return True

    @property
    def timed_out(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise PluginInvocationCancelled("plugin invocation cancelled")
        if self.timed_out:
            raise PluginInvocationTimedOut("plugin invocation deadline exceeded")


def _version_key(version: str) -> tuple[int, int, int, str]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", str(version))
    if match is None:
        return (-1, -1, -1, str(version))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4))


def _freeze_mapping(value: Mapping[str, Any], *, field_name: str) -> Mapping[str, Any]:
    """Snapshot a caller-supplied mapping into an immutable proxy (AUD-22201).

    Callers keep the object they passed in; without a defensive copy they
    could mutate manifest fields after registration and make the recorded
    ``content_hash``/signature drift.  The copy is deep so nested structures
    cannot alias caller-owned mutable objects either.
    """

    if value is None:
        return types.MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping, got {type(value).__name__}")
    try:
        snapshot = copy.deepcopy(dict(value))
    except Exception:  # noqa: BLE001 - non-copyable metadata degrades to a shallow snapshot
        snapshot = dict(value)
    return types.MappingProxyType(snapshot)


def _serialized_char_count(value: Any) -> int | None:
    """Return a deterministic JSON size, or ``None`` for unsupported values."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return len(encoded)


class PluginSource(str, Enum):
    BUILTIN = "builtin"
    LOCAL_DEV = "local_dev"
    VERIFIED_THIRD_PARTY = "verified_third_party"

    @property
    def unverified(self) -> bool:
        return self is PluginSource.LOCAL_DEV


class PluginState(str, Enum):
    REGISTERED = "registered"
    ENABLED = "enabled"
    ACTIVE = "active"
    DEGRADED = "degraded"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass(frozen=True)
class PluginCapability:
    """One capability exposed by a plugin."""

    capability_id: str
    description: str = ""
    tool_names: tuple[str, ...] = ()
    permission: str = "read"
    requires_approval: bool = False
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        capability_id = str(self.capability_id or "").strip()
        if not capability_id:
            raise ValueError("plugin capability_id is required")
        if self.permission not in _PERMISSIONS:
            raise ValueError(f"unsupported plugin permission: {self.permission!r}")
        tools = tuple(dict.fromkeys(str(name).strip() for name in self.tool_names if str(name).strip()))
        forbidden = sorted(set(tools) & _FORBIDDEN_CAPABILITIES)
        if forbidden:
            raise ValueError(f"plugin declares forbidden capabilities: {forbidden}")
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "tool_names", tools)
        object.__setattr__(
            self,
            "requires_approval",
            bool(self.requires_approval or self.permission in {"write", "expensive", "file_write"}),
        )
        # AUD-22201: freeze mapping fields so callers cannot mutate a
        # registered capability in place (content_hash integrity).
        object.__setattr__(
            self, "input_schema",
            _freeze_mapping(self.input_schema, field_name="PluginCapability.input_schema"),
        )
        object.__setattr__(
            self, "output_schema",
            _freeze_mapping(self.output_schema, field_name="PluginCapability.output_schema"),
        )
        object.__setattr__(
            self, "metadata",
            _freeze_mapping(self.metadata, field_name="PluginCapability.metadata"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "description": self.description[:1_000],
            "tool_names": list(self.tool_names),
            "permission": self.permission,
            "requires_approval": bool(self.requires_approval),
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PluginManifest:
    """Immutable, versioned plugin declaration."""

    plugin_id: str
    display_name: str
    version: str = "1.0.0"
    api_version: str = PLUGIN_API_VERSION
    source: PluginSource = PluginSource.BUILTIN
    author: str = ""
    description: str = ""
    capabilities: tuple[PluginCapability, ...] = ()
    dependencies: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    declared_hash: str = ""
    signature: str = ""
    signature_key_id: str = ""

    def __post_init__(self) -> None:
        plugin_id = str(self.plugin_id or "").strip()
        if not _PLUGIN_ID_RE.fullmatch(plugin_id):
            raise ValueError("plugin_id must use lowercase letters, digits, '.', '_' or '-'")
        if not str(self.display_name or "").strip():
            raise ValueError("plugin display_name is required")
        if not _SEMVER_RE.fullmatch(str(self.version)):
            raise ValueError(f"invalid plugin version: {self.version!r}")
        if not str(self.api_version or "").strip():
            raise ValueError("plugin api_version is required")
        source = self.source if isinstance(self.source, PluginSource) else PluginSource(str(self.source))
        capabilities = tuple(self.capabilities)
        ids = [capability.capability_id for capability in capabilities]
        if len(ids) != len(set(ids)):
            raise ValueError("plugin capability ids must be unique")
        dependencies = tuple(dict.fromkeys(
            str(dependency).strip() for dependency in self.dependencies
            if str(dependency).strip()
        ))
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "declared_hash", str(self.declared_hash or "").strip())
        object.__setattr__(self, "signature", str(self.signature or "").strip())
        object.__setattr__(self, "signature_key_id", str(self.signature_key_id or "").strip())
        # AUD-22201: freeze metadata so a registered manifest's content_hash
        # cannot drift via a caller-owned mutable mapping.
        object.__setattr__(
            self, "metadata",
            _freeze_mapping(self.metadata, field_name="PluginManifest.metadata"),
        )

    @property
    def unverified(self) -> bool:
        return self.source.unverified

    @property
    def content_hash(self) -> str:
        payload = self.to_dict(include_hash=False)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def verify_declared_hash(self) -> bool:
        return not self.declared_hash or self.declared_hash == self.content_hash

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plugin_id": self.plugin_id,
            "display_name": self.display_name,
            "version": self.version,
            "api_version": self.api_version,
            "source": self.source.value,
            "author": self.author[:200],
            "description": self.description[:2_000],
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "dependencies": list(self.dependencies),
            "metadata": dict(self.metadata),
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
            payload["declared_hash"] = self.declared_hash
            payload["signature"] = self.signature
            payload["signature_key_id"] = self.signature_key_id
            payload["unverified"] = self.unverified
        return payload


@dataclass(frozen=True)
class PluginHealth:
    status: str = "unknown"
    message: str = ""
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message[:500],
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True)
class PluginContext:
    plugin_id: str
    emit: Callable[[str, Mapping[str, Any]], None] | None = None
    policy: PluginExecutionPolicy = field(default_factory=PluginExecutionPolicy)
    resource_broker: Any = None
    host_io: PluginHostIO | None = None


class PluginLifecycle(Protocol):
    """Host-controlled lifecycle implemented by a concrete plugin adapter."""

    def activate(self, context: PluginContext) -> None:
        ...

    def deactivate(self) -> None:
        ...

    def health(self) -> PluginHealth:
        ...


class PluginToolHandler(Protocol):
    """Optional tool entry point implemented by runnable capability plugins."""

    def invoke(
        self, tool_name: str, args: Mapping[str, Any], *, approval: Any = None,
    ) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class PluginRecord:
    manifest: PluginManifest
    state: PluginState
    health: PluginHealth
    policy: PluginExecutionPolicy = field(default_factory=PluginExecutionPolicy)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "state": self.state.value,
            "health": self.health.to_dict(),
            "policy": self.policy.to_dict(),
            "error": self.error[:500],
        }


PluginEventSink = Callable[[str, str, Mapping[str, Any]], None]


@dataclass
class _PluginEntry:
    manifest: PluginManifest
    plugin: PluginLifecycle | None
    state: PluginState
    policy: PluginExecutionPolicy = field(default_factory=PluginExecutionPolicy)
    semaphore: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(1),
        repr=False,
    )
    health: PluginHealth = field(default_factory=PluginHealth)
    host_io: Any = None
    error: str = ""


class PluginRegistry:
    """Thread-safe host registry with fail-closed lifecycle transitions."""

    def __init__(
        self,
        *,
        event_sink: PluginEventSink | None = None,
        resource_broker: Any = None,
    ) -> None:
        self._entries: dict[str, _PluginEntry] = {}
        self._event_sink = event_sink
        self._resource_broker = resource_broker or DefaultDenyResourceBroker()
        self._lock = threading.RLock()

    def register(
        self,
        manifest: PluginManifest,
        plugin: PluginLifecycle | None = None,
        *,
        enabled: bool = True,
        replace_existing: bool = False,
        policy: PluginExecutionPolicy | None = None,
    ) -> bool:
        """Register a manifest; invalid hashes and duplicate IDs are rejected."""

        execution_policy = policy or PluginExecutionPolicy()
        if not manifest.verify_declared_hash():
            self._emit(manifest.plugin_id, "plugin.registration_rejected", {
                "reason": "content_hash_mismatch",
            })
            return False
        if execution_policy.isolation_mode == "process" and not bool(
            getattr(plugin, "is_process_isolated", False)
        ):
            self._emit(manifest.plugin_id, "plugin.registration_rejected", {
                "reason": "process_isolation_requires_adapter",
                "code": "PLUGIN_ISOLATION_ADAPTER_REQUIRED",
            })
            return False
        if execution_policy.sandbox_mode == "required" and execution_policy.isolation_mode != "process":
            self._emit(manifest.plugin_id, "plugin.registration_rejected", {
                "reason": "required_sandbox_needs_process_isolation",
                "code": "PLUGIN_SANDBOX_REQUIRES_PROCESS",
            })
            return False
        with self._lock:
            existing = self._entries.get(manifest.plugin_id)
            if existing is not None:
                if not replace_existing or existing.state == PluginState.ACTIVE:
                    return False
                if _version_key(manifest.version) <= _version_key(existing.manifest.version):
                    return False
            state = PluginState.ENABLED if enabled else PluginState.DISABLED
            self._entries[manifest.plugin_id] = _PluginEntry(
                manifest=manifest,
                plugin=plugin,
                state=state,
                policy=execution_policy,
                semaphore=threading.BoundedSemaphore(execution_policy.max_concurrent_calls),
            )
        self._emit(manifest.plugin_id, "plugin.registered", {
            "version": manifest.version,
            "state": state.value,
            "capability_count": len(manifest.capabilities),
        })
        return True

    def unregister(self, plugin_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(str(plugin_id))
            if entry is None or entry.state == PluginState.ACTIVE:
                return False
            del self._entries[str(plugin_id)]
        self._emit(str(plugin_id), "plugin.unregistered", {})
        return True

    def get(self, plugin_id: str) -> PluginRecord | None:
        with self._lock:
            entry = self._entries.get(str(plugin_id))
            return self._snapshot(entry) if entry is not None else None

    def list(self, *, include_disabled: bool = True) -> tuple[PluginRecord, ...]:
        with self._lock:
            entries = tuple(self._entries.values())
            if not include_disabled:
                entries = tuple(
                    entry for entry in entries
                    if entry.state != PluginState.DISABLED
                )
            return tuple(
                self._snapshot(entry)
                for entry in sorted(entries, key=lambda item: item.manifest.plugin_id)
            )

    def enable(self, plugin_id: str) -> bool:
        return self._set_state(plugin_id, PluginState.ENABLED)

    def disable(self, plugin_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(str(plugin_id))
            if entry is None or entry.state == PluginState.ACTIVE:
                return False
            if entry.state == PluginState.DISABLED:
                return False
            entry.state = PluginState.DISABLED
        self._emit(str(plugin_id), "plugin.disabled", {})
        return True

    def degrade(self, plugin_id: str, message: str) -> bool:
        """Mark a plugin degraded without activating or executing it."""

        plugin_id = str(plugin_id)
        with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None or entry.state == PluginState.ACTIVE:
                return False
            entry.state = PluginState.DEGRADED
            entry.health = PluginHealth(status="degraded", message=str(message)[:500])
            entry.error = str(message)[:500]
        self._emit(plugin_id, "plugin.degraded", {"reason": str(message)[:500]})
        return True

    def activate(self, plugin_id: str) -> bool:
        plugin_id = str(plugin_id)
        with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None or entry.state not in {
                PluginState.ENABLED, PluginState.DEGRADED,
            }:
                return False
            plugin = entry.plugin
            # Snapshot the policy of the entry that owns ``plugin``; the
            # activation broker/host_io below must be built from it (AUD-22202).
            activation_policy = entry.policy
        if plugin is None:
            with self._lock:
                entry = self._entries.get(plugin_id)
                if entry is None or entry.plugin is not None:
                    # The entry was replaced/unregistered while activation ran
                    # outside the lock; never mark the newcomer ACTIVE.
                    return False
                entry.state = PluginState.ACTIVE
                entry.health = PluginHealth(status="healthy", message="manifest-only plugin")
            self._emit(plugin_id, "plugin.activated", {"manifest_only": True})
            return True
        try:
            bound_broker = PolicyBoundResourceBroker(
                self._resource_broker, plugin_id, activation_policy,
            )
            host_io = PluginHostIO(bound_broker, plugin_id, activation_policy)
            plugin.activate(PluginContext(
                plugin_id=plugin_id,
                emit=self._plugin_emit(plugin_id),
                policy=activation_policy,
                resource_broker=bound_broker,
                host_io=host_io,
            ))
            health = self._read_health(plugin)
        except Exception as exc:  # noqa: BLE001 - isolate plugin failures
            with self._lock:
                current = self._entries.get(plugin_id)
                stale = current is None or current.plugin is not plugin
            if stale:
                # AUD-22202: the entry was replaced while activate() ran; a
                # failure of the old instance must not mark the new one FAILED.
                self._deactivate_orphan(plugin, plugin_id, "activate failed on replaced entry")
            else:
                self._mark_failed(plugin_id, exc)
            return False
        with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None or entry.plugin is not plugin:
                # AUD-22202: register(replace_existing=True) swapped the entry
                # while activate() ran outside the lock.  The new plugin object
                # never ran activate(), so it must not be marked ACTIVE, and
                # the broker/host_io built from the old policy must not be
                # attached to it.  Tear the orphaned instance down instead.
                stale = True
            else:
                stale = False
                entry.state = PluginState.ACTIVE if health.status != "failed" else PluginState.FAILED
                entry.health = health
                entry.host_io = host_io
                entry.error = "" if entry.state == PluginState.ACTIVE else health.message[:500]
        if stale:
            self._deactivate_orphan(plugin, plugin_id, "entry replaced during activation")
            return False
        self._emit(
            plugin_id,
            "plugin.activated" if entry.state == PluginState.ACTIVE else "plugin.failed",
            {"health": health.to_dict()},
        )
        return entry.state == PluginState.ACTIVE

    def _deactivate_orphan(self, plugin: PluginLifecycle, plugin_id: str, reason: str) -> None:
        """Best-effort teardown of an instance whose registry entry vanished."""

        try:
            plugin.deactivate()
        except Exception:  # noqa: BLE001 - failed teardown must not escape the registry
            _LOG.exception(
                "orphaned plugin %s deactivate() failed after %s", plugin_id, reason,
            )
        self._emit(plugin_id, "plugin.activation_abandoned", {"reason": reason})

    def deactivate(self, plugin_id: str) -> bool:
        plugin_id = str(plugin_id)
        with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None or entry.state != PluginState.ACTIVE:
                return False
            plugin = entry.plugin
        try:
            if plugin is not None:
                plugin.deactivate()
        except Exception as exc:  # noqa: BLE001 - failed teardown is visible
            self._mark_failed(plugin_id, exc)
            return False
        with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None:
                return False
            entry.state = PluginState.ENABLED
            entry.health = PluginHealth(status="inactive", message="deactivated")
        self._emit(plugin_id, "plugin.deactivated", {})
        return True

    def restore_project_state(self, plugin_id: str, persisted_state: str) -> bool:
        """Apply persisted lifecycle facts without activating plugin code.

        A project may remember that a plugin used to be active, but reopening
        the project must not execute ``activate`` implicitly.  Such a state is
        restored as ``enabled`` and remains explicitly reactivatable by the
        host.  A persisted ``disabled`` state is restored as disabled.
        """

        plugin_id = str(plugin_id)
        requested = str(persisted_state or "").strip().lower()
        if requested not in {
            PluginState.REGISTERED.value,
            PluginState.ENABLED.value,
            PluginState.ACTIVE.value,
            PluginState.DEGRADED.value,
            PluginState.FAILED.value,
            PluginState.DISABLED.value,
        }:
            return False
        with self._lock:
            entry = self._entries.get(plugin_id)
            current_state = entry.state if entry is not None else None
        if entry is None:
            return False
        if current_state == PluginState.ACTIVE and not self.deactivate(plugin_id):
            return False

        if requested == PluginState.DISABLED.value:
            target = PluginState.DISABLED
            health = PluginHealth(
                status="inactive",
                message="project state restored: disabled; explicit enable required",
            )
        elif requested == PluginState.DEGRADED.value:
            target = PluginState.DEGRADED
            health = PluginHealth(
                status="degraded",
                message="project state restored: previously degraded",
            )
        elif requested == PluginState.FAILED.value:
            target = PluginState.FAILED
            health = PluginHealth(
                status="failed",
                message="project state restored: previously failed",
            )
        else:
            # REGISTERED, ENABLED and ACTIVE all require an explicit activate
            # after a process/project restore.  Never call plugin.activate().
            target = PluginState.ENABLED
            health = PluginHealth(
                status="inactive",
                message="project state restored; explicit activation required",
            )

        with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None:
                return False
            entry.state = target
            entry.health = health
            entry.error = health.message if target in {
                PluginState.DEGRADED, PluginState.FAILED,
            } else ""
        self._emit(plugin_id, "plugin.project_state_restored", {
            "requested_state": requested,
            "restored_state": target.value,
            "reactivation_required": requested == PluginState.ACTIVE.value,
        })
        return True

    def health(self, plugin_id: str) -> PluginHealth | None:
        plugin_id = str(plugin_id)
        with self._lock:
            entry = self._entries.get(plugin_id)
            plugin = entry.plugin if entry is not None else None
        if entry is None:
            return None
        if plugin is None or entry.state != PluginState.ACTIVE:
            return entry.health
        try:
            health = self._read_health(plugin)
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(plugin_id, exc)
            return PluginHealth(status="failed", message=str(exc)[:500])
        with self._lock:
            current = self._entries.get(plugin_id)
            if current is not None:
                current.health = health
                if health.status == "failed":
                    current.state = PluginState.FAILED
        return health

    def capabilities(self, plugin_id: str, *, active_only: bool = True) -> tuple[PluginCapability, ...]:
        with self._lock:
            entry = self._entries.get(str(plugin_id))
            if entry is None or (active_only and entry.state != PluginState.ACTIVE):
                return ()
            return tuple(entry.manifest.capabilities)

    def active_tool_capabilities(self) -> tuple[tuple[str, PluginCapability], ...]:
        """Return runnable tool capabilities from active plugins in stable order."""

        with self._lock:
            rows: list[tuple[str, PluginCapability]] = []
            for plugin_id, entry in self._entries.items():
                if entry.state != PluginState.ACTIVE or not callable(getattr(entry.plugin, "invoke", None)):
                    continue
                rows.extend((plugin_id, capability) for capability in entry.manifest.capabilities)
            return tuple(sorted(rows, key=lambda row: (row[0], row[1].capability_id)))

    def plugin_capability_for_tool(
        self, tool_name: str,
    ) -> tuple[str, PluginCapability] | None:
        """Resolve a unique active plugin owner; ambiguity fails closed."""

        matches = tuple(
            (plugin_id, capability)
            for plugin_id, capability in self.active_tool_capabilities()
            if tool_name in capability.tool_names
        )
        return matches[0] if len(matches) == 1 else None

    def invoke_tool(
        self,
        plugin_id: str,
        tool_name: str,
        args: Mapping[str, Any] | None = None,
        *,
        approval: Any = None,
        cancel_event: threading.Event | None = None,
        cancel_check: Callable[[], bool] | None = None,
        timeout_seconds: float | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        """Invoke a declared plugin tool with cooperative control semantics.

        A handler may accept ``context=PluginCallContext`` and call
        ``context.raise_if_cancelled()`` at safe points.  Legacy handlers keep
        their old signature; for those handlers the host reports late cancel
        or deadline facts but never claims that Python code was forcibly
        terminated.  True process termination remains a separate isolation
        adapter.
        """

        plugin_id = str(plugin_id)
        tool_name = str(tool_name)
        invocation_id = f"plugin-invocation-{uuid.uuid4().hex[:12]}"
        started_at = time.monotonic()
        cancel_event = cancel_event or threading.Event()
        with self._lock:
            entry = self._entries.get(plugin_id)
            plugin = entry.plugin if entry is not None else None
            policy = entry.policy if entry is not None else None
            semaphore = entry.semaphore if entry is not None else None
            capability = next(
                (
                    item for item in entry.manifest.capabilities
                    if item.capability_id and tool_name in item.tool_names
                ),
                None,  # 未知 tool_name → 走 fail-closed 返回，不抛 StopIteration
            ) if entry is not None else None
            active = entry is not None and entry.state == PluginState.ACTIVE
        if not active or plugin is None or capability is None:
            return {
                "ok": False,
                "error": "plugin tool is not active",
                "diagnostics": [{"code": "PLUGIN_NOT_ACTIVE", "severity": "error"}],
            }
        if capability.requires_approval and approval is None:
            return {
                "ok": False,
                "error": "plugin tool requires host approval",
                "diagnostics": [{"code": "PERMISSION_DENIED", "severity": "error"}],
            }
        handler = getattr(plugin, "invoke", None)
        if not callable(handler):
            return {
                "ok": False,
                "error": "plugin tool handler unavailable",
                "diagnostics": [{"code": "PLUGIN_HANDLER_UNAVAILABLE", "severity": "error"}],
            }
        if args is None:
            payload: dict[str, Any] = {}
        elif not isinstance(args, Mapping):
            return {
                "ok": False,
                "error": "plugin tool arguments must be a mapping",
                "diagnostics": [{"code": "PLUGIN_INPUT_INVALID", "severity": "error"}],
            }
        else:
            payload = dict(args)
        input_chars = _serialized_char_count(payload)
        if input_chars is None:
            return {
                "ok": False,
                "error": "plugin tool arguments must be JSON-serializable",
                "diagnostics": [{"code": "PLUGIN_INPUT_NOT_SERIALIZABLE", "severity": "error"}],
            }
        if policy is None or semaphore is None:
            return {
                "ok": False,
                "error": "plugin execution policy unavailable",
                "diagnostics": [{"code": "PLUGIN_POLICY_UNAVAILABLE", "severity": "error"}],
            }
        effective_timeout = policy.timeout_seconds if timeout_seconds is None else timeout_seconds
        if effective_timeout is not None and (
            isinstance(effective_timeout, bool)
            or not isinstance(effective_timeout, (int, float))
            or effective_timeout <= 0
        ):
            return {
                "ok": False,
                "error": "plugin timeout must be a positive number",
                "diagnostics": [{"code": "PLUGIN_TIMEOUT_INVALID", "severity": "error"}],
            }
        deadline = (
            time.monotonic() + float(effective_timeout)
            if effective_timeout is not None else None
        )
        context = PluginCallContext(
            invocation_id=invocation_id,
            plugin_id=plugin_id,
            tool_name=tool_name,
            run_id=str(run_id),
            started_at=started_at,
            deadline=deadline,
            cancel_event=cancel_event,
            cancel_check=cancel_check,
            host_io=entry.host_io,
        )
        if context.cancelled:
            self._emit(plugin_id, "plugin.tool_cancelled", self._invocation_payload(
                context, code="PLUGIN_CANCELLED", input_size=input_chars,
            ))
            return {
                "ok": False,
                "error": "plugin tool invocation was cancelled",
                "diagnostics": [{"code": "PLUGIN_CANCELLED", "severity": "warning"}],
            }
        if input_chars > policy.max_input_chars:
            self._emit(plugin_id, "plugin.tool_rejected", {
                "tool_name": tool_name,
                "code": "PLUGIN_INPUT_LIMIT",
                "size": input_chars,
                "limit": policy.max_input_chars,
                "run_id": str(run_id),
                "invocation_id": invocation_id,
            })
            return {
                "ok": False,
                "error": "plugin tool input exceeds host limit",
                "diagnostics": [{
                    "code": "PLUGIN_INPUT_LIMIT",
                    "severity": "error",
                    "size": input_chars,
                    "limit": policy.max_input_chars,
                }],
            }
        if not semaphore.acquire(blocking=False):
            return {
                "ok": False,
                "error": "plugin tool concurrency limit reached",
                "diagnostics": [{
                    "code": "PLUGIN_BUSY",
                    "severity": "warning",
                    "limit": policy.max_concurrent_calls,
                }],
            }
        try:
            self._emit(plugin_id, "plugin.tool_started", self._invocation_payload(
                context, input_size=input_chars,
            ))
            try:
                parameters = inspect.signature(handler).parameters.values()
                accepts_context = any(
                    parameter.name == "context"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                accepts_context = False
            try:
                if accepts_context:
                    result = handler(
                        tool_name, payload, approval=approval, context=context,
                    )
                else:
                    result = handler(tool_name, payload, approval=approval)
            except PluginInvocationCancelled as exc:
                self._emit(plugin_id, "plugin.tool_cancelled", self._invocation_payload(
                    context, code="PLUGIN_CANCELLED", error=str(exc), input_size=input_chars,
                ))
                return {
                    "ok": False,
                    "error": "plugin tool invocation was cancelled",
                    "diagnostics": [{"code": "PLUGIN_CANCELLED", "severity": "warning"}],
                }
            except PluginInvocationTimedOut as exc:
                self._emit(plugin_id, "plugin.tool_timed_out", self._invocation_payload(
                    context, code="PLUGIN_TIMEOUT", error=str(exc), input_size=input_chars,
                ))
                return {
                    "ok": False,
                    "error": "plugin tool invocation exceeded its deadline",
                    "diagnostics": [{"code": "PLUGIN_TIMEOUT", "severity": "error"}],
                }
            except PluginInvocationProcessCrashed as exc:
                message = str(exc)[:500] or "isolated plugin process exited unexpectedly"
                self._emit(plugin_id, "plugin.tool_failed", self._invocation_payload(
                    context, code="PLUGIN_PROCESS_CRASHED", error=message,
                    input_size=input_chars,
                ))
                return {
                    "ok": False,
                    "error": message,
                    "diagnostics": [{"code": "PLUGIN_PROCESS_CRASHED", "severity": "error"}],
                }

            if context.cancelled or context.timed_out:
                if context.cancelled:
                    code = "PLUGIN_CANCELLED" if accepts_context else "PLUGIN_CANCELLED_NONCOOPERATIVE"
                    kind = "plugin.tool_cancelled"
                    message = "plugin tool invocation was cancelled"
                    severity = "warning"
                else:
                    code = "PLUGIN_TIMEOUT" if accepts_context else "PLUGIN_TIMEOUT_NONCOOPERATIVE"
                    kind = "plugin.tool_timed_out"
                    message = "plugin tool invocation exceeded its deadline"
                    severity = "error"
                self._emit(plugin_id, kind, self._invocation_payload(
                    context, code=code, input_size=input_chars,
                ))
                return {
                    "ok": False,
                    "error": message,
                    "diagnostics": [{"code": code, "severity": severity}],
                }

            if not isinstance(result, Mapping):
                raise TypeError("plugin invoke() must return a mapping")
            output = dict(result)
            output_chars = _serialized_char_count(output)
            if output_chars is None:
                return {
                    "ok": False,
                    "error": "plugin tool output must be JSON-serializable",
                    "diagnostics": [{"code": "PLUGIN_OUTPUT_NOT_SERIALIZABLE", "severity": "error"}],
                }
            if output_chars > policy.max_output_chars:
                self._emit(plugin_id, "plugin.tool_rejected", {
                    "tool_name": tool_name,
                    "code": "PLUGIN_OUTPUT_LIMIT",
                    "size": output_chars,
                    "limit": policy.max_output_chars,
                    "run_id": str(run_id),
                    "invocation_id": invocation_id,
                })
                return {
                    "ok": False,
                    "error": "plugin tool output exceeds host limit",
                    "diagnostics": [{
                        "code": "PLUGIN_OUTPUT_LIMIT",
                        "severity": "error",
                        "size": output_chars,
                        "limit": policy.max_output_chars,
                    }],
                }
            self._emit(plugin_id, "plugin.tool_completed", self._invocation_payload(
                context,
                code="PLUGIN_OK",
                input_size=input_chars,
                output_size=output_chars,
            ))
            return output
        except Exception as exc:  # noqa: BLE001 - plugin failures are contained
            message = f"{type(exc).__name__}: {exc}"[:500]
            self._emit(plugin_id, "plugin.tool_failed", self._invocation_payload(
                context, code="PLUGIN_TOOL_EXCEPTION", error=message, input_size=input_chars,
            ))
            return {
                "ok": False,
                "error": message,
                "diagnostics": [{"code": "PLUGIN_TOOL_EXCEPTION", "severity": "error"}],
            }
        finally:
            semaphore.release()

    def providers_for_capability(self, capability_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(
                entry.manifest.plugin_id
                for entry in self._entries.values()
                if entry.state == PluginState.ACTIVE
                and any(capability.capability_id == capability_id for capability in entry.manifest.capabilities)
            ))

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": "topos-plugin-registry/1.0",
            "plugins": [record.to_dict() for record in self.list()],
        }

    def _set_state(self, plugin_id: str, state: PluginState) -> bool:
        with self._lock:
            entry = self._entries.get(str(plugin_id))
            if entry is None or entry.state == PluginState.ACTIVE:
                return False
            if entry.state == state:
                return False
            entry.state = state
        self._emit(str(plugin_id), f"plugin.{state.value}", {})
        return True

    def _mark_failed(self, plugin_id: str, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"[:500]
        with self._lock:
            entry = self._entries.get(str(plugin_id))
            if entry is not None:
                entry.state = PluginState.FAILED
                entry.health = PluginHealth(status="failed", message=message)
                entry.error = message
        self._emit(str(plugin_id), "plugin.failed", {"error": message})

    @staticmethod
    def _read_health(plugin: PluginLifecycle) -> PluginHealth:
        health = plugin.health()
        if not isinstance(health, PluginHealth):
            raise TypeError("plugin.health() must return PluginHealth")
        return health

    def _plugin_emit(self, plugin_id: str) -> Callable[[str, Mapping[str, Any]], None]:
        return lambda kind, payload: self._emit(plugin_id, kind, payload)

    @staticmethod
    def _invocation_payload(
        context: PluginCallContext,
        *,
        code: str = "",
        error: str = "",
        input_size: int | None = None,
        output_size: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_name": context.tool_name,
            "invocation_id": context.invocation_id,
            "run_id": context.run_id,
            "duration_ms": round((time.monotonic() - context.started_at) * 1000.0, 3),
        }
        if code:
            payload["code"] = code
        if error:
            payload["error"] = str(error)[:500]
        if input_size is not None:
            payload["input_size"] = int(input_size)
        if output_size is not None:
            payload["output_size"] = int(output_size)
        return payload

    def _emit(self, plugin_id: str, kind: str, payload: Mapping[str, Any]) -> None:
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(plugin_id, kind, dict(payload))
        except Exception:
            # Observability must never become a plugin execution backdoor.
            return

    @staticmethod
    def _snapshot(entry: _PluginEntry) -> PluginRecord:
        return PluginRecord(
            manifest=entry.manifest,
            state=entry.state,
            health=entry.health,
            policy=entry.policy,
            error=entry.error,
        )


__all__ = [
    "PLUGIN_API_VERSION",
    "PluginCapability",
    "PluginCallContext",
    "PluginContext",
    "PluginExecutionPolicy",
    "PluginHealth",
    "PluginInvocationCancelled",
    "PluginInvocationProcessCrashed",
    "PluginInvocationTimedOut",
    "PluginLifecycle",
    "PluginToolHandler",
    "PluginManifest",
    "PluginRecord",
    "PluginRegistry",
    "PluginSource",
    "PluginState",
]
