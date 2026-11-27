"""Verify the production convolution dialog and its stream resource lifecycle."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, EffectChain
from src.features.mixer.effects.production_dialog import ProductionEffectDialog


def _write_ir(path: Path) -> None:
    pcm = np.zeros((256, 4), dtype="<i2")
    pcm[32, 0], pcm[64, 1], pcm[108, 2], pcm[192, 3] = 24000, 16000, 8000, 4000
    with wave.open(str(path), "wb") as output:
        output.setparams((4, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(pcm.tobytes())


def test_drag_wav_draws_real_waveform_and_wires_apply_and_clear(
        qtbot: QtBot, tmp_path: Path) -> None:
    path = tmp_path / "matrix.wav"
    _write_ir(path)
    initial = {"future_metadata": {"keep": True}}
    dialog = ProductionEffectDialog("convolution_reverb", "Track", initial,
                                    algorithm_version=ALGORITHM_VERSION)
    qtbot.addWidget(dialog)
    dialog.show()
    panel = dialog.panel
    assert not panel._demo_timer.isActive()
    assert dialog.current_params() == initial
    panel.ir_graph.irFileDropped.emit(str(path))
    panel.true_stereo_button.click()
    qtbot.waitUntil(lambda: panel.ir_graph._ir_waveform is not None, timeout=5000)
    waveform = panel.ir_graph._ir_waveform
    assert waveform.resource.channels == 4
    assert waveform.resource.sample_rate == 48000
    assert len(waveform.left) == 256
    assert np.argmax(waveform.left) == 32
    assert np.argmax(waveform.right) == 108
    assert not panel._ir_preview_timer.isActive()
    assert not panel.ir_graph.grab().isNull()
    with qtbot.waitSignal(dialog.committed) as committed:
        dialog.apply()
    assert committed.args[0] == initial | {"ir_path": str(path), "true_stereo": True}
    panel.clear_ir_button.click()
    assert dialog.current_params()["ir_path"] == ""
    assert panel.ir_graph._ir_waveform is None
    assert not panel._ir_preview_timer.isActive()


@pytest.mark.parametrize("version", [4, ALGORITHM_VERSION])
def test_sparse_chain_snapshot_clears_previewed_ir_and_mix(tmp_path: Path, version: int) -> None:
    path = tmp_path / "matrix.wav"
    _write_ir(path)
    pcm = np.zeros((2, 512), dtype=np.float32)
    pcm[0, 0] = 0.8
    chain = EffectChain()
    edit = "convolution_reverb", {"ir_path": str(path), "wet": 1.0, "dry": 0.0}, True, "conv", version
    chain.set_params([edit])
    assert np.max(np.abs(chain.process(pcm) - pcm)) > 0.1
    chain.set_params([("convolution_reverb", {}, True, "conv", version)])
    np.testing.assert_array_equal(chain.process(pcm), pcm)
    chain.set_params([("convolution_reverb", {"ir_path": str(path)}, True, "conv", version)])
    assert chain._effects[0]._impl._wet_gain == pytest.approx(0.35)
    assert chain._effects[0]._impl._dry_gain == pytest.approx(1.0)
