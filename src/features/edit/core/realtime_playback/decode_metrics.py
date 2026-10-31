"""M16.2 · 解码管理器分域指标（锁等待/解码时长/缓存命中——离屏口径）。

M16 验收要求"分出锁等待/重复解码/命中率"。真实窗口矩阵的裁决性
数据需安静机（deferred_gates 在案）；本模块提供**逐域计数器**供
离屏 harness/测试与后续真实窗口复测同一口径采集：

- 域（domain）：``display``（显示读路径）/ ``prefetch``（后台预取）；
- 每域计数：``cache_hit`` / ``cache_miss``（source cache）、
  ``lock_wait_ns``（流锁等待累计）、``lock_contended``（等待>0 次数）、
  ``decode_ns``（解码累计）、``decode_count``；
- 全局：``duplicate_source_open``（慢路径重复开源——两线程竞态时
  两次都走 miss）。

线程安全（内部锁）；``snapshot()`` 返回可 JSON 化 dict（harness 落
manifest）；``reset()`` 供重复开关工程基线对比。纯计数器零行为影响。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict

_DOMAINS = ("display", "prefetch")


class DecodeMetrics:
    """解码管理器分域计数器（离屏/真实窗口复测共用口径）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[str, int]] = {
            domain: {
                "cache_hit": 0, "cache_miss": 0,
                "lock_wait_ns": 0, "lock_contended": 0,
                "decode_ns": 0, "decode_count": 0,
            } for domain in _DOMAINS
        }
        self._duplicate_source_open = 0

    def record_cache(self, domain: str, hit: bool) -> None:
        key = "cache_hit" if hit else "cache_miss"
        with self._lock:
            slot = self._counters.get(domain)
            if slot is not None:
                slot[key] += 1

    def record_lock_wait(self, domain: str, wait_ns: int) -> None:
        with self._lock:
            slot = self._counters.get(domain)
            if slot is None:
                return
            slot["lock_wait_ns"] += int(wait_ns)
            if wait_ns > 0:
                slot["lock_contended"] += 1

    def record_decode(self, domain: str, decode_ns: int) -> None:
        with self._lock:
            slot = self._counters.get(domain)
            if slot is not None:
                slot["decode_ns"] += int(decode_ns)
                slot["decode_count"] += 1

    def record_duplicate_source_open(self) -> None:
        with self._lock:
            self._duplicate_source_open += 1

    @staticmethod
    def timed_stream_lock(lock: threading.Lock, domain: str,
                          metrics: "DecodeMetrics"):
        """流锁计时上下文（等待时长分域入账）。"""
        class _Ctx:
            def __enter__(self_inner):
                self_inner._t0 = time.perf_counter_ns()
                lock.acquire()
                waited = time.perf_counter_ns() - self_inner._t0
                if metrics is not None:
                    metrics.record_lock_wait(domain, waited)
                return self_inner

            def __exit__(self_inner, *_exc):
                lock.release()
                return False
        return _Ctx()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "domains": {d: dict(c) for d, c in self._counters.items()},
                "duplicate_source_open": self._duplicate_source_open,
            }

    def reset(self) -> None:
        with self._lock:
            for slot in self._counters.values():
                for key in slot:
                    slot[key] = 0
            self._duplicate_source_open = 0


__all__ = ["DecodeMetrics"]
