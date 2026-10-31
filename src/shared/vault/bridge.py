# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""TAEP 桥接：vault 检索结果 → SemanticSegment（喂给 AI 剪辑 Executor）。

把 vault 的 clip / face_track / transcript_line / image_meta 转成主项目的
``SemanticSegment``，让 AI 剪辑的 Executor 能消费 vault 打标出来的语义数据。

设计：
    - 单向：vault → SemanticSegment（主项目不回写 vault）
    - 时间坐标：vault 用秒，SemanticSegment 用帧（FrameRange）。
      转换需 fps（vault 的 clip 是秒，主项目 segment 是帧）。
    - 不强依赖 vault 的内部 dataclass：接收 dict（从 HTTP 查询拿到的），
      这样主项目不需要 import apps.topos_vault（保持 src/ 独立）。

新增 SegmentKind：VISUAL / FACE / CHARACTER（在 editorial_models.py 已加）。
"""

from __future__ import annotations

import logging
import math
from typing import Any

from src.features.edit.ai_editing.editorial_models import (
    FrameRange, SegmentKind, SemanticSegment,
)

logger = logging.getLogger(__name__)


def _relevance_to_confidence(value: Any, default: float = 1.0) -> float:
    """vault 外部 relevance 值 → SemanticSegment.confidence（[0.0, 1.0]）。

    AUD-38601：旧写法 ``float(clip.get("relevance", 1.0) or 1.0)`` 有两处缺陷：
    1. 合法值 0.0 被 ``or`` 误判为缺失而置为 1.0（零相关被报成满置信）；
    2. 非数值（如 "high"）令 float() 抛 ValueError 穿透，越界值（如 2.5）
       触发 SemanticSegment.__post_init__ 的域校验 ValueError。

    规则：
    - None / 缺失 / 非数值 / NaN → default（视为未提供）；
    - 合法数值（含 0.0）按原值保留；
    - 越界数值钳制到 [0.0, 1.0]（confidence 的文档域）。
    """
    if value is None:
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(num):
        return default
    return min(1.0, max(0.0, num))


def clip_to_semantic_segment(
    clip: dict[str, Any],
    *,
    asset_id: str,
    fps: float = 24.0,
    segment_id: str | None = None,
) -> SemanticSegment:
    """vault clip 字典 → SHOT 类型 SemanticSegment。

    Args:
        clip: vault clip 字典（含 start_sec/end_sec/duration_sec/shot_type/keyframes）。
            通常从 vault 查询服务拿（/api/search 命中视频镜头时返回）。
        asset_id: 主项目 asset_id（vault 的 media_source.topos_asset_id 或调用方指定）。
        fps: 帧率（秒 → 帧转换用）。
        segment_id: 显式 segment_id；None 则用 clip_id。

    Returns:
        SemanticSegment(kind=SHOT)，payload 带 vault 的 caption/shot_type/keyframes。
    """
    fps = max(fps, 1.0)  # 防 0/负
    start_sec = float(clip.get("start_sec", 0.0) or 0.0)
    end_sec = float(clip.get("end_sec", start_sec) or start_sec)
    start_frame = max(0, int(round(start_sec * fps)))
    end_frame = max(start_frame + 1, int(round(end_sec * fps)))
    duration = end_frame - start_frame

    clip_id = clip.get("clip_id", "")
    # AUD-38600: 旧单行写法 `segment_id or f"vault_clip_{clip_id}" if clip_id
    # else ...` 因三元表达式优先级低于 or，实际解析为
    # `(segment_id or ...) if clip_id else ...`——clip_id 为空时调用方
    # 显式传入的 segment_id 被静默丢弃。改为显式分支消除歧义。
    if segment_id:
        sid = segment_id
    elif clip_id:
        sid = f"vault_clip_{clip_id}"
    else:
        sid = f"vault_shot_{start_frame}"

    payload = {
        "clip_id": clip_id,
        "shot_type": clip.get("shot_type"),
        "caption": clip.get("caption"),
        "tags": clip.get("tags", []),
        "keyframes": clip.get("keyframes", []),
        "source": "vault",
    }

    return SemanticSegment(
        segment_id=sid,
        asset_id=asset_id,
        source_range=FrameRange(start=start_frame, duration=duration),
        kind=SegmentKind.SHOT,
        payload=payload,
        confidence=_relevance_to_confidence(clip.get("relevance")),
        provenance={"source": "topos_vault", "clip_id": clip_id},
    )


def face_track_to_segments(
    face_track: dict[str, Any],
    clip: dict[str, Any],
    *,
    asset_id: str,
    fps: float = 24.0,
) -> SemanticSegment:
    """vault face_track + clip → FACE 类型 SemanticSegment。

    Args:
        face_track: face_track 字典（含 character/cluster_id/face_angle/completeness）。
        clip: 该 face 所属的 clip（提供时间范围）。
        asset_id: 主项目 asset_id。
        fps: 帧率。

    Returns:
        SemanticSegment(kind=FACE)。若有 cluster_id/character，kind 升级为 CHARACTER。
    """
    fps = max(fps, 1.0)
    start_sec = float(clip.get("start_sec", 0.0) or 0.0)
    end_sec = float(clip.get("end_sec", start_sec) or start_sec)
    start_frame = max(0, int(round(start_sec * fps)))
    end_frame = max(start_frame + 1, int(round(end_sec * fps)))
    duration = end_frame - start_frame

    face_id = face_track.get("face_id", "")
    has_character = bool(face_track.get("character")) or face_track.get("cluster_id") is not None
    kind = SegmentKind.CHARACTER if has_character else SegmentKind.FACE

    sid = f"vault_face_{face_id}" if face_id else f"vault_face_{start_frame}"

    payload = {
        "face_id": face_id,
        "clip_id": clip.get("clip_id", ""),
        "character": face_track.get("character"),
        "cluster_id": face_track.get("cluster_id"),
        "face_angle": face_track.get("face_angle"),
        "completeness": face_track.get("completeness"),
        "source": "vault",
    }

    return SemanticSegment(
        segment_id=sid,
        asset_id=asset_id,
        source_range=FrameRange(start=start_frame, duration=duration),
        kind=kind,
        payload=payload,
        confidence=0.9,  # 人脸检测置信度由 provider 给，这里给保守值
        provenance={"source": "topos_vault", "face_id": face_id},
    )


def transcript_line_to_segment(
    line: dict[str, Any],
    clip: dict[str, Any],
    *,
    asset_id: str,
    fps: float = 24.0,
) -> SemanticSegment:
    """vault transcript_line + clip → TRANSCRIPT 类型 SemanticSegment。

    Args:
        line: transcript_line 字典（含 text/speaker/character）。
        clip: 该台词所属的 clip（提供时间范围，台词时间更精确但 clip 已够用）。
        asset_id: 主项目 asset_id。
        fps: 帧率。
    """
    fps = max(fps, 1.0)
    start_sec = float(line.get("start_sec", clip.get("start_sec", 0.0)) or 0.0)
    end_sec = float(line.get("end_sec", start_sec) or start_sec)
    start_frame = max(0, int(round(start_sec * fps)))
    end_frame = max(start_frame + 1, int(round(end_sec * fps)))
    duration = end_frame - start_frame

    line_id = line.get("line_id", "")
    sid = f"vault_transcript_{line_id}" if line_id else f"vault_transcript_{start_frame}"

    payload = {
        "line_id": line_id,
        "clip_id": clip.get("clip_id", ""),
        "text": line.get("text", ""),
        "speaker": line.get("speaker"),
        "character": line.get("character"),
        "emotion": line.get("emotion"),
        "source": "vault",
    }

    return SemanticSegment(
        segment_id=sid,
        asset_id=asset_id,
        source_range=FrameRange(start=start_frame, duration=duration),
        kind=SegmentKind.TRANSCRIPT,
        payload=payload,
        confidence=1.0,
        provenance={"source": "topos_vault", "line_id": line_id},
    )


def search_hit_to_segments(
    hit: dict[str, Any],
    *,
    asset_id: str,
    fps: float = 24.0,
) -> list[SemanticSegment]:
    """vault 检索命中（file_explorer 格式）→ SemanticSegment 列表。

    一个检索命中可能对应多个 segment（如视频命中 → SHOT + 各 TRANSCRIPT）。
    本函数做粗映射：根据 vault_media_type 决定 segment kind。

    Args:
        hit: vault 查询返回的文件字典（含 vault_caption/vault_tags/vault_media_type）。
        asset_id: 主项目 asset_id。
        fps: 帧率。

    Returns:
        SemanticSegment 列表（命中整图 → 1 个 VISUAL/TAG；命中整曲 → 1 个 AUDIO）。
    """
    segments: list[SemanticSegment] = []
    media_type = hit.get("vault_media_type", "")
    caption = hit.get("vault_caption")
    tags = hit.get("vault_tags", [])

    if media_type in ("image", "image_sequence"):
        # 图片命中 → VISUAL segment（整图，无时间范围，给 0-1 帧）
        sid = f"vault_visual_{asset_id}"
        segments.append(SemanticSegment(
            segment_id=sid,
            asset_id=asset_id,
            source_range=FrameRange(start=0, duration=1),
            kind=SegmentKind.VISUAL,
            payload={
                "caption": caption, "tags": tags,
                "path": hit.get("path"), "source": "vault",
            },
            confidence=_relevance_to_confidence(hit.get("vault_relevance")),
            provenance={"source": "topos_vault"},
        ))
    elif media_type == "audio":
        sid = f"vault_audio_{asset_id}"
        segments.append(SemanticSegment(
            segment_id=sid,
            asset_id=asset_id,
            source_range=FrameRange(start=0, duration=1),
            kind=SegmentKind.AUDIO,
            payload={
                "description": caption, "mood": hit.get("vault_mood"),
                "tags": tags, "path": hit.get("path"), "source": "vault",
            },
            confidence=_relevance_to_confidence(hit.get("vault_relevance")),
            provenance={"source": "topos_vault"},
        ))
    # 视频命中需要 clip 级数据（不在 file_explorer 命中里），留给 clip_to_semantic_segment

    return segments
