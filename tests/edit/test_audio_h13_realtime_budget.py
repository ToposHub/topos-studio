"""H13：实时性能预算回归守卫（复审 §5 H13）。

基线（复审 real-app 实测）：两新 FX 音频时钟 0.460x、mix p50≈71.97ms
（40ms 帧音频块预算击穿）。本批 DSP 向量化后（pitch 逐样本循环 → 段向
量化 Catmull-Rom gather；F0 自相关 O(n²)→FFT）实测 p50 ≈ 7–12ms、
p99 < 20ms、时钟比 > 3x。

守卫（宽松阈值防 CI 抖动）：同块长逐帧 mix_samples 下——
- 两新 FX 全开：p99 < 40ms（帧预算）且时钟比 > 1.0x；
- 单 FX 各自 p99 < 40ms。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from src.features.edit.core.audio_playback import (
    AudioDecoder,
    AudioMixer,
    AudioTrackState,
)
from src.features.edit.core.timeline_v2 import (
    EffectChainParams,
    SpectralManglerParams,
    TrackEffectInstance,
    VoiceChangerParams,
)

pytestmark = pytest.mark.edit_core
SR = 48000
FPS = 25
BLOCK = SR // FPS


class _SilentDecoder(AudioDecoder):
    _channels = 2

    def __init__(self, *a, **k):
        pass

    def open(self, *a, **k):
        return True

    def decode_samples_for_frame(self, f, fps, n):
        return np.zeros((2, n), dtype=np.float32)

    def close(self):
        pass


def _chain(vc: bool, sm: bool) -> EffectChainParams:
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


def _measure(chain, seconds: float = 4.0) -> dict:
    mixer = AudioMixer()
    mixer.add_track("T", _SilentDecoder(), AudioTrackState(track_id="T"))
    if chain is not None:
        mixer.set_track_effect_chain("T", chain)
    durations = []
    total = int(SR * seconds)
    pos = 0
    frame = 0
    t0 = time.perf_counter()
    while pos < total:
        s = time.perf_counter()
        mixer.mix_samples(frame, FPS, BLOCK)
        durations.append((time.perf_counter() - s) * 1000.0)
        pos += BLOCK
        frame += 1
    wall = time.perf_counter() - t0
    d = sorted(durations)
    return {
        "p50": d[len(d) // 2],
        "p99": d[int(len(d) * 0.99)],
        "ratio": seconds / max(1e-9, wall),
    }


@pytest.mark.parametrize("label,vc,sm", [
    ("both", True, True), ("voice", True, False), ("mangler", False, True)])
def test_fx_block_budget(label, vc, sm):
    m = _measure(_chain(vc, sm))
    assert m["p99"] < 40.0, (label, m)   # 帧预算（40ms@25fps）
    assert m["ratio"] > 1.0, (label, m)  # 至少 1x 实时


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
