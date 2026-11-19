"""P0 · Clip FX PDC——片段级效果器链算法延迟的最迟路径对齐（收口批次 2）。

修前缺口：clip FX 链（item-FX，decoder 后、轨音量前）的延迟声明
（true_peak_limiter lookahead / voice_changer WOLA 等）从未参与 PDC——
挂链 clip 相对同轨/异轨干内容迟到 lookahead 样本（voice_changer 默认
1024 ≈ 21 ms，可听的 flam）。

修后契约（轨/Bus 两级对齐在 clip 级的延伸）：

- 轨节点 PDC max = 轨链延迟 + 该轨 clip 链最大延迟（实时 recompute 与
  离线 note_clip_chain_latency 同公式）；``pdc_total_samples`` 预算随之
  延长（尾音/输出长度不截断）；
- 同轨 clip 间由相对延迟线对齐到 clip_max（键=clip_id；无链/旁路/失活
  链按 0 延迟计，须补满 clip_max）；
- mute/solo/禁用/无 decoder/自动化门控跳过路径推进 clip 相对延迟线
  （持留内容按绝对时间继续流出）；
- 已结束 clip 的 drain 预算 = max(链尾音, 链延迟) + 相对延迟线持留量，
  覆盖全部已结束 clip（不只挂链的 clip）；
- 实时 AudioMixer、离线 AudioGraphRenderer、成片与 Stem 导出同语义。
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

from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.stem_export import render_stem_pcm

pytestmark = pytest.mark.edit_core
SR = 48000
FPS = 25
BLOCK = SR // int(FPS)
PULSE_AT = 100
AMP = 0.4


class _LatencyFx:
    """声明算法延迟 L 的测试效果：输出 = 输入延迟 L（EffectChain 可重建）。

    接口与 audio_dsp.AudioEffect 协议对齐（ctor 接 sample_rate、
    set_params(params, sample_rate=...)、process/latency/tail/reset）。
    """

    def __init__(self, sample_rate: float = 48000.0, latency: int = 0) -> None:
        self._sr = float(sample_rate)
        self._latency = int(latency)
        self._history = np.zeros((2, self._latency), dtype=np.float32)
        self.is_active = True

    def set_params(self, params: object, sample_rate: float | None = None,
                   **_kw: object) -> None:
        if isinstance(params, dict):
            self._latency = max(0, int(params.get("latency", self._latency)))
        if sample_rate is not None:
            self._sr = float(sample_rate)
        self._history = np.zeros((2, self._latency), dtype=np.float32)

    def process(self, samples: np.ndarray, sidechain_by_id=None) -> np.ndarray:
        combined = np.concatenate([self._history, samples], axis=1)
        out = combined[:, :samples.shape[-1]]
        self._history = combined[:, samples.shape[-1]:]
        return np.ascontiguousarray(out, dtype=np.float32)

    def latency_samples(self) -> int:
        return self._latency

    def tail_estimate_samples(self) -> int:
        return self._latency

    def sidechain_sources(self) -> set:
        return set()

    def sidechain_map(self, _taps) -> dict:
        return {}

    def collect_gain_reduction(self) -> dict:
        return {}

    def collect_detector_levels(self) -> dict:
        return {}

    def tap_levels(self) -> dict:
        return {}

    def reset_state(self) -> None:
        self._history = np.zeros((2, self._latency), dtype=np.float32)


def _pulse_decoder(clip_id: str, pulse_sample: int = PULSE_AT,
                   timeline_start: int = 0):
    """注入单脉冲的 decoder 替身（块边界无关：跨块状态机）。"""
    state = {"pos": 0}

    def decode(frame, fps, num_samples):
        block = np.zeros((2, num_samples), dtype=np.float32)
        lo, hi = state["pos"], state["pos"] + num_samples
        if lo <= pulse_sample < hi:
            block[:, pulse_sample - lo] = AMP
        state["pos"] += num_samples
        return block

    return SimpleNamespace(
        _channels=2, _clip_id=clip_id,
        _timeline_start=timeline_start, _timeline_duration=BLOCK,
        decode_samples_for_frame=decode,
    )


def _register_probe(monkeypatch) -> None:
    from src.features.edit.core import audio_dsp
    monkeypatch.setitem(audio_dsp._EFFECT_CLASS_REGISTRY, "pdc_probe",
                        _LatencyFx)


def _peak_position(buf: np.ndarray) -> int:
    flat = np.argmax(np.abs(buf[0]))
    return int(flat)


class TestRealtimeClipPdc:
    """实时 AudioMixer：同轨跨 clip / 跨轨脉冲对齐。"""

    def _mixer_two_tracks(self, monkeypatch):
        """干轨 T_dry + 延迟 clip 轨 T_fx（pdc_probe L=1024）。"""
        _register_probe(monkeypatch)
        LAT = 1024
        mixer = AudioMixer()
        mixer.add_track("T_dry", _pulse_decoder("c_dry"),
                        AudioTrackState(track_id="T_dry"))
        mixer.add_track("T_fx", _pulse_decoder("c_fx"),
                        AudioTrackState(track_id="T_fx"))
        mixer.set_clip_effects(
            "c_fx", [("pdc_probe", {"latency": LAT}, True)], track_id="T_fx")
        return mixer, LAT

    def test_cross_track_pulse_alignment(self, monkeypatch):
        """干轨与延迟 clip 轨的脉冲在 Master 汇合处落在同一样本。"""
        mixer, LAT = self._mixer_two_tracks(monkeypatch)
        chunk, _peaks = mixer.mix_samples(0, float(FPS), BLOCK)
        # 两轨脉冲同位于 PULSE_AT + LAT（干轨节点推满补偿、延迟 clip 链
        # 自带 L 而节点不再推）→ 求和后单一重合脉冲
        assert mixer.pdc_total_samples() == LAT
        peak = _peak_position(chunk)
        assert peak == PULSE_AT + LAT
        # 重合：单峰（两轨幅值相加），另一轨独立到达位置无能量
        assert float(np.max(np.abs(chunk[0]))) == pytest.approx(
            2 * AMP, abs=1e-4)

    def test_track_pdc_accounts_clip_latency(self, monkeypatch):
        """轨节点延迟 = 轨链延迟 + clip 链最大延迟（recompute 公式）。"""
        mixer, LAT = self._mixer_two_tracks(monkeypatch)
        assert mixer._clip_lat_max.get("T_fx") == LAT
        assert mixer._clip_chain_lat.get("c_fx") == LAT
        # T_dry 干轨推满；T_fx 节点延迟 = LAT − (0 + LAT) = 0
        assert mixer._pdc_delay.get("T_dry") == LAT
        assert mixer._pdc_delay.get("T_fx", 0) == 0

    def test_same_track_clips_align(self, monkeypatch):
        """同轨：延迟链 clip 与干 clip 的脉冲重合（相对延迟线）。"""
        _register_probe(monkeypatch)
        LAT = 1024
        mixer = AudioMixer()

        class _TwoPulseDecoder:
            def __init__(self, clip_id: str) -> None:
                self._channels = 2
                self._clip_id = clip_id
                self._timeline_start = 0
                self._timeline_duration = BLOCK
                self._pos = 0

            def decode_samples_for_frame(self, frame, fps, num_samples):
                block = np.zeros((2, num_samples), dtype=np.float32)
                lo, hi = self._pos, self._pos + num_samples
                if lo <= PULSE_AT < hi:
                    block[:, PULSE_AT - lo] = AMP
                self._pos += num_samples
                return block

        mixer.add_track("T", _TwoPulseDecoder("c_a"),
                        AudioTrackState(track_id="T"))
        mixer.add_track("T", _TwoPulseDecoder("c_b"),
                        AudioTrackState(track_id="T"), replace_existing=False)
        # c_a 挂延迟链；c_b 干（无链）
        mixer.set_clip_effects(
            "c_a", [("pdc_probe", {"latency": LAT}, True)], track_id="T")
        chunk, _peaks = mixer.mix_samples(0, float(FPS), BLOCK)
        # 干 clip 经相对延迟线补满 LAT；延迟 clip 自带 LAT → 重合
        peak = _peak_position(chunk)
        assert peak == PULSE_AT + LAT
        assert float(np.max(np.abs(chunk[0]))) == pytest.approx(
            2 * AMP, abs=1e-4)

    def test_zero_latency_graph_unchanged(self, monkeypatch):
        """全零延迟图：无补偿、无位移（零回归）。"""
        _register_probe(monkeypatch)
        mixer = AudioMixer()
        mixer.add_track("T", _pulse_decoder("c"),
                        AudioTrackState(track_id="T"))
        mixer.set_clip_effects(
            "c", [("pdc_probe", {"latency": 0}, True)], track_id="T")
        chunk, _peaks = mixer.mix_samples(0, float(FPS), BLOCK)
        assert mixer.pdc_total_samples() == 0
        assert _peak_position(chunk) == PULSE_AT

    def test_muted_track_advances_clip_lines(self, monkeypatch):
        """mute 跳过路径推进 clip 相对延迟线（持留内容按绝对时间流出）。"""
        _register_probe(monkeypatch)
        LAT = 1024
        mixer = AudioMixer()

        class _SilentDecoder:
            def __init__(self, clip_id: str) -> None:
                self._channels = 2
                self._clip_id = clip_id
                self._timeline_start = 0
                self._timeline_duration = BLOCK
                self._pos = 0

            def decode_samples_for_frame(self, frame, fps, num_samples):
                return np.zeros((2, num_samples), dtype=np.float32)

        mixer.add_track("T", _SilentDecoder("c_fx"),
                        AudioTrackState(track_id="T"))
        mixer.add_track("T", _SilentDecoder("c_b"),
                        AudioTrackState(track_id="T"), replace_existing=False)
        mixer.set_clip_effects(
            "c_fx", [("pdc_probe", {"latency": LAT}, True)], track_id="T")
        # 干 clip c_b 的线持留非零内容（rel = LAT − 0 > 0）；
        # _clip_track_of 由此前块解码记账（此处模拟既有播放）
        held = np.zeros((2, 100), dtype=np.float32)
        held[:, 0] = 0.5
        mixer._clip_pdc_lines["c_b"] = held
        mixer._clip_track_of["c_b"] = "T"
        mixer._tracks["T"].muted = True
        mixer.mix_samples(0, float(FPS), BLOCK)
        buffered = mixer._clip_pdc_lines.get("c_b")
        # 持留内容（0.5 脉冲）已被零块推出（线剩余为推出块尾部的零填充）
        assert buffered is None or float(np.max(np.abs(buffered))) == 0.0

    def test_ended_clip_line_flush(self, monkeypatch):
        """已结束干 clip 的相对延迟线持留内容经 drain 流出（不截断）。"""
        _register_probe(monkeypatch)
        LAT = 1024
        mixer = AudioMixer()
        # 干 clip c_b 结束于 frame 0 末；其线持留 100 样本
        mixer.add_track("T", _pulse_decoder("c_a"),
                        AudioTrackState(track_id="T"))
        mixer.set_clip_effects(
            "c_a", [("pdc_probe", {"latency": LAT}, True)], track_id="T")
        mixer._clip_track_of["c_b"] = "T"
        mixer._clip_end_frames["c_b"] = 0  # frame 0 起即已结束（end 帧之后）
        held = np.zeros((2, LAT), dtype=np.float32)
        held[:, -1] = AMP  # 尾部持留脉冲
        mixer._clip_pdc_lines["c_b"] = held
        # c_b 无链 → rel = LAT → drain flush 推出持留脉冲（进轨求和）
        chunk, _peaks = mixer.mix_samples(0, float(FPS), BLOCK)
        buffered = mixer._clip_pdc_lines.get("c_b")
        assert buffered is None or float(np.max(np.abs(buffered))) == 0.0
        # 持留脉冲按绝对时间流出（持留缓冲末端 → 本块 offset LAT−1）
        assert float(chunk[0, LAT - 1]) == pytest.approx(AMP, abs=1e-4)


class TestRendererClipPdc:
    """离线 AudioGraphRenderer：note 申报 + 相对延迟线 + 跨轨对齐。"""

    def test_note_updates_node_pdc(self):
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(SimpleNamespace(
            audio_tracks=(
                SimpleNamespace(track_id="T_dry", name="D", track_type="audio",
                                index=0, clips=(), volume=1.0, pan=0.0,
                                is_bus=False, output_bus_id=None,
                                effect_chain=None, sends=(), enabled=True,
                                muted=False, solo=False),
                SimpleNamespace(track_id="T_fx", name="F", track_type="audio",
                                index=1, clips=(), volume=1.0, pan=0.0,
                                is_bus=False, output_bus_id=None,
                                effect_chain=None, sends=(), enabled=True,
                                muted=False, solo=False),
            ),
            master_volume=1.0, master_muted=False))
        assert renderer.pdc_total_samples() == 0
        renderer.note_clip_chain_latency("T_fx", 1024)
        assert renderer.pdc_total_samples() == 1024
        assert renderer._pdc_delay.get("T_dry") == 1024
        assert renderer._pdc_delay.get("T_fx", 0) == 0

    def test_render_block_relative_delay(self):
        """render_track_block 的 clip_pdc_delay 相对延迟线（键=clip_id）。"""
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(SimpleNamespace(
            audio_tracks=(SimpleNamespace(
                track_id="T", name="T", track_type="audio", index=0,
                clips=(), volume=1.0, pan=0.0, is_bus=False,
                output_bus_id=None, effect_chain=None, sends=(),
                enabled=True, muted=False, solo=False),),
            master_volume=1.0, master_muted=False))
        n = BLOCK
        block = np.zeros((2, n), dtype=np.float32)
        block[:, PULSE_AT] = AMP
        out = renderer.render_track_block(
            "T", block, 0, clip_pdc_delay=1024, clip_pdc_key="c1")
        assert out is not None
        assert _peak_position(out) == PULSE_AT + 1024

    def test_offline_cross_track_alignment(self, monkeypatch):
        """离线：干轨与延迟 clip 轨的脉冲同样本汇合（与实时一致）。

        干轨无延迟链 → 轨 clip_max=0 → 其 clip rel=0，对齐全部来自节点
        PDC（pdc_max − 0 = LAT）；挂链轨 clip 链自带 LAT + rel 0 + 节点
        延迟 0 → 同一绝对样本。
        """
        _register_probe(monkeypatch)
        LAT = 1024
        renderer = AudioGraphRenderer(float(SR))
        renderer.configure_from_sequence(SimpleNamespace(
            audio_tracks=(
                SimpleNamespace(track_id="T_dry", name="D", track_type="audio",
                                index=0, clips=(), volume=1.0, pan=0.0,
                                is_bus=False, output_bus_id=None,
                                effect_chain=None, sends=(), enabled=True,
                                muted=False, solo=False),
                SimpleNamespace(track_id="T_fx", name="F", track_type="audio",
                                index=1, clips=(), volume=1.0, pan=0.0,
                                is_bus=False, output_bus_id=None,
                                effect_chain=None, sends=(), enabled=True,
                                muted=False, solo=False),
            ),
            master_volume=1.0, master_muted=False))
        renderer.note_clip_chain_latency("T_fx", LAT)
        n = BLOCK
        block = np.zeros((2, n), dtype=np.float32)
        block[:, PULSE_AT] = AMP
        # T_fx：clip 链（自带 LAT）+ rel 0
        chain = EffectChain(float(SR))
        chain.set_params([("pdc_probe", {"latency": LAT}, True)])
        fx_out = renderer.render_track_block("T_fx", block, 0,
                                             clip_fx_chain=chain)
        # T_dry：无延迟链 → 无相对延迟（rel=0），节点 PDC 推满 LAT
        dry_out = renderer.render_track_block("T_dry", block, 0)
        output = np.zeros((2, n), dtype=np.float32)
        bus_inputs: dict = {}
        renderer.route_track_output("T_fx", fx_out, output, bus_inputs)
        renderer.route_track_output("T_dry", dry_out, output, bus_inputs)
        renderer.render_bus_stage(bus_inputs, output, has_solo=False,
                                  start_frame=0, fps=FPS)
        assert _peak_position(output) == PULSE_AT + LAT
        assert float(np.max(np.abs(output[0]))) == pytest.approx(
            2 * AMP, abs=1e-4)


class TestStemExportClipPdc:
    """Stem 导出：真实 true_peak_limiter（latency=243@48k）的跨轨对齐。"""

    def _sequence(self, fx_on_second: bool = True):
        from src.features.edit.core.effect_instance import EffectInstance
        from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack
        from src.shared.media.metadata import Rational

        def _clip(clip_id: str, with_fx: bool):
            effects = ()
            if with_fx:
                effects = (EffectInstance(
                    effect_type="true_peak_limiter",
                    params={"ceiling_dbtp": -1.0, "lookahead_ms": 5.0},
                    effect_id=f"fx_{clip_id}", order=0),)
            return ClipInstance(clip_id, "a", "T", 0, 25, 0, 25,
                                effects=effects)

        t_dry = EditTrack("T_dry", "D", "audio", 0,
                          clips=(_clip("c_dry", with_fx=False),))
        t_fx = EditTrack("T_fx", "F", "audio", 1,
                         clips=(_clip("c_fx", with_fx=fx_on_second),))
        return __import__("src.features.edit.core.timeline_v2", fromlist=[
            "TimelineSequence"]).TimelineSequence(
            fps=Rational(FPS, 1), audio_tracks=(t_dry, t_fx))

    def _decode_pulse(self, pulse_sample: int = PULSE_AT):
        def decode(track, clip, frame_idx, fps, num_samples):
            out = np.zeros((2, num_samples), dtype=np.float32)
            if frame_idx == 0 and pulse_sample < num_samples:
                out[:, pulse_sample] = AMP
            return out
        return decode

    def test_cross_track_alignment_in_stem(self):
        """挂链轨与干轨的脉冲在 Master stem 中同样本落点。"""
        from src.features.edit.core.stem_export import StemSpec, StemType
        from src.features.edit.core.audio_effects.true_peak_limiter import (
            TruePeakLimiterEffect,
        )
        seq = self._sequence()
        spec = StemSpec(StemType.MASTER, "Master", (), include_master=True)
        pcm = render_stem_pcm(seq, spec, decode_clip_fn=self._decode_pulse())
        assert pcm is not None
        # 声明延迟 = lookahead（resample_poly 零相位对齐，FIR 无净群延迟）
        probe = TruePeakLimiterEffect(float(SR))
        probe.set_params({"ceiling_dbtp": -1.0, "lookahead_ms": 5.0})
        lat = probe.latency_samples()
        assert _peak_position(pcm) == PULSE_AT + lat

    def test_dry_clip_alone_matches_shifted_position(self):
        """对照：两轨都干 → 脉冲无额外位移（PDC 零补偿，防过补）。"""
        from src.features.edit.core.stem_export import StemSpec, StemType
        seq = self._sequence(fx_on_second=False)
        spec = StemSpec(StemType.MASTER, "Master", (), include_master=True)
        pcm = render_stem_pcm(seq, spec, decode_clip_fn=self._decode_pulse())
        assert pcm is not None
        assert _peak_position(pcm) == PULSE_AT
