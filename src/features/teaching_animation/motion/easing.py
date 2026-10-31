"""MT-06: easing curves — pure functions mapping normalized progress to eased progress.

Every easing function takes ``t`` in ``[0, 1]`` and returns a (possibly
out-of-range) eased progress value.  They are deterministic, side-effect-free
and free of wall-clock or random input, so preview and export ask the same
function the same question and get the same answer.

The implementations follow the well-known CSS/Remotion semantics so authors
(and the AI command catalog) get familiar, predictable curves.  ``CubicBezier``
solves the CSS bezier for ``x`` via Newton-Raphson + bisection fallback.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Protocol


class EasingFn(Protocol):
    """``t in [0,1] -> eased progress`` (may overshoot for back/elastic/bounce)."""

    def __call__(self, t: float) -> float: ...


def linear(t: float) -> float:
    return t


def _quad_in(t: float) -> float:
    return t * t


def _quad_out(t: float) -> float:
    return t * (2.0 - t)


def ease_in(t: float) -> float:
    return _quad_in(t)


def ease_out(t: float) -> float:
    return _quad_out(t)


def ease_in_out(t: float) -> float:
    return 2.0 * t * t if t < 0.5 else -1.0 + (4.0 - 2.0 * t) * t


def _cubic_coeff(t: float, a: float, b: float) -> float:
    """Bezier polynomial coefficient for one axis (a=1st control, b=2nd control)."""
    return 3.0 * (1.0 - t) ** 2 * t * a + 3.0 * (1.0 - t) * t ** 2 * b + t ** 3


@dataclass(frozen=True)
class CubicBezier:
    """CSS-style cubic-bezier easing with control points (x1,y1,x2,y2).

    x1/x2 must be in [0,1] (CSS constraint); y1/y2 may overshoot for back/elastic
    flavours.  Solves progress->x then returns the matching y.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        for value in (self.x1, self.x2):
            if not math.isfinite(value) or value < 0.0 or value > 1.0:
                raise ValueError("cubic-bezier x control points must be in [0,1]")
        for value in (self.y1, self.y2):
            if not math.isfinite(value):
                raise ValueError("cubic-bezier y control points must be finite")

    def _solve_x(self, progress: float) -> float:
        # Newton-Raphson on the x polynomial; fall back to bisection.
        if progress <= 0.0:
            return 0.0
        if progress >= 1.0:
            return 1.0
        guess = progress
        for _ in range(8):
            x = _cubic_coeff(guess, self.x1, self.x2) - progress
            if abs(x) < 1e-6:
                return guess
            derivative = 3.0 * (1.0 - guess) ** 2 * (self.x1) \
                + 6.0 * (1.0 - guess) * guess * (self.x2 - self.x1) \
                + 3.0 * guess ** 2 * (1.0 - self.x2)
            if abs(derivative) < 1e-6:
                break
            guess -= x / derivative
            guess = min(1.0, max(0.0, guess))
        # Bisection fallback.
        lo, hi = 0.0, 1.0
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if _cubic_coeff(mid, self.x1, self.x2) < progress:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    def __call__(self, t: float) -> float:
        return _cubic_coeff(self._solve_x(t), self.y1, self.y2)


# Canonical named beziers (CSS / Remotion equivalents).
EASE = CubicBezier(0.25, 0.1, 0.25, 1.0)
EASE_IN_BEZIER = CubicBezier(0.42, 0.0, 1.0, 1.0)
EASE_OUT_BEZIER = CubicBezier(0.0, 0.0, 0.58, 1.0)
EASE_IN_OUT_BEZIER = CubicBezier(0.42, 0.0, 0.58, 1.0)


def back_in(t: float, overshoot: float = 1.70158) -> float:
    return t * t * ((overshoot + 1.0) * t - overshoot)


def back_out(t: float, overshoot: float = 1.70158) -> float:
    u = t - 1.0
    return u * u * ((overshoot + 1.0) * u + overshoot) + 1.0


def back_in_out(t: float, overshoot: float = 2.5949095) -> float:
    s = overshoot
    if t < 0.5:
        u = 2.0 * t
        return 0.5 * (u * u * ((s + 1.0) * u - s))
    u = 2.0 * t - 2.0
    return 0.5 * (u * u * ((s + 1.0) * u + s) + 2.0)


def elastic_in(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    # Penner easeInElastic: amplitude builds from 0, approaches 1 continuously
    # (mirror of elastic_out: elastic_in(t) == 1 - elastic_out(1 - t)).
    return -(2.0 ** (10.0 * (t - 1.0))) * math.sin((t - 1.075) * (2.0 * math.pi / 0.3))


def elastic_out(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return math.sin((t - 0.075) * (2.0 * math.pi / 0.3)) * (2.0 ** (-10.0 * t)) + 1.0


def elastic_in_out(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    # Penner easeInOutElastic: period 0.45 per half, continuous at t=0/0.5/1.
    if t < 0.5:
        return -0.5 * (2.0 ** (10.0 * (2.0 * t - 1.0))) * math.sin(
            (2.0 * t - 1.1125) * (2.0 * math.pi / 0.45))
    return 0.5 * (2.0 ** (-10.0 * (2.0 * t - 1.0))) * math.sin(
        (2.0 * t - 1.1125) * (2.0 * math.pi / 0.45)) + 1.0


def bounce_out(t: float) -> float:
    if t < 1.0 / 2.75:
        return 7.5625 * t * t
    if t < 2.0 / 2.75:
        t -= 1.5 / 2.75
        return 7.5625 * t * t + 0.75
    if t < 2.5 / 2.75:
        t -= 2.25 / 2.75
        return 7.5625 * t * t + 0.9375
    t -= 2.625 / 2.75
    return 7.5625 * t * t + 0.984375


def bounce_in(t: float) -> float:
    return 1.0 - bounce_out(1.0 - t)


def bounce_in_out(t: float) -> float:
    if t < 0.5:
        return 0.5 * (1.0 - bounce_out(1.0 - 2.0 * t))
    return 0.5 * bounce_out(2.0 * t - 1.0) + 0.5


def steps(count: int, position: str = "end") -> Callable[[float], float]:
    """CSS-style stepped easing.  ``position`` ∈ {start, end} (a.k.a. jump-{start,end})."""
    if not isinstance(count, int) or count <= 0:
        raise ValueError("steps count must be a positive integer")
    if position not in {"start", "end"}:
        raise ValueError("steps position must be 'start' or 'end'")

    def _step(t: float) -> float:
        if t <= 0.0:
            return 1.0 / count if position == "start" else 0.0
        if t >= 1.0:
            return 1.0
        scaled = t * count
        idx = math.floor(scaled)
        if position == "start":
            idx += 1
        return min(1.0, idx / count)

    return _step


NAMED_EASINGS: dict[str, EasingFn] = {
    "linear": linear,
    "ease_in": ease_in, "ease_out": ease_out, "ease_in_out": ease_in_out,
    "ease": EASE,
    "ease_in_bezier": EASE_IN_BEZIER, "ease_out_bezier": EASE_OUT_BEZIER, "ease_in_out_bezier": EASE_IN_OUT_BEZIER,
    "back_in": back_in, "back_out": back_out, "back_in_out": back_in_out,
    "elastic_in": elastic_in, "elastic_out": elastic_out, "elastic_in_out": elastic_in_out,
    "bounce_in": bounce_in, "bounce_out": bounce_out, "bounce_in_out": bounce_in_out,
}


def resolve_easing(name_or_fn: "str | EasingFn | None") -> EasingFn:
    """Resolve an easing spec (name string, callable, or None→linear)."""
    if name_or_fn is None:
        return linear
    if callable(name_or_fn):
        return name_or_fn  # type: ignore[return-value]
    easing = NAMED_EASINGS.get(str(name_or_fn))
    if easing is None:
        raise ValueError(f"unknown easing: {name_or_fn}")
    return easing


__all__ = [
    "EASE", "EASE_IN_BEZIER", "EASE_OUT_BEZIER", "EASE_IN_OUT_BEZIER",
    "NAMED_EASINGS",
    "CubicBezier", "EasingFn",
    "back_in", "back_in_out", "back_out", "bounce_in", "bounce_in_out", "bounce_out",
    "ease_in", "ease_in_out", "ease_out", "elastic_in", "elastic_in_out", "elastic_out",
    "linear", "resolve_easing", "steps",
]
