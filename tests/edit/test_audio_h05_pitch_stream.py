"""H05：Pitch 有界流、时延/Mix、抗混叠与 Link（复审 §5 H05）。

修前复现：
- ``_bufs`` 按累计写位扩容（float64 立体声 ~2.6GiB/小时结构性增长）；
- 0st/50% mix 脉冲在 sample0 与 5280 双重出现而 latency 报 0；
- 20kHz + 12st 强折叠到 8kHz（RMS≈0.237），后置低通无法删除；
- Link 公式 −st·link 与契约（0=保包络/100=随pitch）相反。

修后契约（``_PitchModule`` H05）：
- 物理环形容量 = win + 子块上限 + 边距（与整片时长无关）；
- read_pos 中心初始化：r=1 时 wet 延迟恒 win/2；latency 恒报 win/2
  （含 0st），link warp 时延叠加；dry 经时延线对齐——mix 任意值单一脉冲；
- 升调输入先过 4 级 fc=0.45·SR/r_max 单极点预滤波（折叠前滤除）；
- Glide 按绝对流位置滑动（块长无关）；Link = −st·(1−link)。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_effects.voice_changer import (
    VoiceChangerEffect,
    _PitchModule,
)
from src.features.edit.core.timeline_v2 import VoiceChangerParams

pytestmark = pytest.mark.edit_core
SR = 48000


def _pulse_params(**kw) -> VoiceChangerParams:
    base = dict(formant_enabled=False, pitch_enabled=True,
                pitch_semitones=0.0)
    base.update(kw)
    return VoiceChangerParams(**base)


class TestBoundedRing:
    def test_buffer_bytes_constant_over_long_stream(self):
        fx = VoiceChangerEffect(SR)
        fx.set_params(_pulse_params(pitch_semitones=5.0).to_dict())
        cap0 = fx._pitch._bufs[0].nbytes
        rng = np.random.default_rng(3)
        for _ in range(12):  # 12 秒
            fx.process((rng.standard_normal((2, SR)) * 0.2))
        assert fx._pitch._bufs[0].nbytes == cap0
        assert cap0 == (10560 + 4096 + 16) * 8  # 契约化容量

    def test_subchunking_inside_big_blocks(self):
        """大块内部按 ≤4096 子块推进（容量不变、读写一致）。"""
        pm = _PitchModule(SR)
        pm.set_seed(9)
        p = _pulse_params(pitch_semitones=-7.0, pitch_jitter_pct=30.0)
        rng = np.random.default_rng(4)
        x = rng.standard_normal(SR * 2) * 0.25
        w = pm._process_mono(x, p, 0, 0)
        pm2 = _PitchModule(SR)
        pm2.set_seed(9)
        parts, pos = [], 0
        for m in [4096] * 23 + [1792]:
            parts.append(pm2._process_mono(x[pos:pos + m], p, pos, 0))
            pos += m
        np.testing.assert_allclose(w, np.concatenate(parts), atol=1e-10)


class TestLatencyAndMix:
    @pytest.mark.parametrize("mix", [0.0, 50.0, 100.0])
    @pytest.mark.parametrize("link", [0.0, 100.0])
    def test_zero_st_single_aligned_pulse(self, mix, link):
        """0st：mix 任意/link 任意 → 恰一个脉冲，位于报告的 latency。"""
        fx = VoiceChangerEffect(SR)
        fx.set_params(_pulse_params(pitch_mix_pct=mix,
                                    pitch_formant_link_pct=link).to_dict())
        x = np.zeros((2, 24000))
        x[:, 0] = 0.8
        y = fx.process(x.copy())
        nz = np.flatnonzero(np.abs(y[0]) > 1e-4)
        assert len(nz) == 1
        assert nz[0] == fx.latency_samples()

    def test_latency_reported_includes_zero_st(self):
        fx = VoiceChangerEffect(SR)
        fx.set_params(_pulse_params().to_dict())
        # win/2 = 5280（pitch 0st 也报告）+ link warp 1024（link<100）
        assert fx.latency_samples() == 5280 + 1024
        fx2 = VoiceChangerEffect(SR)
        fx2.set_params(_pulse_params(pitch_formant_link_pct=100.0).to_dict())
        assert fx2.latency_samples() == 5280
        # formant 同时开启时叠加（契约：各模块时延累加）
        fx3 = VoiceChangerEffect(SR)
        fx3.set_params(VoiceChangerParams(
            formant_enabled=True, pitch_enabled=True,
            pitch_formant_link_pct=100.0).to_dict())
        assert fx3.latency_samples() == 1024 + 5280

    @pytest.mark.parametrize("st", [-24.0, -12.0, 12.0, 24.0])
    def test_endpoints_bounded(self, st):
        fx = VoiceChangerEffect(SR)
        fx.set_params(_pulse_params(pitch_semitones=st,
                                    pitch_formant_link_pct=100.0).to_dict())
        rng = np.random.default_rng(5)
        y = fx.process(rng.standard_normal((2, SR)) * 0.3)
        assert np.isfinite(y).all()
        assert np.abs(y).max() < 60.0  # 有界（容忍共振/包络增益）


class TestAntiAlias:
    def test_20k_plus_12st_no_strong_foldback(self):
        """复审基线：修前折叠 RMS≈0.237；预滤波后须显著低于该值。"""
        t = np.arange(SR * 2) / SR
        sig = np.stack([0.5 * np.sin(2 * np.pi * 20000.0 * t)] * 2)
        fx = VoiceChangerEffect(SR)
        fx.set_params(_pulse_params(pitch_semitones=12.0,
                                    pitch_formant_link_pct=100.0).to_dict())
        y = fx.process(sig.copy())
        seg = y[0, 12000:2 * SR - 1200]
        X = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        fr = np.fft.rfftfreq(len(seg), 1 / SR)
        alias = float(np.sqrt(np.sum(X[(fr >= 4000) & (fr < 11000)] ** 2))
                      ) / (len(seg) / 2)
        assert alias < 0.06, alias  # 修前 0.237

    def test_passband_content_survives(self):
        """1kHz + 12st → 2kHz 分量显著存在（预滤波不吃掉有效内容）。"""
        t = np.arange(SR * 2) / SR
        sig = np.stack([0.4 * np.sin(2 * np.pi * 1000.0 * t)] * 2)
        fx = VoiceChangerEffect(SR)
        fx.set_params(_pulse_params(pitch_semitones=12.0,
                                    pitch_formant_link_pct=100.0).to_dict())
        y = fx.process(sig.copy())
        seg = y[0, 20000:-20000]
        X = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        fr = np.fft.rfftfreq(len(seg), 1 / SR)
        band = float(X[(fr > 1500) & (fr < 2500)].max())
        total = float(X.max())
        assert band > 0.2 * total


class TestGlide:
    def test_glide_ramps_by_absolute_time(self):
        """st 目标变更后，ratio 按 glide_ms 内的**绝对推进量**滑动。"""
        pm = _PitchModule(SR)
        p0 = _pulse_params(pitch_semitones=0.0, pitch_glide_ms=100.0,
                           pitch_formant_link_pct=100.0)
        pm._ratio(p0, 1, 0)  # 初始目标 0st
        p1 = _pulse_params(pitch_semitones=7.0, pitch_glide_ms=100.0,
                           pitch_formant_link_pct=100.0)
        glide = int(0.100 * SR)
        r0 = float(pm._ratio(p1, 1, 1000)[0])          # 变更即后：≈1
        r50 = float(pm._ratio(p1, 1, 1000 + glide // 2)[0])
        r100 = float(pm._ratio(p1, 1, 1000 + glide)[0])
        target = 2.0 ** (7.0 / 12.0)
        assert r0 == pytest.approx(1.0, abs=1e-3)
        assert abs(r100 - target) < 1e-6
        assert 1.0 < r50 < target
        # 与块划分无关：中途换块粒度不重置进度
        pm2 = _PitchModule(SR)
        pm2._ratio(p0, 512, 0)
        pm2._ratio(p1, 300, 1000)
        r_mid = float(pm2._ratio(p1, 1, 1000 + glide // 3)[0])
        expected = 2.0 ** ((7.0 / 12.0) * (1.0 / 3.0))
        assert r_mid == pytest.approx(expected, rel=1e-3)

    def test_glide_zero_instant(self):
        pm = _PitchModule(SR)
        p = _pulse_params(pitch_semitones=4.0, pitch_glide_ms=0.0,
                          pitch_formant_link_pct=100.0)
        r = pm._ratio(p, 8, 0)
        np.testing.assert_allclose(r, 2.0 ** (4.0 / 12.0), rtol=1e-12)


class TestLinkContract:
    @staticmethod
    def _centroid(y: np.ndarray, offset: int) -> float:
        """谱矩心（Hz；对绕点边带稳健的方向性度量）。"""
        seg = y[offset:offset + SR] * np.hanning(SR)
        X = np.abs(np.fft.rfft(seg)) ** 2
        fr = np.fft.rfftfreq(SR, 1 / SR)
        m = (fr > 100.0) & (fr < 8000.0)
        return float(np.sum(fr[m] * X[m]) / max(1e-12, np.sum(X[m])))

    def test_link_zero_keeps_envelope_link_full_follows(self):
        """合成共振峰语音 + 12st 的谱矩心：

        - link=100（不补偿）：输出矩心 ≈ 输入 ×2（包络随 pitch 上移）；
        - link=0（完全补偿）：输出矩心 ≈ 输入 ×1（包络被 warp 拉回）。

        输入 4s、分析输出中段 [SR, 2SR]（+12st 读头速率 2×，输出 1s 窗
        消耗输入 2s）。绕点淡化把谐波扩散为窄边带（Doppler 已知权衡），
        矩心对其稳健。
        """
        t = np.arange(SR * 4) / SR
        f0 = 150.0
        voice = np.zeros(SR * 4)
        for k in range(1, 25):
            amp = (np.exp(-((k * f0 - 500.0) / 600.0) ** 2)
                   + 0.5 * np.exp(-((k * f0 - 1500.0) / 500.0) ** 2))
            voice += amp * np.sin(2 * np.pi * f0 * k * t)
        voice = (0.4 * voice / np.max(np.abs(voice)))
        x = np.stack([voice, voice])
        c_in = self._centroid(voice, SR)

        ratios = {}
        for link in (0.0, 100.0):
            fx = VoiceChangerEffect(SR)
            fx.set_params(_pulse_params(pitch_semitones=12.0,
                                        pitch_formant_link_pct=link,
                                        pitch_glide_ms=0.0).to_dict())
            y = fx.process(x.copy())
            ratios[link] = self._centroid(y[0], SR) / c_in
        # link=100：矩心 ~×2（Doppler 边带/预滤波允许 ±25%）
        assert abs(ratios[100.0] - 2.0) < 0.5, ratios
        # link=0：补偿拉回 ~×1（±35%——warp 是近似补偿）
        assert abs(ratios[0.0] - 1.0) < 0.35, ratios
        # 方向不同
        assert ratios[100.0] > ratios[0.0] * 1.4


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
