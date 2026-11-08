"""VP-B06：视图接线——双击"在 Source 打开"走权威映射。

缺口：``_on_clip_double_clicked`` 的朴素公式对变速/倒放/定格片段给出
错帧（2x 片段播放头 25 → 朴素 25，权威 50）。改走
``resolve_match_frame``：拒绝（motion graphic 等）时状态栏回因、不发
打开请求（零写入）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_ui


def _clip(**kwargs: Any) -> ClipInstance:
    base: dict[str, Any] = dict(
        clip_id="c1", asset_id="asset", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
    )
    base.update(kwargs)
    return ClipInstance(**base)


def _view(qtbot: Any, timeline: TimelineSequence):
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)
    return view


def test_b06_double_click_retimed_clip_opens_authoritative_frame(qtbot):
    retimed = _clip(timeline_duration=50, speed=SpeedParams(speed=2.0))
    view = _view(qtbot, TimelineSequence(
        "b06", "B06", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(retimed,)),),
        audio_tracks=()))
    opened: list[tuple[str, int]] = []
    view.clip_open_in_source_requested.connect(
        lambda clip_id, frame: opened.append((clip_id, frame)))
    view._playhead_frame = 25

    view._on_clip_double_clicked("c1", "v1")

    assert opened == [("c1", 50)]  # 权威映射（朴素公式给 25）


def test_b06_double_click_motion_graphic_rejects_with_reason(qtbot):
    mg = _clip(asset_id="motion_graphic:mg1")
    view = _view(qtbot, TimelineSequence(
        "b06", "B06", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(mg,)),),
        audio_tracks=()))
    opened: list[tuple] = []
    view.clip_open_in_source_requested.connect(
        lambda *args: opened.append(args))
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    view._playhead_frame = 10

    view._on_clip_double_clicked("c1", "v1")

    assert opened == []                       # 零写入：不发打开请求
    assert any("no source media" in m.lower() for m in messages)
