"""Compact Mixer geometry and interactions use the existing command signals."""

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel


def _chain(count: int) -> EffectChainParams:
    return EffectChainParams(effects=tuple(
        TrackEffectInstance(effect_type="delay", params={"enabled": True},
                            order=i, instance_id=f"delay-{i}")
        for i in range(count)
    ))


def test_faders_align_with_empty_and_full_racks(qapp) -> None:
    panel = AudioMixerPanel()
    panel.register_track("empty", "Dialogue with a very long name", "A1")
    panel.register_track("full", "Music", "A2")
    panel._track_mixers["full"].set_effect_chain(_chain(8))
    panel.resize(400, 420)
    panel.show()
    qapp.processEvents()
    strips = [*panel._track_mixers.values(), panel._master_mixer]
    faders = [strip._volume_fader for strip in strips]
    assert len({fader.mapTo(panel, fader.rect().topLeft()).y() for fader in faders}) == 1
    assert len({fader.height() for fader in faders}) == 1
    assert all(strip.width() == 112 for strip in strips)
    full = panel._track_mixers["full"]
    assert full._effect_list.verticalScrollBar().maximum() > 0
    assert full._add_effect_btn.isVisible()
    assert panel._track_mixers["empty"]._track_label.toolTip() == "Dialogue with a very long name"
    panel.close()


def test_view_controls_preserve_effects_and_apply_to_new_tracks(qapp) -> None:
    panel = AudioMixerPanel()
    panel.register_track("one", "Track", "A1")
    strip = panel._track_mixers["one"]
    chain = _chain(4)
    strip.set_effect_chain(chain)
    commits: list = []
    panel.track_effects_committed.connect(lambda *args: commits.append(args))
    panel._compact_btn.click()
    panel._inserts_btn.click()
    panel.register_track("two", "New", "A2")
    assert panel.is_narrow_layout
    assert all(s.width() == 70 for s in panel._track_mixers.values())
    assert all(s._effects_container.isHidden() for s in panel._track_mixers.values())
    assert panel._master_mixer._effects_container.isHidden()
    assert strip._effect_chain is chain
    assert commits == []
    panel.set_narrow_layout(False)
    panel.set_inserts_visible(True)
    assert not panel._compact_btn.isChecked()
    assert panel._inserts_btn.isChecked()
    assert all(s.width() == 112 for s in panel._track_mixers.values())
    panel.close()


def test_rack_keyboard_bypass_and_remove_commit_once(qapp) -> None:
    panel = AudioMixerPanel()
    panel.register_track("one", "Track", "A1")
    strip = panel._track_mixers["one"]
    strip.set_effect_chain(_chain(2))
    panel.show()
    strip._effect_list.setFocus()
    strip._effect_list.setCurrentRow(0)
    qapp.processEvents()
    commits: list = []
    panel.track_effects_committed.connect(lambda *args: commits.append(args))
    QTest.keyClick(strip._effect_list, Qt.Key.Key_Space)
    assert len(commits) == 1
    assert strip._effect_chain.effects[0].params["enabled"] is False
    assert strip._effect_list.currentItem().data(Qt.ItemDataRole.UserRole) == "delay-0"
    QTest.keyClick(strip._effect_list, Qt.Key.Key_Delete)
    assert len(commits) == 2
    assert [e.instance_id for e in strip._effect_chain.effects] == ["delay-1"]
    panel.close()


def test_hidden_tracks_update_count_and_minimum_width(qapp) -> None:
    panel = AudioMixerPanel()
    panel.register_track("one", "Track", "A1")
    panel.set_narrow_layout(True)
    assert panel._scroll_area.minimumWidth() == 72
    panel.hide_track("one")
    assert panel._track_count_label.text() == "0"
    assert panel._scroll_area.minimumWidth() < 72
    panel.show_all_tracks()
    assert panel._track_count_label.text() == "1"
    panel.close()


def test_muted_strip_remains_editable_and_emits_existing_gain_signal(qapp) -> None:
    panel = AudioMixerPanel()
    panel.register_track("one", "Track", "A1")
    strip = panel._track_mixers["one"]
    strip.set_mute(True)
    changed: list = []
    panel.track_volume_changed.connect(lambda *args: changed.append(args))
    assert strip._volume_fader.isEnabled()
    assert strip._volume_spin.isEnabled()
    assert strip._balance_fader.isEnabled()
    strip._volume_spin.setValue(-6.0)
    assert changed == [("one", -6.0)]
    assert strip.is_mute()
    panel.close()


@pytest.mark.parametrize("count,width,narrow", [(8, 400, False), (2, 250, False), (8, 300, True)])
def test_scrolling_tracks_keep_same_fader_travel_as_master(qapp, count, width, narrow) -> None:
    panel = AudioMixerPanel()
    for i in range(count):
        panel.register_track(str(i), f"Track {i}", f"A{i+1}")
    panel.set_narrow_layout(narrow)
    panel.resize(width, 420)
    panel.show()
    qapp.processEvents()
    assert panel._scroll_area.horizontalScrollBar().maximum() > 0
    faders = [s._volume_fader for s in panel._track_mixers.values()]
    faders.append(panel._master_mixer._volume_fader)
    assert len({f.mapTo(panel, f.rect().topLeft()).y() for f in faders}) == 1
    assert len({f.height() for f in faders}) == 1
    panel.close()


def test_hidden_track_re_registration_preserves_view_preference(qapp) -> None:
    panel = AudioMixerPanel()
    panel.register_track("one", "Track", "A1")
    panel.hide_track("one")
    panel.deregister_track("one")
    panel.register_track("one", "Track", "A1")
    assert panel._track_mixers["one"].isHidden()
    assert panel._track_count_label.text() == "0"
    panel.show_all_tracks()
    assert not panel._track_mixers["one"].isHidden()
    assert panel._track_count_label.text() == "1"
    panel.close()


def test_resizing_meter_preserves_reading_without_a_new_audio_packet(qapp) -> None:
    panel = AudioMixerPanel()
    panel.register_track("one", "Track", "A1")
    panel.resize(350, 420)
    panel.show()
    qapp.processEvents()
    meter = panel._track_mixers["one"]._meter
    meter.set_audio_values([-12.0, -18.0])
    before = meter._value_positions[0]
    fraction = before / meter._primary_axis_length
    panel.resize(350, 600)
    qapp.processEvents()
    assert meter._value_decibels == [-12.0, -18.0]
    assert meter._value_positions[0] > before
    assert abs(meter._value_positions[0] / meter._primary_axis_length - fraction) < 0.01
    panel.close()
