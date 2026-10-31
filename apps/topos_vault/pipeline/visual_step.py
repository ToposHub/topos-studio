"""画面打标步骤：给图片/视频镜头抽画面向量 + 生成 caption。

视频和图片共用本步骤——"理解画面"与素材是视频还是图片无关。
视频走镜头级（每个 clip 的关键帧），图片走整图级。

产出：
    - visual_embed → 写入 visual.faiss（向量检索用）
    - visual_caption → 写入 image_meta / clip（SQL 检索用）

包含 4 个 step 类：
    VisualEmbedStep        图片画面向量（整图）
    ImageCaptionStep       图片 caption/tag
    VisualEmbedVideoStep   视频镜头画面向量（每个 clip 关键帧）
    VideoCaptionStep       视频镜头 caption（写 clip.shot_type/keyframes）
"""

from __future__ import annotations

import logging
import os
from typing import Any

from apps.topos_vault.db.faiss_store import get_vector_store
from apps.topos_vault.db.repository import MediaSource, VaultRepository
from apps.topos_vault.pipeline.steps import PipelineStep, StepSkipped
from apps.topos_vault.providers import Capability, get_provider

logger = logging.getLogger(__name__)


class VisualEmbedStep(PipelineStep):
    """画面向量嵌入步骤（视频镜头 + 图片共用）。

    对每个素材的关键帧（视频）或整图（图片）调 visual_embed provider，
    把向量写入 visual.faiss，并在 vector_index 表登记映射。
    """

    step_name = "visual_embed"
    applies_to = ("image", "image_sequence")  # 视频镜头级别在 SceneCutStep 之后才跑

    def __init__(self, repo: VaultRepository):
        super().__init__(repo)

    def _run(self, source: MediaSource) -> str:
        provider = get_provider(Capability.VISUAL_EMBED)
        if not provider.is_available():
            raise StepSkipped(self.step_name, source.fingerprint,
                              "visual_embed provider 不可用（模型未装）")

        path = source.path_current or source.path_original
        if not os.path.exists(path):
            raise StepSkipped(self.step_name, source.fingerprint, "文件不存在")

        # 抽向量
        result = provider.embed_image(path)
        if len(result.vector) != result.dim:
            raise StepSkipped(self.step_name, source.fingerprint, "向量维度异常")

        # 写入 visual.faiss（id 用 fingerprint）
        store = get_vector_store("visual", dim=result.dim)
        store.add(source.fingerprint, result.vector)
        store.save()

        # 在 vector_index 表登记
        self.repo.register_vector(
            index_name="visual",
            ref_table="media_source",
            ref_id=source.fingerprint,
            model=result.model,
            dim=result.dim,
        )

        return f"dim={result.dim} model={result.model}"

    def is_done(self, source: MediaSource) -> bool:
        """该素材是否已在 visual 索引里。"""
        store = get_vector_store("visual")
        return store.has(source.fingerprint)


class ImageCaptionStep(PipelineStep):
    """图片 caption/tag 生成步骤。

    用 visual_caption provider 给图片生成描述和标签，
    写入 image_meta（SQL 检索用）。
    """

    step_name = "image_caption"
    applies_to = ("image", "image_sequence")

    def __init__(self, repo: VaultRepository):
        super().__init__(repo)

    def _run(self, source: MediaSource) -> str:
        provider = get_provider(Capability.VISUAL_CAPTION)
        if not provider.is_available():
            raise StepSkipped(self.step_name, source.fingerprint,
                              "visual_caption provider 不可用")

        path = source.path_current or source.path_original
        if not os.path.exists(path):
            raise StepSkipped(self.step_name, source.fingerprint, "文件不存在")

        result = provider.caption_image(path)

        # 写入 image_meta
        from apps.topos_vault.db.repository import ImageMeta
        self.repo.upsert_image_meta(ImageMeta(
            fingerprint=source.fingerprint,
            tags=result.tags,
            caption=result.caption,
        ))

        return f"caption={result.caption[:30]}... tags={result.tags}"

    def is_done(self, source: MediaSource) -> bool:
        """是否已有 caption。"""
        meta = self.repo.get_image_meta(source.fingerprint)
        return meta is not None and bool(meta.caption)


# ---------------------------------------------------------------------- #
# 视频镜头级画面打标（在 scene_cut 之后跑）
# ---------------------------------------------------------------------- #

class VisualEmbedVideoStep(PipelineStep):
    """视频镜头画面向量嵌入步骤。

    遍历每个 clip 的关键帧，调 visual_embed provider 把向量写入 visual.faiss
    （id 用 clip_id），并在 vector_index 表登记（ref_table=clip）。

    前置依赖：scene_cut（需要 clip 记录）。
    """

    step_name = "visual_embed_video"
    applies_to = ("video",)

    def __init__(self, repo: VaultRepository):
        super().__init__(repo)

    def _run(self, source: MediaSource) -> str:
        provider = get_provider(Capability.VISUAL_EMBED)
        if not provider.is_available():
            raise StepSkipped(self.step_name, source.fingerprint,
                              "visual_embed provider 不可用（模型未装）")

        path = source.path_current or source.path_original
        if not os.path.exists(path):
            raise StepSkipped(self.step_name, source.fingerprint, "文件不存在")

        # 拿 clips
        clips = self.repo.list_clips(source.fingerprint)
        if not clips:
            raise StepSkipped(self.step_name, source.fingerprint,
                              "无镜头（需先跑 scene_cut）")

        # 抽关键帧
        from apps.topos_vault.pipeline.keyframe_extract import (
            extract_keyframes, is_keyframe_available,
        )
        if not is_keyframe_available():
            raise StepSkipped(self.step_name, source.fingerprint,
                              "PyAV 未安装，无法抽关键帧")

        keyframes = extract_keyframes(path, clips, source.fingerprint)
        if not keyframes:
            raise StepSkipped(self.step_name, source.fingerprint,
                              "关键帧抽取失败（解码失败或无帧）")

        # 每个 clip 用第一帧的关键帧做向量（一个 clip 一条向量，避免向量膨胀）
        # 按 clip_id 分组取首帧
        first_by_clip: dict[str, Any] = {}
        for kf in keyframes:
            if kf.clip_id not in first_by_clip:
                first_by_clip[kf.clip_id] = kf

        store = get_vector_store("visual")
        written = 0
        last_dim = 0
        last_model = ""
        for clip_id, kf in first_by_clip.items():
            # 幂等：已嵌入则跳过
            if store.has(clip_id):
                written += 1
                continue
            result = provider.embed_image(kf.path)
            if len(result.vector) != result.dim:
                continue
            store.add(clip_id, result.vector)
            last_dim = result.dim
            last_model = result.model
            self.repo.register_vector(
                index_name="visual",
                ref_table="clip",
                ref_id=clip_id,
                model=result.model,
                dim=result.dim,
            )
            written += 1

        if written > 0:
            store.save()
        return f"{written} 个镜头画面向量 dim={last_dim} model={last_model}"

    def is_done(self, source: MediaSource) -> bool:
        """所有 clip 是否都已在 visual 索引里。"""
        clips = self.repo.list_clips(source.fingerprint)
        if not clips:
            return False
        store = get_vector_store("visual")
        if store.size == 0:
            return False
        return all(store.has(c.clip_id) for c in clips)


class VideoCaptionStep(PipelineStep):
    """视频镜头 caption 步骤。

    用 visual_caption provider 给每个 clip 的关键帧生成 caption，
    写入 clip.keyframes（路径）+ shot_type（景别）。
    clip 级 caption 用 clip.status='done' 标记完成。

    前置依赖：scene_cut + 关键帧抽取。
    """

    step_name = "video_caption"
    applies_to = ("video",)

    def __init__(self, repo: VaultRepository):
        super().__init__(repo)

    def _run(self, source: MediaSource) -> str:
        provider = get_provider(Capability.VISUAL_CAPTION)
        if not provider.is_available():
            raise StepSkipped(self.step_name, source.fingerprint,
                              "visual_caption provider 不可用")

        path = source.path_current or source.path_original
        if not os.path.exists(path):
            raise StepSkipped(self.step_name, source.fingerprint, "文件不存在")

        clips = self.repo.list_clips(source.fingerprint)
        if not clips:
            raise StepSkipped(self.step_name, source.fingerprint,
                              "无镜头（需先跑 scene_cut）")

        # 抽关键帧（先确认关键帧已抽出，抽出后写回 clip.keyframes）
        from apps.topos_vault.pipeline.keyframe_extract import (
            extract_keyframes, is_keyframe_available,
        )
        if not is_keyframe_available():
            raise StepSkipped(self.step_name, source.fingerprint,
                              "PyAV 未安装，无法抽关键帧")

        keyframes = extract_keyframes(path, clips, source.fingerprint)
        if not keyframes:
            raise StepSkipped(self.step_name, source.fingerprint,
                              "关键帧抽取失败")

        # 按 clip_id 分组（一个 clip 的所有关键帧路径）
        kf_by_clip: dict[str, list[str]] = {}
        for kf in keyframes:
            kf_by_clip.setdefault(kf.clip_id, []).append(kf.path)

        captioned = 0
        for clip in clips:
            kf_paths = kf_by_clip.get(clip.clip_id, [])
            if not kf_paths:
                continue
            # 幂等：已有 keyframes 数据则跳过 caption
            if clip.keyframes and len(clip.keyframes) == len(kf_paths):
                captioned += 1
                continue
            # 用首帧生成 caption（含 shot_type）
            result = provider.caption_image(kf_paths[0])
            shot_type = result.shot_type or clip.shot_type
            self.repo.update_clip_keyframes(clip.clip_id, kf_paths, shot_type=shot_type)
            self.repo.update_clip_status(clip.clip_id, "done")
            captioned += 1

        return f"{captioned} 个镜头已 caption"

    def is_done(self, source: MediaSource) -> bool:
        """所有 clip 是否都有关键帧路径（已被 caption）。"""
        clips = self.repo.list_clips(source.fingerprint)
        if not clips:
            return False
        return all(bool(c.keyframes) for c in clips)
