"""Plan 3.1/3.2: VideoToolbox export bridge delivery contract, without hardware.

The bridge owns publication.  These cases pin down what it refuses to publish:

* an exact frame rate reaches the muxer and the session unrounded;
* a ``required`` audio track blocks publication when no sample reached it;
* cancellation during the audio mux or the trailer keeps the previous file;
* temporary files and the surface provider are released on every exit.

The native session and muxer are replaced by recording fakes, in the manner of
``test_vt_bridge_transaction_without_hardware``.
"""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np
import pytest

from src.shared.media.macos_vt import gpu_export_bridge
from src.shared.media.macos_vt.gpu_export_bridge import VideoToolboxGpuExportBridge
from src.shared.media.macos_vt.vt_compression import VideoToolboxCompressionError


class _Harness:
    """Recording stand-ins for the native session and the packet muxer."""

    def __init__(self, *, legacy_audio_api: bool = False,
                 after_audio_write: Callable[[], None] | None = None,
                 after_trailer: Callable[[], None] | None = None) -> None:
        self.calls: list[str] = []
        self.sessions: list[Any] = []
        self.muxers: list[Any] = []
        self.after_audio_write = after_audio_write
        self.after_trailer = after_trailer
        self.legacy_audio_api = legacy_audio_api

    # -- muxer -------------------------------------------------------------
    def muxer_write_packet(self, muxer: Any) -> None:
        muxer.packet_count += 1

    def muxer_add_audio_stream(self, muxer: Any, **options: Any) -> None:
        muxer.audio_options = options

    def muxer_write_audio_samples(self, muxer: Any, samples: Any, sample_rate: int = 48000) -> int:
        array = np.asarray(samples)
        muxer.samples.append((array.copy(), sample_rate))
        if self.after_audio_write is not None:
            self.after_audio_write()
        return sum(stored.shape[-1] for stored, _ in muxer.samples)

    def muxer_close(self, muxer: Any) -> None:
        self.calls.append("trailer")
        muxer.path.write_bytes(b"complete movie")
        if self.after_trailer is not None:
            self.after_trailer()

    # -- session -----------------------------------------------------------
    def session_close(self, session: Any) -> None:
        self.calls.append("flush")
        session._on_packet()


def _install(monkeypatch: pytest.MonkeyPatch, **options: Any) -> _Harness:
    harness = _Harness(**options)

    class Muxer:
        def __init__(self, path: str, **settings: Any) -> None:
            self.path = Path(path)
            self.settings = settings
            self.packet_count = 0
            self.audio_options: dict[str, Any] | None = None
            self.samples: list[tuple[np.ndarray, int]] = []
            self.closed = False
            harness.muxers.append(self)

        def write_packet(self) -> None:
            harness.muxer_write_packet(self)

        def add_audio_stream(self, **stream_options: Any) -> None:
            if harness.legacy_audio_api:
                # A muxer that predates ``required`` rejects unknown keywords.
                stream_options.pop("required", None)
            harness.muxer_add_audio_stream(self, **stream_options)

        def write_audio_samples(self, samples: Any, sample_rate: int = 48000) -> int:
            return harness.muxer_write_audio_samples(self, samples, sample_rate)

        def close(self) -> None:
            harness.muxer_close(self)

        def abort(self) -> None:
            harness.calls.append("muxer cleanup")

    class Session:
        last_error = ""
        dropped_packets = 0
        submit = object()

        def __init__(self, *, on_packet: Callable[[], None], **settings: Any) -> None:
            self.settings = settings
            self._on_packet = on_packet
            harness.sessions.append(self)

        def close(self) -> None:
            harness.session_close(self)

        def abort(self) -> None:
            harness.calls.append("invalidate")

    monkeypatch.setattr(gpu_export_bridge, "VideoToolboxPacketMuxer", Muxer)
    monkeypatch.setattr(gpu_export_bridge, "VideoToolboxCompressionSession", Session)
    monkeypatch.setattr(gpu_export_bridge, "VideoToolboxGpuSurfaceProvider",
                        lambda *_args, **_kwargs: SimpleNamespace(
                            close=lambda: harness.calls.append("release")))
    return harness


def _bridge(tmp_path: Path, **kwargs: Any) -> VideoToolboxGpuExportBridge:
    return VideoToolboxGpuExportBridge(
        object(), str(tmp_path / "master.mp4"), width=1920, height=1080, **kwargs)


def _leftovers(tmp_path: Path) -> list[Path]:
    """Anything the bridge staged besides the delivery itself."""
    return [entry for entry in tmp_path.iterdir() if entry.name != "master.mp4"]


# ---------------------------------------------------------------------------
# exact frame rate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps, expected", [
    (25, Fraction(25)),
    (Fraction(30000, 1001), Fraction(30000, 1001)),
    (SimpleNamespace(num=24000, den=1001), Fraction(24000, 1001)),
])
def test_bridge_passes_the_exact_rate_to_muxer_and_session(monkeypatch, tmp_path, fps, expected) -> None:
    harness = _install(monkeypatch)
    bridge = _bridge(tmp_path, fps=fps)
    assert bridge.fps == expected
    assert harness.muxers[0].settings["fps"] == expected
    assert harness.sessions[0].settings["fps"] == expected
    bridge.abort()
    assert harness.calls == ["invalidate", "release", "muxer cleanup"]
    assert _leftovers(tmp_path) == []


def test_bridge_rejects_an_inexact_rate_before_creating_anything(monkeypatch, tmp_path) -> None:
    harness = _install(monkeypatch)
    # 29.97 used to be truncated to an integer timescale; now it is refused.
    with pytest.raises(VideoToolboxCompressionError, match="frame_rate_not_exact"):
        _bridge(tmp_path, fps=29.97)
    assert harness.muxers == [] and harness.sessions == [] and harness.calls == []
    assert _leftovers(tmp_path) == []


# ---------------------------------------------------------------------------
# required audio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["missing", "empty", "published"])
def test_bridge_required_audio_is_never_published_without_samples(monkeypatch, tmp_path, mode) -> None:
    """Plan 3.1: a delivery that asked for audio is not published without it.

    The recording muxer enforces nothing itself, so this is the bridge
    holding the contract on its own sample counter.
    """
    destination = tmp_path / "master.mp4"
    destination.write_bytes(b"existing master")
    harness = _install(monkeypatch)
    bridge = _bridge(tmp_path, expected_frames=1)
    bridge.add_audio_stream("aac", 48000, 2, 256, required=True)
    assert harness.muxers[0].audio_options == dict(
        codec="aac", sample_rate=48000, channels=2, bitrate=256, required=True)
    assert bridge.audio_required is True
    # The flush below delivers the one packet that satisfies the frame count.
    if mode in ("empty", "published"):
        bridge.write_audio_samples(
            np.zeros((2, 0 if mode == "empty" else 48000), dtype=np.float32), 48000)

    if mode == "published":
        assert bridge.close() is True
        assert destination.read_bytes() == b"complete movie"
        # ``abort`` on the already closed muxer is the idempotent final step.
        assert harness.calls == ["flush", "release", "trailer", "muxer cleanup"]
        assert _leftovers(tmp_path) == []
    else:
        with pytest.raises(VideoToolboxCompressionError, match="audio_track_missing"):
            bridge.close()
        assert destination.read_bytes() == b"existing master"
        # The staged movie is gone and a second close cannot publish it either.
        assert bridge.close() is False
        assert _leftovers(tmp_path) == []
    assert harness.calls[0] == "flush" and "release" in harness.calls


@pytest.mark.parametrize("stage", ["audio_mux", "trailer"])
def test_bridge_cancel_during_audio_finalization_keeps_the_previous_file(
    monkeypatch, tmp_path, stage,
) -> None:
    """Plan 3.2: cancellation holds while audio is muxed and at the trailer."""
    destination = tmp_path / "master.mp4"
    destination.write_bytes(b"existing master")
    cancelled = threading.Event()
    harness = _install(
        monkeypatch,
        after_audio_write=cancelled.set if stage == "audio_mux" else None,
        after_trailer=cancelled.set if stage == "trailer" else None,
    )
    bridge = _bridge(tmp_path, expected_frames=1, should_cancel=cancelled.is_set)
    bridge.add_audio_stream("aac", 48000, 2, 256, required=True)
    bridge.write_audio_samples(np.zeros((2, 48000), dtype=np.float32), 48000)
    assert cancelled.is_set() is (stage == "audio_mux")
    assert bridge.close() is False
    assert cancelled.is_set()
    assert destination.read_bytes() == b"existing master"
    assert _leftovers(tmp_path) == []


def test_bridge_write_audio_samples_returns_the_next_sample_offset(monkeypatch, tmp_path) -> None:
    harness = _install(monkeypatch)
    bridge = _bridge(tmp_path)
    bridge.add_audio_stream("aac", 48000, 2, 256, required=True)
    # Chunk boundaries off the AAC frame grid continue by sample count.
    assert bridge.write_audio_samples(np.zeros((2, 20000), dtype=np.float32), 48000) == 20000
    assert bridge.write_audio_samples(np.zeros((2, 40000), dtype=np.float32), 48000) == 60000
    assert bridge.write_audio_samples(np.zeros((2, 12000), dtype=np.float32), 48000) == 72000
    assert [stored.shape[-1] for stored, _ in harness.muxers[0].samples] == [20000, 40000, 12000]
    assert {rate for _, rate in harness.muxers[0].samples} == {48000}
    bridge.abort()


def test_bridge_audio_without_required_keeps_the_historical_contract(monkeypatch, tmp_path) -> None:
    harness = _install(monkeypatch, legacy_audio_api=True)
    bridge = _bridge(tmp_path, expected_frames=1)
    # The legacy muxer rejects unknown keywords and must not receive one.
    bridge.add_audio_stream("aac", 48000, 2, 192)
    assert harness.muxers[0].audio_options == dict(
        codec="aac", sample_rate=48000, channels=2, bitrate=192)
    assert bridge.audio_required is False
    # No audio at all: the container drops the empty stream, as before.
    assert bridge.close() is True
    assert (tmp_path / "master.mp4").read_bytes() == b"complete movie"


def test_bridge_abort_after_declaring_required_audio_releases_everything(monkeypatch, tmp_path) -> None:
    destination = tmp_path / "master.mp4"
    destination.write_bytes(b"existing master")
    harness = _install(monkeypatch)
    bridge = _bridge(tmp_path, expected_frames=2)
    bridge.add_audio_stream("aac", 48000, 2, 256, required=True)
    bridge.abort()
    assert harness.calls == ["invalidate", "release", "muxer cleanup"]
    assert destination.read_bytes() == b"existing master"
    assert _leftovers(tmp_path) == []
    assert bridge.close() is False
