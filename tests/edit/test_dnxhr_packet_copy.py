from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import pytest

from src.features.edit.core import dnxhr_packet_copy
from src.features.edit.core.dnxhr_packet_copy import (
    DnxhrPacketCopyUnsupported,
    copy_dnxhr_hqx_mov,
)

if TYPE_CHECKING:
    import av


@pytest.fixture
def hqx_source() -> Path:
    pytest.importorskip("av")
    source = Path(__file__).resolve().parents[1] / "test_videos" / "4K_DNxHR HQX 10bit.mov"
    if not source.is_file():
        pytest.skip("The 4K DNxHR HQX master is unavailable")
    return source


def test_copies_two_real_edits_with_exact_packets_and_new_timestamps(
    hqx_source: Path, tmp_path: Path,
) -> None:
    import av

    target = tmp_path / "cut.mov"
    completed: list[int] = []
    assert copy_dnxhr_hqx_mov(
        str(hqx_source), str(target), [(25, 1), (275, 1)],
        width=3840, height=2160, fps=Fraction(25),
        continue_export=lambda: True, progress=completed.append,
    )
    assert completed == [1, 2]

    with av.open(str(hqx_source)) as source, av.open(str(target)) as output:
        in_video = source.streams.video[0]
        out_video = output.streams.video[0]
        assert len(output.streams) == 1
        assert out_video.frames == 2
        assert out_video.codec_context.codec_tag == "AVdh"
        assert out_video.codec_context.extradata == in_video.codec_context.extradata
        assert out_video.codec_context.format.name == "yuv422p10le"
        assert out_video.codec_context.color_range == in_video.codec_context.color_range
        assert out_video.codec_context.color_trc == in_video.codec_context.color_trc
        assert "timecode" not in out_video.metadata
        packets = [packet for packet in output.demux(out_video) if packet.size]
        for output_frame, source_frame, packet in zip(range(2), (25, 275), packets, strict=True):
            source.seek(source_frame * 512, stream=in_video)
            source_packet = next(source.demux(in_video))
            assert memoryview(source_packet) == memoryview(packet)
            assert packet.pts == packet.dts == output_frame * 512
            assert packet.duration == 512


@pytest.mark.parametrize("failure", ["format", "timestamp", "cancel_before_publish", "replace"])
def test_preserves_existing_destination_on_failure(
    failure: str, hqx_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cut.mov"
    target.write_bytes(b"previous successful export")
    completed: list[int] = []
    calls = 0

    def continue_export() -> bool:
        nonlocal calls
        calls += 1
        return failure != "cancel_before_publish" or calls < 4

    def fail_publish(_temporary: Path, _target: Path) -> None:
        raise PermissionError("test publish failure")

    if failure == "replace":
        monkeypatch.setattr(dnxhr_packet_copy.os, "replace", fail_publish)
    elif failure == "timestamp":
        original_packets = dnxhr_packet_copy._source_packets

        def packets(container: av.container.InputContainer, stream: av.VideoStream,
                    start_frame: int, frame_ticks: int) -> Iterator[av.Packet]:
            for packet in original_packets(container, stream, start_frame, frame_ticks):
                if packet.pts == 275 * 512:
                    packet.dts = packet.pts - 512
                yield packet

        monkeypatch.setattr(dnxhr_packet_copy, "_source_packets", packets)

    def run() -> bool:
        return copy_dnxhr_hqx_mov(
            str(hqx_source), str(target), [(25, 1), (275, 1)],
            width=1920 if failure == "format" else 3840,
            height=2160, fps=Fraction(25), continue_export=continue_export,
            progress=completed.append,
        )

    if failure == "cancel_before_publish":
        assert run() is False
    else:
        with pytest.raises(PermissionError if failure == "replace" else DnxhrPacketCopyUnsupported):
            run()

    assert target.read_bytes() == b"previous successful export"
    assert list(tmp_path.iterdir()) == [target]
    assert completed == ([] if failure == "format" else [1])
