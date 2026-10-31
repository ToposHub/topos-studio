"""Frame Interpolate node (C2) — real motion in-betweening via RIFE.

Two image inputs (``frame_a`` / ``frame_b``) and an animatable ``t``
(0=A, 1=B); the output is the synthesized in-between frame.  Inference
runs through the shared :class:`RifeInterpolator` (ONNX session resolved
via model_manager, stub-injectable for tests); without a model the
executor downgrades to a linear blend and the node reports the quality
tier through ``used_fallback``.

Pair with time remapping for speed changes: animate ``t`` from the
retime fraction and feed the two neighbouring source frames.
"""

import logging
from typing import Any, Dict, Optional

import numpy as np

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

logger = logging.getLogger(__name__)


class FrameInterpolateNode(Node):
    """RIFE-based motion interpolation between two frames."""

    node_type = "frame_interpolate"
    node_title = "Frame Interpolate"
    i18n_title_key = "nodes.frame_interpolate.title"
    header_color = "#9B59B6"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "frame interpolate", "rife", "optical flow interpolation",
        "inbetween", "帧插值", "补帧", "光流补帧",
    ]
    DESCRIPTION = "RIFE 真运动补帧（区别于线性混合）；无模型时线性降级并提示"
    TAGS = ["ai", "temporal", "interpolation"]

    PARAM_DEFS = [
        ParamDef("t", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.001, group="Interpolate", animated=True,
                 expression_hint="t"),
        ParamDef("hd_mode", "bool", False, group="Interpolate"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("frame_a", "input", "image"),
            Port("frame_b", "input", "image"),
        ]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "t": 0.5,
            "hd_mode": False,
        }
        # Quality tier of the last evaluation (UI state).
        self.used_fallback = False
        # Lazily created shared executor (holds the cached ONNX session).
        self._interpolator = None
        # AUD-6802: bounded readback cache for the RIFE model path.  The
        # model path needs CPU pixels for ONNX; consecutive evaluations
        # (animating t 0→1 over the same neighbouring frames) re-read the
        # SAME full frames.  Keyed by texture identity
        # (texture_id, width, height, revision) so upstream content changes
        # invalidate naturally.  Capped at the 2 most recent frames — the
        # current a/b pair, which are the likely neighbours of the next
        # evaluation (~2 × full-frame RGBA32F at most, e.g. ~264MB at 4K).
        # Render-thread-only (evaluation is single-threaded per node); the
        # readback itself happens on the render thread via _read_input.
        self._readback_cache: Dict[tuple, "np.ndarray"] = {}
        # AUD-6800: persistent, node-owned output texture. Both output paths
        # (GPU blend fallback and RIFE upload) render/write into this single
        # texture; it is recreated only on size / GL-context change (the old
        # one is released at that point — deferred release) and freed by
        # cleanup(). Previously every evaluation allocated a fresh untracked
        # gl.texture() that no release path could ever reclaim.
        self._out_texture: Optional[Any] = None
        self._out_texture_pooled: bool = False  # acquired from resource_pool?
        self._out_texture_pool: Optional[Any] = None  # pool it must return to
        self._out_gl_ctx: Optional[Any] = None
        # AUD-6801: blend program / quad VBO (per GL context, see
        # _ensure_blend_resources) — released by cleanup().
        self._blend_prog: Optional[Any] = None
        self._blend_ctx: Optional[Any] = None
        self._blend_vbo: Optional[Any] = None
        self._blend_vbo_ctx: Optional[Any] = None

    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, Any]) -> bool:
        # ``t`` is animatable — each frame is its own cache dimension.
        return True

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("frame_a")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        return {"frame_a": output_roi, "frame_b": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return 0

    # ── GPU glue (defensive, shared helper) ────────────────────────────
    def _read_input(self, tex_result: TextureResult, ctx: Any,
                    fb_manager: Any) -> Optional["np.ndarray"]:
        from ._gpu_readback import read_rgba
        return read_rgba(tex_result, ctx, fb_manager)

    def _read_input_cached(self, tex_result: TextureResult, ctx: Any,
                           fb_manager: Any) -> Optional["np.ndarray"]:
        """Identity-keyed readback cache (AUD-6802).

        The RIFE model path used to re-read the same full frames from the
        GPU on every evaluation while ``t`` animated across one fixed
        neighbour pair (2 × full-frame fbo.read per frame).  Cache keyed
        by (texture_id, width, height, revision): upstream content changes
        bump the revision and invalidate naturally.  Bounded to the two
        most recent frames; on any cache pressure beyond that the oldest
        entry is evicted FIFO.
        """
        key = (int(tex_result.texture_id), int(tex_result.width),
               int(tex_result.height), int(tex_result.revision))
        cached = self._readback_cache.get(key)
        if cached is not None:
            # Refresh recency (dicts keep insertion order).
            try:
                del self._readback_cache[key]
                self._readback_cache[key] = cached
            except KeyError:
                pass
            return cached
        pixels = self._read_input(tex_result, ctx, fb_manager)
        if pixels is None:
            return None
        self._readback_cache[key] = pixels
        while len(self._readback_cache) > 2:
            self._readback_cache.pop(next(iter(self._readback_cache)))
        return pixels

    # P2-9: dual-texture GPU linear blend for the no-model fallback —
    # replaces the "read both frames back + numpy mix + upload" chain
    # (352ms/1080p, ~2.5s/4K) with a single full-frame draw.
    _BLEND_FALLBACK_FRAGMENT = """
    #version 330
    uniform sampler2D u_tex_a;
    uniform sampler2D u_tex_b;
    uniform float u_t;
    in vec2 v_uv;
    out vec4 fragColor;
    void main() {
        fragColor = mix(texture(u_tex_a, v_uv), texture(u_tex_b, v_uv), u_t);
    }
    """

    _QUAD_VERTICES = np.array([
        -1.0, -1.0, 0.0, 0.0,
         1.0, -1.0, 1.0, 0.0,
        -1.0,  1.0, 0.0, 1.0,
         1.0,  1.0, 1.0, 1.0,
    ], dtype='f4')

    def _ensure_blend_resources(self, gl: Any) -> tuple:
        """Lazy-compile the blend program and quad VBO (cached per GL ctx)."""
        if getattr(self, "_blend_prog", None) is None \
                or self._blend_ctx is not gl:
            # AUD-6801: a context switch orphaned the previous ctx's program.
            # Drop it without calling release() — the owning ctx is gone
            # (same policy as deformer3d's ctx-keyed cache).
            self._blend_prog = None
            self._blend_ctx = None
            from src.features.composite.core.shaders import (
                _PASSTHROUGH_VERTEX,
            )
            self._blend_prog = gl.program(
                vertex_shader=_PASSTHROUGH_VERTEX,
                fragment_shader=self._BLEND_FALLBACK_FRAGMENT,
            )
            self._blend_ctx = gl
        if getattr(self, "_blend_vbo", None) is None \
                or self._blend_vbo_ctx is not gl:
            self._blend_vbo = None
            self._blend_vbo_ctx = None
            self._blend_vbo = gl.buffer(self._QUAD_VERTICES)
            self._blend_vbo_ctx = gl
        return self._blend_prog, self._blend_vbo

    # ── AUD-6800: owned output-texture lifecycle ────────────────────────

    def _release_output_texture(self, release: bool = True) -> None:
        """Release (or drop) the owned output texture.

        ``release=False`` only drops the references: used on GL-context
        switches where the previous ctx is already destroyed and calling
        release() on its objects is invalid.
        """
        tex = self._out_texture
        pooled = self._out_texture_pooled
        pool = self._out_texture_pool
        self._out_texture = None
        self._out_texture_pooled = False
        self._out_texture_pool = None
        if tex is None:
            return
        if not release:
            return
        try:
            if pooled and pool is not None:
                pool.release_texture(tex)
            else:
                tex.release()
        except Exception as exc:
            logger.debug("FrameInterpolate output texture release failed: %s",
                         exc)

    def _ensure_output_texture(self, gl: Any, w: int, h: int,
                               resource_pool: Any) -> Optional[Any]:
        """Return the persistent output texture, (re)creating when needed.

        Preference order: reuse the existing texture (same ctx + size),
        then resource_pool acquisition (pooled/tracked), then a node-owned
        gl.texture() fallback that this node itself releases.
        """
        if self._out_texture is not None and self._out_gl_ctx is gl:
            size = (int(self._out_texture.width), int(self._out_texture.height))
            if size == (int(w), int(h)):
                return self._out_texture
        # Size or context changed — deferred release of the old allocation.
        self._release_output_texture(release=(self._out_gl_ctx is gl))
        self._out_gl_ctx = gl
        if resource_pool is not None:
            try:
                tex = resource_pool.acquire_texture(w, h, components=4,
                                                    dtype="f4")
                if tex is not None:
                    self._out_texture = tex
                    self._out_texture_pooled = True
                    self._out_texture_pool = resource_pool
                    return tex
            except Exception as exc:
                logger.debug("FrameInterpolate pool acquire failed: %s", exc)
        self._out_texture = gl.texture((w, h), 4, dtype="f4")
        return self._out_texture

    def _gpu_linear_blend(self, frame_a: TextureResult,
                          frame_b: TextureResult, t: float, ctx: Any,
                          fb_manager: Any,
                          resource_pool: Any) -> Optional[TextureResult]:
        """Blend frame_a→frame_b entirely on the GPU.

        Returns a TextureResult, or None when GL state is unavailable (the
        caller degrades to the nearest endpoint frame).
        """
        try:
            import moderngl
            from src.features.composite.core.gl_bind import bind_texture_unit
            gl = getattr(ctx, "gl", ctx)
            if fb_manager is None:
                return None
            from ._gpu_readback import find_texture
            tex_a = find_texture(frame_a.texture_id, fb_manager,
                                 resource_pool)
            tex_b = find_texture(frame_b.texture_id, fb_manager,
                                 resource_pool)
            if tex_a is None or tex_b is None:
                return None
            w = max(1, int(frame_a.width))
            h = max(1, int(frame_a.height))
            # AUD-6800: render into the persistent, tracked output texture
            # (pool-backed when available) instead of allocating an
            # untracked gl.texture() on every evaluation.
            out_tex = self._ensure_output_texture(gl, w, h, resource_pool)
            if out_tex is None:
                return None
            prog, vbo = self._ensure_blend_resources(gl)
            fbo = fb_manager.get_fbo(out_tex)
            fbo.use()
            gl.viewport = (0, 0, w, h)
            bind_texture_unit(tex_a.glo, 0)
            bind_texture_unit(tex_b.glo, 1)
            prog['u_tex_a'].value = 0
            prog['u_tex_b'].value = 1
            prog['u_t'].value = float(t)
            vao = gl.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
            vao.render(moderngl.TRIANGLE_STRIP)
            vao.release()
            # Unbind both units (P1-6: releasing pooled textures while
            # still bound corrupts later binds with GL 1282).
            bind_texture_unit(0, 0)
            bind_texture_unit(0, 1)
            self.used_fallback = True
            return TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=frame_a.format,
                bbox=frame_a.bbox,
                alpha_premultiplied=frame_a.alpha_premultiplied,
                color_space=frame_a.color_space,
            )
        except Exception as exc:
            logger.warning("FrameInterpolate GPU blend failed: %s", exc)
            return None

    def _upload(self, rgb: "np.ndarray", reference: TextureResult,
                ctx: Any, resource_pool: Any = None) -> Optional[TextureResult]:
        """Upload the RIFE result into the persistent output texture."""
        try:
            gl = getattr(ctx, "gl", ctx)
            h, w = rgb.shape[:2]
            rgba = np.concatenate(
                [rgb.astype(np.float32),
                 np.ones((h, w, 1), dtype=np.float32)], axis=-1)
            data = np.ascontiguousarray(np.flipud(rgba))
            # AUD-6800: write into the tracked persistent texture rather
            # than allocating an untracked one per frame.
            tex = self._ensure_output_texture(gl, w, h, resource_pool)
            if tex is None:
                return None
            tex.write(data.tobytes())
            return TextureResult(
                texture_id=tex.glo, width=w, height=h,
                format=reference.format,
                bbox=reference.bbox,
                alpha_premultiplied=reference.alpha_premultiplied,
                color_space=reference.color_space,
            )
        except Exception as exc:
            logger.warning("FrameInterpolate upload failed: %s", exc)
            return None

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        frame_a = inputs.get("frame_a")
        frame_b = inputs.get("frame_b")
        if frame_a is None or not frame_a.is_valid:
            return {}
        if frame_b is None or not frame_b.is_valid:
            return {"result": frame_a}

        t = min(1.0, max(0.0, float(self.parameters.get("t", 0.5))))
        if t <= 0.0:
            return {"result": frame_a}
        if t >= 1.0:
            return {"result": frame_b}

        from src.shared.ai_models.rife_interpolator import RifeInterpolator
        # Persistent instances keep the (cached) ONNX session across
        # frames; the module-level session cache also dedupes across nodes.
        if self._interpolator is None:
            self._interpolator = RifeInterpolator()
        interpolator = self._interpolator

        # P2-9: without a model, blend on the GPU instead of reading both
        # frames back to CPU and uploading the mixed result (352ms/1080p,
        # ~2.5s/4K → one full-frame draw). The model path below keeps its
        # CPU round-trip (ONNX tensors).
        if not interpolator.has_model():
            self.used_fallback = True
            blended = self._gpu_linear_blend(frame_a, frame_b, t, ctx,
                                             fb_manager, resource_pool)
            if blended is not None:
                return {"result": blended}
            # No GL surface — degrade to the nearest endpoint frame.
            return {"result": frame_a if t < 0.5 else frame_b}

        # AUD-6802: cached readback — an animated t over the same neighbour
        # pair now reads each distinct frame at most once per identity.
        a = self._read_input_cached(frame_a, ctx, fb_manager)
        b = self._read_input_cached(frame_b, ctx, fb_manager)
        if a is None or b is None:
            # No GL surface — degrade to the nearest endpoint frame.
            return {"result": frame_a if t < 0.5 else frame_b}

        rgb = interpolator.interpolate(a[..., :3], b[..., :3], t,
                                       hd_mode=bool(
                                           self.parameters.get("hd_mode",
                                                               False)))
        self.used_fallback = interpolator.used_fallback

        result = self._upload(rgb, frame_a, ctx, resource_pool)
        if result is None:
            return {"result": frame_a if t < 0.5 else frame_b}
        return {"result": result}

    def cleanup(self) -> None:
        """AUD-6801: release GPU state when the node leaves the graph.

        Releases the owned output texture (back to the pool when pooled),
        the blend program / quad VBO, and drops the RIFE executor
        reference. The module-level ONNX session cache intentionally
        survives — it dedupes sessions across nodes.
        """
        self._release_output_texture(release=True)
        self._out_gl_ctx = None
        for attr in ("_blend_prog", "_blend_vbo"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception as exc:
                    logger.debug("FrameInterpolate %s release failed: %s",
                                 attr, exc)
                setattr(self, attr, None)
        self._blend_ctx = None
        self._blend_vbo_ctx = None
        self._interpolator = None
        # AUD-6802: drop cached CPU readbacks — they are only valid while
        # the node is part of the graph with its inputs unchanged.
        self._readback_cache.clear()
