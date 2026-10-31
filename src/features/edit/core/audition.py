"""F8.3 · 多 take 候选/Audition——纯模型层。

语义（对齐计划）：
- **候选容器**：一个 Audition 挂在时间线位置上，含多个 take 候选
  （各候选保留源范围/素材身份——效果与调色随片段携带）；**不把
  备选做成破坏性替换**（非活动候选保留在容器里，随时切换）；
- **不同长度候选的放置规则**：活动 take 决定容器时长；切换到更长
  take 时可选 ripple（邻片后移）或 fit（裁到现时长）——放置策略
  显式，不静默改变时间线；
- **交换 flatten**：容器拆平=当前活动 take 固化为普通片段，其余
  候选**保留在工程素材池**（不丢其他候选）；
- **缺源**：某候选素材缺失 → 该候选标 offline 不可激活，其他候选
  不受影响；
- 与调色版本（F4.2）/ADR take（素材级）区分：Audition 是**时间线
  片段级**多源候选。

存储：工程 ``auditions`` 字典（{audition_id: {meta, takes}}）；活动
take 绑定经片段 ``audition_id`` 引用——序列化随工程往返。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

#: 放置策略
PLACEMENT_POLICIES = ("ripple", "fit")


@dataclass(frozen=True)
class AuditionTake:
    """单个 take 候选（源范围 + 素材身份）。"""

    take_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    asset_id: str = ""
    source_start: int = 0
    source_duration: int = 0        # 源窗帧数（含）
    label: str = ""
    offline: bool = False           # 缺源标记（不可激活，不丢候选）

    def to_dict(self) -> dict:
        return {
            "take_id": self.take_id, "asset_id": self.asset_id,
            "source_start": self.source_start,
            "source_duration": self.source_duration,
            "label": self.label, "offline": self.offline,
        }

    @staticmethod
    def from_dict(data: dict) -> "AuditionTake":
        return AuditionTake(
            take_id=str(data.get("take_id") or uuid.uuid4()),
            asset_id=str(data.get("asset_id", "") or ""),
            source_start=int(data.get("source_start", 0) or 0),
            source_duration=int(data.get("source_duration", 0) or 0),
            label=str(data.get("label", "") or ""),
            offline=bool(data.get("offline", False)),
        )


@dataclass(frozen=True)
class Audition:
    """多 take 候选容器（时间线位置锚定）。"""

    audition_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timeline_id: str = ""
    track_id: str = ""
    timeline_start: int = 0
    takes: Tuple[AuditionTake, ...] = ()
    active_take_id: Optional[str] = None
    created_at: float = 0.0
    # M15.3：fit 放置的有效窗（持久化）——{take_id: {
    #   "effective_frames": int,   # 实际播放的源窗帧数（裁后）
    #   "short_policy": "hole"|"hold_last",  # 短 take 时尾部策略
    # }}。缺省无键 = 未 fit（整窗播放）。保存重开不丢 fit 语义。
    fit_windows: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def active_take(self) -> Optional[AuditionTake]:
        for take in self.takes:
            if take.take_id == self.active_take_id:
                return take
        return None

    def take_by_id(self, take_id: str) -> Optional[AuditionTake]:
        for take in self.takes:
            if take.take_id == take_id:
                return take
        return None

    def to_dict(self) -> dict:
        return {
            "audition_id": self.audition_id,
            "timeline_id": self.timeline_id, "track_id": self.track_id,
            "timeline_start": self.timeline_start,
            "takes": [t.to_dict() for t in self.takes],
            "active_take_id": self.active_take_id,
            "created_at": self.created_at,
            "fit_windows": {k: dict(v)
                            for k, v in self.fit_windows.items()},
        }

    @staticmethod
    def from_dict(data: dict) -> "Audition":
        return Audition(
            audition_id=str(data.get("audition_id") or uuid.uuid4()),
            timeline_id=str(data.get("timeline_id", "") or ""),
            track_id=str(data.get("track_id", "") or ""),
            timeline_start=int(data.get("timeline_start", 0) or 0),
            takes=tuple(AuditionTake.from_dict(t)
                        for t in data.get("takes", ()) or ()),
            active_take_id=data.get("active_take_id"),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            fit_windows={
                str(k): dict(v)
                for k, v in (data.get("fit_windows") or {}).items()
                if isinstance(v, dict)},
        )


@dataclass(frozen=True)
class SwitchTakeResult:
    """切换 take 的结果（放置变化可预览）。"""

    audition: Audition
    placement: str            # ripple / fit / same_length
    timeline_shift: int       # ripple 时邻片后移帧数（负=前移）
    fitted: bool              # fit 策略是否裁剪了候选


def switch_take(
    audition: Audition,
    new_take_id: str,
    *,
    placement: str = "ripple",
    timeline_fps_num: int = 24,
    timeline_fps_den: int = 1,
    source_fps_by_asset: Optional[Dict[str, Tuple[int, int]]] = None,
    fit_short_policy: str = "hole",
) -> SwitchTakeResult:
    """切换活动 take（非破坏性——其余候选保留）。

    Args:
        placement: "ripple"（更长 take 后移邻片/更短前移）或 "fit"
            （裁到现容器时长，时间线不动）。
        source_fps_by_asset: asset_id → (num, den)；缺省按时间线 fps
            换算（1:1 帧数）。

    Returns:
        SwitchTakeResult：新 audition + 放置变化（预览依据）。
    """
    if placement not in PLACEMENT_POLICIES:
        raise ValueError(f"unknown placement: {placement}")
    new_take = audition.take_by_id(new_take_id)
    if new_take is None:
        raise ValueError(f"take 不存在：{new_take_id}")
    if new_take.offline:
        raise ValueError(
            f"take「{new_take.label or new_take_id}」素材缺失（offline）"
            "——不可激活，其他候选不受影响")
    current = audition.active_take()

    def _timeline_frames(take: AuditionTake) -> int:
        fps_map = source_fps_by_asset or {}
        src = fps_map.get(take.asset_id)
        if src is None:
            return take.source_duration
        s_num, s_den = src
        if s_num * timeline_fps_den == timeline_fps_num * s_den:
            return take.source_duration
        # 混合 fps：源帧数 × (timeline_fps / source_fps)
        return max(1, round(
            take.source_duration
            * (timeline_fps_num * s_den) / (timeline_fps_den * s_num)))

    current_frames = _timeline_frames(current) if current else 0
    new_frames = _timeline_frames(new_take)
    delta = new_frames - current_frames

    fitted = False
    fit_windows = dict(audition.fit_windows)
    if placement == "fit":
        if delta > 0:
            # 长 take 裁到现容器时长——有效窗持久化（保存重开不丢）
            effective = current_frames
            fitted = True
            delta = 0
        else:
            # 短 take：有效窗 = 新 take 全窗；尾部策略显式记录
            # （hole=容器尾部留空 / hold_last=末帧冻结填充）
            effective = new_frames
        fit_windows[new_take_id] = {
            "effective_frames": int(effective),
            "short_policy": str(fit_short_policy),
        }

    updated = Audition(
        audition_id=audition.audition_id,
        timeline_id=audition.timeline_id,
        track_id=audition.track_id,
        timeline_start=audition.timeline_start,
        takes=audition.takes,
        active_take_id=new_take_id,
        created_at=audition.created_at,
        fit_windows=fit_windows,
    )
    return SwitchTakeResult(
        audition=updated,
        placement=("same_length" if delta == 0 else placement),
        timeline_shift=delta if placement == "ripple" else 0,
        fitted=fitted)


def flatten_audition(
    audition: Audition,
) -> Tuple[AuditionTake, Tuple[AuditionTake, ...]]:
    """拆平：返回 (活动 take, 其余候选)——其余候选保留不丢。

    调用方把活动 take 固化为普通片段、其余候选写回素材池/工程表。
    """
    active = audition.active_take()
    if active is None:
        raise ValueError("audition 无活动 take，无法拆平")
    rest = tuple(t for t in audition.takes if t.take_id != active.take_id)
    return active, rest


def mark_take_offline(
    audition: Audition, take_id: str,
) -> Audition:
    """缺源标记：该候选 offline 不可激活；若为活动 take 则回退到首个
    可用候选（无可用候选保持 active=None——容器仍在不丢失）。"""
    takes = tuple(
        t if t.take_id != take_id
        else AuditionTake(**{**t.to_dict(), "offline": True})
        for t in audition.takes)
    active_id = audition.active_take_id
    if active_id == take_id:
        active_id = next(
            (t.take_id for t in takes if not t.offline), None)
    return Audition(
        audition_id=audition.audition_id, timeline_id=audition.timeline_id,
        track_id=audition.track_id, timeline_start=audition.timeline_start,
        takes=takes, active_take_id=active_id,
        created_at=audition.created_at)


__all__ = [
    "Audition",
    "AuditionTake",
    "SwitchTakeResult",
    "PLACEMENT_POLICIES",
    "switch_take",
    "flatten_audition",
    "mark_take_offline",
]
