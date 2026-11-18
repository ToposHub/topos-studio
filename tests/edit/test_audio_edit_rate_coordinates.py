"""P0 · 统一 48 kHz 编辑坐标与消费采样率（2026-11-19 计划第一阶段）。

验收来自 docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md：

- ``mute[48000,96000)`` 在 48 kHz 图落于 1–2 s；修复前 96 kHz 图被当作
  96k 样本索引落到 0.5–1 s，修复后必须仍在 1–2 s；
- 用 44.1／48／96 kHz 复测脉冲、斜坡、fade、零点和忙闲块，边界偏差
  不超过一个输出样本；
- 预渲染基底（48 kHz 参考域）在非 48k 消费率下按绝对位置切块。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_edit import (
    apply_audio_edit_to_pcm,
    apply_clip_audio_edit,
    apply_clip_audio_edit_block,
    make_step,
    new_audio_edit,
)
from src.features.edit.core.sample_time import (
    AUDIO_REFERENCE_SAMPLE_RATE,
    rate_position_to_reference,
    reference_position_to_rate,
)

REF = AUDIO_REFERENCE_SAMPLE_RATE


# ----------------------------------------------------------------------
# 换算层（sample_time）：确定性 + 互逆
# ----------------------------------------------------------------------
class TestRateConversion:
    def test_reference_identity_at_48k(self) -> None:
        assert reference_position_to_rate(48000, 48000) == 48000
        assert rate_position_to_reference(96000, 48000) == 96000

    def test_exact_integer_ratios(self) -> None:
        # 48k→96k ×2；48k→44.1k = 147/160。
        assert reference_position_to_rate(48000, 96000) == 96000
        assert reference_position_to_rate(96000, 96000) == 192000
        assert reference_position_to_rate(16000, 44100) == 14700
        assert rate_position_to_reference(96000, 96000) == 48000
        assert rate_position_to_reference(14700, 44100) == 16000

    def test_rounding_deterministic_and_bounded(self) -> None:
        for rate in (44100, 96000, 88200):
            for pos in range(0, 5000, 7):
                out = reference_position_to_rate(pos, rate)
                # 误差 ≤1 输出样本（有理换算合同）。
                ideal = pos * rate / REF
                assert abs(out - ideal) <= 1.0
                assert reference_position_to_rate(pos, rate) == out  # 确定

    def test_round_trip_within_one_sample(self) -> None:
        for rate in (44100, 96000):
            for pos in range(0, 10000, 13):
                back = rate_position_to_reference(
                    reference_position_to_rate(pos, rate), rate)
                assert abs(back - pos) <= 1


# ----------------------------------------------------------------------
# 验收主用例：mute[48000,96000) 在 44.1/48/96 kHz 的落点
# ----------------------------------------------------------------------
class TestMuteRangeAcrossRates:
    @pytest.mark.parametrize("rate,expected_start,expected_end", [
        (48000, 48000, 96000),
        (96000, 96000, 192000),
        (44100, 44100, 88200),
    ])
    def test_mute_lands_on_same_wall_clock(self, rate, expected_start,
                                           expected_end) -> None:
        duration = int(3.0 * rate)
        pcm = np.full((1, duration), 0.5, dtype=np.float32)
        edit = new_audio_edit([make_step(
            "mute", start=48000, end=96000, channels=(0,))])
        out = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)[0]

        # 选区中段（避开 3 ms 边缘过渡）完全静音。
        mid_lo = expected_start + int(0.01 * rate)
        mid_hi = expected_end - int(0.01 * rate)
        assert np.all(out[mid_lo:mid_hi] == 0.0)
        # 选区外（避开边缘）不变。
        assert np.all(out[:expected_start - int(0.01 * rate)] == 0.5)
        assert np.all(out[expected_end + int(0.01 * rate):] == 0.5)
        # 边界偏差 ≤1 输出样本：首个受影响样本紧贴理想边界。
        affected = np.nonzero(np.abs(out - 0.5) > 1e-6)[0]
        assert affected.size > 0
        assert abs(int(affected[0]) - expected_start) <= 1
        assert abs(int(affected[-1]) - (expected_end - 1)) <= 1

    def test_busy_block_96k_boundaries(self) -> None:
        """忙闲块（随机内容）下 96 kHz 的零点与边界同样准确。"""
        rate = 96000
        rng = np.random.default_rng(20261119)
        pcm = (rng.standard_normal((2, int(2.5 * rate))) * 0.3).astype(np.float32)
        edit = new_audio_edit([make_step(
            "mute", start=48000, end=96000, channels=(0, 1))])
        out = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)
        zero_mask = np.all(out == 0.0, axis=0)
        assert np.all(zero_mask[96000 + 300:192000 - 300])
        assert not zero_mask[:96000 - 300].any()
        assert not zero_mask[192000 + 300:].any()

    def test_block_split_matches_whole_at_96k(self) -> None:
        rate = 96000
        rng = np.random.default_rng(20261120)
        pcm = (rng.standard_normal((2, int(1.6 * rate))) * 0.2).astype(np.float32)
        edit = new_audio_edit([
            make_step("range_gain", 24000, 72000, (0,), gain_db=-4.0),
            make_step("mute", 60000, 100000, (1,)),
        ])
        whole = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)
        chunks = []
        step = 7919  # 与块边界互质，覆盖任意分块相位
        for start in range(0, pcm.shape[1], step):
            block = pcm[:, start:start + step]
            chunks.append(apply_clip_audio_edit(
                block, start, edit, sample_rate=rate))
        np.testing.assert_allclose(
            whole, np.concatenate(chunks, axis=1)[:, : pcm.shape[1]], atol=1e-6)


# ----------------------------------------------------------------------
# 包络跨率：0→−12 dB 拐点/中点
# ----------------------------------------------------------------------
class TestEnvelopeAcrossRates:
    @pytest.mark.parametrize("rate,knee,probe", [
        (48000, 96000, 48000),    # 2 s 末节点，1 s 中点探针
        (96000, 192000, 96000),
        (44100, 88200, 44100),
    ])
    def test_knee_and_midpoint(self, rate, knee, probe) -> None:
        pcm = np.full((1, int(2.0 * rate)), 0.5, dtype=np.float32)
        edit = new_audio_edit()
        edit["envelope"] = [
            {"id": "a", "local_sample": 0, "gain_db": 0.0,
             "interpolation": "linear"},
            {"id": "b", "local_sample": 2 * REF, "gain_db": -12.0,
             "interpolation": "linear"},
        ]
        out = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)[0]
        # 拐点前不变；1 s 中点 −6 dB；末节点（拐点，最后一样本）−12 dB。
        assert out[0] == pytest.approx(0.5)
        assert 20 * np.log10(out[probe] / 0.5) == pytest.approx(-6.0, abs=0.05)
        assert out[knee - 1] == pytest.approx(0.5 * 10 ** (-12 / 20), abs=1e-5)

    def test_zero_crossing_node_position(self) -> None:
        """陡降包络的中点过零检查：0→−120 dB 线性，−60 dB 落在半程。"""
        rate = 96000
        pcm = np.full((1, rate), 0.5, dtype=np.float32)
        edit = new_audio_edit()
        edit["envelope"] = [
            {"id": "a", "local_sample": 0, "gain_db": 0.0,
             "interpolation": "linear"},
            {"id": "b", "local_sample": 24000, "gain_db": -120.0,
             "interpolation": "linear"},
        ]
        out = apply_audio_edit_to_pcm(pcm, edit, sample_rate=rate)[0]
        # −60 dB（增益 0.001）在 0→24000(48k)=48000(96k) 的半程 24000 处。
        below = np.nonzero(out < 0.5 * 10 ** (-60 / 20))[0]
        assert below.size > 0
        assert abs(int(below[0]) - 24000) <= 1


# ----------------------------------------------------------------------
# 预渲染基底跨率切块：绝对位置对齐
# ----------------------------------------------------------------------
def _denoise_only_edit(total: int) -> dict:
    return new_audio_edit([make_step(
        "spectral_denoise", 0, total, (0, 1), reduction_db=6.0)])


class TestPreRenderBaseRatedSlicing:
    def test_ramp_base_time_aligned_at_96k(self) -> None:
        """基底为 48k 线性斜坡时，96k 取块的线性插值精确——时位可验。"""
        total = 48000
        base = (np.arange(total, dtype=np.float32) / total * 0.8)[None, :]
        base = np.repeat(base, 2, axis=0).copy()
        edit = _denoise_only_edit(total)
        out_rate = 96000
        block_start, count = 19200, 9600
        block = apply_clip_audio_edit_block(
            np.zeros((2, count), dtype=np.float32), block_start, edit,
            sample_rate=out_rate, pre_render_base=base)
        assert block.shape == (2, count)
        # 输出样本 c 对应基底位置 c/2：斜坡值 = 0.8 * (c/2) / 48000。
        expected = 0.8 * (np.arange(block_start, block_start + count,
                                    dtype=np.float64) / 2.0) / total
        np.testing.assert_allclose(block[0], expected, atol=1e-6)

    def test_pulse_centroid_within_one_sample(self) -> None:
        """48k 基底脉冲在 96k 取块：能量质心落在 2p ±1 输出样本。"""
        total = 48000
        base = np.zeros((1, total), dtype=np.float32)
        pulse_ref = 12000
        base[0, pulse_ref] = 1.0
        edit = _denoise_only_edit(total)
        out_rate = 96000
        block = apply_clip_audio_edit_block(
            np.zeros((1, 4096), dtype=np.float32), 23800, edit,
            sample_rate=out_rate, pre_render_base=base)[0]
        energy = block.astype(np.float64) ** 2
        centroid = float(np.sum(np.arange(block.size) * energy)
                         / max(np.sum(energy), 1e-12))
        expected = pulse_ref * 2 - 23800
        assert abs(centroid - expected) <= 1.0

    def test_zero_pads_past_base_end_at_44k(self) -> None:
        total = 24000
        base = np.full((2, total), 0.25, dtype=np.float32)
        edit = _denoise_only_edit(total)
        out_rate = 44100
        # 基底末尾在 44.1k 域 = 24000×44100/48000 = 22050 样本。
        block = apply_clip_audio_edit_block(
            np.zeros((2, 4000), dtype=np.float32), 21000, edit,
            sample_rate=out_rate, pre_render_base=base)
        assert block.shape == (2, 4000)
        np.testing.assert_allclose(block[0, :1000], 0.25, atol=1e-6)
        np.testing.assert_array_equal(block[:, 1100:], np.zeros((2, 2900)))

    def test_same_rate_unchanged(self) -> None:
        total = 1000
        base = np.full((2, total), 0.5, dtype=np.float32)
        edit = _denoise_only_edit(total)
        block = apply_clip_audio_edit_block(
            np.zeros((2, 100), dtype=np.float32), 500, edit,
            sample_rate=REF, pre_render_base=base)
        np.testing.assert_array_equal(block, np.full((2, 100), 0.5, dtype=np.float32))
