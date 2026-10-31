"""MT-09: controlled expression evaluator — a pure, sandboxed mini-language.

Motion values may reference a restricted expression instead of a literal (e.g.
``wiggle(frame, 3) * 0.5`` or ``frame % 24``).  This evaluator is the safe
boundary: it parses the expression with Python's ``ast``, walks the tree and
rejects EVERYTHING that is not on an explicit allowlist, then evaluates in a
sandbox with no builtins, no attributes, no imports, no file/network/reflective
access.

Allowed:
* numeric + string + boolean literals;
* arithmetic, comparison, boolean and ternary operators;
* a whitelist of pure names (frame, fps, seed, markers, math constants) and
  pure functions (abs/min/max/round/clamp + math trig/powers);

Rejected (raises ``ExpressionError`` before evaluation): names/calls/attributes
not on the allowlist, imports, assignments, comprehensions with side effects,
and expressions exceeding the complexity budget.  Output is deterministic given
(frame, fps, seed, markers) — preview and export ask the same question.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from typing import Any, Mapping


class ExpressionError(ValueError):
    """Raised when an expression is rejected by the allowlist or budget."""


_MAX_NODES = 200
_MAX_NUMERIC_MAGNITUDE = 1_000_000_000.0
# Bit budget for integer ``**``/``pow`` results.  Integer exponentiation is not
# bounded by the float overflow path, so ``9 ** 9 ** 9`` would otherwise spend
# minutes/GBs computing a 3.7e8-digit int before any post-eval magnitude check.
_MAX_POW_BITS = 4096

_ALLOWED_BINOPS = {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow}
_ALLOWED_UNARYOPS = {ast.UAdd, ast.USub, ast.Not}
_ALLOWED_CMPOPS = {ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE}
_ALLOWED_BOOLOPS = {ast.And, ast.Or}

# Pure constant names injected into the sandbox.
_CONSTANT_NAMES: dict[str, Any] = {
    "pi": math.pi, "e": math.e, "tau": math.tau, "phi": (1 + math.sqrt(5)) / 2,
    "true": True, "false": False, "True": True, "False": False,
}

# Pure functions exposed to expressions.  Each maps to a callable that takes
# only positional args and returns a number/bool — no I/O, no reflection.
def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _step(edge: float, x: float) -> float:
    return 0.0 if x < edge else 1.0


def _safe_pow(base: Any, exp: Any) -> Any:
    """Guarded exponentiation shared by the ``**`` operator and ``pow()``.

    Integer results are bounded by ``_MAX_POW_BITS`` so a hostile or mistyped
    expression (``9 ** 9 ** 9``) fails fast with ``ExpressionError`` instead of
    hanging the render thread.  Float overflow still raises ``OverflowError``,
    which the evaluator translates to ``ExpressionError``.
    """
    if isinstance(base, bool):
        base = int(base)
    if isinstance(exp, bool):
        exp = int(exp)
    if isinstance(base, int) and isinstance(exp, int) and exp >= 0 and abs(base) > 1:
        if exp * abs(base).bit_length() > _MAX_POW_BITS:
            raise ExpressionError("integer power result exceeds complexity budget")
    return base ** exp


_FUNCTION_NAMES: dict[str, Any] = {
    "abs": abs, "min": min, "max": max, "round": round,
    "floor": math.floor, "ceil": math.ceil, "trunc": math.trunc,
    "sqrt": math.sqrt, "pow": _safe_pow, "exp": math.exp, "log": math.log,
    "log10": math.log10, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "radians": math.radians, "degrees": math.degrees,
    "sign": lambda x: (x > 0) - (x < 0),
    "clamp": _clamp, "lerp": _lerp, "step": _step,
}

# Names that resolve from the per-call context (frame/fps/seed/markers).
_CONTEXT_NAMES = frozenset({"frame", "fps", "seed", "markers"})


_ALLOWED_NODE_TYPES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Constant, ast.Name, ast.Call, ast.Tuple, ast.List, ast.Subscript,
    ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


@dataclass(frozen=True)
class ExpressionContext:
    """The deterministic inputs an expression may read."""

    frame: int = 0
    fps: float = 24.0
    seed: int = 0
    markers: Mapping[str, int] | None = None


class _Validator(ast.NodeVisitor):
    def __init__(self) -> None:
        self.node_count = 0

    def _tick(self) -> None:
        self.node_count += 1
        if self.node_count > _MAX_NODES:
            raise ExpressionError(f"expression exceeds complexity budget ({_MAX_NODES} nodes)")

    def generic_visit(self, node: ast.AST) -> None:
        self._tick()
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            raise ExpressionError(f"disallowed construct: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._tick()
        if node.id not in _CONSTANT_NAMES and node.id not in _FUNCTION_NAMES and node.id not in _CONTEXT_NAMES:
            raise ExpressionError(f"disallowed name: {node.id}")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self._tick()
        if isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant) \
                and isinstance(node.right.value, (int, float)) \
                and not isinstance(node.right.value, bool) \
                and abs(node.right.value) > _MAX_POW_BITS:
            raise ExpressionError("power exponent exceeds complexity budget")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._tick()
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("only direct function calls are allowed")
        if node.func.id not in _FUNCTION_NAMES:
            raise ExpressionError(f"disallowed function: {node.func.id}")
        if node.keywords:
            raise ExpressionError("keyword arguments are not allowed")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        self._tick()
        if not isinstance(node.value, (int, float, str, bool)):
            raise ExpressionError(f"disallowed literal type: {type(node.value).__name__}")


class _PowGuard(ast.NodeTransformer):
    """Rewrite every ``a ** b`` into ``_safe_pow(a, b)`` before compilation.

    Python's integer ``**`` has no result-size bound, so the operator itself
    must be funneled through the guarded helper (same guard the ``pow()``
    function uses).
    """

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if not isinstance(node.op, ast.Pow):
            return node
        return ast.copy_location(
            ast.Call(func=ast.Name(id="_pow_guard", ctx=ast.Load()),
                     args=[node.left, node.right], keywords=[]),
            node,
        )


def validate_expression(source: str) -> None:
    """Parse + allowlist-check an expression without evaluating it."""
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error: {exc.msg}") from exc
    _Validator().visit(tree)


def evaluate_expression(source: str, context: ExpressionContext) -> Any:
    """Validate then evaluate an expression against the given context.

    The sandbox has no ``__builtins__``; the only names available are the
    whitelisted constants, functions and the context values.  Evaluation cannot
    import, open files, reach the network or reflect on attributes.
    """
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error: {exc.msg}") from exc
    _Validator().visit(tree)
    sandbox_globals: dict[str, Any] = {"__builtins__": {}}
    sandbox_globals.update(_CONSTANT_NAMES)
    sandbox_globals.update(_FUNCTION_NAMES)
    sandbox_globals["frame"] = context.frame
    sandbox_globals["fps"] = context.fps
    sandbox_globals["seed"] = context.seed
    sandbox_globals["markers"] = dict(context.markers or {})
    sandbox_globals["_pow_guard"] = _safe_pow
    guarded_tree = _PowGuard().visit(tree)
    ast.fix_missing_locations(guarded_tree)
    compiled = compile(guarded_tree, "<motion-expression>", "eval")
    try:
        result = eval(compiled, sandbox_globals, {})  # noqa: S307 - sandboxed allowlist eval
        if isinstance(result, (int, float)):
            numeric = float(result)
            if not math.isfinite(numeric):
                raise ExpressionError("expression result must be finite")
            if abs(numeric) > _MAX_NUMERIC_MAGNITUDE:
                raise ExpressionError("expression result exceeds numeric range")
        return result
    except ZeroDivisionError as exc:
        raise ExpressionError("division by zero in expression") from exc
    except ExpressionError:
        raise  # already a structured error (e.g. _safe_pow budget guard)
    except KeyError as exc:
        key = exc.args[0] if exc.args else ""
        raise ExpressionError(f"unknown name or marker key: {key}") from exc
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ExpressionError(f"evaluation error: {exc}") from exc


__all__ = ["ExpressionContext", "ExpressionError", "evaluate_expression", "validate_expression"]
