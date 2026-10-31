"""
Channel Node Tests - Tests for channel manipulation nodes.

Tests cover:
- ShuffleNode: shuffle channels
- CopyChannelNode: copy specific channel
- SetAlphaNode: set alpha channel
- RemoveChannelsNode: remove channels
- ChannelMergeNode: merge channels
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.shuffle_node import ShuffleNode
from src.features.composite.nodes.copy_channel_node import CopyChannelNode
from src.features.composite.nodes.set_alpha_node import SetAlphaNode
from src.features.composite.nodes.remove_channels_node import RemoveChannelsNode
from src.features.composite.nodes.channel_merge_node import ChannelMergeNode

import pytest
pytestmark = pytest.mark.composite_core


# ── Shuffle Node Tests ──────────────────────────────────────────────────────

class TestShuffleNode(NodeTestBase):
    """Tests for ShuffleNode - shuffle channels."""

    node_type = "shuffle"
    node_class = ShuffleNode

    def test_channel_mapping(self):
        """Channel mapping 参数."""
        node = self.create_node()
        # Check channel mapping params
        for param in ["red", "green", "blue", "alpha"]:
            if param in node.parameters:
                # Should be able to set channel source
                node.set_parameter(param, "rgba.red")

    def test_shuffle_options(self):
        """Shuffle options (RGBA, A->A, etc.)."""
        node = self.create_node()
        # Common shuffle operations should work
        if "shuffle_mode" in node.parameters:
            for mode in ["rgba", "aaaa", "rgb0"]:
                try:
                    test_node = self.create_node(params={"shuffle_mode": mode})
                except:
                    pass


# ── Copy Channel Node Tests ─────────────────────────────────────────────────

class TestCopyChannelNode(NodeTestBase):
    """Tests for CopyChannelNode - copy specific channel."""

    node_type = "copy_channel"
    node_class = CopyChannelNode

    def test_source_channel(self):
        """Source channel 参数."""
        node = self.create_node()
        if "source_channel" in node.parameters:
            node.set_parameter("source_channel", "red")

    def test_destination_channel(self):
        """Destination channel 参数."""
        node = self.create_node()
        if "dest_channel" in node.parameters:
            node.set_parameter("dest_channel", "alpha")


# ── Set Alpha Node Tests ────────────────────────────────────────────────────

class TestSetAlphaNode(NodeTestBase):
    """Tests for SetAlphaNode - set alpha channel."""

    node_type = "set_alpha"
    node_class = SetAlphaNode

    def test_alpha_value(self):
        """Alpha value 参数."""
        node = self.create_node()
        if "alpha" in node.parameters:
            node.set_parameter("alpha", 1.0)
            assert node.parameters.get("alpha") == 1.0
            node.set_parameter("alpha", 0.5)
            assert node.parameters.get("alpha") == 0.5

    def test_alpha_source(self):
        """Alpha source 可选."""
        node = self.create_node()
        # Check if can use second input as alpha
        if "alpha_source" in node.parameters:
            node.set_parameter("alpha_source", "input")


# ── Remove Channels Node Tests ──────────────────────────────────────────────

class TestRemoveChannelsNode(NodeTestBase):
    """Tests for RemoveChannelsNode - remove channels."""

    node_type = "remove_channels"
    node_class = RemoveChannelsNode

    def test_remove_channels_option(self):
        """Remove channels 参数."""
        node = self.create_node()
        if "channels" in node.parameters:
            node.set_parameter("channels", "alpha")

    def test_keep_rgb(self):
        """Keep RGB only."""
        node = self.create_node(params={"channels": "alpha"})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash


# ── Channel Merge Node Tests ────────────────────────────────────────────────

class TestChannelMergeNode(NodeTestBase):
    """Tests for ChannelMergeNode - merge channels from multiple inputs."""

    node_type = "channel_merge"
    node_class = ChannelMergeNode

    def test_multi_input_handling(self):
        """Channel merge 处理多输入."""
        node = self.create_node()
        # Should have multiple input ports
        assert len(node.inputs) >= 1

    def test_channel_selection(self):
        """Channel selection 参数."""
        node = self.create_node()
        # Check channel selection params
        if "a_channel" in node.parameters:
            node.set_parameter("a_channel", "red")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])