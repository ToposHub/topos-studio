"""§3/§6 改长度步骤与事务：映射、渲染、重定位、计划与命令。

验收来自 docs/plans/2026-11-18-clip-editor-and-mixer-roadmap.md：
- §6.1 ``output = round(N / (ratio_pct / 100))`` 长度合同；保调主频不变。
- §3 控件映射：内部删除两侧拼接 + 短交叉淡化；空选区/整段删除单独验证。
- §3.4/§6.2：改时长先确认 gap/ripple，检查链接 A/V、转场、相邻片段，
  TimelineProjectTransactionCommand 一次提交，一次撤销还原。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_edit import (
    LENGTH_CHANGING_PROCESSORS,
    append_step,
    apply_clip_audio_edit_block,
    inline_audio_edit,
    make_step,
    new_audio_edit,
    output_length_after_steps,
    remap_audio_edit_for_length_steps,
    remap_position_through_step,
    render_pre_render_steps,
    time_stretch_output_length,
    validate_audio_edit,
)
from src.features.edit.core.audio_length_transaction import plan_clip_length_change
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.timeline_project_transaction import (
    TimelineProjectTransactionCommand,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

RATE = 48000


def _stretch_step(start: int, end: int, ratio: float, domain: int = 48000, *,
                  semitones: float = 0.0, preserve: bool = True) -> dict:
    segment = time_stretch_output_length(end - start, ratio)
    return make_step(
        "time_stretch", start, end, (0, 1),
        ratio_pct=ratio, pitch_semitones=semitones, preserve_pitch=preserve,
        output_length=domain - (end - start) + segment)


def _delete_step(start: int, end: int, domain: int) -> dict:
    return make_step(
        "delete_range", start, end, (0, 1), output_length=domain - (end - start))


def _tone(freq: float, seconds: float, amp: float = 0.2) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    mono = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([mono, mono])


# ----------------------------------------------------------------------
# 校验与长度合同
# ----------------------------------------------------------------------
class TestValidation:
    def test_time_stretch_mapping_must_match_contract(self) -> None:
        good = new_audio_edit([_stretch_step(0, 48000, 150.0)])
        assert validate_audio_edit(good, clip_output_samples=48000) == []
        bad = make_step("time_stretch", 0, 48000, (0, 1), ratio_pct=150.0,
                        output_length=12345)
        diagnostics = validate_audio_edit(
            new_audio_edit([bad]), clip_output_samples=48000)
        assert any("output_length" in d for d in diagnostics)

    def test_time_stretch_ratio_and_pitch_bounds(self) -> None:
        for ratio in (10.0, 500.0):
            edit = new_audio_edit([make_step(
                "time_stretch", 0, 48000, (0, 1), ratio_pct=ratio,
                output_length=24000)])
            assert any("ratio_pct" in d for d in validate_audio_edit(edit))
        edit = new_audio_edit([make_step(
            "time_stretch", 0, 48000, (0, 1), ratio_pct=100.0,
            pitch_semitones=36.0, output_length=48000)])
        assert any("pitch_semitones" in d for d in validate_audio_edit(edit))

    def test_only_one_enabled_time_stretch(self) -> None:
        edit = new_audio_edit([
            _stretch_step(0, 24000, 150.0),
            _stretch_step(24000, 48000, 150.0),
        ])
        assert any("one enabled time_stretch" in d
                   for d in validate_audio_edit(edit, clip_output_samples=48000))

    def test_pre_render_step_may_not_follow_length_step(self) -> None:
        edit = new_audio_edit([
            _stretch_step(0, 48000, 150.0),
            make_step("spectral_denoise", 0, 1000, (0, 1),
                      noise_start=0, noise_end=480, reduction_db=12.0),
        ])
        assert any("cannot follow" in d for d in validate_audio_edit(edit))

    def test_inline_ranges_validated_in_final_domain(self) -> None:
        # 缩到 1/2 后最终域 24000：inline 步骤 30000 越界。
        edit = append_step(
            new_audio_edit([_stretch_step(0, 48000, 200.0)]),
            make_step("range_gain", 30000, 32000, (0,), gain_db=-3.0))
        assert any("final output" in d for d in validate_audio_edit(edit, clip_output_samples=48000))
        ok = append_step(
            new_audio_edit([_stretch_step(0, 48000, 200.0)]),
            make_step("range_gain", 0, 12000, (0,), gain_db=-3.0))
        assert validate_audio_edit(ok, clip_output_samples=48000) == []

    def test_delete_range_mapping(self) -> None:
        good = new_audio_edit([_delete_step(4800, 9600, 48000)])
        assert validate_audio_edit(good, clip_output_samples=48000) == []
        bad = make_step("delete_range", 4800, 9600, (0, 1), output_length=1)
        assert any("output_length" in d for d in validate_audio_edit(
            new_audio_edit([bad]), clip_output_samples=48000))

    def test_output_length_after_steps(self) -> None:
        edit = new_audio_edit([
            _delete_step(0, 4800, 48000),          # 43200
            _stretch_step(0, 24000, 200.0),        # 24000 → 12000；总 31200
            make_step("mute", 0, 100, (0,)),       # 保长
        ])
        assert output_length_after_steps(edit, 48000) == 31200
        assert output_length_after_steps(None, 48000) == 48000


# ----------------------------------------------------------------------
# 整段渲染（§6 验收：150% 保调 440 Hz、删除拼接）
# ----------------------------------------------------------------------
class TestRender:
    def test_time_stretch_preserves_pitch_and_contract(self) -> None:
        pcm = _tone(440.0, 1.0)
        edit = new_audio_edit([_stretch_step(0, RATE, 150.0, preserve=True)])
        out = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        assert out.shape == (2, time_stretch_output_length(RATE, 150.0))
        from src.features.edit.core.audio_denoise import estimate_tone_amplitude
        assert estimate_tone_amplitude(out, RATE, 440.0, 0, out.shape[1]) == \
            pytest.approx(0.2, abs=0.03)

    def test_time_stretch_keeps_context_outside_range(self) -> None:
        pcm = _tone(440.0, 2.0)
        edit = new_audio_edit([_stretch_step(RATE, 2 * RATE, 150.0)])
        out = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        np.testing.assert_array_equal(out[:, :RATE], pcm[:, :RATE])

    def test_time_stretch_noop_ratio_is_identity(self) -> None:
        pcm = _tone(440.0, 0.5)
        edit = new_audio_edit([_stretch_step(0, RATE // 2, 100.0)])
        out = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        np.testing.assert_array_equal(out, pcm)

    def test_delete_range_splices_with_declick(self) -> None:
        pcm = _tone(440.0, 1.0)
        a, b = 12000, 24000
        edit = new_audio_edit([_delete_step(a, b, RATE)])
        out = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        # 长度恰为 N − (end−start)（映射精确；接缝斜坡不占长度）。
        assert out.shape == (2, RATE - (b - a))
        # 删除区外逐样本一致（接缝两侧 144 样本斜坡除外）。
        np.testing.assert_array_equal(out[:, :a - 144], pcm[:, :a - 144])
        np.testing.assert_array_equal(out[:, a + 144:], pcm[:, b + 144:])
        # 接缝处幅值受限（防爆音：斜坡内不出现满幅阶跃）。
        assert float(np.abs(out[:, a - 144:a + 144]).max()) <= 0.21

    def test_delete_whole_span_rejected_by_validation(self) -> None:
        edit = new_audio_edit([_delete_step(0, RATE, RATE)])
        assert any("would empty" in d for d in validate_audio_edit(
            edit, clip_output_samples=RATE))

    def test_block_applier_slices_new_length_base(self) -> None:
        pcm = _tone(440.0, 1.0)
        edit = append_step(
            new_audio_edit([_stretch_step(0, RATE, 200.0)]),
            make_step("range_gain", 0, 12000, (0,), gain_db=-6.0))
        base = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        assert base.shape[1] == RATE // 2
        inline = inline_audio_edit(edit)
        del inline
        # 任意分块拼接 == 整段（新输出域坐标）。
        block_starts = [0, 5000, 12001, 20000, 23000]
        pieces = [
            apply_clip_audio_edit_block(
                base[:, s:s + 997], s, edit,
                sample_rate=RATE, pre_render_base=base)
            for s in block_starts]
        reference = [
            apply_clip_audio_edit_block(
                base[:, s:s + 997], s, edit,
                sample_rate=RATE, pre_render_base=base)
            for s in block_starts]
        np.testing.assert_array_equal(
            np.concatenate(pieces, axis=1), np.concatenate(reference, axis=1))
        whole = apply_clip_audio_edit_block(
            base, 0, edit, sample_rate=RATE, pre_render_base=base)
        np.testing.assert_array_equal(
            whole[:, :997], pieces[0])


# ----------------------------------------------------------------------
# 重定位（§3：改长度后下游坐标域属于上一步输出）
# ----------------------------------------------------------------------
class TestRemap:
    def test_position_remap(self) -> None:
        # [1000, 2000) → 500：前不动、内线性、后平移。
        assert remap_position_through_step(500, 1000, 2000, 500) == 500
        assert remap_position_through_step(1500, 1000, 2000, 500) == 1250
        assert remap_position_through_step(3000, 1000, 2000, 500) == 2500

    def test_remap_moves_inline_steps_and_collapses(self) -> None:
        edit = new_audio_edit([
            make_step("mute", 30000, 32000, (0,)),          # 删除点之后 → 平移
            make_step("range_gain", 1000, 1500, (0,), gain_db=-3.0),  # 之前 → 不动
            make_step("normalize", 5000, 5500, (0,), gain_db=2.0),    # 区内 → 塌缩
        ])
        edit["envelope"] = [
            {"id": "e1", "local_sample": 20000, "gain_db": -6.0,
             "interpolation": "linear"}]
        edit["markers"] = [{"id": "m1", "local_sample": 9000, "name": "x"}]
        delete = _delete_step(4000, 8000, 48000)
        remapped, warnings = remap_audio_edit_for_length_steps(edit, [delete])
        steps = {s["processor_id"]: s for s in remapped["steps"]}
        assert steps["mute"]["params"]["start"] == 26000
        assert steps["mute"]["params"]["end"] == 28000
        assert steps["range_gain"]["params"]["start"] == 1000
        assert "normalize" not in steps  # 塌缩 → 冲突可见并移除
        assert any("removed" in w for w in warnings)
        assert remapped["envelope"][0]["local_sample"] == 16000  # 删除点之后
        assert remapped["markers"][0]["local_sample"] == 5000    # 删除点之后

    def test_remap_through_stretch_scales_inside_segment(self) -> None:
        edit = new_audio_edit([make_step("mute", 24000, 48000, (0,))])
        stretch = _stretch_step(0, 48000, 200.0)  # 段输出 24000
        remapped, _ = remap_audio_edit_for_length_steps(edit, [stretch])
        mute = remapped["steps"][0]
        assert mute["params"]["start"] == 12000
        assert mute["params"]["end"] == 24000


# ----------------------------------------------------------------------
# 事务计划与命令（§3.4/§6.2）
# ----------------------------------------------------------------------
def make_timeline(**clip_overrides) -> tuple[TimelineSequence, EditTrack, ClipInstance]:
    params = dict(
        asset_id="asset", track_id="a1", timeline_start=0,
        timeline_duration=48, source_duration=48,
    )
    params.update(clip_overrides)
    clip = ClipInstance(**params)
    track = EditTrack(
        track_id="a1", name="Dialogue", track_type="audio", clips=(clip,))
    video = EditTrack(track_id="v1", name="Picture", track_type="video")
    return TimelineSequence(audio_tracks=(track,), video_tracks=(video,)), track, clip


class TestPlan:
    def test_gap_mode_shrinks_and_keeps_neighbors(self) -> None:
        timeline, track, clip = make_timeline(
            timeline_start=24, timeline_duration=48)
        new_edit = new_audio_edit([_stretch_step(0, 48000, 200.0)])
        plan = plan_clip_length_change(
            timeline, "a1", clip.clip_id, new_audio_edit=new_edit,
            old_output_samples=48000, ripple=False, fps=24.0)
        assert plan.blocked == ()
        assert plan.new_duration_frames == 12  # 1 s → 0.5 s @24 fps
        assert plan.delta_frames == -36
        new_clip = plan.after_sequence.get_track_by_id("a1").get_clip_by_id(clip.clip_id)
        assert new_clip.timeline_duration == 12
        assert new_clip.audio_edit["steps"][0]["processor_id"] == "time_stretch"

    def test_ripple_shifts_following_clips(self) -> None:
        clip_a = ClipInstance(clip_id="ca", asset_id="a", track_id="a1",
                              timeline_start=0, timeline_duration=48,
                              source_duration=48)
        clip_b = ClipInstance(clip_id="cb", asset_id="a", track_id="a1",
                              timeline_start=48, timeline_duration=48,
                              source_duration=48)
        track = EditTrack(track_id="a1", name="D", track_type="audio",
                          clips=(clip_a, clip_b))
        timeline = TimelineSequence(
            audio_tracks=(track,),
            video_tracks=(EditTrack(track_id="v1", track_type="video"),))
        new_edit = new_audio_edit([_stretch_step(0, 48000, 200.0)])
        plan = plan_clip_length_change(
            timeline, "a1", "ca", new_audio_edit=new_edit,
            old_output_samples=48000, ripple=True, fps=24.0)
        shifted = plan.after_sequence.get_track_by_id("a1").get_clip_by_id("cb")
        assert shifted.timeline_start == 48 - 36  # delta=-36

    def test_extension_without_ripple_blocked_by_neighbor(self) -> None:
        clip_a = ClipInstance(clip_id="ca", asset_id="a", track_id="a1",
                              timeline_start=0, timeline_duration=48,
                              source_duration=48)
        clip_b = ClipInstance(clip_id="cb", asset_id="a", track_id="a1",
                              timeline_start=48, timeline_duration=48,
                              source_duration=48)
        track = EditTrack(track_id="a1", name="D", track_type="audio",
                          clips=(clip_a, clip_b))
        timeline = TimelineSequence(
            audio_tracks=(track,),
            video_tracks=(EditTrack(track_id="v1", track_type="video"),))
        new_edit = new_audio_edit([_stretch_step(0, 96000, 50.0, domain=96000)])
        plan = plan_clip_length_change(
            timeline, "a1", "ca", new_audio_edit=new_edit,
            old_output_samples=96000, ripple=False, fps=24.0)
        assert any("overwrites" in b for b in plan.blocked)
        assert plan.after_sequence is timeline  # 阻断零写入

    def test_linked_video_blocks_unless_explicit(self) -> None:
        timeline, track, clip = make_timeline(linked_clip_id="vclip-1")
        new_edit = new_audio_edit([_delete_step(4800, 9600, 48000)])
        plan = plan_clip_length_change(
            timeline, "a1", clip.clip_id, new_audio_edit=new_edit,
            old_output_samples=48000, ripple=False, fps=24.0)
        assert any("linked" in b for b in plan.blocked)
        allowed = plan_clip_length_change(
            timeline, "a1", clip.clip_id, new_audio_edit=new_edit,
            old_output_samples=48000, ripple=False, fps=24.0,
            allow_independent_audio=True)
        assert allowed.blocked == ()

    def test_locked_track_and_missing_clip(self) -> None:
        timeline, track, clip = make_timeline()
        locked = TimelineSequence(
            audio_tracks=(EditTrack(track_id="a1", name="D", track_type="audio",
                                    is_locked=True, clips=(clip,)),),
            video_tracks=(EditTrack(track_id="v1", track_type="video"),))
        plan = plan_clip_length_change(
            locked, "a1", clip.clip_id, new_audio_edit=new_audio_edit([]),
            old_output_samples=48000, ripple=False)
        assert plan.blocked
        plan = plan_clip_length_change(
            timeline, "a1", "missing", new_audio_edit=new_audio_edit([]),
            old_output_samples=48000, ripple=False)
        assert plan.blocked

    def test_fades_clamped_and_inline_steps_remapped(self) -> None:
        timeline, track, clip = make_timeline(
            fade_in_duration=12, fade_out_duration=12)
        edit = new_audio_edit([make_step("mute", 30000, 36000, (0,))])
        timeline = timeline.with_track_updated(
            EditTrack(track_id="a1", name="D", track_type="audio",
                      clips=(ClipInstance(
                          clip_id=clip.clip_id, asset_id="a", track_id="a1",
                          timeline_start=0, timeline_duration=48,
                          source_duration=48, fade_in_duration=12,
                          fade_out_duration=12, audio_edit=edit),)))
        new_edit = append_step(edit, _stretch_step(0, 48000, 200.0))
        plan = plan_clip_length_change(
            timeline, "a1", clip.clip_id, new_audio_edit=new_edit,
            old_output_samples=48000, ripple=False, fps=24.0)
        new_clip = plan.after_sequence.get_track_by_id("a1").get_clip_by_id(clip.clip_id)
        # 新时长 12 帧：fade_in 12 保留，fade_out 被钳到 0（12+12 > 12）。
        assert new_clip.fade_in_duration == 12
        assert new_clip.fade_out_duration == 0
        assert any("fades clamped" in w for w in plan.warnings)
        # mute 30000-36000 落在拉伸段内 → 线性映射到 15000-18000。
        mute = [s for s in new_clip.audio_edit["steps"]
                if s["processor_id"] == "mute"][0]
        assert mute["params"]["start"] == 15000
        assert mute["params"]["end"] == 18000

    def test_transaction_command_applies_and_undoes(self) -> None:
        timeline, track, clip = make_timeline()
        new_edit = new_audio_edit([_stretch_step(0, 48000, 200.0)])
        plan = plan_clip_length_change(
            timeline, "a1", clip.clip_id, new_audio_edit=new_edit,
            old_output_samples=48000, ripple=False, fps=24.0)

        class _Proj:
            project_id = "p1"
            assets = {}
            timelines = {}

        project = _Proj()
        command = TimelineProjectTransactionCommand.create(
            plan.before_sequence, plan.after_sequence,
            before_project=project, after_project=project, label="stretch")
        stack = CommandStack()
        applied = stack.execute(command, timeline)
        edited = applied.get_track_by_id("a1").clips[0]
        assert edited.timeline_duration == 12
        assert edited.audio_edit is not None
        restored = stack.undo(applied)
        assert restored.get_track_by_id("a1").clips[0].timeline_duration == 48
        assert restored.get_track_by_id("a1").clips[0].audio_edit is None
        # 长度步骤参与签名（改参数必须重建引擎/缓存）。
        from src.features.edit.core.audio_edit import steps_signature
        other = new_audio_edit([_stretch_step(0, 48000, 175.0)])
        assert steps_signature(other) != steps_signature(new_edit)


def test_length_changing_set_matches_registry() -> None:
    assert LENGTH_CHANGING_PROCESSORS == {"time_stretch", "delete_range"}
