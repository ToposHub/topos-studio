"""Unified host-facing plugin service contracts (PL-07).

One contract per domain: native UI, the plugin SDK, HTTP/Web API and any
future MCP adapter are all *adapters* over these services, so permissions,
locking, schema validation, revisions, idempotency and error codes stay
identical no matter which door a caller walks through.

This module is deliberately Qt-free and domain-neutral (paths are strings,
payloads are mappings). Domain DTOs (e.g. ``src/features/files/contracts.py``)
bind to these Protocols inside their feature packages; ``shared`` must not
import features.

Result honesty rules:
- ``accepted`` means the service took the request; it never means a tool
  window appeared or a file was processed — those are per-item/job outcomes
- ``stage`` records the last completed phase; ``retriable`` says whether an
  identical retry is safe
- every mutating result carries the domain ``revision`` it produced, so two
  adapters cannot disagree about what changed
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

STAGES = ("validated", "accepted", "dispatched", "running", "completed", "failed", "cancelled")


@dataclass(frozen=True)
class ItemResult:
    """Per-item outcome inside one service call."""

    path: str
    ok: bool
    error_code: str = ""
    message: str = ""
    result: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "ok": self.ok, "error_code": self.error_code,
                "message": self.message[:500]}


@dataclass(frozen=True)
class ServiceResult:
    """The only shape a command/service call may return."""

    command_id: str
    ok: bool
    accepted: bool
    invocation_id: str = ""
    job_id: str = ""
    stage: str = "validated"
    items: tuple[ItemResult, ...] = ()
    warnings: tuple[str, ...] = ()
    retriable: bool = False
    error_code: str = ""
    message: str = ""
    revision: int | None = None

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"未知阶段：{self.stage}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "ok": self.ok, "accepted": self.accepted,
            "invocation_id": self.invocation_id, "job_id": self.job_id,
            "stage": self.stage, "items": [item.to_dict() for item in self.items],
            "warnings": self.warnings, "retriable": self.retriable,
            "error_code": self.error_code, "message": self.message[:500],
            "revision": self.revision,
        }


def accepted(command_id: str, *, invocation_id: str = "", job_id: str = "",
             stage: str = "accepted", message: str = "",
             revision: int | None = None) -> ServiceResult:
    return ServiceResult(command_id=command_id, ok=True, accepted=True,
                         invocation_id=invocation_id, job_id=job_id,
                         stage=stage, message=message, revision=revision)


def rejected(command_id: str, *, error_code: str, message: str,
             retriable: bool = False, stage: str = "failed",
             invocation_id: str = "") -> ServiceResult:
    return ServiceResult(command_id=command_id, ok=False, accepted=False,
                         invocation_id=invocation_id, stage=stage,
                         retriable=retriable, error_code=error_code, message=message)


class CommandRejected(ValueError):
    """Raised by ``prepare`` when a command cannot run for this selection.

    Subclasses ``ValueError`` so worker-pool call sites that already catch
    ``ValueError`` report these like any other user-facing rejection.
    """

    def __init__(self, error_code: str, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retriable = retriable


@dataclass(frozen=True)
class PreparedInvocation:
    """A validated, frozen request ready for dispatch (GUI/worker split safe)."""

    context: Any                     # lifecycle.InvocationContext (kept Any to avoid import cycle)
    command_id: str
    plugin_id: str
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        frozen = self.payload
        if not isinstance(frozen, Mapping):
            raise ValueError("payload 必须是键值对象")
        object.__setattr__(self, "payload", MappingProxyType(dict(frozen)))


@runtime_checkable
class PluginCommandService(Protocol):
    """The single launch path for every entry point (PL-04/PL-07).

    Toolbar, launcher, context menu, shortcuts and external automation all
    end here with the same frozen selection and the same validation.
    """

    def prepare(self, command_id: str, entry_paths: Iterable[str]) -> PreparedInvocation:
        """Validate applicability and freeze the request; may raise CommandRejected."""
        ...

    def dispatch(self, prepared: PreparedInvocation) -> ServiceResult:
        """Submit the prepared invocation; completion may still be async."""
        ...


@runtime_checkable
class MetadataService(Protocol):
    """Read/patch user metadata with revision discipline (PL-07 / FR-11)."""

    def get_metadata(self, paths: Iterable[str]) -> ServiceResult: ...

    def patch_metadata(self, paths: Iterable[str], patch: Mapping[str, Any], *,
                       tags_op: str = "replace",
                       expected_revision: int | None = None) -> ServiceResult: ...


@runtime_checkable
class CollectionsService(Protocol):
    """Collection/stack organisation queries and mutations."""

    def list_collections(self, library_id: str = "") -> ServiceResult: ...

    def add_members(self, collection_id: str, paths: Iterable[str], *,
                    expected_revision: int | None = None) -> ServiceResult: ...


@runtime_checkable
class ThumbnailService(Protocol):
    """Bounded thumbnail/preview fetches; identity-keyed, cancellable."""

    def request_thumbnail(self, path: str, size: int, *,
                          session_id: str = "") -> ServiceResult: ...

    def cancel_session(self, session_id: str) -> None: ...


@runtime_checkable
class FileTaskService(Protocol):
    """Safe file tasks through the domain entry (FUP-01/PL-07 alignment)."""

    def start_task(self, operation: str, targets: Iterable[str], destination: str,
                   options: Mapping[str, Any]) -> ServiceResult: ...

    def cancel_task(self, job_id: str) -> ServiceResult: ...

    def task_status(self, job_id: str) -> ServiceResult: ...


@runtime_checkable
class JobService(Protocol):
    """Unified job lifecycle: progress, staged results, honest cancellation."""

    def job_status(self, job_id: str) -> ServiceResult: ...

    def request_cancel(self, job_id: str) -> ServiceResult: ...


@runtime_checkable
class EventService(Protocol):
    """Versioned subscriptions with backpressure/coalescing owned by the host."""

    def subscribe(self, topic: str, listener: Callable[[Mapping[str, Any]], None]) -> Callable[[], None]: ...


__all__ = [
    "STAGES", "ItemResult", "ServiceResult", "accepted", "rejected", "CommandRejected",
    "PreparedInvocation", "PluginCommandService", "MetadataService", "CollectionsService",
    "ThumbnailService", "FileTaskService", "JobService", "EventService",
]
