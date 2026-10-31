"""Metal compute 基础设施（计划 P4-01）。

`MetalComputeContext` 是 compute 维度（`ComputeBackendKind.METAL_COMPUTE`）
的基础实现：MSL kernel 编译、dispatch、buffer/texture 绑定、fence 同步。

绑定路线（ADR 0003 K4 复核结论，2026-09-11）：compute 路径服务于
Composite 大半径效果与 Normalize 直方图，**不在视频渲染热路径上**——
PyObjC 每次 dispatch 的微秒级开销相对毫秒级内核成本可忽略。原生
Obj-C++ 扩展（签名/公证成本）留给 Phase 5+ 视频渲染热路径再评估。
PyObjC 惯用法全部来自 P1-01/P1-02 probe 实测
（`tools/probe_metal_capability.py`：`newLibraryWithSource` /
`newComputePipelineStateWithFunction` / `dispatchThreads_` /
`contents().as_buffer`）。

线程与阻塞契约（验收「不使用每帧全局阻塞」）：

- `dispatch()` 只是把 kernel 编码进**当前** command buffer（惰性创建），
  不提交、不等待——一次 CPU/GPU 同步点可打包多个 kernel。
- `finish()` 才 commit 并 `waitUntilCompleted()`，仅在需要读回结果的
  同步点调用；与 GL 的全局锁（GLContextManager.mutex）完全无关。
- 任何能力不足/编译失败都抛 `MetalComputeError`（.clinerules：显式
  诊断，不静默降级）。`is_supported()` 供调用方做可用性探测。

命令队列约定：

- 每个 context 持有**一条** `MTLCommandQueue`；`_current_cb` 是惰性创建
  的当前 command buffer——`dispatch()` 编码进去，`submit()`/`finish()`
  取走并 commit，取走后下一次 dispatch 再开新 buffer。
- 编码全程持 `self._lock`：同一 buffer 上并发挂两个 encoder、或提交
  他人仍在编码的 buffer，都是 Metal 校验错误（编码是微秒级，锁粒度
  无性能影响）。
- `read_texture` 的 blit 同步刻意用**独立** command buffer：blit 必须
  排在尚未提交的 compute 编码之后，不能复用 `_current_cb`。

autoreleasepool 约定（PyObjC，AUD-1402）：

- `commandBuffer()` / `computeCommandEncoder()` / library 等由非
  new/copy 前缀 selector 返回的对象进入 autorelease 池。**PyObjC 不会
  在无 runloop 的工作线程（QThread）上代建池**——实测（2026-10-01，
  pyobjc 12.1）pool-less 线程上 2000 次 1MB autoreleased NSData 的
  RSS 线性增长约 2GB，无任何回收；早期"每次 Python→ObjC 调用边界
  自动清池"的假设不成立。
- 因此所有产生 PyObjC 对象的公共入口（dispatch/submit/read_texture/
  编译/纹理与 buffer 创建）都包在 `_autoreleasepool()`（即
  `objc.autorelease_pool()`）里；非 macOS 宿主无 objc 时退化为空操作。
- 对象存活性只由 Python 侧强引用锚定（`self._current_cb`、
  `self._kernels`、`self._libraries` 及调用方局部变量）——池清空只
  释放那些唯一所有者是 autorelease 机制的对象；绝不能依赖 autorelease
  延长寿命（encoder 用完即 `endEncoding` 丢弃是唯一例外——它不再被
  引用是有意的）。
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple

import numpy as np

try:
    import objc  # pyobjc-core（macOS）
except ImportError:  # 非 macOS 测试宿主：Metal 本就不可用
    objc = None  # type: ignore[assignment]


@contextmanager
def _autoreleasepool() -> Iterator[None]:
    """在块结束时清空 autorelease 池（AUD-1402，见模块 docstring）。

    无 objc 的宿主上是空操作（fake Metal 模块的语义测试环境）。
    """
    if objc is None:
        yield
        return
    with objc.autorelease_pool():
        yield


class MetalComputeError(RuntimeError):
    """Metal compute 能力不足、编译失败或 GPU 提交错误（显式诊断）。"""


class MetalKernel:
    """已编译的 MSL compute kernel（持有 MTLComputePipelineState）。"""

    def __init__(self, name: str, pipeline_state: Any) -> None:
        """持有已编译的 compute pipeline state。

        Args:
            name: kernel 函数名（与 MSL 源中 ``kernel void <name>`` 同名，
                同时是 `MetalComputeContext.compile_kernel` 的缓存键）。
            pipeline_state: PyObjC 的 MTLComputePipelineState。
        """
        self.name = name
        self._pso = pipeline_state

    @property
    def max_threads_per_group(self) -> int:
        """单个 threadgroup 允许的最大线程数（dispatch 分组上限）。"""
        return int(self._pso.maxTotalThreadsPerThreadgroup())

    @property
    def thread_execution_width(self) -> int:
        """SIMD 组宽度（GPU 一个 cycle 处理的线程数，诊断/调优用）。"""
        return int(self._pso.threadExecutionWidth())

    @property
    def pipeline_state(self) -> Any:
        """底层 MTLComputePipelineState（编码时 setComputePipelineState_）。"""
        return self._pso


class MetalComputeContext:
    """Metal 设备 + 命令队列 + kernel 缓存的最小 compute 上下文。

    生命周期：每个使用方（如 `ComputeShaderManager` 的 Metal 分支、
    `HistogramManager` 的 Metal 分支）持有一个实例即可；MTLDevice 是
    进程级单例对象，多实例共享同一设备没有额外成本。
    """

    _support_cache: Optional[Tuple[bool, str]] = None
    _support_lock = threading.Lock()

    def __init__(self, device: Any = None) -> None:
        """
        Args:
            device: 注入 MTLDevice（测试用）；默认取系统默认设备。
        """
        import Metal  # pyobjc-framework-Metal（§9.2 固定 >=12.1,<13）

        self._metal = Metal
        if device is None:
            device = Metal.MTLCreateSystemDefaultDevice()
        if device is None:
            raise MetalComputeError(
                "Metal compute 不可用：MTLCreateSystemDefaultDevice 返回 None"
                "（无 Metal 硬件或被系统策略禁用）"
            )
        self._device = device
        self._queue = device.newCommandQueue()
        if self._queue is None:
            raise MetalComputeError("Metal compute 不可用：无法创建命令队列")
        self._kernels: Dict[str, MetalKernel] = {}
        self._libraries: Dict[str, Any] = {}
        self._current_cb: Any = None
        self._last_gpu_time_ms: Optional[float] = None
        # AUD-1401: dispatch 异常丢弃了整条已编码 kernel 链时置位，
        # 让随后的 submit() 显式报错而不是静默返回 None。
        self._dropped_work: bool = False
        self._lock = threading.Lock()

    @property
    def device(self) -> Any:
        """底层 MTLDevice（PyObjC 对象）。供 interop 层（如
        CVMetalTextureCache 创建）取设备指针；不要在 UI 线程用它编码命令。"""
        return self._device

    # ------------------------------------------------------------------
    # 可用性探测
    # ------------------------------------------------------------------

    @classmethod
    def is_supported(cls) -> bool:
        """本机 Metal compute 是否可用（进程级缓存，见
        `availability_reason`）。任何线程可调；不创建 context。"""
        ok, _ = cls.availability_reason()
        return ok

    @classmethod
    def availability_reason(cls) -> Tuple[bool, str]:
        """返回 (是否可用, 不可用原因)。结果进程级缓存。"""
        with cls._support_lock:
            if cls._support_cache is not None:
                return cls._support_cache
            try:
                import Metal  # noqa: F401
            except ImportError:
                cls._support_cache = (False, "pyobjc-framework-Metal 未安装")
                return cls._support_cache
            try:
                if Metal.MTLCreateSystemDefaultDevice() is None:
                    cls._support_cache = (
                        False,
                        "无 Metal 硬件（MTLCreateSystemDefaultDevice 为 None）",
                    )
                else:
                    cls._support_cache = (True, "")
            except Exception as exc:  # 虚拟机等环境的未知故障
                cls._support_cache = (False, f"Metal 设备探测失败: {exc}")
            return cls._support_cache

    # ------------------------------------------------------------------
    # kernel 编译与缓存
    # ------------------------------------------------------------------

    def compile_kernel(self, name: str, msl_source: str) -> MetalKernel:
        """编译 MSL 并缓存 compute pipeline（同名重复编译返回缓存）。"""
        source_key = hashlib.sha256(msl_source.encode("utf-8")).hexdigest()
        with self._lock, _autoreleasepool():
            cached = self._kernels.get(name)
            if cached is not None:
                return cached
            library = self._libraries.get(source_key)
            if library is None:
                library, err = self._device.newLibraryWithSource_options_error_(
                    msl_source, None, None
                )
                if library is None:
                    detail = ""
                    if err is not None:
                        detail = getattr(
                            err, "localizedDescription", lambda: str(err))()
                    raise MetalComputeError(
                        f"MSL 编译失败（{name}）: {detail}")
                self._libraries[source_key] = library
            function = library.newFunctionWithName_(name)
            if function is None:
                raise MetalComputeError(
                    f"MSL 编译成功但找不到 kernel 函数 '{name}'"
                )
            pso, perr = self._device.newComputePipelineStateWithFunction_error_(
                function, None
            )
            if pso is None:
                detail = ""
                if perr is not None:
                    detail = getattr(
                        perr, "localizedDescription", lambda: str(perr))()
                raise MetalComputeError(
                    f"compute pipeline 创建失败（{name}）: {detail}")
            kernel = MetalKernel(name, pso)
            self._kernels[name] = kernel
            return kernel

    def get_cached_kernel(self, name: str) -> Optional[MetalKernel]:
        """取已编译 kernel；未编译过返回 ``None``（不触发编译）。"""
        with self._lock:
            return self._kernels.get(name)

    @property
    def has_dropped_work(self) -> bool:
        """上次 dispatch 异常是否丢弃了已编码工作（AUD-1401，诊断用）。

        True 表示曾有一条 kernel 链被丢弃且尚未被任何一次 submit
        消费（成功提交新链 / 抛出显式错误都会清位）。
        """
        with self._lock:
            return self._dropped_work

    # ------------------------------------------------------------------
    # buffer / texture 资源
    # ------------------------------------------------------------------

    def create_buffer_from_numpy(self, arr: np.ndarray) -> Any:
        """用 numpy 数组内容创建共享存储 MTLBuffer（零额外拷贝语义）。"""
        data = np.ascontiguousarray(arr)
        byte_length = data.nbytes
        with _autoreleasepool():
            buf = self._device.newBufferWithBytes_length_options_(
                data.tobytes(),
                byte_length,
                self._metal.MTLResourceStorageModeShared,
            )
        if buf is None:
            raise MetalComputeError(f"MTLBuffer 创建失败（{byte_length} 字节）")
        return buf

    def create_buffer(self, byte_length: int) -> Any:
        """创建未初始化的共享存储 MTLBuffer（输出缓冲用）。"""
        with _autoreleasepool():
            buf = self._device.newBufferWithLength_options_(
                int(byte_length),
                self._metal.MTLResourceStorageModeShared,
            )
        if buf is None:
            raise MetalComputeError(f"MTLBuffer 创建失败（{byte_length} 字节）")
        return buf

    def create_private_buffer(self, byte_length: int) -> Any:
        """创建仅 GPU 可访问的 MTLBuffer（中间计算资源用）。"""
        with _autoreleasepool():
            buf = self._device.newBufferWithLength_options_(
                int(byte_length),
                self._metal.MTLResourceStorageModePrivate,
            )
        if buf is None:
            raise MetalComputeError(
                f"私有 MTLBuffer 创建失败（{byte_length} 字节）")
        return buf

    def read_buffer(self, buffer: Any, byte_length: Optional[int] = None) -> np.ndarray:
        """读回共享存储 buffer 内容（uint8 数组，由调用方重解释）。"""
        length = int(buffer.length()) if byte_length is None else int(byte_length)
        contents = buffer.contents()
        return np.frombuffer(contents.as_buffer(length), dtype=np.uint8, count=length)

    def write_buffer(
        self,
        buffer: Any,
        data: np.ndarray,
        *,
        offset: int = 0,
    ) -> None:
        """把 CPU 参数/输入写入可 CPU 访问的 ``MTLBuffer``。

        这是 ``read_buffer`` 的有意反向 API，而不是通过读回接口取得一个
        ``ndarray`` 再修改。后者会把上传错误地记录为 GPU→CPU 像素搬运，
        在独显的 Managed storage 上还会让读回语义掩盖真实的同步边界。

        该方法只允许 Shared/Managed buffer。GPU-only Private buffer 必须
        由 kernel 产生，不能从 CPU 侧写入；调用方应使用
        :meth:`create_buffer_from_numpy` 创建 staging/参数 buffer。
        """
        if int(offset) < 0:
            raise ValueError("MTLBuffer 写入 offset 不可为负")
        payload = np.ascontiguousarray(data).view(np.uint8).reshape(-1)
        end = int(offset) + int(payload.nbytes)
        if end > int(buffer.length()):
            raise ValueError(
                f"MTLBuffer 写入越界: offset={offset} bytes={payload.nbytes} "
                f"length={buffer.length()}"
            )
        try:
            contents = buffer.contents()
            destination = contents.as_buffer(end)
            destination[int(offset):end] = payload.tobytes()
            # 独显 macOS 的 Managed buffer 需要显式标记 CPU 修改；Shared
            # buffer 没有该 selector 的必要，但 PyObjC/Fake 都允许安全探测。
            did_modify = getattr(buffer, "didModifyRange_", None)
            if callable(did_modify):
                did_modify((int(offset), int(payload.nbytes)))
        except Exception as exc:
            raise MetalComputeError(f"MTLBuffer CPU 写入失败: {exc}") from exc

    def create_texture_from_numpy(
        self,
        arr: np.ndarray,
        pixel_format: Any,
        width: int,
        height: int,
    ) -> Any:
        """从 numpy 数组创建 MTLTexture（CPU staging 桥的输入侧）。

        MTLDevice 没有「带初始字节」的纹理创建入口（那套 selector 不存在，
        早期版本误写为 newTextureWithDescriptor_slice_bytes_bytesPerRow_
        ——从未被调用过所以未暴露）；正确路径是先建空纹理再
        ``replaceRegion`` 上传。存储模式按设备选：Shared 纹理仅在统一
        内存（Apple Silicon）合法，独显 macOS 必须用 Managed。
        """
        data = np.ascontiguousarray(arr)
        with _autoreleasepool():
            desc = self._metal.MTLTextureDescriptor.texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                pixel_format, int(width), int(height), False
            )
            desc.setUsage_(self._metal.MTLTextureUsageShaderRead)
            if bool(self._device.hasUnifiedMemory()):
                desc.setStorageMode_(self._metal.MTLStorageModeShared)
            else:
                desc.setStorageMode_(self._metal.MTLStorageModeManaged)
            tex = self._device.newTextureWithDescriptor_(desc)
            if tex is None:
                raise MetalComputeError(
                    f"MTLTexture 创建失败（{width}x{height} fmt={pixel_format}）"
                )
            tex.replaceRegion_mipmapLevel_withBytes_bytesPerRow_(
                self._metal.MTLRegionMake2D(0, 0, int(width), int(height)),
                0,
                data.tobytes(),
                int(data.strides[0]),
            )
        return tex

    def create_texture_2d(
        self,
        pixel_format: Any,
        width: int,
        height: int,
        shader_write: bool = False,
    ) -> Any:
        """创建空 MTLTexture（ShaderRead [+ ShaderWrite]）。

        存储模式与 create_texture_from_numpy 同规则：统一内存 Shared，
        独显 Managed。供 interop/转换链路分配输出纹理（如 YUV→RGBA16F）。
        """
        with _autoreleasepool():
            desc = self._metal.MTLTextureDescriptor.texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                pixel_format, int(width), int(height), False
            )
            usage = self._metal.MTLTextureUsageShaderRead
            if shader_write:
                usage |= self._metal.MTLTextureUsageShaderWrite
            desc.setUsage_(usage)
            if bool(self._device.hasUnifiedMemory()):
                desc.setStorageMode_(self._metal.MTLStorageModeShared)
            else:
                desc.setStorageMode_(self._metal.MTLStorageModeManaged)
            tex = self._device.newTextureWithDescriptor_(desc)
        if tex is None:
            raise MetalComputeError(
                f"MTLTexture 创建失败（{width}x{height} fmt={pixel_format}）"
            )
        return tex

    # ------------------------------------------------------------------
    # dispatch 与同步
    # ------------------------------------------------------------------

    def dispatch(
        self,
        kernel: MetalKernel,
        *,
        buffers: Optional[Dict[int, Any]] = None,
        textures: Optional[Dict[int, Any]] = None,
        threads: Tuple[int, int, int],
        group: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        """把一次 kernel 调用编码进当前 command buffer（不提交）。

        Args:
            kernel: `compile_kernel` 产出的 kernel。
            buffers: {index: MTLBuffer} 绑定。
            textures: {index: MTLTexture} 绑定。
            threads: 线程网格 (w, h, d)。
            group: 线程组大小；默认 1D 取 max，2D/3D 用 16×16×1 裁剪到
                kernel 上限。
        """
        threads = (int(threads[0]), int(threads[1]), int(threads[2]))
        if group is None:
            max_t = kernel.max_threads_per_group
            if threads[1] <= 1 and threads[2] <= 1:
                group = (min(threads[0], max_t), 1, 1)
            else:
                gx = max(1, min(16, max_t // 16))
                group = (gx, max(1, max_t // gx), 1)
        group = (int(group[0]), int(group[1]), int(group[2]))

        # 编码全程持锁：同一个 command buffer 上并发挂两个 encoder、或
        # finish() 提交他人仍在编码的 buffer，都是 Metal 校验错误。编码
        # 本身是微秒级，锁粒度无性能影响。
        with self._lock, _autoreleasepool():
            if self._current_cb is None:
                self._current_cb = self._queue.commandBuffer()
            cb = self._current_cb
            try:
                enc = cb.computeCommandEncoder()
                enc.setComputePipelineState_(kernel.pipeline_state)
                for index, buf in sorted((buffers or {}).items()):
                    enc.setBuffer_offset_atIndex_(buf, 0, int(index))
                for index, tex in sorted((textures or {}).items()):
                    enc.setTexture_atIndex_(tex, int(index))
                size = self._metal.MTLSizeMake(*threads)
                tg = self._metal.MTLSizeMake(*group)
                enc.dispatchThreads_threadsPerThreadgroup_(size, tg)
                enc.endEncoding()
            except Exception:
                # 丢弃半编码的 buffer：未 endEncoding 的 encoder 之后无法
                # 再挂新 encoder，提交会触发校验错误——宁可弃置不提交。
                self._current_cb = None
                # AUD-1401：buffer 里已成功编码的前序 kernel 调用随之一并
                # 丢失。记录丢弃事实，让随后的 submit() 显式报错，而不是
                # 静默返回 None 冒充"没有 pending 工作"（调用方若只重试
                # 失败的那个 kernel，输出会缺前序计算且无任何信号）。
                self._dropped_work = True
                raise

    def submit(
        self,
        *,
        completion: Any = None,
        wait: bool = False,
    ) -> Any:
        """提交当前 command buffer，可选 completion handler。

        ``wait=False`` 是生产导出路径使用的异步提交语义：提交后立即把
        控制权交还调用方，资源只能在 completion handler 中归还。
        ``wait=True`` 保留给诊断/兼容调用方；它不应出现在逐帧热循环。
        返回已提交的 command buffer；没有待提交工作时返回 ``None``——
        例外是之前 dispatch 异常丢弃过已编码 kernel 链（AUD-1401），
        此时显式抛 `MetalComputeError` 而不是返回 None，调用方必须
        重新编码整条链（"无工作"与"工作被丢弃"不再混同）。
        """
        with self._lock:
            cb = self._current_cb
            self._current_cb = None
            dropped = self._dropped_work
            # 无论走哪条路径，本次 submit 都消费掉丢弃标记：
            # - 有 cb：调用方在 drop 异常后重新编码了整条链，新链取代
            #   被丢弃的旧链；
            # - 无 cb 且 dropped：下面抛出的显式错误本身就是信号，一
            #   次即清，避免把 context 永久毒化。
            self._dropped_work = False
        if cb is None:
            if dropped:
                raise MetalComputeError(
                    "dispatch 异常已丢弃整条已编码的 kernel 链；"
                    "必须重新编码整条链后再提交（不能只补发单个 kernel）"
                )
            return None
        with _autoreleasepool():
            # AUD-1403: _last_gpu_time_ms 是跨线程诊断数据（get_stats /
            # last_gpu_time_ms 可从其他线程读取），读写一律在 _lock 内，
            # 避免并发 submit 时观测到别的 buffer 的时间或撕裂值。
            with self._lock:
                self._last_gpu_time_ms = None
            if completion is not None:
                add_handler = getattr(cb, "addCompletedHandler_", None)
                if not callable(add_handler):
                    raise MetalComputeError(
                        "MTLCommandBuffer 不支持 completion handler；"
                        "不能启用异步 surface 回收"
                    )
                add_handler(completion)
            cb.commit()
            if wait:
                cb.waitUntilCompleted()
                with self._lock:
                    self._last_gpu_time_ms = self._measure_gpu_time_ms(cb)
            error = cb.error()
            # 非阻塞提交时 command buffer 尚未完成，PyObjC 通常返回 None；若
            # 驱动在 commit 时已同步报告错误，仍立即失败闭环。
            if error is not None and (wait or getattr(cb, "status", lambda: 0)() >= 4):
                detail = getattr(error, "localizedDescription", lambda: str(error))()
                raise MetalComputeError(f"Metal command buffer 执行失败: {detail}")
            return cb

    @staticmethod
    def _measure_gpu_time_ms(command_buffer: Any) -> Optional[float]:
        """Read Metal's measured GPU interval when the platform exposes it.

        Wall-clock time is intentionally not substituted here: it includes
        command encoding and scheduler latency, so it is not valid GPU
        evidence for the benchmark report.
        """
        start_fn = getattr(command_buffer, "gpuStartTime", None)
        end_fn = getattr(command_buffer, "gpuEndTime", None)
        if not callable(start_fn) or not callable(end_fn):
            return None
        try:
            start = float(start_fn())
            end = float(end_fn())
        except (TypeError, ValueError, RuntimeError):
            return None
        if start <= 0.0 or end < start:
            return None
        return (end - start) * 1000.0

    def finish(self) -> None:
        """提交当前 command buffer 并等待完成（诊断/显式同步点）。"""
        self.submit(wait=True)

    def read_texture(
        self,
        texture: Any,
        width: int,
        height: int,
        dtype: Any = None,
        region_width: Optional[int] = None,
    ) -> "np.ndarray":
        """CPU 读回一张 kernel 写入的纹理（Managed 显存同步安全）。

        独显上 kernel 对 Managed 纹理的写入不会自动落到 CPU 可见副本，
        必须先经 blit ``synchronizeResource`` 再 ``getBytes``；Apple
        Silicon 的 Shared 纹理上该同步是 no-op，语义一致。仅用于验证/
        诊断路径（如 T-P0-31 kernel 对拍），不在逐帧热循环使用。

        Args:
            texture: MTLTexture（或等价 PyObjC 代理）。
            width: 返回数组一行要填充的元素数（决定 bytes_per_row）。
            height: 读取行数。
            dtype: 返回 ndarray 的元素类型；默认 uint8，可传 uint16。
            region_width: 实际读取的纹理像素宽；交错格式（如 RG8）下
                元素数是像素数的两倍，region 必须用像素宽。默认 = width。

        Returns:
            形状 ``(height, width)`` 的 ndarray（元素宽度 = dtype）。
        """
        itemsize = np.dtype(dtype or np.uint8).itemsize
        row_bytes = int(width) * itemsize
        region_w = int(region_width if region_width is not None else width)
        # 独立 command buffer：当前若挂着未提交的编码（compute pass），
        # blit 必须排在其后完成，不能复用 self._current_cb。
        with _autoreleasepool():
            cb = self._queue.commandBuffer()
            blit = cb.blitCommandEncoder()
            blit.synchronizeResource_(texture)
            blit.endEncoding()
            cb.commit()
            cb.waitUntilCompleted()
            region = self._metal.MTLRegionMake2D(0, 0, region_w, int(height))
            raw = bytearray(row_bytes * int(height))
            texture.getBytes_bytesPerRow_fromRegion_mipmapLevel_(
                raw, row_bytes, region, 0)
        return np.frombuffer(raw, dtype=dtype or np.uint8).reshape(
            height, width)

    @property
    def last_gpu_time_ms(self) -> Optional[float]:
        """Measured GPU execution time of the most recent waited submission.

        AUD-1403: 读取走 submit 同一把锁，避免与并发 submit 的写入竞态。
        """
        with self._lock:
            return self._last_gpu_time_ms

    def dispatch_sync(
        self,
        kernel: MetalKernel,
        *,
        buffers: Optional[Dict[int, Any]] = None,
        textures: Optional[Dict[int, Any]] = None,
        threads: Tuple[int, int, int],
        group: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        """单 kernel 的便捷路径：dispatch + finish（编码与提交同一调用）。"""
        self.dispatch(kernel, buffers=buffers, textures=textures,
                      threads=threads, group=group)
        self.finish()

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    @property
    def device_name(self) -> str:
        """MTLDevice 名称（如 'Apple M2 Pro'，诊断/日志用）。"""
        return str(self._device.name())

    @property
    def device_identity(self) -> str:
        """Process-stable identity shared by Metal producer/consumer seams."""
        try:
            import objc

            pointer = int(objc.pyobjc_id(self._device))
        except Exception:
            pointer = id(self._device)
        return f"metal:{self.device_name}:{pointer:x}"

    def get_stats(self) -> Dict[str, Any]:
        """诊断快照：backend 标识、设备名、已缓存 kernel 名与最近 GPU 时间。"""
        with self._lock:
            last_gpu_time_ms = self._last_gpu_time_ms
        return {
            "backend": "metal_compute",
            "device": self.device_name,
            "cached_kernels": sorted(self._kernels.keys()),
            "last_gpu_time_ms": last_gpu_time_ms,
        }
