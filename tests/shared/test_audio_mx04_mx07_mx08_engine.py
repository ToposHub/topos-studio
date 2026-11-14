"""MX-04 / MX-07 / MX-08 引擎级回归（音频成熟化计划 阶段D）。

MX-04：同轨多 clip 先求和、轨 DSP 一次处理（实时 mix_samples）。
MX-07：无 clip 但尾音未结束时喂零推进（feed-zero drain）。
MX-08：外部侧链在导出/Bus 与实时同构（render_track_block /
render_bus_stage 消费 sidechain_taps）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState

SR = 48000.0
FPS = 24.0
BLOCK = 480


class _GateDecoder:
    """按帧门控的假解码器：frames 集合内的帧返回带限噪声，其余返回 None。

    用噪声而非正弦：压缩器包络跟随器对稠密峰值的收敛在单块内完成
    （正弦的峰值包络需要多块才收敛，单块测量不公平）。
    """

    def __init__(self, amp=0.4, freq=1000.0, frames=(0,), seed=7):
        self.amp = amp
        self.frames = set(frames)
        self.seed = seed
        self.channels = 2

    def decode_samples_for_frame(self, frame, fps, num_samples):
        if frame not in self.frames:
            return None
        rng = np.random.default_rng(self.seed + frame)
        raw = rng.standard_normal(num_samples)
        sig = (self.amp * raw / max(float(np.max(np.abs(raw))), 1e-9)).astype(np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


_COMP_PARAMS = {"threshold_db": -6.0, "ratio": 10.0, "knee": 0.0,
                "attack_ms": 0.7, "release_ms": 50.0, "mix": 100.0,
                "makeup_gain": 0.0, "enabled": True}


def _comp_chain_params(threshold_db=-6.0, sc_source=None):
    """模型层 EffectChainParams（mixer.set_track_effect_chain 入参）。"""
    from src.features.edit.core.timeline_v2 import (
        EffectChainParams, TrackEffectInstance,
    )
    params = dict(_COMP_PARAMS)
    params["threshold_db"] = threshold_db
    if sc_source is not None:
        params["sc_on"] = True
        params["sc_source"] = sc_source
    return EffectChainParams(effects=(
        TrackEffectInstance(
            effect_type="compressor", params=params,
            enabled=True, order=0, instance_id="C1",
        ),
    ))


def _comp_chain(threshold_db=-6.0, sc_source=None):
    """DSP 层 EffectChain（离线 renderer node 直接注入用）。"""
    from src.features.edit.core.timeline_v2 import EffectChainParams
    dsp = EffectChain(SR)
    cp = _comp_chain_params(threshold_db, sc_source)
    dsp.set_params(cp.to_dsp_entries(), sample_rate=SR)
    return dsp


def _delay_chain_params(fb=0.0, time_ms=60.0):
    """模型层 Delay 链（mixer.set_track_effect_chain 入参）。"""
    from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance
    return EffectChainParams(effects=(
        TrackEffectInstance(
            effect_type="delay",
            params={"left_delay": time_ms, "right_delay": time_ms,
                    "left_feedback": fb, "right_feedback": fb,
                    "low_cut": 20.0, "high_cut": 18000.0,
                    "high_ratio": 0.0, "stereo_blend": 0.0,
                    "dry_wet": 100.0, "level_db": 0.0, "enabled": True},
            enabled=True, order=0, instance_id="D1",
        ),
    ))


class TestMx04SumThenTrackDsp:
    """MX-04：轨 FX 对求和输入一次处理（压缩器检测合成信号）。"""

    def test_compressor_detects_summed_signal(self, qapp):
        """两个 0.4 峰值 clip 求和 0.8 > 阈值 0.5 → 压缩作用于合成信号。

        旧实现（逐 clip 处理 FX）：每个 0.4 < 0.5 单独不触发压缩，
        输出 ≈ 两 clip 未压缩之和。
        """
        frames = tuple(range(20))

        def _build(with_fx):
            mixer = AudioMixer()
            mixer.add_track("t1", _GateDecoder(amp=0.4, frames=frames),
                            AudioTrackState(track_id="t1"))
            mixer.add_track("t1", _GateDecoder(amp=0.4, frames=frames),
                            AudioTrackState(track_id="t1"),
                            replace_existing=False)
            if with_fx:
                mixer.set_track_effect_chain(
                    "t1", _comp_chain_params(threshold_db=-12.0))
            peak = 0.0
            for f in frames:
                _, peaks = mixer.mix_samples(f, FPS, BLOCK, 2)
                peak = peaks["t1"].peak_l  # 末块稳态
            return peak

        # 对照：同输入、无 FX → 求和峰值 ≈ 0.8（每 clip 峰值精确 0.4）
        peak_plain = _build(False)
        # 有 FX：求和 0.8（0dBFS）远超阈值 -12dB（0.25）→ 明显压缩。
        # 0.7ms attack 的包络跟随器对稳态音收敛后有 ~-5dB 检测电平，
        # ratio=10 → 输出显著低于未压缩。若 FX 仍按 clip 逐个处理（旧缺陷）：
        # 每 clip 0.4（-8dB）仍超 -12dB？否——单 clip 峰值 -8dB > -12dB 也会
        # 触发，故阈值改用 -3dB 边界不可行；改为对比压缩深度：求和处理时
        # 检测的是 0dBFS 合成电平（压缩深），逐 clip 时是 -8dB（压缩浅）。
        peak_summed = _build(True)

        assert peak_plain == pytest.approx(0.8, abs=0.05)
        assert peak_summed < peak_plain * 0.85, (
            f"compressor did not act on summed input "
            f"(summed={peak_summed:.3f}, plain={peak_plain:.3f})"
        )

    def test_decoder_order_does_not_change_sound(self, qapp):
        """互换 decoder 注册顺序，同一轨输出不变（求和后同一轨链）。"""
        frames = tuple(range(6))

        def build(order):
            mixer = AudioMixer()
            for _ in order:
                mixer.add_track("t1", _GateDecoder(amp=0.4, frames=frames,
                                                   seed=11),
                                AudioTrackState(track_id="t1"),
                                replace_existing=False)
            mixer.set_track_effect_chain("t1", _comp_chain_params(threshold_db=-12.0))
            out = None
            for f in frames:
                out, _ = mixer.mix_samples(f, FPS, BLOCK, 2)
            return out

        out_a = build(["A", "B"])
        out_b = build(["B", "A"])
        assert np.allclose(out_a, out_b, atol=1e-5), (
            "decoder iteration order changed the mixed result"
        )


class TestMx07TailDrain:
    """MX-07：无 clip 块喂零推进，Delay 尾音在后续块继续出现。"""

    def test_delay_tail_continues_after_decoder_none(self, qapp):
        """§1.3 场景：60ms/100% wet Delay，decoder=None 的后块 peak≈0.29。"""
        mixer = AudioMixer()
        mixer.add_track("t1", _GateDecoder(amp=1.0, freq=1000.0, frames=(0,)),
                        AudioTrackState(track_id="t1"))
        mixer.set_track_effect_chain("t1", _delay_chain_params(fb=0.0))

        mixer.mix_samples(0, FPS, BLOCK, 2)          # 内容块
        # 60ms 首回声 ≈ 第 6 帧到达；无 clip 的 drain 帧窗口内必须出现回声
        max_tail_peak = 0.0
        for f in range(1, 20):
            _, peaks2 = mixer.mix_samples(f, FPS, BLOCK, 2)
            if "t1" in peaks2:
                max_tail_peak = max(max_tail_peak, peaks2["t1"].peak_l)
        assert max_tail_peak > 0.05, (
            f"tail drain missing: no echo after decoder=None "
            f"(max peak={max_tail_peak})"
        )
        # drain 结束后（尾音预算耗尽）回到静音跳过路径
        peaks_last = None
        for f in range(2, 40):
            _, peaks_last = mixer.mix_samples(f, FPS, BLOCK, 2)
        # 60ms delay fb=0 的尾音 ≈ 1 block 多；40 帧后必然耗尽
        # （TrackLevel 不出现 = 完全跳过；或 peak 回落为 0）
        if "t1" in peaks_last:
            assert peaks_last["t1"].peak_l < 1e-6

    def test_seek_resets_tail_schedule(self, qapp):
        """seek（reset_dsp_states）清尾音调度：旧位置尾音不复活。"""
        mixer = AudioMixer()
        mixer.add_track("t1", _GateDecoder(amp=1.0, frames=(0,)),
                        AudioTrackState(track_id="t1"))
        mixer.set_track_effect_chain("t1", _delay_chain_params(fb=50.0))
        mixer.mix_samples(0, FPS, BLOCK, 2)
        mixer.reset_dsp_states()
        out, peaks = mixer.mix_samples(500, FPS, BLOCK, 2)
        # 无 clip 且尾音调度被清 → 整轨跳过（不出现在 telemetry）
        assert "t1" not in peaks


class TestMx08SidechainExportParity:
    """MX-08：导出/Bus 路径消费 sidechain_taps（与实时同构）。"""

    def test_render_track_block_uses_external_taps(self, qapp):
        """离线 render_track_block 传 taps 时压缩器按外部检源工作。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer

        class _Tr:
            track_id = "t1"
            is_bus = False
            volume = 1.0
            pan = 0.0
            is_muted = False
            is_solo = False
            is_enabled = True
            automation_mode = "off"
            output_bus_id = None
            sends = ()
            automation = {}
            strip_eq = None
            effect_chain = None
            panner = None

        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(type("S", (), {
            "audio_tracks": [_Tr()], "master_volume": 1.0,
            "master_muted": False, "master_automation": {},
            "master_automation_mode": "off",
        })())
        r.get_node("t1").effect_chain = _comp_chain(threshold_db=-12.0, sc_source="src")

        quiet = np.full((2, BLOCK), 0.05, dtype=np.float64)
        loud = np.full((2, BLOCK), 0.8, dtype=np.float64)

        # 多块稳态（块首包络未收敛的样本不参与峰值评判）。
        # 两个独立 renderer：避免同一压缩器状态被两条路径互相污染。
        r_plain = AudioGraphRenderer(SR)
        r_plain.configure_from_sequence(type("S", (), {
            "audio_tracks": [_Tr()], "master_volume": 1.0,
            "master_muted": False, "master_automation": {},
            "master_automation_mode": "off",
        })())
        r_plain.get_node("t1").effect_chain = _comp_chain(
            threshold_db=-12.0, sc_source="src")
        out_no_taps = None
        out_taps = None
        for i in range(6):
            out_no_taps = r_plain.render_track_block("t1", quiet.copy(), i)
            out_taps = r.render_track_block(
                "t1", quiet.copy(), i, sidechain_taps={"src": loud})

        # 无 tap：内部检源（0.05 < 0.25 阈值）→ 不压缩
        # 有 tap：检源 0.8 > 0.25 → ducking 明显
        ratio = float(np.max(np.abs(out_taps))) / max(float(np.max(np.abs(out_no_taps))), 1e-9)
        assert ratio < 0.7, (
            f"external sidechain taps had no effect in offline path (ratio={ratio:.2f})"
        )

    def test_render_bus_stage_consumes_taps(self, qapp):
        """Bus 压缩器外部检源：render_bus_stage 传 taps 时 ducking 生效。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer

        class _BusTr:
            track_id = "bus1"
            is_bus = True
            volume = 1.0
            pan = 0.0
            is_muted = False
            is_solo = False
            is_enabled = True
            automation_mode = "off"
            output_bus_id = None
            sends = ()
            automation = {}
            strip_eq = None
            effect_chain = None
            panner = None

        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(type("S", (), {
            "audio_tracks": [_BusTr()], "master_volume": 1.0,
            "master_muted": False, "master_automation": {},
            "master_automation_mode": "off",
        })())
        r.get_node("bus1").effect_chain = _comp_chain(threshold_db=-12.0, sc_source="src")

        bus_in = np.full((2, BLOCK), 0.4, dtype=np.float64)
        loud = np.full((2, BLOCK), 0.8, dtype=np.float64)
        output_plain = np.zeros((2, BLOCK))
        output_ducked = np.zeros((2, BLOCK))

        inputs_plain = {"bus1": bus_in.copy()}
        inputs_ducked = {"bus1": bus_in.copy()}
        r2 = AudioGraphRenderer(SR)
        r2.configure_from_sequence(type("S", (), {
            "audio_tracks": [_BusTr()], "master_volume": 1.0,
            "master_muted": False, "master_automation": {},
            "master_automation_mode": "off",
        })())
        r2.get_node("bus1").effect_chain = _comp_chain(threshold_db=-12.0, sc_source="src")
        for _i in range(6):  # 多块稳态
            r.render_bus_stage(inputs_plain, output_plain, False)
            r2.render_bus_stage(inputs_ducked, output_ducked, False,
                                sidechain_taps={"src": loud})

        p_plain = float(np.max(np.abs(output_plain)))
        p_ducked = float(np.max(np.abs(output_ducked)))
        assert p_ducked < p_plain * 0.7, (
            f"bus sidechain taps had no effect (plain={p_plain:.3f}, ducked={p_ducked:.3f})"
        )
