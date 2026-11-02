"""Scope layout switching, input routing and live floating-dock behavior."""
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QMenu, QWidget

from src.features.color.ui.scope_panel import ScopePanel, _MULTI_LAYOUTS, _MODE_PRESENTER_ATTR


@pytest.mark.parametrize('mode', ['split', 'quad', 'all', 'waveform', 'all', 'split'])
def test_visible_presenters_have_distinct_cells_after_switches(qtbot, mode):
    panel = ScopePanel()
    qtbot.addWidget(panel)
    panel.resize(960, 640)
    panel.show()
    for intermediate in ('all', 'quad', 'vectorscope', mode):
        panel.set_mode(intermediate)
    panel.layout().activate()
    qtbot.wait(10)
    modes = _MULTI_LAYOUTS.get(mode, (mode,))
    rects = []
    for key in modes:
        presenter = getattr(panel, _MODE_PRESENTER_ATTR[key])
        assert presenter.isVisibleTo(panel)
        rect = QRect(presenter.mapTo(panel, QPoint(0, 0)), presenter.size())
        assert rect.width() > 50 and rect.height() > 50
        assert all(not rect.intersects(other) for other in rects)
        rects.append(rect)


def test_viewer_open_floats_the_same_live_panel_and_redocks(qtbot):
    window = QMainWindow()
    qtbot.addWidget(window)
    dock = QDockWidget('Scopes', window)
    panel = ScopePanel()
    dock.setWidget(panel)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    window.show()
    panel.open_floating()
    assert dock.isFloating()
    assert dock.widget() is panel
    assert panel.current_mode == 'quad'
    assert panel.layout_toolbar.isVisible()
    panel.expand_btn.click()
    assert not dock.isFloating()
    assert dock.widget() is panel


def test_source_quad_updates_all_four_visible_cpu_analyses(qtbot):
    panel = ScopePanel()
    qtbot.addWidget(panel)
    panel.show()
    panel.set_scope_source('source')
    panel.set_mode('quad')
    image = np.ones((16, 16, 3), dtype=np.float32) * 0.4
    with patch.object(panel.parade_widget, 'update_parade') as parade, \
         patch.object(panel.waveform_widget, 'update_waveform') as waveform, \
         patch.object(panel.vectorscope_widget, 'update_vectorscope') as vector, \
         patch.object(panel.histogram_widget, 'update_histogram') as histogram:
        panel.update_scope(image, source='source')
        panel._process_pending_image()
        assert all(call.call_count == 1 for call in (parade, waveform, vector, histogram))


def test_header_settings_and_more_buttons_open_working_menus(qtbot):
    panel = ScopePanel()
    qtbot.addWidget(panel)
    with patch.object(QMenu, 'exec', return_value=None) as popup:
        panel.settings_btn.click()
        panel.more_btn.click()
        assert popup.call_count == 2


def test_viewer_context_menu_emits_scope_open_request(qtbot):
    from src.features.color.ui.canvas import Canvas
    from src.shared.i18n.i18n import I18N

    class ViewerHost(QWidget):
        scopes_requested = pyqtSignal()

        def fit_to_window(self) -> None:
            pass

    host = ViewerHost()
    qtbot.addWidget(host)

    def choose_scope(menu, position):
        actions = [action for action in menu.actions()
                   if action.text() == I18N.tr('workspace.scopes', 'Scopes')]
        assert len(actions) == 1
        actions[0].trigger()

    with qtbot.waitSignal(host.scopes_requested), patch.object(QMenu, 'exec', choose_scope):
        Canvas._show_image_context_menu(host, SimpleNamespace(globalPos=lambda: QPoint()))
