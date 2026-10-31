"""Explicit fallback policy for Motion Graphic generation (MG-05).

MG-05 closes the silent-fallback hole.  Previously, when the LLM build failed
(parse error, provider timeout, invalid scene plan), the host automatically
swapped in the hard-coded "AI Agent 介绍动画" template and presented *that* on
the confirmation card as if it were the user's requested clip.  The user had
no way to tell "my custom request failed" from "the model produced this".

This module makes the fallback decision **explicit and labelled**:

* :func:`classify_generation_failure` turns a low-level exception / timeout /
  parse error into one of the three stable states (``provider_unavailable``,
  ``invalid_scene_plan``, ``optional_template_fallback``);
* :class:`MotionGraphicFallbackDecision` records the decision plus the next
  actions available to the user;
* :class:`FallbackPolicy` enforces the rule that a template may **only** be
  generated when the user has explicitly chosen "改用内置模板" — never
  automatically.  The Assistant can show a "generation failed, do you want
  the built-in template?" card, but it cannot silently swap results.

The policy is pure data + functions; it does not import Qt and does not touch
the scene store.  The host calls into it from the build response handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .error_codes import (
    MOTION_GRAPHIC_INVALID_SCENE_PLAN,
    MOTION_GRAPHIC_LLM_BUILD_FAILED,
    MOTION_GRAPHIC_LLM_CANCELLED,
    MOTION_GRAPHIC_LLM_PARSE_FAILED,
    MOTION_GRAPHIC_LLM_TIMEOUT,
    MOTION_GRAPHIC_OPTIONAL_TEMPLATE_FALLBACK,
    MOTION_GRAPHIC_PROVIDER_UNAVAILABLE,
    MotionGraphicFailure,
)
from .proposal import (
    SOURCE_FALLBACK_INVALID_PLAN,
    SOURCE_FALLBACK_OPTIONAL_TEMPLATE,
    SOURCE_FALLBACK_PROVIDER_UNAVAILABLE,
    SOURCE_LLM,
    SOURCE_TEMPLATE,
)


# --------------------------------------------------------------------------- #
# Fallback state
# --------------------------------------------------------------------------- #


class FallbackState(str):
    """The explicit outcome of a generation attempt."""


FALLBACK_NONE = "none"                                # generation succeeded
FALLBACK_PROVIDER_UNAVAILABLE = "provider_unavailable"
FALLBACK_INVALID_SCENE_PLAN = "invalid_scene_plan"
FALLBACK_OPTIONAL_TEMPLATE = "optional_template_fallback"

ALL_FALLBACK_STATES: frozenset[str] = frozenset({
    FALLBACK_NONE,
    FALLBACK_PROVIDER_UNAVAILABLE,
    FALLBACK_INVALID_SCENE_PLAN,
    FALLBACK_OPTIONAL_TEMPLATE,
})

#: Map fallback state → source label used on CreativeProposal / MotionGraphicPlan.
FALLBACK_STATE_TO_SOURCE: dict[str, str] = {
    FALLBACK_NONE: SOURCE_LLM,
    FALLBACK_PROVIDER_UNAVAILABLE: SOURCE_FALLBACK_PROVIDER_UNAVAILABLE,
    FALLBACK_INVALID_SCENE_PLAN: SOURCE_FALLBACK_INVALID_PLAN,
    FALLBACK_OPTIONAL_TEMPLATE: SOURCE_FALLBACK_OPTIONAL_TEMPLATE,
}


def source_for_fallback(state: str) -> str:
    return FALLBACK_STATE_TO_SOURCE.get(state, SOURCE_FALLBACK_INVALID_PLAN)


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicFallbackDecision:
    """The host-facing outcome of one generation attempt.

    Attributes:
        state: One of :data:`ALL_FALLBACK_STATES`.
        error_code: Stable error code if ``state != FALLBACK_NONE``.
        message: Short, user-facing reason.
        allows_template: Whether the built-in template may be offered.  Always
            ``False`` for a successful generation; ``True`` for the two
            generation-failure states so the Assistant can show a "改用内置
            模板" button.  ``FALLBACK_OPTIONAL_TEMPLATE`` only appears after
            the user clicked that button.
        next_actions: Ordered list of actions the user may take.  Drives the
            card's button layout.
    """

    state: str
    error_code: str = ""
    message: str = ""
    allows_template: bool = False
    next_actions: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in ALL_FALLBACK_STATES:
            raise ValueError(f"Unknown fallback state: {self.state!r}")

    @property
    def is_success(self) -> bool:
        return self.state == FALLBACK_NONE

    @property
    def is_failure(self) -> bool:
        return self.state in {FALLBACK_PROVIDER_UNAVAILABLE, FALLBACK_INVALID_SCENE_PLAN}

    @property
    def is_template_chosen(self) -> bool:
        return self.state == FALLBACK_OPTIONAL_TEMPLATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "error_code": self.error_code,
            "message": self.message,
            "allows_template": self.allows_template,
            "next_actions": list(self.next_actions),
            "details": dict(self.details),
        }


# Action labels shown on the card / used by the host to route confirmations.
ACTION_RETRY = "retry"
ACTION_EDIT_BRIEF = "edit_brief"
ACTION_CHOOSE_TEMPLATE = "choose_template"
ACTION_CANCEL = "cancel"


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #


def classify_generation_failure(
    error: BaseException | None,
    *,
    reason: str = "",
) -> MotionGraphicFallbackDecision:
    """Turn a raw exception into a stable fallback decision.

    The classifier prefers the stable ``code`` carried by
    :class:`MotionGraphicFailure`; for generic exceptions it falls back to
    matching the message against common timeout / cancellation phrases so the
    UI still gets a meaningful state instead of "unknown error".
    """

    if error is None and not reason:
        return MotionGraphicFallbackDecision(
            state=FALLBACK_NONE,
            next_actions=(ACTION_RETRY, ACTION_EDIT_BRIEF, ACTION_CANCEL),
        )

    code = getattr(error, "code", "") if error is not None else ""
    message = reason or (str(error) if error is not None else "生成失败。")

    if code == MOTION_GRAPHIC_LLM_CANCELLED or "cancel" in message.lower():
        return MotionGraphicFallbackDecision(
            state=FALLBACK_PROVIDER_UNAVAILABLE,
            error_code=MOTION_GRAPHIC_LLM_CANCELLED,
            message="用户取消了生成。",
            allows_template=True,
            next_actions=(ACTION_RETRY, ACTION_EDIT_BRIEF, ACTION_CHOOSE_TEMPLATE, ACTION_CANCEL),
        )
    if code == MOTION_GRAPHIC_LLM_TIMEOUT or "timeout" in message.lower() or "timed out" in message.lower():
        return MotionGraphicFallbackDecision(
            state=FALLBACK_PROVIDER_UNAVAILABLE,
            error_code=MOTION_GRAPHIC_LLM_TIMEOUT,
            message="生成超时。",
            allows_template=True,
            next_actions=(ACTION_RETRY, ACTION_EDIT_BRIEF, ACTION_CHOOSE_TEMPLATE, ACTION_CANCEL),
        )
    if code in {MOTION_GRAPHIC_LLM_PARSE_FAILED, MOTION_GRAPHIC_INVALID_SCENE_PLAN}:
        return MotionGraphicFallbackDecision(
            state=FALLBACK_INVALID_SCENE_PLAN,
            error_code=code or MOTION_GRAPHIC_INVALID_SCENE_PLAN,
            message="模型返回的场景方案不合法。",
            allows_template=True,
            next_actions=(ACTION_RETRY, ACTION_EDIT_BRIEF, ACTION_CHOOSE_TEMPLATE, ACTION_CANCEL),
        )
    if code in {MOTION_GRAPHIC_PROVIDER_UNAVAILABLE, MOTION_GRAPHIC_LLM_BUILD_FAILED}:
        return MotionGraphicFallbackDecision(
            state=FALLBACK_PROVIDER_UNAVAILABLE,
            error_code=code,
            message=message,
            allows_template=True,
            next_actions=(ACTION_RETRY, ACTION_EDIT_BRIEF, ACTION_CHOOSE_TEMPLATE, ACTION_CANCEL),
        )
    # Unrecognized failure → treat as provider-level so the user can retry or
    # pick a template, but never silently substitute a result.
    return MotionGraphicFallbackDecision(
        state=FALLBACK_PROVIDER_UNAVAILABLE,
        error_code=MOTION_GRAPHIC_LLM_BUILD_FAILED,
        message=message,
        allows_template=True,
        next_actions=(ACTION_RETRY, ACTION_EDIT_BRIEF, ACTION_CHOOSE_TEMPLATE, ACTION_CANCEL),
    )


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


class FallbackPolicy:
    """Enforce "template is opt-in, never automatic".

    The policy exposes :meth:`may_use_template` so the host can decide whether
    to even show the "改用内置模板" affordance, and :meth:`template_chosen` to
    produce the explicit ``FALLBACK_OPTIONAL_TEMPLATE`` decision after the user
    has clicked it.  A bare failure never produces a template result on its
    own.
    """

    def __init__(
        self,
        *,
        template_allowed: bool = True,
        template_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.template_allowed = template_allowed
        self._template_factory = template_factory

    def may_use_template(self, decision: MotionGraphicFallbackDecision) -> bool:
        return (
            self.template_allowed
            and decision.allows_template
            and decision.is_failure
        )

    def template_chosen(self, *, reason: str = "") -> MotionGraphicFallbackDecision:
        """Return the decision produced when the user explicitly opts in."""
        if not self.template_allowed:
            raise MotionGraphicFailure(
                MOTION_GRAPHIC_OPTIONAL_TEMPLATE_FALLBACK,
                "当前策略不允许使用内置模板。",
            )
        return MotionGraphicFallbackDecision(
            state=FALLBACK_OPTIONAL_TEMPLATE,
            error_code=MOTION_GRAPHIC_OPTIONAL_TEMPLATE_FALLBACK,
            message=reason or "已切换为内置模板。",
            allows_template=False,
            next_actions=(ACTION_EDIT_BRIEF, ACTION_CANCEL),
        )

    def build_template_proposal_source(self) -> str:
        """Source label to stamp on a template-originated proposal."""
        return SOURCE_TEMPLATE


__all__ = [
    "FallbackState",
    "FallbackPolicy",
    "MotionGraphicFallbackDecision",
    "ALL_FALLBACK_STATES",
    "FALLBACK_NONE",
    "FALLBACK_PROVIDER_UNAVAILABLE",
    "FALLBACK_INVALID_SCENE_PLAN",
    "FALLBACK_OPTIONAL_TEMPLATE",
    "FALLBACK_STATE_TO_SOURCE",
    "ACTION_RETRY",
    "ACTION_EDIT_BRIEF",
    "ACTION_CHOOSE_TEMPLATE",
    "ACTION_CANCEL",
    "classify_generation_failure",
    "source_for_fallback",
]
