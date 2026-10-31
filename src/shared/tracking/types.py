"""
跟踪不可变数据类型（阶段 1）。

所有类型都是不可变的（frozen dataclass / Enum），可安全跨线程传递：
跟踪工作线程生产，UI 线程消费。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Tuple

import numpy as np

# 跟踪算法版本：算法或阈值变更时递增，使旧缓存自动失效
# v2: 增加参考模板外观门控（遮挡/漂移假有效修复）
TRACKING_ALGORITHM_VERSION = 2

# 跟踪结果持久化 schema 版本
TRACKING_SCHEMA_VERSION = 1


class TrackingMode(Enum):
    """跟踪变换模式。"""

    TRANSLATION = "translation"                       # 仅平移
    TRANSLATION_ROTATION_SCALE = "trs"                # 平移 + 旋转 + 缩放
    AFFINE = "affine"                                 # 仿射（含倾斜）
    HOMOGRAPHY = "homography"                         # 透视（平面）
    CAMERA_3D = "camera_3d"                           # 3D 摄像机（阶段 9，本期禁用）

    @property
    def min_points(self) -> int:
        """求解所需最少有效点数。"""
        return {
            TrackingMode.TRANSLATION: 1,
            TrackingMode.TRANSLATION_ROTATION_SCALE: 2,
            TrackingMode.AFFINE: 3,
            TrackingMode.HOMOGRAPHY: 4,
            TrackingMode.CAMERA_3D: 8,
        }[self]

    @property
    def recommended_points(self) -> int:
        """建议有效点数（低于此值置信度受点数因子惩罚）。"""
        return {
            TrackingMode.TRANSLATION: 4,
            TrackingMode.TRANSLATION_ROTATION_SCALE: 6,
            TrackingMode.AFFINE: 8,
            TrackingMode.HOMOGRAPHY: 12,
            TrackingMode.CAMERA_3D: 16,
        }[self]


class TrackStatus(Enum):
    """单帧跟踪结果状态。"""

    VALID = "valid"      # 正常
    WEAK = "weak"        # 弱跟踪（低于置信度阈值但可用）
    FAILED = "failed"    # 跟踪失败（不产生可信变换）
    MANUAL = "manual"    # 手动修正


class FailurePolicy(Enum):
    """失败处理策略（阶段 7，配置化）。

    与面板「失败策略」下拉一致：停止 / 保持上一帧 / 尝试重捕获。
    """

    STOP = "stop"               # 首次失败即停止，后续帧不产生结果（按失败处理）
    HOLD_LAST = "hold"          # 失败帧保持最后一次有效变换（标记弱跟踪，不假有效）
    RECAPTURE = "recapture"     # 失败后重新检测特征点尝试重捕获（默认）


class FailureReason(Enum):
    """失败原因分类（必须在诊断中给出，不允许无解释的失败）。"""

    NONE = "none"
    INSUFFICIENT_FEATURES = "insufficient_features"   # 特征点不足
    OCCLUSION = "occlusion"                           # 目标被遮挡
    OUT_OF_BOUNDS = "out_of_bounds"                   # 目标越界
    BLUR = "blur"                                     # 图像模糊
    SOLVE_FAILED = "solve_failed"                     # 几何求解失败
    DEGENERATE = "degenerate"                         # 退化四边形/极端透视（阶段 8）
    CANCELLED = "cancelled"                           # 任务被取消


@dataclass(frozen=True)
class TrackTransform:
    """单帧跟踪变换结果（不可变）。

    Attributes:
        frame: source frame 序号。
        matrix: 3x3 行主序单应性矩阵（9 个浮点数）。参考帧为单位矩阵。
        translation: 相对参考帧的平移 (tx, ty)，像素单位（原始分辨率）。
        rotation: 相对参考帧的旋转（度）。
        scale: 相对参考帧的缩放 (sx, sy)。
        shear: 剪切分量 (shx, shy)，非仿射模式可为 0。
        confidence: 置信度 0.0-1.0。
        status: 结果状态。
        failure_reason: 失败原因（status != VALID 时有意义）。
    """

    frame: int
    matrix: Tuple[float, ...]
    translation: Tuple[float, float]
    rotation: float
    scale: Tuple[float, float]
    shear: Tuple[float, float]
    confidence: float
    status: TrackStatus
    failure_reason: FailureReason = FailureReason.NONE

    def __post_init__(self) -> None:
        if len(self.matrix) != 9:
            raise ValueError("matrix 必须是 9 个元素的行主序 3x3 矩阵")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence 必须在 0-1: {self.confidence}")

    # ---- 构造辅助 ----

    @staticmethod
    def from_matrix(
        frame: int,
        matrix: np.ndarray,
        confidence: float,
        status: TrackStatus,
        failure_reason: FailureReason = FailureReason.NONE,
    ) -> "TrackTransform":
        """从 3x3 矩阵构造，自动分解出平移/旋转/缩放/剪切。"""
        m = np.asarray(matrix, dtype=np.float64).reshape(3, 3)
        tx, ty = float(m[0, 2]), float(m[1, 2])
        a, b = float(m[0, 0]), float(m[1, 0])
        c, d = float(m[0, 1]), float(m[1, 1])
        sx = math.hypot(a, b)
        sy = math.hypot(c, d)
        rotation = math.degrees(math.atan2(b, a)) if sx > 1e-12 else 0.0
        # 剪切：第二列相对第一列旋转的偏差
        shear_x = 0.0
        if sx > 1e-12 and sy > 1e-12:
            shear_x = math.degrees(math.atan2(c, d)) + rotation
        return TrackTransform(
            frame=frame,
            matrix=tuple(float(v) for v in m.ravel()),
            translation=(tx, ty),
            rotation=rotation,
            scale=(sx, sy),
            shear=(shear_x, 0.0),
            confidence=float(confidence),
            status=status,
            failure_reason=failure_reason,
        )

    @staticmethod
    def failed(frame: int, reason: FailureReason, message: str = "") -> "TrackTransform":
        """构造失败结果（单位矩阵 + 零置信度）。"""
        return TrackTransform(
            frame=frame,
            matrix=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            translation=(0.0, 0.0),
            rotation=0.0,
            scale=(1.0, 1.0),
            shear=(0.0, 0.0),
            confidence=0.0,
            status=TrackStatus.FAILED,
            failure_reason=reason,
        )

    # ---- 访问辅助 ----

    @property
    def matrix_np(self) -> np.ndarray:
        return np.array(self.matrix, dtype=np.float64).reshape(3, 3)

    def to_dict(self) -> dict:
        return {
            "frame": self.frame,
            "matrix": list(self.matrix),
            "translation": list(self.translation),
            "rotation": self.rotation,
            "scale": list(self.scale),
            "shear": list(self.shear),
            "confidence": self.confidence,
            "status": self.status.value,
            "failure_reason": self.failure_reason.value,
        }

    @staticmethod
    def from_dict(data: dict) -> "TrackTransform":
        return TrackTransform(
            frame=int(data["frame"]),
            matrix=tuple(float(v) for v in data["matrix"]),
            translation=tuple(float(v) for v in data.get("translation", (0.0, 0.0))),
            rotation=float(data.get("rotation", 0.0)),
            scale=tuple(float(v) for v in data.get("scale", (1.0, 1.0))),
            shear=tuple(float(v) for v in data.get("shear", (0.0, 0.0))),
            confidence=float(data.get("confidence", 0.0)),
            status=TrackStatus(data.get("status", "valid")),
            failure_reason=FailureReason(data.get("failure_reason", "none")),
        )


@dataclass(frozen=True)
class FeatureObservation:
    """单个特征点在某帧的观测（不可变）。

    坐标使用原始分辨率像素坐标。
    """

    frame: int
    feature_id: int
    x: float
    y: float
    error: float          # 重投影误差（像素）
    confidence: float     # 0.0-1.0（含 forward-backward 检查）
    status: TrackStatus = TrackStatus.VALID

    def to_dict(self) -> dict:
        return {
            "frame": self.frame,
            "feature_id": self.feature_id,
            "x": self.x,
            "y": self.y,
            "error": self.error,
            "confidence": self.confidence,
            "status": self.status.value,
        }

    @staticmethod
    def from_dict(data: dict) -> "FeatureObservation":
        return FeatureObservation(
            frame=int(data["frame"]),
            feature_id=int(data["feature_id"]),
            x=float(data["x"]),
            y=float(data["y"]),
            error=float(data.get("error", 0.0)),
            confidence=float(data.get("confidence", 0.0)),
            status=TrackStatus(data.get("status", "valid")),
        )
