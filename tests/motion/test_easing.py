"""W1 easing: endpoint invariants, bezier solving and stepped curves."""

from __future__ import annotations

import math

import pytest

from src.features.teaching_animation.motion.easing import (
    CubicBezier, NAMED_EASINGS, back_in, back_out, bounce_in, bounce_out,
    ease_in_out, ease_out, elastic_out, linear, resolve_easing, steps,
)


@pytest.mark.parametrize("name", sorted(NAMED_EASINGS))
def test_named_easings_resolve_and_bounded_endpoints(name: str) -> None:
    fn = resolve_easing(name)
    # Every easing maps 0→0 and 1→1 (back/elastic/bounce may overshoot mid-flight
    # but must settle to the endpoints).
    assert math.isclose(fn(0.0), 0.0, abs_tol=1e-6), f"{name}(0) != 0"
    assert math.isclose(fn(1.0), 1.0, abs_tol=1e-6), f"{name}(1) != 1"


def test_linear_identity() -> None:
    assert linear(0.25) == 0.25
    assert linear(0.5) == 0.5


def test_cubic_bezier_endpoints_and_monotone_for_ease() -> None:
    bez = CubicBezier(0.42, 0.0, 0.58, 1.0)  # ease-in-out
    assert math.isclose(bez(0.0), 0.0, abs_tol=1e-6)
    assert math.isclose(bez(1.0), 1.0, abs_tol=1e-6)
    # Symmetric ease-in-out: bez(0.5) == 0.5
    assert math.isclose(bez(0.5), 0.5, abs_tol=1e-4)


def test_cubic_bezier_solves_x_for_y() -> None:
    # A bezier where y=x (linear mapping through control points on diagonal).
    bez = CubicBezier(0.3, 0.3, 0.7, 0.7)
    for t in (0.1, 0.25, 0.5, 0.75, 0.9):
        assert math.isclose(bez(t), t, abs_tol=1e-3)


def test_cubic_bezier_rejects_invalid_x() -> None:
    with pytest.raises(ValueError):
        CubicBezier(-0.1, 0.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        CubicBezier(0.0, 0.0, 1.1, 1.0)


def test_back_overshoots() -> None:
    # back_out overshoots ABOVE 1 near the end, then settles back to 1.
    assert back_out(1.0) == pytest.approx(1.0)
    assert back_out(0.0) == pytest.approx(0.0)
    assert max(back_out(t / 100.0) for t in range(0, 100)) > 1.0


def test_bounce_settles_and_never_exceeds_one() -> None:
    for t in [i / 50 for i in range(51)]:
        v = bounce_out(t)
        assert -1e-6 <= v <= 1.0 + 1e-6
    assert bounce_in(0.0) == pytest.approx(0.0)
    assert bounce_in(1.0) == pytest.approx(1.0)


def test_elastic_settles_to_one() -> None:
    assert elastic_out(1.0) == pytest.approx(1.0)
    assert elastic_out(0.0) == pytest.approx(0.0)


def test_steps_end_and_start() -> None:
    end = steps(4, "end")
    start = steps(4, "start")
    # end: at t=0 -> 0 (first step not yet complete)
    assert end(0.0) == pytest.approx(0.0)
    # start: at t=0 -> 1/4 (first step immediately)
    assert start(0.0) == pytest.approx(0.25)
    assert end(1.0) == pytest.approx(1.0)
    assert start(1.0) == pytest.approx(1.0)


def test_steps_count_must_be_positive_int() -> None:
    with pytest.raises(ValueError):
        steps(0)
    with pytest.raises(ValueError):
        steps(-2)
    with pytest.raises(ValueError):
        steps(2, "middle")


def test_resolve_easing_unknown_raises() -> None:
    with pytest.raises(ValueError):
        resolve_easing("not_a_real_easing")
    assert resolve_easing(None)(0.5) == 0.5
