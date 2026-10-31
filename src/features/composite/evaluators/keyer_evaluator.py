"""
KeyerEvaluatorMixin - Keying effect implementations.

Provides GPU-based keying effects:
- Keyer (unified entry point with automatic mode detection)
- Luma Keyer (luminance-based matte generation)
- Chroma Keyer (color-based transparency)
- Difference Keyer (compare foreground vs background)
- Matte (luminance-based matte with softness)

All keyers support invert mode and output_mode (result/matte).
"""

import logging

from ..nodes.base_node import TextureResult

logger = logging.getLogger(__name__)



class KeyerEvaluatorMixin:
    """Mixin providing keying effect implementations."""

    def _apply_light_wrap(self, outputs: dict) -> dict:
        """Apply background-driven edge light to a keyed foreground.

        The node emits background as ``_effect_input_a`` and foreground as
        ``_effect_input_b``.  Sampling and texture allocation stay on the
        render thread, while this method preserves the foreground's canvas
        metadata for downstream merges and export.
        """
        background = outputs.get("_effect_input_a")
        foreground = outputs.get("_effect_input_b")
        if (background is None or not background.is_valid or
                foreground is None or not foreground.is_valid):
            return {}

        prog = self.utility_shaders.get("light_wrap")
        if prog is None:
            return {"result": foreground}

        width = max(1, int(max(background.width, foreground.width)))
        height = max(1, int(max(background.height, foreground.height)))
        out_tex = self._resource_pool.acquire_texture(width, height)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, width, height)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_texture_unit(background.texture_id, 0)
        self._bind_texture_unit(foreground.texture_id, 1)
        try:
            params = outputs.get("_effect_params", {})
            prog["u_background"].value = 0
            prog["u_foreground"].value = 1
            prog["u_background_resolution"].value = (
                float(max(1, background.width)),
                float(max(1, background.height)),
            )
            prog["u_foreground_resolution"].value = (
                float(max(1, foreground.width)),
                float(max(1, foreground.height)),
            )
            prog["u_width"].value = max(0.0, float(params.get("width", 12.0)))
            prog["u_intensity"].value = max(
                0.0, float(params.get("intensity", 0.35)))
            prog["u_brightness"].value = max(
                0.0, float(params.get("brightness", 1.0)))
            prog["u_falloff"].value = max(
                0.1, float(params.get("falloff", 1.0)))
            prog["u_background_premultiplied"].value = bool(
                background.alpha_premultiplied)
            prog["u_foreground_premultiplied"].value = bool(
                foreground.alpha_premultiplied)
            prog["u_source_alpha"].value = bool(
                params.get("source_alpha", True))
            self._render_quad(prog)
        finally:
            self._unbind_texture_unit(1)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo,
                width=width,
                height=height,
                format=foreground.format or background.format,
                bbox=foreground.bbox or background.bbox,
                alpha_premultiplied=True,
                color_space=foreground.color_space or background.color_space,
                bit_depth=max(foreground.bit_depth, background.bit_depth),
                frame_metadata=foreground.frame_metadata or background.frame_metadata,
            )
        }

    def _apply_keyer(self, outputs: dict) -> dict:
        """Apply unified keying with automatic mode detection.

        Dispatches to specific keyer implementation based on keyer_mode param.
        Supports:
        - chroma: Keylight 1.2-style color-difference keying (default).
        - luma:   luminance-based keying.
        - difference: compare two images.

        Args:
            outputs: Effect marker dict containing:
                - _effect_input: Primary input texture (for chroma/luma)
                - _effect_input_a: Foreground texture (for difference)
                - _effect_input_b: Reference texture (for difference)
                - _effect_params: Dict with keyer_mode, clip_*, gain, softness, etc.

        Returns:
            Dict with "result" TextureResult.
        """
        effect_params = outputs.get("_effect_params", {})
        keyer_mode = effect_params.get("keyer_mode", "chroma")

        # Get common params
        clip_black = effect_params.get("clip_black", 0.0)
        clip_white = effect_params.get("clip_white", 1.0)
        gain = effect_params.get("gain", 1.0)
        softness = effect_params.get("softness", 0.0)
        alpha_offset = effect_params.get("alpha_offset", 0.0)
        invert = effect_params.get("invert", False)
        output_mode = effect_params.get("output_mode", "result")

        if keyer_mode == "difference":
            # Difference keyer: uses input_a and input_b
            input_a = outputs.get("_effect_input_a")
            input_b = outputs.get("_effect_input_b")
            if input_a is None or not input_a.is_valid:
                return {}
            if input_b is None or not input_b.is_valid:
                return {"result": input_a}
            w, h = input_a.width, input_a.height
            diff_params = {
                # Map unified clip/gain/softness to legacy difference_keyer API.
                "threshold": 1.0 - clip_white,  # treat clip_white as "above this = opaque"
                "softness": softness * 0.5,
                "gain": max(gain, 0.001),
                "channel_mode": "luma",
                "invert": invert,
                "output_mode": output_mode,
            }
            return self._apply_difference_keyer(input_a, input_b, diff_params, w, h)

        # Chroma/Luma: uses single _effect_input
        effect_input = outputs.get("_effect_input")
        if effect_input is None or not effect_input.is_valid:
            return {}

        w, h = effect_input.width, effect_input.height

        if keyer_mode == "chroma":
            # Keylight 1.2-style chroma keyer.
            screen_color = effect_params.get("screen_color", (0.0, 1.0, 0.0))
            screen_balance = effect_params.get("screen_balance", 0.5)
            despill_strength = effect_params.get("despill_strength", 0.5)
            despill_method = int(effect_params.get("despill_method", 1))
            chroma_params = {
                "screen_color": screen_color,
                "screen_balance": screen_balance,
                "clip_black": clip_black,
                "clip_white": clip_white,
                "gain": gain,
                "softness": softness,
                "alpha_offset": alpha_offset,
                "despill_strength": despill_strength,
                "despill_method": despill_method,
                "invert": invert,
                "output_mode": output_mode,
            }
            return self._apply_keylight(effect_input, chroma_params, w, h)

        elif keyer_mode == "luma":
            # Luma keyer: use clip_black/white + softness directly.
            luma_params = {
                "black_point": clip_black,
                "white_point": clip_white,
                "softness": softness,
                "invert": invert,
                "output_mode": output_mode,
            }
            return self._apply_luma_keyer(effect_input, luma_params, w, h)

        # Default: return input unchanged
        return {"result": effect_input}

    def _apply_keylight(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply Keylight 1.2-style chroma keying.

        Uses colour-difference keying: projects source + screen colour into
        YCbCr space, measures screen-axis distance, then maps that distance
        to a matte via clip_black/white + gain + softness + despill.
        """
        prog = self.utility_shaders.get('keylight')
        if prog is None:
            # Fallback to legacy chroma_keyer behaviour if the keylight shader
            # is unavailable (older caches), so the node never hard-crashes.
            # AUD-6101: _apply_chroma_keyer reads the r/g/b triplet through
            # separate ``screen_color_r/g/b`` keys — unpack the user's tuple
            # into those keys so the degraded path keeps the configured
            # screen colour instead of silently falling back to default
            # green.  Malformed values (wrong length / non-numeric) fall
            # back to the keylight default rather than raising.
            screen_color = params.get("screen_color", (0.0, 1.0, 0.0))
            try:
                sc_r, sc_g, sc_b = (float(screen_color[0]),
                                    float(screen_color[1]),
                                    float(screen_color[2]))
            except (TypeError, ValueError, IndexError, KeyError):
                sc_r, sc_g, sc_b = 0.0, 1.0, 0.0
            legacy = {
                "screen_color_r": sc_r,
                "screen_color_g": sc_g,
                "screen_color_b": sc_b,
                "tolerance": params.get("clip_black", 0.0) * 0.5 + 0.2,
                "softness": params.get("softness", 0.1),
                "despill": params.get("despill_strength", 0.0) > 0.001,
                "edge_width": params.get("softness", 0.1) * 5.0,
                "output_mode": params.get("output_mode", "result"),
            }
            return self._apply_chroma_keyer(input_tex, legacy, w, h)

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0

        screen_color = params.get("screen_color", (0.0, 1.0, 0.0))
        prog['u_screen_color'].value = (
            float(screen_color[0]),
            float(screen_color[1]),
            float(screen_color[2]),
        )
        prog['u_screen_balance'].value = float(params.get("screen_balance", 0.5))
        prog['u_clip_black'].value = float(params.get("clip_black", 0.0))
        prog['u_clip_white'].value = float(params.get("clip_white", 1.0))
        prog['u_gain'].value = float(params.get("gain", 1.0))
        prog['u_softness'].value = float(params.get("softness", 0.1))
        prog['u_alpha_offset'].value = float(params.get("alpha_offset", 0.0))
        prog['u_despill_strength'].value = float(params.get("despill_strength", 0.5))
        prog['u_despill_method'].value = int(params.get("despill_method", 1))
        prog['u_invert'].value = bool(params.get("invert", False))

        output_mode = params.get("output_mode", "result")
        prog['u_output_mode'].value = 1 if output_mode == "matte" else 0

        self._render_quad(prog)

        is_premult = output_mode == "result"

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=is_premult,
                color_space=input_tex.color_space,
            )
        }

    def _apply_luma_keyer(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply luma-based keying to generate a matte."""
        prog = self.utility_shaders.get('luma_keyer')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0

        black_point = params.get("black_point", 0.0)
        white_point = params.get("white_point", 1.0)
        softness = params.get("softness", 0.0)
        invert = params.get("invert", False)
        output_mode = params.get("output_mode", "result")

        prog['u_black_point'].value = black_point
        prog['u_white_point'].value = white_point
        prog['u_softness'].value = softness
        prog['u_invert'].value = invert
        prog['u_output_mode'].value = 1 if output_mode == "matte" else 0

        self._render_quad(prog)

        is_premult = output_mode == "result"

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=is_premult,
                color_space=input_tex.color_space,
            )
        }

    def _apply_chroma_keyer(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply chroma keying (color-based transparency).

        求值器与 shader/节点的参数模型必须一致：节点暴露的是
        screen_color_r/g/b + tolerance/softness/despill/edge_width/output_mode
        （见 ChromaKeyerNode ParamDef 与 _CHROMA_KEYER_FRAGMENT 的
        u_screen_color 模型）。旧实现按 hue_center/hue_width 模型赋值，
        shader 里不存在这些 uniform → KeyError → 节点整体失效。
        """
        prog = self.utility_shaders.get('chroma_keyer')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0
        prog['u_screen_color'].value = (
            float(params.get("screen_color_r", 0.0)),
            float(params.get("screen_color_g", 1.0)),
            float(params.get("screen_color_b", 0.0)))
        prog['u_tolerance'].value = float(params.get("tolerance", 0.3))
        prog['u_softness'].value = float(params.get("softness", 0.1))
        prog['u_despill'].value = bool(params.get("despill", True))
        prog['u_edge_width'].value = float(params.get("edge_width", 2.0))
        output_mode = params.get("output_mode", "result")
        prog['u_output_mode'].value = 1 if output_mode == "matte" else 0

        self._render_quad(prog)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=False,
                color_space=input_tex.color_space,
            )
        }

    def _apply_difference_keyer(self, input_a: TextureResult, input_b: TextureResult,
                                 params: dict, w: int, h: int) -> dict:
        """Apply difference-based keying between two images.

        Generates a matte by comparing pixel differences between foreground
        (input_a) and reference background (input_b).
        """
        if input_a is None or not input_a.is_valid:
            return {}
        if input_b is None or not input_b.is_valid:
            return {"result": input_a}

        prog = self.utility_shaders.get('difference_keyer')
        if prog is None:
            return {"result": input_a}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_a.texture_id)
        prog['u_texture_a'].value = 0

        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(input_b.texture_id, 1)
        prog['u_texture_b'].value = 1

        threshold = params.get("threshold", 0.1)
        softness = params.get("softness", 0.05)
        gain = params.get("gain", 1.0)
        channel_mode = params.get("channel_mode", "luma")
        invert = params.get("invert", False)
        output_mode = params.get("output_mode", "result")

        prog['u_threshold'].value = threshold
        prog['u_softness'].value = softness
        prog['u_gain'].value = gain

        channel_mode_idx = 0
        if channel_mode == "rgb":
            channel_mode_idx = 1
        elif channel_mode == "individual":
            channel_mode_idx = 2
        prog['u_channel_mode'].value = channel_mode_idx

        prog['u_invert'].value = invert
        prog['u_output_mode'].value = 1 if output_mode == "matte" else 0

        self._render_quad(prog)

        bind_texture_unit(0, 1)

        is_premult = output_mode == "result"

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_a.format,
                bbox=input_a.bbox,
                alpha_premultiplied=is_premult,
                color_space=input_a.color_space,
            )
        }

    def _apply_matte(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply luminance-based matte generation.

        Creates a matte based on the luminance (brightness) values of the input.
        """
        prog = self.utility_shaders.get('matte')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0

        low_threshold = params.get("low_threshold", 0.0)
        high_threshold = params.get("high_threshold", 1.0)
        low_softness = params.get("low_softness", 0.1)
        high_softness = params.get("high_softness", 0.1)
        invert = params.get("invert", False)
        output_mode = params.get("output_mode", "matte")

        prog['u_low_threshold'].value = low_threshold
        prog['u_high_threshold'].value = high_threshold
        prog['u_low_softness'].value = low_softness
        prog['u_high_softness'].value = high_softness
        prog['u_invert'].value = invert
        prog['u_output_mode'].value = 0 if output_mode == "matte" else 1

        self._render_quad(prog)

        is_premult = output_mode == "result"

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=is_premult,
                color_space=input_tex.color_space,
            )
        }

    def _apply_matte_cleanup(self, input_tex: TextureResult,
                             params: dict, w: int, h: int) -> dict:
        """Apply alpha-aware matte cleanup operations.

        The node keeps its historical ``image`` output port, while the
        evaluator utilities return their canonical ``result`` key.  Normalize
        that boundary here so existing graphs remain connected after the
        placeholder implementation is replaced.
        """
        if input_tex is None or not input_tex.is_valid:
            return {}

        operation = str(params.get("operation", "blur"))
        channels = str(params.get("channels", "alpha"))
        if channels not in {"alpha", "all"}:
            channels = "alpha"

        if operation in {"erode", "dilate"}:
            result = self._apply_erode_dilate(input_tex, {
                "operation": operation,
                "size": max(1, int(params.get("size", 3))),
                "iterations": max(1, int(params.get("iterations", 1))),
                "edge_mode": "extend",
                "channels": channels,
            }, w, h)
        elif operation == "blur":
            result = self._apply_blur(input_tex, {
                "blur_type": "gaussian",
                "radius_x": max(0.0, float(params.get("blur_amount", 2.0))),
                "radius_y": max(0.0, float(params.get("blur_amount", 2.0))),
                "channel_r": channels == "all",
                "channel_g": channels == "all",
                "channel_b": channels == "all",
                "channel_a": True,
                "mix": 1.0,
                "quality": "normal",
            }, w, h)
        elif operation == "clamp":
            result = self._apply_clamp(input_tex, {
                "min": float(params.get("black_point", 0.0)),
                "max": float(params.get("white_point", 1.0)),
                "channels": "rgba" if channels == "all" else "a",
            }, w, h)
        elif operation == "invert":
            result = self._apply_invert(input_tex, {
                "channels": [
                    1.0 if channels == "all" else 0.0,
                    1.0 if channels == "all" else 0.0,
                    1.0 if channels == "all" else 0.0,
                    1.0,
                ],
            }, w, h)
        elif operation == "levels":
            result = self._apply_matte_cleanup_levels(input_tex, params, w, h,
                                                      channels)
        else:
            logger.warning("Unknown matte cleanup operation %r; passing through", operation)
            result = {"result": input_tex}

        texture = result.get("result") if isinstance(result, dict) else None
        return {"image": texture} if isinstance(texture, TextureResult) else {}

    def _apply_matte_cleanup_levels(self, input_tex: TextureResult,
                                    params: dict, w: int, h: int,
                                    channels: str) -> dict:
        """Run the alpha-aware levels shader for matte cleanup."""
        prog = self.utility_shaders.get("matte_cleanup")
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog["u_texture"].value = 0
        prog["u_black_point"].value = max(
            0.0, min(1.0, float(params.get("black_point", 0.0))))
        prog["u_white_point"].value = max(
            0.0, min(1.0, float(params.get("white_point", 1.0))))
        prog["u_gamma"].value = max(0.1, min(5.0, float(params.get("gamma", 1.0))))
        prog["u_invert"].value = bool(params.get("invert", False))
        prog["u_channels"].value = 1 if channels == "all" else 0
        self._render_quad(prog)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }
