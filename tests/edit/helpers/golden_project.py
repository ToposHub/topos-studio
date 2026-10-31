"""F0.3 黄金工程 builder——最小工程与扩展工程，供 F1.x 起各功能任务复用。

约定：
- 固定 clip/track/sequence ID；asset_id 经生产入口 ``EditProject.import_asset``
  生成（确定性断言用路径解析，见 golden_structure.snapshot）。
- 变更尽量走生产命令（SetClipLinkedCommand / AddKeyframeCommand /
  UpdateClipColorStackCommand / build_nested_from_selection 事务），初始
  组装沿用既有测试的不可变模型 + with_* 模式。
- 扩展工程覆盖：双序列、嵌套、链接 A/V、混合帧率源、效果关键帧、字幕、
  调色栈、离线素材、Adjustment Clip。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from src.features.edit.core.commands import (
    AddKeyframeCommand,
    CommandStack,
    SetClipLinkedCommand,
    UpdateClipColorStackCommand,
)
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.nested_sequence import build_nested_from_selection
from src.features.edit.core.project import (
    EditProject,
    TimelineSequenceRef,
    update_offline_status,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

from tests.edit.helpers import golden_media


def build_minimal_golden_project(name: str = "GoldenMinimal") -> tuple[EditProject, TimelineSequence, dict[str, Any]]:
    """最小黄金工程：单序列 24fps + 纯色素材单片段（无外部文件依赖）。"""
    project = EditProject.create(name)
    project, solid_id = project.create_solid_color_asset(
        name="Golden Solid", color_rgb=(0.25, 0.5, 0.75)
    )
    clip = ClipInstance(
        clip_id="gm-clip-1", asset_id=solid_id, track_id="gm-v1",
        timeline_start=0, timeline_duration=48, source_start=0, source_duration=48,
    )
    track = EditTrack(track_id="gm-v1", name="V1", track_type="video", index=0, clips=(clip,))
    timeline = TimelineSequence(
        timeline_id="gm-seq-main", name="Golden Minimal", fps=Rational(24, 1),
        width=640, height=360, video_tracks=(track,),
    )
    return project, timeline, {"solid_asset_id": solid_id, "clip_id": "gm-clip-1"}


def build_extended_golden_project(tmp_path: Path) -> tuple[EditProject, dict[str, TimelineSequence], dict[str, Any]]:
    """扩展黄金工程：双顶层序列 + 嵌套 + 链接 A/V + 混 fps 源 + 关键帧 +
    字幕 + 调色栈 + 离线素材 + Adjustment Clip。

    媒体文件生成在 ``tmp_path/golden_media/`` 下，测试隔离、源只读。
    """
    media_dir = tmp_path / "golden_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    grad24_path = golden_media.write_gradient_video(
        media_dir / "grad24.mp4", width=640, height=360, frames=48, fps_num=24, fps_den=1)
    grad_mixed_path = golden_media.write_gradient_video(
        media_dir / "grad_mixed.mp4", width=640, height=360, frames=48,
        fps_num=30000, fps_den=1001)
    sine_path = golden_media.write_wav(media_dir / "sine1k.wav", golden_media.sine_pcm(1000.0, 4.0))
    inv_path = golden_media.write_wav(
        media_dir / "inv1k.wav", golden_media.inverted_phase_stereo_pcm(1000.0, 4.0))

    project = EditProject.create("GoldenExtended")
    project = project.import_asset(
        str(grad24_path), "video",
        {"width": 640, "height": 360, "fps": 24.0, "total_frames": 48, "codec": "h264"},
        display_name="grad24.mp4")
    project = project.import_asset(
        str(grad_mixed_path), "video",
        {"width": 640, "height": 360, "fps": 30000.0 / 1001.0, "total_frames": 48, "codec": "h264"},
        display_name="grad_mixed.mp4")
    project = project.import_asset(
        str(sine_path), "audio",
        {"duration": 4.0, "sample_rate": 48000, "channels": 2, "codec": "pcm_s16le"},
        display_name="sine1k.wav")
    project = project.import_asset(
        str(inv_path), "audio",
        {"duration": 4.0, "sample_rate": 48000, "channels": 2, "codec": "pcm_s16le"},
        display_name="inv1k.wav")
    project = project.import_asset(
        str(media_dir / "missing" / "offline.mov"), "video",
        {"codec": "prores"}, display_name="offline.mov")
    project, solid_id = project.create_solid_color_asset(
        name="Golden Solid", color_rgb=(1.0, 0.2, 0.1))
    project, adj_id = project.create_adjustment_clip(name="Golden Adj")
    project = update_offline_status(project)

    grad24_asset = project.get_asset_by_path(str(grad24_path))
    mixed_asset = project.get_asset_by_path(str(grad_mixed_path))
    sine_asset = project.get_asset_by_path(str(sine_path))
    inv_asset = project.get_asset_by_path(str(inv_path))
    offline_asset = project.get_asset_by_path(str(media_dir / "missing" / "offline.mov"))

    # ── MAIN 序列（24fps）──────────────────────────────────────────
    adj_effect = EffectInstance(effect_type="blur", params={"radius": 8.0, "mix": 1.0}, order=0)
    v1_clip = ClipInstance(
        clip_id="ge-main-v1", asset_id=grad24_asset.asset_id, track_id="ge-v1",
        timeline_start=0, timeline_duration=72, source_start=0, source_duration=48)
    nsrc_clip = ClipInstance(
        clip_id="ge-main-nsrc", asset_id=solid_id, track_id="ge-v1",
        timeline_start=72, timeline_duration=60, source_start=0, source_duration=60)
    adj_clip = ClipInstance(
        clip_id="ge-main-adj", asset_id=adj_id, track_id="ge-v2",
        timeline_start=0, timeline_duration=48, source_start=0, source_duration=48,
        effects=(adj_effect,))
    mixed_clip = ClipInstance(
        clip_id="ge-main-v3", asset_id=mixed_asset.asset_id, track_id="ge-v3",
        timeline_start=24, timeline_duration=72, source_start=0, source_duration=48,
        _source_fps_num=30000, _source_fps_den=1001,
        _timeline_fps_num=24, _timeline_fps_den=1)
    a1_clip = ClipInstance(
        clip_id="ge-main-a1", asset_id=sine_asset.asset_id, track_id="ge-a1",
        timeline_start=0, timeline_duration=96, source_start=0, source_duration=96)
    a2_clip = ClipInstance(
        clip_id="ge-main-a2", asset_id=inv_asset.asset_id, track_id="ge-a2",
        timeline_start=96, timeline_duration=72, source_start=0, source_duration=72)
    sub1 = SubtitleClip("ge-sub-1", "ge-s1", 0, 24, "黄金工程字幕一")
    sub2 = SubtitleClip("ge-sub-2", "ge-s1", 24, 48, "Subtitle two, with comma")

    main = TimelineSequence(
        timeline_id="ge-seq-main", name="Golden Main", fps=Rational(24, 1),
        width=1920, height=1080,
        video_tracks=(
            EditTrack("ge-v1", "V1", "video", 0, clips=(v1_clip, nsrc_clip)),
            EditTrack("ge-v2", "V2 Adj", "video", 1, clips=(adj_clip,)),
            EditTrack("ge-v3", "V3 Mixed", "video", 2, clips=(mixed_clip,)),
        ),
        audio_tracks=(
            EditTrack("ge-a1", "A1", "audio", 0, clips=(a1_clip,), volume=1.0),
            EditTrack("ge-a2", "A2", "audio", 1, clips=(a2_clip,), volume=1.0),
        ),
        subtitle_tracks=(
            EditTrack("ge-s1", "S1", "subtitle", 0, subtitle_clips=(sub1, sub2)),
        ),
    )

    # 嵌套：V1 的 solid 片段经生产事务收进子序列，父时间线原位换成嵌套片段。
    txn = build_nested_from_selection(main, ("ge-main-nsrc",), name="Golden Child")
    main = txn.new_timeline
    child = txn.child_timeline
    # 嵌套素材注册进工程资产表（虚拟 path=topos://sequence/<id>），供结构快照解析。
    project = replace(project, assets={**project.assets, txn.nested_asset.asset_id: txn.nested_asset})

    # ── AUX 序列（25fps，跨时间线复制验证用）───────────────────────
    aux_clip = ClipInstance(
        clip_id="ge-aux-v1", asset_id=grad24_asset.asset_id, track_id="ge-aux-v1t",
        timeline_start=0, timeline_duration=48, source_start=0, source_duration=48)
    aux = TimelineSequence(
        timeline_id="ge-seq-aux", name="Golden Aux", fps=Rational(25, 1),
        width=1920, height=1080,
        video_tracks=(EditTrack("ge-aux-v1t", "V1", "video", 0, clips=(aux_clip,)),),
    )

    # ── 生产命令：链接 / 关键帧 / 调色栈 ──────────────────────────
    stack: tuple = ()
    project, stack_id = project.create_color_stack(
        asset_id=grad24_asset.asset_id, name="Golden Look")
    commands = (
        SetClipLinkedCommand.create_link("ge-main-v1", "ge-v1", "ge-main-a1", "ge-a1"),
        AddKeyframeCommand.create("ge-v1", "ge-main-v1", "opacity", 0, 1.0),
        AddKeyframeCommand.create("ge-v1", "ge-main-v1", "opacity", 36, 0.4),
        AddKeyframeCommand.create("ge-v1", "ge-main-v1", "opacity", 71, 1.0),
        UpdateClipColorStackCommand(
            command_id="ge-cmd-colorstack", label="Bind Golden Look",
            track_id="ge-v1", clip_id="ge-main-v1",
            new_color_stack_id=stack_id, new_color_shared=True),
    )
    command_stack = CommandStack()
    for cmd in commands:
        main = command_stack.execute(cmd, main)
        stack = stack + (cmd,)

    # AUX 片段不参与 MAIN 的命令栈；序列集合挂回工程（含子序列引用）。
    def _ref(seq: TimelineSequence) -> TimelineSequenceRef:
        fps_value = seq.fps.to_float() if hasattr(seq.fps, "to_float") else float(seq.fps)
        return TimelineSequenceRef(
            timeline_id=seq.timeline_id, name=seq.name, fps=fps_value,
            width=seq.width, height=seq.height, sample_rate=seq.sample_rate)

    project = project.with_timeline_ref_added(_ref(child))
    project = project.with_timeline_ref_added(_ref(aux))

    info: dict[str, Any] = {
        "media": {
            "grad24": str(grad24_path), "grad_mixed": str(grad_mixed_path),
            "sine": str(sine_path), "inverted": str(inv_path),
        },
        "asset_ids": {
            "grad24": grad24_asset.asset_id, "grad_mixed": mixed_asset.asset_id,
            "sine": sine_asset.asset_id, "inverted": inv_asset.asset_id,
            "offline": offline_asset.asset_id, "solid": solid_id,
            "adjustment": adj_id,
        },
        "clip_ids": {
            "v1": "ge-main-v1", "nested": txn.nested_clip_id,
            "adjustment": "ge-main-adj", "mixed": "ge-main-v3",
            "a1": "ge-main-a1", "a2": "ge-main-a2",
            "sub1": "ge-sub-1", "sub2": "ge-sub-2", "aux": "ge-aux-v1",
        },
        "track_ids": ["ge-v1", "ge-v2", "ge-v3", "ge-a1", "ge-a2", "ge-s1", "ge-aux-v1t"],
        "stack_id": stack_id,
        "child_timeline_id": child.timeline_id,
    }
    return project, {"main": main, "child": child, "aux": aux}, info
