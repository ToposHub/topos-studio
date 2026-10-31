"""
Material Texture Loader — render-thread material texture upload & cache.

负责把材质贴图（base color / normal / ORM / AO / emissive）从磁盘加载并上传为
ModernGL 纹理，按 ``path + mtime`` 缓存。颜色空间转换由 PBR shader 统一处理：
baseColor/emissive 在 shader 中从 sRGB 解码到线性，normal/ORM/AO 按数据贴图读取。

设计要点（对应 composite_3d_module_audit_optimization_plan_2026-06-29.md
"Implementation route #2: material texture loader/cache in render thread"）：

- 复用 ``image_loader.decode_image_file`` 做 CPU 解码（支持 jpg/png/exr/hdr 等），
  避免重复造轮子。
- color role（baseColor/emissive）：保持源贴图数值上传，避免在 CPU 和 shader
  两侧重复 sRGB→linear。
- data role（normal/ORM/AO）：保持线性，不做转换。
- 缓存 key = (absolute_path, mtime)；文件变了才重上传。
- GL 资源生命周期：随 Model3D 节点 cleanup（节点持有 loader 实例）。

线程安全：仅供 render thread 调用（与 Scene3D 渲染同线程），不加锁。
"""

import os
import logging
from typing import Optional

import numpy as np

from .scene_graph import TextureDescriptor

logger = logging.getLogger(__name__)


class MaterialTextureLoader:
    """材质纹理加载器 + GL 缓存（render thread 用）。

    缓存 {cache_key: (gl_texture, w, h)}。``get_or_upload`` 按 path+mtime 命中
    则复用，否则解码 + 上传。失败返回 None（调用方退化为标量材质）。

    GL 纹理绑定到 sampler 由 Scene3D 在渲染时调 ``tex.use(location=N)`` 完成；
    本类只负责"得到可用的 texture 对象"。
    """

    def __init__(self) -> None:
        # cache_key (abs_path, mtime) -> (moderngl.Texture, applied_filter_wrap)
        # P3-3：applied_filter_wrap 记录已设置的 (filter, wrap)，相同则跳过
        # apply_filter_wrap（避免每 mesh 重复 glTexParameteri）。
        self._cache: dict = {}
        # P0 诊断：上一次 get_or_upload 是否命中缓存（供 Scene3D perf stats 读）。
        self.was_last_cached: bool = False

    def get_or_upload(self, ctx, descriptor: TextureDescriptor,
                      model_dir: str = "") -> Optional[object]:
        """按 descriptor 解析路径并上传/复用纹理，返回 moderngl.Texture（失败 None）。

        Args:
            ctx: moderngl.Context（render thread）。
            descriptor: TextureDescriptor（含 path + role + tex_coord_set）。
            model_dir: 模型文件所在目录，用于解析材质贴图的相对路径。

        Returns:
            moderngl.Texture，或 None（路径空/文件不存在/解码失败）。
        """
        path = self._resolve_path(descriptor.path, model_dir)
        if not path:
            return None
        if not os.path.isfile(path):
            return None

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None

        cache_key = (path, mtime)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self.was_last_cached = True
            # P3-3：缓存条目是 (tex, applied_filter_wrap) 元组
            return cached[0] if isinstance(cached, tuple) else cached
        self.was_last_cached = False

        # AUD-8908：贴图热更新（mtime 变化）后，同 path 旧 mtime 条目的
        # GL 纹理不再被任何渲染引用，却仍被 _cache 强持有直到节点 cleanup
        # ——GPU 内存随修改次数线性累积。插入新条目前立即释放旧条目。
        # 本方法仅 render thread 单线程调用（模块 docstring 契约），无竞态。
        for stale_key in [k for k in self._cache if k[0] == path and k != cache_key]:
            stale_entry = self._cache.pop(stale_key, None)
            if stale_entry is None:
                continue
            stale_tex = (stale_entry[0] if isinstance(stale_entry, tuple)
                         else stale_entry)
            state = getattr(self, "_filter_wrap_state", None)
            if state is not None:
                try:
                    state.pop(int(stale_tex.glo), None)
                except Exception:
                    pass
            try:
                stale_tex.release()
            except Exception:
                pass

        # 解码（复用 image_loader 的 CPU 解码，支持多种格式）
        tex = self._upload(ctx, path, descriptor.role)
        if tex is not None:
            self._cache[cache_key] = (tex, None)  # filter/wrap 未应用
        return tex

    def is_filter_wrap_applied(self, tex, texture_filter: str,
                               texture_wrap: str) -> bool:
        """P3-3：检查该纹理是否已应用相同的 filter/wrap（避免重复设置）。

        按 tex.glo + filter + wrap 查 _filter_wrap_state。命中返回 True（可跳过）。
        """
        state = getattr(self, "_filter_wrap_state", None)
        if state is None:
            return False
        try:
            glo = int(tex.glo)
        except Exception:
            return False
        return state.get(glo) == (str(texture_filter), str(texture_wrap))

    def mark_filter_wrap_applied(self, tex, texture_filter: str,
                                 texture_wrap: str) -> None:
        """P3-3：记录该纹理已应用某 filter/wrap。"""
        state = getattr(self, "_filter_wrap_state", None)
        if state is None:
            self._filter_wrap_state = {}
            state = self._filter_wrap_state
        try:
            glo = int(tex.glo)
            state[glo] = (str(texture_filter), str(texture_wrap))
        except Exception:
            pass

    def release_all(self) -> None:
        """释放所有缓存的 GL 纹理（cleanup 调用）。"""
        for entry in self._cache.values():
            tex = entry[0] if isinstance(entry, tuple) else entry
            try:
                tex.release()
            except Exception:
                pass
        self._cache.clear()
        # P3-3：清 filter/wrap 状态（纹理已释放，glo 失效）
        if hasattr(self, "_filter_wrap_state"):
            self._filter_wrap_state.clear()

    # ── 内部 ──

    @staticmethod
    def _resolve_path(tex_path: str, model_dir: str) -> str:
        """解析材质贴图路径：相对路径优先相对模型目录，其次当前目录。"""
        if not tex_path:
            return ""
        if os.path.isabs(tex_path):
            return tex_path
        if model_dir:
            candidate = os.path.join(model_dir, tex_path)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return os.path.abspath(tex_path) if os.path.isfile(tex_path) else tex_path

    @staticmethod
    def _upload(ctx, path: str, role: str) -> Optional[object]:
        """解码图片并上传为 GL 纹理（按 role 决定 sRGB→linear）。

        Returns:
            moderngl.Texture，或 None（解码失败）。
        """
        from ..core.image_loader import decode_image_file

        decoded = decode_image_file(path)
        if decoded is None:
            logger.warning("材质贴图解码失败: %s", path)
            return None

        img = decoded.data  # (h, w, c) float32 RGB/RGBA
        h, w = decoded.height, decoded.width
        comps = decoded.components

        # 保持贴图源数值上传。baseColor/emissive 的 sRGB→linear 由 shader 做；
        # normal/ORM/AO 是数据贴图，也必须保持原始通道值。

        # ModernGL 纹理通道数：3→用 RGBA（GL 不支持 RGB12 纹理上传时的某些路径，
        # 统一补 alpha=1 更稳健）；1/4 原样。
        if comps == 3:
            alpha = np.ones((h, w, 1), dtype=np.float32)
            img = np.concatenate([img[:, :, :3], alpha], axis=2)
            comps = 4
        elif comps == 1:
            # 单通道（如灰度 AO）→ 扩展为 RGB（线性复制）+ alpha
            g = img[:, :, :1]
            img = np.concatenate([g, g, g, np.ones((h, w, 1), dtype=np.float32)], axis=2)
            comps = 4
        elif comps == 2:
            g = img[:, :, :1]
            img = np.concatenate([g, g, g, np.ones((h, w, 1), dtype=np.float32)], axis=2)
            comps = 4

        img = np.ascontiguousarray(img.astype(np.float32))
        try:
            tex = ctx.texture((w, h), comps, img.tobytes(), dtype='f4')
        except Exception as exc:
            logger.warning("材质贴图 GL 上传失败 %s: %s", path, exc)
            return None

        # 基础采样参数（mipmap 由调用方按 material_params.texture_filter 配置）
        try:
            tex.filter = (moderngl_filter_nearest(), moderngl_filter_nearest())
        except Exception:
            pass
        return tex


def moderngl_filter_nearest():
    """延迟导入 moderngl 常量，避免无 GL 环境下 import 失败。"""
    import moderngl
    return moderngl.NEAREST


def apply_filter_wrap(tex, texture_filter: str, texture_wrap: str) -> None:
    """按 material_params 配置纹理过滤/环绕模式。

    Args:
        tex: moderngl.Texture。
        texture_filter: "nearest" / "linear" / "mipmap"。
        texture_wrap: "repeat" / "clamp" / "mirror"。
    """
    import moderngl
    if texture_filter == "nearest":
        tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
    elif texture_filter == "linear":
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    else:  # mipmap（默认）
        tex.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
        try:
            tex.build_mipmaps()
        except Exception:
            pass

    _apply_texture_wrap(tex, texture_wrap)


def _apply_texture_wrap(tex, texture_wrap: str) -> None:
    """Apply texture wrap without relying on ModernGL wrap constants.

    ModernGL exposes filter constants, but version 5.x does not export
    REPEAT/CLAMP_TO_EDGE/MIRRORED_REPEAT. Use raw OpenGL when available so
    mirror/clamp work, with a ModernGL boolean repeat fallback.
    """
    wrap = str(texture_wrap or "repeat").lower()
    try:
        glo = int(getattr(tex, "glo", 0) or 0)
    except (TypeError, ValueError):
        glo = 0
    try:
        if glo > 0:
            from OpenGL import GL as gl
            wrap_map = {
                "repeat": gl.GL_REPEAT,
                "clamp": gl.GL_CLAMP_TO_EDGE,
                "mirror": gl.GL_MIRRORED_REPEAT,
            }
            # AUD-8907：旧代码裸 glBindTexture 绑到「当前激活纹理单元」。
            # 调用方（Scene3D 首次绑材质）的顺序是先 apply_filter_wrap
            # 再 glActiveTexture(loc)+Bind，处理 slot N+1 时激活单元还停在
            # slot N，会把 N+1 的纹理错绑到 N 的单元上覆盖其 sampler 绑定
            # （首帧材质错乱）。这里保存当前单元与其旧 2D 绑定，操作后
            # 完整恢复，不再扰动任何单元的绑定状态。
            prev_active = int(gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE))
            prev_binding = int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
            gl.glBindTexture(gl.GL_TEXTURE_2D, glo)
            value = int(wrap_map.get(wrap, gl.GL_REPEAT))
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, value)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, value)
            gl.glBindTexture(gl.GL_TEXTURE_2D, prev_binding)
            gl.glActiveTexture(prev_active)
            return
    except Exception:
        pass

    try:
        repeat = (wrap == "repeat")
        tex.repeat_x = repeat
        tex.repeat_y = repeat
    except Exception:
        pass


# 材质贴图 sampler 绑定 location（与 _MESH_PBR_FRAGMENT 的 sampler 声明一致，
# 不能与 IBL 的 location 0/1/2 冲突）。
# base_color=3, normal=4, orm=5, ao=6, emissive=7。
MATERIAL_SAMPLER_LOCATIONS = {
    "base_color": 3,
    "normal": 4,
    "orm": 5,
    "ao": 6,
    "emissive": 7,
}
