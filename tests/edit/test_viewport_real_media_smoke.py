"""I07（复查计划）：用户素材（tests/test_videos 冻结语料）Source 冒烟。

offscreen 环境下可真实执行的部分：真实 ProRes 文件经 CPU 解码 worker
（FFmpeg 线程）打开/seek/取帧/播放时钟推进/In-Out 校验/工程释放全链，
不断言渲染像素与音频听感（那属于真机证据边界）。素材缺失时按
``real_media`` 语义 skip（STRICT 作业下 fail）。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.real_media import expected_frames, require_video

from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.source_viewer import SourceViewerWidget

pytestmark = [pytest.mark.edit_ui, pytest.mark.real_media]


def _real_asset(path) -> MediaAsset:
    return MediaAsset(
        asset_id="i07-real", path=str(path), media_type="video",
        display_name=path.name,
        metadata={"fps": 25.0, "total_frames": expected_frames(path),
                  "width": 1920, "height": 1080,
                  "audio_info": {"channels": 2}})


def _widget(qapp: Any, path) -> SourceViewerWidget:
    widget = SourceViewerWidget()
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    widget._start_source_audio = lambda *_a, **_k: None  # type: ignore[method-assign]
    widget._pause_source_audio = lambda: None  # type: ignore[method-assign]
    widget.open_asset(_real_asset(path))
    return widget


def test_i07_real_media_open_seek_decode(qtbot, qapp):
    path = require_video("2K_prores422.mov")
    widget = _widget(qapp, path)
    qtbot.addWidget(widget)
    assert widget.source_time_domain is not None
    assert widget.source_time_domain.total_frames == expected_frames(path)

    widget.seek(24)
    qtbot.waitUntil(lambda: widget._last_good_frame == 24, timeout=8000)
    stats = widget.gpu_slot_stats
    assert stats["decode_submitted"] >= 1   # 真实解码请求已提交


def test_i07_real_media_playback_clock_advances(qtbot, qapp):
    path = require_video("2K_prores422.mov")
    widget = _widget(qtbot, path)
    qtbot.addWidget(widget)

    widget.toggle_playback()
    assert widget.is_playing
    qtbot.wait(400)                          # 真实时钟推进（25fps ≈ 10 帧）
    frame_now = widget.current_frame
    widget.pause()
    assert not widget.is_playing
    assert frame_now > 0                     # 播放头真实前移

    # 工程释放边界在真实解码会话上同样成立（关闭解码器/清缓存）
    widget.release_for_project_close()
    assert widget.current_asset is None


def test_i07_real_media_marks_and_timecode(qtbot, qapp):
    path = require_video("2K_prores422.mov")
    widget = _widget(qtbot, path)
    qtbot.addWidget(widget)

    widget.set_in_point(10)
    widget.set_out_point(99)
    assert (widget._in_point, widget._out_point) == (10, 99)
    rejected: list[str] = []
    widget.range_rejected.connect(rejected.append)
    widget.set_out_point(5)                  # 反置拒绝（零写入）
    assert rejected == ["in_after_out"]
    assert widget._out_point == 99
    # 25fps CFR 的 TC 显示（起始 00:00:00:00，帧 50 → 00:00:02:00）
    assert widget._frame_to_timecode(50) == "00:00:02:00"
