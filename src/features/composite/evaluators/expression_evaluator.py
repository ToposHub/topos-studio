"""
ExpressionEvaluatorMixin - Expression evaluation methods.

Provides expression-related methods for dynamic parameter computation:
- Expression parsing and evaluation
- Variable substitution

Note: The actual expression parsing is delegated to ExpressionParser
module. This mixin is kept minimal for future expression-related extensions.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class ExpressionEvaluatorMixin:
    """Mixin providing expression evaluation methods.

    Note: Expression parsing is handled by the ExpressionParser module
    in src/composite/expression_parser.py. This mixin is a placeholder
    for future expression-related functionality that may need direct
    evaluator integration.
    """

    # Expression evaluation is handled by ExpressionParser module
    # This mixin is a placeholder for future expression-related functionality
    pass