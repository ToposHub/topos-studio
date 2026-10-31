"""
FilterEvaluatorMixin - Filter and generator effect implementations.

Provides GPU-based effects:
- Erode/Dilate (morphological operations)
- Glow (separable blur with threshold)
- Edge Detect (Sobel, Laplacian, Prewitt, Canny)
- Noise (procedural noise generator)
- Ramp (gradient generator)
- Mask (geometric mask generator)
- Denoise (bilateral filter)
- LUT 3D (color transformation via 3D lookup table)

All effects are optimized for GPU execution with compute shader fallbacks
for large-radius operations.
"""

import moderngl
import logging

import numpy as np
import math
import struct
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# AUD-6002: bounds for the 3D LUT texture LRU (see _get_or_upload_lut).
# A 64^3 f4 LUT is ~3 MB; 8 entries / 64 MB comfortably covers stacking
# several looks while keeping the per-renderer VRAM footprint bounded.
_LUT_CACHE_MAX_ENTRIES = 8
_LUT_CACHE_MAX_BYTES = 64 * 1024 * 1024

from ..nodes.base_node import TextureResult
from src.features.composite.core.node_engine import NODE_REGISTRY


class FilterEvaluatorMixin:
    """Mixin providing filter and generator effect implementations."""

    def _apply_erode_dilate(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply morphological erosion or dilation.

        Automatically chooses between fragment shader and compute shader:
        - size <= 16: fragment shader
        - size > 16: compute shader (shared memory optimization)

        Honors the node's ``edge_mode`` parameter (extend/reflect/wrap) so the
        morphology border handling follows the user setting instead of being
        hardwired to "extend" (clamp).
        """
        operation = params.get("operation", "erode")
        size = params.get("size", 3)
        iterations = params.get("iterations", 1)
        channels_mode = params.get("channels", "alpha")
        edge_mode = params.get("edge_mode", "extend")

        channels_int = 0 if channels_mode == "alpha" else 1
        # Match the shader's u_edge_mode codes: 0=extend(clamp), 1=reflect, 2=wrap.
        edge_map = {"extend": 0, "reflect": 1, "wrap": 2}
        edge_int = edge_map.get(edge_mode, 0)

        if size < 1 or iterations < 1:
            return {"result": input_tex}

        # Compute Shader Path (Large Size)
        compute_mgr = getattr(self, '_compute_shader_manager', None)
        use_compute = compute_mgr and compute_mgr.should_use_compute_erode_dilate(size)
        if use_compute:
            try:
                input_tex_obj = self._get_texture_by_id(input_tex.texture_id)
                if input_tex_obj is None:
                    logger.warning(f"ErodeDilate size={size}: texture_id={input_tex.texture_id} not found, fallback to fragment")
                else:
                    current_tex_obj = input_tex_obj
                    success = True
                    for iter_idx in range(iterations):
                        out_tex_obj = compute_mgr.apply_erode_dilate_compute(
                            current_tex_obj, operation, size, w, h,
                            self._resource_pool, channels_int
                        )
                        if out_tex_obj is None:
                            logger.warning(f"ErodeDilate compute shader failed at iteration {iter_idx}, fallback to fragment")
                            success = False
                            break
                        if current_tex_obj is not input_tex_obj:
                            self._resource_pool.release_texture(current_tex_obj)
                        current_tex_obj = out_tex_obj

                    if success and current_tex_obj:
                        if w >= 3840 or h >= 2160:
                            logger.debug(f"ErodeDilate compute shader: op={operation}, size={size}, iterations={iterations}")

                        return {
                            "result": TextureResult(
                                texture_id=current_tex_obj.glo,
                                width=w, height=h,
                                format=input_tex.format,
                                bbox=input_tex.bbox,
                                alpha_premultiplied=input_tex.alpha_premultiplied,
                                color_space=input_tex.color_space,
                            )
                        }
                    else:
                        if current_tex_obj is not input_tex_obj:
                            self._resource_pool.release_texture(current_tex_obj)
            except Exception as e:
                logger.warning(f"ErodeDilate compute shader failed (size={size}): {e}")

        # Fragment Shader Path
        prog = self.utility_shaders.get('erode_dilate')
        if prog is None:
            return {"result": input_tex}

        current_tex = input_tex
        is_first = True

        for _ in range(iterations):
            out_tex = self._resource_pool.acquire_texture(w, h)
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            if is_first:
                self._bind_external_texture(input_tex.texture_id)
                is_first = False
            else:
                self._bind_external_texture(current_tex.texture_id)
                if current_tex is not input_tex:
                    # AUD-6001: GPUResourcePool has no _texture_by_id dict;
                    # that attribute access raised AttributeError whenever
                    # iterations >= 2 took the fragment path. Release through
                    # the public by-id API (same as base_evaluator and the
                    # compute path contract).
                    self._resource_pool.release_texture_by_id(
                        current_tex.texture_id)

            prog['u_texture'].value = 0
            prog['u_operation'].value = 0 if operation == "erode" else 1
            prog['u_size'].value = size
            prog['u_channels'].value = channels_int
            # Pass edge_mode so the shader honors extend/reflect/wrap borders.
            self._set_uniform_if_present(prog, 'u_edge_mode', edge_int)
            prog['u_resolution'].value = (max(1, w), max(1, h))

            self._render_quad(prog)

            current_tex = TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )

        return {"result": current_tex}

    def _apply_glow(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply glow/bloom effect using a two-pass separable Gaussian.

        Pipeline (mirrors the shader contract):
          1. Threshold bright pixels + horizontal blur → temp texture.
          2. Vertical blur of temp + tint + intensity + screen-blend mix → output.

        Params consumed (from GlowNode._do_evaluate):
          - threshold, radius, intensity
          - tint_r, tint_g, tint_b  (rgb tint multiplier)
          - mix                     (0 = original only, 1 = full bloom added)
          - sample_count            (quality-driven kernel size)
        """
        threshold = float(params.get("threshold", 0.7))
        radius = float(params.get("radius", 15.0))
        intensity = float(params.get("intensity", 1.5))
        tint = (
            float(params.get("tint_r", 1.0)),
            float(params.get("tint_g", 0.85)),
            float(params.get("tint_b", 0.7)),
        )
        mix = float(params.get("mix", 1.0))
        sample_count = int(params.get("sample_count", 16))

        # Skip entirely when there's no visible effect.
        if radius < 0.5 or intensity < 0.001 or mix < 0.001:
            return {"result": input_tex}

        prog_h = self.utility_shaders.get('glow_h')
        prog_v = self.utility_shaders.get('glow_v')
        if prog_h is None or prog_v is None:
            return {"result": input_tex}

        # ── Pass 1: horizontal threshold + blur → temp ──
        temp_tex = self._resource_pool.acquire_texture(w, h)
        temp_fbo = self.fb_manager.get_fbo(temp_tex)
        temp_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog_h['u_texture'].value = 0
        prog_h['u_resolution'].value = (float(max(1, w)), float(max(1, h)))
        prog_h['u_threshold'].value = threshold
        prog_h['u_radius'].value = radius
        prog_h['u_sample_count'].value = sample_count
        self._render_quad(prog_h)

        # ── Pass 2: vertical blur of temp + tint/intensity/mix → output ──
        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        # Pass-2 shader samples BOTH the temp (blurred bloom) and the
        # original input (for compositing). Bind input on unit 0 so the
        # shader's `u_texture` is the original; bind temp on unit 1.
        self._bind_external_texture(input_tex.texture_id)
        prog_v['u_texture'].value = 0

        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(temp_tex.glo, 1)
        prog_v['u_bloom_tex'].value = 1

        prog_v['u_resolution'].value = (float(max(1, w)), float(max(1, h)))
        prog_v['u_radius'].value = radius
        prog_v['u_intensity'].value = intensity
        prog_v['u_tint'].value = tint
        prog_v['u_mix'].value = mix
        prog_v['u_sample_count'].value = sample_count
        self._render_quad(prog_v)

        bind_texture_unit(0, 1)

        self._resource_pool.release_texture(temp_tex)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_lens_distortion(self, input_tex: TextureResult, params: dict,
                               w: int, h: int) -> dict:
        """A4: Brown-Conrady lens distortion (OpenCV convention)."""
        if not input_tex or not input_tex.is_valid:
            return {}

        prog = self.utility_shaders.get('lens_distortion')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0
        prog['u_mode'].value = 0 if params.get("mode", "undistort") == "undistort" else 1
        prog['u_k1'].value = float(params.get("k1", 0.0))
        prog['u_k2'].value = float(params.get("k2", 0.0))
        prog['u_k3'].value = float(params.get("k3", 0.0))
        prog['u_p1'].value = float(params.get("p1", 0.0))
        prog['u_p2'].value = float(params.get("p2", 0.0))
        prog['u_center'].value = (float(params.get("center_x", 0.5)),
                                  float(params.get("center_y", 0.5)))
        prog['u_resolution'].value = (float(max(1, w)), float(max(1, h)))
        edge = params.get("edge_mode", "black")
        prog['u_edge_mode'].value = {"black": 0, "clamp": 1, "mirror": 2}.get(edge, 0)

        self._render_quad(prog)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                color_space=input_tex.color_space,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                bit_depth=input_tex.bit_depth,
                revision=input_tex.revision,
            )
        }

    def _apply_edge_detect(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply edge detection effect."""
        if not input_tex or not input_tex.is_valid:
            return {}

        prog = self.utility_shaders.get('edge_detect')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0

        method = params.get("method", "sobel")
        methods = ["sobel", "laplacian", "prewitt", "canny"]
        method_idx = methods.index(method) if method in methods else 0
        prog['u_method'].value = method_idx

        prog['u_threshold'].value = params.get("threshold", 0.1)
        prog['u_threshold_high'].value = params.get("threshold_high", 0.3)

        direction = params.get("direction", "both")
        directions = ["both", "horizontal", "vertical"]
        direction_idx = directions.index(direction) if direction in directions else 0
        prog['u_direction'].value = direction_idx

        output_mode = params.get("output_mode", "edges")
        output_modes = ["edges", "gradient", "direction"]
        output_mode_idx = output_modes.index(output_mode) if output_mode in output_modes else 0
        prog['u_output_mode'].value = output_mode_idx

        prog['u_invert'].value = params.get("invert", False)
        prog['u_output_strength'].value = params.get("output_strength", 1.0)
        prog['u_kernel_size'].value = params.get("kernel_size", 3)
        prog['u_preblur'].value = params.get("preblur", 0.0)

        channel = params.get("channel", "luminance")
        channels = ["luminance", "r", "g", "b", "a"]
        channel_idx = channels.index(channel) if channel in channels else 0
        prog['u_channel'].value = channel_idx

        prog['u_resolution'].value = (float(max(1, w)), float(max(1, h)))

        self._render_quad(prog)

        # Edge detection produces a new matte-like result; preserve the input
        # format/bbox so downstream layout decisions (transforms, merges) stay
        # consistent with the source canvas.
        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                color_space=input_tex.color_space,
                alpha_premultiplied=input_tex.alpha_premultiplied,
            )
        }

    def _apply_noise(self, params: dict) -> dict:
        """Apply noise generator effect (no input required)."""
        w = max(1, int(params.get("width", 1920)))
        h = max(1, int(params.get("height", 1080)))

        prog = self.utility_shaders.get('noise')
        if prog is None:
            out_tex = self._resource_pool.acquire_texture(w, h)
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.5, 0.5, 0.5, 1.0)
            return {
                "result": TextureResult(
                    texture_id=out_tex.glo, width=w, height=h,
                    color_space="linear",
                )
            }

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        noise_type = params.get("noise_type", "perlin")
        noise_types = ["perlin", "simplex", "fractal", "turbulence", "wave"]
        noise_type_idx = noise_types.index(noise_type) if noise_type in noise_types else 0

        prog['u_noise_type'].value = noise_type_idx
        prog['u_scale'].value = params.get("scale", 100.0)
        prog['u_octaves'].value = params.get("octaves", 4)
        prog['u_persistence'].value = params.get("persistence", 0.5)
        prog['u_lacunarity'].value = params.get("lacunarity", 2.0)
        prog['u_seed'].value = params.get("seed", 0)

        output_channels = params.get("output_channels", "4")
        if output_channels == "1":
            prog['u_output_channels'].value = 1
        elif output_channels == "3":
            prog['u_output_channels'].value = 3
        else:
            prog['u_output_channels'].value = 4

        prog['u_animate'].value = params.get("animate", False)
        prog['u_animation_speed'].value = params.get("animation_speed", 1.0)
        prog['u_time'].value = params.get("time", 0.0)
        prog['u_offset_x'].value = params.get("offset_x", 0.0)
        prog['u_offset_y'].value = params.get("offset_y", 0.0)
        prog['u_resolution'].value = (float(max(1, w)), float(max(1, h)))

        self._render_quad(prog)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                color_space="linear",
                alpha_premultiplied=False,
            )
        }

    def _apply_ramp(self, params: dict) -> dict:
        """Apply ramp/gradient generator effect (no input required)."""
        w = max(1, int(params.get("width", 1920)))
        h = max(1, int(params.get("height", 1080)))

        prog = self.utility_shaders.get('ramp')
        if prog is None:
            out_tex = self._resource_pool.acquire_texture(w, h)
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            return {
                "result": TextureResult(
                    texture_id=out_tex.glo, width=w, height=h,
                    color_space="linear",
                )
            }

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        ramp_type = params.get("ramp_type", "linear")
        ramp_types = ["linear", "radial", "angular", "box", "diamond"]
        ramp_type_idx = ramp_types.index(ramp_type) if ramp_type in ramp_types else 0

        prog['u_ramp_type'].value = ramp_type_idx
        prog['u_start_point'].value = (params.get("start_x", 0.0), params.get("start_y", 0.0))
        prog['u_end_point'].value = (params.get("end_x", 1.0), params.get("end_y", 1.0))
        prog['u_start_color'].value = (
            params.get("start_r", 0.0),
            params.get("start_g", 0.0),
            params.get("start_b", 0.0),
            params.get("start_a", 1.0),
        )
        prog['u_end_color'].value = (
            params.get("end_r", 1.0),
            params.get("end_g", 1.0),
            params.get("end_b", 1.0),
            params.get("end_a", 1.0),
        )

        extend_mode = params.get("extend_mode", "clamp")
        extend_modes = ["clamp", "repeat", "mirror"]
        extend_idx = extend_modes.index(extend_mode) if extend_mode in extend_modes else 0
        prog['u_extend_mode'].value = extend_idx

        prog['u_center'].value = (params.get("center_x", 0.5), params.get("center_y", 0.5))
        prog['u_radius'].value = params.get("radius", 0.5)
        prog['u_angle_start'].value = params.get("angle_start", 0.0)
        prog['u_angle_end'].value = params.get("angle_end", 360.0)

        prog['u_corner0'].value = (
            params.get("corner0_r", 0.0),
            params.get("corner0_g", 0.0),
            params.get("corner0_b", 0.0),
            params.get("corner0_a", 1.0),
        )
        prog['u_corner1'].value = (
            params.get("corner1_r", 1.0),
            params.get("corner1_g", 0.0),
            params.get("corner1_b", 0.0),
            params.get("corner1_a", 1.0),
        )
        prog['u_corner2'].value = (
            params.get("corner2_r", 0.0),
            params.get("corner2_g", 1.0),
            params.get("corner2_b", 0.0),
            params.get("corner2_a", 1.0),
        )
        prog['u_corner3'].value = (
            params.get("corner3_r", 0.0),
            params.get("corner3_g", 0.0),
            params.get("corner3_b", 1.0),
            params.get("corner3_a", 1.0),
        )

        prog['u_resolution'].value = (float(max(1, w)), float(max(1, h)))

        self._render_quad(prog)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                color_space="linear",
                alpha_premultiplied=False,
            )
        }

    def _apply_mask(self, input_tex: TextureResult | None, params: dict) -> dict:
        """Render a geometric matte and optionally apply it to an image.

        The grayscale matte is always emitted on the ``matte`` output.  If ``input_tex``
        exists, ``result`` is a premultiplied image masked by that matte;
        otherwise ``result`` is a standalone visible shape texture.
        """
        w = max(1, int(params.get("output_width", 1920)))
        h = max(1, int(params.get("output_height", 1080)))

        prog = self.utility_shaders.get('mask')
        if prog is None:
            out_tex = self._resource_pool.acquire_texture(w, h)
            out_fbo = self.fb_manager.get_fbo(out_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            matte = TextureResult(
                    texture_id=out_tex.glo, width=w, height=h,
                    color_space="linear",
                    alpha_premultiplied=False,
                )
            return {"result": matte, "matte": matte}

        mask_type = params.get("mask_type", "rectangle")
        # Color-page masks call the equal-axis ellipse a "circle".  Accept
        # that persisted/UI alias in Composite without changing the shader
        # ABI or duplicating another SDF implementation.
        if mask_type == "circle":
            mask_type = "ellipse"
        mask_types = ["rectangle", "ellipse", "polygon"]
        mask_type_idx = mask_types.index(mask_type) if mask_type in mask_types else 0
        prog['u_mask_type'].value = mask_type_idx

        prog['u_center'].value = (params.get("center_x", 0.5), params.get("center_y", 0.5))
        prog['u_size'].value = (params.get("width", 0.5), params.get("height", 0.5))
        prog['u_rotation'].value = params.get("rotation", 0.0)
        prog['u_feather'].value = params.get("feather", 0.0)
        prog['u_invert'].value = params.get("invert", False)

        output_mode = params.get("output_mode", "result")
        output_modes = ["matte", "result"]
        output_mode_idx = output_modes.index(output_mode) if output_mode in output_modes else 0
        prog['u_sides'].value = params.get("sides", 5)

        def render_shape(target_tex, mode: int, source: TextureResult | None = None) -> None:
            out_fbo = self.fb_manager.get_fbo(target_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)
            has_source = source is not None and source.is_valid
            if has_source:
                self._bind_external_texture(source.texture_id)
                prog['u_texture'].value = 0
            prog['u_has_input'].value = has_source
            prog['u_output_mode'].value = mode
            self._render_quad(prog)

        # First render the canonical grayscale matte.  Keeping the shape in
        # RGB *and* alpha makes it work with Merge's default luma mask channel,
        # while remaining valid for consumers that read alpha.
        matte_tex = self._resource_pool.acquire_texture(w, h)
        render_shape(matte_tex, 1)
        matte = TextureResult(
            texture_id=matte_tex.glo, width=w, height=h,
            format=input_tex.format if input_tex else None,
            bbox=input_tex.bbox if input_tex else None,
            color_space="linear", alpha_premultiplied=False,
        )

        if input_tex is not None and input_tex.is_valid:
            result_tex = self._resource_pool.acquire_texture(w, h)
            render_shape(result_tex, 2, input_tex)
            result = TextureResult(
                texture_id=result_tex.glo, width=w, height=h,
                format=input_tex.format, bbox=input_tex.bbox,
                color_space=input_tex.color_space or "linear",
                alpha_premultiplied=True,
            )
            return {"result": result, "matte": matte}

        # Standalone shape: output a visible white matte by default, while
        # honoring the legacy output_mode parameter when explicitly set.
        result_tex = self._resource_pool.acquire_texture(w, h)
        render_shape(result_tex, 1 if output_mode_idx == 1 else 0)
        result = TextureResult(
            texture_id=result_tex.glo, width=w, height=h,
            color_space="linear", alpha_premultiplied=False,
        )
        return {"result": result, "matte": matte}

    @staticmethod
    def _flatten_roto_points_with_feather(
            params: dict) -> list[tuple[float, float, float]]:
        """Flatten a Roto path and carry per-point feather to the GPU.

        A missing point feather is represented by ``-1.0`` and resolved by
        the shader to the legacy global feather.  This keeps old JSON paths
        compatible while allowing explicit zero-width and animated point
        feather values.
        """
        shape_type = str(params.get("shape_type", "bezier"))
        cx = float(params.get("center_x", 0.5))
        cy = float(params.get("center_y", 0.5))
        width = float(params.get("width", 0.5))
        height = float(params.get("height", 0.5))
        rotation = math.radians(float(params.get("rotation", 0.0)))
        global_feather = max(0.0, min(
            0.5, float(params.get("feather", 0.0))))

        def transform(point: tuple[float, float]) -> tuple[float, float]:
            x, y = point
            dx = x - cx
            dy = y - cy
            c = math.cos(rotation)
            s = math.sin(rotation)
            return (cx + dx * c - dy * s, cy + dx * s + dy * c)

        def with_fallback(point: tuple[float, float]) -> tuple[float, float, float]:
            x, y = transform(point)
            return x, y, -1.0

        if shape_type == "ellipse":
            points: list[tuple[float, float, float]] = []
            for i in range(32):
                angle = math.tau * float(i) / 32.0
                points.append(with_fallback((
                    cx + math.cos(angle) * width * 0.5,
                    cy + math.sin(angle) * height * 0.5)))
            return points
        if shape_type == "polygon":
            points = []
            for i in range(6):
                angle = math.tau * float(i) / 6.0 - math.pi * 0.5
                points.append(with_fallback((
                    cx + math.cos(angle) * width * 0.5,
                    cy + math.sin(angle) * height * 0.5)))
            return points
        if shape_type == "rectangle":
            return [with_fallback(point) for point in (
                (cx - width * 0.5, cy - height * 0.5),
                (cx + width * 0.5, cy - height * 0.5),
                (cx + width * 0.5, cy + height * 0.5),
                (cx - width * 0.5, cy + height * 0.5),
            )]

        raw_points = params.get("points", [])
        if not isinstance(raw_points, list) or len(raw_points) < 3:
            return FilterEvaluatorMixin._flatten_roto_points_with_feather({
                **params, "shape_type": "rectangle"})

        def point_value(item: dict, key: str,
                       fallback: list[float]) -> tuple[float, float]:
            value = item.get(key, fallback) if isinstance(item, dict) else fallback
            try:
                return float(value[0]), float(value[1])
            except (TypeError, IndexError, ValueError):
                return float(fallback[0]), float(fallback[1])

        def point_feather(item: dict) -> float:
            if not isinstance(item, dict) or "feather" not in item:
                return -1.0
            try:
                return max(0.0, min(0.5, float(item["feather"])))
            except (TypeError, ValueError):
                return -1.0

        flattened: list[tuple[float, float, float]] = []
        for index, item in enumerate(raw_points):
            if not isinstance(item, dict):
                item = {"pos": item}
            next_item = raw_points[(index + 1) % len(raw_points)]
            if not isinstance(next_item, dict):
                next_item = {"pos": next_item}
            start = point_value(item, "pos", [cx, cy])
            c1 = point_value(item, "c2", list(start))
            end = point_value(next_item, "pos", [cx, cy])
            c2 = point_value(next_item, "c1", list(end))
            start_feather = point_feather(item)
            end_feather = point_feather(next_item)
            if not flattened:
                sx, sy = transform(start)
                flattened.append((sx, sy, start_feather))
            # Six samples per segment is enough for interactive preview while
            # the fixed 64-point shader bound keeps uniform uploads predictable.
            for sample in range(1, 7):
                t = float(sample) / 6.0
                omt = 1.0 - t
                p = (
                    omt ** 3 * start[0] + 3.0 * omt ** 2 * t * c1[0]
                    + 3.0 * omt * t ** 2 * c2[0] + t ** 3 * end[0],
                    omt ** 3 * start[1] + 3.0 * omt ** 2 * t * c1[1]
                    + 3.0 * omt * t ** 2 * c2[1] + t ** 3 * end[1],
                )
                feather = (start_feather if start_feather >= 0.0
                           else global_feather)
                end_width = (end_feather if end_feather >= 0.0
                             else global_feather)
                feather = feather + (end_width - feather) * t
                x, y = transform(p)
                flattened.append((x, y, feather if start_feather >= 0.0
                                  or end_feather >= 0.0 else -1.0))
                if len(flattened) >= 64:
                    return flattened[:64]
        return flattened[:64]

    @staticmethod
    def _flatten_roto_points(params: dict) -> list[tuple[float, float]]:
        """Flatten a Roto path to XY points for callers without feather data."""
        return [(x, y) for x, y, _ in
                FilterEvaluatorMixin._flatten_roto_points_with_feather(params)]

    def _apply_roto(self, input_tex: TextureResult | None, params: dict) -> dict:
        """Render a Roto Bezier matte and optionally apply it to an image."""
        w = max(1, int(params.get("output_width", 1920)))
        h = max(1, int(params.get("output_height", 1080)))
        prog = self.utility_shaders.get("roto")
        if prog is None:
            return {"result": input_tex} if input_tex else {}

        points_with_feather = self._flatten_roto_points_with_feather(params)
        points = [(x, y) for x, y, _ in points_with_feather]
        flat = [component for point in points for component in point]
        flat.extend([0.0] * max(0, 128 - len(flat)))
        feather_values = [feather for _, _, feather in points_with_feather]
        feather_values.extend([-1.0] * max(0, 64 - len(feather_values)))
        try:
            prog["u_points"].write(
                struct.pack(f"{len(flat)}f", *flat[:128]))
        except (KeyError, AttributeError, struct.error):
            try:
                prog["u_points"].value = tuple(flat[:128])
            except Exception:
                logger.exception("Unable to upload Roto point uniforms")
        try:
            prog["u_point_feathers"].write(
                struct.pack(f"{len(feather_values)}f", *feather_values[:64]))
        except (KeyError, AttributeError, struct.error):
            try:
                prog["u_point_feathers"].value = tuple(feather_values[:64])
            except Exception:
                logger.exception("Unable to upload Roto feather uniforms")
        prog["u_point_count"].value = min(len(points), 64)
        prog["u_feather"].value = max(0.0, min(
            0.5, float(params.get("feather", 0.0))))
        prog["u_opacity"].value = max(0.0, min(1.0, float(
            params.get("opacity", 1.0))))
        prog["u_invert"].value = bool(params.get("invert", False))

        def render_shape(target_tex, mode: int,
                         source: TextureResult | None = None) -> None:
            out_fbo = self.fb_manager.get_fbo(target_tex)
            out_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)
            has_source = source is not None and source.is_valid
            if has_source:
                self._bind_external_texture(source.texture_id)
                prog["u_texture"].value = 0
            prog["u_has_input"].value = has_source
            prog["u_output_mode"].value = mode
            self._render_quad(prog)

        matte_tex = self._resource_pool.acquire_texture(w, h)
        render_shape(matte_tex, 1)
        matte = TextureResult(
            texture_id=matte_tex.glo, width=w, height=h,
            color_space="linear", alpha_premultiplied=False)
        if input_tex is not None and input_tex.is_valid:
            result_tex = self._resource_pool.acquire_texture(w, h)
            render_shape(result_tex, 2, input_tex)
            result = TextureResult(
                texture_id=result_tex.glo, width=w, height=h,
                format=input_tex.format, bbox=input_tex.bbox,
                color_space=input_tex.color_space or "linear",
                alpha_premultiplied=True)
            return {"result": result, "matte": matte}

        result_tex = self._resource_pool.acquire_texture(w, h)
        mode = 1 if params.get("output_mode", "result") == "result" else 0
        render_shape(result_tex, mode)
        return {
            "result": TextureResult(texture_id=result_tex.glo, width=w,
                                     height=h, color_space="linear",
                                     alpha_premultiplied=False),
            "matte": matte,
        }

    def _apply_mesh_warp(self, input_tex: TextureResult, params: dict,
                         w: int, h: int) -> dict:
        """C7: bilinear grid warp (AE Mesh Warp / Fusion Grid Warp).

        The lattice offsets are uploaded as a small RG32F texture with
        texel (col, row) = point displacement in image-space px (y down,
        matching the shader's pixel model). Identity grids short-circuit
        to a pass-through (also keeps zero tile padding honest).
        """
        import numpy as np
        if not input_tex or not input_tex.is_valid:
            return {}
        grid = max(4, min(16, int(params.get("grid_size", 4))))
        offsets = params.get("offsets") or []
        if not any(abs(dx) > 1e-9 or abs(dy) > 1e-9 for dx, dy in offsets):
            return {"result": input_tex}

        prog = self.utility_shaders.get("mesh_warp")
        if prog is None:
            return {"result": input_tex}

        # RG32F lattice: array row r == image grid row r; tex.write maps
        # array row 0 to GL texture y=0 so texelFetch(ivec2(col, row))
        # reads exactly what the shader expects. dx is normalized by frame
        # WIDTH and dy by frame HEIGHT (viewer/drag convention) — scaling
        # dy by the width made vertical drags land off by the aspect ratio
        # on non-square frames.
        lattice = np.zeros(((grid + 1) * (grid + 1), 2), dtype=np.float32)
        for i, (dx, dy) in enumerate(offsets[:lattice.shape[0]]):
            lattice[i, 0] = float(dx) * float(w)
            lattice[i, 1] = float(dy) * float(h)
        offsets_tex = self._resource_pool.acquire_texture(
            grid + 1, grid + 1, components=2, dtype="f4")
        offsets_tex.write(lattice.tobytes())

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0
        offsets_tex.use(1)
        prog['u_offsets'].value = 1
        prog['u_grid'].value = grid
        prog['u_resolution'].value = (float(max(1, w)), float(max(1, h)))
        edge = params.get("edge_mode", "black")
        prog['u_edge_mode'].value = {"black": 0, "clamp": 1, "mirror": 2}.get(edge, 0)

        self._render_quad(prog)
        # The lattice texture is per-dispatch scratch — release it or the
        # pool keeps one RG32F texture alive per evaluation forever.
        offsets_tex.use(0)
        self._resource_pool.release_texture(offsets_tex)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _paint_passthrough(self, input_tex: TextureResult | None,
                           w: int, h: int) -> dict:
        """Pass the input through plus an all-zero mask output.

        Every exit path of _apply_paint must keep the node's two-port
        contract (result + mask) — the mask feeds ai_remove/keyer chains
        and a missing output silently disconnects them.
        """
        outputs = {}
        if input_tex is not None and input_tex.is_valid:
            outputs["result"] = input_tex
        try:
            mask_tex = self._resource_pool.acquire_texture(w, h)
            # P1-5: zero the mask on the GPU instead of uploading CPU bytes.
            # The old np.float32 write fed 16 bytes/pixel into a pooled 'f2'
            # texture (8 bytes/pixel) and raised "data size mismatch" on
            # every frame while allocating a full-frame scratch array.
            mask_fbo = self.fb_manager.get_fbo(mask_tex)
            mask_fbo.clear(0.0, 0.0, 0.0, 0.0)
            outputs["mask"] = TextureResult(
                texture_id=mask_tex.glo, width=w, height=h,
                format=input_tex.format if input_tex is not None else None,
                bbox=input_tex.bbox if input_tex is not None else None,
                color_space=(input_tex.color_space
                             if input_tex is not None else None),
            )
        except Exception:
            logger.exception("Paint: empty mask upload failed")
        return outputs

    def _apply_paint(self, input_tex: TextureResult | None, params: dict) -> dict:
        """Rasterize paint strokes on the CPU and composite them (C2).

        Compositing happens in the premultiplied-linear domain (the
        upstream image is premultiplied scene-linear):

        - clone: ``dst = src_at(dst - offset) * m + dst * (1 - m)``
          (source sampled from this node's input image, per frame)
        - erase: ``dst = dst * (1 - m)``
        - solid: ``dst = dst * (1 - m*a) + color.rgb * (m*a)``

        ``m`` is the feathered stroke coverage × stroke opacity.  The
        ``mask`` output is the union (max) of the raw stroke footprints
        written into RGB AND alpha (white = painted area) so ai_remove and
        keyers — which read back only the RGB channels — see the matte.

        Falls back to passing the input through when rasterization
        resources are unavailable.  Frame gating (CP-003) happens upstream
        in PaintNode._do_evaluate — only strokes active at the playhead
        reach ``params["strokes"]`` here.
        """
        import numpy as np

        w = max(1, int(params.get("output_width", 1920)))
        h = max(1, int(params.get("output_height", 1080)))
        strokes = params.get("strokes") or []
        if not strokes:
            # The mask output is a CONTRACT of this node (ai_remove/keyer
            # consume it) — keep it present (all-zero) even with no
            # strokes, or downstream inputs silently vanish.
            return self._paint_passthrough(input_tex, w, h)

        # ── Read the input image back to CPU (premultiplied RGBA, top-left) ──
        img = None
        if input_tex is not None and input_tex.is_valid:
            gl_tex = self._get_texture_by_id(input_tex.texture_id)
            if gl_tex is not None:
                try:
                    fbo = self.fb_manager.get_fbo(gl_tex)
                    fbo.use()
                    self.ctx.viewport = (0, 0, w, h)
                    raw = fbo.read(components=4, dtype="f4")
                    img = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
                    img = img[::-1].copy()  # top-left origin
                except Exception:
                    # A readback failure must NOT paint onto a black base
                    # (that silently replaces the whole image) — pass the
                    # input through, matching the upload-failure path.
                    logger.exception("Paint: input readback failed")
                    return self._paint_passthrough(input_tex, w, h)

        # Transparent (straight-black) base when no input is connected.
        if img is None:
            img = np.zeros((h, w, 4), dtype=np.float32)
            premult_input = False
        else:
            premult_input = bool(input_tex.alpha_premultiplied)
        if not premult_input:
            # Bring straight-alpha input into the premultiplied domain.
            # R-02：只预乘 RGB——旧实现 ``img * img[..., 3:4]`` 连 alpha
            # 一起乘，straight 输入的 alpha 被平方（0.5→0.25），半透明
            # 素材整幅透明度错误、边缘过淡。
            img[:, :, :3] *= img[:, :, 3:4]

        mask_union = np.zeros((h, w), dtype=np.float32)

        for stroke in strokes:
            stroke_mask = self._rasterize_stroke_mask(stroke, w, h)
            if stroke_mask is None:
                continue
            opacity = max(0.0, min(1.0, float(stroke.get("opacity", 1.0))))
            if opacity <= 0.0:
                # Fully transparent strokes paint nothing and must not
                # flag the mask union (ai_remove would erase unseen areas).
                continue
            # The mask is the AREA matte (white = painted), not coverage —
            # ai_remove erases the union fully regardless of paint opacity.
            mask_union = np.maximum(mask_union, stroke_mask)
            m = stroke_mask * opacity
            m3 = m[:, :, np.newaxis]
            stype = stroke.get("type", "solid")
            if stype == "erase":
                img = img * (1.0 - m3)
            elif stype == "clone":
                src = self._sample_clone_source(img, stroke, w, h)
                img = src * m3 + img * (1.0 - m3)
            else:  # solid
                color = stroke.get("color") or [0.0, 0.0, 0.0, 1.0]
                cov = float(color[3]) * m
                cov3 = cov[:, :, np.newaxis]
                rgb = np.array(color[:3], dtype=np.float32)
                # Premultiplied addend: rgb*coverage, alpha=coverage.
                addend = np.concatenate([rgb * cov3, cov3], axis=-1)
                img = img * (1.0 - cov3) + addend

        # ── Upload results (CPU top-left → GL bottom-left) ──
        result_rgba = np.ascontiguousarray(img[::-1])
        # R-01：统一 CPU→纹理上传——float32 结果写入默认 f2 纹理会
        # data-size mismatch，被当成“Paint 失败”静默直通。
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )
        try:
            result_tex = upload_pooled_texture(
                self._resource_pool, result_rgba, w, h)
        except Exception:
            logger.exception("Paint: result upload failed")
            return self._paint_passthrough(input_tex, w, h)

        mask_rgba = np.ones((h, w, 4), dtype=np.float32)
        mask_rgba[:, :, 0] = mask_union
        mask_rgba[:, :, 1] = mask_union
        mask_rgba[:, :, 2] = mask_union
        mask_rgba[:, :, 3] = mask_union
        try:
            mask_tex = upload_pooled_texture(
                self._resource_pool,
                np.ascontiguousarray(mask_rgba[::-1]), w, h)
        except Exception:
            logger.exception("Paint: mask upload failed")
            mask_tex = None

        fmt = input_tex.format if (input_tex and input_tex.is_valid) else None
        bbox = input_tex.bbox if (input_tex and input_tex.is_valid) else None
        outputs = {
            "result": TextureResult(
                texture_id=result_tex.glo, width=w, height=h,
                format=fmt, bbox=bbox,
                color_space=(input_tex.color_space if input_tex and input_tex.is_valid
                             else "linear"),
                alpha_premultiplied=True),
        }
        if mask_tex is not None:
            outputs["mask"] = TextureResult(
                texture_id=mask_tex.glo, width=w, height=h,
                color_space="linear", alpha_premultiplied=False)
        return outputs

    def _rasterize_stroke_mask(self, stroke: dict, w: int, h: int):
        """Feathered coverage mask for one stroke (float32 h×w, 0..1).

        The polyline is drawn with a round-cap/round-join pen on a
        local bounding-box QImage, blurred by the feather amount, then
        pasted into a full-size canvas — strokes only touch their own
        bbox, not the whole frame.
        """
        import numpy as np
        from PyQt6.QtCore import Qt, QPointF
        from PyQt6.QtGui import QPainter, QPainterPath, QColor, QImage, QPen

        points = stroke.get("points") or []
        if not points:
            return None
        size_norm = max(1e-4, float(stroke.get("size", 0.02)))
        feather = max(0.0, min(1.0, float(stroke.get("feather", 0.5))))
        r_px = size_norm * float(w)

        xs = [float(p[0]) * w for p in points]
        ys = [float(p[1]) * h for p in points]
        pad = r_px * (1.0 + 3.0 * feather) + 4.0
        x0 = max(0, int(min(xs) - pad))
        x1 = min(w, int(max(xs) + pad) + 1)
        y0 = max(0, int(min(ys) - pad))
        y1 = min(h, int(max(ys) + pad) + 1)
        if x1 <= x0 or y1 <= y0:
            return None
        bw, bh = x1 - x0, y1 - y0

        img = QImage(bw, bh, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        if len(xs) == 1:
            # Single click = round dot (moveTo+lineTo on one point is empty).
            path.addEllipse(QPointF(xs[0] - x0, ys[0] - y0), r_px, r_px)
            pen = QPen(QColor(255, 255, 255, 255))
            p.setPen(pen)
            p.fillPath(path, QColor(255, 255, 255, 255))
        else:
            path.moveTo(QPointF(xs[0] - x0, ys[0] - y0))
            for x, y in zip(xs[1:], ys[1:]):
                path.lineTo(QPointF(x - x0, y - y0))
            pen = QPen(QColor(255, 255, 255, 255))
            pen.setWidthF(max(1.0, 2.0 * r_px))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPath(path)
        p.end()

        ptr = img.constBits()
        ptr.setsize(bw * bh * 4)
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape(bh, bw, 4)
        mask = arr[:, :, 3].astype(np.float32) / 255.0

        if feather > 0.0:
            import cv2
            sigma = max(feather * r_px * 0.5, 0.1)
            ksize = int(sigma * 6) | 1
            if ksize >= 3:
                mask = cv2.GaussianBlur(mask, (ksize, ksize), sigmaX=sigma)

        canvas = np.zeros((h, w), dtype=np.float32)
        canvas[y0:y1, x0:x1] = mask
        return canvas

    @staticmethod
    def _sample_clone_source(img, stroke: dict, w: int, h: int):
        """Sample ``img`` shifted by the stroke's normalized src_offset.

        Source pixel = dst - offset (clone stamp semantics: after setting
        the source point, the brush copies from ``落点 - 源点`` offset).
        Out-of-range sampling clamps at the border (no black fringes).
        """
        import numpy as np

        offset = stroke.get("src_offset") or [0.0, 0.0]
        dx = int(round(float(offset[0]) * w))
        dy = int(round(float(offset[1]) * h))
        ys = np.clip(np.arange(h) - dy, 0, h - 1)
        xs = np.clip(np.arange(w) - dx, 0, w - 1)
        return img[ys][:, xs]

    def _apply_compare(self, outputs: dict) -> dict:
        """Render a two-input QC comparison on the shared GPU path."""
        input_a = outputs.get("_input_a")
        input_b = outputs.get("_input_b")
        if input_a is None or not input_a.is_valid:
            return {"result": input_b} if input_b and input_b.is_valid else {}
        if input_b is None or not input_b.is_valid:
            return {"result": input_a}

        prog = self.utility_shaders.get("compare")
        if prog is None:
            logger.warning("Compare shader unavailable; returning input A")
            return {"result": input_a}

        width = max(int(input_a.width), int(input_b.width), 1)
        height = max(int(input_a.height), int(input_b.height), 1)
        out_tex = self._resource_pool.acquire_texture(width, height)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, width, height)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        params = outputs.get("_effect_params", {})
        modes = {"split": 0, "wipe": 1, "difference": 2}
        mode = str(params.get("mode", "split"))
        self._bind_texture_unit(input_a.texture_id, 0)
        self._bind_texture_unit(input_b.texture_id, 1)
        try:
            prog["u_texture_a"].value = 0
            prog["u_texture_b"].value = 1
            prog["u_mode"].value = modes.get(mode, 0)
            prog["u_split_position"].value = max(0.0, min(
                1.0, float(params.get("split_position", 0.5))))
            prog["u_split_angle"].value = float(params.get("split_angle", 0.0))
            prog["u_wipe_position"].value = max(0.0, min(
                1.0, float(params.get("wipe_position", 0.5))))
            prog["u_difference_scale"].value = max(0.0, float(
                params.get("difference_scale", 1.0)))
            prog["u_show_difference_value"].value = bool(
                params.get("show_difference_value", False))
            self._render_quad(prog)
        finally:
            self._unbind_texture_unit(1)

        alpha_mode = (input_a.alpha_premultiplied
                      if input_a.alpha_premultiplied == input_b.alpha_premultiplied
                      else None)
        color_space = (input_a.color_space
                       if input_a.color_space == input_b.color_space
                       else None)
        return {"result": TextureResult(
            texture_id=out_tex.glo,
            width=width,
            height=height,
            format=input_a.format,
            bbox=input_a.bbox,
            alpha_premultiplied=alpha_mode,
            color_space=color_space,
        )}

    def _apply_mask_merge(self, input_a: TextureResult | None,
                          input_b: TextureResult | None,
                          params: dict) -> dict:
        """Combine two mattes on the GPU using the node's boolean operation."""
        def alias(result: TextureResult | None) -> dict:
            return {"result": result, "matte": result} if result else {}

        if input_a is None or not input_a.is_valid:
            return alias(input_b) if input_b and input_b.is_valid else {}
        if input_b is None or not input_b.is_valid:
            return alias(input_a)

        prog = self.utility_shaders.get("mask_merge")
        if prog is None:
            return alias(input_a)

        operations = ["add", "subtract", "multiply", "max", "min",
                      "difference", "average", "screen"]
        operation = str(params.get("operation", "add"))
        operation_index = (operations.index(operation)
                           if operation in operations else 0)
        out_w = max(1, int(max(input_a.width or 1, input_b.width or 1)))
        out_h = max(1, int(max(input_a.height or 1, input_b.height or 1)))
        out_tex = self._resource_pool.acquire_texture(out_w, out_h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, out_w, out_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_a.texture_id)
        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(input_b.texture_id, 1)
        prog["u_texture_a"].value = 0
        prog["u_texture_b"].value = 1
        prog["u_operation"].value = operation_index
        prog["u_invert_a"].value = bool(params.get("invert_a", False))
        prog["u_invert_b"].value = bool(params.get("invert_b", False))
        prog["u_opacity_a"].value = max(0.0, min(1.0, float(
            params.get("opacity_a", 1.0))))
        prog["u_opacity_b"].value = max(0.0, min(1.0, float(
            params.get("opacity_b", 1.0))))
        self._render_quad(prog)
        bind_texture_unit(0, 1)

        result = TextureResult(
            texture_id=out_tex.glo, width=out_w, height=out_h,
            format=input_a.format or input_b.format,
            bbox=input_a.bbox or input_b.bbox,
            color_space="linear", alpha_premultiplied=False)
        return alias(result)

    def _apply_denoise(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply bilateral filter denoising.

        Strength controls the blend between original and filtered result; the
        bilateral filter itself preserves edges via range weighting, so
        ``strength`` maps to a soft denoise amount rather than a hard switch.
        """
        if not input_tex or not input_tex.is_valid:
            return {}

        strength = float(params.get("strength", 0.5))
        radius = max(1, int(params.get("radius", 3)))
        luminance_only = bool(params.get("luminance_only", False))
        chroma_only = bool(params.get("chroma_only", False))

        if strength < 0.01:
            return {"result": input_tex}

        prog = self.utility_shaders.get('denoise')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        mask_tex = params.get("_mask_tex")
        use_mask = bool(mask_tex is not None
                        and getattr(mask_tex, "is_valid", False))

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0
        if use_mask:
            self._bind_texture_unit(mask_tex.texture_id, 1)
        prog['u_use_mask'].value = use_mask
        prog['u_mask_tex'].value = 1
        prog['u_strength'].value = strength
        prog['u_radius'].value = radius
        prog['u_luminance_only'].value = luminance_only
        prog['u_chroma_only'].value = chroma_only
        prog['u_resolution'].value = (float(max(1, w)), float(max(1, h)))

        try:
            self._render_quad(prog)
        finally:
            if use_mask:
                self._unbind_texture_unit(1)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_relight(self, input_tex: TextureResult,
                       normal_tex: TextureResult,
                       position_tex: TextureResult, params: dict,
                       w: int, h: int) -> dict:
        """A1: re-light from NORMAL/POSITION AOVs (Lambert MVP).

        ``normal_tex`` is required (the node marker guarantees it);
        ``position_tex`` is optional — without it the point light treats its
        position parameter as a direction from the world origin.
        """
        if not input_tex or not input_tex.is_valid:
            return {}
        if not normal_tex or not normal_tex.is_valid:
            return {"result": input_tex}

        prog = self.utility_shaders.get('relight')
        if prog is None:
            return {"result": input_tex}

        light_type = params.get("light_type", "directional")
        light_type_int = 0 if light_type == "point" else 1
        normal_decode = params.get("normal_decode", "encoded")
        normal_decode_int = 1 if normal_decode == "signed" else 0
        color = params.get("color", (1.0, 1.0, 1.0))

        has_position = bool(position_tex and position_tex.is_valid)

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        self._bind_texture_unit(normal_tex.texture_id, 1)
        self._bind_texture_unit(
            position_tex.texture_id if has_position else input_tex.texture_id,
            2)
        self._set_uniform_if_present(prog, 'u_texture', 0)
        self._set_uniform_if_present(prog, 'u_normal_tex', 1)
        self._set_uniform_if_present(prog, 'u_position_tex', 2)
        self._set_uniform_if_present(prog, 'u_has_position', has_position)
        self._set_uniform_if_present(prog, 'u_light_type', light_type_int)
        self._set_uniform_if_present(prog, 'u_light_position',
                                     params.get("light_position",
                                                (1.0, 1.0, 2.0)))
        self._set_uniform_if_present(prog, 'u_light_direction',
                                     params.get("light_direction",
                                                (0.0, 0.0, -1.0)))
        self._set_uniform_if_present(prog, 'u_intensity',
                                     float(params.get("intensity", 1.0)))
        self._set_uniform_if_present(prog, 'u_light_color', color)
        self._set_uniform_if_present(prog, 'u_ambient',
                                     float(params.get("ambient", 0.0)))
        self._set_uniform_if_present(prog, 'u_normal_decode',
                                     normal_decode_int)
        self._set_uniform_if_present(prog, 'u_flip_y',
                                     bool(params.get("normal_flip_y", False)))

        try:
            self._render_quad(prog)
        finally:
            self._unbind_texture_unit(1)
            self._unbind_texture_unit(2)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_cryptomatte_select(self, input_tex: TextureResult,
                                  params: dict, w: int, h: int) -> dict:
        """A2: Cryptomatte Select — ID texture → matte / isolate.

        Emits an empty (black) matte when the selection is empty so
        downstream connections stay live.
        """
        if not input_tex or not input_tex.is_valid:
            return {}

        prog = self.utility_shaders.get('cryptomatte_select')
        if prog is None:
            return {"result": input_tex}

        encoding = params.get("id_encoding", "rgba24")
        encoding_int = 1 if encoding == "float_r" else 0
        output_mode = params.get("output_mode", "matte")
        mode_int = 1 if output_mode == "isolate" else 0
        selected = [float(v) for v in
                    (params.get("selected_ids") or [])][:32]
        # moderngl requires the full float array for `uniform float[32]`.
        selected_array = np.zeros(32, dtype="f4")
        if selected:
            selected_array[:len(selected)] = selected

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        self._set_uniform_if_present(prog, 'u_texture', 0)
        self._set_uniform_if_present(prog, 'u_selected', selected_array)
        self._set_uniform_if_present(prog, 'u_count', len(selected))
        self._set_uniform_if_present(prog, 'u_id_encoding', encoding_int)
        self._set_uniform_if_present(prog, 'u_output_mode', mode_int)

        self._render_quad(prog)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=True,
                color_space=input_tex.color_space,
            )
        }

    def _apply_depth_tool(self, effect_type: str, input_tex: TextureResult,
                          depth_tex: TextureResult, params: dict,
                          w: int, h: int) -> dict:
        """A3: shared dispatch for the depth tool shaders (zblur / fog /
        depth_matte).  The depth texture binds to unit 1; the image keeps
        unit 0.  The node marker guarantees a valid depth input — if it is
        missing here the input passes through with a warning.
        """
        prog = (self.utility_shaders.get(effect_type)
                if effect_type in ("zblur", "fog", "depth_matte") else None)
        if prog is None or not input_tex or not input_tex.is_valid:
            return {"result": input_tex} if input_tex else {}

        if not depth_tex or not depth_tex.is_valid:
            logger.warning(
                "%s marker reached the evaluator without a valid depth "
                "input", effect_type)
            return {"result": input_tex}

        depth_units = params.get("depth_units", "normalized")
        units_int = 1 if depth_units == "world" else 0

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        self._bind_texture_unit(depth_tex.texture_id, 1)
        self._set_uniform_if_present(prog, 'u_texture', 0)
        self._set_uniform_if_present(prog, 'u_depth', 1)
        self._set_uniform_if_present(prog, 'u_depth_units', units_int)
        self._set_uniform_if_present(prog, 'u_near',
                                     float(params.get("near", 0.0)))
        self._set_uniform_if_present(prog, 'u_far',
                                     float(params.get("far", 100.0)))

        if effect_type == "zblur":
            self._set_uniform_if_present(
                prog, 'u_focus_distance',
                float(params.get("focus_distance", 0.5)))
            self._set_uniform_if_present(
                prog, 'u_focal_range',
                float(params.get("focal_range", 0.15)))
            self._set_uniform_if_present(
                prog, 'u_max_radius',
                float(params.get("max_blur_radius", 12.0)))
            sample_count = max(4, min(32, int(
                params.get("sample_count", 16))))
            self._set_uniform_if_present(prog, 'u_bokeh',
                                         bool(params.get("bokeh_toggle",
                                                         False)))
            self._set_uniform_if_present(prog, 'u_sample_count',
                                         sample_count)
        elif effect_type == "fog":
            self._set_uniform_if_present(prog, 'u_fog_color',
                                         params.get("fog_color",
                                                    (0.5, 0.6, 0.7)))
            self._set_uniform_if_present(prog, 'u_density',
                                         float(params.get("density", 1.0)))
            self._set_uniform_if_present(prog, 'u_start',
                                         float(params.get("start", 0.0)))
            self._set_uniform_if_present(prog, 'u_end',
                                         float(params.get("end", 1.0)))
        elif effect_type == "depth_matte":
            self._set_uniform_if_present(
                prog, 'u_range_start',
                float(params.get("range_start", 0.3)))
            self._set_uniform_if_present(
                prog, 'u_range_end',
                float(params.get("range_end", 0.7)))
            self._set_uniform_if_present(prog, 'u_feather',
                                         float(params.get("feather", 0.05)))

        try:
            self._render_quad(prog)
        finally:
            self._unbind_texture_unit(1)

        matte_output = effect_type == "depth_matte"
        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=True if matte_output
                else input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_godrays(self, input_tex: TextureResult, params: dict,
                       w: int, h: int) -> dict:
        """B3: screen-space volumetric light shafts.  Occluder is the
        input alpha, or the optional mask texture when one was attached to
        the marker params as ``_mask_tex``."""
        if not input_tex or not input_tex.is_valid:
            return {}

        prog = self.utility_shaders.get('godrays')
        if prog is None:
            return {"result": input_tex}

        mask_tex = params.get("_mask_tex")
        use_mask = bool(mask_tex and mask_tex.is_valid)

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        if use_mask:
            self._bind_texture_unit(mask_tex.texture_id, 1)
        self._set_uniform_if_present(prog, 'u_texture', 0)
        self._set_uniform_if_present(prog, 'u_mask', 1)
        self._set_uniform_if_present(prog, 'u_use_mask', use_mask)
        self._set_uniform_if_present(prog, 'u_light_pos',
                                     params.get("light_pos", (0.5, 0.5)))
        self._set_uniform_if_present(prog, 'u_density',
                                     float(params.get("density", 0.35)))
        self._set_uniform_if_present(prog, 'u_weight',
                                     float(params.get("weight", 0.5)))
        self._set_uniform_if_present(prog, 'u_decay',
                                     float(params.get("decay", 0.95)))
        self._set_uniform_if_present(
            prog, 'u_sample_count',
            max(8, min(64, int(params.get("sample_count", 32)))))
        self._set_uniform_if_present(prog, 'u_light_color',
                                     params.get("light_color",
                                                (1.0, 0.95, 0.85)))

        try:
            self._render_quad(prog)
        finally:
            self._unbind_texture_unit(1)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_simple_per_pixel(self, shader_name: str,
                                input_tex: TextureResult, params: dict,
                                uniform_map: dict, w: int, h: int) -> dict:
        """Shared single-pass helper for the B4 lens shaders."""
        prog = self.utility_shaders.get(shader_name)
        if prog is None or not input_tex or not input_tex.is_valid:
            return {"result": input_tex} if input_tex else {}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        for name, value in uniform_map.items():
            self._set_uniform_if_present(prog, name, value)
        self._render_quad(prog)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_chromatic_aberration(self, input_tex: TextureResult,
                                    params: dict, w: int, h: int) -> dict:
        """B4: radial R/B channel separation."""
        return self._apply_simple_per_pixel(
            "chromatic_aberration", input_tex, params, {
                'u_texture': 0,
                'u_resolution': (max(1, w), max(1, h)),
                'u_offset_r': float(params.get("offset_r", 2.0)),
                'u_offset_b': float(params.get("offset_b", -2.0)),
                'u_center': params.get("center", (0.5, 0.5)),
                'u_edge_boost': float(params.get("edge_boost", 0.0)),
            }, w, h)

    def _apply_vignette(self, input_tex: TextureResult, params: dict,
                        w: int, h: int) -> dict:
        """B4: multiplicative lens darkening."""
        return self._apply_simple_per_pixel(
            "vignette", input_tex, params, {
                'u_texture': 0,
                'u_resolution': (max(1, w), max(1, h)),
                'u_amount': float(params.get("amount", 0.5)),
                'u_radius': float(params.get("radius", 0.7)),
                'u_softness': float(params.get("softness", 0.4)),
                'u_roundness': float(params.get("roundness", 0.0)),
            }, w, h)

    def _apply_lens_flare(self, input_tex: TextureResult, params: dict,
                          w: int, h: int) -> dict:
        """B4: procedural ghosts / halo / anamorphic streak."""
        return self._apply_simple_per_pixel(
            "lens_flare", input_tex, params, {
                'u_texture': 0,
                'u_resolution': (max(1, w), max(1, h)),
                'u_light_pos': params.get("light_pos", (0.3, 0.4)),
                'u_light_color': params.get("light_color",
                                            (1.0, 0.9, 0.75)),
                'u_intensity': float(params.get("intensity", 0.6)),
                'u_ghost_count': int(params.get("ghost_count", 4)),
                'u_ghost_spacing': float(
                    params.get("ghost_spacing", 0.5)),
                'u_ghost_decay': float(params.get("ghost_decay", 0.7)),
                'u_ghost_size': float(params.get("ghost_size", 0.06)),
                'u_halo_radius': float(params.get("halo_radius", 0.25)),
                'u_halo_strength': float(
                    params.get("halo_strength", 0.2)),
                'u_streak_strength': float(
                    params.get("streak_strength", 0.3)),
                'u_streak_width': float(params.get("streak_width", 0.01)),
                'u_use_occlusion': bool(
                    params.get("use_occlusion", False)),
                'u_blend_mode': int(params.get("blend_mode", 0)),
            }, w, h)

    # ── CP-044/045 节点插件效果（plugins/node_plugins/*） ──

    def _apply_generic_mask_mix(self, ns, outputs: dict, result: dict) -> dict:
        """CP-044/045: universal mask gating + mix blend post-pass.

        Runs after _dispatch_effect for every node. Applies only when the
        node carries the ``_generic_mask`` marker (generic mask port with a
        connected upstream) or the generic ``mix`` value deviates from 1.
        Nodes that define their own ``mix`` param (blur/glow/grade/…)
        already blend inside their dispatchers and are skipped — no double
        blending. Legacy mask-aware nodes emit ``_effect_mask`` (not
        ``_generic_mask``) and never reach this path.
        """
        effect_result = result.get("result") if isinstance(result, dict) else None
        if not isinstance(effect_result, TextureResult) or not effect_result.is_valid:
            return result

        mask_tex = outputs.get("_generic_mask")
        has_mask = isinstance(mask_tex, TextureResult) and mask_tex.is_valid

        params = getattr(ns, "parameters", None) or {}
        node_cls = NODE_REGISTRY.get(ns.node_type)
        has_own_mix = any(
            getattr(p, "name", "") == "mix"
            for p in getattr(node_cls, "PARAM_DEFS", []) or [])

        mix_val = 1.0
        if not has_own_mix:
            try:
                mix_val = min(1.0, max(0.0, float(params.get("mix", 1.0))))
            except (TypeError, ValueError):
                mix_val = 1.0

        if not has_mask and (has_own_mix or mix_val >= 1.0):
            return result

        mask_channel = str(params.get("mask_channel", "luma"))
        mask_invert = 1.0 if params.get("mask_invert", False) else 0.0
        base_tex = outputs.get("_effect_input")
        has_base = (isinstance(base_tex, TextureResult) and base_tex.is_valid
                    and base_tex.width == effect_result.width
                    and base_tex.height == effect_result.height)

        w, h = effect_result.width, effect_result.height
        prog = self.utility_shaders.get('generic_mask_mix')
        if prog is None:
            return result

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(effect_result.texture_id)
        prog['u_texture'].value = 0
        if has_base:
            self._bind_texture_unit(base_tex.texture_id, 2)
            prog['u_input'].value = 2
            prog['u_has_input'].value = 1.0
        else:
            prog['u_has_input'].value = 0.0
        if has_mask:
            self._bind_mask_texture(mask_tex.texture_id)
            prog['u_mask'].value = 1
            channel_idx = {"luma": 0, "r": 1, "g": 2, "b": 3, "a": 4}.get(
                mask_channel, 0)
            prog['u_mask_channel'].value = channel_idx
            prog['u_mask_invert'].value = mask_invert
            prog['u_mask_enabled'].value = 1.0
        else:
            # No mask connected: gate fully open, only mix blends. Bind the
            # effect texture as a harmless placeholder so unit 1 is never
            # an unbound sampler (the uniform branch skips sampling it).
            self._bind_mask_texture(effect_result.texture_id)
            prog['u_mask_enabled'].value = 0.0
        prog['u_mix'].value = mix_val

        self._render_quad(prog)

        new_result = dict(result)
        new_result["result"] = TextureResult(
            texture_id=out_tex.glo, width=w, height=h,
            format=effect_result.format,
            bbox=effect_result.effective_bbox(),
            alpha_premultiplied=effect_result.alpha_premultiplied,
            color_space=effect_result.color_space,
        )
        return new_result

    # ── LIB1 内容库节点效果（计划 L / L04：C01/C17）──────────────────

    def _apply_lib1_gradient_map(self, input_tex: TextureResult, params: dict,
                                 w: int, h: int) -> dict:
        """C01：多停靠亮度映射（F16 共享核；查找坐标端点饱和策略）。"""
        interps = ["Linear", "Smooth"]
        interp = params.get("interp_mode", "Linear")
        stops = {}
        for idx in range(4):
            value = params.get(f"stop_{idx}", (0.0, 0.0, 0.0))
            stops[f"u_stop_{idx}"] = (
                float(value[0]), float(value[1]), float(value[2]))
        return self._apply_simple_per_pixel(
            "lib1_gradient_map", input_tex, params, {
                'u_texture': 0,
                **stops,
                'u_pos_1': float(params.get("pos_1", 0.33)),
                'u_pos_2': float(params.get("pos_2", 0.66)),
                'u_interp': interps.index(interp) if interp in interps else 0,
                'u_mix': float(params.get("amount", 1.0)),
            }, w, h)

    def _apply_lib1_frequency_separation(self, outputs: dict,
                                         w: int, h: int) -> dict:
        """C21 多输出子门：low=可分离低通、high=rgb−low（双 pass 三趟）。

        _normalize_output_ports 将 low/high 别名到声明的双输出端口。
        """
        image_tex = outputs.get("_effect_input")
        params = outputs.get("_effect_params", {})
        if image_tex is None or not image_tex.is_valid:
            return {"result": image_tex} if image_tex else {}
        prog_blur = self.utility_shaders.get("lib1_freq_blur")
        prog_high = self.utility_shaders.get("lib1_freq_high")
        if prog_blur is None or prog_high is None:
            return {"result": image_tex}
        radius = float(params.get("radius", 4.0))
        filt = 1 if str(params.get("filter", "Box")) == "Gaussian" else 0

        def _run(prog, src_tex, dst_unit_tex, extra):
            fbo = self.fb_manager.get_fbo(dst_unit_tex)
            fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)
            self._bind_external_texture(src_tex)
            for name, value in extra.items():
                self._set_uniform_if_present(prog, name, value)
            self._render_quad(prog)

        tmp_v = self._resource_pool.acquire_texture(w, h)
        low_tex = self._resource_pool.acquire_texture(w, h)
        high_tex = self._resource_pool.acquire_texture(w, h)
        try:
            _run(prog_blur, int(image_tex.texture_id), tmp_v,
                 {"u_radius": radius, "u_filter": filt, "u_direction": 0})
            # tmp_v 是池化裸纹理（.glo），非 TextureResult——初版误用
            # .texture_id 在第二趟直接 AttributeError（dispatcher 级
            # 未执行过所以未暴露）
            _run(prog_blur, int(tmp_v.glo), low_tex,
                 {"u_radius": radius, "u_filter": filt, "u_direction": 1})
            high_fbo = self.fb_manager.get_fbo(high_tex)
            high_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)
            self._bind_external_texture(int(image_tex.texture_id))
            # low_tex 本就是池化 moderngl 纹理对象（非 TextureResult，
            # 不能走 _resolve_lib1_extra_texture），直接绑单元 1
            low_tex.use(1)
            # 采样器名与 fragment 声明一致（u_low_tex；初版误写 u_low_unit
            # 被静默跳过，u_low_tex 落默认单元 0 → high ≡ image−image ≡ 0）
            self._set_uniform_if_present(prog_high, "u_low_tex", 1)
            self._render_quad(prog_high)
        finally:
            pass
        common = dict(format=image_tex.format,
                      bbox=image_tex.effective_bbox(),
                      alpha_premultiplied=image_tex.alpha_premultiplied,
                      color_space=image_tex.color_space)
        return {
            "low": TextureResult(texture_id=low_tex.glo, width=w, height=h,
                                 **common),
            "high": TextureResult(texture_id=high_tex.glo, width=w, height=h,
                                  **common),
        }

    def _apply_lib1_vector_displace(self, outputs: dict, w: int, h: int) -> dict:
        """C12 多输入子门：_effect_input/_effect_input_map/_effect_mask。"""
        image_tex = outputs.get("_effect_input")
        map_tex = outputs.get("_effect_input_map")
        mask_tex = outputs.get("_effect_mask")
        params = outputs.get("_effect_params", {})
        if image_tex is None or not image_tex.is_valid:
            return {"result": image_tex} if image_tex else {}
        if map_tex is None or not map_tex.is_valid:
            return {"result": image_tex}
        prog = self.utility_shaders.get("lib1_vector_displace")
        if prog is None:
            return {"result": image_tex}
        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._bind_external_texture(int(image_tex.texture_id))
        # 采样器 uniform 名与 fragment 声明一致（u_map_tex/u_mask_tex）；
        # 初版误写 u_map_unit/u_mask_unit——名字不存在赋值被跳过，采样器
        # 保持默认单元 0，把图像当向量图采样（dispatcher 级对拍必炸）
        if "u_map_tex" in prog:
            prog["u_map_tex"].value = 1
        map_tex_obj = self._resolve_lib1_extra_texture(map_tex)
        if map_tex_obj is not None:
            map_tex_obj.use(1)
        if mask_tex is not None and mask_tex.is_valid:
            mask_obj = self._resolve_lib1_extra_texture(mask_tex)
            if mask_obj is not None:
                mask_obj.use(2)
            if "u_has_mask" in prog:
                prog["u_has_mask"].value = 1.0
            if "u_mask_tex" in prog:
                prog["u_mask_tex"].value = 2
        else:
            if "u_has_mask" in prog:
                prog["u_has_mask"].value = 0.0
        for name, value in {
            'u_scale': float(params.get("scale", 50.0)),
            'u_neutral': float(params.get("neutral", 0.5)),
            'u_units': 1 if str(params.get("units", "Pixels")) == "UV" else 0,
        }.items():
            self._set_uniform_if_present(prog, name, value)
        self._render_quad(prog)
        return {"result": TextureResult(
            texture_id=out_tex.glo, width=w, height=h,
            format=image_tex.format,
            bbox=image_tex.effective_bbox(),
            alpha_premultiplied=image_tex.alpha_premultiplied,
            color_space=image_tex.color_space,
        )}

    def _resolve_lib1_extra_texture(self, tex):
        """多输入附加纹理对象解析（复用 _get_texture_by_id 池化扫描）。"""
        getter = getattr(self, "_get_texture_by_id", None)
        if getter is None:
            return None
        return getter(int(tex.texture_id))

    def _apply_lib1_stmap(self, outputs: dict, w: int, h: int) -> dict:
        """C13 多输入子门：_effect_input/_effect_input_map。"""
        image_tex = outputs.get("_effect_input")
        map_tex = outputs.get("_effect_input_map")
        params = outputs.get("_effect_params", {})
        if image_tex is None or not image_tex.is_valid:
            return {"result": image_tex} if image_tex else {}
        if map_tex is None or not map_tex.is_valid:
            return {"result": image_tex}
        prog = self.utility_shaders.get("lib1_stmap")
        if prog is None:
            return {"result": image_tex}
        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._bind_external_texture(int(image_tex.texture_id))
        if "u_map_tex" in prog:
            prog["u_map_tex"].value = 1
        map_tex_obj = self._resolve_lib1_extra_texture(map_tex)
        if map_tex_obj is not None:
            map_tex_obj.use(1)
        for name, value in {
            'u_channels': 1 if str(params.get("channels", "RG")) == "BA" else 0,
            'u_origin': 1 if str(params.get("origin", "BottomLeft")) == "TopLeft" else 0,
            'u_invalid': 1 if str(params.get("invalid", "Clamp")) == "Passthrough" else 0,
            'u_filter': 1 if str(params.get("filter", "Nearest")) == "Linear" else 0,
        }.items():
            self._set_uniform_if_present(prog, name, value)
        self._render_quad(prog)
        return {"result": TextureResult(
            texture_id=out_tex.glo, width=w, height=h,
            format=image_tex.format,
            bbox=image_tex.effective_bbox(),
            alpha_premultiplied=image_tex.alpha_premultiplied,
            color_space=image_tex.color_space,
        )}

    def _apply_lib1_fullframe(self, effect_type: str, input_tex: TextureResult,
                              params: dict, w: int, h: int) -> dict:
        """C18/C19 全帧 CPU 子门：回读 RGBA → CPU 计算 → 直传上传。

        全帧声明（capability fusible=False）：BFS/距离场不适合单 pass
        GLSL；α 通道是 matte 源/载荷（_readback want_alpha 直读不裁）。
        """
        from src.features.composite.evaluators.lib1_fullframe_compute import (
            distance_matte_evaluate,
            fill_holes_evaluate,
        )
        rgba = self._readback_texture_fullres(input_tex, want_alpha=True)
        if rgba is None:
            return {"result": input_tex} if input_tex else {}
        if effect_type == "lib1_distance_matte":
            out = distance_matte_evaluate(rgba, params)
        else:
            out = fill_holes_evaluate(rgba, params)
        result = self._upload_lib1_fullframe_texture(out, input_tex)
        if result is None:
            return {"result": input_tex} if input_tex else {}
        return {"result": result}

    def _apply_lib1_polar_coordinates(self, input_tex: TextureResult,
                                      params: dict, w: int, h: int) -> dict:
        """C11：Cartesian↔polar UV 重映射。"""
        center = params.get("center", (0.5, 0.5))
        return self._apply_simple_per_pixel(
            "lib1_polar_coordinates", input_tex, params, {
                'u_texture': 0,
                'u_mode': 1 if str(params.get("mode", "")) == "PolarToCartesian" else 0,
                'u_center': (float(center[0]), float(center[1])),
                'u_span': float(params.get("angle_span", 360.0)),
                'u_radius_scale': float(params.get("radius_scale", 1.0)),
                'u_filter': 1 if str(params.get("filter", "Nearest")) == "Linear" else 0,
            }, w, h)

    def _apply_lib1_tile_array(self, input_tex: TextureResult,
                               params: dict, w: int, h: int) -> dict:
        """C14：格胞阵列。"""
        return self._apply_simple_per_pixel(
            "lib1_tile_array", input_tex, params, {
                'u_texture': 0,
                'u_count_x': float(params.get("count_x", 3.0)),
                'u_count_y': float(params.get("count_y", 3.0)),
                'u_spacing': float(params.get("spacing", 0.1)),
                'u_offset': (float(params.get("offset_x", 0.0)),
                             float(params.get("offset_y", 0.0))),
            }, w, h)

    def _apply_lib1_halftone(self, input_tex: TextureResult, params: dict,
                             w: int, h: int) -> dict:
        """C06：三版旋转网屏套印 halftone。"""
        sep = str(params.get("cmyk_separation", "CMY approx"))
        return self._apply_simple_per_pixel(
            "lib1_halftone", input_tex, params, {
                'u_texture': 0,
                'u_sep': 0 if sep == "CMY approx" else 1,
                'u_offset': (float(params.get("reg_offset_x", 1.0)),
                             float(params.get("reg_offset_y", 0.0))),
                'u_paper_base': float(params.get("paper_base", 0.9)),
                'u_ink': float(params.get("ink_amount", 0.8)),
            }, w, h)

    def _apply_lib1_hatching(self, input_tex: TextureResult, params: dict,
                             w: int, h: int) -> dict:
        """C07：亮度驱动周期排线。"""
        ink = params.get("ink_color", (0.05, 0.05, 0.08))
        return self._apply_simple_per_pixel(
            "lib1_hatching", input_tex, params, {
                'u_texture': 0,
                'u_spacing': float(params.get("line_spacing", 8.0)),
                'u_angle': float(params.get("angle", 45.0)),
                'u_layers': 2 if str(params.get("layers", "2")) == "2" else 1,
                'u_ink_color': (float(ink[0]), float(ink[1]), float(ink[2])),
                'u_paper': float(params.get("paper", 0.9)),
                'u_amount': float(params.get("amount", 1.0)),
            }, w, h)

    def _apply_lib1_median(self, input_tex: TextureResult, params: dict,
                           w: int, h: int) -> dict:
        """C15：小核中值/百分位。"""
        mode = str(params.get("channel_mode", "RGB"))
        return self._apply_simple_per_pixel(
            "lib1_median", input_tex, params, {
                'u_texture': 0,
                'u_radius': 1 if str(params.get("kernel_size", "3x3")) == "3x3" else 2,
                'u_percentile': float(params.get("percentile", 0.5)),
                'u_mode': 1 if mode == "Luma" else 0,
                'u_amount': float(params.get("amount", 1.0)),
            }, w, h)

    def _apply_lib1_convolution(self, input_tex: TextureResult, params: dict,
                                w: int, h: int) -> dict:
        """C16：3x3 自定义核卷积。"""
        return self._apply_simple_per_pixel(
            "lib1_convolution", input_tex, params, {
                'u_texture': 0,
                'u_k00': float(params.get("k00", 0.0)),
                'u_k01': float(params.get("k01", 0.0)),
                'u_k02': float(params.get("k02", 0.0)),
                'u_k10': float(params.get("k10", 0.0)),
                'u_k11': float(params.get("k11", 1.0)),
                'u_k12': float(params.get("k12", 0.0)),
                'u_k20': float(params.get("k20", 0.0)),
                'u_k21': float(params.get("k21", 0.0)),
                'u_k22': float(params.get("k22", 1.0)) if False else float(params.get("k22", 0.0)),
                'u_normalize': 1.0 if bool(params.get("normalize", False)) else 0.0,
                'u_bias': float(params.get("bias", 0.0)),
            }, w, h)

    def _apply_lib1_halation(self, input_tex: TextureResult, params: dict,
                             w: int, h: int) -> dict:
        """C20：高光红晕（单尺度降级口径）。"""
        return self._apply_simple_per_pixel(
            "lib1_halation", input_tex, params, {
                'u_texture': 0,
                'u_strength': float(params.get("strength", 0.5)),
                'u_radius': float(params.get("radius", 0.5)),
                'u_red_scatter': float(params.get("red_scatter", 0.5)),
                'u_threshold': float(params.get("threshold", 0.55)),
            }, w, h)

    def _apply_lib1_mosaic(self, input_tex: TextureResult, params: dict,
                           w: int, h: int) -> dict:
        """C05：块中心采样马赛克 + 阶梯量化 + Bayer 抖动。"""
        return self._apply_simple_per_pixel(
            "lib1_mosaic", input_tex, params, {
                'u_texture': 0,
                'u_block': float(params.get("block_px", 8.0)),
                'u_levels': float(params.get("levels", 8.0)),
                'u_dither': 1.0 if bool(params.get("dither", False)) else 0.0,
            }, w, h)

    def _apply_lib1_crt(self, input_tex: TextureResult, params: dict,
                        w: int, h: int) -> dict:
        """C08：CRT 扫描线 + RGB Grille 荫罩。"""
        modes = ("Scanlines", "RGB Grille", "Full")
        mode = str(params.get("mode", "Full"))
        return self._apply_simple_per_pixel(
            "lib1_crt", input_tex, params, {
                'u_texture': 0,
                'u_mode': modes.index(mode) if mode in modes else 2,
                'u_scan_count': float(params.get("scan_count", 480.0)),
                'u_scan_strength': float(params.get("scan_strength", 0.35)),
                'u_mask_strength': float(params.get("mask_strength", 0.5)),
            }, w, h)

    def _apply_lib1_glitch(self, input_tex: TextureResult, params: dict,
                           w: int, h: int) -> dict:
        """C09：条带故障位移 + RGB 分离。"""
        return self._apply_simple_per_pixel(
            "lib1_glitch", input_tex, params, {
                'u_texture': 0,
                'u_band_height': float(params.get("band_height", 24.0)),
                'u_shift_amount': float(params.get("shift_amount", 0.35)),
                'u_rgb_split': float(params.get("rgb_split", 0.3)),
                'u_corruption': float(params.get("corruption", 0.35)),
                'u_seed': float(params.get("seed", 0.0)),
            }, w, h)

    def _apply_lib1_kaleidoscope(self, input_tex: TextureResult, params: dict,
                                 w: int, h: int) -> dict:
        """C10：N 扇区折叠镜像万花筒。"""
        center = params.get("center", (0.5, 0.5))
        return self._apply_simple_per_pixel(
            "lib1_kaleidoscope", input_tex, params, {
                'u_texture': 0,
                'u_sectors': float(params.get("sectors", 6.0)),
                'u_rotation': float(params.get("rotation", 0.0)),
                'u_center': (float(center[0]), float(center[1])),
                'u_amount': float(params.get("amount", 1.0)),
            }, w, h)

    def _apply_lib1_rgb_matrix(self, input_tex: TextureResult, params: dict,
                               w: int, h: int) -> dict:
        """C02：3x3 通道矩阵 + RGB 偏置 + strength 混合。"""
        bias = params.get("bias", (0.0, 0.0, 0.0))
        return self._apply_simple_per_pixel(
            "lib1_rgb_matrix", input_tex, params, {
                'u_texture': 0,
                'u_m00': float(params.get("row0_r", 1.0)),
                'u_m01': float(params.get("row0_g", 0.0)),
                'u_m02': float(params.get("row0_b", 0.0)),
                'u_m10': float(params.get("row1_r", 0.0)),
                'u_m11': float(params.get("row1_g", 1.0)),
                'u_m12': float(params.get("row1_b", 0.0)),
                'u_m20': float(params.get("row2_r", 0.0)),
                'u_m21': float(params.get("row2_g", 0.0)),
                'u_m22': float(params.get("row2_b", 1.0)),
                'u_bias': (float(bias[0]), float(bias[1]), float(bias[2])),
                'u_strength': float(params.get("strength", 1.0)),
            }, w, h)

    def _apply_lib1_despill(self, input_tex: TextureResult, params: dict,
                            w: int, h: int) -> dict:
        """C03：绿幕去溢色（Subtract 削峰 / Scale 乘性双向）。"""
        method = str(params.get("method", "Subtract"))
        return self._apply_simple_per_pixel(
            "lib1_despill", input_tex, params, {
                'u_texture': 0,
                'u_method': 1 if method == "Scale" else 0,
                'u_limit': float(params.get("limit", 1.0)),
                'u_strength': float(params.get("strength", 1.0)),
            }, w, h)

    def _apply_lib1_solarize(self, input_tex: TextureResult, params: dict,
                             w: int, h: int) -> dict:
        """C04：阈值折返太阳化（Luma/RGB 双档）。"""
        mode = str(params.get("channel_mode", "Luma"))
        return self._apply_simple_per_pixel(
            "lib1_solarize", input_tex, params, {
                'u_texture': 0,
                'u_threshold': float(params.get("threshold", 1.0)),
                'u_softness': float(params.get("softness", 0.0)),
                'u_mode': 1 if mode == "RGB" else 0,
                'u_amount': float(params.get("amount", 1.0)),
            }, w, h)

    def _apply_lib1_edge_extend(self, input_tex: TextureResult, params: dict,
                                w: int, h: int) -> dict:
        """C17：按 alpha 逐轮扩张前景 RGB（乒乓 radius 轮 + 输出组装）。

        dilate pass 的 alpha 通道承载 solid 标志（首轮由原始 alpha 阈值化）；
        输出 pass 从原输入恢复 alpha（color_only 保留原 alpha /
        premultiplied = 延伸 rgb × 原 alpha）。分块语义：tile_padding=radius。
        """
        if not input_tex or not input_tex.is_valid:
            return {}
        radius = max(0, min(8, int(float(params.get("radius", 4.0)))))
        threshold = float(params.get("threshold", 0.5))
        output_mode = 1 if params.get("output_mode") == "premultiplied" else 0
        if radius < 1:
            return {"result": input_tex}

        dilate = self.utility_shaders.get('lib1_edge_extend_dilate')
        combine = self.utility_shaders.get('lib1_edge_extend_output')
        if dilate is None or combine is None:
            return {"result": input_tex}

        from ..core.gl_bind import bind_texture_unit

        current_tex = None
        try:
            for round_idx in range(radius):
                out_tex = self._resource_pool.acquire_texture(w, h)
                out_fbo = self.fb_manager.get_fbo(out_tex)
                out_fbo.use()
                self.ctx.viewport = (0, 0, w, h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
                if round_idx == 0:
                    self._bind_external_texture(input_tex.texture_id)
                    dilate['u_threshold'].value = threshold
                    dilate['u_first'].value = 1
                else:
                    self._bind_external_texture(current_tex.glo)
                    dilate['u_first'].value = 0
                dilate['u_texture'].value = 0
                self._render_quad(dilate)
                if current_tex is not None:
                    # AUD-6001：按公共 by-id API 释放（pool 无 _texture_by_id）
                    self._resource_pool.release_texture(current_tex)
                current_tex = out_tex

            # 输出组装：dilate 链结果(unit 0) + 原输入(unit 1)
            final_tex = self._resource_pool.acquire_texture(w, h)
            final_fbo = self.fb_manager.get_fbo(final_tex)
            final_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)
            self._bind_external_texture(current_tex.glo)
            combine['u_texture'].value = 0
            bind_texture_unit(input_tex.texture_id, 1)
            combine['u_orig_tex'].value = 1
            combine['u_output_mode'].value = output_mode
            self._render_quad(combine)
            bind_texture_unit(0, 1)
        finally:
            if current_tex is not None:
                self._resource_pool.release_texture(current_tex)

        return {
            "result": TextureResult(
                texture_id=final_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_posterize(self, input_tex: TextureResult, params: dict,
                         w: int, h: int) -> dict:
        """Posterize: per-channel level quantization with optional dither."""
        channel_modes = ["rgb", "rgba", "luminance"]
        mode = params.get("channel_mode", "rgb")
        return self._apply_simple_per_pixel(
            "posterize", input_tex, params, {
                'u_texture': 0,
                'u_levels': float(params.get("levels", 8)),
                'u_channel_mode': channel_modes.index(mode)
                if mode in channel_modes else 0,
                'u_dither': 1.0 if params.get("dither", False) else 0.0,
                'u_resolution': (max(1, w), max(1, h)),
            }, w, h)

    def _apply_vintage_tone(self, input_tex: TextureResult, params: dict,
                            w: int, h: int) -> dict:
        """Vintage tone: film-emulsion bias + contrast/saturation/warm shift."""
        film_types = ["kodak_portra", "fuji_pro", "agfa_optima", "ilford_hp5"]
        film_type = params.get("film_type", "kodak_portra")
        return self._apply_simple_per_pixel(
            "vintage_tone", input_tex, params, {
                'u_texture': 0,
                'u_intensity': float(params.get("intensity", 0.8)),
                'u_warm_shift': float(params.get("warm_shift", 0.0)),
                'u_contrast': float(params.get("contrast_boost", 0.1)),
                'u_saturation': float(params.get("saturation_adjust", 0.0)),
                'u_film_type': film_types.index(film_type)
                if film_type in film_types else 0,
            }, w, h)

    def _apply_degrain(self, input_tex: TextureResult, params: dict,
                       w: int, h: int) -> dict:
        """CP-019: grain-gated smoothing — static single-frame degrain.

        Pass 1 reuses the denoise bilateral filter at full strength for a
        smooth estimate; pass 2 replaces the original with the estimate
        only where the deviation is below the grain threshold (soft gate),
        so real detail passes through. Honest boundary: NOT temporal.
        """
        if not input_tex or not input_tex.is_valid:
            return {}

        threshold = float(params.get("grain_threshold", 0.02))
        amount = float(params.get("amount", 1.0))
        spatial_sigma = float(params.get("spatial_sigma", 1.5))
        if amount <= 0.001:
            return {"result": input_tex}

        combine = self.utility_shaders.get('degrain_combine')
        if combine is None:
            return {"result": input_tex}

        # Pass 1: bilateral smooth estimate (full strength, radius from sigma).
        smoothed = self._apply_denoise(
            input_tex, {
                "strength": 1.0,
                "radius": max(1, int(round(spatial_sigma))),
                "luminance_only": False,
                "chroma_only": False,
            }, w, h)
        smooth_tex = smoothed.get("result") if smoothed else None
        if not isinstance(smooth_tex, TextureResult) or not smooth_tex.is_valid:
            return {"result": input_tex}

        # Pass 2: soft-gated replacement.
        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        combine['u_texture'].value = 0
        self._bind_texture_unit(smooth_tex.texture_id, 1)
        combine['u_smooth'].value = 1
        combine['u_threshold'].value = max(threshold, 1e-4)
        combine['u_amount'].value = min(1.0, max(0.0, amount))

        self._render_quad(combine)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.effective_bbox(),
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_film_grain(self, input_tex: TextureResult, params: dict,
                          w: int, h: int) -> dict:
        """Film grain: resolution-independent value-noise grain.

        Also the CP-019 first-class Grain node's shader backend.
        """
        grain_types = ["fine", "medium", "coarse"]
        grain_type = params.get("grain_type", "medium")
        return self._apply_simple_per_pixel(
            "film_grain", input_tex, params, {
                'u_texture': 0,
                'u_resolution': (max(1, w), max(1, h)),
                'u_grain_size': float(params.get("grain_size", 1.5)),
                'u_intensity': float(params.get("grain_intensity",
                                                params.get("intensity", 0.3))),
                'u_grain_type': grain_types.index(grain_type)
                if grain_type in grain_types else 1,
                'u_color_grain': 1.0 if params.get("color_grain", False) else 0.0,
                'u_luminance_only': 1.0 if params.get("luminance_only",
                                                      False) else 0.0,
                'u_seed': float(params.get("seed", 0.0)),
            }, w, h)

    def _apply_lut3d(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply 3D LUT color transformation."""
        lut_path = params.get("lut_path", "")
        intensity = params.get("intensity", 1.0)
        input_min = params.get("input_range_min", 0.0)
        input_max = params.get("input_range_max", 1.0)
        output_min = params.get("output_range_min", 0.0)
        output_max = params.get("output_range_max", 1.0)
        mix = params.get("mix", 1.0)

        if not lut_path:
            return {"result": input_tex}

        if intensity <= 0.0 and mix <= 0.0:
            return {"result": input_tex}

        prog = self.utility_shaders.get('lut3d')
        if prog is None:
            return {"result": input_tex}

        lut_tex = self._get_or_upload_lut(lut_path)
        if lut_tex is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0

        lut_tex.use(1)
        prog['u_lut_tex'].value = 1

        prog['u_intensity'].value = float(intensity)
        prog['u_input_min'].value = float(input_min)
        prog['u_input_max'].value = float(input_max)
        prog['u_output_min'].value = float(output_min)
        prog['u_output_max'].value = float(output_max)
        prog['u_mix'].value = float(mix)

        mask_tex = params.get("_mask_tex")
        if mask_tex and mask_tex.is_valid:
            self._bind_mask_texture(mask_tex.texture_id)
            prog['u_mask'].value = 2
        else:
            self._bind_mask_texture(self._get_white_mask_texture().glo)
            prog['u_mask'].value = 2

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

    def _get_or_upload_lut(self, lut_path: str) -> Optional[object]:
        """Get or upload a 3D LUT texture (cached by path).

        AUD-6002: the cache is a bounded LRU (entry count AND byte budget).
        3D LUT textures are created via ``ctx.texture3d`` (the resource pool
        only manages 2D colour textures), so without an eviction policy the
        cache grew without bound for the whole renderer lifetime and no GL
        object was ever released.  Evicted — and via :meth:`release_lut_cache`
        also destroyed — textures are released eagerly so the VRAM returns
        to the driver instead of waiting for context teardown.
        """
        self._ensure_lut_cache()

        cached = self._lut_cache.get(lut_path)
        if cached is not None:
            self._lut_cache.move_to_end(lut_path)  # LRU refresh
            return cached

        lut_tex = self._upload_3dlut(lut_path)
        if lut_tex is None:
            return None

        self._lut_cache[lut_path] = lut_tex
        self._lut_cache_bytes += self._lut_texture_bytes(lut_tex)
        self._evict_lut_cache_over_budget()
        return lut_tex

    def _ensure_lut_cache(self) -> None:
        """Create the LUT LRU bookkeeping on first use."""
        if not hasattr(self, '_lut_cache'):
            self._lut_cache: "OrderedDict[str, object]" = OrderedDict()
            self._lut_cache_bytes: int = 0

    @staticmethod
    def _lut_texture_bytes(lut_tex: object) -> int:
        """VRAM footprint of a 3D LUT texture (RGBA-free, 3-channel f4)."""
        try:
            width, height, depth = lut_tex.size  # type: ignore[attr-defined]
            return int(width) * int(height) * int(depth) \
                * int(lut_tex.components) * 4  # dtype is 'f4'
        except (AttributeError, TypeError, ValueError):
            # Unknown texture shape: assume a typical 64^3 f4 LUT (~3 MB).
            return 64 * 64 * 64 * 3 * 4

    def _evict_lut_cache_over_budget(self) -> None:
        """Evict least-recently-used LUTs until both budgets hold."""
        while len(self._lut_cache) > 1 and (
                len(self._lut_cache) > _LUT_CACHE_MAX_ENTRIES
                or self._lut_cache_bytes > _LUT_CACHE_MAX_BYTES):
            path, tex = self._lut_cache.popitem(last=False)
            self._lut_cache_bytes -= self._lut_texture_bytes(tex)
            self._release_lut_texture(path, tex)

    @staticmethod
    def _release_lut_texture(path: str, tex: object) -> None:
        """Release one LUT GL texture, tolerating an already-dead context."""
        try:
            tex.release()  # type: ignore[attr-defined]
        except Exception as exc:  # context may already be destroyed
            logger.debug("Failed to release 3D LUT %r: %s", path, exc)

    def release_lut_cache(self) -> None:
        """Release every cached 3D LUT texture (renderer destroy path).

        Must be called on the thread that owns the GL context (the render
        thread), e.g. when the renderer tears down its GPU resources.
        """
        self._ensure_lut_cache()
        for path, tex in self._lut_cache.items():
            self._release_lut_texture(path, tex)
        self._lut_cache.clear()
        self._lut_cache_bytes = 0

    def _upload_3dlut(self, path: str):
        """Parse and upload a 3D LUT file (.cube or .3dl).

        P1-COLOR-11: 解析收敛到统一 lut_loader（含 .3dl 与 metadata），
        上传布局保持 (B, G, R) 轴序与既有 shader 采样一致。
        """
        try:
            from src.shared.color.lut_loader import load_lut
            lut = load_lut(path)
            data = np.ascontiguousarray(lut.table, dtype='f4').tobytes()
            size = lut.size
            lut_tex = self.ctx.texture3d((size, size, size), 3, data=data, dtype='f4')
            lut_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            lut_tex.repeat_x = lut_tex.repeat_y = lut_tex.repeat_z = False
            return lut_tex
        except Exception as e:
            logger.warning(f"Error uploading 3D LUT {path}: {e}")
        return None
