"""B07 · Trim 烘入曲线（coalesce）与保边界简化回归（主计划 §13 步骤 6/8）。

- CoalesceAutomationTrimCommand：coalesce 前后 get_automation_value 逐帧
  相等（声音不变）；单次 undo 完整恢复；溢出拒绝（trim 保留）。
- simplify_samples：protected_frames（hold/触摸边界）不被 RDP 删去；
  volume dB 域误差界（≤0.1 dB）。
"""
from __future__ import annotations

import math

import pytest

from src.features.edit.core.automation_recorder import (
    CoalesceAutomationTrimCommand,
    _FaderSample,
    simplify_samples,
    simplify_volume_db,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


def _timeline_with_track(track: EditTrack) -> TimelineSequence:
    clip = ClipInstance(clip_id="c1", asset_id="a", track_id=track.track_id,
                        timeline_start=0, timeline_duration=48,
                        source_start=0, source_duration=48)
    track = track._rebuild(clips=(clip,)) if not track.clips else track
    return TimelineSequence(audio_tracks=(track,))


class TestCoalesceTrim:
    def test_curve_values_baked_and_trim_reset(self) -> None:
        track = EditTrack("t", "T", "audio", 0).with_automation_trim(
            "volume", 0.5)
        curve = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        curve.add_keyframe(Keyframe(frame=0, value=0.4))
        curve.add_keyframe(Keyframe(frame=48, value=1.2))
        track = track._rebuild(automation={"volume": curve},
                               automation_mode="read")
        timeline = _timeline_with_track(track)
        before = [track.get_automation_value("volume", f, 1.0)
                  for f in range(0, 60, 7)]
        cmd = CoalesceAutomationTrimCommand.create("t")
        new_timeline, applied = cmd.apply(timeline)
        new_track = new_timeline.get_track_by_id("t")
        assert new_track.automation_trim.get("volume") is None
        after = [new_track.get_automation_value("volume", f, 1.0)
                 for f in range(0, 60, 7)]
        # 声音不变：逐帧相等（oracle = 模型求值）
        for a, b in zip(before, after):
            assert math.isclose(a, b, rel_tol=1e-9)
        # 曲线值已乘 trim
        baked = new_track.automation["volume"]
        assert math.isclose(baked.keyframes[0].value, 0.2)
        assert math.isclose(baked.keyframes[1].value, 0.6)

    def test_no_curve_bakes_into_track_volume(self) -> None:
        track = (EditTrack("t", "T", "audio", 0, volume=0.8)
                 .with_automation_trim("volume", 0.5))
        timeline = _timeline_with_track(track)
        cmd = CoalesceAutomationTrimCommand.create("t")
        new_timeline, _applied = cmd.apply(timeline)
        new_track = new_timeline.get_track_by_id("t")
        assert math.isclose(new_track.volume, 0.4)
        assert new_track.automation_trim.get("volume") is None
        # 模型语义等价：default×trim == 新 default
        assert math.isclose(
            new_track.get_automation_value("volume", 0, new_track.volume),
            track.get_automation_value("volume", 0, track.volume),
            rel_tol=1e-9,
        )

    def test_overflow_rejected_and_trim_kept(self) -> None:
        track = EditTrack("t", "T", "audio", 0).with_automation_trim(
            "volume", 2.0)
        curve = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        curve.add_keyframe(Keyframe(frame=0, value=1.5))  # 1.5×2=3 > max 2
        track = track._rebuild(automation={"volume": curve})
        timeline = _timeline_with_track(track)
        cmd = CoalesceAutomationTrimCommand.create("t")
        with pytest.raises(ValueError, match="clamp|outside"):
            cmd.apply(timeline)
        # trim 保留（声音正确性优先）
        assert timeline.get_track_by_id("t").automation_trim.get("volume") == 2.0

    def test_undo_restores_curve_trim_and_volume(self) -> None:
        track = (EditTrack("t", "T", "audio", 0, volume=0.8)
                 .with_automation_trim("volume", 0.5))
        curve = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        curve.add_keyframe(Keyframe(frame=0, value=0.4))
        curve.add_keyframe(Keyframe(frame=48, value=1.2))
        track = track._rebuild(automation={"volume": curve})
        timeline = _timeline_with_track(track)
        cmd = CoalesceAutomationTrimCommand.create("t")
        new_timeline, applied = cmd.apply(timeline)
        restored = applied.undo(new_timeline)
        r_track = restored.get_track_by_id("t")
        assert r_track.automation_trim.get("volume") == 0.5
        assert math.isclose(r_track.volume, 0.8)
        assert math.isclose(r_track.automation["volume"].keyframes[1].value, 1.2)

    def test_non_volume_param_rejected(self) -> None:
        with pytest.raises(ValueError, match="volume"):
            CoalesceAutomationTrimCommand.create("t", param="pan")

    def test_no_trim_rejected(self) -> None:
        timeline = _timeline_with_track(EditTrack("t", "T", "audio", 0))
        cmd = CoalesceAutomationTrimCommand.create("t")
        with pytest.raises(ValueError, match="no volume trim"):
            cmd.apply(timeline)


class TestSimplifyBoundaries:
    def test_protected_frames_survive_rdp(self) -> None:
        # 阶跃（hold 类陡边）：0@0-99 → 1@100-199。RDP 线性近似会删掉 100，
        # protected_frames 强制保留边界。
        samples = [_FaderSample(f, 0.0) for f in range(0, 100, 10)]
        samples += [_FaderSample(f, 1.0) for f in range(100, 200, 10)]
        plain = simplify_samples(samples, threshold=0.05)
        assert 100 in [p.frame for p in plain]  # 陡边端点本身就是拐点
        # 更严苛：斜坡噪声内埋一个受保护边界
        noisy = [_FaderSample(f, 0.5 + 0.004 * ((f % 3) - 1))
                 for f in range(0, 300, 3)]
        noisy.append(_FaderSample(150, 0.9))  # 触摸瞬跳（受保护）
        noisy.append(_FaderSample(153, 0.5))
        out = simplify_samples(noisy, threshold=0.005,
                               protected_frames={150, 153})
        assert 150 in [p.frame for p in out]
        assert 153 in [p.frame for p in out]

    def test_volume_db_domain_error_bound(self) -> None:
        """volume 简化：线性重放的 dB 误差逐样本 ≤0.1 dB（§13 步骤 8）。"""
        # 指数衰减曲线（线性域 DP 的线性误差界在 dB 域不成立）
        frames = list(range(0, 240, 2))
        samples = [_FaderSample(f, 10.0 ** (-1.2 * f / 240.0)) for f in frames]
        out = simplify_volume_db(samples, max_error_db=0.1)
        assert len(out) < len(samples)
        # oracle：与生产同方式重放（关键帧线性插值），逐样本 dB 误差
        for f, v in zip(frames, [s.value for s in samples]):
            lo = max((p for p in out if p.frame <= f), key=lambda p: p.frame)
            hi = min((p for p in out if p.frame >= f), key=lambda p: p.frame)
            if hi.frame == lo.frame:
                replay = lo.value
            else:
                t = (f - lo.frame) / (hi.frame - lo.frame)
                replay = lo.value + t * (hi.value - lo.value)
            err_db = abs(20.0 * math.log10(max(replay, 1e-6))
                         - 20.0 * math.log10(max(v, 1e-6)))
            assert err_db <= 0.1 + 1e-6, (f, err_db)

    def test_volume_db_protected_frames_kept(self) -> None:
        frames = list(range(0, 120, 2))
        samples = [_FaderSample(f, 0.5) for f in frames]
        samples.append(_FaderSample(60, 0.9))
        samples.append(_FaderSample(62, 0.5))
        out = simplify_volume_db(samples, max_error_db=0.1,
                                 protected_frames={60, 62})
        kept = {p.frame for p in out}
        assert 60 in kept and 62 in kept
