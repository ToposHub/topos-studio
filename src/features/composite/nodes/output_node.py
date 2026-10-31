"""
Output Node - Final output of the composite graph.

Takes one input and passes it through to the display.
This is the terminal node that the renderer uses to produce the final image.

Supports:
- format_mode:
    - "input": inherit resolution from upstream input (default)
    - "custom": user-specified width/height
    - "project": project-defined width/height (falls back to input if no project)
- custom width/height when format_mode == "custom"
- output_color_space: force output to a specific color space
    - "same": inherit from input (default)
    - "linear" / "srgb" / "display": explicit target
- alpha_policy:
    - "keep": pass through alpha unchanged (default)
    - "premult": premultiply RGB by alpha
    - "straight": un-premultiply (divide RGB by alpha)
    - "discard": set alpha to 1.0
- background_color: (R, G, B, A) float list — composited behind the image
  when the input has transparent regions. Default [0, 0, 0, 0].
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.core.format import ImageFormat, BBox
from src.features.composite.core.color_space import ColorSpace


class OutputNode(Node):
    """Terminal output node for the composite graph."""

    HELP_TEXT = """终端输出节点。合成管线的最终输出点，支持格式和 alpha 策略控制。
输入：A（图像输入）
输出：无（显示到视口）
参数：
  - format_mode: 格式来源（input/custom/project）
  - custom_format: 自定义格式（当 format_mode=custom）
  - alpha_policy: Alpha 处理方式（keep/premult/straight/discard）
  - bg_color: 背景色（当 alpha_policy 需要合成时）
注意：
  - alpha_policy=premult: RGB × A（直 alpha → 预乘）
  - alpha_policy=straight: RGB ÷ A（预乘 → 直 alpha）
  - alpha_policy=discard: Alpha 设为 1.0（移除透明通道）"""
    DISPLAY_NAME = "Output"

    node_type = "output"
    # CP-044: 渲染汇聚节点不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "Output"
    header_color = "#2d8c3c"  # Green

    # Metadata
    SEARCH_ALIASES = ["output", "display", "out", "结果", "输出", "显示"]
    DESCRIPTION = "合成图的最终输出节点"
    TAGS = ["output", "terminal"]

    PARAM_DEFS = [
        ParamDef("format_mode", "option", "input",
                 labels=["input", "custom", "project"]),
        # Named terminal target. Legacy projects without this field resolve
        # to the default ``display`` target in output_targets.py.
        ParamDef("output_name", "str", "display"),
        # CP-021: 合成级运动模糊快门。VectorBlur（use_project_shutter）等
        # motion-blur 消费节点经 CompositeEvalContext.motion_blur_shutter 读取。
        ParamDef("motion_blur_shutter", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.05, group="Motion Blur"),
        ParamDef("width", "int", 1920, min_val=1, max_val=16384, step=1),
        ParamDef("height", "int", 1080, min_val=1, max_val=16384, step=1),
        # Output color space: controls whether the Output node bakes a colour
        # transform into the display texture.
        #   "same"   — keep working space (linear). Default. The viewer's own
        #              ODT (linear→sRGB) handles display; file_output encodes
        #              on export based on the texture's linear tag.
        #   "linear" — same as "same" (explicit).
        #   "srgb"   — bake linear→sRGB into the display texture here. Use when
        #              you want the output texture itself to be sRGB-encoded
        #              (e.g. for a downstream consumer that expects sRGB). Note:
        #              if you select this, switch the viewer's Display Transform
        #              to "Linear (Raw)" to avoid double-encoding on screen.
        ParamDef("output_color_space", "option", "same",
                 labels=["same", "linear", "srgb"]),
        ParamDef("alpha_policy", "option", "keep",
                 labels=["keep", "premult", "straight", "discard"]),
        ParamDef("background_color", "str", "0,0,0,0"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image")
        ]
        self.outputs = []  # Terminal node, no outputs
        self.parameters = {
            "format_mode": "input",
            "output_name": "display",
            "width": 1920,
            "height": 1080,
            "output_color_space": "same",
            "alpha_policy": "keep",
            "background_color": "0,0,0,0",
        }
        self._resize_prog = None          # Cached resize shader program
        self._placement_prog = None       # Cached packed-data-window placement shader
        self._bg_composite_prog = None    # Cached background-composite program
        self._alpha_prog = None           # Cached alpha-manipulation program
        self._cs_prog = None              # Cached color-space transform program

    # ── Helpers ──

    @staticmethod
    def _parse_bg_color(raw: str):
        """Parse a 'R,G,B,A' string into a tuple of 4 floats clamped to [0,1]."""
        try:
            parts = [float(v) for v in str(raw).split(",")]
            if len(parts) == 3:
                parts.append(1.0)
            elif len(parts) < 3:
                return (0.0, 0.0, 0.0, 0.0)
            return tuple(max(0.0, min(1.0, v)) for v in parts[:4])
        except (ValueError, TypeError):
            return (0.0, 0.0, 0.0, 0.0)

    @staticmethod
    def _resolve_color_space(output_cs_param: str, input_cs: Optional[str]) -> Optional[str]:
        """Determine the effective output color space.

        Legacy values are normalised: "display" (removed from the UI options)
        is treated as "same" to preserve old projects without re-saving.
        """
        if output_cs_param in ("same", "display", "", None):
            return input_cs
        return output_cs_param

    def _needs_alpha_pass(self) -> bool:
        """Check whether an alpha manipulation pass is needed."""
        return self.parameters.get("alpha_policy", "keep") != "keep"

    def _needs_bg_pass(self) -> bool:
        """Check whether a background composite pass is needed."""
        bg = self._parse_bg_color(self.parameters.get("background_color", "0,0,0,0"))
        return bg[3] > 0.0  # Only composite if background is not fully transparent

    @staticmethod
    def _release_intermediate_texture(display: TextureResult, input_texture_id: int,
                                      resource_pool, fb_manager) -> None:
        """Return an Output-only intermediate to the pool after the next pass."""
        if display.texture_id <= 0 or display.texture_id == input_texture_id:
            return
        if resource_pool is not None:
            resource_pool.release_texture_by_id(display.texture_id)
            return
        # Legacy non-pooled evaluation path. FramebufferManager owns these
        # temporary textures and removes their cached FBO attachment safely.
        for texture in list(getattr(fb_manager, "_active_textures", ())):
            if texture.glo == display.texture_id:
                fb_manager.release_temp_texture(texture)
                break

    @staticmethod
    def _result_like(source: TextureResult, **updates: Any) -> TextureResult:
        """Create an output result without silently dropping auxiliary data.

        Depth and AOV textures are valid only while their pixel dimensions
        match the colour result. Format/alpha/colour-space passes keep the
        auxiliary buffers; a resize or canvas placement explicitly invalidates
        them because this node does not resample those passes yet.
        """
        width = int(updates.get("width", source.width))
        height = int(updates.get("height", source.height))
        if (width, height) != (source.width, source.height):
            updates.setdefault("depth_texture_id", None)
            updates.setdefault("has_deep_data", False)
            updates.setdefault("aov_texture_ids", {})
            updates.setdefault("aov_dimensions", {})
            diagnostics = source.render_diagnostics
            if isinstance(diagnostics, dict):
                diagnostics = dict(diagnostics)
                diagnostics["auxiliary_passes_invalidated"] = "output_resize"
                updates.setdefault("render_diagnostics", diagnostics)
        return source.with_updates(**updates)

    # ── Main Evaluation ──

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if not image_input or not image_input.is_valid:
            return {}

        # --- Step 1: Resolve target dimensions & resize if needed ---
        result = self._apply_format(image_input, ctx, fb_manager, resource_pool)
        display = result.get("display")
        if display is None:
            return result

        # --- Step 2: Background composite ---
        if self._needs_bg_pass():
            previous_display = display
            result = self._apply_background(display, ctx, fb_manager, resource_pool)
            display = result.get("display")
            if display is None:
                return result
            self._release_intermediate_texture(
                previous_display, image_input.texture_id, resource_pool, fb_manager
            )

        # --- Step 3: Alpha manipulation ---
        if self._needs_alpha_pass():
            previous_display = display
            result = self._apply_alpha_policy(display, ctx, fb_manager, resource_pool)
            display = result.get("display")
            if display is None:
                return result
            self._release_intermediate_texture(
                previous_display, image_input.texture_id, resource_pool, fb_manager
            )

        # --- Step 4: Output color space transform (bake linear→sRGB if requested) ---
        # Default ("same"/"linear") keeps the working space; the viewer's ODT
        # and file_output's export encode handle display/encoding. "srgb" bakes
        # the encode here so the display texture itself is sRGB-encoded.
        output_cs_param = self.parameters.get("output_color_space", "same")
        output_cs = self._resolve_color_space(output_cs_param, image_input.color_space)
        if output_cs == ColorSpace.SRGB and display.color_space != ColorSpace.SRGB:
            previous_display = display
            result = self._apply_color_space(display, ColorSpace.SRGB,
                                             ctx, fb_manager, resource_pool)
            display = result.get("display")
            if display is None:
                return result
            self._release_intermediate_texture(
                previous_display, image_input.texture_id, resource_pool, fb_manager
            )

        # --- Step 5: Finalize output color space metadata ---
        # output_cs was resolved in Step 4 (and possibly updated by the bake).
        alpha_premul = display.alpha_premultiplied
        policy = self.parameters.get("alpha_policy", "keep")
        if policy == "premult":
            alpha_premul = True
        elif policy == "straight":
            alpha_premul = False
        elif policy == "discard":
            alpha_premul = None  # Opaque — premult status irrelevant

        result["display"] = self._result_like(
            display,
            color_space=output_cs,
            alpha_premultiplied=alpha_premul,
        )
        return result

    # ── Format / Resolution ──

    def _apply_format(self, image_input: TextureResult, ctx, fb_manager,
                      resource_pool) -> Dict[str, TextureResult]:
        """Apply format_mode to determine output dimensions."""
        mode = self.parameters.get("format_mode", "input")

        if mode == "custom":
            target_w = max(1, int(self.parameters.get("width", 1920)))
            target_h = max(1, int(self.parameters.get("height", 1080)))
        elif mode == "project":
            # Project mode: try to use project settings; fall back to input dims
            # For now, project resolution is stored as width/height params
            target_w = max(1, int(self.parameters.get("width", 1920)))
            target_h = max(1, int(self.parameters.get("height", 1080)))
        else:
            # "input" — inherit from upstream
            return self._pass_through_with_format(
                image_input, ctx, fb_manager, resource_pool
            )

        custom_fmt = ImageFormat(width=target_w, height=target_h,
                                 name=f"{'Project' if mode == 'project' else 'Custom'} Output")
        custom_bbox = BBox.full_frame(target_w, target_h)

        # Transform can retain a full ImageFormat while carrying only a packed
        # moved data window. Resizing that texture loses its composition-space
        # position; restore it into the requested output canvas instead.
        if self._needs_canvas_placement(image_input, custom_fmt):
            return self._place_on_canvas(
                image_input, custom_fmt, ctx, fb_manager, resource_pool
            )

        if image_input.width != target_w or image_input.height != target_h:
            result = self._resize(image_input, target_w, target_h,
                                  ctx, fb_manager, resource_pool)
            resized = result.get("display")
            if resized:
                result["display"] = self._result_like(
                    resized,
                    format=custom_fmt,
                    bbox=custom_bbox,
                    color_space=image_input.color_space,
                    alpha_premultiplied=image_input.alpha_premultiplied,
                )
            return result

        return {
            "display": self._result_like(
                image_input,
                format=custom_fmt,
                bbox=custom_bbox,
                color_space=image_input.color_space,
                alpha_premultiplied=image_input.alpha_premultiplied,
            )
        }

    @staticmethod
    def _needs_canvas_placement(src: TextureResult, target_fmt: ImageFormat) -> bool:
        """Return whether a packed source must be restored into its canvas."""
        bbox = src.effective_bbox()
        return (
            bbox.x != target_fmt.origin_x
            or bbox.y != target_fmt.origin_y
            or bbox.width != target_fmt.width
            or bbox.height != target_fmt.height
        )

    @staticmethod
    def _clip_bbox_to_format(bbox: BBox, fmt: ImageFormat) -> BBox:
        """Clip a data window to a format whose origin may be non-zero."""
        left = max(fmt.origin_x, bbox.x)
        bottom = max(fmt.origin_y, bbox.y)
        right = min(fmt.origin_x + fmt.width, bbox.right)
        top = min(fmt.origin_y + fmt.height, bbox.top)
        if right <= left or top <= bottom:
            return BBox(x=fmt.origin_x, y=fmt.origin_y, width=0, height=0)
        return BBox(x=left, y=bottom, width=right - left, height=top - bottom)

    def _pass_through_with_format(self, image_input: TextureResult, ctx, fb_manager,
                                  resource_pool) -> Dict[str, TextureResult]:
        """'input' mode — inherit format/bbox from upstream, or create from dimensions."""
        fmt = image_input.format
        bbox = image_input.bbox
        if fmt is None and image_input.width > 0 and image_input.height > 0:
            fmt = ImageFormat(width=image_input.width, height=image_input.height, name="Auto")
            bbox = BBox.full_frame(image_input.width, image_input.height)
        if fmt is not None and self._needs_canvas_placement(image_input, fmt):
            return self._place_on_canvas(image_input, fmt, ctx, fb_manager, resource_pool)
        if fmt is not None and image_input.format is None:
            return {
                "display": self._result_like(
                    image_input,
                    format=fmt,
                    bbox=bbox,
                    color_space=image_input.color_space,
                    alpha_premultiplied=image_input.alpha_premultiplied,
                    bit_depth=image_input.bit_depth,
                    frame_metadata=image_input.frame_metadata,
                )
            }
        return {"display": image_input}

    def _place_on_canvas(self, src: TextureResult, target_fmt: ImageFormat, ctx,
                         fb_manager, resource_pool) -> Dict[str, TextureResult]:
        """Place a packed data window into a transparent output canvas."""
        import numpy as np
        import moderngl

        if self._placement_prog is None:
            self._placement_prog = ctx.program(
                vertex_shader=_RESIZE_VERTEX,
                fragment_shader=_PLACE_ON_CANVAS_FRAGMENT,
            )
        prog = self._placement_prog

        target_w = max(1, target_fmt.width)
        target_h = max(1, target_fmt.height)
        if resource_pool:
            out_tex = resource_pool.acquire_temp_texture(target_w, target_h)
        else:
            out_tex = fb_manager.get_temp_texture(target_w, target_h)
        out_fbo = fb_manager.get_fbo(out_tex)
        out_fbo.use()
        ctx.viewport = (0, 0, target_w, target_h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)

        bbox = src.effective_bbox()
        # Shader coordinates are local to the target FBO, while BBox values
        # remain in composition-space coordinates.
        dst_bbox = (
            float(bbox.x - target_fmt.origin_x),
            float(bbox.y - target_fmt.origin_y),
            float(max(1, bbox.width)),
            float(max(1, bbox.height)),
        )

        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(src.texture_id, 0)
        prog['u_texture'].value = 0
        prog['u_bbox'].value = dst_bbox
        prog['u_output_resolution'].value = (float(target_w), float(target_h))

        vbo = ctx.buffer(np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ], dtype='f4'))
        vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
        vao.render(moderngl.TRIANGLE_STRIP)
        bind_texture_unit(0, 0)
        vao.release()
        vbo.release()

        return {
            "display": self._result_like(
                src,
                texture_id=out_tex.glo,
                width=target_w,
                height=target_h,
                format=target_fmt,
                bbox=self._clip_bbox_to_format(bbox, target_fmt),
            )
        }

    # ── Background Composite ──

    def _apply_background(self, src: TextureResult, ctx, fb_manager,
                          resource_pool) -> Dict[str, TextureResult]:
        """Composite the image over a solid background color."""
        bg = self._parse_bg_color(self.parameters.get("background_color", "0,0,0,0"))
        if bg[3] <= 0.0:
            return {"display": src}

        import numpy as np
        if self._bg_composite_prog is None:
            self._bg_composite_prog = ctx.program(
                vertex_shader=_RESIZE_VERTEX,
                fragment_shader=_BG_COMPOSITE_FRAGMENT,
            )
        prog = self._bg_composite_prog

        w, h = src.width, src.height
        if resource_pool:
            out_tex = resource_pool.acquire_temp_texture(w, h)
        else:
            out_tex = fb_manager.get_temp_texture(w, h)
        out_fbo = fb_manager.get_fbo(out_tex)
        out_fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)

        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(src.texture_id, 0)

        prog['u_texture'].value = 0
        prog['u_bg_color'].value = bg
        prog['u_input_premultiplied'].value = bool(src.alpha_premultiplied)

        vbo = ctx.buffer(np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ], dtype='f4'))
        vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
        import moderngl
        vao.render(moderngl.TRIANGLE_STRIP)

        bind_texture_unit(0, 0)
        vao.release()
        vbo.release()

        return {
            "display": self._result_like(
                src, texture_id=out_tex.glo, alpha_premultiplied=True)
        }

    # ── Alpha Policy ──

    def _apply_alpha_policy(self, src: TextureResult, ctx, fb_manager,
                            resource_pool) -> Dict[str, TextureResult]:
        """Apply the configured alpha policy via a GPU pass."""
        policy = self.parameters.get("alpha_policy", "keep")
        if policy == "keep":
            return {"display": src}
        if ((policy == "premult" and src.alpha_premultiplied is True)
                or (policy == "straight" and src.alpha_premultiplied is False)):
            return {"display": src}

        import numpy as np
        if self._alpha_prog is None:
            self._alpha_prog = ctx.program(
                vertex_shader=_RESIZE_VERTEX,
                fragment_shader=_ALPHA_FRAGMENT,
            )
        prog = self._alpha_prog

        w, h = src.width, src.height
        if resource_pool:
            out_tex = resource_pool.acquire_temp_texture(w, h)
        else:
            out_tex = fb_manager.get_temp_texture(w, h)
        out_fbo = fb_manager.get_fbo(out_tex)
        out_fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)

        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(src.texture_id, 0)

        prog['u_texture'].value = 0
        prog['u_alpha_mode'].value = float(
            {"keep": 0, "premult": 1, "straight": 2, "discard": 3}.get(policy, 0)
        )
        prog['u_input_premultiplied'].value = bool(src.alpha_premultiplied)

        vbo = ctx.buffer(np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ], dtype='f4'))
        vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
        import moderngl
        vao.render(moderngl.TRIANGLE_STRIP)

        bind_texture_unit(0, 0)
        vao.release()
        vbo.release()

        return {
            "display": self._result_like(src, texture_id=out_tex.glo)
        }

    # ── Color Space Transform ──

    def _apply_color_space(self, src: TextureResult, target_cs: str,
                           ctx, fb_manager, resource_pool=None
                           ) -> Dict[str, TextureResult]:
        """Bake a colour-space transform into the display texture.

        Currently supports linear→sRGB (the only direction the Output node
        bakes — sRGB→linear is the IDT's job, done at source nodes). The
        viewer's ODT is a separate, non-baked display transform; this method
        produces a texture whose pixels are actually in ``target_cs``.

        Args:
            src: input display TextureResult.
            target_cs: target colour space (currently only "srgb" supported).
            ctx: GL context (moderngl via CompositeEvalContext).
            fb_manager: framebuffer manager.
            resource_pool: GPU resource pool.

        Returns:
            {"display": TextureResult} with converted pixels tagged target_cs.
        """
        import numpy as np
        from src.features.composite.core.color_space import ColorSpace as CS

        # Only linear→sRGB is supported here. If target == source, no-op.
        if target_cs != CS.SRGB or src.color_space == CS.SRGB:
            return {"display": src}

        w, h = src.width, src.height
        out_tex = resource_pool.acquire_texture(w, h) \
            if resource_pool is not None else fb_manager.get_temp_texture(w, h)
        out_fbo = fb_manager.get_fbo(out_tex)
        out_fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)

        if self._cs_prog is None:
            self._cs_prog = ctx.program(
                vertex_shader=_RESIZE_VERTEX,
                fragment_shader=_LINEAR_TO_SRGB_FRAGMENT,
            )
        prog = self._cs_prog
        prog['u_input_premultiplied'].value = bool(src.alpha_premultiplied)

        from ..core.gl_bind import bind_texture_unit
        import moderngl
        bind_texture_unit(src.texture_id, 0)

        prog['u_texture'].value = 0

        vbo = ctx.buffer(np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ], dtype='f4'))
        vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
        vao.render(moderngl.TRIANGLE_STRIP)

        bind_texture_unit(0, 0)
        vao.release()
        vbo.release()

        return {
            "display": self._result_like(
                src,
                texture_id=out_tex.glo,
                color_space=CS.SRGB,
            )
        }

    # ── Resize ──

    def _resize(self, src: TextureResult, w: int, h: int, ctx, fb_manager,
                resource_pool=None) -> Dict[str, TextureResult]:
        """Resize the input texture to target dimensions using GPU."""
        import numpy as np

        # Reuse cached program instead of creating a new one each time
        if self._resize_prog is None:
            self._resize_prog = ctx.program(
                vertex_shader=_RESIZE_VERTEX,
                fragment_shader=_RESIZE_FRAGMENT,
            )
        prog = self._resize_prog

        # Use resource_pool if available for tracked texture allocation
        if resource_pool:
            out_tex = resource_pool.acquire_temp_texture(w, h)
        else:
            out_tex = fb_manager.get_temp_texture(w, h)
        out_fbo = fb_manager.get_fbo(out_tex)
        out_fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)

        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(src.texture_id, 0)

        prog['u_texture'].value = 0
        prog['u_src_resolution'].value = (float(src.width), float(src.height))

        vbo = ctx.buffer(np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ], dtype='f4'))
        vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
        import moderngl
        vao.render(moderngl.TRIANGLE_STRIP)
        # No ctx.finish() — GPU pipeline ordering is implicit,
        # and the render thread's fence mechanism handles texture reuse safety.

        bind_texture_unit(0, 0)

        vao.release()
        vbo.release()

        return {
            "display": self._result_like(
                src,
                texture_id=out_tex.glo,
                width=w,
                height=h,
            )
        }

    def cleanup(self) -> None:
        """Release GPU resources."""
        if self._resize_prog is not None:
            self._resize_prog.release()
            self._resize_prog = None
        if self._placement_prog is not None:
            self._placement_prog.release()
            self._placement_prog = None
        if self._bg_composite_prog is not None:
            self._bg_composite_prog.release()
            self._bg_composite_prog = None
        if self._alpha_prog is not None:
            self._alpha_prog.release()
            self._alpha_prog = None
        if self._cs_prog is not None:
            self._cs_prog.release()
            self._cs_prog = None


# ── Shaders ──

# Minimal resize shaders (passthrough with UV scaling)
_RESIZE_VERTEX = """
#version 410 core
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_uv = in_uv;
}
"""

_RESIZE_FRAGMENT = """
#version 410 core
uniform sampler2D u_texture;
uniform vec2 u_src_resolution;
in vec2 v_uv;
out vec4 frag_color;
void main() {
    frag_color = texture(u_texture, v_uv);
}
"""

# Linear → sRGB encode (sRGB OETF). Mirrors the viewer's ODT and the
# cs_transform shader so the Output node can bake an sRGB-encoded texture
# when output_color_space="srgb".
_LINEAR_TO_SRGB_FRAGMENT = """
#version 410 core
uniform sampler2D u_texture;
uniform bool u_input_premultiplied;
in vec2 v_uv;
out vec4 frag_color;

vec3 linear_to_srgb(vec3 c) {
    vec3 lo = c * 12.92;
    vec3 hi = 1.055 * pow(max(c, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
    return mix(hi, lo, step(c, vec3(0.0031308)));
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    float alpha = color.a;
    if (u_input_premultiplied && alpha > 0.0001) {
        color.rgb /= alpha;
    }
    color.rgb = linear_to_srgb(color.rgb);
    if (u_input_premultiplied) {
        color.rgb *= alpha;
    }
    frag_color = color;
}
"""

# Places a packed data window at its composition-space bbox inside the full
# Output canvas. Coordinates use the OpenGL bottom-left convention used by BBox.
_PLACE_ON_CANVAS_FRAGMENT = """
#version 410 core
uniform sampler2D u_texture;
uniform vec4 u_bbox;
uniform vec2 u_output_resolution;
in vec2 v_uv;
out vec4 frag_color;
void main() {
    vec2 output_px = v_uv * u_output_resolution;
    vec2 local_uv = (output_px - u_bbox.xy) / u_bbox.zw;
    if (local_uv.x < 0.0 || local_uv.x > 1.0 ||
        local_uv.y < 0.0 || local_uv.y > 1.0) {
        frag_color = vec4(0.0);
        return;
    }
    frag_color = texture(u_texture, local_uv);
}
"""

# Background composite: over operation (src over bg)
_BG_COMPOSITE_FRAGMENT = """
#version 410 core
uniform sampler2D u_texture;
uniform vec4 u_bg_color;
uniform bool u_input_premultiplied;
in vec2 v_uv;
out vec4 frag_color;
void main() {
    vec4 fg = texture(u_texture, v_uv);
    float alpha = fg.a;
    // Porter-Duff "source over" — composite foreground over background
    vec3 fg_premult = u_input_premultiplied ? fg.rgb : fg.rgb * alpha;
    vec3 result = fg_premult + u_bg_color.rgb * u_bg_color.a * (1.0 - alpha);
    float out_alpha = alpha + u_bg_color.a * (1.0 - alpha);
    frag_color = vec4(result, out_alpha);
}
"""

# Alpha policy shader
# u_alpha_mode: 0=keep, 1=premult, 2=straight, 3=discard
_ALPHA_FRAGMENT = """
#version 410 core
uniform sampler2D u_texture;
uniform float u_alpha_mode;
uniform bool u_input_premultiplied;
in vec2 v_uv;
out vec4 frag_color;
void main() {
    vec4 c = texture(u_texture, v_uv);
    vec3 straight_rgb = c.rgb;
    if (u_input_premultiplied && c.a > 0.001) {
        straight_rgb /= c.a;
    }
    if (u_alpha_mode < 0.5) {
        // keep
        frag_color = c;
    } else if (u_alpha_mode < 1.5) {
        // premult: straight RGB *= A; do not double-premultiply.
        frag_color = vec4(straight_rgb * c.a, c.a);
    } else if (u_alpha_mode < 2.5) {
        // straight: un-premultiply only when the source was premultiplied.
        frag_color = vec4(straight_rgb, c.a);
    } else {
        // discard: preserve straight RGB when promoting alpha to opaque.
        frag_color = vec4(straight_rgb, 1.0);
    }
}
"""
