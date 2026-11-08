"""VP-B03：双视图预算政策与诊断（纯领域）。

计划：docs/plans/2026-11-09-edit-viewport-backend-plan.md §VP-B03。

背景缺口：Timeline／Source 两侧各自有背压（PlaybackBudgetController、
source 的 ``_should_defer_playback_tick``）与 latest-wins 队列，但没有
跨监视器的共享预算政策——谁优先、隐藏/非活动侧是否继续周期请求、预览
请求按什么尺寸上限，全靠各处零散判断；也没有一份可序列化的双路诊断
快照供饥饿/泄漏排查。

政策（对齐计划的固定策略，实测性能数字归 B12 真机报告）：

- 播放中的 Timeline 优先（PLAYHEAD 优先级 + 周期请求 + 较大队列）；
  非活动 Source 静帧策略：禁周期请求（精确一次性 seek 走 B02 的显式
  路径，不受此政策误杀）、低优先级、更小队列（只留最新）；
- Source 播放（B01 单声政策下唯一连续播放侧）时对称：Source PLAYHEAD，
  暂停的 Timeline 静帧；
- 隐藏侧永远禁周期请求、最低优先级；
- 请求尺寸上限 = min(视口尺寸, 素材原生尺寸)，绝不放大超过原生；视口
  未知时不设限（交由既有 native-cap 逻辑）；
- ``collect_dual_view_diagnostics`` 把两路计数（B02 的 widget stats 等）
  与预算决策合并为一份可 JSON 序列化快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

# 与 realtime_playback/task_priority.py 的档位语义一致（数值小 = 优先）
_PRIORITY_PLAYHEAD = 10
_PRIORITY_STILL_FRAME = 20      # 静帧/一次性精确请求
_PRIORITY_HIDDEN = 40           # 隐藏侧：最低（THUMBNAIL 档之下不再压）
_QUEUE_CAP_PLAYING = 4          # 与 GPU 渲染队列 maxsize 对齐
_QUEUE_CAP_STILL = 1            # 只留最新一次请求


@dataclass(frozen=True)
class MonitorLoadState:
    """单侧负载输入（由宿主从 B01 仲裁快照 + 可见性推导）。"""

    monitor: str
    active: bool = True
    playing: bool = False
    hidden: bool = False
    viewport_size: Optional[Tuple[int, int]] = None
    native_size: Optional[Tuple[int, int]] = None


@dataclass(frozen=True)
class BudgetDecision:
    """单侧预算决策。"""

    monitor: str
    allow_periodic_requests: bool
    priority: int
    queue_cap: int
    request_size_cap: Optional[Tuple[int, int]]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "monitor": self.monitor,
            "allow_periodic_requests": self.allow_periodic_requests,
            "priority": self.priority,
            "queue_cap": self.queue_cap,
            "request_size_cap": (list(self.request_size_cap)
                                 if self.request_size_cap is not None
                                 else None),
            "reason": self.reason,
        }


def _request_size_cap(state: MonitorLoadState
                      ) -> Optional[Tuple[int, int]]:
    """请求尺寸上限：视口封顶且绝不放大超过原生。"""
    if state.viewport_size is None:
        return None
    width, height = (max(1, int(state.viewport_size[0])),
                     max(1, int(state.viewport_size[1])))
    if state.native_size is not None:
        width = min(width, max(1, int(state.native_size[0])))
        height = min(height, max(1, int(state.native_size[1])))
    return (width, height)


def _decide_side(state: MonitorLoadState,
                 other_playing: bool) -> BudgetDecision:
    if state.hidden:
        return BudgetDecision(
            state.monitor, allow_periodic_requests=False,
            priority=_PRIORITY_HIDDEN, queue_cap=_QUEUE_CAP_STILL,
            request_size_cap=_request_size_cap(state),
            reason="hidden: no periodic requests, lowest priority")
    if state.playing:
        return BudgetDecision(
            state.monitor, allow_periodic_requests=True,
            priority=_PRIORITY_PLAYHEAD, queue_cap=_QUEUE_CAP_PLAYING,
            request_size_cap=_request_size_cap(state),
            reason="playing: playhead priority, periodic requests")
    # 静帧侧：无论对侧是否在播，都不发周期请求（对侧在播时更不该抢）
    reason = ("still frame while the other monitor plays"
              if other_playing else "idle: still frame only")
    return BudgetDecision(
        state.monitor, allow_periodic_requests=False,
        priority=_PRIORITY_STILL_FRAME, queue_cap=_QUEUE_CAP_STILL,
        request_size_cap=_request_size_cap(state),
        reason=reason)


class DualViewBudgetPolicy:
    """跨监视器共享预算政策（无副作用；执行归各侧既有队列/背压机制）。"""

    def decide(self, *, timeline: MonitorLoadState,
               source: MonitorLoadState) -> Tuple[BudgetDecision,
                                                  BudgetDecision]:
        return (
            _decide_side(timeline, other_playing=source.playing),
            _decide_side(source, other_playing=timeline.playing),
        )


def collect_dual_view_diagnostics(
        decisions: Tuple[BudgetDecision, ...],
        counters: Mapping[str, Mapping[str, int]],
        states: Mapping[str, MonitorLoadState] | None = None,
) -> dict[str, Any]:
    """两路计数 + 负载状态 + 预算决策 → 可 JSON 序列化的诊断快照。"""
    snapshot: dict[str, Any] = {}
    for decision in decisions:
        state = (states or {}).get(decision.monitor)
        snapshot[decision.monitor] = {
            "playing": bool(state.playing) if state else None,
            "active": bool(state.active) if state else None,
            "hidden": bool(state.hidden) if state else None,
            "policy": decision.to_dict(),
            "counters": dict(counters.get(decision.monitor, {})),
        }
    return snapshot
