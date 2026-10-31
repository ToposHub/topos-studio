"""
TransformEvaluatorMixin - Transform and spatial effect implementations.

Provides GPU-based spatial effects:
- Transform (translate/scale/rotate) with filter support
- CornerPin (perspective warp via homography)
- Reformat (resize with fit/fill/distort modes)
- Crop (region extraction)
- CS Transform (color space conversion)

All effects preserve HDR data and support proper bbox tracking.
"""

import math
from typing import Tuple

from ..nodes.base_node import TextureResult
from ..core.format import BBox, ImageFormat
from ..core.color_space import ColorSpace



def _build_transform_pixel_matrix(
    tx_px: float,
    ty_px: float,
    sx: float,
    sy: float,
    rotation: float,
    piv_px: float,
    piv_py: float,
) -> Tuple[float, float, float, float, float, float]:
    """Build the forward transform in image pixel space.

    Pixel space is top-left origin / Y-down, matching Transform node UI
    parameters: positive ``translate_y`` moves the image down on screen.
    Matrix layout matches shader uniforms:
    ``x' = m00*x + m01*y + m02``, ``y' = m10*x + m11*y + m12``.
    """
    angle = math.radians(rotation)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    m00 = cos_a * sx
    m01 = -sin_a * sy
    m10 = sin_a * sx
    m11 = cos_a * sy
    m02 = piv_px - m00 * piv_px - m01 * piv_py + tx_px
    m12 = piv_py - m10 * piv_px - m11 * piv_py + ty_px
    return (m00, m10, m01, m11, m02, m12)


def _pixel_matrix_to_uv_matrix(
    pixel_matrix: Tuple[float, float, float, float, float, float],
    source_width: int,
    source_height: int,
    output_width: int | None = None,
    output_height: int | None = None,
) -> Tuple[float, float, float, float, float, float]:
    """Convert a Y-down pixel affine matrix to the shader's UV affine matrix.

    The transform shader works in texture UV coordinates, where Y increases
    upward from the compositing/image point of view. Directly dividing pixel
    terms by width/height makes ``translate_y`` move in the wrong direction
    and rotates non-square images in a normalized square space. This conversion
    preserves pixel-space geometry, including the aspect-ratio cross terms.

    ``source_*`` and ``output_*`` are independent because a bbox-following
    Transform writes the source into a packed output FBO.
    """
    src_w = float(max(1, source_width))
    src_h = float(max(1, source_height))
    out_w = float(max(1, output_width if output_width is not None else source_width))
    out_h = float(max(1, output_height if output_height is not None else source_height))
    m00, m10, m01, m11, m02, m12 = pixel_matrix

    uv_m00 = m00 * src_w / out_w
    uv_m01 = -m01 * src_h / out_w
    uv_m10 = -m10 * src_w / out_h
    uv_m11 = m11 * src_h / out_h
    uv_m02 = (m01 * src_h + m02) / out_w
    uv_m12 = 1.0 - (m11 * src_h + m12) / out_h
    return (uv_m00, uv_m10, uv_m01, uv_m11, uv_m02, uv_m12)


def _build_transform_uv_matrix(
    tx_px: float,
    ty_px: float,
    sx: float,
    sy: float,
    rotation: float,
    piv_px: float,
    piv_py: float,
    width: int,
    height: int,
) -> Tuple[float, float, float, float, float, float]:
    """Build the forward transform matrix consumed by ``_TRANSFORM_FRAGMENT``."""
    pixel_matrix = _build_transform_pixel_matrix(
        tx_px, ty_px, sx, sy, rotation, piv_px, piv_py
    )
    return _pixel_matrix_to_uv_matrix(pixel_matrix, width, height, width, height)


class TransformEvaluatorMixin:
    """Mixin providing transform and spatial effect implementations."""

    def _apply_transform(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply transform (translate/scale/rotate) with filter support.

        Parameters are in pixel space. We convert to UV-space matrix for the shader.

        Canvas policy:
        - Non-tile (preview) mode: the output canvas grows to enclose the
          transformed content (params['out_w'/'out_h'/'out_bbox']), so a
          small foreground moved beyond its original frame stays visible on
          a larger merge canvas. See TransformNode.compute_output_bbox.
        - Tile (export) mode: the output canvas stays at the tile size (w, h).
          The tile stitcher assumes every node output equals the tile's
          padded_roi, so expanding a single tile's canvas would desync the
          stitch. Export keeps the legacy fixed-canvas behavior; content
          outside the tile is filled by neighbouring tiles instead.
        """
        prog = self.utility_shaders.get('transform')
        if prog is None:
            import logging
            logging.getLogger(__name__).warning("Transform shader not available, returning input")
            return {"result": input_tex}

        # Tile context: adjust pixel-space params to tile-relative coordinates.
        # In tile mode we MUST keep the canvas at tile size — the stitcher
        # relies on each node output matching the tile's padded_roi.
        tile_ctx = getattr(self, '_tile_context', None)
        use_expanded_canvas = tile_ctx is None

        if use_expanded_canvas:
            # Preview / full-frame mode: grow the canvas to hold the moved
            # content. Fall back to w,h if the node didn't supply a target.
            out_w = int(params.get("out_w", w) or w)
            out_h = int(params.get("out_h", h) or h)
            result_bbox = params.get("out_bbox")
        else:
            # Tile (export) mode: fixed canvas at tile size.
            out_w, out_h = w, h
            result_bbox = None

        out_tex = self._resource_pool.acquire_texture(out_w, out_h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, out_w, out_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0

        tx_px, ty_px = params.get("translate", [0, 0])
        sx, sy = params.get("scale", [1, 1])
        rotation = params.get("rotation", 0.0)
        piv_px, piv_py = params.get("pivot", [0, 0])

        if tile_ctx is not None:
            origin_x = tile_ctx.padded_roi.x
            origin_y = tile_ctx.padded_roi.y
            tx_px -= origin_x
            ty_px -= origin_y
            piv_px -= origin_x
            piv_py -= origin_y

        # ``result_bbox`` is expressed in composition coordinates while the
        # FBO is packed at local (0, 0). Rebase the forward transform before
        # converting it to UV. Without this, a scaled foreground is drawn at
        # its world offset inside the small FBO and gets clipped.
        pixel_matrix = _build_transform_pixel_matrix(
            tx_px, ty_px, sx, sy, rotation, piv_px, piv_py
        )
        if use_expanded_canvas and result_bbox is not None:
            m00_px, m10_px, m01_px, m11_px, m02_px, m12_px = pixel_matrix
            in_bbox = input_tex.effective_bbox()
            pixel_matrix = (
                m00_px,
                m10_px,
                m01_px,
                m11_px,
                m02_px + float(in_bbox.x - result_bbox.x),
                m12_px + float(result_bbox.top - in_bbox.top),
            )
        m00, m10, m01, m11, m02, m12 = _pixel_matrix_to_uv_matrix(
            pixel_matrix,
            input_tex.width,
            input_tex.height,
            out_w,
            out_h,
        )

        prog['u_m00'].value = m00
        prog['u_m10'].value = m10
        prog['u_m01'].value = m01
        prog['u_m11'].value = m11
        prog['u_m02'].value = m02
        prog['u_m12'].value = m12
        prog['u_filter'].value = params.get("filter", 0)

        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f"Transform: uv_matrix=({m00:.3f},{m10:.3f},{m01:.3f},{m11:.3f},{m02:.3f},{m12:.3f}) "
                     f"in_tex={input_tex.texture_id} out_tex={out_tex.glo} size={out_w}x{out_h} "
                     f"expanded={use_expanded_canvas}")

        self._render_quad(prog)

        # Resolve the result bbox:
        # - Expanded-canvas (preview): use the moved bbox from params so
        #   downstream nodes (e.g. merge's sampleLayer) place the content at
        #   the correct canvas position.
        # - Tile (export): inherit the input bbox (fixed-canvas semantics).
        in_bbox = input_tex.effective_bbox()
        if use_expanded_canvas and result_bbox is not None:
            final_bbox = result_bbox
        else:
            final_bbox = in_bbox

        # A packed Transform result still belongs to the input's full canvas.
        # Supply that canvas even for legacy sources that did not attach an
        # ImageFormat, so a direct Transform -> Output connection can place the
        # data window back into a stable frame instead of resizing every drag.
        result_format = input_tex.format or ImageFormat(
            width=input_tex.width,
            height=input_tex.height,
            name="Transform Input",
        )

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=out_w, height=out_h,
                format=result_format,
                bbox=final_bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_cornerpin(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply perspective transform via four corner control points.

        Uses homography matrix for perspective warp.
        """
        prog = self.utility_shaders.get('cornerpin')
        if prog is None:
            return {"result": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0
        prog['u_resolution'].value = (float(max(1, w)), float(max(1, h)))

        homography = params.get("homography", (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0))
        h00, h01, h02, h10, h11, h12, h20, h21 = homography

        prog['u_h00'].value = h00
        prog['u_h01'].value = h01
        prog['u_h02'].value = h02
        prog['u_h10'].value = h10
        prog['u_h11'].value = h11
        prog['u_h12'].value = h12
        prog['u_h20'].value = h20
        prog['u_h21'].value = h21
        prog['u_interpolation'].value = params.get("interpolation", 0)

        self._render_quad(prog)

        # Fixed-canvas bbox policy (same as _apply_transform, see notes there):
        # the output texture is a fixed canvas; bbox inherits the input bbox.
        in_bbox = input_tex.effective_bbox()

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=in_bbox,  # fixed canvas: bbox unchanged
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_reformat(self, input_tex: TextureResult, params: dict) -> dict:
        """Apply reformat (resize image to target format)."""
        prog = self.utility_shaders.get('reformat')
        if prog is None:
            return {"result": input_tex}

        target_w = max(1, params.get("target_width", input_tex.width))
        target_h = max(1, params.get("target_height", input_tex.height))

        out_tex = self._resource_pool.acquire_texture(target_w, target_h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, target_w, target_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0
        prog['u_src_resolution'].value = (float(input_tex.width), float(input_tex.height))
        prog['u_dst_resolution'].value = (float(target_w), float(target_h))
        prog['u_resize_mode'].value = params.get("resize_mode", 1)
        prog['u_filter'].value = params.get("filter", 0)
        self._render_quad(prog)

        new_fmt = ImageFormat(width=target_w, height=target_h, name="Reformatted")

        if params.get("preserve_bbox", False):
            result_bbox = input_tex.effective_bbox()
        else:
            in_bbox = input_tex.effective_bbox()
            src_w, src_h = float(input_tex.width), float(input_tex.height)
            resize_mode = params.get("resize_mode", 1)

            if resize_mode == 3:
                scale_x = target_w / src_w
                scale_y = target_h / src_h
                result_bbox = BBox(
                    x=int(in_bbox.x * scale_x),
                    y=int(in_bbox.y * scale_y),
                    width=max(1, int(in_bbox.width * scale_x)),
                    height=max(1, int(in_bbox.height * scale_y)),
                )
            elif resize_mode in (1, 2, 4):
                if resize_mode in (2, 4):
                    scale = max(target_w / src_w, target_h / src_h)
                else:
                    scale = min(target_w / src_w, target_h / src_h)
                scaled_w = src_w * scale
                scaled_h = src_h * scale
                offset_x = (target_w - scaled_w) * 0.5
                offset_y = (target_h - scaled_h) * 0.5
                result_bbox = BBox(
                    x=int(in_bbox.x * scale + offset_x),
                    y=int(in_bbox.y * scale + offset_y),
                    width=max(1, int(in_bbox.width * scale)),
                    height=max(1, int(in_bbox.height * scale)),
                )
                result_bbox = result_bbox.clamped(new_fmt)
            else:
                result_bbox = in_bbox.clamped(new_fmt)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=target_w, height=target_h,
                format=new_fmt,
                bbox=result_bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_crop(self, input_tex: TextureResult, params: dict) -> dict:
        """Apply crop (extract region, optionally reformat)."""
        prog = self.utility_shaders.get('crop')
        if prog is None:
            return {"result": input_tex}

        crop_x = params.get("crop_x", 0)
        crop_y = params.get("crop_y", 0)
        crop_w = max(1, params.get("crop_w", 512))
        crop_h = max(1, params.get("crop_h", 512))

        out_tex = self._resource_pool.acquire_texture(crop_w, crop_h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, crop_w, crop_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0
        prog['u_src_resolution'].value = (float(input_tex.width), float(input_tex.height))
        prog['u_crop_rect'].value = (float(crop_x), float(crop_y), float(crop_w), float(crop_h))
        prog['u_outside'].value = params.get("outside", 1)
        self._render_quad(prog)

        new_fmt = ImageFormat(width=crop_w, height=crop_h, name="Cropped")
        result_bbox = BBox.full_frame(crop_w, crop_h)

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=crop_w, height=crop_h,
                format=new_fmt,
                bbox=result_bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=input_tex.color_space,
            )
        }

    def _apply_cs_transform(self, input_tex: TextureResult, params: dict, w: int, h: int) -> dict:
        """Apply color space transform (sRGB ↔ Linear)."""
        prog = self.utility_shaders.get('cs_transform')
        if prog is None:
            return {"image": input_tex}

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0
        prog['u_direction'].value = params.get("direction", 0)
        try:
            prog['u_input_premultiplied'].value = bool(input_tex.alpha_premultiplied)
        except (KeyError, AttributeError):
            pass
        # u_transfer: 0 = sRGB, 1 = Rec.709. The CSTransform node currently
        # only exposes sRGB modes, so it defaults to sRGB (0). The IDT sets
        # u_transfer directly via _convert_to_linear for video sources.
        try:
            prog['u_transfer'].value = params.get("transfer", 0)
        except (KeyError, AttributeError):
            pass
        # The CSTransform node does not model primaries (gamut); pin the
        # uniform to BT.709 (no-op) so it never picks up a stale value from
        # a previous IDT pass on the same shader program.
        try:
            prog['u_primaries'].value = 0
        except (KeyError, AttributeError):
            pass
        self._render_quad(prog)

        return {
            "image": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=input_tex.alpha_premultiplied,
                color_space=params.get("target_color_space", ColorSpace.LINEAR),
            )
        }
