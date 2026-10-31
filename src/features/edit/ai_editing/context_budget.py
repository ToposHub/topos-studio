"""Context window budget + real-usage bridge (plan §A4).

Turns the top-bar token number from a flat local estimate into an explainable
pair: *the context window about to be sent* vs *the session total actually
spent*.  This module is the pure-logic glue between three existing pieces:

- ``token_estimator.estimate_tokens`` — the local heuristic estimate.
- ``llm_client.LLMUsage`` — the real prompt/completion/cost a provider
  returns after a request.
- ``agent_models.ContextUsage`` — the unified budget object the orchestrator
  stamps on an ``AgentRun`` and the UI renders.

Hard rules (plan §A4 §4.1 / §4.2):

- **Never fake real usage.**  When the provider returns no usage, the numbers
  stay estimated and ``window_prompt_is_estimated`` stays ``True``.  A missing
  number must never be disguised as ground truth.
- **Reserve output tokens.**  Budget checks compare ``prompt + reserved_output``
  against the limit, not the raw prompt sum, so the model always has room to
  answer.
- **Two thresholds, not one.**  80 % warns the user; 90 % triggers automatic
  compression.  The hard ceiling is the limit itself.
- **Compression preserves protected context.**  Motion Graphic canvas / fps /
  scene revision are listed here so the compressor can never drop them.
"""

from __future__ import annotations

from typing import Any, Mapping

from src.features.edit.ai_editing.agent_models import ContextUsage
from src.features.edit.ai_editing.image_attachment import (
    estimate_image_tokens,
    extract_text_from_content,
)
from src.features.edit.ai_editing.llm_client import LLMUsage
from src.features.edit.ai_editing.token_estimator import estimate_tokens


# ---------------------------------------------------------------------- #
# Thresholds (plan §A4 §4.2 step 7)
# ---------------------------------------------------------------------- #

#: Fraction of the window limit at which the UI starts warning the user.
COMPRESSION_WARN_THRESHOLD: float = 0.80
#: Fraction of the window limit at which automatic compression kicks in.
COMPRESSION_AUTO_THRESHOLD: float = 0.90
#: Default share of the window reserved for the model's output when the caller
#: does not pass an explicit ``reserved_output``.
DEFAULT_RESERVED_OUTPUT_RATIO: float = 0.25

#: Context keys the compressor must never drop (plan §A4 §4.2 step 12).
#: Motion Graphic relies on these being present or generation is unsafe.
PROTECTED_CONTEXT_KEYS: tuple[str, ...] = (
    "canvas_width",
    "canvas_height",
    "fps",
    "scene_id",
    "scene_revision",
    "context_fingerprint",
    "artifact_hash",
    "user_intent",
)


# ---------------------------------------------------------------------- #
# Estimation (before the request)
# ---------------------------------------------------------------------- #


def estimate_window_usage(
    messages: list[Mapping[str, Any]] | list[dict[str, Any]],
    *,
    window_limit: int | None,
    reserved_output: int | None = None,
) -> ContextUsage:
    """Estimate the tokens that are about to be sent to the model.

    Uses the local estimator (``estimate_tokens``), so the result is marked
    ``is_estimated=True``.  ``reserved_output`` defaults to a quarter of the
    limit so budget checks leave the model room to answer (plan §A4 §4.2 §6).
    """

    estimated_prompt = _sum_message_tokens(messages)
    resolved_reserved = _resolve_reserved(reserved_output, window_limit)
    return ContextUsage(
        window_prompt=estimated_prompt,
        window_prompt_is_estimated=True,
        window_limit=window_limit,
        reserved_output=resolved_reserved,
    )


def budget_for_profile(profile: Any) -> ContextUsage:
    """Build an empty budget from a profile's context-token limit."""

    limit = _profile_context_limit(profile)
    reserved = _resolve_reserved(None, limit)
    return ContextUsage(
        window_prompt=0,
        window_prompt_is_estimated=True,
        window_limit=limit,
        reserved_output=reserved,
    )


# ---------------------------------------------------------------------- #
# Real usage (after the request)
# ---------------------------------------------------------------------- #


def usage_from_llm(
    llm_usage: LLMUsage,
    *,
    window_usage: ContextUsage | None = None,
    session_total_tokens: int | None = None,
    compression_count: int = 0,
) -> ContextUsage:
    """Convert a provider-reported :class:`LLMUsage` into a :class:`ContextUsage`.

    When the provider returned real numbers they populate ``actual_*``; when it
    did not (``None``), the actual fields stay ``None`` so the UI can keep
    showing the estimate instead of faking a zero (plan §A4 §4.1 §4).
    """

    window = window_usage or ContextUsage()
    has_real_prompt = llm_usage.prompt_tokens is not None
    # The window is only "confirmed" when we have a real prompt reading;
    # otherwise we keep the pre-send estimate and its estimated flag.
    window_prompt = llm_usage.prompt_tokens if has_real_prompt else window.window_prompt
    # Once the provider returns an actual prompt token count, the window value
    # is no longer an estimate — it is the real number that was sent.
    is_estimated = not has_real_prompt
    total = session_total_tokens
    if total is None:
        total = llm_usage.total_tokens or 0
    return ContextUsage(
        window_prompt=window_prompt,
        window_prompt_is_estimated=is_estimated,
        window_limit=window.window_limit,
        reserved_output=window.reserved_output,
        actual_prompt=llm_usage.prompt_tokens,
        actual_completion=llm_usage.completion_tokens,
        session_total_tokens=int(total),
        compression_count=compression_count,
    )


def merge_usage(
    estimated: ContextUsage,
    llm_usage: LLMUsage,
    *,
    session_total_tokens: int,
    compression_count: int = 0,
) -> ContextUsage:
    """Combine a pre-send estimate with a post-send real usage report.

    The estimate contributes the window metadata (limit, reserved output); the
    real report contributes ``actual_*``.  If the provider returned nothing,
    the window stays estimated and actuals stay ``None`` (never faked).
    """

    return usage_from_llm(
        llm_usage,
        window_usage=estimated,
        session_total_tokens=session_total_tokens,
        compression_count=compression_count,
    )


def accumulate_session_total(
    previous_total: int,
    llm_usage: LLMUsage | None,
) -> int:
    """Add one request's tokens to the running session total.

    Falls back to the estimate only when the provider returned nothing, so the
    counter never silently resets to zero on a usage-less response.
    """

    if llm_usage is None:
        return previous_total
    delta = llm_usage.total_tokens
    if delta is None:
        delta = (llm_usage.prompt_tokens or 0) + (llm_usage.completion_tokens or 0)
    return previous_total + int(delta)


# ---------------------------------------------------------------------- #
# Threshold decisions
# ---------------------------------------------------------------------- #


def should_warn_compression(usage: ContextUsage) -> bool:
    """Whether the window is large enough to warn the user."""

    ratio = usage.window_ratio
    return ratio is not None and ratio >= COMPRESSION_WARN_THRESHOLD


def should_auto_compress(usage: ContextUsage) -> bool:
    """Whether the window crossed the automatic-compression threshold."""

    ratio = usage.window_ratio
    return ratio is not None and ratio >= COMPRESSION_AUTO_THRESHOLD


def would_exceed_limit(
    usage: ContextUsage,
    *,
    extra_tokens: int = 0,
) -> bool:
    """Whether adding *extra_tokens* would push the window past its limit.

    Compares ``window_total + extra`` against the limit (plan §A4 §4.2 §6:
    always reserve output room).
    """

    if usage.window_limit is None or usage.window_prompt is None:
        return False
    reserved = usage.reserved_output or 0
    return usage.window_prompt + reserved + extra_tokens > usage.window_limit


def is_protected_context_key(key: str) -> bool:
    """Whether *key* names a context fact the compressor must preserve."""

    return key in PROTECTED_CONTEXT_KEYS


# ---------------------------------------------------------------------- #
# internals
# ---------------------------------------------------------------------- #


def _sum_message_tokens(messages: list[Mapping[str, Any]] | list[dict[str, Any]]) -> int:
    total = 0
    for message in messages or ():
        content = message.get("content", "") if isinstance(message, Mapping) else ""
        # content 可能是纯字符串，也可能是 OpenAI 多模态 content-parts 列表。
        # 对多模态：文本部分按字符估算，图片部分按固定常数（避免按 base64
        # 字符灾难性高估）。
        text = extract_text_from_content(content)
        total += estimate_tokens(text) + 4
        total += estimate_image_tokens(content)
    return total


def _resolve_reserved(reserved_output: int | None, window_limit: int | None) -> int | None:
    if reserved_output is not None:
        return reserved_output
    if window_limit is None:
        return None
    return max(1, int(window_limit * DEFAULT_RESERVED_OUTPUT_RATIO))


def _profile_context_limit(profile: Any) -> int | None:
    limits = getattr(profile, "limits", None)
    if limits is None:
        return None
    value = getattr(limits, "max_context_tokens", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "COMPRESSION_AUTO_THRESHOLD",
    "COMPRESSION_WARN_THRESHOLD",
    "DEFAULT_RESERVED_OUTPUT_RATIO",
    "PROTECTED_CONTEXT_KEYS",
    "accumulate_session_total",
    "budget_for_profile",
    "estimate_window_usage",
    "is_protected_context_key",
    "merge_usage",
    "should_auto_compress",
    "should_warn_compression",
    "usage_from_llm",
    "would_exceed_limit",
]
