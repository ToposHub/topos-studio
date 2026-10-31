"""Tests for stage-2 HDR/wide-gamut color: PQ/HLG, tonemap, gamut matrix.

Covers the HDR additions to the color-management system:
  - PQ (SMPTE ST 2084) encode/decode round-trips and known reference values.
  - HLG (ARIB STD-B67) encode/decode round-trips.
  - HDR -> SDR tone mapping (Reinhard / ACES) stays in display range.
  - Gamut (primaries) conversion BT.709 <-> BT.2020 <-> DCI-P3 round-trips.
  - ColorSpaceDescriptor HDR/wide-gamut presets + OCIO name mapping.
  - The cs_transform and viewport GLSL shaders contain the new transfer
    functions and uniforms (and compile in a real GL context when available).
"""
import re

import numpy as np
import pytest

from src.shared.color.color_space_descriptor import (
    ColorSpaceDescriptor, HDR10_VIDEO, HLG_VIDEO, DISPLAY_P3_SRGB,
    SRGB_IMAGE, LINEAR_SCENE,
)
from src.shared.color.color_transform_provider import (
    get_color_provider, _pq_to_linear_np, _linear_to_pq_np,
    _hlg_to_linear_np, _linear_to_hlg_np,
    _tonemap_reinhard_np, _tonemap_aces_np, _apply_primaries_np,
)
from src.shared.color.types import ColorPrimaries, ColorTransfer, ColorRange


class TestPQ:
    def test_roundtrip_across_nits(self):
        for nits in (1.0, 100.0, 1000.0, 4000.0, 10000.0):
            lin = np.full((1, 1, 3), nits, dtype=np.float32)
            back = _pq_to_linear_np(_linear_to_pq_np(lin))
            assert abs(back[0, 0, 0] - nits) < 1.0, f"PQ round-trip {nits}nits"

    def test_peak_maps_to_one(self):
        """10000 nits (PQ peak) must encode to PQ code 1.0."""
        pq = _linear_to_pq_np(np.full((1, 1, 3), 10000.0, dtype=np.float32))
        assert abs(pq[0, 0, 0] - 1.0) < 1e-3

    def test_zero_maps_to_near_zero(self):
        """0 nits encodes to a PQ code extremely close to 0 (PQ's m1-power
        of a near-zero value yields a tiny non-zero code)."""
        pq = _linear_to_pq_np(np.zeros((1, 1, 3), dtype=np.float32))
        assert pq[0, 0, 0] < 1e-5

    def test_codes_in_unit_range(self):
        """PQ codes must always land in [0,1]."""
        for nits in (0.0, 0.5, 100.0, 5000.0, 10000.0, 50000.0):
            pq = _linear_to_pq_np(np.full((1, 1, 3), nits, dtype=np.float32))
            assert 0.0 <= pq[0, 0, 0] <= 1.0


class TestHLG:
    def test_roundtrip_across_values(self):
        for L in (0.0, 0.01, 0.05, 0.1, 0.3, 0.5, 0.9, 1.0):
            lin = np.full((1, 1, 3), L, dtype=np.float32)
            back = _hlg_to_linear_np(_linear_to_hlg_np(lin))
            assert abs(back[0, 0, 0] - L) < 1e-3, f"HLG round-trip {L}"

    def test_codes_in_unit_range(self):
        for L in (0.0, 0.5, 1.0, 2.0):
            hlg = _linear_to_hlg_np(np.full((1, 1, 3), L, dtype=np.float32))
            assert 0.0 <= hlg[0, 0, 0] <= 1.0


class TestNaNInfRobustness:
    """M-1: PQ/HLG decode must not propagate NaN/Inf through the pipeline.

    Corrupted HDR pixels (NaN from a broken decoder, +inf from a division)
    would otherwise poison every downstream merge/effect. The decode path
    sanitises them to finite [0,1] values first.
    """

    def test_pq_decode_handles_nan_inf(self):
        bad = np.array([[[np.nan, np.inf, -np.inf, 1.0]]], dtype=np.float32)
        out = _pq_to_linear_np(bad)
        assert np.isfinite(out[..., :3]).all(), "PQ decode left non-finite values"
        assert (out[..., :3] >= 0.0).all(), "PQ decode produced negatives"

    def test_hlg_decode_handles_nan_inf(self):
        bad = np.array([[[np.nan, np.inf, -np.inf, 1.0]]], dtype=np.float32)
        out = _hlg_to_linear_np(bad)
        assert np.isfinite(out[..., :3]).all(), "HLG decode left non-finite values"
        assert (out[..., :3] >= 0.0).all(), "HLG decode produced negatives"


class TestTonemap:
    def test_reinhard_stays_in_display_range(self):
        for nits in (100.0, 1000.0, 10000.0):
            hdr = np.full((1, 1, 3), nits, dtype=np.float32)
            out = _tonemap_reinhard_np(hdr, target_peak=100.0)
            assert 0.0 <= out[0, 0, 0] <= 1.0

    def test_aces_stays_in_display_range(self):
        for nits in (100.0, 1000.0, 10000.0):
            hdr = np.full((1, 1, 3), nits, dtype=np.float32)
            out = _tonemap_aces_np(hdr, target_peak=100.0)
            assert 0.0 <= out[0, 0, 0] <= 1.0

    def test_reinhard_monotonic(self):
        """Higher HDR input maps to higher (or equal) SDR output."""
        lo = _tonemap_reinhard_np(np.full((1, 1, 3), 100.0, dtype=np.float32), 100.0)
        hi = _tonemap_reinhard_np(np.full((1, 1, 3), 1000.0, dtype=np.float32), 100.0)
        assert hi[0, 0, 0] >= lo[0, 0, 0]


class TestGamutMatrix:
    def test_bt709_bt2020_roundtrip(self):
        lin = np.array([[[0.5, 0.3, 0.2]]], dtype=np.float32)
        back = _apply_primaries_np(_apply_primaries_np(lin, "bt709", "bt2020"),
                                   "bt2020", "bt709")
        assert np.allclose(back, lin, atol=1e-4)

    def test_bt709_p3d65_roundtrip(self):
        lin = np.array([[[0.4, 0.6, 0.2]]], dtype=np.float32)
        back = _apply_primaries_np(_apply_primaries_np(lin, "bt709", "smpte432"),
                                   "smpte432", "bt709")
        assert np.allclose(back, lin, atol=1e-4)

    def test_noop_when_equal_or_unknown(self):
        lin = np.array([[[0.5, 0.5, 0.5]]], dtype=np.float32)
        assert np.array_equal(_apply_primaries_np(lin, "bt709", "bt709"), lin)
        assert np.array_equal(_apply_primaries_np(lin, "bt709", "unknown"), lin)


class TestHDRDescriptor:
    def test_hdr_presets(self):
        assert HDR10_VIDEO.primaries == ColorPrimaries.BT2020
        assert HDR10_VIDEO.transfer == ColorTransfer.PQ
        assert HLG_VIDEO.transfer == ColorTransfer.HLG
        assert DISPLAY_P3_SRGB.primaries == ColorPrimaries.P3_DISPLAY

    def test_ocio_name_mapping(self):
        assert HDR10_VIDEO.to_ocio_name() == "bt2020_pq"
        assert HLG_VIDEO.to_ocio_name() == "bt2020_hlg"
        assert DISPLAY_P3_SRGB.to_ocio_name() == "display_p3"

    def test_serialization(self):
        for d in (HDR10_VIDEO, HLG_VIDEO, DISPLAY_P3_SRGB):
            assert ColorSpaceDescriptor.from_dict(d.to_dict()) == d


class TestHDRPipeline:
    """End-to-end transform via the active provider (builtin when no OCIO)."""

    def setup_method(self):
        self.provider = get_color_provider(None)

    def test_hdr_to_srgb_preview_in_range(self):
        """BT.2020+PQ source -> sRGB preview stays in display range."""
        pq = np.full((2, 2, 3), 0.75, dtype=np.float32)  # ~1000 nits
        out = self.provider.transform(pq, HDR10_VIDEO, SRGB_IMAGE)
        assert 0.0 <= out[0, 0, 0] <= 1.0


class TestShaderContents:
    """The GPU shaders must contain the stage-2 transfer functions/uniforms."""

    def test_cs_transform_shader_has_pq_hlg(self):
        src = open("src/features/composite/core/shaders.py").read()
        m = re.search(r'_CS_TRANSFORM_FRAGMENT = """(.*?)"""', src, re.S)
        assert m, "cs_transform shader not found"
        frag = m.group(1)
        for fn in ("pq_to_linear", "linear_to_pq",
                   "hlg_to_linear", "linear_to_hlg"):
            assert fn in frag, f"missing {fn}"
        # u_transfer IDs 3 (PQ) and 4 (HLG) must be selectable.
        assert "u_transfer == 3" in frag
        assert "u_transfer == 4" in frag
        # H-3: the shader must expose a u_primaries uniform + gamut conversion
        # so the IDT can convert wide-gamut HDR sources to BT.709 working space.
        assert "u_primaries" in frag, "cs_transform missing u_primaries uniform"
        assert "to_working_gamut" in frag
        assert "from_working_gamut" in frag
        assert "BT709_TO_BT2020" in frag
        assert "BT709_TO_P3D65" in frag

    def test_viewport_shader_has_hdr_odt(self):
        with open("src/features/composite/ui/composite_viewport.py", "rb") as f:
            src = f.read()
        m = re.search(rb'frag_src = b"""(.*?)"""', src, re.DOTALL)
        assert m, "viewport shader not found"
        frag = m.group(1).decode("utf-8")
        for u in ("u_display_primaries", "u_tonemap", "u_target_peak",
                  "linear_to_pq_vec3", "tonemap_reinhard", "tonemap_aces",
                  "BT709_TO_BT2020", "BT709_TO_P3D65"):
            assert u in frag, f"missing {u}"
        assert "u_display_transfer == 3" in frag  # PQ display
        # H-5: HLG display encode now uses the native u_display_transfer==4
        # path (not a workaround to sRGB), and the HLG OETF function exists.
        assert "u_display_transfer == 4" in frag
        assert "linear_to_hlg_vec3" in frag

    def test_viewport_gamut_matrices_are_column_major(self):
        """C-1: GLSL mat3() fills COLUMN-major; the BT709->BT2020 matrix must
        be written so each ORIGINAL ROW is a COLUMN, otherwise the viewport
        applies the transposed gamut matrix (visible hue/saturation error).

        The row-major BT709->BT2020 matrix has off-diagonal entries that are
        clearly asymmetric (e.g. row0/col1 = 0.329 but row1/col0 = 0.069), so
        we assert the first three emitted values equal the original row 0.
        """
        with open("src/features/composite/ui/composite_viewport.py", "rb") as f:
            src = f.read()
        start = src.find(b"BT709_TO_BT2020 = mat3(")
        assert start != -1, "BT709_TO_BT2020 not found"
        # Grab up to the closing paren of this mat3().
        end = src.find(b");", start)
        block = src[start:end]
        nums = re.findall(rb"-?\d+\.\d+", block)
        # First three numeric literals are column 0; they must equal original
        # row 0: [0.6274040, 0.0690970, 0.0163916].
        assert len(nums) >= 9, f"expected >=9 matrix entries, got {len(nums)}"
        col0 = [float(x) for x in nums[:3]]
        assert abs(col0[0] - 0.6274040) < 1e-4, f"col0[0]={col0[0]} should be 0.6274040"
        assert abs(col0[1] - 0.0690970) < 1e-4, f"col0[1]={col0[1]} should be 0.0690970"
        assert abs(col0[2] - 0.0163916) < 1e-4, f"col0[2]={col0[2]} should be 0.0163916"

    def test_viewport_shader_compiles_as_glsl(self):
        """Compile the viewport display shader in a real GL 3.3 context."""
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as e:
            pytest.skip(f"Cannot create standalone GL context: {e}")
        with open("src/features/composite/ui/composite_viewport.py", "rb") as f:
            src = f.read()
        frag = re.search(rb'frag_src = b"""(.*?)"""', src, re.DOTALL).group(1).decode()
        vert = """
#version 330
out vec2 v_uv;
void main() {
    vec2 p = vec2((gl_VertexID == 1) ? 3.0 : -1.0, (gl_VertexID == 2) ? 3.0 : -1.0);
    gl_Position = vec4(p, 0.0, 1.0);
    v_uv = gl_Position.xy * 0.5 + 0.5;
}
"""
        prog = ctx.program(vertex_shader=vert, fragment_shader=frag)
        uniforms = set(prog)
        for u in ("u_display_transfer", "u_display_primaries",
                  "u_tonemap", "u_target_peak"):
            assert u in uniforms, f"shader missing uniform {u}"
        prog.release()
        ctx.release()

    def test_cs_transform_shader_compiles_as_glsl(self):
        """Compile the cs_transform shader in a real GL 3.3 context."""
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as e:
            pytest.skip(f"Cannot create standalone GL context: {e}")
        src = open("src/features/composite/core/shaders.py").read()
        frag = re.search(r'_CS_TRANSFORM_FRAGMENT = """(.*?)"""', src, re.S).group(1)
        vert = """
#version 330
out vec2 v_uv;
void main() {
    vec2 p = vec2((gl_VertexID == 1) ? 3.0 : -1.0, (gl_VertexID == 2) ? 3.0 : -1.0);
    gl_Position = vec4(p, 0.0, 1.0);
    v_uv = gl_Position.xy * 0.5 + 0.5;
}
"""
        prog = ctx.program(vertex_shader=vert, fragment_shader=frag)
        uniforms = set(prog)
        assert "u_transfer" in uniforms
        assert "u_primaries" in uniforms, "cs_transform shader missing u_primaries"
        prog.release()
        ctx.release()
