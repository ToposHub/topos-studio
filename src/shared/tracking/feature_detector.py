"""
特征点检测（阶段 2）。

Shi-Tomasi 角点 + 网格分桶均匀化 + 质量筛选：
- 在 pattern 区域掩码内检测角点；
- 按网格分桶限制每桶数量，避免特征点聚集在高纹理局部，
  保证几何求解的点位分布（置信度"分布因子"的前提）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectorConfig:
    """特征检测参数（全部配置化，禁止散落的魔法数字）。"""

    max_features: int = 240          # 输出点数上限
    min_features: int = 8            # 低于该值视为特征不足
    quality_level: float = 0.01      # Shi-Tomasi 质量阈值（相对最强角点）
    min_distance: float = 5.0        # 角点最小间距（像素）
    block_size: int = 7              # 角点响应窗口
    grid_cols: int = 8               # 分桶网格列数
    grid_rows: int = 6               # 分桶网格行数
    per_bucket_limit: int = 12       # 每个桶保留的最大点数
    detect_oversample: int = 4       # 检测数量过采样倍数（分桶前的候选池）


class FeatureDetector:
    """在指定矩形区域内均匀检测高质量角点。"""

    def __init__(self, config: Optional[DetectorConfig] = None) -> None:
        self.config = config or DetectorConfig()

    def detect(
        self,
        gray: np.ndarray,
        region: Tuple[float, float, float, float],
        mask: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """在 *region*（像素 (x, y, w, h)，gray 坐标系）内检测特征点。

        Args:
            mask: 与 gray 同尺寸的 uint8 掩码（255 = 可检测）。AUD-37307:
                检测掩码取 ``mask ∩ region``——传入形状掩码时，特征点既
                落在遮罩形状内部、也落在 region 矩形内（掩码单独不足以
                约束 region 外的点）；None 时退化为 region 矩形。

        Returns:
            (N, 2) float32 数组，空结果返回形状 (0, 2)。
        """
        cfg = self.config
        frame_h, frame_w = gray.shape[:2]
        x, y, w, h = (float(v) for v in region)
        x0 = max(0.0, min(x, frame_w - 1.0))
        y0 = max(0.0, min(y, frame_h - 1.0))
        x1 = max(0.0, min(x + w, float(frame_w)))
        y1 = max(0.0, min(y + h, float(frame_h)))
        if x1 - x0 < 8.0 or y1 - y0 < 8.0:
            return np.empty((0, 2), dtype=np.float32)

        if mask is not None and (
            mask.shape[:2] != (frame_h, frame_w) or not (mask > 0).any()
        ):
            # 形状掩码与灰度图尺寸不符或全空：退化为矩形，宁可比崩溃好
            mask = None

        if mask is None:
            mask = np.zeros((frame_h, frame_w), dtype=np.uint8)
            mask[int(y0):int(y1), int(x0):int(x1)] = 255
        else:
            # AUD-37307: 检测掩码语义为 mask ∩ region。调用方（tracker_2d）
            # 传入的全帧形状掩码与 pattern region 相互独立——仅用 mask 检测
            # 时，落在 region 外的点会被下面的 clip 逐坐标夹到矩形边界上
            # （多点塌缩、退化几何）。取交集后 region 外的点根本不参与检测。
            combined = np.zeros((frame_h, frame_w), dtype=np.uint8)
            combined[int(y0):int(y1), int(x0):int(x1)] = 255
            mask = cv2.bitwise_and(mask, combined)

        corners = cv2.goodFeaturesToTrack(
            gray,
            maxCorners=cfg.max_features * cfg.detect_oversample,
            qualityLevel=cfg.quality_level,
            minDistance=cfg.min_distance,
            mask=mask,
            blockSize=cfg.block_size,
        )
        if corners is None or len(corners) == 0:
            return np.empty((0, 2), dtype=np.float32)

        points = corners.reshape(-1, 2).astype(np.float32)
        # 掩码按整数像素截断，亚像素角点可能落在请求区域外约 1px；
        # 夹取回区域，保证调用方「点在 region 内」的约定成立。
        points[:, 0] = np.clip(points[:, 0], x0, x1)
        points[:, 1] = np.clip(points[:, 1], y0, y1)
        return self._balance_by_grid(points, x0, y0, x1 - x0, y1 - y0)

    # ---- 内部 ----

    def _balance_by_grid(
        self,
        points: np.ndarray,
        x0: float,
        y0: float,
        w: float,
        h: float,
    ) -> np.ndarray:
        """网格分桶均匀化：每桶保留响应最强的前 N 个点。

        goodFeaturesToTrack 按响应降序返回，桶内先入先保留即"最强优先"。
        超出 ``max_features`` 时按桶轮转截断（见 ``_round_robin_select``），
        不会按桶拼接序整桶丢弃弱纹理桶。
        """
        cfg = self.config
        cols, rows = max(1, cfg.grid_cols), max(1, cfg.grid_rows)
        buckets: dict[Tuple[int, int], list[np.ndarray]] = {}
        for pt in points:
            col = min(cols - 1, max(0, int((pt[0] - x0) / w * cols)))
            row = min(rows - 1, max(0, int((pt[1] - y0) / h * rows)))
            bucket = buckets.setdefault((row, col), [])
            if len(bucket) < cfg.per_bucket_limit:
                bucket.append(pt)

        selected = self._round_robin_select(
            list(buckets.values()), cfg.max_features
        )
        if not selected:
            return np.empty((0, 2), dtype=np.float32)
        return np.asarray(selected, dtype=np.float32)

    @staticmethod
    def _round_robin_select(
        buckets: list[list[np.ndarray]], limit: int
    ) -> list[np.ndarray]:
        """逐桶轮转取点直到达到 limit（桶内保持响应降序）。

        桶序 = 各桶最强点的响应序（``goodFeaturesToTrack`` 降序返回 +
        dict 保持首次插入序）。每一轮所有未空桶各贡献 1 个点，因此
        ``limit >= 非空桶数`` 时每个非空桶至少贡献 1 个点——截断发生在
        轮次之间而不是桶之间，保证点位的空间均匀分布（模块目标）。
        """
        selected: list[np.ndarray] = []
        remaining = [bucket for bucket in buckets if bucket]
        while len(selected) < limit and remaining:
            next_round: list[list[np.ndarray]] = []
            for bucket in remaining:
                if len(selected) >= limit:
                    next_round.append(bucket)
                    continue
                selected.append(bucket.pop(0))
                if bucket:
                    next_round.append(bucket)
            remaining = next_round
        return selected
