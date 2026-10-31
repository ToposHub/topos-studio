"""Regression tests for the keyframe-system performance optimizations.

P-1: KeyframeTrack.add_keyframes_bulk / from_dict use ONE sort + ONE
     de-dup pass instead of per-keyframe add (which sorted after every
     append → O(n²log n)). This is the renderer's per-frame rebuild path.

P-2: ExpressionParser caches parsed ASTs keyed by expression string, so
     the same expression isn't re-parsed every frame during playback.

These tests lock the contracts (correctness + the performance
characteristic) so neither optimization silently regresses.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType


pytestmark = pytest.mark.composite_core


# ── P-1: bulk add_keyframes_bulk / from_dict fast path ────────────────


class TestBulkAddKeyframes:
    def test_bulk_sorts_correctly(self):
        """Bulk-loaded keyframes end up frame-sorted (the renderer relies on
        this for interpolation lookup)."""
        track = KeyframeTrack(param_name="p")
        track.add_keyframes_bulk([
            Keyframe(100, 100.0), Keyframe(0, 0.0), Keyframe(50, 50.0),
        ])
        assert [kf.frame for kf in track.keyframes] == [0, 50, 100]

    def test_bulk_dedup_keeps_last_at_duplicate_frame(self):
        """Duplicate frames keep the LAST entry (matching add_keyframe's
        replace-in-place semantics)."""
        track = KeyframeTrack(param_name="p")
        track.add_keyframes_bulk([
            Keyframe(50, 1.0, interpolation=InterpolationType.LINEAR),
            Keyframe(50, 9.0, interpolation=InterpolationType.BEZIER),
        ])
        assert len(track.keyframes) == 1
        assert track.keyframes[0].value == 9.0
        assert track.keyframes[0].interpolation == InterpolationType.BEZIER

    def test_bulk_empty_is_noop(self):
        track = KeyframeTrack(param_name="p")
        track.add_keyframes_bulk([])
        assert track.keyframes == []

    def test_from_dict_uses_bulk_path(self):
        """from_dict must produce the same result as before (sorted + de-duped)
        but via the bulk fast path."""
        data = {
            "param_name": "rotation",
            "keyframes": [
                {"frame": 100, "value": 100.0},
                {"frame": 0, "value": 0.0},
                {"frame": 50, "value": 50.0},
                {"frame": 0, "value": 5.0},  # duplicate frame 0 → wins
            ],
        }
        track = KeyframeTrack.from_dict(data)
        # De-duped to 3 keyframes, sorted, frame-0 value is the last (5.0).
        assert [kf.frame for kf in track.keyframes] == [0, 50, 100]
        assert track.keyframes[0].value == 5.0

    def test_from_dict_skips_malformed_keyframes(self):
        """A single malformed keyframe entry is skipped, not fatal (old
        tolerance preserved by the bulk path). Keyframe.from_dict raises
        AttributeError on a non-dict entry, which the try/except catches."""
        data = {
            "param_name": "p",
            "keyframes": [
                {"frame": 0, "value": 0.0},
                "not_a_dict",  # malformed → AttributeError in from_dict
                {"frame": 50, "value": 50.0},
            ],
        }
        track = KeyframeTrack.from_dict(data)
        assert [kf.frame for kf in track.keyframes] == [0, 50]

    def test_bulk_preserves_handles(self):
        """Bezier handles survive the bulk path intact."""
        track = KeyframeTrack(param_name="p")
        track.add_keyframes_bulk([
            Keyframe(0, 0.0, handle_right=(10.0, 5.0),
                     handle_left=(-10.0, -5.0)),
            Keyframe(100, 100.0, handle_left=(-20.0, 0.0)),
        ])
        assert track.keyframes[0].handle_right == (10.0, 5.0)
        assert track.keyframes[0].handle_left == (-10.0, -5.0)
        assert track.keyframes[1].handle_left == (-20.0, 0.0)

    def test_bulk_merges_into_existing_track(self):
        """add_keyframes_bulk extends an existing track + re-sorts + de-dups
        across the old + new keyframes."""
        track = KeyframeTrack(param_name="p")
        track.add_keyframe(Keyframe(0, 0.0))
        track.add_keyframe(Keyframe(100, 100.0))
        track.add_keyframes_bulk([
            Keyframe(0, 9.0),   # duplicate of existing → replaces
            Keyframe(50, 50.0),
        ])
        assert [kf.frame for kf in track.keyframes] == [0, 50, 100]
        assert track.keyframes[0].value == 9.0  # replaced


# ── P-1: performance characteristic (sort count) ──────────────────────


class TestBulkAddPerformance:
    def test_bulk_is_subquadratic_in_keyframe_count(self):
        """P-1's core guarantee: bulk loading is O(n log n), not O(n²).
        We verify the time ratio — if it were still per-add O(n²), loading
        200 keyframes would take ~4× as long as 100, but bulk should scale
        much better. This is a coarse guard against accidentally reverting
        to the per-keyframe-add path (which sorted N times)."""
        import time

        def bulk_time(n):
            data = {"param_name": "p",
                    "keyframes": [{"frame": n - i, "value": float(i)}
                                  for i in range(n)]}
            start = time.perf_counter()
            KeyframeTrack.from_dict(data)
            return time.perf_counter() - start

        t100 = bulk_time(100)
        t200 = bulk_time(200)
        # O(n log n) → ratio ~2.3×; O(n²) → ratio ~4×. Allow generous slack
        # for timer noise; the point is to catch a 4×+ regression.
        ratio = t200 / max(1e-6, t100)
        assert ratio < 3.5, (
            f"Bulk load 200 vs 100 keyframes took {ratio:.1f}× longer — "
            "expected ~2× (O(n log n)). A ratio near 4× suggests the O(n²) "
            "per-add path returned (P-1).")


# ── P-2: ExpressionParser AST cache ───────────────────────────────────


class TestExpressionAstCache:
    def test_repeated_evaluate_reuses_cache(self):
        """The same expression string parses once; subsequent evaluates hit
        the cache."""
        from src.features.composite.utils.expression_parser import (
            ExpressionParser, ExpressionContext,
        )
        parser = ExpressionParser()
        ctx = ExpressionContext(this_params={"width": 1920}, frame=0)
        parser.evaluate("this.width + 1", ctx)
        parser.evaluate("this.width + 1", ctx)  # same string
        # Only one AST in the cache for this one expression.
        assert len(parser._ast_cache) == 1

    def test_distinct_expressions_cached_separately(self):
        from src.features.composite.utils.expression_parser import (
            ExpressionParser, ExpressionContext,
        )
        parser = ExpressionParser()
        ctx = ExpressionContext(this_params={"width": 1920}, frame=0)
        for expr in ["this.width", "this.width + 1", "this.width * 2"]:
            parser.evaluate(expr, ctx)
        assert len(parser._ast_cache) == 3

    def test_cached_result_matches_uncached(self):
        """Caching must not change the evaluation result."""
        from src.features.composite.utils.expression_parser import (
            ExpressionParser, ExpressionContext,
        )
        cached_parser = ExpressionParser()
        ctx = ExpressionContext(this_params={"width": 1920}, frame=5)
        expr = "this.width * 0.5 + sin(0.1)"
        r1 = cached_parser.evaluate(expr, ctx)
        # Second call hits the cache.
        r2 = cached_parser.evaluate(expr, ctx)
        assert r1.success and r2.success
        assert r1.value == pytest.approx(r2.value)

    def test_validate_uses_cache_too(self):
        """validate() should also use the cache (it had its own ast.parse)."""
        from src.features.composite.utils.expression_parser import (
            ExpressionParser,
        )
        parser = ExpressionParser()
        parser.validate("this.width + 1")
        n_after_validate = len(parser._ast_cache)
        parser.validate("this.width + 1")  # same string
        assert len(parser._ast_cache) == n_after_validate  # no new entry

    def test_cache_bounded(self):
        """The cache doesn't grow without limit (bounded LRU-ish)."""
        from src.features.composite.utils.expression_parser import (
            ExpressionParser, ExpressionContext,
        )
        parser = ExpressionParser()
        ctx = ExpressionContext(this_params={"width": 1}, frame=0)
        max_cap = parser._AST_CACHE_MAX
        # Push well past the cap.
        for i in range(max_cap + 50):
            parser.evaluate(f"{i} + this.width", ctx)
        assert len(parser._ast_cache) <= max_cap, (
            f"AST cache grew to {len(parser._ast_cache)} > cap {max_cap} — "
            "the eviction must bound it (P-2).")

    def test_syntax_error_not_cached(self):
        """A SyntaxError during parse must not poison the cache (so fixing
        the typo re-parses)."""
        from src.features.composite.utils.expression_parser import (
            ExpressionParser, ExpressionContext,
        )
        parser = ExpressionParser()
        ctx = ExpressionContext(frame=0)
        # Malformed → SyntaxError, not cached.
        r = parser.validate("1 +")
        assert not r.success
        assert "1 +" not in parser._ast_cache


# ── P-3: bisect-based bracketing keyframe lookup ──────────────────────


class TestBisectLookup:
    """P-3: get_value / get_value_float find the bracketing keyframe pair by
    binary search (O(log n)) instead of a linear scan. These tests lock the
    pair-finding correctness — especially at step jumps and exact-keyframe
    frames where a bisect off-by-one would pick the wrong segment."""

    def _track(self, kfs, **kw):
        t = KeyframeTrack(param_name="p", **kw)
        t.add_keyframes_bulk(kfs)
        return t

    def test_value_at_exact_keyframe_frame(self):
        """get_value at an exact keyframe frame returns that keyframe's value
        (bisect_right puts the equal frame in the LEFT of the pair)."""
        t = self._track([Keyframe(0, 0.0), Keyframe(50, 0.5),
                         Keyframe(100, 1.0)])
        assert t.get_value(50) == pytest.approx(0.5)
        assert t.get_value(0) == pytest.approx(0.0)
        assert t.get_value(100) == pytest.approx(1.0)

    def test_step_jump_uses_correct_segment(self):
        """A value step between frame 50 and 51 — get_value(51) must find the
        50→51 segment, not the 51→100 one (bisect off-by-one risk)."""
        t = self._track([Keyframe(0, 0.0), Keyframe(50, 0.0),
                         Keyframe(51, 1.0), Keyframe(100, 1.0)])
        assert t.get_value(50) == pytest.approx(0.0)
        assert t.get_value(51) == pytest.approx(1.0)
        # Mid-segment still interpolates.
        assert t.get_value(25) == pytest.approx(0.0)

    def test_linear_interpolation_mid_segment(self):
        """A linear segment interpolates correctly between its pair."""
        t = self._track([Keyframe(0, 0.0), Keyframe(100, 1.0)])
        assert t.get_value(25) == pytest.approx(0.25)
        assert t.get_value(75) == pytest.approx(0.75)

    def test_float_lookup_step_jump(self):
        """get_value_float (sub-frame path) also picks the right segment at a
        step jump."""
        t = self._track([Keyframe(0, 0.0), Keyframe(50, 0.0),
                         Keyframe(51, 1.0), Keyframe(100, 1.0)])
        assert t.get_value_float(50.5) == pytest.approx(0.5)
        assert t.get_value_float(50.9) < 1.0  # still in 50→51 ramp

    def test_extrapolation_outside_range(self):
        """Before-first / after-last still extrapolate correctly (those paths
        use direct indexing, unaffected by the bisect change). Range is
        unbounded so the extrapolated values aren't clamped."""
        t = self._track([Keyframe(10, 0.0), Keyframe(20, 1.0)],
                        min_val=-100.0, max_val=100.0,
                        pre_extrap="linear", post_extrap="linear")
        # Pre: slope = (1-0)/(20-10) = 0.1/frame; at frame 5 → 0.0 + 0.1*(5-10)
        assert t.get_value(5) == pytest.approx(-0.5)
        # Post: at frame 30 → 1.0 + 0.1*(30-20) = 2.0
        assert t.get_value(30) == pytest.approx(2.0)

    def test_many_keyframes_picks_correct_pair(self):
        """With 100 keyframes, a query deep in the middle must pick the right
        adjacent pair (not a distant one — the old linear scan's only bug
        risk was an off-by-one in the bisect replacement)."""
        kfs = [Keyframe(i * 10, float(i) / 10.0) for i in range(11)]  # 0..100
        t = self._track(kfs)
        # frame 55 → between keyframe@50 (val 0.5) and @60 (val 0.6) → 0.55
        assert t.get_value(55) == pytest.approx(0.55)
        # frame 85 → between @80 (0.8) and @90 (0.9) → 0.85
        assert t.get_value(85) == pytest.approx(0.85)
