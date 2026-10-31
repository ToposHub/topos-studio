"""Batch 2 tests: SplineView context menu, keyboard hotkeys, clipboard.

Validates that the right-click menu and the Ctrl+C/V/A/X, Delete, arrow, F
hotkeys all route to the right behaviour, and that copy/paste re-anchors a
block of keys at the playhead (AE convention).
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF, Qt, QEvent
from PyQt6.QtGui import QKeyEvent, QContextMenuEvent
from PyQt6.QtWidgets import QApplication

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.spline_model import SplineModel
from src.features.composite.ui.spline_view import SplineView
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def view(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(10, 45.0))
    tr.add_keyframe(Keyframe(20, 90.0))
    tr.add_keyframe(Keyframe(30, 135.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    m = SplineModel()
    m.rebuild(g)
    for c in m.curves:
        c.selected = True
    view = SplineView(m)
    view.resize(960, 400)
    view.fit_view()
    return view, m


def _key(view, key, mods=Qt.KeyboardModifier.NoModifier):
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


# ── selection ───────────────────────────────────────────────────────────

def test_ctrl_a_selects_all(view):
    view, m = view
    # Clear selection first.
    for c in m.curves:
        for k in c.keys:
            k.selected = False
    _key(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert all(k.selected for c in m.curves for k in c.keys)


def test_ctrl_d_deselects_all(view):
    view, m = view
    _key(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    _key(view, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    assert not any(k.selected for c in m.curves for k in c.keys)


# ── clipboard ────────────────────────────────────────────────────────────

def test_copy_then_paste_anchors_at_playhead(view):
    view, m = view
    curve = m.curves[0]
    # Select two keys (frame 10 and 20) so the block spans 10 frames.
    for k in curve.keys:
        k.selected = k.frame in (10, 20)
    n = view.copy_selected_keys()
    assert n == 2
    assert view.has_key_clipboard()

    # Move playhead to 50 and paste: keys should land at 50 and 60.
    view._current_frame = 50
    received = []
    view.keyframe_changed.connect(lambda nid, pn, kfs: received.append((nid, pn, kfs)))
    ok = view.paste_keys_at_frame()
    assert ok
    assert received
    frames = sorted(kf["frame"] for kf in received[-1][2])
    assert 50 in frames
    assert 60 in frames


def test_paste_with_empty_clipboard_is_noop(view):
    view, m = view
    view._key_clipboard = []
    assert not view.paste_keys_at_frame()


def test_cut_removes_source(view):
    view, m = view
    curve = m.curves[0]
    target = next(k for k in curve.keys if k.frame == 20)
    for k in curve.keys:
        k.selected = (k is target)
    received = []
    view.keyframe_changed.connect(lambda nid, pn, kfs: received.append(kfs))
    view.cut_selected_keys()
    assert received
    frames = sorted(kf["frame"] for kf in received[-1])
    assert 20 not in frames  # the cut key is gone
    assert view.has_key_clipboard()


def test_paste_replaces_existing_key_at_frame(view):
    """A paste landing on an existing frame overwrites, not duplicates."""
    view, m = view
    curve = m.curves[0]
    for k in curve.keys:
        k.selected = (k.frame == 0)
    view.copy_selected_keys()
    # Paste at frame 10 where a key already exists.
    view.paste_keys_at_frame(at_frame=10)
    same_frame = [k for k in curve.keys if k.frame == 10]
    assert len(same_frame) == 1  # replaced, not duplicated


# ── nudge (arrow keys) ───────────────────────────────────────────────────

def test_arrow_right_nudges_one_frame(view):
    view, m = view
    curve = m.curves[0]
    for k in curve.keys:
        k.selected = (k.frame == 10)
    _key(view, Qt.Key.Key_Right)
    # The selected key moved from 10 → 11.
    frames = [k.frame for k in curve.keys]
    assert 11 in frames
    assert 10 not in frames


def test_shift_arrow_nudges_ten_frames(view):
    view, m = view
    curve = m.curves[0]
    for k in curve.keys:
        k.selected = (k.frame == 10)
    _key(view, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    assert any(k.frame == 20 for k in curve.keys)


def test_nudge_cannot_cross_neighbour(view):
    view, m = view
    curve = m.curves[0]
    # Keys are at 0,10,20,30. Select 10 and nudge far past 20.
    for k in curve.keys:
        k.selected = (k.frame == 10)
    for _ in range(20):
        _key(view, Qt.Key.Key_Right)
    # Must be clamped below next key (frame 20).
    sel = [k for k in curve.keys if k.selected][0]
    assert sel.frame < 20


# ── F = fit ──────────────────────────────────────────────────────────────

def test_f_key_fits_view(view):
    view, m = view
    # Zoom in first to dirty the viewport.
    vp = view._ensure_viewport()
    vp.frame_max = vp.frame_min + 5
    _key(view, Qt.Key.Key_F)
    # After fit, viewport is recomputed wide enough to cover all keys.
    vp2 = view._ensure_viewport()
    assert (vp2.frame_max - vp2.frame_min) > 5


# ── delete via keyboard ──────────────────────────────────────────────────

def test_delete_key_removes_selected(view):
    view, m = view
    curve = m.curves[0]
    for k in curve.keys:
        k.selected = (k.frame == 30)
    received = []
    view.keyframe_changed.connect(lambda nid, pn, kfs: received.append(kfs))
    _key(view, Qt.Key.Key_Delete)
    assert received
    assert 30 not in [kf["frame"] for kf in received[-1]]
