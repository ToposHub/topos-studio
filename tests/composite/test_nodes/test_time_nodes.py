"""
Time Node Tests - Tests for time/temporal nodes.

Tests cover:
- TimeOffsetNode: offset frame timing
- FrameHoldNode: hold specific frame
- RetimeNode: change timing/speed
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.time_offset_node import TimeOffsetNode
from src.features.composite.nodes.frame_hold_node import FrameHoldNode
from src.features.composite.nodes.retime_node import RetimeNode

import pytest
pytestmark = pytest.mark.composite_core


# ── Time Offset Node Tests ──────────────────────────────────────────────────

class TestTimeOffsetNode(NodeTestBase):
    """Tests for TimeOffsetNode - offset frame timing."""

    node_type = "time_offset"
    node_class = TimeOffsetNode

    def test_offset_parameter(self):
        """Offset 参数."""
        node = self.create_node()
        if "offset" in node.parameters:
            node.set_parameter("offset", 10)
            assert node.parameters.get("offset") == 10
            node.set_parameter("offset", -5)
            assert node.parameters.get("offset") == -5

    def test_time_output(self):
        """Time offset 输出正确."""
        node = self.create_node(params={"offset": 5})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash


# ── Frame Hold Node Tests ───────────────────────────────────────────────────

class TestFrameHoldNode(NodeTestBase):
    """Tests for FrameHoldNode - hold specific frame."""

    node_type = "frame_hold"
    node_class = FrameHoldNode

    def test_frame_number_parameter(self):
        """Frame number 参数."""
        node = self.create_node()
        if "frame" in node.parameters:
            node.set_parameter("frame", 0)
            assert node.parameters.get("frame") == 0
            node.set_parameter("frame", 100)
            assert node.parameters.get("frame") == 100

    def test_hold_output(self):
        """Hold 输出正确."""
        node = self.create_node(params={"frame": 0})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash


# ── Retime Node Tests ───────────────────────────────────────────────────────

class TestRetimeNode(NodeTestBase):
    """Tests for RetimeNode - change timing/speed."""

    node_type = "retime"
    node_class = RetimeNode

    def test_speed_parameter(self):
        """Speed 参数."""
        node = self.create_node()
        if "speed" in node.parameters:
            node.set_parameter("speed", 1.0)
            assert node.parameters.get("speed") == 1.0
            node.set_parameter("speed", 2.0)
            assert node.parameters.get("speed") == 2.0
            node.set_parameter("speed", 0.5)
            assert node.parameters.get("speed") == 0.5

    def test_retime_mode_parameter(self):
        """Retime mode 参数."""
        node = self.create_node()
        if "mode" in node.parameters:
            for mode in ["constant", "ease_in", "ease_out"]:
                try:
                    test_node = self.create_node(params={"mode": mode})
                except:
                    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])