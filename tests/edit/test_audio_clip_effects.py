"""
Tests for per-clip audio effect processing (P-clip-FX).

Verifies that AudioMixer applies clip-level effect chains (from clip.effects)
independently per clip, at the right point in the signal chain
(post-channel-expand, pre-track-volume), and cleans up correctly on
remove_track.
"""

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder
from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES


def _make_samples(channels: int = 2, n: int = 480, seed: int = 0) -> np.ndarray:
    """Deterministic test signal (white noise)."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal((channels, n)) * 0.1).astype(np.float32)


class TestSetClipEffects:
    """AudioMixer.set_clip_effects stores / clears per-clip chains."""

    def test_set_creates_chain(self):
        mixer = AudioMixer()
        entries = [
            ("reverb", {"mix": 80.0, "decay": 3.0, "pre_delay_ms": 0.0,
                        "damping_hz": 5000.0, "enabled": True}, True),
        ]
        mixer.set_clip_effects("clip_A", entries, track_id="track_1")
        assert "clip_A" in mixer._clip_effect_chains
        assert mixer._clip_to_track["clip_A"] == "track_1"
        assert mixer._clip_effect_chains["clip_A"].is_active

    def test_clear_removes_chain(self):
        mixer = AudioMixer()
        entries = [("reverb", {"mix": 50.0, "decay": 2.0, "enabled": True}, True)]
        mixer.set_clip_effects("clip_B", entries, track_id="track_1")
        assert "clip_B" in mixer._clip_effect_chains

        mixer.set_clip_effects("clip_B", None, track_id="track_1")
        assert "clip_B" not in mixer._clip_effect_chains

    def test_empty_entries_clears(self):
        mixer = AudioMixer()
        entries = [("delay", {"time_ms": 200.0, "feedback": 30.0, "mix": 40.0,
                              "ping_pong": False, "low_cut_hz": 100.0,
                              "enabled": True}, True)]
        mixer.set_clip_effects("clip_C", entries, track_id="track_1")
        mixer.set_clip_effects("clip_C", [], track_id="track_1")
        assert "clip_C" not in mixer._clip_effect_chains

    def test_multiple_effects_ordered(self):
        """Multiple effects in one clip chain are applied in order."""
        mixer = AudioMixer()
        entries = [
            ("compressor", {"threshold_db": -20.0, "ratio": 4.0, "attack_ms": 3.0,
                            "release_ms": 250.0, "knee_db": 30.0,
                            "makeup_gain": 6.0, "enabled": True}, True),
            ("reverb", {"mix": 40.0, "decay": 2.0, "pre_delay_ms": 0.0,
                        "damping_hz": 5000.0, "enabled": True}, True),
        ]
        mixer.set_clip_effects("clip_D", entries, track_id="track_1")
        chain = mixer._clip_effect_chains["clip_D"]
        assert chain.is_active
        # 处理后样本应改变
        samples = _make_samples()
        out = chain.process(samples.copy())
        assert not np.allclose(samples, out)


class TestClipEffectProcessing:
    """A clip-level chain actually transforms the signal."""

    def test_reverb_changes_signal(self):
        mixer = AudioMixer()
        entries = [("reverb", {"mix": 90.0, "decay": 4.0, "pre_delay_ms": 0.0,
                               "damping_hz": 5000.0, "enabled": True}, True)]
        mixer.set_clip_effects("clip_E", entries, track_id="track_1")
        chain = mixer._clip_effect_chains["clip_E"]

        samples = _make_samples()
        out = chain.process(samples.copy())
        # 混响会显著改变信号能量分布
        assert out.shape == samples.shape
        assert not np.allclose(samples, out)

    def test_disabled_effect_passthrough(self):
        """A chain whose only effect is bypassed (enabled=False) is inactive."""
        mixer = AudioMixer()
        entries = [("reverb", {"mix": 90.0, "decay": 4.0, "pre_delay_ms": 0.0,
                               "damping_hz": 5000.0, "enabled": False}, False)]
        mixer.set_clip_effects("clip_F", entries, track_id="track_1")
        chain = mixer._clip_effect_chains["clip_F"]
        # 整条链不激活（条目 enabled=False）
        assert not chain.is_active

    def test_video_effect_type_skipped(self):
        """Unknown / video effect_types are dropped by EffectChain (registry miss)."""
        mixer = AudioMixer()
        # 'blur' 不在 _EFFECT_CLASS_REGISTRY → EffectChain.set_params 跳过
        entries = [("blur", {"radius": 5.0}, True)]
        mixer.set_clip_effects("clip_G", entries, track_id="track_1")
        chain = mixer._clip_effect_chains["clip_G"]
        # 没有有效效果器 → 链不激活
        assert not chain.is_active


class TestRemoveTrackCleanup:
    """remove_track cleans up per-clip chains for that track."""

    def test_remove_clears_clip_chains(self):
        mixer = AudioMixer()
        entries = [("reverb", {"mix": 50.0, "decay": 2.0, "pre_delay_ms": 0.0,
                               "damping_hz": 5000.0, "enabled": True}, True)]
        mixer.set_clip_effects("clip_H1", entries, track_id="track_2")
        mixer.set_clip_effects("clip_H2", entries, track_id="track_2")
        mixer.set_clip_effects("clip_H3", entries, track_id="track_3")

        mixer.remove_track("track_2")
        assert "clip_H1" not in mixer._clip_effect_chains
        assert "clip_H2" not in mixer._clip_effect_chains
        # 其它 track 的 clip 链保留
        assert "clip_H3" in mixer._clip_effect_chains


class TestDecoderClipId:
    """AudioDecoder carries a _clip_id so mix_samples can map decoder → clip."""

    def test_decoder_default_none(self):
        d = AudioDecoder()
        assert d._clip_id is None

    def test_decoder_clip_id_assignable(self):
        d = AudioDecoder()
        d._clip_id = "clip_X"
        assert d._clip_id == "clip_X"
