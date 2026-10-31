"""
视频帧采样器。

使用 PyAV 顺序解码视频帧，支持缩放和 cancel token。

## 聧责

- VideoFrameSampler: 顺序解码视频帧
- 支持缩放到指定尺寸（sample_width, max_sample_height）
- 记录 PTS、seconds、frame index、is_keyframe
- 支持 cancel token 检查
- 返回 FrameTimePoint 和缩放后的帧数据

## 设计原则

- 使用 PyAV 进行解码（不使用 OpenCV VideoCapture）
- 完整类型注解
- 支持 cancel token（后台任务可取消）
- 保持 aspect ratio 缩放
- 顺序解码，不频繁 seek

## 参考

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md 第5.1节
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Callable, Generator, Iterator, Optional

import numpy as np

from .models import FrameTimePoint, SceneCutSettings

logger = logging.getLogger(__name__)


# =============================================================================
# 转换流水线参数
# =============================================================================

# 连续转换失败熔断阈值：超过该次数的连续失败说明媒体/解码本身有问题，
# 继续用全黑帧填充只会向检测管线注入伪造的 BLACK_GAP/漏检信号
# （AUD-17921）。
MAX_CONSECUTIVE_CONVERT_FAILURES: int = 10

# 帧转换（planar-scale-first：先平面格式缩放、再小图转色）为逐帧独立的
# C 调用且释放 GIL。4K ProRes/DNxHR 实测 8 线程端到端最快（2143 帧
# 全片 34.4s vs 4 线程 36.9-43.2s，取决于页缓存状态），超过 8 无收益；
# 解码内部线程与转换线程在 16 核上可共存。
CONVERT_WORKERS: int = max(1, min(8, os.cpu_count() or 1))

# 有界预读队列深度（限制内存占用：每帧约 110KB @ 256x144 BGR）
CONVERT_QUEUE_DEPTH: int = 32


# =============================================================================
# Cancel Token
# =============================================================================

@dataclass
class CancelToken:
    """
    取消信号 token。

    用于后台任务取消检测。调用者设置 cancelled=True 后，
    任务检查并停止执行。

    Attributes:
        cancelled: 是否已取消
        reason: 取消原因（可选）
    """
    cancelled: bool = False
    reason: str | None = None

    def cancel(self, reason: str | None = None) -> None:
        """取消任务"""
        self.cancelled = True
        self.reason = reason

    def is_cancelled(self) -> bool:
        """检查是否已取消"""
        return self.cancelled

    def check_and_raise(self) -> None:
        """检查取消状态，如果已取消则抛出异常"""
        if self.cancelled:
            raise CancelledError(self.reason or "Task cancelled")


class CancelledError(Exception):
    """任务取消异常"""
    pass


# =============================================================================
# 采样帧数据
# =============================================================================

@dataclass
class SampledFrame:
    """
    采样帧数据。

    包含缩放后的帧数据和帧时间信息。

    Attributes:
        time_point: 帧时间点
        frame_data: 缩放后的帧数据（BGR 格式，numpy array）
        width: 缩放后宽度
        height: 缩放后高度
        original_width: 原始宽度
        original_height: 原始高度
    """
    time_point: FrameTimePoint
    frame_data: np.ndarray
    width: int
    height: int
    original_width: int
    original_height: int

    @property
    def source_frame(self) -> int:
        """源帧号"""
        return self.time_point.source_frame

    @property
    def seconds(self) -> float:
        """时间秒数"""
        return self.time_point.seconds

    @property
    def pts(self) -> int | None:
        """PTS"""
        return self.time_point.pts

    @property
    def is_keyframe(self) -> bool:
        """是否为关键帧"""
        return self.time_point.is_keyframe


# =============================================================================
# 视频帧采样器
# =============================================================================

class VideoFrameSampler:
    """
    视频帧采样器。

    使用 PyAV 顺序解码视频帧，支持缩放和 cancel token。

    Attributes:
        source_path: 源文件路径
        settings: 检测设置
        cancel_token: 取消 token

    Examples:
        >>> sampler = VideoFrameSampler('/path/to/video.mp4', settings)
        >>> with sampler:
        ...     for frame in sampler.sample_frames():
        ...         print(frame.source_frame, frame.seconds)
        ...         # 计算 metrics
    """

    def __init__(
        self,
        source_path: str,
        settings: SceneCutSettings,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """
        初始化帧采样器。

        Args:
            source_path: 源文件路径
            settings: 检测设置（包含 sample_width, max_sample_height）
            cancel_token: 取消 token（可选）
        """
        self._source_path = source_path
        self._settings = settings
        self._cancel_token = cancel_token or CancelToken()

        # PyAV 对象
        self._container: Any = None
        self._video_stream: Any = None

        # 解码状态
        self._is_open: bool = False
        self._current_frame: int = 0
        self._frame_iterator: Iterator[Any] | None = None
        # Seek 预滚后"已取出但未消费"的帧：_seek_to_frame 停在目标帧上，
        # 由后续采样循环优先消费（AUD-17920：不再丢弃起始帧导致
        # source_frame 整体错位一格）。
        self._held_frame: Any | None = None

        # 元数据
        self._fps: float = 0.0
        self._time_base: float = 1.0
        self._total_frames: int = 0
        self._original_width: int = 0
        self._original_height: int = 0

        # 缩放目标尺寸
        self._target_width: int = settings.sample_width
        self._target_height: int = settings.max_sample_height

        # 转换失败统计（_convert_frame_data 在转换线程池中执行，
        # 计数需加锁；AUD-17921）
        self._convert_stats_lock = threading.Lock()
        self._consecutive_convert_failures: int = 0
        self._total_convert_failures: int = 0

    @property
    def source_path(self) -> str:
        """源文件路径"""
        return self._source_path

    @property
    def settings(self) -> SceneCutSettings:
        """检测设置"""
        return self._settings

    @property
    def cancel_token(self) -> CancelToken:
        """取消 token"""
        return self._cancel_token

    @property
    def is_open(self) -> bool:
        """是否已打开"""
        return self._is_open

    @property
    def fps(self) -> float:
        """帧率"""
        return self._fps

    @property
    def total_frames(self) -> int:
        """总帧数"""
        return self._total_frames

    @property
    def original_width(self) -> int:
        """原始宽度"""
        return self._original_width

    @property
    def original_height(self) -> int:
        """原始高度"""
        return self._original_height

    @property
    def target_width(self) -> int:
        """目标宽度"""
        return self._target_width

    @property
    def target_height(self) -> int:
        """目标高度"""
        return self._target_height

    @property
    def total_convert_failures(self) -> int:
        """像素转换累计失败帧数（诊断用，AUD-17921）。"""
        with self._convert_stats_lock:
            return self._total_convert_failures

    # ==================== 生命周期管理 ====================

    def open(self) -> None:
        """
        打开视频文件。

        使用 PyAV 打开容器并初始化解码器。

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 无视频流
        """
        if self._is_open:
            return

        try:
            import av
        except ImportError:
            raise RuntimeError("PyAV is not installed")

        try:
            # 打开容器
            self._container = av.open(self._source_path)

            # 查找最佳视频流
            video_streams = self._container.streams.video
            if not video_streams:
                raise ValueError(f"No video stream found: {self._source_path}")

            self._video_stream = video_streams[0]
            self._video_stream.thread_type = 'AUTO'

            # 提取元数据
            self._original_width = self._video_stream.width
            self._original_height = self._video_stream.height

            # 帧率
            if hasattr(self._video_stream, 'average_rate') and self._video_stream.average_rate:
                rate = self._video_stream.average_rate
                self._fps = float(rate.numerator) / float(rate.denominator)
            elif hasattr(self._video_stream, 'base_rate') and self._video_stream.base_rate:
                rate = self._video_stream.base_rate
                self._fps = float(rate.numerator) / float(rate.denominator)
            else:
                self._fps = 24.0  # 默认值

            # 时间基
            if self._video_stream.time_base:
                self._time_base = float(self._video_stream.time_base)

            # 总帧数
            if hasattr(self._video_stream, 'frames') and self._video_stream.frames:
                self._total_frames = self._video_stream.frames
            elif self._video_stream.duration and self._time_base > 0:
                # 从 duration 计算
                duration_sec = float(self._video_stream.duration) * self._time_base
                self._total_frames = int(duration_sec * self._fps)
            else:
                self._total_frames = 0

            # 计算缩放尺寸（保持 aspect ratio）
            self._calculate_target_size()

            self._is_open = True
            self._current_frame = 0
            self._frame_iterator = None
            self._held_frame = None

            logger.debug(
                f"Opened frame sampler: {self._source_path}, "
                f"{self._original_width}x{self._original_height}, "
                f"{self._fps:.3f}fps, total_frames={self._total_frames}, "
                f"target_size={self._target_width}x{self._target_height}"
            )

        except av.error.FileNotFoundError:
            raise FileNotFoundError(f"File not found: {self._source_path}")
        except Exception as e:
            logger.exception(f"Failed to open video: {self._source_path}")
            raise RuntimeError(f"Failed to open video: {e}") from e

    def close(self) -> None:
        """关闭并释放资源"""
        self._frame_iterator = None
        self._held_frame = None

        if self._container is not None:
            try:
                self._container.close()
            except Exception as e:
                logger.warning(f"Error closing container: {e}")
            finally:
                self._container = None
                self._video_stream = None

        self._is_open = False
        self._current_frame = 0

    def __enter__(self) -> VideoFrameSampler:
        """上下文管理器入口"""
        self.open()
        return self

    def __exit__(self, exc_type: Optional[type[BaseException]], exc_val: Optional[BaseException],
                 exc_tb: Optional[TracebackType]) -> None:
        """上下文管理器退出"""
        self.close()

    # ==================== 缩放计算 ====================

    def _calculate_target_size(self) -> None:
        """
        计算缩放目标尺寸。

        保持 aspect ratio，宽度固定为 sample_width，
        高度不超过 max_sample_height。
        """
        if self._original_width <= 0 or self._original_height <= 0:
            return

        # 固定宽度为 sample_width
        self._target_width = self._settings.sample_width

        # 计算保持 aspect ratio 的高度
        aspect_ratio = self._original_height / self._original_width
        calculated_height = int(self._target_width * aspect_ratio)

        # 限制最大高度
        self._target_height = min(calculated_height, self._settings.max_sample_height)

        # 确保高度至少为 1
        self._target_height = max(1, self._target_height)

    # ==================== 帧采样 ====================

    def sample_frames(
        self,
        start_frame: int | None = None,
        end_frame: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Generator[SampledFrame, None, None]:
        """
        采样视频帧。

        顺序解码视频帧，缩放后返回 SampledFrame。

        Args:
            start_frame: 起始帧（默认使用 settings.start_source_frame）
            end_frame: 结束帧（默认使用 settings.end_source_frame 或视频末尾）
            progress_callback: 进度回调函数 (current_frame, total_frames)

        Returns:
            SampledFrame 生成器

        Raises:
            CancelledError: 任务被取消
            RuntimeError: 媒体源未打开
        """
        if not self._is_open:
            raise RuntimeError("Sampler not open. Call open() first.")

        # 使用设置中的范围，或参数指定范围
        start = start_frame if start_frame is not None else self._settings.start_source_frame
        end = end_frame if end_frame is not None else self._settings.end_source_frame
        if end is None:
            end = self._total_frames

        # 验证范围
        start = max(0, start)
        end = min(end, self._total_frames)

        if start >= end:
            logger.warning(f"Invalid frame range: start={start}, end={end}")
            return

        # 记录实际采样帧数
        actual_total = end - start

        # 如果起始帧不是 0，需要 seek
        if start > 0 and self._current_frame != start:
            self._seek_to_frame(start)

        # 创建帧迭代器
        if self._frame_iterator is None:
            self._frame_iterator = self._container.decode(self._video_stream)

        # 进度更新节流（每 100 帧）
        progress_throttle_interval = 100
        last_progress_frame = 0

        # 有序转换流水线：解码线程提交 swscale 转换任务到线程池，
        # 按提交顺序消费（future.result() 保序），转换与指标计算重叠执行。
        # 队列有界（CONVERT_QUEUE_DEPTH），内存占用可控。
        executor = ThreadPoolExecutor(
            max_workers=CONVERT_WORKERS,
            thread_name_prefix="scene-cut-convert",
        )
        pending: deque[tuple[Future[np.ndarray], FrameTimePoint]] = deque()

        def _next_converted_frame() -> SampledFrame:
            """按序取回一个已提交的转换结果。"""
            future, time_point = pending.popleft()
            return SampledFrame(
                time_point=time_point,
                frame_data=future.result(),
                width=self._target_width,
                height=self._target_height,
                original_width=self._original_width,
                original_height=self._original_height,
            )

        try:
            for frame in self._frame_stream():
                # 检查取消
                self._cancel_token.check_and_raise()

                # 跳过起始帧之前的帧
                if self._current_frame < start:
                    self._current_frame += 1
                    continue

                # 到达结束帧，停止
                if self._current_frame >= end:
                    break

                # 时间信息在解码线程提取（轻量），重转换异步执行
                pending.append((
                    executor.submit(self._convert_frame_data, frame),
                    self._extract_time_point(frame),
                ))

                # 进度回调（节流）
                if progress_callback is not None:
                    if self._current_frame - last_progress_frame >= progress_throttle_interval:
                        progress_callback(self._current_frame - start, actual_total)
                        last_progress_frame = self._current_frame

                self._current_frame += 1

                # 有界预读：队列满时按序吐出，避免无界占用内存
                if len(pending) >= CONVERT_QUEUE_DEPTH:
                    yield _next_converted_frame()

            # 排空剩余转换结果（保持顺序）
            while pending:
                yield _next_converted_frame()

            # 最终进度更新
            if progress_callback is not None:
                progress_callback(actual_total, actual_total)

        except CancelledError:
            logger.info(f"Frame sampling cancelled at frame {self._current_frame}")
            raise
        except StopIteration:
            # 视频结束
            pass
        except Exception as e:
            logger.error(f"Frame sampling error at frame {self._current_frame}: {e}")
            raise
        finally:
            if pending:
                for future, _ in pending:
                    future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

    def sample_single_frame(
        self,
        frame_index: int,
        time_index: Optional[Any] = None,
    ) -> SampledFrame | None:
        """
        采样单个帧。

        用于预览或缩略图生成。会 seek 到目标帧。

        Args:
            frame_index: 目标帧号
            time_index: Optional SourceFrameTimeIndex for accurate PTS-based seeking

        Returns:
            SampledFrame 或 None（如果帧不存在）
        """
        if not self._is_open:
            raise RuntimeError("Sampler not open. Call open() first.")

        if frame_index < 0 or frame_index >= self._total_frames:
            return None

        if self._container is None or self._video_stream is None:
            return None

        # Seek to the nearest earlier keyframe, then decode forward and return
        # the first actual decoded frame whose timestamp maps to frame_index.
        # Prefer PTS from SourceFrameTimeIndex for VFR accuracy.
        target_pts = None

        # Try using SourceFrameTimeIndex for exact PTS (VFR-safe)
        if time_index is not None:
            try:
                point = time_index.get_point_for_frame(frame_index)
                if point is not None and point.pts is not None:
                    target_pts = point.pts
            except Exception:
                target_pts = None

        # Fallback to fps-based estimation
        if target_pts is None:
            if self._fps > 0 and self._time_base > 0:
                target_pts = int(float(frame_index) / self._fps / self._time_base)
            else:
                target_pts = frame_index

        try:
            self._container.seek(
                target_pts,
                stream=self._video_stream,
                any_frame=False,
            )
            self._frame_iterator = self._container.decode(self._video_stream)
            self._current_frame = 0
            self._held_frame = None

            for frame in self._frame_iterator:
                current_pts = frame.pts if hasattr(frame, 'pts') else None
                if current_pts is not None and self._time_base > 0:
                    current_seconds = float(current_pts) * self._time_base
                    current_frame = int(current_seconds * self._fps)
                else:
                    current_frame = self._current_frame

                if current_frame >= frame_index:
                    self._current_frame = frame_index
                    sampled = self._convert_frame(frame)
                    self._current_frame += 1
                    return sampled
                self._current_frame = current_frame + 1
        except Exception as e:
            logger.warning(f"Single-frame seek failed at {frame_index}: {e}")
            self._frame_iterator = None
            self._held_frame = None
            self._current_frame = 0

        return None

    def sample_next_frame(
        self,
        expected_frame: int | None = None,
    ) -> SampledFrame | None:
        """Decode the next frame from the current sequential cursor.

        ``sample_single_frame`` is intentionally seek-oriented and is useful
        for candidate selection, but it is the wrong primitive for playback:
        calling it for every frame repeatedly seeks to a GOP keyframe.  This
        method consumes the existing decode iterator instead, so consecutive
        preview requests share the decoder's forward progress.

        Args:
            expected_frame: Optional logical frame number expected at the
                cursor.  A mismatch returns ``None`` without consuming a
                frame, allowing the caller to fall back to a random seek.
        """
        if not self._is_open:
            raise RuntimeError("Sampler not open. Call open() first.")
        if self._container is None or self._video_stream is None:
            return None
        if expected_frame is not None and self._current_frame != expected_frame:
            return None

        if self._frame_iterator is None:
            self._frame_iterator = self._container.decode(self._video_stream)

        try:
            for frame in self._frame_stream():
                current_pts = frame.pts if hasattr(frame, "pts") else None
                if current_pts is not None and self._time_base > 0:
                    current_frame = int(
                        float(current_pts) * self._time_base * self._fps
                    )
                else:
                    current_frame = self._current_frame

                # Seek pre-roll can leave the iterator a little before the
                # logical cursor.  Consume those frames without converting
                # them, just as the sequential sampler does.
                if current_frame < self._current_frame:
                    self._current_frame = current_frame + 1
                    continue
                if expected_frame is not None and current_frame != expected_frame:
                    return None

                sampled = self._convert_frame(frame)
                self._current_frame = current_frame + 1
                return sampled
        except Exception as e:
            logger.warning(f"Sequential frame decode failed at {self._current_frame}: {e}")
            self._frame_iterator = None

        return None

    def build_time_index(
        self,
        start_frame: int | None = None,
        end_frame: int | None = None,
    ) -> tuple[FrameTimePoint, ...]:
        """
        构建源时间索引。

        快速遍历所有帧，只记录时间信息，不解码完整帧数据。
        用于 VFR 处理和时间映射。

        Args:
            start_frame: 起始帧
            end_frame: 结束帧

        Returns:
            FrameTimePoint 元组
        """
        if not self._is_open:
            raise RuntimeError("Sampler not open. Call open() first.")

        start = start_frame if start_frame is not None else self._settings.start_source_frame
        end = end_frame if end_frame is not None else self._settings.end_source_frame
        if end is None:
            end = self._total_frames

        start = max(0, start)
        end = min(end, self._total_frames)

        if start >= end:
            return ()

        # Seek 到起始帧
        if start > 0 and self._current_frame != start:
            self._seek_to_frame(start)

        if self._frame_iterator is None:
            self._frame_iterator = self._container.decode(self._video_stream)

        time_points: list[FrameTimePoint] = []

        # 记录上一个 PTS，用于检测重复时间
        last_pts: int | None = None

        try:
            for frame in self._frame_stream():
                self._cancel_token.check_and_raise()

                if self._current_frame < start:
                    self._current_frame += 1
                    continue

                if self._current_frame >= end:
                    break

                # 提取时间信息
                pts = frame.pts if hasattr(frame, 'pts') else None
                seconds = 0.0
                duration_seconds = None

                # 计算秒数
                if pts is not None and self._time_base > 0:
                    seconds = float(pts) * self._time_base
                elif self._fps > 0:
                    seconds = float(self._current_frame) / self._fps

                # 帧持续时间
                if hasattr(frame, 'duration') and frame.duration is not None:
                    duration_seconds = float(frame.duration) * self._time_base

                # 是否为关键帧
                is_keyframe = frame.is_keyframe if hasattr(frame, 'is_keyframe') else False

                # 检测重复时间点（VFR 异常）
                is_duplicate_time = pts is not None and pts == last_pts

                # 创建时间点
                time_point = FrameTimePoint(
                    source_frame=self._current_frame,
                    pts=pts,
                    seconds=seconds,
                    duration_seconds=duration_seconds,
                    is_keyframe=is_keyframe,
                    is_duplicate_time=is_duplicate_time,
                )
                time_points.append(time_point)

                last_pts = pts
                self._current_frame += 1

        except CancelledError:
            logger.info(f"Time index building cancelled at frame {self._current_frame}")
            raise

        return tuple(time_points)

    # ==================== 内部方法 ====================

    def _frame_stream(self) -> Iterator[Any]:
        """解码帧流：优先吐出 seek 预滚持有的帧，再接续解码迭代器。

        AUD-17920：_seek_to_frame 将目标起始帧保存在 _held_frame 中而非
        丢弃，本方法保证采样循环拿到的第一帧就是真正的起始帧。
        """
        if self._held_frame is not None:
            held, self._held_frame = self._held_frame, None
            yield held
        assert self._frame_iterator is not None
        yield from self._frame_iterator

    def _seek_to_frame(self, frame_index: int) -> None:
        """
        Seek 到目标帧。

        使用 PyAV seek，然后解码到目标帧。

        Args:
            frame_index: 目标帧号
        """
        if self._container is None or self._video_stream is None:
            return

        # 计算目标 PTS（假设 CFR）
        if self._fps > 0 and self._time_base > 0:
            target_pts = int(float(frame_index) / self._fps / self._time_base)
        else:
            target_pts = frame_index

        # Seek 到目标 PTS（实际上是最近的关键帧）
        try:
            self._container.seek(
                target_pts,
                stream=self._video_stream,
                any_frame=False,
            )
        except Exception as e:
            logger.warning(f"Seek failed: {e}, will decode from beginning")
            # 重置迭代器，从头开始
            self._frame_iterator = None
            self._held_frame = None
            self._current_frame = 0
            return

        # 重置迭代器
        self._frame_iterator = self._container.decode(self._video_stream)
        self._held_frame = None

        # 需要解码到目标帧（从关键帧到目标帧）
        # 这里先解码但丢弃帧数据，只更新 current_frame
        self._current_frame = 0

        # 由于不知道 seek 到了哪个关键帧，
        # 使用 PTS 来确定当前帧位置
        for frame in self._frame_iterator:
            current_pts = frame.pts if hasattr(frame, 'pts') else None
            if current_pts is not None and self._time_base > 0:
                current_seconds = float(current_pts) * self._time_base
                current_frame = int(current_seconds * self._fps)
            else:
                current_frame = self._current_frame

            if current_frame >= frame_index:
                # Hold the first frame at/after the target instead of
                # discarding it: the sampling loops consume it as their
                # first (correctly labelled) frame (AUD-17920).
                self._held_frame = frame
                self._current_frame = current_frame
                return

            self._current_frame = current_frame + 1

    def _convert_frame(self, frame: Any) -> SampledFrame:
        """
        转换 PyAV 帧到 SampledFrame（同步路径）。

        解码并缩放帧数据，提取时间信息。
        用于单帧采样（预览/缩略图）；批量采样走异步流水线。

        Args:
            frame: PyAV VideoFrame

        Returns:
            SampledFrame
        """
        return SampledFrame(
            time_point=self._extract_time_point(frame),
            frame_data=self._convert_frame_data(frame),
            width=self._target_width,
            height=self._target_height,
            original_width=self._original_width,
            original_height=self._original_height,
        )

    def _extract_time_point(self, frame: Any) -> FrameTimePoint:
        """
        从 PyAV 帧提取时间信息（轻量，不含像素转换）。

        Args:
            frame: PyAV VideoFrame

        Returns:
            FrameTimePoint
        """
        # 提取时间信息
        pts = frame.pts if hasattr(frame, 'pts') else None
        seconds = 0.0
        duration_seconds = None

        if pts is not None and self._time_base > 0:
            seconds = float(pts) * self._time_base
        elif self._fps > 0:
            seconds = float(self._current_frame) / self._fps

        if hasattr(frame, 'duration') and frame.duration is not None:
            duration_seconds = float(frame.duration) * self._time_base

        is_keyframe = frame.is_keyframe if hasattr(frame, 'is_keyframe') else False

        return FrameTimePoint(
            source_frame=self._current_frame,
            pts=pts,
            seconds=seconds,
            duration_seconds=duration_seconds,
            is_keyframe=is_keyframe,
        )

    def _convert_frame_data(self, frame: Any) -> np.ndarray:
        """
        转换 PyAV 帧像素数据为缩放后的 BGR 数组（重量级，可异步执行）。

        性能关键路径（planar-scale-first）：
        - 先在源平面像素格式（yuv422p10le 等）下 swscale 缩放到分析分辨率，
          再对 256x144 小图做 YUV→BGR 转换。相比"全分辨率 bgr24 转换 +
          INTER_AREA 全幅缩小"，4K 10/12bit 素材实测转换成本降低 40-60%。
        - 不使用一次性 reformat(width, height, format="bgr24")：对高位深
          输入 swscale 走通用 scaler 路径，ProRes4444 实测反而更慢。
        - 与全分辨率路径的重采样差异约 1.0/255；对相邻帧差指标的影响
          <0.005（0-1 量表），探测出的 cut 帧位与原路径完全一致。
        - 该方法在转换线程池中执行（swscale 调用释放 GIL）。

        Args:
            frame: PyAV VideoFrame

        Returns:
            缩放后的 BGR 帧（target_width x target_height x 3, uint8）
        """
        try:
            small = frame.reformat(
                width=self._target_width,
                height=self._target_height,
            )
            frame_bgr = small.reformat(format="bgr24").to_ndarray()

        except Exception as e:
            # 记录失败并熔断：连续失败说明媒体/解码路径本身损坏，
            # 静默回退全黑帧会把伪黑帧注入检测管线（虚假 BLACK_GAP、
            # 掩盖真实切点）。偶发失败仍回退黑帧保持容错（AUD-17921）。
            with self._convert_stats_lock:
                self._total_convert_failures += 1
                self._consecutive_convert_failures += 1
                consecutive = self._consecutive_convert_failures
            logger.warning(f"Frame conversion error: {e}")
            if consecutive >= MAX_CONSECUTIVE_CONVERT_FAILURES:
                raise RuntimeError(
                    f"Frame conversion failed {consecutive} consecutive "
                    f"times (last error: {e})"
                ) from e
            # 返回空白帧
            frame_bgr = np.zeros(
                (self._target_height, self._target_width, 3),
                dtype=np.uint8
            )
        else:
            with self._convert_stats_lock:
                self._consecutive_convert_failures = 0

        return frame_bgr


# =============================================================================
# 采样器工厂函数
# =============================================================================

def create_sampler(
    source_path: str,
    settings: SceneCutSettings,
    cancel_token: CancelToken | None = None,
) -> VideoFrameSampler:
    """
    创建帧采样器。

    Args:
        source_path: 源文件路径
        settings: 检测设置
        cancel_token: 取消 token

    Returns:
        VideoFrameSampler 实例
    """
    return VideoFrameSampler(source_path, settings, cancel_token)


def sample_frames_for_detection(
    source_path: str,
    settings: SceneCutSettings,
    cancel_token: CancelToken | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Generator[SampledFrame, None, None]:
    """
    为检测采样帧。

    创建采样器并采样帧，自动管理生命周期。

    Args:
        source_path: 源文件路径
        settings: 检测设置
        cancel_token: 取消 token
        progress_callback: 进度回调

    Returns:
        SampledFrame 生成器
    """
    sampler = create_sampler(source_path, settings, cancel_token)

    with sampler:
        yield from sampler.sample_frames(progress_callback=progress_callback)


def build_time_index_for_source(
    source_path: str,
    settings: SceneCutSettings,
    cancel_token: CancelToken | None = None,
) -> tuple[FrameTimePoint, ...]:
    """
    构建源时间索引。

    创建采样器并构建时间索引，自动管理生命周期。

    Args:
        source_path: 源文件路径
        settings: 检测设置
        cancel_token: 取消 token

    Returns:
        FrameTimePoint 元组
    """
    sampler = create_sampler(source_path, settings, cancel_token)

    with sampler:
        return sampler.build_time_index()
