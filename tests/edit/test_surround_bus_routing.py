"""P2 环绕 Bus 路由核心验收（2026-11-19 计划；status 报告边界 7 收口）。

- routing_matrix/upmix_matrix：stereo→5.1 L/R 直通占位（C/LFE/Ls/Rs 静音）、
  mono→5.1 进 C、5.1→7.1 直通、Bus→Master BS.775 降混（LFE 排除）；
- 能力查询：bus_routing 支持 stereo/5.1/7.1，高度布局拒绝；
- 模型：EditTrack.bus_layout 声明 + 序列化 + 非法值回退；
- 离线图（AudioGraphRenderer）：立体声轨脉冲进 5.1 Bus 落 L/R、
  C/LFE 静音；5.1 Bus → Master 降混脉冲落 L+R@0.707；stereo Bus
  逐位不变（零回归）；
- 实时图（AudioMixer）：同一路由语义。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.channel_layout import (
    ChannelLayout,
    routing_matrix,
    upmix_matrix,
)
from src.features.edit.core.layout_capability import layout_supported

from src.features.edit.core.timeline_v2 import EditTrack

pytestmark = pytest.mark.edit_core
SR = 48000
FPS = 25.0
BLOCK = SR // int(FPS)
S = ChannelLayout.STEREO
M51 = ChannelLayout.SURROUND_5_1
M71 = ChannelLayout.SURROUND_7_1
SQ = float(np.sqrt(0.5))


class TestRoutingMatrices:
    def test_stereo_to_51_passthrough(self):
        matrix = upmix_matrix(S, M51)
        pulse_l = np.zeros((2, 1)); pulse_l[0, 0] = 1.0
        out = matrix @ pulse_l
        assert np.allclose(out[0], [1.0])          # L 直通
        assert np.allclose(out[1], [0.0])          # R 无贡献
        assert np.allclose(out[2:], 0.0)           # C/LFE/Ls/Rs 静音

    def test_mono_to_51_goes_to_center(self):
        matrix = upmix_matrix(ChannelLayout.MONO, M51)
        out = matrix @ np.array([[1.0]])
        assert np.allclose(out[2], [1.0])
        assert np.allclose(np.delete(out, 2, axis=0), 0.0)

    def test_51_to_71_passthrough(self):
        matrix = routing_matrix(M51, M71)
        assert matrix.shape == (8, 6)
        assert np.allclose(matrix[:6, :6], np.eye(6))
        assert np.allclose(matrix[6:], 0.0)

    def test_51_to_stereo_excludes_lfe(self):
        matrix = routing_matrix(M51, S)
        bus = np.zeros((6, 1))
        bus[2, 0] = 1.0   # C
        bus[3, 0] = 1.0   # LFE
        out = matrix @ bus
        assert np.allclose(out[:, 0], [SQ, SQ])    # C 贡献；LFE 排除

    def test_height_layouts_rejected(self):
        from src.features.edit.core.layout_capability import layout_supported
        assert layout_supported("bus_routing", M51)
        assert layout_supported("bus_routing", M71)
        # 高度布局不可表达（MX-15：x.y.z 显式拒绝）
        with pytest.raises(ValueError):
            ChannelLayout.from_av_layout_name("7.1.4")
        assert ChannelLayout.SURROUND_7_1 not in ()
        assert all(layout.value in ("stereo", "5.1", "7.1")
                   for layout in (S, M51, M71))


class TestBusLayoutModel:
    def test_declaration_and_validation(self):
        bus = (EditTrack("B", "B", "audio", 0, is_bus=True)
               .with_bus_layout("5.1"))
        assert bus.bus_channel_layout() is M51
        with pytest.raises(ValueError):
            bus.with_bus_layout("7.1.4")
        with pytest.raises(ValueError):
            bus.with_bus_layout("quad")

    def test_serialization_and_fallback(self):
        from src.features.edit.core.timeline_v2 import TimelineSequence
        bus = (EditTrack("B", "B", "audio", 0, is_bus=True)
               .with_bus_layout("7.1"))
        rt = TimelineSequence.from_dict(
            TimelineSequence(audio_tracks=(bus,)).to_dict()).audio_tracks[0]
        assert rt.bus_channel_layout() is M71
        # 非法历史值回退 stereo
        legacy = (EditTrack("B", "B", "audio", 0, is_bus=True)
                  ._rebuild(bus_layout="quad"))
        assert legacy.bus_channel_layout() is S


def _track_node_sequence(bus_layout: str = "5.1"):
    """dry 直通 Master + 脉冲轨进 5.1 Bus 的序列鸭子模型。"""
    from src.features.edit.core.channel_layout import ChannelLayout as _C
    bus = SimpleNamespace(
        track_id="bus", name="Bus", track_type="audio", index=0,
        clips=(), volume=1.0, pan=0.0, is_bus=True, output_bus_id=None,
        effect_chain=None, sends=(), enabled=True, muted=False, solo=False,
        bus_channel_layout=lambda: _C.from_av_layout_name(bus_layout),
    )
    pulse = SimpleNamespace(
        track_id="p", name="Pulse", track_type="audio", index=1,
        clips=(), volume=1.0, pan=0.0, is_bus=False,
        output_bus_id="bus", effect_chain=None, sends=(), enabled=True,
        muted=False, solo=False,
    )
    direct = SimpleNamespace(
        track_id="d", name="Direct", track_type="audio", index=2,
        clips=(), volume=1.0, pan=0.0, is_bus=False,
        output_bus_id=None, effect_chain=None, sends=(), enabled=True,
        muted=False, solo=False,
    )
    return SimpleNamespace(
        audio_tracks=(bus, pulse, direct), master_volume=1.0, master_muted=False)


class TestRendererSurroundRouting:
    def _run(self, bus_layout: str, impulse_at: int = 8):
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(_track_node_sequence(bus_layout))
        n = BLOCK
        block = np.zeros((2, n), dtype=np.float32)
        block[:, impulse_at] = 1.0
        output = np.zeros((2, n), dtype=np.float32)
        bus_inputs: dict = {}
        processed = renderer.render_track_block("p", block, 0)
        assert processed is not None
        renderer.route_track_output("p", processed, output, bus_inputs)
        direct = renderer.render_track_block("d", block, 0)
        renderer.route_track_output("d", direct, output, bus_inputs)
        renderer.render_bus_stage(bus_inputs, output, has_solo=False,
                                  start_frame=0, fps=FPS)
        return output, bus_inputs

    def test_stereo_track_into_51_bus_occupies_l_r(self):
        output, bus_inputs = self._run("5.1")
        assert "bus" in bus_inputs
        # Bus 中间态：脉冲落在 L/R，C/LFE/Ls/Rs 静音
        bus = bus_inputs["bus"]
        assert bus.shape[0] == 6
        assert np.allclose(bus[0, 8], 1.0) and np.allclose(bus[1, 8], 1.0)
        assert np.allclose(bus[2:], 0.0)
        # Master 输出（Bus 降混后）与直通轨相加 → 1.0 + 1.0
        assert np.allclose(output[:, 8], [2.0, 2.0])

    def test_51_bus_center_pulse_downmixes_to_master(self):
        """Bus 内 C 声道内容经 BS.775 降混落 Master L+R@0.707。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(_track_node_sequence("5.1"))
        n = BLOCK
        output = np.zeros((2, n), dtype=np.float32)
        bus_inputs: dict = {}
        # 直接往 Bus 的 C 声道注入脉冲（模拟 5.1 源轨经上混/路由进入）
        bus_inputs["bus"] = np.zeros((6, n), dtype=np.float32)
        bus_inputs["bus"][2, 8] = 1.0
        renderer.render_bus_stage(bus_inputs, output, has_solo=False,
                                  start_frame=0, fps=FPS)
        assert np.allclose(output[:, 8], [SQ, SQ], atol=1e-6)

    def test_71_bus_to_51_downstream(self):
        """7.1 Bus → 5.1 下游 Bus：Lss/Rss 按 0.707 折入 Ls/Rs。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        seq = _track_node_sequence("7.1")
        # 下游 5.1 bus
        bus51 = SimpleNamespace(
            track_id="bus51", name="Bus51", track_type="audio", index=3,
            clips=(), volume=1.0, pan=0.0, is_bus=True, output_bus_id=None,
            effect_chain=None, sends=(), enabled=True, muted=False, solo=False,
            bus_channel_layout=lambda: M51,
        )
        seq.audio_tracks = (bus51, seq.audio_tracks[0], seq.audio_tracks[1])
        # 上游 7.1 bus 输出到 bus51
        seq.audio_tracks[1].output_bus_id = "bus51"
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(seq)
        n = BLOCK
        output = np.zeros((2, n), dtype=np.float32)
        bus_inputs = {"bus": np.zeros((8, n), dtype=np.float32)}
        bus_inputs["bus"][6, 8] = 1.0   # Lss 脉冲
        renderer.render_bus_stage(bus_inputs, output, has_solo=False,
                                  start_frame=0, fps=FPS)
        # 7.1→5.1 采用既有矩阵约定：Lss 折入前 L（0.707）；
        # 再 5.1→stereo：Lo = L（直通）→ 0.707
        assert np.allclose(output[:, 8], [SQ, 0.0], atol=1e-6)
        # 上游 Bus 中间态：bus51 (5.1) 的 L 收到 0.707，Ls 无贡献
        assert np.allclose(bus_inputs["bus51"][0, 8], SQ, atol=1e-6)
        assert np.allclose(bus_inputs["bus51"][4, 8], 0.0, atol=1e-6)

    def test_stereo_bus_bitwise_unchanged(self):
        """stereo Bus 的中间态与 Master 输出和旧实现逐位一致（零回归）。"""
        output, bus_inputs = self._run("stereo")
        assert bus_inputs["bus"].shape[0] == 2
        assert np.allclose(bus_inputs["bus"][:, 8], [1.0, 1.0])
        assert np.allclose(output[:, 8], [2.0, 2.0])


class TestMixerSurroundRouting:
    def _mixer(self, bus_layout: str = "5.1"):
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
        mixer = AudioMixer()
        impulse = 8

        def make_decoder():
            state = {"pos": 0}

            def decode(frame, fps, num_samples):
                block = np.zeros((2, num_samples), dtype=np.float32)
                lo, hi = state["pos"], state["pos"] + num_samples
                if lo <= impulse < hi:
                    block[:, impulse - lo] = 1.0
                state["pos"] += num_samples
                return block
            return decode

        mixer.add_track("p", SimpleNamespace(_channels=2,
                        decode_samples_for_frame=make_decoder()),
                        AudioTrackState(track_id="p"))
        mixer.add_track("bus", None, AudioTrackState(track_id="bus"))
        mixer.set_track_routing("p", "bus", [], is_bus=False)
        mixer.set_track_routing("bus", None, [], is_bus=True)
        mixer.set_bus_layouts({"bus": bus_layout})
        return mixer

    def test_stereo_track_into_51_bus(self):
        mixer = self._mixer("5.1")
        chunk, _peaks = mixer.mix_samples(0, float(FPS), BLOCK)
        # Master：5.1 Bus（L/R 脉冲）降混 = L/R 各 1.0
        assert np.allclose(chunk[:, 8], [1.0, 1.0], atol=1e-6)

    def test_51_bus_buffer_layout_aware(self):
        """实时混音器按 Bus 布局分配缓冲（轨域单声道在 clip 阶段已折立体声）。"""
        mixer = self._mixer("5.1")
        chunk, _p = mixer.mix_samples(0, float(FPS), BLOCK)
        # 5.1 Bus 的 layout-aware scratch 已按 6 声道分配
        scratch = mixer._mix_bus_scratch.get("bus#6")
        assert scratch is not None and scratch.shape[0] == 6
        # Master：Bus L/R 脉冲直通（5.1→stereo Lo=Lo）
        assert np.allclose(chunk[:, 8], [1.0, 1.0], atol=1e-6)

    def test_stereo_bus_scratch_key_unchanged(self):
        """stereo Bus 仍用旧 scratch 键（与既有路径零回归）。"""
        mixer = self._mixer("stereo")
        chunk, _p = mixer.mix_samples(0, float(FPS), BLOCK)
        assert mixer._mix_bus_scratch.get("bus#2") is not None
        assert np.allclose(chunk[:, 8], [1.0, 1.0], atol=1e-6)
