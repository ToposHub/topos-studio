"""W1 motion language: deterministic, frame-accurate animation primitives.

Pure-function layer (no UI, no renderer, no wall clock).  Every function is a
deterministic mapping from (frame, fps, params) to a value, so preview and
export ask the same question and get the same answer on every machine.

Public surface:

* :mod:`time_range` — ``TimeRange``, local-frame mapping, nested transforms.
* :mod:`sequence` — ``SeriesGroup`` (sequential, ripple), ``SequenceGroup`` (parallel).
* :mod:`easing` — easing curves incl. ``CubicBezier``, back/elastic/bounce, steps.
* :mod:`interpolate` — Remotion-style ``interpolate`` for numbers/colours/vectors.
* :mod:`spring` — physics ``spring`` + ``measure_spring`` settle frame.
* :mod:`presets` — motion helpers + versioned ``MotionPresetRegistry``.

Plan ref: ``docs/motion_graphic_clip_remotion_inspired_optimization_plan_2026-08-12.md``
§MT-01…MT-06, TM-03, S1.
"""

from __future__ import annotations

from .easing import (
    NAMED_EASINGS,
    CubicBezier,
    EasingFn,
    back_in,
    back_in_out,
    back_out,
    bounce_in,
    bounce_in_out,
    bounce_out,
    ease_in,
    ease_in_out,
    ease_out,
    elastic_in,
    elastic_in_out,
    elastic_out,
    linear,
    resolve_easing,
    steps,
)
from .interpolate import clamp, interpolate
from .presets import (
    MotionFn,
    MotionPreset,
    MotionPresetRegistry,
    count_up,
    create_default_preset_registry,
    loop,
    ping_pong,
    reveal_progress,
    stagger_index,
    typewriter,
    wiggle,
)
from .sequence import (
    SequenceChild,
    SequenceGroup,
    SeriesGroup,
    SeriesStep,
    stagger_starts,
)
from .spring import SpringConfig, measure_spring, spring
from .time_range import TimeRange, compose_local_frame, transform_to_parent
from .motion_commands import (
    ApplyMotionPresetCommand,
    SetStaggerIntervalCommand,
    StaggerElementsCommand,
    StaggerPlan,
    compile_preset_to_track,
    scene_diff,
)
from .expression import (
    ExpressionContext,
    ExpressionError,
    evaluate_expression,
    validate_expression,
)
from .motion_binding import BindingTarget, MotionBinding
from .beat import Beat, BeatGrid, format_timecode, narration_alignment

__all__ = [
    # easing
    "NAMED_EASINGS", "CubicBezier", "EasingFn",
    "back_in", "back_in_out", "back_out", "bounce_in", "bounce_in_out", "bounce_out",
    "ease_in", "ease_in_out", "ease_out", "elastic_in", "elastic_in_out", "elastic_out",
    "linear", "resolve_easing", "steps",
    # interpolate
    "clamp", "interpolate",
    # spring
    "SpringConfig", "measure_spring", "spring",
    # time
    "TimeRange", "compose_local_frame", "transform_to_parent",
    # sequence
    "SequenceChild", "SequenceGroup", "SeriesGroup", "SeriesStep", "stagger_starts",
    # presets / helpers
    "MotionFn", "MotionPreset", "MotionPresetRegistry",
    "count_up", "create_default_preset_registry", "loop", "ping_pong",
    "reveal_progress", "stagger_index", "typewriter", "wiggle",
    # high-level commands (TM-02)
    "ApplyMotionPresetCommand", "SetStaggerIntervalCommand", "StaggerElementsCommand",
    "StaggerPlan", "compile_preset_to_track", "scene_diff",
    # controlled expressions (MT-09)
    "ExpressionContext", "ExpressionError", "evaluate_expression", "validate_expression",
    # motion binding + beats (MT-07, MT-08)
    "BindingTarget", "MotionBinding",
    "Beat", "BeatGrid", "format_timecode", "narration_alignment",
]
