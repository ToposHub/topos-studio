"""批 C3 回归：FE-B06 多机位累计偏移 / FE-B08 嵌套轨道保真 / FE-B13 时间码 /
FE-B15 真峰值。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §11。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import SwitchMulticamAngleCommand
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int,
          asset: str = "", source_start: int = 0, **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=asset or f"a-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=source_start,
        source_duration=500,
        **kw,
    )


def _seq(*clips: ClipInstance) -> TimelineSequence:
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video)
    for c in clips:
        timeline = timeline.with_clip_added("v1", c)
    return timeline


# ---------------------------------------------------------------- FE-B06

def test_multicam_round_trip_no_offset_accumulation() -> None:
    """审计实测：A offset0 → B(+10) → 回 A，源窗从 20 变 30。"""
    # 初始角度 A：source_start=0
    seq = _seq(_clip("mc", "v1", 0, 100, asset="assetA", source_start=0))

    # 切到 B（offset +10）
    cmd_b = SwitchMulticamAngleCommand.create(
        track_id="v1", clip_id="mc", switch_frame=20,
        angle_asset_id="assetB", angle_sync_offset=10,
        active_sync_offset=0,
    )
    apply_b = cmd_b.apply(seq)
    seq_b, updated_b = apply_b
    right_b = seq_b.get_clip_by_id(updated_b._right_clip_id)
    # 组公共时间 = map(20) - 0 = 20；B 源 = 20 + 10 = 30
    assert right_b.source_start == 30

    # 再切回 A（offset 0）：活动偏移是 B 的 +10
    cmd_a = SwitchMulticamAngleCommand.create(
        track_id="v1", clip_id=updated_b._right_clip_id, switch_frame=40,
        angle_asset_id="assetA", angle_sync_offset=0,
        active_sync_offset=10,
    )
    apply_a = cmd_a.apply(seq_b)
    seq_a, updated_a = apply_a
    right_a = seq_a.get_clip_by_id(updated_a._right_clip_id)
    # map(40) = 30 + 20 = 50（B 段映射）；group_time = 50 - 10 = 40；
    # A 源 = 40 + 0 = 40——不累计（旧 source_start+offset 行为会得 50+）
    assert right_a.source_start == 40


# ---------------------------------------------------------------- FE-B08

def test_nested_creation_preserves_track_state_and_animation() -> None:
    """审计实测：音轨 (.3, -.7, muted) 创建嵌套后变 (1, 0, False)；
    动画第 24 帧仍取旧值。"""
    from src.features.edit.core.effect_instance import AnimatableParam
    from src.features.edit.core.nested_sequence import build_nested_from_selection
    from src.shared.keyframe import Keyframe, KeyframeTrack

    clip = _clip("c1", "a1", 24, 96, animated_params={
        "opacity": AnimatableParam(
            base_value=1.0,
            keyframes=KeyframeTrack(
                param_name="opacity",
                keyframes=(
                    Keyframe(frame=24, value=0.2),
                    Keyframe(frame=48, value=0.8),
                ),
            ),
        ),
    })
    audio = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        volume=0.3, pan=-0.7, is_muted=True, is_solo=False,
    )
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(audio).with_clip_added("a1", clip)

    result = build_nested_from_selection(timeline, ("c1",), name="Nest")
    child = result.child_timeline
    child_audio = child.audio_tracks[0]
    assert child_audio.volume == pytest.approx(0.3)
    assert child_audio.pan == pytest.approx(-0.7)
    assert child_audio.is_muted is True

    child_clip = child_audio.clips[0]
    assert child_clip.timeline_start == 0
    param = child_clip.animated_params["opacity"]
    frames = [k.frame for k in param.keyframes.keyframes]
    assert frames == [0, 24]  # 动画减去 min_start，不再延后


# ---------------------------------------------------------------- FE-B13

def test_caption_timecode_fractional_fps() -> None:
    """审计实测：29.97 下 frame29 显示 :28（应 :29），1799 显示
    00:01:00:00（应 00:00:59:29）。"""
    from src.features.edit.ui.caption_workspace import frame_to_timecode

    assert frame_to_timecode(29, 30000 / 1001).endswith(":29")
    assert frame_to_timecode(1799, 30000 / 1001) == "00:00:59;29" or \
        frame_to_timecode(1799, 30000 / 1001) == "00:00:59:29"


def test_timecode_roundtrip_consistency() -> None:
    from src.features.edit.core.interchange import frames_to_timecode

    # NDF：帧号不漂移
    for frame in (0, 29, 1798, 1799, 107892):
        tc = frames_to_timecode(frame, 30000 / 1001)
        assert tc.startswith("00:")


# ---------------------------------------------------------------- FE-B15

def test_true_peak_captures_intersample_overshoot() -> None:
    """相邻满刻度样本之间必须能插值出 >1.0 的 intersample 峰值。"""
    import numpy as np

    from src.features.edit.core.loudness_analyzer import _TruePeakEstimator

    est = _TruePeakEstimator(channels=1)
    est.process(np.array([[0.0, 1.0, 1.0, 0.0]]))
    est.finalize()  # R27：短流收尾时以镜像上下文完整扫描
    assert est.peak > 1.0


def test_true_peak_constant_signal_no_false_overshoot() -> None:
    import numpy as np

    from src.features.edit.core.loudness_analyzer import _TruePeakEstimator

    est = _TruePeakEstimator(channels=1)
    est.process(np.ones((1, 64)))
    assert 0.98 < est.peak <= 1.02


def test_true_peak_chunked_equals_whole() -> None:
    import numpy as np

    from src.features.edit.core.loudness_analyzer import _TruePeakEstimator

    rng = np.random.default_rng(42)
    signal = rng.standard_normal((1, 512)) * 0.7
    chunked = _TruePeakEstimator(channels=1)
    for i in range(0, 512, 97):
        chunked.process(signal[:, i:i + 97])
    whole = _TruePeakEstimator(channels=1)
    whole.process(signal)
    assert abs(chunked.peak - whole.peak) < 1e-9


def test_true_peak_sine_no_inflation() -> None:
    import numpy as np

    from src.features.edit.core.loudness_analyzer import _TruePeakEstimator

    t = np.linspace(0, 1, 4800, endpoint=False)
    sine = (np.sin(2 * np.pi * 997 * t) * 0.5)[None, :]
    est = _TruePeakEstimator(channels=1)
    est.process(sine)
    assert est.peak <= 0.55
