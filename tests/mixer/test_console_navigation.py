"""Production console navigation is a view over canonical mixer state."""
from __future__ import annotations

from copy import deepcopy

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtTest import QTest
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


def test_search_names_tags_casefold_and_escape(console, qtbot):
    console.setFocus()
    QTest.keySequence(console, QKeySequence(QKeySequence.StandardKey.Find))
    assert console.channel_search.hasFocus()
    qtbot.mouseClick(console.channel_search, Qt.MouseButton.LeftButton)
    console.channel_search.setText('对白 BOOM')
    assert _visible(console) == {'dx'}
    console.channel_search.setText('a2')
    assert _visible(console) == {'mx'}
    assert not console._master_mixer.isHidden()
    qtbot.keyClick(console.channel_search, Qt.Key.Key_Escape)
    assert _visible(console) == {'dx', 'mx', 'bus', 'fx'}


def test_view_filters_preserve_manual_hidden_audio_state_and_signals(console, qtbot):
    changes = []
    console.track_mute_toggled.connect(lambda *args: changes.append(args))
    console.track_volume_changed.connect(lambda *args: changes.append(args))
    console.hide_track('bus')
    console.channel_filter.setCurrentIndex(console.channel_filter.findData('bus'))
    assert _visible(console) == {'fx'}
    assert console.channel_count.text() == '1/4'
    console.channel_search.setText('no such channel')
    assert console.channel_empty_label.isVisible()
    console.channel_search.clear()
    assert console.get_hidden_track_ids() == {'bus'}
    console.reset_channel_view()
    assert _visible(console) == {'dx', 'mx', 'bus', 'fx'}
    assert not console.get_hidden_track_ids()
    assert not changes


def test_sync_rename_reorder_new_tags_and_deletion(console):
    tracks = _tracks()
    tracks = [tracks[3], tracks[1], tracks[0], tracks[2]]
    tracks[0]['tag'] = 'B1'
    tracks[0]['name'] = 'Room return'
    console.channel_search.setText('Room')
    console.sync_tracks(tracks)
    assert _visible(console) == {'fx'}
    assert console._track_mixers['fx']._track_tag_label.text() == 'B1'
    assert [console._tracks_layout.itemAt(i * 2).widget()._track_id for i in range(4)] == ['fx', 'mx', 'dx', 'bus']
    console.sync_tracks(tracks[1:])
    assert console.channel_count.text() == '0/3'
    console.sync_tracks(tracks)
    assert _visible(console) == {'fx'}


def test_solo_and_arm_filters_follow_canonical_updates(console):
    console.channel_filter.setCurrentIndex(console.channel_filter.findData('solo'))
    assert _visible(console) == {'mx'}
    console.clear_all_solo()
    assert not _visible(console)
    assert not console.clear_solo_button.isEnabled()
    console.set_track_solo('dx', True)
    assert _visible(console) == {'dx'}
    console.set_recording_workflow_active(True)
    console.set_recording_inputs({'dx': 'Input 1'})
    console.set_record_arm_state({'dx'})
    console.channel_filter.setCurrentIndex(console.channel_filter.findData('armed'))
    assert _visible(console) == {'dx'}
    console.set_recording_workflow_active(False)
    assert not _visible(console)
    console.set_recording_workflow_active(True)
    console.set_record_arm_state(set())
    assert not _visible(console)


def test_reset_peaks_preserves_live_gr_and_faders_even_hidden(console, qtbot):
    for strip in (*console._track_mixers.values(), console._master_mixer):
        strip.studio_meter.set_levels(0.0, -1.0, -6.0)
        strip.studio_meter.set_levels(-12.0, -13.0, -6.0)
    console.hide_track('dx')
    before = [(s.studio_meter.level_l, s.studio_meter.gr_db, s.get_volume_linear())
              for s in (*console._track_mixers.values(), console._master_mixer)]
    qtbot.mouseClick(console.reset_peaks_button, Qt.MouseButton.LeftButton)
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
    qtbot.mouseClick(console.routing_overview_button, Qt.MouseButton.LeftButton)
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
    assert console.channel_count.text() == '0/0'


@pytest.mark.parametrize('width', [200, 220, 320, 560])
def test_navigation_remains_inside_narrow_console(console, qtbot, width):
    console.resize(width, 620)
    qtbot.wait(20)
    navigation = console.findChild(type(console.channel_search.parent()), 'mixerChannelNavigation')
    for child in (console.channel_search, console.channel_filter, console.console_options_button,
                  console.clear_solo_button, console.reset_peaks_button, console.routing_overview_button):
        assert navigation.rect().contains(child.geometry())
    assert console._master_separator.y() == console._master_mixer.mapTo(console, QPoint()).y()
    assert console._master_separator.y() >= navigation.geometry().bottom()
