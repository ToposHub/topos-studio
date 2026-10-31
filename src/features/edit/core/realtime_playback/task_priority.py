"""
Frame Task Priority Protocol (P1-02)
全局帧任务优先级与满队列丢弃协议（性能总计划 §7 P1-02）。

## 协议（§7.1 P1-02）

    scrub latest > playhead > imminent prefetch > cache fill > thumbnail/GIF > export

- 数值越小优先级越高。
- 满队列时按协议丢弃：高优先级（数值小）到来可以淘汰低优先级排队项；
  同优先级 latest wins；低优先级到来只允许入队空位，绝不淘汰高优先级
  排队项（cache fill 永远挤不掉 playhead）。
- 丢弃必须记 trace（drop 原因带优先级对），绝不阻塞 UI 线程。

## 用法

```python
from src.features.edit.core.realtime_playback.task_priority import (
    FrameTaskPriority, resolve_request_priority, eviction_decision,
)

priority = resolve_request_priority(request_kind="scrub")
verdict = eviction_decision(incoming=priority, pending=pending_priority)
# verdict.action in ("accept", "evict_pending", "drop_incoming")
```
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

# 与 src.shared.video.frame_trace_schema.TASK_PRIORITY_LEVELS 词表一致
# （trace schema 的字符串是这里的 name 小写）。


class FrameTaskPriority(enum.IntEnum):
    """帧任务优先级（数值越小越优先）。"""

    SCRUB_LATEST = 0    # 擦动最新帧：最高，见帧就画
    PLAYHEAD = 10       # 播放头/普通播放 tick
    PREFETCH = 20       # 即将到来的预取（播放方向邻域）
    CACHE_FILL = 30     # 后台缓存回填（idle prebuffer / render cache）
    THUMBNAIL = 40      # 缩略图 / GIF 动态预览
    EXPORT = 50         # 导出 / 交付


#: request_kind（task_meta）→ 优先级映射。
_REQUEST_KIND_PRIORITY = {
    "scrub": FrameTaskPriority.SCRUB_LATEST,
    "playhead": FrameTaskPriority.PLAYHEAD,
    "playback": FrameTaskPriority.PLAYHEAD,
    "prefetch": FrameTaskPriority.PREFETCH,
    "idle_prebuffer": FrameTaskPriority.CACHE_FILL,
    "cache_fill": FrameTaskPriority.CACHE_FILL,
    "background": FrameTaskPriority.CACHE_FILL,
    "thumbnail": FrameTaskPriority.THUMBNAIL,
    "export": FrameTaskPriority.EXPORT,
}


def resolve_request_priority(
    request_kind: str = "",
    *,
    background: bool = False,
) -> FrameTaskPriority:
    """把 task_meta 的 request_kind/background 映射为帧优先级。

    未知 request_kind 一律 PLAYHEAD（保守：宁可高优先也不饿死）。
    """
    if background:
        return FrameTaskPriority.CACHE_FILL
    kind = str(request_kind or "").strip().lower()
    if kind == "scrub":
        return FrameTaskPriority.SCRUB_LATEST
    return _REQUEST_KIND_PRIORITY.get(kind, FrameTaskPriority.PLAYHEAD)


@dataclass(frozen=True)
class EvictionVerdict:
    """满队列丢弃裁决。"""

    action: str            # "accept" | "evict_pending" | "drop_incoming"
    evicted_priority: Optional[int] = None
    reason: str = ""


def eviction_decision(
    incoming: FrameTaskPriority,
    pending: FrameTaskPriority,
) -> EvictionVerdict:
    """满队列时的丢弃裁决（P1-02 协议核心）。

    - incoming < pending（更优先）→ 淘汰 pending；
    - incoming == pending → latest wins，淘汰 pending；
    - incoming > pending（更不优先）→ 丢弃 incoming（绝不阻塞、绝不
      挤掉高优先级工作）。
    """
    if incoming < pending:
        return EvictionVerdict(
            action="evict_pending",
            evicted_priority=int(pending),
            reason=f"incoming:{incoming.name}<pending:{pending.name}",
        )
    if incoming == pending:
        return EvictionVerdict(
            action="evict_pending",
            evicted_priority=int(pending),
            reason=f"same_priority_latest_wins:{incoming.name}",
        )
    return EvictionVerdict(
        action="drop_incoming",
        evicted_priority=None,
        reason=f"incoming:{incoming.name}>pending:{pending.name}",
    )


def priority_name(value: int) -> str:
    """优先级数值 → 协议词表名（trace 用）。"""
    try:
        return FrameTaskPriority(value).name.lower()
    except ValueError:
        return "unknown"
