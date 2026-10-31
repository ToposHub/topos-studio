"""
Final-3 (FINAL-P0-05): GPU pool/compositor 成为唯一 GPU 合成入口。

验收点：
1. TimelinePreviewRenderer.set_gpu_renderer 后，gpu_compositor / gpu_frame_pool
   被实例化并 attach 到 backend。
2. compositor.reset_generation 在 renderer generation bump 时被同步调用。
3. gpu_pipeline_stats 聚合 compositor + frame pool 统计。
4. compositor 包装 backend，submit(TimelineGpuRenderTask) 正确生成 EditRenderTask
   并透传到 backend.submit。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.edit.core.realtime_playback.gpu_frame_pool import GpuFramePool
from src.features.edit.core.realtime_playback.timeline_gpu_compositor import (
    GpuFrameHandle,
    GpuLayerInput,
    TimelineGpuCompositor,
    TimelineGpuRenderTask,
)
from src.features.edit.core.realtime_playback.types import (
    FrameHandle,
    FrameIdentity,
    SourceKind,
)


def _make_frame_handle(source_kind: SourceKind = SourceKind.DISK_PROXY) -> FrameHandle:
    identity = FrameIdentity(
        timeline_id="tl-1",
        clip_id="clip-A",
        asset_id="asset-1",
        source_path="/fake/a.mov",
        source_frame=10,
        source_kind=source_kind,
    )
    return FrameHandle(
        identity=identity,
        frame_index=10,
        width=1920,
        height=1080,
        pixel_format="yuv420p",
        bit_depth=8,
        color_space=None,
        pts_seconds=0.4,
        source_kind=source_kind,
        payload=MagicMock(),
        payload_type="planar_yuv",
    )


# =============================================================================
# 1. set_gpu_renderer attaches compositor + pool
# =============================================================================


def test_set_gpu_renderer_attaches_compositor_and_pool():
    """set_gpu_renderer 后 compositor/pool 都应真实接入。"""
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer

    # 构造一个最小 renderer shell（避免完整 EditProject 构造）。
    class _Shell:
        _generation_id = 0
        _gpu_compositor = None
        _gpu_frame_pool = None

    shell = _Shell()
    shell.set_gpu_renderer = (
        TimelinePreviewRenderer.set_gpu_renderer.__get__(shell, _Shell)
    )
    # 用 fake backend 模拟 EditGpuPreviewRenderer
    fake_backend = MagicMock()
    fake_backend.frame_ready = MagicMock()
    fake_backend.frame_ready.connect = MagicMock()
    fake_backend.present_frame_ready = MagicMock()
    fake_backend.present_frame_ready.connect = MagicMock()
    fake_backend.render_error = MagicMock()
    fake_backend.render_error.connect = MagicMock()
    fake_backend.slot_acked = MagicMock()
    fake_backend.slot_acked.connect = MagicMock()
    fake_decode_mgr = MagicMock()

    # worker 必须非空才能进入 set_gpu_renderer 主路径
    shell._worker = MagicMock()
    shell._realtime_engine = MagicMock()
    # set_gpu_renderer 会 connect 这些 handler，shell 需要可调用属性
    shell._on_gpu_frame_ready = MagicMock()
    shell._on_gpu_snapshot_ready = MagicMock()
    shell._on_present_frame_ready = MagicMock()
    shell._on_present_slot_acked = MagicMock()
    shell._on_gpu_backend_render_error = MagicMock()

    shell.set_gpu_renderer(fake_decode_mgr, fake_backend)

    # pool 必须被实例化
    assert shell._gpu_frame_pool is not None, "frame pool 必须被实例化"
    assert isinstance(shell._gpu_frame_pool, GpuFramePool)
    # compositor 必须真实 attach backend，GPU submit 不再绕过该边界。
    assert isinstance(shell._gpu_compositor, TimelineGpuCompositor)
    assert shell._gpu_compositor._backend is fake_backend
    assert shell._gpu_compositor._connected is True


# =============================================================================
# 2. generation bump 同步到 pool（compositor 不再同步）
# =============================================================================


def test_sync_gpu_pipeline_generation_releases_compositor_and_pool():
    """generation bump 时 compositor 与 pool 必须同步切代。"""
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer

    class _Shell:
        _generation_id = 5
        _gpu_compositor = MagicMock()
        _gpu_frame_pool = MagicMock()

    shell = _Shell()
    shell._sync_gpu_pipeline_generation = (
        TimelinePreviewRenderer._sync_gpu_pipeline_generation.__get__(shell, _Shell)
    )

    shell._sync_gpu_pipeline_generation()

    # 释放的是 generation - 1（旧 generation）
    shell._gpu_frame_pool.release_generation.assert_called_once_with(4)
    shell._gpu_compositor.reset_generation.assert_called_once_with(5)


# =============================================================================
# 3. gpu_pipeline_stats 聚合 compositor + pool
# =============================================================================


def test_gpu_pipeline_stats_aggregates_compositor_and_pool():
    """gpu_pipeline_stats 必须返回 compositor + frame_pool 两部分统计。"""
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer

    class _Shell:
        pass

    shell = _Shell()
    shell._gpu_compositor = MagicMock()
    shell._gpu_compositor.stats.return_value = {"submitted": 3, "cpu_qimage_frames": 0}
    shell._gpu_frame_pool = MagicMock()
    shell._gpu_frame_pool.stats.return_value = {"resident": 5, "uploads": 3}
    shell.gpu_pipeline_stats = (
        TimelinePreviewRenderer.gpu_pipeline_stats.__get__(shell, _Shell)
    )

    stats = shell.gpu_pipeline_stats()
    assert "compositor" in stats
    assert "frame_pool" in stats
    assert stats["compositor"]["submitted"] == 3
    assert stats["frame_pool"]["resident"] == 5


# =============================================================================
# 4. compositor.submit 正确透传 backend
# =============================================================================


def test_compositor_submit_transparent_to_backend():
    """compositor.submit(TimelineGpuRenderTask) 必须调用 backend.submit。"""
    compositor = TimelineGpuCompositor()
    fake_backend = MagicMock()
    fake_backend.submit.return_value = True
    fake_backend.frame_ready = MagicMock()
    fake_backend.frame_ready.connect = MagicMock()
    fake_backend.present_frame_ready = MagicMock()
    fake_backend.present_frame_ready.connect = MagicMock()
    fake_backend.render_error = MagicMock()
    fake_backend.render_error.connect = MagicMock()
    compositor.attach_backend(fake_backend)

    frame = _make_frame_handle()
    layer = GpuLayerInput(
        clip_id="clip-A",
        track_index=0,
        opacity=1.0,
        transform=None,
        crop=None,
        frame=frame,
        blend_mode="normal",
    )
    task = TimelineGpuRenderTask(
        timeline_frame=10,
        generation_id=1,
        output_width=1920,
        output_height=1080,
        layers=(layer,),
        deadline_ns=0,
    )

    ok = compositor.submit(task)
    assert ok is True
    assert fake_backend.submit.call_count == 1
    # compositor 统计应该自增
    assert compositor.stats()["submitted"] == 1
    assert compositor.stats()["gpu_composite_frames"] == 1


def test_compositor_submit_backend_task_preserves_rich_edit_metadata():
    """迁移边界不得丢失特效/调整层/转场字段。"""
    compositor = TimelineGpuCompositor()
    fake_backend = MagicMock()
    fake_backend.submit.return_value = True
    fake_backend.frame_ready = MagicMock()
    fake_backend.frame_ready.connect = MagicMock()
    fake_backend.present_frame_ready = MagicMock()
    fake_backend.present_frame_ready.connect = MagicMock()
    fake_backend.render_error = MagicMock()
    fake_backend.render_error.connect = MagicMock()
    compositor.attach_backend(fake_backend)

    from types import SimpleNamespace

    rich_task = SimpleNamespace(
        frame=27,
        generation_id=4,
        layers=[{"track_info": {"clip_effects": ("blur",)}}],
        adjustment_layers=(("adjustment", ("sharpen",)),),
        transition_jobs=(("motion_blur_left",),),
    )
    assert compositor.submit_backend_task(rich_task) is True
    assert fake_backend.submit.call_args.args[0] is rich_task
    assert fake_backend.submit.call_args.args[0].transition_jobs == (
        ("motion_blur_left",),
    )
    assert compositor.stats()["submitted"] == 1


def test_compositor_submit_rejects_empty_layers():
    """空 layer 必须拒绝 submit，不调用 backend。"""
    compositor = TimelineGpuCompositor()
    fake_backend = MagicMock()
    fake_backend.submit.return_value = True
    fake_backend.frame_ready = MagicMock()
    fake_backend.frame_ready.connect = MagicMock()
    fake_backend.present_frame_ready = MagicMock()
    fake_backend.present_frame_ready.connect = MagicMock()
    fake_backend.render_error = MagicMock()
    fake_backend.render_error.connect = MagicMock()
    compositor.attach_backend(fake_backend)

    task = TimelineGpuRenderTask(
        timeline_frame=10,
        generation_id=1,
        output_width=1920,
        output_height=1080,
        layers=(),
        deadline_ns=0,
    )
    ok = compositor.submit(task)
    assert ok is False
    assert fake_backend.submit.call_count == 0


# =============================================================================
# 5. frame pool cache key 完整性（不同 source_kind 不复用）
# =============================================================================


def test_frame_pool_cache_key_differs_by_source_kind():
    """同 clip/frame 不同 source_kind 必须不复用 texture。"""
    pool = GpuFramePool(max_resident=16)
    upload_calls = []

    def fake_upload(frame):
        upload_calls.append(frame)
        return GpuFrameHandle(
            identity=frame.identity,
            texture_ids=(len(upload_calls),),
            texture_type="rgb",
            width=frame.width,
            height=frame.height,
            bit_depth=frame.bit_depth,
            color_space=frame.color_space,
            generation_id=0,
            release=None,
        )

    pool.set_upload_function(fake_upload)

    frame_proxy = _make_frame_handle(SourceKind.DISK_PROXY)
    frame_master = _make_frame_handle(SourceKind.MASTER)

    h1 = pool.ensure_gpu_frame(frame_proxy, generation_id=1)
    h2 = pool.ensure_gpu_frame(frame_master, generation_id=1)

    # 不同 source_kind 必须分别上传（cache key 不同）
    assert len(upload_calls) == 2, "不同 source_kind 必须不复用 texture"
    assert h1.texture_ids != h2.texture_ids


def test_frame_pool_cache_hit_same_identity():
    """同 identity + generation 的 ensure 应命中缓存（不重新上传）。"""
    pool = GpuFramePool(max_resident=16)
    upload_calls = []

    def fake_upload(frame):
        upload_calls.append(frame)
        return GpuFrameHandle(
            identity=frame.identity,
            texture_ids=(99,),
            texture_type="rgb",
            width=frame.width,
            height=frame.height,
            bit_depth=frame.bit_depth,
            color_space=frame.color_space,
            generation_id=0,
            release=None,
        )

    pool.set_upload_function(fake_upload)

    frame = _make_frame_handle(SourceKind.DISK_PROXY)
    h1 = pool.ensure_gpu_frame(frame, generation_id=1)
    h2 = pool.ensure_gpu_frame(frame, generation_id=1)

    assert len(upload_calls) == 1, "同 identity 应命中缓存"
    assert h1 is h2
    assert pool.stats()["cache_hits"] == 1


def test_frame_pool_release_generation_drops_old():
    """release_generation 必须释放旧 generation 的 resident frame。"""
    pool = GpuFramePool(max_resident=16)
    released = []

    def fake_upload(frame):
        h = GpuFrameHandle(
            identity=frame.identity,
            texture_ids=(1,),
            texture_type="rgb",
            width=frame.width,
            height=frame.height,
            bit_depth=frame.bit_depth,
            color_space=frame.color_space,
            generation_id=0,
            release=lambda: released.append(frame.identity.clip_id),
        )
        return h

    pool.set_upload_function(fake_upload)

    frame = _make_frame_handle(SourceKind.DISK_PROXY)
    pool.ensure_gpu_frame(frame, generation_id=1)
    assert pool.stats()["resident"] == 1

    released_count = pool.release_generation(1)
    assert released_count == 1
    assert pool.stats()["resident"] == 0
    assert released == ["clip-A"]
