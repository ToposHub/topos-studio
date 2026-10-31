"""
Test nodes package - Standardized tests for all node types.

This package contains test files for each node type in the NODE_REGISTRY.
Tests inherit from node_test_base.NodeTestBase for standardized coverage.

Organization:
- test_color_nodes.py: grade, color_correct, huecorrect, lut
- test_merge_nodes.py: blend, merge, premult, unpremult, dissolve
- test_keyer_nodes.py: keyer, chroma_keyer, luma_keyer, difference_keyer
- test_filter_nodes.py: blur, glow, denoise, sharpen, etc.
- test_transform_nodes.py: transform, crop, reformat, cornerpin
- test_channel_nodes.py: shuffle, copy_channel, set_alpha, etc.
- test_input_nodes.py: color_input, constant, image_input
- test_utility_nodes.py: invert, clamp, normalize, compare, etc.
- test_ai_nodes.py: ai_generate, ai_inpaint, ai_expand, etc.
"""

from tests.node_test_base import (
    NodeTestBase,
    ColorNodeTestMixin,
    MergeNodeTestMixin,
    KeyerNodeTestMixin,
    FilterNodeTestMixin,
    MockModernGLContext,
    MockFramebufferManager,
    MockGPUResourcePool,
    make_texture_result,
    get_param_boundaries,
    get_boundary_test_values,
)

__all__ = [
    'NodeTestBase',
    'ColorNodeTestMixin',
    'MergeNodeTestMixin',
    'KeyerNodeTestMixin',
    'FilterNodeTestMixin',
    'MockModernGLContext',
    'MockFramebufferManager',
    'MockGPUResourcePool',
    'make_texture_result',
    'get_param_boundaries',
    'get_boundary_test_values',
]