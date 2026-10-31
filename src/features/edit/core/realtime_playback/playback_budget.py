"""
Playback Budget Controller
解码/渲染预算控制（缺陷计划 RP-P1-14 / §4）。

## 目标

多轨时 CPU/GPU 被同时打满导致 UI 卡死。需要一个 back-pressure 机制：
- 每帧渲染有 deadline（基于 fps 预算）。
- 渲染超预算时 hold/drop，不阻塞 UI tick。
- 限制并发 decode/backpressure，避免多轨雪崩。

## 职责

- 跟踪每帧渲染耗时 vs 预算。
- 连续超预算时通知 engine 进入 REBUFFERING。
- 提供 should_defer_render(frame)：UI tick 查询是否应推迟本次渲染。

## 集成

engine.request_present -> budget.before_render(frame) -> delegate -> after_render
budget 根据 history 决定下一帧是否 back-pressure。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

logger = logging.getLogger(__name__)


@dataclass
class FrameRenderTiming:
    """单帧渲染耗时记录。"""
    frame: int
    start_ns: int
    end_ns: int
    budget_ns: int
    presented: bool

    @property
    def elapsed_ns(self) -> int:
        return self.end_ns - self.start_ns

    @property
    def over_budget(self) -> bool:
        return self.elapsed_ns > self.budget_ns


class PlaybackBudgetController:
    """
    播放预算控制器（back-pressure）。

    用法：
        budget = PlaybackBudgetController(target_fps=25)
        if budget.before_render(frame):
            t0 = time.time_ns()
            ... render ...
            budget.after_render(frame, t0, presented=True)
        else:
            # back-pressure：skip/drop this frame
    """

    def __init__(
        self,
        target_fps: float = 25.0,
        over_budget_threshold: int = 3,
        history_size: int = 30,
        in_flight_timeout_s: float = 2.0,
    ) -> None:
        self._target_fps = max(1.0, float(target_fps))
        # 每帧预算 = 1/fps，留 20% 余量给 UI/合成开销
        self._budget_ns = int((1.0 / self._target_fps) * 1e9 * 0.8)
        self._over_budget_threshold = max(1, int(over_budget_threshold))
        self._history_size = max(8, int(history_size))
        # AUD-17405：in-flight 超时——渲染路径异常中断时 after_render 不
        # 会执行，_in_flight_start 永久占位会让后续所有帧被 drop（播放
        # 永久冻结）。超过超时的 in-flight 视为已被抛弃，before_render
        # 自愈放行新帧。
        self._in_flight_timeout_ns = max(
            int(float(in_flight_timeout_s) * 1e9), 4 * self._budget_ns
        )

        self._lock = threading.RLock()
        self._history: Deque[FrameRenderTiming] = deque(maxlen=self._history_size)
        self._consecutive_over = 0
        self._back_pressure: bool = False
        self._in_flight_start: Optional[int] = None
        self._in_flight_frame: Optional[int] = None
        # back-pressure 期间连续 drop 的计数（探测帧间隔用）
        self._pressure_drops = 0
        # Never probe on the very first dropped tick.  With a threshold of 1,
        # using the over-budget threshold as the probe interval would
        # immediately re-admit every frame and turn back-pressure into a
        # busy loop.  A minimum interval preserves a real drop while still
        # guaranteeing eventual recovery without an explicit reset.
        self._pressure_probe_interval = max(2, self._over_budget_threshold)

        # 统计
        self._total_renders = 0
        self._total_drops = 0
        self._total_over_budget = 0

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------

    def set_target_fps(self, fps: float) -> None:
        """更新目标 fps（时间线 fps 变化时）。"""
        with self._lock:
            self._target_fps = max(1.0, float(fps))
            self._budget_ns = int((1.0 / self._target_fps) * 1e9 * 0.8)

    @property
    def budget_ms(self) -> float:
        # AUD-17407：与 set_target_fps 的双写同一把锁——无锁读可能读到
        # 更新到一半的 (_target_fps, _budget_ns) 组合。
        with self._lock:
            return self._budget_ns / 1e6

    @property
    def is_back_pressured(self) -> bool:
        with self._lock:
            return self._back_pressure

    # ------------------------------------------------------------------
    # before/after render（engine 热路径）
    # ------------------------------------------------------------------

    def before_render(self, frame: int) -> bool:
        """
        渲染前调用。返回 True 表示可以渲染，False 表示应 back-pressure（drop/hold）。

        back-pressure 触发条件：
        - 当前处于 back-pressure 状态（连续超预算）。
        - 已有 in-flight 渲染（避免并发雪崩）。
        """
        with self._lock:
            if self._back_pressure:
                self._total_drops += 1
                # 自陷死锁防护：back-pressure 期间所有帧都被 drop，
                # after_render 永不执行、consecutive_over 永不下降，
                # back-pressure 就永远不会解除（播放中无人调用
                # reset_pressure），表现为播放头永久冻结。按固定间隔
                # 放行一帧探测帧：渲染回到预算内即自然解除压力；
                # 仍然超预算则继续压制（损失一帧渲染时间，可接受）。
                self._pressure_drops += 1
                if self._pressure_drops < self._pressure_probe_interval:
                    return False
                self._pressure_drops = 0
                # 落到下方 in-flight 登记，放行探测帧
            if self._in_flight_start is not None:
                # AUD-17405：in-flight 超时自愈——渲染路径异常中断时
                # after_render 永不执行，占位若无超时会永久 drop 后续
                # 所有帧（播放冻结）。超时的占位视为已抛弃，放行新帧。
                now_ns = time.time_ns()
                if now_ns - self._in_flight_start > self._in_flight_timeout_ns:
                    logger.warning(
                        "PlaybackBudget: stale in-flight render (frame=%s, "
                        "age=%.0fms > %.0fms timeout); recovering",
                        self._in_flight_frame,
                        (now_ns - self._in_flight_start) / 1e6,
                        self._in_flight_timeout_ns / 1e6,
                    )
                    self._in_flight_start = None
                    self._in_flight_frame = None
                else:
                    # 已有渲染在途，drop 本次避免堆积
                    self._total_drops += 1
                    return False
            self._in_flight_start = time.time_ns()
            self._in_flight_frame = frame
            return True

    def after_render(self, frame: int, start_ns: int, *, presented: bool) -> None:
        """
        渲染后调用，记录耗时并更新 back-pressure 状态。

        Args:
            frame: 渲染的帧号
            start_ns: 渲染开始时间戳（before_render 返回的时点）
            presented: 是否成功 present

        AUD-17407：帧号必须与 before_render 登记的 in-flight 帧一致，
        start_ns 做基本健全性校验（<=0 或落在未来时回退到登记值）——
        旧实现无条件信任调用方，脏数据会污染耗时历史并误导背压决策。
        """
        end_ns = time.time_ns()
        with self._lock:
            in_flight_frame = self._in_flight_frame
            in_flight_start = self._in_flight_start
            if in_flight_frame is not None and frame != in_flight_frame:
                logger.warning(
                    "PlaybackBudget: after_render frame mismatch "
                    "(got %s, in-flight %s); timing ignored",
                    frame, in_flight_frame,
                )
                # 仍清除占位，避免 AUD-17405 式的永久 in-flight。
                self._in_flight_start = None
                self._in_flight_frame = None
                return
            if not isinstance(start_ns, int) or start_ns <= 0 or start_ns > end_ns:
                # 健全性回退：优先用 before_render 的登记值。
                start_ns = in_flight_start if in_flight_start is not None else end_ns
            self._in_flight_start = None
            self._in_flight_frame = None
            timing = FrameRenderTiming(
                frame=frame, start_ns=start_ns, end_ns=end_ns,
                budget_ns=self._budget_ns, presented=presented,
            )
            self._history.append(timing)
            self._total_renders += 1

            if timing.over_budget:
                self._consecutive_over += 1
                self._total_over_budget += 1
                if self._consecutive_over >= self._over_budget_threshold:
                    self._back_pressure = True
                    logger.debug(
                        f"PlaybackBudget: back-pressure ON after "
                        f"{self._consecutive_over} over-budget frames "
                        f"(elapsed={timing.elapsed_ns/1e6:.1f}ms, "
                        f"budget={self.budget_ms:.1f}ms)"
                    )
            else:
                # 回到预算内，解除 back-pressure
                if self._consecutive_over > 0:
                    self._consecutive_over = max(0, self._consecutive_over - 1)
                if self._back_pressure and self._consecutive_over == 0:
                    self._back_pressure = False
                    logger.debug("PlaybackBudget: back-pressure OFF")

    def reset_pressure(self) -> None:
        """重置 back-pressure（seek/pause 后）。"""
        with self._lock:
            self._consecutive_over = 0
            self._back_pressure = False
            self._in_flight_start = None
            self._in_flight_frame = None
            self._pressure_drops = 0

    def abort_render(self, frame: Optional[int] = None) -> bool:
        """放弃当前 in-flight 渲染占位（AUD-17405 提供的显式 abort API）。

        渲染路径捕获异常后应调用本方法，而不是等 in-flight 超时自愈。

        Args:
            frame: 预期在途帧号；None 表示无条件清除。

        Returns:
            True 表示确实清除了一个占位。
        """
        with self._lock:
            if self._in_flight_start is None:
                return False
            if frame is not None and frame != self._in_flight_frame:
                return False
            logger.warning(
                "PlaybackBudget: render aborted (frame=%s)",
                self._in_flight_frame,
            )
            self._in_flight_start = None
            self._in_flight_frame = None
            return True

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def should_defer_render(self, frame: int) -> bool:
        """UI tick 查询是否应推迟本次渲染（back-pressure 时 True）。"""
        return not self.before_render(frame)

    def recent_avg_ms(self) -> float:
        """最近 N 帧平均渲染耗时（ms）。"""
        with self._lock:
            if not self._history:
                return 0.0
            total = sum(t.elapsed_ns for t in self._history)
            return (total / len(self._history)) / 1e6

    def p95_ms(self) -> float:
        """最近 N 帧 p95 渲染耗时（ms）。"""
        with self._lock:
            if not self._history:
                return 0.0
            sorted_elapsed = sorted(t.elapsed_ns for t in self._history)
            idx = int(len(sorted_elapsed) * 0.95)
            idx = min(idx, len(sorted_elapsed) - 1)
            return sorted_elapsed[idx] / 1e6

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "target_fps": self._target_fps,
                "budget_ms": round(self.budget_ms, 2),
                "avg_ms": round(self.recent_avg_ms(), 2),
                "p95_ms": round(self.p95_ms(), 2),
                "back_pressure": self._back_pressure,
                "consecutive_over": self._consecutive_over,
                "total_renders": self._total_renders,
                "total_drops": self._total_drops,
                "total_over_budget": self._total_over_budget,
                "in_flight": self._in_flight_start is not None,
            }
