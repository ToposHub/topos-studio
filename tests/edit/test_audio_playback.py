"""
Audio Playback Tests
音频播放控制器测试。

测试覆盖：
- AudioTrackState: 轨道状态验证
- AudioClock: 时钟计算和同步
- AudioMixer: 混合逻辑（mute/solo/volume）
- AudioPlaybackManager: 播放控制流程
- AudioVideoSynchronizer: 音视频同步

验收标准：
- 所有边界情况覆盖
- 线程安全验证
- 同步精度验证

注意：
- AudioDecoder 和 AudioOutput 需要 PyAV 和 QtMultimedia 环境
- 某些测试需要模拟音频设备
"""

import pytest
import sys
import time
import threading
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import numpy as np
from PyQt6.QtMultimedia import QAudioFormat

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core import audio_playback

from src.features.edit.core.audio_playback import (
    AudioTrackState,
    AudioClock,
    AudioClockState,
    AudioPlaybackState,
    AudioSyncMode,
    AudioMixer,
    AudioDecoder,
    AudioOutput,
    AudioPlaybackManager,
    AudioVideoSynchronizer,
    AudioPlaybackThread,
    AudioScrubPreviewWorker,
    DEFAULT_SAMPLE_RATE,
    SCRUB_AUDIO_OUTPUT_FRAMES,
    MIN_VOLUME,
    MAX_VOLUME,
    AV_SYNC_THRESHOLD_MS,
)


# ================================================================
# Test Fixtures
# ================================================================


@pytest.fixture
def sample_track_state() -> AudioTrackState:
    """创建示例轨道状态。"""
    return AudioTrackState(
        track_id="track_001",
        volume=1.0,
        muted=False,
        solo=False,
        pan=0.0,
        enabled=True,
    )


@pytest.fixture
def audio_clock() -> AudioClock:
    """创建音频时钟。"""
    return AudioClock(sample_rate=48000)


@pytest.fixture
def audio_mixer() -> AudioMixer:
    """创建音频混合器。"""
    return AudioMixer()


@pytest.fixture
def mock_video_controller() -> Mock:
    """创建模拟视频控制器。"""
    controller = Mock()
    controller.current_frame = 0
    controller.fps = 24.0
    controller.play = Mock()
    controller.pause = Mock()
    controller.stop = Mock()
    controller.seek = Mock()
    controller.frame_changed = Mock()
    controller.frame_changed.emit = Mock()
    return controller


# ================================================================
# AudioTrackState Tests
# ================================================================


class TestAudioTrackState:
    """AudioTrackState 测试。"""

    def test_create_default_state(self) -> None:
        """测试创建默认状态。"""
        state = AudioTrackState(track_id="test_track")

        assert state.track_id == "test_track"
        assert state.volume == 1.0
        assert state.muted == False
        assert state.solo == False
        assert state.pan == 0.0
        assert state.enabled == True

    def test_invalid_track_id(self) -> None:
        """测试无效轨道 ID。"""
        with pytest.raises(ValueError, match="track_id cannot be empty"):
            AudioTrackState(track_id="")

    def test_volume_out_of_range(self) -> None:
        """测试音量超出范围。"""
        # 音量过低
        with pytest.raises(ValueError, match="volume must be between"):
            AudioTrackState(track_id="test", volume=-0.1)

        # 音量过高
        with pytest.raises(ValueError, match="volume must be between"):
            AudioTrackState(track_id="test", volume=3.0)

    def test_pan_out_of_range(self) -> None:
        """测试声像超出范围。"""
        with pytest.raises(ValueError, match="pan must be between"):
            AudioTrackState(track_id="test", pan=-1.5)

        with pytest.raises(ValueError, match="pan must be between"):
            AudioTrackState(track_id="test", pan=1.5)

    def test_effective_volume_normal(self, sample_track_state: AudioTrackState) -> None:
        """测试正常状态的有效音量。"""
        assert sample_track_state.effective_volume() == 1.0

    def test_effective_volume_muted(self) -> None:
        """测试静音状态的有效音量。"""
        state = AudioTrackState(track_id="test", muted=True, volume=1.5)
        assert state.effective_volume() == 0.0

    def test_to_dict_and_from_dict(self, sample_track_state: AudioTrackState) -> None:
        """测试序列化和反序列化。"""
        data = sample_track_state.to_dict()

        assert data["track_id"] == "track_001"
        assert data["volume"] == 1.0
        assert data["muted"] == False

        restored = AudioTrackState.from_dict(data)
        assert restored.track_id == sample_track_state.track_id
        assert restored.volume == sample_track_state.volume
        assert restored.muted == sample_track_state.muted


# ================================================================
# AudioClock Tests
# ================================================================


class TestAudioClock:
    """AudioClock 测试。"""

    def test_create_clock(self, audio_clock: AudioClock) -> None:
        """测试创建时钟。"""
        assert audio_clock.sample_rate == 48000
        assert audio_clock.is_playing == False

    def test_set_sample_rate(self, audio_clock: AudioClock) -> None:
        """测试设置采样率。"""
        audio_clock.set_sample_rate(44100)
        assert audio_clock.sample_rate == 44100

    def test_invalid_sample_rate(self, audio_clock: AudioClock) -> None:
        """测试无效采样率。"""
        with pytest.raises(ValueError, match="Invalid sample_rate"):
            audio_clock.set_sample_rate(0)

    def test_start_and_get_time(self, audio_clock: AudioClock) -> None:
        """测试启动时钟和获取时间（基于 samples_played）。"""
        audio_clock.start(start_frame=0, fps=24.0)

        assert audio_clock.is_playing == True

        # 模拟音频线程写入采样：添加 2400 采样 = 50ms @ 48kHz
        audio_clock.add_samples(2400)

        # 时间应该增加
        audio_time = audio_clock.get_audio_time()
        assert audio_time >= 0.045  # 允许微小浮点误差
        assert audio_time <= 0.055

    def test_start_from_frame(self, audio_clock: AudioClock) -> None:
        """测试从指定帧启动。"""
        # 从第 24 帧启动（1 秒）
        audio_clock.start(start_frame=24, fps=24.0)

        audio_time = audio_clock.get_audio_time()
        # 应该约为 1 秒
        assert audio_time >= 0.9

    def test_pause_and_resume(self, audio_clock: AudioClock) -> None:
        """测试暂停和恢复（基于 samples_played）。"""
        audio_clock.start(start_frame=0, fps=24.0)

        # 模拟写入 4800 采样 (100ms @ 48kHz)
        audio_clock.add_samples(4800)

        # 暂停
        audio_clock.pause()
        assert audio_clock.is_playing == False

        paused_time = audio_clock.get_audio_time()

        # 等待一段时间，时间应该不变（暂停状态下 add_samples 不会被调用）
        time.sleep(0.05)
        assert audio_clock.get_audio_time() == paused_time

        # 恢复
        audio_clock.resume()
        assert audio_clock.is_playing == True

        # 模拟恢复后继续写入采样
        audio_clock.add_samples(2400)  # 再写入 50ms
        assert audio_clock.get_audio_time() > paused_time

    def test_stop(self, audio_clock: AudioClock) -> None:
        """测试停止时钟。"""
        audio_clock.start(start_frame=100, fps=24.0)
        time.sleep(0.05)

        audio_clock.stop()
        assert audio_clock.is_playing == False
        assert audio_clock.get_audio_time() == 0.0

    def test_seek(self, audio_clock: AudioClock) -> None:
        """测试定位时钟。"""
        audio_clock.start(start_frame=0, fps=24.0)

        # 定位到第 48 帧（2 秒）
        audio_clock.seek(frame=48, fps=24.0)

        audio_time = audio_clock.get_audio_time()
        assert audio_time >= 1.9

    def test_get_frame_for_fps(self, audio_clock: AudioClock) -> None:
        """测试计算视频帧（基于 samples_played）。"""
        audio_clock.start(start_frame=0, fps=24.0)

        # 模拟写入 1 秒的采样：48000 samples @ 48kHz
        audio_clock.add_samples(48000)

        # 应该约为 24 帧
        frame = audio_clock.get_frame_for_fps(24.0)
        assert frame >= 23  # 允许微小浮点误差
        assert frame <= 25

    def test_get_samples_for_frames(self, audio_clock: AudioClock) -> None:
        """测试计算采样数。"""
        # 24 帧 = 1 秒 = 48000 采样
        samples = audio_clock.get_samples_for_frames(24, 24.0)
        assert samples == 48000

    def test_add_samples(self, audio_clock: AudioClock) -> None:
        """测试添加采样数。"""
        audio_clock.start(start_frame=0, fps=24.0)

        # 添加 48000 采样（1 秒）
        audio_clock.add_samples(48000)

        # 时间应该增加
        # 注意：add_samples 是在时钟基础上累加
        # 所以实际时间 = elapsed_at_pause + 流逝时间 + added_samples

    def test_thread_safety(self, audio_clock: AudioClock) -> None:
        """测试线程安全。"""
        results: list = []
        errors: list = []

        def worker(action: str) -> None:
            try:
                if action == "start":
                    audio_clock.start(0, 24.0)
                elif action == "pause":
                    audio_clock.pause()
                elif action == "resume":
                    audio_clock.resume()
                elif action == "seek":
                    audio_clock.seek(100, 24.0)
                elif action == "get_time":
                    results.append(audio_clock.get_audio_time())
            except Exception as e:
                errors.append(e)

        # 创建多个线程同时操作
        threads = [
            threading.Thread(target=worker, args=("start",)),
            threading.Thread(target=worker, args=("get_time",)),
            threading.Thread(target=worker, args=("seek",)),
            threading.Thread(target=worker, args=("pause",)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 应该没有异常
        assert len(errors) == 0


# ================================================================
# AudioMixer Tests
# ================================================================


class TestAudioMixer:
    """AudioMixer 测试。"""

    def test_add_track(self, audio_mixer: AudioMixer) -> None:
        """测试添加轨道。"""
        # 创建模拟解码器
        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.is_open = True

        state = AudioTrackState(track_id="track_001")
        audio_mixer.add_track("track_001", mock_decoder, state)

        assert "track_001" in audio_mixer._tracks
        assert audio_mixer.get_track_state("track_001") == state

    def test_remove_track(self, audio_mixer: AudioMixer) -> None:
        """测试移除轨道。"""
        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.close = Mock()

        audio_mixer.add_track("track_001", mock_decoder)
        audio_mixer.remove_track("track_001")

        assert "track_001" not in audio_mixer._tracks
        mock_decoder.close.assert_called_once()

    def test_add_track_can_append_multiple_clip_decoders(self, audio_mixer: AudioMixer) -> None:
        """同一音轨可以挂多个 clip decoder，播放时都按各自时间段参与混音。"""
        decoder_a = Mock(spec=AudioDecoder)
        decoder_a.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 0.25
        )
        decoder_b = Mock(spec=AudioDecoder)
        decoder_b.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 0.5
        )

        audio_mixer.add_track("track_001", decoder_a)
        audio_mixer.add_track("track_001", decoder_b, replace_existing=False)

        result, _peaks = audio_mixer.mix_samples(current_frame=12, fps=24.0, num_samples=100)

        # 两 decoder 叠加 0.25+0.5=0.75，× PAN_CENTER（AM-08）
        from src.features.edit.core.audio_dsp import balance_gains
        pan_c = balance_gains(0.0)[0]
        assert np.allclose(result, 0.75 * pan_c, atol=0.01)
        decoder_a.decode_samples_for_frame.assert_called_once_with(12, 24.0, 100)
        decoder_b.decode_samples_for_frame.assert_called_once_with(12, 24.0, 100)

    def test_set_track_state(self, audio_mixer: AudioMixer) -> None:
        """测试设置轨道状态。"""
        mock_decoder = Mock(spec=AudioDecoder)
        audio_mixer.add_track("track_001", mock_decoder)

        new_state = AudioTrackState(track_id="track_001", volume=0.5, muted=True)
        audio_mixer.set_track_state("track_001", new_state)

        assert audio_mixer.get_track_state("track_001").volume == 0.5
        assert audio_mixer.get_track_state("track_001").muted == True

    def test_has_solo_tracks_false(self, audio_mixer: AudioMixer) -> None:
        """测试无 solo 轨道。"""
        mock_decoder = Mock(spec=AudioDecoder)
        audio_mixer.add_track("track_001", mock_decoder)

        assert audio_mixer.has_solo_tracks() == False

    def test_has_solo_tracks_true(self, audio_mixer: AudioMixer) -> None:
        """测试有 solo 轨道。"""
        mock_decoder = Mock(spec=AudioDecoder)
        solo_state = AudioTrackState(track_id="track_001", solo=True)
        audio_mixer.add_track("track_001", mock_decoder, solo_state)

        assert audio_mixer.has_solo_tracks() == True

    def test_mix_samples_single_track(self, audio_mixer: AudioMixer) -> None:
        """测试单轨混合。"""
        import numpy as np

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(return_value=np.zeros((2, 100), dtype=np.float32))

        audio_mixer.add_track("track_001", mock_decoder)

        result, _peaks = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        assert result.shape == (2, 100)
        assert result.dtype == np.float32

    def test_mix_samples_muted_track(self, audio_mixer: AudioMixer) -> None:
        """测试静音轨道不参与混合。"""
        import numpy as np

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(return_value=np.ones((2, 100), dtype=np.float32) * 0.5)

        muted_state = AudioTrackState(track_id="track_001", muted=True)
        audio_mixer.add_track("track_001", mock_decoder, muted_state)

        result, _peaks = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        # 静音轨道输出应该为 0
        assert np.all(result == 0.0)

    def test_mix_samples_solo_logic(self, audio_mixer: AudioMixer) -> None:
        """测试 solo 逻辑。"""
        import numpy as np

        # Track 1: 非 solo
        mock_decoder1 = Mock(spec=AudioDecoder)
        mock_decoder1.decode_samples_for_frame = Mock(return_value=np.ones((2, 100), dtype=np.float32) * 0.5)
        audio_mixer.add_track("track_001", mock_decoder1)

        # Track 2: solo
        mock_decoder2 = Mock(spec=AudioDecoder)
        mock_decoder2.decode_samples_for_frame = Mock(return_value=np.ones((2, 100), dtype=np.float32) * 0.8)
        solo_state = AudioTrackState(track_id="track_002", solo=True)
        audio_mixer.add_track("track_002", mock_decoder2, solo_state)

        result, _peaks = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        # 只有 solo 轨道的声音（× PAN_CENTER，AM-08）
        from src.features.edit.core.audio_dsp import balance_gains
        pan_c = balance_gains(0.0)[0]
        assert np.allclose(result, 0.8 * pan_c, atol=0.05)

    def test_mix_samples_volume_adjustment(self, audio_mixer: AudioMixer) -> None:
        """测试音量调整。"""
        import numpy as np
        from src.features.edit.core.audio_dsp import balance_gains

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(return_value=np.ones((2, 100), dtype=np.float32) * 1.0)

        volume_state = AudioTrackState(track_id="track_001", volume=0.5)
        audio_mixer.add_track("track_001", mock_decoder, volume_state)

        result, _peaks = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        # 音量应该被乘以 0.5（× PAN_CENTER，AM-08 pan=0 衰减）
        pan_c = balance_gains(0.0)[0]
        assert np.allclose(result, 0.5 * pan_c, atol=0.05)

    def test_mix_samples_pan_adjustment(self, audio_mixer: AudioMixer) -> None:
        """测试声像调整。"""
        import numpy as np

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(return_value=np.ones((2, 100), dtype=np.float32) * 1.0)

        pan_state = AudioTrackState(track_id="track_001", pan=0.5)  # 偏右
        audio_mixer.add_track("track_001", mock_decoder, pan_state)

        result, _peaks = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        # AM-08: equal-power pan（pan=0.5: left=cos(3π/8)≈0.383, right=sin(3π/8)≈0.924）
        from src.features.edit.core.audio_dsp import balance_gains
        lg, rg = balance_gains(0.5)
        assert np.allclose(result[0], lg, atol=0.1)  # 左声道
        assert np.allclose(result[1], rg, atol=0.1)  # 右声道

    def test_mix_samples_clip_to_range(self, audio_mixer: AudioMixer) -> None:
        """测试混合后防止溢出。"""
        import numpy as np

        # 添加多个轨道，音量都为 1.0
        for i in range(3):
            mock_decoder = Mock(spec=AudioDecoder)
            mock_decoder.decode_samples_for_frame = Mock(return_value=np.ones((2, 100), dtype=np.float32) * 0.9)
            audio_mixer.add_track(f"track_{i}", mock_decoder)

        result, _peaks = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        # 结果应该被 clip 到 [-1.0, 1.0]
        assert np.all(result >= -1.0)
        assert np.all(result <= 1.0)


# ================================================================
# AudioMixer Master Bus Tests (Master volume/mute at sample level)
# ================================================================


class TestAudioMixerMaster:
    """测试 Master 总线音量/静音（sample 级应用，对称于 track volume）。

    背景：原 AudioPlaybackManager.set_master_volume 是死 no-op（依赖恒为 None 的
    self._output），且 AudioMixer 没有 master 状态，mix_samples 也不应用 master 增益。
    修复后 master_volume/muted 在 mix_samples 末端（软限幅之前）应用。
    """

    def test_master_volume_default_is_unity(self, audio_mixer: AudioMixer) -> None:
        """默认 master_volume=1.0，不影响输出（× PAN_CENTER，AM-08）。"""
        import numpy as np
        from src.features.edit.core.audio_dsp import balance_gains

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 0.5)
        audio_mixer.add_track("track_001", mock_decoder)

        result, _ = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        # master_volume 默认 1.0 → 输出 = 输入 × PAN_CENTER
        pan_c = balance_gains(0.0)[0]
        assert np.allclose(result, 0.5 * pan_c, atol=1e-5)

    def test_master_volume_attenuation(self, audio_mixer: AudioMixer) -> None:
        """master_volume=0.5 → 输出为输入的 0.5（× PAN_CENTER，AM-08）。"""
        import numpy as np
        from src.features.edit.core.audio_dsp import balance_gains

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 0.5)
        audio_mixer.add_track("track_001", mock_decoder)

        audio_mixer.set_master_volume(0.5)
        result, _ = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        # 0.5 × PAN_CENTER × 0.5
        pan_c = balance_gains(0.0)[0]
        assert np.allclose(result, 0.5 * pan_c * 0.5, atol=1e-5)

    def test_master_volume_positive_gain(self, audio_mixer: AudioMixer) -> None:
        """master_volume=2.0（+6dB 正增益）→ 输出翻倍（× PAN_CENTER，AM-08）。"""
        import numpy as np
        from src.features.edit.core.audio_dsp import balance_gains

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 0.5)
        audio_mixer.add_track("track_001", mock_decoder)

        audio_mixer.set_master_volume(2.0)
        result, _ = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        # 0.5 × PAN_CENTER × 2.0（峰值未超 1.0，软限幅不触发）
        pan_c = balance_gains(0.0)[0]
        assert np.allclose(result, 0.5 * pan_c * 2.0, atol=1e-5)

    def test_master_volume_positive_gain_soft_limited(self, audio_mixer: AudioMixer) -> None:
        """master 正增益导致峰值 >1.0 时，软限幅器把输出钳制到 ≤1.0。"""
        import numpy as np

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 0.8)
        audio_mixer.add_track("track_001", mock_decoder)

        # 0.8 × 2.0 = 1.6 → 软限幅 → 1.0
        audio_mixer.set_master_volume(2.0)
        result, _ = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        peak = float(np.max(np.abs(result)))
        assert peak <= 1.0 + 1e-6, f"软限幅失效，peak={peak}"
        # 应该确实被放大了（而不是原样 0.8）
        assert peak > 0.9

    def test_master_muted_silences_output(self, audio_mixer: AudioMixer) -> None:
        """master_muted=True → 输出全 0（不论输入或 master_volume）。"""
        import numpy as np

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 0.5)
        audio_mixer.add_track("track_001", mock_decoder)

        # 设非平凡 master_volume，确保 muted 优先于 volume
        audio_mixer.set_master_volume(1.5)
        audio_mixer.set_master_muted(True)
        result, _ = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        assert np.all(result == 0.0)

    def test_master_muted_false_restores_audio(self, audio_mixer: AudioMixer) -> None:
        """master_muted 从 True 切回 False → 输出恢复（不被静音状态粘住）。"""
        import numpy as np
        from src.features.edit.core.audio_dsp import balance_gains

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 0.5)
        audio_mixer.add_track("track_001", mock_decoder)

        audio_mixer.set_master_muted(True)
        audio_mixer.set_master_muted(False)
        result, _ = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        pan_c = balance_gains(0.0)[0]
        assert np.allclose(result, 0.5 * pan_c, atol=1e-5)

    def test_master_volume_zero_silences_output(self, audio_mixer: AudioMixer) -> None:
        """master_volume=0.0 → 输出全 0（与 muted 等效但走不同路径）。"""
        import numpy as np

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 0.5)
        audio_mixer.add_track("track_001", mock_decoder)

        audio_mixer.set_master_volume(0.0)
        result, _ = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        assert np.all(result == 0.0)

    def test_master_volume_combines_with_track_volume(self, audio_mixer: AudioMixer) -> None:
        """master 增益 × track volume 联合作用（× PAN_CENTER，AM-08）。"""
        import numpy as np
        from src.features.edit.core.audio_dsp import balance_gains

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(
            return_value=np.ones((2, 100), dtype=np.float32) * 1.0)
        track_state = AudioTrackState(track_id="track_001", volume=0.6)
        audio_mixer.add_track("track_001", mock_decoder, track_state)

        audio_mixer.set_master_volume(0.5)
        result, _ = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)

        # 1.0 × 0.6 × 0.5 × PAN_CENTER
        pan_c = balance_gains(0.0)[0]
        assert np.allclose(result, 1.0 * 0.6 * 0.5 * pan_c, atol=1e-5)

    def test_master_volume_thread_safe_concurrent_set(self, audio_mixer: AudioMixer) -> None:
        """set_master_volume 在多线程并发调用下不崩、最终值生效。"""
        import numpy as np
        import threading

        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.decode_samples_for_frame = Mock(
            return_value=np.zeros((2, 100), dtype=np.float32))
        audio_mixer.add_track("track_001", mock_decoder)

        errors: list = []

        def setter():
            try:
                for v in (0.3, 1.5, 0.8, 2.0, 0.1):
                    audio_mixer.set_master_volume(v)
            except Exception as e:  # pragma: no cover - 抓并发异常
                errors.append(e)

        threads = [threading.Thread(target=setter) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # 最终能正常 mix（不崩、不死锁）
        result, _ = audio_mixer.mix_samples(current_frame=0, fps=24.0, num_samples=100)
        assert result.shape == (2, 100)


# ================================================================
# AudioDecoder Tests (Mocked)
# ================================================================


class TestAudioDecoder:
    """AudioDecoder 测试（需要 PyAV 环境）。"""

    @pytest.mark.skipif(
        not hasattr(audio_playback, 'PYAV_AVAILABLE') or not audio_playback.PYAV_AVAILABLE,
        reason="PyAV not available"
    )
    def test_open_file(self) -> None:
        """测试打开文件。"""
        decoder = AudioDecoder()

        # 需要实际音频文件
        # 这里跳过，因为测试环境可能没有音频文件
        pass

    def test_open_file_mocked(self) -> None:
        """测试打开文件（模拟）。"""
        decoder = AudioDecoder()

        # 模拟 PyAV 不可用
        with patch('src.features.edit.core.audio_playback.PYAV_AVAILABLE', False):
            result = decoder.open(Path("/fake/audio.mp3"))
            assert result == False

    def test_decode_first_clip_frame_seeks_to_source_start(self) -> None:
        """source_start 非 0 时，clip 第一帧也必须先 seek 到源素材偏移。

        R06（第七轮审计）：seek 目标时间按调用方 fps 换算——此处传
        timeline_fps（= 源 fps），100/25 + 0/25 = 4.0s。
        """
        decoder = AudioDecoder()
        decoder._timeline_start = 20
        decoder._timeline_duration = 10
        decoder._source_start = 100
        decoder._source_fps = 25.0
        decoder._sample_rate = 48000
        decoder._output_sample_rate = 48000
        decoder.seek_to_time = Mock(return_value=True)
        decoder.decode_next_samples = Mock(
            return_value=np.zeros((2, 1920), dtype=np.float32)
        )

        samples = decoder.decode_samples_for_frame(20, 25.0, 1920)

        assert samples is not None
        decoder.seek_to_time.assert_called_once_with(4.0)
        decoder.decode_next_samples.assert_called_once()

    def test_decode_uses_passed_fps_for_seek_clock(self) -> None:
        """R06：混帧率导出传入源 fps——源帧 24 在 50fps 源上是 0.48s，
        而不是按 decoder 内部 timeline_fps(25) 算出的 0.96s。"""
        decoder = AudioDecoder()
        decoder._timeline_start = 0
        decoder._timeline_duration = 1000
        decoder._source_start = 0
        decoder._source_fps = 50.0
        decoder._fps = 25.0
        decoder._sample_rate = 48000
        decoder._output_sample_rate = 48000
        decoder.seek_to_time = Mock(return_value=True)
        decoder.decode_next_samples = Mock(
            return_value=np.zeros((2, 1920), dtype=np.float32)
        )

        samples = decoder.decode_samples_for_frame(24, 50.0, 1920)

        assert samples is not None
        decoder.seek_to_time.assert_called_once_with(
            pytest.approx(24 / 50.0))

    def test_seek_to_frame_uses_timeline_time_for_mixed_fps(self) -> None:
        """24fps timeline frame 24 is one second into a 25fps source."""
        decoder = AudioDecoder()
        decoder._fps = 24.0
        decoder._source_start = 100
        decoder._source_fps = 25.0
        decoder.seek_to_time = Mock(return_value=True)

        decoder.seek_to_frame(24)

        decoder.seek_to_time.assert_called_once_with(5.0)

    def test_decode_after_clip_end_returns_none(self) -> None:
        """clip 结束后不继续读取源文件后续声音，避免串音。"""
        decoder = AudioDecoder()
        decoder._timeline_start = 20
        decoder._timeline_duration = 10
        decoder.seek_to_frame = Mock(return_value=True)
        decoder.decode_next_samples = Mock(
            return_value=np.zeros((2, 1920), dtype=np.float32)
        )

        assert decoder.decode_samples_for_frame(30, 25.0, 1920) is None
        decoder.seek_to_frame.assert_not_called()
        decoder.decode_next_samples.assert_not_called()


# ================================================================
# AudioOutput Tests (Mocked)
# ================================================================


class TestAudioOutput:
    """AudioOutput 测试。"""

    def test_create_output(self) -> None:
        """测试创建输出。"""
        output = AudioOutput(sample_rate=48000, channels=2)

        assert output._sample_rate == 48000
        assert output._channels == 2
        assert output.is_playing == False

    def test_get_set_volume_mocked(self) -> None:
        """测试音量控制（模拟）。"""
        output = AudioOutput(sample_rate=48000, channels=2)

        # 模拟 audio sink
        output._audio_sink = Mock()
        output._audio_sink.volume = Mock(return_value=0.5)
        output._audio_sink.setVolume = Mock()

        # 获取音量
        assert output.get_volume() == 0.5

        # 设置音量
        output.set_volume(0.8)
        output._audio_sink.setVolume.assert_called_with(0.8)

    def test_int32_output_conversion_uses_active_audio_format(self) -> None:
        """测试输出 bytes 跟随当前 QAudioFormat。"""
        output = AudioOutput(sample_rate=48000, channels=2)
        output._format.setSampleFormat(QAudioFormat.SampleFormat.Int32)

        samples = np.array(
            [
                [1.0, -1.0],
                [0.5, -0.5],
            ],
            dtype=np.float32,
        )

        data, bytes_per_sample, num_samples = output._samples_to_output_bytes(samples)
        converted = np.frombuffer(data, dtype=np.int32)

        assert bytes_per_sample == 4
        assert num_samples == 2
        assert converted.shape[0] == 4
        assert converted[0] == 2147483647
        assert converted[2] == -2147483647

    def test_reset_buffer_restarts_sink_and_keeps_suspended(self) -> None:
        """Seek 时应清空设备端旧样本，并保持暂停状态。"""
        output = AudioOutput(sample_rate=48000, channels=2)
        output._audio_sink = Mock()
        output._audio_sink.start.return_value = Mock()
        output._buffer.extend(b"old")
        output._samples_written = 128
        output._is_playing = True

        output.reset_buffer(keep_suspended=True)

        output._audio_sink.stop.assert_called_once()
        output._audio_sink.start.assert_called_once()
        output._audio_sink.suspend.assert_called_once()
        assert output._buffer == bytearray()
        assert output._samples_written == 0
        assert output.is_playing is False

    def test_write_samples_soft_drops_deleted_qiodevice(self) -> None:
        """Qt 已销毁 QIODevice 时，播放线程应静默放弃本次写入。"""
        output = AudioOutput(sample_rate=48000, channels=2)
        output._is_playing = True
        output._io_device = Mock()
        output._io_device.write.side_effect = RuntimeError(
            "wrapped C/C++ object of type QIODevice has been deleted"
        )

        written = output.write_samples(np.zeros((2, 16), dtype=np.float32))

        assert written == 0
        assert output.is_playing is False
        assert output._io_device is None

    def test_bytes_free_soft_drops_deleted_audio_sink(self) -> None:
        """查询剩余缓冲遇到已销毁 QAudioSink 时，清理状态即可。"""
        output = AudioOutput(sample_rate=48000, channels=2)
        output._is_playing = True
        output._audio_sink = Mock()
        output._io_device = Mock()
        output._audio_sink.bytesFree.side_effect = RuntimeError(
            "wrapped C/C++ object of type QAudioSink has been deleted"
        )

        assert output.bytes_free() == 0
        assert output.is_playing is False
        assert output._audio_sink is None
        assert output._io_device is None

    def test_output_buffer_is_low_latency(self) -> None:
        """QAudioSink 目标缓冲不应天然制造数百毫秒音画延迟。"""
        buffer_ms = audio_playback.AUDIO_OUTPUT_FRAMES * 1000 / DEFAULT_SAMPLE_RATE

        assert buffer_ms <= 100


class TestAudioPlaybackThreadCommands:
    """AudioPlaybackThread command queue behavior."""

    def test_seek_commands_are_coalesced(self) -> None:
        """连续 seek 只保留最新目标，避免播放线程排队重置旧位置。"""
        thread = AudioPlaybackThread()
        thread._command_queue.put(("seek", (10, 24.0)))
        thread._command_queue.put(("pause", None))

        thread.seek(25, 24.0)

        queued = []
        while not thread._command_queue.empty():
            queued.append(thread._command_queue.get_nowait())

        assert queued == [("pause", None), ("seek", (25, 24.0))]

    def test_preroll_commands_coalesce_seek_requests(self) -> None:
        """Preroll replaces stale positioning commands so only latest audio target warms."""
        thread = AudioPlaybackThread()
        thread._command_queue.put(("seek", (10, 24.0)))
        thread._command_queue.put(("pause", None))

        thread.preroll(42, 24.0, 4800)

        queued = []
        while not thread._command_queue.empty():
            queued.append(thread._command_queue.get_nowait())

        assert queued == [("pause", None), ("preroll", (42, 24.0, 4800))]

    def test_preroll_command_fills_ring_while_suspended(self) -> None:
        """Preroll should warm continuous ring audio without resuming output."""
        thread = AudioPlaybackThread()
        thread._clock = Mock()
        thread._clock.sample_rate = 48000
        thread._fps = 25.0
        thread._samples_per_frame_exact = 1920.0
        thread._samples_per_frame = 1920
        thread._sample_rate = 48000
        thread._channels = 2
        thread._output = Mock()
        thread._mixer = Mock()
        # mix_samples now returns (output, per_track_peaks) tuple
        thread._mixer.mix_samples.return_value = (
            np.zeros((2, 1920), dtype=np.float32), {}
        )

        thread._process_command(("preroll", (100, 25.0, 3840)))

        thread._output.reset_buffer.assert_called_once_with(keep_suspended=True)
        assert thread._paused is True
        assert thread._ring.filled >= 3840
        assert thread._pending_fade_in is True
        assert thread._ring_write_frame_float > 100.0
        assert thread._prerolled_frame == 100

    def test_prefill_ring_advances_write_cursor(self) -> None:
        """Ring prefill must not repeatedly mix the same timeline frame."""
        thread = AudioPlaybackThread()
        thread._clock = Mock()
        thread._clock.sample_rate = 48000
        thread._fps = 25.0
        thread._samples_per_frame_exact = 1920.0
        thread._samples_per_frame = 1920
        thread._channels = 2
        thread._next_audio_frame = 50
        thread._ring_write_frame_float = 50.0
        mixed_frames: list[int] = []

        def mix(frame: int, fps: float, samples: int, channels: int):
            mixed_frames.append(frame)
            # mix_samples now returns (output, per_track_peaks) tuple
            return np.zeros((channels, samples), dtype=np.float32), {}

        thread._mixer = Mock()
        thread._mixer.mix_samples.side_effect = mix

        filled = thread._prefill_ring_to(3840)

        assert filled >= 3840
        assert mixed_frames[:2] == [50, 51]


class TestAudioScrubPreviewWorker:
    """Dedicated scrub preview worker behavior."""

    def test_stop_requests_interruption(self) -> None:
        mixer = Mock(spec=AudioMixer)
        worker = AudioScrubPreviewWorker(mixer)

        worker.stop()

        assert worker._stop_flag is True
        assert worker._stop_event.is_set()

    def test_request_preview_replaces_stale_positioning_work(self) -> None:
        mixer = Mock(spec=AudioMixer)
        worker = AudioScrubPreviewWorker(mixer)

        worker.request_preview(10, 24.0, 512, 2, 1)
        worker.request_preview(25, 24.0, 768, 2, 2)

        queued = []
        while not worker._queue.empty():
            queued.append(worker._queue.get_nowait())

        assert queued == [(25, 24.0, 768, 2, 2)]
        assert worker._latest_generation == 2

    def test_preview_fade_shapes_edges_without_mutating_input(self) -> None:
        mixer = Mock(spec=AudioMixer)
        worker = AudioScrubPreviewWorker(mixer)
        samples = np.ones((2, 512), dtype=np.float32)

        faded = worker._apply_preview_fade(samples)

        assert samples[0, 0] == 1.0
        assert faded[0, 0] == 0.0
        assert faded[0, -1] == 0.0
        assert faded.shape == samples.shape


# ================================================================
# AudioPlaybackManager Tests
# ================================================================


class TestAudioPlaybackManager:
    """AudioPlaybackManager 测试。"""

    def test_cleanup_joins_scrub_worker_before_releasing_reference(self) -> None:
        manager = AudioPlaybackManager()

        class _StoppedWorker:
            def __init__(self) -> None:
                self.running = True

            def stop(self) -> None:
                pass

            def isRunning(self) -> bool:
                return self.running

            def wait(self, _timeout: int | None = None) -> bool:
                self.running = False
                return True

        worker = _StoppedWorker()
        manager._scrub_preview_worker = worker  # type: ignore[assignment]

        manager._stop_scrub_preview_worker()

        assert manager._scrub_preview_worker is None

    def test_create_manager(self) -> None:
        """测试创建管理器。"""
        manager = AudioPlaybackManager()

        assert manager._sample_rate == DEFAULT_SAMPLE_RATE
        assert manager._timeline_fps == 24.0
        assert manager._timeline_duration == 0

    def test_set_sample_rate(self) -> None:
        """测试设置采样率。"""
        manager = AudioPlaybackManager()
        manager.set_sample_rate(44100)

        assert manager._sample_rate == 44100
        assert manager._clock.sample_rate == 44100

    def test_set_timeline_fps(self) -> None:
        """测试设置帧率。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_fps(30.0)

        assert manager._timeline_fps == 30.0

    def test_set_timeline_duration(self) -> None:
        """测试设置时长。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)  # 100 秒 @ 24fps

        assert manager._timeline_duration == 2400

    def test_set_loop(self) -> None:
        """测试设置循环。"""
        manager = AudioPlaybackManager()
        manager.set_loop(True)

        assert manager._loop_enabled == True

    def test_set_sync_mode(self) -> None:
        """测试设置同步模式。"""
        manager = AudioPlaybackManager()
        manager.set_sync_mode(AudioSyncMode.VIDEO_MASTER)

        assert manager._sync_mode == AudioSyncMode.VIDEO_MASTER

    def test_set_video_controller(self, mock_video_controller: Mock) -> None:
        """测试设置视频控制器。"""
        manager = AudioPlaybackManager()
        manager.set_video_controller(mock_video_controller)

        assert manager._video_controller == mock_video_controller

    def test_add_track_mocked(self) -> None:
        """测试添加轨道（模拟）。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_fps(24.0)

        # 模拟解码器
        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.open = Mock(return_value=True)

        with patch.object(manager._mixer, 'add_track'):
            # 由于实际需要打开文件，这里跳过
            pass

    def test_set_track_volume(self) -> None:
        """测试设置轨道音量。"""
        manager = AudioPlaybackManager()

        # 先添加一个轨道状态
        state = AudioTrackState(track_id="track_001", volume=1.0)
        manager._track_states["track_001"] = state
        manager._mixer._tracks["track_001"] = state

        manager.set_track_volume("track_001", 0.5)

        new_state = manager.get_track_state("track_001")
        assert new_state is not None
        assert new_state.volume == 0.5

    def test_set_track_mute(self) -> None:
        """测试设置轨道静音。"""
        manager = AudioPlaybackManager()

        state = AudioTrackState(track_id="track_001")
        manager._track_states["track_001"] = state
        manager._mixer._tracks["track_001"] = state

        manager.set_track_mute("track_001", True)

        new_state = manager.get_track_state("track_001")
        assert new_state is not None
        assert new_state.muted == True

    def test_set_track_solo(self) -> None:
        """测试设置轨道独奏。"""
        manager = AudioPlaybackManager()

        state = AudioTrackState(track_id="track_001")
        manager._track_states["track_001"] = state
        manager._mixer._tracks["track_001"] = state

        manager.set_track_solo("track_001", True)

        new_state = manager.get_track_state("track_001")
        assert new_state is not None
        assert new_state.solo == True

    def test_set_track_pan(self) -> None:
        """测试设置轨道声像。"""
        manager = AudioPlaybackManager()

        state = AudioTrackState(track_id="track_001")
        manager._track_states["track_001"] = state
        manager._mixer._tracks["track_001"] = state

        manager.set_track_pan("track_001", 0.5)

        new_state = manager.get_track_state("track_001")
        assert new_state is not None
        assert new_state.pan == 0.5

    def test_get_all_track_ids(self) -> None:
        """测试获取所有轨道 ID。"""
        manager = AudioPlaybackManager()

        manager._track_states["track_001"] = AudioTrackState(track_id="track_001")
        manager._track_states["track_002"] = AudioTrackState(track_id="track_002")

        ids = manager.get_all_track_ids()
        assert "track_001" in ids
        assert "track_002" in ids

    def test_play_pause_stop_mocked(self) -> None:
        """测试播放控制流程（模拟）。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)

        # 模拟输出启动
        with patch.object(AudioOutput, 'start', return_value=True):
            with patch.object(AudioPlaybackThread, 'start'):
                manager.play(start_frame=0)

                # 状态应该是 PLAYING
                assert manager.get_state() == AudioPlaybackState.PLAYING

                manager.pause()
                assert manager.get_state() == AudioPlaybackState.PAUSED

                manager.stop()
                assert manager.get_state() == AudioPlaybackState.STOPPED

    def test_seek_mocked(self) -> None:
        """测试定位（模拟）。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)

        manager.seek(100)

        assert manager.get_current_frame() == 100

    def test_seek_does_not_touch_decoders_from_manager_thread(self) -> None:
        """Manager seek must not race PyAV decoder seek against the audio thread."""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)
        decoder = Mock(spec=AudioDecoder)
        decoder.seek_to_frame = Mock()
        manager._mixer._decoders["track_001"] = [decoder]
        manager._playback_thread = Mock()

        manager.seek(100)

        manager._playback_thread.seek.assert_called_once_with(100, 24.0)
        decoder.seek_to_frame.assert_not_called()

    def test_prepare_for_playback_starts_paused_preroll_thread(self) -> None:
        """Manual seek/scrub release should warm audio output without starting playback."""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)

        with patch.object(AudioPlaybackThread, "start"):
            prepared = manager.prepare_for_playback(100, preroll_ms=80)

        assert prepared is True
        assert manager._playback_thread is not None
        assert manager._playback_thread._paused is True
        assert manager._prepared_frame == 100
        queued = []
        while not manager._playback_thread._command_queue.empty():
            queued.append(manager._playback_thread._command_queue.get_nowait())
        assert queued == [("preroll", (100, 24.0, 3840))]

    def test_play_uses_prepared_preroll_without_seek_reset(self) -> None:
        """Prepared ring audio should survive the Play command."""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)

        with patch.object(AudioPlaybackThread, "start"):
            manager.prepare_for_playback(100, preroll_ms=80)

        assert manager._playback_thread is not None
        manager._playback_thread.seek = Mock()
        manager._playback_thread.resume = Mock()

        manager.play(100)

        manager._playback_thread.seek.assert_not_called()
        manager._playback_thread.resume.assert_called_once()
        assert manager._prepared_frame is None
        assert manager.get_state() == AudioPlaybackState.PLAYING

    def test_preview_scrub_audio_queues_worker_without_starting_clock(self) -> None:
        """Audible scrub preview should request async mix and leave transport stopped."""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)
        worker = Mock()
        manager._ensure_scrub_preview_worker = Mock(return_value=worker)

        requested = manager.preview_scrub_audio(100)

        assert requested is True
        worker.request_preview.assert_called_once_with(100, 24.0, 3456, 2, 1)
        assert manager.get_state() == AudioPlaybackState.STOPPED

    def test_scrub_preview_samples_ready_writes_when_output_has_room(self) -> None:
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)
        manager._scrub_preview_generation = 4
        output = Mock()
        output.bytes_per_frame.return_value = 8
        output.bytes_free.return_value = 20000
        output.write_samples.return_value = 512
        manager._ensure_scrub_preview_output = Mock(return_value=output)

        samples = np.zeros((2, 512), dtype=np.float32)
        manager._on_scrub_preview_samples_ready(100, 4, samples)

        output.write_samples.assert_called_once()
        np.testing.assert_array_equal(output.write_samples.call_args.args[0], samples)

    def test_scrub_preview_drops_when_output_buffer_is_full(self) -> None:
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)
        manager._scrub_preview_generation = 4
        output = Mock()
        output.bytes_per_frame.return_value = 8
        output.bytes_free.return_value = 64
        manager._ensure_scrub_preview_output = Mock(return_value=output)

        samples = np.zeros((2, 512), dtype=np.float32)
        manager._on_scrub_preview_samples_ready(100, 4, samples)

        output.write_samples.assert_not_called()

    def test_scrub_preview_output_uses_small_buffer(self) -> None:
        manager = AudioPlaybackManager()
        with patch.object(AudioOutput, "start", return_value=True):
            output = manager._ensure_scrub_preview_output()
        assert output is None or output._buffer_frames == SCRUB_AUDIO_OUTPUT_FRAMES

    def test_get_progress(self) -> None:
        """测试获取进度。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)
        manager.seek(1200)  # 一半

        progress = manager.get_progress()
        assert progress >= 0.45  # 允许误差

    def test_cleanup(self) -> None:
        """测试清理资源。"""
        manager = AudioPlaybackManager()

        # 添加一些轨道
        mock_decoder = Mock(spec=AudioDecoder)
        mock_decoder.close = Mock()
        manager._mixer._decoders["track_001"] = mock_decoder

        manager.cleanup()

        mock_decoder.close.assert_called()


# ================================================================
# AudioVideoSynchronizer Tests
# ================================================================


class TestAudioVideoSynchronizer:
    """AudioVideoSynchronizer 测试。"""

    def test_create_synchronizer(
        self,
        mock_video_controller: Mock,
    ) -> None:
        """测试创建同步器。"""
        audio_manager = AudioPlaybackManager()
        sync = AudioVideoSynchronizer(audio_manager, mock_video_controller)

        assert sync._audio == audio_manager
        assert sync._video == mock_video_controller

    def test_set_sync_mode(
        self,
        mock_video_controller: Mock,
    ) -> None:
        """测试设置同步模式。"""
        audio_manager = AudioPlaybackManager()
        sync = AudioVideoSynchronizer(audio_manager, mock_video_controller)

        sync.set_sync_mode(AudioSyncMode.AUDIO_MASTER)

        assert sync._sync_mode == AudioSyncMode.AUDIO_MASTER

    def test_play_calls(
        self,
        mock_video_controller: Mock,
    ) -> None:
        """测试播放调用。"""
        audio_manager = AudioPlaybackManager()
        audio_manager.set_timeline_duration(2400)
        sync = AudioVideoSynchronizer(audio_manager, mock_video_controller)

        # 模拟
        with patch.object(audio_manager, 'play'):
            sync.play(start_frame=0)
            audio_manager.play.assert_called_with(0)

    def test_pause_calls(
        self,
        mock_video_controller: Mock,
    ) -> None:
        """测试暂停调用。"""
        audio_manager = AudioPlaybackManager()
        sync = AudioVideoSynchronizer(audio_manager, mock_video_controller)

        with patch.object(audio_manager, 'pause'):
            sync.pause()
            audio_manager.pause.assert_called_once()
            mock_video_controller.pause.assert_called_once()

    def test_stop_calls(
        self,
        mock_video_controller: Mock,
    ) -> None:
        """测试停止调用。"""
        audio_manager = AudioPlaybackManager()
        sync = AudioVideoSynchronizer(audio_manager, mock_video_controller)

        with patch.object(audio_manager, 'stop'):
            sync.stop()
            audio_manager.stop.assert_called_once()
            mock_video_controller.stop.assert_called_once()

    def test_seek_calls(
        self,
        mock_video_controller: Mock,
    ) -> None:
        """测试定位调用。"""
        audio_manager = AudioPlaybackManager()
        sync = AudioVideoSynchronizer(audio_manager, mock_video_controller)

        with patch.object(audio_manager, 'seek'):
            sync.seek(100)
            audio_manager.seek.assert_called_with(100)
            mock_video_controller.seek.assert_called_with(100)

    def test_sync_video_to_audio(
        self,
        mock_video_controller: Mock,
    ) -> None:
        """测试视频追音频同步。"""
        audio_manager = AudioPlaybackManager()
        sync = AudioVideoSynchronizer(audio_manager, mock_video_controller)
        sync.set_sync_mode(AudioSyncMode.AUDIO_MASTER)

        # 模拟音频帧变化
        audio_manager.get_current_frame = Mock(return_value=50)

        sync._sync_video_to_audio(50)

        mock_video_controller.seek.assert_called_with(50)


# ================================================================
# Integration Tests
# ================================================================


class TestIntegration:
    """集成测试。"""

    def test_full_playback_flow_mocked(self, mock_video_controller: Mock) -> None:
        """测试完整播放流程（模拟）。"""
        # 创建组件
        audio_manager = AudioPlaybackManager()
        audio_manager.set_timeline_fps(24.0)
        audio_manager.set_timeline_duration(2400)
        audio_manager.set_video_controller(mock_video_controller)

        sync = AudioVideoSynchronizer(audio_manager, mock_video_controller)
        sync.set_sync_mode(AudioSyncMode.AUDIO_MASTER)

        # 模拟播放
        with patch.object(AudioOutput, 'start', return_value=True):
            with patch.object(AudioPlaybackThread, 'start'):
                # 开始播放
                sync.play(0)
                assert audio_manager.get_state() == AudioPlaybackState.PLAYING

                # 定位
                sync.seek(100)
                assert audio_manager.get_current_frame() == 100

                # 暂停
                sync.pause()
                assert audio_manager.get_state() == AudioPlaybackState.PAUSED

                # 停止
                sync.stop()
                assert audio_manager.get_state() == AudioPlaybackState.STOPPED

    def test_track_control_flow(self) -> None:
        """测试轨道控制流程。"""
        manager = AudioPlaybackManager()

        # 添加轨道状态
        state1 = AudioTrackState(track_id="track_001", volume=1.0)
        state2 = AudioTrackState(track_id="track_002", volume=1.0)
        manager._track_states["track_001"] = state1
        manager._track_states["track_002"] = state2
        manager._mixer._tracks["track_001"] = state1
        manager._mixer._tracks["track_002"] = state2

        # 调整轨道 1
        manager.set_track_volume("track_001", 0.5)
        manager.set_track_mute("track_001", True)

        # 设置轨道 2 solo
        manager.set_track_solo("track_002", True)

        # 检查 solo 逻辑
        assert manager._mixer.has_solo_tracks() == True


# ================================================================
# Edge Cases and Error Handling
# ================================================================


class TestEdgeCases:
    """边界情况和错误处理测试。"""

    def test_empty_timeline_play(self) -> None:
        """测试空时间线播放。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(0)

        # 应该不播放
        manager.play(0)
        assert manager.get_state() == AudioPlaybackState.STOPPED

    def test_invalid_volume_clamped(self) -> None:
        """测试无效音量被 clamp。"""
        manager = AudioPlaybackManager()

        state = AudioTrackState(track_id="track_001")
        manager._track_states["track_001"] = state
        manager._mixer._tracks["track_001"] = state

        # 设置超出范围的音量
        manager.set_track_volume("track_001", 3.0)

        new_state = manager.get_track_state("track_001")
        assert new_state is not None
        assert new_state.volume == MAX_VOLUME  # 应被 clamp 到最大值

    def test_seek_out_of_range_clamped(self) -> None:
        """测试 seek 超出范围被 clamp。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)

        # seek 到超出范围的帧
        manager.seek(3000)

        # 应被 clamp 到最大帧
        assert manager.get_current_frame() <= 2399

    def test_negative_seek_clamped(self) -> None:
        """测试负数 seek 被 clamp。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)

        manager.seek(-100)

        assert manager.get_current_frame() == 0

    def test_nonexistent_track_operations(self) -> None:
        """测试不存在轨道的操作。"""
        manager = AudioPlaybackManager()

        # 对不存在轨道设置音量（应该静默失败）
        manager.set_track_volume("nonexistent", 0.5)

        # 获取不存在轨道状态
        state = manager.get_track_state("nonexistent")
        assert state is None

    def test_double_pause(self) -> None:
        """测试重复暂停。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)

        with patch.object(AudioOutput, 'start', return_value=True):
            with patch.object(AudioPlaybackThread, 'start'):
                manager.play(0)
                manager.pause()
                manager.pause()  # 第二次暂停应该无效

                assert manager.get_state() == AudioPlaybackState.PAUSED

    def test_double_stop(self) -> None:
        """测试重复停止。"""
        manager = AudioPlaybackManager()
        manager.set_timeline_duration(2400)

        manager.stop()
        manager.stop()  # 第二次停止应该无效

        assert manager.get_state() == AudioPlaybackState.STOPPED


# Import audio_playback module for PYAV_AVAILABLE check
import src.features.edit.core.audio_playback as audio_playback

import pytest
pytestmark = pytest.mark.edit_core


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
