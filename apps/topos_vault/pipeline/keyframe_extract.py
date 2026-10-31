"""视频关键帧抽取（PyAV）。

在镜头片段（clip）的时间范围内抽取代表性关键帧，存到 cache/<fp>/kf_xxxx.jpg，
供画面向量/画面理解/人脸步骤复用。

设计：
    - 每个 clip 抽 1-N 帧（默认每 clip 取中间帧；长 clip 多取几帧）
    - 存到 cache 目录（paths.get_cache_dir(fingerprint)/kf_<clip_idx>_<n>.jpg）
    - 幂等：已存在的帧不重抽
    - PyAV 未装 → 返回空（调用方降级）

为什么用 PyAV：项目已有依赖（src/shared/media/probe.py 用），自带 ffmpeg 库，
无系统依赖，比 subprocess ffmpeg 快（避免进程启动 + pipe 开销）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import av  # type: ignore
    _HAS_AV = True
except ImportError:
    _HAS_AV = False


@dataclass(frozen=True)
class Keyframe:
    """抽出的关键帧。"""
    clip_id: str
    clip_index: int
    frame_index: int          # 该 clip 内的第几帧（0 起）
    timestamp_sec: float      # 在源视频中的时间戳（秒）
    path: str                 # 缓存的 jpg 路径


def is_keyframe_available() -> bool:
    """PyAV 是否可用。"""
    return _HAS_AV


def extract_keyframes(
    video_path: str,
    clips: list,  # list[Clip]，但延迟引用避免循环导入
    fingerprint: str,
    *,
    frames_per_clip: int = 1,
    cache_dir_factory=None,
) -> list[Keyframe]:
    """给一组 clip 抽关键帧。

    Args:
        video_path: 源视频路径。
        clips: Clip 列表（从 repository.list_clips 拿）。
        fingerprint: 素材指纹（决定缓存目录）。
        frames_per_clip: 每个 clip 抽几帧（默认 1 = 取中间帧）。
            长 clip（>10s）会自动多取（最多 3 帧）。
        cache_dir_factory: 返回缓存目录的 callable(fingerprint) -> Path。
            None 则用 paths.get_cache_dir。

    Returns:
        Keyframe 列表（可能为空：PyAV 没装 / 解码失败 / 无 clip）。
    """
    if not _HAS_AV or not clips:
        return []

    if cache_dir_factory is None:
        from apps.topos_vault.paths import get_cache_dir
        cache_dir_factory = get_cache_dir

    cache_dir = cache_dir_factory(fingerprint)

    # 复用一个 PyAV container（避免每个 clip 重新打开）
    try:
        container = av.open(video_path)
    except Exception as e:
        logger.debug("关键帧抽取：打开视频失败 %s", e)
        return []

    try:
        stream = container.streams.video[0]
        time_base = stream.time_base
        if time_base is None or time_base <= 0:
            # 某些流没 time_base，用容器默认
            time_base = av.time_base

        results: list[Keyframe] = []
        for clip in clips:
            # 决定该 clip 抽几帧
            n = frames_per_clip
            if clip.duration_sec and clip.duration_sec > 10.0:
                n = max(n, min(3, int(clip.duration_sec / 5)))

            timestamps = _pick_timestamps(clip.start_sec, clip.end_sec, n)
            for fi, ts in enumerate(timestamps):
                out_path = str(cache_dir / f"kf_{clip.clip_index:04d}_{fi:02d}.jpg")
                # 幂等：已存在不重抽
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    results.append(Keyframe(
                        clip_id=clip.clip_id, clip_index=clip.clip_index,
                        frame_index=fi, timestamp_sec=ts, path=out_path,
                    ))
                    continue

                frame = _seek_and_decode(container, stream, ts, time_base)
                if frame is None:
                    continue
                if _save_frame_jpg(frame, out_path):
                    results.append(Keyframe(
                        clip_id=clip.clip_id, clip_index=clip.clip_index,
                        frame_index=fi, timestamp_sec=ts, path=out_path,
                    ))

        return results
    finally:
        container.close()


def _pick_timestamps(start_sec: float, end_sec: float, n: int) -> list[float]:
    """在一个 clip 的 [start, end] 内均匀取 n 个时间戳。"""
    if n <= 0 or end_sec <= start_sec:
        return [(start_sec + end_sec) / 2.0] if end_sec > start_sec else [start_sec]
    if n == 1:
        return [(start_sec + end_sec) / 2.0]
    # 均匀取：避开首尾边界（首帧常黑场，尾帧常切黑），取中间区域
    step = (end_sec - start_sec) / (n + 1)
    return [start_sec + step * (i + 1) for i in range(n)]


def _seek_and_decode(container, stream, ts_sec: float, time_base):
    """seek 到 ts_sec 并解码出一帧。

    PyAV 的 seek 是近似定位，需要解码到目标位置。
    """
    try:
        # 转 stream time_base 单位
        target = int(ts_sec / float(time_base))
        container.seek(target, stream=stream)

        for frame in container.decode(stream):
            frame_ts = float(frame.pts * time_base) if frame.pts else 0.0
            if frame_ts >= ts_sec - 0.1:
                return frame
        # 没找到精确帧，取最后一帧（fallback）
        return None
    except Exception as e:
        logger.debug("seek/解码失败 ts=%.2f：%s", ts_sec, e)
        return None


def _save_frame_jpg(frame, out_path: str) -> bool:
    """把 PyAV frame 存成 jpg。"""
    try:
        img = frame.to_image()  # PIL Image
        img.save(out_path, "JPEG", quality=90)
        return True
    except Exception as e:
        logger.debug("存帧失败 %s：%s", out_path, e)
        return False
