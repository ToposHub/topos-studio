"""Tests for playback sync-decode guard."""

import logging
import threading


def test_sync_decode_guard_ignores_decode_worker_threads(caplog):
    """Background decode workers may synchronously read frames during playback."""
    from src.shared.video.video_playback import (
        _assert_no_sync_decode_in_playback,
        _register_playback_engine,
    )

    engine_id = 987654
    _register_playback_engine(engine_id, True)

    try:
        with caplog.at_level(logging.ERROR, logger="src.shared.video.video_playback"):
            worker = threading.Thread(
                target=_assert_no_sync_decode_in_playback,
                kwargs={
                    "operation": "SourceManager.get_proxy_frame",
                    "frame_index": 1,
                    "source_type": "proxy",
                },
            )
            worker.start()
            worker.join(timeout=2.0)

        assert "P2-1 VIOLATION" not in caplog.text
    finally:
        _register_playback_engine(engine_id, False)


def test_sync_decode_guard_still_flags_main_thread(caplog):
    """The guard should still catch UI/main-thread synchronous decode."""
    from src.shared.video.video_playback import (
        _assert_no_sync_decode_in_playback,
        _register_playback_engine,
    )

    engine_id = 987655
    _register_playback_engine(engine_id, True)

    try:
        with caplog.at_level(logging.ERROR, logger="src.shared.video.video_playback"):
            _assert_no_sync_decode_in_playback(
                operation="SourceManager.get_proxy_frame",
                frame_index=1,
                source_type="proxy",
            )

        assert "P2-1 VIOLATION" in caplog.text
    finally:
        _register_playback_engine(engine_id, False)
