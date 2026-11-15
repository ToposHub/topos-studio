"""H13：实时性能基准（复审 §5 H13 的可复现测量）。

口径与复审 real-app-probe 一致（mix_samples 逐帧调用），但离线测量：
- 配置 A：两新 FX（Voice Formant+Pitch / Mangler Dual Ring+Dist）全开
- 配置 B：不挂新 FX
输出各配置 mix_samples 的 p50/p95/p99/max 与音频时钟/墙钟比。
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")
from src.features.edit.core.audio_playback import (  # noqa: E402
    AudioDecoder, AudioMixer, AudioTrackState,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    EffectChainParams, SpectralManglerParams, TrackEffectInstance,
    VoiceChangerParams,
)


class _BenchDecoder(AudioDecoder):
    _channels = 2

    def __init__(self, *a, **k):
        pass

    def open(self, *a, **k):
        return True

    def decode_samples_for_frame(self, f, fps, n):
        return np.zeros((2, n), dtype=np.float32)

    def close(self):
        pass

SR = 48000
FPS = 25
BLOCK = SR // FPS


def build_chain(vc: bool, sm: bool) -> EffectChainParams:
    effects = []
    if vc:
        effects.append(TrackEffectInstance(
            "voice_changer",
            VoiceChangerParams(formant_enabled=True, formant_shift_st=2.0,
                               pitch_enabled=True, pitch_semitones=4.0,
                               random_seed=2026).to_dict(),
            instance_id="VC"))
    if sm:
        effects.append(TrackEffectInstance(
            "spectral_mangler",
            SpectralManglerParams(ring_enabled=True, ring_mode="dual",
                                  dist_enabled=True, dist_drive_db=6.0,
                                  random_seed=77).to_dict(),
            instance_id="SM"))
    return EffectChainParams(effects=tuple(effects))


def run(label: str, chain=None, seconds: float = 8.0) -> dict:
    mixer = AudioMixer()
    mixer.add_track("T", _BenchDecoder(), AudioTrackState(track_id="T"))
    if chain is not None:
        mixer.set_track_effect_chain("T", chain)
    durations = []
    total = int(SR * seconds)
    pos = 0
    frame = 0
    t0 = time.perf_counter()
    while pos < total:
        x = np.zeros((2, BLOCK), dtype=np.float32)
        x[0] = 0.2 * np.sin(2 * np.pi * 220.0 *
                            (pos + np.arange(BLOCK)) / SR)
        x[1] = x[0]
        s = time.perf_counter()
        mixer.mix_samples(frame, FPS, BLOCK)
        durations.append((time.perf_counter() - s) * 1000.0)
        pos += BLOCK
        frame += 1
    wall = time.perf_counter() - t0
    d = sorted(durations)
    out = {
        "p50_ms": round(d[len(d) // 2], 3),
        "p95_ms": round(d[int(len(d) * 0.95)], 3),
        "p99_ms": round(d[int(len(d) * 0.99)], 3),
        "max_ms": round(d[-1], 3),
        "audio_wall_ratio": round(seconds / wall, 3),
        "calls": len(d),
    }
    print(label, json.dumps(out))
    return out


if __name__ == "__main__":
    res = {
        "with_new_fx": run("A(两新FX)", build_chain(True, True)),
        "no_new_fx": run("B(无新FX)", None),
        "vc_only": run("C(Voice)", build_chain(True, False)),
        "sm_only": run("D(Mangler)", build_chain(False, True)),
    }
    out = Path("docs/audit/2026-11-15-audio-fx-post-integration/h13")
    out.mkdir(parents=True, exist_ok=True)
    (out / "bench-before.json").write_text(
        json.dumps(res, indent=1), encoding="utf-8")
