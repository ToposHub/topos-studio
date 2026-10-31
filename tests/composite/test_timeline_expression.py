"""P5 tests: expression status model + precedence policy (T49-T55).

Pure-data tests — no QWidget. The status model + precedence resolver let
the row painter, the upcoming Graph Editor and AI/script all read the same
four-state answer ("does this param have an expression, is it valid, what
is actually driving the value?") without each re-implementing the §5.4
precedence rules.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.utils.expression_parser import (
    ExpressionError,
    ExpressionErrorType,
    ExpressionResult,
)
from src.features.composite.ui.timeline_expression import (
    EXPRESSION_DISABLED,
    EXPRESSION_ERROR,
    EXPRESSION_NONE,
    EXPRESSION_VALID,
    EXPR_ERR_CIRCULAR,
    EXPR_ERR_EMPTY,
    EXPR_ERR_INVALID_FUNCTION,
    EXPR_ERR_PARAM_NOT_FOUND,
    EXPR_ERR_SYNTAX,
    EXPR_ERR_UNDEFINED_VAR,
    EXPR_ERR_UNKNOWN,
    EXPRESSION_ERROR_CODES,
    ExpressionPolicy,
    ExpressionStatus,
    classify_expression_error,
    derive_expression_status,
    resolve_precedence,
)

pytestmark = pytest.mark.composite_core


# ── node stand-ins (duck-typed; tests stay GUI-free) ────────────────────


class _FakeNode:
    def __init__(self, expression="", has_kfs=False, enabled=True,
                 store_enabled=False):
        self._expressions = {"rotation": expression} if expression else {}
        self._has_kfs = has_kfs
        if store_enabled:
            self._expression_enabled = {"rotation": enabled}

    def has_expression(self, param):
        return bool(self._expressions.get(param))

    def get_expression(self, param):
        return self._expressions.get(param) or ""

    def is_param_animated(self, param):
        return self._has_kfs


# ── T49: four-state status ──────────────────────────────────────────────


class TestExpressionStatusStates:
    def test_no_expression_is_none(self):
        node = _FakeNode(expression="")
        s = derive_expression_status(node, "rotation")
        assert s.state == EXPRESSION_NONE
        assert not s.has_expression

    def test_valid_expression_text(self):
        node = _FakeNode(expression="value + 10")
        s = derive_expression_status(node, "rotation")
        assert s.state == EXPRESSION_VALID
        assert s.has_expression
        assert s.text == "value + 10"
        assert s.enabled is True

    def test_syntax_error_expression_is_error_state(self):
        node = _FakeNode(expression="value +")  # incomplete
        s = derive_expression_status(node, "rotation")
        assert s.state == EXPRESSION_ERROR
        assert s.is_error
        assert s.error_code == EXPR_ERR_SYNTAX

    def test_disabled_expression(self):
        node = _FakeNode(expression="value + 10", enabled=False,
                         store_enabled=True)
        s = derive_expression_status(node, "rotation")
        assert s.state == EXPRESSION_DISABLED
        assert s.has_expression  # the text is still there
        assert s.enabled is False

    def test_status_with_live_valid_result_prefers_it(self):
        node = _FakeNode(expression="value + 10")
        result = ExpressionResult(success=True, value=20.0)
        s = derive_expression_status(node, "rotation", last_result=result)
        assert s.state == EXPRESSION_VALID

    def test_status_with_live_error_result(self):
        node = _FakeNode(expression="1/0")
        result = ExpressionResult(
            success=False,
            error=ExpressionError(
                ExpressionErrorType.DIVISION_BY_ZERO, "divide by zero"))
        s = derive_expression_status(node, "rotation", last_result=result)
        assert s.state == EXPRESSION_ERROR
        assert s.error_code == "expr_division_by_zero"

    def test_status_to_dict_round_trip(self):
        node = _FakeNode(expression="value")
        s = derive_expression_status(node, "rotation")
        d = s.to_dict()
        assert d["state"] == EXPRESSION_VALID
        assert d["text"] == "value"
        assert d["enabled"] is True


# ── source-value labelling (§5.4 rule 6) ───────────────────────────────


class TestSourceValueLabel:
    def test_expression_over_keyframes(self):
        node = _FakeNode(expression="value + 10", has_kfs=True)
        s = derive_expression_status(node, "rotation")
        assert s.source_value == "keyframes"

    def test_expression_over_static(self):
        node = _FakeNode(expression="value + 10", has_kfs=False)
        s = derive_expression_status(node, "rotation")
        assert s.source_value == "static"

    def test_error_falls_back_label(self):
        node = _FakeNode(expression="value +", has_kfs=True)
        s = derive_expression_status(node, "rotation")
        # fallback_on_error default True → report the fallback source.
        assert s.state == EXPRESSION_ERROR
        assert s.source_value == "keyframes"  # fallback target


# ── T53: error classification ───────────────────────────────────────────


class TestErrorClassification:
    @pytest.mark.parametrize("etype,expected_code", [
        (ExpressionErrorType.SYNTAX_ERROR, EXPR_ERR_SYNTAX),
        (ExpressionErrorType.UNDEFINED_VARIABLE, EXPR_ERR_UNDEFINED_VAR),
        (ExpressionErrorType.INVALID_FUNCTION, EXPR_ERR_INVALID_FUNCTION),
        (ExpressionErrorType.DIVISION_BY_ZERO, "expr_division_by_zero"),
        (ExpressionErrorType.TYPE_ERROR, "expr_type"),
        (ExpressionErrorType.CIRCULAR_DEPENDENCY, EXPR_ERR_CIRCULAR),
        (ExpressionErrorType.NODE_NOT_FOUND, "expr_node_not_found"),
        (ExpressionErrorType.PARAM_NOT_FOUND, EXPR_ERR_PARAM_NOT_FOUND),
    ])
    def test_each_error_type_maps_to_stable_code(self, etype, expected_code):
        err = ExpressionError(etype, "msg")
        code, msg = classify_expression_error(err)
        assert code == expected_code
        assert msg == "msg"

    def test_none_error_yields_empty(self):
        code, msg = classify_expression_error(None)
        assert code == ""
        assert msg == ""

    def test_unknown_error_type_falls_back(self):
        # Construct an error with a fabricated enum member to simulate a
        # future error type not yet in the code map.
        class _FakeType:
            pass
        err = ExpressionError.__new__(ExpressionError)
        err.error_type = _FakeType()
        err.message = "x"
        code, _ = classify_expression_error(err)
        assert code == EXPR_ERR_UNKNOWN

    def test_error_code_map_is_complete(self):
        # Every enum member must have a stable code (T53 contract).
        for etype in ExpressionErrorType:
            assert etype in EXPRESSION_ERROR_CODES, (
                f"missing stable code for {etype}")


# ── precedence resolver (§5.4) ─────────────────────────────────────────


class TestPrecedenceResolver:
    def test_no_expression_base_wins(self):
        status = ExpressionStatus(state=EXPRESSION_NONE)
        p = resolve_precedence(status, base_value=5.0,
                               expression_value=None)
        assert p.effective_value == 5.0
        assert p.effective_source in ("static", "keyframes")
        assert not p.expression_overrode_base

    def test_valid_expression_overrides_base(self):
        status = ExpressionStatus(state=EXPRESSION_VALID,
                                  source_value="keyframes")
        p = resolve_precedence(status, base_value=5.0,
                               expression_value=20.0)
        assert p.effective_value == 20.0
        assert p.effective_source == "expression"
        assert p.expression_overrode_base is True
        assert p.base_value == 5.0  # still tracked for pre/post graph

    def test_error_with_fallback_returns_base(self):
        status = ExpressionStatus(state=EXPRESSION_ERROR,
                                  source_value="keyframes",
                                  error_code="expr_syntax")
        p = resolve_precedence(status, base_value=5.0,
                               expression_value=None)
        assert p.effective_value == 5.0
        assert p.effective_source == "fallback"

    def test_error_without_fallback_policy(self):
        # Hard-fail policy: even on error, no fallback value.
        status = ExpressionStatus(state=EXPRESSION_ERROR,
                                  source_value="keyframes")
        policy = ExpressionPolicy(fallback_on_error=False)
        p = resolve_precedence(status, base_value=5.0,
                               expression_value=None, policy=policy)
        # base still reported, but effective source isn't "fallback"
        assert p.effective_source != "fallback"

    def test_disabled_expression_uses_base(self):
        status = ExpressionStatus(state=EXPRESSION_DISABLED, text="value",
                                  enabled=False)
        p = resolve_precedence(status, base_value=5.0,
                               expression_value=99.0)
        assert p.effective_value == 5.0  # base wins, expression ignored
        assert not p.expression_overrode_base


# ── policy (P0 §5.3 decision) ───────────────────────────────────────────


class TestExpressionPolicy:
    def test_default_policy_is_coexistence(self):
        """N-04（2026-10-14 审计）：默认即共存模型——base（静态/关键帧）
        + 启用的表达式覆盖 base；A-15 互斥成为历史。"""
        p = ExpressionPolicy()
        assert p.allow_keyframe_expression_coexistence is True
        assert p.fallback_on_error is True
        assert p.allow_disable_without_delete is True

    def test_policy_is_frozen(self):
        p = ExpressionPolicy()
        with pytest.raises(Exception):
            p.fallback_on_error = False  # type: ignore[misc]

    def test_coexistence_flag_enables_layered_model(self):
        # The plan's §5.4 target. The host flips this when it lands the
        # node-side storage; the status model already reads keyframes +
        # expressions independently so it works either way.
        p = ExpressionPolicy(allow_keyframe_expression_coexistence=True)
        assert p.allow_keyframe_expression_coexistence is True


# ── node duck-typing robustness ─────────────────────────────────────────


class TestNodeDuckTyping:
    def test_node_without_expression_methods_uses_private_dicts(self):
        class _Bare:
            _expressions = {"x": "value"}
            keyframe_tracks = {}
        s = derive_expression_status(_Bare(), "x")
        assert s.state == EXPRESSION_VALID

    def test_node_with_failing_methods_returns_safe_status(self):
        class _Broken:
            def has_expression(self, p):
                raise RuntimeError("boom")
        s = derive_expression_status(_Broken(), "x")
        assert s.state == EXPRESSION_NONE  # safe default

    def test_node_without_enabled_map_treats_expression_as_enabled(self):
        node = _FakeNode(expression="value")  # no _expression_enabled
        s = derive_expression_status(node, "rotation")
        assert s.enabled is True
        assert s.state == EXPRESSION_VALID
