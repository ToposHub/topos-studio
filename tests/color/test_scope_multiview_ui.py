"""Scope layout switching, input routing and live floating-dock behavior."""
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QDockWidget, QMainWindow, QMenu, QWidget

from src.features.color.ui.scope_panel import MODE_ORDER, ScopePanel, _MULTI_LAYOUTS, _MODE_PRESENTER_ATTR


def test_scope_panel_minimum_width_is_250(qtbot: QtBot) -> None:
    panel = ScopePanel()
    qtbot.addWidget(panel)
    panel.resize(100, 200)
    assert panel.minimumWidth() == 250
    assert panel.width() == 250


@pytest.mark.parametrize('width', [250, 440])
def test_scope_selector_reserves_the_complete_selected_name(qtbot: QtBot, width: int) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = ScopePanel()
    dock = QDockWidget('Scopes', window)
    dock.setWidget(panel)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    window.show()
    panel.setFixedWidth(width)
    for mode in ('waveform', 'vectorscope', 'parade', 'luma', 'histogram', 'falsecolor', 'cie'):
        panel.set_mode(mode)
        qtbot.wait(1)
        combo = panel.mode_combo
        # The custom painter reserves 4px on the left and 23px for the arrow.
        text_width = combo.fontMetrics().horizontalAdvance(combo.currentText())
        assert combo.width() - 27 >= text_width
        assert combo.geometry().right() < panel.selector_container.width()
        assert combo.geometry().right() < panel.settings_btn.geometry().left()


@pytest.mark.parametrize("programmatic", [False, True])
def test_scope_selector_orders_tools_before_multiview_and_selects_matching_pages(
    qtbot: QtBot, programmatic: bool,
) -> None:
    panel = ScopePanel()
    qtbot.addWidget(panel)
    expected = ('waveform', 'vectorscope', 'parade', 'luma', 'histogram',
                'falsecolor', 'cie', 'split', 'quad', 'all')
    assert tuple(panel.mode_combo.itemData(i) for i in range(panel.mode_combo.count())) == expected
    for selector_index, mode in enumerate(expected):
        if programmatic:
            panel.set_mode(mode)
        else:
            panel.mode_combo.setCurrentIndex(selector_index)
        assert panel.current_mode == mode
        assert panel.mode_combo.currentData() == mode
        assert panel.stack.currentIndex() == MODE_ORDER.index(mode)
        assert panel._gpu_stack.currentIndex() == MODE_ORDER.index(mode)
        if mode in _MODE_PRESENTER_ATTR:
            assert panel.stack.currentWidget() is panel._cpu_single_pages[mode]
            assert panel._gpu_stack.currentWidget() is panel._gpu_single_pages[mode]
        elif mode == 'split':
            assert panel.stack.currentWidget() is panel.split_container
            assert panel._gpu_stack.currentWidget() is panel._gpu_splitter


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


def test_viewer_open_keeps_the_same_live_panel_in_an_independent_window(qtbot):
    window = QMainWindow()
    qtbot.addWidget(window)
    dock = QDockWidget('Scopes', window)
    panel = ScopePanel()
    dock.setWidget(panel)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    window.show()
    panel.open_floating()
    dialog = panel.window()
    assert isinstance(dialog, QDialog)
    assert not isinstance(dialog, QDockWidget)
    assert dialog.windowModality() == Qt.WindowModality.NonModal
    assert panel._host_dock() is None
    assert not dock.isFloating()
    assert dock.widget() is not panel
    assert not dock.isVisible()
    assert panel.is_independent_window_visible()
    assert panel.current_mode == 'quad'
    assert panel.layout_toolbar.isVisible()
    assert panel.scope_title.isHidden()
    assert dialog.windowTitle()
    panel.open_floating()
    assert panel.window() is dialog
    panel.expand_btn.click()
    assert dialog.isMaximized()
    panel.expand_btn.click()
    assert not dialog.isMaximized()
    other = QDockWidget('Other', window)
    other.setWidget(QWidget())
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, other)
    window.tabifyDockWidget(dock, other)
    assert panel.window() is dialog
    assert panel._host_dock() is None
    dialog.close()
    assert not panel.is_independent_window_visible()
    assert dock.widget() is panel
    assert panel.current_mode == 'waveform'
    assert panel.mode_combo.findData('split') == -1
    assert panel.mode_combo.findData('quad') == -1
    assert panel.mode_combo.findData('all') == -1
    assert panel.layout_toolbar.isHidden()
    assert not panel.scope_title.isHidden()


@pytest.mark.parametrize('initially_visible', [False, True])
def test_scope_dialog_close_reopen_and_regular_dock_request(
    qtbot: QtBot, initially_visible: bool,
) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    dock = QDockWidget('Scopes', window)
    panel = ScopePanel()
    dock.setWidget(panel)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    window.show()
    dock.setVisible(initially_visible)
    for _ in range(2):
        panel.open_floating()
        assert panel.is_independent_window_visible()
        panel.window().close()
        assert dock.widget() is panel
        assert dock.isVisible() == initially_visible
    panel.open_floating()
    dock.show()
    assert dock.widget() is panel
    assert panel.isVisible()
    assert not panel.is_independent_window_visible()


def test_independent_scope_remains_live_while_original_dock_is_hidden(qtbot: QtBot) -> None:
    from src.app.controllers.layer_controller import LayerOpsMixin

    window = QMainWindow()
    qtbot.addWidget(window)
    dock = QDockWidget('Scopes', window)
    panel = ScopePanel()
    dock.setWidget(panel)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    window.show()
    host = SimpleNamespace(scope_panel=panel, dock_analysis=dock,
                           _analysis_scope_dock_active=False)
    assert not LayerOpsMixin._is_scope_panel_visible(host)
    panel.open_floating()
    assert LayerOpsMixin._is_scope_panel_visible(host)
    panel.window().close()
    dock.hide()
    assert not LayerOpsMixin._is_scope_panel_visible(host)


def test_viewer_popup_is_independent_even_when_original_dock_was_floating(qtbot: QtBot) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    dock = QDockWidget('Scopes', window)
    panel = ScopePanel()
    dock.setWidget(panel)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    window.show()
    dock.setFloating(True)
    panel.open_floating()
    dialog = panel.window()
    assert isinstance(dialog, QDialog)
    assert dialog.parentWidget() is window
    assert panel._host_dock() is None
    assert panel.is_independent_window_visible()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert dock.widget() is panel
    assert not panel.is_independent_window_visible()


def test_docked_selector_only_offers_single_scopes(qtbot: QtBot) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    dock = QDockWidget('Scopes', window)
    panel = ScopePanel()
    dock.setWidget(panel)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    window.show()
    assert tuple(panel.mode_combo.itemData(i) for i in range(panel.mode_combo.count())) == (
        'waveform', 'vectorscope', 'parade', 'luma', 'histogram', 'falsecolor', 'cie')
    panel.open_floating()
    assert all(panel.mode_combo.findData(mode) >= 0 for mode in ('split', 'quad', 'all'))


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
