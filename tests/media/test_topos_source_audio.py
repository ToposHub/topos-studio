"""M-A5：ToposMediaSource 音频轨接入（v1.1）。

验收（plan §3 M-A5）：
- 工厂 probe 带音频 topos 文件字段正确（has_audio/channels/sample_rate/audio_codec）；
- read_audio_samples 与绑定层读回一致（逐采样值相等）；
- 无音轨文件行为不变（has_audio=False，read → None）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import (
    TC_AUDIO_CODEC_LPCM,
    TC_AUDIO_LAYOUT_STEREO,
)

W, H, N = 64, 48, 8
FRAME_DUR = 1000
TIMESCALE = 24000
RATE = 48000


@pytest.fixture(scope="module")
def codec():
    import os

    status, lib_path, lib = topos_binding.first_resolved_library()
    if status == topos_binding.LIB_RESOLUTION_NOT_FOUND:
        pytest.skip("libtopos_codec 未构建")
    if lib is None:
        pytest.fail(f"libtopos_codec 不可加载（{status}）：{lib_path}")
    return topos_binding.ToposCodec(lib)


def _frame_config() -> topos_binding._CFrameConfig:
    import ctypes

    fc = topos_binding._CFrameConfig()
    fc.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
    fc.abi_version = topos_binding.TOPOS_CODEC_ABI_VERSION
    fc.visible_width = W
    fc.visible_height = H
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


def _plane_bytes(i: int) -> list[bytes]:
    import struct

    cw = (W + 1) // 2
    y = struct.pack(f"<{W * H}H",
                    *[((x * 7 + r * 13 + i) % 400 + 300)
                      for r in range(H) for x in range(W)])
    u = struct.pack(f"<{cw * H}H", *[(480 + (x % 5) * 8)
                                     for r in range(H) for x in range(cw)])
    v = struct.pack(f"<{cw * H}H", *[(520 + (r % 4) * 6)
                                     for r in range(H) for x in range(cw)])
    return [y, u, v]


def _write_movie_with_audio(codec, path: str, audio_pcm: list[bytes]) -> None:
    """topos(lpcm 48k/16bit/stereo)：FRAME_DUR tick 视频帧 + 每帧 1000 采样帧音频。"""
    fc = _frame_config()
    kw: dict = {}
    if audio_pcm:
        kw = dict(audio_codec=TC_AUDIO_CODEC_LPCM, audio_sample_rate=RATE,
                  audio_channels=2, audio_layout=TC_AUDIO_LAYOUT_STEREO,
                  audio_bits_per_sample=16)
    mc = codec.movie_config(W, H, qp=24, qmatrix=1, **kw)
    with topos_binding.ToposMuxFile(codec, path, mc) as mux:
        for i in range(N):
            pkt, _ = codec.encode_frame(fc, _plane_bytes(i))
            mux.add_packet(pkt, pts=i * FRAME_DUR, dur=FRAME_DUR)
            if i < len(audio_pcm):
                mux.add_audio(audio_pcm[i], RATE * FRAME_DUR // TIMESCALE)
        mux.finish()


def test_probe_and_read_audio(codec, tmp_path) -> None:
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.topos_source import ToposMediaSource

    rng = np.random.default_rng(7)
    chunk_frames = RATE * FRAME_DUR // TIMESCALE  # 2000 帧/chunk
    pcm_chunks = [
        rng.integers(-32768, 32767, size=chunk_frames * 2,
                     dtype=np.int16).astype(np.int32)
        for _ in range(N)
    ]
    be_chunks = [c.astype('>i2').tobytes() for c in pcm_chunks]
    path = str(tmp_path / "audio.mov")
    _write_movie_with_audio(codec, path, be_chunks)

    src = ToposMediaSource(path)
    MediaSourceFactory.open_source(src)
    try:
        md = src.probe()
        assert md.has_audio is True
        assert md.audio_channels == 2
        assert md.audio_sample_rate == RATE
        assert md.audio_codec == 'pcm_s16be'

        # read_audio_samples 与绑定层读回一致（逐采样）
        got = src.read_audio_samples(0, chunk_frames * N)
        assert got is not None
        assert got.dtype == np.int32
        assert got.shape == (chunk_frames * N, 2)
        expect = np.concatenate(pcm_chunks, axis=0).reshape(-1, 2)
        assert np.array_equal(got, expect)
        # 局部区间（跨 chunk 边界）
        mid = src.read_audio_samples(chunk_frames - 100, 200)
        assert mid.shape == (200, 2)
        assert np.array_equal(mid, expect[chunk_frames - 100:chunk_frames + 100])
        # 起点越界 → None
        assert src.read_audio_samples(chunk_frames * N + 1, 10) is None
    finally:
        src.close()


def test_probe_no_audio_unchanged(codec, tmp_path) -> None:
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "noaudio.mov")
    _write_movie_with_audio(codec, path, [])
    src = ToposMediaSource(path)
    MediaSourceFactory.open_source(src)
    try:
        md = src.probe()
        assert md.has_audio is False
        assert md.audio_channels is None
        assert md.audio_sample_rate is None
        assert md.audio_codec is None
        assert src.read_audio_samples(0, 100) is None
    finally:
        src.close()


# ---------------- C06（2026-09-27）：float32 PCM 解析语义 ----------------

def _write_movie_with_audio_fmt(codec, path: str, audio_pcm: list[bytes],
                                bits: int, sample_format: int) -> None:
    """指定位深/样本格式的 lpcm 音频 topos 文件（其余同 _write_movie_with_audio）。"""
    from src.shared.codec.topos_binding import TC_AUDIO_FMT_INT

    fc = _frame_config()
    kw: dict = {}
    if audio_pcm:
        kw = dict(audio_codec=TC_AUDIO_CODEC_LPCM, audio_sample_rate=RATE,
                  audio_channels=2, audio_layout=TC_AUDIO_LAYOUT_STEREO,
                  audio_bits_per_sample=bits,
                  audio_sample_format=sample_format)
    mc = codec.movie_config(W, H, qp=24, qmatrix=1, **kw)
    with topos_binding.ToposMuxFile(codec, path, mc) as mux:
        for i in range(N):
            pkt, _ = codec.encode_frame(fc, _plane_bytes(i))
            mux.add_packet(pkt, pts=i * FRAME_DUR, dur=FRAME_DUR)
            if i < len(audio_pcm):
                mux.add_audio(audio_pcm[i], RATE * FRAME_DUR // TIMESCALE)
        mux.finish()


def test_read_audio_float32_exact(codec, tmp_path) -> None:
    """C06 反例：pcm_f32be 采样 0.25 曾被按 >i4 读成 1048576000。

    验收：正/负/零/满幅采样逐样本一致、双声道、跨 chunk 与末尾截断。"""
    from src.shared.codec.topos_binding import TC_AUDIO_FMT_FLOAT32
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.topos_source import ToposMediaSource

    chunk_frames = RATE * FRAME_DUR // TIMESCALE  # 2000
    l = np.linspace(-1.0, 1.0, chunk_frames * N, dtype=np.float32)
    l[0], l[1], l[2] = 0.25, -0.25, 0.0  # 计划点名值 + 零 + 负值
    r = -l
    master = np.stack([l, r], axis=1)
    chunks = [master[i * chunk_frames:(i + 1) * chunk_frames]
              for i in range(N)]
    be_chunks = [c.astype('>f4').tobytes() for c in chunks]
    path = str(tmp_path / "audio_f32.mov")
    _write_movie_with_audio_fmt(codec, path, be_chunks, 32, TC_AUDIO_FMT_FLOAT32)

    src = ToposMediaSource(path)
    MediaSourceFactory.open_source(src)
    try:
        ai = src._movie.audio_info()
        assert ai.bits_per_sample == 32
        assert ai.sample_format == TC_AUDIO_FMT_FLOAT32
        got = src.read_audio_samples(0, chunk_frames * N)
        assert got is not None and got.dtype == np.float32
        assert np.array_equal(got, master)
        # 跨 chunk 区间
        mid = src.read_audio_samples(chunk_frames - 100, 200)
        assert np.array_equal(mid, master[chunk_frames - 100:chunk_frames + 100])
        # 末尾截断
        tail = src.read_audio_samples(chunk_frames * N - 5, 100)
        assert tail.shape == (5, 2)
        assert np.array_equal(tail, master[-5:])
    finally:
        src.close()


def test_read_audio_s32_regression(codec, tmp_path) -> None:
    """C06：s32 整数 PCM 路径不变（仍按 >i4 → int32）。"""
    from src.shared.codec.topos_binding import TC_AUDIO_FMT_INT
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.topos_source import ToposMediaSource

    chunk_frames = RATE * FRAME_DUR // TIMESCALE
    rng = np.random.default_rng(11)
    master = rng.integers(-2**31, 2**31 - 1, size=(chunk_frames * N, 2),
                          dtype=np.int64).astype(np.int32)
    chunks = [master[i * chunk_frames:(i + 1) * chunk_frames]
              for i in range(N)]
    be_chunks = [c.astype('>i4').tobytes() for c in chunks]
    path = str(tmp_path / "audio_s32.mov")
    _write_movie_with_audio_fmt(codec, path, be_chunks, 32, TC_AUDIO_FMT_INT)

    src = ToposMediaSource(path)
    MediaSourceFactory.open_source(src)
    try:
        got = src.read_audio_samples(0, chunk_frames * N)
        assert got is not None and got.dtype == np.int32
        assert np.array_equal(got, master)
    finally:
        src.close()


def test_tail_seek_does_not_scan_from_first_chunk(codec, tmp_path,
                                                  monkeypatch) -> None:
    """C17：尾部随机读取只触碰覆盖区间附近的 chunk（数据读取数不随总时长
    线性增长），且内容逐样本一致。"""
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.topos_source import ToposMediaSource

    chunk_frames = RATE * FRAME_DUR // TIMESCALE  # 2000
    n_chunks = 30
    rng = np.random.default_rng(23)
    master = rng.integers(-32768, 32767, size=(chunk_frames * n_chunks, 2),
                          dtype=np.int16).astype(np.int32)
    be_chunks = [master[i * chunk_frames:(i + 1) * chunk_frames]
                 .astype('>i2').tobytes() for i in range(n_chunks)]

    fc = _frame_config()
    mc = codec.movie_config(
        W, H, qp=24, qmatrix=1, audio_codec=TC_AUDIO_CODEC_LPCM,
        audio_sample_rate=RATE, audio_channels=2,
        audio_layout=TC_AUDIO_LAYOUT_STEREO, audio_bits_per_sample=16)
    path = str(tmp_path / "long_audio.mov")
    with topos_binding.ToposMuxFile(codec, path, mc) as mux:
        for i in range(n_chunks):
            pkt, _ = codec.encode_frame(fc, _plane_bytes(i % N))
            mux.add_packet(pkt, pts=i * FRAME_DUR, dur=FRAME_DUR)
            mux.add_audio(be_chunks[i], chunk_frames)  # 每帧多 chunk 注入
        mux.finish()

    src = ToposMediaSource(path)
    MediaSourceFactory.open_source(src)
    try:
        data_calls = {"n": 0}
        real_read = src._movie.read_audio_samples

        def counting_read(*a, **kw):
            data_calls["n"] += 1
            return real_read(*a, **kw)
        monkeypatch.setattr(src._movie, "read_audio_samples", counting_read)

        # 尾部随机读（index 构建的 probe 走 audio_chunk_size，不计数据读）
        got = src.read_audio_samples(chunk_frames * n_chunks - 100, 100)
        assert got is not None
        assert np.array_equal(got, master[-100:])
        assert data_calls["n"] <= 4, (
            f"尾部读取触碰 {data_calls['n']} 个数据 chunk（线性扫描 >30）")

        # 首部/中部随机读逐样本一致
        head = src.read_audio_samples(0, 500)
        assert np.array_equal(head, master[:500])
        mid = src.read_audio_samples(chunk_frames * 15 - 7, 99)
        assert np.array_equal(mid, master[chunk_frames * 15 - 7:
                                          chunk_frames * 15 + 92])
    finally:
        src.close()
