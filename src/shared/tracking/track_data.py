"""
跟踪绑定数据模型（阶段 1）。

``TrackingBinding`` 描述「一个蒙版形状在一个片段上的跟踪关系」：
- 跟踪范围（source frame 区间、参考帧）与目标区域（pattern/search）。
- 自动跟踪结果（``transforms``）与手动修正（``manual_corrections``）分源存储，
  手动修正永远不会被重新分析覆盖，除非用户明确清除修正。
- 同一帧只暴露一个最终结果（``transform_at``），但内部保留来源区分。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from src.shared.tracking.types import (
    TRACKING_ALGORITHM_VERSION,
    FeatureObservation,
    FailureReason,
    TrackStatus,
    TrackTransform,
    TrackingMode,
)


@dataclass
class TrackingBinding:
    """蒙版形状与跟踪结果的绑定。

    Attributes:
        binding_id: 绑定唯一标识。
        clip_id: 所属片段（Color timeline clip）。
        shape_id: 目标蒙版形状（MaskShape.id）。
        shape_name: 形状名称快照（用于面板显示）。
        source_frame_start / source_frame_end: 分析范围（source frame，含端点）。
        reference_frame: 参考帧（source frame），该帧变换为单位矩阵。
        mode: 跟踪变换模式。
        pattern_region: pattern 区域（归一化 (x, y, w, h)，参考帧）。
        search_region: search 区域（归一化，参考帧）。
        transforms: 自动跟踪结果 {frame: TrackTransform}。
        observations: 特征点观测 {frame: (FeatureObservation, ...)}。
        manual_corrections: 手动修正 {frame: TrackTransform}，优先于自动结果。
        has_forward / has_backward: 前向/后向结果是否已存在。
        result_version: 结果版本号，每次写入递增（供陈旧结果保护）。
        media_fingerprint: 源素材指纹（缓存键与替换检测）。
        algorithm_version: 生成结果的算法版本。
        base_geometry: 参考帧形状几何快照（求值基准，阶段 6 使用）。
        locked_feature_ids: 用户锁定的特征点（不被排除/自动替换，阶段 7）。
        excluded_feature_ids: 用户删除（排除）的特征点（阶段 7）。
        created_at / modified_at: 时间戳。
    """

    binding_id: str
    clip_id: str
    shape_id: str
    shape_name: str = ""
    source_frame_start: int = 0
    source_frame_end: int = 0
    reference_frame: int = 0
    mode: TrackingMode = TrackingMode.TRANSLATION_ROTATION_SCALE
    pattern_region: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    search_region: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    transforms: Dict[int, TrackTransform] = field(default_factory=dict)
    observations: Dict[int, Tuple[FeatureObservation, ...]] = field(default_factory=dict)
    manual_corrections: Dict[int, TrackTransform] = field(default_factory=dict)
    has_forward: bool = False
    has_backward: bool = False
    result_version: int = 0
    media_fingerprint: str = ""
    algorithm_version: int = TRACKING_ALGORITHM_VERSION
    base_geometry: Optional[dict] = None
    locked_feature_ids: set[int] = field(default_factory=set)
    excluded_feature_ids: set[int] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    modified_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.binding_id:
            self.binding_id = str(uuid.uuid4())

    # ---- 查询 ----

    def transform_at(self, frame: int) -> Optional[TrackTransform]:
        """返回该帧的最终结果：手动修正优先于自动结果。"""
        manual = self.manual_corrections.get(frame)
        if manual is not None:
            return manual
        return self.transforms.get(frame)

    def status_at(self, frame: int) -> TrackStatus:
        """该帧的最终状态（无数据时按 FAILED 处理，避免假有效）。"""
        t = self.transform_at(frame)
        if t is None:
            return TrackStatus.FAILED
        return t.status

    def frame_range_with_results(self) -> Optional[Tuple[int, int]]:
        """有结果（自动或手动）的帧范围，无结果返回 None。"""
        frames = set(self.transforms) | set(self.manual_corrections)
        if not frames:
            return None
        return min(frames), max(frames)

    def failed_frames(self) -> list[int]:
        """所有失败帧（按帧号排序）。"""
        frames = [
            f for f, t in self.transforms.items() if t.status == TrackStatus.FAILED
        ]
        return sorted(frames)

    def weak_frames(self) -> list[int]:
        frames = [
            f for f, t in self.transforms.items() if t.status == TrackStatus.WEAK
        ]
        return sorted(frames)

    # ---- 写入 ----

    def apply_result(
        self,
        transform: TrackTransform,
        obs: Optional[Tuple[FeatureObservation, ...]] = None,
    ) -> None:
        """写入一帧自动结果。

        手动修正帧不会被自动结果覆盖（只更新观测数据）。
        """
        frame = transform.frame
        if frame not in self.manual_corrections:
            self.transforms[frame] = transform
        if obs is not None:
            self.observations[frame] = obs
        self.result_version += 1
        self.modified_at = time.time()

    def set_manual_correction(self, transform: TrackTransform) -> None:
        """写入手动修正（状态强制为 MANUAL）。"""
        corrected = TrackTransform(
            frame=transform.frame,
            matrix=transform.matrix,
            translation=transform.translation,
            rotation=transform.rotation,
            scale=transform.scale,
            shear=transform.shear,
            confidence=max(transform.confidence, 1.0),
            status=TrackStatus.MANUAL,
            failure_reason=FailureReason.NONE,
        )
        self.manual_corrections[transform.frame] = corrected
        self.result_version += 1
        self.modified_at = time.time()

    def clear_manual_correction(self, frame: int) -> None:
        if self.manual_corrections.pop(frame, None) is not None:
            self.result_version += 1
            self.modified_at = time.time()

    def clear_results(self, keep_manual: bool = True) -> None:
        """清除自动结果（默认保留手动修正；显式传入 False 才清除修正）。"""
        self.transforms.clear()
        self.observations.clear()
        self.has_forward = False
        self.has_backward = False
        if not keep_manual:
            self.manual_corrections.clear()
        self.result_version += 1
        self.modified_at = time.time()

    def contains_frame(self, frame: int) -> bool:
        return self.source_frame_start <= frame <= self.source_frame_end

    # ---- 特征点管理（阶段 7：锁定/删除，重新检测见 point_manager） ----

    def lock_feature(self, feature_id: int) -> None:
        """锁定特征点（锁定优先于排除）。"""
        self.locked_feature_ids.add(int(feature_id))
        self.excluded_feature_ids.discard(int(feature_id))
        self.result_version += 1
        self.modified_at = time.time()

    def unlock_feature(self, feature_id: int) -> None:
        if int(feature_id) in self.locked_feature_ids:
            self.locked_feature_ids.discard(int(feature_id))
            self.result_version += 1
            self.modified_at = time.time()

    def exclude_feature(self, feature_id: int) -> None:
        """删除（排除）特征点；已锁定的点不可直接排除。"""
        fid = int(feature_id)
        if fid in self.locked_feature_ids:
            return
        self.excluded_feature_ids.add(fid)
        self.result_version += 1
        self.modified_at = time.time()

    def restore_feature(self, feature_id: int) -> None:
        if int(feature_id) in self.excluded_feature_ids:
            self.excluded_feature_ids.discard(int(feature_id))
            self.result_version += 1
            self.modified_at = time.time()

    def is_feature_locked(self, feature_id: int) -> bool:
        return int(feature_id) in self.locked_feature_ids

    def is_feature_excluded(self, feature_id: int) -> bool:
        fid = int(feature_id)
        return fid in self.excluded_feature_ids and fid not in self.locked_feature_ids

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        return {
            "binding_id": self.binding_id,
            "clip_id": self.clip_id,
            "shape_id": self.shape_id,
            "shape_name": self.shape_name,
            "source_frame_start": self.source_frame_start,
            "source_frame_end": self.source_frame_end,
            "reference_frame": self.reference_frame,
            "mode": self.mode.value,
            "pattern_region": list(self.pattern_region),
            "search_region": list(self.search_region),
            "transforms": {str(f): t.to_dict() for f, t in self.transforms.items()},
            "observations": {
                str(f): [o.to_dict() for o in obs]
                for f, obs in self.observations.items()
            },
            "manual_corrections": {
                str(f): t.to_dict() for f, t in self.manual_corrections.items()
            },
            "has_forward": self.has_forward,
            "has_backward": self.has_backward,
            "result_version": self.result_version,
            "media_fingerprint": self.media_fingerprint,
            "algorithm_version": self.algorithm_version,
            "base_geometry": self.base_geometry,
            "locked_feature_ids": sorted(self.locked_feature_ids),
            "excluded_feature_ids": sorted(self.excluded_feature_ids),
            "created_at": self.created_at,
            "modified_at": self.modified_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "TrackingBinding":
        return TrackingBinding(
            binding_id=data.get("binding_id") or str(uuid.uuid4()),
            clip_id=data.get("clip_id", ""),
            shape_id=data.get("shape_id", ""),
            shape_name=data.get("shape_name", ""),
            source_frame_start=int(data.get("source_frame_start", 0)),
            source_frame_end=int(data.get("source_frame_end", 0)),
            reference_frame=int(data.get("reference_frame", 0)),
            mode=TrackingMode(data.get("mode", "trs")),
            pattern_region=tuple(float(v) for v in data.get("pattern_region", (0, 0, 0, 0))),
            search_region=tuple(float(v) for v in data.get("search_region", (0, 0, 0, 0))),
            transforms={
                int(f): TrackTransform.from_dict(t)
                for f, t in (data.get("transforms") or {}).items()
            },
            observations={
                int(f): tuple(FeatureObservation.from_dict(o) for o in obs)
                for f, obs in (data.get("observations") or {}).items()
            },
            manual_corrections={
                int(f): TrackTransform.from_dict(t)
                for f, t in (data.get("manual_corrections") or {}).items()
            },
            has_forward=bool(data.get("has_forward", False)),
            has_backward=bool(data.get("has_backward", False)),
            result_version=int(data.get("result_version", 0)),
            media_fingerprint=data.get("media_fingerprint", ""),
            algorithm_version=int(data.get("algorithm_version", TRACKING_ALGORITHM_VERSION)),
            base_geometry=data.get("base_geometry"),
            locked_feature_ids=set(int(v) for v in data.get("locked_feature_ids", ())),
            excluded_feature_ids=set(
                int(v) for v in data.get("excluded_feature_ids", ())
            ),
            created_at=float(data.get("created_at", 0.0)),
            modified_at=float(data.get("modified_at", 0.0)),
        )
