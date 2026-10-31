"""Context budget + real-usage bridge tests (plan §A4).

Validates the rules that keep the top-bar token display honest:

- A pre-send estimate is marked estimated; a real provider reading clears that
  flag and never fakes a zero when the provider returns nothing.
- Budget checks reserve output room, so a window that fits the prompt but
  leaves no room to answer still counts as over budget.
- 80 % warns, 90 % auto-compresses.
- Motion Graphic protected context keys are recognised.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.agent_models import ContextUsage
from src.features.edit.ai_editing.context_budget import (
    COMPRESSION_AUTO_THRESHOLD,
    COMPRESSION_WARN_THRESHOLD,
    PROTECTED_CONTEXT_KEYS,
    accumulate_session_total,
    budget_for_profile,
    estimate_window_usage,
    is_protected_context_key,
    merge_usage,
    should_auto_compress,
    should_warn_compression,
    usage_from_llm,
    would_exceed_limit,
)
from src.features.edit.ai_editing.llm_client import LLMUsage
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    ProviderLimits,
)


def _llm(prompt=None, completion=None, total=None):
    return LLMUsage(
        request_id="r",
        provider_kind="openai",
        model_id="gpt",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


# ---------------------------------------------------------------------- #
# Estimation
# ---------------------------------------------------------------------- #


class TestEstimateWindow:
    def test_estimate_marked_estimated(self):
        usage = estimate_window_usage(
            [{"role": "user", "content": "hello"}], window_limit=8000
        )
        assert usage.window_prompt is not None
        assert usage.window_prompt > 0
        assert usage.window_prompt_is_estimated is True
        assert usage.window_limit == 8000

    def test_reserved_output_defaults_to_ratio(self):
        usage = estimate_window_usage(
            [{"role": "user", "content": "hi"}], window_limit=8000
        )
        # default 25% reserved for output
        assert usage.reserved_output == 2000

    def test_reserved_output_can_be_overridden(self):
        usage = estimate_window_usage(
            [{"role": "user", "content": "hi"}],
            window_limit=8000,
            reserved_output=500,
        )
        assert usage.reserved_output == 500

    def test_role_overhead_counted_per_message(self):
        single = estimate_window_usage(
            [{"role": "user", "content": "word"}], window_limit=1000
        )
        double = estimate_window_usage(
            [{"role": "user", "content": "word"}, {"role": "user", "content": "word"}],
            window_limit=1000,
        )
        # two messages cost more than one (role overhead adds tokens)
        assert double.window_prompt > single.window_prompt

    def test_no_limit_returns_none_ratio(self):
        usage = estimate_window_usage([{"role": "user", "content": "hi"}], window_limit=None)
        assert usage.window_ratio is None


# ---------------------------------------------------------------------- #
# Real usage bridge
# ---------------------------------------------------------------------- #


class TestUsageFromLlm:
    def test_real_usage_clears_estimated_flag(self):
        est = estimate_window_usage(
            [{"role": "user", "content": "hi"}], window_limit=8000
        )
        real = usage_from_llm(_llm(prompt=120, completion=30, total=150), window_usage=est)
        assert real.actual_prompt == 120
        assert real.actual_completion == 30
        # window_prompt is now the real value -> not estimated
        assert real.window_prompt == 120
        assert real.window_prompt_is_estimated is False

    def test_missing_usage_keeps_estimate_and_none_actuals(self):
        est = estimate_window_usage(
            [{"role": "user", "content": "hi"}], window_limit=8000
        )
        real = usage_from_llm(_llm(), window_usage=est)
        # never fake a zero: actuals stay None, window stays estimated
        assert real.actual_prompt is None
        assert real.actual_completion is None
        assert real.window_prompt_is_estimated is True
        assert real.window_prompt == est.window_prompt

    def test_session_total_uses_explicit_or_total(self):
        real = usage_from_llm(_llm(prompt=10, completion=5, total=15), session_total_tokens=200)
        assert real.session_total_tokens == 200
        real2 = usage_from_llm(_llm(prompt=10, completion=5, total=15))
        assert real2.session_total_tokens == 15


class TestMergeUsage:
    def test_estimate_metadata_plus_real_actuals(self):
        est = estimate_window_usage(
            [{"role": "user", "content": "hi"}], window_limit=8000
        )
        merged = merge_usage(est, _llm(prompt=100, completion=20, total=120), session_total_tokens=120)
        assert merged.window_limit == 8000
        assert merged.reserved_output == 2000
        assert merged.actual_prompt == 100
        assert merged.window_prompt == 100
        assert merged.window_prompt_is_estimated is False

    def test_merge_without_real_usage_keeps_estimate(self):
        est = estimate_window_usage(
            [{"role": "user", "content": "hi"}], window_limit=8000
        )
        merged = merge_usage(est, _llm(), session_total_tokens=0)
        assert merged.actual_prompt is None
        assert merged.window_prompt_is_estimated is True


# ---------------------------------------------------------------------- #
# Session accumulation
# ---------------------------------------------------------------------- #


class TestAccumulateSessionTotal:
    def test_adds_total_tokens(self):
        assert accumulate_session_total(100, _llm(total=50)) == 150

    def test_falls_back_to_prompt_plus_completion(self):
        assert accumulate_session_total(100, _llm(prompt=30, completion=20)) == 150

    def test_none_usage_unchanged(self):
        assert accumulate_session_total(100, None) == 100


# ---------------------------------------------------------------------- #
# Thresholds
# ---------------------------------------------------------------------- #


class TestThresholds:
    def _usage(self, prompt, limit=1000):
        return ContextUsage(
            window_prompt=prompt,
            window_prompt_is_estimated=False,
            window_limit=limit,
            reserved_output=0,
        )

    def test_below_warn(self):
        u = self._usage(700)
        assert not should_warn_compression(u)
        assert not should_auto_compress(u)

    def test_at_warn_not_auto(self):
        u = self._usage(int(1000 * COMPRESSION_WARN_THRESHOLD))
        assert should_warn_compression(u)
        assert not should_auto_compress(u)

    def test_at_auto(self):
        u = self._usage(int(1000 * COMPRESSION_AUTO_THRESHOLD))
        assert should_auto_compress(u)

    def test_unknown_ratio_no_action(self):
        u = ContextUsage(window_prompt=None, window_limit=None)
        assert not should_warn_compression(u)
        assert not should_auto_compress(u)


class TestWouldExceedLimit:
    def test_reserves_output_room(self):
        # prompt fits limit but leaves no reserved room -> exceeds
        u = ContextUsage(
            window_prompt=900,
            window_limit=1000,
            reserved_output=200,
        )
        assert would_exceed_limit(u)

    def test_fits_with_reserved(self):
        u = ContextUsage(
            window_prompt=700,
            window_limit=1000,
            reserved_output=200,
        )
        assert not would_exceed_limit(u)

    def test_extra_tokens_push_over(self):
        u = ContextUsage(
            window_prompt=700,
            window_limit=1000,
            reserved_output=200,
        )
        assert would_exceed_limit(u, extra_tokens=200)

    def test_unknown_limit_never_exceeds(self):
        u = ContextUsage(window_prompt=999, window_limit=None)
        assert not would_exceed_limit(u)


# ---------------------------------------------------------------------- #
# Profile budget + protected context
# ---------------------------------------------------------------------- #


class TestProfileBudget:
    def test_budget_from_profile_limits(self):
        profile = AiProviderProfile(
            profile_id="p",
            display_name="p",
            limits=ProviderLimits(max_context_tokens=32000),
        )
        budget = budget_for_profile(profile)
        assert budget.window_limit == 32000
        assert budget.reserved_output == 8000

    def test_budget_without_limits(self):
        profile = AiProviderProfile(profile_id="p", display_name="p")
        budget = budget_for_profile(profile)
        # default limit applies
        assert budget.window_limit is not None


class TestProtectedContext:
    @pytest.mark.parametrize("key", ["canvas_width", "fps", "scene_revision", "artifact_hash"])
    def test_protected_keys_recognised(self, key):
        assert is_protected_context_key(key)

    def test_non_protected_key(self):
        assert not is_protected_context_key("random_chat_message")

    def test_motion_graphic_critical_keys_present(self):
        # Plan §2.5 invariants: canvas + fps + scene revision must survive
        # compression.
        for key in ("canvas_width", "canvas_height", "fps", "scene_revision"):
            assert key in PROTECTED_CONTEXT_KEYS
