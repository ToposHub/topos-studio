"""
Solo 可听闭包测试（RMA-04 阶段 A3）。

验收门槛（计划表 A3）：
四组 solo PCM oracle：solo source、solo Bus、多 solo、solo source + send/return。
断言选中的信号存在、未选闭包外信号不存在，且静音优先级高于 solo。

核心 bug（旧逻辑）：
- solo 普通轨（输出到 Bus B→Master）：B 未 solo 被剔除 → 该轨无声。
- solo Bus B（接收普通轨 T）：T 未 solo 被剔除 → B 无输入，无声。

修复：compute_audible_solo_closure 保证 solo 节点到 Master 的完整可听路径。
本测试覆盖闭包函数本身 + 实时 AudioMixer + 离线 AudioGraphRenderer 三条路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_playback import (
    AudioMixer, AudioTrackState, compute_audible_solo_closure,
)


N = 480
FPS = 24.0


class _DCDecoder:
    """DC 假 decoder（每帧返回恒定振幅）。"""
    def __init__(self, amp=0.5):
        self.amp = float(amp)
    def decode_samples_for_frame(self, frame, fps, num_samples):
        sig = np.full(num_samples, self.amp, dtype=np.float32)
        return np.stack([sig, sig])
    def close(self):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. 闭包函数本身的 oracle
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeAudibleSoloClosure:
    """RMA-04: compute_audible_solo_closure 的图级闭包计算。"""

    def test_no_solo_returns_empty(self):
        """无 solo → 空集（调用方据此放行全部节点）。"""
        routing = {"t1": {"output_bus_id": None, "sends": [], "is_bus": False}}
        result = compute_audible_solo_closure(["t1"], routing, [])
        assert result == set()

    def test_solo_track_includes_downstream_bus_chain(self):
        """solo 普通轨 → 保留该轨 + 其输出 Bus 链到 Master。

        T → BusA → BusB → Master。solo T 时 BusA、BusB 都必须保留。
        """
        routing = {
            "T": {"output_bus_id": "BusA", "sends": [], "is_bus": False},
            "BusA": {"output_bus_id": "BusB", "sends": [], "is_bus": True},
            "BusB": {"output_bus_id": None, "sends": [], "is_bus": True},
            "Other": {"output_bus_id": None, "sends": [], "is_bus": False},
        }
        result = compute_audible_solo_closure(
            ["T", "BusA", "BusB", "Other"], routing, ["T"],
        )
        assert "T" in result
        assert "BusA" in result  # 下游 Bus
        assert "BusB" in result  # 下游 Bus
        assert "Other" not in result  # 闭包外

    def test_solo_track_does_not_include_other_inputs_to_its_bus(self):
        """普通轨 solo 只保留自身路径，不能放行同一 Bus 的其它输入。"""
        routing = {
            "Solo": {"output_bus_id": "Bus", "sends": [], "is_bus": False},
            "Other": {"output_bus_id": "Bus", "sends": [], "is_bus": False},
            "Bus": {"output_bus_id": None, "sends": [], "is_bus": True},
        }
        result = compute_audible_solo_closure(
            ["Solo", "Other", "Bus"], routing, ["Solo"],
        )
        assert result == {"Solo", "Bus"}

    def test_solo_bus_includes_upstream_inputs(self):
        """solo Bus → 保留该 Bus + 其所有上游输入（让 Bus 收到信号）。

        T1 → BusA, T2 → BusA, BusA → Master。solo BusA 时 T1、T2 都要保留。
        """
        routing = {
            "T1": {"output_bus_id": "BusA", "sends": [], "is_bus": False},
            "T2": {"output_bus_id": "BusA", "sends": [], "is_bus": False},
            "T3": {"output_bus_id": None, "sends": [], "is_bus": False},
            "BusA": {"output_bus_id": None, "sends": [], "is_bus": True},
        }
        result = compute_audible_solo_closure(
            ["T1", "T2", "T3", "BusA"], routing, ["BusA"],
        )
        assert "BusA" in result
        assert "T1" in result  # 上游输入
        assert "T2" in result  # 上游输入
        assert "T3" not in result  # 闭包外（不送 BusA）

    def test_solo_bus_includes_send_upstream(self):
        """solo Bus（接收 send）→ send 的上游也要保留。

        T → Master（direct），同时 send 到 BusFX。solo BusFX 时 T 要保留。
        """
        routing = {
            "T": {"output_bus_id": None, "sends": [("BusFX", 0.5)], "is_bus": False},
            "BusFX": {"output_bus_id": None, "sends": [], "is_bus": True},
        }
        result = compute_audible_solo_closure(
            ["T", "BusFX"], routing, ["BusFX"],
        )
        assert "BusFX" in result
        assert "T" in result  # send 上游

    def test_multi_solo_union(self):
        """多 solo → 闭包是各 solo 闭包的并集。"""
        routing = {
            "T1": {"output_bus_id": "BusA", "sends": [], "is_bus": False},
            "T2": {"output_bus_id": "BusB", "sends": [], "is_bus": False},
            "BusA": {"output_bus_id": None, "sends": [], "is_bus": True},
            "BusB": {"output_bus_id": None, "sends": [], "is_bus": True},
        }
        result = compute_audible_solo_closure(
            ["T1", "T2", "BusA", "BusB"], routing, ["T1", "T2"],
        )
        assert {"T1", "BusA", "T2", "BusB"} <= result


# ─────────────────────────────────────────────────────────────────────────────
# 2. 实时 AudioMixer：solo 闭包的 PCM oracle
# ─────────────────────────────────────────────────────────────────────────────

class TestRealtimeSoloClosure:
    """RMA-04: AudioMixer.mix_samples 的 solo 闭包——选中的信号有声，闭包外无声。"""

    def _mix(self, mixer):
        return mixer.mix_samples(current_frame=0, fps=FPS, num_samples=N,
                                 output_channels=2)

    def test_solo_track_through_bus_is_audible(self, qapp):
        """solo 普通轨（输出到 Bus→Master）→ 该轨有声（旧 bug：Bus 被剔除→无声）。"""
        mixer = AudioMixer()
        # T → Bus → Master
        mixer.register_bus_track("Bus", AudioTrackState(track_id="Bus", volume=1.0))
        mixer.add_track("T", _DCDecoder(0.5),
                        AudioTrackState(track_id="T", volume=1.0, solo=True))
        mixer.set_track_routing("T", output_bus_id="Bus", sends=[], is_bus=False)
        mixer.set_track_routing("Bus", output_bus_id=None, sends=[], is_bus=True)
        mixer.update_track_solo("T", True)

        output, peaks = self._mix(mixer)
        # 关键：信号应到达 master（旧 bug 下 Bus 未 solo 被跳过 → output 全 0）
        peak = float(np.max(np.abs(output)))
        assert peak > 0.1, f"solo 普通轨经 Bus 应有声，但 master peak={peak}"

    def test_solo_track_excludes_other_track_sharing_bus(self, qapp):
        """普通轨 solo 经共享 Bus 时，不得漏进同 Bus 的其它普通轨。"""
        mixer = AudioMixer()
        mixer.register_bus_track("Bus", AudioTrackState(track_id="Bus", volume=1.0))
        mixer.add_track("Solo", _DCDecoder(0.3),
                        AudioTrackState(track_id="Solo", volume=1.0, solo=True))
        mixer.add_track("Other", _DCDecoder(0.4),
                        AudioTrackState(track_id="Other", volume=1.0))
        mixer.set_track_routing("Solo", output_bus_id="Bus", sends=[], is_bus=False)
        mixer.set_track_routing("Other", output_bus_id="Bus", sends=[], is_bus=False)
        mixer.set_track_routing("Bus", output_bus_id=None, sends=[], is_bus=True)
        mixer.update_track_solo("Solo", True)

        output, peaks = self._mix(mixer)
        assert float(np.max(np.abs(output))) == pytest.approx(0.3, abs=1e-5)
        assert set(peaks) == {"Solo", "Bus"}

    def test_solo_bus_keeps_upstream_track(self, qapp):
        """solo Bus → 其上游普通轨保留，Bus 收到信号（旧 bug：上游被剔除→Bus 无声）。"""
        mixer = AudioMixer()
        mixer.register_bus_track("Bus", AudioTrackState(track_id="Bus", volume=1.0, solo=True))
        mixer.add_track("T", _DCDecoder(0.5),
                        AudioTrackState(track_id="T", volume=1.0))
        mixer.set_track_routing("T", output_bus_id="Bus", sends=[], is_bus=False)
        mixer.set_track_routing("Bus", output_bus_id=None, sends=[], is_bus=True)
        mixer.update_track_solo("Bus", True)

        output, peaks = self._mix(mixer)
        peak = float(np.max(np.abs(output)))
        assert peak > 0.1, f"solo Bus 应收到上游信号，但 master peak={peak}"

    def test_solo_track_excludes_unrelated_track(self, qapp):
        """solo 普通轨 T1（→Master）→ 另一独立轨 T2（→Master）被排除。"""
        mixer = AudioMixer()
        mixer.add_track("T1", _DCDecoder(0.5),
                        AudioTrackState(track_id="T1", volume=1.0, solo=True))
        mixer.add_track("T2", _DCDecoder(0.4),
                        AudioTrackState(track_id="T2", volume=1.0))
        mixer.update_track_solo("T1", True)

        output, peaks = self._mix(mixer)
        # T1 amp=0.5, pan=0 → stereo balance unity，每侧 0.5；T2 不贡献。
        # 若 T2 错误参与，会叠加到 ~1.0。阈值确认 T2 被排除。
        peak = float(np.max(np.abs(output)))
        assert 0.4 < peak < 0.6, f"solo T1 应排除 T2，master peak={peak}"

    def test_solo_track_with_send_to_fx_bus(self, qapp):
        """solo 普通轨 T（→Master + send 到 BusFX）→ T 与 BusFX 都保留。"""
        mixer = AudioMixer()
        mixer.register_bus_track("BusFX", AudioTrackState(track_id="BusFX", volume=1.0))
        mixer.add_track("T", _DCDecoder(0.5),
                        AudioTrackState(track_id="T", volume=1.0, solo=True))
        mixer.set_track_routing("T", output_bus_id=None,
                                sends=[("BusFX", 0.5)], is_bus=False)
        mixer.set_track_routing("BusFX", output_bus_id=None, sends=[], is_bus=True)
        mixer.update_track_solo("T", True)

        output, peaks = self._mix(mixer)
        peak = float(np.max(np.abs(output)))
        # T direct: 0.5*0.7071≈0.354/侧；send 0.5→BusFX→master: 0.5*0.5*0.7071≈0.177/侧
        # 合计 ≈0.53/侧。若旧 bug 把 BusFX 剔除，则只有 0.354。
        assert peak > 0.45, f"solo T + send 应让 BusFX 也贡献，master peak={peak}"

    def test_muted_priority_over_solo_closure(self, qapp):
        """静音优先级高于 solo：muted 的 solo 轨仍静音。"""
        mixer = AudioMixer()
        # T muted+solo，输出到 Bus
        mixer.register_bus_track("Bus", AudioTrackState(track_id="Bus", volume=1.0))
        mixer.add_track("T", _DCDecoder(0.5),
                        AudioTrackState(track_id="T", volume=1.0,
                                        muted=True, solo=True))
        mixer.set_track_routing("T", output_bus_id="Bus", sends=[], is_bus=False)
        mixer.set_track_routing("Bus", output_bus_id=None, sends=[], is_bus=True)
        mixer.update_track_solo("T", True)

        output, peaks = self._mix(mixer)
        peak = float(np.max(np.abs(output)))
        assert peak < 1e-6, f"muted+solo 轨应静音，master peak={peak}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. 离线 AudioGraphRenderer：solo 闭包与实时一致
# ─────────────────────────────────────────────────────────────────────────────

class TestOfflineSoloClosure:
    """RMA-04: AudioGraphRenderer 的 solo 闭包与实时路径语义一致。"""

    def _build_seq(self):
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
        # T → Bus → Master
        t = EditTrack(track_id="T", track_type="audio", index=0,
                      volume=1.0, is_solo=True)
        bus = EditTrack(track_id="Bus", track_type="audio", index=1,
                        volume=1.0, is_bus=True, output_bus_id=None)
        t = t.with_output_bus("Bus")
        return TimelineSequence(audio_tracks=(t, bus))

    def test_renderer_closure_includes_downstream_bus(self, qapp):
        """renderer.compute_audible_solo_closure 包含 solo 轨的下游 Bus。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        seq = self._build_seq()
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(seq)
        closure = renderer.compute_audible_solo_closure()
        assert "T" in closure
        assert "Bus" in closure  # 下游 Bus 必须在闭包内（旧 bug 会遗漏）

    def test_solo_track_audible_via_renderer(self, qapp):
        """solo 普通轨经 Bus → render_bus_stage 保留 Bus，master 有声。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        seq = self._build_seq()
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(seq)
        closure = renderer.compute_audible_solo_closure()

        channels = 2
        output = np.zeros((channels, N), dtype=np.float32)
        bus_inputs = {"Bus": np.full((channels, N), 0.3, dtype=np.float32)}
        # has_solo=True，传闭包
        renderer.render_bus_stage(bus_inputs, output, has_solo=True,
                                  audible_solo=closure)
        peak = float(np.max(np.abs(output)))
        assert peak > 0.1, f"render_bus_stage 应保留 solo 轨的下游 Bus，peak={peak}"

    def test_solo_excludes_bus_outside_closure(self, qapp):
        """闭包外的 Bus 不参与处理（has_solo 且 Bus 不在闭包内）。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
        # T1 solo → BusA（在闭包内）；独立 BusB（不在闭包内）
        t1 = EditTrack(track_id="T1", track_type="audio", index=0, is_solo=True)
        busA = EditTrack(track_id="BusA", track_type="audio", index=1,
                         is_bus=True, output_bus_id=None)
        busB = EditTrack(track_id="BusB", track_type="audio", index=2,
                         is_bus=True, output_bus_id=None)
        t1 = t1.with_output_bus("BusA")
        seq = TimelineSequence(audio_tracks=(t1, busA, busB))
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(seq)
        closure = renderer.compute_audible_solo_closure()
        assert "BusA" in closure
        assert "BusB" not in closure  # BusB 闭包外
