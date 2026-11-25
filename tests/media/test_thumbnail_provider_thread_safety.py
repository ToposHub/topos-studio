"""Thread-safety tests for thumbnail_provider.py.

Verifies that QPixmap.fromImage() is NEVER called inside a QRunnable.run()
method (worker thread).  Workers must produce QImage instances only; the
conversion to QPixmap happens in the main-thread callbacks of ThumbnailProvider.
"""
from __future__ import annotations

import threading
from unittest.mock import patch, MagicMock
from typing import List

import numpy as np
import pytest
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QObject, pyqtSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FromImageCalls:
    """Tracks which threads called QPixmap.fromImage()."""

    def __init__(self) -> None:
        self.calls: List[threading.Thread] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(threading.current_thread())
        # Return a non-null QPixmap so downstream code doesn't bail out.
        # We create a tiny 1x1 QPixmap from a 1x1 QImage.
        img = QImage(1, 1, QImage.Format.Format_RGB888)
        return QPixmap.fromImage.__wrapped__(img) if hasattr(QPixmap.fromImage, '__wrapped__') else QPixmap(img)


def _make_test_image_bgr(h: int = 64, w: int = 64) -> np.ndarray:
    """Return a simple BGR test image (uint8)."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = [100, 150, 200]
    return img


class _IsolatedBridge(QObject):
    """Fresh bridge with no pre-existing connections for test isolation."""
    thumbnail_done = pyqtSignal(str, object)
    frame_thumbnail_done = pyqtSignal(str, int, object)
    clip_thumbnails_done = pyqtSignal(str, object)


def test_pause_during_seek_skips_native_read(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.shared.media import thumbnail_provider as thumbnails
    from src.shared.media import topos_source

    paused = threading.Event()
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.get.return_value = 100
    cap.set.side_effect = lambda *_args: paused.set()
    return_capture = MagicMock()
    path = "/tmp/thumb-pause-video.mp4"
    monkeypatch.setattr(topos_source, "is_topos_movie", lambda _path: False)
    monkeypatch.setattr(thumbnails, "_get_capture", lambda _path: cap)
    monkeypatch.setattr(thumbnails, "_return_capture", return_capture)
    monkeypatch.setattr(thumbnails, "_bridge", _IsolatedBridge())

    poster = thumbnails.ThumbnailRequest(path, None, 128, cancelled=paused.is_set)
    assert poster._load_video_poster_frame(path) is None
    paused.clear()

    frame = thumbnails.FrameThumbnailRequest(
        path, 10, 100, 48, None, cancelled=paused.is_set,
    )
    frame.run()

    assert cap.set.call_count == 2
    cap.read.assert_not_called()
    assert return_capture.call_count == 2


def test_thumbnail_pause_delivers_caches_and_ignores_superseded_results(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.shared.media import thumbnail_provider as thumbnails

    monkeypatch.setattr(thumbnails, "_bridge", _IsolatedBridge())
    provider = thumbnails.ThumbnailProvider()
    pool = MagicMock()
    monkeypatch.setattr(provider, "pool", pool)
    cached_path = "/tmp/thumb-already-cached.jpg"
    new_path = "/tmp/thumb-requeued.mp4"
    image = QImage(8, 8, QImage.Format.Format_RGB888)
    pixmap = QPixmap.fromImage(image)
    provider.cache.put(cached_path, pixmap)
    provider.frame_cache.put((cached_path, 2), pixmap)
    provider.get_thumbnail(new_path)
    provider.get_frame_thumbnail(new_path, 10, 100)
    old_poster, old_frame = [call.args[0] for call in pool.start.call_args_list]

    states: list[tuple[bool, bool, bool, bool]] = []
    frames: list[tuple[str, int]] = []
    provider.work_suspended_changed.connect(lambda suspended: states.append((
        suspended, provider.is_thumbnail_work_suspended,
        bool(provider.pending_requests), bool(provider.pending_frame_requests),
    )))
    provider.frame_thumbnail_ready.connect(lambda path, idx, _pixmap: frames.append((path, idx)))

    provider.set_playback_active(True)
    provider.set_playback_active(True)
    pool.clear.assert_called_once()
    assert provider.get_thumbnail(cached_path) is pixmap
    provider.get_frame_thumbnail(cached_path, 2, 100)
    assert frames == [(cached_path, 2)]
    provider.get_thumbnail(new_path)
    provider.get_frame_thumbnail(new_path, 10, 100)
    assert pool.start.call_count == 2

    provider.set_playback_active(False)
    provider.set_playback_active(False)
    assert states == [(True, True, False, False), (False, False, False, False)]
    provider.get_thumbnail(new_path)
    provider.get_frame_thumbnail(new_path, 10, 100)
    new_poster, new_frame = [call.args[0] for call in pool.start.call_args_list[2:]]

    old_poster._emit_result(image)
    old_frame._emit_result(None)
    assert provider.pending_requests == {new_path}
    assert provider.pending_frame_requests == {(new_path, 10)}
    assert provider.get_cached_thumbnail(new_path) is None

    new_poster._emit_result(image)
    new_frame._emit_result(image)
    assert not provider.pending_requests
    assert not provider.pending_frame_requests
    assert provider.get_cached_thumbnail(new_path) is not None
    assert frames == [(cached_path, 2), (new_path, 10)]


def test_timeline_reset_preserves_pending_media_card_frames(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.shared.media import thumbnail_provider as thumbnails
    from src.shared.media import topos_source

    monkeypatch.setattr(thumbnails, "_bridge", _IsolatedBridge())
    monkeypatch.setattr(topos_source, "is_topos_movie", lambda _path: False)
    provider = thumbnails.ThumbnailProvider()
    pool = MagicMock()
    monkeypatch.setattr(provider, "pool", pool)
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.read.return_value = True, _make_test_image_bgr()
    capture = MagicMock(return_value=cap)
    monkeypatch.setattr(thumbnails, "_get_capture", capture)
    monkeypatch.setattr(thumbnails, "_return_capture", lambda *_args: None)
    path = "/tmp/thumb-timeline-replaced.mp4"
    ready: list[tuple[str, int]] = []
    provider.frame_thumbnail_ready.connect(lambda path, index, _pixmap: ready.append((path, index)))

    provider.get_frame_thumbnail(path, 10, 100, asset_id="card")
    provider.request_frame_pixmap_async("timeline", path, 20, 100)
    media_request, timeline_request = [call.args[0] for call in pool.start.call_args_list]
    provider.invalidate_pending_work()
    timeline_request.run()
    media_request.run()

    capture.assert_called_once_with(path)
    assert ready == [(path, 10)]
    assert provider.get_cached_frame_pixmap("card", 10, 256, path=path) is not None
    assert provider.get_cached_frame_pixmap("timeline", 20, 48, path=path) is None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestThumbnailRequestThreadSafety:
    """ThumbnailRequest.run() must NOT call QPixmap.fromImage()."""

    def test_no_qpixmap_in_worker(self, qapp: QApplication) -> None:
        """Pixmap.fromImage is not called from ThumbnailRequest.run()."""
        from src.shared.media.thumbnail_provider import ThumbnailRequest

        tracker = _FromImageCalls()
        isolated_bridge = _IsolatedBridge()

        with (
            patch.object(ThumbnailRequest, '_load_fast_thumbnail', return_value=_make_test_image_bgr()),
            patch.object(ThumbnailRequest, '_get_cache_path', return_value='/tmp/__no_such_cache__.webp'),
            patch('src.shared.media.thumbnail_provider.QPixmap.fromImage', tracker),
            patch('src.shared.media.thumbnail_provider._bridge', isolated_bridge),
        ):
            result_container: dict = {}

            def on_bridge(path, image_or_pixmap):
                result_container['result'] = image_or_pixmap
                result_container['thread'] = threading.current_thread()

            isolated_bridge.thumbnail_done.connect(on_bridge)
            req = ThumbnailRequest('/tmp/test_photo.jpg', None, icon_size=128)
            req.run()

        # The worker should have produced a QImage, not QPixmap.
        # QPixmap.fromImage must NOT have been called from the worker.
        assert len(tracker.calls) == 0, (
            f"QPixmap.fromImage was called {len(tracker.calls)} time(s) in the worker thread!"
        )

        # The bridge should have received a QImage (not a QPixmap).
        assert 'result' in result_container, "Bridge callback was not invoked"
        assert isinstance(result_container['result'], QImage), (
            f"Expected QImage from worker, got {type(result_container['result'])}"
        )


class TestFrameThumbnailRequestThreadSafety:
    """FrameThumbnailRequest.run() must NOT call QPixmap.fromImage()."""

    def test_no_qpixmap_in_worker(self, qapp: QApplication) -> None:
        """Pixmap.fromImage is not called from FrameThumbnailRequest.run()."""
        from src.shared.media.thumbnail_provider import FrameThumbnailRequest

        tracker = _FromImageCalls()
        isolated_bridge = _IsolatedBridge()

        # Patch cv2.VideoCapture to avoid needing a real video file.
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, _make_test_image_bgr())
        mock_cap.get.return_value = 100  # frame count / height

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)

        with (
            patch('src.shared.media.thumbnail_provider._get_capture', return_value=mock_cap),
            patch('src.shared.media.thumbnail_provider._return_capture'),
            patch('src.shared.media.thumbnail_provider._get_file_lock', return_value=mock_lock),
            patch('src.shared.media.format_registry.VIDEO_EXTENSIONS', {'.mp4'}),
            patch('src.shared.media.thumbnail_provider.QPixmap.fromImage', tracker),
            patch('src.shared.media.thumbnail_provider._bridge', isolated_bridge),
        ):
            result_container: dict = {}

            def on_bridge(path, frame_index, image_or_pixmap):
                result_container['result'] = image_or_pixmap

            isolated_bridge.frame_thumbnail_done.connect(on_bridge)
            req = FrameThumbnailRequest(
                '/tmp/test_video.mp4', 5, 100, 256, None,
            )
            req.run()

        # QPixmap.fromImage should NOT have been called from this thread.
        assert len(tracker.calls) == 0, (
            f"QPixmap.fromImage was called {len(tracker.calls)} time(s) in the worker thread!"
        )

        # Bridge callback should have received a QImage.
        assert 'result' in result_container, "Bridge callback was not invoked"
        assert isinstance(result_container['result'], QImage), (
            f"Expected QImage from worker, got {type(result_container['result'])}"
        )


class TestClipThumbnailWorkerThreadSafety:
    """ClipThumbnailWorker must NOT call QPixmap.fromImage()."""

    def test_no_qpixmap_in_video_worker(self, qapp: QApplication) -> None:
        """ClipThumbnailWorker produces QImage, not QPixmap, for video."""
        from src.shared.media.thumbnail_provider import (
            ClipThumbnailWorker,
            ClipThumbnailRequest,
            ClipThumbnailSet,
        )

        tracker = _FromImageCalls()
        isolated_bridge = _IsolatedBridge()

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, _make_test_image_bgr(48, 80))
        mock_cap.get.side_effect = lambda prop: 300 if prop == 100 else 480  # frame count / height

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)

        request = ClipThumbnailRequest(
            asset_id='a1',
            asset_path='/tmp/test_video.mp4',
            clip_width_px=200,
            source_start=0,
            source_duration=100,
            pixels_per_frame=1.0,
            thumbnail_height=48,
        )

        with (
            patch('src.shared.media.thumbnail_provider._get_capture', return_value=mock_cap),
            patch('src.shared.media.thumbnail_provider._return_capture'),
            patch('src.shared.media.thumbnail_provider._get_file_lock', return_value=mock_lock),
            patch('src.shared.media.format_registry.VIDEO_EXTENSIONS', {'.mp4'}),
            patch('src.shared.media.format_registry.IMAGE_EXTENSIONS', set()),
            patch('src.shared.media.thumbnail_provider.QPixmap.fromImage', tracker),
            patch('src.shared.media.thumbnail_provider._bridge', isolated_bridge),
        ):
            result_container: dict = {}

            def on_bridge(request_key, thumbnail_set):
                result_container['result'] = thumbnail_set

            isolated_bridge.clip_thumbnails_done.connect(on_bridge)
            worker = ClipThumbnailWorker(request, None, target_height=48)
            worker.run()

        # QPixmap.fromImage should NOT have been called from the worker.
        assert len(tracker.calls) == 0, (
            f"QPixmap.fromImage was called {len(tracker.calls)} time(s) in the worker thread!"
        )

        # The thumbnail set should contain QImage frames.
        ts: ClipThumbnailSet = result_container['result']
        assert isinstance(ts, ClipThumbnailSet)
        if ts.num_frames > 0:
            _, first_image = ts.frames[0]
            assert isinstance(first_image, QImage), (
                f"Expected QImage in ClipThumbnailSet.frames, got {type(first_image)}"
            )


class TestMainCallbackConvertsToQPixmap:
    """ThumbnailProvider callbacks must convert QImage to QPixmap on the main thread."""

    def test_thumbnail_loaded_converts_to_pixmap(self, qapp: QApplication) -> None:
        """_on_thumbnail_loaded converts QImage to QPixmap and emits."""
        from src.shared.media.thumbnail_provider import ThumbnailProvider

        tracker = _FromImageCalls()

        provider = ThumbnailProvider()
        provider.pending_requests = {'/tmp/test.jpg'}

        emitted: dict = {}
        provider.thumbnail_ready.connect(lambda p, px: emitted.update(path=p, pixmap=px))

        test_img = QImage(10, 10, QImage.Format.Format_RGB888)

        with patch('src.shared.media.thumbnail_provider.QPixmap.fromImage', tracker):
            provider._on_thumbnail_loaded('/tmp/test.jpg', test_img)

        # QPixmap.fromImage should have been called once (on main thread).
        assert len(tracker.calls) == 1
        # The signal should have emitted a QPixmap.
        assert emitted.get('path') == '/tmp/test.jpg'
        # The emitted pixmap should come from QPixmap.fromImage.
        # (We can't easily check the type since our mock returns QPixmap, but
        # the tracker confirms fromImage was called.)

    def test_frame_thumbnail_loaded_converts_to_pixmap(self, qapp: QApplication) -> None:
        """_on_frame_thumbnail_loaded converts QImage to QPixmap and emits."""
        from src.shared.media.thumbnail_provider import ThumbnailProvider

        tracker = _FromImageCalls()

        provider = ThumbnailProvider()
        provider.pending_frame_requests = {('/tmp/test.mp4', 5)}

        emitted: dict = {}
        provider.frame_thumbnail_ready.connect(
            lambda p, f, px: emitted.update(path=p, frame=f, pixmap=px)
        )

        test_img = QImage(10, 10, QImage.Format.Format_RGB888)

        with patch('src.shared.media.thumbnail_provider.QPixmap.fromImage', tracker):
            provider._on_frame_thumbnail_loaded('/tmp/test.mp4', 5, test_img)

        assert len(tracker.calls) == 1
        assert emitted.get('path') == '/tmp/test.mp4'
        assert emitted.get('frame') == 5

    def test_clip_thumbnails_loaded_converts_frames(self, qapp: QApplication) -> None:
        """_on_clip_thumbnails_loaded converts QImage frames to QPixmap."""
        from src.shared.media.thumbnail_provider import (
            ThumbnailProvider,
            ClipThumbnailSet,
        )

        tracker = _FromImageCalls()

        provider = ThumbnailProvider()

        test_img = QImage(10, 10, QImage.Format.Format_RGB888)
        incoming = ClipThumbnailSet(
            request_key='test_key',
            frames=[(0, test_img), (50, test_img)],
            thumbnail_height=48,
            is_complete=True,
        )

        emitted_sets: list = []
        provider.clip_thumbnail_set_ready.connect(lambda k, s: emitted_sets.append(s))

        with patch('src.shared.media.thumbnail_provider.QPixmap.fromImage', tracker):
            provider._on_clip_thumbnails_loaded('test_key', incoming)

        # Two frames => QPixmap.fromImage called twice.
        assert len(tracker.calls) == 2

        # The emitted set should contain QPixmap frames.
        assert len(emitted_sets) == 1
        result_set: ClipThumbnailSet = emitted_sets[0]
        _, first_px = result_set.frames[0]
        assert isinstance(first_px, QPixmap), (
            f"Expected QPixmap in emitted ClipThumbnailSet, got {type(first_px)}"
        )

    def test_clip_thumbnail_conversion_is_batched_across_gui_turns(self, qapp: QApplication) -> None:
        """Large filmstrips must not convert every QImage in one UI callback."""
        from src.shared.media.thumbnail_provider import ThumbnailProvider, ClipThumbnailSet

        tracker = _FromImageCalls()
        provider = ThumbnailProvider()
        image = QImage(10, 10, QImage.Format.Format_RGB888)
        incoming = ClipThumbnailSet(
            request_key="batched-key",
            frames=[(index, image) for index in range(9)],
            thumbnail_height=48,
            is_complete=True,
        )

        with patch("src.shared.media.thumbnail_provider.QPixmap.fromImage", tracker):
            provider._on_clip_thumbnails_loaded("batched-key", incoming)
            # One immediate GUI slice; remaining frames are deferred by QTimer.
            assert len(tracker.calls) == provider.PIXMAP_CONVERSIONS_PER_GUI_TICK
            while provider._clip_conversion_queue:
                qapp.processEvents()

        assert len(tracker.calls) == 9

    def test_playback_drops_completed_clip_batch_before_pixmap_conversion(self, qapp: QApplication) -> None:
        """A queued clip result must not stall playback on the GUI thread."""
        from src.shared.media.thumbnail_provider import ThumbnailProvider, ClipThumbnailSet

        tracker = _FromImageCalls()
        provider = ThumbnailProvider()
        provider.set_playback_active(True)
        image = QImage(10, 10, QImage.Format.Format_RGB888)
        incoming = ClipThumbnailSet(
            request_key="drop-during-playback",
            frames=[(index, image) for index in range(8)],
            thumbnail_height=48,
            is_complete=True,
        )

        with patch("src.shared.media.thumbnail_provider.QPixmap.fromImage", tracker):
            provider._on_clip_thumbnails_loaded("drop-during-playback", incoming)

        assert tracker.calls == []

    def test_stale_clip_batch_is_ignored_after_generation_invalidation(self, qapp: QApplication) -> None:
        """A late worker result cannot overwrite a newer timeline request."""
        from src.shared.media.thumbnail_provider import (
            ThumbnailProvider,
            ClipThumbnailSet,
        )

        provider = ThumbnailProvider()
        provider._thumbnail_generation = 3
        image = QImage(10, 10, QImage.Format.Format_RGB888)
        incoming = ClipThumbnailSet(
            request_key="stale-key",
            frames=[(0, image)],
            thumbnail_height=48,
            is_complete=True,
            generation=2,
        )

        emitted: list[object] = []
        provider.clip_thumbnail_set_ready.connect(
            lambda _key, value: emitted.append(value)
        )
        provider._on_clip_thumbnails_loaded("stale-key", incoming)

        assert emitted == []
