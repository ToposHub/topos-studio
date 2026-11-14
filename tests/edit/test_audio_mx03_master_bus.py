"""MX-03 回归：Master/Bus 处理与遥测闭环（音频成熟化计划 阶段F）。

覆盖：
- 模型：TimelineSequence master_strip_eq / master_effect_chain 序列化往返、
  坏字段降级、已知键（不落入 extras）。
- 命令：SetMasterStripEQCommand / SetMasterEffectChainCommand apply/undo/往返。
- 实时：Master EQ/FX 在音量后限幅器前生效；Master 遥测（"-1" 键）含
  taps/GR/SC；Bus 遥测补 pre/GR/taps；Bus 链外部侧链实时消费（此前被
  无声忽略——MX-08 实时残留缺口）；Master 链外部侧链实时消费。
- 离线：AudioGraphRenderer.configure_from_sequence 构建 Master 条带并经
  apply_master 应用（导出继承同一代码路径）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.timeline_v2 import (
    DelayParams,
    EffectChainParams,
    StripEQParams,
    TimelineSequence,
    TrackEffectInstance,
)

SR = 48000.0
FPS = 24.0
BLOCK = 480


def _eq_params():
    return StripEQParams(enabled=True, bands=[dict({
        "enabled": True, "solo": False, "index": 1,
        "gain": 0.0, "q": 1.1, "slope": 12,
        "filter_type": "HPF", "freq": 2000.0,
    })])


def _delay_chain(instance_id="MD1", time_ms=60.0):
    return EffectChainParams(effects=(
        TrackEffectInstance(
            effect_type="delay",
            params=DelayParams(left_delay=time_ms, right_delay=time_ms,
                               dry_wet=100.0).to_dict(),
            enabled=True, order=0, instance_id=instance_id,
        ),
    ))


def _comp_chain(instance_id="C1", threshold_db=-12.0, sc_source=None):
    params = {"threshold_db": threshold_db, "ratio": 10.0, "knee": 0.0,
              "attack_ms": 0.7, "release_ms": 50.0, "mix": 100.0,
              "makeup_gain": 0.0, "enabled": True}
    if sc_source is not None:
        params["sc_on"] = True
        params["sc_source"] = sc_source
    return EffectChainParams(effects=(
        TrackEffectInstance(
            effect_type="compressor", params=params,
            enabled=True, order=0, instance_id=instance_id,
        ),
    ))


class _ToneDecoder:
    """按帧门控的正弦解码器（frames 外返回 None）。"""

    def __init__(self, amp=0.3, freq=110.0, frames=(0,), channels=2):
        self.amp = amp
        self.freq = freq
        self.frames = set(frames)
        self.channels = channels

    def decode_samples_for_frame(self, frame, fps, num_samples):
        if frame not in self.frames:
            return None
        t = (np.arange(num_samples) + frame * num_samples) / SR
        sig = (self.amp * np.sin(2.0 * np.pi * self.freq * t)).astype(np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


# ═══════════════════════════════════════════════════════════════════════
# 模型与命令
# ═══════════════════════════════════════════════════════════════════════

class TestMx03Model:
    def test_master_strip_fields_roundtrip(self):
        seq = TimelineSequence(name="M")
        seq = seq.with_master_strip_eq(_eq_params())
        seq = seq.with_master_effect_chain(_delay_chain())
        d = seq.to_dict()
        assert d["master_strip_eq"] is not None
        assert d["master_effect_chain"] is not None
        rt = TimelineSequence.from_dict(d)
        assert rt.master_strip_eq is not None
        # from_dict 会把 bands 规范化补全到标准 6 段——语义比较而非逐字节
        assert rt.master_strip_eq.enabled is True
        first = rt.master_strip_eq.bands[0]
        assert first["filter_type"] == "HPF" and first["freq"] == 2000.0
        assert rt.master_effect_chain is not None
        assert len(rt.master_effect_chain.effects) == 1

    def test_master_fields_are_known_keys(self):
        """新字段不落入 _extra_fields（未知键保留机制不被误触）。"""
        seq = TimelineSequence(name="M").with_master_strip_eq(_eq_params())
        assert seq.to_dict().get("_extra_fields", {}) == {} or \
            not seq._extra_fields

    def test_corrupt_master_fields_degrade_to_none(self):
        d = TimelineSequence(name="M").to_dict()
        d["master_strip_eq"] = "not-a-dict"
        d["master_effect_chain"] = 42
        rt = TimelineSequence.from_dict(d)
        assert rt.master_strip_eq is None
        assert rt.master_effect_chain is None

    def test_commands_apply_undo_and_serialize(self):
        from src.features.edit.core.commands import (
            SetMasterEffectChainCommand,
            SetMasterStripEQCommand,
        )
        seq = TimelineSequence(name="M")
        cmd = SetMasterStripEQCommand.create(_eq_params())
        seq2, applied = cmd.apply(seq)
        assert seq2.master_strip_eq is not None
        seq3 = applied.undo(seq2)
        assert seq3.master_strip_eq is None

        chain = _delay_chain()
        cmd2 = SetMasterEffectChainCommand.create(chain)
        seq4, applied2 = cmd2.apply(seq3)
        assert seq4.master_effect_chain is not None
        rt = SetMasterEffectChainCommand.from_dict(applied2.to_dict())
        assert rt.undo(seq4).master_effect_chain is None

        rt_eq = SetMasterStripEQCommand.from_dict(
            SetMasterStripEQCommand.create(_eq_params()).to_dict())
        assert rt_eq.strip_eq is not None


# ═══════════════════════════════════════════════════════════════════════
# 实时引擎
# ═══════════════════════════════════════════════════════════════════════

class TestMx03RealtimeMasterStrip:
    def test_master_eq_attenuates_below_cutoff(self, qapp):
        """Master HPF@2kHz：110Hz 正弦被显著衰减（无 EQ 时直通）。"""
        frames = tuple(range(24))

        def _peak(with_eq):
            mixer = AudioMixer()
            mixer.set_sample_rate(SR)
            mixer.add_track("t1", _ToneDecoder(0.3, 110.0, frames),
                            AudioTrackState(track_id="t1"))
            if with_eq:
                mixer.set_master_strip_eq(_eq_params())
            pk = 0.0
            for f in frames:
                out, _ = mixer.mix_samples(f, FPS, BLOCK, 2)
                if out.shape[-1] > 0:
                    pk = max(pk, float(np.max(np.abs(out))))
            return pk

        plain = _peak(False)
        filtered = _peak(True)
        # HPF 12dB/oct @2kHz 对 110Hz ≈ 5.1 oct → 60dB+ 衰减；
        # 判据用 >20dB 保证跨实现稳健
        assert plain > 0.25
        assert filtered < plain * 10 ** (-20 / 20)

    def test_master_fx_delay_tail_and_telemetry(self, qapp):
        """Master Delay：脉冲后无声块出现回声；payload 键 "-1" 携带实例 tap。"""
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)
        mixer.add_track("t1", _ToneDecoder(0.3, 110.0, frames=(0,)),
                        AudioTrackState(track_id="t1"))
        mixer.set_master_effect_chain(_delay_chain("MD1", 60.0))

        echo_peak = 0.0
        master_taps = None
        for f in range(0, 8):
            out, peaks = mixer.mix_samples(f, FPS, BLOCK, 2)
            if f >= 2:  # 60ms ≈ 1.5 帧；回声在帧 2-3 到达
                echo_peak = max(echo_peak, float(np.max(np.abs(out))))
            lvl = peaks.get("-1")
            if lvl is not None and lvl.taps:
                master_taps = lvl.taps
        # 100% wet：帧 0 无输出（回声未到），帧 2+ 出现回声
        assert echo_peak > 0.005, "master delay echo missing after impulse"
        assert master_taps is not None and "MD1" in master_taps

    def test_master_chain_external_sidechain(self, qapp):
        """Master 压缩器外部检源：master 程序信号 -26dB 低于阈值（内部检源
        路径不可能产生 GR）；检源轨响（路由进静音 Bus，不进 master 混音）
        → 仅凭外部检源 GR > 0。"""
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)
        frames = tuple(range(10))
        # 静音 Bus：吞掉检源轨的可听输出（Bus 静音不阻断轨阶段 tap 记录）
        mixer.add_track("dead", _ToneDecoder(0.0, 220.0, ()),
                        AudioTrackState(track_id="dead", muted=True))
        mixer.set_track_routing("dead", None, [], is_bus=True)
        mixer.add_track("src", _ToneDecoder(0.5, 220.0, frames),
                        AudioTrackState(track_id="src"))
        mixer.set_track_routing("src", "dead", [], is_bus=False)
        # master 程序信号：-26dB，远低于 -12dB 阈值
        mixer.add_track("mprog", _ToneDecoder(0.05, 220.0, frames),
                        AudioTrackState(track_id="mprog"))
        mixer.set_master_effect_chain(_comp_chain("MC1", -12.0, sc_source="src"))

        gr_db = 0.0
        for f in frames:
            _, peaks = mixer.mix_samples(f, FPS, BLOCK, 2)
            lvl = peaks.get("-1")
            if lvl is not None:
                gr_db = max(gr_db, float(lvl.gr.get("MC1", 0.0)))
        assert gr_db > 0.5, (
            f"master chain external SC not consumed (GR={gr_db:.2f}dB)")


class TestMx03BusTelemetryAndSidechain:
    def test_bus_chain_external_sidechain_realtime(self, qapp):
        """Bus 压缩器外部检源：程序信号 -26dB 低于阈值不压缩；外部检源
        -6dB 高于阈值 → GR > 0。旧实现实时 Bus 链不透传检源 → GR 恒 0。
        """
        def _gr(with_sc):
            mixer = AudioMixer()
            mixer.set_sample_rate(SR)
            frames = tuple(range(12))
            mixer.add_track("dead", _ToneDecoder(0.0, 220.0, ()),
                            AudioTrackState(track_id="dead", muted=True))
            mixer.set_track_routing("dead", None, [], is_bus=True)
            mixer.add_track("prog", _ToneDecoder(0.05, 220.0, frames),
                            AudioTrackState(track_id="prog"))
            mixer.set_track_routing("prog", "bus1", [], is_bus=False)
            mixer.add_track("bus1", _ToneDecoder(0.0, 220.0, ()),
                            AudioTrackState(track_id="bus1"))
            mixer.set_track_routing("bus1", None, [], is_bus=True)
            mixer.set_track_effect_chain(
                "bus1",
                _comp_chain("BC1", -12.0, sc_source="src" if with_sc else None))
            mixer.add_track("src", _ToneDecoder(0.5, 300.0, frames),
                            AudioTrackState(track_id="src"))
            mixer.set_track_routing("src", "dead", [], is_bus=False)
            gr = 0.0
            for f in frames:
                _, peaks = mixer.mix_samples(f, FPS, BLOCK, 2)
                lvl = peaks.get("bus1")
                if lvl is not None:
                    gr = max(gr, float(lvl.gr.get("BC1", 0.0)))
            return gr

        assert _gr(False) < 0.1, "program -26dB below -12dB must not compress"
        assert _gr(True) > 0.5, "bus chain external SC ignored (realtime gap)"

    def test_bus_telemetry_carries_pre_gr_taps(self, qapp):
        """Bus TrackLevel 带 pre_peak/GR/taps（与普通轨同一契约）。"""
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)
        frames = tuple(range(10))
        mixer.add_track("prog", _ToneDecoder(0.5, 220.0, frames),
                        AudioTrackState(track_id="prog"))
        mixer.set_track_routing("prog", "bus1", [], is_bus=False)
        mixer.add_track("bus1", _ToneDecoder(0.0, 220.0, ()),
                        AudioTrackState(track_id="bus1"))
        mixer.set_track_routing("bus1", None, [], is_bus=True)
        mixer.set_track_effect_chain("bus1", _comp_chain("BC1", -12.0))

        saw = None
        for f in frames:
            _, peaks = mixer.mix_samples(f, FPS, BLOCK, 2)
            lvl = peaks.get("bus1")
            if lvl is not None:
                saw = lvl
        assert saw is not None
        assert saw.pre_peak > 0.4, "bus pre-fader peak missing"
        assert saw.gr.get("BC1", 0.0) > 0.5, "bus GR missing"
        assert "BC1" in (saw.taps or {}), "bus instance taps missing"


# ═══════════════════════════════════════════════════════════════════════
# 离线渲染器（导出同一代码路径）
# ═══════════════════════════════════════════════════════════════════════

class TestMx03RendererMasterStrip:
    def test_renderer_applies_master_eq_and_fx(self):
        """configure_from_sequence 读 master_strip_eq/master_effect_chain；
        apply_master 在限幅器前应用（高进正弦过 HPF 衰减 + Delay 出回声）。
        """
        seq = TimelineSequence(name="M")
        seq = seq.with_master_strip_eq(_eq_params())
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        assert renderer._master_eq is not None

        n = 2048
        t = np.arange(n) / SR
        x = np.stack([0.3 * np.sin(2 * np.pi * 110.0 * t)] * 2
                     ).astype(np.float32)
        out = renderer.apply_master(np.array(x, copy=True), start_frame=0,
                                    fps=FPS)
        assert float(np.max(np.abs(out))) < 0.3 * 10 ** (-20 / 20)

    def test_renderer_master_delay_echo(self):
        seq = TimelineSequence(name="M").with_master_effect_chain(
            _delay_chain("MD1", 60.0))
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        assert renderer._master_chain is not None

        n = 480
        x = np.zeros((2, n), dtype=np.float32)
        x[:, 0] = 0.5  # 脉冲（100% wet → 输出帧 0 静默）
        out = renderer.apply_master(x, start_frame=0, fps=FPS)
        assert float(np.max(np.abs(out))) < 1e-6
        # 喂零块：60ms ≈ 2880 样本；第 6 块（2880..3360）出现回声
        echo = 0.0
        for blk in range(1, 8):
            z = np.zeros((2, n), dtype=np.float32)
            out = renderer.apply_master(z, start_frame=blk, fps=FPS)
            echo = max(echo, float(np.max(np.abs(out))))
        assert echo > 0.005, "master delay echo missing in renderer"

    def test_renderer_master_sidechain_sources(self):
        seq = TimelineSequence(name="M").with_master_effect_chain(
            _comp_chain("MC1", -12.0, sc_source="src"))
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        assert renderer.master_sidechain_sources() == {"src"}

    def test_renderer_without_master_strip_passthrough(self):
        """旧序列（无 master 字段）→ 条带为 None，apply_master 直通（音量 1）。"""
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(TimelineSequence(name="M"))
        assert renderer._master_eq is None
        assert renderer._master_chain is None
        x = np.full((2, 64), 0.3, dtype=np.float32)
        out = renderer.apply_master(x.copy(), start_frame=0, fps=FPS)
        assert np.allclose(out, x, atol=0.01)
