"""Wiring tests for OS-07 (feedback store) and OS-09 (provider resume).

Proves the dormant contract modules are now consumed by production code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.features.teaching_animation.module import TeachingAnimationModule


# --------------------------------------------------------------------------- #
# OS-07: Feedback categorization and recording
# --------------------------------------------------------------------------- #


class TestFeedbackCategorization:
    """OS-07: user text is categorized into structured feedback dimensions."""

    def test_pacing_keywords(self) -> None:
        assert "pacing" in TeachingAnimationModule._categorize_feedback("节奏太快了")
        assert "pacing" in TeachingAnimationModule._categorize_feedback("速度慢一点")
        assert "pacing" in TeachingAnimationModule._categorize_feedback("太慢")

    def test_copy_keywords(self) -> None:
        cats = TeachingAnimationModule._categorize_feedback("标题改成AI助手")
        assert "copy" in cats

    def test_color_keywords(self) -> None:
        cats = TeachingAnimationModule._categorize_feedback("配色换成科技蓝")
        assert "color" in cats

    def test_audio_keywords(self) -> None:
        cats = TeachingAnimationModule._categorize_feedback("背景音乐太吵")
        assert "audio" in cats

    def test_brand_keywords(self) -> None:
        cats = TeachingAnimationModule._categorize_feedback("logo位置不对")
        assert "brand" in cats

    def test_platform_keywords(self) -> None:
        cats = TeachingAnimationModule._categorize_feedback("改成竖屏")
        assert "platform" in cats

    def test_quality_keywords(self) -> None:
        cats = TeachingAnimationModule._categorize_feedback("文字溢出了")
        assert "quality" in cats

    def test_multi_category(self) -> None:
        """Text touching multiple aspects gets multiple categories."""
        cats = TeachingAnimationModule._categorize_feedback("节奏太快，颜色改成蓝色")
        assert "pacing" in cats
        assert "color" in cats

    def test_no_match_defaults_to_goal(self) -> None:
        cats = TeachingAnimationModule._categorize_feedback("整体换个思路")
        assert cats == ("goal",)

    def test_empty_text(self) -> None:
        cats = TeachingAnimationModule._categorize_feedback("")
        assert cats == ("goal",)


class TestFeedbackRecording:
    """OS-07: feedback is recorded in the FeedbackStore."""

    def test_record_creates_patch(self) -> None:
        module = TeachingAnimationModule()
        patch = module._record_user_feedback(
            "节奏太慢了，标题改成AI助手",
            request_id="req-1",
        )
        assert patch is not None
        assert "pacing" in patch.categories
        assert "copy" in patch.categories
        assert patch.run_id == "req-1"

    def test_record_stores_in_feedback_store(self) -> None:
        module = TeachingAnimationModule()
        module._record_user_feedback("颜色太暗", request_id="req-2")
        store = module._feedback_store
        assert store is not None
        patches = store.for_run("req-2")
        assert len(patches) == 1
        assert "color" in patches[0].categories

    def test_record_accumulates(self) -> None:
        module = TeachingAnimationModule()
        module._record_user_feedback("节奏太慢", request_id="req-3")
        module._record_user_feedback("颜色太暗", request_id="req-3")
        store = module._feedback_store
        patches = store.for_run("req-3")
        assert len(patches) == 2

    def test_record_with_unknown_domain_falls_back(self) -> None:
        module = TeachingAnimationModule()
        patch = module._record_user_feedback(
            "test feedback", request_id="req-4", domain="unknown_domain",
        )
        assert patch is not None
        assert patch.domain == "general"


class TestFeedbackInRequestFlow:
    """OS-07: feedback is recorded when user provides revision text."""

    def test_revision_feedback_recorded(self) -> None:
        """When user types non-selection text in proposal phase, feedback is recorded."""
        from src.features.teaching_animation.ai.brief import MotionGraphicBrief
        from src.features.teaching_animation.ai.proposal import CreativeProposal

        module = TeachingAnimationModule()
        request_id = "test-fb-flow"
        brief = MotionGraphicBrief.from_user_text("test", duration_seconds=15.0, fps=24.0)
        proposal = CreativeProposal(
            proposal_id="A", brief_id=brief.brief_id,
            brief_revision=1, brief_hash="h",
            title="Test", summary="Summary",
        )
        plan = {
            "seconds": 15.0, "fps": 24.0, "frame": 0, "has_timeline": True,
            "track_id": "gfx-1", "variant": 0, "user_text": "test",
            "scene": None, "build_failed": False, "brief": brief,
            "phase": "proposals", "creative_proposals": [proposal],
            "selected_proposal_id": None,
        }
        module._pending_motion_graphic_requests[request_id] = plan

        window = MagicMock()
        # User types revision feedback
        result = module._handle_ai_request(window, "节奏太快了，颜色改成蓝色")
        assert result is True

        # Feedback should be recorded
        assert module._feedback_store is not None
        patches = module._feedback_store.for_run(request_id)
        assert len(patches) >= 1
        cats = set()
        for p in patches:
            cats.update(p.categories)
        assert "pacing" in cats
        assert "color" in cats


# --------------------------------------------------------------------------- #
# OS-09: Provider resume reconciliation
# --------------------------------------------------------------------------- #


