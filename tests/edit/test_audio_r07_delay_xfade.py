"""R07：Delay 双读头交叉淡化回归（计划 §5 R07，2026-11-15 复审）。

复审测量（delay-measurements.json / 复审报告 §5 R07）：
- 48kHz、1kHz、振幅 0.5，Delay 10→10.25ms 热改：边界跳变 ~0.4364，
  稳态相邻采样最大 ~0.0653——读头跳变比信号自身陡 ~6.7 倍。
v4（ALGORITHM_VERSION=4）：15ms 双读头线性交叉淡化，静态渲染与 v3
逐位一致（既有 193 项 DSP 测试护航）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, DelayEffect

SR = 48000.0


def _delay_params(left_ms: float, right_ms: float, **over) -> dict:
    p = {
        "enabled": True, "left_delay": left_ms, "right_delay": right_ms,
        "left_feedback": 0.0, "right_feedback": 0.0,
        "dry_wet": 100.0, "level_db": 0.0, "low_cut": 20.0,
        "high_cut": 20000.0, "high_ratio": 0.0, "stereo_blend": 0.0,
    }
    p.update(over)
    return p


def _sine(n: int, freq: float = 1000.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n) / SR
    sig = amp * np.sin(2 * np.pi * freq * t)
    return np.stack([sig, sig]).astype(np.float32)


class TestDualReadheadCrossfade:
    def test_algorithm_version_bumped_for_xfade(self):
        assert ALGORITHM_VERSION >= 4

    def test_hot_change_boundary_jump_bounded(self):
        """10→10.25ms 热改：变化块边界的相邻采样跳变 ≤ 稳态最大跳变的
        1.6 倍（旧实现 ~6.7 倍 / 绝对 ~0.44）。"""
        fx = DelayEffect(SR)
        fx.set_params(_delay_params(10.0, 10.0), sample_rate=SR)
        n = SR  # 1s
        x = _sine(n)
        # 先跑 0.3s 建立稳态（延迟线填充）
        warm = int(0.3 * SR)
        fx.process(x[:, :warm])
        # 热改 10 → 10.25ms
        fx.set_params(_delay_params(10.25, 10.25), sample_rate=SR)
        y2 = np.asarray(fx.process(x[:, warm:]))
        # 全段相邻采样最大差
        diffs = np.abs(np.diff(y2[0]))
        steady_max = float(diffs[int(0.1 * SR):].max())  # 淡化结束后的稳态
        boundary_max = float(diffs.max())
        assert np.isfinite(y2).all()
        assert boundary_max <= steady_max * 1.6 + 1e-6, (
            f"boundary jump {boundary_max:.4f} vs steady {steady_max:.4f}")

    def test_fade_progress_not_reset_by_mid_fade_change(self):
        """淡化进行中再次改目标：进度接续（不重置回旧头）——第二次热改
        后的输出仍连续，无第二次大跳变。"""
        fx = DelayEffect(SR)
        fx.set_params(_delay_params(100.0, 100.0), sample_rate=SR)
        n = SR
        x = _sine(n)
        warm = int(0.3 * SR)
        fx.process(x[:, :warm])
        # 第一次热改（进入淡化）
        fx.set_params(_delay_params(100.25, 100.25), sample_rate=SR)
        y1 = np.asarray(fx.process(x[:, warm:warm + 480]))  # 10ms < 15ms 淡化窗
        # 淡化中途再改目标（小步 0.05ms——真实拖拽粒度；进度接续不重置）
        fx.set_params(_delay_params(100.3, 100.3), sample_rate=SR)
        y2 = np.asarray(fx.process(x[:, warm + 480:warm + 960]))
        full = np.concatenate([y1[0], y2[0]])
        diffs = np.abs(np.diff(full))
        assert float(diffs.max()) <= 0.15, (
            f"mid-fade change jumped: {float(diffs.max()):.4f}")
        assert np.isfinite(full).all()

    def test_static_rendering_unchanged_by_fade_machinery(self):
        """无热改：读头路径与 v3 公式一致（单头直读，无淡化开销影响）。"""
        fx = DelayEffect(SR)
        fx.set_params(_delay_params(121.0, 121.0), sample_rate=SR)
        x = _sine(int(0.5 * SR))
        y = np.asarray(fx.process(x))
        # 独立 oracle：121ms 环形延迟线性插值读头 + 输入滤波（与
        # DelayEffect 同系数：20Hz HP + 20kHz LP，Q=0.7071，零初始状态）
        from scipy.signal import sosfilt
        from src.features.edit.core.audio_dsp import (
            _biquad_highpass, _biquad_lowpass, _BUTTERWORTH_Q,
        )
        b, a = _biquad_highpass(20.0, _BUTTERWORTH_Q, SR)
        hp = [b[0], b[1], b[2], 1.0, a[1], a[2]]
        b, a = _biquad_lowpass(20000.0, _BUTTERWORTH_Q, SR)
        lp = [b[0], b[1], b[2], 1.0, a[1], a[2]]
        filtered, _ = sosfilt(np.array([hp, lp]), x[0].astype(np.float64),
                              zi=np.zeros((2, 2)))
        delay = 121.0 * 0.001 * SR
        buf = np.zeros(int(1200.0 * 0.001 * SR) + 8)
        expected = np.zeros_like(y[0])
        src = filtered
        widx = 0
        size = buf.size
        for j in range(src.shape[0]):
            rpos = widx - delay
            i0 = int(np.floor(rpos)) % size
            i1 = (i0 + 1) % size
            frac = rpos - np.floor(rpos)
            expected[j] = buf[i0] * (1.0 - frac) + buf[i1] * frac
            buf[widx % size] = src[j]
            widx = (widx + 1) % size
        np.testing.assert_allclose(y[0], expected, atol=1e-6)

    def test_random_block_split_matches_whole_block(self):
        """同一信号 + 同一热改时刻：整块与随机分块输出一致（淡化进度
        按样本推进，与块边界无关）。"""
        n = int(1.0 * SR)
        x = _sine(n)
        change_at = int(0.5 * SR)

        def _run(splits):
            fx = DelayEffect(SR)
            fx.set_params(_delay_params(50.0, 50.0), sample_rate=SR)
            out = np.zeros((2, n), dtype=np.float32)
            pos = 0
            changed = False
            for m in splits:
                m = min(m, n - pos)
                if m <= 0:
                    break
                if not changed and pos >= change_at:
                    fx.set_params(_delay_params(50.3, 50.3), sample_rate=SR)
                    changed = True
                out[:, pos:pos + m] = fx.process(x[:, pos:pos + m])
                pos += m
            return out

        rng = np.random.default_rng(42)
        random_splits = [int(v) for v in rng.integers(64, 3000, size=800)]
        # 在 change_at 强制块边界（参数变化时刻在两种分块下逐块对齐；
        # 否则块粒度差异导致改参时刻漂移，不是渲染不一致）
        aligned: list[int] = []
        acc = 0
        for m in random_splits:
            if acc + m > change_at > acc:
                aligned.append(change_at - acc)
                aligned.append(m - (change_at - acc))
            else:
                aligned.append(m)
            acc += m
        whole = _run([change_at, n - change_at])
        chunked = _run(aligned)
        np.testing.assert_allclose(whole, chunked, atol=1e-5)

    def test_reset_clears_fade_state(self):
        fx = DelayEffect(SR)
        fx.set_params(_delay_params(10.0, 10.0), sample_rate=SR)
        fx.process(_sine(4800))
        fx.set_params(_delay_params(20.0, 20.0), sample_rate=SR)
        fx.process(_sine(100))  # 进入淡化
        assert fx._fade_l_remaining > 0 or fx._fade_r_remaining > 0
        fx.reset_state()
        assert fx._fade_l_remaining == 0
        assert fx._fade_r_remaining == 0
        assert fx._cur_delay_l is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
