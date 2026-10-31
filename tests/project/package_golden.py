"""DESK-08A · 双端黄金包构建器（经生产序列化/保存路径，确定性输出）。

黄金包的定义：**结构黄金**——所有 JSON 条目由生产 serializer 真实产出
（非手写字典），ID/时间戳固定，逐条目可做 canonical JSON 对拍。ZIP 容器
字节（时间戳/压缩细节）不参与契约。

后续批次扩展：
- DESK-01B：内嵌资源（resources/ + 资源索引）黄金形态
- DESK-03A/B：另存/携带来源 backing 的黄金对拍
- DESK-04A：逐层未知字段注入的黄金形态
"""

from __future__ import annotations

import os
from typing import Any

from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    ProjectBin,
    TimelineSequenceRef,
)
from src.features.edit.core.serialization import EditProjectSerializer
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational
from src.shared.project.topos_project import (
    ToposProject,
    MediaAssetRef,
    ProjectBinRef,
    TimelineRef,
)

# ------------------------------------------------------------------
# 固定标识（黄金包确定性）
# ------------------------------------------------------------------

GOLDEN_PROJECT_ID = "golden-desk01a-0001"
GOLDEN_PROJECT_NAME = "DESK Golden Package"
GOLDEN_ASSET_VIDEO = "golden-asset-video"
GOLDEN_ASSET_AUDIO = "golden-asset-audio"
GOLDEN_BIN_ROOT = "golden-bin-root"
GOLDEN_BIN_SUB = "golden-bin-sub"
GOLDEN_TIMELINE = "golden-timeline-main"
GOLDEN_CREATED_AT = 1770000000.0

GOLDEN_VIDEO_FILENAME = "golden_source.mp4"
GOLDEN_AUDIO_FILENAME = "golden_voice.wav"


def build_golden_edit_project() -> EditProject:
    """黄金 EditProject：视频+音频素材、两级 bin、时间线引用。"""
    return EditProject(
        project_id=GOLDEN_PROJECT_ID,
        name=GOLDEN_PROJECT_NAME,
        assets={
            GOLDEN_ASSET_VIDEO: MediaAsset(
                asset_id=GOLDEN_ASSET_VIDEO,
                path=os.path.join("/golden/media", GOLDEN_VIDEO_FILENAME),
                media_type="video",
                display_name="Golden Video",
                metadata={
                    "width": 3840, "height": 2160,
                    "fps": 30000.0 / 1001.0,
                    "duration_seconds": 10.0,
                    "total_frames": 300,
                    "codec": "h264",
                    "audio_info": {"sample_rate": 48000, "channels": 2},
                },
                created_at=GOLDEN_CREATED_AT,
                reel="GLDN",
            ),
            GOLDEN_ASSET_AUDIO: MediaAsset(
                asset_id=GOLDEN_ASSET_AUDIO,
                path=os.path.join("/golden/media", GOLDEN_AUDIO_FILENAME),
                media_type="audio",
                display_name="Golden Voice",
                metadata={"duration_seconds": 8.0, "codec": "pcm_s16le"},
                created_at=GOLDEN_CREATED_AT,
            ),
        },
        bins={
            GOLDEN_BIN_ROOT: ProjectBin(
                bin_id=GOLDEN_BIN_ROOT, name="Root", parent_id=None,
                asset_ids=(GOLDEN_ASSET_VIDEO, GOLDEN_ASSET_AUDIO),
                timeline_ids=(GOLDEN_TIMELINE,)),
            GOLDEN_BIN_SUB: ProjectBin(
                bin_id=GOLDEN_BIN_SUB, name="Sub", parent_id=GOLDEN_BIN_ROOT,
                asset_ids=(GOLDEN_ASSET_AUDIO,)),
        },
        timelines={
            GOLDEN_TIMELINE: TimelineSequenceRef(
                timeline_id=GOLDEN_TIMELINE,
                name="Main 23.976",
                fps=30000.0 / 1001.0,
                width=3840, height=2160,
                sample_rate=48000,
                created_at=GOLDEN_CREATED_AT,
            ),
        },
        root_bin_id=GOLDEN_BIN_ROOT,
        active_timeline_id=GOLDEN_TIMELINE,
        created_at=GOLDEN_CREATED_AT,
        modified_at=GOLDEN_CREATED_AT,
    )


def build_golden_timeline_sequence() -> TimelineSequence:
    """黄金 TimelineSequence：V1/A1 各一 clip（含子帧偏移与 NTSC 帧率）。"""
    video_clip = ClipInstance(
        clip_id="golden-clip-v1",
        asset_id=GOLDEN_ASSET_VIDEO,
        track_id="golden-track-v1",
        timeline_start=0, timeline_duration=48,
        source_start=12, source_duration=48,
        audio_sample_offset=17,          # F3.3 子帧偏移（采样域语义）
        source_sample_offset=0,
    )
    audio_clip = ClipInstance(
        clip_id="golden-clip-a1",
        asset_id=GOLDEN_ASSET_AUDIO,
        track_id="golden-track-a1",
        timeline_start=0, timeline_duration=48,
        source_start=0, source_duration=48,
    )
    return TimelineSequence(
        timeline_id=GOLDEN_TIMELINE,
        name="Main 23.976",
        fps=Rational(30000, 1001),
        width=3840, height=2160,
        sample_rate=48000,
        video_tracks=(EditTrack(
            track_id="golden-track-v1", name="V1", track_type="video",
            index=0, clips=(video_clip,)),),
        audio_tracks=(EditTrack(
            track_id="golden-track-a1", name="A1", track_type="audio",
            index=0, clips=(audio_clip,)),),
        markers=(),
        revision=7,
    )


def build_golden_topos_project() -> ToposProject:
    """黄金 ToposProject（project.json 索引层）。"""
    edit = build_golden_edit_project()
    return ToposProject(
        project_id=edit.project_id,
        name=edit.name,
        project_mode="edit_project",
        assets={
            asset_id: MediaAssetRef(
                asset_id=asset.asset_id, path=asset.path,
                media_type=asset.media_type,
                display_name=asset.display_name,
                is_offline=asset.is_offline,
            )
            for asset_id, asset in edit.assets.items()
        },
        bins={
            bin_id: ProjectBinRef(
                bin_id=b.bin_id, name=b.name, parent_id=b.parent_id,
                asset_count=len(b.asset_ids))
            for bin_id, b in edit.bins.items()
        },
        timelines={
            GOLDEN_TIMELINE: TimelineRef(
                timeline_id=GOLDEN_TIMELINE, name="Main 23.976",
                fps=30000.0 / 1001.0, width=3840, height=2160)
        },
        settings={
            "root_bin_id": GOLDEN_BIN_ROOT,
            "project_sample_rate": 48000,
        },
        active_timeline_id=GOLDEN_TIMELINE,
        created_at=GOLDEN_CREATED_AT,
        modified_at=GOLDEN_CREATED_AT,
        revision=7,
    )


def golden_save_kwargs() -> dict[str, Any]:
    """生产 ToposProjectIO.save 的黄金模块载荷（生产 serializer 产出）。"""
    edit = build_golden_edit_project()
    sequence = build_golden_timeline_sequence()
    return {
        "project": build_golden_topos_project(),
        "full_assets_data": {
            asset_id: EditProjectSerializer._asset_to_dict(asset)
            for asset_id, asset in edit.assets.items()
        },
        "full_bins_data": {
            bin_id: EditProjectSerializer._bin_to_dict(bin_obj)
            for bin_id, bin_obj in edit.bins.items()
        },
        "full_timelines_data": {
            timeline_id: EditProjectSerializer._timeline_ref_to_dict(timeline)
            for timeline_id, timeline in edit.timelines.items()
        },
        "timeline_sequences_data": {GOLDEN_TIMELINE: sequence.to_dict()},
        "smart_bins_data": {},
        "comp_clips_data": {},
    }


def save_golden_package(directory: str, filename: str = "golden.topos") -> str:
    """经生产 ToposProjectIO.save 写出黄金包，返回包路径。"""
    from src.shared.project.topos_project_io import ToposProjectIO

    kwargs = golden_save_kwargs()
    filepath = os.path.join(directory, filename)
    assert ToposProjectIO.save(kwargs.pop("project"), filepath, **kwargs)
    return filepath
