"""台词向量嵌入步骤（视频用）。

对每个 clip 的台词文本调 transcript_embed provider，
把向量写入 transcript.faiss，并在 vector_index 表登记映射。

前置依赖：ASR 步骤（必须先有 transcript_line 才能嵌向量）。
"""

from __future__ import annotations

import logging

from apps.topos_vault.db.faiss_store import get_vector_store
from apps.topos_vault.db.repository import MediaSource, VaultRepository
from apps.topos_vault.pipeline.steps import PipelineStep, StepSkipped
from apps.topos_vault.providers import Capability, get_provider

logger = logging.getLogger(__name__)


class TranscriptEmbedStep(PipelineStep):
    """台词向量嵌入步骤。

    遍历某素材所有 clip 的 transcript_line，把每句台词转向量写入 transcript.faiss。
    支持批量编码（provider.embed_texts 内部批处理）。
    """

    step_name = "transcript_embed"
    applies_to = ("video",)  # 只有视频有台词

    def __init__(self, repo: VaultRepository):
        super().__init__(repo)

    def _run(self, source: MediaSource) -> str:
        provider = get_provider(Capability.TRANSCRIPT_EMBED)
        if not provider.is_available():
            raise StepSkipped(self.step_name, source.fingerprint,
                              "transcript_embed provider 不可用")

        # 拿到所有 clip 的台词
        clips = self.repo.list_clips(source.fingerprint)
        if not clips:
            raise StepSkipped(self.step_name, source.fingerprint, "无镜头（需先跑 scene_cut）")

        all_lines = []
        line_ids = []
        for clip in clips:
            lines = self.repo.list_transcript_lines(clip.clip_id)
            for line in lines:
                if line.text.strip():
                    all_lines.append(line.text)
                    line_ids.append(str(line.line_id))

        if not all_lines:
            raise StepSkipped(self.step_name, source.fingerprint, "无台词（需先跑 ASR）")

        # 批量嵌向量
        results = provider.embed_texts(all_lines)
        if len(results) != len(all_lines):
            raise StepSkipped(self.step_name, source.fingerprint, "批量嵌向量数量不匹配")

        # 写入 transcript.faiss（id 用 line_id 字符串）
        dim = results[0].dim
        store = get_vector_store("transcript", dim=dim)
        for line_id, result in zip(line_ids, results):
            store.add(line_id, result.vector)
        store.save()

        # 在 vector_index 表登记
        for line_id, result in zip(line_ids, results):
            self.repo.register_vector(
                index_name="transcript",
                ref_table="transcript_line",
                ref_id=line_id,
                model=result.model,
                dim=result.dim,
            )

        return f"{len(all_lines)} 句台词 → dim={dim}"

    def is_done(self, source: MediaSource) -> bool:
        """所有台词是否都已嵌向量。

        检查方式：有台词的 clip，其 transcript_line 是否在 transcript 索引里。
        """
        clips = self.repo.list_clips(source.fingerprint)
        if not clips:
            return False

        store = get_vector_store("transcript")
        if store.size == 0:
            return False

        for clip in clips:
            lines = self.repo.list_transcript_lines(clip.clip_id)
            for line in lines:
                if line.text.strip() and not store.has(str(line.line_id)):
                    return False
        return True
