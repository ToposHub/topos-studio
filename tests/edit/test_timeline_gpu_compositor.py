"""
Stage F unit tests: TimelineGpuCompositor + GpuFramePool.
对应规格书 §12 / §13 / §20.1 test_timeline_gpu_compositor.py / test_gpu_frame_pool.py。

核心验收：
- Compositor: layer order / generation / 不内部解码 / GPU miss 不 CPU fallback。
- GpuFramePool: texture lifecycle / generation release / capacity GC。
- 关键断言：cpu_qimage_frames == 0, readback_count == 0。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Qt 应用（compositor 是 QObject，需要 QCoreApplication）
from PyQt6.QtCore import QCoreApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    FrameHandle,
    GpuFrameHandle,
    GpuFramePool,
    GpuLayerInput,
    TimelineGpuCompositor,
    TimelineGpuRenderTask,
)
from src.features.edit.core.realtime_playback.types import FrameIdentity, SourceKind


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    return app


def _make_frame_handle(frame_index: int = 0, clip_id: str = "c1") -> FrameHandle:
    return FrameHandle(
        identity=FrameIdentity(
            timeline_id="t", clip_id=clip_id, asset_id="a",
            source_path="/proxy.mov", source_frame=frame_index,
            source_kind=SourceKind.DISK_PROXY,
        ),
        frame_index=frame_index,
        width=1920, height=1080,
        pixel_format="bgr24", bit_depth=8,
        color_space=None, pts_seconds=0.0,
        source_kind=SourceKind.DISK_PROXY,
        payload=MagicMock(),  # GpuDecodedFrame-like
        payload_type="gpu_texture",
    )


# =============================================================================
# TimelineGpuCompositor
# =============================================================================


class _MockBackend:
    """模拟 EditGpuPreviewRenderer。"""
    def __init__(self) -> None:
        self.submitted_tasks = []
        self.frame_ready = MagicMock()
        self.render_error = MagicMock()
        self.submit_return = True

    def submit(self, task) -> bool:
        self.submitted_tasks.append(task)
        return self.submit_return

    def set_generation_id(self, gen: int) -> None:
        self.last_generation = gen


class TestTimelineGpuCompositor:
    def test_submit_requires_backend(self, qapp) -> None:
        c = TimelineGpuCompositor()
        task = TimelineGpuRenderTask(
            timeline_frame=0, generation_id=0,
            output_width=1920, output_height=1080,
            layers=(GpuLayerInput(
                clip_id="c1", track_index=0, opacity=1.0,
                transform=None, crop=None, frame=_make_frame_handle(),
            ),),
        )
        assert c.submit(task) is False

    def test_submit_rejects_empty_layers(self, qapp) -> None:
        c = TimelineGpuCompositor()
        c.attach_backend(_MockBackend())
        task = TimelineGpuRenderTask(
            timeline_frame=0, generation_id=0,
            output_width=1920, output_height=1080, layers=(),
        )
        assert c.submit(task) is False

    def test_submit_delegates_to_backend(self, qapp) -> None:
        c = TimelineGpuCompositor()
        backend = _MockBackend()
        c.attach_backend(backend)
        task = TimelineGpuRenderTask(
            timeline_frame=5, generation_id=1,
            output_width=1920, output_height=1080,
            layers=(GpuLayerInput(
                clip_id="c1", track_index=0, opacity=1.0,
                transform=None, crop=None, frame=_make_frame_handle(),
            ),),
            deadline_ns=123456789,
        )
        assert c.submit(task) is True
        assert len(backend.submitted_tasks) == 1
        bt = backend.submitted_tasks[0]
        assert bt.frame == 5
        assert bt.generation_id == 1
        assert isinstance(bt.layers[0], dict)
        assert bt.layers[0]["frame_type"] == "gpu"
        assert bt.layers[0]["gpu_frame"] is task.layers[0].frame.payload
        assert bt.layers[0]["track_info"]["track_index"] == 0
        assert bt.layers[0]["track_info"]["opacity"] == 1.0
        assert bt.deadline_ns == task.deadline_ns

    def test_gpu_miss_no_cpu_fallback(self, qapp) -> None:
        """GPU miss 时 compositor 不自行 CPU fallback（规格书 §13.3）。"""
        c = TimelineGpuCompositor()
        backend = _MockBackend()
        backend.submit_return = False  # GPU 提交失败
        c.attach_backend(backend)
        task = TimelineGpuRenderTask(
            timeline_frame=0, generation_id=0,
            output_width=1920, output_height=1080,
            layers=(GpuLayerInput(
                clip_id="c1", track_index=0, opacity=1.0,
                transform=None, crop=None, frame=_make_frame_handle(),
            ),),
        )
        result = c.submit(task)
        # 返回 False，不内部触发 CPU fallback
        assert result is False
        s = c.stats()
        assert s["cpu_qimage_frames"] == 0  # 规格 §25 关键断言

    def test_multi_layer_task(self, qapp) -> None:
        c = TimelineGpuCompositor()
        backend = _MockBackend()
        c.attach_backend(backend)
        task = TimelineGpuRenderTask(
            timeline_frame=10, generation_id=2,
            output_width=1920, output_height=1080,
            layers=(
                GpuLayerInput(clip_id="c1", track_index=0, opacity=1.0,
                              transform=None, crop=None, frame=_make_frame_handle(0, "c1")),
                GpuLayerInput(clip_id="c2", track_index=1, opacity=0.8,
                              transform=None, crop=None, frame=_make_frame_handle(0, "c2")),
            ),
        )
        assert c.submit(task) is True
        assert len(backend.submitted_tasks[0].layers) == 2
        layer0, layer1 = backend.submitted_tasks[0].layers
        assert layer0["track_info"]["clip_id"] == "c1"
        assert layer0["track_info"]["track_index"] == 0
        assert layer0["track_info"]["opacity"] == 1.0
        assert layer1["track_info"]["clip_id"] == "c2"
        assert layer1["track_info"]["track_index"] == 1
        assert layer1["track_info"]["opacity"] == 0.8

    def test_backend_layer_preserves_existing_payload_dict(self, qapp) -> None:
        c = TimelineGpuCompositor()
        backend = _MockBackend()
        c.attach_backend(backend)
        frame = _make_frame_handle(0, "c1")
        frame.payload = {
            "gpu_frame": "gpu-frame-object",
            "frame_type": "gpu",
            "track_info": {"opacity": 0.25, "legacy_key": "keep"},
        }
        task = TimelineGpuRenderTask(
            timeline_frame=12, generation_id=3,
            output_width=1920, output_height=1080,
            layers=(GpuLayerInput(
                clip_id="c1", track_index=4, opacity=0.75,
                transform="transform", crop="crop", frame=frame,
            ),),
        )

        assert c.submit(task) is True
        layer = backend.submitted_tasks[0].layers[0]
        assert layer["gpu_frame"] == "gpu-frame-object"
        assert layer["frame_type"] == "gpu"
        assert layer["track_info"]["legacy_key"] == "keep"
        assert layer["track_info"]["track_index"] == 4
        assert layer["track_info"]["opacity"] == 0.75
        assert layer["track_info"]["transform"] == "transform"
        assert layer["track_info"]["crop"] == "crop"

    def test_reset_generation(self, qapp) -> None:
        c = TimelineGpuCompositor()
        backend = _MockBackend()
        c.attach_backend(backend)
        c.reset_generation(42)
        assert backend.last_generation == 42

    def test_frame_ready_forwarded(self, qapp) -> None:
        c = TimelineGpuCompositor()
        backend = _MockBackend()
        c.attach_backend(backend)
        received = []
        c.frame_ready.connect(lambda f, g, t, w, h: received.append((f, g, t, w, h)))
        # 模拟 backend 发射
        backend.frame_ready.emit = lambda *args: c._on_backend_frame_ready(*args)
        c._on_backend_frame_ready(5, 1, 999, 1920, 1080)
        assert received == [(5, 1, 999, 1920, 1080)]

    def test_render_error_forwarded(self, qapp) -> None:
        c = TimelineGpuCompositor()
        c.attach_backend(_MockBackend())
        received = []
        c.render_error.connect(lambda f, m: received.append((f, m)))
        c._on_backend_render_error(5, "boom")
        assert received == [(5, "boom")]

    def test_stats_no_readback_no_qimage(self, qapp) -> None:
        """规格书 §13.3 关键断言：readback_count==0, cpu_qimage_frames==0。"""
        c = TimelineGpuCompositor()
        s = c.stats()
        assert s["readback_count"] == 0
        assert s["cpu_qimage_frames"] == 0

    def test_shutdown_detaches(self, qapp) -> None:
        c = TimelineGpuCompositor()
        c.attach_backend(_MockBackend())
        c.shutdown()
        assert c._backend is None


# =============================================================================
# GpuFramePool
# =============================================================================


class TestGpuFramePool:
    def test_ensure_caches_resident(self, qapp) -> None:
        pool = GpuFramePool()
        # 注入 mock 上传函数
        uploaded = []
        def upload_fn(frame):
            gh = GpuFrameHandle(
                identity=frame.identity, texture_ids=(42,),
                texture_type="rgb", width=1920, height=1080,
                bit_depth=8, color_space=None, generation_id=0, release=None,
            )
            uploaded.append(gh)
            return gh
        pool.set_upload_function(upload_fn)

        frame = _make_frame_handle(10)
        gh1 = pool.ensure_gpu_frame(frame, generation_id=1)
        gh2 = pool.ensure_gpu_frame(frame, generation_id=1)  # cache hit
        assert gh1 is not None
        assert gh2 is gh1
        s = pool.stats()
        assert s["uploads"] == 1
        assert s["cache_hits"] == 1

    def test_different_generation_not_shared(self, qapp) -> None:
        pool = GpuFramePool()
        def upload_fn(frame):
            return GpuFrameHandle(
                identity=frame.identity, texture_ids=(42,),
                texture_type="rgb", width=1920, height=1080,
                bit_depth=8, color_space=None, generation_id=0, release=None,
            )
        pool.set_upload_function(upload_fn)
        frame = _make_frame_handle(5)
        gh1 = pool.ensure_gpu_frame(frame, generation_id=1)
        gh2 = pool.ensure_gpu_frame(frame, generation_id=2)  # 不同 generation
        assert gh1 is not gh2

    def test_release_generation(self, qapp) -> None:
        pool = GpuFramePool()
        released = []
        def upload_fn(frame):
            return GpuFrameHandle(
                identity=frame.identity, texture_ids=(42,),
                texture_type="rgb", width=1920, height=1080,
                bit_depth=8, color_space=None, generation_id=0,
                release=lambda: released.append(1),
            )
        pool.set_upload_function(upload_fn)
        pool.ensure_gpu_frame(_make_frame_handle(1), generation_id=1)
        pool.ensure_gpu_frame(_make_frame_handle(2), generation_id=1)
        pool.ensure_gpu_frame(_make_frame_handle(3), generation_id=2)
        n = pool.release_generation(1)
        assert n == 2
        # gen 2 仍 resident
        s = pool.stats()
        assert s["resident"] == 1

    def test_capacity_gc(self, qapp) -> None:
        pool = GpuFramePool(max_resident=6)
        released = []
        def upload_fn(frame):
            return GpuFrameHandle(
                identity=frame.identity, texture_ids=(42,),
                texture_type="rgb", width=1920, height=1080,
                bit_depth=8, color_space=None, generation_id=0,
                release=lambda: released.append(frame.frame_index),
            )
        pool.set_upload_function(upload_fn)
        for i in range(10):
            pool.ensure_gpu_frame(_make_frame_handle(i), generation_id=1)
        s = pool.stats()
        # 容量 6，淘汰最旧 4 个
        assert s["resident"] == 6
        assert len(released) >= 4
        # 最旧的被释放
        assert 0 in released

    def test_shutdown_releases_all(self, qapp) -> None:
        pool = GpuFramePool()
        released = []
        def upload_fn(frame):
            return GpuFrameHandle(
                identity=frame.identity, texture_ids=(42,),
                texture_type="rgb", width=1920, height=1080,
                bit_depth=8, color_space=None, generation_id=0,
                release=lambda: released.append(1),
            )
        pool.set_upload_function(upload_fn)
        for i in range(5):
            pool.ensure_gpu_frame(_make_frame_handle(i), generation_id=1)
        pool.shutdown()
        assert len(released) == 5

    def test_upload_none_returns_none(self, qapp) -> None:
        pool = GpuFramePool()
        pool.set_upload_function(lambda frame: None)
        gh = pool.ensure_gpu_frame(_make_frame_handle(0), generation_id=1)
        assert gh is None

    def test_external_texture_passthrough(self, qapp) -> None:
        """external texture（macOS VT IOSurface）直接包装，不上传（规格书 §12）。"""
        pool = GpuFramePool()
        frame = _make_frame_handle(0)
        # payload 带 texture_id（external）
        frame.payload.texture_id = 12345
        gh = pool.ensure_gpu_frame(frame, generation_id=1)
        assert gh is not None
        assert gh.texture_type == "external"
        assert gh.texture_ids == (12345,)

    def test_stats_no_decode_thread_gl_calls(self, qapp) -> None:
        """规格书 §25 关键断言：decode_thread_gl_calls == 0。"""
        pool = GpuFramePool()
        s = pool.stats()
        assert s["gl_calls_from_decode_thread"] == 0
