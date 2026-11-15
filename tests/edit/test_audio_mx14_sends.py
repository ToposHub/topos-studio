"""MX-14 回归：Send 前后级 tap 位置与独立 mute（音频成熟化计划 阶段F）。

验收（计划 §5 MX-14）：
- 通道推子降低 12dB 时 post-send 随之变化，pre-send 不变（实时 + 离线）；
- Send mute 不影响主输出；
- 旧工程（2 元组 / 无 tap 字段）默认 post_fader，声音不变；
- 命令往返：tap/muted 进 diff/undo，改 level 不复位 tap/muted（旧实现会丢）；
- muted send 不构成环检测边（不再假环告警）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import (
    AudioMixer,
    AudioTrackState,
    topological_sort_buses,
    unpack_send,
)
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    Send,
    TimelineSequence,
)

SR = 48000.0
FPS = 24.0
BLOCK = 480


class _ToneDecoder:
    def __init__(self, amp=0.4, freq=220.0, frames=(0,)):
        self.amp = amp
        self.freq = freq
        self.frames = set(frames)

    def decode_samples_for_frame(self, frame, fps, num_samples):
        if frame not in self.frames:
            return None
        t = (np.arange(num_samples) + frame * num_samples) / SR
        sig = (self.amp * np.sin(2.0 * np.pi * self.freq * t)).astype(np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


def _mixer_with_send(tap, muted=False, track_volume=1.0):
    """轨 t1 → master + send→bus1（指定 tap/mute）；bus1 → master。"""
    mixer = AudioMixer()
    mixer.set_sample_rate(SR)
    mixer.add_track("t1", _ToneDecoder(0.4, 220.0, (0,)),
                    AudioTrackState(track_id="t1", volume=track_volume))
    mixer.set_track_routing("t1", None, [("bus1", 0.5, tap, muted)],
                            is_bus=False)
    mixer.add_track("bus1", _ToneDecoder(0.0, 220.0, ()),
                    AudioTrackState(track_id="bus1"))
    mixer.set_track_routing("bus1", None, [], is_bus=True)
    return mixer


def _peaks_after_one_block(mixer):
    out, peaks = mixer.mix_samples(0, FPS, BLOCK, 2)
    return out, peaks


class TestMx14Model:
    def test_send_schema_defaults_and_roundtrip(self):
        s = Send(target_bus_id="b1", level=0.5)
        assert s.tap == "post_fader" and s.muted is False
        rt = Send.from_dict(s.to_dict())
        assert (rt.tap, rt.muted) == ("post_fader", False)

    def test_legacy_dict_loads_post_fader(self):
        """旧工程 send dict（无 tap/muted 键）→ 默认值，声音不变。"""
        legacy = {"send_id": "s1", "target_bus_id": "b1",
                  "level": 0.7, "enabled": True}
        s = Send.from_dict(legacy)
        assert s.tap == "post_fader" and s.muted is False

    def test_unknown_tap_degrades_to_post_fader(self):
        s = Send.from_dict({"target_bus_id": "b1", "tap": "pre_FX_experimental"})
        assert s.tap == "post_fader"

    def test_invalid_tap_rejected(self):
        with pytest.raises(ValueError):
            Send(target_bus_id="b1", tap="bogus")

    def test_unpack_send_compat(self):
        assert unpack_send(("b1", 0.5)) == ("b1", 0.5, "post_fader", False)
        assert unpack_send(("b1", 0.5, "pre_fader", True)) == \
            ("b1", 0.5, "pre_fader", True)
        assert unpack_send({"target_bus_id": "b1", "level": 0.25,
                            "tap": "pre_fader"}) == \
            ("b1", 0.25, "pre_fader", False)


class TestMx14RealtimeTapSemantics:
    def test_fader_down_12db_post_follows_pre_unchanged(self, qapp):
        """验收：推子 -12dB（0.251）→ post send 跌 12dB，pre send 不变。"""
        def _bus_peak(tap, volume):
            mixer = _mixer_with_send(tap, track_volume=volume)
            _, peaks = _peaks_after_one_block(mixer)
            return peaks["bus1"].peak_l

        vol_up, vol_down = 1.0, 10 ** (-12 / 20)
        pre_up = _bus_peak("pre_fader", vol_up)
        pre_dn = _bus_peak("pre_fader", vol_down)
        post_up = _bus_peak("post_fader", vol_up)
        post_dn = _bus_peak("post_fader", vol_down)

        assert pre_up == pytest.approx(pre_dn, rel=0.02), \
            "pre-fader send must not follow the channel fader"
        drop_db = 20 * np.log10(max(post_up, 1e-9) / max(post_dn, 1e-9))
        assert drop_db == pytest.approx(12.0, abs=1.0), \
            f"post-fader send must follow fader (got {drop_db:.2f} dB)"

    def test_send_mute_leaves_main_output_intact(self, qapp):
        """验收：send mute 只断开该 send——主通路（t1→bus1）输出不变。"""
        def _run(muted):
            mixer = AudioMixer()
            mixer.set_sample_rate(SR)
            mixer.add_track("t1", _ToneDecoder(0.4, 220.0, (0,)),
                            AudioTrackState(track_id="t1"))
            # 主通路：t1 → bus1（output）；并行 send → bus2
            mixer.set_track_routing("t1", "bus1", [("bus2", 0.5, "post_fader", muted)],
                                    is_bus=False)
            mixer.add_track("bus1", _ToneDecoder(0.0, 220.0, ()),
                            AudioTrackState(track_id="bus1"))
            mixer.set_track_routing("bus1", None, [], is_bus=True)
            mixer.add_track("bus2", _ToneDecoder(0.0, 220.0, ()),
                            AudioTrackState(track_id="bus2"))
            mixer.set_track_routing("bus2", None, [], is_bus=True)
            return mixer.mix_samples(0, FPS, BLOCK, 2)

        out_on, peaks_on = _run(False)
        out_muted, peaks_muted = _run(True)
        # 主通路 bus1 读数不变
        assert peaks_on["bus1"].peak_l == pytest.approx(
            peaks_muted["bus1"].peak_l, rel=1e-6)
        # send 目标 bus2：mute 后无贡献
        assert peaks_on["bus2"].peak_l > 0.05
        assert peaks_muted.get("bus2") is None or \
            peaks_muted["bus2"].peak_l == 0.0

    def test_legacy_two_tuple_still_post_fader(self, qapp):
        """旧 2 元组路由（无 tap 字段）→ post-fader 行为（声音不变）。"""
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)
        mixer.add_track("t1", _ToneDecoder(0.4, 220.0, (0,)),
                        AudioTrackState(track_id="t1", volume=0.251))
        mixer.set_track_routing("t1", None, [("bus1", 0.5)], is_bus=False)
        mixer.add_track("bus1", _ToneDecoder(0.0, 220.0, ()),
                        AudioTrackState(track_id="bus1"))
        mixer.set_track_routing("bus1", None, [], is_bus=True)
        _, peaks = mixer.mix_samples(0, FPS, BLOCK, 2)
        # 推子 -12dB 生效于 post send（与显式 post_fader 相同量级）
        assert peaks["bus1"].peak_l == pytest.approx(0.4 * 0.251 * 0.5, rel=0.05)

    def test_bus_send_pre_post(self, qapp):
        """Bus send 同一 tap 契约：bus 音量只影响 post send。"""
        def _bus2_peak(tap, bus_vol):
            mixer = AudioMixer()
            mixer.set_sample_rate(SR)
            mixer.add_track("t1", _ToneDecoder(0.4, 220.0, (0,)),
                            AudioTrackState(track_id="t1"))
            mixer.set_track_routing("t1", "bus1", [], is_bus=False)
            mixer.add_track("bus1", _ToneDecoder(0.0, 220.0, ()),
                            AudioTrackState(track_id="bus1", volume=bus_vol))
            mixer.set_track_routing("bus1", None, [("bus2", 1.0, tap, False)],
                                    is_bus=True)
            mixer.add_track("bus2", _ToneDecoder(0.0, 220.0, ()),
                            AudioTrackState(track_id="bus2"))
            mixer.set_track_routing("bus2", None, [], is_bus=True)
            _, peaks = mixer.mix_samples(0, FPS, BLOCK, 2)
            return peaks["bus2"].peak_l

        vol_up, vol_dn = 1.0, 0.251
        assert _bus2_peak("pre_fader", vol_up) == \
            pytest.approx(_bus2_peak("pre_fader", vol_dn), rel=0.02)
        drop = _bus2_peak("post_fader", vol_up) / max(
            _bus2_peak("post_fader", vol_dn), 1e-9)
        assert 20 * np.log10(drop) == pytest.approx(12.0, abs=1.0)


class TestMx14RendererTapSemantics:
    def _renderer(self, tap, bus_volume):
        """t1→bus1（主通路）；bus1 --send(tap)--> bus2→master。"""
        track = EditTrack(
            track_id="t1", name="T1", track_type="audio",
            output_bus_id="bus1",
        )
        bus = EditTrack(track_id="bus1", name="B1", track_type="audio",
                        is_bus=True, volume=bus_volume,
                        sends=(Send(target_bus_id="bus2", level=0.5, tap=tap),))
        bus2 = EditTrack(track_id="bus2", name="B2", track_type="audio",
                         is_bus=True)
        seq = TimelineSequence(name="M", audio_tracks=(track, bus, bus2))
        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(seq)
        return r

    def test_render_bus_stage_pre_post(self):
        """离线 render_bus_stage：pre send 不随 bus 音量，post 随；
        主通路（bus1→master）两种 tap 下都随音量。"""
        def _run(tap, vol):
            r = self._renderer(tap, vol)
            n = 480
            sig = np.zeros((2, n), dtype=np.float32)
            sig[0, 0] = sig[1, 0] = 0.8
            out = np.zeros((2, n), dtype=np.float32)
            bus_inputs = {"bus1": sig}
            r.render_bus_stage(bus_inputs, out, has_solo=False,
                               start_frame=0, fps=FPS)
            master_peak = float(np.max(np.abs(out)))
            send_peak = float(np.max(np.abs(bus_inputs["bus2"])))
            return master_peak, send_peak

        vol_up, vol_dn = 1.0, 10 ** (-12 / 20)
        # pre send：不随 bus 音量
        _, pre_up = _run("pre_fader", vol_up)
        _, pre_dn = _run("pre_fader", vol_dn)
        assert pre_up == pytest.approx(pre_dn, rel=0.02)
        assert pre_up == pytest.approx(0.8 * 0.5, rel=0.02)
        # post send：随 bus 音量 -12dB
        m_up, post_up = _run("post_fader", vol_up)
        m_dn, post_dn = _run("post_fader", vol_dn)
        assert post_up == pytest.approx(0.8 * 0.5, rel=0.02)
        drop = 20 * np.log10(post_up / max(post_dn, 1e-9))
        assert drop == pytest.approx(12.0, abs=1.0)
        # 主通路两种 tap 都随音量（send 不影响主输出路径）
        assert 20 * np.log10(m_up / max(m_dn, 1e-9)) == pytest.approx(12.0, abs=1.0)

    def test_route_track_output_pre_fader_source(self):
        """route_track_output：pre_fader send 消费 pre_fader_samples。"""
        track = EditTrack(
            track_id="t1", name="T1", track_type="audio",
            sends=(Send(target_bus_id="bus1", level=0.5, tap="pre_fader"),),
        )
        bus = EditTrack(track_id="bus1", name="B1", track_type="audio",
                        is_bus=True)
        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(
            TimelineSequence(name="M", audio_tracks=(track, bus)))
        processed = np.full((2, 64), 0.1, dtype=np.float32)   # 推子后 0.1
        pre = np.full((2, 64), 0.4, dtype=np.float32)         # 推子前 0.4
        out = np.zeros((2, 64), dtype=np.float32)
        bus_inputs = {}
        r.route_track_output("t1", processed, out, bus_inputs,
                             pre_fader_samples=pre)
        assert float(np.max(np.abs(bus_inputs["bus1"]))) == \
            pytest.approx(0.4 * 0.5, rel=1e-6)


class TestMx14Commands:
    def _track(self):
        return EditTrack(
            track_id="a1", name="A1", track_type="audio",
            sends=(Send(send_id="s1", target_bus_id="b1", level=0.5,
                        tap="pre_fader", muted=True),),
        )

    def test_update_level_preserves_tap_and_muted(self):
        """旧缺陷锁：改 level 不得复位 tap/muted（旧 UpdateSendCommand 重建
        Send 时丢字段）。"""
        from src.features.edit.core.commands import UpdateSendCommand
        seq = TimelineSequence(name="M", audio_tracks=(self._track(),))
        cmd = UpdateSendCommand.create("a1", "s1", level=0.8)
        seq2, applied = cmd.apply(seq)
        s = seq2.get_track_by_id("a1").sends[0]
        assert s.level == 0.8
        assert s.tap == "pre_fader" and s.muted is True
        # undo 恢复全部
        seq3 = applied.undo(seq2)
        s3 = seq3.get_track_by_id("a1").sends[0]
        assert (s3.level, s3.tap, s3.muted) == (0.5, "pre_fader", True)

    def test_update_tap_and_muted_roundtrip(self):
        from src.features.edit.core.commands import UpdateSendCommand
        seq = TimelineSequence(name="M", audio_tracks=(self._track(),))
        cmd = UpdateSendCommand.create("a1", "s1", tap="post_fader",
                                       muted=False)
        seq2, applied = cmd.apply(seq)
        s = seq2.get_track_by_id("a1").sends[0]
        assert (s.tap, s.muted) == ("post_fader", False)
        rt = UpdateSendCommand.from_dict(applied.to_dict())
        seq3 = rt.undo(seq2)
        s3 = seq3.get_track_by_id("a1").sends[0]
        assert (s3.tap, s3.muted) == ("pre_fader", True)

    def test_add_send_with_tap_muted_serialization(self):
        from src.features.edit.core.commands import AddSendCommand
        cmd = AddSendCommand.create("a1", "b1", level=0.3,
                                    tap="pre_fader", muted=True)
        seq = TimelineSequence(name="M", audio_tracks=(
            EditTrack(track_id="a1", name="A1", track_type="audio"),))
        seq2, _ = cmd.apply(seq)
        s = seq2.get_track_by_id("a1").sends[0]
        assert (s.tap, s.muted) == ("pre_fader", True)
        rt = AddSendCommand.from_dict(cmd.to_dict())
        assert (rt.tap, rt.muted) == ("pre_fader", True)


class TestMx14CycleDetection:
    def test_muted_send_is_not_a_cycle_edge(self):
        """B1→(send, muted)→B2→B1：send 静音时不构成环（不再假环告警）。"""
        routing = {
            "b1": {"output_bus_id": None, "sends": [
                ("b2", 0.5, "post_fader", True)], "is_bus": True},
            "b2": {"output_bus_id": "b1", "sends": [], "is_bus": True},
        }
        sorted_ids, cycles = topological_sort_buses(["b1", "b2"], routing)
        assert not cycles
        assert set(sorted_ids) == {"b1", "b2"}

    def test_unmuted_send_cycle_detected(self):
        routing = {
            "b1": {"output_bus_id": None, "sends": [
                ("b2", 0.5, "post_fader", False)], "is_bus": True},
            "b2": {"output_bus_id": "b1", "sends": [], "is_bus": True},
        }
        _, cycles = topological_sort_buses(["b1", "b2"], routing)
        assert set(cycles) == {"b1", "b2"}
