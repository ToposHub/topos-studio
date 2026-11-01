"""Shared native GPU scope pipeline engine.

Implements every scope pass (sample, waveform, vectorscope, parade, luma,
histogram, CIE chromaticity, false color) as ModernGL raster passes with
additive point-splat accumulation.  It never calls ``read()`` — all outputs
are RGBA8 textures whose ``glo`` ids are published through
``ScopeGpuFrame`` descriptors and blitted by ``GpuScopeWidget``.

Both render threads (Color ``AsyncRenderer`` and the Composite
``CompositeRenderer``) own one engine instance bound to their own context.
The class is deliberately free of Qt types: it only needs a live ModernGL
context and must be used from whatever thread currently owns that context
(both callers hold the global GL mutex around command handling).

Shader sources live in ``gpu_scope.glsl`` as ``#define NAME \"\"\"...\"\"\"``
constants; the shader file is self-contained (including the passthrough
vertex shader), so no feature-specific shader library is imported here.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, Optional, Tuple

import moderngl
import numpy as np

from src.features.color.core.scope_types import (
    DEFAULT_CIE_SIZE as CIE_SIZE,
    DEFAULT_FALSECOLOR_SIZE as FALSECOLOR_SIZE,
    DEFAULT_HISTOGRAM_DENSITY_SIZE as HISTOGRAM_DENSITY_SIZE,
    DEFAULT_HISTOGRAM_DISPLAY_SIZE as HISTOGRAM_DISPLAY_SIZE,
    DEFAULT_CURVES_HISTOGRAM_SIZE as CURVES_HISTOGRAM_SIZE,
)

logger = logging.getLogger(__name__)

# Default shader location (single source of truth shared by both pages).
DEFAULT_SHADER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "features", "color", "shaders",
    "gpu_scope.glsl",
)

# Scope value axis (HDR headroom) shared by waveform / parade / luma.
SCOPE_RANGE_MIN = -0.1
SCOPE_RANGE_MAX = 1.2

# NOTE: the histogram reduce/display shaders hard-code the same 512-wide /
# 256-row bins as DEFAULT_HISTOGRAM_DENSITY_SIZE — change them together.
# CIE plot window (matches CieChromaticityWidget X_RANGE/Y_RANGE and bins).
CIE_X_RANGE = (0.0, 0.8)
CIE_Y_RANGE = (0.0, 0.9)

# Transfer / primaries enum encodings shared with the GLSL uniforms.
_TRANSFER_CODES = {'srgb': 0, 'bt709': 1, 'pq': 2, 'hlg': 3, 'scene': 4}
_PRIMARY_CODES = {'bt709': 0, 'p3': 1, 'bt2020': 2}
_FC_MODE_CODES = {'sdr': 0, 'hdr': 1, 'zebra': 2}


class GpuScopeEngine:
    """GPU scope pass collection bound to one ModernGL context."""

    def __init__(self, ctx: moderngl.Context,
                 shader_path: Optional[str] = None) -> None:
        """绑定一个 ModernGL context 并初始化空缓存。

        Args:
            ctx: 调用方（渲染线程）持有的 ModernGL context；本类不创建
                也不释放 context 本身。
            shader_path: ``gpu_scope.glsl`` 路径；默认仓库内置位置，
                测试可注入自定义源。
        """
        self.ctx = ctx
        self._shader_path = shader_path or DEFAULT_SHADER_PATH
        self.programs: Dict[str, moderngl.Program] = {}
        self.shaders_loaded = False
        self._pool_cache: Dict[tuple, dict] = {}
        self._pool: Optional[dict] = None
        self._points_vao_cache: Dict[tuple, tuple] = {}
        self._quad_vbo: Optional[moderngl.Buffer] = None
        self._quad_vao_cache: Dict[int, moderngl.VertexArray] = {}

    # ------------------------------------------------------------------
    # Shader / resource lifecycle
    # ------------------------------------------------------------------
    def load_shaders(self) -> bool:
        """Compile every scope program. Returns the loaded flag."""
        try:
            with open(self._shader_path, 'r', encoding='utf-8') as f:
                raw = f.read()
        except Exception as exc:
            logger.error("Failed to read %s: %s", self._shader_path, exc)
            self.shaders_loaded = False
            return False

        defs: Dict[str, str] = {
            m.group(1): m.group(2).strip()
            for m in re.finditer(r'#define\s+(\w+)\s*"""(.*?)"""', raw,
                                 flags=re.DOTALL)
        }
        passthrough = defs.get("SCOPE_PASSTHROUGH_VERTEX", "")

        def build(name: str, vertex_src: str, frag_src: str) -> None:
            try:
                self.programs[name] = self.ctx.program(
                    vertex_shader=vertex_src, fragment_shader=frag_src
                )
            except Exception as exc:
                logger.error("Failed to compile scope shader %s: %s", name, exc)

        build("scope_sample", passthrough, defs.get("SCOPE_SAMPLE_FRAGMENT", ""))
        build("scope_waveform_accum",
              defs.get("SCOPE_WAVEFORM_ACCUM_VERTEX", ""),
              defs.get("SCOPE_WAVEFORM_ACCUM_FRAGMENT", ""))
        build("scope_waveform_display", passthrough,
              defs.get("SCOPE_WAVEFORM_DISPLAY_FRAGMENT", ""))
        build("scope_vectorscope_accum",
              defs.get("SCOPE_VECTORSCOPE_ACCUM_VERTEX", ""),
              defs.get("SCOPE_VECTORSCOPE_ACCUM_FRAGMENT", ""))
        build("scope_vectorscope_display", passthrough,
              defs.get("SCOPE_VECTORSCOPE_DISPLAY_FRAGMENT", ""))
        build("scope_histogram_accum",
              defs.get("SCOPE_HISTOGRAM_ACCUM_VERTEX", ""),
              defs.get("SCOPE_HISTOGRAM_ACCUM_FRAGMENT", ""))
        build("scope_histogram_reduce", passthrough,
              defs.get("SCOPE_HISTOGRAM_REDUCE_FRAGMENT", ""))
        build("scope_histogram_display", passthrough,
              defs.get("SCOPE_HISTOGRAM_DISPLAY_FRAGMENT", ""))
        build("scope_curves_histogram_peak", passthrough,
              defs.get("SCOPE_CURVES_HISTOGRAM_PEAK_FRAGMENT", ""))
        build("scope_curves_histogram_display", passthrough,
              defs.get("SCOPE_CURVES_HISTOGRAM_DISPLAY_FRAGMENT", ""))
        build("scope_cie_accum",
              defs.get("SCOPE_CIE_ACCUM_VERTEX", ""),
              defs.get("SCOPE_VECTORSCOPE_ACCUM_FRAGMENT", ""))
        build("scope_cie_display", passthrough,
              defs.get("SCOPE_CIE_DISPLAY_FRAGMENT", ""))
        build("scope_falsecolor", passthrough,
              defs.get("SCOPE_FALSECOLOR_FRAGMENT", ""))

        required = (
            "scope_sample", "scope_waveform_accum", "scope_waveform_display",
            "scope_vectorscope_accum", "scope_vectorscope_display",
            "scope_histogram_accum", "scope_histogram_reduce",
            "scope_histogram_display", "scope_cie_accum", "scope_cie_display",
            "scope_falsecolor",
            "scope_curves_histogram_peak", "scope_curves_histogram_display",
        )
        self.shaders_loaded = all(k in self.programs for k in required)
        return self.shaders_loaded

    def ensure_pool(
        self,
        analysis_size: Tuple[int, int],
        waveform_size: Tuple[int, int],
        vectorscope_size: Tuple[int, int],
    ) -> dict:
        """Allocate/reuse the scope resource pool (cached by size key)."""
        pool_key = (
            tuple(int(v) for v in analysis_size),
            tuple(int(v) for v in waveform_size),
            tuple(int(v) for v in vectorscope_size),
        )
        pool = self._pool_cache.get(pool_key)
        if pool is None:
            pool = {}
            self._pool_cache[pool_key] = pool
        self._pool = pool

        def ensure_tex(key, size, components, dtype):
            cur = pool.get(key)
            if cur is not None and pool.get(key + '_size') == tuple(size):
                return cur
            if cur is not None:
                try:
                    cur.release()
                except Exception:
                    pass
            tex = self.ctx.texture(tuple(size), components, dtype=dtype)
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = False
            tex.repeat_y = False
            pool[key] = tex
            pool[key + '_size'] = tuple(size)
            return tex

        def ensure_fbo(key, color_tex):
            cur = pool.get(key)
            if cur is not None and getattr(cur, '_scope_tex', None) is color_tex:
                return cur
            if cur is not None:
                try:
                    cur.release()
                except Exception:
                    pass
            fbo = self.ctx.framebuffer(color_attachments=[color_tex])
            fbo._scope_tex = color_tex  # type: ignore[attr-defined]
            pool[key] = fbo
            return fbo

        sample_tex = ensure_tex('sample_tex', analysis_size, 4, 'f2')
        ensure_fbo('sample_fbo', sample_tex)

        wf_density = ensure_tex('wf_density', waveform_size, 4, 'f2')
        ensure_fbo('wf_density_fbo', wf_density)
        wf_display = ensure_tex('wf_display', waveform_size, 4, 'f1')
        ensure_fbo('wf_display_fbo', wf_display)

        vs_density = ensure_tex('vs_density', vectorscope_size, 1, 'f2')
        ensure_fbo('vs_density_fbo', vs_density)
        vs_display = ensure_tex('vs_display', vectorscope_size, 4, 'f1')
        ensure_fbo('vs_display_fbo', vs_display)

        hist_density = ensure_tex(
            'hist_density', HISTOGRAM_DENSITY_SIZE, 4, 'f2')
        ensure_fbo('hist_density_fbo', hist_density)
        hist_sum = ensure_tex('hist_sum', (HISTOGRAM_DENSITY_SIZE[0], 1), 4, 'f2')
        ensure_fbo('hist_sum_fbo', hist_sum)
        hist_display = ensure_tex(
            'hist_display', HISTOGRAM_DISPLAY_SIZE, 4, 'f1')
        ensure_fbo('hist_display_fbo', hist_display)
        hist_peak = ensure_tex('hist_peak', (1, 1), 4, 'f4')
        ensure_fbo('hist_peak_fbo', hist_peak)
        curves_hist = ensure_tex('curves_hist_display',
                                 pool.get('curves_hist_display_size', CURVES_HISTOGRAM_SIZE), 4, 'f1')
        ensure_fbo('curves_hist_display_fbo', curves_hist)

        cie_density = ensure_tex('cie_density', CIE_SIZE, 1, 'f2')
        ensure_fbo('cie_density_fbo', cie_density)
        cie_display = ensure_tex('cie_display', CIE_SIZE, 4, 'f1')
        ensure_fbo('cie_display_fbo', cie_display)

        fc_display = ensure_tex('fc_display', FALSECOLOR_SIZE, 4, 'f1')
        ensure_fbo('fc_display_fbo', fc_display)

        pool.update({
            'analysis_size': tuple(analysis_size),
            'waveform_size': tuple(waveform_size),
            'vectorscope_size': tuple(vectorscope_size),
        })
        return pool

    def release(self) -> None:
        """Release every allocated scope resource (idempotent)."""
        for pool in list(self._pool_cache.values()):
            for key, obj in list(pool.items()):
                if key.endswith('_size') or not hasattr(obj, 'release'):
                    continue
                try:
                    obj.release()
                except Exception:
                    pass
            pool.clear()
        self._pool_cache.clear()
        self._pool = None
        for cached in self._points_vao_cache.values():
            for resource in cached:
                try:
                    resource.release()
                except Exception:
                    pass
        self._points_vao_cache.clear()
        if self._quad_vbo is not None:
            try:
                self._quad_vbo.release()
            except Exception:
                pass
            self._quad_vbo = None
        for vao in self._quad_vao_cache.values():
            try:
                vao.release()
            except Exception:
                pass
        self._quad_vao_cache.clear()
        for prog in self.programs.values():
            try:
                prog.release()
            except Exception:
                pass
        self.programs.clear()
        self.shaders_loaded = False

    # ------------------------------------------------------------------
    # GL helpers
    # ------------------------------------------------------------------
    def _points_vao(self, prog: moderngl.Program,
                    vertex_count: int) -> moderngl.VertexArray:
        """Cached VAO for POINTS draws driven by gl_VertexID.

        Some drivers (Apple macOS) reject attribute-less VAOs at draw time,
        so a sequential index buffer is bound (same math, valid VAO).
        """
        key = (prog.glo, int(vertex_count))
        cached = self._points_vao_cache.get(key)
        if cached is not None:
            return cached[0]
        count = max(1, int(vertex_count))
        ibo = self.ctx.buffer(np.arange(count, dtype='u4').tobytes())
        vao = self.ctx.vertex_array(prog, [], index_buffer=ibo)
        self._points_vao_cache[key] = (vao, ibo)
        return vao

    def _render_quad(self, prog: moderngl.Program) -> None:
        """Draw a fullscreen triangle-strip quad for ``prog``."""
        if self._quad_vbo is None:
            self._quad_vbo = self.ctx.buffer(np.array([
                -1.0, -1.0, 0.0, 0.0,
                 1.0, -1.0, 1.0, 0.0,
                -1.0,  1.0, 0.0, 1.0,
                 1.0,  1.0, 1.0, 1.0,
            ], dtype='f4').tobytes())
        key = prog.glo
        vao = self._quad_vao_cache.get(key)
        if vao is None:
            vao = self.ctx.vertex_array(
                prog, [(self._quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')]
            )
            self._quad_vao_cache[key] = vao
        vao.render(moderngl.TRIANGLE_STRIP)

    def _enable_additive(self) -> None:
        """Additive point-splat blend state (self-contained; macOS-safe)."""
        self.ctx.enable(moderngl.BLEND)
        try:
            self.ctx.enable(moderngl.PROGRAM_POINT_SIZE)
        except Exception:
            pass
        self.ctx.blend_func = (moderngl.ONE, moderngl.ONE)

    def _restore_blend(self) -> None:
        """Return to the renderer's normal post-pass blend invariant."""
        self.ctx.blend_func = (
            moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA,
        )
        try:
            self.ctx.disable(moderngl.BLEND)
        except Exception:
            pass
        try:
            self.ctx.disable(moderngl.PROGRAM_POINT_SIZE)
        except Exception:
            pass

    def _use_target(self, fbo: moderngl.Framebuffer, size: Tuple[int, int],
                    clear: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)) -> None:
        """绑定渲染目标并清屏（viewport 对齐目标、撤销 scissor）。"""
        fbo.use()
        self.ctx.viewport = (0, 0, int(size[0]), int(size[1]))
        self.ctx.scissor = None
        fbo.clear(*clear)

    # ------------------------------------------------------------------
    # Passes
    # ------------------------------------------------------------------
    def sample(
        self,
        source_texture: moderngl.Texture,
        pool: dict,
        mask_texture: Optional[moderngl.Texture] = None,
        mask_uv_rect: tuple = (0.0, 0.0, 1.0, 1.0),
    ) -> None:
        """Downscale the source into the analysis texture (+ optional mask).

        The analysis texture is the single input every point-splat pass
        reads; sampling cost is paid once here instead of per scope.
        """
        prog = self.programs.get('scope_sample')
        if prog is None:
            return
        aw, ah = pool['analysis_size']
        self._use_target(pool['sample_fbo'], (aw, ah))
        source_texture.use(0)
        prog['u_texture'].value = 0
        prog['u_use_mask'].value = mask_texture is not None
        prog['u_mask_uv_rect'].value = tuple(float(v) for v in mask_uv_rect)
        if mask_texture is not None:
            mask_texture.use(1)
            prog['u_mask_texture'].value = 1
        self._render_quad(prog)

    def accumulate_waveform(self, pool: dict, channel: int = -1) -> None:
        """RGB (channels 0/1/2) or luma (channel 3) point-splat waveform."""
        prog = self.programs.get('scope_waveform_accum')
        if prog is None:
            return
        aw, ah = pool['analysis_size']
        ww, wh = pool['waveform_size']
        self._use_target(pool['wf_density_fbo'], (ww, wh), clear=(0.0,) * 4)
        pool['sample_tex'].use(0)
        prog['u_sample_tex'].value = 0
        prog['u_sample_size'].value = (float(aw), float(ah))
        prog['u_density_size'].value = (float(ww), float(wh))
        prog['u_range_min'].value = SCOPE_RANGE_MIN
        prog['u_range_max'].value = SCOPE_RANGE_MAX
        self._enable_additive()
        try:
            vao = self._points_vao(prog, aw * ah)
            channels = (channel,) if channel >= 0 else (0, 1, 2)
            for ch in channels:
                prog['u_channel'].value = ch
                vao.render(mode=moderngl.POINTS, vertices=aw * ah)
        finally:
            self._restore_blend()

    def display_waveform(self, pool: dict, single_channel: bool = False) -> None:
        """Tone-map waveform/luma density -> RGBA8 with graticule."""
        prog = self.programs.get('scope_waveform_display')
        if prog is None:
            return
        ww, wh = pool['waveform_size']
        self._use_target(pool['wf_display_fbo'], (ww, wh))
        pool['wf_density'].use(0)
        prog['u_density'].value = 0
        prog['u_density_size'].value = (float(ww), float(wh))
        prog['u_range_min'].value = SCOPE_RANGE_MIN
        prog['u_range_max'].value = SCOPE_RANGE_MAX
        _aw, ah = pool['analysis_size']
        # Fixed scale derived from the analysis row count. The luma trace
        # accumulates once (single channel) while RGB accumulates three
        # passes, so its expected per-texel peak is three times lower.
        divisor = 3.0 if single_channel else 1.0
        expected_peak = max(8.0, float(ah) * 0.10 / divisor)
        prog['u_max_inv'].value = 1.0 / expected_peak
        self._render_quad(prog)

    def accumulate_vectorscope(self, pool: dict, zoom: float = 1.0) -> None:
        """Additive point-splat of Cb/Cr into the vectorscope density map."""
        prog = self.programs.get('scope_vectorscope_accum')
        if prog is None:
            return
        aw, ah = pool['analysis_size']
        vw, vh = pool['vectorscope_size']
        self._use_target(pool['vs_density_fbo'], (vw, vh))
        pool['sample_tex'].use(0)
        prog['u_sample_tex'].value = 0
        prog['u_sample_size'].value = (float(aw), float(ah))
        prog['u_density_size'].value = (float(vw), float(vh))
        prog['u_zoom'].value = float(zoom)
        self._enable_additive()
        try:
            vao = self._points_vao(prog, aw * ah)
            vao.render(mode=moderngl.POINTS, vertices=aw * ah)
        finally:
            self._restore_blend()

    def display_vectorscope(self, pool: dict, zoom: float = 1.0) -> None:
        """Tone-map vectorscope density -> RGBA8 (fixed normalization)."""
        prog = self.programs.get('scope_vectorscope_display')
        if prog is None:
            return
        vw, vh = pool['vectorscope_size']
        self._use_target(pool['vs_display_fbo'], (vw, vh))
        pool['vs_density'].use(0)
        prog['u_density'].value = 0
        prog['u_density_size'].value = (float(vw), float(vh))
        prog['u_max_inv'].value = 1.0  # fixed normalization
        prog['u_zoom'].value = float(zoom)
        self._render_quad(prog)

    def accumulate_parade(self, pool: dict) -> None:
        """RGB parade: waveform accumulation viewport-partitioned in thirds.

        The waveform accum shader maps the source row to NDC [-1, 1]; three
        passes with the viewport narrowed to each density third place the R,
        G, B traces side by side while additive blending keeps per-channel
        separation (R density in .r of the left third, and so on).
        """
        prog = self.programs.get('scope_waveform_accum')
        if prog is None:
            return
        aw, ah = pool['analysis_size']
        ww, wh = pool['waveform_size']
        third = max(1, ww // 3)
        fbo = pool['wf_density_fbo']
        fbo.use()
        self.ctx.viewport = (0, 0, ww, wh)
        self.ctx.scissor = None
        fbo.clear(0.0, 0.0, 0.0, 0.0)
        pool['sample_tex'].use(0)
        prog['u_sample_tex'].value = 0
        prog['u_sample_size'].value = (float(aw), float(ah))
        prog['u_density_size'].value = (float(third), float(wh))
        prog['u_range_min'].value = SCOPE_RANGE_MIN
        prog['u_range_max'].value = SCOPE_RANGE_MAX
        self._enable_additive()
        try:
            vao = self._points_vao(prog, aw * ah)
            for ch in (0, 1, 2):
                self.ctx.viewport = (ch * third, 0, third, wh)
                prog['u_channel'].value = ch
                vao.render(mode=moderngl.POINTS, vertices=aw * ah)
        finally:
            self._restore_blend()
            self.ctx.viewport = (0, 0, ww, wh)

    def display_parade(self, pool: dict) -> None:
        """Tone-map parade density (reuses the waveform display shader)."""
        # Each third carries exactly one channel, so the RGB tone-map renders
        # monochrome red/green/blue panels with the shared value graticule.
        self.display_waveform(pool)

    def accumulate_histogram(self, pool: dict, *, include_luma: bool = False,
                             curve_mode: str = 'Custom') -> None:
        """Per-channel code-value point splat (log scale in reduce pass)."""
        prog = self.programs.get('scope_histogram_accum')
        if prog is None:
            return
        aw, ah = pool['analysis_size']
        hw, hh = pool['hist_density_size']
        self._use_target(pool['hist_density_fbo'], (hw, hh), clear=(0.0,) * 4)
        pool['sample_tex'].use(0)
        prog['u_sample_tex'].value = 0
        prog['u_sample_size'].value = (float(aw), float(ah))
        prog['u_density_size'].value = (float(hw), float(hh))
        prog['u_hist_axis'].value = self._curve_axis(curve_mode)
        self._enable_additive()
        try:
            vao = self._points_vao(prog, aw * ah)
            for ch in ((0, 1, 2, 3) if include_luma else (0, 1, 2)):
                prog['u_channel'].value = ch
                vao.render(mode=moderngl.POINTS, vertices=aw * ah)
        finally:
            self._restore_blend()

    def reduce_histogram(self, pool: dict) -> None:
        """Column-sum the scattered histogram density into log1p totals."""
        prog = self.programs.get('scope_histogram_reduce')
        if prog is None:
            return
        hw = HISTOGRAM_DENSITY_SIZE[0]
        self._use_target(pool['hist_sum_fbo'], (hw, 1))
        pool['hist_density'].use(0)
        prog['u_density'].value = 0
        self._render_quad(prog)

    def display_histogram(self, pool: dict) -> None:
        """Rasterize the log-domain column sums into the RGBA8 display."""
        prog = self.programs.get('scope_histogram_display')
        if prog is None:
            return
        dw, dh = HISTOGRAM_DISPLAY_SIZE
        self._use_target(pool['hist_display_fbo'], (dw, dh))
        pool['hist_sum'].use(0)
        prog['u_hist_sum'].value = 0
        prog['u_display_size'].value = (float(dw), float(dh))
        _aw, ah = pool['analysis_size']
        samples = float(_aw * ah)
        # A column holding ~1/48 of all samples reaches full scale; the log
        # mapping keeps low-count tails visible (fixed gain, no readback).
        prog['u_max_inv'].value = 1.0 / max(1.0, np.log1p(samples / 48.0))
        self._render_quad(prog)

    def display_curves_histogram(
        self, pool: dict, display_size: Tuple[int, int] = CURVES_HISTOGRAM_SIZE,
        line_width: float = 1.0, curve_mode: str = 'Custom',
    ) -> None:
        """Draw a linear, peak-normalized YRGB underlay without CPU readback."""
        display_size = tuple(max(1, int(v)) for v in display_size)
        if pool['curves_hist_display_size'] != display_size:
            tex = self.ctx.texture(display_size, 4, dtype='f1')
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = False
            tex.repeat_y = False
            try:
                fbo = self.ctx.framebuffer(color_attachments=[tex])
            except moderngl.Error:
                tex.release()
                raise
            fbo._scope_tex = tex
            pool['curves_hist_display_fbo'].release()
            pool['curves_hist_display'].release()
            pool['curves_hist_display'] = tex
            pool['curves_hist_display_size'] = display_size
            pool['curves_hist_display_fbo'] = fbo
        peak = self.programs['scope_curves_histogram_peak']
        self._use_target(pool['hist_peak_fbo'], (1, 1))
        pool['hist_sum'].use(0)
        peak['u_hist_sum'].value = 0
        self._render_quad(peak)

        prog = self.programs['scope_curves_histogram_display']
        self._use_target(pool['curves_hist_display_fbo'], display_size)
        pool['hist_sum'].use(0)
        pool['hist_peak'].use(1)
        prog['u_hist_sum'].value = 0
        prog['u_hist_peak'].value = 1
        prog['u_display_size'].value = tuple(float(v) for v in display_size)
        prog['u_line_width'].value = max(0.5, float(line_width))
        prog['u_curve_axis'].value = self._curve_axis(curve_mode)
        self._render_quad(prog)

    @staticmethod
    def _curve_axis(mode: str) -> int:
        if mode.startswith('Hue'):
            return 1
        if mode.startswith('Sat'):
            return 2
        return 3 if mode == 'LumSat' else 0

    def accumulate_cie(self, pool: dict, transfer: str = 'srgb',
                       primaries: str = 'bt709') -> None:
        """Point-splat xy chromaticity density after decode to linear.

        ``transfer`` / ``primaries`` select the shader-side decode matrices
        (see ``_TRANSFER_CODES`` / ``_PRIMARY_CODES``); unknown names fall
        back to sRGB / bt709.
        """
        prog = self.programs.get('scope_cie_accum')
        if prog is None:
            return
        aw, ah = pool['analysis_size']
        cw, ch = pool['cie_density_size']
        self._use_target(pool['cie_density_fbo'], (cw, ch))
        pool['sample_tex'].use(0)
        prog['u_sample_tex'].value = 0
        prog['u_sample_size'].value = (float(aw), float(ah))
        prog['u_density_size'].value = (float(cw), float(ch))
        prog['u_transfer'].value = _TRANSFER_CODES.get(transfer, 0)
        prog['u_primaries'].value = _PRIMARY_CODES.get(primaries, 0)
        self._enable_additive()
        try:
            vao = self._points_vao(prog, aw * ah)
            vao.render(mode=moderngl.POINTS, vertices=aw * ah)
        finally:
            self._restore_blend()

    def display_cie(self, pool: dict) -> None:
        """Heat-map display pass; all plot chrome is presenter-side."""
        prog = self.programs.get('scope_cie_display')
        if prog is None:
            return
        cw, ch = pool['cie_density_size']
        self._use_target(pool['cie_display_fbo'], (cw, ch))
        pool['cie_density'].use(0)
        prog['u_density'].value = 0
        _aw, ah = pool['analysis_size']
        samples = float(_aw * ah)
        # Compensates the 1/64 accum prescale; a ~2% concentration peak maps
        # to full scale (fixed gain, no readback).
        prog['u_max_inv'].value = 64.0 / max(64.0, samples * 0.02)
        self._render_quad(prog)

    def display_falsecolor(
        self,
        source_texture: moderngl.Texture,
        pool: dict,
        mode: str = 'sdr',
        transfer: str = 'srgb',
        zebra_threshold: float = 1.0,
    ) -> None:
        """Single-pass false color / zebra over the untouched source texture.

        The source is aspect-fit into the fixed output canvas; the fit rect
        is computed here and passed as uv scale/offset (GL bottom-up UVs).
        """
        prog = self.programs.get('scope_falsecolor')
        if prog is None:
            return
        fw, fh = FALSECOLOR_SIZE
        sw, sh = source_texture.size
        source_aspect = float(sw) / max(1.0, float(sh))
        out_aspect = float(fw) / float(fh)
        if source_aspect >= out_aspect:
            scale_y = out_aspect / source_aspect
            uv_scale = (1.0, scale_y)
            uv_offset = (0.0, (1.0 - scale_y) * 0.5)
        else:
            scale_x = source_aspect / out_aspect
            uv_scale = (scale_x, 1.0)
            uv_offset = ((1.0 - scale_x) * 0.5, 0.0)
        self._use_target(pool['fc_display_fbo'], (fw, fh))
        source_texture.use(0)
        prog['u_sample_tex'].value = 0
        prog['u_uv_scale'].value = uv_scale
        prog['u_uv_offset'].value = uv_offset
        prog['u_fc_mode'].value = _FC_MODE_CODES.get(mode, 0)
        prog['u_fc_transfer'].value = _TRANSFER_CODES.get(transfer, 0)
        prog['u_zebra_threshold'].value = float(zebra_threshold)
        self._render_quad(prog)

    # ------------------------------------------------------------------
    # Convenience: run a full demand
    # ------------------------------------------------------------------
    def render_demand(self, demand, source_texture: moderngl.Texture,
                      mask_texture: Optional[moderngl.Texture] = None,
                      mask_uv_rect: tuple = (0.0, 0.0, 1.0, 1.0)) -> dict:
        """Run every pass requested by ``demand.mode``.

        Args:
            demand: ``scope_types.ScopeDemand``（mode / analysis_size /
                vectorscope_zoom / scope_transfer / scope_primaries /
                falsecolor_mode / zebra_threshold）。

        Returns a dict of mode -> (texture_id, (w, h)).  The caller owns the
        cross-context finish() and the ``ScopeGpuFrame`` publication.
        """
        from src.features.color.core.scope_types import (
            mode_needs_waveform, mode_needs_vectorscope, mode_needs_parade,
            mode_needs_luma, mode_needs_histogram, mode_needs_cie,
            mode_needs_falsecolor,
            DEFAULT_WAVEFORM_DENSITY_SIZE, DEFAULT_VECTORSCOPE_DENSITY_SIZE,
        )
        analysis = tuple(int(v) for v in demand.analysis_size)
        pool = self.ensure_pool(
            analysis, DEFAULT_WAVEFORM_DENSITY_SIZE,
            DEFAULT_VECTORSCOPE_DENSITY_SIZE,
        )
        mode = demand.mode
        outputs: Dict[str, tuple] = {}

        needs_sample = (
            not mode_needs_falsecolor(mode)
            or bool(getattr(demand, 'histogram_overlay', False))
        )
        if needs_sample:
            self.sample(source_texture, pool,
                        mask_texture=mask_texture, mask_uv_rect=mask_uv_rect)

        if mode_needs_waveform(mode):
            self.accumulate_waveform(pool)
            self.display_waveform(pool)
            outputs['waveform'] = (pool['wf_display'].glo, pool['waveform_size'])
        if mode_needs_vectorscope(mode):
            self.accumulate_vectorscope(pool, demand.vectorscope_zoom)
            self.display_vectorscope(pool, demand.vectorscope_zoom)
            outputs['vectorscope'] = (pool['vs_display'].glo,
                                      pool['vectorscope_size'])
        if mode_needs_parade(mode):
            self.accumulate_parade(pool)
            self.display_parade(pool)
            outputs['parade'] = (pool['wf_display'].glo, pool['waveform_size'])
        if mode_needs_luma(mode):
            self.accumulate_waveform(pool, channel=3)
            self.display_waveform(pool, single_channel=True)
            outputs['luma'] = (pool['wf_display'].glo, pool['waveform_size'])
        if mode_needs_histogram(mode) or bool(getattr(demand, 'histogram_overlay', False)):
            self.accumulate_histogram(pool, include_luma=bool(getattr(demand, 'histogram_overlay', False)),
                                      curve_mode=demand.curves_mode)
            self.reduce_histogram(pool)
            self.display_histogram(pool)
            outputs['histogram'] = (pool['hist_display'].glo,
                                    HISTOGRAM_DISPLAY_SIZE)
            if bool(getattr(demand, 'histogram_overlay', False)):
                self.display_curves_histogram(pool, demand.curves_display_size, demand.curves_line_width,
                                              demand.curves_mode)
                outputs['curves_histogram'] = (pool['curves_hist_display'].glo,
                                               pool['curves_hist_display_size'])
        if mode_needs_cie(mode):
            self.accumulate_cie(pool, getattr(demand, 'scope_transfer', 'srgb'),
                                getattr(demand, 'scope_primaries', 'bt709'))
            self.display_cie(pool)
            outputs['cie'] = (pool['cie_display'].glo, CIE_SIZE)
        if mode_needs_falsecolor(mode):
            self.display_falsecolor(
                source_texture, pool,
                mode=getattr(demand, 'falsecolor_mode', 'sdr'),
                transfer=getattr(demand, 'scope_transfer', 'srgb'),
                zebra_threshold=getattr(demand, 'zebra_threshold', 1.0),
            )
            outputs['falsecolor'] = (pool['fc_display'].glo, FALSECOLOR_SIZE)
        return outputs
