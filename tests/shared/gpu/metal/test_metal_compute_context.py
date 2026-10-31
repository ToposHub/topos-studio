"""MetalComputeContext 测试（计划 P4-01）。

两层：
- Fake 设备层（任意环境可跑）：API 语义——编译缓存、错误诊断、
  dispatch 编码顺序、finish 提交语义、批处理（多 kernel 单 commit）。
- 真机层（skipif 无 Metal 设备）：端到端正确性——buffer 往返、1D/2D
  dispatch、编译失败显式诊断。派发惯用法与 P1-01 probe 一致。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.shared.gpu.metal import MetalComputeContext, MetalComputeError
from src.shared.gpu.metal.context import MetalComputeContext as _Ctx


# ---------------------------------------------------------------------------
# Fake PyObjC 对象
# ---------------------------------------------------------------------------


class _FakeContents:
    def __init__(self, size: int):
        self._buf = bytearray(size)

    def as_buffer(self, length: int):
        return memoryview(self._buf)[:length]


class _FakeBuffer:
    def __init__(self, size: int, data: bytes | None = None, options: int = 0):
        self._size = size
        self.options = options
        self._contents = _FakeContents(size)
        if data:
            self._contents.as_buffer(size)[: len(data)] = data

    def length(self):
        return self._size

    def contents(self):
        return self._contents


class _FakeEncoder:
    def __init__(self, cb):
        self._cb = cb
        self.calls = []

    def setComputePipelineState_(self, pso):
        self.calls.append(("pso", pso))

    def setBuffer_offset_atIndex_(self, buf, offset, index):
        self.calls.append(("buffer", index, buf, offset))

    def setTexture_atIndex_(self, tex, index):
        self.calls.append(("texture", index, tex))

    def dispatchThreads_threadsPerThreadgroup_(self, size, tg):
        self.calls.append(("dispatch", tuple(size), tuple(tg)))

    def endEncoding(self):
        self.calls.append(("end",))


class _FakeCommandBuffer:
    def __init__(self):
        self.encoders = []
        self.committed = False
        self._error = None
        self.completion_handlers = []

    def computeCommandEncoder(self):
        enc = _FakeEncoder(self)
        self.encoders.append(enc)
        return enc

    def commit(self):
        self.committed = True

    def addCompletedHandler_(self, handler):
        self.completion_handlers.append(handler)

    def waitUntilCompleted(self):
        pass

    def error(self):
        return self._error

    def gpuStartTime(self):
        return 10.0

    def gpuEndTime(self):
        return 10.0025


class _FakePSO:
    def maxTotalThreadsPerThreadgroup(self):
        return 512

    def threadExecutionWidth(self):
        return 32


class _FakeFunction:
    def __init__(self, name: str):
        self.name = name


class _FakeLibrary:
    def __init__(self, functions: dict[str, _FakeFunction]):
        self._functions = functions

    def newFunctionWithName_(self, name):
        return self._functions.get(name)


class _FakeQueue:
    def __init__(self):
        self.command_buffers = []

    def commandBuffer(self):
        cb = _FakeCommandBuffer()
        self.command_buffers.append(cb)
        return cb


class _FakeDevice:
    """最小 MTLDevice 替身：记录调用并返回可预期对象。"""

    def __init__(self):
        self.queue = _FakeQueue()
        self.buffers: list[_FakeBuffer] = []
        self.libraries: dict[str, str] = {}  # name -> source
        self.library_compile_calls = 0

    def newCommandQueue(self):
        return self.queue

    def newLibraryWithSource_options_error_(self, source, options, error):
        self.library_compile_calls += 1
        return (
            _FakeLibrary({name: _FakeFunction(name) for name in self.libraries}),
            None,
        )

    def register_kernel(self, function_name: str):
        self.libraries[function_name] = function_name

    def newComputePipelineStateWithFunction_error_(self, function, error):
        if not isinstance(function, _FakeFunction) or function.name not in self.libraries:
            return None, "function not found"
        return _FakePSO(), None

    def newBufferWithBytes_length_options_(self, data, length, options):
        buf = _FakeBuffer(length, data, options)
        self.buffers.append(buf)
        return buf

    def newBufferWithLength_options_(self, length, options):
        buf = _FakeBuffer(length, options=options)
        self.buffers.append(buf)
        return buf


@pytest.fixture
def stub_metal_module(monkeypatch):
    """注入最小 Metal 模块替身（无 pyobjc 环境也能跑语义测试）。"""
    stub = MagicMock()
    stub.MTLResourceStorageModeShared = 1
    stub.MTLResourceStorageModePrivate = 2
    stub.MTLSizeMake = lambda w, h, d: (w, h, d)
    monkeypatch.setitem(sys.modules, "Metal", stub)
    return stub


@pytest.fixture
def fake_device(stub_metal_module):
    return _FakeDevice()


@pytest.fixture
def ctx(fake_device):
    return MetalComputeContext(device=fake_device)


# ---------------------------------------------------------------------------
# 语义层（fake 设备）
# ---------------------------------------------------------------------------


class TestKernelCompilation:
    def test_compile_and_cache(self, ctx, fake_device):
        fake_device.register_kernel("addone")
        k1 = ctx.compile_kernel("addone", "kernel void addone() {}")
        k2 = ctx.compile_kernel("addone", "kernel void addone() {}")
        assert k1 is k2
        assert k1.name == "addone"
        assert ctx.get_cached_kernel("addone") is k1

    def test_kernels_share_a_compiled_source_library(self, ctx, fake_device):
        fake_device.register_kernel("first")
        fake_device.register_kernel("second")
        source = "kernel void first() {} kernel void second() {}"

        first = ctx.compile_kernel("first", source)
        second = ctx.compile_kernel("second", source)

        assert first.name == "first"
        assert second.name == "second"
        assert fake_device.library_compile_calls == 1

    def test_missing_function_raises_with_diagnostic(self, ctx, fake_device):
        # 库里没有名为 missing 的函数
        with pytest.raises(MetalComputeError, match="missing"):
            ctx.compile_kernel("missing", "kernel void other() {}")

    def test_kernel_thread_limits_exposed(self, ctx, fake_device):
        fake_device.register_kernel("addone")
        k = ctx.compile_kernel("addone", "kernel void addone() {}")
        assert k.max_threads_per_group == 512
        assert k.thread_execution_width == 32


class TestBuffers:
    def test_create_buffer_from_numpy_roundtrip(self, ctx):
        arr = np.arange(8, dtype=np.float32)
        buf = ctx.create_buffer_from_numpy(arr)
        raw = ctx.read_buffer(buf)
        assert raw.nbytes == arr.nbytes
        assert np.frombuffer(raw, dtype=np.float32)[0] == 0.0

    def test_create_uninitialized_buffer(self, ctx):
        buf = ctx.create_buffer(64)
        assert buf.length() == 64

    def test_create_private_buffer_uses_gpu_only_storage(self, ctx):
        buf = ctx.create_private_buffer(128)
        assert buf.length() == 128
        assert buf.options == 2

    def test_write_buffer_does_not_use_readback(self, ctx):
        buf = ctx.create_buffer(8)
        ctx.write_buffer(buf, np.arange(8, dtype=np.uint8))
        assert bytes(ctx.read_buffer(buf)) == bytes(range(8))

    def test_write_buffer_rejects_overflow(self, ctx):
        with pytest.raises(ValueError, match="越界"):
            ctx.write_buffer(ctx.create_buffer(2), np.zeros(3, dtype=np.uint8))


class TestDispatchSemantics:
    def test_dispatch_encodes_without_commit(self, ctx, fake_device):
        fake_device.register_kernel("k")
        kernel = ctx.compile_kernel("k", "kernel void k() {}")
        buf = ctx.create_buffer(16)
        ctx.dispatch(kernel, buffers={0: buf}, threads=(4, 1, 1))
        cb = fake_device.queue.command_buffers[-1]
        assert not cb.committed  # 不提交——只有 finish 才 commit

    def test_finish_commits_and_resets(self, ctx, fake_device):
        fake_device.register_kernel("k")
        kernel = ctx.compile_kernel("k", "kernel void k() {}")
        ctx.dispatch(kernel, threads=(4, 1, 1))
        cb = fake_device.queue.command_buffers[-1]
        ctx.finish()
        assert cb.committed
        # finish 后新的 dispatch 开新 command buffer
        ctx.dispatch(kernel, threads=(4, 1, 1))
        assert fake_device.queue.command_buffers[-1] is not cb

    def test_finish_exposes_measured_gpu_interval(self, ctx, fake_device):
        fake_device.register_kernel("k")
        kernel = ctx.compile_kernel("k", "kernel void k() {}")
        ctx.dispatch(kernel, threads=(4, 1, 1))
        ctx.finish()
        assert ctx.last_gpu_time_ms == pytest.approx(2.5)

    def test_multiple_kernels_share_one_command_buffer(self, ctx, fake_device):
        fake_device.register_kernel("a")
        fake_device.register_kernel("b")
        ka = ctx.compile_kernel("a", "kernel void a() {}")
        kb = ctx.compile_kernel("b", "kernel void b() {}")
        ctx.dispatch(ka, threads=(4, 1, 1))
        ctx.dispatch(kb, threads=(4, 1, 1))
        ctx.finish()
        assert len(fake_device.queue.command_buffers) == 1  # 批处理：单 commit
        cb = fake_device.queue.command_buffers[0]
        assert len(cb.encoders) == 2

    def test_encoder_binding_order(self, ctx, fake_device):
        fake_device.register_kernel("k")
        kernel = ctx.compile_kernel("k", "kernel void k() {}")
        b0 = ctx.create_buffer(16)
        b1 = ctx.create_buffer(16)
        ctx.dispatch(kernel, buffers={1: b1, 0: b0}, threads=(4, 1, 1))
        ctx.finish()
        calls = fake_device.queue.command_buffers[0].encoders[0].calls
        # pso 先绑定，buffer 按 index 升序
        assert calls[0][0] == "pso"
        assert [c[1] for c in calls if c[0] == "buffer"] == [0, 1]
        assert calls[-1][0] == "end"

    def test_default_group_1d_capped_by_kernel_limit(self, ctx, fake_device):
        fake_device.register_kernel("k")
        kernel = ctx.compile_kernel("k", "kernel void k() {}")
        ctx.dispatch(kernel, threads=(10_000, 1, 1))
        ctx.finish()
        dispatch_call = next(
            c for c in fake_device.queue.command_buffers[0].encoders[0].calls
            if c[0] == "dispatch"
        )
        _, threads, group = dispatch_call
        assert threads == (10_000, 1, 1)
        assert group == (512, 1, 1)  # 裁剪到 kernel 上限

    def test_default_group_2d_16x16(self, ctx, fake_device):
        fake_device.register_kernel("k")
        kernel = ctx.compile_kernel("k", "kernel void k() {}")
        ctx.dispatch(kernel, threads=(64, 64, 1))
        ctx.finish()
        dispatch_call = next(
            c for c in fake_device.queue.command_buffers[0].encoders[0].calls
            if c[0] == "dispatch"
        )
        _, _, group = dispatch_call
        assert group == (16, 32, 1)  # 16 * (512//16) = 16x32 ≤ 512

    def test_finish_without_dispatch_is_noop(self, ctx, fake_device):
        ctx.finish()
        assert fake_device.queue.command_buffers == []

    def test_gpu_error_raises_with_diagnostic(self, ctx, fake_device):
        fake_device.register_kernel("k")
        kernel = ctx.compile_kernel("k", "kernel void k() {}")
        ctx.dispatch(kernel, threads=(1, 1, 1))
        cb = fake_device.queue.command_buffers[-1]
        cb._error = "simulated GPU fault"
        with pytest.raises(MetalComputeError, match="simulated GPU fault"):
            ctx.finish()

    def test_dispatch_failure_drops_poisoned_command_buffer(self, ctx, fake_device):
        """编码中途抛错的 buffer 必须被弃置：未 endEncoding 的 encoder 之后
        无法再挂新 encoder，提交会触发 Metal 校验错误。"""
        fake_device.register_kernel("k")
        kernel = ctx.compile_kernel("k", "kernel void k() {}")

        def _raising_encoder(self):
            enc = _FakeEncoder(self)
            def _boom(*args, **kwargs):
                raise RuntimeError("bind failed")
            enc.setBuffer_offset_atIndex_ = _boom
            return enc

        original = _FakeCommandBuffer.computeCommandEncoder
        _FakeCommandBuffer.computeCommandEncoder = _raising_encoder
        try:
            with pytest.raises(RuntimeError, match="bind failed"):
                ctx.dispatch(kernel, buffers={0: None}, threads=(4, 1, 1))
        finally:
            _FakeCommandBuffer.computeCommandEncoder = original

        assert ctx.has_dropped_work
        # AUD-1401：丢弃后的 submit 必须显式报错（一次即清），而不是静默
        # 返回 None 冒充"没有 pending 工作"——已成功编码的前序 kernel
        # 随 buffer 一并丢失，调用方需重编整条链。
        with pytest.raises(MetalComputeError, match="丢弃"):
            ctx.finish()
        assert not ctx.has_dropped_work
        assert not any(cb.committed for cb in fake_device.queue.command_buffers)

        # 后续 dispatch 拿到全新 command buffer 并正常提交
        buf = ctx.create_buffer(16)
        ctx.dispatch(kernel, buffers={0: buf}, threads=(4, 1, 1))
        ctx.finish()
        assert fake_device.queue.command_buffers[-1].committed
        assert len(fake_device.queue.command_buffers[-1].encoders) == 1


class TestAvailabilityProbe:
    def test_availability_reason_cached(self):
        ok1, reason1 = MetalComputeContext.availability_reason()
        ok2, reason2 = MetalComputeContext.availability_reason()
        assert (ok1, reason1) == (ok2, reason2)
        assert isinstance(ok1, bool)


# ---------------------------------------------------------------------------
# 真机层（无 Metal 设备时跳过）
# ---------------------------------------------------------------------------

_REAL_OK, _REAL_UNAVAILABLE_REASON = _Ctx.availability_reason()
requires_metal = pytest.mark.skipif(
    not _REAL_OK, reason=f"Metal 不可用: {_REAL_UNAVAILABLE_REASON}"
)

_ADDONE_MSL = """
#include <metal_stdlib>
using namespace metal;
kernel void addone(device float* io [[buffer(0)]],
                   uint gid [[thread_position_in_grid]]) { io[gid] += 1.0f; }
"""


@requires_metal
class TestRealDevice:
    def test_addone_roundtrip(self):
        ctx = MetalComputeContext()
        kernel = ctx.compile_kernel("addone", _ADDONE_MSL)
        seed = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
        buf = ctx.create_buffer_from_numpy(seed)
        ctx.dispatch_sync(kernel, buffers={0: buf}, threads=(4, 1, 1))
        out = np.frombuffer(ctx.read_buffer(buf), dtype=np.float32)
        assert out == pytest.approx([2.0, 3.0, 4.0, 5.0])

    def test_compile_error_raises_with_diagnostics(self):
        ctx = MetalComputeContext()
        with pytest.raises(MetalComputeError, match="MSL 编译失败"):
            ctx.compile_kernel("broken", "kernel void broken( { this is not MSL")

    def test_missing_function_raises(self):
        ctx = MetalComputeContext()
        with pytest.raises(MetalComputeError, match="not_defined"):
            ctx.compile_kernel(
                "not_defined",
                "kernel void different_name() {}",
            )

    def test_2d_dispatch_and_batching(self):
        """2D 网格 + 多 kernel 单 commit 批处理。"""
        msl = """
        #include <metal_stdlib>
        using namespace metal;
        kernel void fill(device uint* out [[buffer(0)]],
                         uint2 gid [[thread_position_in_grid]]) {
            if (gid.x < 8 && gid.y < 8) { out[gid.y * 8 + gid.x] = gid.x * 1000u + gid.y; }
        }
        kernel void scale(device uint* io [[buffer(0)]],
                          uint gid [[thread_position_in_grid]], uint n [[threads_per_grid]]) {
            if (gid < n) { io[gid] = io[gid] * 2u; }
        }
        """
        ctx = MetalComputeContext()
        fill = ctx.compile_kernel("fill", msl)
        scale = ctx.compile_kernel("scale", msl)
        out = ctx.create_buffer(8 * 8 * 4)
        ctx.dispatch(fill, buffers={0: out}, threads=(8, 8, 1))
        ctx.dispatch(scale, buffers={0: out}, threads=(64, 1, 1))
        ctx.finish()  # 单次同步点
        result = np.frombuffer(ctx.read_buffer(out), dtype=np.uint32).reshape(8, 8)
        # kernel 写 out[gid.y*8 + gid.x] = gid.x*1000 + gid.y → result[y][x]
        expected = (np.arange(8)[None, :] * 1000 + np.arange(8)[:, None]) * 2
        assert (result == expected).all()

    def test_stats_and_device_name(self):
        ctx = MetalComputeContext()
        assert ctx.device_name
        assert ctx.get_stats()["backend"] == "metal_compute"

    def test_create_texture_from_numpy_on_discrete_gpu(self):
        """Shared 纹理存储在独显 macOS 非法——必须按统一内存与否选
        Shared/Managed（回归：本机 AMD 6900XT 上 Shared 会返回 nil）。"""
        import Metal

        ctx = MetalComputeContext()
        img = np.zeros((4, 4, 4), dtype=np.uint8)
        tex = ctx.create_texture_from_numpy(
            img, Metal.MTLPixelFormatRGBA8Unorm, 4, 4
        )
        assert tex is not None
