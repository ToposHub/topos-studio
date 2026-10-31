"""Expression status model + precedence policy for the AE-style timeline.

Pure-data layer for P5 (T49-T55). The current pipeline already evaluates
expressions correctly relative to keyframes (P0 §5.2 confirmed: keyframes
first, then expressions seeded with that value, error → fallback). But two
gaps remain that the UI/AI/script need exposed:

1. **Mutual exclusion (A-15).** Today ``set_expression`` clears the
   keyframe track and ``set_keyframe_track`` clears the expression, so a
   parameter can never have both. The plan (§5.4) wants users to be able to
   layer an expression *on top of* keyframes. This module centralises the
   policy so the host can flip it without touching every call site.
2. **Four-state visibility.** A parameter row needs to show one of
   ``none / valid / error / disabled`` (T49) regardless of who set the
   expression or when it last evaluated. :class:`ExpressionStatus` derives
   that state from a node + a frame context + the last evaluation result.

Design rules:

* GUI-free. Imports only the existing ``ExpressionResult`` /
  ``ExpressionErrorType`` from ``utils.expression_parser`` and the node's
  expression storage. No QWidget.
* Policy as data. :class:`ExpressionPolicy` is a frozen config the host
  owns; switching ``allow_keyframe_expression_coexistence`` from False
  (today's A-15) to True (the plan's target) is one flag, not a rewrite.
* Stable error codes for AI/script (T53): every error maps to a string in
  ``EXPRESSION_ERROR_CODES`` so a manifest can declare them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from src.features.composite.utils.expression_parser import (
    ExpressionError,
    ExpressionErrorType,
    ExpressionParser,
    ExpressionResult,
)

import logging

_logger = logging.getLogger(__name__)


# ── four-state status (T49) ─────────────────────────────────────────────

EXPRESSION_NONE = "none"            # no expression on this param
EXPRESSION_VALID = "valid"          # expression present + last eval ok
EXPRESSION_ERROR = "error"          # expression present + last eval failed
EXPRESSION_DISABLED = "disabled"    # expression present but toggled off


@dataclass(frozen=True)
class ExpressionStatus:
    """Derived 4-state view of one parameter's expression.

    Built by :func:`derive_expression_status` from a node + the last
    evaluation result. Pure data — the row painter / AI / script all read
    the same object.
    """

    state: str  # one of EXPRESSION_*
    text: str = ""
    error_code: str = ""     # stable code (see EXPRESSION_ERROR_CODES), "" if none
    error_message: str = ""  # human-readable detail
    source_value: str = ""   # "keyframes" | "static" | "fallback" — what fed `value`
    enabled: bool = True

    @property
    def has_expression(self) -> bool:
        return self.state in (EXPRESSION_VALID, EXPRESSION_ERROR,
                              EXPRESSION_DISABLED)

    @property
    def is_error(self) -> bool:
        return self.state == EXPRESSION_ERROR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "text": self.text,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "source_value": self.source_value,
            "enabled": self.enabled,
        }


# ── policy (P0 §5.3 decision) ───────────────────────────────────────────


@dataclass(frozen=True)
class ExpressionPolicy:
    """Centralised expression/keyframe precedence policy.

    N-04（2026-10-14 审计）后默认即共存模型（base = 静态/关键帧，
    启用的表达式覆盖 base，loopOut/valueAtTime 采样 pre-expression
    轨道）；``base_node.set_expression`` / ``set_keyframe_track`` 不再
    互相清除。锁约束由 :class:`TrackLockRegistry`（timeline_commands.py）
    执行。
    """

    # When False (legacy A-15): setting an expression clears the keyframe
    # track and vice-versa. When True (N-04 default): both coexist; the
    # expression's `value` variable reads the keyframe-evaluated result.
    allow_keyframe_expression_coexistence: bool = True
    # When True, an expression evaluation error falls back to the keyframe/
    # static value silently and only flags the row red (plan §5.4 rule 4).
    # When False, an error propagates as a hard failure (not recommended —
    # it would black out the whole composite).
    fallback_on_error: bool = True
    # When True, expressions can be disabled without deleting the text
    # (plan §5.4 rule 5 / T49 4-state). The host stores this in
    # node._expression_enabled alongside node._expressions.
    allow_disable_without_delete: bool = True


# ── stable error codes (T53) ────────────────────────────────────────────

EXPR_ERR_SYNTAX = "expr_syntax"
EXPR_ERR_UNDEFINED_VAR = "expr_undefined_variable"
EXPR_ERR_INVALID_FUNCTION = "expr_invalid_function"
EXPR_ERR_DIVISION_BY_ZERO = "expr_division_by_zero"
EXPR_ERR_TYPE = "expr_type"
EXPR_ERR_CIRCULAR = "expr_circular_dependency"
EXPR_ERR_NODE_NOT_FOUND = "expr_node_not_found"
EXPR_ERR_PARAM_NOT_FOUND = "expr_param_not_found"
EXPR_ERR_EMPTY = "expr_empty"
EXPR_ERR_UNKNOWN = "expr_unknown"


EXPRESSION_ERROR_CODES: Dict[ExpressionErrorType, str] = {
    ExpressionErrorType.SYNTAX_ERROR: EXPR_ERR_SYNTAX,
    ExpressionErrorType.UNDEFINED_VARIABLE: EXPR_ERR_UNDEFINED_VAR,
    ExpressionErrorType.INVALID_FUNCTION: EXPR_ERR_INVALID_FUNCTION,
    ExpressionErrorType.DIVISION_BY_ZERO: EXPR_ERR_DIVISION_BY_ZERO,
    ExpressionErrorType.TYPE_ERROR: EXPR_ERR_TYPE,
    ExpressionErrorType.CIRCULAR_DEPENDENCY: EXPR_ERR_CIRCULAR,
    ExpressionErrorType.NODE_NOT_FOUND: EXPR_ERR_NODE_NOT_FOUND,
    ExpressionErrorType.PARAM_NOT_FOUND: EXPR_ERR_PARAM_NOT_FOUND,
}


def classify_expression_error(error: Optional[ExpressionError]) -> Tuple[str, str]:
    """Return (stable_code, message) for an ExpressionError.

    A None error yields ("", ""). An empty/whitespace-only expression never
    reaches this function as an :class:`ExpressionError` —
    :func:`derive_expression_status` short-circuits it to EXPR_ERR_EMPTY (a
    distinct code so the UI can prompt "enter an expression" instead of
    showing a generic syntax error) before any validation runs.
    """
    if error is None:
        return "", ""
    code = EXPRESSION_ERROR_CODES.get(error.error_type, EXPR_ERR_UNKNOWN)
    return code, str(error.message or "")


# ── status derivation (T49 / T53) ───────────────────────────────────────


def derive_expression_status(
    node: Any,
    param_name: str,
    *,
    last_result: Optional[ExpressionResult] = None,
    policy: ExpressionPolicy = ExpressionPolicy(),
) -> ExpressionStatus:
    """Build the 4-state :class:`ExpressionStatus` for a node param.

    ``node`` must expose ``has_expression(param)`` / ``get_expression(param)``
    (BaseNode already does). ``last_result`` is the most recent
    :class:`ExpressionResult` from the evaluator — pass None if the
    expression hasn't been evaluated this frame (status falls back to
    VALID if the text parses, ERROR if it doesn't, using a lightweight
    syntax check).

    The "source_value" field reports what fed the expression's ``value``
    variable, so the row tooltip can say "Expression (over keyframes)" vs
    "Expression (over static)" vs "Fallback to keyframes" — matching the
    plan's §5.4 rule 6 ("UI must clearly show whether the result is
    Keyframes, Expression or Fallback").
    """
    if not _node_has_expression(node, param_name):
        return ExpressionStatus(state=EXPRESSION_NONE)

    text = _node_get_expression(node, param_name) or ""
    enabled = _node_expression_enabled(node, param_name, policy)
    if not enabled:
        return ExpressionStatus(
            state=EXPRESSION_DISABLED, text=text, enabled=False)

    has_keyframes = _node_param_has_keyframes(node, param_name)
    source_value = "keyframes" if has_keyframes else "static"

    # AUD-11021: the documented EXPR_ERR_EMPTY contract — an empty or
    # whitespace-only expression maps to its own stable code so the UI can
    # prompt "enter an expression" instead of a generic syntax error.
    if not text.strip():
        return ExpressionStatus(
            state=EXPRESSION_ERROR, text=text, error_code=EXPR_ERR_EMPTY,
            error_message="Expression is empty",
            source_value=source_value, enabled=True)

    # Prefer the live evaluation result; fall back to a syntax-only check.
    if last_result is not None:
        if last_result.success:
            return ExpressionStatus(
                state=EXPRESSION_VALID, text=text, enabled=True,
                source_value=source_value)
        code, msg = classify_expression_error(last_result.error)
        # Error → report the fallback source so the UI can show
        # "Fallback to keyframes" rather than the bare expression text.
        fallback_source = source_value if policy.fallback_on_error else "none"
        return ExpressionStatus(
            state=EXPRESSION_ERROR, text=text, error_code=code,
            error_message=msg, source_value=fallback_source, enabled=True)

    # No evaluation yet: do a cheap syntax validation so the row can render
    # before the first frame eval. The parser lives in the same package that
    # is already imported at module top, so no lazy import (and no broad
    # except) is needed here.
    # AUD-11020: a parser-internal crash must surface as an unknown ERROR
    # code — never silently as VALID.
    try:
        result = ExpressionParser().validate(text)
    except (TypeError, ValueError, RecursionError) as exc:
        _logger.warning("Expression parser crashed on %r: %s", text, exc)
        return ExpressionStatus(
            state=EXPRESSION_ERROR, text=text, error_code=EXPR_ERR_UNKNOWN,
            error_message=f"expression parser failure: {exc}",
            source_value=source_value, enabled=True)
    if result.success:
        return ExpressionStatus(
            state=EXPRESSION_VALID, text=text, enabled=True,
            source_value=source_value)
    code, msg = classify_expression_error(result.error)
    return ExpressionStatus(
        state=EXPRESSION_ERROR, text=text, error_code=code,
        error_message=msg, source_value=source_value, enabled=True)


# ── precedence model (§5.4 / P0 §5.3 decision) ──────────────────────────


@dataclass(frozen=True)
class ExpressionPrecedence:
    """Describes how a parameter's value is composed for one frame.

    The plan's §5.4 precedence:

    1. keyframe/static value (the "base")
    2. expression result, if valid and enabled, overrides the base
    3. on expression error, fall back to the base

    This dataclass is the *answer* the UI/AI asks for: "what's actually
    driving this parameter right now?" :func:`resolve_precedence` computes
    it from a status + the base value, so the row tooltip and the Graph
    Editor's pre/post-expression graph can label themselves correctly.
    """

    base_source: str    # "keyframes" | "static"
    effective_source: str  # "keyframes" | "static" | "expression" | "fallback"
    base_value: Optional[float]
    effective_value: Optional[float]
    expression_overrode_base: bool


def resolve_precedence(
    status: ExpressionStatus,
    base_value: Optional[float],
    expression_value: Optional[float],
    *,
    policy: ExpressionPolicy = ExpressionPolicy(),
) -> ExpressionPrecedence:
    """Compute the effective value + its source given a status + values.

    ``base_value`` is the keyframe/static evaluation. ``expression_value``
    is what the expression returned (None if it didn't run/errored). The
    return value tells the caller which one to display + commit.
    """
    base_source = status.source_value or "static"
    if status.state == EXPRESSION_VALID and expression_value is not None:
        return ExpressionPrecedence(
            base_source=base_source,
            effective_source="expression",
            base_value=base_value,
            effective_value=expression_value,
            expression_overrode_base=True)
    if status.state == EXPRESSION_ERROR and policy.fallback_on_error:
        return ExpressionPrecedence(
            base_source=base_source,
            effective_source="fallback",
            base_value=base_value,
            effective_value=base_value,
            expression_overrode_base=False)
    if status.state == EXPRESSION_DISABLED and status.has_expression:
        # Disabled expression → base value wins.
        return ExpressionPrecedence(
            base_source=base_source,
            effective_source=base_source,
            base_value=base_value,
            effective_value=base_value,
            expression_overrode_base=False)
    # No expression, or no value — base wins.
    return ExpressionPrecedence(
        base_source=base_source,
        effective_source=base_source,
        base_value=base_value,
        effective_value=base_value,
        expression_overrode_base=False)


# ── node accessors (duck-typed, tolerate partial test doubles) ──────────


def _node_has_expression(node: Any, param_name: str) -> bool:
    fn = getattr(node, "has_expression", None)
    if callable(fn):
        try:
            return bool(fn(param_name))
        except Exception:
            return False
    exprs = getattr(node, "_expressions", None) or {}
    return bool(exprs.get(param_name))


def _node_get_expression(node: Any, param_name: str) -> str:
    fn = getattr(node, "get_expression", None)
    if callable(fn):
        try:
            return str(fn(param_name) or "")
        except Exception:
            return ""
    exprs = getattr(node, "_expressions", None) or {}
    return str(exprs.get(param_name) or "")


def _node_expression_enabled(node: Any, param_name: str,
                             policy: ExpressionPolicy) -> bool:
    """Read the per-param enabled flag.

    BaseNode doesn't yet store ``_expression_enabled``; when the flag is
    absent the expression is considered enabled (preserves today's
    behaviour). Once P5-view lands the storage, this reads it directly.
    """
    if not policy.allow_disable_without_delete:
        return True
    enabled_map = getattr(node, "_expression_enabled", None)
    if not isinstance(enabled_map, dict):
        return True
    return bool(enabled_map.get(param_name, True))


def _node_param_has_keyframes(node: Any, param_name: str) -> bool:
    fn = getattr(node, "is_param_animated", None)
    if callable(fn):
        try:
            return bool(fn(param_name))
        except Exception:
            return False
    tracks = getattr(node, "keyframe_tracks", None) or {}
    return param_name in tracks


__all__ = [
    # states
    "EXPRESSION_NONE", "EXPRESSION_VALID", "EXPRESSION_ERROR",
    "EXPRESSION_DISABLED",
    # error codes
    "EXPR_ERR_SYNTAX", "EXPR_ERR_UNDEFINED_VAR", "EXPR_ERR_INVALID_FUNCTION",
    "EXPR_ERR_DIVISION_BY_ZERO", "EXPR_ERR_TYPE", "EXPR_ERR_CIRCULAR",
    "EXPR_ERR_NODE_NOT_FOUND", "EXPR_ERR_PARAM_NOT_FOUND", "EXPR_ERR_EMPTY",
    "EXPR_ERR_UNKNOWN", "EXPRESSION_ERROR_CODES",
    # data
    "ExpressionStatus", "ExpressionPolicy", "ExpressionPrecedence",
    # functions
    "classify_expression_error", "derive_expression_status",
    "resolve_precedence",
]
