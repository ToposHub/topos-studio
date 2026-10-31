"""M03（S05/S06）· 外录音同步与所有音频消费者同源回归。

验收（升级计划 M §M03）：
- S05：绑定 WAV 在 retime/保调片段同样被成片与 Stem 消费（此前静默
  回退机内声，与预览音源不一致）——reader 的 source_offset_seconds
  源窗平移与实时预览 source_start+bound_audio_frame 同口径；
- S06：TC 帧率按素材声明折算（24fps "00:00:01:12" = 1.5s，非 1.48s）；
  TC 法 offset 方向 = video_tc - wav_tc（摄影机晚 10 秒 → 正 10s）。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.audio_sync import (
    bound_audio_frame,
    timecode_seconds_from_meta,
    wav_timecode_seconds,
)
from src.features.edit.core.audio_retime import (
    ClipRetimeReader,
    PreservedPitchReader,
)


class FakeDecoder:
    """seek_to_time + decode_samples_for_frame 假解码器。

    PCM 值 = 全局采样序号 mod 1000 / 1000（单声道）——输出的样本值
    直接反映解码位置，可断言源窗平移。
    """

    def __init__(self, total_seconds: float, sample_rate: int = 48000):
        self._sr = int(sample_rate)
        self._n = int(total_seconds * self._sr)
        self.cursor = 0
        self.last_seek = None

    def seek_to_time(self, t: float) -> bool:
        self.last_seek = float(t)
        self.cursor = int(round(float(t) * self._sr))
        return True

    def decode_samples_for_frame(self, frame, fps, need):
        need = int(need)
        if self.cursor >= self._n or need <= 0:
            return None
        n = min(need, self._n - self.cursor)
        out = (np.arange(self.cursor, self.cursor + n,
                         dtype=np.float64) % 1000.0) / 1000.0
        self.cursor += n
        return out.astype(np.float32)


def _speed(value, preserve=False):
    return NS(speed=value, preserve_pitch=preserve, reverse=False,
              speed_points=())


def _clip(speed_obj=None, source_start=100, source_duration=200,
          timeline_start=0, timeline_duration=50):
    def _pair(f):
        speed = float(getattr(speed_obj, "speed", 1.0) or 1.0)
        pos = (f - timeline_start) * speed
        lo = int(np.floor(pos))
        frac = pos - lo
        return (lo, lo + 1, frac)

    def _source_frames_per_timeline_frame():
        return float(getattr(speed_obj, "speed", 1.0) or 1.0)

    return NS(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=timeline_start, timeline_duration=timeline_duration,
        source_start=source_start, source_duration=source_duration,
        speed=speed_obj, effects=(),
        timeline_end=timeline_start + timeline_duration,
        timeline_to_source_frame_pair=_pair,
        source_frames_per_timeline_frame=_source_frames_per_timeline_frame,
    )


# ================================================================
# S06 · TC 帧率折算
# ================================================================

class TestTimecodeSeconds:
    def test_24fps_tc_frame_fraction(self):
        # 00:00:01:12 @24fps = 1 + 12/24 = 1.5s（此前硬编码 /25 得 1.48）
        assert timecode_seconds_from_meta(
            {"timecode": "00:00:01:12", "fps": 24}) == pytest.approx(1.5)

    def test_default_25_when_fps_missing(self):
        assert timecode_seconds_from_meta(
            {"timecode": "00:00:01:12"}) == pytest.approx(1.48)

    def test_25fps_explicit(self):
        assert timecode_seconds_from_meta(
            {"timecode": "00:00:01:12", "fps": 25}) == pytest.approx(1.48)

    def test_missing_or_invalid_timecode(self):
        assert timecode_seconds_from_meta({"fps": 24}) is None
        assert timecode_seconds_from_meta(
            {"timecode": "garbage", "fps": 24}) is None
        assert timecode_seconds_from_meta({}) is None

    def test_wav_timecode_uses_metadata_fps(self):
        wav = NS(metadata={"timecode": "00:00:01:12", "fps": 24})
        assert wav_timecode_seconds(wav, "/missing.wav") == pytest.approx(1.5)


# ================================================================
# S06 · TC 法 offset 方向契约
# ================================================================

class TestTcOffsetDirectionContract:
    def test_camera_late_10s_yields_positive_offset(self):
        """契约：offset = video_tc - wav_tc（WAV 提前开录为正）。

        摄影机 00:00:10:00 开录、WAV 00:00:00:00 开录 → WAV 早 10s →
        offset = +10s（此前控制器用 wav_tc - video_tc 得 -10，方向反）。
        """
        video_meta = {"timecode": "00:00:10:00", "fps": 25}
        wav = NS(metadata={"timecode": "00:00:00:00", "fps": 25})
        video_tc = timecode_seconds_from_meta(video_meta)
        wav_tc = wav_timecode_seconds(wav, "/w.wav")
        # edit_controller TC 分支的同款公式（M03 修复后）
        offset = video_tc - wav_tc
        assert offset == pytest.approx(10.0)


# ================================================================
# S05 · reader 的绑定源窗平移
# ================================================================

class TestReaderSourceOffset:
    def test_retime_reader_shifts_decode_window(self):
        # 2x 变速：offset=0.5s @25fps → 源窗平移 round(0.5*25)=12 帧
        clip = _clip(speed_obj=_speed(2.0))
        plain = ClipRetimeReader(
            FakeDecoder(60.0), clip, 25.0, 1920, sample_rate=48000.0)
        shifted = ClipRetimeReader(
            FakeDecoder(60.0), clip, 25.0, 1920, sample_rate=48000.0,
            source_offset_seconds=0.5)
        assert plain.available and shifted.available
        assert shifted._source_start == plain._source_start + 12
        # 解码起点（秒域）平移量 ≈ offset（帧取整误差 ≤ 1 帧）
        shifted.read_frame(0, 1920)
        plain.read_frame(0, 1920)
        assert shifted._decoder.last_seek - plain._decoder.last_seek == \
            pytest.approx(0.5, abs=1.0 / 25.0)

    def test_retime_reader_relative_window_unchanged(self):
        # window 是相对帧坐标：绑定平移只改绝对起点（_source_start），
        # 相对窗边界（clip 源窗宽）不受 offset 影响
        clip = _clip(speed_obj=_speed(1.0), source_duration=10,
                     timeline_duration=8)
        plain = ClipRetimeReader(
            FakeDecoder(60.0), clip, 25.0, 1920, sample_rate=48000.0)
        shifted = ClipRetimeReader(
            FakeDecoder(60.0), clip, 25.0, 1920, sample_rate=48000.0,
            source_offset_seconds=1.0)
        assert shifted._window == plain._window
        assert shifted._source_start == plain._source_start + 25  # 1s@25fps

    def test_preserved_pitch_reader_shifts_decode_window(self):
        clip = _clip(speed_obj=_speed(2.0, preserve=True))
        plain_decoder = FakeDecoder(60.0)
        shifted_decoder = FakeDecoder(60.0)
        plain = PreservedPitchReader(
            plain_decoder, clip, 25.0, 1920, 48000.0)
        shifted = PreservedPitchReader(
            shifted_decoder, clip, 25.0, 1920, 48000.0,
            source_offset_seconds=0.5)
        assert plain._audio is not None and shifted._audio is not None
        # 解码起点平移 0.5s（帧取整误差 ≤ 1 帧）
        assert shifted_decoder.last_seek - plain_decoder.last_seek == \
            pytest.approx(0.5, abs=1.0 / 25.0)

    def test_bound_audio_frame_constant(self):
        # 预览侧共用折算：秒 → 帧
        assert bound_audio_frame(100, 0.5, 25.0) == 100 + 12  # round(12.5)
        assert bound_audio_frame(100, 0.0, 25.0) == 100


# ================================================================
# M03.3 · 双源混音显式声明（mix_mode + 声明函数）
# ================================================================

class TestDualSourceMixDeclaration:
    def test_no_binding_single_camera(self):
        from src.features.edit.core.audio_sync import (
            dual_source_mix_declaration,
        )
        decl = dual_source_mix_declaration(None)
        assert decl == {"sources": ["camera"], "camera_audio": True,
                        "mix": "single"}

    def test_replace_and_camera_only_declared(self):
        from src.features.edit.core.audio_sync import (
            AudioSyncBinding,
            dual_source_mix_declaration,
        )
        rep = dual_source_mix_declaration(
            AudioSyncBinding(wav_asset_id="w1"))
        assert rep["mix"] == "replace" and rep["camera_audio"] is False
        assert rep["sources"] == ["external_wav"]
        cam = dual_source_mix_declaration(
            AudioSyncBinding(wav_asset_id="w1", keep_camera_audio=True))
        assert cam["mix"] == "camera_only" and cam["camera_audio"] is True
        assert cam["binding_retained"] is True
        # 显式 mix_mode 覆盖推导
        explicit = dual_source_mix_declaration(
            AudioSyncBinding(wav_asset_id="w1", keep_camera_audio=True,
                             mix_mode="replace"))
        assert explicit["mix"] == "replace"

    def test_dual_unsupported_explicit(self):
        """dual（叠加）未声明支持——声明返回 unsupported，validate 拒绝。"""
        from src.features.edit.core.audio_sync import (
            AudioSyncBinding,
            dual_source_mix_declaration,
        )
        decl = dual_source_mix_declaration(
            AudioSyncBinding(wav_asset_id="w1", mix_mode="dual"))
        assert decl.get("unsupported") == "dual"
        with pytest.raises(ValueError, match="dual"):
            AudioSyncBinding(wav_asset_id="w1",
                             mix_mode="dual").validate()

    def test_mix_mode_roundtrip(self):
        from src.features.edit.core.audio_sync import AudioSyncBinding
        b = AudioSyncBinding(wav_asset_id="w1", mix_mode="camera_only")
        assert AudioSyncBinding.from_dict(b.to_dict()).mix_mode \
            == "camera_only"
        # 未知载荷回落推导（不丢绑定）
        b2 = AudioSyncBinding.from_dict(
            {"wav_asset_id": "w1", "mix_mode": "weird"})
        assert b2.mix_mode == ""


# ================================================================
# M03.5 · 跨日 TC 偏移声明（午夜翻转显式假设）
# ================================================================

class TestTimecodeOffsetWithWrap:
    def test_normal_small_offset_unwrapped(self):
        from src.features.edit.core.audio_sync import (
            timecode_offset_with_wrap,
        )
        offset, decl = timecode_offset_with_wrap(
            "01:00:00:00", "01:00:10:00")
        assert offset == pytest.approx(10.0)
        assert decl["wrapped"] is False and decl["caveat"] == ""
        assert decl["raw_offset"] == pytest.approx(10.0)

    def test_midnight_wrap_normalized(self):
        """跨日录制：23:59 → 次日 00:01 = +120s 而非 −86280s。"""
        from src.features.edit.core.audio_sync import (
            timecode_offset_with_wrap,
        )
        offset, decl = timecode_offset_with_wrap(
            "23:59:00:00", "00:01:00:00")
        assert offset == pytest.approx(120.0)
        assert decl["wrapped"] is True
        assert decl["raw_offset"] == pytest.approx(-86280.0)
        assert "跨日" in decl["caveat"] and "人工" in decl["caveat"]

    def test_reverse_wrap_normalized(self):
        from src.features.edit.core.audio_sync import (
            timecode_offset_with_wrap,
        )
        offset, decl = timecode_offset_with_wrap(
            "00:01:00:00", "23:59:00:00")
        assert offset == pytest.approx(-120.0)
        assert decl["wrapped"] is True

    def test_half_day_boundary_not_wrapped(self):
        """恰在阈值上：朴素值保留（阈值是显式假设边界，不模糊）。"""
        from src.features.edit.core.audio_sync import (
            timecode_offset_with_wrap,
        )
        offset, decl = timecode_offset_with_wrap(
            "00:00:00:00", "12:00:00:00")
        assert offset == pytest.approx(12 * 3600.0)
        assert decl["wrapped"] is False

    def test_invalid_timecode_raises(self):
        from src.features.edit.core.audio_sync import (
            timecode_offset_with_wrap,
        )
        with pytest.raises(ValueError):
            timecode_offset_with_wrap("bad", "00:00:00:00")
