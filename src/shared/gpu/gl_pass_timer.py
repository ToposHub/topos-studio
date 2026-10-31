"""
GL Pass Timer (P0-03)
OpenGL 合成 pass 的 GPU timestamp 计时（性能总计划 §6 P0-03）。

## 职责

- 用 GL_TIME_ELAPSED timer query 实测合成/转场 pass 的 GPU 提交时间。
- 结果延迟一帧读回（in-flight ring），避免 glGetQueryObject 同步等待
  污染被测帧的 wall time。
- 驱动/上下文不支持 timer query 时显式降级为 gpu_time_unavailable，
  禁止用 0.0 或 CPU wall time 冒充 GPU 时间（与 ExportGpuTrail 语义一致）。

## 用法（GL 线程）

```python
timer = GlPassTimer(ctx)

# 每帧合成前：
timer.begin_frame()
with timer.measure("composite"):
    ...draw composite pass...

# 合成完成后（同一 GL 线程，下一帧之前）：
timer.collect()                     # 读回上一帧的 query 结果
ms = timer.last_ms("composite")     # None = gpu_time_unavailable
```

## 线程规则

- 只能在持有当前 GL context 的线程（渲染线程）使用。
- 实例本身不需要加锁：begin/measure/collect 都在同一线程顺序调用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    import moderngl

logger = logging.getLogger(__name__)


class GlPassTimer:
    """ModernGL time query 封装：延迟一帧读回的 pass 级 GPU 计时。

    unsupported=True 时所有 last_*_ms 返回 None，frame 侧必须标记
    gpu_time_unavailable（fail-closed，不冒充测量成功）。
    """

    #: in-flight query 深度：读回延迟 = RING_DEPTH-1 帧。
    RING_DEPTH = 3

    def __init__(self, ctx: "moderngl.Context") -> None:
        """Bind to the render thread's ModernGL context.

        Probes timer-query support once by creating/releasing a throwaway
        query; on failure the instance degrades to ``unsupported=True``
        (fail-closed — every ``last_*`` read returns None).
        """
        self._ctx = ctx
        self._unsupported = False
        self._warned = False
        # pass name -> ring of open queries (list of (frame_seq, tag, query))
        self._open: Dict[str, list] = {}
        self._results: Dict[str, Optional[float]] = {}
        self._frame_seq = 0
        # A0.1：本帧身份标签（由渲染方在 begin_frame 传入，通常为 timeline
        # frame）。query 完成读回时随结果一起返回，调用方据此把 GPU 时间
        # 回填到**发起帧**的 trace——不得把旧 query 写进当前帧。
        self._frame_tag: object = None
        # A0.1：ring 溢出防御性丢弃计数（collect 未被每帧调用时增长）。
        self._overflow_drops = 0
        # AUD-1219: query 对象池——collect 读回后的 query 不再 release，
        # 而是回池供下一帧 measure 复用（ModernGL Query 支持 repeated
        # begin/end），避免每 pass 每帧新建/销毁 GL query。
        self._pool: list = []
        # 探测一次支持性：创建一个 query 立即跑一遍 begin/end。
        # A0.1 修复：ModernGL 5.12 的 Query 没有 release()——旧探测在
        # release 时抛 AttributeError，把可用的 timer query 永久误判为
        # unsupported（fail-closed 关闭了本可用的测量）。release 按
        # 能力探测执行，没有就交给 GC。
        try:
            probe = ctx.query(time=True)
            try:
                probe.__enter__()
                probe.__exit__(None, None, None)
            finally:
                _release = getattr(probe, "release", None)
                if callable(_release):
                    _release()
        except Exception as e:
            self._unsupported = True
            logger.info(f"GlPassTimer: GL timer query unavailable ({e})")

    @property
    def unsupported(self) -> bool:
        """True when GL timer queries are unavailable (fail-closed mode)."""
        return self._unsupported

    def begin_frame(self, tag: object = None) -> None:
        """每帧合成开始时推进帧序号（GL 线程）。

        A0.1：``tag`` 是本帧身份（渲染方通常传 timeline frame int），
        随完成的 query 结果一并返回，用于把 GPU 时间归因到发起帧。
        """
        if self._unsupported:
            return
        self._frame_seq += 1
        if tag is not None:
            self._frame_tag = tag

    def measure(self, pass_name: str) -> "_PassMeasurement":
        """上下文管理器：测量一个 pass 的 GPU 时间。

        with timer.measure("composite"):
            draw()
        """
        return _PassMeasurement(self, pass_name)

    def collect(self) -> list:
        """读回到期的 query 结果（GL 线程，每帧合成完成后调用一次）。

        读回延迟 = RING_DEPTH-1 帧：query 有足够时间在 GPU 上完成，
        glGetQueryObject 不阻塞。读回完成的 query 回池复用（AUD-1219）。

        A0.1：返回本批完成的 query 结果列表（按完成顺序）：
        ``[{"pass": str, "ms": float|None, "frame_seq": int, "tag": object}]``。
        ``ms=None`` 表示该 query 读回失败（消费方按 unavailable 处理，
        不得当 0.0）。``tag`` 是发起该 pass 帧的身份标签。
        """
        completed: list = []
        if self._unsupported:
            return completed
        cutoff = self._frame_seq - (self.RING_DEPTH - 1)
        for name, ring in list(self._open.items()):
            while ring and ring[0][0] <= cutoff:
                _seq, _tag, query = ring.pop(0)
                ms: Optional[float] = None
                try:
                    ns = getattr(query, "elapsed", None)
                    if ns is None:
                        ns = getattr(query, "nanoseconds", None)
                    if ns is not None and ns >= 0:
                        ms = float(ns) / 1e6
                except Exception:
                    ms = None
                finally:
                    self._recycle_query(query)
                self._results[name] = ms
                completed.append(
                    {"pass": name, "ms": ms, "frame_seq": int(_seq), "tag": _tag}
                )
        return completed

    def stats(self) -> dict:
        """A0.1：计时器自身健康统计（溢出丢弃/在飞数量）。"""
        in_flight = sum(len(ring) for ring in self._open.values())
        return {
            "unsupported": self._unsupported,
            "in_flight_queries": in_flight,
            "overflow_drops": int(self._overflow_drops),
        }

    # ── AUD-1219: query 池 ──

    #: 池容量上限（超过即释放而不是回池，防长期不用的 query 滞留）。
    POOL_LIMIT = RING_DEPTH * 8

    def _acquire_query(self):
        """取一个 time query：池非空时复用，池空才向 context 新建。"""
        if self._pool:
            return self._pool.pop()
        return self._ctx.query(time=True)

    def _recycle_query(self, query) -> None:
        """query 生命周期结束后回池；池满（或释放失败兜底）时销毁。

        ModernGL 5.12 的 Query 没有 release()——按能力探测释放，
        没有该方法的实例直接出池交给 GC。
        """
        try:
            if len(self._pool) < self.POOL_LIMIT:
                self._pool.append(query)
                return
            _release = getattr(query, "release", None)
            if callable(_release):
                _release()
        except Exception:
            # 释放失败无法恢复（对象可能已失效）；留 debug 痕迹即可。
            logger.debug("GlPassTimer: query recycle/release failed",
                         exc_info=True)

    def last_ms(self, pass_name: str) -> Optional[float]:
        """最近一次成功读回的 pass GPU 时间（毫秒）。

        None = 尚无读回结果或测量不可用；消费方必须区分
        "还没有数据" 与 0.0ms。
        """
        if self._unsupported:
            return None
        return self._results.get(pass_name)

    def release(self) -> None:
        """释放所有 in-flight 与池中缓存的 query（GL context 销毁前）。"""
        for ring in self._open.values():
            for _seq, _tag, query in ring:
                self._release_query_if_possible(query)
        self._open.clear()
        self._results.clear()
        # AUD-1219: 池中缓存的 query 一并释放，避免 context 销毁后悬挂。
        for query in self._pool:
            self._release_query_if_possible(query)
        self._pool.clear()

    @staticmethod
    def _release_query_if_possible(query) -> None:
        _release = getattr(query, "release", None)
        if not callable(_release):
            return  # ModernGL 5.12 Query 无 release()，交给 GC
        try:
            _release()
        except Exception:
            pass


class _PassMeasurement:
    """单个 pass 的测量上下文。"""

    def __init__(self, timer: GlPassTimer, pass_name: str) -> None:
        self._timer = timer
        self._name = pass_name
        self._query = None
        self._seq = 0

    def __enter__(self) -> "_PassMeasurement":
        """开启 time query 并挂到 ring（unsupported/失败时静默降级为空测量）。

        AUD-1218: query 创建成功但 ``__enter__`` 抛出时必须把对象释放/回池，
        不再泄漏 GL query；AUD-1219: query 优先从池中复用。
        A0.1：ring 条目携带发起帧的 (frame_seq, tag)，collect 读回时
        随结果返回，供调用方归因到发起帧。
        """
        timer = self._timer
        if timer._unsupported:
            return self
        query = None
        try:
            query = timer._acquire_query()
            query.__enter__()
            self._query = query
            self._seq = timer._frame_seq
            timer._open.setdefault(self._name, []).append(
                (self._seq, timer._frame_tag, query))
            if len(timer._open[self._name]) > timer.RING_DEPTH * 4:
                # 防御：collect 长期未被调用时丢弃最旧 query，避免泄漏
                _seq, _tag, stale = timer._open[self._name].pop(0)
                timer._recycle_query(stale)
                timer._overflow_drops += 1
                if not timer._warned:
                    timer._warned = True
                    logger.warning(
                        "GlPassTimer: query ring overflow — collect() not called per frame?"
                    )
        except Exception:
            self._query = None
            if query is not None and not self._query_in_ring(timer, query):
                # 入 ring 之前失败（_acquire_query / query.__enter__）：
                # 释放而非回池——状态未知的 query 复用有风险。
                # （ModernGL 5.12 Query 无 release() 时交给 GC。）
                _release = getattr(query, "release", None)
                if callable(_release):
                    try:
                        _release()
                    except Exception:
                        logger.debug(
                            "GlPassTimer: failed query release after "
                            "__enter__ error", exc_info=True)
        return self

    @staticmethod
    def _query_in_ring(timer: "GlPassTimer", query) -> bool:
        """失败路径防御：query 是否已被挂入某个 pass 的 ring。"""
        return any(
            candidate is query
            for ring in timer._open.values()
            for _seq, candidate in ring
        )

    def __exit__(self, exc_type, exc, tb) -> None:
        """关闭 query；异常向上传播由 with 语义决定，关闭失败静默。"""
        if self._query is not None:
            try:
                self._query.__exit__(exc_type, exc, tb)
            except Exception:
                logger.debug("query __exit__ failed in release()", exc_info=True)
