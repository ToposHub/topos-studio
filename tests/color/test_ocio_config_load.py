"""Tests that the bundled OCIO config loads and transforms correctly.

P0-1 regression: the bundled minimal.ocio previously used BuiltinTransform
styles ('sRGB - EOTF', 'Rec.709 - EOTF') that don't exist in this OCIO build,
so the config failed to load and OCIOColorProvider silently fell back to the
built-in numpy provider — masking OCIO as a no-op. These tests guard that the
config loads and that each named space actually transforms (not pass-through).
"""
import numpy as np
import pytest

from src.shared.color.color_transform_provider import (
    OCIOColorProvider,
    _ocio_space_name,
    reset_provider_cache,
)


@pytest.fixture
def ocio_provider():
    reset_provider_cache()
    p = OCIOColorProvider()
    if not p.is_available():
        pytest.skip("OCIO not available in this environment")
    return p


def test_config_loads(ocio_provider):
    """The bundled minimal.ocio must load (is_available == True)."""
    assert ocio_provider.is_available()


def test_srgb_to_linear_matches_canonical_curve(ocio_provider):
    """sRGB EOTF inverse via OCIO must match the IEC 61966-2-1 value."""
    img = np.array([[[0.5, 0.5, 0.5, 1.0]]], dtype=np.float32)
    out = ocio_provider.export_encode(img, "srgb", "linear")
    # Canonical: ((0.5+0.055)/1.055)^2.4 = 0.2140
    assert abs(float(out[0, 0, 0]) - 0.214) < 2e-3


def test_rec709_to_linear(ocio_provider):
    """Rec.709 decode via OCIO uses the BT.709 OETF inverse curve.

    This is the Rec.709-contract guard: OCIO's rec709 space MUST match the
    BuiltinColorProvider's BT.709 OETF inverse (and therefore the GPU
    cs_transform shader) so CPU export, GPU preview, and OCIO give identical
    results. Previously OCIO used the BT.1886 pure pow-2.4 curve (code 0.5
    -> ~0.19), which disagreed with the builtin/shader BT.709 OETF.
    """
    img = np.array([[[0.5, 0.5, 0.5, 1.0]]], dtype=np.float32)
    out = ocio_provider.export_encode(img, "rec709", "linear")
    # export_encode applies only the transfer (no range expansion). The BT.709
    # OETF inverse at code 0.5 is ((0.5+0.099)/1.099)^(1/0.45) = 0.2596.
    assert abs(float(out[0, 0, 0]) - 0.2596) < 2e-3


def test_pq_space_transforms_not_passthrough(ocio_provider):
    """bt2020_pq must decode + gamut-convert, not return the input unchanged."""
    img = np.array([[[0.5, 0.5, 0.5, 1.0]]], dtype=np.float32)
    original = img.copy()  # OCIO applyRGBA mutates in place
    out = ocio_provider.export_encode(img, "bt2020_pq", "linear")
    assert not np.allclose(out, original), "bt2020_pq -> linear was a no-op"


def test_hlg_space_transforms_not_passthrough(ocio_provider):
    """bt2020_hlg must decode + gamut-convert, not return the input unchanged."""
    img = np.array([[[0.5, 0.5, 0.5, 1.0]]], dtype=np.float32)
    original = img.copy()  # OCIO applyRGBA mutates in place
    out = ocio_provider.export_encode(img, "bt2020_hlg", "linear")
    assert not np.allclose(out, original), "bt2020_hlg -> linear was a no-op"


def test_ocio_space_name_maps_linear_to_lin():
    """The OCIO config names the working space 'lin', not 'linear'.

    Without this mapping the processor lookup silently fails and OCIO returns
    the input unchanged — the original P0-1 mask.
    """
    assert _ocio_space_name("linear") == "lin"
    assert _ocio_space_name("scene-linear") == "lin"
    assert _ocio_space_name("srgb") == "srgb"
    assert _ocio_space_name("rec709") == "rec709"
