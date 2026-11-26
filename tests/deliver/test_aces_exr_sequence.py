"""ACES OpenEXR sequence registration and actual frame readback."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from src.features.deliver.core.delivery_capability import describe_delivery_capability
from src.features.deliver.core.video_export_formats import is_codec_container_compatible
from src.features.deliver.core.video_export_job import VideoConfig, VideoExportJob
from src.features.deliver.core.video_export_preflight import (
    PreflightContext, PreflightResult, _check_color_delivery_preflight,
)
from src.shared.color.project_video_signal import ACES_EXR_CHROMATICITIES
from src.shared.export.image_sequence_encoder import StandardImageSequenceEncoder


@pytest.mark.parametrize("compression", ["zip1", "zip16"])
def test_aces_zip_still_input_retains_signed_rgb_and_alpha(tmp_path: Path, compression: str) -> None:
    import av

    from src.features.edit.core.project import probe_media_metadata
    from src.shared.color.color_space_descriptor import descriptor_for_space_preset
    from src.shared.color.project_video_signal import ProjectAcesSettings, read_aces_exr

    path = str(tmp_path / "plate.exr")
    with av.open(path, "w", format="image2") as container:
        stream = container.add_stream("exr", rate=24)
        stream.width, stream.height = 32, 8
        stream.pix_fmt = "gbrapf32le"
        stream.options = {"compression": compression}
        frame = av.VideoFrame(stream.width, stream.height, format=stream.pix_fmt)
        for plane, value in zip(frame.planes, (0.25, 3.0, -0.125, 0.5), strict=True):
            plane.update(np.full((plane.height, plane.line_size // 4), value, dtype="<f4").tobytes())
        container.mux(stream.encode(frame))
        container.mux(stream.encode())

    settings = ProjectAcesSettings("1.3", "rec709-g24", "auto", False)
    actual, descriptor = read_aces_exr(path, settings)
    assert descriptor == descriptor_for_space_preset("aces2065-1")
    np.testing.assert_array_equal(actual, np.broadcast_to([6.0, 0.5, -0.25, 0.5], (8, 32, 4)))
    metadata = probe_media_metadata(path)
    assert (metadata["width"], metadata["height"], metadata["has_alpha"]) == (32, 8, True)


def test_timeline_aces_exr_sequence_reimport_and_delivery(tmp_path: Path) -> None:
    import OpenEXR

    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.sequence_classify import StillSequenceGroup
    from src.features.edit.core.timeline_export import (
        ExportCodec, TimelineExportConfig, create_timeline_exporter,
    )
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_paths = tuple(str(source_dir / f"plate_{index}.exr") for index in (1001, 1002, 1003))
    expected = np.array([[-0.03, 0.18, 4.0], [2.0, -0.05, 0.4], [0.21, 1.8, -0.07]], dtype=np.float32)
    for path, pixel in zip(source_paths, expected, strict=True):
        OpenEXR.File({
            "type": OpenEXR.scanlineimage, "compression": OpenEXR.PIZ_COMPRESSION,
            "chromaticities": ACES_EXR_CHROMATICITIES["acescg"],
        }, {"RGB": np.broadcast_to(pixel, (16, 16, 3)).copy()}).write(path)
    group = StillSequenceGroup(source_paths[0], source_paths, "plate_", ".exr", (1001, 1002, 1003))
    asset = MediaAsset("asset", source_paths[0], "image_sequence", "plate", group.asset_metadata(24))
    project = EditProject("project", "ACES exchange", assets={"asset": asset}).with_color_management({
        "color.timeline_space": "acescct", "color.output_space": "acescg", "color.aces.version": "2.0",
    })
    clip = ClipInstance("clip", "asset", "track", 0, 3, 0, 3)
    timeline = TimelineSequence(
        timeline_id="sequence", name="ACES", width=16, height=16, fps=Rational(24, 1),
        video_tracks=(EditTrack("track", "V1", "video", 0, clips=(clip,)),),
    )
    config = TimelineExportConfig(
        output_path=str(tmp_path / "roundtrip.exr"), codec=ExportCodec.EXR_SEQUENCE,
        bit_depth=32, timeline_id=timeline.timeline_id, include_audio=False, burn_subtitles=False,
        image_sequence_digits=4, image_sequence_start_index=1001,
    )
    outcome = create_timeline_exporter(project, timeline).export_timeline(config)
    assert outcome.success, outcome.cause
    assert [path.name for path in sorted(tmp_path.glob("*.exr"))] == [
        "roundtrip_1001.exr", "roundtrip_1002.exr", "roundtrip_1003.exr",
    ]
    for path, pixel in zip(sorted(tmp_path.glob("*.exr")), expected, strict=True):
        with OpenEXR.File(str(path)) as file:
            actual = file.channels()["RGB"].pixels
            np.testing.assert_allclose(actual, np.broadcast_to(pixel, actual.shape), atol=6e-6)


def test_aces_exr_delivery_uses_each_asset_input_even_when_files_are_shared(tmp_path: Path) -> None:
    import cv2
    import OpenEXR
    import PyOpenColorIO as ocio

    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_export import ExportCodec, TimelineExportConfig, create_timeline_exporter
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational

    source = str(tmp_path / "log.png")
    code = np.array([25290, 32768, 39211], dtype=np.uint16)
    assert cv2.imwrite(source, np.broadcast_to(code[::-1], (16, 16, 3)).copy())
    selections = (("arri-logc4", "ARRI LogC4"), ("sony-slog3-sgamut3cine", "S-Log3 S-Gamut3.Cine"))
    assets = {str(i): MediaAsset(str(i), source, "image", "Camera", {}).with_input_color_space(choice)
              for i, (choice, _) in enumerate(selections)}
    project = EditProject("project", "Camera conversions", assets=assets).with_color_management({
        "color.timeline_space": "acescct", "color.output_space": "acescg", "color.aces.version": "2.0",
    })
    clips = tuple(ClipInstance(f"clip{i}", str(i), "track", i, 1, 0, 1) for i in range(2))
    timeline = TimelineSequence(
        timeline_id="sequence", name="ACES", width=16, height=16, fps=Rational(24, 1),
        video_tracks=(EditTrack("track", "V1", "video", 0, clips=clips),),
    )
    config = TimelineExportConfig(
        output_path=str(tmp_path / "camera.exr"), codec=ExportCodec.EXR_SEQUENCE,
        bit_depth=32, timeline_id=timeline.timeline_id, include_audio=False, burn_subtitles=False,
    )
    result = create_timeline_exporter(project, timeline).export_timeline(config)
    assert result.success, result.cause
    ocio_config = ocio.Config.CreateFromBuiltinConfig("studio-config-v4.0.0_aces-v2.0_ocio-v2.5")
    for path, (_, ocio_space) in zip(sorted(tmp_path.glob("*.exr")), selections, strict=True):
        processor = ocio_config.getProcessor(ocio_space, "ACEScg").getDefaultCPUProcessor()
        expected = code.astype(np.float32) / 65535.0
        processor.applyRGB(expected)
        with OpenEXR.File(str(path)) as file:
            actual = file.channels()["RGB"].pixels
            np.testing.assert_allclose(actual, np.broadcast_to(expected, actual.shape), rtol=5e-5, atol=4e-6)


@pytest.mark.parametrize(("space", "depth", "dtype"), [
    ("aces2065-1", 16, np.float16), ("acescg", 32, np.float32),
])
def test_exr_reads_linear_color_alpha_and_chromaticities(
    tmp_path: Path, space: str, depth: int, dtype: type,
) -> None:
    import OpenEXR

    config = SimpleNamespace(codec="exr_sequence", bit_depth=depth, has_alpha=True)
    encoder = StandardImageSequenceEncoder(str(tmp_path / "master.exr"), config, exr_space=space)
    bgr = np.array([[[-0.25, 0.125, 4.0, 0.5], [1.75, -0.5, 0.75, 1.0]]], dtype=np.float32)
    encoder.encode_frame(SimpleNamespace(data=bgr))
    assert not list(tmp_path.glob("*.exr"))
    encoder.close()
    info = encoder.sequence_info()
    assert info["codec"] == "exr"
    assert info["bit_depth"] == depth
    with OpenEXR.File(info["frame_files"][0], separate_channels=True) as file:
        np.testing.assert_allclose(file.header()["chromaticities"], ACES_EXR_CHROMATICITIES[space], atol=1e-7)
        assert file.header()["compression"] == OpenEXR.PIZ_COMPRESSION
        actual = file.channels()
        assert set(actual) == set("RGBA")
        expected = {"R": [2.0, 0.75], "G": [0.0625, -0.5], "B": [-0.125, 1.75], "A": [0.5, 1.0]}
        for name, values in expected.items():
            assert actual[name].pixels.dtype == dtype
            np.testing.assert_array_equal(actual[name].pixels, [values])


def test_half_range_overflow_fails_before_publication(tmp_path: Path) -> None:
    config = SimpleNamespace(codec="exr_sequence", bit_depth=16, exr_space="acescg")
    encoder = StandardImageSequenceEncoder(str(tmp_path / "master.exr"), config)
    with pytest.raises(ValueError, match="32-bit float"):
        encoder.encode_frame(SimpleNamespace(data=np.full((1, 1, 3), 70000.0, dtype=np.float32)))
    encoder.abort()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(("space", "codec", "depth", "allowed"), [
    ("acescg", "exr_sequence", 16, True),
    ("aces2065-1", "exr_sequence", 32, True),
    ("aces2065-1", "exr_sequence", 8, False),
    ("acescg", "tiff_sequence", 16, False),
    ("rec709-g24", "exr_sequence", 32, False),
    ("rec709-g24", "tiff_sequence", 16, True),
])
def test_linear_aces_uses_only_exr_delivery(space: str, codec: str, depth: int, allowed: bool) -> None:
    project = SimpleNamespace(color_management={"color.output_space": space})
    config = VideoConfig(container="", codec=codec, bit_depth=depth)
    job = VideoExportJob(video_config=config)
    result = PreflightResult()
    _check_color_delivery_preflight(job, PreflightContext(project=project), result)
    assert result.ok == allowed
    assert is_codec_container_compatible(codec, "")
    assert not is_codec_container_compatible(codec, "mov")


def test_linear_aces_deliver_controls_select_exr_precision(qtbot: QtBot) -> None:
    from src.features.deliver.ui.video_export_settings_panel import VideoExportSettingsPanel

    panel = VideoExportSettingsPanel()
    qtbot.addWidget(panel)
    project = SimpleNamespace(color_management={"color.output_space": "aces2065-1"})
    panel.set_project(project)
    assert panel._codec_combo.count() == 1
    assert panel._codec_combo.currentData() == "exr_sequence"
    assert [panel._image_bit_depth_combo.itemData(i) for i in range(2)] == [16, 32]
    panel._image_bit_depth_combo.setCurrentIndex(1)
    assert panel._read_video_config_from_draft().bit_depth == 32
    job = VideoExportJob(video_config=panel._read_video_config_from_draft())
    config = job.to_timeline_export_config()
    assert config.bit_depth == 32
    assert config.pixel_format == "gbrpf32le"
    capability = describe_delivery_capability(config)
    assert capability.container == "sequence"
    assert capability.hdr and not capability.audio_capable
    panel.load_job(job)
    assert panel._read_video_config_from_draft().bit_depth == 32
    panel.set_project(SimpleNamespace(color_management={"color.output_space": "rec709-g24"}))
    assert panel._codec_combo.currentData() != "exr_sequence"
