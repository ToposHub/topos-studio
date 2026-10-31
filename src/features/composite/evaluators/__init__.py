"""
GraphEvaluator Evaluator Mixins Package.

This package provides modular evaluator mixins for GraphEvaluatorMixin,
splitting the original 3,600+ line monolithic file into focused sub-modules.

Module organization:
- base_evaluator.py: Core evaluation logic, GPU helpers, dispatch
- blur_evaluator.py: Blur, defocus, sharpen effects
- color_evaluator.py: Grade, huecorrect, normalize, invert, premult/unpremult
- transform_evaluator.py: Transform, cornerpin, reformat, crop, cs_transform
- merge_evaluator.py: Blend, dissolve, deep_merge, set_alpha
- keyer_evaluator.py: Luma/chroma/difference keyer, matte
- channel_evaluator.py: Shuffle, copy_channel, remove_channels, channel_merge
- filter_evaluator.py: Erode/dilate, glow, edge_detect, noise, ramp, mask, denoise, lut3d
- cache_evaluator.py: Cache management methods
- tile_evaluator.py: Tile rendering and LOD methods
- histogram_evaluator.py: Histogram computation hooks
- keyframe_evaluator.py: Keyframe interpolation hooks
- expression_evaluator.py: Expression evaluation hooks
- ai_evaluator.py: AI node evaluation hooks

Usage:
    The main GraphEvaluatorMixin in
    src/features/composite/evaluators/graph_evaluator_mixin.py
    inherits from all these mixins to provide the complete evaluation API.
"""

from .base_evaluator import (
    BaseEvaluatorMixin,
    TileContext,
    _AI_NODE_TYPES,
)
from .blur_evaluator import BlurEvaluatorMixin
from .color_evaluator import ColorEvaluatorMixin
from .transform_evaluator import TransformEvaluatorMixin
from .merge_evaluator import MergeEvaluatorMixin
from .keyer_evaluator import KeyerEvaluatorMixin
from .channel_evaluator import ChannelEvaluatorMixin
from .filter_evaluator import FilterEvaluatorMixin
from .cache_evaluator import CacheEvaluatorMixin
from .tile_evaluator import TileEvaluatorMixin
from .histogram_evaluator import HistogramEvaluatorMixin
from .keyframe_evaluator import KeyframeEvaluatorMixin
from .expression_evaluator import ExpressionEvaluatorMixin
from .ai_evaluator import AIEvaluatorMixin

__all__ = [
    # Base
    'BaseEvaluatorMixin',
    'TileContext',
    '_AI_NODE_TYPES',
    # Effects
    'BlurEvaluatorMixin',
    'ColorEvaluatorMixin',
    'TransformEvaluatorMixin',
    'MergeEvaluatorMixin',
    'KeyerEvaluatorMixin',
    'ChannelEvaluatorMixin',
    'FilterEvaluatorMixin',
    # Utilities
    'CacheEvaluatorMixin',
    'TileEvaluatorMixin',
    'HistogramEvaluatorMixin',
    'KeyframeEvaluatorMixin',
    'ExpressionEvaluatorMixin',
    'AIEvaluatorMixin',
]