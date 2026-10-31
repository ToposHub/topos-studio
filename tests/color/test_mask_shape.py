"""Tests for MaskShape class (now at src/features/color/core/layer.py:7)"""

import pytest
from src.features.color.core.layer import MaskShape


class TestMaskShapeCreation:
    def test_circle_creation(self):
        shape = MaskShape(MaskShape.CIRCLE)
        assert shape.type == "circle"

    def test_rect_creation(self):
        shape = MaskShape(MaskShape.RECT)
        assert shape.type == "rect"

    def test_curve_creation(self):
        shape = MaskShape(MaskShape.CURVE)
        assert shape.type == "curve"

    def test_gradient_creation(self):
        shape = MaskShape(MaskShape.GRADIENT)
        assert shape.type == "gradient"

    def test_bitmap_creation(self):
        shape = MaskShape(MaskShape.BITMAP)
        assert shape.type == "bitmap"

    def test_brush_creation(self):
        shape = MaskShape(MaskShape.BRUSH)
        assert shape.type == "brush"


class TestMaskShapeDefaults:
    def test_has_unique_id(self):
        s1 = MaskShape(MaskShape.CIRCLE)
        s2 = MaskShape(MaskShape.CIRCLE)
        assert s1.id != s2.id

    def test_default_visibility(self):
        shape = MaskShape(MaskShape.CIRCLE)
        assert shape.visible is True

    def test_default_feather(self):
        shape = MaskShape(MaskShape.CIRCLE)
        assert shape.feather == 50

    def test_default_opacity(self):
        shape = MaskShape(MaskShape.CIRCLE)
        assert shape.opacity == 1.0

    def test_default_operation(self):
        shape = MaskShape(MaskShape.CIRCLE)
        assert shape.operation == MaskShape.OP_ADD

    def test_default_invert(self):
        shape = MaskShape(MaskShape.CIRCLE)
        assert shape.invert is False

    def test_default_center(self):
        shape = MaskShape(MaskShape.CIRCLE)
        assert shape.center == [0.5, 0.5]

    def test_default_size(self):
        shape = MaskShape(MaskShape.CIRCLE)
        assert shape.size == [0.3, 0.3]


class TestMaskShapeOperations:
    def test_op_add(self):
        assert MaskShape.OP_ADD == "add"

    def test_op_subtract(self):
        assert MaskShape.OP_SUBTRACT == "subtract"

    def test_op_intersect(self):
        assert MaskShape.OP_INTERSECT == "intersect"


class TestMaskShapeSerialization:
    def test_to_dict_roundtrip(self):
        shape = MaskShape(MaskShape.CIRCLE)
        shape.center = [0.3, 0.7]
        shape.feather = 80
        shape.opacity = 0.5
        shape.rotation = 45.0

        data = shape.to_dict()
        restored = MaskShape.from_dict(data)

        assert restored.id == shape.id
        assert restored.type == shape.type
        assert restored.center == [0.3, 0.7]
        assert restored.feather == 80
        assert restored.opacity == 0.5
        assert restored.rotation == 45.0

    def test_to_dict_includes_all_keys(self):
        shape = MaskShape(MaskShape.RECT)
        data = shape.to_dict()

        expected_keys = ["id", "type", "name", "visible", "invert", "operation",
                         "feather", "opacity", "blur", "center", "size", "rotation",
                         "points", "grad_points", "softness"]
        for key in expected_keys:
            assert key in data, f"Missing key: {key}"

    def test_brush_shape_serialization(self):
        shape = MaskShape(MaskShape.BRUSH)
        shape.brush_strokes = [{"points": [(0.1, 0.2, 0.5)], "size": 30}]
        shape.brush_size = 40
        shape.brush_feather = 10

        data = shape.to_dict()
        assert "brush_strokes" in data
        assert "brush_size" in data
        assert "brush_feather" in data

        restored = MaskShape.from_dict(data)
        assert len(restored.brush_strokes) == 1
        assert restored.brush_size == 40
        assert restored.brush_feather == 10

    def test_non_brush_no_brush_keys(self):
        shape = MaskShape(MaskShape.CIRCLE)
        data = shape.to_dict()
        assert "brush_strokes" not in data
        assert "brush_size" not in data
