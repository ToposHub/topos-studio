"""MotionGraphicRunCoordinator tests (plan §A1 §9-12 / §2.5 / §2.6).

Validates that the Motion Graphic HTML pipeline maps onto unified AgentRun
state transitions with a single, guarded confirm→write gate.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.motion_graphic_artifacts import (
    MotionGraphicBuildRequest,
    can_write_artifact,
)
from src.features.edit.ai_editing.motion_graphic_run import (
    MotionGraphicRunCoordinator,
    artifact_ref_from_html,
)


@pytest.fixture
def harness():
    orch = AgentOrchestrator()
    events: list = []
    orch.subscribe(lambda e: events.append(e))
    coord = MotionGraphicRunCoordinator(orch)
    return orch, coord, events


def _request(**overrides):
    base = dict(
        task_id="t1",
        scene_id="s1",
        scene_revision=3,
        context_fingerprint="fp1",
        canvas_width=1920,
        canvas_height=1080,
        fps=30.0,
        user_brief="3 pages",
    )
    base.update(overrides)
    return MotionGraphicBuildRequest(**base)


# ---------------------------------------------------------------------- #
# Build start + staging
# ---------------------------------------------------------------------- #


class TestStartAndStage:
    def test_start_build_creates_motion_graphic_run(self, harness):
        orch, coord, events = harness
        run_id = coord.start_build(_request())
        run = orch.get_run(run_id)
        assert run.mode == "motion_graphic"
        assert run.state == AgentRunState.GATHERING_CONTEXT

    def test_stage_html_emits_artifact_and_verification(self, harness):
        orch, coord, events = harness
        run_id = coord.start_build(_request())
        art = coord.stage_html(
            run_id,
            raw_html_hash="rh",
            sanitized_html_hash="sh",
            html_payload_ref="store://x",
            page_count=3,
            viewport=(1920, 1080),
        )
        assert art.writable is False
        assert art.page_count == 3
        kinds = [e.kind for e in events]
        assert AgentEventKind.ARTIFACT_STAGED in kinds
        assert AgentEventKind.RUN_VERIFICATION_STARTED in kinds

    def test_record_verification_adds_evidence(self, harness):
        orch, coord, _ = harness
        run_id = coord.start_build(_request())
        art = coord.stage_html(
            run_id, raw_html_hash="r", sanitized_html_hash="s",
            html_payload_ref="ref", page_count=2,
        )
        assert not art.has_verification_evidence
        verified = coord.record_verification(
            run_id, art, render_evidence={"frames": 10}, quality_report={"ok": True}
        )
        assert verified.has_verification_evidence
        assert verified.quality_report == {"ok": True}


# ---------------------------------------------------------------------- #
# Confirm → write gate
# ---------------------------------------------------------------------- #


class TestConfirmAndWrite:
    def _stage_and_verify(self, coord, orch, request):
        run_id = coord.start_build(request)
        art = coord.stage_html(
            run_id, raw_html_hash="r", sanitized_html_hash="s",
            html_payload_ref="ref", page_count=3,
        )
        verified = coord.record_verification(run_id, art, render_evidence={"frames": 5})
        return run_id, verified

    def test_confirm_grants_writable_and_advances(self, harness):
        orch, coord, _ = harness
        req = _request()
        run_id, verified = self._stage_and_verify(coord, orch, req)
        coord.request_confirmation(run_id, req, verified)
        assert orch.get_run(run_id).state == AgentRunState.WAITING_APPROVAL
        dec = coord.confirm_and_write(run_id, req, verified, current_scene_revision=3)
        assert dec.writable
        assert dec.artifact.writable is True
        assert orch.get_run(run_id).state == AgentRunState.EXECUTING

    def test_external_gate_validates_writable_artifact(self, harness):
        orch, coord, _ = harness
        req = _request()
        run_id, verified = self._stage_and_verify(coord, orch, req)
        coord.request_confirmation(run_id, req, verified)
        dec = coord.confirm_and_write(run_id, req, verified, current_scene_revision=3)
        # can_write_artifact re-checks the now-writable artifact (all gates incl. writable).
        ok, _ = can_write_artifact(req, dec.artifact, current_scene_revision=3)
        assert ok

    def test_stale_scene_blocks_write(self, harness):
        orch, coord, _ = harness
        req = _request()
        run_id, verified = self._stage_and_verify(coord, orch, req)
        coord.request_confirmation(run_id, req, verified)
        dec = coord.confirm_and_write(run_id, req, verified, current_scene_revision=99)
        assert not dec.writable
        assert "变更" in dec.reason

    def test_missing_evidence_blocks_write(self, harness):
        orch, coord, _ = harness
        req = _request()
        run_id = coord.start_build(req)
        # staged but never verified (no evidence)
        art = coord.stage_html(
            run_id, raw_html_hash="r", sanitized_html_hash="s",
            html_payload_ref="ref", page_count=1,
        )
        coord.request_confirmation(run_id, req, art)
        dec = coord.confirm_and_write(run_id, req, art, current_scene_revision=3)
        assert not dec.writable
        assert "验证" in dec.reason

    def test_complete_write_reaches_succeeded(self, harness):
        orch, coord, _ = harness
        req = _request()
        run_id, verified = self._stage_and_verify(coord, orch, req)
        coord.request_confirmation(run_id, req, verified)
        coord.confirm_and_write(run_id, req, verified, current_scene_revision=3)
        coord.complete_write(run_id, success=True)
        assert orch.get_run(run_id).state == AgentRunState.SUCCEEDED


# ---------------------------------------------------------------------- #
# Failure path + helper
# ---------------------------------------------------------------------- #


class TestFailureAndHelper:
    def test_fail_moves_to_failed(self, harness):
        orch, coord, _ = harness
        run_id = coord.start_build(_request())
        coord.fail(run_id, reason="sanitize rejected")
        assert orch.get_run(run_id).state == AgentRunState.FAILED

    def test_artifact_ref_from_html(self, harness):
        from src.features.edit.ai_editing.motion_graphic_artifacts import HtmlArtifact

        art = HtmlArtifact(
            sanitized_html_hash="abc",
            html_payload_ref="store://x",
            page_count=2,
        )
        ref = artifact_ref_from_html(art)
        assert ref.kind == "html_document"
        assert ref.payload_hash == "abc"
        assert ref.location == "store://x"
