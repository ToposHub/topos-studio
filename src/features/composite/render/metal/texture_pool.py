"""Metal 合成链的纹理池（按字节计量，GPU 完成后才回收）。

阶段 5.3 的资源纪律：

- **按字节计量**：每张纹理按 像素数 × 每像素字节数 计入预算
  （RGBA16F = 8 B/px）。分配超预算时按 LRU 驱逐空闲纹理；驱逐不了
  （全部在途）则抛 ``MetalPoolExhausted``——绝不静默降质。
- **回收等待 GPU 完成**：渲染中的纹理经 :meth:`retire` 进入待回收
  队列，只有当拥有者在 ``MetalSubmissionScheduler.poll()`` 的释放回调
  里调用 :meth:`finalize_retire` 后才回到可复用列表。completion 回调
  由驱动线程只入队、资源线程执行（`submission_scheduler` 的线程契约）。
- 槽位按租约键确认：每张活跃纹理持有 ``(pool 代次, 槽号, 提交号)``
  身份（`slot_key`），发布到 ``CompositeGpuFrameHandle`` 的 release
  回调据此归还，重复 release 幂等。
"""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# RGBA16F 每像素字节数。
RGBA16F_BYTES_PER_PIXEL = 8


class MetalPoolExhausted(RuntimeError):
    """预算内无法分配且无可驱逐的空闲纹理。"""


@dataclass
class PooledTexture:
    """池中一张 MTLTexture 的记账对象。"""

    texture: object                 # MTLTexture（宿主注入/创建）
    width: int
    height: int
    bytes_size: int
    slot: int
    generation: int
    # 租约状态：leased（渲染/发布中）→ retiring（等待 GPU 完成）→ free。
    state: str = "leased"
    # 该纹理承载的数据打包区域（绝对画布坐标；由渲染器在使用处赋值）。
    pack: Tuple[int, int, int, int] = (0, 0, 0, 0)
    # 幂等归还标记。
    _returned: bool = False

    @property
    def slot_key(self) -> Tuple[int, int]:
        """租约键（池代次 + 槽号；提交号由渲染器追加）。"""
        return (self.generation, self.slot)


class MetalTexturePool:
    """字节预算内按尺寸复用 MTLTexture 的池（线程安全）。"""

    def __init__(
        self,
        context,
        budget_bytes: int = 512 * 1024 * 1024,
        pixel_format=None,
    ) -> None:
        """
        Args:
            context: ``MetalComputeContext``（或等价注入），提供
                ``create_texture_2d``。
            budget_bytes: 显存预算（字节）。
            pixel_format: 纹理像素格式（默认 RGBA16Float，由 context 的
                Metal 模块解析）。
        """
        self._context = context
        self._budget = int(budget_bytes)
        if pixel_format is None:
            import Metal  # pyobjc-framework-Metal
            pixel_format = Metal.MTLPixelFormatRGBA16Float
        self._pixel_format = pixel_format
        self._lock = threading.RLock()
        self._slot_counter = itertools.count(1)
        self._generation = 1
        self._free: List[PooledTexture] = []          # LRU：尾新头旧
        self._live: Dict[int, PooledTexture] = {}      # slot → 记账
        self._retiring: Dict[int, PooledTexture] = {}
        self._bytes_allocated = 0

    # ── 计量 ──

    @property
    def pixel_format(self) -> object:
        """池内纹理的像素格式（MTLPixelFormat）。"""
        return self._pixel_format

    @property
    def budget_bytes(self) -> int:
        return self._budget

    @property
    def bytes_allocated(self) -> int:
        with self._lock:
            return self._bytes_allocated

    @property
    def live_count(self) -> int:
        with self._lock:
            return len(self._live)

    @property
    def retiring_count(self) -> int:
        with self._lock:
            return len(self._retiring)

    def texture_bytes(self, width: int, height: int) -> int:
        return int(width) * int(height) * RGBA16F_BYTES_PER_PIXEL

    # ── 分配 / 归还 ──

    def acquire(self, width: int, height: int) -> PooledTexture:
        """取一张 ``width × height`` 的可写纹理（命中复用，未命中创建）。"""
        w, h = int(width), int(height)
        if w <= 0 or h <= 0:
            raise ValueError(f"非法纹理尺寸 {w}x{h}")
        need = self.texture_bytes(w, h)
        with self._lock:
            for i, entry in enumerate(self._free):
                if entry.width == w and entry.height == h:
                    self._free.pop(i)
                    entry.state = "leased"
                    entry._returned = False
                    return entry
            self._evict_locked(need)
            if self._bytes_allocated + need > self._budget:
                raise MetalPoolExhausted(
                    f"Metal 纹理池预算不足：需要 {need} B，"
                    f"已用 {self._bytes_allocated}/{self._budget} B，"
                    f"无可驱逐空闲纹理")
        texture = self._context.create_texture_2d(
            self._pixel_format, w, h, shader_write=True)
        entry = PooledTexture(
            texture=texture, width=w, height=h, bytes_size=need,
            slot=next(self._slot_counter), generation=self._generation)
        with self._lock:
            self._bytes_allocated += need
            self._live[entry.slot] = entry
        return entry

    def retire(self, entry: PooledTexture) -> bool:
        """把渲染完的纹理置为待回收（幂等；等待 GPU 完成后 finalize）。

        返回 True 表示本次调用生效。之后的
        :meth:`finalize_retire` 必须来自 GPU 完成回调（或确定性等待点）。
        """
        with self._lock:
            if entry._returned or entry.slot not in self._live:
                return False
            entry._returned = True
            entry.state = "retiring"
            self._retiring[entry.slot] = self._live.pop(entry.slot)
        return True

    def finalize_retire(self, entry: PooledTexture) -> bool:
        """GPU 完成后把待回收纹理放回复用列表（幂等）。"""
        with self._lock:
            if self._retiring.pop(entry.slot, None) is None:
                return False
            entry.state = "free"
            self._free.append(entry)
        return True

    def release_direct(self, entry: PooledTexture) -> bool:
        """无在途 GPU 工作时的立即归还（如同步等待后的输出纹理租约）。"""
        with self._lock:
            if entry._returned or entry.slot not in self._live:
                return False
            entry._returned = True
            entry.state = "free"
            self._live.pop(entry.slot, None)
            self._free.append(entry)
        return True

    def drop(self, entry: PooledTexture) -> None:
        """彻底丢弃一张纹理（字节记账随之减少；释放依赖引用计数）。"""
        with self._lock:
            self._live.pop(entry.slot, None)
            self._retiring.pop(entry.slot, None)
            try:
                self._free.remove(entry)
            except ValueError:
                pass
            self._bytes_allocated = max(0, self._bytes_allocated - entry.bytes_size)

    # ── 生命周期 ──

    def destroy(self, *, wait_idle=None) -> bool:
        """销毁池（关闭/切页）。返回 True 表示可安全释放。

        Args:
            wait_idle: 可选的 ``fn()``，语义为「等待在途 GPU 完成」；
                返回 False 时仍有在途纹理，调用方不得销毁底层设备资源。
        """
        with self._lock:
            if self._retiring and wait_idle is not None:
                idle = bool(wait_idle())
                if not idle:
                    logger.warning(
                        "MetalTexturePool.destroy: %d 张纹理仍在等待 GPU "
                        "完成，保留记账不销毁", len(self._retiring))
                    return False
                for entry in list(self._retiring.values()):
                    self.finalize_retire(entry)
            live = len(self._live)
            if live:
                logger.warning(
                    "MetalTexturePool.destroy: %d 张纹理仍在租约中", live)
            self._free.clear()
            self._live.clear()
            self._retiring.clear()
            self._bytes_allocated = 0
            self._generation += 1
        return True

    # ── 内部 ──

    def _evict_locked(self, need: int) -> None:
        while self._bytes_allocated + need > self._budget and self._free:
            victim = self._free.pop(0)
            self._bytes_allocated -= victim.bytes_size
            # 底层 MTLTexture 的释放由引用计数完成（记账对象出队即弃）。
