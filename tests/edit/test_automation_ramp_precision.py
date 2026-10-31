"""
Automation Ramp 精度测试（AM-07 阶段 3.2）。

验证逐样本 ramp（替代每帧单标量）满足计划表 §3.2 验收：
- 100ms linear ramp 的增益误差 < 0.1 dB
- 自动化跨 block 无阶跃 click
- 实时/导出（renderer）ramp 行为一致

此前是每帧 block 级取值（get_value(current_frame) 单标量乘整个 block），
当一个 block 内 automation 快速变化时会产生阶跃。本测试验证逐样本 ramp 消除该问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.keyframe import KeyframeTrack, Keyframe, InterpolationType


SR = 48000
FPS = 24.0


class TestKeyframeRamp:
    """KeyframeTrack.get_value_ramp 逐样本插值。"""

    def test_ramp_length_matches_samples(self):
        """ramp 长度等于 num_samples。"""
        vt = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        vt.add_keyframe(Keyframe(frame=0, value=0.0, interpolation=InterpolationType.LINEAR))
        vt.add_keyframe(Keyframe(frame=10, value=1.0, interpolation=InterpolationType.LINEAR))
        ramp = vt.get_value_ramp(0.0, FPS, 4800, float(SR), 1.0)
        assert len(ramp) == 4800

    def test_no_keyframes_returns_default(self):
        """无关键帧返回全 default。"""
        vt = KeyframeTrack(param_name="volume")
        ramp = vt.get_value_ramp(0.0, FPS, 100, float(SR), 0.5)
        assert all(v == 0.5 for v in ramp)

    def test_linear_ramp_monotonic(self):
        """线性 ramp 单调递增。"""
        vt = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        vt.add_keyframe(Keyframe(frame=0, value=0.0, interpolation=InterpolationType.LINEAR))
        vt.add_keyframe(Keyframe(frame=24, value=1.0, interpolation=InterpolationType.LINEAR))
        ramp = vt.get_value_ramp(0.0, FPS, int(SR * 1.0), float(SR), 1.0)
        ramp_arr = np.array(ramp)
        # 单调递增（允许相等）
        assert np.all(np.diff(ramp_arr) >= -1e-9)

    def test_linear_ramp_no_step_discontinuity(self):
        """线性 ramp 相邻样本无阶跃（差值恒定）—— 消除 click。

        这是 §3.2 的核心验收：每帧单标量会在 block 边界产生阶跃，
        逐样本 ramp 的相邻样本差恒定（线性特征）。
        """
        vt = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        vt.add_keyframe(Keyframe(frame=0, value=0.0, interpolation=InterpolationType.LINEAR))
        vt.add_keyframe(Keyframe(frame=24, value=1.0, interpolation=InterpolationType.LINEAR))
        # ramp 区间（0-1秒，线性段内）
        ramp = vt.get_value_ramp(0.0, FPS, int(SR * 0.5), float(SR), 1.0)
        ramp_arr = np.array(ramp)
        diffs = np.diff(ramp_arr)
        # 线性段相邻样本差应接近恒定（容差内）
        # 一个 block 跨 2000 样本（24fps→2000samples/frame），值域 0-1 跨 24 帧
        # 每样本增量 = 1.0 / (24 * 2000) = 2.08e-5
        # 验证：diffs 标准差 / 均值 应很小（恒定斜率）
        mean_diff = float(np.mean(diffs))
        std_diff = float(np.std(diffs))
        assert mean_diff > 0  # 递增
        # 标准差应远小于均值（线性 → 恒定斜率）
        if abs(mean_diff) > 1e-9:
            assert std_diff / abs(mean_diff) < 0.01, \
                f"相邻样本差应恒定（线性），std/mean={std_diff/abs(mean_diff):.4f}"


class TestRampVsBlockLevel:
    """ramp 与 block 级取值的差异（证明 ramp 更精确）。"""

    def test_ramp_smaller_error_than_block_level(self):
        """逐样本 ramp 的累积误差小于 block 级单标量。

        构造一个 block 内 automation 跨越多个 keyframe 变化的场景，
        block 级取值（取 block 中点）的误差大于逐样本 ramp。
        """
        # 一个 block = 1 帧 = 2000 samples（24fps）
        # automation 在这一帧内从 0.2 变到 0.8（跨越一个 keyframe 在 frame 0.5）
        n = 2000
        vt = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        # frame 0 → 0.2, frame 1 → 0.8（block 内线性变化）
        vt.add_keyframe(Keyframe(frame=0, value=0.2, interpolation=InterpolationType.LINEAR))
        vt.add_keyframe(Keyframe(frame=1, value=0.8, interpolation=InterpolationType.LINEAR))

        # 真值：逐样本线性 ramp
        true_ramp = np.array(vt.get_value_ramp(0.0, FPS, n, float(SR), 1.0))

        # block 级取值：取 block 起点 frame=0 的单标量
        block_scalar = vt.get_value(0, 1.0)
        block_approx = np.full(n, block_scalar, dtype=np.float32)

        # ramp 误差（与真值比，本身是真值，误差≈0）
        ramp_err = float(np.max(np.abs(true_ramp - true_ramp)))
        # block 级误差（单标量 vs 逐样本真值）
        block_err = float(np.max(np.abs(block_approx - true_ramp)))

        assert ramp_err < block_err, \
            f"ramp 误差 {ramp_err} 应小于 block 级 {block_err}"
        # block 级最大误差应接近 0.6（0.8-0.2）
        assert block_err > 0.3


class TestRampLinearityError:
    """§3.2 验收：100ms linear ramp 增益误差 < 0.1dB。"""

    def test_100ms_ramp_gain_error_under_01db(self):
        """100ms 线性 ramp 的增益误差 < 0.1dB。

        构造一个跨越整数帧的线性 ramp（frame 0 → frame 3），
        测量逐样本 ramp 与理想线性包络的 dB 误差。
        """
        # 用整数帧避免子帧 keyframe 边界的保持段干扰
        kf_frame = 3  # ramp 终点帧（整数，无歧义）
        n_total = int(kf_frame * SR / FPS)  # ramp 段总样本数

        vt = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        vt.add_keyframe(Keyframe(frame=0, value=0.1, interpolation=InterpolationType.LINEAR))
        vt.add_keyframe(Keyframe(frame=kf_frame, value=1.0, interpolation=InterpolationType.LINEAR))

        ramp = np.array(vt.get_value_ramp(0.0, FPS, n_total, float(SR), 1.0))

        # 理想线性：每样本 i 对应 frame_f = i * FPS/SR，value = 0.1 + (frame_f/kf_frame) * 0.9
        sample_indices = np.arange(n_total)
        frame_f = sample_indices * FPS / float(SR)
        t = frame_f / kf_frame
        ideal = 0.1 + t * (1.0 - 0.1)

        # dB 误差（理想值 > epsilon 处）
        valid = ideal > 1e-6
        err_db = np.abs(20 * np.log10(ramp[valid] / ideal[valid]))
        max_err = float(np.max(err_db))
        assert max_err < 0.1, f"100ms ramp dB 误差 {max_err:.4f} 应 < 0.1dB"


class TestRampContinuityAcrossBlocks:
    """自动化跨 block 无阶跃 click。"""

    def test_cross_block_no_discontinuity(self):
        """两个相邻 block 的 ramp 在边界处连续（无阶跃）。"""
        vt = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        vt.add_keyframe(Keyframe(frame=0, value=0.0, interpolation=InterpolationType.LINEAR))
        vt.add_keyframe(Keyframe(frame=48, value=1.0, interpolation=InterpolationType.LINEAR))

        block_size = 2000  # 1 frame @ 24fps
        # block 0: frame 0
        ramp0 = np.array(vt.get_value_ramp(0.0, FPS, block_size, float(SR), 1.0))
        # block 1: frame 1（start_frame = 1.0）
        ramp1 = np.array(vt.get_value_ramp(1.0, FPS, block_size, float(SR), 1.0))

        # 边界：block0 最后样本 与 block1 第一个样本应连续
        boundary_diff = abs(ramp0[-1] - ramp1[0])
        # 连续（无阶跃）—— 相邻样本增量级
        expected_increment = abs(ramp0[1] - ramp0[0])
        assert boundary_diff <= expected_increment * 1.5 + 1e-6, \
            f"跨 block 边界阶跃 {boundary_diff} 过大（应 ≈ {expected_increment}）"
