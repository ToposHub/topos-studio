"""
Unit tests for Reformat, Crop, and upgraded Transform nodes (Step 7 of Phase 2).

Tests cover:
- Node structure (ports, params, PARAM_DEFS)
- Serialization (to_dict / from_dict round-trip)
- ReformatNode: preset/custom mode, resize mode, filter
- CropNode: reformat/none mode, outside param, bbox update
- TransformNode: filter param, identity detection
- Format/BBox flow through nodes
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.transform_node import TransformNode, FILTER_MODES as TRANSFORM_FILTERS
from src.features.composite.nodes.reformat_node import ReformatNode, RESIZE_MODES, FILTER_MODES
from src.features.composite.nodes.crop_node import CropNode, OUTSIDE_MODES
from src.features.composite.core.format import ImageFormat, BBox, FORMAT_PRESETS

import pytest
pytestmark = pytest.mark.composite_core


# ── TransformNode ──


class TestTransformNodeStructure(unittest.TestCase):
    def test_node_type(self):
        self.assertEqual(TransformNode.node_type, "transform")

    def test_inputs(self):
        node = TransformNode()
        # CP-044: base Node appends a generic "mask" port to image-consuming nodes
        self.assertEqual([p.name for p in node.inputs], ["image", "mask"])

    def test_outputs(self):
        node = TransformNode()
        self.assertEqual([p.name for p in node.outputs], ["result"])

    def test_filter_param_default(self):
        node = TransformNode()
        self.assertEqual(node.parameters["filter"], "linear")

    def test_filter_modes(self):
        self.assertEqual(TRANSFORM_FILTERS, ["linear", "nearest"])

    def test_param_defs_has_filter(self):
        names = [p.name for p in TransformNode.PARAM_DEFS]
        self.assertIn("filter", names)


class TestTransformNodeIdentity(unittest.TestCase):
    def _make_tr(self, w=100, h=100):
        return TextureResult(texture_id=1, width=w, height=h)

    def test_identity_returns_input(self):
        node = TransformNode()
        image = self._make_tr()
        result = node._do_evaluate({"image": image}, None, None)
        self.assertIs(result["result"], image)

    def test_non_identity_returns_marker(self):
        node = TransformNode()
        node.parameters["translate_x"] = 0.1
        image = self._make_tr()
        result = node._do_evaluate({"image": image}, None, None)
        self.assertEqual(result["_effect_type"], "transform")
        self.assertIn("filter", result["_effect_params"])


class TestTransformNodeSerialization(unittest.TestCase):
    def test_round_trip(self):
        node = TransformNode()
        node.parameters["filter"] = "nearest"
        node.parameters["rotation"] = 45.0
        d = node.to_dict()
        restored = TransformNode.from_dict(d)
        self.assertEqual(restored.parameters["filter"], "nearest")
        self.assertEqual(restored.parameters["rotation"], 45.0)


# ── ReformatNode ──


class TestReformatNodeStructure(unittest.TestCase):
    def test_node_type(self):
        self.assertEqual(ReformatNode.node_type, "reformat")

    def test_inputs(self):
        node = ReformatNode()
        # CP-044: base Node appends a generic "mask" port to image-consuming nodes
        self.assertEqual([p.name for p in node.inputs], ["image", "mask"])

    def test_outputs(self):
        node = ReformatNode()
        self.assertEqual([p.name for p in node.outputs], ["result"])

    def test_default_params(self):
        node = ReformatNode()
        self.assertEqual(node.parameters["format_mode"], "preset")
        self.assertEqual(node.parameters["preset"], "HD 1080")
        self.assertEqual(node.parameters["resize_mode"], "fit")
        self.assertEqual(node.parameters["filter"], "linear")

    def test_resize_modes(self):
        self.assertEqual(RESIZE_MODES, ["no_resize", "fit", "fill", "distort", "center_crop"])

    def test_filter_modes(self):
        self.assertEqual(FILTER_MODES, ["nearest", "linear", "cubic", "lanczos"])


class TestReformatNodeEvaluation(unittest.TestCase):
    def _make_tr(self, w=100, h=100):
        return TextureResult(texture_id=1, width=w, height=h)

    def test_no_input_returns_empty(self):
        node = ReformatNode()
        result = node._do_evaluate({}, None, None)
        self.assertEqual(result, {})

    def test_preset_mode_returns_marker(self):
        node = ReformatNode()
        image = self._make_tr(100, 100)
        result = node._do_evaluate({"image": image}, None, None)
        self.assertEqual(result["_effect_type"], "reformat")
        self.assertEqual(result["_effect_params"]["target_width"], 1920)
        self.assertEqual(result["_effect_params"]["target_height"], 1080)

    def test_custom_mode(self):
        node = ReformatNode()
        node.parameters["format_mode"] = "custom"
        node.parameters["width"] = 640
        node.parameters["height"] = 480
        image = self._make_tr(100, 100)
        result = node._do_evaluate({"image": image}, None, None)
        self.assertEqual(result["_effect_params"]["target_width"], 640)
        self.assertEqual(result["_effect_params"]["target_height"], 480)

    def test_same_size_passthrough(self):
        node = ReformatNode()
        node.parameters["preset"] = "HD 720"  # 1280x720
        image = self._make_tr(1280, 720)
        result = node._do_evaluate({"image": image}, None, None)
        # Same dimensions → passthrough (no marker)
        self.assertNotIn("_effect_type", result)
        self.assertEqual(result["result"].format.name, "Reformatted")

    def test_resize_mode_index(self):
        node = ReformatNode()
        node.parameters["resize_mode"] = "fill"
        image = self._make_tr(100, 100)
        result = node._do_evaluate({"image": image}, None, None)
        self.assertEqual(result["_effect_params"]["resize_mode"], 2)  # fill=2

    def test_no_resize_mode_produces_marker(self):
        node = ReformatNode()
        node.parameters["resize_mode"] = "no_resize"
        image = self._make_tr(100, 100)
        result = node._do_evaluate({"image": image}, None, None)
        self.assertEqual(result["_effect_type"], "reformat")
        self.assertEqual(result["_effect_params"]["resize_mode"], 0)  # no_resize=0


class TestReformatNodeSerialization(unittest.TestCase):
    def test_round_trip(self):
        node = ReformatNode()
        node.parameters["format_mode"] = "custom"
        node.parameters["width"] = 640
        node.parameters["height"] = 480
        node.parameters["resize_mode"] = "fill"
        node.parameters["filter"] = "nearest"
        d = node.to_dict()
        restored = ReformatNode.from_dict(d)
        self.assertEqual(restored.parameters["format_mode"], "custom")
        self.assertEqual(restored.parameters["width"], 640)
        self.assertEqual(restored.parameters["resize_mode"], "fill")
        self.assertEqual(restored.parameters["filter"], "nearest")

    def test_old_project_missing_filter(self):
        """Old projects don't have 'filter' param — should get default."""
        d = {"id": "test", "type": "reformat", "parameters": {"preset": "HD 1080"}}
        node = ReformatNode.from_dict(d)
        self.assertEqual(node.parameters["filter"], "linear")


# ── CropNode ──


class TestCropNodeStructure(unittest.TestCase):
    def test_node_type(self):
        self.assertEqual(CropNode.node_type, "crop")

    def test_inputs(self):
        node = CropNode()
        # CP-044: base Node appends a generic "mask" port to image-consuming nodes
        self.assertEqual([p.name for p in node.inputs], ["image", "mask"])

    def test_outputs(self):
        node = CropNode()
        self.assertEqual([p.name for p in node.outputs], ["result"])

    def test_default_params(self):
        node = CropNode()
        self.assertEqual(node.parameters["x"], 0)
        self.assertEqual(node.parameters["y"], 0)
        self.assertEqual(node.parameters["width"], 512)
        self.assertEqual(node.parameters["height"], 512)
        self.assertEqual(node.parameters["reformat"], True)
        self.assertEqual(node.parameters["outside"], "transparent")

    def test_outside_modes(self):
        self.assertEqual(OUTSIDE_MODES, ["black", "transparent"])


class TestCropNodeReformat(unittest.TestCase):
    def _make_tr(self, w=1920, h=1080, tex_id=1):
        fmt = ImageFormat(width=w, height=h, name="Test")
        return TextureResult(texture_id=tex_id, width=w, height=h, format=fmt)

    def test_reformat_true_returns_marker(self):
        node = CropNode()
        node.parameters["reformat"] = True
        image = self._make_tr()
        result = node._do_evaluate({"image": image}, None, None)
        self.assertEqual(result["_effect_type"], "crop")
        self.assertEqual(result["_effect_params"]["crop_w"], 512)
        self.assertEqual(result["_effect_params"]["crop_h"], 512)

    def test_reformat_true_outside_index(self):
        node = CropNode()
        node.parameters["reformat"] = True
        node.parameters["outside"] = "black"
        image = self._make_tr()
        result = node._do_evaluate({"image": image}, None, None)
        self.assertEqual(result["_effect_params"]["outside"], 0)  # black=0


class TestCropNodeNoReformat(unittest.TestCase):
    def _make_tr(self, w=1920, h=1080, bbox=None):
        fmt = ImageFormat(width=w, height=h, name="Test")
        return TextureResult(texture_id=1, width=w, height=h, format=fmt, bbox=bbox)

    def test_reformat_false_returns_clean_result(self):
        node = CropNode()
        node.parameters["reformat"] = False
        node.parameters["x"] = 100
        node.parameters["y"] = 50
        node.parameters["width"] = 200
        node.parameters["height"] = 300
        image = self._make_tr()
        result = node._do_evaluate({"image": image}, None, None)
        self.assertNotIn("_effect_type", result)
        # Same texture ID (no GPU render)
        self.assertEqual(result["result"].texture_id, 1)
        # Bbox is clipped
        self.assertIsNotNone(result["result"].bbox)

    def test_reformat_false_bbox_intersection(self):
        node = CropNode()
        node.parameters["reformat"] = False
        node.parameters["x"] = 100
        node.parameters["y"] = 50
        node.parameters["width"] = 200
        node.parameters["height"] = 300
        # Input bbox covers entire image
        image = self._make_tr(bbox=BBox(0, 0, 1920, 1080))
        result = node._do_evaluate({"image": image}, None, None)
        bbox = result["result"].bbox
        self.assertEqual(bbox.x, 100)
        self.assertEqual(bbox.y, 50)
        self.assertEqual(bbox.width, 200)
        self.assertEqual(bbox.height, 300)


class TestCropNodeSerialization(unittest.TestCase):
    def test_round_trip(self):
        node = CropNode()
        node.parameters["x"] = 100
        node.parameters["y"] = 200
        node.parameters["width"] = 640
        node.parameters["height"] = 480
        node.parameters["reformat"] = False
        node.parameters["outside"] = "black"
        d = node.to_dict()
        restored = CropNode.from_dict(d)
        self.assertEqual(restored.parameters["x"], 100)
        self.assertEqual(restored.parameters["reformat"], False)
        self.assertEqual(restored.parameters["outside"], "black")

    def test_old_project_missing_reformat(self):
        d = {"id": "test", "type": "crop", "parameters": {"x": 0, "y": 0, "width": 100, "height": 100}}
        node = CropNode.from_dict(d)
        self.assertEqual(node.parameters["reformat"], True)
        self.assertEqual(node.parameters["outside"], "transparent")


# ── Format Presets ──


class TestFormatPresets(unittest.TestCase):
    def test_common_presets_exist(self):
        for name in ["HD 720", "HD 1080", "UHD 4K"]:
            self.assertIn(name, FORMAT_PRESETS)

    def test_hd1080_dimensions(self):
        fmt = FORMAT_PRESETS["HD 1080"]
        self.assertEqual(fmt.width, 1920)
        self.assertEqual(fmt.height, 1080)

    def test_uhd4k_dimensions(self):
        fmt = FORMAT_PRESETS["UHD 4K"]
        self.assertEqual(fmt.width, 3840)
        self.assertEqual(fmt.height, 2160)


# ── BBox Operations (verify for transform bbox fix) ──


class TestBBoxTransformedCorners(unittest.TestCase):
    """Verify that BBox.transformed_corners works correctly with the
    corrected pixel-space conversion in _apply_transform."""

    def test_identity_corners(self):
        bbox = BBox(10, 20, 100, 200)
        # Identity transform: m00=1, m10=0, m01=0, m11=1, m02=0, m12=0
        # But in the evaluator, we now use transform_pixel which converts
        # through UV space. For identity: (px, py) → (px, py)
        corners = bbox.transformed_corners((1, 0, 0, 1, 0, 0))
        self.assertEqual(corners[0], (10, 20))
        self.assertEqual(corners[1], (110, 20))
        self.assertEqual(corners[2], (10, 220))
        self.assertEqual(corners[3], (110, 220))

    def test_from_corners_encloses(self):
        corners = [(10.3, 20.7), (110.8, 20.1), (10.1, 220.9), (111.2, 221.3)]
        bbox = BBox.from_corners(corners)
        # floor for min, ceil for max
        self.assertEqual(bbox.x, 10)
        self.assertEqual(bbox.y, 20)
        self.assertEqual(bbox.right, 112)
        self.assertEqual(bbox.top, 222)


if __name__ == "__main__":
    unittest.main()
