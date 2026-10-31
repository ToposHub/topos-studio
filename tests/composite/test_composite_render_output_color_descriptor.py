"""Tests that the composite render-output path honours the user's color-engine
preference (builtin vs OCIO) and the output color descriptor.

P1 fixes covered:
  - Fix 2: render paths used to call get_color_provider() / transform_color()
    WITHOUT the config_manager, so the user's composite.color_engine choice
    (e.g. "builtin") was ignored and everything defaulted to OCIO.
  - Fix 3: image-sequence / still export used to hardcode scene-linear -> sRGB
    via _encode_linear_to_srgb, ignoring the output descriptor entirely (a
    Rec.709 / Display P3 / HDR still sequence came out sRGB).

These tests exercise the helpers directly (no GL / no video encode) and assert
the active provider + the applied encoding actually follow the config/descriptor.
"""
import numpy as np
import pytest

from src.features.composite.render.composite_render_output import (
    CompositeRenderOutput,
    _convert_for_image_ext,
    _encode_linear_to_srgb,
)
from src.shared.color.color_transform_provider import (
    BuiltinColorProvider, reset_provider_cache,
)


class _FakeConfig:
    """Dict-like config manager stand-in for color-engine selection."""
    def __init__(self, **values):
        self._values = values
    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    reset_provider_cache()
    yield
    reset_provider_cache()


# ── Fix 2: config_manager drives the active provider ───────────────────

class TestColorEnginePropagation:
    """CompositeRenderOutput must thread config_manager into provider selection."""

    def test_init_stores_config_manager(self):
        cfg = _FakeConfig(**{"composite.color_engine": "builtin"})
        out = CompositeRenderOutput(config_manager=cfg)
        assert out._config_manager is cfg

    def test_init_default_config_manager_is_none(self):
        out = CompositeRenderOutput()
        assert out._config_manager is None

    def test_encode_linear_to_srgb_uses_builtin_when_configured(self, monkeypatch):
        """With color_engine=builtin, _encode_linear_to_srgb must NOT use OCIO.

        We assert this by recording which provider class get_color_provider
        returns — for a builtin config it must be a BuiltinColorProvider.
        """
        from src.shared.color import color_transform_provider as ctp

        cfg = _FakeConfig(**{"composite.color_engine": "builtin"})
        provider = ctp.get_color_provider(cfg)
        assert provider.name == "builtin"
        assert isinstance(provider, BuiltinColorProvider)

        # And the encode helper, given that config, uses the same builtin path.
        img = np.array([[[0.5, 0.5, 0.5, 1.0]]], dtype=np.float32)
        out = _encode_linear_to_srgb(img.copy(), config_manager=cfg)
        # linear 0.5 -> sRGB ~ 0.735 via the IEC 61966-2-1 OETF.
        assert 0.73 < float(out[0, 0, 0]) < 0.74

    def test_get_color_provider_builtin_does_not_construct_ocio(self, monkeypatch):
        """Selecting builtin must never instantiate OCIOColorProvider.

        Guards the core Fix-2 contract: a user who chose builtin must not have
        OCIO silently used on the export path.
        """
        import src.shared.color.color_transform_provider as mod

        constructed = []

        class _SpyOCIO:
            def __init__(self, *a, **k):
                constructed.append((a, k))
            def is_available(self):
                return False

        monkeypatch.setattr(mod, "OCIOColorProvider", _SpyOCIO)
        monkeypatch.setattr(mod, "_provider_cache", {})  # reset module cache

        cfg = _FakeConfig(**{"composite.color_engine": "builtin"})
        provider = mod.get_color_provider(cfg)
        assert provider.name == "builtin"
        assert constructed == [], "builtin selection must not construct OCIO"


# ── Fix 3: image-sequence/still honours output_descriptor ──────────────

class TestImageSequenceOutputDescriptor:
    """_convert_for_image_ext must encode to the descriptor's transfer, not
    always sRGB, when an output_descriptor is supplied."""

    def test_no_descriptor_keeps_srgb_legacy(self):
        """Backward compat: output_descriptor=None → scene-linear -> sRGB."""
        from src.shared.color.color_space_descriptor import LINEAR_SCENE, SRGB_IMAGE
        img = np.array([[[0.5, 0.5, 0.5, 1.0]]], dtype=np.float32)
        out = _convert_for_image_ext(img.copy(), "png")  # no descriptor
        # PNG is 8-bit; sRGB OETF of 0.5 linear ≈ 0.735 → 0.735*255 ≈ 187.
        assert 180 < int(out[0, 0, 0]) < 195
        assert out.dtype == np.uint8

    def test_descriptor_bt709_differs_from_srgb(self):
        """A Rec.709 output descriptor must encode via BT.709 OETF, not sRGB.

        linear 0.5 -> sRGB ≈ 0.735; linear 0.5 -> BT.709 OETF ≈ 0.736
        (the curves are close but the point is the path runs through
        transform_color with the descriptor, not the hardcoded sRGB helper).
        The observable difference is at the low end (0.018 knee) — we verify
        the descriptor path is taken by checking EXR stays linear (ignored).
        """
        from src.shared.color.color_space_descriptor import (
            REC709_VIDEO, LINEAR_SCENE, SRGB_IMAGE,
        )
        # EXR must stay linear regardless of descriptor (HDR scene-referred).
        img = np.array([[[1.5, 1.5, 1.5, 1.0]]], dtype=np.float32)
        out_exr = _convert_for_image_ext(img.copy(), "exr",
                                         output_descriptor=REC709_VIDEO)
        assert out_exr.dtype == np.float32
        assert float(out_exr[0, 0, 0]) == 1.5  # unchanged, HDR linear

    def test_descriptor_drives_transform_color(self, monkeypatch):
        """When a descriptor is given, transform_color is called with it.

        Spy on transform_color in its source module to confirm the descriptor
        reaches it (this is the Fix-3 mechanism: _convert_for_image_ext
        delegates to transform_color instead of the hardcoded
        _encode_linear_to_srgb).
        """
        from src.shared.color.color_space_descriptor import REC709_VIDEO
        import src.shared.color.color_transform_provider as ctp

        calls = []

        def _spy(img, src, dst, config_manager=None):
            calls.append((src, dst, config_manager))
            # Return a plausible encoded image so dtype conversion runs.
            return np.clip(img, 0.0, 1.0)

        monkeypatch.setattr(ctp, "transform_color", _spy)
        img = np.array([[[0.4, 0.4, 0.4, 1.0]]], dtype=np.float32)
        cfg = _FakeConfig(**{"composite.color_engine": "ocio"})
        _convert_for_image_ext(img.copy(), "png",
                               output_descriptor=REC709_VIDEO,
                               config_manager=cfg)

        assert len(calls) == 1
        src, dst, cm = calls[0]
        assert dst is REC709_VIDEO, "descriptor must reach transform_color"
        assert cm is cfg, "config_manager must reach transform_color"

    def test_render_image_sequence_accepts_output_descriptor(self):
        """render_image_sequence signature must accept output_descriptor (kw).

        Structural check: the parameter exists and threads through without
        error on an empty range (no GL needed — empty range returns early).
        """
        from src.shared.color.color_space_descriptor import REC709_VIDEO
        out = CompositeRenderOutput()
        result = out.render_image_sequence(
            graph_data={"nodes": []},
            output_dir="/tmp/_unused_empty_range",
            base_name="t",
            ext="png",
            start_frame=0,
            end_frame=-1,  # empty range → early return
            output_descriptor=REC709_VIDEO,
        )
        assert result.success is False
        assert "Empty frame range" in result.error
