"""M-A4：v1.1 音频轨绑定层 roundtrip（container_spec_v1.1 附录）。

验收（plan §3 M-A4）：
- lpcm mux→demux PCM 逐字节 MD5 相等；
- mp4a + esds（ASC 原样）包级 roundtrip；
- fail-fast：缺符号直接抛错（新 dylib 唯一基线，无旧库降级）；
- movie_config 音频声明校验（半声明/白名单/声道配对）。

库未构建时整体 skip（构建方式见 native/topos_codec/README.md）。
"""
from __future__ import annotations

import ctypes
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import (
    TC_AUDIO_CODEC_LPCM,
    TC_AUDIO_CODEC_MP4A,
    TC_AUDIO_CODEC_NONE,
    TC_AUDIO_LAYOUT_5_1,
    TC_AUDIO_LAYOUT_STEREO,
    TOPOS_CODEC_ABI_VERSION,
    ToposAudioFormat,
    ToposCodec,
    ToposCodecError,
)

_W, _H = 64, 48


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
    # 新 dylib 唯一基线：缺音频符号直接抛错（fail-fast，无符号探测降级）
    for sym in ("tc_mux_add_audio", "tc_mux_set_audio_asc",
                "tc_movie_audio_info", "tc_movie_read_audio"):
        if not hasattr(lib, sym):
            pytest.fail(
                f"libtopos_codec 缺少音频轨符号 {sym}——请重建：bash "
                f"native/topos_codec/scripts/build_release_repo.sh")
    return ToposCodec(lib)


def _frame_config() -> topos_binding._CFrameConfig:
    """帧级配置（tpcC 字段与 movie config 逐字段一致）。"""
    fc = topos_binding._CFrameConfig()
    fc.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = _W
    fc.visible_height = _H
    fc.profile = 3
    fc.pixel_format = 0  # 4:2:2
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


def _synth_planes() -> list[bytes]:
    """确定性 4:2:2 合成帧平面（uint16 LE bytes；色度半宽）。"""
    import struct

    cw = (_W + 1) // 2
    y = struct.pack(f"<{_W * _H}H",
                    *[((x * 7 + r * 13) % 400 + 300)
                      for r in range(_H) for x in range(_W)])
    u = struct.pack(f"<{cw * _H}H",
                    *[(480 + (x % 5) * 8) for r in range(_H) for x in range(cw)])
    v = struct.pack(f"<{cw * _H}H",
                    *[(520 + (r % 4) * 6) for r in range(_H) for x in range(cw)])
    return [y, u, v]


_PLANES = None


def _encode_packets(codec: ToposCodec, count: int) -> list[bytes]:
    """确定性合成 video packet（同内容重复编码即可，tpcC 与 movie cfg 一致）。"""
    global _PLANES
    if _PLANES is None:
        _PLANES = _synth_planes()
    fc = _frame_config()
    return [codec.encode_frame(fc, _PLANES)[0] for _ in range(count)]


def test_movie_config_audio_declaration(codec: ToposCodec) -> None:
    """音频声明写入 reserved 槽位；半声明/非法值 fail-fast。"""
    cfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                             audio_sample_rate=48000, audio_channels=2,
                             audio_layout=TC_AUDIO_LAYOUT_STEREO,
                             audio_bits_per_sample=24)
    assert cfg.reserved[0] == TC_AUDIO_CODEC_LPCM
    assert cfg.reserved[1] == 48000
    assert cfg.reserved[2] == 2
    assert cfg.reserved[3] == TC_AUDIO_LAYOUT_STEREO
    assert cfg.reserved[4] == 24
    # 缺省 = 无音轨（既有调用方行为不变）
    cfg0 = codec.movie_config(_W, _H)
    assert all(v == 0 for v in cfg0.reserved)
    # 半声明拒绝
    with pytest.raises(ToposCodecError, match="防半声明"):
        codec.movie_config(_W, _H, audio_sample_rate=48000)
    # 采样率白名单
    with pytest.raises(ToposCodecError, match="44100/48000"):
        codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                           audio_sample_rate=32000, audio_channels=2,
                           audio_layout=TC_AUDIO_LAYOUT_STEREO,
                           audio_bits_per_sample=16)
    # 声道/布局配对
    with pytest.raises(ToposCodecError, match="声道/布局"):
        codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                           audio_sample_rate=48000, audio_channels=6,
                           audio_layout=TC_AUDIO_LAYOUT_STEREO,
                           audio_bits_per_sample=16)
    # 5.1 合法
    cfg51 = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                               audio_sample_rate=48000, audio_channels=6,
                               audio_layout=TC_AUDIO_LAYOUT_5_1,
                               audio_bits_per_sample=16)
    assert cfg51.reserved[2] == 6
    # mp4a 不接受位深声明
    with pytest.raises(ToposCodecError, match="mp4a"):
        codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_MP4A,
                           audio_sample_rate=48000, audio_channels=2,
                           audio_layout=TC_AUDIO_LAYOUT_STEREO,
                           audio_bits_per_sample=24)


def test_lpcm_roundtrip_md5(codec: ToposCodec, tmp_path) -> None:
    """lpcm 48k/24bit/2.0 注入 → 读回逐字节 MD5 相等（M-A4 验收）。"""
    import numpy as np

    frames = 96
    chunk_frames = 1024
    rate, channels, bits = 48000, 2, 24
    rng = np.random.default_rng(20260919)
    pcm_chunks = [
        rng.integers(0, 256, size=chunk_frames * channels * (bits // 8),
                     dtype=np.uint8).tobytes()
        for _ in range(frames)
    ]
    pkts = _encode_packets(codec, frames)

    cfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                             audio_sample_rate=rate, audio_channels=channels,
                             audio_layout=TC_AUDIO_LAYOUT_STEREO,
                             audio_bits_per_sample=bits)
    path = tmp_path / "audio_lpcm.mov"
    with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
        for i in range(frames):
            mux.add_packet(pkts[i], i, 1)
            mux.add_audio(pcm_chunks[i], chunk_frames)
        mux.finish()

    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        info = mv.info()
        assert info.sample_count == frames
        ai = mv.audio_info()
        assert ai.has_audio
        assert ai.codec == TC_AUDIO_CODEC_LPCM
        assert (ai.sample_rate, ai.channel_count, ai.channel_layout,
                ai.bits_per_sample) == (rate, channels,
                                        TC_AUDIO_LAYOUT_STEREO, bits)
        assert ai.sample_count == chunk_frames * frames
        assert ai.chunk_count == frames
        # 分段读回逐字节 MD5 相等
        digest = hashlib.md5()
        step = 16
        for start in range(0, frames, step):
            digest.update(mv.read_audio_samples(start,
                                                min(step, frames - start)))
        expect = hashlib.md5()
        for c in pcm_chunks:
            expect.update(c)
        assert digest.hexdigest() == expect.hexdigest()
        # 单 chunk 读回
        assert mv.read_audio_samples(3, 1) == pcm_chunks[3]


def test_aac_roundtrip_packets(codec: ToposCodec, tmp_path) -> None:
    """mp4a + esds：ASC 原样写入、逐包变长 roundtrip。"""
    frames = 32
    pkts = _encode_packets(codec, frames)
    aac_pkts = [bytes([(0x21 + i) & 0xFF]) + bytes(64 + (i % 37))
                for i in range(frames)]
    asc = b"\x12\x10"  # AAC-LC 48kHz stereo

    cfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_MP4A,
                             audio_sample_rate=48000, audio_channels=2,
                             audio_layout=TC_AUDIO_LAYOUT_STEREO)
    path = tmp_path / "audio_aac.mov"
    with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
        mux.set_audio_asc(asc)
        for i in range(frames):
            mux.add_packet(pkts[i], i, 1)
            mux.add_audio(aac_pkts[i], 1024)
        mux.finish()

    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        ai = mv.audio_info()
        assert ai.codec == TC_AUDIO_CODEC_MP4A
        assert ai.bits_per_sample == 0
        assert ai.chunk_count == frames
        for i in range(frames):
            assert mv.read_audio_samples(i, 1) == aac_pkts[i]


def test_no_audio_file_info(codec: ToposCodec, tmp_path) -> None:
    """纯视频文件：audio_info.codec=NONE 不报错；read_audio 抛错。"""
    pkts = _encode_packets(codec, 4)
    path = tmp_path / "noaudio.mov"
    with topos_binding.ToposMuxFile(
            codec, str(path), codec.movie_config(_W, _H)) as mux:
        for i, pkt in enumerate(pkts):
            mux.add_packet(pkt, i, 1)
        mux.finish()
    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        ai = mv.audio_info()
        assert ai.codec == TC_AUDIO_CODEC_NONE
        assert ai.sample_count == 0
        assert not ai.has_audio
        with pytest.raises(ToposCodecError, match="无音频轨"):
            mv.read_audio_samples()


def test_declared_audio_without_samples_fails(codec: ToposCodec,
                                              tmp_path) -> None:
    """声明音频但零样本 → finish fail-fast（与 C 侧规则一致）。"""
    pkts = _encode_packets(codec, 2)
    cfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                             audio_sample_rate=48000, audio_channels=2,
                             audio_layout=TC_AUDIO_LAYOUT_STEREO,
                             audio_bits_per_sample=16)
    path = tmp_path / "empty_audio.mov"
    mux = topos_binding.ToposMuxFile(codec, str(path), cfg)
    try:
        for i, pkt in enumerate(pkts):
            mux.add_packet(pkt, i, 1)
        with pytest.raises(ToposCodecError, match="未注入任何音频"):
            mux.finish()
    finally:
        mux.close()


def test_mp4a_requires_asc(codec: ToposCodec, tmp_path) -> None:
    """mp4a 未声明 ASC → add_audio 拒绝（STATE）。"""
    pkts = _encode_packets(codec, 1)
    cfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_MP4A,
                             audio_sample_rate=48000, audio_channels=2,
                             audio_layout=TC_AUDIO_LAYOUT_STEREO)
    path = tmp_path / "noasc.mov"
    mux = topos_binding.ToposMuxFile(codec, str(path), cfg)
    try:
        mux.add_packet(pkts[0], 0, 1)
        with pytest.raises(ToposCodecError, match="AudioSpecificConfig"):
            mux.add_audio(b"\x00" * 128, 1024)
    finally:
        mux.close()


def test_aac_real_encode_energy_roundtrip(codec: ToposCodec, tmp_path) -> None:
    """真实 AAC 闭环（M-A3 验收）：源 PCM 正弦 → PyAV aac 编码 → topos
    容器（mp4a+esds）→ ffmpeg 解码 → 与源对比能量/相关性（容差断言，
    非逐位——AAC 有损）。"""
    av = pytest.importorskip("av")
    import numpy as np

    rate = 48000
    channels = 2
    seconds = 1.0
    total = int(rate * seconds)
    t = np.arange(total, dtype=np.float64) / rate
    tone = 0.6 * np.sin(2 * np.pi * 440.0 * t)
    src = np.zeros((total, channels), dtype=np.float32)
    src[:, 0] = tone.astype(np.float32)
    src[:, 1] = (0.8 * tone).astype(np.float32)

    # PyAV：float32 → AAC 包 + ASC（应用层编码，native 只存包）
    enc = av.CodecContext.create("aac", "w")
    enc.sample_rate = rate
    enc.layout = "stereo"
    enc.format = "fltp"
    aac_pkts: list[bytes] = []
    asc: bytes = b""

    def _feed(block: "np.ndarray") -> None:
        nonlocal asc
        planar = np.ascontiguousarray(block.T)
        frame = av.AudioFrame.from_ndarray(planar, format="fltp",
                                           layout="stereo")
        frame.sample_rate = rate
        for pkt in enc.encode(frame):
            if not asc:
                asc = bytes(enc.extradata) if enc.extradata else b"\x12\x10"
            aac_pkts.append(bytes(pkt))

    for off in range(0, total, 1024):
        block = src[off:off + 1024]
        if block.shape[0] < 1024:  # 末块补零到整帧
            block = np.vstack([block,
                               np.zeros((1024 - block.shape[0], channels),
                                        dtype=np.float32)])
        _feed(block)
    for pkt in enc.encode(None):
        if not asc:
            asc = bytes(enc.extradata) if enc.extradata else b"\x12\x10"
        aac_pkts.append(bytes(pkt))
    assert aac_pkts and asc, "PyAV 未产出 AAC 包/ASC"

    pkts = _encode_packets(codec, len(aac_pkts))
    cfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_MP4A,
                             audio_sample_rate=rate, audio_channels=channels,
                             audio_layout=TC_AUDIO_LAYOUT_STEREO)
    path = tmp_path / "audio_real_aac.mov"
    from src.shared.export.topos_encoder import TOPOS_AAC_E2E_PRIMING_SAMPLES
    with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
        mux.set_audio_asc(asc)
        for i, ap in enumerate(aac_pkts):
            mux.add_packet(pkts[i], i, 1)
            mux.add_audio(ap, 1024)
        # M-B1：全部包注入后声明 priming（与生产编码器同口径）
        mux.set_audio_priming(TOPOS_AAC_E2E_PRIMING_SAMPLES)
        mux.finish()

    # ffmpeg oracle：解码容器内音轨，与源对比（时长 + 相关性 + 能量）
    import subprocess

    wav = tmp_path / "decoded.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", "0:a",
         "-c:a", "pcm_s16le", "-f", "wav", str(wav)],
        check=True)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name,sample_rate,channels,duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True)
    import json

    streams = json.loads(probe.stdout)["streams"]
    audio = [s for s in streams if s.get("codec_type") == "audio"
             or s.get("codec_name") == "aac"]
    assert audio, "ffprobe 未读到音轨"
    assert int(audio[0]["sample_rate"]) == rate
    assert int(audio[0]["channels"]) == channels

    # M-B1：elst 落地——容器读回 priming；demux 侧 skip_samples 精确等于
    # priming（逐采样确定性契约，ADR-C052）
    from src.shared.codec.topos_binding import ToposMovieFile
    from src.shared.export.topos_encoder import TOPOS_AAC_E2E_PRIMING_SAMPLES
    with ToposMovieFile(codec, str(path)) as mv:
        assert mv.audio_info().priming_samples == TOPOS_AAC_E2E_PRIMING_SAMPLES
    pk_probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_packets", "-read_intervals", "%+#1", "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    pk0 = json.loads(pk_probe.stdout)["packets"][0]
    assert int(pk0["pts"]) == -TOPOS_AAC_E2E_PRIMING_SAMPLES, \
        f"首包 pts {pk0['pts']} ≠ -priming（elst 未生效）"
    sd = pk0.get("side_data_list", [])
    skips = [d.get("skip_samples") for d in sd
             if d.get("side_data_type") == "Skip Samples"]
    assert skips and int(skips[0]) == TOPOS_AAC_E2E_PRIMING_SAMPLES, \
        f"skip_samples {skips} ≠ priming（解码裁剪未生效）"

    import wave

    with wave.open(str(wav), "rb") as wf:
        assert wf.getframerate() == rate
        assert wf.getnchannels() == channels
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    got = data.reshape(-1, channels).astype(np.float64) / 32768.0
    # M-B1：容器已带 elst（media_time = priming，见 ADR-C052）——ffmpeg
    # demux 转为 pts=-media_time + skip_samples=media_time（上面已精确断言），
    # 解码裁剪后端到端残差 = 0（FFT 校准实测）。此处 FFT 全长互相关
    # 断言 |lag| ≤ 1 采样（窗口互相关对纯正弦有周期歧义，不可用）。
    assert got.shape[0] > total // 2
    seg = 8192  # 能量守恒对比窗
    ref_seg = src[8192:8192 + seg, 0]
    ref_norm = ref_seg - ref_seg.mean()
    ref_energy = float(np.sum(ref_norm ** 2))
    dec = got[:total, :]
    e_ref = float(np.mean(ref_seg ** 2))
    e_got = float(np.mean(dec[8192:8192 + seg, 0] ** 2))
    assert e_ref > 0 and abs(e_got - e_ref) / e_ref < 0.05
    N_fft = 1 << 17
    R = np.fft.rfft(src[:total, 0], N_fft)
    G = np.fft.rfft(got[:len(got), 0], N_fft)
    xc = np.fft.irfft(G * np.conj(R), N_fft)
    fft_lag = int(np.argmax(xc))
    if fft_lag > N_fft // 2:
        fft_lag -= N_fft
    assert abs(fft_lag) <= 1, \
        f"elst 对齐残差 {fft_lag} 采样（期望 |lag| ≤ 1；ADR-C052 校准漂移）"


def test_rate_family_and_float32(codec: ToposCodec, tmp_path) -> None:
    """M-B4：全家族采样率 roundtrip + float32 直存逐字节 + sample_format 读回。"""
    import hashlib
    import json
    import subprocess

    import numpy as np
    from src.shared.codec.topos_binding import TC_AUDIO_FMT_FLOAT32

    rate = 96000
    chunk_frames = 3840  # 96k/25fps
    chunks = 6
    rng = np.random.default_rng(20260920)
    master = rng.standard_normal((chunk_frames * chunks, 2)) * 0.4
    packed = np.ascontiguousarray(master.astype(np.float32).T.astype(">f4")).tobytes()

    cfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                             audio_sample_rate=rate, audio_channels=2,
                             audio_layout=TC_AUDIO_LAYOUT_STEREO,
                             audio_bits_per_sample=32,
                             audio_sample_format=TC_AUDIO_FMT_FLOAT32)
    path = tmp_path / "audio_f32_96k.mov"
    pk = _encode_packets(codec, chunks)
    with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
        for i in range(chunks):
            mux.add_packet(pk[i], i, 1)
            mux.add_audio(packed[i * chunk_frames * 8:(i + 1) * chunk_frames * 8],
                          chunk_frames)
        mux.finish()

    # 读回：逐字节相等 + sample_format=float32 + priming=0
    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        ai = mv.audio_info()
        assert (ai.sample_rate, ai.channel_count, ai.bits_per_sample) == (
            rate, 2, 32)
        assert ai.sample_format == TC_AUDIO_FMT_FLOAT32
        got = mv.read_audio_samples(0, chunks)
    assert hashlib.md5(got).hexdigest() == hashlib.md5(packed).hexdigest()

    # ffprobe：V2 声样描述（>65535Hz）→ pcm_f32be/96000
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_name,sample_rate,channels", "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    st = json.loads(probe.stdout)["streams"][0]
    assert st["codec_name"] == "pcm_f32be", st
    assert int(st["sample_rate"]) == rate

    # 全家族采样率（lpcm s16 抽验 roundtrip 采样率读回）
    for r in (44100, 48000, 88200, 176400, 192000):
        cf = int(r / 25)
        cfg_r = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                                   audio_sample_rate=r, audio_channels=2,
                                   audio_layout=TC_AUDIO_LAYOUT_STEREO,
                                   audio_bits_per_sample=16)
        p_r = tmp_path / f"audio_s16_{r}.mov"
        with topos_binding.ToposMuxFile(codec, str(p_r), cfg_r) as mux:
            for i in range(4):
                mux.add_packet(pk[i], i, 1)
                mux.add_audio(b"\x00" * (cf * 4), cf)
            mux.finish()
        with topos_binding.ToposMovieFile(codec, str(p_r)) as mv:
            assert mv.audio_info().sample_rate == r

    # float32 校验拒绝：mp4a+float / 24bit+float / codec=NONE+float
    from src.shared.codec.topos_binding import (
        TC_AUDIO_CODEC_MP4A,
        TC_AUDIO_CODEC_NONE,
        ToposCodecError,
    )
    with pytest.raises(ToposCodecError, match="float32"):
        codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_MP4A,
                           audio_sample_rate=48000, audio_channels=2,
                           audio_layout=TC_AUDIO_LAYOUT_STEREO,
                           audio_sample_format=TC_AUDIO_FMT_FLOAT32)
    with pytest.raises(ToposCodecError, match="float32"):
        codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                           audio_sample_rate=48000, audio_channels=2,
                           audio_layout=TC_AUDIO_LAYOUT_STEREO,
                           audio_bits_per_sample=24,
                           audio_sample_format=TC_AUDIO_FMT_FLOAT32)
    with pytest.raises(ToposCodecError, match="codec=NONE"):
        codec.movie_config(_W, _H,
                           audio_sample_format=TC_AUDIO_FMT_FLOAT32)


def test_priming_roundtrip_and_rejections(codec: ToposCodec, tmp_path) -> None:
    """M-B1：elst/priming 绑定层 roundtrip + fail-fast 校验。"""
    from src.shared.codec.topos_binding import ToposCodecError

    def _mux_aac(path, priming, chunks=8):
        cfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_MP4A,
                                 audio_sample_rate=48000, audio_channels=2,
                                 audio_layout=TC_AUDIO_LAYOUT_STEREO)
        pk = _encode_packets(codec, chunks)
        with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
            mux.set_audio_asc(b"\x12\x10")
            for i in range(chunks):
                mux.add_packet(pk[i], i, 1)
                mux.add_audio(b"\x00" * 64, 1024)
            if priming:
                mux.set_audio_priming(priming)
            mux.finish()

    # roundtrip：priming 读回相等；faststart 保留
    p1 = tmp_path / "primed.mov"
    _mux_aac(p1, 2048)
    with topos_binding.ToposMovieFile(codec, str(p1)) as mv:
        assert mv.audio_info().priming_samples == 2048
    p2 = tmp_path / "primed_fs.mov"
    codec.faststart_file(str(p1), str(p2))
    with topos_binding.ToposMovieFile(codec, str(p2)) as mv:
        assert mv.audio_info().priming_samples == 2048

    # 未声明 → 0（旧文件兼容视图）
    p3 = tmp_path / "noprime.mov"
    _mux_aac(p3, 0)
    with topos_binding.ToposMovieFile(codec, str(p3)) as mv:
        assert mv.audio_info().priming_samples == 0

    # fail-fast：零值 / ≥ 总采样数 / 重复 / lpcm
    cfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_MP4A,
                             audio_sample_rate=48000, audio_channels=2,
                             audio_layout=TC_AUDIO_LAYOUT_STEREO)
    pk = _encode_packets(codec, 4)
    with pytest.raises(ToposCodecError, match="须 > 0"):
        with topos_binding.ToposMuxFile(codec, str(tmp_path / "z.mov"),
                                        cfg) as mux:
            mux.set_audio_asc(b"\x12\x10")
            for i in range(4):
                mux.add_packet(pk[i], i, 1)
                mux.add_audio(b"\x00" * 64, 1024)
            mux.set_audio_priming(0)
    with pytest.raises(ToposCodecError, match="须 <"):
        with topos_binding.ToposMuxFile(codec, str(tmp_path / "big.mov"),
                                        cfg) as mux:
            mux.set_audio_asc(b"\x12\x10")
            for i in range(4):
                mux.add_packet(pk[i], i, 1)
                mux.add_audio(b"\x00" * 64, 1024)
            mux.set_audio_priming(4096)  # == 总采样数 4×1024
    with pytest.raises(ToposCodecError, match="至多一次"):
        with topos_binding.ToposMuxFile(codec, str(tmp_path / "dup.mov"),
                                        cfg) as mux:
            mux.set_audio_asc(b"\x12\x10")
            for i in range(4):
                mux.add_packet(pk[i], i, 1)
                mux.add_audio(b"\x00" * 64, 1024)
            mux.set_audio_priming(512)
            mux.set_audio_priming(256)

    lcfg = codec.movie_config(_W, _H, audio_codec=TC_AUDIO_CODEC_LPCM,
                              audio_sample_rate=48000, audio_channels=2,
                              audio_layout=TC_AUDIO_LAYOUT_STEREO,
                              audio_bits_per_sample=16)
    with pytest.raises(ToposCodecError, match="mp4a"):
        with topos_binding.ToposMuxFile(codec, str(tmp_path / "l.mov"),
                                        lcfg) as mux:
            mux.set_audio_priming(1024)


def test_topos_audio_format_dataclass() -> None:
    """ToposAudioFormat 结构化声明的缺省语义。"""
    fmt = ToposAudioFormat()
    assert fmt.codec == TC_AUDIO_CODEC_NONE
    assert fmt.sample_rate == 0
    fmt2 = ToposAudioFormat(codec=TC_AUDIO_CODEC_LPCM, sample_rate=48000,
                            channel_count=2,
                            channel_layout=TC_AUDIO_LAYOUT_STEREO,
                            bits_per_sample=24)
    assert fmt2.bits_per_sample == 24
