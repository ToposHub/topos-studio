"""M13.7 · cue 动画渲染消费接线（title_animation 模型层 → 字幕渲染）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.subtitle_renderer import apply_cue_animation
from src.features.edit.core.timeline_v2 import SubtitleClip


def _clip(anim=None, start=0, duration=96):
    return SubtitleClip(
        clip_id="s1", track_id="s1", timeline_start=start,
        timeline_duration=duration, text="标题", animation=anim)


def _rgba(h=100, w=40, rows=(20, 60)):
    """rows 行区间不透明的画布（模拟字幕带）。"""
    buf = np.zeros((h, w, 4), np.float32)
    buf[..., :3] = 1.0
    buf[rows[0]:rows[1], :, 3] = 1.0
    return buf


class TestApplyCueAnimation:
    def test_no_declaration_unchanged(self):
        rgba = _rgba()
        assert apply_cue_animation(rgba, _clip(), 10) is rgba

    def test_roll_static_when_content_fits_viewport(self):
        """内容（alpha 行域）不高于画布 → roll_offset 恒 0，输出不变
        （渲染画布即视口——滚动语义限定"内容裁剪前高于视口"的场景，
        帧内行域判定下静态是正确行为）。"""
        rgba = _rgba()
        out = apply_cue_animation(rgba, _clip({"roll": True}), frame=48)
        assert np.array_equal(out, rgba)

    def test_fade_in_scales_alpha(self):
        clip = _clip({"fade_in_frames": 12, "fade_out_frames": 12})
        rgba = _rgba()
        # 帧 6：淡入中点 → alpha≈0.5
        out = apply_cue_animation(rgba, clip, 6)
        assert out[30, 0, 3] == pytest.approx(0.5, abs=1e-6)
        # 中段全亮
        mid = apply_cue_animation(rgba, clip, 48)
        assert mid[30, 0, 3] == pytest.approx(1.0)
        # 帧上界外（含出点淡出到 0）
        end = apply_cue_animation(rgba, clip, 96)
        assert end[30, 0, 3] == pytest.approx(0.0, abs=1e-6)
        # 原数组不被修改
        assert rgba[30, 0, 3] == 1.0

    def test_fade_domain_outside_zero(self):
        clip = _clip({"fade_in_frames": 6}, start=10, duration=24)
        rgba = _rgba()
        out = apply_cue_animation(rgba, clip, 5)  # 入点前
        assert out[30, 0, 3] == 0.0
        out2 = apply_cue_animation(rgba, clip, 40)  # 出点后
        assert out2[30, 0, 3] == 0.0


class TestAnimationFieldRoundtrip:
    def test_sparse_roundtrip(self):
        clip = SubtitleClip(
            clip_id="s", track_id="s", timeline_start=0,
            timeline_duration=24, text="t",
            animation={"fade_in_frames": 6})
        back = SubtitleClip.from_dict(clip.to_dict())
        assert back.animation == {"fade_in_frames": 6}
        # 无动画：键不落盘（旧工程形状不变）
        plain = SubtitleClip(clip_id="p", track_id="s",
                             timeline_start=0, timeline_duration=24,
                             text="t")
        assert "animation" not in plain.to_dict()
        assert SubtitleClip.from_dict(plain.to_dict()).animation is None
