"""B00 十分钟参考工程构建器（主计划 §6 第 2 步）。

DX/ADR/Foley/FX/MX 普通轨 + DX Bus/FX Bus/Return Bus + Master：
- pre/post Send、侧链（FX Bus 压缩器检源 MX）、volume 自动化、
  volume trim、VCA 组、非零源入点、采样淡化（样本级）。
- 构建输入使用48 kHz样本，ClipInstance帧字段经24fps精确换算；
  效果延迟由测试注入 DelayOnlyEffect（模型层
  不绑定具体效果实例身份，catalog 合同由 B23/E09 覆盖）。
- 所有 clip 引用合成 asset ID（"synth-<role>"），不依赖开发机素材路径；
  解码端由具体测试注入确定性 PCM。
"""
from __future__ import annotations

from fractions import Fraction

from src.shared.media.metadata import Rational

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Send,
    TimelineSequence,
    VcaGroup,
)
from src.shared.keyframe import Keyframe, KeyframeTrack

RATE = 48000
FPS = Rational(24, 1)


def _clip(clip_id: str, asset_id: str, track_id: str, timeline_start: int,
          duration_samples: int, source_start: int) -> ClipInstance:
    """合成 clip：非零源入点 + 采样级淡化（fade_in/out_samples）。

    Inputs are 48k reference samples; frame fields remain frames. The clip
    tail must fall on an existing frame boundary because the model has no
    sample-domain end field. Source offsets retain the nonzero subframe part.
    """
    samples_per_frame = RATE // FPS.num
    start_frame, timeline_offset = divmod(timeline_start, samples_per_frame)
    end_frame, remainder = divmod(timeline_start + duration_samples, samples_per_frame)
    if remainder:
        raise ValueError("reference clip tail must be frame aligned")
    source_frame, source_offset = divmod(source_start, samples_per_frame)
    source_duration = (source_offset + duration_samples + samples_per_frame - 1) // samples_per_frame
    return ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=start_frame,
        timeline_duration=end_frame - start_frame,
        audio_sample_offset=timeline_offset,
        source_start=source_frame,
        source_sample_offset=source_offset,
        source_duration=source_duration,
        fade_in_samples=48,      # 1ms @48k
        fade_out_samples=48,
    )


def _volume_curve(points: list[tuple[int, float]]) -> KeyframeTrack:
    curve = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
    for frame, value in points:
        curve.add_keyframe(Keyframe(frame=float(frame), value=float(value)))
    return curve


def build_reference_sequence(total_seconds: float = 600.0) -> TimelineSequence:
    """构建参考工程时间线模型（默认 10 分钟）。

    拓扑：
        DX   → DXBus → Master
        ADR  → DXBus（send post）
        Foley→ FXBus ← （FX 轨 send pre）
        FX   → FXBus → Master（FXBus 压缩器检源 MX 的生产语义在模型层
               由 stem_group/effect_chain 表达；DSP 侧链合同由 B23/E02 接管）
        MX   → Master
        Return（混响返回）→ Master，FX send 一路供长尾效果
    """
    duration = Fraction(str(total_seconds))
    total_frames = duration * FPS.num / FPS.den
    if duration <= 0 or total_frames.denominator != 1:
        raise ValueError("reference duration must be positive and frame aligned")
    total = int(duration * RATE)
    block = RATE // 2  # 0.5s alternating blocks, exactly inside the requested range
    even_starts = tuple(range(0, total, 2 * block))
    odd_starts = tuple(range(block, total, 2 * block))

    clips_dx = tuple(
        _clip(f"dx-{i}", "synth-dx", "dx", start, min(block, total - start), 240 + i * 7)
        for i, start in enumerate(even_starts)
    )
    clips_adr = tuple(
        _clip(f"adr-{i}", "synth-adr", "adr", start, min(block, total - start), 120 + i * 11)
        for i, start in enumerate(odd_starts)
    )
    clips_foley = tuple(
        _clip(f"foley-{i}", "synth-foley", "foley", start, min(block, total - start), 60 + i * 3)
        for i, start in enumerate(even_starts) if i % 4 == 0
    )
    clips_fx = tuple(
        _clip(f"fx-{i}", "synth-fx", "fx", start, min(block, total - start), 0)
        for i, start in enumerate(odd_starts)
    )
    clips_mx = tuple(
        _clip(f"mx-{i}", "synth-mx", "mx", start, min(block, total - start), 480 + i * 5)
        for i, start in enumerate(range(0, total, block))
    )

    dx_track = EditTrack(
        "dx", "DX", "audio", 0, clips=clips_dx, output_bus_id="dx_bus",
        automation={"volume": _volume_curve([(0, 1.0), (min(24 * 30, int(total_frames)), 0.8),
                                             (min(24 * 60, int(total_frames)), 1.0)])},
        automation_mode="read",
    ).with_automation_trim("volume", 0.9)
    adr_track = EditTrack(
        "adr", "ADR", "audio", 1, clips=clips_adr, output_bus_id="dx_bus",
        sends=(Send(send_id="adr-rev", target_bus_id="return_bus",
                    level=0.25, tap="post_fader"),),
    )
    foley_track = EditTrack(
        "foley", "Foley", "audio", 2, clips=clips_foley,
        output_bus_id="fx_bus",
    )
    fx_track = EditTrack(
        "fx", "FX", "audio", 3, clips=clips_fx, output_bus_id="fx_bus",
        sends=(Send(send_id="fx-rev", target_bus_id="return_bus",
                    level=0.4, tap="pre_fader"),),
    )
    mx_track = EditTrack(
        "mx", "MX", "audio", 4, clips=clips_mx,
        automation={"volume": _volume_curve([(0, 0.9), (min(24 * 45, int(total_frames)), 1.0)])},
        automation_mode="read",
    )
    dx_bus = EditTrack("dx_bus", "DX Bus", "audio", 5, is_bus=True)
    fx_bus = EditTrack(
        "fx_bus", "FX Bus", "audio", 6, is_bus=True,
        sends=(Send(send_id="fxbus-rev", target_bus_id="return_bus",
                    level=0.15, tap="post_fader"),),
    )
    return_bus = EditTrack("return_bus", "Return Bus", "audio", 7, is_bus=True)

    return TimelineSequence(
        audio_tracks=(dx_track, adr_track, foley_track, fx_track, mx_track,
                      dx_bus, fx_bus, return_bus),
        vcas=(VcaGroup(vca_id="vca-dx", name="Dialogue",
                       member_track_ids=("dx", "adr"), gain_db=-2.0),),
        fps=FPS,
    )
