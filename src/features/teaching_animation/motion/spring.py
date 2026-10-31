"""MT-04: deterministic physics spring + ``measure_spring`` settle frame.

Unlike CSS easing, a spring has no fixed duration — it settles asymptotically.
We use the closed-form step response of a damped harmonic oscillator so the
value at any frame is a pure function of (frame, fps, mass, damping, stiffness).
That makes it reproducible across preview and export and lets ``measure_spring``
report the settle frame for beat/time-line scheduling.

The math is the standard mass-spring-damper step response:

    m x'' + c x' + k x = target,   x(0)=0, x'(0)=0

with the underdamped / critically-damped / overdamped branches.  ``delay``
shifts the start; ``overshoot_clamp`` bounds the unit response to ``[0, 1]`` so
a "no overshoot" pop is expressible without editing keyframes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


_SettleThreshold = 0.005   # |response - 1| at which a spring is considered settled
_SettleWindow = 6          # consecutive frames within threshold to confirm settle
# AUD-28202: |zeta - 1| below this means near-critical damping.  The
# discriminant test (c*c - 4*m*k) and the under-damped factor
# (1 - zeta*zeta) are two independent floating-point rounding paths, so a
# mathematically critical config can enter the under-damped branch with
# zeta rounding to exactly 1.0 — making omega_d == 0 and the
# (zeta*omega0/omega_d) term a ZeroDivisionError (or zeta slightly above
# 1.0, making sqrt(1 - zeta*zeta) a domain error).  Rounding can only push
# zeta ~1e-15 past 1.0, well inside this band.
_NearCriticalZeta = 1e-12


@dataclass(frozen=True)
class SpringConfig:
    """Physical parameters of a spring.  Defaults give a snappy UI pop."""

    mass: float = 1.0
    damping: float = 10.0
    stiffness: float = 100.0

    def __post_init__(self) -> None:
        for name, value in (("mass", self.mass), ("damping", self.damping), ("stiffness", self.stiffness)):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"spring {name} must be finite and positive")


def _step_response(t: float, config: SpringConfig) -> float:
    """Unit step response (0→1) of the damped oscillator at time t ≥ 0."""
    m, c, k = config.mass, config.damping, config.stiffness
    omega0 = math.sqrt(k / m)
    zeta = c / (2.0 * math.sqrt(k * m))
    if abs(zeta - 1.0) <= _NearCriticalZeta:
        # Critically damped — including near-critical configs where the
        # discriminant and 1 - zeta*zeta round to opposite signs (AUD-28202).
        # The zeta -> 1 limit of the under-damped branch equals this closed
        # form, so it is the mathematically correct evaluation for the whole
        # band, and it avoids dividing by a zero omega_d.
        envelope = math.exp(-omega0 * t)
        return 1.0 - envelope * (1.0 + omega0 * t)
    discriminant = c * c - 4.0 * m * k
    if discriminant < 0.0:
        # Under-damped: oscillates, decays.  The guard above guarantees
        # 1 - zeta*zeta is positive here.
        omega_d = omega0 * math.sqrt(1.0 - zeta * zeta)
        envelope = math.exp(-zeta * omega0 * t)
        return 1.0 - envelope * (math.cos(omega_d * t) + (zeta * omega0 / omega_d) * math.sin(omega_d * t))
    # Over-damped: two real negative roots.
    sqrt_d = math.sqrt(discriminant)
    s1 = (-c + sqrt_d) / (2.0 * m)
    s2 = (-c - sqrt_d) / (2.0 * m)
    return 1.0 - (s2 * math.exp(s1 * t) - s1 * math.exp(s2 * t)) / (s2 - s1)


def spring(
    frame: float,
    fps: float,
    config: SpringConfig | None = None,
    *,
    delay: int = 0,
    overshoot_clamp: bool = False,
    duration: int | None = None,
    duration_rest_threshold: float = _SettleThreshold,
    from_: float = 0.0,
    to: float = 1.0,
) -> float:
    """Evaluate a spring at ``frame`` for the given ``fps``.

    Before ``delay`` the value is ``from_``.  Once the spring is at rest
    within ``duration_rest_threshold`` of ``to``, the value snaps exactly to
    ``to``; when ``duration`` is given, that snap is additionally deferred
    until ``duration`` frames have elapsed.  A spring settles
    asymptotically and has no fixed duration, so a ``duration`` shorter
    than the physical settle time never truncates the motion — the value
    keeps following the physics instead of jumping to ``to`` (see
    ``measure_spring`` for the actual settle frame).
    ``overshoot_clamp`` bounds the unit response to ``[0, 1]`` so the
    spring never exceeds the target.
    """
    if fps <= 0.0 or not math.isfinite(fps):
        raise ValueError("spring fps must be finite and positive")
    cfg = config or SpringConfig()
    if frame < delay:
        return from_
    local_frame = frame - delay
    if local_frame <= 0:
        return from_
    t = local_frame / fps
    response = _step_response(t, cfg)
    if overshoot_clamp:
        response = max(0.0, min(1.0, response))
    value = from_ + (to - from_) * response
    # AUD-28203: ``measure_spring`` returns ``max_frames`` both for a spring
    # that settles exactly at the cap and for one that never settles within
    # it, so the settle frame alone must not authorise a snap — a slow
    # spring (or an unconverged one) would jump to ``to`` in a single
    # visible frame.  Snap only when the response is genuinely at rest
    # within ``duration_rest_threshold``; ``duration`` just defers the exact
    # snap to the requested endpoint.
    if abs(value - to) > duration_rest_threshold:
        return value
    settle = measure_spring(fps, cfg, rest_threshold=duration_rest_threshold)
    if local_frame >= settle and (duration is None or local_frame >= duration):
        return to
    return value


def measure_spring(
    fps: float,
    config: SpringConfig | None = None,
    *,
    rest_threshold: float = _SettleThreshold,
    max_frames: int = 600,
) -> int:
    """Return the first frame at which the spring has settled within threshold.

    "Settled" means the response stays within ``rest_threshold`` of the target
    for ``_SettleWindow`` consecutive frames.  Bounded by ``max_frames`` so a
    pathological config cannot loop forever; if it never settles, ``max_frames``
    is returned.
    """
    if fps <= 0.0:
        raise ValueError("measure_spring fps must be positive")
    cfg = config or SpringConfig()
    consecutive = 0
    for frame in range(0, max_frames + 1):
        t = frame / fps
        response = _step_response(t, cfg)
        if abs(response - 1.0) < rest_threshold:
            consecutive += 1
            if consecutive >= _SettleWindow:
                return max(1, frame - _SettleWindow + 1)
        else:
            consecutive = 0
    return max_frames


__all__ = ["SpringConfig", "measure_spring", "spring"]
