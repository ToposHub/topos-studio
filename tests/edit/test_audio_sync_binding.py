"""F2.10 · 摄影机视频与独立录音的源级同步绑定。

验收锁定：
- 模型/命令/持久化：绑定单步撤销、WAV 必须存在且为 audio（不误绑）、
  重开恢复、未知载荷保留。
- 对齐：真实 WAV（机内声 vs 提前开录 WAV）恢复 offset；不相关 →
  拒绝绑定；跨采样率显式归一后仍可对齐。
- 消费：resolve_audio_source 五路统一口径；播放/导出按绑定源取声并按
  偏移折算；保留机内声 = 不切换。
- 非破坏：全程只写 project dict。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.audio_sync import (
    AudioSyncBinding,
    binding_for_asset,
    bound_audio_frame,
    deserialize_audio_sync_bindings,
    resolve_audio_source,
    serialize_audio_sync_bindings,
    wav_timecode_seconds,
)
from src.features.edit.core.commands import UpdateAudioSyncBindingCommand
from src.features.edit.core.project import EditProject, MediaAsset

SR = 48000


def _write_wav(path: Path, pcm: np.ndarray, sample_rate: int = SR) -> Path:
    import av

    channels = pcm.shape[0]
    layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels}c")
    container = av.open(str(path), mode="w", format="wav")
    stream = container.add_stream("pcm_s16le", rate=sample_rate)
    stream.layout = layout
    block = sample_rate // 5
    for start in range(0, pcm.shape[1], block):
        chunk = np.clip(pcm[:, start:start + block], -1.0, 1.0)
        frame = av.AudioFrame.from_ndarray(
            chunk.astype(np.float32), format="fltp", layout=layout)
        frame.sample_rate = sample_rate
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()
    return path


def _tone(seconds: float, freq: float = 440.0, amp: float = 0.3,
          seed: int = 0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    rng = np.random.default_rng(seed)
    sig = amp * np.sin(2 * np.pi * freq * t) + 0.02 * rng.standard_normal(t.size)
    return np.stack([sig.astype(np.float32), sig.astype(np.float32)])


def _broadband(seconds: float, seed: int = 0) -> np.ndarray:
    """宽带伪随机语音样信号（互相关单峰唯一——纯正弦是周期信号会被
    F2.6 歧义守卫正确拒绝，不能当对齐夹具）。"""
    rng = np.random.default_rng(seed)
    # 低通滤波噪声（语音样包络，避免高频白噪相关退化）
    from scipy.signal import resample_poly
    coarse = rng.standard_normal(int(seconds * SR / 8)) * 0.4
    sig = resample_poly(coarse, 8, 1)
    sig = sig[:int(seconds * SR)]
    return np.stack([sig.astype(np.float32), sig.astype(np.float32)])


def _project() -> EditProject:
    video = MediaAsset(
        "vid", "/tmp/cam.mp4", "video", "cam",
        metadata={"fps": 24.0, "duration": 20.0,
                  "audio_info": {"channels": 2, "sample_rate": SR}})
    wav = MediaAsset(
        "wav1", "/tmp/external.wav", "audio", "ext",
        metadata={"duration": 30.0,
                  "audio_info": {"channels": 2, "sample_rate": SR}})
    return EditProject("p", "P", assets={"vid": video, "wav1": wav})


class TestBindingModel:
    def test_apply_undo_roundtrip(self) -> None:
        project = _project()
        binding = AudioSyncBinding(
            wav_asset_id="wav1", offset_seconds=3.5, method="waveform",
            confidence=0.87)
        cmd = UpdateAudioSyncBindingCommand.create("vid", binding)
        applied = cmd.apply(project)
        assert applied.audio_sync_bindings["vid"] == binding
        undone = cmd.undo(applied)
        assert undone.audio_sync_bindings == {}

    def test_non_audio_or_missing_wav_rejected(self) -> None:
        project = _project()
        with pytest.raises(ValueError, match="not audio"):
            UpdateAudioSyncBindingCommand.create("vid", AudioSyncBinding(
                wav_asset_id="vid", offset_seconds=1.0)).apply(project)
        with pytest.raises(ValueError, match="not found"):
            UpdateAudioSyncBindingCommand.create("vid", AudioSyncBinding(
                wav_asset_id="missing", offset_seconds=1.0)).apply(project)

    def test_invalid_offset_rejected(self) -> None:
        project = _project()
        with pytest.raises(ValueError):
            UpdateAudioSyncBindingCommand.create("vid", AudioSyncBinding(
                wav_asset_id="wav1", offset_seconds=float("nan"))).apply(
                project)

    def test_keep_camera_audio_not_resolved(self) -> None:
        """保留机内参考声 = 绑定留存但音频消费不切换。"""
        project = _project()
        binding = AudioSyncBinding(
            wav_asset_id="wav1", offset_seconds=3.5, keep_camera_audio=True)
        project = UpdateAudioSyncBindingCommand.create(
            "vid", binding).apply(project)
        assert binding_for_asset(project.audio_sync_bindings, "vid") is None
        path, offset = resolve_audio_source(project, project.assets["vid"])
        assert path == "/tmp/cam.mp4" and offset == 0.0

    def test_resolve_returns_wav_and_offset(self) -> None:
        project = _project()
        binding = AudioSyncBinding(wav_asset_id="wav1", offset_seconds=3.5)
        project = UpdateAudioSyncBindingCommand.create(
            "vid", binding).apply(project)
        path, offset = resolve_audio_source(project, project.assets["vid"])
        assert path == "/tmp/external.wav" and offset == 3.5

    def test_bound_audio_frame_math(self) -> None:
        assert bound_audio_frame(100, 2.5, 24.0) == 160
        assert bound_audio_frame(100, 0.0, 24.0) == 100

    def test_persistence_roundtrip_and_unknown_payload(self) -> None:
        binding = AudioSyncBinding(
            wav_asset_id="wav1", offset_seconds=3.5, method="waveform",
            confidence=0.9, audio_channel_map=((2, 0),))
        saved = serialize_audio_sync_bindings({"vid": binding})
        restored = deserialize_audio_sync_bindings(saved)
        assert restored == {"vid": binding}
        garbage = {"x": {"alien": 1}}
        assert deserialize_audio_sync_bindings(garbage) == garbage


class TestWaveformAlignment:
    def test_real_wavs_recover_offset(self, tmp_path) -> None:
        """机内声 = 外录内容的 2 秒后段（外录提前 2s 开录）。"""
        from src.features.edit.core.multicam_sync import (
            compute_audio_binding_alignment,
        )
        full = _broadband(30.0, seed=1)
        camera = full[:, int(2.0 * SR):]   # 机内声少了前 2 秒
        wav_path = _write_wav(tmp_path / "cam.wav", camera)
        ext_path = _write_wav(tmp_path / "ext.wav", full)
        alignment = compute_audio_binding_alignment(
            str(wav_path), str(ext_path),
            analysis_seconds=10.0, max_shift_seconds=10.0)
        assert alignment.rejection == "" and not alignment.error
        assert alignment.lag_seconds == pytest.approx(2.0, abs=0.05)

    def test_uncorrelated_rejected(self, tmp_path) -> None:
        from src.features.edit.core.multicam_sync import (
            compute_audio_binding_alignment,
        )
        a = _write_wav(tmp_path / "a.wav", _tone(8.0, freq=440.0, seed=1))
        b = _write_wav(tmp_path / "b.wav", _tone(8.0, freq=1930.0, seed=2))
        alignment = compute_audio_binding_alignment(
            str(a), str(b), analysis_seconds=8.0)
        assert alignment.rejection == "low_confidence"

    def test_cross_sample_rate_aligned_after_resample(self, tmp_path) -> None:
        """44.1k 机内 vs 48k 外录：显式归一后互相关仍可恢复 offset。"""
        from src.features.edit.core.multicam_sync import (
            compute_audio_binding_alignment,
        )
        tone = _broadband(20.0, seed=3)
        # 直接写 44.1k（构造上把 48k 信号按比值重采样到 44.1k）
        from scipy.signal import resample_poly
        tone_441 = resample_poly(
            tone[:, int(1.0 * SR):], 147, 160, axis=-1)
        a = _write_wav(tmp_path / "a441.wav", tone_441.astype(np.float32),
                       sample_rate=44100)  # 机内声缺前 1 秒（44.1k）
        b = _write_wav(tmp_path / "b48.wav", tone)  # 外录完整（48k）
        alignment = compute_audio_binding_alignment(
            str(a), str(b), analysis_seconds=8.0, max_shift_seconds=5.0)
        assert alignment.rejection == "" and not alignment.error
        assert alignment.lag_seconds == pytest.approx(1.0, abs=0.1)


class TestWavTimecode:
    def test_metadata_tc_wins(self, tmp_path) -> None:
        asset = MediaAsset(
            "w", "/tmp/x.wav", "audio", "w",
            metadata={"timecode": "01:00:10:00"})
        assert wav_timecode_seconds(asset, "") == pytest.approx(3610.0)

    def test_bext_fallback(self, tmp_path) -> None:
        import av

        path = tmp_path / "bext.wav"
        pcm = np.zeros((2, SR), dtype=np.float32)
        _write_wav(path, pcm)
        from src.features.edit.core.bwf import (
            BextMetadata,
            inject_bext_chunk,
        )
        inject_bext_chunk(str(path), BextMetadata(
            time_reference_samples=int(5.0 * SR)))
        asset = MediaAsset("w", str(path), "audio", "w", metadata={})
        assert wav_timecode_seconds(asset, str(path)) == pytest.approx(5.0)

    def test_no_tc_returns_none(self, tmp_path) -> None:
        path = _write_wav(tmp_path / "plain.wav", _tone(1.0))
        asset = MediaAsset("w", str(path), "audio", "w", metadata={})
        assert wav_timecode_seconds(asset, str(path)) is None


class TestExportConsumption:
    def test_export_uses_bound_wav_with_offset(self, tmp_path) -> None:
        """成片导出：绑定素材的音频从 WAV 取（偏移折算）。"""
        from src.features.edit.core.stem_export import (
            StemSpec,
            StemType,
            create_clip_decode_fn,
        )
        from src.features.edit.core.timeline_export import (
            TimelineExporter,
            TimelineExportConfig,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            Rational,
            TimelineSequence,
        )
        cam_tone = _tone(6.0, freq=300.0, seed=5)
        cam_path = _write_wav(tmp_path / "cam.wav", cam_tone)
        ext = _tone(9.0, freq=700.0, seed=6)
        ext_path = _write_wav(tmp_path / "ext.wav", ext)

        project = EditProject("p", "P", assets={
            "vid": MediaAsset(
                "vid", str(cam_path), "video", "cam",
                metadata={"fps": 24.0, "duration": 6.0,
                          "audio_info": {"channels": 2, "sample_rate": SR}}),
            "wav1": MediaAsset(
                "wav1", str(ext_path), "audio", "ext",
                metadata={"duration": 9.0,
                          "audio_info": {"channels": 2, "sample_rate": SR}}),
        })

        clip = ClipInstance("c1", "vid", "a0", 0, 48, 0, 48, speed=1.0)
        track = EditTrack("a0", "A1", "audio", 0, clips=(clip,), volume=1.0)
        seq = TimelineSequence(
            timeline_id="s", name="S", width=64, height=48,
            fps=Rational(24, 1), audio_tracks=(track,))

        # 未绑定：decode 来自机内声（300Hz）
        decode, close = create_clip_decode_fn(project, 24.0, SR)
        try:
            samples = decode(track, clip, 0, 24.0, 480)
            assert samples is not None
            freq = _dominant(samples)
            assert abs(freq - 300.0) < 30.0
        finally:
            close()

        # 绑定（提前 3s，700Hz 外录）：decode 切到 WAV
        binding = AudioSyncBinding(wav_asset_id="wav1", offset_seconds=3.0)
        project = UpdateAudioSyncBindingCommand.create(
            "vid", binding).apply(project)
        decode, close = create_clip_decode_fn(project, 24.0, SR)
        try:
            samples = decode(track, clip, 0, 24.0, 480)
            assert samples is not None
            freq = _dominant(samples)
            assert abs(freq - 700.0) < 40.0, (
                f"绑定后应听到外录 700Hz，got {freq}")
        finally:
            close()


def _dominant(pcm: np.ndarray) -> float:
    sig = pcm[0] if pcm.ndim == 2 else pcm
    spec = np.abs(np.fft.rfft(sig * np.hanning(sig.shape[0])))
    return float(np.fft.rfftfreq(sig.shape[0], 1.0 / SR)[np.argmax(spec)])
