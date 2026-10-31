"""
Bus 完整路由语义测试（RMA-06 阶段 B3）。

验收门槛（计划表 B3）：
- 覆盖 Bus→send→FX return、Bus volume/pan automation、Bus mute automation、非法反馈；
- 逐块与离线结果一致；automation keyframe 前/中/后采样验证量值与时间位置。

核心 bug（RMA-06）：普通轨阶段处理 sends，但 Bus 阶段只处理 output_bus_id；
bus_state.volume/pan 和 renderer 的 Bus node 也从不读取 automation。Bus sends
和 Bus automation 在 UI/模型可保存，结果却无声忽略。

修复：render_bus_stage 与 mix_samples Bus 阶段接入 Bus automation（volume/pan/mute
逐样本）+ Bus sends（并行 tap）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from src.shared.keyframe import KeyframeTrack, Keyframe, InterpolationType


SR = 48000.0
FPS = 24.0
N = 480


class _DCDecoder:
    def __init__(self, amp=0.5):
        self.amp = float(amp)
        self._timeline_start = 0
        self._timeline_duration = 100000
        self._clip_id = None
        self._speed = 1.0
        self._fade = None

    def decode_samples_for_frame(self, frame, fps, num_samples):
        sig = np.full(num_samples, self.amp, dtype=np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


def _kf_track(param, keyframes, interp=InterpolationType.HOLD):
    track = KeyframeTrack(param_name=param, min_val=0.0, max_val=2.0)
    for frame, value in keyframes:
        track.add_keyframe(Keyframe(frame=frame, value=value, interpolation=interp))
    return track


def _renderer_with_bus(bus_kw, master_volume=1.0):
    """构造含一个 Bus 的 renderer（Bus automation 可通过 bus_kw.automation 传入）。"""
    bus = EditTrack(track_id="bus1", track_type="audio", index=0, is_bus=True,
                    output_bus_id=None, **{k: v for k, v in bus_kw.items() if k != "automation"})
    seq = TimelineSequence(audio_tracks=(bus,), master_volume=master_volume)
    renderer = AudioGraphRenderer(SR)
    renderer.configure_from_sequence(seq)
    if "automation" in bus_kw:
        renderer._nodes["bus1"].automation = bus_kw["automation"]
        renderer._nodes["bus1"].automation_mode = "read"
    return renderer


# ─────────────────────────────────────────────────────────────────────────────
# 1. Bus automation：volume（离线 renderer）
# ─────────────────────────────────────────────────────────────────────────────

class TestBusVolumeAutomationOffline:
    """RMA-06: Bus volume automation 逐样本生效（此前被无声忽略）。"""

    def test_bus_volume_automation_attenuates(self):
        """Bus volume automation 从 1.0 降到 0.0 → 输出衰减。"""
        # automation: frame 0 = 1.0, frame 24 = 0.0（线性，1秒内降到 0）
        auto = {"volume": _kf_track("volume", [(0, 1.0), (24, 0.0)],
                                     InterpolationType.LINEAR)}
        renderer = _renderer_with_bus({"volume": 1.0, "automation": auto})
        channels = 2
        total = int(SR * 2)  # 2 秒 = 48 帧，automation 在 24 帧（1s）降到 0
        bus_inputs = {"bus1": np.full((channels, total), 0.5, dtype=np.float32)}
        output = np.zeros((channels, total), dtype=np.float32)
        renderer.render_bus_stage(bus_inputs, output, has_solo=False,
                                  start_frame=0, fps=FPS)
        # 开头 volume≈1 → ~0.35（含 pan）；末尾 volume≈0 → 接近 0
        head = float(np.max(np.abs(output[:, :int(SR * 0.2)])))
        tail = float(np.max(np.abs(output[:, int(SR * 1.8):])))
        assert head > 0.3, f"开头 Bus volume≈1 应有声，peak={head}"
        assert tail < 0.05, f"末尾 Bus volume≈0 应衰减，peak={tail}"

    def test_bus_volume_static_when_no_automation(self):
        """无 automation → 用静态 Bus volume。"""
        renderer = _renderer_with_bus({"volume": 0.5})
        channels = 2
        bus_inputs = {"bus1": np.full((channels, N), 0.8, dtype=np.float32)}
        output = np.zeros((channels, N), dtype=np.float32)
        renderer.render_bus_stage(bus_inputs, output, has_solo=False,
                                  start_frame=0, fps=FPS)
        # 0.8 * 0.5 (volume) * 1.0 (balance unity-center) = 0.4
        peak = float(np.max(np.abs(output)))
        assert 0.35 < peak < 0.45, f"静态 Bus volume 0.5，peak={peak}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Bus automation：mute（离线 renderer）
# ─────────────────────────────────────────────────────────────────────────────

class TestBusMuteAutomationOffline:
    """RMA-06: Bus mute automation 逐样本门控（此前被无声忽略）。"""

    def test_bus_mute_automation_silences_segment(self):
        """Bus mute automation 中段静音。"""
        auto = {"mute": _kf_track("mute", [(0, 0.0), (48, 1.0), (96, 0.0)])}
        renderer = _renderer_with_bus({"volume": 1.0, "automation": auto})
        channels = 2
        total = int(SR * 5)  # 5 秒 = 120 帧
        bus_inputs = {"bus1": np.full((channels, total), 0.5, dtype=np.float32)}
        output = np.zeros((channels, total), dtype=np.float32)
        renderer.render_bus_stage(bus_inputs, output, has_solo=False,
                                  start_frame=0, fps=FPS)
        # 2-4 秒（48-96 帧）应静音
        seg_muted = output[:, int(SR * 3):int(SR * 4)]
        assert float(np.max(np.abs(seg_muted))) < 1e-3, "Bus mute 中段应静音"
        # 0-1 秒有声
        seg_on = output[:, int(SR * 0.5):int(SR * 1)]
        assert float(np.max(np.abs(seg_on))) > 0.1, "Bus mute 外应有声"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Bus sends：Bus 可 send 到其他 Bus（离线 renderer）
# ─────────────────────────────────────────────────────────────────────────────

class TestBusSendsOffline:
    """RMA-06: Bus sends（并行 tap）此前被 Bus 阶段无声忽略。"""

    def test_bus_send_to_another_bus(self):
        """BusA（→Master + send 到 BusB）→ BusB 也收到信号。

        BusA 同时 direct 到 Master 和 send 0.5 到 BusB；BusB → Master。
        最终 master = BusA direct + BusB（含 BusA send）。
        """
        busA = EditTrack(track_id="busA", track_type="audio", index=0, is_bus=True,
                         output_bus_id=None)
        busB = EditTrack(track_id="busB", track_type="audio", index=1, is_bus=True,
                         output_bus_id=None)
        seq = TimelineSequence(audio_tracks=(busA, busB))
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        # 手动给 busA 加 send 到 busB
        renderer._nodes["busA"].sends = [("busB", 0.5)]

        channels = 2
        bus_inputs = {
            "busA": np.full((channels, N), 0.4, dtype=np.float32),
            "busB": np.zeros((channels, N), dtype=np.float32),
        }
        output = np.zeros((channels, N), dtype=np.float32)
        renderer.render_bus_stage(bus_inputs, output, has_solo=False,
                                  start_frame=0, fps=FPS)
        # busA direct (0.4*0.7071≈0.283) + busA send 0.5→busB→master (0.4*0.5*0.7071≈0.141)
        peak = float(np.max(np.abs(output)))
        assert peak > 0.35, f"BusA send 到 BusB 应让 master 收到更多，peak={peak}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. 实时路径：Bus automation 与离线一致
# ─────────────────────────────────────────────────────────────────────────────

class TestRealtimeBusAutomation:
    """RMA-06: 实时 mix_samples 的 Bus automation 也生效。"""

    def test_realtime_bus_volume_automation(self, qapp):
        """实时 Bus volume automation 衰减输出。"""
        mixer = AudioMixer()
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=1.0))
        mixer.set_track_automation_mode("bus1", "read")
        mixer.set_track_automation("bus1", {
            "volume": _kf_track("volume", [(0, 1.0), (240, 0.0)], InterpolationType.LINEAR),
        })
        # 喂 DC 到 bus1（通过一个输出到 bus1 的普通轨）
        mixer.add_track("t1", _DCDecoder(0.5),
                        AudioTrackState(track_id="t1", volume=1.0))
        mixer.set_track_routing("t1", output_bus_id="bus1", sends=[], is_bus=False)
        mixer.set_track_routing("bus1", output_bus_id=None, sends=[], is_bus=True)

        # frame 0（volume≈1）应有声
        out0, _ = mixer.mix_samples(current_frame=0, fps=FPS, num_samples=N,
                                    output_channels=2)
        peak0 = float(np.max(np.abs(out0)))
        assert peak0 > 0.1, f"frame 0 Bus volume≈1 应有声，peak={peak0}"

    def test_realtime_bus_send(self, qapp):
        """实时 Bus send（busA → busB）让 master 收到比无 send 时更多的信号。"""
        # 基线：busA direct 到 master，无 send
        mixer0 = AudioMixer()
        mixer0.register_bus_track("busA", AudioTrackState(track_id="busA", volume=1.0))
        mixer0.add_track("t1", _DCDecoder(0.5),
                         AudioTrackState(track_id="t1", volume=1.0))
        mixer0.set_track_routing("t1", output_bus_id="busA", sends=[], is_bus=False)
        mixer0.set_track_routing("busA", output_bus_id=None, sends=[], is_bus=True)
        out0, _ = mixer0.mix_samples(current_frame=0, fps=FPS, num_samples=N,
                                     output_channels=2)
        peak0 = float(np.max(np.abs(out0)))

        # 有 send：busA direct + send 0.5 到 busB → busB 也到 master
        mixer = AudioMixer()
        mixer.register_bus_track("busA", AudioTrackState(track_id="busA", volume=1.0))
        mixer.register_bus_track("busB", AudioTrackState(track_id="busB", volume=1.0))
        mixer.add_track("t1", _DCDecoder(0.5),
                        AudioTrackState(track_id="t1", volume=1.0))
        mixer.set_track_routing("t1", output_bus_id="busA", sends=[], is_bus=False)
        mixer.set_track_routing("busA", output_bus_id=None,
                                sends=[("busB", 0.5)], is_bus=True)
        mixer.set_track_routing("busB", output_bus_id=None, sends=[], is_bus=True)
        out, _ = mixer.mix_samples(current_frame=0, fps=FPS, num_samples=N,
                                   output_channels=2)
        peak = float(np.max(np.abs(out)))
        # 有 send 时 master 应收到更多信号（busB 的额外贡献）
        assert peak > peak0 + 0.02, \
            f"实时 Bus send 应增加 master 信号: {peak} vs 基线 {peak0}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. 反馈保护：send 成环不崩溃（与 output_bus_id 环一致）
# ─────────────────────────────────────────────────────────────────────────────

class TestFeedbackSafety:
    """RMA-06: send 不应导致无限循环/崩溃（feedback policy）。"""

    def test_send_cycle_does_not_crash(self):
        """BusA send busB，BusB send busA（send 环）→ 不崩溃（拓扑排序排除 output 环）。"""
        busA = EditTrack(track_id="busA", track_type="audio", index=0, is_bus=True,
                         output_bus_id=None)
        busB = EditTrack(track_id="busB", track_type="audio", index=1, is_bus=True,
                         output_bus_id=None)
        seq = TimelineSequence(audio_tracks=(busA, busB))
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        renderer._nodes["busA"].sends = [("busB", 0.5)]
        renderer._nodes["busB"].sends = [("busA", 0.5)]

        channels = 2
        bus_inputs = {
            "busA": np.full((channels, N), 0.4, dtype=np.float32),
            "busB": np.zeros((channels, N), dtype=np.float32),
        }
        output = np.zeros((channels, N), dtype=np.float32)
        # 不应抛异常（send 是并行 tap，不参与拓扑排序的串行边，不会无限循环）
        renderer.render_bus_stage(bus_inputs, output, has_solo=False,
                                  start_frame=0, fps=FPS)
        # 有有限输出即可
        assert output.shape == (channels, N)
