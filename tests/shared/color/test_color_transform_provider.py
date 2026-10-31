"""Tests for the color-transform provider (dual-track colour management).

Covers the built-in sRGB↔linear provider and the OCIO provider's soft-
dependency behaviour (unavailable when PyOpenColorIO isn't installed).
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from src.shared.color.color_transform_provider import (
    BuiltinColorProvider,
    OCIOColorProvider,
    get_color_provider,
    get_bundled_ocio_config_path,
    is_ocio_available,
    reset_provider_cache,
)

pytestmark = pytest.mark.composite_core


# ── BuiltinColorProvider ───────────────────────────────────────────────────

class TestBuiltinColorProvider:
    """The default provider: sRGB↔linear numpy math."""

    def test_always_available(self):
        p = BuiltinColorProvider()
        assert p.is_available() is True
        assert p.name == "builtin"

    def test_same_space_is_noop(self):
        """When src == dst, the array is returned unchanged (same object)."""
        p = BuiltinColorProvider()
        img = np.full((4, 4, 4), 0.5, dtype='f4')
        out = p.export_encode(img, "linear", "linear")
        assert out is img

    def test_srgb_to_linear_correct_formula(self):
        """sRGB 0.5 → linear must use the full EOTF: ((c+0.055)/1.055)^2.4.

        The legacy file_output code omitted the +0.055, producing 0.1666
        instead of the correct 0.2140. This test guards against regression.
        """
        p = BuiltinColorProvider()
        img = np.full((1, 1, 4), 0.5, dtype='f4')
        img[0, 0, 3] = 1.0
        out = p.export_encode(img, "srgb", "linear")
        # Correct: ((0.5 + 0.055) / 1.055) ^ 2.4 ≈ 0.2140
        assert abs(out[0, 0, 0] - 0.2140) < 0.001, \
            f"sRGB 0.5 should map to ~0.2140 linear, got {out[0, 0, 0]}"

    def test_linear_to_srgb_correct_formula(self):
        """linear 0.214 → sRGB ≈ 0.5 (inverse of the above)."""
        p = BuiltinColorProvider()
        img = np.full((1, 1, 4), 0.214, dtype='f4')
        img[0, 0, 3] = 1.0
        out = p.export_encode(img, "linear", "srgb")
        assert abs(out[0, 0, 0] - 0.5) < 0.002, \
            f"linear 0.214 should map to ~0.5 sRGB, got {out[0, 0, 0]}"

    def test_roundtrip_preserves_values(self):
        """sRGB → linear → sRGB should round-trip within float precision."""
        p = BuiltinColorProvider()
        for v in [0.1, 0.25, 0.5, 0.75, 0.9]:
            img = np.array([[[v, v, v, 1.0]]], dtype='f4')
            lin = p.export_encode(img, "srgb", "linear")
            back = p.export_encode(lin, "linear", "srgb")
            assert abs(back[0, 0, 0] - v) < 0.002, \
                f"roundtrip failed for {v}: got {back[0, 0, 0]}"

    def test_preserves_hdr_values(self):
        """Linear values >1.0 are preserved (no clamping in the provider).

        Clamping is the caller's responsibility (file_output clamps for
        non-float formats). The provider must pass HDR values through.
        """
        p = BuiltinColorProvider()
        img = np.array([[[2.0, 4.0, 8.0, 1.0]]], dtype='f4')
        out = p.export_encode(img, "linear", "srgb")
        # linear→sRGB of values >1: 1.055 * c^(1/2.4) - 0.055, all >1.
        assert out[0, 0, 0] > 1.0
        assert out[0, 0, 1] > 1.0
        assert out[0, 0, 2] > 1.0

    def test_alpha_channel_unchanged_by_conversion(self):
        """The conversion must not alter the alpha channel."""
        p = BuiltinColorProvider()
        img = np.array([[[0.5, 0.5, 0.5, 0.7]]], dtype='f4')
        out = p.export_encode(img, "srgb", "linear")
        assert abs(out[0, 0, 3] - 0.7) < 1e-6, "alpha should be unchanged"

    def test_name_aliases_normalised(self):
        """Various name spellings map to the same canonical space."""
        p = BuiltinColorProvider()
        img = np.full((1, 1, 4), 0.5, dtype='f4')
        # "sRGB", "SRGB", "display" should all be treated as srgb.
        for alias in ["sRGB", "SRGB", "display", "srgb_tx"]:
            out = p.export_encode(img.copy(), alias, "linear")
            assert abs(out[0, 0, 0] - 0.2140) < 0.001, \
                f"alias {alias!r} should normalise to srgb"


# ── OCIOColorProvider ─────────────────────────────────────────────────────

class TestOCIOColorProvider:
    """OCIO provider soft-dependency behaviour.

    PyOpenColorIO is not installed in the test environment, so these tests
    verify graceful degradation. When OCIO IS installed, a separate
    integration test (skipped here) would exercise actual transforms.
    """

    def test_is_ocio_available_reflects_install(self):
        """is_ocio_available() returns whether PyOpenColorIO imports."""
        # In this env it's not installed — just assert the function runs.
        result = is_ocio_available()
        assert isinstance(result, bool)

    def test_ocio_provider_unavailable_without_library(self):
        """When PyOpenColorIO isn't installed, the provider is unavailable."""
        if is_ocio_available():
            pytest.skip("PyOpenColorIO is installed — skip the unavailable test")
        p = OCIOColorProvider()
        assert p.is_available() is False
        assert p.name == "ocio"

    def test_ocio_provider_export_passes_through_when_unavailable(self):
        """An unavailable OCIO provider passes data through unchanged."""
        if is_ocio_available():
            pytest.skip("PyOpenColorIO is installed — skip the unavailable test")
        p = OCIOColorProvider()
        img = np.full((2, 2, 4), 0.5, dtype='f4')
        out = p.export_encode(img, "srgb", "linear")
        # Should return the input unchanged (no crash, no conversion).
        assert out is img

    def test_bundled_config_path_exists(self):
        """The bundled minimal OCIO config file must exist."""
        path = get_bundled_ocio_config_path()
        assert os.path.isfile(path), \
            f"Bundled OCIO config not found at {path}"


# ── Factory ────────────────────────────────────────────────────────────────

class TestColorProviderFactory:
    """get_color_provider() selection + fallback logic."""

    def setup_method(self):
        reset_provider_cache()

    def teardown_method(self):
        reset_provider_cache()

    def test_default_is_builtin(self):
        """With no config, the factory returns the built-in provider.

        The default engine preference is "ocio" — but with no config manager
        and no OCIO runtime (importable + loadable config) the factory must
        fall back to builtin. On machines WITH a working OCIO the default
        engine legitimately resolves to the OCIO provider, so only assert
        the fallback direction here (a provider that works).
        """
        p = get_color_provider(None)
        assert p.name in ("builtin", "ocio")
        assert p.is_available()

    def test_builtin_explicit(self):
        """Explicit 'builtin' config returns builtin."""

        class Cfg:
            def get(self, k, d=None):
                return {"composite.color_engine": "builtin"}.get(k, d)

        assert get_color_provider(Cfg()).name == "builtin"

    def test_ocio_unavailable_falls_back_to_builtin(self):
        """Requesting OCIO when unavailable falls back to builtin."""
        if is_ocio_available():
            pytest.skip("PyOpenColorIO installed — fallback path not exercised")

        class Cfg:
            def get(self, k, d=None):
                return {"composite.color_engine": "ocio"}.get(k, d)

        p = get_color_provider(Cfg())
        assert p.name == "builtin", \
            "OCIO unavailable should fall back to builtin"

    def test_provider_cached(self):
        """Repeated calls with the same config return the cached instance."""
        p1 = get_color_provider(None)
        p2 = get_color_provider(None)
        assert p1 is p2

    def test_cache_reset_picks_up_config_change(self):
        """After reset_provider_cache(), a new provider is built."""
        p1 = get_color_provider(None)
        reset_provider_cache()
        p2 = get_color_provider(None)
        # New instance after reset (cache was cleared).
        assert p1 is not p2
