"""Tests for the detachable panel host (float / dock / return).

Verifies the host can:
* Move a panel from a splitter into a floating QDockWidget.
* Move it back to the splitter at the original index.
* Toggle between modes.
* Not create nested docks.
* Preserve the panel widget instance throughout transitions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDockWidget, QMainWindow, QSplitter, QWidget

from src.features.deliver.ui.detachable_panel_host import DetachablePanelHost


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def window(qapp):
    win = QMainWindow()
    # The main window needs a central widget for dock areas to work
    win.setCentralWidget(QWidget())
    yield win
    win.close()


class _StubPanel(QWidget):
    """Stand-in for the batch panel — just needs to be a QWidget."""


class TestDetachablePanelHost:
    def test_initial_attach_is_embedded(self, window):
        host = DetachablePanelHost(parent=window)
        splitter = QSplitter(Qt.Orientation.Vertical)
        panel = _StubPanel()
        splitter.addWidget(panel)
        splitter.addWidget(QWidget())
        host.attach(panel, splitter, window)
        assert host.mode == "embedded"
        assert host.dock is None

    def test_float_creates_dock(self, window):
        host = DetachablePanelHost(parent=window)
        splitter = QSplitter(Qt.Orientation.Vertical)
        panel = _StubPanel()
        splitter.addWidget(panel)
        splitter.addWidget(QWidget())
        host.attach(panel, splitter, window)

        ok = host.float_panel()
        assert ok
        assert host.mode == "floating"
        assert host.dock is not None
        assert isinstance(host.dock, QDockWidget)
        assert host.dock.objectName() == "dockVideoBatchExport"
        # The panel should now live inside the dock
        assert panel.parent() is host.dock or host.dock.widget() is panel

    def test_return_to_splitter_restores_embedded(self, window):
        host = DetachablePanelHost(parent=window)
        splitter = QSplitter(Qt.Orientation.Vertical)
        panel = _StubPanel()
        splitter.addWidget(panel)
        splitter.addWidget(QWidget())
        window.setCentralWidget(splitter)
        host.attach(panel, splitter, window)
        original_index = splitter.indexOf(panel)

        host.float_panel()
        assert host.mode == "floating"
        ok = host.return_to_splitter()
        assert ok
        assert host.mode == "embedded"
        assert host.dock is None
        # Panel must be back in the splitter
        assert splitter.indexOf(panel) >= 0

    def test_toggle_switches_mode(self, window):
        host = DetachablePanelHost(parent=window)
        splitter = QSplitter(Qt.Orientation.Vertical)
        panel = _StubPanel()
        splitter.addWidget(panel)
        window.setCentralWidget(splitter)
        host.attach(panel, splitter, window)

        host.toggle()
        assert host.mode == "floating"
        host.toggle()
        assert host.mode == "embedded"

    def test_panel_instance_preserved(self, window):
        """The same QWidget instance must survive transitions (no rebuild)."""
        host = DetachablePanelHost(parent=window)
        splitter = QSplitter(Qt.Orientation.Vertical)
        panel = _StubPanel()
        splitter.addWidget(panel)
        window.setCentralWidget(splitter)
        host.attach(panel, splitter, window)
        original_id = id(panel)

        host.float_panel()
        assert id(host.dock.widget()) == original_id
        host.return_to_splitter()
        assert id(panel) == original_id

    def test_double_float_is_idempotent(self, window):
        host = DetachablePanelHost(parent=window)
        splitter = QSplitter(Qt.Orientation.Vertical)
        panel = _StubPanel()
        splitter.addWidget(panel)
        window.setCentralWidget(splitter)
        host.attach(panel, splitter, window)

        host.float_panel()
        first_dock = host.dock
        # Calling float again should not destroy / recreate the dock
        host.float_panel()
        assert host.dock is first_dock

    def test_return_when_already_embedded_is_noop(self, window):
        host = DetachablePanelHost(parent=window)
        splitter = QSplitter(Qt.Orientation.Vertical)
        panel = _StubPanel()
        splitter.addWidget(panel)
        host.attach(panel, splitter, window)

        ok = host.return_to_splitter()
        assert ok is True
        assert host.mode == "embedded"
        assert host.dock is None
