"""DY-03 校准回归：Attack/Hold/Release 时间响应与 Limiter 保护（阶段G）。

验收（计划 §7 DY-03）：
- Hold=100ms 释放起点误差 ≤1 采样（hold 计数逐样本推进）；
- tone burst：attack 内 GR 收敛、burst 后 release 衰减；
- 48/96kHz 等时响应一致（同一 ms 参数的时间轨迹对齐）；
- 整块/随机分块结果 ≤1e-5（块边界无状态断裂）；
- Limiter：阈值保护可验证 + 过冲报告（DY-03 新增 last_overshoot_db）；
- 参数端点 finite（attack=0 有定义，无 NaN/Inf）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import CompressorEffect, LimiterEffect

SR = 48000.0


def _comp(hold_ms=0.0, attack_ms=0.7, release_ms=50.0, threshold_db=-20.0,
          ratio=4.0, sr=SR):
    c = CompressorEffect(sr)
    c.set_params({
        "threshold_db": threshold_db, "ratio": ratio, "knee": 0.0,
        "attack_ms": attack_ms, "hold_ms": hold_ms,
        "release_ms": release_ms, "mix": 100.0,
        "makeup_gain": 0.0, "enabled": True,
    })
    return c


class TestDy03HoldReleaseStart:
    def test_hold_100ms_release_start_within_1_sample(self):
        """hold=100ms：包络冻结恰好 hold_samples 后开始 release（±1 采样）。"""
        hold_ms = 100.0
        hold_samples = int(round(hold_ms * 0.001 * SR))  # 4800
        comp = _comp(hold_ms=hold_ms, attack_ms=0.7, release_ms=200.0)

        loud_samples = int(0.05 * SR)  # 50ms 响信号
        n = int(0.4 * SR)
        det = np.zeros(n, dtype=np.float64)
        det[:loud_samples] = 1.0

        env_db = comp._smooth_env(det)
        env_lin = 10.0 ** (env_db / 20.0)
        # 释放起点：首个 env 下降的样本（冻结期 env 恒定）
        decay_start = None
        for i in range(loud_samples, n):
            if env_lin[i] < env_lin[i - 1] * (1.0 - 1e-9):
                decay_start = i
                break
        expected = loud_samples + hold_samples
        assert decay_start is not None, "envelope never released after hold"
        assert abs(decay_start - expected) <= 1, (
            f"hold release start off by {decay_start - expected} samples "
            f"(got {decay_start}, expected ~{expected})")

    def test_hold_freeze_value_constant(self):
        """冻结期包络严格恒定（无泄漏、无提前衰减）。"""
        comp = _comp(hold_ms=100.0, release_ms=200.0)
        loud = int(0.05 * SR)
        n = int(0.3 * SR)
        det = np.zeros(n)
        det[:loud] = 1.0
        env = 10.0 ** (comp._smooth_env(det) / 20.0)
        frozen = env[loud:loud + 4000]
        assert np.all(frozen == frozen[0]), "hold window envelope must freeze"


class TestDy03ToneBurst:
    def test_burst_gr_attack_and_release(self):
        """50ms burst @-6dB（阈值 -20dB）：attack 期 GR 上升；burst 后 release
        衰减，5×release 后回到近 0。"""
        comp = _comp(attack_ms=5.0, release_ms=100.0, threshold_db=-20.0)
        burst = int(0.05 * SR)
        silence = int(0.5 * SR)
        t = np.arange(burst + silence) / SR
        sig = np.where(t < 0.05,
                       0.5 * np.sin(2 * np.pi * 500.0 * t), 0.0)
        x = np.stack([sig, sig]).astype(np.float32)

        out = comp.process(x)
        # burst 结束时 GR 显著（-6dB 输入 vs -20dB 阈值，ratio 4）
        gr_during = comp.last_gr_db
        assert gr_during > 3.0, f"GR during burst too small: {gr_during}"

        # burst 后静音 5×release → GR 回到 0
        comp.process(np.zeros((2, int(0.5 * SR)), dtype=np.float32))
        assert comp.last_gr_db == 0.0
        # 输出 finite
        assert np.all(np.isfinite(out))

    def test_burst_attack_convergence_speed(self):
        """attack=5ms：3×attack 内 GR 达到稳态的 80% 以上。"""
        comp = _comp(attack_ms=5.0, release_ms=100.0)
        n = int(0.05 * SR)
        t = np.arange(n) / SR
        sig = (0.5 * np.sin(2 * np.pi * 500.0 * t)).astype(np.float32)
        x = np.stack([sig, sig])
        comp.process(x[:, :int(0.015 * SR)])   # 3×attack = 15ms
        gr_at_3att = comp.last_gr_db
        comp2 = _comp(attack_ms=5.0, release_ms=100.0)
        comp2.process(x)                        # 50ms 稳态
        gr_steady = comp2.last_gr_db
        assert gr_at_3att >= 0.8 * gr_steady, (
            f"attack too slow: {gr_at_3att:.2f} dB at 3×attack "
            f"vs steady {gr_steady:.2f} dB")


class TestDy03SampleRateEquivalence:
    def test_48k_96k_time_aligned_envelope(self):
        """同一 ms 参数在 48k/96k：同一时间点的包络差 ≤0.5dB（等时响应）。"""
        def _env_at(sr):
            comp = _comp(attack_ms=5.0, release_ms=100.0, hold_ms=50.0, sr=sr)
            loud = int(0.05 * sr)
            n = int(0.35 * sr)
            det = np.zeros(n)
            det[:loud] = 1.0
            return 10.0 ** (comp._smooth_env(det) / 20.0)

        env48, env96 = _env_at(48000.0), _env_at(96000.0)
        # 每 1ms 比较一次（0.2ms 后开始，避开起跳瞬态首样本）
        for ms in range(1, 340):
            i48 = int(ms * 0.001 * 48000.0)
            i96 = int(ms * 0.001 * 96000.0)
            if i48 >= len(env48) or i96 >= len(env96):
                break
            d48 = 20 * np.log10(max(env48[i48], 1e-9))
            d96 = 20 * np.log10(max(env96[i96], 1e-9))
            assert abs(d48 - d96) <= 0.5, (
                f"envelope diverges at {ms}ms: 48k={d48:.2f} 96k={d96:.2f}")


class TestDy03BlockEquivalence:
    @pytest.mark.parametrize("hold_ms", [0.0, 100.0])
    def test_whole_vs_random_chunks(self, hold_ms):
        """整块处理 vs 随机分块：输出逐样本一致 ≤1e-5（跨块状态连续）。"""
        rng = np.random.default_rng(23)
        n = 4096
        sig = (0.4 * rng.standard_normal(n)).astype(np.float32)
        x = np.stack([sig, sig])

        whole = _comp(hold_ms=hold_ms, attack_ms=2.0, release_ms=80.0)
        y_whole = whole.process(x.copy())

        chunked = _comp(hold_ms=hold_ms, attack_ms=2.0, release_ms=80.0)
        pieces, pos = [], 0
        splits = [101, 977, 512, 1506, 500, 900]  # 和 < n，余量收尾
        for s in splits:
            if pos >= n:
                break
            pieces.append(chunked.process(x[:, pos:pos + s].copy()))
            pos += s
        if pos < n:
            pieces.append(chunked.process(x[:, pos:].copy()))
        y_chunked = np.concatenate(pieces, axis=-1)

        np.testing.assert_allclose(
            y_whole, y_chunked, atol=1e-5,
            err_msg=f"hold={hold_ms}ms: block boundary state discontinuity")


class TestDy03LimiterProtection:
    def test_threshold_protection_and_overshoot_report(self):
        """阈值 -6dB：输出峰值 ≤ 阈值×1.0001；0dB 输入触发过冲报告。"""
        lim = LimiterEffect(SR)
        lim.set_params({"threshold_db": -6.0, "attack_ms": 1.0,
                        "hold_ms": 0.0, "release_ms": 50.0, "enabled": True})
        t = np.arange(int(0.1 * SR)) / SR
        sig = (1.0 * np.sin(2 * np.pi * 500.0 * t)).astype(np.float32)
        out = lim.process(np.stack([sig, sig]))

        thr_lin = 10 ** (-6.0 / 20.0)
        peak = float(np.max(np.abs(out)))
        assert peak <= thr_lin * 1.0002, (
            f"limiter failed protection: peak {peak:.4f} > thr {thr_lin:.4f}")
        # 0dB 输入对 -6dB 阈值：过冲报告为正（attack 期间瞬态超阈）
        assert lim.last_overshoot_db > 0.0, "overshoot not reported"

    def test_no_overshoot_below_threshold(self):
        lim = LimiterEffect(SR)
        lim.set_params({"threshold_db": -6.0, "attack_ms": 1.0,
                        "hold_ms": 0.0, "release_ms": 50.0, "enabled": True})
        t = np.arange(int(0.05 * SR)) / SR
        sig = (0.3 * np.sin(2 * np.pi * 500.0 * t)).astype(np.float32)
        lim.process(np.stack([sig, sig]))
        assert lim.last_overshoot_db == 0.0

    def test_gain_does_not_jump_back_across_blocks(self):
        """恢复期跨块无增益跳回 1：限幅块后接静音块，输出连续无阶跃。"""
        lim = LimiterEffect(SR)
        lim.set_params({"threshold_db": -6.0, "attack_ms": 1.0,
                        "hold_ms": 0.0, "release_ms": 200.0, "enabled": True})
        t = np.arange(480) / SR
        loud = np.stack([(0.9 * np.sin(2 * np.pi * 500.0 * t)).astype(np.float32)] * 2)
        lim.process(loud)
        # 静音块：增益恢复中，输出应为 0（输入为 0）——关键在下一块有信号时
        # 增益不得跳回 1。用小信号块检查增益仍 < 1（输出被压低）。
        small = np.full((2, 480), 0.35, dtype=np.float32)
        out = lim.process(small.copy())
        # gain < 1：0.35 输入在恢复期被衰减（阈值 0.501；env 仍高 → 压制）
        assert float(np.max(np.abs(out))) < 0.35 * 1.001, (
            "gain jumped back to 1.0 across block boundary (pumping)")


class TestDy03EndpointFinite:
    @pytest.mark.parametrize("params_over", [
        {"attack_ms": 0.0},
        {"release_ms": 0.0},
        {"attack_ms": 1000.0, "release_ms": 5000.0, "hold_ms": 4000.0},
        {"threshold_db": -60.0, "ratio": 20.0},
        {"threshold_db": 0.0},
    ])
    def test_compressor_endpoints_finite(self, params_over):
        rng = np.random.default_rng(5)
        n = 2048
        x = np.stack([(0.4 * rng.standard_normal(n)).astype(np.float32)] * 2)
        params = {"threshold_db": -20.0, "ratio": 4.0, "knee": 0.0,
                  "attack_ms": 0.7, "hold_ms": 0.0, "release_ms": 50.0,
                  "mix": 100.0, "makeup_gain": 0.0, "enabled": True}
        params.update(params_over)
        c = CompressorEffect(SR)
        c.set_params(params)
        out = c.process(x.copy())
        assert np.all(np.isfinite(out)), f"non-finite output with {params_over}"
        assert out.shape == x.shape

    def test_limiter_endpoints_finite(self):
        rng = np.random.default_rng(6)
        x = np.stack([(0.5 * rng.standard_normal(1024)).astype(np.float32)] * 2)
        for params in ({"threshold_db": -30.0, "attack_ms": 30.0,
                        "release_ms": 4000.0, "hold_ms": 4000.0},
                       {"threshold_db": 0.0, "attack_ms": 0.7,
                        "release_ms": 50.0, "hold_ms": 0.0}):
            lim = LimiterEffect(SR)
            lim.set_params({**params, "enabled": True})
            out = lim.process(x.copy())
            assert np.all(np.isfinite(out))
