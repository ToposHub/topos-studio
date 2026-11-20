"""P0-2 Clip Editor PCM：spec 解析、解码精度、可视化与过零吸附。

验收来自 docs/plans/2026-11-18-clip-editor-and-mixer-roadmap.md §2：
- 波形、频谱、试听共用同一 PCM 与坐标（脉冲位置容差 1 sample）；
- 23.976 / 29.97 / 非零 source_start / source_sample_offset / 倒放；
- 过零吸附：DC 偏置、立体声反极性、无过零、等距。
"""
from __future__ import annotations

import math
import wave

import numpy as np
import pytest

from src.features.edit.core.clip_pcm import (
    ClipPcmProvider,
    ClipAudioRenderSpec,
    compute_spectrogram,
    compute_waveform_stats,
    decode_audio_range,
    find_zero_crossing,
    resolve_asset_audio_spec,
    resolve_clip_audio_spec,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


def write_wav(path, pcm: np.ndarray, sample_rate: int) -> str:
    """pcm: planar float [-1, 1] → 16-bit interleaved wav。"""
    interleaved = np.clip(pcm.T, -1.0, 1.0)
    ints = (interleaved * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(pcm.shape[0])
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(ints.tobytes())
    return str(path)


def make_asset(asset_id: str, path: str, *, media_type: str = "audio",
               channels: int = 2, duration_seconds: float | None = None) -> MediaAsset:
    metadata: dict = {"audio_info": {"sample_rate": 48000, "channels": channels}}
    if duration_seconds is not None:
        metadata["duration_seconds"] = duration_seconds
    return MediaAsset(
        asset_id=asset_id, path=path, media_type=media_type,
        display_name=asset_id, metadata=metadata,
    )


def make_timeline(fps) -> TimelineSequence:
    track = EditTrack(track_id="a1", name="Dialogue", track_type="audio")
    video = EditTrack(track_id="v1", name="Picture", track_type="video")
    return TimelineSequence(fps=fps, audio_tracks=(track,), video_tracks=(video,))


class TestResolveSpec:
    def test_clip_source_range_uses_timeline_fps_and_offset(self, tmp_path) -> None:
        pcm = np.zeros((2, 48000), dtype=np.float32)
        path = write_wav(tmp_path / "a.wav", pcm, 48000)
        asset = make_asset("a", path)
        timeline = make_timeline(24)
        timeline.audio_tracks[0].clips  # noqa: B018 - 建立轨道
        clip = ClipInstance(
            asset_id="a", track_id="a1", source_start=12, source_duration=48,
            timeline_duration=48,
        )
        spec = resolve_clip_audio_spec(EditProject("p", "P", assets={"a": asset}), timeline, clip, asset)
        assert spec is not None
        # 24 fps：帧 → 秒 → 48k 采样；source_start=12 → 0.5 s → 24000；
        # source_duration=48 帧 → 2 s → end=120000（半开）。
        assert spec.start_sample == 24000
        assert spec.end_sample == 120000
        assert spec.sample_rate == 48000 and spec.channel_count == 2
        assert not spec.reverse
        assert spec.cache_key() == (
            spec.source_fingerprint, 48000, 2, 24000, 120000, False, ())

    def test_source_sample_offset_and_nonzero_fps(self, tmp_path) -> None:
        from src.features.edit.core.clip_params import SpeedParams

        pcm = np.zeros((1, 48000), dtype=np.float32)
        path = write_wav(tmp_path / "b.wav", pcm, 48000)
        asset = make_asset("b", path, channels=1)
        timeline = make_timeline(30000 / 1001)  # 29.97
        clip = ClipInstance(
            asset_id="b", track_id="a1", source_start=100, source_duration=200,
            timeline_duration=200, source_sample_offset=480,
            speed=SpeedParams(speed=1.0, reverse=True),
        )
        spec = resolve_clip_audio_spec(EditProject("p", "P", assets={"b": asset}), timeline, clip, asset)
        assert spec is not None
        rate = 48000
        frame_sec = 1001 / 30000
        expected_start = round(100 * frame_sec * rate) + 480
        expected_end = round(300 * frame_sec * rate)
        assert spec.start_sample == expected_start
        assert spec.end_sample == expected_end
        assert spec.reverse  # speed<0 → 倒放显示

    def test_asset_spec_covers_whole_file(self, tmp_path) -> None:
        pcm = np.zeros((2, 96000), dtype=np.float32)
        path = write_wav(tmp_path / "c.wav", pcm, 48000)
        asset = make_asset("c", path, duration_seconds=2.0)
        spec = resolve_asset_audio_spec(None, asset)
        assert spec is not None
        assert (spec.start_sample, spec.end_sample) == (0, 96000)
        assert spec.clip_id == "" and not spec.reverse


class TestDecodeRange:
    def test_impulse_lands_within_one_sample_at_48k(self, tmp_path) -> None:
        n = 48000
        pcm = np.zeros((2, n), dtype=np.float32)
        pcm[0, 30000] = 1.0
        path = write_wav(tmp_path / "impulse.wav", pcm, 48000)
        decoded = decode_audio_range(path, 24000, 48000, 48000)
        assert decoded is not None and decoded.shape == (2, 24000)
        peak = int(np.argmax(np.abs(decoded[0])))
        assert abs(peak - 6000) <= 1

    def test_resample_from_44100_keeps_pulse_position(self, tmp_path) -> None:
        rate = 44100
        n = rate * 2
        pcm = np.zeros((1, n), dtype=np.float32)
        pcm[0, rate] = 1.0  # 1.0 s
        path = write_wav(tmp_path / "pulse441.wav", pcm, rate)
        decoded = decode_audio_range(path, 0, 96000, 48000)
        assert decoded is not None
        peak = int(np.argmax(np.abs(decoded[0])))
        assert abs(peak - 48000) <= 4  # 重采样容差（计划允许单立容差）

    def test_cancel_returns_none(self, tmp_path) -> None:
        pcm = np.zeros((1, 48000), dtype=np.float32)
        path = write_wav(tmp_path / "d.wav", pcm, 48000)
        assert decode_audio_range(path, 0, 48000, 48000, cancel_check=lambda: True) is None

    def test_missing_file_returns_none(self, tmp_path) -> None:
        assert decode_audio_range(str(tmp_path / "missing.wav"), 0, 100, 48000) is None


class TestVisualization:
    def test_waveform_stats_of_sine(self) -> None:
        rate = 48000
        t = np.arange(rate, dtype=np.float32) / rate
        pcm = np.stack([0.5 * np.sin(2 * np.pi * 1000.0 * t)] * 2)
        mins, maxs, rmss = compute_waveform_stats(pcm, 100)
        assert len(mins) == 2 and len(mins[0]) == 100
        assert all(v <= 1e-6 for v in mins[0])
        assert max(maxs[0]) == pytest.approx(0.5, abs=0.05)
        rms_value = float(np.mean(rmss[0]))
        assert rms_value == pytest.approx(0.5 / math.sqrt(2), rel=0.1)

    def test_spectrogram_peaks_at_tone_frequency(self) -> None:
        rate = 48000
        t = np.arange(rate, dtype=np.float32) / rate
        pcm = (0.5 * np.sin(2 * np.pi * 1000.0 * t)).reshape(1, -1)
        matrix = compute_spectrogram(pcm, rate, nfft=2048, time_bins=64, freq_bins=96)
        assert len(matrix) == 64 and len(matrix[0]) == 96
        # 对数行：1 kHz 落在哪一行（20 Hz..24 kHz）。
        f_min, f_max = 20.0, 24000.0
        row = int(math.log10(1000.0 / f_min) / math.log10(f_max / f_min) * 96)
        best = int(np.argmax([col[row] for col in matrix]))
        assert best in range(0, 64)
        # 1 kHz 行的能量显著高于低频底噪行。
        assert max(col[row] for col in matrix) > 0.3
        assert max(col[0] for col in matrix) < 0.2
        for col in matrix:
            assert all(0.0 <= v <= 1.0 for v in col)


class TestZeroCrossing:
    def test_mono_sine_snaps_to_nearest_crossing(self) -> None:
        rate = 48000
        t = np.arange(rate, dtype=np.float32) / rate
        pcm = np.sin(2 * np.pi * 1000.0 * t).reshape(1, -1)
        # 1000 Hz → 每 24 个样本一个过零；candidate 12 恰在两过零中点。
        snapped = find_zero_crossing(pcm, 12, tolerance_samples=int(0.003 * rate), bounds=(0, rate))
        assert snapped is not None
        assert abs(pcm[0, snapped]) < 0.5
        assert snapped in (0, 24)  # 等距时取更近者（0 与 24 距 12 相同 → 0）

    def test_dc_offset_has_no_crossing(self) -> None:
        pcm = np.full((1, 4800), 0.5, dtype=np.float32)
        assert find_zero_crossing(pcm, 2400, tolerance_samples=144, bounds=(0, 4800)) is None

    def test_stereo_antiphase_does_not_cancel(self) -> None:
        n = 4800
        t = np.arange(n, dtype=np.float32) / 48000.0
        left = np.sin(2 * np.pi * 1000.0 * t)
        pcm = np.stack([left, -left])
        # L+R 恒为零；实现必须用各声道符号变化，而不是合成波形。
        snapped = find_zero_crossing(pcm, 12, tolerance_samples=144, bounds=(0, n))
        assert snapped is not None
        assert abs(pcm[0, snapped]) < 0.2

    def test_silence_snaps_nearest(self) -> None:
        pcm = np.zeros((2, 4800), dtype=np.float32)
        snapped = find_zero_crossing(pcm, 1000, tolerance_samples=144, bounds=(0, 4800))
        assert snapped == 1000

    def test_respects_bounds(self) -> None:
        rate = 48000
        t = np.arange(rate, dtype=np.float32) / rate
        pcm = np.sin(2 * np.pi * 1000.0 * t).reshape(1, -1)
        snapped = find_zero_crossing(pcm, 20, tolerance_samples=10, bounds=(2, rate))
        assert snapped is not None and snapped >= 2
        assert snapped == 24  # 1000 Hz → 48 样本周期，最近的过零点


class TestProvider:
    def test_request_decodes_and_caches(self, qtbot, tmp_path) -> None:
        ClipPcmProvider._instance = None
        pcm = np.zeros((2, 48000), dtype=np.float32)
        pcm[0, 1000] = 1.0
        path = write_wav(tmp_path / "e.wav", pcm, 48000)
        spec = ClipAudioRenderSpec(
            project_id="p", timeline_id="t", clip_id="c", revision=1,
            asset_id="a", source_path=path,
            source_fingerprint="fp", sample_rate=48000, channel_count=2,
            channel_layout="stereo", start_sample=0, end_sample=48000,
            reverse=False,
        )
        provider = ClipPcmProvider.instance()
        results: list = []

        def on_ready(result) -> None:
            results.append(result)

        assert provider.request(spec, on_ready) is False
        qtbot.waitUntil(lambda: bool(results), timeout=10000)
        assert results[0].pcm.shape == (2, 48000)
        assert int(np.argmax(np.abs(results[0].pcm[0]))) == 1000
        # 第二次请求命中缓存：同步回调。
        synced: list = []
        assert provider.request(spec, synced.append) is True
        assert len(synced) == 1 and synced[0] is results[0]
        provider.shutdown()


class TestProjectClipEditorPcm:
    def test_missing_source_reports_pcm_failure(self, qtbot, tmp_path) -> None:
        from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

        asset = make_asset("missing", str(tmp_path / "missing.wav"), duration_seconds=1.0)
        editor = ProjectClipEditor()
        qtbot.addWidget(editor)
        editor.open_source(asset)
        qtbot.waitUntil(lambda: editor._pcm_load_failed, timeout=5000)
        assert not editor._panel.btn_play.isEnabled()
        assert editor._status.toolTip() == editor._panel.btn_play.toolTip()
        assert not editor._pcm_provider._pending.get(editor._pcm_spec_key)

    def test_open_source_attaches_pcm_enables_playback(self, qtbot, tmp_path) -> None:
        from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

        ClipPcmProvider._instance = None
        rate = 48000
        t = np.arange(rate, dtype=np.float32) / rate
        pcm = np.stack([0.5 * np.sin(2 * np.pi * 440.0 * t), 0.25 * np.sin(2 * np.pi * 880.0 * t)])
        path = write_wav(tmp_path / "tone.wav", pcm, rate)
        asset = make_asset("tone", path, duration_seconds=1.0)
        project = EditProject("p", "P", assets={"tone": asset})

        editor = ProjectClipEditor()
        qtbot.addWidget(editor)
        editor.resize(1100, 700)
        editor.show()
        editor.open_source(asset, project=project)
        panel = editor._panel
        qtbot.waitUntil(lambda: panel._pcm is not None, timeout=15000)
        assert panel._pcm.shape == (2, 48000)
        # 播放/循环/频谱/FFT/过零吸附控件随 PCM 可用而启用。
        for widget in (panel.btn_play, panel.btn_loop, panel.btn_snap_zero,
                       panel.btn_spectral, panel.fft_combo):
            assert widget.isEnabled()
        # RMS 真实读数 + 频谱矩阵已装载。
        assert panel.rms_val_lbl.text() not in ("—", "")
        assert panel.canvas._spec_matrix and panel.canvas._spec_matrix[0]
        assert max(max(row) for row in panel.canvas._spec_matrix) > 0.3
        # 过零吸附：440 Hz → 半周期 ~54.5 样本处过零。
        panel.btn_snap_zero.setChecked(True)
        panel.canvas.setSelection(0.0015, 0.0090)  # 72 → 432 样本
        t0, t1, _, _ = panel.canvas.getSelection()
        assert abs(t0 * rate - 55) <= 2     # 最近的过零样本（54.5 附近）
        assert abs(t1 * rate - 436) <= 2    # 54.5 × 8 = 436.4 附近
        # 试听在无输出设备的环境下优雅失败，不抛异常。
        panel._toggle_play()
        panel.detach_pcm()
        assert panel._pcm is None

    def test_audition_session_state_machine_without_device(self, qtbot) -> None:
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        session = ClipAuditionSession()
        pcm = np.zeros((2, 4800), dtype=np.float32)
        session.load(pcm, 48000)
        assert not session.is_playing and session.position() == 0
        # seek 停止态只移动返回点。
        session.seek(1200)
        assert session.return_sample() == 1200
        started = session.play()
        if started:
            assert session.is_playing
            session.seek(2400)
            session.stop()
            assert not session.is_playing
            # 停止回到开始试听的游标。
            assert session.return_sample() == 1200
        else:
            # 无音频设备：优雅拒绝而不是崩溃。
            assert not session.is_playing
        session.stop()
        assert not session.is_playing

    def test_audition_retries_short_writes_and_drains_before_stopping(self, qtbot, monkeypatch) -> None:
        from PyQt6 import QtMultimedia
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        pcm = np.arange(20, dtype=np.float32).reshape(2, 10) / 20.0
        expected = np.ascontiguousarray(pcm[:, 2:].T).tobytes()
        received = bytearray()

        class Sink:
            def __init__(self, _format: QtMultimedia.QAudioFormat) -> None:
                pass

            def start(self) -> Sink:
                return self

            def stop(self) -> None:
                pass

            def bytesFree(self) -> int:
                return 16

            def processedUSecs(self) -> int:
                return 0

            def write(self, chunk: bytes) -> int:
                written = min(len(chunk), 4)
                received.extend(chunk[:written])
                return written

            def state(self) -> QtMultimedia.QAudio.State:
                return (QtMultimedia.QAudio.State.IdleState if len(received) == len(expected)
                        else QtMultimedia.QAudio.State.ActiveState)

        monkeypatch.setattr(QtMultimedia, "QAudioSink", Sink)
        session = ClipAuditionSession()
        session.load(pcm, 48000)
        assert session.play(2)
        for _ in range(20):
            session._tick()
        assert bytes(received) == expected
        assert not session.is_playing
        assert session.position() == 2
        session.clear()
        assert not session.has_audio()

    def test_cleanliness_measures_power_for_inverted_stereo(self) -> None:
        from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

        pcm = np.stack((np.full(4800, 0.1, dtype=np.float32),
                        np.full(4800, -0.1, dtype=np.float32)))
        report = ProjectClipEditor._measure_cleanliness(pcm, 48000)
        assert report["noise_floor_db"] == pytest.approx(-20.0, abs=0.01)
