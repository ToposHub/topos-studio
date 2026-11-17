"""Invocation context, contribution ownership and lifecycle rules (PL-03).

Qt-free and host-agnostic: any host (Files window host, AI runtime, future
color/plugin processes) adopts the same semantics.

Core rules from the plan:
- ``InvocationContext`` is immutable; the entry selection is frozen at the
  user's gesture and re-validated before execution (stale selections fail
  loudly, multi-select is never silently narrowed to the first item)
- activation is all-or-nothing: a ``setup()`` that fails halfway leaves no
  residual contributions (rollback releases every registered handle; a
  failing ``release()`` never aborts the remaining ones)
- deactivation forbids new calls first, then cancels/waits *that plugin's*
  jobs, closes windows, unbinds events, releases handles; it is idempotent,
  resumable, and never frees a plugin's contributions while its jobs are
  still terminating or a cleanup step keeps failing (PR-02)
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

from src.shared.plugins.freeze import deep_freeze
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
        if isinstance(self.entry_paths, (str, bytes)):
            raise ValueError("entry_paths 必须是路径序列")
        entry_paths = tuple(self.entry_paths)
        if any(not isinstance(path, str) for path in entry_paths):
            raise ValueError("entry_paths 必须只包含字符串路径")
        object.__setattr__(self, "entry_paths", entry_paths)
        frozen = self.params
        if not isinstance(frozen, Mapping):
            raise ValueError("params 必须是键值对象")
        # 深冻结（PR-04）：嵌套 dict/list 不再与调用方共享可变结构
        object.__setattr__(self, "params", deep_freeze(frozen, strict_keys=True))

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


@dataclass
class _FunctionHandle:
    release_callback: Callable[[], None]
    _lock: Any = field(default_factory=threading.RLock, init=False, repr=False)
    _released: bool = field(default=False, init=False)
    _releasing: bool = field(default=False, init=False)

    def release(self) -> None:
        with self._lock:
            if self._released or self._releasing:
                return
            self._releasing = True
            try:
                self.release_callback()
                self._released = True
            finally:
                self._releasing = False


class ContributionSet:
    """All-or-nothing registration: ``rollback()`` releases everything.

    单个 ``release()`` 抛错不会中止其余释放（PR-02）：失败句柄保留在
    本集合内，用 ``retry_failed_releases()`` 重试——注册所有权不提前移除。
    """

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        self._handles: list[tuple[str, str, ContributionHandle]] = []
        self._keys: set[tuple[str, str]] = set()
        self._failures: list[tuple[str, str, ContributionHandle, str]] = []
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
        self._handles.append((kind, key, handle))
        return handle

    def commit(self) -> None:
        if self._released:
            raise RuntimeError("贡献集已释放，不能提交")
        self._committed = True

    def rollback(self) -> int:
        """Release every pending handle in reverse order; returns released count."""
        released = 0
        while self._handles:
            kind, key, handle = self._handles.pop()
            try:
                handle.release()
                released += 1
            except BaseException as exc:  # noqa: BLE001 - 清理失败必须继续释放其余句柄
                self._failures.append((kind, key, handle, f"{type(exc).__name__}: {exc}"))
        self._released = True
        return released

    def retry_failed_releases(self) -> int:
        """Retry handles whose release previously failed; returns freed count."""
        retried = 0
        remaining: list[tuple[str, str, ContributionHandle, str]] = []
        for kind, key, handle, _error in self._failures:
            try:
                handle.release()
                retried += 1
            except BaseException as exc:  # noqa: BLE001
                remaining.append((kind, key, handle, f"{type(exc).__name__}: {exc}"))
        self._failures = remaining
        return retried

    @property
    def release_failures(self) -> tuple[str, ...]:
        return tuple(f"{kind}:{key} {error}" for kind, key, _handle, error in self._failures)

    @property
    def size(self) -> int:
        return len(self._handles) + len(self._failures)   # 尚未成功释放的句柄

    @property
    def committed(self) -> bool:
        return self._committed


class PluginJobs:
    """Job bookkeeping with honest cancellation semantics (PL-03/06).

    任务记录归属插件（PR-02）：停用插件 A 只取消 A 自己的任务，
    ``cancel_all`` 仅供宿主整体关闭时使用。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel_requested: set[str] = set()
        self._cancelled_confirmed: set[str] = set()
        self._finished: set[str] = set()
        self._stopping_owners: set[str] = set()

    def start(self, job_id: str, cancel: Callable[[], None] | None = None,
              *, plugin_id: str = "") -> None:
        with self._lock:
            if plugin_id and plugin_id in self._stopping_owners:
                raise RuntimeError(f"插件已开始停用，不能登记新任务：{plugin_id}")
            if job_id in self._jobs:
                raise ValueError(f"任务已存在：{job_id}")
            self._jobs[job_id] = {"cancel": cancel, "state": "running",
                                  "plugin_id": plugin_id}

    def owner(self, job_id: str) -> str:
        with self._lock:
            job = self._jobs.get(job_id)
            return str(job["plugin_id"]) if job else ""

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
            try:
                cancel()
            except Exception as exc:
                with self._lock:
                    # 请求未送达时保留可重试状态；worker 已确认的终态不可回退。
                    if job["state"] == "cancel_requested":
                        job["state"] = "running"
                        self._cancel_requested.discard(job_id)
                        job["cancel_error"] = f"{type(exc).__name__}: {exc}"
                raise
        with self._lock:
            job.pop("cancel_error", None)
        return True

    def stop_owner(self, plugin_id: str) -> None:
        """Atomically forbid new owned jobs before taking a cancellation snapshot."""
        with self._lock:
            self._stopping_owners.add(plugin_id)

    def cancel_errors(self, plugin_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(f"cancel {job_id}: {job['cancel_error']}"
                         for job_id, job in self._jobs.items()
                         if job["plugin_id"] == plugin_id and job.get("cancel_error")
                         and job["state"] in ("running", "cancel_requested"))

    def confirm_cancelled(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job["state"] == "cancel_requested":
                self._cancelled_confirmed.add(job_id)
                job["state"] = "cancelled"

    def finish(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job["state"] in ("running", "cancel_requested"):
                job["state"] = "finished"
                self._finished.add(job_id)

    def state(self, job_id: str) -> str:
        with self._lock:
            job = self._jobs.get(job_id)
            return job["state"] if job else "unknown"

    def active(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(job_id for job_id, job in self._jobs.items()
                         if job["state"] in ("running", "cancel_requested"))

    def active_for(self, plugin_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(job_id for job_id, job in self._jobs.items()
                         if job["state"] in ("running", "cancel_requested")
                         and job["plugin_id"] == plugin_id)

    def cancel_all(self) -> int:
        """宿主整体关闭：取消所有仍在跑的任务，与单插件停用无关。"""
        return self._cancel_many(self.active())

    def cancel_owned(self, plugin_id: str) -> int:
        """只取消归属 ``plugin_id`` 的任务（PR-02：不跨插件取消）。"""
        return self._cancel_many(self.active_for(plugin_id))

    def _cancel_many(self, job_ids: Iterable[str]) -> int:
        requested = 0
        for job_id in job_ids:
            try:
                requested += int(self.request_cancel(job_id))
            except Exception:
                # request_cancel 已记录诊断并恢复可重试状态，继续取消独立任务。
                continue
        return requested


class PluginLifecycleController:
    """Per-plugin lifecycle with rollback and ordered shutdown (PL-03/PR-02).

    The host supplies platform callbacks (close windows, unbind events);
    this controller owns the ordering and the idempotency guarantees.

    停用状态机（PR-02）：active → stopping → stopped。贡献所有权在停用
    全部完成前保留在本控制器（重试不丢失所有权）；任务未确认终止、或
    任一清理步骤失败时，``fully_stopped`` 为 False，再次调用 ``deactivate``
    从断点继续。GUI 线程不应等待时传 ``wait_jobs=False`` 轮询重入。
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
        self._stopped: set[str] = set()     # 停用流程已启动（不可再激活）
        self._fully_stopped: set[str] = set()
        self._deactivating: set[str] = set()
        self._activating: set[str] = set()
        self._cleanup_completed: dict[str, set[str]] = {}

    # -- activation -----------------------------------------------------
    def activate(self, plugin_id: str, registrar: Callable[[ContributionSet], None]) -> ContributionSet:
        """Run the plugin's registration; any exception rolls back fully."""
        with self._lock:
            if plugin_id in self._contributions or plugin_id in self._activating:
                raise RuntimeError(f"插件已激活：{plugin_id}")
            if plugin_id in self._stopped:
                raise RuntimeError(f"插件已停用，不能重复激活：{plugin_id}")
            contributions = ContributionSet(plugin_id)
            self._activating.add(plugin_id)
            try:
                registrar(contributions)
                contributions.commit()
            except BaseException as exc:
                if self._jobs.active_for(plugin_id):
                    # setup may already have started a background task. Its
                    # registrations remain owned until termination is confirmed.
                    self._jobs.stop_owner(plugin_id)
                    self._jobs.cancel_owned(plugin_id)
                    self._contributions[plugin_id] = contributions
                    self._stopped.add(plugin_id)
                    raise RuntimeError(
                        "插件激活失败且已启动任务，待任务终止后调用 deactivate 清理") from exc
                contributions.rollback()
                if contributions.release_failures:
                    # 回滚本身有残留：保留所有权，deactivate(plugin_id) 可重试清理
                    self._contributions[plugin_id] = contributions
                    self._stopped.add(plugin_id)
                    raise RuntimeError(
                        f"插件激活失败，且回滚清理未完成（可调用 deactivate 重试清理）："
                        f"{'; '.join(contributions.release_failures)}") from exc
                raise
            finally:
                self._activating.discard(plugin_id)
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
        """Forbid new calls → cancel own jobs → wait → close windows → unbind → release.

        Idempotent and resumable; reports what happened so the UI never prints
        a fake "已停止" while a job is still terminating or a cleanup step
        failed. 不跨插件取消任务（PR-02）。
        """
        with self._lock:
            if plugin_id in self._deactivating or plugin_id in self._activating:
                return {"first_stop": False, "released_handles": 0,
                        "jobs_cancel_requested": 0,
                        "jobs_still_terminating": self._jobs.active_for(plugin_id),
                        "cleanup_failures": (), "fully_stopped": False,
                        "cleanup_in_progress": True}
            self._deactivating.add(plugin_id)
        try:
            return self._deactivate(plugin_id, wait_jobs=wait_jobs)
        finally:
            with self._lock:
                self._deactivating.discard(plugin_id)

    def _deactivate(self, plugin_id: str, *, wait_jobs: bool) -> dict[str, Any]:
        with self._lock:
            first_stop = plugin_id not in self._stopped
            self._stopped.add(plugin_id)
            self._accepting.discard(plugin_id)
            contributions = self._contributions.get(plugin_id)
            if plugin_id in self._fully_stopped:
                return {"first_stop": False, "released_handles": 0,
                        "jobs_cancel_requested": 0, "jobs_still_terminating": (),
                        "cleanup_failures": (), "fully_stopped": True}
        self._jobs.stop_owner(plugin_id)
        requested = self._jobs.cancel_owned(plugin_id)
        active_before = self._jobs.active_for(plugin_id)
        if wait_jobs and active_before:
            deadline = time.monotonic() + self._job_wait_timeout
            while time.monotonic() < deadline and self._jobs.active_for(plugin_id):
                time.sleep(0.01)
        still_active = self._jobs.active_for(plugin_id)
        if still_active:
            # 任务未确认终止：不关窗/不释放贡献，如实报告（所有权保留供重试）
            return {"first_stop": first_stop, "released_handles": 0,
                    "jobs_cancel_requested": requested,
                    "jobs_still_terminating": tuple(still_active),
                    "cleanup_failures": self._jobs.cancel_errors(plugin_id), "fully_stopped": False}
        cleanup_failures: list[str] = []
        completed = self._cleanup_completed.setdefault(plugin_id, set())
        for label, callback in (("close_windows", self._close_windows),
                                ("unbind_events", self._unbind_events)):
            if label in completed:
                continue
            try:
                callback(plugin_id)
                completed.add(label)
            except BaseException as exc:  # noqa: BLE001 - 单步失败不中止其余清理
                cleanup_failures.append(f"{label}: {type(exc).__name__}: {exc}")
        released = 0
        if contributions is not None:
            released = contributions.retry_failed_releases() + contributions.rollback()
            cleanup_failures.extend(f"release {item}" for item in contributions.release_failures)
        with self._lock:
            if cleanup_failures or self._jobs.active_for(plugin_id):
                # 清理有失败或迟到任务：保留 _contributions 供下一次 deactivate 重试
                self._fully_stopped.discard(plugin_id)
                return {"first_stop": first_stop, "released_handles": released,
                        "jobs_cancel_requested": requested,
                        "jobs_still_terminating": tuple(self._jobs.active_for(plugin_id)),
                        "cleanup_failures": tuple(cleanup_failures),
                        "fully_stopped": False}
            self._contributions.pop(plugin_id, None)
            self._fully_stopped.add(plugin_id)
        return {"first_stop": first_stop, "released_handles": released,
                "jobs_cancel_requested": requested,
                "jobs_still_terminating": (), "cleanup_failures": (),
                "fully_stopped": True}

    def is_stopped(self, plugin_id: str) -> bool:
        with self._lock:
            return plugin_id in self._fully_stopped


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
