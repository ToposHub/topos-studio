"""C6-2: viewer ODT via the colour provider (OCIO 3D LUT, safe fallback).

The bake and fallback logic is tested at state level; actual GL upload and
per-frame sampling need a real context (manual QA + shader source asserts).
"""

from __future__ import annotations

import inspect

import numpy as np
import pytest

pytestmark = pytest.mark.composite_core


def _make_viewer(qapp):
    from src.features.composite.ui.composite_viewport import CompositeViewer
    viewer = CompositeViewer()
    yield viewer
    viewer.close()


class _FakeCPU:
    """applyRGBA stand-in that marks the red channel."""

    def applyRGBA(self, flat):
        flat[:, 0] = 0.25


class _FakeOcioProvider:
    name = "ocio"

    def is_available(self):
        return True

    def __init__(self):
        self.calls = []

    def _get_processor(self, src, dst):
        self.calls.append((src, dst))
        return _FakeCPU()


class TestOdtLutFallback:
    def test_no_descriptor_uses_builtin(self, qapp):
        viewer = next(_make_viewer(qapp))
        assert viewer._display_space_descriptor is None
        assert viewer._ensure_odt_lut() is False

    def test_builtin_provider_falls_back(self, qapp):
        from src.shared.color.color_transform_provider import (
            BuiltinColorProvider)
        viewer = next(_make_viewer(qapp))
        viewer._display_space_descriptor = self._descriptor()
        viewer._odt_color_provider = lambda: BuiltinColorProvider()
        assert viewer._ensure_odt_lut() is False
        # Failed bake is remembered — no per-frame re-attempts.
        assert viewer._odt_lut_failed_key is not None

    def test_hdr_without_tonemap_keeps_builtin(self, qapp):
        viewer = next(_make_viewer(qapp))
        from src.shared.color.color_space_descriptor import HDR10_VIDEO
        viewer._display_space_descriptor = HDR10_VIDEO
        viewer._tonemap = 0

        def _boom():
            raise AssertionError("provider must not be consulted")

        viewer._odt_color_provider = _boom
        assert viewer._ensure_odt_lut() is False

    def test_success_path_bakes_and_caches(self, qapp):
        viewer = next(_make_viewer(qapp))
        provider = _FakeOcioProvider()
        viewer._display_space_descriptor = self._descriptor()
        viewer._odt_color_provider = lambda: provider
        uploaded = []
        viewer._upload_odt_lut = lambda data: (
            uploaded.append(data.shape) or 4321)
        viewer._delete_odt_lut_texture = lambda: None

        assert viewer._ensure_odt_lut() is True
        assert provider.calls == [("lin", "srgb")]
        assert uploaded == [(32, 32, 32, 3)]
        assert viewer._odt_lut_tex_id == 4321
        assert viewer._odt_lut_key is not None

        # Cached: a second call must not re-bake or re-upload.
        provider.calls.clear()
        uploaded.clear()
        assert viewer._ensure_odt_lut() is True
        assert provider.calls == []
        assert uploaded == []

    @staticmethod
    def _descriptor():
        from src.shared.color.color_space_descriptor import SRGB_IMAGE
        return SRGB_IMAGE


class TestOdtLutBake:
    def test_bake_grid_layout_and_shape(self, qapp):
        viewer = next(_make_viewer(qapp))
        provider = _FakeOcioProvider()
        data = viewer._bake_odt_lut_data(provider, "srgb")
        assert data.shape == (viewer._ODT_LUT_SIZE,) * 3 + (3,)
        # Marker proves applyRGBA ran on the flat buffer.
        assert np.allclose(data[..., 0], 0.25)
        # glTexImage3D fills texel(x,y,z) from flat[x + y·n + z·n²]; the
        # shader indexes x with the RED input value. After the transpose
        # the RED input grid axis must land on x: the fake leaves green
        # and blue inputs untouched, so blue-OUT (= blue-IN passthrough)
        # tracks the red grid axis (x), and green-OUT tracks y.
        n = viewer._ODT_LUT_SIZE
        assert data[0, 0, 0, 1] == pytest.approx(0.0, abs=1e-6)
        assert data[0, n - 1, 0, 1] == pytest.approx(1.0, abs=1e-6)
        assert data[n - 1, 0, 0, 2] == pytest.approx(1.0, abs=1e-6)
        assert data[0, 0, n - 1, 2] == pytest.approx(0.0, abs=1e-6)


class TestSetDisplaySpaceDescriptor:
    def test_descriptor_stored_and_lut_invalidated(self, qapp):
        from src.shared.color.color_space_descriptor import (
            SRGB_IMAGE, REC709_VIDEO)
        viewer = next(_make_viewer(qapp))
        viewer.set_display_space(SRGB_IMAGE, tonemap=1, target_peak=100.0)
        assert viewer._display_space_descriptor == SRGB_IMAGE
        key = viewer._odt_lut_key = "stale"
        viewer.set_display_space(REC709_VIDEO, tonemap=0)
        # New descriptor invalidates the baked LUT.
        assert viewer._display_space_descriptor == REC709_VIDEO
        assert viewer._odt_lut_key is None
        assert key == "stale"  # local unchanged; just silence linters


def test_display_shader_has_ocio_lut_branch():
    from src.features.composite.ui.composite_viewport import CompositeViewer

    src = inspect.getsource(CompositeViewer.initializeGL)
    assert "u_odt_lut_active" in src
    assert "u_odt_lut_size" in src
    assert "texture(u_odt_lut, lut_coord)" in src
    # Cleanup must release the baked LUT texture.
    cleanup_src = inspect.getsource(CompositeViewer.cleanup)
    assert "_delete_odt_lut_texture" in cleanup_src


def test_odt_sampler_uses_a_dedicated_unit_when_lut_is_unavailable():
    from src.features.composite.ui.composite_viewport import CompositeViewer

    paint_src = inspect.getsource(CompositeViewer.paintGL)
    assert (
        "if lut_loc >= 0:\n"
        "            _gl.glUniform1i(lut_loc, 2)\n"
        "        if odt_ready and lut_loc >= 0:"
    ) in paint_src

    quad_blit_src = inspect.getsource(CompositeViewer._blit_quad_texture)
    assert "_gl.glUniform1i(lut_loc, 2)" in quad_blit_src
    assert "_gl.glUniform1i(odt_active_loc, 0)" in quad_blit_src
