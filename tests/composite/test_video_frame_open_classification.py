"""P2-8: VideoFrameProviderService open-failure classification contract.

The service open failure must distinguish:
- ``MediaOpenError`` → unsupported/corrupt media: session-permanent skip,
  ONE warning per path (no per-frame log noise, no retry storm);
- other exceptions → transient: bounded retries (_SERVICE_TRANSIENT_MAX),
  then demoted to session-permanent with one final warning;
- a later success clears the transient count.
"""

import logging
import os
import tempfile

import pytest

from src.features.composite.nodes.video_frame_node import (
    _SERVICE_OPEN_UNSUPPORTED,
    _SERVICE_OPEN_TRANSIENT_COUNT,
    _SERVICE_TRANSIENT_MAX,
    VideoFrameNode,
    VideoLoadState,
)
from src.shared.media.source import MediaOpenError

pytestmark = pytest.mark.composite_core


class _RaisingContext:
    """VideoContext stand-in whose open_clip always fails with `exc`."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.open_calls = 0

    def open_clip(self, path, node_id):
        self.open_calls += 1
        raise self.exc

    def get_clip_info(self, clip_id):  # pragma: no cover - never reached
        return None

    def close_clip_for_node(self, node_id):  # pragma: no cover
        pass


@pytest.fixture(autouse=True)
def _clean_classification_state():
    _SERVICE_OPEN_UNSUPPORTED.clear()
    _SERVICE_OPEN_TRANSIENT_COUNT.clear()
    yield
    _SERVICE_OPEN_UNSUPPORTED.clear()
    _SERVICE_OPEN_TRANSIENT_COUNT.clear()


def _make_node_with_context(monkeypatch, exc) -> tuple:
    node = VideoFrameNode()
    ctx = _RaisingContext(exc)
    monkeypatch.setattr(node, "_get_video_context", lambda: ctx)
    return node, ctx


def _real_file():
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    return path


class TestProviderOpenClassification:
    def test_media_open_error_is_session_permanent(self, monkeypatch, caplog):
        """MediaOpenError → one warning, path marked unsupported, every
        later open attempt skips the service entirely (no log noise)."""
        path = _real_file()
        node, ctx = _make_node_with_context(
            monkeypatch, MediaOpenError("unsupported codec", source_path=path))
        with caplog.at_level(logging.DEBUG,
                             logger="src.features.composite.nodes.video_frame_node"):
            assert node._open_video_via_service(path) is False
            assert ctx.open_calls == 1
            # Retry storms and close/open cycles must not re-attempt.
            assert node._open_video_via_service(path) is False
            assert node._open_video_via_service(path) is False
            assert ctx.open_calls == 1
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "unsupported_or_corrupt" in warnings[0].getMessage()
        assert path in _SERVICE_OPEN_UNSUPPORTED

    def test_transient_failures_are_retried_then_demoted(
            self, monkeypatch, caplog):
        """Generic exceptions retry up to _SERVICE_TRANSIENT_MAX, warning
        only once (first failure); the demotion logs once more."""
        path = _real_file()
        node, ctx = _make_node_with_context(
            monkeypatch, RuntimeError("service not ready"))
        with caplog.at_level(logging.DEBUG,
                             logger="src.features.composite.nodes.video_frame_node"):
            for _ in range(_SERVICE_TRANSIENT_MAX + 1):
                assert node._open_video_via_service(path) is False
            # MAX transient retries, then the demoted-permanent skip.
            assert ctx.open_calls == _SERVICE_TRANSIENT_MAX
            assert node._open_video_via_service(path) is False
            assert ctx.open_calls == _SERVICE_TRANSIENT_MAX
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2  # first transient + demotion
        assert "transient_x1" in warnings[0].getMessage()
        assert "demoted_permanent" in warnings[1].getMessage()

    def test_success_clears_transient_count(self, monkeypatch):
        """A file that failed transiently then opens OK must not carry a
        stale failure count."""
        path = _real_file()
        node, ctx = _make_node_with_context(monkeypatch, RuntimeError("busy"))

        class _OkContext(_RaisingContext):
            def open_clip(self, path_, node_id):
                self.open_calls += 1
                return "clip-1"

            def get_clip_info(self, clip_id):
                class _Info:
                    fps = 25.0
                    total_frames = 10
                    width = 16
                    height = 16
                    bit_depth = 8
                    is_hdr = False
                    codec_name = "h264"
                    color_primaries = ""
                    color_transfer = ""
                    color_matrix = ""
                return _Info()

        ok = _OkContext(RuntimeError("unused"))
        monkeypatch.setattr(node, "_get_video_context", lambda: ok)

        assert node._open_video_via_service(path) is True
        assert path not in _SERVICE_OPEN_TRANSIENT_COUNT
        assert path not in _SERVICE_OPEN_UNSUPPORTED

    def test_missing_file_fails_before_service(self, monkeypatch):
        """File-not-found must not spend a service attempt (and must not
        poison the classification cache for a later valid path)."""
        node, ctx = _make_node_with_context(
            monkeypatch, MediaOpenError("never raised"))
        assert node._open_video_via_service("") is False
        assert node._open_video_via_service(
            os.path.join(tempfile.gettempdir(), "no_such_file_xyz.mp4")
        ) is False
        assert ctx.open_calls == 0
        assert node._load_state == VideoLoadState.FAILED

    def test_fallback_reason_recorded_for_diagnostics(self, monkeypatch):
        """The node records WHY it left the service path (P3-13 groundwork:
        user-explainable fallbacks)."""
        path = _real_file()
        node, _ = _make_node_with_context(
            monkeypatch, MediaOpenError("bad container", source_path=path))
        node._open_video_via_service(path)
        assert node._service_fallback_reason == "unsupported_or_corrupt"
