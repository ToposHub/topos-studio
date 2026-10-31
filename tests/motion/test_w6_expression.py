"""MT-09: controlled expression evaluator — math correctness + sandbox safety."""

from __future__ import annotations

import math

import pytest

from src.features.teaching_animation.motion import (
    ExpressionContext, ExpressionError, evaluate_expression, validate_expression,
)


class TestMath:
    def test_arithmetic(self) -> None:
        ctx = ExpressionContext(frame=6, fps=24.0)
        assert evaluate_expression("frame * 2 + 1", ctx) == 13
        assert evaluate_expression("frame / fps", ctx) == pytest.approx(0.25)

    def test_functions(self) -> None:
        ctx = ExpressionContext(frame=0)
        assert evaluate_expression("clamp(5, 0, 1)", ctx) == 1.0
        assert evaluate_expression("lerp(0, 10, 0.5)", ctx) == 5.0
        assert evaluate_expression("min(3, 7)", ctx) == 3
        assert evaluate_expression("abs(-4)", ctx) == 4

    def test_constants(self) -> None:
        ctx = ExpressionContext()
        assert evaluate_expression("pi", ctx) == pytest.approx(math.pi)

    def test_ternary_and_comparison(self) -> None:
        ctx = ExpressionContext(frame=10)
        assert evaluate_expression("frame > 5 and frame < 20", ctx) is True
        assert evaluate_expression("frame if frame > 0 else -1", ctx) == 10

    def test_markers_access(self) -> None:
        ctx = ExpressionContext(frame=3, markers={"beat": 4})
        assert evaluate_expression("markers['beat'] - frame", ctx) == 1

    def test_deterministic(self) -> None:
        ctx = ExpressionContext(frame=12, fps=30.0, seed=7)
        a = evaluate_expression("sin(frame / fps) + seed", ctx)
        b = evaluate_expression("sin(frame / fps) + seed", ctx)
        assert a == b


class TestSafety:
    @pytest.mark.parametrize("source", [
        "__import__('os')",
        "open('/etc/passwd')",
        "().__class__.__bases__",
        "frame.__class__",
        "globals()",
        "locals()",
        "eval('1')",
        "compile('x', 'y', 'eval')",
        "getattr(frame, 'real')",
        "frame.bit_length()",        # attribute access
        "lambda x: x",               # lambda not allowed
        "[x for x in range(3)]",     # comprehension not allowed
        "unknown_var + 1",           # unknown name
        "print(1)",                  # builtin not whitelisted
        "import os",                 # import (syntax-different in eval mode)
    ])
    def test_rejects_dangerous_constructs(self, source: str) -> None:
        with pytest.raises(ExpressionError):
            evaluate_expression(source, ExpressionContext())

    def test_validate_does_not_evaluate(self) -> None:
        # validate only parses + checks; safe expression passes.
        validate_expression("frame * 2")
        with pytest.raises(ExpressionError):
            validate_expression("__import__('os')")

    def test_complexity_budget(self) -> None:
        # 250 additions -> over the 200-node budget.
        big = " + ".join(["1"] * 250)
        with pytest.raises(ExpressionError):
            evaluate_expression(big, ExpressionContext())

    def test_no_builtins_leak(self) -> None:
        # Even if a name looks safe, only the whitelist resolves.
        with pytest.raises(ExpressionError):
            evaluate_expression("len('abc')", ExpressionContext())

    def test_division_by_zero_raises_expression_error(self) -> None:
        with pytest.raises(ExpressionError):
            evaluate_expression("1 / 0", ExpressionContext())

    def test_numeric_range_budget(self) -> None:
        with pytest.raises(ExpressionError, match="numeric range"):
            evaluate_expression("10 ** 20", ExpressionContext())
