"""
Benchmark Projects (P0-06 工程矩阵)
确定性时间线基准工程构建器（性能总计划 §6 P0-06 / B-01..B-10 配套）。

## 职责

- 无 GUI、确定性地构建基准工程（TimelineSequence）：同参数必产出同结构。
- 覆盖工程矩阵最低集：单轨、双轨转场、四轨+字幕、重度 FX、长项目、
  海量媒体池。
- clip_id/asset_id/track_id 用 uuid5（seed 派生），保证跨机器/跨提交可比。
- 媒体引用 tests/test_videos 真实视频（P0-06 冻结矩阵）；结构型基准
  不要求文件存在，解码型基准由 runner 负责存在性校验。

## 用法

```python
from tests.benchmark.benchmark_projects import build_benchmark_project

seq = build_benchmark_project("E-02_dual_track_transitions")
assert len(seq.video_tracks) == 2
```

CLI：
    .venv/bin/python -m tests.benchmark.benchmark_projects --list
    .venv/bin/python -m tests.benchmark.benchmark_projects --scenario E-01
"""

from __future__ import annotations

import argparse
import json
import uuid

from pathlib import Path
from typing import Any, Dict, List, Optional

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineSequence,
    TransitionInstance,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: B-01..B-06 场景引用的真实媒体（相对仓库根；由 P0-06 冻结清单背书）。
BENCHMARK_MEDIA: Dict[str, str] = {
    "h264_2k": "tests/test_videos/2K_h264.mp4",
    "h265_4k": "tests/test_videos/4K_H265.mov",
    "prores_2k_hq": "tests/test_videos/2K_prores422HQ.mov",
    "prores_4k_hq": "tests/test_videos/4K_prores422HQ.mov",
    "prores_4k_lt": "tests/test_videos/4K_prores422LT.mov",
    "dnxhr_4k": "tests/test_videos/4K_DNxHR HQX 10bit.mov",
    "topos_4k": "tests/test_videos/4K_topos_422_HQ.mov",
    "topos_4k_proxy": "tests/test_videos/4K_topos_422_Proxy.mov",
    "topos_2k": "tests/test_videos/2K_topos_422_HQ.mov",
}

_UUID_SEED_NS = uuid.UUID("9a1c2b3d-0000-4000-8000-544f504f5331")  # "TOPOS1"


def _det_uuid(kind: str, key: str) -> str:
    """seed 派生的确定性 uuid（跨机器/跨提交稳定）。"""
    return str(uuid.uuid5(_UUID_SEED_NS, f"{kind}:{key}"))


def _clip(
    key: str,
    *,
    asset_id: str,
    track_id: str,
    start: int,
    duration: int,
    source_duration: Optional[int] = None,
    effects: tuple = (),
    opacity: float = 1.0,
) -> ClipInstance:
    return ClipInstance(
        clip_id=_det_uuid("clip", key),
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=source_duration or duration,
        effects=effects,
        opacity=opacity,
    )


def _video_track(
    key: str,
    index: int,
    clips: List[ClipInstance],
    *,
    transitions: tuple = (),
) -> EditTrack:
    return EditTrack(
        track_id=_det_uuid("track", key),
        name=key,
        track_type="video",
        index=index,
        clips=tuple(clips),
        transitions=transitions,
    )


def _asset_ids(media_keys: List[str]) -> Dict[str, str]:
    return {k: _det_uuid("asset", k) for k in media_keys}


# ---------------------------------------------------------------------------
# 场景构建器（工程矩阵最低集）
# ---------------------------------------------------------------------------


def _build_single_track(fps: int) -> TimelineSequence:
    """E-01：单轨多 clip（B-01/B-03/B-04 解码基准的结构）。"""
    assets = _asset_ids(["h264_2k"])
    clips = [
        _clip(f"e01-c{i}", asset_id=assets["h264_2k"],
              track_id=_det_uuid("track", "e01-v0"),
              start=i * 150, duration=150)
        for i in range(4)
    ]
    video = _video_track("e01-v0", 0, clips)
    return TimelineSequence(
        timeline_id=_det_uuid("seq", "e01"),
        name="E01_single_track",
        fps=fps,
        width=1920,
        height=1080,
        video_tracks=(video,),
    )


def _build_dual_track_transitions(fps: int) -> TimelineSequence:
    """E-02：双轨 + GPU 转场（B-05：pass GPU time / copy bytes）。"""
    assets = _asset_ids(["prores_2k_hq", "h264_2k"])
    t0 = _det_uuid("track", "e02-v0")
    t1 = _det_uuid("track", "e02-v1")
    # 轨内 clip 相邻不重叠（EditTrack 校验禁止重叠）；转场窗口由
    # center_frame ± duration/2 决定，窗口内左右 clip 按 in_transition 采样。
    clips_v0 = [
        _clip("e02-a0", asset_id=assets["prores_2k_hq"], track_id=t0,
              start=0, duration=300),
        _clip("e02-a1", asset_id=assets["prores_2k_hq"], track_id=t0,
              start=300, duration=300),
        _clip("e02-a2", asset_id=assets["prores_2k_hq"], track_id=t0,
              start=600, duration=300),
    ]
    clips_v1 = [
        _clip("e02-b0", asset_id=assets["h264_2k"], track_id=t1,
              start=100, duration=500, opacity=0.85),
    ]
    transition = TransitionInstance(
        transition_id=_det_uuid("trans", "e02-t0"),
        track_id=t0,
        transition_type="cross_dissolve",
        left_clip_id=clips_v0[0].clip_id,
        right_clip_id=clips_v0[1].clip_id,
        center_frame=300,
        duration=50,
    )
    v0 = _video_track("e02-v0", 0, clips_v0, transitions=(transition,))
    v1 = _video_track("e02-v1", 1, clips_v1)
    return TimelineSequence(
        timeline_id=_det_uuid("seq", "e02"),
        name="E02_dual_track_transitions",
        fps=fps,
        width=1920,
        height=1080,
        video_tracks=(v0, v1),
    )


def _build_four_track_titles(fps: int) -> TimelineSequence:
    """E-03：四轨 + 字幕（B-06：render plan 构建/缓存/池峰值）。"""
    assets = _asset_ids(["h264_2k", "prores_2k_hq", "topos_2k"])
    tracks: List[EditTrack] = []
    media_cycle = ["h264_2k", "prores_2k_hq", "topos_2k", "h264_2k"]
    for ti in range(4):
        t_key = f"e03-v{ti}"
        t_id = _det_uuid("track", t_key)
        clips = [
            _clip(
                f"e03-t{ti}-c{i}",
                asset_id=assets[media_cycle[ti]],
                track_id=t_id,
                start=i * 200,
                duration=200,
                opacity=1.0 if ti == 0 else 0.9,
            )
            for i in range(3)
        ]
        tracks.append(_video_track(t_key, ti, clips))
    sub_track = EditTrack(
        track_id=_det_uuid("track", "e03-sub"),
        name="e03-subtitles",
        track_type="subtitle",
        index=0,
        subtitle_clips=tuple(
            SubtitleClip(
                clip_id=_det_uuid("sub", f"e03-s{i}"),
                track_id=_det_uuid("track", "e03-sub"),
                timeline_start=100 + i * 200,
                timeline_duration=100,
                text=f"Benchmark caption line {i}",
            )
            for i in range(4)
        ),
    )
    return TimelineSequence(
        timeline_id=_det_uuid("seq", "e03"),
        name="E03_four_track_titles",
        fps=fps,
        width=1920,
        height=1080,
        video_tracks=tuple(tracks),
        subtitle_tracks=(sub_track,),
    )


def _build_heavy_fx(fps: int) -> TimelineSequence:
    """E-04：重度 FX（B-07：模糊/降噪类邻域+时域效果的自适应质量）。"""
    assets = _asset_ids(["prores_2k_hq"])
    t_id = _det_uuid("track", "e04-v0")
    heavy_effects = (
        EffectInstance(
            effect_id=_det_uuid("fx", "e04-blur"),
            effect_type="gaussian_blur",
            params={"radius": 12.0, "intensity": 80.0},
            order=0,
        ),
        EffectInstance(
            effect_id=_det_uuid("fx", "e04-denoise"),
            effect_type="temporal_denoise",
            params={"strength": 0.5},
            order=1,
        ),
        EffectInstance(
            effect_id=_det_uuid("fx", "e04-sharpen"),
            effect_type="sharpen",
            params={"amount": 0.6},
            order=2,
        ),
    )
    clips = [
        _clip(f"e04-c{i}", asset_id=assets["prores_2k_hq"], track_id=t_id,
              start=i * 250, duration=250,
              effects=heavy_effects if i % 2 == 0 else ())
        for i in range(4)
    ]
    return TimelineSequence(
        timeline_id=_det_uuid("seq", "e04"),
        name="E04_heavy_fx",
        fps=fps,
        width=1920,
        height=1080,
        video_tracks=(_video_track("e04-v0", 0, clips),),
    )


def _build_long_project(fps: int) -> TimelineSequence:
    """E-05：长项目（B-08：30 分钟持续播放/内存泄漏/A-V drift）。"""
    assets = _asset_ids(["h264_2k"])
    t_id = _det_uuid("track", "e05-v0")
    clip_len = 10 * fps  # 10s per clip
    n_clips = 180  # 30min
    clips = [
        _clip(f"e05-c{i}", asset_id=assets["h264_2k"], track_id=t_id,
              start=i * clip_len, duration=clip_len)
        for i in range(n_clips)
    ]
    return TimelineSequence(
        timeline_id=_det_uuid("seq", "e05"),
        name="E05_long_project_30min",
        fps=fps,
        width=1920,
        height=1080,
        video_tracks=(_video_track("e05-v0", 0, clips),),
    )


def build_media_pool_descriptor(size: int = 5000) -> Dict[str, Any]:
    """E-06：海量媒体池描述（B-10：5,000 素材首屏/滚动/内存）。

    返回纯 JSON 描述（asset_id、media_key、file 相对路径），不构造
    5,000 个 widget；UI 虚拟化基准由 runner 消费此描述。
    """
    media_keys = list(BENCHMARK_MEDIA.keys())
    entries = []
    for i in range(size):
        key = media_keys[i % len(media_keys)]
        entries.append({
            "asset_id": _det_uuid("pool_asset", f"e06-a{i}"),
            "name": f"pool_asset_{i:05d}",
            "media_key": key,
            "file": BENCHMARK_MEDIA[key],
        })
    return {"scenario": "E-06_mass_media_pool", "size": size, "assets": entries}


#: 场景注册表（新增场景必须同时登记于此）。
PROJECT_BUILDERS = {
    "E-01_single_track": _build_single_track,
    "E-02_dual_track_transitions": _build_dual_track_transitions,
    "E-03_four_track_titles": _build_four_track_titles,
    "E-04_heavy_fx": _build_heavy_fx,
    "E-05_long_project_30min": _build_long_project,
}


def resolve_scenario(scenario: str) -> str:
    """场景名解析：支持全名（E-01_single_track）与前缀（E-01）。

    Raises:
        ValueError: 未知或歧义（多前缀命中）的场景名，附可用列表。
    """
    if scenario in PROJECT_BUILDERS:
        return scenario
    matches = [k for k in PROJECT_BUILDERS if k.startswith(scenario)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"ambiguous scenario {scenario!r}: {matches}"
        )
    raise ValueError(
        f"unknown scenario {scenario!r}; available: {sorted(PROJECT_BUILDERS)}"
    )


def build_benchmark_project(
    scenario: str,
    *,
    fps: int = 25,
) -> TimelineSequence:
    """按场景名构建确定性基准工程（支持前缀匹配，如 "E-01"）。

    Raises:
        ValueError: 未知或歧义的场景名（附可用列表）。
    """
    builder = PROJECT_BUILDERS[resolve_scenario(scenario)]
    return builder(fps)


def project_media_refs(sequence: TimelineSequence) -> List[str]:
    """收集序列引用的媒体文件相对路径（runner 做存在性校验用）。"""
    assets = set()
    for track in sequence.video_tracks:
        for clip in track.clips:
            if clip.asset_id:
                assets.add(clip.asset_id)
    # asset_id 是确定性 uuid；反查 media key
    key_by_asset = {_det_uuid("asset", k): k for k in BENCHMARK_MEDIA}
    return [
        BENCHMARK_MEDIA[key_by_asset[a]]
        for a in sorted(assets)
        if a in key_by_asset
    ]


def describe_project(sequence: TimelineSequence) -> Dict[str, Any]:
    """结构摘要（日志/基准报告用；不序列化全部 clip）。"""
    return {
        "timeline_id": sequence.timeline_id,
        "name": sequence.name,
        "fps": str(sequence.fps),
        "resolution": [sequence.width, sequence.height],
        "video_tracks": len(sequence.video_tracks),
        "subtitle_tracks": len(sequence.subtitle_tracks),
        "clip_count": sum(len(t.clips) for t in sequence.video_tracks),
        "transition_count": sum(len(t.transitions) for t in sequence.video_tracks),
        "subtitle_count": sum(len(t.subtitle_clips) for t in sequence.subtitle_tracks),
        "effect_count": sum(
            len(c.effects)
            for t in sequence.video_tracks
            for c in t.clips
        ),
        "duration_frames": max(
            (c.timeline_start + c.timeline_duration
             for t in sequence.video_tracks for c in t.clips),
            default=0,
        ),
        "media_refs": project_media_refs(sequence),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", help="构建并打印该场景的结构摘要（支持前缀，如 E-01）")
    parser.add_argument("--list", action="store_true", help="列出全部场景")
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    if args.list:
        for name in PROJECT_BUILDERS:
            print(name)
        return 0
    if args.scenario:
        seq = build_benchmark_project(args.scenario, fps=args.fps)
        desc = describe_project(seq)
        print(json.dumps(desc, indent=2, ensure_ascii=False)
              if args.json else desc)
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
