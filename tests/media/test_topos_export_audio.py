"""M-A6：Topos 导出音频轨 e2e（encoder 级；plan §3 M-A6 验收）。

96 帧 topos 源帧 → ToposVideoEncoder（proxy 档）→ 含音频输出：
- PCM 2.0（48k/16bit 大端 'twos'）→ ffprobe 双轨 + 时长≈视频时长；
- AAC 5.1（PyAV 应用层编码）→ ffprobe 读到 aac/6ch；
- preflight：受支持编解码不再触发 topos.video_only（见
  tests/deliver/test_video_export_preflight.py）。
"""
from __future__ import annotations

import json
import os
import subprocess
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


def _encode_with_audio(tmp_path: Path, name: str, audio_codec: str,
                       channels: int, frames: int = 96,
                       master: "np.ndarray | None" = None,
                       expect_failure: bool = False) -> "tuple[Path, float]":
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
            # 音频声明（首帧前；导出器在 open 后 add_audio_stream 同序）
            encoder.add_audio_stream(
                codec=audio_codec, sample_rate=48000, channels=channels,
                bitrate=192)
            for _ in range(frames):
                encoder.encode_frame(frame)
            # 音频母带：(channels, total_samples) float32，多频正弦；
            # 时长按源 fps 精确推导（与视频轨一致）
            rate = 48000
            fps = float(meta.fps.num) / float(meta.fps.den)
            duration = frames / fps
            total = int(rate * duration)
            if master is not None:
                pcm = master
            else:
                t = np.arange(total, dtype=np.float64) / rate
                pcm = np.zeros((channels, total), dtype=np.float32)
                gain = 0.5 / max(1, channels)
                for c in range(channels):
                    pcm[c] = (gain * (c + 1)
                              * np.sin(2 * np.pi * (440 + 110 * c) * t))
            if expect_failure:
                with pytest.raises(RuntimeError, match="NaN/Inf"):
                    encoder.write_audio_samples(pcm, rate)
                # 失败路径走 abort（不留伪成功文件），不走 close（finish
                # 因零音频样本被 C 层拒绝，会掩盖原始 NaN 诊断）
                encoder.abort()
                return output, duration
            encoder.write_audio_samples(pcm, rate)
        finally:
            encoder.close()
    finally:
        source.close()
    return output, duration


def _probe(path: Path) -> list[dict]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name,channels,sample_rate,"
         "bits_per_sample,duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True)
    return json.loads(out.stdout)["streams"]


class TestToposExportAudioE2E:
    def test_pcm_stereo_dual_track(self, require_lib, tmp_path) -> None:
        out, duration = _encode_with_audio(tmp_path, "pcm.mov",
                                           "pcm_s16le", 2)
        streams = _probe(out)
        video = [s for s in streams if s.get("codec_type") == "video"]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        assert len(video) == 1
        assert len(audio) == 1
        a = audio[0]
        # FFmpeg 对 lpcm 16-bit 直接映射为 pcm_s16be；24/32-bit 报 lpcm+位深
        assert a["codec_name"] in ("lpcm", "pcm_s16be", "pcm_s24be")
        assert int(a["sample_rate"]) == 48000
        assert int(a["channels"]) == 2
        # 音频时长 ≈ 视频时长（容差 < 1 采样帧粒度）
        assert abs(float(a["duration"]) - duration) < 0.001

    def test_f32_master_bit_exact(self, require_lib, tmp_path) -> None:
        """M-B4：pcm_f32le 母带直通——导出→ffmpeg 解码→与母带逐位相等
        （零量化；仅 BE↔LE 字节序换序）。"""
        rate, channels = 48000, 2
        rng = np.random.default_rng(20260920)
        total = rate * 4  # 4s
        master = (rng.standard_normal((channels, total)) * 0.3).astype(np.float32)
        out, _ = _encode_with_audio(tmp_path, "f32.mov", "pcm_f32le",
                                    channels, frames=96, master=master)
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:a",
             "-c:a", "pcm_f32le", "-f", "f32le", "-"],
            capture_output=True, check=True).stdout
        got = np.frombuffer(raw, dtype="<f4").reshape(-1, channels)
        n = min(got.shape[0], total)
        # 逐位相等（文件侧大端直存 → ffmpeg 解回小端；值域不 clip）
        assert np.array_equal(got[:n, :], master[:, :n].T), \
            "float32 母带直通非逐位相等（出现量化/削波）"

    def test_f32_nan_fail_fast(self, require_lib, tmp_path) -> None:
        """M-B4：float32 母带含 NaN → 写出 fail-fast 拒绝且不产出文件。"""
        rate, channels = 48000, 2
        total = rate * 4
        master = np.zeros((channels, total), dtype=np.float32)
        master[0, 1000] = np.nan
        out, _ = _encode_with_audio(tmp_path, "f32bad.mov", "pcm_f32le",
                                    channels, frames=96, master=master,
                                    expect_failure=True)
        assert not out.exists(), "NaN 母带不得产出文件"

    @pytest.mark.parametrize("codec,bits,scale", [
        ("pcm_s24le", 24, 8388607.0),
        ("pcm_s32le", 32, 2147483647.0),
    ])
    def test_int_pcm_master_bit_exact(self, require_lib, tmp_path, codec,
                                      bits, scale) -> None:
        """整数 PCM 档内容逐位校验：导出→ffmpeg 解码→与 rint(clip·满刻度)
        逐位相等（'twos' 大端语义）。s24 此前从未被解码校验（字节序 bug
        漏网处），s32 此前无写路径分支（导出必败）。"""
        rate, channels = 48000, 2
        rng = np.random.default_rng(20260921)
        total = rate * 2
        master = (rng.standard_normal((channels, total)) * 0.4).astype(np.float32)
        out, _ = _encode_with_audio(tmp_path, f"{codec}.mov", codec,
                                    channels, frames=96, master=master)
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:a",
             "-c:a", f"pcm_s{bits}le", "-f", f"s{bits}le", "-"],
            capture_output=True, check=True).stdout
        if bits == 24:  # numpy 无 3 字节 dtype：手工组装（s24le = 小端序）
            u = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = (u[:, 2] << 16) | (u[:, 1] << 8) | u[:, 0]
            got = ((v << 8) >> 8).reshape(-1, channels).astype(np.int64)
        else:
            got = np.frombuffer(raw, dtype=f"<i{bits // 8}").reshape(
                -1, channels).astype(np.int64)
        n = min(got.shape[0], total)
        # float64 量化口径与编码器一致（float32 满刻度乘法有 ±1 LSB 漂移）
        want = np.rint(np.clip(master.T.astype(np.float64), -1.0, 1.0)
                       * scale).astype(np.int64)
        assert np.array_equal(got[:n, :].astype(np.int64), want[:n, :]), (
            f"{codec} 解码值与量化口径不一致（字节序/位宽打包错误）")

    def test_aac_short_master_exportable(self, require_lib, tmp_path) -> None:
        """极短 AAC（≤1024 采样 = 单包）导出不得因 priming ≥ 总采样被拒：
        elst 语义不可表达时跳过 priming 声明，导出成功且 ffprobe 可读。
        （48fps 单帧导出 = 1000 采样即可触发原缺陷。）"""
        rate, channels = 48000, 2
        total = 1000  # 单 AAC 包（1024 采样，含填充）
        t = np.arange(total, dtype=np.float64) / rate
        master = np.stack([
            0.5 * np.sin(2 * np.pi * 440 * t),
            0.5 * np.sin(2 * np.pi * 660 * t)]).astype(np.float32)
        out, _ = _encode_with_audio(tmp_path, "aac_short.mov", "aac",
                                    channels, frames=2, master=master)
        streams = _probe(out)
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        assert len(audio) == 1
        assert audio[0]["codec_name"] == "aac"

    def test_aac_51_dual_track(self, require_lib, tmp_path) -> None:
        pytest.importorskip("av")
        out, duration = _encode_with_audio(tmp_path, "aac51.mov", "aac", 6)
        streams = _probe(out)
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        assert len(audio) == 1
        a = audio[0]
        assert a["codec_name"] == "aac"
        assert int(a["channels"]) == 6
        assert abs(float(a["duration"]) - duration) < 0.1  # AAC 帧粒度 21.3ms

    def test_decode_pcm_matches_duration(self, require_lib, tmp_path) -> None:
        """ffmpeg 解码 PCM 音轨：样本数与容器声明一致（roundtrip 完整性）。"""
        out, duration = _encode_with_audio(tmp_path, "pcm_dec.mov",
                                            "pcm_s16le", 2)
        wav = tmp_path / "decoded.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(out), "-map", "0:a",
             "-c:a", "pcm_s16le", "-f", "wav", str(wav)],
            check=True)
        import wave

        with wave.open(str(wav), "rb") as wf:
            assert wf.getframerate() == 48000
            assert wf.getnchannels() == 2
            n = wf.getnframes()
        assert abs(n / 48000.0 - duration) < 0.01
