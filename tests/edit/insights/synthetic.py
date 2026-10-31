"""合成黄金工程（T01 契约验证 / T04 黄金数据共用的构造层）。

设计（计划 T04 的边界覆盖清单）：

- 两序列同名可区分：S1/S2 均含 "cut" 语义；S2 与 S1 有同源镜头（同
  asset 两次使用）与重复字幕文本（跨序列搜索必须保留独立结果）；
- 帧率：S1 = 24/1 NDF，S2 = 30000/1001 DF（NTSC），S3(ref) = 25 未加载；
- 叠轨（V1 与 A1 时间上重叠）、间隙（V1 320→400）、非零入点
  （C1 source_start=24）、恒速 2.0（C3）、冻结（C4，映射 unknown）；
- 中文多行带引号字幕（CSV/JSON 转义边界）；
- 未加载引用（S3 ref 在、sequence map 缺）；
- 离线素材（A_sfx is_offline）。

全部 ID 固定、全部区间可手算。**预期值不调用被测函数**：T04 的
``expected.py`` 用人工手算数字断言（见该文件）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineMarker,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

# ── 固定 ID（黄金工程的所有稳定 ID）──────────────────────────────

P_ID = "gp-project-1"

ASSET_A = "gp-asset-camA"   # CamA_Take3.mov，被 S1/S2 共用（重复源）
ASSET_B = "gp-asset-camB"   # CamB_Wide.mov
ASSET_SFX = "gp-asset-sfx"  # ambience_room.wav，离线

SEQ_MAIN = "gp-seq-main"    # 24fps，中文内容主时间线
SEQ_NTSC = "gp-seq-ntsc"    # 30000/1001 DF
SEQ_UNLOADED = "gp-seq-unloaded"  # 25fps，仅 ref

TRACK_V1 = "gp-track-v1"
TRACK_A1 = "gp-track-a1"
TRACK_SUB1 = "gp-track-sub1"
TRACK_V2 = "gp-track-v2"
TRACK_SUB2 = "gp-track-sub2"

CLIP_A1 = "gp-clip-a1"      # CamA [0,120)，source [24,144)
CLIP_A2 = "gp-clip-a2"      # CamA [120,200)，source [500,580)（同素材二次使用）
CLIP_B1 = "gp-clip-b1"      # CamB [200,320)，speed 2.0，source [0,240)
CLIP_B2 = "gp-clip-b2"      # CamB [400,460)，冻结帧 → mapping unknown
CLIP_SFX = "gp-clip-sfx"    # SFX [0,200)
CLIP_NTSC1 = "gp-clip-ntsc1"  # S2：CamA [0,100)

CUE_ZH = "gp-cue-zh"        # "你好，世界" [10,70)
CUE_QUOTE = "gp-cue-quote"  # 引号+多行+中文 [80,150)
CUE_SHARED = "gp-cue-shared"  # S2 与 S1 CUE_ZH 同文本？不——独立文本：
# CUE_SHARED 文本与 S1 任何 cue 都不同，但与 CUE_ZH 形成"跨序列重复"
# 需求：T10 要求"重复字幕/同一素材两次使用均有独立结果"。这里让 S2 的
# CUE_SHARED 文本 = "你好，世界"（与 CUE_ZH 完全相同），跨序列重复。

MARK_TURN = "gp-mark-turn"    # frame 100 "转折点"
MARK_CHAP = "gp-mark-chapter" # frame 250 章节

CUE_TEXT_ZH = "你好，世界"
CUE_TEXT_QUOTE = 'Second line with "quotes"\nand 中文，逗号。'
CUE_TEXT_SHARED = "你好，世界"

FPS_MAIN = Rational(24, 1)
FPS_NTSC = Rational(30000, 1001)
FPS_UNLOADED = 25.0  # ref 层是 float


def _video_asset(asset_id: str, filename: str, directory: str, **log: Any) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=f"{directory}/{filename}",
        media_type="video",
        display_name=filename,
        metadata={"width": 1920, "height": 1080, "fps": 24.0, "duration_seconds": 60.0},
        **log,
    )


def _audio_asset(asset_id: str, filename: str, directory: str, offline: bool = False) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=f"{directory}/{filename}",
        media_type="audio",
        display_name=filename,
        metadata={"duration_seconds": 30.0, "audio_info": {"channels": 2}},
        is_offline=offline,
    )


@dataclass
class SyntheticState:
    """builder 产物：两层状态 + 便于断言的引用。"""

    state: ProjectSequences
    project: EditProject
    sequences: dict[str, TimelineSequence]
    main: TimelineSequence
    ntsc: TimelineSequence
    revision_main: int = field(default=3)
    revision_ntsc: int = field(default=7)
    revision_project: int = field(default=11)


def build_synthetic_state() -> SyntheticState:
    """构建两序列 + 一未加载 ref 的合成两层状态（全部区间可手算）。"""

    # ── 素材池 ──
    project = EditProject.create("GoldenProject")
    project = replace(project, project_id=P_ID)
    assets = {
        ASSET_A: _video_asset(
            ASSET_A, "CamA_Take3.mov", "/Volumes/Media/Day01",
            keywords=("interview", "hero"), rating=4,
        ),
        ASSET_B: _video_asset(ASSET_B, "CamB_Wide.mov", "/Volumes/Media/Day01"),
        ASSET_SFX: _audio_asset(
            ASSET_SFX, "ambience_room.wav", "/Volumes/Media/Day01/Audio", offline=True
        ),
    }
    project = replace(project, assets=assets)

    # ── S1 主时间线（24fps NDF）──
    v1_clips = (
        ClipInstance(
            clip_id=CLIP_A1, asset_id=ASSET_A, track_id=TRACK_V1,
            timeline_start=0, timeline_duration=120,
            source_start=24, source_duration=120,
        ),
        ClipInstance(
            clip_id=CLIP_A2, asset_id=ASSET_A, track_id=TRACK_V1,
            timeline_start=120, timeline_duration=80,
            source_start=500, source_duration=80,
        ),
        ClipInstance(
            clip_id=CLIP_B1, asset_id=ASSET_B, track_id=TRACK_V1,
            timeline_start=200, timeline_duration=120,
            source_start=0, source_duration=240,  # 2 倍速：240 源帧 → 120 时间线帧
            speed=SpeedParams(speed=2.0),
        ),
        ClipInstance(
            clip_id=CLIP_B2, asset_id=ASSET_B, track_id=TRACK_V1,
            timeline_start=400, timeline_duration=60,
            source_start=0, source_duration=60,
            freeze_source_frame=10,
        ),
    )
    v1 = EditTrack(
        track_id=TRACK_V1, name="V1", track_type="video", index=0, clips=v1_clips,
    )
    a1 = EditTrack(
        track_id=TRACK_A1, name="A1", track_type="audio", index=0,
        clips=(
            ClipInstance(
                clip_id=CLIP_SFX, asset_id=ASSET_SFX, track_id=TRACK_A1,
                timeline_start=0, timeline_duration=200,
                source_start=0, source_duration=200,
            ),
        ),
    )
    sub1 = EditTrack(
        track_id=TRACK_SUB1, name="SUB1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id=CUE_ZH, track_id=TRACK_SUB1,
                timeline_start=10, timeline_duration=60, text=CUE_TEXT_ZH,
            ),
            SubtitleClip(
                clip_id=CUE_QUOTE, track_id=TRACK_SUB1,
                timeline_start=80, timeline_duration=70, text=CUE_TEXT_QUOTE,
            ),
        ),
    )
    main = TimelineSequence(
        timeline_id=SEQ_MAIN,
        name="主时间线",
        fps=FPS_MAIN,
        video_tracks=(v1,),
        audio_tracks=(a1,),
        subtitle_tracks=(sub1,),
        markers=(
            TimelineMarker(
                marker_id=MARK_TURN, frame=100, name="转折点",
                color="#ff0000", comment="剧情反转", kind="standard",
            ),
            TimelineMarker(
                marker_id=MARK_CHAP, frame=250, name="Chapter 2",
                color="#0000ff", comment="", kind="chapter",
            ),
        ),
        revision=3,
    )

    # ── S2 NTSC DF ──
    v2 = EditTrack(
        track_id=TRACK_V2, name="V2", track_type="video", index=0,
        clips=(
            ClipInstance(
                clip_id=CLIP_NTSC1, asset_id=ASSET_A, track_id=TRACK_V2,
                timeline_start=0, timeline_duration=100,
                source_start=24, source_duration=100,
            ),
        ),
    )
    sub2 = EditTrack(
        track_id=TRACK_SUB2, name="SUB2", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id=CUE_SHARED, track_id=TRACK_SUB2,
                timeline_start=0, timeline_duration=100, text=CUE_TEXT_SHARED,
            ),
        ),
    )
    ntsc = TimelineSequence(
        timeline_id=SEQ_NTSC,
        name="NTSC_Cut",
        fps=FPS_NTSC,
        video_tracks=(v2,),
        subtitle_tracks=(sub2,),
        revision=7,
    )

    refs = {
        SEQ_MAIN: TimelineSequenceRef(
            timeline_id=SEQ_MAIN, name=main.name, fps=24.0, created_at=1.0,
        ),
        SEQ_NTSC: TimelineSequenceRef(
            timeline_id=SEQ_NTSC, name=ntsc.name,
            fps=FPS_NTSC.to_float(), width=1920, height=1080, created_at=2.0,
        ),
        SEQ_UNLOADED: TimelineSequenceRef(
            timeline_id=SEQ_UNLOADED, name="Unloaded_Cut",
            fps=FPS_UNLOADED, created_at=3.0,
        ),
    }
    project = replace(
        project,
        timelines=refs,
        active_timeline_id=SEQ_MAIN,
        revision=11,
    )
    sequences = {SEQ_MAIN: main, SEQ_NTSC: ntsc}
    return SyntheticState(
        state=ProjectSequences(project=project, sequences=sequences),
        project=project,
        sequences=sequences,
        main=main,
        ntsc=ntsc,
    )


def build_bumped_state(base: SyntheticState) -> SyntheticState:
    """revision 全体 +1、内容不变的派生状态（content_hash 恒等测试用）。

    构造方式：replace 出新 project/sequence 对象（revision 字段不同），
    实体内容逐字段一致。
    """
    bumped_main = replace(base.main, revision=base.main.revision + 1)
    bumped_ntsc = replace(base.ntsc, revision=base.ntsc.revision + 1)
    bumped_project = replace(base.project, revision=base.project.revision + 1)
    sequences = {SEQ_MAIN: bumped_main, SEQ_NTSC: bumped_ntsc}
    return SyntheticState(
        state=ProjectSequences(project=bumped_project, sequences=dict(sequences)),
        project=bumped_project,
        sequences=sequences,
        main=bumped_main,
        ntsc=bumped_ntsc,
        revision_main=bumped_main.revision,
        revision_ntsc=bumped_ntsc.revision,
        revision_project=bumped_project.revision,
    )
