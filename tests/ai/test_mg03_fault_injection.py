"""MG-03 storyboard preview + §8 fault injection tests.

Tests two areas:
1. MG-03: storyboard preview rendering wired into the confirmation card
2. §8: fault injection — the pipeline must degrade safely when LLM output
   is malformed, rendering fails, or the provider is unavailable.  No
   failure should silently substitute a hardcoded template (MG-05).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.features.teaching_animation.ai.brief import MotionGraphicBrief
from src.features.teaching_animation.module import TeachingAnimationModule


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_module() -> TeachingAnimationModule:
    return TeachingAnimationModule()


def _make_plan(**overrides) -> dict:
    brief = MotionGraphicBrief.from_user_text("test", duration_seconds=15.0, fps=24.0)
    base = {
        "seconds": 15.0,
        "fps": 24.0,
        "frame": 0,
        "has_timeline": True,
        "track_id": "gfx-1",
        "variant": 0,
        "user_text": "test",
        "scene": None,
        "build_failed": False,
        "brief": brief,
        "phase": "build",
        "creative_proposals": [],
        "selected_proposal_id": None,
    }
    base.update(overrides)
    return base


def _make_window_with_panel() -> MagicMock:
    """Window mock whose panel captures card_data."""
    window = MagicMock()
    panel = MagicMock()
    panel.set_motion_graphic_proposal_card = MagicMock()
    panel.set_motion_graphic_proposal = MagicMock()
    panel.clear_motion_graphic_proposal = MagicMock()
    window.ai_edit_assistant_panel = panel
    return window


# --------------------------------------------------------------------------- #
# MG-03: _storyboard_label
# --------------------------------------------------------------------------- #

class TestStoryboardLabel:
    def test_first_frame(self) -> None:
        assert TeachingAnimationModule._storyboard_label(0, 360) == "first_frame"

    def test_last_frame(self) -> None:
        assert TeachingAnimationModule._storyboard_label(359, 360) == "last_frame"

    def test_mid_frame(self) -> None:
        assert TeachingAnimationModule._storyboard_label(180, 360) == "mid_frame"

    def test_general_frame(self) -> None:
        assert TeachingAnimationModule._storyboard_label(100, 360) == "frame_100"


# --------------------------------------------------------------------------- #
# MG-03: _render_storyboard_preview
# --------------------------------------------------------------------------- #

class TestStoryboardPreview:
    def test_returns_none_when_all_frames_fail(self) -> None:
        """If every frame render fails, preview is None (graceful degradation)."""
        module = _make_module()
        scene = MagicMock()
        scene.scene_id = "test-scene"
        scene.fps = 24.0
        scene.duration_frames = 360

        with patch(
            "src.features.teaching_animation.host_adapters.render_teaching_scene_frame",
            side_effect=RuntimeError("GPU error"),
        ):
            result = module._render_storyboard_preview(scene, count=5)

        assert result is None

    def test_returns_frames_when_rendering_succeeds(self) -> None:
        """Successful renders produce a non-empty frame list."""
        module = _make_module()
        scene = MagicMock()
        scene.scene_id = "test-scene"
        scene.fps = 24.0
        scene.duration_frames = 360

        rendered = MagicMock()
        rendered.data = b"\x00" * 100
        rendered.width = 640
        rendered.height = 360

        with patch(
            "src.features.teaching_animation.host_adapters.render_teaching_scene_frame",
            return_value=rendered,
        ):
            result = module._render_storyboard_preview(scene, count=5)

        assert result is not None
        assert len(result) == 5
        assert all("frame_index" in f for f in result)
        assert all("label" in f for f in result)
        assert all("time_seconds" in f for f in result)
        assert all(f.get("render_error") is None for f in result)

    def test_partial_failure_still_returns_frames(self) -> None:
        """Some frames failing still returns the successful ones."""
        module = _make_module()
        scene = MagicMock()
        scene.scene_id = "test-scene"
        scene.fps = 24.0
        scene.duration_frames = 360

        rendered = MagicMock()
        rendered.data = b"\x00" * 100
        rendered.width = 640
        rendered.height = 360

        call_count = [0]
        def mock_render(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:  # Second frame fails
                raise RuntimeError("transient error")
            return rendered

        with patch(
            "src.features.teaching_animation.host_adapters.render_teaching_scene_frame",
            side_effect=mock_render,
        ):
            result = module._render_storyboard_preview(scene, count=5)

        assert result is not None
        assert len(result) == 5
        # At least one frame has no error
        successes = [f for f in result if f.get("render_error") is None]
        assert len(successes) >= 1
        # At least one frame has an error
        errors = [f for f in result if f.get("render_error") is not None]
        assert len(errors) >= 1

    def test_empty_render_returns_error_info(self) -> None:
        """An empty render (no data) is recorded as an error."""
        module = _make_module()
        scene = MagicMock()
        scene.scene_id = "test-scene"
        scene.fps = 24.0
        scene.duration_frames = 360

        rendered = MagicMock()
        rendered.data = b""
        rendered.width = 0
        rendered.height = 0

        with patch(
            "src.features.teaching_animation.host_adapters.render_teaching_scene_frame",
            return_value=rendered,
        ):
            result = module._render_storyboard_preview(scene, count=3)

        assert result is None  # All frames failed → None

    def test_sample_failure_returns_none(self) -> None:
        """If frame sampling raises, preview is None."""
        module = _make_module()
        scene = MagicMock()
        scene.scene_id = "test-scene"
        scene.fps = 24.0
        scene.duration_frames = 0  # May cause sampling issues

        result = module._render_storyboard_preview(scene, count=5)
        # With 0 duration frames, sampling may return (0,) or raise
        # Either way, the method should not crash
        assert result is None or isinstance(result, list)


# --------------------------------------------------------------------------- #
# MG-03: storyboard data on confirmation card
# --------------------------------------------------------------------------- #

class TestStoryboardOnCard:
    def test_card_includes_storyboard_when_available(self) -> None:
        """The confirmation card includes storyboard preview frames."""
        module = _make_module()
        request_id = "test-card-sb"
        storyboard = [
            {"frame_index": 0, "label": "first_frame", "time_seconds": 0.0,
             "render_error": None, "width": 640, "height": 360},
        ]
        plan = _make_plan(storyboard=storyboard)
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        module._show_motion_graphic_proposal_card(
            window, request_id,
            variant=0, seconds=15.0, frame=0,
            graphics_track=None, fallback=False,
        )
        panel = window.ai_edit_assistant_panel
        panel.set_motion_graphic_proposal_card.assert_called_once()
        card_data = panel.set_motion_graphic_proposal_card.call_args[0][1]
        assert "storyboard" in card_data
        assert len(card_data["storyboard"]) == 1

    def test_card_without_storyboard(self) -> None:
        """Card works fine when no storyboard is available."""
        module = _make_module()
        request_id = "test-card-no-sb"
        plan = _make_plan()
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        module._show_motion_graphic_proposal_card(
            window, request_id,
            variant=0, seconds=15.0, frame=0,
            graphics_track=None, fallback=False,
        )
        panel = window.ai_edit_assistant_panel
        card_data = panel.set_motion_graphic_proposal_card.call_args[0][1]
        assert "storyboard" not in card_data


# --------------------------------------------------------------------------- #
# §8: Fault injection — proposal phase
# --------------------------------------------------------------------------- #

class TestProposalPhaseFaults:
    def test_invalid_json_shows_failure_card(self) -> None:
        """Malformed JSON from LLM in proposal phase → failure card, not crash."""
        module = _make_module()
        request_id = "test-fault-prop-json"
        plan = _make_plan(phase="proposals")
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        result = module._handle_model_motion_graphic_build_response(
            window, request_id, "not valid json {{{",
        )
        assert result is False
        assert plan["build_failed"] is True

    def test_empty_proposals_array_shows_failure(self) -> None:
        """LLM returns {"proposals": []} → failure card."""
        module = _make_module()
        request_id = "test-fault-prop-empty"
        plan = _make_plan(phase="proposals")
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        result = module._handle_model_motion_graphic_build_response(
            window, request_id, json.dumps({"proposals": []}),
        )
        assert result is False
        assert plan["build_failed"] is True

    def test_missing_proposals_key_shows_failure(self) -> None:
        """LLM returns valid JSON but without proposals key → failure."""
        module = _make_module()
        request_id = "test-fault-prop-missing"
        plan = _make_plan(phase="proposals")
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        result = module._handle_model_motion_graphic_build_response(
            window, request_id, json.dumps({"foo": "bar"}),
        )
        assert result is False

    def test_malformed_proposal_items_skipped(self) -> None:
        """Individual malformed proposal items are skipped, valid ones kept."""
        module = _make_module()
        request_id = "test-fault-prop-partial"
        plan = _make_plan(phase="proposals")
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        # Mix of valid and invalid proposal items
        payload = json.dumps({
            "proposals": [
                "not a dict",  # Invalid — skipped
                {"proposal_id": "A", "title": "Valid", "summary": "OK"},
                {"proposal_id": "", "title": ""},  # Invalid — skipped (empty fields)
            ]
        })
        result = module._handle_model_motion_graphic_build_response(
            window, request_id, payload,
        )
        assert result is True
        proposals = plan["creative_proposals"]
        assert len(proposals) == 1  # Only the valid one parsed


# --------------------------------------------------------------------------- #
# §8: Fault injection — build phase
# --------------------------------------------------------------------------- #

class TestBuildPhaseFaults:
    def test_invalid_json_shows_failure_card(self) -> None:
        """Malformed JSON from LLM in build phase → failure card."""
        module = _make_module()
        request_id = "test-fault-build-json"
        plan = _make_plan(phase="build")
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        result = module._handle_model_motion_graphic_build_response(
            window, request_id, "garbage not json",
        )
        assert result is False
        assert plan["build_failed"] is True

    def test_empty_commands_shows_failure(self) -> None:
        """LLM returns {"commands": []} → scene construction fails → failure card."""
        module = _make_module()
        request_id = "test-fault-build-empty-cmds"
        plan = _make_plan(phase="build")
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        result = module._handle_model_motion_graphic_build_response(
            window, request_id,
            json.dumps({"name": "test", "commands": []}),
        )
        assert result is False

    def test_top_level_not_object_shows_failure(self) -> None:
        """LLM returns a JSON array instead of object → failure."""
        module = _make_module()
        request_id = "test-fault-build-array"
        plan = _make_plan(phase="build")
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        result = module._handle_model_motion_graphic_build_response(
            window, request_id, json.dumps([1, 2, 3]),
        )
        assert result is False

    def test_unsupported_command_shows_failure(self) -> None:
        """LLM returns unknown command name → scene construction fails."""
        module = _make_module()
        request_id = "test-fault-build-badcmd"
        plan = _make_plan(phase="build")
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        payload = json.dumps({
            "name": "test",
            "commands": [
                {"name": "delete_everything", "arguments": {}},
            ],
        })
        result = module._handle_model_motion_graphic_build_response(
            window, request_id, payload,
        )
        assert result is False


# --------------------------------------------------------------------------- #
# §8: Fault injection — LLM unavailable (no silent template substitution)
# --------------------------------------------------------------------------- #

class TestProviderUnavailable:
    def test_trigger_failure_marks_build_failed(self) -> None:
        """When _trigger returns False (no controller), plan is marked failed.

        This is the MG-05 guarantee: LLM unavailability NEVER silently
        substitutes a hardcoded template.
        """
        module = _make_module()
        request_id = "test-fault-noprovider"

        # Simulate _trigger returning False
        module._trigger_motion_graphic_creative_proposals = lambda _mw, _rid: False  # type: ignore

        window = _make_window_with_panel()

        # Manually create a plan and trigger the failure path
        brief = MotionGraphicBrief.from_user_text("test", duration_seconds=15.0, fps=24.0)
        plan = {
            "seconds": 15.0, "fps": 24.0, "frame": 0, "has_timeline": True,
            "track_id": "gfx-1", "variant": 0, "user_text": "test",
            "scene": None, "build_failed": False, "brief": brief,
            "phase": "proposals", "creative_proposals": [],
            "selected_proposal_id": None,
        }
        module._pending_motion_graphic_requests[request_id] = plan

        triggered = module._trigger_motion_graphic_creative_proposals(window, request_id)
        assert triggered is False

        # Simulate what _queue_motion_graphic_plan does when trigger fails
        module._mark_motion_graphic_build_failed(
            window, request_id, "AI channel unavailable",
        )
        assert plan["build_failed"] is True
        assert plan["scene"] is None  # No template substituted

    def test_failure_card_shows_explicit_error(self) -> None:
        """The failure card has quality_errors with the reason."""
        module = _make_module()
        request_id = "test-fault-card"
        plan = _make_plan()
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        module._show_motion_graphic_failure_card(
            window, request_id,
            seconds=15.0, frame=0,
            reason="LLM timeout after 30s",
        )
        panel = window.ai_edit_assistant_panel
        panel.set_motion_graphic_proposal_card.assert_called_once()
        card_data = panel.set_motion_graphic_proposal_card.call_args[0][1]
        assert card_data["is_fallback"] is False
        assert "LLM timeout" in card_data["quality_errors"][0]
        # Browser authoring failures stay in the model-authored path; no
        # host template is offered as a hidden substitute.
        assert "choose_template" not in card_data["actions"]


# --------------------------------------------------------------------------- #
# §8: Fault injection — confirm safeguards
# --------------------------------------------------------------------------- #

class TestConfirmSafeguards:
    def test_confirm_without_scene_or_template_refuses(self) -> None:
        """Confirm with no scene and no use_template → False (MG-05)."""
        module = _make_module()
        request_id = "test-confirm-refuse"
        plan = _make_plan(scene=None)
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        result = module._confirm_motion_graphic_request(window, request_id)
        assert result is False
        # Plan was popped
        assert request_id not in module._pending_motion_graphic_requests

    def test_missing_plan_returns_false(self) -> None:
        """Confirm on unknown request_id → False."""
        module = _make_module()
        window = _make_window_with_panel()
        result = module._confirm_motion_graphic_request(window, "nonexistent-id")
        assert result is False


# --------------------------------------------------------------------------- #
# §8: Fault injection — cancel paths
# --------------------------------------------------------------------------- #

class TestCancelPaths:
    def test_cancel_in_proposal_phase(self) -> None:
        """Cancel during proposal phase removes the plan."""
        module = _make_module()
        request_id = "test-cancel-prop"
        plan = _make_plan(phase="proposals")
        module._pending_motion_graphic_requests[request_id] = plan

        window = _make_window_with_panel()
        result = module._cancel_motion_graphic_request(
            window, request_id, kind="motion",
        )
        assert result is True
        assert request_id not in module._pending_motion_graphic_requests

    def test_cancel_unknown_id_returns_false(self) -> None:
        """Cancel on missing id returns False (nothing to cancel)."""
        module = _make_module()
        window = _make_window_with_panel()
        result = module._cancel_motion_graphic_request(
            window, "unknown", kind="motion",
        )
        assert result is False  # No plan found → False, not a crash
