"""Detachable panel host — embed a panel in a splitter *or* float it as a dock.

The video batch panel has two conflicting requirements (plan §3.2):

1. By default it sits at the bottom of the Deliver page (inside a
   ``QSplitter``).
2. It must also be able to float as an independent window or dock into any
   area of the main window.

A ``QDockWidget`` cannot be nested inside another ``QDockWidget`` (Qt
forbids it and the result is buggy), so we can't just make the batch
panel itself a dock. Instead this host owns the panel widget and moves it
between two parents:

* **Embedded mode** — the panel's parent is a ``QSplitter`` slot (the
  Deliver workspace). The host tracks the splitter index so it can be
  re-inserted at the same position.
* **Floating/docked mode** — the panel's parent becomes a freshly created
  ``QDockWidget`` (``objectName = "dockVideoBatchExport"``) which is
  ``addDockWidget``-ed to the main window. The dock is floatable and
  allowed in all areas.

The host remembers geometry / area / floating state via the same
ConfigManager keys the rest of the dock system uses
(``dock_video_batch_export_*``) so layout save/restore "just works".
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDockWidget,
    QMainWindow,
    QSplitter,
    QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.panel_divider import PanelDockWidget

logger = logging.getLogger(__name__)

__all__ = ["DetachablePanelHost"]


_DOCK_OBJECT_NAME = "dockVideoBatchExport"


class DetachablePanelHost(QObject):
    """Coordinates a panel between splitter-embedded and dock-floating modes.

    The host does not create the panel — the workspace creates both the
    panel and the splitter, then hands them to :meth:`attach`. This keeps
    the host reusable for any future detachable panel.

    Signals:
        mode_changed(str): emitted with ``"embedded"`` or ``"floating"``
            after a successful transition. The workspace listens to update
            menu checkmarks / layout state.
    """

    mode_changed = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._panel: Optional[QWidget] = None
        self._splitter: Optional[QSplitter] = None
        self._splitter_index: int = -1
        self._main_window: Optional[QMainWindow] = None
        self._dock: Optional[QDockWidget] = None
        self._mode: str = "embedded"
        # Deferred "close ≡ return to splitter" (AUD-12215). A 0 ms
        # single-shot timer owned by this host: it cannot fire on a
        # deleted host and runs only after the dock's close event has
        # fully completed.
        self._dock_close_return = QTimer(self)
        self._dock_close_return.setSingleShot(True)
        self._dock_close_return.setInterval(0)
        self._dock_close_return.timeout.connect(self._on_dock_close_return)
        # AUD-12216：restore_state 的浮窗调度同样用父属定时器——宿主在
        # tick 前销毁时定时器随之销毁，不对已删 wrapper 调用 float_panel。
        self._float_on_restore = QTimer(self)
        self._float_on_restore.setSingleShot(True)
        self._float_on_restore.setInterval(0)
        self._float_on_restore.timeout.connect(self.float_panel)

    # ── public state ──

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def dock(self) -> Optional[QDockWidget]:
        return self._dock

    # ── attach / detach lifecycle ──

    def attach(
        self,
        panel: QWidget,
        splitter: QSplitter,
        main_window: QMainWindow,
    ) -> None:
        """Bind the host to a panel + splitter + main window.

        After this call the panel is in embedded mode at its current
        splitter index. Use :meth:`float_panel` / :meth:`return_to_splitter`
        to move it.
        """
        self._panel = panel
        self._splitter = splitter
        self._main_window = main_window
        # Remember where the panel lives so we can put it back exactly.
        try:
            self._splitter_index = splitter.indexOf(panel)
        except Exception:  # noqa: BLE001
            self._splitter_index = splitter.count() - 1
        if self._splitter_index < 0:
            self._splitter_index = max(0, splitter.count() - 1)
        self._mode = "embedded"

    # ── mode transitions ──

    def float_panel(
        self,
        area: Optional[Qt.DockWidgetArea] = None,
        floating: bool = True,
    ) -> bool:
        """Move the panel out of the splitter into a dock widget.

        Args:
            area: dock area to occupy (TopDockWidgetArea / LeftDockWidgetArea /
                RightDockWidgetArea / BottomDockWidgetArea). When ``None``, the
                dock is created floating in its own window.
            floating: when ``area`` is None this is ignored (always floating).
                When ``area`` is given, ``True`` keeps it as a floating
                window anchored to that area, ``False`` docks it.

        Returns:
            ``True`` on success, ``False`` if the panel is already floating
            or prerequisites aren't met.
        """
        if self._panel is None or self._main_window is None or self._splitter is None:
            return False
        if self._mode == "floating" and self._dock is not None:
            # Already floating — update area/floating instead of recreating.
            if area is not None:
                self._main_window.addDockWidget(area, self._dock)
            # AUD-12214: the old ``floating and area is None or area is None``
            # parsed as ``(floating and area is None) or (area is None)`` so
            # the ``floating`` argument was ignored whenever an area was
            # given (the dock was always docked). Semantics per the
            # docstring: no area → always floating; with an area → honour
            # ``floating``.
            self._dock.setFloating(bool(floating) or area is None)
            return True

        # Remember the splitter position before removing the panel so
        # :meth:`return_to_splitter` can restore it precisely.
        try:
            self._splitter_index = self._splitter.indexOf(self._panel)
        except Exception:  # noqa: BLE001
            self._splitter_index = max(0, self._splitter.count() - 1)

        # Detach from the splitter without destroying the widget.
        self._panel.setParent(None)

        # Build the dock widget (re-use an existing one if present — shouldn't happen)
        self._dock = self._build_dock(self._panel)
        self._dock.setWidget(self._panel)

        if area is None:
            # Floating window
            self._dock.setFloating(True)
            self._main_window.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self._dock)
            self._dock.setFloating(True)
        else:
            self._dock.setFloating(floating)
            self._main_window.addDockWidget(area, self._dock)

        self._mode = "floating"
        self.mode_changed.emit("floating")
        return True

    def return_to_splitter(self) -> bool:
        """Move the panel back into its original splitter slot.

        Returns ``True`` on success, ``False`` if the panel is already
        embedded (no-op) or no dock exists.
        """
        if self._panel is None or self._splitter is None:
            return False
        if self._mode == "embedded":
            return True  # already there
        if self._dock is None:
            return False

        # Reparent the panel back to the splitter before tearing down the
        # dock, so the panel never loses its parent (avoids a flash of
        # top-level window). Stop watching the dock first: it is about to
        # be hidden and deleted.
        self._dock.removeEventFilter(self)
        self._dock.setWidget(None)
        insert_at = max(0, min(self._splitter_index, self._splitter.count()))
        self._splitter.insertWidget(insert_at, self._panel)

        # Remove the dock from the main window and delete it.
        self._main_window.removeDockWidget(self._dock)  # type: ignore[union-attr]
        self._dock.deleteLater()
        self._dock = None

        self._mode = "embedded"
        self.mode_changed.emit("embedded")
        return True

    def toggle(self) -> None:
        """Switch mode (embedded ↔ floating) — convenience for menu actions."""
        if self._mode == "embedded":
            self.float_panel()
        else:
            self.return_to_splitter()

    # ── persistence hooks ──
    #
    # The dock_layout_controller already saves / restores dock geometry &
    # area for any QDockWidget that is added to the main window and has a
    # stable objectName. By giving our dock a stable name we get save /
    # restore for free while it exists. For the embedded↔floating
    # preference across restarts we save a single boolean via the config
    # manager (the workspace reads it on startup and re-floats if needed).

    def save_state(self, config_manager: Any) -> None:
        """Persist the current mode (and dock geometry if floating)."""
        try:
            config_manager.set("dock_video_batch_export_mode", self._mode)
            if self._dock is not None:
                # Let QMainWindow.saveState() capture the dock geometry —
                # we just record whether it was floating.
                config_manager.set("dock_video_batch_export_floating", bool(self._dock.isFloating()))
        except Exception as e:  # noqa: BLE001 — persistence must never break the UI
            logger.warning("DetachablePanelHost.save_state failed: %s", e)

    def restore_state(self, config_manager: Any) -> None:
        """Restore the saved mode (call after :meth:`attach`)."""
        try:
            mode = config_manager.get("dock_video_batch_export_mode", "embedded")
        except Exception:  # noqa: BLE001
            mode = "embedded"
        if mode == "floating":
            # Default to a floating window on restore; the dock's geometry
            # is re-applied by the main window's restoreState() once the
            # dock exists. We schedule the float on the next event loop
            # tick so the workspace has a chance to finish layout.
            # AUD-12216：父属单发定时器（见 __init__）——宿主在 tick 前
            # 销毁时定时器随之销毁，不对已删 wrapper 调用 float_panel。
            self._float_on_restore.start()

    # ── internals ──

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        """Watch the dock for user closes (AUD-12215).

        Closing the floating dock means "return to the splitter": the host
        itself now owns that behaviour, so the panel is never stranded
        inside a closed dock. The return is deferred by one event-loop
        tick so the dock finishes its own close handling before we
        reparent the panel and tear the dock down. (``visibilityChanged``
        is deliberately NOT used — Qt emits transient False values during
        ``addDockWidget``/``setFloating`` transitions, which would rip the
        panel out of the dock on every area change.)
        """
        if obj is self._dock and event.type() == QEvent.Type.Close:
            self._dock_close_return.start()
        return super().eventFilter(obj, event)

    def _on_dock_close_return(self) -> None:
        """Timer slot: finish a dock close by re-embedding the panel."""
        if self._mode != "floating" or self._dock is None:
            return
        try:
            self.return_to_splitter()
        except RuntimeError as e:
            # The C++ side may already be gone during window teardown; a
            # Qt slot must never let an exception escape.
            logger.warning(
                "DetachablePanelHost: return-to-splitter on dock close failed: %s", e
            )

    def _build_dock(self, panel: QWidget) -> QDockWidget:
        """Create the QDockWidget wrapper for the panel.

        We deliberately keep the dock minimal — no custom title bar. The
        project's global QSS hides the native title bar anyway, and the
        FloatingDockTitleBar installed by the dock layout controller will
        take over when the dock becomes floating.
        """
        title = I18N.tr("panel.deliver_video_batch", "Video Batch Export")
        dock = PanelDockWidget(title, self._main_window)
        dock.setObjectName(_DOCK_OBJECT_NAME)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.TopDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        # Closing the floating dock returns the panel to the splitter: the
        # host watches the dock for QEvent.Close (see eventFilter) and
        # reparents the panel back into its embedded slot, so the panel is
        # never left hidden inside a closed dock.
        dock.installEventFilter(self)
        dock.setMinimumHeight(220)
        dock.setMinimumWidth(520)
        return dock
