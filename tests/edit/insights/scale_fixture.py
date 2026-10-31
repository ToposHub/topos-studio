"""T04 · 规模 fixtures（100 / 1万条；10万按环境变量显式开启）。

确定性构造（固定 ID、无随机源、无网络/GPU/模型）：

- 单序列 24fps，一条视频轨，N 个 clip 顺次相接（每个 24 帧 → 无重叠、
  无间隙，duration = 24N，可独立手算）；
- 素材池 ceil(N/3) 个，clip 循环引用（同一素材多次使用）；
- 每 100 个 clip 附一条字幕（text 含 clip 序号）与一个 marker。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineMarker,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

CLIP_FRAMES = 24  # 每个 clip 的时长；duration = 24N 可手算


@dataclass
class ScaleState:
    state: ProjectSequences
    n_clips: int
    n_assets: int
    n_cues: int
    n_markers: int


def build_scale_state(n_clips: int) -> ScaleState:
    assert n_clips >= 1
    n_assets = (n_clips + 2) // 3
    n_cues = n_clips // 100
    n_markers = n_clips // 100

    assets = {
        f"sc-asset-{i:05d}": MediaAsset(
            asset_id=f"sc-asset-{i:05d}",
            path=f"/synthetic/day{ i % 7 }/clip_{i:05d}.mov",
            media_type="video",
            display_name=f"Scale_{i:05d}.mov",
            metadata={"width": 1920, "height": 1080, "fps": 24.0},
        )
        for i in range(n_assets)
    }
    clips = tuple(
        ClipInstance(
            clip_id=f"sc-clip-{i:06d}",
            asset_id=f"sc-asset-{(i % n_assets):05d}",
            track_id="sc-track-v1",
            timeline_start=i * CLIP_FRAMES,
            timeline_duration=CLIP_FRAMES,
            source_start=i * CLIP_FRAMES,
            source_duration=CLIP_FRAMES,
        )
        for i in range(n_clips)
    )
    cues = tuple(
        SubtitleClip(
            clip_id=f"sc-cue-{i:06d}",
            track_id="sc-track-sub1",
            timeline_start=i * 100 * CLIP_FRAMES,
            timeline_duration=CLIP_FRAMES,
            text=f"规模字幕第{i}条：重复文本 common-word-{i % 10}",
        )
        for i in range(n_cues)
    )
    markers = tuple(
        TimelineMarker(
            marker_id=f"sc-mark-{i:06d}",
            frame=i * 100 * CLIP_FRAMES,
            name=f"M{i}",
            color="#ff0000",
            comment="",
            kind="standard",
        )
        for i in range(n_markers)
    )
    v1 = EditTrack(
        track_id="sc-track-v1", name="V1", track_type="video", index=0, clips=clips
    )
    sub1 = EditTrack(
        track_id="sc-track-sub1", name="SUB1", track_type="subtitle", index=0,
        subtitle_clips=cues,
    )
    seq = TimelineSequence(
        timeline_id="sc-seq-1",
        name="Scale",
        fps=Rational(24, 1),
        video_tracks=(v1,),
        subtitle_tracks=(sub1,),
        markers=markers,
    )
    project = replace(
        EditProject.create("ScaleProject"),
        project_id="sc-project-1",
        assets=assets,
        timelines={
            "sc-seq-1": TimelineSequenceRef(
                timeline_id="sc-seq-1", name="Scale", fps=24.0, created_at=1.0
            )
        },
        active_timeline_id="sc-seq-1",
    )
    return ScaleState(
        state=ProjectSequences(project=project, sequences={"sc-seq-1": seq}),
        n_clips=n_clips,
        n_assets=n_assets,
        n_cues=n_cues,
        n_markers=n_markers,
    )
