"""Tests for MaskGroup class (now at src/features/color/core/layer.py:162)"""

import pytest
from src.features.color.core.layer import MaskShape, MaskGroup


class TestMaskGroupCreation:
    def test_default_creation(self):
        group = MaskGroup()
        assert group.name == "Group"
        assert group.children == []
        assert group.visible is True
        assert group.invert is False
        assert group.operation == MaskGroup.OP_ADD
        assert group.opacity == 1.0
        assert group.expanded is True

    def test_custom_name(self):
        group = MaskGroup("My Group")
        assert group.name == "My Group"

    def test_has_unique_id(self):
        g1 = MaskGroup()
        g2 = MaskGroup()
        assert g1.id != g2.id


class TestMaskGroupChildren:
    def test_add_child(self):
        group = MaskGroup()
        shape = MaskShape(MaskShape.CIRCLE)
        group.add_child(shape)
        assert len(group.children) == 1
        assert group.children[0] is shape

    def test_remove_child(self):
        group = MaskGroup()
        shape = MaskShape(MaskShape.CIRCLE)
        group.add_child(shape)
        assert group.remove_child(shape.id) is True
        assert len(group.children) == 0

    def test_remove_nonexistent_child(self):
        group = MaskGroup()
        assert group.remove_child("nonexistent_id") is False

    def test_get_child_by_id(self):
        group = MaskGroup()
        shape = MaskShape(MaskShape.RECT)
        group.add_child(shape)
        found = group.get_child_by_id(shape.id)
        assert found is shape

    def test_get_child_by_id_not_found(self):
        group = MaskGroup()
        assert group.get_child_by_id("nonexistent") is None


class TestMaskGroupNested:
    def test_nested_group(self):
        outer = MaskGroup("Outer")
        inner = MaskGroup("Inner")
        shape = MaskShape(MaskShape.CIRCLE)
        inner.add_child(shape)
        outer.add_child(inner)
        assert len(outer.children) == 1
        assert len(inner.children) == 1

    def test_get_nested_child_by_id(self):
        outer = MaskGroup("Outer")
        inner = MaskGroup("Inner")
        shape = MaskShape(MaskShape.CIRCLE)
        inner.add_child(shape)
        outer.add_child(inner)
        found = outer.get_child_by_id(shape.id)
        assert found is shape

    def test_remove_nested_child(self):
        outer = MaskGroup("Outer")
        inner = MaskGroup("Inner")
        shape = MaskShape(MaskShape.CIRCLE)
        inner.add_child(shape)
        outer.add_child(inner)
        # Remove from outer via inner's child
        assert inner.remove_child(shape.id) is True
        assert len(inner.children) == 0


class TestMaskGroupSerialization:
    def test_to_dict_roundtrip(self):
        group = MaskGroup("Test")
        shape = MaskShape(MaskShape.CIRCLE)
        group.add_child(shape)

        data = group.to_dict()
        restored = MaskGroup.from_dict(data)

        assert restored.name == "Test"
        assert len(restored.children) == 1
        assert restored.children[0].type == MaskShape.CIRCLE

    def test_nested_roundtrip(self):
        outer = MaskGroup("Outer")
        inner = MaskGroup("Inner")
        shape = MaskShape(MaskShape.RECT)
        inner.add_child(shape)
        outer.add_child(inner)

        data = outer.to_dict()
        restored = MaskGroup.from_dict(data)

        assert restored.name == "Outer"
        assert len(restored.children) == 1
        inner_restored = restored.children[0]
        assert isinstance(inner_restored, MaskGroup)
        assert inner_restored.name == "Inner"
        assert len(inner_restored.children) == 1
        assert inner_restored.children[0].type == MaskShape.RECT
