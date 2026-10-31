"""Runs an :class:`EffectSpec` on a specific moderngl context.

The runner is the *execution* half of the EffectSpec contract — given a
context, a spec, an input texture glo and a parameter dict, it produces an
output texture glo. It owns:

* a program cache (keyed by ``(effect_type, structural_param_values)``) so
  shaders compile once per parameter shape, not per frame;
* a fullscreen-quad VAO shared across all programs on this context;
* a small pool of "scratch" textures used as intermediate ping-pong targets.

It deliberately does NOT own the input or output textures — callers pass
the input glo (which may live anywhere in the share group) and receive an
output glo owned by the runner's scratch pool. The caller is expected to
read the result immediately (same frame, same thread) and must not hold
the glo across frames (the scratch may be reused).

Thread-safety: one runner per context, used only from the thread that owns
the context (mirrors the contract of every other moderngl-using object in
the codebase — ``CompositeRenderer``, ``EditGpuPreviewRenderer``, etc).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import numpy as np

from src.features.shared.effects.effect_catalog import (
    EDGE_POLICY_LABELS,
    SHARPEN_CHANNEL_LABELS,
    EDGE_METHOD_LABELS,
    EDGE_DIRECTION_LABELS,
    EDGE_OUTPUT_MODE_LABELS,
    EDGE_CHANNEL_LABELS,
    ERODE_OPERATION_LABELS,
    ERODE_EDGE_MODE_LABELS,
    ERODE_CHANNEL_LABELS,
    DEFOCUS_MODE_LABELS,
)
from src.features.shared.effects.effect_spec import EffectSpec, ShaderKind
from src.features.shared.effects.effect_tiers import (
    FAST_TIER_MIN_RADIUS,
    FAST_TIER_SCALE,
    effective_denoise_radius,
)
from src.shared.render_diagnostics import (
    CODE_EFFECT_PASS_COMPILE_FAILED,
    SOURCE_EFFECT_PASS,
    record_compile_failure,
)

if TYPE_CHECKING:
    import moderngl  # noqa: F401  (avoid hard import at module load)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Vertex shader shared by every effect fragment.
#
# This MUST match the attribute/varying names used in the composite
# shaders.py fragment strings (``in_vert``/``in_uv`` → ``v_uv``). The Edit
# page's own _grade_program/_blend_program use a DIFFERENT convention
# (``in_vert``/``in_texcoord`` → ``v_texcoord`` → ``f_color``) — those are
# not compatible with composite shaders and we do not try to bridge them;
# the runner always pairs composite fragment sources with this vertex
# shader, building its own VAO.
# ──────────────────────────────────────────────────────────────────────────

_PASSTHROUGH_VERTEX = """
#version 330
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_uv = in_uv;
}
"""

# Fullscreen triangle-strip quad: positions (xy) + uvs (xy).
# Matches the layout composite's base_evaluator._render_quad uses so the
# rendered image has the same orientation as the composite output.
_QUAD_VERTICES = np.array([
    -1.0, -1.0, 0.0, 0.0,
     1.0, -1.0, 1.0, 0.0,
    -1.0,  1.0, 0.0, 1.0,
     1.0,  1.0, 1.0, 1.0,
], dtype="f4")


# T-P1-21 batch 2: trivial copy shader for the fast-tier down/upsample
# draws. The resolution change comes entirely from the target viewport +
# the source texture's filter (LINEAR ⇒ bilinear decimation on down,
# bilinear reconstruction on up), so the shader itself is a plain fetch.
_BLIT_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 f_color;
void main() {
    f_color = texture(u_texture, v_uv);
}
"""


# Fast-tier final combine: per-channel mix of the pristine full-res input
# against the upsampled half-res blur. The half-res V pass runs with a full
# mask and mix=1 (pure H+V blur); mask/mix semantics are applied HERE at
# full resolution so masked-off channels stay bit-exact instead of being
# degraded through the half-res round-trip.
_BLUR_FAST_COMBINE_FRAGMENT = """
#version 330
uniform sampler2D u_blurred;   // half-res H+V result (LINEAR upsample)
uniform sampler2D u_original;  // pristine full-res input
uniform vec4 u_channel_mask;
uniform float u_mix;
in vec2 v_uv;
out vec4 f_color;
void main() {
    vec4 o = texture(u_original, v_uv);
    vec4 b = texture(u_blurred, v_uv);
    f_color = vec4(
        mix(o.r, b.r, u_channel_mask.r * u_mix),
        mix(o.g, b.g, u_channel_mask.g * u_mix),
        mix(o.b, b.b, u_channel_mask.b * u_mix),
        mix(o.a, b.a, u_channel_mask.a * u_mix)
    );
}
"""


# ──────────────────────────────────────────────────────────────────────────
# Param-packing helpers.
#
# Composite shader uniforms use GL types (int, vec4) that don't match the
# user-facing ``ParamDef`` schema (string options, four bool channels).
# The packing is per-dispatch-key: the runner knows how to translate
# blur/sharpen params; future effects register their own packer.
# ──────────────────────────────────────────────────────────────────────────

def _label_to_index(value: Any, labels: Tuple[str, ...], default: int = 0) -> int:
    """Map a string option value to its index in ``labels``.

    Accepts the string label, an int already in range, or a numeric string.
    Falls back to ``default`` (and logs) for unknown labels so a corrupt
    saved value can't crash the render thread.
    """
    if isinstance(value, int):
        return value if 0 <= value < len(labels) else default
    if isinstance(value, (float,)):
        idx = int(value)
        return idx if 0 <= idx < len(labels) else default
    if isinstance(value, str):
        if value in labels:
            return labels.index(value)
        if value.isdigit():
            idx = int(value)
            return idx if 0 <= idx < len(labels) else default
    return default


def _pack_blur_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Translate the complete Composite Blur schema into shader uniforms.

    The same packer is consumed by GPU preview and the CPU fallback. It
    therefore includes uniforms for every Blur variant; unused uniforms are
    harmless because the runner only uploads uniforms present in a program.
    """
    blur_type = params.get("blur_type", "box")
    kernel_mode = 1 if blur_type == "gaussian" else 0
    edge_policy = _label_to_index(
        params.get("edge_policy", "clamp"), EDGE_POLICY_LABELS, default=0
    )
    if any(key in params for key in ("channel_r", "channel_g", "channel_b", "channel_a")):
        channel_mask = (
            1.0 if params.get("channel_r", True) else 0.0,
            1.0 if params.get("channel_g", True) else 0.0,
            1.0 if params.get("channel_b", True) else 0.0,
            1.0 if params.get("channel_a", False) else 0.0,
        )
    else:
        channel_mask = (
            1.0, 1.0, 1.0,
            # Fresh Blur instances use channel_a=False. Legacy instances
            # carry process_alpha explicitly, so they still round-trip.
            1.0 if params.get("process_alpha", False) else 0.0,
        )
    quality = params.get("quality", "normal")
    blades = params.get("aperture_blades", "circle")
    if isinstance(blades, (int, float)):
        blades_int = int(blades)
    elif isinstance(blades, str) and blades.isdigit():
        blades_int = int(blades)
    else:
        blades_int = 0
    return {
        "u_kernel_mode": kernel_mode,
        "u_edge_policy": edge_policy,
        "u_channel_mask": channel_mask,
        "u_radius": float(params.get("radius", params.get("radius_x", 5.0))),
        "u_radius_x": float(params.get("radius_x", params.get("radius", 5.0))),
        "u_radius_y": float(params.get("radius_y", params.get("radius", 5.0))),
        "u_edge_preserve": float(params.get("edge_preserve", 0.5)),
        "u_angle": float(params.get("angle", 0.0)),
        # Motion variant: the shader short-circuits to the original when
        # u_shutter is unset (ModernGL leaves it at 0.0), which made motion
        # blur a silent no-op on the Edit effect path. Spec default is 1.0.
        "u_shutter": max(0.0, min(2.0, float(params.get("shutter", 1.0)))),
        # The runner binds no velocity texture; angle-driven streaks only.
        "u_use_velocity": 0,
        # Bokeh's uniform is the actual blade count (5/6/8), not the option
        # index in BOKEH_BLADE_LABELS. "circle" is encoded as zero.
        "u_aperture_blades": blades_int if blades_int in (5, 6, 8) else 0,
        "u_anamorphic": float(params.get("anamorphic", 0.0)),
        "u_highlight_gain": float(params.get("highlight_gain", 0.0)),
        "u_sample_count": {"fast": 8, "normal": 16, "high": 32}.get(quality, 16),
        "u_mix": float(params.get("mix", 1.0)),
    }


def _pack_sharpen_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Translate user-facing sharpen params into shader uniforms.

    Only ``channels`` needs packing (string → int 0/1/2). The rest
    (amount/radius/threshold) are plain floats and flow through unchanged.
    """
    channels_idx = _label_to_index(
        params.get("channels", "all"), SHARPEN_CHANNEL_LABELS, default=0
    )
    return {
        "u_amount": float(params.get("amount", 1.0)),
        "u_radius": float(params.get("radius", 1.0)),
        "u_threshold": float(params.get("threshold", 0.0)),
        "u_channels": channels_idx,
    }


def _pack_denoise_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Denoise: all uniforms are direct pass-throughs (no packing).

    T-P1-21 batch 2: ``quality`` (fast/normal/high) selects the effective
    bilateral kernel radius via the shared tier contract in
    ``effect_tiers`` — the CPU fallback applies the same mapping, so a
    tier change cannot drift between the two paths.
    """
    return {
        "u_strength": float(params.get("strength", 0.5)),
        "u_radius": effective_denoise_radius(
            int(params.get("radius", 3)), str(params.get("quality", "normal"))
        ),
        "u_luminance_only": bool(params.get("luminance_only", False)),
        "u_chroma_only": bool(params.get("chroma_only", False)),
    }


def _pack_edge_detect_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Edge detect: four option params pack to ints via list-index."""
    return {
        "u_method": _label_to_index(params.get("method", "sobel"), EDGE_METHOD_LABELS, 0),
        "u_threshold": float(params.get("threshold", 0.1)),
        "u_threshold_high": float(params.get("threshold_high", 0.3)),
        "u_direction": _label_to_index(params.get("direction", "both"), EDGE_DIRECTION_LABELS, 0),
        "u_output_mode": _label_to_index(params.get("output_mode", "edges"), EDGE_OUTPUT_MODE_LABELS, 0),
        "u_invert": bool(params.get("invert", False)),
        "u_output_strength": float(params.get("output_strength", 1.0)),
        "u_kernel_size": int(params.get("kernel_size", 3)),
        "u_preblur": float(params.get("preblur", 0.0)),
        "u_channel": _label_to_index(params.get("channel", "luminance"), EDGE_CHANNEL_LABELS, 0),
    }


def _pack_erode_dilate_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Erode/dilate: operation/channels/edge_mode pack to ints.

    ``iterations`` is NOT a uniform — it controls how many times the
    runner re-applies the shader (handled in ``_run_erode_dilate``).
    The packer returns it separately under a reserved key so the runner
    can pick it up.
    """
    return {
        "u_operation": _label_to_index(params.get("operation", "erode"), ERODE_OPERATION_LABELS, 0),
        "u_size": max(1, int(params.get("size", 3))),
        "u_channels": _label_to_index(params.get("channels", "alpha"), ERODE_CHANNEL_LABELS, 0),
        "u_edge_mode": _label_to_index(params.get("edge_mode", "extend"), ERODE_EDGE_MODE_LABELS, 0),
        "_iterations": max(1, int(params.get("iterations", 1))),  # runner-only
    }


def _pack_defocus_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Defocus: bridge the PARAM_DEFS schema to the shader's uniforms.

    The shader declares ``u_focal_point`` (vec2), ``u_focal_size``,
    ``u_max_radius``, ``u_mode``, ``u_edge_policy``, ``u_process_alpha``,
    ``u_sample_count``. PARAM_DEFS gives us focal_x/focal_y/focal_size/
    max_radius/mode/edge_policy/process_alpha. We pack focal_x+y into
    the vec2 and map mode (separable/mipmap → 0/1).

    NOTE: this packer targets the SHADER contract (which matches
    PARAM_DEFS), not the legacy ``_apply_defocus`` evaluator which reads
    a different set of keys (iris_size/iris_type/gain) — that evaluator
    bug is out of scope for the Edit-page path.
    """
    return {
        "u_focal_point": (
            float(params.get("focal_x", 0.5)),
            float(params.get("focal_y", 0.5)),
        ),
        "u_focal_size": float(params.get("focal_size", 0.3)),
        "u_max_radius": float(params.get("max_radius", 20.0)),
        "u_mode": _label_to_index(params.get("mode", "separable"), DEFOCUS_MODE_LABELS, 0),
        "u_edge_policy": _label_to_index(params.get("edge_policy", "clamp"), EDGE_POLICY_LABELS, 0),
        "u_process_alpha": bool(params.get("process_alpha", True)),
        "u_sample_count": 16,  # reasonable default; PARAM_DEFS has no quality for defocus
    }


def _pack_glow_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Glow: merge tint_r/g/b into ``u_tint`` (vec3); derive sample_count.

    The glow shaders need both passes to share threshold/radius/
    sample_count; pass-2 additionally needs intensity/tint/mix. The
    runner sets per-pass uniforms from this packed dict (see
    ``_run_glow``).
    """
    quality = params.get("quality", "normal")
    sample_count = {"fast": 8, "normal": 16, "high": 32}.get(quality, 16)
    return {
        "u_threshold": float(params.get("threshold", 0.7)),
        "u_radius": float(params.get("radius", 15.0)),
        "u_intensity": float(params.get("intensity", 1.5)),
        "u_tint": (
            float(params.get("tint_r", 1.0)),
            float(params.get("tint_g", 0.85)),
            float(params.get("tint_b", 0.7)),
        ),
        "u_mix": float(params.get("mix", 1.0)),
        "u_sample_count": sample_count,
    }


def _pack_lib1_posterize_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """F18（计划 L）：Posterize 共享核的 uniform 打包。

    PosterizeNode 参数 → composite `_POSTERIZE_FRAGMENT` uniforms：
    levels(int ParamDef) → u_levels(float uniform)、channel_mode(option)
    → u_channel_mode(int 索引)、dither(bool) → u_dither(float 0/1)。
    """
    modes = ("rgb", "rgba", "luminance")
    mode = params.get("channel_mode", "rgb")
    return {
        "u_levels": float(params.get("levels", 8)),
        "u_channel_mode": modes.index(mode) if mode in modes else 0,
        "u_dither": 1.0 if params.get("dither", False) else 0.0,
    }


# Per-dispatch-key packer registry. Unknown keys get identity packing.
_PARAM_PACKERS = {
    "blur": _pack_blur_params,
    "sharpen": _pack_sharpen_params,
    "denoise": _pack_denoise_params,
    "edge_detect": _pack_edge_detect_params,
    "erode_dilate": _pack_erode_dilate_params,
    "defocus": _pack_defocus_params,
    "glow": _pack_glow_params,
    "lib1_posterize": _pack_lib1_posterize_params,
}


# ──────────────────────────────────────────────────────────────────────────
# The runner.
# ──────────────────────────────────────────────────────────────────────────

class EffectPassRunner:
    """Compiles and executes :class:`EffectSpec` instances on one context.

    Lifecycle:
      * Construct with a ``moderngl.Context`` (must already be current on
        the calling thread).
      * Call :meth:`run` any number of times.
      * Call :meth:`release` from the context-owning thread before the
        context is destroyed.

    The runner keeps up to two scratch textures for separable two-pass
    effects (H → V ping-pong). They are rebuilt on demand when the
    requested size changes.
    """

    def __init__(self, ctx: "moderngl.Context", max_cache_size: int = 32) -> None:
        """Create the runner on ``ctx`` (must be current on this thread).

        Args:
            ctx: The moderngl context all programs/textures/VAOs will be
                created on. Objects never leave this context (share-group
                rule — only texture ``glo`` ids cross).
            max_cache_size: LRU cap for compiled programs; the oldest
                entry is evicted (and released) once exceeded.
        """
        self._ctx = ctx
        self._max_cache_size = max_cache_size

        # Program cache: key → moderngl.Program.
        # key = (effect_type, source_hash, structural_param_values_tuple)
        self._program_cache: Dict[Tuple, Any] = {}
        self._program_order: List[Tuple] = []  # for LRU eviction

        # FX-M07: fallback programs — last known-good per effect_type.
        # When a recompile fails (e.g. plugin updated with broken shader),
        # the runner returns the fallback instead of None (no-op).
        self._fallback_programs: Dict[str, Tuple] = {}
        self._compile_failures: set[str] = set()

        # Shared fullscreen-quad geometry.
        self._quad_vbo = ctx.buffer(_QUAD_VERTICES.tobytes())
        self._vao_cache: Dict[int, Any] = {}  # program id → VAO

        # Scratch textures for intermediate passes (lazily sized).
        # scratch_a/b are the ping-pong pair; scratch_c is the feedback-safe
        # output reserved for glow's pass 2 when the input is scratch_a
        # (FX-M02: glow-on-glow / glow-after-scratch_a feedback loop).
        self._scratch_a: Any = None
        self._scratch_b: Any = None
        self._scratch_c: Any = None
        self._scratch_a_fbo: Any = None
        self._scratch_b_fbo: Any = None
        self._scratch_c_fbo: Any = None
        # FX-M01: scratch cache key is (width, height) + dtype, so f1 (8-bit)
        # and f2 (HDR/RGBA16F) requests don't reuse a wrong-format surface.
        # Was (width, height) with a hard-coded f1 dtype.
        self._scratch_size: Optional[Tuple[int, int]] = None
        self._scratch_dtype: str = "f1"

        # FX-EXPORT: 最近一次 run() 的输出纹理对象（与返回的 glo 同源）。
        # run() 仅返回裸 glo；需要 Texture 对象（.use()）的调用方
        # （导出渲染器效果链）在 run() 之后立即读取本属性。
        self._last_output_tex: Optional[Any] = None

        # T-P1-21 batch 2: fast-tier (下采样质量档) state — a trivial blit
        # program plus a dedicated half-res ping-pong pair. Both are lazy:
        # allocated on the first fast-tier run and reused while the size
        # and dtype stay stable. The ds pair is separate from the full-res
        # scratch pool because rendering into a sub-rectangle of a
        # full-res scratch would let the pass shaders read the undefined
        # region outside the viewport (u_resolution spans the whole uv
        # space, not the viewport).
        self._blit_program: Optional[Any] = None
        self._blit_vao: Optional[Any] = None
        # Fast-tier final combine (mask/mix against the pristine full-res
        # input); sibling of the blit program, compiled lazily.
        self._combine_program: Optional[Any] = None
        self._combine_vao: Optional[Any] = None
        self._ds_a: Any = None
        self._ds_b: Any = None
        self._ds_a_fbo: Any = None
        self._ds_b_fbo: Any = None
        self._ds_size: Optional[Tuple[int, int]] = None
        self._ds_dtype: str = "f1"

        self._lock = threading.RLock()

    # ── Public API ───────────────────────────────────────────────────
    def run(
        self,
        spec: EffectSpec,
        input_tex_glo: int,
        params: Dict[str, Any],
        width: int,
        height: int,
        *,
        dtype: str = "f1",
    ) -> int:
        """Execute ``spec`` on ``input_tex_glo`` and return the output glo.

        The returned glo is owned by the runner's scratch pool and is valid
        only until the next :meth:`run` on the same context (the scratch
        may be reused). Callers must read/use it immediately.

        dtype: Texture data type for intermediate scratch surfaces.
            ``"f1"`` = RGBA8 (SDR default), ``"f2"`` = RGBA16F (HDR/float).
            Callers processing HDR or float-precision sources should pass
            ``"f2"`` to avoid 8-bit quantization banding in the effect chain.

        On any shader/compile error the runner logs and returns the input
        ``input_tex_glo`` unchanged (effect becomes a no-op). This mirrors
        the ``EditGpuPreviewRenderer._grade_program`` failure policy: never
        crash the render thread over a single effect.

        Args:
            dtype: the scratch-surface precision to allocate — ``"f1"`` for
                8-bit SDR (the Edit preview path, whose upstream track/grade
                textures are 8-bit) or ``"f2"`` for RGBA16F HDR (the Composite
                path, whose upstream base texture is already ``rgba16f``).
                FX-M01: prior to this argument the scratch was hard-coded to
                ``"f1"``, which clamped/quantised HDR input to 8-bit at the
                first effect pass and introduced banding. The scratch cache
                key includes dtype, so mixing precision levels on one runner
                does not reuse a wrong-format surface.
        """
        if width <= 0 or height <= 0 or input_tex_glo <= 0:
            return input_tex_glo
        self._last_output_tex = None

        try:
            # Effects with non-standard binding/pipeline get a dedicated
            # path. Everything else dispatches by shader_kind.
            if spec.dispatch_key == "glow":
                return self._run_glow(spec, input_tex_glo, params, width, height, dtype=dtype)
            if spec.dispatch_key == "erode_dilate":
                return self._run_erode_dilate(spec, input_tex_glo, params, width, height, dtype=dtype)
            if spec.dispatch_key == "blur":
                return self._run_blur(spec, input_tex_glo, params, width, height, dtype=dtype)
            if spec.dispatch_key == "defocus":
                return self._run_defocus(spec, input_tex_glo, params, width, height, dtype=dtype)
            if spec.shader_kind == ShaderKind.SEPARABLE_PASSES:
                return self._run_separable(spec, input_tex_glo, params, width, height, dtype=dtype)
            return self._run_single_pass(spec, input_tex_glo, params, width, height, dtype=dtype)
        except Exception as exc:  # noqa: BLE001 — render thread must survive
            logger.warning(
                "EffectPassRunner: effect '%s' failed (%s); returning input",
                spec.effect_type, exc,
            )
            return input_tex_glo

    def output_texture(self) -> Optional[Any]:
        """Return the Texture object behind the most recent run()'s output.

        Valid only immediately after :meth:`run` and only when the run
        actually produced a scratch surface (no-op/declined runs return the
        input glo and leave ``None`` here).
        """
        return self._last_output_tex

    def release(self) -> None:
        """Release all GPU resources. Call from the context-owning thread."""
        with self._lock:
            released_program_ids: set[int] = set()
            for prog in self._program_cache.values():
                for member in self._program_members(prog):
                    if id(member) in released_program_ids:
                        continue
                    released_program_ids.add(id(member))
                    vao = self._vao_cache.pop(id(member), None)
                    if vao is not None:
                        try:
                            vao.release()
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        member.release()
                    except Exception:  # noqa: BLE001
                        pass
            self._program_cache.clear()
            self._program_order.clear()
            self._fallback_programs.clear()
            self._compile_failures.clear()

            for vao in self._vao_cache.values():
                try:
                    vao.release()
                except Exception:  # noqa: BLE001
                    pass
            self._vao_cache.clear()

            if self._quad_vbo is not None:
                try:
                    self._quad_vbo.release()
                except Exception:  # noqa: BLE001
                    pass
                self._quad_vbo = None

            # T-P1-21 batch 2: fast-tier blit program/VAO + half-res pair.
            if self._blit_vao is not None:
                try:
                    self._blit_vao.release()
                except Exception:  # noqa: BLE001
                    pass
                self._blit_vao = None
            if self._blit_program is not None:
                try:
                    self._blit_program.release()
                except Exception:  # noqa: BLE001
                    pass
                self._blit_program = None
            if self._combine_vao is not None:
                try:
                    self._combine_vao.release()
                except Exception:  # noqa: BLE001
                    pass
                self._combine_vao = None
            if self._combine_program is not None:
                try:
                    self._combine_program.release()
                except Exception:  # noqa: BLE001
                    pass
                self._combine_program = None
            self._release_ds_scratch()

            self._release_scratch()

    # ── Program management ───────────────────────────────────────────
    def _get_program(
        self,
        spec: EffectSpec,
        structural_key: Tuple,
        *,
        shader_sources: Optional[Tuple[str, ...]] = None,
        shader_kind: Optional[str] = None,
    ) -> Any:
        """Look up or compile a program for ``spec``.

        ``structural_key`` is a tuple of the structural parameter values
        (those whose change requires a different compiled program — see
        ``EffectSpec.structural_param_names``). For Phase 1 effects there
        are no structural params, so the key is always ``()``.

        FX-M07: the cache key includes a hash of the shader source so that
        a plugin source change (catalog refresh) triggers a recompile rather
        than returning a stale program. On compile failure, the last known-
        good program (fallback) is returned instead of None — the user sees
        the previous working render rather than a silent no-op.
        """
        # Cheap source fingerprint: Python caches hash() on immutable tuples.
        sources = shader_sources or spec.shader_sources
        source_hash = hash(sources)
        cache_key = (spec.effect_type, source_hash, structural_key)
        with self._lock:
            prog = self._program_cache.get(cache_key)
            if prog is not None:
                return prog

            prog = self._compile_spec(
                spec, shader_sources=sources, shader_kind=shader_kind,
            )
            if prog is None:
                self._compile_failures.add(spec.effect_type)
                # FX-M07: fall back to last known-good program.
                fallback_key = self._fallback_programs.get(spec.effect_type)
                fallback = self._program_cache.get(fallback_key) if fallback_key else None
                if fallback is not None:
                    logger.info(
                        "EffectPassRunner: compile failed for '%s'; "
                        "using last known-good program",
                        spec.effect_type,
                    )
                    return fallback
                return None

            self._program_cache[cache_key] = prog
            self._program_order.append(cache_key)
            self._compile_failures.discard(spec.effect_type)
            # FX-M07: store as fallback for future recompile failures.
            self._fallback_programs[spec.effect_type] = cache_key
            self._evict_if_needed()
            return prog

    def _evict_if_needed(self) -> None:
        """Evict oldest programs when the cache is full. Caller holds lock."""
        while len(self._program_cache) > self._max_cache_size:
            oldest = self._program_order.pop(0)
            stale_effect_types = tuple(
                effect_type
                for effect_type, fallback_key in self._fallback_programs.items()
                if fallback_key == oldest
            )
            for effect_type in stale_effect_types:
                del self._fallback_programs[effect_type]
            prog = self._program_cache.pop(oldest, None)
            if prog is not None:
                self._release_cached_program(prog)

    @staticmethod
    def _program_members(program: Any) -> Tuple[Any, ...]:
        """Flatten a single or separable-pair cache value."""
        return tuple(program) if isinstance(program, tuple) else (program,)

    def _release_cached_program(self, program: Any) -> None:
        """Release a cached program value and every VAO bound to it."""
        for member in self._program_members(program):
            vao = self._vao_cache.pop(id(member), None)
            if vao is not None:
                try:
                    vao.release()
                except Exception:  # noqa: BLE001
                    pass
            try:
                member.release()
            except Exception:  # noqa: BLE001
                pass

    def get_compile_failures(self) -> frozenset[str]:
        """Return effect types whose latest compile attempt failed."""
        return frozenset(self._compile_failures)

    def _compile_spec(
        self,
        spec: EffectSpec,
        *,
        shader_sources: Optional[Tuple[str, ...]] = None,
        shader_kind: Optional[str] = None,
    ) -> Any:
        """Compile the spec's fragment source(s) into a moderngl program.

        For SEPARABLE_PASSES we compile both shaders up front (they're a
        matched pair) and return them as a tuple; for SINGLE_PASS we return
        a single program. The caller dispatches on the tuple-ness.
        """

        kind = shader_kind or spec.shader_kind
        sources = shader_sources or spec.shader_sources
        if kind == ShaderKind.SEPARABLE_PASSES:
            h_frag, v_frag = sources
            prog_h: Any | None = None
            try:
                prog_h = self._ctx.program(
                    vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=h_frag,
                )
                prog_v = self._ctx.program(
                    vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=v_frag,
                )
                return (prog_h, prog_v)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EffectPassRunner: failed to compile separable shaders "
                    "for '%s': %s", spec.effect_type, exc,
                )
                # 上报诊断（仅 effect_type + 运行时报错，不含路径/源码）。
                record_compile_failure(
                    source=SOURCE_EFFECT_PASS,
                    code=CODE_EFFECT_PASS_COMPILE_FAILED,
                    subjects=(spec.effect_type,),
                    message=str(exc),
                )
                if prog_h is not None:
                    try:
                        prog_h.release()
                    except Exception:  # noqa: BLE001
                        pass
                return None

        # SINGLE_PASS / DCTL_PROCESS (the wrapped dctl source is already a
        # complete fragment shader).
        frag = sources[0]
        try:
            return self._ctx.program(
                vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=frag,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EffectPassRunner: failed to compile shader for '%s': %s",
                spec.effect_type, exc,
            )
            # 上报诊断（仅 effect_type + 运行时报错，不含路径/源码）。
            record_compile_failure(
                source=SOURCE_EFFECT_PASS,
                code=CODE_EFFECT_PASS_COMPILE_FAILED,
                subjects=(spec.effect_type,),
                message=str(exc),
            )
            return None

    # ── Rendering primitives ─────────────────────────────────────────
    def _render_quad(self, program: Any) -> None:
        """Render a fullscreen quad with ``program``.

        VAOs are cached per-program (moderngl requires the VAO be created
        from the same context as the program, and re-creating per frame is
        wasteful).
        """
        import moderngl
        prog_id = id(program)
        vao = self._vao_cache.get(prog_id)
        if vao is None:
            vao = self._ctx.vertex_array(
                program, [(self._quad_vbo, "2f 2f", "in_vert", "in_uv")],
            )
            self._vao_cache[prog_id] = vao
        vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

    def _bind_external_texture(self, texture_glo: int, unit: int = 0) -> None:
        """Bind an external texture (by glo) to a texture unit (default 0).

        Two binding paths, picked by which GL context is current:

        * **Production (Qt share-group path)** — when a Qt ``QOpenGLContext``
          is current, prefer ``QOpenGLFunctions_4_1_Core``. The input glo
          typically comes from a decoder/upstream texture created on a
          sibling context in the share group; we don't own a
          ``moderngl.Texture`` wrapper for it, so raw GL binding is required.
        * **Standalone moderngl context (tests / no Qt)** — fall back to
          PyOpenGL direct calls. ``initializeOpenGLFunctions()`` would
          segfault without a Qt context, so we must not call it there.
          This mirrors ``vt_zero_copy_probe._bind_external_texture`` and is
          what makes ``EffectPassRunner`` drivable from headless GL
          golden-frame tests (FX-M08).

        Both paths bind ``GL_TEXTURE_2D`` to the requested unit and are
        equivalent on any conformant desktop GL; the split exists only
        because Qt's GL function loader cannot initialise against a bare
        moderngl context.
        """
        # Path 1: Qt context is current (production). initializeOpenGLFunctions
        # is only safe to call here — without a current Qt context it segfaults.
        qt_bound = False
        try:
            from PyQt6.QtGui import QOpenGLContext
            if QOpenGLContext.currentContext() is not None:
                from PyQt6.QtOpenGL import QOpenGLFunctions_4_1_Core
                gl = QOpenGLFunctions_4_1_Core()
                if gl.initializeOpenGLFunctions():
                    gl.glActiveTexture(0x84C0 + unit)  # GL_TEXTURE0 + unit
                    gl.glBindTexture(0x0DE1, texture_glo)  # GL_TEXTURE_2D
                    qt_bound = True
        except Exception as exc:  # noqa: BLE001 — fall through to PyOpenGL
            logger.debug(
                "EffectPassRunner: Qt texture bind unavailable (%s); "
                "using PyOpenGL fallback", exc,
            )

        if qt_bound:
            return

        # Path 2: standalone moderngl context (tests). PyOpenGL is a first-class
        # project dependency (see requirements.txt) and works on a bare moderngl
        # context without a Qt surface.
        from OpenGL.GL import (
            glActiveTexture, glBindTexture, GL_TEXTURE0, GL_TEXTURE_2D,
        )
        glActiveTexture(GL_TEXTURE0 + unit)
        glBindTexture(GL_TEXTURE_2D, texture_glo)

    @staticmethod
    def _set_uniform(program: Any, name: str, value: Any) -> None:
        """Set ``program[name] = value`` if the uniform exists.

        GLSL compilers are free to eliminate uniforms that the fragment
        shader doesn't ultimately use, so absence is benign — log at debug
        and move on. Mirrors composite ``_set_uniform_if_present``.
        """
        try:
            program[name].value = value
        except KeyError:
            logger.debug("EffectPassRunner: uniform '%s' optimized out", name)
        except Exception as exc:  # noqa: BLE001 — wrong-type values, etc.
            logger.debug(
                "EffectPassRunner: failed to set uniform '%s' to %r: %s",
                name, value, exc,
            )

    # ── Scratch management ───────────────────────────────────────────
    def _ensure_scratch(self, width: int, height: int, dtype: str = "f1") -> None:
        """Get or rebuild the scratch textures/FBOs at the given size + dtype.

        Allocates three scratch surfaces: the ping-pong pair (a/b) used by
        single-pass / separable / erode paths, plus a third (c) reserved as
        the feedback-safe output for glow's pass 2 when its input is scratch_a
        (FX-M02: glow-on-glow otherwise reads+writes scratch_a → black frame).

        FX-M01: ``dtype`` selects the precision of intermediate surfaces.
        ``"f1"`` = RGBA8 (SDR), ``"f2"`` = RGBA16F (HDR/float). The cache key
        is ``(width, height)`` plus dtype, so the runner can serve both 8-bit
        and HDR requests on one context without reusing a wrong-format surface.
        When the requested dtype (or size) differs from the current allocation,
        the scratch is rebuilt (the old one is released). Prior to FX-M01 dtype
        was hard-coded to ``f1``, clamping HDR input to 8-bit.
        """
        desired = (width, height)
        if (self._scratch_size == desired and self._scratch_dtype == dtype
                and self._scratch_a is not None):
            return
        self._release_scratch()
        self._scratch_a = self._ctx.texture((width, height), components=4, dtype=dtype)
        self._scratch_a_fbo = self._ctx.framebuffer(color_attachments=[self._scratch_a])
        self._scratch_b = self._ctx.texture((width, height), components=4, dtype=dtype)
        self._scratch_b_fbo = self._ctx.framebuffer(color_attachments=[self._scratch_b])
        self._scratch_c = self._ctx.texture((width, height), components=4, dtype=dtype)
        self._scratch_c_fbo = self._ctx.framebuffer(color_attachments=[self._scratch_c])
        self._scratch_size = desired
        self._scratch_dtype = dtype

    def _release_scratch(self) -> None:
        """Release the full-res scratch textures/FBOs (best-effort).

        Idempotent and exception-safe: individual release failures are
        swallowed (the context may already be tearing down). The dtype is
        intentionally retained so a rebuild-after-release keeps the
        last-used precision.
        """
        for attr in ("_scratch_a", "_scratch_a_fbo", "_scratch_b", "_scratch_b_fbo",
                      "_scratch_c", "_scratch_c_fbo"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)
        self._scratch_size = None
        # _scratch_dtype intentionally retained so a rebuild-after-release
        # keeps the last-used precision (consistent with _ensure_scratch).

    # ── Execution paths ──────────────────────────────────────────────
    def _run_blur(
        self, spec: EffectSpec, input_glo: int, params: Dict[str, Any],
        width: int, height: int, *, dtype: str = "f1",
    ) -> int:
        """Run the Blur variant selected by ``blur_type``.

        Box and Gaussian share the separable programs. Bilateral, motion and
        bokeh select their single-pass composite shader. The selected source
        and execution kind are part of the program cache key, preventing a
        type change from reusing the previous variant's program.
        """
        blur_type = params.get("blur_type", "box")
        if blur_type == "directional":
            blur_type = "motion"
        if blur_type not in {"box", "gaussian", "bilateral", "motion", "bokeh"}:
            logger.warning("Unknown blur_type=%r; falling back to box", blur_type)
            blur_type = "box"

        variant_sources = spec.sources_for_variant(blur_type)
        variant_kind = spec.shader_kind_for_variant(blur_type)
        variant_params = dict(params)
        variant_params["blur_type"] = blur_type
        if variant_kind == ShaderKind.SEPARABLE_PASSES:
            return self._run_separable(
                spec, input_glo, variant_params, width, height,
                dtype=dtype, shader_sources=variant_sources,
                shader_kind=variant_kind,
            )
        return self._run_single_pass(
            spec, input_glo, variant_params, width, height,
            dtype=dtype, shader_sources=variant_sources,
            shader_kind=variant_kind,
        )

    def _run_single_pass(
        self, spec: EffectSpec, input_glo: int, params: Dict[str, Any],
        width: int, height: int, *, dtype: str = "f1",
        shader_sources: Optional[Tuple[str, ...]] = None,
        shader_kind: Optional[str] = None,
    ) -> int:
        """Run a one-pass effect (sharpen, dctl-wrapped plugin, ...).

        Renders ``input_glo`` → a scratch texture, returns the scratch glo.

        Ping-pong output selection: when chaining single-pass effects the
        previous run's output (a scratch glo) becomes this run's input. If
        we always wrote to scratch_a, the second effect would read and write
        scratch_a simultaneously — a GL feedback loop that yields a black /
        corrupted frame (the "two sharpens → black screen" bug). So when the
        input IS scratch_a we write to scratch_b, and vice versa.
        """
        packed = self._pack_params(spec, params)
        # Build the program-cache key from the structural params' current
        # values. For specs with no structural_param_names this is ``()``
        # (one cached program); for DCTL plugins whose @param select
        # drives compile-time branches, changing the option recompiles.
        structural_key = self._structural_key_from_packed(spec, packed)
        program = self._get_program(
            spec, structural_key=structural_key,
            shader_sources=shader_sources, shader_kind=shader_kind,
        )
        if program is None:
            return input_glo

        self._ensure_scratch(width, height, dtype)
        # Pick an output scratch that differs from the input to avoid the
        # feedback loop described above.
        if input_glo == self._scratch_a.glo:
            out_tex, out_fbo = self._scratch_b, self._scratch_b_fbo
        else:
            out_tex, out_fbo = self._scratch_a, self._scratch_a_fbo

        out_fbo.use()
        self._ctx.viewport = (0, 0, width, height)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_glo)
        self._set_uniform(program, "u_texture", 0)
        # All current single-pass shaders take the resolution as a vec2;
        # set it unconditionally (harmless if absent).
        self._set_uniform(program, "u_resolution", (float(width), float(height)))
        for name, value in packed.items():
            self._set_uniform(program, name, value)

        self._render_quad(program)
        self._last_output_tex = out_tex
        return out_tex.glo

    @staticmethod
    def _structural_key_from_packed(
        spec: EffectSpec, packed: Dict[str, Any],
    ) -> tuple:
        """Build the program-cache structural key from packed uniforms.

        ``spec.structural_param_names`` lists params whose value change
        requires a different compiled program (e.g. a DCTL plugin's
        ``@param select`` that switches GLSL branches the compiler can't
        keep live). We read each one's *current packed value* (already
        translated to its uniform form — e.g. option label → int index)
        so the key is stable across i18n label changes.

        Returns ``()`` when the spec has no structural params, so
        non-DCTL specs keep the original single-program behaviour.
        """
        if not spec.structural_param_names:
            return ()
        # The wrapper names select uniforms u_<var_name>; the structural
        # param names are the bare var_names, so prefix to look up.
        return tuple(
            packed.get(f"u_{name}", packed.get(name))
            for name in spec.structural_param_names
        )

    def _run_separable(
        self, spec: EffectSpec, input_glo: int, params: Dict[str, Any],
        width: int, height: int, *, dtype: str = "f1",
        shader_sources: Optional[Tuple[str, ...]] = None,
        shader_kind: Optional[str] = None,
    ) -> int:
        """Run a two-pass separable effect (box / gaussian blur).

        Pipeline (canonical, external input):
          H pass: input → scratch_b    (no channel mask; full-colour sample)
          V pass: scratch_b → scratch_a (applies channel mask + mix + edges)

        Returns the V-pass target's glo. The H shader has no
        u_channel_mask/u_mix/u_edge_policy uniforms — only the V shader
        does — so we set the packing uniforms only on the V program.

        AUD-26401: the H/V write targets are re-picked per run so that no
        pass reads and writes the same texture. The V pass samples the H
        output (unit 0) AND the pristine input (unit 1, ``u_original``), so
        with the historical fixed b→a routing a chained input that WAS
        scratch_a made the V pass write scratch_a while sampling it on
        unit 1, and an input of scratch_b made the H pass write scratch_b
        while sampling it on unit 0 — both GL feedback loops (black /
        corrupted frame). External inputs keep the canonical b→a routing
        byte-for-byte (golden-frame parity unaffected).
        """
        packed = self._pack_params(spec, params)
        structural_key = self._structural_key_from_packed(spec, packed)
        prog_h, prog_v = self._get_program(
            spec, structural_key=structural_key,
            shader_sources=shader_sources, shader_kind=shader_kind,
        ) or (None, None)
        if prog_h is None or prog_v is None:
            return input_glo

        radius_x = packed.pop("u_radius_x")
        radius_y = packed.pop("u_radius_y")

        # Negligible radius → no visible blur, skip the work.
        if max(radius_x, radius_y) < 0.1:
            return input_glo

        # ── T-P1-21 batch 2: fast tier (下采样质量档) ──
        # quality="fast" with a large radius routes the H/V pair through a
        # half-res ping-pong: bilinear downsample → H → V (radius ×0.5 so
        # the physical blur extent is unchanged) → bilinear upsample. The
        # fragment cost is quadratic in radius, so quartering the pixel
        # count wins big once the radius clears FAST_TIER_MIN_RADIUS.
        if (str(params.get("quality", "normal")) == "fast"
                and max(radius_x, radius_y) >= FAST_TIER_MIN_RADIUS):
            return self._run_separable_downsampled(
                spec, input_glo, packed, params,
                width, height, radius_x, radius_y, dtype=dtype,
                shader_sources=shader_sources, shader_kind=shader_kind,
            )

        self._ensure_scratch(width, height, dtype)

        # AUD-26401: pick the H/V write targets at run time so no pass
        # reads and writes the same texture. Per-pass constraints:
        #   H pass: writes h, samples {input}      → h ≠ input
        #   V pass: writes v, samples {h, input}   → v ∉ {h, input}
        # (the V pass samples the input again as u_original on unit 1).
        # The external-input case keeps the canonical b → a routing.
        if input_glo == self._scratch_a.glo:
            h_tex, h_fbo = self._scratch_b, self._scratch_b_fbo
            v_tex, v_fbo = self._scratch_c, self._scratch_c_fbo
        elif input_glo == self._scratch_b.glo:
            h_tex, h_fbo = self._scratch_a, self._scratch_a_fbo
            v_tex, v_fbo = self._scratch_c, self._scratch_c_fbo
        else:
            # External texture, or a previous effect's scratch_c output —
            # aliases neither b (the H target) nor a (the V target).
            h_tex, h_fbo = self._scratch_b, self._scratch_b_fbo
            v_tex, v_fbo = self._scratch_a, self._scratch_a_fbo

        # ── Horizontal pass: input → h_tex ──
        h_fbo.use()
        self._ctx.viewport = (0, 0, width, height)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._bind_external_texture(input_glo)
        self._set_uniform(prog_h, "u_texture", 0)
        self._set_uniform(prog_h, "u_resolution", (float(width), float(height)))
        self._set_uniform(prog_h, "u_radius", radius_x)
        self._set_uniform(prog_h, "u_kernel_mode", packed.get("u_kernel_mode", 0))
        self._set_uniform(prog_h, "u_edge_policy", packed.get("u_edge_policy", 0))
        self._render_quad(prog_h)

        # ── Vertical pass: h_tex → v_tex ──
        v_fbo.use()
        self._ctx.viewport = (0, 0, width, height)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        h_tex.use(0)
        # Mask/mix reference is the pristine input on unit 1 — NOT the
        # H-pass output (that leaks one horizontal blur into masked-off
        # channels and the mix reference; the V shader samples u_original).
        self._bind_external_texture(input_glo, unit=1)
        self._set_uniform(prog_v, "u_texture", 0)
        self._set_uniform(prog_v, "u_original", 1)
        self._set_uniform(prog_v, "u_resolution", (float(width), float(height)))
        self._set_uniform(prog_v, "u_radius", radius_y)
        self._set_uniform(prog_v, "u_kernel_mode", packed.get("u_kernel_mode", 0))
        # Channel mask / mix / edge policy apply only on the final pass.
        self._set_uniform(prog_v, "u_channel_mask", packed.get("u_channel_mask", (1.0, 1.0, 1.0, 0.0)))
        self._set_uniform(prog_v, "u_mix", packed.get("u_mix", 1.0))
        self._set_uniform(prog_v, "u_edge_policy", packed.get("u_edge_policy", 0))
        self._render_quad(prog_v)

        self._last_output_tex = v_tex
        return v_tex.glo

    def _run_separable_downsampled(
        self, spec: EffectSpec, input_glo: int, packed: Dict[str, Any],
        params: Dict[str, Any], width: int, height: int,
        radius_x: float, radius_y: float, *, dtype: str = "f1",
        shader_sources: Optional[Tuple[str, ...]] = None,
        shader_kind: Optional[str] = None,
    ) -> int:
        """Fast-tier separable blur: H/V at half res, mask/mix at full res.

        Pipeline (T-P1-21 batch 2 下采样质量档):
          ds_a ← bilinear-downsample(input)
          ds_b ← H(ds_a)   radius×0.5, u_resolution = half
          ds_a ← V(ds_b)   radius×0.5, FULL mask + mix=1 (pure H+V blur)
          out  ← combine(pristine input, bilinear-upsample(ds_a)) per channel

        Quality note: this is the contract the ``fast`` tier buys its speed
        with — half-res filtering loses sub-half-pixel detail by design
        (same trade the CPU preview path makes with its radius reduction).
        ``normal``/``high`` keep the full-res path untouched. Masked-off
        channels stay bit-exact: the final combine mixes against the
        pristine full-res input, not the half-res round-trip.
        """
        prog_h, prog_v = self._get_program(
            spec, structural_key=self._structural_key_from_packed(spec, packed),
            shader_sources=shader_sources, shader_kind=shader_kind,
        ) or (None, None)
        if prog_h is None or prog_v is None:
            return input_glo
        if not self._ensure_blit_program():
            return input_glo

        dw = max(1, int(width * FAST_TIER_SCALE))
        dh = max(1, int(height * FAST_TIER_SCALE))
        self._ensure_scratch(width, height, dtype)
        self._ensure_ds_scratch(dw, dh, dtype)

        # 1) downsample: input → ds_a
        self._ds_a_fbo.use()
        self._ctx.viewport = (0, 0, dw, dh)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._bind_external_texture(input_glo)
        self._blit_draw()

        # 2) H pass at half res: ds_a → ds_b
        self._ds_b_fbo.use()
        self._ctx.viewport = (0, 0, dw, dh)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._ds_a.use(0)
        self._set_uniform(prog_h, "u_texture", 0)
        self._set_uniform(prog_h, "u_resolution", (float(dw), float(dh)))
        self._set_uniform(prog_h, "u_radius", radius_x * FAST_TIER_SCALE)
        self._set_uniform(prog_h, "u_kernel_mode", packed.get("u_kernel_mode", 0))
        self._set_uniform(prog_h, "u_edge_policy", packed.get("u_edge_policy", 0))
        self._render_quad(prog_h)

        # 3) V pass at half res: ds_b → ds_a. Pure H+V blur — full mask,
        #    mix=1. Mask/mix semantics move to the full-res combine below:
        #    applying them here would (a) still reference the pristine
        #    input only at half-res storage and (b) degrade masked-off
        #    channels through the half-res round-trip.
        self._ds_a_fbo.use()
        self._ctx.viewport = (0, 0, dw, dh)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._ds_b.use(0)
        self._bind_external_texture(input_glo, unit=1)
        self._set_uniform(prog_v, "u_texture", 0)
        self._set_uniform(prog_v, "u_original", 1)
        self._set_uniform(prog_v, "u_resolution", (float(dw), float(dh)))
        self._set_uniform(prog_v, "u_radius", radius_y * FAST_TIER_SCALE)
        self._set_uniform(prog_v, "u_kernel_mode", packed.get("u_kernel_mode", 0))
        self._set_uniform(prog_v, "u_channel_mask", (1.0, 1.0, 1.0, 1.0))
        self._set_uniform(prog_v, "u_mix", 1.0)
        self._set_uniform(prog_v, "u_edge_policy", packed.get("u_edge_policy", 0))
        self._render_quad(prog_v)

        # 4) full-res combine: mix(pristine input, upsampled blur) per
        #    channel (never reads/writes the input scratch — same aliasing
        #    rule as _run_single_pass)
        if not self._ensure_combine_program():
            # Combine can't compile — degrade to the plain blit (old
            # fast-tier semantics) rather than fail the effect.
            if input_glo == self._scratch_a.glo:
                out_tex, out_fbo = self._scratch_b, self._scratch_b_fbo
            else:
                out_tex, out_fbo = self._scratch_a, self._scratch_a_fbo
            out_fbo.use()
            self._ctx.viewport = (0, 0, width, height)
            self._ctx.clear(0.0, 0.0, 0.0, 0.0)
            self._ds_a.use(0)
            self._blit_draw()
            self._last_output_tex = out_tex
            return out_tex.glo
        if input_glo == self._scratch_a.glo:
            out_tex, out_fbo = self._scratch_b, self._scratch_b_fbo
        else:
            out_tex, out_fbo = self._scratch_a, self._scratch_a_fbo
        out_fbo.use()
        self._ctx.viewport = (0, 0, width, height)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._ds_a.use(0)
        self._bind_external_texture(input_glo, unit=1)
        self._set_uniform(self._combine_program, "u_blurred", 0)
        self._set_uniform(self._combine_program, "u_original", 1)
        self._set_uniform(self._combine_program, "u_channel_mask",
                          packed.get("u_channel_mask", (1.0, 1.0, 1.0, 0.0)))
        self._set_uniform(self._combine_program, "u_mix",
                          packed.get("u_mix", 1.0))
        import moderngl as _mgl
        self._combine_vao.render(mode=_mgl.TRIANGLE_STRIP, vertices=4)

        self._last_output_tex = out_tex
        return out_tex.glo

    # ── Special-case paths ───────────────────────────────────────────

    def _run_glow(
        self, spec: EffectSpec, input_glo: int, params: Dict[str, Any],
        width: int, height: int, *, dtype: str = "f1",
    ) -> int:
        """Run the two-pass glow/bloom effect.

        Differs from the generic separable path in two ways:
          1. Pass 1 (glow_h) thresholds + horizontal-blurs the INPUT → a
             bloom scratch picked ≠ input (no tint/mix here).
          2. Pass 2 (glow_v) samples BOTH the original input (unit 0,
             ``u_texture``) AND pass-1's output (unit 1, ``u_bloom_tex``)
             to composite bloom on top of the original. This dual-texture
             bind needs a manual ``glActiveTexture(GL_TEXTURE1)`` because
             ``_bind_external_texture`` only handles unit 0.

        Feedback-safety (FX-M02, extended by AUD-26402): BOTH pass targets
        are re-picked per run so no pass reads and writes the same texture.
        Pass 1 samples {input}, pass 2 samples {input, bloom}. The original
        FX-M02 fix only guarded pass 2 against an input aliasing scratch_a;
        AUD-26402 closes the remaining hole — a chained input that IS
        scratch_b (e.g. [single-pass → single-pass → glow], where the second
        single-pass wrote scratch_b) used to make pass 1 write scratch_b
        while sampling it, the same class of GL feedback loop (black /
        corrupted bloom). Invariant: ``output ∉ {input, bloom}`` and
        ``bloom ≠ input``. The common external-input path still uses
        bloom=scratch_b / output=scratch_a (no routing change in the hot
        path).
        """
        prog_h, prog_v = self._get_program(spec, structural_key=()) or (None, None)
        if prog_h is None or prog_v is None:
            return input_glo

        packed = self._pack_params(spec, params)
        radius = packed.get("u_radius", 15.0)
        intensity = packed.get("u_intensity", 1.5)
        mix_val = packed.get("u_mix", 1.0)
        if radius < 0.5 or intensity < 0.001 or mix_val < 0.001:
            return input_glo

        self._ensure_scratch(width, height, dtype)

        # FX-M02 + AUD-26402: allocate BOTH pass targets so neither pass
        # reads and writes the same texture. Per-pass constraints:
        #   pass 1 (bloom): writes bloom, samples {input} → bloom ≠ input
        #   pass 2: writes out, samples {input, bloom} → out ∉ {input, bloom}
        # FX-M02 guarded only pass 2 (input==scratch_a); AUD-26402 extends
        # the same rule to pass 1 for a chained input that IS scratch_b.
        if input_glo == self._scratch_a.glo:
            bloom_tex, bloom_fbo = self._scratch_b, self._scratch_b_fbo
            out_tex, out_fbo = self._scratch_c, self._scratch_c_fbo
        elif input_glo == self._scratch_b.glo:
            bloom_tex, bloom_fbo = self._scratch_a, self._scratch_a_fbo
            out_tex, out_fbo = self._scratch_c, self._scratch_c_fbo
        else:
            # External texture, or a previous effect's scratch_c output —
            # the canonical bloom=b / out=a routing is feedback-safe.
            bloom_tex, bloom_fbo = self._scratch_b, self._scratch_b_fbo
            out_tex, out_fbo = self._scratch_a, self._scratch_a_fbo

        # ── Pass 1: threshold + horizontal blur → bloom_tex ──
        bloom_fbo.use()
        self._ctx.viewport = (0, 0, width, height)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._bind_external_texture(input_glo)
        self._set_uniform(prog_h, "u_texture", 0)
        self._set_uniform(prog_h, "u_resolution", (float(width), float(height)))
        self._set_uniform(prog_h, "u_threshold", packed.get("u_threshold", 0.7))
        self._set_uniform(prog_h, "u_radius", radius)
        self._set_uniform(prog_h, "u_sample_count", packed.get("u_sample_count", 16))
        self._render_quad(prog_h)

        # ── Pass 2: vertical blur of bloom + composite on original → out_tex ──
        out_fbo.use()
        self._ctx.viewport = (0, 0, width, height)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        # unit 0 = original input
        self._bind_external_texture(input_glo)
        self._set_uniform(prog_v, "u_texture", 0)
        # unit 1 = pass-1 output (bloom_tex)。2026-09-17：改用 moderngl
        # Texture.use(unit)（与导出渲染器 blend pass 的双纹理绑定同款）。
        # 旧实现经 QOpenGLFunctions_4_1_Core 手动 glBindTexture，需要
        # 当前线程有 Qt GL 上下文——导出线程是 standalone context，
        # 直接 access violation 崩进程（pass2.5 白名单扩展时实证）。
        bloom_tex.use(1)
        self._set_uniform(prog_v, "u_bloom_tex", 1)
        self._set_uniform(prog_v, "u_resolution", (float(width), float(height)))
        self._set_uniform(prog_v, "u_radius", radius)
        self._set_uniform(prog_v, "u_intensity", intensity)
        self._set_uniform(prog_v, "u_tint", packed.get("u_tint", (1.0, 0.85, 0.7)))
        self._set_uniform(prog_v, "u_mix", mix_val)
        self._set_uniform(prog_v, "u_sample_count", packed.get("u_sample_count", 16))
        self._render_quad(prog_v)

        # unit 1 的 bloom 绑定无需显式复位：后续所有多纹理 pass
        # （blend/erode 等）渲染前都会对用到的 unit 重新 use()。

        self._last_output_tex = out_tex
        return out_tex.glo

    def _run_defocus(
        self, spec: EffectSpec, input_glo: int, params: Dict[str, Any],
        width: int, height: int, *, dtype: str = "f1",
    ) -> int:
        """Run the defocus/bokeh effect with its fast tier.

        ``mode="mipmap"`` is the node's established "faster, lower quality"
        tier (the CPU fallback already treats it as such — clamped pyramid,
        reduced radius). On the GPU path the mode previously changed
        nothing; T-P1-21 batch 2 gives it real meaning: for a large
        ``max_radius`` the disc kernel runs at half resolution between a
        bilinear down/upsample pair. ``separable`` and small radii keep
        the plain single-pass path.
        """
        max_radius = float(params.get("max_radius", 20.0))
        if (str(params.get("mode", "separable")) == "mipmap"
                and max_radius >= FAST_TIER_MIN_RADIUS):
            packed = self._pack_params(spec, params)
            program = self._get_program(spec, structural_key=())  # no structural params
            if program is not None and self._ensure_blit_program():
                return self._run_single_pass_downsampled(
                    spec, input_glo, packed, program, width, height,
                    radius_uniform="u_max_radius", dtype=dtype,
                )
        return self._run_single_pass(spec, input_glo, params, width, height, dtype=dtype)

    def _run_single_pass_downsampled(
        self, spec: EffectSpec, input_glo: int, packed: Dict[str, Any],
        program: Any, width: int, height: int, *,
        radius_uniform: str, dtype: str = "f1",
    ) -> int:
        """Half-res single-pass variant: down → effect(radius×0.5) → up.

        Shared by the defocus fast tier. The radius uniform is scaled so
        the blur extent in *texels* halves along with the surface,
        preserving the physical blur size.
        """
        dw = max(1, int(width * FAST_TIER_SCALE))
        dh = max(1, int(height * FAST_TIER_SCALE))
        self._ensure_scratch(width, height, dtype)
        self._ensure_ds_scratch(dw, dh, dtype)

        # downsample: input → ds_a
        self._ds_a_fbo.use()
        self._ctx.viewport = (0, 0, dw, dh)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._bind_external_texture(input_glo)
        self._blit_draw()

        # effect pass at half res: ds_a → ds_b
        self._ds_b_fbo.use()
        self._ctx.viewport = (0, 0, dw, dh)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._ds_a.use(0)
        self._set_uniform(program, "u_texture", 0)
        self._set_uniform(program, "u_resolution", (float(dw), float(dh)))
        if radius_uniform in packed:
            self._set_uniform(program, radius_uniform,
                              float(packed[radius_uniform]) * FAST_TIER_SCALE)
        for name, value in packed.items():
            if name != radius_uniform:
                self._set_uniform(program, name, value)
        self._render_quad(program)

        # upsample: ds_b → full-res out (≠ input scratch)
        if input_glo == self._scratch_a.glo:
            out_tex, out_fbo = self._scratch_b, self._scratch_b_fbo
        else:
            out_tex, out_fbo = self._scratch_a, self._scratch_a_fbo
        out_fbo.use()
        self._ctx.viewport = (0, 0, width, height)
        self._ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._ds_b.use(0)
        self._blit_draw()

        self._last_output_tex = out_tex
        return out_tex.glo

    # ── Fast-tier helpers (blit program + half-res scratch) ─────────
    def _ensure_blit_program(self) -> bool:
        """Compile the trivial copy program/VAO once; False on failure.

        The fast tier degrades to the full-res path when the blit can't
        compile — never fails the effect.
        """
        if self._blit_program is not None:
            return True
        prog = vbo = vao = None
        try:
            prog = self._ctx.program(
                vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=_BLIT_FRAGMENT)
            vbo = self._ctx.buffer(_QUAD_VERTICES.tobytes())
            vao = self._ctx.vertex_array(
                prog, [(vbo, "2f 2f", "in_vert", "in_uv")])
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash
            # AUD-26405: 中途失败时逆序释放已创建的 program/vbo/vao，
            # 否则每次 degrade 尝试都泄漏一份 GL 对象。
            for obj in (vao, vbo, prog):
                if obj is not None:
                    try:
                        obj.release()
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        pass
            logger.warning("EffectPassRunner: blit program failed: %s", exc)
            return False
        self._blit_program = prog
        self._blit_vao = vao
        return True

    def _blit_draw(self) -> None:
        """Draw the currently-bound unit-0 texture through the blit program."""
        import moderngl

        self._set_uniform(self._blit_program, "u_texture", 0)
        self._blit_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

    def _ensure_combine_program(self) -> bool:
        """Compile the fast-tier mask/mix combine program once; False on failure.

        Mirrors :meth:`_ensure_blit_program`; the caller degrades to the
        plain blit when this can't compile — never fails the effect.
        """
        if self._combine_program is not None:
            return True
        prog = vbo = vao = None
        try:
            prog = self._ctx.program(
                vertex_shader=_PASSTHROUGH_VERTEX,
                fragment_shader=_BLUR_FAST_COMBINE_FRAGMENT)
            vbo = self._ctx.buffer(_QUAD_VERTICES.tobytes())
            vao = self._ctx.vertex_array(
                prog, [(vbo, "2f 2f", "in_vert", "in_uv")])
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash
            # AUD-26405: 同 blit 路径——中途失败时逆序释放已创建的
            # program/vbo/vao，避免泄漏。
            for obj in (vao, vbo, prog):
                if obj is not None:
                    try:
                        obj.release()
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        pass
            logger.warning("EffectPassRunner: combine program failed: %s", exc)
            return False
        self._combine_program = prog
        self._combine_vao = vao
        return True

    def _ensure_ds_scratch(self, width: int, height: int, dtype: str = "f1") -> None:
        """Half-res ping-pong pair for the fast tier, rebuilt on size/dtype change.

        The surfaces request LINEAR filtering explicitly (upsample quality
        does not depend on the caller's texture defaults).
        """
        desired = (width, height)
        if (self._ds_size == desired and self._ds_dtype == dtype
                and self._ds_a is not None):
            return
        self._release_ds_scratch()
        import moderngl

        linear = (moderngl.LINEAR, moderngl.LINEAR)
        self._ds_a = self._ctx.texture((width, height), components=4, dtype=dtype)
        self._ds_a.filter = linear
        self._ds_b = self._ctx.texture((width, height), components=4, dtype=dtype)
        self._ds_b.filter = linear
        self._ds_a_fbo = self._ctx.framebuffer(color_attachments=[self._ds_a])
        self._ds_b_fbo = self._ctx.framebuffer(color_attachments=[self._ds_b])
        self._ds_size = desired
        self._ds_dtype = dtype

    def _release_ds_scratch(self) -> None:
        """Release the half-res fast-tier ping-pong pair (best-effort, idempotent)."""
        for attr in ("_ds_a", "_ds_a_fbo", "_ds_b", "_ds_b_fbo"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)
        self._ds_size = None

    def _run_erode_dilate(
        self, spec: EffectSpec, input_glo: int, params: Dict[str, Any],
        width: int, height: int, *, dtype: str = "f1",
    ) -> int:
        """Run erode/dilate morphology with iteration loop.

        Each iteration reads the previous iteration's output and writes
        to the other scratch texture (ping-pong). After N iterations the
        result is in scratch_a (we swap at the end if iterations is even
        so the caller always gets scratch_a).

        Fragment-only — the composite evaluator has a compute-shader path
        for size > 16 that we deliberately don't replicate; large sizes
        will be slower but still correct.
        """
        program = self._get_program(spec, structural_key=())
        if program is None:
            return input_glo

        packed = self._pack_params(spec, params)
        iterations = max(1, int(packed.pop("_iterations", 1)))
        size = packed.get("u_size", 3)
        if size < 1:
            return input_glo

        self._ensure_scratch(width, height, dtype)

        # Ping-pong starting target: when chaining erode_dilate (or after
        # another effect whose output is scratch_a), the input IS scratch_a.
        # Writing iteration 0 to scratch_a then would feedback-loop. So if
        # the input is scratch_a we start writing to scratch_b and flip the
        # even/odd mapping; the final-return parity is recomputed below.
        start_on_b = (input_glo == self._scratch_a.glo)

        current_input_glo = input_glo
        last_write_glo = input_glo
        for i in range(iterations):
            # Alternate write target each iteration, starting from the
            # feedback-safe scratch chosen above.
            write_on_b = (i % 2 == 0) == start_on_b
            if write_on_b:
                write_fbo = self._scratch_b_fbo
                write_glo = self._scratch_b.glo
            else:
                write_fbo = self._scratch_a_fbo
                write_glo = self._scratch_a.glo

            write_fbo.use()
            self._ctx.viewport = (0, 0, width, height)
            self._ctx.clear(0.0, 0.0, 0.0, 0.0)
            self._bind_external_texture(current_input_glo)
            self._set_uniform(program, "u_texture", 0)
            for name, value in packed.items():
                self._set_uniform(program, name, value)
            self._set_uniform(program, "u_resolution", (float(width), float(height)))
            self._render_quad(program)

            current_input_glo = write_glo
            last_write_glo = write_glo

        # Return whichever scratch received the last write. The caller's
        # contract is "read the returned glo immediately"; it doesn't care
        # which scratch it came from.
        self._last_output_tex = (
            self._scratch_b if last_write_glo == self._scratch_b.glo
            else self._scratch_a)
        return last_write_glo

    # ── Param packing ────────────────────────────────────────────────
    def _pack_params(self, spec: EffectSpec, params: Dict[str, Any]) -> Dict[str, Any]:
        """Translate user params to shader-ready uniforms for ``spec``.

        Looks up a packer by ``dispatch_key``; falls back to identity
        (every param flows through unchanged under its own name) for
        unregistered effects.

        DCTL plugins (``dispatch_key.startswith("plugin:")``) have no
        static packer because every plugin's param set differs. They use
        :meth:`_pack_plugin_params` which reads ``spec.param_defs`` and
        maps each value to the wrapper's ``u_{var_name}`` uniform,
        translating option labels → int indices.
        """
        if spec.dispatch_key.startswith("plugin:"):
            return self._pack_plugin_params(spec, params)
        packer = _PARAM_PACKERS.get(spec.dispatch_key)
        if packer is None:
            return dict(params)
        return packer(params)

    @staticmethod
    def _pack_plugin_params(
        spec: EffectSpec, params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generic packer for DCTL (.tglsl) plugin specs.

        The wrapper (``dctl_wrapper.wrap_dctl_process``) declares each
        ``@param`` as ``uniform <type> u_{var_name};``. Here we translate
        the user-facing param values (keyed by ParamDef.name, which
        equals var_name) into those uniforms, applying ptype-specific
        conversions:

          * ``float`` → float as-is
          * ``bool``  → int (0/1)
          * ``option``→ int index of the label in ``pdef.labels``
          * ``color`` → currently stored as a float placeholder; the
            wrapper still declares a vec3 uniform, so we expand to
            (v, v, v). A proper color row (Phase 3+) would supply a
            true (r,g,b) here.
        """
        packed: Dict[str, Any] = {}
        for pdef in spec.param_defs:
            value = params.get(pdef.name, pdef.default)
            uniform = f"u_{pdef.name}"
            if pdef.ptype == "float":
                packed[uniform] = float(value) if value is not None else 0.0
            elif pdef.ptype == "bool":
                packed[uniform] = 1 if bool(value) else 0
            elif pdef.ptype == "option":
                labels = pdef.labels or []
                if isinstance(value, str):
                    idx = labels.index(value) if value in labels else 0
                else:
                    idx = int(value) if value is not None else 0
                packed[uniform] = idx
            elif pdef.ptype == "int":
                packed[uniform] = int(value) if value is not None else 0
            elif pdef.ptype == "color":
                # Wrapper declares ``uniform vec3 u_<var>``. The catalog
                # keeps the default as a (r,g,b) tuple; honour that here
                # so moderngl gets a length-3 value (a scalar would raise
                # and be silently dropped by _set_uniform, killing the
                # param). Tolerate partial / scalar input by padding.
                if isinstance(value, (list, tuple)):
                    rgb = tuple(float(v) for v in value[:3])
                    while len(rgb) < 3:
                        rgb = rgb + (rgb[-1],)
                    packed[uniform] = rgb
                else:
                    v = float(value) if value is not None else 0.0
                    packed[uniform] = (v, v, v)
            else:
                # Unknown ptype fallback: expand scalar to vec3.
                v = float(value) if value is not None else 0.0
                packed[uniform] = (v, v, v)
        return packed

    # ── Diagnostics ─────────────────────────────────────────────────
    def get_surface_diagnostics(self) -> Dict[str, Any]:
        """FX-M01: Return current scratch surface state for diagnostics/HUD.

        Returns a dict with: size, dtype, components, allocated (bool),
        program_cache_size, vao_cache_size.
        """
        return {
            "size": self._scratch_size,
            "dtype": self._scratch_dtype,
            "components": 4,
            "allocated": self._scratch_a is not None,
            "texture_count": sum(
                item is not None for item in (self._scratch_a, self._scratch_b, self._scratch_c)
            ),
            "fbo_count": sum(
                item is not None
                for item in (self._scratch_a_fbo, self._scratch_b_fbo, self._scratch_c_fbo)
            ),
            "program_cache_size": len(self._program_cache),
            "vao_cache_size": len(self._vao_cache),
        }
