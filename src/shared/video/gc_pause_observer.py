"""GC 停顿观测器（计划 A A1.1 观测级；docs/perf_baseline_freeze_mac_2026-10.md §6）。

只观测，不改策略：通过 ``gc.callbacks`` 记录每次回收的停顿时长并按
generation 聚合，供与帧 deadline miss 做重叠关联（FrameTraceCollector
的 ``note_gc_pause`` + summary()['gc']）。

约束（计划 A1.1）：
- 回调开销必须极低：两次 monotonic 时钟 + 有界 deque/dict 操作。
- **禁止**在本模块做任何 GC 策略变更（collect/freeze/unfreeze/阈值）；
  播放会话级策略试验只有在关联成立后另行立项。
- 线程安全：回调可能在任意触发回收的线程执行；停顿记录加轻量锁。

用法：
    observer = GcPauseObserver()
    observer.start()
    ... 播放/基准循环 ...
    summary = observer.stop()   # {collections, pause_ms_total, ...}
"""

from __future__ import annotations

import gc
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Tuple


class GcPauseObserver:
    """低开销 GC 停顿观测（start/stop 配对；重复 start 幂等）。

    Args:
        max_pauses: 本地停顿 ring 上限（snapshot/overlap 用）。
        sink: 可选的持续推送目标（FrameTraceCollector.note_gc_pause）；
            每次停顿记录后立即推送一条（collector 侧自有界 ring + 锁）。
    """

    def __init__(self, max_pauses: int = 128, sink=None) -> None:
        self._lock = threading.Lock()
        # (stop_monotonic, duration_s, generation) —— 与 trace 时间轴同源
        # （monotonic），可直接做窗口重叠关联。
        self._pauses: Deque[Tuple[float, float, int]] = deque(maxlen=max_pauses)
        self._starts: Dict[int, float] = {}
        self._counts: Dict[int, int] = {}
        self._registered = False
        self._sink = sink

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """注册 gc 回调（幂等）。"""
        if not self._registered:
            gc.callbacks.append(self._on_gc)
            self._registered = True

    def stop(self) -> Dict[str, object]:
        """注销回调并返回聚合摘要（不重置已记录数据）。"""
        if self._registered:
            try:
                gc.callbacks.remove(self._on_gc)
            except ValueError:
                pass
            self._registered = False
        return self.snapshot()

    @property
    def running(self) -> bool:
        return self._registered

    # ------------------------------------------------------------------
    # 回调与记录
    # ------------------------------------------------------------------

    def _on_gc(self, phase: str, info: dict) -> None:
        """gc.callbacks 协议：phase ∈ {'start', 'stop'}。

        极低开销路径：不分配大对象、不打印、不触发额外回收。
        """
        gen = -1
        try:
            gen = int((info or {}).get("generation", -1))
        except (TypeError, ValueError):
            gen = -1
        now = time.monotonic()
        if phase == "start":
            self._starts[gen] = now
            return
        if phase != "stop":
            return
        t0 = self._starts.pop(gen, None)
        if t0 is None:
            return
        duration_s = now - t0
        if duration_s < 0:
            return
        with self._lock:
            self._pauses.append((now, duration_s, gen))
            self._counts[gen] = self._counts.get(gen, 0) + 1
        sink = self._sink
        if sink is not None:
            try:
                sink.note_gc_pause(now, duration_s, gen)
            except Exception:
                pass  # 观测推送失败不影响回调路径

    def note_to(self, recorder) -> None:
        """把已记录的停顿推给 FrameTraceCollector.note_gc_pause（关联用）。"""
        push = getattr(recorder, "note_gc_pause", None)
        if not callable(push):
            return
        for stop_ts, duration_s, gen in self.pauses_snapshot():
            push(stop_ts, duration_s, gen)

    def pauses_snapshot(self) -> List[Tuple[float, float, int]]:
        with self._lock:
            return list(self._pauses)

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            pauses = list(self._pauses)
            counts = dict(self._counts)
        total_s = sum(d for _, d, _ in pauses)
        by_gen: Dict[str, dict] = {}
        for stop_ts, dur, gen in pauses:
            entry = by_gen.setdefault(
                str(gen), {"collections": 0, "pause_ms_total": 0.0,
                           "pause_ms_max": 0.0})
            entry["collections"] += 1
            entry["pause_ms_total"] = round(
                entry["pause_ms_total"] + dur * 1000.0, 3)
            entry["pause_ms_max"] = round(
                max(entry["pause_ms_max"], dur * 1000.0), 3)
        # counts 里有但已滚出 deque 的代次也保留计数（停顿时长不可得）。
        for gen, count in counts.items():
            by_gen.setdefault(str(gen), {"collections": count,
                                         "pause_ms_total": 0.0,
                                         "pause_ms_max": 0.0})
        return {
            "collections": sum(counts.values()) or len(pauses),
            "pause_ms_total": round(total_s * 1000.0, 3),
            "pause_ms_max": round(
                max((d for _, d, _ in pauses), default=0.0) * 1000.0, 3),
            "by_generation": by_gen,
        }

    def overlap_seconds(self, window_t0: float, window_t1: float) -> float:
        """[t0, t1]（monotonic）窗口与已记录停顿的重叠秒数（关联用）。"""
        if window_t1 <= window_t0:
            return 0.0
        overlap = 0.0
        with self._lock:
            pauses = list(self._pauses)
        for stop_ts, dur, _gen in pauses:
            p0 = stop_ts - dur
            p1 = stop_ts
            lo = max(p0, window_t0)
            hi = min(p1, window_t1)
            if hi > lo:
                overlap += hi - lo
        return overlap
