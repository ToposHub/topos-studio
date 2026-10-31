"""
MergeEvaluatorMixin - Merge and blend effect implementations.

Provides GPU-based compositing effects:
- Blend (merge with multiple blend modes)
- Dissolve (linear interpolation between inputs)
- Deep Merge (depth-based compositing)
- Switch (input selection)
- Set Alpha (alpha channel replacement)

All effects support proper alpha handling (straight/premultiplied) and
format/bbox policies for output resolution determination.
"""


from ..nodes.base_node import TextureResult



class MergeEvaluatorMixin:
    """Mixin providing merge and blend effect implementations."""

    def _apply_dissolve(self, outputs: dict, w: int, h: int) -> dict:
        """Apply dissolve (linear interpolation) between two inputs.

        Output size/bbox follows the same canvas-aware resolution logic as
        Merge so that dissolve between a packed Transform output and a full
        frame input does not resize the canvas on every parameter tweak.
        """
        input_a = outputs.get("_input_a")
        input_b = outputs.get("_input_b")
        effect_params = outputs.get("_effect_params", {})
        mix_val = effect_params.get("mix", 0.5)
        format_policy = effect_params.get("format_policy", "larger")
        bbox_policy = effect_params.get("bbox_policy", "union")

        if input_a is None or not input_a.is_valid:
            return {"result": input_b} if input_b else {}
        if input_b is None or not input_b.is_valid:
            return {"result": input_a}

        # S2-D: Check color space mismatch and log warning
        self._check_color_space_mismatch(input_a, input_b, "dissolve")

        if mix_val <= 0.0:
            return {"result": input_b}
        if mix_val >= 1.0:
            return {"result": input_a}

        prog = self.utility_shaders.get('dissolve')
        if prog is None:
            return {"result": input_a}

        # Canvas-aware output resolution (matches Merge semantics).
        out_w, out_h = self._resolve_merge_dims(input_a, input_b, format_policy)

        out_tex = self._resource_pool.acquire_texture(out_w, out_h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, out_w, out_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_a.texture_id)
        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(input_b.texture_id, 1)

        prog['u_texture_a'].value = 0
        prog['u_texture_b'].value = 1
        prog['u_resolution_a'].value = (float(max(1, input_a.width or out_w)), float(max(1, input_a.height or out_h)))
        prog['u_resolution_b'].value = (float(max(1, input_b.width or out_w)), float(max(1, input_b.height or out_h)))
        prog['u_output_resolution'].value = (float(out_w), float(out_h))
        prog['u_mix'].value = mix_val
        self._render_quad(prog)

        bind_texture_unit(0, 1)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=out_w, height=out_h,
                format=self._resolve_merge_format(input_a, input_b, format_policy),
                bbox=self._resolve_merge_bbox(input_a, input_b, bbox_policy),
                alpha_premultiplied=input_a.alpha_premultiplied and input_b.alpha_premultiplied,
                color_space=input_b.color_space or input_a.color_space,
            )
        }

    def _apply_keymix(self, outputs: dict) -> dict:
        """CP-011: mask-driven A/B blend — out = B + (A - B) * m.

        Three inputs (A fg, B bg, mask). Missing mask behaves as a fully
        open gate (t = mix); the shader lerps premultiplied RGB and alpha
        identically. Output canvas follows the LARGER input (both inputs
        are sampled by normalized UV; like the simple merge family,
        differently-formatted inputs stretch to the shared canvas).
        """
        input_a = outputs.get("_effect_input_a")
        input_b = outputs.get("_effect_input_b")
        input_mask = outputs.get("_effect_mask")
        effect_params = outputs.get("_effect_params", {})

        if not input_a or not input_b:
            result = outputs.get("result")
            return {"result": result} if result and isinstance(result, TextureResult) else {}

        w, h = self._resolve_merge_dims(input_a, input_b, "larger")

        prog = self.utility_shaders.get('keymix')
        if prog is None:
            return {"result": input_a}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        from ..core.gl_bind import bind_texture_unit

        self._bind_external_texture(input_a.texture_id)
        prog['u_a'].value = 0
        bind_texture_unit(input_b.texture_id, 1)
        prog['u_b'].value = 1
        mask_valid = (input_mask is not None and input_mask.is_valid
                      and isinstance(input_mask, TextureResult))
        if mask_valid:
            bind_texture_unit(input_mask.texture_id, 2)
            prog['u_m'].value = 2
            channel = str(effect_params.get("mask_channel", "luma"))
            prog['u_mask_channel'].value = {
                "luma": 0, "r": 1, "g": 2, "b": 3, "a": 4}.get(channel, 0)
            prog['u_mask_invert'].value = 1.0 if effect_params.get(
                "mask_invert", False) else 0.0
            prog['u_mask_valid'].value = 1.0
        else:
            prog['u_mask_valid'].value = 0.0
        try:
            prog['u_mix'].value = float(effect_params.get("mix", 1.0))
        except (TypeError, ValueError):
            prog['u_mix'].value = 1.0

        self._render_quad(prog)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                alpha_premultiplied=True, color_space="linear"),
        }

    def _apply_deep_merge(self, outputs: dict) -> dict:
        """Apply deep merge operation with depth-based compositing.

        Deep compositing uses per-pixel Z values to determine proper occlusion.
        Outputs both RGBA and depth channels.
        """
        input_a = outputs.get("_effect_input_a")
        input_b = outputs.get("_effect_input_b")
        input_mask = outputs.get("_effect_mask")
        effect_params = outputs.get("_effect_params", {})

        if not input_a or not input_b:
            result = outputs.get("result")
            return {"result": result} if result and isinstance(result, TextureResult) else {}

        # S2-D: Check color space mismatch and log warning
        self._check_color_space_mismatch(input_a, input_b, "deep_merge")

        depth_mode_str = effect_params.get("depth_mode", "z_front")
        depth_tolerance = effect_params.get("depth_tolerance", 0.01)
        opacity = effect_params.get("opacity", 1.0)
        format_policy = effect_params.get("format_policy", "from_b")
        bbox_policy = effect_params.get("bbox_policy", "union")
        has_depth_a = effect_params.get("has_depth_a", False)
        has_depth_b = effect_params.get("has_depth_b", False)

        depth_mode_idx = 0
        if depth_mode_str == "z_front":
            depth_mode_idx = 0
        elif depth_mode_str == "z_back":
            depth_mode_idx = 1
        elif depth_mode_str == "z_merge":
            depth_mode_idx = 2

        w, h = self._resolve_merge_dims(input_a, input_b, format_policy)

        prog = self.utility_shaders.get('deep_merge')
        if prog is None:
            # AUD-6106: passthrough must carry input_a's own metadata.  The
            # previous fallback built a fresh TextureResult whose width/height
            # came from _resolve_merge_dims (with the default "from_b" policy
            # these are input_b's canvas) while texture_id stayed input_a's —
            # for differently-sized inputs the declared data window no longer
            # matched the GL texture and downstream consumers sampled with a
            # wrong UV scale.  Returning input_a verbatim (same shape as the
            # dissolve fallback above) keeps texture and metadata consistent.
            return {"result": input_a}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        from ..core.gl_bind import bind_texture_unit

        self._bind_external_texture(input_a.texture_id)

        bind_texture_unit(input_b.texture_id, 1)

        if has_depth_a and input_a.depth_texture_id:
            bind_texture_unit(input_a.depth_texture_id, 2)

        if has_depth_b and input_b.depth_texture_id:
            bind_texture_unit(input_b.depth_texture_id, 3)

        has_mask = input_mask is not None and input_mask.is_valid
        if has_mask:
            bind_texture_unit(input_mask.texture_id, 4)

        prog['u_texture_a'].value = 0
        prog['u_texture_b'].value = 1
        prog['u_depth_a'].value = 2
        prog['u_depth_b'].value = 3
        prog['u_resolution_a'].value = (float(max(1, input_a.width or w)), float(max(1, input_a.height or h)))
        prog['u_resolution_b'].value = (float(max(1, input_b.width or w)), float(max(1, input_b.height or h)))
        prog['u_output_resolution'].value = (float(w), float(h))
        prog['u_opacity'].value = opacity
        prog['u_has_depth_a'].value = has_depth_a
        prog['u_has_depth_b'].value = has_depth_b
        prog['u_depth_mode'].value = depth_mode_idx
        prog['u_depth_tolerance'].value = depth_tolerance

        # S2-B: Alpha policy handling for deep merge
        alpha_policy = effect_params.get("alpha_policy", "auto")
        if alpha_policy == "premult_force":
            prog['u_a_straight'].value = False
            prog['u_b_straight'].value = False
        elif alpha_policy == "straight_force":
            prog['u_a_straight'].value = True
            prog['u_b_straight'].value = True
        else:  # "auto" - read from metadata
            prog['u_a_straight'].value = input_a.alpha_premultiplied is False
            prog['u_b_straight'].value = input_b.alpha_premultiplied is False
        # For deep merge, we don't support output_unpremult (deep data needs premult)

        prog['u_has_mask'].value = has_mask
        if has_mask:
            prog['u_texture_mask'].value = 4

        self._render_quad(prog)

        # Cleanup
        if has_mask:
            bind_texture_unit(0, 4)
        if has_depth_b and input_b.depth_texture_id:
            bind_texture_unit(0, 3)
        if has_depth_a and input_a.depth_texture_id:
            bind_texture_unit(0, 2)
        bind_texture_unit(0, 1)

        # Allocate depth output
        out_depth_tex = None
        if has_depth_a or has_depth_b:
            out_depth_tex = self._resource_pool.acquire_depth_texture(w, h)
            depth_fbo = self.fb_manager.get_fbo(out_depth_tex)
            depth_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            # AUD-6105: moderngl's clear() needs a 4-component color, a
            # scalar raises TypeError. The R32F depth texture is attached
            # as a color attachment (FramebufferManager.get_fbo), so clear
            # red=1.0 to initialize every pixel to far depth — matching
            # the shader's out-of-bounds convention (1.0 == far).
            self.ctx.clear(1.0, 1.0, 1.0, 1.0)

            depth_prog = self.utility_shaders.get('depth_output')
            if depth_prog:
                if has_depth_a and input_a.depth_texture_id:
                    bind_texture_unit(input_a.depth_texture_id, 0)
                if has_depth_b and input_b.depth_texture_id:
                    bind_texture_unit(input_b.depth_texture_id, 1)

                depth_prog['u_depth_a'].value = 0
                depth_prog['u_depth_b'].value = 1
                depth_prog['u_resolution_a'].value = (float(max(1, input_a.width or w)), float(max(1, input_a.height or h)))
                depth_prog['u_resolution_b'].value = (float(max(1, input_b.width or w)), float(max(1, input_b.height or h)))
                depth_prog['u_output_resolution'].value = (float(w), float(h))
                depth_prog['u_has_depth_a'].value = has_depth_a
                depth_prog['u_has_depth_b'].value = has_depth_b
                depth_prog['u_depth_mode'].value = depth_mode_idx
                depth_prog['u_depth_tolerance'].value = depth_tolerance

                self._render_quad(depth_prog)

                if has_depth_b and input_b.depth_texture_id:
                    bind_texture_unit(0, 1)
                bind_texture_unit(0, 0)

        result_fmt = self._resolve_merge_format(input_a, input_b, format_policy)
        result_bbox = self._resolve_merge_bbox(input_a, input_b, bbox_policy)

        # 恢复 FBO 状态：如果绑定了深度 FBO，恢复到主输出 FBO
        # 这确保后续的视口 readback 读取正确的帧缓冲区
        if out_depth_tex is not None:
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo,
                width=w,
                height=h,
                format=result_fmt,
                bbox=result_bbox,
                alpha_premultiplied=True,
                color_space=input_b.color_space or input_a.color_space,
                depth_texture_id=out_depth_tex.glo if out_depth_tex else None,
                has_deep_data=(has_depth_a or has_depth_b),
            )
        }

    def _apply_set_alpha(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply set_alpha: replace alpha channel based on mode.

        The new alpha is derived from a constant, the input's luminance, a
        specific input channel, or an optional mask texture. Output is marked
        straight (alpha_premultiplied=False) because RGB no longer corresponds
        to the new alpha — callers should add a Premult node if needed.
        """
        prog = self.utility_shaders.get('set_alpha')
        if prog is None:
            return {"rgba": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0

        mode = params.get("mode", "constant")
        value = float(params.get("value", 1.0))
        mask_tex = params.get("_mask_tex")

        mode_map = {"constant": 0, "luma": 1, "r": 2, "g": 3, "b": 4, "a": 5}
        mode_int = mode_map.get(mode, 0)

        prog['u_mode'].value = mode_int
        prog['u_value'].value = value

        has_mask = mask_tex is not None and mask_tex.is_valid
        prog['u_has_mask'].value = has_mask
        if has_mask:
            from ..core.gl_bind import bind_texture_unit
            bind_texture_unit(mask_tex.texture_id, 1)
            prog['u_mask_texture'].value = 1

        self._render_quad(prog)

        if has_mask:
            from ..core.gl_bind import bind_texture_unit
            bind_texture_unit(0, 1)

        return {
            "rgba": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=False,
                color_space=input_tex.color_space,
            )
        }