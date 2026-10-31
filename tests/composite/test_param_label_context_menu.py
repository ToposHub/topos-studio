"""Regression: right-clicking a parameter-name label must not crash.

Root cause: ``_ParamLabel.contextMenuEvent`` called
``event.globalPos().toPoint()``, but ``QContextMenuEvent.globalPos()`` (like
``QMouseEvent.globalPos()``) already returns a ``QPoint`` — and ``QPoint`` has no
``toPoint()`` (that is a ``QPointF`` method). Right-clicking any X/Y parameter
label therefore crashed with::

    AttributeError: 'QPoint' object has no attribute 'toPoint'.
                    Did you mean: 'toPointF'?

Fix: pass ``event.globalPos()`` straight to the menu builder (matches the other
two call sites: node_editor_widget.py and composite_viewport.py).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QContextMenuEvent

from src.features.composite.ui.node_properties_panel import _ParamLabel


def test_param_label_context_menu_passes_global_qpoint(qapp):
    """Right-clicking a _ParamLabel hands its context-menu builder the global
    QPoint without raising (regression for the toPoint() crash)."""
    label = _ParamLabel("scale")
    received = []
    label.set_context_menu_builder(lambda gp: received.append(gp))

    ev = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(5, 5), QPoint(120, 80))
    label.contextMenuEvent(ev)

    assert len(received) == 1
    # The builder receives the GLOBAL position, as a QPoint.
    assert received[0] == QPoint(120, 80)
