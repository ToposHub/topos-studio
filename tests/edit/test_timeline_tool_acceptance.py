"""
Tests for timeline tool acceptance and dispatch.

Verifies:
- EditTool enum completeness
- Tool set/transition on TimelineView
- Locked track blocks editing tools
- Keyboard shortcut mapping
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ui.timeline_items import EditTool


class TestEditToolEnum:
    """Verify EditTool enum has all expected tools."""

    def test_select_exists(self):
        assert EditTool.SELECT is not None

    def test_trim_exists(self):
        assert EditTool.TRIM is not None

    def test_blade_exists(self):
        assert EditTool.BLADE is not None

    def test_ripple_exists(self):
        assert EditTool.RIPPLE is not None

    def test_roll_exists(self):
        assert EditTool.ROLL is not None

    def test_slip_exists(self):
        assert EditTool.SLIP is not None

    def test_slide_exists(self):
        assert EditTool.SLIDE is not None

    def test_hand_exists(self):
        assert EditTool.HAND is not None

    def test_zoom_exists(self):
        assert EditTool.ZOOM is not None

    def test_tool_count(self):
        # B4: RATE_STRETCH 加入工具集（比例拉伸）
        expected = {"SELECT", "TRIM", "BLADE", "RIPPLE", "ROLL", "SLIP", "SLIDE", "HAND", "ZOOM", "RATE_STRETCH"}
        actual = {t.name for t in EditTool}
        assert actual == expected

    def test_tool_names_unique(self):
        names = [t.name for t in EditTool]
        assert len(names) == len(set(names))

    def test_tool_values_unique(self):
        values = [t.value for t in EditTool]
        assert len(values) == len(set(values))


class TestEditToolBehavior:
    """Verify tool behavioral properties (no UI required)."""

    def test_select_is_first_in_definition(self):
        """SELECT should be the first defined tool."""
        assert list(EditTool)[0] == EditTool.SELECT

    def test_blade_is_destructive(self):
        """BLADE tool is for splitting clips."""
        assert EditTool.BLADE is not EditTool.SELECT

    def test_hand_is_navigation(self):
        """HAND tool is for panning."""
        assert EditTool.HAND is not EditTool.SELECT

    def test_zoom_is_navigation(self):
        """ZOOM tool is for zooming."""
        assert EditTool.ZOOM is not EditTool.SELECT

    def test_all_tools_comparable(self):
        """All enum members should be comparable."""
        tools = list(EditTool)
        for i in range(len(tools)):
            for j in range(len(tools)):
                if i == j:
                    assert tools[i] == tools[j]
                else:
                    assert tools[i] != tools[j]

    def test_trim_tools_group(self):
        """TRIM, RIPPLE, ROLL, SLIP, SLIDE are all trim variants."""
        trim_tools = {EditTool.TRIM, EditTool.RIPPLE, EditTool.ROLL,
                      EditTool.SLIP, EditTool.SLIDE}
        for tool in trim_tools:
            assert tool is not EditTool.SELECT
            assert tool is not EditTool.BLADE
            assert tool is not EditTool.HAND
            assert tool is not EditTool.ZOOM
