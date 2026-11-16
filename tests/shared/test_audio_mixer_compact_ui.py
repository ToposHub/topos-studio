"""Compact Mixer geometry and interactions use the existing command signals."""

import pytest

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMenu, QWidget

from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel
from src.shared.ui.style_sheet import C, DARK_THEME


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
    assert all(strip.width() == 88 for strip in strips)
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
    menu = QMenu(panel)
    panel._populate_mixer_menu(menu)
    actions = {action.objectName(): action for action in menu.actions()}
    actions["mixer_compact"].trigger()
    actions["mixer_show_inserts"].trigger()
    panel.register_track("two", "New", "A2")
    assert panel.is_narrow_layout
    assert all(s.width() == 70 for s in panel._track_mixers.values())
    assert all(s._effects_container.isHidden() for s in panel._track_mixers.values())
    assert panel._master_mixer._effects_container.isHidden()
    assert strip._effect_chain is chain
    assert commits == []
    panel.set_narrow_layout(False)
    panel.set_inserts_visible(True)
    menu = QMenu(panel)
    panel._populate_mixer_menu(menu)
    actions = {action.objectName(): action for action in menu.actions()}
    assert not actions["mixer_compact"].isChecked()
    assert actions["mixer_show_inserts"].isChecked()
    assert all(s.width() == 88 for s in panel._track_mixers.values())
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
    assert all(strip.isHidden() for strip in panel._track_mixers.values())
    assert panel._scroll_area.minimumWidth() < 72
    panel.show_all_tracks()
    assert not panel._track_mixers["one"].isHidden()
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
    assert all(strip.isHidden() for strip in panel._track_mixers.values())
    panel.show_all_tracks()
    assert not panel._track_mixers["one"].isHidden()
    assert not panel._track_mixers["one"].isHidden()
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


@pytest.mark.parametrize("dpr,narrow", [(1, False), (2, False), (2, True)])
def test_functional_rules_join_strip_boundaries_under_global_theme(qapp, dpr, narrow) -> None:
    panel = AudioMixerPanel()
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    panel.register_track("one", "Dialogue", "A1")
    panel.register_track("two", "Music", "A2")
    panel._track_mixers["one"].set_effect_chain(_chain(8))
    panel.set_narrow_layout(narrow)
    width = 70 if narrow else 88
    panel.resize(width * 3 + 2, 520)
    panel.show()
    qapp.processEvents()
    image = QImage(panel.width() * dpr, panel.height() * dpr, QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.magenta)
    painter = QPainter(image)
    QWidget.render(panel, painter)
    painter.end()
    assert panel.findChild(QWidget, "mixer_toolbar") is None
    strips = [*panel._track_mixers.values(), panel._master_mixer]
    for name in ("_header_widget", "_effects_container", "_button_section", "_balance_widget", "_meter_section"):
        sections = [getattr(strip, name) for strip in strips]
        if sections[0].isHidden():
            continue
        bottoms = [section.mapTo(panel, QPoint(0, section.height() - 1)).y() for section in sections]
        assert len(set(bottoms)) == 1
        for x in range(panel.width() * dpr):
            for dy in range(dpr):
                assert image.pixelColor(x, bottoms[0] * dpr + dy) == QColor(C.PANEL_DIVIDER)
    # The slim Pan row still keeps all controls inside its own strip.
    if not narrow:
        for strip in strips:
            button = strip._panner_expand_btn
            assert button.mapTo(strip, QPoint(button.width(), 0)).x() <= strip.width()
    panel.close()


@pytest.mark.parametrize("dpr,narrow", [(1, False), (2, False), (1, True), (2, True)])
def test_last_visible_strip_keeps_its_right_rule_before_empty_space(qapp, dpr, narrow) -> None:
    panel = AudioMixerPanel()
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    for i in range(4):
        panel.register_track(str(i), f"Track {i}", f"A{i + 1}")
    panel.set_narrow_layout(narrow)
    panel.resize(600, 480)
    panel.show()

    def check_last(track_id: str) -> None:
        qapp.processEvents()
        strip = panel._track_mixers[track_id]
        origin = strip.mapTo(panel, QPoint(strip.width(), 0))
        image = QImage(panel.width() * dpr, panel.height() * dpr, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.magenta)
        painter = QPainter(image)
        QWidget.render(panel, painter)
        painter.end()
        for y in range(origin.y() * dpr, (origin.y() + strip.height()) * dpr):
            for dx in range(dpr):
                assert image.pixelColor(origin.x() * dpr + dx, y) == QColor(C.PANEL_DIVIDER)
        assert image.pixelColor((origin.x() + 2) * dpr, 30 * dpr) == QColor(C.BG_PANEL)
        # The global QWidget background must not reappear behind the dB unit.
        unit = strip._db_label
        point = unit.mapTo(panel, QPoint(1, unit.height() - 2))
        assert image.pixelColor(point.x() * dpr, point.y() * dpr) == QColor(C.BG_DARK)

    check_last("3")
    panel.hide_track("3")
    check_last("2")
    panel.deregister_track("2")
    check_last("1")
    panel.show_all_tracks()
    check_last("3")
    panel.deregister_track("3")
    panel.register_track("3", "New last track", "A4")
    check_last("3")
    panel.close()


@pytest.mark.parametrize("dpr,narrow", [(1, False), (2, False), (1, True), (2, True)])
def test_track_and_master_junctions_remain_one_pixel_wide(qapp, dpr, narrow) -> None:
    panel = AudioMixerPanel()
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    for i in range(6):
        panel.register_track(str(i), f"Track {i}", f"A{i + 1}")
    panel.set_narrow_layout(narrow)
    panel.resize(350, 480)
    panel.show()

    def check_junctions() -> None:
        qapp.processEvents()
        bar = panel._scroll_area.horizontalScrollBar()
        bar.setValue(bar.maximum())
        qapp.processEvents()
        image = QImage(panel.width() * dpr, panel.height() * dpr, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.magenta)
        painter = QPainter(image)
        QWidget.render(panel, painter)
        painter.end()
        master_x = panel._master_mixer.mapTo(panel, QPoint()).x() - 1
        y = panel._master_mixer._meter_section.mapTo(panel, QPoint(0, 10)).y()
        junctions = [master_x]
        for tid, strip in panel._track_mixers.items():
            if strip.isHidden():
                continue
            x = strip.mapTo(panel, QPoint(strip.width(), 0)).x()
            if 1 <= x <= master_x:
                junctions.append(x)
        for x in junctions:
            for dx in range(dpr):
                assert image.pixelColor(x * dpr + dx, y * dpr) == QColor(C.PANEL_DIVIDER)
            assert image.pixelColor((x - 1) * dpr, y * dpr) == QColor(C.BG_PANEL)
            assert image.pixelColor((x + 1) * dpr, y * dpr) == QColor(C.BG_PANEL)

    check_junctions()
    panel.hide_track("5")
    check_junctions()
    panel.deregister_track("4")
    check_junctions()
    panel.show_all_tracks()
    check_junctions()
    panel.resize(410, 550)
    check_junctions()
    panel.close()


@pytest.mark.parametrize("source", ["track", "master", "rack", "blank"])
def test_removed_toolbar_options_remain_available_in_context_menus(qapp, monkeypatch, source) -> None:
    panel = AudioMixerPanel()
    panel.register_track("one", "Dialogue", "A1")
    strip = panel._track_mixers["one"]
    commits: list = []
    panel.track_effects_committed.connect(lambda *args: commits.append(args))
    visited: list = []

    def choose_compact(menu, _pos):
        options = menu
        for action in menu.actions():
            if action.menu() is not None and any(a.objectName() == "mixer_compact" for a in action.menu().actions()):
                options = action.menu()
                break
        actions = {action.objectName(): action for action in options.actions()}
        assert actions["mixer_show_inserts"].isChecked()
        actions["mixer_compact"].trigger()
        visited.append(True)

    monkeypatch.setattr(QMenu, "exec", choose_compact)
    if source == "track":
        strip._track_tag_label.customContextMenuRequested.emit(QPoint())
    elif source == "master":
        panel._master_mixer._track_tag_label.customContextMenuRequested.emit(QPoint())
    elif source == "rack":
        strip._effect_list.customContextMenuRequested.emit(QPoint())
    else:
        panel._scroll_area.viewport().customContextMenuRequested.emit(QPoint())
    assert visited == [True]
    assert panel.is_narrow_layout
    assert commits == []
    assert strip._effect_list.count() == 0
    assert strip._add_effect_btn.text() == "+"
    panel.close()
