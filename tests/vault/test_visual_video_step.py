"""P1-1 视频镜头级画面打标测试：关键帧抽取 + clip 级向量/caption。

测代码逻辑正确性（不依赖真实视频解码/真实模型）：
    1. keyframe_extract：时间戳选取 + 幂等（mock PyAV）
    2. VisualEmbedVideoStep：clip → visual.faiss（id=clip_id，ref_table=clip）
    3. VideoCaptionStep：clip → caption/keyframes/shot_type 写回
    4. pipeline 注册顺序
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------- #
# pipeline 注册
# ---------------------------------------------------------------------- #

def test_video_pipeline_has_visual_video_steps():
    """video pipeline 包含 visual_embed_video + video_caption。"""
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(VaultRepository())
    steps = orch.list_pipeline_steps("video")
    assert "visual_embed_video" in steps
    assert "video_caption" in steps
    # visual_embed_video 在 scene_cut 之后（需要 clip）
    assert steps.index("scene_cut") < steps.index("visual_embed_video")
    assert steps.index("scene_cut") < steps.index("video_caption")
    # video_caption 在 visual_embed_video 之后（共用关键帧，caption 独立）
    assert steps.index("visual_embed_video") < steps.index("video_caption")


# ---------------------------------------------------------------------- #
# keyframe_extract：时间戳选取逻辑
# ---------------------------------------------------------------------- #

def test_pick_timestamps_single_frame_is_midpoint():
    """1 帧 → 取 clip 中间。"""
    from apps.topos_vault.pipeline.keyframe_extract import _pick_timestamps
    ts = _pick_timestamps(0.0, 10.0, 1)
    assert ts == [5.0]


def test_pick_timestamps_avoids_boundaries():
    """多帧 → 避开首尾边界（首帧黑场，尾帧切黑）。"""
    from apps.topos_vault.pipeline.keyframe_extract import _pick_timestamps
    ts = _pick_timestamps(0.0, 12.0, 3)
    assert len(ts) == 3
    # 都在 (0, 12) 内，不触边界
    assert all(0 < t < 12 for t in ts)
    # 均匀
    assert ts[0] < ts[1] < ts[2]


def test_pick_timestamps_handles_zero_duration():
    """duration <= 0 不崩。"""
    from apps.topos_vault.pipeline.keyframe_extract import _pick_timestamps
    ts = _pick_timestamps(5.0, 5.0, 2)
    # 退化情况返回中间或 start
    assert isinstance(ts, list)


# ---------------------------------------------------------------------- #
# VisualEmbedVideoStep：clip → 向量
# ---------------------------------------------------------------------- #

def test_visual_embed_video_step_writes_clip_vectors(repo, tmp_path, monkeypatch):
    """有 clip + mock 关键帧 → clip_id 写入 visual.faiss + vector_index 表。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache, get_vector_store
    from apps.topos_vault.db.repository import Clip, MediaSource, VaultRepository
    from apps.topos_vault.pipeline.visual_step import VisualEmbedVideoStep
    from apps.topos_vault.pipeline.steps import StepStatus
    from apps.topos_vault.pipeline import keyframe_extract as kf_mod

    clear_store_cache()

    # 造假"视频"文件 + ingest
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"FAKE_VIDEO" * 100)

    from apps.topos_vault.pipeline.ingest_step import IngestStep
    src = MediaSource(fingerprint="", media_type="video", path_original=str(fake_video))
    IngestStep(repo).execute(src)
    src2 = repo.get_media_source_by_path(str(fake_video))
    assert src2 is not None

    # 加 2 个 clip
    c1 = Clip(clip_id="", fingerprint=src2.fingerprint, clip_index=0,
              start_sec=0, end_sec=5, duration_sec=5)
    c2 = Clip(clip_id="", fingerprint=src2.fingerprint, clip_index=1,
              start_sec=5, end_sec=10, duration_sec=5)
    repo.insert_clip(c1)
    repo.insert_clip(c2)
    clips = repo.list_clips(src2.fingerprint)
    assert len(clips) == 2

    # mock 关键帧抽取：每个 clip 造一个假 jpg 路径
    fake_kf = tmp_path / "kf.jpg"
    fake_kf.write_bytes(b"JPEG")
    from apps.topos_vault.pipeline.keyframe_extract import Keyframe
    def fake_extract(video_path, clips, fingerprint, **kwargs):
        return [
            Keyframe(clip_id=clips[0].clip_id, clip_index=0, frame_index=0,
                     timestamp_sec=2.5, path=str(fake_kf)),
            Keyframe(clip_id=clips[1].clip_id, clip_index=1, frame_index=0,
                     timestamp_sec=7.5, path=str(fake_kf)),
        ]
    monkeypatch.setattr(kf_mod, "extract_keyframes", fake_extract)
    monkeypatch.setattr(kf_mod, "is_keyframe_available", lambda: True)

    # 跑 step
    step = VisualEmbedVideoStep(repo)
    result = step.execute(MediaSource(
        fingerprint=src2.fingerprint, media_type="video",
        path_original=str(fake_video),
    ))
    assert result.status == StepStatus.DONE

    # 验证 visual 索引有 clip_id
    store = get_vector_store("visual")
    assert store.has(clips[0].clip_id)
    assert store.has(clips[1].clip_id)

    # 验证 vector_index 表登记了 clip
    from apps.topos_vault.paths import get_db_path
    import sqlite3
    conn = sqlite3.connect(str(get_db_path()))
    try:
        rows = conn.execute(
            "SELECT ref_table, ref_id FROM vector_index "
            "WHERE index_name = 'visual' AND ref_table = 'clip'"
        ).fetchall()
        assert len(rows) == 2
        ref_ids = {r[1] for r in rows}
        assert clips[0].clip_id in ref_ids
        assert clips[1].clip_id in ref_ids
    finally:
        conn.close()


def test_visual_embed_video_step_skipped_without_clips(repo, tmp_path):
    """无 clip → 跳过。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.pipeline.visual_step import VisualEmbedVideoStep
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"X")
    step = VisualEmbedVideoStep(repo)
    result = step.execute(MediaSource(
        fingerprint="fp_noclips", media_type="video",
        path_original=str(fake_video),
    ))
    assert result.status == StepStatus.SKIPPED


def test_visual_embed_video_idempotent(repo, tmp_path, monkeypatch):
    """重复跑 → 第二次跳过（已嵌入）。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.db.repository import Clip, MediaSource
    from apps.topos_vault.pipeline.visual_step import VisualEmbedVideoStep
    from apps.topos_vault.pipeline.steps import StepStatus
    from apps.topos_vault.pipeline import keyframe_extract as kf_mod

    clear_store_cache()
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"V")
    fp = "fp_idem_video"
    # 插 media_source（clip 外键依赖）
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original=str(fake_video),
    ))
    fake_kf = tmp_path / "kf.jpg"
    fake_kf.write_bytes(b"JPEG")

    c1 = Clip(clip_id="", fingerprint=fp, clip_index=0,
              start_sec=0, end_sec=5, duration_sec=5)
    repo.insert_clip(c1)
    clips = repo.list_clips(fp)

    from apps.topos_vault.pipeline.keyframe_extract import Keyframe
    monkeypatch.setattr(kf_mod, "extract_keyframes",
        lambda vp, cl, fp_, **kw: [Keyframe(clip_id=cl[0].clip_id, clip_index=0,
                                            frame_index=0, timestamp_sec=2.5,
                                            path=str(fake_kf))])
    monkeypatch.setattr(kf_mod, "is_keyframe_available", lambda: True)

    step = VisualEmbedVideoStep(repo)
    src = MediaSource(fingerprint=fp, media_type="video", path_original=str(fake_video))
    r1 = step.execute(src)
    assert r1.status == StepStatus.DONE
    # 第二次
    r2 = step.execute(src)
    assert r2.status == StepStatus.SKIPPED


# ---------------------------------------------------------------------- #
# VideoCaptionStep：clip → caption/keyframes/shot_type
# ---------------------------------------------------------------------- #

def test_video_caption_writes_keyframes_and_shot_type(repo, tmp_path, monkeypatch):
    """video_caption 把关键帧路径 + shot_type 写回 clip。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.db.repository import Clip, MediaSource
    from apps.topos_vault.pipeline.visual_step import VideoCaptionStep
    from apps.topos_vault.pipeline.steps import StepStatus
    from apps.topos_vault.pipeline import keyframe_extract as kf_mod

    clear_store_cache()
    fp = "fp_caption_video"
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"V")
    # 插 media_source（clip 外键依赖）
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original=str(fake_video),
    ))

    c1 = Clip(clip_id="", fingerprint=fp, clip_index=0,
              start_sec=0, end_sec=5, duration_sec=5)
    repo.insert_clip(c1)
    clips = repo.list_clips(fp)

    fake_kf1 = tmp_path / "kf0.jpg"
    fake_kf1.write_bytes(b"JPEG")
    fake_kf2 = tmp_path / "kf1.jpg"
    fake_kf2.write_bytes(b"JPEG")

    from apps.topos_vault.pipeline.keyframe_extract import Keyframe
    monkeypatch.setattr(kf_mod, "extract_keyframes",
        lambda vp, cl, fp_, **kw: [
            Keyframe(clip_id=cl[0].clip_id, clip_index=0, frame_index=0,
                     timestamp_sec=1.5, path=str(fake_kf1)),
            Keyframe(clip_id=cl[0].clip_id, clip_index=0, frame_index=1,
                     timestamp_sec=3.5, path=str(fake_kf2)),
        ])
    monkeypatch.setattr(kf_mod, "is_keyframe_available", lambda: True)

    step = VideoCaptionStep(repo)
    result = step.execute(MediaSource(
        fingerprint=fp, media_type="video", path_original=str(fake_video),
    ))
    assert result.status == StepStatus.DONE

    # 查回 clip：应有 keyframes + shot_type
    clip = repo.get_clip(clips[0].clip_id)
    assert clip is not None
    assert len(clip.keyframes) == 2
    assert str(fake_kf1) in clip.keyframes
    assert clip.shot_type is not None  # stub caption 给了 shot_type
    assert clip.status == "done"


def test_video_caption_skipped_when_no_clips(repo, tmp_path):
    """无 clip → 跳过。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.pipeline.visual_step import VideoCaptionStep
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"X")
    step = VideoCaptionStep(repo)
    result = step.execute(MediaSource(
        fingerprint="fp_noclips_cap", media_type="video",
        path_original=str(fake_video),
    ))
    assert result.status == StepStatus.SKIPPED


# ---------------------------------------------------------------------- #
# repository.update_clip_keyframes / get_clip
# ---------------------------------------------------------------------- #

def test_update_clip_keyframes(repo):
    """update_clip_keyframes 写入 keyframes + shot_type。"""
    from apps.topos_vault.db.repository import Clip, MediaSource
    fp = "fp_repo_test"
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original="/tmp/test.mp4",
    ))
    c = Clip(clip_id="", fingerprint=fp, clip_index=0,
             start_sec=0, end_sec=5, duration_sec=5)
    repo.insert_clip(c)
    clips = repo.list_clips(fp)
    clip_id = clips[0].clip_id

    repo.update_clip_keyframes(clip_id, ["/a/kf0.jpg", "/a/kf1.jpg"],
                               shot_type="特写")
    clip = repo.get_clip(clip_id)
    assert clip is not None
    assert clip.keyframes == ["/a/kf0.jpg", "/a/kf1.jpg"]
    assert clip.shot_type == "特写"


def test_get_clip_returns_none_for_unknown(repo):
    """get_clip 未知 clip_id → None。"""
    assert repo.get_clip("nonexistent-clip-id") is None
