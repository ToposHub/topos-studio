"""
声道布局真相链测试（RMA-09 阶段 C1）。

验收门槛（计划表 C1）：
- 逐声道 impulse 构造 mono/stereo/5.1(side)/5.1(back)/7.1/未知 layout；
  断言目标通道和系数。
- decoder 输出携带 layout（来自容器），adapt_channels 用真实布局而非按声道数猜测。
- 未知布局不能静默产生文件（B1 校验 + adapt_channels 拒绝）。

核心 bug（RMA-09）：channel_layout.adapt_channels 默认按声道数推断布局，
实时/导出没有把容器实际 layout 传入；5.1(side) vs 5.1(back)、multiple mono 等
会被错误解释或丢声道。异常时又回退截断/复制，与模块注释"拒绝未知 mapping"相反。

本测试覆盖 C1 的 decoder layout 捕获 + adapt_channels 显式布局路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.channel_layout import (
    ChannelLayout, adapt_channels, downmix_matrix, downmix,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 显式 layout vs 按声道数猜测（adapt_channels）
# ─────────────────────────────────────────────────────────────────────────────

class TestAdaptChannelsExplicitLayout:
    """RMA-09: adapt_channels 接受显式 src_layout，不再纯靠声道数猜测。"""

    def test_explicit_layout_5_1_to_stereo(self):
        """5.1 → stereo 用显式 layout，ITUR BS.775 系数。"""
        # 5.1: L, R, C, LFE, Ls, Rs，逐声道 impulse
        src = np.array([
            [1.0, 1.0],   # L
            [1.0, 1.0],   # R
            [1.0, 1.0],   # C
            [0.0, 0.0],   # LFE
            [1.0, 1.0],   # Ls
            [1.0, 1.0],   # Rs
        ], dtype=np.float32)
        out = adapt_channels(src, 2, src_layout=ChannelLayout.SURROUND_5_1)
        assert out.shape == (2, 2)
        # L = L + 0.707*C + 0.707*Ls（不含 LFE）
        expected_l = 1.0 + 0.7071 * 1.0 + 0.7071 * 1.0
        assert abs(out[0][0] - expected_l) < 1e-2

    def test_count_guess_vs_explicit_same_result_for_standard(self):
        """标准声道数下，显式 layout 与按数推断结果一致。"""
        src = np.full((6, 10), 0.5, dtype=np.float32)
        out_guess = adapt_channels(src, 2)  # 按数推断 5.1
        out_explicit = adapt_channels(src, 2, src_layout=ChannelLayout.SURROUND_5_1)
        np.testing.assert_allclose(out_guess, out_explicit, atol=1e-6)

    def test_unknown_layout_rejected_not_truncated(self):
        """未知布局（4 声道非标准）→ adapt_channels 抛 ValueError（拒绝，不静默截断）。

        RMA-09: 与模块注释"拒绝未知 mapping"一致，不再回退截断。
        """
        src = np.full((4, 10), 0.5, dtype=np.float32)  # 4 声道非标准
        with pytest.raises(ValueError):
            adapt_channels(src, 2)  # 按数推断 4 → ValueError


# ─────────────────────────────────────────────────────────────────────────────
# 2. 逐声道 impulse downmix 系数验证
# ─────────────────────────────────────────────────────────────────────────────

class TestPerChannelImpulseDownmix:
    """RMA-09: 逐声道 impulse 验证 downmix 系数（ITUR BS.775）。"""

    def test_5_1_center_channel_coefficient(self):
        """5.1 Center → stereo: 每侧 0.7071（-3dB）。"""
        # 只有 Center 有信号
        src = np.zeros((6, 4), dtype=np.float32)
        src[2] = 1.0  # C
        out = downmix(src, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        # C → L/R 各 0.7071
        assert abs(out[0][0] - 0.7071) < 1e-3
        assert abs(out[1][0] - 0.7071) < 1e-3

    def test_5_1_lfe_excluded_by_default(self):
        """5.1 LFE → stereo: 默认不包含（include_lfe=False）。"""
        src = np.zeros((6, 4), dtype=np.float32)
        src[3] = 1.0  # LFE
        out = downmix(src, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO,
                      include_lfe=False)
        assert np.max(np.abs(out)) < 1e-6  # LFE 不贡献

    def test_5_1_surround_coefficients(self):
        """5.1 Ls/Rs → stereo: 每侧 0.7071。"""
        src = np.zeros((6, 4), dtype=np.float32)
        src[4] = 1.0  # Ls
        out = downmix(src, ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert abs(out[0][0] - 0.7071) < 1e-3  # Ls → L
        assert abs(out[1][0]) < 1e-6  # Ls 不贡献到 R

    def test_mono_to_stereo_replicate(self):
        """mono → stereo: 两声道复制。"""
        src = np.full((1, 4), 0.5, dtype=np.float32)
        out = adapt_channels(src, 2, src_layout=ChannelLayout.MONO)
        np.testing.assert_allclose(out[0], 0.5)
        np.testing.assert_allclose(out[1], 0.5)

    def test_stereo_to_mono_sum(self):
        """stereo → mono: (L+R)/2 或 L+R（看系数）。"""
        src = np.array([[0.6], [0.4]], dtype=np.float32)  # L=0.6, R=0.4
        out = adapt_channels(src, 1, src_layout=ChannelLayout.STEREO)
        # downmix_matrix stereo→mono: [[0.7071, 0.7071]]（等功率）或 [[1,1]]？
        # 验证与矩阵一致即可
        m = downmix_matrix(ChannelLayout.STEREO, ChannelLayout.MONO)
        expected = float((m @ src)[0][0])
        assert abs(out[0][0] - expected) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# 3. AudioDecoder 容器 layout 捕获
# ─────────────────────────────────────────────────────────────────────────────

class TestDecoderLayoutCapture:
    """RMA-09: AudioDecoder 从容器捕获实际 layout。"""

    def test_decoder_has_source_layout_properties(self):
        """AudioDecoder 有 source_layout_name / source_layout 属性。"""
        from src.features.edit.core.audio_playback import AudioDecoder
        d = AudioDecoder()
        assert hasattr(d, "source_layout_name")
        assert hasattr(d, "source_layout")
        # 未 open 时 layout 名为空；source_layout 按默认 2 声道推断为 STEREO
        assert d.source_layout_name == ""
        assert d.source_layout == ChannelLayout.STEREO  # 默认 2 声道

    def test_decoder_captures_stereo_layout(self, tmp_path):
        """打开 stereo WAV → source_layout 为 STEREO。"""
        import av
        from src.features.edit.core.audio_playback import AudioDecoder

        # 写一个 stereo WAV
        wav_path = tmp_path / "stereo.wav"
        container = av.open(str(wav_path), mode="w", format="wav")
        stream = container.add_stream("pcm_s16le", rate=48000)
        stream.layout = "stereo"
        sig = (0.5 * np.sin(2 * np.pi * 440 * np.arange(4800) / 48000)).astype(np.float32)
        frame = av.AudioFrame.from_ndarray(
            np.stack([sig, sig]), format="fltp", layout="stereo",
        )
        frame.sample_rate = 48000
        for pkt in stream.encode(frame):
            container.mux(pkt)
        for pkt in stream.encode(None):
            container.mux(pkt)
        container.close()

        d = AudioDecoder()
        assert d.open(Path(wav_path), timeline_fps=24.0)
        # PyAV 的 stream.layout.name 可能是 "stereo" 或 "2 channels"（取决于版本）
        assert d.source_layout_name in ("stereo", "2 channels")
        # source_layout 应解析为 STEREO（按声道数兜底也能识别）
        assert d.source_layout == ChannelLayout.STEREO
        assert d.channels == 2
        d.close()

    def test_decoder_captures_mono_layout(self, tmp_path):
        """打开 mono WAV → source_layout 为 MONO。"""
        import av
        from src.features.edit.core.audio_playback import AudioDecoder

        wav_path = tmp_path / "mono.wav"
        container = av.open(str(wav_path), mode="w", format="wav")
        stream = container.add_stream("pcm_s16le", rate=48000)
        stream.layout = "mono"
        sig = (0.5 * np.sin(2 * np.pi * 440 * np.arange(4800) / 48000)).astype(np.float32)
        frame = av.AudioFrame.from_ndarray(
            sig.reshape(1, -1), format="fltp", layout="mono",
        )
        frame.sample_rate = 48000
        for pkt in stream.encode(frame):
            container.mux(pkt)
        for pkt in stream.encode(None):
            container.mux(pkt)
        container.close()

        d = AudioDecoder()
        assert d.open(Path(wav_path), timeline_fps=24.0)
        assert d.source_layout == ChannelLayout.MONO
        assert d.channels == 1
        d.close()


# ─────────────────────────────────────────────────────────────────────────────
# 4. ChannelLayout 解析：5.1(side) / 5.1(back) / 7.1
# ─────────────────────────────────────────────────────────────────────────────

class TestChannelLayoutParsing:
    """RMA-09: from_av_layout_name 宽松解析容器 layout 名。"""

    def test_5_1_side_parsed(self):
        """5.1(side) → SURROUND_5_1（PyAV 的 side-surround 命名）。"""
        assert ChannelLayout.from_av_layout_name("5.1(side)") == ChannelLayout.SURROUND_5_1

    def test_5_1_back_parsed(self):
        """5.1(back) → SURROUND_5_1。"""
        assert ChannelLayout.from_av_layout_name("5.1(back)") == ChannelLayout.SURROUND_5_1

    def test_7_1_parsed(self):
        """7.1 → SURROUND_7_1。"""
        assert ChannelLayout.from_av_layout_name("7.1") == ChannelLayout.SURROUND_7_1

    def test_unknown_layout_rejected(self):
        """未知 layout（如 "3.1"）→ ValueError（拒绝，不猜测）。"""
        with pytest.raises(ValueError):
            ChannelLayout.from_av_layout_name("3.1")
