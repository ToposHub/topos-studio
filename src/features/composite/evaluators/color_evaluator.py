"""
ColorEvaluatorMixin - Color grading and correction effect implementations.

Provides GPU-based color effects:
- Grade (lift/gamma/gain/offset) with HDR support
- HueCorrect (hue-based selective corrections)
- Normalize (range/percentile normalization with histogram)
- ColorCorrect (basic color corrections)

All effects support HDR workflows and optional mask input.
"""

import logging
from typing import Tuple, List

import moderngl

logger = logging.getLogger(__name__)

from ..nodes.base_node import TextureResult



class ColorEvaluatorMixin:
    """Mixin providing color grading and correction effect implementations."""

    @staticmethod
    def _set_uniform_if_present(prog, name: str, value) -> None:
        """Set a shader uniform only when the compiled program exposes it."""
        try:
            prog[name].value = value
        except KeyError:
            logger.debug("Shader uniform optimized out or unavailable: %s", name)

    @staticmethod
    def _grading_wheel_to_rgb(value) -> tuple[float, float, float]:
        """M05.2f：色轮 (hue, saturation, brightness) → RGB 乘子。

        与 Edit GPU 预览 ``_grade_params_to_uniforms`` 同一转换
        （``GradingMath.params_to_rgb`` 权威实现）——Composite 与 Edit
        的轮子 uniform 逐字节一致；lift/gamma/gain 公式在
        PRIMARY_GRADE_BODY 内，不做二次换算。
        """
        from src.features.color.core.the_decompiler import GradingMath
        try:
            hue, saturation, brightness = (float(v) for v in value)
        except (TypeError, ValueError):
            hue, saturation, brightness = (0.0, 0.0, 0.0)
        rgb = GradingMath.params_to_rgb(hue, saturation, brightness)
        return (float(rgb[0]), float(rgb[1]), float(rgb[2]))

    @staticmethod
    def _grading_hsb_to_rgb(value, *, lift: bool = False) -> tuple[float, float, float]:
        """Convert Topos grading HSB-style params to RGB shader values.

        Color page stores lift/gamma/gain as (hue, saturation, brightness),
        where (0, 0, 0) is neutral. The composite shader needs direct RGB
        offsets/multipliers.
        """
        try:
            hue, sat, bright = value
        except (TypeError, ValueError):
            hue, sat, bright = (0.0, 0.0, 0.0)

        hue = float(hue)
        sat = float(sat)
        bright = float(bright)
        stops = (bright ** 2.0) * 6.0
        if bright < 0.0:
            stops = -stops
        bright_mult = 2.0 ** stops

        if abs(sat) <= 1e-8:
            rgb = (bright_mult, bright_mult, bright_mult)
        else:
            h = hue / 60.0
            i = int(h // 1.0)
            f = h - float(i)
            p = 1.0 * (1.0 - sat)
            q = 1.0 * (1.0 - sat * f)
            t = 1.0 * (1.0 - sat * (1.0 - f))
            ii = i % 6
            if ii == 0:
                rgb = (1.0, t, p)
            elif ii == 1:
                rgb = (q, 1.0, p)
            elif ii == 2:
                rgb = (p, 1.0, t)
            elif ii == 3:
                rgb = (p, q, 1.0)
            elif ii == 4:
                rgb = (t, p, 1.0)
            else:
                rgb = (1.0, p, q)
            avg = max(1e-6, sum(rgb) / 3.0)
            rgb = tuple((channel / avg) * bright_mult for channel in rgb)

        if lift:
            return tuple((channel - 1.0) * 0.2 for channel in rgb)
        return tuple(max(0.001, channel) for channel in rgb)

    def _apply_grade(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply color grade (lift/gamma/gain/offset) with HDR support."""
        prog = self.utility_shaders.get('grade')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0
            prog['u_lift'].value = tuple(params.get("lift", [0, 0, 0]))
            prog['u_gamma'].value = tuple(params.get("gamma", [1, 1, 1]))
            prog['u_gain'].value = tuple(params.get("gain", [1, 1, 1]))
            prog['u_offset'].value = tuple(params.get("offset", [0, 0, 0]))

            # Extended parameters
            prog['u_blackpoint'].value = params.get("blackpoint", 0.0)
            prog['u_whitepoint'].value = params.get("whitepoint", 1.0)
            prog['u_multiply'].value = params.get("multiply", 1.0)
            prog['u_add'].value = params.get("add", 0.0)
            prog['u_channel_mask'].value = tuple(params.get("channel_mask", [1.0, 1.0, 1.0]))
            prog['u_mix'].value = params.get("mix", 1.0)

            # Handle optional mask input
            mask_tex = params.get("_mask_tex")
            if mask_tex and mask_tex.is_valid:
                self._bind_mask_texture(mask_tex.texture_id)
                prog['u_mask'].value = 1
            else:
                self._bind_mask_texture(self._get_white_mask_texture().glo)
                prog['u_mask'].value = 1

            self._render_quad(prog)
        except Exception:
            # AUD-5903: 出错帧解绑纹理单元并归还已获取的池纹理（标量
            # lift/gamma 等参数类型错误会让 tuple() 抛 TypeError）。
            self._unbind_texture_unit(1)
            self._release_effect_textures_on_error(out_tex)
            raise
        # P1-6: drop both binds after the draw (the white mask is a
        # cached evaluator-owned texture, nothing to return).
        self._unbind_texture_unit(0)
        self._unbind_texture_unit(1)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_color_correct(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply basic color corrections (saturation, contrast, brightness)."""
        prog = self.utility_shaders.get('color_correct')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0
            # AUD-5901/AUD-5902（= AUD-6500 根因）: shader (_COLOR_CORRECT_FRAGMENT)
            # 声明 13 个 uniform，ColorCorrectNode._effect_params 逐一对应产出。
            # 此前只映射 saturation/contrast/mix，其余 uniform 缺省 0 ——
            # u_channel_selector=(0,0,0,0) 会按通道把输出 mix 回原图，抵消全部
            # 校正；exposure/gamma 等 0 值也会扭曲结果。完整映射如下。
            prog['u_exposure'].value = float(params.get("exposure", 0.0))
            prog['u_contrast'].value = float(params.get("contrast", 1.0))
            prog['u_saturation'].value = float(params.get("saturation", 1.0))
            prog['u_hue_shift'].value = float(params.get("hue_shift", 0.0))
            prog['u_temperature'].value = float(params.get("temperature", 0.0))
            prog['u_tint'].value = float(params.get("tint", 0.0))
            prog['u_shadows_lifting'].value = tuple(
                float(v) for v in params.get("shadows_lifting", (0.0, 0.0, 0.0)))
            prog['u_midtones_gamma'].value = tuple(
                float(v) for v in params.get("midtones_gamma", (1.0, 1.0, 1.0)))
            prog['u_highlights_gain'].value = tuple(
                float(v) for v in params.get("highlights_gain", (1.0, 1.0, 1.0)))
            prog['u_channel_selector'].value = tuple(
                float(v) for v in params.get("channel_selector", (1.0, 1.0, 1.0, 0.0)))
            prog['u_mix'].value = float(params.get("mix", 1.0))

            # Handle optional mask
            mask_tex = params.get("_mask_tex")
            if mask_tex and mask_tex.is_valid:
                self._bind_mask_texture(mask_tex.texture_id)
                prog['u_mask'].value = 1
            else:
                self._bind_mask_texture(self._get_white_mask_texture().glo)
                prog['u_mask'].value = 1

            self._render_quad(prog)
        except Exception:
            # AUD-5903: 出错帧解绑 mask 单元并归还已获取的池纹理。
            self._unbind_texture_unit(1)
            self._release_effect_textures_on_error(out_tex)
            raise

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_cdl(self, input_tex: TextureResult, params: dict,
                   w: int, h: int) -> dict:
        """CP-015: ASC CDL — slope/power/offset + luma saturation."""
        if not input_tex or not input_tex.is_valid:
            return {}
        return self._apply_simple_per_pixel(
            "cdl", input_tex, params, {
                'u_texture': 0,
                'u_slope': params.get("slope", (1.0, 1.0, 1.0)),
                'u_offset': params.get("offset", (0.0, 0.0, 0.0)),
                'u_power': params.get("power", (1.0, 1.0, 1.0)),
                'u_saturation': float(params.get("saturation", 1.0)),
            }, w, h)

    def _apply_colorlookup(self, input_tex: TextureResult, params: dict,
                           w: int, h: int) -> dict:
        """CP-014: master/R/G/B curve lookup via a baked 256x4 LUT.

        Curves arrive as parsed control-point lists; they are baked on the
        CPU (linear interpolation, end-hold outside the span) into one
        RGBA32F texture — row 0=R, 1=G, 2=B, 3=master — and applied in a
        single pass: out = master(lut_ch(value)).
        """
        import numpy as np
        from ..nodes.colorlookup_node import bake_curve_lut

        if not input_tex or not input_tex.is_valid:
            return {}
        prog = self.utility_shaders.get('colorlookup')
        if prog is None:
            return {"result": input_tex}

        input_min = float(params.get("input_min", 0.0))
        input_max = float(params.get("input_max", 1.0))
        identity = [(0.0, 0.0), (1.0, 1.0)]
        rows = []
        for key in ("red_curve", "green_curve", "blue_curve", "master_curve"):
            points = params.get(key) or identity
            rows.append(bake_curve_lut(points, input_min, input_max))
        lut = np.zeros((4, 256, 4), dtype=np.float32)
        for row_idx, row in enumerate(rows):
            lut[row_idx, :, 0] = row

        lut_tex = self.ctx.texture((256, 4), 4, dtype="f4")
        lut_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        lut_tex.write(lut.tobytes())

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        try:
            self._bind_external_texture(input_tex.texture_id)
            self._set_uniform_if_present(prog, 'u_texture', 0)
            lut_tex.use(1)
            self._set_uniform_if_present(prog, 'u_lut', 1)
            self._set_uniform_if_present(prog, 'u_input_min', input_min)
            self._set_uniform_if_present(prog, 'u_input_max', input_max)
            self._render_quad(prog)
        finally:
            lut_tex.release()

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_huecorrect(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply hue-based color corrections (hue shift, saturation, brightness).

        Supports selective adjustments based on hue range with soft edges.
        Designed for HDR workflows — no clamping on brightness multiplier.
        """
        prog = self.utility_shaders.get('huecorrect')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0

            prog['u_hue_shift'].value = params.get("hue_shift", 0.0)
            prog['u_saturation_mult'].value = params.get("saturation_mult", 1.0)
            prog['u_brightness_mult'].value = params.get("brightness_mult", 1.0)
            prog['u_range_low'].value = params.get("range_low", 0.0)
            prog['u_range_high'].value = params.get("range_high", 360.0)
            prog['u_range_softness'].value = params.get("range_softness", 10.0)
            prog['u_mix'].value = params.get("mix", 1.0)

            mask_tex = params.get("_mask_tex")
            if mask_tex and mask_tex.is_valid:
                self._bind_mask_texture(mask_tex.texture_id)
                prog['u_mask'].value = 1
            else:
                self._bind_mask_texture(self._get_white_mask_texture().glo)
                prog['u_mask'].value = 1

            self._render_quad(prog)
        except Exception:
            # AUD-5903: 出错帧解绑 mask 单元并归还已获取的池纹理。
            self._unbind_texture_unit(1)
            self._release_effect_textures_on_error(out_tex)
            raise

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_normalize(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply value range normalization to specified channels.

        For percentile mode, uses GPU histogram compute shader to find
        appropriate input_min/input_max values based on image content.
        """
        prog = self.utility_shaders.get('normalize')
        if prog is None:
            return {"result": input_tex}

        mode = params.get("mode", "range")
        input_min = params.get("input_min", 0.0)
        input_max = params.get("input_max", 1.0)
        output_min = params.get("output_min", 0.0)
        output_max = params.get("output_max", 1.0)
        percentile_low = params.get("percentile_low", 1.0)
        percentile_high = params.get("percentile_high", 99.0)
        clamp_output = params.get("clamp_output", True)
        channel_mask = params.get("channel_mask", [1.0, 1.0, 1.0, 0.0])

        # For percentile mode, compute input range from histogram
        if mode == "percentile":
            input_min, input_max = self._compute_percentile_range(
                input_tex, w, h, percentile_low, percentile_high, channel_mask
            )

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0

            prog['u_input_min'].value = input_min
            prog['u_input_max'].value = input_max
            prog['u_output_min'].value = output_min
            prog['u_output_max'].value = output_max
            prog['u_clamp_output'].value = clamp_output
            prog['u_channel_mask'].value = tuple(channel_mask)

            self._render_quad(prog)
        except Exception:
            # AUD-5903: 出错帧归还已获取的池纹理（channel_mask 为标量时
            # tuple() 抛 TypeError）。
            self._release_effect_textures_on_error(out_tex)
            raise

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_invert(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply channel inversion."""
        prog = self.utility_shaders.get('invert')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0
            channels = params.get("channels", [1.0, 1.0, 1.0, 0.0])
            prog['u_channels'].value = tuple(channels)
            self._render_quad(prog)
        except Exception:
            # AUD-5903: 出错帧归还已获取的池纹理（channels 为标量时
            # tuple() 抛 TypeError）。
            self._release_effect_textures_on_error(out_tex)
            raise

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_premult(self, input_tex: TextureResult, w: int, h: int) -> dict:
        """Apply premultiply: RGB *= Alpha (straight → premultiplied)."""
        prog = self.utility_shaders.get('premult')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0
            self._render_quad(prog)
        except Exception:
            # AUD-5903: 出错帧归还已获取的池纹理。
            self._release_effect_textures_on_error(out_tex)
            raise

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=True,
                color_space=input_tex.color_space,
            )
        }

    def _apply_unpremult(self, input_tex: TextureResult, w: int, h: int) -> dict:
        """Apply unpremultiply: RGB /= Alpha (premultiplied → straight)."""
        prog = self.utility_shaders.get('unpremult')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0
            self._render_quad(prog)
        except Exception:
            # AUD-5903: 出错帧归还已获取的池纹理。
            self._release_effect_textures_on_error(out_tex)
            raise

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=False,
                color_space=input_tex.color_space,
            )
        }

    def _apply_clamp(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply value clamping to specified channels."""
        prog = self.utility_shaders.get('clamp')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        try:
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            prog['u_texture'].value = 0

            min_val = params.get("min", 0.0)
            max_val = params.get("max", 1.0)
            channels = params.get("channels", "rgba")

            mask = [0.0, 0.0, 0.0, 0.0]
            min_v = [0.0, 0.0, 0.0, 0.0]
            max_v = [1.0, 1.0, 1.0, 1.0]
            ch_map = {'r': 0, 'g': 1, 'b': 2, 'a': 3}
            for ch_name, idx in ch_map.items():
                if ch_name in channels:
                    mask[idx] = 1.0
                    min_v[idx] = min_val
                    max_v[idx] = max_val
                else:
                    min_v[idx] = 0.0
                    max_v[idx] = 1.0

            prog['u_min_val'].value = tuple(min_v)
            prog['u_max_val'].value = tuple(max_v)
            prog['u_channel_mask'].value = tuple(mask)
            self._render_quad(prog)
        except Exception:
            # AUD-5903: 出错帧归还已获取的池纹理（channels 非 str 时
            # `ch_name in channels` 可抛 TypeError）。
            self._release_effect_textures_on_error(out_tex)
            raise

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_topos_color(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply Topos Color Stack grading to input image.

        Uses the full ColorStackData grading parameters:
        exposure, temp, tint, contrast, lift/gamma/gain, shadow, highlights,
        saturation, vibrance, mid_detail, hue_shift, intensity.

        M05 · Composite 链段消费：主栈所属 clip 的 pre/trim/post 链段以
        顺序 GPU pass 应用（pre 段在主 pass 前、trim/post 段在后——与
        Edit 预览/导出同序，非线性参数非交换；段经独立 scratch 纹理
        ping-pong，最终段落在 out_tex）。
        """
        # Resolve ColorStack grading params from the stack source
        from src.features.color.core.color_stack_render_service import ColorStackRenderService
        service = ColorStackRenderService()

        resolved = service.resolve_grading_params(
            stack_source=params.get("stack_source", "clip_stack"),
            color_stack_id=params.get("color_stack_id", ""),
            preset_id=params.get("preset_id", ""),
            embedded_stack=params.get("embedded_stack"),
            project=getattr(self, '_project', None),
            runtime_context=getattr(self, '_runtime_context', None),
        )

        # Merge resolved params into effect params
        merged_params = {**params, **resolved}

        # N01：Composite ToposColor 出口的 B13 式硬门——栈携带不可投影
        # 复杂图时显式拒绝（此出口只承载 primary 域，静默压扁=「成功但
        # 少效果」；与导出同口径 RuntimeError）。
        project = getattr(self, '_project', None)
        stack = None
        stack_id_ref = str(params.get("color_stack_id") or "")
        if project is not None and stack_id_ref:
            stack = (getattr(project, "color_stacks", {}) or {}).get(
                stack_id_ref)
        color_graph = getattr(stack, "color_graph", None)
        if color_graph is not None:
            from src.features.color.core.color_graph_migration import (
                can_project_to_legacy_layers,
            )
            if not can_project_to_legacy_layers(color_graph):
                raise RuntimeError(
                    "[color_graph_not_exportable] Composite ToposColor "
                    "不支持复杂图（primary-only 出口；显式拒绝而非静默"
                    "压扁——完整图渲染在 Color 页/导出路径）")

        prog = self.utility_shaders.get('topos_color')
        if prog is None:
            return {"result": input_tex}

        # M05：链段解析（producer 侧见 color_chain.chain_for_stack）
        chain_pre, chain_after = self._resolve_topos_chain_segments(
            str(params.get("color_stack_id") or ""))
        grading = merged_params.get("grading_params", {})

        out_tex = self._resource_pool.acquire_texture(w, h)
        scratch_texs = []
        try:
            # 顺序 ping-pong：pre… → 主 → after…；最终段落在 out_tex。
            # (grading, is_main)：主 pass 用节点 mask/intensity，链段
            # （组级调色）白色全通 + 全量——与 Edit 链段语义一致。
            segments = ([(g, False) for g in chain_pre]
                        + [(grading, True)]
                        + [(g, False) for g in chain_after])
            src_glo = input_tex.texture_id
            for i, (seg_grading, is_main) in enumerate(segments):
                if i == len(segments) - 1:
                    dst = out_tex
                else:
                    dst = self._resource_pool.acquire_texture(w, h)
                    scratch_texs.append(dst)
                self._render_topos_grade_pass(
                    src_glo, dst, seg_grading, params, w, h,
                    is_main_pass=is_main)
                src_glo = dst.glo
            stabilization = merged_params.get('video_stabilization') or {}
            if stabilization:
                from src.features.shared.effects.video_stabilization import apply_stabilization_gpu
                from src.features.shared.effects.effect_pass_runner import EffectPassRunner
                runtime = getattr(self, '_runtime_context', None)
                frame = int(params.get('source_frame', getattr(
                    runtime, 'source_frame', getattr(getattr(self, '_snapshot', None), 'frame_number', 0))))
                runner = self.__dict__.get('_stabilization_runner')
                if runner is None:
                    runner = EffectPassRunner(self.ctx)
                    self._stabilization_runner = runner
                result_glo = apply_stabilization_gpu(runner, out_tex.glo, stabilization, frame, w, h)
                if result_glo != out_tex.glo:
                    target = self.fb_manager.get_fbo(out_tex)
                    source = self.fb_manager.get_fbo(runner.output_texture())
                    self.ctx.copy_framebuffer(target, source)
            # 成功路径：scratch 归还池（所有权已消费完毕）
            for tex in scratch_texs:
                try:
                    self._resource_pool.release_texture(tex)
                except Exception:  # noqa: BLE001 — 归还失败不掩盖渲染结果
                    logger.debug("chain scratch release failed", exc_info=True)
        except Exception:
            # AUD-5903: 出错帧解绑 mask 单元并归还已获取的池纹理
            # （含链段 scratch——出错的链段消费不得泄漏池预算）。
            self._unbind_texture_unit(1)
            self._release_effect_textures_on_error(out_tex, *scratch_texs)
            raise

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _resolve_topos_chain_segments(self, stack_id: str) -> tuple:
        """M05：主栈所属 clip 的链段（pre 组 / 其余两组分列）。

        高级标记段（curves/LUT 等不可在 primary GPU pass 表达）跳过并
        留 debug——与 Edit 预览的降级语义一致，不静默丢段。解析失败按
        无链段（不静默应用错链）。
        """
        pre: list = []
        after: list = []
        if not stack_id:
            return pre, after
        project = getattr(self, "_project", None)
        if project is None:
            return pre, after
        try:
            from src.features.edit.core.color_chain import chain_for_stack
            for entry in chain_for_stack(project, stack_id):
                if not isinstance(entry, dict):
                    continue
                if entry.get("_unsupported_advanced", False):
                    logger.debug(
                        "composite color chain: stack %s 链段含 curves/"
                        "LUT 等高级操作，GPU 链消费跳过该段（与 Edit "
                        "预览同语义）", stack_id)
                    continue
                seg = {k: v for k, v in entry.items()
                       if k not in ("_unsupported_advanced",
                                    "_chain_segment")}
                (pre if entry.get("_chain_segment") == "pre"
                 else after).append(seg)
        except Exception:  # noqa: BLE001 — 链段解析失败按无链段
            logger.debug("composite color chain resolution failed",
                         exc_info=True)
        return pre, after

    def _render_topos_grade_pass(self, src_glo: int, dst_tex, grading: dict,
                                 params: dict, w: int, h: int,
                                 is_main_pass: bool = True) -> None:
        """单次 topos_color grade pass（src → dst；链段与主 pass 共用）。

        ``is_main_pass``：主 pass 用节点 mask 与节点 intensity；链段
        （组级调色）不受节点 mask 限定、不经节点 intensity——与 Edit
        链段语义一致。
        """
        prog = self.utility_shaders.get('topos_color')
        if prog is None:
            raise RuntimeError("topos_color shader unavailable")
        out_fbo = self.fb_manager.get_fbo(dst_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(src_glo)
        self._set_uniform_if_present(prog, 'u_texture', 0)

        # M05.2f：轮子经 GradingMath.params_to_rgb（与 Edit GPU 预览
        # _grade_params_to_uniforms 同一转换）；标量 0-100 域直传
        # （PRIMARY_GRADE_BODY 同域）。mid_detail/sharpness 纹理类
        # 不属 primary 域——不再设置。
        self._set_uniform_if_present(
            prog, 'u_lift',
            self._grading_wheel_to_rgb(grading.get("lift", (0, 0, 0))))
        self._set_uniform_if_present(
            prog, 'u_gamma',
            self._grading_wheel_to_rgb(grading.get("gamma", (0, 0, 0))))
        self._set_uniform_if_present(
            prog, 'u_gain',
            self._grading_wheel_to_rgb(grading.get("gain", (0, 0, 0))))
        self._set_uniform_if_present(prog, 'u_exposure', float(grading.get("exposure", 0)))
        self._set_uniform_if_present(prog, 'u_temp', float(grading.get("temp", 0)))
        self._set_uniform_if_present(prog, 'u_tint', float(grading.get("tint", 0)))
        self._set_uniform_if_present(prog, 'u_contrast', float(grading.get("contrast", 0)))
        self._set_uniform_if_present(prog, 'u_shadow', float(grading.get("shadow", 0)))
        self._set_uniform_if_present(prog, 'u_highlights', float(grading.get("highlights", 0)))
        self._set_uniform_if_present(prog, 'u_saturation', float(grading.get("saturation", 0)))
        self._set_uniform_if_present(prog, 'u_vibrance', float(grading.get("vibrance", 0)))
        self._set_uniform_if_present(prog, 'u_hue_shift', float(grading.get("hue_shift", 0)))
        # 链段不经节点 intensity（组级调色全量；主 pass 用节点值）
        intensity = (float(params.get("intensity", 1.0))
                     if is_main_pass else 1.0)
        self._set_uniform_if_present(prog, 'u_intensity', intensity)

        # Mask support（主 pass：节点 mask；链段：白色全通）
        mask_tex = params.get("_mask_tex") if is_main_pass else None
        if mask_tex and mask_tex.is_valid:
            self._bind_mask_texture(mask_tex.texture_id)
        else:
            white_tex = getattr(self, '_topos_color_white_mask_tex', None)
            if white_tex is None:
                white_tex = self.ctx.texture((1, 1), 4, data=bytes([255, 255, 255, 255]), dtype='f1')
                self._topos_color_white_mask_tex = white_tex
            self._bind_mask_texture(white_tex.glo)
        self._set_uniform_if_present(prog, 'u_mask', 1)

        self._render_quad(prog)

    # ── Histogram Percentile Computation ──

    def _compute_percentile_range(self, input_tex: TextureResult, w: int, h: int,
                                   percentile_low: float, percentile_high: float,
                                   channel_mask: List[float]) -> Tuple[float, float]:
        """Compute input range from histogram percentile analysis.

        Uses GPU histogram compute shader for efficient computation.
        """
        if not hasattr(self, '_histogram_manager') or self._histogram_manager is None:
            from src.features.composite.evaluators.histogram_manager import HistogramManager
            self._histogram_manager = HistogramManager(self.ctx)

        if not self._histogram_manager.is_available():
            logger.debug("Histogram compute unavailable, using default range")
            return (0.0, 1.0)

        try:
            actual_tex = self._get_texture_by_id(input_tex.texture_id)
            if actual_tex is None:
                logger.warning("Texture object not found, using fallback")
                return (0.0, 1.0)

            result = self._histogram_manager.compute_percentiles(
                actual_tex, w, h,
                percentile_low, percentile_high,
                channel_mask,
                fb_manager=self.fb_manager,
                resource_pool=self._resource_pool,
                use_cache=True,
            )

            return (result['low'], result['high'])

        except Exception as e:
            logger.warning(f"Histogram computation error: {e}")
            return (0.0, 1.0)
