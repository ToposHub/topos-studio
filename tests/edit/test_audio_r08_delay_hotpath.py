"""R08：近零 Delay 热点与实时预算回归（计划 §5 R08，2026-11-15 复审）。

复审测量（delay-measurements.json）：48kHz／2000 样本块预算 41.67ms，
Delay=0／0.01ms 单实例 median 约 144.8／80.3ms——反馈最短分段为 1 样本，
每段触发 Python 循环。修复：无反馈（fb=0）整块单段向量化读头
（块内历史直接取本块输入），近零延迟不再逐样本循环；反馈非零保持原
按延迟分段语义（声音不变，由既有 DSP 测试护航）。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import DelayEffect

SR = 48000.0


def _params(**over) -> dict:
    p = {
        "enabled": True, "left_delay": 0.01, "right_delay": 0.01,
        "left_feedback": 0.0, "right_feedback": 0.0,
        "dry_wet": 100.0, "level_db": 0.0, "low_cut": 20.0,
        "high_cut": 20000.0, "high_ratio": 0.0, "stereo_blend": 0.0,
    }
    p.update(over)
    return p


class TestNoFeedbackFastPath:
    def test_intra_block_delay_matches_chunked_rendering(self):
        """无反馈 + 延迟短于块宽：整块处理与逐小块处理逐位一致
        （块内"读自身先前写入"语义 = 直接取本块滤波输入）。"""
        n = int(SR)  # 1s 块
        rng = np.random.default_rng(7)
        x = (rng.standard_normal((2, n)) * 0.2).astype(np.float32)

        def _run(block):
            fx = DelayEffect(SR)
            fx.set_params(_params(left_delay=100.0, right_delay=100.0),
                          sample_rate=SR)
            out = np.zeros((2, n), dtype=np.float32)
            pos = 0
            while pos < n:
                m = min(block, n - pos)
                out[:, pos:pos + m] = fx.process(x[:, pos:pos + m])
                pos += m
            return out

        whole = _run(n)      # 单块（延迟 4800 << 块宽 → 走块内读）
        chunked = _run(512)  # 小块（读 ring 历史）
        np.testing.assert_allclose(whole, chunked, atol=1e-6)

    def test_near_zero_delay_echo_position(self):
        """0.01ms 近零延迟：impulse 的湿声出现在 +1 样本（钳位最小延迟）。"""
        fx = DelayEffect(SR)
        fx.set_params(_params(), sample_rate=SR)
        imp = np.zeros((2, 256), np.float32)
        imp[:, 10] = 0.5
        out = np.asarray(fx.process(imp))
        pk = int(np.argmax(np.abs(out[0])))
        assert pk == 11  # 最小 1 样本延迟
        # 输入滤波（20Hz HP + 20kHz LP）会把 impulse 能量摊到邻近样本；
        # 主峰仍应承载主要能量
        assert out[0, pk] > 0.2

    @pytest.mark.slow
    def test_near_zero_delay_within_realtime_budget(self):
        """0.01ms 无反馈单实例 2000 样本块：median ≤ 10ms
        （预算 41.67ms；CI 宽松界——复审基准在空闲机测得旧实现 ~145ms，
        新实现本机 ~0.4ms；此处只防回归回逐样本量级）。"""
        fx = DelayEffect(SR)
        fx.set_params(_params(), sample_rate=SR)
        x = (np.random.default_rng(0).standard_normal((2, 2000))
             * 0.1).astype(np.float32)
        fx.process(x[:, :200])  # warmup
        times = []
        for _ in range(30):
            t0 = time.perf_counter()
            fx.process(x)
            times.append(time.perf_counter() - t0)
        median_ms = sorted(times)[15] * 1000.0
        assert median_ms < 10.0, (
            f"near-zero delay median {median_ms:.2f}ms ≥ 10ms "
            "(per-sample loop regression?)")

    def test_feedback_path_block_size_invariant(self):
        """反馈非零走原分段路径：整块与逐小块输出一致（分段只服务块内
        读-写语义，不改变声音），且有限稳定。"""
        n = int(SR)
        rng = np.random.default_rng(3)
        x = (rng.standard_normal((2, n)) * 0.2).astype(np.float32)

        def _run(block):
            fx = DelayEffect(SR)
            fx.set_params(_params(left_delay=50.0, right_delay=50.0,
                                  left_feedback=50.0, right_feedback=50.0,
                                  high_ratio=30.0),
                          sample_rate=SR)
            out = np.zeros((2, n), dtype=np.float32)
            pos = 0
            while pos < n:
                m = min(block, n - pos)
                out[:, pos:pos + m] = fx.process(x[:, pos:pos + m])
                pos += m
            return out

        whole = _run(n)
        chunked = _run(997)
        assert np.isfinite(whole).all()
        np.testing.assert_allclose(whole, chunked, atol=1e-6)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
