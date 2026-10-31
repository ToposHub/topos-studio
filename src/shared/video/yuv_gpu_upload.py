"""
YUV GPU Upload Module - YUV 平面直接 GPU 上传

设计原则：
- OpenGL/ModernGL 操作只在渲染线程执行（遵守 .clinerules）
- 资源显式 release，避免 GPU 内存泄漏
- 完整类型注解，强类型设计

核心功能：
- YUV420 平面上传（Y + U + V 三个独立纹理）
- NV12 平面上传（Y + interleaved UV 两个纹理）
- P010 平面上传（10-bit NV12）
- GPU 端 YUV -> RGB 转换 shader

带宽优化：
- 4K RGB: 3840×2160×3 = 99.5 MB/frame
- 4K YUV420: 3840×2160 + 1920×1080×2 = 12.4 MB/frame
- 减少 ~87.5% PCIe 带宽

使用方式：
    # 在渲染线程初始化
    uploader = YUVGPUUploader(ctx)

    # 上传 YUV420 帧
    y_tex, u_tex, v_tex = uploader.upload_yuv420(y_plane, u_plane, v_plane, frame_index)

    # 上传 NV12 帧
    y_tex, uv_tex = uploader.upload_nv12(y_plane, uv_plane, frame_index)

    # 获取 shader 用于渲染
    shader = uploader.get_yuv_shader()
"""

from __future__ import annotations

import threading
import time
import itertools
import logging
import numpy as np
import moderngl
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass
import weakref

from .frame_queue import gpu_frame_release_thread_id

logger = logging.getLogger(__name__)


def topos_chroma_sampling_params(
    pixel_format: str,
    source_width: int,
    source_height: int,
    chroma_siting: int = 0,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return normalized UV mapping for Topos planar texture sampling.

    Topos stores 4:2:2 U/V at ``ceil(width / 2)``.  Independent texture
    coordinates still need a phase offset: siting 0 (left) and 2 (top-left)
    are half a luma sample to the right of the centered shader origin; siting
    1 is centered.  4:4:4 and GBR have no horizontal chroma phase.
    """
    if int(source_width) <= 0 or int(source_height) <= 0:
        raise ValueError("source dimensions must be positive")
    if int(chroma_siting) not in (0, 1, 2):
        raise ValueError(f"unsupported Topos chroma_siting: {chroma_siting}")
    fmt = str(pixel_format or "").lower()
    if "422" not in fmt or "gbr" in fmt or "444" in fmt:
        return (1.0, 1.0), (0.0, 0.0)
    offset_x = 0.5 / float(source_width) if int(chroma_siting) in (0, 2) else 0.0
    return (1.0, 1.0), (offset_x, 0.0)


# ================================================================
# 数据结构定义
# ================================================================

# 槽位 LRU 序号：必须严格递增，勿改回 time.time() —— Windows 上其精度约
# 15.6ms，自检与首帧上传在同一 tick 内时间戳平局时，「池满释放最旧」会
# 选中刚发出的纹理（release 后再 write 即 InvalidObject，可稳定复现）
_SLOT_ACQUIRE_SEQ = itertools.count(1)

@dataclass
class YUVTextureSlot:
    """YUV 纹理槽位"""
    texture: moderngl.Texture = None
    frame_index: int = -1
    last_used_time: float = 0.0  # 语义为 LRU 序号（严格递增，见 _SLOT_ACQUIRE_SEQ）
    width: int = 0
    height: int = 0
    components: int = 1  # 通道数 (1 for Y/U/V/A, 2 for interleaved UV)
    plane_type: str = 'y'  # 'y', 'u', 'v', 'uv', 'a'

    def is_size_match(self, width: int, height: int, components: int = 1) -> bool:
        """检查尺寸和通道数是否匹配"""
        return self.width == width and self.height == height and self.components == components

    def is_available(self) -> bool:
        """检查槽位是否可用

        槽位只有当 frame_index < 0 时才被视为空闲。
        如果 texture 为 None 但 frame_index >= 0，说明状态不一致，
        不应视为空闲（这种情况可能是初始化时的临时状态）。
        """
        return self.frame_index < 0


@dataclass
class YUVUploadStats:
    """YUV 上传统计"""
    frame_count: int = 0
    total_upload_time_ms: float = 0.0
    avg_upload_time_ms: float = 0.0
    total_bytes_uploaded: int = 0
    y_bytes: int = 0
    uv_bytes: int = 0
    bandwidth_saved_mb: float = 0.0


# ================================================================
# YUV 平面纹理池
# ================================================================

class YUVTexturePool:
    """
    YUV 平面纹理池。

    管理 Y 平面和 UV 平面的纹理复用：
    - Y 平面池：存储亮度纹理（全分辨率）
    - UV 平面池：存储色度纹理（子采样分辨率）

    特性：
    - 固定池大小，避免 GPU 内存增长
    - 优先复用匹配尺寸的空闲槽位
    - 支持 YUV420/NV12/P010 等格式
    """

    def __init__(
        self,
        ctx: moderngl.Context,
        y_pool_size: int = 5,
        uv_pool_size: int = 5,
        dtype: str = 'f2',
        filter_mode: Optional[Tuple[int, int]] = None
    ) -> None:
        """
        初始化 YUV 纹理池。

        Args:
            ctx: ModernGL 上下文（必须在渲染线程）
            y_pool_size: Y 平面池大小
            uv_pool_size: UV 平面池大小
            dtype: 纹理数据类型（'f2' = float16, 'u2' = uint16）
            filter_mode: 纹理过滤模式，默认 (LINEAR, LINEAR)。
                         R16UI 纹理必须使用 (NEAREST, NEAREST)。
        """
        self._ctx = ctx
        self._y_pool_size = y_pool_size
        self._uv_pool_size = uv_pool_size
        self._dtype = dtype
        self._filter_mode = filter_mode or (moderngl.LINEAR, moderngl.LINEAR)

        # Y 平面槽位
        self._y_slots: List[YUVTextureSlot] = []
        # UV 平面槽位（用于 NV12 interleaved 或分离的 U/V）
        self._uv_slots: List[YUVTextureSlot] = []

        # 统计
        self._stats = YUVUploadStats()

        # GC 后备释放
        self._finalizer = weakref.finalize(
            self,
            self._cleanup_on_gc,
            self._y_slots,
            self._uv_slots
        )

    def acquire_y_texture(
        self,
        width: int,
        height: int,
        frame_index: int
    ) -> Optional[moderngl.Texture]:
        """
        获取 Y 平面纹理。

        Args:
            width: Y 平面宽度
            height: Y 平面高度
            frame_index: 帧号

        Returns:
            Y 平面纹理或 None
        """
        return self._acquire_texture(
            self._y_slots,
            self._y_pool_size,
            width,
            height,
            frame_index,
            1,  # 单通道
            'y'
        )

    def acquire_uv_texture(
        self,
        width: int,
        height: int,
        frame_index: int,
        components: int = 2  # NV12 interleaved UV = 2 通道
    ) -> Optional[moderngl.Texture]:
        """
        获取 UV 平面纹理。

        Args:
            width: UV 平面宽度
            height: UV 平面高度
            frame_index: 帧号
            components: 通道数（NV12=2, 分离 U/V=1）

        Returns:
            UV 平面纹理或 None
        """
        return self._acquire_texture(
            self._uv_slots,
            self._uv_pool_size,
            width,
            height,
            frame_index,
            components,
            'uv'
        )

    def acquire_u_texture(
        self,
        width: int,
        height: int,
        frame_index: int
    ) -> Optional[moderngl.Texture]:
        """获取 U 平面纹理（分离格式）"""
        return self._acquire_texture(
            self._uv_slots,
            self._uv_pool_size,
            width,
            height,
            frame_index,
            1,
            'u'
        )

    def acquire_v_texture(
        self,
        width: int,
        height: int,
        frame_index: int
    ) -> Optional[moderngl.Texture]:
        """获取 V 平面纹理（分离格式）"""
        return self._acquire_texture(
            self._uv_slots,
            self._uv_pool_size,
            width,
            height,
            frame_index,
            1,
            'v'
        )

    def acquire_a_texture(
        self,
        width: int,
        height: int,
        frame_index: int
    ) -> Optional[moderngl.Texture]:
        """获取 Alpha 平面纹理（YUVA 第四平面，全分辨率）。

        Alpha 与 Y 同分辨率（不子采样），槽位挂在 _uv_slots 池
        （U/V/UV/A 按 plane_type+尺寸区分，互不串用）。
        """
        return self._acquire_texture(
            self._uv_slots,
            self._uv_pool_size,
            width,
            height,
            frame_index,
            1,
            'a'
        )

    def _acquire_texture(
        self,
        slots: List[YUVTextureSlot],
        max_size: int,
        width: int,
        height: int,
        frame_index: int,
        components: int,
        plane_type: str
    ) -> Optional[moderngl.Texture]:
        """
        内部纹理获取逻辑。

        Args:
            slots: 槽位列表
            max_size: 最大槽位数量
            width: 纹理宽度
            height: 纹理高度
            frame_index: 帧号
            components: 通道数
            plane_type: 平面类型

        Returns:
            纹理对象或 None
        """
        # 1. 查找匹配尺寸和通道数的空闲槽位
        for slot in slots:
            if (
                slot.is_size_match(width, height, components)
                and slot.plane_type == plane_type
                and slot.is_available()
            ):
                slot.frame_index = frame_index
                slot.last_used_time = next(_SLOT_ACQUIRE_SEQ)
                return slot.texture

        # 2. AUD-39804: 容量未满时优先新建槽位，而不是抢占 busy 槽。
        #    旧顺序是「先抢占匹配尺寸的最旧 busy 槽，再考虑新建」，导致
        #    稳态下每个 (plane_type, 尺寸) 只有 1 张纹理：上一帧纹理尚在
        #    渲染/引用中就被下一帧覆盖（写读竞态），也使池容量形同虚设。
        #    新顺序保证在轨帧各占独立槽位，直到池满才按 LRU 抢占。
        if len(slots) < max_size:
            try:
                texture = self._ctx.texture(
                    size=(width, height),
                    components=components,
                    dtype=self._dtype
                )
                texture.filter = self._filter_mode
                texture.repeat_x = texture.repeat_y = False

                slot = YUVTextureSlot(
                    texture=texture,
                    frame_index=frame_index,
                    last_used_time=next(_SLOT_ACQUIRE_SEQ),
                    width=width,
                    height=height,
                    components=components,
                    plane_type=plane_type
                )
                slots.append(slot)

                logger.debug(f"YUVTexturePool: Created {plane_type} slot "
                           f"#{len(slots)} ({width}x{height}, comp={components})")

                return texture

            except Exception as e:
                logger.error(f"YUVTexturePool: Failed to create texture: {e}")
                return None

        # 3. 池已满：查找匹配尺寸、通道数和平面类型的槽位（含 busy），
        #    复用最旧的——此时抢占是必要的（无空闲槽也无剩余容量）。
        oldest_slot: Optional[YUVTextureSlot] = None
        oldest_time = float('inf')

        for slot in slots:
            if (
                slot.is_size_match(width, height, components)
                and slot.plane_type == plane_type
                and slot.last_used_time < oldest_time
            ):
                oldest_time = slot.last_used_time
                oldest_slot = slot

        if oldest_slot:
            oldest_slot.frame_index = frame_index
            oldest_slot.last_used_time = next(_SLOT_ACQUIRE_SEQ)
            return oldest_slot.texture

        # 4. 池已满且无匹配尺寸槽位，复用最旧的（重建纹理以匹配尺寸/components）
        if slots:
            oldest_idx = 0
            oldest_time = slots[0].last_used_time
            for i, slot in enumerate(slots):
                if slot.last_used_time < oldest_time:
                    oldest_time = slot.last_used_time
                    oldest_idx = i

            slot = slots[oldest_idx]

            # 释放旧纹理
            if slot.texture:
                try:
                    slot.texture.release()
                except Exception:
                    pass

            # 创建新纹理
            try:
                texture = self._ctx.texture(
                    size=(width, height),
                    components=components,
                    dtype=self._dtype
                )
                texture.filter = self._filter_mode
                texture.repeat_x = texture.repeat_y = False

                slot.texture = texture
                slot.frame_index = frame_index
                slot.last_used_time = next(_SLOT_ACQUIRE_SEQ)
                slot.width = width
                slot.height = height
                slot.components = components  # 更新通道数
                slot.plane_type = plane_type

                logger.debug(f"YUVTexturePool: Replaced {plane_type} slot "
                           f"#{oldest_idx} ({width}x{height}, comp={components})")

                return texture

            except Exception as e:
                logger.error(f"YUVTexturePool: Failed to recreate texture: {e}")
                return None

        return None

    def release_all(self) -> None:
        """释放所有纹理"""
        for slot in self._y_slots:
            if slot.texture:
                try:
                    slot.texture.release()
                except Exception as e:
                    logger.debug(f"YUVTexturePool: Release Y failed: {e}")

        for slot in self._uv_slots:
            if slot.texture:
                try:
                    slot.texture.release()
                except Exception as e:
                    logger.debug(f"YUVTexturePool: Release UV failed: {e}")

        self._y_slots.clear()
        self._uv_slots.clear()
        logger.debug("YUVTexturePool: Released all slots")

    def get_stats(self) -> Dict[str, Any]:
        """获取池统计信息"""
        return {
            'y_pool_size': len(self._y_slots),
            'y_max_size': self._y_pool_size,
            'uv_pool_size': len(self._uv_slots),
            'uv_max_size': self._uv_pool_size,
            'active_y_frames': [s.frame_index for s in self._y_slots if s.frame_index >= 0],
            'active_uv_frames': [s.frame_index for s in self._uv_slots if s.frame_index >= 0],
            'y_sizes': [(s.width, s.height) for s in self._y_slots],
            'uv_sizes': [(s.width, s.height) for s in self._uv_slots],
        }

    def update_upload_stats(
        self,
        y_bytes: int,
        uv_bytes: int,
        upload_time_ms: float
    ) -> None:
        """更新上传统计"""
        self._stats.frame_count += 1
        self._stats.total_upload_time_ms += upload_time_ms
        self._stats.avg_upload_time_ms = (
            self._stats.total_upload_time_ms / self._stats.frame_count
        )
        self._stats.y_bytes += y_bytes
        self._stats.uv_bytes += uv_bytes
        self._stats.total_bytes_uploaded += y_bytes + uv_bytes

    def get_upload_stats(self) -> YUVUploadStats:
        """获取上传统计"""
        return self._stats

    @staticmethod
    def _cleanup_on_gc(
        y_slots: List[YUVTextureSlot],
        uv_slots: List[YUVTextureSlot]
    ) -> None:
        """GC 时确保纹理释放"""
        for slot in y_slots:
            try:
                if slot.texture is not None:
                    slot.texture.release()
            except Exception:
                pass
        for slot in uv_slots:
            try:
                if slot.texture is not None:
                    slot.texture.release()
            except Exception:
                pass


# ================================================================
# PBO 双缓冲异步上传池
# ================================================================

# PBO 大小对齐到 256 字节（GL 标准 UNPACK_ALIGNMENT）
_PBO_ALIGNMENT = 256


def _round_to_alignment(size: int, alignment: int = _PBO_ALIGNMENT) -> int:
    """将大小向上对齐到指定倍数。"""
    return ((size + alignment - 1) // alignment) * alignment


class PBOUploadPool:
    """
    PBO (Pixel Buffer Object) 双缓冲异步上传池。

    利用 OpenGL PBO 实现 CPU → GPU 的异步 DMA 传输：
    1. CPU 将数据写入 PBO 缓冲（不阻塞 GPU）
    2. texture.write(pbo) 触发 PBO → 纹理的 GPU 侧 DMA 拷贝
    3. 双缓冲旋转：CPU 写 PBO[N] 时 GPU 可读取 PBO[N-1]

    关键优化：
    - orphan() 重置缓冲避免 GPU stall
    - 按大小分池复用，减少分配
    - 零拷贝：直接接受 numpy 数组（通过 buffer protocol）

    ModernGL 支持：
    - texture.write(buffer_object) 内部绑定 GL_PIXEL_UNPACK_BUFFER
    - 等同于原生 glTexSubImage2D + PBO 的标准异步路径
    """

    def __init__(
        self,
        ctx: moderngl.Context,
        num_slots: int = 2,
    ) -> None:
        """
        初始化 PBO 上传池。

        Args:
            ctx: ModernGL 上下文
            num_slots: 每个大小的 PBO 槽位数（2=双缓冲, 3=三缓冲）
        """
        self._ctx = ctx
        self._num_slots = max(2, num_slots)
        # 按对齐后的大小分组: {rounded_size: [pbo0, pbo1, ...]}
        self._pools: Dict[int, List[moderngl.Buffer]] = {}
        self._indices: Dict[int, int] = {}

    def upload_to_texture(
        self,
        texture: moderngl.Texture,
        data: np.ndarray,
    ) -> None:
        """
        通过 PBO 异步上传数据到纹理。

        流程：
        1. 获取下一个 PBO（双缓冲旋转）
        2. orphan() 重置缓冲（告诉驱动旧数据可丢弃）
        3. CPU 写入数据到 PBO
        4. texture.write(pbo) 触发 GPU DMA 传输

        Args:
            texture: 目标纹理
            data: numpy 数组（必须 contiguous）
        """
        nbytes = data.nbytes
        if nbytes == 0:
            return

        rounded = _round_to_alignment(nbytes)
        pbo = self._get_pbo(rounded)

        # orphan() 通知驱动可以丢弃旧数据，避免 GPU stall
        # 这是 PBO 最佳实践的核心技巧
        pbo.orphan()

        # CPU → PBO（通过 Python buffer protocol，零拷贝）
        pbo.write(data)

        # PBO → texture（GPU 侧 DMA，异步）
        # ModernGL 内部绑定 GL_PIXEL_UNPACK_BUFFER 后调用 glTexSubImage2D
        texture.write(pbo)

    def _get_pbo(self, rounded_size: int) -> moderngl.Buffer:
        """获取指定大小的 PBO，按旋转索引选择。"""
        if rounded_size not in self._pools:
            pool = []
            for _ in range(self._num_slots):
                pbo = self._ctx.buffer(reserve=rounded_size)
                pool.append(pbo)
            self._pools[rounded_size] = pool
            self._indices[rounded_size] = 0

        idx = self._indices[rounded_size]
        pbo = self._pools[rounded_size][idx]
        self._indices[rounded_size] = (idx + 1) % self._num_slots
        return pbo

    def release(self) -> None:
        """释放所有 PBO 缓冲。"""
        for pool in self._pools.values():
            for pbo in pool:
                try:
                    pbo.release()
                except Exception:
                    pass
        self._pools.clear()
        self._indices.clear()

    def get_stats(self) -> Dict[str, Any]:
        """获取 PBO 池统计信息。"""
        return {
            'num_slots': self._num_slots,
            'size_groups': len(self._pools),
            'sizes': list(self._pools.keys()),
            'total_pbos': sum(len(p) for p in self._pools.values()),
        }


# ================================================================
# YUV GPU 上传器
# ================================================================

class YUVGPUUploader:
    """
    YUV 平面直接 GPU 上传器。

    核心功能：
    - upload_yuv420: 上传 YUV420P 格式（三个独立平面）
    - upload_nv12: 上传 NV12 格式（Y + interleaved UV）
    - upload_p010: 上传 P010 格式（10-bit NV12）
    - upload_yuv422: 上传 YUV422P 格式
    - upload_yuv444: 上传 YUV444P 格式

    带宽优化：
    - 避免 CPU 端 YUV -> RGB 转换
    - 直接上传 YUV 平面到 GPU
    - GPU shader 进行高效并行转换

    性能优化（Phase 1 + Phase 2）：
    - Phase 1: 消除 .tobytes() 拷贝，ModernGL 直接读 numpy 数组
    - Phase 2: PBO 双缓冲异步上传，GPU DMA 传输

    注意事项：
    - 所有 OpenGL 操作必须在渲染线程执行
    - 需要先调用 get_yuv_shader() 获取 shader
    - shader uniform 需要正确设置
    """

    # YUV 转换矩阵标识
    MATRIX_BT709 = 0
    MATRIX_BT2020 = 1
    MATRIX_BT601 = 2

    # 颜色范围标识
    RANGE_LIMITED = 0  # TV legal range
    RANGE_FULL = 1     # PC full range

    def __init__(
        self,
        ctx: moderngl.Context,
        pool_size: int = 5,
        dtype: str = 'f2'
    ) -> None:
        """
        初始化 YUV GPU 上传器。

        Args:
            ctx: ModernGL 上下文（必须在渲染线程）
            pool_size: 纹理池大小
            dtype: 纹理数据类型（'f2' = float16, 'f4' = float32, 'u2' = uint16 normalized）
        """
        self._ctx = ctx
        self._pool = YUVTexturePool(ctx, pool_size, pool_size, dtype)
        self._program: Optional[moderngl.Program] = None
        self._yuv_matrix: int = self.MATRIX_BT709
        self._color_range: int = self.RANGE_LIMITED

        # 缓存 VAO 和 quad buffer
        self._quad_vao: Optional[moderngl.VertexArray] = None
        self._quad_buffer: Optional[moderngl.Buffer] = None

        # Phase 1: 上传统计（分阶段计时）
        self._last_upload_timing: Dict[str, float] = {}

        # Phase 2: Staging buffer 池 — 按尺寸和 dtype 复用
        # 格式: {(nbytes, dtype_str): np.ndarray}
        self._staging_buffers: Dict[Tuple[int, str], np.ndarray] = {}
        self._staging_ring_size: int = 3  # 每尺寸缓存 3 个防止写冲突
        # Ring buffer: {(nbytes, dtype_str): [buf0, buf1, buf2]}
        self._staging_rings: Dict[Tuple[int, str], List[np.ndarray]] = {}
        self._staging_ring_idx: Dict[Tuple[int, str], int] = {}

        # R16UI integer texture mode
        # Uses usampler2D in shader to read raw uint16 values, then normalizes
        # in shader: float(code) / float((1 << bit_depth) - 1).
        # This bypasses all Python-side float16 conversion for 10/12/16-bit YUV.
        self._r16ui_probe_ok: bool = False
        self._r16ui_self_test_passed: bool = False
        self._r16ui_auto_tested: bool = False  # True once auto-test attempted
        self._r16ui_pool: Optional[YUVTexturePool] = None
        self._r16ui_program: Optional[moderngl.Program] = None
        self._r16ui_quad_vao: Optional[moderngl.VertexArray] = None
        self._r16ui_disabled_reason: str = ""
        self._last_upload_mode: str = 'float16'

        # NV12 R16UI shader — usampler2D for NV12 2-plane layout (Y + interleaved UV)
        self._nv12_r16ui_program: Optional[moderngl.Program] = None
        self._nv12_r16ui_vao: Optional[moderngl.VertexArray] = None

        # G-P0-01: NV12 sampler2D shader 必须只编译一次。此前 get_nv12_shader()
        # 每帧都 self._ctx.program(...) 重新编译，AsyncRenderer._render_quad 的
        # VAO cache 以 program.glo 为 key，导致每帧新增一个永不释放的 VAO，
        # 造成编译尖峰与 GL 对象泄漏。这里按 _nv12_r16ui_program 的同款模式缓存。
        self._nv12_program: Optional[moderngl.Program] = None

        # AUD-39806: render_nv12_to_rgb 的 quad 几何与 VAO 复用。
        # 此前每帧创建/销毁临时 VBO/IBO/VAO（且 vao.render 抛异常时泄漏），
        # 现按 shader 缓存 VAO，共享 VBO/IBO，随 release() 统一释放。
        self._nv12_quad_ibo: Optional[moderngl.Buffer] = None
        self._nv12_vao_cache: Dict[int, Tuple[moderngl.Program, moderngl.VertexArray]] = {}

        # AUD-39810: 三处四边形 IBO 登记为成员。moderngl 5.12 的
        # VertexArray.release() 不会释放 index buffer，此前这三处只用局部
        # 变量持有 IBO，release() 永远释放不到 → 每个 uploader 生命周期
        # 结束残留 GL buffer。现在随 release() 统一释放。
        self._quad_ibo: Optional[moderngl.Buffer] = None            # _init_quad_buffer
        self._r16ui_quad_ibo: Optional[moderngl.Buffer] = None      # _init_r16ui_quad
        self._nv12_r16ui_ibo: Optional[moderngl.Buffer] = None      # _create_nv12_r16ui_shader

        # R8 direct upload pool for 8-bit YUV (normalized unsigned byte textures)
        # Skips Python-side float16 conversion entirely for 8-bit content.
        self._r8_pool: Optional[YUVTexturePool] = None
        try:
            test_tex = ctx.texture((4, 4), components=1, dtype='f1')
            test_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            test_tex.repeat_x = test_tex.repeat_y = False
            test_tex.release()
            self._r8_pool = YUVTexturePool(
                ctx, pool_size, pool_size, 'f1',
                filter_mode=(moderngl.LINEAR, moderngl.LINEAR)
            )
            logger.info("YUVGPUUploader: R8 direct pool initialized")
        except Exception as e:
            logger.info(f"YUVGPUUploader: R8 pool not available ({e}), using float16 for 8-bit")

        # Probe: can we create uint16 textures?
        try:
            test_tex = ctx.texture((4, 4), components=1, dtype='u2')
            test_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
            test_tex.repeat_x = test_tex.repeat_y = False
            test_tex.release()
            self._r16ui_probe_ok = True
            # Create R16UI pool with NEAREST filtering (required for integer textures)
            self._r16ui_pool = YUVTexturePool(
                ctx, pool_size, pool_size, 'u2',
                filter_mode=(moderngl.NEAREST, moderngl.NEAREST)
            )
            logger.info("YUVGPUUploader: R16UI texture creation succeeded, awaiting self-test")
        except Exception as e:
            self._r16ui_disabled_reason = f"texture creation failed: {e}"
            logger.info(f"YUVGPUUploader: R16UI not available ({e}), using float16")

        # Phase 2: PBO 双缓冲异步上传池（默认关闭，仅在真实渲染上下文中按需启用）
        # 微基准测试发现：standalone context 中 PBO 反而更慢（额外开销 > 收益）
        # PBO 优势在于 CPU/GPU 流水线并行，需在真实 QOpenGLContext 中评估
        self._pbo_pool: Optional[PBOUploadPool] = None
        self._pbo_enabled: bool = False
        self._pbo_slot_count: int = 0

    def enable_pbo(self, enabled: bool = True, *, num_slots: int = 2) -> None:
        """
        启用或禁用 PBO 异步上传。

        PBO 在真实 QOpenGLContext 中可能有优势（CPU/GPU 流水线并行），
        但在 standalone context 中测试反而更慢。建议在应用启动时根据
        渲染上下文类型决定是否启用。

        线程契约（AUD-39811）：本方法会创建/释放 GL buffer（PBO 探针与
        池），按模块铁律只能在渲染/GL 线程调用。当渲染线程已注册
        （VideoRenderThread 运行中）而当前线程不是渲染线程时，调用会被
        拒绝（仅告警，不触碰 GL），避免与渲染线程并发上传竞态释放
        GL 对象。未注册渲染线程的环境（启动配置阶段 / 单测 / standalone
        探针）保持既有行为。

        Args:
            enabled: True 启用 PBO，False 禁用（使用直接上传）
            num_slots: Per-size staging buffers. Multi-track 4K playback uses
                three slots so the third visible layer does not immediately
                recycle a buffer that the GPU may still be reading.
        """
        # AUD-39811: 线程契约守卫。渲染线程经 frame_queue 的注册表登记
        # 自己的 ident；注册后非渲染线程调用一律拒绝执行。
        gl_thread_id = gpu_frame_release_thread_id()
        if gl_thread_id is not None and gl_thread_id != threading.get_ident():
            logger.warning(
                "YUVGPUUploader.enable_pbo refused: called from thread %d but "
                "GL/render thread is %d. PBO pool create/release touches GL "
                "objects and must run on the render thread (AUD-39811).",
                threading.get_ident(), gl_thread_id,
            )
            return
        desired_slots = max(2, int(num_slots))
        if not enabled:
            # A three-slot 4K ring can retain hundreds of MB of staging
            # buffers. Release it when a task returns to normal 1080p input
            # instead of keeping that memory pinned for the rest of the edit.
            if self._pbo_pool is not None:
                self._pbo_pool.release()
                self._pbo_pool = None
            self._pbo_slot_count = 0
            self._pbo_enabled = False
            return
        if enabled and (
            self._pbo_pool is None or self._pbo_slot_count != desired_slots
        ):
            try:
                if self._pbo_pool is not None:
                    self._pbo_pool.release()
                test_pbo = self._ctx.buffer(reserve=1024)
                test_pbo.release()
                self._pbo_pool = PBOUploadPool(self._ctx, num_slots=desired_slots)
                self._pbo_slot_count = desired_slots
                logger.info(
                    "YUVGPUUploader: PBO async upload enabled (slots=%d)",
                    desired_slots,
                )
            except Exception as e:
                logger.warning(f"YUVGPUUploader: PBO not available ({e})")
                self._pbo_pool = None
                self._pbo_slot_count = 0
                enabled = False
        self._pbo_enabled = enabled

    def _auto_enable_r16ui(self) -> None:
        """
        自动触发 R16UI 自检（仅一次）。

        在首次遇到 >8-bit 上传请求时自动调用 run_r16ui_self_test()。
        如果自检通过，后续所有 10/12/16-bit 上传将使用 R16UI 直传路径，
        跳过 Python 端 float16 归一化。

        设计原则：
        - 仅执行一次（_r16ui_auto_tested 标志）
        - probe 失败时跳过（_r16ui_probe_ok 为 False）
        - 自检失败不影响功能（回退到 float16 路径）
        """
        if self._r16ui_auto_tested:
            return
        self._r16ui_auto_tested = True

        if not self._r16ui_probe_ok:
            logger.info("YUVGPUUploader: R16UI auto-test skipped (probe failed)")
            return

        logger.info("YUVGPUUploader: Running R16UI auto self-test for >8-bit upload...")
        try:
            passed = self.run_r16ui_self_test()
            if passed:
                logger.info("YUVGPUUploader: R16UI auto-test PASSED — "
                           "10/12/16-bit uploads will use usampler2D direct path")
            else:
                logger.info(f"YUVGPUUploader: R16UI auto-test FAILED ({self._r16ui_disabled_reason}) — "
                           "falling back to float16 normalization")
        except Exception as e:
            logger.warning(f"YUVGPUUploader: R16UI auto-test exception: {e}")

    # ================================================================
    # Phase 1 + Phase 2: 统一上传辅助方法
    # ================================================================

    def _upload_to_texture(
        self,
        texture: moderngl.Texture,
        data: np.ndarray,
    ) -> None:
        """
        上传 numpy 数组到纹理（零拷贝优化）。

        Phase 1: 使用 np.ascontiguousarray 避免 .tobytes() 拷贝，
                 ModernGL 通过 Python buffer protocol 直接读取 numpy 内存。
        Phase 2: 可选 PBO 异步上传（通过 enable_pbo(True) 启用）。

        Args:
            texture: 目标纹理
            data: numpy 数组数据
        """
        contiguous = np.ascontiguousarray(data)

        if self._pbo_enabled and self._pbo_pool is not None:
            # Phase 2: PBO 异步上传路径（仅在真实渲染上下文中启用）
            self._pbo_pool.upload_to_texture(texture, contiguous)
        else:
            # Phase 1: 直接上传（零拷贝，无 .tobytes()）
            # 微基准测试：2K 单平面 0.08ms vs .tobytes() 0.32ms (快 4x)
            texture.write(contiguous)

    def upload_yuv420(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        frame_index: int,
        bit_depth: int = 8,
        a: Optional[np.ndarray] = None,
        alpha_bit_depth: Optional[int] = None,
    ) -> tuple:
        """
        上传 YUV420P 平面到 GPU。

        YUV420P (4:2:0):
        - Y plane: width × height
        - U plane: (width/2) × (height/2)
        - V plane: (width/2) × (height/2)

        8-bit 内容自动重定向到 R8 直传路径（跳过 float16 归一化），
        将 upload 时间从 ~30ms 降低到 ~0.5ms。

        Args:
            y: Y 平面数据 (uint8 或 uint16)
            u: U 平面数据
            v: V 平面数据
            frame_index: 帧号（用于纹理池管理）
            bit_depth: 位深度 (8, 10, 12, 16)
            a: Alpha 平面数据（可选，YUVA 第四平面，与 Y 同分辨率）
            alpha_bit_depth: Alpha 位深（默认与 bit_depth 一致；
                Topos a8/a10/a12/a16 的 Alpha 位深独立于 luma 的 10）

        Returns:
            (y_texture, u_texture, v_texture) 元组；
            带 Alpha 时为 (y, u, v, a) 四元组

        Raises:
            RuntimeError: 纹理池获取失败
        """
        start_time = time.perf_counter()

        # 8-bit: 重定向到 R8 直传路径（跳过 float16 归一化）
        # 优化效果：~30ms → ~0.5ms（消除无用的 uint8→float16 转换）
        if bit_depth <= 8 and self._r8_pool is not None:
            return self._upload_planar_r8(y, u, v, frame_index,
                                          u_width=y.shape[1] // 2,
                                          u_height=y.shape[0] // 2,
                                          a=a, subsampling='420',
                                          alpha_bit_depth=alpha_bit_depth)

        # 10/12/16-bit: 重定向到 R16UI/usampler2D 直传路径（跳过 float16 归一化）
        # 首次遇到 >8-bit 时自动触发 R16UI 自检
        if bit_depth > 8:
            self._auto_enable_r16ui()
            if self._r16ui_self_test_passed and self._r16ui_pool is not None:
                return self.upload_yuv420_r16ui(
                    y, u, v, frame_index, bit_depth,
                    a=a, alpha_bit_depth=alpha_bit_depth)

        # 获取尺寸
        y_height, y_width = y.shape[:2]
        u_height, u_width = u.shape[:2]
        v_height, v_width = v.shape[:2]

        # 验证尺寸比例（420 格式）
        assert u_width == y_width // 2, f"U width mismatch: {u_width} vs {y_width//2}"
        assert u_height == y_height // 2, f"U height mismatch: {u_height} vs {y_height//2}"

        # 归一化数据 — 使用 staging buffer 避免反复分配
        t_prepare_start = time.perf_counter()
        y_data, u_data, v_data = self._prepare_plane_data(y, u, v, bit_depth)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        # 获取纹理槽位
        y_tex = self._pool.acquire_y_texture(y_width, y_height, frame_index)
        u_tex = self._pool.acquire_u_texture(u_width, u_height, frame_index)
        v_tex = self._pool.acquire_v_texture(v_width, v_height, frame_index)

        if y_tex is None or u_tex is None or v_tex is None:
            raise RuntimeError("Failed to acquire YUV textures from pool")

        # 上传数据（Phase 1+2: 零拷贝 + PBO 异步）
        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(u_tex, u_data)
        self._upload_to_texture(v_tex, v_data)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        # 计算上传统计
        y_bytes = y_data.nbytes
        uv_bytes = u_data.nbytes + v_data.nbytes
        upload_time_ms = (time.perf_counter() - start_time) * 1000.0

        self._pool.update_upload_stats(y_bytes, uv_bytes, upload_time_ms)
        self._last_upload_mode = 'float16'
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'float16',
        }

        logger.debug(f"YUV420 uploaded: frame={frame_index}, "
                    f"Y={y_width}x{y_height}, "
                    f"UV={u_width}x{u_height}, "
                    f"prepare={t_prepare_ms:.2f}ms, write={t_write_ms:.2f}ms, "
                    f"total={upload_time_ms:.2f}ms")

        if a is not None:
            a_tex = self._upload_alpha_plane(
                a, frame_index, alpha_bit_depth or bit_depth, 'float16')
            if a_tex is not None:
                return (y_tex, u_tex, v_tex, a_tex)

        return (y_tex, u_tex, v_tex)

    def upload_nv12(
        self,
        y: np.ndarray,
        uv: np.ndarray,
        frame_index: int,
        bit_depth: int = 8
    ) -> Tuple[moderngl.Texture, moderngl.Texture]:
        """
        上传 NV12 平面到 GPU。

        NV12 (4:2:0 interleaved):
        - Y plane: width × height
        - UV plane: (width/2) × (height/2) × 2 (interleaved U/V)

        Args:
            y: Y 平面数据 (height, width)
            uv: UV interleaved 数据 (height/2, width/2, 2) 或 (height/2, width)
            frame_index: 帧号
            bit_depth: 位深度

        Returns:
            (y_texture, uv_texture) 元组
        """
        # 8-bit NV12: use R8 direct path if available (skip float16)
        if bit_depth <= 8 and self._r8_pool is not None:
            return self._upload_nv12_r8(y, uv, frame_index)

        # 10/12/16-bit NV12: use R16UI direct path if available
        if bit_depth > 8:
            self._auto_enable_r16ui()
            if self._r16ui_self_test_passed and self._r16ui_pool is not None:
                return self.upload_nv12_r16ui(y, uv, frame_index, bit_depth)

        start_time = time.perf_counter()

        y_height, y_width = y.shape[:2]
        uv_height, uv_width = uv.shape[:2]

        # NV12 UV 尺寸验证和统一处理
        if uv.ndim == 3 and uv.shape[2] == 2:
            uv_tex_width = uv.shape[1]
            uv_upload = uv
            assert uv_height == y_height // 2, "UV height mismatch"
            assert uv_tex_width == y_width // 2, "UV width mismatch"
        elif uv.ndim == 2:
            uv_tex_width = uv.shape[1] // 2
            assert uv_height == y_height // 2, "UV height mismatch"
            assert uv.shape[1] == y_width, "UV width mismatch (interleaved)"
            uv_upload = uv.reshape(uv.shape[0], uv_tex_width, 2)
        else:
            raise ValueError(f"Invalid UV array shape: {uv.shape}")

        # 归一化 — 使用 staging buffer
        t_prepare_start = time.perf_counter()
        max_val = (1 << bit_depth) - 1
        y_data = self._normalize_plane(y, max_val)
        uv_data = self._normalize_plane(uv_upload, max_val)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        # 获取纹理
        y_tex = self._pool.acquire_y_texture(y_width, y_height, frame_index)
        uv_tex = self._pool.acquire_uv_texture(
            uv_tex_width, uv_height, frame_index, components=2
        )

        if y_tex is None or uv_tex is None:
            raise RuntimeError("Failed to acquire NV12 textures from pool")

        # 上传（Phase 1+2: 零拷贝 + PBO 异步）
        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(uv_tex, uv_data)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        # 统计
        y_bytes = y_data.nbytes
        uv_bytes = uv_data.nbytes
        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        self._pool.update_upload_stats(y_bytes, uv_bytes, upload_time_ms)
        self._last_upload_mode = 'float16'
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'float16',
        }

        logger.debug(f"NV12 uploaded: frame={frame_index}, "
                    f"prepare={t_prepare_ms:.2f}ms, write={t_write_ms:.2f}ms, "
                    f"total={upload_time_ms:.2f}ms")

        return (y_tex, uv_tex)

    def upload_p010(
        self,
        y: np.ndarray,
        uv: np.ndarray,
        frame_index: int,
        bit_depth: int = 10
    ) -> Tuple[moderngl.Texture, moderngl.Texture]:
        """
        上传 P010/P016 平面到 GPU。

        P010 是 10-bit NV12 格式：
        - Y plane: width × height (uint16, 实际 10-bit)
        - UV plane: (width/2) × (height/2) × 2 (uint16)

        Args:
            y: Y 平面数据 (uint16, 值范围 0-1023，16bit 时 0-65535)
            uv: UV interleaved 数据 (uint16)
            frame_index: 帧号
            bit_depth: 位深度（float 回退路径在上传时按 (1<<bit_depth)-1
                        归一化；R16UI 直传路径上传原始码值，归一化在
                        渲染着色器完成）

        Returns:
            (y_texture, uv_texture) 元组
        """
        if self._r16ui_self_test_passed:
            return self.upload_nv12_r16ui(y, uv, frame_index, bit_depth=bit_depth)
        return self.upload_nv12(y, uv, frame_index, bit_depth=bit_depth)

    def upload_nv12_r16ui(
        self,
        y: np.ndarray,
        uv: np.ndarray,
        frame_index: int,
        bit_depth: int = 10
    ) -> Tuple[moderngl.Texture, moderngl.Texture]:
        """
        上传 NV12/P010 平面到 GPU 使用 R16UI 纹理。

        直接上传 uint16 数据到 usampler2D 纹理，跳过 float16 转换。
        P010 (10-bit NV12) 和高 bit-depth NV12 应使用此路径。

        Returns:
            (y_texture, uv_texture) 元组，使用 R16UI 纹理
        """
        if not self._r16ui_self_test_passed or self._r16ui_pool is None:
            return self.upload_nv12(y, uv, frame_index, bit_depth)

        start_time = time.perf_counter()

        y_height, y_width = y.shape[:2]
        uv_height, uv_width = uv.shape[:2]

        # NV12 UV 尺寸验证
        if uv.ndim == 3 and uv.shape[2] == 2:
            uv_tex_width = uv.shape[1]
            uv_data = np.ascontiguousarray(uv)
        elif uv.ndim == 2:
            uv_tex_width = uv.shape[1] // 2
            uv_data = np.ascontiguousarray(uv.reshape(uv.shape[0], uv_tex_width, 2))
        else:
            raise ValueError(f"Invalid UV array shape: {uv.shape}")

        t_prepare_start = time.perf_counter()
        y_data = np.ascontiguousarray(y)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        # Use R16UI pool for both Y and UV planes
        y_tex = self._r16ui_pool.acquire_y_texture(y_width, y_height, frame_index)
        uv_tex = self._r16ui_pool.acquire_uv_texture(uv_tex_width, uv_height, frame_index, components=2)

        if y_tex is None or uv_tex is None:
            raise RuntimeError("Failed to acquire R16UI NV12 textures")

        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(uv_tex, uv_data)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        y_bytes = y_data.nbytes
        uv_bytes = uv_data.nbytes
        self._pool.update_upload_stats(y_bytes, uv_bytes, upload_time_ms)
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'r16ui_direct',
        }
        self._last_upload_mode = 'r16ui_direct'

        logger.debug(f"NV12 R16UI uploaded: frame={frame_index}, bit_depth={bit_depth}, "
                    f"prepare={t_prepare_ms:.2f}ms, write={t_write_ms:.2f}ms")

        return (y_tex, uv_tex)

    def upload_yuv422(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        frame_index: int,
        bit_depth: int = 8,
        a: Optional[np.ndarray] = None,
        alpha_bit_depth: Optional[int] = None,
    ) -> tuple:
        """
        上传 YUV422P 平面到 GPU。

        YUV422P (4:2:2):
        - Y plane: width × height
        - U plane: ceil(width/2) × height
        - V plane: ceil(width/2) × height

        Args:
            a: Alpha 平面数据（可选，YUVA 第四平面，与 Y 同分辨率）
            alpha_bit_depth: Alpha 位深（默认与 bit_depth 一致）
        """
        # 8-bit: 重定向到 R8 直传路径（跳过 float16 归一化）
        if bit_depth <= 8 and self._r8_pool is not None:
            return self._upload_planar_r8(y, u, v, frame_index,
                                          u_width=(y.shape[1] + 1) // 2,
                                          u_height=y.shape[0],
                                          a=a, subsampling='422',
                                          alpha_bit_depth=alpha_bit_depth)

        # 10/12/16-bit: 重定向到 R16UI/usampler2D 直传路径
        if bit_depth > 8:
            self._auto_enable_r16ui()
            if self._r16ui_self_test_passed and self._r16ui_pool is not None:
                return self.upload_yuv422_r16ui(
                    y, u, v, frame_index, bit_depth,
                    a=a, alpha_bit_depth=alpha_bit_depth)

        start_time = time.perf_counter()

        y_height, y_width = y.shape[:2]
        u_height, u_width = u.shape[:2]

        # 422 尺寸验证；Topos uses ceil(width / 2) for odd visible widths.
        assert u_width == (y_width + 1) // 2, "U width mismatch"
        assert u_height == y_height, "U height mismatch"

        t_prepare_start = time.perf_counter()
        y_data, u_data, v_data = self._prepare_plane_data(y, u, v, bit_depth)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        y_tex = self._pool.acquire_y_texture(y_width, y_height, frame_index)
        u_tex = self._pool.acquire_u_texture(u_width, u_height, frame_index)
        v_tex = self._pool.acquire_v_texture(u_width, u_height, frame_index)

        if y_tex is None or u_tex is None or v_tex is None:
            raise RuntimeError("Failed to acquire YUV422 textures")

        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(u_tex, u_data)
        self._upload_to_texture(v_tex, v_data)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        # 统计
        y_bytes = y_data.nbytes
        uv_bytes = u_data.nbytes + v_data.nbytes
        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        self._pool.update_upload_stats(y_bytes, uv_bytes, upload_time_ms)
        self._last_upload_mode = 'float16'
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'float16',
        }

        logger.debug(f"YUV422 uploaded: frame={frame_index}, "
                    f"prepare={t_prepare_ms:.2f}ms, write={t_write_ms:.2f}ms, "
                    f"total={upload_time_ms:.2f}ms")

        if a is not None:
            a_tex = self._upload_alpha_plane(
                a, frame_index, alpha_bit_depth or bit_depth, 'float16')
            if a_tex is not None:
                return (y_tex, u_tex, v_tex, a_tex)

        return (y_tex, u_tex, v_tex)

    def upload_yuv444(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        frame_index: int,
        bit_depth: int = 8,
        a: Optional[np.ndarray] = None,
        alpha_bit_depth: Optional[int] = None,
    ) -> tuple:
        """
        上传 YUV444P 平面到 GPU。

        YUV444P (4:4:4):
        - 所有平面: width × height

        Args:
            a: Alpha 平面数据（可选，YUVA 第四平面，与 Y 同分辨率）
            alpha_bit_depth: Alpha 位深（默认与 bit_depth 一致）
        """
        # 8-bit: 重定向到 R8 直传路径（跳过 float16 归一化）
        if bit_depth <= 8 and self._r8_pool is not None:
            return self._upload_planar_r8(y, u, v, frame_index,
                                          u_width=y.shape[1],
                                          u_height=y.shape[0],
                                          a=a, subsampling='444',
                                          alpha_bit_depth=alpha_bit_depth)

        # 10/12/16-bit: 重定向到 R16UI/usampler2D 直传路径
        if bit_depth > 8:
            self._auto_enable_r16ui()
            if self._r16ui_self_test_passed and self._r16ui_pool is not None:
                return self.upload_yuv444_r16ui(
                    y, u, v, frame_index, bit_depth,
                    a=a, alpha_bit_depth=alpha_bit_depth)

        start_time = time.perf_counter()

        y_height, y_width = y.shape[:2]

        # 444 所有平面相同尺寸
        assert u.shape == y.shape, "U shape mismatch"
        assert v.shape == y.shape, "V shape mismatch"

        t_prepare_start = time.perf_counter()
        y_data, u_data, v_data = self._prepare_plane_data(y, u, v, bit_depth)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        y_tex = self._pool.acquire_y_texture(y_width, y_height, frame_index)
        u_tex = self._pool.acquire_u_texture(y_width, y_height, frame_index)
        v_tex = self._pool.acquire_v_texture(y_width, y_height, frame_index)

        if y_tex is None or u_tex is None or v_tex is None:
            raise RuntimeError("Failed to acquire YUV444 textures")

        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(u_tex, u_data)
        self._upload_to_texture(v_tex, v_data)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        # 统计
        y_bytes = y_data.nbytes
        uv_bytes = u_data.nbytes + v_data.nbytes
        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        self._pool.update_upload_stats(y_bytes, uv_bytes, upload_time_ms)
        self._last_upload_mode = 'float16'
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'float16',
        }

        logger.debug(f"YUV444 uploaded: frame={frame_index}, "
                    f"prepare={t_prepare_ms:.2f}ms, write={t_write_ms:.2f}ms, "
                    f"total={upload_time_ms:.2f}ms")

        if a is not None:
            a_tex = self._upload_alpha_plane(
                a, frame_index, alpha_bit_depth or bit_depth, 'float16')
            if a_tex is not None:
                return (y_tex, u_tex, v_tex, a_tex)

        return (y_tex, u_tex, v_tex)

    def upload_half_planar(
        self,
        g: np.ndarray,
        b: np.ndarray,
        r: np.ndarray,
        frame_index: int,
    ) -> tuple:
        """浮点样本域（'gbrph16le'，G,B,R 序）half-float 纹理直传。

        M1-F4：平面已是 float16 线性值（shader 采样域，无满刻度契约），
        **不做整数归一化**——R16UI 直传会把 float16 位型当码值、
        _normalize_plane 会按 (1<<bd)-1 缩放，两条既有路径都错标浮点值。
        直接上传 float16 纹理（pool dtype 'f4' 时转 float32），shader 侧
        走 gbr_mode 旁路（rgb=vec3(v,y,u)，采样值即线性 RGB）。

        Returns:
            (g_tex, b_tex, r_tex)——三纹理槽位与 upload_yuv444 返回语义
            一致（消费方按 y/u/v 槽位 + gbr_mode 采样）。
        """
        target_dtype = np.float16 if self._pool._dtype == 'f2' else np.float32
        start_time = time.perf_counter()
        h, w = g.shape[:2]
        assert b.shape == g.shape and r.shape == g.shape, "GBR float shape mismatch"

        g_data = np.ascontiguousarray(g, dtype=target_dtype)
        b_data = np.ascontiguousarray(b, dtype=target_dtype)
        r_data = np.ascontiguousarray(r, dtype=target_dtype)

        g_tex = self._pool.acquire_y_texture(w, h, frame_index)
        b_tex = self._pool.acquire_u_texture(w, h, frame_index)
        r_tex = self._pool.acquire_v_texture(w, h, frame_index)
        if g_tex is None or b_tex is None or r_tex is None:
            raise RuntimeError("Failed to acquire GBR float textures")

        self._upload_to_texture(g_tex, g_data)
        self._upload_to_texture(b_tex, b_data)
        self._upload_to_texture(r_tex, r_data)

        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        self._pool.update_upload_stats(g_data.nbytes,
                                       b_data.nbytes + r_data.nbytes,
                                       upload_time_ms)
        self._last_upload_mode = 'half_direct'
        self._last_upload_timing = {
            'prepare_ms': 0.0,
            'write_ms': upload_time_ms,
            'total_ms': upload_time_ms,
            'mode': 'half_direct',
        }
        return (g_tex, b_tex, r_tex)

    def get_last_upload_timing(self) -> Dict[str, float]:
        """获取最近一次上传的分段计时"""
        return self._last_upload_timing

    # ================================================================
    # Phase 2: Staging Buffer 辅助方法
    # ================================================================

    def _get_staging_buffer(self, nbytes: int, dtype: np.dtype) -> np.ndarray:
        """
        获取可复用的 staging buffer。

        使用 ring buffer 策略，每尺寸保持 3 个 buffer 防止
        GPU write 仍在使用前一个 buffer 时被覆盖。
        """
        key = (nbytes, dtype.str)
        if key not in self._staging_rings:
            ring = []
            for _ in range(self._staging_ring_size):
                buf = np.empty(nbytes // dtype.itemsize, dtype=dtype)
                ring.append(buf)
            self._staging_rings[key] = ring
            self._staging_ring_idx[key] = 0

        idx = self._staging_ring_idx[key]
        buf = self._staging_rings[key][idx]
        self._staging_ring_idx[key] = (idx + 1) % self._staging_ring_size
        return buf

    def _normalize_plane(self, plane: np.ndarray, max_val: int) -> np.ndarray:
        """
        归一化单个平面数据到 float16，使用 staging buffer 减少 alloc。

        仅用于 float16 路径。R16UI 路径不需要归一化（直接上传 uint16）。
        """
        target_dtype = np.float16 if self._pool._dtype == 'f2' else np.float32

        # 使用 staging buffer
        n_elements = plane.size
        buf = self._get_staging_buffer(n_elements * target_dtype().itemsize, np.dtype(target_dtype))
        buf = buf.reshape(plane.shape) if buf.size == n_elements else np.empty(plane.shape, dtype=target_dtype)

        # 直接转 float16 然后原地缩放 — 比 float32 中间数组快
        np.divide(plane.astype(target_dtype), float(max_val), out=buf)
        return np.ascontiguousarray(buf)

    def _prepare_plane_data(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        bit_depth: int
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        准备三个平面的上传数据。

        Phase 2: 使用 staging buffer 和原地操作避免多次大数组分配。
        Phase 3: 10/12/16-bit 优先使用 R16 normalized 直接上传。
        """
        max_val = (1 << bit_depth) - 1
        y_data = self._normalize_plane(y, max_val)
        u_data = self._normalize_plane(u, max_val)
        v_data = self._normalize_plane(v, max_val)
        return y_data, u_data, v_data

    def _upload_alpha_plane(
        self,
        a: np.ndarray,
        frame_index: int,
        alpha_bit_depth: int,
        mode: str
    ) -> Optional[moderngl.Texture]:
        """上传 YUVA 第四平面（Alpha，全分辨率）并返回纹理。

        与 Y/U/V 平面共用同一池（plane_type='a'），按上传模式选择池：
        - 'r16ui_direct': 原始 uint16 码值 → 'u2' 池（shader 内按
          alpha_bit_depth 归一化——Alpha 位深独立于 luma，如 topos a8/a16）
        - 'r8_direct': 原始 uint8 → 'f1' 池（sampler 自动 /255）
        - 'float16': CPU 归一化 /((1<<alpha_bit_depth)-1) → 'f2' 池

        Returns:
            Alpha 纹理，或 None（池不可用/失败——调用方按无 Alpha 继续）
        """
        h, w = a.shape[:2]
        try:
            if mode == 'r16ui_direct':
                if self._r16ui_pool is None:
                    return None
                tex = self._r16ui_pool.acquire_a_texture(w, h, frame_index)
                data = np.ascontiguousarray(a)
            elif mode == 'r8_direct':
                if self._r8_pool is None:
                    return None
                tex = self._r8_pool.acquire_a_texture(w, h, frame_index)
                data = np.ascontiguousarray(a)
            else:
                tex = self._pool.acquire_a_texture(w, h, frame_index)
                data = self._normalize_plane(a, (1 << alpha_bit_depth) - 1)

            if tex is None:
                return None
            self._upload_to_texture(tex, data)
            return tex
        except Exception as e:
            logger.warning(f"Alpha plane upload failed (mode={mode}): {e}")
            return None

    # ================================================================
    # R16UI Integer Texture Upload (usampler2D path)
    # ================================================================

    def upload_yuv444_r16ui(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        frame_index: int,
        bit_depth: int = 12,
        a: Optional[np.ndarray] = None,
        alpha_bit_depth: Optional[int] = None,
    ) -> tuple:
        """
        上传 YUV444P 平面到 GPU 使用 R16UI/usampler2D 纹理。

        直接上传 uint16 原始数据，shader 内用 usampler2D 读取 uint 值
        并归一化为 float(code) / float((1 << bit_depth) - 1)。
        完全跳过 Python 端 float16 转换。

        Args:
            y: Y 平面 (uint16)
            u: U 平面 (uint16)
            v: V 平面 (uint16)
            frame_index: 帧号
            bit_depth: 位深度
            a: Alpha 平面 (uint16 原始码值，全分辨率；位深独立)
            alpha_bit_depth: Alpha 位深（默认与 bit_depth 一致；shader 内
                单独归一化——Topos a8/a16 与 luma 的 10 不同）

        Returns:
            (y_texture, u_texture, v_texture) 或带 Alpha 的四元组
        """
        if not self._r16ui_self_test_passed or self._r16ui_pool is None:
            raise RuntimeError(
                f"R16UI upload path is disabled: {self._r16ui_disabled_reason}"
            )

        start_time = time.perf_counter()

        y_height, y_width = y.shape[:2]
        assert u.shape == y.shape and v.shape == y.shape

        # 直接使用 uint16 bytes — ascontiguousarray 只在必要时拷贝
        t_prepare_start = time.perf_counter()
        y_data = np.ascontiguousarray(y)
        u_data = np.ascontiguousarray(u)
        v_data = np.ascontiguousarray(v)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        # 从 R16UI 池获取纹理 (dtype='u2', NEAREST)
        y_tex = self._r16ui_pool.acquire_y_texture(y_width, y_height, frame_index)
        u_tex = self._r16ui_pool.acquire_u_texture(y_width, y_height, frame_index)
        v_tex = self._r16ui_pool.acquire_v_texture(y_width, y_height, frame_index)

        if y_tex is None or u_tex is None or v_tex is None:
            raise RuntimeError("Failed to acquire R16UI YUV444 textures")

        # 上传原始 uint16 数据（Phase 1+2: 零拷贝 + PBO 异步）
        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(u_tex, u_data)
        self._upload_to_texture(v_tex, v_data)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        y_bytes = y_data.nbytes
        uv_bytes = u_data.nbytes + v_data.nbytes
        self._pool.update_upload_stats(y_bytes, uv_bytes, upload_time_ms)
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'r16ui_direct',
        }
        self._last_upload_mode = 'r16ui_direct'

        logger.debug(f"YUV444 R16UI uploaded: frame={frame_index}, "
                    f"prepare={t_prepare_ms:.2f}ms, write={t_write_ms:.2f}ms, "
                    f"total={upload_time_ms:.2f}ms")

        if a is not None:
            a_tex = self._upload_alpha_plane(
                a, frame_index, alpha_bit_depth or bit_depth, 'r16ui_direct')
            if a_tex is not None:
                return (y_tex, u_tex, v_tex, a_tex)

        return (y_tex, u_tex, v_tex)

    def _upload_planar_r8(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        frame_index: int,
        u_width: int,
        u_height: int,
        a: Optional[np.ndarray] = None,
        subsampling: Optional[str] = None,
        alpha_bit_depth: Optional[int] = None,
    ) -> tuple:
        """
        8-bit YUV planar direct R8 upload — skip float16 conversion entirely.

        Uploads uint8 data to R8 (normalized unsigned byte) textures.
        The GPU sampler automatically returns [0,1] via texture().r / 255.0,
        so no Python-side normalization is needed.

        a: 可选 Alpha 平面（uint8，全分辨率）→ 同池 'a' 槽位（f1），
        sampler 自动归一化，返回四元组。
        subsampling: '420' / '422' / '444'（R8 池不可用回退 float16 路径时
        必须保留——AUD-39801；None 时按平面形状推导）。
        """
        if self._r8_pool is None:
            # AUD-39801: 回退到 float16 路径时必须保留子采样元数据。
            # 旧实现一律回退 upload_yuv420：8-bit 422/444 的色度平面
            # 尺寸不满足 420 断言（u_h == h//2），上传直接 AssertionError。
            y_height, y_width = y.shape[:2]
            if subsampling is None:
                if u_width == y_width and u_height == y_height:
                    subsampling = '444'
                elif u_height == y_height:
                    subsampling = '422'
                else:
                    subsampling = '420'
            if subsampling == '422':
                return self.upload_yuv422(
                    y, u, v, frame_index, bit_depth=8, a=a,
                    alpha_bit_depth=alpha_bit_depth)
            if subsampling == '444':
                return self.upload_yuv444(
                    y, u, v, frame_index, bit_depth=8, a=a,
                    alpha_bit_depth=alpha_bit_depth)
            return self.upload_yuv420(
                y, u, v, frame_index, bit_depth=8, a=a,
                alpha_bit_depth=alpha_bit_depth)

        start_time = time.perf_counter()

        y_height, y_width = y.shape[:2]

        # Ensure contiguous — avoids copy if already contiguous
        t_prepare_start = time.perf_counter()
        y_data = np.ascontiguousarray(y)
        u_data = np.ascontiguousarray(u)
        v_data = np.ascontiguousarray(v)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        # Acquire textures from the R8 pool (dtype='f1' = normalized unsigned byte)
        y_tex = self._r8_pool.acquire_y_texture(y_width, y_height, frame_index)
        u_tex = self._r8_pool.acquire_u_texture(u_width, u_height, frame_index)
        v_tex = self._r8_pool.acquire_v_texture(u_width, u_height, frame_index)

        if y_tex is None or u_tex is None or v_tex is None:
            raise RuntimeError("Failed to acquire R8 YUV textures from pool")

        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(u_tex, u_data)
        self._upload_to_texture(v_tex, v_data)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        y_bytes = y_data.nbytes
        uv_bytes = u_data.nbytes + v_data.nbytes
        self._pool.update_upload_stats(y_bytes, uv_bytes, upload_time_ms)
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'r8_direct',
        }
        self._last_upload_mode = 'r8_direct'

        logger.debug(f"YUV R8 direct uploaded: frame={frame_index}, "
                    f"prepare={t_prepare_ms:.2f}ms, write={t_write_ms:.2f}ms, "
                    f"total={upload_time_ms:.2f}ms")

        if a is not None:
            a_tex = self._upload_alpha_plane(a, frame_index, 8, 'r8_direct')
            if a_tex is not None:
                return (y_tex, u_tex, v_tex, a_tex)

        return (y_tex, u_tex, v_tex)

    def _upload_nv12_r8(
        self,
        y: np.ndarray,
        uv: np.ndarray,
        frame_index: int,
    ) -> Tuple[moderngl.Texture, moderngl.Texture]:
        """
        8-bit NV12 direct R8/RG8 upload -- skip float16 conversion entirely.

        Uploads uint8 data to R8 (Y) and RG8 (UV) textures.
        The GPU sampler automatically normalizes to [0,1], so no
        Python-side normalization (_normalize_plane) is needed.
        """
        if self._r8_pool is None:
            # Fallback to float16 path
            return self.upload_nv12(y, uv, frame_index, bit_depth=8)

        start_time = time.perf_counter()

        y_height, y_width = y.shape[:2]
        uv_height, uv_width = uv.shape[:2]

        # NV12 UV shape normalization
        if uv.ndim == 3 and uv.shape[2] == 2:
            uv_tex_width = uv.shape[1]
            assert uv_height == y_height // 2, "UV height mismatch"
            assert uv_tex_width == y_width // 2, "UV width mismatch"
            uv_upload = uv
        elif uv.ndim == 2:
            uv_tex_width = uv.shape[1] // 2
            assert uv_height == y_height // 2, "UV height mismatch"
            assert uv.shape[1] == y_width, "UV width mismatch (interleaved)"
            uv_upload = uv.reshape(uv.shape[0], uv_tex_width, 2)
        else:
            raise ValueError(f"Invalid UV array shape: {uv.shape}")

        # Ensure contiguous -- no float16 conversion
        t_prepare_start = time.perf_counter()
        y_data = np.ascontiguousarray(y)
        uv_upload = np.ascontiguousarray(uv_upload)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        # Acquire textures from the R8 pool
        y_tex = self._r8_pool.acquire_y_texture(y_width, y_height, frame_index)
        uv_tex = self._r8_pool.acquire_uv_texture(
            uv_tex_width, uv_height, frame_index, components=2
        )

        if y_tex is None or uv_tex is None:
            raise RuntimeError("Failed to acquire NV12 R8 textures from pool")

        # Upload raw uint8 data (Phase 1+2: 零拷贝 + PBO 异步)
        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(uv_tex, uv_upload)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        y_bytes = y_data.nbytes
        uv_bytes = uv_upload.nbytes
        self._pool.update_upload_stats(y_bytes, uv_bytes, upload_time_ms)
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'nv12_r8_rg8',
        }
        self._last_upload_mode = 'nv12_r8_rg8'

        logger.debug(f"NV12 R8 direct uploaded: frame={frame_index}, "
                    f"prepare={t_prepare_ms:.2f}ms, write={t_write_ms:.2f}ms, "
                    f"total={upload_time_ms:.2f}ms")

        return (y_tex, uv_tex)

    def upload_yuv420_r16ui(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        frame_index: int,
        bit_depth: int = 10,
        a: Optional[np.ndarray] = None,
        alpha_bit_depth: Optional[int] = None,
    ) -> tuple:
        """上传 YUV420P 平面到 GPU 使用 R16UI/usampler2D 纹理。"""
        if not self._r16ui_self_test_passed or self._r16ui_pool is None:
            raise RuntimeError(
                f"R16UI upload path is disabled: {self._r16ui_disabled_reason}"
            )

        start_time = time.perf_counter()

        y_height, y_width = y.shape[:2]
        u_height, u_width = u.shape[:2]

        t_prepare_start = time.perf_counter()
        y_data = np.ascontiguousarray(y)
        u_data = np.ascontiguousarray(u)
        v_data = np.ascontiguousarray(v)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        y_tex = self._r16ui_pool.acquire_y_texture(y_width, y_height, frame_index)
        u_tex = self._r16ui_pool.acquire_u_texture(u_width, u_height, frame_index)
        v_tex = self._r16ui_pool.acquire_v_texture(u_width, u_height, frame_index)

        if y_tex is None or u_tex is None or v_tex is None:
            raise RuntimeError("Failed to acquire R16UI YUV420 textures")

        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(u_tex, u_data)
        self._upload_to_texture(v_tex, v_data)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        self._pool.update_upload_stats(y_data.nbytes, u_data.nbytes + v_data.nbytes, upload_time_ms)
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'r16ui_direct',
        }
        self._last_upload_mode = 'r16ui_direct'

        if a is not None:
            a_tex = self._upload_alpha_plane(
                a, frame_index, alpha_bit_depth or bit_depth, 'r16ui_direct')
            if a_tex is not None:
                return (y_tex, u_tex, v_tex, a_tex)

        return (y_tex, u_tex, v_tex)

    def upload_yuv422_r16ui(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        frame_index: int,
        bit_depth: int = 10,
        a: Optional[np.ndarray] = None,
        alpha_bit_depth: Optional[int] = None,
    ) -> tuple:
        """上传 YUV422P 平面到 GPU 使用 R16UI/usampler2D 纹理。"""
        if not self._r16ui_self_test_passed or self._r16ui_pool is None:
            raise RuntimeError(
                f"R16UI upload path is disabled: {self._r16ui_disabled_reason}"
            )

        start_time = time.perf_counter()

        y_height, y_width = y.shape[:2]
        u_height, u_width = u.shape[:2]
        assert u_width == (y_width + 1) // 2, "U width mismatch"
        assert v.shape == u.shape, "V shape mismatch"
        assert u_height == y_height, "U height mismatch"

        t_prepare_start = time.perf_counter()
        y_data = np.ascontiguousarray(y)
        u_data = np.ascontiguousarray(u)
        v_data = np.ascontiguousarray(v)
        t_prepare_ms = (time.perf_counter() - t_prepare_start) * 1000.0

        y_tex = self._r16ui_pool.acquire_y_texture(y_width, y_height, frame_index)
        u_tex = self._r16ui_pool.acquire_u_texture(u_width, u_height, frame_index)
        v_tex = self._r16ui_pool.acquire_v_texture(u_width, u_height, frame_index)

        if y_tex is None or u_tex is None or v_tex is None:
            raise RuntimeError("Failed to acquire R16UI YUV422 textures")

        t_write_start = time.perf_counter()
        self._upload_to_texture(y_tex, y_data)
        self._upload_to_texture(u_tex, u_data)
        self._upload_to_texture(v_tex, v_data)
        t_write_ms = (time.perf_counter() - t_write_start) * 1000.0

        upload_time_ms = (time.perf_counter() - start_time) * 1000.0
        self._pool.update_upload_stats(y_data.nbytes, u_data.nbytes + v_data.nbytes, upload_time_ms)
        self._last_upload_timing = {
            'prepare_ms': t_prepare_ms,
            'write_ms': t_write_ms,
            'total_ms': upload_time_ms,
            'mode': 'r16ui_direct',
        }
        self._last_upload_mode = 'r16ui_direct'

        if a is not None:
            a_tex = self._upload_alpha_plane(
                a, frame_index, alpha_bit_depth or bit_depth, 'r16ui_direct')
            if a_tex is not None:
                return (y_tex, u_tex, v_tex, a_tex)

        return (y_tex, u_tex, v_tex)

    def is_r16_available(self) -> bool:
        """检查 R16UI 模式是否可用（probe + self-test 都通过）。"""
        return self._r16ui_self_test_passed

    def get_upload_mode(self, bit_depth: int) -> str:
        """
        根据位深度获取推荐上传模式。

        - 8-bit: r8_direct（uint8 直传，跳过 float16 转换）
        - 10/12/16-bit + R16UI passed: r16ui_direct（usampler2D）
        - 10/12/16-bit + R16UI failed: float16（传统路径）
        """
        if bit_depth <= 8:
            return 'r8_direct'
        if self._r16ui_self_test_passed:
            return 'r16ui_direct'
        return 'float16'

    def upload_yuv_planar(
        self,
        y: np.ndarray,
        u: np.ndarray,
        v: np.ndarray,
        frame_index: int,
        bit_depth: int = 8,
        subsampling: str = '444',
        a: Optional[np.ndarray] = None,
        alpha_bit_depth: Optional[int] = None,
    ) -> tuple:
        """
        智能选择上传策略的统一入口。

        根据位深度和平台自动选择最优上传路径：
        - 8-bit: r8_direct（uint8 直传，跳过 float16 转换）
        - 10/12/16-bit + R16UI passed: r16ui_direct/usampler2D
        - 10/12/16-bit + R16UI failed: float16 归一化（传统路径）

        a/alpha_bit_depth: 可选第四平面（YUVA Alpha，全分辨率，位深独立）。
        """
        mode = self.get_upload_mode(bit_depth)

        if mode == 'r8_direct':
            self._last_upload_mode = 'r8_direct'
            if subsampling == '420':
                return self._upload_planar_r8(y, u, v, frame_index,
                                              u_width=y.shape[1] // 2, u_height=y.shape[0] // 2,
                                              a=a, subsampling='420',
                                              alpha_bit_depth=alpha_bit_depth)
            elif subsampling == '422':
                return self._upload_planar_r8(y, u, v, frame_index,
                                              u_width=(y.shape[1] + 1) // 2, u_height=y.shape[0],
                                              a=a, subsampling='422',
                                              alpha_bit_depth=alpha_bit_depth)
            else:
                return self._upload_planar_r8(y, u, v, frame_index,
                                              u_width=y.shape[1], u_height=y.shape[0],
                                              a=a, subsampling='444',
                                              alpha_bit_depth=alpha_bit_depth)
        elif mode == 'r16ui_direct':
            if subsampling == '420':
                return self.upload_yuv420_r16ui(y, u, v, frame_index, bit_depth,
                                                a=a, alpha_bit_depth=alpha_bit_depth)
            elif subsampling == '422':
                return self.upload_yuv422_r16ui(y, u, v, frame_index, bit_depth,
                                                a=a, alpha_bit_depth=alpha_bit_depth)
            else:
                return self.upload_yuv444_r16ui(y, u, v, frame_index, bit_depth,
                                                a=a, alpha_bit_depth=alpha_bit_depth)
        else:
            self._last_upload_mode = 'float16'
            if subsampling == '420':
                return self.upload_yuv420(y, u, v, frame_index, bit_depth,
                                          a=a, alpha_bit_depth=alpha_bit_depth)
            elif subsampling == '422':
                return self.upload_yuv422(y, u, v, frame_index, bit_depth,
                                          a=a, alpha_bit_depth=alpha_bit_depth)
            else:
                return self.upload_yuv444(y, u, v, frame_index, bit_depth,
                                          a=a, alpha_bit_depth=alpha_bit_depth)

    def get_yuv_shader(self) -> moderngl.Program:
        """
        获取 YUV -> RGB 转换 shader。

        Returns:
            ModernGL Program 对象
        """
        if self._program is not None:
            return self._program

        # 创建 shader
        self._program = self._create_yuv_program()
        return self._program

    def _create_yuv_program(self) -> moderngl.Program:
        """创建 YUV shader program，支持多 bit depth 的 proper range 处理"""
        vs = """
        #version 330

        in vec2 in_vert;
        in vec2 in_texcoord;
        out vec2 v_texcoord;

        void main() {
            gl_Position = vec4(in_vert, 0.0, 1.0);
            v_texcoord = in_texcoord;
        }
        """

        fs = """
        #version 330

        uniform sampler2D y_plane;
        uniform sampler2D u_plane;
        uniform sampler2D v_plane;
        uniform sampler2D a_plane;        // Alpha plane (full resolution, CPU pre-normalized)
        uniform int yuv_matrix;      // 0=BT.709, 1=BT.2020, 2=BT.601
        uniform int color_range;     // 0=limited, 1=full
        uniform int bit_depth;       // 8, 10, 12, 16
        uniform int has_alpha;            // 0=opaque (alpha=1.0), 1=sample a_plane
        uniform int alpha_premultiplied;  // 1=rgb pre-multiplied by alpha → unpremultiply
        uniform int gbr_mode;             // Topos GBR plane order is G, B, R
        uniform vec2 source_uv_scale;   // Viewport crop scale in source image UV
        uniform vec2 source_uv_offset;  // Viewport crop offset in source image UV
        uniform vec2 chroma_uv_scale;  // Chroma UV coordinate scale (default 1.0, 1.0 for independent textures)
        uniform vec2 chroma_uv_offset; // Chroma UV coordinate offset (default 0.0, 0.0)

        in vec2 v_texcoord;
        out vec4 f_color;

        // BT.709 matrix (HD video)
        const mat3 bt709 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1873, 1.8556,
            1.5748, -0.4681, 0.0
        );

        // BT.2020 matrix (HDR)
        const mat3 bt2020 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1646, 1.8814,
            1.4746, -0.5714, 0.0
        );

        // BT.601 matrix (SD video)。AUD-39800: 数字 YCbCr 约定——shader 的
        // 色度归一化为 (C-128<<s)/(224<<s)，此约定下标准 601 系数为
        // R:+1.402 / G:-0.3441·U-0.7141·V / B:+1.772（Kr=0.299, Kb=0.114）。
        // 旧值是 PAL 模拟 YUV 矩阵系数，误用会导致 SD 内容颜色系统性偏移
        // （红欠饱和约 19%、蓝过饱和约 15%）。
        const mat3 bt601 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.3441, 1.7720,
            1.4020, -0.7141, 0.0
        );

        void main() {
            vec2 source_uv = v_texcoord * source_uv_scale + source_uv_offset;
            if (source_uv.x < 0.0 || source_uv.x > 1.0 || source_uv.y < 0.0 || source_uv.y > 1.0) {
                f_color = vec4(0.0, 0.0, 0.0, 0.0);
                return;
            }

            // 读取 Y 平面
            float y = texture(y_plane, source_uv).r;

            // 读取 U/V 平面（独立纹理，纹理坐标已归一化到该纹理）
            vec2 chroma_uv = source_uv * chroma_uv_scale + chroma_uv_offset;
            float u = texture(u_plane, chroma_uv).r;
            float v = texture(v_plane, chroma_uv).r;

            // GBR stores full-range G/B/R planes and bypasses YUV range/matrix.
            vec3 rgb;
            if (gbr_mode == 1) {
                rgb = vec3(v, y, u); // Topos plane order: G, B, R
            } else {
            // Range 处理 — 基于 bit_depth 计算正确的归一化参数
            if (color_range == 0) {
                // Limited range (TV)
                // Y: black = 16<<(depth-8), range = 219<<(depth-8)
                // Cb/Cr: neutral = 128<<(depth-8), range = 224<<(depth-8)
                float shift = float(bit_depth - 8);
                float nominal_max = pow(2.0, float(bit_depth)) - 1.0;
                float y_black = 16.0 * exp2(shift) / nominal_max;
                float y_range_inv = nominal_max / (219.0 * exp2(shift));
                float c_neutral = 128.0 * exp2(shift) / nominal_max;
                float c_range_inv = nominal_max / (224.0 * exp2(shift));

                y = (y - y_black) * y_range_inv;
                u = (u - c_neutral) * c_range_inv;
                v = (v - c_neutral) * c_range_inv;
            } else {
                // Full range (PC)
                u = u - 0.5;
                v = v - 0.5;
            }

            // 选择矩阵
            mat3 matrix = bt709;
            if (yuv_matrix == 1) {
                matrix = bt2020;
            } else if (yuv_matrix == 2) {
                matrix = bt601;
            }

            // YUV -> RGB
            rgb = matrix * vec3(y, u, v);
            }

            // Alpha 采样（全分辨率，与 Y 同 UV；float 路径 CPU 已归一化）
            float alpha = 1.0;
            if (has_alpha == 1) {
                alpha = texture(a_plane, source_uv).r;
            }

            // 源为预乘 Alpha 时先转 straight（内部约定：全程 straight）
            vec3 out_rgb = rgb;
            if (alpha_premultiplied == 1) {
                out_rgb = (alpha > 0.0001) ? rgb / alpha : vec3(0.0);
            }

            f_color = vec4(out_rgb, alpha);
        }
        """

        self._program = self._ctx.program(vertex_shader=vs, fragment_shader=fs)
        self._program['source_uv_scale'].value = (1.0, 1.0)
        self._program['source_uv_offset'].value = (0.0, 0.0)
        self._program['chroma_uv_scale'].value = (1.0, 1.0)
        self._program['chroma_uv_offset'].value = (0.0, 0.0)
        self._program['bit_depth'].value = 8
        self._program['has_alpha'].value = 0
        self._program['alpha_premultiplied'].value = 0
        self._program['gbr_mode'].value = 0
        self._program['a_plane'].value = 3

        # 初始化 quad buffer
        self._init_quad_buffer()

        return self._program

    def _create_r16ui_yuv_program(self) -> moderngl.Program:
        """
        创建 R16UI/usampler2D YUV -> RGB 转换 shader。

        使用 usampler2D 读取 uint16 原始码值，
        shader 内归一化 float(code) / float((1 << bit_depth) - 1)，
        再走标准 BT.709/BT.2020/BT.601 limited/full range 逻辑。

        Returns:
            ModernGL Program 对象
        """
        vs = """
        #version 330

        in vec2 in_vert;
        in vec2 in_texcoord;
        out vec2 v_texcoord;

        void main() {
            gl_Position = vec4(in_vert, 0.0, 1.0);
            v_texcoord = in_texcoord;
        }
        """

        fs = """
        #version 330

        uniform usampler2D y_plane;   // R16UI: reads uint, not float
        uniform usampler2D u_plane;
        uniform usampler2D v_plane;
        uniform usampler2D a_plane;   // R16UI alpha: raw uint codes (full resolution)
        uniform int yuv_matrix;       // 0=BT.709, 1=BT.2020, 2=BT.601
        uniform int color_range;      // 0=limited, 1=full
        uniform int bit_depth;        // 8, 10, 12, 16
        uniform int alpha_bit_depth;  // Alpha 位深独立于 luma（topos a8/a10/a12/a16 vs 10）
        uniform int has_alpha;            // 0=opaque (alpha=1.0), 1=sample a_plane
        uniform int alpha_premultiplied;  // 1=rgb pre-multiplied by alpha → unpremultiply
        uniform int gbr_mode;             // Topos GBR plane order is G, B, R
        uniform vec2 source_uv_scale;
        uniform vec2 source_uv_offset;
        uniform vec2 chroma_uv_scale;
        uniform vec2 chroma_uv_offset;

        in vec2 v_texcoord;
        out vec4 f_color;

        const mat3 bt709 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1873, 1.8556,
            1.5748, -0.4681, 0.0
        );

        const mat3 bt2020 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1646, 1.8814,
            1.4746, -0.5714, 0.0
        );

        const mat3 bt601 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.3441, 1.7720,
            1.4020, -0.7141, 0.0
        );

        void main() {
            vec2 source_uv = v_texcoord * source_uv_scale + source_uv_offset;
            if (source_uv.x < 0.0 || source_uv.x > 1.0 || source_uv.y < 0.0 || source_uv.y > 1.0) {
                f_color = vec4(0.0, 0.0, 0.0, 0.0);
                return;
            }

            // Read raw uint code values from integer textures
            uint y_code = texture(y_plane, source_uv).r;
            vec2 chroma_uv = source_uv * chroma_uv_scale + chroma_uv_offset;
            uint u_code = texture(u_plane, chroma_uv).r;
            uint v_code = texture(v_plane, chroma_uv).r;

            // Normalize to 0..1 based on bit depth
            float max_val = pow(2.0, float(bit_depth)) - 1.0;
            float y = float(y_code) / max_val;
            float u = float(u_code) / max_val;
            float v = float(v_code) / max_val;

            // GBR stores full-range G/B/R planes and bypasses YUV range/matrix.
            vec3 rgb;
            if (gbr_mode == 1) {
                rgb = vec3(v, y, u); // Topos plane order: G, B, R
            } else {
            // Range handling — same logic as float sampler
            if (color_range == 0) {
                // Limited range (TV)
                float shift = float(bit_depth - 8);
                float y_black = 16.0 * exp2(shift) / max_val;
                float y_range_inv = max_val / (219.0 * exp2(shift));
                float c_neutral = 128.0 * exp2(shift) / max_val;
                float c_range_inv = max_val / (224.0 * exp2(shift));

                y = (y - y_black) * y_range_inv;
                u = (u - c_neutral) * c_range_inv;
                v = (v - c_neutral) * c_range_inv;
            } else {
                // Full range (PC)
                u = u - 0.5;
                v = v - 0.5;
            }

            // Select matrix
            mat3 matrix = bt709;
            if (yuv_matrix == 1) {
                matrix = bt2020;
            } else if (yuv_matrix == 2) {
                matrix = bt601;
            }

            // YUV -> RGB
            rgb = matrix * vec3(y, u, v);
            }

            // Alpha 采样：原始码值按 alpha_bit_depth 归一化（独立于 luma 位深）
            float alpha = 1.0;
            if (has_alpha == 1) {
                float a_max = pow(2.0, float(alpha_bit_depth)) - 1.0;
                alpha = float(texture(a_plane, source_uv).r) / a_max;
            }

            // 源为预乘 Alpha 时先转 straight（内部约定：全程 straight）
            vec3 out_rgb = rgb;
            if (alpha_premultiplied == 1) {
                out_rgb = (alpha > 0.0001) ? rgb / alpha : vec3(0.0);
            }

            f_color = vec4(out_rgb, alpha);
        }
        """

        self._r16ui_program = self._ctx.program(vertex_shader=vs, fragment_shader=fs)
        self._r16ui_program['source_uv_scale'].value = (1.0, 1.0)
        self._r16ui_program['source_uv_offset'].value = (0.0, 0.0)
        self._r16ui_program['chroma_uv_scale'].value = (1.0, 1.0)
        self._r16ui_program['chroma_uv_offset'].value = (0.0, 0.0)
        self._r16ui_program['bit_depth'].value = 12
        self._r16ui_program['alpha_bit_depth'].value = 12
        self._r16ui_program['has_alpha'].value = 0
        self._r16ui_program['alpha_premultiplied'].value = 0
        self._r16ui_program['gbr_mode'].value = 0
        self._r16ui_program['a_plane'].value = 3

        # Initialize quad VAO for R16UI program
        self._init_r16ui_quad()

        return self._r16ui_program

    def _init_r16ui_quad(self) -> None:
        """初始化 R16UI shader 的全屏四边形 VAO。"""
        vertices = np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
             1.0,  1.0, 1.0, 1.0,
            -1.0,  1.0, 0.0, 1.0,
        ], dtype='f4')

        indices = np.array([0, 1, 2, 0, 2, 3], dtype='i4')

        # Reuse the shared quad buffer if available
        if self._quad_buffer is None:
            self._quad_buffer = self._ctx.buffer(vertices.tobytes())
        # AUD-39810: IBO 登记为成员，随 release() 释放
        self._r16ui_quad_ibo = self._ctx.buffer(indices.tobytes())

        if self._r16ui_program:
            self._r16ui_quad_vao = self._ctx.vertex_array(
                self._r16ui_program,
                [(self._quad_buffer, '2f 2f', 'in_vert', 'in_texcoord')],
                self._r16ui_quad_ibo
            )

    def _create_nv12_r16ui_shader(self) -> moderngl.Program:
        """
        创建 NV12 R16UI/usampler2D YUV -> RGB 转换 shader。

        使用 usampler2D 读取 uint16 原始码值，NV12 2-plane 布局（Y + interleaved UV）。
        shader 内归一化 float(code) / float((1 << bit_depth) - 1)，
        再走标准 BT.709/BT.2020/BT.601 limited/full range 逻辑。

        Returns:
            ModernGL Program 对象
        """
        vs = """
        #version 330

        in vec2 in_vert;
        in vec2 in_texcoord;
        out vec2 v_texcoord;

        void main() {
            gl_Position = vec4(in_vert, 0.0, 1.0);
            v_texcoord = in_texcoord;
        }
        """

        fs = """
        #version 330

        uniform usampler2D y_plane;    // R16UI: reads uint, not float
        uniform usampler2D uv_plane;   // R16UI RG: interleaved UV as uint (U=R, V=G)
        uniform usampler2D a_plane;    // R16UI alpha: raw uint codes (full resolution)
        uniform int yuv_matrix;        // 0=BT.709, 1=BT.2020, 2=BT.601
        uniform int color_range;       // 0=limited, 1=full
        uniform int bit_depth;         // 8, 10, 12, 16
        uniform int alpha_bit_depth;   // Alpha 位深（NV12 本身无第四平面，预留一致接口）
        uniform int has_alpha;            // 0=opaque (alpha=1.0), 1=sample a_plane
        uniform int alpha_premultiplied;  // 1=rgb pre-multiplied by alpha → unpremultiply
        uniform vec2 source_uv_scale;
        uniform vec2 source_uv_offset;
        uniform vec2 chroma_uv_scale;
        uniform vec2 chroma_uv_offset;

        in vec2 v_texcoord;
        out vec4 f_color;

        const mat3 bt709 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1873, 1.8556,
            1.5748, -0.4681, 0.0
        );

        const mat3 bt2020 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1646, 1.8814,
            1.4746, -0.5714, 0.0
        );

        const mat3 bt601 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.3441, 1.7720,
            1.4020, -0.7141, 0.0
        );

        void main() {
            vec2 source_uv = v_texcoord * source_uv_scale + source_uv_offset;
            if (source_uv.x < 0.0 || source_uv.x > 1.0 || source_uv.y < 0.0 || source_uv.y > 1.0) {
                f_color = vec4(0.0, 0.0, 0.0, 0.0);
                return;
            }

            // Read raw uint code values from integer textures
            uint y_code = texture(y_plane, source_uv).r;
            vec2 chroma_uv = source_uv * chroma_uv_scale + chroma_uv_offset;
            uvec2 uv_code = texture(uv_plane, chroma_uv).rg;  // U=R, V=G as uint
            uint u_code = uv_code.r;
            uint v_code = uv_code.g;

            // Normalize to 0..1 based on bit depth
            float max_val = pow(2.0, float(bit_depth)) - 1.0;
            float y = float(y_code) / max_val;
            float u = float(u_code) / max_val;
            float v = float(v_code) / max_val;

            // Range handling
            if (color_range == 0) {
                // Limited range (TV)
                float shift = float(bit_depth - 8);
                float y_black = 16.0 * exp2(shift) / max_val;
                float y_range_inv = max_val / (219.0 * exp2(shift));
                float c_neutral = 128.0 * exp2(shift) / max_val;
                float c_range_inv = max_val / (224.0 * exp2(shift));

                y = (y - y_black) * y_range_inv;
                u = (u - c_neutral) * c_range_inv;
                v = (v - c_neutral) * c_range_inv;
            } else {
                // Full range (PC)
                u = u - 0.5;
                v = v - 0.5;
            }

            // Select matrix
            mat3 matrix = bt709;
            if (yuv_matrix == 1) {
                matrix = bt2020;
            } else if (yuv_matrix == 2) {
                matrix = bt601;
            }

            // YUV -> RGB
            vec3 rgb = matrix * vec3(y, u, v);

            // Alpha 采样（NV12 源本身无第四平面，has_alpha=0 时零开销）
            float alpha = 1.0;
            if (has_alpha == 1) {
                float a_max = pow(2.0, float(alpha_bit_depth)) - 1.0;
                alpha = float(texture(a_plane, source_uv).r) / a_max;
            }

            vec3 out_rgb = rgb;
            if (alpha_premultiplied == 1) {
                out_rgb = (alpha > 0.0001) ? rgb / alpha : vec3(0.0);
            }

            f_color = vec4(out_rgb, alpha);
        }
        """

        self._nv12_r16ui_program = self._ctx.program(vertex_shader=vs, fragment_shader=fs)
        self._nv12_r16ui_program['source_uv_scale'].value = (1.0, 1.0)
        self._nv12_r16ui_program['source_uv_offset'].value = (0.0, 0.0)
        self._nv12_r16ui_program['chroma_uv_scale'].value = (1.0, 1.0)
        self._nv12_r16ui_program['chroma_uv_offset'].value = (0.0, 0.0)
        self._nv12_r16ui_program['bit_depth'].value = 10
        self._nv12_r16ui_program['alpha_bit_depth'].value = 10
        self._nv12_r16ui_program['has_alpha'].value = 0
        self._nv12_r16ui_program['alpha_premultiplied'].value = 0
        self._nv12_r16ui_program['a_plane'].value = 2

        # Build VAO from quad vertices
        vertices = np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
             1.0,  1.0, 1.0, 1.0,
            -1.0,  1.0, 0.0, 1.0,
        ], dtype='f4')
        indices = np.array([0, 1, 2, 0, 2, 3], dtype='i4')

        if self._quad_buffer is None:
            self._quad_buffer = self._ctx.buffer(vertices.tobytes())
        # AUD-39810: IBO 登记为成员，随 release() 释放
        self._nv12_r16ui_ibo = self._ctx.buffer(indices.tobytes())

        self._nv12_r16ui_vao = self._ctx.vertex_array(
            self._nv12_r16ui_program,
            [(self._quad_buffer, '2f 2f', 'in_vert', 'in_texcoord')],
            self._nv12_r16ui_ibo
        )

        return self._nv12_r16ui_program

    def get_nv12_shader(self) -> moderngl.Program:
        """
        获取 NV12 -> RGB 转换 shader。

        NV12 使用 interleaved UV 纹理，需要特殊处理。

        G-P0-01: 程序只编译一次并缓存到 self._nv12_program，随 uploader
        生命周期在 release() 中统一释放。每帧重编译会同时造成编译尖峰、
        以及 AsyncRenderer._render_quad VAO cache（按 program.glo 索引）的
        无界增长。

        Returns:
            ModernGL Program 对象
        """
        # G-P0-01: 命中缓存直接返回，避免每帧重编译。
        if self._nv12_program is not None:
            return self._nv12_program

        vs = """
        #version 330

        in vec2 in_vert;
        in vec2 in_texcoord;
        out vec2 v_texcoord;

        void main() {
            gl_Position = vec4(in_vert, 0.0, 1.0);
            v_texcoord = in_texcoord;
        }
        """

        fs = """
        #version 330

        uniform sampler2D y_plane;
        uniform sampler2D uv_plane;   // interleaved UV (RG format)
        uniform sampler2D a_plane;    // Alpha plane (full resolution, CPU pre-normalized)
        uniform int yuv_matrix;
        uniform int color_range;
        uniform int bit_depth;       // 8, 10, 12, 16
        uniform int has_alpha;            // 0=opaque (alpha=1.0), 1=sample a_plane
        uniform int alpha_premultiplied;  // 1=rgb pre-multiplied by alpha → unpremultiply
        uniform vec2 source_uv_scale;   // Viewport crop scale in source image UV
        uniform vec2 source_uv_offset;  // Viewport crop offset in source image UV
        uniform vec2 chroma_uv_scale;  // Chroma UV coordinate scale (default 1.0, 1.0)
        uniform vec2 chroma_uv_offset; // Chroma UV coordinate offset (default 0.0, 0.0)

        in vec2 v_texcoord;
        out vec4 f_color;

        const mat3 bt709 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1873, 1.8556,
            1.5748, -0.4681, 0.0
        );

        const mat3 bt2020 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.1646, 1.8814,
            1.4746, -0.5714, 0.0
        );

        const mat3 bt601 = mat3(
            1.0, 1.0, 1.0,
            0.0, -0.3441, 1.7720,
            1.4020, -0.7141, 0.0
        );

        void main() {
            vec2 source_uv = v_texcoord * source_uv_scale + source_uv_offset;
            if (source_uv.x < 0.0 || source_uv.x > 1.0 || source_uv.y < 0.0 || source_uv.y > 1.0) {
                f_color = vec4(0.0, 0.0, 0.0, 0.0);
                return;
            }

            // 读取 Y
            float y = texture(y_plane, source_uv).r;

            // 读取 interleaved UV（独立纹理，纹理坐标已归一化）
            vec2 chroma_uv = source_uv * chroma_uv_scale + chroma_uv_offset;
            vec2 uv = texture(uv_plane, chroma_uv).rg;  // U = R, V = G
            float u = uv.r;
            float v = uv.g;

            // Range 处理 — 基于 bit_depth 计算正确的归一化参数
            if (color_range == 0) {
                float shift = float(bit_depth - 8);
                float nominal_max = pow(2.0, float(bit_depth)) - 1.0;
                float y_black = 16.0 * exp2(shift) / nominal_max;
                float y_range_inv = nominal_max / (219.0 * exp2(shift));
                float c_neutral = 128.0 * exp2(shift) / nominal_max;
                float c_range_inv = nominal_max / (224.0 * exp2(shift));

                y = (y - y_black) * y_range_inv;
                u = (u - c_neutral) * c_range_inv;
                v = (v - c_neutral) * c_range_inv;
            } else {
                u = u - 0.5;
                v = v - 0.5;
            }

            // 选择矩阵
            mat3 matrix = bt709;
            if (yuv_matrix == 1) {
                matrix = bt2020;
            } else if (yuv_matrix == 2) {
                matrix = bt601;
            }

            vec3 rgb = matrix * vec3(y, u, v);

            // Alpha 采样（NV12 源本身无第四平面，has_alpha=0 时零开销）
            float alpha = 1.0;
            if (has_alpha == 1) {
                alpha = texture(a_plane, source_uv).r;
            }

            vec3 out_rgb = rgb;
            if (alpha_premultiplied == 1) {
                out_rgb = (alpha > 0.0001) ? rgb / alpha : vec3(0.0);
            }

            f_color = vec4(out_rgb, alpha);
        }
        """

        program = self._ctx.program(vertex_shader=vs, fragment_shader=fs)
        program['source_uv_scale'].value = (1.0, 1.0)
        program['source_uv_offset'].value = (0.0, 0.0)
        program['chroma_uv_scale'].value = (1.0, 1.0)
        program['chroma_uv_offset'].value = (0.0, 0.0)
        program['bit_depth'].value = 8
        program['has_alpha'].value = 0
        program['alpha_premultiplied'].value = 0
        program['a_plane'].value = 2
        # G-P0-01: 缓存编译产物，随 release() 统一释放。
        self._nv12_program = program
        return program

    def _init_quad_buffer(self) -> None:
        """初始化全屏四边形 buffer"""
        vertices = np.array([
            # x, y, u, v
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
             1.0,  1.0, 1.0, 1.0,
            -1.0,  1.0, 0.0, 1.0,
        ], dtype='f4')

        indices = np.array([0, 1, 2, 0, 2, 3], dtype='i4')

        self._quad_buffer = self._ctx.buffer(vertices.tobytes())
        # AUD-39810: IBO 登记为成员，随 release() 释放
        self._quad_ibo = self._ctx.buffer(indices.tobytes())

        if self._program:
            self._quad_vao = self._ctx.vertex_array(
                self._program,
                [(self._quad_buffer, '2f 2f', 'in_vert', 'in_texcoord')],
                self._quad_ibo
            )

    def _should_use_integer_sampler(self, y_tex: moderngl.Texture) -> bool:
        """AUD-39805: 按本帧 Y 平面纹理的实际 dtype 判定 usampler2D 分发。

        混合位深多轨道共享同一 uploader 时，全局 ``_last_upload_mode``
        单值会被后续上传覆盖：8-bit 轨道（R8 直传）上传后，仍持有
        R16UI 纹理的轨道再渲染会走 sampler2D 路径——采样器类型与
        integer 纹理不匹配（GL 端采样结果未定义/全黑）。

        R16UI 池纹理 dtype 为 'u2'，是逐帧可靠的信号；dtype 不可得
        （mock/非 ModernGL 对象）时回退到 ``_last_upload_mode`` 旧行为。
        """
        dtype = getattr(y_tex, 'dtype', None)
        if isinstance(dtype, str):
            return dtype == 'u2'
        return self._last_upload_mode == 'r16ui_direct'

    def render_yuv_to_rgb(
        self,
        y_tex: moderngl.Texture,
        u_tex: moderngl.Texture,
        v_tex: moderngl.Texture,
        output_fbo: moderngl.Framebuffer,
        chroma_uv_scale: Tuple[float, float] = (1.0, 1.0),
        chroma_uv_offset: Tuple[float, float] = (0.0, 0.0),
        bit_depth: int = 8,
        a_tex: Optional[moderngl.Texture] = None,
        alpha_bit_depth: int = 10,
        alpha_premultiplied: bool = False,
        gbr_mode: bool = False,
    ) -> None:
        """
        渲染 YUV 平面到 RGB FBO。

        自动根据上次上传模式选择 float sampler 或 R16UI shader。

        Args:
            y_tex: Y 平面纹理
            u_tex: U 平面纹理
            v_tex: V 平面纹理
            output_fbo: 输出 FBO
            chroma_uv_scale: Chroma UV 坐标缩放因子（默认 1.0, 1.0 用于独立纹理）
            chroma_uv_offset: Chroma UV 坐标偏移（默认 0.0, 0.0）
            bit_depth: 位深度 (8, 10, 12, 16)
            a_tex: Alpha 平面纹理（可选；None 时 shader 输出 alpha=1.0）
            alpha_bit_depth: Alpha 位深（R16UI 路径 shader 内归一化用）
            alpha_premultiplied: 源是否预乘 Alpha（True 时 shader 转 straight）
        """
        # AUD-39805: 按本帧纹理 dtype 逐帧判定，不依赖全局 _last_upload_mode
        if self._should_use_integer_sampler(y_tex):
            return self.render_yuv_r16ui_to_rgb(
                y_tex, u_tex, v_tex, output_fbo,
                chroma_uv_scale, chroma_uv_offset, bit_depth,
                a_tex=a_tex, alpha_bit_depth=alpha_bit_depth,
                alpha_premultiplied=alpha_premultiplied, gbr_mode=gbr_mode
            )

        # Float sampler path
        if self._program is None:
            self.get_yuv_shader()

        output_fbo.use()

        # 绑定纹理（unit3 无 Alpha 时绑 Y 作占位，保证 sampler 完整性）
        y_tex.use(0)
        u_tex.use(1)
        v_tex.use(2)
        (a_tex if a_tex is not None else y_tex).use(3)

        # 设置 uniform
        self._program['y_plane'].value = 0
        self._program['u_plane'].value = 1
        self._program['v_plane'].value = 2
        self._program['yuv_matrix'].value = self._yuv_matrix
        self._program['color_range'].value = self._color_range
        self._program['bit_depth'].value = bit_depth
        self._program['has_alpha'].value = 1 if a_tex is not None else 0
        self._program['alpha_premultiplied'].value = 1 if alpha_premultiplied else 0
        self._program['gbr_mode'].value = 1 if gbr_mode else 0
        self._program['source_uv_scale'].value = (1.0, 1.0)
        self._program['source_uv_offset'].value = (0.0, 0.0)
        self._program['chroma_uv_scale'].value = chroma_uv_scale
        self._program['chroma_uv_offset'].value = chroma_uv_offset

        # 渲染
        if self._quad_vao:
            self._quad_vao.render(moderngl.TRIANGLES, vertices=6)

    def render_yuv_r16ui_to_rgb(
        self,
        y_tex: moderngl.Texture,
        u_tex: moderngl.Texture,
        v_tex: moderngl.Texture,
        output_fbo: moderngl.Framebuffer,
        chroma_uv_scale: Tuple[float, float] = (1.0, 1.0),
        chroma_uv_offset: Tuple[float, float] = (0.0, 0.0),
        bit_depth: int = 12,
        source_uv_scale: Tuple[float, float] = (1.0, 1.0),
        source_uv_offset: Tuple[float, float] = (0.0, 0.0),
        a_tex: Optional[moderngl.Texture] = None,
        alpha_bit_depth: int = 10,
        alpha_premultiplied: bool = False,
        gbr_mode: bool = False,
    ) -> None:
        """
        使用 R16UI/usampler2D shader 渲染 YUV 平面到 RGB FBO。

        与 render_yuv_to_rgb 不同的是使用 usampler2D 读取 uint16 原始值。

        a_tex: 可选 Alpha 纹理（R16UI 池，原始码值）；alpha_bit_depth
        独立于 luma 的 bit_depth（Topos a8/a10/a12/a16 vs 10）。
        """
        if self._r16ui_program is None:
            self._create_r16ui_yuv_program()

        output_fbo.use()

        # 绑定纹理（unit3 无 Alpha 时绑 Y 作占位，保证 usampler 完整性）
        y_tex.use(0)
        u_tex.use(1)
        v_tex.use(2)
        (a_tex if a_tex is not None else y_tex).use(3)

        # 设置 uniform
        self._r16ui_program['y_plane'].value = 0
        self._r16ui_program['u_plane'].value = 1
        self._r16ui_program['v_plane'].value = 2
        self._r16ui_program['yuv_matrix'].value = self._yuv_matrix
        self._r16ui_program['color_range'].value = self._color_range
        self._r16ui_program['bit_depth'].value = bit_depth
        self._r16ui_program['alpha_bit_depth'].value = alpha_bit_depth
        self._r16ui_program['has_alpha'].value = 1 if a_tex is not None else 0
        self._r16ui_program['alpha_premultiplied'].value = 1 if alpha_premultiplied else 0
        self._r16ui_program['gbr_mode'].value = 1 if gbr_mode else 0
        self._r16ui_program['source_uv_scale'].value = source_uv_scale
        self._r16ui_program['source_uv_offset'].value = source_uv_offset
        self._r16ui_program['chroma_uv_scale'].value = chroma_uv_scale
        self._r16ui_program['chroma_uv_offset'].value = chroma_uv_offset

        # 渲染
        if self._r16ui_quad_vao:
            self._r16ui_quad_vao.render(moderngl.TRIANGLES, vertices=6)

    def render_nv12_r16ui_to_rgb(
        self,
        y_tex: moderngl.Texture,
        uv_tex: moderngl.Texture,
        output_fbo: moderngl.Framebuffer,
        chroma_uv_scale: Tuple[float, float] = (1.0, 1.0),
        chroma_uv_offset: Tuple[float, float] = (0.0, 0.0),
        bit_depth: int = 10,
        a_tex: Optional[moderngl.Texture] = None,
        alpha_bit_depth: int = 10,
        alpha_premultiplied: bool = False,
    ) -> None:
        """
        使用 NV12 R16UI/usampler2D shader 渲染 NV12 平面到 RGB FBO。

        NV12 2-plane 布局：Y 平面 + interleaved UV 平面，均使用 usampler2D 读取 uint16。

        Args:
            y_tex: Y 平面纹理 (R16UI)
            uv_tex: interleaved UV 纹理 (RG16UI)
            output_fbo: 输出 FBO
            chroma_uv_scale: Chroma UV 坐标缩放因子
            chroma_uv_offset: Chroma UV 坐标偏移
            bit_depth: 位深度 (10, 12, 16)
            a_tex: 可选 Alpha 纹理（R16UI；NV12 源通常无，预留一致接口）
        """
        if self._nv12_r16ui_program is None:
            self._create_nv12_r16ui_shader()

        output_fbo.use()

        # 绑定纹理（unit2 无 Alpha 时绑 Y 作占位）
        y_tex.use(0)
        uv_tex.use(1)
        (a_tex if a_tex is not None else y_tex).use(2)

        # 设置 uniform
        self._nv12_r16ui_program['y_plane'].value = 0
        self._nv12_r16ui_program['uv_plane'].value = 1
        self._nv12_r16ui_program['yuv_matrix'].value = self._yuv_matrix
        self._nv12_r16ui_program['color_range'].value = self._color_range
        self._nv12_r16ui_program['bit_depth'].value = bit_depth
        self._nv12_r16ui_program['alpha_bit_depth'].value = alpha_bit_depth
        self._nv12_r16ui_program['has_alpha'].value = 1 if a_tex is not None else 0
        self._nv12_r16ui_program['alpha_premultiplied'].value = 1 if alpha_premultiplied else 0
        self._nv12_r16ui_program['source_uv_scale'].value = (1.0, 1.0)
        self._nv12_r16ui_program['source_uv_offset'].value = (0.0, 0.0)
        self._nv12_r16ui_program['chroma_uv_scale'].value = chroma_uv_scale
        self._nv12_r16ui_program['chroma_uv_offset'].value = chroma_uv_offset

        # 渲染
        if self._nv12_r16ui_vao:
            self._nv12_r16ui_vao.render(moderngl.TRIANGLES, vertices=6)

    def _get_nv12_quad_vao(self, shader: moderngl.Program) -> moderngl.VertexArray:
        """获取（或按需创建并缓存）NV12 渲染用的全屏四边形 VAO。

        AUD-39806: VBO/IBO 只创建一次、VAO 按 shader 对象缓存复用，
        消除 render_nv12_to_rgb 的每帧 GPU 对象 churn；render 抛异常
        时不再有临时对象可泄漏（缓存对象随 release() 统一释放）。
        """
        cached = self._nv12_vao_cache.get(id(shader))
        if cached is not None:
            return cached[1]

        vertices = np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
             1.0,  1.0, 1.0, 1.0,
            -1.0,  1.0, 0.0, 1.0,
        ], dtype='f4')

        if self._quad_buffer is None:
            self._quad_buffer = self._ctx.buffer(vertices.tobytes())
        if self._nv12_quad_ibo is None:
            indices = np.array([0, 1, 2, 0, 2, 3], dtype='i4')
            self._nv12_quad_ibo = self._ctx.buffer(indices.tobytes())

        # 外部可能传入不同的 shader（内置缓存 shader 之外的场景少见）；
        # 缓存以 shader 对象 id 为键并持有强引用，防止 id 复用误命中。
        # 超过上限（异常场景下外部每帧换 shader）时整体释放重建，避免无界增长。
        if len(self._nv12_vao_cache) >= 8:
            self._release_nv12_quad_resources()

        vao = self._ctx.vertex_array(
            shader,
            [(self._quad_buffer, '2f 2f', 'in_vert', 'in_texcoord')],
            self._nv12_quad_ibo
        )
        self._nv12_vao_cache[id(shader)] = (shader, vao)
        return vao

    def _release_nv12_quad_resources(self) -> None:
        """释放 NV12 float 路径的缓存 VAO 与共享 IBO（保留 quad VBO——其他路径共用）。"""
        for _shader, vao in self._nv12_vao_cache.values():
            try:
                vao.release()
            except Exception as e:
                logger.debug(f"YUVGPUUploader: release NV12 VAO failed: {e}")
        self._nv12_vao_cache.clear()
        if self._nv12_quad_ibo is not None:
            try:
                self._nv12_quad_ibo.release()
            except Exception as e:
                logger.debug(f"YUVGPUUploader: release NV12 IBO failed: {e}")
            self._nv12_quad_ibo = None

    def render_nv12_to_rgb(
        self,
        y_tex: moderngl.Texture,
        uv_tex: moderngl.Texture,
        output_fbo: moderngl.Framebuffer,
        shader: Optional[moderngl.Program] = None,
        chroma_uv_scale: Tuple[float, float] = (1.0, 1.0),
        chroma_uv_offset: Tuple[float, float] = (0.0, 0.0),
        bit_depth: int = 8,
        a_tex: Optional[moderngl.Texture] = None,
        alpha_premultiplied: bool = False,
    ) -> None:
        """
        渲染 NV12 平面到 RGB FBO。

        Args:
            y_tex: Y 平面纹理
            uv_tex: interleaved UV 纹理
            output_fbo: 输出 FBO
            shader: NV12 shader（如果为 None，自动创建）
            chroma_uv_scale: Chroma UV 坐标缩放因子（默认 1.0, 1.0 用于独立纹理）
            chroma_uv_offset: Chroma UV 坐标偏移（默认 0.0, 0.0）
            bit_depth: 位深度 (8, 10, 12, 16)
            a_tex: 可选 Alpha 纹理（NV12 源通常无；仅对内建 shader 生效，
                外部传入的旧版 shader 无 alpha uniform 时按不透明渲染）

        注意：
            对于 NV12 的独立 UV 纹理，纹理坐标已归一化到 [0, 1]，
            直接使用 v_texcoord 即可，无需额外的 subsampling 缩放。
        """
        # AUD-39805: 按本帧纹理 dtype 逐帧判定，不依赖全局 _last_upload_mode
        if self._should_use_integer_sampler(y_tex):
            return self.render_nv12_r16ui_to_rgb(
                y_tex, uv_tex, output_fbo,
                chroma_uv_scale, chroma_uv_offset, bit_depth,
                a_tex=a_tex, alpha_premultiplied=alpha_premultiplied
            )

        if shader is None:
            shader = self.get_nv12_shader()

        output_fbo.use()

        y_tex.use(0)
        uv_tex.use(1)
        (a_tex if a_tex is not None else y_tex).use(2)

        shader['y_plane'].value = 0
        shader['uv_plane'].value = 1
        shader['yuv_matrix'].value = self._yuv_matrix
        shader['color_range'].value = self._color_range
        shader['bit_depth'].value = bit_depth
        # 外部旧版 shader 无 alpha uniform（KeyError）时按不透明渲染
        try:
            shader['has_alpha'].value = 1 if a_tex is not None else 0
            shader['alpha_premultiplied'].value = 1 if alpha_premultiplied else 0
        except KeyError:
            pass
        shader['source_uv_scale'].value = (1.0, 1.0)
        shader['source_uv_offset'].value = (0.0, 0.0)
        shader['chroma_uv_scale'].value = chroma_uv_scale
        shader['chroma_uv_offset'].value = chroma_uv_offset

        # AUD-39806: 复用缓存的 quad VAO（共享 VBO/IBO），消除每帧
        # 创建/销毁临时 VBO/IBO/VAO 的 GPU 对象 churn 与异常泄漏。
        vao = self._get_nv12_quad_vao(shader)
        vao.render(moderngl.TRIANGLES, vertices=6)

    def set_yuv_matrix(self, matrix: int) -> None:
        """
        设置 YUV 转换矩阵。

        Args:
            matrix: MATRIX_BT709, MATRIX_BT2020, 或 MATRIX_BT601
        """
        if matrix in (self.MATRIX_BT709, self.MATRIX_BT2020, self.MATRIX_BT601):
            self._yuv_matrix = matrix
        else:
            logger.warning(f"Unknown YUV matrix: {matrix}, using BT.709")
            self._yuv_matrix = self.MATRIX_BT709

    def set_color_range(self, range_type: int) -> None:
        """
        设置颜色范围。

        Args:
            range_type: RANGE_LIMITED 或 RANGE_FULL
        """
        if range_type in (self.RANGE_LIMITED, self.RANGE_FULL):
            self._color_range = range_type
        else:
            logger.warning(f"Unknown color range: {range_type}, using limited")
            self._color_range = self.RANGE_LIMITED

    def get_pool_stats(self) -> Dict[str, Any]:
        """获取纹理池统计"""
        return self._pool.get_stats()

    def get_upload_stats(self) -> YUVUploadStats:
        """获取上传统计"""
        return self._pool.get_upload_stats()

    def calculate_bandwidth_saved(
        self,
        width: int,
        height: int,
        subsampling: str = '420'
    ) -> float:
        """
        计算带宽节省量。

        Args:
            width: 帧宽度
            height: 帧高度
            subsampling: 子采样类型 ('420', '422', '444')

        Returns:
            节省的 MB 数
        """
        # RGB 数据大小（假设 float16）
        rgb_bytes = width * height * 3 * 2  # float16 = 2 bytes

        # YUV 数据大小
        if subsampling == '420':
            yuv_bytes = width * height * 2 + (width // 2) * (height // 2) * 2 * 2
        elif subsampling == '422':
            yuv_bytes = width * height * 2 + (width // 2) * height * 2 * 2
        elif subsampling == '444':
            yuv_bytes = width * height * 2 * 3
        else:
            yuv_bytes = width * height * 2 * 3

        saved_bytes = rgb_bytes - yuv_bytes
        return saved_bytes / (1024 * 1024)  # MB

    def run_r16ui_self_test(self) -> bool:
        """
        运行 R16UI 12-bit YUV 颜色自检。

        测试流程：
        1. 创建 4x4 12-bit YUV444P 合成数据 (black, gray, white, red, blue)
        2. 通过 R16UI 路径上传 (usampler2D)
        3. 通过 R16UI shader 渲染到 FBO
        4. 读回像素并验证颜色正确性
        5. 如果通过，设置 _r16ui_self_test_passed = True

        必须在渲染线程、有效 GL 上下文环境下调用。

        Returns:
            True 如果自检通过，R16UI 可安全启用
        """
        if not self._r16ui_probe_ok:
            self._r16ui_disabled_reason = "R16UI texture creation probe failed"
            logger.info(f"YUVGPUUploader: R16UI self-test skipped: {self._r16ui_disabled_reason}")
            return False

        # AUD-39802: 自检不得污染共享状态——渲染自检强制 BT.709/limited，
        # 此前覆盖 _yuv_matrix/_color_range 后不恢复，真实首帧会以自检
        # 参数渲染（如 BT.601/full 内容首帧颜色错误）。保存并在 finally 恢复。
        saved_yuv_matrix = self._yuv_matrix
        saved_color_range = self._color_range

        # AUD-39803: 异常路径同样要清理 FBO/输出纹理——提前初始化，
        # except 分支按已创建部分释放。
        output_tex = None
        fbo = None

        try:
            # ---- 12-bit test data ----
            # BT.709 limited range 12-bit code values:
            # Y black=256 (16<<4), Y white=3760 (235<<4), neutral C=2048 (128<<4)
            test_w, test_h = 4, 4
            test_points = [
                # (Y, U, V, label)
                (256,  2048, 2048, 'black'),
                (2048, 2048, 2048, 'gray'),
                (3760, 2048, 2048, 'white'),
                # Red-ish: BT.709 limited 12-bit
                (1280, 640, 3840, 'red'),
                # Blue-ish: BT.709 limited 12-bit
                (832, 3840, 960, 'blue'),
            ]

            y_plane = np.zeros((test_h, test_w), dtype=np.uint16)
            u_plane = np.zeros((test_h, test_w), dtype=np.uint16)
            v_plane = np.zeros((test_h, test_w), dtype=np.uint16)

            for i, (yv, uv, vv, _label) in enumerate(test_points[:test_w * test_h]):
                row = i // test_w
                col = i % test_w
                y_plane[row, col] = yv
                u_plane[row, col] = uv
                v_plane[row, col] = vv

            # ---- Upload via R16UI ----
            # Temporarily enable R16UI for self-test upload
            self._r16ui_self_test_passed = True

            y_tex, u_tex, v_tex = self.upload_yuv444_r16ui(
                y_plane, u_plane, v_plane,
                frame_index=-998, bit_depth=12
            )

            # ---- Render via R16UI shader ----
            output_tex = self._ctx.texture((test_w, test_h), components=4, dtype='f1')
            output_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
            output_tex.repeat_x = output_tex.repeat_y = False
            fbo = self._ctx.framebuffer(color_attachments=[output_tex])

            self.set_yuv_matrix(self.MATRIX_BT709)
            self.set_color_range(self.RANGE_LIMITED)

            fbo.use()
            self._ctx.viewport = (0, 0, test_w, test_h)
            self._ctx.clear(0.0, 0.0, 0.0, 0.0)

            self.render_yuv_r16ui_to_rgb(
                y_tex, u_tex, v_tex, fbo,
                chroma_uv_scale=(1.0, 1.0),
                chroma_uv_offset=(0.0, 0.0),
                bit_depth=12
            )

            # ---- Readback ----
            raw = fbo.read(viewport=(0, 0, test_w, test_h), components=4)
            pixels = np.frombuffer(raw, dtype=np.uint8).reshape(test_h, test_w, 4)
            # ---- Validate ----
            all_black = np.all(pixels[:, :, :3] == 0)
            if all_black:
                self._r16ui_self_test_passed = False
                self._r16ui_disabled_reason = "readback-all-black"
                logger.error("R16UI self-test FAILED: readback returned all-black pixels")
                _cleanup(fbo, output_tex)
                return False

            max_abs_error = 0.0
            for i, (yv, uv, vv, label) in enumerate(test_points[:test_w * test_h]):
                row = i // test_w
                col = i % test_w
                r_u8, g_u8, b_u8 = pixels[row, col, 0], pixels[row, col, 1], pixels[row, col, 2]

                # Compute expected RGB via CPU reference
                max_val = 4095.0
                y_f = yv / max_val
                u_f = uv / max_val
                v_f = vv / max_val

                # BT.709 limited range
                y_black = 256.0 / max_val
                y_range_inv = max_val / 3504.0  # 219 * 16
                c_neutral = 2048.0 / max_val
                c_range_inv = max_val / 3584.0  # 224 * 16

                y_norm = (y_f - y_black) * y_range_inv
                u_norm = (u_f - c_neutral) * c_range_inv
                v_norm = (v_f - c_neutral) * c_range_inv

                r_ref = max(0.0, min(1.0, 1.0 * y_norm + 0.0 * u_norm + 1.5748 * v_norm))
                g_ref = max(0.0, min(1.0, 1.0 * y_norm + (-0.1873) * u_norm + (-0.4681) * v_norm))
                b_ref = max(0.0, min(1.0, 1.0 * y_norm + 1.8556 * u_norm + 0.0 * v_norm))

                r_gpu = r_u8 / 255.0
                g_gpu = g_u8 / 255.0
                b_gpu = b_u8 / 255.0

                pixel_error = max(abs(r_gpu - r_ref), abs(g_gpu - g_ref), abs(b_gpu - b_ref))
                max_abs_error = max(max_abs_error, pixel_error)

                logger.debug(f"  R16UI self-test {label}: Y={yv} U={uv} V={vv} → "
                           f"GPU=({r_u8},{g_u8},{b_u8}) "
                           f"ref=({int(r_ref*255)},{int(g_ref*255)},{int(b_ref*255)}) "
                           f"err={pixel_error:.4f}")

            # Neutral gray check: channel delta <= 2/255
            gray_r, gray_g, gray_b = pixels[0, 1, 0], pixels[0, 1, 1], pixels[0, 1, 2]
            gray_channel_delta = max(abs(int(gray_r) - int(gray_g)), abs(int(gray_r) - int(gray_b)))

            # Cleanup
            _cleanup(fbo, output_tex)

            # Validation thresholds
            ERROR_THRESHOLD = 0.01  # max_abs_error <= 0.01
            GRAY_DELTA_THRESHOLD = 2  # channel_delta <= 2

            if max_abs_error > ERROR_THRESHOLD:
                self._r16ui_self_test_passed = False
                self._r16ui_disabled_reason = f"color_error_too_high: max={max_abs_error:.4f}"
                logger.error(f"R16UI self-test FAILED: max_abs_error={max_abs_error:.4f} > {ERROR_THRESHOLD}")
                return False

            if gray_channel_delta > GRAY_DELTA_THRESHOLD:
                self._r16ui_self_test_passed = False
                self._r16ui_disabled_reason = f"neutral_gray_channel_delta={gray_channel_delta}"
                logger.error(f"R16UI self-test FAILED: neutral gray channel delta={gray_channel_delta} > {GRAY_DELTA_THRESHOLD}")
                return False

            # All checks passed
            self._r16ui_self_test_passed = True
            logger.info(f"R16UI self-test PASSED: max_abs_error={max_abs_error:.4f}, "
                       f"gray_delta={gray_channel_delta}")
            return True

        except Exception as e:
            # AUD-39803: 异常路径清理已创建的 FBO/输出纹理（此前直接泄漏）。
            _cleanup(fbo, output_tex)
            self._r16ui_self_test_passed = False
            self._r16ui_disabled_reason = f"exception: {e}"
            logger.error(f"R16UI self-test exception: {e}", exc_info=True)
            return False
        finally:
            # AUD-39802: 无论通过/失败/异常，恢复自检前的共享渲染参数。
            self._yuv_matrix = saved_yuv_matrix
            self._color_range = saved_color_range

    def release(self) -> None:
        """释放所有资源"""
        self._pool.release_all()

        # 释放 R8 纹理池
        if self._r8_pool:
            self._r8_pool.release_all()
            self._r8_pool = None

        # 释放 R16UI 纹理池
        if self._r16ui_pool:
            self._r16ui_pool.release_all()

        if self._program:
            try:
                self._program.release()
            except Exception as e:
                logger.warning(f"Failed to release shader: {e}")
            self._program = None

        # 释放 R16UI shader
        if self._r16ui_program:
            try:
                self._r16ui_program.release()
            except Exception:
                pass
            self._r16ui_program = None

        if self._r16ui_quad_vao:
            try:
                self._r16ui_quad_vao.release()
            except Exception:
                pass
            self._r16ui_quad_vao = None

        # AUD-39810: 释放 _init_r16ui_quad 的 IBO（VertexArray.release()
        # 在 moderngl 5.12 不释放 index buffer）
        if self._r16ui_quad_ibo:
            try:
                self._r16ui_quad_ibo.release()
            except Exception:
                logger.debug("_r16ui_quad_ibo release failed (AUD-39810)", exc_info=True)
            self._r16ui_quad_ibo = None

        # 释放 NV12 R16UI shader
        if self._nv12_r16ui_program:
            try:
                self._nv12_r16ui_program.release()
            except Exception:
                pass
            self._nv12_r16ui_program = None

        if self._nv12_r16ui_vao:
            try:
                self._nv12_r16ui_vao.release()
            except Exception:
                pass
            self._nv12_r16ui_vao = None

        # AUD-39810: 释放 _create_nv12_r16ui_shader 的 IBO
        if self._nv12_r16ui_ibo:
            try:
                self._nv12_r16ui_ibo.release()
            except Exception:
                logger.debug("_nv12_r16ui_ibo release failed (AUD-39810)", exc_info=True)
            self._nv12_r16ui_ibo = None

        # G-P0-01: 释放缓存的 NV12 sampler2D shader
        if self._nv12_program:
            try:
                self._nv12_program.release()
            except Exception:
                pass
            self._nv12_program = None

        # AUD-39806: 释放 NV12 float 路径的缓存 VAO 与共享 IBO
        self._release_nv12_quad_resources()

        if self._quad_buffer:
            try:
                self._quad_buffer.release()
            except Exception:
                pass
            self._quad_buffer = None

        # AUD-39810: 释放 _init_quad_buffer 的 IBO
        if self._quad_ibo:
            try:
                self._quad_ibo.release()
            except Exception:
                logger.debug("_quad_ibo release failed (AUD-39810)", exc_info=True)
            self._quad_ibo = None

        if self._quad_vao:
            try:
                self._quad_vao.release()
            except Exception:
                pass
            self._quad_vao = None

        # Phase 2: 清理 staging buffers
        self._staging_buffers.clear()
        self._staging_rings.clear()
        self._staging_ring_idx.clear()

        # 释放 PBO 异步上传池
        if self._pbo_pool:
            self._pbo_pool.release()
            self._pbo_pool = None
        self._pbo_enabled = False
        self._pbo_slot_count = 0

    def __enter__(self) -> 'YUVGPUUploader':
        """进入上下文管理器"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """退出上下文管理器"""
        self.release()


# ================================================================
# 辅助函数
# ================================================================

def get_chroma_uv_params(subsampling: str) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """
    获取 Chroma UV 坐标参数。

    对于独立 Y/U/V 纹理，UV 纹理坐标已归一化到 [0, 1]，
    无需额外的 subsampling 缩放。此函数返回默认参数。

    Args:
        subsampling: '420', '422', '444'

    Returns:
        ((scale_x, scale_y), (offset_x, offset_y)) 元组
        默认 scale=(1.0, 1.0), offset=(0.0, 0.0)
    """
    # 独立纹理不需要额外的 scale/offset
    # UV 纹理坐标已归一化到 [0, 1]
    return ((1.0, 1.0), (0.0, 0.0))


# 保留旧函数名作为兼容别名（已废弃，仅用于向后兼容）
def get_chroma_scale(subsampling: str) -> Tuple[float, float]:
    """
    获取色度缩放因子（已废弃）。

    此函数已废弃。对于独立 Y/U/V 纹理，UV 纹理坐标已归一化，
    无需 subsampling 缩放。请使用 get_chroma_uv_params()。

    Args:
        subsampling: '420', '422', '444'

    Returns:
        (scale_x, scale_y) 元组（现在始终返回 (1.0, 1.0)）
    """
    # 独立纹理不需要额外的 scale
    return (1.0, 1.0)


def _cleanup(fbo, tex) -> None:
    """Clean up self-test resources."""
    try:
        if fbo is not None:
            fbo.release()
    except Exception:
        pass
    try:
        if tex is not None:
            tex.release()
    except Exception:
        pass


def create_test_yuv_frame(
    width: int,
    height: int,
    subsampling: str = '420',
    bit_depth: int = 8
) -> Tuple[np.ndarray, ...]:
    """
    创建测试 YUV 帧。

    Args:
        width: 宽度
        height: 高度
        subsampling: 子采样类型
        bit_depth: 位深度

    Returns:
        Y/U/V 平面数组
    """
    dtype = np.uint16 if bit_depth > 8 else np.uint8
    max_val = (1 << bit_depth) - 1

    y = np.linspace(0, max_val, width * height, dtype=dtype).reshape(height, width)

    if subsampling == '420':
        u = np.full((height // 2, width // 2), max_val // 2, dtype=dtype)
        v = np.full((height // 2, width // 2), max_val // 2, dtype=dtype)
    elif subsampling == '422':
        u = np.full((height, width // 2), max_val // 2, dtype=dtype)
        v = np.full((height, width // 2), max_val // 2, dtype=dtype)
    elif subsampling == '444':
        u = np.full((height, width), max_val // 2, dtype=dtype)
        v = np.full((height, width), max_val // 2, dtype=dtype)
    else:
        u = np.full((height, width), max_val // 2, dtype=dtype)
        v = np.full((height, width), max_val // 2, dtype=dtype)

    return (y, u, v)


def create_test_nv12_frame(
    width: int,
    height: int,
    bit_depth: int = 8
) -> Tuple[np.ndarray, np.ndarray]:
    """
    创建测试 NV12 帧。

    Args:
        width: 宽度
        height: 高度
        bit_depth: 位深度

    Returns:
        (y, uv_interleaved) 元组
    """
    dtype = np.uint16 if bit_depth > 8 else np.uint8
    max_val = (1 << bit_depth) - 1

    y = np.linspace(0, max_val, width * height, dtype=dtype).reshape(height, width)

    # NV12: UV interleaved (height/2, width) or (height/2, width/2, 2)
    uv_height = height // 2
    uv_width = width // 2

    # 使用 (height/2, width/2, 2) 格式
    uv = np.full((uv_height, uv_width, 2), max_val // 2, dtype=dtype)

    return (y, uv)
