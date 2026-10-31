"""打标流水线编排器。

把多个 PipelineStep 串起来，按 media_type 分派到不同的 pipeline。
支持：
- 批量处理（遍历目录/文件列表）
- 分阶段执行（24GB 显存：大模型分时加载）
- 断点续跑（每步 is_done 检查）
- 进度回调（UI 显示进度）
- 错误隔离（单个素材失败不中断整批）

三类素材的默认 pipeline：
    video: ingest → scene_cut → asr → transcript_embed
           → visual_embed_video → video_caption → face → audio_features
    audio: ingest → audio_features
    image: ingest → visual_embed → image_caption

人脸角色聚类（cluster_characters）不在 pipeline 内自动跑——它是整库级别的
后处理（跨素材聚合同一角色），在 index 完成后单独调用。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from apps.topos_vault.db.repository import MediaSource, VaultRepository
from apps.topos_vault.pipeline.ingest_step import IngestStep
from apps.topos_vault.pipeline.steps import PipelineStep, StepResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 扩展名分类（与 cli.py 的 _EXT_* 对齐）
# ---------------------------------------------------------------------- #

_EXT_MAP = {
    "video": {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".wmv", ".flv", ".webm",
              ".mxf", ".r3d", ".ari", ".prores"},
    "audio": {".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg", ".wma", ".aiff"},
    "image": {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp", ".heic",
              ".exr", ".dpx", ".psd", ".cr2", ".nef", ".arw", ".ai"},
}


def classify_media_type(path: str) -> str | None:
    """按扩展名推断素材类型。None = 非素材文件。"""
    ext = os.path.splitext(path)[1].lower()
    for mtype, exts in _EXT_MAP.items():
        if ext in exts:
            return mtype
    return None


# ---------------------------------------------------------------------- #
# 进度回调
# ---------------------------------------------------------------------- #

@dataclass
class ProgressInfo:
    """进度信息（传给 progress_callback）。"""
    current: int
    total: int
    current_path: str
    current_step: str
    phase: str = ""        # 阶段名（如 "画面打标"）

    @property
    def percent(self) -> float:
        return (self.current / self.total * 100) if self.total else 0.0


ProgressCallback = Callable[[ProgressInfo], None]


# ---------------------------------------------------------------------- #
# 批次报告
# ---------------------------------------------------------------------- #

@dataclass
class BatchReport:
    """一批素材的打标汇总。"""
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[dict] = field(default_factory=list)

    def add(self, path: str, step_results: list[StepResult]) -> None:
        self.total += 1
        any_failed = any(r.is_failed for r in step_results)
        any_done = any(r.is_done for r in step_results)
        if any_failed:
            self.failed += 1
        elif any_done:
            self.done += 1
        else:
            self.skipped += 1

        self.results.append({
            "path": path,
            "steps": [
                {"step": r.step_name, "status": r.status.value, "detail": r.detail}
                for r in step_results
            ],
        })

    def summary(self) -> str:
        return (
            f"打标完成：{self.done} 成功 / {self.skipped} 跳过 / "
            f"{self.failed} 失败 / {self.total} 总计"
        )


# ---------------------------------------------------------------------- #
# Orchestrator
# ---------------------------------------------------------------------- #

class Orchestrator:
    """打标流水线编排器。

    Usage:
        orch = Orchestrator(repo)
        report = orch.index_directory("/path/to/media", media_type="auto")
        print(report.summary())
    """

    def __init__(self, repo: VaultRepository):
        self.repo = repo
        # steps 按 media_type 分组，每组的顺序就是执行顺序
        # video:  ingest → scene_cut → asr → transcript_embed
        #         → visual_embed_video → video_caption → audio_features
        # audio:  ingest → audio_features
        # image:  ingest → visual_embed → image_caption
        from apps.topos_vault.pipeline.visual_step import (
            VisualEmbedStep, ImageCaptionStep, VisualEmbedVideoStep, VideoCaptionStep,
        )
        from apps.topos_vault.pipeline.transcript_step import TranscriptEmbedStep
        from apps.topos_vault.pipeline.scene_cut_step import SceneCutStep
        from apps.topos_vault.pipeline.asr_step import AsrStep
        from apps.topos_vault.pipeline.audio_features_step import AudioFeaturesStep
        from apps.topos_vault.pipeline.face_step import FaceStep

        self._steps: dict[str, list[PipelineStep]] = {
            "video": [
                IngestStep(repo),
                SceneCutStep(repo),
                AsrStep(repo),
                TranscriptEmbedStep(repo),
                VisualEmbedVideoStep(repo),
                VideoCaptionStep(repo),
                FaceStep(repo),
                AudioFeaturesStep(repo),
            ],
            "audio": [IngestStep(repo), AudioFeaturesStep(repo)],
            "image": [IngestStep(repo), VisualEmbedStep(repo), ImageCaptionStep(repo)],
            "image_sequence": [IngestStep(repo), VisualEmbedStep(repo), ImageCaptionStep(repo)],
        }

    def add_step(self, media_type: str, step: PipelineStep) -> None:
        """给某类素材追加一个步骤（后续阶段扩展用）。

        Example:
            orch.add_step("video", SceneCutStep(repo))
            orch.add_step("video", AsrStep(repo))
        """
        if media_type not in self._steps:
            self._steps[media_type] = []
        self._steps[media_type].append(step)

    def index_file(
        self, path: str, media_type: str = "auto",
        progress: ProgressCallback | None = None,
    ) -> list[StepResult]:
        """给单个文件打标。

        Args:
            path: 文件绝对路径。
            media_type: 'auto' 按扩展名推断，或显式指定。
            progress: 进度回调。

        Returns:
            每步的执行结果列表。
        """
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return [StepResult.failed(path, "ingest", "文件不存在", recoverable=False)]

        if media_type == "auto":
            media_type = classify_media_type(path)
            if media_type is None:
                return [StepResult.skipped(path, "ingest", "非素材文件")]

        # 构造临时 MediaSource（fingerprint 由 IngestStep 填）
        # 如果库里已有该路径的记录，先填回真实 fingerprint——
        # 这样第二次跑（IngestStep 被跳过）时，后续步骤也能拿到正确 fingerprint
        existing = self.repo.get_media_source_by_path(path)
        source = MediaSource(
            fingerprint=existing.fingerprint if existing else "",
            media_type=media_type,
            path_original=path,
            path_current=path,
            title=os.path.basename(path),
        )

        steps = self._steps.get(media_type, [])
        results: list[StepResult] = []
        for i, step in enumerate(steps):
            if progress:
                progress(ProgressInfo(
                    current=i + 1, total=len(steps),
                    current_path=path, current_step=step.step_name,
                ))
            result = step.execute(source)
            results.append(result)
            # 失败且不可恢复 → 不跑后续步骤
            if result.is_failed and not result.recoverable:
                break

        return results

    def index_directory(
        self, root: str, media_type: str = "auto", recursive: bool = True,
        progress: ProgressCallback | None = None,
    ) -> BatchReport:
        """给目录下所有素材打标。

        Args:
            root: 目录路径。
            media_type: 'auto' 按扩展名，或限定类型。
            recursive: 是否递归子目录。
            progress: 进度回调（每个文件调一次）。

        Returns:
            BatchReport 汇总。
        """
        root = os.path.abspath(root)
        if not os.path.exists(root):
            raise FileNotFoundError(root)

        # 收集文件
        files: list[str] = []
        if os.path.isfile(root):
            files = [root]
        elif recursive:
            for dirpath, _dirs, fnames in os.walk(root):
                for name in fnames:
                    if name.startswith("."):
                        continue
                    full = os.path.join(dirpath, name)
                    if media_type == "auto":
                        if classify_media_type(full) is not None:
                            files.append(full)
                    else:
                        ext = os.path.splitext(name)[1].lower()
                        if ext in _EXT_MAP.get(media_type, set()):
                            files.append(full)
        else:
            files = [
                os.path.join(root, f) for f in os.listdir(root)
                if not f.startswith(".") and os.path.isfile(os.path.join(root, f))
                and (media_type == "auto" and classify_media_type(os.path.join(root, f))
                     or os.path.splitext(f)[1].lower() in _EXT_MAP.get(media_type, set()))
            ]

        report = BatchReport()
        total = len(files)
        for idx, path in enumerate(files):
            if progress:
                progress(ProgressInfo(
                    current=idx + 1, total=total,
                    current_path=path, current_step="pipeline",
                    phase="打标中",
                ))
            results = self.index_file(path, media_type=media_type)
            report.add(path, results)

        return report

    def list_pipeline_steps(self, media_type: str) -> list[str]:
        """查看某类素材的 pipeline 步骤（诊断用）。"""
        return [s.step_name for s in self._steps.get(media_type, [])]
