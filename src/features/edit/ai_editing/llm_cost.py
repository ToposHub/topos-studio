"""Small, provider-neutral cost ledger for LLM calls (commercial plan P1)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class CostRecord:
    request_id: str
    provider_kind: str
    model_id: str
    request_type: str
    total_tokens: int | None = None
    cost_units: float | None = None
    estimated: bool = False
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "provider_kind": self.provider_kind,
            "model_id": self.model_id,
            "request_type": self.request_type,
            "total_tokens": self.total_tokens,
            "cost_units": self.cost_units,
            "estimated": self.estimated,
            "created_at": self.created_at,
        }


class CostLedger:
    """Thread-safe in-memory ledger; callers may persist redacted records.

    预算语义（AUD-13707）：``can_spend`` + ``record`` 是两次独立加锁，组合使用
    属 **best-effort** 预算（并发下可小幅超支，最多约一个请求的量）。需要硬预算
    的调用方应使用 :meth:`try_reserve`——在单次持锁内完成"检查 + 记账"，保证
    记账后的总额不会超过 limit。
    """

    def __init__(self) -> None:
        self._records: list[CostRecord] = []
        self._lock = RLock()

    def record(self, record: CostRecord) -> None:
        with self._lock:
            self._records.append(record)

    def records(self) -> tuple[CostRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def total_cost(self) -> float:
        with self._lock:
            return sum(record.cost_units or 0.0 for record in self._records)

    def can_spend(self, limit: float | None) -> bool:
        return limit is None or self.total_cost() < limit

    def try_reserve(self, record: CostRecord, limit: float | None) -> bool:
        """单锁内原子完成预算检查 + 记账（AUD-13707）。

        Args:
            record: 本次开销记录（记账语义与 :meth:`record` 完全一致）。
            limit: 预算上限；``None`` 表示不设限（总是允许并记账）。

        Returns:
            ``True`` 表示记录已入账；``False`` 表示该记录会使总额超过 *limit*，
            未入账。并发调用下绝不会因 check-then-act 竞态而超支。
        """
        amount = float(record.cost_units or 0.0)
        with self._lock:
            if limit is not None:
                total = sum(r.cost_units or 0.0 for r in self._records)
                if total + amount > limit:
                    return False
            self._records.append(record)
            return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_cost_units": self.total_cost(),
            "records": [record.to_dict() for record in self.records()],
        }


def make_cost_record(
    *,
    request_id: str,
    provider_kind: str,
    model_id: str,
    request_type: str,
    total_tokens: int | None,
    cost_units: float | None,
    estimated: bool = False,
) -> CostRecord:
    return CostRecord(
        request_id=request_id,
        provider_kind=provider_kind,
        model_id=model_id,
        request_type=request_type,
        total_tokens=total_tokens,
        cost_units=cost_units,
        estimated=estimated,
        created_at=time.time(),
    )


__all__ = ["CostLedger", "CostRecord", "make_cost_record"]
