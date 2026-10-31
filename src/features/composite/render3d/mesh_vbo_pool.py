"""
Mesh VBO 池（GPU 资源复用）—— 按 content_hash 共享相同几何的 VBO/IBO。

用途：导入模型里若有多个相同子部件（如复制的螺丝、对称部件），每个都建独立
VBO/IBO 是浪费。本池按 mesh 的 content_hash 共享底层的 VBO/IBO，只 VAO 按需建。

设计要点：
- 按 ModernGL context 隔离（render thread 独立，不同 ctx 的 GL 对象不可混用）。
- 引用计数：acquire 返回时 +1，release 时 -1；count=0 才真删，避免误删共享资源。
- VAO 不进池（VAO 绑定 program，不同 program 的 VAO 不能共享），只共享 VBO/IBO。
- 线程模型：必须在创建 ctx 的线程调用。

注：当前 Scene3D 的 procedural 几何只有一份（primitive 单 mesh），无重复；本池主要
价值在导入模型多 mesh 场景。阶段六先建池 + 接入 _ensure_flat_meshes_gl。
"""

import logging
import threading
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


class _VBOEntry:
    """单个共享 VBO/IBO 条目 + 引用计数。"""
    __slots__ = ("vbo", "ibo", "index_count", "is_skinned", "has_tangent", "refcount")

    def __init__(self, vbo, ibo, index_count: int, is_skinned: bool,
                 has_tangent: bool = False) -> None:
        self.vbo = vbo
        self.ibo = ibo
        self.index_count = index_count
        self.is_skinned = is_skinned
        self.has_tangent = has_tangent
        self.refcount = 0


class MeshVBOPool:
    """按 content_hash 共享 VBO/IBO 的池（per-ctx）。

    用法：
        pool = MeshVBOPool.for_context(ctx)
        vbo, ibo, index_count = pool.acquire(ctx, mesh)
        ... 用 vbo/ibo 建 VAO ...
        pool.release(mesh.content_hash())  # 不用时减引用
    """

    # 进程级：每个 ctx 一个池（按 ctx 对象 id 索引）
    _pools: Dict[int, "MeshVBOPool"] = {}
    _lock = threading.Lock()

    def __init__(self) -> None:
        # key = (content_hash, is_skinned, has_tangent) → _VBOEntry
        self._entries: Dict[Tuple[str, bool, bool], _VBOEntry] = {}
        # P8-0：记录最近一次 acquire 是否命中缓存（供 Scene3D 统计 vbo_cache_hit）。
        self.last_was_cached: bool = False

    @classmethod
    def for_context(cls, ctx) -> "MeshVBOPool":
        """获取/创建绑定到指定 ctx 的池（线程安全）。"""
        ctx_id = id(ctx)
        with cls._lock:
            pool = cls._pools.get(ctx_id)
            if pool is None:
                pool = cls()
                cls._pools[ctx_id] = pool
            return pool

    @classmethod
    def release_context(cls, ctx) -> None:
        """ctx 销毁前调用：释放该 ctx 下所有池条目（避免 dangling GL 对象）。"""
        ctx_id = id(ctx)
        with cls._lock:
            pool = cls._pools.pop(ctx_id, None)
        if pool is not None:
            pool.release_all()

    def acquire(self, ctx, mesh, has_tangent: bool = False):
        """获取 mesh 对应的共享 VBO/IBO。哈希命中复用，否则新建。

        返回 (vbo, ibo, index_count)。引用计数 +1。
        同一 mesh 多次 acquire 会复用同一组 GL 对象。

        Args:
            ctx: ModernGL context（必须）。
            mesh: MeshData（用 content_hash + is_skinned 做缓存 key）。
            has_tangent: F5：True 时用 interleaved_with_tangent() 数据（11 float），
                缓存 key 额外带 has_tangent 区分两种 layout。
        """
        key = (mesh.content_hash(), mesh.is_skinned, bool(has_tangent))
        entry = self._entries.get(key)
        if entry is None:
            # 新建
            is_skinned = mesh.is_skinned
            if is_skinned:
                data = mesh.interleaved_skinned().tobytes()
            elif has_tangent:
                data = mesh.interleaved_with_tangent().tobytes()
            else:
                data = mesh.interleaved().tobytes()
            vbo = ctx.buffer(data)
            try:
                ibo = ctx.buffer(mesh.indices.tobytes())
            except Exception:
                # AUD-8919：IBO 创建失败（显存不足/上下文丢失）时回滚已创建的
                # VBO——否则半成品 buffer 不在 _entries 里、无人 release，
                # 调用方的 except 只跳过该 mesh，GL buffer 永久泄漏。
                try:
                    vbo.release()
                except Exception:
                    logger.debug("MeshVBOPool: 回滚时 VBO release 失败",
                                 exc_info=True)
                raise
            entry = _VBOEntry(vbo, ibo, int(mesh.indices.shape[0]),
                              is_skinned, bool(has_tangent))
            self._entries[key] = entry
            self.last_was_cached = False
            logger.debug("MeshVBOPool: 新建共享 VBO hash=%s skinned=%s tangent=%s verts=%d",
                         key[0], is_skinned, has_tangent, mesh.vertex_count)
        else:
            self.last_was_cached = True
        entry.refcount += 1
        return entry.vbo, entry.ibo, entry.index_count

    def release(self, content_hash: str, is_skinned: bool,
                has_tangent: bool = False) -> None:
        """减少引用计数；count=0 时真删 VBO/IBO。重复 release 安全（不会负）。"""
        key = (content_hash, is_skinned, bool(has_tangent))
        entry = self._entries.get(key)
        if entry is None:
            return
        entry.refcount = max(0, entry.refcount - 1)
        if entry.refcount == 0:
            try: entry.vbo.release()
            except Exception: pass
            try: entry.ibo.release()
            except Exception: pass
            del self._entries[key]

    def release_all(self) -> None:
        """释放池内所有条目（ctx 销毁时调用）。"""
        for entry in self._entries.values():
            try: entry.vbo.release()
            except Exception: pass
            try: entry.ibo.release()
            except Exception: pass
        self._entries.clear()

    def stats(self) -> Tuple[int, int]:
        """返回 (共享条目数, 总引用计数) —— 诊断用。"""
        total_refs = sum(e.refcount for e in self._entries.values())
        return len(self._entries), total_refs
