"""W1 interpolate: golden values, extrapolation, colours/vectors, validation, fuzz."""

from __future__ import annotations

import math
import random

import pytest

from src.features.teaching_animation.motion import clamp, interpolate


class TestGoldenValues:
    def test_linear_midpoint(self) -> None:
        assert interpolate(0.5, [0.0, 1.0], [0.0, 100.0]) == 50.0

    def test_piecewise_three_points(self) -> None:
        # 0→0, 10→100, 20→200
        assert interpolate(5.0, [0.0, 10.0, 20.0], [0.0, 100.0, 200.0]) == 50.0
        assert interpolate(15.0, [0.0, 10.0, 20.0], [0.0, 100.0, 200.0]) == 150.0

    def test_eased_segment(self) -> None:
        # ease_out quadratic: at t=0.5 -> 0.75
        v = interpolate(0.5, [0.0, 1.0], [0.0, 1.0], easing="ease_out")
        assert v == pytest.approx(0.75)

    def test_clamp_both_ends(self) -> None:
        assert interpolate(-5.0, [0.0, 1.0], [0.0, 10.0],
                            extrapolate_left="clamp", extrapolate_right="clamp") == 0.0
        assert interpolate(99.0, [0.0, 1.0], [0.0, 10.0],
                            extrapolate_left="clamp", extrapolate_right="clamp") == 10.0

    def test_identity_extrapolation(self) -> None:
        assert interpolate(5.0, [0.0, 1.0], [0.0, 10.0], extrapolate_right="identity") == 5.0

    def test_extend_extrapolation_uses_slope(self) -> None:
        # below range, slope = (10-0)/(1-0) = 10; at -1 -> -10
        assert interpolate(-1.0, [0.0, 1.0], [0.0, 10.0]) == pytest.approx(-10.0)


class TestColorsAndVectors:
    def test_color_midpoint(self) -> None:
        assert interpolate(0.5, [0.0, 1.0], ["#000000", "#ffffff"]) == "#808080"

    def test_color_short_form(self) -> None:
        assert interpolate(0.5, [0.0, 1.0], ["#000", "#fff"]) == "#808080"

    def test_color_alpha(self) -> None:
        out = interpolate(0.5, [0.0, 1.0], ["#00000000", "#ffffff80"])
        # midpoint alpha = (0+0x80)/2 = 0x40
        assert out.endswith("40")

    def test_vector_interpolation(self) -> None:
        v = interpolate(0.5, [0.0, 1.0], [(0.0, 10.0, 20.0), (100.0, 20.0, 40.0)])
        assert v == (50.0, 15.0, 30.0)

    def test_vector_length_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError):
            interpolate(0.5, [0.0, 1.0], [(0.0, 1.0), (2.0, 3.0, 4.0)])

    def test_unsupported_type_rejected(self) -> None:
        with pytest.raises(ValueError):
            interpolate(0.5, [0.0, 1.0], ["a string", "another"])


class TestValidation:
    def test_mismatched_range_lengths(self) -> None:
        with pytest.raises(ValueError):
            interpolate(0.5, [0.0, 1.0, 2.0], [0.0, 1.0])

    def test_non_monotonic_input_rejected(self) -> None:
        with pytest.raises(ValueError):
            interpolate(0.5, [0.0, 1.0, 0.5], [0.0, 1.0, 2.0])

    def test_too_few_points(self) -> None:
        with pytest.raises(ValueError):
            interpolate(0.5, [0.0], [1.0])

    def test_nan_value_rejected(self) -> None:
        with pytest.raises(ValueError):
            interpolate(float("nan"), [0.0, 1.0], [0.0, 1.0])

    def test_non_finite_input_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            interpolate(0.5, [0.0, float("inf")], [0.0, 1.0])


class TestPropertyFuzz:
    def test_monotone_output_for_monotone_increasing_numeric(self) -> None:
        rng = random.Random(42)
        for _ in range(50):
            pts = sorted(rng.uniform(-10, 10) for _ in range(5))
            if len(set(pts)) != 5:
                continue
            outs = sorted(rng.uniform(0, 100) for _ in range(5))
            samples = [interpolate(pts[i] + 1e-3, pts, outs) for i in range(4)]
            assert all(samples[i] <= samples[i + 1] + 1e-6 for i in range(3))

    def test_clamp_never_outside_output_range(self) -> None:
        rng = random.Random(7)
        for _ in range(100):
            x = rng.uniform(-50, 50)
            v = interpolate(x, [0.0, 1.0], [0.0, 10.0],
                            extrapolate_left="clamp", extrapolate_right="clamp")
            assert 0.0 <= v <= 10.0

    def test_evaluable_across_full_range_without_crash(self) -> None:
        rng = random.Random(99)
        for _ in range(200):
            n = rng.randint(2, 6)
            xs = sorted(rng.uniform(0, 100) for _ in range(n))
            if len(set(xs)) != n:
                continue
            ys = [rng.uniform(-50, 50) for _ in range(n)]
            x = rng.uniform(0, 100)
            interpolate(x, xs, ys)  # must not raise


def test_clamp_helper() -> None:
    assert clamp(5.0, 0.0, 1.0) == 1.0
    assert clamp(-1.0, 0.0, 1.0) == 0.0
    assert clamp(0.3) == 0.3
    with pytest.raises(ValueError):
        clamp(1.0, high=0.0, low=1.0)
