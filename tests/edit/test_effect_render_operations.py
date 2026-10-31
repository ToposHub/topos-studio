"""FX-C02 regression: hierarchical render-operation plan scope.

The audit's headline bug: Adjustment Clips were applied as a final
full-screen post-composite pass, so they leaked into tracks ABOVE them.
The fix expresses compositing as an ordered ``RenderOperation`` list
where each ``ApplyAdjustment`` is emitted IN PLACE at its z-order
position — so a renderer walking the list applies the adjustment only to
the accumulator built from layers below it, never to layers above.

These tests pin the scope contract purely in data space (no GL), which
is exactly what audit P0 demands and what the GPU/CPU/export migration
(P2/P3) builds on.
"""

from __future__ import annotations

from typing import Optional

import pytest

from src.features.edit.core.clip_params import TransformParams
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.effect_render_operations import (
    ApplyAdjustment,
    RenderClip,
    build_render_operations,
    adjustment_indices,
    clip_indices,
)
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


# ── Fixture ──────────────────────────────────────────────────────────

class _Resolver:
    """Maps the fixture's asset ids to media types, mimicking the real
    AssetTypeResolver the preview renderer injects."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def get_asset_type(self, asset_id: str) -> Optional[str]:
        return self._mapping.get(asset_id)


def _build_three_track_fixture():
    """Lower / Adj / upper fixture reused across scope tests.

    Returns (frame_plan, resolver, ids) so tests can reason about z-order.
    """
    project = EditProject.create("Ops")
    project, lower_id = project.create_solid_color_asset("Lower", (1.0, 0.0, 0.0))
    project, adj_id = project.create_adjustment_clip("Adj")
    project, upper_id = project.create_solid_color_asset("Upper", (0.0, 1.0, 0.0))

    adj_effect = EffectInstance(
        effect_type="blur", params={"radius": 4.0}, order=0,
    )

    lower = ClipInstance(
        clip_id="c-lower", asset_id=lower_id, track_id="t-lower",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
    )
    middle = ClipInstance(
        clip_id="c-adj", asset_id=adj_id, track_id="t-adj",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
        effects=(adj_effect,),
    )
    upper = ClipInstance(
        clip_id="c-upper", asset_id=upper_id, track_id="t-upper",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
    )

    seq = TimelineSequence(
        timeline_id="seq", name="Ops",
        fps=Rational(24, 1), width=64, height=64,
        video_tracks=(
            EditTrack(track_id="t-lower", name="V1", track_type="video", index=0, clips=(lower,)),
            EditTrack(track_id="t-adj", name="V2", track_type="video", index=1, clips=(middle,)),
            EditTrack(track_id="t-upper", name="V3", track_type="video", index=2, clips=(upper,)),
        ),
    )
    plan = TimelineRenderPlan(seq)
    frame_plan = plan.get_frame_plan(0)
    resolver = _Resolver({lower_id: "solid", adj_id: "adjustment", upper_id: "solid"})
    return frame_plan, resolver, {
        "lower": lower_id, "adj": adj_id, "upper": upper_id,
        "lower_clip": "c-lower", "adj_clip": "c-adj", "upper_clip": "c-upper",
    }


# ── Scope: adjustment applies only to layers below it ────────────────

class TestAdjustmentScopeOrdering:
    """The single most important contract: the adjustment's position in
    the op list is AFTER the lower layer and BEFORE the upper layer, so a
    renderer that walks the list cannot apply it to anything above."""

    def test_op_list_is_lower_adj_upper_in_order(self) -> None:
        frame_plan, resolver, ids = _build_three_track_fixture()
        ops = build_render_operations(frame_plan, resolver)

        assert len(ops) == 3
        # Op 0: lower composite
        assert isinstance(ops[0], RenderClip) and ops[0].layer.clip_id == ids["lower_clip"]
        # Op 1: adjustment applied to accumulator-so-far (lower only)
        assert isinstance(ops[1], ApplyAdjustment) and ops[1].layer.clip_id == ids["adj_clip"]
        # Op 2: upper composite — NOT yet seen when the adjustment ran
        assert isinstance(ops[2], RenderClip) and ops[2].layer.clip_id == ids["upper_clip"]

    def test_adjustment_index_is_strictly_between_lower_and_upper(self) -> None:
        frame_plan, resolver, ids = _build_three_track_fixture()
        ops = build_render_operations(frame_plan, resolver)
        adj_positions = adjustment_indices(ops)
        clip_positions = clip_indices(ops)

        assert len(adj_positions) == 1
        adj_i = adj_positions[0]
        # There must be at least one RenderClip before the adjustment
        # (layers below) and at least one after (layers above).
        below = [i for i in clip_positions if i < adj_i]
        above = [i for i in clip_positions if i > adj_i]
        assert below, "no layer below the adjustment — scope is empty"
        assert above, "no layer above the adjustment — cannot prove non-leak"

    def test_layers_below_count_matches_preceding_clip_ops(self) -> None:
        """A renderer applies the adjustment to ``len(preceding clips)``
        composited layers. With one lower layer, the accumulator at the
        adjustment point holds exactly that one layer — not the upper."""
        frame_plan, resolver, _ = _build_three_track_fixture()
        ops = build_render_operations(frame_plan, resolver)
        adj_i = adjustment_indices(ops)[0]
        layers_below = sum(1 for op in ops[:adj_i] if isinstance(op, RenderClip))
        assert layers_below == 1


# ── Effect chain carried on the adjustment op ────────────────────────

class TestAdjustmentCarriesEffects:
    def test_adjustment_op_carries_the_effect_chain(self) -> None:
        frame_plan, resolver, _ = _build_three_track_fixture()
        ops = build_render_operations(frame_plan, resolver)
        adj = ops[adjustment_indices(ops)[0]]
        assert isinstance(adj, ApplyAdjustment)
        assert len(adj.effects) == 1
        assert adj.effects[0].effect_type == "blur"

    def test_effectless_adjustment_is_dropped(self) -> None:
        """An identity adjustment with no effects remains a no-op."""
        frame_plan, resolver, ids = _build_three_track_fixture()
        # Mutate the plan's adjustment clip to have empty effects via a
        # fresh fixture build is awkward; instead build directly.
        project = EditProject.create("Empty")
        project, lower_id = project.create_solid_color_asset("L", (1.0, 0.0, 0.0))
        project, adj_id = project.create_adjustment_clip("A")
        project, upper_id = project.create_solid_color_asset("U", (0.0, 1.0, 0.0))
        lower = ClipInstance(clip_id="cl", asset_id=lower_id, track_id="tl",
                             timeline_start=0, timeline_duration=10, source_start=0, source_duration=10)
        adj = ClipInstance(clip_id="ca", asset_id=adj_id, track_id="ta",
                           timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
                           effects=())  # no effects
        upper = ClipInstance(clip_id="cu", asset_id=upper_id, track_id="tu",
                             timeline_start=0, timeline_duration=10, source_start=0, source_duration=10)
        seq = TimelineSequence(
            timeline_id="s", name="s", fps=Rational(24, 1), width=8, height=8,
            video_tracks=(
                EditTrack(track_id="tl", name="1", track_type="video", index=0, clips=(lower,)),
                EditTrack(track_id="ta", name="2", track_type="video", index=1, clips=(adj,)),
                EditTrack(track_id="tu", name="3", track_type="video", index=2, clips=(upper,)),
            ),
        )
        frame_plan = TimelineRenderPlan(seq).get_frame_plan(0)
        resolver = _Resolver({lower_id: "solid", adj_id: "adjustment", upper_id: "solid"})
        ops = build_render_operations(frame_plan, resolver)
        # Two clips, zero adjustments.
        assert len(ops) == 2
        assert adjustment_indices(ops) == ()

    def test_effectless_adjustment_with_transform_is_kept(self) -> None:
        """Transform/Opacity alone make an Adjustment Clip renderable."""
        project = EditProject.create("Geometry")
        project, lower_id = project.create_solid_color_asset("L", (1.0, 0.0, 0.0))
        project, adj_id = project.create_adjustment_clip("A")
        lower = ClipInstance(
            clip_id="cl", asset_id=lower_id, track_id="tl",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        adjustment_transform = TransformParams(translate_x=20.0)
        adj = ClipInstance(
            clip_id="ca", asset_id=adj_id, track_id="ta",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
            effects=(),
            transform=adjustment_transform,
            opacity=0.5,
        )
        seq = TimelineSequence(
            timeline_id="s", name="s", fps=Rational(24, 1), width=8, height=8,
            video_tracks=(
                EditTrack(track_id="tl", name="1", track_type="video", index=0, clips=(lower,)),
                EditTrack(track_id="ta", name="2", track_type="video", index=1, clips=(adj,)),
            ),
        )
        frame_plan = TimelineRenderPlan(seq).get_frame_plan(0)
        ops = build_render_operations(
            frame_plan, _Resolver({lower_id: "solid", adj_id: "adjustment"})
        )

        adjustment = next(op for op in ops if isinstance(op, ApplyAdjustment))
        assert adjustment.transform == (
            adjustment_transform.translate_x,
            adjustment_transform.translate_y,
            adjustment_transform.scale_x,
            adjustment_transform.scale_y,
            adjustment_transform.rotation,
            adjustment_transform.pivot_mode,
            adjustment_transform.pivot_x,
            adjustment_transform.pivot_y,
        )
        assert adjustment.opacity == 0.5


# ── Multiple adjustments serialise in z-order ────────────────────────

class TestMultipleAdjustmentsZOrder:
    def test_two_adjustments_appear_at_their_z_positions(self) -> None:
        """Two adjustments on different tracks serialise bottom→top; a
        renderer applies the lower one first, then composites more, then
        the upper one. Moving an adjustment track changes the result."""
        project = EditProject.create("Two")
        project, base_id = project.create_solid_color_asset("Base", (1.0, 1.0, 1.0))
        project, adj_a_id = project.create_adjustment_clip("A")
        project, mid_id = project.create_solid_color_asset("Mid", (0.5, 0.5, 0.5))
        project, adj_b_id = project.create_adjustment_clip("B")
        project, top_id = project.create_solid_color_asset("Top", (0.0, 0.0, 0.0))

        def clip(cid, aid, tid):
            return ClipInstance(clip_id=cid, asset_id=aid, track_id=tid,
                                timeline_start=0, timeline_duration=10,
                                source_start=0, source_duration=10)

        adj_a = clip("ca", adj_a_id, "ta").with_effects(
            (EffectInstance(effect_type="blur", params={"radius": 1.0}),)
        )
        adj_b = clip("cb", adj_b_id, "tb").with_effects(
            (EffectInstance(effect_type="sharpen", params={"amount": 1.0}),)
        )
        seq = TimelineSequence(
            timeline_id="s", name="s", fps=Rational(24, 1), width=8, height=8,
            video_tracks=(
                EditTrack(track_id="t0", name="0", track_type="video", index=0, clips=(clip("c0", base_id, "t0"),)),
                EditTrack(track_id="ta", name="a", track_type="video", index=1, clips=(adj_a,)),
                EditTrack(track_id="t1", name="1", track_type="video", index=2, clips=(clip("c1", mid_id, "t1"),)),
                EditTrack(track_id="tb", name="b", track_type="video", index=3, clips=(adj_b,)),
                EditTrack(track_id="t2", name="2", track_type="video", index=4, clips=(clip("c2", top_id, "t2"),)),
            ),
        )
        frame_plan = TimelineRenderPlan(seq).get_frame_plan(0)
        resolver = _Resolver({
            base_id: "solid", mid_id: "solid", top_id: "solid",
            adj_a_id: "adjustment", adj_b_id: "adjustment",
        })
        ops = build_render_operations(frame_plan, resolver)

        # Expected order: base → adjA → mid → adjB → top
        kinds = [type(op).__name__ for op in ops]
        assert kinds == ["RenderClip", "ApplyAdjustment", "RenderClip",
                         "ApplyAdjustment", "RenderClip"]
        # adjA runs before mid/top exist in the accumulator; adjB runs
        # after base+mid but before top. This is the scope guarantee.
        adj_positions = adjustment_indices(ops)
        assert adj_positions == (1, 3)
        # At adjA (pos 1): only base below. At adjB (pos 3): base+mid below.
        below_a = sum(1 for op in ops[:1] if isinstance(op, RenderClip))
        below_b = sum(1 for op in ops[:3] if isinstance(op, RenderClip))
        assert below_a == 1
        assert below_b == 2


# ── Resolver absent → legacy behaviour (no adjustments detected) ─────

class TestResolverAbsent:
    def test_no_resolver_treats_all_as_clips(self) -> None:
        """Without an asset resolver the builder cannot identify
        adjustments, so it falls back to treating every layer as a plain
        RenderClip. This preserves legacy behaviour for callers that have
        not opted into the operation plan."""
        frame_plan, _, _ = _build_three_track_fixture()
        ops = build_render_operations(frame_plan, asset_resolver=None)
        assert len(ops) == 3
        assert all(isinstance(op, RenderClip) for op in ops)
        assert adjustment_indices(ops) == ()
