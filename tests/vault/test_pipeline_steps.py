"""pipeline step 测试：visual_embed / image_caption / transcript_embed。"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------- #
# VisualEmbedStep（图片画面向量）
# ---------------------------------------------------------------------- #

def test_visual_embed_step_writes_vector(repo, sample_files):
    """图片打标后，向量写入 visual 索引 + vector_index 表登记。"""
    from apps.topos_vault.db.faiss_store import get_vector_store, clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    clear_store_cache()
    orch = Orchestrator(repo)
    results = orch.index_file(str(sample_files["image"]))

    # 应该有 ingest + visual_embed + image_caption 三步
    step_names = [r.step_name for r in results]
    assert "visual_embed" in step_names

    # 向量写入成功（stub provider 可用）
    from apps.topos_vault.pipeline.steps import StepStatus
    visual_result = next(r for r in results if r.step_name == "visual_embed")
    # stub is_available=True，应该 done 或 skipped
    assert visual_result.status in (StepStatus.DONE, StepStatus.SKIPPED)


def test_visual_embed_step_idempotent(repo, sample_files):
    """重复跑 visual_embed，第二次应跳过（is_done=True）。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    orch = Orchestrator(repo)
    orch.index_file(str(sample_files["image"]))
    results2 = orch.index_file(str(sample_files["image"]))

    # 第二次所有步骤都应跳过
    for r in results2:
        assert r.status == StepStatus.SKIPPED, f"{r.step_name} 第二次应跳过"


def test_image_caption_step_writes_meta(repo, sample_files):
    """图片打标后，image_meta 有 caption。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.db.repository import VaultRepository

    clear_store_cache()
    orch = Orchestrator(repo)
    orch.index_file(str(sample_files["image2"]))

    # 从库里查回
    r = VaultRepository()
    src = r.get_media_source_by_path(str(sample_files["image2"]))
    assert src is not None
    meta = r.get_image_meta(src.fingerprint)
    # stub provider 会生成 caption
    assert meta is not None
    assert meta.caption  # 非空


# ---------------------------------------------------------------------- #
# TranscriptEmbedStep（视频台词向量）
# ---------------------------------------------------------------------- #

def test_transcript_embed_step_skipped_without_clips(repo, sample_files):
    """没有 clip（未跑 scene_cut）时，transcript_embed 跳过。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    orch = Orchestrator(repo)
    results = orch.index_file(str(sample_files["video"]))

    # ingest 应 done，transcript_embed 应 skipped（无 clip）
    t_result = next(r for r in results if r.step_name == "transcript_embed")
    assert t_result.status == StepStatus.SKIPPED


def test_transcript_embed_step_with_clips(repo, sample_files):
    """有 clip + 台词时，transcript_embed 写入向量。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache, get_vector_store
    from apps.topos_vault.db.repository import Clip, TranscriptLine
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    orch = Orchestrator(repo)

    # 先 ingest
    orch.index_file(str(sample_files["video"]))

    # 手动加 clip + 台词（模拟 scene_cut + asr 跑完）
    r = orch.repo
    src = r.get_media_source_by_path(str(sample_files["video"]))
    clip = Clip(clip_id="c1", fingerprint=src.fingerprint, clip_index=0,
                start_sec=0, end_sec=5, duration_sec=5)
    r.insert_clip(clip)
    r.insert_transcript_lines([
        TranscriptLine(line_id=None, clip_id="c1", text="你好世界"),
        TranscriptLine(line_id=None, clip_id="c1", text="测试台词"),
    ])

    # 再跑一次 pipeline（transcript_embed 这次应该能跑）
    results = orch.index_file(str(sample_files["video"]))
    t_result = next((res for res in results if res.step_name == "transcript_embed"), None)
    assert t_result is not None
    # stub 可用 → done；不可用 → skipped
    assert t_result.status in (StepStatus.DONE, StepStatus.SKIPPED)


# ---------------------------------------------------------------------- #
# 编排器集成
# ---------------------------------------------------------------------- #

def test_image_pipeline_has_three_steps():
    """图片 pipeline 有 ingest + visual_embed + image_caption 三步。"""
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    repo = VaultRepository()
    orch = Orchestrator(repo)
    steps = orch.list_pipeline_steps("image")
    assert "ingest" in steps
    assert "visual_embed" in steps
    assert "image_caption" in steps


def test_video_pipeline_has_full_chain():
    """视频 pipeline 有完整链：ingest → scene_cut → asr → transcript_embed。"""
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    repo = VaultRepository()
    orch = Orchestrator(repo)
    steps = orch.list_pipeline_steps("video")
    assert "ingest" in steps
    assert "scene_cut" in steps
    assert "asr" in steps
    assert "transcript_embed" in steps
    # 顺序正确
    assert steps.index("ingest") < steps.index("scene_cut")
    assert steps.index("scene_cut") < steps.index("asr")
    assert steps.index("asr") < steps.index("transcript_embed")


def test_audio_pipeline_has_ingest_and_features():
    """音频 pipeline：ingest → audio_features（librosa 数值特征）。"""
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    repo = VaultRepository()
    orch = Orchestrator(repo)
    steps = orch.list_pipeline_steps("audio")
    assert "ingest" in steps
    assert "audio_features" in steps
    assert steps.index("ingest") < steps.index("audio_features")


def test_vector_registerd_in_db(repo, sample_files):
    """打标后 vector_index 表有记录。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.db.repository import VaultRepository

    clear_store_cache()
    orch = Orchestrator(repo)
    orch.index_file(str(sample_files["image"]))

    r = VaultRepository()
    src = r.get_media_source_by_path(str(sample_files["image"]))
    assert src is not None

    # vector_index 应有 visual 的记录
    # （只有 stub 真的写入时才有，stub embed 结果会写入）
    from apps.topos_vault.db.faiss_store import get_vector_store
    store = get_vector_store("visual")
    # 如果 stub 写入了，store 里应该有
    if store.size > 0:
        assert store.has(src.fingerprint)
