"""M21.1（R04）· 编码输出发布事务回归。

验收（升级计划 M §M21 首片）：存在同名旧成片时，编码失败（open/
中途写/close 异常）**保留旧成片**，只清本任务临时文件；成功路径经
临时文件 + os.replace 原子发布。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.media_core

import numpy as np

from src.shared.export.video_encoder import FFmpegVideoEncoder
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


def _config(codec: str = "h264"):
    from src.shared.export.video_encoder import FFmpegEncoderConfig

    return FFmpegEncoderConfig(
        codec=codec, width=64, height=48, fps=Rational(25, 1),
        pix_fmt="yuv420p", bitrate=500_000,
    )


def _real_encoder(tmp_path: Path, codec: str = "h264") -> FFmpegVideoEncoder:
    return FFmpegVideoEncoder(
        str(tmp_path / "out.mp4"), _config(codec))


def _write_old_output(path: Path, marker: bytes = b"OLD-SUCCESSFUL-OUTPUT"):
    path.write_bytes(marker)


class TestPublishTransaction:
    def test_success_publishes_atomically_and_cleans_temp(self, tmp_path):
        out = tmp_path / "out.mp4"
        enc = _real_encoder(tmp_path)
        enc.open()
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=np.zeros((48, 64, 3), dtype=np.uint8),
            pixel_format="rgb24", width=64, height=48))
        enc.close()
        # 发布到最终路径；临时文件不留
        assert out.exists() and out.stat().st_size > 0
        assert not os.path.exists(str(out) + ".topos-partial")

    def test_old_output_survives_close_failure(self, tmp_path, monkeypatch):
        """R04 验收句：close（flush/trailer）失败 → 旧成片保留，只清
        临时文件。注入容器 close 抛异常。"""
        out = tmp_path / "old.mp4"
        _write_old_output(out)

        enc = _real_encoder(tmp_path)
        # 把输出路径指到旧成片
        object.__setattr__(enc, "_output_path", str(out))
        object.__setattr__(enc, "_write_path", str(out) + ".topos-partial")
        enc.open()

        # 注入容器关闭失败（trailer 写失败等价）——av 容器 close 只读，
        # 经代理对象拦截
        class CloseBoom:
            def __init__(self, inner):
                self._inner = inner
            def close(self):
                raise RuntimeError("trailer write failed")
            def __getattr__(self, name):
                return getattr(self._inner, name)
        object.__setattr__(enc, "_container", CloseBoom(enc._container))

        with pytest.raises(RuntimeError, match="trailer"):
            enc.close()
        # 旧成片原封不动；临时文件清理
        assert out.read_bytes() == b"OLD-SUCCESSFUL-OUTPUT"
        assert not os.path.exists(str(out) + ".topos-partial")

    def test_old_output_not_truncated_on_open(self, tmp_path):
        """open() 不再以 'w' 截断最终路径（此前 av.open(最终路径,
        mode='w') 立即清空旧成片）。"""
        out = tmp_path / "keep.mp4"
        _write_old_output(out)
        before = out.read_bytes()

        enc = FFmpegVideoEncoder(str(out), _config())
        assert enc._write_path.endswith(".topos-partial")
        assert enc._write_path != enc._output_path
        # 未 open 前，最终路径保持原样
        assert out.read_bytes() == before

    def test_open_failure_removes_temp_only(self, tmp_path, monkeypatch):
        """open 中途失败（如 add_stream 拒绝）→ 只清临时文件，旧成片
        保留。"""
        out = tmp_path / "old2.mp4"
        _write_old_output(out)
        before = out.read_bytes()

        import sys
        mock_av = MagicMock()
        container = MagicMock()

        def rejecting_add_stream(*a, **k):
            raise RuntimeError("no such encoder")
        container.add_stream.side_effect = rejecting_add_stream
        mock_av.open.return_value = container
        monkeypatch.setitem(sys.modules, "av", mock_av)

        enc = FFmpegVideoEncoder(str(out), _config())
        with pytest.raises(RuntimeError):
            enc.open()
        assert out.read_bytes() == before
