"""N06/C19 防线回归：软件 libx265 10-bit 的确定性崩溃拒绝。

上游缺陷（PyAV 17.0.1 / ffmpeg 8.0.1 / libx265 4.1 十比特构建）：
进程内有过任何 PyAV 媒体探测/解码后，x265 10-bit flush 必发 SIGSEGV
（x265 十比特运动估计读污染参考帧；x264 10-bit、hevc 8-bit、硬件通道
不受影响；pools=none / frame-threads=1 / bframes=0 / ultrafast 均不能
绕过）。防线在 FFmpegVideoEncoder.open 显式拒绝并清理半成品——段错误
击穿整个进程，显式失败优于崩溃。

本文件自身不做任何媒体探测/解码（x265 十比特无前置解码时不崩），
因此 escape-env 用例可以安全走完整 open/close。
"""

from __future__ import annotations

import pytest

from src.shared.export.video_encoder import FFmpegEncoderConfig, FFmpegVideoEncoder
from src.shared.media.metadata import Rational


def _cfg(codec: str, pix_fmt: str) -> FFmpegEncoderConfig:
    # W01 实案：本防线针对**软件 libx265**；hwaccel='auto' 在带 NVENC 的
    # Windows 机器上解析为 hevc_nvenc（防线正确放行硬件通道），测试必须
    # 显式关闭硬件加速才能测到软件拒绝路径。
    return FFmpegEncoderConfig(
        codec=codec, width=320, height=180,
        fps=Rational(25, 1), pix_fmt=pix_fmt, hwaccel="off",
    )


class TestX265TenBitGuard:
    def test_hevc_10bit_software_refused_fail_closed(self, tmp_path, monkeypatch):
        """默认（未设逃逸变量）：拒绝 + 半成品清理 + 可操作错误信息。"""
        monkeypatch.delenv("TOPOS_ALLOW_KNOWN_X265_10BIT_CRASH", raising=False)
        out = tmp_path / "refused.mov"
        enc = FFmpegVideoEncoder(str(out), _cfg("hevc", "yuv420p10le"))
        with pytest.raises(RuntimeError, match="HEVC 10-bit"):
            enc.open()
        assert not out.exists()

    def test_escape_env_allows_open(self, tmp_path, monkeypatch):
        """TOPOS_ALLOW_KNOWN_X265_10BIT_CRASH=1：上游修复验证口径。"""
        monkeypatch.setenv("TOPOS_ALLOW_KNOWN_X265_10BIT_CRASH", "1")
        enc = FFmpegVideoEncoder(
            str(tmp_path / "escape.mov"), _cfg("hevc", "yuv420p10le"))
        enc.open()
        enc.close()  # 本进程无前置 PyAV 解码 → flush 安全

    def test_hevc_8bit_not_guarded(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOPOS_ALLOW_KNOWN_X265_10BIT_CRASH", raising=False)
        enc = FFmpegVideoEncoder(
            str(tmp_path / "hevc8.mov"), _cfg("hevc", "yuv420p"))
        enc.open()
        enc.close()

    def test_h264_10bit_not_guarded(self, tmp_path, monkeypatch):
        """x264 10-bit 实测不受上游缺陷影响（对照矩阵 2026-09-27）。"""
        monkeypatch.delenv("TOPOS_ALLOW_KNOWN_X265_10BIT_CRASH", raising=False)
        enc = FFmpegVideoEncoder(
            str(tmp_path / "h26410.mov"), _cfg("h264", "yuv420p10le"))
        enc.open()
        enc.close()

    def test_p010_input_format_also_guarded(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOPOS_ALLOW_KNOWN_X265_10BIT_CRASH", raising=False)
        out = tmp_path / "p010.mov"
        enc = FFmpegVideoEncoder(str(out), _cfg("hevc", "p010le"))
        with pytest.raises(RuntimeError, match="HEVC 10-bit"):
            enc.open()
        assert not out.exists()
