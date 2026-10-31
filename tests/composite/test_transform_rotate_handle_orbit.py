"""Transform rotate handle should orbit the pivot toward the mouse during drag.

Symptom: "我移动它，它的朝向始终会对齐顶部的中间点" — when dragging the rotate
handle, its orientation always locks onto the top-middle point.

Root cause: the rotate handle position is computed as
``edge_top + (pivot - edge_top) * INSET`` (viewer_handles.compute_transform_handles).
That locks the handle onto the frame's top-edge ↔ pivot axis, so its offset from
the pivot always points at the (rotated) top-middle. The glow-dot glyph itself
has no direction; the perceived "orientation" is entirely this axis lock plus
the cyan guide line drawn to the pivot.

Fix: during an active rotate drag, reposition the handle on a circle around the
pivot (at the home radius — invariant under rotation) in the direction of the
mouse. See ``rotate_handle_ndc_following_mouse``.
"""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.composite.ui.viewer_handles import rotate_handle_ndc_following_mouse


def test_returns_home_position_when_not_following():
    """At rest (no rotate drag) the handle stays at its Fusion-style home."""
    home = (0.0, 0.7)
    pivot = (0.0, 0.0)
    mouse = (0.9, 0.0)

    pos = rotate_handle_ndc_following_mouse(home, pivot, mouse, follow=False)

    assert pos == home


def test_orbits_pivot_toward_mouse_at_home_radius():
    """During a rotate drag the handle swings toward the mouse angle."""
    # Home sits directly above the pivot at radius 0.7.
    home = (0.0, 0.7)
    pivot = (0.0, 0.0)
    # Mouse is far to the right of the pivot.
    mouse = (0.9, 0.0)

    pos = rotate_handle_ndc_following_mouse(home, pivot, mouse, follow=True)

    # Handle must swing to the RIGHT (toward the mouse angle), not stay up top
    # pointing at the top-middle.
    assert pos[0] > 0.5, (
        "rotate handle must follow the mouse angle, not stay glued to the top-middle"
    )
    # Radius preserved: it orbits on the home circle.
    r = math.hypot(pos[0] - pivot[0], pos[1] - pivot[1])
    assert abs(r - 0.7) < 1e-6


def test_orbit_is_purely_angular_not_distance_to_mouse():
    """The handle clamps to the home radius even if the mouse is far away."""
    home = (0.0, 0.5)
    pivot = (0.0, 0.0)
    # Mouse much farther than the home radius (distance ~7.07).
    mouse = (5.0, 5.0)

    pos = rotate_handle_ndc_following_mouse(home, pivot, mouse, follow=True)

    r = math.hypot(pos[0], pos[1])
    assert abs(r - 0.5) < 1e-6  # clamped to home radius, not dragged out to 7.07
    # Same direction as the mouse (45°): x == y.
    assert abs(pos[0] - pos[1]) < 1e-6


def test_mouse_at_pivot_falls_back_to_home():
    """Degenerate mouse (exactly on pivot) must not divide by zero."""
    home = (0.0, 0.7)
    pivot = (0.0, 0.0)
    mouse = (0.0, 0.0)

    pos = rotate_handle_ndc_following_mouse(home, pivot, mouse, follow=True)

    assert pos == home
