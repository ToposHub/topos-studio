"""The subtitle tools and Mixer share one recording workspace."""

from PyQt6.QtWidgets import QDockWidget, QMainWindow, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
from src.features.edit.ui.edit_effects_panel import EditEffectsPanel


class _Host(QMainWindow, DockLayoutMixin):
    def _wire_adr_panel(self) -> None:
        pass


def test_subtitles_hosts_voiceover_and_animation_only_in_edit(qtbot: QtBot) -> None:
    host = _Host()
    qtbot.addWidget(host)
    effects = EditEffectsPanel(host)
    host.edit_effects_panel = effects
    host.dock_edit_effects = QDockWidget(host)
    host.dock_edit_effects.setWidget(effects)
    host.adr_take_sheet_panel = panel = AdrTakeSheetPanel()
    adr_dock = QDockWidget(host)
    adr_host = QWidget()
    QVBoxLayout(adr_host).addWidget(panel)
    adr_dock.setWidget(adr_host)
    host.dock_adr_take_sheet = adr_dock

    panel._pre_roll_spin.setValue(1.5)
    host.current_page = "Edit"
    host.dock_edit_effects.hide()
    adr_dock.show()
    host._sync_adr_panel_host()
    assert adr_dock.isHidden()
    assert not host.dock_edit_effects.isHidden()
    assert effects.tabs.currentWidget() is effects._subtitle_tab
    assert effects._subtitle_tab.isAncestorOf(panel)
    assert panel._tabs.currentWidget() is panel._record_page
    assert panel._tabs.widget(1) is effects._caption_tools
    assert panel._tabs.isTabVisible(1)
    assert not panel._playhead_record_btn.isHidden()
    assert not panel._playhead_record_btn.isEnabled()
    assert not effects.tabs.widget(2).isAncestorOf(effects.caption_effects_tree)

    host.current_page = "Mixer"
    host._sync_adr_panel_host()
    assert panel.parentWidget() is adr_host
    assert panel._tabs.currentWidget() is panel._cue_page
    assert not panel._tabs.isTabVisible(panel._tabs.indexOf(effects._caption_tools))
    assert panel._playhead_record_btn.isHidden()
    assert panel.adr_settings()["pre_roll_seconds"] == 1.5

    host.current_page = "Edit"
    host._sync_adr_panel_host()
    assert effects._subtitle_tab.isAncestorOf(panel)
    assert panel._tabs.count() == 4
    assert panel._tabs.isTabVisible(panel._tabs.indexOf(effects._caption_tools))
    assert panel.adr_settings()["pre_roll_seconds"] == 1.5
