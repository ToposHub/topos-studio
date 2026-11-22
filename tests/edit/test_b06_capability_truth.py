"""Native stage capability and explicit speaker matrix oracles."""
from __future__ import annotations

import math

import numpy as np
import pytest

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.channel_layout import ChannelLayout, downmix_matrix
from src.features.edit.core.layout_capability import (
    layout_supported,
    stage_capability,
    unsupported_reason,
)
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.vbap import SPEAKER_LAYOUTS, vbap_gains


class TestCapabilityTruth:
    def test_stages_declare_native_bed_layouts(self) -> None:
        for stage in ("track_dsp", "bus_master"):
            cap = stage_capability(stage)
            assert cap is not None
            for layout in (ChannelLayout.MONO, ChannelLayout.STEREO,
                           ChannelLayout.SURROUND_5_1, ChannelLayout.SURROUND_7_1):
                assert layout_supported(stage, layout)
            assert unsupported_reason(stage, ChannelLayout.QUAD)

    def test_capability_matches_renderer_reality(self) -> None:
        track = EditTrack("t", "T", "audio", 0, audio_layout="5.1")
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(TimelineSequence(audio_tracks=(track,)))
        pcm_51 = np.zeros((6, 8), dtype=np.float32)
        pcm_51[2, :] = 1.0  # C 通道 one-hot
        out = renderer.render_track_block("t", pcm_51, 0, source_channels=6)
        assert out is not None
        # 轨链不吞声道：6ch 直通（降混在 clip/decode 阶段的合同），
        # 布局裁切/复制只允许发生在显式声明的适配点。
        assert out.shape[0] == 6
        cap = stage_capability("track_dsp")
        assert cap.adaptation  # 降混职责在别处（文档化）

    def test_bs775_matrix_contract(self) -> None:
        """BS.775-1 Lo/Ro 系数逐项锁（含 LFE 默认排除/显式包含）。"""
        m = downmix_matrix(ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO)
        assert m.shape == (2, 6)
        sqrt_half = math.sqrt(0.5)
        # Lo = L + 0.707C + 0.707Ls；Ro = R + 0.707C + 0.707Rs
        np.testing.assert_allclose(
            m, [[1.0, 0.0, sqrt_half, 0.0, sqrt_half, 0.0],
                [0.0, 1.0, sqrt_half, 0.0, 0.0, sqrt_half]], atol=1e-9)
        m_lfe = downmix_matrix(ChannelLayout.SURROUND_5_1, ChannelLayout.STEREO,
                               include_lfe=True)
        assert m_lfe[0, 3] == pytest.approx(sqrt_half)
        assert m_lfe[1, 3] == pytest.approx(sqrt_half)


class TestVbapOracle:
    def test_manual_two_speaker_oracle(self) -> None:
        """§12 手册 oracle：±30° 扬声器、目标 0° → 非负、能量归一。

        2.0 布局即手册的双扬声器对（L=−30°, R=+30°）。
        """
        gains, exact = vbap_gains(0.0, 0.0, layout="2.0")
        assert exact
        values = np.array([gains["L"], gains["R"]], dtype=np.float64)
        assert np.all(values >= 0)
        norm = float(np.linalg.norm(values))
        assert norm == pytest.approx(1.0, abs=1e-9)
        # 能量归一解 = [√.5, √.5]（手册：不能直接当含 LFE 全布局算法）
        assert values[0] == pytest.approx(math.sqrt(0.5), abs=1e-9)
        assert values[1] == pytest.approx(math.sqrt(0.5), abs=1e-9)

    def test_center_direction_resolution(self) -> None:
        """0° 目标：2.0 落 L/R 对（对称等功率），5.1 精确命中 C。"""
        gains, exact = vbap_gains(0.0, 0.0, layout="2.0")
        assert exact
        assert gains["L"] == pytest.approx(gains["R"], abs=1e-9)
        gains, exact = vbap_gains(0.0, 0.0, layout="5.1")
        assert exact
        assert gains["C"] == pytest.approx(1.0, abs=1e-9)
        assert gains["L"] == pytest.approx(0.0, abs=1e-9)

    def test_height_request_flagged_not_silent(self) -> None:
        """仰角>0 请求在无高度布局 → exact=False（显式限制标记）。"""
        gains, exact = vbap_gains(0.0, 30.0, layout="5.1")
        assert not exact
        # 7.1.4 布局有高度声道 → exact
        gains, exact = vbap_gains(0.0, 45.0, layout="7.1.4")
        assert exact
