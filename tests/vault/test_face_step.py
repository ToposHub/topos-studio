"""P1-2 人脸步骤 + 角色聚类测试。

测代码逻辑（不依赖真实 insightface 模型）：
    1. FaceStep：clip 关键帧 → face_track + face.faiss（mock provider）
    2. character_cluster：DBSCAN 聚类正确分组合成向量
    3. _dbscan_numpy 降级实现（sklearn 不可用时的纯 numpy 版）
    4. repository face 相关方法 + list_character_clusters
    5. pipeline 注册
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------- #
# pipeline 注册
# ---------------------------------------------------------------------- #

def test_video_pipeline_has_face_step():
    """video pipeline 包含 face step（在 video_caption 之后）。"""
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(VaultRepository())
    steps = orch.list_pipeline_steps("video")
    assert "face" in steps
    assert steps.index("video_caption") < steps.index("face")


# ---------------------------------------------------------------------- #
# character_cluster：DBSCAN 聚类
# ---------------------------------------------------------------------- #

def test_dbscan_numpy_separates_two_clusters():
    """纯 numpy DBSCAN：两个明显的簇应分成 2 类。"""
    import numpy as np
    from apps.topos_vault.pipeline.character_cluster import _dbscan_numpy

    # 簇 A：4 个点彼此距离 0.05；簇 B：4 个点彼此距离 0.05；两簇距离 1.5
    pts_a = np.array([[0.0, 0.05, 0.04, 0.06, 1.5, 1.51, 1.52, 1.49]])
    pts_a = np.array([
        [0.0], [0.05], [0.04], [0.06],
        [1.5], [1.51], [1.52], [1.49],
    ])
    # 构造距离矩阵（1D 距离）
    n = len(pts_a)
    dist = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            dist[i, j] = abs(pts_a[i, 0] - pts_a[j, 0])

    labels = _dbscan_numpy(dist, eps=0.2, min_samples=2)
    # 应该有 2 个簇（标签 0 和 1），无噪声
    unique = set(int(l) for l in labels if l != -1)
    assert len(unique) == 2
    assert all(l != -1 for l in labels)  # 都成簇，无噪声
    # 前 4 个同簇，后 4 个同簇
    assert labels[0] == labels[1] == labels[2] == labels[3]
    assert labels[4] == labels[5] == labels[6] == labels[7]
    assert labels[0] != labels[4]


def test_dbscan_numpy_marks_isolated_point_as_noise():
    """孤立点（邻居不够）→ 噪声 -1。"""
    import numpy as np
    from apps.topos_vault.pipeline.character_cluster import _dbscan_numpy

    # 3 个紧挨的点 + 1 个远离的点
    pts = [0.0, 0.05, 0.06, 10.0]
    n = 4
    dist = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            dist[i, j] = abs(pts[i] - pts[j])

    labels = _dbscan_numpy(dist, eps=0.2, min_samples=2)
    # 前 3 个成簇，最后一个噪声
    assert labels[3] == -1
    assert labels[0] == labels[1] == labels[2] != -1


def test_cluster_characters_groups_same_person(repo, monkeypatch):
    """聚合同一角色的多个人脸向量。

    造 4 个人脸记录：前 3 个向量相近（角色 A），第 4 个不同（角色 B）。
    聚类后应分成 2 簇，cluster_id 写回 face_track。
    """
    from apps.topos_vault.db.faiss_store import clear_store_cache, get_vector_store
    from apps.topos_vault.db.repository import Clip, FaceTrack, MediaSource
    from apps.topos_vault.pipeline.character_cluster import cluster_characters

    clear_store_cache()
    # 建 media_source + clip
    fp = "fp_cluster_test"
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original="/tmp/x.mp4",
    ))
    c1 = Clip(clip_id="", fingerprint=fp, clip_index=0,
              start_sec=0, end_sec=5, duration_sec=5)
    repo.insert_clip(c1)
    clip = repo.list_clips(fp)[0]

    # 4 个人脸（3 个相近 A，1 个不同 B）
    vec_a = [1.0] * 512  # 归一化前
    vec_b = [-1.0] * 512
    repo.insert_face_tracks([
        FaceTrack(face_id=None, clip_id=clip.clip_id),
        FaceTrack(face_id=None, clip_id=clip.clip_id),
        FaceTrack(face_id=None, clip_id=clip.clip_id),
        FaceTrack(face_id=None, clip_id=clip.clip_id),
    ])
    tracks = repo.list_face_tracks(clip.clip_id)
    assert len(tracks) == 4

    store = get_vector_store("face", dim=512)
    store.add(str(tracks[0].face_id), vec_a)
    store.add(str(tracks[1].face_id), vec_a)
    store.add(str(tracks[2].face_id), vec_a)
    store.add(str(tracks[3].face_id), vec_b)
    store.save()

    # 聚类（eps 较小让 A 和 B 分开）
    result = cluster_characters(repo, eps=0.3, min_samples=2)
    assert result is not None
    assert result.n_clusters == 1  # 只有 A 成簇（3 个），B 是单点噪声
    assert result.n_noise == 1

    # 查回：前 3 个同 cluster_id，最后一个 -1
    updated = repo.list_face_tracks(clip.clip_id)
    cluster_a = updated[0].cluster_id
    assert cluster_id_valid(cluster_a)
    assert updated[0].cluster_id == updated[1].cluster_id == updated[2].cluster_id
    assert updated[3].cluster_id == -1


def cluster_id_valid(cid) -> bool:
    """cluster_id 是非负整数（-1 是噪声）。"""
    return cid is not None and cid >= 0


def test_cluster_returns_none_when_no_faces(repo):
    """无人脸 → 返回 None。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.character_cluster import cluster_characters

    clear_store_cache()
    result = cluster_characters(repo)
    assert result is None


# ---------------------------------------------------------------------- #
# FaceStep：clip → face_track + face.faiss
# ---------------------------------------------------------------------- #

def test_face_step_writes_faces(repo, tmp_path, monkeypatch):
    """有 clip + mock 关键帧 + mock provider（返回 2 张脸）→ 写 face_track + face.faiss。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache, get_vector_store
    from apps.topos_vault.db.repository import Clip, MediaSource
    from apps.topos_vault.pipeline.face_step import FaceStep
    from apps.topos_vault.pipeline.steps import StepStatus
    from apps.topos_vault.pipeline import keyframe_extract as kf_mod
    from apps.topos_vault.providers import base as prov_base
    from apps.topos_vault.providers.contracts import FaceResult, SingleFace

    clear_store_cache()
    fp = "fp_face_step"
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"V")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original=str(fake_video),
    ))
    c1 = Clip(clip_id="", fingerprint=fp, clip_index=0,
              start_sec=0, end_sec=5, duration_sec=5)
    repo.insert_clip(c1)
    clips = repo.list_clips(fp)

    fake_kf = tmp_path / "kf.jpg"
    fake_kf.write_bytes(b"JPEG")

    from apps.topos_vault.pipeline.keyframe_extract import Keyframe
    monkeypatch.setattr(kf_mod, "extract_keyframes",
        lambda vp, cl, fp_, **kw: [Keyframe(clip_id=cl[0].clip_id, clip_index=0,
                                            frame_index=0, timestamp_sec=2.5,
                                            path=str(fake_kf))])
    monkeypatch.setattr(kf_mod, "is_keyframe_available", lambda: True)

    # mock face_recognize provider：返回 2 张脸
    class FakeFaceProvider:
        provider_id = "fake_face"
        capability = prov_base.Capability.FACE_RECOGNIZE
        def is_available(self): return True
        def detect_faces(self, image_path):
            return FaceResult(faces=[
                SingleFace(bbox=(0, 0, 100, 100), embedding=[1.0] * 512,
                            confidence=0.95, face_angle="正面", completeness="完整"),
                SingleFace(bbox=(200, 200, 80, 80), embedding=[0.5] * 512,
                           confidence=0.88, face_angle="侧脸", completeness="遮挡"),
            ], model="fake_face")

    # patch face_step 模块里的 get_provider（它从 apps.topos_vault.providers 导入）
    import apps.topos_vault.pipeline.face_step as face_step_mod
    monkeypatch.setattr(face_step_mod, "get_provider",
                        lambda cap: FakeFaceProvider())

    step = FaceStep(repo)
    result = step.execute(MediaSource(
        fingerprint=fp, media_type="video", path_original=str(fake_video),
    ))
    assert result.status == StepStatus.DONE

    # face_track 有 2 行
    tracks = repo.list_face_tracks(clips[0].clip_id)
    assert len(tracks) == 2
    assert tracks[0].face_angle == "正面"

    # face.faiss 有 2 条向量
    store = get_vector_store("face", dim=512)
    assert store.has(str(tracks[0].face_id))
    assert store.has(str(tracks[1].face_id))


def test_face_step_skipped_when_provider_unavailable(repo, tmp_path, monkeypatch):
    """provider 不可用 → 跳过。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.pipeline.face_step import FaceStep
    from apps.topos_vault.pipeline.steps import StepStatus
    from apps.topos_vault.providers import base as prov_base

    clear_store_cache()
    class UnavailProvider:
        provider_id = "unavail"
        capability = prov_base.Capability.FACE_RECOGNIZE
        def is_available(self): return False
        def detect_faces(self, p): raise RuntimeError("unavailable")
    import apps.topos_vault.pipeline.face_step as face_step_mod
    monkeypatch.setattr(face_step_mod, "get_provider", lambda cap: UnavailProvider())

    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"X")
    step = FaceStep(repo)
    result = step.execute(MediaSource(
        fingerprint="fp_unavail", media_type="video", path_original=str(fake),
    ))
    assert result.status == StepStatus.SKIPPED


def test_face_step_skipped_without_clips(repo, tmp_path):
    """无 clip → 跳过。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.pipeline.face_step import FaceStep
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"X")
    step = FaceStep(repo)
    result = step.execute(MediaSource(
        fingerprint="fp_noclips_face", media_type="video", path_original=str(fake),
    ))
    assert result.status == StepStatus.SKIPPED


def test_face_step_idempotent(repo, tmp_path, monkeypatch):
    """重复跑 → 第二次跳过（已有 face_track）。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.db.repository import Clip, MediaSource
    from apps.topos_vault.pipeline.face_step import FaceStep
    from apps.topos_vault.pipeline.steps import StepStatus
    from apps.topos_vault.pipeline import keyframe_extract as kf_mod
    from apps.topos_vault.providers import base as prov_base
    from apps.topos_vault.providers.contracts import FaceResult, SingleFace

    clear_store_cache()
    fp = "fp_face_idem"
    fake = tmp_path / "f.mp4"
    fake.write_bytes(b"V")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original=str(fake),
    ))
    repo.insert_clip(Clip(clip_id="", fingerprint=fp, clip_index=0,
                          start_sec=0, end_sec=5, duration_sec=5))

    fake_kf = tmp_path / "kf.jpg"
    fake_kf.write_bytes(b"JPEG")
    from apps.topos_vault.pipeline.keyframe_extract import Keyframe
    monkeypatch.setattr(kf_mod, "extract_keyframes",
        lambda vp, cl, fp_, **kw: [Keyframe(clip_id=cl[0].clip_id, clip_index=0,
                                            frame_index=0, timestamp_sec=2.5,
                                            path=str(fake_kf))])
    monkeypatch.setattr(kf_mod, "is_keyframe_available", lambda: True)

    class FakeFaceProvider:
        provider_id = "fake_face"
        capability = prov_base.Capability.FACE_RECOGNIZE
        def is_available(self): return True
        def detect_faces(self, p):
            return FaceResult(faces=[SingleFace(
                bbox=(0,0,10,10), embedding=[1.0]*512,
                confidence=0.9, face_angle="正面", completeness="完整")],
                model="fake_face")
    import apps.topos_vault.pipeline.face_step as face_step_mod
    monkeypatch.setattr(face_step_mod, "get_provider", lambda cap: FakeFaceProvider())

    step = FaceStep(repo)
    src = MediaSource(fingerprint=fp, media_type="video", path_original=str(fake))
    r1 = step.execute(src)
    assert r1.status == StepStatus.DONE
    r2 = step.execute(src)
    assert r2.status == StepStatus.SKIPPED


# ---------------------------------------------------------------------- #
# repository face 方法
# ---------------------------------------------------------------------- #

def test_list_character_clusters(repo):
    """list_character_clusters 返回聚类统计。"""
    from apps.topos_vault.db.repository import Clip, FaceTrack, MediaSource

    fp = "fp_char_list"
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original="/tmp/x.mp4",
    ))
    repo.insert_clip(Clip(clip_id="", fingerprint=fp, clip_index=0,
                          start_sec=0, end_sec=5, duration_sec=5))
    clip = repo.list_clips(fp)[0]

    repo.insert_face_tracks([
        FaceTrack(face_id=None, clip_id=clip.clip_id),
        FaceTrack(face_id=None, clip_id=clip.clip_id),
    ])
    tracks = repo.list_face_tracks(clip.clip_id)
    # 标 cluster
    repo.update_face_cluster(tracks[0].face_id, 5)
    repo.update_face_cluster(tracks[1].face_id, 5)

    clusters = repo.list_character_clusters()
    cluster_5 = [c for c in clusters if c["cluster_id"] == 5]
    assert len(cluster_5) == 1
    assert cluster_5[0]["face_count"] == 2


def test_assign_character_name(repo):
    """assign_character_name 给聚类簇指派角色名。"""
    from apps.topos_vault.db.repository import Clip, FaceTrack, MediaSource
    from apps.topos_vault.pipeline.character_cluster import assign_character_name

    fp = "fp_assign"
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original="/tmp/x.mp4",
    ))
    repo.insert_clip(Clip(clip_id="", fingerprint=fp, clip_index=0,
                          start_sec=0, end_sec=5, duration_sec=5))
    clip = repo.list_clips(fp)[0]
    repo.insert_face_tracks([
        FaceTrack(face_id=None, clip_id=clip.clip_id),
        FaceTrack(face_id=None, clip_id=clip.clip_id),
    ])
    tracks = repo.list_face_tracks(clip.clip_id)
    repo.update_face_cluster(tracks[0].face_id, 7)
    repo.update_face_cluster(tracks[1].face_id, 7)

    n = assign_character_name(repo, 7, "张三")
    assert n == 2
    updated = repo.list_face_tracks(clip.clip_id)
    assert all(t.character == "张三" for t in updated)
