"""
帧差异指标计算。

计算相邻帧之间的差异指标，用于场景剪切检测。

## 聧责

- luma_delta: 亮度差异（MAD）
- histogram_chi_square: 直方图差异（Chi-Square）
- edge_delta: 边缘差异（Sobel）
- color_mean_delta: 颜色均值差异
- black_score: 黑场分数
- flash_score: 闪光分数
- motion_score: 运动分数
- combined_score: 组合分数

## 设计原则

- 使用 OpenCV/Numpy 计算
- 不使用 AI 模型
- 所有指标归一化到 0.0-1.0
- 支持滚动窗口统计（自适应阈值）

## 参考

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md 第5.3节
"""

from __future__ import annotations

import logging
from typing import Any

import cv2
import numpy as np

from .models import SceneCutMetrics

logger = logging.getLogger(__name__)


# =============================================================================
# 常量
# =============================================================================

# 组合分数权重（来自计划文档）
WEIGHT_LUMA_DELTA = 0.35
WEIGHT_HIST_DELTA = 0.30
WEIGHT_EDGE_DELTA = 0.20
WEIGHT_COLOR_DELTA = 0.10
# pHash 权重暂不使用，分配给 color_delta
# WEIGHT_PHASH_DELTA = 0.05

# 黑场阈值（亮度低于此值视为黑）
BLACK_LUMA_THRESHOLD = 10.0  # 0-255 范围

# 闪光检测阈值（单帧亮度突变）
FLASH_DELTA_THRESHOLD = 50.0  # 亮度变化阈值

# 直方图 bins 数量
HIST_BINS = 64


# =============================================================================
# 帧特征缓存
# =============================================================================

class _FrameFeatures:
    """
    单帧可复用特征。

    每帧只计算一次（灰度图、直方图、边缘图、均值），
    相邻帧对差异计算直接复用，避免同一帧被重复转换 4 次。

    Attributes:
        gray: 灰度图（uint8）
        luma_mean: 平均亮度（0-255，float64）
        channel_hists: B/G/R 三通道归一化直方图列表
        edge_map: Sobel 边缘图（uint8，0-255）
        color_mean: B/G/R 通道均值（float64 数组）
    """

    __slots__ = ("gray", "luma_mean", "channel_hists", "edge_map", "color_mean")

    def __init__(
        self,
        gray: np.ndarray,
        luma_mean: float,
        channel_hists: list[np.ndarray],
        edge_map: np.ndarray,
        color_mean: np.ndarray,
    ) -> None:
        self.gray = gray
        self.luma_mean = luma_mean
        self.channel_hists = channel_hists
        self.edge_map = edge_map
        self.color_mean = color_mean


# =============================================================================
# 指标计算器
# =============================================================================

class FrameMetricsCalculator:
    """
    帧差异指标计算器。

    计算两个相邻帧之间的差异指标。

    所有指标归一化到 0.0-1.0 范围。

    性能设计：流式检测中同一帧会作为 curr 再作为 prev 连续传入两次
    （同一 ndarray 对象），计算器按对象身份缓存单帧特征，
    每帧的灰度转换/直方图/Sobel 只计算一次。
    所有公式与数值路径和不缓存版本完全一致。

    Examples:
        >>> calc = FrameMetricsCalculator()
        >>> metrics = calc.calculate(frame_prev, frame_curr)
        >>> print(metrics.combined_score)
    """

    def __init__(
        self,
        hist_bins: int = HIST_BINS,
        black_luma_threshold: float = BLACK_LUMA_THRESHOLD,
        flash_delta_threshold: float = FLASH_DELTA_THRESHOLD,
    ) -> None:
        """
        初始化指标计算器。

        Args:
            hist_bins: 直方图 bins 数量
            black_luma_threshold: 黑场亮度阈值
            flash_delta_threshold: 闪光亮度变化阈值
        """
        self._hist_bins = hist_bins
        self._black_luma_threshold = black_luma_threshold
        self._flash_delta_threshold = flash_delta_threshold

        # 单帧特征缓存（按 ndarray 对象身份命中）
        self._cached_frame: np.ndarray | None = None
        self._cached_features: _FrameFeatures | None = None

    # ==================== 特征提取与缓存 ====================

    def _compute_frame_features(self, frame: np.ndarray) -> _FrameFeatures:
        """
        计算单帧特征（灰度、亮度均值、直方图、边缘、颜色均值）。

        Args:
            frame: BGR 帧

        Returns:
            _FrameFeatures 实例
        """
        # 灰度图（luma/edge/black/flash 共用）
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # 平均亮度（0-255）
        luma_mean = float(np.mean(gray))

        # 三通道归一化直方图
        channel_hists: list[np.ndarray] = []
        for ch in range(3):  # B, G, R 通道
            hist = cv2.calcHist(
                [frame], [ch], None, [self._hist_bins], [0, 256]
            ).flatten()
            channel_hists.append(hist / np.sum(hist))

        # Sobel 边缘图
        edge_map = self._compute_sobel_edge(gray)

        # 颜色均值
        color_mean = np.mean(frame, axis=(0, 1))  # B, G, R 平均值

        return _FrameFeatures(
            gray=gray,
            luma_mean=luma_mean,
            channel_hists=channel_hists,
            edge_map=edge_map,
            color_mean=color_mean,
        )

    def _features_for(self, frame: np.ndarray) -> _FrameFeatures:
        """
        获取帧特征（带身份缓存）。

        缓存持有帧对象的强引用，因此身份比较不会因地址复用误命中；
        若调用方原地修改帧数据，请先调用 reset_cache()。

        Args:
            frame: BGR 帧

        Returns:
            _FrameFeatures 实例
        """
        if self._cached_features is not None and frame is self._cached_frame:
            return self._cached_features

        features = self._compute_frame_features(frame)
        self._cached_frame = frame
        self._cached_features = features
        return features

    def reset_cache(self) -> None:
        """清空帧特征缓存（帧数据被原地修改或切换序列时调用）。"""
        self._cached_frame = None
        self._cached_features = None

    # ==================== 主计算方法 ====================

    def calculate(
        self,
        frame_prev: np.ndarray,
        frame_curr: np.ndarray,
    ) -> SceneCutMetrics:
        """
        计算帧差异指标。

        Args:
            frame_prev: 前一帧（BGR 格式，numpy array）
            frame_curr: 当前帧（BGR 格式，numpy array）

        Returns:
            SceneCutMetrics 实例
        """
        # 验证输入
        if frame_prev is None or frame_curr is None:
            return self._create_zero_metrics()

        if frame_prev.shape != frame_curr.shape:
            logger.warning(
                f"Frame shape mismatch: {frame_prev.shape} vs {frame_curr.shape}"
            )
            return self._create_zero_metrics()

        feats_prev = self._features_for(frame_prev)
        feats_curr = self._features_for(frame_curr)

        # 计算各项指标
        luma_delta = self._luma_delta_from_features(feats_prev, feats_curr)
        hist_delta = self._hist_delta_from_features(feats_prev, feats_curr)
        edge_delta = self._edge_delta_from_features(feats_prev, feats_curr)
        color_delta = self._color_delta_from_features(feats_prev, feats_curr)

        # 计算特殊场景分数
        black_score = self._black_score_from_features(feats_prev, feats_curr)
        flash_score = self._flash_score_from_features(feats_prev, feats_curr)
        motion_score = self._calculate_motion_score(
            luma_delta, edge_delta, hist_delta
        )

        # 组合分数
        combined_score = self._calculate_combined_score(
            luma_delta, hist_delta, edge_delta, color_delta
        )

        return SceneCutMetrics(
            luma_delta=luma_delta,
            hist_delta=hist_delta,
            edge_delta=edge_delta,
            color_delta=color_delta,
            black_score=black_score,
            flash_score=flash_score,
            motion_score=motion_score,
            combined_score=combined_score,
        )

    def calculate_with_history(
        self,
        frame_prev: np.ndarray,
        frame_curr: np.ndarray,
        prev_luma_delta: float | None = None,
        prev_edge_delta: float | None = None,
        window_size: int = 5,
    ) -> SceneCutMetrics:
        """
        计算帧差异指标，考虑历史窗口。

        用于检测 flash 和 fast motion（需要连续帧信息）。

        Args:
            frame_prev: 前一帧
            frame_curr: 当前帧
            prev_luma_delta: 前一帧的 luma_delta
            prev_edge_delta: 前一帧的 edge_delta
            window_size: 滚动窗口大小

        Returns:
            SceneCutMetrics 实例
        """
        metrics = self.calculate(frame_prev, frame_curr)

        # 如果有历史数据，调整 flash_score 和 motion_score
        if prev_luma_delta is not None and prev_edge_delta is not None:
            # Flash: 单帧亮度突变后快速恢复
            # 如果前一帧 luma_delta 也很高，说明不是 flash
            if prev_luma_delta > FLASH_DELTA_THRESHOLD / 255.0:
                # 连续高亮度变化 -> 可能是 motion 或 transition
                metrics = SceneCutMetrics(
                    luma_delta=metrics.luma_delta,
                    hist_delta=metrics.hist_delta,
                    edge_delta=metrics.edge_delta,
                    color_delta=metrics.color_delta,
                    black_score=metrics.black_score,
                    flash_score=0.0,  # 降低 flash_score
                    motion_score=metrics.motion_score,
                    combined_score=metrics.combined_score,
                )

        return metrics

    # ==================== 亮度差异 ====================

    def _luma_delta_from_features(
        self,
        feats_prev: _FrameFeatures,
        feats_curr: _FrameFeatures,
    ) -> float:
        """
        计算亮度差异（MAD - Mean Absolute Difference）。

        灰度图像素级绝对差异均值。

        Args:
            feats_prev: 前一帧特征
            feats_curr: 当前帧特征

        Returns:
            归一化亮度差异（0.0-1.0）
        """
        # 计算绝对差异均值（MAD）
        mad = np.mean(
            np.abs(feats_prev.gray.astype(np.float32) - feats_curr.gray.astype(np.float32))
        )

        # 归一化到 0.0-1.0（假设 8-bit 范围 0-255）
        normalized = mad / 255.0

        return float(np.clip(normalized, 0.0, 1.0))

    # ==================== 直方图差异 ====================

    def _hist_delta_from_features(
        self,
        feats_prev: _FrameFeatures,
        feats_curr: _FrameFeatures,
    ) -> float:
        """
        计算直方图差异（Chi-Square）。

        对每个颜色通道的归一化直方图计算 Chi-Square 距离。

        Args:
            feats_prev: 前一帧特征
            feats_curr: 当前帧特征

        Returns:
            归一化直方图差异（0.0-1.0）
        """
        hist_channels: list[float] = []
        for ch in range(3):  # B, G, R 通道
            hist_prev = feats_prev.channel_hists[ch]
            hist_curr = feats_curr.channel_hists[ch]

            # Chi-Square 距离
            chi_square = self._chi_square_distance(hist_prev, hist_curr)
            hist_channels.append(chi_square)

        # 取三个通道的平均值
        avg_chi_square = float(np.mean(hist_channels))

        # 归一化到 0.0-1.0
        # Chi-Square on normalized histograms typically ranges 0-2
        # (1.0 = completely different distributions)
        normalized = avg_chi_square / 2.0

        return float(np.clip(normalized, 0.0, 1.0))

    def _chi_square_distance(
        self,
        hist1: np.ndarray,
        hist2: np.ndarray,
    ) -> float:
        """
        计算 Chi-Square 距离。

        Chi-Square = sum((h1[i] - h2[i])^2 / (h1[i] + h2[i]))

        Args:
            hist1: 直方图 1
            hist2: 直方图 2

        Returns:
            Chi-Square 距离
        """
        # 避免除零
        sum_hist = hist1 + hist2
        sum_hist = np.maximum(sum_hist, 1e-10)

        diff_sq = (hist1 - hist2) ** 2

        chi_square = np.sum(diff_sq / sum_hist)

        return float(chi_square)

    # ==================== 边缘差异 ====================

    def _edge_delta_from_features(
        self,
        feats_prev: _FrameFeatures,
        feats_curr: _FrameFeatures,
    ) -> float:
        """
        计算边缘差异（Sobel）。

        Sobel 边缘图之间的绝对差异均值。

        Args:
            feats_prev: 前一帧特征
            feats_curr: 当前帧特征

        Returns:
            归一化边缘差异（0.0-1.0）
        """
        # 计算边缘图差异
        edge_diff = np.abs(
            feats_prev.edge_map.astype(np.float32) - feats_curr.edge_map.astype(np.float32)
        )

        # 计算差异均值
        mean_diff = float(np.mean(edge_diff))

        # 归一化到 0.0-1.0（Sobel 输出范围约 0-255）
        normalized = mean_diff / 255.0

        return float(np.clip(normalized, 0.0, 1.0))

    def _compute_sobel_edge(self, gray: np.ndarray) -> np.ndarray:
        """
        计算 Sobel 边缘图。

        Args:
            gray: 灰度图

        Returns:
            边缘图（归一化到 0-255）
        """
        # Sobel X 和 Y
        sobel_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        sobel_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)

        # 合并为梯度幅度
        magnitude = np.sqrt(sobel_x ** 2 + sobel_y ** 2)

        # 归一化到 0-255
        magnitude = np.clip(magnitude, 0, 255)
        magnitude = magnitude.astype(np.uint8)

        return magnitude

    # ==================== 颜色均值差异 ====================

    def _color_delta_from_features(
        self,
        feats_prev: _FrameFeatures,
        feats_curr: _FrameFeatures,
    ) -> float:
        """
        计算颜色均值差异。

        每个颜色通道平均值差异的欧氏距离。

        Args:
            feats_prev: 前一帧特征
            feats_curr: 当前帧特征

        Returns:
            归一化颜色均值差异（0.0-1.0）
        """
        # 计算差异（欧氏距离）
        diff = np.sqrt(
            np.sum((feats_prev.color_mean - feats_curr.color_mean) ** 2)
        )

        # 归一化到 0.0-1.0
        # 最大差异约为 sqrt(3 * 255^2) = 255 * sqrt(3)
        max_diff = 255.0 * np.sqrt(3)
        normalized = diff / max_diff

        return float(np.clip(normalized, 0.0, 1.0))

    # ==================== 黑场分数 ====================

    def _black_score_from_features(
        self,
        feats_prev: _FrameFeatures,
        feats_curr: _FrameFeatures,
    ) -> float:
        """
        计算黑场分数。

        检测画面是否接近纯黑（用于识别黑场过渡）。

        Args:
            feats_prev: 前一帧特征
            feats_curr: 当前帧特征

        Returns:
            黑场分数（0.0-1.0）
        """
        luma_prev = feats_prev.luma_mean
        luma_curr = feats_curr.luma_mean

        # 黑场分数：两帧都接近黑时分数高
        # 使用阈值判断
        prev_is_black = luma_prev < self._black_luma_threshold
        curr_is_black = luma_curr < self._black_luma_threshold

        # 如果两帧都是黑，分数为 1.0
        if prev_is_black and curr_is_black:
            return 1.0

        # 如果只有一帧是黑，分数为中等
        if prev_is_black or curr_is_black:
            # 计算亮度差异（从黑到有内容的过渡）
            black_frame_luma = min(luma_prev, luma_curr)

            # 过渡程度
            transition_score = black_frame_luma / self._black_luma_threshold
            return float(np.clip(transition_score, 0.0, 0.5))

        # 都不是黑场
        return 0.0

    # ==================== 闪光分数 ====================

    def _flash_score_from_features(
        self,
        feats_prev: _FrameFeatures,
        feats_curr: _FrameFeatures,
    ) -> float:
        """
        计算闪光分数。

        检测单帧亮度突变（用于识别闪光误判）。

        Args:
            feats_prev: 前一帧特征
            feats_curr: 当前帧特征

        Returns:
            闪光分数（0.0-1.0）
        """
        luma_prev = feats_prev.luma_mean
        luma_curr = feats_curr.luma_mean

        # 亮度变化
        luma_delta = abs(luma_curr - luma_prev)

        # 如果变化超过阈值，可能是闪光
        if luma_delta > self._flash_delta_threshold:
            # 计算闪光分数
            # 亮度突然升高（正向突变）更可能是闪光
            flash_score = luma_delta / 255.0

            # 如果是从暗到亮的突变，分数更高
            if luma_curr > luma_prev:
                flash_score = min(flash_score * 1.5, 1.0)

            return float(np.clip(flash_score, 0.0, 1.0))

        return 0.0

    # ==================== 运动分数 ====================

    def _calculate_motion_score(
        self,
        luma_delta: float,
        edge_delta: float,
        hist_delta: float,
    ) -> float:
        """
        计算运动分数。

        使用多个指标的组合来判断是否为快速运动场景。

        Args:
            luma_delta: 亮度差异
            edge_delta: 边缘差异
            hist_delta: 直方图差异

        Returns:
            运动分数（0.0-1.0）
        """
        # 运动场景特征：
        # - luma_delta 较高（画面整体变化）
        # - edge_delta 较高（构图变化）
        # - hist_delta 中等（色彩分布变化不如硬切明显）

        # 加权计算
        motion_score = (
            0.4 * luma_delta +
            0.4 * edge_delta +
            0.2 * hist_delta
        )

        # 如果 hist_delta 很高但 luma_delta 和 edge_delta 较低，
        # 可能是颜色变化（不是运动）
        if hist_delta > 0.5 and luma_delta < 0.3 and edge_delta < 0.3:
            motion_score *= 0.5

        return float(np.clip(motion_score, 0.0, 1.0))

    # ==================== 组合分数 ====================

    def _calculate_combined_score(
        self,
        luma_delta: float,
        hist_delta: float,
        edge_delta: float,
        color_delta: float,
    ) -> float:
        """
        计算组合分数。

        使用加权平均组合多个指标。

        Args:
            luma_delta: 亮度差异
            hist_delta: 直方图差异
            edge_delta: 边缘差异
            color_delta: 颜色均值差异

        Returns:
            组合分数（0.0-1.0）
        """
        # 权重来自计划文档
        combined = (
            WEIGHT_LUMA_DELTA * luma_delta +
            WEIGHT_HIST_DELTA * hist_delta +
            WEIGHT_EDGE_DELTA * edge_delta +
            WEIGHT_COLOR_DELTA * color_delta
        )

        return float(np.clip(combined, 0.0, 1.0))

    # ==================== 辅助方法 ====================

    def _create_zero_metrics(self) -> SceneCutMetrics:
        """创建零值指标"""
        return SceneCutMetrics(
            luma_delta=0.0,
            hist_delta=0.0,
            edge_delta=0.0,
            color_delta=0.0,
            black_score=0.0,
            flash_score=0.0,
            motion_score=0.0,
            combined_score=0.0,
        )


# =============================================================================
# 自适应阈值计算
# =============================================================================

class AdaptiveThresholdCalculator:
    """
    自适应阈值计算器。

    使用滚动窗口统计（median/MAD）计算动态阈值，
    适应不同素材的动态范围。

    Examples:
        >>> calc = AdaptiveThresholdCalculator(window_size=30)
        >>> for metrics in frame_metrics_list:
        ...     threshold = calc.update(metrics)
        ...     if metrics.combined_score > threshold:
        ...         print("Potential cut")
    """

    def __init__(
        self,
        window_size: int = 30,
        sensitivity: float = 0.55,
    ) -> None:
        """
        初始化自适应阈值计算器。

        Args:
            window_size: 滚动窗口大小（帧数）
            sensitivity: 灵敏度（0.35-0.75）
        """
        self._window_size = window_size
        self._sensitivity = sensitivity

        # 滚动窗口缓冲区
        self._score_buffer: list[float] = []

        # 当前阈值
        self._current_threshold: float = 0.3

    def update(self, metrics: SceneCutMetrics) -> float:
        """
        更新阈值。

        添加新的指标到滚动窗口，重新计算阈值。

        Args:
            metrics: 帧差异指标

        Returns:
            当前阈值
        """
        # 添加新分数
        self._score_buffer.append(metrics.combined_score)

        # 保持窗口大小
        if len(self._score_buffer) > self._window_size:
            self._score_buffer.pop(0)

        # 计算阈值
        if len(self._score_buffer) >= 10:
            self._calculate_threshold()
        else:
            # 窗口未满，使用初始阈值
            self._current_threshold = 0.3

        return self._current_threshold

    def _calculate_threshold(self) -> None:
        """
        计算自适应阈值。

        使用 Median + MAD（Median Absolute Deviation）方法，
        相比 Mean + Std 更鲁棒，不易受异常值影响。
        """
        scores = np.array(self._score_buffer)

        # 计算中位数
        median = float(np.median(scores))

        # 计算 MAD
        mad = float(np.median(np.abs(scores - median)))

        # 阈值 = median + k * MAD
        # k 值根据灵敏度调整
        # sensitivity 低（保守） -> k 大 -> 阈值高 -> 检出的少
        # sensitivity 高（激进） -> k 小 -> 阈值低 -> 检出的多

        # 基准 k 值：sensitivity=0.55 时 k=5
        k_base = 5.0
        k_range = 3.0  # k 变化范围

        # 线性映射：sensitivity 0.35 -> k=8, 0.75 -> k=2
        normalized_sensitivity = (self._sensitivity - 0.35) / (0.75 - 0.35)
        k = k_base + k_range * (1 - normalized_sensitivity)

        # 计算阈值
        threshold = median + k * mad

        # 确保阈值在合理范围
        # 最小阈值：0.15（非常激进）
        # 最大阈值：0.8（非常保守）
        self._current_threshold = float(np.clip(threshold, 0.15, 0.8))

    @property
    def current_threshold(self) -> float:
        """当前阈值"""
        return self._current_threshold

    def reset(self) -> None:
        """重置窗口"""
        self._score_buffer = []
        self._current_threshold = 0.3


# =============================================================================
# 批量指标计算
# =============================================================================

def calculate_metrics_for_frame_pair(
    frame_prev: np.ndarray,
    frame_curr: np.ndarray,
    calculator: FrameMetricsCalculator | None = None,
) -> SceneCutMetrics:
    """
    计算两个帧的差异指标。

    Args:
        frame_prev: 前一帧（BGR）
        frame_curr: 当前帧（BGR）
        calculator: 指标计算器（可选，会创建新实例）

    Returns:
        SceneCutMetrics 实例
    """
    if calculator is None:
        calculator = FrameMetricsCalculator()

    return calculator.calculate(frame_prev, frame_curr)


def calculate_metrics_sequence(
    frames: list[np.ndarray],
    calculator: FrameMetricsCalculator | None = None,
) -> list[SceneCutMetrics]:
    """
    计算帧序列的差异指标。

    Args:
        frames: 帧序列（BGR 格式）
        calculator: 指标计算器（可选）

    Returns:
        SceneCutMetrics 列表（长度为 frames.length - 1）
    """
    if len(frames) < 2:
        return []

    if calculator is None:
        calculator = FrameMetricsCalculator()

    metrics_list: list[SceneCutMetrics] = []

    for i in range(1, len(frames)):
        metrics = calculator.calculate(frames[i - 1], frames[i])
        metrics_list.append(metrics)

    return metrics_list


def compute_adaptive_thresholds(
    metrics_sequence: list[SceneCutMetrics],
    window_size: int = 30,
    sensitivity: float = 0.55,
) -> list[float]:
    """
    计算自适应阈值序列。

    Args:
        metrics_sequence: 指标序列
        window_size: 滚动窗口大小
        sensitivity: 灵敏度

    Returns:
        阈值序列（与 metrics_sequence 长度相同）
    """
    calc = AdaptiveThresholdCalculator(window_size, sensitivity)
    thresholds: list[float] = []

    for metrics in metrics_sequence:
        threshold = calc.update(metrics)
        thresholds.append(threshold)

    return thresholds


# =============================================================================
# 特征提取（用于后续处理）
# =============================================================================

def extract_frame_features(frame: np.ndarray) -> dict[str, Any]:
    """
    提取帧特征。

    用于缓存和快速比较。

    Args:
        frame: BGR 帧

    Returns:
        特征字典（luma_mean, color_mean, edges_mean）
    """
    try:
        import cv2
    except ImportError:
        raise RuntimeError("OpenCV is not installed")

    # 灰度图
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # 亮度均值
    luma_mean = float(np.mean(gray))
    luma_std = float(np.std(gray))

    # 颜色均值
    color_mean = np.mean(frame, axis=(0, 1)).tolist()  # B, G, R

    # Sobel 边缘
    sobel = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    edges_mean = float(np.mean(np.abs(sobel)))

    return {
        "luma_mean": luma_mean,
        "luma_std": luma_std,
        "color_mean": color_mean,
        "edges_mean": edges_mean,
        "width": frame.shape[1],
        "height": frame.shape[0],
    }


def compare_frame_features(
    features_prev: dict[str, Any],
    features_curr: dict[str, Any],
) -> float:
    """
    比较帧特征差异。

    快速估算差异，用于粗扫阶段。

    Args:
        features_prev: 前一帧特征
        features_curr: 当前帧特征

    Returns:
        简化差异分数（0.0-1.0）
    """
    # 亮度差异
    luma_diff = abs(features_prev["luma_mean"] - features_curr["luma_mean"]) / 255.0

    # 颜色差异（欧氏距离）
    color_prev = np.array(features_prev["color_mean"])
    color_curr = np.array(features_curr["color_mean"])
    color_diff = np.sqrt(np.sum((color_prev - color_curr) ** 2)) / (255.0 * np.sqrt(3))

    # 边缘差异
    edge_diff = abs(features_prev["edges_mean"] - features_curr["edges_mean"]) / 255.0

    # 组合
    combined = 0.5 * luma_diff + 0.3 * color_diff + 0.2 * edge_diff

    return float(np.clip(combined, 0.0, 1.0))


# =============================================================================
# 导出
# =============================================================================

__all__ = [
    "FrameMetricsCalculator",
    "AdaptiveThresholdCalculator",
    "calculate_metrics_for_frame_pair",
    "calculate_metrics_sequence",
    "compute_adaptive_thresholds",
    "extract_frame_features",
    "compare_frame_features",
    "WEIGHT_LUMA_DELTA",
    "WEIGHT_HIST_DELTA",
    "WEIGHT_EDGE_DELTA",
    "WEIGHT_COLOR_DELTA",
]