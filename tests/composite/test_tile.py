"""Tests for Tile class (now at src/features/color/core/image_engine.py:19)"""

import pytest
from src.features.color.core.image_engine import Tile


class TestTile:
    def test_tile_creation(self, sample_tile):
        assert sample_tile.x == 0
        assert sample_tile.y == 0
        assert sample_tile.w == 512
        assert sample_tile.h == 512
        assert sample_tile.level == 0

    def test_tile_different_values(self):
        tile = Tile(100, 200, 256, 128, 2)
        assert tile.x == 100
        assert tile.y == 200
        assert tile.w == 256
        assert tile.h == 128
        assert tile.level == 2

    def test_tile_attributes_mutable(self, sample_tile):
        sample_tile.x = 50
        sample_tile.level = 3
        assert sample_tile.x == 50
        assert sample_tile.level == 3

    def test_tile_equality_by_value(self):
        t1 = Tile(10, 20, 100, 100, 1)
        t2 = Tile(10, 20, 100, 100, 1)
        assert t1.x == t2.x
        assert t1.y == t2.y
        assert t1.w == t2.w
        assert t1.h == t2.h
        assert t1.level == t2.level
