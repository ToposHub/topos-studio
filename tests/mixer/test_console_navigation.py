"""Production console navigation is a view over canonical mixer state."""
from __future__ import annotations

from copy import deepcopy

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QAction, QContextMenuEvent, QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QInputDialog, QMenu, QWidget
import pytest

from src.features.mixer.ui.studio_console import StudioAudioMixerPanel


def _tracks():
    return [
        dict(track_id='dx', name='对白 Boom', tag='A1', output_bus_id='bus',
             sends=[dict(target_bus_id='fx', level=0.5, tap='pre_fader', enabled=True, muted=False)]),
        dict(track_id='mx', name='Music', tag='A2', is_solo=True),
        dict(track_id='bus', name='DX Bus', tag='A3', is_bus=True, output_bus_id='fx'),
        dict(track_id='fx', name='FX Bus', tag='A4', is_bus=True),
    ]


@pytest.fixture
def console(qtbot):
    panel = StudioAudioMixerPanel()
    qtbot.addWidget(panel)
    panel.resize(560, 600)
    panel.sync_tracks(_tracks())
    panel.show()
    return panel


def _visible(panel):
    return {tid for tid, strip in panel._track_mixers.items() if not strip.isHidden()}


def _menu(console: StudioAudioMixerPanel) -> QMenu:
    menu = QMenu(console)
    console._populate_mixer_menu(menu)
    return menu


def _action(menu: QMenu, name: str) -> QAction:
    return next(action for action in menu.actions() if action.objectName() == name)


def _view_menu(menu: QMenu) -> QMenu:
    return menu.findChild(QMenu, 'mixer_channel_view')


def _choose_view(console: StudioAudioMixerPanel, key: str) -> None:
    menu = _menu(console)
    action = next(action for action in _view_menu(menu).actions() if action.data() == key)
    action.trigger()


def test_search_names_and_tags_from_shortcut_and_context_menu(console, monkeypatch):
    queries = iter([('对白 BOOM', True), ('a2', True), ('ignored', False)])
    monkeypatch.setattr(QInputDialog, 'getText', lambda *_args, **_kwargs: next(queries))
    console.setFocus()
    QTest.keySequence(console, QKeySequence(QKeySequence.StandardKey.Find))
    assert _visible(console) == {'dx'}
    _action(_menu(console), 'mixer_find_channel').trigger()
    assert _visible(console) == {'mx'}
    assert not console._master_mixer.isHidden()
    _action(_menu(console), 'mixer_find_channel').trigger()
    assert _visible(console) == {'mx'}
    _action(_menu(console), 'mixer_clear_channel_search').trigger()
    assert _visible(console) == {'dx', 'mx', 'bus', 'fx'}


def test_view_filters_preserve_manual_hidden_audio_state_and_signals(console):
    changes = []
    console.track_mute_toggled.connect(lambda *args: changes.append(args))
    console.track_volume_changed.connect(lambda *args: changes.append(args))
    console.hide_track('bus')
    _choose_view(console, 'bus')
    assert _visible(console) == {'fx'}
    menu = _menu(console)
    assert _action(_view_menu(menu), 'mixer_channel_count').text().endswith('1/4')
    console._set_channel_query('no such channel')
    assert console.channel_empty_label.isVisible()
    _action(_menu(console), 'mixer_clear_channel_search').trigger()
    assert console.get_hidden_track_ids() == {'bus'}
    _action(_view_menu(menu), 'mixer_reset_channel_view').trigger()
    assert _visible(console) == {'dx', 'mx', 'bus', 'fx'}
    assert not console.get_hidden_track_ids()
    _choose_view(console, 'bus')
    menu = _menu(console)
    _action(_view_menu(menu), 'mixer_reset_channel_view').trigger()
    assert _visible(console) == {'dx', 'mx', 'bus', 'fx'}
    assert not changes


def test_sync_rename_reorder_new_tags_and_deletion(console):
    tracks = _tracks()
    tracks = [tracks[3], tracks[1], tracks[0], tracks[2]]
    tracks[0]['tag'] = 'B1'
    tracks[0]['name'] = 'Room return'
    console._set_channel_query('Room')
    console.sync_tracks(tracks)
    assert _visible(console) == {'fx'}
    assert console._track_mixers['fx']._track_tag_label.text() == 'B1'
    assert [console._tracks_layout.itemAt(i * 2).widget()._track_id for i in range(4)] == ['fx', 'mx', 'dx', 'bus']
    console.sync_tracks(tracks[1:])
    menu = _menu(console)
    assert _action(_view_menu(menu), 'mixer_channel_count').text().endswith('0/3')
    console.sync_tracks(tracks)
    assert _visible(console) == {'fx'}


def test_solo_and_arm_filters_follow_canonical_updates(console):
    _choose_view(console, 'solo')
    assert _visible(console) == {'mx'}
    _action(_menu(console), 'mixer_clear_solo').trigger()
    assert not _visible(console)
    assert not _action(_menu(console), 'mixer_clear_solo').isEnabled()
    console.set_track_solo('dx', True)
    assert _visible(console) == {'dx'}
    console.set_recording_workflow_active(True)
    console.set_recording_inputs({'dx': 'Input 1'})
    console.set_record_arm_state({'dx'})
    _choose_view(console, 'armed')
    assert _visible(console) == {'dx'}
    console.set_recording_workflow_active(False)
    assert not _visible(console)
    console.set_recording_workflow_active(True)
    console.set_record_arm_state(set())
    assert not _visible(console)


def test_reset_peaks_preserves_live_gr_and_faders_even_hidden(console):
    for strip in (*console._track_mixers.values(), console._master_mixer):
        strip.studio_meter.set_levels(0.0, -1.0, -6.0)
        strip.studio_meter.set_levels(-12.0, -13.0, -6.0)
    console.hide_track('dx')
    before = [(s.studio_meter.level_l, s.studio_meter.gr_db, s.get_volume_linear())
              for s in (*console._track_mixers.values(), console._master_mixer)]
    _action(_menu(console), 'mixer_reset_peaks').trigger()
    after = [(s.studio_meter.level_l, s.studio_meter.gr_db, s.get_volume_linear())
             for s in (*console._track_mixers.values(), console._master_mixer)]
    assert after == before
    for strip in (*console._track_mixers.values(), console._master_mixer):
        assert not strip.studio_meter.clipped_l
        assert strip.studio_meter.peak_l == strip.studio_meter.level_l
        assert strip.studio_peak.text().startswith(f'{strip.studio_meter.level_l:.1f}')


def test_click_meter_immediately_refreshes_peak_number(console, qtbot):
    strip = console._track_mixers['dx']
    strip.update_audio_level([0.0, -2.0])
    strip.update_audio_level([-12.0, -13.0])
    assert strip.studio_peak.text().startswith('0.0')
    qtbot.mouseClick(strip.studio_meter, Qt.MouseButton.LeftButton)
    assert strip.studio_peak.text().startswith('-1.8')


def test_routing_overview_live_upstream_and_existing_edit_signal(console, qtbot):
    changes = []
    console.track_show_routing.connect(changes.append)
    _action(_menu(console), 'mixer_routing_overview').trigger()
    dialog = console._routing_overview
    assert dialog.table.rowCount() == 4
    dialog.target.setCurrentIndex(dialog.target.findData('fx'))
    assert {dialog.table.item(i, 0).data(Qt.ItemDataRole.UserRole) for i in range(dialog.table.rowCount())} == {'dx', 'bus'}
    dialog.table.selectRow(0)
    qtbot.mouseClick(dialog.edit_button, Qt.MouseButton.LeftButton)
    assert changes == ['dx']
    tracks = _tracks()
    tracks[0]['sends'][0]['muted'] = True
    tracks[0]['output_bus_id'] = None
    console.sync_tracks(tracks)
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == 'bus'
    console.sync_tracks([])
    assert dialog.table.rowCount() == 0
    assert not dialog.edit_button.isEnabled()
    console.close_effect_editors()
    assert not dialog.isVisible()


def test_routing_view_keeps_source_snapshot_and_disabled_send_details(console):
    tracks = _tracks()
    tracks[0]['sends'][0].update(enabled=False, muted=True)
    original = deepcopy(tracks)
    console.sync_tracks(tracks)
    console.open_routing_overview()
    dialog = console._routing_overview
    assert 'Pre' in dialog.table.item(0, 3).text()
    assert 'Off' in dialog.table.item(0, 3).text()
    dialog.search.setText('music')
    assert dialog.table.rowCount() == 1
    assert tracks == original


def test_closing_project_clears_open_routing_overview(console):
    console.open_routing_overview()
    console.clear_all_tracks()
    assert console._routing_overview.table.rowCount() == 0
    assert not console._routing_overview.edit_button.isEnabled()
    menu = _menu(console)
    assert _action(_view_menu(menu), 'mixer_channel_count').text().endswith('0/0')


@pytest.mark.parametrize('width', [200, 220, 320, 560])
def test_channels_start_at_top_without_navigation_rows(console, qtbot, width):
    console.resize(width, 620)
    qtbot.wait(20)
    assert console.findChild(QWidget, 'mixerChannelNavigation') is None
    assert console._scroll_area.y() == 0
    assert console._master_separator.y() == console._master_mixer.mapTo(console, QPoint()).y() == 0


def test_background_and_track_name_context_menus_expose_mixer_commands(console, monkeypatch):
    commands = {'mixer_find_channel', 'mixer_routing_overview', 'mixer_clear_solo', 'mixer_reset_peaks'}
    opened = []

    def inspect_menu(menu: QMenu, _position: QPoint) -> None:
        opened.append(menu)

    monkeypatch.setattr(QMenu, 'exec', inspect_menu)
    background = console._scroll_area.widget()
    point = QPoint(background.width() - 8, 8)
    QApplication.sendEvent(background, QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, point, background.mapToGlobal(point),
    ))
    assert commands <= {action.objectName() for action in opened[0].actions()}
    console._track_mixers['dx']._on_label_context_menu(QPoint())
    assert opened[1].findChild(QMenu, 'mixer_channel_view') is not None
