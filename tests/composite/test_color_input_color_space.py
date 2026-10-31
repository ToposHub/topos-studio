"""
Tests for color_input (COLORNode) color-space tagging.

Background:
    color_input bridges the Color page's ColorOutput texture into the
    composite graph. The Color page neither decodes nor encodes that
    texture for a normal sRGB image with the RAW module off
    (u_is_srgb_source=False, u_raw_gamma=0 — a pure bypass). The pixels
    therefore keep the sRGB encoding they had on disk.

    COLORNode must therefore tag its output as SRGB (the truth), matching
    the composite engine's working-space contract (color_space.py: a
    source node tags the space its pixels are *actually* in, and the
    unified IDT hook decodes sRGB→linear once at the graph boundary).

    A previous LINEAR tag skipped that IDT decode, so the sRGB values
    reached the viewer still sRGB-encoded and the viewer's ODT then
    encoded them a SECOND time → double gamma → the composite page
    looked noticeably brighter than the Color page. Tagging SRGB lets
    the IDT+ODT pair cancel into a correct single round trip.

    The merge-darkening concern that once motivated the LINEAR tag is
    already handled by _auto_align_color_spaces(), which decodes every
    sRGB merge input to linear (including the both-sRGB case), so an
    SRGB-tagged color_input merging with a LINEAR Constant aligns
    correctly in linear light.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.core.color_space import ColorSpace

pytestmark = pytest.mark.composite_core


class TestColorInputColorSpace:
    """color_input must tag its output with the pixels' true space: SRGB.

    The Color page's ColorOutput holds sRGB-encoded (gamma-domain) values
    (RAW off → pure bypass), so the tag must reflect that. The composite
    IDT then decodes it to linear once at the graph boundary.
    """

    def test_output_color_space_is_srgb(self):
        """COLORNode output must be tagged SRGB.

        The Color page's ColorOutput is written through a passthrough
        shader (async_renderer.py) and, for non-RAW images, runs the
        bypass path with gamma=0 (image_engine.py:289-295) — i.e. it
        neither decodes nor encodes, so the texture keeps the sRGB
        encoding the pixels had on disk. The node tag must match the
        actual data so the IDT hook decodes it correctly.
        """
        node = COLORNode(node_id="color_1")
        node.set_color_result(texture_id=42, width=1920, height=1080,
                              revision=1)

        result = node._do_evaluate(inputs={}, ctx=None, fb_manager=None)
        out = result["result"]

        assert out.color_space == ColorSpace.SRGB, (
            "color_input must tag its output as SRGB to match the Color "
            "page's sRGB-encoded ColorOutput. A LINEAR tag skips the IDT "
            "sRGB→linear decode, so the viewer's ODT encodes the still-sRGB "
            "values a second time → double gamma → composite page too bright."
        )

    def test_output_color_space_not_linear(self):
        """Explicit guard against the regression: tag must not be LINEAR."""
        node = COLORNode(node_id="color_1")
        node.set_color_result(texture_id=7, width=10, height=10)

        result = node._do_evaluate(inputs={}, ctx=None, fb_manager=None)
        out = result["result"]

        assert out.color_space != ColorSpace.LINEAR, (
            "color_input tag regressed to LINEAR — this skips the IDT "
            "sRGB→linear decode and the viewer ODT double-encodes the "
            "sRGB data, brightening the composite page."
        )

    def test_constant_color_space_is_linear(self):
        """Constant (a typical merge partner) is LINEAR by contract.

        When color_input (SRGB) merges with a LINEAR Constant,
        _auto_align_color_spaces() decodes the sRGB side to linear so
        both inputs meet in linear light — no spurious conversion on the
        already-linear Constant.
        """
        node = ConstantNode(node_id="const_1")
        # Force a GPU-free path: just inspect the declared tag contract.
        # The node sets color_space=LINEAR in _do_evaluate; verify the
        # source of truth (PARAM_DEFS comment + the node's stated contract).
        from src.features.composite.nodes.constant_node import ConstantNode as _C
        # The constant contract: "Constant always generates pixels in
        # scene-linear space" (constant_node.py). We assert via the class
        # that it does not claim sRGB.
        source = open(_C.__module__ and _C.__init__.__code__.co_filename).read()
        assert "ColorSpace.LINEAR" in source
        assert "ColorSpace.SRGB" not in source


class TestMergeColorSpaceAlignment:
    """The merge auto-alignment normalises inputs to a common linear space.

    These guard _auto_align_color_spaces()'s branching: two LINEAR inputs
    are a no-op, but any sRGB input (including the now-correctly-tagged
    color_input) is decoded to linear so the merge math runs in linear
    light. This is what makes an SRGB color_input merging with a LINEAR
    Constant safe — the sRGB side is decoded, not the linear side.
    """

    def test_linear_inputs_require_no_conversion(self):
        """_auto_align_color_spaces must return inputs unchanged when both
        are LINEAR."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        # The mixin is heavy to instantiate (needs a GL context). We only
        # exercise the pure-Python branching logic by calling the unbound
        # method on a dummy self, avoiding __init__ entirely.
        align = BaseEvaluatorMixin._auto_align_color_spaces

        class _FakeSelf:
            pass

        a = TextureResult(texture_id=1, width=2, height=2,
                          color_space=ColorSpace.LINEAR)
        b = TextureResult(texture_id=2, width=2, height=2,
                          color_space=ColorSpace.LINEAR)

        out_a, out_b = align(_FakeSelf(), a, b)

        # Same objects returned (no conversion path taken)
        assert out_a is a
        assert out_b is b

    def test_srgb_inputs_both_get_decoded_to_linear(self):
        """Two sRGB inputs must BOTH be decoded to linear before merge.

        Regression guard: previously the early ``cs_a == cs_b`` return let
        two sRGB inputs pass through untouched, so the merge over-formula ran
        on gamma-encoded values and darkened semi-transparent merges. Now both
        sRGB inputs are decoded (via _convert_to_linear) so the merge math is
        correct in linear light.
        """
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        align = BaseEvaluatorMixin._auto_align_color_spaces

        converted = []

        class _FakeSelf:
            def _convert_to_linear(self, tex, transfer="srgb", **kwargs):
                # Record the conversion and return a new LINEAR-tagged tex.
                converted.append(tex.texture_id)
                return TextureResult(texture_id=tex.texture_id,
                                     width=tex.width, height=tex.height,
                                     color_space=ColorSpace.LINEAR)

        a = TextureResult(texture_id=1, width=2, height=2,
                          color_space=ColorSpace.SRGB)
        b = TextureResult(texture_id=2, width=2, height=2,
                          color_space=ColorSpace.SRGB)

        out_a, out_b = align(_FakeSelf(), a, b)

        # Both inputs were decoded.
        assert converted == [1, 2]
        # Both outputs are now tagged linear.
        assert out_a.color_space == ColorSpace.LINEAR
        assert out_b.color_space == ColorSpace.LINEAR

    def test_mixed_srgb_linear_still_decodes_srgb_side(self):
        """Mixed inputs (one sRGB, one linear) must decode only the sRGB one.

        The mixed-space case was already handled correctly before the fix;
        this test guards that the new both-sRGB branch did not regress it.
        """
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        align = BaseEvaluatorMixin._auto_align_color_spaces
        converted = []

        class _FakeSelf:
            def _convert_to_linear(self, tex, transfer="srgb", **kwargs):
                converted.append(tex.texture_id)
                return TextureResult(texture_id=tex.texture_id,
                                     width=tex.width, height=tex.height,
                                     color_space=ColorSpace.LINEAR)

        a = TextureResult(texture_id=1, width=2, height=2,
                          color_space=ColorSpace.SRGB)
        b = TextureResult(texture_id=2, width=2, height=2,
                          color_space=ColorSpace.LINEAR)

        out_a, out_b = align(_FakeSelf(), a, b)

        # Only the sRGB input was decoded.
        assert converted == [1]
        assert out_a.color_space == ColorSpace.LINEAR
        # Linear input returned unchanged.
        assert out_b is b


class TestIDTHook:
    """Tests for the unified IDT hook (_apply_idt_to_source_outputs).

    This is the central colour-management hook (stage 1): every source
    node's output is normalised to scene-linear as it enters the graph.
    The hook reuses _convert_to_linear (the same GPU sRGB->linear pass
    used by merge's auto-alignment), so it is tested here with a mock
    self that records conversions — mirroring the TestMergeColorSpaceAlignment
    pattern above.
    """

    def _make_self(self):
        """Build a fake evaluator self with a recording _convert_to_linear."""
        from src.features.composite.nodes.base_node import TextureResult
        converted = []

        class _FakeSelf:
            def _convert_to_linear(self_, tex, transfer="srgb", **kwargs):
                converted.append(tex.texture_id)
                return TextureResult(
                    texture_id=tex.texture_id,
                    width=tex.width, height=tex.height,
                    color_space=ColorSpace.LINEAR,
                )

        return _FakeSelf(), converted

    def test_srgb_source_output_is_decoded_to_linear(self):
        """An sRGB source output must be decoded to linear by the IDT hook."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        idt = BaseEvaluatorMixin._apply_idt_to_source_outputs
        fake, converted = self._make_self()

        tex = TextureResult(texture_id=7, width=10, height=10,
                            color_space=ColorSpace.SRGB)
        out = idt(fake, {"image": tex})

        assert converted == [7]
        assert out["image"].color_space == ColorSpace.LINEAR

    def test_linear_source_output_is_passed_through(self):
        """A linear source output must not be re-converted (returns same object)."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        idt = BaseEvaluatorMixin._apply_idt_to_source_outputs
        fake, converted = self._make_self()

        tex = TextureResult(texture_id=7, width=10, height=10,
                            color_space=ColorSpace.LINEAR)
        out = idt(fake, {"image": tex})

        # No conversion happened.
        assert converted == []
        # The same object is returned (no unnecessary copy).
        assert out is not None
        assert out["image"] is tex

    def test_unknown_color_space_is_treated_as_linear(self):
        """UNKNOWN / None color_space is left alone (assumed already-linear).

        Generators like Constant/Ramp/Noise produce linear data but may tag
        it UNKNOWN (or None). The IDT must not re-encode them.
        """
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        idt = BaseEvaluatorMixin._apply_idt_to_source_outputs
        fake, converted = self._make_self()

        for cs in (ColorSpace.UNKNOWN, None):
            tex = TextureResult(texture_id=9, width=4, height=4,
                                color_space=cs)
            out = idt(fake, {"image": tex})
            assert converted == [], f"UNKNOWN/None ({cs!r}) should not convert"
            assert out["image"] is tex

    def test_mixed_outputs_only_converts_srgb_ones(self):
        """A source with multiple outputs converts only the sRGB ones."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        idt = BaseEvaluatorMixin._apply_idt_to_source_outputs
        fake, converted = self._make_self()

        srgb_tex = TextureResult(texture_id=1, width=4, height=4,
                                 color_space=ColorSpace.SRGB)
        linear_tex = TextureResult(texture_id=2, width=4, height=4,
                                   color_space=ColorSpace.LINEAR)
        out = idt(fake, {"a": srgb_tex, "b": linear_tex})

        assert converted == [1]
        assert out["a"].color_space == ColorSpace.LINEAR
        assert out["b"] is linear_tex

    def test_empty_and_marker_outputs_pass_through(self):
        """Empty dict and non-TextureResult marker entries pass through."""
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        idt = BaseEvaluatorMixin._apply_idt_to_source_outputs
        fake, converted = self._make_self()

        # Empty outputs.
        assert idt(fake, {}) == {}

        # Outputs containing non-TextureResult marker entries (like
        # _effect_type strings) are left untouched.
        marker_outputs = {"_effect_type": "noise", "_effect_params": {}}
        out = idt(fake, marker_outputs)
        assert out["_effect_type"] == "noise"
        assert converted == []

    def test_conversion_failure_falls_back_gracefully(self):
        """If _convert_to_linear raises, the IDT must not kill evaluation.

        It should pass the unconverted texture through and log a warning,
        so a colour-space glitch never blanks the whole viewport.
        """
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

        idt = BaseEvaluatorMixin._apply_idt_to_source_outputs

        class _FailingSelf:
            def _convert_to_linear(self_, tex, transfer="srgb", **kwargs):
                raise RuntimeError("GPU sRGB->linear pass failed")

        tex = TextureResult(texture_id=5, width=8, height=8,
                            color_space=ColorSpace.SRGB)
        out = idt(_FailingSelf(), {"image": tex})

        # The original sRGB texture is passed through unconverted.
        assert out["image"] is tex
        assert out["image"].color_space == ColorSpace.SRGB


class TestViewerODT:
    """Tests for the viewer display transform (linear -> sRGB ODT).

    The ODT lives in composite_viewport.py's display shader. We can't
    instantiate the QOpenGLWidget without a running Qt app, so these
    tests verify the shader source string and the reverse-transform
    math directly.
    """

    def test_display_shader_contains_srgb_encode(self):
        """The viewer fragment shader must contain a linear->sRGB encode.

        The ODT now uses a u_display_transfer int (0=linear, 1=sRGB,
        2=Rec.709) instead of the old u_display_srgb bool, but the sRGB
        encode function and the transfer-selection logic must still be
        present.
        """
        import re
        with open('src/features/composite/ui/composite_viewport.py', 'rb') as f:
            src = f.read()
        m = re.search(rb'frag_src = b"""(.*?)"""', src, re.DOTALL)
        assert m, "frag_src byte string not found"
        frag = m.group(1).decode('utf-8')

        # The ODT function and the new transfer-selection uniform must exist.
        assert 'linear_to_srgb_vec3' in frag, "missing linear_to_srgb ODT function"
        assert 'u_display_transfer' in frag, "missing u_display_transfer uniform"
        assert 'u_display_transfer == 1' in frag, "ODT must select sRGB at transfer==1"

    def test_display_shader_compiles_as_glsl(self):
        """The viewer display shader must compile in a real GL 3.3 context."""
        import re
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as e:
            pytest.skip(f"Cannot create standalone GL context: {e}")

        with open('src/features/composite/ui/composite_viewport.py', 'rb') as f:
            src = f.read()
        m = re.search(rb'frag_src = b"""(.*?)"""', src, re.DOTALL)
        frag = m.group(1).decode('utf-8')

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
        assert 'u_display_transfer' in uniforms
        assert 'u_exposure' in uniforms
        assert 'u_viewer_gamma' in uniforms
        prog.release()
        ctx.release()

    def test_viewer_has_display_srgb_state_and_setter(self):
        """CompositeViewer must declare _display_srgb (default True) + setter.

        Verified via source inspection (instantiating the widget needs Qt).
        """
        with open('src/features/composite/ui/composite_viewport.py') as f:
            src = f.read()
        assert 'self._display_srgb = True' in src, \
            "_display_srgb must default to True (ODT on by default)"
        assert 'def set_display_srgb' in src, "missing set_display_srgb setter"

    def test_inspector_reads_linear_float_from_texture(self):
        """The pixel inspector must read raw linear float values from the
        owned RGBA16F texture, not the 8-bit display framebuffer.

        This replaces the old _reverse_display_transform approach (which
        approximated linear values by inverting the display shader on 8-bit
        framebuffer reads). The new path does a direct float readback from
        the texture, so it sees true HDR values (>1.0) without clamping.

        Verified via source inspection (instantiating the widget needs Qt).
        """
        with open('src/features/composite/ui/composite_viewport.py') as f:
            src = f.read()
        # The new float-readback method must exist.
        assert '_read_linear_pixel_from_texture' in src, \
            "inspector must read linear float from owned texture"
        assert 'GL_FLOAT' in src and 'glReadPixels' in src, \
            "inspector must use float glReadPixels"
        # The old reverse-transform method must be gone (it approximated
        # linear values from 8-bit display reads).
        assert '_reverse_display_transform' not in src, \
            "_reverse_display_transform should be removed (direct float readback replaces it)"
        # The inspector update path must prefer the texture readback.
        assert '_read_linear_pixel_from_texture(uv[0], uv[1]' in src, \
            "inspector must call _read_linear_pixel_from_texture with UV coords"

    def test_inspector_float_readback_preserves_hdr_values(self):
        """A standalone GL test: upload an HDR linear texture (>1.0), read a
        pixel back via the same GL_FLOAT readback the inspector uses, and
        confirm the HDR value survives (>1.0, not clamped to 8-bit).

        This proves the float-readback path can see HDR data that the old
        8-bit framebuffer path would have lost.
        """
        try:
            import moderngl
            import numpy as np
            ctx = moderngl.create_standalone_context()
        except Exception as e:
            pytest.skip(f"Cannot create standalone GL context: {e}")

        # 2x2 RGBA16F texture with an HDR pixel (value 4.0, well above 1.0).
        tex_data = np.array([
            [4.0, 4.0, 4.0, 1.0],  # HDR pixel
            [0.5, 0.5, 0.5, 1.0],
            [0.18, 0.18, 0.18, 1.0],
            [0.0, 0.0, 0.0, 1.0],
        ], dtype='f2').tobytes()  # f2 = half float (RGBA16F)
        tex = ctx.texture((2, 2), 4, tex_data, dtype='f2')
        tex.filter = (moderngl.NEAREST, moderngl.NEAREST)

        # Attach to an FBO and read one pixel as float.
        fbo = ctx.framebuffer([tex])
        fbo.use()
        raw = fbo.read(components=4, dtype='f4', alignment=1)
        pixels = np.frombuffer(raw, dtype='f4')
        # moderngl reads bottom-to-top; pixel (0,0) is our HDR pixel.
        hdr_r = float(pixels[0])
        tex.release()
        fbo.release()
        ctx.release()

        # The HDR value must survive (>1.0). An 8-bit read would clamp to 1.0.
        assert hdr_r > 1.0, \
            f"float readback must preserve HDR value (4.0), got {hdr_r} (8-bit read would clamp to 1.0)"
        assert abs(hdr_r - 4.0) < 0.01, \
            f"float readback must recover 4.0 accurately, got {hdr_r}"


class TestExportColorSpaceAfterIDT:
    """Export-path regression: after the stage-1 IDT, source data is linear,
    so file_output's _prepare_for_export must apply linear→sRGB encode when
    exporting to an sRGB format (PNG/JPEG).

    Before stage 1, source data was tagged SRGB and passed through raw, so a
    linear 0.18 value was written as ~45/255 (way too dark). With the IDT
    in place, the same value is correctly encoded to ~117/255.
    """

    def _make_linear_gray(self, linear_value: float = 0.18):
        """Build a (4,4,4) float32 RGBA image at a constant linear value."""
        import numpy as np
        img = np.full((4, 4, 4), linear_value, dtype='f4')
        img[:, :, 3] = 1.0  # opaque
        return img

    def _expected_srgb_8bit(self, linear_value: float) -> int:
        """Compute the expected 8-bit sRGB-encoded value for a linear input."""
        if linear_value <= 0.0031308:
            srgb = linear_value * 12.92
        else:
            srgb = 1.055 * (max(linear_value, 0.0) ** (1.0 / 2.4)) - 0.055
        return int(round(srgb * 255))

    def test_linear_source_exports_png_with_srgb_encode(self):
        """PNG export of linear data must apply linear→sRGB (auto→sRGB)."""
        import numpy as np
        from src.features.composite.nodes.file_output_node import FileOutputNode
        from src.features.composite.nodes.base_node import TextureResult

        node = FileOutputNode()
        node.parameters["format"] = "PNG"
        node.parameters["color_space"] = "auto"
        node.parameters["channels"] = "RGB"
        node.parameters["bit_depth"] = "8"

        linear_value = 0.18
        src = TextureResult(texture_id=1, width=4, height=4,
                            color_space=ColorSpace.LINEAR)
        out = node._prepare_for_export(self._make_linear_gray(linear_value), src)

        assert out.dtype == np.uint8
        expected = self._expected_srgb_8bit(linear_value)
        # out is BGR; check the green channel (index 1).
        actual = int(out[0, 0, 1])
        assert abs(actual - expected) <= 1, \
            f"PNG export of linear {linear_value} should encode to ~{expected}/255, " \
            f"got {actual}. (Raw linear passthrough would be ~{int(linear_value*255)}.)"

    def test_linear_source_exports_exr_unchanged(self):
        """EXR export must preserve linear data (no encode)."""
        import numpy as np
        from src.features.composite.nodes.file_output_node import FileOutputNode
        from src.features.composite.nodes.base_node import TextureResult

        node = FileOutputNode()
        node.parameters["format"] = "EXR"
        node.parameters["color_space"] = "auto"
        node.parameters["channels"] = "RGBA"
        node.parameters["bit_depth"] = "32"

        linear_value = 0.18
        src = TextureResult(texture_id=1, width=4, height=4,
                            color_space=ColorSpace.LINEAR)
        out = node._prepare_for_export(self._make_linear_gray(linear_value), src)

        assert out.dtype == np.float32
        # Linear value preserved (no encode). EXR is BGR(A); check green.
        actual = float(out[0, 0, 1])
        assert abs(actual - linear_value) < 0.001, \
            f"EXR export must preserve linear value {linear_value}, got {actual}"

    def test_srgb_tagged_source_still_converts_on_export(self):
        """Even if a source is still tagged SRGB (legacy/edge case), exporting
        to PNG with auto→sRGB is a no-op (src==target) — data passes through.

        This guards the defensive path: if the IDT somehow didn't run, the
        export still produces a sane (if not colour-managed) result.
        """
        import numpy as np
        from src.features.composite.nodes.file_output_node import FileOutputNode
        from src.features.composite.nodes.base_node import TextureResult

        node = FileOutputNode()
        node.parameters["format"] = "PNG"
        node.parameters["color_space"] = "auto"
        node.parameters["channels"] = "RGB"
        node.parameters["bit_depth"] = "8"

        # An sRGB-encoded mid-gray (0.5) tagged as SRGB.
        srgb_value = 0.5
        img = np.full((4, 4, 4), srgb_value, dtype='f4')
        img[:, :, 3] = 1.0
        src = TextureResult(texture_id=1, width=4, height=4,
                            color_space=ColorSpace.SRGB)
        out = node._prepare_for_export(img, src)

        # src=sRGB, target=sRGB → no conversion → value passes through.
        actual = int(out[0, 0, 1])
        expected = int(round(srgb_value * 255))
        assert abs(actual - expected) <= 1, \
            f"sRGB→sRGB export should pass through {srgb_value}→~{expected}, got {actual}"
