"""镜头切割步骤（视频专用）。

用 PyAV 做帧差检测，把视频切成镜头片段。写入 clip 表。

设计决策：
    不直接 import 主项目的 ``src.features.edit.core.scene_cut``——那会触发
    src/ 的完整 import 链（可能拉进 Qt/OpenGL）。vault 作为独立应用要保持自包含。

    本实现用 PyAV（项目已有依赖）做简化的帧差检测：
        - 采样关键帧（每 N 帧或每 M 秒）
        - 计算相邻帧的直方图差异
        - 差异超阈值 = 镜头边界

    精度不如主项目 detector（那个有完整的 metrics + filters pipeline），
    但对"把视频切成可检索的镜头片段"够用。未来可通过环境变量切换到
    主项目 detector（subprocess 调用）。

降级安全：PyAV 未装 → 跳过（视频不会切镜头，但 ingest 仍记录整片）。
"""

from __future__ import annotations

import logging
import os

from apps.topos_vault.db.repository import Clip, MediaSource, VaultRepository
from apps.topos_vault.pipeline.steps import PipelineStep, StepSkipped

logger = logging.getLogger(__name__)

try:
    import av  # type: ignore
    import numpy as np
    _HAS_AV = True
except ImportError:
    _HAS_AV = False


# 镜头边界检测阈值（直方图差异 > 此值 = 新镜头）
_DEFAULT_THRESHOLD = 0.3
# 采样间隔（每 N 帧采一帧做比较，降低计算量）
_DEFAULT_SAMPLE_INTERVAL = 15  # 每 15 帧采一帧（约 0.5 秒 @ 30fps）
# 最小镜头时长（秒），避免过短的碎片
_MIN_SHOT_DURATION = 0.5


class SceneCutStep(PipelineStep):
    """镜头切割步骤（PyAV 帧差检测）。

    视频专用。把视频切成镜头片段写入 clip 表。
    后续步骤（ASR/画面打标/人脸）在 clip 级别工作。
    """

    step_name = "scene_cut"
    applies_to = ("video",)

    def __init__(self, repo: VaultRepository, threshold: float = _DEFAULT_THRESHOLD):
        super().__init__(repo)
        self.threshold = threshold

    def _run(self, source: MediaSource) -> str:
        if not _HAS_AV:
            raise StepSkipped(self.step_name, source.fingerprint,
                              "PyAV 未安装，无法做镜头切割")

        path = source.path_current or source.path_original
        if not os.path.exists(path):
            raise StepSkipped(self.step_name, source.fingerprint, "文件不存在")

        # PyAV 解码可能失败（文件损坏/格式不支持/无视频流）
        # 这种情况转成 skipped 而非 failed——让后续步骤有机会跑
        try:
            cuts = self._detect_scene_cuts(path)
        except Exception as e:
            # PyAV 打不开文件 → 整片作为单个 clip（如果有时长）
            logger.debug("镜头切割失败（%s），整片作为单镜头", e)
            cuts = []

        if not cuts:
            # 整片作为一个 clip
            duration = source.duration_sec or 0
            if duration > 0:
                self.repo.insert_clip(Clip(
                    clip_id="", fingerprint=source.fingerprint,
                    clip_index=0, start_sec=0, end_sec=duration,
                    duration_sec=duration, shot_type="全景",
                ))
                return "整片作为 1 个镜头（未检测到切换或解码失败）"
            raise StepSkipped(self.step_name, source.fingerprint, "无法获取时长")

        # 写入 clip 表
        for i, (start, end) in enumerate(cuts):
            duration = end - start
            if duration < _MIN_SHOT_DURATION:
                continue  # 跳过过短的
            self.repo.insert_clip(Clip(
                clip_id="", fingerprint=source.fingerprint,
                clip_index=i, start_sec=start, end_sec=end,
                duration_sec=duration,
            ))

        return f"切出 {len(cuts)} 个镜头"

    def is_done(self, source: MediaSource) -> bool:
        """该视频是否已有 clip 记录。"""
        clips = self.repo.list_clips(source.fingerprint)
        return len(clips) > 0

    def _detect_scene_cuts(self, path: str) -> list[tuple[float, float]]:
        """用 PyAV 做帧差镜头边界检测。

        Returns:
            [(start_sec, end_sec), ...] 镜头列表。
        """
        container = av.open(path)
        try:
            stream = container.streams.video[0]
            fps = float(stream.average_rate)
            total_frames = stream.frames or 0
            duration = float(container.duration / av.time_base) if container.duration else 0

            if fps <= 0 or total_frames <= 0:
                return [(0, duration)] if duration > 0 else []

            # 采样帧计算直方图差异
            boundaries: list[float] = [0.0]  # 第一个边界是 0
            prev_hist = None
            frame_idx = 0

            for frame in container.decode(stream):
                if frame_idx % _DEFAULT_SAMPLE_INTERVAL != 0:
                    frame_idx += 1
                    continue

                # 转灰度 + 计算直方图
                img = frame.to_ndarray(format="gray")
                hist = _calc_histogram(img)

                if prev_hist is not None:
                    diff = _hist_diff(prev_hist, hist)
                    if diff > self.threshold:
                        timestamp = frame_idx / fps
                        # 避免过近的边界
                        if not boundaries or timestamp - boundaries[-1] > _MIN_SHOT_DURATION:
                            boundaries.append(timestamp)

                prev_hist = hist
                frame_idx += 1

            # 最后一个边界是视频结尾
            if duration > 0:
                boundaries.append(duration)
            elif total_frames > 0:
                boundaries.append(total_frames / fps)

            # 组合成 (start, end) 列表
            cuts = []
            for i in range(len(boundaries) - 1):
                cuts.append((boundaries[i], boundaries[i + 1]))

            return cuts

        finally:
            container.close()


def _calc_histogram(img: "np.ndarray") -> "np.ndarray":
    """计算灰度图直方图（归一化）。"""
    hist = np.histogram(img.flatten(), bins=64, range=(0, 255))[0]
    total = hist.sum()
    if total > 0:
        hist = hist / total
    return hist


def _hist_diff(h1: "np.ndarray", h2: "np.ndarray") -> float:
    """两个直方图的差异（卡方距离，0-1）。"""
    diff = np.sqrt(np.sum((h1 - h2) ** 2 / (h1 + h2 + 1e-10)))
    return float(min(diff, 1.0))
