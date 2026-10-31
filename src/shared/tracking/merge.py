"""
双向跟踪结果合并（阶段 7）。

前向（参考帧 → frame_end）与后向（参考帧 → frame_start）结果在参考帧合并：

- 参考帧之后的帧取前向结果，参考帧之前的帧取后向结果；
- 双方都覆盖的帧用 pattern 四角偏差度量冲突：偏差超过阈值记为冲突
  （供面板提示用户），并保留置信度较高的一方；
- 轨迹分段：连续结果帧按「可用（有效/弱/手动）/失败」分组，
  失败段不画假有效轨迹，缺失帧（无结果）断段。

合并只产出数据，不修改输入结果；写入绑定走 ``merge_into_binding``
（手动修正帧不会被覆盖，由 ``TrackingBinding.apply_result`` 保证）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.tracker_2d import TrackResult
from src.shared.tracking.types import FeatureObservation, TrackStatus, TrackTransform

# 冲突判定默认阈值：pattern 四角平均偏差（原始分辨率像素）
DEFAULT_CONFLICT_THRESHOLD_PX = 4.0


@dataclass(frozen=True)
class MergeConflict:
    """双向结果在同一帧上不一致（偏差超阈值）。"""

    frame: int
    deviation_px: float


@dataclass(frozen=True)
class TrackSegment:
    """轨迹分段（供画布分段显示）。

    ``usable=True``：有效/弱/手动帧，可画轨迹；
    ``usable=False``：失败段，只画失败标记不画假有效虚线。
    """

    frame_start: int
    frame_end: int
    usable: bool


@dataclass(frozen=True)
class MergeResult:
    """双向合并输出（不可变）。"""

    reference_frame: int
    transforms: Dict[int, TrackTransform]
    observations: Dict[int, Tuple[FeatureObservation, ...]]
    conflicts: Tuple[MergeConflict, ...]
    segments: Tuple[TrackSegment, ...]


# ================================================================
# 几何冲突度量
# ================================================================


def pattern_corner_deviation(
    h1: np.ndarray, h2: np.ndarray, pattern_rect_px: Tuple[float, float, float, float]
) -> float:
    """pattern 四角分别经两个单应性变换后的平均距离（像素）。"""
    x, y, w, h = pattern_rect_px
    corners = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float64
    ).reshape(1, -1, 2)
    p1 = cv2.perspectiveTransform(corners, h1.astype(np.float64)).reshape(-1, 2)
    p2 = cv2.perspectiveTransform(corners, h2.astype(np.float64)).reshape(-1, 2)
    return float(np.linalg.norm(p1 - p2, axis=1).mean())


def trajectory_segments(
    transforms: Dict[int, TrackTransform],
) -> Tuple[TrackSegment, ...]:
    """把逐帧结果切分为连续段（缺帧断段，失败与可用分段）。"""
    if not transforms:
        return ()
    segments: list[TrackSegment] = []
    frames = sorted(transforms)
    seg_start = frames[0]
    seg_usable = transforms[frames[0]].status != TrackStatus.FAILED
    prev = frames[0]
    for frame in frames[1:]:
        usable = transforms[frame].status != TrackStatus.FAILED
        if frame != prev + 1 or usable != seg_usable:
            segments.append(TrackSegment(seg_start, prev, seg_usable))
            seg_start, seg_usable = frame, usable
        prev = frame
    segments.append(TrackSegment(seg_start, prev, seg_usable))
    return tuple(segments)


# ================================================================
# 合并
# ================================================================


def merge_bidirectional(
    forward: TrackResult,
    backward: TrackResult,
    reference_frame: int,
    pattern_rect_px: Tuple[float, float, float, float],
    conflict_threshold_px: float = DEFAULT_CONFLICT_THRESHOLD_PX,
) -> MergeResult:
    """在参考帧合并前向 + 后向结果。

    Args:
        forward / backward: 两个方向的跟踪结果（变换均为
            「参考帧 → 当前帧」，原始分辨率像素坐标）。
        reference_frame: 参考帧（该帧恒为单位矩阵）。
        pattern_rect_px: pattern 区域（原始分辨率像素 (x, y, w, h)），
            用于冲突偏差度量。
        conflict_threshold_px: 冲突判定阈值（四角平均偏差，像素）。
    """
    merged: Dict[int, TrackTransform] = {}
    observations: Dict[int, Tuple[FeatureObservation, ...]] = {}
    conflicts: list[MergeConflict] = []

    sources: Dict[int, list[Tuple[TrackTransform, TrackResult]]] = {}
    for result in (forward, backward):
        for frame, t in result.transforms.items():
            sources.setdefault(frame, []).append((t, result))

    for frame, candidates in sources.items():
        if frame == reference_frame:
            # 参考帧恒等（不双重变换）
            merged[frame] = TrackTransform.from_matrix(
                frame, np.eye(3), 1.0, TrackStatus.VALID
            )
            obs = _pick_observations(candidates)
            if obs is not None:
                observations[frame] = obs
            continue

        if len(candidates) == 1:
            transform, result = candidates[0]
            merged[frame] = transform
            obs = result.observations.get(frame)
            if obs is not None:
                observations[frame] = obs
            continue

        # 双方覆盖：度量冲突，保留置信度高的一方
        (t_fwd, r_fwd), (t_bwd, r_bwd) = candidates[0], candidates[1]
        deviation = pattern_corner_deviation(
            t_fwd.matrix_np, t_bwd.matrix_np, pattern_rect_px
        )
        if deviation > conflict_threshold_px:
            conflicts.append(MergeConflict(frame=frame, deviation_px=deviation))
        if t_bwd.confidence > t_fwd.confidence:
            winner, winner_result = t_bwd, r_bwd
        else:
            winner, winner_result = t_fwd, r_fwd
        merged[frame] = winner
        obs = winner_result.observations.get(frame)
        if obs is not None:
            observations[frame] = obs

    # 参考帧必须有结果（合并锚点）
    if reference_frame not in merged:
        merged[reference_frame] = TrackTransform.from_matrix(
            reference_frame, np.eye(3), 1.0, TrackStatus.VALID
        )

    return MergeResult(
        reference_frame=reference_frame,
        transforms=merged,
        observations=observations,
        conflicts=tuple(conflicts),
        segments=trajectory_segments(merged),
    )


def _pick_observations(
    candidates: list[Tuple[TrackTransform, TrackResult]],
) -> Optional[Tuple[FeatureObservation, ...]]:
    for _t, result in candidates:
        obs = result.observations.get(_t.frame)
        if obs is not None:
            return obs
    return None


def merge_into_binding(
    binding: TrackingBinding, merge_result: MergeResult
) -> int:
    """把合并结果写入绑定（手动修正帧不被覆盖）。返回冲突数。"""
    for frame, transform in merge_result.transforms.items():
        binding.apply_result(
            transform, merge_result.observations.get(frame)
        )
    binding.has_forward = True
    binding.has_backward = True
    return len(merge_result.conflicts)
