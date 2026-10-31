"""Test all nodes are registered in NODE_REGISTRY and can be instantiated."""
import pytest
from src.features.composite.core.node_engine import NODE_REGISTRY, NODE_CATEGORIES

import pytest
pytestmark = pytest.mark.composite_core


# All expected node types
EXPECTED_NODES = [
    # Input
    "image_input", "constant", "text", "video_frame", "color_input",
    # Output
    "output", "file_output",
    # Merge
    "merge", "blend", "premult", "unpremult", "mask", "matte",
    "dissolve", "switch",
    # Transform
    "transform", "reformat", "crop",
    # Channel
    "shuffle", "copy_channel", "set_alpha", "remove_channels",
    # Color
    "grade", "color_correct", "invert", "clamp", "cs_transform",
    # Filter
    "blur", "defocus", "glow", "sharpen", "erode_dilate", "denoise",
    # Keyer
    "luma_keyer", "chroma_keyer", "keyer",
    # Mask
    "roto_shape", "mask_merge", "matte_cleanup",
    # Time
    "time_offset", "frame_hold", "retime",
    # Utility
    "dot", "noop",
    # AI
    "ai_generate", "ai_inpaint", "ai_remove", "ai_expand", "ai_denoise",
]


class TestNodeRegistry:
    """Test NODE_REGISTRY contains all expected nodes."""

    def test_registry_not_empty(self):
        assert len(NODE_REGISTRY) > 0

    def test_all_expected_nodes_registered(self):
        """Check all expected nodes are in registry."""
        missing = []
        for node_type in EXPECTED_NODES:
            if node_type not in NODE_REGISTRY:
                missing.append(node_type)
        assert len(missing) == 0, f"Missing nodes: {missing}"

    def test_all_registry_nodes_instantiable(self):
        """Check all registered nodes can be instantiated."""
        failed = []
        for node_type, node_class in NODE_REGISTRY.items():
            if node_class is None:
                continue  # Placeholder nodes are OK
            try:
                node = node_class()
                assert hasattr(node, 'node_type')
                assert hasattr(node, 'PARAM_DEFS')
            except Exception as e:
                failed.append(f"{node_type}: {e}")
        assert len(failed) == 0, f"Failed to instantiate: {failed}"


class TestNodeCategories:
    """Test NODE_CATEGORIES structure."""

    def test_categories_not_empty(self):
        assert len(NODE_CATEGORIES) > 0

    def test_categories_have_nodes(self):
        """Check each category has at least one node."""
        empty_cats = [cat for cat, nodes in NODE_CATEGORIES.items() if len(nodes) == 0]
        assert len(empty_cats) == 0, f"Empty categories: {empty_cats}"
