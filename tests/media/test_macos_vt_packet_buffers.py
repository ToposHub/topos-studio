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

from src.shared.media.macos_vt.packet_demux import CompressedPacket, PacketDemuxer, VideoStreamInfo
from src.shared.media.macos_vt import vt_session


def test_destroy_releases_pending_outputs_and_preserves_external_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    session = vt_session.VtSession()
    session._output_queue = vt_session.VtDecodeOutputQueue()
    released = []
    monkeypatch.setattr(vt_session, '_CV_LIB', SimpleNamespace(
        CVPixelBufferRelease=lambda pointer: released.append(pointer.value),
    ))
    pending = vt_session.VtDecodedPixelBuffer(1, 0, 0.0, 1920, 1080)
    external = vt_session.VtDecodedPixelBuffer(2, 1, 0.04, 1920, 1080)
    session._output_queue._queue.put_nowait(pending)
    session.destroy()
    assert released == [1]
    session.release_decoded_frame(external)
    assert released == [1, 2]


def test_regular_decode_keeps_the_timestamp_from_an_earlier_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    session = vt_session.VtSession()
    older = vt_session.VtDecodedPixelBuffer(1, 36, 0.0, 1920, 1080, is_iosurface_backed=True)
    session._output_queue = vt_session.VtDecodeOutputQueue()
    session._output_queue._queue.put_nowait(older)
    monkeypatch.setattr(session, '_submit_packet_raw', lambda data, pts: True)
    result = session._decode_packet_impl(b'frame', 51, False)
    assert result is older and older.pts == 36


@pytest.mark.parametrize(("codec", "expected"), [
    ("h264", [3] * 7 + ["wait", 3, 3, "wait", 0, 3, 1, "wait", 0]),
    ("hevc", [3] * 7 + ["wait", 3, 3, "wait", 0, 3, 1, "wait", 0]),
    ("prores", [2] * 9 + [0, 2, 0, 0]),
])
def test_scrub_batches_reference_packets_and_optional_rewind_output(
    monkeypatch: pytest.MonkeyPatch, codec: str, expected: list[int | str],
) -> None:
    session = vt_session.VtSession()
    monkeypatch.setattr(vt_session.VtSession, "is_created", property(lambda _: True))
    session._codec = codec
    submitted = []

    def submit(data: bytes, pts: int, *, decode_flags: int) -> bool:
        submitted.append(decode_flags)
        return True

    def wait() -> None:
        submitted.append("wait")
        session._scrub_async_sample_count = 0

    monkeypatch.setattr(session, "_submit_packet_raw", submit)
    monkeypatch.setattr(session, "_wait_for_pending_decode", wait)
    for pts, reference_only in enumerate([True] * 9 + [False, True]):
        session.decode_scrub_packet(b"packet", pts, reference_only=reference_only)
    session.decode_scrub_packet(b"packet", 11, reference_only=False, async_output=True)
    session.decode_scrub_packet(b"packet", 12, reference_only=False)
    assert submitted == expected


def test_failed_native_wait_preserves_compressed_backing(monkeypatch: pytest.MonkeyPatch) -> None:
    session = vt_session.VtSession()
    session._ctypes_session = ctypes.c_void_p(1)
    session._scrub_async_sample_count = 7
    session._pending_decode_refs.append(ctypes.c_void_p(2))
    session._pending_decode_bufs.append(b"compressed")
    status = -12903
    freed = []
    monkeypatch.setattr(vt_session, "_VT_LIB", SimpleNamespace(
        VTDecompressionSessionWaitForAsynchronousFrames=lambda _: status,
    ))
    monkeypatch.setattr(vt_session, "_CF_LIB", SimpleNamespace(CFRelease=freed.append))
    try:
        with pytest.raises(RuntimeError, match="OSStatus -12903"):
            session.flush()
        assert len(session._pending_decode_refs) == 1
        assert session._pending_decode_bufs == [b"compressed"]
        assert session._scrub_async_sample_count == 7
        assert freed == []
        status = 0
        session.flush()
        assert session._pending_decode_refs == []
        assert session._pending_decode_bufs == []
        assert session._scrub_async_sample_count == 0
        assert [ref.value for ref in freed] == [2]
    finally:
        session._ctypes_session = None


def test_failed_pending_drain_does_not_submit_another_packet(monkeypatch: pytest.MonkeyPatch) -> None:
    session = vt_session.VtSession()
    monkeypatch.setattr(vt_session, "_PYOBJC_AVAILABLE", True)
    monkeypatch.setattr(vt_session.VtSession, "is_created", property(lambda _: True))
    submitted = []

    def submit(data: bytes, pts: int, *, decode_flags: int = 0) -> bool:
        submitted.append(pts)
        return False

    def fail_wait() -> None:
        raise RuntimeError("test drain failure")

    with monkeypatch.context() as patch:
        patch.setattr(session, "_submit_packet_raw", submit)
        patch.setattr(session, "_wait_for_pending_decode", fail_wait)
        session._pending_decode_bufs = [b"sample"] * 128
        session._pending_decode_refs = [ctypes.c_void_p()] * 128
        assert session.decode_packet(b"frame", 1) is None
        assert session.submit_packet(b"frame", 2) is False
        with pytest.raises(RuntimeError, match="test drain failure"):
            session.decode_scrub_packet(b"frame", 3, reference_only=True)
        assert submitted == []
        assert len(session._pending_decode_bufs) == len(session._pending_decode_refs) == 128
        session._pending_decode_refs.clear()
        session._pending_decode_bufs.clear()


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


def test_hevc_skip_requires_only_complete_disposable_slices() -> None:
    demuxer = PacketDemuxer()
    demuxer._hevc_nal_length_size = 4
    trail_n = b'\x00\x00\x00\x03\x00\x01\x80'
    sei = b'\x00\x00\x00\x03\x4e\x01\x80'
    trail_r = b'\x00\x00\x00\x03\x02\x01\x80'

    def disposable(data: bytes, *, keyframe: bool = False) -> bool:
        return demuxer.is_disposable_hevc_sample(CompressedPacket(data, 1, 1, 1, keyframe))

    assert disposable(trail_n + trail_n)
    assert not disposable(trail_n, keyframe=True)
    assert not disposable(trail_n + sei)
    assert not disposable(trail_n + trail_r)
    assert not disposable(trail_n[:-1])
    assert not disposable(trail_n + b'\x00')
    assert not disposable(b'\x00\x00\x00\x03\x00\x02\x80')


def test_hevc_skip_requires_one_fixed_unlayered_sample_entry(tmp_path: Path) -> None:
    config = bytes([1, 2] + [0] * 19 + [0x0f, 0])  # Main 10, one temporal layer.
    demuxer = PacketDemuxer()
    demuxer._info = VideoStreamInfo(codec_tag='hvc1', extradata=config)

    def box(name: bytes, body: bytes) -> bytes:
        return (len(body) + 8).to_bytes(4, 'big') + name + body

    def movie(entries: list[bytes]) -> bytes:
        stsd = box(b'stsd', b'\x00' * 4 + len(entries).to_bytes(4, 'big') + b''.join(entries))
        hdlr = box(b'hdlr', b'\x00' * 8 + b'vide')
        return box(b'moov', box(b'trak', box(b'mdia', hdlr + box(b'minf', box(b'stbl', stsd)))))

    entry = box(b'hvc1', b'\x00' * 78 + box(b'hvcC', config))
    layered = box(b'hvc1', b'\x00' * 78 + box(b'hvcC', config) + box(b'lhvC', b'layer'))
    path = tmp_path / 'test.mov'
    path.write_bytes(movie([entry]))
    assert demuxer._single_layer_hevc_nal_length_size(str(path)) == 4
    path.write_bytes(movie([entry, layered]))
    assert demuxer._single_layer_hevc_nal_length_size(str(path)) == 0
    path.write_bytes(movie([layered]))
    assert demuxer._single_layer_hevc_nal_length_size(str(path)) == 0
    demuxer._info.extradata = config[:21] + b'\x17\x00'  # More than one temporal layer.
    path.write_bytes(movie([entry]))
    assert demuxer._single_layer_hevc_nal_length_size(str(path)) == 0


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
        assert record.submitted[0][3].value == vt_session._packet_pts_refcon(42)
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
        path = path.with_name("4K_H264.mov")
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
