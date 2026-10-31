"""
File Output Node - Write rendered result to a file on disk.

Terminal node that reads back the upstream GPU texture and saves it
as PNG, TIFF, EXR, or JPEG.

Supported parameters:
- file_path:  output file path (the extension is forced to match *format*)
- format:     image container (PNG / JPEG / TIFF / EXR)
- bit_depth:  pixel depth per channel (8 / 16 / 32)
- color_space: output color space (auto / sRGB / linear)
- channels:   channel count (auto / RGB / RGBA)
- compression: compression level (format-dependent)
- quality:    JPEG / WebP quality (1-100, only relevant for lossy formats)

The actual file write only happens when render_mode == "export".
During "preview" mode the node simply passes its input through as
"display" so the compositor stays responsive.
"""

import os
import logging
from typing import Any, Dict, Optional, Tuple

import cv2
import moderngl
import numpy as np

from .base_node import Node, Port, TextureResult, ParamDef
from ..render3d.aov import AOV_EXPORTABLE_PASSES

logger = logging.getLogger(__name__)


class FileOutputNode(Node):
    """Write rendered image to a file on disk."""

    HELP_TEXT = """将渲染结果写入磁盘文件。支持 PNG、JPEG、TIFF、EXR 格式。
输入：A（图像输入）
输出：无（写入文件）
参数：
  - file_path: 输出文件路径
  - format: 文件格式（png/jpeg/tiff/exr）
  - bit_depth: 位深（8/16/32）
  - color_space: 色彩空间（srgb/linear）
  - compression: 压缩质量（0-100）
注意：EXR 格式支持 16/32 位浮点，适合 HDR 工作流。PNG 仅支持 8/16 位整数。"""
    DISPLAY_NAME = "File Output"

    node_type = "file_output"
    # CP-044: 渲染汇聚节点不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "File Output"
    header_color = "#2d6c3c"

    # FileOutput is a real terminal node. Its disk side effect is limited to
    # export mode; preview mode remains a zero-copy display passthrough.
    IS_PLACEHOLDER = False

    # Metadata
    SEARCH_ALIASES = ["write", "save", "export", "file", "输出文件", "保存", "导出", "写入"]
    DESCRIPTION = "将渲染结果保存到磁盘文件"
    TAGS = ["output", "file", "export"]

    # Extension map — used to normalise the file_path extension
    _EXT_MAP = {
        "PNG":  ".png",
        "JPEG": ".jpg",
        "TIFF": ".tif",
        "EXR":  ".exr",
    }

    # AUD-6711: encodable bit depths per container. Combinations outside
    # this table (JPEG 16/32-bit, PNG 32-bit) fail inside cv2.imwrite with
    # an opaque False — they are rejected up-front instead. EXR accepts any
    # bit_depth because the write path always encodes float32 (the parameter
    # only documents the intended precision).
    _BIT_DEPTHS = {
        "PNG":  ("8", "16"),
        "JPEG": ("8",),
        "TIFF": ("8", "16", "32"),
        "EXR":  ("8", "16", "32"),
    }

    PARAM_DEFS = [
        ParamDef("file_path", "str", ""),
        ParamDef("format", "option", "PNG",
                 labels=["PNG", "JPEG", "TIFF", "EXR"]),
        ParamDef("bit_depth", "option", "8",
                 labels=["8", "16", "32"]),
        ParamDef("color_space", "option", "auto",
                 labels=["auto", "sRGB", "linear"]),
        ParamDef("channels", "option", "auto",
                 labels=["auto", "RGB", "RGBA"]),
        ParamDef("alpha_mode", "option", "auto",
                 labels=["auto", "premult", "straight"]),
        ParamDef(
            "aov_pass", "option", AOV_EXPORTABLE_PASSES[0].value,
            labels=[pass_type.value for pass_type in AOV_EXPORTABLE_PASSES],
        ),
        ParamDef("compression", "option", "default",
                 labels=["default", "none", "low", "medium", "high",
                         # CP-006: 完整 EXR 压缩枚举（OpenEXR 绑定可用时直写
                         # 绑定路径，规避 cv2 轮子 libdeflate SIGILL；不可用
                         # 时这几档回落 PXR24(cv2 medium)。
                         "zip", "zips", "piz", "pxr24", "dwaa", "dwab"]),
        ParamDef("quality", "int", 95, min_val=1, max_val=100),
        # CP-068: EXR 多层写出 —— beauty 为 part 0，每个可用 AOV 一个 part；
        # OpenEXR 绑定不可用时自动回落单层 cv2 写出。
        ParamDef("exr_multipart", "bool", False),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = []  # Terminal node
        self.parameters = {
            "file_path": "",
            "format": "PNG",
            "bit_depth": "8",
            "color_space": "auto",
            "channels": "auto",
            "alpha_mode": "auto",
            "aov_pass": AOV_EXPORTABLE_PASSES[0].value,
            "compression": "default",
            "quality": 95,
        }

        # Cached passthrough shader (for preview display)
        self._passthrough_prog = None

        # Last write status (for UI feedback)
        self.last_write_success: Optional[bool] = None
        self.last_write_message: str = ""
        # AUD-6711: last encoder-level failure reason (set by _save_to_disk
        # so the UI message explains WHY the write failed).
        self._last_save_error: str = ""

    # ── Evaluation ──

    @staticmethod
    def _select_render_pass(src: TextureResult,
                            requested_pass: str) -> Optional[TextureResult]:
        """Select an auxiliary pass for export while keeping beauty default."""
        if requested_pass in ("", "beauty"):
            return src
        texture_id = int(src.aov_texture_ids.get(requested_pass, 0))
        if texture_id <= 0:
            return None
        width, height = src.aov_dimensions.get(
            requested_pass, (src.width, src.height))
        return src.with_updates(
            texture_id=texture_id,
            width=int(width),
            height=int(height),
            color_space="data",
            alpha_premultiplied=False,
            aov_semantic=str(requested_pass),
            depth_texture_id=None,
            has_deep_data=False,
            aov_texture_ids={},
            aov_dimensions={},
        )

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if not image_input or not image_input.is_valid:
            return {}

        # In preview mode just pass through as "display"
        if render_mode != "export":
            return {"display": image_input}

        # Export mode: read back GPU texture and save to disk
        file_path = self.parameters.get("file_path", "")
        if not file_path:
            self.last_write_success = False
            self.last_write_message = "No file path specified"
            return {"display": image_input}

        # AUD-6711: fail fast on impossible format × bit-depth × channels
        # combinations (e.g. JPEG+RGBA, JPEG 16/32-bit, PNG 32-bit) — these
        # used to surface as an opaque cv2.imwrite False after the full GPU
        # readback had already been paid for.
        fmt = str(self.parameters.get("format", "PNG"))
        bit_depth = str(self.parameters.get("bit_depth", "8"))
        channels = str(self.parameters.get("channels", "auto"))
        invalid_reason = self._validate_export_settings(fmt, bit_depth,
                                                        channels)
        if invalid_reason is not None:
            self.last_write_success = False
            self.last_write_message = invalid_reason
            return {"display": image_input}

        # Normalise extension to match chosen format
        file_path = self._normalise_extension(file_path)

        # CP-068: multi-part EXR (beauty + every available AOV in one file).
        if (str(self.parameters.get("format", "PNG")).upper() == "EXR"
                and self._get_param_bool("exr_multipart", False)):
            return self._write_multipart_exr(image_input, ctx, fb_manager,
                                             resource_pool)

        requested_pass = str(self.parameters.get("aov_pass", "beauty") or "beauty")
        export_input = self._select_render_pass(image_input, requested_pass)
        if export_input is None:
            self.last_write_success = False
            self.last_write_message = (
                f"Requested render pass unavailable: {requested_pass}")
            return {"display": image_input}

        # Read back texture from GPU
        np_image = self._readback_texture(export_input, ctx, fb_manager, resource_pool)
        if np_image is None:
            self.last_write_success = False
            self.last_write_message = "GPU readback failed"
            return {"display": image_input}

        # Apply channel / depth / color-space conversions. Pass the
        # ConfigManager (when available via CompositeEvalContext) so the
        # colour conversion honours the user's composite.color_engine
        # (builtin vs OCIO) instead of always defaulting to OCIO.
        config_manager = None
        try:
            from src.features.composite.core.eval_context import CompositeEvalContext
            if isinstance(ctx, CompositeEvalContext):
                config_manager = ctx.config_manager
        except Exception:
            pass
        np_image = self._prepare_for_export(np_image, export_input,
                                            config_manager=config_manager)

        # Ensure output directory exists
        out_dir = os.path.dirname(file_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        # Write to disk
        success = self._save_to_disk(np_image, file_path)
        if success:
            self.last_write_success = True
            self.last_write_message = f"Saved: {file_path}"
        else:
            self.last_write_success = False
            # AUD-6711: include the encoder's failure reason — a bare
            # "Failed to save" hid whether it was an I/O error, an encoder
            # rejection, or an unsupported pixel combination.
            reason = self._last_save_error or "encoder returned failure"
            self.last_write_message = f"Failed to save: {file_path} ({reason})"

        return {"display": image_input}

    # ── GPU readback ──

    @staticmethod
    def _query_draw_fbo_binding() -> Optional[int]:
        """查询当前绘制 FBO 的原生绑定名；OpenGL 模块不可用时返回 None。"""
        try:
            from OpenGL import GL as gl
            return int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        except Exception:
            return None

    @staticmethod
    def _restore_draw_fbo_binding(name: int) -> None:
        """恢复绘制 FBO 原生绑定；失败只留痕（best-effort）。"""
        try:
            from OpenGL import GL as gl
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, int(name))
        except Exception:
            logger.debug("readback 后恢复 FBO 绑定失败", exc_info=True)

    def _readback_texture(self, src: TextureResult, ctx, fb_manager,
                          resource_pool=None) -> Optional[np.ndarray]:
        """Read a GPU texture back to CPU as RGBA float32 numpy array.

        Returns:
            (h, w, 4) float32 array in [0, 1] range (RGBA), or None.
        """
        w = max(1, src.width)
        h = max(1, src.height)

        readback_tex = None
        readback_fbo = None
        vbo = None
        vao = None
        # AUD-6712：保存调用方 GL 状态（viewport / 绘制 FBO 绑定），
        # readback 过程改绑后在 finally 恢复——不恢复会污染同上下文的
        # 后续绘制（视口裁剪、写入目标错乱）。原生 glGetIntegerv 仅在
        # 渲染线程调用（本节点 evaluate 即在渲染线程，同 gpu_resource_pool
        # 的线程约束）。
        prev_viewport: Optional[Tuple[int, ...]] = tuple(ctx.viewport)
        prev_draw_fbo: Optional[int] = self._query_draw_fbo_binding()

        try:
            # Create temporary texture + FBO
            # AUD-6708: ``moderngl`` must resolve from the module-level
            # import — a function-local import made the name local for the
            # whole function scope, so this first reference raised
            # UnboundLocalError and every export failed with a generic
            # "GPU readback failed".
            readback_tex = ctx.texture((w, h), 4, dtype='f4')
            readback_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            readback_fbo = ctx.framebuffer(color_attachments=[readback_tex])
            readback_fbo.use()
            ctx.viewport = (0, 0, w, h)
            ctx.clear(0.0, 0.0, 0.0, 0.0)

            # Blit source texture → readback FBO
            if self._passthrough_prog is None:
                self._passthrough_prog = ctx.program(
                    vertex_shader=_PASSTHROUGH_VERT,
                    fragment_shader=_PASSTHROUGH_FRAG,
                )
            prog = self._passthrough_prog

            from ..core.gl_bind import bind_texture_unit
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
            vao = None
            vbo.release()
            vbo = None

            # Read pixels
            ctx.finish()
            raw = readback_fbo.read(components=4, dtype='f4')
            arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
            # Flip vertically (OpenGL bottom-left → image top-left)
            return np.flipud(arr).copy()

        except Exception as e:
            logger.error(f"GPU readback error: {e}")
            logger.debug("Stack trace:", exc_info=True)
            return None

        finally:
            # AUD-6709 (attached): a failure between buffer creation and
            # the success-path releases (uniform set, render) previously
            # leaked the VAO/VBO — fold them into the same guarantee as
            # the readback objects.
            for obj in (vao, vbo):
                if obj is not None:
                    try:
                        obj.release()
                    except Exception:
                        pass
            if readback_fbo is not None:
                try:
                    readback_fbo.release()
                except Exception:
                    pass
            if readback_tex is not None:
                try:
                    readback_tex.release()
                except Exception:
                    pass
            # AUD-6712：恢复调用方的 viewport 与绘制 FBO 绑定
            try:
                ctx.viewport = prev_viewport
            except Exception:
                pass
            if prev_draw_fbo is not None:
                self._restore_draw_fbo_binding(prev_draw_fbo)

    # ── Image preparation ──

    def _prepare_for_export(self, rgba: np.ndarray,
                            src: TextureResult,
                            config_manager: object | None = None) -> np.ndarray:
        """Convert RGBA float32 image to the target format/depth/channels.

        Args:
            rgba: (h, w, 4) float32 RGBA in [0, 1]
            src:  original TextureResult (for metadata)
            config_manager: ConfigManager for colour-engine selection (builtin
                vs OCIO). None → OCIO default.

        Returns:
            numpy array ready for cv2.imwrite (BGR or BGRA).
        """
        fmt = self.parameters.get("format", "PNG")
        target_cs = self.parameters.get("color_space", "auto")
        channels = self.parameters.get("channels", "auto")
        bit_depth = self.parameters.get("bit_depth", "8")

        # Determine source color space
        src_cs = src.color_space or "linear"

        # Resolve "auto" color_space
        if target_cs == "auto":
            # EXR → linear; everything else → sRGB
            target_cs = "linear" if fmt == "EXR" else "sRGB"

        # AOVs are numeric data. Their values must remain raw even when the
        # selected container is PNG/TIFF; applying an OETF would corrupt depth,
        # ID and motion-vector semantics.
        is_data_pass = str(getattr(src, "aov_semantic", "beauty")) != "beauty"

        alpha_mode = str(self.parameters.get("alpha_mode", "auto"))
        source_premult = src.alpha_premultiplied is True
        target_premult = (source_premult if alpha_mode == "auto"
                          else alpha_mode == "premult")
        if rgba.ndim == 3 and rgba.shape[-1] >= 4 and source_premult:
            # Colour transforms are defined on straight RGB. Preserve the
            # requested output alpha representation after conversion.
            alpha = rgba[..., 3:4]
            straight = rgba.copy()
            straight[..., :3] = np.where(
                alpha > 0.0001, straight[..., :3] / np.maximum(alpha, 0.0001), 0.0)
            rgba = straight

        # Apply colour-space conversion via the active provider (built-in
        # sRGB↔linear math by default; OCIO when the user has enabled it).
        # This replaces the inline numpy sRGB EOTF that used to live here;
        # the provider also fixed a bug in the legacy sRGB→linear formula
        # (missing +0.055), so exports now match the live preview's IDT/ODT.
        if not is_data_pass:
            from src.shared.color.color_transform_provider import get_color_provider
            provider = get_color_provider(config_manager)
            rgba = provider.export_encode(rgba, src_cs, target_cs)

        if rgba.ndim == 3 and rgba.shape[-1] >= 4 and target_premult:
            rgba[..., :3] *= rgba[..., 3:4]

        # AUD-6710: clamp only when the target encoding is integer-valued.
        # Float encodings (EXR, and TIFF written as 32-bit float — see the
        # astype(np.float32) branch below) must keep HDR values above 1.0;
        # the previous blanket "everything but EXR" clip destroyed specular
        # highlights on TIFF-32f exports. Integer depths (8/16-bit) are
        # normalized by definition, so they still clamp.
        float_output = (fmt == "EXR") or (bit_depth == "32")
        if not float_output:
            rgba = np.clip(rgba, 0.0, 1.0)

        # Determine channels
        if channels == "auto":
            # Keep alpha for PNG/TIFF/EXR; drop for JPEG
            keep_alpha = (fmt != "JPEG")
        elif channels == "RGBA":
            keep_alpha = True
        else:  # RGB
            keep_alpha = False

        if keep_alpha:
            # RGBA → BGRA for cv2
            bgr = rgba[:, :, :3]
            alpha = rgba[:, :, 3:4]
            out = np.concatenate([
                bgr[:, :, ::-1],  # RGB→BGR
                alpha,
            ], axis=2)
        else:
            out = rgba[:, :, :3][:, :, ::-1]  # RGB→BGR

        # Apply bit depth
        if fmt == "EXR":
            # EXR is always float32
            out = out.astype(np.float32)
        elif bit_depth == "32":
            out = out.astype(np.float32)
        elif bit_depth == "16":
            out = (out * 65535).astype(np.uint16)
        else:  # 8
            out = (out * 255).astype(np.uint8)

        return out

    # ── Disk write ──

    def _write_multipart_exr(self, image_input: TextureResult, ctx: Any,
                             fb_manager: Any, resource_pool: Any) -> Dict[str, TextureResult]:
        """CP-068: write beauty + all AOV passes as one multi-part EXR.

        Falls back to the caller's single-pass path (returning an empty
        dict here would silently skip the save) when the OpenEXR bindings
        are unavailable or the multipart write fails.
        """
        from ..core.exr_multipart_writer import write_exr_multipart

        beauty = self._readback_texture(image_input, ctx, fb_manager,
                                        resource_pool)
        if beauty is None:
            self.last_write_success = False
            self.last_write_message = "GPU readback failed"
            return {"display": image_input}

        # Beauty readback comes back BGR (cv2 convention) — check the
        # single-pass path's prepare step for the authoritative order and
        # reuse RGBA ordering here.
        beauty = self._prepare_for_export(beauty, image_input)

        aov_arrays: Dict[str, Any] = {}
        for pass_name in list(image_input.aov_texture_ids.keys()):
            tex_id = int(image_input.aov_texture_ids.get(pass_name, 0))
            if tex_id <= 0:
                continue
            w, h = image_input.aov_dimensions.get(
                pass_name, (image_input.width, image_input.height))
            aov_input = image_input.with_updates(
                texture_id=tex_id, width=int(w), height=int(h),
                color_space="data", alpha_premultiplied=False,
                aov_semantic=str(pass_name), depth_texture_id=None,
                has_deep_data=False, aov_texture_ids={}, aov_dimensions={})
            arr = self._readback_texture(aov_input, ctx, fb_manager,
                                         resource_pool)
            if arr is None:
                continue
            # Data AOVs bypass the display transform (same rule as the
            # single-pass export path).
            if arr.ndim == 3 and arr.shape[2] >= 3:
                aov_arrays[str(pass_name)] = arr[:, :, :3].astype(np.float32)
            elif arr.ndim == 3 and arr.shape[2] == 1:
                aov_arrays[str(pass_name)] = arr[:, :, 0].astype(np.float32)
            elif arr.ndim == 2:
                aov_arrays[str(pass_name)] = arr.astype(np.float32)

        file_path = self._normalise_extension(
            self.parameters.get("file_path", ""))
        out_dir = os.path.dirname(file_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        compression = self.parameters.get("compression", "default")
        comp_name = {"default": "PIZ", "none": "NONE", "low": "ZIPS",
                     "medium": "PIZ", "high": "DWAB"}.get(compression, "PIZ")
        # _prepare_for_export 输出 BGR(A)——writer 期望 RGB(A)，仅换前3通道
        beauty_rgba = np.asarray(beauty, dtype=np.float32)
        if beauty_rgba.ndim == 3 and beauty_rgba.shape[2] == 4:
            beauty_rgba = np.ascontiguousarray(np.concatenate(
                [beauty_rgba[:, :, 2:3], beauty_rgba[:, :, 1:2],
                 beauty_rgba[:, :, 0:1], beauty_rgba[:, :, 3:4]], axis=2))
        elif beauty_rgba.ndim == 3 and beauty_rgba.shape[2] == 3:
            beauty_rgba = np.ascontiguousarray(beauty_rgba[:, :, ::-1])
        ok = write_exr_multipart(
            file_path, beauty_rgba, aov_arrays, compression=comp_name)
        if ok:
            self.last_write_success = True
            passes = ", ".join(["beauty"] + sorted(aov_arrays))
            self.last_write_message = (
                f"Saved multi-part EXR ({passes}): {file_path}")
            self._last_save_error = ""
        else:
            self.last_write_success = False
            self._last_save_error = "multipart EXR writer unavailable/failed"
            self.last_write_message = (
                f"Failed multipart EXR, falling back: {file_path}")
        return {"display": image_input}

    def _save_to_disk(self, image: np.ndarray, path: str) -> bool:
        """Write the numpy image to disk using cv2.imwrite.

        Args:
            image: BGR/BGRA numpy array (uint8 / uint16 / float32).
            path:  output file path with correct extension.

        Returns:
            True on success.
        """
        fmt = self.parameters.get("format", "PNG")
        compression = self.parameters.get("compression", "default")
        quality = int(self.parameters.get("quality", 95))
        self._last_save_error = ""

        def _write(params: list, img: Optional[np.ndarray] = None) -> bool:
            """imwrite wrapper that records why an encode failed."""
            if img is None:
                img = image
            ok = cv2.imwrite(path, img, params)
            if not ok:
                self._last_save_error = (
                    f"cv2.imwrite rejected {fmt} output "
                    f"(dtype={img.dtype}, shape={img.shape})")
            return ok

        try:
            if fmt == "EXR":
                # CP-006: 完整压缩枚举。OpenEXR 绑定可用时，zip/zips/piz/
                # pxr24/dwaa/dwab 直接走绑定写出的单 part 路径（绕开 cv2
                # 轮子 libdeflate 的 SIGILL 问题，见 T-P1-71）；绑定缺失时
                # 这些档位回落 cv2 支持的安全档。
                _BINDING_COMPS = {"zip", "zips", "piz", "pxr24", "dwaa", "dwab"}
                if compression in _BINDING_COMPS:
                    from ..core.exr_multipart_writer import write_exr_multipart
                    comp_attr = {"zip": "ZIP", "zips": "ZIPS", "piz": "PIZ",
                                 "pxr24": "PXR24", "dwaa": "DWAA",
                                 "dwab": "DWAB"}[compression]
                    # _prepare_for_export 输出 BGR(A)（cv2 约定）——
                    # 绑定写出前转回 RGB。
                    rgba = image.astype(np.float32)
                    if rgba.ndim == 2:
                        rgba = np.stack([rgba] * 3
                                        + [np.ones_like(rgba)], axis=-1)
                    elif rgba.shape[2] == 1:
                        rgba = np.concatenate(
                            [rgba] * 3
                            + [np.ones(rgba.shape[:2] + (1,), np.float32)],
                            axis=2)
                    elif rgba.shape[2] == 3:
                        # BGR → RGB（仅前3通道；全数组反转会把 alpha 挪到首位）
                        rgba = np.ascontiguousarray(rgba[:, :, ::-1])
                    elif rgba.shape[2] == 4:
                        # BGRA → RGBA（alpha 保持末位）
                        rgba = np.ascontiguousarray(np.concatenate(
                            [rgba[:, :, 2:3], rgba[:, :, 1:2],
                             rgba[:, :, 0:1], rgba[:, :, 3:4]], axis=2))
                    if write_exr_multipart(path, rgba, {},
                                           compression=comp_attr):
                        return True
                    compression = "medium"  # cv2 fallback below
                # cv2 路径：EXR Compression 枚举原值：
                #   none=0, rle=1, zips=2, zip=3, piz=4,
                #   pxr24=5, b44=6, b44a=7, dwaa=8, dwab=9
                comp_map = {
                    "none": 0, "low": 1, "medium": 5, "high": 9, "default": 5,
                }
                comp_val = comp_map.get(compression, 5)
                params = [
                    cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_FLOAT,
                    cv2.IMWRITE_EXR_COMPRESSION, comp_val,
                ]
                return _write(params, image.astype(np.float32))

            if fmt == "TIFF":
                comp_map = {"none": 1, "low": 2, "medium": 5, "high": 8, "default": 5}
                comp_val = comp_map.get(compression, 5)
                params = [cv2.IMWRITE_TIFF_COMPRESSION, comp_val]
                return _write(params)

            if fmt == "JPEG":
                params = [cv2.IMWRITE_JPEG_QUALITY, quality]
                return _write(params)

            if fmt == "PNG":
                # Map compression to PNG level (0-9).
                # Higher number = smaller file but slower.
                comp_map = {
                    "none": 0, "low": 1, "medium": 5, "high": 9, "default": 3,
                }
                level = comp_map.get(compression, 3)
                params = [cv2.IMWRITE_PNG_COMPRESSION, level]
                return _write(params)

            # Unknown format — try imwrite with no params
            return _write([])

        except Exception as e:
            self._last_save_error = str(e)
            logger.error(f"Save error: {e}")
            logger.debug("Stack trace:", exc_info=True)
            return False

    # ── Helpers ──

    @classmethod
    def _validate_export_settings(cls, fmt: str, bit_depth: str,
                                  channels: str) -> Optional[str]:
        """AUD-6711: pre-flight the format × bit-depth × channels request.

        Returns a human-readable error string for combinations the encoders
        cannot write, or None when the combination is encodable. Called
        before the GPU readback so misconfigured exports cost nothing.
        """
        allowed = cls._BIT_DEPTHS.get(fmt)
        if allowed is None:
            return f"Unsupported format: {fmt}"
        if bit_depth not in allowed:
            depths = "/".join(allowed)
            return (f"{fmt} does not support {bit_depth}-bit output "
                    f"(supported: {depths}-bit)")
        # OpenCV's JPEG encoder only accepts 1/3-channel buffers; an
        # explicit RGBA request would fail inside imwrite.
        if fmt == "JPEG" and channels == "RGBA":
            return ("JPEG cannot store an alpha channel; choose RGB "
                    "channels or a PNG/TIFF/EXR container")
        return None

    def _normalise_extension(self, path: str) -> str:
        """Ensure the file extension matches the chosen format."""
        fmt = self.parameters.get("format", "PNG")
        target_ext = self._EXT_MAP.get(fmt, ".png")
        base, ext = os.path.splitext(path)
        if ext.lower() != target_ext:
            path = base + target_ext
        return path

    def cleanup(self) -> None:
        """Release GPU resources."""
        if self._passthrough_prog is not None:
            self._passthrough_prog.release()
            self._passthrough_prog = None


# Minimal passthrough shaders (blit texture to FBO)
_PASSTHROUGH_VERT = """
#version 410 core
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_uv = in_uv;
}
"""

_PASSTHROUGH_FRAG = """
#version 410 core
uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 frag_color;
void main() {
    frag_color = texture(u_texture, v_uv);
}
"""
