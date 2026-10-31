"""
KeyframeEvaluatorMixin - Keyframe interpolation methods.

Provides keyframe-related methods:
- Parameter interpolation for animated values
- KeyframeTrack evaluation

Note: The main interpolation logic is defined in BaseEvaluatorMixin
as part of _create_ephemeral_node and _interpolate_parameters.
This mixin is kept minimal for future keyframe-related extensions.
"""

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass


class KeyframeEvaluatorMixin:
    """Mixin providing keyframe interpolation methods.

    Note: The main interpolation method (_interpolate_parameters) is defined
    in BaseEvaluatorMixin as it's used during ephemeral node creation.
    This mixin is a placeholder for future keyframe-related functionality.
    """

    # Keyframe interpolation is handled by BaseEvaluatorMixin._interpolate_parameters
    # This mixin is a placeholder for future keyframe-related functionality
    pass