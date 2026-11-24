"""Reference vectors for project RGB math, before format quantization."""

import numpy as np
import pytest

from src.shared.color.color_space_descriptor import (
    COLOR_SPACE_PRESETS, LINEAR_SCENE, ColorSpaceDescriptor, descriptor_for_space_preset,
)
from src.shared.color.color_transform_provider import (
    BuiltinColorProvider, OCIOColorProvider, VideoSignalLuminance, video_signal_gamut_matrix,
    apply_aces_output_transform, resolve_aces_output_view,
)
from src.shared.color.project_video_signal import (
    convert_video_signal_bgr, video_signal_spaces, video_signal_uses_legacy_space,
)
from src.shared.color.types import ColorPrimaries, ColorRange, ColorTransfer, delivery_transfer_tag


@pytest.mark.parametrize("preset", COLOR_SPACE_PRESETS)
def test_video_presets_round_trip_the_rgb_descriptor(preset: str) -> None:
    descriptor = descriptor_for_space_preset(preset)
    assert descriptor.range == ColorRange.FULL
    assert descriptor == ColorSpaceDescriptor.from_dict(descriptor.to_dict())


def test_p3_white_points_and_negative_float_values() -> None:
    # SMPTE RP 431-2 cinema white, and ICC Display P3 chromaticities with D65.
    # Both conversions preserve XYZ calculated from their own white points.
    p3d65 = descriptor_for_space_preset("p3-d65-g24").with_transfer(ColorTransfer.LINEAR)
    p3dci = descriptor_for_space_preset("dci-p3-g26").with_transfer(ColorTransfer.LINEAR)
    rgb = np.array([[1, 0, 0], [0.18, 0.18, 0.18], [-0.1, 1.1, 0.01]], np.float64)
    provider = BuiltinColorProvider()
    display = provider.transform_signal(rgb, LINEAR_SCENE, p3d65)
    cinema = provider.transform_signal(rgb, LINEAR_SCENE, p3dci)
    np.testing.assert_allclose(display[0], [0.822461969, 0.033194199, 0.017082631], atol=6e-7)
    np.testing.assert_allclose(display[1], rgb[1], atol=1e-7)
    np.testing.assert_allclose(cinema[0], [0.898951560, 0.031820755, 0.019654169], atol=1e-9)
    np.testing.assert_allclose(cinema[1], [0.196740138, 0.172552318, 0.207096346], atol=1e-9)
    np.testing.assert_allclose(provider.transform_signal(cinema, p3dci, LINEAR_SCENE), rgb, atol=1e-9)
    matrix = video_signal_gamut_matrix(ColorPrimaries.BT709, ColorPrimaries.P3_DCI)
    assert not matrix.flags.writeable


def test_rec709_dci_transform_matches_resolve_gray_and_keeps_color_reversible() -> None:
    rec709 = descriptor_for_space_preset("rec709-g24")
    cinema = descriptor_for_space_preset("dci-p3-g26")
    provider = BuiltinColorProvider()
    signal = np.array([[0.5, 0.5, 0.5], [0.63, 0.28, 0.79]], np.float64)
    encoded = provider.transform_signal(signal, rec709, cinema)
    # Resolve RCM, both mappings None, with the gray read back from 16-bit TIFF.
    np.testing.assert_allclose(encoded[0], [0.54573892, 0.51888304, 0.55661860], atol=1.2e-5)
    np.testing.assert_allclose(encoded[1], [0.633948072, 0.325817574, 0.824449658], atol=1e-9)
    np.testing.assert_allclose(provider.transform_signal(encoded, cinema, rec709), signal, atol=1e-9)
    white = np.full((1, 3), 65535, np.uint16)
    np.testing.assert_allclose(convert_video_signal_bgr(white, rec709, cinema),
                               [[65535, 64479, 65535]], atol=1)


def test_gamma26_preserves_float_headroom_and_alpha() -> None:
    cinema = descriptor_for_space_preset("dci-p3-g26")
    linear = cinema.with_transfer(ColorTransfer.LINEAR)
    bgr = np.array([[[-0.25, 0.18, 2.0, 1.5]]], np.float32)
    result = convert_video_signal_bgr(bgr, linear, cinema)
    expected = [[[-0.586730230, 0.517090249, 1.305511698, 1.5]]]
    np.testing.assert_allclose(result, expected, atol=1e-7)
    np.testing.assert_allclose(convert_video_signal_bgr(result, cinema, linear), bgr, atol=4e-7)
    assert delivery_transfer_tag(cinema.transfer) == "unknown"


def test_bt2020_signal_uses_the_bit_depth_specific_oetf() -> None:
    rec2020_10 = descriptor_for_space_preset("rec2020-bt2020")
    rec2020_12 = rec2020_10.with_transfer(ColorTransfer.BT2020_12)
    linear = rec2020_10.with_transfer(ColorTransfer.LINEAR)
    provider = BuiltinColorProvider()
    rgb = np.array([[0.001, 0.0181, 0.18]], np.float64)
    np.testing.assert_allclose(provider.transform_signal(rgb, linear, rec2020_10),
                               [[0.0045, 0.081697877, 0.409007728]], atol=1e-9)
    np.testing.assert_allclose(provider.transform_signal(rgb, linear, rec2020_12),
                               [[0.0045, 0.081447203, 0.408846402]], atol=1e-9)
    assert delivery_transfer_tag(rec2020_10.transfer) == "bt2020_10"
    assert delivery_transfer_tag(rec2020_12.transfer) == "bt2020_12"


def test_aces_ap0_to_ap1_matches_academy_tra1() -> None:
    ap0 = descriptor_for_space_preset("aces2065-1")
    ap1 = descriptor_for_space_preset("acescg")
    provider = BuiltinColorProvider()
    assert ap0.primaries != ap1.primaries
    # Columns of the Academy S-2014-003 TRA1 matrix for RGB unit vectors.
    rgb = np.eye(3, dtype=np.float64)
    encoded = provider.transform_signal(rgb, ap0, ap1)
    np.testing.assert_allclose(encoded, [
        [1.4514393161, -0.0765537734, 0.0083161484],
        [-0.2365107469, 1.1762296998, -0.0060324498],
        [-0.2149285693, -0.0996759264, 0.9977163014],
    ], atol=1e-9)
    np.testing.assert_allclose(provider.transform_signal(encoded, ap1, ap0), rgb, atol=1e-9)


def test_aces_log_encodings_match_knees_and_known_mid_gray() -> None:
    ap1 = descriptor_for_space_preset("acescg")
    acescct = descriptor_for_space_preset("acescct")
    acescc = descriptor_for_space_preset("acescc")
    provider = BuiltinColorProvider()
    cct_linear = np.array([[-0.01, 0.0078125, 0.18], [0, 1, 4]], np.float64)
    cct = provider.transform_signal(cct_linear, ap1, acescct)
    np.testing.assert_allclose(cct, [
        [-0.032496843221, 0.155251141553, 0.413588402492],
        [0.072905534196, 0.554794520548, 0.668949771689],
    ], atol=1e-12)
    np.testing.assert_allclose(provider.transform_signal(cct, acescct, ap1), cct_linear, atol=1e-12)

    cc_linear = np.array([[0, 2 ** -16, 2 ** -15], [0.18, 1, 4]], np.float64)
    cc = provider.transform_signal(cc_linear, ap1, acescc)
    np.testing.assert_allclose(cc, [
        [-0.358447488584, -0.325059217995, -0.301369863014],
        [0.413588402492, 0.554794520548, 0.668949771689],
    ], atol=1e-12)
    np.testing.assert_allclose(provider.transform_signal(cc, acescc, ap1), cc_linear, atol=1e-12)
    limit = provider.transform_signal(np.full((1, 3), 10.0, np.float32), acescc, ap1)
    np.testing.assert_array_equal(limit, [[65504, 65504, 65504]])


def test_pq_matches_absolute_reference_luminance() -> None:
    pq = descriptor_for_space_preset("rec2100-pq")
    linear = pq.with_transfer(ColorTransfer.LINEAR)
    provider = BuiltinColorProvider()
    codes = np.array([[0.508078421517, 0.580688881041, 0.751827096247]], np.float64)
    luminance = VideoSignalLuminance(203.0)
    expected = np.array([[100.0, 203.0, 1000.0]]) / 203.0
    np.testing.assert_allclose(provider.transform_signal(codes, pq, linear, luminance=luminance),
                               expected, atol=1e-8)
    np.testing.assert_allclose(provider.transform_signal(expected, linear, pq, luminance=luminance),
                               codes, atol=1e-11)
    with pytest.raises(ValueError, match="reference white"):
        provider.transform_signal(codes, pq, linear)


def test_hlg_includes_the_reference_display_ootf() -> None:
    hlg = descriptor_for_space_preset("rec2100-hlg")
    linear = hlg.with_transfer(ColorTransfer.LINEAR)
    provider = BuiltinColorProvider()
    gray_codes = np.repeat([[0.0], [0.5], [0.75], [1.0]], 3, axis=1)
    luminance = VideoSignalLuminance(203.0, 1000.0, 1.2)
    expected_nits = np.repeat([[0.0], [50.697028491], [203.152145354], [1000.00002924]], 3, axis=1)
    decoded = provider.transform_signal(gray_codes, hlg, linear, luminance=luminance)
    np.testing.assert_allclose(decoded * 203.0, expected_nits, atol=1e-8)
    rgb = np.array([[0.75, 0.5, 0.25]], np.float64)
    decoded = provider.transform_signal(rgb, hlg, linear, luminance=luminance)
    np.testing.assert_allclose(provider.transform_signal(decoded, linear, hlg, luminance=luminance),
                               rgb, atol=1e-8)
    with pytest.raises(ValueError, match="display peak"):
        provider.transform_signal(rgb, hlg, linear, luminance=VideoSignalLuminance(203.0))


def test_exact_ocio_probe_rejects_legacy_fallback_color_spaces() -> None:
    provider = OCIOColorProvider()
    if not provider.is_available():
        pytest.skip("OpenColorIO unavailable")
    rec709 = descriptor_for_space_preset("rec709-g24")
    unregistered = ColorSpaceDescriptor(ColorPrimaries.BT2020, ColorTransfer.SRGB)
    assert provider.probe_exact_transform(rec709, LINEAR_SCENE) is None
    assert provider.probe_transform(unregistered, LINEAR_SCENE) is None
    assert provider.probe_exact_transform(unregistered, LINEAR_SCENE) == "processor_missing"


def test_partial_legacy_hdr_project_retains_the_established_transform() -> None:
    settings = {
        "color.timeline_space": "rec709-g24", "color.output_space": "legacy",
        "color.output.timeline_primaries": "bt2020", "color.output.timeline_transfer": "smpte2084",
    }
    assert video_signal_uses_legacy_space(settings)
    assert not video_signal_uses_legacy_space(settings, timeline_only=True)
    spaces = video_signal_spaces(settings)
    assert spaces is not None
    source, destination = spaces
    frame = np.full((1, 1, 3), 0.5, np.float32)
    expected = BuiltinColorProvider().transform(frame, source, destination)
    actual = convert_video_signal_bgr(frame, source, destination, legacy=True)
    np.testing.assert_allclose(actual, expected, atol=1e-5)
    with pytest.raises(ValueError, match="reference white"):
        convert_video_signal_bgr(frame, source, destination)


@pytest.mark.parametrize(("version", "minimum", "expected"), [
    ("1.3", (2, 4), [[0.3895283, 0.3895285, 0.3895285], [1.1342543, 0.3129884, 0.1136975]]),
    ("2.0", (2, 5), [[0.3831160, 0.3831167, 0.3831164], [1.0000063, 0.4592036, 0.3751599]]),
])
def test_pinned_aces_rec709_output_views(
    version: str, minimum: tuple[int, int], expected: list[list[float]],
) -> None:
    ocio = pytest.importorskip("PyOpenColorIO")
    if tuple(map(int, ocio.__version__.split(".")[:2])) < minimum:
        pytest.skip(f"Pinned config requires OpenColorIO {minimum[0]}.{minimum[1]}")
    source = descriptor_for_space_preset("acescg")
    frame = np.array([[[0.18, 0.18, 0.18, 1.5], [4, 0.15, 0.02, -0.1]]], np.float32)
    original = frame.copy()
    view = resolve_aces_output_view(version, "rec709-g24")
    assert view.display == "Rec.1886 Rec.709 - Display"
    assert f"aces-v{version}" in view.config_id
    result = apply_aces_output_transform(frame, source, aces_version=version, target="rec709-g24")
    np.testing.assert_allclose(result[0, :, :3], expected, atol=3e-5)
    np.testing.assert_array_equal(result[..., 3], frame[..., 3])
    np.testing.assert_array_equal(frame, original)


def test_aces13_reference_gamut_compression_is_explicit() -> None:
    ocio = pytest.importorskip("PyOpenColorIO")
    if tuple(map(int, ocio.__version__.split(".")[:2])) < (2, 4):
        pytest.skip("Pinned config requires OpenColorIO 2.4")
    source = descriptor_for_space_preset("acescg")
    patch = np.array([[4, 0.15, 0.02]], np.float32)
    compressed = apply_aces_output_transform(
        patch, source, aces_version="1.3", target="rec709-g24", reference_gamut_compression=True,
    )
    np.testing.assert_allclose(compressed, [[1.0994277, 0.5463747, 0.4257437]], atol=3e-5)
    with pytest.raises(ValueError, match="no registered output"):
        resolve_aces_output_view("2.0", "dci-p3-g26-d60-sim")
