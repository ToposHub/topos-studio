"""OS-06: Mission / Plan workspace view-model tests.

Verifies that ``build_mission_workspace_view`` and
``workspace_from_run_result`` produce correct UI-ready dicts for various
agent states, and that diagnostic classification, action availability,
and progress calculation are correct.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.features.edit.ai_editing.mission_workspace import (
    build_mission_workspace_view,
    workspace_from_run_result,
)


# --------------------------------------------------------------------------- #
# build_mission_workspace_view
# --------------------------------------------------------------------------- #


class TestBuildWorkspaceView:
    def test_running_status(self) -> None:
        data = build_mission_workspace_view(status="running")
        assert data["status"] == "running"
        assert data["status_badge"]["label"] == "Running"
        assert data["status_badge"]["color"] == "#3b82f6"
        assert "cancel" in data["actions"]
        assert "pause" in data["actions"]

    def test_succeeded_status(self) -> None:
        data = build_mission_workspace_view(status="succeeded")
        assert data["status_badge"]["label"] == "Succeeded"
        assert data["status_badge"]["color"] == "#22c55e"
        assert "close" in data["actions"]

    def test_waiting_for_user_status(self) -> None:
        data = build_mission_workspace_view(status="waiting_for_user")
        assert data["status_badge"]["label"] == "Waiting for user"
        assert "resume" in data["actions"]
        assert "retry" in data["actions"]

    def test_failed_status(self) -> None:
        data = build_mission_workspace_view(status="failed")
        assert data["status_badge"]["label"] == "Failed"
        assert "retry" in data["actions"]

    def test_cancelled_status(self) -> None:
        data = build_mission_workspace_view(status="cancelled")
        assert data["status_badge"]["label"] == "Cancelled"

    def test_unknown_status_fallback(self) -> None:
        data = build_mission_workspace_view(status="custom_state")
        assert data["status_badge"]["label"] == "custom_state"
        assert data["status_badge"]["color"] == "#6b7280"

    def test_progress_calculation(self) -> None:
        data = build_mission_workspace_view(
            status="running",
            completed_step_ids=("s0", "s1", "s2"),
            total_steps=5,
        )
        assert data["completed_steps"] == 3
        assert data["total_steps"] == 5
        assert data["progress_pct"] == 60.0

    def test_zero_total_steps(self) -> None:
        data = build_mission_workspace_view(status="running", total_steps=0)
        assert data["progress_pct"] == 0.0
        assert data["completed_steps"] == 0

    def test_cost_rounding(self) -> None:
        data = build_mission_workspace_view(status="running", cost_units=3.14159265)
        assert data["cost_units"] == 3.1416  # Rounded to 4 decimals

    def test_diagnostics_classification(self) -> None:
        data = build_mission_workspace_view(
            status="failed",
            diagnostics=(
                "hard error in render",
                "stale revision warning",
                "retrying with fallback",
                "step completed",
            ),
        )
        diags = data["diagnostics"]
        assert len(diags["errors"]) == 1
        assert "hard error" in diags["errors"][0]
        assert len(diags["warnings"]) == 2  # "stale revision warning" + "retrying..."
        assert len(diags["info"]) == 1

    def test_rollback_failures_listed(self) -> None:
        data = build_mission_workspace_view(
            status="failed",
            rollback_failures=("undo insert failed",),
        )
        assert len(data["rollback_failures"]) == 1
        assert "undo insert" in data["rollback_failures"][0]

    def test_risk_flags(self) -> None:
        data = build_mission_workspace_view(
            status="running",
            steps_writing_timeline=2,
            steps_requiring_approval=1,
        )
        assert data["risk_flags"]["writes_timeline"] == 2
        assert data["risk_flags"]["requires_approval"] == 1

    def test_empty_diagnostics(self) -> None:
        data = build_mission_workspace_view(status="running")
        assert data["diagnostics"]["errors"] == []
        assert data["diagnostics"]["warnings"] == []
        assert data["diagnostics"]["info"] == []


# --------------------------------------------------------------------------- #
# workspace_from_run_result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _MockStep:
    step_id: str
    tool_name: str
    writes_timeline: bool = False
    requires_approval: bool = False


@dataclass(frozen=True)
class _MockPlan:
    steps: tuple[_MockStep, ...]
    plan_hash: str = "planhash1234567"


@dataclass(frozen=True)
class _MockCheckpoint:
    run_id: str = "run-abc"
    status: Any = None
    phase: Any = None
    current_step_id: str = "s1"
    completed_step_ids: tuple[str, ...] = ("s0",)
    cost_units: float = 1.5
    reason: str = ""


@dataclass(frozen=True)
class _MockStatus:
    value: str = "running"


@dataclass(frozen=True)
class _MockPhase:
    value: str = "act"


@dataclass(frozen=True)
class _MockRunResult:
    status: Any = None
    checkpoint: Any = None
    plan: Any = None
    diagnostics: tuple[str, ...] = ()
    rollback_failures: tuple[str, ...] = ()


class TestWorkspaceFromRunResult:
    def test_basic_conversion(self) -> None:
        steps = (
            _MockStep("s0", "analyze_assets"),
            _MockStep("s1", "insert_clip", writes_timeline=True, requires_approval=True),
        )
        result = _MockRunResult(
            status=_MockStatus("running"),
            checkpoint=_MockCheckpoint(
                status=_MockStatus("running"),
                phase=_MockPhase("act"),
            ),
            plan=_MockPlan(steps=steps),
            diagnostics=("step done",),
        )
        data = workspace_from_run_result(result)
        assert data["status"] == "running"
        assert data["phase"] == "act"
        assert data["current_step_id"] == "s1"
        assert data["current_tool_name"] == "insert_clip"
        assert data["completed_steps"] == 1
        assert data["total_steps"] == 2
        assert data["progress_pct"] == 50.0
        assert data["cost_units"] == 1.5
        assert data["risk_flags"]["writes_timeline"] == 1
        assert data["risk_flags"]["requires_approval"] == 1

    def test_no_plan(self) -> None:
        result = _MockRunResult(
            status=_MockStatus("succeeded"),
            checkpoint=_MockCheckpoint(
                status=_MockStatus("succeeded"),
                phase=_MockPhase("verify"),
                current_step_id="",
            ),
            plan=None,
        )
        data = workspace_from_run_result(result)
        assert data["total_steps"] == 0
        assert data["progress_pct"] == 0.0
        assert data["current_tool_name"] == ""

    def test_failed_with_diagnostics(self) -> None:
        result = _MockRunResult(
            status=_MockStatus("failed"),
            checkpoint=_MockCheckpoint(
                status=_MockStatus("failed"),
                phase=_MockPhase("recover"),
                reason="render validation error",
            ),
            plan=_MockPlan(steps=(_MockStep("s0", "render"),)),
            diagnostics=("render failed at frame 120",),
            rollback_failures=("undo failed",),
        )
        data = workspace_from_run_result(result)
        assert data["status"] == "failed"
        assert "render failed" in data["diagnostics"]["errors"][0]
        assert len(data["rollback_failures"]) == 1
        assert data["reason"] == "render validation error"

    def test_step_not_in_plan(self) -> None:
        """When current_step_id doesn't match any plan step, tool is empty."""
        result = _MockRunResult(
            status=_MockStatus("running"),
            checkpoint=_MockCheckpoint(
                status=_MockStatus("running"),
                phase=_MockPhase("act"),
                current_step_id="unknown_step",
            ),
            plan=_MockPlan(steps=(_MockStep("s0", "analyze"),)),
        )
        data = workspace_from_run_result(result)
        assert data["current_tool_name"] == ""


# --------------------------------------------------------------------------- #
# UI panel rendering (with Qt offscreen)
# --------------------------------------------------------------------------- #


class TestPanelRendering:
    """Smoke test that the panel method doesn't crash with valid data."""

    def test_set_mission_workspace_smoke(self) -> None:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        panel = AiEditAssistantPanel()

        data = build_mission_workspace_view(
            status="running", phase="act",
            current_step_id="s1", current_tool_name="render",
            completed_step_ids=("s0",), total_steps=3,
            cost_units=1.2, plan_hash="abc",
            diagnostics=("some info",),
        )
        # Should not raise
        panel.set_mission_workspace(data)

    def test_set_mission_workspace_empty_noop(self) -> None:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        panel = AiEditAssistantPanel()

        # Empty dict should be a no-op, not crash
        panel.set_mission_workspace({})
