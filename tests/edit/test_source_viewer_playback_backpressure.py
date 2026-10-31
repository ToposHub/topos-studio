"""Tests for Source Viewer playback backpressure.

The playhead must not outrun the decoder: when a GPU decode is in flight and
the wall-clock target frame has run more than ``_PLAYBACK_DEFER_LAG_FRAMES``
ahead of it, the playback tick reanchors the clock to the visible frame
instead of submitting another request that ``set_gpu_frame`` would only
discard.  Without this the slider keeps moving while the picture freezes,
and only catches up when playback pauses — the original bug.
"""
from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.source_viewer import SourceViewerWidget


def _make_video_asset() -> MediaAsset:
    return MediaAsset(
        asset_id="asset-1",
        path="/tmp/source.mov",
        media_type="video",
        display_name="source.mov",
        metadata={"fps": 24.0, "total_frames": 240, "width": 1920, "height": 1080},
    )


def _open_and_play(widget: SourceViewerWidget) -> None:
    widget.open_asset(_make_video_asset())
    widget.play()


def test_should_defer_is_false_when_not_playing(qapp) -> None:
    widget = SourceViewerWidget()
    widget.open_asset(_make_video_asset())
    widget._gpu_pending_frame = 100
    # Not playing → never defer, even with a huge lag.
    assert widget._should_defer_playback_tick(200) is False


def test_should_defer_is_false_when_nothing_in_flight(qapp) -> None:
    widget = SourceViewerWidget()
    _open_and_play(widget)
    widget._gpu_pending_frame = -1
    # Nothing in flight → submit freely.
    assert widget._should_defer_playback_tick(widget._current_frame + 50) is False


def test_should_defer_is_false_within_lag_tolerance(qapp) -> None:
    widget = SourceViewerWidget()
    _open_and_play(widget)
    widget._gpu_pending_frame = 100
    # 1-2 frame Qt scheduling jitter must NOT trip backpressure (else low-fps
    # clips stutter).  Only lag > tolerance defers.
    lag = widget._PLAYBACK_DEFER_LAG_FRAMES
    assert widget._should_defer_playback_tick(100 + lag) is False
    assert widget._should_defer_playback_tick(100 + lag + 1) is True


def test_should_defer_is_true_when_playhead_outruns_decode(qapp) -> None:
    widget = SourceViewerWidget()
    _open_and_play(widget)
    widget._gpu_pending_frame = 100
    assert widget._should_defer_playback_tick(100 + widget._PLAYBACK_DEFER_LAG_FRAMES + 5) is True


def test_reanchor_resets_clock_baseline_to_frame(qapp) -> None:
    """After reanchor, the next tick computes its target relative to the
    reanchored frame, not the original play start."""
    widget = SourceViewerWidget()
    _open_and_play(widget)

    # Pretend a stale baseline from long ago so target would race ahead.
    widget._playback_start_frame = 0
    # Reanchor to frame 50 immediately → next tick target ≈ 50.
    widget._reanchor_playback_clock(50)
    assert widget._playback_start_frame == 50
    # Recompute the target the same way _on_playback_tick does, at ~now.
    import time as _time
    elapsed = _time.perf_counter() - widget._playback_start_time
    target = widget._playback_start_frame + int(elapsed * widget._fps)
    assert target in (50, 51)  # within one frame of the reanchor point


def test_playback_tick_reanchors_instead_of_racing_ahead(qapp, monkeypatch) -> None:
    """With a decode in flight and the playhead far ahead, the tick must hold
    at the visible frame and reanchor — NOT submit a new frame load."""
    widget = SourceViewerWidget()
    _open_and_play(widget)

    loaded: list[int] = []
    monkeypatch.setattr(widget, "_load_frame", lambda frame: loaded.append(int(frame)))

    # Visible frame is 40; a GPU decode for frame 40 is still in flight.
    widget._last_good_frame = 40
    widget._current_frame = 40
    widget._gpu_pending_frame = 40
    # Wall clock says we "should" be at frame 80 (>> lag tolerance).
    widget._playback_start_frame = 0
    # Force target_frame = 80 on the next tick: start_time set so that
    # elapsed*fps ≈ 80 at 24fps → elapsed ≈ 3.33s in the past.
    import time as _time
    widget._playback_start_time = _time.perf_counter() - 3.4

    widget._on_playback_tick()

    # Playhead held at the visible frame, not advanced to 80.
    assert widget._current_frame == 40
    # No new frame submitted (decode already in flight for 40).
    assert loaded == []
    # Clock reanchored so the next tick resumes from 40, not from 0.
    assert widget._playback_start_frame == 40


def test_playback_tick_advances_normally_when_decode_idle(qapp, monkeypatch) -> None:
    """With no decode in flight, backpressure must not interfere: the tick
    advances to the wall-clock target and loads it."""
    widget = SourceViewerWidget()
    _open_and_play(widget)

    loaded: list[int] = []
    monkeypatch.setattr(widget, "_load_frame", lambda frame: loaded.append(int(frame)))

    widget._current_frame = 0
    widget._gpu_pending_frame = -1  # nothing in flight
    widget._last_good_frame = 0
    widget._playback_start_frame = 0
    # Force target_frame = 1 at 24fps: start_time set so elapsed ≈ one frame.
    import time as _time
    widget._playback_start_time = _time.perf_counter() - (1.0 / 24.0)

    widget._on_playback_tick()

    assert widget._current_frame == 1
    assert loaded == [1]
