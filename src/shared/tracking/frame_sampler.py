"""
跟踪分析帧采样器（阶段 0）。

职责：
- 按 source frame 序号读取分析帧（灰度）。
- 按分析质量（draft/normal/high）输出代理降采样帧，并给出缩放矩阵，
  保证跟踪结果可以从代理坐标映射回原始帧坐标。
- 提供素材指纹（fingerprint），用于缓存键与源素材替换检测。

线程约定：本类只在跟踪工作线程中使用，不在 UI 线程调用。
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class TrackingFrameError(RuntimeError):
    """帧读取失败（文件不可解码、帧号越界等）。"""


class AnalysisQuality(Enum):
    """分析质量等级，决定代理帧的长边上限。"""

    DRAFT = "draft"      # 快速预览分析
    NORMAL = "normal"    # 默认交互分析
    HIGH = "high"        # 原始分辨率（最终高质量重分析）


# 各质量等级的长边上限（None 表示原始分辨率）
QUALITY_MAX_SIDE: Dict[AnalysisQuality, Optional[int]] = {
    AnalysisQuality.DRAFT: 540,
    AnalysisQuality.NORMAL: 1080,
    AnalysisQuality.HIGH: None,
}


@dataclass(frozen=True)
class SampledFrame:
    """一帧分析结果及其坐标缩放信息。

    Attributes:
        frame_index: source frame 序号。
        gray: 灰度帧（uint8，H x W）。
        width/height: 返回帧尺寸（可能是代理尺寸）。
        scale: 代理帧相对原始分辨率的均匀缩放系数（原始分辨率时为 1.0）。
        source_width/source_height: 原始素材尺寸。
    """

    frame_index: int
    gray: np.ndarray
    width: int
    height: int
    scale: float
    source_width: int
    source_height: int

    def to_source_point(self, x: float, y: float) -> Tuple[float, float]:
        """代理帧坐标 -> 原始帧坐标。"""
        if self.scale <= 0:
            return x, y
        return x / self.scale, y / self.scale

    def to_analysis_point(self, x: float, y: float) -> Tuple[float, float]:
        """原始帧坐标 -> 代理帧坐标。"""
        return x * self.scale, y * self.scale

    def scale_matrix(self) -> np.ndarray:
        """代理 -> 原始的 3x3 缩放矩阵。"""
        inv = 1.0 / self.scale if self.scale > 0 else 1.0
        return np.array([[inv, 0.0, 0.0], [0.0, inv, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)


class FrameSampler:
    """按 source frame 读取分析帧的采样器。

    内部按质量等级懒加载独立解码器（代理尺寸不同），并提供小型
    LRU 帧缓存，避免跟踪回退/双向检查时重复解码同一帧。
    """

    def __init__(self, source_path: str, cache_size: int = 16) -> None:
        if not source_path:
            raise ValueError("source_path 不能为空")
        self._path = source_path
        self._cache_size = max(1, int(cache_size))
        self._sources: Dict[AnalysisQuality, object] = {}
        self._cache: "OrderedDict[Tuple[int, AnalysisQuality], SampledFrame]" = OrderedDict()
        self._meta: Optional[Tuple[int, int, float, int]] = None  # (w, h, fps, total)
        self._closed = False

    # ---- 生命周期 ----

    @property
    def source_path(self) -> str:
        return self._path

    def close(self) -> None:
        for source in self._sources.values():
            try:
                source.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应中断
                logger.debug("关闭跟踪帧采样器失败", exc_info=True)
        self._sources.clear()
        self._cache.clear()
        self._closed = True

    def __enter__(self) -> "FrameSampler":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- 元信息 ----

    def probe_info(self) -> Tuple[int, int, float, int]:
        """返回 (width, height, fps, total_frames)。"""
        return self.probe_info_raw()

    # ---- 帧读取 ----

    def read_gray(self, frame_index: int, quality: AnalysisQuality = AnalysisQuality.NORMAL) -> SampledFrame:
        """读取指定 source frame 的灰度分析帧。"""
        if self._closed:
            raise TrackingFrameError("采样器已关闭")
        if frame_index < 0:
            raise TrackingFrameError(f"帧号不能为负: {frame_index}")

        key = (frame_index, quality)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        source = self._ensure_source(quality)
        try:
            decoded = source.read_frame(frame_index)
        except ValueError as e:
            raise TrackingFrameError(f"帧号越界: {frame_index}: {e}") from e
        except Exception as e:
            raise TrackingFrameError(f"解码失败: {self._path} @ {frame_index}: {e}") from e

        rgb = decoded.to_rgb()
        if rgb is None or rgb.size == 0:
            raise TrackingFrameError(f"帧无法转换为 RGB: {self._path} @ {frame_index}")
        gray = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2GRAY)
        # OpenCV 的 goodFeaturesToTrack/LK 只接受 uint8 或 float32。真实
        # DNxHR/ProRes 高位深素材可能解码为 uint16/float；跟踪只需要
        # 稳定的亮度结构，因此在采样边界统一为 uint8，避免把格式差异
        # 泄漏到 feature detector 和光流实现中。
        if gray.dtype != np.uint8:
            if np.issubdtype(gray.dtype, np.integer):
                info = np.iinfo(gray.dtype)
                gray = np.clip(
                    gray.astype(np.float32) * (255.0 / float(info.max)),
                    0.0, 255.0,
                ).astype(np.uint8)
            else:
                gray_float = np.asarray(gray, dtype=np.float32)
                finite = gray_float[np.isfinite(gray_float)]
                if finite.size and float(finite.max()) <= 1.0:
                    gray_float *= 255.0
                else:
                    low = float(np.percentile(finite, 1.0)) if finite.size else 0.0
                    high = float(np.percentile(finite, 99.0)) if finite.size else 1.0
                    if high > low:
                        gray_float = (gray_float - low) * (255.0 / (high - low))
                gray = np.nan_to_num(gray_float, nan=0.0, posinf=255.0, neginf=0.0)
                gray = np.clip(gray, 0.0, 255.0).astype(np.uint8)

        src_w, src_h, _, _ = self.probe_info()
        h, w = gray.shape[:2]
        scale = (w / src_w) if src_w > 0 else 1.0

        sample = SampledFrame(
            frame_index=frame_index,
            gray=gray,
            width=int(w),
            height=int(h),
            scale=float(scale),
            source_width=src_w,
            source_height=src_h,
        )
        self._cache[key] = sample
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return sample

    # ---- 内部 ----

    def _ensure_source(self, quality: AnalysisQuality):
        source = self._sources.get(quality)
        if source is not None:
            return source

        from src.shared.media.factory import MediaSourceFactory

        target_size = None
        max_side = QUALITY_MAX_SIDE.get(quality)
        if max_side is not None:
            src_w, src_h, _, _ = self.probe_info_raw()
            target_size = _proxy_size(src_w, src_h, max_side)

        source = MediaSourceFactory.create(self._path, target_size=target_size)
        try:
            source.open()
        except Exception as e:
            raise TrackingFrameError(f"打开素材失败: {self._path}: {e}") from e
        self._sources[quality] = source
        return source

    def probe_info_raw(self) -> Tuple[int, int, float, int]:
        """原始分辨率元信息（不依赖已打开的代理源）。"""
        if self._meta is None:
            from src.shared.media.factory import MediaSourceFactory

            meta = MediaSourceFactory.probe_video_metadata(self._path)
            if meta is None:
                raise TrackingFrameError(f"无法探测素材: {self._path}")
            fps = float(meta.fps) if meta.fps is not None else 25.0
            self._meta = (int(meta.width), int(meta.height), fps, int(meta.total_frames or 0))
        return self._meta


def _proxy_size(src_w: int, src_h: int, max_side: int) -> Tuple[int, int]:
    """计算保持长宽比的代理尺寸（偶数边，兼容 swscale）。"""
    if src_w <= 0 or src_h <= 0:
        return max_side, max_side
    longest = max(src_w, src_h)
    if longest <= max_side:
        return _even(src_w), _even(src_h)
    scale = max_side / float(longest)
    return _even(int(round(src_w * scale))), _even(int(round(src_h * scale)))


def _even(v: int) -> int:
    v = max(2, int(v))
    return v + (v % 2)


def media_fingerprint(path: str, sample_bytes: int = 1 << 20) -> str:
    """素材指纹：路径 + 大小 + 修改时间 + 头部字节内容哈希。

    用于跟踪缓存键与"源素材被替换"检测。文件不存在时返回空字符串。
    """
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    digest = hashlib.sha1()
    digest.update(os.path.abspath(path).encode("utf-8"))
    digest.update(f"|{stat.st_size}|{stat.st_mtime_ns}".encode("ascii"))
    try:
        with open(path, "rb") as fh:
            digest.update(fh.read(sample_bytes))
    except OSError:
        pass
    return digest.hexdigest()
