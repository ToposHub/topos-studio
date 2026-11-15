"""Focused regressions found by the audio maturity follow-up review."""

from collections.abc import Iterator
from functools import partial

import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QVBoxLayout, QWidget

from src.features.edit.core.audio_dsp import strip_eq_band_response_db
from src.features.edit.core.timeline_v2 import (
    CompressorParams, EffectChainParams, ExpanderParams, LimiterParams,
    TrackEffectInstance,
)
from src.shared.ui.audio_mixer.strip_eq_dialog import ParametricEQDialog
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel
from src.shared.ui.audio_mixer.track_mixer_widget import _effect_default_params


@pytest.fixture
def dynamics_strip(qtbot: QtBot) -> Iterator[TrackMixerWidget]:
    strip = TrackMixerWidget("track", "Track")
    qtbot.addWidget(strip)
    params = [ExpanderParams().to_dict(), CompressorParams().to_dict(),
              LimiterParams().to_dict()]
    for state, enabled in zip(params, [False, True, False]):
        state["enabled"] = enabled
    strip.set_effect_chain(EffectChainParams(effects=tuple(
        TrackEffectInstance(effect_type=effect_type, params=state,
                            panel="dynamics")
        for effect_type, state in zip(
            ["expander", "compressor", "limiter"], params)
    )))
    yield strip
    strip.close_all_dialogs()


@pytest.mark.parametrize("sample_rate", [44100.0, 96000.0])
def test_eq_graph_uses_constructor_sample_rate(sample_rate: float) -> None:
    """Opening EQ after an engine rate update uses that rate immediately."""
    app = QApplication.instance() or QApplication([])
    dialog = ParametricEQDialog("track", "Track", sample_rate=sample_rate)
    try:
        band = {
            "enabled": True, "filter_type": "Bell", "freq": 16000.0,
            "gain": 12.0, "q": 2.0,
        }
        actual = dialog.eq_graph._band_magnitude_db(band, 18000.0)
        expected = strip_eq_band_response_db(band, 18000.0, sample_rate, "Clean")
        assert actual == pytest.approx(expected, abs=1e-9)
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_dynamics_rack_switch_restores_power_without_changing_modules(
    dynamics_strip: TrackMixerWidget,
) -> None:
    strip = dynamics_strip
    strip._open_dynamics_panel()
    dialog = strip._dynamics_dialog
    dialog._on_power_toggled(False)
    committed = []
    previewed = []
    strip.track_effects_committed.connect(lambda *args: committed.append(args))
    strip.track_effects_preview.connect(lambda *args: previewed.append(args))
    strip._effect_bypass_dynamics()
    assert all(inst.enabled for inst in strip._effect_chain.effects)
    assert [inst.params["enabled"] for inst in strip._effect_chain.effects] == [False, True, False]
    assert dialog._panel_state()["enabled"] is True
    assert dialog.get_chain() == strip._effect_chain
    assert len(committed) == 1
    assert previewed == []
    assert strip._effect_list.item(0).data(Qt.ItemDataRole.UserRole + 2) is True
    strip._effect_bypass_dynamics()
    assert not any(inst.enabled for inst in strip._effect_chain.effects)
    assert [inst.params["enabled"] for inst in strip._effect_chain.effects] == [False, True, False]
    assert dialog._panel_state()["enabled"] is False
    assert strip._effect_list.item(0).data(Qt.ItemDataRole.UserRole + 2) is False
    assert len(committed) == 2


def test_dynamics_context_menu_only_offers_group_actions(
    dynamics_strip: TrackMixerWidget, monkeypatch: pytest.MonkeyPatch,
) -> None:
    strip = dynamics_strip
    strip.show()
    QApplication.processEvents()
    menus = []
    monkeypatch.setattr(QMenu, "exec", lambda menu, *_args: menus.append(menu))
    item = strip._effect_list.item(0)
    strip._on_effect_list_context_menu(strip._effect_list.visualItemRect(item).center())
    actions = [action for action in menus[0].actions() if not action.isSeparator()]
    # Group bypass/delete followed by the Add submenu; no individual instance actions.
    assert len(actions) == 3
    assert actions[-1].menu() is not None


def test_dynamics_badge_respects_expander_and_group_bypass(
    dynamics_strip: TrackMixerWidget,
) -> None:
    strip = dynamics_strip
    expander = TrackEffectInstance(
        effect_type="expander", params={**ExpanderParams().to_dict(), "enabled": True},
        panel="dynamics",
    )
    strip.set_effect_chain(EffectChainParams(effects=(expander,)))
    active_style = strip._dy_badge.styleSheet()
    strip._effect_bypass_dynamics()
    assert strip._dy_badge.styleSheet() != active_style
    strip._effect_bypass_dynamics()
    assert strip._dy_badge.styleSheet() == active_style


@pytest.mark.parametrize("effect_type,panel,badge", [
    ("strip_eq", "eq", "_eq_badge"), ("delay", "fx", "_fx_badge"),
])
@pytest.mark.parametrize("instance_enabled,params_enabled", [(False, True), (True, False)])
def test_eq_and_fx_badges_respect_both_bypass_levels(
    qtbot: QtBot, effect_type: str, panel: str, badge: str,
    instance_enabled: bool, params_enabled: bool,
) -> None:
    strip = TrackMixerWidget("track", "Track")
    qtbot.addWidget(strip)
    active = TrackEffectInstance(effect_type, {"enabled": True}, panel=panel)
    strip.set_effect_chain(EffectChainParams(effects=(active,)))
    active_style = getattr(strip, badge).styleSheet()
    bypassed = TrackEffectInstance(effect_type, {"enabled": params_enabled},
                                   enabled=instance_enabled, panel=panel,
                                   instance_id=active.instance_id)
    strip.set_effect_chain(EffectChainParams(effects=(bypassed,)))
    assert getattr(strip, badge).styleSheet() != active_style


@pytest.mark.parametrize("sample_rate", [44100.0, 96000.0])
def test_new_strip_inherits_engine_rate_before_opening_eq(
    qtbot: QtBot, sample_rate: float,
) -> None:
    panel = AudioMixerPanel()
    qtbot.addWidget(panel)
    panel.on_engine_sample_rate(sample_rate)
    panel.register_track("late", "Late track")
    strip = panel._track_mixers["late"]
    strip._open_eq_from_badge()
    try:
        assert strip._engine_sample_rate == sample_rate
        assert strip._eq_dialog.eq_graph._sample_rate == sample_rate
        assert panel._master_mixer._engine_sample_rate == sample_rate
    finally:
        strip.close_all_dialogs()


@pytest.mark.parametrize("effect_type", [
    "eq_badge", "dynamics", "panner", "limiter", "compressor", "reverb",
    "delay", "saturation", "imager", "expander", "noise_gate", "strip_eq",
])
def test_closed_effect_dialogs_are_released(qtbot: QtBot, effect_type: str) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    strip = TrackMixerWidget("track", "Track")
    QVBoxLayout(host).addWidget(strip)
    if effect_type == "eq_badge":
        open_editor = strip._open_eq_from_badge
    elif effect_type == "dynamics":
        open_editor = strip._open_dynamics_panel
    elif effect_type == "panner":
        open_editor = strip._open_panner_dialog
    else:
        inst = TrackEffectInstance(effect_type, _effect_default_params(effect_type))
        strip.set_effect_chain(EffectChainParams(effects=(inst,)))
        editor_type = "expander" if effect_type == "noise_gate" else effect_type
        open_editor = partial(getattr(strip, f"_open_{editor_type}_editor"), inst)
    for _ in range(3):
        open_editor()
        dialogs = host.findChildren(QDialog)
        assert len(dialogs) == 1
        assert dialogs[0].testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialogs[0].close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert host.findChildren(QDialog) == []
    strip.close_all_dialogs()


def test_old_dynamics_deletion_does_not_clear_reopened_dialog(
    dynamics_strip: TrackMixerWidget,
) -> None:
    strip = dynamics_strip
    strip._open_dynamics_panel()
    old_dialog = strip._dynamics_dialog
    old_dialog.close()
    strip._open_dynamics_panel()
    current_dialog = strip._dynamics_dialog
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert strip._dynamics_dialog is current_dialog
    assert current_dialog.isVisible()
