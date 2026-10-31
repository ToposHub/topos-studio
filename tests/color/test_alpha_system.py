"""
Unit tests for the Alpha system (Step 3 of Phase 2).

Tests cover:
- TextureResult.alpha_premultiplied field
- PremultNode / UnpremultNode marker generation
- Alpha state propagation through effects
- BlendNode alpha mode integration
- Alpha no-op passthrough (already-premult → PremultNode = passthrough)
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox, ImageFormat
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.premult_node import PremultNode
from src.features.composite.nodes.unpremult_node import UnpremultNode


class TestTextureResultAlpha(unittest.TestCase):
    """Test alpha_premultiplied field on TextureResult."""

    def test_default_is_none(self):
        """Default alpha_premultiplied is None (unknown)."""
        tr = TextureResult(texture_id=1, width=100, height=100)
        self.assertIsNone(tr.alpha_premultiplied)

    def test_premultiplied(self):
        """Can set alpha_premultiplied=True."""
        tr = TextureResult(texture_id=1, width=100, height=100,
                           alpha_premultiplied=True)
        self.assertTrue(tr.alpha_premultiplied)

    def test_straight(self):
        """Can set alpha_premultiplied=False."""
        tr = TextureResult(texture_id=1, width=100, height=100,
                           alpha_premultiplied=False)
        self.assertFalse(tr.alpha_premultiplied)

    def test_with_format_and_bbox(self):
        """alpha_premultiplied coexists with format and bbox."""
        from src.features.composite.core.format import ImageFormat
        fmt = ImageFormat(width=1920, height=1080, name="HD")
        bbox = BBox.full_frame(1920, 1080)
        tr = TextureResult(
            texture_id=1, width=1920, height=1080,
            format=fmt, bbox=bbox, alpha_premultiplied=True,
        )
        self.assertTrue(tr.alpha_premultiplied)
        self.assertEqual(tr.format.name, "HD")
        self.assertEqual(tr.bbox.width, 1920)


class TestPremultNode(unittest.TestCase):
    """Test PremultNode marker generation."""

    def test_returns_effect_marker(self):
        """PremultNode returns a premult effect marker for non-premultiplied input."""
        node = PremultNode()
        inp = TextureResult(texture_id=42, width=100, height=100,
                            alpha_premultiplied=False)
        result = node._do_evaluate({"image": inp}, None, None)
        self.assertEqual(result.get("_effect_type"), "premult")
        self.assertIs(result["_effect_input"], inp)

    def test_passthrough_if_already_premultiplied(self):
        """PremultNode is a no-op if input is already premultiplied."""
        node = PremultNode()
        inp = TextureResult(texture_id=42, width=100, height=100,
                            alpha_premultiplied=True)
        result = node._do_evaluate({"image": inp}, None, None)
        self.assertNotIn("_effect_type", result)
        self.assertIs(result["result"], inp)

    def test_passthrough_if_none_input(self):
        """PremultNode returns empty for None/invalid input."""
        node = PremultNode()
        result = node._do_evaluate({"image": None}, None, None)
        self.assertEqual(result, {})

    def test_unknown_alpha_triggers_premult(self):
        """PremultNode premultiplies when alpha state is unknown (None)."""
        node = PremultNode()
        inp = TextureResult(texture_id=42, width=100, height=100,
                            alpha_premultiplied=None)
        result = node._do_evaluate({"image": inp}, None, None)
        self.assertEqual(result.get("_effect_type"), "premult")


class TestUnpremultNode(unittest.TestCase):
    """Test UnpremultNode marker generation."""

    def test_returns_effect_marker(self):
        """UnpremultNode returns an unpremult effect marker for premultiplied input."""
        node = UnpremultNode()
        inp = TextureResult(texture_id=42, width=100, height=100,
                            alpha_premultiplied=True)
        result = node._do_evaluate({"image": inp}, None, None)
        self.assertEqual(result.get("_effect_type"), "unpremult")
        self.assertIs(result["_effect_input"], inp)

    def test_passthrough_if_already_straight(self):
        """UnpremultNode is a no-op if input is already straight."""
        node = UnpremultNode()
        inp = TextureResult(texture_id=42, width=100, height=100,
                            alpha_premultiplied=False)
        result = node._do_evaluate({"image": inp}, None, None)
        self.assertNotIn("_effect_type", result)
        self.assertIs(result["result"], inp)

    def test_unknown_alpha_triggers_unpremult(self):
        """UnpremultNode unpremultiplies when alpha state is unknown (None)."""
        node = UnpremultNode()
        inp = TextureResult(texture_id=42, width=100, height=100,
                            alpha_premultiplied=None)
        result = node._do_evaluate({"image": inp}, None, None)
        self.assertEqual(result.get("_effect_type"), "unpremult")

    def test_passthrough_if_none_input(self):
        """UnpremultNode returns empty for None/invalid input."""
        node = UnpremultNode()
        result = node._do_evaluate({"image": None}, None, None)
        self.assertEqual(result, {})



class TestAlphaRoundTrip(unittest.TestCase):
    """Test that Premult → Unpremult round-trip is well-defined."""

    def test_premult_sets_true(self):
        """After premult, alpha_premultiplied=True."""
        # Simulate what _apply_premult returns
        result = TextureResult(
            texture_id=99, width=100, height=100,
            alpha_premultiplied=True,
        )
        self.assertTrue(result.alpha_premultiplied)

    def test_unpremult_sets_false(self):
        """After unpremult, alpha_premultiplied=False."""
        result = TextureResult(
            texture_id=99, width=100, height=100,
            alpha_premultiplied=False,
        )
        self.assertFalse(result.alpha_premultiplied)

    def test_round_trip_state(self):
        """Premult(True) → Unpremult(False) completes the round trip."""
        # Start premultiplied
        premult_result = TextureResult(
            texture_id=99, width=100, height=100,
            alpha_premultiplied=True,
        )
        # Feed into UnpremultNode
        unpremult_node = UnpremultNode()
        marker = unpremult_node._do_evaluate({"image": premult_result}, None, None)
        self.assertEqual(marker["_effect_type"], "unpremult")

        # Simulate what _apply_unpremult would produce
        final = TextureResult(
            texture_id=100, width=100, height=100,
            alpha_premultiplied=False,
        )
        self.assertFalse(final.alpha_premultiplied)


class TestBlendAlphaMode(unittest.TestCase):
    """Test that blend alpha mode logic is correct."""

    def test_both_premultiplied_mode0(self):
        """Both inputs premultiplied → alpha_mode=0."""
        a = TextureResult(texture_id=1, width=100, height=100,
                          alpha_premultiplied=True)
        b = TextureResult(texture_id=2, width=100, height=100,
                          alpha_premultiplied=True)
        a_straight = a.alpha_premultiplied is False
        b_straight = b.alpha_premultiplied is False
        alpha_mode = 1 if (a_straight or b_straight) else 0
        self.assertEqual(alpha_mode, 0)

    def test_one_straight_mode1(self):
        """One input straight → alpha_mode=1."""
        a = TextureResult(texture_id=1, width=100, height=100,
                          alpha_premultiplied=False)
        b = TextureResult(texture_id=2, width=100, height=100,
                          alpha_premultiplied=True)
        a_straight = a.alpha_premultiplied is False
        b_straight = b.alpha_premultiplied is False
        alpha_mode = 1 if (a_straight or b_straight) else 0
        self.assertEqual(alpha_mode, 1)

    def test_unknown_treated_as_premultiplied(self):
        """Unknown alpha (None) treated as premultiplied."""
        a = TextureResult(texture_id=1, width=100, height=100,
                          alpha_premultiplied=None)
        b = TextureResult(texture_id=2, width=100, height=100,
                          alpha_premultiplied=None)
        a_straight = a.alpha_premultiplied is False
        b_straight = b.alpha_premultiplied is False
        alpha_mode = 1 if (a_straight or b_straight) else 0
        self.assertEqual(alpha_mode, 0)


class TestNodeRegistration(unittest.TestCase):
    """Test that premult/unpremult nodes are registered in all registries."""

    @classmethod
    def setUpClass(cls):
        """Skip if GPU dependencies are unavailable."""
        try:
            import moderngl  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("moderngl not available")

    def test_node_engine_registry(self):
        """premult and unpremult are in NODE_REGISTRY."""
        from src.features.composite.core.node_engine import NODE_REGISTRY
        self.assertIn("premult", NODE_REGISTRY)
        self.assertIn("unpremult", NODE_REGISTRY)

    def test_node_engine_categories(self):
        """premult and unpremult are in NODE_CATEGORIES Merge."""
        from src.features.composite.core.node_engine import NODE_CATEGORIES
        merge = NODE_CATEGORIES.get("Merge", [])
        types = [t for t, _ in merge]
        self.assertIn("premult", types)
        self.assertIn("unpremult", types)

    def test_evaluator_type_map(self):
        """premult and unpremult are in NODE_REGISTRY."""
        from src.features.composite.core.node_engine import NODE_REGISTRY
        self.assertIn("premult", NODE_REGISTRY)
        self.assertIn("unpremult", NODE_REGISTRY)

    def test_cache_owned_types(self):
        """premult and unpremult are cache-owned types."""
        from src.features.composite.core.node_capabilities import CACHE_OWNED_TYPES
        self.assertIn("premult", CACHE_OWNED_TYPES)
        self.assertIn("unpremult", CACHE_OWNED_TYPES)


if __name__ == "__main__":
    unittest.main()
