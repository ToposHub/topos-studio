"""P1-6 TAEP 桥接测试：vault 数据 → SemanticSegment。

测 4 个转换函数 + 新增 SegmentKind：
    1. clip_to_semantic_segment：vault clip → SHOT（秒→帧转换）
    2. face_track_to_segments：face_track + clip → FACE/CHARACTER
    3. transcript_line_to_segment：台词 → TRANSCRIPT
    4. search_hit_to_segments：检索命中 → VISUAL/AUDIO
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------- #
# 新增 SegmentKind
# ---------------------------------------------------------------------- #

def test_new_segment_kinds_exist():
    """P1-6 新增 VISUAL/FACE/CHARACTER SegmentKind。"""
    from src.features.edit.ai_editing.editorial_models import SegmentKind
    assert SegmentKind.VISUAL.value == "visual"
    assert SegmentKind.FACE.value == "face"
    assert SegmentKind.CHARACTER.value == "character"
    # 旧的还在
    assert SegmentKind.SHOT.value == "shot"
    assert SegmentKind.TRANSCRIPT.value == "transcript"


# ---------------------------------------------------------------------- #
# clip_to_semantic_segment
# ---------------------------------------------------------------------- #

def test_clip_to_shot_segment_seconds_to_frames():
    """vault clip（秒）→ SHOT SemanticSegment（帧），秒→帧转换正确。"""
    from src.shared.vault.bridge import clip_to_semantic_segment
    from src.features.edit.ai_editing.editorial_models import SegmentKind

    seg = clip_to_semantic_segment(
        {
            "clip_id": "c1",
            "start_sec": 1.0,
            "end_sec": 2.5,
            "shot_type": "特写",
            "keyframes": ["/a/kf.jpg"],
        },
        asset_id="asset1",
        fps=24.0,
    )
    assert seg.kind == SegmentKind.SHOT
    assert seg.asset_id == "asset1"
    # 1.0 * 24 = 24；2.5 * 24 = 60；duration = 36
    assert seg.source_range.start == 24
    assert seg.source_range.duration == 36
    assert seg.payload["shot_type"] == "特写"
    assert seg.payload["keyframes"] == ["/a/kf.jpg"]
    assert seg.payload["source"] == "vault"
    assert seg.provenance["clip_id"] == "c1"


def test_clip_to_segment_segment_id_from_clip_id():
    """有 clip_id → segment_id 用它。"""
    from src.shared.vault.bridge import clip_to_semantic_segment
    seg = clip_to_semantic_segment(
        {"clip_id": "abc", "start_sec": 0, "end_sec": 1},
        asset_id="a1", fps=24,
    )
    assert "abc" in seg.segment_id


def test_clip_to_segment_handles_missing_fields():
    """缺字段（start_sec/keyframes 等）不崩。"""
    from src.shared.vault.bridge import clip_to_semantic_segment
    seg = clip_to_semantic_segment({}, asset_id="a1", fps=24)
    assert seg.source_range.start == 0
    assert seg.source_range.duration >= 1


def test_clip_to_segment_zero_fps_safe():
    """fps=0 不崩（防 1.0 保护）。"""
    from src.shared.vault.bridge import clip_to_semantic_segment
    seg = clip_to_semantic_segment(
        {"start_sec": 1.0, "end_sec": 2.0}, asset_id="a1", fps=0,
    )
    # fps=0 → 保护为 1.0，start = 1.0 * 1 = 1
    assert seg.source_range.start >= 0


# ---------------------------------------------------------------------- #
# face_track_to_segments
# ---------------------------------------------------------------------- #

def test_face_track_to_face_segment_without_character():
    """无 character/cluster → FACE 类型。"""
    from src.shared.vault.bridge import face_track_to_segments
    from src.features.edit.ai_editing.editorial_models import SegmentKind

    seg = face_track_to_segments(
        {"face_id": 5, "face_angle": "正面", "completeness": "完整"},
        {"clip_id": "c1", "start_sec": 0, "end_sec": 3},
        asset_id="a1", fps=24,
    )
    assert seg.kind == SegmentKind.FACE
    assert seg.payload["face_angle"] == "正面"
    assert seg.source_range.duration == 72  # 3 * 24


def test_face_track_to_character_segment_with_character():
    """有 character → CHARACTER 类型。"""
    from src.shared.vault.bridge import face_track_to_segments
    from src.features.edit.ai_editing.editorial_models import SegmentKind

    seg = face_track_to_segments(
        {"face_id": 5, "character": "张三", "cluster_id": 2},
        {"clip_id": "c1", "start_sec": 0, "end_sec": 3},
        asset_id="a1", fps=24,
    )
    assert seg.kind == SegmentKind.CHARACTER
    assert seg.payload["character"] == "张三"


def test_face_track_to_character_segment_with_cluster_only():
    """只有 cluster_id（无 character 名）→ CHARACTER（已聚类）。"""
    from src.shared.vault.bridge import face_track_to_segments
    from src.features.edit.ai_editing.editorial_models import SegmentKind

    seg = face_track_to_segments(
        {"face_id": 5, "cluster_id": 7},
        {"clip_id": "c1", "start_sec": 0, "end_sec": 3},
        asset_id="a1", fps=24,
    )
    assert seg.kind == SegmentKind.CHARACTER


# ---------------------------------------------------------------------- #
# transcript_line_to_segment
# ---------------------------------------------------------------------- #

def test_transcript_line_to_segment():
    """台词 → TRANSCRIPT，时间用 line 自己的（比 clip 精确）。"""
    from src.shared.vault.bridge import transcript_line_to_segment
    from src.features.edit.ai_editing.editorial_models import SegmentKind

    seg = transcript_line_to_segment(
        {"line_id": 10, "text": "你好", "speaker": "S1", "character": "张三",
         "start_sec": 1.0, "end_sec": 2.0},
        {"clip_id": "c1", "start_sec": 0, "end_sec": 5},
        asset_id="a1", fps=24,
    )
    assert seg.kind == SegmentKind.TRANSCRIPT
    assert seg.payload["text"] == "你好"
    assert seg.payload["speaker"] == "S1"
    # 用 line 的时间，不是 clip 的
    assert seg.source_range.start == 24  # 1.0 * 24
    assert seg.source_range.duration == 24  # (2.0-1.0) * 24


# ---------------------------------------------------------------------- #
# search_hit_to_segments
# ---------------------------------------------------------------------- #

def test_search_hit_image_to_visual_segment():
    """图片命中 → VISUAL segment。"""
    from src.shared.vault.bridge import search_hit_to_segments
    from src.features.edit.ai_editing.editorial_models import SegmentKind

    segs = search_hit_to_segments(
        {
            "vault_media_type": "image",
            "vault_caption": "夜景街道",
            "vault_tags": ["夜景", "街道"],
            "vault_relevance": 0.85,
            "path": "/x/y.jpg",
        },
        asset_id="a1",
    )
    assert len(segs) == 1
    assert segs[0].kind == SegmentKind.VISUAL
    assert segs[0].payload["caption"] == "夜景街道"
    assert segs[0].payload["tags"] == ["夜景", "街道"]
    assert segs[0].confidence == 0.85


def test_search_hit_audio_to_audio_segment():
    """音频命中 → AUDIO segment。"""
    from src.shared.vault.bridge import search_hit_to_segments
    from src.features.edit.ai_editing.editorial_models import SegmentKind

    segs = search_hit_to_segments(
        {
            "vault_media_type": "audio",
            "vault_caption": "欢快的音乐",
            "vault_mood": "energetic",
            "vault_relevance": 0.7,
        },
        asset_id="a1",
    )
    assert len(segs) == 1
    assert segs[0].kind == SegmentKind.AUDIO
    assert segs[0].payload["mood"] == "energetic"


def test_search_hit_video_returns_empty():
    """视频命中（file_explorer 格式无 clip 级数据）→ 空列表（留给 clip_to_semantic_segment）。"""
    from src.shared.vault.bridge import search_hit_to_segments
    segs = search_hit_to_segments(
        {"vault_media_type": "video"}, asset_id="a1",
    )
    assert segs == []
