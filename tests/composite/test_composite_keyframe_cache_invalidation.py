"""P0-2 tests: frame-change cache invalidation + frame_number cache key.

Validates (plan §2 P0-2):
  - the scheduler marks frame-dependent nodes dirty when the frame changes
  - static (non-animated) nodes are NOT marked dirty on a frame change
  - the evaluator passes frame_number into compute_key for animated nodes
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.render.render_scheduler import (
    RenderScheduler, RenderPlan, NodePlan, _snapshot_node_needs_frame)
from src.features.composite.evaluators.base_evaluator import _node_needs_frame_cache
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


def _animated_graph():
    """static source(transform) → animated blur → output. Only blur is animated."""
    g = NodeGraph()
    t = TransformNode()
    b = BlurNode()
    track = KeyframeTrack(param_name="radius", min_val=0.0, max_val=100.0)
    track.add_keyframe(Keyframe(0, 1.0))
    track.add_keyframe(Keyframe(10, 8.0))
    b.set_keyframe_track("radius", track)
    out = OutputNode()
    g.add_node(t)
    g.add_node(b)
    g.add_node(out)
    g.connect(t.id, "result", b.id, "image")
    g.connect(b.id, "result", out.id, "image")
    return g, t, b, out


def _make_node_plans(snapshot):
    plans = {}
    for nid, ns in snapshot.nodes.items():
        plans[nid] = NodePlan(
            node_id=nid, node_type=ns.node_type,
            action="effect", params_hash="", inputs={})
    return plans


# ── _node_needs_frame_cache helper ──────────────────────────────────────

def test_animated_node_needs_frame_cache():
    g, t, b, out = _animated_graph()
    snap = g.snapshot(frame_number=0)
    ns_blur = snap.nodes[b.id]
    ns_static = snap.nodes[t.id]
    assert _node_needs_frame_cache(ns_blur) is True
    assert _node_needs_frame_cache(ns_static) is False


def test_snapshot_node_needs_frame_mirrors_evaluator():
    g, t, b, out = _animated_graph()
    snap = g.snapshot(frame_number=0)
    for ns in snap.nodes.values():
        assert _snapshot_node_needs_frame(ns) == _node_needs_frame_cache(ns)


def test_time_range_node_needs_frame_in_scheduler_and_evaluator():
    """A static node can still change output at a time-range boundary."""
    g = NodeGraph()
    transform = TransformNode()
    transform.time_range = {"enabled": True, "comp_start": 10,
                            "comp_end": 20}
    g.add_node(transform)
    ns = g.snapshot(frame_number=10).nodes[transform.id]
    assert _snapshot_node_needs_frame(ns) is True
    assert _node_needs_frame_cache(ns) is True


def test_context_animated_node_needs_frame_in_scheduler_and_evaluator():
    """Node-declared context animation must invalidate on playhead moves."""
    from src.features.composite.nodes.text_node import TextNode

    g = NodeGraph()
    text = TextNode()
    text.parameters["per_char_enable"] = True
    g.add_node(text)
    ns = g.snapshot(frame_number=0).nodes[text.id]
    assert _snapshot_node_needs_frame(ns) is True
    assert _node_needs_frame_cache(ns) is True


# ── scheduler frame dirty detection (via plan()) ────────────────────────

def test_frame_change_marks_animated_node_dirty():
    """When the frame changes, the animated blur + its downstream output must
    be dirty; the static transform must NOT be."""
    g, t, b, out = _animated_graph()
    sched = RenderScheduler()
    # Seed tracking with the first plan at frame 0.
    snap0 = g.snapshot(frame_number=0)
    sched.plan(snap0)

    # Now move to frame 5 and plan again.
    snap5 = g.snapshot(frame_number=5)
    plan5 = sched.plan(snap5)
    dirty = set(plan5.dirty_node_ids)

    assert b.id in dirty          # animated → dirty
    assert out.id in dirty        # downstream of blur → dirty
    assert t.id not in dirty      # static transform → NOT dirty


def test_same_frame_no_spurious_dirty():
    """Re-planning at the same frame does not mark animated nodes dirty."""
    g, t, b, out = _animated_graph()
    sched = RenderScheduler()
    snap = g.snapshot(frame_number=3)
    sched.plan(snap)
    # Re-plan at the same frame
    plan = sched.plan(snap)
    assert b.id not in plan.dirty_node_ids


def test_first_plan_marks_everything_dirty():
    sched = RenderScheduler()
    g, t, b, out = _animated_graph()
    snap = g.snapshot(frame_number=0)
    plan = sched.plan(snap)
    assert set(plan.node_plans.keys()) == set(plan.dirty_node_ids)


# ── compute_key receives frame_number ───────────────────────────────────

def test_compute_key_gets_frame_number_for_animated_node():
    """The evaluator must pass frame_number for animated nodes so their cache
    key varies per frame."""
    from unittest.mock import MagicMock
    from src.features.composite.render.cache_manager import CacheManager
    g, t, b, out = _animated_graph()
    snap = g.snapshot(frame_number=7)
    ns = snap.nodes[b.id]
    cm = CacheManager(MagicMock())
    key_a = cm.compute_key(ns, {}, frame_number=7)
    key_b = cm.compute_key(ns, {}, frame_number=8)
    assert key_a != key_b


def test_compute_key_no_frame_number_for_static_node():
    """Static nodes get frame_number=None so their key is frame-stable."""
    from unittest.mock import MagicMock
    from src.features.composite.render.cache_manager import CacheManager
    g, t, b, out = _animated_graph()
    snap = g.snapshot(frame_number=0)
    ns = snap.nodes[t.id]  # static transform
    cm = CacheManager(MagicMock())
    key0 = cm.compute_key(ns, {}, frame_number=None)
    key1 = cm.compute_key(ns, {}, frame_number=None)
    assert key0 == key1
