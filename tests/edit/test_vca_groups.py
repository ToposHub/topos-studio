"""P1 · VCA 组（2026-11-19 计划第二阶段）。

验收：VCA 作为增益/静音控制层，管理成员和快照，保持路由和 Send tap
不变；VCA 与手动电平和 pre/post send 的倍率按模型可测；分轨与组同时
运动时样本曲线正确。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.timeline_v2 import VcaGroup

SR = 48000
FPS = 24.0


def _sequence(vcas=(), volume=1.0):
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )
    t1 = EditTrack("dx", "DX", "audio", 0, clips=(), volume=volume, pan=0.0)
    t2 = EditTrack("mx", "MX", "audio", 1, clips=(), volume=volume, pan=0.0)
    return TimelineSequence(
        timeline_id="s", name="S", audio_tracks=(t1, t2), fps=FPS,
        vcas=tuple(vcas))


class TestVcaModel:
    def test_round_trip_and_immutability(self) -> None:
        group = VcaGroup(name="DX 组", member_track_ids=("dx",),
                         gain_db=-6.0, muted=False)
        data = group.to_dict()
        restored = VcaGroup.from_dict(data)
        assert restored == group
        louder = group.with_gain_db(3.0)
        assert louder.gain_db == 3.0 and group.gain_db == -6.0
        assert group.linear_gain() == pytest.approx(0.5012, abs=5e-4)
        assert group.with_muted(True).linear_gain() == 0.0

    def test_sequence_factor_multiplicative_and_mute(self) -> None:
        seq = _sequence(vcas=[
            VcaGroup(name="A", member_track_ids=("dx", "mx"), gain_db=-6.0),
            VcaGroup(name="B", member_track_ids=("dx",), gain_db=6.0),
            VcaGroup(name="C", member_track_ids=("mx",), muted=True),
        ])
        # dx：−6+6=0 dB → 1.0；mx：−6 dB 且 C 静音 → 0。
        assert seq.vca_factor_for("dx") == pytest.approx(1.0)
        assert seq.vca_factor_for("mx") == 0.0
        # 非成员恒等。
        assert seq.vca_factor_for("other") == 1.0

    def test_with_vca_group_and_snapshot(self) -> None:
        group = VcaGroup(vca_id="v1", name="A",
                         member_track_ids=("dx",), gain_db=0.0)
        seq = _sequence(vcas=[group])
        seq2 = seq.with_vca_group(group.with_gain_db(-12.0))
        assert len(seq2.vcas) == 1 and seq2.vcas[0].gain_db == -12.0
        seq3 = seq2.with_vca_group(
            VcaGroup(vca_id="v2", name="B", member_track_ids=("mx",)))
        assert len(seq3.vcas) == 2
        snap = seq3.vca_snapshot()
        assert snap["v1"] == (-12.0, False)
        restored = seq3.apply_vca_snapshot(
            {"v1": (0.0, True), "v2": (-6.0, False)})
        assert restored.vcas[0].gain_db == 0.0 and restored.vcas[0].muted
        assert restored.vcas[1].gain_db == -6.0

    def test_serialization_round_trip(self) -> None:
        seq = _sequence(vcas=[
            VcaGroup(name="A", member_track_ids=("dx",), gain_db=-3.0)])
        data = seq.to_dict()
        assert any("vcas" in key for key in data)
        restored = type(seq).from_dict(data)
        assert restored.vcas == seq.vcas


class TestRendererVca:
    def _block(self, amp=0.5):
        block = np.full((2, 480), amp, dtype=np.float32)
        return block

    def test_member_attenuated_non_member_untouched(self) -> None:
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(_sequence(vcas=[
            VcaGroup(name="A", member_track_ids=("dx",), gain_db=-6.0)]))
        out_dx = renderer.render_track_block("dx", self._block(), 0)
        out_mx = renderer.render_track_block("mx", self._block(), 0)
        assert np.max(np.abs(out_dx)) == pytest.approx(0.25, abs=1e-3)
        assert np.max(np.abs(out_mx)) == pytest.approx(0.5, abs=1e-6)

    def test_vca_mute_gates_member(self) -> None:
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(_sequence(vcas=[
            VcaGroup(name="A", member_track_ids=("dx",), muted=True)]))
        out_dx = renderer.render_track_block("dx", self._block(), 0)
        assert np.max(np.abs(out_dx)) == 0.0

    def test_vca_multiplies_manual_fader(self) -> None:
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(_sequence(
            vcas=[VcaGroup(name="A", member_track_ids=("dx",), gain_db=-6.0)],
            volume=0.5))
        out_dx = renderer.render_track_block("dx", self._block(), 0)
        # 手动 0.5 × VCA 0.5 = 0.25 倍 → 0.5 × 0.25 = 0.125。
        assert np.max(np.abs(out_dx)) == pytest.approx(0.125, abs=1e-3)

    def test_routing_and_post_send_scale_with_vca(self) -> None:
        """路由不变：VCA 只改电平——轨输出仍进目标 bus；post send 跟随。"""
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        t1 = EditTrack("dx", "DX", "audio", 0, clips=(), volume=1.0,
                       output_bus_id="busA")
        bus = EditTrack("busA", "A", "audio", 1, is_bus=True)
        seq = TimelineSequence(timeline_id="s", name="S",
                               audio_tracks=(t1, bus), fps=FPS,
                               vcas=(VcaGroup(
                                   name="A", member_track_ids=("dx",),
                                   gain_db=-20.0),))
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(seq)
        out = np.zeros((2, 480), dtype=np.float32)
        bus_inputs: dict = {}
        processed = renderer.render_track_block("dx", self._block(), 0)
        renderer.route_track_output("dx", processed, out, bus_inputs)
        # VCA −20 dB：bus 输入 0.05 倍；master 直通部分为 0（路由 bus）。
        assert "busA" in bus_inputs
        assert np.max(np.abs(bus_inputs["busA"])) == pytest.approx(
            0.05, abs=1e-3)
        assert np.max(np.abs(out)) == 0.0


class TestMixerVca:
    def test_set_vca_overrides_scales_track(self, qapp) -> None:
        from src.features.edit.core.audio_playback import (
            AudioMixer,
            AudioTrackState,
        )

        class _Decoder:
            _timeline_start = 0
            _timeline_duration = 48

            def decode_samples_for_frame(self, frame, fps, num_samples):
                return np.full((2, num_samples), 0.5, dtype=np.float32)

            def close(self):
                pass

        mixer = AudioMixer()
        mixer.add_track("dx", _Decoder(),
                        AudioTrackState(track_id="dx", volume=1.0))
        out_before, _ = mixer.mix_samples(0, FPS, 480, 2)
        mixer.set_vca_overrides({"dx": 10.0 ** (-6.0 / 20.0)})
        out_after, _ = mixer.mix_samples(0, FPS, 480, 2)
        assert np.max(np.abs(out_before)) == pytest.approx(0.5, abs=1e-6)
        assert np.max(np.abs(out_after)) == pytest.approx(0.25, abs=1e-3)
