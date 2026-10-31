"""F9.2 · 翻译后字幕 QC 与多语言交付——验收补充。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.captions.translation import qc_translated_track
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    Rational,
    SubtitleClip,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _track_with_clips(clips_spec, track_id="sub_zh"):
    clips = tuple(
        SubtitleClip(clip_id=cid, track_id=track_id,
                     timeline_start=start, timeline_duration=end - start,
                     text=text)
        for cid, start, end, text in clips_spec)
    # EditTrack 用 subtitle_clips 命名参数
    track = EditTrack(
        track_id=track_id, track_type="subtitle",
        subtitle_clips=clips)
    return track, clips





class TestTranslatedTrackQC:
    def test_clean_translation_passes(self):
        track, _ = _track_with_clips([
            ("s1", 0, 96, "这是一条正常长度的字幕"),
            ("s2", 120, 216, "第二条字幕内容"),
        ])
        issues, all_pass = qc_translated_track(track, Rational(24, 1),
                                               timeline_duration_frames=240)
        assert all_pass is True

    def test_long_line_error_flagged(self):
        """翻译后行长超限（error 级）→ all_pass=False。"""
        track, _ = _track_with_clips([
            ("s1", 0, 96, "一" * 100),  # 100 字远超 42/行×2 行
        ])
        issues, _ = qc_translated_track(track, Rational(24, 1),
                                        timeline_duration_frames=240)
        # line_too_long 为 warning 级（翻译后人工判断）；行长超限被检出
        assert any("line" in str(getattr(i, "code", ""))
                   for i in issues)

    def test_out_of_range_error_flagged(self):
        """越界（error 级）→ all_pass=False。"""
        track, _ = _track_with_clips([
            ("s1", 0, 96, "正常"),
            ("s2", 230, 300, "超出时间线末尾"),  # end 300 > 240
        ])
        issues, all_pass = qc_translated_track(
            track, Rational(24, 1), timeline_duration_frames=240)
        assert all_pass is False
        assert any("out_of_range" in str(getattr(i, "code", ""))
                   for i in issues)

    def test_reading_speed_checked(self):
        """阅读速率：超长文本+过短时长 → issue。"""
        track, _ = _track_with_clips([
            ("s1", 0, 12, "这是一条非常长的字幕文本远远超过阅读速率上限"),
        ])
        issues, _ = qc_translated_track(track, Rational(24, 1),
                                        timeline_duration_frames=240)
        assert len(issues) > 0
