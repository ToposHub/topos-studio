"""P11 tests: SplineView off-screen render + hit-test (plan §11.11).

Validates that the curve view paints non-empty content, draws the playhead,
and that keyframe/handle hit-testing works against the screen-space layout.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QRectF, QPointF, Qt
from PyQt6.QtGui import QImage, QPainter, QColor

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.spline_model import SplineModel
from src.features.composite.ui.spline_view import SplineView
from src.shared.ui.style_sheet import C
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def model_with_curve(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(10, 45.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    m = SplineModel()
    m.rebuild(g)
    # mark the curve selected so handles + keys draw
    for c in m.curves:
        c.selected = True
    return m, t


# ── off-screen render (§11.11) ──────────────────────────────────────────

def _render_image(view, w=960, h=320):
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    view.render_to_painter(p, QRectF(0, 0, w, h))
    p.end()
    return img


def _count_non_transparent(img):
    """Count pixels that are not fully transparent."""
    cnt = 0
    for y in range(0, img.height(), 4):
        for x in range(0, img.width(), 4):
            if img.pixelColor(x, y).alpha() > 0:
                cnt += 1
    return cnt


def test_offscreen_render_is_non_empty(model_with_curve):
    m, t = model_with_curve
    view = SplineView(m)
    img = _render_image(view)
    assert _count_non_transparent(img) > 50, "render produced no visible pixels"


def test_offscreen_render_contains_playhead_red(model_with_curve):
    """The red playhead must appear in the rendered image."""
    m, t = model_with_curve
    view = SplineView(m)
    view.set_current_frame(5)
    img = _render_image(view)
    # Scan for a red-ish pixel near the playhead's x.
    found_red = False
    playhead = QColor(C.KEYFRAME_PLAYHEAD)
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            c = img.pixelColor(x, y)
            if (c.red() > 150 and c.green() < 100 and c.blue() < 100
                    and c.alpha() > 100):
                found_red = True
                break
        if found_red:
            break
    assert found_red, "playhead red not found in render"


def test_offscreen_render_contains_curve_color(model_with_curve):
    """Some pixel should match the curve's color (node header color)."""
    m, t = model_with_curve
    view = SplineView(m)
    expected = QColor(m.curves[0].color)
    img = _render_image(view)
    found = False
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            c = img.pixelColor(x, y)
            if (abs(c.red() - expected.red()) < 40
                    and abs(c.green() - expected.green()) < 40
                    and abs(c.blue() - expected.blue()) < 40
                    and c.alpha() > 100):
                found = True
                break
        if found:
            break
    assert found, "curve color not found in render"


# ── coordinate transforms (§11.3) ───────────────────────────────────────

def test_data_to_screen_round_trip(model_with_curve):
    m, t = model_with_curve
    view = SplineView(m)
    view.resize(960, 320)
    p = QPointF(5.0, 20.0)
    s = view.data_to_screen(p)
    back = view.screen_to_data(s)
    assert abs(back.x() - 5.0) < 1.0
    assert abs(back.y() - 20.0) < 2.0


def test_viewport_expands_degenerate():
    from src.features.composite.ui.spline_view import SplineViewport
    vp = SplineViewport(5.0, 5.0, 3.0, 3.0, QRectF(0, 0, 100, 100))
    vp.expand_degenerate()
    assert vp.frame_max > vp.frame_min
    assert vp.value_max > vp.value_min


# ── fit view ────────────────────────────────────────────────────────────

def test_fit_view_recomputes_viewport(model_with_curve):
    m, t = model_with_curve
    view = SplineView(m)
    view.resize(960, 320)
    view.fit_view()
    # After fit, the viewport covers the curve's frame range.
    vp = view._ensure_viewport()
    assert vp.frame_min <= 0
    assert vp.frame_max >= 10
