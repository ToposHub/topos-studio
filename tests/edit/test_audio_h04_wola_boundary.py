"""H04：WOLA 边界、窗归一与 Neutral 恒等（复审计划 §5 H04）。

已复现（修前）：默认 Neutral、首样本单位脉冲输出 peak=0/energy=0，
报告 latency=1024——hann 起点样本为 0 且无负时间帧覆盖。

修复契约（``_WolaStream``）：
- 输入前导 ``win−hop`` 零 + 输出队列 ``hop`` 零 → 真实样本净时延仍
  恰为 ``win``，且首/尾边界获得完整 4 帧覆盖；
- 周期 hann（symmetric 的 w² 周期化和在帧缘有 ~1e-4 波纹）→ 中性
  恒等到机器精度；
- ``flush()``：流结束补零完成滞留帧，返回剩余真实输出。

验收矩阵：首样本/逐 hop 相位/末样本脉冲；1 sample、短于窗输入；
整块 vs 随机块；L-only/R-only；reset/采样率切换；mix 端点时延一致；
静音/DC/Nyquist 稳定。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_effects.voice_changer import (
    VoiceChangerEffect,
    _WolaStream,
)
from src.features.edit.core.timeline_v2 import VoiceChangerParams

pytestmark = pytest.mark.edit_core
SR = 48000
WIN = 1024
HOP = 256


def _neutral(X, E):
    return (X / np.maximum(E, 1e-10)) * E


class TestImpulseTraversal:
    def test_first_sample_unit_impulse_reconstructed(self):
        w = _WolaStream(SR)
        imp = np.zeros(WIN * 4)
        imp[0] = 1.0
        y = np.concatenate([w.process(imp[:2048], _neutral),
                            w.process(imp[2048:], _neutral),
                            w.flush(_neutral)])
        seg = y[WIN:WIN + len(imp)]
        assert abs(seg[0] - 1.0) < 1e-9          # 复审案例：修前为 0
        assert np.abs(seg[1:]).max() < 1e-9      # 无时间扩散
        assert abs(np.abs(seg).sum() - 1.0) < 1e-9

    def test_every_hop_phase(self):
        """脉冲遍历每个 hop 相位：输出 = 延迟后的单位脉冲。"""
        for phase in range(0, WIN, HOP // 2):
            w = _WolaStream(SR)
            imp = np.zeros(WIN * 4)
            imp[phase] = 1.0
            y = np.concatenate([w.process(imp[:1536], _neutral),
                                w.process(imp[1536:], _neutral),
                                w.flush(_neutral)])
            seg = y[WIN:WIN + len(imp)]
            peak = int(np.argmax(np.abs(seg)))
            assert peak == phase, phase
            assert abs(abs(seg[phase]) - 1.0) < 1e-9, phase
            assert abs(np.abs(seg).sum() - 1.0) < 1e-9, phase

    def test_last_sample_impulse(self):
        w = _WolaStream(SR)
        n = 3000
        imp = np.zeros(n)
        imp[n - 1] = 1.0
        y = np.concatenate([w.process(imp, _neutral), w.flush(_neutral)])
        ref = np.zeros(n)
        ref[n - 1] = 1.0
        assert np.abs(y[WIN:WIN + n] - ref).max() < 1e-9


class TestNeutralIdentity:
    def test_neutral_passthrough_machine_precision(self):
        rng = np.random.default_rng(11)
        x = rng.standard_normal(9000) * 0.3
        w = _WolaStream(SR)
        y = w.process(x, _neutral)
        y2 = w.flush(_neutral)
        total = np.concatenate([y, y2])
        assert len(total) == 9000 + WIN
        assert np.abs(total[WIN:] - x).max() < 1e-12

    def test_effect_level_neutral_identity(self):
        """整链 Neutral（formant 开、shift=0）：延迟 win 后精确直通。"""
        rng = np.random.default_rng(12)
        x = rng.standard_normal((2, 9000)) * 0.3
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams(
            formant_enabled=True, formant_shift_st=0.0).to_dict())
        y = fx.process(x.copy())
        assert np.abs(y[:, WIN:] - x[:, :9000 - WIN]).max() < 1e-10

    def test_mix_endpoints_latency_is_reported(self):
        """mix=0/100/禁用：各配置按**其报告的**时延精确直通（无隐藏时延）。"""
        rng = np.random.default_rng(13)
        x = rng.standard_normal((2, 9000)) * 0.3
        for mix, enabled, expect_lat in ((0.0, True, WIN),
                                         (100.0, True, WIN),
                                         (None, False, 0)):
            p = (VoiceChangerParams(formant_enabled=True,
                                    formant_mix_pct=mix) if enabled
                 else VoiceChangerParams(formant_enabled=False))
            fx = VoiceChangerEffect(SR)
            fx.set_params(p.to_dict())
            assert fx.latency_samples() == expect_lat
            y = fx.process(x.copy())
            assert np.abs(y[:, expect_lat:]
                          - x[:, :9000 - expect_lat]).max() < 1e-10, mix

    def test_latency_reported_matches_net_delay(self):
        w = _WolaStream(SR)
        assert w.latency == WIN
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams().to_dict())
        assert fx.latency_samples() == WIN


class TestShortInputs:
    def test_one_sample_input(self):
        """单个 0.7 样本后接零流：延迟 win 后重构出唯一非零样本。"""
        w = _WolaStream(SR)
        outs = [w.process(np.array([0.7]), _neutral)]
        outs += [w.process(np.zeros(1), _neutral) for _ in range(499)]
        outs.append(w.flush(_neutral))
        total = np.concatenate(outs)
        assert len(total) == 500 + WIN
        assert abs(total[WIN] - 0.7) < 1e-9
        assert np.abs(np.delete(total, WIN)).max() < 1e-9

    def test_shorter_than_window_input(self):
        w = _WolaStream(SR)
        x = np.linspace(-0.5, 0.5, 700)
        y = w.process(x, _neutral)
        tail = w.flush(_neutral)
        total = np.concatenate([y, tail])
        assert len(total) == 700 + WIN
        assert np.abs(total[WIN:] - x).max() < 1e-12


class TestBlockInvarianceAndChannels:
    def test_random_blocks_equal_whole_block(self):
        rng = np.random.default_rng(21)
        x = rng.standard_normal(20000) * 0.25
        w1 = _WolaStream(SR)
        y_whole = np.concatenate([w1.process(x, _neutral),
                                  w1.flush(_neutral)])
        w2 = _WolaStream(SR)
        parts = []
        pos = 0
        while pos < len(x):
            n = int(rng.integers(1, 3000))
            parts.append(w2.process(x[pos:pos + n], _neutral))
            pos += n
        parts.append(w2.flush(_neutral))
        y_rand = np.concatenate(parts)
        assert np.abs(y_whole - y_rand).max() < 1e-12

    def test_left_only_right_silent(self):
        """仅左声道：右输出严格为 0（无串声，L09 语义保持）。"""
        rng = np.random.default_rng(22)
        x = np.zeros((2, 9000))
        x[0] = rng.standard_normal(9000) * 0.3
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams(
            formant_enabled=True, formant_shift_st=3.0).to_dict())
        y = fx.process(x.copy())
        assert np.abs(y[1, WIN + 64:]).max() < 1e-12

    def test_reset_then_clean_state(self):
        rng = np.random.default_rng(23)
        x = rng.standard_normal((2, 6000)) * 0.3
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams().to_dict())
        fx.process(x.copy())
        fx.reset_state()
        y = fx.process(x.copy())
        assert np.abs(y[:, WIN:] - x[:, :6000 - WIN]).max() < 1e-10

    def test_sample_rate_switch_rebuilds(self):
        rng = np.random.default_rng(24)
        x = rng.standard_normal((2, 6000)) * 0.3
        fx = VoiceChangerEffect(44100.0)
        fx.set_params(VoiceChangerParams().to_dict())
        fx.process(x.copy())
        fx.set_params(VoiceChangerParams().to_dict(), sample_rate=48000.0)
        y = fx.process(x.copy())
        assert np.abs(y[:, WIN:] - x[:, :6000 - WIN]).max() < 1e-10


class TestStability:
    def test_silence_dc_nyquist_stay_bounded(self):
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams(
            formant_enabled=True, formant_shift_st=7.0).to_dict())
        n = SR // 4
        t = np.arange(n) / SR
        sig = np.stack([
            np.zeros(n),                       # 静音
            0.5 * np.ones(n),                  # DC
            0.5 * np.sin(np.pi * np.arange(n)),  # Nyquist
        ])
        y = fx.process(sig.copy())
        assert np.isfinite(y).all()
        assert np.abs(y[:, WIN:]).max() < 3.0  # 无宽带增益爆炸


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
