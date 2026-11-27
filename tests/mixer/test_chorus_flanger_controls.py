"""Production controls and block boundaries for the shared modulated delay."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QInputDialog, QPushButton
from pytestqt.qtbot import QtBot

from src.features.edit.core import effect_preset
from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, EffectChain
from src.features.edit.core.audio_effects.modulation_tools import ChorusFlangerEffect
from src.features.mixer.effects.production_dialog import ProductionEffectDialog
from src.shared.i18n.i18n import I18N


@pytest.mark.parametrize("feedback", [0.0, -0.95, 0.95])
def test_chorus_flanger_multivoice_wrap_and_one_sample_feedback(feedback: float) -> None:
    params = {"mode": "flanger", "rate_hz": 20.0, "base_ms": 0.5,
              "depth_ms": 50.0, "feedback": feedback, "voices": 4, "mix": 0.8}
    samples = np.random.default_rng(52).normal(0, 0.04, (2, 15929)).astype(np.float32)
    whole = ChorusFlangerEffect()
    whole.set_params(params)
    expected = whole.process(samples)
    stream = ChorusFlangerEffect()
    stream.set_params(params)
    chunks = [stream.process(samples[:, start:start + 313])
              for start in range(0, samples.shape[-1], 313)]
    output = np.concatenate(chunks, axis=-1)
    assert np.isfinite(output).all()
    np.testing.assert_allclose(output, expected, rtol=0.0, atol=8e-8)
    assert stream.collect_telemetry()["phase"] == pytest.approx(whole.collect_telemetry()["phase"])


@pytest.mark.parametrize("version", [4, ALGORITHM_VERSION])
def test_sparse_chorus_flanger_snapshot_restores_original_settings(version: int) -> None:
    clip = np.random.default_rng(7).normal(0, 0.1, (2, 2513)).astype(np.float32)
    initial = {"mode": "chorus", "rate_hz": 1.3}
    preview = initial | {"base_ms": 42.0, "voices": 4, "feedback": 0.55, "mix": 0.95}
    baseline = EffectChain()
    baseline.set_params([("chorus_flanger", initial, True, "instance", version)])
    effect = EffectChain()
    effect.set_params([("chorus_flanger", preview, True, "instance", version)])
    assert np.max(np.abs(effect.process(clip) - baseline.process(clip))) > 0.01
    effect.set_params([("chorus_flanger", initial, True, "instance", version)])
    effect.reset_state()
    baseline.reset_state()
    np.testing.assert_array_equal(effect.process(clip), baseline.process(clip))


def test_flanger_factory_preset_a_b_and_mode_edits(qtbot: QtBot) -> None:
    initial = {"mode": "chorus", "feedback": 0.24, "voices": 4, "future_key": {"keep": True}}
    dialog = ProductionEffectDialog("chorus_flanger", "Bus", initial)
    qtbot.addWidget(dialog)
    dialog.show()
    panel = dialog.panel
    combo = panel.preset_combo
    label = I18N.tr("audio.fx.flanger_inverted", "Inverted Flanger")
    index = combo.findText(label)
    assert index >= 0
    combo.setCurrentIndex(index)
    flanger = dialog.current_params()
    assert flanger == initial | {"mode": "flanger", "rate_hz": 0.62, "depth_ms": 1.9,
                                 "feedback": -0.68, "base_ms": 3.4, "mix": 0.54, "voices": 1}
    assert panel.flanger_card.isVisible()
    panel.btn_copy_ab.click()
    panel.btn_b.click()
    assert dialog.current_params() == flanger
    panel.mode_btns["Chorus"].click()
    assert dialog.current_params() == flanger | {"mode": "chorus"}
    panel.chorus_card.c_depth._on_pill_edited("37.6 ms")
    assert dialog.current_params()["depth_ms"] == pytest.approx(37.6)
    panel.chorus_card.c_delay.knob.setValue(87.4, emit=True)
    assert dialog.current_params()["base_ms"] == pytest.approx(87.4)
    panel.btn_a.click()
    assert dialog.current_params() == flanger
    assert panel.flanger_card.isVisible()
    panel.reset_modulation()
    assert dialog.current_params() == initial | {
        "mode": "flanger", "feedback": 0.0, "voices": 2, "rate_hz": 0.6,
        "base_ms": 18.0, "depth_ms": 3.0, "mix": 0.5,
    }


def test_chorus_flanger_user_preset_is_available_after_reopening(
        qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(effect_preset, "_store_path", lambda: str(tmp_path / "fx.json"))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("My Flanger", True))
    saved = {"mode": "flanger", "feedback": -0.42, "base_ms": 3.0}
    source = ProductionEffectDialog("chorus_flanger", "Source", saved)
    qtbot.addWidget(source)
    save = next(button for button in source.panel.findChildren(QPushButton, "ToolBtn")
                if button.text() == "+")
    save.click()
    reopened = ProductionEffectDialog("chorus_flanger", "New track", {"enabled": False})
    qtbot.addWidget(reopened)
    combo = reopened.panel.preset_combo
    assert combo.findText("My Flanger") >= 0
    combo.setCurrentIndex(combo.findText("My Flanger"))
    assert reopened.current_params() == saved | {"enabled": False}


def test_legacy_flanger_retains_snapshot_and_limits_unsupported_controls(qtbot: QtBot) -> None:
    initial = {"mode": "flanger", "base_ms": 88.0, "feedback": -0.7}
    dialog = ProductionEffectDialog("chorus_flanger", "Legacy", initial, algorithm_version=4)
    qtbot.addWidget(dialog)
    card = dialog.panel.flanger_card
    assert dialog.current_params() == initial
    assert card.c_manual.knob._value == 50.0
    assert card.c_feedback.knob._value == 0.0
    combo = dialog.panel.preset_combo
    row = combo.findText(I18N.tr("audio.fx.flanger_inverted", "Inverted Flanger"))
    model = combo.model()
    index = model.index(row, 0)
    assert not model.flags(index) & Qt.ItemFlag.ItemIsEnabled
    dialog.set_algorithm_version(ALGORITHM_VERSION)
    assert dialog.current_params() == initial
    assert card.c_manual.knob._value == 88.0
    assert card.c_feedback.knob._value == -70.0
    assert model.flags(index) & Qt.ItemFlag.ItemIsEnabled
