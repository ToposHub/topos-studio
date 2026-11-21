"""Objective offline checks on the repository's dialogue/music/Foley MP3 assets."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy.signal import resample_poly

from src.features.edit.core.audio_edit import make_step, new_audio_edit, render_audio_edit_full

SOURCES = ("地下城堡人声.mp3", "地下城堡音乐.mp3", "地下城堡音效.mp3")
ROOT = Path(__file__).resolve().parents[2]


def decode_excerpt(path: Path) -> np.ndarray:
    import av

    chunks: list[np.ndarray] = []
    count = 0
    started = False
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                samples = converted.to_ndarray()
                if not started:
                    started = bool(np.max(np.abs(samples)) > 1e-3)
                    if not started:
                        continue
                chunks.append(samples)
                count += converted.samples
            if count >= 48000:
                break
    if not chunks:
        raise ValueError(f"No active audio in {path.name}")
    return np.ascontiguousarray(np.concatenate(chunks, axis=1)[:, :48000], dtype=np.float32)


def measure_excerpt(source: str, rate: int, pcm48: np.ndarray) -> list[dict]:
    divisor = int(np.gcd(rate, 48000))
    pcm = resample_poly(pcm48, rate // divisor, 48000 // divisor, axis=-1).astype(np.float32)
    # Persisted selection endpoints stay in the reference 48 kHz domain.
    reference_start, reference_end = 4800, 43200
    start, end = round(0.1 * rate), round(0.9 * rate)
    rows = []
    for processor, options in (
        ("de_click", {}),
        ("de_hum", {}),
        ("pitch_correction", {"scale_notes": list(range(12))}),
        ("doubler", {"voices": [{"interval_st": 0.0, "detune_cents": 6.0, "delay_ms": 12.0}]}),
    ):
        original = pcm.copy()
        edit = new_audio_edit([make_step(processor, reference_start, reference_end, [0], **options)])
        out = render_audio_edit_full(pcm, edit, sample_rate=rate)
        assert out.shape == original.shape and np.isfinite(out).all()
        np.testing.assert_array_equal(pcm, original)
        np.testing.assert_array_equal(out[1], original[1])
        np.testing.assert_array_equal(out[0, :start], original[0, :start])
        np.testing.assert_array_equal(out[0, end:], original[0, end:])
        rows.append({
            "source": source, "sample_rate": rate, "processor": processor,
            "input_samples": pcm.shape[1], "output_samples": out.shape[1],
            "input_peak_dbfs": float(20.0 * np.log10(max(float(np.max(np.abs(pcm))), 1e-12))),
            "output_peak_dbfs": float(20.0 * np.log10(max(float(np.max(np.abs(out))), 1e-12))),
            "delta_rms": float(np.sqrt(np.mean((out.astype(np.float64) - pcm) ** 2))),
            "unselected_channels_max_error": float(np.max(np.abs(out[1] - pcm[1]))),
            "outside_selection_max_error": float(max(np.max(np.abs(out[0, :start] - pcm[0, :start])),
                                                      np.max(np.abs(out[0, end:] - pcm[0, end:])))),
        })
    return rows


@pytest.fixture(scope="module")
def excerpts() -> dict[str, np.ndarray]:
    directory = ROOT / "tests" / "test_videos"
    if not all((directory / name).is_file() for name in SOURCES):
        pytest.skip("Repository dialogue/music/Foley MP3 assets are unavailable")
    return {name: decode_excerpt(directory / name) for name in SOURCES}


@pytest.mark.real_media
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_real_media_offline_processors(excerpts: dict[str, np.ndarray], source: str, rate: int) -> None:
    assert len(measure_excerpt(source, rate, excerpts[source])) == 4
