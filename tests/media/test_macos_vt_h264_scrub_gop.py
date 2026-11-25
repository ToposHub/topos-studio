"""Indexed AVC scrub warmup retains only verified native callback frames."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace
from typing import Iterator

import pytest

from src.shared.media.macos_vt.packet_demux import CompressedPacket, PacketDemuxer, VideoStreamInfo
from src.shared.media.macos_vt.source import MacOSVtZeroCopySource, VtPixelBufferFrame


class SampleIndex(list[SimpleNamespace]):
    def search_timestamp(self, pts: int, *, backward: bool, any_frame: bool) -> int:
        assert backward and not any_frame
        return max((index for index, entry in enumerate(self)
                    if entry.is_keyframe and entry.timestamp <= pts), default=-1)


class FakeMov:
    format = SimpleNamespace(name='mov,mp4,m4a,3gp,3g2,mj2')

    def __init__(self) -> None:
        self.seek_targets: list[int] = []

    def seek(self, pts: int, *, stream: object, backward: bool, any_frame: bool) -> None:
        assert stream is not None and backward and not any_frame
        self.seek_targets.append(pts)


def make_demuxer() -> PacketDemuxer:
    demuxer = PacketDemuxer()
    demuxer._container = FakeMov()
    demuxer._stream = SimpleNamespace(
        average_rate=Fraction(25), base_rate=Fraction(25), time_base=Fraction(1, 1000),
        start_time=0, codec_context=SimpleNamespace(has_b_frames=False),
        index_entries=SampleIndex(SimpleNamespace(timestamp=frame * 40, pos=100 + frame * 4,
                                                  is_keyframe=frame in (0, 3, 8))
                                  for frame in range(12)),
    )
    demuxer._info = VideoStreamInfo(
        codec_name='h264', codec_tag='avc1', total_frames=12, extradata=b'\x01\x00\x00\x00\x03\x00\x00',
    )
    demuxer._set_h264_scrub_index()
    return demuxer


def sample(frame: int, *, keyframe: bool = False) -> CompressedPacket:
    data = b'\x00\x00\x00\x02' + (b'\x65\x00' if keyframe else b'\x41\x00')
    return CompressedPacket(data, frame * 40, frame * 40, 40, keyframe, frame, 100 + frame * 4)


def test_variable_gop_uses_complete_index_and_stops_before_next_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demuxer = make_demuxer()
    gop = demuxer.previous_h264_scrub_gop(9)
    assert gop is not None
    assert (gop.first_frame, gop.stop_frame) == (3, 8)
    assert demuxer.previous_h264_scrub_gop(1) is None
    other = demuxer.previous_h264_scrub_gop(4)
    assert other is not None and (other.first_frame, other.stop_frame) == (0, 3)
    assert gop.frame_for_pts(5 * 40) == 5
    assert gop.frame_for_pts(5 * 40 - 1) is None
    assert gop.frame_for_pts(8 * 40) is None

    def packets(max_packets: int, *, include_sample_identity: bool) -> Iterator[CompressedPacket]:
        assert max_packets == 4 and include_sample_identity
        yield from (sample(frame, keyframe=frame == 3) for frame in range(3, 7))

    monkeypatch.setattr(demuxer, 'packets', packets)
    assert [p.frame_index for p in demuxer.h264_scrub_gop_packets(gop, 7)] == [3, 4, 5, 6]
    assert demuxer._container.seek_targets == [120]
    assert not list(make_demuxer().h264_scrub_gop_packets(gop, 7))


@pytest.mark.parametrize('change', [
    {'position': 1}, {'dts': 119}, {'pts': None}, {'duration': 39},
    {'has_new_extradata': True}, {'data': b'\x00\x00\x00\x02\x41\x00'},
    {'data': b'\x00\x00\x00\x03\x65\x00'},
])
def test_uncertified_sync_packet_is_never_yielded(monkeypatch: pytest.MonkeyPatch, change: dict) -> None:
    demuxer = make_demuxer()
    gop = demuxer.previous_h264_scrub_gop(9)
    assert gop is not None
    monkeypatch.setattr(demuxer, 'packets', lambda **_: iter([replace(sample(3, keyframe=True), **change)]))
    assert list(demuxer.h264_scrub_gop_packets(gop, 8)) == []


def test_nonzero_origin_reordering_and_incomplete_index_are_ineligible() -> None:
    demuxer = make_demuxer()
    stream = demuxer._stream
    assert demuxer.supports_h264_scrub_gops()
    stream.start_time = 40
    demuxer._set_h264_scrub_index()
    assert not demuxer.supports_h264_scrub_gops()
    stream.start_time = 0
    stream.codec_context.has_b_frames = True
    demuxer._set_h264_scrub_index()
    assert not demuxer.supports_h264_scrub_gops()
    stream.codec_context.has_b_frames = False
    stream.index_entries.pop()
    demuxer._set_h264_scrub_index()
    assert not demuxer.supports_h264_scrub_gops()


@pytest.mark.parametrize('cancel', [False, True])
def test_hardware_warm_drains_callback_pts_and_transfers_independent_leases(
    monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    demuxer = make_demuxer()
    source = MacOSVtZeroCopySource()
    source._demuxer = demuxer
    source._is_open = True
    source._info = SimpleNamespace(width=16, height=8, bit_depth=8)
    source._time_base_den = 1000
    gop = source.previous_h264_scrub_gop(9)
    assert gop is not None

    def packets(max_packets: int, *, include_sample_identity: bool) -> Iterator[CompressedPacket]:
        assert max_packets == 4 and include_sample_identity
        yield from (sample(frame, keyframe=frame == 3) for frame in range(3, 7))

    monkeypatch.setattr(demuxer, 'packets', packets)
    requests = []
    queued = deque()
    release = []
    retain = []
    frames: list[VtPixelBufferFrame] = []

    def submit(data: bytes, pts: int, *, reference_only: bool, async_output: bool) -> None:
        requests.append((pts, reference_only, async_output))
        if not reference_only:
            # These arrive in reverse callback order. One has no native PTS.
            queued.appendleft(SimpleNamespace(pts=pts if pts == 200 else None, pixel_buffer=pts))

    def keep(frame: VtPixelBufferFrame, size: int) -> bool:
        assert size == 192
        frames.append(frame)
        return True

    source._vt_session = SimpleNamespace(
        flush=lambda: None, decode_scrub_packet=submit, drain_scrub_outputs=lambda: None,
        get_decoded_frame=lambda timeout: queued.popleft() if queued else None,
        release_decoded_frame=lambda decoded: release.append(decoded.pixel_buffer),
        retain_decoded_frame=lambda decoded: retain.append(decoded.pixel_buffer) is None,
        decoded_frame_data_size=lambda _: 192,
    )
    options = dict(first_output_frame=5, stop_output_frame=7, is_stale=lambda: cancel and len(requests) >= 3,
                   deadline_at=time.perf_counter() + 1.0, sink=keep)
    assert not source.decode_h264_scrub_gop(gop, **options)
    assert not requests
    source._hardware_required = True
    if cancel:
        assert source.decode_h264_scrub_gop(gop, **options) == 0
        assert requests == [(120, True, False), (160, True, False), (200, False, True)]
        assert not frames and release == [200]
        return
    assert source.decode_h264_scrub_gop(gop, **options) == 1
    assert requests == [(120, True, False), (160, True, False), (200, False, True), (240, False, True)]
    frame, = frames
    assert frame.frame_index == 5 and frame.pts == 200 and frame.time_seconds == 0.2
    assert source.h264_scrub_frame_matches(frame)
    assert release == [240]
    renderer_lease = frame.acquire_lease()
    assert renderer_lease is not None and retain == [200]
    frame.release()
    assert frame.acquire_lease() is None
    source._vt_session = None
    renderer_lease.release()
    renderer_lease.release()
    assert release == [240, 200, 200]
