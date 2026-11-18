"""P0-3 非破坏性处理栈：模型、命令、DSP 与渲染路由签名。

验收来自 docs/plans/2026-11-18-clip-editor-and-mixer-roadmap.md §3：
- 只改左声道样本且保持右声道；选区外信号逐样本不变；撤销还原；
- 未知 processor 阻断应用与交付并给出逐条诊断；
- 步骤/旁路进入音频图快照哈希（改栈必须重建引擎）。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from src.features.edit.core.audio_edit import (
    AUDIO_EDIT_SCHEMA_VERSION,
    append_step,
    apply_audio_edit_to_pcm,
    apply_clip_audio_edit,
    canvas_points_from_envelope,
    envelope_from_canvas_points,
    envelope_nodes,
    evaluate_envelope_gain,
    make_step,
    new_audio_edit,
    remove_step,
    replace_step_enabled,
    set_envelope,
    set_markers,
    steps_signature,
    validate_audio_edit,
)
from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
from src.features.edit.core.commands import (
    CommandStack,
    UpdateClipAudioEditCommand,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


def make_timeline() -> TimelineSequence:
    track = EditTrack(
        track_id="a1", name="Dialogue", track_type="audio",
        clips=(ClipInstance(
            asset_id="asset", track_id="a1", timeline_start=0,
            timeline_duration=240, source_duration=240),),
    )
    video = EditTrack(track_id="v1", name="Picture", track_type="video")
    return TimelineSequence(audio_tracks=(track,), video_tracks=(video,))


def make_clip(**overrides) -> ClipInstance:
    params = dict(
        asset_id="asset", track_id="a1", timeline_start=0,
        timeline_duration=240, source_duration=240,
    )
    params.update(overrides)
    return ClipInstance(**params)


class TestModel:
    def test_make_step_structure_and_validation(self) -> None:
        step = make_step("range_gain", start=4800, end=9600, channels=(0,), gain_db=-6.0)
        assert step["processor_id"] == "range_gain"
        assert step["algorithm_version"] == 1
        edit = new_audio_edit([step])
        assert validate_audio_edit(edit, clip_output_samples=48000, channel_count=2) == []

    def test_unknown_processor_blocks_with_diagnostics(self) -> None:
        edit = new_audio_edit([make_step("range_gain", 0, 10, (0,), gain_db=0.0)])
        edit["steps"].append({
            "step_id": "x", "processor_id": "spectral_heal_2099",
            "algorithm_version": 9, "enabled": True, "params": {"start": 0, "end": 10, "channels": [0]},
        })
        diagnostics = validate_audio_edit(edit)
        assert any("unknown processor" in d for d in diagnostics)

    def test_out_of_range_and_duplicate_ids_flagged(self) -> None:
        edit = new_audio_edit([
            make_step("mute", start=100, end=50, channels=(0,)),
        ])
        assert any("half-open" in d for d in validate_audio_edit(edit))
        s1 = make_step("mute", start=0, end=10, channels=(0,), step_id="dup")
        s2 = make_step("mute", start=20, end=30, channels=(0,), step_id="dup")
        assert any("duplicate" in d for d in validate_audio_edit(new_audio_edit([s1, s2])))
        assert any("exceeds" in d for d in validate_audio_edit(
            new_audio_edit([make_step("mute", 0, 999999, (0,))]), clip_output_samples=48000))

    def test_clip_round_trip_preserves_steps_and_none_stays_absent(self) -> None:
        clip = make_clip(audio_edit=new_audio_edit(
            [make_step("normalize", 0, 48000, (0, 1), gain_db=3.2, label="norm")]))
        data = clip.to_dict()
        restored = ClipInstance.from_dict(data)
        assert restored.audio_edit is not None
        assert restored.audio_edit["steps"][0]["params"]["gain_db"] == pytest.approx(3.2)
        assert ClipInstance.from_dict(make_clip().to_dict()).audio_edit is None
        # 旧工程（无 audio_edit 键）载入不抛、保存不写。
        legacy = make_clip().to_dict()
        assert "audio_edit" not in legacy or legacy["audio_edit"] is None

    def test_unknown_top_level_fields_round_trip(self) -> None:
        edit = new_audio_edit([make_step("mute", 0, 10, (0,))])
        edit["future_field"] = {"keep": True}
        clip = make_clip(audio_edit=edit)
        restored = ClipInstance.from_dict(clip.to_dict())
        assert restored.audio_edit["future_field"] == {"keep": True}
        assert restored.audio_edit["version"] == AUDIO_EDIT_SCHEMA_VERSION


class TestCommand:
    def test_apply_undo_redo(self) -> None:
        timeline = make_timeline()
        stack = CommandStack()
        edit = new_audio_edit([make_step("mute", 0, 4800, (0, 1))])
        cmd = UpdateClipAudioEditCommand.create(
            "a1", timeline.get_track_by_id("a1").clips[0], edit)
        current = stack.execute(cmd, timeline)
        assert current.get_track_by_id("a1").clips[0].audio_edit == edit
        current = stack.undo(current)
        assert current.get_track_by_id("a1").clips[0].audio_edit is None
        current = stack.redo(current)
        assert current.get_track_by_id("a1").clips[0].audio_edit == edit

    def test_invalid_stack_rejected_at_construction(self) -> None:
        timeline = make_timeline()
        clip = timeline.audio_tracks[0].clips[0]
        bad = new_audio_edit([{
            "step_id": "z", "processor_id": "mystery", "algorithm_version": 1,
            "enabled": True, "params": {"start": 0, "end": 5, "channels": [0]},
        }])
        with pytest.raises(ValueError, match="unknown processor"):
            UpdateClipAudioEditCommand.create("a1", clip, bad)

    def test_serialization_round_trip(self) -> None:
        edit = new_audio_edit([make_step("range_gain", 10, 20, (1,), gain_db=-3.0)])
        clip = make_clip(audio_edit=edit)
        cmd = UpdateClipAudioEditCommand.create("a1", clip, edit)
        restored = UpdateClipAudioEditCommand.from_dict(cmd.to_dict())
        assert restored._after.audio_edit == edit


class TestDsp:
    def test_left_channel_only_gain_keeps_right(self) -> None:
        rate = 48000
        t = np.arange(rate, dtype=np.float32) / rate
        pcm = np.stack([
            0.4 * np.sin(2 * np.pi * 440.0 * t),
            0.4 * np.sin(2 * np.pi * 880.0 * t),
        ])
        edit = new_audio_edit([make_step(
            "range_gain", start=12000, end=36000, channels=(0,), gain_db=-6.0)])
        out = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)
        # 选区内：左声道 -6 dB（边缘 3 ms 过渡带内放宽），右声道不变。
        mid = slice(16000, 32000)
        assert 20 * math.log10(np.max(np.abs(out[0][mid])) / 0.4) == pytest.approx(-6.0, abs=0.05)
        assert np.array_equal(out[1][mid], pcm[1][mid])
        # 选区外逐样本不变。
        assert np.array_equal(out[0][:12000 - 200], pcm[0][:12000 - 200])
        assert np.array_equal(out[0][36000 + 200:], pcm[0][36000 + 200:])
        assert np.array_equal(out[1], pcm[1])

    def test_mute_zeros_middle_with_short_edges(self) -> None:
        rate = 48000
        pcm = np.full((1, rate), 0.5, dtype=np.float32)
        edit = new_audio_edit([make_step("mute", start=4800, end=9600, channels=(0,))])
        out = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)[0]
        assert np.all(out[6000:8400] == 0.0)               # 中段静音
        assert abs(out[4800 - 1] - 0.5) < 1e-6             # 选区外不变
        assert out[7200] == 0.0
        # 3 ms 边缘过渡（2–5 ms 合同）。
        assert 0.0 < out[4800 + 24] < 0.5
        assert 0.0 < out[9600 - 24] < 0.5

    def test_disabled_step_is_identity(self) -> None:
        pcm = np.ones((1, 100), dtype=np.float32) * 0.25
        step = make_step("mute", 0, 100, (0,))
        step["enabled"] = False
        out = apply_audio_edit_to_pcm(pcm, new_audio_edit([step]))
        assert np.array_equal(out, pcm)

    def test_block_split_matches_whole_array(self) -> None:
        rate = 48000
        rng = np.random.default_rng(20261118)
        pcm = rng.standard_normal((2, 4000)).astype(np.float32) * 0.2
        edit = new_audio_edit([
            make_step("range_gain", 500, 2500, (0,), gain_db=-4.0),
            make_step("mute", 2000, 3500, (1,)),
        ])
        whole = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)
        chunks = []
        for start in range(0, 4000, 700):
            block = pcm[:, start:start + 700]
            chunks.append(apply_clip_audio_edit(
                block, start, edit, sample_rate=rate))
        split = np.concatenate(chunks, axis=1)
        # 分块渲染与整段渲染逐样本一致（实时/离线一致的基础）。
        np.testing.assert_allclose(whole, split, atol=1e-6)

    def test_normalize_gain_math(self) -> None:
        rate = 48000
        t = np.arange(rate, dtype=np.float32) / rate
        sine = 0.2512 * np.sin(2 * np.pi * 1000.0 * t)   # ≈ -12 dBFS peak
        pcm = sine.reshape(1, -1)
        measured_peak = float(np.max(np.abs(pcm)))
        gain_db = -3.0 - 20 * math.log10(measured_peak)
        edit = new_audio_edit([make_step(
            "normalize", 0, rate, (0,), gain_db=round(gain_db, 4))])
        out = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)
        assert 20 * math.log10(float(np.max(np.abs(out)))) == pytest.approx(-3.0, abs=0.1)


class TestEnvelope:
    def test_linear_db_interpolation_midpoint(self) -> None:
        rate = 48000
        nodes = [
            {"id": "a", "local_sample": 0, "gain_db": 0.0, "interpolation": "linear"},
            {"id": "b", "local_sample": 4 * rate, "gain_db": -12.0, "interpolation": "linear"},
        ]
        gain = evaluate_envelope_gain(0, 4 * rate, nodes)
        assert gain is not None
        # 验收：4 s 从 0 dB 线性降到 −12 dB，2 s 处应为 −6 dB。
        assert 20 * math.log10(float(gain[2 * rate])) == pytest.approx(-6.0, abs=0.01)
        assert float(gain[0]) == pytest.approx(1.0)
        assert 20 * math.log10(float(gain[-1])) == pytest.approx(-12.0, abs=0.01)

    def test_endpoint_extension_outside_nodes(self) -> None:
        rate = 48000
        nodes = [{"id": "a", "local_sample": rate, "gain_db": -6.0, "interpolation": "linear"}]
        gain = evaluate_envelope_gain(0, 2 * rate, nodes)
        assert float(gain[0]) == pytest.approx(10 ** (-6 / 20))
        assert float(gain[-1]) == pytest.approx(10 ** (-6 / 20))

    def test_block_split_matches_whole_array(self) -> None:
        rate = 48000
        nodes = [
            {"id": "a", "local_sample": 0, "gain_db": 0.0, "interpolation": "linear"},
            {"id": "b", "local_sample": rate, "gain_db": -9.0, "interpolation": "linear"},
            {"id": "c", "local_sample": 2 * rate, "gain_db": 3.0, "interpolation": "linear"},
        ]
        whole = evaluate_envelope_gain(0, 3 * rate, nodes)
        chunks = np.concatenate([
            evaluate_envelope_gain(s, 700, nodes)
            for s in range(0, 3 * rate, 700)
        ])[: len(whole)]
        np.testing.assert_allclose(whole, chunks, atol=1e-6)

    def test_canvas_round_trip_and_unique_samples(self) -> None:
        points = [{"t": 0.0, "db": 0.0}, {"t": 1.0, "db": -6.0}, {"t": 2.0, "db": -6.0}]
        nodes = envelope_from_canvas_points(points, 2.0, 96000)
        assert [n["local_sample"] for n in nodes] == [0, 48000, 96000]
        back = canvas_points_from_envelope(nodes, 2.0, 96000)
        assert [p["t"] for p in back] == [0.0, 1.0, 2.0]
        assert [p["db"] for p in back] == [0.0, -6.0, -6.0]
        # 同一 sample 的重复点去重（后写覆盖）。
        dupes = envelope_from_canvas_points(
            [{"t": 0.5, "db": -1.0}, {"t": 0.5, "db": -2.0}], 1.0, 48000)
        assert len(dupes) == 1 and dupes[0]["gain_db"] == -2.0

    def test_envelope_applies_with_steps(self) -> None:
        rate = 48000
        pcm = np.full((1, rate), 0.5, dtype=np.float32)
        edit = new_audio_edit([make_step("mute", 0, 4800, (0,))])
        edit["envelope"] = [
            {"id": "a", "local_sample": 0, "gain_db": 0.0, "interpolation": "linear"},
            {"id": "b", "local_sample": rate, "gain_db": -20.0, "interpolation": "linear"},
        ]
        out = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)[0]
        # 末节点附近插值接近 −20 dB：0.5 × 10^(−20/20) ≈ 0.05。
        assert float(out[47990]) == pytest.approx(0.05, rel=0.02)

    def test_set_envelope_and_signature(self) -> None:
        edit = new_audio_edit([])
        nodes = [{"id": "a", "local_sample": 0, "gain_db": -3.0, "interpolation": "linear"}]
        with_env = set_envelope(edit, nodes)
        assert envelope_nodes(with_env) == nodes
        assert set_envelope(with_env, None).get("envelope") is None
        assert steps_signature(with_env) != steps_signature(edit)
        # 校验：重复 sample / 非法插值被逐条诊断。
        bad = set_envelope(edit, [
            {"id": "x", "local_sample": 5, "gain_db": 0.0, "interpolation": "spline"},
            {"id": "y", "local_sample": 5, "gain_db": 1.0, "interpolation": "linear"},
        ])
        diagnostics = validate_audio_edit(bad)
        assert any("duplicate local_sample" in d for d in diagnostics)
        assert any("unsupported interpolation" in d for d in diagnostics)


class TestMarkersAndPolarity:
    def test_marker_round_trip_and_copy_reassigns_ids(self) -> None:
        from src.features.edit.core.audio_edit import (
            canvas_markers_from, marker_list, markers_from_canvas,
            regenerate_marker_ids, set_markers,
        )
        points = [{"time": 0.5, "label": "breath"}, {"time": 2.0, "label": "pop"}]
        markers = markers_from_canvas(points, 2.0, 96000)
        assert [m["local_sample"] for m in markers] == [24000, 96000]
        assert len({m["id"] for m in markers}) == 2
        edit = set_markers(new_audio_edit([]), markers)
        assert marker_list(edit) == markers
        back = canvas_markers_from(marker_list(edit), 2.0, 96000)
        assert [p["label"] for p in back] == ["breath", "pop"]
        # 复制片段：同一源位置、全新 marker ID。
        copied = regenerate_marker_ids(edit)
        assert [m["local_sample"] for m in marker_list(copied)] == [24000, 96000]
        assert {m["id"] for m in marker_list(copied)}.isdisjoint(
            {m["id"] for m in markers})
        assert set_markers(edit, None).get("markers") is None
        # 诊断：重复/缺失 ID。
        bad = set_markers(edit, [
            {"id": "m1", "local_sample": 10, "name": "a"},
            {"id": "m1", "local_sample": 20, "name": "b"},
            {"id": "", "local_sample": 30, "name": "c"},
        ])
        diagnostics = validate_audio_edit(bad)
        assert any("duplicate marker id" in d for d in diagnostics)
        assert any("missing id" in d for d in diagnostics)

    def test_polarity_step_inverts_channel(self) -> None:
        rate = 48000
        pcm = np.full((2, rate), 0.25, dtype=np.float32)
        edit = new_audio_edit([make_step("channel_polarity", 0, rate, (1,))])
        out = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)
        # 左声道不变，右声道取反（边缘 3 ms 过渡带除外）。
        assert np.array_equal(out[0], pcm[0])
        assert abs(float(out[1][24000]) + 0.25) < 1e-6
        # 标记点不进声音签名（不影响引擎重建）。
        edit_with_marker = set_markers(edit, [
            {"id": "m", "local_sample": 100, "name": "x"}])
        assert steps_signature(edit_with_marker) == steps_signature(edit)

    def test_clip_copy_site_regenerates_marker_ids(self) -> None:
        from src.features.edit.core.audio_edit import set_markers
        edit = set_markers(None, [{"id": "orig", "local_sample": 5, "name": "m"}])
        clip = make_clip(audio_edit=edit)
        data = clip.to_dict()
        data["clip_id"] = "new-clip"
        # 与 Alt-拖动复制/粘贴相同的处理路径。
        from src.features.edit.core.audio_edit import regenerate_marker_ids
        data["audio_edit"] = regenerate_marker_ids(data["audio_edit"])
        copied = ClipInstance.from_dict(data)
        assert copied.audio_edit["markers"][0]["id"] != "orig"
        assert copied.audio_edit["markers"][0]["local_sample"] == 5


class TestRoutingSignatures:
    def test_snapshot_hash_changes_with_stack_and_ignores_bypassed(self) -> None:
        timeline = make_timeline()
        base = build_audio_graph_snapshot(timeline, revision=1)
        clip = timeline.audio_tracks[0].clips[0]
        edited = timeline.with_track_updated(
            timeline.audio_tracks[0].with_clip_updated(
                make_clip(clip_id=clip.clip_id, audio_edit=new_audio_edit(
                    [make_step("mute", 0, 4800, (0,))]))))
        with_edit = build_audio_graph_snapshot(edited, revision=1)
        assert with_edit.stable_hash != base.stable_hash
        # 旁路的步骤不影响声音 → 哈希回到无编辑基线（与 enabled 签名一致）。
        step = make_step("mute", 0, 4800, (0,))
        step["enabled"] = False
        bypassed = timeline.with_track_updated(
            timeline.audio_tracks[0].with_clip_updated(
                make_clip(clip_id=clip.clip_id, audio_edit=new_audio_edit([step]))))
        assert build_audio_graph_snapshot(bypassed, revision=1).stable_hash == base.stable_hash

    def test_steps_signature_orders_matter(self) -> None:
        s1 = make_step("mute", 0, 10, (0,), step_id="a")
        s2 = make_step("range_gain", 20, 30, (1,), gain_db=-3.0, step_id="b")
        assert steps_signature(new_audio_edit([s1, s2])) != \
            steps_signature(new_audio_edit([s2, s1]))

    def test_remove_step_returns_none_when_empty(self) -> None:
        edit = new_audio_edit([make_step("mute", 0, 10, (0,), step_id="only")])
        assert remove_step(edit, "only") is None
        untouched = remove_step(edit, "missing")
        assert untouched == edit and untouched is not None
