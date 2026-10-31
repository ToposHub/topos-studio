"""WD17-G94 修复回归测试（13 条审计发现）。

覆盖 ID：AUD-36104 / 36105 / 36107 / 36109 / 36114 / 36201 / 36203 /
36206（本文件内可修部分）/ 36208 / 36302 / 36601 / 36602 / 36700。

本机为 macOS：D3D11 路径全部用注入式 fake / monkeypatch 验证逻辑，
不触碰真实 COM；探测失败语义 = fail-closed（软件回退）。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from src.shared.export.gpu_trail import ExportGpuTrail
from src.shared.media.gpu_frame import GpuFrameRelease
from src.shared.media.source import MediaOpenError
from src.shared.media.windows_hw.d3d11_device import (
    DXGI_FORMAT_NV12,
    D3D11Device,
    D3D11DeviceIdentity,
)
from src.shared.media.windows_hw.d3d11_frame import D3D11GpuFrame
from src.shared.media.windows_hw.d3d11_passthrough_chain import D3D11PassthroughChain
from src.shared.media.windows_hw.d3d11_render_graph import (
    D3D11RenderResult,
    D3D11TimelineRenderer,
    compile_d3d11_render_plan,
)
from src.shared.media.windows_hw.d3d11_sync import (
    EventD3D11Fence,
    ImmediateD3D11Fence,
)
from src.shared.media.windows_hw.d3d11_texture_pool import (
    D3D11ExternalTextureLease,
    D3D11TexturePool,
)
from src.shared.media.windows_hw.native_bridge import (
    NativeBridgeCapabilities,
    load_windows_native_bridge,
)
from src.shared.media.windows_hw.reasons import FallbackReason

DEVICE_LUID = "00011E23-00000000"


# =========================================================================
# 共用 fake 工具（D3D11 注入式，不触碰真实 COM）
# =========================================================================


def make_fake_pool(depth: int = 4) -> Tuple[D3D11TexturePool, list]:
    calls = {"n": 0}
    released: list = []

    def factory() -> int:
        calls["n"] += 1
        return 0xBEEF0000 + calls["n"]

    pool = D3D11TexturePool(
        depth=depth, width=1920, height=1080,
        dxgi_format=DXGI_FORMAT_NV12,
        texture_factory=factory, texture_releaser=released.append,
    )
    return pool, released


def make_fake_device(luid: str = DEVICE_LUID) -> D3D11Device:
    return D3D11Device(
        0xABCDEF, 0x123456,
        D3D11DeviceIdentity(
            feature_level=0xC100,
            adapter_luid=luid,
            adapter_description="Fake AMD Radeon",
            vendor_name="amd",
            bgra_support=True,
        ),
    )


def make_frame(pool: D3D11TexturePool, index: int = 0) -> D3D11GpuFrame:
    lease = pool.try_acquire()
    assert lease is not None
    return D3D11GpuFrame(
        texture_ptr=lease.texture_ptr,
        dxgi_format=DXGI_FORMAT_NV12,
        width=1920, height=1080,
        lease=lease, adapter_luid=DEVICE_LUID,
        frame_index=index, pts_us=index * 1000,
    )


class FakeDecodeSource:
    """按队列供帧；close() 释放未读帧（解码源生命周期契约）。"""

    def __init__(self, frames: list) -> None:
        self._frames = list(frames)
        self.closed = False

    def read_frame(self) -> Tuple[Optional[D3D11GpuFrame], str]:
        if self._frames:
            return self._frames.pop(0), ""
        return None, "end_of_stream"

    def adapter_luid(self) -> str:
        return DEVICE_LUID

    def close(self) -> None:
        for frame in self._frames:
            frame.release()
        self._frames.clear()
        self.closed = True


class FakeSurface:
    """编码 surface 替身：持有与来源帧共享的 lease。"""

    def __init__(self, frame: D3D11GpuFrame, release_after_exc: Optional[Exception] = None) -> None:
        self._frame = frame
        self._release_after_exc = release_after_exc
        self.release_calls = 0
        self.release_after_calls = 0

    def release(self) -> None:
        self.release_calls += 1
        self._frame.release()

    def release_after(self, fence: object) -> None:
        self.release_after_calls += 1
        if self._release_after_exc is not None:
            raise self._release_after_exc
        self.release()


class FakeEncodeProvider:
    """直通链编码 provider 替身：可控制 release_after 是否失败。"""

    def __init__(self, release_after_exc: Optional[Exception] = None) -> None:
        self._release_after_exc = release_after_exc
        self._fence = ImmediateD3D11Fence()
        self.last_gpu_submit_ms: Optional[float] = 0.5
        self.last_completion_fence: object = None
        self.prepared = False

    def prepare(self) -> Tuple[bool, str]:
        self.prepared = True
        return True, ""

    def surface_from_frame(
        self, frame: D3D11GpuFrame,
    ) -> Tuple[Optional[FakeSurface], str]:
        return FakeSurface(frame, self._release_after_exc), ""

    def submit_to_encoder(self, surface: FakeSurface) -> Tuple[bool, str]:
        self.last_completion_fence = self._fence
        return True, ""


def make_chain(
    provider: FakeEncodeProvider, frame_count: int, trail: ExportGpuTrail,
) -> Tuple[D3D11PassthroughChain, D3D11TexturePool, FakeDecodeSource]:
    pool, _ = make_fake_pool(depth=max(1, frame_count))
    source = FakeDecodeSource([make_frame(pool, i) for i in range(frame_count)])
    chain = D3D11PassthroughChain(
        make_fake_device(), source, provider, trail,
    )
    return chain, pool, source


# =========================================================================
# AUD-36104：release_after 异常路径不泄漏共享 lease
# =========================================================================


class TestAud36104:
    def test_release_after_failure_releases_shared_lease(self) -> None:
        trail = ExportGpuTrail("aud36104", adapter_luid=DEVICE_LUID)
        provider = FakeEncodeProvider(
            release_after_exc=RuntimeError("fence wait failed"),
        )
        chain, pool, source = make_chain(provider, 1, trail)

        assert chain.prepare() == (True, "")
        outcome = chain.run(1)

        assert not outcome.ok
        assert FallbackReason.GPU_PROBE_FAILED.value in outcome.reason
        assert "encode_submit" in outcome.reason
        # 关键断言：surface 的兜底 release 被调用且共享 lease 已归还
        # （decode 池槽位不因 release_after 失败而永久损失）。
        assert pool.stats().in_use == 0
        chain.close()
        assert pool.stats().in_use == 0

    def test_release_after_double_release_guarded(self) -> None:
        """兜底 release 自身抛错（如重复归还）也不得逃逸，仅留痕。"""
        trail = ExportGpuTrail("aud36104b", adapter_luid=DEVICE_LUID)
        pool, _ = make_fake_pool(depth=1)
        frame = make_frame(pool)

        surface = SimpleNamespace(
            release_after=lambda fence: (_ for _ in ()).throw(RuntimeError("boom")),
            release=lambda: (_ for _ in ()).throw(ValueError("lease 重复归还")),
        )
        provider = FakeEncodeProvider()
        provider.surface_from_frame = lambda f: (surface, "")  # type: ignore[method-assign]
        source = FakeDecodeSource([frame])
        chain = D3D11PassthroughChain(make_fake_device(), source, provider, trail)

        assert chain.prepare() == (True, "")
        outcome = chain.run(1)  # 兜底 release 抛 ValueError 被吞，不逃逸
        assert not outcome.ok
        assert FallbackReason.GPU_PROBE_FAILED.value in outcome.reason
        chain.close()
        # fake surface 的 release 抛错意味着 lease 仍滞留——显式归还后关池，
        # 保持测试 hermetic（pool 拒绝带在途 lease 关闭是其 fail-closed 设计）。
        frame.release()
        pool.close()


# =========================================================================
# AUD-36105：同一 trail 二次 run() 不因段重复登记抛 ValueError / 泄漏帧
# =========================================================================


class TestAud36105:
    def test_second_run_on_same_trail_is_absorbed(self) -> None:
        trail = ExportGpuTrail("aud36105", adapter_luid=DEVICE_LUID)
        provider = FakeEncodeProvider()

        chain1, pool1, source1 = make_chain(provider, 1, trail)
        assert chain1.prepare() == (True, "")
        first = chain1.run(1)
        assert first.ok, first.reason
        chain1.close()

        # 同一 trail 二次 run：ExportGpuTrail.record_stage 会对同名段抛
        # ValueError——修复后链路吸收该错误，帧照常流转、不泄漏。
        chain2, pool2, source2 = make_chain(provider, 2, trail)
        assert chain2.prepare() == (True, "")
        second = chain2.run(2)
        assert second.ok, second.reason
        assert second.frames_completed == 2
        chain2.close()

        assert pool1.stats().in_use == 0
        assert pool2.stats().in_use == 0
        # 首轮 trail 证据保留，段名仍唯一。
        snap = trail.snapshot()
        stage_names = [s.name for s in snap.stages]
        assert "decode" in stage_names and "encode_submit" in stage_names
        assert len(stage_names) == len(set(stage_names))


# =========================================================================
# AUD-36107：release() 与 render_frame() 并发必须被状态锁串行化
# =========================================================================


class _RaceBackend:
    """render_frame 阻塞至测试放行；release() 记录是否发生在渲染完成前。"""

    def __init__(self) -> None:
        self.render_entered = threading.Event()
        self.render_done = threading.Event()
        self.released_before_render_done = False
        self.released = threading.Event()

    def prepare_plan(self, plan: dict, adapter_luid: str) -> None:
        return None

    def render_frame(self, plan, source_texture, frame_index, width, height):
        self.render_entered.set()
        self.render_done.wait(timeout=5.0)
        return D3D11RenderResult(None, "")

    def release(self) -> None:
        if not self.render_done.is_set():
            self.released_before_render_done = True
        self.released.set()


class TestAud36107:
    def _make_frame(self) -> D3D11GpuFrame:
        return D3D11GpuFrame(
            texture_ptr=0xCAFE,
            dxgi_format=DXGI_FORMAT_NV12,
            width=1920, height=1080,
            lease=D3D11ExternalTextureLease(0xCAFE, lambda: None),
            adapter_luid=DEVICE_LUID,
            frame_index=0, pts_us=0,
        )

    def test_release_waits_for_in_flight_render(self) -> None:
        backend = _RaceBackend()
        renderer = D3D11TimelineRenderer(DEVICE_LUID, backend=backend)
        plan = compile_d3d11_render_plan(
            timeline_revision="rev-1", renderer_version="t",
            source_format="nv12", output_format="nv12",
        )
        assert renderer.prepare(plan) == (True, "")

        results: list = []

        def _render() -> None:
            results.append(renderer.render_frame(self._make_frame()))

        worker = threading.Thread(target=_render)
        worker.start()
        assert backend.render_entered.wait(timeout=2.0)

        # 并发 release：修复后必须等待在途 render_frame 完成（持锁），
        # 而不是中途置空 _plan/_backend。
        releaser = threading.Thread(target=renderer.release)
        releaser.start()
        time.sleep(0.05)  # 给 release 线程机会去抢锁（若未串行化会立刻通过）
        backend.render_done.set()
        worker.join(timeout=5.0)
        releaser.join(timeout=5.0)
        assert not worker.is_alive() and not releaser.is_alive()

        # release 严格发生在原生 render_frame 返回之后。
        assert backend.released_before_render_done is False
        assert len(results) == 1
        assert isinstance(results[0], D3D11RenderResult)  # 无 AttributeError 逃逸

    def test_render_after_release_fails_closed_without_raising(self) -> None:
        backend = _RaceBackend()
        renderer = D3D11TimelineRenderer(DEVICE_LUID, backend=backend)
        renderer.release()
        renderer.release()  # 幂等
        result = renderer.render_frame(self._make_frame())
        assert isinstance(result, D3D11RenderResult)
        assert result.target is None
        assert result.reason  # fail-closed 而非异常


# =========================================================================
# AUD-36109：fence 回收线程有界等待 + 失败兜底归还
# =========================================================================


def _wait_until(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _NeverFence:
    """永不 signal 的 fence；wait 按超时真实阻塞后返回 False。"""

    signalled = False

    def __init__(self) -> None:
        self.wait_args: list = []

    def wait(self, timeout_s=None) -> bool:
        self.wait_args.append(timeout_s)
        if timeout_s is not None:
            time.sleep(timeout_s)
        return False


class _ExplodingFence:
    """wait 抛 OSError（设备移除类故障）。"""

    signalled = False

    def wait(self, timeout_s=None) -> bool:
        raise OSError("device removed (DXGI_ERROR_DEVICE_REMOVED)")


class TestAud36109:
    def test_wait_timeout_returns_slot_to_pool(self, monkeypatch) -> None:
        import src.shared.media.windows_hw.d3d11_texture_pool as tp
        monkeypatch.setattr(tp, "_FENCE_RECLAIM_TIMEOUT_S", 0.05)

        pool, _ = make_fake_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        fence = _NeverFence()
        lease.release_after(fence)
        assert pool.stats().in_use == 1  # 归还前不复用

        # 修复点：超时后兜底同步归还，池槽不静默蒸发。
        assert _wait_until(lambda: pool.stats().in_use == 0)
        assert fence.wait_args and fence.wait_args[0] == 0.05  # 有界等待
        recycled = pool.try_acquire()
        assert recycled is not None
        recycled.release()
        pool.close()

    def test_wait_oserror_returns_slot_to_pool(self, monkeypatch) -> None:
        import src.shared.media.windows_hw.d3d11_texture_pool as tp
        monkeypatch.setattr(tp, "_FENCE_RECLAIM_TIMEOUT_S", 0.05)

        pool, _ = make_fake_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        lease.release_after(_ExplodingFence())
        assert _wait_until(lambda: pool.stats().in_use == 0)
        pool.close()

    def test_external_lease_variant_same_semantics(self, monkeypatch) -> None:
        import src.shared.media.windows_hw.d3d11_texture_pool as tp
        monkeypatch.setattr(tp, "_FENCE_RECLAIM_TIMEOUT_S", 0.05)

        released: list = []
        lease = D3D11ExternalTextureLease(0xCAFE, lambda: released.append(0xCAFE))
        lease.release_after(_ExplodingFence())
        assert _wait_until(lambda: bool(released))
        assert lease.released

    def test_signalled_fence_releases_synchronously(self) -> None:
        pool, _ = make_fake_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        lease.release_after(ImmediateD3D11Fence())
        assert pool.stats().in_use == 0  # 不等回收线程
        pool.close()

    def test_event_fence_normal_path_unchanged(self) -> None:
        pool, _ = make_fake_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        fence = EventD3D11Fence()
        lease.release_after(fence)
        fence.signal()
        assert _wait_until(lambda: pool.stats().in_use == 0)
        pool.close()


# =========================================================================
# AUD-36114：destroy() 与 in-flight bind_texture 不得造成 ring 槽双占
# =========================================================================


def _make_interop_factory():
    def factory(d3d_texture, frame_index, pixel_format, bit_depth):
        native_release = GpuFrameRelease(
            retain_token=(d3d_texture, frame_index),
            release_callback=lambda: None,
            owner_backend="opengl",
        )
        return (SimpleNamespace(component="Y", api="opengl"),), native_release

    return factory


class TestAud36114:
    def test_destroy_keeps_active_slots_no_double_occupancy(self) -> None:
        bridge_mod = __import__(
            "src.shared.media.windows_hw.d3d11_gl_bridge",
            fromlist=["D3D11GLBridge"],
        )
        D3D11GLBridge = bridge_mod.D3D11GLBridge
        bridge = D3D11GLBridge(ring_size=2, interop_factory=_make_interop_factory())
        assert bridge.initialize() is True

        first = bridge.bind_texture(0xAA, frame_index=0)
        assert first is not None
        assert bridge.is_initialized is True

        # destroy 后：初始化态撤销，但 in-flight 槽位不擦除。
        bridge.destroy()
        assert bridge.is_initialized is False

        # 重新初始化后 bind 同一 ring 槽必须被拒（双占防护）。
        assert bridge.initialize() is True
        second = bridge.bind_texture(0xBB, frame_index=0)  # slot 0 仍被 first 持有
        assert second is None
        assert bridge.last_fallback_reason is not None
        assert "ring_exhausted" in bridge.last_fallback_reason

        # 归还 in-flight 句柄后槽位可复用。
        first[1].release()
        third = bridge.bind_texture(0xCC, frame_index=0)
        assert third is not None
        third[1].release()

    def test_bind_after_destroy_reports_not_implemented(self) -> None:
        bridge_mod = __import__(
            "src.shared.media.windows_hw.d3d11_gl_bridge",
            fromlist=["D3D11GLBridge"],
        )
        bridge = bridge_mod.D3D11GLBridge(ring_size=2, interop_factory=_make_interop_factory())
        assert bridge.initialize() is True
        bridge.destroy()
        result = bridge.bind_texture(0xAA, frame_index=1)  # slot 1 从未占用
        assert result is None
        assert bridge.last_fallback_reason == (
            FallbackReason.INTEROP_NOT_YET_IMPLEMENTED.value
        )


# =========================================================================
# AUD-36201：open() 失败路径关闭残留容器并复位 _is_open
# =========================================================================


class _FakeContainer:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _make_planar_source(container: _FakeContainer):
    from src.shared.media.windows_hw.hw_planar_source import WindowsHwPlanarSource
    src = WindowsHwPlanarSource.__new__(WindowsHwPlanarSource)
    src._path = "fake.mp4"
    src._hw_decoder = None
    src._hw_active = False
    src._hw_output_format = None
    src._is_planar_mode = False
    src._is_open = True  # 模拟 super().open() 已成功
    src._container = container
    src._video_stream = object()
    src._pending_av_frame = None
    src._metadata = MagicMock()
    src._metadata.codec_name = "h264"
    return src


def _wait_close_calls(container, n: int = 1, timeout: float = 3.0) -> None:
    """有界等待常驻关闭线程处理完容器（Q01/F1.1：close 恒异步入队）。

    W01：Windows 上守护线程调度时序不同，open() 抛错返回后 close_calls
    可能尚未递增——立即断言 0==1 为时序误报，不是句柄泄漏。
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if container.close_calls >= n:
            return
        _time.sleep(0.02)


class TestAud36201:
    def test_open_failure_closes_container_and_resets_state(self, monkeypatch) -> None:
        from src.shared.media.ffmpeg_source import FFmpegMediaSource
        from src.shared.media.windows_hw import hw_planar_source as mod

        container = _FakeContainer()
        src = _make_planar_source(container)
        monkeypatch.setattr(FFmpegMediaSource, "open", lambda self: None)
        # 让"无硬解器"检查失败 → open 抛 MediaOpenError。
        monkeypatch.setattr(
            mod.HardwareCapability, "get_best_decoder_for_codec",
            staticmethod(lambda codec: None),
        )

        with pytest.raises(MediaOpenError, match="No hardware decoder"):
            src.open()

        # 修复点：残留容器被关闭、打开状态复位——消费方捕获异常后
        # 无需（也无法）再 close，句柄不泄漏。
        _wait_close_calls(container)
        assert container.close_calls == 1
        assert src._container is None
        assert src._is_open is False
        assert src.using_hwaccel is False

    def test_hwaccel_reopen_failure_also_cleans_up(self, monkeypatch) -> None:
        from src.shared.media.ffmpeg_source import FFmpegMediaSource
        from src.shared.media.windows_hw import hw_planar_source as mod

        container = _FakeContainer()
        src = _make_planar_source(container)
        monkeypatch.setattr(FFmpegMediaSource, "open", lambda self: None)
        monkeypatch.setattr(
            mod.HardwareCapability, "get_best_decoder_for_codec",
            staticmethod(lambda codec: mod.HardwareDecoder.NVDEC),
        )
        monkeypatch.setattr(
            mod.HardwareCapability, "check_codec_support",
            staticmethod(lambda decoder, codec: True),
        )

        def _boom(decoder) -> None:
            raise MediaOpenError("hwaccel open failed (nvdec): boom", source_path="fake.mp4")

        monkeypatch.setattr(src, "_reopen_with_hwaccel", _boom)

        with pytest.raises(MediaOpenError, match="hwaccel open failed"):
            src.open()
        _wait_close_calls(container)
        assert container.close_calls == 1
        assert src._is_open is False
        assert src._container is None

    def test_successful_open_still_works(self, monkeypatch) -> None:
        """清理保护不影响成功路径：monkeypatch 各阶段为 no-op。"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource
        from src.shared.media.windows_hw import hw_planar_source as mod

        container = _FakeContainer()
        src = _make_planar_source(container)
        monkeypatch.setattr(FFmpegMediaSource, "open", lambda self: None)
        monkeypatch.setattr(
            mod.HardwareCapability, "get_best_decoder_for_codec",
            staticmethod(lambda codec: mod.HardwareDecoder.NVDEC),
        )
        monkeypatch.setattr(
            mod.HardwareCapability, "check_codec_support",
            staticmethod(lambda decoder, codec: True),
        )
        monkeypatch.setattr(src, "_reopen_with_hwaccel", lambda decoder: None)
        monkeypatch.setattr(src, "_verify_hw_output", lambda: None)
        monkeypatch.setattr(src, "_resolve_probe_color_tags", lambda: {})

        src.open()
        assert container.close_calls == 0
        assert src._is_open is True
        assert src.using_hwaccel is True


# =========================================================================
# AUD-36203：probe 的 NV12 层必须用 dxgiformat.h 真值（103），错值 fail-closed
# =========================================================================


def _fake_native_device() -> SimpleNamespace:
    return SimpleNamespace(
        identity=SimpleNamespace(
            adapter_luid=DEVICE_LUID,
            feature_level=0xC100,
            adapter_description="Fake GPU",
        ),
        device_ptr=0x1234,
        release=lambda: None,
    )


class TestAud36203:
    def _patch_windows(self, monkeypatch, probe_mod) -> None:
        monkeypatch.setattr(probe_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(
            probe_mod, "create_d3d11_device", lambda: (_fake_native_device(), ""),
        )

    def test_probe_checks_true_nv12_value(self, monkeypatch) -> None:
        import src.shared.media.windows_hw.windows_zero_copy_probe as probe_mod
        self._patch_windows(monkeypatch, probe_mod)
        # 根因修复后的世界：包常量 = dxgiformat.h 真值 103。
        monkeypatch.setattr(probe_mod, "DXGI_FORMAT_NV12", 103)
        seen: dict = {}

        def fake_check(device, fmt):
            seen["fmt"] = fmt
            return False, 0, "gpu_probe_failed:no_support"

        monkeypatch.setattr(probe_mod, "check_format_support", fake_check)

        result = probe_mod.run_probe(frames=4)
        # 检测的是真 NV12（103），不是 A8_UNORM（65）。
        assert seen.get("fmt") == 103
        assert result.fallback_reason is not None
        assert result.fallback_reason.startswith("format_support_layer:")
        assert "dxgi_constant_mismatch" not in result.fallback_reason

    def test_probe_fails_closed_on_wrong_package_constant(self, monkeypatch) -> None:
        """包常量仍是错值（65=A8_UNORM）时：显式 fail-closed，不误放行。"""
        import src.shared.media.windows_hw.windows_zero_copy_probe as probe_mod
        self._patch_windows(monkeypatch, probe_mod)
        monkeypatch.setattr(probe_mod, "DXGI_FORMAT_NV12", 65)
        called: list = []

        def fake_check(device, fmt):  # 即使驱动对 65 报支持也不得采信
            called.append(fmt)
            return True, 0x80, ""

        monkeypatch.setattr(probe_mod, "check_format_support", fake_check)

        result = probe_mod.run_probe(frames=4)
        assert not called  # 常量门禁在 CheckFormatSupport 之前拦截
        layer = result.layers["format_support_nv12"]
        assert layer["ok"] is False
        assert "dxgi_constant_mismatch" in layer["reason"]
        assert "dxgi_constant_mismatch" in (result.fallback_reason or "")
        assert result.zero_copy is False


# =========================================================================
# AUD-36206（本文件内可修部分）：格式码识别引用共享常量，真值可传导
# =========================================================================


class TestAud36206:
    def _patch_true_values(self, monkeypatch) -> None:
        """模拟根因修复后的世界：包常量 = dxgiformat.h 真值（103/104）。"""
        import src.shared.media.windows_hw.d3d11_frame as frame_mod
        import src.shared.media.windows_hw.gpu_encode_surface as ges
        monkeypatch.setattr(ges, "DXGI_FORMAT_NV12", 103)
        monkeypatch.setattr(ges, "DXGI_FORMAT_P010", 104)
        monkeypatch.setattr(frame_mod, "DXGI_FORMAT_NV12", 103)
        monkeypatch.setattr(frame_mod, "DXGI_FORMAT_P010", 104)
        monkeypatch.setattr(frame_mod, "D3D11_SEMI_PLANAR_FORMATS", (103, 104))
        return ges

    @staticmethod
    def _ok_support(device, fmt) -> Tuple[bool, int, str]:
        return True, 0x80, ""

    def test_true_abi_values_flow_through_provider(self, monkeypatch) -> None:
        ges = self._patch_true_values(monkeypatch)
        device = make_fake_device()

        p010 = ges.D3D11EncodeSurfaceProvider(
            device, "amf", 1920, 1080, dxgi_format=104,
            format_support_fn=self._ok_support,
        )
        assert p010.prepare() == (True, "")  # 旧字面量(100)会把 104 判 unknown

        frame = D3D11GpuFrame(
            texture_ptr=0xCAFE, dxgi_format=104, width=1920, height=1080,
            lease=D3D11ExternalTextureLease(0xCAFE, lambda: None),
            adapter_luid=DEVICE_LUID, frame_index=0, bit_depth=10,
        )
        surface, why = p010.surface_from_frame(frame)
        assert surface is not None, why
        assert surface.dxgi_format == 104  # ABI 边界收到真值

        nv12 = ges.D3D11EncodeSurfaceProvider(
            device, "amf", 1920, 1080, dxgi_format=103,
            format_support_fn=self._ok_support,
        )
        assert nv12.prepare() == (True, "")

    def test_legacy_wrong_value_fails_closed_not_mislabeled(self, monkeypatch) -> None:
        ges = self._patch_true_values(monkeypatch)
        device = make_fake_device()
        provider = ges.D3D11EncodeSurfaceProvider(
            device, "amf", 1920, 1080, dxgi_format=65,  # 旧错值 = A8_UNORM
            format_support_fn=self._ok_support,
        )
        ok, why = provider.prepare()
        assert not ok  # 不再把 65 误标为 "nv12" 放行
        assert FallbackReason.CODEC_UNSUPPORTED_BY_HW.value in why


# =========================================================================
# AUD-36208：loader 对 factory()/capabilities() 异常的防护
# =========================================================================


class _GoodBridge:
    def capabilities(self) -> NativeBridgeCapabilities:
        return NativeBridgeCapabilities(
            api_version=1, name="fake", version="t",
            decode_codecs=("h264",), encode_kinds=("amf",),
            d3d11_renderer=True,
        )

    def open_decoder(self, path, codec, device_ptr) -> object:
        return {}

    def decode_next(self, session) -> None:
        return None

    def close_decoder(self, session) -> None:
        return None

    def submit_encode(self, *args, **kwargs) -> None:
        return None


class TestAud36208:
    def test_factory_exception_converted_to_probe_failure(self) -> None:
        def exploding_factory():
            raise RuntimeError("native init crashed")

        module = SimpleNamespace(create_bridge=exploding_factory)
        result = load_windows_native_bridge(module=module)  # 不得抛出
        assert not result.available
        assert "native_bridge_factory" in result.reason
        assert FallbackReason.GPU_PROBE_FAILED.value in result.reason

    def test_capabilities_exception_converted_to_probe_failure(self) -> None:
        class _CapsBoom(_GoodBridge):
            def capabilities(self):
                raise OSError("driver query failed")

        module = SimpleNamespace(create_bridge=lambda: _CapsBoom())
        result = load_windows_native_bridge(module=module)
        assert not result.available
        assert "native_bridge_capabilities" in result.reason

    def test_normal_load_still_succeeds(self) -> None:
        module = SimpleNamespace(create_bridge=lambda: _GoodBridge())
        result = load_windows_native_bridge(module=module)
        assert result.available
        assert result.capabilities is not None and result.capabilities.valid


# =========================================================================
# AUD-36302：undo_fn/redo_fn 抛错时 action 留在原栈（栈/模型不失配）
# =========================================================================


class TestAud36302:
    def test_undo_fn_failure_keeps_action_on_undo_stack(self, qapp) -> None:
        from src.shared.node_canvas.undo_stack import EditAction, UndoStack
        stack = UndoStack()
        signals: list = []
        stack.changed.connect(lambda: signals.append(1))

        def bad_undo() -> None:
            raise RuntimeError("model rollback failed")

        stack.push(EditAction("bad", undo_fn=bad_undo, redo_fn=lambda: None))
        stack.undo()  # 不得抛出

        assert stack.can_undo()  # action 留在 undo 栈可重试
        assert not stack.can_redo()  # 不再转移到 redo（旧缺陷行为）
        assert stack.depth() == 1
        assert len(signals) == 2  # push + undo(失败) 均 emit

    def test_redo_fn_failure_keeps_action_on_redo_stack(self, qapp) -> None:
        from src.shared.node_canvas.undo_stack import EditAction, UndoStack
        stack = UndoStack()

        def bad_redo() -> None:
            raise RuntimeError("model replay failed")

        stack.push(EditAction("bad", undo_fn=lambda: None, redo_fn=bad_redo))
        stack.undo()
        assert stack.can_redo()
        stack.redo()  # 不得抛出
        assert not stack.can_undo()
        assert stack.can_redo()  # action 留在 redo 栈

    def test_failed_undo_can_be_retried(self, qapp) -> None:
        from src.shared.node_canvas.undo_stack import EditAction, UndoStack
        stack = UndoStack()
        calls: list = []

        def flaky_undo() -> None:
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("transient")

        stack.push(EditAction("flaky", undo_fn=flaky_undo, redo_fn=lambda: None))
        stack.undo()  # 第一次失败
        assert stack.can_undo()
        stack.undo()  # 重试成功 → 移入 redo
        assert not stack.can_undo()
        assert stack.can_redo()
        assert len(calls) == 2

    def test_successful_undo_redo_unchanged(self, qapp) -> None:
        from src.shared.node_canvas.undo_stack import EditAction, UndoStack
        stack = UndoStack()
        log: list = []
        stack.push(EditAction("ok", undo_fn=lambda: log.append("u"),
                              redo_fn=lambda: log.append("r")))
        stack.undo()
        stack.redo()
        assert log == ["u", "r"]
        assert stack.can_undo() and not stack.can_redo()


# =========================================================================
# AUD-36601 / AUD-36602：路径解析
# =========================================================================


class TestAud36601:
    def test_project_root_is_repo_root_not_src(self) -> None:
        import src.shared.paths.paths as paths_mod
        root = Path(paths_mod.__file__).resolve().parents[3]
        assert Path(paths_mod.get_project_root()).resolve() == root
        assert not paths_mod.get_project_root().endswith("/src")

    @pytest.mark.parametrize("name,fn", [
        ("luts", "get_lut_dir"),
        ("hdri", "get_hdri_dir"),
        ("models", "get_models_dir"),
        ("cache", "get_cache_dir"),
    ])
    def test_resource_dirs_resolve_under_repo_root(self, monkeypatch, name, fn) -> None:
        import src.shared.paths.paths as paths_mod
        root = Path(paths_mod.__file__).resolve().parents[3]
        # exists → True：避免测试真实创建目录。
        monkeypatch.setattr(paths_mod.os.path, "exists", lambda p: True)
        result = getattr(paths_mod, fn)()
        assert Path(result).resolve() == (root / name).resolve()
        assert "/src/" not in str(Path(result).resolve())


class TestAud36602:
    def test_makedirs_oserror_degrades_with_warning(self, monkeypatch, caplog) -> None:
        import logging as _logging
        import src.shared.paths.paths as paths_mod
        monkeypatch.setattr(paths_mod.os.path, "exists", lambda p: False)

        def failing_makedirs(path, exist_ok=False):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(paths_mod.os, "makedirs", failing_makedirs)
        with caplog.at_level(_logging.WARNING, logger="src.shared.paths.paths"):
            lut_dir = paths_mod.get_lut_dir()
        assert lut_dir.endswith("luts")  # 降级返回路径，不抛出
        assert any("创建 LUT 目录失败" in r.message for r in caplog.records)

    def test_non_os_error_is_not_masked(self, monkeypatch) -> None:
        """宽泛 except 已收窄为 OSError：非 OS 领域错误必须向上暴露。"""
        import src.shared.paths.paths as paths_mod
        monkeypatch.setattr(paths_mod.os.path, "exists", lambda p: False)

        def buggy_makedirs(path, exist_ok=False):
            raise ValueError("not a domain OSError")

        monkeypatch.setattr(paths_mod.os, "makedirs", buggy_makedirs)
        with pytest.raises(ValueError, match="not a domain OSError"):
            paths_mod.get_cache_dir()


# =========================================================================
# AUD-36700：包导出 get_config_manager
# =========================================================================


class TestAud36700:
    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        import src.shared.preferences as prefs
        saved = prefs._CONFIG_MANAGER_INSTANCE
        prefs._CONFIG_MANAGER_INSTANCE = None
        yield
        prefs._CONFIG_MANAGER_INSTANCE = saved

    def test_get_config_manager_exported_and_singleton(self, monkeypatch) -> None:
        import src.shared.preferences as prefs

        class FakeConfigManager:
            instantiated = 0

            def __init__(self) -> None:
                FakeConfigManager.instantiated += 1

            def get(self, key: str, default=None):
                return {"composite.node_cache_format": "npz"}.get(key, default)

        monkeypatch.setattr(prefs, "ConfigManager", FakeConfigManager)

        # 消费方（disk_cache_store）的确切导入方式：
        from src.shared.preferences import get_config_manager

        manager = get_config_manager()
        assert isinstance(manager, FakeConfigManager)
        assert get_config_manager() is manager  # 进程级单例
        assert FakeConfigManager.instantiated == 1
        assert manager.get("composite.node_cache_format", "auto") == "npz"

    def test_symbol_listed_in_all(self) -> None:
        import src.shared.preferences as prefs
        assert "get_config_manager" in prefs.__all__
        assert callable(prefs.get_config_manager)
