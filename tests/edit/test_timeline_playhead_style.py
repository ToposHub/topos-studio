"""Geometry regressions for the compact timeline playhead."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF

from src.features.edit.ui.timeline_items import PlayheadItem


pytestmark = pytest.mark.edit_core


def test_playhead_pointer_is_compact_and_connected_to_stem() -> None:
    """The pointer must remain a compact shape with no gap before its stem."""
    playhead = PlayheadItem(frame=0, pixels_per_frame=1.0, num_tracks=1)

    assert playhead.boundingRect().width() == 12.0
    assert playhead.shape().contains(QPointF(0.0, 9.5))
    assert playhead.shape().contains(QPointF(0.0, 10.0))
