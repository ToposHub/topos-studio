"""调色页快拖冻结修复：scrub 高频 seek 不翻 generation + 最新完成帧过渡显示。

真实 App 实测（4K ProRes，flag 关）：快拖 249 次 seek 仅 1 帧显示。
根因一：每次 seek 递增 generation，40-80ms 完成的 master 解码入队时
全部被丢为过期帧，队列永远为空。根因二：精确目标总在解码完成前移走，
take_ready 永远 miss；无 proxy 轨道时 proxy 兜底恒为 None。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.video.session import VideoSession


def _session_with_queue():
    session = VideoSession.__new__(VideoSession)
    session._frame_queue = MagicMock()
    session._current_master_frame = None
    return session


def _entry(frame_index: int):
    return SimpleNamespace(frame=SimpleNamespace(frame_index=frame_index))


def test_scrub_seek_skips_generation_churn() -> None:
    """churn_generation=False 时 seek 不递增 generation（解码不被作废）。"""
    import threading

    session = VideoSession.__new__(VideoSession)
    session._state = None
    session._playback_clock = None
    session._worker_manager = None
    session._generation = 7
    session._generation_lock = threading.Lock()
    session._state_lock = threading.Lock()
    session._current_proxy_frame = None
    session._metadata = None  # total_frames property guard
    session._request_current_proxy_frame_if_missing = lambda f: None
    session._notify_state_change = lambda: None
    session._stats = MagicMock()

    from unittest.mock import patch

    with patch.object(session, "_increment_generation") as bump:
        session.seek_to_frame(100, blocking=False, churn_generation=False)
        bump.assert_not_called()
        session.seek_to_frame(100, blocking=False, churn_generation=True)
        bump.assert_called_once()


def test_take_latest_ready_master_frame_returns_newest_in_range() -> None:
    session = _session_with_queue()
    session._frame_queue.dequeue.side_effect = [_entry(50), _entry(64), None]

    frame = session.take_latest_ready_master_frame(70)
    assert frame is not None
    assert frame.frame_index == 64
    assert session._current_master_frame.frame_index == 64


def test_take_latest_ready_master_frame_rejects_far_frames() -> None:
    session = _session_with_queue()
    session._frame_queue.dequeue.side_effect = [_entry(10), None]

    frame = session.take_latest_ready_master_frame(500, max_frame_distance=96)
    assert frame is None


def test_take_latest_ready_master_frame_empty_queue() -> None:
    session = _session_with_queue()
    session._frame_queue.dequeue.return_value = None

    assert session.take_latest_ready_master_frame(10) is None


def test_engine_fast_scrub_skips_generation_and_uses_latest() -> None:
    """引擎层：fast_scrub seek 不翻代数，且 take_ready miss 时取最新完成帧。"""
    from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

    coordinator = MagicMock()
    latest_frame = SimpleNamespace(frame_index=42)
    coordinator.take_ready_master_frame.return_value = None
    coordinator.take_latest_ready_master_frame.return_value = latest_frame
    coordinator.get_current_frame_data.return_value = None

    submitted = []
    engine = SimpleNamespace(
        _use_dual_path=True,
        _coordinator=coordinator,
        is_playing=False,
        current_frame=0,
        _generation_id=0,
        stop_playback=lambda: None,
        frame_changed=MagicMock(),
        _submit_decoded_frame_playback=lambda frame, fn, **k: submitted.append((frame, fn, k)) or True,
        _set_static_preview_frame_hold=lambda v: None,
        _begin_static_preview_generation=lambda: None,
        _mark_renderer_video_texture_pending=lambda: None,
    )
    # 该类继承 QObject，不能绕过 __init__ 构造实例；直接以未绑定方法调用。
    EnhancedVideoPlaybackEngine.seek_to_frame(
        engine, 45, blocking=False, allow_proxy_fallback=True, fast_scrub=True
    )

    coordinator.seek_to_frame.assert_called_once_with(
        45, blocking=False, churn_generation=False
    )
    coordinator.take_latest_ready_master_frame.assert_called_once_with(45)
    assert submitted, "latest completed master must be submitted for display"
    frame, fn, kwargs = submitted[0]
    assert frame is latest_frame


def test_present_latest_ready_scrub_frame_submits_newest() -> None:
    """泵上屏：取最新完成 master 帧提交显示（帧号用完成帧自身的索引）。"""
    from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

    coordinator = MagicMock()
    frame = SimpleNamespace(frame_index=77)
    coordinator.take_latest_ready_master_frame.return_value = frame

    submitted = []
    engine = SimpleNamespace(
        _use_dual_path=True,
        _coordinator=coordinator,
        _last_displayed_frame_index=-1,
        _submit_decoded_frame_playback=lambda f, fn, **k: submitted.append((f, fn, k)),
    )
    assert EnhancedVideoPlaybackEngine.present_latest_ready_scrub_frame(engine, 80) is True
    coordinator.take_latest_ready_master_frame.assert_called_once_with(80)
    assert submitted and submitted[0][1] == 77


def test_present_latest_ready_scrub_frame_dedup_and_miss() -> None:
    """队列空 → False 不提交；已完成帧与上次显示相同 → 去重不重复提交。"""
    from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

    coordinator = MagicMock()
    submitted = []
    engine = SimpleNamespace(
        _use_dual_path=True,
        _coordinator=coordinator,
        _last_displayed_frame_index=-1,
        _submit_decoded_frame_playback=lambda f, fn, **k: submitted.append((f, fn, k)),
    )

    coordinator.take_latest_ready_master_frame.return_value = None
    assert EnhancedVideoPlaybackEngine.present_latest_ready_scrub_frame(engine, 80) is False
    assert not submitted

    engine._last_displayed_frame_index = 5
    coordinator.take_latest_ready_master_frame.return_value = SimpleNamespace(frame_index=5)
    assert EnhancedVideoPlaybackEngine.present_latest_ready_scrub_frame(engine, 80) is True
    assert not submitted


def test_present_latest_ready_requires_coordinator_api() -> None:
    from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

    coordinator = MagicMock(spec=["take_ready_master_frame"])  # 无 latest 接口
    engine = SimpleNamespace(
        _use_dual_path=True,
        _coordinator=coordinator,
        _last_displayed_frame_index=-1,
        _submit_decoded_frame_playback=lambda f, fn, **k: None,
    )
    assert EnhancedVideoPlaybackEngine.present_latest_ready_scrub_frame(engine, 80) is False
