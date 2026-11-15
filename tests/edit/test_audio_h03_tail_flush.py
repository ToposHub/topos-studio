"""H03：全路径尾音/latency flush/导出长度（复审 §5 H03）。

修前复现（state-export 探针）：
- 200ms Track Delay 在 include_fx_tail 模式只输出 160ms 且全零（轨尾音
  截在选区内；预算只取 Bus/Master max）；
- Bus200ms→Master200ms 的首回声（400ms 处）被截断；
- clip 级 Delay：成片峰 0.344 / Stem 0 / 实时 0（三路径不一致）；
- Voice 报 tail=0，末端脉冲滞留 WOLA 永远丢失。

修后契约：
- include_fx_tail 预算 = 轨链尾 + 沿途 Bus 链尾之和 + Master 链尾（串联
  累计）；轨 drain 延长进扩展区（clip 尾音同理）；
- Stem 输出长度按同一全路径预算（含 clip 级链尾）延长；
- 实时 mix_samples 对已结束 clip 的链按预算喂零；
- 新 FX 的 tail_estimate 报告真实 flush 需求（Voice=WOLA 时延；
  Mangler=max(颗粒尾, dist 时延)）。
"""

from __future__ import annotations

from unittest.mock import Mock

import numpy as np
import pytest

from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.project import EditProject, MediaAsset
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
from src.features.edit.core.effect_instance import EffectInstance
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core
FPS = 25
SR = 48000
SPF = SR // FPS


def _delay_chain(iid: str = "D", ms: float = 200.0) -> EffectChainParams:
    return EffectChainParams(effects=(
        TrackEffectInstance("delay", {
            "enabled": True, "left_delay": ms, "right_delay": ms,
            "left_feedback": 0.0, "right_feedback": 0.0, "dry_wet": 100.0,
            "level_db": 0.0, "low_cut": 20.0, "high_cut": 20000.0,
            "high_ratio": 0.0, "stereo_blend": 0.0,
            "input_format": "Stereo"}, instance_id=iid),
    ))


def _project() -> EditProject:
    return EditProject("p", "p", assets={
        "a": MediaAsset("a", "/tmp/h03.wav", "audio", "A", {})})


def _export(seq, s: int, e: int, tail: bool = False) -> np.ndarray:
    out = TimelineExporter(_project(), seq, Mock())._render_audio_master(
        s, e, TimelineExportConfig(output_path="/tmp/h03.mov",
                                   include_fx_tail=tail),
        encoder=Mock())
    assert out is not None
    return np.asarray(out)


class TestTrackTailExtension:
    def _pulse_seq(self, chain: EffectChainParams = None,
                   clip_fx=None) -> TimelineSequence:
        clip = ClipInstance("c", "a", "T", 0, 2, 0, 2)
        if clip_fx is not None:
            clip = ClipInstance("c", "a", "T", 0, 2, 0, 2, effects=(clip_fx,))
        track = EditTrack("T", "T", "audio", 0, clips=(clip,))
        if chain is not None:
            track = track.with_effect_chain(chain)
        return TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(track,))

    def test_track_delay_tail_present(self):
        import src.features.edit.core.audio_playback as ap

        seq = self._pulse_seq(chain=_delay_chain())
        orig = ap.AudioDecoder

        class _Dec(ap.AudioDecoder):
            _channels = 2

            def __init__(self, *a, **k):
                pass

            def open(self, *a, **k):
                return True

            def decode_samples_for_frame(self, f, fps, n):
                out = np.zeros((2, n), dtype=np.float32)
                if f == 0:
                    out[:, 0] = 0.5
                return out if f < 2 else None

            def close(self):
                pass

        ap.AudioDecoder = _Dec
        try:
            out = _export(seq, 0, 4, tail=True)
        finally:
            ap.AudioDecoder = orig
        # 200ms 回声落在 f5=选区外——扩展区必须包含它
        idx = np.flatnonzero(np.abs(out[0]) > 1e-4)
        assert idx.size > 0
        echo_ms = idx[0] * 1000.0 / SR
        assert 190.0 <= echo_ms <= 210.0
        # 0.5 脉冲 × Delay 输入 FIR（0.688）≈ 0.344（复审探针同源值）
        assert float(np.abs(out).max()) == pytest.approx(0.344, abs=5e-3)
        # 严格模式不延长
        strict = _export(seq, 0, 4, tail=False)
        assert strict.shape[-1] == 4 * SPF

    def test_bus_master_series_tail(self):
        """Bus200→Master200：首回声在 400ms 完整保留。"""
        import src.features.edit.core.audio_playback as ap

        t = EditTrack("T", "T", "audio", 0,
                      clips=(ClipInstance("c", "a", "T", 0, 2, 0, 2),))
        seq = TimelineSequence(
            fps=Rational(FPS, 1),
            audio_tracks=(t.with_output_bus("B"),
                          EditTrack("B", "B", "audio", 1, is_bus=True,
                                    effect_chain=_delay_chain("B"))),
            master_effect_chain=_delay_chain("M"))
        orig = ap.AudioDecoder

        class _Dec(ap.AudioDecoder):
            _channels = 2

            def __init__(self, *a, **k):
                pass

            def open(self, *a, **k):
                return True

            def decode_samples_for_frame(self, f, fps, n):
                out = np.zeros((2, n), dtype=np.float32)
                if f == 0:
                    out[:, 0] = 0.5
                return out if f < 2 else None

            def close(self):
                pass

        ap.AudioDecoder = _Dec
        try:
            out = _export(seq, 0, 4, tail=True)
        finally:
            ap.AudioDecoder = orig
        idx = np.flatnonzero(np.abs(out[0]) > 1e-4)
        assert idx.size > 0
        echo_ms = idx[0] * 1000.0 / SR
        assert 390.0 <= echo_ms <= 410.0


class TestClipTailThreePaths:
    def _clip_fx_delay(self) -> EffectInstance:
        return EffectInstance(
            effect_type="delay", params=_delay_chain().effects[0].params,
            effect_id="CF")

    def _seq(self) -> TimelineSequence:
        c = ClipInstance("c", "a", "T", 0, 2, 0, 2,
                         effects=(self._clip_fx_delay(),))
        return TimelineSequence(
            fps=Rational(FPS, 1),
            audio_tracks=(EditTrack("T", "T", "audio", 0, clips=(c,)),))

    @staticmethod
    def _decode(t, c, f, fp, n):
        y = np.zeros((2, n), np.float32)
        if f == 0:
            y[:, 0] = 0.5
        return y

    def test_stem_clip_tail(self):
        stem = render_stem_pcm(
            self._seq(), StemSpec(StemType.MASTER, "M", ()),
            decode_clip_fn=self._decode)
        # 回声幅度 = 0.5 脉冲 × Delay 输入 FIR 响应（0.688——复审探针的
        # 0.344 同源）；三路径一致才是本断言的目标
        assert float(np.abs(stem).max()) == pytest.approx(0.344, abs=5e-3)
        idx = np.flatnonzero(np.abs(stem[0]) > 1e-4)
        assert 190.0 <= idx[0] * 1000.0 / SR <= 210.0

    def test_live_clip_tail(self):
        class _Dec:
            _channels = 2
            _clip_id = "c"
            _timeline_start = 0
            _timeline_duration = 2
            _source_channel_map = None
            _gain_db = 0.0
            _speed = 1.0

            def decode_samples_for_frame(self, f, fps, n):
                return self_dec(f, fps, n) if f < 2 else None

            def close(self):
                pass

        def self_dec(f, fps, n):
            y = np.zeros((2, n), np.float32)
            if f == 0:
                y[:, 0] = 0.5
            return y

        mixer = AudioMixer()
        mixer.add_track("T", _Dec(), AudioTrackState(track_id="T"))
        mixer.set_clip_effects("c", _delay_chain().to_dsp_entries())
        live = np.concatenate(
            [mixer.mix_samples(f, FPS, SPF)[0] for f in range(10)], axis=-1)
        assert float(np.abs(live).max()) == pytest.approx(0.344, abs=5e-3)
        idx = np.flatnonzero(np.abs(live[0]) > 1e-4)
        assert 190.0 <= idx[0] * 1000.0 / SR <= 210.0


class TestNewFxTailReporting:
    def test_voice_reports_latency_flush_need(self):
        """Voice tail_estimate = 报告时延（WOLA 滞留 flush 需求）。"""
        from src.features.edit.core.audio_effects.voice_changer import (
            VoiceChangerEffect,
        )
        from src.features.edit.core.timeline_v2 import VoiceChangerParams
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams().to_dict())
        assert fx.tail_estimate_samples() == fx.latency_samples() == 1024

    def test_voice_end_pulse_survives_via_drain(self):
        """末端脉冲经尾音预算喂零后完整出现（修前 tail=0 永丢）。

        中性参数（H04 恒等）下脉冲在 延迟 1024 处精确重构。
        """
        from src.features.edit.core.audio_effects.voice_changer import (
            VoiceChangerEffect,
        )
        from src.features.edit.core.timeline_v2 import VoiceChangerParams
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams(
            formant_enabled=True, formant_shift_st=0.0,
            pitch_enabled=False, vocoder_enabled=False).to_dict())
        x = np.zeros((2, 8000))
        x[:, 7999] = 0.7
        y = fx.process(x.copy())
        tail_budget = fx.tail_estimate_samples()
        tail = fx.process(np.zeros((2, tail_budget), dtype=np.float32))
        total = np.concatenate([y, tail], axis=-1)
        late = total[:, 8000:]
        assert float(np.abs(late).max()) == pytest.approx(0.7, abs=1e-6)
        assert int(np.argmax(np.abs(late[0]))) == 1023  # 7999+1024−8000

    def test_mangler_dist_latency_in_tail(self):
        from src.features.edit.core.audio_effects.spectral_mangler import (
            SpectralManglerEffect,
        )
        from src.features.edit.core.timeline_v2 import SpectralManglerParams
        fx = SpectralManglerEffect(SR)
        fx.set_params(SpectralManglerParams(
            dist_enabled=True, grain_enabled=False).to_dict())
        assert fx.tail_estimate_samples() >= 30  # dist FIR 群延迟


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
