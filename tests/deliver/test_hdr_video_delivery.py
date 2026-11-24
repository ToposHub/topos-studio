"""HDR media through the ordinary timeline delivery entry point."""

from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import av
import cv2
import numpy as np
import pytest

from src.features.deliver.core.video_export_job import VideoConfig, VideoExportJob
from src.features.deliver.core.video_export_preflight import (
    PreflightContext, PreflightResult, _check_color_delivery_preflight,
)
from src.features.edit.core.project import EditProject, MediaAsset, probe_media_metadata
from src.features.edit.core.timeline_export import ExportCodec, TimelineExportConfig, create_timeline_exporter
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.export.hdr_hevc_encoder import HdrHevcEncoder
from src.shared.export.video_encoder import ColorMetadataConfig, FFmpegEncoderConfig
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


@pytest.mark.parametrize(("output", "codec", "extension", "transfer", "codec_tag"), [
    ("rec2100-pq", ExportCodec.HEVC, "mp4", 16, "hvc1"),
    ("rec2100-hlg", ExportCodec.HEVC, "mov", 18, "hvc1"),
    ("rec2100-pq", ExportCodec.PRORES_422HQ, "mov", 16, "apch"),
    ("rec2100-hlg", ExportCodec.PRORES_422, "mov", 18, "apcn"),
])
def test_hdr_timeline_export_and_readback(
    tmp_path: Path, output: str, codec: ExportCodec, extension: str, transfer: int, codec_tag: str,
) -> None:
    input_path = tmp_path / "hdr-plate.tif"
    # Mid-gray in the already encoded input. The three-band color samples
    # exercise matrix ordering and horizontal 4:2:2 subsampling as well.
    bgr = np.full((64, 128, 3), 32768, dtype=np.uint16)
    bgr[32:, :64] = [12000, 18000, 44000]
    bgr[32:, 64:] = [44000, 18000, 12000]
    assert cv2.imwrite(str(input_path), bgr)
    asset = MediaAsset("plate", str(input_path), "image", "HDR plate", {})
    project = EditProject("hdr", "HDR", assets={asset.asset_id: asset}).with_color_management({
        "color.timeline_space": "davinci-wide-gamut-intermediate", "color.output_space": output,
        "color.hdr.input": output, "color.hdr.master_gamut": "p3-d65", "color.hdr.master_peak": "2000",
    })
    clip = ClipInstance("clip", asset.asset_id, "track", 0, 3, 0, 3)
    timeline = TimelineSequence(
        timeline_id="sequence", name="HDR", width=128, height=64, fps=Rational(24, 1),
        video_tracks=(EditTrack("track", "V1", "video", 0, clips=(clip,)),),
    )
    path = tmp_path / f"hdr.{extension}"
    config = TimelineExportConfig(
        output_path=str(path), codec=codec, timeline_id=timeline.timeline_id,
        include_audio=False, burn_subtitles=False, quality=12,
    )
    outcome = create_timeline_exporter(project, timeline).export_timeline(config)
    assert outcome.success, outcome.cause
    with av.open(str(path)) as reader:
        stream = reader.streams.video[0]
        context = stream.codec_context
        assert (context.codec_tag, context.color_primaries, context.color_trc, context.colorspace) == (
            codec_tag, 9, transfer, 9,
        )
        frames = list(reader.decode(video=0))
        assert len(frames) == 3
        assert frames[0].format.name == ("yuv420p10le" if codec is ExportCodec.HEVC else "yuv422p10le")
        luma = frames[0].planes[0]
        values = np.frombuffer(luma, dtype="<u2").reshape(luma.height, luma.line_size // 2)
        assert np.median(values[10:20, 10:20]) == pytest.approx(502.0, abs=1)
        assert np.median(values[42:52, 10:20]) > np.median(values[42:52, 90:100])
        side_data = {entry.type.name for entry in frames[0].side_data}
        assert ("MASTERING_DISPLAY_METADATA" in side_data) == (
            output == "rec2100-pq" and codec is ExportCodec.HEVC
        )
    if output == "rec2100-pq" and codec is ExportCodec.HEVC:
        reimport = replace(asset, path=str(path), media_type="video", metadata=probe_media_metadata(str(path)))
        assert reimport.metadata["color_transfer"] == "smpte2084"
        project = replace(project, assets={asset.asset_id: reimport}).with_color_management({
            **project.color_management, "color.hdr.input": "auto",
        })
        roundtrip_path = tmp_path / "roundtrip.mov"
        repeat = replace(config, output_path=str(roundtrip_path), codec=ExportCodec.PRORES_422HQ)
        outcome = create_timeline_exporter(project, timeline).export_timeline(repeat)
        assert outcome.success, outcome.cause
        with av.open(str(roundtrip_path)) as reader:
            plane = next(reader.decode(video=0)).planes[0]
            roundtrip = np.frombuffer(plane, dtype="<u2").reshape(plane.height, plane.line_size // 2)
            np.testing.assert_allclose(roundtrip[10:20, 10:20], values[10:20, 10:20], atol=1)


def test_hdr_hevc_can_cancel_without_replacing_the_previous_file(tmp_path: Path) -> None:
    output = tmp_path / "master.mp4"
    output.write_bytes(b"previous master")
    config = FFmpegEncoderConfig(
        codec="hevc", pix_fmt="yuv420p10le", width=64, height=32, fps=Rational(24, 1),
        color_metadata=ColorMetadataConfig.hlg(),
    )
    encoder = HdrHevcEncoder(str(output), config)
    encoder.open()
    frame = DecodedFrame(
        frame_index=0, pts=0, time_seconds=0, data=np.full((32, 64, 3), 32768, np.uint16),
        pixel_format="bgr48le", width=64, height=32,
    )
    try:
        encoder.encode_frame(frame)
        assert not encoder.close(should_cancel=lambda: True, expected_frames=1)
    finally:
        encoder.abort()
    assert output.read_bytes() == b"previous master"
    assert not list(tmp_path.glob(".topos-hdr-*"))

    encoder = HdrHevcEncoder(str(output), config, total_frames=1)
    encoder.open()
    try:
        encoder.encode_frame(frame)
        assert encoder.close(expected_frames=1)
    finally:
        encoder.abort()
    with av.open(str(output)) as reader:
        assert len(list(reader.decode(video=0))) == 1


def test_hdr_delivery_preflight_blocks_eight_bit() -> None:
    job = VideoExportJob(video_config=VideoConfig(codec="hevc", pixel_format="yuv420p"))
    project = SimpleNamespace(color_management={"color.timeline_space": "rec2100-pq",
                                                "color.output_space": "rec2100-pq"})
    result = PreflightResult()
    _check_color_delivery_preflight(job, PreflightContext(project=project), result)
    assert [issue.code for issue in result.blocking] == ["color.delivery.hdr_codec"]
