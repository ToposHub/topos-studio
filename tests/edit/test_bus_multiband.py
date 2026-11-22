"""P1 · Bus / Multiband Compressor（2026-11-19 计划第三阶段 #5）。

验收：等增益时合成响应平坦（LR4 和为全通）；跨带独立 envelope
（低段压缩不影响安静高段）；分块一致；旁路逐位；Bus 模板参数与
检源高通；链注册。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.bus_multiband import (
    BUS_COMP_TEMPLATES,
    BusCompEffect,
    MultibandCompressorEffect,
    _LR4Section,
)

RATE = 48000


def _tone(freq: float, amp: float, seconds: float = 1.5):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class TestLR4Crossover:
    def test_flat_sum_allpass(self) -> None:
        """等增益合成：low+high ≈ 原信号（LR4 全通和）。"""
        rng = np.random.default_rng(5)
        sig = (0.4 * rng.standard_normal(RATE)).astype(np.float32)
        pcm = np.stack([sig, sig])
        section = _LR4Section(180.0, RATE)
        low, high = section.split(pcm)
        summed = low + high
        # 跳过滤波器建立期。
        steady = slice(2000, None)
        np.testing.assert_allclose(
            summed[:, steady], pcm[:, steady], atol=2e-3)

    def test_block_consistency(self) -> None:
        rng = np.random.default_rng(6)
        pcm = np.stack(
            [(0.3 * rng.standard_normal(RATE)).astype(np.float32)] * 2)
        section = _LR4Section(180.0, RATE)
        low_a, high_a = section.split(pcm)
        section2 = _LR4Section(180.0, RATE)
        lows, highs = [], []
        for start in range(0, RATE, 7771):
            lo, hi = section2.split(pcm[:, start:start + 7771])
            lows.append(lo)
            highs.append(hi)
        np.testing.assert_allclose(
            low_a[:, 2000:], np.concatenate(lows, axis=1)[:, 2000:],
            atol=1e-4)


class TestMultibandCompressor:
    def test_loud_low_band_compressed_quiet_high_untouched(self) -> None:
        low = _tone(80.0, 0.6)
        high = _tone(9000.0, 0.05)
        pcm = np.stack([low + high] * 2)
        fx = MultibandCompressorEffect(RATE)
        fx.set_params({
            "crossovers": [400.0, 6000.0],
            "bands": [
                {"threshold_db": -14.0, "ratio": 4.0, "attack_ms": 5.0,
                 "release_ms": 120.0, "mix": 100.0, "makeup_db": 0.0},
                {"threshold_db": 0.0, "ratio": 1.5, "attack_ms": 5.0,
                 "release_ms": 120.0, "mix": 100.0, "makeup_db": 0.0},
                {"threshold_db": 0.0, "ratio": 1.5, "attack_ms": 5.0,
                 "release_ms": 120.0, "mix": 100.0, "makeup_db": 0.0},
            ]})
        out = fx.process(pcm.copy())
        steady = slice(RATE // 2, None)

        def band_amp(x, freq, bw=120.0):
            spec = np.abs(np.fft.rfft(x[0, steady] * np.hanning(
                x.shape[-1] - RATE // 2)))
            freqs = np.fft.rfftfreq(x.shape[-1] - RATE // 2, 1.0 / RATE)
            mask = np.abs(freqs - freq) <= bw
            return float(np.max(spec[mask]))

        low_cut = 20.0 * np.log10(band_amp(pcm, 80.0)
                                  / band_amp(out, 80.0))
        high_delta = abs(20.0 * np.log10(
            band_amp(out, 9000.0) / band_amp(pcm, 9000.0)))
        assert low_cut > 3.0          # 低段压缩
        assert high_delta < 1.5       # 高段（1.0001 比率）基本不动

    def test_bypass_bit_exact(self) -> None:
        pcm = np.stack([_tone(500.0, 0.2)] * 2)
        fx = MultibandCompressorEffect(RATE)
        fx.set_params({"enabled": False})
        np.testing.assert_array_equal(fx.process(pcm.copy()), pcm)

    def test_registered_in_chain(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain

        chain = EffectChain(float(RATE))
        chain.set_params([("multiband_comp", {"crossovers": [200.0, 5000.0]},
                           True)])
        pcm = np.stack([_tone(80.0, 0.5)] * 2)
        out = chain.process(pcm.copy())
        assert out.shape == pcm.shape
        assert float(np.max(np.abs(out))) > 0.05


class TestBusComp:
    def test_glue_templates_have_distinct_params(self) -> None:
        dx = BUS_COMP_TEMPLATES["dx_glue"]
        mx = BUS_COMP_TEMPLATES["mx_glue"]
        assert dx["ratio"] != mx["ratio"]
        assert dx["release_ms"] < mx["release_ms"]

    def test_sc_highpass_reduces_bass_triggering(self) -> None:
        """检源高通：低频鼓组能量不触发 ducking（对比无高通）。"""
        bass = _tone(50.0, 0.7)
        pcm = np.stack([bass] * 2)
        with_hpf = BusCompEffect(RATE)
        with_hpf.set_params({"template": "dx_glue", "sc_hpf_hz": 150.0})
        out_hpf = with_hpf.process(pcm.copy())
        no_hpf = BusCompEffect(RATE)
        no_hpf.set_params({"template": "dx_glue", "sc_hpf_hz": 0.0})
        out_flat = no_hpf.process(pcm.copy())
        steady = slice(RATE // 3, None)
        hpf_level = float(np.sqrt(np.mean(out_hpf[:, steady] ** 2)))
        flat_level = float(np.sqrt(np.mean(out_flat[:, steady] ** 2)))
        # 无高通时低频触发压缩（输出更低）；有高通则基本不受影响。
        assert flat_level < hpf_level

    def test_registered_in_chain(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain

        chain = EffectChain(float(RATE))
        chain.set_params([("bus_comp", {"template": "mx_glue"}, True)])
        pcm = np.stack([_tone(440.0, 0.5)] * 2)
        out = chain.process(pcm.copy())
        assert out.shape == pcm.shape


class TestMultibandFX04:
    """FX-04（2026-11-21 计划）：N 段、通道自适应、旋钮复用、别名。"""

    FLAT = {"threshold_db": 0.0, "ratio": 1.0, "attack_ms": 1.0,
            "release_ms": 10.0, "makeup_gain": 0.0}

    def _effect(self, crossovers, bands):
        fx = MultibandCompressorEffect(RATE)
        fx.set_params({"crossovers": list(crossovers),
                       "bands": [dict(b) for b in bands]})
        return fx

    @pytest.mark.parametrize("channels", [1, 2, 6])
    def test_flat_reconstruction_channels(self, channels) -> None:
        """mono/stereo/6ch：全平段重构精确（FX-04 合同 ≤1e-6）。"""
        rng = np.random.default_rng(3)
        x = (rng.uniform(-0.3, 0.3, (channels, 8000))).astype(np.float32)
        fx = self._effect([180.0, 4200.0], [self.FLAT] * 3)
        out = fx.process(x.copy())
        np.testing.assert_allclose(out, x, rtol=0, atol=1e-6)

    @pytest.mark.parametrize("count,crossovers", [
        (2, [300.0]), (3, [180.0, 4200.0]), (4, [150.0, 3000.0, 9000.0])])
    def test_flat_reconstruction_band_counts(self, count, crossovers) -> None:
        """2/3/4 段：全平段重构精确。"""
        rng = np.random.default_rng(4)
        x = (rng.uniform(-0.3, 0.3, (2, 8000))).astype(np.float32)
        fx = self._effect(crossovers, [self.FLAT] * count)
        out = fx.process(x.copy())
        np.testing.assert_allclose(out, x, rtol=0, atol=1e-6)
        assert fx.collect_telemetry()["band_count"] == count

    def test_knob_change_reuses_band_comps(self) -> None:
        """拓扑不变时旋钮提交复用 CompressorEffect 包络（不重建）。"""
        rng = np.random.default_rng(3)
        x = (rng.uniform(-0.3, 0.3, (2, 4000))).astype(np.float32)
        fx = self._effect([180.0, 4200.0], [dict(self.FLAT,
                                                 threshold_db=-24.0)] * 3)
        fx.process(x.copy())
        identity = id(fx._band_comps[1])
        fx.set_params({"bands": [dict(self.FLAT, threshold_db=-24.0,
                                      release_ms=120.0)] * 3})
        assert id(fx._band_comps[1]) == identity

    def test_makeup_db_alias_applies(self) -> None:
        """makeup_db（旧默认键）→ makeup_gain 别名：稳态 +6 dB。"""
        rng = np.random.default_rng(5)
        x = (rng.uniform(-0.3, 0.3, (2, 8000))).astype(np.float32)
        # 旧格式（仅 makeup_db，无 makeup_gain）——别名应生效。
        compressed = {"threshold_db": -24.0, "ratio": 2.0,
                      "attack_ms": 1.0, "release_ms": 10.0,
                      "makeup_db": 0.0}
        y0 = self._effect([180.0, 4200.0],
                          [compressed] * 3).process(x.copy())
        y6 = self._effect([180.0, 4200.0],
                          [dict(compressed, makeup_db=6.0)] * 3
                          ).process(x.copy())
        rms0 = float(np.sqrt(np.mean(y0[:, 2000:].astype(np.float64) ** 2)))
        rms6 = float(np.sqrt(np.mean(y6[:, 2000:].astype(np.float64) ** 2)))
        assert rms6 / rms0 == pytest.approx(2.0, rel=0.05)

    def test_mute_band_zeroed(self) -> None:
        """mute 段归零；其余段照常重构。"""
        rng = np.random.default_rng(6)
        x = (rng.uniform(-0.3, 0.3, (2, 8000))).astype(np.float32)
        y_all = self._effect([180.0, 4200.0],
                             [self.FLAT] * 3).process(x.copy())
        y_mute = self._effect([180.0, 4200.0],
                              [self.FLAT,
                               dict(self.FLAT, mute=True),
                               self.FLAT]).process(x.copy())
        assert float(np.max(np.abs(y_mute - y_all))) > 0.01

    def test_model_rejects_non_ascending_crossovers(self) -> None:
        """非法交叉点报错（计划合同）。"""
        from src.features.edit.core.timeline_v2 import MultibandCompParams
        with pytest.raises(ValueError):
            MultibandCompParams.from_dict({"crossovers": [5000.0, 200.0]})

    def test_model_band_count_round_trip(self) -> None:
        """2/3/4 段模型往返；makeup_db → makeup_gain 归一。"""
        from src.features.edit.core.timeline_v2 import MultibandCompParams
        for count, crossovers in ((2, [250.0]), (3, [180.0, 4200.0]),
                                  (4, [150.0, 3000.0, 9000.0])):
            params = MultibandCompParams.from_dict(
                {"crossovers": crossovers,
                 "bands": [{} for _ in range(count)]})
            restored = MultibandCompParams.from_dict(params.to_dict())
            assert restored.band_count == count
            assert len(restored.to_dict()["bands"]) == count
        aliased = MultibandCompParams.from_dict(
            {"crossovers": [180.0, 4200.0],
             "bands": [{"makeup_db": 6.0}, {}, {}]})
        assert aliased.band_low["makeup_gain"] == 6.0


class TestBusCompFX05:
    """FX-05（2026-11-21 计划）：连续参数覆盖模板 + makeup 别名生效。"""

    def test_knob_overrides_template(self) -> None:
        """显式连续字段覆盖模板值；未给字段沿用模板。"""
        fx = BusCompEffect(RATE)
        fx.set_params({"template": "dx_glue", "threshold_db": -30.0})
        assert fx._comp._threshold_db == -30.0      # 覆盖
        assert fx._comp._ratio == 2.5               # dx_glue 模板值
        assert fx._comp._makeup_db == 1.5           # 模板 makeup 别名生效

    def test_template_makeup_alias_effective(self) -> None:
        """模板 makeup_db（旧键）→ makeup_gain：补偿实际生效。"""
        rng = np.random.default_rng(3)
        x = (rng.uniform(-0.3, 0.3, (2, RATE))).astype(np.float32)
        fx0 = BusCompEffect(RATE)
        fx0.set_params({"template": "dx_glue", "makeup_gain": 0.0})
        y0 = fx0.process(x.copy())
        fx6 = BusCompEffect(RATE)
        fx6.set_params({"template": "dx_glue", "makeup_gain": 6.0})
        y6 = fx6.process(x.copy())
        rms0 = float(np.sqrt(np.mean(y0[:, RATE // 2:] ** 2)))
        rms6 = float(np.sqrt(np.mean(y6[:, RATE // 2:] ** 2)))
        assert 20 * np.log10(rms6 / rms0) == pytest.approx(6.0, abs=0.2)

    def test_hard_knee_transfer_curve(self) -> None:
        """硬膝传递曲线：GR = over·(1 − 1/ratio)（±0.2 dB，detector 校准解耦）。"""
        # 高频正弦（检测包络纹波最小）→ 传递曲线与纹波相位解耦。
        t = np.arange(2 * RATE) / RATE
        sine = (10 ** (-10.0 / 20.0)
                * np.sin(2 * np.pi * 12000.0 * t))[None, :].astype(np.float32)
        fx = BusCompEffect(RATE)
        fx.set_params({"threshold_db": -20.0, "ratio": 2.0, "knee": 0.0,
                       "mix": 100.0, "attack_ms": 1.0, "release_ms": 300.0,
                       "makeup_gain": 0.0})
        out = fx.process(sine.copy())
        measured_gr = -20.0 * np.log10(
            float(np.max(np.abs(out[0, RATE:])))
            / max(float(np.max(np.abs(sine[0, RATE:]))), 1e-9))
        detector_db = float(fx._comp.last_detector_db)
        over = detector_db - (-20.0)
        theoretical = max(0.0, over) * (1.0 - 1.0 / 2.0)
        assert measured_gr == pytest.approx(theoretical, abs=0.2)

    def test_sc_hpf_keeps_bass_in_output(self) -> None:
        """SC 高通只作用检测支路：输出低频保留，触发减少。"""
        t = np.arange(2 * RATE) / RATE
        bass = (0.4 * np.sin(2 * np.pi * 50.0 * t))[None, :].astype(
            np.float32)
        hi = (0.05 * np.sin(2 * np.pi * 1000.0 * t))[None, :].astype(
            np.float32)
        mix_sig = (bass + hi).astype(np.float32)
        common = {"threshold_db": -18.0, "ratio": 3.0, "knee": 0.0,
                  "mix": 100.0, "attack_ms": 3.0, "release_ms": 50.0}
        o_off = BusCompEffect(RATE).set_params(
            dict(common, sc_hpf_hz=0.0)) or BusCompEffect(RATE)
        o_off = BusCompEffect(RATE)
        o_off.set_params(dict(common, sc_hpf_hz=0.0))
        y_off = o_off.process(mix_sig.copy())
        o_on = BusCompEffect(RATE)
        o_on.set_params(dict(common, sc_hpf_hz=120.0))
        y_on = o_on.process(mix_sig.copy())
        bass_off = float(np.max(np.abs(y_off[0, RATE:])))
        bass_on = float(np.max(np.abs(y_on[0, RATE:])))
        # 输出未被直接高通（低频保留）+ HPF 减少触发 → bass 幅度不降反升。
        assert bass_on >= bass_off * 0.9
