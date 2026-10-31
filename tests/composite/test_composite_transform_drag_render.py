"""Regression: transform-handle drag must re-render the image in real time.

Root cause discovered while optimizing the Composite Transform node's on-screen
control handles:

``CompositePage._flush_interaction_update`` applied drag params via
``node.set_parameter()`` called *directly on the node object*. That bumps the
node's ``param_revision`` but NOT ``NodeGraph._generation``. The renderer's
skip-if-same-signature logic (``composite_renderer._handle_evaluate_graph``)
keys on ``snapshot.generation``, so every drag-frame render was silently
skipped as "nothing changed" — the displayed image froze during the drag and
only updated on mouse release (when the PREVIEW→FULL mode flip broke the skip).

Fix: apply drag params through ``NodeGraph.set_node_parameter()``, which bumps
``_generation`` under the graph lock — the same path every other param change
in CompositePage already uses.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.composite_page import CompositePage


def test_flush_interaction_update_bumps_graph_generation(qapp):
    """Applying drag params through the flush path must bump graph generation.

    Without the bump the renderer treats each drag frame as unchanged and
    never re-renders, so the displayed image is frozen for the whole drag.
    """
    # Construct a real CompositePage (offscreen). __init__ does not create a GL
    # viewer (it's injected later), so this is safe without a GL context.
    page = CompositePage(parent=None)
    try:
        graph = page._node_graph  # NodeGraph created in __init__
        node = TransformNode(node_id="transform_1")
        graph.add_node(node)
        page._pending_interaction_update = {
            "node_id": "transform_1",
            "params": {"translate_x": 12.0, "translate_y": -8.0},
        }

        # Stub the heavy tail of the flush (render submission, panel/handle
        # sync). Stub both render entry points so the test is robust to
        # latency tuning of which one the flush calls.
        render_calls = []
        page._evaluate_graph = lambda: render_calls.append("debounced")
        page._evaluate_graph_now = lambda: render_calls.append("immediate")
        page._refresh_properties_panel = lambda node_id: None
        page.update_viewer_handles = lambda: None

        gen_before = graph.generation

        page._flush_interaction_update()

        gen_after = graph.generation
        _assert_drag_flush_bumped_generation(node, gen_before, gen_after, render_calls)
    finally:
        page.deleteLater()


def _assert_drag_flush_bumped_generation(node, gen_before, gen_after, render_calls):
    """Shared assertions for the drag-flush generation-bump regression."""
    # The drag params were applied to the node...
    assert node.parameters["translate_x"] == 12.0
    assert node.parameters["translate_y"] == -8.0
    # ...and the graph generation bumped so the renderer re-evaluates this
    # frame instead of short-circuiting on an unchanged signature.
    assert gen_after > gen_before, (
        "drag param flush must bump NodeGraph.generation; otherwise the "
        "renderer's same-signature skip freezes the image during drag"
    )
    # A render was triggered (one of the two entry points).
    assert len(render_calls) == 1


def test_nodegraph_set_node_parameter_bumps_generation():
    """Documents the invariant the flush path relies on.

    Direct ``node.set_parameter()`` does not bump the graph generation (it only
    touches per-node dirty/revision state); ``NodeGraph.set_node_parameter()``
    does. The drag flush must use the graph-level setter.
    """
    graph = NodeGraph()
    node = TransformNode(node_id="transform_1")
    graph.add_node(node)
    baseline = graph.generation

    # Direct node mutation — the buggy path — leaves generation unchanged.
    node.set_parameter("translate_x", 5.0)
    assert graph.generation == baseline

    # Graph-level setter — the fix — bumps generation.
    graph.set_node_parameter("transform_1", "translate_x", 7.0)
    assert graph.generation > baseline
    assert node.parameters["translate_x"] == 7.0


def test_parameter_changed_syncs_viewer_handles_live(qapp):
    """Live parameter edits (panel DragValue ticks) must refresh the on-canvas
    control handles on every tick, not only after the debounce flush.

    Regression: ``_on_parameter_changed`` re-rendered the image every tick but
    only called ``update_viewer_handles`` from the debounce flush — so the
    transform gizmo stayed frozen mid-drag and snapped into place on release.
    """
    page = CompositePage(parent=None)
    try:
        graph = page._node_graph
        node = TransformNode(node_id="transform_1")
        graph.add_node(node)

        handle_calls = []
        render_calls = []
        page._evaluate_graph = lambda: render_calls.append("render")
        page.update_viewer_handles = lambda: handle_calls.append("handles")

        # Two consecutive ticks of the same drag.
        page._on_parameter_changed("transform_1", "translate_x", 10.0)
        page._on_parameter_changed("transform_1", "translate_x", 20.0)
        # Keep the deferred debounce flush from firing after the assertions.
        page._param_debounce_timer.stop()

        # Image re-renders every tick...
        assert len(render_calls) == 2
        # ...and the handles follow every tick too (the bug: zero mid-drag).
        assert len(handle_calls) == 2
        assert node.parameters["translate_x"] == 20.0
    finally:
        page.deleteLater()


# ── Interaction hint flows into RenderRequest during drag ─────────────────

@pytest.mark.skipif(
    os.environ.get("QT_QPA_PLATFORM") == "offscreen",
    reason="drives the real render scheduler into GL texture work, which "
           "segfaults under the offscreen platform (QOpenGLWidget is not "
           "supported); run on a real desktop GL session (macOS release matrix)",
)
def test_interaction_hint_propagates_to_render_request_during_drag(qapp):
    """During a handle drag the RenderRequest must carry interaction_active=True.

    The hint tells the render thread to raise the eval-cache budget
    (CacheManager.begin_interaction) so per-frame parameter changes don't
    thrash the cache → texture release → real GL allocation jitter.

    Regression guard: _do_evaluate_graph reads CompositePage._interaction_active
    (set True on drag-start, False on release) and forwards it.
    """
    from src.features.composite.render.render_scheduler import RenderRequest

    page = CompositePage(parent=None)
    try:
        graph = page._node_graph
        node = TransformNode(node_id="transform_1")
        graph.add_node(node)

        submitted_requests = []

        # Capture the RenderRequest that _do_evaluate_graph builds and submits.
        def fake_submit(cmd, payload):
            req = payload.get('request')
            if isinstance(req, RenderRequest):
                submitted_requests.append(req)

        page._renderer = type("FakeRenderer", (), {"submit": staticmethod(fake_submit)})()
        page._is_active = True  # bypass the hidden-page skip in _do_evaluate_graph

        # --- Baseline: not dragging → hint False ---
        page._interaction_active = False
        page._do_evaluate_graph()
        assert len(submitted_requests) == 1
        assert submitted_requests[-1].interaction_active is False, (
            "non-drag frame must not carry interaction_active=True"
        )

        # --- Mid-drag: _interaction_active True → hint True ---
        page._interaction_active = True
        page._do_evaluate_graph()
        assert len(submitted_requests) == 2
        assert submitted_requests[-1].interaction_active is True, (
            "drag frame must carry interaction_active=True so the cache budget "
            "is raised and per-frame param changes don't trigger GL alloc jitter"
        )

        # --- After release: hint flips back to False ---
        page._interaction_active = False
        page._do_evaluate_graph()
        assert len(submitted_requests) == 3
        assert submitted_requests[-1].interaction_active is False
    finally:
        page.deleteLater()
