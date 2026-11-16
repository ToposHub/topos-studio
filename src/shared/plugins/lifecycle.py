"""Invocation context, contribution ownership and lifecycle rules (PL-03).

Qt-free and host-agnostic: any host (Files window host, AI runtime, future
color/plugin processes) adopts the same semantics.

Core rules from the plan:
- ``InvocationContext`` is immutable; the entry selection is frozen at the
  user's gesture and re-validated before execution (stale selections fail
  loudly, multi-select is never silently narrowed to the first item)
- activation is all-or-nothing: a ``setup()`` that fails halfway leaves no
  residual contributions (rollback releases every registered handle)
- deactivation forbids new calls first, then cancels/waits jobs, closes
  windows, unbinds events, releases handles; it is idempotent
- cancellation is a *request*; "cancelled" is only published after the job
  confirms termination
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol

from src.shared.plugins.manifest import PluginCapability


class InvocationCancelled(Exception):
    """Raised inside handlers when the invocation's cancel token is set."""


@dataclass(frozen=True)
class CancelToken:
    """Cooperative cancellation: request() is a ask, ``cancelled`` is fact."""

    _event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def request(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise InvocationCancelled("调用已被请求取消")


@dataclass(frozen=True)
class InvocationContext:
    """Everything one command invocation may know; built by the host only."""

    invocation_id: str
    command_id: str
    plugin_id: str
    library_id: str = ""
    location_kind: str = "directory"
    location_key: str = ""
    entry_paths: tuple[str, ...] = ()   # frozen at the user's gesture
    entry_revision: int = 0
    host_revision: int = 0
    deadline: float | None = None
    cancel_token: CancelToken = field(default_factory=CancelToken, repr=False)
    authorization: Any = None           # host-issued grant handle, never a secret
    params: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        frozen = self.params
        if not isinstance(frozen, Mapping):
            raise ValueError("params 必须是键值对象")
        object.__setattr__(self, "params", MappingProxyType(dict(frozen)))

    @property
    def cancelled(self) -> bool:
        return self.cancel_token.cancelled

    def raise_if_cancelled(self) -> None:
        self.cancel_token.raise_if_cancelled()

    def timed_out(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def selection_valid(self, current_entries: Iterable[str]) -> bool:
        """Re-validate the frozen selection against the host's current view."""
        current = set(current_entries)
        return all(path in current for path in self.entry_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id, "command_id": self.command_id,
            "plugin_id": self.plugin_id, "library_id": self.library_id,
            "location_kind": self.location_kind, "location_key": self.location_key,
            "entry_paths": self.entry_paths, "entry_revision": self.entry_revision,
            "host_revision": self.host_revision,
        }


def make_invocation(command_id: str, plugin_id: str, **kwargs: Any) -> InvocationContext:
    """Host-side factory: generates the unique invocation id."""
    return InvocationContext(invocation_id=uuid.uuid4().hex, command_id=command_id,
                             plugin_id=plugin_id, **kwargs)


# ---------------------------------------------------------------------------
# Contribution ownership (activation rollback / deactivation order)
# ---------------------------------------------------------------------------


class ContributionHandle(Protocol):
    """An owned registration that can be released exactly once."""

    def release(self) -> None: ...


@dataclass(frozen=True)
class _FunctionHandle:
    release_callback: Callable[[], None]

    def release(self) -> None:
        self.release_callback()


class ContributionSet:
    """All-or-nothing registration: ``rollback()`` releases everything."""

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        self._handles: list[ContributionHandle] = []
        self._keys: set[tuple[str, str]] = set()
        self._committed = False
        self._released = False

    def register(self, kind: str, key: str, release: Callable[[], None]) -> ContributionHandle:
        """Register one contribution; duplicate kind+key is an error."""
        if self._committed or self._released:
            raise RuntimeError("贡献集已提交或已释放，不能再注册")
        if (kind, key) in self._keys:
            raise ValueError(f"贡献重复注册：{kind}:{key}")
        self._keys.add((kind, key))
        handle = _FunctionHandle(release)
        self._handles.append(handle)
        return handle

    def commit(self) -> None:
        if self._released:
            raise RuntimeError("贡献集已释放，不能提交")
        self._committed = True

    def rollback(self) -> int:
        """Release every handle in reverse order; returns released count."""
        released = 0
        while self._handles:
            handle = self._handles.pop()
            handle.release()
            released += 1
        self._released = True
        return released

    @property
    def size(self) -> int:
        return len(self._handles)

    @property
    def committed(self) -> bool:
        return self._committed


class PluginJobs:
    """Job bookkeeping with honest cancellation semantics (PL-03/06)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel_requested: set[str] = set()
        self._cancelled_confirmed: set[str] = set()
        self._finished: set[str] = set()

    def start(self, job_id: str, cancel: Callable[[], None] | None = None) -> None:
        with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"任务已存在：{job_id}")
            self._jobs[job_id] = {"cancel": cancel, "state": "running"}

    def request_cancel(self, job_id: str) -> bool:
        """Cancellation is a request; completion is confirmed by the worker."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["state"] != "running":
                return False
            self._cancel_requested.add(job_id)
            job["state"] = "cancel_requested"
            cancel = job.get("cancel")
        if cancel is not None:
            cancel()
        return True

    def confirm_cancelled(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._cancel_requested:
                self._cancelled_confirmed.add(job_id)
                self._jobs[job_id]["state"] = "cancelled"

    def finish(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["state"] = "finished"
                self._finished.add(job_id)

    def state(self, job_id: str) -> str:
        with self._lock:
            job = self._jobs.get(job_id)
            return job["state"] if job else "unknown"

    def active(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(job_id for job_id, job in self._jobs.items()
                         if job["state"] in ("running", "cancel_requested"))

    def cancel_all(self) -> int:
        return sum(1 for job_id in list(self.active()) if self.request_cancel(job_id))


class PluginLifecycleController:
    """Per-plugin lifecycle with rollback and ordered shutdown (PL-03).

    The host supplies platform callbacks (close windows, unbind events);
    this controller owns the ordering and the idempotency guarantees.
    """

    def __init__(self, *, close_windows: Callable[[str], None] | None = None,
                 unbind_events: Callable[[str], None] | None = None,
                 job_wait_timeout: float = 2.0) -> None:
        self._close_windows = close_windows or (lambda plugin_id: None)
        self._unbind_events = unbind_events or (lambda plugin_id: None)
        self._job_wait_timeout = job_wait_timeout
        self._lock = threading.RLock()
        self._contributions: dict[str, ContributionSet] = {}
        self._jobs = PluginJobs()
        self._accepting: set[str] = set()   # plugins accepting new invocations
        self._stopped: set[str] = set()

    # -- activation -----------------------------------------------------
    def activate(self, plugin_id: str, registrar: Callable[[ContributionSet], None]) -> ContributionSet:
        """Run the plugin's registration; any exception rolls back fully."""
        with self._lock:
            if plugin_id in self._contributions:
                raise RuntimeError(f"插件已激活：{plugin_id}")
            if plugin_id in self._stopped:
                raise RuntimeError(f"插件已停用，不能重复激活：{plugin_id}")
            contributions = ContributionSet(plugin_id)
            try:
                registrar(contributions)
                contributions.commit()
            except BaseException:
                contributions.rollback()
                raise
            self._contributions[plugin_id] = contributions
            self._accepting.add(plugin_id)
            return contributions

    def accepting_calls(self, plugin_id: str) -> bool:
        with self._lock:
            return plugin_id in self._accepting

    # -- jobs -----------------------------------------------------------
    @property
    def jobs(self) -> PluginJobs:
        return self._jobs

    # -- deactivation ---------------------------------------------------
    def deactivate(self, plugin_id: str, *, wait_jobs: bool = True) -> dict[str, Any]:
        """Forbid new calls → cancel jobs → close windows → unbind → release.

        Idempotent; reports what happened so the UI never prints a fake
        "已停止" while a job is still terminating.
        """
        with self._lock:
            self._accepting.discard(plugin_id)
            contributions = self._contributions.pop(plugin_id, None)
            first_stop = plugin_id not in self._stopped
            self._stopped.add(plugin_id)
        active_before = self._jobs.active()
        self._jobs.cancel_all()
        if wait_jobs and active_before:
            deadline = time.monotonic() + self._job_wait_timeout
            while time.monotonic() < deadline and self._jobs.active():
                time.sleep(0.01)
        still_active = self._jobs.active()
        self._close_windows(plugin_id)
        self._unbind_events(plugin_id)
        released = contributions.rollback() if contributions is not None else 0
        return {
            "first_stop": first_stop,
            "released_handles": released,
            "jobs_cancel_requested": len(active_before),
            "jobs_still_terminating": tuple(still_active),
            "fully_stopped": not still_active,
        }

    def is_stopped(self, plugin_id: str) -> bool:
        with self._lock:
            return plugin_id in self._stopped


def check_command_applicability(capability: PluginCapability, *,
                                selected_kinds: tuple[str, ...],
                                ) -> tuple[bool, str]:
    """Unified applicability matching for every entry point (PL-04)."""
    mode = capability.selection_mode
    count = len(selected_kinds)
    if mode == "none" and count:
        return False, "该命令不需要选中素材"
    if mode == "single" and count != 1:
        return False, "该命令需要一个选中素材"
    if mode == "many" and count < 1:
        return False, "该命令需要至少一个选中素材"
    if capability.applies_to_kinds:
        mismatched = [kind for kind in selected_kinds if kind not in capability.applies_to_kinds]
        if mismatched:
            return False, f"不支持的素材类型：{sorted(set(mismatched))}"
    return True, ""


__all__ = [
    "InvocationCancelled", "CancelToken", "InvocationContext", "make_invocation",
    "ContributionHandle", "ContributionSet", "PluginJobs", "PluginLifecycleController",
    "check_command_applicability",
]
