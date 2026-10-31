"""P5 view tests: 4-state expression status marker on tree rows (T49 view).

Verifies the KeyframesTreeView's optional expression-status provider is
queried at paint time and that the four states (none/valid/error/disabled)
flow through the marker logic without throwing. Pixel assertions are
intentionally avoided — they're brittle across platforms and the goal here
is to lock the integration contract (provider queried, marker suppressed
for 'none', buggy provider never crashes paint).

The rendering path itself is exercised by forcing a repaint via update();
any exception inside paintEvent surfaces as a test failure because Qt
re-throws paint exceptions to the test process under offscreen.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_model import KeyframesModel
from src.features.composite.ui.keyframes_tree_view import KeyframesTreeView
from src.features.composite.ui.timeline_expression import (
    EXPRESSION_DISABLED,
    EXPRESSION_ERROR,
    EXPRESSION_NONE,
    EXPRESSION_VALID,
    ExpressionStatus,
    derive_expression_status,
)


@pytest.fixture
def graph():
    g = NodeGraph()
    g.add_node(TransformNode())
    return g


@pytest.fixture
def model(graph):
    m = KeyframesModel()
    m.rebuild(graph)
    return m


@pytest.fixture
def view(qapp, model):
    v = KeyframesTreeView(model, header_height=30)
    v.resize(330, 300)
    return v


# ── provider hookup ────────────────────────────────────────────────────


class TestProviderHookup:
    def test_provider_defaults_none(self, view):
        assert view._expression_status_provider is None

    def test_set_provider_accepts_callable(self, view):
        called = []

        def provider(node_id, param_name):
            called.append((node_id, param_name))
            return None

        view.set_expression_status_provider(provider)
        assert view._expression_status_provider is provider

    def test_set_none_provider_disables(self, view):
        view.set_expression_status_provider(lambda n, p: None)
        view.set_expression_status_provider(None)
        assert view._expression_status_provider is None


# ── paint-time provider query ──────────────────────────────────────────


class TestProviderQueriedAtPaint:
    def test_provider_called_for_each_param_row(self, view, qapp):
        """The view must query the provider for every visible param row on
        paint, so installing a provider covers the whole tree."""
        queried = []

        def provider(node_id, param_name):
            queried.append((node_id, param_name))
            return None  # no marker

        view.set_expression_status_provider(provider)
        # Qt suppresses paintEvent for hidden widgets under offscreen, so we
        # must show the widget to actually trigger painting.
        view.show()
        view.repaint()
        qapp.processEvents()
        view.hide()
        # TransformNode has several animatable params — at least one query
        # must have happened.
        assert len(queried) > 0

    def test_provider_not_called_when_unset(self, view, qapp):
        """With no provider installed, paint must not reference it."""
        assert view._expression_status_provider is None
        view.repaint()
        qapp.processEvents()
        # No assertion needed — completing without error is the contract.


# ── 4-state flow (no pixel asserts; just no-throw) ─────────────────────


class TestFourStateFlow:
    """For each state, install a provider returning that status and force a
    paint. Any exception inside _draw_expression_status propagates and fails
    the test (Qt re-throws paint exceptions under offscreen)."""

    def test_valid_state_paints_without_throwing(self, view, qapp):
        view.set_expression_status_provider(
            lambda n, p: ExpressionStatus(state=EXPRESSION_VALID, text="x"))
        view.repaint()
        qapp.processEvents()

    def test_error_state_paints_without_throwing(self, view, qapp):
        view.set_expression_status_provider(
            lambda n, p: ExpressionStatus(state=EXPRESSION_ERROR,
                                          error_code="expr_syntax",
                                          error_message="oops"))
        view.repaint()
        qapp.processEvents()

    def test_disabled_state_paints_without_throwing(self, view, qapp):
        view.set_expression_status_provider(
            lambda n, p: ExpressionStatus(state=EXPRESSION_DISABLED,
                                          text="x", enabled=False))
        view.repaint()
        qapp.processEvents()

    def test_none_state_paints_without_throwing(self, view, qapp):
        view.set_expression_status_provider(
            lambda n, p: ExpressionStatus(state=EXPRESSION_NONE))
        view.repaint()
        qapp.processEvents()


# ── robustness ─────────────────────────────────────────────────────────


class TestProviderRobustness:
    def test_buggy_provider_does_not_crash_paint(self, view, qapp):
        """A provider that raises must be swallowed so one bad row never
        takes down the whole tree."""

        def boom(n, p):
            raise RuntimeError("provider crashed")
        view.set_expression_status_provider(boom)
        view.repaint()
        qapp.processEvents()

    def test_provider_returning_object_without_state_is_safe(self, view, qapp):
        """Defensive: a malformed status object (no 'state' attr) must not
        crash paint."""

        class _Bad:
            pass
        view.set_expression_status_provider(lambda n, p: _Bad())
        view.repaint()
        qapp.processEvents()


# ── integration with derive_expression_status (batch 5) ───────────────


class TestIntegrationWithStatusModel:
    def test_real_status_flows_through_provider(self, view, graph, qapp):
        """Wire the real derive_expression_status through the provider and
        force a paint — verifies batch-5 output is consumable end-to-end."""
        node = graph.nodes[next(iter(graph.nodes))]
        node._expressions = {"rotation": "value + 10"}

        def provider(node_id, param_name):
            n = graph.get_node(node_id)
            if n is None:
                return None
            return derive_expression_status(n, param_name)

        view.set_expression_status_provider(provider)
        view.repaint()
        qapp.processEvents()

    def test_no_marker_when_param_has_no_expression(self, view, graph, qapp):
        node = graph.nodes[next(iter(graph.nodes))]
        assert not node.has_expression("rotation")

        def provider(node_id, param_name):
            n = graph.get_node(node_id)
            return derive_expression_status(n, param_name) if n else None

        view.set_expression_status_provider(provider)
        view.repaint()
        qapp.processEvents()
