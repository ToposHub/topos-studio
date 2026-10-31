"""
BlurEvaluatorMixin - Blur, Defocus, and Sharpen effect implementations.

Provides GPU-based blur, defocus (bokeh), and sharpening effects:
- Separable Box/Gaussian blur (fragment shader for small radius, compute shader for large)
- Bilateral edge-preserving blur
- Motion blur (directional)
- Bokeh (lens defocus) blur with aperture/anamorphic/highlight controls
- Defocus/bokeh blur with hexagon/circle iris shapes (legacy defocus node)
- Sharpening filter

All effects support HDR workflows (no clamping unless explicitly requested).
"""

import logging

logger = logging.getLogger(__name__)

from ..nodes.base_node import TextureResult



# Edge policy name -> shader int code (shared by all blur paths).
_EDGE_POLICY_MAP = {"clamp": 0, "wrap": 1, "mirror": 2}

# Quality name -> sample count for single-pass shaders.
_QUALITY_SAMPLES = {"fast": 8, "normal": 16, "high": 32}


class BlurEvaluatorMixin:
    """Mixin providing blur, defocus, and sharpen effect implementations."""

    # ───────────────────────────────────────────────────────────────────────
    # Blur dispatch
    # ───────────────────────────────────────────────────────────────────────
    def _apply_blur(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Dispatch blur by ``blur_type``.

        Supported types: box, gaussian, bilateral, motion, bokeh.
        box/gaussian use the separable two-pass path (with compute-shader
        fallback for large radii). The others are single-pass fragment shaders.
        ``directional`` is accepted for backward compatibility and routed to
        the motion path.
        """
        blur_type = params.get("blur_type", "box")
        # Backward compat: legacy "directional" type maps to "motion".
        if blur_type == "directional":
            blur_type = "motion"

        if blur_type in ("box", "gaussian"):
            return self._blur_separable(input_tex, params, w, h, blur_type)
        elif blur_type == "bilateral":
            return self._blur_single_pass(input_tex, params, w, h, "blur_bilateral")
        elif blur_type == "motion":
            return self._blur_single_pass(input_tex, params, w, h, "blur_motion")
        elif blur_type == "bokeh":
            return self._blur_single_pass(input_tex, params, w, h, "blur_bokeh")
        else:
            # Unknown type -> safe fallback to box.
            logger.warning(f"Unknown blur_type='{blur_type}', falling back to box")
            return self._blur_separable(input_tex, params, w, h, "box")

    # ───────────────────────────────────────────────────────────────────────
    # Shared helpers
    # ───────────────────────────────────────────────────────────────────────
    @staticmethod
    def _channel_mask_from_params(params: dict):
        """Build a (r, g, b, a) channel-enable tuple from params.

        Falls back to process_alpha semantics when channel_* keys are absent
        (legacy nodes): RGB always on, alpha on iff process_alpha is True.
        """
        if "channel_r" in params or "channel_g" in params \
                or "channel_b" in params or "channel_a" in params:
            r = 1.0 if params.get("channel_r", True) else 0.0
            g = 1.0 if params.get("channel_g", True) else 0.0
            b = 1.0 if params.get("channel_b", True) else 0.0
            a = 1.0 if params.get("channel_a", False) else 0.0
            return (r, g, b, a)
        process_alpha = params.get("process_alpha", True)
        return (1.0, 1.0, 1.0, 1.0 if process_alpha else 0.0)

    def _make_blur_result(self, out_tex, w: int, h: int,
                          input_tex: TextureResult) -> dict:
        """Wrap a rendered texture in a TextureResult using the fixed-canvas
        bbox policy (blur renders into a texture of the input size, so the
        bbox stays aligned with the input bbox)."""
        in_bbox = input_tex.effective_bbox()
        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=in_bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _acquire_fbo(self, w: int, h: int):
        """Acquire an output texture + FBO, bind it, set viewport and clear."""
        out_tex = self._resource_pool.acquire_texture(w, h)
        fbo = self.fb_manager.get_fbo(out_tex)
        fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        return out_tex

    # ───────────────────────────────────────────────────────────────────────
    # Separable path: box / gaussian
    # ───────────────────────────────────────────────────────────────────────
    def _blur_separable(self, input_tex: TextureResult, params: dict,
                        w: int, h: int, blur_type: str) -> dict:
        """Two-pass separable box/gaussian blur.

        Large radii use the compute shader (shared-memory gaussian); small
        radii use the separable fragment shaders. Channel mask is applied on
        the V (final) pass.
        """
        radius_x = params.get("radius_x", params.get("radius", 5.0))
        radius_y = params.get("radius_y", params.get("radius", 5.0))
        edge_policy = params.get("edge_policy", "clamp")
        edge_int = _EDGE_POLICY_MAP.get(edge_policy, 0)
        channel_mask = self._channel_mask_from_params(params)
        mix = params.get("mix", 1.0)
        kernel_mode = 1 if blur_type == "gaussian" else 0

        radius = max(radius_x, radius_y)
        if radius < 0.1:
            return {"result": input_tex}

        # ── Compute Shader Path (Large Radius) ──
        # Only box/gaussian support the compute path; it applies a gaussian
        # approximation. Channel mask is not applied here (compute path is a
        # fast approximation), but mix/alpha semantics are preserved via the
        # TextureResult passthrough.
        compute_mgr = getattr(self, '_compute_shader_manager', None)
        use_compute = compute_mgr and compute_mgr.should_use_compute_blur(radius)
        # Only use compute when channels are fully enabled and mix == 1, so the
        # fast path matches the per-channel semantics of the fragment path.
        if use_compute and all(c > 0.5 for c in channel_mask) and abs(mix - 1.0) < 0.001:
            try:
                input_tex_obj = self._get_texture_by_id(input_tex.texture_id)
                if input_tex_obj is None:
                    logger.warning(f"Blur radius={radius:.1f}: texture_id={input_tex.texture_id} not found, fallback to fragment")
                else:
                    out_tex_obj = compute_mgr.apply_blur_compute(
                        input_tex_obj, radius, w, h,
                        self._resource_pool, edge_int
                    )
                    if out_tex_obj:
                        if w >= 3840 or h >= 2160:
                            logger.debug(f"Blur compute shader: radius={radius:.1f}, size={w}x{h}")
                        return self._make_blur_result(out_tex_obj, w, h, input_tex)
                    else:
                        logger.warning("Blur compute shader returned None, fallback to fragment")
            except Exception as e:
                logger.warning(f"Blur compute shader failed (radius={radius:.1f}): {e}")

        # ── Fragment Shader Path (separable H+V) ──
        prog_h = self.utility_shaders.get('blur_separable_h')
        prog_v = self.utility_shaders.get('blur_separable_v')
        if prog_h is None or prog_v is None:
            # Fallback to legacy single-type shaders if new ones unavailable.
            prog_h = self.utility_shaders.get('blur_h')
            prog_v = self.utility_shaders.get('blur_v')
        if prog_h is None or prog_v is None:
            return {"result": input_tex}

        # Horizontal pass -> temp texture (no channel mask here).
        temp_tex = self._resource_pool.acquire_texture(w, h)
        out_tex = None
        try:
            temp_fbo = self.fb_manager.get_fbo(temp_tex)
            temp_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            self._bind_external_texture(input_tex.texture_id)
            self._set_uniform_if_present(prog_h, 'u_texture', 0)
            self._set_uniform_if_present(prog_h, 'u_resolution', (max(1, w), max(1, h)))
            self._set_uniform_if_present(prog_h, 'u_radius', radius_x)
            self._set_uniform_if_present(prog_h, 'u_kernel_mode', kernel_mode)
            self._set_uniform_if_present(prog_h, 'u_edge_policy', edge_int)
            self._render_quad(prog_h)

            # Vertical pass -> output (applies channel mask + mix + edge policy).
            out_tex = self._acquire_fbo(w, h)
            temp_tex.use(0)
            # Mask/mix reference is the pristine input on unit 1 — NOT the
            # H-pass output (that leaks one horizontal blur into masked-off
            # channels and the mix reference; the V shader samples u_original).
            self._bind_texture_unit(input_tex.texture_id, 1)
            self._set_uniform_if_present(prog_v, 'u_texture', 0)
            self._set_uniform_if_present(prog_v, 'u_original', 1)
            self._set_uniform_if_present(prog_v, 'u_resolution', (max(1, w), max(1, h)))
            self._set_uniform_if_present(prog_v, 'u_radius', radius_y)
            self._set_uniform_if_present(prog_v, 'u_kernel_mode', kernel_mode)
            self._set_uniform_if_present(prog_v, 'u_channel_mask', channel_mask)
            self._set_uniform_if_present(prog_v, 'u_mix', mix)
            self._set_uniform_if_present(prog_v, 'u_edge_policy', edge_int)
            self._render_quad(prog_v)
        except Exception:
            # AUD-5910: 求值中途抛错时归还已获取的池纹理（temp/out 两处
            # acquire 与返回之间此前无异常保护，出错帧永久泄漏整幅纹理）。
            self._release_effect_textures_on_error(temp_tex, out_tex)
            raise

        self._resource_pool.release_texture(temp_tex)
        return self._make_blur_result(out_tex, w, h, input_tex)

    # ───────────────────────────────────────────────────────────────────────
    # Single-pass path: bilateral / motion / bokeh
    # ───────────────────────────────────────────────────────────────────────
    def _blur_single_pass(self, input_tex: TextureResult, params: dict,
                          w: int, h: int, shader_name: str) -> dict:
        """Run a single-pass blur shader (bilateral / motion / bokeh).

        Each shader applies its own channel mask + mix + edge policy
        internally on the final result.
        """
        prog = self.utility_shaders.get(shader_name)
        if prog is None:
            logger.warning(f"Blur shader '{shader_name}' not available, passthrough")
            return {"result": input_tex}

        radius = params.get("radius", params.get("radius_x", 5.0))
        edge_policy = params.get("edge_policy", "clamp")
        edge_int = _EDGE_POLICY_MAP.get(edge_policy, 0)
        quality = params.get("quality", "normal")
        sample_count = int(params.get(
            "sample_count", _QUALITY_SAMPLES.get(quality, 16)))
        sample_count = max(4, min(32, sample_count))
        channel_mask = self._channel_mask_from_params(params)
        mix = params.get("mix", 1.0)

        if radius < 0.1:
            return {"result": input_tex}

        out_tex = self._acquire_fbo(w, h)
        try:
            self._bind_external_texture(input_tex.texture_id)
            self._set_uniform_if_present(prog, 'u_texture', 0)
            self._set_uniform_if_present(prog, 'u_resolution', (max(1, w), max(1, h)))
            self._set_uniform_if_present(prog, 'u_radius', radius)
            self._set_uniform_if_present(prog, 'u_sample_count', sample_count)
            self._set_uniform_if_present(prog, 'u_channel_mask', channel_mask)
            self._set_uniform_if_present(prog, 'u_mix', mix)
            self._set_uniform_if_present(prog, 'u_edge_policy', edge_int)

            # Type-specific uniforms.
            if shader_name == "blur_bilateral":
                self._set_uniform_if_present(prog, 'u_edge_preserve',
                                             params.get("edge_preserve", 0.5))
            elif shader_name == "blur_motion":
                self._set_uniform_if_present(prog, 'u_angle',
                                             params.get("angle", 0.0))
                velocity_tex = params.get("_velocity_tex")
                use_velocity = (
                    params.get("motion_source", "angle") == "velocity"
                    and isinstance(velocity_tex, TextureResult)
                    and velocity_tex.is_valid
                )
                if use_velocity:
                    self._bind_texture_unit(velocity_tex.texture_id, 1)
                self._set_uniform_if_present(prog, 'u_velocity', 1)
                self._set_uniform_if_present(prog, 'u_use_velocity', use_velocity)
                shutter = max(0.0, min(2.0, float(params.get("shutter", 1.0))))
                self._set_uniform_if_present(prog, 'u_shutter', shutter)
            elif shader_name == "blur_bokeh":
                # aperture_blades is stored as a string label ("circle","5","6","8").
                blades = params.get("aperture_blades", "circle")
                blades_int = 0
                if isinstance(blades, (int, float)):
                    blades_int = int(blades)
                elif isinstance(blades, str) and blades.isdigit():
                    blades_int = int(blades)
                # "circle" / anything non-numeric -> 0 (circular aperture)
                self._set_uniform_if_present(prog, 'u_aperture_blades', blades_int)
                self._set_uniform_if_present(prog, 'u_anamorphic',
                                             params.get("anamorphic", 0.0))
                self._set_uniform_if_present(prog, 'u_highlight_gain',
                                             params.get("highlight_gain", 0.0))

            try:
                self._render_quad(prog)
            finally:
                if shader_name == "blur_motion":
                    self._unbind_texture_unit(1)
        except Exception:
            # AUD-5910: 出错帧归还已获取的输出纹理（同 _blur_separable）。
            self._release_effect_textures_on_error(out_tex)
            raise
        return self._make_blur_result(out_tex, w, h, input_tex)

    # Defocus mode label -> shader int code (matches _DEFOCUS_FRAGMENT's u_mode).
    _DEFOCUS_MODE_MAP = {"separable": 0, "mipmap": 1}

    @staticmethod
    def _defocus_params_to_uniforms(params: dict) -> dict:
        """Map DefocusNode PARAM_DEFS values to the _DEFOCUS_FRAGMENT uniforms.

        The node schema (``DefocusNode.PARAM_DEFS``) and the shader
        (``_DEFOCUS_FRAGMENT``) agree on these names: ``mode / focal_x /
        focal_y / focal_size / max_radius / edge_policy / process_alpha``.
        We merge focal_x+focal_y into the shader's ``vec2 u_focal_point``
        and translate the two ``option`` params (mode, edge_policy) into
        the int codes the shader expects.

        Returns a dict of ``{uniform_name: value}`` ready to upload. This
        is split out from :meth:`_apply_defocus` so it can be unit-tested
        without a GL context.

        Previously this evaluator read ``iris_size / iris_type / gain``
        (a legacy bokeh API) which PARAM_DEFS never declares — every
        value fell back to the default and the node's real parameters
        were silently dropped, so defocus was a no-op.
        """
        mode = params.get("mode", "separable")
        edge_policy = params.get("edge_policy", "clamp")
        return {
            "u_focal_point": (
                float(params.get("focal_x", 0.5)),
                float(params.get("focal_y", 0.5)),
            ),
            "u_focal_size": float(params.get("focal_size", 0.3)),
            "u_max_radius": float(params.get("max_radius", 20.0)),
            "u_mode": BlurEvaluatorMixin._DEFOCUS_MODE_MAP.get(mode, 0),
            "u_edge_policy": _EDGE_POLICY_MAP.get(edge_policy, 0),
            "u_process_alpha": bool(params.get("process_alpha", True)),
            # PARAM_DEFS has no quality knob for defocus; pick a fixed
            # sample count that gives acceptable quality at preview size.
            "u_sample_count": 16,
        }

    def _apply_vectorblur(self, input_tex: TextureResult, params: dict,
                          w: int, h: int) -> dict:
        """CP-020: motion-vector blur — the blur-motion path, velocity-driven.

        Reuses the ``blur_motion`` shader with ``motion_source=velocity``;
        the velocity texture carries signed normalized UV displacement per
        frame (Scene3D MOTION_VECTOR AOV convention). ``shutter`` scales
        the sampled interval (CP-021 project shutter resolved at the node).
        """
        # 无有效 velocity 输入时必须直通：blur_motion shader 在
        # u_use_velocity=0 时回落「角度×半径」路径（radius 默认 5px），
        # 会把缺 velocity 的 VectorBlur 变成意外的水平模糊。
        velocity_tex = params.get("_velocity_tex")
        if not (isinstance(velocity_tex, TextureResult)
                and velocity_tex.is_valid):
            return {"result": input_tex}
        motion_params = dict(params)
        motion_params["blur_type"] = "motion"
        motion_params["motion_source"] = "velocity"
        motion_params.setdefault("shutter", 1.0)
        return self._blur_single_pass(input_tex, motion_params, w, h,
                                      "blur_motion")

    def _apply_defocus(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply defocus (depth-of-field) blur using the focal-point model.

        Feeds every uniform ``_DEFOCUS_FRAGMENT`` declares so the node's
        PARAM_DEFS (focal_x/y, focal_size, max_radius, mode, edge_policy,
        process_alpha) actually drive the shader — the previous
        implementation read a different (iris_size/iris_type/gain) schema
        that PARAM_DEFS never populates, leaving defocus as a no-op.
        """
        uniforms = self._defocus_params_to_uniforms(params)

        # Skip when the shader would passthrough anyway.
        if uniforms["u_max_radius"] < 0.1:
            return {"result": input_tex}

        prog = self.utility_shaders.get('defocus')
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
            prog['u_resolution'].value = (max(1, w), max(1, h))
            for name, value in uniforms.items():
                prog[name].value = value

            self._render_quad(prog)
        except Exception:
            # AUD-5910: 出错帧归还已获取的输出纹理。
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

    def _apply_sharpen(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply sharpening filter using unsharp-mask.

        Feeds every uniform the sharpen shader declares (u_amount, u_radius,
        u_threshold, u_channels) so the node's PARAM_DEFS actually take
        effect. Previously only u_amount was set, so radius/threshold/
        channels were silently ignored and sharpen behaved like a fixed
        1-pixel kernel regardless of the user-facing parameters.
        """
        amount = float(params.get("amount", 1.0))
        radius = max(0.0, float(params.get("radius", 1.0)))
        threshold = max(0.0, float(params.get("threshold", 0.0)))
        channels = params.get("channels", "all")
        # Match the shader's u_channels codes: 0=all, 1=rgb, 2=luminance.
        channels_map = {"all": 0, "rgb": 1, "luminance": 2}
        channels_int = channels_map.get(channels, 0)

        # Skip when there is no visible effect.
        if abs(amount) < 0.01 or radius < 0.1:
            return {"result": input_tex}

        prog = self.utility_shaders.get('sharpen')
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
            prog['u_amount'].value = amount
            prog['u_radius'].value = radius
            prog['u_threshold'].value = threshold
            prog['u_channels'].value = channels_int
            prog['u_resolution'].value = (float(max(1, w)), float(max(1, h)))

            self._render_quad(prog)
        except Exception:
            # AUD-5910: 出错帧归还已获取的输出纹理。
            self._release_effect_textures_on_error(out_tex)
            raise

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }
