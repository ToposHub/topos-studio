"""ProjectSnapshot 服务（Phase 1）。

给模型一个**脱敏的只读世界状态**（TAEP §5.2）。snapshot 不是 ``.topos`` 原文件，
也不含帧/缩略图/本机绝对路径/secret/未授权脚本/原始二进制。

职责：

- 从 ``ProjectSequences``（两层状态）读取。
- 按 ``detail_level``（minimal/structure/full）与分页控制 token 量。
- **路径脱敏**：绝不返回本机绝对路径；display_name 来自 ``MediaAsset``。
- 选配：转写/场景切分/镜头标签等“分析资产”摘要（P5 完整实现，此处预留接口）。
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.ai_editing.models import (
    TAEP_PROTOCOL_VERSION,
    DetailLevel,
    ProjectSnapshot,
    SnapshotAsset,
    SnapshotTrack,
)

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

logger = logging.getLogger(__name__)

# AUD-14418：provider 取状态时预期内的领域/IO 错误（缺记录、状态机、存储/IO）。
# AttributeError/TypeError 等编程错误不在列——必须照常上抛，不得被掩盖。
_PROVIDER_STATUS_ERRORS: tuple[type[BaseException], ...] = (
    LookupError, ValueError, RuntimeError, OSError,
)


def _redact_path(path: str) -> str:
    """脱敏：把本机绝对路径转为不泄露用户目录的显示名。

    保守策略：只保留 basename，剥离目录。AI 不需要路径即可操作（用 asset_id）。
    """
    if not path:
        return ""
    # 兼容 win/unix 分隔符
    for sep in ("/", "\\"):
        if sep in path:
            path = path.rsplit(sep, 1)[-1]
    return path


def _build_track_summary(
    sequence: TimelineSequence, track: EditTrack, detail_level: DetailLevel
) -> SnapshotTrack:
    """构建单轨摘要。full 级别才带 clip 详情。"""
    clips: list[dict[str, Any]] = []
    source_clips = (
        track.subtitle_clips
        if getattr(track, "track_type", "") == "subtitle"
        else track.clips
    )
    if detail_level == DetailLevel.FULL:
        for clip in source_clips:
            if getattr(track, "track_type", "") == "subtitle":
                clips.append({
                    "clip_id": clip.clip_id,
                    "timeline": {
                        "start": clip.timeline_start,
                        "duration": clip.timeline_duration,
                    },
                    "text": clip.text,
                    "is_enabled": getattr(clip, "is_enabled", True),
                    "style_override": (
                        clip.style_override.to_dict()
                        if getattr(clip, "style_override", None) is not None
                        else None
                    ),
                    "caption_metadata": clip.caption_metadata.to_dict(),
                })
                continue
            clips.append({
                "clip_id": clip.clip_id,
                "asset_id": clip.asset_id,
                "timeline": {"start": clip.timeline_start, "duration": clip.timeline_duration},
                "source": {"start": clip.source_start, "duration": clip.source_duration},
                "is_enabled": getattr(clip, "is_enabled", True),
            })
    elif detail_level == DetailLevel.STRUCTURE:
        # structure 级别：只给 clip 计数与时间范围
        clips.append({"_count": len(source_clips)})
    caption_metadata = getattr(track, "caption_metadata", None)
    return SnapshotTrack(
        track_id=track.track_id,
        kind=track.track_type,
        index=track.index,
        name=getattr(track, "name", ""),
        locked=getattr(track, "is_locked", False),
        clips=tuple(clips),
        caption_metadata=(
            caption_metadata.to_dict()
            if caption_metadata is not None else None
        ),
    )


def build_snapshot(
    state: ProjectSequences,
    *,
    detail_level: DetailLevel = DetailLevel.STRUCTURE,
    timeline_id: str | None = None,
    asset_limit: int | None = None,
    analysis_provider: Any | None = None,
) -> ProjectSnapshot:
    """从两层状态构建脱敏 snapshot。

    Args:
        state: ``ProjectSequences``（project + sequences map）。
        detail_level: minimal/structure/full。
        timeline_id: 指定 snapshot 的 timeline；None 用 active。
        asset_limit: 限制返回的 asset 数量（分页；None=全部）。
        analysis_provider: 可选，提供 analysis_status 摘要（P5 实现；此处 None
            时 status 全为 "unknown"）。
    """
    project = state.project

    # 选择 timeline
    target_tid = timeline_id or project.active_timeline_id
    sequence = None
    if target_tid is not None:
        sequence = state.sequences.get(target_tid)

    # assets（脱敏）
    assets: list[SnapshotAsset] = []
    for i, (asset_id, asset) in enumerate(project.assets.items()):
        if asset_limit is not None and i >= asset_limit:
            break
        analysis_status = "unknown"
        if analysis_provider is not None:
            try:
                analysis_status = analysis_provider.get_status(asset_id)
            except _PROVIDER_STATUS_ERRORS as exc:
                # AUD-14418：只捕获 provider 的领域/IO 错误并留痕回退，
                # 不再用宽泛 except Exception 静默吞掉编程错误。
                logger.warning(
                    "analysis status unavailable for asset %s: %s: %s",
                    asset_id, type(exc).__name__, exc,
                )
                analysis_status = "unknown"
        # duration_frames：从 metadata 推断（若存在）
        duration_frames = None
        meta = getattr(asset, "metadata", {}) or {}
        if isinstance(meta, dict):
            df = meta.get("duration_frames") or meta.get("frame_count")
            if df is not None:
                try:
                    duration_frames = int(df)
                except (TypeError, ValueError):
                    duration_frames = None
        assets.append(SnapshotAsset(
            asset_id=asset_id,
            name=getattr(asset, "display_name", None) or _redact_path(getattr(asset, "path", "")),
            kind=getattr(asset, "media_type", "unknown"),
            duration_frames=duration_frames,
            analysis_status=analysis_status,
            offline=bool(getattr(asset, "is_offline", False)),
        ))

    # active timeline
    active_timeline: dict[str, Any] | None = None
    if sequence is not None:
        # format
        fps = sequence.fps
        fps_str = f"{fps.num}/{fps.den}" if hasattr(fps, "num") else str(fps)
        tracks: list[dict[str, Any]] = []
        for track in sequence.all_tracks:
            tracks.append(_build_track_summary(sequence, track, detail_level).to_dict())
        active_timeline = {
            "timeline_id": sequence.timeline_id,
            "name": sequence.name,
            "format": {
                "width": sequence.width,
                "height": sequence.height,
                "fps": fps_str,
                "sample_rate": sequence.sample_rate,
            },
            "revision": sequence.revision,
            "duration_frames": sequence.duration_frames,
            "tracks": tracks,
        }

    return ProjectSnapshot(
        protocol=TAEP_PROTOCOL_VERSION,
        project_id=project.project_id,
        name=project.name,
        revision=project.revision,
        active_timeline=active_timeline,
        assets=tuple(assets),
        detail_level=detail_level,
    )
