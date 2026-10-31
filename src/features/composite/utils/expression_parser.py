"""
Expression Parser - Safe mathematical expression evaluation for node parameters.

Provides a secure expression evaluator similar to NUKE's Expression node.
Supports:
- Mathematical operations: +, -, *, /, %, **, //, comparison operators
- Math functions: sin, cos, tan, sqrt, abs, floor, ceil, round, min, max, log, exp, pow
- Variable references:
  - this.xxx: current node's parameters (e.g., this.width, this.radius)
  - NodeName.param: other node's parameters (e.g., Blur1.radius)
  - input.xxx: input attributes (input.width, input.height)
  - frame: current frame number
- Ternary expressions: condition ? value_if_true : value_if_false

Security: Uses Python's ast module to parse and evaluate expressions safely.
No arbitrary code execution - only whitelisted operations and functions.

Example expressions:
  - "this.width * 0.5"
  - "Blur1.radius + 10"
  - "sin(frame * 0.1) * 50"
  - "input.width > 1000 ? 1.0 : 0.5"
"""

import ast
import hashlib
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from enum import Enum


class ExpressionErrorType(Enum):
    """Types of expression errors for UI feedback."""
    SYNTAX_ERROR = "syntax_error"
    UNDEFINED_VARIABLE = "undefined_variable"
    INVALID_FUNCTION = "invalid_function"
    DIVISION_BY_ZERO = "division_by_zero"
    TYPE_ERROR = "type_error"
    CIRCULAR_DEPENDENCY = "circular_dependency"
    NODE_NOT_FOUND = "node_not_found"
    PARAM_NOT_FOUND = "param_not_found"


@dataclass
class ExpressionError:
    """Detailed error information for expression validation."""
    error_type: ExpressionErrorType
    message: str
    position: Optional[int] = None  # Character position in expression string
    context: Optional[str] = None   # Surrounding text for error highlight


@dataclass
class ExpressionDependency:
    """Tracks dependencies extracted from an expression.

    Used for dependency graph construction and change propagation.
    """
    node_id: Optional[str] = None  # None means "this node"
    param_name: str = ""
    is_input_attr: bool = False    # True if referencing input.width/height
    is_layer_attr: bool = False    # True if referencing layer.inPoint, etc.
    is_frame: bool = False         # True if referencing frame variable


@dataclass
class ExpressionResult:
    """Result of expression evaluation or validation."""
    success: bool
    value: Optional[float] = None
    error: Optional[ExpressionError] = None
    dependencies: List[ExpressionDependency] = field(default_factory=list)


MAX_EXPRESSION_LENGTH = 4096
MAX_AST_NODES = 256


def _linear(t: float, t_min: float, t_max: float,
            value_min: float, value_max: float) -> float:
    if t_max == t_min:
        return float(value_min)
    u = (float(t) - float(t_min)) / (float(t_max) - float(t_min))
    return float(value_min) + (float(value_max) - float(value_min)) * u


def _ease(t: float, t_min: float, t_max: float,
          value_min: float, value_max: float) -> float:
    if t_max == t_min:
        return float(value_min)
    u = max(0.0, min(1.0, (float(t) - float(t_min)) /
                      (float(t_max) - float(t_min))))
    u = u * u * (3.0 - 2.0 * u)
    return float(value_min) + (float(value_max) - float(value_min)) * u


def _ease_in(t: float, t_min: float, t_max: float,
             value_min: float, value_max: float) -> float:
    if t_max == t_min:
        return float(value_min)
    u = max(0.0, min(1.0, (float(t) - float(t_min)) /
                      (float(t_max) - float(t_min))))
    return float(value_min) + (float(value_max) - float(value_min)) * u * u


def _ease_out(t: float, t_min: float, t_max: float,
              value_min: float, value_max: float) -> float:
    if t_max == t_min:
        return float(value_min)
    u = max(0.0, min(1.0, (float(t) - float(t_min)) /
                      (float(t_max) - float(t_min))))
    u = 1.0 - (1.0 - u) * (1.0 - u)
    return float(value_min) + (float(value_max) - float(value_min)) * u


def _ease_in_out(t: float, t_min: float, t_max: float,
                 value_min: float, value_max: float) -> float:
    return _ease(t, t_min, t_max, value_min, value_max)


# Whitelisted math functions (safe to use in expressions)
SAFE_MATH_FUNCTIONS = {
    'sin': math.sin,
    'cos': math.cos,
    'tan': math.tan,
    'asin': math.asin,
    'acos': math.acos,
    'atan': math.atan,
    'atan2': math.atan2,
    'sqrt': math.sqrt,
    'abs': abs,
    'floor': math.floor,
    'ceil': math.ceil,
    'round': round,
    'min': min,
    'max': max,
    'log': math.log,
    'log10': math.log10,
    'log2': math.log2,
    'exp': math.exp,
    'pow': pow,
    'sign': lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0),
    'clamp': lambda x, lo, hi: max(lo, min(hi, x)),
    'lerp': lambda a, b, t: a + (b - a) * t,
    'linear': _linear,
    'ease': _ease,
    'easeIn': _ease_in,
    'easeOut': _ease_out,
    'easeInOut': _ease_in_out,
    'step': lambda edge, x: 1.0 if x >= edge else 0.0,
    'smoothstep': lambda edge0, edge1, x: (
        # Hermite smooth interpolation
        0.0 if x <= edge0 else (
            1.0 if x >= edge1 else
            ((x - edge0) / (edge1 - edge0)) ** 2 * (3 - 2 * ((x - edge0) / (edge1 - edge0)))
        )
    ),
    'mod': lambda x, y: x % y,
    'fract': lambda x: x - math.floor(x),
    'radians': lambda deg: deg * math.pi / 180.0,
    'degrees': lambda rad: rad * 180.0 / math.pi,
    'pi': math.pi,
    'e': math.e,
}

RUNTIME_FUNCTION_NAMES = {
    'valueAtTime', 'wiggle', 'loopIn', 'loopOut', 'random',
    'seedRandom', 'noise',
}


def _numeric_truth(value) -> bool:
    """N-12：统一数值真值规则——nonzero 为真（负数也是真）。

    Unary not 一直用 Python bool 语义（非零即真），而 BoolOp/IfExp 旧
    实现用 ``v > 0`` 判真，负数被判假；三处必须一致。返回值合同保持
    0/1 数值不变。
    """
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return bool(value)

# Whitelisted operators (automatically handled by ast)
SAFE_OPERATORS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b if b != 0 else float('inf'),
    ast.FloorDiv: lambda a, b: a // b if b != 0 else float('inf'),
    ast.Mod: lambda a, b: a % b if b != 0 else 0,
    ast.Pow: lambda a, b: pow(a, b),
    ast.USub: lambda a: -a,  # Unary minus
    ast.UAdd: lambda a: +a,  # Unary plus
    ast.Eq: lambda a, b: 1.0 if a == b else 0.0,
    ast.NotEq: lambda a, b: 1.0 if a != b else 0.0,
    ast.Lt: lambda a, b: 1.0 if a < b else 0.0,
    ast.LtE: lambda a, b: 1.0 if a <= b else 0.0,
    ast.Gt: lambda a, b: 1.0 if a > b else 0.0,
    ast.GtE: lambda a, b: 1.0 if a >= b else 0.0,
    ast.And: lambda a, b: 1.0 if (a and b) else 0.0,
    ast.Or: lambda a, b: 1.0 if (a or b) else 0.0,
    ast.Not: lambda a: 0.0 if a else 1.0,
}


class ExpressionParser:
    """Safe mathematical expression parser and evaluator.

    Usage:
        parser = ExpressionParser()

        # Validate expression syntax
        result = parser.validate("this.width * 0.5 + Blur1.radius")
        if not result.success:
            print(result.error.message)

        # Get dependencies
        deps = parser.extract_dependencies("Blur1.radius + input.width")
        # deps = [ExpressionDependency(node_id="Blur1", param_name="radius"),
        #         ExpressionDependency(is_input_attr=True, param_name="width")]

        # Evaluate expression with context
        context = ExpressionContext(
            this_params={"width": 1920},
            node_params={"Blur1": {"radius": 10.0}},
            input_attrs={"width": 1920, "height": 1080},
            frame=0
        )
        result = parser.evaluate("this.width * 0.5 + Blur1.radius", context)
        print(result.value)  # 1920 * 0.5 + 10 = 970
    """

    def __init__(self):
        self._math_functions = SAFE_MATH_FUNCTIONS.copy()
        self._operators = SAFE_OPERATORS.copy()
        self._known_function_names = set(self._math_functions) | RUNTIME_FUNCTION_NAMES
        # P-2: cache parsed ASTs keyed by expression string. The same
        # expression is parsed repeatedly during playback (every frame ×
        # every expression) even though the string never changes — the AST
        # only depends on the string. A bounded LRU keeps memory in check.
        self._ast_cache: dict = {}
        self._AST_CACHE_MAX = 256
        # AUD-11511: the eviction (pop(next(iter(...)))) and the insert can
        # be interrupted by a concurrent insert from another thread, raising
        # "dictionary changed size during iteration". Guard the mutation.
        self._ast_cache_lock = threading.Lock()

    def _parse_cached(self, expression: str):
        """P-2: parse ``expression`` once and cache the AST. Returns the
        ``ast.Expression`` node, or raises ``SyntaxError`` (so callers'
        existing try/except still work). Subsequent calls with the same
        string skip ``ast.parse`` entirely — the hot path during playback."""
        cached = self._ast_cache.get(expression)
        if cached is not None:
            return cached
        tree = ast.parse(expression, mode='eval')
        with self._ast_cache_lock:
            if len(self._ast_cache) >= self._AST_CACHE_MAX:
                # Drop one arbitrary entry (dict preserves insertion order; the
                # oldest is the next eviction candidate — a poor man's LRU).
                self._ast_cache.pop(next(iter(self._ast_cache)))
            self._ast_cache[expression] = tree
        return tree

    def validate(self, expression: str, context: Optional['ExpressionContext'] = None) -> ExpressionResult:
        """Validate expression syntax without evaluating.

        Returns ExpressionResult with:
        - success: True if syntax is valid
        - error: Detailed error info if invalid
        - dependencies: List of referenced variables/nodes
        """
        if not expression or not expression.strip():
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.SYNTAX_ERROR,
                    message="Expression is empty"
                )
            )

        if len(expression) > MAX_EXPRESSION_LENGTH:
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.SYNTAX_ERROR,
                    message=f"Expression is too long (maximum {MAX_EXPRESSION_LENGTH} characters)"
                )
            )

        try:
            tree = self._parse_cached(expression)

            if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
                return ExpressionResult(
                    success=False,
                    error=ExpressionError(
                        error_type=ExpressionErrorType.SYNTAX_ERROR,
                        message=f"Expression is too complex (maximum {MAX_AST_NODES} AST nodes)"
                    )
                )

            # Check for forbidden nodes
            forbidden = self._check_forbidden_nodes(tree)
            if forbidden:
                return ExpressionResult(
                    success=False,
                    error=ExpressionError(
                        error_type=ExpressionErrorType.SYNTAX_ERROR,
                        message=f"Forbidden operation: {forbidden}"
                    )
                )

            # Extract dependencies
            dependencies = self._extract_dependencies(tree)

            return ExpressionResult(
                success=True,
                dependencies=dependencies
            )

        except SyntaxError as e:
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.SYNTAX_ERROR,
                    message=f"Syntax error: {e.msg}",
                    position=e.offset,
                    context=expression[max(0, e.offset - 10):e.offset + 10] if e.offset else None
                )
            )

    def extract_dependencies(self, expression: str) -> List[ExpressionDependency]:
        """Extract all dependencies referenced in an expression.

        Returns list of ExpressionDependency objects for:
        - this.xxx: current node's parameters
        - NodeName.param: other node's parameters
        - input.xxx: input attributes
        - frame: frame number
        """
        try:
            tree = self._parse_cached(expression)
            return self._extract_dependencies(tree)
        except SyntaxError:
            return []

    def extract_local_parameter_names(self, expression: str) -> List[str]:
        """Return parameters referenced through the ``this`` namespace.

        ``extract_dependencies`` intentionally omits local parameters because
        they do not create graph edges.  Static graph diagnostics still need
        to distinguish ``this.radius`` from a typo and to detect the direct
        self-cycle ``this.radius + 1`` on the ``radius`` expression.
        """
        try:
            tree = self._parse_cached(expression)
        except SyntaxError:
            return []

        names: List[str] = []
        seen: Set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if self._get_attribute_root(node) != "this":
                continue
            if node.attr in {"width", "height"} or node.attr in seen:
                continue
            seen.add(node.attr)
            names.append(node.attr)
        return names

    def evaluate(self, expression: str, context: 'ExpressionContext') -> ExpressionResult:
        """Evaluate expression with given context.

        Args:
            expression: The expression string to evaluate
            context: ExpressionContext providing variable values

        Returns:
            ExpressionResult with:
            - success: True if evaluation succeeded
            - value: The computed float value
            - error: Error details if failed
        """
        if not expression or not expression.strip():
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.SYNTAX_ERROR,
                    message="Expression is empty"
                )
            )

        if len(expression) > MAX_EXPRESSION_LENGTH:
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.SYNTAX_ERROR,
                    message=f"Expression is too long (maximum {MAX_EXPRESSION_LENGTH} characters)"
                )
            )

        try:
            tree = self._parse_cached(expression)
            if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
                return ExpressionResult(
                    success=False,
                    error=ExpressionError(
                        error_type=ExpressionErrorType.SYNTAX_ERROR,
                        message=f"Expression is too complex (maximum {MAX_AST_NODES} AST nodes)"
                    )
                )
            value = self._eval_node(tree.body, context)

            # Ensure result is numeric
            if isinstance(value, bool):
                value = 1.0 if value else 0.0
            elif not isinstance(value, (int, float)):
                value = float(value)

            return ExpressionResult(
                success=True,
                value=float(value)
            )

        except SyntaxError as e:
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.SYNTAX_ERROR,
                    message=f"Syntax error: {e.msg}",
                    position=e.offset
                )
            )
        except ZeroDivisionError:
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.DIVISION_BY_ZERO,
                    message="Division by zero"
                )
            )
        except TypeError as e:
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.TYPE_ERROR,
                    message=f"Type error: {str(e)}"
                )
            )
        except KeyError as e:
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.UNDEFINED_VARIABLE,
                    message=f"Undefined variable: {str(e)}"
                )
            )
        except ValueError as e:
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.INVALID_FUNCTION,
                    message=f"Invalid function call: {str(e)}"
                )
            )
        except OverflowError as e:
            return ExpressionResult(
                success=False,
                error=ExpressionError(
                    error_type=ExpressionErrorType.TYPE_ERROR,
                    message=f"Expression overflow: {str(e)}"
                )
            )

    def _eval_node(self, node, context: 'ExpressionContext') -> Any:
        """Recursively evaluate an AST node."""

        # Literals
        if isinstance(node, ast.Constant):
            return node.value

        # Variables and attributes (this.xxx, NodeName.param, input.xxx, frame)
        if isinstance(node, ast.Name):
            return self._eval_variable(node.id, context)

        if isinstance(node, ast.Attribute):
            return self._eval_attribute(node, context)

        # Subscript access (array[index]) - not supported for safety
        if isinstance(node, ast.Subscript):
            raise ValueError("Array subscript access is not supported in expressions")

        # Binary operations (+, -, *, /, etc.)
        if isinstance(node, ast.BinOp):
            left = self._eval_node(node.left, context)
            right = self._eval_node(node.right, context)

            op_type = type(node.op)
            if op_type in self._operators:
                return self._operators[op_type](left, right)
            else:
                raise ValueError(f"Unsupported operator: {op_type.__name__}")

        # Unary operations (-x, +x, not x)
        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand, context)
            op_type = type(node.op)
            if op_type in self._operators:
                return self._operators[op_type](operand)
            else:
                raise ValueError(f"Unsupported unary operator: {op_type.__name__}")

        # Comparisons (==, !=, <, <=, >, >=)
        if isinstance(node, ast.Compare):
            left = self._eval_node(node.left, context)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval_node(comparator, context)
                op_type = type(op)
                if op_type in self._operators:
                    cmp_result = self._operators[op_type](left, right)
                    # N-12：链式比较短路——已判假后不再求值后续比较子。
                    if not (cmp_result > 0):
                        return 0.0
                    left = right
                else:
                    raise ValueError(f"Unsupported comparison: {op_type.__name__}")
            return 1.0

        # Boolean operations (and, or)
        if isinstance(node, ast.BoolOp):
            # N-12：逐项短路 + 统一 nonzero 真值。旧实现先求全部
            # operands 再用 v > 0 判真——负数被判假，且 "1 or sqrt(-1)"
            # 这类不可达右支会被执行并报 domain error，条件保护表达式
            # （如 1/x 的除零保护）因此失效。
            is_and = isinstance(node.op, ast.And)
            if not is_and and not isinstance(node.op, ast.Or):
                raise ValueError("Unsupported boolean operator")
            for value_node in node.values:
                value = self._eval_node(value_node, context)
                truthy = _numeric_truth(value)
                if is_and and not truthy:
                    return 0.0
                if not is_and and truthy:
                    return 1.0
            return 1.0 if is_and else 0.0

        # Conditional expression (x if condition else y) - ternary
        if isinstance(node, ast.IfExp):
            condition = self._eval_node(node.test, context)
            # N-12：负数条件也是真（与 BoolOp/Unary not 同一规则）。
            if _numeric_truth(condition):
                return self._eval_node(node.body, context)
            else:
                return self._eval_node(node.orelse, context)

        # Function calls (sin, cos, sqrt, etc.)
        if isinstance(node, ast.Call):
            func_name = self._get_function_name(node.func)
            functions = self._function_map(context)
            if func_name not in functions:
                raise ValueError(f"Unsupported function: {func_name}")

            args = [self._eval_node(arg, context) for arg in node.args]
            kwargs = {}
            for keyword in node.keywords:
                if keyword.arg is None:
                    raise ValueError("Expanded keyword arguments are not supported")
                kwargs[keyword.arg] = self._eval_node(keyword.value, context)
            func = functions[func_name]

            try:
                return func(*args, **kwargs)
            except (TypeError, ValueError) as e:
                raise ValueError(f"Function '{func_name}' error: {str(e)}")

        # Parenthesized expression (just a wrapper)
        if isinstance(node, ast.Expression):
            return self._eval_node(node.body, context)

        # Unsupported node type
        raise ValueError(f"Unsupported expression element: {type(node).__name__}")

    def _eval_variable(self, name: str, context: 'ExpressionContext') -> float:
        """Evaluate a simple variable reference (frame, pi, e)."""
        # Check for frame variable
        if name == 'frame':
            return float(context.frame)
        if name == 'time':
            return float(context.time)
        if name == 'fps':
            return float(context.fps)
        if name == 'value':
            return float(context.current_value)
        if name == 'width':
            return float(context.comp_width)
        if name == 'height':
            return float(context.comp_height)

        # Check for math constants
        if name == 'pi':
            return math.pi
        if name == 'e':
            return math.e

        # Check if it's a node reference (standalone node name)
        # This handles expressions like "Blur1" referring to a node's primary output value
        if name in context.node_params:
            # If node name alone, try to get a default parameter
            node_params = context.node_params[name]
            # Some nodes have a primary "value" parameter
            if 'value' in node_params:
                return float(node_params['value'])
            raise KeyError(f"Node '{name}' has no default 'value' parameter")

        # Check if it's a local parameter in this node (without this. prefix)
        # This allows shorthand: "radius" instead of "this.radius"
        if name in context.this_params:
            return float(context.this_params[name])

        raise KeyError(name)

    def _eval_attribute(self, node: ast.Attribute, context: 'ExpressionContext') -> float:
        """Evaluate node, input, layer, and composition attributes."""
        # Get the object being accessed
        obj_name = self._get_attribute_root(node)

        # Get the attribute name
        attr_name = node.attr

        # Handle this.xxx (current node's parameters)
        if obj_name == 'this':
            if attr_name in context.this_params:
                return float(context.this_params[attr_name])
            # Also allow accessing this.width, this.height (canvas dimensions)
            if attr_name in ('width', 'height'):
                return float(context.input_attrs.get(attr_name, 0))
            raise KeyError(f"this.{attr_name}")

        # Handle input.xxx (input texture attributes)
        if obj_name in ('input', 'comp', 'thisComp'):
            if attr_name in context.input_attrs:
                return float(context.input_attrs[attr_name])
            if attr_name == 'width':
                return float(context.comp_width)
            if attr_name == 'height':
                return float(context.comp_height)
            raise KeyError(f"input.{attr_name}")

        # Handle layer.inPoint / thisLayer.opacity.  Layer metadata is copied
        # into the immutable evaluation context by the graph host.
        if obj_name in ('layer', 'thisLayer'):
            if attr_name in context.layer_attrs:
                return float(context.layer_attrs[attr_name])
            raise KeyError(f"layer.{attr_name}")

        # Handle NodeName.param (other node's parameters)
        if obj_name in context.node_params:
            node_params = context.node_params[obj_name]
            if attr_name in node_params:
                return float(node_params[attr_name])
            raise KeyError(f"{obj_name}.{attr_name}")

        # Handle nested attributes (e.g., input1.width)
        # Try to interpret obj_name as a node name
        if obj_name not in ('this', 'input', 'comp', 'thisComp'):
            if obj_name in context.node_params:
                node_params = context.node_params[obj_name]
                if attr_name in node_params:
                    return float(node_params[attr_name])
                raise KeyError(f"{obj_name}.{attr_name}")

        raise KeyError(f"{obj_name}.{attr_name}")

    def _get_attribute_root(self, node: ast.Attribute) -> str:
        """Get the root object name from an attribute chain.

        Handles: this.xxx -> "this"
                 NodeName.param -> "NodeName"
                 input.xxx -> "input"
        """
        if isinstance(node.value, ast.Name):
            return node.value.id
        elif isinstance(node.value, ast.Attribute):
            # Nested attribute - recurse
            return self._get_attribute_root(node.value)
        return ""

    def _get_function_name(self, node) -> str:
        """Get function name from a Call node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            # Attribute-based function call (e.g., math.sin)
            # We only allow simple function names for safety
            return node.attr
        return ""

    def _function_map(self, context: 'ExpressionContext') -> Dict[str, Callable]:
        """Return static plus context-bound AE-style functions."""
        functions = self._math_functions.copy()

        def value_at_time(sample_time: float) -> float:
            if context.value_at_time is None:
                return float(context.current_value)
            return float(context.value_at_time(float(sample_time)))

        def wiggle(freq: float, amp: float, octaves: int = 1,
                   amp_mult: float = 0.5) -> float:
            octaves_i = max(1, min(8, int(octaves)))
            result = 0.0
            for index in range(octaves_i):
                phase = (context.random_seed + index * 7919) * \
                        0.017453292519943295
                result += math.sin((context.time * float(freq) *
                                    (2.0 ** index) * math.tau) + phase) * \
                          float(amp) * (float(amp_mult) ** index)
            return float(context.current_value) + result

        def deterministic_random(min_value: float = 0.0,
                                 max_value: float = 1.0) -> float:
            key = f"{context.random_seed}:{context.time:.9f}:{context.frame}"
            digest = hashlib.sha256(key.encode("utf-8")).digest()
            unit = int.from_bytes(digest[:8], "big") / float(2 ** 64 - 1)
            return float(min_value) + (float(max_value) - float(min_value)) * unit

        def seed_random(seed: float = 0.0, timeless: bool = False) -> float:
            key = f"{seed}:{0.0 if timeless else context.time:.9f}"
            digest = hashlib.sha256(key.encode("utf-8")).digest()
            return int.from_bytes(digest[:8], "big") / float(2 ** 64 - 1)

        def value_noise(value: float) -> float:
            key = f"noise:{context.random_seed}:{float(value):.9f}"
            digest = hashlib.sha256(key.encode("utf-8")).digest()
            return (int.from_bytes(digest[:8], "big") /
                    float(2 ** 64 - 1)) * 2.0 - 1.0

        functions.update({
            'valueAtTime': value_at_time,
            'wiggle': wiggle,
            'random': deterministic_random,
            'seedRandom': seed_random,
            'noise': value_noise,
        })
        if context.loop_in is not None:
            functions['loopIn'] = context.loop_in
        if context.loop_out is not None:
            functions['loopOut'] = context.loop_out
        return functions

    def _check_forbidden_nodes(self, tree) -> Optional[str]:
        """Check for forbidden AST nodes that could execute code."""
        FORBIDDEN = {
            ast.Import: "import statement",
            ast.ImportFrom: "import statement",
            ast.Lambda: "lambda expression",
            ast.ListComp: "list comprehension",
            ast.SetComp: "set comprehension",
            ast.DictComp: "dict comprehension",
            ast.GeneratorExp: "generator expression",
            ast.Yield: "yield expression",
            ast.YieldFrom: "yield from expression",
            ast.Await: "await expression",
            ast.Call: None,  # Checked separately for function whitelist
            ast.UAdd: "unary plus",
            ast.Subscript: "array subscript",
            ast.Starred: "starred expression",
            ast.NamedExpr: "named expression (walrus operator)",
        }

        for node in ast.walk(tree):
            node_type = type(node)
            if node_type in FORBIDDEN:
                forbidden_name = FORBIDDEN[node_type]
                if forbidden_name:
                    return forbidden_name

                # Special handling for Call nodes - check function whitelist
                if isinstance(node, ast.Call):
                    func_name = self._get_function_name(node.func)
                    if func_name not in self._known_function_names:
                        return f"function '{func_name}'"

        return None

    def _extract_dependencies(self, tree) -> List[ExpressionDependency]:
        """Extract all dependencies from an AST tree."""
        dependencies: List[ExpressionDependency] = []
        seen: Set[Tuple] = set()  # Avoid duplicates

        # Names that should not be treated as dependencies
        # - Math functions (sin, cos, sqrt, etc.)
        # - Math constants (pi, e)
        # - Special keywords (this, input, frame)
        IGNORED_NAMES = set(self._math_functions.keys()) | {
            'pi', 'e', 'this', 'input', 'comp', 'thisComp', 'frame',
            'time', 'fps', 'value', 'width', 'height',
        }

        # Also collect function names used in Call nodes to exclude them
        call_func_names: Set[str] = set()
        attribute_root_names: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_function_name(node.func)
                if func_name:
                    call_func_names.add(func_name)
            elif isinstance(node, ast.Attribute):
                root_name = self._get_attribute_root(node)
                if root_name:
                    attribute_root_names.add(root_name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                name = node.id
                # frame is a special dependency
                if name == 'frame':
                    dep = ExpressionDependency(is_frame=True, param_name='frame')
                    key = (None, 'frame', False, True)
                    if key not in seen:
                        seen.add(key)
                        dependencies.append(dep)
                # Skip ignored names (functions, constants, keywords)
                elif name in IGNORED_NAMES or name in call_func_names or name in attribute_root_names:
                    continue
                # Node name reference (Blur1) - only if it's not a function call
                else:
                    dep = ExpressionDependency(node_id=name, param_name='value')
                    key = (name, 'value', False, False)
                    if key not in seen:
                        seen.add(key)
                        dependencies.append(dep)

            elif isinstance(node, ast.Attribute):
                obj_name = self._get_attribute_root(node)
                attr_name = node.attr

                # this.xxx - current node's parameters (not an external dependency)
                if obj_name == 'this':
                    # this.width/height are input attributes
                    if attr_name in ('width', 'height'):
                        dep = ExpressionDependency(
                            is_input_attr=True,
                            param_name=attr_name
                        )
                        key = (None, attr_name, True, False)
                        if key not in seen:
                            seen.add(key)
                            dependencies.append(dep)
                    # Other this.xxx are local params, not external deps
                    # (we don't track them as external dependencies)

                # input.xxx - input texture attributes
                elif obj_name in ('input', 'comp', 'thisComp'):
                    dep = ExpressionDependency(
                        is_input_attr=True,
                        param_name=attr_name
                    )
                    key = (None, attr_name, True, False)
                    if key not in seen:
                        seen.add(key)
                        dependencies.append(dep)

                # AE-style layer metadata is supplied by the host for the
                # current node.  It is contextual rather than a graph edge.
                elif obj_name in ('layer', 'thisLayer'):
                    dep = ExpressionDependency(
                        is_layer_attr=True,
                        param_name=attr_name)
                    key = (None, attr_name, True, False, True)
                    if key not in seen:
                        seen.add(key)
                        dependencies.append(dep)

                # NodeName.param - other node's parameters
                elif obj_name not in ('this', 'input'):
                    dep = ExpressionDependency(
                        node_id=obj_name,
                        param_name=attr_name
                    )
                    key = (obj_name, attr_name, False, False)
                    if key not in seen:
                        seen.add(key)
                        dependencies.append(dep)

        return dependencies


@dataclass
class ExpressionContext:
    """Context for expression evaluation.

    Provides values for all variable types referenced in expressions.
    """
    # Current node's parameters (accessed via this.xxx)
    this_params: Dict[str, Any] = field(default_factory=dict)

    # Other nodes' parameters (accessed via NodeName.param)
    # Maps node_id/name -> {param_name -> value}
    node_params: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Input texture attributes (accessed via input.width, input.height)
    input_attrs: Dict[str, int] = field(default_factory=dict)

    # Current layer metadata (accessed via layer.inPoint / thisLayer.opacity).
    # Values are numeric/bool and intentionally copied into each evaluation
    # context so expressions cannot mutate the live Layer model.
    layer_attrs: Dict[str, Any] = field(default_factory=dict)

    # Current frame number (accessed via frame)
    frame: int = 0

    # AE-style timeline context.
    time: float = 0.0
    fps: float = 24.0
    current_value: float = 0.0
    value_at_time: Optional[Callable[[float], Any]] = None
    loop_in: Optional[Callable[..., Any]] = None
    loop_out: Optional[Callable[..., Any]] = None
    random_seed: int = 0

    # Global project settings (optional)
    project_width: int = 1920
    project_height: int = 1080

    @property
    def comp_width(self) -> int:
        return self.project_width

    @property
    def comp_height(self) -> int:
        return self.project_height


class ExpressionManager:
    """Manages expressions across the node graph.

    Responsibilities:
    - Track expressions for all node parameters
    - Build dependency graph
    - Detect circular dependencies
    - Propagate changes through dependency chain
    - Provide batch evaluation for efficiency
    """

    def __init__(self):
        self._parser = ExpressionParser()
        # AUD-11511: node_engine's edit path (set_expression from the UI
        # thread) and evaluation path (evaluate_all from the render thread)
        # share one manager. Without a lock, ``dict changed size during
        # iteration`` can escape from the render path and abort a whole
        # frame. RLock: internal helpers (remove_node → remove_expression,
        # evaluate_all → _compute_eval_order) re-enter on the same thread.
        self._lock = threading.RLock()
        # Map: (node_id, param_name) -> expression_string
        self._expressions: Dict[Tuple[str, str], str] = {}
        # Dependency graph: (node_id, param_name) -> set of (node_id, param_name)
        self._dependencies: Dict[Tuple[str, str], Set[Tuple[str, str]]] = {}
        # Reverse dependency graph: (node_id, param_name) -> set of dependents
        self._dependents: Dict[Tuple[str, str], Set[Tuple[str, str]]] = {}
        # Errors from the most recent batch evaluation.  Keep this separate
        # from ``ExpressionResult`` so the long-standing ``evaluate_all``
        # return type stays backwards compatible while render snapshots can
        # expose per-parameter runtime failures.
        self._last_errors: Dict[Tuple[str, str], ExpressionError] = {}
        self._last_cyclic_keys: Set[Tuple[str, str]] = set()

    def set_expression(self, node_id: str, param_name: str,
                       expression: str,
                       local_param_names=None,
                       resolve_node_ref=None) -> ExpressionResult:
        """Set an expression for a parameter.

        Validates the expression and updates dependency tracking.

        Args:
            node_id: Node owning the expression.
            param_name: Parameter the expression is attached to.
            expression: Expression source text.
            local_param_names: 当前节点可用的参数名集合（N-02）。提供时，
                ``this.foo``（及经由解析器的本地引用）会注册为
                (node_id, foo) 图边，参与拓扑排序与环检测；不提供时保持
                旧行为（本地引用不入图）。
            resolve_node_ref: 节点引用 canonical 化回调（N-03）：
                ref -> 真实 node_id 或 None。ID、``n_<id>`` 别名与可用
                显示名统一映射到真实 ID，使提交校验/快照求值/preflight
                共享同一依赖图。

        Returns:
            ExpressionResult with validation status and extracted dependencies.
        """
        key = (node_id, param_name)

        # Validate expression (parsing happens outside the lock)
        result = self._parser.validate(expression)
        if not result.success:
            return result

        new_deps: Set[Tuple[str, str]] = set()
        for dep in result.dependencies:
            if dep.node_id:
                # External node reference
                target_id = dep.node_id
                if resolve_node_ref is not None:
                    resolved = resolve_node_ref(dep.node_id)
                    if resolved is not None and resolved != target_id:
                        # N-03：canonical 边用于提交校验/环检测；raw 引用
                        # 边保留给快照求值——别名键（n_<id>）在快照
                        # manager 中注册为独立表达式并承担值发布，依赖
                        # 方读别名参数时仍需按别名边排序。
                        new_deps.add((target_id, dep.param_name))
                        target_id = resolved
                new_deps.add((target_id, dep.param_name))

        # N-02：this.xxx 本地引用注册为同节点图边（width/height 画布别名
        # 除外），使 x=this.y*2 的求值顺序不再依赖表达式插入顺序，且
        # this 自环在提交时即被拒绝。引用了本节点不存在的参数时仍加边
        # （悬空键不参与入度），诊断由 preflight/求值层报告。
        if local_param_names is not None:
            for local_name in self._parser.extract_local_parameter_names(
                    expression):
                new_deps.add((node_id, local_name))

        with self._lock:
            # Store expression
            self._expressions[key] = expression

            # Update dependencies
            old_deps = self._dependencies.get(key, set())

            # Update dependency graph
            self._dependencies[key] = new_deps

            # Update reverse dependency graph
            # Remove old dependents
            for dep_key in old_deps:
                if dep_key in self._dependents:
                    self._dependents[dep_key].discard(key)

            # Add new dependents
            for dep_key in new_deps:
                if dep_key not in self._dependents:
                    self._dependents[dep_key] = set()
                self._dependents[dep_key].add(key)

        return result

    def remove_expression(self, node_id: str, param_name: str) -> None:
        """Remove expression from a parameter."""
        key = (node_id, param_name)

        with self._lock:
            # Clean up dependencies
            old_deps = self._dependencies.get(key, set())
            for dep_key in old_deps:
                if dep_key in self._dependents:
                    self._dependents[dep_key].discard(key)

            # Remove expression
            self._expressions.pop(key, None)
            self._dependencies.pop(key, None)
            self._last_errors.pop(key, None)

    def remove_node(self, node_id: str) -> None:
        """Remove expressions owned by ``node_id``.

        Dependencies *to* a removed node are intentionally retained.  A
        remaining expression should continue to report a missing dependency,
        and a node restored with the same ID can become a valid source again.
        """
        with self._lock:
            for expression_node, param_name in list(self._expressions):
                if expression_node == node_id:
                    self.remove_expression(expression_node, param_name)

    def clear(self) -> None:
        """Clear expressions, dependency edges, and evaluation diagnostics."""
        with self._lock:
            self._expressions.clear()
            self._dependencies.clear()
            self._dependents.clear()
            self._last_errors.clear()
            self._last_cyclic_keys.clear()

    @property
    def last_errors(self) -> Dict[Tuple[str, str], ExpressionError]:
        """Errors produced by the most recent :meth:`evaluate_all` call.

        A copy prevents callers from mutating the manager's diagnostics while
        another evaluation is in progress.
        """
        with self._lock:
            return dict(self._last_errors)

    def get_last_error(self, node_id: str,
                       param_name: str) -> Optional[ExpressionError]:
        """Return the most recent runtime error for one expression."""
        with self._lock:
            return self._last_errors.get((node_id, param_name))

    def has_expression(self, node_id: str, param_name: str) -> bool:
        """Check if a parameter has an expression."""
        with self._lock:
            return (node_id, param_name) in self._expressions

    def get_expression(self, node_id: str, param_name: str) -> Optional[str]:
        """Get expression string for a parameter."""
        with self._lock:
            return self._expressions.get((node_id, param_name))

    def get_dependencies(self, node_id: str, param_name: str) -> Set[Tuple[str, str]]:
        """Get dependencies for a parameter expression."""
        with self._lock:
            return set(self._dependencies.get((node_id, param_name), set()))

    def get_dependents(self, node_id: str, param_name: str) -> Set[Tuple[str, str]]:
        """Get all parameters that depend on this one."""
        with self._lock:
            return set(self._dependents.get((node_id, param_name), set()))

    def detect_circular_dependency(self, node_id: str, param_name: str) -> bool:
        """Detect if setting an expression would create a circular dependency.

        Returns True if a cycle would be created.
        """
        key = (node_id, param_name)

        with self._lock:
            # DFS to check if key is reachable from itself through dependencies
            visited: Set[Tuple[str, str]] = set()
            stack: List[Tuple[str, str]] = [key]

            while stack:
                current = stack.pop()
                if current == key and len(visited) > 0:
                    return True  # Cycle detected

                if current in visited:
                    continue
                visited.add(current)

                # Add all dependencies of current
                for dep_key in self._dependencies.get(current, set()):
                    if dep_key not in visited:
                        stack.append(dep_key)

        return False

    def _build_context(
        self,
        key: Tuple[str, str],
        result: Dict[str, Dict[str, Any]],
        input_attrs: Dict[str, int],
        frame: int,
        fps: float,
        project_width: int,
        project_height: int,
        value_at_time: Optional[Callable[[str, str, float], Any]],
        loop_in: Optional[Callable[[str, str, str, int, int], Any]],
        loop_out: Optional[Callable[[str, str, str, int, int], Any]],
        layer_attrs: Optional[Dict[str, Dict[str, Any]]],
    ) -> ExpressionContext:
        """Build the full AE-style evaluation context for one expression.

        Shared by :meth:`evaluate_all` and :meth:`propagate_change` so both
        paths see identical ``time``/``fps``/``layer_attrs``/… values
        (AUD-11510).
        """
        node_id, param_name = key
        return ExpressionContext(
            this_params=result[node_id],
            node_params=result,
            input_attrs=input_attrs,
            layer_attrs=(layer_attrs.get(node_id)
                         or layer_attrs.get(node_id[2:], {})
                         if layer_attrs is not None else {}),
            frame=frame,
            time=float(frame) / float(fps or 24.0),
            fps=float(fps or 24.0),
            current_value=result[node_id].get(param_name, 0.0),
            value_at_time=(
                (lambda sample_time, nid=node_id, pname=param_name:
                 value_at_time(nid, pname, sample_time))
                if value_at_time is not None else None
            ),
            loop_in=(
                (lambda mode="cycle", count=0, nid=node_id, pname=param_name:
                 loop_in(nid, pname, str(mode), int(count), frame))
                if loop_in is not None else None
            ),
            loop_out=(
                (lambda mode="cycle", count=0, nid=node_id, pname=param_name:
                 loop_out(nid, pname, str(mode), int(count), frame))
                if loop_out is not None else None
            ),
            project_width=project_width,
            project_height=project_height,
            random_seed=int.from_bytes(
                hashlib.sha256(f"{node_id}:{param_name}".encode("utf-8")).digest()[:4],
                "big",
            ),
        )

    def _evaluate_into_result(
        self,
        key: Tuple[str, str],
        result: Dict[str, Dict[str, Any]],
        context: ExpressionContext,
    ) -> None:
        """Evaluate one expression, storing finite values and recording errors.

        Non-finite results are rejected (never written into parameters) and
        reported through ``_last_errors`` — the same contract
        :meth:`evaluate_all` has always applied (AUD-11510).
        """
        node_id, param_name = key
        expression = self._expressions.get(key)
        if not expression:
            return
        eval_result = self._parser.evaluate(expression, context)
        if eval_result.success and eval_result.value is not None:
            value = eval_result.value
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                result[node_id][param_name] = value
            else:
                self._last_errors[key] = ExpressionError(
                    error_type=ExpressionErrorType.TYPE_ERROR,
                    message="Expression produced a non-finite value",
                )
        elif eval_result.error is not None:
            self._last_errors[key] = eval_result.error

    def evaluate_all(self, node_params: Dict[str, Dict[str, Any]],
                     input_attrs: Dict[str, int],
                     frame: int,
                     fps: float = 24.0,
                     project_width: int = 1920,
                     project_height: int = 1080,
                     value_at_time: Optional[Callable[[str, str, float], Any]] = None,
                     loop_in: Optional[Callable[[str, str, str, int, int], Any]] = None,
                     loop_out: Optional[Callable[[str, str, str, int, int], Any]] = None,
                     layer_attrs: Optional[Dict[str, Dict[str, Any]]] = None,
                     ) -> Dict[str, Dict[str, Any]]:
        """Evaluate all expressions in dependency order.

        Args:
            node_params: Current node parameters (node_id -> {param -> value})
            input_attrs: Input texture attributes
            frame: Current frame number
            fps: Composition frame rate.
            value_at_time: Optional callback ``(node_id, param, time)`` used by
                AE-style ``valueAtTime`` expressions.
            loop_in/loop_out: Optional callbacks for loop expressions.
            layer_attrs: Optional map of node ID to current layer metadata.

        Returns:
            Updated node_params with evaluated expressions.
        """
        result = {nid: dict(params) for nid, params in node_params.items()}

        with self._lock:
            self._last_errors.clear()
            self._last_cyclic_keys = set()

            # Build evaluation order (topological sort of dependencies)
            eval_order = self._compute_eval_order()

            # Evaluate in order
            for key in eval_order:
                node_id, param_name = key

                if key in self._last_cyclic_keys:
                    continue

                if node_id not in result:
                    continue

                context = self._build_context(
                    key, result, input_attrs, frame, fps,
                    project_width, project_height,
                    value_at_time, loop_in, loop_out, layer_attrs)
                self._evaluate_into_result(key, result, context)

            # A cycle is kept in the evaluation order for compatibility with the
            # old manager, but its results are not trustworthy.  Surface an error
            # for every unresolved expression instead of silently using stale
            # values.
            for key in self._last_cyclic_keys:
                if key in self._expressions:
                    self._last_errors[key] = ExpressionError(
                        error_type=ExpressionErrorType.CIRCULAR_DEPENDENCY,
                        message="Circular expression dependency",
                    )

        return result

    def _compute_eval_order(self) -> List[Tuple[str, str]]:
        """Compute topological order for expression evaluation.

        Ensures dependencies are evaluated before dependents.  Caller must
        hold ``self._lock``.
        """
        # Kahn's algorithm
        in_degree: Dict[Tuple[str, str], int] = {key: 0 for key in self._expressions}
        adjacency: Dict[Tuple[str, str], List[Tuple[str, str]]] = {key: [] for key in self._expressions}

        # Build graph
        for key, deps in self._dependencies.items():
            for dep_key in deps:
                if dep_key in self._expressions:
                    adjacency[dep_key].append(key)
                    in_degree[key] += 1

        # Start with nodes that have no dependencies
        queue = [key for key, deg in in_degree.items() if deg == 0]
        result = []

        while queue:
            key = queue.pop(0)
            result.append(key)
            for neighbor in adjacency[key]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # Handle remaining nodes (circular dependencies)
        remaining = [k for k in self._expressions if k not in result]
        self._last_cyclic_keys = set(remaining)
        if remaining:
            # Circular deps - just append them, they'll evaluate with stale values
            result.extend(remaining)

        return result

    def propagate_change(self, node_id: str, param_name: str,
                         node_params: Dict[str, Dict[str, Any]],
                         input_attrs: Dict[str, int],
                         frame: int,
                         fps: float = 24.0,
                         project_width: int = 1920,
                         project_height: int = 1080,
                         value_at_time: Optional[Callable[[str, str, float], Any]] = None,
                         loop_in: Optional[Callable[[str, str, str, int, int], Any]] = None,
                         loop_out: Optional[Callable[[str, str, str, int, int], Any]] = None,
                         layer_attrs: Optional[Dict[str, Dict[str, Any]]] = None,
                         ) -> Dict[str, Dict[str, Any]]:
        """Propagate a parameter change through all dependent expressions.

        Evaluation uses the exact same context construction as
        :meth:`evaluate_all` (time/fps/layer_attrs/random_seed/callbacks),
        so the same expression produces the same value on both paths
        (AUD-11510).  Non-finite results are filtered and runtime errors
        recorded in ``last_errors``.

        Args:
            node_id: Node that changed
            param_name: Parameter that changed
            node_params: Current node parameters
            input_attrs: Input texture attributes
            frame: Current frame number
            fps: Composition frame rate.
            value_at_time: Optional callback ``(node_id, param, time)`` used by
                AE-style ``valueAtTime`` expressions.
            loop_in/loop_out: Optional callbacks for loop expressions.
            layer_attrs: Optional map of node ID to current layer metadata.

        Returns:
            Updated node_params with re-evaluated dependent expressions.
        """
        result = {nid: dict(params) for nid, params in node_params.items()}
        key = (node_id, param_name)

        with self._lock:
            # Get all dependents (transitive closure)
            to_update: Set[Tuple[str, str]] = set()
            stack = [key]

            while stack:
                current = stack.pop()
                for dependent in self._dependents.get(current, set()):
                    if dependent not in to_update:
                        to_update.add(dependent)
                        stack.append(dependent)

            # Evaluate in dependency order
            eval_order = self._compute_eval_order()
            to_update_ordered = [k for k in eval_order if k in to_update]

            for update_key in to_update_ordered:
                update_node_id, _update_param_name = update_key

                if update_node_id not in result:
                    continue

                context = self._build_context(
                    update_key, result, input_attrs, frame, fps,
                    project_width, project_height,
                    value_at_time, loop_in, loop_out, layer_attrs)
                self._evaluate_into_result(update_key, result, context)

        return result


# Global expression manager instance
_expression_manager: Optional[ExpressionManager] = None


def get_expression_manager() -> ExpressionManager:
    """Get the global ExpressionManager instance."""
    global _expression_manager
    if _expression_manager is None:
        _expression_manager = ExpressionManager()
    return _expression_manager


def reset_expression_manager() -> None:
    """Reset the global ExpressionManager (for testing)."""
    global _expression_manager
    _expression_manager = None
