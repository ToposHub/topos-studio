"""B00 十分钟参考工程构建器（主计划 §6 第 2 步）。

DX/ADR/Foley/FX/MX 普通轨 + DX Bus/FX Bus/Return Bus + Master：
- pre/post Send、侧链（FX Bus 压缩器检源 MX）、volume 自动化、
  volume trim、VCA 组、非零源入点、采样淡化（样本级）。
- 音频长度按 48 kHz 样本表达；效果延迟由测试注入 DelayOnlyEffect（模型层
  不绑定具体效果实例身份，catalog 合同由 B23/E09 覆盖）。
- 所有 clip 引用合成 asset ID（"synth-<role>"），不依赖开发机素材路径；
  解码端由具体测试注入确定性 PCM。
"""
from __future__ import annotations

from fractions import Fraction

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Send,
    TimelineSequence,
    VcaGroup,
)
from src.shared.keyframe import Keyframe, KeyframeTrack

RATE = 48000
FPS = Fraction(24, 1)


def _clip(clip_id: str, asset_id: str, track_id: str, timeline_start: int,
          duration_samples: int, source_start: int) -> ClipInstance:
    """合成 clip：非零源入点 + 采样级淡化（fade_in/out_samples）。

    timeline 时长按 48kHz 样本直接表达（0.5s/块），source_duration 同宽。
    """
    return ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=timeline_start,
        timeline_duration=duration_samples,
        source_start=source_start,
        source_duration=duration_samples,
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
    total = int(round(RATE * total_seconds))
    block = RATE // 2  # 0.5s 交替块

    clips_dx = tuple(
        _clip(f"dx-{i}", "synth-dx", "dx", i * 2 * block, block, 240 + i * 7)
        for i in range(total // block)
    )
    clips_adr = tuple(
        _clip(f"adr-{i}", "synth-adr", "adr", i * 2 * block + block, block,
              120 + i * 11)
        for i in range(total // block - 1)
    )
    clips_foley = tuple(
        _clip(f"foley-{i}", "synth-foley", "foley", i * 2 * block, block,
              60 + i * 3)
        for i in range(0, total // block, 4)
    )
    clips_fx = tuple(
        _clip(f"fx-{i}", "synth-fx", "fx", i * 2 * block + block, block, 0)
        for i in range(total // block - 1)
    )
    clips_mx = tuple(
        _clip(f"mx-{i}", "synth-mx", "mx", i * 2 * block, block, 480 + i * 5)
        for i in range(total // block)
    )

    dx_track = EditTrack(
        "dx", "DX", "audio", 0, clips=clips_dx, output_bus_id="dx_bus",
        automation={"volume": _volume_curve([(0, 1.0), (RATE * 30, 0.8),
                                             (RATE * 60, 1.0)])},
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
        "fx", "FX", "audio", 3, clips=clips_fx,
        sends=(Send(send_id="fx-rev", target_bus_id="return_bus",
                    level=0.4, tap="pre_fader"),),
    )
    mx_track = EditTrack(
        "mx", "MX", "audio", 4, clips=clips_mx,
        automation={"volume": _volume_curve([(0, 0.9), (RATE * 45, 1.0)])},
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
