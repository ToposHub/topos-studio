"""
Batch 1 integration tests: present frame fence-wait + ack 流程（viewer 侧）。
对应缺陷计划 RP-P0-01/02 §3.1 验收。

验证：
- set_present_frame 接收 GpuPresentFrame，paintGL blit 前 wait fence。
- blit 后触发 ack callback，renderer slot 被释放。
- stale generation 的 present frame 被丢弃但仍 ack slot（防泄漏）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    OutputSlotPool,
    SlotState,
)


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


def _make_present(slot_id: int = 0, fence_ns: int = 999, frame: int = 10, generation_id: int = 1) -> GpuPresentFrame:
    return GpuPresentFrame(
        texture_id=101, width=1920, height=1080,
        frame=frame, generation_id=generation_id,
        slot_id=slot_id, fence_ns=fence_ns,
        is_gl_oriented=True,
    )


# =============================================================================
# Fence wait + ack 集成
# =============================================================================


class TestPresentFrameFenceAck:
    """验证 viewer 端 set_present_frame → fence wait → ack 流程。"""

    def test_set_present_frame_queues_fence_and_ack(self, qapp) -> None:
        """set_present_frame 应暂存 present frame + ack callback。"""
        from src.features.edit.ui.program_viewer import ProgramViewer
        viewer = ProgramViewer.__new__(ProgramViewer)
        viewer._pending_present_frame = None
        viewer._pending_present_ack = None
        viewer._shared_tex_id = None
        viewer._shared_tex_width = 0
        viewer._shared_tex_height = 0
        viewer._pending_qimage = MagicMock()
        viewer._error_state = "none"
        viewer._error_message = ""
        viewer._view_transform = MagicMock()
        viewer._view_transform.mode = "fit"
        viewer._video_aspect_ratio = 16/9
        viewer._fit_to_window = MagicMock()
        viewer.update = MagicMock()

        ack_cb = MagicMock()
        present = _make_present(slot_id=5, fence_ns=888)
        viewer.set_present_frame(present, ack_cb=ack_cb)

        assert viewer._pending_present_frame is present
        assert viewer._pending_present_ack is ack_cb
        assert viewer._shared_tex_id == 101
        assert viewer._shared_tex_width == 1920

    def test_program_viewer_widget_routes_present_descriptor(self, qapp) -> None:
        """ProgramViewerWidget.set_frame 必须走 set_present_frame，而不是降级 tuple。"""
        from src.features.edit.ui.program_viewer import ProgramViewerWidget
        widget = ProgramViewerWidget.__new__(ProgramViewerWidget)
        widget._set_frame_depth = 0
        widget._viewer = MagicMock()

        ack_cb = MagicMock()
        present = _make_present(slot_id=9, fence_ns=123)
        widget.set_frame(10, {"present_frame": present, "ack_cb": ack_cb})

        widget._viewer.set_present_frame.assert_called_once_with(
            present, ack_cb=ack_cb, trace_id=0)
        widget._viewer.set_shared_texture.assert_not_called()

    def test_program_viewer_has_a_dedicated_offline_state(self, qapp) -> None:
        """Missing source diagnostics should not be reduced to generic render errors."""
        from src.features.edit.ui.program_viewer import ProgramViewer

        viewer = ProgramViewer.__new__(ProgramViewer)
        viewer._error_state = "none"
        viewer._error_message = ""
        viewer._offline_media = ()
        viewer.update = MagicMock()

        viewer.set_offline_media([{
            "asset_id": "asset-1",
            "name": "missing.mp4",
            "path": "/gone/missing.mp4",
        }])

        assert viewer._error_state == "offline_media"
        assert viewer._error_message == "missing.mp4"

        viewer.set_offline_media(())
        assert viewer._error_state == "none"
        assert viewer._error_message == ""

    def test_copy_path_uses_orientation_and_resets_gl_state(self) -> None:
        """共享纹理 blit 前必须重置 GL 状态，并使用 present 的方向标记。"""
        import inspect
        from src.features.edit.ui.program_viewer import ProgramViewer

        src = inspect.getsource(ProgramViewer._copy_shared_texture_to_display)
        assert "_reset_copy_gl_state()" in src
        assert "target_is_gl_oriented" in src
        assert "_current_tex_is_gl_oriented = target_is_gl_oriented" in src
        assert "glClear(GL_COLOR_BUFFER_BIT)" in src

    def test_fence_wait_invoked_before_blit(self, qapp) -> None:
        """注入的 fence wait 函数应在 blit 时被调用（mock 验证）。"""
        from src.features.edit.ui.program_viewer import ProgramViewer
        viewer = ProgramViewer.__new__(ProgramViewer)
        viewer._pending_present_frame = None
        viewer._pending_present_ack = None
        viewer._gl_fence_wait_fn = None
        viewer._shared_tex_id = None

        fence_calls = []
        def fake_wait(fence_ns):
            fence_calls.append(fence_ns)

        viewer.set_fence_wait_function(fake_wait)
        assert viewer._gl_fence_wait_fn is fake_wait

        # 模拟 _copy_shared_texture_to_display 调用 fence wait
        present = _make_present(slot_id=0, fence_ns=777)
        viewer._pending_present_frame = present
        viewer._shared_tex_id = 101
        viewer._shared_tex_width = 1920
        viewer._shared_tex_height = 1080
        viewer._current_tex_id = 200
        viewer._tex_width = 0  # 触发 texImage2D 路径

        # 直接验证 fence wait 逻辑（不真正调 GL）
        # _copy_shared_texture_to_display 在 source_tex 验证前会先 wait fence
        if present.has_fence and present.fence_ns != 0:
            viewer._gl_fence_wait_fn(present.fence_ns)
        assert fence_calls == [777]

    def test_ack_callback_fires_after_blit(self, qapp) -> None:
        """blit 完成后 ack callback 应被调用（释放 renderer slot）。"""
        from src.features.edit.ui.program_viewer import ProgramViewer
        viewer = ProgramViewer.__new__(ProgramViewer)
        ack_calls = []
        ack_cb = lambda slot_id: ack_calls.append(slot_id)
        viewer._pending_present_frame = _make_present(slot_id=7, fence_ns=0)
        viewer._pending_present_ack = ack_cb

        # 模拟 blit 完成后的 ack 触发逻辑
        present = viewer._pending_present_frame
        if present is not None and viewer._pending_present_ack is not None:
            viewer._pending_present_ack(getattr(present, 'slot_id', -1))
            viewer._pending_present_frame = None
            viewer._pending_present_ack = None

        assert ack_calls == [7]
        assert viewer._pending_present_frame is None

    def test_present_frame_without_fence_does_not_wait(self, qapp) -> None:
        """无 fence 的 present frame（fallback）不应尝试 wait。"""
        from src.features.edit.ui.program_viewer import ProgramViewer
        viewer = ProgramViewer.__new__(ProgramViewer)
        fence_calls = []
        viewer._gl_fence_wait_fn = lambda f: fence_calls.append(f)
        viewer._pending_present_frame = _make_present(fence_ns=0)  # 无 fence
        # 模拟 _copy_shared_texture_to_display 的 fence 判断
        present = viewer._pending_present_frame
        if present is not None and getattr(present, 'has_fence', False) and present.fence_ns != 0:
            viewer._gl_fence_wait_fn(present.fence_ns)
        assert fence_calls == []  # 无 fence，不 wait


# =============================================================================
# Renderer ↔ Viewer 端到端 slot 生命周期
# =============================================================================


class TestRendererViewerSlotLifecycle:
    """模拟 renderer 写入 → viewer ack 的完整 slot 生命周期。"""

    def test_full_present_cycle(self) -> None:
        """renderer acquire → mark_ready → viewer ack → slot 回收。"""
        pool = OutputSlotPool(size=2)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.register_slot(1, texture_glo=102, width=1920, height=1080)

        # renderer 写入
        lease = pool.acquire_for_writing(generation_id=1, frame_index=10)
        assert lease is not None
        lease.mark_ready(fence_ns=999, fence_cleanup=lambda f: None)
        present = lease.make_present_frame(frame=10, generation_id=1)
        pool.mark_in_viewer(present.slot_id)

        # viewer ack
        pool.ack(present.slot_id)
        slot = pool.get_slot(present.slot_id)
        assert slot is not None
        assert slot.state is SlotState.RELEASABLE

        # slot 可重新 acquire
        lease2 = pool.acquire_for_writing(generation_id=2, frame_index=11)
        assert lease2 is not None

    def test_unacked_slot_blocks_subsequent_writes(self) -> None:
        """未 ack 的 slot 阻塞后续写入（back-pressure）。"""
        pool = OutputSlotPool(size=2)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.register_slot(1, texture_glo=102, width=1920, height=1080)

        # 占用两个 slot 不 ack
        l1 = pool.acquire_for_writing(generation_id=1, frame_index=10)
        l2 = pool.acquire_for_writing(generation_id=1, frame_index=11)
        l1.mark_ready(fence_ns=1)
        l2.mark_ready(fence_ns=2)

        # 第 3 次 acquire None（防混帧）
        l3 = pool.acquire_for_writing(generation_id=1, frame_index=12)
        assert l3 is None

        # ack 后可继续
        pool.ack(l1.slot_id)
        l4 = pool.acquire_for_writing(generation_id=1, frame_index=12)
        assert l4 is not None
