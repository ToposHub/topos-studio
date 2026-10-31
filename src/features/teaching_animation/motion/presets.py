"""MT-05 / TM-03: motion helpers + the parameterized ``MotionPresetRegistry``.

Two layers live here:

* **Motion helpers** — small pure functions that compose with ``interpolate`` /
  ``spring``: ``loop``, ``ping_pong``, ``wiggle`` (seeded, deterministic),
  ``count_up``, ``typewriter``, ``reveal_progress`` and ``stagger_index``.
* **MotionPresetRegistry** — versioned, parameterized recipes (fade / slide /
  pop / scale / overshoot / reveal / draw / word_highlight).  A preset ``build``
  returns a ``MotionFn(frame, fps) -> value``; the recipe itself is data so AI
  can select a preset and fill params instead of emitting opaque keyframe blobs.

Per the plan, presets are *parameterized recipes, not opaque keyframe blobs*:
the same preset + params always produces the same motion, and a preset can be
migrated because its version + params are serialized, not its output samples.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .easing import EasingFn, resolve_easing
from .interpolate import interpolate
from .spring import SpringConfig, spring


MotionFn = Callable[[int, float], float]


# ---------------------------------------------------------------------------
# Motion helpers (MT-05)
# ---------------------------------------------------------------------------

def loop(frame: int, period: int) -> int:
    """Sawtooth: wrap ``frame`` into ``[0, period)``."""
    if period <= 0:
        raise ValueError("loop period must be positive")
    return int(frame) % period


def ping_pong(frame: int, period: int) -> int:
    """Triangle: bounce ``frame`` inside ``[0, period]``."""
    if period <= 0:
        raise ValueError("ping_pong period must be positive")
    cycle = 2 * period
    f = int(frame) % cycle
    return f if f <= period else cycle - f


def _hash01(seed: int, i: int) -> float:
    """Deterministic pseudo-random in [0,1) from (seed, lattice index)."""
    digest = hashlib.sha256(f"{seed}:{i}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little") / 2 ** 64


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def wiggle(frame: float, *, seed: int = 0, frequency: float = 1.0, amplitude: float = 1.0) -> float:
    """Deterministic smoothed value-noise in ``[-amplitude, amplitude]``.

    No ``random`` module — the same (seed, frame, frequency, amplitude) always
    yields the same value, so preview/export match exactly.
    """
    if frequency <= 0.0:
        raise ValueError("wiggle frequency must be positive")
    if amplitude < 0.0:
        raise ValueError("wiggle amplitude must be non-negative")
    x = float(frame) * frequency
    i0 = math.floor(x)
    i1 = i0 + 1
    frac = x - i0
    v0 = _hash01(seed, i0) * 2.0 - 1.0
    v1 = _hash01(seed, i1) * 2.0 - 1.0
    eased = _smoothstep(frac)
    return (v0 + (v1 - v0) * eased) * amplitude


def count_up(frame: int, *, start_frame: int, end_frame: int, from_: float, to: float) -> float:
    """Linearly interpolate a number from ``from_`` to ``to`` over a frame range."""
    if end_frame <= start_frame:
        raise ValueError("count_up end_frame must exceed start_frame")
    return interpolate(
        float(frame),
        [float(start_frame), float(end_frame)],
        [from_, to],
        extrapolate_left="clamp", extrapolate_right="clamp",
    )


def typewriter(frame: int, text: str, *, start_frame: int, chars_per_frame: float = 1.0) -> str:
    """Reveal ``text`` progressively; deterministic substring based on frame."""
    if chars_per_frame <= 0.0:
        raise ValueError("typewriter chars_per_frame must be positive")
    if frame < start_frame:
        return ""
    visible = (int(frame) - start_frame) * chars_per_frame
    return text[: int(visible)]


def reveal_progress(frame: int, *, start: int, duration: int, easing: EasingFn | str | None = None) -> float:
    """``[0, 1]`` progress through a reveal/wipe window, clamped at both ends."""
    if duration <= 0:
        raise ValueError("reveal duration must be positive")
    return interpolate(
        float(frame), [float(start), float(start + duration)], [0.0, 1.0],
        easing=easing, extrapolate_left="clamp", extrapolate_right="clamp",
    )


def stagger_index(index: int, *, interval: int, delay: int = 0) -> int:
    """Start frame for the ``index``-th staggered element."""
    if interval < 0:
        raise ValueError("stagger interval must be non-negative")
    return delay + index * interval


# ---------------------------------------------------------------------------
# MotionPresetRegistry (TM-03)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MotionPreset:
    """A versioned, parameterized motion recipe."""

    preset_id: str
    version: int
    description: str
    build: Callable[..., MotionFn] = field(repr=False)
    default_params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.preset_id:
            raise ValueError("preset_id cannot be empty")
        if self.version < 1:
            raise ValueError("preset version must be positive")


def _fade_preset(start: int = 0, duration: int = 12, *, easing: EasingFn | str | None = "ease_out",
                 from_: float = 0.0, to: float = 1.0) -> MotionFn:
    ease = resolve_easing(easing)

    def fn(frame: int, fps: float) -> float:
        return interpolate(float(frame), [float(start), float(start + duration)], [from_, to],
                           easing=ease, extrapolate_left="clamp", extrapolate_right="clamp")

    return fn


_SLIDE_DIRECTIONS: dict[str, float] = {"up": 1.0, "down": -1.0, "left": 1.0, "right": -1.0}


def _slide_preset(start: int = 0, duration: int = 12, *, distance: float = 80.0,
                  direction: str = "up", easing: EasingFn | str | None = "ease_out") -> MotionFn:
    """Returns the *offset* (px) to apply along an axis; caller maps to x/y."""
    ease = resolve_easing(easing)
    # AUD-28119: direction is an editor/AI-filled preset parameter, so an
    # unknown value must fail loudly (like ``resolve_easing``) instead of
    # silently sliding the wrong way with the "up" sign.
    try:
        sign = _SLIDE_DIRECTIONS[direction]
    except KeyError:
        raise ValueError(
            f"unsupported slide direction: {direction!r} (expected one of "
            f"{', '.join(sorted(_SLIDE_DIRECTIONS))})") from None

    def fn(frame: int, fps: float) -> float:
        progress = interpolate(float(frame), [float(start), float(start + duration)], [0.0, 1.0],
                               easing=ease, extrapolate_left="clamp", extrapolate_right="clamp")
        return sign * distance * (1.0 - progress)

    return fn


def _coerce_spring_config(value) -> SpringConfig | None:
    """Accept SpringConfig or its JSON round-trip dict form (plan metadata)."""
    if isinstance(value, SpringConfig):
        return value
    if isinstance(value, dict):
        return SpringConfig(
            mass=float(value.get("mass", 1.0)),
            damping=float(value.get("damping", 10.0)),
            stiffness=float(value.get("stiffness", 100.0)),
        )
    return None


def _pop_preset(start: int = 0, *, spring_config: SpringConfig | None = None,
                overshoot: bool = True, to: float = 1.0) -> MotionFn:
    cfg = _coerce_spring_config(spring_config) or SpringConfig(mass=1.0, damping=12.0, stiffness=180.0)

    def fn(frame: int, fps: float) -> float:
        return spring(frame, fps, cfg, delay=start, overshoot_clamp=not overshoot, to=to)

    return fn


def _scale_preset(start: int = 0, duration: int = 12, *, from_: float = 0.5, to: float = 1.0,
                  easing: EasingFn | str | None = "back_out") -> MotionFn:
    ease = resolve_easing(easing)

    def fn(frame: int, fps: float) -> float:
        return interpolate(float(frame), [float(start), float(start + duration)], [from_, to],
                           easing=ease, extrapolate_left="clamp", extrapolate_right="clamp")

    return fn


def _overshoot_preset(start: int = 0, *, spring_config: SpringConfig | None = None, to: float = 1.0) -> MotionFn:
    cfg = _coerce_spring_config(spring_config) or SpringConfig(mass=1.0, damping=7.0, stiffness=170.0)

    def fn(frame: int, fps: float) -> float:
        return spring(frame, fps, cfg, delay=start, overshoot_clamp=False, to=to)

    return fn


def _reveal_preset(start: int = 0, duration: int = 12, *, easing: EasingFn | str | None = "ease_out") -> MotionFn:
    ease = resolve_easing(easing)

    def fn(frame: int, fps: float) -> float:
        return interpolate(float(frame), [float(start), float(start + duration)], [0.0, 1.0],
                           easing=ease, extrapolate_left="clamp", extrapolate_right="clamp")

    return fn


def _draw_preset(start: int = 0, duration: int = 18, *, easing: EasingFn | str | None = "ease_in_out") -> MotionFn:
    ease = resolve_easing(easing)

    def fn(frame: int, fps: float) -> float:
        return interpolate(float(frame), [float(start), float(start + duration)], [0.0, 1.0],
                           easing=ease, extrapolate_left="clamp", extrapolate_right="clamp")

    return fn


def _word_highlight_preset(start: int = 0, duration: int = 10, *, peak: float = 1.0) -> MotionFn:
    def fn(frame: int, fps: float) -> float:
        # ramp up then hold
        if frame < start:
            return 0.0
        if frame < start + duration:
            return interpolate(float(frame), [float(start), float(start + duration)], [0.0, peak],
                               extrapolate_left="clamp", extrapolate_right="clamp")
        return peak

    return fn


def create_default_preset_registry() -> "MotionPresetRegistry":
    """Build the registry seeded with the P1 preset set."""
    registry = MotionPresetRegistry()
    registry.register(MotionPreset("fade", 1, "Opacity from→to over a window.",
                                   default_params={"start": 0, "duration": 12}, build=_fade_preset))
    registry.register(MotionPreset("slide", 1, "Translate offset that settles to 0.",
                                   default_params={"start": 0, "duration": 12, "distance": 80.0, "direction": "up"},
                                   build=_slide_preset))
    registry.register(MotionPreset("pop", 1, "Spring-driven scale-in (snappy).",
                                   default_params={"start": 0, "overshoot": True}, build=_pop_preset))
    registry.register(MotionPreset("scale", 1, "Eased scale from→to.",
                                   default_params={"start": 0, "duration": 12, "from_": 0.5, "to": 1.0},
                                   build=_scale_preset))
    registry.register(MotionPreset("overshoot", 1, "Spring with visible overshoot.",
                                   default_params={"start": 0}, build=_overshoot_preset))
    registry.register(MotionPreset("reveal", 1, "Mask/wipe progress 0→1.",
                                   default_params={"start": 0, "duration": 12}, build=_reveal_preset))
    registry.register(MotionPreset("draw", 1, "Stroke draw progress 0→1.",
                                   default_params={"start": 0, "duration": 18}, build=_draw_preset))
    registry.register(MotionPreset("word_highlight", 1, "Word highlight intensity ramp.",
                                   default_params={"start": 0, "duration": 10}, build=_word_highlight_preset))
    return registry


class MotionPresetRegistry:
    """Versioned registry of parameterized motion presets."""

    def __init__(self) -> None:
        self._presets: dict[str, MotionPreset] = {}

    def register(self, preset: MotionPreset, *, replace: bool = False) -> bool:
        existing = self._presets.get(preset.preset_id)
        if existing is not None and not replace:
            return False
        # With replace=True the caller explicitly overrides, including a
        # version downgrade; without replace, any duplicate is rejected so
        # preset versions never regress silently.
        self._presets[preset.preset_id] = preset
        return True

    def get(self, preset_id: str) -> MotionPreset | None:
        return self._presets.get(preset_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._presets))

    def build(self, preset_id: str, **params: Any) -> MotionFn:
        preset = self._presets.get(preset_id)
        if preset is None:
            raise KeyError(f"unknown motion preset: {preset_id}")
        merged = dict(preset.default_params)
        merged.update(params)
        return preset.build(**merged)


__all__ = [
    "MotionFn",
    "MotionPreset",
    "MotionPresetRegistry",
    "count_up",
    "create_default_preset_registry",
    "loop",
    "ping_pong",
    "reveal_progress",
    "stagger_index",
    "typewriter",
    "wiggle",
]
