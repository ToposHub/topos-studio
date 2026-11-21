"""B15 · Reconform 改版事务的样本域分段映射与冲突校验（主计划 §20/B15）。

- ``KeptSpan`` / ``remap_sample``：分段保留映射（手册算法 oracle）——
  半开区间防切点重复映射；删除区段返回 None（不吸附"最近"位置）；
  重叠 span 报"ambiguous source mapping"。
- ``plan_kept_spans``：帧域 changes（keep/move/delete）→ 样本域 spans
  （绝对边界式换算，禁止 float 累计）。
- ``validate_reconform_transaction``：锁轨/保护区间冲突 → 整个提交拒绝
  （手册步骤 5：不能半途移动）；孤立项必须显式列出。

复用既有 `match_shots/plan_audio_reconform`（不重写已交付算法）。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, List, Optional, Sequence, Tuple

from src.features.edit.core.reconform import ReconformChange
from src.features.edit.core.sample_time import TimelineSampleClock


@dataclass(frozen=True)
class KeptSpan:
    """保留片段：旧域 [old_start, old_end) → 新域起点 new_start（样本）。"""

    old_start: int
    old_end: int
    new_start: int

    def __post_init__(self) -> None:
        if self.old_end <= self.old_start:
            raise ValueError("empty kept span")


def remap_sample(position: int, spans: Tuple[KeptSpan, ...]) -> Optional[int]:
    """旧域绝对样本 → 新域绝对样本；不在任何保留区段内 → None。

    手册 oracle（§20/B15）：命中多个 span = 源映射歧义，显式报错而非
    随机取一；None 表示删除区段（内容不悄悄复活）。
    """
    matches = [s for s in spans if s.old_start <= position < s.old_end]
    if len(matches) > 1:
        raise ValueError("ambiguous source mapping")
    if not matches:
        return None
    s = matches[0]
    return s.new_start + position - s.old_start


def plan_kept_spans(
    changes: Sequence[ReconformChange],
    *,
    sample_rate: int,
    fps: Fraction,
) -> Tuple[KeptSpan, ...]:
    """帧域 changes → 样本域保留区段（绝对边界式；重叠 = 候选错误）。"""
    clock = TimelineSampleClock(fps, sample_rate)
    spans: List[KeptSpan] = []
    for change in changes:
        kind = str(getattr(change, "kind", ""))
        if kind not in ("keep", "move"):
            continue  # delete：不留 span（remap 返回 None）
        old_start_frame = int(getattr(change, "old_start", 0) or 0)
        new_start_frame = int(getattr(change, "new_start", 0) or 0)
        shot = getattr(change, "old_shot", None)
        duration_frames = int(getattr(shot, "duration_frames", 0) or 0) \
            if shot is not None else 0
        if duration_frames <= 0:
            continue
        old_start = int(clock.frame_start_sample(old_start_frame))
        old_end = int(clock.frame_start_sample(old_start_frame + duration_frames))
        new_start = int(clock.frame_start_sample(new_start_frame))
        spans.append(KeptSpan(old_start=old_start, old_end=old_end,
                              new_start=new_start))
    spans.sort(key=lambda s: (s.old_start, s.old_end))
    for prev, nxt in zip(spans, spans[1:]):
        if nxt.old_start < prev.old_end:
            raise ValueError(
                f"overlapping kept spans: [{prev.old_start}, {prev.old_end})"
                f" and [{nxt.old_start}, {nxt.old_end})")
    return tuple(spans)


def validate_reconform_transaction(
    spans: Tuple[KeptSpan, ...],
    *,
    protected_ranges: Sequence[Tuple[int, int]] = (),
    deleted_ranges: Sequence[Tuple[int, int]] = (),
) -> List[str]:
    """事务级校验：保护区间（音乐轨/锁定轨）被切 → 拒绝；删除区段与
    保留区段重叠 → 拒绝（内容既删又留=事务矛盾）。"""
    errors: List[str] = []
    for lo, hi in protected_ranges:
        if hi <= lo:
            continue
        for span in spans:
            # 保护区间与保留 span 部分相交（非完整包含）= 会被切碎
            if span.old_start < hi and span.old_end > lo:
                if not (lo <= span.old_start and span.old_end <= hi):
                    errors.append(
                        f"protected range [{lo}, {hi}) would be split by "
                        f"span [{span.old_start}, {span.old_end})")
    for lo, hi in deleted_ranges:
        if hi <= lo:
            continue
        for span in spans:
            if span.old_start < hi and span.old_end > lo:
                errors.append(
                    f"deleted range [{lo}, {hi}) overlaps kept span "
                    f"[{span.old_start}, {span.old_end})")
    return errors


__all__ = [
    "KeptSpan",
    "plan_kept_spans",
    "remap_sample",
    "validate_reconform_transaction",
]
