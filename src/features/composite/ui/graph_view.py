"""GraphCanvas host for the Keyframes panel's right side (T101).

A thin QWidget container holding a QStackedLayout that swaps between the
Timeline canvas (the existing KeyframesTimelineView) and the Graph canvas
(a future GraphEditorView, T105+). Plan §22.1 recommends this over a
single branching paintEvent because the two canvases have very different
hit-test + interaction models, and mixing them in one widget pollutes both.

Why a separate host:

* the Keyframes panel keeps ONE splitter, ONE tree, ONE footer, ONE shared
  selection model — only the right-hand canvas changes. Wrapping the swap
  in this host means the panel doesn't need to know which canvas is live;
* mode switches (T100) don't destroy the shared state: the same
  KeyframesModel, FrameMapper, current frame and selection feed both
  canvases;
* the host exposes a stable signal surface (current_frame_changed,
  track_changed, …) so the panel connects once, regardless of which
  canvas is visible.

The actual GraphEditorView (the QPainter canvas that draws value/speed
graphs) lands in T105-T109. Until then the graph slot holds a placeholder
widget so the stack always has two pages and the toggle is testable.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QLabel, QStackedLayout, QWidget
from PyQt6.QtCore import Qt

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


class GraphCanvasHost(QWidget):
    """Stacked container swapping Timeline ↔ Graph canvases.

    Signals are re-emitted from whichever canvas is current, so the panel
    wires up once. The Timeline canvas is always present (it's the
    existing KeyframesTimelineView); the Graph canvas is added when the
    host is built and shown/hidden via the stack index.
    """

    # Re-emitted from the active canvas.
    current_frame_changed = pyqtSignal(int)
    vertical_scroll_changed = pyqtSignal(int)
    # The Timeline canvas emits these directly; the Graph canvas (when it
    # lands) will too. Host re-emits so the panel doesn't care which is live.
    track_changed = pyqtSignal(str, str, list)
    range_changed = pyqtSignal(str, dict)
    play_range_changed = pyqtSignal(int, int)
    display_mode_changed = pyqtSignal(str)

    TIMELINE = "timeline"
    GRAPH = "graph"

    def __init__(self, timeline_view: QWidget,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("GraphCanvasHost")
        self._timeline_view = timeline_view
        self._graph_view: Optional[QWidget] = None
        self._view_mode: str = self.TIMELINE

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        # Page 0: the existing timeline canvas.
        self._stack.addWidget(timeline_view)
        # Page 1: the graph canvas placeholder. Replaced by GraphEditorView
        # when T105+ lands; for now a labelled empty state so the toggle
        # has somewhere to land.
        self._placeholder = self._build_placeholder()
        self._stack.addWidget(self._placeholder)
        self._stack.setCurrentIndex(0)

        self._forward_timeline_signals()

    # ── public API ──

    @property
    def view_mode(self) -> str:
        return self._view_mode

    @property
    def timeline_view(self) -> QWidget:
        return self._timeline_view

    @property
    def graph_view(self) -> Optional[QWidget]:
        """The graph canvas (None until T105+ installs a real one)."""
        return self._graph_view

    def set_graph_view(self, graph_view: Optional[QWidget]) -> None:
        """Install the real Graph Editor canvas (T105+).

        Until this is called the graph slot shows a placeholder. Passing
        None reverts to the placeholder. The host does NOT take ownership
        semantics beyond reparenting — the caller may keep its own
        reference for teardown.
        """
        self._graph_view = graph_view
        if graph_view is None:
            # AUD-9916: destroy the outgoing placeholder exactly like the
            # install path below does — removing it from the layout alone
            # left the old QLabel parented to (and hidden inside) the host.
            if self._placeholder is not None:
                self._stack.removeWidget(self._placeholder)
                self._placeholder.setParent(None)
                self._placeholder.deleteLater()
            self._placeholder = self._build_placeholder()
            self._stack.insertWidget(1, self._placeholder)
            if self._view_mode == self.GRAPH:
                self._stack.setCurrentIndex(1)
            return
        # Replace the placeholder at index 1 with the real canvas.
        if self._placeholder is not None:
            self._stack.removeWidget(self._placeholder)
            self._placeholder.setParent(None)
            self._placeholder.deleteLater()
            self._placeholder = None
        graph_view.setParent(self)
        self._stack.insertWidget(1, graph_view)
        # AUD-9917: the class docstring promises signal re-emission from
        # whichever canvas is current — wire the graph canvas's signals
        # through the host surface too, not just the timeline's.
        self._forward_canvas_signals(graph_view)
        if self._view_mode == self.GRAPH:
            self._stack.setCurrentIndex(1)

    def set_view_mode(self, mode: str) -> None:
        """Switch the visible canvas. 'timeline' or 'graph'.

        Does NOT touch the shared state (selection, current frame, work
        area) — that's the whole point of the stack vs. rebuilding (plan
        §22.1 '模式切换不销毁 shared model'). An unknown mode is a no-op.
        """
        if mode not in (self.TIMELINE, self.GRAPH):
            return
        if mode == self._view_mode:
            return
        self._view_mode = mode
        self._stack.setCurrentIndex(0 if mode == self.TIMELINE else 1)

    # ── internals ──

    def _build_placeholder(self) -> QWidget:
        """The empty-state shown when Graph mode has no real canvas yet."""
        w = QLabel(self)
        w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        w.setText(I18N.tr(
            "keyframes.graph_editor_placeholder",
            "Graph Editor mode\n(select an animated property)"))
        w.setStyleSheet(
            f"color: {C.TEXT_HINT}; background-color: {C.BG_DARKEST};")
        return w

    def _forward_timeline_signals(self) -> None:
        """Re-emit the Timeline canvas's signals through the host.

        The Graph canvas (installed via set_graph_view) gets the same
        treatment in :meth:`_forward_canvas_signals`; the panel connects to
        the host, not to either canvas directly.
        """
        self._forward_canvas_signals(self._timeline_view)

    def _forward_canvas_signals(self, view: Optional[QWidget]) -> None:
        """Re-emit ``view``'s signals through the host's stable surface.

        AUD-9917: applies to BOTH canvases — the docstring promises signal
        re-emission from whichever canvas is current, but only the timeline
        was ever wired. Be defensive: some canvas builds may not expose
        every signal, so connect only what exists (a Qt signal→signal
        connection re-emits with the same payload).
        """
        if view is None:
            return
        # Idempotence guard: installing the same canvas object twice must
        # not double-bind its signals (Qt re-emits once per connection).
        forwarded_ids = getattr(self, "_forwarded_canvas_ids", None)
        if forwarded_ids is None:
            forwarded_ids = set()
            self._forwarded_canvas_ids = forwarded_ids
        if id(view) in forwarded_ids:
            return
        for sig_name, host_sig in (
            ("current_frame_changed", self.current_frame_changed),
            ("vertical_scroll_changed", self.vertical_scroll_changed),
            ("track_changed", self.track_changed),
            ("range_changed", self.range_changed),
            ("play_range_changed", self.play_range_changed),
            ("display_mode_changed", self.display_mode_changed),
        ):
            sig = getattr(view, sig_name, None)
            if sig is None or not hasattr(sig, "connect"):
                continue
            try:
                sig.connect(host_sig)
            except (TypeError, RuntimeError):
                # Signature mismatch / already-destroyed canvas — the host
                # surface simply won't carry that signal for this canvas.
                continue
        forwarded_ids.add(id(view))

    # ── pass-throughs used by the panel ──

    def refresh(self) -> None:
        """Repaint whichever canvas is current."""
        if self._view_mode == self.GRAPH and self._graph_view is not None:
            if hasattr(self._graph_view, "refresh"):
                self._graph_view.refresh()
            else:
                self._graph_view.update()
        else:
            if hasattr(self._timeline_view, "refresh"):
                self._timeline_view.refresh()
            else:
                self._timeline_view.update()

    def set_current_frame(self, frame: int) -> None:
        """Push the current frame to whichever canvas is visible."""
        for view in (self._timeline_view, self._graph_view):
            if view is None:
                continue
            setter = getattr(view, "set_current_frame", None)
            if callable(setter):
                setter(frame)
