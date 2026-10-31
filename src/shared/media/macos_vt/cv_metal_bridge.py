"""CVPixelBuffer → CVMetalTextureCache → MTLTexture 零拷贝桥（计划 Phase 5，P5-01/02/05）。

对齐既有 ``iosurface_gl_bridge.py`` 的 GL 侧零拷贝桥：同一 VT 解码
（``vt_session``，ctypes 回调），interop 换成 CVMetalTextureCache，产物是
MTLTexture plane（``GpuPlaneResource(api='metal')``），供 Metal 渲染/转换
消费。仅适用于 **VT 真有硬解** 的格式（H.264/HEVC 的 NV12/P010）；本机
ProRes 无硬解，不存在可零拷贝的 surface（plan §0.1），不做伪装。

FFI 决策（plan P1-04，已实测）：CoreVideo interop 沿 ctypes 层声明，
不在解码回调边界混用 PyObjC。所有 CVReturn 必须 ``== 0`` 显式判断——
``kCVReturnInvalidArgument``(-6661) 在 Python 里为真值，``if rc`` 会把
失败当成功（probe 因此误报过一轮，见 tools/probe_metal_capability.py）。

生命周期契约（plan §7.2 / P5-05）：

- 桥对每个导入的 CVPixelBuffer 显式 ``CFRetain``，release 回调
  （fence 安全后由消费方调用）里 ``CFRelease``——exactly once。
- ``CVMetalTextureRef`` 是 +1 CF 引用，release 回调里 ``CFRelease``；
  它存活期间 CVMetalTextureCache 保证 MTLTexture 有效。
- ``MetalVideoFrameRing`` 提供有界 in-flight 登记：command completion
  （``MetalComputeContext.finish()`` 之后调用 ``on_gpu_complete()``）前
  不释放任何帧。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from typing import TYPE_CHECKING, Any, Optional, Sequence

from src.shared.media.gpu_frame import (
    GpuFramePlane,
    GpuFrameRelease,
    GpuPlaneResource,
)
from src.shared.media.frame_path import DecodePath

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    import numpy as np

logger = logging.getLogger(__name__)

CV_FRAMEWORK = "/System/Library/Frameworks/CoreVideo.framework/CoreVideo"
CF_FRAMEWORK = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

# MTLPixelFormat 一律从 PyObjC Metal 模块解析（头文件权威值）。**禁止
# 手写魔数**：手写稿曾把 RG8Unorm 误写为 70（实为 RGBA8Unorm 的值，
# RG8Unorm 实为 30），转换门禁以错误色度采样失败的形式暴露了它。
# R8Unorm=10 / R16Unorm=20 / BGRA8Unorm=80 等少数值恰好手写对了。
def mtl_pixel_format(name: str) -> int:
    """resource_format 名 → MTLPixelFormat 原始值（Metal 模块权威值）。"""
    import Metal

    mapping = {
        "r8unorm": Metal.MTLPixelFormatR8Unorm,
        "rg8unorm": Metal.MTLPixelFormatRG8Unorm,
        "r16unorm": Metal.MTLPixelFormatR16Unorm,
        "rg16unorm": Metal.MTLPixelFormatRG16Unorm,
        "rgba16float": Metal.MTLPixelFormatRGBA16Float,
    }
    try:
        return mapping[name]
    except KeyError:
        raise ValueError(
            f"未知 MTLPixelFormat 名 {name!r}（支持: {', '.join(sorted(mapping))}）"
        ) from None

# CVPixelBuffer FourCC（kCVPixelFormatType_*，按字节序即字面量）
_FOURCC_420V = int.from_bytes(b"420v", "big")  # NV12 video range
_FOURCC_420F = int.from_bytes(b"420f", "big")  # NV12 full range
_FOURCC_X420 = int.from_bytes(b"x420", "big")  # P010 video range（实测：kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange FourCC 是 x420，P010 是 codec 术语）
_FOURCC_X420F = int.from_bytes(b"xf20", "big")  # P010 full range (CoreVideo: kCVPixelFormatType_420YpCbCr10BiPlanarFullRange)

# format name → (FourCC 集合, [plane 描述])。plane: (component, mtl 格式名,
# resource_format, x_div, y_div)。仅 4:2:0 双平面（VT 硬解输出的 NV12/P010）；
# v210/422 无 10-bit 直通（ADR 0003），不在支持矩阵里伪装。
_PLANE_LAYOUTS = {
    "nv12": (
        {_FOURCC_420V, _FOURCC_420F},
        [
            ("Y", "r8unorm", "r8unorm", 1, 1),
            ("UV", "rg8unorm", "rg8unorm", 2, 2),
        ],
    ),
    "p010": (
        {_FOURCC_X420, _FOURCC_X420F},
        [
            ("Y", "r16unorm", "r16unorm", 1, 1),
            ("UV", "rg16unorm", "rg16unorm", 2, 2),
        ],
    ),
}

_CV_METAL_BRIDGE_IMPLEMENTED = False
_cv: Optional[ctypes.CDLL] = None
_cf: Optional[ctypes.CDLL] = None


def _load_frameworks() -> bool:
    """声明 CoreVideo/CoreVideo 的 ctypes 符号；缺符号视为未实现。"""
    global _cv, _cf, _CV_METAL_BRIDGE_IMPLEMENTED
    if _CV_METAL_BRIDGE_IMPLEMENTED:
        return True
    try:
        V, U, Z = ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t
        cv = ctypes.CDLL(CV_FRAMEWORK)
        cf = ctypes.CDLL(CF_FRAMEWORK)

        cv.CVMetalTextureCacheCreate.restype = ctypes.c_int32
        cv.CVMetalTextureCacheCreate.argtypes = [V, V, V, V, ctypes.POINTER(V)]

        cv.CVMetalTextureCacheCreateTextureFromImage.restype = ctypes.c_int32
        cv.CVMetalTextureCacheCreateTextureFromImage.argtypes = [
            V, V, V, V, U, Z, Z, Z, ctypes.POINTER(V),
        ]

        cv.CVMetalTextureGetTexture.restype = V
        cv.CVMetalTextureGetTexture.argtypes = [V]

        cv.CVPixelBufferGetWidth.restype = Z
        cv.CVPixelBufferGetWidth.argtypes = [V]
        cv.CVPixelBufferGetHeight.restype = Z
        cv.CVPixelBufferGetHeight.argtypes = [V]
        cv.CVPixelBufferGetPixelFormatType.restype = U
        cv.CVPixelBufferGetPixelFormatType.argtypes = [V]
        cv.CVPixelBufferLockBaseAddress.restype = ctypes.c_int32
        cv.CVPixelBufferLockBaseAddress.argtypes = [V, U]
        cv.CVPixelBufferUnlockBaseAddress.restype = ctypes.c_int32
        cv.CVPixelBufferUnlockBaseAddress.argtypes = [V, U]
        cv.CVPixelBufferGetBaseAddress.restype = V
        cv.CVPixelBufferGetBaseAddress.argtypes = [V]
        cv.CVPixelBufferGetBytesPerRow.restype = Z
        cv.CVPixelBufferGetBytesPerRow.argtypes = [V]
        cv.CVPixelBufferGetPlaneCount.restype = Z
        cv.CVPixelBufferGetPlaneCount.argtypes = [V]
        cv.CVPixelBufferGetBaseAddressOfPlane.restype = V
        cv.CVPixelBufferGetBaseAddressOfPlane.argtypes = [V, Z]
        cv.CVPixelBufferGetBytesPerRowOfPlane.restype = Z
        cv.CVPixelBufferGetBytesPerRowOfPlane.argtypes = [V, Z]

        # 注意：CVBufferRetain/CVBufferRelease 在现代 SDK 是 CV_INLINE 宏
        #（无导出符号），引用计数直接走 CFRetain/CFRelease——CVPixelBuffer
        # 是 CF 类型，语义等价。
        cf.CFRetain.restype = None
        cf.CFRetain.argtypes = [V]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [V]

        _cv, _cf = cv, cf
        _CV_METAL_BRIDGE_IMPLEMENTED = True
    except Exception as exc:  # 非 macOS / 系统库缺失
        logger.info(f"CVMetal bridge 依赖加载失败: {exc}")
        _cv, _cf = None, None
    return _CV_METAL_BRIDGE_IMPLEMENTED


def is_cv_metal_bridge_implemented() -> bool:
    """系统库符号齐全（不代表设备可用——设备可用性走 MetalComputeContext）。"""
    return _load_frameworks()


def cv_metal_bridge_available() -> tuple[bool, str]:
    """(是否可用, 不可用原因)：桥符号 + Metal 设备 二者齐备。"""
    if not _load_frameworks():
        return False, "CoreVideo/CoreFoundation 符号加载失败（非 macOS？）"
    from src.shared.gpu.metal.context import MetalComputeContext

    return MetalComputeContext.availability_reason()


def _pb_ptr(pixel_buffer: Any) -> ctypes.c_void_p:
    """CVPixelBuffer（PyObjC 对象或裸指针 int）→ c_void_p。

    None 返回空指针——绝不把垃圾地址传给 CoreVideo（pyobjc_id(None)
    返回的是 PyObject 地址，传给 CV 函数会段错误而非可诊断的失败）。
    """
    if pixel_buffer is None:
        return ctypes.c_void_p(0)
    if isinstance(pixel_buffer, int):
        return ctypes.c_void_p(pixel_buffer)
    import objc

    return ctypes.c_void_p(objc.pyobjc_id(pixel_buffer))


def unlock_pixel_buffer_base_address(pixel_buffer: Any) -> None:
    _load_frameworks()
    _cv.CVPixelBufferUnlockBaseAddress(_pb_ptr(pixel_buffer), 0)


def lock_pixel_buffer_planes(pixel_buffer: Any) -> list[tuple[int, int]]:
    """锁定并返回各 plane 的 ``(base_address, bytes_per_row)``。

    双平面格式（NV12/P010）返回 2 项；packed 格式返回 1 项。
    测试/诊断用（写入合成图案验证零拷贝路径）。
    """
    _load_frameworks()
    ptr = _pb_ptr(pixel_buffer)
    rc = int(_cv.CVPixelBufferLockBaseAddress(ptr, 0))
    if rc != 0:  # `== 0` 判断（-6661 真值坑）
        raise RuntimeError(f"CVPixelBufferLockBaseAddress rc={rc}")
    try:
        count = int(_cv.CVPixelBufferGetPlaneCount(ptr))
        if count > 0:
            planes = []
            for i in range(count):
                base = int(_cv.CVPixelBufferGetBaseAddressOfPlane(ptr, i) or 0)
                bpr = int(_cv.CVPixelBufferGetBytesPerRowOfPlane(ptr, i))
                if base == 0:
                    raise RuntimeError(f"plane{i} base address 为空")
                planes.append((base, bpr))
            return planes
        base = int(_cv.CVPixelBufferGetBaseAddress(ptr) or 0)
        if base == 0:
            raise RuntimeError("CVPixelBufferGetBaseAddress 返回空指针")
        return [(base, int(_cv.CVPixelBufferGetBytesPerRow(ptr)))]
    except Exception:
        _cv.CVPixelBufferUnlockBaseAddress(ptr, 0)
        raise


class MetalVideoFrameRing:
    """有界 in-flight 帧登记表（P5-05）：command completion 前不释放。

    语义（与 ``MetalComputeContext.finish()`` 的全屏障语义对齐）：

    - ``track``：登记帧（release 契约由调用方持有的 ``GpuFrameRelease``
      提供，ring 只负责时机）。满员返回 False——调用方必须先
      ``finish() + on_gpu_complete() + release_completed()`` 再导入新帧。
    - ``mark_submitted``：已 track 的帧都有 GPU 命令在途（dispatch 后调用）。
    - ``on_gpu_complete``：completion handler 调用——指定命令的帧已完成。
    - ``release_completed``：释放「已提交且已完成」的条目。

    exactly-once 由 ``GpuFrameRelease`` 自身保证，ring 重复触发无害。
    """

    def __init__(self, max_in_flight: int = 4) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight 必须 >= 1")
        self._max = int(max_in_flight)
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        # AUD-35007：条目身份 ID（单调递增）。frame_index 只是展示值，
        # 重复 frame_index（如 seek 后重导入同一帧号）不允许跨提交互相
        # 置完成——按帧号值匹配会让旧提交的 completion 提前释放新提交
        # 仍在 GPU 上使用的表面。
        self._next_entry_id = 1

    @property
    def max_in_flight(self) -> int:
        return self._max

    @property
    def in_flight(self) -> int:
        with self._lock:
            return len(self._entries)

    def track(self, frame_index: int, release: GpuFrameRelease) -> int:
        """登记帧，返回**条目 ID**（int，>=1 真值）；满员返回 ``False``。

        AUD-35007：返回条目身份 ID，供 mark_submitted/on_gpu_complete 按
        条目（而非帧号值）精确匹配。
        """
        with self._lock:
            if len(self._entries) >= self._max:
                return False
            entry_id = self._next_entry_id
            self._next_entry_id += 1
            self._entries.append(
                {"entry_id": entry_id, "frame_index": int(frame_index),
                 "release": release, "submitted": False, "completed": False}
            )
            return entry_id

    def mark_submitted(
        self, frame_indices: Optional[Sequence[int]] = None,
    ) -> tuple[int, ...]:
        """Mark a stable set of tracked frames as belonging to one submission.

        AUD-35007：返回**条目 ID** 元组（非帧号）。completion 回调携带的
        是这组 ID，按条目身份匹配——重复 frame_index 的在途条目不会被
        其他提交的 completion 提前置完成。已被先前提交认领的条目
        （submitted=True）不再被重复认领。
        """
        with self._lock:
            selected = (
                {int(index) for index in frame_indices}
                if frame_indices is not None
                else None
            )
            claimed: list[int] = []
            for e in self._entries:
                if e["submitted"] or e["completed"]:
                    continue
                if selected is None or int(e["frame_index"]) in selected:
                    e["submitted"] = True
                    claimed.append(int(e["entry_id"]))
            return tuple(claimed)

    def _unmark_submitted(self, entry_ids: Sequence[int]) -> None:
        selected = {int(entry_id) for entry_id in entry_ids}
        with self._lock:
            for e in self._entries:
                if int(e["entry_id"]) in selected and not e["completed"]:
                    e["submitted"] = False

    def on_gpu_complete(self, entry_ids: Optional[Sequence[int]] = None) -> None:
        """Mark only frames from the completed command buffer as safe to release.

        AUD-35007：``entry_ids`` 是 :meth:`mark_submitted` 返回的条目 ID
        集，按条目身份精确匹配；``None`` 保留"全部已提交条目完成"语义
        （显式 finish() 之后调用）。
        """
        with self._lock:
            selected = (
                {int(entry_id) for entry_id in entry_ids}
                if entry_ids is not None
                else None
            )
            for e in self._entries:
                if e["submitted"] and (
                    selected is None or int(e["entry_id"]) in selected
                ):
                    e["completed"] = True

    def submit_pending(
        self,
        scheduler: Any,
        frame_indices: Optional[Sequence[int]] = None,
    ) -> Optional[Any]:
        """Submit the current Metal command buffer without a global wait.

        ``scheduler`` is a :class:`MetalSubmissionScheduler`-compatible
        object. The selected frame leases are marked completed from its
        completion callback; release itself remains explicit via
        :meth:`release_completed` on the consumer thread.
        """
        selected = self.mark_submitted(frame_indices)
        if not selected:
            return None
        try:
            submission = scheduler.submit(
                on_complete=lambda _submission, _error: self.on_gpu_complete(selected)
            )
        except Exception:
            self._unmark_submitted(selected)
            raise
        if submission is None:
            self._unmark_submitted(selected)
        return submission

    def release_completed(self) -> int:
        """释放已完成条目，返回释放数。"""
        with self._lock:
            done = [e for e in self._entries if e["completed"]]
            self._entries = [e for e in self._entries if not e["completed"]]
        for e in done:
            e["release"].release()
        return len(done)

    def release_all(self) -> int:
        """无条件释放全部条目（仅限调用方已确认 GPU 完成/放弃的场景，
        如 destroy、seek 后丢弃）。"""
        with self._lock:
            entries = list(self._entries)
            self._entries.clear()
        for e in entries:
            e["release"].release()
        return len(entries)


class CVMetalVideoBridge:
    """CVPixelBuffer → MTLTexture 导入桥（P5-01/02）。

    Usage:
        bridge = CVMetalVideoBridge(metal_ctx)
        if bridge.initialize():
            result = bridge.import_pixel_buffer(pb, frame_index, 'nv12', 8)
            if result is not None:
                planes, gpu_release = result   # GpuFramePlane(api='metal')
                # ... Metal dispatch 消费 planes，finish() 后 gpu_release.release()

    线程模型：Metal 无 context 概念，本桥可跨线程使用（与 GL 桥的
    owner_thread 限制不同）；但 command encoding 仍须单线程消费方
    （§7.1）经由 MetalComputeContext 的锁串行化。
    """

    def __init__(self, metal_ctx: Any = None, ring_size: int = 4) -> None:
        self._metal_ctx = metal_ctx
        self._ring_size = max(1, int(ring_size))
        self._cache: Optional[ctypes.c_void_p] = None
        self._initialized = False
        self._bind_count = 0
        self._fail_count = 0
        self._last_fallback_reason: Optional[str] = None
        # AUD-35005/35006：生命周期锁——initialize 的双检、destroy 的换出、
        # import 对 cache 的使用都以它互斥，杜绝跨线程首导双建
        # CVMetalTextureCache（泄漏一个 CF 对象）与 destroy/import 竞态
        # （NULL 传入 CoreVideo FFI）。
        self._state_lock = threading.RLock()
        self.ring = MetalVideoFrameRing(max_in_flight=self._ring_size)

    # -- 生命周期 -------------------------------------------------------

    def initialize(self) -> bool:
        """创建 CVMetalTextureCache。失败显式诊断（不静默）。

        AUD-35005：双检加锁——跨线程首次导入竞态会双建
        CVMetalTextureCache，其中一个 CF 对象被覆盖泄漏。
        """
        if self._initialized:
            return True
        with self._state_lock:
            if self._initialized:
                # 另一线程已完成初始化：复用其 cache，不再创建
                return True
            ok, reason = cv_metal_bridge_available()
            if not ok:
                self._last_fallback_reason = f"availability:{reason}"
                logger.warning(f"CVMetalVideoBridge: 不可用——{reason}")
                return False
            if self._metal_ctx is None:
                from src.shared.gpu.metal.context import MetalComputeContext

                try:
                    self._metal_ctx = MetalComputeContext()
                except Exception as exc:
                    self._last_fallback_reason = f"metal_ctx_init:{exc}"
                    logger.warning(f"CVMetalVideoBridge: MetalComputeContext 创建失败: {exc}")
                    return False

            import objc

            dev_ptr = ctypes.c_void_p(objc.pyobjc_id(self._metal_ctx.device))
            cache = ctypes.c_void_p(0)
            rc = int(_cv.CVMetalTextureCacheCreate(None, None, dev_ptr, None, ctypes.byref(cache)))
            if rc != 0 or not cache.value:  # `== 0` 判断（-6661 真值坑）
                self._last_fallback_reason = f"cache_create_rc:{rc}"
                logger.error(f"CVMetalVideoBridge: CVMetalTextureCacheCreate rc={rc}")
                return False
            self._cache = cache
            self._initialized = True
            logger.info(
                f"CVMetalVideoBridge: initialized (device={self._metal_ctx.device_name}, "
                f"ring={self._ring_size})"
            )
            return True

    def is_initialized(self) -> bool:
        return self._initialized

    # -- 导入 -----------------------------------------------------------

    def import_pixel_buffer(
        self,
        pixel_buffer: Any,
        frame_index: int,
        pixel_format: str = "nv12",
        bit_depth: int = 8,
        decode_path: str = DecodePath.MACOS_VT_CVMETAL_ZERO_COPY.value,
    ) -> Optional[tuple[Sequence[GpuFramePlane], GpuFrameRelease]]:
        """导入一帧，返回 ``(planes, GpuFrameRelease)``；失败返回 None。

        planes 为 ``GpuFramePlane(api='metal')``，``resource.native_handle``
        是 MTLTexture（PyObjC 代理）。调用方在 GPU 完成前不得调用 release，
        完成后 exactly-once 调用。
        """
        if not self._initialized and not self.initialize():
            self._record_failure(self._last_fallback_reason or "not_initialized")
            return None

        fmt_key = str(pixel_format or "").strip().lower()
        layout = _PLANE_LAYOUTS.get(fmt_key)
        if layout is None:
            self._record_failure(
                f"unsupported_pixel_format:{fmt_key}"
                "（Phase 5 仅支持 VT 硬解可用的 nv12/p010）"
            )
            return None
        fourccs, plane_defs = layout

        pb_ptr = _pb_ptr(pixel_buffer)
        if not pb_ptr.value:
            self._record_failure("null_pixel_buffer")
            return None

        actual_fourcc = int(_cv.CVPixelBufferGetPixelFormatType(pb_ptr))
        if actual_fourcc not in fourccs:
            got = actual_fourcc.to_bytes(4, "big")
            self._record_failure(
                f"pixel_format_mismatch:declared={fmt_key}:fourcc={got!r}"
            )
            return None

        try:
            width = int(_cv.CVPixelBufferGetWidth(pb_ptr))
            height = int(_cv.CVPixelBufferGetHeight(pb_ptr))
        except Exception as exc:
            self._record_failure(f"dimensions_error:{exc}")
            return None
        if width <= 0 or height <= 0:
            self._record_failure(f"bad_dimensions:{width}x{height}")
            return None

        _cf.CFRetain(pb_ptr)  # 桥自己的引用计数；release 回调平衡
        cv_tex_ptrs: list[ctypes.c_void_p] = []
        planes: list[GpuFramePlane] = []

        def _fail(reason: str) -> None:
            # 失败路径统一清理：已导入的 cv textures + 桥的 buffer 引用
            self._release_cv_textures(cv_tex_ptrs)
            _cf.CFRelease(pb_ptr)
            self._record_failure(reason)

        # AUD-35006：snapshot + 锁内使用——import 通过检查后 destroy()
        # 仍可能把 self._cache 换成 None，NULL 传入 CoreVideo FFI 会崩。
        # 在 _state_lock 内取局部 cache 引用并完成全部创建调用，与
        # destroy 的换出互斥。
        with self._state_lock:
            cache = self._cache
            if not self._initialized or cache is None or not cache.value:
                _cf.CFRelease(pb_ptr)
                self._record_failure("destroyed_during_import")
                return None

            try:
                import objc

                for plane_idx, (component, mtl_fmt_name, res_fmt, x_div, y_div) in enumerate(plane_defs):
                    plane_w, plane_h = width // x_div, height // y_div
                    mtl_fmt = mtl_pixel_format(mtl_fmt_name)
                    out = ctypes.c_void_p(0)
                    rc = int(_cv.CVMetalTextureCacheCreateTextureFromImage(
                        None, cache, pb_ptr, None, mtl_fmt,
                        plane_w, plane_h, plane_idx, ctypes.byref(out),
                    ))
                    if rc != 0 or not out.value:  # `== 0` 判断（-6661 真值坑）
                        _fail(f"import_plane{plane_idx}_rc:{rc}({fmt_key} {plane_w}x{plane_h})")
                        return None
                    cv_tex_ptrs.append(out)

                    mtl_ptr = _cv.CVMetalTextureGetTexture(out)
                    if not mtl_ptr:
                        _fail(f"get_texture_plane{plane_idx}_null")
                        return None
                    mtl_tex = objc.objc_object(c_void_p=mtl_ptr)
                    planes.append(GpuFramePlane(
                        component=component,
                        api="metal",
                        texture_id=0,  # 非 GL 后端：handle 走 resource.native_handle
                        target="2d",
                        width=plane_w,
                        height=plane_h,
                        internal_format=res_fmt,
                        pixel_format=f"{fmt_key}_{component}",
                        pixel_type="uint8" if mtl_fmt_name.endswith("8") else "uint16",
                        texture_unit_hint=plane_idx,
                        resource=GpuPlaneResource(
                            api="metal",
                            native_handle=mtl_tex,
                            resource_format=res_fmt,
                            width=plane_w,
                            height=plane_h,
                            ownership="borrowed",
                        ),
                    ))
            except Exception as exc:
                _fail(f"import_exception:{exc}")
                logger.exception("CVMetalVideoBridge: import failed")
                return None

        # 闭包持有：+1 cv textures（保 MTLTexture 有效）与桥的 buffer 引用
        _cv_tex_ptrs = cv_tex_ptrs
        _pb = pb_ptr

        def _release_cb() -> None:
            try:
                self._release_cv_textures(_cv_tex_ptrs)
            finally:
                _cf.CFRelease(_pb)

        gpu_release = GpuFrameRelease(
            retain_token=(tuple(cv_tex_ptrs), pixel_buffer),
            release_callback=_release_cb,
        )
        self._bind_count += 1
        return planes, gpu_release

    # -- 状态与清理 -----------------------------------------------------

    def bind_count(self) -> int:
        return self._bind_count

    def fail_count(self) -> int:
        return self._fail_count

    def last_fallback_reason(self) -> Optional[str]:
        return self._last_fallback_reason

    def destroy(self) -> None:
        """释放 cache。in-flight 帧的释放由消费方通过各自 GpuFrameRelease
        完成（destroy 不代持——避免在错误的线程/GPU 状态下释放资源）。

        AUD-35006：先在锁内换出 cache 指针（此后新 import 立即失败），
        再释放 CF 对象。import 侧在锁内使用 cache，因此不会有并发
        import 正拿着已释放的 cache 调 CoreVideo FFI。
        """
        with self._state_lock:
            cache = self._cache
            self._cache = None
            self._initialized = False
        if cache is not None and _cf is not None:
            _cf.CFRelease(cache)

    # -- 内部 -----------------------------------------------------------

    @staticmethod
    def _release_cv_textures(ptrs: list[ctypes.c_void_p]) -> None:
        for ptr in ptrs:
            if ptr and ptr.value:
                _cf.CFRelease(ptr)
        ptrs.clear()

    def _record_failure(self, reason: str) -> None:
        self._fail_count += 1
        self._last_fallback_reason = reason
        logger.warning(f"CVMetalVideoBridge: import failed: {reason}")


# ---------------------------------------------------------------------------
# P5-04：YUV → RGBA16F 转换（消费导入的 planes；benchmark/预览用）
# ---------------------------------------------------------------------------


def convert_planes_to_rgba16f(
    metal_ctx: Any,
    y_tex: Any,
    uv_tex: Any,
    params: "np.ndarray",
    width: int,
    height: int,
    out_tex: Any = None,
) -> Any:
    """编码 YUV→RGBA16F 转换 kernel。只编码不提交（与 finish 批处理语义一致）。

    Args:
        metal_ctx: MetalComputeContext
        y_tex / uv_tex: import_pixel_buffer 产出的 plane 纹理
        params: kernels_video.build_yuv_convert_params 的单元素结构化数组
        width / height: 输出（=Y plane）尺寸
        out_tex: 可选复用的输出纹理（RGBA16Float）；None 则新建
    Returns:
        输出 MTLTexture（调用方 finish() 后可读/消费）
    """
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.gpu.metal.kernels_video import METAL_YUV_TO_RGBA_MSL

    if not isinstance(metal_ctx, MetalComputeContext):
        raise TypeError("metal_ctx 必须是 MetalComputeContext")

    kernel = metal_ctx.compile_kernel("yuv_to_rgba", METAL_YUV_TO_RGBA_MSL)
    params_buf = metal_ctx.create_buffer_from_numpy(params)
    if out_tex is None:
        out_tex = metal_ctx.create_texture_2d(
            mtl_pixel_format("rgba16float"), width, height, shader_write=True
        )
    metal_ctx.dispatch(
        kernel,
        buffers={0: params_buf},
        textures={0: y_tex, 1: uv_tex, 2: out_tex},
        threads=(int(width), int(height), 1),
    )
    return out_tex


def read_texture_rgba16f(
    metal_ctx: Any, tex: Any, width: int, height: int
) -> "np.ndarray":
    """读回 RGBA16F 纹理为 (H, W, 4) float32 ndarray。

    诊断/测试路径（不是播放热路径）：finish + Managed 存储的 blit 同步。
    """
    import numpy as np

    metal_ctx.finish()
    storage_mode = int(tex.storageMode())
    if storage_mode == 1:  # MTLStorageModeManaged：GPU 写后需显式同步
        cb = metal_ctx._queue.commandBuffer()
        blit = cb.blitCommandEncoder()
        blit.synchronizeResource_(tex)
        blit.endEncoding()
        cb.commit()
        cb.waitUntilCompleted()

    bpr = width * 8  # RGBA16Float = 4×half = 8 B/px（不是 16——那是 RGBA32）
    region = metal_ctx._metal.MTLRegionMake2D(0, 0, int(width), int(height))
    out = np.zeros((int(height), bpr), dtype=np.uint8)
    # selector（PyObjC 实测）：getBytes:bytesPerRow:fromRegion:mipmapLevel:
    tex.getBytes_bytesPerRow_fromRegion_mipmapLevel_(out, bpr, region, 0)
    pixels = out.view("<f2").reshape(int(height), int(width), 4).astype(np.float32)
    return pixels
