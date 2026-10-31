"""
特征点管理（阶段 7）：排名、过滤与重新检测。

- ``rank_observations``：按 内点状态 / 置信度 / 重投影误差 / 位置分布
  综合评分排序，供面板点列表与用户筛选；
- ``active_observations``：按绑定的锁定/排除集合过滤观测
  （锁定优先于排除，锁定点永不被删）；
- ``redetect_reference_points``：在参考帧 pattern 区域重新检测特征点
  （用户显式操作，写入 ``binding.observations[reference_frame]``）。

除重新检测外均为纯函数，无 Qt 依赖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

import numpy as np

from src.shared.tracking.coordinate_map import norm_region_to_pixels
from src.shared.tracking.feature_detector import DetectorConfig, FeatureDetector
from src.shared.tracking.frame_sampler import AnalysisQuality, FrameSampler
from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.types import FeatureObservation, TrackStatus


@dataclass(frozen=True)
class RankedPoint:
    """排名后的特征点（含综合评分与各分量）。"""

    feature_id: int
    x: float
    y: float
    score: float
    error: float
    confidence: float
    is_inlier: bool


def rank_observations(
    observations: Iterable[FeatureObservation],
    weight_inlier: float = 0.40,
    weight_confidence: float = 0.25,
    weight_error: float = 0.25,
    weight_position: float = 0.10,
    error_sigma: float = 1.5,
) -> list[RankedPoint]:
    """点排名：内点状态 > 置信度 > 重投影误差 > 位置分布。

    位置因子取该点相对点集质心的归一化半径（靠外的点几何条件更好，
    对变换求解贡献更大）。返回按评分降序的列表。
    """
    obs = list(observations)
    if not obs:
        return []

    pts = np.array([[o.x, o.y] for o in obs], dtype=np.float64)
    centroid = pts.mean(axis=0)
    radii = np.linalg.norm(pts - centroid, axis=1)
    max_radius = float(radii.max()) if len(radii) else 0.0

    ranked: list[RankedPoint] = []
    for i, o in enumerate(obs):
        is_inlier = o.status in (TrackStatus.VALID, TrackStatus.MANUAL)
        error_factor = (
            float(np.exp(-o.error / error_sigma))
            if o.error != float("inf")
            else 0.0
        )
        position_factor = float(radii[i] / max_radius) if max_radius > 0 else 0.0
        score = (
            weight_inlier * (1.0 if is_inlier else 0.0)
            + weight_confidence * max(0.0, min(1.0, o.confidence))
            + weight_error * error_factor
            + weight_position * position_factor
        )
        ranked.append(
            RankedPoint(
                feature_id=o.feature_id,
                x=o.x,
                y=o.y,
                score=score,
                error=o.error,
                confidence=o.confidence,
                is_inlier=is_inlier,
            )
        )
    ranked.sort(key=lambda p: (-p.score, p.feature_id))
    return ranked


def active_observations(
    observations: Iterable[FeatureObservation], binding: TrackingBinding
) -> Tuple[FeatureObservation, ...]:
    """按绑定的排除集合过滤观测；锁定的点永不被排除。"""
    return tuple(
        o
        for o in observations
        if o.feature_id not in binding.excluded_feature_ids
        or o.feature_id in binding.locked_feature_ids
    )


def redetect_reference_points(
    binding: TrackingBinding,
    media_path: str,
    quality: AnalysisQuality = AnalysisQuality.DRAFT,
    detector: Optional[FeatureDetector] = None,
) -> int:
    """在参考帧 pattern 区域重新检测特征点（用户显式操作）。

    结果写入 ``binding.observations[reference_frame]``（原始分辨率坐标），
    返回写入的观测总数。特征点 ID 的跨检测语义：

    - 锁定点的旧观测被原样保留（ID 与坐标不变），"锁定点永不被删"
      在重新检测后依然成立；
    - 新检测点从既有观测的最大 feature_id 之后编号，旧 ID（包括排除
      集合中的 ID）不会命中任何新点——排除语义不会误伤新检测点，
      也不会因重新从 0 编号而错位到任意新点上。

    锁定/排除集合本身不修改。
    """
    det = detector or FeatureDetector(DetectorConfig())
    old_obs = binding.observations.get(binding.reference_frame, ())
    locked_ids = frozenset(binding.locked_feature_ids)
    kept = tuple(o for o in old_obs if o.feature_id in locked_ids)
    next_id = max((o.feature_id for o in old_obs), default=-1) + 1
    sampler = FrameSampler(media_path)
    try:
        sample = sampler.read_gray(binding.reference_frame, quality)
        scale = sample.scale if sample.scale > 0 else 1.0
        rect_src = norm_region_to_pixels(
            binding.pattern_region, sample.source_width, sample.source_height
        )
        rect_ana = (
            rect_src[0] * scale,
            rect_src[1] * scale,
            rect_src[2] * scale,
            rect_src[3] * scale,
        )
        pts = det.detect(sample.gray, rect_ana)
        inv = 1.0 / scale if scale > 0 else 1.0
        fresh = tuple(
            FeatureObservation(
                frame=binding.reference_frame,
                feature_id=next_id + int(i),
                x=float(p[0]) * inv,
                y=float(p[1]) * inv,
                error=0.0,
                confidence=1.0,
                status=TrackStatus.VALID,
            )
            for i, p in enumerate(pts)
        )
        binding.observations[binding.reference_frame] = kept + fresh
        binding.result_version += 1
        binding.modified_at = time.time()
        return len(kept) + len(fresh)
    finally:
        sampler.close()
