"""MG-04: Multi-proposal creative flow tests.

Verifies that the Motion Graphic pipeline now follows the three-stage flow:
    brief -> creative proposals -> user selection -> scene plan

The first LLM call produces lightweight text proposals (no scene commands).
The user picks one, then a second LLM call compiles it into scene commands.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.features.teaching_animation.ai.brief import MotionGraphicBrief
from src.features.teaching_animation.ai.proposal import (
    CreativeProposal,
    NarrativeBeat,
    StyleTokens,
)
from src.features.teaching_animation.module import TeachingAnimationModule


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_module() -> TeachingAnimationModule:
    return TeachingAnimationModule()


def _make_plan(
    *,
    phase: str = "proposals",
    proposals: list[CreativeProposal] | None = None,
    selected: str | None = None,
) -> dict:
    """Minimal plan dict matching the structure created by _queue_motion_graphic_plan."""
    brief = MotionGraphicBrief.from_user_text("test", duration_seconds=15.0, fps=24.0)
    return {
        "seconds": 15.0,
        "fps": 24.0,
        "frame": 0,
        "has_timeline": True,
        "track_id": "gfx-1",
        "variant": 0,
        "user_text": "test animation",
        "scene": None,
        "build_failed": False,
        "brief": brief,
        "phase": phase,
        "creative_proposals": proposals or [],
        "selected_proposal_id": selected,
    }


def _make_proposal(pid: str = "A", title: str = "Test Proposal") -> CreativeProposal:
    return CreativeProposal(
        proposal_id=pid,
        brief_id="b1",
        brief_revision=1,
        brief_hash="abc123",
        title=title,
        summary=f"Summary for {pid}",
        beats=(
            NarrativeBeat(
                beat_id="b1", label="Intro", start_seconds=0.0,
                duration_seconds=5.0, on_screen_copy="Hello World",
            ),
            NarrativeBeat(
                beat_id="b2", label="Body", start_seconds=5.0,
                duration_seconds=10.0, on_screen_copy="Main content",
            ),
        ),
        style=StyleTokens(palette={"bg": "#0f172a", "accent": "#3b82f6"}),
        element_sketch={"title": "Main Title", "cards": ["Card1", "Card2"]},
        estimated_risks=(),
        source="llm",
        rationale="Good fit",
    )


def _make_proposal_json(num: int = 2) -> str:
    """Build a JSON payload simulating the LLM's creative proposal output."""
    proposals = []
    for i in range(num):
        pid = chr(ord("A") + i)
        proposals.append({
            "proposal_id": pid,
            "title": f"Proposal {pid}",
            "summary": f"Summary for proposal {pid}",
            "beats": [
                {"beat_id": "b1", "label": "Intro", "start_seconds": 0.0,
                 "duration_seconds": 5.0, "on_screen_copy": "Opening text"},
                {"beat_id": "b2", "label": "Body", "start_seconds": 5.0,
                 "duration_seconds": 10.0, "on_screen_copy": "Content here"},
            ],
            "style": {"palette": {"bg": "#000000", "accent": "#ff0000"}, "tone": "tech"},
            "element_sketch": {"title": f"Title {pid}", "cards": ["A", "B", "C"]},
            "estimated_risks": ["risk1"],
            "rationale": f"Rationale {pid}",
        })
    return json.dumps({"proposals": proposals})


# --------------------------------------------------------------------------- #
# _extract_proposal_selection
# --------------------------------------------------------------------------- #


class TestExtractProposalSelection:
    def test_matches_simple_letter(self) -> None:
        assert TeachingAnimationModule._extract_proposal_selection("A") == "A"
        assert TeachingAnimationModule._extract_proposal_selection("B") == "B"
        assert TeachingAnimationModule._extract_proposal_selection("C") == "C"

    def test_matches_chinese_prefix(self) -> None:
        assert TeachingAnimationModule._extract_proposal_selection("方案A") == "A"
        assert TeachingAnimationModule._extract_proposal_selection("方案B") == "B"
        assert TeachingAnimationModule._extract_proposal_selection("选C") == "C"
        assert TeachingAnimationModule._extract_proposal_selection("选择A") == "A"

    def test_matches_lowercase(self) -> None:
        assert TeachingAnimationModule._extract_proposal_selection("a") == "A"
        assert TeachingAnimationModule._extract_proposal_selection("b") == "B"

    def test_matches_with_spaces(self) -> None:
        assert TeachingAnimationModule._extract_proposal_selection("方案 A") == "A"
        assert TeachingAnimationModule._extract_proposal_selection("  b  ") == "B"

    def test_rejects_non_selection_text(self) -> None:
        assert TeachingAnimationModule._extract_proposal_selection("我想改一下") is None
        assert TeachingAnimationModule._extract_proposal_selection("好的") is None
        assert TeachingAnimationModule._extract_proposal_selection("") is None

    def test_rejects_too_long_text(self) -> None:
        assert TeachingAnimationModule._extract_proposal_selection("方案A非常好但我还想再改改") is None


# --------------------------------------------------------------------------- #
# _build_motion_graphic_proposal_user_prompt
# --------------------------------------------------------------------------- #


class TestProposalUserPrompt:
    def test_includes_duration_and_canvas(self) -> None:
        prompt = TeachingAnimationModule._build_motion_graphic_proposal_user_prompt(
            "15秒AI Agent介绍", seconds=15.0, fps=24.0, width=1920, height=1080,
        )
        assert "15" in prompt
        assert "1920x1080" in prompt
        assert "16:9" in prompt
        assert "AI Agent" in prompt

    def test_detects_vertical_aspect(self) -> None:
        prompt = TeachingAnimationModule._build_motion_graphic_proposal_user_prompt(
            "竖屏广告", seconds=10.0, fps=30.0, width=1080, height=1920,
        )
        assert "9:16" in prompt


# --------------------------------------------------------------------------- #
# _build_motion_graphic_build_user_prompt with proposal_context
# --------------------------------------------------------------------------- #


class TestBuildPromptWithProposalContext:
    def test_without_context_backward_compat(self) -> None:
        prompt = TeachingAnimationModule._build_motion_graphic_build_user_prompt(
            "test", seconds=15.0, fps=24.0,
        )
        assert "test" in prompt
        assert "360" in prompt  # 15*24
        assert "用户已选定" not in prompt

    def test_with_context_includes_proposal(self) -> None:
        prompt = TeachingAnimationModule._build_motion_graphic_build_user_prompt(
            "test", seconds=15.0, fps=24.0,
            proposal_context="方案 Tech Blue\n主标题：AI Agent",
        )
        assert "用户已选定" in prompt
        assert "Tech Blue" in prompt
        assert "AI Agent" in prompt


# --------------------------------------------------------------------------- #
# _build_proposal_context
# --------------------------------------------------------------------------- #


class TestBuildProposalContext:
    def test_empty_when_no_proposals(self) -> None:
        plan = _make_plan(proposals=[])
        assert TeachingAnimationModule._build_proposal_context(plan) == ""

    def test_empty_when_no_selection(self) -> None:
        plan = _make_plan(proposals=[_make_proposal("A")], selected=None)
        assert TeachingAnimationModule._build_proposal_context(plan) == ""

    def test_includes_title_and_summary(self) -> None:
        p = _make_proposal("A", "Tech Blue")
        plan = _make_plan(proposals=[p], selected="A")
        ctx = TeachingAnimationModule._build_proposal_context(plan)
        assert "Tech Blue" in ctx
        assert "Summary for A" in ctx

    def test_includes_beats_and_copy(self) -> None:
        p = _make_proposal("A")
        plan = _make_plan(proposals=[p], selected="A")
        ctx = TeachingAnimationModule._build_proposal_context(plan)
        assert "Intro" in ctx
        assert "Hello World" in ctx
        assert "Body" in ctx
        assert "Main content" in ctx

    def test_includes_palette(self) -> None:
        p = _make_proposal("A")
        plan = _make_plan(proposals=[p], selected="A")
        ctx = TeachingAnimationModule._build_proposal_context(plan)
        assert "#0f172a" in ctx
        assert "#3b82f6" in ctx

    def test_includes_element_sketch(self) -> None:
        p = _make_proposal("A")
        plan = _make_plan(proposals=[p], selected="A")
        ctx = TeachingAnimationModule._build_proposal_context(plan)
        assert "Main Title" in ctx
        assert "Card1" in ctx


# --------------------------------------------------------------------------- #
# _handle_model_creative_proposal_response
# --------------------------------------------------------------------------- #


class TestCreativeProposalResponse:
    def test_parses_valid_json(self) -> None:
        module = _make_module()
        request_id = "test-req-1"
        plan = _make_plan()
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        window.ai_edit_assistant_panel = MagicMock()
        window.ai_edit_assistant_panel.set_motion_graphic_proposal_card = MagicMock()

        result = module._handle_model_creative_proposal_response(
            window, request_id, _make_proposal_json(2),
        )
        assert result is True
        proposals = plan["creative_proposals"]
        assert len(proposals) == 2
        assert proposals[0].proposal_id == "A"
        assert proposals[1].proposal_id == "B"
        assert plan["selected_proposal_id"] is None
        assert plan["phase"] == "proposals"

    def test_parses_markdown_fenced_json(self) -> None:
        module = _make_module()
        request_id = "test-req-2"
        plan = _make_plan()
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        window.ai_edit_assistant_panel = MagicMock()
        window.ai_edit_assistant_panel.set_motion_graphic_proposal_card = MagicMock()

        fenced = f"```json\n{_make_proposal_json(1)}\n```"
        result = module._handle_model_creative_proposal_response(
            window, request_id, fenced,
        )
        assert result is True
        assert len(plan["creative_proposals"]) == 1

    def test_rejects_invalid_json(self) -> None:
        module = _make_module()
        request_id = "test-req-3"
        plan = _make_plan()
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        window.ai_edit_assistant_panel = MagicMock()
        window.ai_edit_assistant_panel.set_motion_graphic_proposal_card = MagicMock()

        result = module._handle_model_creative_proposal_response(
            window, request_id, "not json at all",
        )
        assert result is False
        assert plan["build_failed"] is True

    def test_rejects_missing_proposals_array(self) -> None:
        module = _make_module()
        request_id = "test-req-4"
        plan = _make_plan()
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        window.ai_edit_assistant_panel = MagicMock()
        window.ai_edit_assistant_panel.set_motion_graphic_proposal_card = MagicMock()

        result = module._handle_model_creative_proposal_response(
            window, request_id, json.dumps({"foo": "bar"}),
        )
        assert result is False

    def test_proposals_inherit_brief_binding(self) -> None:
        module = _make_module()
        request_id = "test-req-5"
        plan = _make_plan()
        brief = plan["brief"]
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        window.ai_edit_assistant_panel = MagicMock()
        window.ai_edit_assistant_panel.set_motion_graphic_proposal_card = MagicMock()

        module._handle_model_creative_proposal_response(
            window, request_id, _make_proposal_json(1),
        )
        proposals = plan["creative_proposals"]
        assert proposals[0].brief_id == brief.brief_id
        assert proposals[0].brief_revision == brief.revision


# --------------------------------------------------------------------------- #
# Phase dispatch in _handle_model_motion_graphic_build_response
# --------------------------------------------------------------------------- #


class TestPhaseDispatch:
    def test_dispatches_to_proposals_in_proposal_phase(self) -> None:
        module = _make_module()
        request_id = "test-req-disp-1"
        plan = _make_plan(phase="proposals")
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        window.ai_edit_assistant_panel = MagicMock()
        window.ai_edit_assistant_panel.set_motion_graphic_proposal_card = MagicMock()

        result = module._handle_model_motion_graphic_build_response(
            window, request_id, _make_proposal_json(2),
        )
        assert result is True
        assert len(plan["creative_proposals"]) == 2
        assert plan["phase"] == "proposals"


# --------------------------------------------------------------------------- #
# _select_creative_proposal
# --------------------------------------------------------------------------- #


class TestSelectCreativeProposal:
    def test_transitions_to_build_phase(self) -> None:
        module = _make_module()
        request_id = "test-select-1"
        proposals = [_make_proposal("A"), _make_proposal("B")]
        plan = _make_plan(phase="proposals", proposals=proposals)
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()

        result = module._select_creative_proposal(window, request_id, "B")
        assert result is True
        assert plan["phase"] == "build"
        assert plan["selected_proposal_id"] == "B"

    def test_case_insensitive_match(self) -> None:
        module = _make_module()
        request_id = "test-select-2"
        proposals = [_make_proposal("A")]
        plan = _make_plan(phase="proposals", proposals=proposals)
        module._pending_motion_graphic_requests[request_id] = plan

        result = module._select_creative_proposal(window := MagicMock(), request_id, "a")
        assert result is True
        assert plan["selected_proposal_id"] == "A"

    def test_rejects_unknown_proposal_id(self) -> None:
        module = _make_module()
        request_id = "test-select-3"
        proposals = [_make_proposal("A")]
        plan = _make_plan(phase="proposals", proposals=proposals)
        module._pending_motion_graphic_requests[request_id] = plan

        result = module._select_creative_proposal(MagicMock(), request_id, "Z")
        assert result is False
        assert plan["phase"] == "proposals"

    def test_rejects_missing_plan(self) -> None:
        module = _make_module()
        result = module._select_creative_proposal(MagicMock(), "nonexistent", "A")
        assert result is False


# --------------------------------------------------------------------------- #
# _show_creative_proposal_selection_card
# --------------------------------------------------------------------------- #


class TestProposalSelectionCard:
    def test_card_data_has_proposal_mode(self) -> None:
        module = _make_module()
        request_id = "test-card-1"
        proposals = [_make_proposal("A"), _make_proposal("B")]
        plan = _make_plan(phase="proposals", proposals=proposals)
        module._pending_motion_graphic_requests[request_id] = plan

        captured: dict = {}
        panel = MagicMock()
        panel.set_motion_graphic_proposal_card = MagicMock(
            side_effect=lambda rid, data: captured.update(rid=rid, data=data),
        )
        window = MagicMock()
        window.ai_edit_assistant_panel = panel

        module._show_creative_proposal_selection_card(window, request_id)
        assert captured["rid"] == request_id
        card = captured["data"]
        assert card["mode"] == "proposal_selection"
        assert len(card["proposals"]) == 2
        assert card["proposals"][0]["proposal_id"] == "A"
        assert card["proposals"][1]["proposal_id"] == "B"

    def test_card_proposals_contain_beats_preview(self) -> None:
        module = _make_module()
        request_id = "test-card-2"
        proposals = [_make_proposal("A")]
        plan = _make_plan(phase="proposals", proposals=proposals)
        module._pending_motion_graphic_requests[request_id] = plan

        captured: dict = {}
        panel = MagicMock()
        panel.set_motion_graphic_proposal_card = MagicMock(
            side_effect=lambda rid, data: captured.update(data=data),
        )
        window = MagicMock()
        window.ai_edit_assistant_panel = panel

        module._show_creative_proposal_selection_card(window, request_id)
        card = captured["data"]
        prop = card["proposals"][0]
        assert "Hello World" in prop["beats_preview"]


# --------------------------------------------------------------------------- #
# _handle_ai_request proposal-phase interception
# --------------------------------------------------------------------------- #


class TestHandleAiRequestProposalPhase:
    def test_proposal_selection_text_triggers_select(self) -> None:
        """In proposals phase, typing "A" selects proposal A."""
        module = _make_module()
        request_id = "test-ai-1"
        proposals = [_make_proposal("A"), _make_proposal("B")]
        plan = _make_plan(phase="proposals", proposals=proposals)
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()

        # Mock _select_creative_proposal to avoid triggering LLM build
        called: list[str] = []
        def fake_select(_mw, _rid, pid):
            called.append(pid)
            plan["phase"] = "build"
            plan["selected_proposal_id"] = pid
            return True
        module._select_creative_proposal = fake_select  # type: ignore[assignment]

        result = module._handle_ai_request(window, "方案B")
        assert result is True
        assert called == ["B"]

    def test_cancel_text_works_in_proposal_phase(self) -> None:
        module = _make_module()
        request_id = "test-ai-2"
        proposals = [_make_proposal("A")]
        plan = _make_plan(phase="proposals", proposals=proposals)
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        module._cancel_motion_graphic_request = MagicMock(return_value=True)  # type: ignore

        result = module._handle_ai_request(window, "取消")
        assert result is True
        module._cancel_motion_graphic_request.assert_called_once()

    def test_confirm_in_proposal_phase_selects_first(self) -> None:
        """Typing "确认" in proposal phase auto-selects the first proposal."""
        module = _make_module()
        request_id = "test-ai-3"
        proposals = [_make_proposal("A"), _make_proposal("B")]
        plan = _make_plan(phase="proposals", proposals=proposals)
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        selected: list[str] = []
        def fake_select(_mw, _rid, pid):
            selected.append(pid)
            return True
        module._select_creative_proposal = fake_select  # type: ignore[assignment]

        result = module._handle_ai_request(window, "确认")
        assert result is True
        assert selected == ["A"]

    def test_other_text_keeps_conversation(self) -> None:
        """Non-selection text in proposal phase continues the conversation."""
        module = _make_module()
        request_id = "test-ai-4"
        proposals = [_make_proposal("A")]
        plan = _make_plan(phase="proposals", proposals=proposals)
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        result = module._handle_ai_request(window, "我想要更快的节奏")
        assert result is True
        # Phase unchanged — user hasn't selected yet
        assert plan["phase"] == "proposals"
