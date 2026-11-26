"""Spatially narrowed envelope hits must retain the original UI semantics."""
from __future__ import annotations

import random

import pytest
from PyQt6.QtCore import QPointF

from src.features.edit.ui.timeline_items import ClipItem


def _item() -> ClipItem:
    return ClipItem("clip", "track", "Audio", 0, 100, 4.8, 0,
                    source_start=0, source_duration=100, max_source_frames=100,
                    track_height=72, media_type="audio")


def _original_hit(item: ClipItem, pos: QPointF) -> int | None:
    """Independent original exhaustive implementation, including last-index ties."""
    geometry = item._envelope_geometry()
    if geometry is None:
        return None
    best = None
    distance = item.ENVELOPE_NODE_HIT_RADIUS
    for index, (fraction, gain) in enumerate(item._envelope_nodes):
        x = fraction * geometry["width"]
        y = geometry["zero_y"] - gain * geometry["pixels_per_db"]
        candidate = ((pos.x() - x) ** 2 + (pos.y() - y) ** 2) ** 0.5
        if candidate <= distance:
            best, distance = index, candidate
    return best


@pytest.mark.parametrize("width,height", [(36.0, 13), (480.0, 72), (8192.3, 120)])
def test_binary_hit_matches_exhaustive_search_after_geometry_changes(
        qapp, width: float, height: int) -> None:
    item = _item()
    rng = random.Random(709)
    # Duplicate fractions and out-of-order input exercise normalization's
    # stable ordering, rather than assuming the caller's original list order.
    nodes = [(rng.random(), rng.uniform(-60.0, 12.0)) for _ in range(1200)]
    nodes += [(0.5, -6.0), (0.5, -6.0), (0.0, 0.0), (1.0, 0.0)]
    item.set_volume_envelope(nodes)
    item._width, item._track_height = width, height
    for _ in range(200):
        position = QPointF(rng.uniform(-8.0, width + 8.0), rng.uniform(-8.0, height + 8.0))
        assert item._envelope_node_at(position) == _original_hit(item, position)
    geometry = item._envelope_geometry()
    for fraction, gain in item._envelope_nodes[::73]:
        x = fraction * width
        y = item._envelope_db_to_y(gain, geometry)
        for dx in (-7.0, 0.0, 7.0):
            position = QPointF(x + dx, y)
            assert item._envelope_node_at(position) == _original_hit(item, position)


def test_equal_distance_uses_last_canonical_index_and_drag_overlay_is_separate(qapp) -> None:
    item = _item()
    item.set_volume_envelope([(0.4, 0.0), (0.4, 0.0), (0.6, 0.0)])
    geometry = item._envelope_geometry()
    assert item._envelope_node_at(QPointF(0.4 * item._width, geometry["zero_y"])) == 1
    item._envelope_drag_nodes = [(0.8, -18.0)]
    assert item._envelope_node_at(QPointF(0.4 * item._width, geometry["zero_y"])) == 1
    item.set_volume_envelope([(100.0 / 480.0, 0.0), (110.0 / 480.0, 0.0)])
    assert item._envelope_node_at(QPointF(105.0, geometry["zero_y"])) == 1
    item.set_volume_envelope([])
    assert item._envelope_node_at(QPointF()) is None


def test_nearest_node_uses_both_axes_not_only_fraction(qapp) -> None:
    item = _item()
    item.set_volume_envelope([(0.5, -8.0), (0.51, 0.0)])
    geometry = item._envelope_geometry()
    position = QPointF(0.5 * item._width, geometry["zero_y"])
    assert item._envelope_node_at(position) == 1
