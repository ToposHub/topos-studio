"""MX-15 回归：布局能力矩阵与逐声道脉冲全扫（音频成熟化计划 阶段F）。

验收（计划 §5 MX-15）：
- 每个目标声道编号/语义正确，无交换 Center 与 LFE；
- 只选 C 轨或 5.1(side) 等具体布局不退化成盲目截前两路；
- 未知布局不静默丢声道（能力查询给出原因，回退有日志）；
- 预览与导出使用同一转换矩阵（downmix_matrix 单一真源）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.channel_layout import (
    ChannelLayout,
    downmix,
    downmix_matrix,
    resolve_export_channels,
)
from src.features.edit.core.layout_capability import (
    StageCapability,
    all_stages,
    downmix_supported,
    layout_supported,
    stage_capability,
    supported_downmix_pairs,
    unsupported_reason,
)


class TestMx15CapabilityMatrix:
    def test_all_stages_declared(self):
        assert set(all_stages()) >= {
            "source_decode", "clip_stage", "track_dsp", "bus_master",
            "export"}

    def test_track_dsp_is_stereo_domain(self):
        """轨链 DSP 域=立体声；5.1/7.1/高度不支持且有原因。"""
        assert layout_supported("track_dsp", ChannelLayout.STEREO)
        assert not layout_supported("track_dsp", ChannelLayout.SURROUND_5_1)
        reason = unsupported_reason("track_dsp", ChannelLayout.SURROUND_5_1)
        assert reason and "立体声" in reason

    def test_export_supports_surround_delivery(self):
        for layout in (ChannelLayout.STEREO, ChannelLayout.SURROUND_5_1,
                       ChannelLayout.SURROUND_7_1):
            assert layout_supported("export", layout)

    def test_unknown_stage_diagnosed(self):
        assert stage_capability("atmos_render") is None
        reason = unsupported_reason("atmos_render", ChannelLayout.STEREO)
        assert reason and "未知能力段" in reason

    def test_capability_object_shape(self):
        cap = stage_capability("clip_stage")
        assert isinstance(cap, StageCapability)
        assert cap.adaptation


class TestMx15ImpulseSweepAllPairs:
    """全部支持组合 × 每源声道脉冲：输出 == 矩阵列（无交换/无丢失）。"""

    PAIRS = supported_downmix_pairs()

    @pytest.mark.parametrize("src,dst", PAIRS)
    def test_impulse_matches_matrix_column(self, src, dst):
        m = downmix_matrix(src, dst)
        n = 32
        for src_ch in range(src.channel_count):
            x = np.zeros((src.channel_count, n), dtype=np.float32)
            x[src_ch, :] = 1.0
            out = downmix(x, src, dst)
            assert out.shape == (dst.channel_count, n)
            # 每个目标声道首样本 = 矩阵对应系数（脉冲响应）
            np.testing.assert_allclose(
                out[:, 0], m[:, src_ch], atol=1e-6,
                err_msg=f"{src.av_layout_name}->{dst.av_layout_name} "
                        f"src ch {src_ch} ({src.channel_labels[src_ch]})")
            # 全列恒定（线性时不变，无瞬态泄漏；numpy 2.x assert_allclose
            # 不广播不等形状，显式平铺比较）
            col_var = float(np.max(np.abs(out - out[:, :1])))
            assert col_var < 1e-7, (
                f"{src.av_layout_name}->{dst.av_layout_name} "
                f"src ch {src_ch} 非时不变 (var={col_var})")

    def test_center_and_lfe_never_swapped(self):
        """5.1 声道顺序 [L,R,C,LFE,Ls,Rs]：C 脉冲只进 Lo/Ro（0.707），
        LFE 脉冲默认被排除——C 与 LFE 位置无交换。"""
        x = np.zeros((6, 16), dtype=np.float32)
        x[2, :] = 1.0  # C
        out = downmix(x, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert np.allclose(out[0], np.sqrt(0.5), atol=1e-6)
        assert np.allclose(out[1], np.sqrt(0.5), atol=1e-6)
        x = np.zeros((6, 16), dtype=np.float32)
        x[3, :] = 1.0  # LFE
        out = downmix(x, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert np.allclose(out, 0.0, atol=1e-6)

    def test_51_side_layout_resolves_and_downmixes(self):
        """5.1(side) 具体布局不退化成截前两路——解析到 5.1 并走标准矩阵。"""
        layout = ChannelLayout.from_av_layout_name("5.1(side)")
        assert layout == ChannelLayout.SURROUND_5_1
        x = np.zeros((6, 16), dtype=np.float32)
        x[2, :] = 0.5  # C
        out = downmix(x, layout, ChannelLayout.STEREO)
        assert np.allclose(out[0], 0.5 * np.sqrt(0.5), atol=1e-6)

    def test_71_side_surround_folds_into_lr(self):
        """7.1 [.., Lss, Rss] → 5.1：Lss 并入 L（0.707），不进 R。"""
        x = np.zeros((8, 16), dtype=np.float32)
        x[6, :] = 1.0  # Lss
        out = downmix(x, ChannelLayout.SURROUND_7_1, ChannelLayout.SURROUND_5_1)
        np.testing.assert_allclose(out[0], np.sqrt(0.5), atol=1e-6)
        np.testing.assert_allclose(out[1], 0.0, atol=1e-6)


class TestMx15ExportTruth:
    def test_resolve_export_channels_single_truth(self):
        """导出声道数：显式布局优先，未知布局显式报错（不静默猜测）。"""
        assert resolve_export_channels(2, "5.1") == 6
        assert resolve_export_channels(2, "7.1") == 8
        assert resolve_export_channels(6, "") == 6
        with pytest.raises(ValueError):
            resolve_export_channels(2, "7.1.4")

    def test_preview_export_same_matrix(self):
        """预览/导出同一矩阵：同一输入两次 downmix 结果一致且可复用。"""
        rng = np.random.default_rng(11)
        x = rng.standard_normal((6, 512)).astype(np.float32)
        a = downmix(x, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        b = downmix(x, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        np.testing.assert_array_equal(a, b)
        m = downmix_matrix(ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        np.testing.assert_allclose(a, m @ x.astype(np.float64), atol=1e-5)

    def test_upmix_not_silently_supported(self):
        """stereo→5.1 upmix 无标准矩阵（显式 ValueError，不静默假环绕）。"""
        assert not downmix_supported(ChannelLayout.STEREO,
                                     ChannelLayout.SURROUND_5_1)
        with pytest.raises(ValueError):
            downmix_matrix(ChannelLayout.STEREO, ChannelLayout.SURROUND_5_1)
