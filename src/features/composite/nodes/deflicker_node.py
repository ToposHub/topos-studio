"""Deflicker node (A4) — temporal luminance stabilisation.

Removes brightness flicker (timelapse, old footage, screen recordings) by
dividing each pixel's luminance by the windowed temporal median luminance
and applying the same gain to RGB (chroma preserved):

    gain = percentile(luma[f-h .. f+h or trailing], p) / luma[f]
    out  = image · gain            (luma_only)
    out  = percentile(rgb stack)   (per_pixel, colour channel-wise)

Architecture (plan A4): the composition graph is pull-based, so the node
keeps a runtime ring buffer of per-frame luminance planes keyed by
ABSOLUTE frame number.  The window is *trailing* — sequential playback and
sequential export fill it naturally; a random jump finds the window
missing and DEGRADES to passthrough with a visible warming message rather
than pretending the window was valid.  The buffer is runtime state only:
it is never serialized, never undone, and a reopened project starts in
warming state by design.

Persistence follows the particle-node precedent: the node is classified
``action_type="source"`` so the evaluator keeps ONE live instance and this
node performs its own evaluation instead of emitting an effect marker.
Since P2-9/P2-11 the default (luma_only) path keeps the CPU percentile but
moves both frame transfers to single channel: a GPU luma-extraction pass
feeds the ring buffer through an R32F readback, and a GPU composite pass
applies the gain (per-pixel R32F map or scalar uniform).  The legacy
full-frame CPU chain remains for the channel-wise RGB percentile mode and
as the fallback for every GPU failure.  The result stays a fresh texture,
so the cache key must include the frame (``params_require_frame_cache``).
"""

import logging
import os
from collections import OrderedDict
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import numpy as np

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

logger = logging.getLogger(__name__)

WINDOWS = [3, 5, 7, 9]
MODES = ["per_pixel", "global_gain"]

# Runtime buffer bookkeeping (never serialized).
_BUFFER_MAX_ENTRIES = 96
# AUD-6605: byte budget for the NON-window history kept for scrub-backs.
# The old entry-only cap let the buffers accumulate to 96 full-resolution
# float32 planes before pruning (luma 1080p ≈ 796MB / 4K ≈ 3.2GB peak;
# per-pixel RGB mode ×3 channels ≈ 2.4GB / 9.6GB).  The causal window
# (<=9 frames) is the algorithm's working set and is never evicted;
# everything outside it is convenience that must fit this budget.
_BUFFER_MAX_BYTES = 256 * 1024 * 1024

# AUD-6603: bounds for the node-owned per-frame output texture registry.
# The node is action_type="source" / owns_color_texture=False, so the
# evaluator's cache never owns or releases its output glos — without this
# registry every evaluated frame leaked one full-resolution RGBA float32
# texture (1080p ≈ 33MB, 4K ≈ 133MB each).  The byte budget dominates at
# large resolutions (4K → ~2 frames); the entry cap dominates at
# small/proxy resolutions.  At least the current and previous frame are
# always kept alive so the evaluator's frame cache stays correct for
# one-frame scrub-backs.
_OUTPUT_RING_MAX_ENTRIES = 8
_OUTPUT_RING_MAX_BYTES = 256 * 1024 * 1024

# P2-9/P2-11: when set, the node skips the GPU luma/gain path and runs the
# legacy full-frame CPU chain (also the A/B comparison surface for tests).
_ENV_FORCE_CPU = "TOPOS_DEFLICKER_CPU"


def compute_reference(luma_stack: List["np.ndarray"],
                      percentile: float) -> "np.ndarray":
    """Per-pixel windowed percentile of the luma planes.

    Semantically identical to ``np.percentile(stacked, p*100, axis=0)``
    but built on partition/sort so the 1080p window stays affordable
    (np.percentile's generic machinery dominates otherwise).
    """
    stacked = np.stack(luma_stack, axis=0)
    n = stacked.shape[0]
    p = min(1.0, max(0.0, float(percentile)))
    if p == 0.5 and n % 2 == 1:
        # Exact median via introselect — no full sort.
        return np.partition(stacked, n // 2, axis=0)[n // 2].astype(
            np.float32)
    ordered = np.sort(stacked, axis=0)
    pos = p * (n - 1)
    lo = int(np.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = np.float32(pos - lo)
    return (ordered[lo] * (1.0 - frac) + ordered[hi] * frac).astype(
        np.float32)


def compute_gain_map(current_luma: "np.ndarray",
                     luma_stack: List["np.ndarray"],
                     percentile: float) -> "np.ndarray":
    """Per-pixel gain = windowed percentile luma / current luma."""
    reference = compute_reference(luma_stack, percentile)
    safe_current = np.maximum(current_luma, 1e-6)
    gain = reference / safe_current
    # Clamp to avoid blowups on near-black pixels.
    return np.clip(gain, 0.0, 8.0).astype(np.float32)


def compute_global_gain(luma_stack: List["np.ndarray"],
                        current_luma: "np.ndarray") -> float:
    """Scalar gain = median of the window's per-frame mean luma / current.

    The analytic surface for the ``global_gain`` mode — shared by the CPU
    chain and the GPU composite uniform so both paths agree bit-for-bit on
    the gain itself.
    """
    median_mean = float(np.median(
        [float(np.mean(l)) for l in luma_stack]))
    current_mean = max(float(np.mean(current_luma)), 1e-6)
    return float(np.clip(median_mean / current_mean, 0.0, 8.0))


def apply_deflicker(image_rgb: "np.ndarray", current_luma: "np.ndarray",
                    luma_stack: List["np.ndarray"], percentile: float,
                    luma_only: bool, mode: str,
                    rgb_stack: Optional[List["np.ndarray"]] = None
                    ) -> "np.ndarray":
    """Pure numpy deflicker — the analytic surface for tests.

    ``rgb_stack`` carries the window's RGB planes for the (non-default)
    ``per_pixel`` + ``luma_only=False`` path; without it that combination
    falls back to the luminance gain so the node never silently no-ops.
    """
    if mode == "global_gain":
        gain = compute_global_gain(luma_stack, current_luma)
        return np.clip(image_rgb * gain, 0.0, None)
    if not luma_only and rgb_stack:
        # Full per-pixel mode: percentile-stack every channel separately.
        out = np.empty_like(image_rgb)
        for c in range(3):
            out[..., c] = compute_reference(
                [plane[..., c] for plane in rgb_stack], percentile)
        return np.clip(out, 0.0, None)
    gain = compute_gain_map(current_luma, luma_stack, percentile)
    return np.clip(image_rgb * gain[..., None], 0.0, None)


def luma_of(rgb: "np.ndarray") -> "np.ndarray":
    """Rec.709 luma plane."""
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1]
            + 0.0722 * rgb[..., 2]).astype(np.float32)


class _OutputEntry(NamedTuple):
    """One node-owned output texture plus the allocator to return it to.

    ``gl`` is the context the texture lives in (identity-compared —
    ``id()`` would be recycled after context teardown); ``resource_pool``
    / ``fb_manager`` are the collaborators captured at creation so the
    retirement path can use the same allocator even when a later
    evaluation runs with different collaborators (e.g. cleanup).
    """

    gl: Any
    tex: Any
    resource_pool: Any
    fb_manager: Any


class DeflickerNode(Node):
    """Temporal luminance stabiliser (trailing window, GPU transfers + CPU percentile)."""

    node_type = "deflicker"
    node_title = "Deflicker"
    i18n_title_key = "nodes.deflicker.title"
    header_color = "#5a8a6a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "deflicker", "de-flicker", "flicker removal", "去闪烁", "稳亮度",
        "去闪",
    ]
    DESCRIPTION = "时域亮度去闪烁（滑动窗口中值，跳帧时降级直通并提示 warming）"
    TAGS = ["filter", "temporal", "restoration"]

    PARAM_DEFS = [
        ParamDef("window", "option", "5", labels=[str(w) for w in WINDOWS],
                 group="Deflicker"),
        ParamDef("percentile", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.01, group="Deflicker", animated=True),
        ParamDef("luma_only", "bool", True, group="Deflicker"),
        ParamDef("mode", "option", "per_pixel", labels=MODES,
                 group="Deflicker"),
    ]

    # ── P2-9/P2-11 GPU-assisted path (default luma_only modes) ──────────
    # The legacy chain costs two full RGBA f4 frame transfers per frame
    # (readback 33MB@1080p/132MB@4K + result upload).  The audit-sanctioned
    # intermediate keeps the CPU percentile (identical semantics) but moves
    # the transfers to single-channel: a GPU luma extraction pass feeds the
    # ring buffer via an R32F readback (¼ the bytes), and the gain is
    # applied by a GPU composite pass (¼ upload, gain map only).
    _LUMA_EXTRACTION_FRAGMENT = """
    #version 330
    uniform sampler2D u_texture;
    in vec2 v_uv;
    out vec4 fragColor;
    void main() {
        float luma = dot(texture(u_texture, v_uv).rgb,
                         vec3(0.2126, 0.7152, 0.0722));
        fragColor = vec4(luma, 0.0, 0.0, 1.0);
    }
    """

    _GAIN_COMPOSITE_FRAGMENT = """
    #version 330
    uniform sampler2D u_texture;
    uniform sampler2D u_gain_tex;
    uniform float u_gain;
    uniform float u_use_tex;
    in vec2 v_uv;
    out vec4 fragColor;
    void main() {
        vec4 src = texture(u_texture, v_uv);
        float g = mix(u_gain, texture(u_gain_tex, v_uv).r, u_use_tex);
        fragColor = vec4(max(src.rgb * g, vec3(0.0)), src.a);
    }
    """

    _QUAD_VERTICES = np.array([
        -1.0, -1.0, 0.0, 0.0,
         1.0, -1.0, 1.0, 0.0,
        -1.0,  1.0, 0.0, 1.0,
         1.0,  1.0, 1.0, 1.0,
    ], dtype='f4')

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "window": "5",
            "percentile": 0.5,
            "luma_only": True,
            "mode": "per_pixel",
        }
        # Runtime temporal buffer: absolute frame → luma plane.  Not part
        # of the graph schema; never serialized or undone.  The RGB buffer
        # only fills for the per_pixel + luma_only=False channel path.
        self._luma_buffer: Dict[int, "np.ndarray"] = {}
        self._rgb_buffer: Dict[int, "np.ndarray"] = {}
        self.warming = False
        # AUD-6603: bounded registry of per-frame output textures, keyed
        # (gl, w, h, frame) and owned by the node.  The evaluator never
        # owns source-node outputs (owns_color_texture=False → not in
        # CACHE_OWNED_TYPES), so without this registry the per-frame
        # output textures had no owner and were never released.
        self._output_ring: "OrderedDict[Tuple[Any, int, int, int], _OutputEntry]" = OrderedDict()
        self._output_ring_bytes: int = 0
        # Set whenever a registered texture is retired or overwritten;
        # consumed by base_evaluator's self-managed-source handling to
        # drop stale cache entries (this node + downstream) that still
        # reference the released glo.
        self._cache_invalidated_needed: bool = False

    # ── Cache contract ─────────────────────────────────────────────────
    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, Any]) -> bool:
        """Temporal node — every frame is its own cache dimension."""
        return True

    # ── Geometry contract ──────────────────────────────────────────────
    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        return {"image": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return 0

    # ── Window bookkeeping ─────────────────────────────────────────────
    def _window_size(self) -> int:
        try:
            size = int(self.parameters.get("window", "5"))
        except (TypeError, ValueError):
            size = 5
        size = size if size in WINDOWS else 5
        return size

    def _trailing_frames(self, frame: int) -> List[int]:
        """Causal window: the ``window`` frames ending at ``frame``."""
        half = self._window_size() // 2
        start = max(0, frame - 2 * half)
        return list(range(start, frame + 1))

    def _frame_plane_bytes(self, frame: int) -> int:
        """Host bytes held for one buffered frame (luma + optional RGB)."""
        total = 0
        plane = self._luma_buffer.get(frame)
        if plane is not None:
            total += int(getattr(plane, "nbytes", 0))
        plane = self._rgb_buffer.get(frame)
        if plane is not None:
            total += int(getattr(plane, "nbytes", 0))
        return total

    def _store_luma(self, frame: int, luma: "np.ndarray",
                    rgb: Optional["np.ndarray"] = None) -> None:
        self._luma_buffer[int(frame)] = luma
        if rgb is not None:
            self._rgb_buffer[int(frame)] = rgb
        self._prune_buffers(int(frame))

    def _prune_buffers(self, frame: int) -> None:
        """Bound the runtime history (AUD-6605).

        Two stages:

        1. Entry cap (legacy behaviour) — over ``_BUFFER_MAX_ENTRIES``
           distinct frames, drop everything outside the causal window.
        2. Byte budget — the frames OUTSIDE the current causal window are
           scrub-back convenience only; evict them farthest-from-playhead
           first until they fit ``_BUFFER_MAX_BYTES``.  Window frames are
           never evicted: dropping them would silently flip the node into
           its warming passthrough every evaluation, so the window-sized
           working set (worst case 9 planes) is the floor this fix keeps.
        """
        # Stage 1: entry cap.
        if len(self._luma_buffer) > _BUFFER_MAX_ENTRIES:
            keep = set(self._trailing_frames(frame))
            for stored in list(self._luma_buffer):
                if stored not in keep:
                    self._luma_buffer.pop(stored, None)
                    self._rgb_buffer.pop(stored, None)

        # Stage 2: byte budget on the non-window history.
        window = set(self._trailing_frames(frame))
        history = [f for f in self._luma_buffer if f not in window]
        history_bytes = sum(self._frame_plane_bytes(f) for f in history)
        while history and history_bytes > _BUFFER_MAX_BYTES:
            victim = max(history, key=lambda f: abs(f - frame))
            history.remove(victim)
            history_bytes -= self._frame_plane_bytes(victim)
            self._luma_buffer.pop(victim, None)
            self._rgb_buffer.pop(victim, None)

    def _collect_window(
            self, frame: int,
            buffer: Optional[Dict[int, "np.ndarray"]] = None,
            expected_shape: Optional[Tuple[int, ...]] = None,
    ) -> Tuple[List[int], List["np.ndarray"]]:
        """Gather the window's planes, skipping resolution mismatches.

        Preview (proxy/LOD) and export (full res) evaluations share the
        live node instance; a plane recorded at another resolution is
        treated as ABSENT (→ warming) rather than stacked — mixing shapes
        would raise inside np.stack and blank the frame.
        """
        if buffer is None:
            buffer = self._luma_buffer
        frames = self._trailing_frames(frame)
        planes: List["np.ndarray"] = []
        present: List[int] = []
        for f in frames:
            plane = buffer.get(int(f))
            if plane is None:
                continue
            if (expected_shape is not None
                    and tuple(plane.shape) != tuple(expected_shape)):
                continue
            present.append(f)
            planes.append(plane)
        return present, planes

    def clear_buffer(self) -> None:
        """Drop the runtime buffer (project reload, manual reset)."""
        self._luma_buffer.clear()
        self._rgb_buffer.clear()
        self.warming = False

    # ── Output ownership (AUD-6603) ────────────────────────────────────
    @staticmethod
    def _output_texture_bytes(tex: Any) -> int:
        """GPU bytes of an RGBA float32 output texture (0 if unknown)."""
        try:
            return int(tex.width) * int(tex.height) * 4 * 4
        except Exception:
            return 0

    @staticmethod
    def _output_ring_key(gl: Any, w: int, h: int,
                         frame: int) -> Tuple[Any, int, int, int]:
        """Registry key: GL context object + geometry + ABSOLUTE frame.

        The frame dimension keeps cache entries for different frames
        backed by distinct textures (one shared texture would alias the
        per-frame results); the context object keys context recreation
        (identity compare — an ``id()`` int could be recycled).
        """
        return (gl, int(w), int(h), int(frame))

    def _retire_output_texture(self, entry: _OutputEntry,
                               release: bool = True) -> None:
        """Hand a retired output texture back to its allocator.

        Pool-tracked textures go through ``resource_pool.release_texture``
        (which returns them to the pool for reuse and itself delegates
        untracked textures to the framebuffer manager's pool); without a
        usable pool the texture is released directly and its cached FBO
        (``fb_manager.get_fbo``) is dropped with it.  ``release=False``
        only drops the reference — used when the owning context went
        away, where ``.release()`` on dead-context objects raises
        (particle-node precedent).
        """
        tex = entry.tex
        if not release or tex is None:
            return
        pool = entry.resource_pool
        if pool is not None and hasattr(pool, "release_texture"):
            try:
                pool.release_texture(tex)
                return
            except Exception as exc:
                logger.debug("Deflicker pool retire failed: %s", exc)
        fb_manager = entry.fb_manager
        if fb_manager is not None and hasattr(fb_manager, "release_temp_texture"):
            try:
                fb_manager.release_temp_texture(tex)
                return
            except Exception as exc:
                logger.debug("Deflicker fb retire failed: %s", exc)
        fbo_cache = getattr(fb_manager, "_fbo_cache", None)
        if isinstance(fbo_cache, dict):
            fbo = fbo_cache.pop(tex, None)
            if fbo is not None:
                try:
                    fbo.release()
                except Exception:
                    pass
        try:
            tex.release()
        except Exception:
            pass

    def _acquire_output_texture(self, gl: Any, w: int, h: int, frame: int,
                                resource_pool: Any) -> Tuple[Any, bool]:
        """Fetch this frame's output texture — reuse from the ring or allocate.

        Returns ``(texture, reused)``.  A registry hit means the exact
        (ctx, size, frame) key is being re-evaluated (cache miss after an
        eviction/invalidation, or a parameter change): rendering into the
        existing texture avoids a fresh allocation.
        """
        key = self._output_ring_key(gl, w, h, frame)
        entry = self._output_ring.get(key)
        if entry is not None:
            # LRU refresh: this frame is hot again.
            self._output_ring.move_to_end(key)
            return entry.tex, True
        if resource_pool is not None:
            try:
                return resource_pool.acquire_texture(
                    w, h, components=4, dtype="f4"), False
            except Exception as exc:
                logger.debug("Deflicker output pool acquire failed: %s", exc)
        return gl.texture((w, h), 4, dtype="f4"), False

    def _register_output_texture(self, key: Tuple[Any, int, int, int],
                                 tex: Any, gl: Any, resource_pool: Any,
                                 fb_manager: Any) -> None:
        """Take ownership of a freshly allocated output texture.

        Retires registry overflow (byte/entry budgets) and drops entries
        from a previous GL context without releasing them.  Every
        retirement sets ``_cache_invalidated_needed`` because the
        evaluator's cache may still hold TextureResults pointing at the
        retired glo.
        """
        ring = self._output_ring
        if key not in ring:
            ring[key] = _OutputEntry(gl, tex, resource_pool, fb_manager)
            self._output_ring_bytes += self._output_texture_bytes(tex)
        # Context recreation: old-context objects are dead — drop the
        # references without touching GL.
        for stale_key in [k for k in ring if k[0] is not gl]:
            entry = ring.pop(stale_key)
            self._output_ring_bytes -= self._output_texture_bytes(entry.tex)
            self._retire_output_texture(entry, release=False)
            self._cache_invalidated_needed = True
        # Bound the registry: retire the oldest entries beyond the
        # budgets while always keeping the newest two alive (the current
        # frame plus one-frame-back for the evaluator's frame cache).
        while len(ring) > 2 and (
                len(ring) > _OUTPUT_RING_MAX_ENTRIES
                or self._output_ring_bytes > _OUTPUT_RING_MAX_BYTES):
            _, old_entry = ring.popitem(last=False)
            self._output_ring_bytes -= self._output_texture_bytes(old_entry.tex)
            self._retire_output_texture(old_entry)
            self._cache_invalidated_needed = True

    def cleanup(self) -> None:
        """Release every self-owned GL resource.

        Called on the render thread when the node leaves the graph
        (``_sync_render_nodes``) or at renderer shutdown.  Retires the
        output registry through each entry's captured allocator and also
        releases the luma texture / shader programs / quad VBO — the
        removal-path half of AUD-6604.
        """
        for entry in list(self._output_ring.values()):
            self._retire_output_texture(entry)
        self._output_ring.clear()
        self._output_ring_bytes = 0
        # Released glos may live on in evaluator cache entries (this node
        # + downstream) — flag them for invalidation.
        self._cache_invalidated_needed = True
        for name in ("_luma_tex", "_luma_prog", "_composite_prog", "_gpu_vbo"):
            obj = getattr(self, name, None)
            setattr(self, name, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass
        self._gpu_ctx = None
        self._luma_ctx = None

    # ── GPU glue (defensive: failures degrade to passthrough) ─────────
    def _read_input_rgba(self, image: TextureResult, ctx: Any,
                         fb_manager: Any) -> Optional["np.ndarray"]:
        """Read the upstream texture back as a top-down RGBA array."""
        from ._gpu_readback import read_rgba
        return read_rgba(image, ctx, fb_manager)

    def _upload_result(self, rgba: "np.ndarray", image: TextureResult,
                       ctx: Any, frame: int, resource_pool: Any = None,
                       fb_manager: Any = None) -> Optional[TextureResult]:
        """Upload a corrected frame into the node-owned output texture.

        AUD-6603: the texture comes from (and returns to) the bounded
        output registry — previously every call created an unowned
        ``gl.texture`` that nothing ever released.
        """
        try:
            gl = getattr(ctx, "gl", ctx)
            h, w = rgba.shape[:2]
            key = self._output_ring_key(gl, w, h, frame)
            tex, reused = self._acquire_output_texture(
                gl, w, h, frame, resource_pool)
            data = np.ascontiguousarray(np.flipud(rgba))
            tex.write(data.tobytes())
            if reused:
                # Re-rendered the same (ctx, size, frame): the texture was
                # overwritten, so cache entries holding this glo with the
                # previous pixels must be invalidated.
                self._cache_invalidated_needed = True
            else:
                self._register_output_texture(
                    key, tex, gl, resource_pool, fb_manager)
            return TextureResult(
                texture_id=tex.glo, width=w, height=h,
                format=image.format,
                bbox=image.bbox,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            )
        except Exception as exc:
            logger.warning("Deflicker upload failed: %s", exc)
            return None

    # ── P2-9/P2-11 GPU path ────────────────────────────────────────────
    def _ensure_gpu_resources(self, gl: Any) -> tuple:
        """Lazy-compile the luma/composite programs and quad VBO (per ctx)."""
        if getattr(self, "_gpu_ctx", None) is not gl:
            from src.features.composite.core.shaders import (
                _PASSTHROUGH_VERTEX,
            )
            self._luma_prog = gl.program(
                vertex_shader=_PASSTHROUGH_VERTEX,
                fragment_shader=self._LUMA_EXTRACTION_FRAGMENT,
            )
            self._composite_prog = gl.program(
                vertex_shader=_PASSTHROUGH_VERTEX,
                fragment_shader=self._GAIN_COMPOSITE_FRAGMENT,
            )
            self._gpu_vbo = gl.buffer(self._QUAD_VERTICES)
            self._gpu_ctx = gl
        return self._luma_prog, self._composite_prog, self._gpu_vbo

    def _luma_plane_gpu(self, image: TextureResult, ctx: Any,
                        fb_manager: Any,
                        resource_pool: Any) -> Optional["np.ndarray"]:
        """Extract the Rec.709 luma plane on the GPU, read back 1 channel.

        Returns a top-down (H, W) float32 plane, or None when GL state is
        unavailable (the caller degrades to the legacy full-frame chain).
        """
        try:
            import moderngl
            from src.features.composite.core.gl_bind import bind_texture_unit
            gl = getattr(ctx, "gl", ctx)
            if fb_manager is None:
                return None
            from ._gpu_readback import find_texture
            src_tex = find_texture(image.texture_id, fb_manager,
                                   resource_pool)
            if src_tex is None:
                return None
            w = max(1, int(image.width))
            h = max(1, int(image.height))
            # R32F target, cached per ctx and resized on dimension change.
            lt = getattr(self, "_luma_tex", None)
            if (lt is None or getattr(self, "_luma_ctx", None) is not gl
                    or lt.width != w or lt.height != h):
                if (lt is not None
                        and getattr(self, "_luma_ctx", None) is gl):
                    lt.release()
                lt = gl.texture((w, h), 1, dtype="f4")
                self._luma_tex = lt
                self._luma_ctx = gl
            prog, _, vbo = self._ensure_gpu_resources(gl)
            fbo = fb_manager.get_fbo(lt)
            fbo.use()
            gl.viewport = (0, 0, w, h)
            bind_texture_unit(src_tex.glo, 0)
            prog['u_texture'].value = 0
            vao = gl.vertex_array(
                prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
            vao.render(moderngl.TRIANGLE_STRIP)
            vao.release()
            bind_texture_unit(0, 0)
            raw = fbo.read(components=1, dtype="f4")
            plane = np.frombuffer(raw, dtype=np.float32).reshape(h, w)
            # GL rows start at the bottom; the ring buffer is top-down.
            return np.ascontiguousarray(np.flipud(plane))
        except Exception as exc:
            logger.warning("Deflicker GPU luma extraction failed: %s", exc)
            return None

    def _composite_gpu(self, image: TextureResult,
                       gain_map: Optional["np.ndarray"], gain_scalar: float,
                       ctx: Any, fb_manager: Any,
                       resource_pool: Any, frame: int) -> Optional[TextureResult]:
        """Apply the gain on the GPU (per-pixel R32F map or scalar uniform).

        Alpha passes through untouched and RGB is clamped at 0, matching
        ``apply_deflicker``.  Returns None on GL failure (legacy fallback).
        """
        gain_tex = None
        try:
            import moderngl
            from src.features.composite.core.gl_bind import bind_texture_unit
            gl = getattr(ctx, "gl", ctx)
            if fb_manager is None:
                return None
            from ._gpu_readback import find_texture
            src_tex = find_texture(image.texture_id, fb_manager,
                                   resource_pool)
            if src_tex is None:
                return None
            w = max(1, int(image.width))
            h = max(1, int(image.height))
            # AUD-6603: per-frame output textures are owned by the node's
            # bounded registry (reuse by key, retire on overflow / ctx
            # change / cleanup).  The evaluator never owns source-node
            # outputs, so an unowned acquire here previously leaked one
            # full-resolution RGBA f4 texture per evaluated frame.
            key = self._output_ring_key(gl, w, h, frame)
            out_tex, reused = self._acquire_output_texture(
                gl, w, h, frame, resource_pool)
            if reused:
                # Same (ctx, size, frame) re-rendered: content overwrite,
                # cache entries holding this glo need invalidation.
                self._cache_invalidated_needed = True
            else:
                self._register_output_texture(
                    key, out_tex, gl, resource_pool, fb_manager)
            use_tex = 0.0
            if gain_map is not None:
                use_tex = 1.0
                data = np.ascontiguousarray(np.flipud(gain_map))
                gain_tex = gl.texture(
                    (w, h), 1, data=data.tobytes(), dtype="f4")
            _, prog, vbo = self._ensure_gpu_resources(gl)
            fbo = fb_manager.get_fbo(out_tex)
            fbo.use()
            gl.viewport = (0, 0, w, h)
            bind_texture_unit(src_tex.glo, 0)
            # Unit 1 needs a bound texture even in scalar mode — the mix()
            # select makes the fetched value irrelevant, but sampling an
            # unbound unit is undefined on some drivers.
            bind_texture_unit(gain_tex.glo if gain_tex is not None
                              else src_tex.glo, 1)
            prog['u_texture'].value = 0
            prog['u_gain_tex'].value = 1
            prog['u_gain'].value = float(gain_scalar)
            prog['u_use_tex'].value = use_tex
            vao = gl.vertex_array(
                prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
            vao.render(moderngl.TRIANGLE_STRIP)
            vao.release()
            bind_texture_unit(0, 0)
            bind_texture_unit(0, 1)
            return TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=image.format,
                bbox=image.bbox,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            )
        except Exception as exc:
            logger.warning("Deflicker GPU composite failed: %s", exc)
            return None
        finally:
            if gain_tex is not None:
                gain_tex.release()

    def _evaluate_gpu_luma(self, image: TextureResult, frame: int,
                           percentile: float, mode: str, ctx: Any,
                           fb_manager: Any,
                           resource_pool: Any) -> Optional[Dict[str, Any]]:
        """Default-mode evaluation with single-channel transfers.

        Returns the result dict, a warming passthrough dict, or None when
        the GPU path failed and the caller must run the legacy chain.
        """
        luma = self._luma_plane_gpu(image, ctx, fb_manager, resource_pool)
        if luma is None:
            return None
        self._store_luma(frame, luma)

        shape = luma.shape
        present, lumas = self._collect_window(frame, self._luma_buffer,
                                              shape)
        if not lumas or len(present) < len(self._trailing_frames(frame)):
            # Window incomplete (jump or cold start) — identical visible
            # degradation to the legacy chain.
            self.warming = True
            self.error_message = (
                f"Deflicker warming: window {present[0] if present else '-'}"
                f"..{frame} incomplete — passing input through")
            return {"result": image}
        self.warming = False
        self.error_message = ""

        if mode == "global_gain":
            gain_map = None
            gain_scalar = compute_global_gain(lumas, luma)
        else:
            gain_map = compute_gain_map(luma, lumas, percentile)
            gain_scalar = 1.0
        result = self._composite_gpu(image, gain_map, gain_scalar, ctx,
                                     fb_manager, resource_pool, frame)
        if result is None:
            return None
        return {"result": result}

    def _evaluate_legacy(self, image: TextureResult, frame: int,
                         percentile: float, mode: str, luma_only: bool,
                         keep_rgb: bool, ctx: Any,
                         fb_manager: Any,
                         resource_pool: Any = None) -> Dict[str, Any]:
        """Original full-frame CPU chain (RGB mode, force-CPU, fallback)."""
        rgba = self._read_input_rgba(image, ctx, fb_manager)
        if rgba is None:
            # No GL surface available: passthrough WITHOUT buffer
            # bookkeeping, but visibly (silent no-ops hide pipeline bugs).
            self.warming = True
            self.error_message = (
                "Deflicker: input readback unavailable — passing through")
            return {"result": image}

        luma = luma_of(rgba[..., :3])
        self._store_luma(frame, luma,
                         rgba[..., :3] if keep_rgb else None)

        shape = luma.shape
        present, lumas = self._collect_window(frame, self._luma_buffer,
                                              shape)
        rgb_stack = None
        if keep_rgb:
            rgb_present, rgb_stack = self._collect_window(
                frame, self._rgb_buffer, shape)
            if len(rgb_present) < len(lumas):
                # RGB window lagging the luma window (mode just changed):
                # degrade rather than mixing channels across windows.
                rgb_stack = None

        if not lumas or len(present) < len(self._trailing_frames(frame)):
            # Window incomplete (jump or cold start): degrade to a visible
            # passthrough instead of faking a full window (or crashing in
            # np.stack on an empty stack).
            self.warming = True
            self.error_message = (
                f"Deflicker warming: window {present[0] if present else '-'}"
                f"..{frame} incomplete — passing input through")
            return {"result": image}
        self.warming = False
        self.error_message = ""

        corrected = apply_deflicker(rgba[..., :3], luma, lumas,
                                    percentile, luma_only, mode,
                                    rgb_stack=rgb_stack)
        out_rgba = np.concatenate(
            [corrected.astype(np.float32), rgba[..., 3:4]], axis=-1)

        result = self._upload_result(out_rgba, image, ctx, frame,
                                     resource_pool, fb_manager)
        if result is None:
            return {"result": image}
        return {"result": result}

    # ── Evaluation ─────────────────────────────────────────────────────
    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}

        frame = 0
        if ctx is not None:
            frame = int(getattr(ctx, "frame_number", 0) or 0)
        if frame < 0:
            # Negative absolute frame (nested/remapped timelines): the
            # trailing window is undefined and the incomplete-window guard
            # below would compare 0 < 0 and fall through to np.stack([]) —
            # "need at least one array to stack" (P2-9). Degrade visibly.
            self.warming = True
            self.error_message = (
                f"Deflicker: negative frame {frame} — passing input through")
            return {"result": image}

        percentile = min(1.0, max(0.0,
                         float(self.parameters.get("percentile", 0.5))))
        mode = self.parameters.get("mode", "per_pixel")
        if mode not in MODES:
            mode = "per_pixel"
        luma_only = bool(self.parameters.get("luma_only", True))
        keep_rgb = (mode == "per_pixel" and not luma_only)

        # P2-9/P2-11: single-channel GPU path for every luma_only
        # evaluation; the full-frame CPU chain remains for the channel-wise
        # RGB percentile mode, the force-CPU escape hatch, and as the
        # fallback whenever any GPU step fails.
        if not keep_rgb and not os.environ.get(_ENV_FORCE_CPU):
            gpu_result = self._evaluate_gpu_luma(
                image, frame, percentile, mode, ctx, fb_manager,
                resource_pool)
            if gpu_result is not None:
                return gpu_result

        return self._evaluate_legacy(image, frame, percentile, mode,
                                     luma_only, keep_rgb, ctx, fb_manager,
                                     resource_pool)
