"""Project video profiles define the RGB before delivery range packing."""

import numpy as np
import pytest

from src.shared.color.color_space_descriptor import (
    LINEAR_SCENE, descriptor_for_space_preset,
)
from src.shared.color.color_transform_provider import BuiltinColorProvider, OCIOColorProvider
from src.shared.color.project_video_signal import (
    convert_video_signal_bgr, video_signal_spaces,
)
from src.shared.color.types import ColorPrimaries, ColorRange, ColorTransfer


@pytest.mark.parametrize(("preset", "ocio_space"), [
    ("arri-logc3-ei800", "ARRI LogC3 (EI800)"),
    ("arri-logc4", "ARRI LogC4"),
    ("sony-slog3-sgamut3", "S-Log3 S-Gamut3"),
    ("sony-slog3-sgamut3cine", "S-Log3 S-Gamut3.Cine"),
])
def test_camera_aces_input_matches_reference_csc(preset: str, ocio_space: str) -> None:
    ocio = pytest.importorskip("PyOpenColorIO")
    from src.shared.color.camera_input import CAMERA_LOG_CURVES
    from src.shared.color.project_video_signal import ProjectAcesSettings, aces_input_descriptor
    from src.shared.color.video_signal_gl import can_render_video_signal_gl

    aces = ProjectAcesSettings("2.0", "rec709-g24", "rec709-g24", False)
    source = aces_input_descriptor(aces, input_override=preset)
    destination = descriptor_for_space_preset("acescg")
    assert can_render_video_signal_gl(source, destination)
    curve = CAMERA_LOG_CURVES[source.transfer]
    rgb = np.array([[-0.01, 0.03, curve.encoded_break + 0.01],
                    [0.25, 0.39, 0.51], [0.83, 0.51, 0.97]], dtype=np.float32)
    config = ocio.Config.CreateFromBuiltinConfig("studio-config-v4.0.0_aces-v2.0_ocio-v2.5")
    processor = config.getProcessor(ocio_space, "ACEScg").getDefaultCPUProcessor()
    expected = rgb.copy()
    processor.applyRGB(expected)
    actual = BuiltinColorProvider().transform_signal(rgb, source, destination)
    np.testing.assert_allclose(actual, expected, rtol=5e-5, atol=3e-6)
    explicit_auto = aces_input_descriptor(aces, media_type="image", input_override="auto")
    assert explicit_auto == descriptor_for_space_preset("srgb")


def test_open_old_video_keeps_legacy_preview_and_export() -> None:
    from src.shared.color.color_context_lifecycle import on_project_loaded
    from src.shared.color.color_space_descriptor import (
        clear_project_color_overrides, descriptor_from_config,
    )

    try:
        assert video_signal_spaces({}) is None
        on_project_loaded({}, video=True)
        config = {"color.timeline_space": "rec709-g24"}
        preview = descriptor_from_config(config, "preview", "timeline", True)
        assert preview.transfer == ColorTransfer.BT709
    finally:
        clear_project_color_overrides()


def test_project_color_setting_can_be_undone_and_redone() -> None:
    from src.features.edit.core.commands import ProjectCommandStack
    from src.features.edit.core.commands.project_color_spaces import SetProjectColorSpacesCommand
    from src.features.edit.core.project import EditProject

    settings = {"color.timeline_space": "rec709-g24", "future_setting": {"version": 2}}
    project = EditProject.create("Color settings").with_color_management(settings)
    stack = ProjectCommandStack()
    command = SetProjectColorSpacesCommand("Color spaces", (
        ("color.timeline_space", "rec709-bt709"), ("color.output_space", "srgb")))
    edited = stack.execute(command, project)
    undone = stack.undo(edited)
    redone = stack.redo(undone)
    assert edited.color_management["color.output_space"] == "srgb"
    assert undone.color_management == settings
    assert redone.color_management == edited.color_management
    assert redone.revision == project.revision + 3


def test_composite_sdr_input_uses_timeline_transfer_and_keeps_decoder_tags() -> None:
    from src.features.edit.core.timeline_clip_frame_provider import (
        _TexResult, TimelineClipFrameProvider,
    )
    from src.features.edit.core.project import EditProject

    provider = TimelineClipFrameProvider()
    provider.set_project(EditProject.create("Color settings").with_color_management({
        "color.timeline_space": "rec709-g24", "color.output_space": "rec709-g24",
    }))
    meta = {"color_transfer": "bt709", "color_primaries": "bt709",
            "color_matrix": "smpte170m", "color_range": "limited"}
    source = _TexResult(texture_id=17, color_space="bt709", frame_metadata=meta)
    result = provider._interpret_timeline_sdr(source)
    assert result.color_space == "gamma24"
    assert result.frame_metadata == {**meta, "color_transfer": "gamma24"}
    assert source.color_space == "bt709"
    hdr = _TexResult(texture_id=18, color_space="pq")
    assert provider._interpret_timeline_sdr(hdr) is hdr


def test_old_project_without_explicit_selection_stays_on_legacy_path() -> None:
    assert video_signal_spaces({"color.preview.timeline_transfer": "bt709"}) is None
    assert video_signal_spaces({
        "color.timeline_space": "legacy", "color.output_space": "legacy",
    }) is None


def test_partial_upgrade_uses_persisted_legacy_space_without_rgb_range_packing() -> None:
    source, destination = video_signal_spaces({
        "color.timeline_space": "legacy",
        "color.preview.timeline_primaries": "smpte432",
        "color.preview.timeline_transfer": "iec61966_2_1",
        "color.preview.timeline_range": "limited",
        "color.output_space": "rec709-g24",
    })
    assert source.primaries == ColorPrimaries.P3_DISPLAY
    assert source.transfer == ColorTransfer.SRGB
    assert destination.transfer == ColorTransfer.GAMMA24
    assert source.range == destination.range == ColorRange.FULL


def test_video_transform_preserves_precision_and_alpha() -> None:
    source = descriptor_for_space_preset("rec709-g24")
    output = descriptor_for_space_preset("srgb")
    bgr = np.array([[[0, 8192, 32768, 21], [65535, 200, 400, 61500]]], dtype=np.uint16)
    assert convert_video_signal_bgr(bgr, source, source) is bgr
    actual = convert_video_signal_bgr(bgr, source, output)
    linear = (bgr[..., :3] / 65535.0) ** 2.4
    expected = np.where(linear <= 0.0031308, linear * 12.92,
                        1.055 * linear ** (1.0 / 2.4) - 0.055)
    np.testing.assert_array_equal(actual[..., :3], np.rint(expected * 65535))
    np.testing.assert_array_equal(actual[..., 3], bgr[..., 3])
    assert actual.dtype == bgr.dtype


def test_gamma24_is_distinct_from_the_bt709_oetf_in_both_providers() -> None:
    light = np.array([[0.002, 0.18, 0.5]], dtype=np.float32)
    g24 = descriptor_for_space_preset("rec709-g24")
    bt709 = descriptor_for_space_preset("rec709-bt709")
    cpu = BuiltinColorProvider()
    expected = light ** (1.0 / 2.4)
    np.testing.assert_allclose(cpu.transform(light, LINEAR_SCENE, g24), expected, atol=1e-7)
    assert not np.allclose(cpu.transform(light, LINEAR_SCENE, bt709), expected)
    ocio = OCIOColorProvider()
    if not ocio.is_available():
        pytest.skip("OpenColorIO unavailable")
    assert ocio.probe_transform(LINEAR_SCENE, g24) is None
    np.testing.assert_allclose(ocio.transform(light, LINEAR_SCENE, g24), expected, atol=5e-6)
