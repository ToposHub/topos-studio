"""第七轮审计 0B 回归：TTML 时基正确性（R12/R13）。

R12：缺省 tickRate 按 W3C TTML1 推导——声明 frameRate 时
     tickRate = effectiveFrameRate × subFrameRate（兼顾 multiplier）。
     frameRate=30 无 tickRate 时 30t 应为 1 秒（24fps 目标 = 24 帧），
     旧实现按 1 tick/秒算成 30 秒（720 帧）。
R13：tickRate=0/负/非数明确拒绝（旧实现夹 1e-9 产出天文数字起点）；
     帧式时钟 HH:MM:SS:FF 按声明帧率换算（旧实现解析失败静默当 0）；
     子元素活动区间被父 div 的 [begin, end) 裁切。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.captions.importer import parse_subtitle_text

pytestmark = pytest.mark.edit_core

FPS_24 = 24


def _ttml(body_attrs: str = "", p_attrs: str = "", body: str = "",
          root_extra: str = "") -> str:
    return (
        f'<tt xmlns="http://www.w3.org/ns/ttml" '
        f'xmlns:ttp="http://www.w3.org/ns/ttml#parameter" {root_extra}>'
        f'<body {body_attrs}><div>{body or f"<p {p_attrs}>x</p>"}</div></body></tt>'
    )


def _parse(content: str, fps: int = FPS_24):
    return parse_subtitle_text(content, ".ttml", fps)


class TestR12DefaultTickRate:
    def test_framerate_30_default_tickrate(self):
        """frameRate=30 无 tickRate：30t = 1s（24 帧），60t = 2s（48 帧）。
        旧实现按 1 tick/秒 → 720/1440 帧。"""
        cues = _parse(_ttml(
            root_extra='ttp:frameRate="30" ttp:timeBase="media"',
            p_attrs='begin="30t" end="60t"',
        ))
        assert len(cues) == 1
        assert cues[0].start_frame == 24
        assert (cues[0].start_frame + cues[0].duration_frames) == 48

    def test_framerate_with_multiplier_default_tickrate(self):
        """multiplier 参与缺省推导：frameRate 30 × 1000/1001 →
        30t ≈ 1.001s → 24fps 下 24 帧。"""
        cues = _parse(_ttml(
            root_extra=('ttp:frameRate="30" '
                        'ttp:frameRateMultiplier="1000 1001"'),
            p_attrs='begin="30t" end="60t"',
        ))
        assert len(cues) == 1
        assert cues[0].start_frame == 24
        assert (cues[0].start_frame + cues[0].duration_frames) == 48

    def test_subframerate_affects_default_tickrate(self):
        """subFrameRate 参与缺省 tickRate：frameRate 25 × subFrame 10 →
        tickRate 250，10t = 40ms（1 秒的 1/25）。"""
        cues = _parse(_ttml(
            root_extra='ttp:frameRate="25" ttp:subFrameRate="10"',
            p_attrs='begin="10t" end="260t"',
        ))
        assert len(cues) == 1
        assert cues[0].start_frame == 1, "40ms@24fps ≈ 1 帧"

    def test_explicit_tickrate_still_wins(self):
        """显式 tickRate 不受 frameRate 缺省推导影响（D09 行为保持）。"""
        cues = _parse(_ttml(
            root_extra='ttp:frameRate="30" ttp:tickRate="15"',
            p_attrs='begin="15t" end="30t"',
        ))
        assert len(cues) == 1
        assert cues[0].start_frame == 24

    def test_no_framerate_default_tick_1s(self):
        """无 frameRate：缺省 1 tick = 1 秒（规范缺省，保持既有行为）。"""
        cues = _parse(_ttml(p_attrs='begin="1t" end="2t"'))
        assert cues[0].start_frame == 24


class TestR13InvalidTimebaseRejected:
    @pytest.mark.parametrize("attrs", [
        'ttp:tickRate="0"',
        'ttp:tickRate="-30"',
        'ttp:tickRate="abc"',
        'ttp:frameRate="0"',
        'ttp:frameRate="-25"',
    ])
    def test_nonpositive_timebase_raises(self, attrs):
        """tickRate/frameRate 为 0/负/非数 → 明确 ValueError（旧实现
        夹 1e-9 或忽略——tickRate=0 让 1t 变 10 亿秒）。"""
        with pytest.raises(ValueError, match="Invalid TTML"):
            _parse(_ttml(root_extra=attrs, p_attrs='begin="1t" end="2t"'))

    def test_smptimebase_still_rejected(self):
        with pytest.raises(ValueError, match="timeBase"):
            _parse(_ttml(root_extra='ttp:timeBase="smpte"',
                         p_attrs='begin="1s" end="2s"'))


class TestR13FrameClock:
    def test_frame_clock_uses_declared_framerate(self):
        """源 30fps 的 00:00:01:00 → 00:00:02:00 映射目标 24fps：
        帧 24 → 48（旧实现第 4 段解析失败当 0 → 帧 0→0）。"""
        cues = _parse(_ttml(
            root_extra='ttp:frameRate="30"',
            p_attrs='begin="00:00:01:00" end="00:00:02:00"',
        ))
        assert len(cues) == 1
        assert cues[0].start_frame == 24
        assert (cues[0].start_frame + cues[0].duration_frames) == 48

    def test_plain_fraction_clock_unchanged(self):
        """HH:MM:SS.ms 分秒时钟保持既有换算。"""
        cues = _parse(_ttml(
            p_attrs='begin="00:00:01.500" end="00:00:02.500"'))
        assert cues[0].start_frame == 36
        assert (cues[0].start_frame + cues[0].duration_frames) == 60


class TestR13ParentIntervalClipping:
    def test_child_clipped_to_parent_div(self):
        """父 div begin=1s end=3s；子 p 相对 1s–9s → 绝对 [2,10) 被裁为
        [2,3)（24fps：帧 48 → 72）。"""
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml">'
            '<body><div begin="1s" end="3s">'
            '<p begin="1s" end="9s">x</p>'
            '</div></body></tt>'
        )
        cues = _parse(content)
        assert len(cues) == 1
        assert cues[0].start_frame == 48
        assert (cues[0].start_frame + cues[0].duration_frames) == 72

    def test_child_inside_parent_unaffected(self):
        """父区间完全覆盖子区间时不裁切。"""
        content = (
            '<tt xmlns="http://www.w3.org/ns/ttml">'
            '<body><div begin="0s" end="100s">'
            '<p begin="1s" end="2s">x</p>'
            '</div></body></tt>'
        )
        cues = _parse(content)
        assert cues[0].start_frame == 24
        assert (cues[0].start_frame + cues[0].duration_frames) == 48
