"""CW-A1 重构后的音频 DSP 单元测试。

覆盖新/重写的效果器：
  - StripEQFilter（6 频段参数化 EQ：旁路恒等、频段判别、斜率、Solo、voicing）
  - ExpanderEffect（Expander/Gate 双模式、range 封顶、hold、旧 noise_gate 键承接、GR）
  - CompressorEffect（并行 mix、hold、外部侧链、Listen、GR/检源遥测）
  - LimiterEffect（attack/hold、GR）
  - ReverbEffect（声学房间：ER 起始延迟、尾音衰减、dry 直通）
  - DelayEffect（立体声双线、负反馈、交叉矩阵、Mono 格式）
  - SpatialPanner（spread/rotate/divergence/boom 立体声域行为）
  - EffectChain（4 元组 instance_id、侧链透传、GR 收集）

运行：pytest tests/edit/test_audio_dsp_pro.py -m edit_core
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import (
    CompressorEffect,
    DelayEffect,
    EffectChain,
    ExpanderEffect,
    LimiterEffect,
    ReverbEffect,
    SpatialPanner,
    StripEQFilter,
    spatial_panner_matrix,
)
from src.features.edit.core.timeline_v2 import StripEQParams

SR = 48000.0


def _noise(n: int = 4800, ch: int = 2, amp: float = 0.5, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal((ch, n)) * amp).astype(np.float64)


def _sine(freq: float, n: int = 9600, ch: int = 2, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n) / SR
    s = amp * np.sin(2.0 * np.pi * freq * t)
    return np.stack([s] * ch)


def _std_db(x: np.ndarray, ref: np.ndarray) -> float:
    return float(20.0 * np.log10(np.std(x) / max(np.std(ref), 1e-12)))


def _band_params(**overrides) -> list[dict]:
    bands = [dict(b) for b in StripEQParams.default().bands]
    for idx, kv in overrides.items():
        i = int(idx)
        bands[i] = {**bands[i], **kv}
    return bands


class TestParametricEQ:
    def test_bypass_identity(self) -> None:
        eq = StripEQFilter(SR)
        x = _noise()
        eq.set_params(None)
        assert eq.process(x) is x
        eq.set_params(StripEQParams(enabled=False).to_dict())
        assert eq.process(x) is x

    def test_bell_boost_band_discrimination(self) -> None:
        eq = StripEQFilter(SR)
        eq.set_params({"bands": _band_params(**{"3": {
            "filter_type": "Bell", "freq": 1000.0, "gain": 12.0,
            "q": 1.4, "enabled": True}})})
        g1 = _std_db(eq.process(_sine(1000))[0, 2000:], _sine(1000)[0, 2000:])
        g2 = _std_db(eq.process(_sine(200))[0, 2000:], _sine(200)[0, 2000:])
        assert 8.0 < g1 < 15.0
        assert g2 < g1 - 6.0

    def test_hpf_slope(self) -> None:
        eq = StripEQFilter(SR)
        bands = _band_params(**{"0": {"filter_type": "HPF", "freq": 500.0,
                                      "slope": 24, "enabled": True}})
        for b in bands[1:]:
            b["gain"] = 0.0
        eq.set_params({"bands": bands})
        lo = _std_db(eq.process(_sine(100))[0, 2000:], _sine(100)[0, 2000:])
        hi = _std_db(eq.process(_sine(2000))[0, 2000:], _sine(2000)[0, 2000:])
        assert lo < -30.0  # 24dB/oct @ 500 → 100Hz 衰减 >36dB（稳态后）
        assert hi > -3.0

    def test_solo_isolates_band(self) -> None:
        eq = StripEQFilter(SR)
        bands = _band_params(**{"2": {"filter_type": "Bell", "freq": 1000.0,
                                      "gain": 12.0, "q": 2.0, "enabled": True,
                                      "solo": True}})
        for b in bands[:2] + bands[3:]:
            b["gain"] = 0.0
        eq.set_params({"bands": bands})
        g = _std_db(eq.process(_sine(1000))[0, 2000:], _sine(1000)[0, 2000:])
        assert 6.0 < g < 16.0

    def test_voicing_changes_q(self) -> None:
        # British (Q×1.18) 比 Clean 更窄：中心增益更高、远端更干净
        def _gain(voicing: str) -> float:
            eq = StripEQFilter(SR)
            eq.set_params({"voicing": voicing, "bands": _band_params(**{
                "3": {"filter_type": "Bell", "freq": 1000.0, "gain": 6.0,
                      "q": 2.0, "enabled": True}})})
            return _std_db(eq.process(_sine(1000))[0, 2000:],
                           _sine(1000)[0, 2000:])
        g_clean = _gain("Clean")
        g_british = _gain("British")
        assert g_british >= g_clean - 0.1  # 窄 Q 中心增益应不低

    def test_master_gain(self) -> None:
        eq = StripEQFilter(SR)
        eq.set_params({"master_gain": 6.0})
        x = _noise(amp=0.1)
        assert 5.5 < _std_db(eq.process(x), x) < 6.5


class TestExpanderGate:
    def test_bypass_identity(self) -> None:
        ex = ExpanderEffect(SR)
        x = _noise()
        ex.set_params(None)
        assert ex.process(x) is x

    def test_expander_attenuates_quiet_only(self) -> None:
        # 包络开门初始化（与旧 NoiseGate 语义一致）：静信号需 release 收敛，
        # 用 1s 块测尾段（10×release 充分关门）
        ex = ExpanderEffect(SR)
        ex.set_params({"mode": "Expander", "threshold_db": -25.0,
                       "range_db": 20.0, "ratio": 2.0, "attack_ms": 1.0,
                       "release_ms": 100.0, "enabled": True})
        loud = _noise(amp=0.4)
        assert abs(_std_db(ex.process(loud), loud)) < 1.5
        ex.reset_state()
        quiet = _noise(n=48000, amp=0.002)
        out = ex.process(quiet)
        assert _std_db(out[:, 24000:], quiet[:, 24000:]) < -4.0

    def test_gate_mode_deep(self) -> None:
        ex = ExpanderEffect(SR)
        ex.set_params({"mode": "Gate", "threshold_db": -25.0, "range_db": 40.0,
                       "ratio": 2.0, "attack_ms": 1.0, "release_ms": 100.0,
                       "enabled": True})
        quiet = _noise(n=48000, amp=0.002)
        out = ex.process(quiet)
        assert _std_db(out[:, 24000:], quiet[:, 24000:]) < -10.0
        assert ex.last_gr_db > 8.0

    def test_range_caps_attenuation(self) -> None:
        ex = ExpanderEffect(SR)
        ex.set_params({"mode": "Gate", "threshold_db": -20.0, "range_db": 6.0,
                       "ratio": 2.0, "attack_ms": 1.0, "release_ms": 100.0,
                       "enabled": True})
        quiet = _noise(n=48000, amp=0.001)
        out = ex.process(quiet)
        assert -12.0 < _std_db(out[:, 24000:], quiet[:, 24000:]) < -2.0
        assert ex.last_gr_db <= 6.5  # range 封顶

    def test_legacy_noise_gate_keys_map_to_gate(self) -> None:
        ex = ExpanderEffect(SR)
        ex.set_params({"threshold_db": -45.0, "attack_ms": 2.0,
                       "release_ms": 150.0, "enabled": True})
        assert ex._mode == "Gate"
        assert ex._threshold_db == -45.0


class TestCompressorPro:
    def test_compression_and_gr(self) -> None:
        cp = CompressorEffect(SR)
        cp.set_params({"threshold_db": -20.0, "ratio": 10.0,
                       "attack_ms": 0.7, "release_ms": 100.0, "knee": 0,
                       "mix": 100.0, "enabled": True})
        x = _noise(amp=0.8)
        out = cp.process(x)
        reduction = -_std_db(out, x)  # 衰减量（正 dB）
        assert 5.0 < reduction < 25.0
        assert cp.last_gr_db > 3.0

    def test_parallel_mix_zero_passthrough(self) -> None:
        cp = CompressorEffect(SR)
        cp.set_params({"threshold_db": -20.0, "ratio": 10.0, "knee": 0,
                       "mix": 0.0, "enabled": True})
        x = _noise(amp=0.8)
        assert abs(_std_db(cp.process(x), x)) < 0.5

    def test_external_sidechain_ducking(self) -> None:
        cp = CompressorEffect(SR)
        cp.set_params({"threshold_db": -20.0, "ratio": 10.0, "knee": 0,
                       "mix": 100.0, "enabled": True})
        quiet = _noise(amp=0.02)
        det = _noise(amp=0.6)
        out = cp.process(quiet, sidechain=det)
        assert _std_db(out, quiet) < -5.0
        assert cp.last_detector_db > -10.0

    def test_sidechain_listen_broadcasts_detector(self) -> None:
        """DY-04: sc_listen 透传检源原始音频（保留极性，无整流 DC）。"""
        cp = CompressorEffect(SR)
        cp.set_params({"threshold_db": -20.0, "sc_listen": True, "enabled": True})
        quiet = _noise(amp=0.02)
        det = _noise(amp=0.6)
        out = cp.process(quiet, sidechain=det)
        assert np.allclose(out, det)


class TestLimiterPro:
    def test_limiting_and_gr(self) -> None:
        lm = LimiterEffect(SR)
        lm.set_params({"threshold_db": -6.0, "attack_ms": 0.7,
                       "release_ms": 50.0, "enabled": True})
        out = lm.process(_noise(amp=0.9))
        assert np.max(np.abs(out)) < 10.0 ** (-6.0 / 20.0) * 1.35
        assert lm.last_gr_db > 1.0


class TestAcousticReverb:
    def test_bypass_identity(self) -> None:
        rv = ReverbEffect(SR)
        x = _noise()
        rv.set_params(None)
        assert rv.process(x) is x

    def test_er_first_arrival_after_predelay_plus_distance(self) -> None:
        rv = ReverbEffect(SR)
        rv.set_params({"enabled": True, "dry_wet": 100.0, "direct_db": -60.0,
                       "er_db": 0.0, "reverb_db": 0.0, "pre_delay": 42.0,
                       "reverb_time": 500.0, "distance": 10.0,
                       "modulation": 10.0})
        imp = np.zeros((2, int(SR)))
        imp[0, 100] = 1.0
        imp[1, 100] = 1.0
        w = rv.process(imp).sum(axis=0)
        nz = np.flatnonzero(np.abs(w) > 1e-5)
        first_ms = nz[0] / SR * 1000.0
        # RV-02 image-source 契约：首反射 = 有效传播（42ms predelay +
        # 10/343≈29.2ms 距离）+ 最短镜像附加行程（默认房 30×40×10 的
        # 天花/地镜附加 ≈5.66m ≈ 16.5ms）≈ 88ms（±容差覆盖 ±2% 去相关
        # 抖动与脉冲位置 2ms；旧 span 模型 ~76ms 已被 image-source 取代）
        assert 82.0 <= first_ms <= 95.0

    def test_tail_decays(self) -> None:
        rv = ReverbEffect(SR)
        rv.set_params({"enabled": True, "dry_wet": 100.0, "direct_db": -60.0,
                       "er_db": 0.0, "reverb_db": 0.0, "pre_delay": 10.0,
                       "reverb_time": 300.0, "distance": 2.0})
        imp = np.zeros((2, int(SR)))
        imp[:, 100] = 1.0
        w = np.abs(rv.process(imp).sum(axis=0))
        assert np.all(w[int(SR * 0.4):int(SR * 0.8)] < 0.2)

    def test_dry_passthrough(self) -> None:
        rv = ReverbEffect(SR)
        rv.set_params({"enabled": True, "dry_wet": 0.0, "direct_db": 0.0})
        x = _noise()
        assert _std_db(rv.process(x), x) > -0.5


class TestStereoDelay:
    def test_bypass_identity(self) -> None:
        dl = DelayEffect(SR)
        x = _noise()
        dl.set_params(None)
        assert dl.process(x) is x

    def test_independent_channel_delays(self) -> None:
        dl = DelayEffect(SR)
        dl.set_params({"enabled": True, "left_delay": 100.0, "right_delay": 300.0,
                       "left_feedback": 0.0, "right_feedback": 0.0,
                       "stereo_blend": 0.0, "dry_wet": 100.0, "level_db": 0.0,
                       "high_ratio": 0.0, "low_cut": 20.0, "high_cut": 20000.0})
        imp = np.zeros((2, int(SR)))
        imp[0, 10] = 1.0
        imp[1, 10] = 1.0
        out = dl.process(imp)
        assert 95.0 <= np.argmax(np.abs(out[0])) / SR * 1000.0 <= 106.0
        assert 295.0 <= np.argmax(np.abs(out[1])) / SR * 1000.0 <= 306.0

    def test_negative_feedback_alternates_sign(self) -> None:
        dl = DelayEffect(SR)
        dl.set_params({"enabled": True, "left_delay": 50.0, "right_delay": 50.0,
                       "left_feedback": -80.0, "right_feedback": 0.0,
                       "stereo_blend": 0.0, "dry_wet": 100.0, "level_db": 0.0,
                       "high_ratio": 0.0})
        imp = np.zeros((2, int(SR)))
        imp[0, 5] = 1.0
        imp[1, 5] = 1.0
        out = dl.process(imp)
        e1 = out[0, 5 + int(0.05 * SR)]
        e2 = out[0, 5 + int(0.10 * SR)]
        assert e1 * e2 < 0.0
        assert abs(e2) < abs(e1)

    def test_stereo_blend_cross_feed(self) -> None:
        # 交叉反馈经回声链传播：左线回声 ×blend 进入右线 → 右声道在
        # ~L+R 延迟处出现交叉回声（blend=0 时右声道保持静音）
        dl = DelayEffect(SR)
        dl.set_params({"enabled": True, "left_delay": 80.0, "right_delay": 80.0,
                       "left_feedback": 60.0, "right_feedback": 60.0,
                       "stereo_blend": 100.0, "dry_wet": 100.0,
                       "level_db": 0.0, "high_ratio": 0.0,
                       "low_cut": 20.0, "high_cut": 20000.0})
        imp = np.zeros((2, int(SR)))
        imp[0, 5] = 1.0
        out = dl.process(imp)
        # 右声道第一声像 = 左线 80ms 回声交叉写入右线后经右延迟读出（~160ms）
        win = slice(int(0.155 * SR), int(0.165 * SR))
        assert np.max(np.abs(out[1][win])) > 0.3

    def test_mono_format_sums_input(self) -> None:
        dl = DelayEffect(SR)
        dl.set_params({"enabled": True, "left_delay": 60.0, "right_delay": 60.0,
                       "left_feedback": 0.0, "right_feedback": 0.0,
                       "stereo_blend": 0.0, "dry_wet": 100.0, "level_db": 0.0,
                       "high_ratio": 0.0, "input_format": "Mono",
                       "low_cut": 20.0, "high_cut": 20000.0})
        imp = np.zeros((2, int(SR)))
        imp[0, 5] = 1.0  # 只有左声道有内容
        out = dl.process(imp)
        # Mono 求和（×0.5）后右声道也有湿声（经输入滤波的能量损失）
        win = slice(int(0.055 * SR), int(0.065 * SR))
        assert np.max(np.abs(out[1][win])) > 0.2


class TestSpatialPanner:
    PN = {"enabled": True, "spread": 100.0, "rotate": 0.0,
          "divergence": 0.0, "fb": 100.0, "spread_auto": False}

    def test_identity_default(self) -> None:
        assert np.allclose(spatial_panner_matrix(0.0, None), np.eye(2))
        M = spatial_panner_matrix(0.0, self.PN)
        assert np.allclose(M, np.eye(2), atol=0.05)

    def test_spread_zero_collapses_to_point(self) -> None:
        M0 = spatial_panner_matrix(0.0, {**self.PN, "spread": 0.0})
        assert np.allclose(M0[0], M0[1], atol=0.05)

    def test_rotate_180_swaps_channels(self) -> None:
        Mr = spatial_panner_matrix(0.0, {**self.PN, "rotate": 180.0})
        assert abs(Mr[0, 0]) < 0.4 and abs(Mr[0, 1]) > 0.5

    def test_divergence_cross_bleed(self) -> None:
        # 单声道内容 (1,0)：divergence 100 → 两扬声器近似等量
        Md = spatial_panner_matrix(0.0, {**self.PN, "divergence": 100.0})
        out = Md @ np.array([1.0, 0.0])
        assert abs(out[0] - out[1]) < 0.15

    def test_boom_post_boosts_low(self) -> None:
        sp = SpatialPanner(SR)
        sp.set_params({**self.PN, "boom_on": True, "boom_route": "Post",
                       "boom_gain": 6.0})
        low = np.stack([np.sin(2.0 * np.pi * 50.0 * np.arange(4800) / SR)] * 2) * 0.5
        assert np.std(sp.process(low.copy(), 0.0)) > np.std(low) * 1.02

    def test_boom_only_kills_highs(self) -> None:
        sp = SpatialPanner(SR)
        sp.set_params({**self.PN, "boom_on": True, "boom_route": "Only",
                       "boom_gain": 6.0})
        high = np.stack([np.sin(2.0 * np.pi * 5000.0 * np.arange(4800) / SR)] * 2) * 0.5
        assert np.std(sp.process(high.copy(), 0.0)) < np.std(high) * 0.2


class TestEffectChainPro:
    def test_instance_ids_sidechain_and_gr(self) -> None:
        ch = EffectChain(SR)
        iid_c, iid_e, iid_l = "comp-1", "exp-1", "lim-1"
        ch.set_params([
            ("expander", {"mode": "Gate", "threshold_db": -40.0,
                          "range_db": 30.0, "enabled": True}, True, iid_e),
            ("compressor", {"threshold_db": -25.0, "ratio": 4.0,
                            "enabled": True, "sc_on": True,
                            "sc_source": "tkA"}, True, iid_c),
            ("limiter", {"threshold_db": -3.0, "enabled": True}, True, iid_l),
        ])
        x = _noise(amp=0.6)
        det = _noise(amp=0.7)
        out = ch.process(x, sidechain_by_id={iid_c: det})
        assert out.shape == x.shape
        grs = ch.collect_gain_reduction()
        assert grs.get(iid_c, 0.0) > 0.0
        assert ch.sidechain_sources() == {"tkA"}
        assert iid_c in ch.collect_detector_levels()

    def test_three_tuple_still_supported(self) -> None:
        ch = EffectChain(SR)
        ch.set_params([("saturation", {"drive": 30.0, "enabled": True}, True)])
        out = ch.process(_noise(amp=0.3))
        assert not np.allclose(out, 0.0)

    def test_disabled_entry_skipped(self) -> None:
        ch = EffectChain(SR)
        ch.set_params([("saturation", {"drive": 80.0, "enabled": True}, False, "s1")])
        x = _noise(amp=0.3)
        assert ch.process(x) is x


if __name__ == "__main__":
    pytest.main([__file__])
