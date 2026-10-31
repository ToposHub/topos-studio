"""P1-B06 RAISE 复审回归：并行导出成功路径 encoder.close() 失败不得交付。

历史行为：成功态（STATUS_COMPLETE）下 mp4 muxer finalize（encoder.close）
异常被 `except Exception: pass` 吞掉，产物可能截断/损坏却以 success=True
交付（composite_render_output.py 原 1246-1249）。修复后：成功态 close
失败 → 删坏文件 + STATUS_FAILED + error 指明原因；非成功态 close 失败
不改变结果语义（本来就要删文件/报失败）。
"""

from __future__ import annotations

import numpy as np
import pytest


class _FakeEncoder:
    def __init__(self, path=None, config=None):
        self.frames = []
        self.closed = False

    def open(self):
        pass

    def close(self):
        self.closed = True

    def encode_frame(self, frame):
        self.frames.append(frame)


class _CloseBoomEncoder(_FakeEncoder):
    def close(self):
        raise RuntimeError("muxer finalize died")


def _render(monkeypatch, tmp_path, encoder_cls, end_frame=2):
    import src.shared.export.video_encoder as ve
    import src.shared.color.color_transform_provider as ctp
    from src.features.composite.render.composite_render_output import (
        CompositeRenderOutput,
    )

    monkeypatch.setattr(ve, "FFmpegVideoEncoder", encoder_cls)
    monkeypatch.setattr(
        ctp, "transform_color",
        lambda arr, src, dst, config_manager=None: arr)
    out = CompositeRenderOutput()
    monkeypatch.setattr(out, "_resolve_parallel_frames", lambda n: 1)
    frame = np.full((4, 4, 3), 0.5, np.float32)
    monkeypatch.setattr(out, "_render_one_frame", lambda **kw: frame)
    monkeypatch.setattr(out, "_cleanup_frame_renderer", lambda: None)
    return out.render_video(
        graph_data={}, output_path=str(tmp_path / "v.mp4"),
        width=4, height=4, fps=24.0, start_frame=0, end_frame=end_frame,
        is_cancelled=lambda: False)


def test_close_failure_on_success_path_is_failed_not_delivered(monkeypatch, tmp_path):
    result = _render(monkeypatch, tmp_path, _CloseBoomEncoder)
    assert result.success is False
    assert result.status == "failed"
    assert result.output_path == ""  # 不交付路径
    assert "muxer finalize died" in (result.error or "")


def test_close_ok_success_path_unchanged(monkeypatch, tmp_path):
    result = _render(monkeypatch, tmp_path, _FakeEncoder)
    assert result.success is True
    assert result.status == "complete"
    assert result.frames_written == 3


def test_close_failure_on_failed_path_keeps_failure_semantics(monkeypatch, tmp_path):
    # 全部帧渲染失败 → status=failed；此路径 close 失败不改变结果语义
    import src.shared.export.video_encoder as ve
    import src.shared.color.color_transform_provider as ctp
    from src.features.composite.render.composite_render_output import (
        CompositeRenderOutput,
    )

    monkeypatch.setattr(ve, "FFmpegVideoEncoder", _CloseBoomEncoder)
    monkeypatch.setattr(
        ctp, "transform_color",
        lambda arr, src, dst, config_manager=None: arr)
    out = CompositeRenderOutput()
    monkeypatch.setattr(out, "_resolve_parallel_frames", lambda n: 1)
    monkeypatch.setattr(out, "_render_one_frame", lambda **kw: None)
    monkeypatch.setattr(out, "_cleanup_frame_renderer", lambda: None)
    result = out.render_video(
        graph_data={}, output_path=str(tmp_path / "v.mp4"),
        width=4, height=4, fps=24.0, start_frame=0, end_frame=2,
        is_cancelled=lambda: False)
    assert result.success is False
    assert result.status == "failed"  # 修复前：默认 status="complete" 出门
    assert "muxer finalize died" in (result.error or "")
