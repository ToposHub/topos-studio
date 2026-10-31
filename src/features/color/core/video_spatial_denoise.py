"""视频空域降噪器（GPU，单帧）。

对应计划 M1：三个质量模式（fast / better / enhanced），
亮度/色度在 YCbCr 域独立处理，输出写入自有的 RGBA16F 纹理。

用法（仅渲染线程）：
    denoiser = VideoSpatialDenoiser(ctx, quad, surfaces)
    out_tex = denoiser.process(source_tex, spatial_params)
    # out_tex 由 surface cache 持有，下一帧复用；调用方不得释放

失败语义：任何 GL 异常都回退为返回 source_tex（原始帧），不阻塞播放。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

import moderngl

from src.shared.video.denoise.params import SpatialDenoiseParams

from .video_denoise_gl import QuadRenderer, RenderSurfaceCache
from . import video_denoise_shaders as vs

logger = logging.getLogger(__name__)


class VideoSpatialDenoiser:
    """视频空域降噪（fast/better/enhanced）。"""

    def __init__(self, ctx: moderngl.Context, quad: QuadRenderer,
                 surfaces: RenderSurfaceCache) -> None:
        self._ctx = ctx
        self._quad = quad
        self._surfaces = surfaces
        self._programs: Dict[str, moderngl.Program] = {}
        self.last_mode: Optional[str] = None
        self.last_pass_count = 0

    # ------------------------------------------------------------------

    def _prog(self, name: str, fragment: str) -> Optional[moderngl.Program]:
        prog = self._programs.get(name)
        if prog is not None:
            return prog
        try:
            prog = self._ctx.program(vs.QUAD_VERTEX_SHADER, fragment)
        except Exception:
            logger.exception("VideoSpatialDenoiser: compile %s failed", name)
            return None
        self._programs[name] = prog
        return prog

    def process(self, source_tex: moderngl.Texture,
                params: SpatialDenoiseParams) -> moderngl.Texture:
        """对 source_tex 执行空域降噪，返回输出纹理（可能就是 source_tex）。"""
        p = params.sanitized()
        if not p.is_active():
            return source_tex

        width, height = source_tex.size
        if width < 8 or height < 8:
            return source_tex

        try:
            if p.mode == "enhanced":
                return self._process_enhanced(source_tex, p, width, height)
            if p.mode == "better":
                result = self._process_separable(source_tex, p, width, height)
            else:  # fast
                result = self._process_fast(source_tex, p, width, height)
            if p.detail_recovery > 0.01:
                result = self._apply_detail_recovery(source_tex, result, p, width, height)
            return result
        except Exception:
            logger.exception("VideoSpatialDenoiser: process failed, passthrough")
            return source_tex

    # ------------------------------------------------------------------
    # Fast：单 pass 联合双边

    def _process_fast(self, source_tex: moderngl.Texture, p: SpatialDenoiseParams,
                      width: int, height: int) -> moderngl.Texture:
        prog = self._prog("spatial_fast", vs.SPATIAL_FAST_FRAGMENT)
        if prog is None:
            return source_tex
        # AUD-0907: 缓存键不带尺寸——RenderSurfaceCache.get 在尺寸不匹配时
        # 自动释放重建；键内嵌尺寸会让每种出现过的分辨率各占一份 GPU 显存
        # 且永不淘汰（多分辨率时间线无上界累积）。
        out = self._surfaces.get("spatial_out", width, height)
        if out is None:
            return source_tex
        self.last_mode = "fast"
        self.last_pass_count = 1

        out.use()
        self._ctx.viewport = (0, 0, width, height)
        source_tex.use(0)
        prog["u_texture"].value = 0
        prog["u_texel"].value = (1.0 / width, 1.0 / height)
        prog["u_luma_strength"].value = p.luma / 100.0
        prog["u_chroma_strength"].value = p.chroma / 100.0
        prog["u_edge_threshold"].value = p.threshold
        prog["u_radius"].value = int(p.radius)
        self._quad.draw(prog)
        return out.texture

    # ------------------------------------------------------------------
    # Better：可分离两 pass

    def _process_separable(self, source_tex: moderngl.Texture, p: SpatialDenoiseParams,
                           width: int, height: int) -> moderngl.Texture:
        prog = self._prog("spatial_sep", vs.SPATIAL_SEPARABLE_FRAGMENT)
        if prog is None:
            return source_tex
        tmp = self._surfaces.get("spatial_mid", width, height)
        out = self._surfaces.get("spatial_out", width, height)
        if tmp is None or out is None:
            return source_tex
        self.last_mode = "better"
        self.last_pass_count = 2

        common = dict(
            u_texel=(1.0 / width, 1.0 / height),
            u_luma_strength=p.luma / 100.0,
            u_chroma_strength=p.chroma / 100.0,
            u_edge_threshold=p.threshold,
            u_radius=int(min(p.radius, 6)),
        )
        for pass_index, direction, target, src in (
            (0, (1.0, 0.0), tmp, source_tex),
            (1, (0.0, 1.0), out, tmp.texture),
        ):
            target.use()
            self._ctx.viewport = (0, 0, width, height)
            src.use(0)
            prog["u_texture"].value = 0
            prog["u_texel"].value = common["u_texel"]
            prog["u_dir"].value = direction
            prog["u_luma_strength"].value = common["u_luma_strength"]
            prog["u_chroma_strength"].value = common["u_chroma_strength"]
            prog["u_edge_threshold"].value = common["u_edge_threshold"]
            prog["u_radius"].value = common["u_radius"]
            prog["u_pass_index"].value = pass_index
            self._quad.draw(prog)
        return out.texture

    # ------------------------------------------------------------------
    # Enhanced：多尺度（1/2 降采样 → 可分离双边 → 上采样 + 细节恢复）

    def _process_enhanced(self, source_tex: moderngl.Texture, p: SpatialDenoiseParams,
                          width: int, height: int) -> moderngl.Texture:
        down_prog = self._prog("spatial_down", vs.SPATIAL_DOWNSAMPLE_FRAGMENT)
        sep_prog = self._prog("spatial_sep", vs.SPATIAL_SEPARABLE_FRAGMENT)
        up_prog = self._prog(
            "spatial_up_detail", vs.SPATIAL_UPSAMPLE_DETAIL_FRAGMENT
        )
        if down_prog is None or sep_prog is None or up_prog is None:
            return self._process_separable(source_tex, p, width, height)

        half_w = max(1, width // 2)
        half_h = max(1, height // 2)
        low = self._surfaces.get("enh_low", half_w, half_h)
        low_mid = self._surfaces.get("enh_low_mid", half_w, half_h)
        low_out = self._surfaces.get("enh_low_out", half_w, half_h)
        out = self._surfaces.get("spatial_out", width, height)
        if any(s is None for s in (low, low_mid, low_out, out)):
            return self._process_separable(source_tex, p, width, height)
        self.last_mode = "enhanced"
        self.last_pass_count = 4

        # 1. 降采样
        low.use()
        self._ctx.viewport = (0, 0, half_w, half_h)
        source_tex.use(0)
        down_prog["u_texture"].value = 0
        down_prog["u_texel"].value = (1.0 / width, 1.0 / height)
        self._quad.draw(down_prog)

        # 2. 半分辨率可分离双边（保 low 的原始低频用于残差计算）
        luma_strength = p.luma / 100.0
        chroma_strength = p.chroma / 100.0
        for pass_index, direction, target, src in (
            (0, (1.0, 0.0), low_mid, low.texture),
            (1, (0.0, 1.0), low_out, low_mid.texture),
        ):
            target.use()
            self._ctx.viewport = (0, 0, half_w, half_h)
            src.use(0)
            sep_prog["u_texture"].value = 0
            sep_prog["u_texel"].value = (1.0 / half_w, 1.0 / half_h)
            sep_prog["u_dir"].value = direction
            sep_prog["u_luma_strength"].value = luma_strength
            sep_prog["u_chroma_strength"].value = chroma_strength
            sep_prog["u_edge_threshold"].value = p.threshold
            sep_prog["u_radius"].value = int(min(max(p.radius, 2), 6))
            sep_prog["u_pass_index"].value = pass_index
            self._quad.draw(sep_prog)

        # 3. 上采样 + 细节恢复（detail_recovery 由独立 pass 统一处理，此处透传
        # p.detail_recovery；Fast/Better 路径的独立细节恢复 pass 见下方
        # _apply_detail_recovery）
        out.use()
        self._ctx.viewport = (0, 0, width, height)
        source_tex.use(0)
        low_out.texture.use(1)
        low.texture.use(2)
        up_prog["u_original"].value = 0
        up_prog["u_denoised"].value = 1
        up_prog["u_lowfreq"].value = 2
        up_prog["u_detail_recovery"].value = p.detail_recovery
        self._quad.draw(up_prog)
        return out.texture

    # ------------------------------------------------------------------
    # 细节恢复（Fast/Better 复用）

    def _apply_detail_recovery(self, original: moderngl.Texture, denoised: moderngl.Texture,
                               p: SpatialDenoiseParams, width: int, height: int) -> moderngl.Texture:
        prog = self._prog("detail_recovery", vs.DETAIL_RECOVERY_FRAGMENT)
        if prog is None:
            return denoised
        out = self._surfaces.get("spatial_detail_out", width, height)
        if out is None:
            return denoised
        out.use()
        self._ctx.viewport = (0, 0, width, height)
        original.use(0)
        denoised.use(1)
        prog["u_original"].value = 0
        prog["u_denoised"].value = 1
        prog["u_detail_recovery"].value = p.detail_recovery
        self._quad.draw(prog)
        self.last_pass_count += 1
        return out.texture

    # ------------------------------------------------------------------

    def release(self) -> None:
        for prog in self._programs.values():
            try:
                prog.release()
            except Exception:
                pass
        self._programs.clear()
        # 表面由共享 RenderSurfaceCache 统一管理
