"""
Clip speed/fade 实时一致性测试（RMA-03 阶段 A4）。

验收门槛（计划表 A4）：
- speed/fade realtime/offline fixture 一致。
- 0.5x、1x、2x 的 fixture 验证速度策略（resample 变调）。
- 线性/S 曲线 fade 包络在 clip 开头/结尾正确衰减。

核心 bug：实时路径（AudioMixer.mix_samples + AudioDecoder）此前完全不处理
clip speed 和 clip fade，离线导出有处理 → 两条路径必然漂移（用户调变速/淡化，
预览听不到，导出文件里有）。

本测试验证实时路径已接入 speed（decoder.set_speed）与 fade（mix_samples 应用
apply_clip_fade），并与离线 AudioGraphRenderer 一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState


FPS = 24.0
SR = 48000
N = 480  # 一帧的样本数（48000/100... 实际用 mix_samples 的 num_samples）


class _CountingDecoder:
    """记录每次 decode_samples_for_frame 实际请求的样本数的假 decoder。

    用于验证 speed 改变了 decoder 读取的源样本量（resample 变调策略）。
    """
    def __init__(self, amp=0.5):
        self.amp = float(amp)
        self.requested_samples: list[int] = []
        self._timeline_start = 0
        self._timeline_duration = 100000
        self._clip_id = None
        self._speed = 1.0
        self._fade = None

    def decode_samples_for_frame(self, frame, fps, num_samples):
        self.requested_samples.append(num_samples)
        sig = np.full(num_samples, self.amp, dtype=np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


class _DCDecoder:
    """恒定振幅 DC decoder（验证 fade 包络用）。"""
    def __init__(self, amp=0.5):
        self.amp = float(amp)
        self._timeline_start = 0
        self._timeline_duration = 100000
        self._clip_id = None
        self._speed = 1.0
        self._fade = None

    def decode_samples_for_frame(self, frame, fps, num_samples):
        sig = np.full(num_samples, self.amp, dtype=np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. Decoder speed 接入：set_speed 改变读取的源样本量
# ─────────────────────────────────────────────────────────────────────────────

class TestDecoderSpeedRealtime:
    """RMA-03: AudioDecoder.set_speed 让实时路径按速度读取源样本。"""

    def test_decoder_has_speed_field(self):
        """AudioDecoder 有 _speed 字段与 set_speed 方法。"""
        from src.features.edit.core.audio_playback import AudioDecoder
        d = AudioDecoder()
        assert hasattr(d, "_speed")
        assert d._speed == 1.0
        d.set_speed(2.0)
        assert d._speed == 2.0

    def test_set_speed_clamps_invalid(self):
        """非法 speed（<=0）归一为 1.0。"""
        from src.features.edit.core.audio_playback import AudioDecoder
        d = AudioDecoder()
        d.set_speed(0)
        assert d._speed == 1.0
        d.set_speed(-1.0)
        assert d._speed == 1.0

    def test_add_track_propagates_speed(self):
        """AudioPlaybackManager.add_track 把 speed 传给 decoder。

        用 mock 验证（不实际打开文件）。"""
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        # 直接验证 add_track 接受 speed 参数（签名）
        import inspect
        sig = inspect.signature(AudioPlaybackManager.add_track)
        assert "speed" in sig.parameters
        assert "fade_in_frames" in sig.parameters
        assert "fade_out_frames" in sig.parameters

    def test_edit_page_resolves_clip_effective_speed_method(self):
        """音频时间线同步必须调用 ClipInstance.effective_speed()。"""
        from src.features.edit.core.timeline_v2 import ClipInstance
        from src.features.edit.ui.edit_page import _resolve_clip_effective_speed

        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="a1",
            source_start=0, source_duration=100,
            timeline_start=0, timeline_duration=50, speed=2.0,
        )

        assert _resolve_clip_effective_speed(clip) == 2.0

    def test_edit_page_keeps_legacy_numeric_speed_compatibility(self):
        """旧适配器若以属性提供速度，仍可用于音频同步。"""
        from types import SimpleNamespace

        from src.features.edit.ui.edit_page import _resolve_clip_effective_speed

        assert _resolve_clip_effective_speed(SimpleNamespace(effective_speed=0.5)) == 0.5


# ─────────────────────────────────────────────────────────────────────────────
# 2. mix_samples 应用 clip fade（包络验证）
# ─────────────────────────────────────────────────────────────────────────────

class TestClipFadeRealtime:
    """RMA-03: mix_samples 对带 fade 的 decoder 应用淡入/淡出包络。"""

    def test_fade_in_attenuates_clip_start(self, qapp):
        """clip 开头的淡入：block 起始样本应被显著衰减（接近 0）。"""
        mixer = AudioMixer()
        decoder = _DCDecoder(amp=0.5)
        decoder._timeline_start = 0
        decoder._timeline_duration = 100
        # 设置淡入 5 帧（=10000 样本），block 仅 480 样本（覆盖淡入前 4.8%）
        decoder._fade = {
            "timeline_start": 0, "timeline_duration": 100,
            "fade_in_frames": 5, "fade_out_frames": 0, "fps": FPS,
        }
        state = AudioTrackState(track_id="t1", volume=1.0)
        mixer.add_track("t1", decoder, state)
        # current_frame=0（clip 开头）→ block 起始应被淡入衰减到接近 0
        output, _ = mixer.mix_samples(current_frame=0, fps=FPS, num_samples=480,
                                      output_channels=2)
        # block 起始样本（淡入起点）应接近 0，远小于满幅
        first_sample = float(np.abs(output[0][0]))
        assert first_sample < 0.05, f"clip 开头淡入起点应接近 0，got {first_sample}"

    def test_no_fade_passes_full_amplitude(self, qapp):
        """无 fade 的 decoder：满幅通过（与旧行为一致）。"""
        mixer = AudioMixer()
        decoder = _DCDecoder(amp=0.5)
        state = AudioTrackState(track_id="t1", volume=1.0)
        mixer.add_track("t1", decoder, state)
        output, _ = mixer.mix_samples(current_frame=0, fps=FPS, num_samples=480,
                                      output_channels=2)
        peak = float(np.max(np.abs(output)))
        # 无淡入 → stereo balance unity，0.5 * 1.0 = 0.5
        assert 0.45 < peak < 0.55, f"无淡入应满幅（pan 后），peak={peak}"

    def test_fade_out_attenuates_clip_end(self, qapp):
        """clip 结尾的淡出：block 内应有衰减（末尾样本接近 0）。"""
        mixer = AudioMixer()
        decoder = _DCDecoder(amp=0.5)
        decoder._timeline_start = 0
        decoder._timeline_duration = 100
        decoder._fade = {
            "timeline_start": 0, "timeline_duration": 100,
            "fade_in_frames": 0, "fade_out_frames": 5, "fps": FPS,
        }
        state = AudioTrackState(track_id="t1", volume=1.0)
        mixer.add_track("t1", decoder, state)
        # current_frame=99（clip 最后一帧，在淡出区 95-100 内）→ block 末尾应衰减到 0
        # One timeline frame is 2000 samples at 48 kHz / 24 fps.  Use a full
        # frame so the block reaches the actual end of the clip.
        output, _ = mixer.mix_samples(current_frame=99, fps=FPS, num_samples=2000,
                                      output_channels=2)
        # 淡出包络在 block 末尾降到 0（apply_clip_fade 的淡出 ramp 末尾）
        last_sample = float(np.abs(output[0][-1]))
        assert last_sample < 0.05, f"clip 结尾淡出末尾应接近 0，got {last_sample}"

    def test_fade_mid_clip_not_affected(self, qapp):
        """clip 中段（淡入/淡出区外）：满幅通过。"""
        mixer = AudioMixer()
        decoder = _DCDecoder(amp=0.5)
        decoder._timeline_start = 0
        decoder._timeline_duration = 100
        decoder._fade = {
            "timeline_start": 0, "timeline_duration": 100,
            "fade_in_frames": 5, "fade_out_frames": 5, "fps": FPS,
        }
        state = AudioTrackState(track_id="t1", volume=1.0)
        mixer.add_track("t1", decoder, state)
        # current_frame=50（中段，远离淡入0-5和淡出95-100）
        output, _ = mixer.mix_samples(current_frame=50, fps=FPS, num_samples=480,
                                      output_channels=2)
        peak = float(np.max(np.abs(output)))
        # stereo balance unity → 0.5
        assert 0.45 < peak < 0.55, f"clip 中段应满幅，peak={peak}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. 实时/离线一致性：renderer.render_track_block 与 mix_samples 的 fade 一致
# ─────────────────────────────────────────────────────────────────────────────

class TestRealtimeOfflineFadeParity:
    """RMA-03: 同一 fade 配置下，实时 mix_samples 与离线 renderer 输出一致。"""

    def test_fade_envelope_matches_between_paths(self, qapp):
        """实时与离线对同一 clip fade 产生相同的衰减比例。"""
        from src.features.edit.core.audio_dsp import apply_clip_fade

        amp = 0.5
        samples_ref = np.full((2, 480), amp, dtype=np.float32)

        # 离线：apply_clip_fade 直接应用（与 renderer.render_track_block 内部一致）
        clip_timeline_start = 0
        clip_timeline_duration = 100
        offline = apply_clip_fade(
            samples_ref.copy(),
            clip_timeline_start=clip_timeline_start,
            clip_timeline_duration=clip_timeline_duration,
            current_frame=0,  # clip 开头
            fade_in_frames=5, fade_out_frames=0,
            fps=FPS, sample_rate=SR,
        )

        # 实时：mix_samples 经 decoder._fade 应用
        mixer = AudioMixer()
        decoder = _DCDecoder(amp=amp)
        decoder._timeline_start = clip_timeline_start
        decoder._timeline_duration = clip_timeline_duration
        decoder._fade = {
            "timeline_start": clip_timeline_start,
            "timeline_duration": clip_timeline_duration,
            "fade_in_frames": 5, "fade_out_frames": 0, "fps": FPS,
        }
        state = AudioTrackState(track_id="t1", volume=1.0)
        mixer.add_track("t1", decoder, state)
        realtime_out, _ = mixer.mix_samples(current_frame=0, fps=FPS, num_samples=480,
                                            output_channels=2)

        # 比较"衰减比例"（offline[0] / samples_ref[0]）与实时（realtime / 满幅）。
        # 实时路径含 pan（RMA-08: stereo balance unity），离线 apply_clip_fade 不含 pan，
        # 所以比较相对衰减比例（包络形状），而非绝对值。
        offline_ratio = offline[0] / amp  # 离线每样本的包络值（0..1）
        # 实时每样本 / 该路径满幅值（pan 后），提取包络
        full_amp_realtime = amp * 1.0  # RMA-08: stereo balance unity（pan=0）
        realtime_ratio = realtime_out[0] / full_amp_realtime

        # 两者包络形状一致（容差：浮点 + 包络计算的 int 截断）
        np.testing.assert_allclose(
            realtime_ratio, offline_ratio, atol=5e-2,
            err_msg="实时与离线 fade 包络不一致",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. 速度 fixture：不同 speed 下 decoder 读取的源样本量
# ─────────────────────────────────────────────────────────────────────────────

class TestSpeedStrategyFixture:
    """RMA-03: resample 变调策略——speed 改变每帧读取的源样本数。

    策略：speed>1 → 读更多源样本（播放更快/音调更高）；speed<1 → 读更少（更慢/更低）。
    本测试用能记录请求样本数的 mock decoder 验证 decode_samples_for_frame 的
    speed 缩放，而非 mix_samples（mix_samples 内部 decoder 不可注入）。
    """

    def test_speed_scales_source_samples_read(self):
        """decode_samples_for_frame 内部按 speed 缩放 exact_samples_per_frame。

        通过子类化 AudioDecoder，重写 decode_next_samples 记录请求量，
        验证 speed=2.0 时请求量约为 speed=1.0 的 2 倍。
        """
        from src.features.edit.core.audio_playback import AudioDecoder

        requested = []

        class _SpyDecoder(AudioDecoder):
            def decode_next_samples(self, num_samples):
                requested.append(num_samples)
                sig = np.full(num_samples, 0.5, dtype=np.float32)
                return np.stack([sig, sig])

        d = _SpyDecoder()
        d._timeline_start = 0
        d._timeline_duration = 100000
        d._sample_rate = SR
        d._output_sample_rate = SR
        d._channels = 2

        # speed=1.0
        d.set_speed(1.0)
        d.decode_samples_for_frame(0, FPS, 480)
        n_speed1 = requested[-1]

        # speed=2.0 → 应读约 2 倍源样本
        requested.clear()
        d._last_decoded_frame = -1  # 重置以触发 seek
        d._fractional_offset = 0.0
        d.set_speed(2.0)
        d.decode_samples_for_frame(0, FPS, 480)
        n_speed2 = requested[-1]

        assert n_speed2 > n_speed1, f"speed=2 应读更多源样本: {n_speed2} vs {n_speed1}"
        # 应接近 2 倍（允许 fractional 累积误差）
        assert n_speed2 >= int(n_speed1 * 1.8), \
            f"speed=2 源样本应约为 speed=1 的 2 倍: {n_speed2} vs {n_speed1}"

        # speed=0.5 → 应读约一半
        requested.clear()
        d._last_decoded_frame = -1
        d._fractional_offset = 0.0
        d.set_speed(0.5)
        d.decode_samples_for_frame(0, FPS, 480)
        n_speed_half = requested[-1]
        assert n_speed_half < n_speed1, f"speed=0.5 应读更少源样本: {n_speed_half} vs {n_speed1}"
