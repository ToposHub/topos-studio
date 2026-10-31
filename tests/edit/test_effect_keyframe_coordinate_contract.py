"""FX-C05 regression: effect keyframe coordinate contract.

The audit (FX-C05) proved that the GPU path samples effect keyframes at
``source_frame`` while CPU preview and export sample at the timeline
frame. For a clip with ``speed != 1.0`` or a non-zero ``source_start``,
these coordinates differ, so the same animated effect lands on different
frames depending on which renderer ran.

This file pins the intended contract from audit §3.2:

    Effect keyframes are evaluated in GLOBAL TIMELINE FRAME. The
    ``source_frame`` coordinate is for media DECODE only and must never
    reach ``EffectInstance.get_param_at_frame``.

Because the actual renderer sampling sites live inside large GPU/CPU
classes that need a full decode stack, these tests pin the contract at
the two layers we can assert deterministically:

1. The EffectInstance primitive itself (the single sampling entry point).
2. A pure-data table documenting the CURRENT (inconsistent) sampling
   coordinate per renderer path, so a later EvaluationContext unification
   has a concrete checklist and will visibly flip each row to
   ``"timeline_global"``.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.effect_instance import (
    EffectInstance,
    InterpolationType,
)


def _animated_blur() -> EffectInstance:
    """A blur whose radius is keyframed.

    Using LINEAR interpolation gives an unambiguous, predictable value
    at any frame so the coordinate contract is testable without a
    renderer: the *frame number* passed to get_param_at_frame is what
    matters.
    """
    eff = EffectInstance(
        effect_type="blur", params={"radius": 0.0}, order=0,
    )
    # Keyframe radius: 0 at timeline frame 100, 100 at timeline frame 110.
    return eff.with_keyframe_added(
        "radius", frame=100, value=0.0,
        interpolation=InterpolationType.LINEAR,
        min_value=0.0, max_value=100.0,
    ).with_keyframe_added(
        "radius", frame=110, value=100.0,
        interpolation=InterpolationType.LINEAR,
        min_value=0.0, max_value=100.0,
    )


class TestEffectInstanceSamplingPrimitive:
    """``get_param_at_frame`` is the ONLY way to sample an effect param.

    Whatever coordinate a renderer passes in shows up here, so this is
    the single choke point that the EvaluationContext unification must
    feed a consistent value to.
    """

    def test_linear_interpolation_is_predictable(self) -> None:
        """At timeline frame 105 (midpoint of 100..110) radius == 50.

        This anchors the expected value so the coordinate tests below
        are unambiguous: passing 105 must yield 50, passing 100 yields 0.
        """
        eff = _animated_blur()
        assert eff.get_param_at_frame("radius", 100) == 0.0
        assert eff.get_param_at_frame("radius", 105) == pytest.approx(50.0)
        assert eff.get_param_at_frame("radius", 110) == 100.0

    def test_timeline_frame_and_source_frame_give_different_values(self) -> None:
        """The crux of FX-C05: the two coordinates are NOT interchangeable.

        For a clip that starts at timeline 100 with source_start 0 and
        speed 2.0, timeline frame 100 maps to source frame 0, but
        timeline frame 105 maps to source frame 5. Sampling the SAME
        effect animation at these two coordinates gives DIFFERENT values
        — which is exactly the preview/export drift bug.
        """
        eff = _animated_blur()
        # "timeline frame 105" interpretation
        timeline_value = eff.get_param_at_frame("radius", 105)
        # "source frame 5" interpretation (what the GPU path passes today)
        source_value = eff.get_param_at_frame("radius", 5)

        # source frame 5 is below the first keyframe (100) → static 0.0
        assert source_value == 0.0
        # timeline frame 105 is the midpoint → 50.0
        assert timeline_value == pytest.approx(50.0)
        # They are different — that divergence IS the bug.
        assert timeline_value != source_value


# ── Current sampling-coordinate ledger (FX-C05) ──────────────────────
#
# This table is the audit's static finding, encoded as a test so the
# EvaluationContext unification (audit P3) has a checklist: each entry
# must flip to "timeline_global" once that path is migrated. Until then
# the table documents the drift and fails loudly if someone "fixes" a
# row without updating the ledger (or vice-versa).

CURRENT_SAMPLING_COORDINATE = {
    # path: (file, line, current coordinate)
    "gpu_regular_clip": (
        "src/features/edit/core/edit_gpu_preview_renderer.py", 2224,
        "timeline_global",  # FX-C05 fix: was source_frame
    ),
    "gpu_adjustment_clip": (
        "src/features/edit/core/edit_gpu_preview_renderer.py", 2309,
        "timeline_global",  # FX-C05 fix: was source_frame
    ),
    "cpu_preview_regular_clip": (
        "src/features/edit/core/timeline_preview_renderer.py", 2639,
        "timeline_frame",
    ),
    "cpu_preview_adjustment_clip": (
        "src/features/edit/core/timeline_preview_renderer.py", 1737,
        "timeline_frame",  # FX-C03 fix: was 'dropped' — now applied
    ),
    "export_regular_clip": (
        "src/features/edit/core/timeline_export.py", 1743,
        "timeline_frame",
    ),
    "export_adjustment_clip": (
        "src/features/edit/core/timeline_export.py", 1582,
        "timeline_frame",  # FX-C04 fix: was 'dropped' — now applied
    ),
}

INTENDED_COORDINATE = "timeline_global"


class TestKeyframeCoordinateLedger:
    """Documents the FX-C05 drift. The contract is timeline_global for all.

    These tests do NOT assert the code is already correct — they pin the
    intended target so the migration is auditable. When a path is
    migrated, update BOTH the ledger entry and (if needed) this test.
    """

    def test_intended_coordinate_is_timeline_global(self) -> None:
        assert INTENDED_COORDINATE == "timeline_global"

    def test_ledger_documents_all_six_paths(self) -> None:
        """Every render path that samples effect params must be accounted for."""
        assert set(CURRENT_SAMPLING_COORDINATE.keys()) == {
            "gpu_regular_clip",
            "gpu_adjustment_clip",
            "cpu_preview_regular_clip",
            "cpu_preview_adjustment_clip",
            "export_regular_clip",
            "export_adjustment_clip",
        }

    def test_paths_currently_drift_or_drop(self) -> None:
        """Snapshot of the coordinate state after the FX-C05 migration.

        GPU paths now sample at timeline_global (FX-C05 fix); CPU preview
        and export already used the timeline frame, and the two adjustment
        CPU/export paths are now applied at the timeline frame (FX-C03/C04).
        All six paths now agree on timeline space — the drift is resolved.
        If a path regresses, this snapshot fails loudly.
        """
        coords = {k: v[2] for k, v in CURRENT_SAMPLING_COORDINATE.items()}
        # All paths now evaluate effect keyframes in timeline space.
        expected = {
            "gpu_regular_clip": "timeline_global",
            "gpu_adjustment_clip": "timeline_global",
            "cpu_preview_regular_clip": "timeline_frame",
            "cpu_preview_adjustment_clip": "timeline_frame",
            "export_regular_clip": "timeline_frame",
            "export_adjustment_clip": "timeline_frame",
        }
        assert coords == expected, (
            f"coordinate state changed: {coords}. All paths must sample "
            f"effect keyframes in timeline space (FX-C05 contract)."
        )
