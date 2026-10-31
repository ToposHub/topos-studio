"""P1-09 多通道音频布局交付测试：素材布局 → 导出/stem 的真相贯通。

核心验收：
- 导出声道数以显式布局为单一真相（resolve_export_channels）；
- 5.1 素材下混立体声走 ITU-R BS.775 矩阵（Center/Ls/Rs 贡献保留），
  不再按"前两个声道"截断；
- 未知布局（4ch）回退截断必须留警告日志（禁止静默猜测）；
- stem 导出与时间线导出共用同一适配入口。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.channel_layout import (
    ChannelLayout,
    adapt_samples_for_delivery,
    downmix_matrix,
    resolve_export_channels,
)
from src.features.edit.core.timeline_export import TimelineExportConfig

pytestmark = pytest.mark.edit_core

SR = 48000
FPS = 24


def _impulse_51() -> np.ndarray:
    """6 声道（L R C LFE Ls Rs），每声道独立幅度 DC，便于系数核对。"""
    amps = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6], dtype=np.float32)
    return np.stack([np.full(4, a, dtype=np.float32) for a in amps])


class TestResolveExportChannels:
    def test_empty_layout_falls_back_to_channels(self):
        assert resolve_export_channels(2, "") == 2
        assert resolve_export_channels(6, "") == 6

    def test_layout_overrides_channel_count(self):
        assert resolve_export_channels(2, "5.1(side)") == 6
        assert resolve_export_channels(6, "stereo") == 2
        assert resolve_export_channels(2, "7.1") == 8
        assert resolve_export_channels(2, "mono") == 1

    def test_unknown_layout_raises(self):
        with pytest.raises(ValueError):
            resolve_export_channels(2, "quad")
        with pytest.raises(ValueError):
            resolve_export_channels(2, "nonsense")

    def test_export_config_has_layout_field_default_empty(self):
        config = TimelineExportConfig()
        assert config.audio_channel_layout == ""
        assert resolve_export_channels(
            config.audio_channels, config.audio_channel_layout
        ) == 2


class TestAdaptSamplesForDelivery:
    def test_51_to_stereo_matches_bs775_matrix(self):
        """5.1 → stereo 的 golden 系数：L' = L + 0.707C + 0.707Ls（默认不含 LFE）。"""
        samples = _impulse_51()
        out = adapt_samples_for_delivery(
            samples, 2, src_layout=ChannelLayout.SURROUND_5_1
        )
        matrix = downmix_matrix(
            ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO
        )
        expected = matrix @ samples.astype(np.float64)
        np.testing.assert_allclose(out, expected, atol=1e-6)
        # 抽查关键系数（BS.775：C=1/√2≈0.7071，LFE 默认排除）
        l, r, c, _lfe, ls, rs = (a for a in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        k = 0.70710678
        assert out[0, 0] == pytest.approx(l + k * c + k * ls, abs=1e-4)
        assert out[1, 0] == pytest.approx(r + k * c + k * rs, abs=1e-4)

    def test_layout_none_count_inference_same_result(self):
        """无显式布局时按标准声道数推断：6ch → 5.1，与显式布局结果一致。"""
        samples = _impulse_51()
        explicit = adapt_samples_for_delivery(
            samples, 2, src_layout=ChannelLayout.SURROUND_5_1
        )
        inferred = adapt_samples_for_delivery(samples, 2, src_layout=None)
        np.testing.assert_allclose(explicit, inferred, atol=1e-6)

    def test_unknown_layout_truncates_with_warning(self, caplog):
        """4 声道非标准布局：回退截断 + 必须留警告日志（不静默）。"""
        samples = np.ones((4, 8), dtype=np.float32)
        with caplog.at_level("WARNING", logger="src.features.edit.core.channel_layout"):
            out = adapt_samples_for_delivery(
                samples, 2, context="clip test-clip"
            )
        assert out.shape == (2, 8)
        np.testing.assert_array_equal(out, np.ones((2, 8), dtype=np.float32))
        assert any("test-clip" in r.message for r in caplog.records)

    def test_upmix_falls_back_to_broadcast_with_warning(self, caplog):
        """stereo → 5.1 上混不支持矩阵：广播复制回退 + 警告。"""
        samples = np.full((2, 8), 0.25, dtype=np.float32)
        with caplog.at_level("WARNING", logger="src.features.edit.core.channel_layout"):
            out = adapt_samples_for_delivery(
                samples, 6, src_layout=ChannelLayout.STEREO
            )
        assert out.shape == (6, 8)
        np.testing.assert_array_equal(np.asarray(out), np.full((6, 8), 0.25, dtype=np.float32))
        assert caplog.records

    def test_mono_to_stereo_uses_copy_no_warning(self, caplog):
        samples = np.full((1, 8), 0.5, dtype=np.float32)
        with caplog.at_level("WARNING", logger="src.features.edit.core.channel_layout"):
            out = adapt_samples_for_delivery(samples, 2)
        np.testing.assert_allclose(out, 0.5, atol=1e-6)
        assert not caplog.records

    def test_same_channel_count_passthrough(self):
        samples = _impulse_51()
        out = adapt_samples_for_delivery(
            samples, 6, src_layout=ChannelLayout.SURROUND_5_1
        )
        assert out is samples


class TestStemExportUsesDownmixMatrix:
    """stem 导出通道适配与时间线导出同源（不再 naive 截断）。"""

    def test_51_source_in_stereo_stem_keeps_center_surround(self):
        from src.features.edit.core.stem_export import render_stem_pcm
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            Rational,
            TimelineSequence,
        )

        track = EditTrack(
            track_id="t1", name="Music", track_type="audio", index=0,
            stem_group="music",
            clips=(ClipInstance(
                clip_id="c1", asset_id="a1", track_id="t1",
                timeline_start=0, timeline_duration=48,
                source_start=0, source_duration=48,
            ),),
        )
        seq = TimelineSequence(
            timeline_id="s", name="S", fps=Rational(FPS, 1),
            audio_tracks=(track,),
        )

        amps = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]  # L R C LFE Ls Rs

        def _decode_51(track, clip, frame_idx, fps, n_samples):
            return np.stack(
                [np.full(n_samples, a, dtype=np.float32) for a in amps]
            )

        # 2 声道 stem：期望 BS.775 组合而非旧的"只取 L/R"
        from src.features.edit.core.stem_export import StemSpec, StemType

        spec = StemSpec(
            stem_type=StemType.GROUP, name="Music",
            track_ids=("t1",), include_master=False,
        )
        pcm = render_stem_pcm(seq, spec, SR, 2, _decode_51)
        expected_l = 0.1 + 0.707 * 0.3 + 0.707 * 0.5
        expected_r = 0.2 + 0.707 * 0.3 + 0.707 * 0.6
        # clip 时长 48 帧 × 2000 采样/帧 = 96000；后续为静音段
        active = pcm[:, :2000]
        assert active[0, 0] == pytest.approx(expected_l, abs=1e-4)
        assert active[1, 0] == pytest.approx(expected_r, abs=1e-4)
