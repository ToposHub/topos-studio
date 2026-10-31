"""B3: Render Cache smart 自动评估器（纯函数打分，便于单测）。

score(clip) =
    效果数×2 + (有 grade 且非默认)×3 + (speed≠1)×2
    + (composite_id 非空)×4 + (transform 动画参数数)×1

- 分数 ≥ 阈值（默认 6，偏好可调）→ 值得自动缓存；
- 素材时长过短（< 2s，按 fps 折算）不评——缓存收益低于成本；
- 纯函数无 Qt 依赖；触发时机（工程加载完成 / 命令提交后 debounce 5s /
  auto_mode == "smart"）在 edit_controller。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SCORE_THRESHOLD",
    "MIN_SOURCE_DURATION_SECONDS",
    "score_clip",
    "should_auto_cache",
    "select_auto_cache_clips",
]

DEFAULT_SCORE_THRESHOLD = 6
MIN_SOURCE_DURATION_SECONDS = 2.0

# 与计划公式一致的权重表
_WEIGHT_EFFECT = 2
_WEIGHT_GRADE = 3
_WEIGHT_SPEED = 2
_WEIGHT_COMPOSITE = 4
_WEIGHT_ANIMATED = 1

#: Transform 的可动画参数（关键帧存在即计分）
_TRANSFORM_ANIMATED_PARAMS = (
    "opacity",
    "transform.translate_x", "transform.translate_y",
    "transform.scale_x", "transform.scale_y",
    "transform.rotation",
)


def _has_non_default_grade(clip: Any) -> bool:
    """P9.1 共享/本地调色栈是否非默认（栈 id 存在即视为有调色）。"""
    return bool(getattr(clip, "color_stack_id", None))


def _count_animated_params(clip: Any) -> int:
    """统计 transform/opacity 域的关键帧动画参数数量。"""
    animated = getattr(clip, "animated_params", None) or {}
    if not isinstance(animated, dict):
        return 0
    return sum(1 for name in _TRANSFORM_ANIMATED_PARAMS if name in animated)


def score_clip(clip: Any) -> int:
    """按公式给片段打分（越高越值得缓存）。"""
    score = 0
    score += len(getattr(clip, "effects", ()) or ()) * _WEIGHT_EFFECT
    if _has_non_default_grade(clip):
        score += _WEIGHT_GRADE
    try:
        speed = float(clip.effective_speed())
    except (AttributeError, TypeError, ValueError):
        speed = 1.0
    if abs(speed - 1.0) > 1e-9:
        score += _WEIGHT_SPEED
    if getattr(clip, "composite_id", None):
        score += _WEIGHT_COMPOSITE
    score += _count_animated_params(clip) * _WEIGHT_ANIMATED
    return score


def should_auto_cache(
    clip: Any,
    *,
    threshold: int = DEFAULT_SCORE_THRESHOLD,
    min_source_duration_frames: int = 0,
) -> bool:
    """是否值得自动缓存：分数达标且素材时长不过短。"""
    source_duration = int(getattr(clip, "source_duration", 0) or 0)
    if min_source_duration_frames > 0 and source_duration < min_source_duration_frames:
        return False
    return score_clip(clip) >= threshold


def select_auto_cache_clips(
    clips: Iterable[Any],
    *,
    threshold: int = DEFAULT_SCORE_THRESHOLD,
    fps: float = 24.0,
    min_source_duration_seconds: float = MIN_SOURCE_DURATION_SECONDS,
) -> List[Tuple[Any, int]]:
    """从时间线片段中筛选值得自动缓存的片段（分数降序）。

    Args:
        clips: ClipInstance 可迭代
        threshold: 分数阈值
        fps: 时间线帧率（时长折算）
        min_source_duration_seconds: 最短素材时长（秒）

    Returns:
        [(clip, score), ...] 分数降序
    """
    min_frames = max(
        0, int(round(min_source_duration_seconds * max(fps, 1.0))))
    scored: List[Tuple[Any, int]] = []
    for clip in clips or ():
        if getattr(clip, "is_enabled", True) is False:
            continue
        if should_auto_cache(
            clip, threshold=threshold, min_source_duration_frames=min_frames,
        ):
            scored.append((clip, score_clip(clip)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored
