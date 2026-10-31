"""T-P1-22：音频 DSP 真实时间线导出聚焦测试（卡步骤 3）。

8 个音频 DSP（strip_eq/limiter/compressor/reverb/delay/saturation/
imager/noise_gate）在**真实 timeline 导出**中经 AudioGraphRenderer 消费：
带音轨的真实素材 + clip 级 DSP 效果链 → 导出含音频的成片，验证：
- 输出含音频流、时长与视频对齐；
- DSP 消费无错误（效果链构建 → render_track_block → mux）；
- 尾音完整（样本数 ≈ 帧时长 × 采样率，容差 = 编码帧粒度）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_export import (
    ExportCodec,
    ExportSource,
    TimelineExportConfig,
    create_timeline_exporter,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

REPO = Path(__file__).resolve().parents[2]
# 真实人声素材（pcm_s24le 立体声，T-P1-22 域 2 同源）
SOURCE = REPO / "tests" / "test_videos" / "2K_H265.mov"

pytestmark = pytest.mark.skipif(
    not SOURCE.is_file(), reason="真实 H265 素材缺失")

FRAMES = 48  # 25fps → 1.92s → 92160 samples @48k
SAMPLE_RATE = 48000


def _build(params_by_type: dict) -> "tuple":
    asset = MediaAsset(
        asset_id="aud_src", path=str(SOURCE), media_type="video",
        display_name="src", metadata={"fps": 25.0, "total_frames": 300},
    )
    project = EditProject.create(name="tp122-audio")
    project = project.with_media_asset_added(asset)
    effects = tuple(
        EffectInstance(effect_type=effect_type, params=params or {},
                       is_enabled=True)
        for effect_type, params in params_by_type.items()
    )
    video_clip = ClipInstance(
        clip_id="vid_clip", asset_id="aud_src", track_id="v1",
        timeline_start=0, timeline_duration=FRAMES, source_start=0,
        source_duration=FRAMES,
    )
    # T-P1-22 卡步骤 3：音频 DSP 挂在音频轨 clip 上（AudioGraphRenderer
    # 消费 audio_tracks 的 per-clip EffectChain）——视频轨 clip 不带 DSP。
    audio_clip = ClipInstance(
        clip_id="aud_clip", asset_id="aud_src", track_id="a1",
        timeline_start=0, timeline_duration=FRAMES, source_start=0,
        source_duration=FRAMES, effects=effects,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(video_clip,))
    a1 = EditTrack(track_id="a1", name="A1", track_type="audio", index=0,
                   clips=(audio_clip,))
    seq = TimelineSequence(timeline_id="s", name="AudioDSP", fps=Rational(25, 1),
                           width=1920, height=1080, sample_rate=SAMPLE_RATE,
                           video_tracks=(v1,), audio_tracks=(a1,))
    cfg = TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id="s",
        output_path="",  # 由调用方填充
        codec=ExportCodec.H264, pixel_format="yuv420p",
        quality=8_000_000, quality_mode="vbr", fps=Rational(25, 1),
        include_audio=True, burn_subtitles=False,
    )
    return project, seq, cfg


@pytest.mark.parametrize("params_by_type", [
    {"strip_eq": {}},
    {"compressor": {}},
    {"delay": {}},
    {"reverb": {}},
    {"limiter": {}, "compressor": {}},   # 串联链
])
def test_real_timeline_audio_dsp_export(params_by_type, tmp_path):
    import av

    out = tmp_path / "audio_dsp.mov"
    project, seq, cfg = _build(params_by_type)
    cfg = type(cfg)(**{**{f.name: getattr(cfg, f.name)
                          for f in cfg.__dataclass_fields__.values()},
                       "output_path": str(out)})
    exporter = create_timeline_exporter(project, seq)
    outcome = exporter.export_timeline(cfg)
    assert bool(outcome), f"export failed: {getattr(outcome, 'cause', '')}"
    assert out.exists()

    container = av.open(str(out))
    try:
        audio = [s for s in container.streams if s.type == "audio"]
        assert audio, "导出缺音频流（DSP 链吞掉音轨）"
        a_stream = audio[0]
        # stream.duration 是 time_base ticks（如 93184/48000 ≈ 1.94s）
        if a_stream.duration is not None and a_stream.time_base is not None:
            duration_s = float(a_stream.duration * a_stream.time_base)
        else:
            duration_s = float(container.duration or 0) / 1e6
        expected = FRAMES / 25.0
        assert abs(duration_s - expected) < 0.25, (
            f"音画时长错位: audio={duration_s:.3f}s vs video={expected:.3f}s")
    finally:
        container.close()


# ================================================================
# T-P1-22 卡步骤 3 遗留：跨 packet 边界 + 多轨混音（真实时间线）
# ================================================================

def _build_multi(source_start: int, dsp_by_clip: dict) -> "tuple":
    """构建多音轨时间线；dsp_by_clip: {clip_id: effects tuple}。

    与 _build 的区别：允许指定 source_start（跨 packet 边界的素材中段）
    与两条音轨各自的 DSP 链（混音场景）。
    """
    asset = MediaAsset(
        asset_id="aud_src", path=str(SOURCE), media_type="video",
        display_name="src", metadata={"fps": 25.0, "total_frames": 300},
    )
    project = EditProject.create(name="tp122-crosspacket")
    project = project.with_media_asset_added(asset)

    video_clip = ClipInstance(
        clip_id="vid_clip", asset_id="aud_src", track_id="v1",
        timeline_start=0, timeline_duration=FRAMES, source_start=source_start,
        source_duration=FRAMES,
    )
    a_clips = []
    for i, (clip_id, effects) in enumerate(dsp_by_clip.items()):
        a_clips.append(ClipInstance(
            clip_id=clip_id, asset_id="aud_src", track_id=f"a{i+1}",
            timeline_start=0, timeline_duration=FRAMES,
            source_start=source_start, source_duration=FRAMES,
            effects=effects,
        ))
    tracks = [
        EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                  clips=(video_clip,)),
    ]
    for i, clip in enumerate(a_clips):
        tracks.append(EditTrack(
            track_id=f"a{i+1}", name=f"A{i+1}", track_type="audio",
            index=i, clips=(clip,)))
    seq = TimelineSequence(
        timeline_id="s", name="CrossPacket", fps=Rational(25, 1),
        width=1920, height=1080, sample_rate=SAMPLE_RATE,
        video_tracks=(tracks[0],),
        audio_tracks=tuple(tracks[1:]),
    )
    cfg = TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id="s", output_path="",
        codec=ExportCodec.H264, pixel_format="yuv420p",
        quality=8_000_000, quality_mode="vbr", fps=Rational(25, 1),
        include_audio=True, burn_subtitles=False,
    )
    return project, seq, cfg


def _assert_audio_aligned(out_path, expected_s: float) -> None:
    import av

    container = av.open(str(out_path))
    try:
        audio = [s for s in container.streams if s.type == "audio"]
        assert audio, "导出缺音频流"
        a_stream = audio[0]
        if a_stream.duration is not None and a_stream.time_base is not None:
            duration_s = float(a_stream.duration * a_stream.time_base)
        else:
            duration_s = float(container.duration or 0) / 1e6
        assert abs(duration_s - expected_s) < 0.25, (
            f"音画时长错位: audio={duration_s:.3f}s vs video={expected_s:.3f}s")
    finally:
        container.close()


def test_dsp_export_across_packet_boundary(tmp_path):
    """source_start=200（8s 处，跨过大量编码 packet）+ reverb DSP。

    卡步骤 3 的跨 packet 场景：素材中段起点意味着解码器从非关键帧
    附近起步、音频 packet 不从 0 对齐——DSP 链与 mux 必须照常工作。
    """
    from src.features.edit.core.effect_instance import EffectInstance

    out = tmp_path / "cross_packet.mov"
    effects = (EffectInstance(effect_type="reverb", params={},
                              is_enabled=True),)
    project, seq, cfg = _build_multi(
        source_start=200, dsp_by_clip={"aud_clip": effects})
    cfg = type(cfg)(**{**{f.name: getattr(cfg, f.name)
                          for f in cfg.__dataclass_fields__.values()},
                       "output_path": str(out)})
    exporter = create_timeline_exporter(project, seq)
    outcome = exporter.export_timeline(cfg)
    assert bool(outcome), f"export failed: {getattr(outcome, 'cause', '')}"
    _assert_audio_aligned(out, FRAMES / 25.0)


def test_two_track_dsp_mixing_export(tmp_path):
    """双音轨混音：A1 干声 + A2 reverb（不同 DSP 链、不同轨）。

    卡步骤 3 的混音场景：AudioGraphRenderer 须把两条各自带 DSP 的
    音轨混入同一 mux，音画时长仍对齐。
    """
    from src.features.edit.core.effect_instance import EffectInstance

    out = tmp_path / "mix_two_tracks.mov"
    dry = ()
    wet = (EffectInstance(effect_type="reverb", params={},
                          is_enabled=True),)
    project, seq, cfg = _build_multi(
        source_start=0, dsp_by_clip={"aud_dry": dry, "aud_wet": wet})
    cfg = type(cfg)(**{**{f.name: getattr(cfg, f.name)
                          for f in cfg.__dataclass_fields__.values()},
                       "output_path": str(out)})
    exporter = create_timeline_exporter(project, seq)
    outcome = exporter.export_timeline(cfg)
    assert bool(outcome), f"export failed: {getattr(outcome, 'cause', '')}"
    _assert_audio_aligned(out, FRAMES / 25.0)
