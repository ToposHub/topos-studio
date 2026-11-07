"""VT packets exclude PyAV EOF markers and retain Python-owned sample bytes."""
from __future__ import annotations

import ctypes
import gc
import logging
import platform
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from src.shared.media.macos_vt.packet_demux import PacketDemuxer
from src.shared.media.macos_vt import vt_session


class _Packet:
    def __init__(self, data: bytes, pts: int | None = None) -> None:
        self.backing = ctypes.create_string_buffer(data)
        self.buffer_ptr = ctypes.addressof(self.backing)
        self.buffer_size = len(data)
        self.pts = pts
        self.dts = pts
        self.duration = 1
        self.is_keyframe = pts == 0


class _Container:
    def __init__(self, packets: list[_Packet]) -> None:
        self.packets = packets
        self.iterator = iter(packets)
        self.pulled = 0

    def demux(self, stream: object) -> Iterator[_Packet]:
        for packet in self.iterator:
            self.pulled += 1
            yield packet
        # PyAV can produce this marker again on a new demux after EOF.
        yield _Packet(b"")


def test_demux_eof_markers_do_not_count_or_consume_next_sample() -> None:
    container = _Container([
        _Packet(b"first", 0), _Packet(b""), _Packet(b"second", 1),
        _Packet(b"third", 2),
    ])
    demuxer = PacketDemuxer()
    demuxer._container = container
    demuxer._stream = object()
    first = list(demuxer.packets(max_packets=2))
    assert [packet.data for packet in first] == [b"first", b"second"]
    assert [packet.frame_index for packet in first] == [0, 1]
    assert container.pulled == 3
    assert [packet.data for packet in demuxer.packets()] == [b"third"]
    assert list(demuxer.packets()) == []
    assert demuxer._frame_index == 3


def test_demux_invalid_nonempty_buffer_keeps_diagnostic_and_next_sample(
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid = _Packet(b"broken", 0)
    invalid.buffer_ptr = 0
    container = _Container([invalid, _Packet(b"valid", 1)])
    demuxer = PacketDemuxer()
    demuxer._container = container
    demuxer._stream = object()
    with caplog.at_level(logging.WARNING):
        packets = list(demuxer.packets(max_packets=1))
    assert [packet.data for packet in packets] == [b"valid"]
    assert "nonempty packet has no buffer pointer" in caplog.text


def test_empty_submit_never_calls_coremedia(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vt_session, "_PYOBJC_AVAILABLE", True)
    session = vt_session.VtSession()
    session._ctypes_session = ctypes.c_void_p(1)
    try:
        # No CoreMedia mocks are required: any call would raise -12704 or
        # access the deliberately invalid native handle.
        assert session._submit_packet_raw(b"") is False
        assert session._pending_decode_refs == []
        assert session._pending_decode_bufs == []
    finally:
        session._ctypes_session = None
        session.destroy()


def _mock_submission(
    monkeypatch: pytest.MonkeyPatch, *, block_status: int = 0,
) -> tuple[vt_session.VtSession, SimpleNamespace]:
    record = SimpleNamespace(blocks=[], retained=[], released=[], submitted=[],
                             null_allocator=object())

    def create_block(*args: object) -> tuple[int, object]:
        record.blocks.append(args)
        return block_status, object()

    monkeypatch.setitem(sys.modules, "CoreFoundation", SimpleNamespace(
        kCFAllocatorNull=record.null_allocator))
    monkeypatch.setitem(sys.modules, "CoreMedia", SimpleNamespace(
        CMBlockBufferCreateWithMemoryBlock=create_block,
        CMSampleBufferCreateReady=lambda *args: (0, 1234)))
    monkeypatch.setattr(vt_session, "_PYOBJC_AVAILABLE", True)
    monkeypatch.setattr(vt_session, "_CF_LIB", SimpleNamespace(
        CFRetain=lambda pointer: record.retained.append(pointer.value),
        CFRelease=lambda pointer: record.released.append(pointer.value)))
    monkeypatch.setattr(vt_session, "_VT_LIB", SimpleNamespace(
        VTDecompressionSessionDecodeFrame=lambda *args:
            record.submitted.append(args) or 0))
    session = vt_session.VtSession()
    session._ctypes_session = ctypes.c_void_p(1)
    return session, record


def test_nonempty_sample_retains_python_backing_until_decode_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, record = _mock_submission(monkeypatch)
    data = b"\x00\x00\x00\x04test\x00"
    try:
        assert session._submit_packet_raw(data, pts=42)
        block_args = record.blocks[0]
        assert block_args[2] == len(data)
        assert block_args[3] is record.null_allocator
        assert block_args[4] is None
        assert block_args[6] == len(data)
        assert record.retained == [1234]
        assert record.submitted[0][3].value == 42
        gc.collect()
        assert bytes(session._pending_decode_bufs[0])[:len(data)] == data
        session._release_pending_decode_refs()
        assert record.released == [1234]
        assert session._pending_decode_bufs == []
    finally:
        session._ctypes_session = None
        session.destroy()


def test_nonempty_coremedia_failure_remains_an_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    session, record = _mock_submission(monkeypatch, block_status=-12704)
    try:
        with caplog.at_level(logging.ERROR):
            assert not session._submit_packet_raw(b"bad sample")
        assert "CMBlockBufferCreateWithMemoryBlock failed: -12704" in caplog.text
        assert record.retained == []
        assert record.submitted == []
    finally:
        session._ctypes_session = None
        session.destroy()


@pytest.mark.skipif(platform.system() != "Darwin", reason="CoreMedia requires macOS")
def test_native_coremedia_accepts_nonempty_python_owned_buffer() -> None:
    coremedia = pytest.importorskip("CoreMedia")
    corefoundation = pytest.importorskip("CoreFoundation")
    data = b"\x00\x00\x00\x04test\x00"
    backing = ctypes.create_string_buffer(data)
    status, block = coremedia.CMBlockBufferCreateWithMemoryBlock(
        None, backing, len(data), corefoundation.kCFAllocatorNull,
        None, 0, len(data), 0, None)
    assert status == 0
    assert coremedia.CMBlockBufferGetDataLength(block) == len(data)
    status, copied = coremedia.CMBlockBufferCopyDataBytes(block, 0, len(data), None)
    assert status == 0
    assert bytes(copied) == data


@pytest.mark.real_media
def test_real_h264_demux_excludes_repeated_eof_marker() -> None:
    pytest.importorskip("av")
    path = Path(__file__).resolve().parents[1] / "test_videos" / "测试素材01.mov"
    if not path.exists():
        pytest.skip("H.264 media fixture is unavailable")
    demuxer = PacketDemuxer()
    try:
        info = demuxer.open(str(path))
        packets = list(demuxer.packets())
        assert len(packets) == info.total_frames
        assert all(packet.data for packet in packets)
        assert list(demuxer.packets()) == []
    finally:
        demuxer.close()
