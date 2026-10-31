"""
Clip 音频淡化测试（AM-06 阶段 2.3）。

验证 clip 淡入/淡出包络的数学正确性（linear + equal-power），
以及共享 renderer 对 clip fade 的应用。

验收标准（计划表 2.3）：
- 固定时长淡入/淡出测量每 10ms gain 符合曲线
- equal-power crossfade 中合成功率波动 ≤0.2 dB
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    apply_clip_fade,
    equal_power_fade_envelope,
    linear_fade_envelope,
)


class TestLinearFadeEnvelope:
    """线性淡化包络。"""

    def test_fade_in_zero_to_one(self):
        """淡入从 0 线性升到 1。"""
        env = linear_fade_envelope(100, fade_in_samples=100)
        assert abs(env[0]) < 1e-9
        assert abs(env[50] - 0.5) < 0.01
        assert abs(env[99] - 1.0) < 0.01

    def test_fade_out_one_to_zero(self):
        """淡出从 1 线性降到 0。"""
        env = linear_fade_envelope(100, fade_out_samples=100)
        assert abs(env[0] - 1.0) < 0.01
        assert abs(env[99]) < 1e-9

    def test_no_fade_all_ones(self):
        """无淡化时包络全 1。"""
        env = linear_fade_envelope(480)
        assert np.allclose(env, 1.0)

    def test_middle_unaffected(self):
        """淡入淡出之间的中段保持 1.0。"""
        env = linear_fade_envelope(480, fade_in_samples=100, fade_out_samples=100)
        assert np.allclose(env[150:330], 1.0)


class TestEqualPowerFadeEnvelope:
    """等功率淡化包络（AM-06 验收核心）。"""

    def test_fade_in_sine_curve(self):
        """淡入用 sin 曲线：0→1。"""
        env = equal_power_fade_envelope(100, fade_in_samples=100)
        assert abs(env[0]) < 1e-6
        assert abs(env[99] - 1.0) < 0.01
        # 中点 ≈ sin(45°) ≈ 0.707（-3dB，等功率特征）
        assert abs(env[50] - np.sin(np.pi / 4)) < 0.02

    def test_fade_out_cosine_curve(self):
        """淡出用 cos 曲线：1→0。"""
        env = equal_power_fade_envelope(100, fade_out_samples=100)
        assert abs(env[0] - 1.0) < 0.01
        assert abs(env[99]) < 1e-6

    def test_crossfade_constant_power(self):
        """AM-06 验收：等功率交叉淡化合成功率波动 ≤0.2 dB。

        淡入 env_in 与淡出 env_out 交叉时，sin²+cos²=1（恒功率）。
        """
        n = 1000
        env_in = equal_power_fade_envelope(n, fade_in_samples=n)
        env_out = equal_power_fade_envelope(n, fade_out_samples=n)
        power = env_in ** 2 + env_out ** 2
        # 功率应恒为 1.0（波动 0 dB）
        power_db = 10 * np.log10(np.maximum(power, 1e-12))
        max_deviation = float(np.max(np.abs(power_db)))
        assert max_deviation <= 0.2, \
            f"等功率交叉功率波动 {max_deviation:.3f} dB > 0.2 dB"


class TestApplyClipFade:
    """apply_clip_fade 在样本块上应用淡化。"""

    def test_fade_in_at_clip_start(self):
        """clip 开头的块应用淡入（首样本≈0）。"""
        samples = np.ones((2, 480), dtype=np.float32) * 0.5
        # clip 0-100帧，淡入10帧（10*2000=20000样本/帧=2000样本）
        out = apply_clip_fade(samples, 0, 100, current_frame=0,
                              fade_in_frames=10, fade_out_frames=0,
                              fps=24.0, sample_rate=48000.0)
        # 首样本应接近 0（淡入起点）
        assert abs(out[0][0]) < 0.05

    def test_no_fade_outside_fade_region(self):
        """不在淡化区域的块不受影响。"""
        samples = np.ones((2, 480), dtype=np.float32) * 0.5
        # clip 0-100帧，淡入10帧；当前帧50（淡入结束后）
        out = apply_clip_fade(samples, 0, 100, current_frame=50,
                              fade_in_frames=10, fade_out_frames=0,
                              fps=24.0, sample_rate=48000.0)
        assert np.allclose(out, 0.5, atol=1e-5)

    def test_fade_out_at_clip_end(self):
        """clip 结尾的块应用淡出（末样本≈0）。"""
        samples = np.ones((2, 480), dtype=np.float32) * 0.5
        # clip 0-100帧，淡出10帧；当前帧95（淡出区）
        out = apply_clip_fade(samples, 0, 100, current_frame=95,
                              fade_in_frames=0, fade_out_frames=10,
                              fps=24.0, sample_rate=48000.0)
        # 块内应衰减（接近 clip 结尾）
        assert out[0][479] < out[0][0]

    def test_equal_power_option(self):
        """apply_clip_fade 支持等功率包络。"""
        from src.features.edit.core.audio_dsp import equal_power_fade_envelope
        samples = np.ones((2, 480), dtype=np.float32) * 0.5
        out = apply_clip_fade(samples, 0, 100, current_frame=0,
                              fade_in_frames=10, fade_out_frames=0,
                              fps=24.0, sample_rate=48000.0,
                              envelope_fn=equal_power_fade_envelope)
        # 等功率淡入中段应 > 线性（sin > linear 在中段）
        out_linear = apply_clip_fade(samples, 0, 100, current_frame=0,
                                     fade_in_frames=10, fade_out_frames=0,
                                     fps=24.0, sample_rate=48000.0,
                                     envelope_fn=linear_fade_envelope)
        # 在淡入中段，等功率包络值更大
        assert out[0][100] >= out_linear[0][100] - 1e-6

    def test_fade_in_is_continuous_across_audio_blocks(self):
        """淡入不能在每个音频块边界重新从 0 开始。"""
        samples = np.ones((2, 480), dtype=np.float32)
        first = apply_clip_fade(
            samples.copy(), 0, 100, current_frame=0,
            fade_in_frames=5, fade_out_frames=0,
            fps=24.0, sample_rate=48000.0,
        )
        second = apply_clip_fade(
            samples.copy(), 0, 100, current_frame=1,
            fade_in_frames=5, fade_out_frames=0,
            fps=24.0, sample_rate=48000.0,
        )

        assert second[0][0] > first[0][-1]
        assert second[0][0] > 0.0

    def test_fade_out_is_continuous_across_audio_blocks(self):
        """淡出也应按片段绝对位置持续下降。"""
        samples = np.ones((2, 480), dtype=np.float32)
        first = apply_clip_fade(
            samples.copy(), 0, 100, current_frame=96,
            fade_in_frames=0, fade_out_frames=5,
            fps=24.0, sample_rate=48000.0,
        )
        second = apply_clip_fade(
            samples.copy(), 0, 100, current_frame=97,
            fade_in_frames=0, fade_out_frames=5,
            fps=24.0, sample_rate=48000.0,
        )

        assert second[0][0] < first[0][-1]


class TestRendererClipFade:
    """共享 renderer 应用 clip fade（导出路径）。"""

    def test_renderer_applies_clip_fade(self):
        """renderer.render_track_block 传入 clip_fade 时应用淡化。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

        track = EditTrack(track_id="t1", track_type="audio", index=0, volume=1.0)
        seq = TimelineSequence(audio_tracks=(track,))
        r = AudioGraphRenderer(48000.0)
        r.configure_from_sequence(seq)

        samples = np.ones((2, 480), dtype=np.float32) * 0.5
        # 无淡化
        no_fade = r.render_track_block("t1", samples.copy(), 0)
        # 有淡入
        clip_fade = {
            "timeline_start": 0, "timeline_duration": 100,
            "fade_in_frames": 10, "fade_out_frames": 0, "fps": 24.0,
        }
        with_fade = r.render_track_block("t1", samples.copy(), 0, clip_fade=clip_fade)

        # 淡入应使首样本衰减
        assert with_fade[0][0] < no_fade[0][0]
        # 480 samples 只覆盖淡入的开头一小段，不能提前到满幅。
        assert with_fade[0][0] < with_fade[0][479] < no_fade[0][479]
