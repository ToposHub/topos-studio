"""Cooperative per-context decode worker budgeting (D3).

The native slice pool is process wide, while decoder contexts can be active in
different playback/export jobs.  A context configured with ``0`` would claim
the whole pool, so two such contexts oversubscribe the machine.  This module
keeps a small process-local registry and fairly rebalances explicit context
budgets whenever a lease is added or removed.

The registry is deliberately cooperative: callers that do not use a lease keep
the native default.  The Topos media source and parallel sequential decoder use
it, and diagnostics expose the active leases so a benchmark can tell whether a
run was fairly budgeted or fell back to an unregistered path.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_MAX_WORKERS = 16
def _default_capacity() -> int:
    raw = os.environ.get("TOPOS_DECODE_WORKER_BUDGET")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return max(1, min(_MAX_WORKERS, value))
    return max(1, min(_MAX_WORKERS, int(os.cpu_count() or 1)))


@dataclass
class _Entry:
    key: str
    requested: int
    role: str
    apply: Callable[[int], None]
    assigned: int = 1


class DecodeWorkerLease:
    """A releasable registration returned by :class:`DecodeWorkerBudget`."""

    def __init__(self, manager: "DecodeWorkerBudget", key: str) -> None:
        self._manager = manager
        self.key = key
        self._released = False

    @property
    def assigned(self) -> int:
        return self._manager.assigned(self.key)

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._manager.release(self.key)

    def __enter__(self) -> "DecodeWorkerLease":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class DecodeWorkerBudget:
    """Fair process-local worker budget for independent decoder contexts."""

    def __init__(self, capacity: Optional[int] = None) -> None:
        self._lock = threading.RLock()
        self._capacity = self._sanitize_capacity(
            _default_capacity() if capacity is None else capacity
        )
        self._entries: dict[str, _Entry] = {}
        # AUD-32104：回调在注册表锁外执行（避免持锁调用用户代码），但
        # "计算 → 应用"必须串行化——世代号在注册表锁内每次变更时递增，
        # apply 侧用独立 apply 锁 + 单调世代比对丢弃后到的过期快照。
        self._generation = 0          # 注册表变更世代（锁内递增）
        self._applied_generation = 0  # 已按序下发的最新世代
        self._apply_lock = threading.Lock()

    @staticmethod
    def _sanitize_capacity(value: int) -> int:
        return max(1, min(_MAX_WORKERS, int(value)))

    @staticmethod
    def _sanitize_requested(value: int) -> int:
        # 0 means no per-context cap and participates in fair sharing.
        return max(0, min(_MAX_WORKERS, int(value)))

    def acquire(
        self,
        key: str,
        *,
        requested: int = 0,
        role: str = "playback",
        capacity: Optional[int] = None,
        apply: Optional[Callable[[int], None]] = None,
    ) -> DecodeWorkerLease:
        """Register a context and apply its initial fair share.

        ``requested=0`` follows the fair share.  A positive value is a hard
        per-context ceiling.  ``apply`` is called once immediately and again
        when another lease changes the allocation; callbacks should only update
        an idle context or a thread-safe native setting.
        """
        if not key:
            raise ValueError("decode worker lease key 不能为空")
        callback = apply or (lambda _workers: None)
        with self._lock:
            if capacity is not None:
                # AUD-32106: the capacity of this process-level singleton must
                # not be pinned to a transiently low value — a source opened
                # while the export policy temporarily lowered the process-wide
                # slice-threads value would otherwise shrink every future fair
                # share until the next capacity-bearing acquire.  Adopt a
                # caller-supplied capacity only in the increasing direction;
                # leases never restore it on release (monotonic growth is
                # benign, a permanent shrink is not).
                candidate = self._sanitize_capacity(capacity)
                if candidate > self._capacity:
                    self._capacity = candidate
            if key in self._entries:
                raise ValueError(f"decode worker lease 已存在: {key}")
            self._entries[key] = _Entry(
                key=key,
                requested=self._sanitize_requested(requested),
                role=str(role or "playback"),
                apply=callback,
            )
            self._generation += 1
            generation = self._generation
            updates = self._rebalance_locked()
            snapshot = self._snapshot_locked()
        self._apply_updates(updates, generation)
        logger.debug("decode worker lease acquired: %s", snapshot)
        return DecodeWorkerLease(self, key)

    def release(self, key: str) -> None:
        with self._lock:
            if key not in self._entries:
                return
            del self._entries[key]
            self._generation += 1
            generation = self._generation
            updates = self._rebalance_locked()
            snapshot = self._snapshot_locked()
        self._apply_updates(updates, generation)
        logger.debug("decode worker lease released: %s", snapshot)

    def assigned(self, key: str) -> int:
        with self._lock:
            entry = self._entries.get(key)
            return int(entry.assigned) if entry is not None else 0

    def bind(self, key: str, apply: Callable[[int], None]) -> None:
        """Attach the live context setter to an already acquired lease."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                raise KeyError(key)
            entry.apply = apply

    def _rebalance_locked(self) -> list[tuple[Callable[[int], None], int, str]]:
        entries = list(self._entries.values())
        if not entries:
            return []
        # Every active context gets at least one worker.  Remaining workers are
        # handed out round-robin in registration order until each requested cap
        # is reached.  This is deterministic and avoids a late export starving
        # an already-running playback context.
        for entry in entries:
            entry.assigned = 1
        remaining = max(0, self._capacity - len(entries))
        cursor = 0
        while remaining:
            progressed = False
            for _ in range(len(entries)):
                entry = entries[cursor]
                cursor = (cursor + 1) % len(entries)
                limit = entry.requested or self._capacity
                if entry.assigned < limit:
                    entry.assigned += 1
                    remaining -= 1
                    progressed = True
                    if remaining == 0:
                        break
            if not progressed:
                break
        # If there are more contexts than workers, the minimum-one rule is an
        # intentional bounded oversubscription; surface it in diagnostics.
        return [(e.apply, e.assigned, e.key) for e in entries]

    def _apply_updates(
        self,
        updates: list[tuple[Callable[[int], None], int, str]],
        generation: int,
    ) -> None:
        """按世代单调顺序下发分配（AUD-32104）。

        并发 acquire/release 的两个快照可能后到先应用：apply 锁串行化
        应用顺序，世代号落后的快照（其计算所基于的登记表状态已被更新
        世代取代）直接丢弃，保证最终下发的一定是最新世代的分配。
        """
        with self._apply_lock:
            if generation < self._applied_generation:
                logger.debug(
                    "decode worker budget: drop stale snapshot "
                    "gen=%d (applied=%d)",
                    generation, self._applied_generation,
                )
                return
            for callback, workers, key in updates:
                try:
                    callback(workers)
                except Exception:  # noqa: BLE001 - a bad callback must not poison registry
                    logger.warning(
                        "decode worker lease callback failed: key=%s workers=%d",
                        key, workers, exc_info=True,
                    )
            self._applied_generation = generation

    def _snapshot_locked(self) -> Dict[str, Any]:
        assigned = sum(entry.assigned for entry in self._entries.values())
        return {
            "capacity": self._capacity,
            "active": len(self._entries),
            "assigned_total": assigned,
            "oversubscribed": assigned > self._capacity,
            "leases": [
                {
                    "key": entry.key,
                    "role": entry.role,
                    "requested": entry.requested,
                    "assigned": entry.assigned,
                }
                for entry in self._entries.values()
            ],
        }

    def describe(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def reset_for_test(self) -> None:
        with self._lock:
            self._entries.clear()
            # 世代继续递增（不归零）：使 reset 前仍在途的旧快照作废
            self._generation += 1


DECODE_WORKER_BUDGET = DecodeWorkerBudget()
