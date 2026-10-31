"""Map the Motion Graphic HTML pipeline onto an AgentRun (plan §A1 §9-12 / §2.6).

The current Motion Graphic path (``start_html_generate_chat`` → HTML response
→ quality gate → browser verify → confirm card → ``MotionGraphicClipCommand``)
is a set of special-case handlers in the controller.  This coordinator is the
typed bridge that expresses the same flow as unified ``AgentRun`` state
transitions and ``AgentEvent`` emissions, so Motion Graphic becomes observable
through the same Run Drawer / metrics / recovery machinery as ordinary chat.

It is deliberately a **staging + gate** layer: it never writes the timeline
itself.  ``confirm_and_write`` checks the single write gate (staged +
verified + writable + not stale) and only then flips ``HtmlArtifact.writable``
and advances the run to ``EXECUTING``; the actual ``MotionGraphicClipCommand``
is issued by the host (plan §2.5 §9).

Invariants baked in (plan §2.5 / §2.6):

- **One confirm action.**  ``request_confirmation`` is the only path to
  ``WAITING_APPROVAL``; a stale artifact or changed scene blocks the write.
- **Artifacts staged, not applied.**  ``stage_html`` emits ``artifact.staged``;
  the raw HTML lives in the artifact store, referenced by hash.
- **Re-verification on resume.**  A restored artifact is never trusted without
  fresh verification (plan §2.7 / §10.2 §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
    AgentTask,
    AgentTaskKind,
    ApprovalRequest,
    ApprovalRisk,
    ArtifactRef,
    ArtifactKind,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.motion_graphic_artifacts import (
    HtmlArtifact,
    MotionGraphicBuildRequest,
    scene_is_stale,
)


@dataclass(frozen=True)
class MotionGraphicWriteDecision:
    """Outcome of a confirm-and-write attempt (plan §2.5 §9)."""

    writable: bool
    artifact: HtmlArtifact | None = None
    reason: str = ""


class MotionGraphicRunCoordinator:
    """Stage Motion Graphic HTML artifacts against a unified AgentRun."""

    def __init__(self, orchestrator: AgentOrchestrator) -> None:
        self._orchestrator = orchestrator

    # ------------------------------------------------------------------ #
    # Build start
    # ------------------------------------------------------------------ #

    def start_build(self, request: MotionGraphicBuildRequest) -> str:
        """Create the task + run for a Motion Graphic build."""

        task = AgentTask(
            task_id=request.task_id,
            session_id=request.run_id or request.task_id,
            title=(request.user_brief or "Motion Graphic")[:60],
            user_goal=request.user_brief,
            kind=AgentTaskKind.MOTION_GRAPHIC,
        )
        return self._orchestrator.start_task(task, mode="motion_graphic")

    # ------------------------------------------------------------------ #
    # Staging + verification
    # ------------------------------------------------------------------ #

    def stage_html(
        self,
        run_id: str,
        *,
        raw_html_hash: str,
        sanitized_html_hash: str,
        html_payload_ref: str,
        page_count: int,
        viewport: tuple[int, int] = (0, 0),
    ) -> HtmlArtifact:
        """Record a sanitised HTML document as a staged artifact.

        Emits ``artifact.staged`` then ``run.verification_started`` so the UI
        shows "HTML 已收齐 → 安全检查 → 浏览器验证" (plan §A3 §9).  The
        artifact is **not writable** — only confirmation flips that.
        """

        artifact = HtmlArtifact(
            raw_html_hash=raw_html_hash,
            sanitized_html_hash=sanitized_html_hash,
            html_payload_ref=html_payload_ref,
            page_count=page_count,
            viewport_width=viewport[0],
            viewport_height=viewport[1],
            writable=False,
        )
        self._orchestrator.emit(
            run_id,
            AgentEventKind.ARTIFACT_STAGED,
            payload={
                "artifact_id": artifact.artifact_id,
                "page_count": page_count,
                "sanitized_html_hash": sanitized_html_hash,
            },
        )
        self._orchestrator.emit(run_id, AgentEventKind.RUN_VERIFICATION_STARTED)
        return artifact

    def record_verification(
        self,
        run_id: str,
        artifact: HtmlArtifact,
        *,
        render_evidence: dict[str, Any] | None = None,
        quality_report: dict[str, Any] | None = None,
        sanitizer_version: str = "",
    ) -> HtmlArtifact:
        """Attach browser-capture evidence to a staged artifact."""

        from dataclasses import replace

        updated = replace(
            artifact,
            render_evidence=render_evidence or {},
            quality_report=quality_report or {},
            sanitizer_version=sanitizer_version or artifact.sanitizer_version,
        )
        self._orchestrator.emit(run_id, AgentEventKind.RUN_VERIFICATION_FINISHED)
        return updated

    # ------------------------------------------------------------------ #
    # Confirmation gate (plan §2.5 §8 / §2.6)
    # ------------------------------------------------------------------ #

    def request_confirmation(
        self,
        run_id: str,
        request: MotionGraphicBuildRequest,
        artifact: HtmlArtifact,
    ) -> ApprovalRequest:
        """Move to WAITING_APPROVAL and surface the single confirm card."""


        # The state machine requires PLANNING before WAITING_APPROVAL; if the
        # run is still gathering context (HTML just arrived), advance it first.
        run = self._orchestrator.get_run(run_id)
        if run is not None and run.state == AgentRunState.GATHERING_CONTEXT:
            self._orchestrator.mark_context_ready(run_id)
        approval = ApprovalRequest(
            run_id=run_id,
            tool_name="topos.apply_motion_graphic_clip",
            risk=ApprovalRisk.WRITE,
            scope_summary=f"{artifact.page_count} 页 HTML 动画",
            expected_change="插入 Motion Graphic Clip 到时间线",
            can_undo=True,
        )
        self._orchestrator.request_approval(run_id, approval)
        return approval

    def confirm_and_write(
        self,
        run_id: str,
        request: MotionGraphicBuildRequest,
        artifact: HtmlArtifact,
        *,
        current_scene_revision: int | None = None,
    ) -> MotionGraphicWriteDecision:
        """Check the write gate and, if it passes, mark the artifact writable.

        Confirmation is the act that grants ``writable``, so this checks the
        other preconditions (staged + verified + not stale) but **not**
        ``writable`` itself.  :func:`can_write_artifact` (which includes the
        writable flag) is the gate an *external* caller uses to double-check an
        already-confirmed artifact before issuing the host command.

        Returns a :class:`MotionGraphicWriteDecision`.  When ``writable`` is
        True the host may issue ``MotionGraphicClipCommand``; the coordinator
        advances the run to ``EXECUTING`` so the timeline write is observable.
        """

        from dataclasses import replace

        if not artifact.is_staged:
            return MotionGraphicWriteDecision(
                writable=False, artifact=artifact, reason="HTML artifact 尚未完成清洗或未就绪"
            )
        if not artifact.has_verification_evidence:
            return MotionGraphicWriteDecision(
                writable=False, artifact=artifact, reason="缺少真实浏览器验证证据"
            )
        if scene_is_stale(
            request, artifact, current_scene_revision=current_scene_revision
        ):
            return MotionGraphicWriteDecision(
                writable=False,
                artifact=artifact,
                reason="场景在确认后已变更（scene revision / fingerprint 不一致）",
            )
        # All preconditions met: confirmation grants writable + advances to EXECUTING.
        writable_artifact = replace(artifact, writable=True)
        self._orchestrator.grant_approval(run_id)
        return MotionGraphicWriteDecision(
            writable=True, artifact=writable_artifact, reason=""
        )

    # ------------------------------------------------------------------ #
    # Completion
    # ------------------------------------------------------------------ #

    def complete_write(self, run_id: str, *, success: bool = True) -> Any:
        """Advance to VERIFYING → terminal after the host wrote the clip."""

        self._orchestrator.begin_verification(run_id)
        return self._orchestrator.finish_verification(run_id, success=success)

    def fail(self, run_id: str, *, reason: str = "") -> Any:
        """Move a Motion Graphic run straight to FAILED (e.g. sanitize rejected)."""

        return self._orchestrator.fail(run_id, reason=reason)


def artifact_ref_from_html(artifact: HtmlArtifact) -> ArtifactRef:
    """Build an :class:`ArtifactRef` for orchestrator staging / history."""

    return ArtifactRef(
        kind=ArtifactKind.HTML_DOCUMENT,
        location=artifact.html_payload_ref,
        payload_hash=artifact.sanitized_html_hash,
        provenance=f"motion_graphic:{artifact.artifact_id}",
    )


__all__ = [
    "MotionGraphicRunCoordinator",
    "MotionGraphicWriteDecision",
    "artifact_ref_from_html",
]
