"""AQ-01: structured motion/transition/cost fields on the narrative beat.

The beat sheet already carried timing + copy; this pins the additive structured
fields (motion_preset_id / transition / render_cost / asset_refs) so the planner
can map a beat directly to a command, and so old proposals still load.
"""

from __future__ import annotations

from src.features.teaching_animation.ai.proposal import CreativeProposal, NarrativeBeat


def test_structured_fields_round_trip():
    beat = NarrativeBeat(
        "b1", "开场", 0.0, 3.0,
        on_screen_copy="AI Agent",
        motion_preset_id="pop", transition="dissolve",
        render_cost="medium", asset_refs=("logo", "bg"),
    )
    restored = NarrativeBeat.from_dict(beat.to_dict())
    assert restored == beat
    assert restored.motion_preset_id == "pop"
    assert restored.transition == "dissolve"
    assert restored.render_cost == "medium"
    assert restored.asset_refs == ("logo", "bg")


def test_structured_fields_default_empty_for_legacy_proposals():
    # A dict produced before the structured fields existed must still load.
    legacy = {"beat_id": "x", "label": "y", "start_seconds": 0.0, "duration_seconds": 1.0}
    beat = NarrativeBeat.from_dict(legacy)
    assert beat.motion_preset_id == ""
    assert beat.transition == ""
    assert beat.render_cost == ""
    assert beat.asset_refs == ()


def test_positional_construction_unchanged():
    # Existing callers pass 4 positional args + keyword on_screen_copy.
    beat = NarrativeBeat("b1", "开场", 0.0, 3.0, on_screen_copy="AI Agent")
    assert beat.beat_id == "b1" and beat.on_screen_copy == "AI Agent"


def test_proposal_carries_structured_design_plan():
    # The user can read off each segment's timing, motion, transition and cost
    # before confirmation (AQ-01 acceptance).
    proposal = CreativeProposal(
        proposal_id="p", brief_id="b", brief_revision=1, brief_hash="h",
        title="t", summary="s",
        beats=(
            NarrativeBeat("b1", "开场", 0.0, 3.0, motion_preset_id="pop", transition="dissolve",
                          render_cost="low"),
            NarrativeBeat("b2", "正文", 3.0, 9.0, motion_preset_id="slide", render_cost="medium",
                          asset_refs=("chart",)),
        ),
    )
    restored = CreativeProposal.from_dict(proposal.to_dict())
    assert restored.beats[0].motion_preset_id == "pop"
    assert restored.beats[1].asset_refs == ("chart",)
    # The design plan is queryable per beat.
    plan = [(b.beat_id, b.transition, b.render_cost) for b in restored.beats]
    assert plan == [("b1", "dissolve", "low"), ("b2", "", "medium")]
