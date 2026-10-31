"""
TileEvaluatorMixin - Tile rendering and LOD methods.

Provides tile rendering and LOD (Level of Detail) optimization:
- Downsample texture (for nodes outside viewport)
- Upsample texture (restore low-res outputs for downstream nodes)
- LOD application to inputs/outputs

Tile rendering enables memory-bounded large image processing by
dividing the image into tiles that fit within GPU memory limits.
LOD reduces resolution for nodes outside the viewport to improve
rendering performance during zoomed-out previews.
"""

from typing import Dict, TYPE_CHECKING

from ..nodes.base_node import TextureResult
from ..core.format import BBox

if TYPE_CHECKING:
    pass


class TileEvaluatorMixin:
    """Mixin providing tile rendering and LOD optimization methods."""

    def _downsample_texture(self, tex: TextureResult, scale: float) -> TextureResult:
        """Downsample a texture to a lower resolution for LOD rendering.

        Uses high-quality 2x2 box filter for proper averaging.
        Called when a node's output is outside the viewport (lod_level > 0).
        """
        src_w, src_h = tex.width, tex.height
        dst_w = max(1, int(src_w * scale))
        dst_h = max(1, int(src_h * scale))

        if dst_w >= src_w or dst_h >= src_h:
            return tex

        prog = self.utility_shaders.get('downsample')
        if prog is None:
            return tex

        dst_tex = self._resource_pool.acquire_texture(dst_w, dst_h)
        dst_fbo = self.fb_manager.get_fbo(dst_tex)
        dst_fbo.use()
        self.ctx.viewport = (0, 0, dst_w, dst_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(tex.texture_id)
        prog['u_texture'].value = 0
        prog['u_src_resolution'].value = (float(src_w), float(src_h))
        prog['u_dst_resolution'].value = (float(dst_w), float(dst_h))
        prog['u_scale'].value = scale
        self._render_quad(prog)

        scaled_bbox = None
        if tex.bbox is not None and not tex.bbox.is_empty:
            scaled_bbox = BBox(
                x=int(tex.bbox.x * scale),
                y=int(tex.bbox.y * scale),
                width=int(tex.bbox.width * scale),
                height=int(tex.bbox.height * scale),
            )

        return TextureResult(
            texture_id=dst_tex.glo, width=dst_w, height=dst_h,
            format=tex.format,
            bbox=scaled_bbox,
            alpha_premultiplied=tex.alpha_premultiplied,
            color_space=tex.color_space,
        )

    def _upsample_texture(self, tex: TextureResult, target_w: int, target_h: int) -> TextureResult:
        """Upsample a texture to a higher resolution.

        Uses bilinear interpolation for smooth upsampling.
        Called when a low-res LOD output needs to be restored for downstream nodes.
        """
        src_w, src_h = tex.width, tex.height

        if target_w <= src_w or target_h <= src_h:
            return tex

        prog = self.utility_shaders.get('upsample')
        if prog is None:
            return tex

        dst_tex = self._resource_pool.acquire_texture(target_w, target_h)
        dst_fbo = self.fb_manager.get_fbo(dst_tex)
        dst_fbo.use()
        self.ctx.viewport = (0, 0, target_w, target_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(tex.texture_id)
        prog['u_texture'].value = 0
        self._render_quad(prog)

        scale = float(target_w) / float(src_w)
        upscaled_bbox = None
        if tex.bbox is not None and not tex.bbox.is_empty:
            upscaled_bbox = BBox(
                x=int(tex.bbox.x * scale),
                y=int(tex.bbox.y * scale),
                width=int(tex.bbox.width * scale),
                height=int(tex.bbox.height * scale),
            )

        return TextureResult(
            texture_id=dst_tex.glo, width=target_w, height=target_h,
            format=tex.format,
            bbox=upscaled_bbox,
            alpha_premultiplied=tex.alpha_premultiplied,
            color_space=tex.color_space,
        )

    def _apply_lod_to_inputs(self, input_textures: Dict[str, TextureResult],
                               lod_level: int) -> Dict[str, TextureResult]:
        """Apply LOD downsample to input textures.

        Args:
            input_textures: Original input textures.
            lod_level: LOD level (0=full, 2=quarter).

        Returns:
            Downsampled input textures if lod_level > 0.
        """
        if lod_level <= 0:
            return input_textures

        scale = 0.25 if lod_level == 2 else 1.0 / (2 ** lod_level)

        lod_inputs = {}
        for port_name, tex in input_textures.items():
            if isinstance(tex, TextureResult) and tex.is_valid:
                lod_inputs[port_name] = self._downsample_texture(tex, scale)
            else:
                lod_inputs[port_name] = tex

        return lod_inputs

    def _apply_lod_to_outputs(self, outputs: dict, lod_level: int,
                                original_w: int, original_h: int) -> dict:
        """Apply LOD upsample to outputs if needed.

        If a node was evaluated at low-res but needs to output full-res
        (for downstream nodes), upsample the results.
        """
        if lod_level <= 0:
            return outputs

        upscaled_outputs = {}
        for port_name, tex in outputs.items():
            if isinstance(tex, TextureResult) and tex.is_valid:
                upscaled_outputs[port_name] = self._upsample_texture(
                    tex, original_w, original_h)
            else:
                upscaled_outputs[port_name] = tex

        return upscaled_outputs