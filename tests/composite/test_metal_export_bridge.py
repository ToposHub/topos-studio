"""阶段 5.6 · 输出与资源池测试（导出桥 / 传输策略 / 纹理形态 / 异步发布）。

分层：

- 纯逻辑层（无 GPU）：AOV 显式回退、开关门控、传输策略表、纹理池
  多形态字节计量与复用、异步发布状态机（注入 fake 调度器驱动）。
- 真机层（无 Metal 设备自动 skip）：导出桥多 pass/序列/取消、
  与 ``render_cpu``（GL 无头路径）同帧对拍的服务级一致性、多帧压测后
  GPU 句柄与池回到空闲上限。

对拍容差依据：Metal 与 GL 两端内部均为 RGBA16F（半浮点，10 位尾数，
1 ulp @1.0 ≈ 2^-11 ≈ 4.9e-4）；两端采样/混合公式逐条对齐（含 5.6 修正
的 constant 像素中心采样）。实测 Metal↔GL 逐像素最大差 ≈ 4.9e-4（恰
1 ulp），取 atol=2e-3（4 倍裕量）。对拍图有意避开两类已知跨端差异：
Transform 的 GL bbox 跟随语义（文档化）与 crop-reformat 的输出画布
语义（本次发现，见架构文档 8.5）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.shared.gpu.composite_gpu_frame_handle import (
    CompositeGpuFrameHandle,
    make_unavailable_handle,
)
from src.shared.gpu.metal import MetalComputeContext

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备")

GPU_VS_GL_TOL = 2e-3


# ── 图构造辅助（与 test_metal_composite_chain 同式） ────────────────────


def constant(nid: str, w: int, h: int, rgba: tuple, **kw) -> dict:
    params = {"width": w, "height": h, "mode": "solid",
              "color": rgba, "color2": (1.0, 1.0, 1.0, 1.0), "checker_size": 4}
    params.update(kw)
    return {"id": nid, "type": "constant", "parameters": params}


def node(nid: str, ntype: str, **params) -> dict:
    return {"id": nid, "type": ntype, "parameters": dict(params)}


def conn(src: str, dst: str, src_port: str = "result",
         dst_port: str = "image") -> dict:
    return {"src_node_id": src, "src_port_name": src_port,
            "dst_node_id": dst, "dst_port_name": dst_port}


def graph(nodes: list, connections: list, revision: int = 5) -> dict:
    return {"nodes": nodes, "connections": connections,
            "graph_revision": revision, "composite_id": "test-comp",
            "fps": 25, "duration_frames": 16}


def checker_transform_graph(w: int = 48, h: int = 32) -> dict:
    """对拍用图（Metal↔GL 语义逐像素一致的部分集）。

    故意不含 Transform（GL 的 bbox 跟随语义是文档化已知差异）与
    crop-reformat（两端输出画布语义不同，见架构文档 8.5「已知差异」）；
    也不含 checker（GL 路径的 checker 上传存在既有垂直相位问题——
    shader 路径显式翻 Y 而 numpy 上传路径没翻，金属侧与
    ``ConstantNode._make_checker`` 一致）。
    """
    return graph(
        [constant("fg", w, h, (1.0, 0.0, 0.0, 1.0), mode="gradient",
                  color2=(0.0, 1.0, 0.5, 1.0),
                  start_x=0.0, start_y=0.0, end_x=1.0, end_y=1.0),
         node("u", "unpremult"), node("p", "premult"),
         constant("bg", w, h, (0.1, 0.2, 0.3, 1.0)),
         node("m", "merge", blend_mode="over", opacity=0.9),
         node("o", "output")],
        [conn("fg", "u"), conn("u", "p"),
         conn("p", "m", dst_port="A"), conn("bg", "m", dst_port="B"),
         conn("m", "o")])


# ── AOV 显式回退 ────────────────────────────────────────────────────────


class TestAovExplicitFallback:
    def test_aov_only_request_falls_back_without_gpu_work(self):
        from src.features.composite.render.metal.export_bridge import (
            MetalCompositeExportBridge,
        )
        bridge = MetalCompositeExportBridge()
        try:
            result = bridge.render_passes(
                checker_transform_graph(), passes=("depth", "object_id"))
            assert not result.ok
            assert set(result.fallbacks) == {"depth", "object_id"}
            for reason in result.fallbacks.values():
                assert "无 Metal 产出" in reason
                assert "回退 GL" in reason
        finally:
            bridge.cleanup()

    def test_mixed_request_renders_beauty_and_falls_back_aov(self):
        if not MetalComputeContext.is_supported():
            pytest.skip("无 Metal 设备")
        from src.features.composite.render.metal.export_bridge import (
            MetalCompositeExportBridge,
        )
        bridge = MetalCompositeExportBridge()
        try:
            result = bridge.render_passes(
                checker_transform_graph(), timeline_frame=2,
                passes=("beauty", "motion"))
            assert "beauty" in result.passes and result.passes["beauty"].shape \
                == (32, 48, 4)
            assert "motion" in result.fallbacks
        finally:
            bridge.cleanup()


# ── 开关门控（默认行为不变） ────────────────────────────────────────────


class TestBridgeGating:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("TOPOS_COMPOSITE_METAL", raising=False)
        from src.features.composite.render.metal.export_bridge import (
            MetalCompositeExportBridge,
        )
        bridge = MetalCompositeExportBridge()
        assert bridge.available is False
        assert "未启用" in bridge.unavailable_reason
        bridge.cleanup()

    def test_kill_switch_disables(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COMPOSITE_METAL", "1")
        monkeypatch.setenv("TOPOS_METAL_DISABLE", "composite_metal")
        from src.features.composite.render.metal.export_bridge import (
            MetalCompositeExportBridge,
        )
        bridge = MetalCompositeExportBridge()
        assert bridge.available is False
        bridge.cleanup()

    def test_service_render_metal_async_disabled(self, monkeypatch):
        monkeypatch.delenv("TOPOS_COMPOSITE_METAL", raising=False)
        from src.features.composite.render.composite_graph_render_service import (
            CompositeGraphRenderService,
        )
        service = CompositeGraphRenderService()
        assert service.render_metal_async(checker_transform_graph()) is None
        assert service.poll_metal_completions() == ()
        service.cleanup()


# ── 传输策略 ────────────────────────────────────────────────────────────


class TestTransferPolicy:
    def _handle(self, **kw) -> CompositeGpuFrameHandle:
        base = dict(frame=3, generation=7, texture_id=11, width=4, height=4,
                    backend="metal", device_identity="metal:AMD:1a",
                    available=True)
        base.update(kw)
        return CompositeGpuFrameHandle(**base)

    def test_unavailable_refused(self):
        from src.features.composite.render.metal.transfer_policy import (
            TransferAction, decide_metal_transfer,
        )
        handle = make_unavailable_handle("x")
        assert decide_metal_transfer(handle) is TransferAction.REFUSE

    def test_same_device_and_generation_present(self):
        from src.features.composite.render.metal.transfer_policy import (
            TransferAction, decide_metal_transfer,
        )
        action = decide_metal_transfer(
            self._handle(), consumer_backend="metal",
            consumer_device_identity="metal:AMD:1a", consumer_generation=7)
        assert action is TransferAction.PRESENT

    def test_stale_generation_dropped_before_device_check(self):
        from src.features.composite.render.metal.transfer_policy import (
            TransferAction, decide_metal_transfer,
        )
        # 代际过期优先于设备/后端判定：过期帧不搬运。
        action = decide_metal_transfer(
            self._handle(), consumer_backend="opengl",
            consumer_device_identity="other", consumer_generation=8)
        assert action is TransferAction.DROP_STALE

    def test_backend_mismatch_readback(self):
        from src.features.composite.render.metal.transfer_policy import (
            TransferAction, decide_metal_transfer,
        )
        action = decide_metal_transfer(
            self._handle(), consumer_backend="opengl",
            consumer_device_identity="", consumer_generation=7)
        assert action is TransferAction.READBACK_TRANSFER

    def test_device_mismatch_readback(self):
        from src.features.composite.render.metal.transfer_policy import (
            TransferAction, decide_metal_transfer,
        )
        action = decide_metal_transfer(
            self._handle(), consumer_backend="metal",
            consumer_device_identity="metal:AMD:2b", consumer_generation=7)
        assert action is TransferAction.READBACK_TRANSFER


# ── 纹理形态（5.6 形态盘点） ────────────────────────────────────────────


class _FakeContext:
    def create_texture_2d(self, pixel_format, width, height, shader_write=False):
        return (pixel_format, int(width), int(height), bool(shader_write))


class TestTexturePoolShapes:
    def test_default_shape_rgba16f(self):
        from src.features.composite.render.metal.texture_pool import (
            RGBA16F_BYTES_PER_PIXEL, MetalTexturePool,
        )
        pool = MetalTexturePool(_FakeContext())
        entry = pool.acquire(8, 4)
        assert entry.bytes_size == 8 * 4 * RGBA16F_BYTES_PER_PIXEL

    def test_r32f_and_rgba32f_accounting(self):
        import Metal
        from src.features.composite.render.metal.texture_pool import (
            MetalTexturePool,
        )
        pool = MetalTexturePool(_FakeContext())
        depth = pool.acquire(8, 4, pixel_format=Metal.MTLPixelFormatR32Float)
        wide = pool.acquire(8, 4, pixel_format=Metal.MTLPixelFormatRGBA32Float)
        assert depth.bytes_size == 8 * 4 * 4     # R32F = 4 B/px
        assert wide.bytes_size == 8 * 4 * 16    # RGBA32F = 16 B/px
        assert pool.bytes_allocated == depth.bytes_size + wide.bytes_size

    def test_same_size_different_format_not_shared(self):
        import Metal
        from src.features.composite.render.metal.texture_pool import (
            MetalTexturePool,
        )
        pool = MetalTexturePool(_FakeContext())
        a = pool.acquire(4, 4)                                   # RGBA16F
        pool.release_direct(a)
        b = pool.acquire(4, 4, pixel_format=Metal.MTLPixelFormatR32Float)
        assert b.texture is not a.texture                        # 格式不同不复用
        pool.release_direct(b)
        c = pool.acquire(4, 4)
        assert c.texture is a.texture                            # 同格式复用

    def test_unknown_format_rejected_explicitly(self):
        from src.features.composite.render.metal.texture_pool import (
            MetalPoolUnknownFormat, MetalTexturePool,
        )
        pool = MetalTexturePool(_FakeContext())
        with pytest.raises(MetalPoolUnknownFormat):
            pool.acquire(4, 4, pixel_format="RG8Unorm-fake")

    def test_migrated_shape_registry(self):
        # 形态盘点断言：已迁移 = RGBA16F/R32F/RGBA32F。
        import Metal
        from src.features.composite.render.metal import texture_pool as tp
        for name in ("MTLPixelFormatRGBA16Float", "MTLPixelFormatR32Float",
                     "MTLPixelFormatRGBA32Float"):
            assert name in tp._FORMAT_BYTES_BY_NAME
            assert hasattr(Metal, name)


# ── 异步发布状态机（注入 fake 调度器/上下文，无 GPU） ────────────────────


class _FakeCommandBuffer:
    def gpuStartTime(self):
        return 1.0

    def gpuEndTime(self):
        return 1.002


class _FakeScheduler:
    """环容量 2 的最小调度器（只驱动 submit/poll 语义，不碰 GPU）。"""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 2
        self._callbacks: list = []
        self.paused = False

    def try_reserve(self):
        if self.paused or self.in_flight >= self.max_in_flight:
            return None
        self.in_flight += 1
        return len(self._callbacks)

    def cancel_reservation(self, sequence):
        self.in_flight = max(0, self.in_flight - 1)
        return True

    def submit_reserved(self, sequence, *, release_callbacks=(), on_complete=None):
        self._callbacks.append((sequence, release_callbacks, on_complete))
        return type("S", (), {"sequence": sequence,
                              "command_buffer": _FakeCommandBuffer()})()

    def poll(self, *, now=None):
        finished = tuple(seq for seq, _, _ in self._callbacks)
        for seq, callbacks, on_complete in self._callbacks:
            for cb in callbacks:
                cb()
            if on_complete is not None:
                on_complete(None, None)
        self._callbacks.clear()
        self.in_flight = 0
        return finished

    def pending_timeouts(self, *, now=None):
        return ()

    def resume_if_idle(self):
        return False


class TestAsyncPublicationStateMachine:
    def _renderer_with_fake_scheduler(self):
        from src.features.composite.render.metal.renderer import (
            MetalCompositeRenderer,
        )
        renderer = MetalCompositeRenderer.__new__(MetalCompositeRenderer)
        renderer._context = None
        renderer._scheduler = _FakeScheduler()
        renderer._pool = None
        renderer._last_support = None
        renderer._last_plan = None
        renderer._last_gpu_ms = None
        renderer._pending = []
        renderer._published = {}
        renderer._history = None
        renderer.last_history_reset_reason = ""
        renderer._pending_frames = {}
        return renderer

    def test_pending_publishes_only_after_completion(self):
        from src.features.composite.render.metal.renderer import MetalFramePending
        renderer = self._renderer_with_fake_scheduler()
        pending = MetalFramePending(renderer, 0, 5, 1, "t")
        assert pending.try_get() is None          # 完成前不发布
        assert not pending.done
        handle = make_unavailable_handle("驱动测试")
        pending._settle(handle)
        assert pending.done
        assert pending.try_get() is handle

    def test_abandoned_late_completion_releases_lease(self):
        from src.features.composite.render.metal.renderer import MetalFramePending
        renderer = self._renderer_with_fake_scheduler()
        pending = MetalFramePending(renderer, 0, 5, 1, "t")
        released: list = []
        handle = CompositeGpuFrameHandle(
            frame=5, texture_id=1, available=True,
            release_callback=lambda: released.append(1))
        # 超时放弃后，迟到的完成只回收租约、不再发布。
        fallback = pending.wait(timeout_s=0.01)
        assert not fallback.available
        assert "超时" in fallback.fallback_reason
        pending._settle(handle)
        assert pending.try_get() is None
        assert released == [1]                    # 租约恰好归还一次

    def test_wait_returns_published_handle(self):
        from src.features.composite.render.metal.renderer import MetalFramePending
        renderer = self._renderer_with_fake_scheduler()

        class _AutoSettleScheduler(_FakeScheduler):
            def poll(self, *, now=None):
                for pending in list(renderer._pending_frames.values()):
                    if not pending.done:
                        pending._settle(CompositeGpuFrameHandle(
                            frame=pending.frame, texture_id=9,
                            width=4, height=4, available=True))
                return super().poll(now=now)

        renderer._scheduler = _AutoSettleScheduler()
        pending = MetalFramePending(renderer, 0, 5, 1, "t")
        renderer._pending_frames[0] = pending
        handle = pending.wait(timeout_s=1.0)
        assert handle.available and handle.texture_id == 9


# ── 真机层：导出桥 + 服务级一致性 + 压测 ─────────────────────────────────


@requires_metal
class TestExportBridgeOnGpu:
    def test_sequence_matches_cpu_reference_per_frame(self):
        from src.features.composite.render.metal.cpu_reference import (
            render_graph_cpu,
        )
        from src.features.composite.render.metal.export_bridge import (
            MetalCompositeExportBridge,
        )
        g = checker_transform_graph()
        bridge = MetalCompositeExportBridge()
        try:
            result = bridge.render_sequence(g, range(4))
            assert result.ok and not result.error
            assert sorted(result.frames) == [0, 1, 2, 3]
            for frame, pixels in result.frames.items():
                ref = render_graph_cpu(g, timeline_frame=frame)
                np.testing.assert_allclose(pixels, ref, atol=5e-3, rtol=0)
        finally:
            bridge.cleanup()

    def test_cancelled_sequence_stops_and_recycles(self):
        from src.features.composite.render.metal.export_bridge import (
            MetalCompositeExportBridge,
        )
        g = checker_transform_graph()
        bridge = MetalCompositeExportBridge()
        try:
            calls = {"n": 0}

            def cancel_after_two() -> bool:
                return calls["n"] >= 2

            def count(frame, pixels):
                calls["n"] += 1

            result = bridge.render_sequence(
                g, range(10), should_cancel=cancel_after_two, on_frame=count)
            assert result.cancelled
            assert len(result.frames) == 2
            renderer = bridge.renderer
            assert renderer.in_flight_frames == 0      # 在途帧全部结算
            assert renderer.pool.retiring_count == 0   # 完成回调已回收
            assert renderer.pool.live_count == 0       # 无泄漏的 GPU 句柄
        finally:
            bridge.cleanup()

    def test_error_sequence_reports_and_stays_idle(self):
        from src.features.composite.render.metal.export_bridge import (
            MetalCompositeExportBridge,
        )
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             node("b", "blur", radius=2.0), node("o", "output")],
            [conn("c", "b"), conn("b", "o")])
        bridge = MetalCompositeExportBridge()
        try:
            result = bridge.render_sequence(g, range(3))
            assert not result.ok and not result.cancelled
            assert "blur" in result.error               # 显式回退诊断
            renderer = bridge.renderer
            assert renderer.in_flight_frames == 0       # 无在途后台任务
            if renderer.pool is not None:               # 预检失败未触 GPU 分配
                assert renderer.pool.retiring_count == 0
                assert renderer.pool.live_count == 0
        finally:
            bridge.cleanup()

    def test_gpu_async_backpressure(self):
        """环容量 1：第一帧在途时第二帧必须显式回退，不发布半成品。"""
        from src.features.composite.render.metal.renderer import (
            MetalCompositeRenderer,
        )
        from src.shared.gpu.metal.submission_scheduler import (
            MetalSubmissionScheduler,
        )
        renderer = MetalCompositeRenderer()
        try:
            context = renderer._ensure_context()
            # 注入容量 1 的环（与默认调度器同设备）。
            renderer._scheduler = MetalSubmissionScheduler(
                context, max_in_flight=1,
                completion_timeout_s=MetalCompositeRenderer.COMPLETION_TIMEOUT_S)
            g = checker_transform_graph(8, 8)
            p1 = renderer.render_async(g, timeline_frame=0)
            assert not isinstance(p1, CompositeGpuFrameHandle)
            outcome = renderer.render_async(g, timeline_frame=1)
            assert isinstance(outcome, CompositeGpuFrameHandle)
            assert not outcome.available
            assert "环已满" in outcome.fallback_reason
            handle = p1.wait()
            assert handle.available
            handle.release()
            # 环排空后可再次提交。
            p3 = renderer.render_async(g, timeline_frame=2)
            assert not isinstance(p3, CompositeGpuFrameHandle)
            h3 = p3.wait()
            assert h3.available
            h3.release()
        finally:
            renderer.cleanup()


def _render_gl_reference(output_path: str) -> None:
    """子进程内渲染 GL 参考帧（run_standalone_gpu 的模块级检查函数）。"""
    import numpy as _np
    from src.features.composite.render.composite_graph_render_service import (
        CompositeGraphRenderService as _Service,
    )
    g = checker_transform_graph()
    service = _Service()
    try:
        frames = {}
        for frame in (0, 2, 5):
            image = service.render_cpu(g, timeline_frame=frame,
                                       quality="export")
            if image is None:
                raise RuntimeError(f"GL 参考帧 {frame} 渲染失败")
            frames[str(frame)] = image
        _np.savez_compressed(output_path, **frames)
    finally:
        service.cleanup()


@requires_metal
class TestServiceLevelConsistency:
    """验收：同一合成经 Metal（独立页/导出桥）与 GL（render_cpu）取相同帧对拍。

    GL 无头上下文不能与活跃 Metal 设备同进程共存（本机实测
    moderngl.create_context 段错误，也是仓库提供 ``run_standalone_gpu``
    的原因），因此 GL 参考帧在独立子进程渲染后落盘，再与 Metal 帧对拍。
    """

    def test_metal_vs_gl_same_frames(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOPOS_COMPOSITE_METAL", "1")
        from tests.composite.standalone_gpu import run_standalone_gpu

        reference_npz = str(tmp_path / "gl_reference.npz")
        run_standalone_gpu(_render_gl_reference, reference_npz)
        gl_frames = np.load(reference_npz)

        from src.features.composite.render.composite_graph_render_service import (
            CompositeGraphRenderService,
        )
        service = CompositeGraphRenderService()
        try:
            g = checker_transform_graph()
            for frame in (0, 2, 5):
                handle = service.render_metal(g, timeline_frame=frame)
                assert handle is not None and handle.available
                metal_rgb = service._metal_renderer.readback(handle)[..., :3]
                handle.release()
                gl_rgb = gl_frames[str(frame)][..., ::-1]  # BGR → RGB
                np.testing.assert_allclose(
                    metal_rgb, gl_rgb, atol=GPU_VS_GL_TOL, rtol=0)
        finally:
            service.cleanup()

    def test_stress_returns_pool_to_idle(self):
        """压测：多帧渲染 + 中途 seek 重渲染后，GPU 句柄回到空闲上限。"""
        from src.features.composite.render.metal.export_bridge import (
            MetalCompositeExportBridge,
        )
        g = checker_transform_graph(64, 48)
        bridge = MetalCompositeExportBridge()
        try:
            frames = list(range(8)) + [2, 3] + list(range(8, 12))
            result = bridge.render_sequence(g, frames, use_cache=True)
            assert result.ok
            # 中途 seek 回重渲染的 2/3 与首次同键 → dict 去重（内容一致）。
            assert sorted(result.frames) == sorted(set(frames))
            renderer = bridge.renderer
            assert renderer.in_flight_frames == 0
            pool = renderer.pool
            assert pool is not None
            assert pool.retiring_count == 0
            assert pool.live_count == 0
            assert pool.bytes_allocated <= pool.budget_bytes
            assert renderer.cache.bytes_used <= renderer.cache.budget_bytes
        finally:
            bridge.cleanup()
