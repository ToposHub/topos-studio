"""音频成熟化计划 §1.3 已数值复现缺陷的 focused 回归（Phase A 证据冻结）。

每条测试对应计划表一个 ID，先以"暴露原始缺陷"的断言失败，
修复后转为守护回归。全部纯 DSP 层，不需要 Qt / 音频设备。

来源：docs/plans/2026-11-14-audio-mixer-fx-maturity-plan.md §1.3
  - EQ-01: HPF/LPF 标称斜率与实际传输一致（6/12/18/24 dB/oct）
  - EQ-01: Shelf Q 有效控制滤波器（或 UI 不承诺该参数）
  - RV-01: Brightness 运行中改变阻尼且不清空尾音
  - PAN-01: Panner 矩阵输出由未污染输入计算
  - DY-02: Expander 标准 (ratio-1) 下扩斜率，Range 封顶
  - DY-02: Compressor hard-knee 传输 oracle
  - DY-04: Sidechain Listen 监听原始检源（保留极性）
  - MX-06: EffectChain 按 instance_id 复用状态 / 旁路 GR 清零
  - MX-07/DL-03: Delay 尾音在无输入块继续推进
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    CompressorEffect,
    DelayEffect,
    EffectChain,
    ExpanderEffect,
    ReverbEffect,
    SpatialPanner,
    StripEQFilter,
    spatial_panner_matrix,
)
from src.features.edit.core.timeline_v2 import StripEQParams

SR = 48000.0


def _impulse_response(effect, n=16384, channels=2):
    """冲激响应（magnitude dB 频谱）——经真实 process() 路径。"""
    x = np.zeros((channels, n), dtype=np.float64)
    x[0, 0] = 1.0
    y = effect.process(x)
    spec = np.fft.rfft(y[0])
    return np.abs(spec) * 2.0 / 1.0  # 线性幅度谱


def _mag_db_at(spec_db_freqs, f_hz, sr=SR):
    freqs, mag_db = spec_db_freqs
    idx = int(np.argmin(np.abs(freqs - f_hz)))
    return mag_db[idx]


def _spectrum_db(signal, n=16384, sr=SR):
    # 冲激响应的频谱幅度 = |FFT|（无单边谱 ×2 补偿——那是正弦幅度的约定）
    spec = np.abs(np.fft.rfft(signal))
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    return freqs, 20.0 * np.log10(np.maximum(spec, 1e-12))


# =============================================================================
# EQ-01：滤波斜率
# =============================================================================

def _make_eq_band(band_dict):
    return StripEQParams(
        enabled=True,
        bands=[dict({"enabled": True, "solo": False, "index": 1,
                     "gain": 0.0, "q": 1.1, "slope": 12}, **band_dict)],
    )


class TestEQHPFSlope:
    """EQ-01 P0：HPF slope 6/12/18/24 应产生 N 阶 Butterworth（N=slope/6）。

    缺陷复现：旧实现级联 `slope//6` 个相同 Q=0.7071 的二阶 biquad，
    实际斜率 = 2×标称（12/24/36/48 dB/oct），截止处 -3/-6/-9/-12 dB。
    """

    PARAMS = [(6, 12.0), (12, 24.0), (18, 36.0), (24, 48.0)]  # (标称, 旧缺陷值)

    @pytest.mark.parametrize("slope", [6, 12, 18, 24])
    def test_far_region_slope_matches_nominal(self, slope):
        """远低于截止区间每倍频程衰减 ≈ 标称斜率（±0.5dB/oct）。"""
        eq = StripEQFilter(SR)
        eq.set_params(_make_eq_band({
            "filter_type": "HPF", "freq": 1000.0, "slope": slope,
            "gain": 0.0, "q": 1.1,
        }))
        n = 16384
        x = np.zeros((2, n), dtype=np.float64)
        x[0, 0] = 1.0
        y = eq.process(x)
        freqs, mag_db = _spectrum_db(y[0], n=n)
        # 测量点远离截止（125Hz 与 250Hz，均在 -18dB 以下区域）
        m125 = _mag_db_at((freqs, mag_db), 125.0)
        m250 = _mag_db_at((freqs, mag_db), 250.0)
        slope_measured = m250 - m125  # 每 +1 octave 的增量（应为正，越低越衰减）
        # 期望：6dB/oct 滤波器每倍频程升 6dB
        assert slope_measured == pytest.approx(float(slope), abs=0.5), (
            f"HPF slope={slope}: measured {slope_measured:.2f} dB/oct, "
            f"expected {slope} ±0.5"
        )

    @pytest.mark.parametrize("slope", [6, 12, 18, 24])
    def test_cutoff_minus_3db(self, slope):
        """Butterworth 截止频率处衰减 ≈ -3.01 dB（±0.5）。"""
        eq = StripEQFilter(SR)
        eq.set_params(_make_eq_band({
            "filter_type": "HPF", "freq": 1000.0, "slope": slope,
            "gain": 0.0, "q": 1.1,
        }))
        n = 16384
        x = np.zeros((2, n), dtype=np.float64)
        x[0, 0] = 1.0
        y = eq.process(x)
        freqs, mag_db = _spectrum_db(y[0], n=n)
        m1000 = _mag_db_at((freqs, mag_db), 1000.0)
        assert m1000 == pytest.approx(-3.01, abs=0.5), (
            f"HPF slope={slope} @cutoff: {m1000:.2f} dB, expected -3.01±0.5"
        )


class TestEQShelfQ:
    """EQ-01 P0：Shelf 的 Q 必须有效控制滤波器传输。

    缺陷复现：Q 0.3→10.3 输出最大差 0（_biquad_lowshelf 不消费 Q）。
    """

    def test_shelf_q_changes_response(self):
        specs = []
        for q in (0.3, 10.3):
            eq = StripEQFilter(SR)
            eq.set_params(_make_eq_band({
                "filter_type": "Low Shelf", "freq": 200.0,
                "gain": 6.0, "q": q,
            }))
            n = 16384
            x = np.zeros((2, n), dtype=np.float64)
            x[0, 0] = 1.0
            y = eq.process(x)
            specs.append(_spectrum_db(y[0], n=n))
        d = np.abs(specs[0][1] - specs[1][1])
        assert float(np.max(d)) > 0.5, (
            "Shelf Q 0.3 vs 10.3 produced identical transfer (Q ignored)"
        )

    def test_shelf_q_extremes_finite(self):
        """Q 极端值不产生 NaN/Inf。"""
        for q in (0.1, 10.0):
            eq = StripEQFilter(SR)
            eq.set_params(_make_eq_band({
                "filter_type": "High Shelf", "freq": 4000.0,
                "gain": -4.0, "q": q,
            }))
            rng = np.random.default_rng(7)
            x = rng.standard_normal((2, 4096)) * 0.1
            y = eq.process(x)
            assert np.all(np.isfinite(y))


# =============================================================================
# RV-01：Brightness 就地阻尼
# =============================================================================

class TestReverbBrightness:
    """RV-01 P0：Brightness 改变必须实际改变运行中阻尼，且不清空尾音。

    缺陷复现：brightness 50→100 两实例输出最大差 0
    （comb 阻尼只在 _rebuild → _apply_fdn_params 更新，
    set_params 只把 brightness 归入 tone_changed → _rebuild_tone）。
    """

    def _params(self, brightness):
        return {
            "height": 10.0, "length": 30.0, "width": 40.0,
            "pre_delay": 20.0, "reverb_time": 2000.0, "distance": 10.0,
            "brightness": brightness, "modulation": 0.0,
            "dry_wet": 100.0, "direct_db": -60.0, "er_db": -60.0,
            "reverb_db": 0.0, "enabled": True,
        }

    def test_brightness_hot_update_changes_output(self):
        """运行中 brightness 50→100（几何不变）必须改变阻尼与输出。

        缺陷复现路径：set_params 把 brightness 归入 tone_changed →
        只重建 shelf，comb 阻尼（_apply_fdn_params）不更新 → 输出差 0。
        """
        rng = np.random.default_rng(42)
        x = rng.standard_normal((2, 48000)) * 0.2
        # rvA 一直在 50
        rv_a = ReverbEffect(SR)
        rv_a.set_params(self._params(50.0))
        out_a = rv_a.process(x.copy())
        # rvB 运行中从 50 改到 100（几何参数不变）
        rv_b = ReverbEffect(SR)
        rv_b.set_params(self._params(50.0))
        rv_b.process(x.copy())  # 建立运行状态
        rv_b.set_params(self._params(100.0))
        out_b = rv_b.process(x.copy())
        d = float(np.max(np.abs(out_a - out_b)))
        assert d > 1e-3, (
            f"runtime brightness 50→100 produced max diff {d} "
            "(comb damping not hot-updated)"
        )

    def test_brightness_hot_update_preserves_tail(self):
        """运行中改变 brightness：尾音不归零、不清空（无 _rebuild）。"""
        rng = np.random.default_rng(11)
        rv = ReverbEffect(SR)
        rv.set_params(self._params(50.0))
        x = rng.standard_normal((2, 48000)) * 0.2
        rv.process(x)  # 建立尾音
        # 就地改 brightness（几何不变）
        p = self._params(100.0)
        rv.set_params(p)
        tail_after = rv.process(np.zeros((2, 48000)))
        peak_tail = float(np.max(np.abs(tail_after)))
        assert peak_tail > 1e-3, (
            "brightness hot-update cleared the reverb tail (network rebuilt)"
        )


# =============================================================================
# PAN-01：矩阵别名
# =============================================================================

class TestPannerMatrixAlias:
    """PAN-01 P0：两路输出必须由未污染的原输入计算。

    缺陷复现：out[0] 先被覆写，out[1] 的计算读到污染后的 out[0]
    （0.4 幅度输入与正确矩阵乘法最大差 0.1465）。
    """

    def test_output_matches_matrix_oracle(self):
        rng = np.random.default_rng(3)
        x = rng.standard_normal((2, 512)) * 0.4
        panner_dict = {
            "enabled": True, "spread": 100.0, "rotate": 30.0,
            "divergence": 40.0, "fb": 50.0, "spread_auto": False,
        }
        pan = 0.3
        sp = SpatialPanner(SR)
        sp.set_params(panner_dict)
        y = sp.process(x.copy(), pan)
        m = spatial_panner_matrix(pan, panner_dict)
        expected = m @ x
        err = float(np.max(np.abs(y - expected)))
        assert err <= 1e-6, f"panner output deviates from M@input by {err}"

    def test_symmetric_input_mirror_symmetry(self):
        """左右对称输入产生镜像对称输出（旋转=0、divergence=0）。"""
        x = np.zeros((2, 256))
        x[0] = 0.5
        x[1] = 0.5
        panner_dict = {"enabled": True, "spread": 100.0, "rotate": 0.0,
                       "divergence": 0.0, "fb": 100.0, "spread_auto": False}
        sp = SpatialPanner(SR)
        sp.set_params(panner_dict)
        y = sp.process(x, 0.0)
        assert np.allclose(y[0], y[1], atol=1e-9)


# =============================================================================
# DY-02：Expander 下扩斜率与 Compressor oracle
# =============================================================================

class TestExpanderSlope:
    """DY-02 P0：标准 1:ratio 下扩 = 每低于阈值 1dB 额外衰减 (ratio-1) dB。

    缺陷复现：threshold=-20、input=-40、ratio=2 只衰减 10dB
    （用了 (1-1/ratio) 斜率），标准应为 20dB。
    """

    def _steady_output_peak_db(self, ratio, range_db, input_dbfs=-40.0,
                               threshold_db=-20.0, seconds=1.0):
        ex = ExpanderEffect(SR)
        ex.set_params({
            "mode": "Expander", "threshold_db": threshold_db,
            "range_db": range_db, "ratio": ratio,
            "attack_ms": 0.5, "release_ms": 50.0, "enabled": True,
        })
        n = int(SR * seconds)
        f = 1000.0
        t = np.arange(n) / SR
        x = 10.0 ** (input_dbfs / 20.0) * np.sin(2.0 * np.pi * f * t)
        x = np.stack((x, x))
        y = ex.process(x)
        # 取稳态后半段峰值
        tail = y[0, n // 2:]
        peak = float(np.max(np.abs(tail)))
        return 20.0 * np.log10(max(peak, 1e-12))

    def test_ratio2_expands_20db(self):
        out_db = self._steady_output_peak_db(ratio=2.0, range_db=60.0)
        # 输入峰值 -40dBFS，阈值 -20，1:2 下扩额外衰减 20dB → 峰值 -60 dBFS
        assert out_db == pytest.approx(-60.0, abs=1.5), (
            f"1:2 downexp steady peak {out_db:.2f} dBFS, expected ≈-60"
        )

    def test_range_caps_attenuation(self):
        out_db = self._steady_output_peak_db(ratio=2.0, range_db=12.0)
        # 20dB 理论衰减被 Range 封顶 12dB → 峰值 ≈ -52 dBFS
        assert out_db == pytest.approx(-52.0, abs=1.5), (
            f"range=12 capped peak {out_db:.2f} dBFS, expected ≈-52"
        )


class TestCompressorHardKnee:
    """DY-02：hard-knee 静态传输 oracle：threshold=-20, ratio=4, input=-8
    → gain = (input-threshold)*(1/ratio-1) = 12*(-0.75) = -9dB。"""

    def test_hard_knee_9db(self):
        comp = CompressorEffect(SR)
        comp.set_params({
            "threshold_db": -20.0, "ratio": 4.0, "knee": 0.0,
            "attack_ms": 0.7, "release_ms": 50.0, "mix": 100.0,
            "makeup_gain": 0.0, "enabled": True,
        })
        n = int(SR * 1.0)
        t = np.arange(n) / SR
        x = 10.0 ** (-8.0 / 20.0) * np.sin(2.0 * np.pi * 1000.0 * t)
        x = np.stack((x, x))
        y = comp.process(x)
        tail = y[0, n // 2:]
        peak = float(np.max(np.abs(tail)))
        out_db = 20.0 * np.log10(max(peak, 1e-12))
        assert out_db == pytest.approx(-17.0, abs=1.5), (
            f"hard-knee comp steady peak {out_db:.2f} dBFS, expected ≈-17"
        )


# =============================================================================
# DY-04：Sidechain Listen 监听原始检源
# =============================================================================

class TestSidechainListen:
    """DY-04 P0：sc_listen 输出原始检源音频（保留极性），不是整流检测量。

    缺陷复现：±0.5 检源变为全正（示例 DC=0.375）。
    """

    def test_listen_preserves_polarity(self):
        comp = CompressorEffect(SR)
        comp.set_params({
            "threshold_db": -20.0, "ratio": 3.0, "knee": 15.0,
            "attack_ms": 1.4, "release_ms": 93.0, "mix": 100.0,
            "makeup_gain": 0.0, "sc_on": True, "sc_listen": True,
            "enabled": True,
        })
        n = 4800
        sq = np.where((np.arange(n) // 600) % 2 == 0, 0.5, -0.5)
        x = np.stack((sq, sq))
        sidechain = np.stack((sq, sq))
        y = comp.process(x, sidechain=sidechain)
        assert float(np.min(y[0])) < -0.4, (
            "SC Listen output lost negative polarity (detector rectified)"
        )
        # DC 接近 0（不做整流均值）
        assert abs(float(np.mean(y[0]))) < 0.1, (
            f"SC Listen output has DC {float(np.mean(y[0])):.3f}"
        )

    def test_listen_without_sidechain_passes_input(self):
        """内部检源监听：输出 = 原始输入（不整流）。"""
        comp = CompressorEffect(SR)
        comp.set_params({
            "threshold_db": -20.0, "ratio": 3.0, "knee": 15.0,
            "attack_ms": 1.4, "release_ms": 93.0, "mix": 100.0,
            "makeup_gain": 0.0, "sc_on": False, "sc_listen": True,
            "enabled": True,
        })
        n = 4800
        sq = np.where((np.arange(n) // 600) % 2 == 0, 0.5, -0.5)
        x = np.stack((sq, sq))
        y = comp.process(x)
        assert float(np.min(y[0])) < -0.4
        assert abs(float(np.mean(y[0]))) < 0.1


# =============================================================================
# MX-06：EffectChain 实例身份
# =============================================================================

def _delay_entry(iid, time_ms, fb, enabled=True):
    return ("delay", {
        "left_delay": time_ms, "right_delay": time_ms,
        "left_feedback": fb, "right_feedback": fb,
        "dry_wet": 100.0, "level_db": 0.0, "enabled": enabled,
    }, enabled, iid)


class TestEffectChainInstanceIdentity:
    """MX-06 P0：状态按 instance_id 匹配，不按 class 顺序。"""

    def test_reorder_preserves_instance_state(self):
        """调序后各实例携带自己的延迟线（回声能量连续，不被清零）。"""
        chain = EffectChain(SR)
        entries = [_delay_entry("A", 10.0, 60.0), _delay_entry("B", 300.0, 60.0)]
        chain.set_params(entries, sample_rate=SR)
        rng = np.random.default_rng(5)
        x = rng.standard_normal((2, 4800)) * 0.2
        for _ in range(10):
            chain.process(x.copy())
        last_before = chain.process(x.copy())
        energy_before = float(np.mean(last_before ** 2))

        # 调序：相同 instance_id，不同位置
        chain.set_params([entries[1], entries[0]], sample_rate=SR)
        first_after = chain.process(x.copy())
        energy_after = float(np.mean(first_after ** 2))

        # 状态被清零时（bug：按 class 复用导致 params 错配 → 重建），
        # 回声结构消失，能量骤降 >10dB。
        ratio_db = 10.0 * np.log10(max(energy_after, 1e-12) / max(energy_before, 1e-12))
        assert ratio_db > -10.0, (
            f"reorder dropped echo energy by {-ratio_db:.1f} dB "
            "(instance state not matched by instance_id)"
        )

    def test_remove_one_does_not_transfer_state(self):
        """删除 A 后 B 仍携带自己的状态（A 的缓存不给 B）。"""
        chain = EffectChain(SR)
        entries = [_delay_entry("A", 10.0, 60.0), _delay_entry("B", 300.0, 60.0)]
        chain.set_params(entries, sample_rate=SR)
        rng = np.random.default_rng(5)
        x = rng.standard_normal((2, 4800)) * 0.2
        for _ in range(10):
            chain.process(x.copy())
        energy_before = float(np.mean(chain.process(x.copy()) ** 2))

        chain.set_params([entries[1]], sample_rate=SR)  # 只剩 B
        energy_after = float(np.mean(chain.process(x.copy()) ** 2))
        ratio_db = 10.0 * np.log10(max(energy_after, 1e-12) / max(energy_before, 1e-12))
        assert ratio_db > -10.0, (
            f"removing A reset B's state (energy dropped {-ratio_db:.1f} dB)"
        )

    def test_bypassed_instances_report_zero_gr(self):
        """旁路实例的 collect_gain_reduction 不暴露上一块的过期 GR。"""
        chain = EffectChain(SR)
        comp_entry = ("compressor", {
            "threshold_db": -30.0, "ratio": 4.0, "knee": 0.0,
            "attack_ms": 0.7, "release_ms": 50.0, "mix": 100.0,
            "makeup_gain": 0.0, "enabled": True,
        }, True, "C1")
        chain.set_params([comp_entry], sample_rate=SR)
        n = 4800
        loud = np.full((2, n), 0.9)
        chain.process(loud.copy())
        gr = chain.collect_gain_reduction()
        assert gr.get("C1", 0.0) > 1.0  # 大信号下确实压缩了

        # 旁路同一实例（enabled=False）
        chain.set_params([("compressor", comp_entry[1], False, "C1")],
                         sample_rate=SR)
        chain.process(np.zeros((2, n)))
        gr = chain.collect_gain_reduction()
        assert gr.get("C1", 0.0) == 0.0, (
            f"bypassed instance still reports GR {gr.get('C1')}"
        )


# =============================================================================
# MX-07 / DL-03：尾音推进
# =============================================================================

class TestDelayTailContinues:
    """MX-07/DL-03 P0：无输入块（decoder=None 场景的 DSP 层前提）尾音继续。

    DSP 层契约：喂零块后回声仍在（peak ≈ 0.29 量级，60ms/100% wet）。
    mix_samples 的"跳过块"修复在引擎层测试覆盖。
    """

    def test_zero_block_after_impulse_keeps_echo(self):
        d = DelayEffect(SR)
        d.set_params({
            "left_delay": 60.0, "right_delay": 60.0,
            "left_feedback": 0.0, "right_feedback": 0.0,
            "low_cut": 20.0, "high_cut": 18000.0,
            "high_ratio": 0.0, "stereo_blend": 0.0,
            "dry_wet": 100.0, "level_db": 0.0, "enabled": True,
        })
        x = np.zeros((2, 2048))
        x[0, 0] = 1.0
        y1 = d.process(x)
        # 60ms@48k=2880 样本 > 块长：首块内干声为 0（100% wet），回声在下一块
        assert float(np.max(np.abs(y1))) < 1e-9, (
            "dry should be fully zeroed at 100% wet"
        )
        y2 = d.process(np.zeros((2, 2048)))  # 尾音块（feed-zero drain）
        peak = float(np.max(np.abs(y2)))
        assert peak > 0.05, f"echo tail lost: zero-fed block peak={peak}"
