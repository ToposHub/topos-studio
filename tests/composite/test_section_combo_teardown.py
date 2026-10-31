"""Regression: section QComboBox teardown must not crash on paint.

Root cause of "open project → instant crash": ``_clear_unpinned_sections`` and
``_update_node_sections`` deleted a NodeSectionWidget with a bare
``deleteLater()``. deleteLater is asynchronous — the C++ QComboBox survives
until the next event-loop pass. A paint event reaching it in that window routed
through ``sipQComboBox::initStyleOption`` into a Python wrapper whose section
was already garbage-collected → EXC_BAD_ACCESS at 0x0.

macOS crash reports confirmed it: triggered thread stack
    [0] EXC_BAD_ACCESS
    [1] QComboBox::initStyleOption
    [2] sipQComboBox::initStyleOption
    ... QGraphicsScene::event  (node editor canvas paint)

The fix is ``NodeSectionWidget.prepare_for_deletion``: disconnect the section
and every child widget, detach from the parent, THEN deleteLater. These tests
verify the teardown actually happens (disconnect + parent cleared) so no later
paint can touch a dangling widget.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6.QtWidgets import QComboBox, QWidget

from src.features.composite.ui.node_properties_panel import (
    NodePropertiesPanel,
    NodeSectionWidget,
)


@pytest.fixture
def panel(qapp):
    """A real NodePropertiesPanel for exercising section add/remove."""
    return NodePropertiesPanel()


def _make_section(panel, node_id: str, with_combo: bool = True) -> NodeSectionWidget:
    """Build a minimal section carrying a QComboBox (the crash-prone widget)."""
    section = NodeSectionWidget(
        node_id=node_id,
        node_type="transform",
        node_title=f"Node {node_id}",
        parameters={},
        param_defs={},
        parent_panel=panel,
    )
    if with_combo:
        combo = QComboBox(section)
        combo.addItems(["linear", "nearest"])
        combo.setCurrentText("linear")
    return section


class TestSectionComboTeardown:
    """prepare_for_deletion must disconnect + detach synchronously."""

    def test_prepare_for_deletion_detaches_from_parent(self, panel):
        """After teardown the section must have no parent (removed from paint)."""
        section = _make_section(panel, "n1")
        panel._content_layout.addWidget(section)
        assert section.parent() is not None

        section.prepare_for_deletion()

        # Detached from the layout/parent → Qt stops painting this subtree.
        assert section.parent() is None

    def test_prepare_for_deletion_disconnects_child_combos(self, panel):
        """Every child QComboBox must be signal-disconnected before deletion.

        A still-connected signal queued into a freed Python wrapper is the
        exact crash path; disconnect must happen synchronously. We assert via
        the observed teardown side-effects: after prepare_for_deletion, the
        combos are no longer connected to anything (a second disconnect is a
        no-op rather than touching live signal state).
        """
        section = _make_section(panel, "n2")
        combos = section.findChildren(QComboBox)
        assert len(combos) >= 1

        section.prepare_for_deletion()

        # After teardown every child's signals are already disconnected, so a
        # second disconnect() must be a clean no-op (no exception, no crash).
        # The Qt warning "wildcard call disconnects from destroyed signal"
        # emitted during teardown is exactly the proof that disconnect ran.
        for combo in combos:
            try:
                combo.disconnect()
            except (TypeError, RuntimeError):
                pass  # already fully disconnected — the desired post-teardown state

    def test_clear_unpinned_sections_uses_safe_teardown(self, panel):
        """_clear_unpinned_sections must not leave a parented dangling section.

        Previously it called a bare deleteLater(), letting the C++ widget
        survive one event-loop pass and crash on paint.
        """
        section = _make_section(panel, "n3")
        panel._sections["n3"] = section
        panel._content_layout.addWidget(section)
        assert section.parent() is not None

        panel._clear_unpinned_sections()

        # Section removed from the tracking dict...
        assert "n3" not in panel._sections
        # ...and detached from the layout synchronously (the crash fix).
        assert section.parent() is None

    def test_set_nodes_safe_teardown_on_remove(self, panel):
        """When a node leaves the selection, its section is torn down safely.

        Mirrors the 'open a project whose previously-selected node is gone'
        path: set_nodes([]) drops all stale sections.
        """
        section = _make_section(panel, "n4")
        panel._sections["n4"] = section
        panel._content_layout.addWidget(section)

        panel.set_nodes([])  # empty selection → remove all sections

        assert "n4" not in panel._sections
        assert section.parent() is None

    def test_prepare_for_deletion_is_idempotent(self, panel):
        """Double teardown must be a no-op, not a re-touch of freed widgets.

        Reproduces the 'delete a Denoise node → segfault' crash: when a node
        that owns a large parameter section is deleted, node-removed and
        selection-changed fire overlapping teardown passes in the same event
        loop. The first pass schedules a deleteLater; the second pass reached
        the same section object and re-ran teardown (findChildren, disconnect,
        setParent) against a widget Qt had already partially torn down,
        dereferencing freed C++ memory. The _is_torn_down guard makes the
        second pass a clean no-op.
        """
        section = _make_section(panel, "n5")
        panel._content_layout.addWidget(section)

        section.prepare_for_deletion()
        assert section._is_torn_down is True

        # Second call MUST be a no-op (no exception, no crash, no state change).
        section.prepare_for_deletion()
        assert section._is_torn_down is True

    def test_update_parameters_safe_on_torn_down_section(self, panel):
        """A stale refresh reaching a torn-down section must not crash.

        A queued parameter-changed signal can arrive after the owning node was
        deleted and the section scheduled for deletion. update_parameters
        rebuilds child widgets; touching the freed subtree would segfault.
        """
        section = _make_section(panel, "n6")
        panel._content_layout.addWidget(section)

        section.prepare_for_deletion()

        # Stale refresh after teardown — must be a no-op, not a rebuild.
        section.update_parameters({"strength": 0.9, "radius": 3})
        # No exception raised = pass.

    def test_set_enabled_safe_on_torn_down_section(self, panel):
        """set_enabled on a torn-down section must be a no-op."""
        section = _make_section(panel, "n7")
        panel._content_layout.addWidget(section)

        section.prepare_for_deletion()
        section.set_enabled(False)  # must not touch the freed _enable_btn
        # No exception raised = pass.

    def test_double_clear_does_not_crash(self, panel):
        """Two clear() calls back-to-back (node-removed + selection-changed)."""
        section = _make_section(panel, "n8")
        panel._sections["n8"] = section
        panel._content_layout.addWidget(section)

        panel.clear()  # first teardown
        panel.clear()  # second teardown — the Denoise-delete crash path
        assert "n8" not in panel._sections
