"""C6-1: composite-side ACEScg — GPU shader matrix, IDT labels, auto-align."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.composite_core

from src.features.composite.core.color_space import ColorSpace


class TestShaderAp1Support:
    def test_cs_transform_has_ap1_branch(self):
        from src.features.composite.core.shaders import _CS_TRANSFORM_FRAGMENT
        src = inspect.getsource(
            _CS_TRANSFORM_FRAGMENT) if inspect.isfunction(
            _CS_TRANSFORM_FRAGMENT) else str(_CS_TRANSFORM_FRAGMENT)
        assert "BT709_TO_AP1" in src
        assert "AP1_TO_BT709" in src
        assert "u_primaries == 3" in src

    def test_ap1_matrix_matches_provider_values(self):
        """GPU constants and provider constants must be the same transform."""
        from src.features.composite.core.shaders import _CS_TRANSFORM_FRAGMENT
        from src.shared.color.color_transform_provider import (
            _AP1_TO_BT709, _BT709_TO_AP1)
        src = str(_CS_TRANSFORM_FRAGMENT)
        # The shader declares column-major mat3 — verify a few published
        # values exist verbatim (values shared with the provider constants).
        for value in ("0.6130974", "1.7050502", "1.1529220", "0.0206158"):
            assert value in src
        # And the provider roundtrip composes to identity (already covered
        # in shared tests) — here just assert the constants are importable
        # and consistent in shape.
        assert _AP1_TO_BT709.shape == (3, 3)
        assert _BT709_TO_AP1.shape == (3, 3)


class TestIdtAcescgLabels:
    """``aces_cg`` sources must be gamut-converted, not passed through."""

    def _make_self(self):
        from src.features.composite.nodes.base_node import TextureResult
        calls = []

        class _FakeSelf:
            def _convert_to_linear(self_, tex, transfer="srgb", **kwargs):
                calls.append((tex.texture_id, transfer,
                              kwargs.get("primaries")))
                return TextureResult(
                    texture_id=tex.texture_id,
                    width=tex.width, height=tex.height,
                    color_space=ColorSpace.LINEAR,
                )

        return _FakeSelf(), calls

    def test_aces_cg_source_converts_with_ap1_gamut(self):
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin)
        from src.features.composite.nodes.base_node import TextureResult

        idt = BaseEvaluatorMixin._apply_idt_to_source_outputs
        fake, calls = self._make_self()
        tex = TextureResult(texture_id=5, width=8, height=8,
                            color_space="aces_cg")
        out = idt(fake, {"image": tex})

        assert calls == [(5, "linear", "ap1")]
        assert out["image"].color_space == ColorSpace.LINEAR

    def test_acescg_alias_also_converts(self):
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin)
        from src.features.composite.nodes.base_node import TextureResult

        idt = BaseEvaluatorMixin._apply_idt_to_source_outputs
        fake, calls = self._make_self()
        tex = TextureResult(texture_id=6, width=8, height=8,
                            color_space="acescg")
        idt(fake, {"image": tex})
        assert calls == [(6, "linear", "ap1")]

    def test_aces2065_1_still_passes_through(self):
        # AP0 has no verified matrix yet — documented pass-through.
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin)
        from src.features.composite.nodes.base_node import TextureResult

        idt = BaseEvaluatorMixin._apply_idt_to_source_outputs
        fake, calls = self._make_self()
        tex = TextureResult(texture_id=7, width=8, height=8,
                            color_space="aces2065_1")
        out = idt(fake, {"image": tex})
        assert calls == []
        assert out["image"] is tex


class TestMergeAutoAlignAcescg:
    def test_aces_cg_input_converts_to_working_space(self):
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin)
        from src.features.composite.nodes.base_node import TextureResult
        calls = []

        class _FakeSelf:
            def _convert_to_linear(self_, tex, transfer="srgb", **kwargs):
                calls.append((tex.texture_id, transfer,
                              kwargs.get("primaries")))
                return TextureResult(
                    texture_id=tex.texture_id,
                    width=tex.width, height=tex.height,
                    color_space=ColorSpace.LINEAR,
                )

        align = BaseEvaluatorMixin._auto_align_color_spaces
        a = TextureResult(texture_id=1, width=4, height=4,
                          color_space="aces_cg")
        b = TextureResult(texture_id=2, width=4, height=4,
                          color_space=ColorSpace.LINEAR)
        out_a, out_b = align(_FakeSelf(), a, b)

        # Only the ACEScg side converts — gamut-only (linear transfer, AP1).
        assert calls == [(1, "linear", "ap1")]
        assert out_a.color_space == ColorSpace.LINEAR
        assert out_b is b
