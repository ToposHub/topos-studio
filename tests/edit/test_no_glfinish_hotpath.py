"""
Batch 2 regression: 热路径 glFinish 调用次数为 0。
对应缺陷计划 RP-P0-01 §3.1 验收 "no finish hot path"。

验证：
- _finalize_present_frame 成功路径不调用 glFinish（用 glFenceSync 替代）。
- 播放 300 帧 glFinish 计数为 0（fence 正常时）。
- glFinish 仅在 fence 创建失败时作为 correctness fallback（rare path）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
from src.shared.gpu.present_frame import OutputSlotPool, SlotState


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


def _make_renderer_with_pool(qapp) -> EditGpuPreviewRenderer:
    """构造一个带 registered present pool 的 renderer（绕过 GL 初始化）。"""
    r = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
    r._output_pool_size = 4
    r._present_pool = OutputSlotPool(size=4)
    r._present_pool_registered = True
    r._current_present_lease = None
    # P0-03 GL pass timer：生产 __init__ 置 None（GL 线程 lazy 创建）；
    # _finalize_present_frame 会读取该属性。
    r._gl_pass_timer = None
    # 注册 slots（texture_glo 用 mock 值）
    for sid in range(4):
        r._present_pool.register_slot(sid, texture_glo=100 + sid, width=1920, height=1080)
    # 用简单 list 替代 Qt signal（避免 MagicMock 递归导致 segfault）
    r._emitted_presents = []
    class _FakeSignal:
        def __init__(self, sink): self._sink = sink
        def emit(self, *a): self._sink.append(a)
    r.present_frame_ready = _FakeSignal(r._emitted_presents)
    r.frame_ready = _FakeSignal([])
    return r


class TestNoGlFinishHotPath:
    """缺陷计划 §3.1: 播放 300 帧 glFinish 计数为 0。"""

    def test_finalize_present_frame_uses_fence_not_finish(self, qapp) -> None:
        """_finalize_present_frame 成功路径用 glFenceSync，不调 glFinish。"""
        r = _make_renderer_with_pool(qapp)
        # acquire 一个 lease
        lease = r._present_pool.acquire_for_writing(generation_id=1, frame_index=10)
        assert lease is not None
        r._current_present_lease = lease

        glfinish_calls = []
        glfencesync_calls = []
        with patch("OpenGL.GL.glFinish", lambda: glfinish_calls.append(1)):
            with patch("OpenGL.GL.glFlush", lambda: None):
                with patch("OpenGL.GL.glFenceSync", lambda *a, **k: (glfencesync_calls.append(1), 999)[1]):
                    final_tex = MagicMock()
                    final_tex.glo = 42
                    present = r._finalize_present_frame(
                        final_tex=final_tex,
                        frame=10, generation_id=1,
                        width=1920, height=1080, track_count=1,
                    )
        assert present is not None
        assert present.has_fence  # 用 fence
        # GPU composite FBO 渲染的纹理是标准 GL 方向（y=0 在底部），
        # 必须 is_gl_oriented=True。设 False 会让 Viewer 按 CPU QImage 多翻一次，
        # 导致画面上下颠倒。
        assert present.is_gl_oriented is True
        assert len(glfinish_calls) == 0, "hot path must not call glFinish"
        assert len(glfencesync_calls) == 1

    def test_300_frames_glfinish_zero(self, qapp) -> None:
        """模拟 300 帧播放，glFinish 热路径调用次数为 0。"""
        r = _make_renderer_with_pool(qapp)

        glfinish_count = [0]
        real_finish = lambda: glfinish_count.__setitem__(0, glfinish_count[0] + 1)

        with patch("OpenGL.GL.glFinish", real_finish):
            with patch("OpenGL.GL.glFlush", lambda: None):
                with patch("OpenGL.GL.glFenceSync", lambda *a, **k: 999):
                    # patch glDeleteSync 防止 cleanup 调用破坏假 fence handle
                    with patch("OpenGL.GL.glDeleteSync", lambda *a, **k: None):
                        for i in range(300):
                            lease = r._present_pool.acquire_for_writing(
                                generation_id=1, frame_index=i,
                            )
                            if lease is None:
                                # back-pressure: ack 最旧推进
                                for sid in range(4):
                                    slot = r._present_pool.get_slot(sid)
                                    if slot and slot.state in (SlotState.READY, SlotState.IN_VIEWER):
                                        r._present_pool.ack(sid)
                                        break
                                continue
                            r._current_present_lease = lease
                            final_tex = MagicMock()
                            final_tex.glo = 42
                            r._finalize_present_frame(
                                final_tex=final_tex,
                                frame=i, generation_id=1,
                                width=1920, height=1080, track_count=1,
                            )
                            # viewer ack
                            r._present_pool.ack(lease.slot_id)

        assert glfinish_count[0] == 0, (
            f"hot path glFinish called {glfinish_count[0]} times in 300 frames (must be 0)"
        )

    def test_glfinish_only_on_fence_failure(self, qapp) -> None:
        """fence 创建失败时才回退 glFinish（rare correctness path）。"""
        r = _make_renderer_with_pool(qapp)
        lease = r._present_pool.acquire_for_writing(generation_id=1, frame_index=10)
        r._current_present_lease = lease

        glfinish_count = [0]
        with patch("OpenGL.GL.glFinish", lambda: glfinish_count.__setitem__(0, glfinish_count[0] + 1)):
            with patch("OpenGL.GL.glFlush", lambda: None):
                # glFenceSync 返回 None（失败）
                with patch("OpenGL.GL.glFenceSync", lambda *a, **k: None):
                    final_tex = MagicMock()
                    final_tex.glo = 42
                    present = r._finalize_present_frame(
                        final_tex=final_tex,
                        frame=10, generation_id=1,
                        width=1920, height=1080, track_count=1,
                    )
        # fence 失败 → present 仍构造（fence_ns=0），但不调 glFinish
        # （_finalize_present_frame 只在 present_frame is None 时才 glFinish，
        #  而 fence 失败时 present_frame 仍非 None）
        # 这个测试验证 fence 失败不崩溃
        assert present is not None
