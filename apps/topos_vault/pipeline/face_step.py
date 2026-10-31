"""人脸识别步骤（视频镜头级）。

用 FACE_RECOGNIZE provider 给 clip 关键帧检测人脸，把人脸向量写入 face.faiss，
并在 face_track 表登记（一个 clip × 一张脸 = 一行 face_track）。

产出：
    - face.faiss：512 维人脸向量（id = face_track_id，str 形式）
    - face_track：clip_id / face_angle / completeness / cluster_id（聚类后填）
    - vector_index：ref_table=face_track

前置依赖：scene_cut（需要 clip + 关键帧）。

降级安全：FACE_RECOGNIZE provider 不可用（insightface 未装）→ 跳过。
"""

from __future__ import annotations

import logging
import os

from apps.topos_vault.db.faiss_store import get_vector_store
from apps.topos_vault.db.repository import FaceTrack, MediaSource, VaultRepository
from apps.topos_vault.pipeline.steps import PipelineStep, StepSkipped
from apps.topos_vault.providers import Capability, get_provider

logger = logging.getLogger(__name__)


class FaceStep(PipelineStep):
    """人脸识别步骤（视频镜头级）。

    遍历每个 clip 的关键帧，调 face_recognize provider 检测人脸，
    每张检测到的人脸：
        - 写入 face_track 表（拿到 face_id）
        - 把 512 维向量写入 face.faiss（id = str(face_id)）
        - 在 vector_index 登记（ref_table=face_track）
    """

    step_name = "face"
    applies_to = ("video",)  # 图片也可跑，但图片没有 clip 概念，暂不接

    def __init__(self, repo: VaultRepository):
        super().__init__(repo)

    def _run(self, source: MediaSource) -> str:
        provider = get_provider(Capability.FACE_RECOGNIZE)
        if not provider.is_available():
            raise StepSkipped(self.step_name, source.fingerprint,
                              "face_recognize provider 不可用（insightface 未装）")

        path = source.path_current or source.path_original
        if not os.path.exists(path):
            raise StepSkipped(self.step_name, source.fingerprint, "文件不存在")

        clips = self.repo.list_clips(source.fingerprint)
        if not clips:
            raise StepSkipped(self.step_name, source.fingerprint,
                              "无镜头（需先跑 scene_cut）")

        # 拿关键帧（复用 keyframe_extract）
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

        # 按 clip_id 分组（每个 clip 用首帧检测人脸）
        first_by_clip: dict[str, str] = {}
        for kf in keyframes:
            if kf.clip_id not in first_by_clip:
                first_by_clip[kf.clip_id] = kf.path

        store = get_vector_store("face")
        total_faces = 0

        for clip in clips:
            kf_path = first_by_clip.get(clip.clip_id)
            if not kf_path:
                continue

            try:
                result = provider.detect_faces(kf_path)
            except Exception as e:
                logger.debug("人脸检测失败 clip=%s：%s", clip.clip_id, e)
                continue

            if not result.faces:
                continue

            # 每张脸写一行 face_track + 一条 face.faiss 向量
            tracks: list[FaceTrack] = []
            embeddings: list[list[float]] = []
            angles: list[str | None] = []
            for face in result.faces:
                # 幂等检查：已有 face 记录则跳过该 clip（粗粒度）
                angles.append(face.face_angle)
                tracks.append(FaceTrack(
                    face_id=None,
                    clip_id=clip.clip_id,
                    face_angle=face.face_angle,
                    completeness=face.completeness,
                ))
                embeddings.append(face.embedding)

            # 幂等：该 clip 已有人脸记录则跳过
            if self.repo.list_face_tracks(clip.clip_id):
                total_faces += len(result.faces)
                continue

            self.repo.insert_face_tracks(tracks)
            # 拿回刚插入的 face_id（按 clip 重查）
            inserted = self.repo.list_face_tracks(clip.clip_id)

            # 取最后 len(tracks) 条（刚插入的）
            new_tracks = inserted[-len(tracks):] if len(inserted) >= len(tracks) else inserted

            for track, emb in zip(new_tracks, embeddings):
                if track.face_id is None:
                    continue
                store.add(str(track.face_id), emb)

            total_faces += len(result.faces)

        if total_faces > 0:
            store.save()

        return f"{total_faces} 张脸" if total_faces else "无人脸"

    def is_done(self, source: MediaSource) -> bool:
        """该素材是否已跑过人脸检测（有 face_track 记录）。"""
        clips = self.repo.list_clips(source.fingerprint)
        if not clips:
            return False
        for clip in clips:
            if self.repo.list_face_tracks(clip.clip_id):
                return True
        return False
