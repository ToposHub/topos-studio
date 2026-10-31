"""
HistogramEvaluatorMixin - Histogram computation methods.

Provides histogram-related methods for percentile-based normalization:
- Percentile range computation using GPU histogram compute shader

Note: The actual histogram computation is delegated to HistogramManager,
which uses GPU compute shaders for efficient calculation.
"""

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass


class HistogramEvaluatorMixin:
    """Mixin providing histogram computation methods.

    Note: The main percentile computation method (_compute_percentile_range)
    is defined in ColorEvaluatorMixin as it's closely tied to the normalize effect.
    This mixin is kept minimal for future histogram-related extensions.
    """

    # Histogram computation is handled by ColorEvaluatorMixin._compute_percentile_range
    # This mixin is a placeholder for future histogram-related functionality
    pass