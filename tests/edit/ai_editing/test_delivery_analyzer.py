"""Frame/audio delivery analyzer tests with deterministic ffmpeg fixtures."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.delivery_analyzer import FfmpegDeliveryAnalyzer


def _runner_for(video_frames: np.ndarray, audio: np.ndarray | None = None):
    raw_video = video_frames.astype(np.uint8).tobytes()
    raw_audio = (audio if audio is not None else np.zeros(1000, dtype=np.float32)).astype(np.float32).tobytes()

    def runner(args, **_kwargs):
        if "rawvideo" in args:
            return SimpleNamespace(returncode=0, stdout=raw_video)
        return SimpleNamespace(returncode=0, stdout=raw_audio)

    return runner


def test_analyzer_measures_frames_and_audio(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    frames = np.stack([
        np.full((4, 4), 40 + index * 20, dtype=np.uint8)
        for index in range(6)
    ])
    audio = np.full(1000, 0.25, dtype=np.float32)
    analyzer = FfmpegDeliveryAnalyzer(
        frame_size=(4, 4), sample_fps=1.0, frozen_run_samples=3,
        audio_window_seconds=0.1, runner=_runner_for(frames, audio),
    )

    result = analyzer.analyze(output, audio_present=True, audio_sample_rate=1000)

    assert result.succeeded
    assert result.sampled_frame_count == 6
    assert result.black_frame_count == 0
    assert result.frozen_frame_count == 0
    assert result.audio_present is True
    assert result.peak_db == pytest.approx(20 * np.log10(0.25), abs=0.01)
    assert result.silence_seconds == 0.0
    assert result.integrated_lufs is not None
    assert result.loudness_backend_available is True


def test_analyzer_detects_black_frozen_and_silent_output(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    frames = np.zeros((6, 4, 4), dtype=np.uint8)
    audio = np.zeros(1000, dtype=np.float32)
    analyzer = FfmpegDeliveryAnalyzer(
        frame_size=(4, 4), sample_fps=1.0, frozen_run_samples=3,
        audio_window_seconds=0.1, runner=_runner_for(frames, audio),
    )

    result = analyzer.analyze(output, audio_present=True, audio_sample_rate=1000)

    assert result.succeeded
    assert result.black_frame_count == 6
    assert result.frozen_frame_count >= 1
    assert result.audio_present is True
    assert result.peak_db == -np.inf
    assert result.silence_seconds == pytest.approx(1.0)
    assert result.to_dict()["peak_db"] is None


def test_analyzer_marks_decode_failure_as_corrupt(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    def failed_runner(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout=b"")

    analyzer = FfmpegDeliveryAnalyzer(frame_size=(4, 4), runner=failed_runner)
    result = analyzer.analyze(output)

    assert not result.succeeded
    assert result.corrupt_frame_count == 1
    assert result.error_code == "FRAME_DECODE_FAILED"
