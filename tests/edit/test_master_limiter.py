"""
Master True-Peak Limiter 测试（AM-09 阶段 2.5）。

验证连续状态 MasterLimiter 替代无状态逐 buffer 峰值归一化：
- 跨 buffer 增益连续平滑（无 block-boundary gain jump / pumping）
- 超阈值时衰减，信号回落时恢复
- 输出不削波（≤ 阈值）
- 连续正弦/瞬态无增益不连续

验收标准（计划表 AM-09）：
- 连续正弦和瞬态测试无 block-boundary gain jump
- 实时/导出波形一致（两路径用同一 limiter）
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import MasterLimiter


SR = 48000.0


class TestMasterLimiterBasics:
    """MasterLimiter 基本行为。"""

    def test_below_threshold_no_change(self):
        """信号低于阈值时不衰减。"""
        lim = MasterLimiter(SR, threshold=1.0)
        sig = np.ones((2, 480), dtype=np.float32) * 0.5
        out = lim.process(sig.copy())
        # 远低于阈值，增益≈1.0，输出≈输入
        assert np.allclose(out, sig, atol=1e-5)

    def test_above_threshold_no_clip(self):
        """超阈值信号输出不削波（≤ 阈值）。"""
        lim = MasterLimiter(SR, threshold=1.0)
        sig = np.ones((2, 480), dtype=np.float32) * 1.5
        out = lim.process(sig.copy())
        assert np.max(np.abs(out)) <= 1.0 + 1e-5

    def test_reset_restores_gain(self):
        """reset 后增益状态恢复为 1.0。"""
        lim = MasterLimiter(SR, threshold=1.0)
        lim.process(np.ones((2, 480), dtype=np.float32) * 1.5)
        assert lim._gain < 1.0  # 已衰减
        lim.reset()
        assert lim._gain == 1.0


class TestContinuousState:
    """连续状态：跨 buffer 增益平滑（AM-09 核心）。"""

    def test_gain_continuous_across_blocks(self):
        """连续超阈值 block 的增益应连续变化（非每 block 重置）。"""
        lim = MasterLimiter(SR, threshold=1.0, attack_ms=1, release_ms=100)
        sig = np.ones((2, 480), dtype=np.float32) * 1.5
        lim.process(sig.copy())
        gain_after_b1 = lim._gain
        lim.process(sig.copy())
        gain_after_b2 = lim._gain
        # 增益应继续衰减（连续状态，非重置后再 attack）
        assert gain_after_b2 < gain_after_b1 + 1e-6, \
            "增益应跨 block 连续衰减，而非每 block 重新 attack"

    def test_release_recovers_gain(self):
        """信号回落后增益应缓慢恢复（release）。"""
        lim = MasterLimiter(SR, threshold=1.0, attack_ms=1, release_ms=50)
        # 先衰减
        loud = np.ones((2, 480), dtype=np.float32) * 1.5
        lim.process(loud.copy())
        assert lim._gain < 0.95
        # 再喂静音信号，增益应恢复
        quiet = np.ones((2, 4800), dtype=np.float32) * 0.3  # 较长以观察 release
        lim.process(quiet.copy())
        assert lim._gain > 0.9, f"release 后增益应恢复，实际 {lim._gain}"

    def test_no_block_boundary_gain_jump(self):
        """AM-09 验收：连续正弦无 block-boundary gain jump（pumping）。

        喂连续正弦（恒定幅度），分多个 block 处理，检查相邻 block 边界
        处的增益变化是否平滑（无突变）。
        """
        lim = MasterLimiter(SR, threshold=1.0, attack_ms=2, release_ms=100)
        # 连续正弦，幅度刚好超阈值
        total = 480 * 10  # 10 个 block
        t = np.arange(total) / SR
        sig = (1.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        # 分 block 处理
        block_size = 480
        outputs = []
        for i in range(0, total, block_size):
            block = sig[i:i + block_size]
            out = lim.process(block.reshape(1, -1))
            outputs.append(out[0])
        full_out = np.concatenate(outputs)
        # 检查 block 边界处的样本连续性（相邻 block 交界样本差值小）
        for boundary in range(block_size, total - block_size, block_size):
            jump = abs(full_out[boundary - 1] - full_out[boundary])
            # 连续状态下边界 jump 应小（增益平滑过渡）
            assert jump < 0.15, \
                f"block 边界 {boundary} jump={jump:.4f} 过大（pumping）"


class TestParityWithMixer:
    """limiter 在实时/导出两路径行为一致。"""

    def test_mixer_and_renderer_use_same_limiter_behavior(self, qapp):
        """实时 mix_samples 与 renderer.apply_master 的限幅行为一致。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

        # 两轨大信号叠加（超阈值）
        amp = 0.8
        class DC:
            def decode_samples_for_frame(self, f, fps, n):
                s = np.full(n, amp, dtype=np.float32)
                return np.stack([s, s])
            def close(self): pass

        mixer = AudioMixer()
        mixer.add_track("t1", DC(), AudioTrackState(track_id="t1"))
        mixer.add_track("t2", DC(), AudioTrackState(track_id="t2"))
        rt, _ = mixer.mix_samples(0, 24.0, 480, 2)

        # renderer 同配置
        t1 = EditTrack(track_id="t1", track_type="audio", index=0)
        t2 = EditTrack(track_id="t2", track_type="audio", index=1)
        seq = TimelineSequence(audio_tracks=(t1, t2))
        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(seq)
        out = np.zeros((2, 480), dtype=np.float32)
        for tid in ("t1", "t2"):
            s = np.full((2, 480), amp, dtype=np.float32)
            out += r.render_track_block(tid, s, 0)
        out = r.apply_master(out)

        # 两路径输出一致（limiter 相同行为）
        assert np.allclose(rt, out, atol=1e-5), \
            f"实时 {rt[0][0]} vs renderer {out[0][0]}"
        # 都不削波
        assert np.max(np.abs(rt)) <= 1.0 + 1e-5
        assert np.max(np.abs(out)) <= 1.0 + 1e-5
