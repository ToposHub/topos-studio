"""Regression targets measured in Resolve Studio 20.2.1.6, full-range 709/Gamma 2.4."""
from pathlib import Path

import numpy as np
import pytest

from src.features.color.core.warper_color import (
    RESOLVE_HS709_SPACE,
    RESOLVE_HS709_V2_SPACE,
    RESOLVE_HS709_V3_SPACE,
    WARPER_CHROMA709_SPACE,
    apply_resolve_hs709,
    apply_resolve_hs709_texture,
    prepare_resolve_hs709_texture,
)


@pytest.mark.parametrize("index, maximum", [(0, 10.0), (1, 5.0), (2, 18.0), (3, 3.5)])
def test_refined_warper_improves_held_out_multi_axis_settings(index: int, maximum: float) -> None:
    fixture = Path(__file__).with_name("fixtures") / "resolve_warper_rec709_g24_v3.npz"
    with np.load(fixture, allow_pickle=False) as data:
        rgb = data["before"][:4096].astype(np.float32) / 65535.0
        expected = data["targets"][index, :4096].astype(np.float32) / 65535.0
        offsets = data["offsets"][index]
    errors = []
    for space in (RESOLVE_HS709_V2_SPACE, RESOLVE_HS709_V3_SPACE):
        texture = prepare_resolve_hs709_texture(offsets, space)
        output = apply_resolve_hs709_texture(rgb, texture, space, encoded_input=True)
        errors.append(float(np.sqrt(np.mean((output.clip(0.0, 1.0) - expected) ** 2)) * 1023))
    assert errors[1] < min(errors[0], maximum)


@pytest.mark.parametrize("index, maximum", [(0, 14.0), (1, 15.0), (2, 20.0), (3, 15.0)])
def test_chroma_response_on_held_out_resolve_gestures(index: int, maximum: float) -> None:
    fixture = Path(__file__).with_name("fixtures") / "resolve_chroma_rec709_g24.npz"
    with np.load(fixture, allow_pickle=False) as data:
        rgb = data["before"][:4096].astype(np.float32) / 65535.0
        expected = data["targets"][index, :4096].astype(np.float32) / 65535.0
        controls = data[f"controls_{index}"]
    texture = prepare_resolve_hs709_texture(controls, WARPER_CHROMA709_SPACE)
    output = apply_resolve_hs709_texture(rgb, texture, WARPER_CHROMA709_SPACE, encoded_input=True)
    rmse = np.sqrt(np.mean((output.clip(0.0, 1.0) - expected) ** 2)) * 1023
    baseline = np.sqrt(np.mean((rgb - expected) ** 2)) * 1023
    assert rmse < min(baseline, maximum)


@pytest.fixture(scope="module")
def resolve_samples() -> tuple[np.ndarray, dict[str, tuple[np.ndarray, np.ndarray]]]:
    path = Path(__file__).with_name("fixtures") / "resolve_warper_rec709_g24_v2.npz"
    with np.load(path, allow_pickle=False) as data:
        patches = data["before"].astype(np.float64) / 65535.0
        cases = {
            str(case): (target.astype(np.float64) / 65535.0, offsets)
            for case, target, offsets in zip(data["cases"], data["targets"], data["offsets"])
        }
    return patches, cases


@pytest.mark.parametrize(("case", "max_error"), [
    ("hsp-red-hue", 1.0),
    ("v2-red-hue-reverse", 1.1),
    ("hsp-red-sat", 1.0),
    ("hsp-red-luma", 0.3),
    ("v2-cyan-hue", 0.8),
    ("v2-cyan-sat", 0.5),
    ("v2-magenta-inner-hue", 0.1),
    # These four control settings were excluded from profile generation.
    ("hsp-cyan-mixed", 1.6),
    ("v2-red-hue-small", 3.6),
    ("v2-blue-outer-mixed", 16.0),
    ("v2-green-inner-mixed", 19.0),
])
def test_v2_matches_resolve_on_independent_rgb_patches(
    resolve_samples: tuple[np.ndarray, dict[str, tuple[np.ndarray, np.ndarray]]],
    case: str, max_error: float,
) -> None:
    patches, cases = resolve_samples
    target, offsets = cases[case]
    source = patches ** 2.4
    v1 = apply_resolve_hs709(source, offsets, RESOLVE_HS709_SPACE)
    v2 = apply_resolve_hs709(source, offsets, RESOLVE_HS709_V2_SPACE)
    texture = prepare_resolve_hs709_texture(offsets, RESOLVE_HS709_V2_SPACE)
    encoded_v2 = apply_resolve_hs709_texture(
        patches, texture, RESOLVE_HS709_V2_SPACE, encoded_input=True)
    # Exclude the gray ramp here. TIFF comparison clips the final output to the
    # file's format while the renderer itself preserves extended-range RGB.
    rgb_patches = slice(0, 4096)
    error_v1 = np.mean((np.clip(v1, 0.0, 1.0) ** (1.0 / 2.4) - target)[rgb_patches] ** 2)
    error_v2 = np.mean((np.clip(v2, 0.0, 1.0) ** (1.0 / 2.4) - target)[rgb_patches] ** 2)
    assert np.sqrt(error_v2) * 1023.0 < max_error
    assert error_v2 < error_v1
    np.testing.assert_allclose(
        np.clip(encoded_v2, 0.0, 1.0),
        np.clip(v2, 0.0, 1.0) ** (1.0 / 2.4), atol=2e-6, rtol=0,
    )
    np.testing.assert_array_equal(v2[4096:], source[4096:])


def test_v2_preserves_negative_values_and_hdr_headroom() -> None:
    offsets = np.zeros((6, 6, 3), np.float32)
    offsets[2, 0] = (-0.06, 0.14, 0.12)
    source = np.array([[2.0, 0.5, 0.5], [2.0, -0.125, 0.6], [-0.2, -0.2, -0.2]], np.float32)
    output = apply_resolve_hs709(source, offsets, RESOLVE_HS709_V2_SPACE)
    assert np.isfinite(output).all()
    assert output[0, 0] > 1.0
    assert output[1, 1] >= -0.125
    np.testing.assert_array_equal(output[-1], source[-1])
    np.testing.assert_array_equal(apply_resolve_hs709(source, offsets * 0, RESOLVE_HS709_V2_SPACE), source)
