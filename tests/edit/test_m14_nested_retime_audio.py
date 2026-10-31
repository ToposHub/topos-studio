"""M14.2 · 嵌套 retime 子片段音频窗映射回归。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.nested_retime_audio import (
    nested_child_audio_window,
    nested_retime_diagnostics,
)


def _parent(src_start=10, src_dur=40):
    return NS(clip_id="nest", source_start=src_start,
              source_duration=src_dur, timeline_start=0,
              timeline_duration=40, speed=NS(speed=2.0,
              preserve_pitch=False, reverse=False, speed_points=()))


def _child(start, dur, speed_obj=None, freeze=None):
    return NS(clip_id=f"c{start}", timeline_start=start,
              timeline_duration=dur, speed=speed_obj,
              freeze_source_frame=freeze, source_start=0,
              source_duration=dur)


class TestNestedChildAudioWindow:
    def test_overlap_clipped_to_parent_window(self):
        # 父源窗 [10,50)；子片段时间线 [0,60) → 交叠 [10,50)
        win, reason = nested_child_audio_window(
            _parent(), _child(0, 60))
        assert win == (10, 50) and reason == ""

    def test_partial_overlap(self):
        # 子 [40,80) → 交叠 [40,50)
        win, _ = nested_child_audio_window(_parent(), _child(40, 40))
        assert win == (40, 50)

    def test_no_overlap(self):
        win, reason = nested_child_audio_window(
            _parent(), _child(60, 20))
        assert win is None and reason.startswith("no_overlap")

    def test_child_retime_rejected(self):
        win, reason = nested_child_audio_window(
            _parent(), _child(10, 20,
                              speed_obj=NS(speed=1.5, reverse=False,
                                           speed_points=())))
        assert win is None
        assert reason.startswith("child_retime_unsupported")

    def test_child_frozen(self):
        win, reason = nested_child_audio_window(
            _parent(), _child(10, 20, freeze=5))
        assert win is None and reason.startswith("child_frozen")


class TestNestedRetimEDiagnostics:
    def test_batch_statuses(self):
        diags = nested_retime_diagnostics(_parent(), (
            _child(0, 60),        # ok
            _child(60, 20),       # no_overlap
            _child(10, 20, speed_obj=NS(speed=2.0, reverse=False,
                                        speed_points=())),  # child_retime
        ))
        statuses = [d["status"] for d in diags]
        assert statuses == ["ok", "no_overlap", "child_retime_unsupported"]
        assert diags[0]["window"] == [10, 50]
