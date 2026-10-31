"""C6-1: ACEScg (AP1) support across provider / OCIO config / descriptor."""

from __future__ import annotations

import numpy as np
import pytest

from src.shared.color.color_transform_provider import (
    BuiltinColorProvider,
    _AP1_TO_BT709,
    _BT709_TO_AP1,
    _normalise_space,
    reset_provider_cache,
)
from src.shared.color.color_space_descriptor import (
    ACESCG_SCENE,
    LINEAR_SCENE,
    SRGB_IMAGE,
)
from src.shared.color.types import ColorPrimaries

pytestmark = pytest.mark.composite_core


def setup_function(function):
    reset_provider_cache()


class TestAp1Matrices:
    def test_compose_to_identity(self):
        # Published 7-digit matrices: composition error stays below 5e-5 —
        # far under 8-bit quantisation (1/255 ≈ 3.9e-3).
        product = _AP1_TO_BT709.astype(np.float64) @ _BT709_TO_AP1.astype(
            np.float64)
        assert np.allclose(product, np.eye(3), atol=2e-4)

    def test_white_maps_to_white(self):
        # Rows of each matrix sum to ~1: ACES white ↔ BT.709 white without
        # a scale error (published values are rounded; 1e-4 covers it).
        white = np.array([1.0, 1.0, 1.0], dtype=np.float64)
        assert np.allclose(_AP1_TO_BT709 @ white, white, atol=1e-4)
        assert np.allclose(_BT709_TO_AP1 @ white, white, atol=1e-4)


class TestBuiltinAcescg:
    def test_alias_normalisation(self):
        assert _normalise_space("aces_cg") == "acescg"
        assert _normalise_space("ACEScg") == "acescg"

    def test_acescg_linear_roundtrip(self):
        b = BuiltinColorProvider()
        img = np.random.default_rng(7).random((2, 3, 4)).astype(np.float32)
        to_aces = b.export_encode(img.copy(), "linear", "aces_cg")
        back = b.export_encode(to_aces, "acescg", "linear")
        assert np.allclose(back, img, atol=2e-4)

    def test_srgb_acescg_roundtrip(self):
        b = BuiltinColorProvider()
        img = np.random.default_rng(3).random((2, 2, 4)).astype(np.float32)
        to_aces = b.export_encode(img.copy(), "srgb", "acescg")
        back = b.export_encode(to_aces, "aces_cg", "srgb")
        assert np.allclose(back, img, atol=1e-4)

    def test_white_preserved_through_acescg(self):
        b = BuiltinColorProvider()
        img = np.ones((1, 1, 4), dtype=np.float32)
        out = b.export_encode(img.copy(), "acescg", "linear")
        assert np.allclose(out[0, 0, :3], 1.0, atol=1e-5)


class TestOcioAcescgParity:
    """OCIO (bundled config's acescg space) must agree with the builtin."""

    def test_ocio_matches_builtin_acescg_to_linear(self):
        pytest.importorskip("PyOpenColorIO")
        from src.shared.color.color_transform_provider import OCIOColorProvider
        ocio = OCIOColorProvider()
        if not ocio.is_available():
            pytest.skip("OCIO not available in this environment")
        builtin = BuiltinColorProvider()
        img = np.random.default_rng(11).random((2, 3, 4)).astype(np.float32)
        via_ocio = ocio.export_encode(img.copy(), "acescg", "lin")
        via_builtin = builtin.export_encode(img.copy(), "acescg", "linear")
        assert np.allclose(via_ocio, via_builtin, atol=1e-5)

    def test_acescg_processor_is_not_identity(self):
        pytest.importorskip("PyOpenColorIO")
        from src.shared.color.color_transform_provider import OCIOColorProvider
        ocio = OCIOColorProvider()
        if not ocio.is_available():
            pytest.skip("OCIO not available in this environment")
        img = np.array([[[0.2, 0.5, 0.8, 1.0]]], dtype=np.float32)
        out = ocio.export_encode(img.copy(), "acescg", "lin")
        assert not np.allclose(out, img, atol=1e-6)


class TestDescriptorAcescg:
    def test_preset_fields(self):
        assert ACESCG_SCENE.primaries == ColorPrimaries.AP1
        assert ACESCG_SCENE.is_linear

    def test_to_ocio_name(self):
        assert ACESCG_SCENE.to_ocio_name() == "acescg"
        # Plain BT.709 scene-linear stays the "lin" working space.
        assert LINEAR_SCENE.to_ocio_name() == "lin"

    def test_from_ocio_name_roundtrip(self):
        back = type(ACESCG_SCENE).from_ocio_name("acescg")
        assert back == ACESCG_SCENE
        # Alias defined in the bundled config resolves to the same preset.
        assert type(ACESCG_SCENE).from_ocio_name("aces_cg") == ACESCG_SCENE

    def test_srgb_image_descriptor_unchanged(self):
        assert SRGB_IMAGE.to_ocio_name() == "srgb"
