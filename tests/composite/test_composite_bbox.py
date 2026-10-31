"""
Tests for BBox — bounding box / data window data type.

Covers:
- Construction, properties (right, top, area, is_empty)
- Union and intersection
- Expanded (for blur radius)
- Clamped (for format boundary)
- transformed_corners / from_corners (for transform bbox)
- full_frame factory
- Serialization round-trip
- Edge cases (empty, zero-size, negative)
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox, ImageFormat

import pytest
pytestmark = pytest.mark.composite_core


class TestBBoxConstruction:
    def test_basic(self):
        b = BBox(x=10, y=20, width=100, height=200)
        assert b.x == 10
        assert b.y == 20
        assert b.width == 100
        assert b.height == 200

    def test_properties(self):
        b = BBox(x=10, y=20, width=100, height=200)
        assert b.right == 110
        assert b.top == 220

    def test_area(self):
        b = BBox(x=0, y=0, width=100, height=50)
        assert b.area == 5000

    def test_area_zero(self):
        b = BBox(x=0, y=0, width=0, height=100)
        assert b.area == 0

    def test_is_empty(self):
        assert BBox(x=0, y=0, width=0, height=100).is_empty
        assert BBox(x=0, y=0, width=100, height=0).is_empty
        assert not BBox(x=0, y=0, width=1, height=1).is_empty

    def test_frozen(self):
        b = BBox(x=0, y=0, width=100, height=100)
        with pytest.raises(AttributeError):
            b.width = 200


class TestBBoxContains:
    def test_inside(self):
        b = BBox(x=10, y=20, width=100, height=50)
        assert b.contains(10, 20)    # bottom-left corner (inclusive)
        assert b.contains(50, 40)    # center
        assert b.contains(109, 20)   # still inside: 109 < 110 (right)
        assert not b.contains(110, 20)  # right edge (exclusive)
        assert not b.contains(10, 70)   # top edge (exclusive)

    def test_outside(self):
        b = BBox(x=10, y=20, width=100, height=50)
        assert not b.contains(0, 0)
        assert not b.contains(200, 200)


class TestBBoxUnion:
    def test_overlapping(self):
        a = BBox(x=0, y=0, width=100, height=100)
        b = BBox(x=50, y=50, width=100, height=100)
        u = a.union(b)
        assert u.x == 0 and u.y == 0
        assert u.right == 150
        assert u.top == 150
        assert u.width == 150 and u.height == 150

    def test_non_overlapping(self):
        a = BBox(x=0, y=0, width=50, height=50)
        b = BBox(x=100, y=100, width=50, height=50)
        u = a.union(b)
        assert u.x == 0 and u.y == 0
        assert u.width == 150 and u.height == 150

    def test_same_bbox(self):
        a = BBox(x=10, y=20, width=30, height=40)
        assert a.union(a) == a

    def test_one_empty(self):
        a = BBox(x=0, y=0, width=100, height=100)
        empty = BBox(x=0, y=0, width=0, height=0)
        assert a.union(empty) == a
        assert empty.union(a) == a

    def test_both_empty(self):
        a = BBox(x=0, y=0, width=0, height=0)
        b = BBox(x=10, y=10, width=0, height=0)
        result = a.union(b)
        assert result.is_empty


class TestBBoxIntersection:
    def test_overlapping(self):
        a = BBox(x=0, y=0, width=100, height=100)
        b = BBox(x=50, y=50, width=100, height=100)
        i = a.intersection(b)
        assert i.x == 50 and i.y == 50
        assert i.width == 50 and i.height == 50

    def test_non_overlapping(self):
        a = BBox(x=0, y=0, width=50, height=50)
        b = BBox(x=100, y=100, width=50, height=50)
        i = a.intersection(b)
        assert i.is_empty

    def test_contained(self):
        a = BBox(x=0, y=0, width=200, height=200)
        b = BBox(x=50, y=50, width=50, height=50)
        i = a.intersection(b)
        assert i == b

    def test_edge_touch(self):
        a = BBox(x=0, y=0, width=50, height=50)
        b = BBox(x=50, y=0, width=50, height=50)
        i = a.intersection(b)
        assert i.is_empty  # touching edges don't overlap


class TestBBoxExpanded:
    def test_positive_margin(self):
        b = BBox(x=10, y=20, width=100, height=200)
        e = b.expanded(5)
        assert e.x == 5
        assert e.y == 15
        assert e.width == 110
        assert e.height == 210

    def test_zero_margin(self):
        b = BBox(x=10, y=20, width=100, height=200)
        assert b.expanded(0) == b

    def test_negative_margin_allowed(self):
        b = BBox(x=10, y=20, width=100, height=200)
        e = b.expanded(-5)
        assert e.x == 15
        assert e.y == 25
        assert e.width == 90
        assert e.height == 190


class TestBBoxClamped:
    def test_within_format(self):
        fmt = ImageFormat(width=1920, height=1080)
        b = BBox(x=0, y=0, width=100, height=100)
        c = b.clamped(fmt)
        assert c == b

    def test_exceeds_format(self):
        fmt = ImageFormat(width=1920, height=1080)
        b = BBox(x=-10, y=-20, width=2000, height=1200)
        c = b.clamped(fmt)
        assert c.x == 0
        assert c.y == 0
        assert c.width == 1920
        assert c.height == 1080

    def test_outside_format(self):
        fmt = ImageFormat(width=100, height=100)
        b = BBox(x=200, y=200, width=50, height=50)
        c = b.clamped(fmt)
        assert c.is_empty


class TestBBoxTransformedCorners:
    def test_identity(self):
        b = BBox(x=10, y=20, width=100, height=200)
        corners = b.transformed_corners((1, 0, 0, 1, 0, 0))
        assert corners[0] == (10, 20)
        assert corners[1] == (110, 20)
        assert corners[2] == (10, 220)
        assert corners[3] == (110, 220)

    def test_translation(self):
        b = BBox(x=10, y=20, width=100, height=50)
        # Translate by (5, 10)
        corners = b.transformed_corners((1, 0, 0, 1, 5, 10))
        assert corners[0] == (15, 30)

    def test_scale(self):
        b = BBox(x=10, y=10, width=100, height=100)
        # Scale 2x
        corners = b.transformed_corners((2, 0, 0, 2, 0, 0))
        assert corners[0] == (20, 20)
        assert corners[3] == (220, 220)


class TestBBoxFromCorners:
    def test_integer_corners(self):
        corners = [(10, 20), (110, 20), (10, 220), (110, 220)]
        b = BBox.from_corners(corners)
        assert b.x == 10
        assert b.y == 20
        assert b.width == 100
        assert b.height == 200

    def test_float_corners_floors_ceil(self):
        corners = [(10.3, 20.7), (110.8, 20.1), (10.1, 220.9), (111.2, 221.3)]
        b = BBox.from_corners(corners)
        assert b.x == 10    # floor(10.1)
        assert b.y == 20    # floor(20.1)
        assert b.right == 112  # ceil(111.2)
        assert b.top == 222   # ceil(221.3)

    def test_empty_corners(self):
        b = BBox.from_corners([])
        assert b.is_empty

    def test_single_point(self):
        b = BBox.from_corners([(5.5, 5.5)])
        assert b.x == 5
        assert b.y == 5
        assert b.right == 6
        assert b.top == 6


class TestBBoxFullFrame:
    def test_basic(self):
        b = BBox.full_frame(1920, 1080)
        assert b.x == 0 and b.y == 0
        assert b.width == 1920
        assert b.height == 1080

    def test_small(self):
        b = BBox.full_frame(1, 1)
        assert b.area == 1


class TestBBoxSerialization:
    def test_roundtrip(self):
        b = BBox(x=10, y=20, width=100, height=200)
        data = b.to_dict()
        b2 = BBox.from_dict(data)
        assert b2 == b

    def test_from_dict_missing_fields(self):
        data = {"x": 5}
        b = BBox.from_dict(data)
        assert b.x == 5
        assert b.y == 0
        assert b.width == 0
        assert b.height == 0

    def test_from_dict_empty(self):
        b = BBox.from_dict({})
        assert b.x == 0 and b.y == 0 and b.width == 0 and b.height == 0

    def test_to_dict_keys(self):
        b = BBox(x=1, y=2, width=3, height=4)
        d = b.to_dict()
        assert set(d.keys()) == {"x", "y", "width", "height"}
