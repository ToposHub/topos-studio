"""Long sources keep their full extent; detail reads are bounded and asynchronous."""
from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.audio_waveform import WaveformData
from src.features.edit.core.clip_pcm import (
    WAVEFORM_WINDOW_MAX_BYTES,
    decode_clip_waveform_window,
    full_clip_pcm_available,
    resolve_asset_audio_spec,
)
from src.features.edit.core.project import MediaAsset


RATE = 48000
DURATION = 480
PULSE = 420 * RATE + 123


def _long_source(tmp_path: Path) -> MediaAsset:
    path = tmp_path / "eight_minutes.wav"
    samples = RATE * DURATION
    with path.open("wb") as stream:
        stream.write(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + samples * 2,
                                 b"WAVE", b"fmt ", 16, 1, 1, RATE, RATE * 2, 2,
                                 16, b"data", samples * 2))
        stream.truncate(44 + samples * 2)
        stream.seek(44 + PULSE * 2)
        stream.write(struct.pack("<h", 24576))
    return MediaAsset("long", str(path), "audio", "Long source",
                      {"duration_seconds": DURATION, "audio_info": {"channels": 1, "sample_rate": RATE}})


def test_source_spec_never_silently_truncates_to_five_minutes(tmp_path: Path) -> None:
    asset = _long_source(tmp_path)
    spec = resolve_asset_audio_spec(None, asset)
    assert spec is not None
    assert spec.end_sample == DURATION * RATE
    assert spec.display_duration == DURATION
    assert not full_clip_pcm_available(spec)
    assert resolve_asset_audio_spec(None, replace(asset, metadata={})) is None


def test_seven_minute_source_window_has_absolute_position_and_bounded_pcm(tmp_path: Path) -> None:
    spec = resolve_asset_audio_spec(None, _long_source(tmp_path))
    window = decode_clip_waveform_window(spec, 420 * RATE, 420 * RATE + 480)
    assert window is not None
    assert window.pcm.nbytes <= WAVEFORM_WINDOW_MAX_BYTES
    assert window.pcm.shape[1] <= RATE + 480
    assert window.start_sample + np.argmax(window.pcm[0]) == PULSE
    assert window.pcm.max() == pytest.approx(0.75)
    assert not window.pcm.flags.writeable
    assert decode_clip_waveform_window(spec, 0, spec.sample_count) is None
    assert decode_clip_waveform_window(spec, 420 * RATE, 420 * RATE + 480,
                                       cancel_check=lambda: True) is None


def test_reverse_window_maps_before_decode_without_changing_clip_coordinates(tmp_path: Path) -> None:
    spec = replace(resolve_asset_audio_spec(None, _long_source(tmp_path)), reverse=True)
    reverse_pulse = spec.sample_count - 1 - PULSE
    window = decode_clip_waveform_window(spec, reverse_pulse - 10, reverse_pulse + 11)
    assert window is not None
    assert window.start_sample + np.argmax(window.pcm[0]) == reverse_pulse


@pytest.mark.parametrize("known_duration", [True, False])
def test_editor_uses_window_worker_without_attaching_partial_processing_pcm(qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, known_duration: bool) -> None:
    from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

    asset = _long_source(tmp_path)
    if not known_duration:
        asset = replace(asset, metadata={"audio_info": {"channels": 1, "sample_rate": RATE}})
    overview = WaveformData(asset.asset_id, np.zeros(DURATION), np.zeros(DURATION),
                            1.0, DURATION, channels=1, source_path=asset.path)
    editor = ProjectClipEditor()
    qtbot.addWidget(editor)
    monkeypatch.setattr(editor, "_cached_source_waveform", lambda: overview)
    editor.resize(1000, 650)
    editor.show()
    editor.open_source(asset)
    panel = editor._panel
    assert panel._duration == DURATION
    assert panel.canvas._duration == DURATION
    assert panel._raw_pcm is None and panel._pcm is None
    assert not panel.btn_play.isEnabled()
    assert "300" in panel.btn_play.toolTip()
    assert editor._pcm_provider._active_key != editor._pcm_spec_key
    canvas = panel.canvas
    # This public viewport setter is also used by zoom/scroll interactions.
    canvas.setViewport(420.0, 420.01)
    qtbot.waitUntil(lambda: canvas._waveform_window_rate is not None, timeout=5000)
    pcm = canvas._waveform_samples
    position = canvas._waveform_window_start + int(np.argmax(pcm[0])) / canvas._waveform_window_rate
    assert position == pytest.approx(PULSE / RATE, abs=1 / RATE)
    assert canvas._duration == DURATION and panel._duration == DURATION
    assert panel._raw_pcm is None and panel._pcm is None
    # Data coordinates remain global. A window cannot move a 7-minute peak to t=0.
    times = np.array([PULSE / RATE - 0.5 / RATE, PULSE / RATE + 1.5 / RATE])
    high, _ = canvas._visible_wave_extrema((0,), times, pcm, None)
    assert high.max() == pytest.approx(0.75)
    worker = editor._source_detail_worker
    editor.reset()
    assert editor._windowed_waveform_spec is None
    assert canvas._waveform_samples is None
    editor.close()
    assert worker.wait(2000)


def test_slow_playback_cannot_bypass_full_preview_duration_budget(tmp_path: Path) -> None:
    spec = resolve_asset_audio_spec(None, _long_source(tmp_path))
    # 240 seconds of source stretched to an 8-minute clip still needs a
    # streaming processor; source-size checks alone are insufficient.
    assert not full_clip_pcm_available(replace(spec, end_sample=240 * RATE))


def test_detail_can_be_drawn_before_full_source_envelope_finishes(qtbot) -> None:
    from src.features.mixer.ui.project_clip_editor import ProjectClipEditor
    editor = ProjectClipEditor()
    qtbot.addWidget(editor)
    canvas = editor._ensure_panel().canvas
    canvas.loadAudioData(RATE, DURATION, [], [], [])
    canvas.setWaveformWindow(np.ones((1, 480), np.float32), 420.0, RATE)
    channels, muted = canvas._waveform_display_channels()
    assert channels == (0,) and not muted
    assert canvas._duration == DURATION
    editor.close()


def test_window_rejects_source_replaced_after_spec_resolution(tmp_path: Path) -> None:
    asset = _long_source(tmp_path)
    spec = resolve_asset_audio_spec(None, asset)
    Path(asset.path).write_bytes(b"replaced source")
    assert decode_clip_waveform_window(spec, 420 * RATE, 420 * RATE + 480) is None
