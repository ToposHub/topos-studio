"""基础打标步骤：指纹入库 + 探测元数据。

这是所有素材都要跑的第一步——把文件变成 vault 里的一条 media_source 记录，
带内容指纹和探测出的 duration/WxH 等元数据。

后续阶段会在本目录补更多 step：
    scene_cut_step.py    视频镜头切割（复用项目 scene_cut）
    asr_step.py          ASR 转写（复用项目 transcript_provider）
    visual_step.py       画面打标（Qwen3-VL + SigLIP）
    face_step.py         人脸识别（InsightFace）
    audio_step.py        音乐理解（Qwen2.5-Omni + librosa）
"""

from __future__ import annotations

import os

from apps.topos_vault.db.repository import MediaSource
from apps.topos_vault.fingerprint import compute_fingerprint
from apps.topos_vault.pipeline.steps import PipelineStep
from apps.topos_vault.probe import probe_signature


class IngestStep(PipelineStep):
    """第一步：指纹入库 + 探测元数据。

    把一个文件路径变成 vault 里的一条 media_source 记录。
    幂等：已存在的指纹更新元数据，不重复创建。
    """

    step_name = "ingest"
    applies_to = ()  # 所有类型

    def _run(self, source: MediaSource) -> str:
        # 重新计算指纹（source 可能是 CLI 临时构造的，指纹可能不准）
        fp = compute_fingerprint(source.path_original, source.media_type)

        # 探测元数据（duration/WxH/codec，用于 relink Level 3 兜底）
        sig = probe_signature(source.path_original)
        duration = sig.duration_sec if sig else source.duration_sec
        width = sig.width if sig else source.width
        height = sig.height if sig else source.height
        codec = sig.codec if sig else source.codec

        # 更新 source 的指纹和元数据
        source.fingerprint = fp
        source.duration_sec = duration
        source.width = width
        source.height = height
        source.codec = codec
        source.file_size = os.path.getsize(source.path_original)
        source.path_status = "valid"

        self.repo.upsert_media_source(source)
        return f"fp={fp[:24]}... dur={duration} {width}x{height}"

    def is_done(self, source: MediaSource) -> bool:
        """指纹非 size: 降级且记录存在 → 已完成。

        size: 指纹是降级模式，不视为真正完成（用户装好依赖后应重算）。
        """
        existing = self.repo.get_media_source_by_path(source.path_original)
        if existing is None:
            return False
        # 如果当前是降级指纹，且现在依赖装好了，应重算（返回 False 触发重跑）
        from apps.topos_vault.fingerprint import parse_fingerprint_type
        if parse_fingerprint_type(existing.fingerprint) == "size":
            # 检查现在能否算出更好的指纹
            new_fp = compute_fingerprint(source.path_original, source.media_type)
            if parse_fingerprint_type(new_fp) != "size":
                return False  # 依赖装好了，重算
        return True
