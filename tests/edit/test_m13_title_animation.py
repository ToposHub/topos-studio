"""M13.4 · 标题 roll/cue 动画/批量样式回归。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.title_animation import (
    batch_style_override_diff,
    cue_fade_alpha,
    roll_offset,
)


class TestRollOffset:
    def test_holds_and_linear_scroll(self):
        # 内容 1000px 视口 400px → 滚动量 600；hold 10/10，滚动区间 80 帧
        kw = dict(clip_start=0, clip_duration=100,
                  content_height=1000.0, viewport_height=400.0,
                  hold_start_frames=10, hold_end_frames=10)
        assert roll_offset(0, **kw) == 0.0
        assert roll_offset(9, **kw) == 0.0          # 开头 hold
        assert roll_offset(50, **kw) == pytest.approx(300.0)  # 中点
        assert roll_offset(89, **kw) == pytest.approx(592.5)  # (89-10)/80*600
        assert roll_offset(90, **kw) == 600.0       # 滚动完成进入结尾 hold
        assert roll_offset(100, **kw) == 600.0      # 结尾 hold
        assert roll_offset(200, **kw) == 600.0      # 出界钳制

    def test_short_content_static(self):
        kw = dict(clip_start=0, clip_duration=50,
                  content_height=300.0, viewport_height=400.0)
        assert roll_offset(25, **kw) == 0.0

    def test_zero_scroll_frames_no_divzero(self):
        kw = dict(clip_start=0, clip_duration=10,
                  content_height=800.0, viewport_height=200.0,
                  hold_start_frames=10, hold_end_frames=10)
        # hold 吃满时长：不除零，比例展开
        mid = roll_offset(5, **kw)
        assert 0.0 <= mid <= 600.0


class TestCueFadeAlpha:
    def test_fade_in_out(self):
        kw = dict(clip_start=0, clip_duration=100,
                  fade_in_frames=10, fade_out_frames=20)
        assert cue_fade_alpha(0, **kw) == 0.0
        assert cue_fade_alpha(5, **kw) == pytest.approx(0.5)
        assert cue_fade_alpha(50, **kw) == 1.0
        assert cue_fade_alpha(90, **kw) == pytest.approx(0.5)
        assert cue_fade_alpha(100, **kw) == 0.0

    def test_out_of_clip_zero(self):
        kw = dict(clip_start=10, clip_duration=50)
        assert cue_fade_alpha(5, **kw) == 0.0
        assert cue_fade_alpha(100, **kw) == 0.0


class TestBatchStyleDiff:
    def test_changed_vs_same(self):
        style_a, style_b = NS(name="A"), NS(name="B")
        clips = [
            NS(style_override=None),      # → A：变
            NS(style_override=style_a),   # 已 A：同
            NS(style_override=style_b),   # → A：变
        ]
        changed, same = batch_style_override_diff(clips, style_a)
        assert changed == (0, 2)
        assert same == (1,)

    def test_clear_to_none(self):
        clips = [NS(style_override=NS(x=1)), NS(style_override=None)]
        changed, same = batch_style_override_diff(clips, None)
        assert changed == (0,)
        assert same == (1,)
