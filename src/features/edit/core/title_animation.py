"""M13.4 · 标题 roll 动画与批量样式（模型层）。

滚动片尾（roll）动画：纯函数按帧求滚动偏移——内容高于可视区时整
段匀速上滚（片尾样式）；cue 级动画（fade-in/out 滚入）按声明子集。
批量样式应用（样式覆盖批量替换）经既有 ``with_style_override`` 逐
clip 生效——本模块提供批量事务的纯计算部分。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple


def roll_offset(
    frame: int,
    *,
    clip_start: int,
    clip_duration: int,
    content_height: float,
    viewport_height: float,
    hold_start_frames: int = 0,
    hold_end_frames: int = 0,
) -> float:
    """滚动片尾的滚动偏移（像素，M13.4）。

    内容高于视口时：开头 hold → 线性上滚（总滚动量 =
    content_height − viewport_height）→ 结尾 hold。内容不高于视口时
    恒 0（静态）。滚动区间 = 总时长 − 首尾 hold（不足 1 帧时居中静态
    显示，不除零）。
    """
    total_scroll = max(0.0, float(content_height) - float(viewport_height))
    if total_scroll <= 0.0:
        return 0.0
    local = int(frame) - int(clip_start)
    duration = int(clip_duration)
    if local <= 0:
        return 0.0
    if local >= duration:
        return total_scroll
    hold_s = max(0, int(hold_start_frames))
    hold_e = max(0, int(hold_end_frames))
    scroll_frames = duration - hold_s - hold_e
    if scroll_frames <= 0:
        # hold 吃满时长：内容无法完整滚完——按比例展开（不除零）
        scroll_frames = max(1, duration)
        hold_s = hold_e = 0
    if local < hold_s:
        return 0.0
    if local >= duration - hold_e:
        return total_scroll
    t = (local - hold_s) / float(scroll_frames)
    return total_scroll * min(1.0, max(0.0, t))


def cue_fade_alpha(
    frame: int,
    *,
    clip_start: int,
    clip_duration: int,
    fade_in_frames: int = 0,
    fade_out_frames: int = 0,
) -> float:
    """cue 级淡入/淡出透明度（M13.4 cue 动画子集）。

    入点 fade_in_frames 内 0→1，出点 fade_out_frames 内 1→0。
    """
    local = int(frame) - int(clip_start)
    duration = int(clip_duration)
    if local < 0 or local > duration:
        return 0.0
    alpha = 1.0
    fi = max(0, int(fade_in_frames))
    fo = max(0, int(fade_out_frames))
    if fi > 0 and local < fi:
        alpha = min(alpha, local / float(fi))
    if fo > 0 and local > duration - fo:
        alpha = min(alpha, (duration - local) / float(fo))
    return min(1.0, max(0.0, alpha))


def batch_style_override_diff(
    clips: Sequence[Any],
    new_style: Optional[Any],
) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """批量样式应用的差异计算：(需变更 clip 下标, 已一致 clip 下标)。

    与 resolved_style(track_default) 比较——批量替换只触碰实际差异项
    （事务载荷最小化）。None = 清除覆盖回轨道默认。
    """
    changed: list[int] = []
    same: list[int] = []
    for i, clip in enumerate(clips):
        current = getattr(clip, "style_override", None)
        if current is None and new_style is None:
            same.append(i)
        elif current == new_style:
            same.append(i)
        else:
            changed.append(i)
    return tuple(changed), tuple(same)


__all__ = ["roll_offset", "cue_fade_alpha", "batch_style_override_diff"]
