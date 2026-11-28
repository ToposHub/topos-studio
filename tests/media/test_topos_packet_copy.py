"""TOPOS 422 smart delivery through the production timeline exporter."""
from __future__ import annotations

import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.clip_params import TransformParams
from src.features.edit.core.project import EditProject, probe_media_metadata
from src.features.edit.core.timeline_export import (
    ExportCodec, ExportRangeType, TimelineExportConfig, create_timeline_exporter,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile, candidate_library_paths
from src.shared.export.topos_encoder import ToposVideoEncoder
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational
from src.shared.media.topos_source import ToposMediaSource

pytestmark = pytest.mark.media_core
WIDTH, HEIGHT, FRAMES = 64, 40, 8
FPS = Rational(25, 1)


@pytest.fixture
def source_files(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, Path]:
    if not any(path.is_file() for path in candidate_library_paths()):
        pytest.skip("TOPOS native library is required")
    source_files = (tmp_path / "first.mov", tmp_path / "second.mov")
    tiers = getattr(request, "param", ("standard", "standard"))
    for source_index, path in enumerate(source_files):
        config = FFmpegEncoderConfig(
            codec="topos", profile=tiers[source_index], pix_fmt="yuv422p10le",
            width=WIDTH, height=HEIGHT, fps=FPS,
        )
        encoder = ToposVideoEncoder(str(path), config)
        encoder.open()
        for index in range(FRAMES):
            planes = (
                np.full((HEIGHT, WIDTH), 180 + index * 40 + 300 * source_index, np.uint16),
                np.full((HEIGHT, WIDTH // 2), 512, np.uint16),
                np.full((HEIGHT, WIDTH // 2), 512, np.uint16),
            )
            encoder.encode_frame(DecodedFrame(
                index, index, index / 25, planes=planes, pixel_format=config.pix_fmt,
                width=WIDTH, height=HEIGHT,
            ))
        encoder.close()
    return source_files


def _timeline(source_files: tuple[Path, ...], audio_path: Path | None = None
              ) -> tuple[EditProject, TimelineSequence]:
    project = EditProject.create("TOPOS packet copy")
    tracks = []
    for index, path in enumerate(source_files):
        project = project.import_asset(str(path), "video", metadata=probe_media_metadata(str(path)))
        asset_id = list(project.assets)[-1]
        track_id = f"v{index}"
        clip = ClipInstance(
            clip_id=f"clip{index}", asset_id=asset_id, track_id=track_id,
            timeline_start=index * 4, timeline_duration=4, source_start=3 - index,
            source_duration=4,
        )
        tracks.append(EditTrack(track_id=track_id, track_type="video", index=index, clips=(clip,)))
    audio_tracks = ()
    if audio_path is not None:
        sample_count = 48_000 * len(tracks) * 4 // 25
        pcm = np.arange(sample_count, dtype="<i2") + 3000
        with wave.open(str(audio_path), "wb") as wav:
            wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            wav.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())
        project = project.import_asset(str(audio_path), "audio", metadata=probe_media_metadata(str(audio_path)))
        track = EditTrack(
            track_id="a1", track_type="audio", index=0,
            clips=(ClipInstance(
                clip_id="audio", asset_id=list(project.assets)[-1], track_id="a1",
                timeline_start=0, timeline_duration=len(tracks) * 4, source_start=0,
                source_duration=len(tracks) * 4,
            ),),
        )
        audio_tracks = (track,)
    return project, TimelineSequence(
        name="TOPOS packet copy", fps=FPS, width=WIDTH, height=HEIGHT,
        video_tracks=tuple(tracks), audio_tracks=audio_tracks,
        format_settings={"start_timecode": "01:02:03:04"},
    )


def _config(sequence: TimelineSequence, output: Path) -> TimelineExportConfig:
    return TimelineExportConfig(
        timeline_id=sequence.timeline_id, output_path=str(output), codec=ExportCodec.TOPOS,
        include_audio=False, bypass_reencode=True,
    )


def test_trimmed_multisource_copy_and_audio(source_files, tmp_path, monkeypatch):
    project, sequence = _timeline(source_files, tmp_path / "timeline.wav")
    output = tmp_path / "copied.mov"
    config = replace(
        _config(sequence, output), range_type=ExportRangeType.CUSTOM,
        start_frame=1, end_frame=7, include_audio=True, audio_codec="pcm_s16le",
    )
    baseline = tmp_path / "encoded.mov"
    encoded = create_timeline_exporter(project, sequence).export_timeline(
        replace(config, output_path=str(baseline), bypass_reencode=False))
    assert encoded.success, encoded

    def unexpected_encode(*_args, **_kwargs):
        pytest.fail("the eligible compressed packets must not enter the video encoder")

    monkeypatch.setattr(ToposVideoEncoder, "encode_frame", unexpected_encode)
    result = create_timeline_exporter(project, sequence).export_timeline(config)
    assert result.success and result.delivery_path == "topos_packet_copy", result
    codec = ToposCodec()
    with (ToposMovieFile(codec, str(source_files[0])) as first,
          ToposMovieFile(codec, str(source_files[1])) as second,
          ToposMovieFile(codec, str(output)) as actual,
          ToposMovieFile(codec, str(baseline)) as reference):
        expected = [first.packet(index) for index in (4, 5, 6)]
        expected.extend(second.packet(index) for index in (2, 3, 4))
        assert actual.info().sample_count == len(expected)
        assert [actual.packet(index) for index in range(len(expected))] == expected
        assert [actual.pts(index) for index in range(len(expected))] == [(index, 1) for index in range(6)]
        assert actual.timecode().format() == "01:02:03:04"
        assert actual.audio_info().sample_count == 6 * 48000 // 25
        assert actual.read_audio_samples() == reference.read_audio_samples()


@pytest.mark.parametrize("change", ["option_off", "different_tier", "transform"])
def test_ineligible_clip_uses_normal_export(source_files, tmp_path, change):
    project, sequence = _timeline(source_files[:1])
    config = _config(sequence, tmp_path / "fallback.mov")
    if change == "option_off":
        config = replace(config, bypass_reencode=False)
    elif change == "different_tier":
        config = replace(config, codec=ExportCodec.TOPOS_HQ)
    else:
        track = sequence.video_tracks[0]
        clip = replace(track.clips[0], transform=TransformParams(scale_x=1.25))
        sequence = replace(sequence, video_tracks=(replace(track, clips=(clip,)),))
    result = create_timeline_exporter(project, sequence).export_timeline(config)
    assert result.success and result.delivery_path != "topos_packet_copy", result


@pytest.mark.parametrize("bypass_reencode, cancel_frame", [(True, 2), (False, 4)])
def test_cancel_preserves_existing_output(source_files, tmp_path, bypass_reencode, cancel_frame):
    project, sequence = _timeline(source_files[:1])
    output = tmp_path / "exists.mov"
    output.write_bytes(b"previous delivery")
    exporter = create_timeline_exporter(project, sequence)
    exporter._progress_callback = (
        lambda current, _total, _percent: exporter.cancel_export() if current == cancel_frame else None
    )
    result = exporter.export_timeline(replace(
        _config(sequence, output), bypass_reencode=bypass_reencode))
    assert result.cancelled
    assert output.read_bytes() == b"previous delivery"
    assert not list(tmp_path.glob("*.topos-tmp"))


@pytest.mark.parametrize("source_files", [("standard", "hq")], indirect=True)
def test_multi_source_checks_later_tier_before_writing(
    source_files: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, sequence = _timeline(source_files)

    def unexpected_packet(*_args: object, **_kwargs: object) -> None:
        pytest.fail("all source tiers must match before the first packet is written")

    monkeypatch.setattr(ToposVideoEncoder, "write_source_packet", unexpected_packet)
    result = create_timeline_exporter(project, sequence).export_timeline(
        _config(sequence, tmp_path / "fallback.mov"))
    assert result.success and result.delivery_path != "topos_packet_copy", result
    assert not list(tmp_path.glob("*.topos-tmp"))


def test_single_clip_reencode_obeys_inner_range(
    source_files: tuple[Path, Path], tmp_path: Path,
) -> None:
    project, sequence = _timeline(source_files[:1])
    output = tmp_path / "trimmed.mov"
    config = replace(
        _config(sequence, output), bypass_reencode=False,
        range_type=ExportRangeType.CUSTOM, start_frame=1, end_frame=3,
    )
    result = create_timeline_exporter(project, sequence).export_timeline(config)
    assert result.success and result.delivery_path == "topos_direct_planar_cpu_encode", result
    codec = ToposCodec()
    with (ToposMovieFile(codec, str(source_files[0])) as source,
          ToposMovieFile(codec, str(output)) as actual):
        assert actual.info().sample_count == 2
        for index in range(2):
            expected_luma = codec.decode(source.packet(index + 4)).planes[0]
            result_luma = codec.decode(actual.packet(index)).planes[0]
            expected_value = np.frombuffer(expected_luma, dtype="<u2").mean()
            actual_value = np.frombuffer(result_luma, dtype="<u2").mean()
            assert abs(actual_value - expected_value) < 8


@pytest.mark.parametrize("finish", ["close", "abort"])
def test_copy_encoder_can_reopen_for_pixel_encoding(source_files, tmp_path, finish):
    path = str(source_files[0])
    export_config = FFmpegEncoderConfig(
        codec="topos", profile="standard", pix_fmt="yuv422p10le",
        width=WIDTH, height=HEIGHT, fps=FPS,
    )
    encoder = ToposVideoEncoder(str(tmp_path / "reopened.mov"), export_config)
    with ToposMovieFile(ToposCodec(), path) as movie:
        encoder.open(packet_copy_source=movie.info())
        encoder.write_source_packet(*movie.packet_into(0))
        getattr(encoder, finish)()
    source = ToposMediaSource(path)
    source.open()
    try:
        encoder.open()
        encoder.encode_frame(source.read_frame(1))
        encoder.close()
    finally:
        source.close()
    assert encoder.frame_count == 1
    assert not list(tmp_path.glob("*.topos-tmp"))


def test_lt_packet_copy_retains_legacy_matrix(tmp_path: Path) -> None:
    if not any(path.is_file() for path in candidate_library_paths()):
        pytest.skip("TOPOS native library is required")

    class LegacyLtEncoder(ToposVideoEncoder):
        def _effective_qmatrix_id(self) -> int:
            return 4

    width, height = 1920, 1080
    config = FFmpegEncoderConfig(codec="topos", profile="lt", pix_fmt="yuv422p10le",
                                  width=width, height=height, fps=FPS)
    planes = tuple(np.full((height, plane_width), 512, np.uint16)
                   for plane_width in (width, width // 2, width // 2))
    source_path = tmp_path / "legacy-lt.mov"
    source = LegacyLtEncoder(str(source_path), config)
    source.open()
    source.encode_frame(DecodedFrame(0, 0, 0.0, planes=planes, pixel_format=config.pix_fmt,
                                     width=width, height=height))
    source.close()

    output = tmp_path / "copied-lt.mov"
    encoder = ToposVideoEncoder(str(output), config)
    codec = ToposCodec()
    with ToposMovieFile(codec, str(source_path)) as movie:
        encoder.open(packet_copy_source=movie.info())
        encoder.add_audio_stream(codec="pcm_s16le")
        encoder.write_source_packet(*movie.packet_into(0))
        encoder.write_audio_samples(np.zeros((2, 48_000 // 25), dtype=np.float32))
        encoder.close()
        with ToposMovieFile(codec, str(output)) as copied:
            assert copied.info().qmatrix_id == 4
            assert copied.packet(0) == movie.packet(0)
            assert copied.audio_info().sample_count == 48_000 // 25
    encoder.open()
    try:
        assert encoder._movie_cfg.qmatrix_id == 0
    finally:
        encoder.abort()
