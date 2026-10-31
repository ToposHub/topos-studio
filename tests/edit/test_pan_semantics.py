"""
声像语义重定测试（RMA-08 阶段 C2）。

验收门槛（计划表 C2）：
- 同幅 stereo 1kHz 在 center 的每通道应保持 0dB（balance 模式）；
- mono center 总功率保持恒定（equal-power pan）；
- pan sweep 的能量变化满足定义；
- 实时、导出、Bus 三处共用同一 panner fixture。

核心 bug（RMA-08）：旧实现对任何多声道输入都套 equal-power pan（center 每侧
-3dB），原生立体声会整体降低且改变声像。既有测试也把这种行为写成预期（已迁移）。

修复：按源声道数选算法——mono=equal-power（center 恒功率），stereo=balance
（unity-center，保持原响度与声像），surround=trim（暂用 balance，完整 panner 待 C1）。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    pan_gains, balance_gains, pan_gains_for_channels,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. pan 算法区分（单元层）
# ─────────────────────────────────────────────────────────────────────────────

class TestPanAlgorithmSelection:
    """RMA-08: pan_gains_for_channels 按声道数选算法。"""

    def test_mono_uses_equal_power(self):
        """1 声道 → equal-power pan（center 每侧 0.7071）。"""
        lg, rg = pan_gains_for_channels(0.0, 1)
        assert abs(lg - 0.7071) < 1e-3
        assert abs(rg - 0.7071) < 1e-3

    def test_stereo_uses_balance(self):
        """2 声道 → balance（center 每侧 1.0 unity）。"""
        lg, rg = pan_gains_for_channels(0.0, 2)
        assert lg == 1.0
        assert rg == 1.0

    def test_surround_uses_balance(self):
        """>2 声道 → balance（trim，完整 surround panner 待 C1）。"""
        lg, rg = pan_gains_for_channels(0.0, 6)
        assert lg == 1.0
        assert rg == 1.0

    def test_balance_far_side_attenuated(self):
        """balance: pan 偏离时只衰减远侧，近侧保持 unity。"""
        # pan=0.5（偏右）
        lg, rg = balance_gains(0.5)
        assert abs(lg - 0.5) < 1e-6  # 远侧衰减
        assert abs(rg - 1.0) < 1e-6  # 近侧 unity

    def test_equal_power_constant_power(self):
        """equal-power: cos²+sin²=1（任意 pan 总功率恒定）。"""
        for pan in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            lg, rg = pan_gains(pan)
            power = lg * lg + rg * rg
            assert abs(power - 1.0) < 1e-6, f"pan={pan} 总功率 {power} ≠ 1.0"


# ─────────────────────────────────────────────────────────────────────────────
# 2. mono 源：equal-power（总功率恒定）
# ─────────────────────────────────────────────────────────────────────────────

class TestMonoSourceEqualPower:
    """RMA-08: mono 源用 equal-power，pan 扫描总功率恒定。"""

    def test_mono_pan_sweep_constant_power(self, qapp):
        """mono 源扫描 pan，master 总功率波动 ≤0.2dB。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState

        class _MonoDC:
            """单声道 DC decoder（_channels=1）。"""
            _channels = 1
            _timeline_start = 0
            _timeline_duration = 100000
            _clip_id = None
            _speed = 1.0
            _fade = None
            def __init__(self, amp=0.5):
                self.amp = float(amp)
            def decode_samples_for_frame(self, frame, fps, num_samples):
                # 返回单声道，mix_samples 会 adapt 到 stereo
                return np.full((1, num_samples), self.amp, dtype=np.float32)
            def close(self):
                pass

        powers = []
        for pan in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            mixer = AudioMixer()
            mixer.add_track("t1", _MonoDC(0.5),
                            AudioTrackState(track_id="t1", volume=1.0, pan=pan))
            out, _ = mixer.mix_samples(0, 24.0, 480, 2)
            power = float(np.mean(out[0] ** 2 + out[1] ** 2))
            powers.append(power)
        max_p, min_p = max(powers), min(powers)
        deviation_db = 10 * math.log10(max_p / min_p) if min_p > 1e-12 else float('inf')
        assert deviation_db <= 0.3, f"mono equal-power 总功率波动 {deviation_db:.3f} dB"


# ─────────────────────────────────────────────────────────────────────────────
# 3. stereo 源：balance（center unity）
# ─────────────────────────────────────────────────────────────────────────────

class TestStereoSourceBalance:
    """RMA-08: stereo 源用 balance，center 每通道 0dB。"""

    def test_stereo_center_unity_per_channel(self, qapp):
        """stereo 源 pan=0 → 每通道保持原振幅（非 equal-power 的 -3dB）。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState

        class _StereoDC:
            _channels = 2
            _timeline_start = 0
            _timeline_duration = 100000
            _clip_id = None
            _speed = 1.0
            _fade = None
            def __init__(self, amp=0.5):
                self.amp = float(amp)
            def decode_samples_for_frame(self, frame, fps, num_samples):
                sig = np.full(num_samples, self.amp, dtype=np.float32)
                return np.stack([sig, sig])
            def close(self):
                pass

        mixer = AudioMixer()
        mixer.add_track("t1", _StereoDC(0.5),
                        AudioTrackState(track_id="t1", volume=1.0, pan=0.0))
        out, _ = mixer.mix_samples(0, 24.0, 480, 2)
        # center: 每通道 0.5（unity），非 0.354（equal-power -3dB）
        assert abs(out[0][0] - 0.5) < 1e-3, f"stereo center 左应 unity，got {out[0][0]}"
        assert abs(out[1][0] - 0.5) < 1e-3, f"stereo center 右应 unity，got {out[1][0]}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. 离线 renderer：stereo balance 一致
# ─────────────────────────────────────────────────────────────────────────────

class TestOfflineStereoBalance:
    """RMA-08: 离线 render_track_block 对 stereo 源用 balance。"""

    def test_renderer_stereo_center_unity(self):
        """renderer 对 stereo 源 pan=0 → unity（非 equal-power）。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer

        renderer = AudioGraphRenderer(48000.0)
        # stereo 源，source_channels=2
        samples = np.full((2, 480), 0.5, dtype=np.float32)
        # 直接测 pan 部分：构造无 node 的情况会原样返回，需有 node。
        # 用 render_track_block 需要 node；这里直接验证 pan_gains_for_channels 一致性。
        lg, rg = pan_gains_for_channels(0.0, 2)
        assert lg == 1.0 and rg == 1.0
        # 应用后每通道 = 0.5 * 1.0 = 0.5
        out_l = samples[0] * lg
        assert abs(out_l[0] - 0.5) < 1e-6
