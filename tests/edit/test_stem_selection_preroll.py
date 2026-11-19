"""H02（选区预卷）× Stem——选区导出复用成片预卷预算语义（边界 2 收口）。

修前复现：Stem 的 frame_range 起点冷启动——区间前 Delay/Reverb/包络
状态全零（成片复审：从头渲染再裁 f4..10 峰 0.344，直接导出 f4..10 全零）。

修后契约：
- ``render_stem_pcm`` / ``export_all_stems`` 支持 ``frame_range``；
- 选区导出按全路径尾音预算确定性预卷（feed 坐标，与成片
  _render_audio_master 同一公式，预算不含 PDC），预卷段裁掉不交付；
- 选区输出与"整片渲染再裁剪"一致；
- frame_range=None 与 (0, n) 行为与旧实现逐样本一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.stem_export import (
    StemSpec,
    StemType,
    render_stem_pcm,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    EffectChainParams,
    TimelineSequence,
    TrackEffectInstance,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core
FPS = 25
SR = 48000
SPF = SR // FPS


def _feedback_delay_chain() -> EffectChainParams:
    """带反馈的立体声 Delay（200ms 回声，反馈 0.35——冷启动状态敏感）。"""
    return EffectChainParams(effects=(
        TrackEffectInstance("delay", {
            "enabled": True, "left_delay": 200.0, "right_delay": 200.0,
            "left_feedback": 0.35, "right_feedback": 0.35,
            "dry_wet": 100.0, "level_db": 0.0, "low_cut": 20.0,
            "high_cut": 20000.0, "high_ratio": 0.0, "stereo_blend": 0.0,
            "input_format": "Stereo",
        }, instance_id="D"),
    ))


def _sequence() -> TimelineSequence:
    t = EditTrack("T", "T", "audio", 0,
                  clips=(ClipInstance("c", "a", "T", 0, 25, 0, 25),),
                  effect_chain=_feedback_delay_chain())
    return TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(t,))


def _decode_pulse():
    """frame 0 注入 8 样本 hann 脉冲（其余静音）。"""
    def decode(track, clip, frame_idx, fps, num_samples):
        out = np.zeros((2, num_samples), dtype=np.float32)
        if frame_idx == 0:
            out[:, :8] = np.hanning(8).astype(np.float32)
        return out
    return decode


MASTER_SPEC = StemSpec(StemType.MASTER, "Master", (), include_master=True)


class TestStemSelectionPreRoll:
    def test_selection_equals_earlier_start_crop(self):
        """选区 stem == 更早起点的选区导出裁剪（预卷重建选区前 Delay 状态）。

        与成片 H02 同一契约：render(4,10) == render(0,10)[:, 4*SPF:]——
        修前 (4,10) 冷启动无预卷，选区内回声全零。
        """
        seq = _sequence()
        base = render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse(),
                               frame_range=(0, 10))
        sel = render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse(),
                              frame_range=(4, 10))
        assert base is not None and sel is not None
        crop = base[:, 4 * SPF:]
        assert sel.shape == crop.shape
        np.testing.assert_allclose(sel, crop, atol=1e-6)
        # 选区含回声能量（修前冷启动直接导出 f4..10 全零）
        assert float(np.abs(sel).max()) > 0.2

    def test_zero_start_matches_none(self):
        """frame_range=(0, 全片) 与 None 逐样本一致（无预卷路径不变）。"""
        seq = _sequence()
        ref = render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse())
        sel = render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse(),
                              frame_range=(0, seq.duration_frames))
        assert ref is not None and sel is not None
        assert sel.shape == ref.shape
        np.testing.assert_array_equal(sel, ref)

    def test_selection_length_is_selection_plus_tail(self):
        """长度契约 = 选区样本 + 尾音延长（与整片导出同一尾部预算）。"""
        seq = _sequence()
        full = render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse())
        sel = render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse(),
                              frame_range=(4, 10))
        assert full is not None and sel is not None
        tail = full.shape[-1] - seq.duration_frames * SPF
        assert sel.shape[-1] == 6 * SPF + tail

    def test_empty_and_degenerate_ranges(self):
        """空/退化 range 不产内容；range 越界按整片钳制。"""
        seq = _sequence()
        assert render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse(),
                               frame_range=(6, 6)) is None
        assert render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse(),
                               frame_range=(8, 4)) is None
        beyond = render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse(),
                                 frame_range=(0, 10_000))
        ref = render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse())
        assert beyond is not None and ref is not None
        np.testing.assert_array_equal(beyond, ref)

    def test_selection_output_starts_with_converged_content(self):
        """选区首样本即有内容（预卷收敛，无前置静音段）。"""
        seq = _sequence()
        sel = render_stem_pcm(seq, MASTER_SPEC, decode_clip_fn=_decode_pulse(),
                              frame_range=(4, 10))
        assert sel is not None
        # 选区首段即有收敛后的回声内容（全湿链：frame5 回声起于选区内）
        assert float(np.abs(sel[:, :2 * SPF]).max()) > 0.05
