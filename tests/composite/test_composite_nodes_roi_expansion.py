"""ROI expansion regression tests for MatteCleanup/Sharpen/ErodeDilate nodes."""

import pytest
from src.features.composite.core.format import BBox
from src.features.composite.nodes.matte_cleanup_node import MatteCleanupNode
from src.features.composite.nodes.sharpen_node import SharpenNode
from src.features.composite.nodes.erode_dilate_node import ErodeDilateNode

import pytest
pytestmark = pytest.mark.composite_core


class TestMatteCleanupROIExpansion:
    """Test MatteCleanupNode ROI expansion for blur/erode/dilate."""

    def test_blur_operation_roi_expansion(self):
        """Test blur operation expands ROI by the full gaussian radius.

        AUD-7006: the dispatcher feeds ``blur_amount`` verbatim into the
        gaussian ``radius_x``/``radius_y`` (keyer_evaluator), and BlurNode's
        padding convention for the same semantic is the full radius — the
        old ``blur_amount // 2 + 1`` under-padded by ~half the kernel and
        darkened ROI/tile edges.
        """
        node = MatteCleanupNode()
        params = {"operation": "blur", "blur_amount": 4.0}
        output_roi = BBox(100, 100, 200, 150)
        input_rois = node.compute_input_roi(output_roi, params)
        expected_padding = 4  # full gaussian radius
        expected = output_roi.expanded(expected_padding)
        assert input_rois["image"] == expected

    def test_erode_operation_roi_expansion(self):
        """Test erode operation expands ROI by size//2+1."""
        node = MatteCleanupNode()
        params = {"operation": "erode", "size": 5}
        output_roi = BBox(100, 100, 200, 150)
        input_rois = node.compute_input_roi(output_roi, params)
        expected_padding = 5 // 2 + 1  # 3
        expected = output_roi.expanded(expected_padding)
        assert input_rois["image"] == expected

    def test_dilate_operation_roi_expansion(self):
        """Test dilate operation expands ROI by size//2+1."""
        node = MatteCleanupNode()
        params = {"operation": "dilate", "size": 7}
        output_roi = BBox(100, 100, 200, 150)
        input_rois = node.compute_input_roi(output_roi, params)
        expected_padding = 7 // 2 + 1  # 4
        expected = output_roi.expanded(expected_padding)
        assert input_rois["image"] == expected

    def test_clamp_operation_no_expansion(self):
        """Test clamp operation does NOT expand ROI."""
        node = MatteCleanupNode()
        params = {"operation": "clamp"}
        output_roi = BBox(100, 100, 200, 150)
        input_rois = node.compute_input_roi(output_roi, params)
        assert input_rois["image"] == output_roi


class TestSharpenROIExpansion:
    """Test SharpenNode ROI expansion."""

    def test_sharpen_roi_expansion(self):
        """Test sharpen expands ROI by max(1, int(radius*2))."""
        node = SharpenNode()
        params = {"radius": 2.5}
        output_roi = BBox(100, 100, 200, 150)
        input_rois = node.compute_input_roi(output_roi, params)
        expected_padding = max(1, int(2.5 * 2))  # 5
        expected = output_roi.expanded(expected_padding)
        assert input_rois["image"] == expected

    def test_sharpen_roi_expansion_min_padding(self):
        """Test sharpen minimum padding is 1."""
        node = SharpenNode()
        params = {"radius": 0.1}
        output_roi = BBox(100, 100, 200, 150)
        input_rois = node.compute_input_roi(output_roi, params)
        expected = output_roi.expanded(1)  # min padding = 1
        assert input_rois["image"] == expected


class TestErodeDilateROIExpansion:
    """Test ErodeDilateNode ROI expansion."""

    def test_erode_roi_expansion(self):
        """Test erode expands ROI by size//2+1."""
        node = ErodeDilateNode()
        params = {"operation": "erode", "size": 5}
        output_roi = BBox(100, 100, 200, 150)
        input_rois = node.compute_input_roi(output_roi, params)
        expected_padding = 5 // 2 + 1  # 3
        expected = output_roi.expanded(expected_padding)
        assert input_rois["image"] == expected

    def test_dilate_roi_expansion(self):
        """Test dilate expands ROI by size//2+1."""
        node = ErodeDilateNode()
        params = {"operation": "dilate", "size": 9}
        output_roi = BBox(100, 100, 200, 150)
        input_rois = node.compute_input_roi(output_roi, params)
        expected_padding = 9 // 2 + 1  # 5
        expected = output_roi.expanded(expected_padding)
        assert input_rois["image"] == expected
