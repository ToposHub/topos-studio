"""M-B8：v1.6 多音轨 / stems 绑定层 + 互操作（container_spec v1.6）。

验收（plan §3 M-B8）：
- N 轨混合格式（f32 立体声 + s16 mono + mp4a 5.1）mux→demux 逐字节
  roundtrip、轨名读写一致；
- v1.1 单轨 API = 轨 0 视图（旧文件旧 API 行为不变）；
- fail-fast：轨数超限 / 半声明 / 非法格式 / 越界轨索引；
- ffprobe 多音轨识别（轨数/各自格式/声道/轨名）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import (
    TC_AUDIO_CODEC_LPCM,
    TC_AUDIO_CODEC_MP4A,
    TC_AUDIO_FMT_FLOAT32,
    TC_AUDIO_FMT_INT,
    TC_AUDIO_LAYOUT_5_1,
    TC_AUDIO_LAYOUT_MONO,
    TC_AUDIO_LAYOUT_STEREO,
    TOPOS_CODEC_ABI_VERSION,
    ToposAudioTrackConfig,
    ToposCodec,
    ToposCodecError,
)

_W, _H = 64, 48
_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

_ASC_51 = b"\x11\xb0"  # AAC-LC 48kHz 6ch（AOT=2 freqIdx=3 chanCfg=6）


@pytest.fixture(scope="module")
def codec() -> ToposCodec:
    import os

    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is None and not any(
        p.is_file() for p in topos_binding.candidate_library_paths()
    ):
        pytest.skip("libtopos_codec 未构建；先运行 "
                    "bash native/topos_codec/scripts/build_release_repo.sh")
    status, lib_path, lib = topos_binding.first_resolved_library(explicit)
    if status == topos_binding.LIB_RESOLUTION_NOT_FOUND:
        pytest.skip("libtopos_codec 未构建；先运行 "
                    "bash native/topos_codec/scripts/build_release_repo.sh")
    if lib is None:
        pytest.fail(f"libtopos_codec 不可加载（{status}）：{lib_path}")
    for sym in ("tc_mux_add_audio_track", "tc_movie_audio_track_count",
                "tc_movie_read_audio_at"):
        if not hasattr(lib, sym):
            pytest.fail(
                f"libtopos_codec 缺少多音轨符号 {sym}——请重建：bash "
                f"native/topos_codec/scripts/build_release_repo.sh")
    return ToposCodec(lib)


def _frame_config() -> topos_binding._CFrameConfig:
    import ctypes

    fc = topos_binding._CFrameConfig()
    fc.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = _W
    fc.visible_height = _H
    fc.profile = 3
    fc.pixel_format = 0
    fc.bit_depth = 10
    fc.qmatrix_id = 1
    fc.qp_base = 24
    fc.color_range = 1
    fc.color_primaries = 1
    fc.color_transfer = 1
    fc.color_matrix = 1
    fc.sar_num = 1
    fc.sar_den = 1
    return fc


def _encode_packets(codec: ToposCodec, count: int) -> list[bytes]:
    import struct

    cw = (_W + 1) // 2
    y = struct.pack(f"<{_W * _H}H",
                    *[((x * 7 + r * 13) % 400 + 300)
                      for r in range(_H) for x in range(_W)])
    u = struct.pack(f"<{cw * _H}H",
                    *[(480 + (x % 5) * 8) for r in range(_H) for x in range(cw)])
    v = struct.pack(f"<{cw * _H}H",
                    *[(520 + (r % 4) * 6) for r in range(_H) for x in range(cw)])
    fc = _frame_config()
    return [codec.encode_frame(fc, [y, u, v])[0] for _ in range(count)]


def _write_stems(codec: ToposCodec, path: Path, frames: int = 8,
                 with_aac: bool = False) -> dict[int, list[bytes]]:
    """视频 + 三轨 stems：f32 立体声（Music）+ s16 mono（Dialogue）
    [+ mp4a 5.1（Effects）]。返回 {轨索引: chunks}。"""
    pkts = _encode_packets(codec, 4)
    rng_data: dict[int, list[bytes]] = {
        0: [bytes(((i * 7 + k * 13 + 1) & 0xFF) for k in range(2 * 4))
            for i in range(frames)],
        1: [bytes(((i * 11 + k * 5 + 2) & 0xFF) for k in range(1 * 2))
            for i in range(frames)],
    }
    if with_aac:
        rng_data[2] = [bytes(((i * 3 + k) & 0xFF) for k in range(16))
                       for i in range(frames)]
    cfg = codec.movie_config(_W, _H)  # reserved 全 0：音频全走逐轨声明
    with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
        for i, p in enumerate(pkts):
            mux.add_packet(p, i, 1)
        idx0 = mux.add_audio_track(ToposAudioTrackConfig(
            codec=TC_AUDIO_CODEC_LPCM, sample_rate=48000, channel_count=2,
            channel_layout=TC_AUDIO_LAYOUT_STEREO, bits_per_sample=32,
            sample_format=TC_AUDIO_FMT_FLOAT32, name="Music"))
        idx1 = mux.add_audio_track(ToposAudioTrackConfig(
            codec=TC_AUDIO_CODEC_LPCM, sample_rate=48000, channel_count=1,
            channel_layout=TC_AUDIO_LAYOUT_MONO, bits_per_sample=16,
            sample_format=TC_AUDIO_FMT_INT, name="Dialogue"))
        if with_aac:
            idx2 = mux.add_audio_track(ToposAudioTrackConfig(
                codec=TC_AUDIO_CODEC_MP4A, sample_rate=48000, channel_count=6,
                channel_layout=TC_AUDIO_LAYOUT_5_1, name="Effects"))
            mux.set_audio_track_asc(idx2, _ASC_51)
            assert idx2 == 2
        assert (idx0, idx1) == (0, 1)
        for i in range(frames):
            mux.add_audio_to(0, rng_data[0][i], 1)
            mux.add_audio_to(1, rng_data[1][i], 1)
            if with_aac:
                mux.add_audio_to(2, rng_data[2][i], 1024)
        if with_aac:
            # priming < 该轨已写采样数 → 数据先行（v1.4 语义）
            mux.set_audio_track_priming(2, 1024)
        mux.finish()
    return rng_data


def test_multitrack_roundtrip(codec, tmp_path) -> None:
    """三轨混合格式逐字节 roundtrip + 轨名 + 旧 API 轨 0 视图。"""
    path = tmp_path / "stems.mov"
    expect = _write_stems(codec, path, with_aac=True)
    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        assert mv.audio_track_count() == 3
        t0 = mv.audio_info_at(0)
        assert (t0.name, t0.channel_count, t0.channel_layout,
                t0.bits_per_sample, t0.sample_format) == (
            "Music", 2, TC_AUDIO_LAYOUT_STEREO, 32, TC_AUDIO_FMT_FLOAT32)
        t1 = mv.audio_info_at(1)
        assert (t1.name, t1.channel_count,
                t1.channel_layout) == ("Dialogue", 1, TC_AUDIO_LAYOUT_MONO)
        t2 = mv.audio_info_at(2)
        assert (t2.name, t2.codec, t2.channel_count) == (
            "Effects", TC_AUDIO_CODEC_MP4A, 6)
        assert t2.priming_samples == 1024
        for track in range(3):
            chunks = expect[track]
            for i, chunk in enumerate(chunks):
                assert mv.read_audio_samples_at(track, i, 1) == chunk
        # 旧 API = 轨 0 视图
        legacy = mv.audio_info()
        assert legacy.name == "Music"
        assert mv.read_audio_samples(0, 1) == expect[0][0]
        # 越界轨索引
        with pytest.raises(ToposCodecError, match="越界"):
            mv.audio_info_at(3)
        with pytest.raises(ToposCodecError, match="越界"):
            mv.read_audio_samples_at(3, 0, 1)


def test_multitrack_limit_and_validation(codec, tmp_path) -> None:
    """fail-fast：17 轨 LIMIT；非法格式/半声明拒绝。"""
    path = tmp_path / "limit.mov"
    pkts = _encode_packets(codec, 1)
    cfg = codec.movie_config(_W, _H)
    with pytest.raises(ToposCodecError, match="上限"):
        with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
            mux.add_packet(pkts[0], 0, 1)
            mono = ToposAudioTrackConfig(
                codec=TC_AUDIO_CODEC_LPCM, sample_rate=48000, channel_count=1,
                channel_layout=TC_AUDIO_LAYOUT_MONO, bits_per_sample=16)
            for _ in range(17):
                mux.add_audio_track(mono)
            mux.finish()

    path2 = tmp_path / "badfmt.mov"
    with pytest.raises(ToposCodecError, match="float32"):
        with topos_binding.ToposMuxFile(codec, str(path2), cfg) as mux:
            mux.add_packet(pkts[0], 0, 1)
            mux.add_audio_track(ToposAudioTrackConfig(
                codec=TC_AUDIO_CODEC_MP4A, sample_rate=48000, channel_count=2,
                channel_layout=TC_AUDIO_LAYOUT_STEREO,
                sample_format=TC_AUDIO_FMT_FLOAT32))
            mux.finish()

    # 半声明（声明轨 0 无样本）→ finish 拒绝
    path3 = tmp_path / "half.mov"
    with pytest.raises(ToposCodecError, match="未注入"):
        with topos_binding.ToposMuxFile(codec, str(path3), cfg) as mux:
            mux.add_packet(pkts[0], 0, 1)
            mux.add_audio_track(ToposAudioTrackConfig(
                codec=TC_AUDIO_CODEC_LPCM, sample_rate=48000, channel_count=1,
                channel_layout=TC_AUDIO_LAYOUT_MONO, bits_per_sample=16,
                name="Empty"))
            mux.finish()


def test_probe_metadata_audio_tracks(codec, tmp_path) -> None:
    """工厂 probe：audio_tracks 逐轨元数据；旧字段 = 首轨视图。"""
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.topos_source import ToposMediaSource

    path = tmp_path / "probe_stems.mov"
    _write_stems(codec, path, with_aac=True)
    src = ToposMediaSource(str(path))
    MediaSourceFactory.open_source(src)
    try:
        md = src.probe()
        assert md.has_audio is True
        assert md.audio_codec == "pcm_f32be"   # 首轨视图
        assert md.audio_channels == 2
        assert len(md.audio_tracks) == 3
        assert [t["name"] for t in md.audio_tracks] == [
            "Music", "Dialogue", "Effects"]
        assert md.audio_tracks[1]["channel_count"] == 1
        assert md.audio_tracks[2]["priming_samples"] == 1024
    finally:
        src.close()


@pytest.mark.skipif(_FFPROBE is None, reason="ffprobe 不可用")
def test_ffprobe_multitrack(codec, tmp_path) -> None:
    """外部 oracle：ffprobe 识别 3 轨、各自声道/格式与轨名。"""
    path = tmp_path / "stems_probe.mov"
    _write_stems(codec, path, with_aac=True)
    out = subprocess.run(
        [_FFPROBE, "-v", "error", "-show_entries",
         "stream=index,codec_name,channels,channel_layout:stream_tags="
         "handler_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    import json
    streams = json.loads(out)["streams"]
    audio = [s for s in streams if s.get("codec_name") in
             ("lpcm", "pcm_s16be", "pcm_s24be", "pcm_s32be", "pcm_f32be",
              "aac")]
    assert len(audio) == 3, f"ffprobe 应识别 3 音轨，得 {len(audio)}"
    by_ch = {s["channels"]: s for s in audio}
    assert set(by_ch) == {1, 2, 6}
    # 部分ffmpeg构建对 mono 缺省 layout 字符串（1ch 已表意）
    assert by_ch[1].get("channel_layout") in (None, "mono")
    assert by_ch[2].get("channel_layout") == "stereo"
    names = {s.get("tags", {}).get("handler_name") for s in audio}
    assert {"Music", "Dialogue", "Effects"} <= names
