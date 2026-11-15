"""E2E：两款新 FX 的实时/成片/Stem 三路径一致性（计划 §9 E2E-01）。

同工程、同 seed、同质量档：
1. 实时捕获 PCM（AudioMixer.mix_samples 直接取回主输出）；
2. 成片导出 ``_render_audio_master``；
3. MASTER Stem ``render_stem_pcm``（R03 共图）。

三方对齐（建议 ≤1e-5；当前 DSP 块边界状态推进一致，允许 2e-5 的
浮点累加误差）。种子确定性 + R02/R03 共享调度是该一致性的结构性来源。
"""

from __future__ import annotations

from unittest.mock import Mock

import numpy as np
import pytest

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import (
    AudioMixer,
    AudioTrackState,
)
from src.features.edit.core.stem_export import StemSpec, StemType, render_stem_pcm
from src.features.edit.core.timeline_export import (
    TimelineExportConfig,
    TimelineExporter,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    EffectChainParams,
    TimelineSequence,
    TrackEffectInstance,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core
SR = 48000
FPS = 25
BLOCK = SR // FPS  # 实时块=帧宽（解码起点与离线同口径）


class _ConstDecoder:
    _channels = 2

    def __init__(self, seed: int = 3):
        rng = np.random.default_rng(seed)
        self._sig = (rng.standard_normal((2, SR)) * 0.18).astype(np.float32)

    def decode_samples_for_frame(self, frame, fps, num_samples):
        start = int(frame * SR / max(1e-6, fps)) % SR
        idx = (start + np.arange(num_samples)) % SR
        return self._sig[:, idx].copy()

    def open(self, *a, **k):
        return True

    def close(self):
        pass


def _track(effect_type: str, params: dict) -> EditTrack:
    return EditTrack("T", "T", "audio", 0,
                     clips=(ClipInstance("c", "a", "T", 0, 40, 0, 40),),
                     effect_chain=EffectChainParams(effects=(
                         TrackEffectInstance(effect_type=effect_type,
                                             instance_id="X", params=params),
                     )))


def _capture_realtime(sequence, total: int) -> np.ndarray:
    mixer = AudioMixer()
    dec = _ConstDecoder()
    mixer.add_track("T", dec, AudioTrackState(track_id="T"))
    mixer.set_track_effect_chain("T", sequence.audio_tracks[0].effect_chain)
    out = np.zeros((2, total), dtype=np.float32)
    pos = 0
    frame = 0
    while pos < total:
        n = min(BLOCK, total - pos)
        chunk = mixer.mix_samples(frame, float(FPS), n)[0]
        out[:, pos:pos + n] = chunk
        pos += n
        frame += 1
    return out


def _vc_params() -> dict:
    from src.features.edit.core.timeline_v2 import VoiceChangerParams
    return VoiceChangerParams(pitch_enabled=True, pitch_semitones=4.0,
                              formant_shift_st=2.0,
                              pitch_jitter_pct=40.0,
                              random_seed=2026).to_dict()


def _sm_params() -> dict:
    from src.features.edit.core.timeline_v2 import SpectralManglerParams
    return SpectralManglerParams(ring_enabled=True, ring_carrier_hz=220.0,
                                 grain_enabled=True, grain_density_hz=30.0,
                                 grain_pitch_jitter_pct=50.0,
                                 grain_spray_pct=60.0,
                                 dist_enabled=True, dist_drive_db=6.0,
                                 random_seed=77).to_dict()


class TestThreePathConsistency:
    def test_voice_changer_realtime_export_stem_aligned(self):
        total = SR  # 1 秒
        seq = TimelineSequence(fps=Rational(FPS, 1),
                               audio_tracks=(_track("voice_changer",
                                                    _vc_params()),))
        # 1) 实时
        realtime = _capture_realtime(seq, total)
        # 2) Stem（MASTER 含 master）
        stem = render_stem_pcm(
            seq, StemSpec(StemType.MASTER, "Master", (),
                          include_master=True),
            decode_clip_fn=lambda t, c, f, fp, n:
            _ConstDecoder().decode_samples_for_frame(f, fp, n))
        # 3) 成片
        import src.features.edit.core.audio_playback as ap_mod
        from src.features.edit.core.project import EditProject, MediaAsset
        project = EditProject("e2e", "E", assets={
            "a": MediaAsset("a", "/tmp/e2e.wav", "audio", "A", {})})
        exporter = TimelineExporter(project, seq, Mock())
        orig_dec = ap_mod.AudioDecoder
        ap_mod.AudioDecoder = _ConstDecoder
        try:
            exported = exporter._render_audio_master(
                0, 40, TimelineExportConfig(output_path="/tmp/e2e.mov"),
                encoder=Mock())
        finally:
            ap_mod.AudioDecoder = orig_dec
        assert exported is not None
        n = min(total, stem.shape[-1], exported.shape[-1])
        np.testing.assert_allclose(realtime[:, :n], stem[:, :n], atol=2e-5)
        np.testing.assert_allclose(exported[:, :n], stem[:, :n], atol=2e-5)

    def test_spectral_mangler_realtime_export_stem_aligned(self):
        total = SR
        seq = TimelineSequence(fps=Rational(FPS, 1),
                               audio_tracks=(_track("spectral_mangler",
                                                    _sm_params()),))
        realtime = _capture_realtime(seq, total)
        stem = render_stem_pcm(
            seq, StemSpec(StemType.MASTER, "Master", (),
                          include_master=True),
            decode_clip_fn=lambda t, c, f, fp, n:
            _ConstDecoder().decode_samples_for_frame(f, fp, n))
        import src.features.edit.core.audio_playback as ap_mod
        from src.features.edit.core.project import EditProject, MediaAsset
        project = EditProject("e2e", "E", assets={
            "a": MediaAsset("a", "/tmp/e2e2.wav", "audio", "A", {})})
        exporter = TimelineExporter(project, seq, Mock())
        orig_dec = ap_mod.AudioDecoder
        ap_mod.AudioDecoder = _ConstDecoder
        try:
            exported = exporter._render_audio_master(
                0, 40, TimelineExportConfig(output_path="/tmp/e2e2.mov"),
                encoder=Mock())
        finally:
            ap_mod.AudioDecoder = orig_dec
        assert exported is not None
        n = min(total, stem.shape[-1], exported.shape[-1])
        np.testing.assert_allclose(realtime[:, :n], stem[:, :n], atol=2e-5)
        np.testing.assert_allclose(exported[:, :n], stem[:, :n], atol=2e-5)

    def test_project_roundtrip_preserves_new_fx(self):
        """工程 to_dict→from_dict：两款新 FX 实例/参数/seed 无损往返。"""
    # 序列化往返
        seq = TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(
            _track("voice_changer", _vc_params()),
            _track("spectral_mangler", _sm_params()),))
        rt = TimelineSequence.from_dict(seq.to_dict())
        types = [e.effect_type for t in rt.audio_tracks
                 for e in t.effect_chain.effects]
        assert types == ["voice_changer", "spectral_mangler"]
        vc_params = rt.audio_tracks[0].effect_chain.effects[0].params
        assert vc_params["random_seed"] == 2026
        assert vc_params["pitch_semitones"] == 4.0
        sm_params = rt.audio_tracks[1].effect_chain.effects[0].params
        assert sm_params["random_seed"] == 77
        assert sm_params["ring_carrier_hz"] == 220.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
