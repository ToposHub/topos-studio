"""
Regression test: GPU renderer must sync IOSurface reads before releasing source frames.
对应画面重叠/撕裂 bug 修复。

验证：
- _release_previous_frames 在 task 使用了 IOSource-backed 纹理时插入 GL sync barrier。
- 未使用 IOSurface 纹理的 task 不阻塞（性能优化）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer, EditRenderTask
from src.shared.gpu.gl_backend import GLContextManager


class _Releasable:
    def __init__(self) -> None:
        self.released = 0

    def release(self) -> None:
        self.released += 1


class TestIOSurfaceSyncBarrier:
    """画面重叠 bug 修复验证。"""

    def test_io_surface_source_release_is_deferred_until_fence_signals(self) -> None:
        """IOSurface 源帧必须等 release fence 完成后才释放，热路径不阻塞。"""
        from OpenGL.GL import GL_ALREADY_SIGNALED, GL_TIMEOUT_EXPIRED

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        source_frame = object()
        renderer._previous_gpu_frames = [source_frame]
        renderer._task_used_io_surface_source = True
        renderer._deferred_gpu_frame_releases = []

        released = []
        renderer._release_gpu_frame_list = lambda frames: released.extend(frames)

        gl_calls = []
        fake_sync_obj = 12345
        with patch("OpenGL.GL.glFlush", lambda: gl_calls.append("flush")):
            with patch("OpenGL.GL.glFenceSync", lambda *a, **k: (gl_calls.append("fence"), fake_sync_obj)[1]):
                with patch("OpenGL.GL.glClientWaitSync", lambda *a, **k: GL_TIMEOUT_EXPIRED):
                    renderer._release_previous_frames()

        assert gl_calls == ["fence", "flush"]
        assert renderer._previous_gpu_frames == []
        assert renderer._deferred_gpu_frame_releases == [(fake_sync_obj, [source_frame])]
        assert released == []
        assert renderer._task_used_io_surface_source is False

        with patch("OpenGL.GL.glClientWaitSync", lambda *a, **k: GL_ALREADY_SIGNALED):
            with patch("OpenGL.GL.glDeleteSync", lambda *a, **k: gl_calls.append("delete")):
                renderer._drain_deferred_gpu_frame_releases(block=False)

        assert released == [source_frame]
        assert renderer._deferred_gpu_frame_releases == []

    def test_sync_skipped_when_no_io_surface_source(self) -> None:
        """未使用 IOSurface 纹理的 task 不应触发 sync（避免不必要的阻塞）。

        验证真实的 _sync_gpu_source_reads 在 flag=False 时早返回，不做 GL 调用。
        """
        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._task_used_io_surface_source = False

        gl_calls = []
        with patch("OpenGL.GL.glFlush", lambda: gl_calls.append("flush")):
            with patch("OpenGL.GL.glFenceSync", lambda *a, **k: gl_calls.append("fence") or 0):
                with patch("OpenGL.GL.glFinish", lambda: gl_calls.append("finish")):
                    renderer._sync_gpu_source_reads()

        # flag=False → 早返回，不调用任何 GL API
        assert gl_calls == [], f"unexpected GL calls when flag=False: {gl_calls}"
        assert renderer._task_used_io_surface_source is False

    def test_sync_invokes_gl_when_io_surface_source_used(self) -> None:
        """flag=True 时必须调用 GL sync API（fence 或 finish）。"""
        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._task_used_io_surface_source = True

        gl_calls = []
        fake_sync_obj = 12345
        with patch("OpenGL.GL.glFlush", lambda: gl_calls.append("flush")):
            with patch("OpenGL.GL.glFenceSync", lambda *a, **k: (gl_calls.append("fence"), fake_sync_obj)[1]):
                with patch("OpenGL.GL.glClientWaitSync", lambda *a, **k: gl_calls.append("wait") or 1):
                    with patch("OpenGL.GL.glDeleteSync", lambda *a, **k: gl_calls.append("delete")):
                        with patch("OpenGL.GL.glFinish", lambda: gl_calls.append("finish")):
                            renderer._sync_gpu_source_reads()

        # The fence must enter the stream before its bounded client wait.
        assert gl_calls[:3] == ["fence", "flush", "wait"]
        assert "delete" in gl_calls
        # flag 被清除
        assert renderer._task_used_io_surface_source is False

    def test_flag_per_task_reset(self) -> None:
        """_process_task 应在每个任务开始时重置标志。"""
        # 验证 _process_task 设置了 _task_used_io_surface_source = False
        # （在循环开始前）。这里通过源码静态检查确认。
        import inspect
        src = inspect.getsource(EditGpuPreviewRenderer._process_task)
        assert "_task_used_io_surface_source = False" in src, (
            "_process_task must reset the IOSurface-source flag per task"
        )

    def test_process_task_finishes_output_before_emit(self) -> None:
        """输出纹理交给 UI context 前必须完成写入，不能只 glFlush。"""
        import inspect
        src = inspect.getsource(EditGpuPreviewRenderer._process_task)
        assert "from OpenGL.GL import glFinish" in src
        assert "glFinish()" in src
        assert "from OpenGL.GL import glFlush" not in src

    def test_submit_drop_oldest_releases_task_sources(self) -> None:
        """GPU 队列满时丢弃旧 task 必须释放其 CVPixelBuffer/GPU frame。"""
        import queue

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._initialized = True
        renderer._queue = queue.Queue(maxsize=1)

        old_resource = _Releasable()
        old_task = EditRenderTask(
            frame=1,
            generation_id=1,
            layers=[{"vt_pixel_buffer": old_resource, "frame_type": "vt_pixel_buffer"}],
        )
        new_task = EditRenderTask(
            frame=2,
            generation_id=1,
            layers=[],
        )
        renderer._queue.put_nowait(old_task)

        assert renderer.submit(new_task) is True
        assert old_resource.released == 1
        assert renderer._queue.qsize() == 1
        assert renderer._queue.get_nowait().frame == 2

    def test_submit_scrub_drops_only_queued_scrub_tasks(self) -> None:
        """快速拖拽时，GPU 队列应丢弃旧 scrub task，但保留普通播放 task。"""
        import queue

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._queue = queue.Queue(maxsize=4)

        released_frames: list[int] = []
        renderer._release_task_sources = lambda task: released_frames.append(task.frame)

        old_scrub = EditRenderTask(
            frame=10,
            generation_id=1,
            layers=[],
            request_kind="scrub",
            scrub_request_token=1,
        )
        playback = EditRenderTask(
            frame=11,
            generation_id=1,
            layers=[],
            request_kind="playback",
        )
        newer_scrub = EditRenderTask(
            frame=12,
            generation_id=1,
            layers=[],
            request_kind="scrub",
            scrub_request_token=2,
        )
        renderer._queue.put_nowait(old_scrub)
        renderer._queue.put_nowait(playback)
        renderer._queue.put_nowait(newer_scrub)

        dropped = renderer._drop_queued_scrub_tasks()

        assert dropped == 2
        assert released_frames == [10, 12]
        assert renderer._queue.qsize() == 1
        kept_task = renderer._queue.get_nowait()
        assert kept_task.frame == 11
        assert kept_task.request_kind == "playback"

    def test_submit_keeps_only_latest_interactive_gpu_task(self) -> None:
        """Queued interactive frames must not consume a stale render slot."""
        import queue

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._initialized = True
        renderer._queue = queue.Queue(maxsize=4)
        released: list[int] = []
        renderer._release_task_sources = lambda task: released.append(task.frame)

        first = EditRenderTask(frame=20, generation_id=1, layers=[])
        latest = EditRenderTask(frame=21, generation_id=1, layers=[])

        assert renderer.submit(first) is True
        assert renderer.submit(latest) is True
        queued = renderer._queue.get_nowait()
        renderer._queue.task_done()

        assert queued.frame == 21
        assert released == [20]
        renderer._queue.join()

    def test_generation_reset_purges_queued_stale_gpu_tasks(self) -> None:
        """Seek generation changes must release queued work from the old timeline."""
        import queue

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._queue = queue.Queue(maxsize=4)
        released: list[int] = []
        renderer._release_task_sources = lambda task: released.append(task.frame)

        old = EditRenderTask(frame=50, generation_id=4, layers=[])
        current = EditRenderTask(frame=51, generation_id=5, layers=[])
        renderer._queue.put_nowait(old)
        renderer._queue.put_nowait(current)

        renderer.set_generation_id(5)

        queued = renderer._queue.get_nowait()
        renderer._queue.task_done()
        assert queued.frame == 51
        assert released == [50]
        renderer._queue.join()

    def test_submit_rejects_stale_generation_without_releasing_incoming_task(self) -> None:
        """Rejected stale input remains owned by the caller for one release only."""
        import queue

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._initialized = True
        renderer._queue = queue.Queue(maxsize=2)
        renderer.set_generation_id(8)

        stale = EditRenderTask(frame=60, generation_id=7, layers=[])

        assert renderer.submit(stale) is False
        assert renderer._queue.empty()

    def test_submit_rejects_expired_deadline_without_releasing_incoming_task(self) -> None:
        """A GPU task that already missed its present tick must be dropped at submit."""
        import queue

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._initialized = True
        renderer._queue = queue.Queue(maxsize=2)
        expired = EditRenderTask(
            frame=65,
            generation_id=0,
            layers=[],
            deadline_ns=time.time_ns() - 1,
        )

        assert renderer.submit(expired) is False
        assert renderer._queue.empty()
        assert renderer.deadline_drop_count == 1

    def test_submit_generation_advance_purges_previous_queue(self) -> None:
        """A direct backend submit can establish a newer generation safely."""
        import queue

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._initialized = True
        renderer._queue = queue.Queue(maxsize=4)
        released: list[int] = []
        renderer._release_task_sources = lambda task: released.append(task.frame)

        renderer._queue.put_nowait(EditRenderTask(frame=70, generation_id=1, layers=[]))
        newer = EditRenderTask(frame=71, generation_id=2, layers=[])

        assert renderer.submit(newer) is True
        queued = renderer._queue.get_nowait()
        renderer._queue.task_done()
        assert queued.frame == 71
        assert released == [70]
        assert renderer._generation_id == 2
        renderer._queue.join()

    def test_background_gpu_task_cannot_evict_interactive_work(self) -> None:
        """A full queue of interactive work rejects background cache fill."""
        import queue

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._initialized = True
        renderer._queue = queue.Queue(maxsize=4)
        released: list[int] = []
        renderer._release_task_sources = lambda task: released.append(task.frame)

        interactive = [
            EditRenderTask(frame=30 + i, generation_id=1, layers=[])
            for i in range(4)
        ]
        for task in interactive:
            renderer._queue.put_nowait(task)
        background = EditRenderTask(
            frame=40, generation_id=1, layers=[], background=True,
            request_kind="idle_prebuffer",
        )

        assert renderer.submit(background) is False
        assert released == []
        queued_frames = []
        while True:
            try:
                task = renderer._queue.get_nowait()
            except queue.Empty:
                break
            queued_frames.append(task.frame)
            renderer._queue.task_done()
        assert queued_frames == [30, 31, 32, 33]
        renderer._queue.join()

    def test_edit_gpu_task_uses_shared_gpu_lane_and_releases_on_error(self) -> None:
        """Edit presenter must share the producer lane, including failures."""

        class _RecordingMutex:
            def __init__(self) -> None:
                self.locked = False
                self.events: list[str] = []

            def lock(self) -> None:
                assert not self.locked
                self.locked = True
                self.events.append("lock")

            def unlock(self) -> None:
                assert self.locked
                self.locked = False
                self.events.append("unlock")

        mutex = _RecordingMutex()
        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._drain_deferred_gpu_frame_releases = lambda block: None
        task = EditRenderTask(frame=3, generation_id=1, layers=[])
        observed = []

        def fail_while_locked(_task) -> None:
            observed.append(mutex.locked)
            raise RuntimeError("synthetic gpu failure")

        renderer._process_task = fail_while_locked
        with patch.object(
            GLContextManager,
            "instance",
            return_value=SimpleNamespace(mutex=mutex),
        ):
            with pytest.raises(RuntimeError, match="synthetic gpu failure"):
                renderer._process_task_with_gpu_lane(task)

        assert observed == [True]
        assert mutex.events == ["lock", "unlock"]
        assert mutex.locked is False
