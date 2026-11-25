"""The exact reverse window owns leases independently of decoder seeks."""
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import Mock

from src.features.edit.core.reverse_scrub import ReverseScrubFrames, ScrubClipRange
from src.shared.media.macos_vt.source import _ScrubRewindBuffer


@dataclass
class _Frame:
    source: object
    frame_index: int
    released: bool = False

    def allocated_bytes(self) -> int:
        return 4

    def acquire_lease(self) -> '_Frame':
        assert not self.released
        return replace(self)

    def release(self) -> None:
        assert not self.released
        self.released = True


def test_exact_history_survives_turn_but_not_clip_or_generation_change(monkeypatch) -> None:
    cache = _ScrubRewindBuffer(32, 12, preview_limit=24)
    reverse = ReverseScrubFrames()
    monkeypatch.setattr(reverse, '_buffer', lambda: cache)
    monkeypatch.setattr(reverse, '_start_worker_locked', lambda: setattr(reverse, '_thread', Mock()))
    clip = ScrubClipRange(generation=5, clip_id='cut-a', timeline_start=10, timeline_stop=30, source_start=20)
    source = SimpleNamespace(
        scrub_media_identity=(0, 1, 2, 3),
        supports_h264_scrub_gops=lambda: True,
        h264_scrub_frame_matches=lambda frame: True,
    )
    foreground = _Frame(source, frame_index=33)
    reverse.move(5, 23, 1, 50.0)
    reverse.observe('stream', '/source.mov', clip, source, foreground)
    assert reverse.borrow('stream', clip, 33) is None
    assert cache._held_bytes == 4

    reverse.move(5, 24, -1, 60.0)
    renderer_frame = reverse.borrow('stream', clip, 33)
    assert renderer_frame is not None and renderer_frame.frame_index == 33
    assert renderer_frame is not foreground
    assert reverse.borrow('other-route', clip, 33) is None

    reverse.move(5, 30, -1, 60.0)
    assert not reverse.engaged(5)
    assert cache._held_bytes == 0
    assert reverse.borrow('stream', clip, 33) is None
    assert not renderer_frame.released

    reverse.move(6, 23, 1, 50.0)
    reverse.observe('stream', '/source.mov', clip, source, foreground)
    assert cache._held_bytes == 0
    renderer_frame.release()


def test_preview_and_decoder_rewind_share_one_cap_and_release_independently() -> None:
    cache = _ScrubRewindBuffer(20, 12, preview_limit=12)
    source = object()
    owner, second_preview = object(), object()
    foreground = _Frame(source, 20)
    assert cache.store(foreground, 4)
    old = _Frame(source, 41)
    assert cache.store_preview(owner, old, 4, head=41, reverse=False)
    assert cache.store_preview(owner, _Frame(source, 42), 4, head=42, reverse=False)

    accepted = _Frame(source, 36)
    assert cache.store_preview(second_preview, accepted, 8, head=45, reverse=True)
    assert old.released
    assert cache._held_bytes == 16
    assert not foreground.released

    rendering = cache.borrow_preview(second_preview, 36)
    cache.discard(second_preview)
    assert accepted.released
    assert rendering is not None and not rendering.released
    rendering.release()
    cache.discard(owner)
    cache.discard(source)
    assert cache._held_bytes == 0


def test_new_route_reopens_the_same_path_while_old_decode_drains(monkeypatch) -> None:
    import threading
    from src.shared.media.macos_vt import source as vt_source

    first_started = threading.Event()
    resume_first = threading.Event()
    reopened = threading.Event()
    opened = []
    media = (0, 1, 2, 3)

    class Cursor:
        scrub_media_identity = media

        def open(self, path: str, *, require_hardware: bool) -> bool:
            assert require_hardware
            opened.append(path)
            if len(opened) == 2:
                reopened.set()
            return True

        def supports_h264_scrub_gops(self) -> bool:
            return True

        def previous_h264_scrub_gop(self, head: int) -> object:
            return SimpleNamespace(first_frame=25, stop_frame=50)

        def decode_h264_scrub_gop(self, *args, is_stale, **kwargs) -> int:
            first_started.set()
            assert resume_first.wait(2)
            assert is_stale()
            return 0

        def close(self) -> None:
            pass

    monkeypatch.setattr(vt_source, 'MacOSVtZeroCopySource', Cursor)
    cache = _ScrubRewindBuffer(32, 12, preview_limit=24)
    reverse = ReverseScrubFrames()
    monkeypatch.setattr(reverse, '_buffer', lambda: cache)
    foreground = SimpleNamespace(
        scrub_media_identity=media,
        h264_scrub_frame_matches=lambda frame: True,
    )
    frame = _Frame(foreground, frame_index=55)
    with_cleanup = None
    try:
        reverse.move(5, 55, -1, 60.0)
        reverse.observe('stream', '/same.mov', ScrubClipRange(5, 'a', 0, 100, 0), foreground, frame)
        with_cleanup = reverse._thread
        assert first_started.wait(2)

        reverse.cancel()
        reverse.move(6, 55, 1, 60.0)
        reverse.observe('stream', '/same.mov', ScrubClipRange(6, 'a', 0, 100, 0), foreground, frame)
        resume_first.set()
        assert reopened.wait(2)
        assert opened == ['/same.mov', '/same.mov']
    finally:
        reverse.cancel(close=True)
        resume_first.set()
        if with_cleanup is not None:
            with_cleanup.join(2)
