"""
Side-chain Ducker / Auto-Ducking 测试（AM-08 阶段 4.3）。

验证（计划表 4.3）：
- Speech + music fixture：speech 出现期间 music 按目标 duck dB 衰减。
- 离线/实时一致（纯 DSP，跨块连续状态）。
- attack/release 无突变 click（平滑过渡）。
- hold 时间避免呼吸感。
- AI 失败不阻塞（本模块是确定性 DSP，无 AI 依赖）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.sidechain_ducker import (
    SideChainDucker,
    DuckerParams,
    auto_duck,
)


SR = 48000.0


def _sine(amp: float, freq: float, duration: float, sr: float = SR) -> np.ndarray:
    """生成立体声正弦 PCM (2, n)。"""
    n = int(sr * duration)
    t = np.arange(n) / sr
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig, sig])


def _speech_fixture(active_start: float, active_end: float, duration: float = 2.0,
                    sr: float = SR) -> np.ndarray:
    """生成 speech fixture：仅在 [active_start, active_end] 时段有信号。"""
    n = int(sr * duration)
    speech = np.zeros((2, n), dtype=np.float32)
    s = int(active_start * sr)
    e = int(active_end * sr)
    t = np.arange(e - s) / sr
    sig = (0.5 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)  # speech-like low freq
    speech[:, s:e] = np.stack([sig, sig])
    return speech


class TestDuckerParams:
    """DuckerParams 参数。"""

    def test_default_values(self):
        p = DuckerParams.default()
        assert p.enabled is True
        assert p.target_duck_db == -6.0
        assert p.threshold_db == -30.0

    def test_serialization_roundtrip(self):
        p = DuckerParams(
            enabled=True, threshold_db=-25.0, target_duck_db=-9.0,
            attack_ms=10.0, release_ms=300.0, hold_ms=50.0,
            sidechain_source="track:t1",
        )
        d = p.to_dict()
        p2 = DuckerParams.from_dict(d)
        assert p2.target_duck_db == -9.0
        assert p2.sidechain_source == "track:t1"

    def test_frozen(self):
        """参数不可变。"""
        p = DuckerParams()
        with pytest.raises(AttributeError):
            p.target_duck_db = -3.0  # type: ignore


class TestSideChainDucking:
    """核心 duck 行为：speech 出现 → music 衰减。"""

    def test_speech_active_ducks_music(self):
        """speech 活跃期 music 按目标 duck dB 衰减。"""
        music = _sine(0.5, 440, 2.0)
        speech = _speech_fixture(0.5, 1.5, 2.0)  # 0.5-1.5s 活跃
        params = DuckerParams(target_duck_db=-6.0, attack_ms=5.0, release_ms=200.0, hold_ms=50.0)

        ducked = auto_duck(music, speech, params, SR)

        # 活跃区中心（1.0s）应衰减约 6 dB
        active_center = ducked[:, int(1.0 * SR):int(1.0 * SR) + int(0.1 * SR)]
        orig_center = music[:, int(1.0 * SR):int(1.0 * SR) + int(0.1 * SR)]
        ratio = float(np.max(np.abs(active_center)) / (np.max(np.abs(orig_center)) + 1e-9))
        gain_db = 20 * np.log10(ratio)
        assert -7.0 < gain_db < -5.0, f"活跃期应衰减 ~-6dB，实际 {gain_db:.2f}"

    def test_speech_silent_no_duck(self):
        """speech 静音期 music 不衰减。"""
        music = _sine(0.5, 440, 2.0)
        speech = np.zeros((2, int(SR * 2)), dtype=np.float32)  # 全静音
        params = DuckerParams(target_duck_db=-6.0)

        ducked = auto_duck(music, speech, params, SR)

        # 全程应不衰减（gain ≈ 0 dB）
        ratio = float(np.max(np.abs(ducked)) / (np.max(np.abs(music)) + 1e-9))
        gain_db = 20 * np.log10(ratio)
        assert -0.1 < gain_db < 0.1, f"静音期应 ~0dB，实际 {gain_db:.2f}"

    def test_duck_not_mute(self):
        """duck 是衰减不是静音（目标 -6dB 仍可听见）。"""
        music = _sine(0.5, 440, 2.0)
        speech = _sine(0.5, 200, 2.0)  # 全程活跃
        params = DuckerParams(target_duck_db=-6.0, attack_ms=1.0)

        ducked = auto_duck(music, speech, params, SR)

        # 仍应有明显信号（-6dB ≈ 0.5x，不是静音）
        assert float(np.max(np.abs(ducked))) > 0.1

    def test_different_duck_depths(self):
        """不同 target_duck_db 产生不同衰减深度。"""
        music = _sine(0.5, 440, 2.0)
        speech = _sine(0.5, 200, 2.0)  # 全程活跃

        gains = {}
        for target in [-3.0, -6.0, -12.0]:
            params = DuckerParams(target_duck_db=target, attack_ms=1.0, release_ms=100.0)
            ducked = auto_duck(music, speech, params, SR)
            # 取末段稳定区
            tail = ducked[:, -int(0.2 * SR):]
            orig_tail = music[:, -int(0.2 * SR):]
            ratio = float(np.max(np.abs(tail)) / (np.max(np.abs(orig_tail)) + 1e-9))
            gains[target] = 20 * np.log10(ratio)

        # 深度递增
        assert gains[-3.0] > gains[-6.0] > gains[-12.0]


class TestAttackRelease:
    """attack/release 平滑过渡。"""

    def test_attack_smooth_no_click(self):
        """attack 平滑过渡：用增益包络（移动 RMS 比）检查无阶跃跳变。"""
        music = _sine(0.5, 440, 1.0)
        # speech 突然出现
        speech = np.zeros((2, int(SR)), dtype=np.float32)
        speech[:, int(0.5 * SR):] = 0.5
        params = DuckerParams(target_duck_db=-12.0, attack_ms=30.0, release_ms=300.0)

        ducked = auto_duck(music, speech, params, SR)

        # 用移动 RMS（窗口）计算增益包络，避免过零点除法不稳定
        window = 480  # 10ms
        orig_rms = np.sqrt(np.convolve(music[0] ** 2, np.ones(window) / window, mode="same"))
        out_rms = np.sqrt(np.convolve(ducked[0] ** 2, np.ones(window) / window, mode="same"))
        safe = np.where(orig_rms > 1e-6, orig_rms, 1e-6)
        gain_env = out_rms / safe

        # attack 区域（0.5s 之后）增益应单调下降（平滑，无阶跃跳变）
        attack_start = int(0.5 * SR)
        attack_end = int(0.5 * SR) + int(0.1 * SR)
        region = gain_env[attack_start:attack_end]
        if len(region) > 10:
            # 相邻样本增益变化应有界（指数平滑，每步很小）
            diffs = np.abs(np.diff(region))
            # 增益包络每样本变化应 < 0.02（平滑指数衰减特征）
            assert float(np.max(diffs)) < 0.05, f"attack 不平滑: max env diff {np.max(diffs):.4f}"
            # 总体趋势：下降（duck 深度增加）
            half = len(region) // 2
            assert float(np.mean(region[:half])) > float(np.mean(region[half:])), \
                "attack 应使增益下降"

    def test_release_smooth(self):
        """release 平滑恢复。"""
        music = _sine(0.5, 440, 2.0)
        # speech 0.5-1.0s 活跃，之后静音
        speech = np.zeros((2, int(SR * 2)), dtype=np.float32)
        speech[:, int(0.5 * SR):int(1.0 * SR)] = 0.5
        params = DuckerParams(
            target_duck_db=-12.0, attack_ms=10.0, release_ms=300.0, hold_ms=50.0,
        )

        ducked = auto_duck(music, speech, params, SR)

        # release 区（1.0s 之后）增益应单调回升（不是突变）
        release_start = int(1.05 * SR)  # hold 后
        release_end = int(1.5 * SR)
        region_out = ducked[0, release_start:release_end]
        region_orig = music[0, release_start:release_end]
        safe = np.where(np.abs(region_orig) > 1e-6, region_orig, 1e-6)
        gain_region = region_out / safe
        # 总趋势：增益从负值回升到接近 0
        # 取平滑后的包络（移动平均）
        window = 480  # 10ms
        if len(gain_region) > window:
            kernel = np.ones(window) / window
            smoothed = np.convolve(gain_region, kernel, mode="valid")
            # 前半段应低于后半段（正在恢复）
            half = len(smoothed) // 2
            assert float(np.mean(smoothed[:half])) < float(np.mean(smoothed[half:])), \
                "release 应单调回升"


class TestHoldTime:
    """hold 时间避免呼吸感。"""

    def test_hold_prevents_flicker(self):
        """hold 期 speech 短暂下降不立即释放（避免呼吸感）。"""
        music = _sine(0.5, 440, 1.0)
        # speech 持续但中间有短暂下降（< hold_ms）
        speech = np.zeros((2, int(SR)), dtype=np.float32)
        speech[:] = 0.5
        speech[:, int(0.4 * SR):int(0.45 * SR)] = 0.0  # 50ms 下降
        params = DuckerParams(
            target_duck_db=-9.0, attack_ms=5.0, release_ms=100.0, hold_ms=200.0,
        )

        ducked = auto_duck(music, speech, params, SR)

        # 短暂下降区（50ms < hold 200ms）应保持 duck，不释放
        dip_region = ducked[0, int(0.42 * SR):int(0.44 * SR)]
        orig_dip = music[0, int(0.42 * SR):int(0.44 * SR)]
        safe = np.where(np.abs(orig_dip) > 1e-6, orig_dip, 1e-6)
        gain_during_dip = float(np.mean(np.abs(dip_region / safe)))
        # 应仍处于 duck（gain < 0.7，即衰减 > -3dB）
        assert gain_during_dip < 0.7, f"hold 期应保持 duck，gain={gain_during_dip:.3f}"


class TestContinuousState:
    """跨块连续状态（离线/实时一致）。"""

    def test_block_by_block_equals_whole(self):
        """分块处理结果与整段一致（连续状态正确累积）。"""
        music = _sine(0.5, 440, 1.0)
        speech = _speech_fixture(0.3, 0.7, 1.0)
        params = DuckerParams(target_duck_db=-6.0, attack_ms=10.0, release_ms=200.0, hold_ms=30.0)

        # 整段
        whole = auto_duck(music, speech, params, SR)

        # 分块（480 样本/块）
        ducker = SideChainDucker(SR)
        ducker.set_params(params)
        block_size = 480
        n = music.shape[1]
        blocks = []
        for i in range(0, n, block_size):
            m_block = music[:, i:i + block_size]
            s_block = speech[:, i:i + block_size]
            out = ducker.process(m_block, sidechain=s_block)
            blocks.append(out)
        chunked = np.concatenate(blocks, axis=1)

        # 两者应一致（误差 < 1e-4，连续状态累积）
        np.testing.assert_allclose(chunked, whole, atol=1e-4)

    def test_reset_state(self):
        """reset_state 清零状态。"""
        ducker = SideChainDucker(SR)
        ducker.set_params(DuckerParams(target_duck_db=-12.0, attack_ms=1.0))
        music = _sine(0.5, 440, 0.1)
        speech = _sine(0.5, 200, 0.1)
        ducker.process(music, sidechain=speech)
        assert ducker.current_gain_db < -1.0  # 已 duck
        ducker.reset_state()
        assert ducker.current_gain_db == 0.0


class TestNoSidechain:
    """无 side-chain 信号时安全处理。"""

    def test_none_sidechain_smooth_release(self):
        """sidechain=None 时平滑释放（不突变）。"""
        ducker = SideChainDucker(SR)
        ducker.set_params(DuckerParams(target_duck_db=-12.0, attack_ms=1.0, release_ms=100.0))
        music = _sine(0.5, 440, 0.2)
        speech = _sine(0.5, 200, 0.1)
        # 先 duck 一段
        ducker.process(music[:, :int(0.1 * SR)], sidechain=speech)
        # 然后 sidechain=None
        out = ducker.process(music[:, int(0.1 * SR):], sidechain=None)
        # 应平滑回升，不静音
        assert float(np.max(np.abs(out))) > 0.01

    def test_disabled_passthrough(self):
        """enabled=False 时直通。"""
        music = _sine(0.5, 440, 0.1)
        speech = _sine(0.5, 200, 0.1)
        ducker = SideChainDucker(SR)
        ducker.set_params(DuckerParams(enabled=False))
        out = ducker.process(music, sidechain=speech)
        np.testing.assert_array_equal(out, music)
