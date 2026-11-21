"""B06 核心锁：capability 矩阵与渲染实际一致 + VBAP 手册 oracle。

主计划 §12 完成条件之一是"capability矩阵与实际一致"。本文件把
``layout_capability`` 声明的立体声域现实与离线渲染路径逐项对锁（5.1
素材进立体声轨按 clip 阶段降混的声明语义），并跑 §12 的 VBAP 双扬声器
手册 oracle（±30° 扬声器、0° 目标 → [√.5, √.5]，非负、能量归一）。

注：B06 全链（原生 track_dsp/bus_master 多声道）保持待实施——capability
如实声明 stereo，测试锁死"声明==实际"，防止声像错误被包装成原生环绕。
"""
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
    def test_stages_declared_stereo(self) -> None:
        """track_dsp/bus_master 如实声明 stereo（不虚报原生环绕）。"""
        for stage in ("track_dsp", "bus_master"):
            cap = stage_capability(stage)
            assert cap is not None
            assert cap.layouts == (ChannelLayout.STEREO,)
            assert not cap.supports(ChannelLayout.SURROUND_5_1)
            assert not layout_supported(stage, ChannelLayout.SURROUND_5_1)
            reason = unsupported_reason(stage, ChannelLayout.SURROUND_5_1)
            assert reason  # 明确拒绝原因，不是静默

    def test_capability_matches_renderer_reality(self) -> None:
        """渲染实际与声明一致：轨域不吞多声道（clip 阶段负责降混）。

        track_dsp 声明立体声域——轨链对 >2ch 输入按文档化的 trim 语义
        直通（pan_gains_for_channels >2ch = balance），降混责任在
        clip/decode 阶段（capability 的 adaptation 字段）。本测试锁死
        这一分工，防止"轨链悄悄吞声道"或"capability 虚报原生"。
        """
        track = EditTrack("t", "T", "audio", 0)
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
