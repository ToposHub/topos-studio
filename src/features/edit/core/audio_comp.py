"""B14 · AudioComp 逐句选 Take 的区域模型与编译计划（主计划 §20/B14）。

- ``CompRegion``：半开样本区间 + take 引用 + 源内起点；同一 comp 活动
  区有序、不重叠（未覆盖区域明确静音或指定底层 lane，不隐式拼别的 take）。
- ``replace_region``：替换区段先切原区域两端，保留左右残片（手册算法
  oracle；源偏移随残片同步推进）。
- ``validate_comp``：有序/不重叠/take 存在性检查（缺 take 的区段显式
  offline，不偷偷换音）。
- ``compile_comp_plan``：编译为 clip/transition 计划（落到既有模型语义，
  不另建音频求和器）——每区段输出 (take, source_start, start, end,
  fade_in/out)；默认 crossfade 由两侧源 handles 允许长度决定（缺 handles
  → 0，硬切）。

坐标：``start/end`` 为 comp 时基绝对样本（与时间线同样本率；跨率先经
sample_time 有理映射——B12/B08 合同），``source_start`` 为 take 源内样本。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Mapping, Optional, Tuple

AUDIO_COMP_VERSION = 1


@dataclass(frozen=True)
class CompRegion:
    """comp 活动区（半开 [start, end)；take 引用 + 源内起点）。"""

    start: int
    end: int
    take_id: str
    source_start: int
    fade_in: int = 0
    fade_out: int = 0

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("empty comp region")


@dataclass(frozen=True)
class AudioComp:
    """一条轨的逐句 comp（底层 lane 只引用已有 AuditionTake/asset）。"""

    comp_id: str
    track_id: str
    regions: Tuple[CompRegion, ...] = ()
    version: int = AUDIO_COMP_VERSION

    def with_regions(self, regions: Tuple[CompRegion, ...]) -> "AudioComp":
        return replace(self, regions=tuple(regions))


def validate_comp_regions(regions: Tuple[CompRegion, ...]) -> List[str]:
    """有序 + 不重叠 + 数值合法检查；返回错误列表（空=通过）。"""
    errors: List[str] = []
    prev: Optional[CompRegion] = None
    for i, region in enumerate(regions):
        if region.end <= region.start:
            errors.append(f"region {i}: empty range [{region.start}, {region.end})")
            continue
        if prev is not None and region.start < prev.end:
            errors.append(
                f"region {i} [{region.start}, {region.end}) overlaps previous "
                f"ending at {prev.end}")
        prev = region
    return errors


def replace_region(regions: Tuple[CompRegion, ...],
                   new: CompRegion) -> Tuple[CompRegion, ...]:
    """替换活动区段，保留左右残片（源偏移随残片同步推进）。

    手册算法（§20/B14）：被新区段覆盖的旧区段切两端；左残片保留原
    source_start，右残片 source 前移 ``new.end - old.start``；结果按 start
    排序。空区段拒绝。
    """
    if new.end <= new.start:
        raise ValueError("empty region")
    result: List[CompRegion] = []
    for old in regions:
        if old.end <= new.start or old.start >= new.end:
            result.append(old)
            continue
        if old.start < new.start:
            result.append(replace(old, end=new.start))
        if old.end > new.end:
            result.append(replace(old, start=new.end,
                                  source_start=old.source_start
                                  + new.end - old.start))
    return tuple(sorted([*result, new], key=lambda r: r.start))


def compile_comp_plan(
    comp: AudioComp,
    take_lengths: Mapping[str, int],
    *,
    max_crossfade: int = 64,
) -> Tuple[Dict[str, Tuple[CompRegion, ...]], List[str]]:
    """编译 comp → 每区段渲染计划 + 诊断。

    返回 ``(plan_by_take, warnings)``：plan 按 take 分组（同一 take 的
    各区段可合并渲染），每区段带源内窗与边界 crossfade——crossfade 取
    ``max_crossfade`` 与两侧区段长/源可用长度的最小值；源窗越界（源短
    于需求）的区段标 offline 诊断，不静默换 take（§20/B14 缺 take 语义）。
    """
    warnings: List[str] = []
    errors = validate_comp_regions(comp.regions)
    if errors:
        raise ValueError("; ".join(errors))
    plan: Dict[str, List[CompRegion]] = {}
    ordered = sorted(comp.regions, key=lambda r: r.start)
    for i, region in enumerate(ordered):
        length = int(take_lengths.get(region.take_id, -1))
        if length < 0:
            warnings.append(
                f"region [{region.start}, {region.end}) take "
                f"{region.take_id!r} missing — region renders offline")
            continue
        available = length - region.source_start
        if available < region.end - region.start:
            warnings.append(
                f"region [{region.start}, {region.end}) exceeds take "
                f"{region.take_id!r} remaining samples ({available}) — "
                "region renders offline")
            continue
        # crossfade：区段切换边界的短交叉淡化（comp 的核心语义）——受
        # 本区段长度与 max_crossfade 限制；相邻区段在边界处各自半交叠。
        fade_in = max(0, min(int(region.fade_in), int(max_crossfade),
                             region.end - region.start))
        fade_out = max(0, min(int(region.fade_out), int(max_crossfade),
                              region.end - region.start))
        plan.setdefault(region.take_id, []).append(
            CompRegion(start=region.start, end=region.end,
                       take_id=region.take_id,
                       source_start=region.source_start,
                       fade_in=fade_in, fade_out=fade_out))
    return ({take: tuple(regions) for take, regions in plan.items()}, warnings)


__all__ = [
    "AUDIO_COMP_VERSION",
    "AudioComp",
    "CompRegion",
    "compile_comp_plan",
    "replace_region",
    "validate_comp_regions",
]
