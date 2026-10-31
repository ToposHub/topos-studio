"""视频降噪 GL 基础设施：全屏 quad 渲染与渲染表面缓存。

约束（M6 性能原则）：
- 不在播放热路径每帧创建 FBO/纹理：表面按 (key, size) 缓存复用
- 所有方法只能在渲染线程调用（持有 ModernGL 上下文时）
"""

from __future__ import annotations

import logging
import struct
from typing import Dict, Hashable, Optional

import moderngl

logger = logging.getLogger(__name__)


def _pack_vertex(x: float, y: float) -> bytes:
    return struct.pack("<2f", x, y)


class QuadRenderer:
    """共享全屏 quad VBO + 每个 program 独立 VAO。"""

    def __init__(self, ctx: moderngl.Context) -> None:
        self._ctx = ctx
        # triangle strip: (-1,-1) (1,-1) (-1,1) (1,1)
        self._vbo = ctx.buffer(
            b"".join(
                [
                    _pack_vertex(-1.0, -1.0),
                    _pack_vertex(1.0, -1.0),
                    _pack_vertex(-1.0, 1.0),
                    _pack_vertex(1.0, 1.0),
                ]
            )
        )
        self._vaos: Dict[int, moderngl.VertexArray] = {}

    def draw(self, program: moderngl.Program) -> None:
        vao = self._vaos.get(program.glo)
        if vao is None:
            try:
                vao = self._ctx.vertex_array(program, [(self._vbo, "2f", "in_pos")])
            except Exception:
                logger.exception("QuadRenderer: failed to build VAO")
                return
            self._vaos[program.glo] = vao
        vao.render(moderngl.TRIANGLE_STRIP)

    def release(self) -> None:
        for vao in self._vaos.values():
            try:
                vao.release()
            except Exception:
                pass
        self._vaos.clear()
        try:
            self._vbo.release()
        except Exception:
            pass


class RenderSurface:
    """一张可重复用作渲染目标的纹理（自带 FBO）。"""

    __slots__ = ("texture", "fbo", "size", "components", "dtype")

    def __init__(self, ctx: moderngl.Context, width: int, height: int,
                 components: int = 4, dtype: str = "f2") -> None:
        self.texture = ctx.texture((width, height), components, dtype=dtype)
        self.texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.texture.repeat_x = self.texture.repeat_y = False
        self.fbo = ctx.framebuffer([self.texture])
        self.size = (width, height)
        # AUD-0901：记录创建参数，供缓存命中校验与字节数估算使用
        self.components = components
        self.dtype = dtype

    def use(self) -> None:
        self.fbo.use()

    def release(self) -> None:
        try:
            self.fbo.release()
        except Exception:
            pass
        try:
            self.texture.release()
        except Exception:
            pass


class RenderSurfaceCache:
    """按 key 缓存 RenderSurface，尺寸/通道数/dtype 不匹配时自动重建。

    热路径零分配：同一 key 同一参数连续帧直接复用。
    """

    def __init__(self, ctx: moderngl.Context) -> None:
        self._ctx = ctx
        self._surfaces: Dict[Hashable, RenderSurface] = {}

    def get(self, key: Hashable, width: int, height: int,
            components: int = 4, dtype: str = "f2") -> Optional[RenderSurface]:
        width = max(1, int(width))
        height = max(1, int(height))
        surface = self._surfaces.get(key)
        if surface is not None and surface.size == (width, height) \
                and surface.components == components and surface.dtype == dtype:
            return surface
        if surface is not None:
            surface.release()
            # AUD-0900：旧表面已 release，必须先移出缓存——若随后新表面
            # 创建失败（OOM/上下文丢失）直接 return None，缓存里残留的
            # 已释放表面会在以旧尺寸再次请求时命中 `size == (w, h)` 被
            # 复用，use() 触发 ModernGL 对已释放对象的异常，降噪持续
            # 失效并刷异常日志。
            self._surfaces.pop(key, None)
        try:
            surface = RenderSurface(self._ctx, width, height, components, dtype)
        except Exception:
            logger.exception("RenderSurfaceCache: failed to create %sx%s surface", width, height)
            return None
        self._surfaces[key] = surface
        return surface

    def drop(self, key: Hashable) -> None:
        surface = self._surfaces.pop(key, None)
        if surface is not None:
            surface.release()

    def peek(self, key: Hashable) -> Optional[RenderSurface]:
        """返回已缓存的表面（不创建）。"""
        return self._surfaces.get(key)

    def release_all(self) -> None:
        for surface in self._surfaces.values():
            surface.release()
        self._surfaces.clear()

    @property
    def surface_count(self) -> int:
        return len(self._surfaces)

    def total_bytes(self) -> int:
        total = 0
        for s in self._surfaces.values():
            w, h = s.size
            # AUD-0901：按表面实际创建参数估算，不再硬编码 comp=4/f2
            comp = s.components
            bytes_per_component = int(s.dtype[1:]) // 8
            total += w * h * comp * bytes_per_component
        return total
