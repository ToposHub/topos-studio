"""
GraphEvaluatorMixin - Combiner for all evaluator mixins.

This file acts as a composition layer, importing and combining
all the specialized evaluator mixins into a single unified class.

The original 3,600+ line monolithic file has been split into
focused sub-modules in src/composite/evaluators/:

Module organization:
- base_evaluator.py: Core evaluation logic, GPU helpers, dispatch (~700 lines)
- blur_evaluator.py: Blur, defocus, sharpen effects (~200 lines)
- color_evaluator.py: Grade, huecorrect, normalize, invert, premult (~400 lines)
- transform_evaluator.py: Transform, cornerpin, reformat, crop (~300 lines)
- merge_evaluator.py: Blend, dissolve, deep_merge, set_alpha (~300 lines)
- keyer_evaluator.py: Luma/chroma/difference keyer, matte (~200 lines)
- channel_evaluator.py: Shuffle, copy_channel, remove_channels (~200 lines)
- filter_evaluator.py: Erode/dilate, glow, edge_detect, noise, ramp, mask (~400 lines)
- cache_evaluator.py: Cache management methods (~50 lines)
- tile_evaluator.py: Tile rendering and LOD methods (~150 lines)
- histogram_evaluator.py: Histogram computation hooks (~30 lines)
- keyframe_evaluator.py: Keyframe interpolation hooks (~30 lines)
- expression_evaluator.py: Expression evaluation hooks (~30 lines)
- ai_evaluator.py: AI node evaluation hooks (~30 lines)

All public interfaces remain unchanged for backward compatibility.
"""

# Import all evaluator mixins
from src.features.composite.evaluators.base_evaluator import (
    BaseEvaluatorMixin,
    TileContext,
    _AI_NODE_TYPES,
)
from src.features.composite.evaluators.blur_evaluator import BlurEvaluatorMixin
from src.features.composite.evaluators.color_evaluator import ColorEvaluatorMixin
from src.features.composite.evaluators.transform_evaluator import TransformEvaluatorMixin
from src.features.composite.evaluators.merge_evaluator import MergeEvaluatorMixin
from src.features.composite.evaluators.keyer_evaluator import KeyerEvaluatorMixin
from src.features.composite.evaluators.channel_evaluator import ChannelEvaluatorMixin
from src.features.composite.evaluators.filter_evaluator import FilterEvaluatorMixin
from src.features.composite.evaluators.cache_evaluator import CacheEvaluatorMixin
from src.features.composite.evaluators.tile_evaluator import TileEvaluatorMixin
from src.features.composite.evaluators.histogram_evaluator import HistogramEvaluatorMixin
from src.features.composite.evaluators.keyframe_evaluator import KeyframeEvaluatorMixin
from src.features.composite.evaluators.expression_evaluator import ExpressionEvaluatorMixin
from src.features.composite.evaluators.ai_evaluator import AIEvaluatorMixin


# ── Combined GraphEvaluatorMixin ──

class GraphEvaluatorMixin(
    BaseEvaluatorMixin,
    BlurEvaluatorMixin,
    ColorEvaluatorMixin,
    TransformEvaluatorMixin,
    MergeEvaluatorMixin,
    KeyerEvaluatorMixin,
    ChannelEvaluatorMixin,
    FilterEvaluatorMixin,
    CacheEvaluatorMixin,
    TileEvaluatorMixin,
    HistogramEvaluatorMixin,
    KeyframeEvaluatorMixin,
    ExpressionEvaluatorMixin,
    AIEvaluatorMixin,
):
    """Unified graph evaluation mixin combining all specialized evaluators.

    Provides the complete graph evaluation API for composite renderers:
    - CompositeRenderer (async, thread-based, for interactive preview)
    - CompositeExportRenderer (synchronous, one-shot, for full-res export)

    This class inherits from all the specialized evaluator mixins,
    providing a single unified interface while keeping the implementation
    modular and maintainable.

    Dependencies (set by the consuming class):
    - self.ctx: moderngl.Context
    - self.fb_manager: FramebufferManager
    - self.utility_shaders: dict of loaded shader programs
    - self._render_nodes: Dict[str, Node] — persistent GPU-stateful node instances
    - self._cache_manager: CacheManager — content-addressable cache
    - self._resource_pool: GPUResourcePool — memory-tracked texture allocation
    - self._snapshot: Optional[GraphSnapshot] — current graph state being evaluated
    """

    pass


# ── Public exports for backward compatibility ──

# Re-export TileContext for external use
__all__ = [
    'GraphEvaluatorMixin',
    'TileContext',
    '_AI_NODE_TYPES',
]