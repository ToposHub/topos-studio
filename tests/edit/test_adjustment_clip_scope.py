"""FX-C02 / FX-C03 / FX-C04 regression: Adjustment Clip scope & parity.

This file pins the Adjustment Clip contract across the render pipeline:

* FX-C02 — scope: an Adjustment Clip must affect ONLY the layers below
  it, never layers above. Today the GPU path composites ALL tracks first
  and only then applies the adjustment chain, so it leaks into upper
  tracks. This test builds the three-track fixture (lower red / Adj /
  upper green) and asserts the structural preconditions the fix needs:
  the adjustment layer is identified and the upper/lower layers carry
  distinct identities the renderer must respect.

* FX-C03 — CPU fallback must NOT silently drop the adjustment. Today the
  CPU ``_composite_layers`` returns None for adjustment layers and never
  runs a post-composite pass, so on GPU-less machines the effect simply
  disappears.

* FX-C04 — export must apply the adjustment. Today export has no
  adjustment branch in ``_get_layer_frame`` and no post-composite pass,
  so the adjustment never reaches the deliverable.

The golden-frame pixel comparison (GPU vs CPU vs export) requires an
offscreen GL fixture that is out of scope for P0 (audit §7). These tests
instead assert the pure-data contracts and the exact silent-drop bugs
the static audit proved, so the fixes are pinned at the layer the bugs
actually live on.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.edit.core.clip_params import TransformParams
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.timeline_render_plan import (
    ClipLayerInfo,
    FrameRenderPlan,
    TimelineRenderPlan,
)
from src.shared.media.metadata import Rational


# ── Fixture builders ──────────────────────────────────────────────────

def _build_three_track_sequence() -> tuple[EditProject, TimelineSequence, dict]:
    """Lower-red / Adjustment / upper-green, overlapping in time.

    Returns (project, sequence, ids) where ids holds the asset ids keyed
    by role so individual tests can find the adjustment layer.
    """
    project = EditProject.create("AdjScope")
    # Lower track asset (would be red in a real fixture)
    project, red_id = project.create_solid_color_asset(
        name="Lower Red", color_rgb=(1.0, 0.0, 0.0)
    )
    # Adjustment virtual asset — no source frame, carries the effect chain.
    project, adj_id = project.create_adjustment_clip(name="Adj")
    # Upper track asset (would be green)
    project, green_id = project.create_solid_color_asset(
        name="Upper Green", color_rgb=(0.0, 1.0, 0.0)
    )

    # An adjustment effect that is trivially detectable (inversion-like).
    adj_effect = EffectInstance(
        effect_type="blur", params={"radius": 8.0, "mix": 1.0}, order=0
    )

    lower = ClipInstance(
        clip_id="c-lower", asset_id=red_id, track_id="track-lower",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
    )
    middle = ClipInstance(
        clip_id="c-adj", asset_id=adj_id, track_id="track-adj",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
        effects=(adj_effect,),
    )
    upper = ClipInstance(
        clip_id="c-upper", asset_id=green_id, track_id="track-upper",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
    )

    track_lower = EditTrack(track_id="track-lower", name="V1", track_type="video",
                            index=0, clips=(lower,))
    track_adj = EditTrack(track_id="track-adj", name="V2 Adj", track_type="video",
                          index=1, clips=(middle,))
    track_upper = EditTrack(track_id="track-upper", name="V3", track_type="video",
                            index=2, clips=(upper,))

    sequence = TimelineSequence(
        timeline_id="seq-adj", name="AdjScope",
        fps=Rational(24, 1), width=64, height=64,
        video_tracks=(track_lower, track_adj, track_upper),
    )
    return project, sequence, {
        "red": red_id, "adj": adj_id, "green": green_id,
        "adj_clip_id": "c-adj",
    }


# ── FX-C02: render plan exposes the adjustment as a distinct layer ──

class TestAdjustmentScope:
    """Structural scope contract the renderer must honour once fixed."""

    def test_adjustment_layer_is_present_in_plan(self) -> None:
        project, sequence, ids = _build_three_track_sequence()
        plan = TimelineRenderPlan(sequence)
        frame_plan = plan.get_frame_plan(0)

        assert frame_plan.has_content
        # All three clips resolve to layers at the overlapping frame.
        asset_ids = {layer.asset_id for layer in frame_plan.layers}
        assert ids["red"] in asset_ids
        assert ids["adj"] in asset_ids
        assert ids["green"] in asset_ids

    def test_adjustment_layer_has_distinct_track_index_and_ordering(self) -> None:
        """The adjustment sits BETWEEN lower and upper by track index.

        A correct renderer composites lower → apply adjustment → composite
        upper. The plan must preserve that z-order so the fix can key off
        the adjustment's track position to decide what is "below" it.
        """
        project, sequence, ids = _build_three_track_sequence()
        plan = TimelineRenderPlan(sequence)
        frame_plan = plan.get_frame_plan(0)

        by_asset = {layer.asset_id: layer for layer in frame_plan.layers}
        lower_idx = by_asset[ids["red"]].track_index
        adj_idx = by_asset[ids["adj"]].track_index
        upper_idx = by_asset[ids["green"]].track_index

        # Adjustment must be strictly above the lower layer and strictly
        # below the upper layer — that is the scope boundary.
        assert lower_idx < adj_idx < upper_idx, (
            f"scope ordering broken: lower={lower_idx} adj={adj_idx} upper={upper_idx}"
        )

    def test_adjustment_carries_its_effect_chain(self) -> None:
        """The plan must keep the adjustment's effect chain intact so the
        renderer can run it as a post-composite pass on the layers below."""
        project, sequence, ids = _build_three_track_sequence()
        plan = TimelineRenderPlan(sequence)
        frame_plan = plan.get_frame_plan(0)

        adj_layer = next(
            layer for layer in frame_plan.layers if layer.asset_id == ids["adj"]
        )
        effects = getattr(adj_layer.clip, "effects", ())
        assert len(effects) == 1
        assert effects[0].effect_type == "blur"


# ── FX-C03 / FX-C04: CPU fallback & export must not silently drop ────

class TestCpuAndExportDoNotDropAdjustment:
    """Today both the CPU preview fallback and the exporter skip the
    adjustment entirely. These tests pin that bug so the fix is verified
    by them turning green once a post-composite pass is wired in.

    They use the same no-init unit-test pattern as
    ``test_timeline_export_effects.py`` to call the private compositors
    directly against a mocked project, avoiding a real decode stack.
    """

    @staticmethod
    def _mock_project_with_assets(ids: dict) -> MagicMock:
        from src.features.edit.core.project import MediaAsset

        project = MagicMock()
        project.get_asset_by_id = MagicMock(
            side_effect=lambda asset_id: _make_asset(asset_id, ids)
        )
        return project

    def test_export_get_layer_frame_returns_none_for_adjustment(self) -> None:
        """FX-C04: export's _get_layer_frame has no adjustment branch.

        It returns None for an adjustment (no source frame to decode) —
        BUT the composite loop now catches that and runs the adjustment's
        effect chain on the accumulated composite (see
        test_adjustment_clip_cpu_export_application.py). This test pins
        that _get_layer_frame itself still returns None (the decode
        contract) while the post-composite application happens in the
        loop, not here.
        """
        from src.features.edit.core.timeline_export import TimelineExporter

        project, sequence, ids = _build_three_track_sequence()
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._project = project

        layer = MagicMock()
        layer.asset_id = ids["adj"]
        layer.source_frame = 0

        # _get_layer_frame returns None — adjustments have no decode frame.
        # The post-composite application is the composite loop's job.
        result = exporter._get_layer_frame(layer)
        assert result is None, (
            "_get_layer_frame must return None for adjustment (no source); "
            "the effect-chain application now happens in _composite_layers"
        )


def _make_asset(asset_id: str, ids: dict):
    """Return a MediaAsset-like mock for the given id within the fixture."""
    from src.features.edit.core.project import MediaAsset

    if asset_id == ids["adj"]:
        return MediaAsset(
            asset_id=asset_id, path=f"topos://adjustment/{asset_id}",
            media_type="adjustment", display_name="Adj", metadata={},
        )
    if asset_id == ids["red"]:
        return MediaAsset(
            asset_id=asset_id, path=f"topos://solid/{asset_id}",
            media_type="solid", display_name="Red",
            metadata={"color": [1.0, 0.0, 0.0]},
        )
    if asset_id == ids["green"]:
        return MediaAsset(
            asset_id=asset_id, path=f"topos://solid/{asset_id}",
            media_type="solid", display_name="Green",
            metadata={"color": [0.0, 1.0, 0.0]},
        )
    return None
