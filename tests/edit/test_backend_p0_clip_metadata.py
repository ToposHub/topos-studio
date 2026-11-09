"""P0-1 完整 Clip 元数据与来源时间码——后端验收测试。

对应《媒体池后端交接计划》P0 第 1 项：
- 帧率有理数（fps_num/fps_den）、起始时间码、drop-frame 标志、总帧数、
  时长、采样率/位深/通道布局、拍摄日期与来源；
- 非整数帧率、跨小时、无时间码、纯音频均得到正确值；
- 未知值保持明确缺失（None），不能用 0 代替。
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from src.features.edit.core.project import (
    EditProject,
    _derive_timecode_flags,
    probe_media_metadata,
)
from src.features.edit.core.media_replacement import merge_replacement_metadata
from src.shared.media.factory import MediaSourceFactory
from src.shared.media.metadata import Rational
from src.shared.media.timecode import Timecode, TimecodeType

TEST_VIDEOS = Path(__file__).resolve().parents[2] / "tests" / "test_videos"
H264_CLIP = TEST_VIDEOS / "2K_h264.mp4"
PRORES_CLIP = TEST_VIDEOS / "2K_prores422.mov"

av = pytest.importorskip("av", reason="PyAV 探测依赖")


def _skip_if_missing(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"测试素材缺失: {path}")


def test_probe_h264_technical_fields() -> None:
    """真实 H.264：帧率有理数、总帧数、时长、逐轨音频与拍摄日期。"""
    _skip_if_missing(H264_CLIP)
    meta = probe_media_metadata(str(H264_CLIP))
    assert meta["width"] == 1920 and meta["height"] == 1080
    assert meta["fps"] == pytest.approx(25.0)
    assert meta["fps_num"] == 25 and meta["fps_den"] == 1
    assert meta["total_frames"] == 1572
    assert meta["duration_seconds"] == pytest.approx(62.88, abs=1e-6)
    assert meta["codec"] == "h264"
    audio = meta["audio_info"]
    assert audio["channels"] == 2
    assert audio["sample_rate"] == 48000
    assert audio["channel_layout"] == "stereo"
    # AAC → fltp：浮点采样格式不代表整数位深，保持 None。
    assert audio["bits_per_sample"] is None
    assert audio["sample_format"] == "fltp"
    tracks = meta["audio_tracks"]
    assert len(tracks) == 1
    assert tracks[0]["channel_count"] == 2
    assert tracks[0]["sample_rate"] == 48000
    # 容器 creation_time → date（ISO 字符串）
    assert isinstance(meta["date"], str) and "T" in meta["date"]


def test_probe_timecode_unknown_stays_missing() -> None:
    """无时间码素材：三个时间码键保持 None，不伪造 00:00:00:00。"""
    _skip_if_missing(H264_CLIP)
    meta = probe_media_metadata(str(H264_CLIP))
    assert meta.get("timecode") is None
    assert meta.get("timecode_fps") is None
    assert meta.get("timecode_drop_frame") is None


def test_probe_prores_pcm_bit_depth() -> None:
    """pcm_s24le：编码名内嵌位深 24 优先于容器采样格式 s32 的 32。"""
    _skip_if_missing(PRORES_CLIP)
    meta = probe_media_metadata(str(PRORES_CLIP))
    audio = meta["audio_info"]
    assert audio["codec"] == "pcm_s24le"
    assert audio["bits_per_sample"] == 24
    assert audio["sample_format"] == "s32"


def test_fast_probe_total_frames_never_zero() -> None:
    """快速探测：总帧数未知必须为 None，不得以 0 冒充。"""
    _skip_if_missing(H264_CLIP)
    meta = MediaSourceFactory._probe_video_metadata_ffmpeg(str(H264_CLIP))
    assert meta is not None
    assert meta.total_frames is None or meta.total_frames > 0


def test_derive_timecode_flags() -> None:
    """时间码派生：';'=DF、':'=NDF、标称帧率 round(fps)、无 TC 全 None。"""
    ntsc = Rational(30000, 1001)
    assert _derive_timecode_flags("01:00:00;00", ntsc) == (30, True)
    assert _derive_timecode_flags("01:00:00:00", Rational(25, 1)) == (25, False)
    assert _derive_timecode_flags(None, ntsc) == (None, None)
    assert _derive_timecode_flags("", ntsc) == (None, None)


def test_timecode_cross_hour_arithmetic() -> None:
    """跨小时：59:59:24 + 2 帧 → 01:00:00:01。"""
    fps25 = Rational(25, 1)
    tc = Timecode.from_string("00:59:59:24", fps25)
    assert tc.add_frames(2).to_string() == "01:00:00:01"
    # DF 往返：01:00:00;00 @29.97 ↔ 帧号 107892
    ntsc = Rational(30000, 1001)
    df = Timecode.from_string("01:00:00;00", ntsc)
    assert df.tc_type == TimecodeType.DROP_FRAME
    assert df.to_frame_number() == 107892
    assert Timecode.from_frame_number(
        107892, ntsc, TimecodeType.DROP_FRAME).to_string() == "01:00:00;00"


def test_audio_only_probe(tmp_path: Path) -> None:
    """纯音频：PCM WAV 的声道/采样率/位深/时长正确，total_frames 缺失而非 0。"""
    wav_path = tmp_path / "tone.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x00\x00" * 48000)  # 1 秒静音
    meta = probe_media_metadata(str(wav_path))
    assert meta.get("audio") is True
    audio = meta["audio_info"]
    assert audio["channels"] == 1
    assert audio["sample_rate"] == 48000
    assert audio["bits_per_sample"] == 16
    assert audio["sample_format"] == "s16"
    assert meta["duration_seconds"] == pytest.approx(1.0, abs=1e-3)
    # 纯音频没有帧域概念：未知值保持缺失
    assert meta.get("total_frames") is None


def test_asset_properties_expose_timecode_and_audio_fields() -> None:
    """MediaAsset 类型化读取：timecode_*/audio_tracks/audio_bits_per_sample。"""
    project = EditProject.create("P0 metadata")
    path = "/tmp/p0-clip.mov"
    project = project.import_asset(path, "video", {
        "fps": 29.97, "fps_num": 30000, "fps_den": 1001,
        "total_frames": 100, "duration_seconds": 100 / (30000 / 1001),
        "timecode": "01:23:45;12", "timecode_fps": 30,
        "timecode_drop_frame": True,
        "audio_info": {"channels": 2, "sample_rate": 48000,
                       "bits_per_sample": 24, "codec": "pcm_s24le"},
        "audio_tracks": [{"codec": "pcm_s24le", "channel_count": 2,
                          "bits_per_sample": 24}],
    })
    asset = project.get_asset_by_path(path)
    assert asset.timecode_start == "01:23:45;12"
    assert asset.timecode_fps == 30
    assert asset.timecode_drop_frame is True
    assert asset.audio_bits_per_sample == 24
    assert len(asset.audio_tracks) == 1
    assert asset.audio_tracks[0]["bits_per_sample"] == 24


def test_replacement_replaces_timecode_domain() -> None:
    """换源时时间码派生字段与逐轨音频属于来源域，旧值不得存活。"""
    previous = {
        "fps": 25.0, "fps_num": 25, "fps_den": 1,
        "timecode": "01:00:00:00", "timecode_fps": 25,
        "timecode_drop_frame": False,
        "audio_tracks": [{"codec": "pcm_s24le"}],
        "keywords": ("dailies",),  # 日志字段必须保留
    }
    probed = {"fps": 25.0, "fps_num": 25, "fps_den": 1, "total_frames": 50}
    merged = merge_replacement_metadata(previous, probed)
    assert "timecode" not in merged
    assert "timecode_fps" not in merged
    assert "timecode_drop_frame" not in merged
    assert "audio_tracks" not in merged
    assert merged["keywords"] == ("dailies",)
    assert merged["total_frames"] == 50
