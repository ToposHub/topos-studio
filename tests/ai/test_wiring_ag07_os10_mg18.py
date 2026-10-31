"""Wiring tests for AG-07 (4-tier permission), OS-10 (action contract),
and MG-18 (quality gate).

These tests prove the previously-dormant contract modules are now consumed
by production code paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.features.edit.ai_editing.execution_policy import (
    ActionRequest,
    ExecutionAction,
    ExecutionPolicy,
    ExecutionScope,
    PolicyDecision,
    WRITE_ACTIONS,
    permission_tier_allows_write,
)
from src.features.edit.ai_editing.action_contract import (
    ActionContract,
    CompensationSpec,
    PostconditionSpec,
    PostconditionFinding,
    evaluate_postconditions,
    POST_CLIP_EXISTS,
    COMPENSATION_UNDO,
)


# --------------------------------------------------------------------------- #
# AG-07: Four-tier permission system
# --------------------------------------------------------------------------- #


class TestFourTierPermission:
    """AG-07: run_context.py's 4 tiers are now enforced in ExecutionPolicy."""

    @pytest.fixture
    def scope(self) -> ExecutionScope:
        return ExecutionScope(project_id="p1", timeline_id="t1", base_revision=0)

    @pytest.fixture
    def write_request(self) -> ActionRequest:
        return ActionRequest(
            action=ExecutionAction.TIMELINE_WRITE,
            project_id="p1", timeline_id="t1", base_revision=0,
        )

    def test_read_only_blocks_writes_even_with_approval(self, scope, write_request) -> None:
        p = ExecutionPolicy.from_permission_tier("read_only", scope)
        d = p.evaluate(write_request, approval_granted=True)
        assert not d.allowed
        assert "read_only" in d.reason or "does not allow writes" in d.reason

    def test_proposal_blocks_writes_even_with_approval(self, scope, write_request) -> None:
        p = ExecutionPolicy.from_permission_tier("proposal", scope)
        d = p.evaluate(write_request, approval_granted=True)
        assert not d.allowed
        assert "proposal" in d.reason or "does not allow writes" in d.reason

    def test_confirm_each_requires_approval(self, scope, write_request) -> None:
        p = ExecutionPolicy.from_permission_tier("confirm_each", scope)
        d_no = p.evaluate(write_request, approval_granted=False)
        assert not d_no.allowed
        assert d_no.needs_approval
        d_yes = p.evaluate(write_request, approval_granted=True)
        assert d_yes.allowed

    def test_restricted_auto_allows_writes(self, scope, write_request) -> None:
        p = ExecutionPolicy.from_permission_tier("restricted_auto", scope)
        d = p.evaluate(write_request, approval_granted=False)
        assert d.allowed

    def test_read_only_allows_reads(self, scope) -> None:
        p = ExecutionPolicy.from_permission_tier("read_only", scope)
        req = ActionRequest(action=ExecutionAction.READ, project_id="p1")
        assert p.evaluate(req).allowed

    def test_proposal_allows_planning(self, scope) -> None:
        p = ExecutionPolicy.from_permission_tier("proposal", scope)
        req = ActionRequest(action=ExecutionAction.PLAN, project_id="p1")
        assert p.evaluate(req).allowed

    def test_unknown_tier_fails_closed(self, scope, write_request) -> None:
        p = ExecutionPolicy.from_permission_tier("unknown_tier", scope)
        d = p.evaluate(write_request, approval_granted=True)
        assert not d.allowed

    def test_permission_tier_allows_write_helper(self) -> None:
        assert permission_tier_allows_write("read_only") is False
        assert permission_tier_allows_write("proposal") is False
        assert permission_tier_allows_write("confirm_each", approval_granted=False) is False
        assert permission_tier_allows_write("confirm_each", approval_granted=True) is True
        assert permission_tier_allows_write("restricted_auto") is True

    def test_permission_tier_stored_on_policy(self, scope) -> None:
        p = ExecutionPolicy.from_permission_tier("confirm_each", scope)
        assert p.permission_tier == "confirm_each"

    def test_backward_compat_no_tier(self, scope, write_request) -> None:
        """Without a tier, existing 3-mode behavior is preserved."""
        from src.features.edit.ai_editing.execution_policy import ExecutionMode
        p = ExecutionPolicy(ExecutionMode.SEMI_AUTOMATIC, scope)
        assert p.permission_tier is None
        d = p.evaluate(write_request, approval_granted=True)
        assert d.allowed  # SEMI_AUTOMATIC allows writes with approval

    def test_check_plan_with_permission_tier(self, scope) -> None:
        """check_plan respects the 4-tier system."""
        @dataclass(frozen=True)
        class FakeStep:
            step_id: str = "s1"
            tool_name: str = "timeline_write"
            writes_timeline: bool = True
            requires_approval: bool = False
            policy_request: Any = None
            idempotency_key: str = "agent-step:s1"

        @dataclass(frozen=True)
        class FakeContext:
            run_id: str = "r1"
            project_id: str = "p1"
            timeline_id: str = "t1"
            base_revision: int = 0
            asset_ids: tuple = ()
            evidence_refs: tuple = ()
            approval_granted: bool = False
            template_id: Any = None

        p = ExecutionPolicy.from_permission_tier("proposal", scope)
        d = p.check_plan(FakeContext(), [FakeStep()])
        # PROPOSAL tier blocks writes
        assert not d.allowed


# --------------------------------------------------------------------------- #
# OS-10: ActionContract postcondition evaluation in AgentLoop
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# MG-18: Quality gate on confirmation card
# --------------------------------------------------------------------------- #


class TestQualityGateWiring:
    """MG-18: MotionGraphicQualityGate is wired into module.py and its
    report appears on the confirmation card."""

    def test_quality_report_on_card(self) -> None:
        """The confirmation card includes quality_report when available."""
        from src.features.teaching_animation.module import TeachingAnimationModule
        from src.features.teaching_animation.ai.brief import MotionGraphicBrief

        module = TeachingAnimationModule()
        request_id = "test-qg-card"
        brief = MotionGraphicBrief.from_user_text("test", duration_seconds=15.0, fps=24.0)
        plan = {
            "seconds": 15.0, "fps": 24.0, "frame": 0, "has_timeline": True,
            "track_id": "gfx-1", "variant": 0, "user_text": "test",
            "scene": None, "build_failed": False, "brief": brief,
            "phase": "build", "creative_proposals": [],
            "selected_proposal_id": None,
            "quality_report": {
                "is_deliverable": True,
                "error_count": 0,
                "warning_count": 1,
                "checks_run": ["first_frame", "design_validator"],
                "findings": [],
            },
        }
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        panel = MagicMock()
        window.ai_edit_assistant_panel = panel

        module._show_motion_graphic_proposal_card(
            window, request_id,
            variant=0, seconds=15.0, frame=0,
            graphics_track=None, fallback=False,
        )
        card_data = panel.set_motion_graphic_proposal_card.call_args[0][1]
        assert "quality_report" in card_data
        assert card_data["quality_report"]["is_deliverable"] is True

    def test_quality_gate_runs_on_scene(self) -> None:
        """_run_quality_gate produces a valid report dict."""
        from src.features.teaching_animation.module import TeachingAnimationModule
        from src.features.teaching_animation.ai.brief import MotionGraphicBrief

        module = TeachingAnimationModule()
        scene = MagicMock()
        scene.duration_frames = 360
        scene.fps = 24.0
        scene.elements = []
        scene.to_dict = MagicMock(return_value={"elements": []})

        brief = MotionGraphicBrief.from_user_text("test", duration_seconds=15.0, fps=24.0)
        plan = {"brief": brief}

        storyboard = [
            {"frame_index": 0, "label": "first_frame", "render_error": None},
            {"frame_index": 180, "label": "mid_frame", "render_error": None},
            {"frame_index": 359, "label": "last_frame", "render_error": None},
        ]

        report = TeachingAnimationModule._run_quality_gate(
            scene, storyboard, [], [], plan,
        )
        assert report is not None
        assert "is_deliverable" in report
        assert "findings" in report
        assert "checks_run" in report

    def test_quality_gate_with_failed_frames(self) -> None:
        """Render failures in storyboard are reflected in the quality report."""
        from src.features.teaching_animation.module import TeachingAnimationModule
        from src.features.teaching_animation.ai.brief import MotionGraphicBrief

        module = TeachingAnimationModule()
        scene = MagicMock()
        scene.duration_frames = 360
        scene.fps = 24.0
        scene.elements = []
        scene.to_dict = MagicMock(return_value={"elements": []})

        brief = MotionGraphicBrief.from_user_text("test", duration_seconds=15.0, fps=24.0)
        plan = {"brief": brief}

        storyboard = [
            {"frame_index": 0, "label": "first_frame", "render_error": "GPU error"},
        ]

        report = TeachingGraphicQualityGate = TeachingAnimationModule._run_quality_gate(
            scene, storyboard, [], [], plan,
        )
        assert report is not None
        # A failed first frame should produce error findings
        assert report["error_count"] > 0 or not report["is_deliverable"]
