"""
Video Texture Pool - 视频专用纹理池

设计目的：
- 避免每帧创建/释放纹理，减少 GPU 开销
- 复用 3-5 个固定纹理槽位，实现稳定的 GPU 内存占用
- 针对视频帧特性：不生成 mipmap，使用 LINEAR 过滤

线程安全：
- 所有 ModernGL 操作必须在渲染线程执行
- 通过 RenderCommand 机制管理，不直接暴露 GPU API

使用方式：
    # 在渲染线程初始化
    pool = VideoTexturePool(ctx, pool_size=5)
    
    # 获取纹理槽位
    texture = pool.acquire(width, height, frame_index)
    
    # 上传帧数据（不调用 build_mipmaps）
    texture.write(frame_data)
    
    # 释放所有纹理（视频播放结束）
    pool.release_all()
"""

import itertools
import logging
import weakref
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import moderngl

logger = logging.getLogger(__name__)


# 槽位 LRU 序号：必须严格递增，勿改回 time.time() —— Windows 上其精度约
# 15.6ms，时间戳平局会让「池满释放最旧」选中刚发出的纹理
_SLOT_ACQUIRE_SEQ = itertools.count(1)


@dataclass
class VideoTextureSlot:
    """视频纹理槽位"""
    texture: moderngl.Texture = None
    frame_index: int = -1           # 当前持有的帧号
    last_used_time: float = 0.0     # 最后使用时间
    width: int = 0
    height: int = 0
    components: int = 4             # RGBA=4, RGB=3
    dtype: str = 'f2'              # P1-8: track dtype to avoid f1/f2 mismatch

    def is_size_match(self, width: int, height: int, components: int = 4, dtype: str = 'f2') -> bool:
        """检查尺寸和数据类型是否匹配"""
        return (self.width == width and self.height == height
                and self.components == components and self.dtype == dtype)
    
    def is_available(self) -> bool:
        """检查槽位是否可用（未持有有效帧）"""
        return self.frame_index < 0 or self.texture is None


class VideoTexturePool:
    """
    视频纹理池，复用固定数量的纹理槽位。
    
    核心特性：
    - 固定池大小（默认 5），避免 GPU 内存增长
    - 优先复用匹配尺寸的空闲槽位
    - 必要时创建新槽位（不超过 max_size）
    - 不生成 mipmap（视频帧不需要）
    
    与图片纹理的区别：
    - 图片：使用 mipmap + LINEAR_MIPMAP_LINEAR，支持平滑缩放
    - 视频：使用 LINEAR 过滤，轻量快速上传
    
    性能收益：
    - 避免每帧 texture 创建/释放的开销
    - 减少 GPU 内存碎片
    - 稳定的内存占用（播放 1000 帧不会增长）
    """
    
    def __init__(self, ctx: moderngl.Context, pool_size: int = 5) -> None:
        """
        初始化纹理池。
        
        Args:
            ctx: ModernGL 上下文（必须在渲染线程）
            pool_size: 池大小（默认 5，足够覆盖前后帧缓冲）
        """
        self._ctx = ctx
        self._slots: List[VideoTextureSlot] = []
        self._pool_size = pool_size
        self._current_size = (0, 0)  # 当前帧尺寸
        
        # weakref.finalize 后备释放机制
        # GC 时确保纹理资源释放
        self._finalizer = weakref.finalize(
            self,
            self._cleanup_on_gc,
            self._slots  # 引用 slots，但 finalizer 不直接访问 self
        )
        
    def acquire(self, width: int, height: int, frame_index: int, 
                components: int = 4, dtype: str = 'f2') -> Optional[moderngl.Texture]:
        """
        获取纹理槽位。
        
        优先复用已有纹理，必要时创建新槽位。
        
        Args:
            width: 帧宽度
            height: 帧高度
            frame_index: 当前帧号（用于追踪）
            components: 通道数（默认 4 RGBA）
            dtype: 数据类型（默认 'f2' float16）
            
        Returns:
            ModernGL Texture 对象，或 None（池已满）
            
        注意：
            返回的纹理不包含数据，需要调用 texture.write() 上传帧数据
        """
        # 1. 查找匹配尺寸的空闲槽位
        for slot in self._slots:
            if slot.is_size_match(width, height, components, dtype) and slot.is_available():
                slot.frame_index = frame_index
                slot.last_used_time = next(_SLOT_ACQUIRE_SEQ)
                return slot.texture

        # 2. 查找匹配尺寸但非空闲的槽位（复用最旧的）
        #    视频播放通常只需要当前帧，旧帧可以复用
        oldest_slot = None
        oldest_time = float('inf')

        for slot in self._slots:
            if slot.is_size_match(width, height, components, dtype):
                if slot.last_used_time < oldest_time:
                    oldest_time = slot.last_used_time
                    oldest_slot = slot
        
        if oldest_slot:
            oldest_slot.frame_index = frame_index
            oldest_slot.last_used_time = next(_SLOT_ACQUIRE_SEQ)
            return oldest_slot.texture
        
        # 3. 没有匹配尺寸，检查是否可以创建新槽位
        if len(self._slots) < self._pool_size:
            # 创建新纹理槽位
            try:
                texture = self._ctx.texture(
                    size=(width, height),
                    components=components,
                    dtype=dtype
                )
                # 视频帧不使用 mipmap，仅 LINEAR 过滤
                texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
                texture.repeat_x = texture.repeat_y = False
                
                slot = VideoTextureSlot(
                    texture=texture,
                    frame_index=frame_index,
                    last_used_time=next(_SLOT_ACQUIRE_SEQ),
                    width=width,
                    height=height,
                    components=components,
                    dtype=dtype
                )
                self._slots.append(slot)

                logger.debug(f"VideoTexturePool: Created new slot #{len(self._slots)} "
                           f"({width}x{height}, components={components}, dtype={dtype})")
                
                return texture
                
            except Exception as e:
                logger.error(f"VideoTexturePool: Failed to create texture: {e}")
                return None
        
        # 4. 池已满，复用最旧的槽位（即使尺寸不匹配）
        #    需要释放旧纹理并创建新尺寸的纹理
        if self._slots:
            # 找最旧的槽位
            oldest_idx = 0
            oldest_time = self._slots[0].last_used_time
            for i, slot in enumerate(self._slots):
                if slot.last_used_time < oldest_time:
                    oldest_time = slot.last_used_time
                    oldest_idx = i

            slot = self._slots[oldest_idx]

            # AUD-39700: 先创建新纹理，成功后再释放旧纹理并更新槽位字段。
            # 旧实现先 release 再 create：create 失败（GPU 内存不足等）时
            # 槽位仍是「已释放纹理 + 旧元数据」的损坏状态，后续 acquire
            # 以旧尺寸命中该槽（第 2 步复用不检查可用性）会返回已释放
            # 纹理，调用方 write() 产生未定义行为，损坏一直存续到
            # release_all。重建失败直接返回 None，旧纹理保持有效。
            try:
                texture = self._ctx.texture(
                    size=(width, height),
                    components=components,
                    dtype=dtype
                )
            except moderngl.Error as e:
                logger.error(f"VideoTexturePool: Failed to recreate texture: {e}")
                return None

            texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
            texture.repeat_x = texture.repeat_y = False

            old_texture = slot.texture
            slot.texture = texture
            slot.frame_index = frame_index
            slot.last_used_time = next(_SLOT_ACQUIRE_SEQ)
            slot.width = width
            slot.height = height
            slot.components = components
            slot.dtype = dtype

            if old_texture is not None:
                try:
                    old_texture.release()
                except moderngl.Error:
                    logger.debug(
                        "VideoTexturePool: old texture release failed "
                        "during slot replacement", exc_info=True)

            logger.debug(f"VideoTexturePool: Replaced slot #{oldest_idx} "
                       f"with new size ({width}x{height}, dtype={dtype})")

            return texture

        return None
    
    def mark_unused(self, frame_index: int) -> None:
        """
        标记帧号为不再使用。
        
        Args:
            frame_index: 要标记的帧号
        """
        for slot in self._slots:
            if slot.frame_index == frame_index:
                slot.frame_index = -1  # 标记为可用
    
    def release_all(self) -> None:
        """
        释放所有纹理。
        
        用于视频播放结束或尺寸变化时清理。
        """
        for slot in self._slots:
            if slot.texture:
                try:
                    slot.texture.release()
                except Exception as e:
                    logger.debug(f"VideoTexturePool: Release failed: {e}")
        
        self._slots.clear()
        logger.debug("VideoTexturePool: Released all slots")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        返回池统计信息。
        
        Returns:
            包含以下键的字典：
            - 'pool_size': 当前槽位数量
            - 'max_size': 最大槽位数量
            - 'active_frames': 当前持有的帧号列表
            - 'sizes': 各槽位尺寸列表
        """
        return {
            'pool_size': len(self._slots),
            'max_size': self._pool_size,
            'active_frames': [s.frame_index for s in self._slots if s.frame_index >= 0],
            'sizes': [(s.width, s.height) for s in self._slots],
            'components': [s.components for s in self._slots],
            'dtypes': [s.dtype for s in self._slots]
        }
    
    def has_texture_for_frame(self, frame_index: int) -> bool:
        """
        检查指定帧号的纹理是否存在。
        
        Args:
            frame_index: 帧号
            
        Returns:
            是否存在
        """
        for slot in self._slots:
            if slot.frame_index == frame_index and slot.texture:
                return True
        return False
    
    def get_texture_for_frame(self, frame_index: int) -> Optional[moderngl.Texture]:
        """
        获取指定帧号的纹理。
        
        Args:
            frame_index: 帧号
            
        Returns:
            Texture 或 None
        """
        for slot in self._slots:
            if slot.frame_index == frame_index and slot.texture:
                return slot.texture
        return None
    
    def resize_pool(self, new_size: int) -> None:
        """
        调整池大小。
        
        Args:
            new_size: 新的最大槽位数量
        """
        self._pool_size = new_size
        
        # 如果当前槽位超过新大小，释放多余的
        while len(self._slots) > self._pool_size:
            slot = self._slots.pop()
            if slot.texture:
                try:
                    slot.texture.release()
                except Exception:
                    pass
    
    def __repr__(self) -> str:
        stats = self.get_stats()
        return (f"VideoTexturePool(slots={stats['pool_size']}/{stats['max_size']}, "
               f"frames={stats['active_frames']})")
    
    # ================================================================
    # 上下文管理器协议
    # ================================================================
    
    def __enter__(self) -> 'VideoTexturePool':
        """
        进入上下文管理器。
        
        Returns:
            self，支持 with 语句使用
        """
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        退出上下文管理器，确保纹理释放。
        
        Args:
            exc_type: 异常类型（如果有）
            exc_val: 异常值
            exc_tb: 异常 traceback
        
        注意：
            无论是否发生异常，都会调用 release_all() 清理所有纹理
        """
        self.release_all()
    
    # ================================================================
    # weakref.finalize GC 后备释放
    # ================================================================
    
    @staticmethod
    def _cleanup_on_gc(slots: List[VideoTextureSlot]) -> None:
        """GC 兜底：报告泄漏并解除引用，绝不在 GC 线程调用 GL release。

        AUD-39701: 旧实现直接调用 slot.texture.release()——CPython 的
        finalizer 在触发 GC 的任意线程（UI 线程/解释器关闭阶段）运行，
        那里没有 current 的 GL 上下文，glDeleteTextures 属未定义行为，
        且异常被静默吞掉，实际效果退化为「泄漏 + 潜在驱动层错误」，
        与「GL 仅渲染线程 / deferred-release」纪律冲突。

        现在的分工：
        - 常规释放由渲染线程负责（release_all / acquire 槽位替换 /
          renderer 退出路径，均在渲染线程调用）；
        - finalizer 只提供可观测性：记录泄漏警告并置空引用（纯 Python
          操作，任意线程安全），GPU 内存随所属 GL 上下文销毁统一回收。
        """
        leaked = [s for s in slots if s.texture is not None]
        if leaked:
            logger.warning(
                "VideoTexturePool: %d texture slot(s) outlived explicit "
                "release_all() on the render thread — dropping references "
                "without GL release (GC thread has no current context); "
                "GPU memory is reclaimed when the owning GL context is "
                "destroyed",
                len(leaked))
        for slot in slots:
            slot.texture = None
