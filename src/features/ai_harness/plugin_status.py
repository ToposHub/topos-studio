"""Qt-free plugin status projections for cards, settings and the Run Drawer.

The registry remains the source of truth.  This module only turns immutable
plugin records and bounded lifecycle events into a UI-safe view model, so a
Qt widget never needs to reach into plugin implementations or private state.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable

from src.features.ai_harness.invocation_models import sanitize_for_storage
from src.features.ai_harness.plugin_runtime import (
    PLUGIN_API_VERSION,
    PluginRecord,
    PluginRegistry,
    PluginState,
)


_FAILURE_EVENT_KINDS = frozenset({
    "plugin.failed",
    "plugin.tool_failed",
    "plugin.tool_rejected",
    "plugin.tool_cancelled",
    "plugin.tool_timed_out",
})


@dataclass(frozen=True)
class PluginEvent:
    """A bounded, secret-free lifecycle event suitable for diagnostics UI."""

    sequence: int
    plugin_id: str
    kind: str
    timestamp: float
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "plugin_id": self.plugin_id,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "payload": sanitize_for_storage(dict(self.payload)),
        }


class PluginEventJournal:
    """Small thread-safe event buffer that can be passed to ``PluginRegistry``."""

    def __init__(self, *, max_events: int = 200) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self._events: deque[PluginEvent] = deque(maxlen=max_events)
        self._next_sequence = 0
        self._subscribers: list[Callable[[PluginEvent], None]] = []
        self._lock = threading.RLock()

    def subscribe(self, subscriber: Callable[[PluginEvent], None]) -> Callable[[], None]:
        """Subscribe to bounded events without making observers a failure path."""

        with self._lock:
            self._subscribers.append(subscriber)

        def _unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return _unsubscribe

    def record(
        self,
        plugin_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        safe_payload = sanitize_for_storage(dict(payload or {}))
        with self._lock:
            event = PluginEvent(
                sequence=self._next_sequence,
                plugin_id=str(plugin_id),
                kind=str(kind),
                timestamp=time.time(),
                payload=safe_payload,
            )
            self._events.append(event)
            self._next_sequence += 1
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:
                # A trace/UI observer must never affect plugin execution.
                continue

    def recent(
        self,
        *,
        plugin_id: str = "",
        run_id: str = "",
        limit: int = 20,
    ) -> tuple[PluginEvent, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            events = tuple(reversed(self._events))
        if plugin_id:
            events = tuple(event for event in events if event.plugin_id == str(plugin_id))
        if run_id:
            events = tuple(
                event for event in events
                if str(event.payload.get("run_id", "")) == str(run_id)
            )
        return events[:limit]

    def latest_failure(self, plugin_id: str) -> PluginEvent | None:
        return next((event for event in self.recent(plugin_id=plugin_id) if event.kind in _FAILURE_EVENT_KINDS), None)


@dataclass(frozen=True)
class PluginCapabilityView:
    capability_id: str
    description: str
    permission: str
    requires_approval: bool
    tool_names: tuple[str, ...]
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "description": self.description,
            "permission": self.permission,
            "requires_approval": self.requires_approval,
            "tool_names": list(self.tool_names),
            "input_schema": sanitize_for_storage(dict(self.input_schema)),
            "output_schema": sanitize_for_storage(dict(self.output_schema)),
        }


@dataclass(frozen=True)
class PluginStatusDiagnostic:
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message[:500],
        }


@dataclass(frozen=True)
class PluginStatusView:
    """Stable view model consumed by plugin cards and run details."""

    plugin_id: str
    display_name: str
    version: str
    api_version: str
    api_compatible: bool
    source: str
    unverified: bool
    content_hash: str
    state: str
    health_status: str
    health_message: str
    isolation_mode: str = "in_process"
    sandbox_mode: str = "report"
    last_failure: str = ""
    last_failure_kind: str = ""
    capability_count: int = 0
    permissions: tuple[str, ...] = ()
    requires_approval: bool = False
    capabilities: tuple[PluginCapabilityView, ...] = ()
    can_enable: bool = False
    can_activate: bool = False
    can_deactivate: bool = False
    diagnostics: tuple[PluginStatusDiagnostic, ...] = ()

    @property
    def status_level(self) -> str:
        if any(item.severity == "error" for item in self.diagnostics):
            return "error"
        if any(item.severity == "warning" for item in self.diagnostics):
            return "warning"
        return "ok"

    @property
    def runnable(self) -> bool:
        return self.state == PluginState.ACTIVE.value and self.api_compatible

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "display_name": self.display_name,
            "version": self.version,
            "api_version": self.api_version,
            "api_compatible": self.api_compatible,
            "source": self.source,
            "unverified": self.unverified,
            "content_hash": self.content_hash,
            "isolation_mode": self.isolation_mode,
            "sandbox_mode": self.sandbox_mode,
            "state": self.state,
            "health_status": self.health_status,
            "health_message": self.health_message,
            "last_failure": self.last_failure,
            "last_failure_kind": self.last_failure_kind,
            "capability_count": self.capability_count,
            "permissions": list(self.permissions),
            "requires_approval": self.requires_approval,
            "capabilities": [item.to_dict() for item in self.capabilities],
            "can_enable": self.can_enable,
            "can_activate": self.can_activate,
            "can_deactivate": self.can_deactivate,
            "status_level": self.status_level,
            "runnable": self.runnable,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


class PluginStatusProjection:
    """Build deterministic status rows without importing Qt or UI widgets."""

    @classmethod
    def project(
        cls,
        registry: PluginRegistry,
        *,
        event_journal: PluginEventJournal | None = None,
        host_api_version: str = PLUGIN_API_VERSION,
        include_disabled: bool = True,
    ) -> tuple[PluginStatusView, ...]:
        return tuple(
            cls._from_record(
                record,
                event_journal=event_journal,
                host_api_version=host_api_version,
            )
            for record in registry.list(include_disabled=include_disabled)
        )

    @classmethod
    def one(
        cls,
        registry: PluginRegistry,
        plugin_id: str,
        *,
        event_journal: PluginEventJournal | None = None,
        host_api_version: str = PLUGIN_API_VERSION,
    ) -> PluginStatusView | None:
        record = registry.get(plugin_id)
        if record is None:
            return None
        return cls._from_record(
            record,
            event_journal=event_journal,
            host_api_version=host_api_version,
        )

    @staticmethod
    def _from_record(
        record: PluginRecord,
        *,
        event_journal: PluginEventJournal | None,
        host_api_version: str,
    ) -> PluginStatusView:
        manifest = record.manifest
        api_compatible = manifest.api_version == str(host_api_version)
        latest_failure = event_journal.latest_failure(manifest.plugin_id) if event_journal else None
        failure_message = ""
        failure_kind = ""
        if latest_failure is not None:
            failure_kind = latest_failure.kind
            failure_message = str(
                latest_failure.payload.get("error")
                or latest_failure.payload.get("reason")
                or latest_failure.payload.get("code")
                or latest_failure.kind
            )[:500]
        elif record.error or record.health.status in {"failed", "degraded"}:
            failure_message = str(record.error or record.health.message)[:500]
            failure_kind = "plugin.health"

        diagnostics: list[PluginStatusDiagnostic] = []
        if not api_compatible:
            diagnostics.append(PluginStatusDiagnostic(
                "PLUGIN_API_INCOMPATIBLE",
                "error",
                f"plugin api {manifest.api_version} is incompatible with host {host_api_version}",
            ))
        if record.state == PluginState.FAILED:
            diagnostics.append(PluginStatusDiagnostic(
                "PLUGIN_FAILED", "error", failure_message or "plugin is failed",
            ))
        elif record.state == PluginState.DEGRADED:
            diagnostics.append(PluginStatusDiagnostic(
                "PLUGIN_DEGRADED", "warning", failure_message or "plugin is degraded",
            ))
        elif record.state == PluginState.DISABLED:
            diagnostics.append(PluginStatusDiagnostic(
                "PLUGIN_DISABLED", "info", "plugin is disabled",
            ))
        if manifest.unverified:
            diagnostics.append(PluginStatusDiagnostic(
                "PLUGIN_UNVERIFIED_SOURCE", "warning", "local development plugin is unverified",
            ))

        capabilities = tuple(
            PluginCapabilityView(
                capability_id=item.capability_id,
                description=item.description,
                permission=item.permission,
                requires_approval=item.requires_approval,
                tool_names=item.tool_names,
                input_schema=item.input_schema,
                output_schema=item.output_schema,
            )
            for item in manifest.capabilities
        )
        permissions = tuple(sorted({item.permission for item in manifest.capabilities}))
        return PluginStatusView(
            plugin_id=manifest.plugin_id,
            display_name=manifest.display_name,
            version=manifest.version,
            api_version=manifest.api_version,
            api_compatible=api_compatible,
            source=manifest.source.value,
            unverified=manifest.unverified,
            content_hash=manifest.content_hash,
            isolation_mode=record.policy.isolation_mode,
            sandbox_mode=record.policy.sandbox_mode,
            state=record.state.value,
            health_status=record.health.status,
            health_message=record.health.message[:500],
            last_failure=failure_message,
            last_failure_kind=failure_kind,
            capability_count=len(capabilities),
            permissions=permissions,
            requires_approval=any(item.requires_approval for item in capabilities),
            capabilities=capabilities,
            can_enable=record.state == PluginState.DISABLED,
            can_activate=(
                api_compatible
                and record.state in {PluginState.ENABLED, PluginState.DEGRADED}
            ),
            can_deactivate=record.state == PluginState.ACTIVE,
            diagnostics=tuple(diagnostics),
        )


__all__ = [
    "PluginCapabilityView",
    "PluginEvent",
    "PluginEventJournal",
    "PluginStatusDiagnostic",
    "PluginStatusProjection",
    "PluginStatusView",
]
