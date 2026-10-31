"""
Channel Layout & Downmix 测试（AM-08 阶段 4.1，声道映射）。

验证（计划表 4.1）：
1. ChannelLayout 枚举：声道数/标签/PyAV 名称正确。
2. downmix_matrix：标准 ITUR BS.775-1 系数。
3. 逐声道 impulse fixture 验证每一目标声道系数。
4. downmix 执行：声道数/类型正确。
5. adapt_channels：替代 naive 截断，保留 Center/LFE/Surround 贡献。
6. BS.1770-4 channel weights：LFE 排除，环绕声道加权。
7. 拒绝未知 mapping（抛 ValueError 而非截断）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.channel_layout import (
    ChannelLayout,
    downmix_matrix,
    downmix,
    adapt_channels,
    bs1770_channel_weights,
)


SQRT_HALF = float(np.sqrt(0.5))


class TestChannelLayoutEnum:
    """ChannelLayout 枚举。"""

    def test_channel_counts(self):
        assert ChannelLayout.MONO.channel_count == 1
        assert ChannelLayout.STEREO.channel_count == 2
        assert ChannelLayout.SURROUND_5_1.channel_count == 6
        assert ChannelLayout.SURROUND_7_1.channel_count == 8

    def test_channel_labels_51(self):
        labels = ChannelLayout.SURROUND_5_1.channel_labels
        assert labels == ("L", "R", "C", "LFE", "Ls", "Rs")

    def test_channel_labels_71(self):
        labels = ChannelLayout.SURROUND_7_1.channel_labels
        assert labels == ("L", "R", "C", "LFE", "Ls", "Rs", "Lss", "Rss")

    def test_av_layout_names(self):
        assert ChannelLayout.MONO.av_layout_name == "mono"
        assert ChannelLayout.STEREO.av_layout_name == "stereo"
        assert ChannelLayout.SURROUND_5_1.av_layout_name == "5.1"
        assert ChannelLayout.SURROUND_7_1.av_layout_name == "7.1"

    def test_from_channel_count(self):
        assert ChannelLayout.from_channel_count(1) == ChannelLayout.MONO
        assert ChannelLayout.from_channel_count(2) == ChannelLayout.STEREO
        assert ChannelLayout.from_channel_count(6) == ChannelLayout.SURROUND_5_1
        assert ChannelLayout.from_channel_count(8) == ChannelLayout.SURROUND_7_1

    def test_from_channel_count_unknown_raises(self):
        """未知声道数抛 ValueError（拒绝而非截断）。"""
        with pytest.raises(ValueError):
            ChannelLayout.from_channel_count(3)
        with pytest.raises(ValueError):
            ChannelLayout.from_channel_count(4)
        with pytest.raises(ValueError):
            ChannelLayout.from_channel_count(7)

    def test_from_av_layout_name(self):
        assert ChannelLayout.from_av_layout_name("mono") == ChannelLayout.MONO
        assert ChannelLayout.from_av_layout_name("stereo") == ChannelLayout.STEREO
        assert ChannelLayout.from_av_layout_name("5.1") == ChannelLayout.SURROUND_5_1
        assert ChannelLayout.from_av_layout_name("5.1(side)") == ChannelLayout.SURROUND_5_1
        assert ChannelLayout.from_av_layout_name("7.1") == ChannelLayout.SURROUND_7_1

    def test_from_av_layout_name_unknown_raises(self):
        with pytest.raises(ValueError):
            ChannelLayout.from_av_layout_name("quad")


class TestDownmixMatrix:
    """downmix 矩阵系数正确性。"""

    def test_identity_for_same_layout(self):
        """同布局为单位矩阵。"""
        for layout in ChannelLayout:
            m = downmix_matrix(layout, layout)
            np.testing.assert_array_almost_equal(m, np.eye(layout.channel_count))

    def test_51_to_stereo_shape(self):
        m = downmix_matrix(ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert m.shape == (2, 6)

    def test_51_to_stereo_center_coefficient(self):
        """Center 系数为 sqrt(0.5) (-3 dB)，Lo/Ro 均等。"""
        m = downmix_matrix(ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        # C 是 src 第 3 声道（index 2）
        assert abs(m[0, 2] - SQRT_HALF) < 1e-9
        assert abs(m[1, 2] - SQRT_HALF) < 1e-9

    def test_51_to_stereo_lfe_excluded_by_default(self):
        """默认 LFE 不参与 downmix（include_lfe=False）。"""
        m = downmix_matrix(ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert m[0, 3] == 0.0  # LFE (index 3)
        assert m[1, 3] == 0.0

    def test_51_to_stereo_lfe_included(self):
        """include_lfe=True 时 LFE 系数 sqrt(0.5)。"""
        m = downmix_matrix(
            ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO, include_lfe=True,
        )
        assert abs(m[0, 3] - SQRT_HALF) < 1e-9
        assert abs(m[1, 3] - SQRT_HALF) < 1e-9

    def test_51_to_stereo_surround_coefficients(self):
        """Ls→Lo, Rs→Ro 各 sqrt(0.5)。"""
        m = downmix_matrix(ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        # Ls (index 4) → Lo
        assert abs(m[0, 4] - SQRT_HALF) < 1e-9
        assert m[0, 5] == 0.0  # Rs 不进 Lo
        # Rs (index 5) → Ro
        assert abs(m[1, 5] - SQRT_HALF) < 1e-9
        assert m[1, 4] == 0.0  # Ls 不进 Ro

    def test_stereo_to_mono_equal_power(self):
        """stereo→mono 等功率（0.5/0.5）。"""
        m = downmix_matrix(ChannelLayout.STEREO, ChannelLayout.MONO)
        assert m.shape == (1, 2)
        assert abs(m[0, 0] - 0.5) < 1e-9
        assert abs(m[0, 1] - 0.5) < 1e-9

    def test_mono_to_stereo_copy(self):
        """mono→stereo 复制（不衰减）。"""
        m = downmix_matrix(ChannelLayout.MONO, ChannelLayout.STEREO)
        assert m[0, 0] == 1.0
        assert m[1, 0] == 1.0

    def test_71_to_51_side_surround_merge(self):
        """7.1→5.1：侧环绕 (Lss/Rss) 以 sqrt(0.5) 并入 L/R。"""
        m = downmix_matrix(ChannelLayout.SURROUND_7_1, ChannelLayout.SURROUND_5_1)
        assert m.shape == (6, 8)
        # L' = L + 0.707*Lss
        assert m[0, 0] == 1.0  # L 直通
        assert abs(m[0, 6] - SQRT_HALF) < 1e-9  # Lss (index 6) → L
        # R' = R + 0.707*Rss
        assert m[1, 1] == 1.0
        assert abs(m[1, 7] - SQRT_HALF) < 1e-9  # Rss (index 7) → R
        # C/LFE/Ls/Rs 直通
        assert m[2, 2] == 1.0
        assert m[3, 3] == 1.0
        assert m[4, 4] == 1.0
        assert m[5, 5] == 1.0

    def test_unsupported_upmix_raises(self):
        """upmix（如 stereo→5.1）抛 ValueError。"""
        with pytest.raises(ValueError):
            downmix_matrix(ChannelLayout.STEREO, ChannelLayout.SURROUND_5_1)
        with pytest.raises(ValueError):
            downmix_matrix(ChannelLayout.MONO, ChannelLayout.SURROUND_5_1)


class TestImpulseResponse:
    """逐声道 impulse fixture 验证每一目标声道系数（计划表 4.1 核心验收）。"""

    def test_center_impulse_to_stereo(self):
        """C-only 信号 → Lo/Ro 均等 sqrt(0.5)。"""
        impulse = np.zeros((6, 100), dtype=np.float32)
        impulse[2, :] = 1.0  # C
        out = downmix(impulse, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        np.testing.assert_allclose(out[0, :], np.full(100, SQRT_HALF), atol=1e-5)
        np.testing.assert_allclose(out[1, :], np.full(100, SQRT_HALF), atol=1e-5)

    def test_left_impulse_to_stereo(self):
        """L-only 信号 → Lo=1.0, Ro=0。"""
        impulse = np.zeros((6, 100), dtype=np.float32)
        impulse[0, :] = 1.0  # L
        out = downmix(impulse, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        np.testing.assert_allclose(out[0, :], 1.0, atol=1e-5)
        np.testing.assert_allclose(out[1, :], 0.0, atol=1e-5)

    def test_right_impulse_to_stereo(self):
        """R-only 信号 → Lo=0, Ro=1.0。"""
        impulse = np.zeros((6, 100), dtype=np.float32)
        impulse[1, :] = 1.0  # R
        out = downmix(impulse, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        np.testing.assert_allclose(out[0, :], 0.0, atol=1e-5)
        np.testing.assert_allclose(out[1, :], 1.0, atol=1e-5)

    def test_lfe_impulse_excluded_by_default(self):
        """LFE-only 信号 → 默认被排除（Lo/Ro=0）。"""
        impulse = np.zeros((6, 100), dtype=np.float32)
        impulse[3, :] = 1.0  # LFE
        out = downmix(impulse, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        np.testing.assert_allclose(out[0, :], 0.0, atol=1e-5)
        np.testing.assert_allclose(out[1, :], 0.0, atol=1e-5)

    def test_lfe_impulse_included(self):
        """LFE-only 信号 → include_lfe=True 时 Lo/Ro=sqrt(0.5)。"""
        impulse = np.zeros((6, 100), dtype=np.float32)
        impulse[3, :] = 1.0  # LFE
        out = downmix(
            impulse, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO, include_lfe=True,
        )
        np.testing.assert_allclose(out[0, :], SQRT_HALF, atol=1e-5)
        np.testing.assert_allclose(out[1, :], SQRT_HALF, atol=1e-5)

    def test_left_surround_impulse_to_stereo(self):
        """Ls-only 信号 → Lo=sqrt(0.5), Ro=0。"""
        impulse = np.zeros((6, 100), dtype=np.float32)
        impulse[4, :] = 1.0  # Ls
        out = downmix(impulse, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        np.testing.assert_allclose(out[0, :], SQRT_HALF, atol=1e-5)
        np.testing.assert_allclose(out[1, :], 0.0, atol=1e-5)

    def test_right_surround_impulse_to_stereo(self):
        """Rs-only 信号 → Lo=0, Ro=sqrt(0.5)。"""
        impulse = np.zeros((6, 100), dtype=np.float32)
        impulse[5, :] = 1.0  # Rs
        out = downmix(impulse, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        np.testing.assert_allclose(out[0, :], 0.0, atol=1e-5)
        np.testing.assert_allclose(out[1, :], SQRT_HALF, atol=1e-5)

    def test_all_channels_preserved_in_mix(self):
        """全声道信号 downmix 后能量合理（不丢失声道）。"""
        impulse = np.full((6, 1000), 0.5, dtype=np.float32)
        out = downmix(impulse, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        # naive 截断会丢失 C/LFE/Ls/Rs，正确 downmix 应含全部贡献
        # Lo = 0.5*(1 + 0.707 + 0 + 0.707) = 0.5*2.414 = 1.207
        expected_lo = 0.5 * (1.0 + SQRT_HALF + 0.0 + SQRT_HALF)
        np.testing.assert_allclose(out[0, 0], expected_lo, atol=1e-4)


class TestDownmixExecution:
    """downmix 执行正确性。"""

    def test_downmix_preserves_dtype(self):
        """downmix 保持 dtype。"""
        samples = np.zeros((6, 100), dtype=np.float32)
        out = downmix(samples, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert out.dtype == np.float32
        assert out.shape == (2, 100)

    def test_downmix_1d_input(self):
        """1D 输入自动 reshape 为 (1, n)。"""
        samples = np.full(100, 0.5, dtype=np.float32)
        out = downmix(samples, ChannelLayout.MONO, ChannelLayout.STEREO)
        assert out.shape == (2, 100)

    def test_downmix_channel_count_mismatch_raises(self):
        """声道数与 layout 不匹配抛 ValueError。"""
        samples = np.zeros((4, 100), dtype=np.float32)  # 4 声道，非标准
        with pytest.raises(ValueError):
            downmix(samples, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)

    def test_71_to_stereo_via_51(self):
        """7.1→stereo（经 5.1 中转）正确。"""
        impulse = np.zeros((8, 100), dtype=np.float32)
        impulse[2, :] = 1.0  # C
        out = downmix(impulse, ChannelLayout.SURROUND_7_1, ChannelLayout.STEREO)
        np.testing.assert_allclose(out[0, :], SQRT_HALF, atol=1e-5)
        np.testing.assert_allclose(out[1, :], SQRT_HALF, atol=1e-5)


class TestAdaptChannels:
    """adapt_channels 替代 naive 截断。"""

    def test_same_channel_count_passthrough(self):
        """声道数匹配时直接返回。"""
        samples = np.random.rand(2, 100).astype(np.float32)
        out = adapt_channels(samples, 2)
        assert out is samples or np.array_equal(out, samples)

    def test_51_to_stereo(self):
        """5.1→stereo 正确 downmix（不截断）。"""
        samples = np.zeros((6, 100), dtype=np.float32)
        samples[2, :] = 1.0  # C
        out = adapt_channels(samples, 2)
        assert out.shape == (2, 100)
        # C 贡献到 Lo/Ro
        assert abs(out[0, 0] - SQRT_HALF) < 1e-5

    def test_stereo_to_mono(self):
        """stereo→mono。"""
        samples = np.array([[1.0], [0.5]], dtype=np.float32)
        out = adapt_channels(samples, 1)
        assert out.shape == (1, 1)
        assert abs(out[0, 0] - 0.75) < 1e-5

    def test_naive_truncation_bug_fixed(self):
        """验证 adapt_channels 不再用 naive 截断（保留 Center 贡献）。

        naive 截断 samples[:2] 只取 L/R，丢弃 C。正确 downmix 应含 C 贡献。
        """
        # C-only 信号：naive 截断得 [0, 0]（静音），正确 downmix 得 [0.707, 0.707]
        samples = np.zeros((6, 50), dtype=np.float32)
        samples[2, :] = 1.0  # C
        out = adapt_channels(samples, 2)
        # naive 截断会是 0，正确应是 0.707
        assert float(np.max(np.abs(out))) > 0.5, "Center 贡献不应被丢弃"


class TestBS1770Weights:
    """BS.1770-4 声道权重（响度计算用）。"""

    def test_stereo_weights(self):
        w = bs1770_channel_weights(ChannelLayout.STEREO)
        np.testing.assert_array_almost_equal(w, [1.0, 1.0])

    def test_51_weights(self):
        w = bs1770_channel_weights(ChannelLayout.SURROUND_5_1)
        # [L, R, C, LFE, Ls, Rs]
        # AUD-16100: BS.1770-4 环绕声道权重为 1.41（+3 dB，对称），
        # 旧断言 0.59/0.41 锁定的是实现错误（锁定旧错误行为的合法翻转）。
        np.testing.assert_array_almost_equal(w, [1.0, 1.0, 1.0, 0.0, 1.41, 1.41])

    def test_71_weights(self):
        w = bs1770_channel_weights(ChannelLayout.SURROUND_7_1)
        # [L, R, C, LFE, Ls, Rs, Lss, Rss]
        np.testing.assert_array_almost_equal(
            w, [1.0, 1.0, 1.0, 0.0, 1.41, 1.41, 1.41, 1.41])

    def test_lfe_excluded(self):
        """LFE 权重为 0（排除）。"""
        w51 = bs1770_channel_weights(ChannelLayout.SURROUND_5_1)
        assert w51[3] == 0.0  # LFE
        w71 = bs1770_channel_weights(ChannelLayout.SURROUND_7_1)
        assert w71[3] == 0.0  # LFE

    def test_surround_weighted(self):
        """环绕声道加权（AUD-16100：BS.1770-4 规定 1.41，左右对称）。"""
        w = bs1770_channel_weights(ChannelLayout.SURROUND_5_1)
        assert abs(w[4] - 1.41) < 1e-9  # Ls
        assert abs(w[5] - 1.41) < 1e-9  # Rs
