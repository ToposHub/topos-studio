"""M-B3：topos 导出文件再导入回放（AudioDecoder 直连验证）。

导出闭环：源帧 → ToposVideoEncoder（含音轨）→ tops .mov →
AudioDecoder.open（PyAV 直开文件路径，不经 source 抽象）→ 解码：

- PCM 2.0（lpcm 16bit）：解码与量化母带逐位相等（16bit 域 /32768 精确）；
- AAC 5.1（mp4a，v1.4 带 elst）：解码非静音 + 互相关 > 0.9（有损容差；
  elst 使 PyAV 解码自动裁剪 priming，无需手动对齐）；
- 时间线插入语义 smoke：decode_next_samples 连续解码走完（Stage B
  remainder 机制不被 topos 容器破坏）。

调研结论（plan M-B3 任务 1，结论 (a)）：回放栈 AudioDecoder.open(path)
纯 PyAV 直开路径、无 codec/扩展名挡板——topos .mov（lpcm/mp4a 双轨）
由 ffmpeg mov 通用 demuxer 直接读取（v1.1 e2e 已证），零接线工作量。
ADR-C008 修订：再导入的导出文件即 master（渲染缓存/代理仍纯视频）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TEST_VIDEOS = Path(__file__).resolve().parents[1] / "test_videos"


def _source_video() -> Path:
    from tests.real_media import require_video
    return require_video("2K_topos_422_Proxy.mov", "2K_topos_422_HQ.mov")


@pytest.fixture(scope="module")
def require_lib():
    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is None and not any(
        p.is_file() for p in __import__(
            "src.shared.codec.topos_binding", fromlist=["candidate_library_paths"]
        ).candidate_library_paths()
    ):
        pytest.skip("libtopos_codec 未构建")


def _export_with_audio(tmp_path: Path, name: str, audio_codec: str,
                       channels: int, frames: int = 48,
                       ) -> "tuple[Path, np.ndarray, float]":
    """生产编码器路径导出含音轨文件；返回 (路径, 音频母带, fps)。"""
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.metadata import Rational
    from src.shared.media.topos_source import ToposMediaSource

    source = ToposMediaSource(str(_source_video()), decode_role="export")
    source.open()
    try:
        meta = source.probe()
        frame = source.read_frame(0)
        config = FFmpegEncoderConfig(
            codec="topos",
            width=int(meta.width),
            height=int(meta.height),
            fps=Rational(int(meta.fps.num), int(meta.fps.den)),
            pix_fmt="yuv422p10le",
            profile="proxy",
            crf=20,
            threads=0,
            pipelined_conversion=False,
            color_metadata=None,
        )
        output = tmp_path / name
        encoder = ToposVideoEncoder(str(output), config)
        encoder.open()
        try:
            encoder.add_audio_stream(
                codec=audio_codec, sample_rate=48000, channels=channels,
                bitrate=192)
            for _ in range(frames):
                encoder.encode_frame(frame)
            rate = 48000
            fps = float(meta.fps.num) / float(meta.fps.den)
            total = int(rate * (frames / fps))
            t = np.arange(total, dtype=np.float64) / rate
            pcm = np.zeros((channels, total), dtype=np.float32)
            gain = 0.5 / max(1, channels)
            for c in range(channels):
                pcm[c] = (gain * (c + 1)
                          * np.sin(2 * np.pi * (440 + 110 * c) * t))
            encoder.write_audio_samples(pcm, rate)
        finally:
            encoder.close()
    finally:
        source.close()
    return output, pcm, fps


def _decode_all(decoder, max_samples: int) -> np.ndarray:
    """decode_next_samples 连续解码到收满/结束（时间线插入 smoke 语义）。"""
    pieces: list[np.ndarray] = []
    need = max_samples
    while need > 0:
        got = decoder.decode_next_samples(min(need, 2048))
        if got is None or got.shape[1] == 0:
            break
        pieces.append(got)
        need -= got.shape[1]
    assert pieces, "解码零输出"
    return np.concatenate(pieces, axis=1)


def test_reimport_pcm_stereo_bit_exact(require_lib, tmp_path) -> None:
    """再导入 PCM 2.0：AudioDecoder 打开 + 16bit 域逐位相等 + 非静音。"""
    pytest.importorskip("av")
    from src.features.edit.core.audio_playback import AudioDecoder

    out, master, fps = _export_with_audio(tmp_path, "re_pcm.mov",
                                          "pcm_s16le", 2)
    dec = AudioDecoder()
    assert dec.open(out, timeline_fps=fps), "AudioDecoder 打开 topos 失败"
    try:
        assert dec.sample_rate == 48000
        assert dec.channels == 2
        assert dec.duration_samples > 0
        got = _decode_all(dec, master.shape[1])  # (ch, n)
        assert got.shape[0] == 2
        n = min(got.shape[1], master.shape[1])
        assert float(np.max(np.abs(got[:, :n]))) > 0.1, "解码静音"
        # lpcm 16bit：导出量化 (clip+rint*32767) → 存储 → 解码 /32768，
        # 全链无损 → 16bit 域逐位相等
        q = np.rint(np.clip(master, -1.0, 1.0) * 32767.0) / 32768.0
        assert np.array_equal(got[:, :n], q[:, :n]), "lpcm 16bit 域不逐位相等"
    finally:
        dec.close()


def test_reimport_aac_51_nonsilent_and_aligned(require_lib, tmp_path) -> None:
    """再导入 AAC 5.1：打开 + 非静音 + 互相关 > 0.9（elst 使 PyAV 自动
    裁剪 priming——解码输出与母带零手动对齐）。"""
    pytest.importorskip("av")
    from src.features.edit.core.audio_playback import AudioDecoder

    out, master, fps = _export_with_audio(tmp_path, "re_aac.mov",
                                          "aac", 6)
    dec = AudioDecoder()
    assert dec.open(out, timeline_fps=fps)
    try:
        assert dec.sample_rate == 48000
        assert dec.channels == 6
        got = _decode_all(dec, master.shape[1])  # (6, n)
        n = min(got.shape[1], master.shape[1])
        got = got[:, :n]
        ref = master[:, :n].astype(np.float64)
        assert float(np.max(np.abs(got))) > 0.05, "解码静音"
        # 互相关（AAC 有损 + 5.1 布局映射容差；elst 生效应零滞后对齐）
        a = got[0].astype(np.float64) - got[0].mean()
        b = ref[0] - ref[0].mean()
        denom = float(np.sqrt(np.sum(a * a) * np.sum(b * b))) or 1.0
        corr = float(np.sum(a * b) / denom)
        assert corr > 0.9, f"AAC 再导入相关性不足：{corr:.3f}"
    finally:
        dec.close()
